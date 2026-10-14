"""Reading a position from gpsd.

gpsd owns the serial device, so this box never opens /dev/serial0 itself -- it
asks gpsd over its TCP socket and folds the JSON stream into one snapshot. That
indirection is what lets the USB test dongle and the L76K HAT be
interchangeable: only gpsd's device line changes, never this code.

Ported from m1ndb0x. Reading never raises: no gpsd, or no fix, is a state the
survey writer already handles (it simply writes no rows), not a fault that should
take the daemon down.
"""
import json
import logging
import socket

log = logging.getLogger(__name__)


def has_fix(gps: dict) -> bool:
    """Whether a snapshot carries a usable 2D-or-better position.

    Exactly 0,0 is rejected. A cold-starting receiver reports mode 2 with 0,0
    for a while before it has a real position -- observed on a u-blox 7 with 3
    satellites used -- and in the WigleWifi format 0,0 is how "nowhere" is
    written, not a spot in the Gulf of Guinea. The upload path already strips
    such rows, so accepting them here only fills the capture with records that
    get thrown away, and makes the panel's "saved" count a lie.
    """
    gps = gps or {}
    if (gps.get("mode") or 0) < 2:
        return False
    latitude, longitude = gps.get("latitude"), gps.get("longitude")
    if latitude is None or longitude is None:
        return False
    return not (latitude == 0 and longitude == 0)


def parse_gpsd(lines) -> dict:
    """Fold gpsd's JSON stream into one GPS state.

    Satellites seen and used are both reported because they answer different
    questions: seen proves the antenna works, used proves the fix is real.
    Indoors the box sees a dozen and uses none.
    """
    gps = {"mode": 0, "latitude": None, "longitude": None,
           "satellites_seen": 0, "satellites_used": 0, "device": None,
           "altitude": None, "speed": None, "track": None, "accuracy": None,
           "time": None, "hdop": None}
    for line in lines or []:
        try:
            message = json.loads(line)
        except (TypeError, ValueError):
            continue
        kind = message.get("class")
        if kind == "TPV":
            gps["mode"] = message.get("mode", gps["mode"])
            if message.get("lat") is not None:
                gps["latitude"] = message.get("lat")
                gps["longitude"] = message.get("lon")
            for field, key in (("altitude", "altMSL"), ("speed", "speed"),
                               ("track", "track"), ("time", "time")):
                if message.get(key) is not None:
                    gps[field] = message.get(key)
            # epx and epy are separate one-sigma errors on the two axes. The
            # worse of them is the honest number: the smaller would overstate how
            # well the box knows where it is.
            errors = [e for e in (message.get("epx"), message.get("epy"))
                      if isinstance(e, (int, float))]
            if errors:
                gps["accuracy"] = max(errors)
        elif kind == "SKY":
            satellites = message.get("satellites") or []
            if satellites:
                gps["satellites_seen"] = len(satellites)
                gps["satellites_used"] = sum(1 for s in satellites if s.get("used"))
            if message.get("hdop") is not None:
                gps["hdop"] = message.get("hdop")
    return gps


def read_gpsd(host="127.0.0.1", port=2947, timeout=2.0, lines=12) -> dict:
    """Ask gpsd for a snapshot. Never raises: no gpsd is a state, not a fault."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
            stream = sock.makefile("r")
            return parse_gpsd([stream.readline() for _ in range(lines)])
    except OSError:
        log.debug("gpsd unavailable", exc_info=True)
        return parse_gpsd([])
