from django.db import migrations, models

# Spain and Latin America: Spanish. A copy of events.languages.SPANISH_FIRST_COUNTRIES as it was when
# this migration was written (migrations shouldn't change when that module does).
SPANISH_FIRST_COUNTRIES = {
    "AR", "BO", "BR", "CL", "CO", "CR", "CU", "DO", "EC", "ES", "GQ", "GT", "HN", "MX", "NI", "PA",
    "PE", "PR", "PY", "SV", "UY", "VE",
}


def set_languages(apps, schema_editor):
    """Existing profiles get Spanish, unless their home country speaks something else (then English)."""
    Profile = apps.get_model("events", "Profile")
    english = [
        pk for pk, country in Profile.objects.exclude(home_country="").values_list("pk", "home_country")
        if str(country).upper() not in SPANISH_FIRST_COUNTRIES
    ]
    Profile.objects.filter(pk__in=english).update(language="en")
    Profile.objects.exclude(pk__in=english).update(language="es")


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0034_feedback"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="language",
            field=models.CharField(
                blank=True,
                choices=[("es", "Español"), ("en", "English")],
                default="",
                help_text="The language En Vivo is shown in for you.",
                max_length=5,
                verbose_name="Language",
            ),
        ),
        migrations.RunPython(set_languages, migrations.RunPython.noop),
    ]
