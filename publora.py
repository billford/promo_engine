import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from config import PUBLORA_BASE_URL, POST_HOUR


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
        # Match by platformId prefix, e.g. "linkedin-n2c6artUXk" -> "linkedin"
        platform_name = platform_id.split("-")[0].lower()
        accounts[platform_name] = platform_id

    return accounts


def _scheduled_time_utc(local_tz: str) -> str:
    tz = ZoneInfo(local_tz)
    now = datetime.now(tz)
    target = now.replace(hour=POST_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        from datetime import timedelta
        target += timedelta(days=1)
    utc_dt = target.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _post_once(api_key: str, payload: dict) -> dict:
    url = f"{PUBLORA_BASE_URL}/create-post"
    try:
        resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=15)
        return resp
    except requests.RequestException:
        return None


def schedule_post(
    api_key: str,
    account_id: str,
    post_text: str,
    scheduled_time: str,
) -> str:
    """Submit one post to Publora. Returns the Publora post ID."""
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

    data = resp.json()
    return data.get("postGroupId") or ""


def run_publora(
    posts: dict,
    config: dict,
    platforms: list[str],
) -> dict[str, str]:
    """Schedule posts for requested platforms. Returns {platform: publora_post_id}."""
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
        publora_id = schedule_post(api_key, account_id, post_text, scheduled_time)
        result[platform] = publora_id
        print(f"Scheduled {platform} post (Publora ID: {publora_id}) for {scheduled_time}")

    return result
