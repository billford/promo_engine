import json
from unittest.mock import MagicMock, patch
import pytest
from db import init_db, get_conn, upsert_content, get_unclassified_content
from collector import classify_unclassified, _CLASSIFY_PROMPT


def _sample_item(content_id="http://example.com/1"):
    return {
        "id": content_id,
        "source": "medium",
        "title": "Test Article",
        "url": content_id,
        "published_date": "2024-01-01T00:00:00+00:00",
        "description": "Test description",
        "tags": [],
    }


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def test_classify_unclassified_empty_returns_zero(db_path):
    with get_conn(db_path) as conn:
        count = classify_unclassified(conn, "fake-key")
    assert count == 0


@patch("collector.anthropic.Anthropic")
def test_classify_unclassified_classifies_items(mock_cls, db_path):
    mock_client = MagicMock()
    mock_cls.return_value = mock_client
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(
            text=json.dumps([
                {"id": "http://example.com/1", "type": "business"},
                {"id": "http://example.com/2", "type": "personal"},
            ])
        )]
    )

    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_item("http://example.com/1"))
        upsert_content(conn, _sample_item("http://example.com/2"))
        count = classify_unclassified(conn, "fake-key")
        unclassified = get_unclassified_content(conn)

    assert count == 2
    assert len(unclassified) == 0


@patch("collector.anthropic.Anthropic")
def test_classify_unclassified_handles_json_in_markdown(mock_cls, db_path):
    mock_client = MagicMock()
    mock_cls.return_value = mock_client
    wrapped = '```json\n[{"id": "http://example.com/1", "type": "business"}]\n```'
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text=wrapped)]
    )

    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_item("http://example.com/1"))
        count = classify_unclassified(conn, "fake-key")

    assert count == 1


@patch("collector.anthropic.Anthropic")
def test_classify_unclassified_api_error_returns_zero(mock_cls, db_path):
    mock_client = MagicMock()
    mock_cls.return_value = mock_client
    mock_client.messages.create.side_effect = RuntimeError("API error")

    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_item())
        count = classify_unclassified(conn, "fake-key")

    assert count == 0


@patch("collector.anthropic.Anthropic")
def test_classify_unclassified_skips_invalid_type(mock_cls, db_path):
    mock_client = MagicMock()
    mock_cls.return_value = mock_client
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(
            text=json.dumps([{"id": "http://example.com/1", "type": "unknown"}])
        )]
    )

    with get_conn(db_path) as conn:
        upsert_content(conn, _sample_item())
        count = classify_unclassified(conn, "fake-key")
        remaining = get_unclassified_content(conn)

    assert count == 0
    assert len(remaining) == 1


def test_classify_prompt_has_required_placeholder():
    assert "{catalog}" in _CLASSIFY_PROMPT
