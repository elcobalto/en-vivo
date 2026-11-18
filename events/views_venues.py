"""Venue search backed by our own venues + Google Places (the API key never reaches the browser)."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from . import google_places, image_search, setlistfm_events
from .models import Event, Log, Performer, Setlist, Venue, add_all_acts
from .roles import can_edit_event, home_place, is_admin
from .setlistfm import SetlistFmError, _as_list, parse_event_date, search_setlists

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
            return JsonResponse({**data, "error": "Too many searches, wait a moment."}, status=429)
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
        return JsonResponse({"error": "Missing place."}, status=400)
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
    messages.success(request, f"“{venue.name}” now uses Google's data.")
    return redirect(venue)


# --------------------------------------------------------------------------- shows from setlist.fm (any logged-in user)

MAX_IMPORTS_PER_REQUEST = 10  # each import ~1-2 s; keeps requests under the 30 s worker timeout


@login_required
def setlistfm_shows(request: HttpRequest) -> HttpResponse:
    """Search setlist.fm and create music events (venue, tour, lineup, songs) from the results."""
    if request.method == "POST":
        ids = [i.strip() for i in request.POST.getlist("setlist_id") if i.strip()][:MAX_IMPORTS_PER_REQUEST]
        if not ids:
            messages.error(request, "Select at least one setlist.")
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
            summary = [f"{created} event{'s' if created != 1 else ''} created"]
            if joined:
                summary.append(f"{joined} added to existing events")
            if skipped:
                summary.append(f"{skipped} already imported")
            if attended:
                summary.append(f"{attended} added to your diary")
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
        error = "Fill in at least one filter: artist, city, venue, year or country."
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
         "max_imports": MAX_IMPORTS_PER_REQUEST},
    )


# --------------------------------------------------------------------------- "Find photo" for events

PHOTO_SEARCHES_PER_HOUR = 30  # Google's free tier is ~100 searches/day for the whole site


def _photo_picker(request, *, obj, default_query: str, artist: str, context: dict) -> HttpResponse:
    if request.method == "POST":
        url = (request.POST.get("url") or "").strip()
        if not url.startswith("https://") or len(url) > 1000:
            messages.error(request, "That photo can't be used (it needs a secure https link).")
            return redirect(request.path)
        obj.image_url = url
        obj.save(update_fields=["image_url", "updated_at"])
        messages.success(request, "Photo updated.")
        return redirect(obj)

    query = (request.GET.get("q") or default_query).strip()[:200]
    candidates, errors = [], []
    key = f"photo-search:{request.user.pk}"
    used = cache.get_or_set(key, 0, timeout=60 * 60)
    if used >= PHOTO_SEARCHES_PER_HOUR:
        errors.append("You've searched a lot in the last hour; try again later.")
    elif query:
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=60 * 60)
        candidates, errors = image_search.find_candidates(query, artist)

    return render(
        request,
        "events/event_photo.html",
        {"query": query, "candidates": candidates, "errors": errors,
         "google_enabled": image_search.google_enabled(),
         "google_url": image_search.google_images_url(query), **context},
    )


@login_required
def event_photo(request: HttpRequest, pk: int) -> HttpResponse:
    """Show image matches for an event and save the one the person picks."""
    event = get_object_or_404(Event.objects.select_related("venue"), pk=pk)
    if not can_edit_event(request.user, event):
        raise PermissionDenied
    headliner = event.headliner if event.is_music else None
    return _photo_picker(
        request, obj=event, default_query=image_search.default_query(event),
        artist=headliner.name if headliner else "",
        context={"subject_title": event.title, "back_url": event.get_absolute_url(),
                 "edit_url": reverse("events:event_edit", args=[event.pk]),
                 "hint": "Pick the image that fits best: a poster, the artist, or the show itself."},
    )


@login_required
def performer_photo(request: HttpRequest, pk: int) -> HttpResponse:
    """Anyone logged in can add a photo to an artist without one; changing it is for admins."""
    performer = get_object_or_404(Performer, pk=pk)
    if performer.image_url and not is_admin(request.user):
        raise PermissionDenied
    return _photo_picker(
        request, obj=performer, default_query=performer.name, artist=performer.name,
        context={"subject_title": performer.name, "back_url": performer.get_absolute_url(),
                 "edit_url": reverse("events:performer_edit", args=[performer.pk]) if is_admin(request.user) else "",
                 "hint": "Pick a photo of the artist. It's also used for their events that have no photo."},
    )
