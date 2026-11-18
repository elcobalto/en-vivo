from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import models, transaction
from django.db.models import Avg, Count, F, Max, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import ExtractYear
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import RATING_CHOICES, EventForm, EventPerformerFormSet, LogForm, PerformerForm, PlayForm, ProductionForm, VenueForm
from .models import (
    CREDIT_ROLES,
    ActLog,
    Event,
    EventList,
    EventPerformer,
    EventType,
    Log,
    Performer,
    Play,
    Production,
    SetlistSong,
    Venue,
    WantToGo,
)

SORTS = {
    "newest": ("Latest date first", [F("date").desc(nulls_last=True), "title"]),
    "oldest": ("Earliest date first", [F("date").asc(nulls_last=True), "title"]),
    "popular": ("Most logged", ["-log_count", F("date").desc(nulls_last=True)]),
    "rating": ("Highest rated", [F("avg_rating").desc(nulls_last=True), "-log_count"]),
    "added": ("Recently added", ["-created_at", "-pk"]),
}
WHEN = {"": "Any time", "upcoming": "Upcoming", "past": "Past"}
from .roles import can_delete_event, can_edit_event, home_place, is_admin
from .merge import merge_into_same_festival_day
from .views_merge import (
    festival_admin_actions,
    festival_suggestion,
    performer_duplicate_candidates,
    suggestions_for,
)
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
            **_upcoming_near(request, events.filter(date__gte=today).order_by("date")),
            "recent": events.filter(date__lt=today).order_by("-date")[:6],
            "just_added": events.order_by("-created_at")[:6],
            "reviews": _reviews(Log.objects.exclude(review=""))[:6],
            "lists": EventList.objects.filter(items__isnull=False).select_related("user")
            .annotate(item_count=Count("items")).order_by("-updated_at")[:6],
            "event_types": EventType.choices,
            "friends_activity": _friends_activity(request),
        },
    )


def _upcoming_near(request: HttpRequest, upcoming) -> dict:
    """Upcoming events in the user's home city (or country); everyone else sees all of them."""
    city, country = home_place(request.user)
    if city:
        return {"upcoming": upcoming.filter(venue__city__iexact=city)[:6], "upcoming_place": city,
                "upcoming_query": f"when=upcoming&city={city}"}
    if country:
        from django_countries import countries

        return {"upcoming": upcoming.filter(venue__country=country)[:6], "upcoming_place": dict(countries).get(country, country),
                "upcoming_query": "when=upcoming"}
    return {"upcoming": upcoming[:6], "upcoming_place": "", "upcoming_query": "when=upcoming"}


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
    # Upcoming shows read best soonest-first; everything else newest-first.
    default_sort = "oldest" if when == "upcoming" else "newest"
    sort = request.GET.get("sort") or default_sort
    if sort not in SORTS:
        sort = default_sort

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
    lineup = EventPerformer.objects.filter(event=event).select_related("performer", "production").order_by(
        "billing_order", F("start_time").asc(nulls_last=True), "performer__name"  # the order set in the form
    )
    billing = EventPerformer.objects.filter(event=event, performer=OuterRef("performer")).values("billing_order")[:1]
    setlists = event.setlists.select_related("performer").annotate(order=Subquery(billing)).order_by(
        F("order").asc(nulls_last=True), "performer__name"
    ).prefetch_related(
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
            "slot_suggestions": suggestions_for(request.user, event),
            "act_rows": _act_rows(event, [l for l in lineup if l.role not in CREDIT_ROLES], my_log),
            "credits": [l for l in lineup if l.role in CREDIT_ROLES],
            "latest_videos": event.videos.select_related("performer").order_by("-created_at")[:LATEST_VIDEOS],
            "video_count": event.videos.count(),
            "rating_choices": RATING_CHOICES,
            "festival_suggestion": festival_suggestion(request.user, event),
            **festival_admin_actions(request.user, event),
            "wanted": wanted,
            "my_lists": my_lists,
            "in_lists": in_lists[:5],
            "in_lists_count": in_lists.count(),
            "want_count": event.wanted_by.count(),
        },
    )


def production_detail(request: HttpRequest, pk: int) -> HttpResponse:
    production = get_object_or_404(Production.objects.select_related("performer"), pk=pk)
    if production.event_type == EventType.FESTIVAL:
        # Festivals group whole events (their days).
        events = Event.objects.filter(production=production)
        logs = Log.objects.filter(event__production=production)
        rated = logs
    else:
        # Tours belong to a performer: its dates are the events where that act was on this tour.
        events = Event.objects.filter(eventperformer__production=production).distinct()
        logs = Log.objects.filter(event__in=events)
        rated = ActLog.objects.filter(log__event__in=events, performer_id=production.performer_id) \
            if production.performer_id else logs
    stats = rated.aggregate(avg_rating=Avg("rating"), log_count=Count("id"))
    return render(
        request,
        "events/production_detail.html",
        {
            "production": production,
            "events": events.with_stats().select_related("venue")
            .prefetch_related(Prefetch("eventperformer_set", queryset=EventPerformer.objects.select_related("performer")
                                       .filter(role=EventPerformer.Role.HEADLINER)))
            .order_by(F("date").asc(nulls_last=True)),
            "reviews": _reviews(logs),
            "production_logs": rated,
            **stats,
        },
    )


LATEST_FIRST = (F("date").desc(nulls_last=True), "-pk")


def _seen_by(request: HttpRequest):
    """The user from ?seen_by=<username> (links from a profile's "Most seen" lists), or None."""
    username = (request.GET.get("seen_by") or "").strip()
    return get_user_model().objects.filter(username=username).select_related("profile").first() if username else None


PERFORMER_SORTS = {
    "events": "Most events",
    "popular": "Most popular",
    "rated": "Highest rated",
    "recent": "Last performed",
}


def performer_list(request: HttpRequest) -> HttpResponse:
    """Every performer; ?q= searches by name, ?country= filters, ?sort= orders (most events by default)."""
    from django_countries import countries as all_countries

    q = request.GET.get("q", "").strip()
    country = request.GET.get("country", "").upper()
    sort = request.GET.get("sort", "events")
    if sort not in PERFORMER_SORTS:
        sort = "events"
    performers = Performer.objects.annotate(
        n=Count("events", distinct=True),
        fans=Count("act_logs__log__user", distinct=True),  # people who logged seeing them
        avg_rating=Avg("act_logs__rating"),
        last_date=Max("events__date"),
    )
    if q:
        performers = performers.filter(name__icontains=q)
    if country:
        performers = performers.filter(country=country)
    order = {
        "events": ["-n", "name"],
        "popular": ["-fans", "-n", "name"],
        "rated": [F("avg_rating").desc(nulls_last=True), "-fans", "name"],
        "recent": [F("last_date").desc(nulls_last=True), "name"],
    }[sort]
    page = Paginator(performers.order_by(*order), 48).get_page(request.GET.get("page"))
    used = set(Performer.objects.exclude(country="").values_list("country", flat=True).distinct())
    filters = {"q": q, "country": country, "sort": sort if sort != "events" else ""}
    return render(
        request, "events/performer_list.html",
        {
            "page": page, "q": q, "country": country, "sort": sort, "sorts": PERFORMER_SORTS,
            "countries": [(code, name) for code, name in all_countries if code in used],
            "querystring": urlencode({k: v for k, v in filters.items() if v}),
        },
    )


def performer_detail(request: HttpRequest, pk: int) -> HttpResponse:
    performer = get_object_or_404(Performer, pk=pk)
    # Ratings for the artist come from people who saw this act, not from the whole event.
    act_logs = ActLog.objects.filter(performer=performer)
    stats = act_logs.aggregate(avg_rating=Avg("rating"), log_count=Count("id"))
    events = performer.events.all()
    seen_by = _seen_by(request)
    if seen_by:  # only the shows where this person saw the act
        events = events.filter(logs__user=seen_by, logs__acts__performer=performer)
    return render(
        request,
        "events/performer_detail.html",
        {
            "performer": performer,
            "tours": performer.tours.order_by("title"),
            **(dict(zip(("merge_similar", "merge_rest"), performer_duplicate_candidates(performer)))
               if is_admin(request.user) else {}),
            "act_logs": act_logs,
            "events": events.with_stats().select_related("venue").distinct().order_by(*LATEST_FIRST),
            "seen_by": seen_by,
            **stats,
        },
    )


def venue_detail(request: HttpRequest, pk: int) -> HttpResponse:
    venue = get_object_or_404(Venue, pk=pk)
    events = venue.events.all()
    seen_by = _seen_by(request)
    if seen_by:
        events = events.filter(logs__user=seen_by)
    return render(
        request,
        "events/venue_detail.html",
        {"venue": venue, "seen_by": seen_by,
         "events": events.with_stats().select_related("venue").distinct().order_by(*LATEST_FIRST)},
    )


def diary(request: HttpRequest, username: str | None = None) -> HttpResponse:
    if username is None:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return redirect("events:user_diary", username=request.user.get_username())

    owner = get_object_or_404(get_user_model(), username=username)
    all_logs = Log.objects.filter(user=owner)
    event_type = request.GET.get("type") or ""
    year = request.GET.get("year") or ""
    logs = all_logs.select_related("event", "event__venue").order_by("-event__date", "-created_at")
    if event_type in EventType.values:
        logs = logs.filter(event__event_type=event_type)
    if year.isdigit():
        logs = logs.filter(event__date__year=int(year))
    years = (all_logs.exclude(event__date=None).annotate(y=ExtractYear("event__date"))
             .values_list("y", flat=True).distinct().order_by("-y"))
    types_logged = all_logs.values_list("event__event_type", flat=True).distinct()
    expanded = request.GET.get("view") == "expanded"
    if expanded:  # posters and lineups: fetch every event's bill in one query
        logs = logs.prefetch_related(Prefetch(
            "event__eventperformer_set",
            queryset=EventPerformer.objects.select_related("performer").order_by("billing_order", "performer__name"),
            to_attr="bill",
        ))
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
            "f": {"type": event_type, "year": year},
            "expanded": expanded,
            "years": years,
            "event_types": [(v, l) for v, l in EventType.choices if v in set(types_logged)],
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
    if not event:
        initial["new_venue_city"], initial["new_venue_country"] = home_place(request.user)
    if not event and request.GET.get("production"):
        production = Production.objects.filter(pk=request.GET["production"]).first()
        if production:
            initial.update(production=production, event_type=production.event_type)

    if not event and request.GET.get("play"):
        play = Play.objects.filter(pk=request.GET["play"]).first()
        if play:
            initial.update(play=play, event_type=EventType.THEATER)

    form = EventForm(request.POST or None, instance=event, initial=initial)
    formset = EventPerformerFormSet(request.POST or None, instance=event or Event())

    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            form.create_new_venue()
            saved = form.save(commit=False)
            if not saved.created_by_id:
                saved.created_by = request.user
            saved.save()
            formset.instance = saved
            formset.save()
            if saved.play_id:
                _credit_play_authors(saved)
            # Whoever is first on the bill is the headliner (credits like authors excepted).
            first = saved.acts_on_stage().order_by("billing_order", "pk").first()
            if first and first.role != EventPerformer.Role.HEADLINER:
                first.role = EventPerformer.Role.HEADLINER
                first.save(update_fields=["role"])
            merged = merge_into_same_festival_day(saved)
            if not merged and form.cleaned_data.get("photo_for_all_days") and saved.image_url:
                days = Event.objects.filter(production=saved.production, date__year=saved.date.year).exclude(pk=saved.pk)
                updated = [d.pk for d in days if can_edit_event(request.user, d)]
                Event.objects.filter(pk__in=updated).update(image_url=saved.image_url,
                                                          image_display=saved.image_display)
                messages.info(request, f"Photo also used for {len(updated)} other day{'s' if len(updated) != 1 else ''}"
                                       f" of {saved.production.title} {saved.date.year}.")
        if merged:
            messages.info(request, f"“{merged.title}” already existed on that day, so this was added to it.")
            return redirect(merged)
        messages.success(request, "Event saved.")
        return redirect(saved)

    return render(request, "events/event_form.html", {"form": form, "formset": formset, "event": event})


def _credit_play_authors(event: Event) -> None:
    """A staging credits its play's authors (“Written by”) unless they're already on it."""
    on_event = set(event.eventperformer_set.values_list("performer_id", flat=True))
    last = event.eventperformer_set.aggregate(m=models.Max("billing_order"))["m"] or 0
    for i, author in enumerate(event.play.authors.exclude(pk__in=on_event), start=1):
        EventPerformer.objects.create(event=event, performer=author, role=EventPerformer.Role.AUTHOR, billing_order=last + i)
    event.refresh_auto_title()


def play_list(request: HttpRequest) -> HttpResponse:
    q = request.GET.get("q", "").strip()
    plays = Play.objects.prefetch_related("authors").annotate(n=Count("stagings", distinct=True))
    if q:
        plays = plays.filter(Q(title__icontains=q) | Q(authors__name__icontains=q)).distinct()
    return render(request, "events/play_list.html", {"plays": plays.order_by("title"), "q": q})


def play_detail(request: HttpRequest, pk: int) -> HttpResponse:
    play = get_object_or_404(Play.objects.prefetch_related("authors"), pk=pk)
    stagings = play.stagings.with_stats().select_related("venue").order_by(F("date").desc(nulls_last=True))
    return render(request, "events/play_detail.html", {"play": play, "stagings": stagings})


@login_required
def play_create(request: HttpRequest) -> HttpResponse:
    return _simple_create(request, PlayForm, "Play")


@login_required
def play_edit(request: HttpRequest, pk: int) -> HttpResponse:
    return _simple_edit(request, Play, pk, PlayForm, "Play")


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


def _simple_create(request: HttpRequest, form_class, title: str,
                   template: str = "events/simple_form.html") -> HttpResponse:
    form = form_class(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        messages.success(request, f"{title} “{obj}” created.")
        return redirect(_safe_next(request, obj.get_absolute_url()))
    return render(
        request, template, {"form": form, "title": f"New {title.lower()}", "next": _safe_next(request, "")}
    )


def _simple_edit(request: HttpRequest, model, pk: int, form_class, title: str,
                 template: str = "events/simple_form.html") -> HttpResponse:
    """Admins only: edit catalog data shared by every event (performers, venues, productions)."""
    if not is_admin(request.user):
        raise PermissionDenied
    obj = get_object_or_404(model, pk=pk)
    form = form_class(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        messages.success(request, f"{title} “{obj}” saved.")
        return redirect(obj.get_absolute_url())
    context = {"form": form, "title": f"Edit {title.lower()}", "next": obj.get_absolute_url()}
    if model is Venue:
        context.update(venue=obj, next="", link_url=reverse("events:venue_link_place", args=[obj.pk]))
    return render(request, template, context)


@login_required
def performer_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Admins only: remove a performer from the catalog (events and tours stay)."""
    if not is_admin(request.user):
        raise PermissionDenied
    performer = get_object_or_404(Performer, pk=pk)
    if request.method == "POST":
        name = performer.name
        with transaction.atomic():
            # Setlist songs protect their Song rows, so remove the setlists (and their entries) first.
            SetlistSong.objects.filter(song__performer=performer).delete()
            performer.setlists.all().delete()
            performer.delete()  # lineup spots, songs and acts seen go with it; tours lose their artist
        messages.success(request, f"Deleted “{name}”.")
        return redirect("events:event_list")
    impact = {
        "events": performer.events.distinct().count(),
        "setlists": performer.setlists.count(),
        "act_logs": performer.act_logs.count(),
        "tours": performer.tours.count(),
    }
    return render(request, "events/performer_confirm_delete.html", {"performer": performer, "impact": impact})


@login_required
def performer_edit(request: HttpRequest, pk: int) -> HttpResponse:
    return _simple_edit(request, Performer, pk, PerformerForm, "Performer")


@login_required
def venue_edit(request: HttpRequest, pk: int) -> HttpResponse:
    return _simple_edit(request, Venue, pk, VenueForm, "Venue", template="events/venue_form.html")


@login_required
def production_edit(request: HttpRequest, pk: int) -> HttpResponse:
    return _simple_edit(request, Production, pk, ProductionForm, "Production")


@login_required
def performer_create(request: HttpRequest) -> HttpResponse:
    return _simple_create(request, PerformerForm, "Performer")


@login_required
def venue_create(request: HttpRequest) -> HttpResponse:
    return _simple_create(request, VenueForm, "Venue", template="events/venue_form.html")


@login_required
def production_create(request: HttpRequest) -> HttpResponse:
    return _simple_create(request, ProductionForm, "Production")


# --------------------------------------------------------------------------- diary entries


@login_required
@require_POST
def log_save(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk)
    if not event.has_happened:
        messages.error(
            request, f"“{event.title}” hasn't happened yet. You can log it from {event.date.day} {event.date:%b %Y}."
        )
        return redirect(event)
    log = Log.objects.filter(user=request.user, event=event).first()
    form = LogForm(request.POST, instance=log)
    if form.is_valid():
        entry = form.save(commit=False)
        entry.user = request.user
        entry.event = event
        entry.save()
        if request.POST.get("acts_submitted"):
            _save_acts(entry, request.POST)
        messages.success(request, "Saved to your diary.")
    else:
        messages.error(request, "Could not save your log: " + form.errors.as_text())
    return redirect(event)


def _save_acts(log: Log, data) -> None:
    """Sync which acts the user saw at this event, with each act's rating and note."""
    for performer_id in log.event.acts_on_stage().values_list("performer_id", flat=True):
        if data.get(f"act_seen_{performer_id}"):
            raw = data.get(f"act_rating_{performer_id}") or ""
            rating = int(raw) if raw.isdigit() and 1 <= int(raw) <= 10 else None
            ActLog.objects.update_or_create(
                log=log, performer_id=performer_id,
                defaults={"rating": rating, "note": (data.get(f"act_note_{performer_id}") or "").strip()[:200]},
            )
        else:
            ActLog.objects.filter(log=log, performer_id=performer_id).delete()


LATEST_VIDEOS = 4  # the event page shows only the newest; the rest are on the videos page


def event_videos(request: HttpRequest, pk: int) -> HttpResponse:
    """Every video of an event, grouped by act."""
    event = get_object_or_404(Event.objects.select_related("venue"), pk=pk)
    lineup = list(EventPerformer.objects.filter(event=event).select_related("performer")
                  .exclude(role__in=CREDIT_ROLES).order_by("billing_order", "pk"))
    return render(request, "events/event_videos.html", {
        "event": event, "video_groups": _video_groups(event, lineup), "acts": lineup,
    })


def _video_groups(event: Event, lineup) -> list[tuple[str, object, list]]:
    """Videos grouped as (label, performer or None, videos): whole event first, then acts in billing order."""
    videos = list(event.videos.select_related("performer", "added_by"))
    groups = []
    whole = [v for v in videos if v.performer_id is None]
    if whole:
        groups.append(("Whole event", None, whole))
    for link in lineup:
        mine = [v for v in videos if v.performer_id == link.performer_id]
        if mine:
            groups.append((link.performer.name, link.performer, mine))
    return groups


def _act_rows(event: Event, lineup, my_log: Log | None) -> list[dict]:
    """One row per act for the event page: my seen/rating/note plus everyone's average."""
    mine = {a.performer_id: a for a in my_log.acts.all()} if my_log else {}
    stats = {
        row["performer_id"]: row
        for row in ActLog.objects.filter(log__event=event).values("performer_id")
        .annotate(avg=Avg("rating"), seen_by=Count("id"))
    }
    rows = []
    for link in lineup:
        act = mine.get(link.performer_id)
        rows.append({
            "link": link,
            "seen": bool(act) if my_log else True,  # a new log starts with every act ticked
            "rating": act.rating if act else None,
            "note": act.note if act else "",
            "avg": (stats.get(link.performer_id) or {}).get("avg"),
            "seen_by": (stats.get(link.performer_id) or {}).get("seen_by", 0),
        })
    return rows


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
    # Suggest the highest-billed act that doesn't have a setlist on this event yet.
    next_act = (
        EventPerformer.objects.filter(event=event).exclude(role__in=CREDIT_ROLES)
        .exclude(performer__setlists__event=event)
        .select_related("performer").order_by("billing_order", "pk").first()
    )
    default = next_act.performer.name if next_act else (event.headliner.name if event.headliner else "")
    artist = (request.GET.get("artist") or default).strip()
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
    except Exception:
        logging.getLogger(__name__).exception("Importing setlist %s into event %s failed", setlist_id, event.pk)
        messages.error(request, "Something went wrong importing that setlist. Nothing was saved.")
        return redirect("events:setlist_search", pk=event.pk)

    messages.success(request, f"Imported {setlist.entries.count()} songs for {setlist.performer}.")
    return redirect(event)
