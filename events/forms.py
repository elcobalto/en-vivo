from __future__ import annotations

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory
from django_countries import countries

from .models import (
    CREDIT_ROLES,
    ActLog,
    Comment,
    Event,
    EventType,
    EventList,
    EventArtist,
    FavoriteEvent,
    Feedback,
    Log,
    Artist,
    Play,
    Production,
    Profile,
    Venue,
)
from .templatetags.event_extras import stars


class BootstrapMixin:
    """Adds Bootstrap classes to default Django widgets."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.RadioSelect, forms.CheckboxSelectMultiple)):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")


class EventForm(BootstrapMixin, forms.ModelForm):
    photo_for_all_days = forms.BooleanField(required=False)
    new_venue_name = forms.CharField(label="New venue name", max_length=200, required=False)
    new_venue_city = forms.CharField(label="City", max_length=120, required=False)
    new_venue_country = forms.ChoiceField(
        label="Country", required=False, choices=[("", "—")] + list(countries)
    )

    class Meta:
        model = Event
        fields = [
            "title",
            "event_type",
            "date",
            "venue",
            "play",
            "production",
            "description",
            "image_url",
            "image_display",
        ]
        widgets = {
            "venue": forms.HiddenInput(attrs={"class": "vs-value"}),
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "description": forms.Textarea(attrs={"rows": 4}),
        }
        labels = {"event_type": "Type", "image_url": "Poster / image URL"}
        help_texts = {"play": "Only for theater: the original play this staging is of. Its name becomes the event's name.",
                      "production": "Only for festivals: the festival this day belongs to. Tours are set per act in the lineup."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["production"].queryset = Production.objects.filter(event_type=EventType.FESTIVAL)
        self.fields["production"].label = "Festival"
        self.fields["image_display"].required = False
        from .merge import photo_siblings

        siblings = photo_siblings(self.instance)
        if siblings:
            self.fields["photo_for_all_days"].label = (
                f"Use this photo for {siblings['label']} ({siblings['count']} other {siblings['unit']}"
                f"{'s' if siblings['count'] != 1 else ''})"
            )
            # On by default, unless one of them has a different photo of its own: then saving other edits
            # shouldn't replace it (ticking the box still does).
            self.fields["photo_for_all_days"].initial = not siblings["own_photos"]
            if siblings["own_photos"]:
                n = siblings["own_photos"]
                self.fields["photo_for_all_days"].help_text = (
                    f"Off because {n} of them {'has' if n == 1 else 'have'} a different photo; tick it to replace "
                    f"{'it' if n == 1 else 'them'}.")
        else:
            del self.fields["photo_for_all_days"]
        order = [n for n in self.fields if n != "photo_for_all_days"]
        if "photo_for_all_days" in self.fields:
            order.insert(order.index("image_url") + 1, "photo_for_all_days")
        self.order_fields(order)
        self.fields["title"].required = False
        placeholder = "Leave blank to use the headliner's name"
        if self.instance.pk and self.instance.title_is_auto:
            # Keep generated titles blank in the form so they keep following lineup/venue/date.
            self.initial["title"] = ""
            placeholder = f"Automatic: {self.instance.title}"
        self.fields["title"].widget.attrs["placeholder"] = placeholder

    def clean_title(self) -> str:
        title = (self.cleaned_data.get("title") or "").strip()
        self.instance.title_is_auto = not title
        return title

    def clean_image_display(self) -> str:
        return self.cleaned_data.get("image_display") or Event.Thumbnail.FIT

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("venue") and (cleaned.get("new_venue_name") or "").strip():
            self.add_error("new_venue_name", "Pick an existing venue or add a new one, not both.")
        # City/country only matter when a new venue name is typed (they may be pre-filled from the profile).
        return cleaned

    def selected_venue(self) -> Venue | None:
        """The venue currently chosen (also after a failed submit), for the search box label."""
        value = self["venue"].value()
        if not value:
            return None
        return Venue.objects.filter(pk=value).first() if str(value).isdigit() else None

    def create_new_venue(self) -> Venue | None:
        """Create the venue typed into the 'new venue' fields (call before save, inside a transaction)."""
        name = (self.cleaned_data.get("new_venue_name") or "").strip()
        if not name:
            return None
        city = (self.cleaned_data.get("new_venue_city") or "").strip()
        venue = Venue.objects.filter(name__iexact=name, city__iexact=city).first() or Venue.objects.create(
            name=name, city=city, country=(self.cleaned_data.get("new_venue_country") or "").strip().upper()
        )
        self.instance.venue = venue
        return venue


class LazySelect(forms.Select):
    """A select that renders only the empty choice and the current value.

    Lineup rows would otherwise each repeat every artist (and tour) in the catalogue, which makes the
    edit page huge and slow. The full list is sent once as JSON (``source`` is its element id) and the
    page fills a row's options when it's opened. Validation still uses the field's whole queryset.
    """

    def __init__(self, source: str, attrs=None):
        super().__init__(attrs)
        self.source = source

    def optgroups(self, name, value, attrs=None):
        chosen = [v for v in value if v not in (None, "")]
        field = self.choices.field
        options = [self.create_option(name, "", field.empty_label or "", not chosen, 0, attrs=attrs)]
        for i, obj in enumerate(self.choices.queryset.filter(pk__in=chosen), start=1):
            options.append(self.create_option(name, str(obj.pk), field.label_from_instance(obj), True, i, attrs=attrs))
        return [(None, options, 0)]

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["attrs"]["data-options"] = self.source
        return context


class EventArtistForm(BootstrapMixin, forms.ModelForm):
    new_artist = forms.CharField(
        label="New artist", max_length=200, required=False,
        widget=forms.TextInput(attrs={"placeholder": "…or type a new artist", "class": "form-control form-control-sm mt-1"}),
    )

    class Meta:
        model = EventArtist
        fields = ["artist", "role", "billing_order", "start_time", "production"]
        labels = {"billing_order": "Order", "start_time": "Time", "production": "Tour"}
        widgets = {"start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].required = False
        self.fields["billing_order"].required = False
        self.fields["artist"].required = False
        for field_name, source in (("artist", "artist-options"), ("production", "tour-options")):
            field = self.fields[field_name]
            field.widget = LazySelect(source, attrs=field.widget.attrs)
            field.widget.choices = field.choices
        tours = self.fields["production"]
        tours.queryset = Production.objects.exclude(event_type=EventType.FESTIVAL).select_related("artist")
        tours.label_from_instance = lambda p: f"{p.title} ({p.artist.name})" if p.artist_id else p.title
        tours.empty_label = "No tour"

    def has_changed(self) -> bool:
        # A new row without a artist (picked or typed) is an unused blank row, whatever role/order say.
        if (not self.instance.pk and not self.data.get(self.add_prefix("artist"))
                and not (self.data.get(self.add_prefix("new_artist")) or "").strip()):
            return False
        return super().has_changed()

    def clean(self):
        cleaned = super().clean()
        name = (cleaned.get("new_artist") or "").strip()
        if cleaned.get("artist") and name:
            self.add_error("new_artist", "Pick an artist or type a new one, not both.")
        elif not cleaned.get("artist") and not name and not cleaned.get("DELETE"):
            self.add_error("artist", "Pick an artist or type a new one.")
        return cleaned

    def save(self, commit: bool = True):
        name = (self.cleaned_data.get("new_artist") or "").strip()
        if name and not self.cleaned_data.get("artist"):
            existing = next(
                (p for p in Artist.objects.filter(name__icontains=name[:3]) if p.name.casefold() == name.casefold()),
                None,
            )
            self.instance.artist = existing or Artist.objects.create(name=name[:200])
        return super().save(commit=commit)

    def clean_role(self) -> str:
        return self.cleaned_data.get("role") or EventArtist.Role.HEADLINER

    def clean_billing_order(self) -> int:
        return self.cleaned_data.get("billing_order") or 1


class BaseEventArtistFormSet(BaseInlineFormSet):
    """Acts added to a lineup start as Support: the rows "+ Add another act" adds (``empty_form``) and the
    blank row of an event that already has acts. A new event's first row stays Headliner."""

    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        if i >= self.initial_form_count() and self.initial_form_count():
            form.initial["role"] = EventArtist.Role.SUPPORT  # over the model's Headliner default
        return form

    @property
    def empty_form(self):
        form = super().empty_form
        form.initial["role"] = EventArtist.Role.SUPPORT
        return form


EventArtistFormSet = inlineformset_factory(
    Event,
    EventArtist,
    form=EventArtistForm,
    formset=BaseEventArtistFormSet,
    extra=1,
    can_delete=True,
)


class ArtistForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Artist
        fields = ["name", "kind", "country", "member_of", "bio", "image_url", "mbid"]
        widgets = {"bio": forms.Textarea(attrs={"rows": 4}), "member_of": forms.SelectMultiple(attrs={"data-search-multi": "", "data-placeholder": "Search bands…"})}
        help_texts = {"mbid": "Optional. Helps match setlist.fm imports to this artist.",
                      "member_of": "Bands this artist was in. Their songs played as "
                                   "covers get the band's own album chart."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        bands = Artist.objects.filter(kind=Artist.Kind.GROUP).order_by("name")
        if self.instance.pk:
            bands = bands.exclude(pk=self.instance.pk)
        self.fields["member_of"].queryset = bands


class VenueForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Venue
        fields = ["name", "address", "city", "region", "country", "capacity"]


class PlayForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Play
        fields = ["title", "authors", "description", "image_url"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4}), "authors": forms.SelectMultiple(attrs={"size": 6})}
        labels = {"authors": "Author(s)", "image_url": "Poster / image URL"}
        help_texts = {"authors": "Who wrote it. Hold Ctrl/Cmd to pick more than one."}


class ProductionForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Production
        fields = ["title", "event_type", "artist", "description", "image_url"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}
        labels = {"event_type": "Type", "image_url": "Poster / image URL", "artist": "Artist"}
        help_texts = {"artist": "Whose tour or run this is. Leave empty for a festival."}

    def clean_title(self) -> str:
        from django.utils.html import format_html

        title = " ".join((self.cleaned_data.get("title") or "").split())
        existing = Production.find_by_name(title, exclude_pk=self.instance.pk)
        if existing:
            raise forms.ValidationError(
                format_html('“{}” already exists: <a href="{}">open it</a> instead of creating a new one.',
                            existing.title, existing.get_absolute_url())
            )
        return title


RATING_CHOICES = [("", "No rating")] + [(i, stars(i)) for i in range(1, 11)]


class LogForm(BootstrapMixin, forms.ModelForm):
    rating = forms.TypedChoiceField(
        choices=RATING_CHOICES, coerce=int, empty_value=None, required=False
    )

    class Meta:
        model = Log
        fields = ["rating", "liked", "review", "contains_spoilers"]
        widgets = {"review": forms.Textarea(attrs={"rows": 5, "placeholder": "Add a review…"})}
        labels = {"liked": "♥ Liked it", "contains_spoilers": "Contains spoilers"}


class EventListForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = EventList
        fields = ["title", "description", "is_ranked"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


USERNAME_RE = r"^[A-Za-z0-9_.-]{3,30}$"


class ProfileForm(BootstrapMixin, forms.ModelForm):
    username = forms.RegexField(
        regex=USERNAME_RE,
        min_length=3,
        max_length=30,
        help_text="3–30 letters, numbers, dots, hyphens or underscores. Your profile lives at /u/<username>/.",
        error_messages={"invalid": "Use only letters, numbers, dots, hyphens or underscores (3–30 characters)."},
    )

    class Meta:
        model = Profile
        fields = ["display_name", "location", "home_city", "home_country", "avatar_url", "bio", "thumbnail_display",
                  "import_as_attended"]
        widgets = {"bio": forms.Textarea(attrs={"rows": 4})}
        labels = {"avatar_url": "Avatar image URL"}
        help_texts = {
            "home_city": "Upcoming events on the home page are shown for this city (or your country).",
            "home_country": "Also the default country for setlist.fm searches and new venues.",
        }

    field_order = ["username", "display_name", "location", "home_city", "home_country", "avatar_url", "bio"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].initial = self.instance.user.get_username()
        self.fields["username"].widget.attrs.update(
            {"autocomplete": "username", "autocapitalize": "none", "spellcheck": "false"}
        )

    def clean_username(self) -> str:
        from django.contrib.auth import get_user_model

        username = self.cleaned_data["username"].strip()
        taken = get_user_model().objects.filter(username__iexact=username).exclude(pk=self.instance.user_id)
        if taken.exists():
            raise forms.ValidationError("That username is taken.")
        return username

    def save(self, commit: bool = True):
        profile = super().save(commit=commit)
        user = self.instance.user
        if commit and user.get_username() != self.cleaned_data["username"]:
            user.username = self.cleaned_data["username"]
            user.save(update_fields=["username"])
        return profile


def favorite_choices(user=None):
    """The events you can pick as favourites: the ones in your diary (plus any already picked)."""
    from django.db.models import F, Q
    from django.db.models.functions import Lower

    events = Event.objects.select_related("venue")
    if user is not None:
        events = events.filter(Q(logs__user=user) | Q(favorited_by__user=user)).distinct()
    return events.order_by(Lower("title"), F("date").asc(nulls_last=True))


class EventChoiceField(forms.ModelChoiceField):
    """“Lollapalooza Chile — Parque Cerrillos · 15 Mar 2024”: the event, then where and when."""

    def label_from_instance(self, event):
        details = " · ".join(
            bit for bit in [event.venue.name if event.venue_id else "",
                            f"{event.date.day} {event.date:%b %Y}" if event.date else "date not added"] if bit
        )
        return f"{event.title} — {details}"


class FavoritesForm(BootstrapMixin, forms.Form):
    """Four optional slots for a user's favourite events. A festival slot can also pick up to two acts from
    its lineup that the user saw (``slot_N_act_1`` / ``slot_N_act_2``) to show on the card instead of the
    headliners.

    The act dropdowns only carry the picked festival's acts; favorites-form.js refills them from ``lineups``
    when another event is picked (a full list per dropdown made the page very heavy).
    """

    ACTS = 2

    def __init__(self, *args, events=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if events is None:
            events = favorite_choices(user)
        # Every festival you could pick, with the acts you saw there (in billing order).
        festivals = events.filter(event_type=EventType.FESTIVAL)
        links = (EventArtist.objects.filter(event__in=festivals).exclude(role__in=CREDIT_ROLES)
                 .select_related("artist").order_by("billing_order", "artist__name"))
        seen = None
        if user is not None:
            seen = set(ActLog.objects.filter(log__user=user, log__event__in=festivals)
                       .values_list("log__event_id", "artist_id"))
        self.lineups: dict[int, list[dict]] = {}
        for link in links:
            if seen is None or (link.event_id, link.artist_id) in seen:
                self.lineups.setdefault(link.event_id, []).append({"id": link.artist_id, "name": link.artist.name})
        for i in range(1, FavoriteEvent.MAX + 1):
            self.fields[f"slot_{i}"] = EventChoiceField(
                queryset=events, required=False, label=f"Favourite {i}", empty_label="—"
            )
            self.fields[f"slot_{i}"].widget.attrs.update({"class": "form-select", "data-favorite-slot": i})
            lineup = self.lineups.get(self._picked_event_id(i), [])
            for n in range(1, self.ACTS + 1):
                empty = "Headliners" if n == 1 else "—"
                field = forms.CharField(required=False, label=f"Act {n}", widget=forms.Select(
                    choices=[("", empty)] + [(str(a["id"]), a["name"]) for a in lineup]))
                field.widget.attrs.update({"class": "form-select form-select-sm", "data-favorite-act": i})
                self.fields[f"slot_{i}_act_{n}"] = field

    def _picked_event_id(self, i: int) -> int | None:
        """The event in slot ``i`` as submitted (or as saved, before submitting)."""
        value = self.data.get(f"slot_{i}") if self.is_bound else self.initial.get(f"slot_{i}")
        value = getattr(value, "pk", value)
        return int(value) if str(value or "").isdigit() else None

    def slots(self) -> list[dict]:
        """For the template: each slot's event field and its act fields."""
        return [{"number": i, "event": self[f"slot_{i}"],
                 "acts": [self[f"slot_{i}_act_{n}"] for n in range(1, self.ACTS + 1)]}
                for i in range(1, FavoriteEvent.MAX + 1)]

    def clean(self):
        cleaned = super().clean()
        chosen = [cleaned.get(f"slot_{i}") for i in range(1, FavoriteEvent.MAX + 1)]
        picked = [e.pk for e in chosen if e]
        if len(picked) != len(set(picked)):
            raise forms.ValidationError("Each favourite must be a different event.")
        acts = {}
        for i, event in enumerate(chosen, start=1):
            lineup = {a["id"] for a in self.lineups.get(event.pk, [])} if event else set()
            ids = []
            for n in range(1, self.ACTS + 1):
                value = str(cleaned.get(f"slot_{i}_act_{n}") or "")
                # Only acts you saw at this festival; anything else (another festival's act, not a
                # festival, picked twice) is dropped.
                if value.isdigit() and int(value) in lineup and int(value) not in ids:
                    ids.append(int(value))
            acts[i] = ids
        artists = Artist.objects.in_bulk([pk for ids in acts.values() for pk in ids])
        for i, ids in acts.items():
            for n in range(1, self.ACTS + 1):
                cleaned[f"slot_{i}_act_{n}"] = artists.get(ids[n - 1]) if n <= len(ids) else None
        return cleaned

    def ordered_favorites(self) -> list[tuple]:
        """[(event, [acts])] in slot order, empty slots skipped."""
        result = []
        for i in range(1, FavoriteEvent.MAX + 1):
            event = self.cleaned_data.get(f"slot_{i}")
            if event:
                acts = [self.cleaned_data.get(f"slot_{i}_act_{n}") for n in range(1, self.ACTS + 1)]
                result.append((event, [a for a in acts if a is not None]))
        return result

    def save(self, user) -> None:
        """Replace the user's favourites with these."""
        from django.db import transaction

        with transaction.atomic():
            FavoriteEvent.objects.filter(user=user).delete()
            FavoriteEvent.objects.bulk_create([
                FavoriteEvent(user=user, event=event, position=i,
                              first_act=(acts + [None, None])[0], second_act=(acts + [None, None])[1])
                for i, (event, acts) in enumerate(self.ordered_favorites(), start=1)
            ])

    @staticmethod
    def initial_for(user) -> dict:
        """The form's starting values: the user's saved favourites."""
        initial = {}
        for i, fav in enumerate(FavoriteEvent.objects.filter(user=user).order_by("position"), start=1):
            initial.update({f"slot_{i}": fav.event_id, f"slot_{i}_act_1": fav.first_act_id or "",
                            f"slot_{i}_act_2": fav.second_act_id or ""})
        return initial


class CommentForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Comment
        fields = ["body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Reply…", "maxlength": 2000})}
        labels = {"body": ""}


class FeedbackForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Feedback
        fields = ["kind", "message"]
        labels = {"kind": "What is it?", "message": "Tell us"}
        widgets = {
            "kind": forms.RadioSelect,
            "message": forms.Textarea(attrs={"rows": 6, "placeholder": "What happened, or what would you like? "
                                                                       "For bugs: what you did and what you expected."}),
        }
