import json
from unittest.mock import MagicMock, patch
import pytest
from db import init_db, get_conn, upsert_content
from scorer import _build_catalog_text, pick_content


def _sample_item(content_id="http://example.com/1", content_type="business"):
    return {
        "id": content_id,
        "source": "medium",
        "title": "Test Article",
        "url": content_id,
        "published_date": "2024-01-01T00:00:00+00:00",
        "description": "Test description",
        "tags": ["tech"],
        "content_type": content_type,
        "last_posted": None,
    }


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def test_build_catalog_text_includes_content_type():
    items = [_sample_item(content_type="business")]
    text = _build_catalog_text(items, [])
    assert "Type: business" in text


def test_build_catalog_text_unclassified_label():
    item = _sample_item()
    item["content_type"] = None
    text = _build_catalog_text([item], [])
    assert "Type: unclassified" in text


def test_build_catalog_text_includes_recent_history():
    history = [{
        "platform": "linkedin",
        "title": "Old Article",
        "source": "medium",
        "posted_at": "2024-01-01T09:00:00",
    }]
    text = _build_catalog_text([], history)
    assert "Old Article" in text
    assert "Recent post history" in text


def test_build_catalog_text_includes_all_fields():
    items = [_sample_item()]
    text = _build_catalog_text(items, [])
    assert "Test Article" in text
    assert "http://example.com/1" in text
    assert "tech" in text


@patch("scorer.anthropic.Anthropic")
def test_pick_content_returns_required_keys(mock_cls, db_path):
    mock_client = MagicMock()
    mock_cls.return_value = mock_client
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text=json.dumps({
            "content_id": "http://example.com/1",
            "title": "Test Article",
            "url": "http://example.com/1",
            "source": "medium",
            "rationale": "Good evergreen content",
        }))]
    )

    with get_conn(db_path) as conn:
        upsert_content(conn, {
            "id": "http://example.com/1",
            "source": "medium",
            "title": "Test Article",
            "url": "http://example.com/1",
            "published_date": "2024-01-01",
            "description": "desc",
            "tags": [],
        })
        result = pick_content(conn, {"anthropic_api_key": "key"}, ["linkedin"], "business")

    assert result["content_id"] == "http://example.com/1"
    assert result["title"] == "Test Article"
    assert "rationale" in result


@patch("scorer.anthropic.Anthropic")
def test_pick_content_handles_json_with_preamble(mock_cls, db_path):
    mock_client = MagicMock()
    mock_cls.return_value = mock_client
    raw = (
        'Here is my pick:\n'
        '{"content_id":"http://example.com/1","title":"T",'
        '"url":"http://example.com/1","source":"medium","rationale":"r"}'
    )
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text=raw)]
    )

    with get_conn(db_path) as conn:
        upsert_content(conn, {
            "id": "http://example.com/1",
            "source": "medium",
            "title": "T",
            "url": "http://example.com/1",
            "published_date": "2024-01-01",
            "description": "desc",
            "tags": [],
        })
        result = pick_content(conn, {"anthropic_api_key": "key"}, ["linkedin"])

    assert result["content_id"] == "http://example.com/1"


def _scorer_response(payload: dict) -> MagicMock:
    return MagicMock(content=[MagicMock(text=json.dumps(payload))])


@patch("scorer.anthropic.Anthropic")
def test_pick_content_uses_db_fields_not_model_transcription(mock_cls, db_path):
    """The model picks an id; every posted field must come from the database row."""
    real_id = "https://billfordx.medium.com/real-article-155c27d86f7f?source=rss-x"
    mock_client = MagicMock()
    mock_cls.return_value = mock_client
    mock_client.messages.create.return_value = _scorer_response({
        "content_id": real_id,
        "title": "Hallucinated Title",
        "url": "https://example.com/wrong-link",
        "source": "youtube",
        "rationale": "because",
    })

    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_item(real_id))
        selected = pick_content(conn, {"anthropic_api_key": "k"}, ["linkedin"])

    assert selected["url"] == real_id          # not the model's invented link
    assert selected["title"] == "Test Article"  # not the model's invented title
    assert selected["source"] == "medium"
    assert selected["content_id"] == "medium:155c27d86f7f"
    assert selected["description"] == "Test description"


@patch("scorer.anthropic.Anthropic")
def test_pick_content_rejects_unknown_content_id(mock_cls, db_path):
    """A garbled id must fail loudly, not post a dead link and poison the cooldown."""
    mock_client = MagicMock()
    mock_cls.return_value = mock_client
    mock_client.messages.create.return_value = _scorer_response({
        "content_id": "https://medium.com/@billfordx/my-wife-hates-ghost-stories-old",
        "title": "T", "url": "u", "source": "medium", "rationale": "r",
    })

    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_item("https://billfordx.medium.com/real-155c27d86f7f"))
        with pytest.raises(RuntimeError, match="not in the catalog"):
            pick_content(conn, {"anthropic_api_key": "k"}, ["linkedin"])


@patch("scorer.anthropic.Anthropic")
def test_pick_content_tolerates_url_variant_of_chosen_id(mock_cls, db_path):
    """A different-but-equivalent Medium URL should resolve, not fail."""
    stored = "https://billfordx.medium.com/real-article-155c27d86f7f?source=rss-x"
    mock_client = MagicMock()
    mock_cls.return_value = mock_client
    mock_client.messages.create.return_value = _scorer_response({
        "content_id": "https://medium.com/@billfordx/real-article-155c27d86f7f",
        "title": "T", "url": "u", "source": "medium", "rationale": "r",
    })

    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_item(stored))
        selected = pick_content(conn, {"anthropic_api_key": "k"}, ["linkedin"])

    assert selected["content_id"] == "medium:155c27d86f7f"
