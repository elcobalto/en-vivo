"""Venue search backed by our own venues + Google Places (the API key never reaches the browser)."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext, ngettext
from django.views.decorators.http import require_GET, require_POST

from . import google_places, image_search, setlistfm_events, setlistfm_quota
from .models import Event, Log, Artist, Setlist, Venue, add_all_acts
from .roles import can_edit_event, home_place, is_admin
from .setlistfm import SetlistFmError, _as_list, parse_event_date, search_setlists
from .views_social import get_profile

SEARCHES_PER_MINUTE = 60


def _rate_limited(request: HttpRequest) -> bool:
    key = f"venue-search:{request.user.pk}"
    count = cache.get_or_set(key, 0, timeout=60)
    if count >= SEARCHES_PER_MINUTE:
        return True
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=60)
    return False


def _venue_json(venue: Venue) -> dict:
    return {
        "id": venue.pk,
        "name": venue.name,
        "secondary": venue.location_label,
        "url": venue.get_absolute_url(),
    }


@login_required
@require_GET
def venue_search(request: HttpRequest) -> JsonResponse:
    q = (request.GET.get("q") or "").strip()
    session = (request.GET.get("session") or "")[:100]
    if len(q) < 2:
        return JsonResponse({"local": [], "google": [], "google_enabled": google_places.enabled()})

    local = list(
        Venue.objects.filter(Q(name__icontains=q) | Q(city__icontains=q)).order_by("name")[:5]
    )
    data = {"local": [_venue_json(v) for v in local], "google": [], "google_enabled": google_places.enabled()}

    if google_places.enabled() and len(q) >= 3:
        if _rate_limited(request):
            return JsonResponse({**data, "error": gettext("Too many searches, wait a moment.")}, status=429)
        try:
            known = set(Venue.objects.exclude(google_place_id=None).values_list("google_place_id", flat=True))
            data["google"] = [s for s in google_places.autocomplete(q, session) if s["place_id"] not in known]
        except google_places.PlacesError as e:
            data["error"] = str(e)
    return JsonResponse(data)


@login_required
@require_POST
def venue_from_place(request: HttpRequest) -> JsonResponse:
    place_id = (request.POST.get("place_id") or "").strip()
    if not place_id:
        return JsonResponse({"error": gettext("Missing place.")}, status=400)
    try:
        venue = google_places.venue_from_place(place_id, (request.POST.get("session") or "")[:100])
    except google_places.PlacesError as e:
        return JsonResponse({"error": str(e)}, status=502)
    return JsonResponse(_venue_json(venue))


@login_required
@require_POST
def venue_link_place(request: HttpRequest, pk: int) -> HttpResponse:
    """Admins: replace a venue's hand-typed data with Google's standardized data."""
    if not is_admin(request.user):
        raise PermissionDenied
    venue = get_object_or_404(Venue, pk=pk)
    place_id = (request.POST.get("place_id") or "").strip()
    try:
        google_places.apply_place(venue, place_id, session=(request.POST.get("session") or "")[:100])
    except google_places.PlacesError as e:
        messages.error(request, str(e))
        return redirect("events:venue_edit", pk=venue.pk)
    messages.success(request, gettext("“%(name)s” now uses Google's data.") % {"name": venue.name})
    return redirect(venue)


# --------------------------------------------------------------------------- shows from setlist.fm (any logged-in user)

MAX_IMPORTS_PER_REQUEST = 10  # each import ~1-2 s; keeps requests under the 30 s worker timeout


def max_setlist_imports(user) -> int:
    """How many setlists one import may fetch: admins up to MAX_IMPORTS_PER_REQUEST, everyone else one at a
    time (beta: each one is a setlist.fm API call)."""
    return MAX_IMPORTS_PER_REQUEST if is_admin(user) else 1


@login_required
def setlistfm_shows(request: HttpRequest) -> HttpResponse:
    """Search setlist.fm and create music events (venue, tour, lineup, songs) from the results."""
    if request.method == "POST":
        ids = [i.strip() for i in request.POST.getlist("setlist_id") if i.strip()][:max_setlist_imports(request.user)]
        if not ids:
            messages.error(request, gettext("Select at least one setlist."))
        else:
            created = joined = skipped = attended = 0
            mark_attended = request.POST.get("attended") == "1"
            for setlist_id, result, error in setlistfm_events.import_many(ids, request.user):
                if error:
                    messages.error(request, f"{setlist_id}: {error}")
                    continue
                if mark_attended and result.event.has_happened:
                    log, added = Log.objects.get_or_create(user=request.user, event=result.event)
                    attended += added
                    if not added:
                        add_all_acts(log)  # an act this import added to an event you'd logged
                if result.setlist_already_imported:
                    skipped += 1
                elif result.created:
                    created += 1
                else:
                    joined += 1
            summary = [ngettext("%(n)s event created", "%(n)s events created", created) % {"n": created}]
            if joined:
                summary.append(gettext("%(n)s added to existing events") % {"n": joined})
            if skipped:
                summary.append(gettext("%(n)s already imported") % {"n": skipped})
            if attended:
                summary.append(gettext("%(n)s added to your diary") % {"n": attended})
            messages.success(request, ", ".join(summary) + ".")
        return redirect(request.get_full_path())

    filters = {k: (request.GET.get(k) or "").strip() for k in ["artist", "city", "year", "venue", "country"]}
    searched = any(k in request.GET for k in filters)
    if not searched:  # first visit: start from the user's home city/country
        filters["city"], filters["country"] = home_place(request.user)
    try:
        page = max(1, min(int(request.GET.get("page") or 1), 50))
    except ValueError:
        page = 1
    results, error = [], None
    if searched and not any(filters.values()):
        error = gettext("Fill in at least one filter: artist, city, venue, year or country.")
    elif searched:
        try:
            results = search_setlists(
                artist_name=filters["artist"],
                city=filters["city"],
                year=filters["year"],
                venue=filters["venue"],
                country_code=filters["country"],
                page=page,
            )
        except SetlistFmError as e:
            error = str(e)

    imported = {
        s.setlistfm_id: s.event
        for s in Setlist.objects.filter(setlistfm_id__in=[r.get("id") for r in results]).select_related("event")
    }
    for r in results:
        r["parsed_date"] = parse_event_date(r.get("eventDate"))
        r["song_count"] = sum(len(_as_list(s.get("song"))) for s in _as_list((r.get("sets") or {}).get("set")))
        r["imported_event"] = imported.get(r.get("id"))

    return render(
        request,
        "events/setlistfm_shows.html",
        {"f": filters, "page": page, "results": results, "error": error, "searched": searched,
         "max_imports": max_setlist_imports(request.user),
         "api_remaining": setlistfm_quota.remaining() if is_admin(request.user) else None,
         # The profile setting picks whether "I attended these shows" starts ticked.
         "attended_default": get_profile(request.user).import_as_attended},
    )


# --------------------------------------------------------------------------- "Find photo" for events

PHOTO_SEARCHES_PER_HOUR = 30  # each search calls Deezer and Wikimedia Commons from our server


def _photo_picker(request, *, obj, default_query: str, artist: str, context: dict) -> HttpResponse:
    if request.method == "POST":
        url = image_search.clean_image_link(request.POST.get("url") or "")
        if image_search.is_search_page(url):
            messages.error(request, gettext("That's a link to the search results, not to the photo. Drag the photo "
                                            "itself here, or right-click it → “Copy image address”."))
            return redirect(request.get_full_path())
        if not url.startswith("https://") or len(url) > 1000:
            messages.error(request, gettext("That photo can't be used (it needs a secure https link)."))
            return redirect(request.get_full_path())
        obj.image_url = url
        obj.save(update_fields=["image_url", "updated_at"])
        messages.success(request, gettext("Photo updated."))
        if isinstance(obj, Event) and request.POST.get("all_days"):
            from .merge import share_photo, shared_photo_message

            n, unit = share_photo(request.user, obj)
            if n:
                messages.info(request, shared_photo_message(n, unit))
        return redirect(obj)

    query = (request.GET.get("q") or default_query).strip()[:200]
    candidates, errors = [], []
    key = f"photo-search:{request.user.pk}"
    used = cache.get_or_set(key, 0, timeout=60 * 60)
    if used >= PHOTO_SEARCHES_PER_HOUR:
        errors.append(gettext("You've searched a lot in the last hour; try again later."))
    elif query:
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=60 * 60)
        candidates, errors = image_search.find_candidates(query, artist)

    return render(
        request,
        "events/event_photo.html",
        {"query": query, "artist": artist, "candidates": candidates, "errors": errors,
         "google_url": image_search.google_images_url(query), **context},
    )


@login_required
def event_photo(request: HttpRequest, pk: int) -> HttpResponse:
    """Show image matches for an event and save the one the person picks."""
    from .merge import photo_siblings

    event = get_object_or_404(Event.objects.select_related("venue"), pk=pk)
    if not can_edit_event(request.user, event):
        raise PermissionDenied
    headliner = event.headliner if event.is_music else None
    return _photo_picker(
        request, obj=event, default_query=image_search.default_query(event),
        artist=headliner.name if headliner else "",
        context={"subject_title": event.title, "back_url": event.get_absolute_url(),
                 "photo_share": photo_siblings(event),
                 "edit_url": reverse("events:event_edit", args=[event.pk]),
                 "hint": gettext("Pick the image that fits best: a poster, the artist, or the show itself.")},
    )


@login_required
def production_photo(request: HttpRequest, pk: int) -> HttpResponse:
    """Festivals and tours: anyone logged in adds a missing photo; changing it is for admins."""
    from .models import Production

    production = get_object_or_404(Production.objects.select_related("artist"), pk=pk)
    if production.image_url and not is_admin(request.user):
        raise PermissionDenied
    artist = production.artist.name if production.artist_id else ""
    query = " ".join(b for b in [production.title, artist, "poster"] if b)
    return _photo_picker(
        request, obj=production, default_query=query, artist="",
        context={"subject_title": production.title, "back_url": production.get_absolute_url(),
                 "edit_url": reverse("events:production_edit", args=[production.pk]) if is_admin(request.user) else "",
                 "hint": gettext("Pick the festival or tour poster. It's also used for its events that have no photo.")},
    )


@login_required
def artist_photo(request: HttpRequest, pk: int) -> HttpResponse:
    """Anyone logged in can add a photo to an artist without one; changing it is for admins."""
    artist = get_object_or_404(Artist, pk=pk)
    if artist.image_url and not is_admin(request.user):
        raise PermissionDenied
    return _photo_picker(
        request, obj=artist, default_query=artist.name, artist=artist.name,
        context={"subject_title": artist.name, "back_url": artist.get_absolute_url(),
                 "edit_url": reverse("events:artist_edit", args=[artist.pk]) if is_admin(request.user) else "",
                 "hint": gettext("Pick a photo of the artist. It's also used for their events that have no photo.")},
    )


# --------------------------------------------------------------------------- event videos


@login_required
@require_POST
def video_add(request: HttpRequest, pk: int) -> HttpResponse:
    from .models import EventVideo
    from .videos import video_title, youtube_id

    event = get_object_or_404(Event, pk=pk)
    vid = youtube_id(request.POST.get("url") or "")
    if not vid:
        messages.error(request, gettext("That doesn't look like a YouTube link."))
        return redirect("events:event_videos", pk=event.pk)
    artist_id = (request.POST.get("artist") or "").strip()
    artist = None
    if artist_id.isdigit():
        artist = event.artists.filter(pk=int(artist_id)).first()
    if EventVideo.objects.filter(event=event, youtube_id=vid).exists():
        messages.info(request, gettext("That video is already on this event."))
    else:
        EventVideo.objects.create(event=event, artist=artist, youtube_id=vid,
                                  title=video_title(vid), added_by=request.user)
        messages.success(request, gettext("Video added."))
    return redirect("events:event_videos", pk=event.pk)


@login_required
@require_POST
def video_remove(request: HttpRequest, pk: int, video_pk: int) -> HttpResponse:
    from .models import EventVideo

    video = get_object_or_404(EventVideo, pk=video_pk, event_id=pk)
    if video.added_by_id != request.user.pk and not is_admin(request.user):
        raise PermissionDenied
    video.delete()
    messages.success(request, gettext("Video removed."))
    return redirect("events:event_videos", pk=pk)


POSTER_MAX_BYTES = 8 * 1024 * 1024


def _public_https_url(url: str) -> bool:
    """Only https links to public hosts (no localhost or private networks)."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


def _image_type(data: bytes) -> str:
    """The image type from the file's first bytes, or "" if it isn't a common image."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


@login_required
def event_poster_image(request: HttpRequest, pk: int) -> HttpResponse:
    """The event's photo served from En Vivo, so the edit page can read it (browsers block reading
    images from most other sites). Editors only; https images from public hosts, up to 8 MB."""
    from urllib.error import HTTPError, URLError
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    event = get_object_or_404(Event, pk=pk)
    if not can_edit_event(request.user, event):
        raise PermissionDenied
    url = event.display_image
    if not url:
        raise Http404
    if not _public_https_url(url):
        return HttpResponse(gettext("Only https photos from public sites can be read."), status=400, content_type="text/plain")

    class SafeRedirects(HTTPRedirectHandler):
        max_redirections = 3

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not _public_https_url(newurl):
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    def download(user_agent):
        with build_opener(SafeRedirects).open(
            Request(url, headers={"User-Agent": user_agent, "Accept": "image/*,*/*;q=0.8"}), timeout=8
        ) as resp:
            return (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower(), resp.read(POSTER_MAX_BYTES + 1)

    try:
        try:
            content_type, data = download("EnVivo/1.0 (poster reader)")
        except HTTPError as e:
            if e.code not in (401, 403, 406, 429):
                raise
            # Some hosts only answer browsers.
            content_type, data = download("Mozilla/5.0 (compatible; EnVivo poster reader)")
    except HTTPError as e:
        return HttpResponse(gettext("The photo's site answered %(code)s.") % {"code": e.code}, status=502,
                            content_type="text/plain")
    except (URLError, OSError, ValueError) as e:
        return HttpResponse(gettext("Couldn't download the photo (%(error)s).") % {"error": e.__class__.__name__},
                            status=502, content_type="text/plain")
    if len(data) > POSTER_MAX_BYTES:
        return HttpResponse(gettext("The photo is larger than 8 MB."), status=413, content_type="text/plain")
    if not content_type.startswith("image/"):
        sniffed = _image_type(data)  # e.g. served as application/octet-stream
        if not sniffed:
            return HttpResponse(gettext("That link isn't an image."), status=415, content_type="text/plain")
        content_type = sniffed
    response = HttpResponse(data, content_type=content_type)
    response["Cache-Control"] = "private, max-age=300"
    return response

