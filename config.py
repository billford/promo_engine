import os
import sys
from dotenv import load_dotenv

load_dotenv()

REQUIRED_KEYS = ["ANTHROPIC_API_KEY"]

CLAUDE_MODEL = "claude-opus-4-7"
MEDIUM_RSS_URL = "https://medium.com/feed/@billfordx"
YOUTUBE_CHANNEL_HANDLE = "@billfordx"
PUBLORA_BASE_URL = "https://api.publora.com/api/v1"
COOLDOWN_DAYS = 30
# Hard floor on re-selecting the same article, applied even on the cooldown-exhausted
# fallback path. Counts dry runs so testing doesn't leak repeats into live posting.
RECENT_SELECTION_DAYS = 14
# How many eligible articles the scorer sees per pick, sampled evenly across years.
SHORTLIST_SIZE = 30
POST_HOUR = 9
# A photo is not reused on a live post within this window.
IMAGE_COOLDOWN_DAYS = 90
IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")


def load_config() -> dict:
    missing = [k for k in REQUIRED_KEYS if not os.getenv(k)]
    if missing:
        print(f"Missing required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    return {
        "anthropic_api_key": os.environ["ANTHROPIC_API_KEY"],
        "youtube_api_key": os.getenv("YOUTUBE_API_KEY"),
        "publora_api_key": os.getenv("PUBLORA_API_KEY"),
        "bluesky_handle": os.getenv("BLUESKY_HANDLE"),
        "bluesky_app_password": os.getenv("BLUESKY_APP_PASSWORD"),
        "timezone": os.getenv("TIMEZONE", "America/New_York"),
        "reminders_list": os.getenv("REMINDERS_LIST", "Social Posts"),
        "db_path": os.getenv("DB_PATH", "promo_engine.db"),
        # Optional: Negative Space photo library (Postgres). Unset = text-only posts.
        "photo_db_url": os.getenv("PHOTO_DB_URL"),
        "image_dir": IMAGE_DIR,
        # "browser" stages the post in Chrome for a one-click publish; "reminder" is the
        # old manual flow, and also the fallback whenever the browser path fails.
        "facebook_mode": os.getenv("FACEBOOK_MODE", "browser"),
        # Holds the logged-in Facebook session, so it lives outside the repo.
        "fb_browser_profile": os.getenv(
            "FB_BROWSER_PROFILE",
            os.path.expanduser("~/Library/Application Support/promo_engine/facebook-chrome"),
        ),
        "fb_debug_port": int(os.getenv("FB_DEBUG_PORT", "9223")),
    }
