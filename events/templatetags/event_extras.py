from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def stars(value) -> str:
    """Render a 1–10 rating as ½–5 stars, e.g. 7 -> '★★★½'."""
    if value in (None, ""):
        return ""
    try:
        points = int(round(float(value)))
    except (TypeError, ValueError):
        return ""
    points = max(0, min(points, 10))
    return "★" * (points // 2) + ("½" if points % 2 else "")


@register.filter
def avg_stars(value) -> str:
    """Render an average 1–10 rating as '3.6' on the 5-star scale."""
    if value is None:
        return ""
    return f"{float(value) / 2:.1f}"


@register.filter
def is_admin(user) -> bool:
    """{% if user|is_admin %} — members of the Admins group and superusers."""
    from ..roles import is_admin as _is_admin

    return _is_admin(user)


@register.inclusion_tag("events/_rating_histogram.html")
def rating_histogram(logs, title: str = "Ratings", show_average: bool = False):
    """Letterboxd-style bars for ½★ … ★★★★★ from a queryset of logs (ratings stored 1–10)."""
    from django.db.models import Count

    counts = dict(logs.exclude(rating=None).values_list("rating").annotate(n=Count("id")).values_list("rating", "n"))
    total = sum(counts.values())
    peak = max(counts.values(), default=0)
    bars = [
        {
            "rating": r,
            "label": stars(r),
            "count": counts.get(r, 0),
            "height": round(counts.get(r, 0) / peak * 100) if peak else 0,
        }
        for r in range(1, 11)
    ]
    average = sum(r * n for r, n in counts.items()) / total if total else None
    return {"bars": bars, "total": total, "average": average, "title": title, "show_average": show_average}


@register.inclusion_tag("events/_year_histogram.html")
def year_histogram(logs, title: str = "Events by year"):
    """Bars for how many dated events were attended each year, gap years included as empty bars."""
    from django.db.models import Count
    from django.db.models.functions import ExtractYear

    counts = dict(
        logs.exclude(event__date=None).annotate(y=ExtractYear("event__date"))
        .values("y").annotate(n=Count("id")).values_list("y", "n")
    )
    if not counts:
        return {"total": 0}
    peak = max(counts.values())
    years = range(min(counts), max(counts) + 1)
    bars = [{"year": y, "count": counts.get(y, 0), "height": round(counts.get(y, 0) / peak * 100)} for y in years]
    return {"bars": bars, "total": sum(counts.values()), "title": title, "first": years[0], "last": years[-1]}


@register.simple_tag(takes_context=True)
def poster_mode(context, event) -> str:
    """The viewer's thumbnail preference if they set one, else the event's own."""
    user = getattr(context.get("request"), "user", None)
    if user is not None and user.is_authenticated:
        profile = getattr(user, "profile", None)  # missing profile raises an AttributeError subclass
        if profile is not None and profile.thumbnail_display:
            return profile.thumbnail_display
    return event.image_display
