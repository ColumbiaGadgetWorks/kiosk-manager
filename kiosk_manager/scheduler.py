"""Day-of-week + time-of-day scheduling for wake / open events."""

import datetime
import logging

log = logging.getLogger("kiosk.scheduler")

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = {
    "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
    "fri": "Fri", "sat": "Sat", "sun": "Sun",
}

# How long after the nominal time an entry may still fire (covers a daemon
# that was asleep, or a machine that booted a few seconds late).
CATCHUP_SECONDS = 120


def parse_hhmm(text):
    try:
        hour, minute = str(text).split(":", 1)
        hour = int(hour)
        minute = int(minute)
    except (ValueError, AttributeError):
        return 0, 0
    return max(0, min(23, hour)), max(0, min(59, minute))


def format_days(days):
    days = [d for d in DAYS if d in (days or [])]
    if not days:
        return "never"
    if len(days) == 7:
        return "Every day"
    if days == ["mon", "tue", "wed", "thu", "fri"]:
        return "Weekdays"
    if days == ["sat", "sun"]:
        return "Weekends"
    return ", ".join(DAY_LABELS[d] for d in days)


def describe(entry):
    actions = []
    if entry.get("wake_screen", True):
        actions.append("wake screen")
    if entry.get("open_browser", True):
        actions.append("open page")
        if entry.get("return_to_home", True):
            actions.append("back to home URL")
    return "%s  %s  (%s)" % (
        entry.get("time", "--:--"),
        format_days(entry.get("days")),
        ", ".join(actions) or "nothing",
    )


def occurrence(entry, reference):
    """Datetime of this entry on the reference day, or None if not scheduled."""
    day = DAYS[reference.weekday()]
    if day not in (entry.get("days") or []):
        return None
    hour, minute = parse_hhmm(entry.get("time"))
    return reference.replace(hour=hour, minute=minute, second=0, microsecond=0)


def next_run(entry, now=None):
    now = now or datetime.datetime.now()
    if not entry.get("enabled", True):
        return None
    for offset in range(0, 8):
        day = now + datetime.timedelta(days=offset)
        when = occurrence(entry, day)
        if when and when > now:
            return when
    return None


def next_run_all(entries, now=None):
    now = now or datetime.datetime.now()
    upcoming = [(next_run(e, now), e) for e in entries or []]
    upcoming = [(w, e) for w, e in upcoming if w]
    if not upcoming:
        return None, None
    upcoming.sort(key=lambda pair: pair[0])
    return upcoming[0]


class Scheduler:
    """Tracks which entries have already fired so each one runs once."""

    def __init__(self):
        self._fired = set()

    def reset(self):
        self._fired.clear()

    def due(self, entries, now=None):
        now = now or datetime.datetime.now()
        result = []
        for entry in entries or []:
            if not entry.get("enabled", True):
                continue
            when = occurrence(entry, now)
            if not when:
                continue
            delta = (now - when).total_seconds()
            if delta < 0 or delta > CATCHUP_SECONDS:
                continue
            key = "%s@%s" % (entry.get("id"), when.isoformat())
            if key in self._fired:
                continue
            self._fired.add(key)
            result.append(entry)
        if len(self._fired) > 200:
            self._fired = set(list(self._fired)[-100:])
        return result
