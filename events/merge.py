"""
Merge two events that turned out to be the same show (same venue, same day).

Everything attached to the removed event moves to the kept one — lineup, setlists,
diary logs (with their likes and replies), Want to go, list entries and favourites —
then the removed event is deleted. Nothing users wrote is lost.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Max

from .models import (
    Event,
    EventListItem,
    EventPerformer,
    EventType,
    FavoriteEvent,
    Log,
    Setlist,
    WantToGo,
)


def same_slot_events(event: Event):
    """Other events at the same venue on the same day that nobody marked as separate shows."""
    if not (event.date and event.venue_id):
        return Event.objects.none()
    return (
        Event.objects.filter(date=event.date, venue_id=event.venue_id)
        .exclude(pk=event.pk)
        .exclude(separate_from=event)
        .prefetch_related("eventperformer_set__performer")
        .order_by("pk")
    )


def _move_lineup(keep: Event, remove: Event, role: str | None) -> None:
    next_order = (keep.eventperformer_set.aggregate(m=Max("billing_order"))["m"] or 0) + 1
    for link in remove.eventperformer_set.order_by("billing_order", "performer__name"):
        if keep.eventperformer_set.filter(performer_id=link.performer_id).exists():
            continue
        EventPerformer.objects.create(
            event=keep, performer_id=link.performer_id, role=role or link.role, billing_order=next_order
        )
        next_order += 1


def _move_logs(keep: Event, remove: Event) -> None:
    for log in Log.objects.filter(event=remove):
        existing = Log.objects.filter(event=keep, user_id=log.user_id).first()
        if not existing:
            Log.objects.filter(pk=log.pk).update(event=keep)
            continue
        # The same person logged both: keep one entry, filling its gaps from the other.
        existing.rating = existing.rating or log.rating
        existing.review = existing.review or log.review
        existing.liked = existing.liked or log.liked
        existing.contains_spoilers = existing.contains_spoilers or log.contains_spoilers
        existing.save()
        log.comments.update(log=existing)
        for like in log.likes.all():
            if not existing.likes.filter(user_id=like.user_id).exists():
                like.log = existing
                like.save(update_fields=["log"])
        log.delete()


def _move_setlists(keep: Event, remove: Event) -> None:
    for setlist in Setlist.objects.filter(event=remove):
        if Setlist.objects.filter(event=keep, performer_id=setlist.performer_id).exists():
            setlist.delete()
        else:
            Setlist.objects.filter(pk=setlist.pk).update(event=keep)


def _move_saves(keep: Event, remove: Event) -> None:
    for model in (WantToGo, FavoriteEvent):
        for row in model.objects.filter(event=remove):
            if model.objects.filter(event=keep, user_id=row.user_id).exists():
                row.delete()
            else:
                model.objects.filter(pk=row.pk).update(event=keep)

    for item in EventListItem.objects.filter(event=remove).select_related("event_list"):
        if EventListItem.objects.filter(event=keep, event_list_id=item.event_list_id).exists():
            event_list = item.event_list
            item.delete()
            for i, other in enumerate(event_list.items.order_by("position"), start=1):
                if other.position != i:
                    EventListItem.objects.filter(pk=other.pk).update(position=i)
        else:
            EventListItem.objects.filter(pk=item.pk).update(event=keep)


@transaction.atomic
def merge_events(keep: Event, remove: Event, *, mode: str) -> Event:
    """mode: "festival" (everyone headlines a festival) or "support" (remove's acts open for keep)."""
    if keep.pk == remove.pk:
        raise ValueError("Can't merge an event with itself.")
    if mode not in {"festival", "support"}:
        raise ValueError(f"Unknown merge mode: {mode}")

    if mode == "festival":
        _move_lineup(keep, remove, None)  # each act keeps its role: headliners stay headliners
        keep.event_type = EventType.FESTIVAL
    else:
        _move_lineup(keep, remove, EventPerformer.Role.SUPPORT)

    _move_setlists(keep, remove)
    _move_logs(keep, remove)
    _move_saves(keep, remove)

    for field in ("image_url", "description"):
        if not getattr(keep, field) and getattr(remove, field):
            setattr(keep, field, getattr(remove, field))
    if not keep.production_id and remove.production_id:
        keep.production_id = remove.production_id
    if not keep.start_time and remove.start_time:
        keep.start_time = remove.start_time

    keep.separate_from.add(*remove.separate_from.exclude(pk=keep.pk))
    remove.delete()
    keep.save()  # recomputes an automatic title from the new lineup
    return keep
