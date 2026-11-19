"""Performers are now called artists everywhere: models, fields and tables are renamed in place.

Constraints that name the old field are dropped first and recreated under the new names
(SQLite rebuilds tables on rename and would otherwise trip over them). No data changes.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0027_song_album")]

    operations = [
        migrations.RemoveConstraint(model_name="actlog", name="uq_actlog_log_performer"),
        migrations.RemoveConstraint(model_name="eventperformer", name="uq_event_performer"),
        migrations.RemoveConstraint(model_name="eventperformer", name="ck_event_performer_billing_gte_1"),
        migrations.RemoveConstraint(model_name="setlist", name="uq_setlist_event_performer"),
        migrations.RemoveConstraint(model_name="song", name="uq_song_performer_title"),

        migrations.RenameModel("Performer", "Artist"),
        migrations.RenameModel("EventPerformer", "EventArtist"),
        migrations.RenameField("production", "performer", "artist"),
        migrations.RenameField("eventartist", "performer", "artist"),
        migrations.RenameField("actlog", "performer", "artist"),
        migrations.RenameField("song", "performer", "artist"),
        migrations.RenameField("setlist", "performer", "artist"),
        migrations.RenameField("eventvideo", "performer", "artist"),
        migrations.RenameField("event", "performers", "artists"),

        migrations.AlterModelOptions(name="eventartist", options={"ordering": ["billing_order", "artist__name"]}),
        migrations.AlterModelOptions(name="setlist", options={"ordering": ["event", "artist__name"]}),
        migrations.AlterModelOptions(name="song", options={"ordering": ["artist__name", "title"]}),
        migrations.AddConstraint(
            model_name="actlog",
            constraint=models.UniqueConstraint(fields=("log", "artist"), name="uq_actlog_log_artist"),
        ),
        migrations.AddConstraint(
            model_name="eventartist",
            constraint=models.UniqueConstraint(fields=("event", "artist"), name="uq_event_artist"),
        ),
        migrations.AddConstraint(
            model_name="eventartist",
            constraint=models.CheckConstraint(check=models.Q(("billing_order__gte", 1)), name="ck_event_artist_billing_gte_1"),
        ),
        migrations.AddConstraint(
            model_name="setlist",
            constraint=models.UniqueConstraint(fields=("event", "artist"), name="uq_setlist_event_artist"),
        ),
        migrations.AddConstraint(
            model_name="song",
            constraint=models.UniqueConstraint(fields=("artist", "title"), name="uq_song_artist_title"),
        ),
    ]
