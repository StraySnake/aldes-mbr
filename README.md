# aldes-mqtt-bridge

> Local control of Aldes Connect ventilation units (VMC / HRV) via Home Assistant — bypassing the unreliable Aldes cloud.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/ghcr.io-aldes--mbr-blue?logo=docker)](https://github.com/StraySnake/aldes-mbr/pkgs/container/aldes-mbr)

If you own an **Aldes Dee Fly Cube** (or possibly InspirAir Home / T.Flow / T.One — same Azure backend) with a Connect Box, you have probably noticed:

- The Aldes Connect app can't pilot reliably (5xx errors on auth, MQTT drops to the device)
- The cloud-based Home Assistant integrations ([guix77](https://github.com/guix77/homeassistant-aldes), [Fredzxda](https://github.com/Fredzxda/homeassistant-aldes), [hugz](https://github.com/hugz/homeassistant-aldes), [tiliarou](https://github.com/tiliarou/homeassistant-aldes)) inherit the same problems — all of them depend on the cloud
- The HF-LPB100 module's local TCP server is locked by Aldes firmware
- The Aldes BUS between the Connect Box and the VMC is proprietary

**This project takes a different approach**: we redirect the Connect Box's MQTT-over-TLS traffic from Azure IoT Hub to a fake broker we run locally. The box thinks it's still talking to Azure. We get full bidirectional access — telemetry in, commands out — with no hardware modifications.

## How it works

```
Connect Box ──(MQTT/TLS)──> [Pi-hole DNS rewrite] ──> [this fake broker] ──> [Mosquitto] ──> Home Assistant
                                                            │
                                                            └──(commands)──> Connect Box
```

1. **Pi-hole** (or any local DNS) rewrites `aldesiotsuite.azure-devices.net` → your Docker host
2. The **fake broker** accepts the box's TLS connection with a self-signed cert (the HF-LPB100 firmware doesn't pin certs, only checks if the cipher suite matches — we serve legacy ciphers via `SECLEVEL=0`)
3. The MQTT-CONNECT, SUBSCRIBE and PUBLISH frames are decoded, and PUBLISH telemetry is **republished to your Mosquitto** under `aldes/vmc/raw/...` with `retain=true`. Each frame is merged with the previous ones, so the short boot heartbeat the box sends after connecting does not turn the sensors to *unknown*
4. **Home Assistant MQTT discovery** is auto-published — sensors and a Mode select appear automatically under an "Aldes VMC" device (named after `DEVICE_NAME`)
5. **Commands** are injected back into the box by publishing on `aldes/vmc/cmd/macro/<name>` — the broker translates the macro to the real Aldes JSON-RPC payload and forwards it on the box's `devicebound` topic

## Quick start

### Prerequisites

- A Linux Docker host on your LAN (Synology NAS, Raspberry Pi, anything)
- Pi-hole, Adguard Home, dnsmasq, or a router that supports DNS rewrites
- A Mosquitto MQTT broker reachable from the Docker host
- Home Assistant with the MQTT integration configured against the same Mosquitto
- An Aldes Connect Box already paired on your WiFi (use [`tools/aldes_pair.py`](tools/aldes_pair.py) if you can't pair via the app)
- The Connect Box's **MAC address** (12 hex chars, prefix usually `28:9C:6E`)
- **Time for the box**: it must reach its NTP server, or a local one through a redirection (see [Box clock (NTP)](#box-clock-ntp)). Without it, holidays and the weekly schedule do not work.

### Setup

1. **Clone this repo on your Docker host**
   ```bash
   git clone https://github.com/StraySnake/aldes-mbr.git
   cd aldes-mbr
   cp docker-compose.yml.example docker-compose.yml
   ```

   **Coming from djo1338/aldes-mqtt-bridge?** Stop the old container first, from its own folder: `docker compose down` (or `docker rm -f aldes-mqtt-bridge`). Both use port 8883 and the same container name. Copy your settings (`DEVICE_MAC`, `UPSTREAM_MQTT_*`, `DEVICE_NAME`) into the new `docker-compose.yml`, and keep `build: .`: the old `image: ghcr.io/djo1338/...` line would run the old code. Home Assistant keeps the same entities.

2. **Edit `docker-compose.yml`**: set `DEVICE_MAC` and `UPSTREAM_MQTT_HOST` (your Mosquitto IP). Recommended: reserve the box's IP in your DHCP server and set `DEVICE_IP` to it (see [Security](#security)).

3. **Configure DNS rewrites** in Pi-hole (`/etc/pihole/pihole.toml`, key `dns.hosts`, requires Pi-hole v6+):
   ```toml
   [dns]
   hosts = [
     "<docker-host-IP> aldesiotsuite.azure-devices.net",
     "0.0.0.0 aldes.blob.core.windows.net"
   ]
   ```
   Then `pihole reloaddns`.

   The first line redirects the MQTT/IoT Hub traffic to our broker. The second line **blocks Aldes firmware updates** by sinkholing the Azure Blob endpoint where the OTA blobs are hosted. This protects you from a future firmware push that could add cert pinning, change the hostname, or otherwise break this bridge. To re-allow updates later, just remove the line.

4. **Start the broker**
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
   The first lines show the version you run: `HA discovery: 25 sensors + 1 binary sensor + 1 select + 9 buttons`.

5. **Wait for the box to reconnect** (a few minutes, or power-cycle it). You should see in the logs:
   ```
   NEW CONNECTION from ('<box-IP>', xxxxx)
   ('<box-IP>', xxxxx) TLS TLSv1.2 AES256-SHA256
   ('<box-IP>', xxxxx) CONNECT proto=MQTT level=4 keepalive=180
   ('<box-IP>', xxxxx)   client_id = XXXXXXXXXXXX_AIR
   ('<box-IP>', xxxxx) SUBSCRIBE pkt_id=...
   ('<box-IP>', xxxxx) PUBLISH qos=1 topic=devices/XXXXXXXXXXXX_AIR/messages/events/ (408b)
   ```
   If `client_id` is not `<DEVICE_MAC>_AIR`, the log says so: check `DEVICE_MAC`.

6. **Check the box's clock** (see [Box clock (NTP)](#box-clock-ntp)): the date of the `se=` value in the `password` line must be today's date, not 2016.

7. **Open Home Assistant** — an "Aldes VMC" device should appear automatically with all sensors and a Mode select (entity IDs `sensor.aldes_vmc_*`, `select.aldes_vmc_mode` with the default `DEVICE_NAME`). Entities show as *unavailable* while the box is not connected to the bridge (availability topic `aldes/vmc/status`), except **Dernière mise à jour** (`sensor.aldes_vmc_derniere_mise_a_jour`), the time of the last frame received from the box: it stays visible so you can alert on a silent VMC, e.g. "last update more than 1 h ago". Sensors also turn *unavailable* if no frame arrives for 30 min, i.e. 3 missed frames (`SENSOR_EXPIRE_AFTER`, in seconds, `0` to disable), instead of showing frozen values. Diagnostic entities: **Horloge de la box** (*Problème* while the box's clock is not set, see [Box clock (NTP)](#box-clock-ntp)), *Firmware WiFi* shown as its build date, and six **Champ … (inconnu)** sensors for telemetry fields not identified yet (`ff_cpt`, `dep_ind`, `dtb_ind`, `cve_csn`, `vi_csn`, `tte_csn`), disabled by default: enable them to help identify them. *CO₂*, *PM2.5*, *COV* (0 without the optional probes) and *Signal WiFi* are disabled by default too. Other entities: **Début / Fin des vacances** (published by the bridge when holidays start, since the box does not report them), **Dernière commande** (see [Command confirmation](#command-confirmation)) and the **Ignorer le programme / Suivre le programme** buttons (`changeCMO`).

8. **Optional**: copy [`examples/homeassistant/dashboard.yaml`](examples/homeassistant/dashboard.yaml) into your Lovelace, and [`examples/homeassistant/automations.yaml`](examples/homeassistant/automations.yaml) for a sample night/day cycle automation.

## Mode mapping (validated empirically on Dee Fly Cube)

| Aldes code | Friendly name | Speed | Flow observed |
|---|---|---|---|
| W | Vacances (Holiday) | V1 base | ~50 m³/h |
| V | Quotidien (Daily) | V2 normal | ~165 m³/h |
| Y | Invités (Guests) | V3 | ~250 m³/h |
| X | Boost | V4 max | ~330 m³/h |
| Z | Programme (Schedule) | varies | follows weekly plan |

⚠️ **Y/X are inverted compared to InspirAir documentation** — measured on a Dee Fly Cube. If you have a different Aldes model, run the broker and watch which mode produces what flow before trusting these labels.

The flows above come from one installation. On another one, the reported flows (`extf_flw`, `ffe_flw`) stayed at 105 m³/h in every mode while the speed setpoint (`set_spd`: 0 holidays, 1 daily, 2 guests) and the fan speeds did change: rely on *Vitesse demandée* and *Dernière commande* to check a mode change.

## Macros

The broker exposes high-level macros at `aldes/vmc/cmd/macro/<name>` that translate to the proper Aldes JSON-RPC payload server-side (e.g. computing the holiday date range automatically):

| Macro | Effect |
|---|---|
| `quotidien` / `v` | mode V (V2) |
| `invites` / `y` | mode Y (V3) |
| `boost` / `x` | mode X (V4) |
| `vacances` / `vacances<N>j` (e.g. `vacances1j`, `vacances21j`) | mode W with auto dates (now − 1 min → now + N days, 1–365) |
| `vacances_stop` | cancel holidays: back to the mode used before them (Quotidien if unknown) |
| `programme` / `z` | mode Z (schedule) |
| `override_on` / `override_off` | `changeCMO 1` / `changeCMO 0` |

Holidays and the weekly schedule rely on the box's clock: see [Box clock (NTP)](#box-clock-ntp). If the box cannot set its clock, holiday commands are acknowledged but never start.

The Aldes app cancels holidays with a special date range that a real box ignores; `vacances_stop` switches back to the previous mode instead, which works.

### Command confirmation

Commands are sent with MQTT QoS 1: the box acknowledges each one (PUBACK), shown as *Reçue par la box*. For mode changes, the bridge then checks the next telemetry frame: if the box still reports the old mode, the command is sent once more, then reported as failed. The outcome is published (retained) on `aldes/vmc/command_result` and shown by the **Dernière commande** sensor: *En attente*, *Reçue par la box*, *Confirmée*, *Renvoyée*, *Échec*, *Non confirmée* (no telemetry within `CMD_CONFIRM_TIMEOUT`, 15 min by default), *Envoyée* (not verifiable by the telemetry: override), *Box déconnectée*. The attributes give the command, the expected and the reported mode.

The box sends a telemetry frame about 5 s after each command, so a mode change is normally confirmed within seconds.

`CMD_QOS=0` restores the old behaviour (no acknowledgement). Avoid it: the firmware reads a packet id in every PUBLISH, so with QoS 0 it drops the first 2 bytes of each command.

Publish commands with `retain=false`: retained messages on `aldes/vmc/cmd/#` are ignored (they would otherwise be replayed to the box every time the bridge reconnects).

## Box clock (NTP)

The Connect Box does not keep the time across reboots: **at every boot its clock restarts at a fixed date in November 2016**, and the only way it sets the time is NTP, from a **hard-coded IP address: `61.164.36.105`, UDP port 123** (no DNS lookup, so a local NTP server announced by DHCP is ignored). It retries about every 24 s until it gets an answer.

If the box cannot reach that address (a firewall rule blocking its Internet access, for example), its clock stays in 2016:

- **holidays never start**: a holiday range dated today is years in the box's future, so the command is acknowledged (*Reçue par la box*) but the box stays in its current mode (*Échec* after one resend);
- **the weekly schedule (Programme) runs at wrong days and hours**.

### Fix

Choose one, on your router or firewall:

1. **Redirect it to a local NTP server (recommended, keeps the box off the Internet).** Add a destination NAT (port forward) rule: from the box's IP, UDP, destination `61.164.36.105` port 123 → your router's or NTP server's address, port 123. The box thinks it talks to its server; your local server answers.

   On **OPNsense**: *Services → Network Time → General*, listen on the box's interface; then *Firewall → NAT → Port Forward*, add: interface = the box's VLAN, protocol UDP, source = box IP, destination = `61.164.36.105` (or *any*), port 123, redirect target = OPNsense's address on that VLAN, port 123, filter rule association = *Add associated filter rule*.

2. **Allow only that traffic out**: a *pass* rule for UDP 123 from the box's IP to `61.164.36.105`, before any rule blocking its Internet access. Simpler, but it depends on a server abroad that may stop answering.

Your firewall log shows whether the box is blocked: look for UDP packets from the box's IP to `61.164.36.105:123`.

### Check

The box writes its own time into its credentials at each connection: the `se=` (expiry) value of the `password` line in the bridge's log. After fixing NTP, power-cycle the box and wait for it to reconnect, then:

```bash
docker compose logs | grep -o "se=[0-9]*" | tail -1
```

Convert the number (replace `<value>`):

```bash
date -u -d @<value>
```

- **2016**: the box has not set its clock yet (still blocked, or not reconnected since).
- **Today's date** (expiry ahead by some hours): the clock is set. Holidays now start within seconds (*Confirmée*, *Début / Fin des vacances* shown in HA).

The bridge does this check for you at every connection of the box: the **Horloge de la box** entity in Home Assistant shows *Problème* while the clock is not set, and the log says `box clock NOT SET`. Meanwhile, holiday commands are not sent (*Dernière commande*: *Refusée : horloge de la box non réglée*) since the box would ignore them; other modes work normally. The state is only updated when the box connects: after fixing NTP, wait for its next reconnection or power-cycle it.

The box goes back to 2016 after every power cut and sets its clock again as soon as NTP answers, so keep the rule in place. A useful automation: notify when *Horloge de la box* turns to *Problème*.

## Security

The box cannot be authenticated: its SAS token is signed with an Aldes key we do not have. What the broker does:

- The first packet of a connection must be `CONNECT`, and only `PUBLISH`es from the session whose client_id is `DEVICE_ID`, on its own `devices/<DEVICE_ID>/...` topics, are relayed to Mosquitto. Other clients are logged but cannot inject values into Home Assistant.
- **`DEVICE_IP` (optional, recommended)**: connections from any other IP are closed immediately. Without it, a device on your LAN that knows the box's MAC (visible in any ARP table) can connect with its client_id, take over the session and receive your commands instead of the box. Reserve the box's IP in your DHCP server before setting it, otherwise the bridge stops accepting the box when its IP changes (look for `REFUSED` in the logs). A firewall rule on the host restricting port 8883 to the box's IP gives the same protection.
- Commands only come from your Mosquitto: protect it with a username/password, otherwise any LAN device can publish on `aldes/vmc/cmd/...` and control the VMC directly.
- The box does not need Internet access once the bridge is set up, except NTP (see [Box clock (NTP)](#box-clock-ntp)). Isolating it (its own VLAN, outbound traffic blocked) is a good idea; with the NTP redirection, it can stay fully offline.

## Operations

- **Logs**: `LOG_LEVEL=info` (default) logs connections, commands and the first telemetry frame of each connection. `LOG_LEVEL=debug` logs every frame with its payload, as older versions did; `warning` only problems. `mitm.log` rotates at 5 MB (`LOG_MAX_BYTES`, `LOG_BACKUPS`).
- **Docker image**: the bridge runs as the unprivileged user `aldes` (uid 1000). The entrypoint starts as root only to take ownership of `/data` and `/logs`, so a `data/` folder created by an older image keeps working. If that is refused (e.g. on a network share), it runs as root and logs a warning. You can also set `user:` in `docker-compose.yml`; the volumes must then be writable by that user.
- **Health check**: the container is *healthy* while the broker loop runs and Mosquitto is connected (`docker ps` shows the status). An offline box does not make it unhealthy: that is what HA's availability is for.
- **TLS with the box**: the broker offers every cipher suite (`ALL:@SECLEVEL=0`) because the HF-LPB100 firmware only speaks legacy TLS. Each connection logs the negotiated version and suite; you can then restrict `TLS_CIPHERS` to it.
- **TLS to Mosquitto**: `UPSTREAM_MQTT_TLS=1`, with `UPSTREAM_MQTT_CA` for a private CA, or `UPSTREAM_MQTT_TLS_INSECURE=1` to accept any certificate.
- Invalid settings (e.g. `LOG_MAX_BYTES=5M`) stop the bridge at startup with an explicit message.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

The end-to-end tests start the bridge as a subprocess against [amqtt](https://github.com/Yakifo/amqtt) (a pure-Python MQTT broker standing in for Mosquitto) and fake Connect Boxes speaking MQTT over TLS. They run on Linux (one test connects from `127.0.0.2`). CI runs them on every pull request, along with a Docker build and start-up check.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — full data flow
- [Protocol](docs/PROTOCOL.md) — Aldes JSON-RPC commands and telemetry format
- [Reverse engineering log](docs/REVERSE-ENGINEERING.md) — how we found the protocol from the APK + traffic

## Disclaimer

This project performs **reverse engineering for interoperability** of the Aldes Connect Box. In the EU, this is permitted under [Directive 2009/24/EC, Article 6](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32009L0024). It does not redistribute Aldes' app code, manuals, or proprietary protocol details — only the minimum necessary to communicate with the device you legally own.

Use at your own risk. The author is not affiliated with Aldes. Aldes may change their firmware or backend at any time and break this project.

## Credits

- **[zongyl/smartLink](https://github.com/zongyl/smartLink)** — Hi-Flying SmartLink V3 Java reference (used for `tools/aldes_pair.py`)
- **[The-Smartest-Home/hass_inspirair](https://github.com/The-Smartest-Home/hass_inspirair)** — InspirAir Modbus register map (referenced during early reverse engineering)
- **[skylot/jadx](https://github.com/skylot/jadx)** — APK decompiler used to map the JSON-RPC commands
- **[guix77/homeassistant-aldes](https://github.com/guix77/homeassistant-aldes)** and forks — early cloud-based attempts that revealed the API URLs

## License

[MIT](LICENSE) — Do whatever you want, just don't blame me if Aldes pushes a firmware update.
