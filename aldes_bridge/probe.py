"""Diagnostic listener for the box's HTTPS connections (port 443).

At boot, and again after its MQTT connection, the box tries a TCP connection to port 443
of aldesiotsuite.azure-devices.net (the same name as MQTT, so with the DNS rewrite: this
host). Nothing answers there, and what it wants is not known. When HTTPS_PROBE_PORT is
set, the bridge listens on that port and logs what the box sends: the server name of its
TLS ClientHello (SNI), then the HTTP request (credentials redacted). It answers
"404 Not Found" with the current time in the Date header, closes, and never sends
anything else: it is only meant to find out what the box asks for.

The bridge runs as an unprivileged user, so it cannot listen on 443 itself: use
e.g. HTTPS_PROBE_PORT=8443 and a port forward on the router (box -> this host, TCP 443
-> 8443).
"""
import re, socket, ssl, struct, threading
from email.utils import formatdate
from .config import HANDSHAKE_TIMEOUT, HOST, HTTPS_PROBE_PORT
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
        log(f"{addr} probe: {len(data)} bytes, not an HTTP request: {_redact(repr(data[:LOG_BODY]))}")
        return
    for line in head.decode("utf-8", errors="replace").split("\r\n"):
        log(f"{addr} probe:   {_redact(line)}")
    if body:
        text = body[:LOG_BODY].decode("utf-8", errors="replace")
        log(f"{addr} probe:   body ({len(body)} bytes): {_redact(text)}"
            + ("..." if len(body) > LOG_BODY else ""))


def _handle(raw, addr, ctx):
    log(f"{addr} probe: NEW CONNECTION on the HTTPS probe port")
    sock = raw
    try:
        raw.settimeout(HANDSHAKE_TIMEOUT)
        first = raw.recv(2048, socket.MSG_PEEK)
        if not first:
            log(f"{addr} probe: closed without sending anything")
            return
        if first[0] == 0x16:
            log(f"{addr} probe: TLS ClientHello, server name (SNI) = {parse_sni(first)!r}")
            sock = ctx.wrap_socket(raw, server_side=True)
            cipher = sock.cipher()
            log(f"{addr} probe: TLS {sock.version()} {cipher[0] if cipher else '?'}")
        else:
            log(f"{addr} probe: plain connection (not TLS)")
        data = _read_request(sock)
        _log_request(addr, data)
        if data.startswith((b"GET ", b"POST ", b"PUT ", b"HEAD ", b"PATCH ", b"DELETE ")):
            sock.sendall(("HTTP/1.1 404 Not Found\r\n"
                          f"Date: {formatdate(usegmt=True)}\r\n"
                          "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
            log(f"{addr} probe: answered 404 (with the current time in the Date header)")
    except ssl.SSLError as e:
        log(f"{addr} probe: TLS error: {e}")
    except (OSError, ValueError) as e:
        log(f"{addr} probe: {type(e).__name__}: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass
        log(f"{addr} probe: closed")


def start():
    """Start the probe listener in a background thread (no-op if HTTPS_PROBE_PORT is 0)."""
    if not HTTPS_PROBE_PORT:
        return
    from .box import ip_allowed  # same DEVICE_IP restriction as the MQTT port
    ctx = build_ssl_context()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, HTTPS_PROBE_PORT))
    srv.listen(4)
    log(f"HTTPS probe listening on {HOST}:{HTTPS_PROBE_PORT} (diagnostic: logs what the box asks for)")

    def loop():
        while True:
            try:
                client, addr = srv.accept()
            except OSError as e:
                log(f"probe accept err: {e}")
                continue
            if not ip_allowed(addr[0]):
                client.close()
                continue
            threading.Thread(target=_handle, args=(client, addr, ctx), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
