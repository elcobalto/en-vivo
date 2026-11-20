"""
Photo candidates for an event, picked by a person on the "Find photo" page.

Sources (all free, no key):
  * Deezer artist pictures — music events only.
  * Wikimedia Commons — openly licensed photos with author/licence credit.
  * Openverse (openly licensed photos, mostly Flickr) — searched from the browser by
    static/events/photo-openverse.js, because a new search there can take ~15 seconds.

Google Images has no free API any more, so the page links to a normal Google Images search and
people drag (or paste) the photo they like back in; `clean_image_link` unwraps Google's result links.

Nothing is downloaded or stored except the URL of the photo someone chooses.
"""
from __future__ import annotations

import hashlib
import json
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.utils.html import strip_tags
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy

USER_AGENT = "EnVivo/1.0 (event photo picker)"
CACHE_SECONDS = 60 * 60 * 24  # the same search within a day costs nothing
PARTIAL_CACHE_SECONDS = 60 * 10  # a source failed: keep what we got briefly, then try it again


@dataclass
class Candidate:
    url: str  # full-size image, saved on the event when picked
    thumb: str  # small preview for the picker grid
    source: str  # "Deezer", "Wikimedia Commons" (and "Openverse", added in the browser)
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


def _get_json(url: str, params: dict[str, Any], timeout: float = 8) -> dict[str, Any]:
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as e:
        try:
            message = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            message = ""
        if e.code == 429:
            raise ImageSearchError(gettext_lazy("has reached its free limit for now; try again later")) from e
        if e.code in (502, 503, 504):
            raise ImageSearchError(gettext_lazy("is busy right now; try again in a moment")) from e
        raise ImageSearchError(format_lazy(gettext_lazy("error {code}: {message}"), code=e.code,
                                           message=message or e.reason)) from e
    except (URLError, TimeoutError, ValueError) as e:
        if isinstance(getattr(e, "reason", e), TimeoutError):
            raise ImageSearchError(gettext_lazy("is taking too long to answer; try again in a moment")) from e
        raise ImageSearchError(format_lazy(gettext_lazy("request failed: {reason}"),
                                           reason=getattr(e, "reason", e))) from e


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




def default_query(event) -> str:
    """“Paul McCartney Chile 2019”: the event's name, the venue's country and the year."""
    bits = [event.title]
    if event.venue_id and event.venue.country:
        bits.append(event.venue.country.name)
    if event.date:
        bits.append(str(event.date.year))
    return " ".join(str(b) for b in bits if b)


def google_images_url(query: str) -> str:
    """A normal Google Images search, opened in the browser (no API key needed)."""
    return f"https://www.google.com/search?tbm=isch&q={quote_plus(query)}"


# Image search result links that carry the real photo address in a query parameter.
WRAPPED_IMAGE_PARAMS = {"google.": "imgurl", "bing.com": "mediaurl"}


def clean_image_link(url: str) -> str:
    """The photo's own address from a pasted or dropped link.

    A copied Google Images result (google.com/imgres?imgurl=…) or Bing result (…&mediaurl=…) becomes
    the image it points to; any other link is returned unchanged.
    """
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    for marker, param in WRAPPED_IMAGE_PARAMS.items():
        if marker in host:
            inner = (parse_qs(parts.query).get(param) or [""])[0].strip()
            if inner.startswith(("https://", "http://")):
                return inner
    return url


def is_search_page(url: str) -> bool:
    """A link to a Google/Bing results page rather than to a photo (e.g. copied from the address bar)."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return any(marker in host for marker in WRAPPED_IMAGE_PARAMS) and (
        parts.path.rstrip("/") in ("", "/search", "/imgres", "/images/search")
    )


def find_candidates(query: str, artist: str = "") -> tuple[list[dict], list[str]]:
    """Merged candidates (as dicts, for templates/caching) and per-source error messages."""
    key = "imgsearch:v2:" + hashlib.sha256(f"{query}|{artist}".encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached

    candidates: list[Candidate] = []
    errors: list[str] = []
    sources = []
    if artist:
        sources.append(("Deezer", lambda: deezer_artist_pictures(artist)))
    sources.append(("Wikimedia Commons", lambda: commons_images(artist or query)))

    def run(search):
        try:
            return search(), ""
        except ImageSearchError as e:
            return [], e  # its (lazy) message is translated below: worker threads don't share the page's language

    # In parallel, so a slow source doesn't hold up the others; results keep the order above.
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        outcomes = list(pool.map(run, [search for _, search in sources]))
    for (name, _), (found, error) in zip(sources, outcomes):
        candidates.extend(found)
        if error:
            errors.append(f"{name} {error}")

    seen, unique = set(), []
    for c in candidates:
        if c.url not in seen:
            seen.add(c.url)
            unique.append(asdict(c))
    result = (unique, errors)
    if unique:
        cache.set(key, result, PARTIAL_CACHE_SECONDS if errors else CACHE_SECONDS)
    return result
