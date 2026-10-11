"""BLE advertisement scanning on the internal Bluetooth radio.

The GPS HAT freed the Bluetooth radio (it is not needed for GPS), so the box
scans Bluetooth LE advertisements and folds every device it hears into the same
WigleWifi survey as the WiFi scan, marked Type=BLE. All three services count
them.

Scanning uses bleak, which talks to the system BlueZ over D-Bus. bleak is async;
it runs on its own event loop on a daemon thread, and each advertisement updates
a table of the latest sighting per device. The daemon polls snapshot() on its own
cadence, exactly as it polls the WiFi scan. bleak is imported lazily so the pure
logic here imports on any OS for testing.
"""
import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def to_sighting(address: str, name, rssi, now_wall: float = None) -> dict:
    """Shape a discovered BLE device into the sighting survey.wigle_row expects.

    A BLE advertisement was, by definition, just heard, so last_seen is now --
    the survey writer treats it as a fresh sighting, the same as a scanned AP.
    """
    now_wall = time.time() if now_wall is None else now_wall
    stamp = datetime.fromtimestamp(now_wall, timezone.utc).isoformat()
    return {"mac": address, "name": (name or "").strip(), "rssi": rssi,
            "kind": "ble", "last_seen": stamp}


class BleScanner:
    """Keeps the latest advertisement heard from each BLE device.

    snapshot() returns the current sightings for the daemon to fold into the
    survey. The bleak scan runs on a background thread; _record is the seam the
    detection callback calls and the tests drive directly.
    """

    def __init__(self, clock=time.time):
        self._clock = clock
        self._devices = {}
        self._lock = threading.Lock()
        self._thread = None
        self._loop = None
        self._stop = threading.Event()

    def _record(self, address: str, name, rssi) -> None:
        with self._lock:
            self._devices[address.lower()] = to_sighting(address, name, rssi,
                                                         self._clock())

    def snapshot(self) -> list:
        with self._lock:
            return list(self._devices.values())

    def clear(self) -> None:
        with self._lock:
            self._devices.clear()

    # ------------------------------------------------------------- the radio
    def start(self) -> None:
        """Begin scanning on a background thread. Safe to call once."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ble-scan",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        loop, self._loop = self._loop, None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    def _run(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._scan())
        except Exception:                                  # noqa: BLE001
            log.exception("ble scan loop stopped")

    async def _scan(self) -> None:
        # Imported here so the module loads without bleak on a dev machine.
        from bleak import BleakScanner

        def detected(device, advertisement_data):
            # RSSI moved onto advertisement_data in recent bleak; fall back to
            # the device for older versions.
            rssi = getattr(advertisement_data, "rssi", None)
            if rssi is None:
                rssi = getattr(device, "rssi", None)
            self._record(device.address, advertisement_data.local_name, rssi)

        scanner = BleakScanner(detection_callback=detected)
        await scanner.start()
        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.5)
        finally:
            await scanner.stop()
