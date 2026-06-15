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
2. Wait 1–2 days for the export email
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
0 9 * * * cd /path/to/promo_engine && .venv/bin/python3 main.py >> promo_engine.log 2>&1

# First-comment poster at 9:05 AM
5 9 * * * cd /path/to/promo_engine && .venv/bin/python3 post_comments.py >> promo_engine.log 2>&1
```

The 5-minute gap gives Publora time to publish the scheduled post before the comment script polls for the LinkedIn URN. If a comment still fails (Publora outage, etc.), `process_pending_comments` will retry on the next daily run and fall back to a macOS notification after 48 hours.

---

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
├── collector.py                   # Fetches Medium RSS + YouTube catalog
├── scorer.py                      # Claude API: picks today's winner
├── writer.py                      # Claude API: writes platform posts
├── publora.py                     # Publora API: schedules posts
├── db.py                          # SQLite state management
├── config.py                      # Loads .env, constants
├── tools/
│   └── import_medium_archive.py   # One-time Medium export importer
├── .env.template
├── requirements.txt
└── README.md
```
