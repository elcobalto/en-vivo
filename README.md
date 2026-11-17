## En Vivo

Concert logging app built with Django.

### Run locally

```bash
cd "en-vivo"
python3.12 -m venv .venv
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

### Roles

Every logged-in user can add events and edit the ones they created. Admins (the `Admins` group,
plus superusers) can also edit and delete any event. Manage the role with:

```bash
python manage.py make_admin <username>
python manage.py make_admin <username> --remove
```

Event titles are optional: a blank title becomes “Artist in City, date” and updates itself when the
lineup, venue or date change.

### Environment

Copy `.env.example` to `.env` and fill in `SETLISTFM_API_KEY`, `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

### Tests

```bash
python manage.py test events
```

### Deploy (Render)

`render.yaml` defines the web service and a PostgreSQL database; `build.sh` installs dependencies,
collects static files and runs migrations on every deploy.

1. Push this repository to GitHub (private is fine).
2. In Render: **New → Blueprint**, pick the repository. Render creates `en-vivo` and `en-vivo-db`.
3. Fill in the secrets it asks for: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SETLISTFM_API_KEY`,
   and `DJANGO_SUPERUSER_EMAILS` (your Google email: you become superuser + admin on first login).
   `DJANGO_SECRET_KEY` is generated automatically.
4. In Google Cloud Console add `https://<your-service>.onrender.com/accounts/google/login/callback/`
   as an authorized redirect URI, and publish the OAuth consent screen.
5. For a custom domain, add it in Render and set `DJANGO_ALLOWED_HOSTS` to it.

Production behaviour is driven by environment variables: `DEBUG` is off on Render (`DJANGO_DEBUG`
overrides), `DATABASE_URL` selects PostgreSQL, and HTTPS-only cookies/redirects are enabled.
