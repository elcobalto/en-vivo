"""One name per city and region: the source of truth venues and profiles are standardized with.

Venues arrive from Google, setlist.fm and people typing, and they name places differently: a show in Ñuñoa
may say "Ñuñoa" or "Santiago", the region "Región Metropolitana de Santiago" or "Santiago Metropolitan". The
``City`` and ``Region`` tables (edited in the Django admin) list each place once with the other names it goes
by; ``standardize`` turns any of those names into the standard one. A commune folded into its city (Ñuñoa into
Santiago) is kept as the venue's district.
"""
from __future__ import annotations

import unicodedata


def key(name: str) -> str:
    """How names are compared: case, accents and extra spaces don't matter ("ÑUÑOA" = "Nunoa")."""
    text = unicodedata.normalize("NFKD", " ".join((name or "").split()).casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def aliases(text: str) -> list[str]:
    """The other names in an aliases box: one per line (commas work too)."""
    return [name.strip() for line in (text or "").splitlines() for name in line.split(",") if name.strip()]


def _tables(country: str) -> tuple[dict, dict]:
    """{name key: City} and {name key: Region} for a country, every alias included."""
    from .models import City, Region

    regions = {}
    for region in Region.objects.filter(country=country):
        for name in [region.name, region.setlistfm_name, *aliases(region.aliases)]:
            if name:
                regions[key(name)] = region
    cities = {}  # name key: (City, the district it names or "")
    for city in City.objects.filter(country=country).select_related("region"):
        for name in aliases(city.districts):
            cities[key(name)] = (city, name)
        for name in [city.name, *aliases(city.aliases)]:
            cities[key(name)] = (city, "")
    return cities, regions


def standardize(country: str, city: str, region: str = "") -> tuple[str, str, str]:
    """(city, region, district) by their standard names. The district is set when the name given was one of the
    city's districts (Ñuñoa for Santiago), as the table writes it; names the tables don't know stay as they are."""
    country = str(country or "").upper()
    city, region = " ".join((city or "").split()), " ".join((region or "").split())
    if not country:
        return city, region, ""
    cities, regions = _tables(country)
    district = ""
    known_city, district = cities.get(key(city), (None, "")) if city else (None, "")
    if known_city:
        city = known_city.name
        if known_city.region:
            region = known_city.region.name
    known_region = regions.get(key(region)) if region else None
    if known_region:
        region = known_region.name
    return city, region, district


def standard_city(country: str, city: str) -> str:
    return standardize(country, city)[0]


def setlistfm_region(country: str, region: str) -> str:
    """A region as setlist.fm names it (for its state filter), from any of its names."""
    known = _tables(str(country or "").upper())[1].get(key(region)) if region else None
    return (known.setlistfm_name or known.name) if known else region


def restandardize(country: str) -> int:
    """Apply the tables again to a country's venues and profiles (after they change). Returns venues changed."""
    from .models import Profile, Venue

    changed = 0
    for venue in Venue.objects.filter(country=country):
        city, region, district = standardize(country, venue.district or venue.city, venue.region)
        if (city, region, district) != (venue.city, venue.region, venue.district):
            Venue.objects.filter(pk=venue.pk).update(city=city, region=region, district=district)
            changed += 1
    for profile in Profile.objects.filter(home_country=country):
        city, region, _ = standardize(country, profile.home_city, profile.home_region)
        if (city, region) != (profile.home_city, profile.home_region):
            Profile.objects.filter(pk=profile.pk).update(home_city=city, home_region=region)
    return changed
