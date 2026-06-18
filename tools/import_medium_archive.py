#!/usr/bin/env python3
"""One-time importer: seeds the full Medium catalog from a Medium data export."""

import argparse
import re
import sys
from pathlib import Path

# Allow running from tools/ or from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# pylint: disable=wrong-import-position
from bs4 import BeautifulSoup
from db import init_db, get_conn, upsert_content


def parse_date_from_filename(filename: str) -> str:
    match = re.match(r"(\d{4}-\d{2}-\d{2})", filename)
    if match:
        return match.group(1) + "T00:00:00+00:00"
    return ""


def parse_post(html_path: Path) -> dict | None:
    try:
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="replace"), "lxml")
    except Exception:  # pylint: disable=broad-exception-caught
        return None

    # Canonical URL — Medium exports use <a class="p-canonical"> in the footer
    canonical_tag = soup.find("a", class_="p-canonical")
    if not canonical_tag or not canonical_tag.get("href"):
        return None
    url = canonical_tag["href"]
    if "medium.com" not in url:
        return None

    # Title — Medium exports use <h3 class="...graf--title..."> inside the article
    title_tag = soup.find(class_=re.compile(r"graf--title"))
    if title_tag:
        title = title_tag.get_text(strip=True)
    else:
        title_el = soup.find("title")
        title = re.sub(r"\s*[-–|]\s*Medium\s*$", "", title_el.get_text(strip=True)) if title_el else ""

    # Published date — <time class="dt-published" datetime="...">
    time_tag = soup.find("time", class_="dt-published")
    if time_tag and time_tag.get("datetime"):
        published = time_tag["datetime"]
    else:
        published = parse_date_from_filename(html_path.name)

    # Description: first substantive paragraph inside the article body
    description = ""
    for p in soup.find_all("p", class_=re.compile(r"graf--p")):
        text = p.get_text(strip=True)
        if text and len(text) > 40:
            description = text[:300]
            break

    return {
        "id": url,
        "source": "medium",
        "title": title,
        "url": url,
        "published_date": published,
        "description": description,
        "tags": [],
    }


def run(archive_path: Path, db_path: str, verbose: bool, dry_run: bool) -> None:
    html_files = sorted(archive_path.glob("*.html"))

    print("Medium Archive Importer")
    print("-----------------------")
    print(f"Archive path:  {archive_path}")
    print(f"Database:      {db_path}")
    print(f"\nScanning... {len(html_files)} HTML files found.")

    if not html_files:
        print("No HTML files found. Check the --archive path.", file=sys.stderr)
        sys.exit(1)

    imported = 0
    skipped = 0
    already_in_db = 0

    if not dry_run:
        init_db(db_path)

    with get_conn(db_path) as conn:
        for html_file in html_files:
            post = parse_post(html_file)

            if post is None:
                skipped += 1
                if verbose:
                    print(f"  SKIP (no canonical URL): {html_file.name}")
                continue

            if dry_run:
                imported += 1
                if verbose:
                    print(f"  [dry-run] {post['title'][:80]}")
                continue

            # Check if already present
            existing = conn.execute(
                "SELECT id FROM content WHERE id = ?", (post["id"],)
            ).fetchone()

            if existing:
                already_in_db += 1
                if verbose:
                    print(f"  SKIP (already in DB): {post['title'][:80]}")
                continue

            upsert_content(conn, post)
            imported += 1
            if verbose:
                print(f"  Imported: {post['title'][:80]}")

    print("\nResults:")
    print(f"  Imported:       {imported:>4}")
    print(f"  Skipped:        {skipped:>4}  (no canonical URL)")
    if not dry_run:
        print(f"  Already in DB:  {already_in_db:>4}")
    print(f"\nDone. {imported} posts now available to the scorer.")


def main():
    parser = argparse.ArgumentParser(description="Import Medium archive into promo_engine.db")
    parser.add_argument("--archive", required=True, help="Path to posts/ directory from Medium export")
    parser.add_argument("--db", default="promo_engine.db", help="Path to promo_engine.db")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Parse and count without writing to DB")
    args = parser.parse_args()

    archive_path = Path(args.archive)
    if not archive_path.is_dir():
        print(f"ERROR: Archive path does not exist or is not a directory: {archive_path}", file=sys.stderr)
        sys.exit(1)

    run(archive_path, args.db, args.verbose, args.dry_run)


if __name__ == "__main__":
    main()
