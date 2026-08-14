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
(AirPods, mice, keyboards…). AirPods get Left/Right/Case sensors;
mice/keyboards get a single Battery. Only devices that are *currently
connected and report a battery* appear — when one disconnects it keeps
showing its last reading, and the `last_seen` attribute tells you how stale
that is. Set `"bluetooth": false` in `config.json` to disable.

**Levels are read live over BLE where possible.** `system_profiler
SPBluetoothDataType` serves a cache that macOS refreshes only
opportunistically: for a BLE peripheral it often reads the Battery Service
once at connect and never again, so a device can sit pinned at a wrong value
indefinitely (a Glove80 stuck at 100% is the classic symptom), and sometimes
it reports no battery at all. So for any peripheral exposing the BLE Battery
Service, [`mac/ble_battery.py`](mac/ble_battery.py) reads characteristic
`0x2A19` directly via CoreBluetooth — always a live value. It only touches
peripherals macOS is *already* connected to, so there's no scan and it costs
well under a second. `system_profiler` remains the fallback, and is still the
only source for classic-Bluetooth accessories like AirPods, which have no
GATT Battery Service. Each sensor's `battery_source` attribute records which
path produced the value.

**Bluetooth devices are host-independent.** Their MQTT topics, `unique_id`
and HA device identity are keyed on the peripheral's *name* — not on the
publishing Mac, and deliberately **not** on its Bluetooth address. AirPods
(and an AirPods case broadcasting on its own) rotate their address for
privacy, and different Macs observe different addresses for the same
accessory, so an address-keyed identity minted a brand-new HA device on every
rotation and abandoned the last one. The name is stable across rotations,
reboots and Macs (it syncs via iCloud). So if you run this on several Macs, a
peripheral shows up **once** in HA, last-writer-wins: whichever Mac most
recently saw it connected sets the value. Each battery sensor carries a
`source` attribute naming that Mac and an `address` attribute with the
address last observed.

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

### Clearing duplicate Bluetooth devices

If HA shows a pile of inactive duplicates of the same peripheral — typically
AirPods `Case`/`Left`/`Right` entities, one set per rotated address — those
were created by the old address-keyed identity. Clear them **once**, from any
one Mac (this step is host-independent):

```bash
make prune-orphan-bt-dry   # preview exactly which retained topics go
make prune-orphan-bt       # delete them
make run                   # republish under the name-based identity
```

It only ever matches nodes whose id is a bare 12-hex-digit address, which the
name-based ids can't collide with. Any duplicate HA devices left in the UI
afterwards can be deleted there; they won't come back.

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
