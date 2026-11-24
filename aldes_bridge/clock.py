"""The Connect Box's clock, as seen from its SAS token.

At every boot the box's clock restarts at a fixed date in November 2016 (2016-11-12 00:00
UTC). It is set either by NTP, from a hard-coded address (61.164.36.105, UDP 123), which
puts it at UTC+8, or by the Date header of the answers to its HTTPS requests (port 443),
which puts it at UTC (see https.py). Its time is not in the
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

NTP_HINT = ("it has no time source: forward its HTTPS port 443 to the bridge (HTTPS_PORT), "
            "or let it reach its NTP server 61.164.36.105 (UDP 123) (README, 'Box clock')")


def parse_se(password_text):
    """Expiry (Unix time) of a SharedAccessSignature, or None."""
    m = _SE_RE.search(password_text or "")
    return int(m.group(1)) if m else None


def is_clock_set(se, now=None):
    now = time.time() if now is None else now
    return se >= now - _TOLERANCE
