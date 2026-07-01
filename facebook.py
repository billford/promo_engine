import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import EventKit  # pylint: disable=import-error
from Foundation import NSDate, NSCalendar  # pylint: disable=import-error,no-name-in-module


def create_facebook_reminder(title: str, post_text: str, config: dict) -> None:
    """Create an Apple Reminder with the Facebook post text for manual posting."""
    list_name = config.get("reminders_list", "Social Posts")
    tz = ZoneInfo(config.get("timezone", "America/New_York"))
    now = datetime.now(tz)
    due_dt = now.replace(hour=10, minute=0, second=0, microsecond=0)
    if due_dt <= now:
        due_dt += timedelta(days=1)

    store = EventKit.EKEventStore.alloc().init()

    granted_event = threading.Event()
    access_result = [False]

    def _on_access(granted, _error):
        access_result[0] = bool(granted)
        granted_event.set()

    store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeReminder, _on_access)
    if not granted_event.wait(timeout=10):
        raise RuntimeError("Reminders access request timed out.")
    if not access_result[0]:
        raise RuntimeError(
            "Reminders access denied. "
            "Grant access in: System Settings → Privacy & Security → Reminders"
        )

    # Find or create the target list
    calendars = store.calendarsForEntityType_(EventKit.EKEntityTypeReminder)
    target_cal = next((c for c in calendars if c.title() == list_name), None)
    if target_cal is None:
        source = store.defaultCalendarForNewReminders().source()
        target_cal = EventKit.EKCalendar.calendarForEntityType_eventStore_(
            EventKit.EKEntityTypeReminder, store
        )
        target_cal.setTitle_(list_name)
        target_cal.setSource_(source)
        store.saveCalendar_commit_error_(target_cal, True, None)

    reminder_title = f"Post to Facebook: {title}"
    reminder = EventKit.EKReminder.reminderWithEventStore_(store)
    reminder.setTitle_(reminder_title)
    reminder.setNotes_(post_text)
    reminder.setCalendar_(target_cal)
    reminder.setPriority_(5)

    nsdate = NSDate.dateWithTimeIntervalSince1970_(due_dt.timestamp())
    ns_cal = NSCalendar.currentCalendar()
    units = (
        EventKit.NSCalendarUnitYear
        | EventKit.NSCalendarUnitMonth
        | EventKit.NSCalendarUnitDay
        | EventKit.NSCalendarUnitHour
        | EventKit.NSCalendarUnitMinute
    )
    components = ns_cal.components_fromDate_(units, nsdate)
    reminder.setDueDateComponents_(components)
    reminder.addAlarm_(EventKit.EKAlarm.alarmWithAbsoluteDate_(nsdate))

    success = store.saveReminder_commit_error_(reminder, True, None)
    if not success:
        raise RuntimeError("EventKit failed to save reminder.")

    print(f"Created Reminder: {reminder_title} (due {due_dt.strftime('%Y-%m-%d %H:%M %Z')})")
