from __future__ import annotations

from django import forms
from django.forms import inlineformset_factory
from django_countries import countries

from .models import (
    Comment,
    Event,
    EventType,
    EventList,
    EventPerformer,
    FavoriteEvent,
    Log,
    Performer,
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
            if isinstance(widget, forms.CheckboxInput):
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
        help_texts = {"production": "Only for festivals: the festival this day belongs to. Tours are set per act in the lineup."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["production"].queryset = Production.objects.filter(event_type=EventType.FESTIVAL)
        self.fields["production"].label = "Festival"
        self.fields["image_display"].required = False
        event = self.instance
        if event.pk and event.event_type == EventType.FESTIVAL and event.production_id and event.date:
            self.fields["photo_for_all_days"].label = (
                f"Use this photo for every day of {event.production.title} in {event.date.year}"
            )
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


class EventPerformerForm(BootstrapMixin, forms.ModelForm):
    new_performer = forms.CharField(
        label="New artist", max_length=200, required=False,
        widget=forms.TextInput(attrs={"placeholder": "…or type a new artist", "class": "form-control form-control-sm mt-1"}),
    )

    class Meta:
        model = EventPerformer
        fields = ["performer", "role", "billing_order", "start_time", "production"]
        labels = {"billing_order": "Order", "start_time": "Time", "production": "Tour"}
        widgets = {"start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].required = False
        self.fields["billing_order"].required = False
        self.fields["performer"].required = False
        tours = self.fields["production"]
        tours.queryset = Production.objects.exclude(event_type=EventType.FESTIVAL).select_related("performer")
        tours.label_from_instance = lambda p: f"{p.title} ({p.performer.name})" if p.performer_id else p.title
        tours.empty_label = "No tour"

    def has_changed(self) -> bool:
        # A new row without a performer (picked or typed) is an unused blank row, whatever role/order say.
        if (not self.instance.pk and not self.data.get(self.add_prefix("performer"))
                and not (self.data.get(self.add_prefix("new_performer")) or "").strip()):
            return False
        return super().has_changed()

    def clean(self):
        cleaned = super().clean()
        name = (cleaned.get("new_performer") or "").strip()
        if cleaned.get("performer") and name:
            self.add_error("new_performer", "Pick an artist or type a new one, not both.")
        elif not cleaned.get("performer") and not name and not cleaned.get("DELETE"):
            self.add_error("performer", "Pick an artist or type a new one.")
        return cleaned

    def save(self, commit: bool = True):
        name = (self.cleaned_data.get("new_performer") or "").strip()
        if name and not self.cleaned_data.get("performer"):
            existing = next(
                (p for p in Performer.objects.filter(name__icontains=name[:3]) if p.name.casefold() == name.casefold()),
                None,
            )
            self.instance.performer = existing or Performer.objects.create(name=name[:200])
        return super().save(commit=commit)

    def clean_role(self) -> str:
        return self.cleaned_data.get("role") or EventPerformer.Role.HEADLINER

    def clean_billing_order(self) -> int:
        return self.cleaned_data.get("billing_order") or 1


EventPerformerFormSet = inlineformset_factory(
    Event,
    EventPerformer,
    form=EventPerformerForm,
    extra=1,
    can_delete=True,
)


class PerformerForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Performer
        fields = ["name", "kind", "country", "bio", "image_url", "mbid"]
        widgets = {"bio": forms.Textarea(attrs={"rows": 4})}
        help_texts = {"mbid": "Optional. Helps match setlist.fm imports to this performer."}


class VenueForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Venue
        fields = ["name", "address", "city", "region", "country", "capacity"]


class ProductionForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Production
        fields = ["title", "event_type", "performer", "description", "image_url"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}
        labels = {"event_type": "Type", "image_url": "Poster / image URL", "performer": "Artist"}
        help_texts = {"performer": "Whose tour or run this is. Leave empty for a festival."}

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
        fields = ["display_name", "location", "home_city", "home_country", "avatar_url", "bio", "thumbnail_display"]
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
    """Four optional slots for a user's favourite events."""

    def __init__(self, *args, events=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if events is None:
            events = favorite_choices(user)
        for i in range(1, FavoriteEvent.MAX + 1):
            self.fields[f"slot_{i}"] = EventChoiceField(
                queryset=events, required=False, label=f"Favourite {i}", empty_label="—"
            )
            self.fields[f"slot_{i}"].widget.attrs["class"] = "form-select"

    def clean(self):
        cleaned = super().clean()
        chosen = [cleaned.get(f"slot_{i}") for i in range(1, FavoriteEvent.MAX + 1)]
        picked = [e.pk for e in chosen if e]
        if len(picked) != len(set(picked)):
            raise forms.ValidationError("Each favourite must be a different event.")
        return cleaned

    def ordered_events(self) -> list:
        return [self.cleaned_data.get(f"slot_{i}") for i in range(1, FavoriteEvent.MAX + 1)
                if self.cleaned_data.get(f"slot_{i}")]


class CommentForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Comment
        fields = ["body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Reply…", "maxlength": 2000})}
        labels = {"body": ""}
