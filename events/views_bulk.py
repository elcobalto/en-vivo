"""Bulk import: paste "date, event name, type, venue" lines (type and venue optional), then add the lineups."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Max
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext, ngettext

from .models import Event, EventArtist, EventType, Artist, Venue, added_act_role, fit_role, normalize_name
from .roles import can_edit_event, home_place

MAX_ROWS = 200
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y")
TYPE_ALIASES = {
    **{normalize_name(v): v for v in EventType.values},
    **{normalize_name(label): value for value, label in EventType.choices},
    "concert": EventType.MUSIC, "concierto": EventType.MUSIC, "musica": EventType.MUSIC, "gig": EventType.MUSIC,
    "teatro": EventType.THEATER, "theatre": EventType.THEATER, "comedia": EventType.COMEDY,
    "stand up": EventType.COMEDY, "stand-up": EventType.COMEDY, "standup": EventType.COMEDY,
    "danza": EventType.DANCE, "ballet": EventType.DANCE, "festivales": EventType.FESTIVAL, "otro": EventType.OTHER,
}


@dataclass
class Row:
    line: int
    raw: str
    date: date | None = None
    title: str = ""
    event_type: str = EventType.MUSIC
    venue_name: str = ""  # as typed (optional)
    venue: Venue | None = None  # the venue on en vivo with that name; None with a name: a new venue
    error: str = ""


def parse_date(text: str) -> date | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def venue_named(name: str, home_city: str = "") -> Venue | None:
    """The venue on en vivo with this name (ignoring capitals and accents); the one in ``home_city`` first."""
    wanted = normalize_name(name)
    matches = [v for v in Venue.objects.filter(name__icontains=name.strip()[:3]) if normalize_name(v.name) == wanted]
    matches.sort(key=lambda v: (normalize_name(v.city) != normalize_name(home_city), v.pk))
    return matches[0] if matches else None


def parse_lines(text: str, home_city: str = "") -> list[Row]:
    """One event per line: `date, event name, type, venue` (type and venue optional; commas in names need quotes)."""
    rows = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        parts = [p.strip() for p in next(csv.reader(io.StringIO(raw), skipinitialspace=True))]
        row = Row(line=number, raw=raw.strip())
        if len(parts) < 2 or not parts[1]:
            row.error = gettext("Needs at least a date and an event name.")
        else:
            row.date = parse_date(parts[0])
            row.title = parts[1][:240]
            if row.date is None:
                row.error = gettext("Couldn't read the date “%(date)s” (use 2019-03-20 or 20-03-2019).") % {"date": parts[0]}
            if len(parts) > 2 and parts[2]:
                kind = TYPE_ALIASES.get(normalize_name(parts[2]))
                if kind:
                    row.event_type = kind
                else:
                    row.error = row.error or gettext(
                        "Unknown type “%(type)s” (music, theater, comedy, dance, festival, other).") % {"type": parts[2]}
            if len(parts) > 3 and parts[3]:
                row.venue_name = parts[3][:200]
                row.venue = venue_named(row.venue_name, home_city)
        rows.append(row)
    return rows


@login_required
def bulk_import(request: HttpRequest) -> HttpResponse:
    text = request.POST.get("lines", "") if request.method == "POST" else ""
    home_city, home_country = home_place(request.user)
    rows = parse_lines(text, home_city) if text else []
    too_many = len(rows) > MAX_ROWS

    if request.method == "POST" and request.POST.get("confirm") and rows and not too_many \
            and not any(r.error for r in rows):
        ids, reused = [], 0
        new_venues: dict[str, Venue] = {}  # a venue typed on several lines is created once
        with transaction.atomic():
            for row in rows:
                venue = row.venue
                if row.venue_name and venue is None:
                    key = normalize_name(row.venue_name)
                    if key not in new_venues:
                        new_venues[key] = Venue.objects.create(name=row.venue_name, city=home_city, country=home_country)
                    venue = new_venues[key]
                existing = next((e for e in Event.objects.filter(date=row.date, event_type=row.event_type)
                                 if normalize_name(e.title) == normalize_name(row.title)), None)
                if existing:
                    if venue and not existing.venue_id:  # the list knows where it was
                        existing.venue = venue
                        existing.save(update_fields=["venue", "updated_at"])
                    ids.append(existing.pk)
                    reused += 1
                    continue
                event = Event.objects.create(title=row.title, date=row.date, event_type=row.event_type,
                                             venue=venue, created_by=request.user)
                ids.append(event.pk)
        new = len(ids) - reused
        if reused:
            text = gettext("%(created)s, %(existing)s. Now add venues and lineups.") % {
                "created": ngettext("%(n)s event created", "%(n)s events created", new) % {"n": new},
                "existing": ngettext("%(n)s already existed", "%(n)s already existed", reused) % {"n": reused}}
        else:
            text = ngettext("%(n)s event created. Now add venues and lineups.",
                            "%(n)s events created. Now add venues and lineups.", new) % {"n": new}
        messages.success(request, text)
        return redirect(f"{reverse('events:bulk_review')}?ids={','.join(map(str, ids))}")

    return render(request, "events/bulk_import.html", {
        "text": text, "rows": rows, "has_errors": any(r.error for r in rows),
        "too_many": too_many, "max_rows": MAX_ROWS, "types": EventType.choices,
    })


def _artist_named(name: str) -> Artist:
    wanted = normalize_name(name)
    existing = next((p for p in Artist.objects.filter(name__icontains=name[:3]) if normalize_name(p.name) == wanted), None)
    return existing or Artist.objects.create(name=name[:200])


@login_required
def bulk_review(request: HttpRequest) -> HttpResponse:
    """Second step: a venue and lineup for every imported event, saved in one go."""
    raw_ids = (request.GET.get("ids") or request.POST.get("ids") or "").split(",")
    ids = [int(i) for i in raw_ids if i.strip().isdigit()][:MAX_ROWS]
    events = [e for e in Event.objects.filter(pk__in=ids).select_related("venue").order_by("date", "pk")
              if can_edit_event(request.user, e)]

    if request.method == "POST":
        updated = 0
        with transaction.atomic():
            for event in events:
                changed = False
                venue_id = (request.POST.get(f"venue_{event.pk}") or "").strip()
                if venue_id.isdigit() and int(venue_id) != event.venue_id:
                    venue = Venue.objects.filter(pk=int(venue_id)).first()
                    if venue:
                        event.venue = venue
                        event.save()
                        changed = True
                names = [n.strip() for n in (request.POST.get(f"artists_{event.pk}") or "").split(",") if n.strip()]
                if names:
                    order = (event.eventartist_set.aggregate(m=Max("billing_order"))["m"] or 0) + 1
                    for name in names:
                        artist = _artist_named(name)
                        _, created = EventArtist.objects.get_or_create(
                            event=event, artist=artist,
                            defaults={"billing_order": order,
                                      "role": fit_role("headliner", event.event_type) if order == 1
                                      else added_act_role(event.event_type)},
                        )
                        order += created
                        changed |= created
                    event.order_bill()
                updated += changed
        messages.success(request, ngettext("Updated %(n)s event.", "Updated %(n)s events.", updated) % {"n": updated})
        return redirect(f"{reverse('events:bulk_review')}?ids={','.join(str(e.pk) for e in events)}")

    rows = [{
        "event": e,
        "venue_input": format_html('<input type="hidden" class="vs-value" name="venue_{}" value="{}" />',
                                   e.pk, e.venue_id or ""),
        "lineup": ", ".join(e.eventartist_set.order_by("billing_order").values_list("artist__name", flat=True)),
    } for e in events]
    return render(request, "events/bulk_review.html", {"rows": rows, "ids": ",".join(str(e.pk) for e in events)})
