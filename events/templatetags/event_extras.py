from __future__ import annotations

import re

from django import template
from django.utils.safestring import mark_safe
from django.utils import formats
from django.utils.translation import gettext_lazy

register = template.Library()

# The site's name, written in text as "En Vivo" (so the everyday phrase "en vivo" is left alone).
BRAND = "En Vivo"
_TAGS = re.compile(r"(<[^>]*>)")
_RAW = ("script", "style", "title", "textarea", "option")  # text inside these can't hold markup


@register.filter(is_safe=True)
def brand(html) -> str:
    """Show the site's name the house way, in lowercase bold (**en vivo**), wherever it appears in a page's text.

    Applied to rendered HTML (``{% filter brand %}``): only text between tags changes, never attributes, and not
    inside scripts, styles, titles, text areas or select options.
    """
    html = str(html)
    if BRAND not in html:
        return mark_safe(html)
    out, raw = [], None
    for part in _TAGS.split(html):
        if part.startswith("<"):
            name = part[1:].split(None, 1)[0].rstrip(">/").lower() if len(part) > 2 else ""
            if raw is None and name in _RAW and not part.endswith("/>"):
                raw = name
            elif raw and name == "/" + raw:
                raw = None
        elif raw is None and BRAND in part:
            part = part.replace(BRAND, '<strong class="brand">en vivo</strong>')
        out.append(part)
    return mark_safe("".join(out))


# Music and festival bills show headliners in bold and everyone else plainly, without a "Headliner"/"Performer" tag.
BILLED_TYPES = ("music", "festival")
UNTAGGED_ROLES = ("headliner", "support", "none")


@register.simple_tag(takes_context=True)
def page_query(context, param: str, number) -> str:
    """The current query string with ``param`` set to ``number`` (other filters and pages kept)."""
    query = context["request"].GET.copy()
    query[param] = number
    return query.urlencode()


@register.filter
def get_item(mapping, key):
    return mapping.get(key) if mapping else None


@register.filter
def role_tag(link, event_type) -> str:
    """The label next to an act on a lineup ("Host", "Cast"…), or "" where the bill's order and bold say it."""
    return "" if event_type in BILLED_TYPES and link.role in UNTAGGED_ROLES else link.get_role_display()


@register.filter
def is_bold_headliner(link, event_type) -> bool:
    return event_type in BILLED_TYPES and link.role == "headliner"


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
    """Render an average 1–10 rating as '3.6' on the 5-star scale ('3,6' in Spanish)."""
    if value is None:
        return ""
    return formats.number_format(round(float(value) / 2, 1), 1)


@register.filter
def is_admin(user) -> bool:
    """{% if user|is_admin %} — members of the Admins group and superusers."""
    from ..roles import is_admin as _is_admin

    return _is_admin(user)


@register.inclusion_tag("events/_rating_histogram.html")
def rating_histogram(logs, title: str = gettext_lazy("Ratings"), show_average: bool = False, link: str = ""):
    """Letterboxd-style bars for ½★ … ★★★★★ from a queryset of logs (ratings stored 1–10).
    With ``link`` (a diary URL), each bar with logs links to the diary filtered by that rating."""
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
            "url": f"{link}?rating={r}" if link and counts.get(r) else "",
        }
        for r in range(1, 11)
    ]
    average = sum(r * n for r, n in counts.items()) / total if total else None
    return {"bars": bars, "total": total, "average": average, "title": title, "show_average": show_average}


@register.inclusion_tag("events/_year_histogram.html")
def year_histogram(logs, title: str = gettext_lazy("Events by year"), link: str = ""):
    """Bars for how many dated events were attended each year, gap years included as empty bars.
    With ``link`` (a diary URL), each year with events links to the diary filtered by that year."""
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
    bars = [{"year": y, "count": counts.get(y, 0), "height": round(counts.get(y, 0) / peak * 100),
             "url": f"{link}{'&' if '?' in link else '?'}year={y}" if link and counts.get(y) else ""} for y in years]
    return {"bars": bars, "total": sum(counts.values()), "title": title, "first": years[0], "last": years[-1]}


@register.simple_tag(takes_context=True)
def poster_mode(context, event) -> str:
    """The viewer's thumbnail preference if they set one, else the event's own."""
    user = getattr(context.get("request"), "user", None)
    if user is not None and user.is_authenticated:
        profile = getattr(user, "profile", None)  # missing profile raises an AttributeError subclass
        if profile is not None and profile.thumbnail_display:
            return profile.thumbnail_display
    return getattr(event, "display_mode", event.image_display)


@register.simple_tag
def open_feedback_count() -> int:
    """How many bug reports and ideas no admin has resolved yet (for the admins' navbar link)."""
    from ..models import Feedback

    return Feedback.objects.filter(resolved=False).count()
