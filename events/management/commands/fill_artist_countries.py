import time

from django.core.management.base import BaseCommand

from events.models import Artist
from events.musicbrainz import artist_country


class Command(BaseCommand):
    help = "Fill missing artist countries from MusicBrainz (artists with a MusicBrainz id)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, limit, **options):
        filled = 0
        for artist in Artist.objects.filter(country="").exclude(mbid=None)[:limit]:
            code = artist_country(artist.mbid)
            if code:
                artist.country = code
                artist.save(update_fields=["country", "updated_at"])
                filled += 1
            time.sleep(1.1)  # MusicBrainz allows about one request per second
        self.stdout.write(f"Filled {filled} artist countries.")
