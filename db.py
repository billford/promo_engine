import sqlite3
import json
from datetime import datetime, timezone
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS content (
    id TEXT PRIMARY KEY,
    source TEXT,
    title TEXT,
    url TEXT,
    published_date TEXT,
    description TEXT,
    tags TEXT,
    fetched_at TEXT
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
"""


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
        try:
            conn.execute("ALTER TABLE post_history ADD COLUMN scheduled_for TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists


def upsert_content(conn: sqlite3.Connection, item: dict) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO content
            (id, source, title, url, published_date, description, tags, fetched_at)
        VALUES
            (:id, :source, :title, :url, :published_date, :description, :tags, :fetched_at)
        """,
        {
            **item,
            "tags": json.dumps(item.get("tags", [])),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    )


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


def get_oldest_content_by_platform(conn: sqlite3.Connection, platform: str) -> list[dict]:
    """Fallback: content ordered by last-posted date ascending (oldest first)."""
    rows = conn.execute(
        """
        SELECT c.*, MAX(ph.posted_at) AS last_posted
        FROM content c
        LEFT JOIN post_history ph
            ON ph.content_id = c.id AND ph.platform = ? AND ph.dry_run = 0
        GROUP BY c.id
        ORDER BY last_posted ASC NULLS FIRST
        LIMIT 20
        """,
        (platform,),
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
