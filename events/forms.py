from __future__ import annotations

from django import forms
from django.forms import inlineformset_factory
from django_countries import countries

from .models import (
    Comment,
    Event,
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
            "start_time",
            "venue",
            "production",
            "description",
            "image_url",
        ]
        widgets = {
            "venue": forms.HiddenInput(attrs={"class": "vs-value"}),
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "description": forms.Textarea(attrs={"rows": 4}),
        }
        labels = {"event_type": "Type", "image_url": "Poster / image URL"}
        help_texts = {"production": "Optional: the tour, run or show this date belongs to."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("venue") and (cleaned.get("new_venue_name") or "").strip():
            self.add_error("new_venue_name", "Pick an existing venue or add a new one, not both.")
        if not (cleaned.get("new_venue_name") or "").strip() and (
            (cleaned.get("new_venue_city") or "").strip() or (cleaned.get("new_venue_country") or "").strip()
        ):
            self.add_error("new_venue_name", "Add the new venue's name.")
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
    class Meta:
        model = EventPerformer
        fields = ["performer", "role", "billing_order"]
        labels = {"billing_order": "Order"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].required = False
        self.fields["billing_order"].required = False

    def has_changed(self) -> bool:
        # A new row without a performer is an unused blank row, whatever role/order say.
        if not self.instance.pk and not self.data.get(self.add_prefix("performer")):
            return False
        return super().has_changed()

    def clean_role(self) -> str:
        return self.cleaned_data.get("role") or EventPerformer.Role.HEADLINER

    def clean_billing_order(self) -> int:
        return self.cleaned_data.get("billing_order") or 1


EventPerformerFormSet = inlineformset_factory(
    Event,
    EventPerformer,
    form=EventPerformerForm,
    extra=3,
    can_delete=True,
)


class PerformerForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Performer
        fields = ["name", "kind", "bio", "image_url", "mbid"]
        widgets = {"bio": forms.Textarea(attrs={"rows": 4})}
        help_texts = {"mbid": "Optional. Helps match setlist.fm imports to this performer."}


class VenueForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Venue
        fields = ["name", "address", "city", "region", "country", "capacity"]


class ProductionForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Production
        fields = ["title", "event_type", "description", "image_url"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}
        labels = {"event_type": "Type", "image_url": "Poster / image URL"}


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


class ProfileForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Profile
        fields = ["display_name", "location", "avatar_url", "bio"]
        widgets = {"bio": forms.Textarea(attrs={"rows": 4})}
        labels = {"avatar_url": "Avatar image URL"}


class FavoritesForm(BootstrapMixin, forms.Form):
    """Four optional slots for a user's favourite events."""

    def __init__(self, *args, events=None, **kwargs):
        super().__init__(*args, **kwargs)
        events = events if events is not None else Event.objects.all()
        for i in range(1, FavoriteEvent.MAX + 1):
            self.fields[f"slot_{i}"] = forms.ModelChoiceField(
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
