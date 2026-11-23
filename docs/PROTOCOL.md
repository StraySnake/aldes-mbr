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

Sent every ~10 min (and ~5 s after each command):

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

## Observed behaviour of the box (Dee Fly Cube, logs of a real installation)

- TLS: negotiates **TLS 1.2, `AES256-SHA256`** with our broker.
- In Programme mode, `current_mode` is `"Z"` (not the mode of the current time slot).
- Telemetry also reports `Vers_UC` (e.g. 26) and `Vers_W` as a Unix timestamp (e.g. `1544745600.0` = 2018-12-14), not the `"21"` shown above.
- MQTT CONNECT: keepalive **180 s**, clean session, SAS token as password; subscribes to `devices/<id>/messages/devicebound/#` with QoS 1.
- Telemetry: a ~410-byte frame every **10 min** (QoS 1), **plus one about 5 s after each command** it receives.
- A few times a day it also sends a **truncated, invalid frame**, e.g. `{"modemid":"…","productid":"…",+++}`. The bridge ignores it (only "last update" is refreshed) so that it does not replace the retained telemetry.
- After a mode change, the frame sent ~5 s later already has the new `current_mode` and `set_spd`, but flows, fan speeds and `echange_pwr` follow about a minute later (next frames).

### Telemetry fields, from 13 h of frames of one Dee Fly Cube

| Field | Observed | Meaning |
|---|---|---|
| `extf_flw`, `ffe_flw` | always identical, and only 105 (V), 195 (Y), 210 (X) m³/h | **flow setpoint of the mode** (configured per installation), not a measurement. Holidays (W) stayed at 105 over 4 min, so the holiday flow may equal the daily one here. |
| `set_spd` | 0 W, 1 V, 2 Y, 3 X | speed step of the mode |
| `echange_pwr` | 441–475 at 105 m³/h, 825–910 at 210 m³/h, inside 23.5 °C, outside 9.5 °C | **heat recovered by the exchanger, in W**: 0.34 × flow × ΔT × ~0.9 gives 450 W and 900 W |
| `extf_spd`, `vi_spd` | 2159–2585 / 1539–1663 rpm at 105 m³/h; ~2900 / ~2800 rpm at 195–210 m³/h | extract / supply fan speeds (rpm) |
| `cve_csn`, `vi_csn` | 27–42 / 13–20 at 105 m³/h; 93–99 / 83–92 at 195–210 m³/h, never above 100 | follow the extract / supply fan exactly. Most likely the **fan command in %** ("csn" = consigne): doubling it at the top only adds ~10 % rpm, as a fan near its maximum speed does; electrical power could not double for +10 % rpm. Not formally confirmed; exposed as *Commande ventilateur extraction / insufflation* (%). |
| `tte_csn` | 2790 for at least 14 h, then 2791 (11:34 the next day) | a slow counter, maybe days of operation (≈ 7.6 years). To confirm with the next increments. |
| `ff_cpt` | 0, while the unit's remote showed "filter 0 %, change it"; 50 in the Aldes app's demo data | **remaining filter life in %**. Exposed as *Filtre (durée de vie restante)* and *Filtre à changer*. |
| `dep_ind` | always 0 | unknown; maybe defrost: watch it on frosty nights. |
| `dtb_ind` | 0 for at least a day; 55–255, changing at every frame, once the comfort mode was set to "favour cool" (bypass open); then 255 in "favour heat" and in "auto" (bypass closed) | does **not** encode the comfort mode (same value in heat and auto) nor the bypass state. Maybe a timer started by a change on the remote (the manual mentions a 3 h manual bypass override): watch whether it goes back to 0. |
- **Its clock is wrong.** The `se` (expiry) of the SAS token it generates at each connection is in November 2016, follows real time from there, and goes back to the same 2016 date after a reboot: the box counts time since boot from a fixed date until it reaches its NTP server. **That server is a hard-coded IP address, `61.164.36.105` (UDP 123)**, queried about every 24 s while unsynchronised: no DNS lookup, so a local NTP server announced by DHCP is not used. If outbound traffic from the box is blocked, its clock stays in 2016. Consequence: a holiday range dated now (2026) is in the box's future, so `changeMode W…` is acknowledged but not applied (and a weekly schedule runs at wrong times). Fix: let it reach that address, or better, redirect UDP 123 from the box to a local NTP server with a destination NAT rule on the router.
- **It reads a packet id in every PUBLISH it receives, even QoS 0 ones, and sends a PUBACK for it.** With a QoS 0 command, the first 2 bytes of the payload (`{"`) are taken as the packet id (PUBACK id `0x7b22`) and the command it parses starts with `method":…`. Mode changes seemed to work anyway (its parser is lenient), but commands are now sent with QoS 1, which gives it a real packet id and makes the PUBACK a receipt.

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

| code | name (FR/EN) | `set_spd` | flow on one Dee Fly Cube (configured per installation) |
|---|---|---|---|
| `V` | Quotidien / Daily | 1 | 105 m³/h (~165 m³/h on the original project's unit) |
| `W` | Vacances / Holiday | 0 | 105 m³/h (~50 m³/h on the original project's unit) — see below |
| `X` | Boost (max) | 3 | 210 m³/h (~330 m³/h) |
| `Y` | Invités / Guests | 2 | 195 m³/h for ~40 min, then 105 m³/h (~250 m³/h) |
| `Z` | Programme / Schedule | varies | follows weekly plan |

Notes:
- Names differ between Aldes documents: the InspirAir Home Modbus document numbers the speeds 0 Vacances, 1 Normal, 2 « Pointe cuisine », 3 « Invités ». On a Dee Fly Cube, `X` is the highest speed (`set_spd` 3) and `Y` the intermediate one (2).
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

**On a real Dee Fly Cube this does not work**: the box acknowledges it and stays in `W`. Sending `changeMode` with another mode (`V`, `X`, etc.) does end the holidays, within seconds; that is what the bridge's `vacances_stop` macro does, restoring the mode used before the holidays.

While in holidays, the box reports `current_mode: "W"` and `set_spd: 0`, but leaves `Start_date_mode` / `End_date_mode` empty: the bridge publishes the range itself on `<prefix>/holiday`.

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

We don't use this in `aldes-mbr` directly — Home Assistant handles scheduling.

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

By being the broker ourselves, we don't have this problem: when we receive a command from HA, we send it immediately on the box's currently-active MQTT session, with QoS 1. The box's PUBACK confirms receipt, and the telemetry frame it sends a few seconds later confirms the new `current_mode`.

## What the Aldes Connect app knows (version 4.21)

The string table of the Android app (APK 4.21, read without running it) lists every command it can send to a Connect Box: `changeMode`, `changeCMO`, `changePlanningMode` (variants A–D), `changeTemperatureReference` and `prixkwh`, built from three command types only (string, on/off override, temperature). There is **no bypass command**: the word does not appear in Aldes' code, and the "Rafraîchissement" / heating and cooling screens, the `COOLING_*` / `HEATING_*` modes, the temperature setpoints and the A–D programs all belong to the T.One heat pump. The comfort mode of a Dee Fly Cube (favour heat / favour cool / auto), which drives its bypass, is set with the unit's remote, which talks to the unit directly, not through the Connect Box.

The app has a **filter alert** for InspirAir / Dee Fly Cube ("reset the filter timer on your unit" after a change), consistent with `ff_cpt` being the remaining filter life. Its demo data (`assets/indicators/inspir_air_demo.json`) also names fields not sent by the box observed here: `AIR_TII_EST`, `AIR_EXTF_TSN`, `AIR_VV_TSN`, `AIR_EXCH_ENG`, and probe values (`AIR_TEMP_CAPT`, `AIR_HYGR_CAPT`, `AIR_CO2_CAPT`, …).


## Bypass and comfort mode: what the telemetry shows

Two settings are independent: the **ventilation flow profile** (`current_mode`: V, W, X, Y, Z), set through the box, and the **comfort mode** (favour heat / favour cool / auto, in the Aldes manual), set with the unit's remote only, which decides when the bypass opens. The box has no command for the comfort mode (see above) and reports it nowhere directly, but its effects are visible. On a real Dee Fly Cube, 23.5 °C inside and 10.5 °C outside, after setting the comfort mode to "favour cool":

| | Before | "Favour cool" (≈ 13:00–14:09): bypass open | "Favour heat" (from 14:09) |
|---|---|---|---|
| `echange_pwr` | 430 W | **0 W** | 410 W |
| `reject_tpt` | 15.5 °C | 19.0–19.5 °C | 15.1 °C |
| flows | 105 m³/h | **210 m³/h**, fans at 96–100 %, even after switching to Quotidien (V) | 105 m³/h |

The change shows in the frame sent right after the remote is set (here 14:09:43 for "favour heat"). With "favour cool" and a warm house, the unit opened the bypass and cooled at 210 m³/h, above the flow of the profile: `current_mode` was not changed (it stayed `Y`, then became `V` when Quotidien was selected, while the flow stayed at 210 m³/h). "Favour heat" closed the bypass at once and the flow went back to the profile's. The bridge's *By-pass ouvert* binary sensor uses `echange_pwr = 0` while air flows (unknown when inside and outside differ by less than 2 °C).

### Auto

In "auto", the unit chooses by itself from the indoor and outdoor temperatures. In November (23 °C inside, 11 °C outside), switching from "favour heat" to "auto" changed nothing: bypass closed, ~410 W recovered, flow of the profile.

### Changing the comfort mode: research

- **Connect Box**: no command. The Aldes Connect app 4.21 only sends `changeMode`, `changeCMO`, `changePlanningMode`, `changeTemperatureReference` and `prixkwh`; its InspirAir settings screen has no comfort or bypass setting.
- **Modbus**: the Aldes document *ModBus EasyVEC® & InspirAir® Home* (11024829-B, 02/2017) lists, for the InspirAir Home (RS485, 115200 bauds, even parity, 1 stop bit, slave 2), register **259 ($103) *Gestion Bypass Echangeur*: 0 OFF, 1 Auto, 2 winter optimisation, 3 summer optimisation, 4 open**, and register 348 ($15C) *Etat Bypass* (0 closed, 1 open, 2 closing, 3 opening…). The Dee Fly Cube board has no Modbus terminal: its 2-wire green terminal is the **iBus**, which powers the Connect Box and carries its data; its 10-pin connector takes the radio receiver of the remote.
- **Remote**: the Dee Fly Cube remote (« ALS CDE SANS FIL DEE FLY », ref. 11023850) is wireless, with a **TI CC110L** sub-GHz transceiver (26 MHz crystal) driven over SPI by an Aldes-programmed microcontroller (« PRG-1348/A 1.31 »). The link is two-way: the remote shows the unit's filter alert and sends its own indoor temperature. Next step: capture the SPI bus of the CC110L (radio configuration at power-up, packets sent when the comfort mode is changed), then reproduce the packets with an ESP32 and a CC1101 module.

## Other observations

- `changeMode "A"` (the "Auto" mode of the Aldes app, seen in its InspirAir / EasyHome demo data) is acknowledged by a Dee Fly Cube but ignored: `current_mode` stays unchanged.
- Invités (`Y`): 195 m³/h for about 40 min, then back to 105 m³/h while `current_mode` stays `Y` and `set_spd` 2: the guest boost seems time-limited.
