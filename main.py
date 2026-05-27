#!/usr/bin/env python3
import argparse
import sys

from config import load_config
from db import init_db, get_conn, insert_post_record


def parse_args():
    parser = argparse.ArgumentParser(description="Promo Engine — daily content promotion")
    parser.add_argument("--dry-run", action="store_true", help="Print posts, skip Publora")
    parser.add_argument("--skip-collect", action="store_true", help="Skip catalog refresh")
    parser.add_argument(
        "--platform",
        choices=["linkedin", "bluesky", "both"],
        default="linkedin",
        help="Which platform(s) to post to",
    )
    parser.add_argument("--verbose", action="store_true", help="Print scorer rationale and full post text")
    return parser.parse_args()


def main():
    args = parse_args()

    # Step 1: load and validate config
    config = load_config()
    db_path = config["db_path"]
    init_db(db_path)

    platforms = ["linkedin", "bluesky"] if args.platform == "both" else [args.platform]

    with get_conn(db_path) as conn:

        # Step 2: process pending LinkedIn first comments from yesterday's scheduled post
        if not args.dry_run:
            from publora import process_pending_comments
            process_pending_comments(conn, config)

        # Step 3: refresh catalog
        if not args.skip_collect:
            from collector import run_collector
            run_collector(conn, config)
        else:
            print("Skipping catalog refresh (--skip-collect).")

        # Step 4: pick today's winner
        from scorer import pick_content
        selected = pick_content(conn, config, platforms)

        print(f"\nSelected: \"{selected['title']}\" ({selected['source']})")
        if args.verbose:
            print(f"Rationale: {selected['rationale']}")

        # Step 5: write platform posts
        from writer import write_posts
        posts = write_posts(selected, config)

        if args.dry_run or args.verbose:
            if args.dry_run:
                print(f"\n[DRY RUN] Selected: \"{selected['title']}\" ({selected['source']})")
                print(f"Rationale: {selected['rationale']}")
            for p in platforms:
                print(f"\n--- {p.upper()} ---")
                print(posts[p])

        # Step 6: schedule or dry-run
        if args.dry_run:
            for platform in platforms:
                insert_post_record(
                    conn,
                    content_id=selected["content_id"],
                    platform=platform,
                    post_text=posts[platform],
                    publora_post_id=None,
                    dry_run=True,
                )
            print("\nDry run complete. Records logged to DB with dry_run=1.")
            return

        from publora import run_publora
        publora_ids = run_publora(
            posts, config, platforms,
            conn=conn,
            content_url=selected["url"],
            content_title=selected["title"],
        )

        # Step 7: record to DB
        for platform in platforms:
            insert_post_record(
                conn,
                content_id=selected["content_id"],
                platform=platform,
                post_text=posts[platform],
                publora_post_id=publora_ids.get(platform),
                dry_run=False,
            )

    print("\nDone.")


if __name__ == "__main__":
    main()
