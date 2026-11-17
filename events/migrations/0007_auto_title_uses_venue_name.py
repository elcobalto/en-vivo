from django.db import migrations, models

TYPE_LABELS = {
    "music": "Music",
    "theater": "Theater",
    "comedy": "Comedy",
    "dance": "Dance",
    "festival": "Festival",
    "other": "Other",
}


def rebuild_auto_titles(apps, schema_editor):
    """Regenerate automatic titles with the venue name instead of the city.

    Mirrors Event.build_auto_title (historical models don't have its methods).
    """
    Event = apps.get_model("events", "Event")
    EventPerformer = apps.get_model("events", "EventPerformer")
    for event in Event.objects.filter(title_is_auto=True).select_related("venue", "production"):
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
        if event.venue_id and (event.venue.name or event.venue.city):
            title += f" in {event.venue.name or event.venue.city}"
        if event.date:
            title += f", {event.date.day} {event.date:%b %Y}"
        if title[:240] != event.title:
            Event.objects.filter(pk=event.pk).update(title=title[:240])


class Migration(migrations.Migration):
    dependencies = [("events", "0006_admins_group")]

    operations = [
        migrations.AlterField(
            model_name="event",
            name="title",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Optional. Leave blank to use “Artist in Venue, date”.",
                max_length=240,
            ),
        ),
        migrations.RunPython(rebuild_auto_titles, migrations.RunPython.noop),
    ]
