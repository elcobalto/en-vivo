from django.db import migrations


class Migration(migrations.Migration):
    """Dates of the same show are grouped by their staging now (filled in 0065)."""

    dependencies = [("events", "0065_stagings_from_runs")]

    operations = [migrations.RemoveField(model_name="event", name="run_key")]
