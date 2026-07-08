from unittest.mock import MagicMock, patch
from writer import _enforce_bluesky_limit, write_posts


def test_bluesky_limit_under_280_unchanged():
    text = "Short post https://example.com #tag"
    assert _enforce_bluesky_limit(text, "https://example.com") == text


def test_bluesky_limit_truncates_prefix_preserves_url():
    url = "https://example.com/article"
    long_prefix = "x" * 300
    text = f"{long_prefix} {url}"
    result = _enforce_bluesky_limit(text, url)
    assert len(result) <= 280
    assert url in result


def test_bluesky_limit_exactly_280_unchanged():
    url = "https://x.com/a"
    body = "a" * (280 - len(url) - 1)
    text = f"{body} {url}"
    assert len(text) == 280
    assert _enforce_bluesky_limit(text, url) == text


def test_bluesky_limit_url_not_in_text_truncates():
    long_text = "a" * 300
    result = _enforce_bluesky_limit(long_text, "https://not-present.com")
    assert len(result) <= 280
    assert result.endswith("...")


@patch("writer.anthropic.Anthropic")
def test_write_posts_returns_both_platforms(mock_cls):
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
    result = write_posts(content, {"anthropic_api_key": "test-key"})
    assert "linkedin" in result
    assert "bluesky" in result


@patch("writer.anthropic.Anthropic")
def test_write_posts_adds_ai_promoted_if_missing(mock_cls):
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
    result = write_posts(content, {"anthropic_api_key": "key"})
    assert "#AIPromoted" in result["bluesky"]
    assert len(result["bluesky"]) <= 280
