## En Vivo

Concert logging app built with Django.

### Run locally

```bash
cd "en-vivo"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

### Domain models

En Vivo is "Letterboxd for live events" (music, theater, comedy, dance, festivals).
The catalog is our own; setlist.fm is only used to import song lists for music events.

Implemented in `events/models.py`:

- **Catalog:** `Event` (one performance: type, date, venue) optionally grouped by a `Production`
  (tour, theater run, special). `Performer`s join events via `EventPerformer(role, billing_order)`.
  `Venue` holds location details.
- **Diary:** `Log` is a user's entry for an event: rating stored 1–10 and shown as ½–5 stars,
  review, liked, spoilers flag. One log per user per event.
- **Setlists (music):** `Setlist(event, performer)` with ordered `SetlistSong(position, song, set_name, note)`.
  `Song` belongs to a `Performer`. Imported from setlist.fm via `events/setlistfm.py`.
- **Discovery:** `WantToGo(user, event)` is the watchlist. `EventList` + ordered `EventListItem(position, note)`
  are user-curated lists (optionally ranked). Global search lives at `/search/`; `/events/` filters by
  type, when, year and city and sorts by date, popularity or rating.
- **Social:** `Profile` (display name, bio, location, avatar; filled from Google on sign-up) and up to four
  `FavoriteEvent`s. `Follow(follower, following)`. Reviews (`Log`s with text) have pages at `/reviews/<id>/`
  with `ReviewLike` and `Comment`. `/activity/` merges recent actions of people you follow
  (built from existing tables, no separate activity log). Profiles live at `/u/<username>/`.

### Environment

Copy `.env.example` to `.env` and fill in `SETLISTFM_API_KEY`, `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

### Tests

```bash
python manage.py test events
```
