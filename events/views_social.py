"""Phase 3 — social: profiles, follows, review pages (likes + comments) and the activity feed."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, F, Max, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import CommentForm, FavoritesForm, ProfileForm
from .models import (
    Comment,
    EventList,
    FavoriteEvent,
    Follow,
    Log,
    Performer,
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


def _seen_artists(owner):
    return (
        Performer.objects.filter(act_logs__log__user=owner)
        .annotate(
            n=Count("act_logs", filter=Q(act_logs__log__user=owner), distinct=True),
            last_seen=Max("act_logs__log__event__date", filter=Q(act_logs__log__user=owner)),
        )
        .order_by("-n", F("last_seen").desc(nulls_last=True), "name")  # ties: most recently seen first
    )


def _local_foreign_artists(owner) -> dict:
    """Most seen artists split by the owner's home country (local artists are usually seen more)."""
    home = get_profile(owner).home_country
    if not home:
        return {"home_country": None}
    seen = _seen_artists(owner)
    return {
        "home_country": home,
        "local_artists": seen.filter(country=home.code)[:5],
        "foreign_artists": seen.exclude(country=home.code).exclude(country="")[:5],
        "unknown_artists": seen.filter(country="")[:5],
    }


def profile(request: HttpRequest, username: str) -> HttpResponse:
    owner = _owner(username)
    favorites = FavoriteEvent.objects.filter(user=owner).select_related("event", "event__venue")
    recent = Log.objects.filter(user=owner).select_related("event").order_by("-created_at")[:6]
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
            # Only acts the person actually saw count.
            "top_artists": _seen_artists(owner)[:5],
            **_local_foreign_artists(owner),
            "top_venues": Venue.objects.filter(events__logs__user=owner)
            .annotate(
                n=Count("events__logs", filter=Q(events__logs__user=owner), distinct=True),
                last_seen=Max("events__date", filter=Q(events__logs__user=owner)),
            )
            .order_by("-n", F("last_seen").desc(nulls_last=True), "name")[:5],
        },
    )


def user_reviews(request: HttpRequest, username: str) -> HttpResponse:
    owner = _owner(username)
    reviews = with_social_counts(
        Log.objects.filter(user=owner).exclude(review="").select_related("user__profile", "event")
    ).order_by("-created_at")
    return render(
        request, "events/user_reviews.html", {"owner": owner, "tab": "reviews", "reviews": reviews}
    )


@login_required
def profile_edit(request: HttpRequest) -> HttpResponse:
    prof = get_profile(request.user)
    current = list(FavoriteEvent.objects.filter(user=request.user).order_by("position"))
    initial = {f"slot_{i}": fav.event for i, fav in enumerate(current, start=1)}

    form = ProfileForm(request.POST or None, instance=prof)
    favorites_form = FavoritesForm(request.POST or None, initial=initial)

    if request.method == "POST" and form.is_valid() and favorites_form.is_valid():
        with transaction.atomic():
            form.save()
            FavoriteEvent.objects.filter(user=request.user).delete()
            FavoriteEvent.objects.bulk_create(
                [
                    FavoriteEvent(user=request.user, event=event, position=i)
                    for i, event in enumerate(favorites_form.ordered_events(), start=1)
                ]
            )
        messages.success(request, "Profile saved.")
        # form.instance.user carries the (possibly new) username; request.user is a stale copy.
        return redirect("events:profile", username=form.instance.user.get_username())

    return render(
        request,
        "events/profile_edit.html",
        {"form": form, "favorites_form": favorites_form, "welcome": request.GET.get("welcome") == "1"},
    )


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
        messages.error(request, "You can't follow yourself.")
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
            "acts": log.acts.select_related("performer"),
            "missed": log.event.eventperformer_set.exclude(performer__act_logs__log=log).select_related("performer"),
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
        messages.error(request, "Your reply can't be empty.")
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
