import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from config import POST_HOUR, PUBLORA_BASE_URL


def _next_scheduled_time(conn, platform: str, local_tz: str) -> str:
    """Return the next unoccupied 9 AM slot for the given platform as a UTC ISO string."""
    from db import get_latest_scheduled_for
    tz = ZoneInfo(local_tz)
    now = datetime.now(tz)
    latest = get_latest_scheduled_for(conn, platform)
    if latest:
        latest_dt = datetime.fromisoformat(latest).astimezone(tz)
        candidate = (latest_dt + timedelta(days=1)).replace(
            hour=POST_HOUR, minute=0, second=0, microsecond=0
        )
        # Don't schedule in the past if the queue has fallen behind
        if candidate <= now:
            candidate = now.replace(hour=POST_HOUR, minute=0, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
    else:
        candidate = now.replace(hour=POST_HOUR, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _headers(api_key: str) -> dict:
    return {"x-publora-key": api_key}


def _get_accounts(api_key: str) -> dict[str, str]:
    """Return {platform_name_lower: platformId} for all connected accounts."""
    url = f"{PUBLORA_BASE_URL}/platform-connections"
    try:
        resp = requests.get(url, headers=_headers(api_key), timeout=15)
    except requests.RequestException as exc:
        print(f"ERROR: Publora /platform-connections network failure: {exc}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        print(f"ERROR: Publora /platform-connections returned {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)

    accounts = {}
    for conn in resp.json().get("connections", []):
        platform_id = conn.get("platformId") or ""
        if not platform_id:
            continue
        platform_name = platform_id.split("-")[0].lower()
        accounts[platform_name] = platform_id

    return accounts


def _post_once(api_key: str, payload: dict):
    url = f"{PUBLORA_BASE_URL}/create-post"
    try:
        return requests.post(url, json=payload, headers=_headers(api_key), timeout=15)
    except requests.RequestException:
        return None





def _notify_linkedin_comment(title: str, url: str) -> None:
    """Send a macOS notification to add the link as the first comment."""
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_url = url.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display notification "{safe_url}" '
        f'with title "LinkedIn: paste as first comment" '
        f'subtitle "{safe_title}"'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        print(f"WARNING: macOS notification failed: {exc.stderr.decode().strip()}", file=sys.stderr)


def process_pending_comments(conn, config: dict) -> None:
    """Notify about any backlogged LinkedIn first comments that couldn't be automated."""
    from db import get_due_pending_comments, mark_comment_done
    due = get_due_pending_comments(conn)
    for row in due:
        _notify_linkedin_comment(row["content_title"] or "today's post", row["content_url"])
        mark_comment_done(conn, row["id"])


def schedule_post(
    api_key: str, account_id: str, post_text: str, scheduled_time: str, first_comment: str | None = None
) -> dict:
    """Submit one post to Publora scheduled for scheduled_time. Returns full response data."""
    payload = {
        "content": post_text,
        "platforms": [account_id],
        "scheduledTime": scheduled_time,
    }
    if first_comment:
        payload["firstComment"] = first_comment

    resp = _post_once(api_key, payload)

    if resp is None:
        print("WARNING: Publora network failure, retrying once...", file=sys.stderr)
        time.sleep(3)
        resp = _post_once(api_key, payload)

    if resp is None:
        print("ERROR: Publora post failed after retry (network error)", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        print(f"ERROR: Publora /create-post returned {resp.status_code}:\n{resp.text}", file=sys.stderr)
        sys.exit(1)

    return resp.json()


def run_publora(
    posts: dict,
    config: dict,
    platforms: list[str],
    conn=None,
    content_url: str | None = None,
    content_title: str | None = None,
) -> dict[str, str]:
    """Schedule posts via Publora. Returns {platform: publora_post_id}."""
    api_key = config["publora_api_key"]
    accounts = _get_accounts(api_key)
    result = {}
    scheduled_times = {}

    for platform in platforms:
        account_id = accounts.get(platform)
        if not account_id:
            print(f"ERROR: No Publora account found for platform '{platform}'.", file=sys.stderr)
            print(f"       Connected accounts: {list(accounts.keys())}", file=sys.stderr)
            sys.exit(1)

        scheduled_time = _next_scheduled_time(conn, platform, config["timezone"])
        post_text = posts[platform]
        first_comment = content_url if platform == "linkedin" else None
        data = schedule_post(api_key, account_id, post_text, scheduled_time, first_comment=first_comment)
        publora_id = data.get("postGroupId") or ""
        result[platform] = publora_id
        scheduled_times[platform] = scheduled_time
        extra = " (first comment queued)" if first_comment else ""
        print(f"Scheduled {platform} post (Publora ID: {publora_id}) for {scheduled_time}{extra}")

    result["_scheduled_times"] = scheduled_times
    return result
