import sqlite3

import pytest

from db import init_db, get_conn, upsert_content, insert_post_record
from health import run_health_checks
from tests.fixtures import ARTICLE_BODY

ARTICLE = "https://billfordx.medium.com/an-article-155c27d86f7f?source=rss-x"


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def _add_article(conn, content_id=ARTICLE, content_type="business"):
    upsert_content(conn, {
        "id": content_id,
        "source": "medium",
        "title": "An Article",
        "url": content_id,
        "published_date": "2024-01-01T00:00:00+00:00",
        "description": ARTICLE_BODY,
        "tags": [],
        "content_type": content_type,
        "full_content_fetched": 1,
    })


def test_healthy_db_reports_nothing(db_path):
    with get_conn(db_path) as conn:
        _add_article(conn)
        assert run_health_checks(conn) == ([], [])


def test_detects_cooldown_violation(db_path):
    """The original symptom: same article, same platform, twice inside the cooldown."""
    with get_conn(db_path) as conn:
        _add_article(conn)
        for _ in range(2):
            insert_post_record(conn, "medium:155c27d86f7f", "linkedin", "text")
        problems = run_health_checks(conn)

    assert any("cooldown" in p for p in problems[0])


def test_detects_unclassified_backlog(db_path):
    with get_conn(db_path) as conn:
        _add_article(conn, content_type=None)
        assert any("unclassified" in p for p in run_health_checks(conn)[1])  # warning


def test_detects_orphaned_history(db_path):
    with get_conn(db_path, enforce_fk=False) as conn:
        _add_article(conn)
        insert_post_record(conn, "medium:does-not-exist", "linkedin", "text")
        assert any("no matching article" in p for p in run_health_checks(conn)[0])  # error


def test_foreign_keys_block_orphan_inserts(db_path):
    """FK enforcement is what stops orphans accumulating in the first place."""
    with get_conn(db_path) as conn:
        _add_article(conn)
        with pytest.raises(sqlite3.IntegrityError):
            insert_post_record(conn, "medium:does-not-exist", "linkedin", "text")


def test_cooldown_check_ignores_pre_baseline_history(db_path):
    """Known-bad history from before the fix must not fire forever."""
    with get_conn(db_path) as conn:
        _add_article(conn)
        conn.execute(
            "UPDATE schema_meta SET value = '2999-01-01T00:00:00+00:00' WHERE key = 'health_baseline'"
        )
        for _ in range(2):
            insert_post_record(conn, "medium:155c27d86f7f", "linkedin", "text")
        assert not any("cooldown" in p for p in run_health_checks(conn)[0])


def test_orphan_history_repair_reattaches_by_slug(db_path):
    """A garbled id whose slug uniquely identifies one article should be recovered."""
    with get_conn(db_path, enforce_fk=False) as conn:
        _add_article(conn)
        insert_post_record(conn, "https://medium.com/@billfordx/an-article-garbled", "linkedin", "t")
        conn.execute("DELETE FROM schema_meta WHERE key = 'orphan_history_repair_v2'")

    # Re-run init to trigger the orphan-history repair migration.
    init_db(db_path)

    with get_conn(db_path) as conn:
        assert run_health_checks(conn) == ([], [])
        row = conn.execute("SELECT content_id FROM post_history").fetchone()
        assert row["content_id"] == "medium:155c27d86f7f"


def test_stalled_fetch_is_a_warning_not_an_error(db_path):
    """One unscrapeable article must not fail the run and trigger five retries."""
    with get_conn(db_path) as conn:
        _add_article(conn)
        conn.execute("UPDATE content SET full_content_fetched = 0, fetch_attempts = 99")
        errors, warnings = run_health_checks(conn)
    assert errors == []
    assert any("fetch attempts" in w for w in warnings)
