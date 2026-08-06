"""Post-run invariant checks.

Every bug this project has hit failed silently: the classifier reported "0 items
classified" for months, duplicate URL variants looked like distinct articles, and
orphaned history rows accumulated against a foreign key that was never enforced.
These checks assert the invariants directly so a broken run exits non-zero instead
of looking normal.
"""
import sqlite3

from config import COOLDOWN_DAYS
from db import MAX_FETCH_ATTEMPTS, canonical_content_id, get_health_baseline


def _orphaned_history(conn: sqlite3.Connection) -> str | None:
    n = conn.execute(
        "SELECT COUNT(*) FROM post_history WHERE content_id NOT IN (SELECT id FROM content)"
    ).fetchone()[0]
    if n:
        return f"{n} post_history row(s) reference a content_id with no matching article"
    return None


def _non_canonical_ids(conn: sqlite3.Connection) -> str | None:
    bad = [
        row["id"]
        for row in conn.execute("SELECT id, source FROM content")
        if canonical_content_id(row["id"], row["source"]) != row["id"]
    ]
    if bad:
        return f"{len(bad)} content row(s) are not canonical, e.g. {bad[0]!r} — duplicates will reappear"
    return None


def _cooldown_violations(conn: sqlite3.Connection) -> str | None:
    """The original symptom: one article hitting a platform twice inside the cooldown.

    Scoped to history written after the fix — pre-baseline rows are known-bad and
    would otherwise fire on every run until they aged out.
    """
    rows = conn.execute(
        """
        SELECT a.content_id, a.platform, COUNT(*) AS n
        FROM post_history a
        JOIN post_history b
          ON a.content_id = b.content_id AND a.platform = b.platform AND a.id < b.id
         AND b.posted_at < datetime(a.posted_at, ? || ' days')
        WHERE a.dry_run = 0 AND b.dry_run = 0
          AND a.posted_at >= ?
        GROUP BY a.content_id, a.platform
        """,
        (f"+{COOLDOWN_DAYS}", get_health_baseline(conn)),
    ).fetchall()
    if rows:
        worst = max(rows, key=lambda r: r["n"])
        return (
            f"{len(rows)} article/platform pair(s) posted twice within the "
            f"{COOLDOWN_DAYS}-day cooldown in the last 90 days, e.g. "
            f"{worst['content_id']} on {worst['platform']}"
        )
    return None


def _unclassified_backlog(conn: sqlite3.Connection) -> str | None:
    n = conn.execute("SELECT COUNT(*) FROM content WHERE content_type IS NULL").fetchone()[0]
    if n:
        return f"{n} article(s) still unclassified — platform content-type routing is degraded"
    return None


def _stalled_fetches(conn: sqlite3.Connection) -> str | None:
    n = conn.execute(
        "SELECT COUNT(*) FROM content WHERE full_content_fetched = 0 AND COALESCE(fetch_attempts, 0) >= ?",
        (MAX_FETCH_ATTEMPTS,),
    ).fetchone()[0]
    if n:
        return f"{n} article(s) exhausted {MAX_FETCH_ATTEMPTS} fetch attempts and will post from the RSS snippet only"
    return None


def _stuck_comments(conn: sqlite3.Connection) -> str | None:
    n = conn.execute(
        "SELECT COUNT(*) FROM pending_comments WHERE done = 0 AND fires_at < datetime('now', '-48 hours')"
    ).fetchone()[0]
    if n:
        return f"{n} pending LinkedIn comment(s) are more than 48h overdue"
    return None


CHECKS = (
    _orphaned_history,
    _non_canonical_ids,
    _cooldown_violations,
    _unclassified_backlog,
    _stalled_fetches,
    _stuck_comments,
)


def run_health_checks(conn: sqlite3.Connection) -> list[str]:
    """Return a list of human-readable problems. Empty means healthy."""
    return [problem for check in CHECKS if (problem := check(conn))]
