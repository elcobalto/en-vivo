"""Same venue, same day: merge two events into a festival or a headliner + support show."""
from __future__ import annotations

from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .merge import merge_events, same_slot_events
from .models import Event
from .roles import can_edit_event


@dataclass
class SlotSuggestion:
    other: Event
    festival: bool  # can make one festival of both
    this_supports_other: bool  # this event's acts open for the other's
    other_supports_this: bool
    can_dismiss: bool

    @property
    def any(self) -> bool:
        return self.festival or self.this_supports_other or self.other_supports_this or self.can_dismiss


def _festival_pair(user, event: Event, other: Event) -> tuple[Event, Event] | None:
    """(keep, remove) for a festival merge: the removed event must be one the user may edit."""
    if can_edit_event(user, other):
        return event, other
    if can_edit_event(user, event):
        return other, event
    return None


def suggestions_for(user, event: Event) -> list[SlotSuggestion]:
    """What this user can do about other events at the same venue and date (empty for visitors)."""
    if not user.is_authenticated:
        return []
    results = []
    for other in same_slot_events(event):
        suggestion = SlotSuggestion(
            other=other,
            festival=_festival_pair(user, event, other) is not None,
            this_supports_other=can_edit_event(user, event),  # this event is merged away
            other_supports_this=can_edit_event(user, other),
            can_dismiss=can_edit_event(user, event) or can_edit_event(user, other),
        )
        if suggestion.any:
            results.append(suggestion)
    return results


@login_required
@require_POST
def event_merge(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk)
    other = get_object_or_404(same_slot_events(event), pk=request.POST.get("other") or 0)
    action = request.POST.get("action")

    if action == "festival":
        pair = _festival_pair(request.user, event, other)
        if not pair:
            raise PermissionDenied
        keep, remove = pair
        mode = "festival"
    elif action == "this_supports_other":
        keep, remove, mode = other, event, "support"
    elif action == "other_supports_this":
        keep, remove, mode = event, other, "support"
    else:
        messages.error(request, "Unknown action.")
        return redirect(event)

    if not can_edit_event(request.user, remove):
        raise PermissionDenied

    removed_title = remove.title
    keep = merge_events(keep, remove, mode=mode)
    if mode == "festival":
        messages.success(request, f"Merged into one festival: “{keep.title}”.")
    else:
        messages.success(request, f"“{removed_title}” is now the support act on “{keep.title}”.")
    return redirect(keep)


@login_required
@require_POST
def event_separate(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk)
    other = get_object_or_404(same_slot_events(event), pk=request.POST.get("other") or 0)
    if not (can_edit_event(request.user, event) or can_edit_event(request.user, other)):
        raise PermissionDenied
    event.separate_from.add(other)
    messages.info(request, f"Got it: “{event.title}” and “{other.title}” are separate shows.")
    return redirect(event)


# --------------------------------------------------------------------------- multi-day festivals

FESTIVAL_DAY_WINDOW = 4  # days either side that can belong to the same festival edition


def festival_days(event: Event):
    """Other festival events at this venue within a few days that aren't grouped with it yet."""
    from datetime import timedelta

    from .models import EventType

    if event.event_type != EventType.FESTIVAL or not (event.date and event.venue_id):
        return Event.objects.none()
    window = timedelta(days=FESTIVAL_DAY_WINDOW)
    others = Event.objects.filter(
        event_type=EventType.FESTIVAL,
        venue_id=event.venue_id,
        date__gte=event.date - window,
        date__lte=event.date + window,
    ).exclude(pk=event.pk).exclude(date=event.date)
    if event.production_id:
        others = others.exclude(production_id=event.production_id)
    return others.order_by("date")


def festival_suggestion(user, event: Event) -> dict | None:
    days = list(festival_days(event))
    if not days or not user.is_authenticated:
        return None
    if not (can_edit_event(user, event) or any(can_edit_event(user, d) for d in days)):
        return None
    existing = event.production or next((d.production for d in days if d.production_id), None)
    return {"days": days, "name": existing.title if existing else event.title, "existing": existing}


@login_required
@require_POST
def festival_group(request: HttpRequest, pk: int) -> HttpResponse:
    """Group nearby festival days under one festival (a Production of type festival)."""
    from .models import EventType, Production

    event = get_object_or_404(Event, pk=pk)
    suggestion = festival_suggestion(request.user, event)
    if not suggestion:
        raise PermissionDenied
    festival = suggestion["existing"]
    if not festival:
        name = (request.POST.get("name") or "").strip()[:240] or event.title
        festival = Production.objects.create(title=name, event_type=EventType.FESTIVAL)
    for day in [event, *suggestion["days"]]:
        if day.production_id != festival.pk:
            day.production = festival
            day.save()
    messages.success(request, f"Grouped {len(suggestion['days']) + 1} days as “{festival.title}”.")
    return redirect(festival)


# --------------------------------------------------------------------------- admins: festivals

def same_day_festivals(event: Event):
    """Festival events on the same day in the same city as this (non-festival) event."""
    from .models import EventType

    if event.event_type == EventType.FESTIVAL or not (event.date and event.venue_id and event.venue.city):
        return Event.objects.none()
    return (
        Event.objects.filter(event_type=EventType.FESTIVAL, date=event.date, venue__city__iexact=event.venue.city)
        .exclude(pk=event.pk).select_related("venue", "production").order_by("pk")
    )


def festival_admin_actions(user, event: Event) -> dict:
    """Admin-only festival buttons for the event page."""
    from .models import EventType
    from .roles import is_admin

    if not is_admin(user):
        return {}
    return {
        "can_make_festival": event.event_type == EventType.MUSIC and event.eventperformer_set.count() > 1,
        "festivals_same_day": list(same_day_festivals(event)),
    }


@login_required
@require_POST
def make_festival(request: HttpRequest, pk: int) -> HttpResponse:
    """Admins: turn a multi-act music event into a festival with the given name."""
    from .models import EventType, Production
    from .roles import is_admin

    event = get_object_or_404(Event, pk=pk)
    if not is_admin(request.user):
        raise PermissionDenied
    name = (request.POST.get("name") or "").strip()[:240]
    if not name:
        messages.error(request, "Give the festival a name.")
        return redirect(event)
    festival = next((p for p in Production.objects.filter(event_type=EventType.FESTIVAL, title__icontains=name[:3])
                     if p.title.casefold() == name.casefold()), None) \
        or Production.objects.create(title=name, event_type=EventType.FESTIVAL)
    event.event_type = EventType.FESTIVAL
    event.production = festival
    event.save()
    messages.success(request, f"“{event.title}” is now part of the festival “{festival.title}”.")
    return redirect(event)


@login_required
@require_POST
def add_to_festival(request: HttpRequest, pk: int) -> HttpResponse:
    """Admins: fold this show into a festival happening the same day in the same city."""
    from .roles import is_admin

    event = get_object_or_404(Event, pk=pk)
    if not is_admin(request.user):
        raise PermissionDenied
    festival_event = get_object_or_404(same_day_festivals(event), pk=request.POST.get("festival") or 0)
    title = event.title
    kept = merge_events(festival_event, event, mode="festival")
    messages.success(request, f"“{title}” was added to “{kept.title}”.")
    return redirect(kept)
