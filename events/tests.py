from __future__ import annotations

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
        self.assertContains(page, "Teatro Caupolicán", count=1)  # shown on the event card only
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
    def test_blank_title_builds_artist_city_date(self):
        event = Event.objects.create(title="", venue=self.venue, date=date(2010, 11, 10))
        self.assertTrue(event.title_is_auto)
        self.assertEqual(event.title, "Music event in Santiago, 10 Nov 2010")

        green_day = Performer.objects.create(name="Green Day")
        EventPerformer.objects.create(event=event, performer=green_day)
        event.refresh_from_db()
        self.assertEqual(event.title, "Green Day in Santiago, 10 Nov 2010")

        self.venue.city = "Valparaíso"
        self.venue.save()
        event.refresh_from_db()
        self.assertEqual(event.title, "Green Day in Valparaíso, 10 Nov 2010")

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
        self.assertEqual(event.title, "Bill Burr in Santiago, 1 Dec 2026")

        page = self.client.get(reverse("events:event_edit", args=[event.pk]))
        self.assertContains(page, "Automatic: Bill Burr in Santiago, 1 Dec 2026")

        link = event.eventperformer_set.get()
        edit = dict(data, date="2026-12-02", **{
            "eventperformer_set-INITIAL_FORMS": "1", "eventperformer_set-0-id": str(link.pk),
            "eventperformer_set-0-event": str(event.pk),
        })
        self.client.post(reverse("events:event_edit", args=[event.pk]), edit)
        event.refresh_from_db()
        self.assertEqual(event.title, "Bill Burr in Santiago, 2 Dec 2026")

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
