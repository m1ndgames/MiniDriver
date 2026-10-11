"""The WigleWifi-1.6 survey file: row shaping and the append-as-you-go writer.

Ported from m1ndb0x, which proved this format against WiGLE, WDGWars and
Wardrift uploads. WiFi and BLE sightings share one file, told apart by Type.
"""
import csv
import io
import time
from datetime import datetime, timezone

from minidriver import survey
from minidriver.survey import COLUMNS

FIX = {"mode": 3, "latitude": 51.2, "longitude": 6.7, "altitude": 40.0,
       "accuracy": 4.5}
NO_FIX = {"mode": 1, "latitude": None, "longitude": None}


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.123456789Z")


def ap(mac="aa:bb:cc:dd:ee:01", heard=None, **fields):
    record = {"mac": mac, "hostname": "home", "encryption": "WPA2",
              "authentication": "PSK", "cipher": "CCMP", "channel": 6,
              "frequency": 2437, "rssi": -60,
              "last_seen": stamp(time.time() if heard is None else heard)}
    record.update(fields)
    return record


def ble(mac="dd:dd:dd:dd:dd:01", name="Fitband", heard=None, **fields):
    record = {"mac": mac, "name": name, "rssi": -70, "kind": "ble",
              "last_seen": stamp(time.time() if heard is None else heard)}
    record.update(fields)
    return record


def rows_of(path):
    lines = path.read_text().splitlines()
    return list(csv.reader(io.StringIO("\n".join(lines[2:]))))


class TestWigleRow:
    def test_row_lines_up_with_columns(self):
        row = survey.wigle_row(ap(), FIX, time.time())
        assert len(row) == len(COLUMNS)
        fields = dict(zip(COLUMNS, row))
        assert fields["MAC"] == "AA:BB:CC:DD:EE:01"
        assert fields["Type"] == "WIFI"
        assert fields["AuthMode"] == "[WPA2-PSK-CCMP][ESS]"

    def test_hidden_network_has_no_name(self):
        row = dict(zip(COLUMNS, survey.wigle_row(ap(hostname="<hidden>"), FIX,
                                                 time.time())))
        assert row["SSID"] == ""

    def test_missing_frequency_is_derived_from_channel(self):
        row = dict(zip(COLUMNS, survey.wigle_row(ap(frequency=None, channel=6),
                                                 FIX, time.time())))
        assert row["Frequency"] == 2437

    def test_a_ble_device_is_typed_ble_with_only_a_name(self):
        row = dict(zip(COLUMNS, survey.wigle_row(ble(), FIX, time.time())))
        assert row["Type"] == "BLE"
        assert row["SSID"] == "Fitband"
        assert row["AuthMode"] == "" and row["Channel"] == "" and row["Frequency"] == ""

    def test_a_nameless_ble_device_has_no_name(self):
        row = dict(zip(COLUMNS, survey.wigle_row(ble(name=""), FIX, time.time())))
        assert row["SSID"] == ""


class TestSurveyLog:
    def test_a_fresh_file_starts_with_both_header_lines(self, tmp_path):
        path = tmp_path / "s.csv"
        survey.SurveyLog(path).close()
        lines = path.read_text().splitlines()
        assert lines[0].startswith(survey.VERSION)
        assert lines[1] == ",".join(COLUMNS)

    def test_nothing_is_written_without_a_fix(self, tmp_path):
        log = survey.SurveyLog(tmp_path / "s.csv")
        assert log.record([ap()], NO_FIX, time.time(), 100.0) == []
        log.close()

    def test_a_stale_sighting_is_not_recorded(self, tmp_path):
        log = survey.SurveyLog(tmp_path / "s.csv")
        now = time.time()
        # Heard a full minute ago: too old to tag with where the box is now.
        assert log.record([ap(heard=now - 60)], FIX, now, 100.0) == []
        log.close()

    def test_one_row_per_access_point_per_interval(self, tmp_path):
        log = survey.SurveyLog(tmp_path / "s.csv")
        now = time.time()
        a = ap()
        log.record([a], FIX, now, 100.0)
        # Same AP seen again a second later: no second row yet.
        a2 = ap(heard=now + 1)
        assert log.record([a2], FIX, now + 1, 101.0) == []
        # ...but past ROW_SECONDS it earns another.
        a3 = ap(heard=now + survey.ROW_SECONDS + 1)
        assert log.record([a3], FIX, now + survey.ROW_SECONDS + 1,
                          100.0 + survey.ROW_SECONDS + 1) != []
        log.close()

    def test_an_ssid_with_a_comma_survives_as_one_field(self, tmp_path):
        path = tmp_path / "s.csv"
        log = survey.SurveyLog(path)
        log.record([ap(hostname="Cafe, upstairs")], FIX, time.time(), 100.0)
        log.close()
        assert rows_of(path)[0][1] == "Cafe, upstairs"

    def test_reopening_appends_without_a_second_header(self, tmp_path):
        path = tmp_path / "s.csv"
        log = survey.SurveyLog(path)
        log.record([ap()], FIX, time.time(), 100.0)
        log.close()
        log2 = survey.SurveyLog(path)
        log2.record([ap(mac="aa:bb:cc:dd:ee:02")], FIX, time.time(), 100.0)
        log2.close()
        lines = [ln for ln in path.read_text().splitlines() if ln]
        assert sum(1 for ln in lines if ln.startswith(survey.VERSION)) == 1
        assert len(rows_of(path)) == 2


class TestStripUnlocated:
    def test_drops_zero_and_blank_coordinates(self):
        text = (survey.VERSION + "\n" + ",".join(COLUMNS) + "\n"
                + "AA:BB:CC:00:00:01,n,,,,,,51.2,6.7,,,,,WIFI\n"
                + "AA:BB:CC:00:00:02,n,,,,,,0,0,,,,,WIFI\n"
                + "AA:BB:CC:00:00:03,n,,,,,,,,,,,,WIFI\n")
        kept = survey.strip_unlocated(text)
        assert survey.records_in(kept) == 1


class TestRecordsIn:
    def test_counts_data_rows_past_the_two_headers(self):
        text = survey.VERSION + "\n" + ",".join(COLUMNS) + "\nrow1\nrow2\n"
        assert survey.records_in(text) == 2
        assert survey.records_in("") == 0
