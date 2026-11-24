from django.db import migrations


def runs_become_stagings(apps, schema_editor):
    """Dates of the same show (one ``run_key``) become one staging; so does each other date of a play. A photo
    every date shares moves to the staging (the dates then show it from there)."""
    Event = apps.get_model("events", "Event")
    Staging = apps.get_model("events", "Staging")
    groups = {}
    for event in Event.objects.exclude(run_key="").order_by("date", "pk"):
        groups.setdefault(event.run_key, []).append(event)
    for event in Event.objects.filter(run_key="").exclude(play=None).filter(event_type__in=("theater", "dance")):
        groups[f"event-{event.pk}"] = [event]
    for dates in groups.values():
        first = dates[0]
        photos = {e.image_url for e in dates}
        shared = photos.pop() if len(photos) == 1 else ""
        staging = Staging.objects.create(
            play_id=next((e.play_id for e in dates if e.play_id), None), event_type=first.event_type,
            image_url=shared, image_display=first.image_display if shared else "fit", created_by_id=first.created_by_id,
        )
        Event.objects.filter(pk__in=[e.pk for e in dates]).update(staging=staging)
        if shared:
            Event.objects.filter(pk__in=[e.pk for e in dates]).update(image_url="")


class Migration(migrations.Migration):

    dependencies = [("events", "0064_profile_follow_logged_artists")]

    operations = [migrations.RunPython(runs_become_stagings, migrations.RunPython.noop)]
