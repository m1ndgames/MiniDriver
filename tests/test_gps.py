"""Reading gpsd. The socket read is thin; the JSON folding is what carries the
logic and is tested here. Ported from m1ndb0x. gpsd abstracts the hardware, so
the USB test dongle and the L76K HAT fold identically."""
import json

from minidriver.gps import has_fix, parse_gpsd


def tpv(**fields):
    return json.dumps({"class": "TPV", **fields})


def sky(sats):
    return json.dumps({"class": "SKY",
                       "satellites": [{"used": u} for u in sats]})


class TestParseGpsd:
    def test_no_data_is_no_fix(self):
        gps = parse_gpsd([])
        assert gps["mode"] == 0 and gps["latitude"] is None
        assert has_fix(gps) is False

    def test_a_tpv_gives_position_and_mode(self):
        gps = parse_gpsd([tpv(mode=3, lat=51.2, lon=6.7, altMSL=40.0)])
        assert gps["mode"] == 3
        assert gps["latitude"] == 51.2 and gps["longitude"] == 6.7
        assert gps["altitude"] == 40.0
        assert has_fix(gps) is True

    def test_null_island_is_not_a_fix(self):
        """A cold-starting receiver reports mode 2 with 0,0 before it has a real
        position. Recording that fills the capture with rows the upload path
        then throws away -- and 0,0 is how "nowhere" is written in this format,
        not a spot in the Gulf of Guinea."""
        assert has_fix({"mode": 2, "latitude": 0.0, "longitude": 0.0}) is False
        assert has_fix({"mode": 3, "latitude": 0, "longitude": 0}) is False

    def test_a_real_position_near_the_equator_still_counts(self):
        """Only exactly 0,0 is rejected; genuinely small coordinates are fine."""
        assert has_fix({"mode": 2, "latitude": 0.0001,
                        "longitude": 0.0}) is True

    def test_a_missing_longitude_is_not_a_fix(self):
        assert has_fix({"mode": 2, "latitude": 51.2, "longitude": None}) is False

    def test_satellites_seen_and_used_are_counted(self):
        gps = parse_gpsd([sky([True, False, True, False, False])])
        assert gps["satellites_seen"] == 5
        assert gps["satellites_used"] == 2

    def test_accuracy_is_the_worse_of_the_two_axes(self):
        gps = parse_gpsd([tpv(mode=2, lat=1.0, lon=2.0, epx=3.0, epy=7.5)])
        assert gps["accuracy"] == 7.5

    def test_garbage_lines_are_ignored(self):
        gps = parse_gpsd(["not json", "", tpv(mode=2, lat=1.0, lon=2.0)])
        assert gps["latitude"] == 1.0
