"""YouTube Data API v3: read the public 'Inbox' playlist and fetch video metadata."""
from __future__ import annotations

import re

import requests

from common import http

API = "https://www.googleapis.com/youtube/v3"
DUR_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
CHAPTER_RE = re.compile(r"^\s*\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?\s*[-–—:|]?\s*(.+)$")


def iso_duration_seconds(s: str | None) -> int:
    m = DUR_RE.match(s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def chapters(description: str) -> list[str]:
    """Lines like '00:00 Intro' / '1:02:03 - Topic' from the description."""
    out = [f"{m.group(1)} {m.group(2).strip()}" for line in (description or "").splitlines()
           if (m := CHAPTER_RE.match(line))]
    return out if len(out) >= 3 else []


class YouTube:
    def __init__(self, api_key: str, retries: int = 3):
        self.s = requests.Session()
        self.s.headers["X-Goog-Api-Key"] = api_key   # header, so the key never appears in a URL or log
        self.retries = retries

    def playlist_items(self, playlist_id: str) -> list[dict]:
        out, token = [], None
        while True:
            params = {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": 50}
            if token:
                params["pageToken"] = token
            r = http(self.s, "GET", f"{API}/playlistItems", retries=self.retries, params=params)
            for it in r.get("items", []):
                out.append({
                    "video_id": it["contentDetails"]["videoId"],
                    "added_at": it["snippet"].get("publishedAt"),   # = when YOU added it to the playlist
                    "title": it["snippet"].get("title", ""),
                })
            token = r.get("nextPageToken")
            if not token:
                return out

    def videos(self, ids: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(ids), 50):
            r = http(self.s, "GET", f"{API}/videos", retries=self.retries, params={
                "part": "snippet,contentDetails,statistics", "id": ",".join(ids[i:i + 50]), "maxResults": 50})
            for v in r.get("items", []):
                sn, cd, st = v["snippet"], v["contentDetails"], v.get("statistics", {})
                out[v["id"]] = {
                    "title": sn.get("title", ""),
                    "channel": sn.get("channelTitle", ""),
                    "published": sn.get("publishedAt", ""),
                    "description": sn.get("description", ""),
                    "duration_sec": iso_duration_seconds(cd.get("duration")),
                    "views": int(st.get("viewCount", 0) or 0),
                    "likes": int(st.get("likeCount", 0) or 0),
                    "live": sn.get("liveBroadcastContent", "none"),
                }
        return out

    def is_short(self, video_id: str) -> bool:
        """youtube.com/shorts/<id> answers 200 for Shorts and redirects for normal videos."""
        try:
            r = self.s.get(f"https://www.youtube.com/shorts/{video_id}", allow_redirects=False, timeout=15,
                           headers={"X-Goog-Api-Key": None})  # None = don't send the key to youtube.com
            return r.status_code == 200
        except requests.RequestException:
            return False
