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


class _FakeKeyboard:
    def __init__(self, typed):
        self.typed = typed

    def type(self, text, delay=0):
        self.typed.append(text)

    def press(self, key):
        self.typed.append(f"<{key}>")


class _FakeTextbox:
    def __init__(self, typed):
        self.typed = typed

    def inner_text(self):
        return "".join(t for t in self.typed if not t.startswith("<"))


def _run_type_post(monkeypatch, text, matched=lambda tag: True):
    import facebook_browser

    typed, registered = [], []

    def _register(page, textbox, tag):
        registered.append(tag)
        if matched(tag):
            typed.append(" ")  # a picked suggestion inserts its own trailing space
            return True
        return False

    monkeypatch.setattr(facebook_browser, "_register_hashtag", _register)
    page = SimpleNamespace(keyboard=_FakeKeyboard(typed))
    facebook_browser._type_post(page, _FakeTextbox(typed), text)
    return "".join(t for t in typed if not t.startswith("<")), registered


def test_each_hashtag_is_registered_without_doubling_spaces(monkeypatch):
    out, registered = _run_type_post(monkeypatch, "Read it\nhttps://x.test/a#frag\n#Books #Writing done")

    assert registered == ["#Books", "#Writing"]  # the URL fragment is not a tag
    assert out == "Read ithttps://x.test/a#frag#Books #Writing done"


def test_unmatched_hashtag_stays_plain_text(monkeypatch):
    out, registered = _run_type_post(monkeypatch, "#Rare #Books", matched=lambda tag: tag == "#Books")

    assert registered == ["#Rare", "#Books"]
    assert out == "#Rare #Books "


def test_short_tags_are_spelled_out(monkeypatch):
    out, registered = _run_type_post(monkeypatch, "#AI #ai #Writing")

    assert registered == ["#ArtificialIntelligence", "#ArtificialIntelligence", "#Writing"]
    assert out == "#ArtificialIntelligence #ArtificialIntelligence #Writing "


@pytest.fixture()
def chrome_spawns(monkeypatch):
    import facebook_browser

    spawned = []
    monkeypatch.setattr(facebook_browser.subprocess, "Popen", lambda args, **kw: spawned.append(args[-1]))
    return spawned


def _tabs(monkeypatch, *counts):
    """Successive _open_pages answers: None = Chrome not running, 0 = running, no windows."""
    import facebook_browser

    answers = iter(counts)
    monkeypatch.setattr(facebook_browser, "_open_pages", lambda port: next(answers))


def test_chrome_with_windows_is_reused_without_a_new_one(monkeypatch, chrome_spawns):
    import facebook_browser

    _tabs(monkeypatch, 2)

    assert facebook_browser.launch_chrome({"fb_browser_profile": "/p"}) == (9223, False)
    assert chrome_spawns == []


def test_fresh_launch_opens_one_facebook_window(monkeypatch, chrome_spawns):
    import facebook_browser

    _tabs(monkeypatch, None, None, 1)

    assert facebook_browser.launch_chrome({"fb_browser_profile": "/p"}) == (9223, True)
    assert chrome_spawns == [facebook_browser.FACEBOOK_HOME]


def test_windowless_chrome_gets_one_window(monkeypatch, chrome_spawns):
    import facebook_browser

    _tabs(monkeypatch, 0, 1)

    assert facebook_browser.launch_chrome({"fb_browser_profile": "/p"}) == (9223, True)
    assert chrome_spawns == [facebook_browser.FACEBOOK_HOME]


def test_login_opens_a_window_even_when_running(monkeypatch, chrome_spawns):
    import facebook_browser

    _tabs(monkeypatch, 2, 3)

    facebook_browser.launch_chrome({"fb_browser_profile": "/p"}, facebook_browser.FACEBOOK_HOME)
    assert chrome_spawns == [facebook_browser.FACEBOOK_HOME]


@pytest.fixture()
def chrome_kills(monkeypatch):
    import facebook_browser

    killed = []
    monkeypatch.setattr(facebook_browser, "_chrome_pid", lambda profile: 4242)
    monkeypatch.setattr(facebook_browser.os, "kill", lambda pid, sig: killed.append(pid))
    return killed


def test_idle_chrome_is_restarted(monkeypatch, chrome_kills):
    import facebook_browser

    _tabs(monkeypatch, 2, None)  # two tabs, then gone after the quit
    monkeypatch.setattr(facebook_browser, "_has_unsent_draft", lambda port: False)

    facebook_browser.restart_idle_chrome({"fb_browser_profile": "/p"})
    assert chrome_kills == [4242]


def test_chrome_with_unsent_draft_is_left_running(monkeypatch, chrome_kills):
    import facebook_browser

    _tabs(monkeypatch, 2)
    monkeypatch.setattr(facebook_browser, "_has_unsent_draft", lambda port: True)

    facebook_browser.restart_idle_chrome({"fb_browser_profile": "/p"})
    assert chrome_kills == []


def test_windowless_chrome_is_restarted_without_a_draft_check(monkeypatch, chrome_kills):
    import facebook_browser

    _tabs(monkeypatch, 0, None)

    def _no_check(port):
        raise AssertionError("Playwright can't attach to a windowless Chrome")

    monkeypatch.setattr(facebook_browser, "_has_unsent_draft", _no_check)

    facebook_browser.restart_idle_chrome({"fb_browser_profile": "/p"})
    assert chrome_kills == [4242]


def test_nothing_to_restart_when_chrome_is_down(monkeypatch, chrome_kills):
    import facebook_browser

    _tabs(monkeypatch, None)

    facebook_browser.restart_idle_chrome({"fb_browser_profile": "/p"})
    assert chrome_kills == []
