"""Theater, comedy and dance shows on several dates: one event per date, grouped in a ``Staging`` (montaje).

A play can have many stagings (the West End, Broadway, a season in Santiago…), and each staging many dates.
"Add another date" copies an event to a new date of the same staging. Editing one date can apply the same
changes to the others (the edit form asks when the staging has other dates). Dates without a photo of their
own show the staging's.
"""
from __future__ import annotations

from datetime import date

from django.db import transaction

from .models import PLAY_TYPES, RUN_TYPES, Event, EventArtist, Staging

# Event fields that "apply to all dates" may copy (never the date itself). The photo lives on the staging,
# so a date's own photo is only ever its own.
SHARED_FIELDS = ("title", "event_type", "venue", "play", "production", "description")


def other_dates(event: Event):
    """The staging's other dates, soonest first (none for one-off events)."""
    if not event or not event.staging_id:
        return Event.objects.none()
    return (Event.objects.filter(staging_id=event.staging_id).exclude(pk=event.pk).select_related("venue")
            .order_by("date", "pk"))


def can_add_dates(event: Event) -> bool:
    return event.event_type in RUN_TYPES


def new_staging(event: Event, user=None) -> Staging:
    """A staging for this event alone, taking over its photo (so dates added later share it)."""
    staging = Staging.objects.create(
        play_id=event.play_id, event_type=event.event_type, created_by=user or event.created_by,
        image_url=event.image_url, image_display=event.image_display,
    )
    event.staging = staging
    event.image_url = ""
    Event.objects.filter(pk=event.pk).update(staging=staging, image_url="")
    return staging


def place_in_staging(event: Event, user=None) -> None:
    """Keep an event's staging in step with it after an edit: a date that changed play leaves a staging of
    another play (or takes the staging along when it's its only date), and a play's date gets a staging."""
    if event.event_type not in RUN_TYPES:
        if event.staging_id:
            Event.objects.filter(pk=event.pk).update(staging=None)
            event.staging = None
        return
    staging = event.staging
    if staging and staging.play_id != event.play_id:
        if staging.dates.exclude(pk=event.pk).exists():
            Event.objects.filter(pk=event.pk).update(staging=None)
            event.staging = None
        else:
            staging.play_id = event.play_id
            staging.save(update_fields=["play", "updated_at"])
    if not event.staging_id and event.play_id and event.event_type in PLAY_TYPES:
        new_staging(event, user)


def _copy_lineup(source: Event, target: Event) -> None:
    """Make ``target``'s lineup the same as ``source``'s (labels, order, set times, tours)."""
    wanted = {link.artist_id: link for link in source.eventartist_set.all()}
    target.eventartist_set.exclude(artist_id__in=wanted).delete()
    for artist_id, link in wanted.items():
        EventArtist.objects.update_or_create(event=target, artist_id=artist_id, defaults={
            "role": link.role, "billing_order": link.billing_order, "start_time": link.start_time,
            "production": link.production})


def _add_date(source: Event, staging: Staging, on: date, user=None) -> tuple[Event, bool]:
    existing = staging.dates.filter(date=on).first()
    if existing:
        return existing, False
    copy = Event.objects.create(
        **{field: getattr(source, field) for field in SHARED_FIELDS}, title_is_auto=source.title_is_auto,
        image_display=source.image_display, date=on, staging=staging, created_by=user,
    )
    _copy_lineup(source, copy)
    copy.refresh_auto_title()
    return copy, True


@transaction.atomic
def add_date(event: Event, on: date, user=None) -> tuple[Event, bool]:
    """A copy of the event on another date (venue, lineup…) in the same staging, which gives it its photo.

    Returns (the event on that date, created). A date the staging already has returns that event."""
    staging = event.staging or new_staging(event, user)
    return _add_date(event, staging, on, user)


@transaction.atomic
def add_staging_date(staging: Staging, on: date, user=None) -> tuple[Event, bool]:
    """A new date of the staging, copied from its latest date. None when the staging has no dates to copy."""
    source = staging.dates.order_by("-date", "-pk").first()
    if source is None:
        return None, False
    return _add_date(source, staging, on, user)


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
