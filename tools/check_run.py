#!/usr/bin/env python3
"""Post-run inspection: did today's scheduled run behave?

Reads the authoritative record (post_history) rather than the log, because the log
has no timestamps and a retry appends to it without any run delimiter. Usage:

    .venv/bin/python tools/check_run.py [YYYY-MM-DD]
"""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# pylint: disable=wrong-import-position
from config import load_config
from db import get_conn
from health import run_health_checks

PLATFORMS = ("linkedin", "bluesky", "facebook")
LOG_ERROR_RE = re.compile(r"^(ERROR \[|HEALTH \(error\)|Attempt \d+/\d+ failed|WARNING: LinkedIn post failed)")
URL_RE = re.compile(r"https?://\S+")


def main() -> int:
    config = load_config()
    tz = ZoneInfo(config.get("timezone", "America/New_York"))
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(tz).strftime("%Y-%m-%d")

    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    window = (start.astimezone(ZoneInfo("UTC")).isoformat(),
              (start + timedelta(days=1)).astimezone(ZoneInfo("UTC")).isoformat())

    problems = []
    with get_conn(config["db_path"]) as conn:
        rows = conn.execute(
            """
            SELECT ph.platform, ph.posted_at, ph.post_text, c.title
            FROM post_history ph JOIN content c ON c.id = ph.content_id
            WHERE ph.dry_run = 0 AND ph.posted_at >= ? AND ph.posted_at < ?
            ORDER BY ph.posted_at
            """,
            window,
        ).fetchall()
        errors, warnings = run_health_checks(conn)

    print(f"=== Run check for {day} ({len(rows)} post(s))\n")

    for platform in PLATFORMS:
        posts = [r for r in rows if r["platform"] == platform]
        mark = "OK  " if len(posts) == 1 else "BAD "
        if len(posts) != 1:
            problems.append(f"{platform}: {len(posts)} post(s), expected exactly 1")
        print(f"{mark}{platform}: {len(posts)} post(s)")
        for r in posts:
            print(f"      {r['posted_at'][11:19]}Z  {r['title']}")

    # Facebook is pasted by hand, so a post without a link is useless.
    for r in (r for r in rows if r["platform"] == "facebook"):
        if URL_RE.search(r["post_text"]):
            print("\nOK  facebook post carries a link")
        else:
            problems.append("facebook post has no link in the body")
            print("\nBAD facebook post has NO link")

    tail = []
    try:
        with open("promo_engine.log", encoding="utf-8", errors="replace") as fh:
            tail = [ln.rstrip() for ln in fh.readlines()[-400:] if LOG_ERROR_RE.match(ln)]
    except OSError as exc:
        print(f"\n(could not read promo_engine.log: {exc})")

    print(f"\n--- log errors/retries in last 400 lines: {len(tail)}")
    for line in tail[-15:]:
        print(f"    {line[:160]}")

    print(f"\n--- health errors: {errors or 'none'}")
    print(f"--- health warnings: {warnings or 'none'}")

    print("\n" + ("PROBLEMS:\n  " + "\n  ".join(problems) if problems else "All checks passed."))

    if rows:
        print("\n--- tone sample (first two lines of each post)")
        for r in rows:
            body = "\n".join(r["post_text"].strip().splitlines()[:2])
            print(f"\n[{r['platform'].upper()}] {body}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
