"""Who uses En Vivo and how: user counts, daily activity and what people create (the admins' page)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import Comment, Event, EventList, Follow, Log, Setlist, UserDay, WantToGo


class UserDayMiddleware:
    """Counts the pages each signed-in person opens per day (successful GETs of HTML pages)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if (user is not None and user.is_authenticated and request.method == "GET" and response.status_code == 200
                and response.get("Content-Type", "").startswith("text/html")):
            try:
                count_view(user)
            except Exception:  # stats must never break a page
                pass
        return response


def count_view(user) -> None:
    today = timezone.localdate()
    if UserDay.objects.filter(user=user, day=today).update(views=F("views") + 1):
        return
    try:
        with transaction.atomic():
            UserDay.objects.create(user=user, day=today, views=1)
    except IntegrityError:  # another request made today's row first
        UserDay.objects.filter(user=user, day=today).update(views=F("views") + 1)


@dataclass
class SiteStats:
    users: int
    new_7: int
    new_30: int
    active_today: int
    active_7: int
    active_30: int
    views_today: int
    views_7: int
    days: list  # [(date, active people, page views)] for the last 30 days, oldest first
    active_max: int
    created: list  # [(label, last 7 days, last 30 days, all time)]
    top: list  # [(user, page views, days active)] for the last 30 days


def site_stats() -> SiteStats:
    now = timezone.now()
    today = timezone.localdate()
    User = get_user_model()
    activity = UserDay.objects.filter(day__gt=today - timedelta(days=30))

    per_day = {row["day"]: row for row in activity.values("day").annotate(people=Count("user"), views=Sum("views"))}
    days = []
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        row = per_day.get(d) or {}
        days.append((d, row.get("people", 0), row.get("views", 0)))

    def people_since(n_days: int) -> int:
        return activity.filter(day__gt=today - timedelta(days=n_days)).values("user").distinct().count()

    def views_since(n_days: int) -> int:
        return activity.filter(day__gt=today - timedelta(days=n_days)).aggregate(n=Sum("views"))["n"] or 0

    week, month = now - timedelta(days=7), now - timedelta(days=30)
    sources = [
        (_("Diary entries"), Log.objects.all()),
        (_("Reviews"), Log.objects.exclude(review="")),
        (_("Events created"), Event.objects.all()),
        (_("Setlists imported"), Setlist.objects.all()),
        (_("Lists"), EventList.objects.all()),
        (_("Comments"), Comment.objects.all()),
        (_("Follows"), Follow.objects.all()),
        (_("Want to go"), WantToGo.objects.all()),
    ]
    created = [(label, qs.filter(created_at__gte=week).count(), qs.filter(created_at__gte=month).count(), qs.count())
               for label, qs in sources]

    top_rows = (activity.values("user").annotate(views=Sum("views"), days=Count("day")).order_by("-views")[:10])
    users = User.objects.in_bulk([r["user"] for r in top_rows])
    top = [(users[r["user"]], r["views"], r["days"]) for r in top_rows if r["user"] in users]

    return SiteStats(
        users=User.objects.filter(is_active=True).count(),
        new_7=User.objects.filter(date_joined__gte=week).count(),
        new_30=User.objects.filter(date_joined__gte=month).count(),
        active_today=people_since(1),
        active_7=people_since(7),
        active_30=people_since(30),
        views_today=views_since(1),
        views_7=views_since(7),
        days=days,
        active_max=max([d[1] for d in days] + [1]),
        created=created,
        top=top,
    )
