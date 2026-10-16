# promo_engine

Daily content promotion engine. Picks one piece of published work each day, writes a short, lightly inviting summary of it for each platform, and hands them to Publora for scheduling.

One post per platform per day. Every post tagged as AI-assisted.

---

## Setup

Complete these steps in order before the first run.

### 1. Publora

1. Sign up at [publora.com](https://publora.com)
2. Connect your LinkedIn and Bluesky accounts
3. Generate an API key from the Publora dashboard
4. Add it to `.env` as `PUBLORA_API_KEY`

### 2. YouTube Data API

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project
3. Enable **YouTube Data API v3**
4. Create an API key under Credentials
5. Add it to `.env` as `YOUTUBE_API_KEY`

### 3. Anthropic API

Add your existing key to `.env` as `ANTHROPIC_API_KEY`.

### 4. Environment file

```bash
cp .env.template .env
# Fill in all three keys
```

### 5. Install dependencies

```bash
chmod +x setup.sh run.sh
./setup.sh
```

This creates a `.venv/` in the project directory and installs all dependencies into it. Safe to re-run.

### 6. Seed the Medium archive (first run only)

Medium's RSS only returns ~10 recent posts. Before the first daily run, seed the full catalog from a Medium data export:

1. Medium → Settings → Security and apps → Download your information
2. Wait 1–2 days for the export email -- It almost never takes this long. Typically it's a few minutes. 
3. Unzip and locate the `posts/` folder
4. Run the importer:

```bash
./run.sh --skip-collect --dry-run  # verify setup first
.venv/bin/python tools/import_medium_archive.py --archive /path/to/medium-export/posts/
```

Options:
```
--archive   Path to posts/ directory (required)
--db        Path to DB file (default: ./promo_engine.db)
--verbose   Print each title as it's imported
--dry-run   Parse and count without writing to DB
```

The importer is safe to re-run — it's idempotent.

---

## Running

### Dry run (no posting)

```bash
./run.sh --dry-run
```

Prints the selected content, rationale, and both post drafts. Logs to DB with `dry_run=1` — does not affect future eligibility.

### Single platform

```bash
./run.sh --platform linkedin
./run.sh --platform bluesky
```

### Verbose output (scorer rationale + full post text)

```bash
./run.sh --verbose
```

### Skip catalog refresh (use cached data)

```bash
./run.sh --skip-collect
```

---

## Cron / scheduler setup

Two jobs are required: one to run the daily engine, and one to post the LinkedIn first comment shortly after posts go live.

**Don't put the project directory under `~/Documents`, `~/Desktop`, or any other iCloud Drive-synced folder.** If local disk space gets tight, macOS's "Optimize Mac Storage" will evict the project's files to dataless iCloud placeholders. A scheduled job that fires while a file is mid-fault-in can crash at import time with `OSError: [Errno 11] Resource deadlock avoided` — before any of the app's own retry logic ever runs. Keep the checkout somewhere local-only, e.g. `~/Developer/promo_engine`.

Both jobs should also run through `run_with_retry.sh` instead of calling `python3` directly. It retries the whole invocation (5 attempts, 20s apart) if the process exits non-zero, which is cheap insurance against any other transient startup failure.

Because that wrapper re-runs *everything*, and `main.py` exits non-zero when any single platform fails, each platform first checks whether it already has a real (non-dry-run) post recorded for the current local day and skips itself if so. A retry therefore only picks up the platforms that actually failed, instead of posting twice to the ones that succeeded.

### Option A — macOS launchd (recommended)

**Daily engine** (`~/Library/LaunchAgents/local.promo-engine.daily.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.promo-engine.daily</string>

    <key>ProgramArguments</key>
    <array>
        <string>/path/to/promo_engine/run_with_retry.sh</string>
        <string>/path/to/promo_engine/.venv/bin/python3</string>
        <string>/path/to/promo_engine/main.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/path/to/promo_engine</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>/path/to/promo_engine/promo_engine.log</string>

    <key>StandardErrorPath</key>
    <string>/path/to/promo_engine/promo_engine.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

**First-comment poster** (`~/Library/LaunchAgents/local.promo-engine.comments.plist`) — runs 5 minutes after posts go live:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.promo-engine.comments</string>

    <key>ProgramArguments</key>
    <array>
        <string>/path/to/promo_engine/run_with_retry.sh</string>
        <string>/path/to/promo_engine/.venv/bin/python3</string>
        <string>/path/to/promo_engine/post_comments.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/path/to/promo_engine</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>5</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>/path/to/promo_engine/promo_engine.log</string>

    <key>StandardErrorPath</key>
    <string>/path/to/promo_engine/promo_engine.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

Load both jobs:

```bash
launchctl load ~/Library/LaunchAgents/local.promo-engine.daily.plist
launchctl load ~/Library/LaunchAgents/local.promo-engine.comments.plist
```

### Option B — cron

```cron
# Daily engine at 9:00 AM
0 9 * * * cd /path/to/promo_engine && ./run_with_retry.sh .venv/bin/python3 main.py >> promo_engine.log 2>&1

# First-comment poster at 9:05 AM
5 9 * * * cd /path/to/promo_engine && ./run_with_retry.sh .venv/bin/python3 post_comments.py >> promo_engine.log 2>&1
```

The 5-minute gap gives Publora time to publish the scheduled post before the comment script polls for the LinkedIn URN. If a comment still fails (Publora outage, etc.), `process_pending_comments` will retry on the next daily run and fall back to a macOS notification after 48 hours.

---

## Health checks and exit codes

`main.py` runs a set of invariant checks after every run (`health.py`) and prints them
to stderr. They come in two severities:

**Errors** — `HEALTH (error):` — the repeat-suppression invariants are broken. **The
process exits non-zero**, so `run_with_retry.sh` and launchd surface it instead of the
run looking successful. A platform that errored also exits non-zero.

- orphaned `post_history` rows (a history row pointing at no article)
- content ids that are not canonical (duplicates will reappear)
- the same article posted to a platform twice inside `COOLDOWN_DAYS`

**Warnings** — `HEALTH (warning):` — degraded but not broken; the run still exits 0.
Failing here would trip the retry wrapper five times over a condition that won't clear.

- articles still unclassified (platform content-type routing degraded)
- articles that exhausted their content-fetch retries (Medium won't serve some pages
  to a scraper; those post from the RSS snippet)
- LinkedIn first comments overdue by 48h

The cooldown check is scoped to history written after the `health_baseline` row in
`schema_meta`, stamped the first time the current schema initialises. History from
before that point contains known duplicates and is deliberately ignored.

## Articles vs Medium responses

A Medium export bundles your replies in with your posts, and the archive importer
originally took everything with a canonical URL — so ~200 two-sentence comments sat in
the promotion pool as if they were articles.

`content.content_kind` is `article` or `response`; only articles are eligible for
promotion, classification, or content backfill. The importer now skips responses
structurally (a reply has no `graf--title` element), and `classify_content_kind()` in
`db.py` labels anything already in the catalog. That classifier is deliberately biased
against false positives — a few long replies stay marked `article` rather than risk
discarding a real post. Responses already in `post_history` keep their history; they
simply stop being selected.

## Repeat suppression

Three independent mechanisms, all keyed on the **canonical** content id (`medium:<hash>`
or `youtube:<id>`) rather than the feed URL, because Medium serves one article under
several hosts and the raw link made one article look like several:

1. `COOLDOWN_DAYS` (30) — per platform, excludes anything posted within the window.
2. `RECENT_SELECTION_DAYS` (14) — a hard floor applied on every path, including the
   cooldown-exhausted fallback, and counting dry runs.
3. The scorer's chosen id must resolve to a real catalog row; an unrecognised id
   aborts that platform rather than posting a model-invented URL.

## Photos from Negative Space

Bluesky and Facebook posts can carry a photo from the Negative Space library
(`~/projects/photo_classifier`, Postgres on misfit). Set `PHOTO_DB_URL` in `.env` to turn
it on; leave it unset for text-only posts. LinkedIn stays text-only for now.

Per post, `images.py` asks Claude for visual search terms, full-text searches the photo
descriptions and tags, and has Claude pick one of the top 25 or pass.

- **No people, no home.** The library is mostly family photos, so SQL drops anything whose
  description or tags mention people, body parts, screens, documents, or home/street
  scenes (`PEOPLE_RE`, `PRIVATE_RE`), and the picker is told to reject the same.
- **Only our own photos.** The library also holds downloaded and watermarked images and
  AI generations, so only photos whose EXIF camera make is in `OWN_CAMERA_MAKES` (Nikon,
  Apple, DJI) are considered. About 7,000 of 37,000 photos are eligible.
- **Claude looks at the actual photo.** Descriptions miss things (a stranger reflected in
  aquarium glass, hikers a few pixels tall at the foot of a waterfall), so before a photo
  is attached Claude checks the prepared file, whole and in zoomed sections, for people,
  text, watermarks, and poor quality. Up to 3 ranked picks are tried; if none pass, no photo.
- **No metadata leaves the machine.** The original is converted with `sips`, then
  re-encoded by Pillow with every EXIF tag (GPS included) dropped, under 1MB, into
  `images/` (gitignored, pruned after 14 days).
- **Never costs a post.** Off the home LAN, NAS unmounted, or any error: a warning, and
  the post goes out text-only.
- **No reuse.** A photo used on a live post is skipped for `IMAGE_COOLDOWN_DAYS` (90).
  Dry runs don't count.

Bluesky gets the photo embedded with alt text. Facebook gets a photo only in `reminder`
mode, where the Reminder links the prepared file to attach by hand (see below). `--dry-run` prints the chosen photo's path so it can be checked first.

## Facebook

Meta has no API for posting to a personal profile, so by default (`FACEBOOK_MODE=browser`)
the daily run **stages** the post in Chrome: it opens the composer, types the text and
link, waits for the link preview, and pops a notification. **You click Post.** Nothing is
published without that click. The run doesn't wait for you; the window just stays open.

One-time setup: sign in once in the dedicated Chrome profile (it's kept outside the repo,
under `~/Library/Application Support/promo_engine/`):

```bash
.venv/bin/python facebook_browser.py --login
.venv/bin/python facebook_browser.py --test "Staging test, do not post"   # then discard it
```

If staging fails for any reason (logged out, Facebook changed its layout, Chrome missing),
the run prints a `WARNING [facebook]` and falls back to the old Apple Reminder, so the post
still reaches you. `FACEBOOK_MODE=reminder` skips the browser entirely.

Worth knowing:

- **Automating Facebook is against Meta's terms.** A person clicking Post, once a day, from
  your own Chrome keeps the risk low, but a checkpoint or temporary lock is possible.
- **It will break sometimes.** The composer is found by its accessible labels ("What's on
  your mind", "Post"), which assumes an English UI. When it breaks, you get the Reminder.
- **The staging Chrome listens on a local DevTools port** (`FB_DEBUG_PORT`, default 9223)
  while it's open, and any local process can drive it through that port. Quit that Chrome
  when you're done if that bothers you; the next run relaunches it.
- The post is logged to `post_history` when it's staged, not when you click Post (the same
  as the Reminder flow).

## Known limitations

**Scoring is heuristic.** Claude picks based on defined criteria. If selections feel off, adjust the `SCORING_SYSTEM_PROMPT` in `scorer.py`.

**Posting time is fixed by cron.** 9 AM local is a reasonable default. No dynamic optimization without analytics feedback.

**Medium RSS only returns ~10 posts.** Seed the full catalog with the archive importer before first run. The RSS collector keeps the catalog current for new posts after that.

**YouTube API free tier: 10,000 units/day.** A full channel scan costs ~100 units. Fine for daily use, but don't run repeatedly in a loop.

**No engagement feedback loop.** The system doesn't read back likes or comments to improve future picks. Selection is based on content metadata and recency only.

**Publora dependency.** If Publora's API changes or goes down, posting breaks. The rest of the pipeline still runs and logs to DB — posts can be submitted manually using the `post_text` values in `post_history`.

---

## Project structure

```
promo_engine/
├── main.py                        # Entry point / orchestrator
├── post_comments.py               # Standalone: post LinkedIn first comments
├── run_with_retry.sh              # Wraps launchd/cron invocations with retry-on-failure
├── collector.py                   # Fetches Medium RSS + YouTube catalog
├── scorer.py                      # Claude API: picks today's winner
├── writer.py                      # Claude API: writes platform posts
├── images.py                      # Picks a photo from Negative Space for a post
├── publora.py                     # Publora API: schedules posts
├── bluesky.py                     # Bluesky: posts directly
├── facebook_browser.py            # Facebook: stages the post in Chrome for a manual click
├── facebook.py                    # Facebook: Apple Reminder fallback
├── health.py                      # Post-run invariant checks
├── db.py                          # SQLite state management
├── config.py                      # Loads .env, constants
├── tools/
│   └── import_medium_archive.py   # One-time Medium export importer
├── .env.template
├── requirements.txt
└── README.md
```
