"""Reversible handoff of the single radio between NetworkManager and the daemon.

The daemon has to own wlan0 outright to scan unassociated, but on a WiFi-only box
that ownership is the only route back in -- so it must be possible to give back.

This is deliberately a runtime `nmcli device set` call and not a file in
/etc/NetworkManager/conf.d. A config file persists, which means a daemon that
fails to associate strands the box across every future reboot; a runtime flag
dies with NetworkManager, so a plain power cycle always returns the radio and the
box comes back on home WiFi by itself.
"""
import logging
import subprocess

log = logging.getLogger(__name__)

NMCLI = "nmcli"


def set_managed_argv(iface: str, managed: bool, binary: str = NMCLI) -> list:
    return [binary, "device", "set", iface, "managed",
            "yes" if managed else "no"]


def _runner(argv) -> str:
    """Run a command, returning stdout; never raises, because a box with no
    NetworkManager at all simply owns the radio outright."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            log.debug("%s exited %s: %s", argv[0], result.returncode,
                      (result.stderr or "").strip()[:200])
        return result.stdout or ""
    except (OSError, subprocess.SubprocessError):
        log.debug("could not run %s", " ".join(argv), exc_info=True)
        return ""


def _set(iface: str, managed: bool, runner=None) -> None:
    runner = runner or _runner
    try:
        runner(set_managed_argv(iface, managed))
    except Exception:                                       # noqa: BLE001
        log.debug("handing %s %s NetworkManager failed", iface,
                  "to" if managed else "away from", exc_info=True)


def take(iface: str, runner=None) -> None:
    """Take *iface* off NetworkManager so the daemon can drive it."""
    log.info("taking %s from NetworkManager", iface)
    _set(iface, False, runner)


def release(iface: str, runner=None) -> None:
    """Give *iface* back, so NetworkManager reconnects and the box is reachable."""
    log.info("handing %s back to NetworkManager", iface)
    _set(iface, True, runner)
