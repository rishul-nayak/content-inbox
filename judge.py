"""Gemini judge: rubric prompt, JSON response schema, video + batched X calls, robust parsing."""
from __future__ import annotations

import json
import time
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from common import redact

ROOT = Path(__file__).resolve().parent
RED_FLAGS = ["Hype", "Clickbait", "Selling a course", "No evidence", "Survivorship bias", "Outdated", "Engagement bait", "Recycled"]
VALUE_TYPES = ["Knowledge", "Action", "Tool/Resource", "Inspiration", "Entertainment"]
FORMATS = ["Video", "Short", "Post", "Thread", "Post + Video", "Article"]
JUDGED_FROM = ["Full video", "Post text", "Metadata only"]
SCORE_FIELDS = ["timeless", "substance", "credibility", "relevance", "actionability"]

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "item_id": {"type": "string"},
        "title": {"type": "string"},
        "format": {"type": "string", "enum": FORMATS},
        **{f: {"type": "integer", "minimum": 0, "maximum": 5} for f in SCORE_FIELDS},
        "red_flags": {"type": "array", "items": {"type": "string", "enum": RED_FLAGS}},
        "verdict_suggestion": {"type": "string", "enum": ["Gem", "Solid", "Skim", "Skip"]},
        "value_type": {"type": "string", "enum": VALUE_TYPES},
        "domains": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
        "summary": {"type": "string"},
        "why": {"type": "string"},
        "takeaways": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "action_item": {"type": "string"},
        "judged_from": {"type": "string", "enum": JUDGED_FROM},
    },
    "required": ["item_id", "title", "format", *SCORE_FIELDS, "red_flags", "verdict_suggestion", "value_type",
                 "domains", "summary", "why", "takeaways", "evidence", "action_item", "judged_from"],
}
BATCH_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": ITEM_SCHEMA}}, "required": ["items"]}


class QuotaExhausted(Exception):
    """Daily quota for this model is used up."""


class JudgeError(Exception):
    pass


class Overloaded(JudgeError):
    """Model kept answering 5xx (overloaded) after all retries."""


# ---------------- prompt ----------------
def build_system_prompt(cfg: dict, calibration: list[dict], existing_domains: list[str]) -> str:
    rubric = (ROOT / "rubric.md").read_text()
    focus = "\n".join(f"{i}. {a}" for i, a in enumerate(cfg["focus_areas"], 1))
    if calibration:
        lines = ["## Calibration from the reader's own corrections (learn their taste)"]
        for c in calibration:
            lines.append(f"- \"{c['title'][:100]}\" ({c.get('author') or '?'}): judge said {c['judge']}, reader said {c['mine']}."
                         + (f" Reader's note: {c['takeaway'][:150]}" if c.get("takeaway") else ""))
        calib = "\n".join(lines)
    else:
        calib = ""
    return (rubric.replace("{focus_areas}", focus)
                  .replace("{existing_domains}", ", ".join(existing_domains[:60]) or "(none yet)")
                  .replace("{calibration}", calib))


# ---------------- parsing ----------------
def _int05(x) -> int:
    try:
        return max(0, min(5, int(round(float(x)))))
    except (TypeError, ValueError):
        return 0


def normalise(obj: dict, item_id: str, judged_from_default: str) -> dict:
    """Coerce one judgement into a safe, complete shape."""
    out = dict(obj or {})
    out["item_id"] = item_id
    for f in SCORE_FIELDS:
        out[f] = _int05(out.get(f))
    out["red_flags"] = [f for f in (out.get("red_flags") or []) if f in RED_FLAGS]
    if out.get("value_type") not in VALUE_TYPES:
        out["value_type"] = "Knowledge"
    if out.get("judged_from") not in JUDGED_FROM:
        out["judged_from"] = judged_from_default
    if out["judged_from"] == "Metadata only":   # rubric rule, enforced in code too
        out["substance"] = min(out["substance"], 3)
        out["credibility"] = min(out["credibility"], 3)
    out["domains"] = [str(d).strip()[:40] for d in (out.get("domains") or []) if str(d).strip()][:3] or ["General"]
    for k in ("takeaways", "evidence"):
        out[k] = [str(x).strip() for x in (out.get(k) or []) if str(x).strip()]
    for k in ("summary", "why", "action_item", "title"):
        out[k] = str(out.get(k) or "").strip()
    return out


def parse_single(raw: str, item_id: str, judged_from_default: str) -> dict:
    data = json.loads(raw)
    if isinstance(data, dict) and "items" in data and isinstance(data["items"], list) and data["items"]:
        data = data["items"][0]
    if not isinstance(data, dict):
        raise JudgeError("Judge returned non-object JSON")
    return normalise(data, item_id, judged_from_default)


def parse_batch(raw: str, item_ids: list[str]) -> dict[str, dict]:
    data = json.loads(raw)
    items = data.get("items", []) if isinstance(data, dict) else data
    by_id = {}
    for it in items or []:
        iid = str(it.get("item_id", "")).strip()
        if iid in item_ids and iid not in by_id:
            it = dict(it, judged_from="Post text")   # X posts are always judged from their text
            by_id[iid] = normalise(it, iid, "Post text")
    return by_id


# ---------------- calls ----------------
class Judge:
    def __init__(self, api_key: str, cfg: dict, system_prompt: str, retries: int = 3):
        self.client = genai.Client(api_key=api_key)
        self.cfg = cfg
        self.g = cfg["gemini"]
        self.system = system_prompt
        self.retries = retries
        self.video_calls = 0      # successful + failed calls to the video model this run

    def _config(self, model: str, schema: dict, media_low: bool) -> types.GenerateContentConfig:
        kw = dict(
            system_instruction=self.system,
            response_mime_type="application/json",
            response_json_schema=schema,
            temperature=0.2,
        )
        if media_low:
            kw["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_LOW
        if model == self.g["video_model"] and self.g.get("thinking_level"):
            kw["thinking_config"] = types.ThinkingConfig(thinking_level=self.g["thinking_level"])
        return types.GenerateContentConfig(**kw)

    def _call(self, model: str, contents, config) -> str:
        last = None
        for attempt in range(self.retries + 1):
            if model == self.g["video_model"]:
                self.video_calls += 1
            try:
                resp = self.client.models.generate_content(model=model, contents=contents, config=config)
                text = resp.text
                if not text:
                    raise JudgeError("Empty response (possibly blocked)")
                return text
            except genai_errors.APIError as e:
                msg = redact(str(e))[:400]
                code = getattr(e, "code", 0) or 0
                if code == 429:
                    if "PerDay" in msg or "per day" in msg.lower() or "daily" in msg.lower():
                        raise QuotaExhausted(msg)
                    last = JudgeError(f"429 rate limit: {msg}")
                    if attempt < self.retries:
                        time.sleep(30 * (attempt + 1))   # per-minute limit: wait it out
                        continue
                    raise QuotaExhausted(msg)
                if code >= 500:
                    last = Overloaded(f"{code}: {msg}")
                    if attempt < self.retries:
                        time.sleep(min(2 ** attempt * 5, 40))
                        continue
                    raise last
                raise JudgeError(f"{code}: {msg}")
        raise last or JudgeError("unknown error")

    # ---- YouTube ----
    def judge_video(self, item_id: str, url: str, meta: dict, model: str, mode: str) -> dict:
        """mode: "static" (frames + audio), "agentic" (model jumps through the video), "metadata"."""
        info = video_context(meta)
        if mode in ("static", "agentic"):
            if mode == "static":
                part = types.Part(file_data=types.FileData(file_uri=url),
                                  video_metadata=types.VideoMetadata(fps=self.g.get("video_fps", 0.2)))
            else:
                part = types.Part(file_data=types.FileData(file_uri=url),
                                  media_processing=types.MediaProcessing.AGENTIC)
            prompt = (f"Judge this YouTube video. item_id = {item_id}. You have the full video. "
                      f"judged_from must be \"Full video\".\n\n{info}")
            contents = types.Content(role="user", parts=[part, types.Part(text=prompt)])
        else:
            prompt = (f"Judge this YouTube video from its METADATA ONLY (the video itself was not available). "
                      f"item_id = {item_id}. judged_from must be \"Metadata only\".\n\n{info}")
            contents = prompt
        watched = mode != "metadata"
        raw = self._call(model, contents, self._config(model, ITEM_SCHEMA, media_low=watched))
        j = parse_single(raw, item_id, "Full video" if watched else "Metadata only")
        if watched:
            j["judged_from"] = "Full video"   # we sent the video; don't let the model self-cap
        return j

    # ---- X posts (batched) ----
    def judge_x_batch(self, posts: list[dict]) -> dict[str, dict]:
        blocks = []
        for p in posts:
            d = p["data"]
            parts = [f"### item_id: {p['item_id']}",
                     f"Author: {d['author_name']} (@{d['handle']}), followers: {d.get('followers')}",
                     f"Format: {d['format']}" + (" (thread may be incomplete: only part was available)" if d.get("partial_thread") else ""),
                     f"Likes: {d.get('likes')}, reposts: {d.get('reposts')}, posted: {d.get('created_at')}",
                     f"Embedded video: {round(d['video_sec'] / 60, 1)} min (not watched; judge from text)" if d.get("video_sec") else "",
                     "Text:\n" + d["text"][:12000]]
            if d.get("quote"):
                parts.append(f"Quoted post by @{d['quote']['author']}:\n{d['quote']['text'][:2000]}")
            if d.get("card") and d["card"].get("url"):
                parts.append(f"Link card: {d['card'].get('title') or ''} ({d['card'].get('domain') or d['card']['url']})")
            blocks.append("\n".join(x for x in parts if x))
        prompt = ("Judge each X post below separately. Return one object per post in `items`, with the exact item_id given. "
                  "judged_from = \"Post text\".\n\n" + "\n\n---\n\n".join(blocks))
        model = self.g["text_model"]
        raw = self._call(model, prompt, self._config(model, BATCH_SCHEMA, media_low=False))
        return parse_batch(raw, [p["item_id"] for p in posts])


def video_context(meta: dict) -> str:
    from sources.youtube import chapters
    ch = chapters(meta.get("description", ""))
    lines = [
        f"Title: {meta.get('title')}",
        f"Channel: {meta.get('channel')}",
        f"Length: {round(meta.get('duration_sec', 0) / 60, 1)} min",
        f"Published: {meta.get('published')}",
        f"Views: {meta.get('views')}, likes: {meta.get('likes')}",
        "Description:\n" + (meta.get("description") or "")[:3000],
    ]
    if ch:
        lines.append("Chapters:\n" + "\n".join(ch[:40]))
    return "\n".join(lines)
