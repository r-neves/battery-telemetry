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
    args = ap.parse_args()

    if not os.path.exists(args.config):
        print(f"Config not found: {args.config}\n"
              f"Copy {os.path.join(HERE, 'config.example.json')} to config.json and edit it.",
              file=sys.stderr)
        return 1

    cfg = load_config(args.config)
    battery = read_battery()
    messages = build_messages(cfg, battery)

    if args.dry_run:
        print("Battery:", json.dumps(battery, indent=2))
        print(f"\n{len(messages)} MQTT messages -> {cfg['mqtt']['host']}:{cfg['mqtt']['port']}")
        for m in messages:
            print(f"  {m['topic']}  {m['payload']}")
        return 0

    mqtt = cfg["mqtt"]
    auth = None
    if mqtt.get("username"):
        auth = {"username": mqtt["username"], "password": mqtt.get("password", "")}
    tls = {} if mqtt.get("tls") else None

    publish.multiple(
        messages,
        hostname=mqtt["host"],
        port=int(mqtt["port"]),
        client_id=f"battery-telemetry-{cfg['device']['id']}",
        auth=auth,
        tls=tls,
    )
    print(f"Published battery ({battery.get('percent')}%, {battery.get('state')}) "
          f"to {mqtt['host']} as '{cfg['device']['name']}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
