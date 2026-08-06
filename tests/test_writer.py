from unittest.mock import MagicMock, patch
from writer import BLUESKY_LIMIT, _enforce_bluesky_limit, write_post


def test_bluesky_limit_under_280_unchanged():
    text = "Short post https://example.com #tag"
    assert _enforce_bluesky_limit(text, "https://example.com") == text


def test_bluesky_limit_truncates_prefix_preserves_url():
    url = "https://example.com/article"
    long_prefix = "x" * (BLUESKY_LIMIT + 50)
    text = f"{long_prefix} {url}"
    result = _enforce_bluesky_limit(text, url)
    assert len(result) <= BLUESKY_LIMIT
    assert url in result


def test_bluesky_limit_exactly_at_limit_unchanged():
    url = "https://x.com/a"
    body = "a" * (BLUESKY_LIMIT - len(url) - 1)
    text = f"{body} {url}"
    assert len(text) == BLUESKY_LIMIT
    assert _enforce_bluesky_limit(text, url) == text


def test_bluesky_limit_url_not_in_text_truncates():
    long_text = "a" * (BLUESKY_LIMIT + 50)
    result = _enforce_bluesky_limit(long_text, "https://not-present.com")
    assert len(result) <= BLUESKY_LIMIT
    assert result.endswith("...")


@patch("writer.anthropic.Anthropic")
def test_write_post_drafts_only_the_requested_platform(mock_cls):
    mock_client = MagicMock()
    mock_cls.return_value = mock_client
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text="Test post text #tag")]
    )

    content = {
        "title": "Test Article",
        "source": "medium",
        "url": "https://example.com/article",
        "description": "Test description",
    }
    result = write_post(content, {"anthropic_api_key": "test-key"}, "linkedin")
    assert result == "Test post text #tag"
    # One platform requested -> exactly one API call (was three, two discarded)
    assert mock_client.messages.create.call_count == 1


@patch("writer.anthropic.Anthropic")
def test_write_post_adds_ai_promoted_if_missing(mock_cls):
    mock_client = MagicMock()
    mock_cls.return_value = mock_client
    short_post = "A post https://example.com/a #tag"
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text=short_post)]
    )

    content = {
        "title": "Test",
        "source": "medium",
        "url": "https://example.com/a",
        "description": "desc",
    }
    result = write_post(content, {"anthropic_api_key": "key"}, "bluesky")
    assert "#AIPromoted" in result
    assert len(result) <= BLUESKY_LIMIT
