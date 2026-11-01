#!/usr/bin/python3
"""EIZO FlexScan USB brightness helper for Linux (Python standard library only).

Protocol references: markbolhuis/libeizo and Linux's hidraw documentation.
The USB descriptor supplies report IDs and brightness limits; no IDs for monitor
settings other than brightness are exposed to callers.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import struct
import sys
import time


BRIGHTNESS = 0x00820010
CHANNELS = {
    "descriptor": (0xFF300001, 517),
    "set": (0xFF300002, 39),
    "get": (0xFF300003, 39),
    "counter": (0xFF300006, 3),
    "verify": (0xFF300007, 8),
    "identity": (0xFF300008, 25),
}


class MonitorError(Exception):
    """A device, protocol, or permission error suitable for display in the UI."""


@dataclass(frozen=True)
class Feature:
    usage: int
    report_id: int
    bits: int
    count: int
    minimum: int
    maximum: int
    offset: int = 0

    @property
    def length(self) -> int:
        return 1 + (self.bits * self.count + 7) // 8


def parse_features(data: bytes, *, usages: set[int] | None = None) -> dict[int, Feature]:
    """Read HID short items and require uniqueness only for requested usages.

    EV3240X repeats some unrelated vendor usages across different reports.
    Keep parsing every field for offsets, but only select the controls we use.
    """
    state = dict(page=0, report=0, bits=0, count=0, minimum=0, maximum=0)
    stack = []
    local_usages = []
    offsets = {}
    features = {}
    position = 0
    while position < len(data):
        prefix = data[position]
        position += 1
        if prefix == 0xFE:
            raise MonitorError("The HID descriptor contains an unsupported long item.")
        size = (0, 1, 2, 4)[prefix & 3]
        if position + size > len(data):
            raise MonitorError("The HID descriptor is truncated.")
        raw = data[position:position + size]
        position += size
        value = int.from_bytes(raw, "little")
        item_type, tag = (prefix >> 2) & 3, prefix >> 4
        if item_type == 1:
            key = {0: "page", 7: "bits", 8: "report", 9: "count"}.get(tag)
            if key:
                state[key] = value
            elif tag == 1:
                state["minimum"] = int.from_bytes(raw, "little", signed=True)
            elif tag == 2:
                state["maximum"] = int.from_bytes(
                    raw, "little", signed=state["minimum"] < 0)
            elif tag == 10:
                stack.append(state.copy())
            elif tag == 11:
                if not stack:
                    raise MonitorError("The HID descriptor has an invalid item stack.")
                state = stack.pop()
        elif item_type == 2 and tag == 0:
            local_usages.append(value if size == 4 else (state["page"] << 16) | value)
        elif item_type == 0:
            if tag in (8, 9, 11):
                key = (tag, state["report"])
                offset = offsets.get(key, 0)
                if tag == 11 and not value & 1 and len(local_usages) == 1:
                    usage = local_usages[0]
                    if usages is None or usage in usages:
                        if usage in features:
                            raise MonitorError(
                                f"The HID descriptor has a duplicate control (usage {usage:08x}).")
                        features[usage] = Feature(
                            usage, state["report"], state["bits"], state["count"],
                            state["minimum"], state["maximum"], offset)
                offsets[key] = offset + state["bits"] * state["count"]
            local_usages.clear()
    return features


@dataclass(frozen=True)
class Device:
    node: str
    sys_path: Path
    product_id: int


def channels_from_descriptor(data: bytes) -> dict[str, int]:
    features = parse_features(data, usages={usage for usage, _length in CHANNELS.values()})
    channels = {}
    for name, (usage, length) in CHANNELS.items():
        feature = features.get(usage)
        if (feature is None or feature.length != length or feature.offset != 0
                or not 1 <= feature.report_id <= 255):
            raise MonitorError("This EIZO device uses an unsupported USB protocol.")
        channels[name] = feature.report_id
    return channels


def discover(sys_root: Path = Path("/sys/class/hidraw")) -> list[Device]:
    devices = []
    for entry in sorted(sys_root.glob("hidraw*")):
        device_path = entry / "device"
        try:
            fields = dict(line.split("=", 1) for line in
                          (device_path / "uevent").read_text().splitlines() if "=" in line)
            bus, vendor, product = (int(part, 16) for part in fields["HID_ID"].split(":"))
            if bus != 3 or vendor != 0x056D:
                continue
            channels_from_descriptor((device_path / "report_descriptor").read_bytes())
            devices.append(Device(f"/dev/{entry.name}", device_path, product))
        except (OSError, KeyError, ValueError, MonitorError):
            continue
    return devices


def feature_ioctl(fd: int, report_id: int, length: int,
                  payload: bytes | None = None) -> bytes:
    """HIDIOCGFEATURE / HIDIOCSFEATURE on Linux's asm-generic ioctl ABI."""
    buffer = bytearray(length)
    buffer[0] = report_id
    if payload is not None:
        if len(payload) != length - 1:
            raise MonitorError("The USB report has an invalid length.")
        buffer[1:] = payload
    number = 0x06 if payload is not None else 0x07
    request = (3 << 30) | (length << 16) | (ord("H") << 8) | number
    received = fcntl.ioctl(fd, request, buffer, True)
    if received < 1 or received > length or buffer[0] != report_id:
        raise MonitorError("The USB report response is invalid.")
    if payload is not None and received != length:
        raise MonitorError("The USB report could not be sent completely.")
    return bytes(buffer[:received])


def percentage(value: float) -> float:
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise MonitorError("Brightness must be between 0 and 100%.")
    return value


class Monitor:
    def __init__(self, device: Device):
        self.device = device
        self.fd = None

    def __enter__(self) -> Monitor:
        try:
            self.fd = os.open(self.device.node, os.O_RDWR | os.O_CLOEXEC)
            # Cooperating helper processes must not interleave USB transactions.
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.channels = channels_from_descriptor(
                (self.device.sys_path / "report_descriptor").read_bytes())
            identity = self._get("identity")
            if len(identity) != 25:
                raise MonitorError("The monitor returned an invalid identity response.")
            self.serial = identity[1:9].decode("ascii", "replace").strip(" \0")
            self.model = identity[9:25].decode("ascii", "replace").strip(" \0")
            features = parse_features(self._descriptor(), usages={BRIGHTNESS})
            feature = features.get(BRIGHTNESS)
            if (feature is None or feature.bits != 16 or feature.count != 1
                    or feature.offset != 0 or not 0 <= feature.minimum < feature.maximum <= 65535):
                raise MonitorError("Brightness control is not supported on this monitor.")
            self.brightness = feature
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _get(self, channel: str) -> bytes:
        return feature_ioctl(self.fd, self.channels[channel], CHANNELS[channel][1])

    def _send(self, channel: str, payload: bytes):
        feature_ioctl(self.fd, self.channels[channel], CHANNELS[channel][1], payload)

    def _descriptor(self) -> bytes:
        self._send("descriptor", bytes(516))
        result = bytearray()
        total = None
        while total is None or len(result) < total:
            response = self._get("descriptor")
            if len(response) < 5:
                raise MonitorError("Could not read the monitor's settings descriptor.")
            offset, length = struct.unpack_from("<HH", response, 1)
            if (not 0 < length <= 4096 or offset != len(result)
                    or total is not None and total != length):
                raise MonitorError("The monitor's settings descriptor is invalid.")
            total = length
            count = min(512, total - offset)
            if len(response) < 5 + count:
                raise MonitorError("The monitor's settings descriptor is truncated.")
            result.extend(response[5:5 + count])
        return bytes(result)

    def _request(self, value: int | None = None) -> int | None:
        counter = self._get("counter")
        if len(counter) != 3:
            raise MonitorError("Could not read the request counter.")
        # EIZO sends the usage page first, then the usage, both little endian.
        header = struct.pack("<HH", BRIGHTNESS >> 16, BRIGHTNESS & 0xFFFF) + counter[1:3]
        if value is None:
            self._send("get", header + bytes(32))
            response = self._get("get")
            if len(response) < 9 or response[1:7] != header:
                raise MonitorError("The brightness response does not match the request.")
            result = int.from_bytes(response[7:9], "little")
        else:
            self._send("set", header + value.to_bytes(2, "little") + bytes(30))
            result = None
        verification = self._get("verify")
        if len(verification) != 8 or verification[1:7] != header:
            raise MonitorError("Detected a conflict with other monitor control software.")
        if verification[7] != 0:
            raise MonitorError("The monitor rejected the brightness change. Check the display mode.")
        return result

    def read(self) -> dict:
        raw = self._request()
        feature = self.brightness
        if not feature.minimum <= raw <= feature.maximum:
            raise MonitorError("The monitor returned an out-of-range brightness.")
        return dict(device=self.device.node, model=self.model, serial=self.serial,
                    percent=round((raw - feature.minimum) * 100 /
                                  (feature.maximum - feature.minimum), 2),
                    raw=raw, minimum=feature.minimum, maximum=feature.maximum)

    def set(self, percent: float) -> dict:
        percentage(percent)
        feature = self.brightness
        raw = feature.minimum + round(percent * (feature.maximum - feature.minimum) / 100)
        self._request(raw)
        for attempt in range(5):
            actual = self.read()
            if actual["raw"] == raw:
                return actual
            if attempt < 4:
                time.sleep(0.05)
        raise MonitorError("The brightness does not match the requested value. Check Auto EcoView and the display mode.")


def error_message(error: Exception) -> str:
    if isinstance(error, PermissionError):
        return "No permission to access the USB device. Follow the udev setup in the README."
    if isinstance(error, FileNotFoundError):
        return "USB device not found. Check the USB connection and that this environment can access the device."
    if isinstance(error, BlockingIOError):
        return "Another brightness operation is in progress. Wait a moment and try again."
    return str(error)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="USB brightness control for EIZO monitors")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "get", "set"):
        sub = commands.add_parser(command)
        sub.add_argument("--json", action="store_true")
        if command != "list":
            sub.add_argument("--device", help="e.g. /dev/hidraw1 (optional when only one monitor is connected)")
        if command == "set":
            sub.add_argument("percent", type=float, help="brightness, 0-100%%")
    args = parser.parse_args(argv)
    try:
        if args.command == "set":
            percentage(args.percent)
        devices = discover()
        if args.command == "list":
            monitors, errors = [], []
            for device in devices:
                try:
                    with Monitor(device) as monitor:
                        monitors.append(monitor.read())
                except (OSError, MonitorError) as error:
                    errors.append(dict(device=device.node, error=error_message(error)))
            result = dict(monitors=monitors, errors=errors)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                for monitor in monitors:
                    print(f'{monitor["device"]}  {monitor["model"]}  '
                          f'{monitor["serial"]}  {monitor["percent"]:g}%')
                for error in errors:
                    print(f'{error["device"]}: {error["error"]}', file=sys.stderr)
                if not monitors and not errors:
                    print("No supported EIZO USB monitor found. Check the USB connection.")
            return 1 if errors and not monitors else 0
        if args.device:
            devices = [device for device in devices if device.node == args.device]
        if len(devices) != 1:
            raise MonitorError("Could not select a single monitor. Run list, then specify one with --device.")
        with Monitor(devices[0]) as monitor:
            result = monitor.set(args.percent) if args.command == "set" else monitor.read()
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f'{result["model"]}: {result["percent"]:g}% '
                  f'(raw {result["raw"]}/{result["maximum"]})')
        return 0
    except (OSError, MonitorError) as error:
        message = error_message(error)
        if args.json:
            print(json.dumps(dict(error=message), ensure_ascii=False))
        else:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
