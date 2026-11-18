"""Tiny MusicBrainz lookup: an artist's country from its MusicBrainz id (best effort, no key)."""
from __future__ import annotations

import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.core.cache import cache

USER_AGENT = "EnVivo/1.0 (artist country lookup)"


def artist_country(mbid: str) -> str:
    """ISO country code for a MusicBrainz artist, or "" if unknown or the lookup fails."""
    if not mbid:
        return ""
    key = f"mb-country:{mbid}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    try:
        request = Request(f"https://musicbrainz.org/ws/2/artist/{mbid}?fmt=json",
                          headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urlopen(request, timeout=6, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except (HTTPError, URLError, TimeoutError, ValueError):
        return ""
    code = (data.get("country") or "").upper()
    if not code:
        codes = ((data.get("area") or {}).get("iso-3166-1-codes") or [])
        code = (codes[0] if codes else "").upper()
    code = code if len(code) == 2 else ""
    cache.set(key, code, 60 * 60 * 24 * 30)
    return code
