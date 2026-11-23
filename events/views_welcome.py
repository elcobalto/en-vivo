"""Signing up, step by step: a few questions per page instead of the whole settings page at once.

Each step saves only its own fields; optional steps can be skipped. The last step is “What's the last show you
went to?” (``first_show``)."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .forms import ProfileForm
from .views_social import get_profile

# (slug, title, intro, fields, can be skipped)
STEPS = [
    ("name", _("What should we call you?"),
     _("You can change it, and everything else, any time."),
     ["username", "display_name"], False),
    ("place", _("Where do you live?"),
     _("So we can show you the shows near you."),
     ["home_country", "home_city", "home_region"], True),
    ("home", _("What should your home page show?"),
     _("Where to look for upcoming shows, depending on where the artists are from."),
     ["upcoming_local", "upcoming_international", "home_followed_only"], True),
    ("about", _("A bit about you"),
     _("A photo and a few words for your profile. Optional."),
     ["avatar_url", "bio"], True),
]
SLUGS = [step[0] for step in STEPS]
# The last show they went to comes after these, as its own page.
TOTAL_STEPS = len(STEPS) + 1


class WelcomeStepForm(ProfileForm):
    """The profile form with only one step's fields; the rest of the profile is left as it is."""

    def __init__(self, *args, fields=(), **kwargs):
        super().__init__(*args, **kwargs)
        for name in list(self.fields):
            if name not in fields:
                del self.fields[name]
        self.order_fields(fields)


def _next_url(index: int) -> str:
    return reverse("events:welcome_step", args=[SLUGS[index + 1]]) if index + 1 < len(STEPS) else reverse(
        "events:first_show")


@login_required
def welcome(request: HttpRequest) -> HttpResponse:
    return redirect("events:welcome_step", step=SLUGS[0])


@login_required
def welcome_step(request: HttpRequest, step: str) -> HttpResponse:
    if step not in SLUGS:
        raise Http404
    index = SLUGS.index(step)
    slug, title, intro, fields, optional = STEPS[index]
    if request.method == "POST" and optional and "skip" in request.POST:
        return redirect(_next_url(index))
    form = WelcomeStepForm(request.POST or None, instance=get_profile(request.user), fields=fields)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(_next_url(index))
    return render(request, "events/welcome_step.html", {
        "form": form, "title": title, "intro": intro, "optional": optional,
        "number": index + 1, "total": TOTAL_STEPS, "percent": round(100 * (index + 1) / TOTAL_STEPS),
        "back": reverse("events:welcome_step", args=[SLUGS[index - 1]]) if index else "",
    })
