"""One-time: create the "Content Inbox" database (+ 9 views) under your shared Notion page and print its IDs.

Run it from GitHub: Actions -> "Setup Notion (one time)" -> Run workflow.
Safe to re-run: if NOTION_DATABASE_ID is already set, it only (re)creates missing views.
"""
from __future__ import annotations

import sys

from common import env, load_dotenv, log
from notion_client import Notion

SEL = lambda *names: {"select": {"options": [{"name": n} for n in names]}}  # noqa: E731
MULTI = lambda *names: {"multi_select": {"options": [{"name": n} for n in names]}}  # noqa: E731
NUM = {"number": {"format": "number"}}
TXT = {"rich_text": {}}
DATE = {"date": {}}

PROPERTIES = {
    "Name": {"title": {}},
    "URL": {"url": {}},
    "Platform": SEL("YouTube", "X", "Other"),
    "Format": SEL("Video", "Short", "Post", "Thread", "Post + Video", "Article"),
    "Author": TXT,
    "Saved On": DATE,
    "Pipeline": SEL("New", "Ready", "Error", "Skipped-by-limit"),
    "Done": {"checkbox": {}},
    "Done On": DATE,
    "Verdict": SEL("💎 Gem", "✅ Solid", "👀 Skim", "🗑 Skip"),
    "Priority": SEL("P1", "P2", "P3", "P4"),
    "Quality": NUM, "Timeless": NUM, "Substance": NUM, "Credibility": NUM, "Relevance": NUM, "Actionability": NUM,
    "Domain": MULTI(),
    "Value Type": SEL("Knowledge", "Action", "Tool/Resource", "Inspiration", "Entertainment"),
    "Time (min)": NUM,
    "Summary": TXT,
    "Why": TXT,
    "Red Flags": MULTI("Hype", "Clickbait", "Selling a course", "No evidence", "Survivorship bias", "Outdated",
                       "Engagement bait", "Recycled"),
    "Action Item": TXT,
    "Planned For": DATE,
    "My Rating": SEL("Gem", "Solid", "Skim", "Skip"),
    "My Takeaway": TXT,
    "Item ID": TXT,
    "Judged From": SEL("Full video", "Post text", "Metadata only"),
    "Error": TXT,
}

NOT_DONE = {"property": "Done", "checkbox": {"equals": False}}
PRIO_SORT = [{"property": "Priority", "direction": "ascending"}]


def views(today: str) -> list[tuple[str, dict | None, list | None]]:
    return [
        ("📅 Today", {"and": [{"property": "Planned For", "date": {"equals": today}}, NOT_DONE]},
         PRIO_SORT + [{"property": "Time (min)", "direction": "ascending"}]),
        ("📥 Inbox", {"and": [{"property": "Pipeline", "select": {"equals": "Ready"}}, NOT_DONE,
                             {"property": "Verdict", "select": {"does_not_equal": "🗑 Skip"}}]},
         PRIO_SORT + [{"property": "Saved On", "direction": "descending"}]),
        ("🗓 This Week", {"property": "Saved On", "date": {"past_week": {}}},
         [{"property": "Saved On", "direction": "descending"}]),
        ("⚡ Quick Wins", {"and": [{"property": "Time (min)", "number": {"less_than_or_equal_to": 10}}, NOT_DONE,
                                  {"or": [{"property": "Verdict", "select": {"equals": "💎 Gem"}},
                                          {"property": "Verdict", "select": {"equals": "✅ Solid"}}]}]}, PRIO_SORT),
        ("🛠 Act On These", {"and": [{"property": "Value Type", "select": {"equals": "Action"}}, NOT_DONE]}, PRIO_SORT),
        ("💎 Gems", {"property": "Verdict", "select": {"equals": "💎 Gem"}}, PRIO_SORT),
        ("✅ Done", {"property": "Done", "checkbox": {"equals": True}}, [{"property": "Done On", "direction": "descending"}]),
        ("🗑 Skipped", {"property": "Verdict", "select": {"equals": "🗑 Skip"}},
         [{"property": "Saved On", "direction": "descending"}]),
        ("⚠️ Errors", {"property": "Pipeline", "select": {"equals": "Error"}}, None),
    ]


def main() -> int:
    load_dotenv()
    from common import load_config, today_local
    cfg = load_config()
    n = Notion(env("NOTION_TOKEN"))
    db_id = env("NOTION_DATABASE_ID", required=False)

    if db_id:
        log(f"NOTION_DATABASE_ID is set; reusing database {db_id}")
        ds_id = n.data_source_id(db_id)
    else:
        db = n.create_database(env("NOTION_PARENT_PAGE_ID"), "Content Inbox", PROPERTIES)
        db_id = db["id"]
        ds_id = (db.get("data_sources") or [{}])[0].get("id") or n.data_source_id(db_id)
        log("Created database 'Content Inbox'.")

    existing = set()
    try:
        existing = {v.get("name") for v in n.list_views(db_id)}
    except Exception as e:
        log(f"(could not list views: {str(e)[:150]})")

    failed = []
    for name, flt, sorts in views(today_local(cfg["timezone"])):
        if name in existing:
            log(f"  view exists: {name}")
            continue
        try:
            n.create_view(db_id, ds_id, name, flt, sorts)
            log(f"  view created: {name}")
        except Exception as e:
            failed.append(name)
            log(f"  view FAILED: {name}: {str(e)[:200]}")

    log("\n================ COPY THESE ================")
    log(f"NOTION_DATABASE_ID   = {db_id}      <- add as a GitHub secret")
    log(f"NOTION_DATA_SOURCE_ID = {ds_id}     <- paste into the iPhone Shortcut")
    log("Notion API version  = 2026-03-11   <- use this in the Shortcut header")
    log("============================================")
    if failed:
        log(f"\n{len(failed)} views could not be created by the API. Make them by hand: see README, 'Views by hand'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
