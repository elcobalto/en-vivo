from django.db import migrations, models

TYPE_LABELS = {
    "music": "Music",
    "theater": "Theater",
    "comedy": "Comedy",
    "dance": "Dance",
    "festival": "Festival",
    "other": "Other",
}


def headliner_only_titles(apps, schema_editor):
    """Automatic titles become just the headliner's name (venue/date are shown beside them).

    Mirrors Event.build_auto_title (historical models don't have its methods).
    """
    Event = apps.get_model("events", "Event")
    EventPerformer = apps.get_model("events", "EventPerformer")
    for event in Event.objects.filter(title_is_auto=True).select_related("production"):
        link = (
            EventPerformer.objects.filter(event=event)
            .select_related("performer")
            .order_by("billing_order", "performer__name")
            .first()
        )
        if link:
            title = link.performer.name
        elif event.production_id:
            title = event.production.title
        else:
            title = f"{TYPE_LABELS.get(event.event_type, 'Other')} event"
        if title[:240] != event.title:
            Event.objects.filter(pk=event.pk).update(title=title[:240])


class Migration(migrations.Migration):
    dependencies = [("events", "0008_venue_country_and_place_id")]

    operations = [
        migrations.AlterField(
            model_name="event",
            name="title",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Optional. Leave blank to use the headliner's name.",
                max_length=240,
            ),
        ),
        migrations.RunPython(headliner_only_titles, migrations.RunPython.noop),
    ]
