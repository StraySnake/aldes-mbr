"""End-to-end tests: amqtt (stand-in for Mosquitto) + the bridge process + fake TLS boxes."""
import json
import signal
import threading
import time

import pytest

from helpers import DEVICE_ID, EVENTS, FakeBox, publish_pkt, remaining_length, wait

RAW = f"aldes/vmc/raw/{EVENTS}"
STATUS = "aldes/vmc/status"
DEVICEBOUND = f"aldes/vmc/cmd/devices/{DEVICE_ID}/messages/devicebound"


@pytest.fixture
def box(bridge):
    b = FakeBox(bridge.port)
    assert wait(lambda: "SUBSCRIBE" in bridge.log())
    yield b
    b.close()


# ---------- telemetry and commands ----------

def test_telemetry_relayed_and_status_online(bridge, box, mqtt_client):
    box.publish(EVENTS, b'{"outside_tpt": 12.5}')
    assert wait(lambda: mqtt_client.last(RAW) == '{"outside_tpt": 12.5}')
    assert wait(lambda: mqtt_client.last(STATUS) == "online")


def test_macro_and_select_commands_reach_the_box(bridge, box, mqtt_client):
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: box.received('"X"'))
    mqtt_client.publish("aldes/vmc/cmd/macro/_select", "Invités")
    assert wait(lambda: box.received('"Y"'))


def test_sas_signature_not_logged(bridge, box):
    assert "SECRET123" not in bridge.log()
    assert "sig=<redacted>" in bridge.log()


def test_commands_in_order_during_telemetry_stream(bridge, box, mqtt_client):
    stop = threading.Event()
    sent = []

    def stream():
        i = 0
        while not stop.is_set():
            box.publish(EVENTS, json.dumps({"seq": i, "pad": "x" * 800}).encode())
            sent.append(i)
            i += 1
            time.sleep(0.005)

    t = threading.Thread(target=stream)
    t.start()
    for i in range(200):
        mqtt_client.client.publish(DEVICEBOUND, f"cmd{i}")
    ok = wait(lambda: sum(r.startswith("cmd") for r in box.rx) >= 200, 20)
    stop.set()
    t.join()
    assert ok
    assert [r for r in box.rx if r.startswith("cmd")] == [f"cmd{i}" for i in range(200)]
    # every telemetry frame made it to Mosquitto
    assert wait(lambda: len(mqtt_client.payloads(RAW)) >= len(sent), 10)


# ---------- retained commands ----------

def test_retained_command_ignored_and_clearing_it_does_nothing(start_bridge, mqtt_client):
    topic = "aldes/vmc/cmd/macro/vacances7j"
    mqtt_client.publish(topic, "1", retain=True)
    try:
        bridge = start_bridge()  # subscribes -> receives the retained message
        assert wait(lambda: "IGNORED: retained" in bridge.log())
    finally:
        mqtt_client.publish(topic, "", retain=True)  # clear it
    assert wait(lambda: "empty payload" in bridge.log())
    assert "changeMode" not in bridge.log()


# ---------- sessions ----------

def test_reconnect_replaces_previous_session(bridge, box, mqtt_client):
    box2 = FakeBox(bridge.port)
    assert box.closed.wait(5), "old session not closed"
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: box2.received('"X"'))
    assert mqtt_client.last(STATUS) == "online"
    box2.close()


def test_second_connect_closes_connection_and_status_goes_offline(bridge, box, mqtt_client):
    from helpers import connect_pkt
    assert wait(lambda: mqtt_client.last(STATUS) == "online")
    box.send(connect_pkt("other"))
    assert box.closed.wait(5)
    assert wait(lambda: mqtt_client.last(STATUS) == "offline")
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: "not connected, dropped" in bridge.log())


def test_oversized_packet_closes_connection(bridge, box):
    box.send(bytes([0x30]) + remaining_length(10 * 1024 * 1024))
    assert box.closed.wait(5)
    assert "packet too large" in bridge.log()


def test_keepalive_timeout(bridge):
    b = FakeBox(bridge.port, keepalive=2)
    t0 = time.monotonic()
    assert b.closed.wait(8)
    assert 2.5 < time.monotonic() - t0 < 5  # 1.5 x keepalive
    assert "timeout" in bridge.log()


# ---------- who may feed the bridge ----------

def test_publish_on_another_device_topic_not_relayed(bridge, box, mqtt_client):
    box.publish("devices/OTHER_AIR/messages/events/", b"{}")
    assert wait(lambda: "not relayed" in bridge.log())
    assert not any("OTHER_AIR" in t for t in mqtt_client.msgs)


def test_intruder_client_id_not_relayed(bridge, box, mqtt_client):
    intruder = FakeBox(bridge.port, cid="intruder")
    intruder.publish(EVENTS, b'{"outside_tpt": 99}')
    assert wait(lambda: "not relayed" in bridge.log())
    assert '{"outside_tpt": 99}' not in mqtt_client.payloads(RAW)
    assert not box.closed.is_set()  # the real box keeps its session
    intruder.close()


def test_publish_before_connect_closes_connection(bridge, mqtt_client):
    b = FakeBox(bridge.port, connect=False)
    b.send(publish_pkt(EVENTS, b'{"outside_tpt": 77}'))
    assert b.closed.wait(5)
    assert "before CONNECT" in bridge.log()
    assert '{"outside_tpt": 77}' not in mqtt_client.payloads(RAW)


def test_device_ip_filter(start_bridge, mqtt_client):
    bridge = start_bridge({"DEVICE_IP": "127.0.0.2"})
    for _ in range(3):
        with pytest.raises(OSError):
            FakeBox(bridge.port)  # from 127.0.0.1: closed before the TLS handshake
    assert wait(lambda: "127.0.0.1 REFUSED" in bridge.log())
    assert bridge.log().count("127.0.0.1 REFUSED") == 1  # rate-limited

    good = FakeBox(bridge.port, source_ip="127.0.0.2")
    good.publish(EVENTS, b'{"outside_tpt": 21}')
    assert wait(lambda: '{"outside_tpt": 21}' in mqtt_client.payloads(RAW))
    mqtt_client.publish("aldes/vmc/cmd/macro/quotidien", "1")
    assert wait(lambda: good.received('"V"'))
    good.close()


def test_invalid_device_ip_is_fatal(mqtt_port, tmp_path):
    import os
    import subprocess
    import sys

    from conftest import REPO
    env = dict(os.environ, CERT_DIR=str(tmp_path), DEVICE_IP="192.168.1.300", LOG_FILE=str(tmp_path / "x.log"))
    p = subprocess.run([sys.executable, str(REPO / "fake_iothub.py")], capture_output=True, text=True,
                       timeout=15, env=env)
    assert p.returncode != 0 and "invalid IP" in p.stderr


# ---------- shutdown ----------

@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_clean_shutdown_publishes_offline(bridge, box, mqtt_client, sig):
    assert wait(lambda: mqtt_client.last(STATUS) == "online")
    t0 = time.monotonic()
    assert bridge.stop(sig) == 0
    assert time.monotonic() - t0 < 4
    assert wait(lambda: mqtt_client.last(STATUS) == "offline")
    assert "shutting down" in bridge.log()
