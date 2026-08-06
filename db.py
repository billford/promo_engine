import sqlite3
import json
import re
from datetime import datetime, timezone
from contextlib import contextmanager


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


def _migrate_canonical_ids(conn: sqlite3.Connection) -> None:
    """One-time remap of content ids and post history onto canonical article ids."""
    applied = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'canonical_ids'"
    ).fetchone()
    if applied:
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

    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('canonical_ids', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    print(
        f"Migration: merged {merged} duplicate content row(s), "
        f"remapped {remapped} post-history id(s) onto canonical article ids."
    )


@contextmanager
def get_conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        for alter in [
            "ALTER TABLE post_history ADD COLUMN scheduled_for TEXT",
            "ALTER TABLE content ADD COLUMN content_type TEXT",
            "ALTER TABLE content ADD COLUMN full_content_fetched INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(alter)
            except sqlite3.OperationalError:
                pass  # column already exists
        _migrate_canonical_ids(conn)


def upsert_content(conn: sqlite3.Connection, item: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    params = {
        **item,
        "id": canonical_content_id(item["id"], item.get("source")),
        "tags": json.dumps(item.get("tags", [])),
        "fetched_at": now,
        "content_type": item.get("content_type"),
        "full_content_fetched": item.get("full_content_fetched", 0),
    }
    conn.execute(
        """
        INSERT INTO content
            (id, source, title, url, published_date, description, tags, fetched_at,
             content_type, full_content_fetched)
        VALUES
            (:id, :source, :title, :url, :published_date, :description, :tags, :fetched_at,
             :content_type, :full_content_fetched)
        ON CONFLICT(id) DO UPDATE SET
            description = CASE
                WHEN length(excluded.description) > length(content.description)
                THEN excluded.description ELSE content.description END,
            full_content_fetched = CASE
                WHEN length(excluded.description) > length(content.description)
                THEN excluded.full_content_fetched ELSE content.full_content_fetched END,
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE content.title END,
            fetched_at = excluded.fetched_at
        """,
        params,
    )


def get_content_needing_fetch(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """Articles whose full content has never been fetched."""
    rows = conn.execute(
        """
        SELECT id, url FROM content
        WHERE source = 'medium'
          AND full_content_fetched = 0
        ORDER BY published_date DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_unclassified_content(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, description FROM content WHERE content_type IS NULL ORDER BY published_date DESC"
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
