"""
Create music events straight from setlist.fm setlists (any logged-in user).

One setlist becomes (or joins) an Event with its venue, tour (as a Production),
headliner and songs. Existing data is reused whenever possible so imports never
duplicate venues, tours, artists or events.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction

logger = logging.getLogger(__name__)

from . import google_places
from .models import Event, EventArtist, EventType, Production, Setlist, Venue, production_named
from .setlistfm import SetlistFmError, get_setlist, import_setlist, parse_event_date


@dataclass
class ImportResult:
    event: Event
    created: bool  # False when the setlist joined an event that already existed
    setlist_already_imported: bool = False


def _coord(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _google_venue(name: str, city: str, country: str) -> Venue | None:
    """Best-effort: the Google Places venue for a setlist.fm venue, if the top match looks right."""
    if not google_places.enabled():
        return None
    session = str(uuid.uuid4())
    try:
        suggestions = google_places.autocomplete(", ".join(b for b in [name, city, country] if b), session)
    except google_places.PlacesError:
        return None
    wanted = name.casefold()
    for suggestion in suggestions[:3]:
        main = suggestion["main"].casefold()
        if wanted in main or main in wanted:
            try:
                return google_places.venue_from_place(suggestion["place_id"], session)
            except google_places.PlacesError:
                return None
    return None


def match_venue(venue_payload: dict[str, Any]) -> Venue | None:
    """Our Venue for a setlist.fm venue: by setlist.fm id, by name+city, via Google, or created."""
    setlistfm_id = (venue_payload.get("id") or "").strip() or None
    name = (venue_payload.get("name") or "").strip()
    if not name:
        return None
    city_payload = venue_payload.get("city") or {}
    city = (city_payload.get("name") or "").strip()
    country = ((city_payload.get("country") or {}).get("code") or "").strip().upper()
    country_name = ((city_payload.get("country") or {}).get("name") or "").strip()

    if setlistfm_id:
        venue = Venue.objects.filter(setlistfm_id=setlistfm_id).first()
        if venue:
            return venue

    def remember(venue: Venue) -> Venue:
        if setlistfm_id and not venue.setlistfm_id:
            venue.setlistfm_id = setlistfm_id
            venue.save(update_fields=["setlistfm_id", "updated_at"])
        return venue

    # Compared in Python: SQLite's case-insensitive matching ignores letters like Ñ/ñ.
    for venue in Venue.objects.filter(name__icontains=name[:3]):
        if venue.name.casefold() == name.casefold() and venue.city.casefold() == city.casefold():
            return remember(venue)

    venue = _google_venue(name, city, country_name)
    if venue:
        return remember(venue)

    coords = city_payload.get("coords") or {}
    return Venue.objects.create(
        name=name[:200],
        city=city[:120],
        region=(city_payload.get("state") or "")[:120],
        country=country[:2],
        latitude=_coord(coords.get("lat")),
        longitude=_coord(coords.get("long")),
        setlistfm_id=setlistfm_id,
    )


def match_tour(tour_payload: dict[str, Any] | None, artist) -> Production | None:
    """The artist's tour with this name (tours belong to artists), created if new."""
    name = ((tour_payload or {}).get("name") or "").strip()
    if not name:
        return None
    return production_named(name, EventType.MUSIC, artist)


@transaction.atomic
def create_event_from_setlist(payload: dict[str, Any], user=None) -> ImportResult:
    """Create (or join) the music event for a setlist.fm setlist and attach its songs."""
    setlistfm_id = (payload.get("id") or "").strip()
    if not setlistfm_id:
        raise SetlistFmError("Setlist.fm response has no id.")

    existing = Setlist.objects.filter(setlistfm_id=setlistfm_id).select_related("event").first()
    if existing:
        return ImportResult(existing.event, created=False, setlist_already_imported=True)

    event_date = parse_event_date(payload.get("eventDate"))
    venue = match_venue(payload.get("venue") or {})

    # Same date at the same venue is the same show (e.g. support acts, festivals, an event added by hand).
    event = None
    if event_date and venue:
        event = Event.objects.filter(date=event_date, venue=venue).order_by("pk").first()

    created = event is None
    if created:
        event = Event.objects.create(
            title="",  # automatic: the headliner's name once the setlist adds them
            event_type=EventType.MUSIC,
            date=event_date,
            venue=venue,
            created_by=user,
        )

    setlist = import_setlist(event, payload)
    tour = match_tour(payload.get("tour"), setlist.artist)
    if tour:  # the tour is this act's, at this event
        EventArtist.objects.filter(event=event, artist=setlist.artist, production=None).update(production=tour)
    event.refresh_from_db()
    return ImportResult(event, created=created)


def import_many(setlist_ids: list[str], user=None, pause: float = 0.6) -> list[tuple[str, ImportResult | None, str]]:
    """Import several setlists; setlist.fm allows about 2 requests per second."""
    results = []
    for i, setlist_id in enumerate(setlist_ids):
        if i and pause:
            time.sleep(pause)
        try:
            results.append((setlist_id, create_event_from_setlist(get_setlist(setlist_id), user), ""))
        except SetlistFmError as e:
            results.append((setlist_id, None, str(e)))
        except Exception:  # never turn one bad setlist into a 500 for the whole import
            logger.exception("Importing setlist %s failed", setlist_id)
            results.append((setlist_id, None, "Something went wrong importing this setlist. Nothing was saved for it."))
    return results
