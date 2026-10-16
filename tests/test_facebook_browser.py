import sys
import types
from types import SimpleNamespace

import pytest

from main import _pick_image, _post_facebook
from facebook_browser import FacebookBrowserError

SELECTED = {"title": "An article", "content_id": "http://example.com/a"}


@pytest.fixture()
def reminders(monkeypatch):
    """Stub the macOS-only facebook module so the fallback is observable anywhere."""
    calls = []
    fake = types.ModuleType("facebook")
    fake.create_facebook_reminder = lambda title, text, config, image_path=None: calls.append(
        (title, text, image_path)
    )
    monkeypatch.setitem(sys.modules, "facebook", fake)
    return calls


def test_browser_mode_stages_post_without_reminder(monkeypatch, reminders):
    staged = []
    monkeypatch.setattr("facebook_browser.stage_facebook_post", lambda text, config: staged.append(text))

    used = _post_facebook(SELECTED, "hello https://x.test", None, {"facebook_mode": "browser"})

    assert staged == ["hello https://x.test"]
    assert reminders == []
    assert used is None


def test_browser_failure_falls_back_to_reminder(monkeypatch, reminders, capsys):
    def _fail(text, config):
        raise FacebookBrowserError("Not logged in")

    monkeypatch.setattr("facebook_browser.stage_facebook_post", _fail)

    _post_facebook(SELECTED, "hello", None, {"facebook_mode": "browser"})

    assert reminders == [("An article", "hello", None)]
    assert "browser staging failed" in capsys.readouterr().err


def test_reminder_mode_skips_browser_and_keeps_photo(monkeypatch, reminders):
    def _boom(text, config):
        raise AssertionError("browser must not run in reminder mode")

    monkeypatch.setattr("facebook_browser.stage_facebook_post", _boom)
    image = SimpleNamespace(path="/tmp/p.jpg", photo_id=7)

    used = _post_facebook(SELECTED, "hello", image, {"facebook_mode": "reminder"})

    assert reminders == [("An article", "hello", "/tmp/p.jpg")]
    assert used is image


def test_browser_mode_picks_no_photo():
    config = {"facebook_mode": "browser", "photo_db_url": "postgresql://unused"}
    assert _pick_image("facebook", SELECTED, None, config, verbose=False) is None
