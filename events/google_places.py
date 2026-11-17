"""
Google Places API (New) client for standardized venues.

Flow: the venue search box calls `autocomplete()` while the user types and, when a
suggestion is picked, `venue_from_place()` fetches the details once and creates (or
reuses) a Venue keyed by its Google place id. Both calls share a session token, so
Google bills them as one autocomplete session.

The key stays on the server; browsers only talk to our own endpoints.
"""
from __future__ import annotations

import json
import ssl
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction

from .models import Venue

BASE_URL = "https://places.googleapis.com/v1"
# displayName makes this a "Pro" Place Details call; the rest are basic fields.
DETAILS_FIELDS = "id,displayName,formattedAddress,addressComponents,location"


class PlacesError(Exception):
    pass


# Google's machine-readable reasons, with the fix in Google Cloud Console.
REASON_HINTS = {
    "SERVICE_DISABLED": "enable “Places API (New)” in this key's project",
    "API_KEY_SERVICE_BLOCKED": "the key's API restrictions don't include “Places API (New)”",
    "BILLING_DISABLED": "link a billing account to the project",
    "API_KEY_INVALID": "the key is wrong or was deleted",
    "API_KEY_HTTP_REFERRER_BLOCKED": "remove the website restriction; this key is used by the server",
    "API_KEY_IP_ADDRESS_BLOCKED": "remove the IP restriction; Render's outgoing IPs change",
}


def enabled() -> bool:
    return bool(getattr(settings, "GOOGLE_PLACES_API_KEY", ""))


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _call(method: str, path: str, *, body: dict | None = None, field_mask: str | None = None,
          query: dict | None = None) -> dict[str, Any]:
    if not enabled():
        raise PlacesError("Venue search isn't configured (missing GOOGLE_PLACES_API_KEY).")
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.GOOGLE_PLACES_API_KEY,
    }
    if field_mask:
        headers["X-Goog-FieldMask"] = field_mask
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urlencode({k: v for k, v in query.items() if v})}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=8,
                     context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as e:
        message, reason = "", ""
        try:
            error = json.loads(e.read().decode("utf-8")).get("error", {})
            message = error.get("message", "")
            # e.g. SERVICE_DISABLED, API_KEY_SERVICE_BLOCKED, BILLING_DISABLED
            reason = next((d.get("reason") for d in error.get("details") or [] if d.get("reason")), "")
        except Exception:
            pass
        hint = f" ({reason}{': ' + REASON_HINTS[reason] if reason in REASON_HINTS else ''})" if reason else ""
        raise PlacesError(f"Google Places error {e.code}: {message or e.reason}{hint}") from e
    except URLError as e:
        raise PlacesError(f"Google Places request failed: {e.reason}") from e


def autocomplete(text: str, session: str) -> list[dict[str, str]]:
    """Suggestions for `text` as [{place_id, main, secondary}]."""
    payload = _call(
        "POST",
        "/places:autocomplete",
        body={
            "input": text[:200],
            "sessionToken": session,
            "languageCode": settings.GOOGLE_PLACES_LANGUAGE,
        },
    )
    results = []
    for suggestion in payload.get("suggestions") or []:
        prediction = suggestion.get("placePrediction") or {}
        place_id = prediction.get("placeId")
        if not place_id:
            continue
        structured = prediction.get("structuredFormat") or {}
        results.append(
            {
                "place_id": place_id,
                "main": (structured.get("mainText") or {}).get("text") or (prediction.get("text") or {}).get("text", ""),
                "secondary": (structured.get("secondaryText") or {}).get("text", ""),
            }
        )
    return results


def place_details(place_id: str, session: str = "") -> dict[str, Any]:
    return _call(
        "GET",
        f"/places/{quote(place_id, safe='')}",
        field_mask=DETAILS_FIELDS,
        query={"sessionToken": session, "languageCode": settings.GOOGLE_PLACES_LANGUAGE},
    )


def _component(components: list[dict], *types: str, short: bool = False) -> str:
    for wanted in types:
        for component in components:
            if wanted in (component.get("types") or []):
                return (component.get("shortText" if short else "longText") or "").strip()
    return ""


def venue_fields(details: dict[str, Any]) -> dict[str, Any]:
    """Map a Place Details response onto Venue fields."""
    components = details.get("addressComponents") or []
    location = details.get("location") or {}
    fields: dict[str, Any] = {
        "name": ((details.get("displayName") or {}).get("text") or "").strip()[:200],
        "address": (details.get("formattedAddress") or "").strip()[:255],
        # Commune/municipality first (e.g. Ñuñoa), then wider areas.
        "city": _component(components, "locality", "postal_town", "administrative_area_level_3",
                           "sublocality", "administrative_area_level_2")[:120],
        "region": _component(components, "administrative_area_level_1")[:120],
        "country": _component(components, "country", short=True).upper()[:2],
    }
    if "latitude" in location and "longitude" in location:
        fields["latitude"] = Decimal(str(location["latitude"])).quantize(Decimal("0.000001"))
        fields["longitude"] = Decimal(str(location["longitude"])).quantize(Decimal("0.000001"))
    return fields


@transaction.atomic
def venue_from_place(place_id: str, session: str = "") -> Venue:
    """The Venue for a Google place: existing one if we have it, otherwise created from its details."""
    existing = Venue.objects.filter(google_place_id=place_id).first()
    if existing:
        return existing

    fields = venue_fields(place_details(place_id, session))
    if not fields["name"]:
        raise PlacesError("Google returned a place without a name.")

    # A venue typed by hand earlier with the same name and city gets standardized instead of duplicated.
    # Compared in Python: SQLite's case-insensitive matching ignores non-ASCII letters like Ñ/ñ.
    manual = next(
        (
            v
            for v in Venue.objects.filter(google_place_id__isnull=True)
            if v.name.casefold() == fields["name"].casefold() and v.city.casefold() == fields["city"].casefold()
        ),
        None,
    )
    if manual:
        return apply_place(manual, place_id, fields)
    return Venue.objects.create(google_place_id=place_id, **fields)


def apply_place(venue: Venue, place_id: str, fields: dict[str, Any] | None = None, session: str = "") -> Venue:
    """Overwrite a venue's location data with Google's (keeps capacity)."""
    clash = Venue.objects.filter(google_place_id=place_id).exclude(pk=venue.pk).first()
    if clash:
        raise PlacesError(f"That place is already linked to “{clash.name}”.")
    fields = fields or venue_fields(place_details(place_id, session))
    for key, value in fields.items():
        setattr(venue, key, value)
    venue.google_place_id = place_id
    venue.save()
    return venue
