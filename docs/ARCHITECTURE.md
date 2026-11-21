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
- **MQTT 3.1.1 codec**: minimal hand-rolled parser. Only the packet types we care about (CONNECT, PUBLISH, SUBSCRIBE, UNSUBSCRIBE, PUBACK, PINGREQ, DISCONNECT). No retained message handling, no QoS 2 — the box doesn't need them. Packets larger than 64 kB and a second CONNECT on the same connection close the connection.
- **Box sessions**: one thread per connection, which does *all* the TLS reads and writes (OpenSSL does not support two threads using one SSL object at once). Commands coming from Mosquitto are queued and sent by the connection thread within 0.2 s. A connection is closed after 1.5× its MQTT keepalive without traffic; a new connection with the same `client_id` replaces the previous one.
- **`UpstreamBridge`**: paho-mqtt client connected to user's Mosquitto (reconnects automatically). Republishes telemetry as retained, listens for `aldes/vmc/cmd/#` for command injection — **retained messages on that topic are ignored**, otherwise they would be replayed to the box on every reconnect. Publishes `aldes/vmc/status` (`online`/`offline`, with an LWT) for HA availability.
- **Telemetry merge**: the box sends a short boot heartbeat (firmware, RSSI) after connecting and the full telemetry every 1–5 min, on the same topic. Each JSON frame is merged into the last known values before being republished, and `aldes/vmc/last_seen` (retained, ISO 8601) records when the last frame arrived. After a restart, the merge is seeded from the retained copy in Mosquitto.
- **`CommandTracker`**: commands are QoS 0, so mode changes are confirmed through the telemetry. After a command, the next frame carrying `current_mode` (ignoring frames received within 3 s, possibly sampled before the change) either confirms it or triggers one resend; a second mismatch reports a failure. No frame within `CMD_CONFIRM_TIMEOUT` → "unconfirmed". Programme (`Z`) and `changeCMO` cannot be checked and are reported as "sent". The outcome goes to `aldes/vmc/command_result` (retained JSON).
- **`publish_ha_discovery()`**: on every connection to Mosquitto, publishes ~29 retained config messages on `homeassistant/{sensor,select,button}/aldes_vmc_<MAC>/<id>/config`. HA auto-creates the entities.
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
- **HA shows "unknown" values** → the box hasn't pushed a full telemetry frame yet since the very first start (interval is ~1–5 min). Wait for one cycle. Afterwards, frames are merged with the last known values (also restored from Mosquitto's retained copy when the bridge restarts), so the boot heartbeat no longer blanks the sensors.
