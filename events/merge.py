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
    EventArtist,
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
        .prefetch_related("eventartist_set__artist")
        .order_by("pk")
    )


def _move_lineup(keep: Event, remove: Event, role: str | None) -> None:
    next_order = (keep.eventartist_set.aggregate(m=Max("billing_order"))["m"] or 0) + 1
    for link in remove.eventartist_set.order_by("billing_order", "artist__name"):
        if keep.eventartist_set.filter(artist_id=link.artist_id).exists():
            continue
        EventArtist.objects.create(
            event=keep, artist_id=link.artist_id, role=role or link.role, billing_order=next_order,
            start_time=link.start_time, production_id=link.production_id,
        )
        next_order += 1
    keep.order_bill()


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
        for act in log.acts.all():  # acts seen at either show are kept
            if not existing.acts.filter(artist_id=act.artist_id).exists():
                act.log = existing
                act.save(update_fields=["log"])
        for like in log.likes.all():
            if not existing.likes.filter(user_id=like.user_id).exists():
                like.log = existing
                like.save(update_fields=["log"])
        log.delete()


def _move_setlists(keep: Event, remove: Event) -> None:
    for setlist in Setlist.objects.filter(event=remove):
        if Setlist.objects.filter(event=keep, artist_id=setlist.artist_id).exists():
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
    """mode: "festival", "support" (remove's acts open for keep) or "duplicate" (same show twice)."""
    if keep.pk == remove.pk:
        raise ValueError("Can't merge an event with itself.")
    if mode not in {"festival", "support", "duplicate"}:
        raise ValueError(f"Unknown merge mode: {mode}")

    if mode == "festival":
        _move_lineup(keep, remove, None)  # each act keeps its role: headliners stay headliners
        keep.event_type = EventType.FESTIVAL
    elif mode == "duplicate":
        _move_lineup(keep, remove, None)  # the same show entered twice: acts keep their roles
    else:
        _move_lineup(keep, remove, EventArtist.Role.SUPPORT)

    _move_setlists(keep, remove)
    _move_logs(keep, remove)
    _move_saves(keep, remove)

    for field in ("image_url", "description"):
        if not getattr(keep, field) and getattr(remove, field):
            setattr(keep, field, getattr(remove, field))
    if not keep.production_id and remove.production_id:
        keep.production_id = remove.production_id

    keep.separate_from.add(*remove.separate_from.exclude(pk=keep.pk))
    remove.delete()
    keep.save()  # recomputes an automatic title from the new lineup
    return keep


# --------------------------------------------------------------------------- duplicate artists


@transaction.atomic
def merge_artists(keep, remove):
    """Fold a duplicate artist into `keep`; everything that pointed at `remove` moves over."""
    from .models import ActLog, Production, SetlistSong, Song

    if keep.pk == remove.pk:
        raise ValueError("Can't merge a artist with itself.")

    # Lineup spots: one per event.
    for link in EventArtist.objects.filter(artist=remove):
        if EventArtist.objects.filter(event_id=link.event_id, artist=keep).exists():
            link.delete()
        else:
            EventArtist.objects.filter(pk=link.pk).update(artist=keep)

    # "Acts seen": one per log, keeping a rating/note if only the duplicate had one.
    for act in ActLog.objects.filter(artist=remove):
        existing = ActLog.objects.filter(log_id=act.log_id, artist=keep).first()
        if existing:
            existing.rating = existing.rating or act.rating
            existing.note = existing.note or act.note
            existing.save()
            act.delete()
        else:
            ActLog.objects.filter(pk=act.pk).update(artist=keep)

    # Songs: same title → same song; setlist entries follow.
    for song in Song.objects.filter(artist=remove):
        twin = next((s for s in Song.objects.filter(artist=keep, title__icontains=song.title[:3])
                     if s.title.casefold() == song.title.casefold()), None)
        if twin:
            SetlistSong.objects.filter(song=song).update(song=twin)
            song.delete()
        else:
            Song.objects.filter(pk=song.pk).update(artist=keep)

    # Setlists: one per event and artist.
    for setlist in Setlist.objects.filter(artist=remove):
        if Setlist.objects.filter(event_id=setlist.event_id, artist=keep).exists():
            setlist.delete()
        else:
            Setlist.objects.filter(pk=setlist.pk).update(artist=keep)

    Production.objects.filter(artist=remove).update(artist=keep)

    for field in ("bio", "image_url"):
        if not getattr(keep, field) and getattr(remove, field):
            setattr(keep, field, getattr(remove, field))
    mbid = remove.mbid
    remove.delete()
    if not keep.mbid and mbid:
        keep.mbid = mbid
    keep.save()
    # Events whose title is the headliner's name pick up the kept spelling.
    for event in Event.objects.filter(artists=keep, title_is_auto=True):
        event.refresh_auto_title()
    return keep


def _festival_name(event: Event) -> str:
    """Comparable festival name: ignores capitals, accents and extra spaces."""
    return _normalize(event.production.title if event.production_id else event.title)


def _normalize(text: str) -> str:
    import unicodedata

    plain = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def _city(event: Event) -> str:
    return _normalize(event.venue.city) if event.venue_id else ""


def same_festival_day(event: Event) -> Event | None:
    """Another festival event with the same name, on the same date, in the same city."""
    if event.event_type != EventType.FESTIVAL or not event.date:
        return None
    name, city = _festival_name(event), _city(event)
    if not (name and city):
        return None
    candidates = Event.objects.filter(event_type=EventType.FESTIVAL, date=event.date).exclude(pk=event.pk)
    for other in candidates.select_related("production", "venue").order_by("pk"):
        if _festival_name(other) == name and _city(other) == city:
            return other
    return None


def merge_all_duplicate_festival_days() -> int:
    """Merge every group of festival days sharing name, date and city into its oldest event."""
    merged = 0
    for event in Event.objects.filter(event_type=EventType.FESTIVAL).exclude(date=None).order_by("-pk"):
        if not Event.objects.filter(pk=event.pk).exists():
            continue  # already merged away
        existing = same_festival_day(event)
        if existing and existing.pk < event.pk:
            merge_events(existing, event, mode="duplicate")
            merged += 1
    return merged


def merge_into_same_festival_day(event: Event) -> Event | None:
    """Same festival name + same day = the same event: fold `event` into the existing one."""
    existing = same_festival_day(event)
    if existing:
        return merge_events(existing, event, mode="duplicate")
    return None


def festival_days_for_photo(event: Event):
    """The other days of the same festival edition (same festival, same year), or None if it isn't one."""
    if event.event_type != EventType.FESTIVAL or not event.production_id or not event.date:
        return None
    return Event.objects.filter(production=event.production, date__year=event.date.year).exclude(pk=event.pk)


def share_photo_with_festival_days(user, event: Event) -> int:
    """Copy this day's photo to the other days of its festival edition the user may edit; returns how many."""
    from .roles import can_edit_event

    days = festival_days_for_photo(event)
    if days is None or not event.image_url:
        return 0
    updated = [d.pk for d in days if can_edit_event(user, d)]
    Event.objects.filter(pk__in=updated).update(image_url=event.image_url, image_display=event.image_display)
    return len(updated)

