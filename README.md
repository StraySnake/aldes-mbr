# aldes-mbr

> Local control of an Aldes ventilation unit (VMC / HRV) with a Connect Box, from Home Assistant, without the Aldes cloud.

aldes-mbr is a fork of [djo1338/aldes-mqtt-bridge](https://github.com/djo1338/aldes-mqtt-bridge), released under the same [MIT license](LICENSE). It keeps the same idea — a fake Azure IoT Hub the box talks to — and adds command confirmation, a time source for the box, more Home Assistant entities, access control, and a test suite. See [Changes from aldes-mqtt-bridge](#changes-from-aldes-mqtt-bridge).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/ghcr.io-aldes--mbr-blue?logo=docker)](https://github.com/StraySnake/aldes-mbr/pkgs/container/aldes-mbr)

Developed and tested on an **Aldes Dee Fly Cube** with its Connect Box. Other units that use the same Connect Box and Azure backend (InspirAir Home, T.Flow, T.One…) may work, but have not been tested: check the logs and the [ventilation profiles](#débit-de-ventilation-ventilation-profiles) before trusting the labels.

## Why

- The Aldes Connect app often fails to control the unit: errors when logging in, commands accepted by the cloud but never delivered to the box.
- The cloud-based Home Assistant integrations ([guix77](https://github.com/guix77/homeassistant-aldes), [Fredzxda](https://github.com/Fredzxda/homeassistant-aldes), [hugz](https://github.com/hugz/homeassistant-aldes), [tiliarou](https://github.com/tiliarou/homeassistant-aldes)) depend on the same cloud, and have the same problems.
- The box's Wi-Fi module (HF-LPB100) has no usable local API, and the bus between the box and the unit (iBus) is proprietary.

**What this project does instead**: the Connect Box talks MQTT over TLS to Azure IoT Hub. A local DNS rewrite sends that traffic to a fake IoT Hub run by this project. The box thinks it is still talking to Azure; we get the telemetry and can send commands, with no hardware modification and no Internet access needed.

## How it works

```
Connect Box ──(MQTT/TLS, port 8883)──> [local DNS rewrite] ──> [aldes-mbr] ──(MQTT)──> [Mosquitto] ──> Home Assistant
     ▲                                                            │
     └──────────────────────────(commands)─────────────────────────┘
```

1. Your **local DNS** (Pi-hole, AdGuard Home, Unbound on OPNsense…) answers `aldesiotsuite.azure-devices.net` with the address of your Docker host.
2. **aldes-mbr** accepts the box's TLS connection with a self-signed certificate. The box does not check the certificate; it only needs an old cipher suite, which the bridge offers (`SECLEVEL=0`).
3. The box's telemetry (one frame every 10 minutes, plus one a few seconds after each command) is **republished to your Mosquitto**, retained, and merged with the previous frames.
4. **Home Assistant MQTT discovery** creates an "Aldes VMC" device with its sensors, a *Débit de ventilation* select and buttons.
5. **Commands** published on `aldes/vmc/cmd/macro/<name>` are translated into the Aldes JSON-RPC payload and sent to the box, which acknowledges them. The bridge then checks in the next frame that the mode really changed.

## Quick start

### Prerequisites

- A Linux Docker host on your LAN (NAS, Raspberry Pi, server…), reachable by the box on **port 8883**.
- A local DNS server where you can add a host override (Pi-hole v6, AdGuard Home, Unbound/OPNsense, dnsmasq…), used by the box.
- A Mosquitto broker, and Home Assistant with the MQTT integration connected to it.
- A Connect Box already paired with your Wi-Fi (use [`tools/aldes_pair.py`](tools/aldes_pair.py) if the app cannot pair it).
- The box's **MAC address** (12 hex digits; from your DHCP leases or `arp -a`). Its client id is that MAC followed by `_AIR`.
- **The time, for the box**: forward its HTTPS port (443) to the bridge, or let it reach its NTP server. Without it, holidays and the weekly schedule do not work: see [Box clock](#box-clock).

### Setup

1. **Clone the repository on your Docker host**
   ```bash
   git clone https://github.com/StraySnake/aldes-mbr.git
   cd aldes-mbr
   cp docker-compose.yml.example docker-compose.yml
   ```

2. **Edit `docker-compose.yml`**: set `DEVICE_MAC` and `UPSTREAM_MQTT_HOST` (plus `UPSTREAM_MQTT_USER` / `UPSTREAM_MQTT_PASS` if Mosquitto needs them). Recommended: reserve the box's IP in your DHCP server and set `DEVICE_IP` to it (see [Security](#security)). All settings: [Configuration](#configuration).

3. **Redirect the box to the bridge with your DNS.** Two names:

   | Name | Answer | Why |
   |---|---|---|
   | `aldesiotsuite.azure-devices.net` | IP of the Docker host | sends the box's MQTT (and HTTPS) traffic to aldes-mbr |
   | `aldes.blob.core.windows.net` | IP of the Docker host (with `HTTPS_PORT`), else `0.0.0.0` | the box checks its firmware version here; the bridge answers so that no Aldes update can break it (see [Box clock](#box-clock) and [Firmware updates](#firmware-updates)). `0.0.0.0` also blocks updates but gives no time |

   - **Pi-hole v6**: `/etc/pihole/pihole.toml`, then `pihole reloaddns`:
     ```toml
     [dns]
     hosts = [
       "<docker-host-IP> aldesiotsuite.azure-devices.net",
       "<docker-host-IP> aldes.blob.core.windows.net"
     ]
     ```
   - **OPNsense (Unbound)**: *Services → Unbound DNS → Overrides*, add a host override for each name.
   - **AdGuard Home**: *Filters → DNS rewrites*.

   The box must use this DNS server (the one given by your DHCP server).

4. **Start the bridge**
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
   At startup it logs `fake Azure IoT Hub listening on 0.0.0.0:8883`, then, once Mosquitto is connected, `HA discovery: 25 sensors + 3 binary sensors + 1 select + 9 buttons`.

5. **Wait for the box to reconnect** (a few minutes, or power-cycle it). The logs show:
   ```
   NEW CONNECTION from ('<box-IP>', xxxxx)
   ('<box-IP>', xxxxx) TLS TLSv1.2 AES256-SHA256
   ('<box-IP>', xxxxx) CONNECT proto=MQTT level=4 keepalive=180
   ('<box-IP>', xxxxx)   client_id = XXXXXXXXXXXX_AIR
   ('<box-IP>', xxxxx)   box clock: set (token expiry ...)
   ('<box-IP>', xxxxx) SUBSCRIBE pkt_id=...
   ```
   followed by the first telemetry frame. If the `client_id` is not `<DEVICE_MAC>_AIR`, the log says so: check `DEVICE_MAC`. If it says `box clock NOT SET`, see [Box clock](#box-clock).

6. **Open Home Assistant**: an "Aldes VMC" device appears (entities `sensor.aldes_vmc_*`, `select.aldes_vmc_mode`… with the default `DEVICE_NAME`). See [Data in Home Assistant](#data-in-home-assistant).

7. **Optional**: [`examples/homeassistant/dashboard.yaml`](examples/homeassistant/dashboard.yaml) (a Lovelace card) and [`examples/homeassistant/automations.yaml`](examples/homeassistant/automations.yaml) (a night/day cycle).

### Coming from djo1338/aldes-mqtt-bridge

- Stop the old container first, from its own folder: `docker compose down` (or `docker rm -f aldes-mqtt-bridge`). Both listen on port 8883.
- Copy your settings (`DEVICE_MAC`, `UPSTREAM_MQTT_*`, `DEVICE_NAME`) into the new `docker-compose.yml`. If you reuse your old file, replace its `image: ghcr.io/djo1338/...` line with `build: .`, otherwise the old code keeps running. An old service or container name (`aldes-mqtt-bridge`) still works.
- Home Assistant keeps the same entities. Entities of fields identified since (e.g. *Champ cve_csn (inconnu)*) are removed and replaced by named ones.

### Updating

```bash
git pull
docker compose up -d --build
```

The first lines of the log (`HA discovery: …`) show which version runs.

## Data in Home Assistant

Everything is published through MQTT discovery under one device, "Aldes VMC" (`DEVICE_NAME`). The typical values come from a Dee Fly Cube, at about 23 °C inside and 10 °C outside.

### Measurements

| Entity | Telemetry field | Unit | Meaning, typical values |
|---|---|---|---|
| Air extérieur | `outside_tpt` | °C | outdoor air entering the unit |
| Air intérieur (extrait) | `ext_tpt` | °C | indoor air extracted from the house |
| Air rejeté | `reject_tpt` | °C | air blown outside after the exchanger: ≈ 15 °C when outside is 10 °C; ≈ 19 °C when the bypass is open |
| Débit extraction / Débit insufflation | `extf_flw` / `ffe_flw` | m³/h | **flow setpoint of the current profile**, not a measurement: e.g. 105 (Quotidien), 195 (Invités), 210 (Boost). It follows a mode change about a minute later |
| Vitesse extraction / Vitesse insufflation | `extf_spd` / `vi_spd` | rpm | fan speeds: ~2300 / ~1600 rpm at 105 m³/h, ~2900 / ~2800 rpm at 210 m³/h |
| Commande ventilateur extraction / insufflation | `cve_csn` / `vi_csn` | % | fan commands: ~30 / ~15 % at 105 m³/h, ~97 / ~91 % at 210 m³/h (deduced from the data, see [PROTOCOL.md](docs/PROTOCOL.md#telemetry-fields-from-13-h-of-frames-of-one-dee-fly-cube)) |
| Puissance échangeur | `echange_pwr` | W | heat recovered by the exchanger: ~450 W at 105 m³/h, ~880 W at 210 m³/h; **0 when the bypass is open** |
| Vitesse demandée | `set_spd` | — | speed step of the profile: 0 Vacances, 1 Quotidien, 2 Invités, 3 Boost |
| CO₂, PM2.5, COV | `Co2_vmc`, `Pm25_Vmc`, `Voc_Vmc` | ppm, µg/m³, — | only with the optional air quality probes (0 otherwise); **disabled by default** |

Sensors become *unavailable* when the box disconnects, or after 30 min without telemetry (`SENSOR_EXPIRE_AFTER`).

### Control

| Entity | Type | Effect |
|---|---|---|
| Débit de ventilation | select | Quotidien, Invités, Boost, Vacances (1 day), Programme; shows the profile reported by the box (`current_mode`). Entity ID `select.aldes_vmc_mode` |
| Quotidien, Invités, Boost, Programme | buttons | switch profile |
| Vacances 1 jour / 7 jours | buttons | holidays from now for 1 / 7 days (needs the box's clock) |
| Annuler vacances | button | ends holidays by restoring the profile used before them |
| Ignorer le programme / Suivre le programme | buttons | `changeCMO 1` / `0`: stay on the current profile, or follow the weekly schedule again. The box does not report this state |

Any other [macro](#macros) can be sent by publishing on `aldes/vmc/cmd/macro/<name>`. The **comfort mode** (favour heat / cool / auto) cannot be changed through the box: see [below](#mode-de-confort-comfort-mode).

### Status and diagnostics

| Entity | Meaning |
|---|---|
| Dernière commande | outcome of the last command (see [Command confirmation](#command-confirmation)); attributes: command, expected and reported mode |
| Dernière mise à jour | time of the last frame from the box; stays available when the box is offline, for "VMC silent for 1 h" alerts |
| Début / Fin des vacances | current holiday range, published by the bridge (the box does not report it); empty outside holidays |
| Horloge de la box | *OK* / *Problème*: whether the box's clock is set (NTP), checked at each connection |
| Filtre (durée de vie restante) | `ff_cpt`, filter life left in %: 0 when the remote asks to change the filter; should go back to 100 after a filter change and a reset of the filter timer |
| Filtre à changer | *Problème* when the filter life is 0, like the remote's alert: a good trigger for a notification |
| By-pass ouvert | *Ouvert* when the exchanger recovers nothing while air flows (`echange_pwr` = 0); unknown when inside and outside differ by less than 2 °C. It is the **state** of the bypass, decided by the unit from its comfort mode |
| Firmware (date) / Version firmware | `Vers_W` (Wi-Fi module build date, e.g. 2018-12-14) / `Vers_UC`, the box's firmware version (e.g. 26; see [Firmware updates](#firmware-updates)) |
| Signal WiFi | `RSSI`; missing from the frames of some boxes, **disabled by default** |
| Champ dep_ind / dtb_ind / tte_csn (inconnu) | fields not identified yet, **disabled by default**: `dep_ind` always 0 so far; `dtb_ind` 0 until the comfort mode is changed on the remote, then 55–255; `tte_csn` a slow counter (2790, then 2791 a day later). Enable them to help identify them |

### MQTT topics (for other home automation systems)

With the default prefix `aldes/vmc`:

| Topic | Content |
|---|---|
| `aldes/vmc/raw/devices/<DEVICE_ID>/messages/events/` | latest telemetry, JSON, retained; each frame merged with the previous ones |
| `aldes/vmc/status` | `online` / `offline` (box connected to the bridge), retained, with LWT |
| `aldes/vmc/last_seen` | ISO 8601 time of the last frame, retained |
| `aldes/vmc/command_result` | JSON outcome of the last command, retained |
| `aldes/vmc/holiday` | JSON `{"start", "end"}` of the current holidays, `{}` otherwise, retained |
| `aldes/vmc/box_clock` | JSON `{"synced", "token_expiry", "checked_at"}`, retained |
| `aldes/vmc/cmd/macro/<name>` | send a [macro](#macros) (publish with `retain=false`) |

## Débit de ventilation (ventilation profiles)

The *débit de ventilation* is the profile reported in `current_mode` and set with `changeMode`:

| Code | Name | `set_spd` | Flow observed on one Dee Fly Cube |
|---|---|---|---|
| W | Vacances | 0 | 105 m³/h (same as Quotidien on this installation) |
| V | Quotidien | 1 | 105 m³/h |
| Y | Invités | 2 | 195 m³/h, for about 40 min, then back to 105 m³/h while `current_mode` stays `Y` |
| X | Boost | 3 | 210 m³/h |
| Z | Programme | varies | follows the weekly schedule; `current_mode` stays `Z` |

- The flows are **configured per installation** by the installer: the original project measured about 50 / 165 / 250 / 330 m³/h on another unit. Rely on *Vitesse demandée* (`set_spd`) and *Dernière commande* to check a change.
- Names differ between Aldes documents: the Modbus document of the InspirAir Home calls speed 2 « Pointe cuisine » and speed 3 « Invités ». This project keeps the names of the original one; the speed step is what matters.
- `changeMode "A"` (an "Auto" mode of other Aldes products) is acknowledged by a Dee Fly Cube but ignored.

## Mode de confort (comfort mode)

The Aldes manual describes a second setting, **independent** of the profile:

| | **Débit de ventilation** (`current_mode`) | **Mode de confort** |
|---|---|---|
| What it sets | the flow profile: Vacances, Quotidien, Invités, Boost, Programme | whether the unit favours heat (keeps the exchanger), favours cool (opens the bypass to bring in cooler outside air) or decides by itself (Auto) |
| Where it is set | the Connect Box (`changeMode`), so Home Assistant, or the remote | **the unit's remote only** |
| What the box reports | `current_mode`, `set_spd` | nothing directly, only its effects |
| In Home Assistant | *Débit de ventilation* select, buttons | *By-pass ouvert* (the state of the bypass, not the setting) |

What was observed on a Dee Fly Cube in November (23 °C inside, 11 °C outside):

- **Favour cool**: bypass open at once (`echange_pwr` 0 W, air rejected at ~19 °C), and flow forced to 210 m³/h whatever the profile; `current_mode` unchanged.
- **Favour heat**: bypass closed at once, flow back to the profile's.
- **Auto**: the unit chooses by itself from the indoor and outdoor temperatures. In winter, it behaves like "favour heat".

**Changing it from Home Assistant is not possible yet.** The Aldes app (version 4.21) has no command for it, and the box only accepts the app's commands. Research in progress ([details](docs/PROTOCOL.md#bypass-and-comfort-mode-what-the-telemetry-shows)):

- The Aldes Modbus document for the InspirAir Home has a register for it (259, *Gestion Bypass Echangeur*: 1 Auto, 2 winter optimisation, 3 summer optimisation), but the Dee Fly Cube board has no Modbus terminal.
- The Dee Fly Cube remote is wireless, with a TI CC110L radio chip (two-way). The next step is to capture the radio commands it sends, to reproduce them with an ESP32 and a CC1101 module.

## Macros

Published on `aldes/vmc/cmd/macro/<name>`, translated by the bridge into the Aldes JSON-RPC payload:

| Macro | Effect |
|---|---|
| `quotidien` / `v` | profile V |
| `invites` / `y` | profile Y |
| `boost` / `x` | profile X |
| `vacances` / `vacances<N>j` (e.g. `vacances1j`, `vacances21j`) | profile W from now − 1 min to now + N days (1–365) |
| `vacances_stop` | ends holidays: back to the profile used before them (Quotidien if unknown) |
| `programme` / `z` | profile Z (weekly schedule) |
| `override_on` / `override_off` | `changeCMO 1` / `changeCMO 0` |

- Holidays and the weekly schedule rely on the box's clock: while it is not set, holiday commands are refused by the bridge (the box would ignore them).
- The Aldes app cancels holidays with a special date range that a real box ignores; `vacances_stop` switches back to the previous profile instead, which works.
- Publish commands with `retain=false`: retained messages on `aldes/vmc/cmd/#` are ignored, otherwise they would be replayed to the box at every reconnection.

### Command confirmation

Commands are sent with MQTT QoS 1, so the box acknowledges each one. For profile changes, the bridge then checks the frame the box sends about 5 s later: if it still reports the old mode, the command is sent once more, then reported as failed. The outcome is published on `aldes/vmc/command_result` and shown by *Dernière commande*:

| State | Meaning |
|---|---|
| En attente | sent, no acknowledgement yet |
| Reçue par la box | acknowledged by the box (PUBACK) |
| Confirmée | the telemetry reports the expected mode |
| Renvoyée | the mode did not change: sent once more |
| Échec | still not changed after the resend |
| Non confirmée | no telemetry within `CMD_CONFIRM_TIMEOUT` (15 min) |
| Envoyée | sent, but not verifiable from the telemetry (override) |
| Box déconnectée | the box was not connected: nothing sent |
| Refusée : horloge de la box non réglée | holiday command not sent, the box's clock is not set |

`CMD_QOS=0` restores the old behaviour, without acknowledgement. Avoid it: the firmware reads a packet id in every message, so with QoS 0 it drops the first 2 bytes of each command.

## Box clock

The Connect Box does not keep the time across reboots: **at every boot its clock restarts at 2016-11-12 00:00 UTC**. Until it gets the time:

- **holidays never start**: a range dated today is years in the box's future. The bridge refuses to send them (*Refusée : horloge de la box non réglée*);
- **the weekly schedule (Programme) runs at wrong days and hours**.

It has two time sources:

| | **HTTPS (recommended)** | **NTP** |
|---|---|---|
| How | the box sends HTTPS requests to port 443 of `aldesiotsuite.azure-devices.net`, i.e. of the bridge's host; it sets its clock from the `Date` header of the answers | queries a **hard-coded address, `61.164.36.105`, UDP 123** (no DNS lookup: a local NTP server announced by DHCP is ignored) |
| Clock obtained | **UTC** | **UTC+8** (China time): holidays end 8 h early |
| Needs | `HTTPS_PORT` and a port forward on the router | a NAT rule to a local NTP server, or Internet access to that address |
| Observed | sets the clock within a minute or two of every boot | after a power cut, the box stopped sending NTP requests altogether (30 h without time) |

### Fix: HTTPS (recommended)

1. Set `HTTPS_PORT: "8443"` in `docker-compose.yml` and restart the bridge (the log says `HTTPS listening on 0.0.0.0:8443`). The bridge runs as an unprivileged user, so it cannot listen on 443 itself.
2. On the router, add a port forward: from the box's IP, TCP, destination the Docker host, port 443 → the Docker host, port **8443**.

   On **OPNsense**: *Firewall → NAT → Port Forward*: interface = the box's network, protocol **TCP**, source = box IP, destination = the Docker host's IP, port 443 (HTTPS), redirect target = the Docker host's IP, port 8443, filter rule association = *Add associated filter rule*.
3. Power-cycle the box. Within a minute or two the log shows `https: GET /devices/…/messages/devicebound…` then `answered 204 No Content`, and the box reconnects to MQTT on its own with `box clock: set`.

The bridge answers the box's request like the real Azure IoT Hub with no command waiting (`204 No Content`), with the current time in the `Date` header, and nothing else.

### Alternative: NTP

On your router, choose one:

1. **Redirect it to a local NTP server.** Add a destination NAT (port forward) rule: from the box's IP, UDP, destination `61.164.36.105` port 123 → your router's (or NTP server's) address, port 123. On **OPNsense**: *Services → Network Time → General*, listen on the box's interface; then *Firewall → NAT → Port Forward*: interface = the box's network, protocol UDP, source = box IP, destination = `61.164.36.105`, port 123, redirect target = OPNsense's address on that network, port 123, filter rule association = *Add associated filter rule*.
2. **Allow only that traffic out**: a *pass* rule for UDP 123 from the box to `61.164.36.105`, before any rule that blocks its Internet access. It depends on a server abroad that may stop answering.

The box then runs at UTC+8. Your firewall log shows whether its NTP requests are blocked: UDP packets from the box's IP to `61.164.36.105:123`.

### Check

The bridge checks the clock at every connection of the box, from the expiry date the box writes into its credentials:

- the log says `box clock: set (token expiry …)` or `box clock NOT SET (…)`;
- the *Horloge de la box* entity shows *OK* or *Problème*.

The box's clock is only visible when it connects, and right after a boot (a power cut, for example) it connects before it has the time. With HTTPS it reconnects by itself about a minute later; otherwise: while the clock is not set, the bridge closes the connection after 10 minutes: the box reconnects within seconds with a fresh token, and the clock is checked again. If it is still not set, the next check comes after 20, then 40 minutes, then every hour. Home Assistant does not see these short disconnections (the box is reported offline only if it does not come back within 15 minutes). `CLOCK_RECHECK=0` disables this.

The box goes back to 2016 after every power cut and sets its clock again from its time source, so keep the port forward in place. A useful automation: notify when *Horloge de la box* stays on *Problème* for more than an hour.

## Firmware updates

At every boot, before anything else, the box fetches `https://aldes.blob.core.windows.net/firmware/VersionAldesConnectBoxIOTHUB.txt` and compares the version there (`V=53` in January 2026) to its own (`Vers_UC`, e.g. 26). If the server's is higher, it downloads and flashes the new firmware.

**Keeping the box on its current firmware is recommended.** A newer firmware could verify the TLS certificate, change the hostname, or otherwise stop working with the bridge, and a Wi-Fi-module flash cannot be rolled back. By pointing `aldes.blob.core.windows.net` at the bridge (or `0.0.0.0`), the version check gets a `404`, so the box never updates. Pointing it at the bridge is better than `0.0.0.0`: that first request then also sets the box's clock (its `Date` header), before the MQTT connection.

### The firmware store (for the curious)

Aldes hosts the firmware in a **public, listable** Azure Blob container. Nothing here is secret, but none of it is needed to run the bridge — it is documented for people reverse engineering the box. Reading these files does not touch your box.

- **Base URL:** `https://aldes.blob.core.windows.net/firmware/`
- **Version file** (what the box reads at boot): [`VersionAldesConnectBoxIOTHUB.txt`](https://aldes.blob.core.windows.net/firmware/VersionAldesConnectBoxIOTHUB.txt) — a 4-byte text file, `V=53` in Jan 2026.
- **Current image:** `ModemWifi00M1aAppIOTHUB.binc` (identical to the latest `…_v53.binc`).

**List every file, with dates and sizes:**

```bash
curl -s "https://aldes.blob.core.windows.net/firmware?restype=container&comp=list"
```

That returns XML listing the version file, the current image, every past app image (`ModemWifi00M1aAppIOTHUB_v25.binc` … `_v53.binc`, and an `old/` folder going back further), and `LPB100_v2x.bin` — the Wi-Fi module's own base firmware, separate from the Aldes app.

**Download one file** (for offline analysis; nothing is flashed):

```bash
curl -O "https://aldes.blob.core.windows.net/firmware/ModemWifi00M1aAppIOTHUB_v53.binc"
```

**File format** (`.binc`, Aldes-specific):

| Bytes | Meaning |
|---|---|
| 0 | version, as hex digits (`0x53` = "53", `0x26` = "26") |
| 4–7 | payload length, little-endian |
| 8… | **encrypted** body (≈ 7.998 bits/byte of entropy; 16-byte blocks, no chaining) |

The body is encrypted with a key that is **not in the files** and that changed between v26 and v47. No public key or tool to decrypt it was found; Hi-Flying's own module firmware (`LPB100_*.bin`) is plain, so the `.binc` container is Aldes' own. The bridge reads and serves none of it, so this project does not modify the box's firmware.

Your box's version is the **`Vers_UC`** value in the telemetry (`Version firmware` sensor): `26` means the firmware dated 2018-12-14 (`Vers_W`).

## Security

The box cannot be authenticated: its credentials are signed with an Aldes key we do not have. What the bridge does:

- The first packet of a connection must be `CONNECT`, and only messages from the session whose client id is the box's, on its own topics, are relayed to Mosquitto. Other clients are logged but cannot inject values into Home Assistant.
- **`DEVICE_IP` (optional, recommended)**: connections from any other IP are closed at once. Without it, a device on your LAN that knows the box's MAC can connect with its client id, take over the session and receive your commands instead of the box. Reserve the box's IP in your DHCP server first, otherwise the bridge refuses the box when its IP changes (`REFUSED` in the logs). A firewall rule restricting port 8883 to the box's IP gives the same protection.
- Commands only come from your Mosquitto: protect it with a username and password, otherwise any device on your LAN can control the unit.
- Once the bridge is set up, the box needs no Internet access: it gets the time from the bridge (HTTPS). Isolating it (its own VLAN, outbound traffic blocked) is a good idea.

## Configuration

Environment variables of the container (see [`docker-compose.yml.example`](docker-compose.yml.example)):

| Variable | Default | Meaning |
|---|---|---|
| `DEVICE_MAC` | **required** | MAC address of the box (`:` or `-` accepted) |
| `DEVICE_IP` | — | only accept connections from this IP (comma-separated list allowed) |
| `UPSTREAM_MQTT_HOST` / `_PORT` | `127.0.0.1` / `1883` | your Mosquitto |
| `UPSTREAM_MQTT_USER` / `_PASS` | — | Mosquitto credentials |
| `UPSTREAM_MQTT_TLS` | `0` | `1` = connect to Mosquitto over TLS |
| `UPSTREAM_MQTT_CA` | system CAs | CA file to verify Mosquitto's certificate |
| `UPSTREAM_MQTT_TLS_INSECURE` | `0` | `1` = do not verify Mosquitto's certificate |
| `DEVICE_NAME` / `DEVICE_MODEL` | `Aldes VMC` / `Dee Fly Cube` | name and model of the device in Home Assistant |
| `BRIDGE_TOPIC_PREFIX` | `aldes/vmc` | prefix of the MQTT topics |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant discovery prefix |
| `SENSOR_EXPIRE_AFTER` | `1800` | seconds without a frame before the sensors become unavailable (0 = never) |
| `CMD_QOS` | `1` | QoS of the commands sent to the box |
| `CMD_CONFIRM_TIMEOUT` | `900` | seconds to wait for a frame confirming a profile change |
| `LOG_LEVEL` | `info` | `info`: connections, commands, first frame of each connection; `debug`: every frame with its payload; `warning`: problems only |
| `LOG_FILE` | `/logs/mitm.log` | log file, rotated at `LOG_MAX_BYTES` (5 MB, 0 = never), `LOG_BACKUPS` (3) files kept |
| `TLS_CIPHERS` | `ALL:@SECLEVEL=0` | cipher suites offered to the box; the negotiated one is logged at each connection |
| `CLOCK_RECHECK` | `600` | while the box's clock is not set, seconds before the bridge closes the connection to check it again (doubled at each failed check, up to 1 h; 0 = never) |
| `CLOCK_RECHECK_GRACE` | `900` | seconds the box may take to reconnect after such a check before Home Assistant shows it offline |
| `HTTPS_PORT` | `0` (off) | answer the box's HTTPS requests on this port (with a 443 → this port forward on the router): gives the box the time (see [Box clock](#box-clock)). Old name: `HTTPS_PROBE_PORT` |
| `CERT_DIR` | `/data` | where the self-signed certificate is stored |

Invalid values (e.g. `LOG_MAX_BYTES=5M`) stop the bridge at startup with an explicit message.

## Operations

- **Docker image**: the bridge runs as the unprivileged user `aldes` (uid 1000). The entrypoint starts as root only to take ownership of `/data` and `/logs`, so a `data/` folder created by an older image keeps working. If that is refused (e.g. on a network share), it runs as root and logs a warning. You can also set `user:` in `docker-compose.yml`; the volumes must then be writable by that user.
- **Health check**: the container is *healthy* while the bridge runs and Mosquitto is connected (`docker ps` shows it). An offline box does not make it unhealthy: Home Assistant's availability shows that.
- **Network**: the example uses `network_mode: host`, so that the bridge sees the box's real IP (needed for `DEVICE_IP`).

### Troubleshooting

| Symptom | Cause, fix |
|---|---|
| No `NEW CONNECTION` in the logs | the box does not use your DNS rewrite, or cannot reach port 8883 of the host: check the DNS answer for `aldesiotsuite.azure-devices.net` and the firewall; power-cycle the box |
| `REFUSED: not in DEVICE_IP` | the box's IP changed: reserve it in your DHCP server and update `DEVICE_IP` |
| `WARNING: client_id != DEVICE_ID` | wrong `DEVICE_MAC` |
| `box clock NOT SET` | normal for a minute or two after the box boots (it is checked again automatically); if it persists, the box has no time source: see [Box clock](#box-clock) |
| `Address already in use` at startup | another container (the old aldes-mqtt-bridge?) already listens on port 8883 |
| *Dernière commande*: *Échec* | the box acknowledged but did not change mode; for holidays, check the clock |
| Entities *unavailable* | box disconnected, or no frame for 30 min: check `docker compose logs` |

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

The end-to-end tests start the bridge as a subprocess against [amqtt](https://github.com/Yakifo/amqtt) (a pure-Python MQTT broker standing in for Mosquitto) and fake Connect Boxes speaking MQTT over TLS, including the firmware's quirks. They run on Linux (one test connects from `127.0.0.2`). The CI runs them on every pull request, along with a Docker build and startup check.

## Changes from aldes-mqtt-bridge

aldes-mbr started from [djo1338/aldes-mqtt-bridge](https://github.com/djo1338/aldes-mqtt-bridge) (a single `fake_iothub.py`) and builds on it. The base idea is unchanged; what follows is what this fork adds and fixes.

### Command reliability

- **Commands are sent with QoS 1 and a real packet id.** The firmware reads a packet id in *every* PUBLISH it receives, even QoS 0 ones: with QoS 0 (the original behaviour) it takes the first 2 bytes of the payload as the id and parses a corrupted command. QoS 1 fixes that, and the box's PUBACK becomes a real receipt (*Reçue par la box*).
- **Command confirmation.** For a mode change, the bridge checks the next telemetry frame, resends once if the box did not switch, then reports failure. The outcome is a sensor (*Dernière commande*): *En attente, Reçue par la box, Confirmée, Renvoyée, Échec, …*
- **Holiday cancel that works.** The app's cancel (an epoch date range) is ignored by a real Dee Fly Cube; `vacances_stop` switches back to the previous profile instead.
- **TLS I/O is confined to each connection's own thread** (commands are queued to it), which avoids using one OpenSSL socket from two threads at once.

### Time for the box (new)

- The box's clock restarts in 2016 at every boot. The bridge **detects** whether it is set (from the SAS token) and exposes it (*Horloge de la box*); it **refuses holidays** while it is unset, instead of letting them fail silently.
- It **re-checks** an unset clock by forcing a short reconnection, so the box does not stay stuck for days.
- It can **give the box the time over HTTPS** (`HTTPS_PORT`): UTC, and with no NTP at all. See [Box clock](#box-clock).

### Home Assistant

- New entities: *Dernière commande*, *Dernière mise à jour*, *Horloge de la box*, *By-pass ouvert*, *Filtre à changer*, *Filtre (durée de vie restante)*, *Début / Fin des vacances*, fan-command sensors.
- **Online/offline availability** (MQTT LWT) and an *unavailable after N minutes without a frame* timeout, so a disconnected box shows as unavailable rather than frozen.
- The *Débit de ventilation* select is kept separate from the comfort mode, which the box cannot set (documented, with the research behind it).
- Several raw fields are now named and typed; unknown ones are disabled by default.

### Robustness

- **Telemetry merge:** the short boot heartbeat no longer blanks every sensor; each frame is merged into the last values, seeded from Mosquitto's retained copy after a restart.
- **Truncated/again-mode frames are ignored** (`…,+++}`, `AT+ENTM`) instead of replacing the telemetry.
- A second CONNECT, a stale half-open session and oversized packets are handled.

### Security

- **`DEVICE_IP`**: only the box's IP is accepted.
- Only the box's own session, on its own topics, is relayed to Mosquitto — another LAN device cannot inject values into Home Assistant.

### Operations

- The container **runs as an unprivileged user** (the original ran as root), with a Docker **healthcheck**.
- **Log levels** (`info`/`debug`/`warning`) and **log rotation**; clear startup errors on invalid settings.
- **Firmware:** the box's version check is answered so no Aldes update can silently break the bridge, and that answer also carries the time. See [Firmware updates](#firmware-updates).

### Code and documentation

- Split from one file into the `aldes_bridge` package (see [ARCHITECTURE.md](docs/ARCHITECTURE.md)).
- A **test suite** (end-to-end against a real broker and fake boxes, plus unit tests) and **CI** on every pull request.
- The mode flows are the ones measured here (105 / 195 / 210 m³/h, Invités time-limited), noted as installer-configured; the unverified "X/Y inverted" claim is dropped. [PROTOCOL.md](docs/PROTOCOL.md) is expanded (time sources, firmware, bypass/comfort research).

## Documentation

- [Architecture](docs/ARCHITECTURE.md): modules and data flow
- [Protocol](docs/PROTOCOL.md): telemetry fields, JSON-RPC commands, observed behaviour of the box, comfort mode research
- [Reverse engineering log](docs/REVERSE-ENGINEERING.md): how the original project found the protocol

## Disclaimer

This project performs **reverse engineering for interoperability** of the Aldes Connect Box. In the EU, this is permitted under [Directive 2009/24/EC, Article 6](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32009L0024). It does not redistribute Aldes' app code or manuals, only what is needed to communicate with a device you own.

Use at your own risk. The authors are not affiliated with Aldes. Aldes may change their firmware or backend at any time and break this project.

## Credits

- **[djo1338/aldes-mqtt-bridge](https://github.com/djo1338/aldes-mqtt-bridge)**: the original project this fork is based on (fake Azure IoT Hub broker, protocol reverse engineering, Home Assistant integration)
- **[zongyl/smartLink](https://github.com/zongyl/smartLink)**: Hi-Flying SmartLink V3 Java reference (used for `tools/aldes_pair.py`)
- **[The-Smartest-Home/hass_inspirair](https://github.com/The-Smartest-Home/hass_inspirair)**: InspirAir Modbus register map
- **[skylot/jadx](https://github.com/skylot/jadx)**: APK decompiler used by the original project to map the JSON-RPC commands
- **[guix77/homeassistant-aldes](https://github.com/guix77/homeassistant-aldes)** and forks: cloud-based integrations that revealed the API URLs

## License

[MIT](LICENSE). Do whatever you want, just don't blame us if Aldes pushes a firmware update.
