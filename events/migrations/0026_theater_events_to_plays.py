"""Every existing theater event becomes a staging of a play named after its title.

The oldest staging names the play. Events with the same title (ignoring capitals, accents and spaces) share one play; the
play's authors are the events' author credits. Titles then follow the play.
"""
import unicodedata

from django.db import migrations


def _key(text):
    plain = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def forwards(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    Play = apps.get_model("events", "Play")
    EventPerformer = apps.get_model("events", "EventPerformer")
    plays = {_key(p.title): p for p in Play.objects.all()}
    for event in Event.objects.filter(event_type="theater", play__isnull=True).exclude(title="").order_by("date", "pk"):
        key = _key(event.title)
        if not key:
            continue
        play = plays.get(key)
        if play is None:
            play = plays[key] = Play.objects.create(title=" ".join(event.title.split())[:240])
        authors = EventPerformer.objects.filter(event=event, role="author").values_list("performer_id", flat=True)
        play.authors.add(*authors)
        Event.objects.filter(pk=event.pk).update(play=play, title=play.title, title_is_auto=True)


def backwards(apps, schema_editor):
    apps.get_model("events", "Event").objects.filter(event_type="theater").update(play=None, title_is_auto=False)


class Migration(migrations.Migration):
    dependencies = [("events", "0025_play")]
    operations = [migrations.RunPython(forwards, backwards)]
