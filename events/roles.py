"""Roles: every logged-in user can add events; admins can edit and delete any event."""
from __future__ import annotations

ADMINS_GROUP = "Admins"
MODERATE_PERMISSION = "events.moderate_event"


def is_admin(user) -> bool:
    """Members of the Admins group (and superusers) can edit and delete any event."""
    return bool(user and user.is_authenticated and user.has_perm(MODERATE_PERMISSION))


def can_edit_event(user, event) -> bool:
    if not (user and user.is_authenticated):
        return False
    return is_admin(user) or event.created_by_id in (None, user.pk)


def can_delete_event(user, event) -> bool:
    return is_admin(user)
