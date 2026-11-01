import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extension"))
import eizo_brightness as eizo


# The primary HID descriptor read from the attached EV3240X (056d:4081).
PRIMARY = bytes.fromhex(
    "05800901a1010630ff85017508960402150026ff000901b102"
    "850275089526150026ff000902b102"
    "850375089526150026ff00090381020903b102"
    "85047508960601150026ff000904b102"
    "85057508960601150026ff000905b102"
    "850675109501150126ff7f0906b102"
    "850775089507150026ff000907b102"
    "850875089518150026ff000908b102"
    "850975089554150026ff000909b102c0")
# A single virtual brightness control: usage 0082:0010, uint16, 0..200.
SECONDARY = bytes.fromhex("0582858275109501150026c8000910b102")
# EV3240X exposes usage ff01:00fe in two reports (78 and 79).
# https://github.com/markbolhuis/libeizo/issues/10
DUPLICATE_COMPATIBILITY = bytes.fromhex(
    "0601ff854e75089501150026ff0009feb102854f950509feb102")
# Complete settings descriptor read from the attached EV3240X on 2026-10-18.
EV3240X_SETTINGS = bytes.fromhex(
    (Path(__file__).parent / "fixtures" / "ev3240x-settings.hex").read_text())
HEADER = bytes.fromhex("820010003412")


def monitor():
    instance = eizo.Monitor(eizo.Device("/dev/hidraw1", Path("/unused"), 0x4081))
    instance.model = "EV3240X"
    instance.serial = "TEST0001"
    instance.brightness = eizo.parse_features(SECONDARY)[eizo.BRIGHTNESS]
    instance.channels = eizo.channels_from_descriptor(PRIMARY)
    return instance


class DescriptorTests(unittest.TestCase):
    def test_real_ev3240x_descriptor_routes_vendor_channels(self):
        self.assertEqual(eizo.channels_from_descriptor(PRIMARY), {
            "descriptor": 1, "set": 2, "get": 3,
            "counter": 6, "verify": 7, "identity": 8})

    def test_unsigned_maximum_and_brightness_limits(self):
        brightness = eizo.parse_features(SECONDARY)[eizo.BRIGHTNESS]
        self.assertEqual((brightness.minimum, brightness.maximum), (0, 200))
        identity = eizo.parse_features(PRIMARY)[0xFF300008]
        self.assertEqual(identity.maximum, 255)

    def test_truncated_descriptor_is_rejected(self):
        with self.assertRaises(eizo.MonitorError):
            eizo.parse_features(bytes.fromhex("26c8"))

    def test_unrelated_hid_device_is_rejected(self):
        with self.assertRaises(eizo.MonitorError):
            eizo.channels_from_descriptor(SECONDARY)

    def test_global_push_pop_restores_usage_page(self):
        data = bytes.fromhex("0582a40501b475109501150026c80085010910b102")
        self.assertIn(eizo.BRIGHTNESS, eizo.parse_features(data))

    def test_unrelated_duplicate_usages_do_not_block_monitor_open(self):
        for data in (DUPLICATE_COMPATIBILITY + SECONDARY,
                     SECONDARY + DUPLICATE_COMPATIBILITY, EV3240X_SETTINGS):
            with (
                self.subTest(descriptor=data.hex()),
                patch.object(eizo.os, "open", return_value=123),
                patch.object(eizo.os, "close"),
                patch.object(eizo.fcntl, "flock"),
                patch.object(Path, "read_bytes", return_value=PRIMARY),
                patch.object(eizo.Monitor, "_get", return_value=(
                    b"\x08TEST0001" + b"EV3240X".ljust(16, b" "))),
                patch.object(eizo.Monitor, "_descriptor", return_value=data),
            ):
                with eizo.Monitor(eizo.Device("/dev/hidraw1", Path("/unused"), 0x4081)) as instance:
                    self.assertEqual((instance.brightness.minimum, instance.brightness.maximum), (0, 200))

    def test_real_ev3240x_settings_descriptor_selects_brightness(self):
        features = eizo.parse_features(EV3240X_SETTINGS, usages={eizo.BRIGHTNESS})
        self.assertEqual(features[eizo.BRIGHTNESS],
                         eizo.Feature(eizo.BRIGHTNESS, 130, 16, 1, 0, 200))

    def test_duplicate_brightness_usage_is_rejected(self):
        data = SECONDARY + bytes.fromhex("85830910b102")
        with self.assertRaisesRegex(eizo.MonitorError, "00820010"):
            eizo.parse_features(data, usages={eizo.BRIGHTNESS})

    def test_duplicate_required_channel_is_rejected(self):
        data = PRIMARY[:-1] + bytes.fromhex("850a95180908b102c0")
        with self.assertRaisesRegex(eizo.MonitorError, "ff300008"):
            eizo.channels_from_descriptor(data)

    def test_ignored_usages_still_count_towards_report_offset(self):
        data = bytes.fromhex("0601ff85827508950109feb102") + SECONDARY
        brightness = eizo.parse_features(data, usages={eizo.BRIGHTNESS})[eizo.BRIGHTNESS]
        self.assertEqual(brightness.offset, 8)

    def test_descriptor_download_checks_offsets(self):
        instance = monitor()
        instance._send = Mock()
        instance._get = Mock(return_value=b"\x01\x01\x00\x04\x00abcd")
        with self.assertRaises(eizo.MonitorError):
            instance._descriptor()

    def test_descriptor_download_accepts_partial_final_chunk(self):
        instance = monitor()
        instance._send = Mock()
        instance._get = Mock(return_value=b"\x01\x00\x00" +
                            len(SECONDARY).to_bytes(2, "little") + SECONDARY)
        self.assertEqual(instance._descriptor(), SECONDARY)

    def test_discovery_filters_vendor_and_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, vendor in [("hidraw1", "056D"), ("hidraw2", "1234")]:
                device = root / name / "device"
                device.mkdir(parents=True)
                (device / "uevent").write_text(f"HID_ID=0003:0000{vendor}:00004081\n")
                (device / "report_descriptor").write_bytes(PRIMARY)
            devices = eizo.discover(root)
            self.assertEqual([device.node for device in devices], ["/dev/hidraw1"])


class ProtocolTests(unittest.TestCase):
    def test_get_uses_read_channel_with_page_first_wire_order(self):
        instance = monitor()
        instance._send = Mock()
        instance._get = Mock(side_effect=[
            bytes.fromhex("063412"),
            b"\x03" + HEADER + b"\x64\x00" + bytes(30),
            b"\x07" + HEADER + b"\x00",
        ])
        self.assertEqual(instance.read()["percent"], 50)
        instance._send.assert_called_once_with("get", HEADER + bytes(32))

    def test_set_only_writes_brightness_and_verifies_result(self):
        instance = monitor()
        instance._send = Mock()
        instance._get = Mock(side_effect=[bytes.fromhex("063412"), b"\x07" + HEADER + b"\x00"])
        instance._request(101)
        instance._send.assert_called_once_with("set", HEADER + b"\x65\x00" + bytes(30))

    def test_stale_response_is_rejected(self):
        instance = monitor()
        instance._send = Mock()
        instance._get = Mock(side_effect=[
            bytes.fromhex("063412"), b"\x03" + HEADER + b"\x64\x00",
            b"\x07" + bytes.fromhex("82001000785600"),
        ])
        with self.assertRaisesRegex(eizo.MonitorError, "conflict"):
            instance.read()

    def test_monitor_rejection_is_reported(self):
        instance = monitor()
        instance._send = Mock()
        instance._get = Mock(side_effect=[
            bytes.fromhex("063412"), b"\x07" + HEADER + b"\x01",
        ])
        with self.assertRaisesRegex(eizo.MonitorError, "rejected"):
            instance._request(100)

    def test_percentage_maps_half_percent_to_native_step(self):
        instance = monitor()
        instance._request = Mock(side_effect=[None, 101])
        self.assertEqual(instance.set(50.5)["percent"], 50.5)
        self.assertEqual(instance._request.call_args_list[0].args, (101,))

    def test_nonzero_device_minimum_is_respected(self):
        instance = monitor()
        instance.brightness = eizo.Feature(eizo.BRIGHTNESS, 130, 16, 1, 20, 220)
        instance._request = Mock(side_effect=[None, 20])
        self.assertEqual(instance.set(0)["percent"], 0)
        self.assertEqual(instance._request.call_args_list[0].args, (20,))

    def test_invalid_values_never_write_to_device(self):
        instance = monitor()
        instance._request = Mock()
        for value in (-1, 101, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(eizo.MonitorError):
                instance.set(value)
        instance._request.assert_not_called()

    @patch.object(eizo.time, "sleep")
    def test_delayed_readback_is_allowed(self, _sleep):
        instance = monitor()
        instance._request = Mock(side_effect=[None, 99, 100])
        self.assertEqual(instance.set(50)["raw"], 100)

    @patch.object(eizo.time, "sleep")
    def test_ignored_setting_does_not_report_success(self, _sleep):
        instance = monitor()
        instance._request = Mock(side_effect=[None] + [80] * 5)
        with self.assertRaisesRegex(eizo.MonitorError, "does not match"):
            instance.set(50)

    def test_short_hid_response_is_rejected(self):
        with patch.object(eizo.fcntl, "ioctl", return_value=0):
            with self.assertRaises(eizo.MonitorError):
                eizo.feature_ioctl(123, 6, 3)


class CliTests(unittest.TestCase):
    def test_invalid_json_set_fails_before_device_discovery(self):
        with patch.object(eizo, "discover") as discover, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(eizo.main(["set", "nan", "--json"]), 1)
        discover.assert_not_called()
        self.assertIn("error", json.loads(output.getvalue()))

    def test_multiple_monitors_require_explicit_selection(self):
        devices = [eizo.Device(f"/dev/hidraw{n}", Path("/unused"), 0x4081) for n in (1, 2)]
        with patch.object(eizo, "discover", return_value=devices), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(eizo.main(["set", "50", "--json"]), 1)
        self.assertIn("--device", json.loads(output.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
