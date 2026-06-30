import subprocess  # nosec B404 - used only for osascript macOS system calls
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def create_facebook_reminder(title: str, post_text: str, config: dict) -> None:
    """Create an Apple Reminder with the Facebook post text for manual posting."""
    list_name = config.get("reminders_list", "Social Posts")
    tz = ZoneInfo(config.get("timezone", "America/New_York"))
    now = datetime.now(tz)
    due_dt = now.replace(hour=10, minute=0, second=0, microsecond=0)
    if due_dt <= now:
        due_dt += timedelta(days=1)

    due_str = due_dt.strftime("%m/%d/%Y %I:%M:%S %p")
    reminder_title = f"Post to Facebook: {title}"

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        'tell application "Reminders"\n'
        f'    try\n'
        f'        set theList to list "{esc(list_name)}"\n'
        f'    on error\n'
        f'        make new list with properties {{name:"{esc(list_name)}"}}\n'
        f'        set theList to list "{esc(list_name)}"\n'
        f'    end try\n'
        f'    make new reminder in theList with properties '
        f'{{name:"{esc(reminder_title)}", body:"{esc(post_text)}", '
        f'due date:date "{due_str}", priority:5}}\n'
        f'    return name of result\n'
        f'end tell\n'
    )

    try:
        result = subprocess.run(  # nosec B603 B607
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        created_name = result.stdout.strip()
        print(f"Created Reminder: {created_name} (due {due_dt.strftime('%Y-%m-%d %H:%M %Z')})")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        if "not authorized" in stderr.lower() or "permission" in stderr.lower():
            raise RuntimeError(
                "Reminders access denied. "
                "Grant access in: System Settings → Privacy & Security → Reminders"
            ) from exc
        raise RuntimeError(f"Failed to create Reminder: {stderr}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Reminders creation timed out.") from exc
