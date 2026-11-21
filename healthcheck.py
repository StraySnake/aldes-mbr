"""Docker HEALTHCHECK: healthy when fake_iothub.py wrote "ok" to its health file recently.

The broker rewrites the file every 10 s; "ok" means its main loop is alive and the
upstream Mosquitto is connected. Exit code 0 = healthy, 1 = unhealthy.
"""
import os
import sys
import time
from pathlib import Path

MAX_AGE = 60  # seconds

path = Path(os.getenv("HEALTH_FILE", "/tmp/aldes-bridge.health"))
try:
    age = time.time() - path.stat().st_mtime
    status = path.read_text().strip()
except OSError as e:
    sys.exit(f"unhealthy: no health file ({e})")
if age > MAX_AGE:
    sys.exit(f"unhealthy: health file not updated for {age:.0f} s (broker stuck?)")
if status != "ok":
    sys.exit(f"unhealthy: {status}")
print("healthy")
