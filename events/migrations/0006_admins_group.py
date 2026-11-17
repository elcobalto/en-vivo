from django.contrib.auth.management import create_permissions
from django.db import migrations

ADMINS_GROUP = "Admins"
ADMIN_PERMISSIONS = ["moderate_event", "change_event", "delete_event"]


def create_admins_group(apps, schema_editor):
    # Permissions are normally created after migrations; make sure they exist now.
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group, _ = Group.objects.get_or_create(name=ADMINS_GROUP)
    group.permissions.add(
        *Permission.objects.filter(content_type__app_label="events", codename__in=ADMIN_PERMISSIONS)
    )


def remove_admins_group(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name=ADMINS_GROUP).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0005_optional_titles_and_moderation"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(create_admins_group, remove_admins_group)]
