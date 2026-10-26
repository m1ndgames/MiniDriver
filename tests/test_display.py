"""The e-paper status dashboard.

The 2.13" panel is 250x122, monochrome and has no buttons, so the UI is
output-only. PIL and the Waveshare driver are hardware; what is tested is how a
status snapshot becomes the text that goes on the panel.

There is one content page and nothing else. The button blanks the panel to
white and brings it back; it no longer cycles through pages.

Layout:

    Status: scanning
    GPS: 51.2345, 6.7654 3D 7/11
    WiFi: localhost_2G
      192.168.178.43 -52dBm
    seen: 42 wifi, 18 saved
    Batt: 3.72V 38%
"""
import threading

from minidriver.display import ui
from minidriver.display.ui import (BackgroundDisplay, activity_text,
                                   battery_text, connection_text, gps_text)

# 250px at 14px DejaVu Sans Mono is about 29 characters.
PANEL_COLUMNS = 29
# 122px tall from y=2 at LINE_HEIGHT 16 leaves room for seven lines; an eighth
# is clipped by the panel, so the single page has a hard line budget.
PANEL_LINES = 7


def status(**over):
    base = {
        "connected": False,
        "activity": "scanning",
        "battery": None,
        "gps": {"fix": True, "mode": 3, "latitude": 51.2345,
                "longitude": 6.7654, "sats_used": 7, "sats_seen": 11},
        "wifi_aps": 42, "wifi_saved": 18,
        "ble_devices": 5, "ble_saved": 2,
        "rows": 318,
    }
    base.update(over)
    return base


class TestHeader:
    def test_shows_whether_the_box_is_connected(self):
        assert connection_text(status(connected=True)) == "connected"
        assert connection_text(status(connected=False)) == "not connected"

    def test_shows_the_battery_percentage(self):
        assert battery_text(status(battery={"percent": 75})) == "75%"

    def test_marks_charging_with_a_plus(self):
        assert battery_text(
            status(battery={"percent": 75, "charging": True})) == "+75%"

    def test_no_ups_fitted_leaves_the_corner_empty(self):
        assert battery_text(status(battery=None)) == ""
        assert battery_text(status(battery={})) == ""

    def test_header_fits_across_the_panel(self):
        worst = status(connected=False,
                       battery={"percent": 100, "charging": True})
        assert (len(connection_text(worst)) + 1
                + len(battery_text(worst))) <= PANEL_COLUMNS


class TestGpsLine:
    def test_shows_the_position_when_there_is_a_fix(self):
        assert gps_text(status()).startswith("GPS: 51.2345, 6.7654")

    def test_shows_search_progress_instead_of_a_bare_no_fix(self):
        """Satellite counts answer the real question during a cold start: is
        this getting anywhere, or is the antenna blind?"""
        searching = {"fix": False, "mode": 1, "latitude": None,
                     "longitude": None, "sats_used": 3, "sats_seen": 11}
        assert gps_text(status(gps=searching)) == "GPS: searching 3/11"

    def test_nothing_heard_at_all_says_no_signal(self):
        """No antenna, no sky, or no gpsd -- distinct from searching."""
        blind = {"fix": False, "mode": 0, "latitude": None, "longitude": None,
                 "sats_used": 0, "sats_seen": 0}
        assert gps_text(status(gps=blind)) == "GPS: no signal"

    def test_null_island_is_never_shown_as_a_position(self):
        """A cold-starting receiver reports mode 2 at exactly 0,0. build_status
        marks that as no fix, and the panel must then show progress rather than
        printing 0.0000, 0.0000."""
        null_island = {"fix": False, "mode": 2, "latitude": 0.0,
                       "longitude": 0.0, "sats_used": 3, "sats_seen": 11}
        assert gps_text(status(gps=null_island)) == "GPS: searching 3/11"

    def test_a_mode_without_coordinates_is_not_a_position(self):
        """gpsd can report a mode before it has a position."""
        assert gps_text(status(gps={"fix": True, "mode": 2,
                                    "latitude": None, "longitude": None,
                                    "sats_seen": 4, "sats_used": 0})
                        ) == "GPS: searching 0/4"

    def test_a_wide_position_gives_up_the_satellites_before_the_panel(self):
        """"GPS: -33.8688, -151.2093 3D 12/20" is 33 characters. The position
        is the line's reason to exist, so the counts drop first and the fix
        quality after them, rather than the text running off the panel."""
        wide = status(gps={"fix": True, "mode": 3, "latitude": -33.8688,
                           "longitude": -151.2093, "sats_used": 12,
                           "sats_seen": 20})
        assert gps_text(wide) == "GPS: -33.8688, -151.2093 3D"

    def test_southern_and_western_positions_still_fit(self):
        wide = status(gps={"fix": True, "mode": 3, "latitude": -33.8688,
                           "longitude": -151.2093, "sats_used": 12,
                           "sats_seen": 20})
        assert len(gps_text(wide)) <= PANEL_COLUMNS

    def test_a_fix_carries_position_quality_and_satellites_on_one_line(self):
        """Two GPS lines collapsed into one. The second said "3D 7/11" plus the
        GPS clock, and with a fix showing it duplicated what the position had
        already proven; without one it just repeated the search counts."""
        line = gps_text(status())
        assert line == "GPS: 51.2345, 6.7654 3D 7/11"
        assert len(line) <= PANEL_COLUMNS

    def test_a_two_dimensional_fix_says_so(self):
        assert "2D" in gps_text(status(gps={
            "fix": True, "mode": 2, "latitude": 51.2345, "longitude": 6.7654,
            "sats_used": 4, "sats_seen": 9}))

    def test_searching_reports_the_climb_from_seen_to_used(self):
        assert gps_text(status(gps={"fix": False, "sats_used": 0,
                                    "sats_seen": 12})) == "GPS: searching 0/12"

    def test_nothing_heard_at_all_is_said_plainly(self):
        assert gps_text(status(gps={})) == "GPS: no signal"

    def test_satellites_text_is_gone_with_its_second_line(self):
        assert not hasattr(ui, "satellites_text")


class TestPages:
    def test_the_blank_page_draws_nothing(self):
        """Blank is white: the button's off state, not a resting page."""
        assert ui.page_lines(status(page=ui.BLANK)) == []

    def test_the_button_toggles_the_page_against_blank(self):
        assert ui.TOGGLE_PAGE[ui.STATUS] == ui.BLANK
        assert ui.TOGGLE_PAGE[ui.BLANK] == ui.STATUS

    def test_there_is_no_scanning_page_any_more(self):
        assert not hasattr(ui, "SCANNING")
        assert not hasattr(ui, "scanning_page")

    def test_the_status_page_carries_everything_that_survived(self):
        lines = "\n".join(ui.page_lines(status(
            page=ui.STATUS, connected=True, ssid="localhost_2G", rssi=-52,
            ip="192.168.178.43", wifi_aps=42, wifi_saved=18,
            battery={"percent": 38, "volts": 3.72})))
        assert "Status:" in lines                        # what it is doing
        assert "GPS:" in lines                           # position or search
        assert "localhost_2G" in lines and "192.168.178.43" in lines
        assert "seen: 42 wifi, 18 saved" in lines       # wifi finds
        assert "3.72V" in lines and "38%" in lines       # battery, measured

    def test_the_status_page_drops_ble_and_the_upload_backlog(self):
        """Both lost their place to the battery line: seven lines is the lot."""
        lines = "\n".join(ui.page_lines(status(
            page=ui.STATUS, ble_devices=5, ble_saved=2,
            pending={"wigle": 2, "wdgwars": 1})))
        assert "BLE" not in lines
        assert "upload" not in lines

    def test_the_page_fits_the_panel(self):
        wide = status(page=ui.STATUS, connected=True,
                      ssid="a-very-long-network-name-indeed", rssi=-100,
                      ip="192.168.178.200", wifi_aps=99999, wifi_saved=99999,
                      battery={"percent": 100, "volts": 4.20})
        for page in (ui.BLANK, ui.STATUS):
            wide["page"] = page
            lines = ui.page_lines(wide)
            assert len(lines) <= PANEL_LINES, f"{page}: {len(lines)} lines"
            assert 2 + len(lines) * 16 <= 122, f"{page} too tall"
            for line in lines:
                assert len(line) <= PANEL_COLUMNS, f"{page}: {line!r}"

    def test_power_line_prefers_voltage_and_flags_charging(self):
        charging = ui.power_line(status(battery={"percent": 60, "volts": 3.85,
                                                 "charging": True},
                                        runtime="5h00"))
        assert "3.85V" in charging and "chg" in charging
        # Runtime is meaningless while charging, so it is not shown.
        assert "5h00" not in charging

    def test_power_line_without_a_ups(self):
        assert ui.power_line(status(battery=None)) == "Batt: no UPS"

    def test_not_connected_says_so_rather_than_inventing_a_link(self):
        assert ui.wifi_link_lines(status(connected=False)) == [
            "WiFi: not connected"]


class TestActivityAndSparseStatus:
    def test_activity_defaults_to_idle(self):
        assert activity_text({}) == "idle"

    def test_the_longest_activity_still_fits(self):
        line = f"Status: {activity_text(status(activity='waiting for gps'))}"
        assert len(line) <= PANEL_COLUMNS

    def test_pages_survive_a_sparse_status(self):
        """A status taken before the first scan must still render."""
        for page in (ui.BLANK, ui.STATUS):
            assert isinstance(ui.page_lines({"page": page}), list)
        assert "seen: 0 wifi, 0 saved" in ui.page_lines({"page": ui.STATUS})

    def test_counts_fit_the_panel_at_implausible_scale(self):
        lines = ui.page_lines(status(page=ui.STATUS, wifi_aps=99999,
                                     wifi_saved=99999))
        for line in lines:
            assert len(line) <= PANEL_COLUMNS


class WedgedPanel:
    """A panel that never finishes drawing.

    Waveshare's driver waits on the BUSY pin with no timeout, so this is the real
    hardware failure: not an exception, an unbounded block.
    """

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.slept = False

    def render(self, status):
        self.entered.set()
        self.release.wait()          # never released in these tests

    def sleep(self):
        self.slept = True


class RecordingPanel:
    def __init__(self):
        self.drawn = []
        self.slept = False

    def render(self, status):
        self.drawn.append(status)

    def sleep(self):
        self.slept = True


class TestBackgroundDisplay:
    def test_a_wedged_panel_does_not_block_the_caller(self):
        """The daemon's loop also pets the systemd watchdog, so blocking here
        would turn a dark screen into a box that gets restarted or stranded."""
        panel = WedgedPanel()
        screen = BackgroundDisplay(panel)
        screen.start()
        try:
            screen.render(status())
            assert panel.entered.wait(timeout=5), "worker never started drawing"
            for _ in range(100):
                screen.render(status())
        finally:
            panel.release.set()
            screen.stop(timeout=1)

    def test_stop_gives_up_on_a_wedged_panel(self):
        panel = WedgedPanel()
        screen = BackgroundDisplay(panel)
        screen.start()
        try:
            screen.render(status())
            panel.entered.wait(timeout=5)
            screen.stop(timeout=0.2)     # must return, not hang forever
        finally:
            panel.release.set()

    def test_only_the_newest_status_is_drawn(self):
        """There is no queue: a slow panel must not accumulate a backlog of
        frames to replay, because only the current status is worth showing."""
        panel = RecordingPanel()
        screen = BackgroundDisplay(panel)
        screen.start()
        try:
            for seen in range(50):
                screen.render(status(wifi_aps=seen))
            screen.stop(timeout=5)
        finally:
            screen.stop(timeout=1)
        assert panel.drawn, "nothing was drawn at all"
        assert len(panel.drawn) <= 50
        assert panel.drawn[-1]["wifi_aps"] == 49

    def test_stop_sleeps_a_healthy_panel(self):
        """Leaving the panel un-slept across a reboot is what makes the next
        init unreliable."""
        panel = RecordingPanel()
        screen = BackgroundDisplay(panel)
        screen.start()
        screen.render(status())
        screen.stop(timeout=5)
        assert panel.slept is True
