"""The PiSugar timer tasks: low-battery shutdown and RTC seeding.

Both are safety nets that run outside the survey daemon, so what matters is that
they act only when they should -- a spurious shutdown is worse than none.
"""
import datetime

from minidriver import pisugar


class TestShouldShutDown:
    def test_a_flat_cell_on_battery_shuts_down(self):
        assert pisugar.should_shut_down(
            {"volts": 3.48, "charging": False, "plugged": False}, 3.55) is True

    def test_a_healthy_cell_does_not(self):
        assert pisugar.should_shut_down(
            {"volts": 3.80, "charging": False, "plugged": False}, 3.55) is False

    def test_charging_is_never_low(self):
        """Voltage is depressed while a flat cell recovers, and pulling the plug
        on a box that is plugged in would be absurd."""
        assert pisugar.should_shut_down(
            {"volts": 3.40, "charging": True, "plugged": False}, 3.55) is False
        assert pisugar.should_shut_down(
            {"volts": 3.40, "charging": False, "plugged": True}, 3.55) is False

    def test_no_ups_is_not_our_business(self):
        assert pisugar.should_shut_down(None, 3.55) is False
        assert pisugar.should_shut_down({}, 3.55) is False

    def test_a_missing_voltage_never_triggers(self):
        """Without a measured voltage there is nothing to act on -- guessing
        would risk shutting the box down mid-survey."""
        assert pisugar.should_shut_down(
            {"volts": None, "charging": False, "plugged": False}, 3.55) is False


class TestLowBatteryAction:
    def _run(self, readings, threshold=3.55):
        seen = iter(readings)
        calls = []
        pisugar._read_battery = lambda: next(seen)
        pisugar.low_battery(threshold, runner=calls.append, sleep=lambda s: None)
        return calls

    def test_it_powers_off_when_the_cell_stays_low(self):
        flat = {"volts": 3.40, "charging": False, "plugged": False}
        assert self._run([flat, flat]) == [["systemctl", "poweroff"]]

    def test_a_single_sag_does_not_trigger_it(self):
        """A WiFi scan burst sags the rail; one low reading is not a flat cell,
        and a spurious shutdown mid-survey is worse than none."""
        flat = {"volts": 3.40, "charging": False, "plugged": False}
        fine = {"volts": 3.72, "charging": False, "plugged": False}
        assert self._run([flat, fine]) == []

    def test_a_healthy_cell_is_not_even_re_checked(self):
        fine = {"volts": 3.80, "charging": False, "plugged": False}
        assert self._run([fine]) == []


class TestRtcTime:
    def test_it_reads_a_credible_timestamp(self):
        when = pisugar.rtc_time({"rtc_time": "2026-10-14T20:32:44.000+02:00"})
        assert when is not None and when.year == 2026

    def test_an_unset_rtc_is_rejected(self):
        """A PiSugar that has never been set reports 2020; seeding the clock
        from that would break TLS on every upload."""
        assert pisugar.rtc_time(
            {"rtc_time": "2020-06-27T17:06:53.000+02:00"}) is None

    def test_junk_and_absence_are_rejected(self):
        assert pisugar.rtc_time({}) is None
        assert pisugar.rtc_time({"rtc_time": "not a time"}) is None

    def test_the_credibility_floor_is_in_the_past(self):
        assert pisugar.EARLIEST_CREDIBLE < datetime.datetime.now(
            datetime.timezone.utc)
