"""End-to-end run against in-memory fakes of Notion, YouTube, FxTwitter and Gemini."""
import json
import uuid

import pipeline
from common import load_config
from judge import Judge, build_system_prompt

CFG = load_config()


def _shape(name, v):
    t = next(iter(v))
    val = v[t]
    if t in ("title", "rich_text"):
        val = [dict(x, plain_text=x["text"]["content"]) for x in val]
    return {"type": t, t: val}


class FakeNotion:
    def __init__(self):
        self.pages, self.bodies, self.trashed = {}, {}, set()

    def data_source_id(self, _): return "ds"

    def query(self, ds, filter=None, sorts=None, limit=None):
        return sorted([p for k, p in self.pages.items() if k not in self.trashed], key=lambda p: p["created_time"])

    def create_page(self, ds, props, children=None):
        pid = str(uuid.uuid4())
        self.pages[pid] = {"id": pid, "created_time": f"2026-11-0{len(self.pages) + 1}T00:00:00Z",
                           "last_edited_time": "", "properties": {k: _shape(k, v) for k, v in props.items()}}
        return self.pages[pid]

    def update_page(self, pid, props=None, in_trash=None):
        if in_trash:
            self.trashed.add(pid)
        for k, v in (props or {}).items():
            self.pages[pid]["properties"][k] = _shape(k, v)

    def append_children(self, pid, children): self.bodies.setdefault(pid, []).extend(children)
    def list_children(self, pid): return [{"id": f"{pid}:{i}", "type": "paragraph"} for i, _ in enumerate(self.bodies.get(pid, []))]
    def delete_block(self, bid):
        pid, _ = bid.rsplit(":", 1)
        self.bodies[pid] = self.bodies[pid][:-1]
    def list_views(self, db): return [{"id": "v1", "name": "📅 Today"}]
    def update_view(self, vid, **kw): self.view_update = kw


class FakeYT:
    def playlist_items(self, pl):
        return [{"video_id": "3kRB2TXewus", "added_at": "2026-11-03T10:00:00Z", "title": "Pricing deep dive"},
                {"video_id": "LONGLONGLON", "added_at": "2026-11-03T11:00:00Z", "title": "3h podcast"}]

    def videos(self, ids):
        base = {"channel": "Chan", "published": "2026-01-01", "description": "00:00 a\n01:00 b\n02:00 c",
                "views": 1, "likes": 1, "live": "none"}
        return {"3kRB2TXewus": dict(base, title="Pricing deep dive", duration_sec=1500),
                "LONGLONGLON": dict(base, title="3h podcast", duration_sec=3 * 3600)}

    def is_short(self, vid): return False


class FakeX:
    def fetch(self, pid, wpm=230):
        return {"post_id": pid, "title": "@kyr0stack: hello", "text": "A thread 🧵 " * 30, "author_name": "K",
                "handle": "kyr0stack", "followers": 10, "likes": 1, "reposts": 1, "views": 1, "created_at": "",
                "quote": None, "card": None, "video_sec": 0, "format": "Thread", "minutes": 2, "thread_posts": 3,
                "partial_thread": False}


class FakeResp:
    def __init__(self, text): self.text = text


class FakeModels:
    def __init__(self): self.calls, self.contents_seen = [], []

    def generate_content(self, model, contents, config):
        self.calls.append((model, config))
        self.contents_seen.append(contents)
        item = {"title": "t", "format": "Video", "timeless": 5, "substance": 5, "credibility": 4, "relevance": 5,
                "actionability": 4, "red_flags": [], "verdict_suggestion": "Gem", "value_type": "Action",
                "domains": ["Pricing"], "summary": "s", "why": "w", "takeaways": ["a", "b", "c"],
                "evidence": ["01:02 — quote"], "action_item": "Do X", "judged_from": "Full video"}
        text = contents if isinstance(contents, str) else contents.parts[-1].text
        if "item_id: x:" in text:
            return FakeResp(json.dumps({"items": [dict(item, item_id="x:2117251353168752764", format="Thread",
                                                       judged_from="Post text")]}))
        iid = text.split("item_id = ")[1].split(".")[0]
        if "METADATA ONLY" in text:
            item = dict(item, judged_from="Metadata only")
        return FakeResp(json.dumps(dict(item, item_id=iid)))


def test_full_run(monkeypatch):
    run = pipeline.Run(CFG, dry=False)
    run.notion, run.ds_id, run.db_id = FakeNotion(), "ds", "db"
    # Shortcut-created rows: an X post + a duplicate of the playlist video
    run.notion.create_page("ds", {"Name": {"title": [{"type": "text", "text": {"content":
                           "https://x.com/kyr0stack/status/2117251353168752764?a=ewq&s=20"}}]},
                           "Pipeline": {"select": {"name": "New"}}})
    rows = pipeline.load_rows(run)
    pipeline.ingest_youtube(run, rows, FakeYT(), "PL")
    run.notion.create_page("ds", {"Name": {"title": [{"type": "text", "text": {"content": "x"}}]},
                                  "URL": {"url": "https://youtu.be/3kRB2TXewus?si=abc"},
                                  "Pipeline": {"select": {"name": "New"}}})
    rows = pipeline.normalise_and_dedupe(run, pipeline.load_rows(run))
    assert run.counts["duplicates"] == 1

    judge = Judge("fake", CFG, build_system_prompt(CFG, [], []), retries=0)
    fm = FakeModels()
    judge.client = type("C", (), {"models": fm})()
    usage = {"pt_date": "x", "video_calls": 0, "youtube_minutes": 0}
    pending = [r for r in rows if r.pipeline in pipeline.PENDING and r.item_id]
    pipeline.process_youtube(run, [r for r in pending if r.platform == "YouTube"], FakeYT(), judge, usage)
    pipeline.process_x(run, [r for r in pending if r.platform == "X"], FakeX(), judge)

    rows = pipeline.load_rows(run)
    by = {r.item_id: r for r in rows}
    assert set(by) == {"yt:3kRB2TXewus", "yt:LONGLONGLON", "x:2117251353168752764"}
    assert all(r.pipeline == "Ready" for r in rows), run.errors
    assert by["yt:3kRB2TXewus"].verdict == "💎 Gem" and by["yt:3kRB2TXewus"].time == 25
    assert usage["video_calls"] == 1 and usage["youtube_minutes"] == 205.0   # 25 + 180 (agentic counts too)
    models = [m for m, _ in fm.calls]
    assert models.count("gemini-3.8-flash") == 1          # 3-hour podcast went to Flash-Lite in agentic mode
    long_props = run.notion.pages[by["yt:LONGLONGLON"].id]["properties"]
    assert long_props["Judged From"]["select"]["name"] == "Full video"
    assert any(getattr(p, "media_processing", None) is not None for p in fm.contents_seen[1].parts)
    assert by["x:2117251353168752764"].platform == "X"

    run.counts["planned"] = 0
    pipeline.plan_today(run, rows, "2026-11-03")
    planned = [r for r in pipeline.load_rows(run) if r.planned_for == "2026-11-03"]
    assert planned and sum(r.time for r in planned) <= 120


def test_quota_fallback(monkeypatch):
    from google.genai import errors as ge
    run = pipeline.Run(CFG, dry=False)
    run.notion, run.ds_id = FakeNotion(), "ds"
    rows = []
    pipeline.ingest_youtube(run, rows, FakeYT(), "PL")
    judge = Judge("fake", CFG, build_system_prompt(CFG, [], []), retries=0)
    fm = FakeModels()
    real = fm.generate_content

    def gen(model, contents, config):
        if model == "gemini-3.8-flash":
            raise ge.ClientError(429, {"error": {"code": 429, "message": "Quota exceeded GenerateRequestsPerDayPerProjectPerModel-FreeTier", "status": "RESOURCE_EXHAUSTED"}})
        return real(model, contents, config)
    fm.generate_content = gen
    judge.client = type("C", (), {"models": fm})()
    usage = {"pt_date": "x", "video_calls": 0, "youtube_minutes": 0}
    rows = pipeline.load_rows(run)
    pipeline.process_youtube(run, rows, FakeYT(), judge, usage)
    rows = pipeline.load_rows(run)
    assert all(r.pipeline == "Ready" for r in rows), run.errors
    assert usage["video_model_exhausted"]
    short = next(r for r in rows if r.item_id == "yt:3kRB2TXewus")
    why = run.notion.pages[short.id]["properties"]["Why"]["rich_text"][0]["plain_text"]
    assert "gemini-3.5-flash-lite" in why


def test_digest_builds():
    import digest
    from pipeline import Row
    rows = [Row(id="1", name="Gem A", verdict="💎 Gem", pipeline="Ready", saved_on="2026-11-02", time=20,
                quality=9, relevance=5, actionability=4, domains=["Pricing"], url="u"),
            Row(id="2", name="Done B", verdict="✅ Solid", my_rating="Skip", pipeline="Ready", done=True,
                done_on="2099-01-01", saved_on="2026-09-01", time=10, takeaway="note", domains=["AI Agents"]),
            Row(id="3", name="Old C", verdict="👀 Skim", pipeline="Ready", saved_on="2020-01-01", time=5)]
    title, blocks = digest.build(rows, CFG)
    text = json.dumps(blocks, ensure_ascii=False)
    assert title.startswith("Week of ") and "Gem A" in text and "Old C" in text and "Agreement: 0/1" in text
    assert "Judge too generous: 1" in text and "note" in text


def test_overload_fallback_watches_with_lite():
    from google.genai import errors as ge
    run = pipeline.Run(CFG, dry=False)
    run.notion, run.ds_id = FakeNotion(), "ds"
    rows = []
    pipeline.ingest_youtube(run, rows, FakeYT(), "PL")
    judge = Judge("fake", CFG, build_system_prompt(CFG, [], []), retries=0)
    fm = FakeModels()
    real = fm.generate_content

    def gen(model, contents, config):
        if model == "gemini-3.8-flash":
            raise ge.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
        return real(model, contents, config)
    fm.generate_content = gen
    judge.client = type("C", (), {"models": fm})()
    usage = {"pt_date": "x", "video_calls": 0, "youtube_minutes": 0}
    pipeline.process_youtube(run, pipeline.load_rows(run), FakeYT(), judge, usage)
    rows = {r.item_id: r for r in pipeline.load_rows(run)}
    short = rows["yt:3kRB2TXewus"]
    props = run.notion.pages[short.id]["properties"]
    assert short.pipeline == "Ready"
    assert props["Judged From"]["select"]["name"] == "Full video"          # still watched, by Flash-Lite
    assert "overloaded" in props["Why"]["rich_text"][0]["plain_text"]
    assert usage["_overloaded_this_run"] and usage["video_calls"] == 1   # Flash tried once, then skipped


def test_busy_defers_then_metadata():
    from google.genai import errors as ge
    run = pipeline.Run(CFG, dry=False)
    run.notion, run.ds_id = FakeNotion(), "ds"
    rows = []
    pipeline.ingest_youtube(run, rows, FakeYT(), "PL")
    judge = Judge("fake", CFG, build_system_prompt(CFG, [], []), retries=0)
    fm = FakeModels()
    real = fm.generate_content

    def gen(model, contents, config):
        if not isinstance(contents, str):     # every video request is "busy"; metadata requests work
            raise ge.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
        return real(model, contents, config)
    fm.generate_content = gen
    judge.client = type("C", (), {"models": fm})()
    for attempt in range(1, 4):
        usage = {"pt_date": "x", "video_calls": 0, "youtube_minutes": 0}
        pend = [r for r in pipeline.load_rows(run) if r.pipeline in pipeline.PENDING]
        pipeline.process_youtube(run, pend, FakeYT(), judge, usage)
        rows = {r.item_id: r for r in pipeline.load_rows(run)}
        if attempt < 3:
            assert rows["yt:3kRB2TXewus"].pipeline == "Skipped-by-limit"
            assert f"try {attempt}" in rows["yt:3kRB2TXewus"].error
    assert rows["yt:3kRB2TXewus"].pipeline == "Ready"
    props = run.notion.pages[rows["yt:3kRB2TXewus"].id]["properties"]
    assert props["Judged From"]["select"]["name"] == "Metadata only"
