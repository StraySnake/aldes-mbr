# Architecture

## Big picture

```
┌──────────────────┐      DNS query                 ┌─────────────┐
│  Connect Box     │ ─────────────────────────────> │   Pi-hole   │
│  (HF-LPB100)     │      aldesiotsuite.azure-      │             │
│  192.168.x.x     │      devices.net               │             │
└─────────┬────────┘                                └─────┬───────┘
          │           returns local IP                    │
          │ <─────────────────────────────────────────────┘
          │
          │  MQTT/TLS to local IP:8883
          │  client_id = <MAC>_AIR
          │  username  = aldesiotsuite.azure-devices.net/<MAC>_AIR/api-version=2016-11-14
          │  password  = SharedAccessSignature ...
          ▼
┌──────────────────────────────────────────────┐
│  fake_iothub.py (this project)               │
│  - TLS server with self-signed cert,         │
│    legacy ciphers (SECLEVEL=0)               │
│  - Decodes MQTT 3.1.1 frames                 │
│  - Republishes PUBLISH telemetry to upstream │
│  - Listens for command injections,           │
│    forwards as MQTT PUBLISH back to box      │
└─────────┬────────────────────────────────────┘
          │
          │ MQTT 1883 (plain)
          ▼
┌──────────────────────────────────────────────┐
│  Mosquitto                                    │
│  topics:                                      │
│   aldes/vmc/raw/devices/<MAC>_AIR/messages/   │
│      events/         (telemetry, retain=true) │
│   aldes/vmc/cmd/macro/<name>                  │
│      (commands from HA)                       │
│   homeassistant/sensor/aldes_vmc_<MAC>/...    │
│      (HA discovery configs, retain=true)      │
└─────────┬────────────────────────────────────┘
          │
          ▼
   Home Assistant
   (auto-discovered device + entities)
```

## Why this works

The HF-LPB100 firmware in the Aldes Connect Box has a few oversights that make this project possible:

1. **DNS resolution at every reconnect**: the box does not pin to an IP — it queries DNS for `aldesiotsuite.azure-devices.net` whenever the MQTT connection drops (which happens often).
2. **No certificate pinning**: the box accepts any TLS certificate as long as the cipher suite is one it knows. It speaks legacy TLS 1.0/1.2 with old ciphers (we serve `SECLEVEL=0` on the Python side to negotiate).
3. **No client certificate authentication**: it sends a username (`<hub>/<deviceId>/api-version=...`) and a SharedAccessSignature as password, but never presents a client cert.
4. **Standard Azure IoT Hub MQTT topology**: the box subscribes to `devices/<id>/messages/devicebound/#` and publishes telemetry to `devices/<id>/messages/events/`. We just have to replay messages on these topics.

## Component breakdown

### `fake_iothub.py`

- **`gen_cert()`**: creates a self-signed TLS cert with CN=`aldesiotsuite.azure-devices.net` and SAN=`*.azure-devices.net`. Stored in `/data/` for persistence across container restarts.
- **MQTT 3.1.1 codec**: minimal hand-rolled parser. Only the packet types we care about (CONNECT, PUBLISH, SUBSCRIBE, PUBACK, PINGREQ, DISCONNECT). No retained message handling, no QoS 2 — the box doesn't need them.
- **`UpstreamBridge`**: paho-mqtt client connected to user's Mosquitto. Republishes telemetry as retained, listens for `aldes/vmc/cmd/#` for command injection.
- **`publish_ha_discovery()`**: at startup, publishes ~17 retained config messages on `homeassistant/{sensor,select,button}/aldes_vmc_<MAC>/<id>/config`. HA auto-creates the entities.
- **`_macro_payload()`**: translates high-level macros (`vacances1j`, `boost`, etc.) to the actual Aldes JSON-RPC payload, including computing UTC date strings for holiday mode.

### `network_mode: host`

Required because Docker bridge NAT hides the real client IP — the broker would see all connections as coming from `172.18.0.1`, useless for debugging and for any future IP-based logic. `host` mode also avoids extra firewall rules for cross-subnet traffic.

### Pi-hole

Default DNS for the box (delivered via DHCP). The broker host runs Pi-hole or the user has another DNS that supports per-hostname rewrites. Custom config goes in `/etc/pihole/pihole.toml` under `dns.hosts` (Pi-hole v6+).

## Failure modes

- **Box won't reconnect** → check that DNS resolves to your Docker host IP. Use `nslookup aldesiotsuite.azure-devices.net <pihole-ip>` from a client.
- **TLS handshake FAILED: NO_SHARED_CIPHER** → ensure Python ssl was built with `SECLEVEL=0` support. The Dockerfile uses `python:3.12-slim` which works.
- **TLS handshake FAILED: UNEXPECTED_EOF** → the box closed the connection. Most often because of `network_mode: bridge` rate-limiting or NAT issues — switch to `network_mode: host`.
- **No HA entities appear** → either MQTT discovery prefix doesn't match HA's config (default `homeassistant`), or HA hasn't reloaded MQTT (Settings → Devices & Services → MQTT → Reload).
- **HA shows "unknown" values** → the box hasn't pushed a telemetry yet (interval is ~1–5 min) and the broker hasn't started republishing with retain. Wait for one cycle.
