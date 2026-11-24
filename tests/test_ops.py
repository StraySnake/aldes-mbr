"""Configuration validation, log levels, TLS options and the Docker health check."""
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import REPO, free_port
from helpers import EVENTS, FakeBox, wait

HEALTHCHECK = [sys.executable, str(REPO / "healthcheck.py")]


def run_bridge_expecting_exit(tmp_path, **env):
    full = dict(os.environ, CERT_DIR=str(tmp_path), LOG_FILE=str(tmp_path / "x.log"), **env)
    return subprocess.run([sys.executable, str(REPO / "fake_iothub.py")], capture_output=True, text=True,
                          timeout=15, env=full)


@pytest.mark.parametrize("name,value,message", [
    ("LOG_MAX_BYTES", "5M", "LOG_MAX_BYTES must be an integer"),
    ("LISTEN_PORT", "abc", "LISTEN_PORT must be an integer"),
    ("UPSTREAM_MQTT_PORT", "0", "UPSTREAM_MQTT_PORT must be >= 1"),
    ("SENSOR_EXPIRE_AFTER", "-5", "SENSOR_EXPIRE_AFTER must be >= 0"),
    ("CMD_CONFIRM_TIMEOUT", "soon", "CMD_CONFIRM_TIMEOUT must be a number"),
    ("LOG_LEVEL", "verbose", "LOG_LEVEL must be one of"),
    ("UPSTREAM_MQTT_TLS", "maybe", "UPSTREAM_MQTT_TLS must be 0 or 1"),
    ("TLS_CIPHERS", "NOT-A-CIPHER", "TLS_CIPHERS 'NOT-A-CIPHER' rejected"),
    ("CMD_QOS", "2", "CMD_QOS must be 0 or 1"),
])
def test_invalid_config_is_a_clear_fatal_error(tmp_path, name, value, message):
    p = run_bridge_expecting_exit(tmp_path, **{name: value})
    assert p.returncode != 0
    assert message in p.stderr and "Traceback" not in p.stderr, p.stderr


# ---------- log levels ----------

def _two_frames(bridge):
    box = FakeBox(bridge.port)
    box.publish(EVENTS, b'{"frame": "first"}')
    assert wait(lambda: "first" in bridge.log())
    box.publish(EVENTS, b'{"frame": "second"}')
    time.sleep(0.5)
    box.close()


def test_info_level_logs_only_the_first_frame(start_bridge):
    bridge = start_bridge()
    _two_frames(bridge)
    assert "first" in bridge.log() and "second" not in bridge.log()
    assert "set LOG_LEVEL=debug" in bridge.log()


def test_debug_level_logs_every_frame(start_bridge):
    bridge = start_bridge({"LOG_LEVEL": "debug"})
    _two_frames(bridge)
    assert wait(lambda: "second" in bridge.log())
    assert "DEBUG" in bridge.log()


def test_warning_level_hides_connections(start_bridge):
    bridge = start_bridge({"LOG_LEVEL": "warning"}, wait_upstream=False)
    box = FakeBox(bridge.port)
    time.sleep(1)
    box.close()
    assert "NEW CONNECTION" not in bridge.log()


# ---------- TLS with the box ----------

def test_negotiated_tls_suite_is_logged(start_bridge):
    bridge = start_bridge()
    box = FakeBox(bridge.port)
    assert wait(lambda: " TLS TLSv1" in bridge.log())
    box.close()


def test_tls_ciphers_restriction(start_bridge):
    # TLS 1.3 suites are not affected by the cipher string, so this only checks that a
    # restricted, valid string is accepted and the box can still connect.
    bridge = start_bridge({"TLS_CIPHERS": "ECDHE-RSA-AES128-GCM-SHA256"})
    box = FakeBox(bridge.port)
    assert wait(lambda: "SUBSCRIBE" in bridge.log())
    box.close()


# ---------- TLS to Mosquitto ----------

@pytest.fixture(scope="module")
def tls_mqtt_port(tmp_path_factory):
    import shutil
    work = tmp_path_factory.mktemp("amqtt-tls")
    # self-signed cert for CN=aldesiotsuite.azure-devices.net (not 127.0.0.1)
    subprocess.run([sys.executable, "-c", "from aldes_bridge.tls import gen_cert; gen_cert()"], cwd=REPO, check=True,
                   env=dict(os.environ, CERT_DIR=str(work), LOG_FILE=str(work / "gen.log")),
                   capture_output=True)
    port = free_port()
    cfg = work / "amqtt.yaml"
    cfg.write_text(
        "listeners:\n"
        "  default:\n"
        "    type: tcp\n"
        f"    bind: 127.0.0.1:{port}\n"
        "    ssl: true\n"
        f"    certfile: {work / 'fake_azure.crt'}\n"
        f"    keyfile: {work / 'fake_azure.key'}\n"
        "plugins:\n"
        "  amqtt.plugins.authentication.AnonymousAuthPlugin:\n"
        "    allow_anonymous: true\n"
    )
    exe = shutil.which("amqtt") or str(sys.executable).replace("python", "amqtt")
    proc = subprocess.Popen([exe, "-c", str(cfg)], stdout=open(work / "amqtt.log", "w"), stderr=subprocess.STDOUT)
    time.sleep(2)
    yield port, work / "fake_azure.crt"
    proc.terminate()
    proc.wait(10)


def test_upstream_tls_insecure_connects(start_bridge, tls_mqtt_port):
    port, _ = tls_mqtt_port
    bridge = start_bridge({"UPSTREAM_MQTT_PORT": str(port), "UPSTREAM_MQTT_TLS": "1",
                           "UPSTREAM_MQTT_TLS_INSECURE": "1"})
    assert "certificate NOT verified" in bridge.log()
    assert "upstream MQTT connected" in bridge.log()


def test_upstream_tls_verifies_certificate(start_bridge, tls_mqtt_port):
    # the CA is right but the certificate is not for 127.0.0.1: must be refused
    port, ca = tls_mqtt_port
    bridge = start_bridge({"UPSTREAM_MQTT_PORT": str(port), "UPSTREAM_MQTT_TLS": "1",
                           "UPSTREAM_MQTT_CA": str(ca)}, wait_upstream=False)
    time.sleep(3)
    assert "upstream MQTT connected" not in bridge.log()


# ---------- health check ----------

def healthcheck(health_file):
    return subprocess.run(HEALTHCHECK, capture_output=True, text=True, timeout=10,
                          env=dict(os.environ, HEALTH_FILE=str(health_file)))


def test_healthy_when_mosquitto_connected(bridge):
    assert wait(lambda: healthcheck(bridge.health_file).returncode == 0, 15)


def test_unhealthy_without_mosquitto(start_bridge):
    bridge = start_bridge({"UPSTREAM_MQTT_PORT": str(free_port())}, wait_upstream=False)
    assert wait(lambda: bridge.health_file.exists())
    r = healthcheck(bridge.health_file)
    assert r.returncode == 1 and "upstream MQTT not connected" in r.stderr


def test_unhealthy_when_status_file_is_stale_or_missing(tmp_path):
    r = healthcheck(tmp_path / "missing")
    assert r.returncode == 1 and "no health file" in r.stderr
    f = tmp_path / "health"
    f.write_text("ok\n")
    old = time.time() - 120
    os.utime(f, (old, old))
    r = healthcheck(f)
    assert r.returncode == 1 and "not updated" in r.stderr


def test_ha_discovery_unchanged_by_log_level(start_bridge, mqtt_port):
    # sanity: debug level must not change what is published
    from conftest import Recorder
    start_bridge({"LOG_LEVEL": "warning"}, wait_upstream=False)
    rec = Recorder(mqtt_port, "disco-ops")
    rec.client.subscribe("homeassistant/#", qos=1)
    assert wait(lambda: rec.last("homeassistant/select/aldes_vmc_AABBCCDDEEFF/mode/config"))
    assert json.loads(rec.last("homeassistant/select/aldes_vmc_AABBCCDDEEFF/mode/config"))["name"] == "Débit de ventilation"
    rec.close()


# ---------- HTTPS (the box's requests to port 443; their Date header sets its clock) ----------

def _probe_request(port, request, server_name="aldesiotsuite.azure-devices.net"):
    import socket
    import ssl
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with c.wrap_socket(raw, server_hostname=server_name) as s:
            s.sendall(request)
            resp = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    return resp
                resp += chunk


def test_https_probe_logs_request_and_answers_404(start_bridge):
    port = free_port()
    bridge = start_bridge({"HTTPS_PORT": str(port), "LOG_LEVEL": "debug"})
    assert wait(lambda: f"HTTPS listening on 0.0.0.0:{port}" in bridge.log())
    body = b'{"hello":"box"}'
    resp = _probe_request(port, (
        b"POST /devices/AABBCCDDEEFF_AIR/messages/events?api-version=2016-11-14 HTTP/1.1\r\n"
        b"Host: aldesiotsuite.azure-devices.net\r\n"
        b"Authorization: SharedAccessSignature sr=x&sig=SECRET123&se=1478918800\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body))
    assert resp.startswith(b"HTTP/1.1 404")
    assert b"\r\nDate: " in resp
    assert wait(lambda: "https: closed" in bridge.log())
    log = bridge.log()
    assert "server name (SNI) = 'aldesiotsuite.azure-devices.net'" in log
    assert "POST /devices/AABBCCDDEEFF_AIR/messages/events?api-version=2016-11-14 HTTP/1.1" in log
    assert '{"hello":"box"}' in log
    assert "SECRET123" not in log and "sig=<redacted>" in log


def test_https_probe_answers_c2d_poll_with_204(start_bridge):
    """What a real box sends at boot: Azure IoT Hub's HTTPS poll for cloud-to-device messages."""
    port = free_port()
    bridge = start_bridge({"HTTPS_PROBE_PORT": str(port), "LOG_LEVEL": "debug"})  # old name still works
    assert wait(lambda: "HTTPS listening" in bridge.log())
    resp = _probe_request(port, (
        b"GET /devices/AABBCCDDEEFF_AIR/messages/devicebound?api-version=2016-02-03 HTTP/1.1\r\n"
        b"Host: aldesiotsuite.azure-devices.net\r\n"
        b"Authorization: SharedAccessSignature sr=x&sig=SECRET123&se=1478918800\r\n"
        b"Connection: keep-alive\r\n\r\n"), server_name=None)
    assert resp.startswith(b"HTTP/1.1 204 No Content")
    assert wait(lambda: "https: closed" in bridge.log())
    log = bridge.log()
    assert "server name (SNI) = None" in log
    assert "answered 204 No Content" in log
    assert "SECRET123" not in log


def test_https_info_log_is_one_line_per_request(start_bridge):
    port = free_port()
    bridge = start_bridge({"HTTPS_PORT": str(port)})
    _probe_request(port, b"GET /devices/AABBCCDDEEFF_AIR/messages/devicebound?api-version=2016-02-03 HTTP/1.1\r\n"
                         b"Authorization: SharedAccessSignature sr=x&sig=SECRET123&se=1\r\n\r\n")
    assert wait(lambda: "answered 204" in bridge.log())
    log = bridge.log()
    assert "https: GET /devices/AABBCCDDEEFF_AIR/messages/devicebound" in log
    assert "Authorization" not in log and "SECRET123" not in log  # headers: debug only


def test_https_firmware_check_gets_404(start_bridge):
    """The box's firmware-version check: the bridge 404s it to keep the box on its firmware."""
    port = free_port()
    bridge = start_bridge({"HTTPS_PORT": str(port)})
    assert wait(lambda: "HTTPS listening" in bridge.log())
    resp = _probe_request(port, b"GET /firmware/VersionAldesConnectBoxIOTHUB.txt HTTP/1.1\r\n"
                                b"Host: aldes.blob.core.windows.net\r\n\r\n", server_name=None)
    assert resp.startswith(b"HTTP/1.1 404")
    assert b"\r\nDate: " in resp  # the Date header still sets the box's clock
    assert wait(lambda: "firmware version check" in bridge.log())


def test_https_off_by_default(bridge):
    assert "HTTPS listening" not in bridge.log()
