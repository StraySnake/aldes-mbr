"""Unit tests of the pure functions of fake_iothub.py (no network)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import fake_iothub as fi


def mode_of(macro):
    payload = fi._macro_payload(macro)
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
    assert abs(start - datetime.now(timezone.utc)) < timedelta(minutes=1)
    return (end - start).days


@pytest.mark.parametrize("macro,days", [
    ("vacances", 1), ("Vacances", 1), ("w", 1), ("vacances1j", 1),
    ("vacances7j", 7), ("vacances21j", 21), ("vacances14d", 14), ("vacances365j", 365),
])
def test_holiday_durations(macro, days):
    assert _holiday_days(macro) == days


@pytest.mark.parametrize("macro", ["vacances0j", "vacances366j", "vacancesxj", "unknown", ""])
def test_invalid_macros(macro):
    assert fi._macro_payload(macro) is None


def test_holiday_stop_and_override():
    assert mode_of("vacances_stop") == "W" + "00010101000000Z" * 2
    assert json.loads(fi._macro_payload("override_on")) == {"method": "changeCMO", "params": [1]}
    assert json.loads(fi._macro_payload("override_off")) == {"method": "changeCMO", "params": [0]}


def test_every_select_option_maps_to_its_own_mode():
    """The HA select sends its label as the macro name: each label must round-trip."""
    assert set(fi.SELECT_OPTIONS) == set(fi.MODES.values())
    for code, label in fi.MODES.items():
        assert mode_of(label)[0] == code, label


class FakeSock:
    def __init__(self, data):
        self.data = bytearray(data)

    def recv(self, n):
        chunk, self.data = bytes(self.data[:n]), self.data[n:]
        return chunk


@pytest.mark.parametrize("n", [0, 1, 127, 128, 16383, 16384, 2097151, 2097152, 268435455])
def test_remaining_length_roundtrip(n):
    assert fi.read_remaining_length(FakeSock(fi.encode_remaining_length(n))) == n


def test_remaining_length_malformed():
    with pytest.raises(ValueError):
        fi.read_remaining_length(FakeSock(b"\xff\xff\xff\xff\x01"))


def test_recv_exact_raises_on_truncated_data():
    with pytest.raises(fi.ConnectionClosed):
        fi.recv_exact(FakeSock(b"abc"), 5)


def test_string_roundtrip():
    s, off = fi.parse_string(fi.encode_string("devices/é") + b"rest", 0)
    assert s == "devices/é" and off == len(fi.encode_string("devices/é"))


def test_redact_password():
    sas = b"SharedAccessSignature sr=hub%2Fdevices%2Fx&sig=AbC%2B123%3D&se=1700000000"
    red = fi.redact_password(sas)
    assert "AbC" not in red and "sig=<redacted>" in red and "se=1700000000" in red
    other = fi.redact_password(b"supersecretpassword")
    assert "secretpassword" not in other


def test_parse_allowed_ips():
    assert fi._parse_allowed_ips("") == set()
    assert fi._parse_allowed_ips(" 192.168.1.50 , 10.0.0.1,") == {"192.168.1.50", "10.0.0.1"}
    with pytest.raises(SystemExit):
        fi._parse_allowed_ips("192.168.1.300")
