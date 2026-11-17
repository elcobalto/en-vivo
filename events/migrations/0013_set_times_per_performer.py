from django.db import migrations, models


def copy_event_times_to_headliners(apps, schema_editor):
    """An event's start time becomes its headliner's set time (the first billed act)."""
    Event = apps.get_model("events", "Event")
    EventPerformer = apps.get_model("events", "EventPerformer")
    for event in Event.objects.exclude(start_time=None):
        link = EventPerformer.objects.filter(event=event).order_by("billing_order", "pk").first()
        if link and not link.start_time:
            link.start_time = event.start_time
            link.save(update_fields=["start_time"])


class Migration(migrations.Migration):
    dependencies = [("events", "0012_profile_home_city")]

    operations = [
        migrations.AddField(
            model_name="eventperformer",
            name="start_time",
            field=models.TimeField(blank=True, null=True, verbose_name="Set time"),
        ),
        migrations.RunPython(copy_event_times_to_headliners, migrations.RunPython.noop),
        migrations.RemoveField(model_name="event", name="start_time"),
    ]
