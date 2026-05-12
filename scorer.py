import json
import sys
import sqlite3
from datetime import date

import anthropic

from config import CLAUDE_MODEL, COOLDOWN_DAYS
from db import get_eligible_content, get_oldest_content_by_platform, get_recent_post_history, get_recently_selected_ids


SCORING_SYSTEM_PROMPT = """\
You are a content promotion strategist for a semi-retired cybersecurity professional and tech writer.
The author publishes at medium.com/@billfordx and youtube.com/@billfordx (Conspiracy Corner channel).
Topics: AI skepticism, cybersecurity, pop culture commentary, paranormal/conspiracy.

Your job: pick one piece of content from the catalog to promote today across LinkedIn and Bluesky.

Scoring criteria (apply in order of weight):
1. Evergreen value — prefer content that doesn't go stale over time-sensitive posts
2. Platform fit — AI/cybersecurity/tech/pop culture skew well on LinkedIn; conspiracy/paranormal/urban legend skew well on Bluesky
3. YouTube boost — YouTube videos are underused on text-based platforms and get a scoring boost
4. Variety — avoid the same topic category as recent posts (check recent history provided)
5. Engagement hook — strong opinion, surprising claim, or provocative question in title/description

Respond with JSON only, no preamble, no explanation outside the JSON:
{
  "content_id": "<id>",
  "title": "<title>",
  "url": "<url>",
  "source": "<medium|youtube>",
  "rationale": "<one sentence>"
}
"""


def _build_catalog_text(items: list[dict], recent_history: list[dict]) -> str:
    lines = [f"Today's date: {date.today().isoformat()}\n"]

    if recent_history:
        lines.append("Recent post history (avoid same category):")
        for h in recent_history[:10]:
            lines.append(f"  - [{h['platform']}] {h['title']} ({h['source']}) — {h['posted_at'][:10]}")
        lines.append("")

    lines.append(f"Eligible content catalog ({len(items)} items):")
    for item in items:
        tags_str = ", ".join(item["tags"]) if item["tags"] else "none"
        last = item.get("last_posted") or "never"
        lines.append(
            f"\nID: {item['id']}\n"
            f"Source: {item['source']}\n"
            f"Title: {item['title']}\n"
            f"URL: {item['url']}\n"
            f"Published: {item.get('published_date', 'unknown')[:10]}\n"
            f"Tags: {tags_str}\n"
            f"Last promoted: {last[:10] if last != 'never' else 'never'}\n"
            f"Description: {item.get('description', '')[:200]}"
        )

    return "\n".join(lines)


def pick_content(conn: sqlite3.Connection, config: dict, platforms: list[str] | None = None) -> dict:
    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
    active_platforms = platforms or ["linkedin"]

    # Content must be eligible on all active platforms
    eligible_sets = [
        {i["id"]: i for i in get_eligible_content(conn, p, COOLDOWN_DAYS)}
        for p in active_platforms
    ]
    eligible_ids = set(eligible_sets[0].keys())
    for s in eligible_sets[1:]:
        eligible_ids &= s.keys()

    merged = {**eligible_sets[0]}
    for s in eligible_sets[1:]:
        merged.update(s)

    # Exclude anything selected in the last 7 days, dry run or not
    recently_selected = get_recently_selected_ids(conn, days=7)
    eligible_items = [v for k, v in merged.items() if k in eligible_ids and k not in recently_selected]

    if not eligible_items:
        # Cooldown reset: fall back to oldest items
        print("NOTE: All content within cooldown window. Resetting to oldest items.", file=sys.stderr)
        eligible_items = get_oldest_content_by_platform(conn, "linkedin")[:20]

    if not eligible_items:
        print("ERROR: Content catalog is empty. Run the Medium archive importer first.", file=sys.stderr)
        sys.exit(1)

    recent_history = get_recent_post_history(conn, days=7)
    catalog_text = _build_catalog_text(eligible_items, recent_history)

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SCORING_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": catalog_text},
        ],
    )

    raw = response.content[0].text.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON from response if model added any surrounding text
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            print(f"ERROR: Scorer returned non-JSON response:\n{raw}", file=sys.stderr)
            sys.exit(1)
        result = json.loads(match.group())

    required = {"content_id", "title", "url", "source", "rationale"}
    if not required.issubset(result.keys()):
        print(f"ERROR: Scorer response missing keys: {required - result.keys()}", file=sys.stderr)
        sys.exit(1)

    return result
