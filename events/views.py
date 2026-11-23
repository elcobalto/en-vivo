from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import models, transaction
from django.db.models import Avg, Count, Exists, F, Max, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import ExtractYear
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django_countries import countries
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext, gettext_lazy, ngettext
from django.views.decorators.http import require_POST

from .forms import RATING_CHOICES, EventForm, EventArtistFormSet, LogForm, ArtistForm, PlayForm, ProductionForm, VenueForm
from .models import (
    CREDIT_ROLES,
    PLAY_TYPES,
    ROLES_BY_TYPE,
    added_act_role,
    Album,
    normalize_name,
    ActLog,
    Event,
    EventList,
    EventArtist,
    EventType,
    Log,
    Artist,
    ArtistFollow,
    Play,
    Profile,
    Production,
    Setlist,
    SetlistSong,
    Song,
    Venue,
    WantToGo,
)

SORTS = {
    "newest": (gettext_lazy("Latest date first"), [F("date").desc(nulls_last=True), "title"]),
    "oldest": (gettext_lazy("Earliest date first"), [F("date").asc(nulls_last=True), "title"]),
    "popular": (gettext_lazy("Most logged"), ["-log_count", F("date").desc(nulls_last=True)]),
    "rating": (gettext_lazy("Highest rated"), [F("avg_rating").desc(nulls_last=True), "-log_count"]),
    "added": (gettext_lazy("Recently added"), ["-created_at", "-pk"]),
}
WHEN = {"": gettext_lazy("Any time"), "upcoming": gettext_lazy("Upcoming"), "past": gettext_lazy("Past")}
from . import runs
from .roles import can_delete_event, can_edit_event, home_place, is_admin
from .albums import (album_colors, album_groups, album_rows, choose_album, mark_album_choices, played_by_album,
                     remember_album,
                     split_plays, top_albums)
from .merge import merge_into_same_festival_day, share_photo, shared_photo_message
from .views_merge import (
    festival_admin_actions,
    festival_suggestion,
    artist_duplicate_candidates,
    suggestions_for,
)
from .templatetags.event_extras import stars
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
    if not request.user.is_authenticated:  # people sign in (or up) before seeing the feed
        return render(request, "events/landing.html", {"event_count": Event.objects.count()})
    today = timezone.localdate()
    events = Event.objects.with_stats().select_related("venue")
    logs = Log.objects.exclude(review="")
    followed = _followed_artists(request)
    followed_only = followed is not None
    if followed_only:  # only shows with an artist this person follows (and reviews of them)
        with_followed = Event.objects.filter(artists__in=followed).values("pk")
        events = events.filter(pk__in=with_followed)
        logs = logs.filter(event__in=with_followed)
    upcoming, upcoming_place, upcoming_city = _near(request, events.filter(date__gte=today).order_by("date"))
    recent, recent_place, recent_city = _near(request, events.filter(date__lt=today).order_by("-date"))
    return render(
        request,
        "events/home.html",
        {
            "upcoming": upcoming[:6],
            "upcoming_place": upcoming_place,
            "upcoming_query": _list_query("upcoming", upcoming_city, followed_only),
            "recent": recent[:6],
            "recent_place": recent_place,
            "recent_query": _list_query("past", recent_city, followed_only),
            "just_added": events.order_by("-created_at")[:6],
            "reviews": _reviews(logs)[:6],
            "lists": [] if followed_only else EventList.objects.filter(items__isnull=False).select_related("user")
            .annotate(item_count=Count("items")).order_by("-updated_at")[:6],
            "event_types": EventType.choices,
            "friends_activity": _friends_activity(request),
            "followed_only": followed_only,
            "followed_artists": followed if followed_only else [],
            **_area_switch(request),
        },
    )


@login_required
def first_show(request: HttpRequest) -> HttpResponse:
    """After signing up: find the last show you went to and add it to your diary, or add it if it isn't here
    (concerts from setlist.fm)."""
    if request.method == "POST":
        event = get_object_or_404(Event, pk=request.POST.get("event"))
        if not event.has_happened:
            messages.error(request, gettext("“%(title)s” hasn't happened yet.") % {"title": event.title})
            return redirect(f"{reverse('events:first_show')}?{urlencode({'q': request.POST.get('q', '')})}")
        Log.objects.get_or_create(user=request.user, event=event)
        messages.success(request, gettext("Added to your diary. Rate it or write a review here."))
        return redirect(event)
    q = (request.GET.get("q") or "").strip()
    results = []
    if q:
        today = timezone.localdate()
        results = list(
            Event.objects.select_related("venue")
            .filter(Q(date__lte=today) | Q(date__isnull=True))
            .filter(Q(title__icontains=q) | Q(artists__name__icontains=q) | Q(venue__name__icontains=q))
            .distinct().order_by(F("date").desc(nulls_last=True))[:15]
        )
    logged = set(Log.objects.filter(user=request.user, event__in=results).values_list("event_id", flat=True))
    music = {EventType.MUSIC, EventType.FESTIVAL}
    return render(request, "events/first_show.html", {
        "q": q, "results": results, "logged": logged,
        "other_types": [(value, label) for value, label in EventType.choices if value not in music],
    })


def _area_switch(request: HttpRequest) -> dict:
    """The area buttons the person can use (Region and City need them in the profile) and the one picked."""
    profile = getattr(request.user, "profile", None) if request.user.is_authenticated else None
    if not profile:
        return {}
    has = {"all": True, "country": bool(profile.home_country), "region": bool(profile.home_region.strip()),
           "city": bool(profile.home_city.strip())}
    return {"areas": [(value, label) for value, label in Profile.HomeArea.choices if has[value]],
            "home_area": profile.home_area}


def _followed_artists(request: HttpRequest) -> list | None:
    """The artists the signed-in person follows, when their home page shows only those (None otherwise)."""
    profile = getattr(request.user, "profile", None) if request.user.is_authenticated else None
    if not profile or not profile.home_followed_only:
        return None
    return list(Artist.objects.filter(follows__user=request.user).order_by("name"))


@login_required
@require_POST
def home_area(request: HttpRequest) -> HttpResponse:
    """The home page's area switch for upcoming and recent shows: all, country, region or city."""
    from .views_social import get_profile

    profile = get_profile(request.user)
    area = request.POST.get("area") or ""
    profile.home_area = area if area in Profile.HomeArea.values else ""
    profile.save(update_fields=["home_area"])
    return redirect("events:home")


@login_required
@require_POST
def home_followed_only(request: HttpRequest) -> HttpResponse:
    """The home page's switch between everything and only the artists the person follows."""
    from .views_social import get_profile

    profile = get_profile(request.user)
    profile.home_followed_only = request.POST.get("followed_only") == "1"
    profile.save(update_fields=["home_followed_only"])
    return redirect("events:home")


@login_required
@require_POST
def artist_follow_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    artist = get_object_or_404(Artist, pk=pk)
    deleted, _ = ArtistFollow.objects.filter(user=request.user, artist=artist).delete()
    if not deleted:
        ArtistFollow.objects.create(user=request.user, artist=artist)
    return redirect(_safe_next(request, artist.get_absolute_url()))


def _near(request: HttpRequest, shows) -> tuple:
    """Shows near the person, as their profile asks: shows by local artists (from their country) in their city,
    region or country, and shows by international artists in their city, region or country. Everyone else sees
    all of them. Returns the shows, the place's name ("" for everywhere) and the city to filter the events list by
    ("" when the list can't show the same area)."""
    profile = getattr(request.user, "profile", None) if request.user.is_authenticated else None
    country = profile.home_country.code if profile and profile.home_country else ""
    city = profile.home_city.strip() if profile else ""
    chosen = profile.home_area if profile else ""
    if chosen == "all":
        return shows, "", ""
    if chosen == "city" and city:
        where = Q(venue__city__iexact=city) & (Q(venue__country=country) if country else Q())
        return shows.filter(where), city, city
    if chosen == "region" and country and profile.home_region.strip():
        region = profile.home_region.strip()
        return shows.filter(venue__region__iexact=region, venue__country=country), region, ""
    if chosen == "country" and country:
        return shows.filter(venue__country=country), dict(countries).get(country, country), ""
    if not country:
        if city:
            return shows.filter(venue__city__iexact=city), city, city
        return shows, "", ""

    region = profile.home_region.strip()

    def area(scope: str) -> tuple[Q, str]:
        """Where a scope looks (narrower ones fall back to the country when the profile lacks the city or region)."""
        if scope == "city" and city:
            return Q(venue__city__iexact=city, venue__country=country), city
        if scope == "region" and region:
            return Q(venue__region__iexact=region, venue__country=country), region
        return Q(venue__country=country), dict(countries).get(country, country)

    # A show is international when its headliner (first act on stage) is from another country.
    headliner_country = Subquery(
        EventArtist.objects.filter(event=OuterRef("pk")).exclude(role__in=CREDIT_ROLES)
        .order_by("billing_order", "pk").values("artist__country")[:1])
    shows = shows.annotate(headliner_country=headliner_country)
    local_area, local_place = area(profile.upcoming_local)
    intl_area, intl_place = area(profile.upcoming_international)
    is_international = ~Q(headliner_country=country) & ~Q(headliner_country="") & Q(headliner_country__isnull=False)
    shows = shows.filter((~is_international & local_area) | (is_international & intl_area))
    place = local_place if local_place == intl_place else f"{local_place} · {intl_place}"
    return shows, place, city if local_place == intl_place == city else ""


def _list_query(when: str, city: str, followed: bool) -> str:
    """The events list's query string for a home page section's "All …" link."""
    return urlencode({"when": when, **({"city": city} if city else {}), **({"followed": "1"} if followed else {})})


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
    country = (request.GET.get("country") or "").strip().upper()[:2]  # from the site stats' world map
    year = request.GET.get("year") or ""
    when = request.GET.get("when") or ""
    followed = "1" if request.GET.get("followed") == "1" and request.user.is_authenticated else ""
    # Upcoming shows read best soonest-first; everything else newest-first.
    default_sort = "oldest" if when == "upcoming" else "newest"
    sort = request.GET.get("sort") or default_sort
    if sort not in SORTS:
        sort = default_sort

    events = Event.objects.select_related("venue", "production")
    if event_type in EventType.values:
        events = events.filter(event_type=event_type)
    if city:  # a city, or a district in one (Ñuñoa)
        events = events.filter(Q(venue__city__iexact=city) | Q(venue__district__iexact=city))
    if country:
        events = events.filter(venue__country=country)
    if year.isdigit():
        events = events.filter(date__year=int(year))
    today = timezone.localdate()
    if when == "upcoming":
        events = events.filter(date__gte=today)
    elif when == "past":
        events = events.filter(date__lt=today)
    if followed:
        events = events.filter(pk__in=Event.objects.filter(artists__follows__user=request.user).values("pk"))
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

    filters = {"q": q, "type": event_type, "city": city, "country": country, "year": year, "when": when, "sort": sort,
               "followed": followed}
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
            "can_add_dates": runs.can_add_dates(event) and _can_edit(request.user, event),
            "other_dates": runs.other_dates(event),
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


PRODUCTION_DATES_PER_PAGE = 30
PRODUCTION_REVIEWS_PER_PAGE = 20


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
    # A long tour or festival can have hundreds of dates and reviews: a page of each at a time.
    dates = Paginator(
        events.with_stats().select_related("venue")
        .prefetch_related(Prefetch("eventartist_set", queryset=EventArtist.objects.select_related("artist")
                                   .filter(role=EventArtist.Role.HEADLINER)))
        .order_by(F("date").asc(nulls_last=True), "pk"),
        PRODUCTION_DATES_PER_PAGE,
    ).get_page(request.GET.get("page"))
    # Dates per year across every page, for the year headings.
    year_counts = dict(events.order_by().annotate(y=ExtractYear("date")).values("y").annotate(n=Count("id", distinct=True))
                       .values_list("y", "n"))
    return render(
        request,
        "events/production_detail.html",
        {
            "production": production,
            "events": dates.object_list,
            "dates_page": dates,
            "year_counts": year_counts,
            "reviews": Paginator(_reviews(logs), PRODUCTION_REVIEWS_PER_PAGE).get_page(request.GET.get("rpage")),
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
    "events": gettext_lazy("Most events"),
    "popular": gettext_lazy("Most popular"),
    "rated": gettext_lazy("Highest rated"),
    "recent": gettext_lazy("Last performed"),
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


def _songs_to_show(albums: list, band_charts: list) -> int:
    """How many "most played songs" to list so the column is about as tall as the album charts next to it."""
    rows = len(albums) + sum(len(chart["rows"]) + 2 for chart in band_charts)  # +2: each band chart's heading
    return max(10, min(50, round(rows * 1.5) + 3))  # +3: the controls above the chart


def _artist_plays(artist: Artist, events, *, skip_empty: bool = False, sort: str = "count", by: str = "n"):
    """How often each song and album was played by this act across these events' setlists (no tapes).

    Each song's ``of`` counts only the shows since it came out (shows in or after its album's year,
    plus undated shows) whose setlist has songs, so a new song isn't measured against shows from before it existed.
    Returns (songs, own album rows, [{"band", "rows"}] for bands the artist was in, number of shows).
    Covers filed under the artist's own albums count there; covers of those bands go to the band's chart;
    other covers to a "No album covers" row.
    """
    plays = SetlistSong.objects.filter(setlist__artist=artist, setlist__event__in=events, is_tape=False)
    # Only setlists with at least one song played live count as chances to play a song (an empty setlist
    # says nothing about what was played).
    show_years = list(
        Setlist.objects.filter(artist=artist, event__in=events)
        .filter(Exists(SetlistSong.objects.filter(setlist=OuterRef("pk"), is_tape=False)))
        .values_list("event__date__year", flat=True)
    )
    songs = list(plays.values("song_id", "song__title", "song__album_title", "song__album_year", "song__cover_of_name")
                 .annotate(n=Count("setlist", distinct=True)).order_by("-n", "song__title"))
    for song in songs:
        released = song["song__album_year"]
        song["of"] = sum(1 for y in show_years if y is None or not released or y >= released)
    # Most plays first; on a tie, the song that had fewer chances ranks higher (5/5 above 5/6, 5/6 above 4/4).
    songs.sort(key=lambda s: (-s["n"], s["of"], s["song__title"].casefold()))
    plain = [{"title": s["song__title"], "album_title": s["song__album_title"], "album_year": s["song__album_year"],
              "cover_of": s["song__cover_of_name"], "n": s["n"], "of": s["of"]} for s in songs]
    own, by_band, covers = split_plays(artist, plain)
    played, no_album = played_by_album(own)
    options = {"skip_empty": skip_empty, "sort": sort, "by": by}
    albums = album_rows(artist, played, no_album=no_album, covers=covers, **options)
    band_charts = []
    for band, band_songs in sorted(by_band.items(), key=lambda kv: kv[0].name.casefold()):
        band_played, band_none = played_by_album(band_songs)
        rows = album_rows(band, band_played, no_album=band_none, **options)
        top_albums(rows, by)  # the band's most played album(s) get the green bar too
        band_charts.append({"band": band, "rows": rows})
    return songs, albums, band_charts, len(show_years)


@login_required
def artist_songs_manage(request: HttpRequest, pk: int) -> HttpResponse:
    """Admins only: every song of an artist grouped by album (newest first, no album last) to fix in bulk."""
    if not is_admin(request.user):
        raise PermissionDenied
    artist = get_object_or_404(Artist, pk=pk)
    songs = list(artist.songs.annotate(plays=Count("setlist_entries", filter=Q(setlist_entries__is_tape=False))))
    album_choices = album_groups(artist, songs)
    allowed = mark_album_choices(songs, album_choices)
    if request.method == "POST":
        changed = 0
        for song in songs:
            album_changed = choose_album(song, request.POST.get(f"album_{song.pk}"), allowed) \
                if f"album_{song.pk}" in request.POST else False
            cover = " ".join((request.POST.get(f"cover_of_{song.pk}", song.cover_of_name) or "").split())[:200]
            if not album_changed and cover == song.cover_of_name:
                continue
            song.cover_of_name = cover
            song.album_checked_at = timezone.now()  # fixed by hand: never looked up again
            song.save(update_fields=["album_title", "album_year", "album_mbid", "album_checked_at", "cover_of_name"])
            changed += 1
        messages.success(request, ngettext("Updated the album of %(n)s song.", "Updated the album of %(n)s songs.",
                                           changed) % {"n": changed})
        return redirect("events:artist_songs_manage", pk=artist.pk)
    groups: dict[str, dict] = {}
    for song in songs:
        key = normalize_name(song.album_title)
        group = groups.setdefault(key, {"title": song.album_title or gettext("No album"), "year": song.album_year,
                                        "is_none": not song.album_title, "songs": []})
        if group["year"] is None:
            group["year"] = song.album_year
        group["songs"].append(song)
    ordered = sorted(groups.values(), key=lambda g: (g["is_none"], -(g["year"] or 0), g["title"].casefold()))
    for group in ordered:
        group["songs"].sort(key=lambda s: s.title.casefold())
    return render(request, "events/artist_songs_manage.html", {
        "artist": artist, "groups": ordered, "song_total": len(songs), "album_choices": album_choices,
    })


@login_required
@require_POST
def artist_albums_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Admins only: add an album to an artist (so unplayed albums show), change its year, or remove one."""
    if not is_admin(request.user):
        raise PermissionDenied
    artist = get_object_or_404(Artist, pk=pk)
    raw_year = (request.POST.get("year") or "").strip()
    year = int(raw_year) if raw_year.isdigit() and 1000 < int(raw_year) < 3000 else None
    kind = request.POST.get("kind") if request.POST.get("kind") in Album.Kind.values else None
    if request.POST.get("delete"):
        Album.objects.filter(artist=artist, pk=request.POST["delete"]).delete()
        messages.success(request, gettext("Album removed."))
    elif (request.POST.get("update") or "").isdigit():
        album = get_object_or_404(Album, artist=artist, pk=int(request.POST["update"]))
        if raw_year and year is None:
            messages.error(request, gettext("Type the year with four digits, e.g. 1994."))
        else:
            album.year = year
            album.kind = kind or album.kind
            album.save(update_fields=["year", "kind", "updated_at"])
            # Songs keep their album's year too (it decides which shows count as chances to play them).
            wanted = normalize_name(album.title)
            songs = [song.pk for song in Song.objects.filter(Q(artist=artist) | Q(cover_of_name__iexact=artist.name))
                     .exclude(album_title="").only("pk", "album_title") if normalize_name(song.album_title) == wanted]
            Song.objects.filter(pk__in=songs).update(album_year=year)
            messages.success(request, gettext("“%(title)s” is now from %(year)s.") % {
                "title": album.title, "year": year} if year else gettext("“%(title)s” has no year now.") % {"title": album.title})
    else:
        title = " ".join((request.POST.get("title") or "").split())[:240]
        if not title:
            messages.error(request, gettext("Give the album a title."))
        elif any(normalize_name(a.title) == normalize_name(title) for a in artist.albums.all()):
            messages.info(request, gettext("“%(title)s” is already in %(artist)s's albums.") % {
                "title": title, "artist": artist.name})
        else:
            Album.objects.create(artist=artist, title=title, year=year, kind=kind or Album.Kind.LP)
            messages.success(request, gettext("Added “%(title)s”.") % {"title": title})
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
    album_by = "songs" if request.GET.get("by") == "songs" else "n"
    songs, albums, band_charts, shows = _artist_plays(artist, events.distinct(), skip_empty=skip_empty, sort=sort,
                                                      by=album_by)
    tab = "albums" if request.GET.get("tab") == "albums" else "songs"
    return render(request, "events/artist_songs.html", {
        "artist": artist, "seen_by": seen_by, "tab": tab, "artist_tab": "stats", "shows": shows,
        "songs": songs, "albums": albums, "band_charts": band_charts, "top_albums": top_albums(albums, album_by),
        "skip_empty": skip_empty, "album_sort": sort, "album_by": album_by,
        "album_keep": [("tab", "albums")] + ([("seen_by", seen_by.get_username())] if seen_by else []),
        "can_fix_albums": is_admin(request.user),
        "album_kinds": Album.Kind.choices,
        "seen_q": f"seen_by={quote(seen_by.get_username())}" if seen_by else "",
    })


def _my_shows_toggle(request: HttpRequest, artist: Artist) -> dict:
    """The "All shows | Shows I saw" links for a signed-in user (other query params, like album sorting, kept)."""
    if not request.user.is_authenticated:
        return {}
    params = request.GET.copy()
    params.pop("seen_by", None)
    all_url = request.path + (f"?{params.urlencode()}" if params else "")
    params["seen_by"] = request.user.get_username()
    return {
        "my_show_count": artist.events.filter(logs__user=request.user, logs__acts__artist=artist).distinct().count(),
        "all_shows_url": all_url,
        "my_shows_url": f"{request.path}?{params.urlencode()}",
    }


ARTIST_PREVIEW_SHOWS = 12  # on the artist's page; the Shows tab has them all, a page at a time
ARTIST_SHOWS_PER_PAGE = 24


def _artist_show_cards(artist: Artist, events):
    """Event cards for an artist's shows, latest first; cards open this artist's setlist there when there is one."""
    return events.with_stats().select_related("venue").distinct().order_by(*LATEST_FIRST).annotate(
        artist_setlist=Exists(Setlist.objects.filter(event=OuterRef("pk"), artist=artist))
    )


def artist_shows(request: HttpRequest, pk: int) -> HttpResponse:
    """The Shows tab: every show of the artist (or the ones ?seen_by= saw), a page at a time."""
    artist = get_object_or_404(Artist, pk=pk)
    events = artist.events.all()
    seen_by = _seen_by(request)
    if seen_by:
        events = events.filter(logs__user=seen_by, logs__acts__artist=artist)
    page = Paginator(_artist_show_cards(artist, events), ARTIST_SHOWS_PER_PAGE).get_page(request.GET.get("page"))
    return render(request, "events/artist_shows.html", {
        "artist": artist, "artist_tab": "shows", "seen_by": seen_by, "page": page,
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
    album_by = "songs" if request.GET.get("by") == "songs" else "n"
    songs, albums, band_charts, setlist_shows = _artist_plays(artist, events.distinct(), skip_empty=skip_empty,
                                                              sort=album_sort, by=album_by)
    return render(
        request,
        "events/artist_detail.html",
        {
            "artist": artist,
            **_my_shows_toggle(request, artist),
            "follower_count": artist.follows.count(),
            "is_following_artist": request.user.is_authenticated
            and artist.follows.filter(user=request.user).exists(),
            "tours": artist.tours.order_by("title"),
            **(dict(zip(("merge_similar", "merge_rest"), artist_duplicate_candidates(artist)))
               if is_admin(request.user) else {}),
            "act_logs": act_logs,
            # The latest shows only: an artist can have hundreds (the Shows tab pages through them all).
            "artist_tab": "overview",
            "seen_q": f"seen_by={quote(seen_by.get_username())}" if seen_by else "",
            "events": _artist_show_cards(artist, events)[:ARTIST_PREVIEW_SHOWS],
            "event_count": events.distinct().count(),
            "seen_by": seen_by,
            # Played songs nobody has looked up on MusicBrainz yet (for "Find albums").
            "pending": list(artist.songs.filter(album_checked_at=None, setlist_entries__is_tape=False)
                            .values_list("pk", flat=True).distinct().order_by("pk")),
            # Enough songs to fill the space beside the album charts (an album bar is ~1.5 song rows tall).
            "top_songs": songs[:_songs_to_show(albums, band_charts)],
            "album_chart": albums,
            "band_charts": band_charts,
            "member_of": artist.member_of.all(),
            "members": artist.members.all(),
            "top_albums": top_albums(albums, album_by),  # ties all count; the green bars follow the toggle
            "skip_empty": skip_empty,
            "album_sort": album_sort,
            "album_by": album_by,
            "album_keep": [("seen_by", seen_by.get_username())] if seen_by else [],
            "can_fix_albums": is_admin(request.user),
            "artist_albums": artist.albums.all() if is_admin(request.user) else [],
            "album_kinds": Album.Kind.choices,
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


def _iso_date(raw):
    """A YYYY-MM-DD date from the query string, or None."""
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def diary(request: HttpRequest, username: str | None = None) -> HttpResponse:
    if username is None:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return redirect("events:user_diary", username=request.user.get_username())

    owner = get_object_or_404(get_user_model(), username=username)
    all_logs = Log.objects.filter(user=owner)
    # One type from the diary's own filter, or several from the stats page (?type=music&type=festival).
    types = [t for t in dict.fromkeys(request.GET.getlist("type")) if t in EventType.values]
    event_type = types[0] if len(types) == 1 else ""
    year = request.GET.get("year") or ""
    rating = request.GET.get("rating") or ""
    if not (rating.isdigit() and 1 <= int(rating) <= 10):
        rating = ""  # ratings are stored 1–10 (half stars)
    country = (request.GET.get("country") or "").strip().upper()[:2]  # from the stats page's world map
    city = (request.GET.get("city") or "").strip()[:120]  # from the stats page's "Most visited cities"
    # A date range (the stats page's month and week bars): ?from=2026-03-01&to=2026-03-31.
    since, until = _iso_date(request.GET.get("from")), _iso_date(request.GET.get("to"))
    logs = all_logs.select_related("event", "event__venue").order_by("-event__date", "-created_at")
    if types:
        logs = logs.filter(event__event_type__in=types)
    if year.isdigit():
        logs = logs.filter(event__date__year=int(year))
    if rating:
        logs = logs.filter(rating=int(rating))
    if country:
        logs = logs.filter(event__venue__country=country)
    if city:
        logs = logs.filter(Q(event__venue__city__iexact=city) | Q(event__venue__district__iexact=city))
    if since:
        logs = logs.filter(event__date__gte=since)
    if until:
        logs = logs.filter(event__date__lte=until)
    years = (all_logs.exclude(event__date=None).annotate(y=ExtractYear("event__date"))
             .values_list("y", flat=True).distinct().order_by("-y"))
    types_logged = all_logs.values_list("event__event_type", flat=True).distinct()
    expanded = request.GET.get("view") == "expanded"
    if expanded:  # posters and lineups: fetch every event's bill (and which acts they saw) in one query each
        logs = logs.prefetch_related(Prefetch(
            "event__eventartist_set",
            queryset=EventArtist.objects.select_related("artist").order_by("billing_order", "artist__name"),
            to_attr="bill",
        ), "acts")
    stats = logs.aggregate(avg_rating=Avg("rating"), log_count=Count("id"))
    by_type = logs.values("event__event_type").annotate(n=Count("id")).order_by("-n")
    type_labels = dict(EventType.choices)
    # Expanded rows are heavier (posters, lineups), so fewer per page.
    page = Paginator(logs, 25 if expanded else 100).get_page(request.GET.get("page"))
    if expanded:
        page.object_list = list(page.object_list)
        for log in page.object_list:
            # Only the acts this person saw (credits like a play's author always show).
            seen = {act.artist_id for act in log.acts.all()}
            log.seen_bill = [link for link in log.event.bill if link.role in CREDIT_ROLES or link.artist_id in seen]
    filters = {k: v for k, v in {"type": types, "year": year, "rating": rating, "country": country,
                                 "city": city, "from": since.isoformat() if since else "",
                                 "to": until.isoformat() if until else ""}.items() if v}
    countries_logged = sorted(
        ((code, countries.name(code)) for code in all_logs.exclude(event__venue__country="").exclude(event__venue=None)
         .values_list("event__venue__country", flat=True).distinct()), key=lambda c: c[1])
    return render(
        request,
        "events/diary.html",
        {
            "owner": owner,
            "tab": "diary",
            "logs": page,
            "page": page,
            "querystring": urlencode({**filters, **({"view": "expanded"} if expanded else {})}, doseq=True),
            "filters_qs": urlencode(filters, doseq=True),  # for the List / Expanded switch
            "f": {"type": event_type, "types": types, "year": year, "rating": rating, "country": country, "city": city,
                  "since": since, "until": until},
            "countries_logged": countries_logged,
            "ratings": [(r, stars(r)) for r in range(10, 0, -1)],
            "expanded": expanded,
            "years": years,
            "event_types": [(v, l) for v, l in EventType.choices if v in set(types_logged)],
            "by_type": [(type_labels.get(r["event__event_type"], EventType.OTHER.label), r["n"]) for r in by_type],
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
    if not event and request.GET.get("type") in EventType.values:
        initial["event_type"] = request.GET["type"]
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
    formset.event_type = request.POST.get("event_type", "") if request.method == "POST" else ""

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
            saved.order_bill()  # headliners (if any: an event doesn't need one) go above everyone else
            if event and request.POST.get("apply_to_run") == "all":
                fields = list(form.changed_data) + (["venue"] if form.cleaned_data.get("new_venue_name") else [])
                dates = runs.apply_to_other_dates(saved, fields, formset.has_changed(), request.user)
                if dates:
                    messages.info(request, ngettext("The change was also made to %(n)s other date.",
                                                    "The changes were also made to %(n)s other dates.", dates) % {"n": dates})
            merged = merge_into_same_festival_day(saved)
            # Whenever the box is ticked (it starts unticked when another show has a different photo of its own).
            if not merged and form.cleaned_data.get("photo_for_all_days") and saved.image_url:
                updated, unit = share_photo(request.user, saved)
                if updated:
                    messages.info(request, shared_photo_message(updated, unit))
        if merged:
            messages.info(request, gettext("“%(title)s” already existed on that day, so this was added to it.")
                          % {"title": merged.title})
            return redirect(merged)
        messages.success(request, gettext("Event saved."))
        return redirect(saved)

    return render(request, "events/event_form.html", {
        "form": form, "formset": formset, "event": event,
        "other_dates_count": runs.other_dates(event).count() if event else 0,
        "type_rules": {"roles": ROLES_BY_TYPE, "all": [v for v, _ in EventArtist.Role.choices], "play_types": PLAY_TYPES},
        # Sent once for every lineup row's artist/tour picker (rows only render their current choice).
        "artist_options": list(Artist.objects.order_by("name").values_list("pk", "name")),
        "tour_options": [
            (pk, f"{title} ({artist})" if artist else title)
            for pk, title, artist in Production.objects.exclude(event_type=EventType.FESTIVAL)
            .order_by("title").values_list("pk", "title", "artist__name")
        ],
    })


TYPED_NAMES_SPLIT = re.compile(r"[\n\r•·*;]+")  # one per line, or poster-style separators (not commas)


def add_typed_artists(event: Event, text: str, role: str) -> tuple[int, int]:
    """Add the artists typed in ``text`` to the event's bill with ``role``: known artists (same name, any case)
    are linked, other names become new artists, acts already on the bill are skipped. Returns (added, skipped)."""
    names = [n.strip()[:200] for n in TYPED_NAMES_SPLIT.split(text or "") if n.strip()]
    on_bill = {normalize_name(n) for n in event.eventartist_set.values_list("artist__name", flat=True)}
    last = event.eventartist_set.aggregate(m=models.Max("billing_order"))["m"] or 0
    added = skipped = 0
    with transaction.atomic():
        for name in names:
            key = normalize_name(name)
            if key in on_bill:
                skipped += 1
                continue
            on_bill.add(key)
            artist = Artist.objects.filter(name__iexact=name).order_by("pk").first() or Artist.objects.create(name=name)
            added += 1
            EventArtist.objects.create(event=event, artist=artist, role=role, billing_order=last + added)
        event.order_bill()  # headliners (if any) go above everyone else
    event.refresh_auto_title()
    return added, skipped


@login_required
def festival_add_artists(request: HttpRequest, pk: int) -> HttpResponse:
    """Festival days: add the acts setlist.fm has on this date, searched by city (default), venue or tour name."""
    from .setlistfm import _match_artist

    event = get_object_or_404(Event.objects.select_related("venue", "production"), pk=pk)
    if event.event_type != EventType.FESTIVAL or not _can_edit(request.user, event):
        raise PermissionDenied
    if request.method == "POST" and "typed" in request.POST:  # "Type the artists instead": no setlist.fm search
        role = (EventArtist.Role.HEADLINER if request.POST.get("typed_role") == "headliner"
                else added_act_role(event.event_type))
        added, skipped = add_typed_artists(event, request.POST["typed"], role)
        if added:
            text = ngettext("Added %(n)s artist to the lineup.", "Added %(n)s artists to the lineup.", added) % {"n": added}
            if skipped:
                text += " " + ngettext("%(n)s was already on it.", "%(n)s were already on it.", skipped) % {"n": skipped}
            messages.success(request, text)
        else:
            messages.info(request, gettext("Nothing added: type at least one artist that isn't on the lineup yet."))
            return redirect(request.get_full_path())
        return redirect(event)
    on_bill = {normalize_name(n) for n in event.eventartist_set.values_list("artist__name", flat=True)}
    candidates, error, capped = [], "", False
    # How to search setlist.fm (it has no festival filter): the city, a venue name or a tour name, plus the date.
    defaults = {
        "city": event.venue.city if event.venue_id else "",
        "venue": event.venue.name if event.venue_id else "",
        "tour": event.production.title if event.production_id else event.title,
    }
    mode = request.GET.get("mode") if request.GET.get("mode") in defaults else ("city" if defaults["city"] else "venue")
    term = (request.GET.get("q") or defaults[mode]).strip()[:200]
    if event.date and term:
        where = {mode: term}
        seen = set()
        try:
            # 20 per page; big festivals need a few pages. Beta: one setlist.fm call per search for non-admins.
            for page_no in ((1, 2, 3) if is_admin(request.user) else (1,)):
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
                                       "same_venue": bool(event.venue_id) and
                                       normalize_name(venue_name) == normalize_name(event.venue.name)})
                if len(results) < 20:
                    break
            else:  # every page asked for was full: setlist.fm may have more
                capped = not is_admin(request.user)
        except SetlistFmError as e:
            error = str(e)
        candidates.sort(key=lambda c: (not c["same_venue"], c["venue"].casefold(), c["name"].casefold()))
    if request.method == "POST" and not error:
        chosen = [c for c in candidates if request.POST.get(f"add_{c['key']}")]
        last = event.eventartist_set.aggregate(m=models.Max("billing_order"))["m"] or 0
        setlists, skipped = 0, []
        with transaction.atomic():
            for i, c in enumerate(chosen, start=1):
                artist = _match_artist(c["sfm_artist"])
                role = (EventArtist.Role.HEADLINER if request.POST.get(f"role_{c['key']}") == "headliner"
                        else added_act_role(event.event_type))
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
        messages.success(request, gettext("Added %(artists)s to the lineup and %(setlists)s.") % {
            "artists": ngettext("%(n)s artist", "%(n)s artists", len(chosen)) % {"n": len(chosen)},
            "setlists": ngettext("%(n)s setlist", "%(n)s setlists", setlists) % {"n": setlists},
        })
        for note in skipped:
            messages.warning(request, gettext("Setlist not added for %(note)s") % {"note": note})
        return redirect(event)
    # The venues setlist.fm found that day (the festival's own first): ticking one ticks all its artists.
    venues: dict[str, dict] = {}
    for c in candidates:
        c["venue_key"] = normalize_name(c["venue"]) or "unknown"
        v = venues.setdefault(c["venue_key"], {"key": c["venue_key"], "name": c["venue"] or gettext("Unknown venue"),
                                               "count": 0, "same_venue": c["same_venue"]})
        v["count"] += 1
    return render(request, "events/festival_add_artists.html", {
        "event": event, "candidates": candidates, "error": error, "mode": mode, "term": term, "defaults": defaults,
        "capped": capped,
        "venues": sorted(venues.values(), key=lambda v: (not v["same_venue"], -v["count"], v["name"].casefold())),
    })


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
    own, by_band, covers = split_plays(setlist.artist, [
        {"title": e.song.title, "album_title": e.song.album_title, "album_year": e.song.album_year,
         "cover_of": e.song.cover_of_name, "n": 1} for e in played_entries
    ])
    played, no_album = played_by_album(own)
    pending = sorted({e.song_id for e in played_entries if e.song.album_checked_at is None})
    album_sort = "count" if request.GET.get("sort") == "count" else "newest"
    skip_empty = request.GET.get("empty") == "skip"
    # Only albums already out at this show (an album released later can't have been "skipped").
    released_by = event.date.year if event.date else None
    timeline = album_rows(setlist.artist, played, skip_empty=skip_empty, sort=album_sort,
                          released_by=released_by, no_album=no_album, covers=covers)
    band_charts = []
    for band, band_songs in sorted(by_band.items(), key=lambda kv: kv[0].name.casefold()):
        band_played, band_none = played_by_album(band_songs)
        rows = album_rows(band, band_played, no_album=band_none, skip_empty=skip_empty, sort=album_sort,
                          released_by=released_by)
        top_albums(rows)  # the band's most played album(s) get the green bar too
        band_charts.append({"band": band, "rows": rows})
    songs = list({e.song_id: e.song for e in played_entries}.values())
    album_choices = []
    if is_admin(request.user):  # the "Edit albums" dropdowns
        album_choices = album_groups(setlist.artist, songs)
        mark_album_choices(songs, album_choices)
    # "Colour by album": songs and their chart rows share a colour. "Only LPs" (the default) uses the second set.
    colors, lp_colors = album_colors(setlist.artist), album_colors(setlist.artist, lps_only=True)
    for row in timeline:
        if not row.get("is_none"):
            row["color"] = colors.get(normalize_name(row["title"]))
            row["lp_color"] = lp_colors.get(normalize_name(row["title"]))
    number = 0
    for entry in entries:  # songs are numbered 1, 2, 3… skipping tapes
        if not entry.is_tape:
            number += 1
        entry.number = None if entry.is_tape else number
        album = entry.song.album_title
        entry.album_color = colors.get(normalize_name(album)) if album and not entry.is_tape else None
        entry.lp_color = lp_colors.get(normalize_name(album)) if album and not entry.is_tape else None
    return render(request, "events/act_setlist.html", {
        "event": event, "setlist": setlist, "entries": entries, "song_count": len(played_entries),
        "top_albums": top_albums(timeline), "pending": pending, "timeline": timeline, "band_charts": band_charts,
        "album_sort": album_sort, "skip_empty": skip_empty, "can_fix_albums": is_admin(request.user),
        "songs": songs, "album_choices": album_choices,
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
            # A cover is found on the original artist's albums (a Beatles song on a Beatles album).
            if song.cover_of_name:
                found = song_album(song.title, song.cover_of_name, "")
            else:
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
    """Admins only: correct the albums of a setlist's songs in one go, picked from the artist's albums."""
    if not is_admin(request.user):
        raise PermissionDenied
    setlist = get_object_or_404(Setlist.objects.select_related("artist"), event_id=pk, artist_id=artist_pk)
    songs = list(Song.objects.filter(setlist_entries__setlist=setlist).distinct())
    allowed = mark_album_choices(songs, album_groups(setlist.artist, songs))
    changed = 0
    for song in songs:
        if f"album_{song.pk}" not in request.POST:
            continue
        if choose_album(song, request.POST.get(f"album_{song.pk}"), allowed):
            song.save(update_fields=["album_title", "album_year", "album_mbid", "album_checked_at"])
            changed += 1
    messages.success(request, ngettext("Updated the album of %(n)s song.", "Updated the album of %(n)s songs.",
                                       changed) % {"n": changed})
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
    return _simple_create(request, PlayForm, "play")


@login_required
def play_edit(request: HttpRequest, pk: int) -> HttpResponse:
    return _simple_edit(request, Play, pk, PlayForm, "play")


@login_required
def event_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Admins only: remove an event and everything attached to it."""
    event = get_object_or_404(Event, pk=pk)
    if not can_delete_event(request.user, event):
        raise PermissionDenied
    if request.method == "POST":
        title = event.title
        event.delete()
        messages.success(request, gettext("Deleted “%(name)s”.") % {"name": title})
        return redirect("events:event_list")
    impact = {
        "logs": event.logs.count(),
        "setlists": event.setlists.count(),
        "lists": event.list_items.count(),
        "want_to_go": event.wanted_by.count(),
        "favorites": event.favorited_by.count(),
    }
    return render(request, "events/event_confirm_delete.html", {"event": event, "impact": impact})


# Page titles and messages for the catalog forms, per kind (whole sentences, so they translate well).
CATALOG_TEXTS = {
    "artist": {"new": gettext_lazy("New artist"), "edit": gettext_lazy("Edit artist"),
               "created": gettext_lazy("Artist “%(name)s” created."), "saved": gettext_lazy("Artist “%(name)s” saved.")},
    "venue": {"new": gettext_lazy("New venue"), "edit": gettext_lazy("Edit venue"),
              "created": gettext_lazy("Venue “%(name)s” created."), "saved": gettext_lazy("Venue “%(name)s” saved.")},
    "production": {"new": gettext_lazy("New production"), "edit": gettext_lazy("Edit production"),
                   "created": gettext_lazy("Production “%(name)s” created."),
                   "saved": gettext_lazy("Production “%(name)s” saved.")},
    "play": {"new": gettext_lazy("New play"), "edit": gettext_lazy("Edit play"),
             "created": gettext_lazy("Play “%(name)s” created."), "saved": gettext_lazy("Play “%(name)s” saved.")},
}


def _simple_create(request: HttpRequest, form_class, kind: str,
                   template: str = "events/simple_form.html") -> HttpResponse:
    texts = CATALOG_TEXTS[kind]
    form = form_class(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        messages.success(request, str(texts["created"]) % {"name": obj})
        return redirect(_safe_next(request, obj.get_absolute_url()))
    return render(
        request, template, {"form": form, "title": texts["new"], "next": _safe_next(request, "")}
    )


def _simple_edit(request: HttpRequest, model, pk: int, form_class, kind: str,
                 template: str = "events/simple_form.html") -> HttpResponse:
    """Admins only: edit catalog data shared by every event (artists, venues, productions)."""
    if not is_admin(request.user):
        raise PermissionDenied
    texts = CATALOG_TEXTS[kind]
    obj = get_object_or_404(model, pk=pk)
    form = form_class(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        messages.success(request, str(texts["saved"]) % {"name": obj})
        return redirect(obj.get_absolute_url())
    context = {"form": form, "title": texts["edit"], "next": obj.get_absolute_url()}
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
        messages.success(request, gettext("Deleted “%(name)s”.") % {"name": name})
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
    return _simple_edit(request, Artist, pk, ArtistForm, "artist")


@login_required
def venue_edit(request: HttpRequest, pk: int) -> HttpResponse:
    return _simple_edit(request, Venue, pk, VenueForm, "venue", template="events/venue_form.html")


@login_required
def production_edit(request: HttpRequest, pk: int) -> HttpResponse:
    return _simple_edit(request, Production, pk, ProductionForm, "production")


@login_required
def artist_create(request: HttpRequest) -> HttpResponse:
    return _simple_create(request, ArtistForm, "artist")


@login_required
def venue_create(request: HttpRequest) -> HttpResponse:
    return _simple_create(request, VenueForm, "venue", template="events/venue_form.html")


@login_required
def production_create(request: HttpRequest) -> HttpResponse:
    return _simple_create(request, ProductionForm, "production")


# --------------------------------------------------------------------------- diary entries


@login_required
@require_POST
def log_save(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk)
    if not event.has_happened:
        messages.error(
            request, gettext("“%(title)s” hasn't happened yet. You can log it from %(date)s.") % {
                "title": event.title, "date": date_format(event.date, "j M Y")}
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
        messages.success(request, gettext("Saved to your diary."))
    else:
        messages.error(request, gettext("Could not save your log: %(errors)s") % {"errors": form.errors.as_text()})
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
        groups.append((gettext("Whole event"), None, whole))
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
        messages.success(request, gettext("Removed from your diary."))
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
        messages.error(request, gettext("Missing setlist id."))
        return redirect("events:setlist_search", pk=event.pk)
    try:
        setlist = import_setlist(event, get_setlist(setlist_id))
    except SetlistFmError as e:
        messages.error(request, str(e))
        return redirect("events:setlist_search", pk=event.pk)
    except Exception:
        logging.getLogger(__name__).exception("Importing setlist %s into event %s failed", setlist_id, event.pk)
        messages.error(request, gettext("Something went wrong importing that setlist. Nothing was saved."))
        return redirect("events:setlist_search", pk=event.pk)

    count = setlist.entries.count()
    messages.success(request, ngettext("Imported %(n)s song for %(artist)s.", "Imported %(n)s songs for %(artist)s.",
                                       count) % {"n": count, "artist": setlist.artist})
    return redirect(event)


@login_required
@require_POST
def event_add_date(request: HttpRequest, pk: int) -> HttpResponse:
    """Theater, comedy and dance: the same show on another date (a copy of this event, linked to it)."""
    event = get_object_or_404(Event, pk=pk)
    if not runs.can_add_dates(event) or not _can_edit(request.user, event):
        raise PermissionDenied
    try:
        on = date.fromisoformat((request.POST.get("date") or "").strip())
    except ValueError:
        messages.error(request, gettext("Pick a date."))
        return redirect(event)
    other, created = runs.add_date(event, on, request.user)
    if created:
        messages.success(request, gettext("Added %(date)s. Edit it if anything is different that day.") % {
            "date": date_format(on)})
    else:
        messages.info(request, gettext("The show already has that date."))
    return redirect(other)
