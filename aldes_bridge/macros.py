"""Ventilation modes and high-level command macros."""
import re
from datetime import datetime, timedelta, timezone
from .logs import log


# Single source of truth for mode codes <-> labels (validated on Dee Fly Cube: X = max boost).
MODES = {
    "V": "Quotidien",
    "W": "Vacances",
    "X": "Boost",
    "Y": "Invités",
    "Z": "Programme",
}
SELECT_OPTIONS = ["Quotidien", "Vacances", "Invités", "Boost", "Programme"]



_VACANCES_RE = re.compile(r"vacances(\d+)?[jd]?")


HOLIDAY_DATE_FMT = "%Y%m%d%H%M%SZ"
# Start the holiday range slightly in the past: if the box's clock is a little behind,
# a start date of "now" would be in its future and the holiday would not begin.
HOLIDAY_START_MARGIN = timedelta(minutes=1)


def parse_holiday(param):
    """(start, end) datetimes of a changeMode "W<start><end>" parameter, else None."""
    if not isinstance(param, str) or len(param) != 31 or param[0] != "W":
        return None
    try:
        start = datetime.strptime(param[1:16], HOLIDAY_DATE_FMT).replace(tzinfo=timezone.utc)
        end = datetime.strptime(param[16:], HOLIDAY_DATE_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (start, end) if start.year > 1 else None


def macro_payload(macro, resume_mode="V"):
    """Translate a high-level macro name to the proper Aldes JSON-RPC payload.
    resume_mode: mode restored by "vacances_stop" (the one active before the holidays)."""
    m = macro.strip().lower()
    for a, b in (("é", "e"), ("è", "e"), ("ê", "e")):
        m = m.replace(a, b)
    if m in ("quotidien", "v", "v2"):
        return '{"method":"changeMode","params":["V"]}'
    if m in ("invites", "y", "v3", "guests", "kitchen", "cuisine"):
        return '{"method":"changeMode","params":["Y"]}'
    if m in ("boost", "x", "v4", "max"):
        return '{"method":"changeMode","params":["X"]}'
    if m in ("programme", "z"):
        return '{"method":"changeMode","params":["Z"]}'
    if m in ("override_on", "cmo_on"):
        return '{"method":"changeCMO","params":[1]}'
    if m in ("override_off", "cmo_off"):
        return '{"method":"changeCMO","params":[0]}'
    if m == "vacances_stop":
        # The Aldes app cancels with "W" + two 0001-01-01 dates, but a real box ignores it
        # (acknowledged, stays in W). Switching to another mode does end the holidays.
        return '{"method":"changeMode","params":["' + (resume_mode if resume_mode in ("V", "X", "Y", "Z") else "V") + '"]}'
    match = _VACANCES_RE.fullmatch(m)
    if m == "w" or match:
        # vacances / vacances<N>j / vacances<N>d → now → now + N days (default 1)
        days = int(match.group(1)) if match and match.group(1) else 1
        if not 1 <= days <= 365:
            log(f"macro '{macro}': holiday duration {days} days out of range (1-365)")
            return None
        now = datetime.now(timezone.utc)
        start = (now - HOLIDAY_START_MARGIN).strftime(HOLIDAY_DATE_FMT)
        end = (now + timedelta(days=days)).strftime(HOLIDAY_DATE_FMT)
        return '{"method":"changeMode","params":["W' + start + end + '"]}'
    return None
