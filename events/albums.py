"""Album breakdowns for charts: songs played per album, including albums nobody heard live."""
from __future__ import annotations

from django.utils import timezone
from django.utils.translation import gettext

from .models import Album, Artist, Song, normalize_name


def album_rows(artist: Artist, played: dict, *, skip_empty: bool = False, sort: str = "newest",
               released_by: int | None = None, no_album: dict | None = None, by: str = "n",
               covers: dict | None = None) -> list[dict]:
    """Rows of {title, year, n, songs, pct} for a chart or list.

    ``played`` maps album title → {"title", "year", "n", "songs"} counted from setlists (tapes left out).
    The artist's saved albums are added with 0 songs unless ``skip_empty``. ``sort`` is "newest" (default)
    or "count"; ``pct`` is the bar width relative to the most played album (the extra rows below don't count). With ``released_by`` (a year),
    saved albums released after it are left out: they didn't exist yet at that show. ``no_album``
    ({"n", "songs", "tracks"} for played songs filed under no album) adds a last "No album" row.
    ``by`` picks what the bars and the "count" sort measure: "n" (plays) or "songs" (different songs).
    ``covers`` (same shape as ``no_album``, tracks carry "by") adds a last "No album covers" row.
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
        result.append({"title": gettext("No album"), "year": None, "is_none": True, **no_album})
    if covers and covers["n"]:
        result.append({"title": gettext("No album covers"), "year": None, "is_none": True, "is_covers": True,
                       **covers})
    # The most played album fills the bar; "No album" and "No album covers" don't set the scale (capped at full).
    peak = max((r[by] for r in result if not r.get("is_none")), default=0) or max((r[by] for r in result), default=0)
    for r in result:
        r["pct"] = min(100, round(r[by] / peak * 100)) if peak else 0
    return result


def top_albums(rows: list[dict], by: str = "n") -> list[dict]:
    """Every album tied for the most plays (``by="n"``) or the most different songs (``by="songs"``), oldest
    first, and marks them ``is_top`` (the green bar). "No album" and "No album covers" never count."""
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
    A cover filed under one of the artist's own albums (they recorded it) counts with that album, marked
    ``is_cover``. Returns (own songs, {band: [songs]}, "No album covers" row {"n", "songs", "tracks"}).
    """
    bands = {normalize_name(b.name): b for b in artist.member_of.all()}
    own_albums = {normalize_name(title) for title in artist.albums.values_list("title", flat=True)}
    own, by_band = [], {}
    covers = {"n": 0, "songs": 0, "tracks": []}
    for song in songs:
        cover = normalize_name(song["cover_of"])
        if cover and song["album_title"] and normalize_name(song["album_title"]) in own_albums:
            own.append({**song, "is_cover": True})
        elif cover and cover in bands:
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
        if song.get("is_cover"):  # a cover the artist recorded on this album: shown in italics
            track.update(is_cover=True, by=song["cover_of"])
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


# --------------------------------------------------------------------------- album dropdowns (admins)
#
# Songs are filed under albums picked from a list; new albums are added in "Manage albums" only.


def album_groups(artist: Artist, songs=()) -> list[dict]:
    """The albums a song of ``artist`` can be filed under, as [{"artist", "albums"}]: the artist's own, then
    each band they were in, then the original artist of any of ``songs`` that are covers (if we know them)."""
    owners = [artist, *artist.member_of.order_by("name")]
    for song in songs:
        if song.cover_of_name:
            owner = album_owner(song)
            if owner is not None and owner not in owners:
                owners.append(owner)
    groups = [{"artist": owner, "albums": list(owner.albums.all())} for owner in owners]
    return [g for g in groups if g["albums"]]


def mark_album_choices(songs, groups: list[dict]) -> dict[int, Album]:
    """Sets ``song.album_choice`` (the pk of the listed album it's filed under, or None) and ``song.album_unlisted``
    (it has an album that isn't in the lists) on each song. Returns every album that can be picked, by pk."""
    allowed = {album.pk: album for g in groups for album in g["albums"]}
    by_title: dict[str, list[Album]] = {}
    for album in allowed.values():
        by_title.setdefault(normalize_name(album.title), []).append(album)
    names = {g["artist"].pk: normalize_name(g["artist"].name) for g in groups}
    for song in songs:
        matches = by_title.get(normalize_name(song.album_title), []) if song.album_title else []
        # The same title can be the artist's and a band's: prefer the covered artist's, else the artist's own.
        cover = normalize_name(song.cover_of_name) if song.cover_of_name else ""

        def preferred(album: Album, song=song, cover=cover) -> bool:
            return names.get(album.artist_id) == cover if cover else album.artist_id == song.artist_id

        matches = sorted(matches, key=lambda album: not preferred(album))
        song.album_choice = matches[0].pk if matches else None
        song.album_unlisted = bool(song.album_title) and song.album_choice is None
    return allowed


KEEP_ALBUM = "keep"  # dropdown value: leave a song's album as it is (one that isn't in the lists)


def choose_album(song: Song, value: str | None, allowed: dict[int, Album]) -> bool:
    """Files ``song`` under a dropdown pick: "" for no album, an album pk, or ``KEEP_ALBUM``. Doesn't save.
    Returns whether the song changed; unknown values (and picking what it already has) change nothing."""
    value = (value or "").strip()
    if value == KEEP_ALBUM or (value and value == str(getattr(song, "album_choice", None))):
        return False
    if not value:
        if not song.album_title:
            return False
        title, year = "", None
    else:
        album = allowed.get(int(value)) if value.isdigit() else None
        if album is None:
            return False
        title, year = album.title, album.year
    if normalize_name(title) != normalize_name(song.album_title):
        song.album_mbid = ""  # no longer the MusicBrainz match
    song.album_title, song.album_year = title, year
    song.album_checked_at = timezone.now()  # a manual answer is never looked up again
    return True


# --------------------------------------------------------------------------- album colours
#
# "Colour by album" on setlists: each of an artist's albums keeps one colour, by its place in their
# discography (oldest first). Ten hues in a fixed order, then the same cycle in four other shades: 50 colours,
# after which it starts over.

ALBUM_HUES = [215, 140, 275, 0, 28, 50, 175, 325, 195, 90]  # blue, green, purple, red, orange, yellow, teal, pink, cyan, lime
ALBUM_SHADES = [(70, 58), (60, 72), (75, 42), (45, 64), (85, 32)]  # (saturation %, lightness %) per round of hues


def album_color(index: int) -> str:
    """The colour of an artist's ``index``-th album (0 = their first)."""
    index %= len(ALBUM_HUES) * len(ALBUM_SHADES)
    saturation, lightness = ALBUM_SHADES[index // len(ALBUM_HUES)]
    return f"hsl({ALBUM_HUES[index % len(ALBUM_HUES)]} {saturation}% {lightness}%)"


def album_colors(artist: Artist, lps_only: bool = False) -> dict[str, str]:
    """Normalized album title → colour, for the artist's albums in "Manage albums" (oldest first; undated last).
    With ``lps_only``, only their LPs, coloured in order among themselves (EPs, compilations… get none)."""
    albums = artist.albums.filter(kind=Album.Kind.LP) if lps_only else artist.albums.all()
    albums = sorted(albums, key=lambda a: (a.year is None, a.year or 0, a.title.casefold()))
    colors: dict[str, str] = {}
    for album in albums:
        colors.setdefault(normalize_name(album.title), album_color(len(colors)))
    return colors
