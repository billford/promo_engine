#!/usr/bin/env python3
"""Stage a Facebook post in a real Chrome window, ready for a human to click Post.

Meta has no API for posting to a personal profile, so this drives the web UI. It never
clicks Post itself: it opens the composer, types the post (text + link), picks each
hashtag from Facebook's suggestion popup so it registers as a tag, waits for the
link preview, and leaves the window up with a notification. The human click is the
final step.

Chrome is launched as a detached process with its own profile and a local DevTools port,
and Playwright only attaches to it over CDP. That keeps the window open after this
process exits (the daily job doesn't block waiting for the click), and it's the user's
real Chrome rather than Playwright's automation-flagged Chromium.

One-time login:  .venv/bin/python facebook_browser.py --login
"""
import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.request

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FACEBOOK_HOME = "https://www.facebook.com/"

# Accessible names rather than CSS classes: Facebook's class names are generated and churn.
# The apostrophe may be straight or curly, hence the dot.
COMPOSER_OPENER = re.compile(r"What.s on your mind")
COMPOSER_SUBMIT = re.compile(r"^(Next|Post)$")

PAGE_TIMEOUT_MS = 30_000
LINK_PREVIEW_WAIT_S = 6
# Per-keystroke delay. Typing (rather than filling) is what makes Facebook's editor
# notice the URL and build the link preview.
TYPE_DELAY_MS = 25
# A typed hashtag is only plain text until it's picked from Facebook's suggestion popup.
# Only tags preceded by whitespace (or line start), so a URL's #fragment is left alone.
HASHTAG = re.compile(r"(?<!\S)(#\w+)")
HASHTAG_POPUP_WAIT_MS = 4_000
# Short tags Facebook never offers as an exact suggestion (#AI only brings up #aiart,
# #airport, ...), swapped for the spelled-out tag it does offer. Keys are lowercase.
HASHTAG_EXPANSIONS = {
    "#ai": "#ArtificialIntelligence",
    "#ml": "#MachineLearning",
    "#genai": "#GenerativeAI",
}


class FacebookBrowserError(RuntimeError):
    """Staging the post in Chrome failed; the caller falls back to a Reminder."""


def _open_pages(port: int) -> int | None:
    """How many tabs the DevTools Chrome has open, or None if it isn't running."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
            return sum(1 for target in json.load(resp) if target.get("type") == "page")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def launch_chrome(config: dict, url: str | None = None) -> tuple[int, bool]:
    """Start (or reuse) the dedicated Chrome; return (DevTools port, opened_window).

    A running instance with windows open is reused as-is, unless a url is given (Chrome
    then hands that url to it). Otherwise one window is opened, on url or the Facebook
    home page: by a fresh launch, or by handing the url to an instance left running with
    every window closed. Playwright can't attach to Chrome while it has no windows, so
    that case needs one opened either way.
    """
    port = int(config.get("fb_debug_port", 9223))
    if _open_pages(port) and url is None:
        return port, False
    profile = config["fb_browser_profile"]
    subprocess.Popen(  # pylint: disable=consider-using-with
        [
            CHROME_BIN,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            url or FACEBOOK_HOME,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # survives this process and the launchd job
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _open_pages(port):
            return port, True
        time.sleep(0.5)
    raise FacebookBrowserError(f"Chrome DevTools port {port} never came up.")


def _notify(message: str) -> None:
    subprocess.run(
        ["osascript", "-e", f"display notification {json.dumps(message)} with title \"Promo Engine\""],
        check=False,
    )


def _check_logged_in(page) -> None:
    if "login" in page.url or "checkpoint" in page.url or page.locator("input[name=email]").count():
        raise FacebookBrowserError(
            "Not logged in to Facebook in the promo_engine Chrome profile. "
            "Run: .venv/bin/python facebook_browser.py --login"
        )


def _register_hashtag(page, textbox, tag: str) -> bool:
    """Click the popup suggestion for the tag just typed. False if none matched."""
    # An option reads "#writing\n45M posts" (its textContent runs those together, so match
    # on the first rendered line). The popup also offers longer tags sharing the prefix
    # (#AI -> #aiart, #airport), which must not be picked in its place. Poll, because the
    # list refreshes a beat after the last keystroke.
    deadline = time.monotonic() + HASHTAG_POPUP_WAIT_MS / 1000
    while time.monotonic() < deadline:
        for option in page.get_by_role("option").all():
            try:
                if option.inner_text().split("\n")[0].strip().lower() == tag.lower():
                    option.click()
                    textbox.focus()  # the click can pull focus off the editor
                    return True
            except Exception:  # pylint: disable=broad-except
                continue  # the list re-rendered under us; rescan
        time.sleep(0.25)
    print(f"Facebook offered no exact suggestion for {tag}; left as plain text.")
    return False


def _type_post(page, textbox, post_text: str) -> None:
    """Type post_text into the composer, registering each hashtag as it goes."""
    for i, line in enumerate(post_text.split("\n")):
        if i:
            page.keyboard.press("Shift+Enter")
        spaced = False  # did the last picked suggestion already insert a trailing space?
        for part in HASHTAG.split(line):
            if not part:
                continue
            if HASHTAG.fullmatch(part):
                part = HASHTAG_EXPANSIONS.get(part.lower(), part)
                page.keyboard.type(part, delay=TYPE_DELAY_MS)
                spaced = _register_hashtag(page, textbox, part) and textbox.inner_text()[-1:].isspace()
                continue
            if spaced and part.startswith(" "):
                part = part[1:]
            spaced = False
            if part:
                page.keyboard.type(part, delay=TYPE_DELAY_MS)


def stage_facebook_post(post_text: str, config: dict) -> None:
    """Open the composer with post_text typed in. Raises FacebookBrowserError on any failure."""
    # Imported here so the Reminder fallback still works if Playwright isn't installed.
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    port, opened_window = launch_chrome(config)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            # If launch_chrome just opened a window, it's the only one; write in it rather
            # than adding another. Otherwise the open windows may hold an earlier day's
            # unposted draft, so leave them be and add a tab.
            page = context.pages[0] if opened_window and context.pages else context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.goto(FACEBOOK_HOME, wait_until="domcontentloaded")
            _check_logged_in(page)

            page.get_by_role("button", name=COMPOSER_OPENER).first.click()
            dialog = page.get_by_role("dialog").filter(has=page.get_by_role("textbox")).last
            textbox = dialog.get_by_role("textbox").first
            textbox.click()
            _type_post(page, textbox, post_text)
            # A trailing space closes off a URL at the very end so the preview triggers.
            page.keyboard.type(" ")
            time.sleep(LINK_PREVIEW_WAIT_S)

            # Facebook now steps through "Next" before "Post"; either means the composer is live.
            if not dialog.get_by_role("button", name=COMPOSER_SUBMIT).count():
                raise FacebookBrowserError("Composer opened but no Next/Post button found; layout may have changed.")
            page.bring_to_front()
            # Deliberately no browser.close(): for a CDP connection that would tear down
            # the page. Leaving the with-block just disconnects; Chrome keeps the window.
    except PlaywrightError as exc:
        raise FacebookBrowserError(f"Playwright: {exc}") from exc

    _notify("Facebook post is ready. Review it and click Post.")
    print("Facebook post staged in Chrome; waiting on a manual click of Post.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Facebook browser helper")
    parser.add_argument("--login", action="store_true", help="Open the promo_engine Chrome profile to sign in")
    parser.add_argument("--test", metavar="TEXT", help="Stage TEXT in the composer (won't post)")
    args = parser.parse_args()

    from config import load_config
    config = load_config()
    if args.login:
        launch_chrome(config, FACEBOOK_HOME)
        print("Sign in to Facebook in the Chrome window that opened (tick 'remember me'), then close it.")
    elif args.test:
        stage_facebook_post(args.test, config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
