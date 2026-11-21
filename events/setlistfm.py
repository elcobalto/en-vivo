"""
Minimal setlist.fm API client + importer.

setlist.fm is only used to attach song lists to events that already exist in our
own catalog. It never creates events or venues.
"""
from __future__ import annotations

import hashlib
import json
import ssl
from datetime import date, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils.translation import gettext

from .models import Event, EventArtist, Artist, Setlist, SetlistSong, Song

FALSY = {"0", "false", "no", "off"}


class SetlistFmError(Exception):
    pass


class SetlistFmQuotaError(SetlistFmError):
    """We held back a request so En Vivo stays inside setlist.fm's rate limits."""


# Answers are reused for a while, so paging back, the page after an import and importing a setlist
# straight from search results don't cost another request.
SEARCH_CACHE_SECONDS = 60 * 15
SETLIST_CACHE_SECONDS = 60 * 60


def _setlist_cache_key(setlist_id: str) -> str:
    return f"setlistfm:setlist:{hashlib.sha1(setlist_id.encode()).hexdigest()}"


def _ssl_context() -> ssl.SSLContext:
    if str(getattr(settings, "SETLISTFM_SSL_VERIFY", "1")).strip().lower() in FALSY:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _request_json(path: str, query: dict[str, str] | None = None, *, endpoint: str) -> dict[str, Any]:
    from . import setlistfm_quota

    api_key = getattr(settings, "SETLISTFM_API_KEY", "")
    if not api_key:
        raise SetlistFmError("Missing SETLISTFM_API_KEY. Add it to your .env file.")

    base_url = getattr(settings, "SETLISTFM_API_BASE_URL", "https://api.setlist.fm").rstrip("/")
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{urlencode({k: v for k, v in query.items() if v})}"

    req = Request(
        url,
        headers={
            "x-api-key": api_key,
            "Accept": "application/json",
            "Accept-Language": "en",
            "User-Agent": "en-vivo-local/1.0",
        },
    )

    handlers: list = [HTTPSHandler(context=_ssl_context())]
    # Local intercepting proxies can break HTTPS tunnelling and cause misleading 403s.
    if str(getattr(settings, "SETLISTFM_BYPASS_PROXY", "1")).strip().lower() not in FALSY:
        handlers.insert(0, ProxyHandler({}))

    call = setlistfm_quota.reserve_call(endpoint)
    try:
        with build_opener(*handlers).open(req, timeout=20) as resp:
            setlistfm_quota.finish_call(call, resp.status)
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        setlistfm_quota.finish_call(call, e.code)
        if e.code == 404:
            return {}
        if e.code == 429:
            raise SetlistFmQuotaError(gettext("setlist.fm is getting too many requests from En Vivo. "
                                              "Please try again in a minute.")) from e
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        raise SetlistFmError(gettext("Setlist.fm error %(code)s: %(message)s") % {"code": e.code, "message": body or e.reason}) from e
    except URLError as e:
        setlistfm_quota.finish_call(call, 0)
        raise SetlistFmError(gettext("Setlist.fm request failed: %(reason)s") % {"reason": e.reason}) from e


def search_setlists(**filters) -> list[dict]:
    """One page of setlists matching the filters (see ``search_setlists_page``)."""
    return search_setlists_page(**filters)[0]


def search_setlists_page(
    *,
    artist_name: str = "",
    artist_mbid: str = "",
    on_date: date | None = None,
    page: int = 1,
    city: str = "",
    year: str = "",
    venue: str = "",
    country_code: str = "",
    tour: str = "",
) -> tuple[list[dict], int, int]:
    """(setlists on this page, total matches, page size). Cached for a while, like every answer."""
    query = {
        "artistName": artist_name,
        "artistMbid": artist_mbid,
        "p": str(page),
        "cityName": city,
        "year": year,
        "venueName": venue,
        "countryCode": country_code.upper(),
        "tourName": tour,
    }
    if on_date:
        query["date"] = on_date.strftime("%d-%m-%Y")
    query = {k: v for k, v in query.items() if v}
    from . import setlistfm_quota

    key = "setlistfm:searchpage:" + hashlib.sha1(json.dumps(query, sort_keys=True).encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        setlistfm_quota.count_cache_hit()
        return cached
    payload = _request_json("/rest/1.0/search/setlists", query, endpoint="search")
    results = payload.get("setlist") or []
    answer = (results, int(payload.get("total") or 0), int(payload.get("itemsPerPage") or 20))
    cache.set(key, answer, SEARCH_CACHE_SECONDS)
    # Search results are whole setlists: importing one of them needs no further request.
    cache.set_many({_setlist_cache_key(r["id"]): r for r in results if r.get("id")}, SETLIST_CACHE_SECONDS)
    return answer


def search_artists(name: str) -> list[dict]:
    """Artists on setlist.fm named like ``name``, best matches first: [{"mbid", "name", "disambiguation"}]."""
    name = " ".join(name.split())
    if not name:
        return []
    from . import setlistfm_quota

    key = "setlistfm:artists:" + hashlib.sha1(name.casefold().encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        setlistfm_quota.count_cache_hit()
        return cached
    payload = _request_json("/rest/1.0/search/artists", {"artistName": name, "sort": "relevance"}, endpoint="artists")
    artists = [{"mbid": a.get("mbid") or "", "name": a.get("name") or "", "disambiguation": a.get("disambiguation") or ""}
               for a in _as_list(payload.get("artist")) if isinstance(a, dict) and a.get("mbid")]
    cache.set(key, artists, SEARCH_CACHE_SECONDS)
    return artists


def get_setlist(setlist_id: str) -> dict[str, Any]:
    from . import setlistfm_quota

    key = _setlist_cache_key(setlist_id)
    payload = cache.get(key)
    if payload:
        setlistfm_quota.count_cache_hit()
        return payload
    payload = _request_json(f"/rest/1.0/setlist/{quote(setlist_id, safe='')}", endpoint="setlist")
    if not payload:
        raise SetlistFmError(gettext("Setlist not found on setlist.fm."))
    cache.set(key, payload, SETLIST_CACHE_SECONDS)
    return payload


def parse_event_date(raw: str | None) -> date | None:
    try:
        return datetime.strptime((raw or "").strip(), "%d-%m-%Y").date()
    except ValueError:
        return None


def _as_list(value) -> list:
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def _match_artist(artist_payload: dict) -> Artist:
    artist = _find_or_create_artist(artist_payload)
    if not artist.country and artist.mbid:
        from .musicbrainz import artist_country

        code = artist_country(artist.mbid)
        if code:
            artist.country = code
            artist.save(update_fields=["country", "updated_at"])
    return artist


def _find_or_create_artist(artist_payload: dict) -> Artist:
    name = (artist_payload.get("name") or "").strip() or "Unknown artist"
    mbid = (artist_payload.get("mbid") or "").strip() or None

    if mbid:
        artist = Artist.objects.filter(mbid=mbid).first()
        if artist:
            return artist

    artist = Artist.objects.filter(name__iexact=name).first()
    if artist:
        if mbid and not artist.mbid:
            artist.mbid = mbid
            artist.save(update_fields=["mbid", "updated_at"])
        return artist

    return Artist.objects.create(name=name, mbid=mbid, kind=Artist.Kind.GROUP)


def _set_name(set_payload: dict, encore_count: int) -> str:
    if set_payload.get("name"):
        return str(set_payload["name"]).strip()
    if set_payload.get("encore"):
        return "Encore" if encore_count == 1 else f"Encore {encore_count}"
    return ""


def _song_note(song_payload: dict) -> str:
    bits = []  # tapes are flagged on the entry (is_tape), not in the note
    cover = song_payload.get("cover") or {}
    if isinstance(cover, dict) and cover.get("name"):
        bits.append(f"{cover['name']} cover")
    with_ = song_payload.get("with") or {}
    if isinstance(with_, dict) and with_.get("name"):
        bits.append(f"with {with_['name']}")
    if song_payload.get("info"):
        bits.append(str(song_payload["info"]).strip())
    return "; ".join(bits)[:200]


@transaction.atomic
def import_setlist(event: Event, payload: dict[str, Any]) -> Setlist:
    """Attach (or refresh) a setlist.fm setlist on an existing event."""

    setlistfm_id = (payload.get("id") or "").strip()
    if not setlistfm_id:
        raise SetlistFmError("Setlist.fm response has no id.")

    existing = Setlist.objects.filter(setlistfm_id=setlistfm_id).exclude(event=event).first()
    if existing:
        raise SetlistFmError(gettext("This setlist is already attached to “%(event)s”.") % {"event": existing.event})

    artist = _match_artist(payload.get("artist") or {})

    if not EventArtist.objects.filter(event=event, artist=artist).exists():
        current = EventArtist.objects.filter(event=event)
        EventArtist.objects.create(
            event=event,
            artist=artist,
            role=EventArtist.Role.SUPPORT if current.exists() else EventArtist.Role.HEADLINER,
            billing_order=current.count() + 1,
        )
        event.order_bill()

    setlist, _ = Setlist.objects.update_or_create(
        event=event,
        artist=artist,
        defaults={
            "setlistfm_id": setlistfm_id,
            "source_url": (payload.get("url") or "").strip(),
            "notes": (payload.get("info") or "").strip(),
        },
    )

    # Re-importing refreshes the song list.
    setlist.entries.all().delete()
    position = 0
    encore_count = 0
    for set_payload in _as_list((payload.get("sets") or {}).get("set")):
        if not isinstance(set_payload, dict):
            continue
        if set_payload.get("encore"):
            encore_count += 1
        set_name = _set_name(set_payload, encore_count)
        for song_payload in _as_list(set_payload.get("song")):
            if not isinstance(song_payload, dict):
                continue
            title = (song_payload.get("name") or "").strip()
            if not title:
                continue
            song, _ = Song.objects.get_or_create(artist=artist, title=title)
            cover = song_payload.get("cover") or {}
            if isinstance(cover, dict) and cover.get("name") and not song.cover_of_name:
                song.cover_of_name = str(cover["name"]).strip()[:200]
                song.save(update_fields=["cover_of_name", "updated_at"])
            position += 1
            SetlistSong.objects.create(
                setlist=setlist,
                song=song,
                position=position,
                set_name=set_name,
                note=_song_note(song_payload),
                is_tape=bool(song_payload.get("tape")),
            )

    return setlist
