"""Answers the box's HTTPS requests (port 443), and so gives it the time.

Besides MQTT (8883), the box sends HTTPS requests to port 443 of
aldesiotsuite.azure-devices.net (with the DNS rewrite: this host): at boot, before and right
after its MQTT connection. Observed on a Dee Fly Cube, without SNI:
GET /devices/<id>/messages/devicebound?api-version=2016-02-03, Azure IoT Hub's HTTPS poll for
cloud-to-device messages queued while it was offline.

The bridge answers like the real hub with an empty queue, "204 No Content" ("404 Not Found"
for anything else), always with the current time in the HTTP Date header. The box sets its
clock from that header (to UTC), then reconnects to MQTT about a minute later with a fresh
token: a time source that needs no NTP. The request is logged, credentials redacted.

If aldes.blob.core.windows.net is also pointed at this host, the box's firmware-version
check (GET /firmware/Version…txt, its very first request at boot) lands here too. The bridge
answers 404 instead of the real "V=53", which keeps the box on its firmware (so an Aldes OTA
push cannot break the bridge) — and that answer carries the Date header, so the clock is set
even earlier, before the MQTT connection.

The bridge runs as an unprivileged user, so it cannot listen on 443 itself: set e.g.
HTTPS_PORT=8443 and add a port forward on the router (box -> this host, TCP 443 -> 8443).
"""
import re, socket, ssl, struct, threading
from email.utils import formatdate
from .config import HANDSHAKE_TIMEOUT, HOST, HTTPS_PORT
from .logs import log
from .tls import build_ssl_context

MAX_REQUEST = 16 * 1024   # enough for headers and a small body
LOG_BODY = 2000           # bytes of body shown in the log


def parse_sni(hello):
    """Server name from the first bytes of a TLS ClientHello, or None."""
    try:
        if len(hello) < 5 or hello[0] != 0x16:
            return None
        rec_len = struct.unpack(">H", hello[3:5])[0]
        body = hello[5:5 + rec_len]
        if not body or body[0] != 0x01:  # handshake type: client_hello
            return None
        off = 4 + 2 + 32                 # handshake header, client version, random
        off += 1 + body[off]             # session id
        off += 2 + struct.unpack(">H", body[off:off + 2])[0]  # cipher suites
        off += 1 + body[off]             # compression methods
        end = off + 2 + struct.unpack(">H", body[off:off + 2])[0]
        off += 2
        while off + 4 <= end:
            etype, elen = struct.unpack(">HH", body[off:off + 4])
            off += 4
            if etype == 0:  # server_name
                # list length (2), name type (1), name length (2), name
                nlen = struct.unpack(">H", body[off + 3:off + 5])[0]
                return body[off + 5:off + 5 + nlen].decode("ascii", errors="replace")
            off += elen
    except (IndexError, struct.error):
        pass
    return None


def _redact(text):
    # SAS tokens are valid credentials for the real Azure IoT Hub: never log them.
    text = re.sub(r"(sig=)[^&\s\"]+", r"\1<redacted>", text)
    # Other Authorization schemes: hide the whole value (SAS tokens keep sr/se, useful).
    return re.sub(r"(?im)^(authorization:[ \t]*)(?![ \t]|SharedAccessSignature).+$", r"\1<redacted>", text)


def _read_request(sock):
    """Read an HTTP request (headers, then Content-Length bytes of body), bounded."""
    data = b""
    while b"\r\n\r\n" not in data and len(data) < MAX_REQUEST:
        chunk = sock.recv(4096)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    m = re.search(rb"(?im)^content-length:\s*(\d+)", head)
    want = min(int(m.group(1)), MAX_REQUEST) if m else 0
    while len(body) < want:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return data if not head else head + b"\r\n\r\n" + body


def _log_request(addr, data):
    head, sep, body = data.partition(b"\r\n\r\n")
    if not sep:
        log(f"{addr} https: {len(data)} bytes, not an HTTP request: {_redact(repr(data[:LOG_BODY]))}")
        return
    lines = head.decode("utf-8", errors="replace").split("\r\n")
    log(f"{addr} https: {_redact(lines[0])}")
    for line in lines[1:]:
        log(f"{addr} https:   {_redact(line)}", "debug")
    if body:
        text = body[:LOG_BODY].decode("utf-8", errors="replace")
        log(f"{addr} https:   body ({len(body)} bytes): {_redact(text)}"
            + ("..." if len(body) > LOG_BODY else ""), "debug")


# GET /devices/<id>/messages/devicebound?api-version=...
C2D_POLL = re.compile(rb"GET /devices/[^/\s]+/messages/devicebound(\?\S*)? HTTP/1\.[01]\r\n")
# GET /firmware/VersionAldesConnectBox…txt — the box's firmware-update version check.
FW_CHECK = re.compile(rb"GET /firmware/\S*[Vv]ersion\S*\.txt HTTP/1\.[01]\r\n")


def _reply(sock, status):
    sock.sendall((f"HTTP/1.1 {status}\r\n"
                  f"Date: {formatdate(usegmt=True)}\r\n"
                  "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())


def _handle(raw, addr, ctx):
    log(f"{addr} https: new connection", "debug")
    sock = raw
    try:
        raw.settimeout(HANDSHAKE_TIMEOUT)
        first = raw.recv(2048, socket.MSG_PEEK)
        if not first:
            log(f"{addr} https: closed without sending anything")
            return
        if first[0] == 0x16:
            log(f"{addr} https: TLS ClientHello, server name (SNI) = {parse_sni(first)!r}", "debug")
            sock = ctx.wrap_socket(raw, server_side=True)
            cipher = sock.cipher()
            log(f"{addr} https: TLS {sock.version()} {cipher[0] if cipher else '?'}", "debug")
        else:
            log(f"{addr} https: plain connection (not TLS)")
        data = _read_request(sock)
        _log_request(addr, data)
        if C2D_POLL.match(data):
            # Azure IoT Hub's HTTPS "receive cloud-to-device message": the box polls for
            # commands queued while it was offline. Empty queue = 204, as the real hub does.
            _reply(sock, "204 No Content")
            log(f"{addr} https: answered 204 No Content (no pending cloud-to-device message), "
                f"with the current time")
        elif FW_CHECK.match(data):
            # The box checks its firmware version here before every boot. Answering 404
            # (not the real "V=53") keeps it on its current firmware, so an Aldes OTA push
            # can never change the hostname, add cert pinning, or otherwise break the bridge.
            _reply(sock, "404 Not Found")
            log(f"{addr} https: answered 404 to the firmware version check "
                f"(keeps the box on its firmware), with the current time")
        elif data.startswith((b"GET ", b"POST ", b"PUT ", b"HEAD ", b"PATCH ", b"DELETE ")):
            _reply(sock, "404 Not Found")
            log(f"{addr} https: answered 404 (with the current time in the Date header)")
    except ssl.SSLError as e:
        log(f"{addr} https: TLS error: {e}")
    except (OSError, ValueError) as e:
        log(f"{addr} https: {type(e).__name__}: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass
        log(f"{addr} https: closed", "debug")


def start():
    """Start the HTTPS listener in a background thread (no-op if HTTPS_PORT is 0)."""
    if not HTTPS_PORT:
        return
    from .box import ip_allowed  # same DEVICE_IP restriction as the MQTT port
    ctx = build_ssl_context()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, HTTPS_PORT))
    srv.listen(4)
    log(f"HTTPS listening on {HOST}:{HTTPS_PORT} (answers the box's requests to port 443, "
        f"gives it the time)")

    def loop():
        while True:
            try:
                client, addr = srv.accept()
            except OSError as e:
                log(f"https accept err: {e}")
                continue
            if not ip_allowed(addr[0]):
                client.close()
                continue
            threading.Thread(target=_handle, args=(client, addr, ctx), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
