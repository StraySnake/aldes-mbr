"""
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
  LOG_MAX_BYTES          rotate LOG_FILE above this size (default: 5 MB, 0 = no rotation)
  LOG_BACKUPS            number of rotated log files kept (default: 3)
"""
import ipaddress, json, os, re, signal, socket, ssl, struct, sys, threading, time, warnings
from collections import deque
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

CERT_DIR = Path(os.getenv("CERT_DIR", "/data"))
CERT_DIR.mkdir(parents=True, exist_ok=True)
CERT_FILE = CERT_DIR / "fake_azure.crt"
KEY_FILE = CERT_DIR / "fake_azure.key"
SERVER_CN = "aldesiotsuite.azure-devices.net"

UPSTREAM_HOST = os.getenv("UPSTREAM_MQTT_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.getenv("UPSTREAM_MQTT_PORT", "1883"))
UPSTREAM_USER = os.getenv("UPSTREAM_MQTT_USER", "") or None
UPSTREAM_PASS = os.getenv("UPSTREAM_MQTT_PASS", "") or None
PREFIX = os.getenv("BRIDGE_TOPIC_PREFIX", "aldes/vmc")
LOG_FILE = os.getenv("LOG_FILE", "/logs/mitm.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUPS = int(os.getenv("LOG_BACKUPS", "3"))

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


def _parse_allowed_ips(raw):
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
ALLOWED_IPS = _parse_allowed_ips(os.getenv("DEVICE_IP", ""))

EVENTS_TOPIC = f"{PREFIX}/raw/devices/{DEVICE_ID}/messages/events/"
# Only PUBLISHes from the box's own session, on its own topics, are relayed to Mosquitto.
BOX_TOPIC_PREFIX = f"devices/{DEVICE_ID}/"
CMD_TOPIC = f"{PREFIX}/cmd/devices/{DEVICE_ID}/messages/devicebound"
# Availability: "online" while the bridge is up AND the box has an active session.
STATUS_TOPIC = f"{PREFIX}/status"

HOST = "0.0.0.0"
PORT = 8883

# Timeouts (seconds)
HANDSHAKE_TIMEOUT = 30      # TLS handshake + first CONNECT packet
DEFAULT_KEEPALIVE = 180     # used when the box announces keepalive=0
# The box sends ~1 kB telemetry frames; refuse anything absurd instead of buffering it.
MAX_PACKET_SIZE = 64 * 1024
# How often the connection thread checks for commands queued by other threads.
POLL_INTERVAL = 0.2


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


def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
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


# ---------- self-signed cert ----------

def gen_cert():
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    log(f"generating self-signed cert for {SERVER_CN}...")
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "FR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FakeAzure"),
        x509.NameAttribute(NameOID.COMMON_NAME, SERVER_CN),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365 * 10))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(SERVER_CN),
                x509.DNSName("*.azure-devices.net"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_FILE.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    log(f"cert written: {CERT_FILE}")


# ---------- minimal MQTT 3.1.1 codec ----------

CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = (
    1, 2, 3, 4, 8, 9, 10, 11, 12, 13, 14
)


class ConnectionClosed(Exception):
    pass


def recv_exact(s, n):
    """Read exactly n bytes, or raise ConnectionClosed if the peer goes away mid-packet."""
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionClosed()
        buf += chunk
    return bytes(buf)


def read_remaining_length(s):
    multiplier = 1
    value = 0
    for _ in range(4):
        digit = recv_exact(s, 1)[0]
        value += (digit & 0x7F) * multiplier
        if (digit & 0x80) == 0:
            return value
        multiplier *= 128
    raise ValueError("malformed remaining length")


def encode_remaining_length(length):
    out = b""
    while True:
        digit = length & 0x7F
        length >>= 7
        if length > 0:
            digit |= 0x80
        out += bytes([digit])
        if length == 0:
            break
    return out


def parse_string(buf, off):
    n = struct.unpack_from(">H", buf, off)[0]
    return buf[off + 2: off + 2 + n].decode("utf-8", errors="replace"), off + 2 + n


def encode_string(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


# ---------- box sessions ----------

# client_id -> Session. Guarded by _sessions_lock.
sessions = {}
_sessions_lock = threading.Lock()


class Session:
    """One box connection.

    All TLS I/O happens in the connection's own thread: OpenSSL does not support
    reading and writing one SSL object from two threads at once. Other threads
    (paho's, for injected commands) queue their packets; the connection thread
    sends them within POLL_INTERVAL.
    """

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.client_id = None
        self.subs = []
        self.owner = threading.get_ident()
        self._outbox = deque()

    def send(self, data):
        if threading.get_ident() == self.owner:
            self.sock.sendall(data)
        else:
            self._outbox.append(data)

    def flush(self):
        """Owner thread only: send packets queued by other threads."""
        while self._outbox:
            self.sock.sendall(self._outbox.popleft())

    def kill(self):
        """Any thread: abort the connection at the TCP level, without touching the SSL
        object. The owner thread then sees EOF/an error and cleans up."""
        try:
            socket.socket.shutdown(self.sock, socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def box_online():
    with _sessions_lock:
        return DEVICE_ID in sessions


# ---------- upstream bridge to user's Mosquitto ----------

class UpstreamBridge:
    def __init__(self):
        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="aldes-mitm-bridge",
            clean_session=True,
        )
        if UPSTREAM_USER:
            self.client.username_pw_set(UPSTREAM_USER, UPSTREAM_PASS)
        self.client.will_set(STATUS_TOPIC, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.cmd_callbacks = []  # list of fn(topic, payload) called when user injects a cmd
        self._status_lock = threading.Lock()  # keeps "compute status" + "publish" atomic
        # connect_async + loop_start: paho keeps retrying in the background, so the bridge
        # comes up even if Mosquitto starts after us (e.g. NAS boot).
        self.client.connect_async(UPSTREAM_HOST, UPSTREAM_PORT, keepalive=60)
        self.client.loop_start()
        log(f"upstream MQTT connecting to {UPSTREAM_HOST}:{UPSTREAM_PORT}...")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log(f"upstream MQTT connection refused: {reason_code}")
            return
        log(f"upstream MQTT connected to {UPSTREAM_HOST}:{UPSTREAM_PORT}")
        # Subscribe to command-injection topic so HA can publish e.g. aldes/vmc/cmd/macro/boost
        client.subscribe(f"{PREFIX}/cmd/#")
        publish_ha_discovery(client)
        self.publish_status()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log(f"upstream MQTT disconnected: {reason_code} (will retry)")

    def _on_message(self, client, userdata, msg):
        if msg.retain:
            # A command published with retain=true would be replayed by Mosquitto at every
            # (re)connect of the bridge, e.g. restarting holidays after each reboot.
            log(f"upstream RX cmd topic={msg.topic} IGNORED: retained message "
                f"(publish commands with retain=false; clear it by publishing an empty retained message)")
            return
        log(f"upstream RX cmd topic={msg.topic} payload={msg.payload!r}")
        for cb in self.cmd_callbacks:
            try:
                cb(msg.topic, msg.payload)
            except Exception as e:
                log(f"cmd callback err: {e}")

    def publish(self, subtopic, payload, qos=0, retain=False):
        topic = f"{PREFIX}/{subtopic}"
        try:
            self.client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as e:
            log(f"upstream publish err: {e}")

    def publish_status(self):
        # Under a lock so that a session closing and a session opening at the same time
        # cannot publish their states in the wrong order (stale "offline" after "online").
        with self._status_lock:
            try:
                self.client.publish(STATUS_TOPIC, "online" if box_online() else "offline",
                                    qos=1, retain=True)
            except Exception as e:
                log(f"upstream status publish err: {e}")

    def stop(self):
        try:
            with self._status_lock:
                info = self.client.publish(STATUS_TOPIC, "offline", qos=1, retain=True)
            info.wait_for_publish(2)
        except Exception:
            pass
        self.client.disconnect()
        self.client.loop_stop()


bridge = None


# ---------- Home Assistant MQTT Discovery ----------

DEVICE_BLOCK = {
    "identifiers": [f"aldes_vmc_{DEVICE_MAC}"],
    "name": DEVICE_NAME,
    "manufacturer": "Aldes",
    "model": DEVICE_MODEL,
    "sw_version": "Connect Box (HF-LPB100)",
}

AVAILABILITY = {
    "availability_topic": STATUS_TOPIC,
    "payload_available": "online",
    "payload_not_available": "offline",
}

# Each tuple: (object_id, friendly_name, payload_key, dict_extra)
SENSORS = [
    ("outside_temperature", "Air extérieur",        "outside_tpt",  {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}),
    ("indoor_temperature",  "Air intérieur (extrait)", "ext_tpt",   {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}),
    ("reject_temperature",  "Air rejeté",            "reject_tpt",  {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}),
    ("extract_flow",        "Débit extraction",      "extf_flw",    {"unit_of_measurement": "m³/h", "icon": "mdi:fan-chevron-down", "state_class": "measurement"}),
    ("supply_flow",         "Débit insufflation",    "ffe_flw",     {"unit_of_measurement": "m³/h", "icon": "mdi:fan-chevron-up", "state_class": "measurement"}),
    ("extract_speed",       "Vitesse extraction",    "extf_spd",    {"unit_of_measurement": "rpm", "icon": "mdi:fan", "state_class": "measurement"}),
    ("supply_speed",        "Vitesse insufflation",  "vi_spd",      {"unit_of_measurement": "rpm", "icon": "mdi:fan", "state_class": "measurement"}),
    ("exchanger_power",     "Puissance échangeur",   "echange_pwr", {"device_class": "power", "unit_of_measurement": "W", "state_class": "measurement"}),
    ("set_speed",           "Vitesse demandée",      "set_spd",     {"icon": "mdi:speedometer", "state_class": "measurement"}),
    ("co2",                 "CO₂",                   "Co2_vmc",     {"device_class": "carbon_dioxide", "unit_of_measurement": "ppm", "state_class": "measurement"}),
    ("pm25",                "PM2.5",                 "Pm25_Vmc",    {"device_class": "pm25", "unit_of_measurement": "µg/m³", "state_class": "measurement"}),
    ("voc",                 "COV",                   "Voc_Vmc",     {"icon": "mdi:molecule", "state_class": "measurement"}),
    ("rssi",                "Signal WiFi",           "RSSI",        {"icon": "mdi:wifi", "entity_category": "diagnostic"}),
    ("fw_wifi",             "Firmware WiFi",         "Vers_W",      {"icon": "mdi:chip", "entity_category": "diagnostic"}),
    ("fw_uc",               "Firmware MCU",          "Vers_UC",     {"icon": "mdi:chip", "entity_category": "diagnostic"}),
]

# Single source of truth for mode codes <-> labels (validated on Dee Fly Cube: X = max boost).
MODES = {
    "V": "Quotidien",
    "W": "Vacances",
    "X": "Boost",
    "Y": "Invités",
    "Z": "Programme",
}
SELECT_OPTIONS = ["Quotidien", "Vacances", "Invités", "Boost", "Programme"]


def publish_ha_discovery(client):
    """Publish HA MQTT Discovery configs for all VMC entities. Called on each upstream connect."""
    log("publishing HA discovery...")
    base_id = f"aldes_vmc_{DEVICE_MAC}"

    # Sensors
    for obj_id, name, key, extra in SENSORS:
        cfg = {
            "name": name,
            "unique_id": f"{base_id}_{obj_id}",
            "state_topic": EVENTS_TOPIC,
            "value_template": f"{{{{ value_json.{key} | default(none) }}}}",
            "device": DEVICE_BLOCK,
            **AVAILABILITY,
            **extra,
        }
        topic = f"{HA_DISCO_PREFIX}/sensor/{base_id}/{obj_id}/config"
        client.publish(topic, json.dumps(cfg), qos=1, retain=True)

    # Mode select. "Vacances" auto-resolves now -> now+1d via the macro translator
    # (date math is hard in Jinja). The boot heartbeat has no current_mode: keep the
    # previous state in that case (HA ignores an empty template output).
    mode_map = json.dumps(MODES, ensure_ascii=False).replace('"', "'")
    mode_select_cfg = {
        "name": "Mode",
        "unique_id": f"{base_id}_mode",
        "state_topic": EVENTS_TOPIC,
        "value_template": (
            "{% set m = " + mode_map + " %}"
            "{% set code = (value_json.current_mode | default(''))[:1] %}"
            "{{ m[code] if code in m else '' }}"
        ),
        "command_topic": f"{PREFIX}/cmd/macro/_select",
        "command_template": "{{ value }}",
        "options": SELECT_OPTIONS,
        "icon": "mdi:fan-clock",
        "device": DEVICE_BLOCK,
        **AVAILABILITY,
    }
    client.publish(
        f"{HA_DISCO_PREFIX}/select/{base_id}/mode/config",
        json.dumps(mode_select_cfg), qos=1, retain=True,
    )

    # Quick-action buttons via macro topics (broker translates server-side)
    macro_topic = f"{PREFIX}/cmd/macro"
    macros = [
        ("quotidien",     "Quotidien",         "mdi:fan-speed-2"),
        ("invites",       "Invités",           "mdi:fan-speed-3"),
        ("boost",         "Boost",             "mdi:fan-plus"),
        ("vacances1j",    "Vacances 1 jour",   "mdi:bag-suitcase"),
        ("vacances7j",    "Vacances 7 jours",  "mdi:bag-suitcase"),
        ("vacances_stop", "Annuler vacances",  "mdi:bag-suitcase-off"),
        ("programme",     "Programme",         "mdi:fan-clock"),
    ]
    for key, name, icon in macros:
        cfg = {
            "name": name,
            "unique_id": f"{base_id}_btn_{key}",
            "command_topic": f"{macro_topic}/{key}",
            "payload_press": "1",
            "icon": icon,
            "device": DEVICE_BLOCK,
            **AVAILABILITY,
        }
        client.publish(
            f"{HA_DISCO_PREFIX}/button/{base_id}/btn_{key}/config",
            json.dumps(cfg), qos=1, retain=True,
        )

    log(f"HA discovery: {len(SENSORS)} sensors + 1 select + {len(macros)} buttons")


# ---------- per-client MQTT handler ----------

def send_publish_to_box(client_id, topic, payload):
    """Inject a QoS 0 PUBLISH back to a connected device — used for command injection."""
    with _sessions_lock:
        sess = sessions.get(client_id)
    if not sess:
        log(f"inject: client {client_id} not connected")
        return False
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    body = encode_string(topic) + payload
    pkt = bytes([0x30]) + encode_remaining_length(len(body)) + body
    try:
        sess.send(pkt)  # queued, written by the session's own thread
        log(f"inject: queued for {client_id} topic={topic} ({len(payload)} bytes)")
        return True
    except Exception as e:
        log(f"inject err: {e}")
        return False


def _unregister(sess):
    """Remove the session only if it is still the registered one for its client_id
    (a newer connection from the same box may already have replaced it)."""
    if not sess.client_id:
        return False
    with _sessions_lock:
        if sessions.get(sess.client_id) is sess:
            del sessions[sess.client_id]
            return True
    return False


def handle_client(raw_sock, addr):
    log(f"NEW CONNECTION from {addr}")
    sess = None
    tls_sock = None
    try:
        raw_sock.settimeout(HANDSHAKE_TIMEOUT)
        tls_sock = ctx.wrap_socket(raw_sock, server_side=True, do_handshake_on_connect=False)
        tls_sock.do_handshake()
        sess = Session(tls_sock, addr)
        idle_timeout = HANDSHAKE_TIMEOUT  # until CONNECT, then 1.5x keepalive
        last_rx = time.monotonic()

        while True:
            tls_sock.settimeout(idle_timeout)
            sess.flush()
            # Short recv timeout to pick up queued commands regularly. Not select():
            # OpenSSL may already hold the next record in memory (e.g. CONNECT read
            # together with the handshake), and select() would not see it.
            tls_sock.settimeout(POLL_INTERVAL)
            try:
                head = tls_sock.recv(1)
            except socket.timeout:
                if time.monotonic() - last_rx > idle_timeout:
                    raise
                continue
            if not head:
                break
            tls_sock.settimeout(idle_timeout)  # bounds a stalled mid-packet read
            ptype = (head[0] >> 4) & 0xF
            flags = head[0] & 0xF
            length = read_remaining_length(tls_sock)
            if length > MAX_PACKET_SIZE:
                raise ValueError(f"packet too large ({length} bytes, max {MAX_PACKET_SIZE})")
            payload = recv_exact(tls_sock, length)
            last_rx = time.monotonic()

            if sess.client_id is None and ptype != CONNECT:
                # MQTT 3.1.1 §3.1: the first packet must be CONNECT.
                log(f"{addr} packet type={ptype} before CONNECT, closing")
                break

            elif ptype == CONNECT and sess.client_id is not None:
                # MQTT 3.1.1 §3.1: a second CONNECT is a protocol violation -> disconnect.
                # (Otherwise the first client_id would stay registered on a dead session.)
                log(f"{addr} second CONNECT on the same connection, closing")
                break

            elif ptype == CONNECT:
                proto, off = parse_string(payload, 0)
                level = payload[off]; off += 1
                cflags = payload[off]; off += 1
                keepalive = struct.unpack_from(">H", payload, off)[0]; off += 2
                cid, off = parse_string(payload, off)
                username = pwd = None
                if cflags & 0x04:  # will flag: skip will topic + will message
                    _, off = parse_string(payload, off)
                    wlen = struct.unpack_from(">H", payload, off)[0]
                    off += 2 + wlen
                if cflags & 0x80:
                    username, off = parse_string(payload, off)
                if cflags & 0x40:
                    pwlen = struct.unpack_from(">H", payload, off)[0]
                    pwd = payload[off+2:off+2+pwlen]
                    off += 2 + pwlen
                log(f"{addr} CONNECT proto={proto} level={level} keepalive={keepalive}")
                log(f"{addr}   client_id = {cid}")
                log(f"{addr}   username  = {username}")
                if pwd:
                    log(f"{addr}   password  = {redact_password(pwd)}")
                if cid != DEVICE_ID:
                    log(f"{addr}   WARNING: client_id != DEVICE_ID ({DEVICE_ID}); "
                        f"commands will NOT be routed to this client. Check DEVICE_MAC / DEVICE_ID.")
                # MQTT spec: close the connection if nothing is received within 1.5x keepalive.
                idle_timeout = (keepalive or DEFAULT_KEEPALIVE) * 1.5
                tls_sock.settimeout(idle_timeout)
                sess.client_id = cid
                with _sessions_lock:
                    old = sessions.get(cid)
                    sessions[cid] = sess
                if old is not None:
                    # Same box reconnected: drop the stale (half-open) socket.
                    log(f"{addr}   replacing previous session from {old.addr}")
                    old.kill()
                # Reply CONNACK accepted (session_present=0, return_code=0)
                sess.send(bytes([0x20, 0x02, 0x00, 0x00]))
                if bridge:
                    bridge.publish("_meta/connect", f"{cid} from {addr[0]}")
                    bridge.publish_status()

            elif ptype == PUBLISH:
                qos = (flags >> 1) & 0x3
                topic, off = parse_string(payload, 0)
                pkt_id = None
                if qos > 0:
                    pkt_id = struct.unpack_from(">H", payload, off)[0]; off += 2
                msg = payload[off:]
                preview = msg[:300]
                log(f"{addr} PUBLISH qos={qos} topic={topic} ({len(msg)}b)")
                log(f"{addr}   payload = {preview!r}{'...' if len(msg)>300 else ''}")
                if qos == 1 and pkt_id is not None:
                    sess.send(bytes([0x40, 0x02]) + struct.pack(">H", pkt_id))
                elif qos == 2 and pkt_id is not None:
                    # Not expected from the box; acknowledge with PUBREC so it doesn't stall.
                    sess.send(bytes([0x50, 0x02]) + struct.pack(">H", pkt_id))
                # Bridge to upstream — retain=true so HA has the last value after reloads.
                # Only the box's own telemetry: another LAN client must not be able to
                # inject values into HA or fill Mosquitto with retained topics.
                if sess.client_id != DEVICE_ID or not topic.startswith(BOX_TOPIC_PREFIX):
                    log(f"{addr}   not relayed (only {BOX_TOPIC_PREFIX}... from client_id {DEVICE_ID})")
                elif bridge:
                    bridge.publish(f"raw/{topic.replace('$', '_').replace('#', '_').replace('+', '_')}",
                                   msg, retain=True)

            elif ptype == SUBSCRIBE:
                pkt_id = struct.unpack_from(">H", payload, 0)[0]
                off = 2
                topics = []
                while off < len(payload):
                    t, off = parse_string(payload, off)
                    q = payload[off]; off += 1
                    topics.append((t, q))
                    sess.subs.append(t)
                log(f"{addr} SUBSCRIBE pkt_id={pkt_id}")
                for t, q in topics:
                    log(f"{addr}   topic = {t!r}  qos={q}")
                body = struct.pack(">H", pkt_id) + bytes([min(q, 1) for _, q in topics])
                sess.send(bytes([0x90]) + encode_remaining_length(len(body)) + body)

            elif ptype == UNSUBSCRIBE:
                pkt_id = struct.unpack_from(">H", payload, 0)[0]
                log(f"{addr} UNSUBSCRIBE pkt_id={pkt_id}")
                sess.send(bytes([0xB0, 0x02]) + struct.pack(">H", pkt_id))

            elif ptype == PINGREQ:
                sess.send(bytes([0xD0, 0x00]))

            elif ptype == PUBACK:
                pass  # we only send QoS 0, but tolerate it

            elif ptype == DISCONNECT:
                log(f"{addr} DISCONNECT")
                break
            else:
                log(f"{addr} unknown packet type={ptype} flags={flags} len={length}")
                if payload:
                    log(f"{addr}   data = {payload[:60].hex(' ')}")
    except ConnectionClosed:
        log(f"{addr} connection closed by peer mid-packet")
    except socket.timeout:
        log(f"{addr} timeout (no traffic within keepalive window)")
    except ssl.SSLError as e:
        log(f"{addr} TLS error: {e}")
    except Exception as e:
        log(f"{addr} error: {type(e).__name__}: {e}")
    finally:
        if sess:
            sess.close()
        else:
            try:
                (tls_sock or raw_sock).close()
            except Exception:
                pass
        if sess and _unregister(sess) and bridge:
            bridge.publish_status()
        log(f"{addr} closed (client_id={sess.client_id if sess else None})")


_VACANCES_RE = re.compile(r"vacances(\d+)?[jd]?")


def _macro_payload(macro):
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
    if relative.startswith("macro/"):
        macro_name = relative[len("macro/"):]
        if macro_name == "_select":
            macro_name = payload.decode("utf-8", errors="replace").strip()
        translated = _macro_payload(macro_name)
        if not translated:
            log(f"macro '{macro_name}' unknown, dropped")
            return
        relative = f"devices/{DEVICE_ID}/messages/devicebound"
        payload = translated.encode()
        log(f"macro '{macro_name}' -> {translated}")

    # Only ever send to the configured box, never to another LAN client that connected.
    if not box_online():
        with _sessions_lock:
            others = list(sessions)
        hint = f" (connected client_ids: {others})" if others else ""
        log(f"cmd: box {DEVICE_ID} not connected, dropped {topic}{hint}")
        return
    send_publish_to_box(DEVICE_ID, relative, payload)


def _build_ssl_context():
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    c.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    # HF-LPB100 firmware speaks legacy TLS (small set of older ciphers).
    # Relax everything so we can negotiate.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # intentional: legacy box
            c.minimum_version = ssl.TLSVersion.TLSv1
    except (AttributeError, ValueError):
        pass
    try:
        c.set_ciphers("ALL:@SECLEVEL=0")
    except ssl.SSLError as e:
        log(f"set_ciphers err: {e}")
    return c


ctx = None

_rejected_log = {}  # ip -> last time a rejection was logged (avoid flooding the log)


def _ip_allowed(ip):
    if not ALLOWED_IPS or ip in ALLOWED_IPS:
        return True
    now = time.monotonic()
    if now - _rejected_log.get(ip, -1e9) > 60:
        _rejected_log[ip] = now
        log(f"connection from {ip} REFUSED: not in DEVICE_IP ({', '.join(sorted(ALLOWED_IPS))})"
            f" (logged at most once a minute per IP)")
    return False


def _on_sigterm(signum, frame):
    # Only raise here: log() and paho take locks that the interrupted main thread
    # might be holding. The cleanup runs in main()'s finally block instead.
    raise SystemExit(0)


def main():
    global bridge, ctx
    signal.signal(signal.SIGTERM, _on_sigterm)
    gen_cert()
    ctx = _build_ssl_context()
    try:
        bridge = UpstreamBridge()
        bridge.cmd_callbacks.append(cmd_callback)
    except Exception as e:
        log(f"bridge init failed: {e} (will run without bridge)")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, PORT))
    sock.listen(8)
    log(f"fake Azure IoT Hub listening on {HOST}:{PORT} (TLS, CN={SERVER_CN})")
    log(f"bridging captured PUBLISHes to mqtt://{UPSTREAM_HOST}:{UPSTREAM_PORT} prefix={PREFIX}")
    log(f"expecting box client_id={DEVICE_ID}")
    if ALLOWED_IPS:
        log(f"accepting connections only from {', '.join(sorted(ALLOWED_IPS))}")
    else:
        log("accepting connections from any IP (set DEVICE_IP to restrict)")

    try:
        while True:
            try:
                client, addr = sock.accept()
            except OSError as e:
                log(f"accept err: {e}")
                continue
            if not _ip_allowed(addr[0]):
                client.close()  # before TLS: costs nothing, reveals nothing
                continue
            # TLS handshake happens in the worker thread so a slow/broken client can't
            # block the accept loop (or crash it with a non-SSL exception).
            threading.Thread(target=handle_client, args=(client, addr), daemon=True).start()
    except KeyboardInterrupt:
        pass  # Ctrl+C: same clean shutdown as SIGTERM
    finally:
        log("shutting down")
        if bridge:
            bridge.stop()  # publishes "offline" so HA marks the entities unavailable


if __name__ == "__main__":
    main()
