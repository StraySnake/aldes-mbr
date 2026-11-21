"""Merging of the box's telemetry frames."""
import json, threading


# ---------- telemetry merge ----------
#
# The box sends two kinds of frames on the same topic: a short boot heartbeat
# (firmware, RSSI...) right after connecting, and the full telemetry every 10 min.
# Republishing the heartbeat as-is would turn every sensor to "unknown" until the
# next full frame, so each frame is merged into the last known values instead.

_telemetry = {}
_telemetry_lock = threading.Lock()


def merge_telemetry(frame):
    """Merge a decoded frame into the last known values; returns the merged dict."""
    with _telemetry_lock:
        _telemetry.update(frame)
        return dict(_telemetry)


def seed_telemetry(values):
    """Initial values (from Mosquitto's retained copy) after a restart of the bridge.
    Ignored once the box has sent something: live data wins."""
    with _telemetry_lock:
        if _telemetry:
            return False
        _telemetry.update(values)
        return True


def decode_frame(msg):
    """JSON object of a telemetry frame, or None if it is not one."""
    try:
        frame = json.loads(msg)
    except (ValueError, UnicodeDecodeError):
        return None
    return frame if isinstance(frame, dict) else None
