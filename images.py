"""Pick a photo from the Negative Space library (~/projects/photo_classifier) for a post.

That library is a personal archive: most of it is family photos, and most files carry
GPS in their EXIF. So selection is deliberately conservative, in three layers:

1. SQL never returns a photo whose description or tags mention people, screens,
   documents, or anything that places the home (house, yard, street), or that wasn't
   taken on one of our own cameras.
2. Claude, choosing among candidates by description, is told to reject the same
   things and to pass rather than attach a weak match.
3. Claude then looks at the actual prepared image. Descriptions miss things: a
   stranger reflected in aquarium glass, tiny hikers at the foot of a waterfall. A
   photo is only attached if this visual check passes.
4. The file that leaves this machine is a re-encoded JPEG with no metadata at all.

Anything going wrong (off the home LAN, NAS unmounted, bad model reply) returns None
and the post goes out text-only. An image is a nice-to-have; it must never cost a post.
"""
import base64
import io
import json
import re
import subprocess  # nosec B404 - used only for the macOS sips image converter
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

import anthropic

from config import CLAUDE_MODEL

BLUESKY_MAX_BYTES = 950_000  # Bluesky rejects blobs over 1,000,000 bytes
MAX_EDGE = 2000
MIN_SHORT_EDGE = 1000
CANDIDATE_LIMIT = 25
PRUNE_AFTER_DAYS = 14
MAX_PICKS = 3  # ranked picks to try through the visual check before giving up
TILE_GRID = 3
TILE_OVERLAP = 0.15
TILE_EDGE = 1000

# Only photos taken on these makes. The library also holds saved and downloaded images
# (NASA shots, a watermarked astrophotography print) with no camera EXIF, plus "Meta AI"
# generations and Snow filter selfies. None of those are ours to post.
OWN_CAMERA_MAKES = ["NIKON CORPORATION", "Apple", "DJI"]

# Postgres word-boundary regexes (\m, \M). Over-matching only shrinks the pool, which
# is the right direction to fail in: the vision model often says "a figure" or
# "performers" rather than "person", and the tag vocabulary is just as loose.
PEOPLE_RE = (
    r"\m(person|persons|people|man|men|woman|women|boy|boys|girl|girls|child|children|"
    r"kid|kids|baby|babies|toddler|toddlers|infant|family|portrait|selfie|face|faces|"
    r"crowd|crowds|couple|teen|teens|teenager|teenagers|he|she|his|her|hers|him|someone|"
    r"individual|individuals|guy|guys|lady|ladies|adult|adults|son|daughter|mother|"
    r"father|mom|dad|grandmother|grandfather|human|humans|figure|figures|silhouette|"
    r"hand|hands|finger|fingers|foot|feet|leg|legs|arm|arms|performer|performers|player|"
    r"players|skater|skaters|dancer|dancers|athlete|athletes|audience|spectator|"
    r"spectators|worker|workers|rider|riders|pedestrian|pedestrians|tourist|tourists|"
    r"visitor|visitors|student|students|swimmer|swimmers|wedding|bride|groom|party|"
    r"character|characters|wearing|wears|dressed|clothing|costume|costumes|cosplay|band|"
    r"musician|musicians|singer|singers|ceremony|congregation|poster|posters|collage|"
    r"photographs|selfies|group|team|shadow of)\M"
)
PRIVATE_RE = (
    r"\m(screenshot|screen|screens|document|documents|receipt|text|letter|letters|"
    r"envelope|address|license|plate|passport|card|cards|whiteboard|monitor|display|"
    r"sign|signs|signage|label|labels|handwriting|handwritten|note|notes|paper|papers|"
    r"invoice|menu|house|houses|home|backyard|yard|driveway|neighborhood|porch|mailbox|"
    r"street|residential|bedroom|bathroom|kitchen|living|watermark|watermarked|logo|logos|"
    r"copyright|credit|caption|signature)\M"
)

_QUERY_PROMPT = """\
An article is being promoted on social media with an accompanying photograph pulled
from a personal photo library of nature, animals, travel, food, objects, and scenery.
Photos of people are excluded.

Title: {title}
Description: {description}

List 8-12 concrete things a photograph could show that would suit this article, either
literally or as a clear visual metaphor (e.g. an article on burnout -> "empty bench",
"fog", "candle"). One or two plain words each, mostly single common nouns a photo
description would name.

Respond with a JSON array of strings only.
"""

_CHOOSE_PROMPT = """\
Pick a photograph to accompany a social media post promoting this article.

Title: {title}
Description: {description}

Candidate photos (id: description):
{candidates}

Rules:
- The photo must clearly suit the article's topic or mood. A decorative mismatch is
  worse than no photo, so choose none if nothing genuinely fits.
- Reject any photo that shows or mentions people or body parts, screens, text,
  documents, vehicles with plates, or anything that could identify a home or address.
- Reject any photo that looks like someone else's work: a watermark, logo, credit, or a
  famous image (NASA imagery, nebulae, film stills, artwork).
- Prefer photos described as sharp and well composed.

Respond with JSON only, listing up to 3 suitable photos best first (empty if none fit):
{{"photo_ids": [<id>, ...], "reason": "<one sentence>"}}
"""

_VERIFY_PROMPT = """This photo is about to be posted publicly on social media. Check it strictly.

Reject it if ANY of these are visible, however small, blurred, distant, or partial:
- a person or any part of one: faces, bodies, hands, feet, silhouettes, shadows of
  people, people reflected in glass, water, mirrors, or screens
- readable text, screens, documents, signs, license plates, or house numbers
- a watermark, logo, photographer credit, or signs it is someone else's work
- anything that could identify a private home or its location

Also reject it if it is badly blurred, very dark, or otherwise poor quality.

Respond with JSON only:
{"ok": <true|false>, "reason": "<one sentence>", "alt_text": "<under 250 chars, plain description of what the photo shows>"}
"""


class PostImage(NamedTuple):
    """A prepared, metadata-free photo ready to attach to a post."""
    photo_id: int
    path: Path
    alt_text: str
    width: int
    height: int


def _parse_json(raw: str, opener: str):
    """First JSON value starting with opener. raw_decode stops at the end of that value,
    so prose or a second example after it (which a greedy regex swallowed) is ignored."""
    decoder = json.JSONDecoder()
    start = raw.find(opener)
    while start != -1:
        try:
            return decoder.raw_decode(raw, start)[0]
        except json.JSONDecodeError:
            start = raw.find(opener, start + 1)
    raise RuntimeError(f"Image picker got non-JSON reply: {raw[:200]!r}")


def _ask(client, prompt: str, max_tokens: int) -> str:
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _jpeg_block(img) -> dict:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(buf.getvalue()).decode(),
    }}


def _tiles(img, grid: int) -> list:
    """Overlapping grid sections, each upscaled to ~TILE_EDGE px so small details survive."""
    width, height = img.size
    tile_w, tile_h = width / grid, height / grid
    tiles = []
    for row in range(grid):
        for col in range(grid):
            box = (
                max(0, int((col - TILE_OVERLAP) * tile_w)),
                max(0, int((row - TILE_OVERLAP) * tile_h)),
                min(width, int((col + 1 + TILE_OVERLAP) * tile_w)),
                min(height, int((row + 1 + TILE_OVERLAP) * tile_h)),
            )
            tile = img.crop(box)
            scale = TILE_EDGE / max(tile.size)
            tiles.append(tile.resize((int(tile.width * scale), int(tile.height * scale))))
    return tiles


def verify_image(client, path: Path) -> tuple[bool, str, str]:
    """Look at the prepared file itself. Returns (ok, reason, alt_text).

    The whole image alone isn't enough: the API downscales it, and a waterfall shot
    passed with several people at its base who were each a few pixels tall. Sending
    zoomed grid sections alongside it caught them.
    """
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        content = [{"type": "text", "text": "Full photo:"}, _jpeg_block(img), {
            "type": "text",
            "text": f"The same photo in {TILE_GRID * TILE_GRID} overlapping zoomed sections, "
                    "so small details are visible:",
        }]
        content += [_jpeg_block(tile) for tile in _tiles(img, TILE_GRID)]
    content.append({"type": "text", "text": _VERIFY_PROMPT})

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": content}],
    )
    verdict = _parse_json(response.content[0].text.strip(), "{")
    # Anything but an explicit true is a rejection.
    return verdict.get("ok") is True, str(verdict.get("reason") or ""), str(verdict.get("alt_text") or "").strip()


def build_tsquery(terms: list[str]) -> str:
    """OR of AND-ed words. Letters only, so model output can never inject tsquery syntax.

    A multi-word term also contributes its head noun alone ("flickering candle" ->
    candle): requiring every modifier found nothing at all for whole articles, and
    ts_rank still puts photos matching the full phrase first.
    """
    clauses = []
    for term in terms:
        words = re.findall(r"[a-z]+", str(term).lower())
        if not words:
            continue
        clause = "(" + " & ".join(words) + ")"
        if clause not in clauses:
            clauses.append(clause)
        if len(words) > 1 and f"({words[-1]})" not in clauses:
            clauses.append(f"({words[-1]})")
    return " | ".join(clauses)


_SEARCH_SQL = """
WITH q AS (SELECT to_tsquery('english', %(tsq)s) AS tsq)
SELECT p.id, p.nas_path, a.description,
       ts_rank(to_tsvector('english', a.description), q.tsq)
       + 0.2 * (SELECT count(*) FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
                WHERE pt.photo_id = p.id AND t.name = ANY(%(tags)s)) AS score
FROM photos p
JOIN ai_analysis a ON a.photo_id = p.id
CROSS JOIN q
WHERE a.description IS NOT NULL
  AND LEAST(p.width, p.height) >= %(min_edge)s
  AND p.camera_make = ANY(%(makes)s)
  AND NOT (p.id = ANY(%(exclude)s))
  AND (to_tsvector('english', a.description) @@ q.tsq
       OR EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
                  WHERE pt.photo_id = p.id AND t.name = ANY(%(tags)s)))
  AND a.description !~* %(people)s
  AND a.description !~* %(private)s
  AND NOT EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
                  WHERE pt.photo_id = p.id AND (t.name ~* %(people)s OR t.name ~* %(private)s))
ORDER BY score DESC
LIMIT %(limit)s
"""


def search_candidates(photo_db_url: str, terms: list[str], exclude_ids: set[int]) -> list[dict]:
    import psycopg

    tsq = build_tsquery(terms)
    if not tsq:
        return []
    tags = sorted({str(t).strip().lower() for t in terms if str(t).strip()})
    params = {
        "tsq": tsq,
        "tags": tags,
        "min_edge": MIN_SHORT_EDGE,
        "makes": OWN_CAMERA_MAKES,
        "exclude": sorted(exclude_ids),
        "people": PEOPLE_RE,
        "private": PRIVATE_RE,
        "limit": CANDIDATE_LIMIT,
    }
    # The DB is misfit on the home LAN; fail fast when away rather than stall the run.
    with psycopg.connect(photo_db_url, connect_timeout=5) as pg:
        rows = pg.execute(_SEARCH_SQL, params).fetchall()
    return [{"id": r[0], "nas_path": r[1], "description": r[2]} for r in rows]


def prepare_file(src: Path, dest: Path) -> tuple[int, int]:
    """Write a metadata-free JPEG under Bluesky's size limit. Returns (width, height).

    sips decodes everything in the library (DNG, HEIC, JPEG) natively. Pillow then
    re-encodes without passing exif=, which drops every tag, GPS included.
    """
    from PIL import Image, ImageOps

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        decoded = Path(tmp) / "decoded.jpg"
        subprocess.run(  # nosec B603
            ["/usr/bin/sips", "-s", "format", "jpeg", "-Z", str(MAX_EDGE), str(src), "--out", str(decoded)],
            check=True, capture_output=True, timeout=120,
        )
        with Image.open(decoded) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((MAX_EDGE, MAX_EDGE))
            for quality in (88, 80, 72, 64):
                img.save(dest, "JPEG", quality=quality, optimize=True)
                if dest.stat().st_size <= BLUESKY_MAX_BYTES:
                    break
            else:
                img.thumbnail((MAX_EDGE // 2, MAX_EDGE // 2))
                img.save(dest, "JPEG", quality=80, optimize=True)
            return img.size


def _prune(image_dir: Path) -> None:
    cutoff = time.time() - PRUNE_AFTER_DAYS * 86400
    for old in image_dir.glob("*.jpg"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)


def find_image(content: dict, config: dict, exclude_ids: set[int]) -> PostImage | None:
    """Best-effort photo for this article, or None. Never raises."""
    photo_db_url = config.get("photo_db_url")
    if not photo_db_url:
        return None
    try:
        return _find_image(content, config, photo_db_url, exclude_ids)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"WARNING: no image attached ({type(exc).__name__}: {exc})", file=sys.stderr)
        return None


def _find_image(content: dict, config: dict, photo_db_url: str, exclude_ids: set[int]) -> PostImage | None:
    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
    description = (content.get("description") or "")[:1500]

    terms = _parse_json(_ask(client, _QUERY_PROMPT.format(title=content["title"], description=description), 300),
                        "[")
    if not isinstance(terms, list):
        raise RuntimeError(f"expected a list of search terms, got {terms!r}")

    candidates = search_candidates(photo_db_url, terms, exclude_ids)
    if not candidates:
        print(f"No candidate photos for: {', '.join(map(str, terms))}")
        return None

    listing = "\n".join(f"{c['id']}: {c['description'][:300]}" for c in candidates)
    choice = _parse_json(
        _ask(client, _CHOOSE_PROMPT.format(title=content["title"], description=description, candidates=listing), 400),
        "{",
    )
    by_id = {c["id"]: c for c in candidates}
    ranked = choice.get("photo_ids") if isinstance(choice, dict) else None
    if not isinstance(ranked, list) or not ranked:
        print(f"No photo fit well enough: {choice.get('reason', '') if isinstance(choice, dict) else ''}")
        return None

    image_dir = Path(config.get("image_dir", "images"))
    image_dir.mkdir(parents=True, exist_ok=True)
    _prune(image_dir)

    for raw_id in ranked[:MAX_PICKS]:
        try:
            photo_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if photo_id not in by_id:
            print(f"WARNING: picker chose photo {photo_id}, which was not a candidate", file=sys.stderr)
            continue

        src = Path(by_id[photo_id]["nas_path"])
        if not src.exists():
            raise RuntimeError(f"{src} not found (is the NAS mounted?)")
        dest = image_dir / f"{photo_id}.jpg"
        width, height = prepare_file(src, dest)

        ok, reason, alt_text = verify_image(client, dest)
        if not ok:
            print(f"Rejected photo #{photo_id} on visual check: {reason}")
            dest.unlink(missing_ok=True)
            continue

        alt_text = alt_text or by_id[photo_id]["description"]
        return PostImage(photo_id=photo_id, path=dest.resolve(), alt_text=alt_text[:1000], width=width, height=height)

    print("No photo passed the visual check.")
    return None
