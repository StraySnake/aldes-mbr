"""Entry point: wiring, accept loop, health file, shutdown."""
import signal, socket, threading, time
from .config import (
    ALLOWED_IPS, DEVICE_ID, HEALTH_FILE, HOST, LOG_LEVEL, PORT, PREFIX, SERVER_CN,
    UPSTREAM_HOST, UPSTREAM_PORT,
)
from .logs import log
from .tls import build_ssl_context, gen_cert
from .box import handle_client, ip_allowed
from .commands import cmd_callback, tracker
from .upstream import UpstreamBridge
from . import probe
from . import runtime


def _health_loop():
    """Write the status read by healthcheck.py (Docker HEALTHCHECK) every 10 s.
    Healthy = the main loop is alive and Mosquitto is connected. The box being
    offline is not a container problem: it is reported by HA's availability."""
    while True:
        ok = runtime.bridge is not None and runtime.bridge.client.is_connected()
        try:
            tmp = HEALTH_FILE.with_suffix(".tmp")
            tmp.write_text("ok\n" if ok else "upstream MQTT not connected\n")
            tmp.replace(HEALTH_FILE)
        except OSError:
            pass
        time.sleep(10)


def _on_sigterm(signum, frame):
    # Only raise here: log() and paho take locks that the interrupted main thread
    # might be holding. The cleanup runs in main()'s finally block instead.
    raise SystemExit(0)


def main():
    signal.signal(signal.SIGTERM, _on_sigterm)
    gen_cert()
    runtime.ssl_context = build_ssl_context()
    runtime.commands = tracker
    try:
        runtime.bridge = UpstreamBridge()
        runtime.bridge.cmd_callbacks.append(cmd_callback)
    except Exception as e:
        log(f"bridge init failed: {e} (will run without bridge)")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, PORT))
    sock.listen(8)
    log(f"fake Azure IoT Hub listening on {HOST}:{PORT} (TLS, CN={SERVER_CN})")
    log(f"bridging captured PUBLISHes to mqtt://{UPSTREAM_HOST}:{UPSTREAM_PORT} prefix={PREFIX}")
    log(f"expecting box client_id={DEVICE_ID}")
    log(f"log level {LOG_LEVEL}" + ("" if LOG_LEVEL == "debug" else
                                     " (set LOG_LEVEL=debug to log every telemetry frame)"))
    threading.Thread(target=_health_loop, daemon=True).start()
    probe.start()
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
            if not ip_allowed(addr[0]):
                client.close()  # before TLS: costs nothing, reveals nothing
                continue
            # TLS handshake happens in the worker thread so a slow/broken client can't
            # block the accept loop (or crash it with a non-SSL exception).
            threading.Thread(target=handle_client, args=(client, addr), daemon=True).start()
    except KeyboardInterrupt:
        pass  # Ctrl+C: same clean shutdown as SIGTERM
    finally:
        log("shutting down")
        if runtime.bridge:
            runtime.bridge.stop()  # publishes "offline" so HA marks the entities unavailable
