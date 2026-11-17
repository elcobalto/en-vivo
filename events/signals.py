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
