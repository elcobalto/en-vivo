"""
Photo candidates for an event, picked by a person on the "Find photo" page.

Sources (each optional, results merged in this order):
  * Google Images via the Custom Search JSON API — needs GOOGLE_SEARCH_API_KEY and
    GOOGLE_SEARCH_ENGINE_ID (a Programmable Search Engine with image search on).
  * Deezer artist pictures — no key, music events only.
  * Wikimedia Commons — no key, openly licensed photos with author/licence credit.

Nothing is downloaded or stored except the URL of the photo someone chooses.
"""
from __future__ import annotations

import hashlib
import json
import ssl
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.utils.html import strip_tags

USER_AGENT = "EnVivo/1.0 (event photo picker)"
CACHE_SECONDS = 60 * 60 * 24  # the same search within a day costs nothing


@dataclass
class Candidate:
    url: str  # full-size image, saved on the event when picked
    thumb: str  # small preview for the picker grid
    source: str  # "Google", "Deezer", "Wikimedia Commons"
    title: str = ""
    credit: str = ""  # author / licence or site, shown under the photo
    page: str = ""  # where the image comes from


class ImageSearchError(Exception):
    pass


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=8, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as e:
        try:
            message = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            message = ""
        raise ImageSearchError(f"error {e.code}: {message or e.reason}") from e
    except (URLError, TimeoutError, ValueError) as e:
        raise ImageSearchError(f"request failed: {getattr(e, 'reason', e)}") from e


def google_enabled() -> bool:
    return bool(getattr(settings, "GOOGLE_SEARCH_API_KEY", "") and getattr(settings, "GOOGLE_SEARCH_ENGINE_ID", ""))


def google_images(query: str) -> list[Candidate]:
    data = _get_json(
        "https://www.googleapis.com/customsearch/v1",
        {
            "key": settings.GOOGLE_SEARCH_API_KEY,
            "cx": settings.GOOGLE_SEARCH_ENGINE_ID,
            "q": query,
            "searchType": "image",
            "num": 10,
            "safe": "active",
            "imgSize": "large",
        },
    )
    results = []
    for item in data.get("items") or []:
        link = item.get("link") or ""
        if not link.startswith("https://"):
            continue  # http images would be blocked on our https pages
        image = item.get("image") or {}
        results.append(
            Candidate(
                url=link,
                thumb=image.get("thumbnailLink") or link,
                source="Google",
                title=item.get("title", ""),
                credit=item.get("displayLink", ""),
                page=image.get("contextLink", ""),
            )
        )
    return results


def deezer_artist_pictures(artist: str) -> list[Candidate]:
    data = _get_json("https://api.deezer.com/search/artist", {"q": artist, "limit": 5})
    wanted = artist.casefold()
    results = []
    for item in data.get("data") or []:
        picture = item.get("picture_xl") or item.get("picture_big") or ""
        if not picture or "/images/artist//" in picture:  # Deezer's blank placeholder
            continue
        if item.get("name", "").casefold() != wanted:
            continue
        results.append(
            Candidate(
                url=picture,
                thumb=item.get("picture_medium") or picture,
                source="Deezer",
                title=item.get("name", ""),
                credit="Deezer",
                page=item.get("link", ""),
            )
        )
    return results


def commons_images(query: str) -> list[Candidate]:
    data = _get_json(
        "https://commons.wikimedia.org/w/api.php",
        {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrnamespace": 6,  # files
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrlimit": 8,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "iiurlwidth": 1000,
        },
    )
    pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    results = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url") or ""
        if not url:
            continue
        meta = info.get("extmetadata") or {}
        author = strip_tags((meta.get("Artist") or {}).get("value", "")).strip()
        licence = (meta.get("LicenseShortName") or {}).get("value", "")
        results.append(
            Candidate(
                url=url,
                thumb=url,
                source="Wikimedia Commons",
                title=page.get("title", "").removeprefix("File:"),
                credit=" · ".join(b for b in [author[:80], licence] if b),
                page=info.get("descriptionurl", ""),
            )
        )
    return results


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def default_query(event) -> str:
    """“Paul McCartney Estadio Nacional 20 March 2019”: the event's name, venue and date."""
    bits = [event.title]
    if event.venue_id:
        bits.append(event.venue.name or event.venue.city)
    if event.date:
        bits.append(f"{event.date.day} {MONTHS[event.date.month - 1]} {event.date.year}")
    return " ".join(b for b in bits if b)


def google_images_url(query: str) -> str:
    """A normal Google Images search, opened in the browser (no API key needed)."""
    from urllib.parse import quote_plus

    return f"https://www.google.com/search?tbm=isch&q={quote_plus(query)}"


def find_candidates(query: str, artist: str = "") -> tuple[list[dict], list[str]]:
    """Merged candidates (as dicts, for templates/caching) and per-source error messages."""
    key = "imgsearch:" + hashlib.sha256(f"{query}|{artist}|{google_enabled()}".encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached

    candidates: list[Candidate] = []
    errors: list[str] = []
    sources = []
    if google_enabled():
        sources.append(("Google Images", lambda: google_images(query)))
    if artist:
        sources.append(("Deezer", lambda: deezer_artist_pictures(artist)))
    sources.append(("Wikimedia Commons", lambda: commons_images(artist or query)))

    for name, search in sources:
        try:
            candidates.extend(search())
        except ImageSearchError as e:
            errors.append(f"{name} {e}")

    seen, unique = set(), []
    for c in candidates:
        if c.url not in seen:
            seen.add(c.url)
            unique.append(asdict(c))
    result = (unique, errors)
    if unique:
        cache.set(key, result, CACHE_SECONDS)
    return result
