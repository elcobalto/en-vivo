"""A person's stats: what they've seen most, rated highest, and heard — for one or more event types, or all.

Every function takes the person and the event types to count (an empty list: every event). With no person
(``owner=None``) they count everyone's diaries: the site's stats, for admins. Ratings of an act
fall back to the rating of the event it was part of, so an artist seen in a rated show without a rating of their
own still counts with the show's.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Avg, Count, F, Max, Q
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _
from django_countries import countries

from .models import ActLog, Artist, Event, EventType, Log, SetlistSong, Venue

# A category (an event type, a country) needs more events than this to be among the highest rated.
MIN_EVENTS_TO_RATE = 5
# An artist needs at least this many ratings (acts rated, or rated shows they were in) to be among the highest rated.
MIN_ARTIST_RATINGS = 5


def _by(owner, path: str) -> Q:
    """``path`` is the person (``owner``), or anyone when there's no owner."""
    return Q(**{path: owner}) if owner is not None else Q()


def logs_of(owner, types: list[str]):
    logs = Log.objects.filter(_by(owner, "user"))
    return logs.filter(event__event_type__in=types) if types else logs


def acts_of(owner, types: list[str]):
    """The acts the person saw, each with its rating (or the show's, when the act has none) as ``r``."""
    acts = ActLog.objects.filter(_by(owner, "log__user"))
    if types:
        acts = acts.filter(log__event__event_type__in=types)
    return acts.annotate(r=Coalesce("rating", "log__rating"))


# --------------------------------------------------------------------------- most seen


def seen_artists(owner, types: list[str]):
    seen = Q(act_logs__isnull=False) & _by(owner, "act_logs__log__user") & (Q(act_logs__log__event__event_type__in=types) if types else Q())
    return (
        Artist.objects.filter(seen)
        .annotate(n=Count("act_logs", filter=seen, distinct=True),
                  last_seen=Max("act_logs__log__event__date", filter=seen))
        .order_by("-n", F("last_seen").desc(nulls_last=True), "name")  # ties: most recently seen first
    )


def seen_venues(owner, types: list[str]):
    seen = Q(events__logs__isnull=False) & _by(owner, "events__logs__user") & (Q(events__event_type__in=types) if types else Q())
    return (
        Venue.objects.filter(seen)
        .annotate(n=Count("events__logs", filter=seen, distinct=True), last_seen=Max("events__date", filter=seen))
        .order_by("-n", F("last_seen").desc(nulls_last=True), "name")
    )


def seen_cities(owner, types: list[str]):
    """[{"event__venue__city", "n", "last_seen"}], most shows first."""
    logs = logs_of(owner, types).exclude(event__venue=None).exclude(event__venue__city="")
    return (
        logs.values("event__venue__city").annotate(n=Count("id"), last_seen=Max("event__date"))
        .order_by("-n", F("last_seen").desc(nulls_last=True), "event__venue__city")
    )


# --------------------------------------------------------------------------- highest rated


def rated_artists(owner, types: list[str]):
    """Artists by the person's average rating of them (act ratings, or the show's when the act has none), among
    those rated at least MIN_ARTIST_RATINGS times."""
    rows = list(
        acts_of(owner, types).exclude(r=None).values("artist")
        .annotate(avg=Avg("r"), n=Count("id"), last_seen=Max("log__event__date"))
        .filter(n__gte=MIN_ARTIST_RATINGS)
        .order_by("-avg", "-n", F("last_seen").desc(nulls_last=True))
    )
    return RatedArtists(rows)


class RatedArtists:
    """A lazy list of Artists with ``avg`` and ``n`` set, slicable (for pages) without loading them all."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def __len__(self) -> int:
        return len(self.rows)

    def count(self) -> int:
        return len(self.rows)

    def __getitem__(self, index):
        rows = self.rows[index] if isinstance(index, slice) else [self.rows[index]]
        artists = Artist.objects.in_bulk([r["artist"] for r in rows])
        found = []
        for r in rows:
            artist = artists[r["artist"]]
            artist.avg, artist.n = r["avg"], r["n"]
            found.append(artist)
        return found if isinstance(index, slice) else found[0]


@dataclass
class Category:
    """An event type or a country, with how many events the person saw in it and their average rating."""

    key: str
    label: str
    n: int
    avg: float | None
    url: str = ""


def event_types(owner) -> list[Category]:
    """Every event type the person went to, most attended first (average: the shows' own ratings)."""
    labels = dict(EventType.choices)
    rows = (logs_of(owner, []).values("event__event_type")
            .annotate(n=Count("id"), avg=Avg("rating")).order_by("-n", "event__event_type"))
    return [Category(r["event__event_type"], str(labels.get(r["event__event_type"], r["event__event_type"])),
                     r["n"], r["avg"]) for r in rows]


def artist_countries(owner) -> list[Category]:
    """Where the artists the person saw are from, most shows first (a show counts once per country)."""
    names = dict(countries)
    rows = (acts_of(owner, []).exclude(artist__country="").values("artist__country")
            .annotate(n=Count("log", distinct=True), avg=Avg("r")).order_by("-n", "artist__country"))
    return [Category(r["artist__country"], str(names.get(r["artist__country"], r["artist__country"])), r["n"],
                     r["avg"]) for r in rows]


def highest_rated(categories: list[Category], limit: int = 5) -> list[Category]:
    """The best rated categories among those with more than MIN_EVENTS_TO_RATE events."""
    rated = [c for c in categories if c.avg is not None and c.n > MIN_EVENTS_TO_RATE]
    return sorted(rated, key=lambda c: (-c.avg, -c.n))[:limit]


# --------------------------------------------------------------------------- songs


def heard(owner, types: list[str]):
    """The songs the person heard live: setlist entries (no tapes) of the acts they saw, at the shows they went to."""
    entries = SetlistSong.objects.filter(
        _by(owner, "setlist__event__logs__user"), is_tape=False,
        setlist__event__logs__acts__artist=F("setlist__artist"))
    if types:
        entries = entries.filter(setlist__event__event_type__in=types)
    return entries.distinct()


def top_songs(owner, types: list[str], limit: int = 10) -> list[dict]:
    """[{"song", "title", "artist", "cover_of", "n"}]: the most heard songs, with who wrote them when covered."""
    ids = heard(owner, types).values("pk")
    rows = (SetlistSong.objects.filter(pk__in=ids).values("song")
            .annotate(n=Count("id"), title=F("song__title"), artist=F("song__artist__name"),
                      artist_id=F("song__artist"), cover_of=F("song__cover_of_name"))
            .order_by("-n", "title"))
    return list(rows[:limit])


def song_years(owner, types: list[str]) -> dict:
    """The songs heard by the year they came out (their album's): {"bars": [{"year", "n", "height"}], "unknown"}."""
    ids = heard(owner, types).values("pk")
    entries = SetlistSong.objects.filter(pk__in=ids)
    counts = dict(entries.exclude(song__album_year=None).values_list("song__album_year")
                  .annotate(n=Count("id")).values_list("song__album_year", "n"))
    unknown = entries.filter(song__album_year=None).count()
    if not counts:
        return {"bars": [], "unknown": unknown}
    peak = max(counts.values())
    first, last = min(counts), max(counts)
    bars = [{"year": y, "n": counts.get(y, 0), "height": round(100 * counts.get(y, 0) / peak)}
            for y in range(first, last + 1)]
    return {"bars": bars, "unknown": unknown}


def has_setlists(owner, types: list[str]) -> bool:
    return heard(owner, types).exists()


# --------------------------------------------------------------------------- world map


def world(owner, types: list[str]) -> list[dict]:
    """[{"code", "id" (ISO numeric, as the world map's shapes), "name", "n"}]: the countries of the shows the person
    went to, most first."""
    rows = (logs_of(owner, types).exclude(event__venue=None).exclude(event__venue__country="")
            .values("event__venue__country").annotate(n=Count("id")).order_by("-n", "event__venue__country"))
    return [{"code": r["event__venue__country"], "id": countries.numeric(r["event__venue__country"], padded=True),
             "name": countries.name(r["event__venue__country"]), "n": r["n"]} for r in rows]


# --------------------------------------------------------------------------- pictures


def venue_images(venues) -> None:
    """Each venue's ``picture``: its own photo, or the latest show's there."""
    missing = [v for v in venues if not v.image_url]
    latest = _latest_event_images(Q(venue__in=missing)) if missing else {}
    for v in venues:
        v.picture = v.image_url or latest.get(v.pk, "")


def city_images(owner, cities: list[dict]) -> None:
    """Each city row's ``picture``: the photo of the latest show the person saw there."""
    for c in cities:
        events = (Event.objects.filter(_by(owner, "logs__user"), logs__isnull=False,
                                       venue__city__iexact=c["event__venue__city"]).distinct()
                  .select_related("venue").order_by(F("date").desc(nulls_last=True))[:5])
        c["picture"] = next((e.display_image for e in events if e.display_image), "")


def _latest_event_images(where: Q) -> dict[int, str]:
    """{venue id: the photo of its latest show that has one}."""
    found: dict[int, str] = {}
    events = Event.objects.filter(where).exclude(venue=None).order_by(F("date").desc(nulls_last=True))
    wanted = set(events.values_list("venue_id", flat=True).distinct())
    for event in events.iterator():
        if event.venue_id not in found and event.display_image:
            found[event.venue_id] = event.display_image
            if len(found) == len(wanted):
                break
    return found


RANKING_TITLES = {
    "artists": _("Most seen artists"),
    "local-artists": _("Most seen local artists"),
    "foreign-artists": _("Most seen foreign artists"),
    "rated-artists": _("Highest rated artists"),
    "venues": _("Most visited venues"),
    "cities": _("Most visited cities"),
}
