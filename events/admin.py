from django.contrib import admin

from .models import (
    Event,
    EventList,
    EventListItem,
    EventArtist,
    Log,
    Artist,
    Production,
    Setlist,
    SetlistSong,
    Song,
    Venue,
    WantToGo,
    Comment,
    FavoriteEvent,
    Follow,
    Profile,
    ReviewLike,
)


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    search_fields = ["name", "city", "country"]
    list_display = ["name", "city", "country", "capacity"]


@admin.register(Artist)
class ArtistAdmin(admin.ModelAdmin):
    search_fields = ["name", "mbid"]
    list_display = ["name", "kind"]
    list_filter = ["kind"]


@admin.register(Production)
class ProductionAdmin(admin.ModelAdmin):
    search_fields = ["title"]
    list_display = ["title", "event_type"]
    list_filter = ["event_type"]


class EventArtistInline(admin.TabularInline):
    model = EventArtist
    extra = 0
    autocomplete_fields = ["artist"]


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    search_fields = ["title", "venue__name", "artists__name"]
    list_display = ["title", "event_type", "date", "venue", "production"]
    list_filter = ["event_type"]
    list_select_related = ["venue", "production"]
    autocomplete_fields = ["venue", "production"]
    inlines = [EventArtistInline]


@admin.register(Log)
class LogAdmin(admin.ModelAdmin):
    search_fields = ["event__title", "user__username"]
    list_display = ["event", "user", "rating", "liked", "created_at"]
    list_select_related = ["event", "user"]


@admin.register(Song)
class SongAdmin(admin.ModelAdmin):
    search_fields = ["title", "artist__name"]
    list_display = ["title", "artist"]
    list_select_related = ["artist"]


class SetlistSongInline(admin.TabularInline):
    model = SetlistSong
    extra = 0
    autocomplete_fields = ["song"]


@admin.register(Setlist)
class SetlistAdmin(admin.ModelAdmin):
    search_fields = ["event__title", "artist__name"]
    list_display = ["event", "artist", "setlistfm_id"]
    list_select_related = ["event", "artist"]
    inlines = [SetlistSongInline]


@admin.register(WantToGo)
class WantToGoAdmin(admin.ModelAdmin):
    search_fields = ["event__title", "user__username"]
    list_display = ["event", "user", "created_at"]
    list_select_related = ["event", "user"]


class EventListItemInline(admin.TabularInline):
    model = EventListItem
    extra = 0
    autocomplete_fields = ["event"]


@admin.register(EventList)
class EventListAdmin(admin.ModelAdmin):
    search_fields = ["title", "user__username"]
    list_display = ["title", "user", "is_ranked", "updated_at"]
    list_select_related = ["user"]
    inlines = [EventListItemInline]


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    search_fields = ["user__username", "display_name"]
    list_display = ["user", "display_name", "location"]
    list_select_related = ["user"]


@admin.register(Follow)
class FollowAdmin(admin.ModelAdmin):
    list_display = ["follower", "following", "created_at"]
    list_select_related = ["follower", "following"]


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    search_fields = ["body", "user__username"]
    list_display = ["user", "log", "created_at"]
    list_select_related = ["user", "log"]


admin.site.register(ReviewLike)
admin.site.register(FavoriteEvent)
