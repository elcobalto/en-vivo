"""
Catalog + diary models for En Vivo ("Letterboxd for live events").

Catalog (our own database):
    Venue, Artist, Production, Event, EventArtist
Diary:
    Log (a user's rating/review of an event they attended)
Music-only enrichment (imported from setlist.fm):
    Setlist, Song, SetlistSong
"""
from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Avg, Count, F
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext, gettext_lazy as _
from django_countries.fields import CountryField


class EventType(models.TextChoices):
    MUSIC = "music", _("Music")
    THEATER = "theater", _("Theater")
    COMEDY = "comedy", _("Comedy")
    DANCE = "dance", _("Dance")
    FESTIVAL = "festival", _("Festival")
    OTHER = "other", _("Other")


# The title of an event with no lineup or production yet.
AUTO_TITLES = {
    EventType.MUSIC: _("Music event"),
    EventType.THEATER: _("Theater event"),
    EventType.COMEDY: _("Comedy event"),
    EventType.DANCE: _("Dance event"),
    EventType.FESTIVAL: _("Festival event"),
    EventType.OTHER: _("Other event"),
}


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class LogStatsQuerySet(models.QuerySet):
    def with_log_stats(self, logs_path: str):
        return self.annotate(
            avg_rating=Avg(f"{logs_path}__rating"),
            log_count=Count(logs_path, distinct=True),
        )


# --------------------------------------------------------------------------- catalog


class Venue(TimestampedModel):
    name = models.CharField(max_length=200, db_index=True)
    address = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=120, blank=True, default="")
    region = models.CharField(max_length=120, blank=True, default="")
    country = CountryField(blank=True, default="")
    capacity = models.PositiveIntegerField(null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    setlistfm_id = models.CharField(max_length=40, null=True, blank=True, unique=True)
    # Google Places id: venues picked from search are standardized and never duplicated.
    google_place_id = models.CharField(max_length=255, null=True, blank=True, unique=True, editable=False)

    class Meta:
        ordering = ["name", "city"]

    def __str__(self) -> str:
        return ", ".join(bit for bit in [self.name, self.city, self.country.code] if bit)

    @property
    def location_label(self) -> str:
        """“Ñuñoa, Región Metropolitana, Chile” — for search results and venue pages."""
        return ", ".join(bit for bit in [self.city, self.region, self.country.name] if bit)

    def get_absolute_url(self) -> str:
        return reverse("events:venue_detail", args=[self.pk])


class Artist(TimestampedModel):
    class Kind(models.TextChoices):
        PERSON = "person", _("Person")
        GROUP = "group", _("Band / group")
        COMPANY = "company", _("Company / troupe")

    name = models.CharField(max_length=200, db_index=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.PERSON)
    bio = models.TextField(blank=True, default="")
    image_url = models.URLField(max_length=1000, blank=True, default="")

    # Where the artist is from: splits "local" and "foreign" in profile stats.
    country = CountryField(blank=True, default="")
    # MusicBrainz id: what setlist.fm uses to identify artists.
    mbid = models.CharField("MusicBrainz ID", max_length=36, null=True, blank=True, unique=True)
    # Bands this artist was in (Paul McCartney → The Beatles): their songs get their own album chart.
    member_of = models.ManyToManyField(
        "self", symmetrical=False, blank=True, related_name="members", verbose_name=_("Member of"),
        help_text=_("Bands this artist was a member of."),
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("events:artist_detail", args=[self.pk])


def normalize_name(text: str) -> str:
    """Comparable name: ignores capitals, accents and extra spaces."""
    import unicodedata

    plain = "".join(c for c in unicodedata.normalize("NFKD", str(text or "")) if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


class Production(TimestampedModel):
    """A tour or run by a artist (linked per lineup act), or a festival grouping events."""

    title = models.CharField(max_length=240, db_index=True)
    event_type = models.CharField(max_length=20, choices=EventType.choices, default=EventType.MUSIC)
    # Whose tour/run this is. Empty for festivals, which group whole events instead.
    artist = models.ForeignKey(
        "Artist", on_delete=models.SET_NULL, null=True, blank=True, related_name="tours"
    )
    description = models.TextField(blank=True, default="")
    image_url = models.URLField(max_length=1000, blank=True, default="")

    objects = LogStatsQuerySet.as_manager()

    class Meta:
        ordering = ["title"]
        constraints = [
            # One production per name (ignoring capitals); forms also ignore accents.
            models.UniqueConstraint(Lower("title"), name="uq_production_title_ci"),
        ]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("events:production_detail", args=[self.pk])

    @classmethod
    def find_by_name(cls, title: str, exclude_pk=None):
        """The production with this name, ignoring capitals, accents and extra spaces (or None)."""
        wanted = normalize_name(title)
        if not wanted:
            return None
        qs = cls.objects.all() if exclude_pk is None else cls.objects.exclude(pk=exclude_pk)
        return next((p for p in qs.only("pk", "title", "artist_id", "event_type") if normalize_name(p.title) == wanted), None)


class Play(TimestampedModel):
    """An original play. Each staging of it is a theater event linked here (one play, many adaptations)."""

    title = models.CharField(max_length=240, db_index=True)
    authors = models.ManyToManyField("Artist", blank=True, related_name="plays_written")
    description = models.TextField(blank=True, default="")
    image_url = models.URLField(max_length=1000, blank=True, default="")

    class Meta:
        ordering = ["title"]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("events:play_detail", args=[self.pk])


class EventQuerySet(LogStatsQuerySet):
    def with_stats(self):
        return self.with_log_stats("logs")



def production_named(title: str, event_type: str, artist=None) -> Production:
    """The one production with this name, created if needed (names are unique).

    A tour already owned by another artist isn't hijacked: the new one gets the artist's
    name added, e.g. “World Tour (Green Day)”. A name used by a tour isn't reused as a
    festival (or vice versa) for the same reason.
    """
    title = " ".join(title.split())[:240]
    existing = Production.find_by_name(title)
    is_festival = event_type == EventType.FESTIVAL
    if existing:
        same_kind = (existing.event_type == EventType.FESTIVAL) == is_festival
        same_owner = artist is None or existing.artist_id in (None, getattr(artist, "pk", None))
        if same_kind and same_owner:
            if artist is not None and not existing.artist_id and not is_festival:
                Production.objects.filter(pk=existing.pk).update(artist=artist)
            return Production.objects.get(pk=existing.pk)
        suffix = artist.name if (artist is not None and not same_owner) else ("festival" if is_festival else "tour")
        return production_named(f"{title} ({suffix})", event_type, artist)
    return Production.objects.create(title=title, event_type=event_type, artist=artist)

class Event(TimestampedModel):
    """One specific performance: a date at a venue. This is what people attend and log."""

    title = models.CharField(
        max_length=240,
        db_index=True,
        blank=True,
        help_text=_("Optional. Leave blank to use the headliner's name."),
    )
    # True when the title was generated from lineup/venue/date and should follow their changes.
    title_is_auto = models.BooleanField(default=False, editable=False)
    event_type = models.CharField(
        max_length=20, choices=EventType.choices, default=EventType.MUSIC, db_index=True
    )
    production = models.ForeignKey(
        Production, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    # Theater only: the original play this event stages.
    play = models.ForeignKey(
        Play, on_delete=models.SET_NULL, null=True, blank=True, related_name="stagings",
        verbose_name=_("Original play"),
    )
    venue = models.ForeignKey(
        Venue, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    date = models.DateField(null=True, blank=True, db_index=True)
    description = models.TextField(blank=True, default="")
    image_url = models.URLField(max_length=1000, blank=True, default="")
    class Thumbnail(models.TextChoices):
        FIT = "fit", _("Whole photo on a blurred background")
        CROP = "crop", _("Crop to fill")
        STRETCH = "stretch", _("Stretch to fill (may look distorted)")
        AUTO = "auto", _("Auto: stretch photos close to poster shape, blur wider ones")

    # How the photo sits in the poster-shaped thumbnails.
    image_display = models.CharField(
        _("Thumbnail"), max_length=10, choices=Thumbnail.choices, default=Thumbnail.FIT,
        help_text=_("How the photo fits the poster-shaped thumbnails on cards and lists."),
    )

    artists = models.ManyToManyField(Artist, through="EventArtist", related_name="events")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events_created",
    )
    # Events at the same venue and date that someone confirmed are different shows
    # (so we stop suggesting to merge them).
    separate_from = models.ManyToManyField("self", symmetrical=True, blank=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["-date", "title"]
        permissions = [("moderate_event", "Can edit or delete any event")]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        if not self.title.strip():
            self.title_is_auto = True
        if self.title_is_auto:
            self.title = self.build_auto_title()
        super().save(*args, **kwargs)

    def build_auto_title(self) -> str:
        """The headliner's name (venue and date are shown next to the title, not in it).

        Falls back to the production's title, then to “Music event”, “Comedy event”… Titles are saved, so
        they're written in the site's language (Spanish), whoever saves the event.
        """
        with translation.override(settings.LANGUAGE_CODE):
            return self._auto_title()

    def _auto_title(self) -> str:
        if self.event_type == EventType.THEATER and self.play_id:
            return self.play.title[:240]  # stagings are named after their play
        headliner = self.headliner if self.pk else None
        if self.event_type == EventType.FESTIVAL and self.production_id:
            return self.production.title[:240]  # festival days are named after their festival
        if self.pk and self.event_type == EventType.FESTIVAL and not self.production_id:
            names = list(
                self.eventartist_set.filter(role=EventArtist.Role.HEADLINER)
                .order_by("billing_order", "artist__name")
                .values_list("artist__name", flat=True)[:4]
            )
            if len(names) > 1:
                # “Green Day & Foo Fighters”, “A, B & C”, “A, B, C & more”
                shown = names[:3]
                tail = gettext("more") if len(names) > 3 else shown.pop()
                return f"{', '.join(shown)} & {tail}"[:240]
        if headliner:
            title = headliner.name
        elif self.production_id:
            title = self.production.title
        else:
            title = str(AUTO_TITLES.get(self.event_type, AUTO_TITLES[EventType.OTHER]))
        return title[:240]

    def refresh_auto_title(self) -> None:
        """Recompute a generated title after its lineup changed."""
        if self.title_is_auto:
            new_title = self.build_auto_title()
            if new_title != self.title:
                Event.objects.filter(pk=self.pk).update(title=new_title)
                self.title = new_title

    def get_absolute_url(self) -> str:
        return reverse("events:event_detail", args=[self.pk])

    @property
    def display_image(self) -> str:
        """Own photo, else festival or play photo, else the headliner's tour photo, else the photo
        of the first person on the bill (cast, crew or lineup, in billing order) who has one."""
        if self.image_url:
            return self.image_url
        if self.production_id and self.production.image_url:  # festival days
            return self.production.image_url
        if self.play_id and self.play.image_url:  # theater stagings
            return self.play.image_url
        if not self.pk:
            return ""
        link = self.acts_on_stage().select_related("production").first()
        if link and link.production_id and link.production.image_url:
            return link.production.image_url
        first_with_photo = (
            self.eventartist_set.exclude(artist__image_url="")
            .order_by("billing_order", "pk").values_list("artist__image_url", flat=True).first()
        )
        return first_with_photo or ""

    @property
    def has_happened(self) -> bool:
        """Can it be logged yet? Undated events count as happened (many past shows lack a date)."""
        from django.utils import timezone

        return self.date is None or self.date <= timezone.localdate()

    @property
    def is_upcoming(self) -> bool:
        """Dated today or later: the only events you can add to Want to go."""
        from django.utils import timezone

        return self.date is not None and self.date >= timezone.localdate()

    @property
    def is_music(self) -> bool:
        return self.event_type in (EventType.MUSIC, EventType.FESTIVAL)

    @property
    def headliner(self) -> Artist | None:
        """The top-billed act on stage (authors and directors are credits, not acts)."""
        link = self.acts_on_stage().select_related("artist").first()
        return link.artist if link else None

    def acts_on_stage(self):
        return self.eventartist_set.exclude(role__in=CREDIT_ROLES)

    @property
    def card_lineup(self) -> list[str]:
        """Festival cards show the headliners (the acts billed as Headliner), in billing order."""
        if self.event_type != EventType.FESTIVAL or not self.pk:
            return []
        return list(self.eventartist_set.filter(role=EventArtist.Role.HEADLINER)
                    .order_by("billing_order", "pk").values_list("artist__name", flat=True))

    def order_bill(self) -> None:
        """Headliners are always billed above everyone else on stage; credits (authors, directors) go last.

        The order within each group is kept. Only billing numbers that change are saved.
        """
        links = list(self.eventartist_set.order_by("billing_order", "pk"))
        headliners = [l for l in links if l.role == EventArtist.Role.HEADLINER]
        credits = [l for l in links if l.role in CREDIT_ROLES]
        others = [l for l in links if l not in headliners and l not in credits]
        for position, link in enumerate(headliners + others + credits, start=1):
            if link.billing_order != position:
                link.billing_order = position
                link.save(update_fields=["billing_order"])

    def credits(self):
        return self.eventartist_set.filter(role__in=CREDIT_ROLES).select_related("artist")



# Roles that credit someone without them being an act you watch (theater authors, directors).
CREDIT_ROLES = ("author", "director")

class EventArtist(models.Model):
    class Role(models.TextChoices):
        HEADLINER = "headliner", _("Headliner")
        SUPPORT = "support", _("Support")
        GUEST = "guest", _("Guest")
        CAST = "cast", _("Cast")
        HOST = "host", _("Host")
        DIRECTOR = "director", _("Director")
        AUTHOR = "author", _("Author / playwright")
        OTHER = "other", _("Other")

    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.HEADLINER)
    billing_order = models.PositiveIntegerField(default=1)
    start_time = models.TimeField(_("Set time"), null=True, blank=True)
    # The tour this act was on at this event (tours belong to artists, not events).
    production = models.ForeignKey(
        Production, on_delete=models.SET_NULL, null=True, blank=True, related_name="lineup_entries",
        verbose_name=_("Tour"),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "artist"], name="uq_event_artist"),
            models.CheckConstraint(
                check=models.Q(billing_order__gte=1), name="ck_event_artist_billing_gte_1"
            ),
        ]
        ordering = ["billing_order", "artist__name"]

    def __str__(self) -> str:
        return f"{self.artist} @ {self.event} ({self.get_role_display()})"


# --------------------------------------------------------------------------- diary


class Log(TimestampedModel):
    """A diary entry: the user attended this event. Rating is stored 1–10, shown as ½–5 stars."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="logs")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="logs")

    rating = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    review = models.TextField(blank=True, default="")
    liked = models.BooleanField(default=False)
    contains_spoilers = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "event"], name="uq_log_user_event"),
        ]
        ordering = ["-event__date", "-created_at"]

    def __str__(self) -> str:
        return f"{self.user} — {self.event}"


class ActLog(TimestampedModel):
    """One act the user actually saw at a logged event, with its own rating and note."""

    log = models.ForeignKey(Log, on_delete=models.CASCADE, related_name="acts")
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="act_logs")
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["log", "artist"], name="uq_actlog_log_artist")]
        ordering = ["pk"]

    def __str__(self) -> str:
        return f"{self.log.user} saw {self.artist} at {self.log.event}"


def add_all_acts(log: Log) -> None:
    """Mark every act in the event's lineup as seen (the default for a new log)."""
    seen = set(log.acts.values_list("artist_id", flat=True))
    ActLog.objects.bulk_create(
        [ActLog(log=log, artist_id=pid)
         for pid in log.event.acts_on_stage().values_list("artist_id", flat=True) if pid not in seen]
    )


# --------------------------------------------------------------------------- setlists (music)


class Song(TimestampedModel):
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="songs")
    title = models.CharField(max_length=240, db_index=True)
    # The studio album it first appeared on, from MusicBrainz (looked up once, on demand).
    album_title = models.CharField(max_length=240, blank=True, default="")
    album_year = models.PositiveSmallIntegerField(null=True, blank=True)
    album_mbid = models.CharField("MusicBrainz release group", max_length=36, blank=True, default="")
    album_checked_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Whose song it originally is when this artist plays it as a cover (from setlist.fm's "cover").
    cover_of_name = models.CharField(_("Cover of"), max_length=200, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["artist", "title"], name="uq_song_artist_title"),
        ]
        ordering = ["artist__name", "title"]

    def __str__(self) -> str:
        return f"{self.title} — {self.artist}"


class Album(TimestampedModel):
    """An artist's album, added by admins so albums nobody heard live still show (with 0 songs)."""

    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="albums")
    title = models.CharField(max_length=240)
    year = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["artist__name", F("year").desc(nulls_last=True), "title"]
        constraints = [models.UniqueConstraint(Lower("title"), "artist", name="uq_album_artist_title_ci")]

    def __str__(self) -> str:
        return f"{self.title} — {self.artist}"


class Setlist(TimestampedModel):
    """What one artist played at one event. Imported from setlist.fm or entered by hand."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="setlists")
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="setlists")

    setlistfm_id = models.CharField(max_length=40, null=True, blank=True, unique=True)
    source_url = models.URLField(blank=True, default="")
    notes = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "artist"], name="uq_setlist_event_artist"),
        ]
        ordering = ["event", "artist__name"]

    def __str__(self) -> str:
        return f"{self.artist} — {self.event}"

    @property
    def song_count(self) -> int:
        """Songs actually played (tapes and intros played from a recording don't count)."""
        return sum(1 for entry in self.entries.all() if not entry.is_tape)


class SetlistSong(models.Model):
    setlist = models.ForeignKey(Setlist, on_delete=models.CASCADE, related_name="entries")
    song = models.ForeignKey(Song, on_delete=models.PROTECT, related_name="setlist_entries")
    position = models.PositiveIntegerField()
    set_name = models.CharField(max_length=60, blank=True, default="", help_text='e.g. "Encore"')
    note = models.CharField(max_length=200, blank=True, default="", help_text='e.g. "acoustic"')
    # Played from a recording (intro/outro tapes): listed, but not counted as a song played.
    is_tape = models.BooleanField(_("Tape"), default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["setlist", "position"], name="uq_setlistsong_position"),
            models.CheckConstraint(check=models.Q(position__gte=1), name="ck_setlistsong_position_gte_1"),
        ]
        ordering = ["position"]

    def __str__(self) -> str:
        return f"{self.position}. {self.song.title}"


# --------------------------------------------------------------------------- discovery


class WantToGo(models.Model):
    """Letterboxd's watchlist: events a user wants to attend (or wishes they had)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="want_to_go")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="wanted_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "event"], name="uq_wanttogo_user_event")]
        ordering = ["event__date", "-created_at"]

    def __str__(self) -> str:
        return f"{self.user} wants to go to {self.event}"


class EventList(TimestampedModel):
    """A user-curated list of events, e.g. 'Best concerts of 2025'."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_lists")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    is_ranked = models.BooleanField(
        _("Ranked list"), default=False, help_text=_("Show numbers next to each entry.")
    )

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("events:list_detail", args=[self.pk])


class EventListItem(models.Model):
    event_list = models.ForeignKey(EventList, on_delete=models.CASCADE, related_name="items")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="list_items")
    position = models.PositiveIntegerField()
    note = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event_list", "event"], name="uq_listitem_list_event"),
        ]
        ordering = ["position"]

    def __str__(self) -> str:
        return f"{self.position}. {self.event}"


# --------------------------------------------------------------------------- social


class Profile(TimestampedModel):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    display_name = models.CharField(max_length=80, blank=True, default="")
    bio = models.TextField(blank=True, default="", max_length=1000)
    location = models.CharField(max_length=120, blank=True, default="")
    avatar_url = models.URLField(blank=True, default="")
    # Used as defaults (setlist.fm search, new venues) and to show nearby upcoming events.
    home_city = models.CharField(_("Home city"), max_length=120, blank=True, default="")
    home_country = CountryField(_("Home country"), blank=True, default="")
    # Overrides each event's own thumbnail setting for this person, when set.
    thumbnail_display = models.CharField(
        _("Thumbnails"), max_length=10, blank=True, default="",
        choices=[("", _("As each event is set"))] + list(Event.Thumbnail.choices),
        help_text=_("How photos fit the poster-shaped thumbnails when you browse."),
    )
    # Whether "I attended these shows" starts ticked when importing from setlist.fm (it can be unticked there).
    import_as_attended = models.BooleanField(
        _("Mark setlist.fm imports as attended"), default=True,
        help_text=_("Starts the \"I attended these shows\" box ticked when you import shows from setlist.fm. "
                    "You can still untick it on each import."),
    )
    # The language the site is shown in for this person. Filled from home_country when the profile is created
    # (Spanish for Spain and Latin America, English elsewhere, Spanish without a country): see save().
    language = models.CharField(
        _("Language"), max_length=5, choices=settings.LANGUAGES, blank=True, default="",
        help_text=_("The language En Vivo is shown in for you."),
    )

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.language:
            from .languages import language_for_country

            self.language = language_for_country(self.home_country.code or "")
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = {*kwargs["update_fields"], "language"}
        super().save(*args, **kwargs)

    @property
    def name(self) -> str:
        return self.display_name or self.user.get_full_name() or self.user.get_username()

    @property
    def initial(self) -> str:
        return (self.name[:1] or "?").upper()

    def get_absolute_url(self) -> str:
        return reverse("events:profile", args=[self.user.get_username()])


class FavoriteEvent(models.Model):
    """Up to four favourite events shown on a profile, like Letterboxd's four favourite films."""

    MAX = 4

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="favorite_events")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="favorited_by")
    position = models.PositiveSmallIntegerField()
    # Festivals only: up to two acts from its lineup to show on the card instead of its headliners.
    first_act = models.ForeignKey(Artist, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    second_act = models.ForeignKey(Artist, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "event"], name="uq_favorite_user_event"),
            models.UniqueConstraint(fields=["user", "position"], name="uq_favorite_user_position"),
        ]
        ordering = ["position"]

    def __str__(self) -> str:
        return f"{self.user} ♥ {self.event}"

    @property
    def chosen_acts(self) -> list[str]:
        """The names of the acts picked for this festival favourite's card (none for other events)."""
        if self.event.event_type != EventType.FESTIVAL:
            return []
        return [act.name for act in (self.first_act, self.second_act) if act is not None]


class Follow(models.Model):
    follower = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="following_set")
    following = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="follower_set")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["follower", "following"], name="uq_follow"),
            models.CheckConstraint(check=~models.Q(follower=models.F("following")), name="ck_follow_not_self"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.follower} → {self.following}"


class ReviewLike(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="review_likes")
    log = models.ForeignKey(Log, on_delete=models.CASCADE, related_name="likes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "log"], name="uq_reviewlike_user_log")]

    def __str__(self) -> str:
        return f"{self.user} likes review {self.log_id}"


class Comment(TimestampedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="comments")
    log = models.ForeignKey(Log, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField(max_length=2000)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.user} on review {self.log_id}"


# --------------------------------------------------------------------------- videos


class EventVideo(TimestampedModel):
    """A YouTube video from an event, of one act or of the whole event."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="videos")
    artist = models.ForeignKey(
        Artist, on_delete=models.CASCADE, null=True, blank=True, related_name="videos",
        help_text="Empty for videos of the whole event.",
    )
    youtube_id = models.CharField(max_length=20)
    title = models.CharField(max_length=200, blank=True, default="")
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="videos_added"
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "youtube_id"], name="uq_eventvideo_event_youtube")]
        ordering = ["created_at"]

    def __str__(self) -> str:
        return self.title or self.youtube_id

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.youtube_id}"

    @property
    def embed_url(self) -> str:
        return f"https://www.youtube-nocookie.com/embed/{self.youtube_id}"


class Feedback(TimestampedModel):
    """A bug report or idea sent from the beta banner, for admins to read in the feedback inbox."""

    class Kind(models.TextChoices):
        BUG = "bug", _("Bug")
        IDEA = "idea", _("Idea or feedback")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="feedback")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.BUG)
    message = models.TextField(max_length=4000)
    page = models.CharField(_("Page"), max_length=500, blank=True, default="")  # where they were when they wrote
    user_agent = models.CharField(max_length=300, blank=True, default="")  # browser and device, for bugs
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} from {self.user or 'someone'}: {self.message[:40]}"
