"""Test helpers: MQTT packet builders and a fake Connect Box speaking MQTT over TLS."""
import socket
import ssl
import struct
import threading
import time
from collections import deque

DEVICE_MAC = "AABBCCDDEEFF"
DEVICE_ID = DEVICE_MAC + "_AIR"
EVENTS = f"devices/{DEVICE_ID}/messages/events/"


def wait(cond, timeout=5.0):
    """Poll cond() until it is truthy or timeout expires; returns its last value."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def enc_str(s):
    b = s.encode()
    return struct.pack(">H", len(b)) + b


def remaining_length(n):
    out = b""
    while True:
        d = n & 0x7F
        n >>= 7
        if n:
            d |= 0x80
        out += bytes([d])
        if not n:
            return out


def connect_pkt(cid, keepalive=60):
    # flags 0xC2: username + password + clean session, like the box
    vh = enc_str("MQTT") + bytes([4, 0xC2]) + struct.pack(">H", keepalive)
    pl = enc_str(cid) + enc_str("hub/" + cid) + enc_str("SharedAccessSignature sr=x&sig=SECRET123&se=1")
    body = vh + pl
    return bytes([0x10]) + remaining_length(len(body)) + body


def subscribe_pkt(topic, pkt_id=1):
    body = struct.pack(">H", pkt_id) + enc_str(topic) + bytes([1])
    return bytes([0x82]) + remaining_length(len(body)) + body


def publish_pkt(topic, payload):
    body = enc_str(topic) + payload
    return bytes([0x30]) + remaining_length(len(body)) + body


class FakeBox:
    """A fake Connect Box. Like the real firmware (and like the broker), it does all
    its TLS I/O in one thread: send() only queues."""

    def __init__(self, port, cid=DEVICE_ID, keepalive=60, connect=True, source_ip=None,
                 ack=True, quirky=False):
        """ack: answer QoS 1 PUBLISHes with a PUBACK.
        quirky: behave like the real firmware, which reads a packet id in EVERY PUBLISH,
        QoS 0 included (eating the first 2 bytes of the payload), and acknowledges it."""
        self.ack = ack
        self.quirky = quirky
        self.acked = []  # packet ids we acknowledged
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection(("127.0.0.1", port), timeout=10,
                                       source_address=(source_ip, 0) if source_ip else None)
        self.sock = c.wrap_socket(raw)
        self.rx = []  # payloads of PUBLISHes received from the broker
        self.closed = threading.Event()
        self._out = deque()
        threading.Thread(target=self._io, daemon=True).start()
        if connect:
            self.send(connect_pkt(cid, keepalive))
            self.send(subscribe_pkt(f"devices/{cid}/messages/devicebound/#"))

    def send(self, data):
        self._out.append(data)

    def publish(self, topic, payload):
        self.send(publish_pkt(topic, payload))

    def received(self, needle):
        return any(needle in r for r in self.rx)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _io(self):
        try:
            while True:
                while self._out:
                    self.sock.settimeout(10)
                    self.sock.sendall(self._out.popleft())
                self.sock.settimeout(0.05)
                try:
                    head = self.sock.recv(1)
                except socket.timeout:
                    continue
                if not head:
                    raise EOFError
                self.sock.settimeout(10)
                mult, n = 1, 0
                while True:
                    d = self._recv_exact(1)[0]
                    n += (d & 0x7F) * mult
                    mult *= 128
                    if not d & 0x80:
                        break
                payload = self._recv_exact(n)
                if head[0] >> 4 == 3:  # PUBLISH
                    qos = (head[0] >> 1) & 3
                    tlen = struct.unpack(">H", payload[:2])[0]
                    off = 2 + tlen
                    pkt_id = None
                    if qos or self.quirky:
                        pkt_id = struct.unpack(">H", payload[off:off + 2])[0]
                        off += 2
                    self.rx.append(payload[off:].decode(errors="replace"))
                    if pkt_id is not None and self.ack:
                        self.acked.append(pkt_id)
                        self._out.append(bytes([0x40, 0x02]) + struct.pack(">H", pkt_id))
        except Exception:
            pass
        self.closed.set()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass
