"""BLE advertisement scanning on the internal radio.

bleak and its event loop are hardware and are exercised on the box. What is
tested here is ours: shaping a discovered device into the sighting the survey
writer expects, and the bookkeeping that keeps the latest advert per device.
"""
from minidriver import survey
from minidriver.ble import BleScanner, to_sighting
from minidriver.wifi import seen_at


class TestToSighting:
    def test_shapes_a_ble_sighting(self):
        s = to_sighting("AA:BB:CC:DD:EE:01", "Fitband", -70, now_wall=1_790_000_000.0)
        assert s["kind"] == "ble"
        assert s["mac"] == "AA:BB:CC:DD:EE:01"
        assert s["name"] == "Fitband"
        assert s["rssi"] == -70
        assert seen_at(s["last_seen"]) == 1_790_000_000.0

    def test_a_nameless_device_has_a_blank_name(self):
        assert to_sighting("AA:BB:CC:DD:EE:02", None, -80)["name"] == ""

    def test_it_feeds_wigle_row_as_a_ble_row(self):
        fix = {"mode": 3, "latitude": 51.2, "longitude": 6.7}
        row = dict(zip(survey.COLUMNS,
                       survey.wigle_row(to_sighting("AA:BB:CC:DD:EE:03", "Watch", -60),
                                        fix, 1_790_000_000.0)))
        assert row["Type"] == "BLE" and row["SSID"] == "Watch"


class TestBookkeeping:
    def test_a_recorded_device_appears_in_the_snapshot(self):
        scanner = BleScanner()
        scanner._record("AA:BB:CC:DD:EE:04", "Tag", -55)
        snap = scanner.snapshot()
        assert len(snap) == 1 and snap[0]["mac"] == "AA:BB:CC:DD:EE:04"

    def test_the_same_device_keeps_one_entry_with_the_latest_advert(self):
        scanner = BleScanner()
        scanner._record("AA:BB:CC:DD:EE:05", "Tag", -80)
        scanner._record("AA:BB:CC:DD:EE:05", "Tag", -40)
        snap = scanner.snapshot()
        assert len(snap) == 1 and snap[0]["rssi"] == -40

    def test_clear_empties_the_snapshot(self):
        scanner = BleScanner()
        scanner._record("AA:BB:CC:DD:EE:06", "Tag", -55)
        scanner.clear()
        assert scanner.snapshot() == []
