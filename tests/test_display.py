"""The e-paper status dashboard.

The 2.13" panel is 250x122, monochrome and has no buttons, so the UI is
output-only. PIL and the Waveshare driver are hardware; what is tested is how a
status snapshot becomes the text that goes on the panel.

Layout:

    not connected                75%
    WIFI: 42
    BLE: 5
    GPS: 51.2345, 6.7654
    Status: scanning
"""
import threading

from minidriver.display.ui import (BackgroundDisplay, activity_text,
                                   battery_text, body_lines, connection_text,
                                   gps_text)

# 250px at 14px DejaVu Sans Mono is about 29 characters.
PANEL_COLUMNS = 29


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
        assert gps_text(status()) == "GPS: 51.2345, 6.7654"

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

    def test_southern_and_western_positions_still_fit(self):
        wide = status(gps={"fix": True, "mode": 3, "latitude": -33.8688,
                           "longitude": -151.2093})
        assert len(gps_text(wide)) <= PANEL_COLUMNS


class TestBody:
    def test_counts_wifi_and_ble_seen_and_saved(self):
        lines = body_lines(status(wifi_aps=42, wifi_saved=18,
                                  ble_devices=5, ble_saved=2))
        assert "WIFI: 42 (18 saved)" in lines
        assert "BLE: 5 (2 saved)" in lines

    def test_saving_nothing_is_visible(self):
        """Without a GPS fix nothing is written, and the panel should say so
        rather than implying the sightings were captured."""
        lines = body_lines(status(wifi_aps=42, wifi_saved=0))
        assert "WIFI: 42 (0 saved)" in lines

    def test_counts_fit_the_panel_at_implausible_scale(self):
        lines = body_lines(status(wifi_aps=99999, wifi_saved=99999,
                                  ble_devices=99999, ble_saved=99999))
        for line in lines:
            assert len(line) <= PANEL_COLUMNS

    def test_shows_the_activity(self):
        assert "Status: scanning" in body_lines(status(activity="scanning"))
        assert "Status: uploading" in body_lines(status(activity="uploading"))
        assert ("Status: waiting for gps"
                in body_lines(status(activity="waiting for gps")))

    def test_the_longest_activity_still_fits(self):
        line = f"Status: {activity_text(status(activity='waiting for gps'))}"
        assert len(line) <= PANEL_COLUMNS

    def test_activity_defaults_to_idle(self):
        assert activity_text({}) == "idle"
        assert "Status: idle" in body_lines(status(activity=None))

    def test_survives_a_sparse_status(self):
        """A status taken before the first scan must still render."""
        lines = body_lines({})
        assert isinstance(lines, list) and lines
        assert "WIFI: 0 (0 saved)" in lines and "BLE: 0 (0 saved)" in lines

    def test_every_line_fits_the_panel(self):
        for line in body_lines(status()):
            assert len(line) <= PANEL_COLUMNS

    def test_fits_the_panel_height(self):
        """Header plus body at 16px from y=2 must stay inside 122px."""
        lines = 1 + len(body_lines(status()))
        assert 2 + lines * 16 <= 122


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
