import pytest
from db import (
    init_db,
    get_conn,
    upsert_content,
    get_all_content,
    get_eligible_content,
    get_unclassified_content,
    get_recent_post_history,
    get_recently_selected_ids,
    get_latest_scheduled_for,
    insert_pending_comment,
    get_due_pending_comments,
    mark_comment_done,
    insert_post_record,
)


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def _sample_content(content_id="http://example.com/1", source="medium", content_type=None):
    return {
        "id": content_id,
        "source": source,
        "title": "Test Article",
        "url": content_id,
        "published_date": "2024-01-01T00:00:00+00:00",
        "description": "A test article description.",
        "tags": ["tech", "ai"],
        "content_type": content_type,
    }


def test_init_db_creates_tables(db_path):
    with get_conn(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "content" in tables
    assert "post_history" in tables
    assert "pending_comments" in tables


def test_upsert_content_inserts(db_path):
    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_content())
        rows = get_all_content(conn)
    assert len(rows) == 1
    assert rows[0]["title"] == "Test Article"
    assert rows[0]["tags"] == ["tech", "ai"]


def test_upsert_content_ignores_duplicate(db_path):
    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_content())
        upsert_content(conn, _sample_content())
        rows = get_all_content(conn)
    assert len(rows) == 1


def test_get_eligible_content_returns_unposted(db_path):
    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_content())
        rows = get_eligible_content(conn, "linkedin", cooldown_days=30)
    assert len(rows) == 1


def test_get_eligible_content_excludes_recent(db_path):
    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_content())
        insert_post_record(conn, "http://example.com/1", "linkedin", "post text")
        rows = get_eligible_content(conn, "linkedin", cooldown_days=30)
    assert len(rows) == 0


def test_get_eligible_content_different_platform(db_path):
    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_content())
        insert_post_record(conn, "http://example.com/1", "linkedin", "post text")
        rows = get_eligible_content(conn, "bluesky", cooldown_days=30)
    assert len(rows) == 1


def test_get_unclassified_content(db_path):
    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_content("http://example.com/1", content_type=None))
        upsert_content(conn, _sample_content("http://example.com/2", content_type="business"))
        rows = get_unclassified_content(conn)
    assert len(rows) == 1
    assert rows[0]["id"] == "http://example.com/1"


def test_get_recent_post_history(db_path):
    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_content())
        insert_post_record(conn, "http://example.com/1", "linkedin", "post text")
        history = get_recent_post_history(conn, days=7)
    assert len(history) == 1
    assert history[0]["platform"] == "linkedin"
    assert history[0]["title"] == "Test Article"


def test_get_recently_selected_ids(db_path):
    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_content())
        insert_post_record(conn, "http://example.com/1", "linkedin", "post text")
        ids = get_recently_selected_ids(conn, days=7)
    assert "http://example.com/1" in ids


def test_get_latest_scheduled_for_none(db_path):
    with get_conn(db_path) as conn:
        result = get_latest_scheduled_for(conn, "linkedin")
    assert result is None


def test_get_latest_scheduled_for_returns_max(db_path):
    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_content())
        insert_post_record(
            conn, "http://example.com/1", "linkedin", "text",
            scheduled_for="2024-06-01T09:00:00Z",
        )
        result = get_latest_scheduled_for(conn, "linkedin")
    assert result == "2024-06-01T09:00:00Z"


def test_pending_comment_lifecycle(db_path):
    with get_conn(db_path) as conn:
        insert_pending_comment(
            conn,
            publora_post_id="pub123",
            platform_account_id="acc456",
            content_url="https://example.com",
            content_title="My Post",
            fires_at="2000-01-01T00:00:00Z",
        )
        due = get_due_pending_comments(conn)
        assert len(due) == 1
        assert due[0]["publora_post_id"] == "pub123"

        mark_comment_done(conn, due[0]["id"])
        due_after = get_due_pending_comments(conn)
        assert len(due_after) == 0


def test_insert_post_record_dry_run(db_path):
    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_content())
        insert_post_record(conn, "http://example.com/1", "linkedin", "post text", dry_run=True)
        history = get_recent_post_history(conn, days=7)
    assert len(history) == 0
