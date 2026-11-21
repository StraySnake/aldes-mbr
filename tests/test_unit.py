"""Unit tests of the pure functions of the aldes_bridge package (no network)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from aldes_bridge import commands, config, logs, macros
from aldes_bridge import mqtt_codec as codec


def mode_of(macro):
    payload = macros.macro_payload(macro)
    return json.loads(payload)["params"][0] if payload else None


@pytest.mark.parametrize("macro,code", [
    ("quotidien", "V"), ("v", "V"), ("Quotidien", "V"),
    ("invites", "Y"), ("Invités", "Y"), ("y", "Y"),
    ("boost", "X"), ("Boost", "X"), ("max", "X"),
    ("programme", "Z"), ("z", "Z"),
])
def test_simple_modes(macro, code):
    assert mode_of(macro) == code


def _holiday_days(macro):
    code = mode_of(macro)
    assert code.startswith("W") and len(code) == 1 + 2 * 15
    fmt = "%Y%m%d%H%M%SZ"
    start = datetime.strptime(code[1:16], fmt).replace(tzinfo=timezone.utc)
    end = datetime.strptime(code[16:], fmt).replace(tzinfo=timezone.utc)
    # starts 1 min in the past (margin for a box clock slightly behind)
    ago = datetime.now(timezone.utc) - start
    assert timedelta(seconds=59) <= ago < timedelta(minutes=2)
    return (end - start).days


@pytest.mark.parametrize("macro,days", [
    ("vacances", 1), ("Vacances", 1), ("w", 1), ("vacances1j", 1),
    ("vacances7j", 7), ("vacances21j", 21), ("vacances14d", 14), ("vacances365j", 365),
])
def test_holiday_durations(macro, days):
    assert _holiday_days(macro) == days


@pytest.mark.parametrize("macro", ["vacances0j", "vacances366j", "vacancesxj", "unknown", ""])
def test_invalid_macros(macro):
    assert macros.macro_payload(macro) is None


def test_holiday_stop_and_override():
    # a real box ignores the app's "W" + 0001-01-01 dates: cancel by switching mode
    assert mode_of("vacances_stop") == "V"
    assert json.loads(macros.macro_payload("vacances_stop", resume_mode="Y"))["params"] == ["Y"]
    assert json.loads(macros.macro_payload("vacances_stop", resume_mode="W"))["params"] == ["V"]
    assert json.loads(macros.macro_payload("override_on")) == {"method": "changeCMO", "params": [1]}
    assert json.loads(macros.macro_payload("override_off")) == {"method": "changeCMO", "params": [0]}


def test_every_select_option_maps_to_its_own_mode():
    """The HA select sends its label as the macro name: each label must round-trip."""
    assert set(macros.SELECT_OPTIONS) == set(macros.MODES.values())
    for code, label in macros.MODES.items():
        assert mode_of(label)[0] == code, label


class FakeSock:
    def __init__(self, data):
        self.data = bytearray(data)

    def recv(self, n):
        chunk, self.data = bytes(self.data[:n]), self.data[n:]
        return chunk


@pytest.mark.parametrize("n", [0, 1, 127, 128, 16383, 16384, 2097151, 2097152, 268435455])
def test_remaining_length_roundtrip(n):
    assert codec.read_remaining_length(FakeSock(codec.encode_remaining_length(n))) == n


def test_remaining_length_malformed():
    with pytest.raises(ValueError):
        codec.read_remaining_length(FakeSock(b"\xff\xff\xff\xff\x01"))


def test_recv_exact_raises_on_truncated_data():
    with pytest.raises(codec.ConnectionClosed):
        codec.recv_exact(FakeSock(b"abc"), 5)


def test_string_roundtrip():
    s, off = codec.parse_string(codec.encode_string("devices/é") + b"rest", 0)
    assert s == "devices/é" and off == len(codec.encode_string("devices/é"))


def test_redact_password():
    sas = b"SharedAccessSignature sr=hub%2Fdevices%2Fx&sig=AbC%2B123%3D&se=1700000000"
    red = logs.redact_password(sas)
    assert "AbC" not in red and "sig=<redacted>" in red and "se=1700000000" in red
    other = logs.redact_password(b"supersecretpassword")
    assert "secretpassword" not in other


def test_parse_allowed_ips():
    assert config.parse_allowed_ips("") == set()
    assert config.parse_allowed_ips(" 192.168.1.50 , 10.0.0.1,") == {"192.168.1.50", "10.0.0.1"}
    with pytest.raises(SystemExit):
        config.parse_allowed_ips("192.168.1.300")


@pytest.mark.parametrize("macro,expected", [
    ("boost", ("is", "X")), ("quotidien", ("is", "V")), ("invites", ("is", "Y")),
    ("vacances7j", ("is", "W")), ("vacances_stop", ("is", "V")),
    ("programme", ("is", "Z")),  # a real box reports current_mode "Z" in Programme mode
    ("override_on", None),    # changeCMO: not reported in the telemetry
])
def test_expected_mode_of_macros(macro, expected):
    assert commands.expected_mode(macros.macro_payload(macro)) == expected


@pytest.mark.parametrize("payload", [b"not json", b"{}", b'{"method":"changeMode","params":[]}',
                                     b'{"method":"changeMode","params":[1]}',
                                     b'{"method":"changeMode","params":["Q"]}'])
def test_expected_mode_of_unverifiable_payloads(payload):
    assert commands.expected_mode(payload) is None


def test_parse_holiday():
    start, end = macros.parse_holiday("W20261121223139Z20261122223139Z")
    assert start.isoformat() == "2026-11-21T22:31:39+00:00"
    assert end.isoformat() == "2026-11-22T22:31:39+00:00"
    assert macros.parse_holiday("W00010101000000Z00010101000000Z") is None
    assert macros.parse_holiday("V") is None
    assert macros.parse_holiday("Wgarbage") is None


def test_box_clock_from_sas_token():
    from aldes_bridge import clock
    pwd = "SharedAccessSignature sr=hub%2Fdevices%2Fx&sig=abc%3D&se=1478918800"
    assert clock.parse_se(pwd) == 1478918800
    assert clock.parse_se("SharedAccessSignature sr=x&sig=y") is None
    now = 1795300000  # 2026-11-21
    assert not clock.is_clock_set(1478918800, now)      # boot date, Nov 2016
    assert clock.is_clock_set(now + 38790, now)          # set (expiry a few hours ahead)
    assert clock.is_clock_set(now - 3600, now)           # small offsets tolerated
