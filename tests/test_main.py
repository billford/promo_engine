import argparse
from zoneinfo import ZoneInfo

import pytest
from db import init_db, get_conn, upsert_content, insert_post_record
from main import print_weekly_report, PLATFORM_CONTENT_TYPE
from tests.fixtures import ARTICLE_BODY


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def _insert_content_and_post(conn, content_id, platform):
    upsert_content(conn, {
        "id": content_id,
        "source": "medium",
        "title": f"Article {content_id}",
        "url": content_id,
        "published_date": "2024-01-01",
        "description": ARTICLE_BODY,
        "tags": [],
    })
    insert_post_record(conn, content_id, platform, f"Post about {content_id}")


def test_print_weekly_report_empty(db_path, capsys):
    with get_conn(db_path) as conn:
        print_weekly_report(conn)
    output = capsys.readouterr().out
    assert "No posts in the last 7 days" in output


def test_print_weekly_report_shows_posts(db_path, capsys):
    with get_conn(db_path) as conn:
        _insert_content_and_post(conn, "http://example.com/1", "linkedin")
        _insert_content_and_post(conn, "http://example.com/2", "bluesky")
        print_weekly_report(conn)
    output = capsys.readouterr().out
    assert "LINKEDIN" in output
    assert "BLUESKY" in output
    assert "Article http://example.com/1" in output
    assert "Total: 2 post(s)" in output


def test_print_weekly_report_excludes_dry_runs(db_path, capsys):
    with get_conn(db_path) as conn:
        upsert_content(conn, {
            "id": "http://example.com/1",
            "source": "medium",
            "title": "Dry Run Article",
            "url": "http://example.com/1",
            "published_date": "2024-01-01",
            "description": ARTICLE_BODY,
            "tags": [],
        })
        insert_post_record(conn, "http://example.com/1", "linkedin", "text", dry_run=True)
        print_weekly_report(conn)
    output = capsys.readouterr().out
    assert "No posts in the last 7 days" in output


def test_platform_content_type_mapping():
    assert PLATFORM_CONTENT_TYPE["linkedin"] == "business"
    assert PLATFORM_CONTENT_TYPE["bluesky"] == "personal"


def test_has_posted_today_false_before_any_post(db_path):
    from db import has_posted_today
    with get_conn(db_path) as conn:
        assert has_posted_today(conn, "facebook", ZoneInfo("America/New_York")) is False


def test_has_posted_today_true_after_real_post(db_path):
    from db import has_posted_today
    with get_conn(db_path) as conn:
        _insert_content_and_post(conn, "http://example.com/1", "facebook")
        assert has_posted_today(conn, "facebook", ZoneInfo("America/New_York")) is True
        assert has_posted_today(conn, "bluesky", ZoneInfo("America/New_York")) is False


def test_has_posted_today_ignores_dry_runs(db_path):
    from db import has_posted_today
    with get_conn(db_path) as conn:
        upsert_content(conn, {
            "id": "http://example.com/dry",
            "source": "medium",
            "title": "Dry",
            "url": "http://example.com/dry",
            "published_date": "2024-01-01",
            "description": ARTICLE_BODY,
            "tags": [],
        })
        insert_post_record(conn, "http://example.com/dry", "facebook", "text", dry_run=True)
        assert has_posted_today(conn, "facebook", ZoneInfo("America/New_York")) is False


def test_has_posted_today_ignores_yesterday(db_path):
    from db import has_posted_today
    with get_conn(db_path) as conn:
        _insert_content_and_post(conn, "http://example.com/2", "facebook")
        conn.execute(
            "UPDATE post_history SET posted_at = datetime('now', '-2 days')"
        )
        assert has_posted_today(conn, "facebook", ZoneInfo("America/New_York")) is False


def test_run_platform_skips_platform_already_posted_today(db_path, capsys, monkeypatch):
    import main as main_mod

    def _boom(*_args, **_kwargs):
        raise AssertionError("pick_content must not run for an already-posted platform")

    monkeypatch.setattr("scorer.pick_content", _boom)
    args = argparse.Namespace(dry_run=False, verbose=False)

    with get_conn(db_path) as conn:
        _insert_content_and_post(conn, "http://example.com/3", "facebook")
        main_mod.run_platform("facebook", conn, {"timezone": "America/New_York"}, args)

    assert "Skipping facebook: already posted today" in capsys.readouterr().out
