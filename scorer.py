import json
import re
import sys
import sqlite3
from datetime import date

import anthropic

from config import CLAUDE_MODEL, COOLDOWN_DAYS
from db import (
    get_eligible_content,
    get_oldest_content_by_platform,
    get_recent_post_history,
    get_recently_selected_ids,
)


LINKEDIN_EXCLUDE_PATTERNS = [
    "conspiracy corner", "haunted", "paranormal", "ghost", "ufo", "alien",
    "cryptid", "bigfoot", "urban legend", "supernatural", "occult", "curse",
]

_SCORING_SYSTEM_PROMPT_TEMPLATE = """\
You are a content promotion strategist for a semi-retired cybersecurity professional and tech writer.
The author publishes at medium.com/@billfordx.

Your job: pick one piece of content from the catalog to promote today on {platform}.

Content type preference: {content_type_pref}
- business = professional, tech, AI, cybersecurity, career, leadership
- personal = opinion, personal story, humor, pop culture, lifestyle

Strongly prefer '{content_type_pref}' content. Fall back to unclassified or the other type only
if no '{content_type_pref}' content is available.

Scoring criteria (apply in order of weight):
1. Content type match — must match platform preference above
2. Evergreen value — prefer content that doesn't go stale over time-sensitive posts
3. Variety — avoid the same topic category as recent posts (check recent history provided)
4. YouTube boost — YouTube videos are underused on text-based platforms and get a scoring boost
5. Engagement hook — strong opinion, surprising claim, or clear specific insight

Respond with JSON only, no preamble, no explanation outside the JSON:
{{
  "content_id": "<id>",
  "title": "<title>",
  "url": "<url>",
  "source": "<medium|youtube>",
  "rationale": "<one sentence>"
}}
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
            f"Type: {item.get('content_type') or 'unclassified'}\n"
            f"Title: {item['title']}\n"
            f"URL: {item['url']}\n"
            f"Published: {item.get('published_date', 'unknown')[:10]}\n"
            f"Tags: {tags_str}\n"
            f"Last promoted: {last[:10] if last != 'never' else 'never'}\n"
            f"Description: {item.get('description', '')[:200]}"
        )

    return "\n".join(lines)


def _resolve_eligible_items(conn: sqlite3.Connection, active_platforms: list[str]) -> list[dict]:
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

    recently_selected = get_recently_selected_ids(conn, days=7)
    items = [v for k, v in merged.items() if k in eligible_ids and k not in recently_selected]

    if "linkedin" in active_platforms:
        filtered = [i for i in items if _is_linkedin_appropriate(i)]
        if filtered:
            return filtered
        if items:
            print("NOTE: All eligible content matched exclusion filter — using unfiltered list.", file=sys.stderr)
            return items

    print("NOTE: All content within cooldown window. Resetting to oldest items.", file=sys.stderr)
    return get_oldest_content_by_platform(conn, "linkedin")[:20]


def _is_linkedin_appropriate(item: dict) -> bool:
    text = (item.get("title", "") + " " + item.get("description", "")).lower()
    return not any(pat in text for pat in LINKEDIN_EXCLUDE_PATTERNS)


def pick_content(
    conn: sqlite3.Connection,
    config: dict,
    platforms: list[str] | None = None,
    content_type_pref: str = "business",
) -> dict:
    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
    active_platforms = platforms or ["linkedin"]

    eligible_items = _resolve_eligible_items(conn, active_platforms)

    if not eligible_items:
        raise RuntimeError("Content catalog is empty. Run the Medium archive importer first.")

    recent_history = get_recent_post_history(conn, days=7)
    catalog_text = _build_catalog_text(eligible_items, recent_history)

    platform_label = active_platforms[0] if len(active_platforms) == 1 else "/".join(active_platforms)
    scoring_system_prompt = _SCORING_SYSTEM_PROMPT_TEMPLATE.format(
        platform=platform_label,
        content_type_pref=content_type_pref,
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": scoring_system_prompt,
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
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise RuntimeError(f"Scorer returned non-JSON response:\n{raw}") from None
        result = json.loads(match.group())

    required = {"content_id", "title", "url", "source", "rationale"}
    if not required.issubset(result.keys()):
        raise RuntimeError(f"Scorer response missing keys: {required - result.keys()}")

    return result
