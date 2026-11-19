from __future__ import annotations

import logging
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import models, transaction
from django.db.models import Avg, Count, F, Max, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import ExtractYear
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import RATING_CHOICES, EventForm, EventArtistFormSet, LogForm, ArtistForm, PlayForm, ProductionForm, VenueForm
from .models import (
    CREDIT_ROLES,
    Album,
    normalize_name,
    ActLog,
    Event,
    EventList,
    EventArtist,
    EventType,
    Log,
    Artist,
    Play,
    Production,
    Setlist,
    SetlistSong,
    Song,
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
from .albums import album_rows, remember_album, top_album
from .merge import merge_into_same_festival_day, share_photo_with_festival_days
from .views_merge import (
    festival_admin_actions,
    festival_suggestion,
    artist_duplicate_candidates,
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
            | Q(artists__name__icontains=q)
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
    lineup = EventArtist.objects.filter(event=event).select_related("artist", "production").order_by(
        "billing_order", F("start_time").asc(nulls_last=True), "artist__name"  # the order set in the form
    )
    billing = EventArtist.objects.filter(event=event, artist=OuterRef("artist")).values("billing_order")[:1]
    setlists = event.setlists.select_related("artist").annotate(order=Subquery(billing)).order_by(
        F("order").asc(nulls_last=True), "artist__name"
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
    # Each act's setlists open under it in the lineup; setlists for anyone not on the bill go after.
    setlists = list(setlists) if event.is_music else []  # other kinds of shows don't have setlists
    act_rows = _act_rows(event, [l for l in lineup if l.role not in CREDIT_ROLES], my_log)
    for row in act_rows:
        row["setlists"] = [sl for sl in setlists if sl.artist_id == row["link"].artist_id]
    on_bill = {row["link"].artist_id for row in act_rows}

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
            "act_rows": act_rows,
            "other_setlists": [sl for sl in setlists if sl.artist_id not in on_bill],
            "credits": [l for l in lineup if l.role in CREDIT_ROLES],
            "latest_videos": event.videos.select_related("artist").order_by("-created_at")[:LATEST_VIDEOS],
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
    production = get_object_or_404(Production.objects.select_related("artist"), pk=pk)
    if production.event_type == EventType.FESTIVAL:
        # Festivals group whole events (their days).
        events = Event.objects.filter(production=production)
        logs = Log.objects.filter(event__production=production)
        rated = logs
    else:
        # Tours belong to a artist: its dates are the events where that act was on this tour.
        events = Event.objects.filter(eventartist__production=production).distinct()
        logs = Log.objects.filter(event__in=events)
        rated = ActLog.objects.filter(log__event__in=events, artist_id=production.artist_id) \
            if production.artist_id else logs
    stats = rated.aggregate(avg_rating=Avg("rating"), log_count=Count("id"))
    return render(
        request,
        "events/production_detail.html",
        {
            "production": production,
            "events": events.with_stats().select_related("venue")
            .prefetch_related(Prefetch("eventartist_set", queryset=EventArtist.objects.select_related("artist")
                                       .filter(role=EventArtist.Role.HEADLINER)))
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


ARTIST_SORTS = {
    "events": "Most events",
    "popular": "Most popular",
    "rated": "Highest rated",
    "recent": "Last performed",
}


def artist_list(request: HttpRequest) -> HttpResponse:
    """Every artist; ?q= searches by name, ?country= filters, ?sort= orders (most events by default)."""
    from django_countries import countries as all_countries

    q = request.GET.get("q", "").strip()
    country = request.GET.get("country", "").upper()
    sort = request.GET.get("sort", "events")
    if sort not in ARTIST_SORTS:
        sort = "events"
    artists = Artist.objects.annotate(
        n=Count("events", distinct=True),
        fans=Count("act_logs__log__user", distinct=True),  # people who logged seeing them
        avg_rating=Avg("act_logs__rating"),
        last_date=Max("events__date"),
    )
    if q:
        artists = artists.filter(name__icontains=q)
    if country == "NONE":  # artists nobody has set a country for
        artists = artists.filter(country="")
    elif country:
        artists = artists.filter(country=country)
    order = {
        "events": ["-n", "name"],
        "popular": ["-fans", "-n", "name"],
        "rated": [F("avg_rating").desc(nulls_last=True), "-fans", "name"],
        "recent": [F("last_date").desc(nulls_last=True), "name"],
    }[sort]
    page = Paginator(artists.order_by(*order), 48).get_page(request.GET.get("page"))
    used = set(Artist.objects.exclude(country="").values_list("country", flat=True).distinct())
    filters = {"q": q, "country": country, "sort": sort if sort != "events" else ""}
    return render(
        request, "events/artist_list.html",
        {
            "page": page, "q": q, "country": country, "sort": sort, "sorts": ARTIST_SORTS,
            "countries": [(code, name) for code, name in all_countries if code in used],
            "querystring": urlencode({k: v for k, v in filters.items() if v}),
        },
    )


def _artist_plays(artist: Artist, events, *, skip_empty: bool = False, sort: str = "count"):
    """How often each song and album was played by this act across these events' setlists (no tapes)."""
    plays = SetlistSong.objects.filter(setlist__artist=artist, setlist__event__in=events, is_tape=False)
    songs = (plays.values("song_id", "song__title", "song__album_title", "song__album_year")
             .annotate(n=Count("setlist", distinct=True)).order_by("-n", "song__title"))
    played = {
        r["song__album_title"]: {"title": r["song__album_title"], "year": r["song__album_year"],
                                 "n": r["n"], "songs": r["songs"]}
        for r in plays.exclude(song__album_title="").values("song__album_title", "song__album_year")
        .annotate(n=Count("id"), songs=Count("song", distinct=True))
    }
    shows = Setlist.objects.filter(artist=artist, event__in=events).count()
    return songs, album_rows(artist, played, skip_empty=skip_empty, sort=sort), shows


@login_required
@require_POST
def artist_albums_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Admins only: add an album to an artist (so unplayed albums show), or remove one."""
    if not is_admin(request.user):
        raise PermissionDenied
    artist = get_object_or_404(Artist, pk=pk)
    if request.POST.get("delete"):
        Album.objects.filter(artist=artist, pk=request.POST["delete"]).delete()
        messages.success(request, "Album removed.")
    else:
        title = " ".join((request.POST.get("title") or "").split())[:240]
        raw_year = (request.POST.get("year") or "").strip()
        year = int(raw_year) if raw_year.isdigit() and 1000 < int(raw_year) < 3000 else None
        if not title:
            messages.error(request, "Give the album a title.")
        elif any(normalize_name(a.title) == normalize_name(title) for a in artist.albums.all()):
            messages.info(request, f"“{title}” is already in {artist.name}'s albums.")
        else:
            Album.objects.create(artist=artist, title=title, year=year)
            messages.success(request, f"Added “{title}”.")
    return redirect(_safe_next(request, artist.get_absolute_url()))


def artist_songs(request: HttpRequest, pk: int) -> HttpResponse:
    """The whole list of songs and albums this act played (optionally only shows ?seen_by= saw)."""
    artist = get_object_or_404(Artist, pk=pk)
    seen_by = _seen_by(request)
    events = artist.events.all()
    if seen_by:
        events = events.filter(logs__user=seen_by, logs__acts__artist=artist)
    skip_empty = request.GET.get("empty") == "skip"
    sort = "newest" if request.GET.get("sort") == "newest" else "count"
    songs, albums, shows = _artist_plays(artist, events.distinct(), skip_empty=skip_empty, sort=sort)
    tab = "albums" if request.GET.get("tab") == "albums" else "songs"
    return render(request, "events/artist_songs.html", {
        "artist": artist, "seen_by": seen_by, "tab": tab, "shows": shows,
        "songs": songs, "albums": albums, "skip_empty": skip_empty, "album_sort": sort,
        "album_keep": [("tab", "albums")] + ([("seen_by", seen_by.get_username())] if seen_by else []),
        "seen_q": f"seen_by={quote(seen_by.get_username())}" if seen_by else "",
    })


def artist_detail(request: HttpRequest, pk: int) -> HttpResponse:
    artist = get_object_or_404(Artist, pk=pk)
    # Ratings for the artist come from people who saw this act, not from the whole event.
    act_logs = ActLog.objects.filter(artist=artist)
    stats = act_logs.aggregate(avg_rating=Avg("rating"), log_count=Count("id"))
    events = artist.events.all()
    seen_by = _seen_by(request)
    if seen_by:  # only the shows where this person saw the act
        events = events.filter(logs__user=seen_by, logs__acts__artist=artist)
    skip_empty = request.GET.get("empty") == "skip"
    album_sort = "count" if request.GET.get("sort") == "count" else "newest"
    songs, albums, setlist_shows = _artist_plays(artist, events.distinct(), skip_empty=skip_empty, sort=album_sort)
    return render(
        request,
        "events/artist_detail.html",
        {
            "artist": artist,
            "tours": artist.tours.order_by("title"),
            **(dict(zip(("merge_similar", "merge_rest"), artist_duplicate_candidates(artist)))
               if is_admin(request.user) else {}),
            "act_logs": act_logs,
            "events": events.with_stats().select_related("venue").distinct().order_by(*LATEST_FIRST),
            "seen_by": seen_by,
            "top_songs": songs[:10],
            "album_chart": albums,
            "top_album": top_album(albums),
            "skip_empty": skip_empty,
            "album_sort": album_sort,
            "album_keep": [("seen_by", seen_by.get_username())] if seen_by else [],
            "can_fix_albums": is_admin(request.user),
            "artist_albums": artist.albums.all() if is_admin(request.user) else [],
            "setlist_shows": setlist_shows,
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
            "event__eventartist_set",
            queryset=EventArtist.objects.select_related("artist").order_by("billing_order", "artist__name"),
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
            # Expanded rows are heavier (posters, lineups), so fewer per page.
            "logs": (page := Paginator(logs, 25 if expanded else 100).get_page(request.GET.get("page"))),
            "page": page,
            "querystring": urlencode({k: v for k, v in
                                      {"type": event_type, "year": year, "view": "expanded" if expanded else ""}.items() if v}),
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
    formset = EventArtistFormSet(request.POST or None, instance=event or Event())

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
            # Headliners go above support acts; with no headliner, whoever is first on the bill is one.
            saved.order_bill()
            first = saved.acts_on_stage().order_by("billing_order", "pk").first()
            if first and first.role != EventArtist.Role.HEADLINER:
                first.role = EventArtist.Role.HEADLINER
                first.save(update_fields=["role"])
            merged = merge_into_same_festival_day(saved)
            if not merged and form.cleaned_data.get("photo_for_all_days") and saved.image_url:
                updated = share_photo_with_festival_days(request.user, saved)
                messages.info(request, f"Photo also used for {updated} other day{'s' if updated != 1 else ''}"
                                       f" of {saved.production.title} {saved.date.year}.")
        if merged:
            messages.info(request, f"“{merged.title}” already existed on that day, so this was added to it.")
            return redirect(merged)
        messages.success(request, "Event saved.")
        return redirect(saved)

    return render(request, "events/event_form.html", {"form": form, "formset": formset, "event": event})


@login_required
def festival_add_artists(request: HttpRequest, pk: int) -> HttpResponse:
    """Festival days: add the acts setlist.fm has at this city (or venue) on this date."""
    from .setlistfm import _match_artist

    event = get_object_or_404(Event.objects.select_related("venue"), pk=pk)
    if event.event_type != EventType.FESTIVAL or not _can_edit(request.user, event):
        raise PermissionDenied
    on_bill = {normalize_name(n) for n in event.eventartist_set.values_list("artist__name", flat=True)}
    candidates, error = [], ""
    if event.date and event.venue_id:
        where = {"city": event.venue.city} if event.venue.city else {"venue": event.venue.name}
        seen = set()
        try:
            for page_no in (1, 2, 3):  # 20 per page; big festivals need a few pages
                results = search_setlists(on_date=event.date, page=page_no, **where)
                for item in results:
                    sfm_artist = item.get("artist") or {}
                    name = (sfm_artist.get("name") or "").strip()
                    key = normalize_name(name)
                    if not name or key in seen or key in on_bill:
                        continue
                    seen.add(key)
                    venue_name = ((item.get("venue") or {}).get("name") or "").strip()
                    candidates.append({"key": key, "sfm_artist": sfm_artist, "setlist": item, "name": name, "venue": venue_name,
                                       "same_venue": normalize_name(venue_name) == normalize_name(event.venue.name)})
                if len(results) < 20:
                    break
        except SetlistFmError as e:
            error = str(e)
        candidates.sort(key=lambda c: (not c["same_venue"], c["name"].casefold()))
    if request.method == "POST" and not error:
        chosen = [c for c in candidates if request.POST.get(f"add_{c['key']}")]
        last = event.eventartist_set.aggregate(m=models.Max("billing_order"))["m"] or 0
        setlists, skipped = 0, []
        with transaction.atomic():
            for i, c in enumerate(chosen, start=1):
                artist = _match_artist(c["sfm_artist"])
                role = (EventArtist.Role.HEADLINER if request.POST.get(f"role_{c['key']}") == "headliner"
                        else EventArtist.Role.SUPPORT)
                EventArtist.objects.get_or_create(
                    event=event, artist=artist, defaults={"role": role, "billing_order": last + i},
                )
                if (c["setlist"].get("sets") or {}).get("set"):  # bring their setlist along when it has songs
                    try:
                        with transaction.atomic():
                            import_setlist(event, c["setlist"])
                            setlists += 1
                    except SetlistFmError as e:  # e.g. already attached to another event: keep the artist
                        skipped.append(f"{c['name']}: {e}")
            event.order_bill()  # new headliners move above the support acts
        event.refresh_auto_title()
        messages.success(request, f"Added {len(chosen)} artist{'s' if len(chosen) != 1 else ''} to the lineup"
                                  f" and {setlists} setlist{'s' if setlists != 1 else ''}.")
        for note in skipped:
            messages.warning(request, f"Setlist not added for {note}")
        return redirect(event)
    return render(request, "events/festival_add_artists.html",
                  {"event": event, "candidates": candidates, "error": error})


def act_setlist(request: HttpRequest, pk: int, artist_pk: int) -> HttpResponse:
    """One act's setlist at an event, with which albums the songs came from."""
    event = get_object_or_404(Event.objects.select_related("venue"), pk=pk)
    if not event.is_music:  # only music shows (and festivals) have setlists
        raise Http404
    setlist = get_object_or_404(
        Setlist.objects.select_related("artist").prefetch_related(
            Prefetch("entries", queryset=SetlistSong.objects.select_related("song").order_by("position"))
        ),
        event=event, artist_id=artist_pk,
    )
    entries = list(setlist.entries.all())
    played_entries = [e for e in entries if not e.is_tape]  # tapes are listed but not counted
    played: dict[str, dict] = {}
    for entry in played_entries:
        if entry.song.album_title:
            row = played.setdefault(entry.song.album_title, {"title": entry.song.album_title,
                                                            "year": entry.song.album_year, "n": 0, "songs": 0})
            row["n"] += 1
            row["songs"] += 1
    pending = sorted({e.song_id for e in played_entries if e.song.album_checked_at is None})
    album_sort = "count" if request.GET.get("sort") == "count" else "newest"
    skip_empty = request.GET.get("empty") == "skip"
    timeline = album_rows(setlist.artist, played, skip_empty=skip_empty, sort=album_sort)
    return render(request, "events/act_setlist.html", {
        "event": event, "setlist": setlist, "entries": entries, "song_count": len(played_entries),
        "top_album": top_album(timeline), "pending": pending, "timeline": timeline,
        "album_sort": album_sort, "skip_empty": skip_empty, "can_fix_albums": is_admin(request.user),
        "known_albums": (Song.objects.filter(artist=setlist.artist).exclude(album_title="")
                         .values_list("album_title", flat=True).distinct().order_by("album_title")
                         if is_admin(request.user) else []),
        "songs": list({e.song_id: e.song for e in played_entries}.values()),
        # After one pass the page reloads with ?albums=done; songs still unchecked then wait for a click.
        "auto_lookup": request.GET.get("albums") != "done",
    })


@require_POST
def song_album_lookup(request: HttpRequest, pk: int) -> JsonResponse:
    """Find (once) the album a song came from. The setlist page calls this one song at a time."""
    import time

    from django.core.cache import cache

    from .musicbrainz import song_album

    song = get_object_or_404(Song.objects.select_related("artist"), pk=pk)
    if song.album_checked_at is None:
        # MusicBrainz allows about one request per second from us, across all visitors.
        wait = (cache.get("mb-last-call") or 0) + 1.1 - time.time()
        if wait > 0:
            time.sleep(min(wait, 2))
        cache.set("mb-last-call", time.time(), 10)
        try:
            found = song_album(song.title, song.artist.name, song.artist.mbid or "")
        except Exception as e:  # busy or unreachable: leave it unchecked so it's tried again later
            logging.getLogger(__name__).warning("Album lookup for song %s failed: %r", song.pk, e)
            return JsonResponse({"ok": False}, status=503)
        if found:
            song.album_title, song.album_year, song.album_mbid = found["title"][:240], found["year"], found["mbid"]
        song.album_checked_at = timezone.now()
        song.save(update_fields=["album_title", "album_year", "album_mbid", "album_checked_at"])
        remember_album(song)
    return JsonResponse({"ok": True, "album": song.album_title, "year": song.album_year})


@login_required
@require_POST
def setlist_albums_edit(request: HttpRequest, pk: int, artist_pk: int) -> HttpResponse:
    """Admins only: correct the albums of a setlist's songs in one go (an empty title means no album)."""
    if not is_admin(request.user):
        raise PermissionDenied
    setlist = get_object_or_404(Setlist, event_id=pk, artist_id=artist_pk)
    changed = 0
    for song in Song.objects.filter(setlist_entries__setlist=setlist).distinct():
        if f"album_title_{song.pk}" not in request.POST:
            continue
        title = " ".join((request.POST.get(f"album_title_{song.pk}") or "").split())[:240]
        raw_year = (request.POST.get(f"album_year_{song.pk}") or "").strip()
        year = int(raw_year) if raw_year.isdigit() and 1000 < int(raw_year) < 3000 and title else None
        if (title, year) == (song.album_title, song.album_year):
            continue
        if title != song.album_title:
            song.album_mbid = ""  # no longer the MusicBrainz match
        song.album_title, song.album_year = title, year
        song.album_checked_at = timezone.now()  # a manual answer is never looked up again
        song.save(update_fields=["album_title", "album_year", "album_mbid", "album_checked_at"])
        remember_album(song)
        changed += 1
    messages.success(request, f"Updated the album of {changed} song{'s' if changed != 1 else ''}.")
    return redirect(reverse("events:act_setlist", args=[pk, artist_pk]))


def _credit_play_authors(event: Event) -> None:
    """A staging credits its play's authors (“Written by”) unless they're already on it."""
    on_event = set(event.eventartist_set.values_list("artist_id", flat=True))
    last = event.eventartist_set.aggregate(m=models.Max("billing_order"))["m"] or 0
    for i, author in enumerate(event.play.authors.exclude(pk__in=on_event), start=1):
        EventArtist.objects.create(event=event, artist=author, role=EventArtist.Role.AUTHOR, billing_order=last + i)
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
    """Admins only: edit catalog data shared by every event (artists, venues, productions)."""
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
def artist_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Admins only: remove a artist from the catalog (events and tours stay)."""
    if not is_admin(request.user):
        raise PermissionDenied
    artist = get_object_or_404(Artist, pk=pk)
    if request.method == "POST":
        name = artist.name
        with transaction.atomic():
            # Setlist songs protect their Song rows, so remove the setlists (and their entries) first.
            SetlistSong.objects.filter(song__artist=artist).delete()
            artist.setlists.all().delete()
            artist.delete()  # lineup spots, songs and acts seen go with it; tours lose their artist
        messages.success(request, f"Deleted “{name}”.")
        return redirect("events:event_list")
    impact = {
        "events": artist.events.distinct().count(),
        "setlists": artist.setlists.count(),
        "act_logs": artist.act_logs.count(),
        "tours": artist.tours.count(),
    }
    return render(request, "events/artist_confirm_delete.html", {"artist": artist, "impact": impact})


@login_required
def artist_edit(request: HttpRequest, pk: int) -> HttpResponse:
    return _simple_edit(request, Artist, pk, ArtistForm, "Artist")


@login_required
def venue_edit(request: HttpRequest, pk: int) -> HttpResponse:
    return _simple_edit(request, Venue, pk, VenueForm, "Venue", template="events/venue_form.html")


@login_required
def production_edit(request: HttpRequest, pk: int) -> HttpResponse:
    return _simple_edit(request, Production, pk, ProductionForm, "Production")


@login_required
def artist_create(request: HttpRequest) -> HttpResponse:
    return _simple_create(request, ArtistForm, "Artist")


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
    for artist_id in log.event.acts_on_stage().values_list("artist_id", flat=True):
        if data.get(f"act_seen_{artist_id}"):
            raw = data.get(f"act_rating_{artist_id}") or ""
            rating = int(raw) if raw.isdigit() and 1 <= int(raw) <= 10 else None
            ActLog.objects.update_or_create(
                log=log, artist_id=artist_id,
                defaults={"rating": rating, "note": (data.get(f"act_note_{artist_id}") or "").strip()[:200]},
            )
        else:
            ActLog.objects.filter(log=log, artist_id=artist_id).delete()


LATEST_VIDEOS = 4  # the event page shows only the newest; the rest are on the videos page


def event_videos(request: HttpRequest, pk: int) -> HttpResponse:
    """Every video of an event, grouped by act."""
    event = get_object_or_404(Event.objects.select_related("venue"), pk=pk)
    lineup = list(EventArtist.objects.filter(event=event).select_related("artist")
                  .exclude(role__in=CREDIT_ROLES).order_by("billing_order", "pk"))
    return render(request, "events/event_videos.html", {
        "event": event, "video_groups": _video_groups(event, lineup), "acts": lineup,
    })


def _video_groups(event: Event, lineup) -> list[tuple[str, object, list]]:
    """Videos grouped as (label, artist or None, videos): whole event first, then acts in billing order."""
    videos = list(event.videos.select_related("artist", "added_by"))
    groups = []
    whole = [v for v in videos if v.artist_id is None]
    if whole:
        groups.append(("Whole event", None, whole))
    for link in lineup:
        mine = [v for v in videos if v.artist_id == link.artist_id]
        if mine:
            groups.append((link.artist.name, link.artist, mine))
    return groups


def _act_rows(event: Event, lineup, my_log: Log | None) -> list[dict]:
    """One row per act for the event page: my seen/rating/note plus everyone's average."""
    mine = {a.artist_id: a for a in my_log.acts.all()} if my_log else {}
    stats = {
        row["artist_id"]: row
        for row in ActLog.objects.filter(log__event=event).values("artist_id")
        .annotate(avg=Avg("rating"), seen_by=Count("id"))
    }
    rows = []
    for link in lineup:
        act = mine.get(link.artist_id)
        rows.append({
            "link": link,
            "seen": bool(act) if my_log else True,  # a new log starts with every act ticked
            "rating": act.rating if act else None,
            "note": act.note if act else "",
            "avg": (stats.get(link.artist_id) or {}).get("avg"),
            "seen_by": (stats.get(link.artist_id) or {}).get("seen_by", 0),
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
    if not event.is_music:  # only music shows (and festivals) have setlists
        raise Http404
    # Suggest the highest-billed act that doesn't have a setlist on this event yet.
    next_act = (
        EventArtist.objects.filter(event=event).exclude(role__in=CREDIT_ROLES)
        .exclude(artist__setlists__event=event)
        .select_related("artist").order_by("billing_order", "pk").first()
    )
    default = next_act.artist.name if next_act else (event.headliner.name if event.headliner else "")
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
    if not event.is_music:  # only music shows (and festivals) have setlists
        raise Http404
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

    messages.success(request, f"Imported {setlist.entries.count()} songs for {setlist.artist}.")
    return redirect(event)
