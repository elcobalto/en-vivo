"""Phase 3 — social: profiles, follows, review pages (likes + comments) and the activity feed."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, F, Max, Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext, gettext_lazy, ngettext
from django.views.decorators.http import require_POST

from .forms import CommentForm, FavoritesForm, ProfileForm
from .models import (
    Comment,
    EventList,
    EventType,
    FavoriteEvent,
    Follow,
    Log,
    Artist,
    Profile,
    Venue,
    ReviewLike,
    WantToGo,
)
from .roles import is_admin
from .views import _safe_next

User = get_user_model()


def get_profile(user) -> Profile:
    return Profile.objects.get_or_create(user=user)[0]


def _owner(username: str):
    return get_object_or_404(User.objects.select_related("profile"), username=username)


def _follow_state(request: HttpRequest, owner) -> dict[str, bool]:
    if not request.user.is_authenticated or request.user == owner:
        return {"is_following": False, "follows_you": False}
    return {
        "is_following": Follow.objects.filter(follower=request.user, following=owner).exists(),
        "follows_you": Follow.objects.filter(follower=owner, following=request.user).exists(),
    }


def _profile_header(request: HttpRequest, owner) -> dict[str, Any]:
    """Context shared by every profile tab (header with avatar, stats and follow button)."""
    logs = Log.objects.filter(user=owner)
    return {
        "owner": owner,
        "owner_profile": get_profile(owner),
        "owner_is_admin": is_admin(owner),
        "stats": {
            "events": logs.count(),
            "this_year": logs.filter(event__date__year=timezone.localdate().year).count(),
            "lists": EventList.objects.filter(user=owner).count(),
            "following": Follow.objects.filter(follower=owner).count(),
            "followers": Follow.objects.filter(following=owner).count(),
        },
        **_follow_state(request, owner),
    }


def with_social_counts(logs_qs):
    return logs_qs.annotate(
        like_count=Count("likes", distinct=True), comment_count=Count("comments", distinct=True)
    )


# --------------------------------------------------------------------------- profiles


def _stats_types(request: HttpRequest) -> list[str]:
    """The stats page's event types (?type=music&type=festival), or [] for every event."""
    return [t for t in dict.fromkeys(request.GET.getlist("type")) if t in EventType.values]


def _stats_filter(request: HttpRequest):
    """The stats pages' filter: the event types (?type=, several allowed) and the year (?year=)."""
    from .stats import Filter

    raw = request.GET.get("year") or ""
    year = int(raw) if raw.isdigit() and 1900 <= int(raw) <= 2100 else None
    return Filter(tuple(_stats_types(request)), year)


def _filter_qs(f) -> str:
    """"type=music&year=2024&" (or ""): a stats filter, to put in front of other query parameters."""
    return _types_qs(list(f.types)) + (f"year={f.year}&" if f.year else "")


def _types_qs(types: list[str]) -> str:
    """"type=music&type=festival&" (or ""), to put in front of other query parameters."""
    return "".join(f"type={t}&" for t in types)


def profile(request: HttpRequest, username: str) -> HttpResponse:
    owner = _owner(username)
    favorites = FavoriteEvent.objects.filter(user=owner).select_related("event", "event__venue", "first_act",
                                                                         "second_act")
    # Recently attended: the shows the person went to most recently (by show date, not log date).
    recent = (
        Log.objects.filter(user=owner).select_related("event", "event__venue")
        .order_by(F("event__date").desc(nulls_last=True), "-created_at")[:6]
    )
    reviews = with_social_counts(
        Log.objects.filter(user=owner).exclude(review="").select_related("user__profile", "event")
    ).order_by("-created_at")[:3]
    return render(
        request,
        "events/profile.html",
        {
            "owner": owner,
            "tab": "profile",
            "favorites": favorites,
            "recent": recent,
            "reviews": reviews,
            "owner_logs": Log.objects.filter(user=owner),
        },
    )


def _this_year(owner, f) -> dict:
    """The current year's shows by month and by week (7-day blocks from 1 January), for the stats page.

    {"year", "total", "periods": [("months", bars), ("weeks", bars)]}; each bar is {"label", "short", "count",
    "height" (% of the busiest), "future"}."""
    from datetime import date, timedelta

    from django.utils.formats import date_format

    today = timezone.localdate()
    year = f.year or today.year  # the year picked, or this one
    logs = Log.objects.filter(event__date__year=year, **({"user": owner} if owner is not None else {}))
    if f.types:
        logs = logs.filter(event__event_type__in=f.types)
    dates = list(logs.values_list("event__date", flat=True))
    months = [0] * 12
    weeks = [0] * 53  # day 365 (or 366) of the year starts a 53rd block
    for d in dates:
        months[d.month - 1] += 1
        weeks[(d - date(year, 1, 1)).days // 7] += 1

    # A bar with shows opens the person's diary for that month or week (the site's stats have no such page).
    diary = reverse("events:user_diary", args=[owner.get_username()]) if owner is not None else ""
    type_qs = _types_qs(list(f.types))

    def bars(counts, starts, label, short, end):
        peak = max(counts) or 1
        return [{"label": label(start, i), "short": short(start, i), "count": n, "height": round(n / peak * 100),
                 "future": start > today,
                 # A bar with shows opens the diary for that month or week.
                 "url": f"{diary}?{type_qs}from={start.isoformat()}&to={end(start).isoformat()}" if n and diary else ""}
                for i, (n, start) in enumerate(zip(counts, starts))]

    def month_end(start):
        return (date(year + 1, 1, 1) if start.month == 12 else date(year, start.month + 1, 1)) - timedelta(days=1)

    def week_end(start):
        return min(start + timedelta(days=6), date(year, 12, 31))

    month_starts = [date(year, m, 1) for m in range(1, 13)]
    week_starts = [date(year, 1, 1) + timedelta(days=7 * i) for i in range(53)]
    week_starts = [d for d in week_starts if d.year == year]
    weeks = weeks[:len(week_starts)]
    return {
        "year": year,
        "total": len(dates),
        "periods": [
            ("months", bars(months, month_starts, lambda d, i: date_format(d, "F Y"), lambda d, i: date_format(d, "M"),
                            month_end)),
            ("weeks", bars(weeks, week_starts,
                           lambda d, i: gettext("Week %(n)s (from %(date)s)") % {"n": i + 1, "date": date_format(d, "j M")},
                           lambda d, i: date_format(d, "j M"), week_end)),
        ],
    }


def _rank_rows(kind: str, items, owner, f, first: int = 1) -> list[dict]:
    """Rows for a ranking's cards or list: {"rank", "url", "name", "n", "unit", "picture", "sub", "avg"}.
    Venues and cities need their ``picture`` set first (``stats.venue_images`` / ``city_images``) to show one."""
    # A person's: their shows (seen_by, their diary); the site's: the events list.
    seen_by = f"?seen_by={quote(owner.get_username())}" if owner is not None else ""
    shows_in = (reverse("events:user_diary", args=[owner.get_username()]) + f"?{_filter_qs(f)}" if owner is not None
                else reverse("events:event_list") + (f"?year={f.year}&" if f.year else "?"))
    rows = []
    for rank, item in enumerate(items, start=first):
        if kind == "cities":
            name = item["event__venue__city"]
            rows.append({"rank": rank, "url": f"{shows_in}city={quote(name)}", "name": name,
                         "n": item["n"], "unit": "show", "picture": item.get("picture", "")})
        elif kind == "venues":
            rows.append({"rank": rank, "url": item.get_absolute_url() + seen_by, "name": item.name, "n": item.n,
                         "unit": "visit", "picture": getattr(item, "picture", ""), "sub": item.city})
        else:
            rows.append({"rank": rank, "url": item.get_absolute_url() + seen_by, "name": item.name, "n": item.n,
                         "unit": "show", "picture": item.image_url, "avg": getattr(item, "avg", None)})
    return rows


def _years(logs, qs: str) -> list[tuple[int, str]]:
    """[(year, its query string)] for the years these diary entries have shows in, newest first."""
    from django.db.models.functions import ExtractYear

    years = (logs.exclude(event__date=None).annotate(y=ExtractYear("event__date"))
             .values_list("y", flat=True).distinct().order_by("-y"))
    return [(y, f"{qs}year={y}") for y in years]


def _filtered_title(title: str, f) -> str:
    """"Most seen artists · Music, Festival · 2024"."""
    labels = dict(EventType.choices)
    bits = [title, *([", ".join(str(labels[t]) for t in f.types)] if f.types else []), *([str(f.year)] if f.year else [])]
    return " · ".join(bits)


# How many each "Highest rated" list shows (artists, types, countries) on the stats page.
RATED_TOP = 3


def stats_context(request: HttpRequest, owner) -> dict:
    """What a person (or with ``owner=None``, everyone) saw most, rated highest and heard — for every event or
    the types picked (?type=, several allowed)."""
    from . import stats

    f = _stats_filter(request)
    types, year = list(f.types), f.year
    logs = stats.logs_of(owner, f)
    types_logged = set(stats.logs_of(owner, stats.Filter()).values_list("event__event_type", flat=True).distinct())
    type_qs = _filter_qs(f)
    years_qs = _types_qs(types)  # the year bars and the year picker: every year, of these types
    home = (get_profile(owner).home_country or None) if owner is not None else None
    seen = stats.seen_artists(owner, f)
    venues = list(stats.seen_venues(owner, f)[:5])
    stats.venue_images(venues)
    cities = list(stats.seen_cities(owner, f)[:5])
    stats.city_images(owner, cities)
    if owner is not None:
        diary = reverse("events:user_diary", args=[owner.get_username()])
        years_url = diary + (f"?{years_qs.rstrip('&')}" if types else "")
        country_base = f"{diary}?{type_qs}"
        ranking_base = reverse("events:user_ranking", args=[owner.get_username(), "x"])[:-2]
    else:  # the events list takes one type
        events = reverse("events:event_list")
        years_url = events + (f"?type={types[0]}" if len(types) == 1 else "")
        country_base = events + "?" + (f"type={types[0]}&" if len(types) == 1 else "") + (f"year={year}&" if year else "")
        ranking_base = reverse("events:site_ranking", args=["x"])[:-2]
    context = {
        "owner": owner,
        "types": types,
        "year": year,
        # The filter: every event, or any of the types logged; each button adds or removes its type (the year stays).
        "event_types": [(value, label, value in types, _filter_qs(stats.Filter(
                            tuple(t for t in types if t != value) if value in types else (*types, value), year)))
                        for value, label in EventType.choices if value in types_logged],
        # …and every year, or one of the years with shows (the types stay).
        "years": _years(stats.logs_of(owner, stats.Filter(f.types)), years_qs),
        "all_years_qs": years_qs.rstrip("&"),
        "history_logs": stats.logs_of(owner, stats.Filter(f.types)),
        "type_qs": type_qs,
        "ranking_qs": f"?{type_qs.rstrip('&')}" if type_qs else "",
        "ranking_base": ranking_base,  # + kind + "/": a "See all" page
        "country_base": country_base,  # + "country=GB": shows in a country
        "type_base": country_base,  # + "type=music": shows of a type (only listed when no type is picked)
        "diary_url": years_url,  # the year bars' links (+ year=)
        "owner_logs": logs,
        "this_year": _this_year(owner, f),
        "world": stats.world(owner, f),
        "home_country": home,
        # Only acts actually seen count.
        "top_artists": [] if home else _rank_rows("artists", seen[:5], owner, f),
        "local_artists": _rank_rows("artists", seen.filter(country=home.code)[:5], owner, f) if home else [],
        "foreign_artists": _rank_rows("artists", seen.exclude(country=home.code).exclude(country="")[:5], owner,
                                      f) if home else [],
        "rated_artists": _rank_rows("rated-artists", stats.rated_artists(owner, f)[:RATED_TOP], owner, f),
        "min_ratings": stats.MIN_ARTIST_RATINGS,
        "top_venues": _rank_rows("venues", venues, owner, f),
        "top_cities": _rank_rows("cities", cities, owner, f),
    }
    if stats.has_setlists(owner, f):
        context.update(top_songs=stats.top_songs(owner, f), song_years=stats.song_years(owner, f))
    if not types:  # across every event: which kinds of shows and where the artists come from
        event_types, countries_ = stats.event_types(owner, f), stats.artist_countries(owner, f)
        context.update(
            top_types=event_types[:5], top_countries=countries_[:5],
            rated_types=stats.highest_rated(event_types, RATED_TOP),
            rated_countries=stats.highest_rated(countries_, RATED_TOP),
            min_events=stats.MIN_EVENTS_TO_RATE + 1,
        )
    return context


def user_stats(request: HttpRequest, username: str) -> HttpResponse:
    """A person's stats. Meant for a paid plan; free for everyone in the beta."""
    return render(request, "events/profile_stats.html", {**stats_context(request, _owner(username)), "tab": "stats"})


# "See all" pages: a list, or a grid of pictures (fewer per page: they're heavier, and 24 fills 2, 3, 4 or 6
# columns evenly).
RANKING_PAGE = {"list": 50, "grid": 24}


def ranking_context(request: HttpRequest, owner, kind: str) -> dict:
    """The whole list behind one of the stats page's top fives (a person's, or with ``owner=None`` the site's),
    as a list or a grid of pictures, a page at a time."""
    from django.core.paginator import Paginator

    from . import stats

    if kind not in stats.RANKING_TITLES:
        raise Http404
    f = _stats_filter(request)  # the stats page's filter carries over
    types = list(f.types)
    view = "grid" if request.GET.get("view") == "grid" else "list"
    home = get_profile(owner).home_country if owner is not None else None
    title = str(stats.RANKING_TITLES[kind])
    if kind in ("artists", "local-artists", "foreign-artists"):
        qs = stats.seen_artists(owner, f)
        if kind != "artists":
            if not home:
                raise Http404
            qs = qs.filter(country=home.code) if kind == "local-artists" else qs.exclude(country=home.code).exclude(country="")
            if kind == "local-artists":
                title = f"{title} · {home.name}"
    elif kind == "rated-artists":
        qs = stats.rated_artists(owner, f)
    elif kind == "venues":
        qs = stats.seen_venues(owner, f)
    else:
        qs = stats.seen_cities(owner, f)
    page = Paginator(qs, RANKING_PAGE[view]).get_page(request.GET.get("page"))
    items = list(page.object_list)
    offset = page.start_index() - 1 if items else 0
    if kind == "venues" and view == "grid":
        stats.venue_images(items)
    if kind == "cities" and view == "grid":
        stats.city_images(owner, items)
    types_qs = _filter_qs(f)
    stats_url = reverse("events:user_stats", args=[owner.get_username()]) if owner is not None \
        else reverse("events:site_stats")
    return {"owner": owner, "rows": _rank_rows(kind, items, owner, f, first=offset + 1), "page": page,
            "view": view, "kind": kind, "types_qs": types_qs,
            "stats_url": stats_url + (f"?{types_qs.rstrip('&')}" if types_qs else ""),
            "title": _filtered_title(title, f)}


SONG_MINIMUMS = [2, 3, 5, 10]  # "Heard at least … times" on the songs list (2 by default)


def songs_context(request: HttpRequest, owner) -> dict:
    """Every song heard live (a person's, or with ``owner=None`` everyone's), as original songs, heard at least
    ?min= times, a page at a time."""
    from django.core.paginator import Paginator

    from . import stats

    f = _stats_filter(request)
    raw = request.GET.get("min") or ""
    min_times = int(raw) if raw.isdigit() and int(raw) in SONG_MINIMUMS else SONG_MINIMUMS[0]
    page = Paginator(stats.heard_songs(owner, f, min_times), 50).get_page(request.GET.get("page"))
    page.object_list = list(page.object_list)
    stats.link_originals(page.object_list)
    types_qs = _filter_qs(f)
    stats_url = reverse("events:user_stats", args=[owner.get_username()]) if owner is not None \
        else reverse("events:site_stats")
    return {"owner": owner, "page": page, "types": list(f.types), "year": f.year, "min_times": min_times,
            "min_choices": SONG_MINIMUMS, "stats_url": stats_url + (f"?{types_qs.rstrip('&')}" if types_qs else ""),
            "title": _filtered_title(gettext("Most heard songs"), f)}


def user_songs(request: HttpRequest, username: str) -> HttpResponse:
    return render(request, "events/songs_ranking.html", {**songs_context(request, _owner(username)), "tab": "stats"})


def user_ranking(request: HttpRequest, username: str, kind: str) -> HttpResponse:
    return render(request, "events/user_ranking.html",
                  {**ranking_context(request, _owner(username), kind), "tab": "stats"})


def user_reviews(request: HttpRequest, username: str) -> HttpResponse:
    owner = _owner(username)
    reviews = with_social_counts(
        Log.objects.filter(user=owner).exclude(review="").select_related("user__profile", "event")
    ).order_by("-created_at")
    return render(
        request, "events/user_reviews.html", {"owner": owner, "tab": "reviews", "reviews": reviews}
    )


def _favorites(user):
    return FavoriteEvent.objects.filter(user=user).select_related("event", "event__venue", "first_act", "second_act")


@login_required
def profile_edit(request: HttpRequest) -> HttpResponse:
    """Profile details. Favourites are edited in a modal that loads its (heavy) form only when opened."""
    if request.GET.get("welcome") == "1":  # old sign-up link: the step-by-step sign-up replaced it
        return redirect("events:welcome")
    form = ProfileForm(request.POST or None, instance=get_profile(request.user))
    if request.method == "POST" and form.is_valid():
        form.save()
        translation.activate(form.instance.language)  # "Profile saved." in the language just picked
        messages.success(request, gettext("Profile saved."))
        followed = getattr(form, "followed_now", 0)
        if followed:
            messages.success(request, ngettext("You now follow %(n)s more artist.", "You now follow %(n)s more artists.",
                                               followed) % {"n": followed})
        # form.instance.user carries the (possibly new) username; request.user is a stale copy.
        return redirect("events:profile", username=form.instance.user.get_username())
    return render(
        request,
        "events/profile_edit.html",
        {"form": form, "favorites": _favorites(request.user)},
    )


@login_required
def favorites_edit(request: HttpRequest) -> HttpResponse:
    """The favourites form: inside the profile page's modal (fetched, saved without leaving the page) or as
    a page of its own without JavaScript."""
    in_modal = request.headers.get("X-Requested-With") == "fetch"
    form = FavoritesForm(request.POST or None, initial=FavoritesForm.initial_for(request.user), user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save(request.user)
        if in_modal:  # the page swaps in the new list and closes the modal
            summary = render_to_string("events/_favorites_summary.html", {"favorites": _favorites(request.user)},
                                       request=request)
            return JsonResponse({"ok": True, "summary": summary})
        messages.success(request, gettext("Favourites saved."))
        return redirect("events:profile_edit")
    if in_modal:
        html = render_to_string("events/_favorites_form.html", {"favorites_form": form}, request=request)
        return JsonResponse({"ok": False, "form": html}) if request.method == "POST" else HttpResponse(html)
    return render(request, "events/favorites_edit.html", {"favorites_form": form})


def people(request: HttpRequest) -> HttpResponse:
    users = (
        User.objects.filter(is_active=True)
        .select_related("profile")
        .annotate(
            log_count=Count("logs", distinct=True),
            follower_count=Count("follower_set", distinct=True),
        )
        .order_by("-log_count", "-follower_count", "username")
    )
    following_ids = set()
    if request.user.is_authenticated:
        following_ids = set(
            Follow.objects.filter(follower=request.user).values_list("following_id", flat=True)
        )
    return render(request, "events/people.html", {"people": users[:60], "following_ids": following_ids})


# --------------------------------------------------------------------------- follows


@login_required
@require_POST
def follow_toggle(request: HttpRequest, username: str) -> HttpResponse:
    owner = _owner(username)
    if owner == request.user:
        messages.error(request, gettext("You can't follow yourself."))
    else:
        deleted, _ = Follow.objects.filter(follower=request.user, following=owner).delete()
        if not deleted:
            Follow.objects.create(follower=request.user, following=owner)
    return redirect(_safe_next(request, get_profile(owner).get_absolute_url()))


def _follow_list(request: HttpRequest, username: str, which: str) -> HttpResponse:
    owner = _owner(username)
    if which == "followers":
        users = User.objects.filter(following_set__following=owner)
    else:
        users = User.objects.filter(follower_set__follower=owner)
    users = users.select_related("profile").annotate(log_count=Count("logs", distinct=True)).order_by("username")
    following_ids = set()
    if request.user.is_authenticated:
        following_ids = set(Follow.objects.filter(follower=request.user).values_list("following_id", flat=True))
    return render(
        request,
        "events/follow_list.html",
        {"owner": owner, "tab": which, "people": users, "following_ids": following_ids},
    )


def followers(request: HttpRequest, username: str) -> HttpResponse:
    return _follow_list(request, username, "followers")


def following(request: HttpRequest, username: str) -> HttpResponse:
    return _follow_list(request, username, "following")


# --------------------------------------------------------------------------- review pages


def review_detail(request: HttpRequest, pk: int) -> HttpResponse:
    log = get_object_or_404(
        with_social_counts(Log.objects.select_related("user__profile", "event", "event__venue")), pk=pk
    )
    liked = request.user.is_authenticated and ReviewLike.objects.filter(user=request.user, log=log).exists()
    return render(
        request,
        "events/review_detail.html",
        {
            "log": log,
            "comments": log.comments.select_related("user__profile"),
            "acts": log.acts.select_related("artist"),
            "missed": log.event.eventartist_set.exclude(artist__act_logs__log=log).select_related("artist"),
            "liked": liked,
            "comment_form": CommentForm(),
            "likers": User.objects.filter(review_likes__log=log).select_related("profile")[:12],
        },
    )


@login_required
@require_POST
def review_like_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    log = get_object_or_404(Log, pk=pk)
    deleted, _ = ReviewLike.objects.filter(user=request.user, log=log).delete()
    if not deleted:
        ReviewLike.objects.create(user=request.user, log=log)
    return redirect(_safe_next(request, f"/reviews/{log.pk}/"))


@login_required
@require_POST
def comment_add(request: HttpRequest, pk: int) -> HttpResponse:
    log = get_object_or_404(Log, pk=pk)
    form = CommentForm(request.POST)
    if form.is_valid():
        comment = form.save(commit=False)
        comment.user = request.user
        comment.log = log
        comment.save()
    else:
        messages.error(request, gettext("Your reply can't be empty."))
    return redirect("events:review_detail", pk=log.pk)


@login_required
@require_POST
def comment_delete(request: HttpRequest, pk: int, comment_pk: int) -> HttpResponse:
    comment = get_object_or_404(Comment.objects.select_related("log"), pk=comment_pk, log_id=pk)
    # The comment's author or the review's author can remove it.
    if request.user.pk not in (comment.user_id, comment.log.user_id):
        raise PermissionDenied
    comment.delete()
    return redirect("events:review_detail", pk=pk)


# --------------------------------------------------------------------------- activity feed


@dataclass
class Activity:
    kind: str  # "log" | "want" | "list" | "follow" | "like" | "comment"
    at: datetime
    actor: Any
    obj: Any


def activity_for(user_ids, limit: int = 50) -> list[Activity]:
    """Merge recent actions by these users, newest first. Built from existing tables, no extra writes."""
    items: list[Activity] = []
    items += [
        Activity("log", x.created_at, x.user, x)
        for x in Log.objects.filter(user__in=user_ids).select_related("user__profile", "event")
        .order_by("-created_at")[:limit]
    ]
    items += [
        Activity("want", x.created_at, x.user, x)
        for x in WantToGo.objects.filter(user__in=user_ids).select_related("user__profile", "event")
        .order_by("-created_at")[:limit]
    ]
    items += [
        Activity("list", x.created_at, x.user, x)
        for x in EventList.objects.filter(user__in=user_ids).select_related("user__profile")
        .order_by("-created_at")[:limit]
    ]
    items += [
        Activity("follow", x.created_at, x.follower, x)
        for x in Follow.objects.filter(follower__in=user_ids).select_related("follower__profile", "following__profile")
        .order_by("-created_at")[:limit]
    ]
    items += [
        Activity("like", x.created_at, x.user, x)
        for x in ReviewLike.objects.filter(user__in=user_ids)
        .select_related("user__profile", "log__user__profile", "log__event")
        .order_by("-created_at")[:limit]
    ]
    items += [
        Activity("comment", x.created_at, x.user, x)
        for x in Comment.objects.filter(user__in=user_ids)
        .select_related("user__profile", "log__user__profile", "log__event")
        .order_by("-created_at")[:limit]
    ]
    items.sort(key=lambda a: a.at, reverse=True)
    return items[:limit]


@login_required
def activity(request: HttpRequest) -> HttpResponse:
    mode = request.GET.get("show") or "friends"
    if mode == "you":
        user_ids = [request.user.pk]
    else:
        mode = "friends"
        user_ids = list(Follow.objects.filter(follower=request.user).values_list("following_id", flat=True))

    suggestions = []
    if mode == "friends" and not user_ids:
        suggestions = (
            User.objects.exclude(pk=request.user.pk).filter(is_active=True).select_related("profile")
            .annotate(log_count=Count("logs")).filter(log_count__gt=0).order_by("-log_count")[:8]
        )
    return render(
        request,
        "events/activity.html",
        {"items": activity_for(user_ids) if user_ids else [], "mode": mode, "suggestions": suggestions},
    )
