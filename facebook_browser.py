#!/usr/bin/env python3
"""Stage a Facebook post in a real Chrome window, ready for a human to click Post.

Meta has no API for posting to a personal profile, so this drives the web UI. It never
clicks Post itself: it opens the composer, types the post (text + link), waits for the
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

PAGE_TIMEOUT_MS = 30_000
LINK_PREVIEW_WAIT_S = 6
# Per-keystroke delay. Typing (rather than filling) is what makes Facebook's editor
# notice the URL and build the link preview.
TYPE_DELAY_MS = 25


class FacebookBrowserError(RuntimeError):
    """Staging the post in Chrome failed; the caller falls back to a Reminder."""


def _devtools_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
            return "webSocketDebuggerUrl" in json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def launch_chrome(config: dict, url: str = FACEBOOK_HOME) -> int:
    """Start (or reuse) the dedicated Chrome instance; return its DevTools port.

    If yesterday's instance is still running, Chrome hands the new URL to it and the
    port is already listening.
    """
    port = int(config.get("fb_debug_port", 9223))
    profile = config["fb_browser_profile"]
    subprocess.Popen(  # pylint: disable=consider-using-with
        [
            CHROME_BIN,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # survives this process and the launchd job
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _devtools_up(port):
            return port
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


def stage_facebook_post(post_text: str, config: dict) -> None:
    """Open the composer with post_text typed in. Raises FacebookBrowserError on any failure."""
    # Imported here so the Reminder fallback still works if Playwright isn't installed.
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    port = launch_chrome(config)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = browser.contexts[0].new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.goto(FACEBOOK_HOME, wait_until="domcontentloaded")
            _check_logged_in(page)

            page.get_by_role("button", name=COMPOSER_OPENER).first.click()
            dialog = page.get_by_role("dialog").filter(has=page.get_by_role("textbox")).last
            textbox = dialog.get_by_role("textbox").first
            textbox.click()
            for i, line in enumerate(post_text.split("\n")):
                if i:
                    page.keyboard.press("Shift+Enter")
                page.keyboard.type(line, delay=TYPE_DELAY_MS)
            # A trailing space closes off a URL at the very end so the preview triggers.
            page.keyboard.type(" ")
            time.sleep(LINK_PREVIEW_WAIT_S)

            if not dialog.get_by_role("button", name="Post", exact=True).count():
                raise FacebookBrowserError("Composer opened but no Post button found; layout may have changed.")
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
        launch_chrome(config)
        print("Sign in to Facebook in the Chrome window that opened (tick 'remember me'), then close it.")
    elif args.test:
        stage_facebook_post(args.test, config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
