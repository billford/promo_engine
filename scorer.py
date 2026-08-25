import json
import re
import sys
import sqlite3
from datetime import date

import anthropic

from config import CLAUDE_MODEL, COOLDOWN_DAYS, RECENT_SELECTION_DAYS
from db import (
    canonical_content_id,
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

Respond with JSON only, no preamble, no explanation outside the JSON.
Copy content_id verbatim from the catalog's "ID:" line, including its "medium:" or
"youtube:" prefix. Do not shorten it, and do not substitute the URL for it.
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
    # Eligible on every requested platform. main.py drives one platform per call, but
    # the intersection keeps a combined call honest rather than quietly using platform one.
    eligible = {i["id"]: i for i in get_eligible_content(conn, active_platforms[0], COOLDOWN_DAYS)}
    for platform in active_platforms[1:]:
        still_ok = {i["id"] for i in get_eligible_content(conn, platform, COOLDOWN_DAYS)}
        eligible = {k: v for k, v in eligible.items() if k in still_ok}

    recently_selected = get_recently_selected_ids(conn, days=RECENT_SELECTION_DAYS)
    items = [v for k, v in eligible.items() if k not in recently_selected]

    if "linkedin" in active_platforms:
        filtered = [i for i in items if _is_linkedin_appropriate(i)]
        if filtered:
            return filtered
        if items:
            print("NOTE: All eligible content matched exclusion filter — using unfiltered list.", file=sys.stderr)
            return items
    elif items:
        return items

    print("NOTE: All content within cooldown window. Resetting to oldest items.", file=sys.stderr)
    fallback = get_oldest_content_by_platform(conn, active_platforms[0])
    # The fallback pool is ordered by last-posted, so without this filter it hands the
    # scorer the same handful of articles on consecutive days.
    fresh = [i for i in fallback if i["id"] not in recently_selected]
    if not fresh:
        print(
            "NOTE: Entire fallback pool was posted in the last "
            f"{RECENT_SELECTION_DAYS} days — allowing a repeat.",
            file=sys.stderr,
        )
        fresh = fallback
    return fresh[:20]


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

    if "content_id" not in result:
        raise RuntimeError("Scorer response missing 'content_id'.")

    original = _match_catalog_item(eligible_items, result["content_id"])
    if original is None:
        raise RuntimeError(
            f"Scorer returned content_id {result['content_id']!r}, which is not in the "
            f"catalog of {len(eligible_items)} eligible item(s). Refusing to post."
        )

    # Everything the poster touches comes from the database row. The model's job is to
    # choose an id; its transcription of a 95-character URL is not trustworthy, and a
    # garbled one previously meant posting a dead link and recording a history row that
    # no cooldown check could ever match.
    return {
        "content_id": original["id"],
        "title": original["title"],
        "url": original["url"],
        "source": original["source"],
        "description": original.get("description") or "",
        "content_type": original.get("content_type"),
        "rationale": str(result.get("rationale", "")),
    }


def _match_catalog_item(items: list[dict], content_id: str) -> dict | None:
    """Resolve the scorer's chosen id against the catalog, tolerating URL variants."""
    by_id = {i["id"]: i for i in items}
    if content_id in by_id:
        return by_id[content_id]

    canonical = canonical_content_id(content_id)
    if canonical in by_id:
        return by_id[canonical]

    # The scorer sometimes echoes the id with its "medium:"/"youtube:" prefix stripped.
    # Accept the bare key only when exactly one catalog item carries it, so a genuinely
    # ambiguous id still fails loudly rather than promoting the wrong article.
    bare = canonical.split(":", 1)[-1].strip()
    if bare:
        matches = [i for k, i in by_id.items() if k.split(":", 1)[-1] == bare]
        if len(matches) == 1:
            return matches[0]

    return None
