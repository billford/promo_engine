import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from config import POST_HOUR, PUBLORA_BASE_URL


def _scheduled_time_utc(local_tz: str) -> str:
    tz = ZoneInfo(local_tz)
    now = datetime.now(tz)
    target = now.replace(hour=POST_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


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


def _extract_linkedin_urn(data: dict) -> str | None:
    """Find the LinkedIn post URN in a Publora create-post response."""
    for key in ("linkedinUrn", "postUrn", "platformPostId", "linkedinPostId", "urn"):
        if val := data.get(key):
            return val
    for key in ("post", "linkedin", "platform", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            for subkey in ("urn", "postUrn", "linkedinUrn", "id", "platformPostId"):
                if val := nested.get(subkey):
                    return val
    return None


def _post_linkedin_comment(api_key: str, account_id: str, posted_id: str, url: str) -> bool:
    """Post the article URL as the first comment. Returns True on success."""
    endpoint = f"{PUBLORA_BASE_URL}/linkedin-comments"
    payload = {"postedId": posted_id, "message": url, "platformId": account_id}
    try:
        resp = requests.post(endpoint, json=payload, headers=_headers(api_key), timeout=15)
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
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        print(f"WARNING: macOS notification failed: {exc.stderr.decode().strip()}", file=sys.stderr)


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
    content_url: str | None = None,
    content_title: str | None = None,
) -> dict[str, str]:
    """Post immediately for requested platforms. Returns {platform: publora_post_id}."""
    api_key = config["publora_api_key"]
    accounts = _get_accounts(api_key)
    scheduled_time = _scheduled_time_utc(config["timezone"])
    result = {}

    for platform in platforms:
        account_id = accounts.get(platform)
        if not account_id:
            print(f"ERROR: No Publora account found for platform '{platform}'.", file=sys.stderr)
            print(f"       Connected accounts: {list(accounts.keys())}", file=sys.stderr)
            sys.exit(1)

        post_text = posts[platform]
        data = schedule_post(api_key, account_id, post_text, scheduled_time)
        publora_id = data.get("postGroupId") or ""
        result[platform] = publora_id
        print(f"Scheduled {platform} post (Publora ID: {publora_id}) for {scheduled_time}")

        if platform == "linkedin" and content_url:
            linkedin_urn = _extract_linkedin_urn(data)
            if linkedin_urn:
                time.sleep(3)  # brief pause to let LinkedIn index the post
                posted = _post_linkedin_comment(api_key, account_id, linkedin_urn, content_url)
            else:
                posted = False
            if not posted:
                _notify_linkedin_comment(content_title or "today's post", content_url)

    return result
