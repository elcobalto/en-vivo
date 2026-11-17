from __future__ import annotations

from django import template

register = template.Library()


@register.inclusion_tag("events/_profile_header.html", takes_context=True)
def profile_header(context, owner, tab: str = ""):
    from ..views_social import _profile_header

    request = context["request"]
    return {
        **_profile_header(request, owner),
        "tab": tab,
        "user": request.user,
        "request": request,
        "csrf_token": context.get("csrf_token"),
    }


@register.inclusion_tag("events/_avatar.html")
def avatar(user, size: int = 32):
    profile = getattr(user, "profile", None)
    name = profile.name if profile else user.get_username()
    return {
        "url": profile.avatar_url if profile else "",
        "initial": (name[:1] or "?").upper(),
        "name": name,
        "size": size,
        "font": max(10, size // 2 - 2),
    }


@register.filter
def display_name(user) -> str:
    profile = getattr(user, "profile", None)
    return profile.name if profile else user.get_username()
