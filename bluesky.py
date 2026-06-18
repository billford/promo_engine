import re
import sys
from datetime import datetime, timezone

import requests

BSKY_API = "https://bsky.social/xrpc"


def _create_session(handle: str, password: str) -> tuple[str, str]:
    resp = requests.post(
        f"{BSKY_API}/com.atproto.server.createSession",
        json={"identifier": handle, "password": password},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"ERROR: Bluesky login failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)
    data = resp.json()
    return data["accessJwt"], data["did"]


def _url_facets(text: str) -> list[dict]:
    """Return AT Protocol facets for any URLs found in text."""
    facets = []
    for m in re.finditer(r'https?://[^\s]+', text):
        byte_start = len(text[:m.start()].encode("utf-8"))
        byte_end = len(text[:m.end()].encode("utf-8"))
        facets.append({
            "index": {"byteStart": byte_start, "byteEnd": byte_end},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": m.group()}],
        })
    return facets


def post_to_bluesky(text: str, config: dict) -> str | None:
    """Post directly to Bluesky. Returns the post URI or None on failure."""
    handle = config.get("bluesky_handle")
    password = config.get("bluesky_app_password")
    if not handle or not password:
        print(
            "ERROR: BLUESKY_HANDLE and BLUESKY_APP_PASSWORD must be set for Bluesky posting.",
            file=sys.stderr,
        )
        sys.exit(1)

    access_jwt, did = _create_session(handle, password)

    record: dict = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    facets = _url_facets(text)
    if facets:
        record["facets"] = facets

    resp = requests.post(
        f"{BSKY_API}/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {access_jwt}"},
        json={"repo": did, "collection": "app.bsky.feed.post", "record": record},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"ERROR: Bluesky post failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)

    uri = resp.json().get("uri")
    print(f"Posted to Bluesky: {uri}")
    return uri
