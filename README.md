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

It also publishes the battery of **connected Bluetooth peripherals**
(AirPods, mice, keyboards…) read from `system_profiler SPBluetoothDataType`.
AirPods get Left/Right/Case sensors; mice/keyboards get a single Battery.
Only devices that are *currently connected and report a battery* appear —
when one disconnects, its sensors go unavailable (via `expire_after`) and
the device stays in HA. Set `"bluetooth": false` in `config.json` to disable.

**Bluetooth devices are host-independent.** Their MQTT topics, `unique_id`
and HA device identity are keyed only on the peripheral's Bluetooth address —
*not* on the publishing Mac. So if you run this on several Macs, a peripheral
shows up **once** in HA (no duplicates, no `unique_id` collisions), with
last-writer-wins: whichever Mac most recently saw it connected sets the
value. Each battery sensor carries a `source` attribute naming that Mac.

### Setup

Run everything from the repo root via the `Makefile` (`make help` lists all
targets):

```bash
make venv            # 1. create .venv and install deps
make config          # 2. copy config.example.json -> mac/config.json (git-ignored)
$EDITOR mac/config.json   #    set your MQTT broker host/credentials
make dry-run         # 3. print what would be published (no connection)
make run             # 4. publish once
make install         # 5. install + start the launchd job (every interval_seconds)
```

After step 4 or 5 the device appears in Home Assistant under
**Settings → Devices & Services → MQTT**.

(Equivalent raw commands, if you prefer: `python3 -m venv .venv &&
.venv/bin/pip install -r requirements.txt`, then
`.venv/bin/python mac/battery_to_mqtt.py [--dry-run]` and `mac/install.sh`.)

### Config (`mac/config.json`)

```json
{
  "mqtt": {
    "host": "IP_ADDRESS_OR_HOSTNAME",
    "port": 1883,
    "username": "CHANGE_ME",
    "password": "CHANGE_ME",
    "tls": false
  },
  "device": { "name": "My MacBook Pro", "id": "my_macbook_pro" },
  "discovery_prefix": "homeassistant",
  "interval_seconds": 60,
  "bluetooth": true
}
```

- `device.id` must be `[a-z0-9_]`; it keys the MQTT topics and HA unique IDs.
- `bluetooth` (default `true`) also publishes connected Bluetooth peripherals.
- `interval_seconds` controls both the launchd schedule and the sensor
  `expire_after` (= 3× interval).
- Leave `username` empty for an anonymous broker. Set `"tls": true` for 8883.

### Managing the launchd job

```bash
make install     # (re)install + start
make start       # trigger one run now
make status      # is the job loaded?
make logs        # tail mac/battery-telemetry.log
make uninstall   # stop + remove the job
```

### Migrating Bluetooth from an older version

Earlier versions namespaced Bluetooth topics per Mac
(`…/sensor/<mac_id>_bt_<addr>/…`). The current host-independent layout reuses
the same `unique_id`s, so those stale retained messages make HA reject the new
(shared) device until they're cleared. On **each Mac** that ran the old
version, once:

```bash
make prune-legacy-bt   # delete the old per-Mac retained discovery/state
make run               # republish the new shared device
```

If HA still shows the old (now-stale) peripheral, delete it once in the UI —
it won't come back once the retained config is gone. This is a one-time step;
fresh installs can ignore it.
