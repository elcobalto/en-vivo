from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.get_or_create(user=instance)


try:
    from allauth.account.signals import user_signed_up
except ImportError:  # allauth not installed
    user_signed_up = None


def fill_profile_from_social(user, sociallogin) -> None:
    """Use the name and photo from Google (or another provider) for a new profile."""
    data = getattr(getattr(sociallogin, "account", None), "extra_data", None) or {}
    profile, _ = Profile.objects.get_or_create(user=user)
    changed = []
    if not profile.display_name and data.get("name"):
        profile.display_name = str(data["name"])[:80]
        changed.append("display_name")
    if not profile.avatar_url and str(data.get("picture", "")).startswith("https://"):
        profile.avatar_url = data["picture"]
        changed.append("avatar_url")
    if changed:
        profile.save(update_fields=changed + ["updated_at"])


if user_signed_up is not None:

    @receiver(user_signed_up)
    def on_signed_up(sender, request, user, sociallogin=None, **kwargs):
        if sociallogin is not None:
            fill_profile_from_social(user, sociallogin)


# --------------------------------------------------------------------------- auto titles

from django.db.models.signals import post_delete  # noqa: E402

from .models import Event, EventPerformer  # noqa: E402


@receiver(post_save, sender=EventPerformer)
@receiver(post_delete, sender=EventPerformer)
def refresh_title_after_lineup_change(sender, instance, **kwargs):
    event = Event.objects.filter(pk=instance.event_id, title_is_auto=True).first()
    if event:
        event.refresh_auto_title()


# --------------------------------------------------------------------------- bootstrap superusers

from django.contrib.auth.signals import user_logged_in  # noqa: E402


@receiver(user_logged_in)
def promote_configured_superusers(sender, request, user, **kwargs):
    """Users whose email is in DJANGO_SUPERUSER_EMAILS become superusers on login."""
    emails = getattr(settings, "DJANGO_SUPERUSER_EMAILS", [])
    if user.email and user.email.lower() in emails and not (user.is_superuser and user.is_staff):
        user.is_superuser = True
        user.is_staff = True
        user.save(update_fields=["is_superuser", "is_staff"])
