"""Possible festivals: music events with four or more acts on stage, for admins to confirm or dismiss."""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q

from .models import CREDIT_ROLES, Event, EventArtist, EventType, Production, production_named

MIN_ACTS = 4
# The labels a festival act can have; everything else becomes "none" (no label) when an event turns into a festival.
FESTIVAL_ROLES = (EventArtist.Role.HEADLINER, EventArtist.Role.HOST, EventArtist.Role.NONE)


def candidates(*, dismissed: bool = False):
    """Music events (not festivals) with at least ``MIN_ACTS`` acts on stage; ``dismissed``: the ones an admin
    said aren't festivals."""
    return (
        Event.objects.filter(event_type=EventType.MUSIC, not_festival=dismissed)
        .annotate(act_count=Count("eventartist", filter=~Q(eventartist__role__in=CREDIT_ROLES)))
        .filter(act_count__gte=MIN_ACTS)
        .select_related("venue", "production")
        .order_by("-act_count", "-date", "-pk")
    )


def suggested_name(event: Event) -> str:
    """A starting point for the festival's name: a title someone typed, else the venue and year."""
    if event.title and not event.title_is_auto:
        return event.title
    return " ".join(b for b in [event.venue.name if event.venue_id else "", str(event.date.year) if event.date else ""] if b)


@transaction.atomic
def make_festival(event: Event, name: str) -> Event:
    """Turn the event into a festival day of the festival named ``name`` (found or created).

    Acts keep Headliner and Host; every other label is removed, the way festival bills are shown."""
    event.event_type = EventType.FESTIVAL
    event.production = production_named(name, EventType.FESTIVAL) if name.strip() else None
    event.play = None
    event.not_festival = False
    event.save(update_fields=["event_type", "production", "play", "not_festival", "updated_at"])
    EventArtist.objects.filter(event=event).exclude(role__in=FESTIVAL_ROLES).update(role=EventArtist.Role.NONE)
    event.order_bill()
    event.refresh_auto_title()
    return event


def festival_names() -> list[str]:
    return list(Production.objects.filter(event_type=EventType.FESTIVAL).order_by("title").values_list("title", flat=True))
