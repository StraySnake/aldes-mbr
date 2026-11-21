"""Commands from Mosquitto to the box, and their confirmation through the telemetry."""
import json, threading, time
from datetime import datetime, timezone
from .config import CMD_CONFIRM_GRACE, CMD_CONFIRM_TIMEOUT, DEVICE_ID, PREFIX
from .logs import log
from .macros import MODES, macro_payload
from .box import _sessions_lock, box_online, send_publish_to_box, sessions
from . import runtime


# ---------- command confirmation ----------
#
# Commands go to the box as QoS 0: nothing says it got them. The telemetry does say
# which mode it is in, so a mode change is confirmed (or not) by the next frames.

CMD_STATUS_LABELS = {
    "pending": "En attente",
    "confirmed": "Confirmée",
    "retrying": "Renvoyée",
    "failed": "Échec",
    "unconfirmed": "Non confirmée",
    "sent": "Envoyée",
    "received": "Reçue par la box",
    "not_connected": "Box déconnectée",
}
_HOLIDAY_STOP = "W" + "00010101000000Z" * 2


def is_mode_change(payload):
    try:
        return json.loads(payload).get("method") == "changeMode"
    except (ValueError, AttributeError):
        return False


def expected_mode(payload):
    """What current_mode should become after this command: ("is", code), ("not", code),
    or None when the telemetry cannot tell (changeCMO, unknown payloads)."""
    try:
        cmd = json.loads(payload)
        param = cmd["params"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if cmd.get("method") != "changeMode" or not isinstance(param, str) or not param:
        return None
    if param == _HOLIDAY_STOP:
        return ("not", "W")
    code = param[0]
    if code not in MODES:
        return None
    # Z (Programme) included: a real box reports current_mode "Z", not the scheduled mode.
    return ("is", code)


class CommandTracker:
    """Follows the last command sent to the box.

    1. "pending" (mode change) or "sent" (cannot be checked by the telemetry).
    2. "received" when the box acknowledges it (PUBACK, commands sent with QoS 1).
    3. For mode changes, the next frame carrying current_mode (after CMD_CONFIRM_GRACE)
       confirms it, or triggers one resend; a second mismatch is a failure.
    A newer command replaces the previous one.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last = None  # dict describing the last command sent

    def _publish(self, info, status, reported=None):
        result = {
            "command": info["label"],
            "status": status,
            "expected_mode": info.get("expected"),
            "reported_mode": reported,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        log(f"command '{info['label']}': {status}" + (f" (box reports {reported})" if reported else ""))
        if runtime.bridge:
            runtime.bridge.publish("command_result", json.dumps(result, ensure_ascii=False), qos=1, retain=True)

    def not_connected(self, label):
        self._publish({"label": label}, "not_connected")

    def sent(self, label, topic, payload, pkt_id=0):
        expect = expected_mode(payload)
        info = {
            "label": label, "topic": topic, "payload": payload, "pkt_id": pkt_id,
            "expect": expect, "expected": None if expect is None else ("!" if expect[0] == "not" else "") + expect[1],
            "is_mode_change": is_mode_change(payload),
            "sent_at": time.monotonic(), "retried": False, "acked": False, "done": False, "observed": False,
            "status": "pending" if expect else "sent", "timer": None,
        }
        with self._lock:
            if self._last and self._last["timer"]:
                self._last["timer"].cancel()
            self._last = info
            if expect:
                info["timer"] = threading.Timer(CMD_CONFIRM_TIMEOUT, self._timeout, args=(info,))
                info["timer"].daemon = True
                info["timer"].start()
        self._publish(info, info["status"])

    def on_puback(self, pkt_id):
        """The box acknowledged a PUBLISH. True if it was the last command."""
        with self._lock:
            info = self._last
            if not info or not pkt_id or info["pkt_id"] != pkt_id or info["acked"]:
                return False
            info["acked"] = True
            if info["done"] or info["retried"]:
                # Keep the outcome, or "retrying": "received" would look like a step back.
                log(f"command '{info['label']}': resend acknowledged by the box", "debug")
                return True
            info["status"] = "received"
        self._publish(info, "received")
        return True

    def _timeout(self, info):
        with self._lock:
            if self._last is not info or info["done"]:
                return
            info["done"] = True
        self._publish(info, "unconfirmed")

    def on_frame(self, frame):
        mode = frame.get("current_mode")
        if not isinstance(mode, str) or not mode:
            return  # boot heartbeat: says nothing about the mode
        with self._lock:
            p = self._last
            if p is None or p["done"] or time.monotonic() - p["sent_at"] < CMD_CONFIRM_GRACE:
                return
            if p["expect"] is None:
                # Not verifiable (Programme): still record what the box reports, once.
                if not p["is_mode_change"] or p["observed"]:
                    return
                p["observed"] = True
                status = p["status"]
            else:
                kind, code = p["expect"]
                ok = (mode[0] == code) if kind == "is" else (mode[0] != code)
                if ok or p["retried"]:
                    p["timer"].cancel()
                    p["done"] = True
                    status = "confirmed" if ok else "failed"
                else:
                    p["retried"] = True
                    p["acked"] = False
                    p["sent_at"] = time.monotonic()
                    status = "retrying"
        if status == "retrying":
            pkt_id = send_publish_to_box(DEVICE_ID, p["topic"], p["payload"])
            if pkt_id is None:
                status = "failed"
                with self._lock:
                    p["timer"].cancel()
                    p["done"] = True
            else:
                with self._lock:
                    p["pkt_id"] = pkt_id
        self._publish(p, status, reported=mode)


tracker = CommandTracker()


def cmd_callback(topic, payload):
    """Forward injection commands to the box.
    Two routes:
      aldes/vmc/cmd/macro/<name>   -> macro translation (V1/V3/Boost/Vacances1J/...)
      aldes/vmc/cmd/<azure_topic>  -> raw forward (e.g. devices/.../messages/devicebound)
    """
    if not topic.startswith(f"{PREFIX}/cmd/"):
        return
    relative = topic[len(f"{PREFIX}/cmd/"):]
    if not payload:
        # Clearing a retained message publishes an empty payload: it must not trigger
        # the macro (macros are chosen by topic name, the payload is not used).
        log(f"cmd: empty payload on {topic}, ignored")
        return

    # Macro mode:
    #   aldes/vmc/cmd/macro/<name>           -> use <name>
    #   aldes/vmc/cmd/macro/_select payload  -> use payload as macro name (HA select)
    label = "raw"
    if relative.startswith("macro/"):
        macro_name = relative[len("macro/"):]
        if macro_name == "_select":
            macro_name = payload.decode("utf-8", errors="replace").strip()
        translated = macro_payload(macro_name)
        if not translated:
            log(f"macro '{macro_name}' unknown, dropped")
            return
        relative = f"devices/{DEVICE_ID}/messages/devicebound"
        payload = translated.encode()
        log(f"macro '{macro_name}' -> {translated}")
        label = macro_name

    # Only ever send to the configured box, never to another LAN client that connected.
    if not box_online():
        with _sessions_lock:
            others = list(sessions)
        hint = f" (connected client_ids: {others})" if others else ""
        log(f"cmd: box {DEVICE_ID} not connected, dropped {topic}{hint}")
        tracker.not_connected(label)
        return
    pkt_id = send_publish_to_box(DEVICE_ID, relative, payload)
    if pkt_id is not None:
        tracker.sent(label, relative, payload, pkt_id)
