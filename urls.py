"""URL parsing: find the first link in shared text, normalise it, derive a stable Item ID."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
X_HOSTS = {
    "x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com", "m.twitter.com",
    "fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com", "nitter.net",
}
YT_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}
TRACKING_PARAMS = {"s", "t", "si", "feature", "pp", "ref", "ref_src", "fbclid", "igshid"}


@dataclass(frozen=True)
class ParsedUrl:
    platform: str      # "YouTube" | "X" | "Other"
    url: str           # canonical URL
    item_id: str       # "yt:<id>" | "x:<id>" | "url:<hash>"
    is_short: bool = False


def extract_first_url(text: str | None) -> str | None:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0).rstrip(".,;:!?") if m else None


def _host(netloc: str) -> str:
    host = netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def parse(url: str) -> ParsedUrl:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    host = _host(parts.netloc)
    path = parts.path
    query = dict(parse_qsl(parts.query))

    # ---- YouTube ----
    vid, is_short = None, False
    if host == "youtu.be":
        vid = path.strip("/").split("/")[0]
    elif host in YT_HOSTS:
        if path == "/watch":
            vid = query.get("v")
        else:
            m = re.match(r"^/(shorts|live|embed|v)/([^/?#]+)", path)
            if m:
                vid, is_short = m.group(2), m.group(1) == "shorts"
    if vid and YT_ID_RE.match(vid):
        return ParsedUrl("YouTube", f"https://www.youtube.com/watch?v={vid}", f"yt:{vid}", is_short)

    # ---- X / Twitter ----
    if host in X_HOSTS:
        m = re.match(r"^/(?:([A-Za-z0-9_]{1,15})|i(?:/web)?)/status(?:es)?/(\d+)", path)
        if m:
            user, sid = m.group(1) or "i", m.group(2)
            return ParsedUrl("X", f"https://x.com/{user}/status/{sid}", f"x:{sid}")

    # ---- Anything else: drop tracking params, hash for a stable ID ----
    clean_q = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
    canon = urlunsplit(("https", host, path.rstrip("/") or "/", urlencode(clean_q), ""))
    return ParsedUrl("Other", canon, "url:" + hashlib.sha1(canon.encode()).hexdigest()[:16])
