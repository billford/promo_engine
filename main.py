#!/usr/bin/env python3
import argparse
import sys
from zoneinfo import ZoneInfo

from config import load_config
from db import init_db, get_conn, insert_post_record, has_posted_today

PLATFORM_CONTENT_TYPE = {
    "linkedin": "business",
    "bluesky": "personal",
    "facebook": "personal",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Promo Engine — daily content promotion")
    parser.add_argument("--dry-run", action="store_true", help="Print posts, skip scheduling")
    parser.add_argument("--skip-collect", action="store_true", help="Skip catalog refresh")
    parser.add_argument(
        "--platform",
        choices=["linkedin", "bluesky", "facebook", "all"],
        default="all",
        help="Which platform(s) to post to (default: all)",
    )
    parser.add_argument("--verbose", action="store_true", help="Print scorer rationale and full post text")
    parser.add_argument("--report", action="store_true", help="Print weekly posting report and exit")
    return parser.parse_args()


def print_weekly_report(conn) -> None:
    from db import get_recent_post_history
    history = get_recent_post_history(conn, days=7)

    print(f"\n{'=' * 60}")
    print("WEEKLY POSTING REPORT — last 7 days")
    print(f"{'=' * 60}")

    if not history:
        print("No posts in the last 7 days.")
        return

    by_date: dict[str, list] = {}
    for row in history:
        day = row["posted_at"][:10]
        by_date.setdefault(day, []).append(row)

    for day in sorted(by_date.keys(), reverse=True):
        print(f"\n{day}")
        for row in by_date[day]:
            sched = f"  (scheduled: {row['scheduled_for'][:10]})" if row.get("scheduled_for") else ""
            print(f"  [{row['platform'].upper()}] {row['title']} ({row['source']}){sched}")

    total = len(history)
    print(f"\nTotal: {total} post(s) across {len(by_date)} day(s)")


def run_platform(platform: str, conn, config: dict, args) -> None:
    tz = ZoneInfo(config.get("timezone", "America/New_York"))
    if not args.dry_run and has_posted_today(conn, platform, tz):
        # A retry of the whole invocation (run_with_retry.sh) must not repost the
        # platforms that already succeeded on the earlier attempt.
        print(f"\nSkipping {platform}: already posted today.")
        return

    content_type_pref = PLATFORM_CONTENT_TYPE.get(platform, "business")

    from scorer import pick_content
    selected = pick_content(conn, config, [platform], content_type_pref)

    content_type = selected.get("content_type") or "unclassified"
    print(f"\nSelected for {platform}: \"{selected['title']}\" ({selected['source']}, {content_type})")
    if args.verbose:
        print(f"Rationale: {selected['rationale']}")

    from writer import write_post
    post_text = write_post(selected, config, platform)

    if args.dry_run or args.verbose:
        label = "FACEBOOK (Reminder)" if platform == "facebook" else platform.upper()
        if args.dry_run:
            print(f"\n[DRY RUN] {label}")
        else:
            print(f"\n--- {label} ---")
        print(post_text)
        if args.dry_run and platform == "facebook":
            from datetime import datetime, timedelta
            now = datetime.now(tz)
            due_dt = now.replace(hour=10, minute=0, second=0, microsecond=0)
            if due_dt <= now:
                due_dt += timedelta(days=1)
            print(f"Would create Reminder due: {due_dt.strftime('%Y-%m-%dT%H:%M:%S')} local")

    if args.dry_run:
        insert_post_record(
            conn,
            content_id=selected["content_id"],
            platform=platform,
            post_text=post_text,
            dry_run=True,
        )
        return

    if platform == "linkedin":
        if not config.get("publora_api_key"):
            print("ERROR: PUBLORA_API_KEY required for LinkedIn posting.", file=sys.stderr)
            sys.exit(1)
        from publora import run_publora
        scheduled = run_publora(
            {platform: post_text},
            config,
            [platform],
            conn=conn,
            content_url=selected["url"],
            content_title=selected["title"],
        )[platform]
        insert_post_record(
            conn,
            content_id=selected["content_id"],
            platform=platform,
            post_text=post_text,
            publora_post_id=scheduled.publora_post_id,
            scheduled_for=scheduled.scheduled_for,
        )

    elif platform == "bluesky":
        from bluesky import post_to_bluesky
        uri = post_to_bluesky(post_text, config)
        insert_post_record(
            conn,
            content_id=selected["content_id"],
            platform=platform,
            post_text=post_text,
            publora_post_id=uri,
        )

    elif platform == "facebook":
        from facebook import create_facebook_reminder
        create_facebook_reminder(selected["title"], post_text, config)
        insert_post_record(
            conn,
            content_id=selected["content_id"],
            platform=platform,
            post_text=post_text,
        )


def main():
    args = parse_args()

    config = load_config()
    db_path = config["db_path"]
    init_db(db_path)

    platforms = ["linkedin", "bluesky", "facebook"] if args.platform == "all" else [args.platform]

    with get_conn(db_path) as conn:

        if args.report:
            print_weekly_report(conn)
            return

        if not args.dry_run and "linkedin" in platforms and config.get("publora_api_key"):
            from publora import process_pending_comments
            process_pending_comments(conn, config)

        if not args.skip_collect:
            from collector import run_collector
            run_collector(conn, config)
        else:
            print("Skipping catalog refresh (--skip-collect).")
        conn.commit()  # bank the catalog refresh before any platform can roll back

        failures = []
        for platform in platforms:
            try:
                run_platform(platform, conn, config, args)
                conn.commit()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # One platform must never take down the others, so catch broadly rather
                # than only RuntimeError. Platform modules raise RuntimeError instead of
                # calling sys.exit precisely because SystemExit would bypass this.
                conn.rollback()
                failures.append(platform)
                print(f"ERROR [{platform}]: {type(exc).__name__}: {exc}", file=sys.stderr)

        from health import run_health_checks
        problems, warnings = run_health_checks(conn)
        for warning in warnings:
            print(f"HEALTH (warning): {warning}", file=sys.stderr)
        for problem in problems:
            print(f"HEALTH (error): {problem}", file=sys.stderr)

    if args.dry_run:
        print("\nDry run complete. Records logged to DB with dry_run=1.")
    else:
        print("\nDone.")

    if failures or problems:
        if failures:
            print(f"Failed platform(s): {', '.join(failures)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
