# Aldes Connect Box MQTT protocol

What we observed and reverse-engineered. All payloads are real (anonymized) captures from a Dee Fly Cube unit.

## Connection

The box opens a TLS-MQTT connection to **`aldesiotsuite.azure-devices.net:8883`** every few minutes. This is a standard **Azure IoT Hub MQTT** endpoint.

### CONNECT packet

```
proto      = MQTT
level      = 4 (MQTT 3.1.1)
keepalive  = 180 s
client_id  = <DEVICE_MAC>_AIR              # e.g. AABBCCDDEEFF_AIR
username   = aldesiotsuite.azure-devices.net/<DEVICE_MAC>_AIR/api-version=2016-11-14
password   = SharedAccessSignature sr=...&sig=...&se=...&skn=...
```

The SharedAccessSignature is a SAS token signed by the device's symmetric key (provisioned by Aldes during onboarding). We don't need it — we're the broker, we just accept and reply CONNACK with status 0.

### SUBSCRIBE

The box subscribes once after CONNECT:

```
topic = devices/<DEVICE_MAC>_AIR/messages/devicebound/#
qos   = 1
```

This is the standard **Cloud-to-Device** topic in Azure IoT Hub. Anything we publish here, the box receives.

## Telemetry (Device → Cloud)

The box periodically PUBLISHes a JSON payload on:

```
topic = devices/<DEVICE_MAC>_AIR/messages/events/
qos   = 1
```

### Boot heartbeat (~118 bytes)

Sent shortly after connect:

```json
{
  "modemid": "AABBCCDDEEFF",
  "Vers_W": "21",
  "productid": "AABBCCDDEEFF_AIR",
  "RSSI": "--%%",
  "week_planning": " ",
  "Vers_UC": 53
}
```

### Full telemetry (~414 bytes)

Sent every ~1–5 min:

```json
{
  "modemid": "AABBCCDDEEFF",
  "Vers_W": "21",
  "productid": "AABBCCDDEEFF_AIR",
  "RSSI": "--%%",
  "outside_tpt": 22.2,        // outdoor air temperature (°C)
  "reject_tpt": 23.5,         // air rejected by the unit (°C)
  "extf_spd": 1676,           // extraction fan speed (rpm)
  "vi_spd": 2412,             // intake fan speed (rpm)
  "ff_cpt": 0,
  "ffe_flw": 165,             // intake airflow (m³/h)
  "extf_flw": 165,            // extraction airflow (m³/h)
  "dep_ind": 38,
  "ext_tpt": 23.0,            // indoor air temperature (extracted) (°C)
  "cve_csn": 22,
  "vi_csn": 47,
  "tte_csn": 1397,
  "dtb_ind": 0,
  "echange_pwr": 44,          // heat exchanger power (W)
  "set_spd": 1,               // current speed setpoint (0..3)
  "Start_date_mode": "",      // active for holiday mode
  "End_date_mode": "",
  "current_mode": "V",        // V/W/X/Y/Z (see below)
  "Co2_vmc": 0.0,             // 0 if no CO2 sensor
  "Pm25_Vmc": 0.0,
  "Voc_Vmc": 0,
  "Vers_UC": 53
}
```

## Commands (Cloud → Device)

We publish commands on:

```
topic   = devices/<DEVICE_MAC>_AIR/messages/devicebound
payload = JSON-RPC 2.0 envelope
```

### `changeMode` — set ventilation mode

```json
{"method": "changeMode", "params": ["V"]}
```

Single-param string. Valid values:

| code | name (FR/EN) | speed on Dee Fly Cube |
|---|---|---|
| `V` | Quotidien / Daily | V2 (~165 m³/h) |
| `W` | Vacances / Holiday | V1 (~50 m³/h) — see below |
| `X` | Boost (max) | V4 (~330 m³/h) |
| `Y` | Invités / Guests / Kitchen | V3 (~250 m³/h) |
| `Z` | Programme / Schedule | follows weekly plan |

Notes:
- **X and Y are inverted vs InspirAir docs**. On Dee Fly Cube, X is the "max boost" code that returns the highest fan speed.
- Holiday mode (`W`) alone is **ignored** — it requires concatenated start/end dates.

### `changeMode W…` — holiday mode with date range

The single string `params[0]` is `"W" + startDate + endDate` where each date is formatted **`yyyyMMddHHmmss'Z'`** (UTC, no separators):

```json
{"method":"changeMode","params":["W20260429120000Z20260506120000Z"]}
```

Above = "set holiday mode from 2026-04-29T12:00:00Z to 2026-05-06T12:00:00Z".

To **cancel** holiday mode the Aldes app sends an epoch placeholder:

```json
{"method":"changeMode","params":["W00010101000000Z00010101000000Z"]}
```

Or simply send `changeMode` with another mode (`V`, `X`, etc.) to overwrite.

### `changeCMO` — toggle "current mode override"

```json
{"method":"changeCMO","params":[1]}
```

Single integer (0 or 1). When `1`, the box stays on the current mode and ignores the weekly schedule. When `0`, schedule resumes.

### `changePlanningMode` — edit weekly schedule

```json
{"method":"changePlanningMode","params":["00V","01V","02V",...,"66V"]}
```

168 entries, one per `(hour, day)` slot. Each entry is `"{hour}{day}{mode}"`:
- `hour` is `0`–`9` for 0–9, then ASCII letters from `A` (=10) up to `N` (=23). The Java code applies `(c - 7) % 24` for letters.
- `day` is `0`–`6` (Sunday–Saturday).
- `mode` is one of `V/W/X/Y/Z` (and `B`/`C` for products with heating/cooling).

We don't use this in `aldes-mqtt-bridge` directly — Home Assistant handles scheduling.

### Other commands seen in the APK (not used by this project)

- `changeTemperatureReference` — for T.One, T.Flow (heating setpoint)
- `prixkwh` — to set kWh price for energy stats

## JSON-RPC envelope

The Aldes app code (`BaseCommand.kt`) uses these defaults:

```json
{"id": 1, "jsonrpc": "2.0", "method": "...", "params": [...]}
```

In practice the box accepts payloads without `id` and `jsonrpc` fields too. We send the minimal `{"method":..., "params":...}` and it works fine.

## Why the cloud-to-device path can break for users

When the box's MQTT connection to Azure drops (every few minutes, especially on weak 2.4 GHz signal), any C2D commands queued at Azure are lost — Azure IoT Hub's default C2D message TTL is 1 hour but the **delivery only happens while the device is connected**. If the box reconnects every 30s but the auth fails 5 times out of 6 (Aldes' SAS-key minting endpoint returns 503 frequently), the C2D queue piles up and the box never sees the command.

By being the broker ourselves, we don't have this problem: when we receive a command from HA, we send it immediately on the box's currently-active MQTT session (QoS 0: the box does not acknowledge it, so delivery is not confirmed — check the next telemetry frame's `current_mode`).
