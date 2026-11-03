# Content Inbox: owner's manual

Everything needed to use, change, fix and rebuild Content Inbox. The overview is in the [README](../README.md).

## How you use it (daily)

| You want to… | Do this |
|---|---|
| Save an X post | Share → **Share via…** → **Save to Inbox** (top of the list). Or: Share → **Copy Link**, then press the **Action Button**. You get "Saved ✓". |
| Save a YouTube video | **Save** → **Inbox** playlist. (Shorts: **⋯ → Save to playlist → Inbox**.) |
| See today's list | Notion → Content Inbox → **📅 Today**. It is planned at 07:00 IST. |
| Finish something | Tick **Done**. Optional: write one line in **My Takeaway**. |
| Disagree with the judge | Set **My Rating** (Gem / Solid / Skim / Skip). Only when you disagree. The judge reads your last 10 disagreements before each run. |
| Check what got binned | **🗑 Skipped** view. If the judge was wrong, set My Rating. |
| Get a fixed item re-judged | Set **Pipeline** back to **New**. The next run picks it up. |

Views: 📅 Today · 📥 Inbox · 🗓 This Week · ⚡ Quick Wins (≤ 10 min) · 🛠 Act On These · 💎 Gems · ✅ Done · 🗑 Skipped · ⚠️ Errors.

### How the judge scores (summary; full rules in the README)
Gemini gives 0–5 on **Timeless, Substance, Credibility, Relevance, Actionability** and lists red flags. The **code** (not the AI) then calculates:
- `Quality = 2 × mean(Timeless, Substance, Credibility)` − 1 per serious red flag
- `score = 0.45·Quality/10 + 0.35·Relevance/5 + 0.20·Actionability/5`
- Gem = Quality ≥ 8 and Relevance ≥ 3 · Solid ≥ 6 · Skim ≥ 4 · else Skip
- P1 ≥ 0.80 · P2 ≥ 0.65 · P3 ≥ 0.45 · else P4 (Skip is always P4)

All thresholds are in `config.yaml`. The rubric text is in `rubric.md`.

---

## Change things

Edit on github.com (open the file → ✏️ pencil → **Commit changes**). The next run uses the change.

| Change | File → key |
|---|---|
| Focus areas (drives Relevance) | `config.yaml` → `focus_areas` |
| Daily time budget | `config.yaml` → `planner.budget_min` (per weekday) |
| Strictness of the judge | `rubric.md` |
| Gem/Solid/Skim cut-offs, priorities | `config.yaml` → `scoring` |
| Run times | `.github/workflows/pipeline.yml` → `cron` (times are UTC; IST − 5:30) |

---

## If GitHub emails you "Run failed"

1. GitHub → repo → **Actions** → click the red run → open the **Run pipeline** step. The last lines tell you what broke.
2. Common causes:
   - `Missing environment variable / GitHub secret: X` → add that secret (Settings → Secrets and variables → Actions).
   - `HTTP 404 … object_not_found` from Notion → the integration is not connected to **Life → Content Inbox** (••• → Connections).
   - `HTTP 401` from Notion → token is wrong, or the page is not connected to the integration (page **••• → Connections**).
   - `HTTP 404` from Notion → wrong `NOTION_DATABASE_ID`.
   - `HTTP 403` from YouTube → API key restricted to the wrong API, or the playlist is not **Public**.
   - Gemini `429` → free quota used up. Items wait as **Skipped-by-limit** and go through on the next run. Nothing is lost.
3. One bad link never stops the run. It shows in **⚠️ Errors** with the reason in **Error**.
4. Re-run by hand: Actions → **Content Inbox pipeline** → **Run workflow**.

Saving never depends on the job. The phone writes to Notion directly, so a failed run loses nothing.

---

## Free-tier limits (checked Nov 2026; Google can change them anytime)

| Thing | Limit | How the code handles it |
|---|---|---|
| `gemini-3.8-flash` (watches videos) | ~5/min, ~20/day, 250k tokens/min | Max 18 calls/day (counted in `state/last_run.json`, reset at midnight Pacific = ~13:30 IST). Then videos fall back to Flash-Lite. |
| `gemini-3.5-flash-lite` (X posts, fallback) | ~500/day | X posts are judged 10 per request. |
| YouTube video into Gemini | 8 h/day, public videos only | Capped at 450 min/day. Over that, videos wait. |
| Long videos | 250k tokens/min | 1 frame per 5 s at low resolution. Videos > 85 min are judged from title, description and chapters ("Metadata only"). |
| YouTube Data API | 10,000 units/day | One run uses < 10. |
| GitHub Actions (private repo) | 2,000 min/month | ~2–4 min per run → ~150 min/month. |

See your real Gemini limits: aistudio.google.com → usage / rate-limit page for your project.

Free-tier note: on Gemini's free tier, Google may use the prompts to improve its products. Here that is public videos and posts plus your focus areas. Nothing private.

---

## One-time setup

### 1. Already done for you
- Notion database **Content Inbox** under **Life → 💼 Professional**, with all 9 views. The IDs are in `config.yaml` → `ids`.
- This repo, with tests passing on every push.

### 2. Keys (only 3 secrets)
1. **Gemini key**: aistudio.google.com → **Get API key** → **Create API key**.
2. **YouTube key**: console.cloud.google.com → select the same project → **APIs & Services → Library** → "YouTube Data API v3" → **Enable** → **Credentials → Create credentials → API key** → **Restrict key** → YouTube Data API v3.
3. **Notion token**: notion.so/profile/integrations → **New integration** (Internal, your personal workspace) → copy the secret. Then open **Life → Content Inbox** → **••• → Connections →** add the integration.

Add them in GitHub: repo → **Settings → Secrets and variables → Actions → New repository secret**: `GEMINI_API_KEY`, `YOUTUBE_API_KEY`, `NOTION_TOKEN`.

**YouTube playlist**: You → Playlists → **New playlist** "Inbox", **Public**. Put its `list=` ID in `config.yaml` → `ids.YT_PLAYLIST_ID`. It is not a secret.

Never paste keys into a chat. Only put them in GitHub secrets, the Shortcut on your phone, or a local `.env`.

### 3. Test the judge, then go live
1. Actions → **Content Inbox pipeline** → Run workflow → mode **test-urls**, paste a YouTube link and an X link separated by a space. The log prints the full judgement. Nothing is written.
2. Same, mode **dry-run**: reads Notion and the playlist, judges, prints what it would write.
3. Same, mode **normal**, tick **plan**: real run. Check Notion.

Notion API version used everywhere: **`2026-03-11`** (latest). From `2025-09-03` on, a database holds "data sources". Rows are created under the **data source ID**, so the Shortcut uses `data_source_id`, not `database_id`.

### 4. The iPhone Shortcut "Save to Inbox"

Open **Shortcuts** → **+** → name it **Save to Inbox**.

**Settings** (tap the ⓘ / name at the top → Details):
1. **Show in Share Sheet** → ON.
2. Tap **Receives** (the first grey line in the editor) → keep only **URLs** and **Text**.
3. **If there's no input** → **Get Clipboard**.

**Actions**, in this order (search each name in the action bar at the bottom):

1. **Get URLs from Input** → input = *Shortcut Input*.
2. **Get Item from List** → *First Item* from *URLs*.
3. **If** → *Item from List* → **does not have any value**
   - inside: **Show Alert** → "No link found" (turn off *Show Cancel*)
   - inside: **Stop This Shortcut**
   - **Otherwise** stays empty. **End If**.
4. **Replace Text** → find `(mobile\.)?twitter\.com` → replace with `x.com` → in *Item from List*. Tap ▸ and turn **Regular Expression** ON.
5. **Replace Text** → find `https?://youtu\.be/([A-Za-z0-9_-]+).*` → replace `https://www.youtube.com/watch?v=$1` → in *Updated Text* (Regular Expression ON).
6. **Replace Text** → find `(?<!/watch)\?.*$` → replace with nothing → in *Updated Text* (Regular Expression ON). This drops `?s=20`, `?si=…` and similar tracking junk.
7. **Replace Text** → find `&.*$` → replace with nothing → in *Updated Text* (Regular Expression ON).
8. **Date** → *Current Date*.
9. **Format Date** → *Date* → Date Format **ISO 8601**, *Include ISO 8601 Time* OFF. (Gives `2026-11-03`.)
10. **Text** → paste this, then replace the two `VARIABLE` words by tapping them and choosing the variable (long-press → *Select Variable*):
    ```json
    {
      "parent": {"type": "data_source_id", "data_source_id": "PASTE_DATA_SOURCE_ID"},
      "properties": {
        "Name": {"title": [{"text": {"content": "VARIABLE_Updated_Text"}}]},
        "URL": {"url": "VARIABLE_Updated_Text"},
        "Pipeline": {"select": {"name": "New"}},
        "Saved On": {"date": {"start": "VARIABLE_Formatted_Date"}}
      }
    }
    ```
    `PASTE_DATA_SOURCE_ID` = `ba2e223d-cc9b-41cf-996d-8f06d8b90ea3` (also in `config.yaml`). Use the *last* **Updated Text** (from step 7).
11. **Get Contents of URL** → URL `https://api.notion.com/v1/pages` → tap ▸:
    - Method **POST**
    - Headers (tap *Add new header* 3 times):
      `Authorization` = `Bearer ntn_…your Notion secret…`
      `Notion-Version` = `2026-03-11`
      `Content-Type` = `application/json`
    - Request Body **File** → choose the **Text** variable from step 10.
12. **Get Dictionary Value** → key `object` in *Contents of URL*.
13. **If** *Dictionary Value* **is** `page`
    - **Show Notification** → "Saved ✓"
    - **Otherwise**: **Get Dictionary Value** → key `message` in *Contents of URL* → **Show Alert** → *Dictionary Value*.
    - **End If**.

Your Notion secret lives only inside this Shortcut on your phone. It can only touch pages you connected to the integration.

**Test it:** open any X post → Share → Share via… → Save to Inbox → "Saved ✓" → the row shows in Notion with Pipeline = New.

### 5. Make it 1 tap away
**Share sheet (no scrolling):** in X tap Share → **Share via…** → scroll to the bottom of the action list → **Edit Actions…** → find *Save to Inbox* → tap the green **+** (adds to Favorites) → drag it to the **top of Favorites** → Done. It now sits directly under the row of app icons. (The app-icon row itself only takes apps, not Shortcuts.)

**Action Button (fastest):** Settings → **Action Button** → swipe to **Shortcut** → **Choose a Shortcut** → *Save to Inbox*. Flow: in X tap Share → **Copy Link** → press the Action Button.

### 6. YouTube capture
Tap **Save** under a video → if it says "Saved to Watch later", tap **Change** → tick **Inbox**. YouTube usually remembers the last playlist after that. Shorts: **⋯ → Save to playlist → Inbox**. Watch Later can't be read by any API, so always use **Inbox**. The playlist must stay **Public**.

---

## Views by hand (only if you ever rebuild the database with `setup_notion.py`)
In the database: **+** next to the view tabs → **Table** → name it → **Filter** / **Sort**:
- 📅 Today: Planned For *is* today · Done *unchecked* · sort Priority ↑, Time ↑
- 📥 Inbox: Pipeline = Ready · Done unchecked · Verdict ≠ 🗑 Skip · sort Priority ↑, Saved On ↓
- 🗓 This Week: Saved On *is within* past week
- ⚡ Quick Wins: Time (min) ≤ 10 · Done unchecked · Verdict is 💎 Gem or ✅ Solid
- 🛠 Act On These: Value Type = Action · Done unchecked
- 💎 Gems: Verdict = 💎 Gem · ✅ Done: Done checked, sort Done On ↓ · 🗑 Skipped: Verdict = 🗑 Skip · ⚠️ Errors: Pipeline = Error

---

## Files
| File | What it does |
|---|---|
| `pipeline.py` | Main job: ingest → normalise/dedupe → enrich → judge → write → plan day → housekeeping |
| `judge.py` + `rubric.md` | Gemini prompt, JSON schema, video and batched X calls |
| `scoring.py` / `planner.py` | Quality, verdict, priority / daily checklist (pure functions, tested) |
| `sources/youtube.py`, `sources/x.py` | YouTube Data API, FxTwitter `/2/thread` |
| `notion_client.py` | Notion REST wrapper (API 2026-03-11) |
| `setup_notion.py` / `digest.py` | One-time database + views / weekly page |
| `state/last_run.json` | Last run counts + today's Gemini usage. Committed by the bot. |
| `tests/` | `python -m pytest -q` (also runs on every push) |

Local run (optional): `pip install -r requirements.txt`, copy `.env.example` to `.env`, fill it in, then `python pipeline.py --dry-run`.
