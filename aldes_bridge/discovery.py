"""Home Assistant MQTT discovery."""
import json
from .config import (
    CLOCK_TOPIC, CMD_RESULT_TOPIC, DEVICE_MAC, DEVICE_MODEL, DEVICE_NAME, EVENTS_TOPIC, HA_DISCO_PREFIX,
    HOLIDAY_TOPIC, LAST_SEEN_TOPIC, PREFIX, SENSOR_EXPIRE_AFTER, STATUS_TOPIC,
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
    # Air quality probes are optional: 0 when the unit has none. Disabled by default
    # (HA applies this only when it first creates the entity), enable them if you have them.
    ("co2",                 "CO₂",                   "Co2_vmc",     {"device_class": "carbon_dioxide", "unit_of_measurement": "ppm", "state_class": "measurement", "enabled_by_default": False}),
    ("pm25",                "PM2.5",                 "Pm25_Vmc",    {"device_class": "pm25", "unit_of_measurement": "µg/m³", "state_class": "measurement", "enabled_by_default": False}),
    ("voc",                 "COV",                   "Voc_Vmc",     {"icon": "mdi:molecule", "state_class": "measurement", "enabled_by_default": False}),
    # Absent from the frames of some boxes, "--%%" in others: disabled by default.
    ("rssi",                "Signal WiFi",           "RSSI",        {"icon": "mdi:wifi", "entity_category": "diagnostic", "enabled_by_default": False}),
    # Vers_W is the WiFi firmware build date as a Unix timestamp (e.g. 1544745600.0 =
    # 2018-12-14) on recent boxes, a plain version string ("21") on others.
    ("fw_wifi",             "Firmware WiFi",         "Vers_W",      {"icon": "mdi:chip", "entity_category": "diagnostic", "value_template": (
        "{% set v = value_json.Vers_W | default(none) %}"
        "{% if v is number and v > 1000000000 %}{{ v | int | timestamp_custom('%Y-%m-%d', false) }}"
        "{% else %}{{ v }}{% endif %}")}),
    ("fw_uc",               "Firmware MCU",          "Vers_UC",     {"icon": "mdi:chip", "entity_category": "diagnostic"}),
]

# Telemetry fields whose meaning is not known yet, exposed raw (diagnostic, disabled by
# default) so they can be compared with real observations and named later. The guesses
# come from the field names only.
UNKNOWN_FIELDS = [
    ("ff_cpt",  "maybe a filter counter"),
    ("dep_ind", ""),
    ("dtb_ind", "maybe the bypass state"),
    ("cve_csn", "maybe the extract fan consumption"),
    ("vi_csn",  "maybe the supply fan consumption"),
    ("tte_csn", "maybe a total consumption"),
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

    # Unknown fields (see UNKNOWN_FIELDS)
    for key, _hint in UNKNOWN_FIELDS:
        client.publish(
            f"{HA_DISCO_PREFIX}/sensor/{base_id}/raw_{key}/config",
            json.dumps({
                "name": f"Champ {key} (inconnu)",
                "unique_id": f"{base_id}_raw_{key}",
                "state_topic": EVENTS_TOPIC,
                "value_template": f"{{{{ value_json.{key} | default(none) }}}}",
                "state_class": "measurement",
                "icon": "mdi:help-circle-outline",
                "entity_category": "diagnostic",
                "enabled_by_default": False,
                "device": DEVICE_BLOCK,
                **AVAILABILITY,
            }),
            qos=1, retain=True,
        )

    # Holiday start/end, published by the bridge on HOLIDAY_TOPIC when a holiday command
    # is confirmed: a real box leaves Start_date_mode / End_date_mode empty, even in W.
    for obj_id, name, key in (("holiday_start", "Début des vacances", "start"),
                              ("holiday_end", "Fin des vacances", "end")):
        tpl = "{{ value_json." + key + " | default(None) }}"
        client.publish(
            f"{HA_DISCO_PREFIX}/sensor/{base_id}/{obj_id}/config",
            json.dumps({
                "name": name,
                "unique_id": f"{base_id}_{obj_id}",
                "state_topic": HOLIDAY_TOPIC,
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

    # Box clock (from its SAS token, see clock.py). device_class problem: "Problème" while
    # the clock is not set (holidays and the schedule cannot work), "OK" otherwise.
    client.publish(
        f"{HA_DISCO_PREFIX}/binary_sensor/{base_id}/box_clock/config",
        json.dumps({
            "name": "Horloge de la box",
            "unique_id": f"{base_id}_box_clock",
            "state_topic": CLOCK_TOPIC,
            "value_template": "{{ 'OFF' if value_json.synced else 'ON' }}",
            "json_attributes_topic": CLOCK_TOPIC,
            "device_class": "problem",
            "entity_category": "diagnostic",
            "icon": "mdi:clock-alert-outline",
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

    n_sensors = len(SENSORS) + len(UNKNOWN_FIELDS) + 4
    log(f"HA discovery: {n_sensors} sensors + 1 binary sensor + 1 select + {len(macros)} buttons")
