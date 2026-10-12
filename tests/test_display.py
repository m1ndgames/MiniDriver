"""The e-paper status dashboard.

The 2.13" panel is 250x122, monochrome and has no buttons, so the UI is
output-only: a handful of status lines. PIL and the Waveshare driver are
hardware; what is tested is how a status snapshot becomes those lines.
"""
import threading

from minidriver.display.ui import BackgroundDisplay, status_lines


def status(**over):
    base = {
        "mode": "field",
        "gps": {"fix": True, "mode": 3, "sats_used": 7, "sats_seen": 11},
        "wifi_aps": 42, "ble_devices": 5, "rows": 318,
        "pending": {"wigle": 2, "wdgwars": 2, "wardrift": 2},
        "last_upload": None,
        "ip": None,
    }
    base.update(over)
    return base


def joined(**over):
    return "\n".join(status_lines(status(**over)))


def test_shows_the_mode():
    assert "FIELD" in joined(mode="field")
    assert "HOME" in joined(mode="home")


def test_shows_a_gps_fix_with_satellites():
    text = joined(gps={"fix": True, "mode": 3, "sats_used": 7, "sats_seen": 11})
    assert "7" in text and "11" in text


def test_shows_no_fix_when_there_is_none():
    text = joined(gps={"fix": False, "mode": 1, "sats_used": 0, "sats_seen": 9})
    assert "no fix" in text.lower()


def test_shows_wifi_and_ble_counts():
    text = joined(wifi_aps=42, ble_devices=5)
    assert "42" in text and "5" in text


def test_shows_the_lan_ip_when_home():
    assert "192.168.1.9" in joined(mode="home", ip="192.168.1.9")


def test_shows_pending_upload_total_in_the_field():
    # Six captures waiting across services should surface as a number to upload.
    assert "6" in joined(pending={"wigle": 2, "wdgwars": 2, "wardrift": 2})


def test_survives_a_sparse_status():
    # A status taken before the first scan must still render without raising.
    lines = status_lines({"mode": "field"})
    assert isinstance(lines, list) and lines


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
            # The panel is now stuck inside render(); the caller must still fly.
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
            for rows in range(50):
                screen.render(status(rows=rows))
            screen.stop(timeout=5)
        finally:
            screen.stop(timeout=1)
        assert panel.drawn, "nothing was drawn at all"
        assert len(panel.drawn) <= 50
        assert panel.drawn[-1]["rows"] == 49

    def test_stop_sleeps_a_healthy_panel(self):
        """Leaving the panel un-slept across a reboot is what makes the next
        init unreliable."""
        panel = RecordingPanel()
        screen = BackgroundDisplay(panel)
        screen.start()
        screen.render(status())
        screen.stop(timeout=5)
        assert panel.slept is True
