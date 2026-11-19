"""Album breakdowns for charts: songs played per album, including albums nobody heard live."""
from __future__ import annotations

from .models import Album, Artist, Song, normalize_name


def album_rows(artist: Artist, played: dict, *, skip_empty: bool = False, sort: str = "newest",
               released_by: int | None = None) -> list[dict]:
    """Rows of {title, year, n, songs, pct} for a chart or list.

    ``played`` maps album title → {"title", "year", "n", "songs"} counted from setlists (tapes left out).
    The artist's saved albums are added with 0 songs unless ``skip_empty``. ``sort`` is "newest" (default)
    or "count"; ``pct`` is the bar width relative to the most played album. With ``released_by`` (a year),
    saved albums released after it are left out: they didn't exist yet at that show.
    """
    rows = {normalize_name(a["title"]): dict(a) for a in played.values()}
    saved = Album.objects.filter(artist=artist)
    if released_by:
        saved = saved.exclude(year__gt=released_by)
    for album in saved:
        row = rows.get(normalize_name(album.title))
        if row is None:
            rows[normalize_name(album.title)] = {"title": album.title, "year": album.year, "n": 0, "songs": 0}
        elif row["year"] is None:
            row["year"] = album.year
    result = [r for r in rows.values() if r["n"] or not skip_empty]
    if sort == "count":
        result.sort(key=lambda r: (-r["n"], r["year"] or 9999, r["title"].casefold()))
    else:
        result.sort(key=lambda r: (-(r["year"] or 0), r["title"].casefold()))
    peak = max((r["n"] for r in result), default=0)
    for r in result:
        r["pct"] = round(r["n"] / peak * 100) if peak else 0
    return result


def top_album(rows: list[dict]) -> dict | None:
    """The most played album (ties: the older one), or None when nothing was matched to an album."""
    played = [r for r in rows if r["n"]]
    return min(played, key=lambda r: (-r["n"], r["year"] or 9999, r["title"].casefold())) if played else None


def remember_album(song: Song) -> None:
    """Keep the artist's album list in step with what songs are filed under."""
    if not song.album_title:
        return
    wanted = normalize_name(song.album_title)
    existing = next((a for a in Album.objects.filter(artist_id=song.artist_id) if normalize_name(a.title) == wanted), None)
    if existing is None:
        Album.objects.create(artist_id=song.artist_id, title=song.album_title, year=song.album_year)
    elif existing.year is None and song.album_year:
        existing.year = song.album_year
        existing.save(update_fields=["year", "updated_at"])
