import subprocess  # nosec B404 - used only for osascript macOS system calls
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

import requests

from config import POST_HOUR, PUBLORA_BASE_URL
from db import get_due_pending_comments, get_latest_scheduled_for, insert_pending_comment, mark_comment_done


class PostStatus(NamedTuple):
    """Publora's view of a scheduled post. `error` is always a string or None."""
    status: str
    urn: str | None
    error: str | None


class ScheduledPost(NamedTuple):
    """Result of scheduling one post — replaces a dict that smuggled a nested
    dict under a magic '_scheduled_times' key the caller had to pop()."""
    publora_post_id: str | None
    scheduled_for: str


def _stringify_error(value) -> str:
    """Publora returns errors as a string, a dict, or nothing. Normalize at the boundary."""
    if value is None:
        return "no error detail returned by Publora"
    if isinstance(value, dict):
        return str(value.get("message") or value.get("code") or value)
    return str(value)


def _get_scheduled_times(api_key: str, platform: str) -> set[str]:
    """Return the set of scheduledTime strings Publora already has queued for this platform."""
    times = set()
    page = 1
    while True:
        try:
            resp = requests.get(
                f"{PUBLORA_BASE_URL}/list-posts",
                headers=_headers(api_key),
                params={"status": "scheduled", "platform": platform, "page": page, "limit": 100},
                timeout=15,
            )
        except requests.RequestException as exc:
            print(f"WARNING: Publora /list-posts network failure: {exc} — skipping collision check",
                  file=sys.stderr)
            return times
        if resp.status_code != 200:
            print(f"WARNING: Publora /list-posts returned {resp.status_code} — skipping collision check",
                  file=sys.stderr)
            return times
        data = resp.json()
        for post in data.get("posts", []):
            scheduled_time = post.get("scheduledTime")
            if scheduled_time:
                times.add(scheduled_time)
        if not data.get("pagination", {}).get("hasNextPage"):
            break
        page += 1
    return times


def _next_scheduled_time(conn, platform: str, local_tz: str, api_key: str) -> str:
    """Return the next unoccupied 9 AM slot for the given platform as a UTC ISO string."""
    tz = ZoneInfo(local_tz)
    now = datetime.now(tz)
    latest = get_latest_scheduled_for(conn, platform)
    if latest:
        latest_dt = datetime.fromisoformat(latest).astimezone(tz)
        candidate = (latest_dt + timedelta(days=1)).replace(
            hour=POST_HOUR, minute=0, second=0, microsecond=0
        )
        if candidate <= now:
            candidate = now.replace(hour=POST_HOUR, minute=0, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
    else:
        candidate = now.replace(hour=POST_HOUR, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)

    # Local db state can fall behind reality (e.g. manual re-runs) — confirm against
    # what Publora actually has scheduled so we never double-book a slot.
    occupied = _get_scheduled_times(api_key, platform)
    candidate_str = candidate.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    while candidate_str in occupied:
        candidate += timedelta(days=1)
        candidate_str = candidate.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return candidate_str


def _headers(api_key: str) -> dict:
    return {"x-publora-key": api_key}


def _get_accounts(api_key: str) -> dict[str, str]:
    """Return {platform_name_lower: platformId} for all connected accounts."""
    url = f"{PUBLORA_BASE_URL}/platform-connections"
    try:
        resp = requests.get(url, headers=_headers(api_key), timeout=15)
    except requests.RequestException as exc:
        raise RuntimeError(f"Publora /platform-connections network failure: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(f"Publora /platform-connections returned {resp.status_code}: {resp.text}")

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


def _get_post_status(api_key: str, publora_post_id: str) -> PostStatus | None:
    """Fetch Publora's view of a scheduled post, or None on lookup failure."""
    try:
        resp = requests.get(
            f"{PUBLORA_BASE_URL}/get-post/{publora_post_id}",
            headers=_headers(api_key),
            timeout=15,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    posts = resp.json().get("posts", [])
    for post in posts:
        status = post.get("status")
        if status == "published":
            urn = post.get("postedId")
            if urn:
                return PostStatus(status=status, urn=urn, error=None)
        elif status == "failed":
            error = (
                post.get("error")
                or post.get("errorMessage")
                or post.get("failureReason")
                or post.get("reason")
            )
            return PostStatus(status=status, urn=None, error=_stringify_error(error))
    return None


def _post_linkedin_comment(api_key: str, account_id: str, linkedin_urn: str, url: str) -> bool:
    """Post the article URL as the first comment on a LinkedIn post."""
    try:
        resp = requests.post(
            f"{PUBLORA_BASE_URL}/linkedin-comments",
            json={"postedId": linkedin_urn, "message": url, "platformId": account_id},
            headers=_headers(api_key),
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"WARNING: LinkedIn comment network failure: {exc}", file=sys.stderr)
        return False
    if resp.status_code == 201:
        print("Posted LinkedIn link comment.")
        return True
    print(f"WARNING: LinkedIn comment failed ({resp.status_code}): {resp.text}", file=sys.stderr)
    return False


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
        subprocess.run(["/usr/bin/osascript", "-e", script], check=True, capture_output=True)  # nosec B603
    except subprocess.CalledProcessError as exc:
        print(f"WARNING: macOS notification failed: {exc.stderr.decode().strip()}", file=sys.stderr)


def _notify_linkedin_post_failed(title: str, error: str) -> None:
    """Send a macOS notification that the LinkedIn post itself failed to publish, with Publora's reason."""
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_error = error.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display notification "{safe_error}" '
        f'with title "LinkedIn post failed to publish" '
        f'subtitle "{safe_title}"'
    )
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], check=True, capture_output=True)  # nosec B603
    except subprocess.CalledProcessError as exc:
        print(f"WARNING: macOS notification failed: {exc.stderr.decode().strip()}", file=sys.stderr)


def process_pending_comments(conn, config: dict) -> None:
    """Post first comments for any LinkedIn posts that have gone live since last run."""
    api_key = config["publora_api_key"]
    due = get_due_pending_comments(conn)
    for row in due:
        fires_at_dt = datetime.fromisoformat(row["fires_at"].replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - fires_at_dt).total_seconds() / 3600

        if age_hours > 48:
            # Post never published or Publora marked it failed — stop retrying
            print(f"WARNING: giving up on first comment for '{row['content_title']}' (>48h) — sending notification",
                  file=sys.stderr)
            _notify_linkedin_comment(row["content_title"] or "a recent post", row["content_url"])
            mark_comment_done(conn, row["id"])
            conn.commit()
            continue

        info = _get_post_status(api_key, row["publora_post_id"])

        if info and info.status == "failed":
            title = row["content_title"] or "a recent post"
            print(f"WARNING: LinkedIn post failed to publish for '{title}': {info.error}", file=sys.stderr)
            _notify_linkedin_post_failed(title, info.error or "unknown error")
            mark_comment_done(conn, row["id"])
            conn.commit()
            continue

        urn = info.urn if info and info.status == "published" else None
        if urn:
            posted = _post_linkedin_comment(api_key, row["platform_account_id"], urn, row["content_url"])
            if posted:
                mark_comment_done(conn, row["id"])
                conn.commit()
            else:
                print(f"WARNING: LinkedIn comment API failed for '{row['content_title']}' — will retry next run",
                      file=sys.stderr)
                _notify_linkedin_comment(row["content_title"] or "a recent post", row["content_url"])
        else:
            print(f"Post not yet live for '{row['content_title']}' — will retry next run")


def schedule_post(api_key: str, account_id: str, post_text: str, scheduled_time: str) -> dict:
    """Submit one post to Publora scheduled for scheduled_time. Returns full response data."""
    payload = {
        "content": post_text,
        "platforms": [account_id],
        "scheduledTime": scheduled_time,
    }

    resp = _post_once(api_key, payload)

    if resp is None:
        print("WARNING: Publora network failure, retrying once...", file=sys.stderr)
        time.sleep(3)
        resp = _post_once(api_key, payload)

    if resp is None:
        raise RuntimeError("Publora post failed after retry (network error)")

    if resp.status_code != 200:
        raise RuntimeError(f"Publora /create-post returned {resp.status_code}:\n{resp.text}")

    return resp.json()


def run_publora(
    posts: dict,
    config: dict,
    platforms: list[str],
    conn=None,
    content_url: str | None = None,
    content_title: str | None = None,
) -> dict[str, ScheduledPost]:
    """Schedule posts via Publora. Returns {platform: ScheduledPost}."""
    api_key = config["publora_api_key"]
    accounts = _get_accounts(api_key)
    result: dict[str, ScheduledPost] = {}

    for platform in platforms:
        account_id = accounts.get(platform)
        if not account_id:
            raise RuntimeError(
                f"No Publora account found for platform '{platform}'. "
                f"Connected accounts: {list(accounts.keys())}"
            )

        scheduled_time = _next_scheduled_time(conn, platform, config["timezone"], api_key)
        post_text = posts[platform]
        data = schedule_post(api_key, account_id, post_text, scheduled_time)
        publora_id = data.get("postGroupId") or None
        result[platform] = ScheduledPost(publora_post_id=publora_id, scheduled_for=scheduled_time)
        print(f"Scheduled {platform} post (Publora ID: {publora_id}) for {scheduled_time}")

        if platform == "linkedin" and content_url and conn is not None and publora_id:
            insert_pending_comment(conn, publora_id, account_id, content_url, content_title, scheduled_time)
            print("Queued LinkedIn first comment for after post goes live.")

    return result
