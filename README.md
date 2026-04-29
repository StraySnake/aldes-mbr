# aldes-mqtt-bridge

> Local control of Aldes Connect ventilation units (VMC / HRV) via Home Assistant — bypassing the unreliable Aldes cloud.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

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
3. The MQTT-CONNECT, SUBSCRIBE and PUBLISH frames are decoded, and PUBLISH telemetry is **republished to your Mosquitto** under `aldes/vmc/raw/...` with `retain=true`
4. **Home Assistant MQTT discovery** is auto-published — sensors and a Mode select appear automatically under a "VMC Aldes Dee Fly Cube" device
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

2. **Edit `docker-compose.yml`**: set `DEVICE_MAC` and `UPSTREAM_MQTT_HOST` (your Mosquitto IP).

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

6. **Open Home Assistant** — a "VMC Aldes" device should appear automatically with all sensors and a Mode select.

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
| `vacances` / `vacances1j` / `vacances7j` / `vacances14j` | mode W with auto dates (now → now+Nd) |
| `vacances_stop` | cancel holidays |
| `programme` / `z` | mode Z (schedule) |
| `override_on` / `override_off` | `changeCMO 1` / `changeCMO 0` |

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
