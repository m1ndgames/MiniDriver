"""Battery level from the PiSugar 3 UPS, for the status panel.

`pisugar-server` speaks a plain-text protocol -- send `get <field>`, read back
`<field>: <value>`. It listens on TCP and a websocket as well, but this reads the
Unix socket: it needs no network, no credentials, and is world-readable
(`srw-rw-rw-`), so it works whether or not the TCP ports are reachable.

Reading is best-effort by design. A box with no UPS fitted, or with
pisugar-server stopped, is a normal state and must not disturb the survey -- so
every failure returns None, and the timeout is short because this is read on the
daemon loop that also sends the systemd watchdog heartbeat.
"""
import logging
import socket

log = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/pisugar-server.sock"
# Short on purpose: see the module docstring. A wedged pisugar-server must cost
# the survey a fraction of a second, not a watchdog restart.
TIMEOUT = 0.5

FIELDS = ("battery", "battery_v", "battery_charging", "battery_power_plugged")

# Cell voltage to remaining charge, interpolated between points.
#
# PiSugar's own percentage is not used, because it is badly optimistic at the
# low end -- exactly where it matters. Measured on this box: it reported 71.6%
# at 3.755V and 50.3% at 3.589V, and the Pi browned out while it still claimed
# ~30%. An abrupt power cut is what this is here to prevent: the one that
# happened left the filesystem needing orphan recovery on the next boot.
#
# This curve is deliberately pessimistic below 3.7V so that "low" is reached
# before the cell actually collapses. It is an approximation either way --
# voltage under load sits below the resting voltage, and the 3.7-3.9V plateau
# makes any voltage-based estimate coarse in the middle. Being coarse in the
# middle and honest at the bottom is the right trade for a box that must shut
# down cleanly.
VOLTAGE_CURVE = (
    (4.15, 100.0),
    (4.05, 90.0),
    (3.95, 80.0),
    (3.87, 70.0),
    (3.80, 60.0),
    (3.75, 50.0),
    (3.71, 40.0),
    (3.67, 30.0),
    (3.63, 20.0),
    (3.58, 10.0),
    (3.50, 5.0),
    (3.40, 0.0),
)


def percent_from_voltage(volts):
    """Remaining charge for a cell voltage, or None if it is not a number."""
    try:
        volts = float(volts)
    except (TypeError, ValueError):
        return None
    if volts >= VOLTAGE_CURVE[0][0]:
        return 100.0
    if volts <= VOLTAGE_CURVE[-1][0]:
        return 0.0
    for (high_v, high_pc), (low_v, low_pc) in zip(VOLTAGE_CURVE,
                                                  VOLTAGE_CURVE[1:]):
        if volts >= low_v:
            span = high_v - low_v
            return low_pc + (volts - low_v) / span * (high_pc - low_pc)
    return 0.0


def parse_reply(text: str) -> dict:
    """The `key: value` lines of a reply, as a dict. Junk lines are ignored."""
    values = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip()] = value.strip()
    return values


def to_status(values: dict):
    """Shape raw replies into what the panel renders, or None without a level.

    The level is computed from the cell voltage rather than taken from PiSugar's
    reported percentage -- see VOLTAGE_CURVE for why. The gauge's own figure is
    only a fallback for a UPS that reports no voltage.
    """
    values = values or {}
    volts = values.get("battery_v")
    percent = percent_from_voltage(volts)
    if percent is None:
        try:
            percent = float(values.get("battery"))
        except (TypeError, ValueError):
            return None
    try:
        volts = float(volts)
    except (TypeError, ValueError):
        volts = None
    return {
        # An estimate can land slightly outside 0-100; the panel should not.
        "percent": max(0, min(100, round(percent))),
        "volts": volts,
        "charging": values.get("battery_charging") == "true",
        "plugged": values.get("battery_power_plugged") == "true",
    }


def _ask(path: str, timeout: float) -> str:
    """One connection, one request per field, replies concatenated."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        replies = []
        for field in FIELDS:
            sock.sendall(f"get {field}\n".encode())
            replies.append(sock.recv(256).decode("utf-8", "replace"))
        return "".join(replies)
    finally:
        sock.close()


def read(path: str = SOCKET_PATH, timeout: float = TIMEOUT, ask=None):
    """The battery status, or None when the UPS cannot be read."""
    ask = ask or _ask
    try:
        return to_status(parse_reply(ask(path, timeout)))
    except Exception:                                      # noqa: BLE001
        log.debug("could not read the battery from %s", path, exc_info=True)
        return None
