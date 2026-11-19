"""Album breakdowns for charts: songs played per album, including albums nobody heard live."""
from __future__ import annotations

from .models import Album, Artist, Song, normalize_name


def album_rows(artist: Artist, played: dict, *, skip_empty: bool = False, sort: str = "newest",
               released_by: int | None = None, no_album: dict | None = None, by: str = "n",
               covers: dict | None = None) -> list[dict]:
    """Rows of {title, year, n, songs, pct} for a chart or list.

    ``played`` maps album title → {"title", "year", "n", "songs"} counted from setlists (tapes left out).
    The artist's saved albums are added with 0 songs unless ``skip_empty``. ``sort`` is "newest" (default)
    or "count"; ``pct`` is the bar width relative to the most played album. With ``released_by`` (a year),
    saved albums released after it are left out: they didn't exist yet at that show. ``no_album``
    ({"n", "songs", "tracks"} for played songs filed under no album) adds a last "No album" row.
    ``by`` picks what the bars and the "count" sort measure: "n" (plays) or "songs" (different songs).
    ``covers`` (same shape as ``no_album``, tracks carry "by") adds a last "Covers" row.
    """
    rows = {normalize_name(a["title"]): dict(a) for a in played.values()}
    saved = Album.objects.filter(artist=artist)
    if released_by:
        saved = saved.exclude(year__gt=released_by)
    for album in saved:
        row = rows.get(normalize_name(album.title))
        if row is None:
            rows[normalize_name(album.title)] = {"title": album.title, "year": album.year, "n": 0, "songs": 0,
                                                 "tracks": []}
        elif row["year"] is None:
            row["year"] = album.year
    result = [r for r in rows.values() if r["n"] or not skip_empty]
    if sort == "count":
        result.sort(key=lambda r: (-r[by], -r["n"], r["year"] or 9999, r["title"].casefold()))
    else:
        result.sort(key=lambda r: (-(r["year"] or 0), r["title"].casefold()))
    if no_album and no_album["n"]:
        result.append({"title": "No album", "year": None, "is_none": True, **no_album})
    if covers and covers["n"]:
        result.append({"title": "Covers", "year": None, "is_none": True, "is_covers": True, **covers})
    peak = max((r[by] for r in result), default=0)
    for r in result:
        r["pct"] = round(r[by] / peak * 100) if peak else 0
    return result


def top_albums(rows: list[dict], by: str = "n") -> list[dict]:
    """Every album tied for the most plays (``by="n"``) or the most different songs (``by="songs"``), oldest
    first, and marks them ``is_top`` (the green bar). "No album" and "Covers" never count."""
    played = [r for r in rows if r["n"] and not r.get("is_none")]
    if not played:
        return []
    best = max(r[by] for r in played)
    tops = sorted((r for r in played if r[by] == best), key=lambda r: (r["year"] or 9999, r["title"].casefold()))
    for r in tops:
        r["is_top"] = True
    return tops


def top_album(rows: list[dict], by: str = "n") -> dict | None:
    """The first of ``top_albums`` (marks all of them), or None."""
    tops = top_albums(rows, by)
    return tops[0] if tops else None


def album_owner(song: Song):
    """Whose album list a song's album belongs to: the original artist for covers we know, else the song's artist."""
    if song.cover_of_name:
        wanted = normalize_name(song.cover_of_name)
        return next((a for a in Artist.objects.filter(name__iexact=song.cover_of_name.strip())
                     if normalize_name(a.name) == wanted), None)
    return song.artist


def remember_album(song: Song) -> None:
    """Keep the album list of the song's artist (or the covered artist) in step with what songs are filed under."""
    if not song.album_title:
        return
    owner = album_owner(song)
    if owner is None:
        return
    wanted = normalize_name(song.album_title)
    existing = next((a for a in Album.objects.filter(artist=owner) if normalize_name(a.title) == wanted), None)
    if existing is None:
        Album.objects.create(artist=owner, title=song.album_title, year=song.album_year)
    elif existing.year is None and song.album_year:
        existing.year = song.album_year
        existing.save(update_fields=["year", "updated_at"])


def split_plays(artist: Artist, songs: list[dict]) -> tuple[list[dict], dict, dict]:
    """Split played songs into the artist's own, per band they were in, and other covers.

    ``songs`` are dicts with "title", "album_title", "album_year", "cover_of", "n" (plays) and maybe "of".
    Returns (own songs, {band: [songs]}, covers row {"n", "songs", "tracks"}).
    """
    bands = {normalize_name(b.name): b for b in artist.member_of.all()}
    own, by_band = [], {}
    covers = {"n": 0, "songs": 0, "tracks": []}
    for song in songs:
        cover = normalize_name(song["cover_of"])
        if cover and cover in bands:
            by_band.setdefault(bands[cover], []).append(song)
        elif cover:
            covers["n"] += song["n"]
            covers["songs"] += 1
            covers["tracks"].append({"title": song["title"], "n": song["n"], "of": song.get("of"),
                                     "by": song["cover_of"]})
        else:
            own.append(song)
    return own, by_band, covers


def played_by_album(songs: list[dict]) -> tuple[dict, dict]:
    """Album rows (title → {"title", "year", "n", "songs", "tracks"}) and the "No album" row for these songs."""
    played: dict[str, dict] = {}
    no_album = {"n": 0, "songs": 0, "tracks": []}
    for song in songs:
        track = {"title": song["title"], "n": song["n"], "of": song.get("of")}
        if not song["album_title"]:
            row = no_album
        else:
            row = played.setdefault(song["album_title"], {
                "title": song["album_title"], "year": song["album_year"], "n": 0, "songs": 0, "tracks": [],
            })
        row["n"] += song["n"]
        row["songs"] += 1
        row["tracks"].append(track)
    return played, no_album
