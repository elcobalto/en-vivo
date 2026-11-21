"""Data for the maps on a profile's stats page: countries, the home country's regions and cities, the home city's venues.

Everything counts the person's diary entries (one per show attended) by the event's venue. Countries are matched to
the world map by their ISO numeric code; cities and venues are placed by the venues' coordinates (venues without
coordinates are counted but not drawn).
"""
from __future__ import annotations

from django.db.models import Avg, Count
from django_countries import countries

from .models import Log
from .roles import home_place


def _rows(qs, *fields):
    return list(qs.values(*fields).annotate(n=Count("id")).order_by("-n", *fields))


def map_data(owner, event_type: str = "") -> dict:
    logs = Log.objects.filter(user=owner).exclude(event__venue=None)
    if event_type:
        logs = logs.filter(event__event_type=event_type)

    by_country = _rows(logs.exclude(event__venue__country=""), "event__venue__country")
    world = [{"code": r["event__venue__country"], "id": countries.numeric(r["event__venue__country"], padded=True),
              "name": countries.name(r["event__venue__country"]), "n": r["n"]} for r in by_country]

    home_city, home_country = home_place(owner)
    # The person's home country, or where they've seen the most shows.
    country = home_country if home_country else (world[0]["code"] if world else "")
    in_country = logs.filter(event__venue__country=country) if country else logs.none()

    regions = [{"name": r["event__venue__region"], "n": r["n"]}
               for r in _rows(in_country.exclude(event__venue__region=""), "event__venue__region")]
    cities = [
        {"name": r["event__venue__city"], "n": r["n"], "lat": float(r["lat"]), "lng": float(r["lng"])}
        for r in in_country.exclude(event__venue__city="").exclude(event__venue__latitude=None)
        .values("event__venue__city").annotate(n=Count("id"), lat=Avg("event__venue__latitude"),
                                                lng=Avg("event__venue__longitude")).order_by("-n")
    ]

    # The home city if they've been to shows there, else their most visited city in that country.
    city_counts = _rows(in_country.exclude(event__venue__city=""), "event__venue__city")
    city = next((r["event__venue__city"] for r in city_counts if r["event__venue__city"].casefold() == home_city.casefold()),
                city_counts[0]["event__venue__city"] if city_counts else "")
    in_city = in_country.filter(event__venue__city__iexact=city) if city else logs.none()
    venue_rows = _rows(in_city, "event__venue", "event__venue__name", "event__venue__latitude", "event__venue__longitude")
    venues = [{"id": r["event__venue"], "name": r["event__venue__name"], "n": r["n"],
               "lat": float(r["event__venue__latitude"]), "lng": float(r["event__venue__longitude"])}
              for r in venue_rows if r["event__venue__latitude"] is not None and r["event__venue__longitude"] is not None]

    return {
        "world": world,
        "country": {"code": country, "id": countries.numeric(country, padded=True) if country else None,
                    "name": countries.name(country) if country else ""},
        "regions": regions,
        "cities": cities,
        "city": city,
        "venues": venues,
        "venues_without_place": len(venue_rows) - len(venues),
    }
