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
