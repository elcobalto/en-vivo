from django.db import migrations


def merge_duplicate_festival_days(apps, schema_editor):
    """Festival days with the same name, date and city are the same event: merge each group.

    The cheap check below uses this migration's historical models. Only when duplicates
    exist does it call the app's merge code (which moves logs, acts, setlists, lists...),
    so a fresh database never imports models that may have changed since.
    """
    import unicodedata

    def norm(text):
        plain = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))
        return " ".join(plain.casefold().split())

    Event = apps.get_model("events", "Event")
    seen, has_duplicates = set(), False
    for event in Event.objects.filter(event_type="festival").exclude(date=None).select_related("production", "venue"):
        key = (norm(event.production.title if event.production_id else event.title), event.date,
               norm(event.venue.city) if event.venue_id else "")
        if key[0] and key[2]:
            has_duplicates |= key in seen
            seen.add(key)
    if not has_duplicates:
        return

    from events.merge import merge_all_duplicate_festival_days

    merge_all_duplicate_festival_days()


class Migration(migrations.Migration):
    dependencies = [("events", "0015_tours_belong_to_performers")]

    operations = [migrations.RunPython(merge_duplicate_festival_days, migrations.RunPython.noop)]
