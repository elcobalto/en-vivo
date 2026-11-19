"""Tiny MusicBrainz lookup: an artist's country from its MusicBrainz id (best effort, no key)."""
from __future__ import annotations

import json
import ssl
import logging
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.core.cache import cache

USER_AGENT = "EnVivo/1.0 (artist and album lookup)"
logger = logging.getLogger(__name__)


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
        with urlopen(request, timeout=3, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as e:  # any network/HTTP/SSL/JSON problem: the country is just left empty
        logger.warning("MusicBrainz lookup for %s failed: %r", mbid, e)
        cache.set(key, "", 60 * 60)  # don't retry this artist for an hour
        return ""
    code = (data.get("country") or "").upper()
    if not code:
        codes = ((data.get("area") or {}).get("iso-3166-1-codes") or [])
        code = (codes[0] if codes else "").upper()
    code = code if len(code) == 2 else ""
    cache.set(key, code, 60 * 60 * 24 * 30)
    return code


def _get(path: str, params: dict) -> dict:
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    request = Request(f"https://musicbrainz.org/ws/2/{path}?{urlencode({**params, 'fmt': 'json'})}",
                      headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=6, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _quote(text: str) -> str:
    return '"' + text.replace("\\", " ").replace('"', " ") + '"'


def song_album(title: str, artist_name: str, artist_mbid: str = "") -> dict | None:
    """The earliest official studio album with this song, as {"title", "year", "mbid"}; None if none.

    Raises on network problems so callers can retry later instead of saving "no album".
    """
    artist = f"arid:{artist_mbid}" if artist_mbid else f"artist:{_quote(artist_name)}"
    data = _get("recording", {
        "query": f"recording:{_quote(title)} AND {artist} AND primarytype:album AND status:official",
        "limit": 50,
    })
    best = None
    wanted = " ".join(title.casefold().split())
    for rec in data.get("recordings") or []:
        if " ".join((rec.get("title") or "").casefold().split()) != wanted:
            continue
        for release in rec.get("releases") or []:
            group = release.get("release-group") or {}
            # Studio albums only: no live albums, compilations, soundtracks, remixes…
            if group.get("primary-type") != "Album" or group.get("secondary-types"):
                continue
            date = release.get("date") or "9999"
            if best is None or date < best[0]:
                best = (date, {"title": group.get("title") or release.get("title") or "",
                               "year": int(date[:4]) if date[:4].isdigit() and date[:4] != "9999" else None,
                               "mbid": group.get("id") or ""})
    return best[1] if best else None
