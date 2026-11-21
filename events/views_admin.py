"""The admins' page: who uses En Vivo, open feedback, setlist.fm API usage, and importing an artist's setlists."""
from __future__ import annotations

from django.contrib import messages
from django_countries import countries
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from . import artist_import as importer, festivals, setlistfm_quota, site_stats
from .models import ArtistImport, Feedback
from .setlistfm import SetlistFmError, search_artists
from .roles import home_place
from .views_feedback import _require_admin


@login_required
def admin_tools(request: HttpRequest) -> HttpResponse:
    """Who uses the site, open feedback, setlist.fm usage and artist imports, linking to the full pages."""
    _require_admin(request)
    open_items = Feedback.objects.filter(resolved=False).select_related("user")
    return render(request, "events/admin_tools.html", {
        "open_count": open_items.count(),
        "latest": open_items[:3],
        "resolved_count": Feedback.objects.filter(resolved=True).count(),
        "u": setlistfm_quota.usage(),
        "s": site_stats.site_stats(),
        "imports": ArtistImport.objects.select_related("started_by")[:5],
        "possible_festivals": festivals.candidates().count(),
    })


@login_required
def setlistfm_usage(request: HttpRequest) -> HttpResponse:
    """How much of setlist.fm's daily request limit En Vivo has used, and by whom."""
    _require_admin(request)
    return render(request, "events/setlistfm_usage.html", {"u": setlistfm_quota.usage()})


def _since_year(raw) -> int | None:
    """A "since year" typed in the import form, or None (every year)."""
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() and 1900 <= int(raw) <= 2100 else None


@login_required
def artist_import_new(request: HttpRequest) -> HttpResponse:
    """Find the artist on setlist.fm (GET ?artist=), then start importing all their setlists (POST)."""
    _require_admin(request)
    if request.method == "POST":
        mbid = (request.POST.get("mbid") or "").strip()[:40]
        name = (request.POST.get("name") or "").strip()[:200]
        if not mbid or not name:
            messages.error(request, gettext("Pick an artist first."))
            return redirect("events:artist_import_new")
        country = "" if request.POST.get("all_countries") == "1" else (request.POST.get("country") or "").strip().upper()[:2]
        job = ArtistImport.objects.filter(artist_mbid=mbid, country=country, since_year=_since_year(request.POST.get("since_year"))) \
            .exclude(status=ArtistImport.Status.DONE).first()
        if job:  # one unfinished import per artist and country: continue it
            messages.info(request, gettext("There's already an unfinished import for %(name)s.") % {"name": job.artist_name})
        else:
            job = ArtistImport.objects.create(
                artist_name=name, artist_mbid=mbid, started_by=request.user, country=country,
                only_with_songs=request.POST.get("only_with_songs") == "1",
                look_up_venues=True,  # new venues are always matched on Google Places
                since_year=_since_year(request.POST.get("since_year")),
            )
        return redirect("events:artist_import", pk=job.pk)
    query = (request.GET.get("artist") or "").strip()[:200]
    candidates, error = [], ""
    if query:
        try:
            candidates = search_artists(query)[:10]
        except SetlistFmError as e:
            error = str(e)
    return render(request, "events/artist_import_new.html", {
        "query": query, "candidates": candidates, "error": error,
        "imports": ArtistImport.objects.select_related("started_by")[:20],
        "remaining": setlistfm_quota.remaining(),
        # Shows in the admin's home country by default (setlist.fm's own country filter); they can untick it.
        "country": (request.GET.get("country") or home_place(request.user)[1] or "").upper()[:2],
        "countries": list(countries),
    })


@login_required
def artist_import(request: HttpRequest, pk: int) -> HttpResponse:
    """The progress page: its script asks for steps until the import is done or paused."""
    _require_admin(request)
    job = get_object_or_404(ArtistImport, pk=pk)
    return render(request, "events/artist_import.html", {"job": job, "state": importer.as_json(job)})


@login_required
@require_POST
def artist_import_step(request: HttpRequest, pk: int) -> JsonResponse:
    """Do about ten seconds of the import and report where it stands."""
    _require_admin(request)
    job = get_object_or_404(ArtistImport, pk=pk)
    action = request.POST.get("action")
    if action == "pause" and job.status == ArtistImport.Status.RUNNING:
        ArtistImport.objects.filter(pk=job.pk).update(status=ArtistImport.Status.PAUSED, last_error="")
        job.refresh_from_db()
        return JsonResponse(importer.as_json(job))
    if action == "resume" and job.status == ArtistImport.Status.PAUSED:
        job.status, job.last_error = ArtistImport.Status.RUNNING, ""
        job.save(update_fields=["status", "last_error", "updated_at"])
    if job.status != ArtistImport.Status.RUNNING:
        return JsonResponse(importer.as_json(job))
    worked = importer.run_step(job)
    return JsonResponse(importer.as_json(job, busy=not worked))


@login_required
def possible_festivals(request: HttpRequest) -> HttpResponse:
    """Music events with four or more acts: turn them into festivals, or mark them as not festivals."""
    _require_admin(request)
    from .models import Event

    if request.method == "POST":
        pk = request.POST.get("event") or ""
        event = get_object_or_404(Event, pk=int(pk) if pk.isdigit() else 0, event_type="music")
        action = request.POST.get("action")
        if action == "festival":
            name = " ".join((request.POST.get("name") or "").split())[:240]
            if not name:
                messages.error(request, gettext("Give the festival a name."))
            else:
                festivals.make_festival(event, name)
                messages.success(request, gettext("“%(event)s” is now a festival day of %(name)s.") % {
                    "event": event.title, "name": event.production.title})
        elif action in ("dismiss", "restore"):
            event.not_festival = action == "dismiss"
            event.save(update_fields=["not_festival", "updated_at"])
        return redirect(request.get_full_path())

    tab = "dismissed" if request.GET.get("tab") == "dismissed" else "possible"
    possible, dismissed = festivals.candidates(), festivals.candidates(dismissed=True)
    events = list((dismissed if tab == "dismissed" else possible)[:100])
    for event in events:
        event.suggested_name = festivals.suggested_name(event)
        event.some_acts = [l.artist.name for l in event.acts_on_stage().select_related("artist")[:6]]
    return render(request, "events/possible_festivals.html", {
        "tab": tab, "events": events, "possible_count": possible.count(), "dismissed_count": dismissed.count(),
        "festival_names": festivals.festival_names(), "min_acts": festivals.MIN_ACTS,
    })


@login_required
def artist_import_preview(request: HttpRequest) -> JsonResponse:
    """How many setlists an import would bring (one setlist.fm request; the import's first page then comes from the
    cache): {"total", "requests"}."""
    _require_admin(request)
    from .setlistfm import search_setlists_page

    mbid = (request.GET.get("mbid") or "").strip()[:40]
    country = (request.GET.get("country") or "").strip().upper()[:2]
    if not mbid:
        return JsonResponse({"error": gettext("Pick an artist first.")}, status=400)
    try:
        results, total, per_page = search_setlists_page(artist_mbid=mbid, country_code=country, page=1)
    except SetlistFmError as e:
        return JsonResponse({"error": str(e)}, status=503)
    return JsonResponse({"total": total, "requests": -(-total // per_page) if total else 0,
                         "remaining": setlistfm_quota.remaining()})
