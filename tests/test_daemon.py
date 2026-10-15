"""The daemon: the loop that ties scanning, the mode machine, the survey and the
upload sweep together. Threads and sleeps live in run(); the behaviour that
matters -- what a field poll records, and what happens when home appears -- is
driven here one poll at a time with fake collaborators.
"""
from minidriver.display import ui
from minidriver.daemon import CAPTURE_NAME, Daemon, build_status
from minidriver.statemachine import HOME, ModeMachine
from minidriver.survey import COLUMNS, VERSION
from minidriver.uploads import UploadLedger

# A scan that includes the home network "HomeNet".
from tests.test_wifi import SCAN_TEXT

FIX = {"mode": 3, "latitude": 51.2, "longitude": 6.7, "altitude": 40.0,
       "accuracy": 4.5, "fix": True, "sats_used": 7, "sats_seen": 11}
NO_HOME_SCAN = """\
BSS aa:bb:cc:00:00:09(on wlan0)
	freq: 2412.0
	signal: -60.00 dBm
	SSID: Stranger
	capability: ESS Privacy (0x0411)
"""


class FakeStation:
    def __init__(self, scan_text=""):
        self.scan_text = scan_text
        self.linked = None          # SSID the link reports, None when down
        self.associated = []
        self.field_calls = 0
        self.teardown_calls = 0

    def scan(self):
        return self.scan_text

    def enter_field(self):
        self.field_calls += 1

    def associate(self, networks, country=None):
        self.associated.append((networks, country))

    def link_ssid(self):
        return self.linked

    def address(self):
        return "192.168.1.9"

    def teardown(self):
        self.teardown_calls += 1


class FakeBle:
    def snapshot(self):
        return []

    def start(self):
        pass

    def stop(self):
        pass

    def clear(self):
        pass


class FakeClient:
    def __init__(self, service):
        self.service = service
        self.uploaded = []

    def upload(self, path):
        self.uploaded.append(path)
        return {"ok": True}


def make_daemon(tmp_path, station, home=("HomeNet",), confirm=1, clients=None):
    clients = clients if clients is not None else {}
    cfg = {
        "network": {"interface": "wlan0", "country": "DE",
                    "home_networks": [{"ssid": s} for s in home]},
        "scan": {"home_confirm_scans": confirm, "home_lost_checks": 3},
        "ble": {"enabled": False},
        "paths": {"data_dir": str(tmp_path)},
    }
    machine = ModeMachine(home, confirm_scans=confirm, lost_checks=3)
    ledger = UploadLedger(tmp_path / "db")

    def factory(service):
        return clients.setdefault(service, FakeClient(service))

    return Daemon(cfg, station=station, ble=FakeBle(), machine=machine,
                  ledger=ledger, client_factory=factory,
                  gps_read=lambda: FIX)


def located_session(tmp_path, name="session_old"):
    """A past session directory with one located row, waiting to be uploaded."""
    d = tmp_path / "sessions" / name
    d.mkdir(parents=True)
    (d / CAPTURE_NAME).write_text(
        VERSION + "\n" + ",".join(COLUMNS) + "\n"
        + "AA:BB:CC:00:00:01,net,,,,,,51.2,6.7,,,,,WIFI\n")
    return d


class TestStopReleasesTheRadio:
    def test_stopping_hands_the_radio_back(self, tmp_path):
        """A stopped daemon must leave the box reachable. It holds wlan0
        unassociated while it runs, so without this a `systemctl stop` on a
        WiFi-only box would take it off the network for good."""
        station = FakeStation()
        daemon = make_daemon(tmp_path, station)
        daemon.start()
        daemon.stop()
        assert station.teardown_calls == 1


class TestFollowsTheBestNetwork:
    """wpa_supplicant only weighs priorities when it associates, so a hotspot
    appearing or vanishing afterwards leaves the box on the wrong network --
    or on a dead link while a good one is in range. Neither changes which
    SSIDs are *visible*, so the mode machine cannot see it."""

    def daemon_at_home(self, tmp_path, linked, present):
        station = FakeStation()
        station.linked = linked
        daemon = make_daemon(tmp_path, station,
                             home=("iPhone von Florian", "localhost_2G"))
        daemon._home_networks = [
            {"ssid": "iPhone von Florian", "priority": 10},
            {"ssid": "localhost_2G", "priority": 5},
        ]
        daemon.machine.present = set(present)
        return daemon, station

    def test_it_switches_to_a_higher_priority_network_that_appears(self, tmp_path):
        daemon, station = self.daemon_at_home(
            tmp_path, linked="localhost_2G",
            present=["localhost_2G", "iPhone von Florian"])
        assert daemon.keep_home(1000.0) is True
        assert station.associated, "should have re-associated"

    def test_it_returns_home_when_the_hotspot_disappears(self, tmp_path):
        """The interface keeps its address, so nothing else notices."""
        daemon, station = self.daemon_at_home(
            tmp_path, linked=None, present=["localhost_2G"])
        assert daemon.keep_home(1000.0) is True
        assert station.associated

    def test_it_leaves_a_correct_link_alone(self, tmp_path):
        daemon, station = self.daemon_at_home(
            tmp_path, linked="iPhone von Florian",
            present=["localhost_2G", "iPhone von Florian"])
        assert daemon.keep_home(1000.0) is False
        assert not station.associated

    def test_it_does_not_thrash_a_flapping_link(self, tmp_path):
        """Associating tears the link down and back up; doing that every scan
        would be worse than the problem."""
        daemon, station = self.daemon_at_home(
            tmp_path, linked=None, present=["localhost_2G"])
        daemon._associated_at = 1000.0
        assert daemon.keep_home(1001.0) is False
        assert not station.associated

    def test_nothing_in_range_is_not_its_business(self, tmp_path):
        daemon, station = self.daemon_at_home(tmp_path, linked=None, present=[])
        assert daemon.keep_home(1000.0) is False
        assert not station.associated

    def test_preferred_visible_picks_the_highest_priority_in_range(self, tmp_path):
        daemon, _ = self.daemon_at_home(
            tmp_path, linked=None,
            present=["localhost_2G", "iPhone von Florian"])
        assert daemon.preferred_visible() == "iPhone von Florian"
        daemon.machine.present = {"localhost_2G"}
        assert daemon.preferred_visible() == "localhost_2G"


class TestPanelPages:
    def press(self, daemon, at=0.0):
        """A button press, then the loop tick that acts on it."""
        daemon.next_page()
        daemon.apply_presses(at)

    def test_the_button_cycles_blank_status_scanning_status(self, tmp_path):
        daemon = make_daemon(tmp_path, FakeStation())
        assert daemon.page == ui.BLANK          # rests blank at start
        self.press(daemon); assert daemon.page == ui.STATUS
        self.press(daemon); assert daemon.page == ui.SCANNING
        self.press(daemon); assert daemon.page == ui.STATUS

    def test_the_handler_itself_touches_no_locks(self, tmp_path):
        """next_page runs in a signal handler on the main thread. Taking a lock
        there can deadlock against the loop holding it, so the handler only
        bumps a counter and the loop does the work."""
        daemon = make_daemon(tmp_path, FakeStation())
        daemon.next_page()
        assert daemon.page == ui.BLANK          # nothing changed yet
        assert daemon.apply_presses(0.0) is True
        assert daemon.page == ui.STATUS
        assert daemon.apply_presses(0.0) is False   # no press, no redraw

    def test_it_returns_to_blank_after_the_timeout(self, tmp_path):
        daemon = make_daemon(tmp_path, FakeStation())
        daemon._page_timeout = 15.0
        self.press(daemon)
        daemon._page_at = 100.0
        assert daemon._expire_page(110.0) is False      # still within 15s
        assert daemon.page == ui.STATUS
        assert daemon._expire_page(116.0) is True       # past it
        assert daemon.page == ui.BLANK

    def test_an_already_blank_panel_does_not_keep_redrawing(self, tmp_path):
        """Expiring an expired page would mark the panel dirty every tick and
        burn a refresh each time, which e-paper does not forgive."""
        daemon = make_daemon(tmp_path, FakeStation())
        assert daemon._expire_page(10_000.0) is False

    def test_a_zero_timeout_keeps_the_page_up(self, tmp_path):
        daemon = make_daemon(tmp_path, FakeStation())
        daemon._page_timeout = 0
        self.press(daemon)
        assert daemon._expire_page(10_000.0) is False
        assert daemon.page == ui.STATUS



class TestBuildStatus:
    def test_reflects_mode_counts_and_gps(self):
        st = build_status(mode="field", gps=FIX, aps={"a": 1, "b": 2},
                          ble=[1], rows=10, pending={"wigle": 3}, last_upload=None,
                          ip=None)
        assert st["mode"] == "field"
        assert st["wifi_aps"] == 2 and st["ble_devices"] == 1 and st["rows"] == 10
        assert st["gps"]["fix"] is True

    def test_null_island_is_not_reported_as_a_fix(self):
        """The panel must use the same test as the survey writer, or it claims a
        fix the capture is rejecting. A cold start sits at mode 2 / 0,0."""
        cold = {"mode": 2, "latitude": 0.0, "longitude": 0.0,
                "sats_used": 3, "sats_seen": 11}
        st = build_status(mode="field", gps=cold, aps={}, ble=[], rows=0,
                          pending={}, last_upload=None, ip=None)
        assert st["gps"]["fix"] is False

    def test_carries_the_position_through_for_the_panel(self):
        st = build_status(mode="field", gps=FIX, aps={}, ble=[], rows=0,
                          pending={}, last_upload=None, ip=None)
        assert st["gps"]["latitude"] == FIX["latitude"]
        assert st["gps"]["longitude"] == FIX["longitude"]

    def test_connected_means_associated_and_addressed(self):
        """An association with no lease is not something you can SSH into."""
        home = dict(gps=FIX, aps={}, ble=[], rows=0, pending={}, last_upload=None)
        assert build_status(mode="home", ip="192.168.1.9", **home)["connected"]
        assert not build_status(mode="home", ip=None, **home)["connected"]
        assert not build_status(mode="field", ip="192.168.1.9", **home)["connected"]

    def test_activity_follows_the_mode_unless_overridden(self):
        common = dict(gps=FIX, aps={}, ble=[], rows=0, pending={},
                      last_upload=None, ip=None)
        assert build_status(mode="field", **common)["activity"] == "scanning"
        assert build_status(mode="home", **common)["activity"] == "idle"
        assert build_status(mode="home", activity="uploading",
                            **common)["activity"] == "uploading"

    def test_scanning_without_a_fix_is_waiting_for_gps(self):
        """Nothing is written without a fix, so "scanning" would overstate what
        the box is achieving -- it is waiting on the receiver."""
        no_fix = {"mode": 1, "latitude": None, "longitude": None,
                  "sats_used": 0, "sats_seen": 9}
        common = dict(aps={}, ble=[], rows=0, pending={}, last_upload=None,
                      ip=None)
        assert build_status(mode="field", gps=no_fix,
                            **common)["activity"] == "waiting for gps"
        # Null Island counts as no fix, so it reads the same way.
        cold = {"mode": 2, "latitude": 0.0, "longitude": 0.0}
        assert build_status(mode="field", gps=cold,
                            **common)["activity"] == "waiting for gps"
        # At home it is idle regardless of the receiver.
        assert build_status(mode="home", gps=no_fix,
                            **common)["activity"] == "idle"


class TestFieldPoll:
    def test_a_field_poll_records_scanned_aps(self, tmp_path):
        station = FakeStation(scan_text=NO_HOME_SCAN)
        daemon = make_daemon(tmp_path, station)
        daemon.start()
        daemon.poll(now_wall=1_790_000_000.0, now_mono=100.0)
        assert len(daemon.aps) == 1                   # the one stranger AP
        assert daemon.survey.rows >= 1                # written with a fix
        daemon.stop()


class TestHomeSwitch:
    def test_home_appearing_associates_and_uploads(self, tmp_path):
        located_session(tmp_path)                     # a capture waiting to go
        clients = {}
        station = FakeStation(scan_text=SCAN_TEXT)     # SCAN_TEXT contains HomeNet
        daemon = make_daemon(tmp_path, station, home=("HomeNet",), confirm=1,
                             clients=clients)
        daemon.start()
        daemon.poll(now_wall=1_790_000_000.0, now_mono=100.0)
        assert daemon.machine.mode == HOME
        assert station.associated, "should have associated to the home network"
        # The waiting located capture went to every configured service.
        assert clients["wigle"].uploaded
        daemon.stop()

    def test_returning_to_the_field_opens_a_new_session(self, tmp_path):
        station = FakeStation(scan_text=SCAN_TEXT)
        daemon = make_daemon(tmp_path, station, home=("HomeNet",), confirm=1)
        daemon.start()
        first_session = daemon.survey.path
        daemon.poll(1_790_000_000.0, 100.0)           # -> HOME
        station.scan_text = NO_HOME_SCAN
        for i in range(3):                             # home lost for lost_checks
            daemon.poll(1_790_000_010.0 + i, 110.0 + i)
        assert daemon.machine.mode != HOME
        assert daemon.survey.path != first_session    # a fresh capture file
        daemon.stop()
