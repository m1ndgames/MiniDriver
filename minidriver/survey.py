"""The session's WigleWifi-1.6 capture file.

One file per session, appended to as the survey runs and uploaded as-is -- it is
already in the format WiGLE, WDGWars and Wardrift all accept, so there is no
export step. WiFi and BLE sightings share the file, told apart by the Type
column, exactly as m1ndb0x's bettercap survey wrote them.

Each row is flushed as it is written: the box runs off a battery in a pocket and
may lose power at any moment, and the rows already on disk are the capture.
"""
import csv
import logging
import pathlib

from minidriver.wifi import (auth_mode, frequency_for, has_fix, seen_at, ssid)

log = logging.getLogger(__name__)

VERSION = "WigleWifi-1.6"
COLUMNS = ["MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "Frequency",
           "RSSI", "CurrentLatitude", "CurrentLongitude", "AltitudeMeters",
           "AccuracyMeters", "RCOIs", "MfgrId", "Type"]
HEADER = (f"{VERSION},appRelease=minidriver,model=minidriver,release=1,"
          "device=minidriver,display=epaper,board=raspberrypi,brand=minidriver")

# One row per sighting per this many seconds while it keeps being heard: enough
# observations for the services to place it, without a row per scan.
ROW_SECONDS = 30.0
# Only sightings heard this recently are tagged with the current fix. A scan can
# list a network for a while after it was last actually heard, and pinning a
# stale sighting to where the box is now would misplace it.
FRESH_SECONDS = 10.0


def ble_name(device: dict) -> str:
    """A BLE device's advertised name, blank when it gave none."""
    return (device.get("name") or "").strip()


def wigle_row(ap: dict, fix: dict, when: float) -> list:
    """One WigleWifi-1.6 row for a sighting heard at *when*, at *fix*.

    WiFi carries a channel, frequency and capability string; a BLE device
    carries none of those -- only a name -- so those columns are blank and the
    Type tells them apart, which is how the services read the file.
    """
    from datetime import datetime, timezone
    kind = ap.get("kind", "wifi")
    seen = datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if kind == "ble":
        ssid_value, auth, channel, frequency, wigle_type = \
            ble_name(ap), "", "", "", "BLE"
    else:
        ssid_value, auth = ssid(ap), auth_mode(ap)
        channel = ap.get("channel") or ""
        frequency = ap.get("frequency") or frequency_for(ap.get("channel"))
        wigle_type = "WIFI"
    values = {
        "MAC": (ap.get("mac") or "").upper(),
        "SSID": ssid_value,
        "AuthMode": auth,
        "FirstSeen": seen,
        "Channel": channel,
        "Frequency": frequency,
        "RSSI": ap.get("rssi") if ap.get("rssi") is not None else "",
        "CurrentLatitude": fix["latitude"],
        "CurrentLongitude": fix["longitude"],
        "AltitudeMeters": fix.get("altitude") if fix.get("altitude") is not None else "",
        "AccuracyMeters": fix.get("accuracy") if fix.get("accuracy") is not None else "",
        "RCOIs": "",
        "MfgrId": "",
        "Type": wigle_type,
    }
    return [values[name] for name in COLUMNS]


class SurveyLog:
    """The session's capture, appended to as the survey runs."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.rows = 0
        self._last_row = {}          # mac -> monotonic time of its last row
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        self._stream = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._stream, lineterminator="\n")
        if fresh:
            self._stream.write(HEADER + "\n")
            self._writer.writerow(COLUMNS)
            self._stream.flush()

    def record(self, sightings, fix: dict, now_wall: float, now_mono: float) -> list:
        """Write a row for each sighting heard just now, when there is a fix and
        it has not had one in the last ROW_SECONDS. Returns the MACs written."""
        if not has_fix(fix):
            return []
        written = []
        for item in sightings:
            heard = seen_at(item.get("last_seen"))
            if heard is None or now_wall - heard > FRESH_SECONDS:
                continue
            mac = item["mac"].lower()
            last = self._last_row.get(mac)
            if last is not None and now_mono - last < ROW_SECONDS:
                continue
            self._writer.writerow(wigle_row(item, fix, heard))
            self._last_row[mac] = now_mono
            written.append(mac)
        if written:
            self._stream.flush()
            self.rows += len(written)
        return written

    def close(self) -> None:
        try:
            self._stream.close()
        except OSError:
            log.debug("could not close the survey log", exc_info=True)


def strip_unlocated(text: str) -> str:
    """Drop records that carry no position.

    All three services discard positionless rows at the far end and all cap
    upload size, so sending them spends allowance on data that is thrown away. A
    blank or 0,0 coordinate both mean "nowhere": 0,0 is how absence is written
    in this format, not a spot in the Gulf of Guinea.
    """
    import io
    if not (text or "").strip():
        return text

    lines = text.splitlines()
    reader = csv.reader(io.StringIO("\n".join(lines[1:])))
    try:
        columns = next(reader)
    except StopIteration:
        return text
    try:
        lat_at = columns.index("CurrentLatitude")
        lon_at = columns.index("CurrentLongitude")
    except ValueError:
        return text

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    out.write(lines[0] + "\n")
    writer.writerow(columns)
    for row in reader:
        if not row:
            continue
        try:
            latitude = float(row[lat_at])
            longitude = float(row[lon_at])
        except (ValueError, IndexError):
            continue
        if latitude == 0 and longitude == 0:
            continue
        writer.writerow(row)
    return out.getvalue()


def records_in(text: str) -> int:
    """Count the data rows, past the two header lines."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return max(0, len(lines) - 2)
