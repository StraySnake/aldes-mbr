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


def macro_payload(macro):
    """Translate a high-level macro name to the proper Aldes JSON-RPC payload."""
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
        # Cancelling holidays: send epoch placeholder per APK
        epoch = "00010101000000Z"
        return '{"method":"changeMode","params":["W' + epoch + epoch + '"]}'
    match = _VACANCES_RE.fullmatch(m)
    if m == "w" or match:
        # vacances / vacances<N>j / vacances<N>d → now → now + N days (default 1)
        days = int(match.group(1)) if match and match.group(1) else 1
        if not 1 <= days <= 365:
            log(f"macro '{macro}': holiday duration {days} days out of range (1-365)")
            return None
        fmt = "%Y%m%d%H%M%SZ"
        now = datetime.now(timezone.utc)
        start = now.strftime(fmt)
        end = (now + timedelta(days=days)).strftime(fmt)
        return '{"method":"changeMode","params":["W' + start + end + '"]}'
    return None
