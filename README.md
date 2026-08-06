# promo_engine

Daily content promotion engine. Picks one piece of published work each day, writes platform-appropriate social posts in the author's voice, and hands them to Publora for scheduling.

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

`main.py` runs a set of invariant checks after every run (`health.py`) and prints any
problem to stderr prefixed with `HEALTH:`. **The process exits non-zero if any check
fails or any platform errored**, so `run_with_retry.sh` and launchd surface it instead
of the run looking successful.

Checks: orphaned `post_history` rows, non-canonical content ids, the same article
posted to a platform twice inside `COOLDOWN_DAYS`, unclassified articles, articles that
exhausted their content-fetch retries, and LinkedIn first comments overdue by 48h.

The cooldown check is scoped to history written after the `health_baseline` row in
`schema_meta`, which is stamped the first time the current schema initialises. History
from before that point contains known duplicates and is deliberately ignored.

Because a failing check now exits non-zero, `run_with_retry.sh` will retry the whole
invocation up to 5 times. Health problems are usually persistent, so expect the retries
to burn through quickly and the job to end non-zero — that is the intended signal.

## Repeat suppression

Three independent mechanisms, all keyed on the **canonical** content id (`medium:<hash>`
or `youtube:<id>`) rather than the feed URL, because Medium serves one article under
several hosts and the raw link made one article look like several:

1. `COOLDOWN_DAYS` (30) — per platform, excludes anything posted within the window.
2. `RECENT_SELECTION_DAYS` (14) — a hard floor applied on every path, including the
   cooldown-exhausted fallback, and counting dry runs.
3. The scorer's chosen id must resolve to a real catalog row; an unrecognised id
   aborts that platform rather than posting a model-invented URL.

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
├── publora.py                     # Publora API: schedules posts
├── health.py                      # Post-run invariant checks
├── db.py                          # SQLite state management
├── config.py                      # Loads .env, constants
├── tools/
│   └── import_medium_archive.py   # One-time Medium export importer
├── .env.template
├── requirements.txt
└── README.md
```
