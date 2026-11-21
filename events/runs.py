"""Theater, comedy and dance shows on several dates: one event per date, sharing ``Event.run_key``.

"Add another date" copies an event to a new date. Editing one date can apply the same changes to the
others (the edit form asks when the show has other dates).
"""
from __future__ import annotations

import uuid
from datetime import date

from django.db import transaction

from .models import RUN_TYPES, Event, EventArtist

# Event fields that "apply to all dates" may copy (never the date itself).
SHARED_FIELDS = ("title", "event_type", "venue", "play", "production", "description", "image_url", "image_display")


def other_dates(event: Event):
    """The show's other dates, soonest first (none for one-off events)."""
    if not event.run_key:
        return Event.objects.none()
    return (Event.objects.filter(run_key=event.run_key).exclude(pk=event.pk).select_related("venue")
            .order_by("date", "pk"))


def can_add_dates(event: Event) -> bool:
    return event.event_type in RUN_TYPES


def _copy_lineup(source: Event, target: Event) -> None:
    """Make ``target``'s lineup the same as ``source``'s (labels, order, set times, tours)."""
    wanted = {link.artist_id: link for link in source.eventartist_set.all()}
    target.eventartist_set.exclude(artist_id__in=wanted).delete()
    for artist_id, link in wanted.items():
        EventArtist.objects.update_or_create(event=target, artist_id=artist_id, defaults={
            "role": link.role, "billing_order": link.billing_order, "start_time": link.start_time,
            "production": link.production})


@transaction.atomic
def add_date(event: Event, on: date, user=None) -> tuple[Event, bool]:
    """A copy of the event on another date (venue, lineup, photo…), linked as the same show.

    Returns (the event on that date, created). A date the show already has returns that event."""
    if not event.run_key:
        event.run_key = uuid.uuid4().hex
        event.save(update_fields=["run_key", "updated_at"])
    existing = Event.objects.filter(run_key=event.run_key, date=on).first()
    if existing:
        return existing, False
    copy = Event.objects.create(
        **{field: getattr(event, field) for field in SHARED_FIELDS}, title_is_auto=event.title_is_auto,
        date=on, run_key=event.run_key, created_by=user,
    )
    _copy_lineup(event, copy)
    copy.refresh_auto_title()
    return copy, True


@transaction.atomic
def apply_to_other_dates(event: Event, fields: list[str], lineup_changed: bool, user=None) -> int:
    """Copy the changed ``fields`` (and the lineup, if it changed) from ``event`` to its other dates the user
    may edit. Returns how many dates changed."""
    from .roles import can_edit_event

    fields = [f for f in fields if f in SHARED_FIELDS]
    if fields and "title" in fields:
        fields.append("title_is_auto")
    changed = 0
    for other in other_dates(event):
        if user is not None and not can_edit_event(user, other):
            continue
        for field in fields:
            setattr(other, field, getattr(event, field))
        if fields:
            other.save()
        if lineup_changed:
            _copy_lineup(event, other)
        other.refresh_auto_title()
        changed += bool(fields or lineup_changed)
    return changed
