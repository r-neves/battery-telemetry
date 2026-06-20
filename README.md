# battery-telemetry

Publish battery info from several devices (Mac, iPhone, Android) to Home
Assistant over MQTT, with [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
so each device shows up automatically with its sensors.

| Device  | Status        | Location |
| ------- | ------------- | -------- |
| Mac     | ✅ done        | [`mac/`](mac/) |
| iPhone  | planned       | —        |
| Android | planned       | —        |

## Mac

Reads the battery from `pmset -g batt` (level, charge state, time remaining)
and `ioreg` (cycle count, health, temperature), then publishes to MQTT.
It's **single-shot** — read once, publish, exit — and is run on an interval
by **launchd**. Home Assistant marks the sensors unavailable automatically
(via `expire_after`) if updates stop.

Sensors created: Battery Level, Charging State, Power Adapter, Time
Remaining, Cycle Count, Battery Health, Battery Temperature.

### Setup

```bash
# 1. From the repo root, create the venv and install deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Configure (host/credentials of your MQTT broker)
cp mac/config.example.json mac/config.json
$EDITOR mac/config.json        # config.json is git-ignored

# 3. Try it once — prints what it would publish, doesn't connect
.venv/bin/python mac/battery_to_mqtt.py --dry-run

# 4. Publish for real, once
.venv/bin/python mac/battery_to_mqtt.py

# 5. Install the launchd job (runs every interval_seconds from config.json)
mac/install.sh
```

After step 4 or 5 the device appears in Home Assistant under
**Settings → Devices & Services → MQTT**.

### Config (`mac/config.json`)

```json
{
  "mqtt": {
    "host": "homeassistant.local",
    "port": 1883,
    "username": "CHANGE_ME",
    "password": "CHANGE_ME",
    "tls": false
  },
  "device": { "name": "Rodrigo's MacBook Pro", "id": "rodrigos_macbook_pro" },
  "discovery_prefix": "homeassistant",
  "interval_seconds": 60
}
```

- `device.id` must be `[a-z0-9_]`; it keys the MQTT topics and HA unique IDs.
- `interval_seconds` controls both the launchd schedule and the sensor
  `expire_after` (= 3× interval).
- Leave `username` empty for an anonymous broker. Set `"tls": true` for 8883.

### Managing the launchd job

```bash
mac/install.sh                                       # (re)install + start
launchctl kickstart -k gui/$(id -u)/com.battery-telemetry.mac   # run now
tail -f mac/battery-telemetry.log                    # watch output
launchctl bootout gui/$(id -u)/com.battery-telemetry.mac \
  && rm ~/Library/LaunchAgents/com.battery-telemetry.mac.plist  # uninstall
```
