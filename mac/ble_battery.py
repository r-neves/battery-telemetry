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
"""
from __future__ import annotations

import time

BATTERY_SERVICE = "180F"
BATTERY_LEVEL_CHAR = "2A19"

# CBManagerState
_POWERED_ON = 5
_UNAUTHORIZED = 3

_UNAVAILABLE_REASON: str | None = None


def unavailable_reason() -> str | None:
    """Why the last read_ble_batteries() call returned nothing, if it did."""
    return _UNAVAILABLE_REASON


def read_ble_batteries(timeout: float = 6.0) -> dict[str, int]:
    """{peripheral name: battery %} for already-connected BLE peripherals.

    Returns {} (and sets unavailable_reason()) if CoreBluetooth is missing,
    Bluetooth is off, or the process lacks Bluetooth permission.
    """
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
    started = False
    while time.time() < deadline:
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        if state_error:
            break
        started = started or bool(pending)
        if started and not pending:
            break

    for p in keepalive:
        manager.cancelPeripheralConnection_(p)

    if state_error:
        _UNAVAILABLE_REASON = state_error[0]
    elif not results and pending:
        _UNAVAILABLE_REASON = "timed out reading Battery Service"
    return results


if __name__ == "__main__":
    import json

    values = read_ble_batteries()
    print(json.dumps(values, indent=2))
    if not values:
        print("no readings:", unavailable_reason())
