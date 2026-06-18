from unittest.mock import MagicMock, patch
import pytest
from bluesky import _url_facets, post_to_bluesky


def test_url_facets_empty_text():
    assert _url_facets("No URLs here") == []


def test_url_facets_single_url():
    text = "Check this https://example.com out"
    facets = _url_facets(text)
    assert len(facets) == 1
    facet = facets[0]
    assert facet["features"][0]["uri"] == "https://example.com"
    byte_start = facet["index"]["byteStart"]
    byte_end = facet["index"]["byteEnd"]
    assert text.encode("utf-8")[byte_start:byte_end] == b"https://example.com"


def test_url_facets_non_ascii_prefix():
    text = "Héllo https://example.com"
    facets = _url_facets(text)
    assert len(facets) == 1
    byte_start = facets[0]["index"]["byteStart"]
    byte_end = facets[0]["index"]["byteEnd"]
    assert text.encode("utf-8")[byte_start:byte_end] == b"https://example.com"


def test_url_facets_multiple_urls():
    text = "See https://one.com and https://two.com"
    facets = _url_facets(text)
    assert len(facets) == 2
    uris = {f["features"][0]["uri"] for f in facets}
    assert uris == {"https://one.com", "https://two.com"}


def test_post_to_bluesky_missing_handle_exits(monkeypatch):
    monkeypatch.setattr("sys.exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        post_to_bluesky("text", {"bluesky_handle": None, "bluesky_app_password": None})


def test_post_to_bluesky_missing_password_exits(monkeypatch):
    monkeypatch.setattr("sys.exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        post_to_bluesky("text", {"bluesky_handle": "user.bsky.social", "bluesky_app_password": None})


@patch("bluesky.requests.post")
def test_post_to_bluesky_success(mock_post):
    session_resp = MagicMock(status_code=200)
    session_resp.json.return_value = {"accessJwt": "tok123", "did": "did:plc:abc"}

    create_resp = MagicMock(status_code=200)
    create_resp.json.return_value = {"uri": "at://did:plc:abc/app.bsky.feed.post/xyz"}

    mock_post.side_effect = [session_resp, create_resp]

    config = {"bluesky_handle": "user.bsky.social", "bluesky_app_password": "app-pass"}
    uri = post_to_bluesky("Hello https://example.com #tag", config)
    assert uri == "at://did:plc:abc/app.bsky.feed.post/xyz"


@patch("bluesky.requests.post")
def test_post_to_bluesky_login_failure_exits(mock_post, monkeypatch):
    monkeypatch.setattr("sys.exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    fail_resp = MagicMock(status_code=401, text="Unauthorized")
    mock_post.return_value = fail_resp

    config = {"bluesky_handle": "user.bsky.social", "bluesky_app_password": "wrong"}
    with pytest.raises(SystemExit):
        post_to_bluesky("Hello", config)
