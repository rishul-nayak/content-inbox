"""Shared helpers: config, env, secret-safe logging, HTTP retries, time."""
from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).resolve().parent
SECRET_NAMES = ["NOTION_TOKEN", "GEMINI_API_KEY", "YOUTUBE_API_KEY"]
PT = ZoneInfo("America/Los_Angeles")   # Gemini daily quotas reset at midnight Pacific


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Tiny .env loader for local runs (no extra dependency)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def env(name: str, required: bool = True) -> str | None:
    v = os.environ.get(name, "").strip()
    if required and not v:
        sys.exit(f"Missing environment variable / GitHub secret: {name}")
    return v or None


def setting(name: str, cfg: dict) -> str:
    """Env var if set, else config.yaml ids.<name>. Exits with a clear message if neither."""
    v = (os.environ.get(name, "").strip() or str((cfg.get("ids") or {}).get(name) or "").strip())
    if not v:
        sys.exit(f"Missing setting: {name} (set it in config.yaml under ids:, or as a GitHub secret)")
    return v


def redact(text: str) -> str:
    """Remove any secret value that slipped into a message."""
    text = str(text)
    for n in SECRET_NAMES:
        v = os.environ.get(n)
        if v and len(v) > 6:
            text = text.replace(v, "***")
    return text


def log(*parts) -> None:
    print(redact(" ".join(str(p) for p in parts)), flush=True)


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(redact(f"HTTP {status}: {message}"[:500]))
        self.status = status


def is_retryable(status: int) -> bool:
    return status == 429 or status >= 500


def backoff_sleep(attempt: int, retry_after: str | None = None) -> None:
    if retry_after:
        try:
            time.sleep(min(float(retry_after), 60))
            return
        except ValueError:
            pass
    time.sleep(min(2 ** attempt + random.random(), 30))


def http(session: requests.Session, method: str, url: str, retries: int = 3, **kw) -> dict:
    """JSON request with exponential backoff on 429/5xx and network errors."""
    kw.setdefault("timeout", 60)
    last = None
    for attempt in range(retries + 1):
        try:
            r = session.request(method, url, **kw)
        except requests.RequestException as e:
            last = APIError(0, f"network error: {type(e).__name__}")
            if attempt < retries:
                backoff_sleep(attempt)
                continue
            raise last
        if r.status_code < 300:
            return r.json() if r.content else {}
        try:
            body = r.json()
            msg = body.get("message") or body.get("error", {}).get("message") or str(body)[:300]
        except ValueError:
            msg = r.text[:300]
        last = APIError(r.status_code, msg)
        if is_retryable(r.status_code) and attempt < retries:
            backoff_sleep(attempt, r.headers.get("Retry-After"))
            continue
        raise last
    raise last  # pragma: no cover


def now_local(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def today_local(tz: str) -> str:
    return now_local(tz).date().isoformat()


def pt_date() -> str:
    return datetime.now(PT).date().isoformat()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def days_ago(tz: str, n: int) -> str:
    return (now_local(tz).date() - timedelta(days=n)).isoformat()


STATE_PATH = ROOT / "state" / "last_run.json"


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
