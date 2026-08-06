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
    canonical_content_id,
)
from tests.fixtures import ARTICLE_BODY


MEDIUM_VARIANTS = [
    "https://billfordx.medium.com/help-ai-deleted-my-company-155c27d86f7f?source=rss-1384bc4a7965------2",
    "https://medium.com/@billfordx/help-ai-deleted-my-company-155c27d86f7f",
    "https://medium.com/new-literary-society/help-ai-deleted-my-company-155c27d86f7f",
    "https://radiohackers.com/help-ai-deleted-my-company-155c27d86f7f?source=rss-1384bc4a7965------2",
]


@pytest.mark.parametrize("url", MEDIUM_VARIANTS)
def test_medium_url_variants_share_one_canonical_id(url):
    assert canonical_content_id(url, "medium") == "medium:155c27d86f7f"


def test_youtube_ids_canonicalize():
    assert canonical_content_id("dQw4w9WgXcQ", "youtube") == "youtube:dQw4w9WgXcQ"
    assert canonical_content_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "youtube:dQw4w9WgXcQ"


def test_canonical_id_is_idempotent():
    once = canonical_content_id(MEDIUM_VARIANTS[0], "medium")
    assert canonical_content_id(once, "medium") == once


def test_url_variants_collapse_to_one_catalog_row(db_path):
    """The repeat bug: one article ingested under several URLs became several rows."""
    with get_conn(db_path) as conn:
        for url in MEDIUM_VARIANTS:
            upsert_content(conn, {
                "id": url,
                "source": "medium",
                "title": "Help, AI Deleted My Company",
                "url": url,
                "published_date": "2026-04-30T18:23:31+00:00",
                "description": ARTICLE_BODY,
                "tags": [],
            })
        assert len(get_all_content(conn)) == 1


def test_cooldown_covers_every_url_variant(db_path):
    """Posting via one URL variant must put the article on cooldown for all of them."""
    with get_conn(db_path) as conn:
        for url in MEDIUM_VARIANTS:
            upsert_content(conn, {
                "id": url, "source": "medium", "title": "A Test Article Title", "url": url,
                "published_date": "2026-04-30T18:23:31+00:00", "description": ARTICLE_BODY, "tags": [],
            })
        insert_post_record(
            conn,
            content_id=canonical_content_id(MEDIUM_VARIANTS[0], "medium"),
            platform="linkedin",
            post_text="x",
        )

        assert get_eligible_content(conn, "linkedin", 30) == []
        assert get_recently_selected_ids(conn, days=14) == {"medium:155c27d86f7f"}


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
        "description": ARTICLE_BODY,
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


REAL_ARTICLES = [
    ("Cybersecurity Clichés: How Companies Dodge Responsibility", 91),
    ("A Sort Of Halloween Ghost Tale", 177),
    ("My Plain Old Soul", 412),
    ("Rheumatoid Arthritis, Surviving & AI", 500),
    ("Bill's Short Blasts: Holiday Movies", 545),
    ("Who's Afraid of Their TBR?", 2021),          # '?' is valid headline punctuation
    ("Where In The World Do These People Come From?", 834),
    ("Software Defined Radio is Fun: AI Can Help You Learn It.", 3000),
]

MEDIUM_RESPONSES = [
    ("Thank you", 32),
    ("Fair", 26),
    ("Lizzie Borden", 36),
    ("True story", 33),
    ("Same. Had no idea", 40),
    ("I think there's a lot of merit to this.", 302),
    ("Huge Prince fan.", 331),
    ("I know I'm going to get piled on for this, but I hate Nolan films.", 539),
    ("You might be the first person I've ever heard of that doesn't eat snacks", 526),
]


@pytest.mark.parametrize("title,length", REAL_ARTICLES)
def test_real_articles_are_not_classified_as_responses(title, length):
    from db import classify_content_kind
    assert classify_content_kind("medium:x", title, "x" * length) == "article"


@pytest.mark.parametrize("title,length", MEDIUM_RESPONSES)
def test_medium_replies_are_classified_as_responses(title, length):
    from db import classify_content_kind
    assert classify_content_kind("medium:x", title, "x" * length) == "response"


def test_responses_are_excluded_from_selection(db_path):
    """A Medium reply must never reach the promotion pool."""
    from db import get_oldest_content_by_platform
    with get_conn(db_path) as conn:
        upsert_content(conn, {
            "id": "https://medium.com/@billfordx/thank-you-34610b897100",
            "source": "medium", "title": "Thank you",
            "url": "https://medium.com/@billfordx/thank-you-34610b897100",
            "published_date": "2024-01-01T00:00:00+00:00",
            "description": "Thank you so much.", "tags": [],
        })
        upsert_content(conn, _sample_content("https://billfordx.medium.com/real-155c27d86f7f"))

        assert conn.execute(
            "SELECT content_kind FROM content WHERE id = 'medium:34610b897100'"
        ).fetchone()[0] == "response"
        assert [r["id"] for r in get_eligible_content(conn, "linkedin", 30)] == ["medium:155c27d86f7f"]
        assert [r["id"] for r in get_oldest_content_by_platform(conn, "linkedin")] == ["medium:155c27d86f7f"]
