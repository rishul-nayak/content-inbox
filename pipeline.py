"""Content Inbox main job: ingest -> normalise/dedupe -> enrich -> judge -> write -> plan day -> housekeeping.

Usage:
  python pipeline.py                 # normal run
  python pipeline.py --dry-run       # read everything, judge, but write nothing to Notion
  python pipeline.py --plan          # force planning today's checklist (normally only morning runs plan)
  python pipeline.py --test-url URL  # judge one or more links and print the result (no Notion needed)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass, field

import scoring
import urls
from common import (APIError, env, iso_now, setting, load_config, load_dotenv, load_state, log, now_local, pt_date,
                    redact, save_state, today_local)
from judge import Judge, JudgeError, Overloaded, QuotaExhausted, build_system_prompt
from notion_client import Notion, P, bullet, divider, heading, para, prop, quote, todo
from planner import plan_day
from sources.x import X, XFetchError
from sources.youtube import YouTube

PENDING = {"New", "Skipped-by-limit", None}


# ---------------------------------------------------------------- rows
@dataclass
class Row:
    id: str
    name: str = ""
    url: str | None = None
    platform: str | None = None
    item_id: str | None = None
    pipeline: str | None = None
    saved_on: str | None = None
    done: bool = False
    done_on: str | None = None
    verdict: str | None = None
    priority: str | None = None
    quality: float | None = None
    relevance: float | None = None
    actionability: float | None = None
    value_type: str | None = None
    time: float | None = None
    planned_for: str | None = None
    my_rating: str | None = None
    takeaway: str | None = None
    author: str | None = None
    error: str | None = None
    domains: list = field(default_factory=list)
    created: str = ""
    edited: str = ""

    @classmethod
    def from_page(cls, pg: dict) -> "Row":
        return cls(
            id=pg["id"], name=prop(pg, "Name") or "", url=prop(pg, "URL"), platform=prop(pg, "Platform"),
            item_id=prop(pg, "Item ID") or None, pipeline=prop(pg, "Pipeline"), saved_on=prop(pg, "Saved On"),
            done=bool(prop(pg, "Done")), done_on=prop(pg, "Done On"), verdict=prop(pg, "Verdict"),
            priority=prop(pg, "Priority"), quality=prop(pg, "Quality"), relevance=prop(pg, "Relevance"),
            actionability=prop(pg, "Actionability"), value_type=prop(pg, "Value Type"), time=prop(pg, "Time (min)"),
            planned_for=prop(pg, "Planned For"), my_rating=prop(pg, "My Rating"), takeaway=prop(pg, "My Takeaway"),
            author=prop(pg, "Author"), error=prop(pg, "Error"), domains=prop(pg, "Domain") or [],
            created=pg.get("created_time", ""), edited=pg.get("last_edited_time", ""),
        )


class Run:
    def __init__(self, cfg: dict, dry: bool):
        self.cfg, self.dry = cfg, dry
        self.counts = {"ingested": 0, "duplicates": 0, "judged": 0, "errors": 0, "deferred": 0, "planned": 0, "done_on": 0}
        self.errors: list[str] = []
        self.notion: Notion | None = None
        self.ds_id: str | None = None
        self.db_id: str | None = None

    def write(self, page_id: str, props: dict | None = None, children: list | None = None,
              trash: bool | None = None, label: str = "") -> None:
        if self.dry:
            shown = {k: v for k, v in (props or {}).items()}
            log(f"[dry-run] would update {label or page_id}: " + json.dumps(shown, ensure_ascii=False)[:1500])
            if children:
                log(f"[dry-run]   + {len(children)} body blocks")
            if trash:
                log("[dry-run]   + move to trash")
            return
        if props or trash is not None:
            self.notion.update_page(page_id, props, in_trash=trash)
        if children:
            for old in self.notion.list_children(page_id):   # re-judged item: replace the old write-up
                if old.get("type") != "child_database":
                    self.notion.delete_block(old["id"])
            self.notion.append_children(page_id, children)

    def fail(self, row: Row, msg: str) -> None:
        msg = redact(msg)[:1800]
        self.counts["errors"] += 1
        self.errors.append(f"{row.item_id or row.id}: {msg[:200]}")
        log(f"  ERROR {row.item_id or row.id}: {msg[:300]}")
        self.write(row.id, {"Pipeline": P.select("Error"), "Error": P.text(msg)}, label=row.item_id or row.id)


# ---------------------------------------------------------------- judgement -> Notion
def result_props(j: dict, res: scoring.Result, extra: dict, model_note: str = "") -> dict:
    why = " ".join(x for x in [j["why"], res.note, model_note] if x)
    props = {
        "Verdict": P.select(scoring.VERDICT_LABEL[res.verdict]),
        "Priority": P.select(res.priority),
        "Quality": P.number(res.quality),
        "Timeless": P.number(j["timeless"]),
        "Substance": P.number(j["substance"]),
        "Credibility": P.number(j["credibility"]),
        "Relevance": P.number(j["relevance"]),
        "Actionability": P.number(j["actionability"]),
        "Domain": P.multi(j["domains"]),
        "Value Type": P.select(j["value_type"]),
        "Summary": P.text(j["summary"]),
        "Why": P.text(why),
        "Red Flags": P.multi(j["red_flags"]),
        "Action Item": P.text(j["action_item"]),
        "Judged From": P.select(j["judged_from"]),
        "Pipeline": P.select("Ready"),
        "Error": P.text(""),
    }
    props.update(extra)
    return props


def body_blocks(j: dict, res: scoring.Result, model: str) -> list:
    b = [heading("Key takeaways")] + [bullet(t) for t in j["takeaways"][:5]]
    if j["evidence"]:
        b += [heading("Evidence")] + [quote(e) for e in j["evidence"][:3]]
    if j["action_item"]:
        b += [heading("Action item"), todo(j["action_item"])]
    b += [heading("Verdict"),
          para(f"{scoring.VERDICT_LABEL[res.verdict]} · {res.priority} · Quality {res.quality}/10 · score {res.score}"),
          para(f"Timeless {j['timeless']} · Substance {j['substance']} · Credibility {j['credibility']} · "
               f"Relevance {j['relevance']} · Actionability {j['actionability']}"),
          para(" ".join(x for x in [j["why"], res.note] if x)),
          para(f"Red flags: {', '.join(j['red_flags']) or 'none'} · Judged from: {j['judged_from']} · Model: {model}"),
          divider()]
    return b


# ---------------------------------------------------------------- steps
def load_rows(run: Run) -> list[Row]:
    pages = run.notion.query(run.ds_id, sorts=[{"timestamp": "created_time", "direction": "ascending"}])
    return [Row.from_page(p) for p in pages]


def ingest_youtube(run: Run, rows: list[Row], yt: YouTube, playlist_id: str) -> None:
    known = {r.item_id for r in rows if r.item_id}
    items = yt.playlist_items(playlist_id)
    log(f"YouTube playlist: {len(items)} items")
    for it in items:
        iid = f"yt:{it['video_id']}"
        if iid in known:
            continue
        known.add(iid)
        url = f"https://www.youtube.com/watch?v={it['video_id']}"
        props = {
            "Name": P.title(it["title"] or url), "URL": P.url(url), "Platform": P.select("YouTube"),
            "Pipeline": P.select("New"), "Saved On": P.date((it["added_at"] or iso_now())[:10]), "Item ID": P.text(iid),
        }
        run.counts["ingested"] += 1
        if run.dry:
            log(f"[dry-run] would create row {iid}")
            rows.append(Row(id=f"dry-{iid}", name=it["title"], url=url, platform="YouTube", item_id=iid,
                            pipeline="New", saved_on=(it["added_at"] or "")[:10], created=iso_now()))
        else:
            pg = run.notion.create_page(run.ds_id, props)
            rows.append(Row.from_page(pg))
            log(f"  + {iid}")


def normalise_and_dedupe(run: Run, rows: list[Row]) -> list[Row]:
    """Give every row an Item ID + canonical URL; trash newer duplicates that are still unprocessed."""
    for r in rows:
        if r.item_id or r.pipeline not in PENDING:
            continue
        raw = r.url or urls.extract_first_url(r.name) or (r.name if "." in (r.name or "") else None)
        if not raw:
            run.fail(r, "No link found in URL or Name.")
            r.pipeline = "Error"
            continue
        p = urls.parse(raw)
        r.item_id, r.url, r.platform = p.item_id, p.url, p.platform
        props = {"Item ID": P.text(p.item_id), "URL": P.url(p.url), "Platform": P.select(p.platform)}
        if not r.saved_on:
            r.saved_on = (r.created or iso_now())[:10]
            props["Saved On"] = P.date(r.saved_on)
        run.write(r.id, props, label=p.item_id)

    groups: dict[str, list[Row]] = {}
    for r in rows:
        if r.item_id:
            groups.setdefault(r.item_id, []).append(r)
    trashed = set()
    for iid, rs in groups.items():
        if len(rs) < 2:
            continue
        rs.sort(key=lambda r: r.created)
        for dup in rs[1:]:
            if dup.pipeline in PENDING or dup.pipeline == "Error":
                run.counts["duplicates"] += 1
                log(f"  duplicate {iid}: trashing newer row")
                run.write(dup.id, trash=True, label=f"dup {iid}")
                trashed.add(dup.id)
    return [r for r in rows if r.id not in trashed]


def process_youtube(run: Run, pending: list[Row], yt: YouTube, judge: Judge, usage: dict) -> None:
    g, lim = run.cfg["gemini"], run.cfg["limits"]
    pending = sorted(pending, key=lambda r: r.saved_on or "")
    batch, deferred = pending[:lim["max_videos_per_run"]], pending[lim["max_videos_per_run"]:]
    for r in deferred:
        mark_deferred(run, r, "per-run video limit")
    if not batch:
        return
    try:
        metas = yt.videos([r.item_id[3:] for r in batch])
    except APIError as e:
        for r in batch:
            run.fail(r, f"YouTube API: {e}")
        return

    for r in batch:
        vid = r.item_id[3:]
        meta = metas.get(vid)
        try:
            if not meta:
                run.fail(r, "Video not found: private, deleted, or not public.")
                continue
            if meta["live"] in ("live", "upcoming"):
                mark_deferred(run, r, "live/upcoming stream; will judge after it ends")
                continue
            dur_min = meta["duration_sec"] / 60
            is_short = meta["duration_sec"] <= 180 and (urls.parse(r.url or "").is_short or yt.is_short(vid))
            fmt = "Short" if is_short else "Video"

            long_video = dur_min > g["max_full_video_min"]
            mode = "static" if not long_video else ("agentic" if dur_min <= g.get("max_agentic_video_min", 240) else "metadata")
            if mode != "metadata" and usage["youtube_minutes"] + dur_min > g["youtube_minutes_daily_cap"]:
                mark_deferred(run, r, "daily free YouTube-minutes limit reached")
                continue
            flash_ok = (usage["video_calls"] < g["video_model_daily_cap"] and not usage.get("video_model_exhausted")
                        and not usage.get("_overloaded_this_run"))
            model = g["video_model"] if (flash_ok and mode == "static") else g["fallback_model"]
            note = "" if model == g["video_model"] else f"(Judged by {model}.)"
            busy_tries = busy_count(r.error)

            j = None
            while j is None:
                calls_before = judge.video_calls
                try:
                    j = judge.judge_video(r.item_id, r.url, meta, model, mode)
                    if mode != "metadata":
                        usage["youtube_minutes"] += round(dur_min, 1)
                except QuotaExhausted:
                    if model == g["video_model"]:
                        log(f"  {model} daily quota used up -> falling back to {g['fallback_model']}")
                        usage["video_model_exhausted"] = True
                        model, note = g["fallback_model"], f"(Judged by {g['fallback_model']}: main model out of quota.)"
                        continue
                    raise
                except Overloaded:
                    if model == g["video_model"]:
                        log(f"  {model} overloaded -> watching with {g['fallback_model']} instead (for the rest of this run)")
                        usage["_overloaded_this_run"] = True
                        model, note = g["fallback_model"], f"(Judged by {g['fallback_model']}: main model overloaded.)"
                        continue
                    if mode != "metadata" and busy_tries + 1 < g.get("busy_retries", 3):
                        raise Busy(busy_tries + 1)
                    log("  still busy after several runs -> metadata only")
                    mode, note = "metadata", "(Gemini was busy on several runs; judged from metadata.)"
                except JudgeError as e:
                    if mode != "metadata":
                        log(f"  video judge failed ({str(e)[:120]}) -> metadata only")
                        mode, model = "metadata", g["fallback_model"]
                        note = "(Video could not be processed; judged from metadata.)"
                        continue
                    raise
                finally:
                    usage["video_calls"] += judge.video_calls - calls_before

            res = scoring.compute(j, run.cfg)
            extra = {"Name": P.title(meta["title"] or r.name), "Author": P.text(meta["channel"]),
                     "Format": P.select(fmt), "Time (min)": P.number(max(1, round(dur_min))),
                     "Platform": P.select("YouTube")}
            run.write(r.id, result_props(j, res, extra, note), body_blocks(j, res, model), label=r.item_id)
            run.counts["judged"] += 1
            log(f"  judged {r.item_id}: {res.verdict} {res.priority} q={res.quality} ({model}, {j['judged_from']})")
        except Busy as b:
            mark_deferred(run, r, f"Gemini busy (try {b.tries})", force=True)
        except QuotaExhausted as e:
            mark_deferred(run, r, f"Gemini quota: {str(e)[:100]}")
        except Exception as e:  # one bad item never stops the run
            run.fail(r, f"{type(e).__name__}: {e}")


def process_x(run: Run, pending: list[Row], xapi: X, judge: Judge) -> None:
    g, lim = run.cfg["gemini"], run.cfg["limits"]
    size = g["x_batch_size"]
    max_items = size * lim["max_x_batches_per_run"]
    pending = sorted(pending, key=lambda r: r.saved_on or "")
    for r in pending[max_items:]:
        mark_deferred(run, r, "per-run X limit")
    fetched = []
    for r in pending[:max_items]:
        try:
            fetched.append({"row": r, "item_id": r.item_id, "data": xapi.fetch(r.item_id[2:], run.cfg["reading_wpm"])})
        except XFetchError as e:
            run.fail(r, str(e))
        except Exception as e:
            run.fail(r, f"FxTwitter: {type(e).__name__}: {e}")

    for i in range(0, len(fetched), size):
        chunk = fetched[i:i + size]
        try:
            results = judge.judge_x_batch(chunk)
        except QuotaExhausted as e:
            for p in chunk:
                mark_deferred(run, p["row"], f"Gemini quota: {str(e)[:100]}")
            continue
        except Exception as e:
            for p in chunk:
                run.fail(p["row"], f"Judge: {type(e).__name__}: {e}")
            continue
        for p in chunk:
            r, d = p["row"], p["data"]
            j = results.get(p["item_id"])
            if not j:
                mark_deferred(run, r, "judge skipped this post; retry next run")
                continue
            try:
                res = scoring.compute(j, run.cfg)
                note = "(Only part of the thread was available.)" if d.get("partial_thread") else ""
                extra = {"Name": P.title(d["title"]), "Author": P.text(f"@{d['handle']}"),
                         "Format": P.select(d["format"]), "Time (min)": P.number(d["minutes"]),
                         "Platform": P.select("X")}
                run.write(r.id, result_props(j, res, extra, note), body_blocks(j, res, run.cfg["gemini"]["text_model"]),
                          label=r.item_id)
                run.counts["judged"] += 1
                log(f"  judged {r.item_id}: {res.verdict} {res.priority} q={res.quality}")
            except Exception as e:
                run.fail(r, f"{type(e).__name__}: {e}")


def process_other(run: Run, pending: list[Row]) -> None:
    for r in pending:
        run.write(r.id, {"Pipeline": P.select("Ready"), "Format": P.select("Article"), "Priority": P.select("P3"),
                         "Why": P.text("Not judged: only YouTube and X links are judged automatically.")},
                  label=r.item_id)


class Busy(Exception):
    def __init__(self, tries: int):
        super().__init__(f"busy try {tries}")
        self.tries = tries


def busy_count(error_text: str | None) -> int:
    m = re.search(r"Gemini busy \(try (\d+)\)", error_text or "")
    return int(m.group(1)) if m else 0


def mark_deferred(run: Run, r: Row, reason: str, force: bool = False) -> None:
    run.counts["deferred"] += 1
    log(f"  deferred {r.item_id}: {reason}")
    if r.pipeline != "Skipped-by-limit" or force:
        run.write(r.id, {"Pipeline": P.select("Skipped-by-limit"), "Error": P.text(f"Waiting: {reason}")}, label=r.item_id)


def housekeeping_done(run: Run, rows: list[Row], today: str) -> None:
    for r in rows:
        if r.done and not r.done_on:
            r.done_on = today
            run.counts["done_on"] += 1
            run.write(r.id, {"Done On": P.date(today)}, label=f"done {r.item_id}")


def plan_today(run: Run, rows: list[Row], today: str) -> None:
    cfg = run.cfg
    for r in rows:
        if not r.done and r.planned_for and r.planned_for < today:
            run.write(r.id, {"Planned For": P.date(None)}, label=f"unplan {r.item_id}")
            r.planned_for = None

    def as_item(r: Row) -> dict:
        return {"id": r.id, "priority": r.priority, "time": r.time, "value_type": r.value_type,
                "score": scoring.score_from_props(r.quality, r.relevance, r.actionability, cfg)}

    already = [as_item(r) for r in rows if not r.done and r.planned_for == today]
    cands = [as_item(r) for r in rows if not r.done and r.pipeline == "Ready" and not r.planned_for
             and scoring.plain_verdict(r.verdict) != "Skip"]
    weekday = now_local(cfg["timezone"]).strftime("%a")
    budget = cfg["planner"]["budget_min"].get(weekday, 60)
    chosen = plan_day(cands, already, budget, cfg, weekday)
    for pid in chosen:
        run.write(pid, {"Planned For": P.date(today)}, label=f"plan {pid}")
    run.counts["planned"] = len(chosen)
    log(f"Planned {len(chosen)} new items for {today} (budget {budget} min, {len(already)} already planned)")



def calibration_examples(rows: list[Row], n: int) -> list[dict]:
    out = []
    for r in sorted(rows, key=lambda r: r.edited, reverse=True):
        judge_v, mine = scoring.plain_verdict(r.verdict), scoring.plain_verdict(r.my_rating)
        if judge_v and mine and judge_v != mine:
            out.append({"title": r.name, "author": r.author, "judge": judge_v, "mine": mine, "takeaway": r.takeaway})
        if len(out) >= n:
            break
    return out


def existing_domains(rows: list[Row]) -> list[str]:
    freq: dict[str, int] = {}
    for r in rows:
        for d in r.domains:
            freq[d] = freq.get(d, 0) + 1
    return [d for d, _ in sorted(freq.items(), key=lambda kv: -kv[1])]


# ---------------------------------------------------------------- test mode
def test_urls(cfg: dict, links: list[str]) -> None:
    judge = Judge(env("GEMINI_API_KEY"), cfg, build_system_prompt(cfg, [], []), cfg["limits"]["max_retries"])
    try:
        _test_urls(cfg, links, judge)
    finally:  # test calls count toward the same daily free quota
        state = load_state()
        usage = state.get("usage", {})
        if usage.get("pt_date") != pt_date():
            usage = {"pt_date": pt_date(), "video_calls": 0, "youtube_minutes": 0, "video_model_exhausted": False}
        usage["video_calls"] = usage.get("video_calls", 0) + judge.video_calls
        state["usage"] = usage
        save_state(state)
        log(f"Video-model calls this test: {judge.video_calls} (today: {usage['video_calls']})")


def _test_urls(cfg: dict, links: list[str], judge: Judge) -> None:
    for link in links:
        p = urls.parse(urls.extract_first_url(link) or link)
        log(f"\n=== {p.item_id} ({p.platform}) {p.url}")
        try:
            if p.platform == "YouTube":
                yt = YouTube(env("YOUTUBE_API_KEY"))
                meta = yt.videos([p.item_id[3:]]).get(p.item_id[3:])
                if not meta:
                    log("Video not found / not public")
                    continue
                g = cfg["gemini"]
                dur_min = meta["duration_sec"] / 60
                mode = "static" if dur_min <= g["max_full_video_min"] else ("agentic" if dur_min <= g.get("max_agentic_video_min", 240) else "metadata")
                model = g["video_model"] if mode == "static" else g["fallback_model"]
                log(f"Title: {meta['title']} | {meta['channel']} | {round(dur_min,1)} min | mode {mode} | model {model}")
                try:
                    j = judge.judge_video(p.item_id, p.url, meta, model, mode)
                except (Overloaded, QuotaExhausted) as e:
                    log(f"{model} unavailable ({type(e).__name__}) -> {g['fallback_model']}")
                    model = g["fallback_model"]
                    j = judge.judge_video(p.item_id, p.url, meta, model, mode)
                log(f"Judged by {model}")
            elif p.platform == "X":
                d = X().fetch(p.item_id[2:], cfg["reading_wpm"])
                log(f"Post: {d['title']} | format {d['format']} | {d['minutes']} min | thread posts {d['thread_posts']}")
                j = judge.judge_x_batch([{"item_id": p.item_id, "data": d}]).get(p.item_id)
            else:
                log("Only YouTube and X links are judged.")
                continue
            res = scoring.compute(j, cfg)
            log(json.dumps(j, indent=2, ensure_ascii=False))
            log(f"--> Quality {res.quality}/10 · score {res.score} · {scoring.VERDICT_LABEL[res.verdict]} · {res.priority} {res.note}")
        except Exception as e:
            log(f"FAILED: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--plan", action="store_true", help="plan today's checklist even if it's not the morning run")
    ap.add_argument("--no-plan", action="store_true")
    ap.add_argument("--test-url", action="append", help="judge this link and print the result; no Notion")
    args = ap.parse_args()

    load_dotenv()
    cfg = load_config()
    if args.test_url:
        test_urls(cfg, args.test_url)
        return 0

    run = Run(cfg, args.dry_run)
    tz = cfg["timezone"]
    today = today_local(tz)
    retries = cfg["limits"]["max_retries"]
    run.notion = Notion(env("NOTION_TOKEN"), retries)
    run.db_id = setting("NOTION_DATABASE_ID", cfg)
    run.ds_id = run.notion.data_source_id(run.db_id)

    state = load_state()
    usage = state.get("usage", {})
    if usage.get("pt_date") != pt_date():
        usage = {"pt_date": pt_date(), "video_calls": 0, "youtube_minutes": 0, "video_model_exhausted": False}

    log(f"Run {'(DRY) ' if run.dry else ''}{iso_now()} — IST date {today}")
    rows = load_rows(run)
    log(f"Notion rows: {len(rows)}")

    # 1. Ingest
    playlist = (os.environ.get("YT_PLAYLIST_ID", "").strip() or str(cfg.get("ids", {}).get("YT_PLAYLIST_ID") or "").strip())
    try:
        if playlist:
            ingest_youtube(run, rows, YouTube(env("YOUTUBE_API_KEY"), retries), playlist)
        else:
            log("No YT_PLAYLIST_ID set in config.yaml: skipping YouTube playlist")
    except Exception as e:
        run.errors.append(f"YouTube ingest: {redact(e)}")
        log(f"YouTube ingest failed: {redact(e)}")
    rows = normalise_and_dedupe(run, rows)

    # 2-5. Enrich + judge + write
    pending = [r for r in rows if r.pipeline in PENDING and r.item_id]
    yt_p = [r for r in pending if r.platform == "YouTube"]
    x_p = [r for r in pending if r.platform == "X"]
    other = [r for r in pending if r.platform not in ("YouTube", "X")]
    log(f"Pending: {len(yt_p)} YouTube, {len(x_p)} X, {len(other)} other")
    if yt_p or x_p:
        system = build_system_prompt(cfg, calibration_examples(rows, cfg["calibration_examples"]), existing_domains(rows))
        judge = Judge(env("GEMINI_API_KEY"), cfg, system, retries)
        if yt_p:
            process_youtube(run, yt_p, YouTube(env("YOUTUBE_API_KEY"), retries), judge, usage)
        if x_p:
            process_x(run, x_p, X(retries), judge)
    process_other(run, other)

    # 7. Housekeeping (before planning so ticked items don't get re-planned)
    housekeeping_done(run, rows, today)

    # 6. Plan the day
    if not run.dry:
        rows = load_rows(run)   # fresh verdicts/priorities after judging
    morning = now_local(tz).hour < cfg["planner"]["plan_before_hour"]
    if (morning or args.plan) and not args.no_plan:
        plan_today(run, rows, today)

    summary = {"finished_at": iso_now(), "dry_run": run.dry, "counts": run.counts, "errors": run.errors[:50]}
    log("Summary: " + json.dumps(summary["counts"]))
    if not run.dry:
        usage.pop("_overloaded_this_run", None)
        state.update({"last_run": summary, "usage": usage})
        save_state(state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(redact("FATAL: " + "".join(traceback.format_exception_only(type(e), e))), flush=True)
        sys.exit(1)
