"""Logging to stdout and a rotating file."""
import re, threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .config import LOG_BACKUPS, LOG_FILE, LOG_LEVEL, LOG_LEVELS, LOG_MAX_BYTES


# ---------- logging ----------

_log_lock = threading.Lock()
_log_handler = None
try:
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    _log_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=max(LOG_MAX_BYTES, 0), backupCount=LOG_BACKUPS, encoding="utf-8",
    )
except Exception:
    pass


def log(msg, level="info"):
    if LOG_LEVELS[level] < LOG_LEVELS[LOG_LEVEL]:
        return
    prefix = "" if level == "info" else f"{level.upper()} "
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {prefix}{msg}"
    with _log_lock:
        print(line, flush=True)
        if _log_handler:
            try:
                _log_handler.stream.write(line + "\n")
                _log_handler.stream.flush()
                if LOG_MAX_BYTES > 0 and _log_handler.stream.tell() >= LOG_MAX_BYTES:
                    _log_handler.doRollover()
            except Exception:
                pass


def redact_password(pwd):
    """Never log the SAS token: it is a valid credential for the real Azure IoT Hub."""
    text = pwd.decode("utf-8", errors="replace")
    redacted = re.sub(r"(sig=)[^&]+", r"\1<redacted>", text)
    if redacted == text and len(text) > 8:
        redacted = text[:8] + "...<redacted>"
    return redacted
