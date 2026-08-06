import json
import re
import sys
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import anthropic
import feedparser
import requests
from bs4 import BeautifulSoup
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import CLAUDE_MODEL, MEDIUM_RSS_URL, YOUTUBE_CHANNEL_HANDLE
from db import upsert_content, get_unclassified_content, get_content_needing_fetch


_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _fetch_article_text(url: str) -> str:
    """Fetch full article text from URL. Returns empty string on failure."""
    try:
        r = requests.get(url, headers=_FETCH_HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        article = soup.find("article") or soup
        text = article.get_text(separator=" ").strip()
        if "Enable JavaScript" in text[:200]:
            return ""
        return text[:3000]
    except Exception:  # pylint: disable=broad-exception-caught
        return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def collect_medium(conn: sqlite3.Connection, rss_url: str = MEDIUM_RSS_URL) -> int:
    try:
        feed = feedparser.parse(rss_url)
        if feed.bozo and not feed.entries:
            print(f"WARNING: Medium RSS parse error: {feed.bozo_exception}", file=sys.stderr)
            return 0
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"WARNING: Medium RSS fetch failed: {exc}", file=sys.stderr)
        return 0

    count = 0
    for entry in feed.entries:
        url = entry.get("link", "")
        if not url:
            continue

        tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]

        raw_html = ""
        if entry.get("content"):
            raw_html = entry["content"][0].get("value", "")
        if not raw_html:
            raw_html = entry.get("summary", "")
        try:
            description = BeautifulSoup(raw_html, "lxml").get_text(separator=" ").strip()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"DEBUG: HTML strip failed for entry {url!r}: {exc}", file=sys.stderr)
            description = raw_html

        published = entry.get("published", "")
        if published:
            try:
                published = parsedate_to_datetime(published).isoformat()
            except (ValueError, TypeError) as exc:
                print(f"DEBUG: Date parse failed for {published!r}: {exc}", file=sys.stderr)

        upsert_content(conn, {
            "id": url,
            "source": "medium",
            "title": entry.get("title", "").strip(),
            "url": url,
            "published_date": published,
            "description": description[:3000],
            "tags": tags,
            "full_content_fetched": 1,
        })
        count += 1

    return count


def _resolve_channel(youtube, handle: str) -> tuple[str, str]:
    """Return (channel_id, uploads_playlist_id) for a given handle."""
    resp = youtube.channels().list(
        part="contentDetails",
        forHandle=handle,
    ).execute()

    items = resp.get("items", [])
    if not items:
        raise ValueError(f"No YouTube channel found for handle {handle!r}")

    channel_id = items[0]["id"]
    uploads_playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    return channel_id, uploads_playlist


def collect_youtube(conn: sqlite3.Connection, api_key: str) -> int:
    try:
        youtube = build("youtube", "v3", developerKey=api_key)
        _, uploads_playlist = _resolve_channel(youtube, YOUTUBE_CHANNEL_HANDLE)
    except HttpError as exc:
        if exc.resp.status == 403:
            print("WARNING: YouTube API quota exceeded — using cached catalog", file=sys.stderr)
        else:
            print(f"WARNING: YouTube API error: {exc}", file=sys.stderr)
        return 0
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"WARNING: YouTube fetch failed: {exc}", file=sys.stderr)
        return 0

    count = 0
    next_page = None

    while True:
        try:
            kwargs = {"part": "snippet", "playlistId": uploads_playlist, "maxResults": 50}
            if next_page:
                kwargs["pageToken"] = next_page

            resp = youtube.playlistItems().list(**kwargs).execute()
        except HttpError as exc:
            if exc.resp.status == 403:
                print("WARNING: YouTube API quota exceeded mid-fetch — stopping early", file=sys.stderr)
            else:
                print(f"WARNING: YouTube API error: {exc}", file=sys.stderr)
            break

        for item in resp.get("items", []):
            snippet = item["snippet"]
            video_id = snippet["resourceId"]["videoId"]
            url = f"https://www.youtube.com/watch?v={video_id}"
            published = snippet.get("publishedAt", "")

            description = snippet.get("description", "")[:500]

            upsert_content(conn, {
                "id": video_id,
                "source": "youtube",
                "title": snippet.get("title", "").strip(),
                "url": url,
                "published_date": published,
                "description": description,
                "tags": [],
            })
            count += 1

        next_page = resp.get("nextPageToken")
        if not next_page:
            break

    return count


_CLASSIFY_PROMPT = """\
Classify each article as 'business' or 'personal'.

business = professional, tech, AI, cybersecurity, career, leadership, productivity
personal = opinion, personal story, humor, pop culture, lifestyle, general commentary

Return a JSON array of {{"n": <number>, "type": "business" or "personal"}} — no other text.

Articles (pipe-delimited: n|title|description):
{catalog}"""

_CLASSIFY_BATCH = 40


def classify_unclassified(conn: sqlite3.Connection, api_key: str) -> int:
    items = get_unclassified_content(conn)[:_CLASSIFY_BATCH]
    if not items:
        return 0

    # Reference articles by batch index, not by id: ids are full Medium URLs, and echoing
    # 60 of them back blew past max_tokens, truncating the JSON so nothing ever parsed.
    catalog = "\n".join(
        f"{n}|{item['title']}|{(item.get('description') or '')[:200]}"
        for n, item in enumerate(items)
    )

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(catalog=catalog)}],
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"WARNING: Content classification failed: {exc}", file=sys.stderr)
        return 0

    if response.stop_reason == "max_tokens":
        print("WARNING: Classifier response hit max_tokens — results truncated.", file=sys.stderr)

    raw = response.content[0].text.strip()
    try:
        results = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            print("WARNING: Classifier returned unparseable response.", file=sys.stderr)
            return 0
        try:
            results = json.loads(m.group())
        except json.JSONDecodeError:
            print("WARNING: Classifier returned malformed JSON array.", file=sys.stderr)
            return 0

    count = 0
    for entry in results:
        index, ctype = entry.get("n"), entry.get("type")
        if ctype not in ("business", "personal") or not isinstance(index, int):
            continue
        if not 0 <= index < len(items):
            continue
        cursor = conn.execute(
            "UPDATE content SET content_type = ? WHERE id = ?",
            (ctype, items[index]["id"]),
        )
        count += cursor.rowcount  # count real updates, not just parsed rows

    return count


def backfill_article_content(conn: sqlite3.Connection) -> int:
    """Fetch full content for older articles not in the current RSS window."""
    items = get_content_needing_fetch(conn, limit=5)
    fetched = 0
    for item in items:
        text = _fetch_article_text(item["url"])
        if text:
            conn.execute(
                "UPDATE content SET description = ?, full_content_fetched = 1 WHERE id = ?",
                (text, item["id"]),
            )
            fetched += 1
        else:
            # Mark as attempted so we don't retry on every run
            conn.execute(
                "UPDATE content SET full_content_fetched = 1 WHERE id = ?",
                (item["id"],),
            )
    return fetched


def run_collector(conn: sqlite3.Connection, config: dict) -> None:
    medium_count = collect_medium(conn)

    youtube_key = config.get("youtube_api_key")
    if youtube_key:
        youtube_count = collect_youtube(conn, youtube_key)
    else:
        youtube_count = 0
        print("NOTE: YOUTUBE_API_KEY not set — skipping YouTube collection.")

    backfilled = backfill_article_content(conn)
    classified = classify_unclassified(conn, config["anthropic_api_key"])
    print(
        f"Collector: {medium_count} Medium items, {youtube_count} YouTube items refreshed. "
        f"{backfilled} articles backfilled. {classified} items classified."
    )
