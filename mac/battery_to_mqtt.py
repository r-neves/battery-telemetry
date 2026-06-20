#!/usr/bin/env python3
"""Publish this Mac's battery status to Home Assistant over MQTT.

Single-shot: reads the battery once, publishes MQTT discovery + state
(both retained), then exits. Run it on a schedule with launchd (see the
plist next to this file). HA marks the sensors unavailable automatically
via `expire_after` if updates stop arriving.

Data comes from `pmset -g batt` (percentage, charge state, time remaining)
and `ioreg` (cycle count, health, temperature).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

import paho.mqtt.client as mqtt_client
import paho.mqtt.publish as publish

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = json.load(f)

    mqtt = cfg.setdefault("mqtt", {})
    mqtt.setdefault("host", "homeassistant.local")
    mqtt.setdefault("port", 1883)
    mqtt.setdefault("tls", False)

    dev = cfg.setdefault("device", {})
    host = socket.gethostname().split(".")[0]
    dev.setdefault("name", host)
    # entity/object ids must be [a-z0-9_]
    default_id = re.sub(r"[^a-z0-9_]", "_", dev["name"].lower())
    dev.setdefault("id", default_id)

    cfg.setdefault("discovery_prefix", "homeassistant")
    cfg.setdefault("interval_seconds", 60)
    cfg.setdefault("bluetooth", True)  # also publish connected BT peripherals
    return cfg


# --------------------------------------------------------------------------- #
# Battery reading
# --------------------------------------------------------------------------- #
def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def read_pmset() -> dict:
    """Percentage, charge state, AC power, minutes remaining."""
    out = _run(["pmset", "-g", "batt"])
    data: dict = {}

    data["ac_power"] = "AC Power" in out.splitlines()[0]

    m = re.search(r"(\d+)%", out)
    if m:
        data["percent"] = int(m.group(1))

    # e.g. "68%; discharging; 5:57 remaining"
    m = re.search(r"%;\s*([^;]+);", out)
    if m:
        data["state"] = m.group(1).strip()  # charging / discharging / charged / ...

    m = re.search(r"(\d+):(\d+)\s+remaining", out)
    if m:
        data["minutes_remaining"] = int(m.group(1)) * 60 + int(m.group(2))
    else:
        data["minutes_remaining"] = None  # "(no estimate)" / calculating

    return data


def read_ioreg() -> dict:
    """Cycle count, health %, temperature, charging flag."""
    try:
        out = _run(["ioreg", "-rn", "AppleSmartBattery"])
    except subprocess.CalledProcessError:
        return {}

    def num(key: str):
        m = re.search(rf'"{key}"\s*=\s*(-?\d+)', out)
        return int(m.group(1)) if m else None

    def yesno(key: str):
        m = re.search(rf'"{key}"\s*=\s*(Yes|No)', out)
        return None if not m else m.group(1) == "Yes"

    data: dict = {}
    data["cycle_count"] = num("CycleCount")
    data["health_percent"] = num("MaxCapacity")  # Apple Silicon reports this as a %
    data["is_charging"] = yesno("IsCharging")
    temp = num("Temperature")
    if temp is not None:
        data["temperature_c"] = round(temp / 100.0, 1)  # 1/100 °C
    return data


def read_battery() -> dict:
    data = read_pmset()
    data.update(read_ioreg())
    return data


# --------------------------------------------------------------------------- #
# Bluetooth peripherals (AirPods, mice, keyboards, …)
# --------------------------------------------------------------------------- #
# system_profiler key suffix -> (state key, friendly sensor name)
BT_BATTERY_SUFFIX = {
    "": ("level", "Battery"),
    "Main": ("level", "Battery"),
    "Left": ("left", "Left"),
    "Right": ("right", "Right"),
    "Case": ("case", "Case"),
}


def _parse_pct(value) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"(\d+)", str(value))
    return int(m.group(1)) if m else None


def read_bluetooth() -> list[dict]:
    """Currently-connected Bluetooth devices that report a battery level."""
    try:
        out = _run(["system_profiler", "SPBluetoothDataType", "-json"])
        data = json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []

    devices: list[dict] = []
    for controller in data.get("SPBluetoothDataType", []):
        for entry in controller.get("device_connected", []):
            for name, props in entry.items():
                if not isinstance(props, dict):
                    continue
                batteries: dict[str, int] = {}
                for k, v in props.items():
                    if not k.startswith("device_batteryLevel"):
                        continue
                    key, _ = BT_BATTERY_SUFFIX.get(k[len("device_batteryLevel"):], (None, None))
                    pct = _parse_pct(v)
                    if key and pct is not None:
                        batteries[key] = pct
                if not batteries:
                    continue  # connected but no battery reported (skip)
                addr = props.get("device_address", "")
                devices.append({
                    "name": name.strip(),
                    "address": addr,
                    "id": re.sub(r"[^a-z0-9]", "", addr.lower()) or re.sub(r"[^a-z0-9]", "", name.lower()),
                    "minor_type": props.get("device_minorType"),
                    "batteries": batteries,
                })
    return devices


# --------------------------------------------------------------------------- #
# Sensor definitions (HA MQTT discovery)
# --------------------------------------------------------------------------- #
# key -> (Friendly Name, device_class, unit, icon)
SENSORS = {
    "percent":           ("Battery Level",    "battery",     "%",   None),
    "state":             ("Charging State",   None,          None,  "mdi:battery-charging"),
    "ac_power":          ("Power Adapter",    None,          None,  "mdi:power-plug"),
    "minutes_remaining": ("Time Remaining",   "duration",    "min", "mdi:timer-sand"),
    "cycle_count":       ("Cycle Count",      None,          None,  "mdi:counter"),
    "health_percent":    ("Battery Health",   None,          "%",   "mdi:heart-pulse"),
    "temperature_c":     ("Battery Temperature", "temperature", "°C", None),
}


def build_messages(cfg: dict, battery: dict) -> list[dict]:
    dev_id = cfg["device"]["id"]
    dev_name = cfg["device"]["name"]
    prefix = cfg["discovery_prefix"]
    state_topic = f"battery-telemetry/{dev_id}/state"
    expire = max(60, int(cfg["interval_seconds"]) * 3)

    device_block = {
        "identifiers": [f"battery_telemetry_{dev_id}"],
        "name": dev_name,
        "model": "macOS",
        "manufacturer": "Apple",
    }

    messages: list[dict] = []

    # One retained discovery config per sensor.
    for key, (name, dev_class, unit, icon) in SENSORS.items():
        cfg_topic = f"{prefix}/sensor/{dev_id}/{key}/config"
        payload = {
            "name": name,
            "unique_id": f"battery_telemetry_{dev_id}_{key}",
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "device": device_block,
            "expire_after": expire,
        }
        # ac_power is really a binary, but as a sensor it shows on/off text fine;
        # keep it simple and uniform here.
        if dev_class:
            payload["device_class"] = dev_class
        if unit:
            payload["unit_of_measurement"] = unit
        if icon:
            payload["icon"] = icon
        # Don't let unavailable (null) values blow up numeric sensors.
        if unit or dev_class in ("battery", "temperature", "duration"):
            payload["value_template"] = (
                f"{{{{ value_json.{key} if value_json.{key} is not none else 'unknown' }}}}"
            )
        messages.append({"topic": cfg_topic, "payload": json.dumps(payload), "retain": True})

    # Single retained state message with everything.
    messages.append(
        {"topic": state_topic, "payload": json.dumps(battery), "retain": True}
    )
    return messages


def build_bluetooth_messages(cfg: dict, devices: list[dict]) -> list[dict]:
    """One shared, host-independent HA device per peripheral.

    Identity is keyed only on the peripheral's Bluetooth address — discovery
    topic, unique_id, device identifiers and state topic all omit the
    publishing Mac. So several Macs running this script publish to the *exact
    same* retained topics: it shows up once in HA, last-writer-wins. A
    `source` attribute records which Mac last reported the value.
    """
    prefix = cfg["discovery_prefix"]
    source = cfg["device"]["name"]
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    messages: list[dict] = []
    for dev in devices:
        bt_id = dev["id"]
        state_topic = f"battery-telemetry/bluetooth/{bt_id}/state"

        device_block = {
            "identifiers": [f"battery_telemetry_bt_{bt_id}"],
            "name": dev["name"],
        }
        if dev.get("address"):
            device_block["connections"] = [["mac", dev["address"]]]
        if dev.get("minor_type"):
            device_block["model"] = dev["minor_type"]

        for key in dev["batteries"]:
            friendly = dict(BT_BATTERY_SUFFIX.values()).get(key, key.title())
            cfg_topic = f"{prefix}/sensor/bt_{bt_id}/{key}/config"
            payload = {
                "name": friendly,
                "unique_id": f"battery_telemetry_bt_{bt_id}_{key}",
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.{key} if value_json.{key} is not none else 'unknown' }}}}",
                "json_attributes_topic": state_topic,
                "json_attributes_template": "{{ {'source': value_json.source, 'last_seen': value_json.last_seen} | tojson }}",
                "device_class": "battery",
                "unit_of_measurement": "%",
                "device": device_block,
                # No expire_after: peripherals keep showing their last-seen
                # value (from the retained state) after they disconnect.
                # `last_seen` tells you how stale it is.
            }
            messages.append({"topic": cfg_topic, "payload": json.dumps(payload), "retain": True})

        state = dict(dev["batteries"])
        state["source"] = source
        state["last_seen"] = now_str
        messages.append(
            {"topic": state_topic, "payload": json.dumps(state), "retain": True}
        )
    return messages


# --------------------------------------------------------------------------- #
# MQTT helpers
# --------------------------------------------------------------------------- #
def _mqtt_auth_tls(cfg: dict):
    mqtt = cfg["mqtt"]
    auth = None
    if mqtt.get("username"):
        auth = {"username": mqtt["username"], "password": mqtt.get("password", "")}
    tls = {} if mqtt.get("tls") else None
    return auth, tls


def prune_legacy_bluetooth(cfg: dict) -> int:
    """Clear retained discovery/state left by the old per-Mac Bluetooth scheme.

    Before peripherals became host-independent, this Mac published discovery to
    `<prefix>/sensor/<mac_id>_bt_<addr>/<key>/config` and state to
    `battery-telemetry/<mac_id>/bluetooth/<addr>/state`. Those retained
    messages reuse the new `unique_id`s, so HA rejects the new (shared) configs
    until they're gone. This subscribes, finds every such retained topic, and
    publishes an empty retained payload to delete it.
    """
    mac = cfg["mqtt"]
    mac_id = cfg["device"]["id"]
    prefix = cfg["discovery_prefix"]
    legacy_node = f"{mac_id}_bt_"

    found: set[str] = set()
    settled = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe(f"{prefix}/sensor/+/+/config")
        client.subscribe("battery-telemetry/+/bluetooth/+/state")

    def on_message(client, userdata, msg):
        if not msg.retain or not msg.payload:
            return  # only delete non-empty retained messages
        parts = msg.topic.split("/")
        # legacy discovery: <prefix>/sensor/<mac_id>_bt_<addr>/<key>/config
        is_cfg = (msg.topic.endswith("/config") and len(parts) >= 4
                  and parts[1] == "sensor" and parts[2].startswith(legacy_node))
        # legacy state: battery-telemetry/<mac_id>/bluetooth/<addr>/state
        is_state = (len(parts) == 5 and parts[0] == "battery-telemetry"
                    and parts[1] == mac_id and parts[2] == "bluetooth")
        if is_cfg or is_state:
            found.add(msg.topic)

    client = mqtt_client.Client(
        callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
        client_id=f"battery-telemetry-prune-{mac_id}",
    )
    auth, tls = _mqtt_auth_tls(cfg)
    if auth:
        client.username_pw_set(auth["username"], auth["password"])
    if tls is not None:
        client.tls_set()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(mac["host"], int(mac["port"]))
    client.loop_start()
    try:
        time.sleep(2.0)  # let retained messages flush in
        # Publish the deletions while the network loop is still running,
        # otherwise wait_for_publish() blocks forever.
        for topic in sorted(found):
            info = client.publish(topic, payload=b"", retain=True, qos=1)
            info.wait_for_publish(timeout=5)
            print(f"  cleared {topic}")
    finally:
        client.loop_stop()
        client.disconnect()

    print(f"Pruned {len(found)} legacy Bluetooth topic(s) for '{cfg['device']['name']}'.")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--config",
        default=os.environ.get("BATTERY_TELEMETRY_CONFIG", os.path.join(HERE, "config.json")),
        help="Path to config.json (default: alongside this script).",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print what would be published; don't connect.")
    ap.add_argument("--prune-legacy-bt", action="store_true",
                    help="Clear retained discovery/state from the old per-Mac Bluetooth scheme, then exit.")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        print(f"Config not found: {args.config}\n"
              f"Copy {os.path.join(HERE, 'config.example.json')} to config.json and edit it.",
              file=sys.stderr)
        return 1

    cfg = load_config(args.config)

    if args.prune_legacy_bt:
        return prune_legacy_bluetooth(cfg)

    battery = read_battery()
    messages = build_messages(cfg, battery)

    bt_devices = read_bluetooth() if cfg.get("bluetooth", True) else []
    messages += build_bluetooth_messages(cfg, bt_devices)

    if args.dry_run:
        print("Battery:", json.dumps(battery, indent=2))
        print("Bluetooth:", json.dumps(
            [{d["name"]: d["batteries"]} for d in bt_devices], ensure_ascii=False))
        print(f"\n{len(messages)} MQTT messages -> {cfg['mqtt']['host']}:{cfg['mqtt']['port']}")
        for m in messages:
            print(f"  {m['topic']}  {m['payload']}")
        return 0

    mqtt = cfg["mqtt"]
    auth, tls = _mqtt_auth_tls(cfg)

    publish.multiple(
        messages,
        hostname=mqtt["host"],
        port=int(mqtt["port"]),
        client_id=f"battery-telemetry-{cfg['device']['id']}",
        auth=auth,
        tls=tls,
    )
    bt_summary = ", ".join(d["name"] for d in bt_devices) or "none"
    print(f"Published battery ({battery.get('percent')}%, {battery.get('state')}) "
          f"to {mqtt['host']} as '{cfg['device']['name']}'. "
          f"Bluetooth devices: {bt_summary}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
