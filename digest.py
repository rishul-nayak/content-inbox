"""Weekly summary page: 'Week of <date>' under your Content Inbox parent page."""
from __future__ import annotations

import argparse
import sys
from collections import Counter

import scoring
from common import days_ago, env, load_config, load_dotenv, log, today_local
from notion_client import Notion, bullet, heading, para
from pipeline import Row, load_rows, Run


def hrs(minutes: float) -> str:
    return f"{minutes / 60:.1f} h"


def t(r: Row) -> float:
    return r.time or 10


def build(rows: list[Row], cfg: dict) -> tuple[str, list]:
    tz = cfg["timezone"]
    today, week_ago = today_local(tz), days_ago(tz, 6)
    stale_cut = days_ago(tz, cfg["digest"]["stale_after_days"])
    score = lambda r: scoring.score_from_props(r.quality, r.relevance, r.actionability, cfg)  # noqa: E731
    verdict = lambda r: scoring.plain_verdict(r.verdict)  # noqa: E731

    saved = [r for r in rows if (r.saved_on or "")[:10] >= week_ago]
    finished = [r for r in rows if r.done and (r.done_on or "") >= week_ago]
    backlog = [r for r in rows if not r.done and r.pipeline == "Ready" and verdict(r) != "Skip"]
    backlog_min = sum(t(r) for r in backlog)
    added_min = sum(t(r) for r in saved if verdict(r) != "Skip")
    backlog_prev = backlog_min - added_min + sum(t(r) for r in finished)
    delta = backlog_min - backlog_prev

    b = [heading("📊 This week"),
         bullet(f"Saved: {len(saved)} items ({hrs(sum(t(r) for r in saved))})"),
         bullet(f"Finished: {len(finished)} items ({hrs(sum(t(r) for r in finished))})"),
         bullet(f"Backlog: {len(backlog)} items, {hrs(backlog_min)} — "
                + (f"grew by {hrs(delta)} ⚠️" if delta > 0 else f"shrank by {hrs(-delta)} ✅"))]
    if saved and len(finished) < len(saved) / 2:
        b.append(para("You saved more than twice what you finished. Save less, or drop more."))

    vc = Counter(verdict(r) or "Unjudged" for r in saved)
    b += [heading("⚖️ Verdicts (saved this week)"),
          para(" · ".join(f"{scoring.VERDICT_LABEL.get(k, k)}: {vc.get(k, 0)}" for k in ["Gem", "Solid", "Skim", "Skip"])
               + (f" · Unjudged: {vc['Unjudged']}" if vc.get("Unjudged") else ""))]

    gems = sorted([r for r in rows if not r.done and verdict(r) == "Gem"], key=score, reverse=True)[:5]
    b.append(heading("💎 Gems you are still skipping"))
    b += [bullet(f"{r.name[:120]} — {int(t(r))} min · saved {r.saved_on or '?'} · {r.url or ''}") for r in gems] or [para("None. Good.")]

    stale = sorted([r for r in backlog if (r.saved_on or today) < stale_cut], key=score)[:10]
    b.append(heading(f"🧹 Undone for {cfg['digest']['stale_after_days']}+ days — consider dropping"))
    b += [bullet(f"{r.name[:120]} ({scoring.VERDICT_LABEL.get(verdict(r) or '', '?')}, {r.priority or '?'}, "
                 f"{int(t(r))} min, saved {r.saved_on})") for r in stale] or [para("Nothing stale.")]

    month = days_ago(tz, 29)
    sd = Counter(d for r in rows if (r.saved_on or "") >= month for d in r.domains)
    fd = Counter(d for r in rows if r.done and (r.done_on or "") >= month for d in r.domains)
    b += [heading("🧭 Domains (last 30 days)"),
          bullet("Saved most: " + (", ".join(f"{d} ({c})" for d, c in sd.most_common(5)) or "—")),
          bullet("Finished most: " + (", ".join(f"{d} ({c})" for d, c in fd.most_common(5)) or "—"))]

    rated = [r for r in rows if r.my_rating and verdict(r)]
    agree = sum(1 for r in rated if scoring.plain_verdict(r.my_rating) == verdict(r))
    order = scoring.VERDICTS
    higher = sum(1 for r in rated if order.index(verdict(r)) > order.index(scoring.plain_verdict(r.my_rating)))
    lower = len(rated) - agree - higher
    b.append(heading("🎯 Judge calibration (all time)"))
    if rated:
        b += [bullet(f"You rated {len(rated)} items. Agreement: {agree}/{len(rated)} ({100 * agree // len(rated)}%)"),
              bullet(f"Judge too generous: {higher} · Judge too harsh: {lower}")]
    else:
        b.append(para("No 'My Rating' overrides yet. Rate items you disagree with; the judge learns from them."))

    tk = [r for r in finished if r.takeaway]
    b.append(heading("📝 Your takeaways this week"))
    b += [bullet(f"{r.name[:80]}: {r.takeaway}") for r in tk] or [para("None written. One line after each item makes it stick.")]
    return f"Week of {week_ago}", b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_dotenv()
    cfg = load_config()
    run = Run(cfg, args.dry_run)
    run.notion = Notion(env("NOTION_TOKEN"))
    run.ds_id = run.notion.data_source_id(env("NOTION_DATABASE_ID"))
    rows = load_rows(run)
    title, blocks = build(rows, cfg)
    if args.dry_run:
        log(f"[dry-run] would create page '{title}' with {len(blocks)} blocks")
        for blk in blocks:
            typ = blk["type"]
            log("   ", typ, "".join(x["text"]["content"] for x in blk[typ].get("rich_text", [])))
        return 0
    page = run.notion.create_child_page(env("NOTION_PARENT_PAGE_ID"), title, blocks)
    log(f"Created digest page: {page.get('url')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
