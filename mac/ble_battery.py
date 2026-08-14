#!/usr/bin/env python3
"""Live BLE Battery Service reads via CoreBluetooth.

`system_profiler SPBluetoothDataType` reports battery levels from a cache
macOS refreshes only opportunistically. For BLE peripherals it often reads
the Battery Service once at connect and never again, so a device can sit
pinned at a stale value indefinitely (the Glove80 reporting 100% forever is
the canonical case), and sometimes it reports no battery at all.

This module talks to the Battery Service (0x180F, characteristic 0x2A19)
directly, which is always a live read. It only touches peripherals macOS is
*already* connected to and that already expose the service, so there is no
scan: a connect + read on an existing link takes well under a second.

Peripherals are keyed by name, because that is the only field CoreBluetooth
and system_profiler share -- CoreBluetooth exposes a per-Mac UUID, not the
Bluetooth address.

Requires `pyobjc-framework-CoreBluetooth` and Bluetooth permission (TCC) for
the process. Everything degrades to an empty result rather than raising, so
callers can always fall back to system_profiler.

The CoreBluetooth work happens in a child process (see read_ble_batteries),
because one of its failure modes is not an exception but SIGABRT.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

BATTERY_SERVICE = "180F"
BATTERY_LEVEL_CHAR = "2A19"

# CBManagerState
_POWERED_ON = 5
_UNAUTHORIZED = 3

_UNAVAILABLE_REASON: str | None = None

# Killed for touching CoreBluetooth without a usage description; see
# read_ble_batteries. Phrased for someone staring at a dry-run.
_TCC_ABORT = (
    "macOS killed the Bluetooth helper (no NSBluetoothAlwaysUsageDescription "
    "in Python's bundle). Live BLE reads work when launchd runs the job, not "
    "from a terminal"
)


def unavailable_reason() -> str | None:
    """Why the last read_ble_batteries() call returned nothing, if it did."""
    return _UNAVAILABLE_REASON


def read_ble_batteries(timeout: float = 6.0) -> dict[str, int]:
    """{peripheral name: battery %} for already-connected BLE peripherals.

    Runs the actual read in a child interpreter. macOS does not merely refuse
    a CoreBluetooth client whose bundle lacks NSBluetoothAlwaysUsageDescription
    -- TCC SIGABRTs it. Python's own bundle has no such key, and the check is
    charged to the *responsible* process, which for anything started from a
    shell is the terminal (Terminal.app and most others don't carry the key
    either). Run in-process, that abort would take the whole publish down
    rather than falling back to system_profiler; in a child it costs one
    interpreter start and an empty result.

    Returns {} (and sets unavailable_reason()) if CoreBluetooth is missing,
    Bluetooth is off, the read is refused, or the child dies.
    """
    global _UNAVAILABLE_REASON
    _UNAVAILABLE_REASON = None

    cmd = [sys.executable, os.path.abspath(__file__), "--json",
           "--timeout", str(timeout)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        _UNAVAILABLE_REASON = "Bluetooth helper did not finish in time"
        return {}
    except OSError as exc:
        _UNAVAILABLE_REASON = f"could not start Bluetooth helper ({exc})"
        return {}

    if proc.returncode < 0:
        sig = -proc.returncode
        _UNAVAILABLE_REASON = (
            _TCC_ABORT if sig == signal.SIGABRT
            else f"Bluetooth helper killed by signal {sig}"
        )
        return {}

    try:
        payload = json.loads(proc.stdout)
        values = {str(k): int(v) for k, v in payload["values"].items()}
    except (ValueError, KeyError, TypeError):
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        _UNAVAILABLE_REASON = (
            f"Bluetooth helper failed ({detail[-1] if detail else 'no output'})"
        )
        return {}

    _UNAVAILABLE_REASON = payload.get("reason")
    return values


def _read_ble_batteries_here(timeout: float = 6.0) -> dict[str, int]:
    """read_ble_batteries' actual body -- only ever run in the child."""
    global _UNAVAILABLE_REASON
    _UNAVAILABLE_REASON = None

    try:
        from CoreBluetooth import CBCentralManager, CBUUID
        from Foundation import NSObject, NSRunLoop, NSDate
    except ImportError as exc:
        _UNAVAILABLE_REASON = f"pyobjc CoreBluetooth unavailable ({exc})"
        return {}

    service_uuid = CBUUID.UUIDWithString_(BATTERY_SERVICE)
    level_uuid = CBUUID.UUIDWithString_(BATTERY_LEVEL_CHAR)

    results: dict[str, int] = {}
    pending: set[str] = set()
    state_error: list[str] = []
    # Truthy once the state callback has run and enumerated peripherals, which
    # separates "nothing connected exposes a Battery Service" from "the state
    # callback never fired at all" -- both otherwise look like an empty result.
    enumerated: list[bool] = []
    # CoreBluetooth does not retain peripherals for us; without a strong
    # reference here they can be collected mid-connect.
    keepalive: list = []

    class _Delegate(NSObject):
        def centralManagerDidUpdateState_(self, central):
            state = central.state()
            if state == _UNAUTHORIZED:
                state_error.append(
                    "Bluetooth permission denied for this process "
                    "(System Settings > Privacy & Security > Bluetooth)"
                )
                return
            if state != _POWERED_ON:
                state_error.append(f"Bluetooth not powered on (state={state})")
                return

            for p in central.retrieveConnectedPeripheralsWithServices_([service_uuid]):
                keepalive.append(p)
                pending.add(p.identifier().UUIDString())
                p.setDelegate_(self)
                central.connectPeripheral_options_(p, None)
            enumerated.append(True)

        def centralManager_didConnectPeripheral_(self, central, peripheral):
            peripheral.discoverServices_([service_uuid])

        def centralManager_didFailToConnectPeripheral_error_(self, central, peripheral, error):
            pending.discard(peripheral.identifier().UUIDString())

        def centralManager_didDisconnectPeripheral_error_(self, central, peripheral, error):
            pending.discard(peripheral.identifier().UUIDString())

        def peripheral_didDiscoverServices_(self, peripheral, error):
            if error:
                pending.discard(peripheral.identifier().UUIDString())
                return
            for service in peripheral.services() or []:
                peripheral.discoverCharacteristics_forService_([level_uuid], service)

        def peripheral_didDiscoverCharacteristicsForService_error_(self, peripheral, service, error):
            if error:
                pending.discard(peripheral.identifier().UUIDString())
                return
            for char in service.characteristics() or []:
                peripheral.readValueForCharacteristic_(char)

        def peripheral_didUpdateValueForCharacteristic_error_(self, peripheral, char, error):
            pending.discard(peripheral.identifier().UUIDString())
            if error:
                return
            data = bytes(char.value() or b"")
            name = peripheral.name()
            # 0x2A19 is a single uint8 percentage.
            if data and name and 0 <= data[0] <= 100:
                results[str(name)] = data[0]

    delegate = _Delegate.alloc().init()
    manager = CBCentralManager.alloc().initWithDelegate_queue_(delegate, None)

    loop = NSRunLoop.currentRunLoop()
    deadline = time.time() + timeout
    while time.time() < deadline:
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        if state_error:
            break
        # Every peripheral answered -- or there were none to begin with, in
        # which case there is nothing to wait out.
        if enumerated and not pending:
            break

    for p in keepalive:
        manager.cancelPeripheralConnection_(p)

    if state_error:
        _UNAVAILABLE_REASON = state_error[0]
    elif not enumerated:
        _UNAVAILABLE_REASON = (
            "CoreBluetooth never reported its state "
            "(a Bluetooth permission prompt may be waiting)"
        )
    elif not results and pending:
        _UNAVAILABLE_REASON = "timed out reading Battery Service"
    return results


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Live BLE Battery Service reads.")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="Seconds to wait for peripherals to answer.")
    ap.add_argument("--json", action="store_true",
                    help="Do the CoreBluetooth read here and print "
                         "{values, reason} as JSON. Used by the parent process; "
                         "run without it to read via that safety net.")
    args = ap.parse_args(argv)

    if args.json:
        values = _read_ble_batteries_here(args.timeout)
        print(json.dumps({"values": values, "reason": unavailable_reason()}))
        return 0

    values = read_ble_batteries(args.timeout)
    print(json.dumps(values, indent=2))
    if not values:
        print("no readings:", unavailable_reason())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
