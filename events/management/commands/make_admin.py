from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from events.roles import ADMINS_GROUP


class Command(BaseCommand):
    help = "Give (or with --remove, take away) the En Vivo Admins role for a user."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--remove", action="store_true", help="Remove the Admins role instead.")

    def handle(self, username, remove=False, **options):
        try:
            user = get_user_model().objects.get(username=username)
        except get_user_model().DoesNotExist:
            raise CommandError(f"No user named “{username}”.")
        group = Group.objects.get(name=ADMINS_GROUP)
        if remove:
            user.groups.remove(group)
            self.stdout.write(self.style.SUCCESS(f"{username} is no longer an admin."))
        else:
            user.groups.add(group)
            self.stdout.write(self.style.SUCCESS(f"{username} is now an admin."))
