"""Venue search backed by our own venues + Google Places (the API key never reaches the browser)."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from . import google_places, setlistfm_events
from .models import Setlist, Venue
from .roles import is_admin
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


# --------------------------------------------------------------------------- admins: shows from setlist.fm

MAX_IMPORTS_PER_REQUEST = 10  # each import ~1-2 s; keeps requests under the 30 s worker timeout


@login_required
def setlistfm_shows(request: HttpRequest) -> HttpResponse:
    """Search setlist.fm and create music events (venue, tour, lineup, songs) from the results."""
    if not is_admin(request.user):
        raise PermissionDenied

    if request.method == "POST":
        ids = [i.strip() for i in request.POST.getlist("setlist_id") if i.strip()][:MAX_IMPORTS_PER_REQUEST]
        if not ids:
            messages.error(request, "Select at least one setlist.")
        else:
            created = joined = skipped = 0
            for setlist_id, result, error in setlistfm_events.import_many(ids, request.user):
                if error:
                    messages.error(request, f"{setlist_id}: {error}")
                elif result.setlist_already_imported:
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
            messages.success(request, ", ".join(summary) + ".")
        return redirect(request.get_full_path())

    filters = {k: (request.GET.get(k) or "").strip() for k in ["artist", "city", "year", "venue", "country"]}
    try:
        page = max(1, min(int(request.GET.get("page") or 1), 50))
    except ValueError:
        page = 1
    results, error = [], None
    if filters["artist"]:
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
        {"f": filters, "page": page, "results": results, "error": error,
         "max_imports": MAX_IMPORTS_PER_REQUEST},
    )
