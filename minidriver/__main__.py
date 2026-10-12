"""Entry point: wire the real collaborators and run the daemon.

    python3 -m minidriver

Builds the Station that owns the radio, the BLE scanner, the mode machine from
the configured home networks, the upload ledger, and a client factory that reads
service credentials from config.yaml -- then hands them to the daemon and runs.
"""
import logging
import signal
import sys

from minidriver import nm
from minidriver.ble import BleScanner
from minidriver.config import DEFAULT_CONFIG_PATH, load_config
from minidriver.daemon import Daemon
from minidriver.display.ui import BackgroundDisplay, Display
from minidriver.gps import read_gpsd
from minidriver.statemachine import ModeMachine
from minidriver.station import Station
from minidriver.uploads import (UploadLedger, WardriftClient, WdgwarsClient,
                                WigleClient)

log = logging.getLogger(__name__)


def _client_factory(services: dict):
    """Return a factory that builds a service client, or None when that service
    has no credentials configured (so an unconfigured service is simply skipped,
    not retried into an error on every trip home)."""
    def factory(service):
        try:
            if service == "wigle":
                token = services.get("wigle_token", "")
                return WigleClient(token) if token else None
            if service == "wdgwars":
                key = services.get("wdgwars_key", "")
                return WdgwarsClient(key) if key else None
            if service == "wardrift":
                user = services.get("wardrift_username", "")
                pw = services.get("wardrift_password", "")
                return WardriftClient(user, pw) if user and pw else None
        except ValueError:
            return None
        return None
    return factory


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(DEFAULT_CONFIG_PATH)

    network = cfg.get("network", {})

    if "--release" in (argv if argv is not None else sys.argv[1:]):
        # ExecStopPost hook. The daemon's own cleanup cannot run after a SIGKILL
        # or a hard crash, so the unit calls this unconditionally once the
        # process is gone: whatever happened, the radio goes back.
        nm.release(network.get("interface", "wlan0"))
        return 0

    scan = cfg.get("scan", {})
    home = network.get("home_networks", [])
    home_ssids = [n.get("ssid") for n in home if n.get("ssid")]

    station = Station(network.get("interface", "wlan0"))
    machine = ModeMachine(home_ssids,
                          confirm_scans=scan.get("home_confirm_scans", 2),
                          lost_checks=scan.get("home_lost_checks", 3))
    ledger = UploadLedger(
        f"{cfg.get('paths', {}).get('data_dir', '/var/lib/minidriver')}/minidriver.db")
    display_cfg = cfg.get("display", {})
    screen = None
    if display_cfg.get("enabled", True):
        # Wrapped so the panel draws on its own thread: the driver blocks on the
        # panel's BUSY pin without a timeout, and the loop below also sends the
        # watchdog heartbeat.
        screen = BackgroundDisplay(Display(rotate=display_cfg.get("rotate", 0)))
        screen.start()
    else:
        log.info("display disabled by config")

    daemon = Daemon(cfg, station=station, ble=BleScanner(), machine=machine,
                    ledger=ledger,
                    client_factory=_client_factory(cfg.get("services", {})),
                    gps_read=read_gpsd, display=screen)

    def shutdown(*_):
        log.info("shutting down")
        daemon.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("minidriver starting; home networks: %s", home_ssids or "none set")
    try:
        daemon.run()
    finally:
        if screen is not None:
            # Sleeping the panel is what keeps the next init reliable; stop()
            # gives up rather than hanging shutdown on a panel that has stopped
            # answering.
            screen.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
