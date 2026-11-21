"""The Connect Box's clock, as seen from its SAS token.

At every boot the box's clock restarts at a fixed date in November 2016; it is only set
by NTP from a hard-coded address (61.164.36.105, UDP 123). Its time is not in the
telemetry, but at each connection it signs a SAS token whose expiry ("se=", Unix time)
is computed from its own clock: still in 2016 means the clock is not set, and then
holiday commands are acknowledged but never start.
"""
import re
import time

_SE_RE = re.compile(r"(?:^|&)se=(\d+)")

# A set clock gives an expiry a few hours ahead of now; an unset one, years behind.
# One day of tolerance covers any time zone offset or token lifetime.
_TOLERANCE = 24 * 3600

NTP_HINT = ("it cannot reach its NTP server 61.164.36.105 (UDP 123): allow it or redirect it "
            "to a local NTP server (README, 'Box clock (NTP)')")


def parse_se(password_text):
    """Expiry (Unix time) of a SharedAccessSignature, or None."""
    m = _SE_RE.search(password_text or "")
    return int(m.group(1)) if m else None


def is_clock_set(se, now=None):
    now = time.time() if now is None else now
    return se >= now - _TOLERANCE
