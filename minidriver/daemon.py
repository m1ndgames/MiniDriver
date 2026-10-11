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

from minidriver import wifi
from minidriver.statemachine import FIELD, HOME, ModeMachine
from minidriver.survey import SurveyLog
from minidriver import sweep

log = logging.getLogger(__name__)

CAPTURE_NAME = "survey.wiglewifi.csv"
SERVICES = ("wigle", "wdgwars", "wardrift")


def build_status(mode, gps, aps, ble, rows, pending, last_upload, ip) -> dict:
    """The status snapshot the display renders. Pure so it is easy to check."""
    gps = gps or {}
    return {
        "mode": mode,
        "gps": {"fix": bool(gps.get("fix") or gps.get("mode", 0) >= 2),
                "mode": gps.get("mode", 0),
                "sats_used": gps.get("sats_used", gps.get("satellites_used", 0)),
                "sats_seen": gps.get("sats_seen", gps.get("satellites_seen", 0))},
        "wifi_aps": len(aps or {}),
        "ble_devices": len(ble or []),
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
                 gps_read, display=None):
        self.cfg = cfg
        self.station = station
        self.ble = ble
        self.machine = machine
        self.ledger = ledger
        self._client_for = client_factory
        self._gps_read = gps_read
        self.display = display

        self.data_root = Path(cfg.get("paths", {}).get("data_dir",
                                                       "/var/lib/minidriver"))
        self._home_networks = cfg.get("network", {}).get("home_networks", [])
        self._country = cfg.get("network", {}).get("country")
        self._ble_enabled = cfg.get("ble", {}).get("enabled", True)

        self.gps = {}
        self.aps = {}
        self.survey = None
        self.ip = None
        self.last_upload = None
        self.pending = {s: 0 for s in SERVICES}
        self._stop = threading.Event()

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
        try:
            self.ble.clear()
        except Exception:                                  # noqa: BLE001
            log.debug("clearing ble failed", exc_info=True)
        self.station.enter_field()
        self._open_session(now_wall)
        log.info("entering FIELD; new session %s", self.survey.path)

    def _enter_home(self) -> None:
        if self.survey is not None:
            self.survey.close()
            self.survey = None
        log.info("home network in range; associating")
        self.station.associate(self._home_networks, country=self._country)
        self.ip = self.station.address()
        self._sweep_uploads()

    # ------------------------------------------------------------------ uploads
    def _captures(self) -> list:
        return sorted(self.data_root.glob(f"sessions/session_*/{CAPTURE_NAME}"))

    def _sweep_uploads(self) -> None:
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

    def _record(self, aps, now_wall, now_mono) -> None:
        if self.survey is None:
            return
        self.survey.record(aps, self.gps, now_wall, now_mono)
        if self._ble_enabled:
            self.survey.record(self.ble.snapshot(), self.gps, now_wall, now_mono)

    def status(self) -> dict:
        return build_status(self.machine.mode, self.gps, self.aps,
                            self.ble.snapshot() if self._ble_enabled else [],
                            self.survey.rows if self.survey else 0,
                            self.pending, self.last_upload,
                            self.ip if self.machine.mode == HOME else None)

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
        try:
            while not self._stop.is_set():
                self.poll()
                now = time.monotonic()
                if self.display is not None and now - last_draw >= refresh_gap:
                    last_draw = now
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
