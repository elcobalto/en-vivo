from __future__ import annotations

import json
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Event, EventPerformer, EventType, Log, Performer, Production, Setlist, Venue
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
    def test_import_creates_performer_lineup_and_songs(self):
        setlist = import_setlist(self.event, SETLIST_PAYLOAD)

        performer = Performer.objects.get(name="Green Day")
        self.assertEqual(performer.mbid, "084308bd-1654-436f-ba03-df6697104e19")
        link = EventPerformer.objects.get(event=self.event, performer=performer)
        self.assertEqual(link.role, EventPerformer.Role.HEADLINER)

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

    def test_matches_existing_performer_by_name(self):
        existing = Performer.objects.create(name="green day")
        import_setlist(self.event, SETLIST_PAYLOAD)
        self.assertEqual(Performer.objects.count(), 1)
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
        performer = Performer.objects.create(name="Green Day")
        EventPerformer.objects.create(event=self.event, performer=performer)
        Log.objects.create(user=self.user, event=self.event, rating=9, review="Loud.")

        for url in [
            reverse("events:home"),
            reverse("events:event_list"),
            reverse("events:event_list") + "?type=music&q=green",
            self.event.get_absolute_url(),
            production.get_absolute_url(),
            performer.get_absolute_url(),
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
        for name in ["event_create", "performer_create", "venue_create", "production_create", "diary"]:
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
        self.performer = Performer.objects.create(name="Bill Burr")

    def _payload(self, **extra):
        data = {
            "title": "Bill Burr Live",
            "event_type": "comedy",
            "date": "2026-12-01",
            "venue": str(self.venue.pk),
            "eventperformer_set-TOTAL_FORMS": "3",
            "eventperformer_set-INITIAL_FORMS": "0",
            "eventperformer_set-MIN_NUM_FORMS": "0",
            "eventperformer_set-MAX_NUM_FORMS": "1000",
            "eventperformer_set-0-performer": str(self.performer.pk),
            "eventperformer_set-0-role": "headliner",
            "eventperformer_set-0-billing_order": "1",
        }
        data.update(extra)
        return data

    def test_create_event_with_lineup(self):
        response = self.client.post(reverse("events:event_create"), self._payload())
        event = Event.objects.get(title="Bill Burr Live")
        self.assertRedirects(response, event.get_absolute_url())
        self.assertEqual(event.created_by, self.user)
        self.assertEqual(list(event.performers.all()), [self.performer])

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
        page = self.client.get(self.event.get_absolute_url())
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
        self.client.force_login(self.user)
        url = reverse("events:want_to_go_toggle", args=[self.event.pk])
        self.client.post(url)
        self.assertTrue(WantToGo.objects.filter(user=self.user, event=self.event).exists())
        page = self.client.get(self.event.get_absolute_url())
        self.assertContains(page, "In your Want to go")

        # The 1992 show is in the past, so it appears under "Missed it".
        page = self.client.get(reverse("events:user_want_to_go", args=["ana"]))
        self.assertContains(page, "Missed it")
        self.assertContains(page, "Green Day in Chile")

        self.client.post(url)
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
        performer = Performer.objects.create(name="Green Day")
        EventPerformer.objects.create(event=self.event, performer=performer)
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
        self.assertContains(page, "Performers")
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
        response = self.client.post(reverse("events:profile_edit"), {
            "display_name": "Ana M", "location": "Santiago", "bio": "Front row.",
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
            "slot_1": str(self.event.pk), "slot_2": str(self.event.pk),
        })
        self.assertContains(response, "different event")
        self.assertFalse(FavoriteEvent.objects.exists())


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

        green_day = Performer.objects.create(name="Green Day")
        EventPerformer.objects.create(event=event, performer=green_day)
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

    def test_migration_shortens_old_auto_titles(self):
        import importlib

        from django.apps import apps

        event = Event.objects.create(title="", venue=self.venue, date=date(2019, 3, 29))
        EventPerformer.objects.create(event=event, performer=Performer.objects.create(name="Paul McCartney"))
        Event.objects.filter(pk=event.pk).update(title="Paul McCartney in Estadio Nacional, 29 Mar 2019")
        custom = Event.objects.create(title="My own title", venue=self.venue)

        migration = importlib.import_module("events.migrations.0009_auto_title_headliner_only")
        migration.headliner_only_titles(apps, None)

        event.refresh_from_db()
        custom.refresh_from_db()
        self.assertEqual(event.title, "Paul McCartney")
        self.assertEqual(custom.title, "My own title")

    def test_card_shows_title_then_venue_and_date(self):
        auto = Event.objects.create(title="", venue=self.venue, date=date(2019, 3, 29),
                                    image_url="https://example.com/poster.jpg")
        EventPerformer.objects.create(event=auto, performer=Performer.objects.create(name="Paul McCartney"))
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
        EventPerformer.objects.create(event=self.event, performer=Performer.objects.create(name="X"))
        self.event.refresh_from_db()
        self.assertEqual(self.event.title, "Green Day in Chile")

    def test_falls_back_without_city_or_date(self):
        event = Event.objects.create(title="", event_type=EventType.COMEDY)
        self.assertEqual(event.title, "Comedy event")

    def test_form_creates_auto_title_and_keeps_it_auto_on_edit(self):
        self.client.force_login(self.user)
        performer = Performer.objects.create(name="Bill Burr")
        data = {
            "title": "", "event_type": "comedy", "date": "2026-12-01", "venue": str(self.venue.pk),
            "eventperformer_set-TOTAL_FORMS": "1", "eventperformer_set-INITIAL_FORMS": "0",
            "eventperformer_set-MIN_NUM_FORMS": "0", "eventperformer_set-MAX_NUM_FORMS": "1000",
            "eventperformer_set-0-performer": str(performer.pk),
            "eventperformer_set-0-role": "headliner", "eventperformer_set-0-billing_order": "1",
        }
        self.client.post(reverse("events:event_create"), data)
        event = Event.objects.get(performers=performer)
        self.assertEqual(event.title, "Bill Burr")

        page = self.client.get(reverse("events:event_edit", args=[event.pk]))
        self.assertContains(page, "Automatic: Bill Burr")

        link = event.eventperformer_set.get()
        edit = dict(data, date="2026-12-02", **{
            "eventperformer_set-INITIAL_FORMS": "1", "eventperformer_set-0-id": str(link.pk),
            "eventperformer_set-0-event": str(event.pk),
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
        self.performer = Performer.objects.create(name="Paul McCartny")
        self.production = Production.objects.create(title="Got Back Tour")
        self.admin = get_user_model().objects.create_user("mod", password="pw-test-000")
        call_command("make_admin", "mod", stdout=open("/dev/null", "w"))

    def test_regular_users_cannot_edit_catalog(self):
        self.client.force_login(self.user)
        for name, obj in [("performer", self.performer), ("venue", self.venue), ("production", self.production)]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"events:{name}_edit", args=[obj.pk])).status_code, 403)
                self.assertNotContains(self.client.get(obj.get_absolute_url()), f"Edit {name}")

    def test_admin_edits_performer_and_venue(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.performer.get_absolute_url()), "Edit performer")
        response = self.client.post(
            reverse("events:performer_edit", args=[self.performer.pk]), {"name": "Paul McCartney", "kind": "person"}
        )
        self.assertRedirects(response, self.performer.get_absolute_url())
        self.performer.refresh_from_db()
        self.assertEqual(self.performer.name, "Paul McCartney")

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
            "eventperformer_set-TOTAL_FORMS": "0", "eventperformer_set-INITIAL_FORMS": "0",
            "eventperformer_set-MIN_NUM_FORMS": "0", "eventperformer_set-MAX_NUM_FORMS": "1000",
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
        self.assertEqual(event.production.title, "Freshen Up")
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
        self.assertEqual(second_night.event.production, first.event.production)

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

    def test_admins_only(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("events:setlistfm_shows")).status_code, 403)
        self.assertNotContains(self.client.get(reverse("events:event_list")), "Import from setlist.fm")

    @mock.patch("events.views_venues.search_setlists", return_value=[_setlist()])
    def test_search_then_import_selected(self, search):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("events:event_list")), "Import from setlist.fm")
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
