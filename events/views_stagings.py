"""Stagings (montajes): one production of a play, shown on many dates."""
from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.core.paginator import Paginator
from django.db.models import Avg, Count, F, Prefetch
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from . import image_search, runs
from .forms import StagingForm
from .models import CREDIT_ROLES, Event, EventArtist, Log, Staging
from .roles import can_edit_staging

REVIEWS_PER_PAGE = 20


def staging_detail(request: HttpRequest, pk: int) -> HttpResponse:
    from .views import _reviews

    staging = get_object_or_404(Staging.objects.select_related("play"), pk=pk)
    dates = (staging.dates.with_stats().select_related("venue")
             .prefetch_related(Prefetch("eventartist_set", queryset=EventArtist.objects.select_related("artist")
                                        .order_by("billing_order", "pk")))
             .order_by(F("date").asc(nulls_last=True), "pk"))
    logs = Log.objects.filter(event__staging=staging)
    stats = logs.aggregate(avg_rating=Avg("rating"), log_count=Count("id"))
    # Who is on most dates (the cast and crew of the staging; a date can still change someone).
    people = {}
    n_dates = 0
    for event in dates:
        n_dates += 1
        for link in event.eventartist_set.all():
            row = people.setdefault(link.artist_id, {"artist": link.artist, "link": link, "dates": 0,
                                                     "credit": link.role in CREDIT_ROLES, "order": link.billing_order})
            row["dates"] += 1
    regulars = sorted(people.values(), key=lambda r: (r["credit"], r["order"]))
    return render(request, "events/staging_detail.html", {
        "staging": staging,
        "dates": dates,
        "n_dates": n_dates,
        "credits": [r for r in regulars if r["credit"]],
        "cast": [r for r in regulars if not r["credit"]],
        "reviews": Paginator(_reviews(logs), REVIEWS_PER_PAGE).get_page(request.GET.get("rpage")),
        "staging_logs": logs,
        "can_edit": can_edit_staging(request.user, staging),
        **stats,
    })


@login_required
def staging_edit(request: HttpRequest, pk: int) -> HttpResponse:
    staging = get_object_or_404(Staging, pk=pk)
    if not can_edit_staging(request.user, staging):
        raise PermissionDenied
    form = StagingForm(request.POST or None, instance=staging)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            staging = form.save()
            if "play" in form.changed_data:  # its dates stage the same play
                for event in staging.dates.all():
                    event.play = staging.play
                    event.save()
        messages.success(request, gettext("Staging saved."))
        return redirect(staging)
    return render(request, "events/simple_form.html", {
        "form": form, "title": gettext("Edit staging"), "next": staging.get_absolute_url()})


@login_required
def staging_photo(request: HttpRequest, pk: int) -> HttpResponse:
    """The photo every date of the staging shows unless it has its own."""
    from .views_venues import _photo_picker

    staging = get_object_or_404(Staging.objects.select_related("play"), pk=pk)
    if not can_edit_staging(request.user, staging):
        raise PermissionDenied
    first = staging.dates.order_by("date", "pk").first()
    query = image_search.default_query(first) if first else staging.show_title
    return _photo_picker(
        request, obj=staging, default_query=query, artist="",
        context={"subject_title": staging.name, "back_url": staging.get_absolute_url(),
                 "edit_url": reverse("events:staging_edit", args=[staging.pk]),
                 "hint": gettext("Pick the staging's poster. Every date without a photo of its own shows it.")},
    )


@login_required
@require_POST
def staging_add_date(request: HttpRequest, pk: int) -> HttpResponse:
    """Another date of the staging: a copy of its latest date (venue, cast…) on the day picked."""
    staging = get_object_or_404(Staging, pk=pk)
    if not can_edit_staging(request.user, staging):
        raise PermissionDenied
    try:
        on = date.fromisoformat((request.POST.get("date") or "").strip())
    except ValueError:
        messages.error(request, gettext("Pick a date."))
        return redirect(staging)
    event, created = runs.add_staging_date(staging, on, request.user)
    if event is None:
        return redirect(reverse("events:event_create") + f"?staging={staging.pk}")
    if created:
        messages.success(request, gettext("Added %(date)s. Edit it if anything is different that day.") % {
            "date": date_format(on)})
    else:
        messages.info(request, gettext("The show already has that date."))
    return redirect(event)
