"""The e-paper status dashboard.

The Waveshare 2.13" V4 panel is 250x122, one bit per pixel, and has no buttons,
so the whole UI is a glance: mode, whether the GPS has a fix, how much the
current session has found, what is waiting to upload, and -- when the box is home
-- its LAN address, so you can find and SSH it.

status_lines() turns a status snapshot into the lines to draw and is pure. The
Display class draws them with PIL onto the Waveshare driver and is imported
lazily, because PIL and the driver only exist on the box. E-paper wears a little
with every refresh, so the daemon redraws on a cadence, not on every change.
"""
import logging
import threading

log = logging.getLogger(__name__)


def connection_text(status: dict) -> str:
    """Top left: whether the box is on a network and reachable."""
    return "connected" if (status or {}).get("connected") else "not connected"


def battery_text(status: dict) -> str:
    """Top right: the battery level, blank when there is no UPS to read.

    A bare "+" means charging: the panel is one-bit monochrome, so a single
    character costs far less width than a word and still reads at a glance.
    """
    battery = (status or {}).get("battery") or {}
    percent = battery.get("percent")
    if percent is None:
        return ""
    return f"{'+' if battery.get('charging') else ''}{percent}%"


def gps_text(status: dict) -> str:
    """The position when there is a fix, otherwise the search progress.

    The position is the one number worth reading off the panel mid-drive, and it
    doubles as proof the survey is recording, since nothing is written without a
    fix. Without one, satellite counts are what answer the actual question --
    "is this getting anywhere, or is the antenna blind?" -- which a bare
    "no fix" does not. A cold start can take minutes, so watching used climb
    towards seen is the difference between waiting and giving up.
    """
    gps = (status or {}).get("gps") or {}
    latitude, longitude = gps.get("latitude"), gps.get("longitude")
    if gps.get("fix") and latitude is not None and longitude is not None:
        # Four decimals is about 11m: finer would not fit and would not be read.
        return f"GPS: {latitude:.4f}, {longitude:.4f}"
    seen = gps.get("sats_seen") or 0
    if not seen:
        # Nothing heard at all: no antenna, no sky, or no gpsd.
        return "GPS: no signal"
    return f"GPS: searching {gps.get('sats_used') or 0}/{seen}"


def activity_text(status: dict) -> str:
    return str((status or {}).get("activity") or "idle")


BLANK, STATUS, SCANNING = "blank", "status", "scanning"
# Blank is the resting page. Once woken, the button toggles the two content
# pages; the inactivity timeout drops back to blank from wherever you are.
NEXT_PAGE = {BLANK: STATUS, STATUS: SCANNING, SCANNING: STATUS}


def satellites_text(status: dict) -> str:
    """Fix quality, satellites and GPS time -- the detail page's GPS line."""
    gps = (status or {}).get("gps") or {}
    quality = {2: "2D", 3: "3D"}.get(gps.get("mode"), "--")
    line = f"{quality} {gps.get('sats_used', 0)}/{gps.get('sats_seen', 0)}"
    when = gps.get("time")
    return f"{line}  {when}" if when else line


def wifi_link_lines(status: dict) -> list:
    """The home link, when there is one. Two lines so the IP has room."""
    status = status or {}
    if not status.get("connected"):
        return ["WiFi: not connected"]
    ssid = status.get("ssid") or "?"
    rssi = status.get("rssi")
    signal = f" {rssi}dBm" if rssi is not None else ""
    return [f"WiFi: {ssid}"[:29], f"  {status.get('ip') or '...'}{signal}"[:29]]


def power_line(status: dict) -> str:
    """Voltage, charge and estimated runtime in one line.

    Voltage is shown alongside the percentage on purpose: the percentage is an
    estimate from a curve, the voltage is measured, and when they disagree the
    voltage is the one to believe.
    """
    battery = (status or {}).get("battery") or {}
    percent = battery.get("percent")
    if percent is None:
        return "Batt: no UPS"
    volts = battery.get("volts")
    parts = [f"{volts:.2f}V" if volts is not None else "", f"{percent}%"]
    if battery.get("charging"):
        parts.append("chg")
    elif status.get("runtime"):
        parts.append(f"~{status['runtime']}")
    return "Batt: " + " ".join(p for p in parts if p)


def status_page(status: dict) -> list:
    """Page 2: what the box is doing, and the state of its three subsystems."""
    status = status or {}
    return ([f"Status: {activity_text(status)}",
             gps_text(status),
             f"  {satellites_text(status)}"]
            + wifi_link_lines(status)
            + [power_line(status)])


def scanning_page(status: dict) -> list:
    """Page 3: what the survey has collected and what is waiting to go out."""
    status = status or {}
    pending = status.get("pending") or {}
    waiting = sum(int(v or 0) for v in pending.values())
    return [
        "Scanning",
        "",
        # Seen and saved both, because nothing is written without a GPS fix:
        # "42 (0 saved)" says the radio is fine and the GPS is not.
        f"WIFI: {status.get('wifi_aps', 0)} ({status.get('wifi_saved', 0)} saved)",
        f"BLE: {status.get('ble_devices', 0)} ({status.get('ble_saved', 0)} saved)",
        f"rows: {status.get('rows', 0)}",
        f"to upload: {waiting}",
    ]


def page_lines(status: dict) -> list:
    """The lines for whichever page is showing. Blank draws nothing at all."""
    page = (status or {}).get("page", BLANK)
    if page == STATUS:
        return status_page(status)
    if page == SCANNING:
        return scanning_page(status)
    return []


LINE_HEIGHT = 16
MARGIN = 4


class Display:
    """Draws the dashboard onto the Waveshare e-paper panel.

    Hardware-only: PIL and the waveshare_epd driver are imported on first use so
    this module loads on a dev machine. A failure to reach the panel is logged
    and swallowed -- the box must keep capturing with a dark screen.
    """

    def __init__(self, rotate: int = 0, full_refresh_every: int = 20):
        self.rotate = rotate
        # Partial updates accumulate ghosting, so a full refresh is forced
        # periodically to clear it. At the daemon's 30s cadence the default is
        # roughly every ten minutes.
        self.full_refresh_every = max(1, int(full_refresh_every))
        self._epd = None
        self._font = None
        self._since_full = None     # None until a base image has been set

    def _ensure(self) -> bool:
        if self._epd is not None:
            return True
        try:
            from waveshare_epd import epd2in13_V4
            from PIL import ImageFont
            self._epd = epd2in13_V4.EPD()
            self._epd.init()
            self._epd.Clear(0xFF)
            try:
                self._font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 14)
            except OSError:
                self._font = ImageFont.load_default()
            return True
        except Exception:                                  # noqa: BLE001
            log.exception("e-paper panel unavailable")
            self._epd = None
            return False

    def _width_of(self, draw, text: str) -> float:
        """Rendered width of *text*, for right-aligning the battery."""
        try:
            return draw.textlength(text, font=self._font)
        except Exception:                                  # noqa: BLE001
            # Pillow too old for textlength: the font is monospace, so an
            # estimate is close enough to keep the text on the panel.
            return len(text) * 8.4

    def _compose(self, status: dict):
        from PIL import Image, ImageDraw
        # The panel's native buffer is width=height_px; draw landscape and let
        # the driver orient it.
        width, height = self._epd.height, self._epd.width
        image = Image.new("1", (width, height), 0xFF)
        draw = ImageDraw.Draw(image)

        y = 2
        for line in page_lines(status):
            if line:
                draw.text((MARGIN, y), line, font=self._font, fill=0)
            y += LINE_HEIGHT

        if self.rotate:
            image = image.rotate(self.rotate, expand=False)
        return image

    def render(self, status: dict) -> None:
        if not self._ensure():
            return
        try:
            buffer = self._epd.getbuffer(self._compose(status))
            # The first frame, and every full_refresh_every-th one, is written
            # as a new base image: that is a full refresh, which both re-bases
            # the partial updates and clears their ghosting.
            if self._since_full is None or self._since_full >= self.full_refresh_every:
                self._epd.displayPartBaseImage(buffer)
                self._since_full = 0
            else:
                self._epd.displayPartial(buffer)
            self._since_full += 1
        except Exception:                                  # noqa: BLE001
            log.exception("could not draw the panel")

    def sleep(self) -> None:
        if self._epd is not None:
            try:
                self._epd.sleep()
            except Exception:                              # noqa: BLE001
                log.debug("could not sleep the panel", exc_info=True)


class BackgroundDisplay:
    """Draws on a worker thread, so a wedged panel cannot stall the daemon.

    The Waveshare driver waits on the panel's BUSY pin in an unbounded loop, so a
    panel that never reports ready blocks its caller forever rather than raising.
    The daemon's loop also scans and sends the systemd watchdog heartbeat, so
    blocking there turns a dark screen into a box that gets killed by the
    watchdog with its radio already taken -- which is to say, into a box nobody
    can reach.

    Only the newest status is kept. There is no queue, because a backlog of stale
    frames is worth nothing: if drawing falls behind, the right thing to show
    when it catches up is the current state, not the history.
    """

    def __init__(self, display):
        self._display = display
        self._lock = threading.Lock()
        self._pending = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # A daemon thread on purpose: if the panel is wedged in a driver call
        # that never returns, the process must still be able to exit.
        self._thread = threading.Thread(target=self._run, name="display",
                                        daemon=True)
        self._thread.start()

    def render(self, status: dict) -> None:
        """Hand over the latest status. Never blocks on the panel."""
        with self._lock:
            self._pending = status
        self._wake.set()

    def _take(self):
        with self._lock:
            status, self._pending = self._pending, None
        return status

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait()
            self._wake.clear()
            status = self._take()
            if status is None:
                continue
            try:
                self._display.render(status)
            except Exception:                              # noqa: BLE001
                log.exception("rendering the panel failed")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop drawing and put the panel to sleep, giving up if it is wedged.

        An un-slept panel makes the next init unreliable, so sleeping matters --
        but not enough to hang shutdown on hardware that has stopped answering.
        """
        self._stopping.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                log.warning("panel still drawing after %.1fs; leaving it", timeout)
                return
        try:
            self._display.sleep()
        except Exception:                                  # noqa: BLE001
            log.debug("could not sleep the panel", exc_info=True)
