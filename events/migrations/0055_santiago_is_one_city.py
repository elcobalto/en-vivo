"""Seeds the place tables with Santiago (its communes count as the city) and the Metropolitan Region, then
standardizes every Chilean venue and profile with them (a venue in Ñuñoa: city Santiago, district Ñuñoa)."""
from django.db import migrations

# Greater Santiago: the 32 communes of the Santiago province, plus Puente Alto and San Bernardo.
COMMUNES = """Cerrillos
Cerro Navia
Conchalí
El Bosque
Estación Central
Huechuraba
Independencia
La Cisterna
La Florida
La Granja
La Pintana
La Reina
Las Condes
Lo Barnechea
Lo Espejo
Lo Prado
Macul
Maipú
Ñuñoa
Pedro Aguirre Cerda
Peñalolén
Providencia
Pudahuel
Quilicura
Quinta Normal
Recoleta
Renca
San Joaquín
San Miguel
San Ramón
Vitacura
Puente Alto
San Bernardo
Santiago Centro"""

CITY_ALIASES = """Santiago de Chile
Gran Santiago"""

REGION_ALIASES = """Región Metropolitana de Santiago
Santiago Metropolitan Region
Santiago Metropolitan
Metropolitan Region
Región Metropolitana de Santiago de Chile
Metropolitana
RM"""


def seed(apps, schema_editor):
    from events.places import aliases, key

    Region, City = apps.get_model("events", "Region"), apps.get_model("events", "City")
    Venue, Profile = apps.get_model("events", "Venue"), apps.get_model("events", "Profile")
    rm, _ = Region.objects.get_or_create(country="CL", name="Región Metropolitana", defaults={
        "setlistfm_name": "Santiago Metropolitan", "aliases": REGION_ALIASES})
    santiago, _ = City.objects.get_or_create(country="CL", name="Santiago", defaults={
        "region": rm, "aliases": CITY_ALIASES, "districts": COMMUNES})

    region_names = {key(n): rm.name for n in [rm.name, rm.setlistfm_name, *aliases(rm.aliases)]}
    districts = {key(n): n for n in aliases(santiago.districts)}
    city_names = {key(n) for n in [santiago.name, *aliases(santiago.aliases)]} | set(districts)
    for venue in Venue.objects.filter(country="CL"):
        city, region, district = venue.city, region_names.get(key(venue.region), venue.region), venue.district
        if key(venue.city) in city_names:
            district = districts.get(key(venue.city), district)
            city, region = santiago.name, rm.name
        if (city, region, district) != (venue.city, venue.region, venue.district):
            Venue.objects.filter(pk=venue.pk).update(city=city, region=region, district=district)
    for profile in Profile.objects.filter(home_country="CL"):
        city, region = profile.home_city, region_names.get(key(profile.home_region), profile.home_region)
        if key(city) in city_names:
            city, region = santiago.name, rm.name
        if (city, region) != (profile.home_city, profile.home_region):
            Profile.objects.filter(pk=profile.pk).update(home_city=city, home_region=region)


class Migration(migrations.Migration):
    dependencies = [("events", "0054_standard_places")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
