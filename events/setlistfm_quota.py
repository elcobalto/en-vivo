"""
Keep En Vivo inside setlist.fm's API limits: 2 requests per second and 1440 per day per key.

Every real request goes through ``reserve_call``, which
- spaces calls at least ``SETLISTFM_MIN_INTERVAL`` apart across all web workers (a locked DB row),
- refuses calls once the rolling 24 h total reaches ``SETLISTFM_DAILY_BUDGET`` (a rolling window
  keeps every calendar day under the limit too, whatever time zone setlist.fm counts in),
- stops multi-setlist imports while fewer than ``SETLISTFM_BULK_RESERVE`` calls are left,
- caps each non-admin at ``SETLISTFM_USER_DAILY_LIMIT`` calls per 24 h,
and logs a ``SetlistFmCall`` row that the admins' usage page reads.
"""
from __future__ import annotations

import contextvars
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from .models import SetlistFmCall, SetlistFmThrottle

API_DAILY_LIMIT = 1440  # setlist.fm's own limit, shown on the usage page
KEEP_DAYS = 35

_current_user = contextvars.ContextVar("setlistfm_user", default=None)
_bulk = contextvars.ContextVar("setlistfm_bulk", default=False)


class CurrentUserMiddleware:
    """Remembers the signed-in user for the request, so each setlist.fm call is logged (and capped) per person."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        token = _current_user.set(user if user is not None and user.is_authenticated else None)
        try:
            return self.get_response(request)
        finally:
            _current_user.reset(token)


@contextmanager
def bulk():
    """Calls made inside are part of a multi-setlist import: they give way before the daily budget runs low."""
    token = _bulk.set(True)
    try:
        yield
    finally:
        _bulk.reset(token)


def daily_budget() -> int:
    return int(getattr(settings, "SETLISTFM_DAILY_BUDGET", 1400))


def bulk_reserve() -> int:
    return int(getattr(settings, "SETLISTFM_BULK_RESERVE", 300))


def user_daily_limit() -> int:
    return int(getattr(settings, "SETLISTFM_USER_DAILY_LIMIT", 60))


def _window_start():
    return timezone.now() - timedelta(hours=24)


def used_last_24h() -> int:
    return SetlistFmCall.objects.filter(created_at__gte=_window_start()).count()


def remaining() -> int:
    return max(0, daily_budget() - used_last_24h())


def _is_admin(user) -> bool:
    from .roles import is_admin

    return is_admin(user)


def reserve_call(endpoint: str) -> SetlistFmCall:
    """Wait for our turn and log the call, or raise ``SetlistFmQuotaError`` if a limit says no."""
    from .setlistfm import SetlistFmQuotaError

    user = _current_user.get()
    is_bulk = _bulk.get()
    interval = float(getattr(settings, "SETLISTFM_MIN_INTERVAL", 0.6))
    with transaction.atomic():
        throttle = SetlistFmThrottle.objects.select_for_update().filter(pk=1).first()
        if throttle is None:  # normally made by the migration
            throttle, _ = SetlistFmThrottle.objects.get_or_create(pk=1)
        since = _window_start()
        used = SetlistFmCall.objects.filter(created_at__gte=since).count()
        left = daily_budget() - used
        if left <= 0:
            raise SetlistFmQuotaError(gettext(
                "En Vivo has used its setlist.fm requests for today. Please try again in a few hours."))
        if is_bulk and left <= bulk_reserve():
            raise SetlistFmQuotaError(gettext(
                "Bulk imports are paused: setlist.fm requests are running low today. "
                "Import one setlist at a time, or try again later."))
        if user is not None and not _is_admin(user):
            mine = SetlistFmCall.objects.filter(created_at__gte=since, user=user).count()
            if mine >= user_daily_limit():
                raise SetlistFmQuotaError(gettext(
                    "You've made a lot of setlist.fm searches today. Please try again tomorrow."))
        if throttle.last_call_at:
            wait = (throttle.last_call_at + timedelta(seconds=interval) - timezone.now()).total_seconds()
            if wait > 0:
                time.sleep(min(wait, interval))
        now = timezone.now()
        throttle.last_call_at = now
        throttle.save(update_fields=["last_call_at"])
        call = SetlistFmCall.objects.create(created_at=now, endpoint=endpoint, user=user, bulk=is_bulk)
    if random.random() < 0.02:  # now and then, forget old calls
        SetlistFmCall.objects.filter(created_at__lt=now - timedelta(days=KEEP_DAYS)).delete()
    return call


def finish_call(call: SetlistFmCall, status: int) -> None:
    """The HTTP status setlist.fm answered with, or 0 when no answer came."""
    call.status = status
    call.save(update_fields=["status"])


# ---------------------------------------------------------------- cache hits (calls we didn't need to make)

def _hits_key(day) -> str:
    return f"setlistfm:hits:{day:%Y%m%d}"


def count_cache_hit() -> None:
    key = _hits_key(timezone.now().date())
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=60 * 60 * 24 * 3)


def cache_hits(day) -> int:
    return cache.get(_hits_key(day)) or 0


# ---------------------------------------------------------------- the admins' usage page

@dataclass
class Usage:
    budget: int
    limit: int
    used: int
    remaining: int
    percent: int
    bulk_reserve: int
    bulk_paused: bool
    user_limit: int
    today_utc: int
    cache_hits_today: int
    peak_per_second: int
    failures: int
    rate_limited: int
    hours: list  # [(datetime hour start, calls)] for the last 24 h, oldest first
    hour_max: int
    days: list  # [(date, calls)] for the last 30 days, oldest first
    day_max: int
    by_endpoint: list  # [(label, calls)]
    by_user: list  # [(user or None, calls, bulk calls)]
    recent: list


def usage() -> Usage:
    now = timezone.now()
    since = now - timedelta(hours=24)
    calls = list(SetlistFmCall.objects.filter(created_at__gte=since).select_related("user").order_by("created_at"))
    budget = daily_budget()
    used = len(calls)

    hour0 = (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
    hours = [[hour0 + timedelta(hours=i), 0] for i in range(24)]
    peak = 0
    start = 0
    for i, call in enumerate(calls):
        slot = int((call.created_at - hour0).total_seconds() // 3600)
        if 0 <= slot < 24:
            hours[slot][1] += 1
        while call.created_at - calls[start].created_at >= timedelta(seconds=1):
            start += 1
        peak = max(peak, i - start + 1)

    endpoints = dict(SetlistFmCall.Endpoint.choices)
    by_endpoint: dict[str, int] = {}
    by_user: dict = {}
    for call in calls:
        by_endpoint[call.endpoint] = by_endpoint.get(call.endpoint, 0) + 1
        row = by_user.setdefault(call.user_id, [call.user, 0, 0])
        row[1] += 1
        row[2] += call.bulk

    from django.db.models import Count
    from django.db.models.functions import TruncDate

    first_day = (now - timedelta(days=29)).date()
    per_day = dict(
        SetlistFmCall.objects.filter(created_at__date__gte=first_day)
        .annotate(day=TruncDate("created_at")).values_list("day").annotate(n=Count("id")).values_list("day", "n")
    )
    days = [(first_day + timedelta(days=i), per_day.get(first_day + timedelta(days=i), 0)) for i in range(30)]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    return Usage(
        budget=budget,
        limit=API_DAILY_LIMIT,
        used=used,
        remaining=max(0, budget - used),
        percent=min(100, round(100 * used / budget)) if budget else 100,
        bulk_reserve=bulk_reserve(),
        bulk_paused=budget - used <= bulk_reserve(),
        user_limit=user_daily_limit(),
        today_utc=sum(1 for c in calls if c.created_at >= midnight),
        cache_hits_today=cache_hits(now.date()),
        peak_per_second=peak,
        failures=sum(1 for c in calls if c.status == 0 or (c.status and c.status >= 400 and c.status != 404)),
        rate_limited=sum(1 for c in calls if c.status == 429),
        hours=[tuple(h) for h in hours],
        hour_max=max([h[1] for h in hours] + [1]),
        days=days,
        day_max=max([d[1] for d in days] + [1]),
        by_endpoint=sorted(((endpoints.get(k, k), n) for k, n in by_endpoint.items()), key=lambda r: -r[1]),
        by_user=sorted((tuple(r) for r in by_user.values()), key=lambda r: -r[1])[:15],
        recent=list(reversed(calls[-50:])),
    )
