"""Configuration from environment variables (read once, at import).

Fake Azure IoT Hub MQTT/TLS broker for Aldes Connect Box.

Architecture:
  Connect Box --(MQTT/TLS)--> [this fake broker] --(MQTT)--> [user's Mosquitto] --> Home Assistant

The box thinks it's talking to aldesiotsuite.azure-devices.net (DNS rewritten via Pi-hole).
This broker:
  - accepts the TLS connection with a self-signed cert (CN=aldesiotsuite.azure-devices.net)
  - parses MQTT packets (CONNECT auth, SUBSCRIBE topics, PUBLISH telemetry)
  - logs everything to a file
  - bridges captured PUBLISHes to the user's existing Mosquitto under a clean topic prefix
  - listens on the user's Mosquitto for command injections at <prefix>/cmd/+ and forwards to the box

Env vars:
  CERT_DIR               where to store the generated cert/key (default: /data)
  LISTEN_PORT            TLS listening port (default: 8883, the port the box uses)
  UPSTREAM_MQTT_HOST     bridge destination (default: 127.0.0.1)
  DEVICE_MAC             REQUIRED. The HF-LPB100 module MAC (12 hex chars, ':' or '-' OK)
  DEVICE_NAME            HA device name (default: "Aldes VMC")
  DEVICE_MODEL           HA device model (default: "Dee Fly Cube")
  DEVICE_IP              optional. Only accept connections from this IP (comma-separated
                         list allowed). Reserve the box's IP in your DHCP server first.
  UPSTREAM_MQTT_PORT     bridge port (default: 1883)
  UPSTREAM_MQTT_USER     optional auth
  UPSTREAM_MQTT_PASS     optional auth
  BRIDGE_TOPIC_PREFIX    topic prefix for republished messages (default: aldes/vmc)
  LOG_FILE               additional log file (default: /logs/mitm.log)
  SENSOR_EXPIRE_AFTER    seconds without a frame from the box before HA marks the sensors
                         unavailable (default: 900, 0 = never)
  CMD_QOS                QoS of the commands sent to the box: 1 (default, acknowledged)
                         or 0 (legacy; the box then drops the first 2 bytes of the payload)
  CMD_CONFIRM_TIMEOUT    seconds to wait for a telemetry frame confirming a mode change
                         (default: 900)
  LOG_LEVEL              debug | info | warning (default: info). "info" logs connections,
                         commands and the first telemetry frame of each connection;
                         "debug" logs every frame with its payload.
  TLS_CIPHERS            OpenSSL cipher string offered to the box (default: ALL:@SECLEVEL=0).
                         The suite the box negotiates is logged at each connection.
  UPSTREAM_MQTT_TLS      1 = connect to Mosquitto over TLS (default: 0)
  UPSTREAM_MQTT_CA       CA file to verify Mosquitto's certificate (default: system CAs)
  UPSTREAM_MQTT_TLS_INSECURE  1 = do not verify Mosquitto's certificate (self-signed)
  HEALTH_FILE            status file read by the Docker HEALTHCHECK (default:
                         /tmp/aldes-bridge.health)
  LOG_MAX_BYTES          rotate LOG_FILE above this size (default: 5 MB, 0 = no rotation)
  LOG_BACKUPS            number of rotated log files kept (default: 3)
"""
import ipaddress, os, re, sys
from pathlib import Path


# ---------- configuration ----------

def _env_num(name, default, cast=int, minimum=None):
    """Numeric env var, with a clear message instead of a traceback when it is invalid."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        sys.exit(f"FATAL: {name} must be {'an integer' if cast is int else 'a number'}, got {raw!r}")
    if minimum is not None and value < minimum:
        sys.exit(f"FATAL: {name} must be >= {minimum}, got {raw!r}")
    return value


def _env_bool(name):
    raw = os.getenv(name, "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    sys.exit(f"FATAL: {name} must be 0 or 1, got {raw!r}")


CERT_DIR = Path(os.getenv("CERT_DIR", "/data"))
CERT_FILE = CERT_DIR / "fake_azure.crt"
KEY_FILE = CERT_DIR / "fake_azure.key"
SERVER_CN = "aldesiotsuite.azure-devices.net"

UPSTREAM_HOST = os.getenv("UPSTREAM_MQTT_HOST", "127.0.0.1")
UPSTREAM_PORT = _env_num("UPSTREAM_MQTT_PORT", 1883, minimum=1)
UPSTREAM_USER = os.getenv("UPSTREAM_MQTT_USER", "") or None
UPSTREAM_PASS = os.getenv("UPSTREAM_MQTT_PASS", "") or None
UPSTREAM_TLS = _env_bool("UPSTREAM_MQTT_TLS")
UPSTREAM_CA = os.getenv("UPSTREAM_MQTT_CA", "") or None
UPSTREAM_TLS_INSECURE = _env_bool("UPSTREAM_MQTT_TLS_INSECURE")
PREFIX = os.getenv("BRIDGE_TOPIC_PREFIX", "aldes/vmc")
LOG_FILE = os.getenv("LOG_FILE", "/logs/mitm.log")
LOG_MAX_BYTES = _env_num("LOG_MAX_BYTES", 5 * 1024 * 1024, minimum=0)
LOG_BACKUPS = _env_num("LOG_BACKUPS", 3, minimum=0)
LOG_LEVELS = {"debug": 10, "info": 20, "warning": 30}
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").strip().lower() or "info"
if LOG_LEVEL not in LOG_LEVELS:
    sys.exit(f"FATAL: LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {LOG_LEVEL!r}")
TLS_CIPHERS = os.getenv("TLS_CIPHERS", "").strip() or "ALL:@SECLEVEL=0"
HEALTH_FILE = Path(os.getenv("HEALTH_FILE", "/tmp/aldes-bridge.health"))
SENSOR_EXPIRE_AFTER = _env_num("SENSOR_EXPIRE_AFTER", 900, minimum=0)
CMD_CONFIRM_TIMEOUT = _env_num("CMD_CONFIRM_TIMEOUT", 900.0, cast=float, minimum=1)
# Frames received this soon after a command may have been sampled before the box
# applied it: they neither confirm nor refute it.
CMD_CONFIRM_GRACE = _env_num("CMD_CONFIRM_GRACE", 3.0, cast=float, minimum=0)
# QoS of the commands sent to the box. The firmware reads a packet id in every PUBLISH
# it receives, even QoS 0 ones, and acknowledges it: with QoS 0 it eats the first 2
# bytes of the payload. QoS 1 gives it a real packet id, and its PUBACK confirms receipt.
CMD_QOS = _env_num("CMD_QOS", 1, minimum=0)
if CMD_QOS > 1:
    sys.exit(f"FATAL: CMD_QOS must be 0 or 1, got {CMD_QOS}")

# Device identity — DEVICE_MAC is REQUIRED
_DEVICE_MAC_RAW = os.getenv("DEVICE_MAC", "").upper().replace(":", "").replace("-", "")
if not re.fullmatch(r"[0-9A-F]{12}", _DEVICE_MAC_RAW):
    sys.exit("FATAL: DEVICE_MAC env var is required (12 hex chars, with or without ':')")
_DEVICE_SUFFIX = os.getenv("DEVICE_SUFFIX", "_AIR")  # Aldes appends "_AIR" for ventilation
DEVICE_MAC = _DEVICE_MAC_RAW
DEVICE_ID = os.getenv("DEVICE_ID", "") or (DEVICE_MAC + _DEVICE_SUFFIX)
DEVICE_NAME = os.getenv("DEVICE_NAME", "Aldes VMC")
DEVICE_MODEL = os.getenv("DEVICE_MODEL", "Dee Fly Cube")
HA_DISCO_PREFIX = os.getenv("HA_DISCOVERY_PREFIX", "homeassistant")


def parse_allowed_ips(raw):
    ips = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ips.add(str(ipaddress.ip_address(part)))
        except ValueError:
            sys.exit(f"FATAL: DEVICE_IP contains an invalid IP address: {part!r}")
    return ips


# Empty set = accept any client (default). The broker does not authenticate the box
# otherwise: the SAS token is signed with an Aldes key we do not have.
ALLOWED_IPS = parse_allowed_ips(os.getenv("DEVICE_IP", ""))

EVENTS_TOPIC = f"{PREFIX}/raw/devices/{DEVICE_ID}/messages/events/"
# Only PUBLISHes from the box's own session, on its own topics, are relayed to Mosquitto.
BOX_TOPIC_PREFIX = f"devices/{DEVICE_ID}/"
CMD_TOPIC = f"{PREFIX}/cmd/devices/{DEVICE_ID}/messages/devicebound"
# Availability: "online" while the bridge is up AND the box has an active session.
STATUS_TOPIC = f"{PREFIX}/status"
# ISO 8601 time of the last frame received from the box (retained).
LAST_SEEN_TOPIC = f"{PREFIX}/last_seen"
# Outcome of the last command (retained JSON). Not under <prefix>/cmd/: the bridge
# subscribes there and would forward it to the box.
CMD_RESULT_TOPIC = f"{PREFIX}/command_result"
# The box's telemetry topic, as published by the box (without our prefix).
BOX_EVENTS = f"devices/{DEVICE_ID}/messages/events/"

HOST = "0.0.0.0"
PORT = _env_num("LISTEN_PORT", 8883, minimum=1)  # the box always uses 8883; override for tests

# Timeouts (seconds)
HANDSHAKE_TIMEOUT = 30      # TLS handshake + first CONNECT packet
DEFAULT_KEEPALIVE = 180     # used when the box announces keepalive=0
# The box sends ~1 kB telemetry frames; refuse anything absurd instead of buffering it.
MAX_PACKET_SIZE = 64 * 1024
# How often the connection thread checks for commands queued by other threads.
POLL_INTERVAL = 0.2
