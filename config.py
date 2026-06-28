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
POST_HOUR = 9


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
    }
