# Reverse engineering log

How we found the protocol. Useful for anyone porting this project to a different Aldes model (T.Flow, T.One, EasyHome).

## What we knew at the start

- The Aldes Connect Box ships a **HF-LPB100** WiFi module (Hi-Flying / Shanghai High-Flying), confirmed by the MAC OUI `28:9C:6E` and the hostname `HF-LPB100`.
- The HF-LPB100 has well-known AT commands over UDP port 48899 (discovery via `HF-A11ASSISTHREAD`).
- The Aldes Connect app is a heavy Android app from Keyrus consultancy, decompilable with [jadx](https://github.com/skylot/jadx).
- A few Home Assistant integrations exist already, all cloud-based: [guix77](https://github.com/guix77/homeassistant-aldes), [Fredzxda](https://github.com/Fredzxda/homeassistant-aldes), [hugz](https://github.com/hugz/homeassistant-aldes), [tiliarou](https://github.com/tiliarou/homeassistant-aldes). They all hit `https://aldesiotsuite-aldeswebapi.azurewebsites.net/` (Azure App Service) and POST `/aldesoc/v5/users/me/products/<mac>/commands`. They share the same reliability problems as the official app.

## Step 1 — Confirm the cloud is broken for our user

We tested the REST API directly:

```bash
curl -X POST https://aldesiotsuite-aldeswebapi.azurewebsites.net/oauth2/token \
  -d grant_type=password -d username=... -d password=...
```

The endpoint returned **HTTP 503 in ~5 attempts out of 6**, with body:

```json
{"code":"503","message":"An operation on a socket could not be performed because the system lacked sufficient buffer space or because a queue was full. (login.aldesgroupe.com:443)"}
```

The `login.aldesgroupe.com` upstream (their Identity Provider) is overloaded. Even when auth succeeds and we POST `changeMode`, the response is `Status: CREATED` and `isConnected: false` — the cloud accepts the command but the box never sees it. Confirmed by polling the device state on a 5-min window: `set_spd` and `current_mode` never change.

So: **Aldes' problem is in their cloud's C2D delivery, not in our auth or in HA**. We need to bypass the cloud entirely.

## Step 2 — Locate the box's outbound traffic

The box only has WiFi, so we couldn't tap it physically. We used **ARP spoofing** with [scapy](https://scapy.net/) to make our PC pretend to be the LAN gateway, then sniffed in promisc mode — see [`tools/arp_sniff.py`](https://github.com/<you>/aldes-mqtt-bridge/blob/main/tools/arp_sniff.py) (in the original project repo, not shipped here).

After ~2 min the box revealed:

```
192.168.X.215 → 13.69.109.2:8883      # Azure IoT Hub MQTT/TLS
192.168.X.215 → 192.168.Y.2:53         # DNS → aldesiotsuite.azure-devices.net
192.168.X.215 → 192.168.Y.2:53         # DNS → aldes.blob.core.windows.net  (probably OTA)
192.168.X.215 → 61.164.36.105:123      # NTP (pool.ntp.org)
```

Two key facts:

1. The box uses **port 8883** (MQTT over TLS), the standard Azure IoT Hub port.
2. It does a fresh DNS query for `aldesiotsuite.azure-devices.net` every reconnection — meaning we can hijack via DNS, no need to touch ARP or the network gateway.

## Step 3 — DNS hijack with Pi-hole

Most home-lab users already have Pi-hole. In Pi-hole v6 we add to `/etc/pihole/pihole.toml` under `[dns]`:

```toml
hosts = [ "<docker-host-ip> aldesiotsuite.azure-devices.net" ]
```

Then `pihole reloaddns`. Verify:

```bash
nslookup aldesiotsuite.azure-devices.net <docker-host-ip>
# → <docker-host-ip>  ✓
```

## Step 4 — Make the box accept our self-signed cert

Our first attempt at a Python TLS server failed with:

```
TLS handshake FAILED: [SSL: NO_SHARED_CIPHER] no shared cipher
```

Modern Python's default ssl context excludes legacy ciphers. The HF-LPB100 firmware speaks old TLS (1.0/1.2) with a small set of ciphers. Fix:

```python
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.minimum_version = ssl.TLSVersion.TLSv1
ctx.set_ciphers("ALL:@SECLEVEL=0")
```

After this, the box completes the TLS handshake without complaint. **It does not validate the certificate** — no pinning, no chain check. Self-signed CN=`aldesiotsuite.azure-devices.net` is enough.

## Step 5 — Decode MQTT, find the topics

We wrote a minimal MQTT 3.1.1 codec (no library dependency for the inner parsing — paho-mqtt is used only for the upstream bridge). The first CONNECT revealed:

```
client_id  = AABBCCDDEEFF_AIR
username   = aldesiotsuite.azure-devices.net/AABBCCDDEEFF_AIR/api-version=2016-11-14
password   = SharedAccessSignature sr=...&sig=...
SUBSCRIBE   devices/AABBCCDDEEFF_AIR/messages/devicebound/#
```

Standard Azure IoT Hub MQTT topology. The subscribe topic `devicebound` is the inbox for **Cloud-to-Device** messages — that's where we publish commands.

## Step 6 — Find the JSON-RPC method names

We decompiled the APK with jadx and explored:

```
sources/com/keyrus/aldes/utils/Constants.java
  METHOD_CHANGE_MODE          = "changeMode"
  METHOD_CHANGE_CMO           = "changeCMO"
  METHOD_CHANGE_PLANNING_MODE = "changePlanningMode"
  METHOD_CHANGE_TEMPERATURE_REFERENCE = "changeTemperatureReference"

sources/com/keyrus/aldes/data/net/model/command/BaseCommand.java
  // Standard JSON-RPC 2.0: {"id":1, "jsonrpc":"2.0", "method":..., "params":[...]}

sources/com/keyrus/aldes/data/net/model/command/StringCommand.java
sources/com/keyrus/aldes/data/net/model/command/ModeOverrideCommand.java
sources/com/keyrus/aldes/data/net/model/command/TemperatureCommand.java

sources/com/keyrus/aldes/domain/interactor/CommandInteractor.java
  sendModeCommand(mac, mode)       → StringCommand("changeMode", listOf(mode))
  sendHolidaysModeCommand(mac, s, e) → StringCommand("changeMode", listOf("W" + s + e))
  updateModeOverride(mac, bool)    → ModeOverrideCommand(listOf(bool ? 1 : 0))
```

This gave us the exact format of every command and the date concatenation trick for holiday mode.

## Step 7 — Identify mode codes & verify mapping

`ProgramMode.java` enumerates: `V` (Daily), `W` (Holidays), `X` (Guests), `Y` (Kitchen/Boost), `Z` (Off/Schedule), `B` (Comfort), `C` (Eco).

But the **mapping mode→speed** is product-specific and not encoded in the app. We tested empirically on a Dee Fly Cube:

| code | observed flow (extf_flw) | observed set_spd |
|---|---|---|
| Z | varies (schedule) | varies |
| V | 165 m³/h | 1 |
| Y | 250 m³/h | 2 (ramps) |
| X | 330 m³/h | 3 |

So on Dee Fly Cube, **X is "max boost"** (V4) and **Y is "guests/intermediate"** (V3). The HVAC app calls Y "Kitchen" and X "Guests" but the resulting fan speeds are inverted compared to the InspirAir documentation.

We exposed both `boost`→X and `invites`→Y macros in the broker so users get the labels matching what they observe physically.

## Step 8 — HA discovery

Once we had stable telemetry being republished on Mosquitto, we hard-coded the [Home Assistant MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) configs into `publish_ha_discovery()` — 15 sensors covering every meaningful field in the JSON, plus a Mode select that uses a Jinja template both for state and command via a server-side macro.

Done. ~30 lines of YAML in HA, zero manual config, full sensor pack + remote control.

## Tools used

- [jadx 1.5.5](https://github.com/skylot/jadx/releases) — APK decompiler
- [scapy](https://scapy.net/) + [Npcap](https://npcap.com/) — ARP spoof + packet sniff (Windows)
- [Wireshark / tshark](https://www.wireshark.org/) — packet inspection
- [paho-mqtt](https://pypi.org/project/paho-mqtt/) — upstream MQTT client
- [Pi-hole](https://pi-hole.net/) — DNS rewrite
