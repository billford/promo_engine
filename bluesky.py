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
        raise RuntimeError(f"Bluesky login failed ({resp.status_code}): {resp.text}")
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


def _image_embed(access_jwt: str, image) -> dict | None:
    """Upload the photo and return an images embed, or None so the post still goes out."""
    try:
        resp = requests.post(
            f"{BSKY_API}/com.atproto.repo.uploadBlob",
            headers={"Authorization": f"Bearer {access_jwt}", "Content-Type": "image/jpeg"},
            data=image.path.read_bytes(),
            timeout=30,
        )
    except (OSError, requests.RequestException) as exc:
        print(f"WARNING: Bluesky image upload failed, posting text only: {exc}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"WARNING: Bluesky image upload returned {resp.status_code}, posting text only: {resp.text}",
              file=sys.stderr)
        return None
    return {
        "$type": "app.bsky.embed.images",
        "images": [{
            "alt": image.alt_text,
            "image": resp.json()["blob"],
            "aspectRatio": {"width": image.width, "height": image.height},
        }],
    }


def post_to_bluesky(text: str, config: dict, image=None) -> str | None:
    """Post directly to Bluesky, with an optional images.PostImage. Returns the post URI."""
    handle = config.get("bluesky_handle")
    password = config.get("bluesky_app_password")
    if not handle or not password:
        raise RuntimeError(
            "BLUESKY_HANDLE and BLUESKY_APP_PASSWORD must be set for Bluesky posting."
        )

    access_jwt, did = _create_session(handle, password)

    record: dict = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    facets = _url_facets(text)
    if facets:
        record["facets"] = facets
    if image is not None:
        embed = _image_embed(access_jwt, image)
        if embed:
            record["embed"] = embed

    resp = requests.post(
        f"{BSKY_API}/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {access_jwt}"},
        json={"repo": did, "collection": "app.bsky.feed.post", "record": record},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Bluesky post failed ({resp.status_code}): {resp.text}")

    uri = resp.json().get("uri")
    print(f"Posted to Bluesky: {uri}")
    return uri
