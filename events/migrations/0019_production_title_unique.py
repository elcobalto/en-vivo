import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0018_unique_production_names")]

    operations = [
        migrations.AddConstraint(
            model_name="production",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("title"),
                name="uq_production_title_ci",
            ),
        ),
    ]
