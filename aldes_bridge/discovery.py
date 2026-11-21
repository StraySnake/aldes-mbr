"""Home Assistant MQTT discovery."""
import json
from .config import (
    CMD_RESULT_TOPIC, DEVICE_MAC, DEVICE_MODEL, DEVICE_NAME, EVENTS_TOPIC, HA_DISCO_PREFIX,
    LAST_SEEN_TOPIC, PREFIX, SENSOR_EXPIRE_AFTER, STATUS_TOPIC,
)
from .logs import log
from .macros import MODES, SELECT_OPTIONS
from .commands import CMD_STATUS_LABELS


# ---------- Home Assistant MQTT Discovery ----------

DEVICE_BLOCK = {
    "identifiers": [f"aldes_vmc_{DEVICE_MAC}"],
    "name": DEVICE_NAME,
    "manufacturer": "Aldes",
    "model": DEVICE_MODEL,
    "sw_version": "Connect Box (HF-LPB100)",
}

AVAILABILITY = {
    "availability_topic": STATUS_TOPIC,
    "payload_available": "online",
    "payload_not_available": "offline",
}

# Each tuple: (object_id, friendly_name, payload_key, dict_extra)
SENSORS = [
    ("outside_temperature", "Air extérieur",        "outside_tpt",  {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}),
    ("indoor_temperature",  "Air intérieur (extrait)", "ext_tpt",   {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}),
    ("reject_temperature",  "Air rejeté",            "reject_tpt",  {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}),
    ("extract_flow",        "Débit extraction",      "extf_flw",    {"unit_of_measurement": "m³/h", "icon": "mdi:fan-chevron-down", "state_class": "measurement"}),
    ("supply_flow",         "Débit insufflation",    "ffe_flw",     {"unit_of_measurement": "m³/h", "icon": "mdi:fan-chevron-up", "state_class": "measurement"}),
    ("extract_speed",       "Vitesse extraction",    "extf_spd",    {"unit_of_measurement": "rpm", "icon": "mdi:fan", "state_class": "measurement"}),
    ("supply_speed",        "Vitesse insufflation",  "vi_spd",      {"unit_of_measurement": "rpm", "icon": "mdi:fan", "state_class": "measurement"}),
    ("exchanger_power",     "Puissance échangeur",   "echange_pwr", {"device_class": "power", "unit_of_measurement": "W", "state_class": "measurement"}),
    ("set_speed",           "Vitesse demandée",      "set_spd",     {"icon": "mdi:speedometer", "state_class": "measurement"}),
    ("co2",                 "CO₂",                   "Co2_vmc",     {"device_class": "carbon_dioxide", "unit_of_measurement": "ppm", "state_class": "measurement"}),
    ("pm25",                "PM2.5",                 "Pm25_Vmc",    {"device_class": "pm25", "unit_of_measurement": "µg/m³", "state_class": "measurement"}),
    ("voc",                 "COV",                   "Voc_Vmc",     {"icon": "mdi:molecule", "state_class": "measurement"}),
    ("rssi",                "Signal WiFi",           "RSSI",        {"icon": "mdi:wifi", "entity_category": "diagnostic"}),
    ("fw_wifi",             "Firmware WiFi",         "Vers_W",      {"icon": "mdi:chip", "entity_category": "diagnostic"}),
    ("fw_uc",               "Firmware MCU",          "Vers_UC",     {"icon": "mdi:chip", "entity_category": "diagnostic"}),
]

def publish_ha_discovery(client):
    """Publish HA MQTT Discovery configs for all VMC entities. Called on each upstream connect."""
    log("publishing HA discovery...")
    base_id = f"aldes_vmc_{DEVICE_MAC}"

    # Sensors
    for obj_id, name, key, extra in SENSORS:
        cfg = {
            "name": name,
            "unique_id": f"{base_id}_{obj_id}",
            "state_topic": EVENTS_TOPIC,
            "value_template": f"{{{{ value_json.{key} | default(none) }}}}",
            "device": DEVICE_BLOCK,
            **AVAILABILITY,
            **extra,
        }
        if SENSOR_EXPIRE_AFTER > 0:
            # Box silent (e.g. half-open connection, not yet detected): show
            # "unavailable" rather than frozen values.
            cfg["expire_after"] = SENSOR_EXPIRE_AFTER
        topic = f"{HA_DISCO_PREFIX}/sensor/{base_id}/{obj_id}/config"
        client.publish(topic, json.dumps(cfg), qos=1, retain=True)

    # Holiday start/end. ASSUMPTION: Start_date_mode / End_date_mode use the same
    # yyyyMMddHHmmssZ format as the changeMode W command (unverified on a real box);
    # anything else, "" or the 0001-01-01 placeholder shows as unknown.
    for obj_id, name, key in (("holiday_start", "Début des vacances", "Start_date_mode"),
                              ("holiday_end", "Fin des vacances", "End_date_mode")):
        tpl = (
            "{% set d = (value_json." + key + " | default('')) | string %}"
            "{% if d is match('^[0-9]{14}Z?$') and d[0:4] != '0001' %}"
            "{{ d[0:4] }}-{{ d[4:6] }}-{{ d[6:8] }}T{{ d[8:10] }}:{{ d[10:12] }}:{{ d[12:14] }}+00:00"
            "{% else %}None{% endif %}"
        )
        client.publish(
            f"{HA_DISCO_PREFIX}/sensor/{base_id}/{obj_id}/config",
            json.dumps({
                "name": name,
                "unique_id": f"{base_id}_{obj_id}",
                "state_topic": EVENTS_TOPIC,
                "value_template": tpl,
                "device_class": "timestamp",
                "icon": "mdi:bag-suitcase",
                "device": DEVICE_BLOCK,
                **AVAILABILITY,
            }),
            qos=1, retain=True,
        )

    # Outcome of the last command (see CommandTracker).
    labels = json.dumps(CMD_STATUS_LABELS, ensure_ascii=False).replace('"', "'")
    client.publish(
        f"{HA_DISCO_PREFIX}/sensor/{base_id}/last_command/config",
        json.dumps({
            "name": "Dernière commande",
            "unique_id": f"{base_id}_last_command",
            "state_topic": CMD_RESULT_TOPIC,
            "value_template": "{% set m = " + labels + " %}{{ m.get(value_json.status, value_json.status) }}",
            "json_attributes_topic": CMD_RESULT_TOPIC,
            "icon": "mdi:message-check-outline",
            "device": DEVICE_BLOCK,
        }),
        qos=1, retain=True,
    )

    # Last frame received from the box. No availability on purpose: it is most useful
    # precisely when the box is offline ("silent since ...").
    client.publish(
        f"{HA_DISCO_PREFIX}/sensor/{base_id}/last_seen/config",
        json.dumps({
            "name": "Dernière mise à jour",
            "unique_id": f"{base_id}_last_seen",
            "state_topic": LAST_SEEN_TOPIC,
            "device_class": "timestamp",
            "icon": "mdi:clock-check-outline",
            "device": DEVICE_BLOCK,
        }),
        qos=1, retain=True,
    )

    # Mode select. "Vacances" auto-resolves now -> now+1d via the macro translator
    # (date math is hard in Jinja). Frames are merged, so current_mode is normally
    # always there; if not (first frame ever is a heartbeat), output nothing: HA keeps
    # the previous state on an empty template output.
    mode_map = json.dumps(MODES, ensure_ascii=False).replace('"', "'")
    mode_select_cfg = {
        "name": "Mode",
        "unique_id": f"{base_id}_mode",
        "state_topic": EVENTS_TOPIC,
        "value_template": (
            "{% set m = " + mode_map + " %}"
            "{% set code = (value_json.current_mode | default(''))[:1] %}"
            "{{ m[code] if code in m else '' }}"
        ),
        "command_topic": f"{PREFIX}/cmd/macro/_select",
        "command_template": "{{ value }}",
        "options": SELECT_OPTIONS,
        "icon": "mdi:fan-clock",
        "device": DEVICE_BLOCK,
        **AVAILABILITY,
    }
    client.publish(
        f"{HA_DISCO_PREFIX}/select/{base_id}/mode/config",
        json.dumps(mode_select_cfg), qos=1, retain=True,
    )

    # Quick-action buttons via macro topics (broker translates server-side)
    macro_topic = f"{PREFIX}/cmd/macro"
    macros = [
        ("quotidien",     "Quotidien",         "mdi:fan-speed-2"),
        ("invites",       "Invités",           "mdi:fan-speed-3"),
        ("boost",         "Boost",             "mdi:fan-plus"),
        ("vacances1j",    "Vacances 1 jour",   "mdi:bag-suitcase"),
        ("vacances7j",    "Vacances 7 jours",  "mdi:bag-suitcase"),
        ("vacances_stop", "Annuler vacances",  "mdi:bag-suitcase-off"),
        ("programme",     "Programme",         "mdi:fan-clock"),
        # changeCMO: the box does not report this state, hence buttons, not a switch.
        ("override_on",   "Ignorer le programme", "mdi:calendar-remove"),
        ("override_off",  "Suivre le programme",  "mdi:calendar-check"),
    ]
    for key, name, icon in macros:
        cfg = {
            "name": name,
            "unique_id": f"{base_id}_btn_{key}",
            "command_topic": f"{macro_topic}/{key}",
            "payload_press": "1",
            "icon": icon,
            "device": DEVICE_BLOCK,
            **AVAILABILITY,
        }
        client.publish(
            f"{HA_DISCO_PREFIX}/button/{base_id}/btn_{key}/config",
            json.dumps(cfg), qos=1, retain=True,
        )

    log(f"HA discovery: {len(SENSORS) + 4} sensors + 1 select + {len(macros)} buttons")
