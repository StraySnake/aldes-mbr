"""Box side: one thread per TLS connection, sessions, PUBLISH relay."""
import json, socket, ssl, struct, threading, time
from collections import deque
from datetime import datetime, timezone
from .config import (
    ALLOWED_IPS, BOX_EVENTS, BOX_TOPIC_PREFIX, DEFAULT_KEEPALIVE, DEVICE_ID, HANDSHAKE_TIMEOUT,
    MAX_PACKET_SIZE, POLL_INTERVAL,
)
from .logs import log, redact_password
from .mqtt_codec import (
    CONNECT, ConnectionClosed, DISCONNECT, PINGREQ, PUBACK, PUBLISH, SUBSCRIBE, UNSUBSCRIBE,
    encode_remaining_length, encode_string, parse_string, read_remaining_length, recv_exact,
)
from .telemetry import decode_frame, merge_telemetry
from . import runtime


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
        self.frames = 0  # PUBLISHes received on this connection
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
        log(f"inject: queued for {client_id} topic={topic} ({len(payload)} bytes)", "debug")
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
        tls_sock = runtime.ssl_context.wrap_socket(raw_sock, server_side=True, do_handshake_on_connect=False)
        tls_sock.do_handshake()
        cipher = tls_sock.cipher()
        log(f"{addr} TLS {tls_sock.version()} {cipher[0] if cipher else '?'}")
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
                if runtime.bridge:
                    runtime.bridge.publish("_meta/connect", f"{cid} from {addr[0]}")
                    runtime.bridge.publish_status()

            elif ptype == PUBLISH:
                qos = (flags >> 1) & 0x3
                topic, off = parse_string(payload, 0)
                pkt_id = None
                if qos > 0:
                    pkt_id = struct.unpack_from(">H", payload, off)[0]; off += 2
                msg = payload[off:]
                preview = msg[:300]
                # The first frame of a connection is logged at info level (shows the box works),
                # the following ones only at debug level (one every 1-5 min, forever).
                lvl = "debug" if sess.frames else "info"
                sess.frames += 1
                log(f"{addr} PUBLISH qos={qos} topic={topic} ({len(msg)}b)", lvl)
                log(f"{addr}   payload = {preview!r}{'...' if len(msg)>300 else ''}", lvl)
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
                elif runtime.bridge:
                    if topic == BOX_EVENTS:
                        frame = decode_frame(msg)
                        if frame is not None:
                            msg = json.dumps(merge_telemetry(frame), ensure_ascii=False)
                            runtime.commands.on_frame(frame)
                    runtime.bridge.publish(f"raw/{topic.replace('$', '_').replace('#', '_').replace('+', '_')}",
                                   msg, retain=True)
                    runtime.bridge.publish("last_seen", datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   retain=True)

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
        if sess and _unregister(sess) and runtime.bridge:
            runtime.bridge.publish_status()
        log(f"{addr} closed (client_id={sess.client_id if sess else None})")


_rejected_log = {}  # ip -> last time a rejection was logged (avoid flooding the log)


def ip_allowed(ip):
    if not ALLOWED_IPS or ip in ALLOWED_IPS:
        return True
    now = time.monotonic()
    if now - _rejected_log.get(ip, -1e9) > 60:
        _rejected_log[ip] = now
        log(f"connection from {ip} REFUSED: not in DEVICE_IP ({', '.join(sorted(ALLOWED_IPS))})"
            f" (logged at most once a minute per IP)")
    return False
