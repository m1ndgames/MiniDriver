"""The daemon: the loop that runs the box.

It owns the single radio through the Station, decides the mode with the
ModeMachine, writes the survey while in the field, and runs the upload sweep when
it reaches home. GPS and BLE are read on their own threads and polled here.

The collaborators are injected so the loop's behaviour is testable without
hardware; run() wires the real ones and does the timing and the systemd watchdog
heartbeat. poll() is one iteration and holds the logic worth testing: what a
field scan records, and what the switch into and out of home does.
"""
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from minidriver import battery as battery_mod
from minidriver import wifi
from minidriver.display import ui
from minidriver.gps import has_fix
from minidriver.statemachine import FIELD, HOME, ModeMachine
from minidriver.survey import SurveyLog
from minidriver import sweep

log = logging.getLogger(__name__)

CAPTURE_NAME = "survey.wiglewifi.csv"
# Associating tears the link down and back up, so a flapping network must not
# be able to make the box do it on every scan.
REASSOCIATE_GAP = 30.0
SERVICES = ("wigle", "wdgwars", "wardrift")


def build_status(mode, gps, aps, ble, rows, pending, last_upload, ip,
                 battery=None, activity=None, wifi_saved=0, ble_saved=0) -> dict:
    """The status snapshot the display renders. Pure so it is easy to check."""
    gps = gps or {}
    fix = has_fix(gps)
    if activity:
        current = activity
    elif mode != FIELD:
        current = "idle"
    else:
        # "scanning" would overstate it: without a fix nothing is written at
        # all, so what the box is really doing is waiting on the receiver.
        current = "scanning" if fix else "waiting for gps"
    return {
        "mode": mode,
        # Reachable means associated *and* addressed: an association with no
        # lease is not something you can SSH into.
        "connected": mode == HOME and bool(ip),
        "activity": current,
        "battery": battery,
        # The same test the survey writer uses, so the panel can never claim a
        # fix the capture is rejecting -- a cold-starting receiver reports
        # mode 2 at 0,0, which is not a position.
        "gps": {"fix": fix,
                "mode": gps.get("mode", 0),
                "latitude": gps.get("latitude"),
                "longitude": gps.get("longitude"),
                "sats_used": gps.get("sats_used", gps.get("satellites_used", 0)),
                "sats_seen": gps.get("sats_seen", gps.get("satellites_seen", 0))},
        "wifi_aps": len(aps or {}),
        "ble_devices": len(ble or []),
        # Seen and saved are different numbers, and the gap is the whole point:
        # nothing is written without a GPS fix, so "seeing plenty, saving none"
        # is the signature of a GPS problem rather than a radio problem.
        "wifi_saved": wifi_saved,
        "ble_saved": ble_saved,
        "rows": rows,
        "pending": dict(pending or {}),
        "last_upload": last_upload,
        "ip": ip,
    }


def session_name(now_wall: float = None) -> str:
    now_wall = time.time() if now_wall is None else now_wall
    return "session_" + datetime.fromtimestamp(
        now_wall, timezone.utc).strftime("%Y%m%d-%H%M%S")


class Daemon:
    def __init__(self, cfg, station, ble, machine, ledger, client_factory,
                 gps_read, display=None, battery_read=None):
        self.cfg = cfg
        self.station = station
        self.ble = ble
        self.machine = machine
        self.ledger = ledger
        self._client_for = client_factory
        self._gps_read = gps_read
        self._battery_read = battery_read
        self.display = display

        self.data_root = Path(cfg.get("paths", {}).get("data_dir",
                                                       "/var/lib/minidriver"))
        self._home_networks = cfg.get("network", {}).get("home_networks", [])
        self._country = cfg.get("network", {}).get("country")
        self._ble_enabled = cfg.get("ble", {}).get("enabled", True)

        self.gps = {}
        self.aps = {}
        # Unique sightings actually written this session, per kind.
        self.saved_wifi = set()
        self.saved_ble = set()
        self.survey = None
        self.ip = None
        self.last_upload = None
        self.pending = {s: 0 for s in SERVICES}
        self._activity = None
        self._associated_at = 0.0
        self._stop = threading.Event()
        # Button presses are counted, not signalled through an Event. next_page
        # runs in a signal handler on the main thread, and threading primitives
        # take locks the main thread may already hold -- setting an Event from
        # there can deadlock the process. A plain integer bump is atomic under
        # CPython and safe; the loop notices it on its next tick.
        self._presses = 0
        self._presses_seen = 0

        self.page = ui.BLANK
        self._page_at = 0.0
        self._page_timeout = float(cfg.get("display", {})
                                   .get("page_timeout_seconds", 15.0))
        self._runtime = battery_mod.RuntimeEstimate()

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._ble_enabled:
            self.ble.start()
        self._enter_field()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.ble.stop()
        except Exception:                                  # noqa: BLE001
            log.debug("stopping ble failed", exc_info=True)
        if self.survey is not None:
            self.survey.close()
        # Give the radio back last: a stopped daemon must leave the box
        # reachable, not holding an unassociated interface.
        try:
            self.station.teardown()
        except Exception:                                  # noqa: BLE001
            log.debug("releasing the radio failed", exc_info=True)

    # -------------------------------------------------------------------- modes
    def _open_session(self, now_wall: float = None) -> None:
        directory = self.data_root / "sessions" / session_name(now_wall)
        directory.mkdir(parents=True, exist_ok=True)
        self.survey = SurveyLog(directory / CAPTURE_NAME)

    def _enter_field(self, now_wall: float = None) -> None:
        self.aps = {}
        self.saved_wifi = set()
        self.saved_ble = set()
        try:
            self.ble.clear()
        except Exception:                                  # noqa: BLE001
            log.debug("clearing ble failed", exc_info=True)
        self.station.enter_field()
        self._open_session(now_wall)
        log.info("entering FIELD; new session %s", self.survey.path)

    def preferred_visible(self):
        """The highest-priority home network currently in range, or None.

        wpa_supplicant prefers the larger `priority`, and so do we -- but it
        only weighs them when it associates. Deciding this ourselves each scan
        is what lets the box follow a hotspot appearing or disappearing.
        """
        best, best_priority = None, None
        for entry in self._home_networks or []:
            ssid = (entry or {}).get("ssid")
            if ssid not in self.machine.present:
                continue
            priority = entry.get("priority") or 0
            if best is None or priority > best_priority:
                best, best_priority = ssid, priority
        return best

    def _associate(self, now_mono: float) -> None:
        self.station.associate(self._home_networks, country=self._country)
        self.ip = self.station.address()
        self._associated_at = now_mono

    def keep_home(self, now_mono: float) -> bool:
        """Re-associate when the link is not on the best available network.

        Covers two cases the mode machine cannot see, because neither changes
        which SSIDs are *visible*: a higher-priority network appearing after we
        joined a lesser one, and the joined network vanishing while another
        stays in range. The second is the nastier -- the interface keeps its
        address, so everything looks fine while nothing is reachable.
        """
        wanted = self.preferred_visible()
        if wanted is None:
            return False
        if self.station.link_ssid() == wanted:
            return False
        if now_mono - self._associated_at < REASSOCIATE_GAP:
            return False        # do not thrash a flapping link
        log.info("link is not on %s, which is in range and preferred; "
                 "re-associating", wanted)
        self._associate(now_mono)
        return True

    def _enter_home(self) -> None:
        if self.survey is not None:
            self.survey.close()
            self.survey = None
        log.info("home network in range; associating")
        self._associate(time.monotonic())
        self._sweep_uploads()

    # ------------------------------------------------------------------ uploads
    def _captures(self) -> list:
        return sorted(self.data_root.glob(f"sessions/session_*/{CAPTURE_NAME}"))

    def _sweep_uploads(self) -> None:
        # The sweep blocks this loop for as long as the uploads take, so push a
        # frame before starting: the panel draws on its own thread, so this is
        # the only way "uploading" is ever actually seen.
        self._activity = "uploading"
        self._refresh_panel()
        try:
            self._run_sweep()
        finally:
            self._activity = None

    # --------------------------------------------------------------- display
    def next_page(self) -> None:
        """Record a button press.

        Runs inside a signal handler, so it does the least possible: no locks,
        no clock, no drawing. Everything else happens in apply_presses() on the
        loop's own thread.
        """
        self._presses += 1

    def apply_presses(self, now_mono: float) -> bool:
        """Advance the page if the button was pressed since the last tick."""
        if self._presses == self._presses_seen:
            return False
        self._presses_seen = self._presses
        self.page = ui.NEXT_PAGE.get(self.page, ui.STATUS)
        self._page_at = now_mono
        log.info("button: panel page -> %s", self.page)
        return True

    def _expire_page(self, now_mono: float) -> bool:
        """Drop back to the resting page after the configured idle time."""
        if self.page == ui.BLANK or self._page_timeout <= 0:
            return False
        if now_mono - self._page_at < self._page_timeout:
            return False
        self.page = ui.BLANK
        log.info("panel idle for %.0fs; back to the blank page",
                 self._page_timeout)
        return True

    def _refresh_panel(self) -> None:
        if self.display is None:
            return
        try:
            self.display.render(self.status())
        except Exception:                                  # noqa: BLE001
            log.debug("could not refresh the panel", exc_info=True)

    def _run_sweep(self) -> None:
        captures = self._captures()
        summary = []
        for service in SERVICES:
            try:
                client = self._client_for(service)
                if client is None:
                    # No credentials configured for this service: nothing to do.
                    continue
                result = sweep.upload_pending(service, captures, self.ledger, client)
            except Exception as exc:                        # noqa: BLE001
                log.exception("sweep to %s failed", service)
                self.pending[service] = len(
                    sweep.pending_for(captures, service, self.ledger))
                summary.append(f"{service}: {exc}")
                continue
            self.pending[service] = len(
                sweep.pending_for(captures, service, self.ledger))
            if result["sent"]:
                summary.append(f"{service} +{result['sent']}")
        self.last_upload = ", ".join(summary) if summary else "nothing new"
        self._refresh_pending()

    def _refresh_pending(self) -> None:
        captures = self._captures()
        for service in SERVICES:
            self.pending[service] = len(
                sweep.pending_for(captures, service, self.ledger))

    # --------------------------------------------------------------------- poll
    def poll(self, now_wall: float = None, now_mono: float = None) -> None:
        """One iteration: scan, decide the mode, and act on it."""
        now_wall = time.time() if now_wall is None else now_wall
        now_mono = time.monotonic() if now_mono is None else now_mono

        self.gps = self._gps_read() or {}
        aps = wifi.parse_scan(self.station.scan(), now_wall)
        for ap in aps:
            self.aps[ap["mac"].lower()] = ap

        mode = self.machine.observe(wifi.visible_ssids(aps))
        if self.machine.changed:
            if mode == HOME:
                self._enter_home()
            else:
                self._enter_field(now_wall)
                self._record(aps, now_wall, now_mono)
            return

        if mode == FIELD:
            self._record(aps, now_wall, now_mono)
        else:
            # Already home: make sure we are on the best network in range.
            self.keep_home(now_mono)

    def _record(self, aps, now_wall, now_mono) -> None:
        if self.survey is None:
            return
        # record() returns the MACs it actually wrote, which is empty without a
        # fix -- that is what makes the panel's "saved" counts honest.
        self.saved_wifi.update(
            self.survey.record(aps, self.gps, now_wall, now_mono))
        if self._ble_enabled:
            self.saved_ble.update(
                self.survey.record(self.ble.snapshot(), self.gps, now_wall,
                                   now_mono))

    def home_link(self):
        """(ssid, rssi) of the home network we are on, from the last scan.

        Taken from scan data we already hold rather than another subprocess:
        the loop scans even at home, so the association's signal is already
        there to be read.
        """
        if self.machine.mode != HOME:
            return None, None
        for ssid in sorted(self.machine.present):
            for ap in self.aps.values():
                if wifi.ssid(ap) == ssid:
                    return ssid, ap.get("rssi")
            return ssid, None
        return None, None

    def status(self) -> dict:
        # Read only when a snapshot is wanted, which is the panel's refresh
        # cadence rather than every scan.
        battery = self._battery_read() if self._battery_read else None
        if battery:
            self._runtime.observe(time.monotonic(), battery.get("percent"),
                                  battery.get("charging")
                                  or battery.get("plugged"))
        ssid, rssi = self.home_link()
        snapshot = build_status(self.machine.mode, self.gps, self.aps,
                                self.ble.snapshot() if self._ble_enabled else [],
                                self.survey.rows if self.survey else 0,
                                self.pending, self.last_upload,
                                self.ip if self.machine.mode == HOME else None,
                                battery=battery, activity=self._activity,
                                wifi_saved=len(self.saved_wifi),
                                ble_saved=len(self.saved_ble))
        snapshot["page"] = self.page
        snapshot["ssid"] = ssid
        snapshot["rssi"] = rssi
        snapshot["runtime"] = battery_mod.format_runtime(
            self._runtime.seconds_left())
        return snapshot

    # ---------------------------------------------------------------------- run
    def run(self) -> None:
        """Real loop: scan on a cadence, redraw the panel, pet the watchdog."""
        scan_gap = float(self.cfg.get("scan", {}).get("field_scan_seconds", 3.0))
        refresh_gap = float(self.cfg.get("display", {}).get("refresh_seconds", 30.0))
        notify = _sd_notifier()
        self.start()
        if notify:
            notify("READY=1")
        last_draw = 0.0
        last_page = self.page
        try:
            while not self._stop.is_set():
                self.poll()
                now = time.monotonic()
                # The page can change between ticks, from the button or from
                # the idle timeout, and either has to redraw now rather than
                # wait out the refresh cadence.
                self.apply_presses(now)
                self._expire_page(now)
                page_changed = self.page != last_page
                if self.display is not None and (
                        page_changed or now - last_draw >= refresh_gap):
                    last_draw = now
                    last_page = self.page
                    self.display.render(self.status())
                if notify:
                    notify("WATCHDOG=1")
                self._stop.wait(scan_gap)
        finally:
            self.stop()


def _sd_notifier():
    """A function that sends systemd notifications, or None off systemd.

    The service runs Type=notify with a watchdog; petting it is how a hung daemon
    gets restarted instead of silently stranding the box away from home.
    """
    import os
    import socket

    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return None
    if address.startswith("@"):
        address = "\0" + address[1:]

    def notify(message: str) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(address)
                sock.sendall(message.encode())
        except OSError:
            log.debug("sd_notify failed", exc_info=True)

    return notify
