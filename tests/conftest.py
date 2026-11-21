"""Fixtures: an amqtt broker standing in for Mosquitto, and the bridge as a subprocess."""
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

from helpers import DEVICE_MAC, EVENTS, wait

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# fake_iothub reads its config at import time (unit tests import it directly).
os.environ.setdefault("DEVICE_MAC", DEVICE_MAC)
_IMPORT_DIR = Path(tempfile.mkdtemp(prefix="aldes-test-"))
os.environ.setdefault("LOG_FILE", str(_IMPORT_DIR / "mitm.log"))
os.environ.setdefault("CERT_DIR", str(_IMPORT_DIR))  # default /data is not writable in CI


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mqtt_port(tmp_path_factory):
    exe = shutil.which("amqtt") or str(Path(sys.executable).parent / "amqtt")
    port = free_port()
    work = tmp_path_factory.mktemp("amqtt")
    cfg = work / "amqtt.yaml"
    cfg.write_text(
        "listeners:\n"
        "  default:\n"
        "    type: tcp\n"
        f"    bind: 127.0.0.1:{port}\n"
        "plugins:\n"
        "  amqtt.plugins.authentication.AnonymousAuthPlugin:\n"
        "    allow_anonymous: true\n"
        "  amqtt.plugins.sys.broker.BrokerSysPlugin:\n"
        "    sys_interval: 0\n"
    )
    proc = subprocess.Popen([exe, "-c", str(cfg)], stdout=open(work / "amqtt.log", "w"),
                            stderr=subprocess.STDOUT)

    def up():
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            return False

    assert wait(up, 15), "amqtt did not start"
    yield port
    proc.terminate()
    proc.wait(10)


class Recorder:
    """paho client recording every message under aldes/vmc/#."""

    def __init__(self, port, client_id):
        self.msgs = {}  # topic -> [(payload str, retain)]
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.on_message = self._on_message
        self.client.connect("127.0.0.1", port)
        self.client.subscribe("aldes/vmc/#", qos=1)
        self.client.loop_start()

    def _on_message(self, client, userdata, msg):
        self.msgs.setdefault(msg.topic, []).append((msg.payload.decode(errors="replace"), msg.retain))

    def payloads(self, topic):
        return [p for p, _ in self.msgs.get(topic, [])]

    def last(self, topic):
        values = self.payloads(topic)
        return values[-1] if values else None

    def publish(self, topic, payload, retain=False):
        self.client.publish(topic, payload, qos=1, retain=retain).wait_for_publish(5)

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


@pytest.fixture
def mqtt_client(mqtt_port, request):
    rec = Recorder(mqtt_port, f"test-{request.node.name}"[:60])
    time.sleep(0.3)  # let retained messages arrive
    yield rec
    rec.close()


class Bridge:
    def __init__(self, mqtt_port, workdir, env=None, wait_upstream=True):
        self.port = free_port()
        self.logfile = workdir / "mitm.log"
        self.health_file = workdir / "health"
        full_env = dict(
            os.environ,
            CERT_DIR=str(workdir),
            DEVICE_MAC=DEVICE_MAC,
            LOG_FILE=str(self.logfile),
            LISTEN_PORT=str(self.port),
            UPSTREAM_MQTT_HOST="127.0.0.1",
            UPSTREAM_MQTT_PORT=str(mqtt_port),
            HEALTH_FILE=str(self.health_file),
        )
        full_env.update(env or {})
        self.proc = subprocess.Popen([sys.executable, "-u", str(REPO / "fake_iothub.py")], env=full_env,
                                     stdout=open(workdir / "stdout.log", "w"), stderr=subprocess.STDOUT)
        # The health file is written once the listener is up (works at any LOG_LEVEL).
        ok = wait(lambda: self.health_file.exists() and (not wait_upstream or "HA discovery:" in self.log()), 20)
        assert ok, f"bridge did not start:\n{(workdir / 'stdout.log').read_text()}"

    def log(self):
        try:
            return self.logfile.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def stop(self, sig=signal.SIGTERM):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                return self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                raise
        return self.proc.returncode


@pytest.fixture(scope="session")
def clear_retained(mqtt_port):
    """Callable removing the bridge's retained state, so each test starts from scratch.
    One client for the whole session (paho's loop_stop() takes up to 1 s)."""
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cleaner")
    c.connect("127.0.0.1", mqtt_port)
    c.loop_start()

    def _clear():
        for topic in (f"aldes/vmc/raw/{EVENTS}", "aldes/vmc/last_seen", "aldes/vmc/status",
                      "aldes/vmc/command_result"):
            c.publish(topic, "", qos=1, retain=True).wait_for_publish(5)

    yield _clear
    c.loop_stop()
    c.disconnect()


@pytest.fixture
def start_bridge(mqtt_port, clear_retained, tmp_path):
    """Factory: start_bridge(env={...}, clear=True, wait_upstream=True) -> Bridge.
    Stopped at teardown. clear=False keeps Mosquitto's retained state from a previous
    bridge; wait_upstream=False does not wait for the connection to Mosquitto."""
    bridges = []

    def _start(env=None, clear=True, wait_upstream=True):
        if clear:
            clear_retained()
        workdir = tmp_path / f"bridge{len(bridges)}"
        workdir.mkdir()
        b = Bridge(mqtt_port, workdir, env, wait_upstream)
        bridges.append(b)
        return b

    yield _start
    for b in bridges:
        b.stop()


@pytest.fixture
def bridge(start_bridge):
    return start_bridge()
