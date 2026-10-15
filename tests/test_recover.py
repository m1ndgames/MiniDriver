"""The independent safety net that keeps the box from stranding itself.

The daemon takes wlan0 away from NetworkManager and parks it unassociated to
scan, which means any daemon fault leaves a WiFi-only box unreachable. This
watchdog runs on a timer *outside* the daemon, so a crashed, hung or
mis-configured daemon cannot defeat it.

The decision has to tell "legitimately driving around" apart from "stranded",
and the only signal that separates them is whether a configured home network is
within reach while we still have no address.
"""
from minidriver import recover


class TestStranded:
    def test_home_in_range_without_an_address_is_stranded(self):
        assert recover.stranded(["home"], ["home", "neighbour"], None, recover.DOWN) is True

    def test_home_in_range_with_an_address_is_working(self):
        assert recover.stranded(["home"], ["home"], "192.168.1.42",
                                "home") is False

    def test_an_address_with_a_dead_link_is_still_stranded(self):
        """When an access point vanishes the interface keeps its IP, so the box
        looks healthy while nothing is reachable. A hotspot being switched off,
        with home WiFi still in range, walked straight past the old check."""
        assert recover.stranded(["home", "hotspot"], ["home"], "192.168.1.42",
                                recover.DOWN) is True

    def test_associated_elsewhere_still_counts_as_reachable(self):
        """Being on a different network is not this check's business -- it only
        cares that the box can be reached at all."""
        assert recover.stranded(["home", "hotspot"], ["home"], "192.168.1.42",
                                "hotspot") is False

    def test_home_out_of_range_is_just_the_field(self):
        """Mid-drive there is no home network and no address, and that is the
        normal working state -- recovering here would end every survey."""
        assert recover.stranded(["home"], ["cafe", "bus"], None, recover.DOWN) is False

    def test_an_address_away_from_home_is_left_alone(self):
        assert recover.stranded(["home"], ["cafe"], "10.0.0.9", "cafe") is False

    def test_no_home_network_configured_is_stranded(self):
        """With nothing to come home to the daemon can never make the box
        reachable, so NetworkManager is the only way back in -- whatever is
        currently visible."""
        assert recover.stranded([], ["cafe"], None, recover.DOWN) is True
        assert recover.stranded(None, [], None, recover.DOWN) is True

    def test_no_home_configured_but_already_addressed_is_fine(self):
        assert recover.stranded([], ["cafe"], "10.0.0.9", "cafe") is False


class TestReason:
    def test_names_the_home_network_that_was_in_range(self):
        assert "['home']" in recover.reason(["home"], ["home", "cafe"])

    def test_an_empty_config_says_so_rather_than_blaming_range(self):
        """The operator reads this line to find out why their daemon stopped, so
        it must not claim a home network was in range when none is configured."""
        why = recover.reason([], ["cafe"])
        assert "no home networks are configured" in why
        assert "in range" not in why


class TestScanDumpArgv:
    def test_reads_the_cache_instead_of_triggering_a_scan(self):
        """A fresh scan would race the daemon's own scanning and can fail with
        EBUSY; `scan dump` reads the cache the daemon keeps warm."""
        argv = recover.scan_dump_argv("wlan0")
        assert argv == ["iw", "dev", "wlan0", "scan", "dump"]
