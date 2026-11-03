import json

import pytest

import scoring
import urls
from common import load_config
from judge import build_system_prompt, parse_batch, parse_single
from pipeline import Row, Run, normalise_and_dedupe, calibration_examples
from planner import plan_day
from sources import x as xsrc
from sources.youtube import chapters, iso_duration_seconds

CFG = load_config()


# ---------------- URL parsing ----------------
@pytest.mark.parametrize("raw,platform,item_id,url", [
    ("https://youtu.be/3kRB2TXewus?si=wbPAc_uq_ViC5DtK", "YouTube", "yt:3kRB2TXewus", "https://www.youtube.com/watch?v=3kRB2TXewus"),
    ("https://www.youtube.com/watch?v=3kRB2TXewus&t=42s&si=x", "YouTube", "yt:3kRB2TXewus", "https://www.youtube.com/watch?v=3kRB2TXewus"),
    ("https://m.youtube.com/watch?v=3kRB2TXewus", "YouTube", "yt:3kRB2TXewus", "https://www.youtube.com/watch?v=3kRB2TXewus"),
    ("https://youtube.com/shorts/abcdefghijk?feature=share", "YouTube", "yt:abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk"),
    ("https://www.youtube.com/live/abcdefghijk", "YouTube", "yt:abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk"),
    ("https://x.com/kyr0stack/status/2117251353168752764?a=ewq5C4qij7J6iMMIUrM&s=20", "X", "x:2117251353168752764", "https://x.com/kyr0stack/status/2117251353168752764"),
    ("https://twitter.com/jack/status/20", "X", "x:20", "https://x.com/jack/status/20"),
    ("https://mobile.twitter.com/jack/status/20/photo/1", "X", "x:20", "https://x.com/jack/status/20"),
    ("https://x.com/i/web/status/20", "X", "x:20", "https://x.com/i/status/20"),
    ("https://fxtwitter.com/jack/status/20", "X", "x:20", "https://x.com/jack/status/20"),
])
def test_parse(raw, platform, item_id, url):
    p = urls.parse(raw)
    assert (p.platform, p.item_id, p.url) == (platform, item_id, url)


def test_short_flag_and_other():
    assert urls.parse("https://youtube.com/shorts/abcdefghijk").is_short
    a = urls.parse("https://example.com/post/?utm_source=x&id=5&s=1")
    b = urls.parse("https://www.example.com/post?id=5")
    assert a.platform == "Other" and a.item_id == b.item_id and a.url == "https://example.com/post?id=5"


def test_extract_first_url():
    t = "Look at this 🧵 https://x.com/a/status/1?s=20, and https://youtu.be/xyz"
    assert urls.extract_first_url(t) == "https://x.com/a/status/1?s=20"
    assert urls.extract_first_url("no link here") is None
    assert urls.extract_first_url(None) is None


# ---------------- scoring ----------------
def J(t, s, c, r, a, flags=(), v=None):
    return {"timeless": t, "substance": s, "credibility": c, "relevance": r, "actionability": a,
            "red_flags": list(flags), "verdict_suggestion": v}


def test_quality_formula_and_penalty():
    assert scoring.compute(J(5, 5, 5, 5, 5), CFG).quality == 10.0
    assert scoring.compute(J(4, 4, 4, 0, 0), CFG).quality == 8.0
    # one serious flag (-1), one non-serious (Hype, 0)
    assert scoring.compute(J(4, 4, 4, 0, 0, ["No evidence", "Hype"]), CFG).quality == 7.0
    assert scoring.compute(J(0, 0, 1, 0, 0, ["No evidence", "Clickbait"]), CFG).quality == 0.0  # floor


def test_verdicts_and_priority():
    gem = scoring.compute(J(5, 5, 4, 5, 4), CFG)
    assert gem.verdict == "Gem" and gem.priority == "P1"
    # high quality but irrelevant -> Solid, not Gem
    assert scoring.compute(J(5, 5, 5, 2, 0), CFG).verdict == "Solid"
    assert scoring.compute(J(3, 3, 3, 3, 3), CFG).verdict == "Solid"     # q=6
    assert scoring.compute(J(2, 2, 2, 5, 5), CFG).verdict == "Skim"      # q=4
    skip = scoring.compute(J(1, 1, 1, 5, 5), CFG)
    assert skip.verdict == "Skip" and skip.priority == "P4"             # Skip is always P4


def test_score_and_priority_bands():
    r = scoring.compute(J(4, 4, 4, 4, 2), CFG)      # q=8 -> .36 + .28 + .08 = .72
    assert r.score == pytest.approx(0.72) and r.priority == "P2"
    assert scoring.score_from_props(8, 4, 2, CFG) == pytest.approx(0.72)


def test_llm_disagreement_note():
    assert "Judge suggested Gem" in scoring.compute(J(2, 2, 2, 5, 5, v="Gem"), CFG).note   # code says Skim
    assert scoring.compute(J(3, 3, 3, 3, 3, v="Gem"), CFG).note == ""                       # one level apart


def test_plain_verdict():
    assert scoring.plain_verdict("💎 Gem") == "Gem"
    assert scoring.plain_verdict("Skip") == "Skip"
    assert scoring.plain_verdict(None) is None


# ---------------- planner ----------------
def it(i, p="P2", s=0.7, t=10, v="Knowledge"):
    return {"id": i, "priority": p, "score": s, "time": t, "value_type": v}


def test_planner_budget_and_order():
    cands = [it("a", "P3", t=20), it("b", "P1", t=30), it("c", "P2", t=20), it("d", "P2", t=15)]
    chosen = plan_day(cands, [], 60, CFG)
    assert chosen[:2] == ["b", "c"] and sum(c["time"] for c in cands if c["id"] in chosen) <= 60


def test_planner_action_and_long_items():
    cands = [it("long1", "P1", t=50), it("long2", "P1", t=46), it("act", "P4", t=5, v="Action")]
    chosen = plan_day(cands, [], 120, CFG)
    assert chosen[0] == "act"                      # action item guaranteed
    assert "long1" in chosen and "long2" not in chosen   # max one long item


def test_planner_respects_already_planned():
    already = [it("x", t=55, v="Action")]
    chosen = plan_day([it("a", t=10), it("b", t=5)], already, 60, CFG)
    assert chosen == ["b"]


def test_planner_missing_time_defaults():
    assert plan_day([it("a", t=None)], [], 10, CFG) == ["a"]


# ---------------- dedupe ----------------
def test_normalise_and_dedupe_dry():
    run = Run(CFG, dry=True)
    rows = [
        Row(id="1", name="https://youtu.be/3kRB2TXewus", url="https://youtu.be/3kRB2TXewus?si=1", pipeline="Ready",
            item_id="yt:3kRB2TXewus", created="2026-11-01T00:00:00Z"),
        Row(id="2", name="https://youtu.be/3kRB2TXewus?si=2", url="https://youtu.be/3kRB2TXewus?si=2", pipeline="New",
            created="2026-11-02T00:00:00Z"),
        Row(id="3", name="hey https://x.com/a/status/9?s=20", url=None, pipeline="New", created="2026-11-02T00:00:00Z"),
        Row(id="4", name="no link", url=None, pipeline="New", created="2026-11-02T00:00:00Z"),
    ]
    kept = normalise_and_dedupe(run, rows)
    ids = {r.id for r in kept}
    assert "2" not in ids and "1" in ids                 # newer duplicate trashed, original kept
    x = next(r for r in kept if r.id == "3")
    assert x.item_id == "x:9" and x.platform == "X"
    assert next(r for r in kept if r.id == "4").pipeline == "Error"
    assert run.counts["duplicates"] == 1


def test_calibration_examples():
    rows = [Row(id="1", name="A", verdict="💎 Gem", my_rating="Skip", edited="2"),
            Row(id="2", name="B", verdict="✅ Solid", my_rating="Solid", edited="3"),
            Row(id="3", name="C", verdict="🗑 Skip", my_rating="Gem", edited="1")]
    ex = calibration_examples(rows, 10)
    assert [e["title"] for e in ex] == ["A", "C"]
    prompt = build_system_prompt(CFG, ex, ["AI Agents"])
    assert "judge said Gem, reader said Skip" in prompt and CFG["focus_areas"][0] in prompt and "AI Agents" in prompt
    assert not any(ph in prompt for ph in ("{focus_areas}", "{calibration}", "{existing_domains}"))


# ---------------- judge JSON parsing ----------------
def test_parse_single_clamps_and_caps():
    raw = json.dumps({"timeless": 9, "substance": 5, "credibility": 5, "relevance": "4", "actionability": -2,
                      "red_flags": ["Hype", "Made up"], "value_type": "Weird", "judged_from": "Metadata only",
                      "domains": [], "takeaways": ["a", " "], "evidence": [], "summary": "s", "why": "w", "action_item": None})
    j = parse_single(raw, "yt:1", "Full video")
    assert j["timeless"] == 5 and j["actionability"] == 0 and j["relevance"] == 4
    assert j["substance"] == 3 and j["credibility"] == 3        # metadata-only cap enforced in code
    assert j["red_flags"] == ["Hype"] and j["value_type"] == "Knowledge"
    assert j["domains"] == ["General"] and j["takeaways"] == ["a"] and j["action_item"] == ""
    assert j["item_id"] == "yt:1"


def test_parse_batch_matches_ids():
    raw = json.dumps({"items": [{"item_id": "x:2", "timeless": 3}, {"item_id": "x:999", "timeless": 5},
                                {"item_id": "x:1", "timeless": 4}]})
    out = parse_batch(raw, ["x:1", "x:2", "x:3"])
    assert set(out) == {"x:1", "x:2"} and out["x:1"]["timeless"] == 4 and out["x:2"]["judged_from"] == "Post text"
    meta = parse_batch(json.dumps({"items": [{"item_id": "x:1", "judged_from": "Metadata only", "substance": 5}]}), ["x:1"])
    assert meta["x:1"]["judged_from"] == "Post text" and meta["x:1"]["substance"] == 5   # no metadata cap for X


def test_parse_bad_json_raises():
    with pytest.raises(ValueError):
        parse_single("not json", "x:1", "Post text")


# ---------------- sources ----------------
def test_iso_duration_and_chapters():
    assert iso_duration_seconds("PT1H2M3S") == 3723
    assert iso_duration_seconds("PT45S") == 45
    assert iso_duration_seconds("P0D") == 0
    assert iso_duration_seconds("P1DT1M") == 86460
    desc = "Intro text\n00:00 Intro\n02:15 - Pricing\n1:02:03 Q&A\nnot a chapter"
    assert chapters(desc) == ["00:00 Intro", "02:15 Pricing", "1:02:03 Q&A"]
    assert chapters("0:00 only one") == []


def _post(i, text, ts, handle="kyr0stack", videos=None):
    return {"id": i, "type": "status", "text": text, "created_timestamp": ts, "likes": 10, "reposts": 2,
            "author": {"name": "K", "screen_name": handle, "followers": 1234},
            "media": {"videos": videos or []}}


def test_x_parse_thread():
    payload = {"code": 200, "status": _post("1", "How we grew 10x 🧵", 1),
               "thread": [_post("1", "How we grew 10x 🧵", 1), _post("2", "Step one " * 50, 2),
                          _post("3", "reply from someone else", 3, handle="other")]}
    d = xsrc.parse(payload, "1")
    assert d["format"] == "Thread" and d["thread_posts"] == 2 and "other" not in d["text"]
    assert d["handle"] == "kyr0stack" and d["title"].startswith("@kyr0stack:") and d["minutes"] >= 1


def test_x_parse_video_and_partial_thread():
    d = xsrc.parse({"code": 200, "status": _post("1", "watch", 1, videos=[{"type": "video", "duration": 125.0}]),
                    "thread": None}, "1")
    assert d["format"] == "Post + Video" and d["minutes"] == 3
    d2 = xsrc.parse({"code": 200, "status": _post("1", "A thread on pricing 1/", 1), "thread": None}, "1")
    assert d2["format"] == "Thread" and d2["partial_thread"]


def test_x_parse_errors():
    with pytest.raises(xsrc.XFetchError):
        xsrc.parse({"code": 404, "status": None}, "1")
    with pytest.raises(xsrc.XFetchError):
        xsrc.parse({"code": 200, "status": {"type": "tombstone", "reason": "deleted", "message": "gone"}}, "1")


def test_x_parse_article():
    st = _post("1", "", 1)
    st["article"] = {"title": "Pricing 101", "preview_text": "p", "content": {"blocks": [{"text": "Body one"}, {"text": "Body two"}]}}
    d = xsrc.parse({"code": 200, "status": st, "thread": None}, "1")
    assert d["format"] == "Article" and "Body two" in d["text"] and "Pricing 101" in d["title"]
