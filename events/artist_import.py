"""
Import every setlist.fm setlist of one artist (admins), in short steps the browser asks for one after another.

Why steps: the web server has no background worker and cuts requests at 30 s, and The Beatles have
over a thousand setlists. Each step reads search pages (20 whole setlists per request, so an artist
costs about one request per 20 setlists) and imports setlists until ``STEP_SECONDS`` have passed.

Nothing is lost when something fails: every setlist is saved in its own transaction and the job's
cursor (page, index) moves after each one. Running low on the day's setlist.fm budget, a setlist.fm
error or a closed tab pauses the job; resuming continues from the next setlist. The request pace
(2 per second at most) is kept by ``setlistfm_quota`` like any other call.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext

from . import setlistfm_quota
from .models import ArtistImport
from .setlistfm import SetlistFmError, _as_list, search_setlists_page
from .setlistfm_events import create_event_from_setlist

logger = logging.getLogger(__name__)

STEP_SECONDS = 10  # well under the server's 30 s request limit
LOCK_SECONDS = 60  # a step that died (worker killed) stops blocking the job after this
# What a step saves as it goes; the status only changes on purpose (a pause from the page wins).
PROGRESS_FIELDS = ["total", "page", "index", "processed", "created", "joined", "already", "empty", "failed",
                   "last_error", "updated_at"]


def has_songs(payload: dict) -> bool:
    return any(_as_list(s.get("song")) for s in _as_list((payload.get("sets") or {}).get("set")) if isinstance(s, dict))


def run_step(job: ArtistImport, seconds: float | None = None) -> bool:
    """Work on a running job for about ``seconds``. False if another step holds it (two tabs open)."""
    now = timezone.now()
    took = ArtistImport.objects.filter(pk=job.pk, status=ArtistImport.Status.RUNNING).filter(
        Q(locked_until__isnull=True) | Q(locked_until__lt=now)
    ).update(locked_until=now + timedelta(seconds=LOCK_SECONDS))
    job.refresh_from_db()
    if not took:
        return False
    try:
        with setlistfm_quota.bulk():
            _work(job, time.monotonic() + (STEP_SECONDS if seconds is None else seconds))
    finally:
        ArtistImport.objects.filter(pk=job.pk).update(locked_until=None)
        job.locked_until = None
    return True


def _paused_meanwhile(job: ArtistImport) -> bool:
    """The admin pressed Pause while this step ran."""
    status = ArtistImport.objects.filter(pk=job.pk).values_list("status", flat=True).first()
    if status != ArtistImport.Status.RUNNING:
        job.status = status
        return True
    return False


def _pause(job: ArtistImport, reason: str) -> None:
    job.status = ArtistImport.Status.PAUSED
    job.last_error = reason
    job.save(update_fields=PROGRESS_FIELDS + ["status"])


def _work(job: ArtistImport, deadline: float) -> None:
    while job.status == ArtistImport.Status.RUNNING and time.monotonic() < deadline:
        try:
            results, total, per_page = search_setlists_page(artist_mbid=job.artist_mbid, page=job.page)
        except SetlistFmError as e:  # out of budget, rate limited, setlist.fm down: resume later
            _pause(job, str(e))
            return
        job.total = max(total, job.processed)
        if _paused_meanwhile(job):
            return
        while job.index < len(results) and time.monotonic() < deadline:
            if _paused_meanwhile(job):
                return
            _import_one(job, results[job.index])
            job.index += 1
            job.processed += 1
            job.save(update_fields=PROGRESS_FIELDS)
        if job.index < len(results):
            return  # out of time mid-page; the next step reads the same page (from the cache)
        if not results or job.page * per_page >= total:
            job.status = ArtistImport.Status.DONE
            job.finished_at = timezone.now()
            job.save(update_fields=PROGRESS_FIELDS + ["status", "finished_at"])
            return
        job.page += 1
        job.index = 0
        job.save(update_fields=PROGRESS_FIELDS)


def _import_one(job: ArtistImport, payload: dict) -> None:
    if job.only_with_songs and not has_songs(payload):
        job.empty += 1
        return
    try:
        result = create_event_from_setlist(payload, job.started_by, look_up_venue=job.look_up_venues)
    except SetlistFmError as e:
        job.failed += 1
        job.last_error = f"{payload.get('id')}: {e}"
        return
    except Exception:  # one bad setlist never stops the import
        logger.exception("Artist import %s: setlist %s failed", job.pk, payload.get("id"))
        job.failed += 1
        job.last_error = gettext("Setlist %(id)s couldn't be imported.") % {"id": payload.get("id")}
        return
    if result.setlist_already_imported:
        job.already += 1
    elif result.created:
        job.created += 1
    else:
        job.joined += 1


def as_json(job: ArtistImport, busy: bool = False) -> dict:
    return {
        "status": job.status,
        "status_label": job.get_status_display(),
        "busy": busy,
        "percent": job.percent,
        "processed": job.processed,
        "total": job.total,
        "page": job.page,
        "created": job.created,
        "joined": job.joined,
        "already": job.already,
        "empty": job.empty,
        "failed": job.failed,
        "last_error": job.last_error,
        "remaining_requests": setlistfm_quota.remaining(),
    }
