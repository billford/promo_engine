import pytest
from db import init_db, get_conn, upsert_content, insert_post_record
from main import print_weekly_report, PLATFORM_CONTENT_TYPE


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
        "description": "desc",
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
            "description": "desc",
            "tags": [],
        })
        insert_post_record(conn, "http://example.com/1", "linkedin", "text", dry_run=True)
        print_weekly_report(conn)
    output = capsys.readouterr().out
    assert "No posts in the last 7 days" in output


def test_platform_content_type_mapping():
    assert PLATFORM_CONTENT_TYPE["linkedin"] == "business"
    assert PLATFORM_CONTENT_TYPE["bluesky"] == "personal"
