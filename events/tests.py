from __future__ import annotations

import json
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

# Tests never call MusicBrainz for real (the real function is kept for the tests that need it).
from events import musicbrainz as _musicbrainz  # noqa: E402

REAL_ARTIST_COUNTRY = _musicbrainz.artist_country
mock.patch("events.musicbrainz.artist_country", return_value="").start()

from .models import Album, Event, EventArtist, EventType, Log, Artist, Production, Setlist, Venue
from .setlistfm import SetlistFmError, import_setlist
from .templatetags.event_extras import avg_stars, stars

SETLIST_PAYLOAD = {
    "id": "63de4613",
    "eventDate": "10-11-1992",
    "url": "https://www.setlist.fm/setlist/green-day/1992/x.html",
    "artist": {"mbid": "084308bd-1654-436f-ba03-df6697104e19", "name": "Green Day"},
    "sets": {
        "set": [
            {"song": [{"name": "Welcome to Paradise"}, {"name": "2000 Light Years Away", "info": "fast"}]},
            {"encore": 1, "song": [{"name": "Knowledge", "cover": {"name": "Operation Ivy"}}]},
        ]
    },
}


@override_settings(ALLOWED_HOSTS=["testserver"])
class BaseTestCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ana", password="pw-test-123")
        self.venue = Venue.objects.create(name="Teatro Caupolicán", city="Santiago", country="CL")
        self.event = Event.objects.create(
            title="Green Day in Chile", event_type=EventType.MUSIC, venue=self.venue, date=date(1992, 11, 10)
        )


class StarsFilterTests(TestCase):
    def test_stars(self):
        self.assertEqual(stars(None), "")
        self.assertEqual(stars(1), "½")
        self.assertEqual(stars(7), "★★★½")
        self.assertEqual(stars(10), "★★★★★")
        self.assertEqual(avg_stars(7), "3.5")


class SetlistImportTests(BaseTestCase):
    def test_import_creates_artist_lineup_and_songs(self):
        setlist = import_setlist(self.event, SETLIST_PAYLOAD)

        artist = Artist.objects.get(name="Green Day")
        self.assertEqual(artist.mbid, "084308bd-1654-436f-ba03-df6697104e19")
        link = EventArtist.objects.get(event=self.event, artist=artist)
        self.assertEqual(link.role, EventArtist.Role.HEADLINER)

        entries = list(setlist.entries.select_related("song"))
        self.assertEqual([e.song.title for e in entries], ["Welcome to Paradise", "2000 Light Years Away", "Knowledge"])
        self.assertEqual(entries[1].note, "fast")
        self.assertEqual(entries[2].set_name, "Encore")
        self.assertEqual(entries[2].note, "Operation Ivy cover")
        # The catalog is ours: the import never changes the event's venue/date.
        self.event.refresh_from_db()
        self.assertEqual(self.event.venue, self.venue)

    def test_reimport_refreshes_instead_of_duplicating(self):
        import_setlist(self.event, SETLIST_PAYLOAD)
        import_setlist(self.event, SETLIST_PAYLOAD)
        self.assertEqual(Setlist.objects.count(), 1)
        self.assertEqual(Setlist.objects.get().entries.count(), 3)

    def test_matches_existing_artist_by_name(self):
        existing = Artist.objects.create(name="green day")
        import_setlist(self.event, SETLIST_PAYLOAD)
        self.assertEqual(Artist.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.mbid, "084308bd-1654-436f-ba03-df6697104e19")

    def test_same_setlist_cannot_attach_to_two_events(self):
        import_setlist(self.event, SETLIST_PAYLOAD)
        other = Event.objects.create(title="Other show", date=date(1992, 11, 11))
        with self.assertRaises(SetlistFmError):
            import_setlist(other, SETLIST_PAYLOAD)


class PageTests(BaseTestCase):
    def test_public_pages_render(self):
        production = Production.objects.create(title="Kerplunk Tour")
        self.event.production = production
        self.event.save()
        artist = Artist.objects.create(name="Green Day")
        EventArtist.objects.create(event=self.event, artist=artist)
        Log.objects.create(user=self.user, event=self.event, rating=9, review="Loud.")

        for url in [
            reverse("events:home"),
            reverse("events:event_list"),
            reverse("events:event_list") + "?type=music&q=green",
            self.event.get_absolute_url(),
            production.get_absolute_url(),
            artist.get_absolute_url(),
            self.venue.get_absolute_url(),
            reverse("events:user_diary", args=["ana"]),
            reverse("login"),
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

        page = self.client.get(self.event.get_absolute_url())
        self.assertContains(page, "★★★★½")
        self.assertContains(page, "Kerplunk Tour")

    def test_create_pages_require_login(self):
        for name in ["event_create", "artist_create", "venue_create", "production_create", "diary"]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"events:{name}")).status_code, 302)


class LogTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_log_create_update_delete(self):
        url = reverse("events:log_save", args=[self.event.pk])
        self.client.post(url, {"rating": "8", "review": "Great", "liked": "on"})
        log = Log.objects.get(user=self.user, event=self.event)
        self.assertEqual((log.rating, log.liked), (8, True))

        self.client.post(url, {"rating": "", "review": "Changed my mind"})
        log.refresh_from_db()
        self.assertEqual((log.rating, log.review, log.liked), (None, "Changed my mind", False))
        self.assertEqual(Log.objects.count(), 1)

        self.client.post(reverse("events:log_delete", args=[self.event.pk]))
        self.assertFalse(Log.objects.exists())

    def test_diary_lists_logs(self):
        Log.objects.create(user=self.user, event=self.event, rating=10)
        page = self.client.get(reverse("events:diary"), follow=True)
        self.assertContains(page, "Green Day in Chile")
        self.assertContains(page, "★★★★★")


class EventFormTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.artist = Artist.objects.create(name="Bill Burr")

    def _payload(self, **extra):
        data = {
            "title": "Bill Burr Live",
            "event_type": "comedy",
            "date": "2026-12-01",
            "venue": str(self.venue.pk),
            "eventartist_set-TOTAL_FORMS": "3",
            "eventartist_set-INITIAL_FORMS": "0",
            "eventartist_set-MIN_NUM_FORMS": "0",
            "eventartist_set-MAX_NUM_FORMS": "1000",
            "eventartist_set-0-artist": str(self.artist.pk),
            "eventartist_set-0-role": "headliner",
            "eventartist_set-0-billing_order": "1",
        }
        data.update(extra)
        return data

    def test_create_event_with_lineup(self):
        response = self.client.post(reverse("events:event_create"), self._payload())
        event = Event.objects.get(title="Bill Burr Live")
        self.assertRedirects(response, event.get_absolute_url())
        self.assertEqual(event.created_by, self.user)
        self.assertEqual(list(event.artists.all()), [self.artist])

    def test_only_creator_can_edit(self):
        self.client.post(reverse("events:event_create"), self._payload())
        event = Event.objects.get(title="Bill Burr Live")
        other = get_user_model().objects.create_user("bob", password="pw-test-456")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("events:event_edit", args=[event.pk])).status_code, 403)


@override_settings(ALLOWED_HOSTS=["testserver"])
class SetlistViewTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    @mock.patch("events.views.get_setlist", return_value=SETLIST_PAYLOAD)
    def test_import_view(self, _):
        response = self.client.post(
            reverse("events:setlist_import", args=[self.event.pk]), {"setlist_id": "63de4613"}
        )
        self.assertRedirects(response, self.event.get_absolute_url())
        setlist = self.event.setlists.get()
        page = self.client.get(reverse("events:act_setlist", args=[self.event.pk, setlist.artist_id]))
        self.assertContains(page, "Welcome to Paradise")
        self.assertContains(page, "Encore")

    @mock.patch("events.views.search_setlists", side_effect=SetlistFmError("Setlist.fm error 403: Forbidden"))
    def test_search_shows_api_errors(self, _):
        page = self.client.get(reverse("events:setlist_search", args=[self.event.pk]) + "?artist=Green+Day")
        self.assertContains(page, "Setlist.fm error 403")


# --------------------------------------------------------------------------- Phase 2: discovery

from .models import EventList, EventListItem, WantToGo  # noqa: E402


class WantToGoTests(BaseTestCase):
    def test_toggle_and_profile_page(self):
        from datetime import timedelta

        from django.utils import timezone

        upcoming = Event.objects.create(title="Next tour", date=timezone.localdate() + timedelta(days=60))
        self.client.force_login(self.user)
        url = reverse("events:want_to_go_toggle", args=[upcoming.pk])
        self.client.post(url)
        self.assertTrue(WantToGo.objects.filter(user=self.user, event=upcoming).exists())
        self.assertContains(self.client.get(upcoming.get_absolute_url()), "In your Want to go")

        page = self.client.get(reverse("events:user_want_to_go", args=["ana"]))
        self.assertContains(page, "Coming up")
        self.assertContains(page, "Next tour")

        self.client.post(url)
        self.assertFalse(WantToGo.objects.exists())

    def test_past_and_undated_events_cannot_be_wanted(self):
        self.client.force_login(self.user)
        undated = Event.objects.create(title="Someday")
        for event in [self.event, undated]:
            with self.subTest(event=event.title):
                self.assertNotContains(self.client.get(event.get_absolute_url()), "+ Want to go")
                response = self.client.post(reverse("events:want_to_go_toggle", args=[event.pk]), follow=True)
                self.assertContains(response, "only for upcoming events")
        self.assertFalse(WantToGo.objects.exists())

    def test_saved_event_that_already_happened_can_still_be_removed(self):
        self.client.force_login(self.user)
        WantToGo.objects.create(user=self.user, event=self.event)  # saved before the show
        self.assertContains(self.client.get(self.event.get_absolute_url()), "Remove from Want to go")
        self.assertContains(self.client.get(reverse("events:user_want_to_go", args=["ana"])), "Missed it")
        self.client.post(reverse("events:want_to_go_toggle", args=[self.event.pk]))
        self.assertFalse(WantToGo.objects.exists())

    def test_toggle_requires_login(self):
        self.client.post(reverse("events:want_to_go_toggle", args=[self.event.pk]))
        self.assertFalse(WantToGo.objects.exists())


class ListTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.e2 = Event.objects.create(title="Hamlet", event_type=EventType.THEATER, date=date(2025, 5, 1))
        self.e3 = Event.objects.create(title="Bill Burr", event_type=EventType.COMEDY, date=date(2024, 3, 1))

    def _add(self, event, **data):
        return self.client.post(reverse("events:list_add_event", args=[event.pk]), data)

    def test_create_list_from_event_page_then_add_more(self):
        self._add(self.event, new_list_title="Best of all time", note="Unreal")
        event_list = EventList.objects.get(title="Best of all time")
        self._add(self.e2, list_id=event_list.pk)
        self._add(self.e3, list_id=event_list.pk)
        self._add(self.e3, list_id=event_list.pk)  # duplicate is ignored
        items = list(event_list.items.values_list("event__title", "position", "note"))
        self.assertEqual(
            items, [("Green Day in Chile", 1, "Unreal"), ("Hamlet", 2, ""), ("Bill Burr", 3, "")]
        )

        page = self.client.get(event_list.get_absolute_url())
        self.assertContains(page, "Unreal")
        self.assertContains(self.client.get(self.event.get_absolute_url()), "Best of all time")

    def test_reorder_and_remove(self):
        event_list = EventList.objects.create(user=self.user, title="Ranked", is_ranked=True)
        items = [EventListItem.objects.create(event_list=event_list, event=e, position=i)
                 for i, e in enumerate([self.event, self.e2, self.e3], start=1)]

        self.client.post(reverse("events:list_item_move", args=[event_list.pk, items[2].pk]), {"direction": "up"})
        self.assertEqual(list(event_list.items.values_list("event__title", flat=True)),
                         ["Green Day in Chile", "Bill Burr", "Hamlet"])

        self.client.post(reverse("events:list_item_remove", args=[event_list.pk, items[0].pk]))
        self.assertEqual(list(event_list.items.values_list("event__title", "position")),
                         [("Bill Burr", 1), ("Hamlet", 2)])

    def test_cannot_change_someone_elses_list(self):
        other = get_user_model().objects.create_user("bob", password="pw-test-456")
        theirs = EventList.objects.create(user=other, title="Bob's list")
        self.assertEqual(self._add(self.event, list_id=theirs.pk).status_code, 403)
        self.assertEqual(self.client.get(reverse("events:list_edit", args=[theirs.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("events:list_delete", args=[theirs.pk])).status_code, 403)
        self.assertFalse(theirs.items.exists())

    def test_list_pages_render(self):
        event_list = EventList.objects.create(user=self.user, title="Comedy")
        EventListItem.objects.create(event_list=event_list, event=self.e3, position=1)
        for url in [
            reverse("events:list_index"),
            reverse("events:user_lists", args=["ana"]),
            reverse("events:list_create"),
            reverse("events:list_edit", args=[event_list.pk]),
            reverse("events:home"),
        ]:
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertEqual(page.status_code, 200)
        self.assertContains(self.client.get(reverse("events:list_index")), "Comedy")

    def test_edit_and_delete_list(self):
        event_list = EventList.objects.create(user=self.user, title="Old")
        self.client.post(reverse("events:list_edit", args=[event_list.pk]), {"title": "New", "is_ranked": "on"})
        event_list.refresh_from_db()
        self.assertEqual((event_list.title, event_list.is_ranked), ("New", True))
        self.client.post(reverse("events:list_delete", args=[event_list.pk]))
        self.assertFalse(EventList.objects.exists())


class SearchAndBrowseTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        artist = Artist.objects.create(name="Green Day")
        EventArtist.objects.create(event=self.event, artist=artist)
        Production.objects.create(title="Green Room Sessions")
        self.hamlet = Event.objects.create(
            title="Hamlet", event_type=EventType.THEATER, date=date(2030, 1, 1),
            venue=Venue.objects.create(name="GAM", city="Santiago"),
        )
        self.lima = Event.objects.create(
            title="Lima show", event_type=EventType.MUSIC, date=date(2020, 6, 1),
            venue=Venue.objects.create(name="Arena", city="Lima"),
        )
        Log.objects.create(user=self.user, event=self.lima, rating=10)

    def test_search_groups_results(self):
        page = self.client.get(reverse("events:search") + "?q=green")
        self.assertContains(page, "Green Day in Chile")
        self.assertContains(page, "Artists")
        self.assertContains(page, "Green Room Sessions")
        self.assertContains(page, "Teatro Caupolicán", count=2)  # event card only (text + hover title), not under Venues
        self.assertContains(self.client.get(reverse("events:search") + "?q=zzz"), "Nothing found")

    def _titles(self, query):
        page = self.client.get(reverse("events:event_list") + query)
        return [e.title for e in page.context["page"].object_list]

    def test_filters_and_sorting(self):
        self.assertEqual(self._titles("?type=theater"), ["Hamlet"])
        self.assertEqual(self._titles("?when=upcoming"), ["Hamlet"])
        self.assertEqual(self._titles("?city=santiago&sort=oldest"), ["Green Day in Chile", "Hamlet"])
        self.assertEqual(self._titles("?year=2020"), ["Lima show"])
        self.assertEqual(self._titles("?sort=rating")[0], "Lima show")
        self.assertEqual(self._titles("?sort=newest"), ["Hamlet", "Lima show", "Green Day in Chile"])
        self.assertEqual(self._titles("?q=green+day"), ["Green Day in Chile"])


# --------------------------------------------------------------------------- Phase 3: social

from types import SimpleNamespace  # noqa: E402

from .models import Comment, FavoriteEvent, Follow, Profile, ReviewLike  # noqa: E402
from .signals import fill_profile_from_social  # noqa: E402


class SocialBase(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.bob = get_user_model().objects.create_user("bob", password="pw-test-456")
        self.log = Log.objects.create(user=self.user, event=self.event, rating=9, review="So loud.")


class ProfileTests(SocialBase):
    def test_profile_created_for_new_users(self):
        self.assertTrue(Profile.objects.filter(user=self.bob).exists())

    def test_google_name_and_photo_fill_empty_profile(self):
        account = SimpleNamespace(extra_data={"name": "Bob Builder", "picture": "https://lh3.example/p.jpg"})
        fill_profile_from_social(self.bob, SimpleNamespace(account=account))
        self.bob.profile.refresh_from_db()
        self.assertEqual(self.bob.profile.display_name, "Bob Builder")
        self.assertEqual(self.bob.profile.avatar_url, "https://lh3.example/p.jpg")

    def test_profile_pages_render(self):
        for name in ["profile", "user_diary", "user_reviews", "user_want_to_go", "user_lists", "followers", "following"]:
            with self.subTest(name=name):
                page = self.client.get(reverse(f"events:{name}", args=["ana"]))
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, "@ana")
        self.assertContains(self.client.get(reverse("events:user_reviews", args=["ana"])), "So loud.")
        self.assertEqual(self.client.get(reverse("events:people")).status_code, 200)

    def test_edit_profile_and_favorites(self):
        self.client.force_login(self.user)
        hamlet = Event.objects.create(title="Hamlet", event_type=EventType.THEATER)
        for e in [hamlet, self.event]:
            Log.objects.get_or_create(user=self.user, event=e)  # favourites come from your diary
        response = self.client.post(reverse("events:profile_edit"), {
            "username": "ana", "display_name": "Ana M", "location": "Santiago", "bio": "Front row.",
            "slot_1": str(hamlet.pk), "slot_2": "", "slot_3": str(self.event.pk), "slot_4": "",
        })
        self.assertRedirects(response, reverse("events:profile", args=["ana"]))
        self.assertEqual(
            list(FavoriteEvent.objects.filter(user=self.user).values_list("event__title", "position")),
            [("Hamlet", 1), ("Green Day in Chile", 2)],
        )
        page = self.client.get(reverse("events:profile", args=["ana"]))
        self.assertContains(page, "Ana M")
        self.assertContains(page, "Front row.")

    def test_duplicate_favorites_rejected(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("events:profile_edit"), {
            "username": "ana", "slot_1": str(self.event.pk), "slot_2": str(self.event.pk),
        })
        self.assertContains(response, "different event")
        self.assertFalse(FavoriteEvent.objects.exists())


class FestivalFavoriteActsTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.lolla = Event.objects.create(title="Lollapalooza Chile", event_type=EventType.FESTIVAL,
                                          date=date(2013, 4, 7))
        self.acts = {}
        for order, (name, role) in enumerate([("Pearl Jam", "headliner"), ("The Black Keys", "headliner"),
                                              ("Alabama Shakes", "support"), ("Foals", "support")], start=1):
            self.acts[name] = Artist.objects.create(name=name)
            EventArtist.objects.create(event=self.lolla, artist=self.acts[name], role=role, billing_order=order)
        self.other_fest = Event.objects.create(title="Primavera", event_type=EventType.FESTIVAL, date=date(2014, 5, 1))
        self.stranger = Artist.objects.create(name="Not At Lolla")
        EventArtist.objects.create(event=self.other_fest, artist=self.stranger)
        for e in [self.lolla, self.other_fest, self.event]:
            Log.objects.create(user=self.user, event=e)
        self.client.force_login(self.user)

    def _save(self, **slots):
        return self.client.post(reverse("events:profile_edit"), {"username": "ana", **slots})

    def test_pick_two_acts_for_a_festival_favourite(self):
        page = self.client.get(reverse("events:profile_edit"))
        lineups = page.context["favorites_form"].lineups
        self.assertEqual([a["name"] for a in lineups[self.lolla.pk]],
                         ["Pearl Jam", "The Black Keys", "Alabama Shakes", "Foals"])
        self.assertNotIn(self.event.pk, lineups)  # not a festival: no act dropdowns
        self.assertContains(page, 'id="favorite-lineups"')
        self._save(slot_1=self.lolla.pk, slot_1_act_1=self.acts["Foals"].pk,
                   slot_1_act_2=self.acts["Alabama Shakes"].pk, slot_2=self.event.pk)
        fav = FavoriteEvent.objects.get(user=self.user, event=self.lolla)
        self.assertEqual((fav.first_act, fav.second_act), (self.acts["Foals"], self.acts["Alabama Shakes"]))
        profile = self.client.get(reverse("events:profile", args=["ana"]))
        self.assertContains(profile, "Foals · Alabama Shakes</div>")
        # Only "Recently attended" still shows the headliners; the favourite card shows the picks.
        self.assertContains(profile, "Pearl Jam · The Black Keys", count=1)
        # The edit page remembers them.
        form = self.client.get(reverse("events:profile_edit")).context["favorites_form"]
        self.assertEqual(form["slot_1_act_1"].value(), self.acts["Foals"].pk)

    def test_only_acts_they_saw_can_be_picked(self):
        log = Log.objects.get(user=self.user, event=self.lolla)
        log.acts.filter(artist=self.acts["Foals"]).delete()  # missed Foals
        lineups = self.client.get(reverse("events:profile_edit")).context["favorites_form"].lineups
        self.assertEqual([a["name"] for a in lineups[self.lolla.pk]], ["Pearl Jam", "The Black Keys", "Alabama Shakes"])
        self._save(slot_1=self.lolla.pk, slot_1_act_1=self.acts["Foals"].pk,
                   slot_1_act_2=self.acts["Pearl Jam"].pk)
        fav = FavoriteEvent.objects.get(user=self.user, event=self.lolla)
        self.assertEqual((fav.first_act, fav.second_act), (self.acts["Pearl Jam"], None))

    def test_without_picks_the_card_shows_the_headliners(self):
        self._save(slot_1=self.lolla.pk)
        self.assertContains(self.client.get(reverse("events:profile", args=["ana"])), "Pearl Jam · The Black Keys</div>")

    def test_acts_not_on_that_festival_or_repeated_are_dropped(self):
        self._save(slot_1=self.lolla.pk, slot_1_act_1=self.stranger.pk, slot_1_act_2=self.acts["Foals"].pk,
                   slot_2=self.event.pk, slot_2_act_1=self.acts["Foals"].pk,
                   slot_3=self.other_fest.pk, slot_3_act_1=self.stranger.pk, slot_3_act_2=self.stranger.pk)
        favs = {f.event_id: f for f in FavoriteEvent.objects.filter(user=self.user)}
        # The act from another festival is dropped, so Foals moves up.
        self.assertEqual((favs[self.lolla.pk].first_act, favs[self.lolla.pk].second_act), (self.acts["Foals"], None))
        self.assertIsNone(favs[self.event.pk].first_act)  # concerts don't pick acts
        self.assertEqual((favs[self.other_fest.pk].first_act, favs[self.other_fest.pk].second_act),
                         (self.stranger, None))
        self.assertEqual(favs[self.lolla.pk].chosen_acts, ["Foals"])


class FollowTests(SocialBase):
    def test_follow_unfollow_and_no_self_follow(self):
        self.client.force_login(self.bob)
        url = reverse("events:follow_toggle", args=["ana"])
        self.client.post(url)
        self.assertTrue(Follow.objects.filter(follower=self.bob, following=self.user).exists())
        page = self.client.get(reverse("events:profile", args=["ana"]))
        self.assertContains(page, "Following ✓")

        self.client.post(url)
        self.assertFalse(Follow.objects.exists())

        self.client.post(reverse("events:follow_toggle", args=["bob"]))
        self.assertFalse(Follow.objects.exists())

    def test_follows_you_badge_and_lists(self):
        Follow.objects.create(follower=self.user, following=self.bob)
        self.client.force_login(self.bob)
        self.assertContains(self.client.get(reverse("events:profile", args=["ana"])), "Follows you")
        self.assertContains(self.client.get(reverse("events:followers", args=["bob"])), "@ana")


class ReviewTests(SocialBase):
    def test_like_and_comment(self):
        self.client.force_login(self.bob)
        self.client.post(reverse("events:review_like_toggle", args=[self.log.pk]))
        self.client.post(reverse("events:comment_add", args=[self.log.pk]), {"body": "Was there too!"})
        self.client.post(reverse("events:comment_add", args=[self.log.pk]), {"body": "   "})
        self.assertEqual(ReviewLike.objects.count(), 1)
        self.assertEqual(Comment.objects.count(), 1)

        page = self.client.get(reverse("events:review_detail", args=[self.log.pk]))
        self.assertContains(page, "Was there too!")
        self.assertContains(page, "1 like")
        self.assertContains(self.client.get(self.event.get_absolute_url()), "1 reply")

        self.client.post(reverse("events:review_like_toggle", args=[self.log.pk]))
        self.assertFalse(ReviewLike.objects.exists())

    def test_comment_delete_permissions(self):
        comment = Comment.objects.create(user=self.bob, log=self.log, body="hi")
        stranger = get_user_model().objects.create_user("eve", password="pw-test-789")
        self.client.force_login(stranger)
        url = reverse("events:comment_delete", args=[self.log.pk, comment.pk])
        self.assertEqual(self.client.post(url).status_code, 403)
        # The review's author may remove replies on their review.
        self.client.force_login(self.user)
        self.client.post(url)
        self.assertFalse(Comment.objects.exists())


class ActivityTests(SocialBase):
    def test_feed_shows_followed_users_only(self):
        eve = get_user_model().objects.create_user("eve", password="pw-test-789")
        Log.objects.create(user=eve, event=Event.objects.create(title="Secret show"))
        Follow.objects.create(follower=self.bob, following=self.user)
        WantToGo.objects.create(user=self.user, event=Event.objects.create(title="Future gig"))
        EventList.objects.create(user=self.user, title="Faves")

        self.client.force_login(self.bob)
        page = self.client.get(reverse("events:activity"))
        kinds = [item.kind for item in page.context["items"]]
        self.assertEqual(sorted(kinds), ["list", "log", "want"])
        self.assertContains(page, "reviewed")
        self.assertNotContains(page, "Secret show")
        self.assertContains(self.client.get(reverse("events:home")), "From people you follow")

        mine = self.client.get(reverse("events:activity") + "?show=you")
        self.assertEqual([i.kind for i in mine.context["items"]], ["follow"])

    def test_empty_feed_suggests_people(self):
        self.client.force_login(self.bob)
        page = self.client.get(reverse("events:activity"))
        self.assertContains(page, "People to follow")
        self.assertContains(page, "@ana")


# --------------------------------------------------------------------------- auto titles + admin role

from django.contrib.auth.models import Group  # noqa: E402
from django.core.management import call_command  # noqa: E402

from .roles import ADMINS_GROUP, is_admin  # noqa: E402


class AutoTitleTests(BaseTestCase):
    def test_blank_title_is_the_headliners_name(self):
        event = Event.objects.create(title="", venue=self.venue, date=date(2010, 11, 10))
        self.assertTrue(event.title_is_auto)
        self.assertEqual(event.title, "Music event")

        green_day = Artist.objects.create(name="Green Day")
        EventArtist.objects.create(event=event, artist=green_day)
        event.refresh_from_db()
        self.assertEqual(event.title, "Green Day")

        # The venue and date stay out of the title.
        self.venue.name = "Estadio Nacional"
        self.venue.save()
        event.refresh_from_db()
        self.assertEqual(event.title, "Green Day")

    def test_production_is_fallback_without_lineup(self):
        event = Event.objects.create(title="", production=Production.objects.create(title="Hamilton"))
        self.assertEqual(event.title, "Hamilton")

    def test_card_shows_title_then_venue_and_date(self):
        auto = Event.objects.create(title="", venue=self.venue, date=date(2019, 3, 29),
                                    image_url="https://example.com/poster.jpg")
        EventArtist.objects.create(event=auto, artist=Artist.objects.create(name="Paul McCartney"))
        html = self.client.get(reverse("events:event_list")).content.decode()
        self.assertIn("Paul McCartney</a>", html)
        # Date on its own line (never cut off), venue below it.
        self.assertIn("29 Mar 2019", html)
        self.assertIn("10 Nov 1992", html)
        self.assertIn('title="Teatro Caupolicán">Teatro Caupolicán</div>', html)
        undated = Event.objects.create(title="Someday")
        self.assertContains(self.client.get(reverse("events:event_list")), "Date not added")
        self.assertTrue(undated.pk)
        # Real posters are not covered by the title text.
        self.assertNotIn('class="poster-title"', html)

    def test_custom_title_is_kept(self):
        self.assertFalse(self.event.title_is_auto)
        EventArtist.objects.create(event=self.event, artist=Artist.objects.create(name="X"))
        self.event.refresh_from_db()
        self.assertEqual(self.event.title, "Green Day in Chile")

    def test_falls_back_without_city_or_date(self):
        event = Event.objects.create(title="", event_type=EventType.COMEDY)
        self.assertEqual(event.title, "Comedy event")

    def test_form_creates_auto_title_and_keeps_it_auto_on_edit(self):
        self.client.force_login(self.user)
        artist = Artist.objects.create(name="Bill Burr")
        data = {
            "title": "", "event_type": "comedy", "date": "2026-12-01", "venue": str(self.venue.pk),
            "eventartist_set-TOTAL_FORMS": "1", "eventartist_set-INITIAL_FORMS": "0",
            "eventartist_set-MIN_NUM_FORMS": "0", "eventartist_set-MAX_NUM_FORMS": "1000",
            "eventartist_set-0-artist": str(artist.pk),
            "eventartist_set-0-role": "headliner", "eventartist_set-0-billing_order": "1",
        }
        self.client.post(reverse("events:event_create"), data)
        event = Event.objects.get(artists=artist)
        self.assertEqual(event.title, "Bill Burr")

        page = self.client.get(reverse("events:event_edit", args=[event.pk]))
        self.assertContains(page, "Automatic: Bill Burr")

        link = event.eventartist_set.get()
        edit = dict(data, date="2026-12-02", **{
            "eventartist_set-INITIAL_FORMS": "1", "eventartist_set-0-id": str(link.pk),
            "eventartist_set-0-event": str(event.pk),
        })
        self.client.post(reverse("events:event_edit", args=[event.pk]), edit)
        event.refresh_from_db()
        self.assertEqual(event.title, "Bill Burr")

        self.client.post(reverse("events:event_edit", args=[event.pk]), dict(edit, title="Burr Live"))
        event.refresh_from_db()
        self.assertEqual((event.title, event.title_is_auto), ("Burr Live", False))


class AdminRoleTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_user("mod", password="pw-test-000")
        call_command("make_admin", "mod", stdout=open("/dev/null", "w"))
        self.event.created_by = self.user
        self.event.save()
        Log.objects.create(user=self.user, event=self.event, rating=8)

    def test_admins_group_and_command(self):
        self.assertTrue(Group.objects.filter(name=ADMINS_GROUP).exists())
        self.assertTrue(is_admin(get_user_model().objects.get(username="mod")))
        self.assertFalse(is_admin(self.user))

    def test_regular_users_cannot_delete_even_their_own_events(self):
        self.client.force_login(self.user)
        page = self.client.get(self.event.get_absolute_url())
        self.assertContains(page, "Edit event")
        self.assertNotContains(page, "Delete event")
        self.assertEqual(self.client.post(reverse("events:event_delete", args=[self.event.pk])).status_code, 403)
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())

    def test_admin_can_edit_and_delete_any_event(self):
        self.client.force_login(self.admin)
        page = self.client.get(self.event.get_absolute_url())
        self.assertContains(page, "Delete event")
        self.assertEqual(self.client.get(reverse("events:event_edit", args=[self.event.pk])).status_code, 200)

        confirm = self.client.get(reverse("events:event_delete", args=[self.event.pk]))
        self.assertContains(confirm, "1 diary log")
        response = self.client.post(reverse("events:event_delete", args=[self.event.pk]))
        self.assertRedirects(response, reverse("events:event_list"))
        self.assertFalse(Event.objects.filter(pk=self.event.pk).exists())
        self.assertFalse(Log.objects.exists())

    def test_admin_badge_and_removing_role(self):
        self.assertContains(self.client.get(reverse("events:profile", args=["mod"])), "Admin")
        call_command("make_admin", "mod", "--remove", stdout=open("/dev/null", "w"))
        self.assertFalse(is_admin(get_user_model().objects.get(username="mod")))


class SuperuserBootstrapTests(TestCase):
    @override_settings(DJANGO_SUPERUSER_EMAILS=["boss@example.com"])
    def test_configured_email_becomes_superuser_on_login(self):
        boss = get_user_model().objects.create_user("boss", email="Boss@Example.com", password="pw-test-1")
        other = get_user_model().objects.create_user("other", email="o@example.com", password="pw-test-2")
        self.client.login(username="boss", password="pw-test-1")
        self.client.login(username="other", password="pw-test-2")
        boss.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(boss.is_superuser and boss.is_staff)
        self.assertTrue(is_admin(boss))
        self.assertFalse(other.is_superuser)


# --------------------------------------------------------------------------- catalog editing + event page polish


class CatalogEditTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.artist = Artist.objects.create(name="Paul McCartny")
        self.production = Production.objects.create(title="Got Back Tour")
        self.admin = get_user_model().objects.create_user("mod", password="pw-test-000")
        call_command("make_admin", "mod", stdout=open("/dev/null", "w"))

    def test_regular_users_cannot_edit_catalog(self):
        self.client.force_login(self.user)
        for name, obj in [("artist", self.artist), ("venue", self.venue), ("production", self.production)]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"events:{name}_edit", args=[obj.pk])).status_code, 403)
                self.assertNotContains(self.client.get(obj.get_absolute_url()), f"Edit {name}")

    def test_admin_edits_artist_and_venue(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.artist.get_absolute_url()), "Edit artist")
        response = self.client.post(
            reverse("events:artist_edit", args=[self.artist.pk]), {"name": "Paul McCartney", "kind": "person"}
        )
        self.assertRedirects(response, self.artist.get_absolute_url())
        self.artist.refresh_from_db()
        self.assertEqual(self.artist.name, "Paul McCartney")

        self.client.post(
            reverse("events:venue_edit", args=[self.venue.pk]),
            {"name": "Estadio Nacional", "city": "Santiago", "country": "CL"},
        )
        self.venue.refresh_from_db()
        self.assertEqual(self.venue.name, "Estadio Nacional")


class EventPagePolishTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _payload(self, **extra):
        data = {
            "title": "", "event_type": "music", "date": "",
            "eventartist_set-TOTAL_FORMS": "0", "eventartist_set-INITIAL_FORMS": "0",
            "eventartist_set-MIN_NUM_FORMS": "0", "eventartist_set-MAX_NUM_FORMS": "1000",
        }
        data.update(extra)
        return data

    def test_new_venue_created_from_event_form(self):
        self.client.post(reverse("events:event_create"), self._payload(
            date="2026-03-20", new_venue_name="Estadio Nacional", new_venue_city="Santiago", new_venue_country="CL",
        ))
        event = Event.objects.get(venue__name="Estadio Nacional")
        self.assertEqual(event.venue.country, "CL")
        self.assertEqual(event.title, "Music event")

        # Same name + city again reuses the venue instead of duplicating it.
        self.client.post(reverse("events:event_create"), self._payload(
            new_venue_name="estadio nacional", new_venue_city="Santiago",
        ))
        self.assertEqual(Venue.objects.filter(name__iexact="estadio nacional").count(), 1)

    def test_cannot_pick_and_add_venue_at_once(self):
        page = self.client.post(reverse("events:event_create"), self._payload(
            venue=str(self.venue.pk), new_venue_name="Other",
        ))
        self.assertContains(page, "not both")
        self.assertEqual(Event.objects.count(), 1)

    def test_detail_shows_venue_date_or_placeholders(self):
        page = self.client.get(self.event.get_absolute_url())
        self.assertContains(page, "Teatro Caupolicán</a>, Santiago")
        self.assertContains(page, "10 November 1992")

        bare = Event.objects.create(title="Paul McCartney", created_by=self.user)
        page = self.client.get(bare.get_absolute_url())
        self.assertContains(page, "Venue not added")
        self.assertContains(page, "Date not added")
        self.assertContains(page, "Add venue &amp; date")

    def test_undated_events_appear_on_home(self):
        Event.objects.create(title="Paul McCartney")
        page = self.client.get(reverse("events:home"))
        self.assertContains(page, "Just added")
        self.assertContains(page, "Paul McCartney")



# --------------------------------------------------------------------------- Google Places venue search

from . import google_places  # noqa: E402

ESTADIO_DETAILS = {
    "id": "ChIJ_estadio",
    "displayName": {"text": "Estadio Nacional Julio Martínez Prádanos", "languageCode": "es"},
    "formattedAddress": "Av. Grecia 2001, 7750000 Ñuñoa, Región Metropolitana, Chile",
    "location": {"latitude": -33.4645234, "longitude": -70.6107442},
    "addressComponents": [
        {"longText": "2001", "shortText": "2001", "types": ["street_number"]},
        {"longText": "Ñuñoa", "shortText": "Ñuñoa", "types": ["locality", "political"]},
        {"longText": "Santiago", "shortText": "Santiago", "types": ["administrative_area_level_2", "political"]},
        {"longText": "Región Metropolitana", "shortText": "RM", "types": ["administrative_area_level_1", "political"]},
        {"longText": "Chile", "shortText": "CL", "types": ["country", "political"]},
    ],
}
SUGGESTIONS = [
    {"place_id": "ChIJ_estadio", "main": "Estadio Nacional", "secondary": "Ñuñoa, Chile"},
    {"place_id": "ChIJ_other", "main": "Estadio Nacional de Lima", "secondary": "Lima, Perú"},
]


@override_settings(GOOGLE_PLACES_API_KEY="test-key", ALLOWED_HOSTS=["testserver"])
class GooglePlacesTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_venue_fields_are_standardized(self):
        fields = google_places.venue_fields(ESTADIO_DETAILS)
        self.assertEqual(fields["name"], "Estadio Nacional Julio Martínez Prádanos")
        self.assertEqual(fields["city"], "Ñuñoa")
        self.assertEqual(fields["region"], "Región Metropolitana")
        self.assertEqual(fields["country"], "CL")
        self.assertEqual(str(fields["latitude"]), "-33.464523")

    @mock.patch("events.google_places.autocomplete", return_value=SUGGESTIONS)
    def test_search_returns_local_then_google(self, autocomplete):
        Venue.objects.create(name="Estadio Nacional", city="Ñuñoa", google_place_id="ChIJ_estadio")
        data = self.client.get(reverse("events:venue_search"), {"q": "estadio", "session": "s1"}).json()
        self.assertEqual([v["name"] for v in data["local"]], ["Estadio Nacional"])
        # A place we already have isn't offered again from Google.
        self.assertEqual([p["place_id"] for p in data["google"]], ["ChIJ_other"])
        autocomplete.assert_called_once_with("estadio", "s1")

    def test_search_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("events:venue_search"), {"q": "estadio"}).status_code, 302)

    @override_settings(GOOGLE_PLACES_API_KEY="")
    def test_search_without_key_still_finds_local_venues(self):
        data = self.client.get(reverse("events:venue_search"), {"q": "caupol"}).json()
        self.assertFalse(data["google_enabled"])
        self.assertEqual(data["local"][0]["id"], self.venue.pk)

    @mock.patch("events.google_places.place_details", return_value=ESTADIO_DETAILS)
    def test_pick_creates_venue_once(self, details):
        url = reverse("events:venue_from_place")
        first = self.client.post(url, {"place_id": "ChIJ_estadio", "session": "s1"}).json()
        second = self.client.post(url, {"place_id": "ChIJ_estadio", "session": "s2"}).json()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(details.call_count, 1)  # the second pick reuses our venue, no extra Google call
        venue = Venue.objects.get(pk=first["id"])
        self.assertEqual((venue.city, venue.country.code), ("Ñuñoa", "CL"))

    @mock.patch("events.google_places.place_details", return_value=ESTADIO_DETAILS)
    def test_pick_standardizes_matching_hand_typed_venue(self, _):
        typed = Venue.objects.create(name="Estadio Nacional Julio Martínez Prádanos", city="ñuñoa")
        data = self.client.post(reverse("events:venue_from_place"), {"place_id": "ChIJ_estadio"}).json()
        self.assertEqual(data["id"], typed.pk)
        typed.refresh_from_db()
        self.assertEqual((typed.google_place_id, typed.region), ("ChIJ_estadio", "Región Metropolitana"))

    @mock.patch("events.google_places.place_details", return_value=ESTADIO_DETAILS)
    def test_admin_links_existing_venue_and_auto_titles_follow(self, _):
        event = Event.objects.create(title="", venue=self.venue, date=date(2019, 3, 29))
        url = reverse("events:venue_link_place", args=[self.venue.pk])
        self.assertEqual(self.client.post(url, {"place_id": "ChIJ_estadio"}).status_code, 403)

        admin = get_user_model().objects.create_user("mod", password="pw-test-000")
        call_command("make_admin", "mod", stdout=open("/dev/null", "w"))
        self.client.force_login(admin)
        response = self.client.post(url, {"place_id": "ChIJ_estadio"})
        self.assertRedirects(response, self.venue.get_absolute_url())
        self.venue.refresh_from_db()
        event.refresh_from_db()
        self.assertEqual(self.venue.city, "Ñuñoa")
        self.assertEqual(event.title, "Music event")

    def test_google_errors_are_reported_not_raised(self):
        with mock.patch("events.google_places.autocomplete",
                        side_effect=google_places.PlacesError("Google Places error 403: API key not valid")):
            data = self.client.get(reverse("events:venue_search"), {"q": "estadio"}).json()
        self.assertIn("API key not valid", data["error"])

    def test_event_form_shows_search_box(self):
        page = self.client.get(reverse("events:event_create"))
        self.assertContains(page, "data-venue-search")
        self.assertContains(page, "venue-search.js")
        page = self.client.get(reverse("events:venue_create"))
        self.assertContains(page, 'data-mode="create"')


class PlacesErrorDetailTests(TestCase):
    @override_settings(GOOGLE_PLACES_API_KEY="test-key")
    def test_error_includes_google_reason_and_fix(self):
        import io
        from urllib.error import HTTPError

        body = json.dumps({"error": {"code": 403, "message": "Permission denied on the service.",
                                     "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                                  "reason": "API_KEY_SERVICE_BLOCKED"}]}}).encode()
        error = HTTPError("https://places.googleapis.com", 403, "Forbidden", {}, io.BytesIO(body))
        with mock.patch("events.google_places.urlopen", side_effect=error):
            with self.assertRaises(google_places.PlacesError) as ctx:
                google_places.autocomplete("estadio", "s1")
        self.assertIn("API_KEY_SERVICE_BLOCKED", str(ctx.exception))
        self.assertIn("API restrictions", str(ctx.exception))


# --------------------------------------------------------------------------- admins: shows from setlist.fm

from . import setlistfm_events  # noqa: E402

MCCARTNEY_2019 = {
    "id": "3b8e1c4c",
    "eventDate": "20-03-2019",
    "url": "https://www.setlist.fm/setlist/paul-mccartney/2019/x.html",
    "artist": {"mbid": "ba550d0e-adac-4864-b88b-407cab5e76af", "name": "Paul McCartney"},
    "tour": {"name": "Freshen Up"},
    "venue": {
        "id": "6bd6ca6e",
        "name": "Estadio Nacional",
        "city": {"name": "Santiago", "state": "Santiago Metropolitan",
                 "coords": {"lat": -33.4569, "long": -70.6482}, "country": {"code": "CL", "name": "Chile"}},
    },
    "sets": {"set": [{"song": [{"name": "A Hard Day's Night"}, {"name": "Junior's Farm"}]},
                     {"encore": 1, "song": [{"name": "Birthday"}]}]},
}


def _setlist(**overrides):
    import copy

    data = copy.deepcopy(MCCARTNEY_2019)
    data.update(overrides)
    return data


@override_settings(GOOGLE_PLACES_API_KEY="")
class SetlistShowImportTests(BaseTestCase):
    def test_creates_event_with_venue_tour_lineup_and_songs(self):
        result = setlistfm_events.create_event_from_setlist(_setlist(), self.user)
        event = result.event
        self.assertTrue(result.created)
        self.assertEqual(event.title, "Paul McCartney")
        self.assertEqual(event.date, date(2019, 3, 20))
        self.assertEqual((event.venue.name, event.venue.city, event.venue.country.code),
                         ("Estadio Nacional", "Santiago", "CL"))
        self.assertEqual(event.venue.setlistfm_id, "6bd6ca6e")
        link = event.eventartist_set.get(artist__name="Paul McCartney")
        self.assertEqual(link.production.title, "Freshen Up")  # the tour belongs to the act
        self.assertEqual(link.production.artist.name, "Paul McCartney")
        self.assertIsNone(event.production)
        self.assertEqual(event.created_by, self.user)
        self.assertEqual(event.setlists.get().entries.count(), 3)

    def test_reimport_and_second_date_reuse_venue_and_tour(self):
        first = setlistfm_events.create_event_from_setlist(_setlist(), self.user)
        again = setlistfm_events.create_event_from_setlist(_setlist(), self.user)
        self.assertTrue(again.setlist_already_imported)
        self.assertEqual(again.event, first.event)

        second_night = setlistfm_events.create_event_from_setlist(_setlist(id="43b8e1c8", eventDate="21-03-2019"))
        self.assertNotEqual(second_night.event, first.event)
        self.assertEqual(Venue.objects.filter(name="Estadio Nacional").count(), 1)
        self.assertEqual(Production.objects.filter(title="Freshen Up").count(), 1)
        tour = lambda e: e.eventartist_set.get(artist__name="Paul McCartney").production  # noqa: E731
        self.assertEqual(tour(second_night.event), tour(first.event))

    def test_joins_event_added_by_hand_and_hand_typed_venue(self):
        typed_venue = Venue.objects.create(name="estadio nacional", city="santiago")
        by_hand = Event.objects.create(title="McCartney en Chile", venue=typed_venue, date=date(2019, 3, 20))
        result = setlistfm_events.create_event_from_setlist(_setlist(), self.user)
        self.assertFalse(result.created)
        self.assertEqual(result.event, by_hand)
        self.assertEqual(result.event.title, "McCartney en Chile")  # custom titles are kept
        typed_venue.refresh_from_db()
        self.assertEqual(typed_venue.setlistfm_id, "6bd6ca6e")
        self.assertEqual(result.event.setlists.count(), 1)

    @override_settings(GOOGLE_PLACES_API_KEY="test-key")
    def test_uses_google_venue_when_top_match_fits(self):
        google_venue = Venue.objects.create(name="Estadio Nacional Julio Martínez Prádanos", city="Ñuñoa",
                                            google_place_id="ChIJ_estadio")
        with mock.patch("events.google_places.autocomplete", return_value=[
            {"place_id": "ChIJ_estadio", "main": "Estadio Nacional Julio Martínez Prádanos", "secondary": "Ñuñoa"},
        ]), mock.patch("events.google_places.venue_from_place", return_value=google_venue):
            event = setlistfm_events.create_event_from_setlist(_setlist(), self.user).event
        self.assertEqual(event.venue, google_venue)
        google_venue.refresh_from_db()
        self.assertEqual(google_venue.setlistfm_id, "6bd6ca6e")


@override_settings(GOOGLE_PLACES_API_KEY="")
class SetlistShowPageTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_user("mod", password="pw-test-000")
        call_command("make_admin", "mod", stdout=open("/dev/null", "w"))

    def test_any_logged_in_user_can_import(self):
        self.assertEqual(self.client.get(reverse("events:setlistfm_shows")).status_code, 302)  # anonymous
        self.assertNotContains(self.client.get(reverse("events:home")), "+ From setlist.fm")
        self.client.force_login(self.user)  # regular user, not an admin
        self.assertEqual(self.client.get(reverse("events:setlistfm_shows")).status_code, 200)
        self.assertContains(self.client.get(reverse("events:home")), "+ From setlist.fm")
        self.assertContains(self.client.get(reverse("events:event_list")), "Import from setlist.fm")

    @mock.patch("events.views_venues.search_setlists", return_value=[_setlist()])
    def test_search_then_import_selected(self, search):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("events:event_list")), "Import from setlist.fm")
        self.assertContains(self.client.get(reverse("events:home")), "+ From setlist.fm")  # navbar, every page
        page = self.client.get(reverse("events:setlistfm_shows"), {"artist": "Paul McCartney", "country": "cl"})
        self.assertContains(page, "Freshen Up")
        self.assertContains(page, "3 songs")
        search.assert_called_with(artist_name="Paul McCartney", city="", year="", venue="", country_code="cl", page=1)

        with mock.patch("events.setlistfm_events.get_setlist", return_value=_setlist()):
            response = self.client.post(
                reverse("events:setlistfm_shows") + "?artist=Paul+McCartney", {"setlist_id": ["3b8e1c4c"]}
            )
        self.assertRedirects(response, reverse("events:setlistfm_shows") + "?artist=Paul+McCartney")
        self.assertTrue(Event.objects.filter(title="Paul McCartney", date=date(2019, 3, 20)).exists())

        page = self.client.get(reverse("events:setlistfm_shows"), {"artist": "Paul McCartney"})
        self.assertContains(page, "Already imported")


class ImportAsAttendedSettingTests(BaseTestCase):
    @mock.patch("events.views_venues.search_setlists", return_value=[_setlist()])
    def test_profile_setting_picks_whether_the_attended_box_starts_ticked(self, search):
        from events.models import Profile
        self.client.force_login(self.user)
        url = reverse("events:setlistfm_shows")
        box = 'name="attended" value="1" checked'
        self.assertContains(self.client.get(url, {"artist": "Paul McCartney"}), box)  # on by default
        edit = self.client.get(reverse("events:profile_edit"))
        self.assertContains(edit, "Mark setlist.fm imports as attended")
        self.client.post(reverse("events:profile_edit"), {"username": "ana"})  # box left unticked
        self.assertFalse(Profile.objects.get(user=self.user).import_as_attended)
        page = self.client.get(url, {"artist": "Paul McCartney"})
        self.assertContains(page, 'name="attended" value="1" />')  # still there, just not ticked
        self.assertNotContains(page, box)
        # Ticking it on an import still adds the show to the diary.
        with mock.patch("events.setlistfm_events.get_setlist", return_value=_setlist()):
            self.client.post(url, {"setlist_id": ["3b8e1c4c"], "attended": "1"})
        self.assertTrue(Log.objects.filter(user=self.user, event__title="Paul McCartney").exists())


class GoogleOnlyLoginTests(TestCase):
    def test_login_page_offers_only_google(self):
        page = self.client.get(reverse("login"))
        self.assertContains(page, "Continue with Google")
        self.assertNotContains(page, 'type="password"')
        self.assertNotContains(page, "or use your username")

    def test_password_signup_is_closed_but_google_is_open(self):
        from allauth.account.adapter import get_adapter
        from allauth.socialaccount.adapter import get_adapter as get_social_adapter
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        self.assertFalse(get_adapter(request).is_open_for_signup(request))
        self.assertTrue(get_social_adapter(request).is_open_for_signup(request, None))
        page = self.client.get("/accounts/signup/")
        self.assertNotContains(page, 'type="password"', status_code=page.status_code)



class UsernameTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        get_user_model().objects.create_user("bob", password="pw-test-456")
        self.client.force_login(self.user)

    def _save(self, username):
        return self.client.post(reverse("events:profile_edit"), {"username": username})

    def test_change_username_moves_profile_url(self):
        response = self._save("ana.rocks")
        self.assertRedirects(response, reverse("events:profile", args=["ana.rocks"]))
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "ana.rocks")
        self.assertEqual(self.client.get(reverse("events:profile", args=["ana"])).status_code, 404)

    def test_taken_and_invalid_usernames_are_rejected(self):
        self.assertContains(self._save("BOB"), "That username is taken.")
        self.assertContains(self._save("ana rocks!"), "Use only letters")
        self.assertContains(self._save("ab"), "Use only letters")
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "ana")

    def test_form_shows_current_username_and_welcome(self):
        page = self.client.get(reverse("events:profile_edit") + "?welcome=1")
        self.assertContains(page, 'value="ana"')
        self.assertContains(page, "Choose your username")

    def test_new_google_users_are_sent_to_pick_a_username(self):
        from django.conf import settings

        self.assertEqual(settings.ACCOUNT_SIGNUP_REDIRECT_URL, "/settings/profile/?welcome=1")


class FutureEventLogTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        from datetime import timedelta

        from django.utils import timezone

        self.today = timezone.localdate()
        self.future = Event.objects.create(title="Next year's show", date=self.today + timedelta(days=30))
        self.client.force_login(self.user)

    def test_cannot_log_future_event(self):
        response = self.client.post(reverse("events:log_save", args=[self.future.pk]), {"rating": "10"}, follow=True)
        self.assertFalse(Log.objects.filter(event=self.future).exists())
        self.assertContains(response, "hasn&#x27;t happened yet")

    def test_future_event_page_hides_log_form(self):
        page = self.client.get(self.future.get_absolute_url())
        self.assertContains(page, "Coming up")
        self.assertNotContains(page, "Log it</button>")
        self.assertContains(page, "+ Want to go")

    def test_today_past_and_undated_events_can_be_logged(self):
        today = Event.objects.create(title="Tonight", date=self.today)
        undated = Event.objects.create(title="Some old show")
        for event in [today, undated, self.event]:
            with self.subTest(event=event.title):
                self.client.post(reverse("events:log_save", args=[event.pk]), {"rating": "8"})
                self.assertTrue(Log.objects.filter(event=event, user=self.user).exists())


# --------------------------------------------------------------------------- "Find photo"

from django.core.cache import cache  # noqa: E402

from . import image_search  # noqa: E402

DEEZER_ARTISTS = {"data": [
    {"name": "Paul McCartney", "picture_xl": "https://cdn.deezer.example/xl.jpg",
     "picture_medium": "https://cdn.deezer.example/m.jpg", "link": "https://www.deezer.com/artist/1"},
    {"name": "Paul McCartney Tribute", "picture_xl": "https://cdn.deezer.example/other.jpg"},
]}
COMMONS = {"query": {"pages": {"1": {"index": 1, "title": "File:Paul McCartney 2019.jpg", "imageinfo": [{
    "thumburl": "https://upload.wikimedia.org/thumb/pm.jpg", "descriptionurl": "https://commons.wikimedia.org/wiki/File:PM.jpg",
    "extmetadata": {"Artist": {"value": "<a href='x'>Jane Doe</a>"}, "LicenseShortName": {"value": "CC BY-SA 4.0"}}}]}}}}


def _fake_get_json(url, params, **kwargs):
    if "deezer" in url:
        return DEEZER_ARTISTS
    return COMMONS


class FindPhotoTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.event.created_by = self.user
        self.event.save()
        EventArtist.objects.create(event=self.event, artist=Artist.objects.create(name="Paul McCartney"))
        self.client.force_login(self.user)

    def test_parsers(self):
        with mock.patch("events.image_search._get_json", side_effect=_fake_get_json):
            deezer = image_search.deezer_artist_pictures("paul mccartney")
            commons = image_search.commons_images("x")
        self.assertEqual([c.url for c in deezer], ["https://cdn.deezer.example/xl.jpg"])  # exact name only
        self.assertEqual(commons[0].credit, "Jane Doe · CC BY-SA 4.0")
        self.assertEqual(commons[0].title, "Paul McCartney 2019.jpg")

    @mock.patch("events.image_search._get_json", side_effect=_fake_get_json)
    def test_picker_lists_free_sources_and_asks_openverse_from_the_browser(self, get_json):
        page = self.client.get(reverse("events:event_photo", args=[self.event.pk]))
        self.assertEqual([c["source"] for c in page.context["candidates"]][:2], ["Deezer", "Wikimedia Commons"])
        self.assertFalse(any("openverse" in c.args[0] for c in get_json.call_args_list))
        self.assertContains(page, 'data-openverse data-query="Green Day in Chile Chile 1992" data-artist="Paul McCartney"')
        self.assertContains(page, "events/photo-openverse.js")
        self.assertContains(page, "data-candidate-template")
        self.assertContains(page, "Wikimedia Commons")
        self.assertNotContains(page, "isn't set up yet")
        self.assertEqual(page.context["query"], "Green Day in Chile Chile 1992")  # name, venue country, year
        self.assertContains(page, "https://www.google.com/search?tbm=isch&amp;q=Green+Day+in+Chile+Chile+1992")
        self.assertContains(page, "Use this link")
        self.assertContains(page, "Drop a photo here")
        self.assertContains(page, "events/photo-drop.js")

    def test_clean_image_link(self):
        clean = image_search.clean_image_link
        self.assertEqual(clean("https://www.google.com/imgres?q=x&imgurl=https%3A%2F%2Fimg.example%2Fa.jpg&imgrefurl=https%3A%2F%2Fex.cl"),
                         "https://img.example/a.jpg")
        self.assertEqual(clean("https://www.google.cl/imgres?imgurl=https://img.example/b.jpg"), "https://img.example/b.jpg")
        self.assertEqual(clean("https://www.bing.com/images/search?q=x&mediaurl=https%3A%2F%2Fimg.example%2Fc.jpg"),
                         "https://img.example/c.jpg")
        self.assertEqual(clean("  https://img.example/d.jpg "), "https://img.example/d.jpg")
        self.assertTrue(image_search.is_search_page("https://www.google.com/search?q=x&udm=2"))
        self.assertFalse(image_search.is_search_page("https://lh3.googleusercontent.com/photo.jpg"))
        self.assertFalse(image_search.is_search_page("https://img.example/d.jpg"))

    def test_pick_unwraps_google_links_and_rejects_results_pages(self):
        url = reverse("events:event_photo", args=[self.event.pk])
        self.client.post(url, {"url": "https://www.google.com/imgres?imgurl=https%3A%2F%2Fimg.example%2Fgd.jpg"})
        self.event.refresh_from_db()
        self.assertEqual(self.event.image_url, "https://img.example/gd.jpg")

        response = self.client.post(url + "?q=green+day", {"url": "https://www.google.com/search?q=green+day&udm=2"}, follow=True)
        self.assertRedirects(response, url + "?q=green+day")
        self.assertContains(response, "link to the search results")
        self.event.refresh_from_db()
        self.assertEqual(self.event.image_url, "https://img.example/gd.jpg")

    def test_festival_toggle_script_is_on_the_page_not_in_the_title(self):
        with mock.patch("events.image_search.find_candidates", return_value=([], [])):
            page = self.client.get(reverse("events:event_photo", args=[self.event.pk]))
        title = page.content.decode().split("<title>")[1].split("</title>")[0]
        self.assertNotIn("<script", title)

    def test_pick_saves_photo(self):
        url = "https://img.example/" + "a" * 400 + ".jpg"  # long Google links fit
        response = self.client.post(reverse("events:event_photo", args=[self.event.pk]), {"url": url})
        self.assertRedirects(response, self.event.get_absolute_url())
        self.event.refresh_from_db()
        self.assertEqual(self.event.image_url, url)

        self.client.post(reverse("events:event_photo", args=[self.event.pk]), {"url": "javascript:alert(1)"})
        self.event.refresh_from_db()
        self.assertEqual(self.event.image_url, url)

    def test_only_people_who_can_edit(self):
        other = get_user_model().objects.create_user("bob", password="pw-test-456")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("events:event_photo", args=[self.event.pk])).status_code, 403)
        self.assertNotContains(self.client.get(self.event.get_absolute_url()), "Find photo")

    @mock.patch("events.image_search._get_json", side_effect=image_search.ImageSearchError("error 503: down"))
    def test_source_errors_are_shown_not_raised(self, _):
        page = self.client.get(reverse("events:event_photo", args=[self.event.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "error 503")

    def test_event_page_offers_find_photo(self):
        page = self.client.get(self.event.get_absolute_url())
        self.assertContains(page, "Find photo")


# --------------------------------------------------------------------------- same venue, same day

from .merge import merge_events  # noqa: E402
from .models import Comment, ReviewLike  # noqa: E402, F811


class SameSlotTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.green_day = Artist.objects.create(name="Green Day")
        self.foo = Artist.objects.create(name="Foo Fighters")
        self.opener = Artist.objects.create(name="Opener Band")
        self.a = Event.objects.create(title="", venue=self.venue, date=date(2019, 3, 20), created_by=self.user)
        EventArtist.objects.create(event=self.a, artist=self.green_day)
        self.b = Event.objects.create(title="", venue=self.venue, date=date(2019, 3, 20), created_by=self.user,
                                      image_url="https://img.example/foo.jpg")
        EventArtist.objects.create(event=self.b, artist=self.foo)
        EventArtist.objects.create(event=self.b, artist=self.opener, role="support", billing_order=2)
        self.a.refresh_from_db()
        self.b.refresh_from_db()

    def test_page_suggests_merging(self):
        self.client.force_login(self.user)
        page = self.client.get(self.b.get_absolute_url())
        self.assertContains(page, "Same venue, same day")
        self.assertContains(page, "Make it one festival")
        self.assertContains(page, "Foo Fighters opened for Green Day")
        self.assertContains(page, "Green Day opened for Foo Fighters")

    def test_no_suggestion_for_visitors_other_days_or_separate_shows(self):
        self.assertNotContains(self.client.get(self.b.get_absolute_url()), "Same venue, same day")
        self.client.force_login(self.user)
        other_day = Event.objects.create(title="Other day", venue=self.venue, date=date(2019, 3, 21))
        self.assertNotContains(self.client.get(other_day.get_absolute_url()), "Same venue, same day")

        self.client.post(reverse("events:event_separate", args=[self.b.pk]), {"other": self.a.pk})
        self.assertNotContains(self.client.get(self.b.get_absolute_url()), "Same venue, same day")
        self.assertNotContains(self.client.get(self.a.get_absolute_url()), "Same venue, same day")

    def test_festival_merge_moves_everything(self):
        bob = get_user_model().objects.create_user("bob", password="pw-test-456")
        log_a = Log.objects.create(user=bob, event=self.a, rating=8)
        log_b = Log.objects.create(user=bob, event=self.b, review="Both were great")
        Comment.objects.create(user=self.user, log=log_b, body="Agreed")
        ReviewLike.objects.create(user=self.user, log=log_b)
        Log.objects.create(user=self.user, event=self.b, rating=10)
        WantToGo.objects.create(user=bob, event=self.b)
        event_list = EventList.objects.create(user=bob, title="2019")
        EventListItem.objects.create(event_list=event_list, event=self.a, position=1)
        EventListItem.objects.create(event_list=event_list, event=self.b, position=2)

        kept = merge_events(self.a, self.b, mode="festival")
        self.assertFalse(Event.objects.filter(pk=self.b.pk).exists())
        self.assertEqual(kept.event_type, EventType.FESTIVAL)
        self.assertEqual(kept.title, "Green Day & Foo Fighters")  # kept event's headliner billed first
        self.assertEqual(kept.image_url, "https://img.example/foo.jpg")
        roles = dict(kept.eventartist_set.values_list("artist__name", "role"))
        self.assertEqual(roles, {"Green Day": "headliner", "Foo Fighters": "headliner", "Opener Band": "support"})

        bob_log = Log.objects.get(user=bob, event=kept)  # one entry, gaps filled
        self.assertEqual((bob_log.rating, bob_log.review), (8, "Both were great"))
        self.assertEqual(bob_log.pk, log_a.pk)
        self.assertEqual(bob_log.comments.count(), 1)
        self.assertEqual(bob_log.likes.count(), 1)
        self.assertTrue(Log.objects.filter(user=self.user, event=kept, rating=10).exists())
        self.assertTrue(WantToGo.objects.filter(user=bob, event=kept).exists())
        self.assertEqual(list(event_list.items.values_list("event_id", "position")), [(kept.pk, 1)])

    def test_support_merge_from_the_page(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("events:event_merge", args=[self.b.pk]), {"other": self.a.pk, "action": "this_supports_other"}
        )
        self.assertRedirects(response, self.a.get_absolute_url())
        self.assertFalse(Event.objects.filter(pk=self.b.pk).exists())
        roles = dict(self.a.eventartist_set.values_list("artist__name", "role"))
        self.assertEqual(roles["Green Day"], "headliner")
        self.assertEqual(roles["Foo Fighters"], "support")
        self.a.refresh_from_db()
        self.assertEqual(self.a.title, "Green Day")

    def test_cannot_merge_away_someone_elses_event(self):
        bob = get_user_model().objects.create_user("bob", password="pw-test-456")
        self.client.force_login(bob)
        # bob's own event at the same slot: he may fold it into ours, not ours into his.
        mine = Event.objects.create(title="", venue=self.venue, date=date(2019, 3, 20), created_by=bob)
        response = self.client.post(
            reverse("events:event_merge", args=[mine.pk]), {"other": self.a.pk, "action": "other_supports_this"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Event.objects.filter(pk=self.a.pk).exists())
        page = self.client.get(mine.get_absolute_url())
        self.assertNotContains(page, "Green Day opened for")
        self.client.post(reverse("events:event_merge", args=[mine.pk]), {"other": self.a.pk, "action": "festival"})
        self.assertFalse(Event.objects.filter(pk=mine.pk).exists())  # his event merged into ours
        self.assertEqual(Event.objects.get(pk=self.a.pk).event_type, EventType.FESTIVAL)


class EventPhotoProportionsTests(BaseTestCase):
    def test_event_page_shows_full_photo_cards_stay_uniform(self):
        self.event.image_url = "https://img.example/wide.jpg"
        self.event.save()
        page = self.client.get(self.event.get_absolute_url()).content.decode()
        self.assertIn('class="event-photo" src="https://img.example/wide.jpg"', page)
        listing = self.client.get(reverse("events:event_list")).content.decode()
        self.assertIn('<div class="poster position-relative">', listing)  # cards keep the poster crop


class HomeOrderTests(BaseTestCase):
    def test_recently_happened_comes_before_just_added(self):
        html = self.client.get(reverse("events:home")).content.decode()
        self.assertLess(html.index("Recently happened"), html.index("Just added"))


class ProfileStatsAndArtistPhotoTests(BaseTestCase):
    def test_most_seen_artists_and_venues(self):
        green_day = Artist.objects.create(name="Green Day")
        other_venue = Venue.objects.create(name="Movistar Arena", city="Santiago")
        for i, venue in enumerate([self.venue, self.venue, other_venue]):
            e = Event.objects.create(title=f"Show {i}", venue=venue, date=date(2020, 1, i + 1))
            EventArtist.objects.create(event=e, artist=green_day)
            Log.objects.create(user=self.user, event=e)
        stranger = get_user_model().objects.create_user("bob", password="pw-test-456")
        Log.objects.create(user=stranger, event=self.event)  # not counted for ana

        page = self.client.get(reverse("events:profile", args=["ana"]))
        self.assertEqual([(p.name, p.n) for p in page.context["top_artists"]], [("Green Day", 3)])
        self.assertEqual([(v.name, v.n) for v in page.context["top_venues"]],
                         [("Teatro Caupolicán", 2), ("Movistar Arena", 1)])
        self.assertContains(page, "Most seen artists")
        self.assertContains(page, "2 visits")

    def test_event_uses_artist_photo_when_it_has_none(self):
        artist = Artist.objects.create(name="Green Day", image_url="https://img.example/gd.jpg")
        EventArtist.objects.create(event=self.event, artist=artist)
        self.assertEqual(self.event.display_image, "https://img.example/gd.jpg")
        self.assertContains(self.client.get(reverse("events:event_list")), "https://img.example/gd.jpg")
        self.event.image_url = "https://img.example/poster.jpg"
        self.assertEqual(self.event.display_image, "https://img.example/poster.jpg")
        bare = Event.objects.create(title="No photos")
        self.assertEqual(bare.display_image, "")


class ArtistPhotoTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.artist = Artist.objects.create(name="Paul McCartney")
        EventArtist.objects.create(event=self.event, artist=self.artist)
        self.client.force_login(self.user)

    @mock.patch("events.image_search._get_json", side_effect=_fake_get_json)
    def test_any_user_adds_missing_artist_photo_and_events_use_it(self, _):
        self.assertContains(self.client.get(self.artist.get_absolute_url()), "Find photo")
        page = self.client.get(reverse("events:artist_photo", args=[self.artist.pk]))
        self.assertEqual(page.context["query"], "Paul McCartney")
        self.assertContains(page, "Deezer")
        response = self.client.post(reverse("events:artist_photo", args=[self.artist.pk]),
                                    {"url": "https://cdn.deezer.example/xl.jpg"})
        self.assertRedirects(response, self.artist.get_absolute_url())
        self.artist.refresh_from_db()
        self.assertEqual(self.artist.image_url, "https://cdn.deezer.example/xl.jpg")
        self.assertEqual(Event.objects.get(pk=self.event.pk).display_image, "https://cdn.deezer.example/xl.jpg")

    def test_only_admins_change_an_existing_artist_photo(self):
        self.artist.image_url = "https://img.example/old.jpg"
        self.artist.save()
        self.assertEqual(self.client.get(reverse("events:artist_photo", args=[self.artist.pk])).status_code, 403)
        self.assertNotContains(self.client.get(self.artist.get_absolute_url()), "Change photo")
        admin = get_user_model().objects.create_user("mod", password="pw-test-000")
        call_command("make_admin", "mod", stdout=open("/dev/null", "w"))
        self.client.force_login(admin)
        self.assertContains(self.client.get(self.artist.get_absolute_url()), "Change photo")


class HomePlaceTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        from datetime import timedelta

        from django.utils import timezone

        soon = timezone.localdate() + timedelta(days=10)
        self.santiago_show = Event.objects.create(title="Santiago show", venue=self.venue, date=soon)
        lima = Venue.objects.create(name="Arena Lima", city="Lima", country="PE")
        self.lima_show = Event.objects.create(title="Lima show", venue=lima, date=soon)
        self.client.force_login(self.user)

    def _set_home(self, city="", country=""):
        self.user.profile.home_city, self.user.profile.home_country = city, country
        self.user.profile.save()

    def test_upcoming_filtered_by_city_then_country(self):
        upcoming = lambda: [e.title for e in self.client.get(reverse("events:home")).context["upcoming"]]  # noqa: E731
        self.assertEqual(sorted(upcoming()), ["Lima show", "Santiago show"])
        self._set_home(city="santiago")
        self.assertEqual(upcoming(), ["Santiago show"])
        self.assertContains(self.client.get(reverse("events:home")), "Upcoming in santiago")
        self._set_home(country="PE")
        self.assertEqual(upcoming(), ["Lima show"])
        self.assertContains(self.client.get(reverse("events:home")), "Upcoming in Peru")

    def test_defaults_in_setlistfm_search_and_new_venue(self):
        self._set_home(city="Santiago", country="CL")
        page = self.client.get(reverse("events:setlistfm_shows"))
        self.assertEqual((page.context["f"]["city"], page.context["f"]["country"]), ("Santiago", "CL"))
        with mock.patch("events.views_venues.search_setlists", return_value=[]):
            searched = self.client.get(reverse("events:setlistfm_shows"), {"artist": "X", "city": "", "country": ""})
        self.assertEqual(searched.context["f"]["city"], "")  # an explicit search is respected
        form = self.client.get(reverse("events:event_create")).context["form"]
        self.assertEqual((form["new_venue_city"].value(), form["new_venue_country"].value()), ("Santiago", "CL"))

    def test_profile_form_saves_home_place(self):
        self.client.post(reverse("events:profile_edit"), {"username": "ana", "home_city": "Valparaíso", "home_country": "CL"})
        self.user.profile.refresh_from_db()
        self.assertEqual((self.user.profile.home_city, self.user.profile.home_country.code), ("Valparaíso", "CL"))



@override_settings(GOOGLE_PLACES_API_KEY="")
class ImportAttendedTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _import(self, **extra):
        with mock.patch("events.setlistfm_events.get_setlist", return_value=_setlist()):
            return self.client.post(reverse("events:setlistfm_shows") + "?artist=X",
                                    {"setlist_id": ["3b8e1c4c"], **extra}, follow=True)

    def test_checkbox_is_on_by_default_and_logs_the_show(self):
        with mock.patch("events.views_venues.search_setlists", return_value=[_setlist(id="other")]):
            page = self.client.get(reverse("events:setlistfm_shows"), {"artist": "X"})
        self.assertContains(page, 'name="attended" value="1" checked')
        response = self._import(attended="1")
        self.assertContains(response, "1 added to your diary")
        self.assertTrue(Log.objects.filter(user=self.user, event__date=date(2019, 3, 20)).exists())
        self._import(attended="1")  # re-importing doesn't duplicate the diary entry
        self.assertEqual(Log.objects.filter(user=self.user).count(), 1)

    def test_unchecked_does_not_log(self):
        self._import()
        self.assertFalse(Log.objects.filter(user=self.user).exists())

    def test_future_shows_are_not_logged(self):
        with mock.patch("events.setlistfm_events.get_setlist", return_value=_setlist(eventDate="20-03-2099")):
            self.client.post(reverse("events:setlistfm_shows") + "?artist=X", {"setlist_id": ["3b8e1c4c"], "attended": "1"})
        self.assertFalse(Log.objects.filter(user=self.user).exists())



class NewVenueDefaultsIgnoredTests(BaseTestCase):
    def test_prefilled_city_alone_does_not_require_a_venue(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("events:event_create"), {
            "title": "No venue yet", "event_type": "music", "date": "",
            "new_venue_name": "", "new_venue_city": "Santiago", "new_venue_country": "CL",
            "eventartist_set-TOTAL_FORMS": "0", "eventartist_set-INITIAL_FORMS": "0",
            "eventartist_set-MIN_NUM_FORMS": "0", "eventartist_set-MAX_NUM_FORMS": "1000",
        })
        event = Event.objects.get(title="No venue yet")
        self.assertRedirects(response, event.get_absolute_url())
        self.assertIsNone(event.venue)
        self.assertEqual(Venue.objects.count(), 1)  # nothing new created


class SetTimesAndFestivalTests(BaseTestCase):
    def test_set_times_per_artist_in_form_and_lineup(self):
        self.client.force_login(self.user)
        opener = Artist.objects.create(name="Opener")
        star = Artist.objects.create(name="Star")
        self.client.post(reverse("events:event_create"), {
            "title": "", "event_type": "music", "date": "2020-01-10",
            "eventartist_set-TOTAL_FORMS": "2", "eventartist_set-INITIAL_FORMS": "0",
            "eventartist_set-MIN_NUM_FORMS": "0", "eventartist_set-MAX_NUM_FORMS": "1000",
            "eventartist_set-0-artist": str(star.pk), "eventartist_set-0-role": "headliner",
            "eventartist_set-0-billing_order": "1", "eventartist_set-0-start_time": "21:30",
            "eventartist_set-1-artist": str(opener.pk), "eventartist_set-1-role": "support",
            "eventartist_set-1-billing_order": "2", "eventartist_set-1-start_time": "19:00",
        })
        event = Event.objects.get(artists=star)
        page = self.client.get(event.get_absolute_url())
        self.assertEqual([l.artist.name for l in page.context["lineup"]], ["Star", "Opener"])  # billing order wins
        self.assertContains(page, "21:30")

    def test_group_nearby_festival_days(self):
        self.client.force_login(self.user)
        day1 = Event.objects.create(title="", event_type=EventType.FESTIVAL, venue=self.venue,
                                    date=date(2024, 3, 15), created_by=self.user)
        day2 = Event.objects.create(title="", event_type=EventType.FESTIVAL, venue=self.venue,
                                    date=date(2024, 3, 16), created_by=self.user)
        far = Event.objects.create(title="", event_type=EventType.FESTIVAL, venue=self.venue, date=date(2024, 9, 1))
        page = self.client.get(day1.get_absolute_url())
        self.assertContains(page, "Other festival days")
        self.assertEqual(page.context["festival_suggestion"]["days"], [day2])

        response = self.client.post(reverse("events:festival_group", args=[day1.pk]), {"name": "Lollapalooza Chile"})
        festival = Production.objects.get(title="Lollapalooza Chile")
        self.assertRedirects(response, festival.get_absolute_url())
        day1.refresh_from_db(); day2.refresh_from_db(); far.refresh_from_db()
        self.assertEqual((day1.production, day2.production, far.production), (festival, festival, None))
        self.assertEqual(day1.title, "Lollapalooza Chile")
        self.assertNotContains(self.client.get(day1.get_absolute_url()), "Other festival days")
        festival_page = self.client.get(festival.get_absolute_url())
        self.assertContains(festival_page, "2024 · 2 days")


class RatingHistogramTests(BaseTestCase):
    def test_histogram_counts_and_average(self):
        from .templatetags.event_extras import rating_histogram

        for i, rating in enumerate([10, 10, 8, 7, None]):
            u = get_user_model().objects.create_user(f"u{i}", password="pw-test-000")
            Log.objects.create(user=u, event=self.event, rating=rating)
        data = rating_histogram(self.event.logs.all())
        self.assertEqual(data["total"], 4)  # unrated logs are left out
        self.assertEqual([b["count"] for b in data["bars"]], [0, 0, 0, 0, 0, 0, 1, 1, 0, 2])
        self.assertEqual([b["height"] for b in data["bars"]][-1], 100)
        self.assertAlmostEqual(data["average"], 8.75)

        for url in [self.event.get_absolute_url(), reverse("events:profile", args=["u0"])]:
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), 'class="rating-bars')

    def test_no_chart_without_ratings(self):
        self.assertNotContains(self.client.get(self.event.get_absolute_url()), 'class="rating-bars')



class FavoriteChoicesTests(BaseTestCase):
    def test_choices_are_your_diary_events_labelled_by_event(self):
        zz = Event.objects.create(title="Zappa Tribute", venue=self.venue, date=date(2019, 3, 20))
        aa = Event.objects.create(title="ABBA", date=None)
        not_mine = Event.objects.create(title="Not in my diary")
        for e in [zz, aa, self.event]:
            Log.objects.create(user=self.user, event=e)
        self.client.force_login(self.user)
        field = self.client.get(reverse("events:profile_edit")).context["favorites_form"].fields["slot_1"]
        labels = [label for _, label in field.choices][1:]
        self.assertEqual(labels, ["ABBA — date not added",
                                  "Green Day in Chile — Teatro Caupolicán · 10 Nov 1992",
                                  "Zappa Tribute — Teatro Caupolicán · 20 Mar 2019"])
        self.assertNotIn(not_mine.title, " ".join(labels))


class NewArtistInLineupTests(BaseTestCase):
    def _post(self, **rows):
        data = {
            "title": "", "event_type": "music", "date": "2020-01-10",
            "eventartist_set-TOTAL_FORMS": "3", "eventartist_set-INITIAL_FORMS": "0",
            "eventartist_set-MIN_NUM_FORMS": "0", "eventartist_set-MAX_NUM_FORMS": "1000",
        }
        data.update(rows)
        self.client.force_login(self.user)
        return self.client.post(reverse("events:event_create"), data)

    def test_typing_a_new_artist_creates_it(self):
        self._post(**{"eventartist_set-0-new_artist": "Los Bunkers", "eventartist_set-0-role": "headliner",
                      "eventartist_set-0-billing_order": "1"})
        event = Event.objects.get(artists__name="Los Bunkers")
        self.assertEqual(event.title, "Los Bunkers")

    def test_existing_artist_is_reused_ignoring_case(self):
        existing = Artist.objects.create(name="Los Bunkers")
        self._post(**{"eventartist_set-0-new_artist": "los bunkers"})
        self.assertEqual(Artist.objects.filter(name__iexact="los bunkers").count(), 1)
        self.assertTrue(Event.objects.filter(artists=existing).exists())

    def test_picking_and_typing_is_rejected(self):
        p = Artist.objects.create(name="A")
        page = self._post(**{"eventartist_set-0-artist": str(p.pk), "eventartist_set-0-new_artist": "B"})
        self.assertContains(page, "not both")
        self.assertFalse(Artist.objects.filter(name="B").exists())


class UpcomingSortTests(BaseTestCase):
    def test_upcoming_defaults_to_soonest_first(self):
        from datetime import timedelta

        from django.utils import timezone

        today = timezone.localdate()
        far = Event.objects.create(title="Far", date=today + timedelta(days=200))
        soon = Event.objects.create(title="Soon", date=today + timedelta(days=3))
        titles = lambda q: [e.title for e in self.client.get(reverse("events:event_list") + q).context["page"].object_list]  # noqa: E731
        self.assertEqual(titles("?when=upcoming"), ["Soon", "Far"])
        self.assertEqual(titles("?when=upcoming&sort=newest"), ["Far", "Soon"])  # explicit choice wins
        self.assertEqual(titles("")[:2], ["Far", "Soon"])  # everything: latest date first
        self.assertTrue(far.pk and soon.pk)


# --------------------------------------------------------------------------- acts seen at an event

from .models import ActLog  # noqa: E402


class ActLogTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.star = Artist.objects.create(name="Star")
        self.opener = Artist.objects.create(name="Opener")
        EventArtist.objects.create(event=self.event, artist=self.star)
        EventArtist.objects.create(event=self.event, artist=self.opener, role="support", billing_order=2)
        self.client.force_login(self.user)

    def _log(self, **data):
        return self.client.post(reverse("events:log_save", args=[self.event.pk]), {"rating": "8", **data})

    def test_new_log_sees_every_act_by_default(self):
        Log.objects.create(user=self.user, event=self.event)
        self.assertEqual(set(ActLog.objects.values_list("artist__name", flat=True)), {"Star", "Opener"})

    def test_untick_rate_and_note_acts(self):
        self._log(acts_submitted="1",
                  **{f"act_seen_{self.star.pk}": "1", f"act_rating_{self.star.pk}": "10",
                     f"act_note_{self.star.pk}": "Played Yesterday"})
        log = Log.objects.get(user=self.user, event=self.event)
        self.assertEqual(log.rating, 8)  # event rating stays on the event
        self.assertEqual([(a.artist.name, a.rating, a.note) for a in log.acts.all()],
                         [("Star", 10, "Played Yesterday")])

        page = self.client.get(reverse("events:review_detail", args=[log.pk]))
        self.assertContains(page, "Acts seen")
        self.assertContains(page, "Played Yesterday")
        self.assertContains(page, "Didn't see: Opener")

    def test_artist_stats_count_only_acts_seen(self):
        self._log(acts_submitted="1", **{f"act_seen_{self.star.pk}": "1", f"act_rating_{self.star.pk}": "6"})
        profile = self.client.get(reverse("events:profile", args=["ana"]))
        self.assertEqual([p.name for p in profile.context["top_artists"]], ["Star"])
        opener_page = self.client.get(self.opener.get_absolute_url())
        self.assertEqual(opener_page.context["log_count"], 0)
        star_page = self.client.get(self.star.get_absolute_url())
        self.assertEqual((star_page.context["log_count"], star_page.context["avg_rating"]), (1, 6))

    def test_event_page_form_and_lineup_average(self):
        page = self.client.get(self.event.get_absolute_url())
        self.assertContains(page, "Acts you saw")
        self.assertContains(page, f'name="act_seen_{self.opener.pk}" value="1" checked')
        self._log(acts_submitted="1", **{f"act_seen_{self.star.pk}": "1", f"act_rating_{self.star.pk}": "9"})
        page = self.client.get(self.event.get_absolute_url())
        self.assertNotContains(page, f'name="act_seen_{self.opener.pk}" value="1" checked')
        self.assertContains(page, 'title="Average from 1 who saw them"')

class ToursBelongToArtistsTests(BaseTestCase):
    def test_tour_page_lists_the_acts_dates_and_act_ratings(self):
        artist = Artist.objects.create(name="Paul McCartney")
        tour = Production.objects.create(title="Got Back", artist=artist)
        link = EventArtist.objects.create(event=self.event, artist=artist, production=tour)
        log = Log.objects.create(user=self.user, event=self.event, rating=4)
        log.acts.filter(artist=artist).update(rating=10)
        page = self.client.get(tour.get_absolute_url())
        self.assertContains(page, "Tour by")
        self.assertEqual(page.context["avg_rating"], 10)  # the act's rating, not the event's
        self.assertContains(self.client.get(self.event.get_absolute_url()), "Got Back")
        self.assertContains(self.client.get(artist.get_absolute_url()), "Got Back")
        self.assertTrue(link.pk)

class AdminFestivalButtonsTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_user("mod", password="pw-test-000")
        call_command("make_admin", "mod", stdout=open("/dev/null", "w"))
        for i, name in enumerate(["A", "B"], start=1):
            EventArtist.objects.create(event=self.event, artist=Artist.objects.create(name=name), billing_order=i)

    def test_turn_into_festival_admin_only(self):
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(self.event.get_absolute_url()), "Turn into festival")
        self.assertEqual(self.client.post(reverse("events:make_festival", args=[self.event.pk]), {"name": "X"}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.event.get_absolute_url()), "Turn into festival")
        self.client.post(reverse("events:make_festival", args=[self.event.pk]), {"name": "Rock en Ñuñoa"})
        self.event.refresh_from_db()
        self.assertEqual((self.event.event_type, self.event.production.title), ("festival", "Rock en Ñuñoa"))

    def test_add_show_to_same_day_festival_in_city(self):
        other_venue = Venue.objects.create(name="Parque O'Higgins", city="Santiago")
        fest = Event.objects.create(title="Lollapalooza", event_type=EventType.FESTIVAL, venue=other_venue,
                                    date=self.event.date)
        self.client.force_login(self.admin)
        page = self.client.get(self.event.get_absolute_url())
        self.assertContains(page, "Add to Lollapalooza")
        response = self.client.post(reverse("events:add_to_festival", args=[self.event.pk]), {"festival": fest.pk})
        self.assertRedirects(response, fest.get_absolute_url())
        self.assertFalse(Event.objects.filter(pk=self.event.pk).exists())
        self.assertEqual(set(fest.artists.values_list("name", flat=True)), {"A", "B"})


class LineupOrderTests(BaseTestCase):
    def test_lineup_and_setlists_follow_billing_order(self):
        first = Artist.objects.create(name="Zed")  # alphabetically last, billed first
        second = Artist.objects.create(name="Abe")
        EventArtist.objects.create(event=self.event, artist=first, billing_order=1)
        EventArtist.objects.create(event=self.event, artist=second, billing_order=2)
        Setlist.objects.create(event=self.event, artist=second)
        Setlist.objects.create(event=self.event, artist=first)
        page = self.client.get(self.event.get_absolute_url())
        self.assertEqual([r["link"].artist.name for r in page.context["act_rows"]], ["Zed", "Abe"])
        self.assertEqual([s.artist.name for s in page.context["setlists"]], ["Zed", "Abe"])

    def test_form_has_drag_handles_and_hidden_order(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("events:event_create"))
        self.assertContains(page, 'class="drag-handle')
        self.assertContains(page, 'type="hidden" name="eventartist_set-0-billing_order"')
        self.assertContains(page, "lineup-order.js")


class ArtistVenueEventsTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.artist = Artist.objects.create(name="Paul McCartney")
        self.e2010 = Event.objects.create(title="2010", venue=self.venue, date=date(2010, 5, 1))
        self.e2019 = Event.objects.create(title="2019", venue=self.venue, date=date(2019, 3, 20))
        self.undated = Event.objects.create(title="Undated", venue=self.venue)
        for e in [self.e2010, self.e2019, self.undated, self.event]:
            EventArtist.objects.create(event=e, artist=self.artist)

    def test_events_sorted_latest_first(self):
        for url in [self.artist.get_absolute_url(), self.venue.get_absolute_url()]:
            with self.subTest(url=url):
                titles = [e.title for e in self.client.get(url).context["events"]]
                self.assertEqual(titles, ["2019", "2010", "Green Day in Chile", "Undated"])

    def test_most_seen_links_filter_to_that_persons_shows(self):
        Log.objects.create(user=self.user, event=self.e2019)
        Log.objects.create(user=self.user, event=self.e2010)
        log = Log.objects.create(user=self.user, event=self.event)
        log.acts.filter(artist=self.artist).delete()  # was there but missed this act

        profile = self.client.get(reverse("events:profile", args=["ana"]))
        self.assertContains(profile, f"{self.artist.get_absolute_url()}?seen_by=ana")

        page = self.client.get(self.artist.get_absolute_url(), {"seen_by": "ana"})
        self.assertEqual([e.title for e in page.context["events"]], ["2019", "2010"])
        self.assertContains(page, "has seen Paul McCartney")
        self.assertContains(page, "2 times")
        venue_page = self.client.get(self.venue.get_absolute_url(), {"seen_by": "ana"})
        self.assertEqual(len(venue_page.context["events"]), 3)  # venue visits count every logged event
        self.assertContains(venue_page, "3 times")

    def test_artist_page_toggles_to_only_the_shows_i_saw(self):
        url = self.artist.get_absolute_url()
        self.assertNotContains(self.client.get(url), "Shows I saw")  # signed out: no toggle

        self.client.force_login(self.user)
        page = self.client.get(url)
        self.assertContains(page, "Shows I saw (0)")  # nothing logged yet: shown, but disabled
        self.assertContains(page, "Log a show of theirs")

        Log.objects.create(user=self.user, event=self.e2019)
        missed = Log.objects.create(user=self.user, event=self.event)
        missed.acts.filter(artist=self.artist).delete()  # at the event, but didn't see this act
        page = self.client.get(url, {"sort": "count"})
        self.assertEqual(page.context["my_show_count"], 1)
        self.assertContains(page, 'href="/artists/%d/?sort=count&amp;seen_by=ana">Shows I saw (1)</a>' % self.artist.pk)
        self.assertEqual(len(page.context["events"]), 4)

        mine = self.client.get(url, {"sort": "count", "seen_by": "ana"})
        self.assertEqual([e.title for e in mine.context["events"]], ["2019"])
        self.assertContains(mine, "Events you saw")
        self.assertNotContains(mine, "has seen Paul McCartney")  # the banner is for other people's lists
        self.assertContains(mine, 'href="/artists/%d/?sort=count">All shows</a>' % self.artist.pk)

        other = get_user_model().objects.create_user("bea", password="pw-test-789")
        Log.objects.create(user=other, event=self.e2010)
        theirs = self.client.get(url, {"seen_by": "bea"})
        self.assertContains(theirs, "@bea</a> has seen Paul McCartney")
        self.assertContains(theirs, "Events they saw")


class LineupFormUiTests(BaseTestCase):
    def test_artist_search_replaces_the_new_artist_box_and_add_act_button(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("events:event_create")).content.decode()
        self.assertNotIn('id="new-artist-box"', html)
        self.assertIn('id="artist-options"', html)  # the list the search box suggests from
        self.assertIn("Type to search artists", html)
        self.assertIn('id="add-act"', html)
        self.assertIn('id="lineup-row-template"', html)
        self.assertIn("eventartist_set-__prefix__-artist", html)  # template row for new acts
        self.assertNotIn("…or type a new artist", html)
        self.assertIn('type="hidden" name="eventartist_set-0-new_artist"', html)

    def test_rows_added_on_the_page_are_saved(self):
        self.client.force_login(self.user)
        existing = Artist.objects.create(name="Headliner")
        self.client.post(reverse("events:event_create"), {
            "title": "", "event_type": "music", "date": "2020-02-02",
            "eventartist_set-TOTAL_FORMS": "3", "eventartist_set-INITIAL_FORMS": "0",
            "eventartist_set-MIN_NUM_FORMS": "0", "eventartist_set-MAX_NUM_FORMS": "1000",
            "eventartist_set-0-artist": str(existing.pk), "eventartist_set-0-billing_order": "1",
            # row 1 was removed with × on the page: its fields are simply missing
            "eventartist_set-2-new_artist": "Brand New Band", "eventartist_set-2-billing_order": "2",
            "eventartist_set-2-role": "support",
        })
        event = Event.objects.get(artists=existing)
        self.assertEqual(list(event.eventartist_set.values_list("artist__name", "billing_order")),
                         [("Headliner", 1), ("Brand New Band", 2)])


class ArtistDeleteTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.artist = Artist.objects.create(name="Old Band")
        self.other = Artist.objects.create(name="Stays")
        EventArtist.objects.create(event=self.event, artist=self.other)
        EventArtist.objects.create(event=self.event, artist=self.artist, billing_order=2)
        self.log = Log.objects.create(user=self.user, event=self.event, rating=9)
        import_setlist(self.event, {**SETLIST_PAYLOAD, "artist": {"name": "Old Band"}})
        self.tour = Production.objects.create(title="Farewell", artist=self.artist)

    def test_regular_users_cannot_delete(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("events:artist_delete", args=[self.artist.pk])).status_code, 403)
        self.assertNotContains(self.client.get(self.artist.get_absolute_url()), "Delete artist")

    def test_admin_deletes_artist_keeps_events_and_logs(self):
        admin = get_user_model().objects.create_user("mod", password="pw-test-000")
        call_command("make_admin", "mod", stdout=open("/dev/null", "w"))
        self.client.force_login(admin)
        confirm = self.client.get(reverse("events:artist_delete", args=[self.artist.pk]))
        self.assertContains(confirm, "1 setlist")
        self.client.post(reverse("events:artist_delete", args=[self.artist.pk]))
        self.assertFalse(Artist.objects.filter(pk=self.artist.pk).exists())
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.log.refresh_from_db()
        self.assertEqual(list(self.log.acts.values_list("artist__name", flat=True)), ["Stays"])
        self.assertFalse(Setlist.objects.exists())
        self.tour.refresh_from_db()
        self.assertIsNone(self.tour.artist)


class MergeDuplicatesTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_user("mod", password="pw-test-000")
        call_command("make_admin", "mod", stdout=open("/dev/null", "w"))

    def test_merge_duplicate_artist(self):
        keep = Artist.objects.create(name="Los Bunkers")
        dup = Artist.objects.create(name="Los bunkers", image_url="https://img.example/lb.jpg")
        other_event = Event.objects.create(title="", date=date(2020, 1, 1))
        EventArtist.objects.create(event=self.event, artist=keep)
        EventArtist.objects.create(event=self.event, artist=dup, billing_order=2)  # both on one lineup
        EventArtist.objects.create(event=other_event, artist=dup)
        log = Log.objects.create(user=self.user, event=self.event)
        log.acts.filter(artist=dup).update(rating=9)
        import_setlist(other_event, {**SETLIST_PAYLOAD, "artist": {"name": "Los bunkers"}})
        tour = Production.objects.create(title="Tour", artist=dup)

        self.client.force_login(self.admin)
        page = self.client.get(keep.get_absolute_url())
        self.assertContains(page, "Similar names")
        self.client.post(reverse("events:artist_merge", args=[keep.pk]), {"other": dup.pk})

        self.assertFalse(Artist.objects.filter(pk=dup.pk).exists())
        self.assertEqual(self.event.eventartist_set.filter(artist=keep).count(), 1)
        self.assertTrue(other_event.eventartist_set.filter(artist=keep).exists())
        self.assertEqual(log.acts.get(artist=keep).rating, 9)
        self.assertEqual(Setlist.objects.get().artist, keep)
        tour.refresh_from_db(); keep.refresh_from_db(); other_event.refresh_from_db()
        self.assertEqual((tour.artist, keep.image_url), (keep, "https://img.example/lb.jpg"))
        self.assertEqual(other_event.title, "Los Bunkers")

    def test_merge_duplicate_event(self):
        dup = Event.objects.create(title="Green Day in Chile", date=self.event.date)
        EventArtist.objects.create(event=dup, artist=Artist.objects.create(name="Green Day"))
        bob = get_user_model().objects.create_user("bob", password="pw-test-456")
        Log.objects.create(user=bob, event=dup, rating=7)
        self.client.force_login(self.admin)
        page = self.client.get(self.event.get_absolute_url())
        self.assertContains(page, "Merge a duplicate")
        self.client.post(reverse("events:event_merge_duplicate", args=[self.event.pk]), {"other": dup.pk})
        self.assertFalse(Event.objects.filter(pk=dup.pk).exists())
        self.assertTrue(Log.objects.filter(user=bob, event=self.event, rating=7).exists())
        self.assertEqual(self.event.eventartist_set.get().role, "headliner")

    def test_merge_by_pasted_link_and_admin_only(self):
        dup = Event.objects.create(title="Other title", date=date(2001, 1, 1))
        self.client.force_login(self.user)
        url = reverse("events:event_merge_duplicate", args=[self.event.pk])
        self.assertEqual(self.client.post(url, {"other_id": str(dup.pk)}).status_code, 403)
        self.client.force_login(self.admin)
        self.client.post(url, {"other_id": f"https://envivo.example/events/{dup.pk}/"})
        self.assertFalse(Event.objects.filter(pk=dup.pk).exists())


class SameFestivalDayTests(BaseTestCase):
    def _payload(self, **extra):
        data = {"title": "", "event_type": "festival", "date": "2024-03-15", "venue": str(self.venue.pk),
                "eventartist_set-TOTAL_FORMS": "1", "eventartist_set-INITIAL_FORMS": "0",
                "eventartist_set-MIN_NUM_FORMS": "0", "eventartist_set-MAX_NUM_FORMS": "1000"}
        data.update(extra)
        return data

    def test_same_name_day_and_city_is_the_same_festival(self):
        fest = Production.objects.create(title="Lollapalooza Chile", event_type=EventType.FESTIVAL)
        park = Venue.objects.create(name="Parque Cerrillos", city="santiago")  # another venue, same city
        existing = Event.objects.create(title="", event_type=EventType.FESTIVAL, production=fest,
                                        date=date(2024, 3, 15), venue=park)
        EventArtist.objects.create(event=existing, artist=Artist.objects.create(name="A"))
        self.client.force_login(self.user)
        response = self.client.post(reverse("events:event_create"), self._payload(
            title="lollapalooza chile", **{"eventartist_set-0-new_artist": "B",
                                           "eventartist_set-0-billing_order": "1"}))
        self.assertRedirects(response, existing.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(Event.objects.filter(event_type=EventType.FESTIVAL).count(), 1)
        self.assertEqual(set(existing.artists.values_list("name", flat=True)), {"A", "B"})

    def test_other_day_name_or_city_stays_separate(self):
        Event.objects.create(title="Rock en Ñuñoa", event_type=EventType.FESTIVAL, date=date(2024, 3, 15), venue=self.venue)
        lima = Venue.objects.create(name="Arena", city="Lima")
        self.client.force_login(self.user)
        self.client.post(reverse("events:event_create"), self._payload(title="Rock en Nunoa", date="2024-03-16"))
        self.client.post(reverse("events:event_create"), self._payload(title="Otro festival"))
        self.client.post(reverse("events:event_create"), self._payload(title="Rock en Ñuñoa", venue=str(lima.pk)))
        self.assertEqual(Event.objects.filter(event_type=EventType.FESTIVAL).count(), 4)
        self.client.post(reverse("events:event_create"), self._payload(title="ROCK EN NUÑOA"))  # accents/caps ignored
        self.assertEqual(Event.objects.filter(event_type=EventType.FESTIVAL).count(), 4)

    def test_migration_merges_existing_duplicates(self):
        import importlib

        from django.apps import apps

        days = [Event.objects.create(title="Fest", event_type=EventType.FESTIVAL, date=date(2023, 1, 1), venue=self.venue)
                for _ in range(3)]
        bob = get_user_model().objects.create_user("bob", password="pw-test-456")
        Log.objects.create(user=bob, event=days[2], rating=8)
        elsewhere = Event.objects.create(title="Fest", event_type=EventType.FESTIVAL, date=date(2023, 1, 1),
                                         venue=Venue.objects.create(name="X", city="Lima"))
        importlib.import_module("events.migrations.0016_merge_duplicate_festival_days") \
            .merge_duplicate_festival_days(apps, None)
        remaining = set(Event.objects.filter(event_type=EventType.FESTIVAL).values_list("pk", flat=True))
        self.assertEqual(remaining, {days[0].pk, elsewhere.pk})
        self.assertTrue(Log.objects.filter(user=bob, event=days[0]).exists())


class DiaryFilterTests(BaseTestCase):
    def test_filter_by_type_and_year(self):
        comedy = Event.objects.create(title="Burr", event_type=EventType.COMEDY, date=date(2024, 5, 1))
        music24 = Event.objects.create(title="Gig 24", event_type=EventType.MUSIC, date=date(2024, 6, 1))
        for e in [self.event, comedy, music24]:
            Log.objects.create(user=self.user, event=e, rating=8)
        url = reverse("events:user_diary", args=["ana"])
        titles = lambda q: [l.event.title for l in self.client.get(url + q).context["logs"]]  # noqa: E731
        self.assertEqual(len(titles("")), 3)
        self.assertEqual(titles("?type=comedy"), ["Burr"])
        self.assertEqual(titles("?year=2024"), ["Gig 24", "Burr"])
        self.assertEqual(titles("?type=music&year=2024"), ["Gig 24"])
        page = self.client.get(url + "?type=comedy")
        self.assertEqual(page.context["log_count"], 1)  # stats follow the filter
        self.assertEqual(list(page.context["years"]), [2024, 1992])
        self.assertEqual([v for v, _ in page.context["event_types"]], ["music", "comedy"])
        self.assertContains(self.client.get(url + "?year=2001"), "No events match these filters.")


class LocalForeignAndSearchTests(BaseTestCase):
    def test_profile_splits_local_and_foreign_artists(self):
        local = Artist.objects.create(name="Los Bunkers", country="CL")
        foreign = Artist.objects.create(name="Green Day", country="US")
        unknown = Artist.objects.create(name="Mystery")
        for p in [local, foreign, unknown]:
            EventArtist.objects.create(event=self.event, artist=p)
        Log.objects.create(user=self.user, event=self.event)
        page = self.client.get(reverse("events:profile", args=["ana"]))
        self.assertIsNone(page.context["home_country"])
        self.assertContains(page, "Most seen artists")
        self.user.profile.home_country = "CL"
        self.user.profile.save()
        page = self.client.get(reverse("events:profile", args=["ana"]))
        self.assertEqual([p.name for p in page.context["local_artists"]], ["Los Bunkers"])
        self.assertEqual([p.name for p in page.context["foreign_artists"]], ["Green Day"])
        self.assertNotIn("unknown_artists", page.context)
        self.assertNotContains(page, "Mystery")  # artists without a country aren't listed
        self.assertContains(page, "Most seen local artists · Chile")

    def test_import_fills_artist_country_from_musicbrainz(self):
        with mock.patch("events.musicbrainz.artist_country", return_value="US") as lookup:
            import_setlist(self.event, SETLIST_PAYLOAD)
        self.assertEqual(Artist.objects.get(name="Green Day").country.code, "US")
        lookup.assert_called_once_with("084308bd-1654-436f-ba03-df6697104e19")

    @mock.patch("events.views_venues.search_setlists", return_value=[])
    def test_setlist_search_without_artist(self, search):
        self.client.force_login(self.user)
        self.client.get(reverse("events:setlistfm_shows"), {"artist": "", "country": "CL", "year": "2019"})
        search.assert_called_with(artist_name="", city="", year="2019", venue="", country_code="CL", page=1)
        page = self.client.get(reverse("events:setlistfm_shows"), {"artist": "", "country": ""})
        self.assertContains(page, "Fill in at least one filter")
        first_visit = self.client.get(reverse("events:setlistfm_shows"))
        self.assertNotContains(first_visit, "No setlists found")

    def test_lineup_header_is_outside_draggable_rows(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("events:event_create")).content.decode()
        rows_start = html.index('id="lineup-rows"')
        self.assertLess(html.index(">Set time</div>"), rows_start)
        self.assertNotIn('class="form-label small">Artist', html[rows_start:])


class ImportFailsSafelyTests(BaseTestCase):
    def test_musicbrainz_connection_drop_returns_no_country(self):
        from http.client import RemoteDisconnected
        from ssl import SSLError

        cache.clear()
        for error in [RemoteDisconnected("closed"), ConnectionResetError(), SSLError(), TimeoutError()]:
            with self.subTest(error=type(error).__name__):
                cache.clear()
                with mock.patch("events.musicbrainz.urlopen", side_effect=error):
                    self.assertEqual(REAL_ARTIST_COUNTRY("084308bd-1654-436f-ba03-df6697104e19"), "")

    def test_unexpected_error_is_reported_not_500(self):
        self.client.force_login(self.user)
        with mock.patch("events.setlistfm_events.get_setlist", side_effect=RuntimeError("boom")):
            response = self.client.post(reverse("events:setlistfm_shows") + "?artist=X",
                                        {"setlist_id": ["abc"]}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Something went wrong importing this setlist")


class SetlistImportSuggestsNextActTests(BaseTestCase):
    @mock.patch("events.views.search_setlists", return_value=[])
    def test_suggests_highest_billed_act_without_setlist(self, search):
        top = Artist.objects.create(name="Headliner")
        second = Artist.objects.create(name="Second")
        EventArtist.objects.create(event=self.event, artist=top, billing_order=1)
        EventArtist.objects.create(event=self.event, artist=second, billing_order=2)
        self.client.force_login(self.user)
        url = reverse("events:setlist_search", args=[self.event.pk])
        self.assertEqual(self.client.get(url).context["artist"], "Headliner")
        Setlist.objects.create(event=self.event, artist=top)
        self.assertEqual(self.client.get(url).context["artist"], "Second")
        Setlist.objects.create(event=self.event, artist=second)
        self.assertEqual(self.client.get(url).context["artist"], "Headliner")  # all done: back to the headliner


class UniqueProductionNameTests(BaseTestCase):
    def test_database_rejects_same_name_ignoring_case(self):
        from django.db import IntegrityError, transaction

        Production.objects.create(title="Freshen Up")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Production.objects.create(title="freshen up")

    def test_form_blocks_duplicate_and_links_to_existing(self):
        existing = Production.objects.create(title="Rock en Ñuñoa", event_type=EventType.FESTIVAL)
        self.client.force_login(self.user)
        page = self.client.post(reverse("events:production_create"), {"title": "ROCK EN NUNOA", "event_type": "festival"})
        self.assertContains(page, f'<a href="{existing.get_absolute_url()}">open it</a>', html=False)
        self.assertEqual(Production.objects.count(), 1)

    def test_tour_of_another_artist_gets_artist_suffix(self):
        from .models import production_named

        gd = Artist.objects.create(name="Green Day")
        pm = Artist.objects.create(name="Paul McCartney")
        first = production_named("World Tour", EventType.MUSIC, gd)
        self.assertEqual(production_named("world tour", EventType.MUSIC, gd), first)  # reused
        other = production_named("World Tour", EventType.MUSIC, pm)
        self.assertEqual(other.title, "World Tour (Paul McCartney)")
        fest = production_named("World Tour", EventType.FESTIVAL)
        self.assertEqual(fest.title, "World Tour (festival)")

class MostSeenTieBreakTests(BaseTestCase):
    def test_ties_go_to_most_recently_seen(self):
        old_fav = Artist.objects.create(name="Aaa Old")
        recent = Artist.objects.create(name="Zzz Recent")
        old_venue = Venue.objects.create(name="Aaa Hall", city="Santiago")
        new_venue = Venue.objects.create(name="Zzz Arena", city="Santiago")
        for artist, venue, when in [(old_fav, old_venue, date(2010, 1, 1)), (recent, new_venue, date(2024, 1, 1))]:
            e = Event.objects.create(title=artist.name, venue=venue, date=when)
            EventArtist.objects.create(event=e, artist=artist)
            Log.objects.create(user=self.user, event=e)
        page = self.client.get(reverse("events:profile", args=["ana"]))
        self.assertEqual([p.name for p in page.context["top_artists"]], ["Zzz Recent", "Aaa Old"])
        self.assertEqual([v.name for v in page.context["top_venues"]][:2], ["Zzz Arena", "Aaa Hall"])


class ProductionPhotoTests(BaseTestCase):
    def test_event_photo_fallback_order(self):
        artist = Artist.objects.create(name="Paul McCartney", image_url="https://img.example/artist.jpg")
        tour = Production.objects.create(title="Got Back", artist=artist)
        link = EventArtist.objects.create(event=self.event, artist=artist)
        self.assertEqual(self.event.display_image, "https://img.example/artist.jpg")
        link.production = tour
        link.save()
        tour.image_url = "https://img.example/tour.jpg"
        tour.save()
        self.assertEqual(Event.objects.get(pk=self.event.pk).display_image, "https://img.example/tour.jpg")
        fest = Production.objects.create(title="Lolla", event_type=EventType.FESTIVAL, image_url="https://img.example/fest.jpg")
        self.event.production = fest
        self.event.save()
        self.assertEqual(Event.objects.get(pk=self.event.pk).display_image, "https://img.example/fest.jpg")
        self.event.image_url = "https://img.example/own.jpg"
        self.assertEqual(self.event.display_image, "https://img.example/own.jpg")

    @mock.patch("events.image_search._get_json", side_effect=lambda url, params, **kwargs: {})
    def test_find_photo_for_production(self, _):
        cache.clear()
        fest = Production.objects.create(title="Lolla", event_type=EventType.FESTIVAL)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(fest.get_absolute_url()), "Find photo")
        self.assertEqual(self.client.get(reverse("events:production_photo", args=[fest.pk])).status_code, 200)
        self.client.post(reverse("events:production_photo", args=[fest.pk]), {"url": "https://img.example/f.jpg"})
        fest.refresh_from_db()
        self.assertEqual(fest.image_url, "https://img.example/f.jpg")
        # changing an existing photo is admin-only
        self.assertEqual(self.client.get(reverse("events:production_photo", args=[fest.pk])).status_code, 403)


class BulkImportTests(BaseTestCase):
    LINES = ("2019-03-20, Paul McCartney, music\n"
             "15/03/2024, Lollapalooza Chile, festival\n"
             '2023-11-02, "Burr, Bill", Stand-up\n'
             "# comments are ignored\n"
             "2010-05-01, Green Day\n")

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_parse_formats_types_and_errors(self):
        from .views_bulk import parse_lines

        rows = parse_lines(self.LINES + "31-02-2020, Bad date\nnot a line\n2020-01-01, X, opera\n")
        ok = [(r.date.isoformat(), r.title, r.event_type) for r in rows if not r.error]
        self.assertEqual(ok, [("2019-03-20", "Paul McCartney", "music"), ("2024-03-15", "Lollapalooza Chile", "festival"),
                              ("2023-11-02", "Burr, Bill", "comedy"), ("2010-05-01", "Green Day", "music")])
        self.assertEqual(len([r for r in rows if r.error]), 3)

    def test_preview_then_create_then_add_venues_and_lineups(self):
        preview = self.client.post(reverse("events:bulk_import"), {"lines": self.LINES})
        self.assertContains(preview, "Create 4 events")
        self.assertEqual(Event.objects.count(), 1)  # nothing created on preview

        response = self.client.post(reverse("events:bulk_import"), {"lines": self.LINES, "confirm": "1"})
        self.assertEqual(Event.objects.count(), 5)
        self.assertIn(reverse("events:bulk_review"), response["Location"])
        macca = Event.objects.get(title="Paul McCartney")

        review = self.client.get(response["Location"])
        self.assertContains(review, f'name="venue_{macca.pk}"')
        Artist.objects.create(name="Paul McCartney")
        self.client.post(reverse("events:bulk_review"), {
            "ids": str(macca.pk), f"venue_{macca.pk}": str(self.venue.pk),
            f"artists_{macca.pk}": "paul mccartney, Opener Band",
        })
        macca.refresh_from_db()
        self.assertEqual(macca.venue, self.venue)
        self.assertEqual(list(macca.eventartist_set.order_by("billing_order").values_list("artist__name", "role")),
                         [("Paul McCartney", "headliner"), ("Opener Band", "support")])
        self.assertEqual(Artist.objects.filter(name__iexact="paul mccartney").count(), 1)

        # importing the same list again reuses the events
        self.client.post(reverse("events:bulk_import"), {"lines": self.LINES, "confirm": "1"})
        self.assertEqual(Event.objects.count(), 5)

    def test_errors_block_creation(self):
        page = self.client.post(reverse("events:bulk_import"), {"lines": "2020-13-45, Nope", "confirm": "1"})
        self.assertContains(page, "Couldn&#x27;t read the date")
        self.assertEqual(Event.objects.count(), 1)

    def test_lineup_shows_right_under_venue_line(self):
        html = self.client.get(self.event.get_absolute_url()).content.decode()
        self.assertLess(html.index("Lineup</h2>"), html.index("Reviews</h2>"))
        self.assertLess(html.index("10 November 1992"), html.index("Lineup</h2>"))



class ProfileLayoutTests(BaseTestCase):
    def test_favourites_first_ratings_last_and_cities(self):
        valpo = Venue.objects.create(name="Teatro Municipal", city="Valparaíso")
        for venue, when in [(self.venue, date(2020, 1, 1)), (self.venue, date(2021, 1, 1)), (valpo, date(2022, 1, 1))]:
            Log.objects.create(user=self.user, event=Event.objects.create(title="x", venue=venue, date=when), rating=8)
        page = self.client.get(reverse("events:profile", args=["ana"]))
        html = page.content.decode()
        self.assertLess(html.index("Favourite events"), html.index("Most visited venues"))
        self.assertLess(html.index("Recently attended"), html.index('class="rating-bars'))
        self.assertEqual([(c["event__venue__city"], c["n"]) for c in page.context["top_cities"]],
                         [("Santiago", 2), ("Valparaíso", 1)])
        self.assertContains(page, "Most visited cities")


class ProfileRankingTests(BaseTestCase):
    def test_see_all_links_open_the_whole_ranking(self):
        for i in range(7):
            event = Event.objects.create(title=f"Show {i}", venue=Venue.objects.create(name=f"Venue {i}", city=f"City {i}"),
                                         date=date(2020, 1, i + 1))
            Log.objects.create(user=self.user, event=event, rating=8)
        page = self.client.get(reverse("events:profile", args=["ana"]))
        for kind in ["venues", "cities"]:
            url = reverse("events:user_ranking", args=["ana", kind])
            self.assertContains(page, url)
            self.assertEqual(len(self.client.get(url).context["rows"]), 7)
        self.assertEqual(self.client.get(reverse("events:user_ranking", args=["ana", "artists"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("events:user_ranking", args=["ana", "nope"])).status_code, 404)


class YearHistogramTests(BaseTestCase):
    def test_events_by_year_includes_gap_years(self):
        for when in [date(2019, 3, 1), date(2019, 5, 1), date(2021, 1, 1)]:
            Log.objects.create(user=self.user, event=Event.objects.create(title="x", date=when), rating=8)
        Log.objects.create(user=self.user, event=Event.objects.create(title="undated"))
        page = self.client.get(reverse("events:profile", args=["ana"]))
        self.assertContains(page, "Events by year")
        self.assertContains(page, 'title="2019: 2 events · show in diary"')
        self.assertContains(page, 'title="2020: 0 events"')
        self.assertContains(page, 'title="2021: 1 event · show in diary"')


class FestivalCardTests(BaseTestCase):
    def test_festival_cards_show_the_headliners(self):
        fest = Event.objects.create(title="Lollapalooza Chile", event_type=EventType.FESTIVAL, date=date(2013, 4, 7))
        for order, (name, role) in enumerate([("Pearl Jam", "headliner"), ("The Black Keys", "headliner"),
                                              ("Deadmau5", "support")], start=1):
            EventArtist.objects.create(event=fest, artist=Artist.objects.create(name=name), role=role,
                                       billing_order=order)
        EventArtist.objects.create(event=self.event, artist=Artist.objects.create(name="Green Day"))
        self.assertEqual(fest.card_lineup, ["Pearl Jam", "The Black Keys"])  # support acts aren't headliners
        self.assertEqual(self.event.card_lineup, [])  # concerts already say who in the title
        Log.objects.create(user=self.user, event=fest)
        page = self.client.get(reverse("events:profile", args=["ana"]))
        self.assertContains(page, "Pearl Jam · The Black Keys</div>")


class ProfileChartLinkTests(BaseTestCase):
    def test_chart_bars_open_the_diary_filtered_by_that_rating_or_year(self):
        old = Log.objects.create(user=self.user, event=Event.objects.create(title="Old show", date=date(2019, 3, 1)),
                                 rating=7, review="Loud.")
        Log.objects.create(user=self.user, event=Event.objects.create(title="New show", date=date(2021, 1, 1)), rating=9)
        page = self.client.get(reverse("events:profile", args=["ana"]))
        diary = reverse("events:user_diary", args=["ana"])
        html = page.content.decode()
        self.assertIn(f'href="{diary}?rating=7"', html)
        self.assertIn(f'href="{diary}?year=2019"', html)
        self.assertNotIn(f'href="{diary}?rating=8"', html)  # empty bars aren't links
        self.assertNotIn(f'href="{diary}?year=2020"', html)
        # Reviews on the left, the charts in a column beside them.
        self.assertLess(html.index("Recent reviews"), html.index('class="rating-bars'))
        titles = lambda qs: [log.event.title for log in self.client.get(diary + qs).context["logs"]]
        self.assertEqual(titles("?rating=7"), ["Old show"])
        self.assertEqual(titles("?year=2021"), ["New show"])
        self.assertEqual(titles("?rating=11"), ["New show", "Old show"])  # not a rating: ignored
        filtered = self.client.get(diary + "?rating=7")
        self.assertContains(filtered, '<option value="7" selected>')
        self.assertContains(filtered, "?view=expanded&rating=7")
        self.assertEqual(old.rating, 7)

    def test_charts_elsewhere_are_not_links(self):
        Log.objects.create(user=self.user, event=self.event, rating=8)
        self.assertNotContains(self.client.get(self.event.get_absolute_url()), "?rating=8")


class ArtistListTests(BaseTestCase):
    def test_artists_tab_lists_and_searches(self):
        from events.models import Artist
        Artist.objects.create(name="Los Bunkers")
        Artist.objects.create(name="Mon Laferte")
        page = self.client.get(reverse("events:artist_list"))
        self.assertContains(page, 'href="/artists/">Artists</a>')
        self.assertNotContains(page, "+ New artist")
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("events:artist_list")), reverse("events:artist_create"))
        self.assertContains(page, "Los Bunkers")
        page = self.client.get(reverse("events:artist_list") + "?q=bunk")
        self.assertContains(page, "Los Bunkers")
        self.assertNotContains(page, "Mon Laferte")


class PlayTests(BaseTestCase):
    def test_new_staging_from_a_play_gets_its_name_and_authors(self):
        from events.models import Artist, Play
        lorca = Artist.objects.create(name="Federico García Lorca")
        play = Play.objects.create(title="La casa de Bernarda Alba")
        play.authors.add(lorca)
        self.client.force_login(self.user)
        form_page = self.client.get(reverse("events:event_create") + f"?play={play.pk}")
        self.assertEqual(form_page.context["form"].initial["play"], play)
        self.assertEqual(form_page.context["form"].initial["event_type"], EventType.THEATER)
        data = {"title": "", "event_type": EventType.THEATER, "date": "2024-05-01", "play": play.pk,
                "image_display": "fit"}
        mgmt = form_page.context["formset"].management_form
        data.update({mgmt.prefix + "-TOTAL_FORMS": "0", mgmt.prefix + "-INITIAL_FORMS": "0"})
        resp = self.client.post(reverse("events:event_create") + f"?play={play.pk}", data)
        event = Event.objects.get(play=play)
        self.assertRedirects(resp, event.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(event.title, "La casa de Bernarda Alba")
        self.assertTrue(event.eventartist_set.filter(artist=lorca, role="author").exists())
        page = self.client.get(play.get_absolute_url())
        self.assertContains(page, "Written by")
        self.assertContains(page, event.get_absolute_url())
        self.assertContains(self.client.get(event.get_absolute_url()), "Staging of")


class ExpandedDiaryTests(BaseTestCase):
    def test_expanded_diary_shows_posters_and_lineup(self):
        from events.models import Artist
        EventArtist.objects.create(event=self.event, artist=Artist.objects.create(name="Green Day"))
        EventArtist.objects.create(event=self.event, artist=Artist.objects.create(name="Supporting Act"),
                                      role="support", billing_order=2)
        self.event.image_url = "https://img.example/p.jpg"
        self.event.save()
        Log.objects.create(user=self.user, event=self.event, rating=9)
        url = reverse("events:user_diary", args=["ana"])
        regular = self.client.get(url)
        self.assertContains(regular, "?view=expanded")
        self.assertNotContains(regular, "Supporting Act")
        expanded = self.client.get(url + "?view=expanded")
        self.assertContains(expanded, "Supporting Act")
        self.assertContains(expanded, "https://img.example/p.jpg")

    def test_expanded_lineup_lists_only_the_acts_they_saw(self):
        headliner = Artist.objects.create(name="Green Day")
        opener = Artist.objects.create(name="Missed Opener")
        EventArtist.objects.create(event=self.event, artist=headliner)
        EventArtist.objects.create(event=self.event, artist=opener, role="support", billing_order=2)
        play = Event.objects.create(title="Bodas de sangre", event_type=EventType.THEATER, date=date(1993, 1, 1))
        EventArtist.objects.create(event=play, artist=Artist.objects.create(name="Lorca"), role="author")
        EventArtist.objects.create(event=play, artist=Artist.objects.create(name="Lead Actor"))
        log = Log.objects.create(user=self.user, event=self.event)
        log.acts.filter(artist=opener).delete()  # got there after the opener
        Log.objects.create(user=self.user, event=play)
        page = self.client.get(reverse("events:user_diary", args=["ana"]) + "?view=expanded")
        self.assertContains(page, "Green Day</a>")
        self.assertNotContains(page, "Missed Opener")
        self.assertContains(page, "Lorca")  # credits aren't acts: always listed
        self.assertContains(page, "Lead Actor")


class ArtistSortTests(BaseTestCase):
    def test_country_filter_and_sorts(self):
        from django.contrib.auth import get_user_model
        from events.models import ActLog
        old = Artist.objects.create(name="Old Band", country="CL")
        new = Artist.objects.create(name="New Band", country="AR")
        e1 = Event.objects.create(title="a", date=date(2001, 1, 1))
        e2 = Event.objects.create(title="b", date=date(2023, 1, 1))
        e3 = Event.objects.create(title="c", date=date(2002, 1, 1))
        for e in (e1, e3):
            EventArtist.objects.create(event=e, artist=old)
        EventArtist.objects.create(event=e2, artist=new)
        bob = get_user_model().objects.create_user("bob", password="pw-test-123")
        for u in (self.user, bob):
            ActLog.objects.update_or_create(log=Log.objects.create(user=u, event=e2), artist=new,
                                            defaults={"rating": 10})
        ActLog.objects.update_or_create(log=Log.objects.create(user=self.user, event=e1), artist=old,
                                        defaults={"rating": 4})
        url = reverse("events:artist_list")
        names = lambda qs: [p.name for p in self.client.get(url + qs).context["page"]]
        self.assertEqual(names(""), ["Old Band", "New Band"])
        self.assertEqual(names("?sort=popular"), ["New Band", "Old Band"])
        self.assertEqual(names("?sort=rated"), ["New Band", "Old Band"])
        self.assertEqual(names("?sort=recent"), ["New Band", "Old Band"])
        self.assertEqual(names("?country=CL"), ["Old Band"])
        Artist.objects.create(name="Unknown origin")
        self.assertEqual(names("?country=NONE"), ["Unknown origin"])


class PhotoFallbackTests(BaseTestCase):
    def test_no_photo_uses_first_person_on_the_bill_with_one(self):
        lead = Artist.objects.create(name="Lead, no photo")
        director = Artist.objects.create(name="Director", image_url="https://img.example/dir.jpg")
        late = Artist.objects.create(name="Later", image_url="https://img.example/late.jpg")
        EventArtist.objects.create(event=self.event, artist=lead, billing_order=1)
        EventArtist.objects.create(event=self.event, artist=director, role="director", billing_order=2)
        EventArtist.objects.create(event=self.event, artist=late, role="cast", billing_order=3)
        self.assertEqual(self.event.display_image, "https://img.example/dir.jpg")
        self.event.image_url = "https://img.example/own.jpg"
        self.assertEqual(self.event.display_image, "https://img.example/own.jpg")


class DiaryPaginationTests(BaseTestCase):
    def test_100_per_page_regular_25_expanded(self):
        for i in range(101):
            Log.objects.create(user=self.user, event=Event.objects.create(title=f"Show {i}", date=date(2020, 1, 1)))
        url = reverse("events:user_diary", args=["ana"])
        regular = self.client.get(url)
        self.assertEqual(len(regular.context["page"]), 100)
        self.assertEqual(regular.context["log_count"], 101)  # stats still cover everything
        self.assertEqual(len(self.client.get(url + "?page=2").context["page"]), 1)
        expanded = self.client.get(url + "?view=expanded")
        self.assertEqual(len(expanded.context["page"]), 25)
        self.assertContains(expanded, "view=expanded&page=2")


class EventActionModalTests(BaseTestCase):
    def test_actions_are_buttons_that_open_modals(self):
        Log.objects.create(user=self.user, event=self.event, rating=8)
        self.client.force_login(self.user)
        html = self.client.get(self.event.get_absolute_url()).content.decode()
        self.assertIn('data-bs-target="#logModal"', html)
        self.assertIn('id="logModal"', html)
        self.assertIn('data-bs-target="#listModal"', html)
        self.assertIn("data-confirm=", html)  # remove from diary asks first
        self.assertNotIn("onsubmit=", html)
        # actions sit in the right bar, after the ratings card
        self.assertLess(html.index('class="event-side"'), html.index('data-bs-target="#logModal"'))


class LineupSetlistTests(BaseTestCase):
    def test_lineup_links_to_each_acts_setlist_page(self):
        from unittest import mock
        from events.models import SetlistSong, Song
        band = Artist.objects.create(name="Green Day")
        guest = Artist.objects.create(name="Not on the bill")
        EventArtist.objects.create(event=self.event, artist=band)
        setlist = Setlist.objects.create(event=self.event, artist=band)
        Setlist.objects.create(event=self.event, artist=guest)
        dookie = [Song.objects.create(artist=band, title=t) for t in ["Basket Case", "Welcome to Paradise"]]
        later = Song.objects.create(artist=band, title="Holiday")
        for i, song in enumerate(dookie + [later], start=1):
            SetlistSong.objects.create(setlist=setlist, song=song, position=i)
        page = self.client.get(self.event.get_absolute_url()).content.decode()
        url = reverse("events:act_setlist", args=[self.event.pk, band.pk])
        self.assertIn(url, page)
        self.assertIn(reverse("events:act_setlist", args=[self.event.pk, guest.pk]), page)
        self.assertNotIn("Welcome to Paradise", page)  # songs live on the setlist page now

        self.assertEqual(len(self.client.get(url).context["pending"]), 3)
        albums = {"Basket Case": {"title": "Dookie", "year": 1994, "mbid": "x"},
                  "Welcome to Paradise": {"title": "Dookie", "year": 1994, "mbid": "x"},
                  "Holiday": {"title": "American Idiot", "year": 2004, "mbid": "y"}}
        with mock.patch("events.musicbrainz.song_album", side_effect=lambda t, *a: albums[t]):
            for song in dookie + [later]:
                self.assertEqual(self.client.post(reverse("events:song_album_lookup", args=[song.pk])).status_code, 200)
        resp = self.client.get(url)
        self.assertEqual(resp.context["pending"], [])
        self.assertEqual((resp.context["top_albums"][0]["title"], resp.context["top_albums"][0]["n"]), ("Dookie", 2))
        self.assertContains(resp, "Most played album")
        self.assertContains(resp, f'<a class="link-light" href="{band.get_absolute_url()}">Green Day</a>: setlist')
        self.assertEqual([(a["title"], a["n"], a["pct"]) for a in resp.context["timeline"]],
                         [("American Idiot", 1, 50), ("Dookie", 2, 100)])  # newest first by default
        by_count = self.client.get(url + "?sort=count").context["timeline"]
        self.assertEqual([a["title"] for a in by_count], ["Dookie", "American Idiot"])
        self.assertNotContains(resp, "Edit albums")  # admins only

    def test_admin_fixes_albums_for_the_whole_setlist_at_once(self):
        from events.models import SetlistSong, Song
        band = Artist.objects.create(name="Band")
        EventArtist.objects.create(event=self.event, artist=band)
        setlist = Setlist.objects.create(event=self.event, artist=band)
        wrong = Song.objects.create(artist=band, title="Tune", album_title="Wrong", album_mbid="abc")
        fine = Song.objects.create(artist=band, title="Other", album_title="fine", album_year=1999, album_mbid="def")
        right = band.albums.create(title="Right Album", year=2001)
        fine_album = band.albums.create(title="Fine", year=1999)
        for i, song in enumerate([wrong, fine], start=1):
            SetlistSong.objects.create(setlist=setlist, song=song, position=i)
        page_url = reverse("events:act_setlist", args=[self.event.pk, band.pk])
        edit = reverse("events:setlist_albums_edit", args=[self.event.pk, band.pk])
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(page_url), "Edit albums")
        self.assertEqual(self.client.post(edit, {f"album_{wrong.pk}": right.pk}).status_code, 403)
        self.user.is_superuser = True
        self.user.save()
        page = self.client.get(page_url).content.decode()
        self.assertEqual(page.count("Edit albums"), 2)  # one button + the modal title, not one per song
        self.assertIn(f'<select class="form-select form-select-sm" id="album-{wrong.pk}"', page)
        self.assertIn("Wrong · not in Manage albums", page)  # an unlisted album stays until another is picked
        self.assertIn(f'<option value="{fine_album.pk}" selected>Fine (1999)</option>', page)  # matched ignoring case
        self.client.post(edit, {f"album_{wrong.pk}": str(right.pk), f"album_{fine.pk}": str(fine_album.pk)})
        wrong.refresh_from_db()
        fine.refresh_from_db()
        self.assertEqual((wrong.album_title, wrong.album_year, wrong.album_mbid), ("Right Album", 2001, ""))
        self.assertIsNotNone(wrong.album_checked_at)
        self.assertEqual((fine.album_title, fine.album_mbid), ("fine", "def"))  # unchanged rows are left alone

        other = Artist.objects.create(name="Someone else").albums.create(title="Not theirs")
        self.client.post(edit, {f"album_{wrong.pk}": str(other.pk)})  # only the act's (and its bands') albums
        self.client.post(edit, {f"album_{fine.pk}": "keep"})
        wrong.refresh_from_db()
        fine.refresh_from_db()
        self.assertEqual(wrong.album_title, "Right Album")
        self.assertEqual(fine.album_title, "fine")
        self.client.post(edit, {f"album_{wrong.pk}": ""})
        wrong.refresh_from_db()
        self.assertEqual((wrong.album_title, wrong.album_year), ("", None))

    def test_busy_musicbrainz_leaves_song_unchecked(self):
        from unittest import mock
        from events.models import SetlistSong, Song
        song = Song.objects.create(artist=Artist.objects.create(name="X"), title="Y")
        with mock.patch("events.musicbrainz.song_album", side_effect=OSError("busy")):
            self.assertEqual(self.client.post(reverse("events:song_album_lookup", args=[song.pk])).status_code, 503)
        song.refresh_from_db()
        self.assertIsNone(song.album_checked_at)


class MusicOnlySetlistTests(BaseTestCase):
    def test_non_music_events_have_no_setlists(self):
        play = Event.objects.create(title="Hamlet", event_type=EventType.THEATER, date=date(2020, 1, 1))
        actor = Artist.objects.create(name="Actor")
        EventArtist.objects.create(event=play, artist=actor, role="cast")
        Setlist.objects.create(event=play, artist=actor)
        self.client.force_login(self.user)
        page = self.client.get(play.get_absolute_url())
        self.assertNotContains(page, "Setlist")
        self.assertEqual(self.client.get(reverse("events:setlist_search", args=[play.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("events:act_setlist", args=[play.pk, actor.pk])).status_code, 404)


class FestivalAddArtistsTests(BaseTestCase):
    def test_suggests_setlistfm_acts_from_that_city_and_day(self):
        fest = Event.objects.create(title="Lolla", event_type=EventType.FESTIVAL, venue=self.venue, date=date(2024, 3, 15),
                                    created_by=self.user)
        EventArtist.objects.create(event=fest, artist=Artist.objects.create(name="Already On"))
        results = [
            {"id": "sfm1", "artist": {"name": "Same Venue Band", "mbid": ""}, "venue": {"name": "Teatro Caupolicán"},
             "sets": {"set": [{"song": [{"name": "Opening song"}, {"name": "Closer"}]}]}},
            {"artist": {"name": "City Band", "mbid": ""}, "venue": {"name": "Movistar Arena"}},
            {"artist": {"name": "Already On", "mbid": ""}, "venue": {"name": "Teatro Caupolicán"}},
            {"artist": {"name": "same venue band", "mbid": ""}, "venue": {"name": "Teatro Caupolicán"}},
        ]
        self.client.force_login(self.user)
        url = reverse("events:festival_add_artists", args=[fest.pk])
        self.assertContains(self.client.get(fest.get_absolute_url()), url)
        with mock.patch("events.views.search_setlists", return_value=results) as search:
            page = self.client.get(url)
            self.assertEqual(search.call_args.kwargs, {"on_date": fest.date, "page": 1, "city": "Santiago"})
            self.assertEqual([c["name"] for c in page.context["candidates"]], ["Same Venue Band", "City Band"])
            self.assertTrue(page.context["candidates"][0]["same_venue"])
            key = page.context["candidates"][0]["key"]
            self.client.post(url, {f"add_{key}": "1"})
        self.assertTrue(fest.eventartist_set.filter(artist__name="Same Venue Band", role="support").exists())
        imported = fest.setlists.get(artist__name="Same Venue Band")
        self.assertEqual(list(imported.entries.values_list("song__title", flat=True)), ["Opening song", "Closer"])
        self.assertFalse(fest.eventartist_set.filter(artist__name="City Band").exists())
        with mock.patch("events.views.search_setlists", return_value=results):
            key = self.client.get(url).context["candidates"][0]["key"]  # City Band now
            self.client.post(url, {f"add_{key}": "1", f"role_{key}": "headliner"})
        self.assertEqual(list(fest.eventartist_set.order_by("billing_order").values_list("artist__name", "role")),
                         [("Already On", "headliner"), ("City Band", "headliner"), ("Same Venue Band", "support")])

    def test_venues_found_that_day_are_listed_to_tick_all_their_artists(self):
        fest = Event.objects.create(title="Lolla", event_type=EventType.FESTIVAL, venue=self.venue, date=date(2024, 3, 15),
                                    created_by=self.user)
        results = [
            {"artist": {"name": "A"}, "venue": {"name": "Movistar Arena"}},
            {"artist": {"name": "B"}, "venue": {"name": "Movistar Arena"}},
            {"artist": {"name": "C"}, "venue": {"name": "Teatro Caupolicán"}},
        ]
        self.client.force_login(self.user)
        with mock.patch("events.views.search_setlists", return_value=results):
            page = self.client.get(reverse("events:festival_add_artists", args=[fest.pk]))
        self.assertEqual([(v["name"], v["count"]) for v in page.context["venues"]],
                         [("Teatro Caupolicán", 1), ("Movistar Arena", 2)])  # the festival's own venue first
        self.assertContains(page, 'class="form-check-input venue-pick"', count=2)
        self.assertContains(page, 'data-venue="movistar arena"', count=3)  # the box + its two artists

    def test_search_by_venue_or_festival_name_as_tour(self):
        fest_prod = Production.objects.create(title="Lollapalooza Chile", event_type=EventType.FESTIVAL)
        fest = Event.objects.create(title="Lollapalooza Chile", event_type=EventType.FESTIVAL, venue=self.venue,
                                    production=fest_prod, date=date(2024, 3, 15), created_by=self.user)
        url = reverse("events:festival_add_artists", args=[fest.pk])
        self.client.force_login(self.user)
        with mock.patch("events.views.search_setlists", return_value=[]) as search:
            page = self.client.get(url + "?mode=venue")
            self.assertEqual(search.call_args.kwargs, {"on_date": fest.date, "page": 1, "venue": "Teatro Caupolicán"})
            self.client.get(url + "?mode=tour")
            self.assertEqual(search.call_args.kwargs["tour"], "Lollapalooza Chile")
            self.client.get(url + "?mode=venue&q=Parque+O%27Higgins")
            self.assertEqual(search.call_args.kwargs["venue"], "Parque O'Higgins")
        self.assertContains(page, "Venue + date")
        self.assertContains(page, "Festival name as tour + date")
        self.assertContains(page, 'id="mode-venue" data-default="Teatro Caupolicán" checked')

    def test_tour_name_goes_to_setlistfm(self):
        from events import setlistfm
        with mock.patch.object(setlistfm, "_request_json", return_value={"setlist": []}) as req:
            setlistfm.search_setlists(tour="Lollapalooza Chile", on_date=date(2024, 3, 15))
        self.assertEqual(req.call_args.args[1]["tourName"], "Lollapalooza Chile")

    def test_setlistfm_error_is_shown(self):
        fest = Event.objects.create(title="Lolla", event_type=EventType.FESTIVAL, venue=self.venue, date=date(2024, 3, 15),
                                    created_by=self.user)
        self.client.force_login(self.user)
        with mock.patch("events.views.search_setlists", side_effect=SetlistFmError("Setlist.fm error 403")):
            self.assertContains(self.client.get(reverse("events:festival_add_artists", args=[fest.pk])), "Setlist.fm error 403")


class EditFormPhotoTests(BaseTestCase):
    def test_edit_form_shows_the_photo(self):
        self.event.image_url = "https://img.example/poster.jpg"
        self.event.created_by = self.user
        self.event.save()
        self.client.force_login(self.user)
        page = self.client.get(reverse("events:event_edit", args=[self.event.pk]))
        self.assertContains(page, 'src="https://img.example/poster.jpg"')


class ArtistSongStatsTests(BaseTestCase):
    def test_most_played_songs_and_albums_all_and_seen_by(self):
        from events.models import ActLog, SetlistSong, Song
        band = Artist.objects.create(name="Band")
        hit = Song.objects.create(artist=band, title="Hit", album_title="First", album_year=2000)
        deep = Song.objects.create(artist=band, title="Deep cut", album_title="Second", album_year=2005)
        shows = []
        for i, songs in enumerate([[hit, deep], [hit], [hit]]):
            e = Event.objects.create(title=f"Show {i}", date=date(2020, 1, i + 1))
            EventArtist.objects.create(event=e, artist=band)
            sl = Setlist.objects.create(event=e, artist=band)
            for pos, song in enumerate(songs, start=1):
                SetlistSong.objects.create(setlist=sl, song=song, position=pos)
            shows.append(e)
        log = Log.objects.create(user=self.user, event=shows[0])
        ActLog.objects.get_or_create(log=log, artist=band)
        page = self.client.get(band.get_absolute_url())
        self.assertEqual([(s["song__title"], s["n"]) for s in page.context["top_songs"]], [("Hit", 3), ("Deep cut", 1)])
        self.assertEqual(page.context["top_albums"][0]["title"], "First")
        self.assertContains(page, reverse("events:artist_songs", args=[band.pk]))
        mine = self.client.get(band.get_absolute_url() + "?seen_by=ana")
        self.assertEqual([(s["song__title"], s["n"]) for s in mine.context["top_songs"]], [("Deep cut", 1), ("Hit", 1)])
        full = self.client.get(reverse("events:artist_songs", args=[band.pk]) + "?tab=albums&seen_by=ana")
        self.assertEqual(full.context["shows"], 1)
        self.assertEqual([a["title"] for a in full.context["albums"]], ["First", "Second"])  # 1 play each: older first


class FestivalPhotoPickerTests(BaseTestCase):
    def test_picked_photo_can_go_to_every_day_of_the_festival(self):
        fest = Production.objects.create(title="Lolla", event_type=EventType.FESTIVAL)
        days = [Event.objects.create(title="Lolla", event_type=EventType.FESTIVAL, production=fest,
                                     date=date(2024, 3, d), created_by=self.user) for d in (15, 16, 17)]
        next_year = Event.objects.create(title="Lolla", event_type=EventType.FESTIVAL, production=fest,
                                         date=date(2025, 3, 15), created_by=self.user)
        self.client.force_login(self.user)
        url = reverse("events:event_photo", args=[days[0].pk])
        with mock.patch("events.image_search.find_candidates", return_value=([], [])):
            page = self.client.get(url)
        self.assertContains(page, "Use this photo for every day of Lolla in 2024 (2 other days)")
        self.assertContains(page, 'id="photo-all-days" checked')  # on by default
        self.client.post(url, {"url": "https://img.example/one.jpg"})
        days[1].refresh_from_db()
        self.assertEqual(days[1].image_url, "")  # toggle off: only this day
        self.client.post(url, {"url": "https://img.example/all.jpg", "all_days": "1"})
        for d in days[1:]:
            d.refresh_from_db()
            self.assertEqual(d.image_url, "https://img.example/all.jpg")
        next_year.refresh_from_db()
        self.assertEqual(next_year.image_url, "")

    def test_no_toggle_for_a_one_off_show(self):
        self.event.created_by = self.user
        self.event.save()
        EventArtist.objects.create(event=self.event, artist=Artist.objects.create(name="Green Day"))
        self.client.force_login(self.user)
        with mock.patch("events.image_search.find_candidates", return_value=([], [])):
            self.assertNotContains(self.client.get(reverse("events:event_photo", args=[self.event.pk])), "photo-all-days")

    def test_headliners_other_shows_in_the_same_city_and_year_share_the_photo(self):
        mc = Artist.objects.create(name="Paul McCartney")
        other_venue = Venue.objects.create(name="Estadio Nacional", city="santiago", country="CL")
        elsewhere = Venue.objects.create(name="Estadio Monumental", city="Lima", country="PE")

        def show(day, venue, year=2019, role=EventArtist.Role.HEADLINER, **extra):
            e = Event.objects.create(title=f"Show {day}", date=date(year, 4, day), venue=venue, created_by=self.user,
                                     **extra)
            EventArtist.objects.create(event=e, artist=mc, role=role)
            return e

        first, second = show(1, self.venue), show(2, other_venue)  # Santiago, both headlined by him
        lima = show(3, elsewhere)
        next_year = show(4, self.venue, year=2020)
        as_support = show(5, self.venue, role=EventArtist.Role.SUPPORT)
        fest = show(6, self.venue, event_type=EventType.FESTIVAL)

        self.client.force_login(self.user)
        url = reverse("events:event_photo", args=[first.pk])
        with mock.patch("events.image_search.find_candidates", return_value=([], [])):
            page = self.client.get(url)
        self.assertContains(page, "Use this photo for Paul McCartney&#x27;s other show in Santiago in 2019 (1 other show)")
        self.assertContains(page, 'id="photo-all-days" checked')
        self.client.post(url, {"url": "https://img.example/mc.jpg", "all_days": "1"}, follow=True)
        second.refresh_from_db()
        self.assertEqual(second.image_url, "https://img.example/mc.jpg")
        for e in (lima, next_year, as_support, fest):
            e.refresh_from_db()
            self.assertEqual(e.image_url, "", e.title)

        # The edit form offers it too, checked, but only shares when the photo changes.
        edit = reverse("events:event_edit", args=[second.pk])
        self.assertContains(self.client.get(edit), "Use this photo for Paul McCartney&#x27;s other show in santiago")
        form = {"title": "", "event_type": "music", "date": "2019-04-02", "venue": str(other_venue.pk),
                "image_url": "https://img.example/mc.jpg", "image_display": second.image_display, "photo_for_all_days": "on",
                "eventartist_set-TOTAL_FORMS": "1", "eventartist_set-INITIAL_FORMS": "1",
                "eventartist_set-MIN_NUM_FORMS": "0", "eventartist_set-MAX_NUM_FORMS": "1000",
                "eventartist_set-0-id": str(second.eventartist_set.get().pk),
                "eventartist_set-0-artist": str(mc.pk), "eventartist_set-0-role": "headliner",
                "eventartist_set-0-billing_order": "1"}
        first.image_url = "https://img.example/its-own.jpg"
        first.save()
        self.client.post(edit, form)  # photo unchanged: the first show keeps its own
        first.refresh_from_db()
        self.assertEqual(first.image_url, "https://img.example/its-own.jpg")
        self.client.post(edit, {**form, "image_url": "https://img.example/new.jpg"})
        first.refresh_from_db()
        self.assertEqual(first.image_url, "https://img.example/new.jpg")


class ProductionThumbnailTests(BaseTestCase):
    def test_each_date_shows_a_thumbnail_or_a_no_photo_marker(self):
        fest = Production.objects.create(title="Lolla", event_type=EventType.FESTIVAL)
        Event.objects.create(title="Lolla", event_type=EventType.FESTIVAL, production=fest, date=date(2024, 3, 15),
                             image_url="https://img.example/day1.jpg")
        Event.objects.create(title="Lolla", event_type=EventType.FESTIVAL, production=fest, date=date(2024, 3, 16))
        page = self.client.get(fest.get_absolute_url())
        self.assertContains(page, 'src="https://img.example/day1.jpg"')
        self.assertContains(page, "No photo", count=1)


class BillOrderTests(BaseTestCase):
    def test_headliners_go_above_support_and_credits_last(self):
        names = ["Opener", "Director", "Late headliner", "Second opener"]
        roles = ["support", "director", "headliner", "support"]
        for i, (n, r) in enumerate(zip(names, roles), start=1):
            EventArtist.objects.create(event=self.event, artist=Artist.objects.create(name=n), role=r, billing_order=i)
        self.event.order_bill()
        self.assertEqual(list(self.event.eventartist_set.order_by("billing_order").values_list("artist__name", flat=True)),
                         ["Late headliner", "Opener", "Second opener", "Director"])


class TapesAndAlbumsTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        from events.models import SetlistSong, Song
        self.band = Artist.objects.create(name="Band")
        EventArtist.objects.create(event=self.event, artist=self.band)
        setlist = Setlist.objects.create(event=self.event, artist=self.band)
        intro = Song.objects.create(artist=self.band, title="Intro tape", album_title="First", album_year=2000)
        hit = Song.objects.create(artist=self.band, title="Hit", album_title="First", album_year=2000)
        SetlistSong.objects.create(setlist=setlist, song=intro, position=1, is_tape=True)
        SetlistSong.objects.create(setlist=setlist, song=hit, position=2)
        Album.objects.create(artist=self.band, title="Unplayed", year=2010)
        self.url = reverse("events:act_setlist", args=[self.event.pk, self.band.pk])

    def test_tapes_are_listed_but_not_counted(self):
        page = self.client.get(self.url)
        self.assertEqual(page.context["song_count"], 1)
        self.assertEqual(page.context["top_albums"][0]["n"], 1)
        self.assertContains(page, ">tape<")
        html = page.content.decode()
        self.assertIn("is-tape", html)
        self.assertEqual([e.number for e in page.context["entries"]], [None, 1])  # tape isn't numbered
        self.assertContains(self.client.get(self.event.get_absolute_url()), "Setlist · 1 song →")
        songs = self.client.get(reverse("events:artist_songs", args=[self.band.pk])).context["songs"]
        self.assertEqual([s["song__title"] for s in songs], ["Hit"])

    def test_setlist_chart_leaves_out_albums_released_after_the_show(self):
        Album.objects.create(artist=self.band, title="Future", year=2030)  # event is in 1992
        Album.objects.filter(title="Unplayed").update(year=1990)
        titles = [r["title"] for r in self.client.get(self.url).context["timeline"]]
        self.assertIn("Unplayed", titles)
        self.assertNotIn("Future", titles)
        self.assertIn("First", titles)  # played songs always count, whatever the album's year

    def test_empty_albums_show_unless_skipped(self):
        self.event.date = date(2020, 1, 1)  # after the "Unplayed" album (2010)
        self.event.save()
        rows = self.client.get(self.url).context["timeline"]
        self.assertEqual([(r["title"], r["n"]) for r in rows], [("Unplayed", 0), ("First", 1)])
        skipped = self.client.get(self.url + "?empty=skip").context["timeline"]
        self.assertEqual([r["title"] for r in skipped], ["First"])
        artist_page = self.client.get(self.band.get_absolute_url())
        self.assertEqual([r["title"] for r in artist_page.context["album_chart"]], ["Unplayed", "First"])
        self.assertContains(artist_page, "Skip empty albums")

    def test_admins_add_and_remove_albums(self):
        edit = reverse("events:artist_albums_edit", args=[self.band.pk])
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(edit, {"title": "New"}).status_code, 403)
        self.user.is_superuser = True
        self.user.save()
        self.assertContains(self.client.get(self.band.get_absolute_url()), "Manage albums")
        self.client.post(edit, {"title": "  Third  Album ", "year": "2015"})
        self.client.post(edit, {"title": "third album"})  # same title: ignored
        self.assertEqual(list(self.band.albums.filter(title__icontains="third").values_list("title", "year")),
                         [("Third Album", 2015)])
        self.client.post(edit, {"delete": self.band.albums.get(title="Unplayed").pk})
        self.assertFalse(self.band.albums.filter(title="Unplayed").exists())

    def test_albums_tab_is_one_chart_with_plays_and_songs_and_an_add_button(self):
        url = reverse("events:artist_songs", args=[self.band.pk]) + "?tab=albums"
        page = self.client.get(url).content.decode()
        self.assertIn('1 <span class="opacity-75">/ 1</span>', page)  # First: 1 play / 1 song
        self.assertNotIn("1 play · 1 song", page)  # no second list repeating the chart
        self.assertNotIn("addAlbumModal", page)
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        self.assertContains(self.client.get(url), "+ Add album")

    def test_songs_are_filed_under_listed_albums_only(self):
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        hit = self.band.songs.get(title="Hit")
        before = self.band.albums.count()
        self.client.post(reverse("events:setlist_albums_edit", args=[self.event.pk, self.band.pk]),
                         {f"album_{hit.pk}": "Second"})  # not an album pick: nothing is created
        self.assertEqual(self.band.albums.count(), before)
        second = self.band.albums.create(title="Second", year=2005)
        self.client.post(reverse("events:setlist_albums_edit", args=[self.event.pk, self.band.pk]),
                         {f"album_{hit.pk}": str(second.pk)})
        hit.refresh_from_db()
        self.assertEqual((hit.album_title, hit.album_year), ("Second", 2005))


class PosterSortTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.event.image_url = "https://img.example/poster.jpg"
        self.event.created_by = self.user
        self.event.save()
        self.url = reverse("events:event_poster_image", args=[self.event.pk])

    def test_edit_form_has_the_poster_button(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("events:event_edit", args=[self.event.pk]))
        self.assertContains(page, 'id="poster-sort"')
        self.assertContains(page, self.url)
        self.event.image_url = ""
        self.event.save()
        self.assertNotContains(self.client.get(reverse("events:event_edit", args=[self.event.pk])), 'id="poster-sort"')

    def test_poster_is_proxied_for_editors_only(self):
        class FakeResponse:
            headers = {"Content-Type": "image/jpeg"}
            def read(self, n): return b"jpegdata"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        opener = mock.Mock()
        opener.open.return_value = FakeResponse()
        with mock.patch("events.views_venues._public_https_url", return_value=True), \
             mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(self.client.get(self.url).status_code, 302)  # login first
            other = get_user_model().objects.create_user("bob", password="pw-test-123")
            self.client.force_login(other)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.client.force_login(self.user)
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/jpeg")
        self.assertEqual(resp.content, b"jpegdata")

    def test_private_or_plain_http_hosts_are_refused(self):
        from events.views_venues import _public_https_url
        self.assertFalse(_public_https_url("http://example.com/a.jpg"))
        self.assertFalse(_public_https_url("https://127.0.0.1/a.jpg"))
        self.assertFalse(_public_https_url("https://10.0.0.5/a.jpg"))
        self.event.image_url = "https://localhost/a.jpg"
        self.event.save()
        self.client.force_login(self.user)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"public sites", resp.content)

    def test_download_problems_are_explained(self):
        from urllib.error import HTTPError
        opener = mock.Mock()
        opener.open.side_effect = HTTPError(self.event.image_url, 403, "Forbidden", {}, None)
        self.client.force_login(self.user)
        with mock.patch("events.views_venues._public_https_url", return_value=True), \
             mock.patch("urllib.request.build_opener", return_value=opener):
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.content, b"The photo's site answered 403.")
        self.assertEqual(opener.open.call_count, 2)  # retried with a browser-like user agent

    def test_images_served_as_octet_stream_are_recognised(self):
        from events.views_venues import _image_type
        self.assertEqual(_image_type(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(_image_type(b"<html>"), "")


class SongShowsSinceReleaseTests(BaseTestCase):
    def test_song_totals_only_count_shows_since_release_and_albums_open_to_songs(self):
        from events.models import SetlistSong, Song
        band = Artist.objects.create(name="Band")
        old = Song.objects.create(artist=band, title="Old hit", album_title="Debut", album_year=2000)
        new = Song.objects.create(artist=band, title="New single", album_title="Comeback", album_year=2020)
        for year, songs in [(2005, [old]), (2010, [old]), (2021, [old, new]), (2022, [new])]:
            e = Event.objects.create(title=f"Show {year}", date=date(year, 1, 1))
            EventArtist.objects.create(event=e, artist=band)
            sl = Setlist.objects.create(event=e, artist=band)
            for pos, song in enumerate(songs, start=1):
                SetlistSong.objects.create(setlist=sl, song=song, position=pos)
        page = self.client.get(band.get_absolute_url())
        rows = {s["song__title"]: (s["n"], s["of"]) for s in page.context["top_songs"]}
        self.assertEqual(rows, {"Old hit": (3, 4), "New single": (2, 2)})  # new song: only 2021 and 2022 count
        self.assertEqual([s["song__title"] for s in page.context["top_songs"]], ["Old hit", "New single"])  # by plays
        self.assertContains(page, "2 / 2")
        comeback = next(r for r in page.context["album_chart"] if r["title"] == "Comeback")
        self.assertEqual(comeback["tracks"], [{"title": "New single", "n": 2, "of": 2}])
        self.assertContains(page, '<details class="album-item"')


class NoAlbumRowTests(BaseTestCase):
    def test_songs_without_album_get_a_last_no_album_row(self):
        from events.models import SetlistSong, Song
        band = Artist.objects.create(name="Band")
        EventArtist.objects.create(event=self.event, artist=band)
        sl = Setlist.objects.create(event=self.event, artist=band)
        on_album = Song.objects.create(artist=band, title="Hit", album_title="First", album_year=1990)
        loose = Song.objects.create(artist=band, title="Cover song")
        for i, song in enumerate([on_album, loose], start=1):
            SetlistSong.objects.create(setlist=sl, song=song, position=i)
        url = reverse("events:act_setlist", args=[self.event.pk, band.pk])
        rows = self.client.get(url).context["timeline"]
        self.assertEqual([(r["title"], r["n"]) for r in rows], [("First", 1), ("No album", 1)])
        self.assertEqual(self.client.get(url).context["top_albums"][0]["title"], "First")
        chart = self.client.get(band.get_absolute_url() + "?sort=count").context["album_chart"]
        self.assertEqual(chart[-1]["title"], "No album")
        page = self.client.get(url).content.decode()
        self.assertIn("Find albums on MusicBrainz", page)  # lookups wait for the button


class AlbumMeasureToggleTests(BaseTestCase):
    def test_bars_can_measure_plays_or_different_songs(self):
        from events.models import SetlistSong, Song
        band = Artist.objects.create(name="Band")
        hit = Song.objects.create(artist=band, title="Hit", album_title="One-hit album", album_year=2000)
        a = Song.objects.create(artist=band, title="A", album_title="Deep album", album_year=2001)
        b = Song.objects.create(artist=band, title="B", album_title="Deep album", album_year=2001)
        for year, songs in [(2005, [hit]), (2006, [hit]), (2007, [hit, a, b])]:
            e = Event.objects.create(title=str(year), date=date(year, 1, 1))
            EventArtist.objects.create(event=e, artist=band)
            sl = Setlist.objects.create(event=e, artist=band)
            for pos, song in enumerate(songs, start=1):
                SetlistSong.objects.create(setlist=sl, song=song, position=pos)
        url = band.get_absolute_url() + "?sort=count"
        by_plays = self.client.get(url).context["album_chart"]
        self.assertEqual([(r["title"], r["pct"]) for r in by_plays], [("One-hit album", 100), ("Deep album", 67)])
        by_songs = self.client.get(url + "&by=songs")
        self.assertEqual([(r["title"], r["pct"]) for r in by_songs.context["album_chart"]],
                         [("Deep album", 100), ("One-hit album", 50)])
        self.assertContains(by_songs, 'id="by-songs" checked')
        self.assertEqual(self.client.get(url).context["top_albums"][0]["title"], "One-hit album")
        self.assertEqual(by_songs.context["top_albums"][0]["title"], "Deep album")  # green bar follows the toggle
        self.assertContains(by_songs, "Most songs played")


class ManageSongsTests(BaseTestCase):
    def test_admins_fix_songs_grouped_by_album_newest_first_no_album_last(self):
        from events.models import SetlistSong, Song
        band = Artist.objects.create(name="Band")
        Song.objects.create(artist=band, title="Old one", album_title="Debut", album_year=1990)
        Song.objects.create(artist=band, title="New one", album_title="Latest", album_year=2020)
        loose = Song.objects.create(artist=band, title="Loose")
        debut = band.albums.create(title="Debut", year=1990)
        earlier = Artist.objects.create(name="Earlier Band")
        band.member_of.add(earlier)
        earlier_album = earlier.albums.create(title="Garage Tape", year=1985)
        url = reverse("events:artist_songs_manage", args=[band.pk])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.user.is_superuser = True
        self.user.save()
        self.assertContains(self.client.get(band.get_absolute_url()), url)
        page = self.client.get(url)
        self.assertEqual([g["title"] for g in page.context["groups"]], ["Latest", "Debut", "No album"])
        self.assertContains(page, '<optgroup label="Earlier Band">')  # a band they were in
        self.assertContains(page, "#manage-albums")
        self.assertNotContains(page, "album_year_")
        self.client.post(url, {f"album_{loose.pk}": str(debut.pk)})
        loose.refresh_from_db()
        self.assertEqual((loose.album_title, loose.album_year), ("Debut", 1990))  # the year comes from the album
        self.assertIsNotNone(loose.album_checked_at)
        self.assertEqual([g["title"] for g in self.client.get(url).context["groups"]], ["Latest", "Debut"])
        self.client.post(url, {f"album_{loose.pk}": str(earlier_album.pk), f"cover_of_{loose.pk}": "Earlier Band"})
        loose.refresh_from_db()
        self.assertEqual((loose.album_title, loose.cover_of_name), ("Garage Tape", "Earlier Band"))

    def test_artist_page_offers_find_albums_for_unchecked_played_songs(self):
        from events.models import SetlistSong, Song
        band = Artist.objects.create(name="Band")
        sl = Setlist.objects.create(event=self.event, artist=band)
        unchecked = Song.objects.create(artist=band, title="Unchecked")
        tape = Song.objects.create(artist=band, title="Intro")
        SetlistSong.objects.create(setlist=sl, song=unchecked, position=1)
        SetlistSong.objects.create(setlist=sl, song=tape, position=2, is_tape=True)
        page = self.client.get(band.get_absolute_url())
        self.assertEqual(page.context["pending"], [unchecked.pk])
        self.assertContains(page, "Find albums on MusicBrainz")
        self.assertContains(page, "1 song hasn&#x27;t been looked up yet.")

    def test_event_cards_open_the_artists_setlist_when_there_is_one(self):
        band = Artist.objects.create(name="Band")
        EventArtist.objects.create(event=self.event, artist=band)
        other = Event.objects.create(title="No setlist", date=date(2000, 1, 1))
        EventArtist.objects.create(event=other, artist=band)
        Setlist.objects.create(event=self.event, artist=band)
        page = self.client.get(band.get_absolute_url())
        self.assertContains(page, reverse("events:act_setlist", args=[self.event.pk, band.pk]))
        self.assertContains(page, f'href="{other.get_absolute_url()}"')

    def test_tours_sit_in_the_header(self):
        band = Artist.objects.create(name="Band")
        Production.objects.create(title="World Tour", artist=band)
        html = self.client.get(band.get_absolute_url()).content.decode()
        self.assertLess(html.index('class="artist-tours'), html.index('<h2 class="section-title mb-3">Events'))
        self.assertIn("World Tour", html)


class CoversAndBandsTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        from events.models import SetlistSong, Song
        self.beatles = Artist.objects.create(name="The Beatles", kind=Artist.Kind.GROUP)
        self.paul = Artist.objects.create(name="Paul McCartney")
        self.paul.member_of.add(self.beatles)
        Album.objects.create(artist=self.beatles, title="Revolver", year=1966)
        EventArtist.objects.create(event=self.event, artist=self.paul)
        sl = Setlist.objects.create(event=self.event, artist=self.paul)
        songs = [
            Song.objects.create(artist=self.paul, title="Band on the Run", album_title="Band on the Run", album_year=1973),
            Song.objects.create(artist=self.paul, title="Hey Jude", cover_of_name="The Beatles",
                                album_title="Hey Jude", album_year=1968),
            Song.objects.create(artist=self.paul, title="Something", cover_of_name="the beatles"),
            Song.objects.create(artist=self.paul, title="Foxy Lady", cover_of_name="Jimi Hendrix"),
        ]
        for i, song in enumerate(songs, start=1):
            SetlistSong.objects.create(setlist=sl, song=song, position=i)

    def test_band_songs_get_the_band_chart_and_other_covers_a_covers_row(self):
        page = self.client.get(self.paul.get_absolute_url())
        own = [(r["title"], r["n"]) for r in page.context["album_chart"]]
        self.assertEqual(own, [("Band on the Run", 1), ("No album covers", 1)])
        self.assertEqual(page.context["album_chart"][-1]["tracks"][0]["by"], "Jimi Hendrix")
        [chart] = page.context["band_charts"]
        self.assertEqual(chart["band"], self.beatles)
        self.assertEqual([(r["title"], r["n"]) for r in chart["rows"]],
                         [("Hey Jude", 1), ("Revolver", 0), ("No album", 1)])
        self.assertContains(page, "As a member of")
        self.assertContains(page, "Member of ")
        self.assertContains(self.client.get(self.beatles.get_absolute_url()), "Members: ")
        self.assertTrue(next(r for r in chart["rows"] if r["title"] == "Hey Jude").get("is_top"))  # green in band chart

    def test_covers_recorded_on_the_artists_album_count_there_in_italics(self):
        from events.models import SetlistSong, Song
        Album.objects.create(artist=self.paul, title="Run Devil Run", year=1999)
        sl = self.event.setlists.get(artist=self.paul)
        song = Song.objects.create(artist=self.paul, title="Lonesome Town", cover_of_name="Ricky Nelson",
                                   album_title="Run Devil Run", album_year=1999)
        SetlistSong.objects.create(setlist=sl, song=song, position=9)
        page = self.client.get(self.paul.get_absolute_url())
        rows = {r["title"]: r for r in page.context["album_chart"]}
        self.assertEqual(rows["Run Devil Run"]["n"], 1)
        self.assertEqual(rows["Run Devil Run"]["tracks"][0]["by"], "Ricky Nelson")
        self.assertEqual([t["title"] for t in rows["No album covers"]["tracks"]], ["Foxy Lady"])
        self.assertContains(page, '<span class="fst-italic" title="Cover of Ricky Nelson">Lonesome Town')
        setlist_page = self.client.get(reverse("events:act_setlist", args=[self.event.pk, self.paul.pk]))
        self.assertIn("Run Devil Run", [r["title"] for r in setlist_page.context["timeline"] if r["n"]])

    def test_setlist_page_has_the_band_chart_too(self):
        page = self.client.get(reverse("events:act_setlist", args=[self.event.pk, self.paul.pk]))
        self.assertEqual(page.context["top_albums"][0]["title"], "Band on the Run")
        self.assertEqual(page.context["band_charts"][0]["band"], self.beatles)
        self.assertEqual(page.context["timeline"][-1]["title"], "No album covers")

    def test_cover_lookups_use_the_original_artist_and_file_albums_under_it(self):
        song = self.paul.songs.get(title="Something")
        with mock.patch("events.musicbrainz.song_album",
                        return_value={"title": "Abbey Road", "year": 1969, "mbid": "x"}) as lookup:
            self.client.post(reverse("events:song_album_lookup", args=[song.pk]))
        self.assertEqual(lookup.call_args.args[:2], ("Something", "the beatles"))
        self.assertTrue(self.beatles.albums.filter(title="Abbey Road").exists())
        self.assertFalse(self.paul.albums.filter(title="Abbey Road").exists())

    def test_imports_and_old_notes_record_the_original_artist(self):
        import importlib
        from django.apps import apps as real_apps
        from events.models import SetlistSong, Song
        mig = importlib.import_module("events.migrations.0030_covers_and_bands")
        sl = Setlist.objects.create(event=Event.objects.create(title="x"), artist=self.paul)
        old = Song.objects.create(artist=self.paul, title="Old import")
        SetlistSong.objects.create(setlist=sl, song=old, position=1, note="The Beatles cover; with Ringo")
        mig.covers_from_notes(real_apps, None)
        old.refresh_from_db()
        self.assertEqual(old.cover_of_name, "The Beatles")
        payload = {"id": "c1", "artist": {"name": "Paul McCartney"}, "url": "",
                   "sets": {"set": [{"song": [{"name": "Let It Be", "cover": {"name": "The Beatles"}}]}]}}
        import_setlist(Event.objects.create(title="y"), payload)
        self.assertEqual(self.paul.songs.get(title="Let It Be").cover_of_name, "The Beatles")


class TiedTopAlbumsTests(BaseTestCase):
    def test_albums_tied_for_most_played_are_all_highlighted(self):
        from events.models import SetlistSong, Song
        band = Artist.objects.create(name="Band")
        EventArtist.objects.create(event=self.event, artist=band)
        sl = Setlist.objects.create(event=self.event, artist=band)
        for i, (title, album, year) in enumerate([("A", "First", 1980), ("B", "Second", 1985), ("C", "Third", 1990),
                                                  ("D", "Third", 1990), ("E", "First", 1980)], start=1):
            SetlistSong.objects.create(setlist=sl, position=i,
                                       song=Song.objects.create(artist=band, title=title, album_title=album, album_year=year))
        page = self.client.get(reverse("events:act_setlist", args=[self.event.pk, band.pk]))
        self.assertEqual([a["title"] for a in page.context["top_albums"]], ["First", "Third"])
        self.assertContains(page, "(tied)")
        self.assertEqual(page.content.decode().count("album-bar has-songs is-top"), 2)


class SongsListLengthTests(BaseTestCase):
    def test_more_albums_show_more_songs(self):
        from events.views import _songs_to_show
        self.assertEqual(_songs_to_show([], []), 10)
        self.assertEqual(_songs_to_show([{}] * 12, []), 21)
        self.assertEqual(_songs_to_show([{}] * 12, [{"rows": [{}] * 4}]), 30)
        self.assertEqual(_songs_to_show([{}] * 100, []), 50)


class TheaterCreditsTests(BaseTestCase):
    def test_author_and_director_are_credits_not_acts(self):
        play = Event.objects.create(title="", event_type=EventType.THEATER, date=date(2020, 1, 1))
        author = Artist.objects.create(name="William Shakespeare")
        director = Artist.objects.create(name="La Directora")
        actor = Artist.objects.create(name="Compañía Teatro")
        EventArtist.objects.create(event=play, artist=author, role="author", billing_order=1)
        EventArtist.objects.create(event=play, artist=director, role="director", billing_order=2)
        EventArtist.objects.create(event=play, artist=actor, role="cast", billing_order=3)
        play.refresh_from_db()
        self.assertEqual(play.title, "Compañía Teatro")  # credits never become the title

        log = Log.objects.create(user=self.user, event=play)
        self.assertEqual(list(log.acts.values_list("artist__name", flat=True)), ["Compañía Teatro"])

        self.client.force_login(self.user)
        page = self.client.get(play.get_absolute_url())
        self.assertContains(page, "Written by")
        self.assertContains(page, "Directed by")
        self.assertContains(page, ">Cast<")
        self.assertEqual([r["link"].artist.name for r in page.context["act_rows"]], ["Compañía Teatro"])


class ImageFitTests(BaseTestCase):
    def _listing(self):
        return self.client.get(reverse("events:event_list")).content.decode()

    def test_blurred_whole_photo_by_default_crop_and_stretch_on_request(self):
        self.event.image_url = "https://img.example/wide.jpg"
        self.event.save()
        self.assertEqual(self.event.image_display, "fit")
        self.assertIn('class="poster-fit" src="https://img.example/wide.jpg"', self._listing())
        self.assertIn('class="poster-backdrop"', self._listing())
        self.event.image_display = "crop"
        self.event.save()
        html = self._listing()
        self.assertNotIn('class="poster-fit"', html)
        self.assertIn('<img src="https://img.example/wide.jpg"', html)
        self.event.image_display = "stretch"
        self.event.save()
        self.assertIn('class="poster-stretch" src="https://img.example/wide.jpg"', self._listing())

    def test_auto_mode_renders_the_blurred_layout_for_the_script_to_switch(self):
        self.event.image_url = "https://img.example/poster.jpg"
        self.event.image_display = "auto"
        self.event.save()
        html = self._listing()
        self.assertIn('class="poster-fit poster-auto" src="https://img.example/poster.jpg"', html)
        self.assertIn('class="poster-backdrop"', html)  # stays blurred unless the photo is poster-shaped
        self.assertIn("events/poster-auto.js", html)

    def test_profile_preference_overrides_event_only_for_that_viewer(self):
        from events.models import Profile
        self.event.image_url = "https://img.example/wide.jpg"
        self.event.save()  # event set to the blurred "fit"
        Profile.objects.update_or_create(user=self.user, defaults={"thumbnail_display": "stretch"})
        self.client.force_login(self.user)
        self.assertIn('class="poster-stretch"', self._listing())
        self.client.logout()
        html = self._listing()
        self.assertIn('class="poster-fit"', html)
        self.assertNotIn('class="poster-stretch"', html)
        self.event.refresh_from_db()
        self.assertEqual(self.event.image_display, "fit")

    def test_option_is_in_the_event_form(self):
        self.client.force_login(self.user)
        self.event.created_by = self.user
        self.event.save()
        page = self.client.get(reverse("events:event_edit", args=[self.event.pk]))
        self.assertContains(page, "Stretch to fill")


class EventFormOrderTests(BaseTestCase):
    def test_lineup_editor_comes_right_after_venue(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("events:event_create")).content.decode()
        venue = html.index("Add the venue by hand")
        lineup = html.index('id="lineup-rows"')
        description = html.index('name="description"')
        self.assertLess(venue, lineup)
        self.assertLess(lineup, description)
        self.assertEqual(html.count('id="lineup-rows"'), 1)


class FestivalPhotoForAllDaysTests(BaseTestCase):
    def test_copy_photo_to_every_day_of_that_years_festival(self):
        fest = Production.objects.create(title="Lollapalooza Chile", event_type=EventType.FESTIVAL)
        days = [Event.objects.create(title="", event_type=EventType.FESTIVAL, production=fest, date=date(2024, 3, d),
                                     venue=Venue.objects.create(name=f"Stage {d}", city=f"City {d}"), created_by=self.user)
                for d in (15, 16, 17)]
        next_year = Event.objects.create(title="", event_type=EventType.FESTIVAL, production=fest, date=date(2025, 3, 15),
                                         created_by=self.user)
        self.client.force_login(self.user)
        page = self.client.get(reverse("events:event_edit", args=[days[0].pk]))
        self.assertContains(page, "Use this photo for every day of Lollapalooza Chile in 2024 (2 other days)")
        self.assertTrue(page.context["form"].fields["photo_for_all_days"].initial)  # on by default
        html = page.content.decode()
        self.assertLess(html.index('name="image_url"'), html.index('name="photo_for_all_days"'))

        self.client.post(reverse("events:event_edit", args=[days[0].pk]), {
            "title": "", "event_type": "festival", "date": "2024-03-15", "production": str(fest.pk),
            "venue": str(days[0].venue_id), "image_url": "https://img.example/lolla.jpg", "photo_for_all_days": "on",
            "eventartist_set-TOTAL_FORMS": "0", "eventartist_set-INITIAL_FORMS": "0",
            "eventartist_set-MIN_NUM_FORMS": "0", "eventartist_set-MAX_NUM_FORMS": "1000",
        })
        for d in days:
            d.refresh_from_db()
            self.assertEqual(d.image_url, "https://img.example/lolla.jpg")
        next_year.refresh_from_db()
        self.assertEqual(next_year.image_url, "")

    def test_option_hidden_for_a_one_off_show(self):
        self.client.force_login(self.user)
        self.event.created_by = self.user
        self.event.save()
        self.assertNotContains(self.client.get(reverse("events:event_edit", args=[self.event.pk])), "photo_for_all_days")


class FirstActIsHeadlinerTests(BaseTestCase):
    def test_first_act_saved_as_headliner(self):
        self.client.force_login(self.user)
        a, b = Artist.objects.create(name="Was support"), Artist.objects.create(name="Other")
        self.client.post(reverse("events:event_create"), {
            "title": "", "event_type": "music", "date": "2020-02-02",
            "eventartist_set-TOTAL_FORMS": "2", "eventartist_set-INITIAL_FORMS": "0",
            "eventartist_set-MIN_NUM_FORMS": "0", "eventartist_set-MAX_NUM_FORMS": "1000",
            "eventartist_set-0-artist": str(a.pk), "eventartist_set-0-role": "support",
            "eventartist_set-0-billing_order": "1",
            "eventartist_set-1-artist": str(b.pk), "eventartist_set-1-role": "support",
            "eventartist_set-1-billing_order": "2",
        })
        event = Event.objects.get(artists=a)
        roles = dict(event.eventartist_set.values_list("artist__name", "role"))
        self.assertEqual(roles, {"Was support": "headliner", "Other": "support"})
        self.assertEqual(event.title, "Was support")


class EventVideoTests(BaseTestCase):
    def test_parse_youtube_links(self):
        from .videos import youtube_id

        for url in ["https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s", "https://youtu.be/dQw4w9WgXcQ",
                    "youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/live/dQw4w9WgXcQ?si=x",
                    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"]:
            with self.subTest(url=url):
                self.assertEqual(youtube_id(url), "dQw4w9WgXcQ")
        self.assertEqual(youtube_id("https://vimeo.com/123"), "")
        self.assertEqual(youtube_id("https://www.youtube.com/watch?v=short"), "")

    @mock.patch("events.videos.video_title", return_value="Live at Caupolicán")
    def test_add_list_limit_and_remove(self, _):
        from .models import EventVideo

        act = Artist.objects.create(name="Green Day")
        EventArtist.objects.create(event=self.event, artist=act)
        self.client.force_login(self.user)
        add = reverse("events:video_add", args=[self.event.pk])
        self.client.post(add, {"url": "https://youtu.be/AAAAAAAAAAA", "artist": str(act.pk)})
        self.client.post(add, {"url": "https://youtu.be/AAAAAAAAAAA"})  # duplicate ignored
        self.client.post(add, {"url": "not a link"})
        for i in range(5):
            self.client.post(add, {"url": f"https://youtu.be/BBBBBBBBBB{i}"})
        self.assertEqual(EventVideo.objects.count(), 6)
        self.assertEqual(EventVideo.objects.get(youtube_id="AAAAAAAAAAA").artist, act)

        page = self.client.get(self.event.get_absolute_url())
        self.assertEqual(len(page.context["latest_videos"]), 4)
        self.assertContains(page, "See all 6 videos")
        self.assertEqual(page.content.decode().count("youtube-nocookie.com/embed/"), 4)

        full = self.client.get(reverse("events:event_videos", args=[self.event.pk]))
        self.assertEqual([label for label, _, _ in full.context["video_groups"]], ["Whole event", "Green Day"])
        self.assertContains(full, "Live at Caupolicán")

        video = EventVideo.objects.get(youtube_id="AAAAAAAAAAA")
        other = get_user_model().objects.create_user("bob", password="pw-test-456")
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("events:video_remove", args=[self.event.pk, video.pk])).status_code, 403)
        self.client.force_login(self.user)
        self.client.post(reverse("events:video_remove", args=[self.event.pk, video.pk]))
        self.assertFalse(EventVideo.objects.filter(pk=video.pk).exists())


class HomeLayoutTests(BaseTestCase):
    def test_home_order_and_links(self):
        from datetime import timedelta

        from django.utils import timezone

        Event.objects.create(title="Soon", date=timezone.localdate() + timedelta(days=5), venue=self.venue)
        Log.objects.create(user=self.user, event=self.event, rating=9, review="Great")
        html = self.client.get(reverse("events:home")).content.decode()
        order = [html.index(t) for t in ["Upcoming", "Recently happened", "Latest reviews", "Just added"]]
        self.assertEqual(order, sorted(order))
        self.assertIn("?when=past\">All events →", html)
        self.assertIn("?sort=added\">Last added →", html)

    def test_sort_by_recently_added(self):
        older = Event.objects.create(title="Added first", date=date(2030, 1, 1))
        newer = Event.objects.create(title="Added last", date=date(1990, 1, 1))
        titles = [e.title for e in self.client.get(reverse("events:event_list") + "?sort=added").context["page"].object_list]
        self.assertEqual(titles[:2], ["Added last", "Added first"])
        self.assertTrue(older.pk < newer.pk)


class SongRankingAndBarScaleTests(BaseTestCase):
    def test_tied_plays_rank_the_song_with_fewer_chances_higher(self):
        from events.models import SetlistSong, Song
        band = Artist.objects.create(name="Los Tres")
        Album.objects.create(artist=band, title="Old", year=2000)
        Album.objects.create(artist=band, title="New", year=2010)
        shows = [Event.objects.create(title=f"Show {y}", date=date(y, 1, 1)) for y in (2005, 2011, 2012, 2013, 2014, 2015)]
        setlists = [Setlist.objects.create(event=e, artist=band) for e in shows]
        for e in shows:
            EventArtist.objects.create(event=e, artist=band)
        # "Always" (old album) at 5 of 6 shows; "Fresh" (new album) at all 5 shows since it came out;
        # "Steady" (new album) at 4 of those 5.
        songs = {
            "Always": (Song.objects.create(artist=band, title="Always", album_title="Old", album_year=2000), setlists[1:]),
            "Fresh": (Song.objects.create(artist=band, title="Fresh", album_title="New", album_year=2010), setlists[1:]),
            "Steady": (Song.objects.create(artist=band, title="Steady", album_title="New", album_year=2010), setlists[2:]),
        }
        for song, played_at in songs.values():
            for sl in played_at:
                SetlistSong.objects.create(setlist=sl, song=song, position=song.pk)
        top = self.client.get(band.get_absolute_url()).context["top_songs"]
        self.assertEqual([(s["song__title"], s["n"], s["of"]) for s in top],
                         [("Fresh", 5, 5), ("Always", 5, 6), ("Steady", 4, 5)])

    def test_most_played_album_fills_the_bar_whatever_no_album_and_covers_say(self):
        from events.albums import album_rows
        band = Artist.objects.create(name="Los Tres")
        played = {"Hits": {"title": "Hits", "year": 1999, "n": 4, "songs": 2, "tracks": []}}
        rows = album_rows(band, played, no_album={"n": 8, "songs": 3, "tracks": []},
                          covers={"n": 6, "songs": 2, "tracks": []})
        self.assertEqual([(r["title"], r["pct"]) for r in rows],
                         [("Hits", 100), ("No album", 100), ("No album covers", 100)])
        only_loose = album_rows(band, {}, no_album={"n": 2, "songs": 1, "tracks": []})
        self.assertEqual(only_loose[0]["pct"], 100)  # nothing on an album: the extra rows set the scale


class AlbumColourTests(BaseTestCase):
    def test_palette_order_shades_and_wraparound(self):
        from events.albums import album_color
        self.assertEqual(album_color(0), "hsl(215 70% 58%)")  # blue
        self.assertEqual(album_color(1), "hsl(140 70% 58%)")  # green
        self.assertEqual(album_color(2), "hsl(275 70% 58%)")  # purple
        self.assertEqual(album_color(3), "hsl(0 70% 58%)")  # red
        self.assertEqual(album_color(4), "hsl(28 70% 58%)")  # orange
        self.assertEqual(album_color(10), "hsl(215 60% 72%)")  # second round: lighter blue
        self.assertEqual(len({album_color(i) for i in range(50)}), 50)
        self.assertEqual(album_color(50), album_color(0))  # past 50 it starts over

    def test_setlist_songs_and_chart_rows_get_their_albums_colour(self):
        from events.models import SetlistSong, Song
        band = Artist.objects.create(name="Los Tres")
        Album.objects.create(artist=band, title="Second", year=1993)
        Album.objects.create(artist=band, title="First", year=1991)
        EventArtist.objects.create(event=self.event, artist=band)
        sl = Setlist.objects.create(event=self.event, artist=band)
        for i, (title, album) in enumerate([("Late", "Second"), ("Early", "First"), ("Loose", "")], start=1):
            SetlistSong.objects.create(setlist=sl, position=i, song=Song.objects.create(
                artist=band, title=title, album_title=album))
        page = self.client.get(reverse("events:act_setlist", args=[self.event.pk, band.pk]))
        colours = {e.song.title: e.album_color for e in page.context["entries"]}
        self.assertEqual(colours, {"Late": "hsl(140 70% 58%)", "Early": "hsl(215 70% 58%)", "Loose": None})
        rows = {r["title"]: r.get("color") for r in page.context["timeline"]}
        self.assertEqual(rows["First"], "hsl(215 70% 58%)")
        self.assertIsNone(rows["No album"])
        self.assertContains(page, 'id="color-albums"')
        self.assertContains(page, 'data-album-color style="--album-color: hsl(215 70% 58%)"')
