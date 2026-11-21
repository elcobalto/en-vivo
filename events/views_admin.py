"""The admins' page: who uses En Vivo, open feedback, setlist.fm API usage, and importing an artist's setlists."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from . import artist_import as importer, setlistfm_quota, site_stats
from .models import ArtistImport, Feedback
from .setlistfm import SetlistFmError, search_artists
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
    })


@login_required
def setlistfm_usage(request: HttpRequest) -> HttpResponse:
    """How much of setlist.fm's daily request limit En Vivo has used, and by whom."""
    _require_admin(request)
    return render(request, "events/setlistfm_usage.html", {"u": setlistfm_quota.usage()})


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
        job = ArtistImport.objects.filter(artist_mbid=mbid).exclude(status=ArtistImport.Status.DONE).first()
        if job:  # one unfinished import per artist: continue it
            messages.info(request, gettext("There's already an unfinished import for %(name)s.") % {"name": job.artist_name})
        else:
            job = ArtistImport.objects.create(
                artist_name=name, artist_mbid=mbid, started_by=request.user,
                only_with_songs=request.POST.get("only_with_songs") == "1",
                look_up_venues=request.POST.get("look_up_venues") == "1",
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
