import sqlite3
import json
import re
from datetime import datetime, timezone
from contextlib import contextmanager
from zoneinfo import ZoneInfo


_YOUTUBE_URL_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})")
_YOUTUBE_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_MEDIUM_HASH_RE = re.compile(r"-([0-9a-f]{12})$")


def canonical_content_id(raw_id: str, source: str | None = None) -> str:
    """Stable identity for one piece of content, independent of URL variant.

    Medium serves the same article under several hosts — billfordx.medium.com,
    medium.com/@billfordx, publication paths, and custom domains — with or
    without a ?source=rss- tracking param. Keying content on the raw feed link
    therefore created several rows per article, and every cooldown and
    repeat-suppression check silently treated them as different articles.
    The trailing hex hash in the slug is the only stable part, so key on that.
    """
    value = (raw_id or "").strip()
    if not value or value.startswith(("medium:", "youtube:")):
        return value

    match = _YOUTUBE_URL_RE.search(value)
    if match:
        return f"youtube:{match.group(1)}"
    if source == "youtube" or ("/" not in value and _YOUTUBE_ID_RE.fullmatch(value)):
        return f"youtube:{value}"

    path = value.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    match = _MEDIUM_HASH_RE.search(path.rsplit("/", 1)[-1])
    if match:
        return f"medium:{match.group(1)}"
    return path


_HEADLINE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")

# Long replies whose length puts them past the article threshold but which are plainly
# responses. Identified by inspection; the heuristic below cannot separate these from a
# short article without also discarding real ones.
_KNOWN_RESPONSE_IDS = frozenset({
    "medium:07c1ad754782",
    "medium:6729b3dd474e",
    "medium:2a9a3267e34f",
    "medium:191cb478e2fe",
})

ARTICLE_LENGTH_FLOOR = 800
STUB_LENGTH_CEILING = 60


def _looks_like_headline(title: str) -> bool:
    """True if the title reads as a headline rather than the opening of a sentence."""
    text = (title or "").strip()
    if not text or text[-1] in ".!" or ". " in text:
        return False  # '?' is common in real headlines; '.' is not
    words = _HEADLINE_WORD_RE.findall(text)
    if not 3 <= len(words) <= 14:
        return False
    return sum(1 for w in words if w[0].isupper()) / len(words) >= 0.6


def classify_content_kind(content_id: str, title: str, description: str | None) -> str:
    """'article' or 'response'.

    A Medium export bundles the author's replies alongside their posts, and the archive
    importer took everything with a canonical URL, so ~200 two-sentence comments sat in
    the promotion pool as if they were articles. Medium gives a reply no title, so the
    importer fell back to the page <title>, which is just the reply's opening words --
    that is the signal used here. Tuned to never discard a real article: a handful of
    long replies stay classified as articles rather than risk a false positive.
    """
    if content_id in _KNOWN_RESPONSE_IDS:
        return "response"
    length = len(description or "")
    if length >= ARTICLE_LENGTH_FLOOR:
        return "article"
    if length < STUB_LENGTH_CEILING:
        return "response"
    return "article" if _looks_like_headline(title) else "response"


SCHEMA = """
CREATE TABLE IF NOT EXISTS content (
    id TEXT PRIMARY KEY,
    source TEXT,
    title TEXT,
    url TEXT,
    published_date TEXT,
    description TEXT,
    tags TEXT,
    fetched_at TEXT,
    content_type TEXT
);

CREATE TABLE IF NOT EXISTS post_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id TEXT,
    platform TEXT,
    posted_at TEXT,
    post_text TEXT,
    publora_post_id TEXT,
    scheduled_for TEXT,
    dry_run INTEGER DEFAULT 0,
    FOREIGN KEY (content_id) REFERENCES content(id)
);

CREATE TABLE IF NOT EXISTS pending_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publora_post_id TEXT NOT NULL,
    platform_account_id TEXT NOT NULL,
    content_url TEXT NOT NULL,
    content_title TEXT,
    fires_at TEXT NOT NULL,
    done INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _best_url(urls: list[str]) -> str:
    """Prefer a medium.com host, then the shortest (drops ?source= tracking params)."""
    candidates = [u for u in urls if u] or [""]
    return min(candidates, key=lambda u: (0 if "medium.com" in u else 1, len(u)))


def _merge_duplicate_content(conn: sqlite3.Connection) -> int:
    """Collapse rows that are the same article under different URLs onto one canonical id."""
    groups: dict[str, list[dict]] = {}
    for row in conn.execute("SELECT * FROM content").fetchall():
        item = dict(row)
        groups.setdefault(canonical_content_id(item["id"], item.get("source")), []).append(item)

    merged = 0
    for canonical, rows in groups.items():
        if len(rows) == 1 and rows[0]["id"] == canonical:
            continue
        if len(rows) > 1:
            merged += len(rows) - 1

        def _first(field, rows=rows):
            return next((r[field] for r in rows if r.get(field)), None)

        best = {
            "id": canonical,
            "source": _first("source"),
            "title": _first("title"),
            "url": _best_url([r.get("url") or r["id"] for r in rows]),
            "published_date": _first("published_date"),
            "description": max((r.get("description") or "" for r in rows), key=len),
            "tags": max((r.get("tags") or "[]" for r in rows), key=len),
            "fetched_at": max((r.get("fetched_at") or "" for r in rows)),
            "content_type": _first("content_type"),
            "full_content_fetched": max((r.get("full_content_fetched") or 0) for r in rows),
        }

        conn.executemany("DELETE FROM content WHERE id = ?", [(r["id"],) for r in rows])
        conn.execute(
            """
            INSERT INTO content
                (id, source, title, url, published_date, description, tags, fetched_at,
                 content_type, full_content_fetched)
            VALUES
                (:id, :source, :title, :url, :published_date, :description, :tags, :fetched_at,
                 :content_type, :full_content_fetched)
            """,
            best,
        )
    return merged


def _migration_applied(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM schema_meta WHERE key = ?", (key,)).fetchone() is not None


def _mark_migration(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        (key, datetime.now(timezone.utc).isoformat()),
    )


def _slug_stem(content_id: str) -> str:
    """Slug with any trailing hash/suffix token removed, for fuzzy orphan matching."""
    path = content_id.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return path.rsplit("/", 1)[-1].rsplit("-", 1)[0]


def _migrate_orphan_history(conn: sqlite3.Connection) -> None:
    """Reattach post_history rows whose content_id matches no article.

    A garbled id (see the scorer's id handling) makes an article invisible to the
    cooldown check forever. Where the slug stem identifies exactly one article we
    can recover the link; anything ambiguous is left for the health check to report.
    """
    if _migration_applied(conn, "orphan_history_repair_v2"):
        return

    # Match on the stored url, not the id: ids are canonical ("medium:<hash>") and no
    # longer carry the slug the orphaned history row can be recognised by.
    stems: dict[str, list[str]] = {}
    for row in conn.execute("SELECT id, url FROM content"):
        stems.setdefault(_slug_stem(row["url"] or row["id"]), []).append(row["id"])

    repaired = 0
    orphans = conn.execute(
        "SELECT DISTINCT content_id FROM post_history WHERE content_id NOT IN (SELECT id FROM content)"
    ).fetchall()
    for row in orphans:
        matches = stems.get(_slug_stem(row["content_id"]), [])
        if len(matches) == 1:  # unique match only — never guess between candidates
            conn.execute(
                "UPDATE post_history SET content_id = ? WHERE content_id = ?",
                (matches[0], row["content_id"]),
            )
            repaired += 1

    _mark_migration(conn, "orphan_history_repair_v2")
    if orphans:
        print(
            f"Migration: reattached {repaired} of {len(orphans)} orphaned post-history id(s)."
        )


def _migrate_canonical_ids(conn: sqlite3.Connection) -> None:
    """One-time remap of content ids and post history onto canonical article ids."""
    if _migration_applied(conn, "canonical_ids"):
        return

    merged = _merge_duplicate_content(conn)

    remapped = 0
    for row in conn.execute("SELECT DISTINCT content_id FROM post_history").fetchall():
        old = row["content_id"]
        new = canonical_content_id(old)
        if new != old:
            conn.execute(
                "UPDATE post_history SET content_id = ? WHERE content_id = ?", (new, old)
            )
            remapped += 1

    _mark_migration(conn, "canonical_ids")
    print(
        f"Migration: merged {merged} duplicate content row(s), "
        f"remapped {remapped} post-history id(s) onto canonical article ids."
    )


@contextmanager
def get_conn(db_path: str, enforce_fk: bool = True):
    """Open a connection. Foreign keys are OFF by default in SQLite, which left
    post_history.content_id -> content.id unchecked and let orphan rows pile up
    silently; enable them so a bad content_id fails at insert time instead."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if enforce_fk:
        conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    # Migrations rewrite content ids and delete/reinsert rows that post_history
    # points at, so they must run with FK enforcement off.
    with get_conn(db_path, enforce_fk=False) as conn:
        conn.executescript(SCHEMA)
        for alter in [
            "ALTER TABLE post_history ADD COLUMN scheduled_for TEXT",
            "ALTER TABLE content ADD COLUMN content_type TEXT",
            "ALTER TABLE content ADD COLUMN full_content_fetched INTEGER DEFAULT 0",
            "ALTER TABLE content ADD COLUMN fetch_attempts INTEGER DEFAULT 0",
            "ALTER TABLE content ADD COLUMN content_kind TEXT DEFAULT 'article'",
        ]:
            try:
                conn.execute(alter)
            except sqlite3.OperationalError:
                pass  # column already exists
        _migrate_canonical_ids(conn)
        _migrate_orphan_history(conn)
        _migrate_retry_thin_descriptions(conn)
        _migrate_content_kind(conn)
        _ensure_health_baseline(conn)


def _migrate_content_kind(conn: sqlite3.Connection) -> None:
    """Label existing rows as article or response so replies stop being promoted."""
    if _migration_applied(conn, "content_kind"):
        return

    responses = 0
    for row in conn.execute("SELECT id, title, description FROM content").fetchall():
        kind = classify_content_kind(row["id"], row["title"], row["description"])
        conn.execute("UPDATE content SET content_kind = ? WHERE id = ?", (kind, row["id"]))
        responses += kind == "response"

    _mark_migration(conn, "content_kind")
    if responses:
        print(f"Migration: classified {responses} catalog row(s) as Medium responses (excluded from promotion).")


THIN_DESCRIPTION_CHARS = 400


def _migrate_retry_thin_descriptions(conn: sqlite3.Connection) -> None:
    """Re-open articles the old backfill marked done after a failed fetch.

    Failures used to set full_content_fetched = 1 permanently, so a rate-limited
    fetch locked an article to whatever stub the archive importer captured — some
    posts were being promoted from a 90-character description.
    """
    if _migration_applied(conn, "retry_thin_descriptions"):
        return

    cursor = conn.execute(
        """
        UPDATE content SET full_content_fetched = 0, fetch_attempts = 0
        WHERE full_content_fetched = 1
          AND length(COALESCE(description, '')) < ?
        """,
        (THIN_DESCRIPTION_CHARS,),
    )
    _mark_migration(conn, "retry_thin_descriptions")
    if cursor.rowcount:
        print(f"Migration: re-queued {cursor.rowcount} thin article(s) for content backfill.")


HEALTH_BASELINE_KEY = "health_baseline"


def _ensure_health_baseline(conn: sqlite3.Connection) -> None:
    """Stamp the moment repeat-suppression was fixed.

    History written before this point contains known cooldown violations. Without a
    baseline the health check would report them on every run for 90 days, which is
    the alarm fatigue that let the earlier failures go unnoticed.
    """
    if not _migration_applied(conn, HEALTH_BASELINE_KEY):
        _mark_migration(conn, HEALTH_BASELINE_KEY)


def get_health_baseline(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (HEALTH_BASELINE_KEY,)
    ).fetchone()
    return row["value"] if row else "1970-01-01T00:00:00+00:00"


def upsert_content(conn: sqlite3.Connection, item: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    canonical = canonical_content_id(item["id"], item.get("source"))
    params = {
        **item,
        "id": canonical,
        "content_kind": item.get("content_kind") or classify_content_kind(
            canonical, item.get("title", ""), item.get("description", "")
        ),
        "tags": json.dumps(item.get("tags", [])),
        "fetched_at": now,
        "content_type": item.get("content_type"),
        "full_content_fetched": item.get("full_content_fetched", 0),
    }
    conn.execute(
        """
        INSERT INTO content
            (id, source, title, url, published_date, description, tags, fetched_at,
             content_type, full_content_fetched, content_kind)
        VALUES
            (:id, :source, :title, :url, :published_date, :description, :tags, :fetched_at,
             :content_type, :full_content_fetched, :content_kind)
        ON CONFLICT(id) DO UPDATE SET
            description = CASE
                WHEN length(excluded.description) > length(content.description)
                THEN excluded.description ELSE content.description END,
            full_content_fetched = CASE
                WHEN length(excluded.description) > length(content.description)
                THEN excluded.full_content_fetched ELSE content.full_content_fetched END,
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE content.title END,
            content_kind = excluded.content_kind,
            fetched_at = excluded.fetched_at
        """,
        params,
    )


MAX_FETCH_ATTEMPTS = 3


def get_content_needing_fetch(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """Articles whose full content has never been fetched and still have retries left.

    Failures are retried across runs rather than marked done on the first miss: a
    single network timeout used to lock an article to its RSS snippet permanently.
    """
    rows = conn.execute(
        """
        SELECT id, url FROM content
        WHERE source = 'medium'
          AND COALESCE(content_kind, 'article') = 'article'
          AND full_content_fetched = 0
          AND COALESCE(fetch_attempts, 0) < ?
        ORDER BY published_date DESC
        LIMIT ?
        """,
        (MAX_FETCH_ATTEMPTS, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_unclassified_content(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT id, title, description FROM content
        WHERE content_type IS NULL AND COALESCE(content_kind, 'article') = 'article'
        ORDER BY published_date DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_content(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM content ORDER BY published_date DESC").fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["tags"] = json.loads(d["tags"] or "[]")
        result.append(d)
    return result


def get_eligible_content(conn: sqlite3.Connection, platform: str, cooldown_days: int) -> list[dict]:
    """Content not posted to platform within cooldown window."""
    rows = conn.execute(
        """
        SELECT c.*, MAX(ph.posted_at) AS last_posted
        FROM content c
        LEFT JOIN post_history ph
            ON ph.content_id = c.id
            AND ph.platform = ?
            AND ph.dry_run = 0
            AND ph.posted_at >= datetime('now', ? || ' days')
        WHERE COALESCE(c.content_kind, 'article') = 'article'
        GROUP BY c.id
        HAVING last_posted IS NULL
        ORDER BY c.published_date DESC
        """,
        (platform, f"-{cooldown_days}"),
    ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        d["tags"] = json.loads(d["tags"] or "[]")
        result.append(d)
    return result


def get_oldest_content_by_platform(
    conn: sqlite3.Connection, platform: str, limit: int = 60
) -> list[dict]:
    """Fallback: content ordered by last-posted date ascending (oldest first).

    Deliberately over-fetches so the caller can drop recently-selected items and
    still have a usable pool.
    """
    rows = conn.execute(
        """
        SELECT c.*, MAX(ph.posted_at) AS last_posted
        FROM content c
        LEFT JOIN post_history ph
            ON ph.content_id = c.id AND ph.platform = ? AND ph.dry_run = 0
        WHERE COALESCE(c.content_kind, 'article') = 'article'
        GROUP BY c.id
        ORDER BY last_posted ASC NULLS FIRST
        LIMIT ?
        """,
        (platform, limit),
    ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        d["tags"] = json.loads(d["tags"] or "[]")
        result.append(d)
    return result


def get_recent_post_history(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    rows = conn.execute(
        """
        SELECT ph.*, c.title, c.source
        FROM post_history ph
        JOIN content c ON c.id = ph.content_id
        WHERE ph.dry_run = 0
          AND ph.posted_at >= datetime('now', ? || ' days')
        ORDER BY ph.posted_at DESC
        """,
        (f"-{days}",),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recently_selected_ids(conn: sqlite3.Connection, days: int = 7) -> set[str]:
    """Content IDs picked recently — including dry runs — to prevent repeat selections."""
    rows = conn.execute(
        """
        SELECT DISTINCT content_id FROM post_history
        WHERE posted_at >= datetime('now', ? || ' days')
        """,
        (f"-{days}",),
    ).fetchall()
    return {r["content_id"] for r in rows}


def get_latest_scheduled_for(conn: sqlite3.Connection, platform: str) -> str | None:
    """Return the latest scheduled_for timestamp for the given platform (non-dry-run only)."""
    row = conn.execute(
        """
        SELECT MAX(scheduled_for) AS latest FROM post_history
        WHERE platform = ? AND dry_run = 0 AND scheduled_for IS NOT NULL
        """,
        (platform,),
    ).fetchone()
    return row["latest"] if row else None


def insert_pending_comment(
    conn: sqlite3.Connection,
    publora_post_id: str,
    platform_account_id: str,
    content_url: str,
    content_title: str | None,
    fires_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO pending_comments
            (publora_post_id, platform_account_id, content_url, content_title, fires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (publora_post_id, platform_account_id, content_url, content_title, fires_at,
         datetime.now(timezone.utc).isoformat()),
    )


def get_due_pending_comments(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM pending_comments WHERE done = 0 AND datetime(fires_at) <= datetime('now')"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_comment_done(conn: sqlite3.Connection, comment_id: int) -> None:
    conn.execute("UPDATE pending_comments SET done = 1 WHERE id = ?", (comment_id,))


def insert_post_record(
    conn: sqlite3.Connection,
    content_id: str,
    platform: str,
    post_text: str,
    publora_post_id: str | None = None,
    scheduled_for: str | None = None,
    dry_run: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO post_history
            (content_id, platform, posted_at, post_text, publora_post_id, scheduled_for, dry_run)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            content_id,
            platform,
            datetime.now(timezone.utc).isoformat(),
            post_text,
            publora_post_id,
            scheduled_for,
            1 if dry_run else 0,
        ),
    )


def has_posted_today(conn: sqlite3.Connection, platform: str, tz: ZoneInfo) -> bool:
    """True if a real (non-dry-run) post already went out for this platform today.

    run_with_retry.sh re-runs the whole invocation when main.py exits non-zero, and
    main.py exits non-zero when *any* platform fails. Without this check, one failing
    platform makes the retry repost every platform that already succeeded.
    Day boundaries are local, matching the once-a-day local posting schedule.
    """
    day_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        """
        SELECT 1 FROM post_history
        WHERE platform = ? AND dry_run = 0 AND posted_at >= ?
        LIMIT 1
        """,
        (platform, day_start.astimezone(timezone.utc).isoformat()),
    ).fetchone()
    return row is not None
