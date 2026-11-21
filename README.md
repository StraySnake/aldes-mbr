# aldes-mqtt-bridge

> Local control of Aldes Connect ventilation units (VMC / HRV) via Home Assistant — bypassing the unreliable Aldes cloud.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/ghcr.io-aldes--mqtt--bridge-blue?logo=docker)](https://github.com/djo1338/aldes-mqtt-bridge/pkgs/container/aldes-mqtt-bridge)

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

### Setup

1. **Clone this repo on your Docker host**
   ```bash
   git clone https://github.com/djo1338/aldes-mqtt-bridge.git
   cd aldes-mqtt-bridge
   cp docker-compose.yml.example docker-compose.yml
   ```

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

5. **Wait for the box to reconnect** (1–5 min, or power-cycle it). You should see in the logs:
   ```
   NEW CONNECTION from <box-IP>:xxxxx
   CONNECT proto=MQTT level=4 client_id=XXXXXXXXXXXX_AIR
   SUBSCRIBE topic = devices/XXXXXXXXXXXX_AIR/messages/devicebound/#
   PUBLISH ... payload = {"outside_tpt":..., "current_mode":"V", ...}
   ```

6. **Open Home Assistant** — an "Aldes VMC" device should appear automatically with all sensors and a Mode select (entity IDs `sensor.aldes_vmc_*`, `select.aldes_vmc_mode` with the default `DEVICE_NAME`). Entities show as *unavailable* while the box is not connected to the bridge (availability topic `aldes/vmc/status`), except **Dernière mise à jour** (`sensor.aldes_vmc_derniere_mise_a_jour`), the time of the last frame received from the box: it stays visible so you can alert on a silent VMC, e.g. "last update more than 1 h ago". Sensors also turn *unavailable* if no frame arrives for 15 min (`SENSOR_EXPIRE_AFTER`, in seconds, `0` to disable), instead of showing frozen values. Other entities: **Début / Fin des vacances**, **Dernière commande** (see [Command confirmation](#command-confirmation)) and the **Ignorer le programme / Suivre le programme** buttons (`changeCMO`).

7. **Optional**: copy [`examples/homeassistant/dashboard.yaml`](examples/homeassistant/dashboard.yaml) into your Lovelace, and [`examples/homeassistant/automations.yaml`](examples/homeassistant/automations.yaml) for a sample night/day cycle automation.

## Mode mapping (validated empirically on Dee Fly Cube)

| Aldes code | Friendly name | Speed | Flow observed |
|---|---|---|---|
| W | Vacances (Holiday) | V1 base | ~50 m³/h |
| V | Quotidien (Daily) | V2 normal | ~165 m³/h |
| Y | Invités (Guests) | V3 | ~250 m³/h |
| X | Boost | V4 max | ~330 m³/h |
| Z | Programme (Schedule) | varies | follows weekly plan |

⚠️ **Y/X are inverted compared to InspirAir documentation** — measured on a Dee Fly Cube. If you have a different Aldes model, run the broker and watch which mode produces what flow before trusting these labels.

## Macros

The broker exposes high-level macros at `aldes/vmc/cmd/macro/<name>` that translate to the proper Aldes JSON-RPC payload server-side (e.g. computing the holiday date range automatically):

| Macro | Effect |
|---|---|
| `quotidien` / `v` | mode V (V2) |
| `invites` / `y` | mode Y (V3) |
| `boost` / `x` | mode X (V4) |
| `vacances` / `vacances<N>j` (e.g. `vacances1j`, `vacances21j`) | mode W with auto dates (now → now+N days, 1–365) |
| `vacances_stop` | cancel holidays |
| `programme` / `z` | mode Z (schedule) |
| `override_on` / `override_off` | `changeCMO 1` / `changeCMO 0` |

### Command confirmation

Commands reach the box without acknowledgement (MQTT QoS 0). For mode changes, the bridge checks the next telemetry frames: if the box still reports the old mode, the command is sent once more, then reported as failed. The outcome is published (retained) on `aldes/vmc/command_result` and shown by the **Dernière commande** sensor: *En attente*, *Confirmée*, *Renvoyée*, *Échec*, *Non confirmée* (no telemetry within `CMD_CONFIRM_TIMEOUT`, 15 min by default), *Envoyée* (not verifiable: Programme, override), *Box déconnectée*. The attributes give the command, the expected and the reported mode.

Since the box sends its telemetry every 1–5 min, confirmation can take a few minutes.

Publish commands with `retain=false`: retained messages on `aldes/vmc/cmd/#` are ignored (they would otherwise be replayed to the box every time the bridge reconnects).

## Security

The box cannot be authenticated: its SAS token is signed with an Aldes key we do not have. What the broker does:

- The first packet of a connection must be `CONNECT`, and only `PUBLISH`es from the session whose client_id is `DEVICE_ID`, on its own `devices/<DEVICE_ID>/...` topics, are relayed to Mosquitto. Other clients are logged but cannot inject values into Home Assistant.
- **`DEVICE_IP` (optional, recommended)**: connections from any other IP are closed immediately. Without it, a device on your LAN that knows the box's MAC (visible in any ARP table) can connect with its client_id, take over the session and receive your commands instead of the box. Reserve the box's IP in your DHCP server before setting it, otherwise the bridge stops accepting the box when its IP changes (look for `REFUSED` in the logs). A firewall rule on the host restricting port 8883 to the box's IP gives the same protection.
- Commands only come from your Mosquitto: protect it with a username/password, otherwise any LAN device can publish on `aldes/vmc/cmd/...` and control the VMC directly.

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
