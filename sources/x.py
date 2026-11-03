"""X posts via the public FxTwitter API (no key). Uses /2/thread/{id} so self-threads come back unrolled."""
from __future__ import annotations

import math
import re

import requests

from common import APIError, http

API = "https://api.fxtwitter.com/2"
THREAD_MARKERS = re.compile(r"🧵|\b1/\d*\b|\(1/\d+\)|\ba thread\b|\bthread\s*👇|👇\s*thread", re.IGNORECASE)


class XFetchError(Exception):
    pass


def _article_text(article: dict) -> str:
    blocks = (article.get("content") or {}).get("blocks") or []
    body = "\n".join(b.get("text", "") for b in blocks if b.get("text"))
    return "\n\n".join(x for x in [article.get("title", ""), body or article.get("preview_text", "")] if x)


def parse(payload: dict, post_id: str, wpm: int = 230) -> dict:
    code = payload.get("code", 200)
    st = payload.get("status")
    if code != 200 or not st:
        raise XFetchError(f"FxTwitter code {code}: {payload.get('message') or 'post not found, deleted or private'}")
    if st.get("type") == "tombstone":
        raise XFetchError(f"Post unavailable ({st.get('reason')}): {st.get('message', '')}")

    author = st.get("author") or {}
    handle = author.get("screen_name", "")

    # Self-thread: keep the author's own posts, in order, de-duplicated.
    posts, seen = [], set()
    for p in (payload.get("thread") or []) + [st]:
        if not p or p.get("type") == "tombstone" or p.get("id") in seen:
            continue
        if (p.get("author") or {}).get("screen_name", handle) != handle:
            continue
        seen.add(p.get("id"))
        posts.append(p)
    posts.sort(key=lambda p: p.get("created_timestamp") or 0)
    if not posts:
        posts = [st]

    article = st.get("article")
    if article:
        text = _article_text(article)
    else:
        text = "\n\n".join(p.get("text", "") for p in posts)

    videos = []
    for p in posts:
        for v in (p.get("media") or {}).get("videos") or []:
            if v.get("type") == "video":
                videos.append(float(v.get("duration") or 0))
    video_sec = sum(videos)

    quote = st.get("quote")
    quote_info = None
    if quote and quote.get("type") != "tombstone":
        quote_info = {"author": (quote.get("author") or {}).get("screen_name", ""), "text": quote.get("text", "")}

    card = st.get("card") or {}
    is_thread = len(posts) > 1 or bool(THREAD_MARKERS.search(st.get("text", "")))
    if article:
        fmt = "Article"
    elif is_thread:
        fmt = "Thread"
    elif videos:
        fmt = "Post + Video"
    else:
        fmt = "Post"

    words = len(re.findall(r"\w+", text + " " + (quote_info or {}).get("text", "")))
    minutes = max(1, math.ceil(words / wpm + video_sec / 60))
    first_line = (article or {}).get("title") or (st.get("text") or "").strip().split("\n")[0]
    title = f"@{handle}: {first_line[:90]}" if handle else first_line[:100]

    partial_thread = is_thread and len(posts) == 1
    return {
        "post_id": post_id,
        "title": title,
        "text": text,
        "author_name": author.get("name", ""),
        "handle": handle,
        "followers": author.get("followers"),
        "likes": st.get("likes"),
        "reposts": st.get("reposts"),
        "views": st.get("views"),
        "created_at": st.get("created_at"),
        "quote": quote_info,
        "card": {"url": card.get("url"), "title": card.get("title"), "domain": card.get("domain")} if card else None,
        "video_sec": video_sec,
        "format": fmt,
        "minutes": minutes,
        "thread_posts": len(posts),
        "partial_thread": partial_thread,
    }


class X:
    def __init__(self, retries: int = 3):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "content-inbox/1.0 (personal reading list)"
        self.retries = retries

    def fetch(self, post_id: str, wpm: int = 230) -> dict:
        try:
            payload = http(self.s, "GET", f"{API}/thread/{post_id}", retries=self.retries)
        except APIError as e:
            if e.status in (401, 403, 404):
                raise XFetchError(f"Post unavailable (HTTP {e.status}): deleted, private or wrong link")
            raise
        return parse(payload, post_id, wpm)
