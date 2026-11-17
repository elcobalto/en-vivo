"""
Catalog + diary models for En Vivo ("Letterboxd for live events").

Catalog (our own database):
    Venue, Performer, Production, Event, EventPerformer
Diary:
    Log (a user's rating/review of an event they attended)
Music-only enrichment (imported from setlist.fm):
    Setlist, Song, SetlistSong
"""
from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Avg, Count
from django.urls import reverse
from django_countries.fields import CountryField


class EventType(models.TextChoices):
    MUSIC = "music", "Music"
    THEATER = "theater", "Theater"
    COMEDY = "comedy", "Comedy"
    DANCE = "dance", "Dance"
    FESTIVAL = "festival", "Festival"
    OTHER = "other", "Other"


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


class Performer(TimestampedModel):
    class Kind(models.TextChoices):
        PERSON = "person", "Person"
        GROUP = "group", "Band / group"
        COMPANY = "company", "Company / troupe"

    name = models.CharField(max_length=200, db_index=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.PERSON)
    bio = models.TextField(blank=True, default="")
    image_url = models.URLField(max_length=1000, blank=True, default="")

    # MusicBrainz id: what setlist.fm uses to identify artists.
    mbid = models.CharField("MusicBrainz ID", max_length=36, null=True, blank=True, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("events:performer_detail", args=[self.pk])


class Production(TimestampedModel):
    """A show, tour or run that groups several events (Letterboxd's 'film' page)."""

    title = models.CharField(max_length=240, db_index=True)
    event_type = models.CharField(max_length=20, choices=EventType.choices, default=EventType.MUSIC)
    description = models.TextField(blank=True, default="")
    image_url = models.URLField(max_length=1000, blank=True, default="")

    objects = LogStatsQuerySet.as_manager()

    class Meta:
        ordering = ["title"]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("events:production_detail", args=[self.pk])


class EventQuerySet(LogStatsQuerySet):
    def with_stats(self):
        return self.with_log_stats("logs")


class Event(TimestampedModel):
    """One specific performance: a date at a venue. This is what people attend and log."""

    title = models.CharField(
        max_length=240,
        db_index=True,
        blank=True,
        help_text="Optional. Leave blank to use the headliner's name.",
    )
    # True when the title was generated from lineup/venue/date and should follow their changes.
    title_is_auto = models.BooleanField(default=False, editable=False)
    event_type = models.CharField(
        max_length=20, choices=EventType.choices, default=EventType.MUSIC, db_index=True
    )
    production = models.ForeignKey(
        Production, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    venue = models.ForeignKey(
        Venue, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    date = models.DateField(null=True, blank=True, db_index=True)
    start_time = models.TimeField(null=True, blank=True)
    description = models.TextField(blank=True, default="")
    image_url = models.URLField(max_length=1000, blank=True, default="")

    performers = models.ManyToManyField(Performer, through="EventPerformer", related_name="events")

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

        Falls back to the production's title, then to “Music event”, “Comedy event”…
        """
        headliner = self.headliner if self.pk else None
        if self.pk and self.event_type == EventType.FESTIVAL and not self.production_id:
            names = list(
                self.eventperformer_set.filter(role=EventPerformer.Role.HEADLINER)
                .order_by("billing_order", "performer__name")
                .values_list("performer__name", flat=True)[:4]
            )
            if len(names) > 1:
                # “Green Day & Foo Fighters”, “A, B & C”, “A, B, C & more”
                shown = names[:3]
                tail = "more" if len(names) > 3 else shown.pop()
                return f"{', '.join(shown)} & {tail}"[:240]
        if headliner:
            title = headliner.name
        elif self.production_id:
            title = self.production.title
        else:
            title = f"{self.get_event_type_display()} event"
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
        """The event's own photo, else its headliner's photo, else "" (the placeholder is shown)."""
        if self.image_url:
            return self.image_url
        headliner = self.headliner if self.pk else None
        return headliner.image_url if headliner else ""

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
    def headliner(self) -> Performer | None:
        link = self.eventperformer_set.select_related("performer").first()
        return link.performer if link else None


class EventPerformer(models.Model):
    class Role(models.TextChoices):
        HEADLINER = "headliner", "Headliner"
        SUPPORT = "support", "Support"
        GUEST = "guest", "Guest"
        CAST = "cast", "Cast"
        HOST = "host", "Host"
        DIRECTOR = "director", "Director"
        OTHER = "other", "Other"

    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    performer = models.ForeignKey(Performer, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.HEADLINER)
    billing_order = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "performer"], name="uq_event_performer"),
            models.CheckConstraint(
                check=models.Q(billing_order__gte=1), name="ck_event_performer_billing_gte_1"
            ),
        ]
        ordering = ["billing_order", "performer__name"]

    def __str__(self) -> str:
        return f"{self.performer} @ {self.event} ({self.get_role_display()})"


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


# --------------------------------------------------------------------------- setlists (music)


class Song(TimestampedModel):
    performer = models.ForeignKey(Performer, on_delete=models.CASCADE, related_name="songs")
    title = models.CharField(max_length=240, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["performer", "title"], name="uq_song_performer_title"),
        ]
        ordering = ["performer__name", "title"]

    def __str__(self) -> str:
        return f"{self.title} — {self.performer}"


class Setlist(TimestampedModel):
    """What one performer played at one event. Imported from setlist.fm or entered by hand."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="setlists")
    performer = models.ForeignKey(Performer, on_delete=models.CASCADE, related_name="setlists")

    setlistfm_id = models.CharField(max_length=40, null=True, blank=True, unique=True)
    source_url = models.URLField(blank=True, default="")
    notes = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "performer"], name="uq_setlist_event_performer"),
        ]
        ordering = ["event", "performer__name"]

    def __str__(self) -> str:
        return f"{self.performer} — {self.event}"


class SetlistSong(models.Model):
    setlist = models.ForeignKey(Setlist, on_delete=models.CASCADE, related_name="entries")
    song = models.ForeignKey(Song, on_delete=models.PROTECT, related_name="setlist_entries")
    position = models.PositiveIntegerField()
    set_name = models.CharField(max_length=60, blank=True, default="", help_text='e.g. "Encore"')
    note = models.CharField(max_length=200, blank=True, default="", help_text='e.g. "acoustic"')

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
        "Ranked list", default=False, help_text="Show numbers next to each entry."
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
    home_city = models.CharField("Home city", max_length=120, blank=True, default="")
    home_country = CountryField("Home country", blank=True, default="")

    def __str__(self) -> str:
        return self.name

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

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "event"], name="uq_favorite_user_event"),
            models.UniqueConstraint(fields=["user", "position"], name="uq_favorite_user_position"),
        ]
        ordering = ["position"]

    def __str__(self) -> str:
        return f"{self.user} ♥ {self.event}"


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
