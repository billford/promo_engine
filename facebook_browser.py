#!/usr/bin/env python3
"""Stage a Facebook post in a real Chrome window, ready for a human to click Post.

Meta has no API for posting to a personal profile, so this drives the web UI. It never
clicks Post itself: it opens the composer, types the post (text + link), picks each
hashtag from Facebook's suggestion popup so it registers as a tag, waits for the
link preview, and leaves the window up with a notification. The human click is the
final step for the post itself.

The article link is kept out of the post, because Facebook shows link posts to fewer
people. A detached watcher waits for that click, then adds the link as the post's first
comment. If it can't, it falls back to a notification with the link on the clipboard.

Chrome is launched as a detached process with its own profile and a local DevTools port,
and Playwright only attaches to it over CDP. That keeps the window open after this
process exits (the daily job doesn't block waiting for the click), and it's the user's
real Chrome rather than Playwright's automation-flagged Chromium. Each run quits and
relaunches that Chrome first, unless it still holds an unsent post, so the memory it
piles up between runs is handed back daily.

One-time login:  .venv/bin/python facebook_browser.py --login
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FACEBOOK_HOME = "https://www.facebook.com/"
PROFILE_URL = "https://www.facebook.com/me"

# Accessible names rather than CSS classes: Facebook's class names are generated and churn.
# The apostrophe may be straight or curly, hence the dot.
COMPOSER_OPENER = re.compile(r"What.s on your mind")
COMPOSER_SUBMIT = re.compile(r"^(Next|Post)$")
COMMENT_OPENER = re.compile(r"^(Leave a comment|Comment)$")
COMMENT_BOX = re.compile(r"Write a comment|Comment as")
POST_MESSAGE = '[data-ad-preview="message"], [data-ad-rendering-role="story_message"]'

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
# How long the comment watcher waits for the Post click before giving up.
COMMENT_WATCH_S = 12 * 3600
COMMENT_POLL_S = 5
# A just-sent post can take several reloads to show on the profile (four or five, in
# practice), so keep reloading for several minutes before giving up.
POST_FIND_ATTEMPTS = 12
POST_FIND_DELAY_S = 20
POST_FIND_WAIT_MS = 10_000
# The post is found on the profile by its opening words, before any "See more" cut.
MATCH_CHARS = 50


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


def _composer_open(context) -> bool:
    return any(
        page.get_by_role("dialog").get_by_role("button", name=COMPOSER_SUBMIT).count()
        for page in context.pages
    )


def _has_unsent_draft(port: int) -> bool:
    """True if any tab has the post composer open. Errs toward True: losing a draft is worse."""
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            return _composer_open(browser.contexts[0])
    except PlaywrightError:
        return True


def _chrome_pid(profile: str) -> int | None:
    """PID of the dedicated Chrome's main process (its helpers run a different binary)."""
    ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False)
    for row in ps.stdout.splitlines():
        pid, _, command = row.strip().partition(" ")
        if command.startswith(f"{CHROME_BIN} --user-data-dir={profile} "):
            return int(pid)
    return None


def restart_idle_chrome(config: dict) -> None:
    """Quit the dedicated Chrome if it's running with no unsent post, freeing its memory.

    It's left alone while any tab still has the composer open, i.e. an earlier post that
    hasn't been sent or dismissed yet.
    """
    port = int(config.get("fb_debug_port", 9223))
    tabs = _open_pages(port)
    if tabs is None:
        return
    if tabs and _has_unsent_draft(port):
        print("Earlier Facebook post still open in Chrome; adding a tab instead of restarting.")
        return
    pid = _chrome_pid(config["fb_browser_profile"])
    if pid is None:
        return
    os.kill(pid, signal.SIGTERM)  # Chrome treats SIGTERM as a normal quit
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _open_pages(port) is not None:
        time.sleep(0.5)


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


def stage_facebook_post(post_text: str, config: dict, comment_url: str | None = None) -> None:
    """Open the composer with post_text typed in. Raises FacebookBrowserError on any failure.

    With comment_url, also starts a watcher that adds it as the first comment once the
    post is sent.
    """
    # Imported here so the Reminder fallback still works if Playwright isn't installed.
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    restart_idle_chrome(config)
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

    if comment_url:
        _start_comment_watcher(comment_url, post_text)
        _notify("Facebook post is ready. Review it and click Post; the link goes in the first comment.")
    else:
        _notify("Facebook post is ready. Review it and click Post.")
    print("Facebook post staged in Chrome; waiting on a manual click of Post.")


def match_text(post_text: str) -> str:
    """The post's opening words, used to find it on the profile once it's sent."""
    first_line = next((line.strip() for line in post_text.split("\n") if line.strip()), "")
    if len(first_line) <= MATCH_CHARS:
        return first_line
    return first_line[:MATCH_CHARS].rsplit(" ", 1)[0]


def _start_comment_watcher(url: str, post_text: str) -> None:
    """Run comment_after_post in a detached process, so the daily job doesn't wait on the click."""
    here = os.path.dirname(os.path.abspath(__file__))
    subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, os.path.join(here, "facebook_browser.py"),
         "--comment", url, "--match", match_text(post_text)],
        cwd=here,
        start_new_session=True,  # outlives this process and the launchd job
    )
    print("Started the Facebook first-comment watcher.")


def _comment_fallback(url: str, reason: str) -> None:
    print(f"WARNING: Facebook first comment not posted ({reason}); link copied for a manual paste.",
          file=sys.stderr)
    subprocess.run(["pbcopy"], input=url, text=True, check=False)
    _notify(f"Couldn't add the Facebook first comment ({reason}). The link is on the clipboard.")


def _find_post(page, snippet: str):
    """The newest profile post containing snippet, or None."""
    # Profile posts carry no article role, so find the message body by its text and climb
    # to the nearest container holding that post's own comment button.
    post = page.locator(POST_MESSAGE).filter(has_text=snippet).first.locator(
        "xpath=ancestor::div[.//div[@role='button' and @aria-label='Leave a comment']][1]"
    )
    for attempt in range(POST_FIND_ATTEMPTS):
        if attempt:
            time.sleep(POST_FIND_DELAY_S)
        page.goto(PROFILE_URL, wait_until="domcontentloaded")
        try:
            post.wait_for(timeout=POST_FIND_WAIT_MS)
            print(f"Found the Facebook post on profile load {attempt + 1}.")
            return post
        except Exception:  # pylint: disable=broad-except
            continue  # not rendered yet; reload and look again
    return None


def comment_after_post(url: str, snippet: str, config: dict) -> None:
    """Wait for the staged post to be sent, then add url as its first comment.

    Every failure, from the composer never being sent to a changed layout, ends in the
    clipboard-and-notification fallback, so the link always reaches the user one way or
    the other.
    """
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    port = int(config.get("fb_debug_port", 9223))
    deadline = time.monotonic() + COMMENT_WATCH_S
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            while _composer_open(context):
                if time.monotonic() > deadline:
                    _comment_fallback(url, "the post was never sent")
                    return
                time.sleep(COMMENT_POLL_S)

            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            post = _find_post(page, snippet)
            if post is None:
                page.close()
                _comment_fallback(url, "the post isn't on the profile; was it discarded?")
                return
            post.get_by_role("button", name=COMMENT_OPENER).first.click()
            # Clicking Comment opens the box inline, or the whole post in a dialog.
            box = post.get_by_role("textbox", name=COMMENT_BOX).or_(
                page.get_by_role("dialog").get_by_role("textbox", name=COMMENT_BOX)
            ).first
            box.click()
            page.keyboard.type(url, delay=TYPE_DELAY_MS)
            page.keyboard.press("Enter")
            # The box clears once Facebook takes the comment.
            deadline = time.monotonic() + PAGE_TIMEOUT_MS / 1000
            while box.inner_text().strip() and time.monotonic() < deadline:
                time.sleep(0.5)
            if box.inner_text().strip():
                _comment_fallback(url, "the comment box didn't submit")
                return
            page.close()
    except PlaywrightError as exc:
        _comment_fallback(url, f"Playwright: {exc}")
        return

    _notify("Link added as the Facebook post's first comment.")
    print("Posted the Facebook first comment.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Facebook browser helper")
    parser.add_argument("--login", action="store_true", help="Open the promo_engine Chrome profile to sign in")
    parser.add_argument("--test", metavar="TEXT", help="Stage TEXT in the composer (won't post)")
    parser.add_argument("--comment", metavar="URL", help="After the staged post is sent, comment URL on it")
    parser.add_argument("--match", metavar="TEXT", help="With --comment: opening words of the post")
    args = parser.parse_args()

    from config import load_config
    config = load_config()
    if args.login:
        launch_chrome(config, FACEBOOK_HOME)
        print("Sign in to Facebook in the Chrome window that opened (tick 'remember me'), then close it.")
    elif args.comment and args.match:
        comment_after_post(args.comment, args.match, config)
    elif args.test:
        stage_facebook_post(args.test, config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
