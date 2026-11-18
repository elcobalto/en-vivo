"""YouTube links for events: parse the video id and (best effort) fetch its title."""
from __future__ import annotations

import json
import logging
import re
import ssl
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def youtube_id(url: str) -> str:
    """The 11-character id from any common YouTube link (watch, youtu.be, shorts, live, embed), or ""."""
    url = (url or "").strip()
    if ID_RE.match(url):
        return url
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        if parsed.path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            parts = parsed.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v"}:
                candidate = parts[1]
    return candidate if ID_RE.match(candidate) else ""


def video_title(video_id: str) -> str:
    """The video's title from YouTube's public oEmbed endpoint (no key), or "" if unavailable."""
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    url = "https://www.youtube.com/oembed?format=json&url=" + quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
    try:
        with urlopen(Request(url, headers={"User-Agent": "EnVivo/1.0"}), timeout=4, context=ctx) as resp:
            return (json.loads(resp.read().decode("utf-8") or "{}").get("title") or "")[:200]
    except Exception as e:  # private, removed or offline: the video still works without a title
        logger.info("No YouTube title for %s: %r", video_id, e)
        return ""
