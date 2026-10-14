"""PiSugar UPS tasks that run on timers, outside the survey daemon.

Two jobs live here, both safety nets, and both deliberately independent of
`minidriver.service` -- a net must not depend on the thing it is protecting:

  low-battery  Shut down cleanly before the cell collapses. PiSugar's own
               `safe_shutdown_level` works off its reported percentage, which is
               optimistic enough that it never fires: the Pi browned out while
               the gauge still read ~30%, and the filesystem needed orphan
               recovery on the next boot. Cell voltage is measured rather than
               modelled, so a guard on voltage actually triggers.

  rtc-restore  Seed the system clock from the PiSugar RTC at boot, before chrony
               starts. The Pi has no RTC of its own, so without this a boot with
               no network begins at whatever the filesystem last recorded.

  rtc-save     Push the (chrony-maintained) system time back into the RTC, so
               the next cold boot starts from something recent.

The split matters: PiSugar's own `auto_rtc_sync` does a continuous two-way sync
with `date -s`, which fights chrony for ownership of the clock. One-way pushes on
a timer do not.
"""
import datetime
import logging
import socket
import subprocess
import sys

log = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/pisugar-server.sock"
TIMEOUT = 2.0

# Below this, shut down.
#
# Raised from 3.55V after it failed in the field: the guard ran at 00:43:45,
# found the cell above threshold, and the Pi was dead by 00:45:43 -- the
# filesystem needed orphan recovery again. A LiPo falls off a cliff below about
# 3.6V under load, and a scan burst sags it further, so by the time a quiet
# reading shows 3.55V there is no time left to shut down in. Erring high costs
# some capacity; erring low costs the card.
DEFAULT_SHUTDOWN_VOLTS = 3.65
# A WiFi scan burst sags the rail briefly; one low reading is not a flat cell.
# Short, because on that cliff twenty seconds is a meaningful part of what is
# left.
CONFIRM_DELAY = 10.0

# An RTC reading older than this is not believable and must not be used to set
# the clock -- a PiSugar that has never been set reports 2020, and seeding that
# would break TLS on every upload.
EARLIEST_CREDIBLE = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)


def ask(commands, path: str = SOCKET_PATH, timeout: float = TIMEOUT) -> str:
    """Send each command on one connection and return the replies joined."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        replies = []
        for command in commands:
            sock.sendall(f"{command}\n".encode())
            replies.append(sock.recv(512).decode("utf-8", "replace"))
        return "".join(replies)
    finally:
        sock.close()


def parse_reply(text: str) -> dict:
    """The `key: value` lines of a reply, as a dict. Junk lines are ignored."""
    values = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip()] = value.strip()
    return values


# ------------------------------------------------------------------- battery
def should_shut_down(status, threshold: float) -> bool:
    """Whether the cell is low enough to stop, given a battery status.

    Charging or on external power is never low: the voltage may be depressed
    while it recovers, and pulling the plug on a box that is plugged in would be
    absurd. No UPS at all is not our business either.
    """
    if not status:
        return False
    if status.get("charging") or status.get("plugged"):
        return False
    volts = status.get("volts")
    if volts is None:
        return False
    return float(volts) < float(threshold)


def _read_battery():
    from minidriver import battery
    return battery.read(path=SOCKET_PATH, timeout=TIMEOUT)


def low_battery(threshold: float, runner=None, sleep=None) -> int:
    runner = runner or (lambda argv: subprocess.run(argv, check=False))
    sleep = sleep or __import__("time").sleep

    status = _read_battery()
    # Logged every run, healthy or not: the journal is persistent, so this is
    # what lets the threshold be tuned from a real discharge curve rather than
    # guessed at. Guessing it is what let the last one through.
    if status:
        log.info("cell %.3fV (%s%%)%s", status.get("volts") or 0.0,
                 status.get("percent"),
                 " on charge" if status.get("charging")
                 or status.get("plugged") else "")
    if not should_shut_down(status, threshold):
        return 0
    log.warning("cell at %.3fV, below %.2fV; confirming before shutting down",
                status.get("volts"), threshold)
    sleep(CONFIRM_DELAY)
    status = _read_battery()
    if not should_shut_down(status, threshold):
        log.info("cell recovered; not shutting down")
        return 0
    log.error("cell at %.3fV is below %.2fV: shutting down cleanly to avoid "
              "an abrupt power cut", status.get("volts"), threshold)
    runner(["systemctl", "poweroff"])
    return 0


# ----------------------------------------------------------------------- rtc
def rtc_time(replies: dict):
    """The RTC's time, or None when it is missing or not believable."""
    raw = (replies or {}).get("rtc_time")
    if not raw:
        return None
    try:
        when = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when if when >= EARLIEST_CREDIBLE else None


def rtc_restore() -> int:
    """Seed the system clock from the RTC, if the RTC is believable."""
    when = rtc_time(parse_reply(ask(["get rtc_time"])))
    if when is None:
        log.warning("RTC time missing or not credible; leaving the clock alone")
        return 0
    ask(["rtc_rtc2pi"])
    log.info("system clock seeded from the RTC (%s)", when.isoformat())
    return 0


def rtc_save() -> int:
    """Push system time into the RTC so the next cold boot starts recent."""
    ask(["rtc_pi2rtc"])
    log.info("RTC updated from the system clock")
    return 0


# --------------------------------------------------------------------- entry
def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else ""
    try:
        if action == "low-battery":
            from minidriver.config import DEFAULT_CONFIG_PATH, load_config
            cfg = load_config(DEFAULT_CONFIG_PATH).get("battery", {})
            return low_battery(cfg.get("shutdown_volts",
                                       DEFAULT_SHUTDOWN_VOLTS))
        if action == "rtc-restore":
            return rtc_restore()
        if action == "rtc-save":
            return rtc_save()
    except Exception:                                      # noqa: BLE001
        # No UPS fitted, or pisugar-server stopped, is a normal state for this
        # box; a timer must never fail noisily over it.
        log.debug("pisugar %s failed", action, exc_info=True)
        return 0
    sys.exit(f"usage: python -m minidriver.pisugar "
             f"[low-battery|rtc-restore|rtc-save]")


if __name__ == "__main__":
    sys.exit(main())
