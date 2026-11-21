"""Beta feedback: anyone signed in can report a bug or send an idea; admins read them in the inbox."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from .forms import FeedbackForm
from .models import Feedback
from .roles import is_admin
from .views import _safe_next

FEEDBACK_PER_HOUR = 10  # per person, so a stuck button or a spammer can't flood the inbox


@login_required
def feedback(request: HttpRequest) -> HttpResponse:
    """The form the beta banner links to. ``next`` is the page they came from (stored, and where they go back)."""
    back = _safe_next(request, reverse("events:home"))
    form = FeedbackForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        key = f"feedback-sent:{request.user.pk}"
        sent = cache.get_or_set(key, 0, timeout=60 * 60)
        if sent >= FEEDBACK_PER_HOUR:
            messages.error(request, gettext("You've sent a lot of feedback in the last hour; please try again later."))
            return render(request, "events/feedback.html", {"form": form, "back": back})
        item = form.save(commit=False)
        item.user = request.user
        item.page = back[:500]
        item.user_agent = request.headers.get("User-Agent", "")[:300]
        item.save()
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=60 * 60)
        messages.success(request, gettext("Thanks! Your message reached the En Vivo team."))
        return redirect(back)
    return render(request, "events/feedback.html", {"form": form, "back": back})


def _require_admin(request: HttpRequest) -> None:
    if not is_admin(request.user):
        raise PermissionDenied


@login_required
def feedback_inbox(request: HttpRequest) -> HttpResponse:
    """Admins only: bug reports and ideas, open ones first by default."""
    _require_admin(request)
    status = request.GET.get("status") if request.GET.get("status") in ("open", "resolved", "all") else "open"
    kind = request.GET.get("kind") if request.GET.get("kind") in Feedback.Kind.values else ""
    items = Feedback.objects.select_related("user")
    counts = {"open": items.filter(resolved=False).count(), "resolved": items.filter(resolved=True).count()}
    counts["all"] = counts["open"] + counts["resolved"]
    if status != "all":
        items = items.filter(resolved=status == "resolved")
    if kind:
        items = items.filter(kind=kind)
    return render(request, "events/feedback_inbox.html", {
        "items": items[:200], "status": status, "kind": kind, "counts": counts, "kinds": Feedback.Kind.choices,
    })


@login_required
@require_POST
def feedback_resolve(request: HttpRequest, pk: int) -> HttpResponse:
    """Admins only: mark a message resolved, or open again."""
    _require_admin(request)
    item = get_object_or_404(Feedback, pk=pk)
    item.resolved = not item.resolved
    item.save(update_fields=["resolved", "updated_at"])
    return redirect(_safe_next(request, reverse("events:feedback_inbox")))

