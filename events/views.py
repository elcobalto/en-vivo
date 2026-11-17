from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, F, Prefetch, Q
from django.db.models.functions import ExtractYear
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import EventForm, EventPerformerFormSet, LogForm, PerformerForm, ProductionForm, VenueForm
from .models import (
    Event,
    EventList,
    EventPerformer,
    EventType,
    Log,
    Performer,
    Production,
    SetlistSong,
    Venue,
    WantToGo,
)

SORTS = {
    "newest": ("Newest first", [F("date").desc(nulls_last=True), "title"]),
    "oldest": ("Oldest first", [F("date").asc(nulls_last=True), "title"]),
    "popular": ("Most logged", ["-log_count", F("date").desc(nulls_last=True)]),
    "rating": ("Highest rated", [F("avg_rating").desc(nulls_last=True), "-log_count"]),
}
WHEN = {"": "Any time", "upcoming": "Upcoming", "past": "Past"}
from .roles import can_delete_event, can_edit_event
from .setlistfm import SetlistFmError, get_setlist, import_setlist, parse_event_date, search_setlists


def _safe_next(request: HttpRequest, fallback: str) -> str:
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return fallback


def _reviews(logs_qs):
    return (
        logs_qs.select_related("user__profile", "event")
        .filter(~Q(review="") | Q(rating__isnull=False))
        .annotate(like_count=Count("likes", distinct=True), comment_count=Count("comments", distinct=True))
        .order_by("-created_at")
    )


def _can_edit(user, event: Event) -> bool:
    return can_edit_event(user, event)


# --------------------------------------------------------------------------- browse


def home(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    events = Event.objects.with_stats().select_related("venue")
    return render(
        request,
        "events/home.html",
        {
            "upcoming": events.filter(date__gte=today).order_by("date")[:6],
            "recent": events.filter(date__lt=today).order_by("-date")[:6],
            "reviews": _reviews(Log.objects.exclude(review=""))[:6],
            "lists": EventList.objects.filter(items__isnull=False).select_related("user")
            .annotate(item_count=Count("items")).order_by("-updated_at")[:6],
            "event_types": EventType.choices,
            "friends_activity": _friends_activity(request),
        },
    )


def _friends_activity(request: HttpRequest) -> list:
    if not request.user.is_authenticated:
        return []
    from .views_social import activity_for

    ids = list(request.user.following_set.values_list("following_id", flat=True))
    return activity_for(ids, limit=8) if ids else []


def event_list(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    event_type = request.GET.get("type") or ""
    city = (request.GET.get("city") or "").strip()
    year = request.GET.get("year") or ""
    when = request.GET.get("when") or ""
    sort = request.GET.get("sort") or "newest"
    if sort not in SORTS:
        sort = "newest"

    events = Event.objects.select_related("venue", "production")
    if event_type in EventType.values:
        events = events.filter(event_type=event_type)
    if city:
        events = events.filter(venue__city__iexact=city)
    if year.isdigit():
        events = events.filter(date__year=int(year))
    today = timezone.localdate()
    if when == "upcoming":
        events = events.filter(date__gte=today)
    elif when == "past":
        events = events.filter(date__lt=today)
    if q:
        matching = Event.objects.filter(
            Q(title__icontains=q)
            | Q(performers__name__icontains=q)
            | Q(venue__name__icontains=q)
            | Q(venue__city__icontains=q)
        ).values("pk")
        events = events.filter(pk__in=matching)
    events = events.with_stats().order_by(*SORTS[sort][1])

    cities = (
        Venue.objects.exclude(city="").filter(events__isnull=False)
        .values_list("city", flat=True).distinct().order_by("city")
    )
    years = (
        Event.objects.exclude(date__isnull=True).annotate(y=ExtractYear("date"))
        .values_list("y", flat=True).distinct().order_by("-y")
    )

    filters = {"q": q, "type": event_type, "city": city, "year": year, "when": when, "sort": sort}
    page = Paginator(events, 24).get_page(request.GET.get("page"))
    return render(
        request,
        "events/event_list.html",
        {
            "page": page,
            "f": filters,
            "querystring": urlencode({k: v for k, v in filters.items() if v}),
            "event_types": EventType.choices,
            "cities": cities,
            "years": years,
            "sorts": [(k, label) for k, (label, _) in SORTS.items()],
            "whens": WHEN.items(),
        },
    )


def event_detail(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(
        Event.objects.with_stats().select_related("venue", "production"), pk=pk
    )
    lineup = EventPerformer.objects.filter(event=event).select_related("performer")
    setlists = event.setlists.select_related("performer").prefetch_related(
        Prefetch("entries", queryset=SetlistSong.objects.select_related("song"))
    )
    my_log = None
    wanted = False
    my_lists = EventList.objects.none()
    if request.user.is_authenticated:
        my_log = Log.objects.filter(user=request.user, event=event).first()
        wanted = WantToGo.objects.filter(user=request.user, event=event).exists()
        my_lists = EventList.objects.filter(user=request.user).exclude(items__event=event)
    in_lists = EventList.objects.filter(items__event=event).select_related("user").distinct()

    return render(
        request,
        "events/event_detail.html",
        {
            "event": event,
            "lineup": lineup,
            "setlists": setlists,
            "reviews": _reviews(event.logs.all()),
            "my_log": my_log,
            "log_form": LogForm(instance=my_log),
            "can_edit": _can_edit(request.user, event),
            "can_delete": can_delete_event(request.user, event),
            "wanted": wanted,
            "my_lists": my_lists,
            "in_lists": in_lists[:5],
            "in_lists_count": in_lists.count(),
            "want_count": event.wanted_by.count(),
        },
    )


def production_detail(request: HttpRequest, pk: int) -> HttpResponse:
    production = get_object_or_404(Production, pk=pk)
    logs = Log.objects.filter(event__production=production)
    stats = logs.aggregate(avg_rating=Avg("rating"), log_count=Count("id"))
    return render(
        request,
        "events/production_detail.html",
        {
            "production": production,
            "events": production.events.with_stats().select_related("venue").order_by("date"),
            "reviews": _reviews(logs),
            **stats,
        },
    )


def performer_detail(request: HttpRequest, pk: int) -> HttpResponse:
    performer = get_object_or_404(Performer, pk=pk)
    stats = Log.objects.filter(event__performers=performer).aggregate(
        avg_rating=Avg("rating"), log_count=Count("id")
    )
    return render(
        request,
        "events/performer_detail.html",
        {
            "performer": performer,
            "events": performer.events.with_stats().select_related("venue").distinct(),
            **stats,
        },
    )


def venue_detail(request: HttpRequest, pk: int) -> HttpResponse:
    venue = get_object_or_404(Venue, pk=pk)
    return render(
        request,
        "events/venue_detail.html",
        {"venue": venue, "events": venue.events.with_stats()},
    )


def diary(request: HttpRequest, username: str | None = None) -> HttpResponse:
    if username is None:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return redirect("events:user_diary", username=request.user.get_username())

    owner = get_object_or_404(get_user_model(), username=username)
    logs = (
        Log.objects.filter(user=owner)
        .select_related("event", "event__venue")
        .order_by("-event__date", "-created_at")
    )
    stats = logs.aggregate(avg_rating=Avg("rating"), log_count=Count("id"))
    by_type = logs.values("event__event_type").annotate(n=Count("id")).order_by("-n")
    type_labels = dict(EventType.choices)
    return render(
        request,
        "events/diary.html",
        {
            "owner": owner,
            "tab": "diary",
            "logs": logs,
            "by_type": [(type_labels.get(r["event__event_type"], "Other"), r["n"]) for r in by_type],
            **stats,
        },
    )


# --------------------------------------------------------------------------- create / edit


@login_required
def event_form(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk) if pk else None
    if event and not _can_edit(request.user, event):
        raise PermissionDenied

    initial = {}
    if not event and request.GET.get("production"):
        production = Production.objects.filter(pk=request.GET["production"]).first()
        if production:
            initial = {"production": production, "event_type": production.event_type}

    form = EventForm(request.POST or None, instance=event, initial=initial)
    formset = EventPerformerFormSet(request.POST or None, instance=event or Event())

    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            saved = form.save(commit=False)
            if not saved.created_by_id:
                saved.created_by = request.user
            saved.save()
            formset.instance = saved
            formset.save()
        messages.success(request, "Event saved.")
        return redirect(saved)

    return render(request, "events/event_form.html", {"form": form, "formset": formset, "event": event})


@login_required
def event_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Admins only: remove an event and everything attached to it."""
    event = get_object_or_404(Event, pk=pk)
    if not can_delete_event(request.user, event):
        raise PermissionDenied
    if request.method == "POST":
        title = event.title
        event.delete()
        messages.success(request, f"Deleted “{title}”.")
        return redirect("events:event_list")
    impact = {
        "logs": event.logs.count(),
        "setlists": event.setlists.count(),
        "lists": event.list_items.count(),
        "want_to_go": event.wanted_by.count(),
        "favorites": event.favorited_by.count(),
    }
    return render(request, "events/event_confirm_delete.html", {"event": event, "impact": impact})


def _simple_create(request: HttpRequest, form_class, title: str) -> HttpResponse:
    form = form_class(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        messages.success(request, f"{title} “{obj}” created.")
        return redirect(_safe_next(request, obj.get_absolute_url()))
    return render(
        request,
        "events/simple_form.html",
        {"form": form, "title": f"New {title.lower()}", "next": _safe_next(request, "")},
    )


@login_required
def performer_create(request: HttpRequest) -> HttpResponse:
    return _simple_create(request, PerformerForm, "Performer")


@login_required
def venue_create(request: HttpRequest) -> HttpResponse:
    return _simple_create(request, VenueForm, "Venue")


@login_required
def production_create(request: HttpRequest) -> HttpResponse:
    return _simple_create(request, ProductionForm, "Production")


# --------------------------------------------------------------------------- diary entries


@login_required
@require_POST
def log_save(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk)
    log = Log.objects.filter(user=request.user, event=event).first()
    form = LogForm(request.POST, instance=log)
    if form.is_valid():
        entry = form.save(commit=False)
        entry.user = request.user
        entry.event = event
        entry.save()
        messages.success(request, "Saved to your diary.")
    else:
        messages.error(request, "Could not save your log: " + form.errors.as_text())
    return redirect(event)


@login_required
@require_POST
def log_delete(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk)
    deleted, _ = Log.objects.filter(user=request.user, event=event).delete()
    if deleted:
        messages.success(request, "Removed from your diary.")
    return redirect(event)


# --------------------------------------------------------------------------- setlist.fm


@login_required
def setlist_search(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk)
    headliner = event.headliner
    artist = (request.GET.get("artist") or (headliner.name if headliner else "")).strip()
    match_date = request.GET.get("any_date") != "1" and event.date is not None

    results: list[dict] = []
    error = None
    if artist:
        try:
            results = search_setlists(artist_name=artist, on_date=event.date if match_date else None)
        except SetlistFmError as e:
            error = str(e)
    for r in results:
        r["parsed_date"] = parse_event_date(r.get("eventDate"))

    return render(
        request,
        "events/setlist_search.html",
        {"event": event, "artist": artist, "match_date": match_date, "results": results, "error": error},
    )


@login_required
@require_POST
def setlist_import(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk)
    setlist_id = (request.POST.get("setlist_id") or "").strip()
    if not setlist_id:
        messages.error(request, "Missing setlist id.")
        return redirect("events:setlist_search", pk=event.pk)
    try:
        setlist = import_setlist(event, get_setlist(setlist_id))
    except SetlistFmError as e:
        messages.error(request, str(e))
        return redirect("events:setlist_search", pk=event.pk)

    messages.success(request, f"Imported {setlist.entries.count()} songs for {setlist.performer}.")
    return redirect(event)
