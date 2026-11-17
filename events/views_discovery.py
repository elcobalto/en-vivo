"""Phase 2 — discovery: want-to-go, user lists and global search."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import EventListForm
from .models import Event, EventList, EventListItem, Performer, Production, Venue, WantToGo
from .views import _safe_next


def _profile_owner(username: str):
    return get_object_or_404(get_user_model(), username=username)


def _lists_with_preview(qs):
    """Annotate item counts and prefetch the first few events for poster previews."""
    return qs.select_related("user").annotate(item_count=Count("items")).prefetch_related(
        Prefetch(
            "items",
            queryset=EventListItem.objects.select_related("event").order_by("position"),
            to_attr="all_items",
        )
    )


# --------------------------------------------------------------------------- want to go


@login_required
@require_POST
def want_to_go_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(Event, pk=pk)
    deleted, _ = WantToGo.objects.filter(user=request.user, event=event).delete()
    if not deleted and not event.is_upcoming:
        messages.error(request, "Want to go is only for upcoming events.")
    elif not deleted:
        WantToGo.objects.create(user=request.user, event=event)
        messages.success(request, "Added to your Want to go.")
    else:
        messages.info(request, "Removed from your Want to go.")
    return redirect(_safe_next(request, event.get_absolute_url()))


def user_want_to_go(request: HttpRequest, username: str) -> HttpResponse:
    owner = _profile_owner(username)
    today = timezone.localdate()
    wanted = Event.objects.with_stats().filter(wanted_by__user=owner).select_related("venue")
    return render(
        request,
        "events/want_to_go.html",
        {
            "owner": owner,
            "tab": "want",
            "upcoming": wanted.filter(date__gte=today).order_by("date"),
            "past": wanted.filter(date__lt=today).order_by("-date"),
        },
    )


# --------------------------------------------------------------------------- lists


def _own_list(request: HttpRequest, pk: int) -> EventList:
    event_list = get_object_or_404(EventList, pk=pk)
    if event_list.user_id != request.user.pk:
        raise PermissionDenied
    return event_list


def list_index(request: HttpRequest) -> HttpResponse:
    lists = _lists_with_preview(EventList.objects.filter(items__isnull=False).distinct())[:24]
    return render(request, "events/list_index.html", {"lists": lists})


def user_lists(request: HttpRequest, username: str) -> HttpResponse:
    owner = _profile_owner(username)
    lists = _lists_with_preview(EventList.objects.filter(user=owner))
    return render(request, "events/user_lists.html", {"owner": owner, "tab": "lists", "lists": lists})


def list_detail(request: HttpRequest, pk: int) -> HttpResponse:
    event_list = get_object_or_404(EventList.objects.select_related("user"), pk=pk)
    items = event_list.items.select_related("event", "event__venue")
    return render(
        request,
        "events/list_detail.html",
        {"event_list": event_list, "items": items, "is_owner": event_list.user_id == request.user.pk},
    )


@login_required
def list_form(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    event_list = _own_list(request, pk) if pk else None
    form = EventListForm(request.POST or None, instance=event_list)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.user = request.user
        saved.save()
        messages.success(request, "List saved.")
        return redirect(saved)
    return render(request, "events/list_form.html", {"form": form, "event_list": event_list})


@login_required
@require_POST
def list_delete(request: HttpRequest, pk: int) -> HttpResponse:
    event_list = _own_list(request, pk)
    event_list.delete()
    messages.success(request, f"Deleted list “{event_list.title}”.")
    return redirect("events:user_lists", username=request.user.get_username())


@login_required
@require_POST
def list_add_event(request: HttpRequest, pk: int) -> HttpResponse:
    """Add an event (pk) to one of the user's lists, or to a brand-new list."""
    event = get_object_or_404(Event, pk=pk)
    new_title = (request.POST.get("new_list_title") or "").strip()

    with transaction.atomic():
        if new_title:
            event_list = EventList.objects.create(user=request.user, title=new_title[:200])
        else:
            event_list = _own_list(request, int(request.POST.get("list_id") or 0))

        if event_list.items.filter(event=event).exists():
            messages.info(request, f"Already in “{event_list.title}”.")
        else:
            last = event_list.items.aggregate(m=Max("position"))["m"] or 0
            EventListItem.objects.create(
                event_list=event_list,
                event=event,
                position=last + 1,
                note=(request.POST.get("note") or "").strip()[:300],
            )
            event_list.save(update_fields=["updated_at"])
            messages.success(request, f"Added to “{event_list.title}”.")
    return redirect(event)


def _renumber(event_list: EventList) -> None:
    for i, item in enumerate(event_list.items.order_by("position"), start=1):
        if item.position != i:
            EventListItem.objects.filter(pk=item.pk).update(position=i)


@login_required
@require_POST
def list_item_remove(request: HttpRequest, pk: int, item_pk: int) -> HttpResponse:
    event_list = _own_list(request, pk)
    with transaction.atomic():
        event_list.items.filter(pk=item_pk).delete()
        _renumber(event_list)
        event_list.save(update_fields=["updated_at"])
    return redirect(event_list)


@login_required
@require_POST
def list_item_move(request: HttpRequest, pk: int, item_pk: int) -> HttpResponse:
    event_list = _own_list(request, pk)
    item = get_object_or_404(EventListItem, pk=item_pk, event_list=event_list)
    up = request.POST.get("direction") == "up"
    neighbour = (
        event_list.items.filter(position__lt=item.position).order_by("-position").first()
        if up
        else event_list.items.filter(position__gt=item.position).order_by("position").first()
    )
    if neighbour:
        with transaction.atomic():
            a, b = item.position, neighbour.position
            EventListItem.objects.filter(pk=neighbour.pk).update(position=a)
            EventListItem.objects.filter(pk=item.pk).update(position=b)
            event_list.save(update_fields=["updated_at"])
    return redirect(event_list)


# --------------------------------------------------------------------------- search


def search(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    results = {}
    if q:
        results = {
            "events": Event.objects.with_stats()
            .select_related("venue")
            .filter(Q(title__icontains=q) | Q(performers__name__icontains=q))
            .distinct()[:12],
            "performers": Performer.objects.filter(name__icontains=q).annotate(n=Count("events"))[:12],
            "productions": Production.objects.filter(title__icontains=q).annotate(n=Count("events"))[:12],
            "venues": Venue.objects.filter(Q(name__icontains=q) | Q(city__icontains=q)).annotate(n=Count("events"))[:12],
            "lists": _lists_with_preview(EventList.objects.filter(title__icontains=q))[:12],
        }
    total = sum(len(v) for v in results.values())
    return render(request, "events/search.html", {"q": q, "results": results, "total": total})
