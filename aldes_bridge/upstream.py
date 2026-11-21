"""Connection to the user's Mosquitto."""
import ssl, threading
from .config import (
    EVENTS_TOPIC, PREFIX, STATUS_TOPIC, UPSTREAM_CA, UPSTREAM_HOST, UPSTREAM_PASS,
    UPSTREAM_PORT, UPSTREAM_TLS, UPSTREAM_TLS_INSECURE, UPSTREAM_USER,
)
from .logs import log
from .telemetry import _telemetry, _telemetry_lock, decode_frame, seed_telemetry
from .box import box_online
from .discovery import publish_ha_discovery


# ---------- upstream bridge to user's Mosquitto ----------

class UpstreamBridge:
    def __init__(self):
        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="aldes-mitm-bridge",
            clean_session=True,
        )
        if UPSTREAM_USER:
            self.client.username_pw_set(UPSTREAM_USER, UPSTREAM_PASS)
        if UPSTREAM_TLS:
            if UPSTREAM_TLS_INSECURE:
                # tls_insecure_set() alone only skips the hostname check: a self-signed
                # certificate would still be rejected.
                self.client.tls_set(cert_reqs=ssl.CERT_NONE)
                self.client.tls_insecure_set(True)
            else:
                self.client.tls_set(ca_certs=UPSTREAM_CA)
        self.client.will_set(STATUS_TOPIC, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        # Our own retained telemetry, read back once to seed the merge after a restart.
        self.client.message_callback_add(EVENTS_TOPIC, self._on_seed)
        self.cmd_callbacks = []  # list of fn(topic, payload) called when user injects a cmd
        self._status_lock = threading.Lock()  # keeps "compute status" + "publish" atomic
        # connect_async + loop_start: paho keeps retrying in the background, so the bridge
        # comes up even if Mosquitto starts after us (e.g. NAS boot).
        self.client.connect_async(UPSTREAM_HOST, UPSTREAM_PORT, keepalive=60)
        self.client.loop_start()
        log(f"upstream MQTT connecting to {UPSTREAM_HOST}:{UPSTREAM_PORT}"
            f"{' (TLS' + (', certificate NOT verified' if UPSTREAM_TLS_INSECURE else '') + ')' if UPSTREAM_TLS else ''}...")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log(f"upstream MQTT connection refused: {reason_code}")
            return
        log(f"upstream MQTT connected to {UPSTREAM_HOST}:{UPSTREAM_PORT}")
        # Subscribe to command-injection topic so HA can publish e.g. aldes/vmc/cmd/macro/boost
        client.subscribe(f"{PREFIX}/cmd/#")
        with _telemetry_lock:
            need_seed = not _telemetry
        if need_seed:
            client.subscribe(EVENTS_TOPIC)
        publish_ha_discovery(client)
        self.publish_status()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log(f"upstream MQTT disconnected: {reason_code} (will retry)")

    def _on_seed(self, client, userdata, msg):
        # First message on our telemetry topic: the retained copy if there is one,
        # otherwise our own next publish. Either way, one is enough.
        client.unsubscribe(EVENTS_TOPIC)
        if not msg.retain:
            return
        values = decode_frame(msg.payload)
        if values and seed_telemetry(values):
            log(f"telemetry merge seeded with {len(values)} values from the retained copy")

    def _on_message(self, client, userdata, msg):
        if msg.retain:
            # A command published with retain=true would be replayed by Mosquitto at every
            # (re)connect of the bridge, e.g. restarting holidays after each reboot.
            log(f"upstream RX cmd topic={msg.topic} IGNORED: retained message "
                f"(publish commands with retain=false; clear it by publishing an empty retained message)")
            return
        log(f"upstream RX cmd topic={msg.topic} payload={msg.payload!r}")
        for cb in self.cmd_callbacks:
            try:
                cb(msg.topic, msg.payload)
            except Exception as e:
                log(f"cmd callback err: {e}")

    def publish(self, subtopic, payload, qos=0, retain=False):
        topic = f"{PREFIX}/{subtopic}"
        try:
            self.client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as e:
            log(f"upstream publish err: {e}")

    def publish_status(self):
        # Under a lock so that a session closing and a session opening at the same time
        # cannot publish their states in the wrong order (stale "offline" after "online").
        with self._status_lock:
            try:
                self.client.publish(STATUS_TOPIC, "online" if box_online() else "offline",
                                    qos=1, retain=True)
            except Exception as e:
                log(f"upstream status publish err: {e}")

    def stop(self):
        try:
            with self._status_lock:
                info = self.client.publish(STATUS_TOPIC, "offline", qos=1, retain=True)
            info.wait_for_publish(2)
        except Exception:
            pass
        self.client.disconnect()
        self.client.loop_stop()
