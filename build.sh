#!/usr/bin/env bash
# Render build step: install dependencies, collect static files, apply migrations.
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate --no-input
python manage.py createcachetable

# Best effort: fill artist countries from MusicBrainz (never fails the build).
python manage.py fill_artist_countries --limit 100 || true
