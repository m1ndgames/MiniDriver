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

log = logging.getLogger(__name__)


def _gps_line(gps: dict) -> str:
    gps = gps or {}
    if not gps.get("fix"):
        return "GPS: no fix"
    label = {2: "2D", 3: "3D"}.get(gps.get("mode"), "fix")
    return f"GPS: {label} {gps.get('sats_used', 0)}/{gps.get('sats_seen', 0)} sats"


def status_lines(status: dict) -> list:
    """The lines to draw for a status snapshot. Tolerates a sparse status taken
    before the first scan -- every field is read with a default."""
    status = status or {}
    mode = str(status.get("mode", "field")).upper()
    lines = [f"MiniDriver  {mode}", _gps_line(status.get("gps"))]

    lines.append(f"WiFi: {status.get('wifi_aps', 0)}   "
                 f"BLE: {status.get('ble_devices', 0)}")
    lines.append(f"rows: {status.get('rows', 0)}")

    if status.get("mode") == "home":
        last = status.get("last_upload")
        lines.append(f"upload: {last}" if last else "upload: ready")
        lines.append(f"IP: {status.get('ip') or '...'}")
    else:
        pending = status.get("pending") or {}
        waiting = sum(int(v or 0) for v in pending.values())
        lines.append(f"pending: {waiting}")

    return lines


class Display:
    """Draws the dashboard onto the Waveshare e-paper panel.

    Hardware-only: PIL and the waveshare_epd driver are imported on first use so
    this module loads on a dev machine. A failure to reach the panel is logged
    and swallowed -- the box must keep capturing with a dark screen.
    """

    def __init__(self, rotate: int = 0):
        self.rotate = rotate
        self._epd = None
        self._font = None

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

    def render(self, status: dict) -> None:
        if not self._ensure():
            return
        try:
            from PIL import Image, ImageDraw
            # The panel's native buffer is width=height_px; draw landscape and
            # let the driver orient it.
            width, height = self._epd.height, self._epd.width
            image = Image.new("1", (width, height), 0xFF)
            draw = ImageDraw.Draw(image)
            y = 2
            for line in status_lines(status):
                draw.text((4, y), line, font=self._font, fill=0)
                y += 16
            if self.rotate:
                image = image.rotate(self.rotate, expand=False)
            self._epd.display(self._epd.getbuffer(image))
        except Exception:                                  # noqa: BLE001
            log.exception("could not draw the panel")

    def sleep(self) -> None:
        if self._epd is not None:
            try:
                self._epd.sleep()
            except Exception:                              # noqa: BLE001
                log.debug("could not sleep the panel", exc_info=True)
