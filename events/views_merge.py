"""Same venue, same day: merge two events into a festival or a headliner + support show."""
from __future__ import annotations

from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .merge import merge_events, same_slot_events
from .models import Event
from .roles import can_edit_event


@dataclass
class SlotSuggestion:
    other: Event
    festival: bool  # can make one festival of both
    this_supports_other: bool  # this event's acts open for the other's
    other_supports_this: bool
    can_dismiss: bool

    @property
    def any(self) -> bool:
        return self.festival or self.this_supports_other or self.other_supports_this or self.can_dismiss


def _festival_pair(user, event: Event, other: Event) -> tuple[Event, Event] | None:
    """(keep, remove) for a festival merge: the removed event must be one the user may edit."""
    if can_edit_event(user, other):
        return event, other
    if can_edit_event(user, event):
        return other, event
    return None


def suggestions_for(user, event: Event) -> list[SlotSuggestion]:
    """What this user can do about other events at the same venue and date (empty for visitors)."""
    if not user.is_authenticated:
        return []
    results = []
    for other in same_slot_events(event):
        suggestion = SlotSuggestion(
            other=other,
            festival=_festival_pair(user, event, other) is not None,
            this_supports_other=can_edit_event(user, event),  # this event is merged away
            other_supports_this=can_edit_event(user, other),
            can_dismiss=can_edit_event(user, event) or can_edit_event(user, other),
        )
        if suggestion.any:
            results.append(suggestion)
    return results


@login_required
@require_POST
def event_merge(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk)
    other = get_object_or_404(same_slot_events(event), pk=request.POST.get("other") or 0)
    action = request.POST.get("action")

    if action == "festival":
        pair = _festival_pair(request.user, event, other)
        if not pair:
            raise PermissionDenied
        keep, remove = pair
        mode = "festival"
    elif action == "this_supports_other":
        keep, remove, mode = other, event, "support"
    elif action == "other_supports_this":
        keep, remove, mode = event, other, "support"
    else:
        messages.error(request, "Unknown action.")
        return redirect(event)

    if not can_edit_event(request.user, remove):
        raise PermissionDenied

    removed_title = remove.title
    keep = merge_events(keep, remove, mode=mode)
    if mode == "festival":
        messages.success(request, f"Merged into one festival: “{keep.title}”.")
    else:
        messages.success(request, f"“{removed_title}” is now the support act on “{keep.title}”.")
    return redirect(keep)


@login_required
@require_POST
def event_separate(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk)
    other = get_object_or_404(same_slot_events(event), pk=request.POST.get("other") or 0)
    if not (can_edit_event(request.user, event) or can_edit_event(request.user, other)):
        raise PermissionDenied
    event.separate_from.add(other)
    messages.info(request, f"Got it: “{event.title}” and “{other.title}” are separate shows.")
    return redirect(event)
