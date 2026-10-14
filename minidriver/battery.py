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

FIELDS = ("battery", "battery_charging", "battery_power_plugged")


def parse_reply(text: str) -> dict:
    """The `key: value` lines of a reply, as a dict. Junk lines are ignored."""
    values = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip()] = value.strip()
    return values


def to_status(values: dict):
    """Shape raw replies into what the panel renders, or None without a level."""
    try:
        percent = round(float((values or {}).get("battery")))
    except (TypeError, ValueError):
        return None
    return {
        # The fuel gauge can read a little outside 0-100; the panel should not.
        "percent": max(0, min(100, percent)),
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
