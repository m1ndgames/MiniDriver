"""Battery level from the PiSugar 3 UPS.

pisugar-server answers `key: value` over a Unix socket. The socket talk is
injected, so the parsing and the shaping are tested without a UPS attached --
which is also the state of any dev machine.
"""
from minidriver import battery


class TestParseReply:
    def test_reads_key_value_lines(self):
        text = "battery: 75.18806\nbattery_charging: false\n"
        assert battery.parse_reply(text) == {
            "battery": "75.18806", "battery_charging": "false"}

    def test_tolerates_junk_and_blanks(self):
        assert battery.parse_reply("") == {}
        assert battery.parse_reply("no colon here\n\n") == {}


class TestToStatus:
    def test_rounds_the_percentage(self):
        assert battery.to_status({"battery": "75.18806"})["percent"] == 75

    def test_reads_the_charging_and_plugged_flags(self):
        status = battery.to_status({
            "battery": "40", "battery_charging": "true",
            "battery_power_plugged": "true"})
        assert status["charging"] is True and status["plugged"] is True

    def test_flags_default_to_false(self):
        status = battery.to_status({"battery": "40"})
        assert status["charging"] is False and status["plugged"] is False

    def test_clamps_to_a_sane_range(self):
        """The gauge can read slightly outside 0-100; the panel should not."""
        assert battery.to_status({"battery": "103.4"})["percent"] == 100
        assert battery.to_status({"battery": "-2"})["percent"] == 0

    def test_no_battery_value_is_no_status(self):
        assert battery.to_status({}) is None
        assert battery.to_status({"battery": "not a number"}) is None


class TestRead:
    def test_returns_the_status_from_the_socket(self):
        def ask(path, timeout):
            return "battery: 51.5\nbattery_charging: true\n"

        assert battery.read(ask=ask) == {
            "percent": 52, "charging": True, "plugged": False}

    def test_an_absent_ups_is_not_an_error(self):
        """No UPS fitted, or pisugar-server not running, is a normal state for
        this box -- it must never disturb the survey."""
        def explode(path, timeout):
            raise OSError("no such socket")

        assert battery.read(ask=explode) is None

    def test_asks_only_for_what_the_panel_needs(self):
        seen = {}

        def ask(path, timeout):
            seen["path"] = path
            seen["timeout"] = timeout
            return "battery: 10\n"

        battery.read(path="/tmp/x.sock", timeout=0.25, ask=ask)
        assert seen == {"path": "/tmp/x.sock", "timeout": 0.25}

    def test_the_default_timeout_is_short(self):
        """It is read on the loop that also pets the systemd watchdog."""
        assert battery.TIMEOUT <= 1.0
