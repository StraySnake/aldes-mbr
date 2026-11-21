"""End-to-end tests: amqtt (stand-in for Mosquitto) + the bridge process + fake TLS boxes."""
import json
import signal
import threading
import time

import pytest

from helpers import DEVICE_ID, EVENTS, FakeBox, publish_pkt, remaining_length, wait

RAW = f"aldes/vmc/raw/{EVENTS}"
LAST_SEEN = "aldes/vmc/last_seen"
STATUS = "aldes/vmc/status"
DEVICEBOUND = f"aldes/vmc/cmd/devices/{DEVICE_ID}/messages/devicebound"


def frames(mqtt_client):
    """Decoded telemetry frames republished by the bridge (skips retained clears and
    non-JSON payloads)."""
    out = []
    for p in mqtt_client.payloads(RAW):
        try:
            f = json.loads(p)
        except ValueError:
            continue
        if isinstance(f, dict):
            out.append(f)
    return out


def last_frame(mqtt_client):
    f = frames(mqtt_client)
    return f[-1] if f else {}


def any_frame_has(mqtt_client, key, value):
    return any(f.get(key) == value for f in frames(mqtt_client))


@pytest.fixture
def box(bridge):
    b = FakeBox(bridge.port)
    assert wait(lambda: "SUBSCRIBE" in bridge.log())
    yield b
    b.close()


# ---------- telemetry and commands ----------

def test_telemetry_relayed_and_status_online(bridge, box, mqtt_client):
    box.publish(EVENTS, b'{"outside_tpt": 12.5}')
    assert wait(lambda: last_frame(mqtt_client).get("outside_tpt") == 12.5)
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
    assert wait(lambda: len(frames(mqtt_client)) >= len(sent), 10)


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
    assert not any_frame_has(mqtt_client, "outside_tpt", 99)
    assert not box.closed.is_set()  # the real box keeps its session
    intruder.close()


def test_publish_before_connect_closes_connection(bridge, mqtt_client):
    b = FakeBox(bridge.port, connect=False)
    b.send(publish_pkt(EVENTS, b'{"outside_tpt": 77}'))
    assert b.closed.wait(5)
    assert "before CONNECT" in bridge.log()
    assert not any_frame_has(mqtt_client, "outside_tpt", 77)


def test_device_ip_filter(start_bridge, mqtt_client):
    bridge = start_bridge({"DEVICE_IP": "127.0.0.2"})
    for _ in range(3):
        with pytest.raises(OSError):
            FakeBox(bridge.port)  # from 127.0.0.1: closed before the TLS handshake
    assert wait(lambda: "127.0.0.1 REFUSED" in bridge.log())
    assert bridge.log().count("127.0.0.1 REFUSED") == 1  # rate-limited

    good = FakeBox(bridge.port, source_ip="127.0.0.2")
    good.publish(EVENTS, b'{"outside_tpt": 21}')
    assert wait(lambda: any_frame_has(mqtt_client, "outside_tpt", 21))
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


# ---------- telemetry merge and last update ----------

FULL = {"modemid": "AABBCCDDEEFF", "Vers_W": "21", "RSSI": "--%%", "outside_tpt": 22.2,
        "ext_tpt": 21.0, "current_mode": "V", "extf_flw": 165}
HEARTBEAT = {"modemid": "AABBCCDDEEFF", "Vers_W": "22", "RSSI": "--%%", "week_planning": " ", "Vers_UC": 53}


def test_heartbeat_merged_into_last_telemetry(bridge, box, mqtt_client):
    box.publish(EVENTS, json.dumps(FULL).encode())
    assert wait(lambda: last_frame(mqtt_client).get("outside_tpt") == 22.2)
    box.publish(EVENTS, json.dumps(HEARTBEAT).encode())
    assert wait(lambda: last_frame(mqtt_client).get("Vers_UC") == 53)
    merged = last_frame(mqtt_client)
    assert merged["outside_tpt"] == 22.2 and merged["current_mode"] == "V"  # kept
    assert merged["Vers_W"] == "22"  # updated by the newer frame
    box.publish(EVENTS, json.dumps({"outside_tpt": 18.0}).encode())
    assert wait(lambda: last_frame(mqtt_client).get("outside_tpt") == 18.0)
    assert last_frame(mqtt_client)["Vers_UC"] == 53


def test_non_json_frame_relayed_unchanged(bridge, box, mqtt_client):
    box.publish(EVENTS, b"not json")
    assert wait(lambda: mqtt_client.last(RAW) == "not json")


def test_merge_survives_bridge_restart(start_bridge, mqtt_client):
    bridge1 = start_bridge()
    b = FakeBox(bridge1.port)
    b.publish(EVENTS, json.dumps(FULL).encode())
    assert wait(lambda: last_frame(mqtt_client).get("outside_tpt") == 22.2)
    b.close()
    bridge1.stop()

    bridge2 = start_bridge(clear=False)  # Mosquitto still holds the merged frame
    assert wait(lambda: "seeded with" in bridge2.log())
    b = FakeBox(bridge2.port)
    b.publish(EVENTS, json.dumps(HEARTBEAT).encode())
    assert wait(lambda: last_frame(mqtt_client).get("Vers_UC") == 53)
    assert last_frame(mqtt_client)["outside_tpt"] == 22.2
    b.close()


def test_last_seen_published_and_discovered(bridge, box, mqtt_client, mqtt_port):
    from datetime import datetime, timedelta, timezone

    from conftest import Recorder

    box.publish(EVENTS, json.dumps(HEARTBEAT).encode())
    assert wait(lambda: mqtt_client.last(LAST_SEEN))
    seen = datetime.fromisoformat(mqtt_client.last(LAST_SEEN))
    assert seen.tzinfo is not None
    assert abs(datetime.now(timezone.utc) - seen) < timedelta(seconds=10)

    cfg_topic = "homeassistant/sensor/aldes_vmc_AABBCCDDEEFF/last_seen/config"
    rec = Recorder(mqtt_port, "disco-reader")
    rec.client.subscribe("homeassistant/#", qos=1)
    assert wait(lambda: rec.last(cfg_topic))
    cfg = json.loads(rec.last(cfg_topic))
    rec.close()
    assert cfg["device_class"] == "timestamp" and cfg["state_topic"] == LAST_SEEN
    assert "availability_topic" not in cfg  # must stay visible while the box is offline


def test_rejected_publish_does_not_update_last_seen(bridge, mqtt_client):
    intruder = FakeBox(bridge.port, cid="intruder")
    intruder.publish(EVENTS, b"{}")
    assert wait(lambda: "not relayed" in bridge.log())
    time.sleep(0.5)
    assert not mqtt_client.last(LAST_SEEN)
    intruder.close()


# ---------- Home Assistant discovery ----------

def discovery(mqtt_port):
    from conftest import Recorder
    rec = Recorder(mqtt_port, "disco-reader")
    rec.client.subscribe("homeassistant/#", qos=1)
    assert wait(lambda: len([t for t in rec.msgs if t.startswith("homeassistant/")]) >= 28)
    rec.close()
    return {t: json.loads(rec.last(t)) for t in rec.msgs if t.startswith("homeassistant/") and rec.last(t)}


def test_discovery_expire_after_holiday_and_buttons(bridge, mqtt_port):
    cfgs = discovery(mqtt_port)
    base = "homeassistant/sensor/aldes_vmc_AABBCCDDEEFF"
    assert cfgs[f"{base}/outside_temperature/config"]["expire_after"] == 900
    assert "expire_after" not in cfgs[f"{base}/last_seen/config"]
    for obj in ("holiday_start", "holiday_end"):
        cfg = cfgs[f"{base}/{obj}/config"]
        assert cfg["device_class"] == "timestamp" and "_date_mode" in cfg["value_template"]
    assert cfgs[f"{base}/last_command/config"]["state_topic"] == "aldes/vmc/command_result"
    btn = "homeassistant/button/aldes_vmc_AABBCCDDEEFF"
    assert cfgs[f"{btn}/btn_override_on/config"]["command_topic"] == "aldes/vmc/cmd/macro/override_on"
    assert f"{btn}/btn_override_off/config" in cfgs


def test_expire_after_can_be_disabled(start_bridge, mqtt_port):
    start_bridge({"SENSOR_EXPIRE_AFTER": "0"})
    cfgs = discovery(mqtt_port)
    assert "expire_after" not in cfgs["homeassistant/sensor/aldes_vmc_AABBCCDDEEFF/outside_temperature/config"]


# ---------- command confirmation ----------

RESULT = "aldes/vmc/command_result"


def result(mqtt_client):
    last = mqtt_client.last(RESULT)
    return json.loads(last) if last else {}


def statuses(mqtt_client):
    return [json.loads(p)["status"] for p in mqtt_client.payloads(RESULT) if p]


@pytest.fixture
def cmd_box(start_bridge):
    bridge = start_bridge({"CMD_CONFIRM_GRACE": "0.2", "CMD_CONFIRM_TIMEOUT": "30"})
    b = FakeBox(bridge.port)
    assert wait(lambda: "SUBSCRIBE" in bridge.log())
    yield bridge, b
    b.close()


def report_mode(box, mode):
    time.sleep(0.3)  # past the grace period
    box.publish(EVENTS, json.dumps({"current_mode": mode, "outside_tpt": 20.0}).encode())


def test_command_confirmed_by_telemetry(cmd_box, mqtt_client):
    bridge, box = cmd_box
    n = len(statuses(mqtt_client))
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: box.received('"X"'))
    assert wait(lambda: "received" in statuses(mqtt_client)[n:])  # PUBACK from the box
    box.publish(EVENTS, json.dumps(HEARTBEAT).encode())  # no current_mode: ignored
    report_mode(box, "X")
    assert wait(lambda: result(mqtt_client).get("status") == "confirmed")
    r = result(mqtt_client)
    assert r["command"] == "boost" and r["expected_mode"] == "X" and r["reported_mode"] == "X"


def test_command_retried_once_then_confirmed(cmd_box, mqtt_client):
    bridge, box = cmd_box
    mqtt_client.publish("aldes/vmc/cmd/macro/_select", "Invités")
    assert wait(lambda: box.received('"Y"'))
    report_mode(box, "V")  # old mode: resent once
    assert wait(lambda: sum('"Y"' in r for r in box.rx) == 2)
    assert wait(lambda: len(set(box.acked)) == 2)  # the resend has its own packet id
    assert wait(lambda: result(mqtt_client).get("status") == "retrying")
    report_mode(box, "Y")
    assert wait(lambda: result(mqtt_client).get("status") == "confirmed")


def test_command_failed_after_one_retry(cmd_box, mqtt_client):
    bridge, box = cmd_box
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: box.received('"X"'))
    report_mode(box, "V")
    assert wait(lambda: result(mqtt_client).get("status") == "retrying")
    report_mode(box, "V")
    assert wait(lambda: result(mqtt_client).get("status") == "failed")
    assert sum('"X"' in r for r in box.rx) == 2  # one retry only
    report_mode(box, "V")
    time.sleep(0.5)
    assert sum('"X"' in r for r in box.rx) == 2


def test_frame_within_grace_period_ignored(start_bridge, mqtt_client):
    bridge = start_bridge({"CMD_CONFIRM_GRACE": "5"})
    box = FakeBox(bridge.port)
    assert wait(lambda: "SUBSCRIBE" in bridge.log())
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: box.received('"X"'))
    box.publish(EVENTS, json.dumps({"current_mode": "V"}).encode())  # sampled before the change
    assert wait(lambda: any_frame_has(mqtt_client, "current_mode", "V"))
    time.sleep(0.3)
    assert result(mqtt_client).get("status") == "received"  # acknowledged, not confirmed yet
    assert sum('"X"' in r for r in box.rx) == 1
    box.close()


def test_holiday_stop_confirmed_when_mode_leaves_w(cmd_box, mqtt_client):
    bridge, box = cmd_box
    mqtt_client.publish("aldes/vmc/cmd/macro/vacances_stop", "1")
    assert wait(lambda: box.received("00010101000000Z"))
    report_mode(box, "V")
    assert wait(lambda: result(mqtt_client).get("status") == "confirmed")
    assert result(mqtt_client)["expected_mode"] == "!W"


def test_newer_command_replaces_pending_one(cmd_box, mqtt_client):
    bridge, box = cmd_box
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    mqtt_client.publish("aldes/vmc/cmd/macro/quotidien", "1")
    assert wait(lambda: box.received('"V"'))
    report_mode(box, "V")
    assert wait(lambda: result(mqtt_client).get("status") == "confirmed")
    assert result(mqtt_client)["command"] == "quotidien"
    assert sum('"X"' in r for r in box.rx) == 1  # boost not retried


def test_unverifiable_command_reported_as_sent(cmd_box, mqtt_client):
    bridge, box = cmd_box
    n = len(statuses(mqtt_client))
    mqtt_client.publish("aldes/vmc/cmd/macro/programme", "1")
    assert wait(lambda: statuses(mqtt_client)[n:][:1] == ["sent"])  # then "received" (PUBACK)
    mqtt_client.publish("aldes/vmc/cmd/macro/override_on", "1")
    assert wait(lambda: box.received('"changeCMO"'))
    assert wait(lambda: result(mqtt_client).get("command") == "override_on")
    assert wait(lambda: result(mqtt_client)["status"] == "received")


def test_command_unconfirmed_after_timeout(start_bridge, mqtt_client):
    bridge = start_bridge({"CMD_CONFIRM_TIMEOUT": "1"})
    box = FakeBox(bridge.port)
    assert wait(lambda: "SUBSCRIBE" in bridge.log())
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: box.received('"X"'))
    assert wait(lambda: result(mqtt_client).get("status") == "unconfirmed", 5)
    box.close()


def test_command_without_box_reported(bridge, mqtt_client):
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: result(mqtt_client).get("status") == "not_connected")


# ---------- QoS 1 commands and PUBACK ----------

def test_commands_sent_with_qos1_and_acknowledged(cmd_box, mqtt_client):
    bridge, box = cmd_box
    n = len(statuses(mqtt_client))
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: box.acked)
    assert json.loads(box.rx[-1]) == {"method": "changeMode", "params": ["X"]}  # intact JSON
    assert wait(lambda: "received" in statuses(mqtt_client)[n:])
    report_mode(box, "X")
    assert wait(lambda: result(mqtt_client).get("status") == "confirmed")
    assert statuses(mqtt_client)[n:] == ["pending", "received", "confirmed"]


def test_real_box_with_qos0_loses_the_first_two_bytes(start_bridge, mqtt_client):
    # What the logs of a real Connect Box showed: PUBACK with pkt_id 0x7b22 ('{"').
    bridge = start_bridge({"CMD_QOS": "0"})
    box = FakeBox(bridge.port, quirky=True)
    assert wait(lambda: "SUBSCRIBE" in bridge.log())
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: box.rx)
    assert box.rx[-1] == 'method":"changeMode","params":["X"]}'
    assert box.acked == [0x7B22]
    assert wait(lambda: "Set CMD_QOS=1" in bridge.log())
    box.close()


def test_real_box_with_qos1_gets_the_whole_command(start_bridge, mqtt_client):
    bridge = start_bridge()
    box = FakeBox(bridge.port, quirky=True)
    assert wait(lambda: "SUBSCRIBE" in bridge.log())
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: box.rx)
    assert json.loads(box.rx[-1]) == {"method": "changeMode", "params": ["X"]}
    assert wait(lambda: result(mqtt_client).get("status") == "received")
    box.close()


def test_programme_acknowledged_and_reported_mode_recorded(cmd_box, mqtt_client):
    bridge, box = cmd_box
    mqtt_client.publish("aldes/vmc/cmd/macro/programme", "1")
    assert wait(lambda: result(mqtt_client).get("status") == "received")
    report_mode(box, "V")  # what the box reports in Programme mode, whatever it is
    assert wait(lambda: result(mqtt_client).get("reported_mode") == "V")
    assert result(mqtt_client)["status"] == "received"
    assert "box reports V" in bridge.log()


def test_without_puback_still_confirmed_by_telemetry(start_bridge, mqtt_client):
    bridge = start_bridge({"CMD_CONFIRM_GRACE": "0.2"})
    box = FakeBox(bridge.port, ack=False)
    assert wait(lambda: "SUBSCRIBE" in bridge.log())
    n = len(statuses(mqtt_client))
    mqtt_client.publish("aldes/vmc/cmd/macro/boost", "1")
    assert wait(lambda: box.received('"X"'))
    report_mode(box, "X")
    assert wait(lambda: result(mqtt_client).get("status") == "confirmed")
    assert statuses(mqtt_client)[n:] == ["pending", "confirmed"]
    box.close()


def test_full_frame_logged_in_debug(start_bridge):
    bridge = start_bridge({"LOG_LEVEL": "debug"})
    box = FakeBox(bridge.port)
    frame = {"pad": "x" * 400, "current_mode": "V"}
    box.publish(EVENTS, json.dumps(frame).encode())
    assert wait(lambda: '"current_mode": "V"' in bridge.log())
    box.close()
