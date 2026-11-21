"""Minimal MQTT 3.1.1 encoding/decoding helpers."""
import struct


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
