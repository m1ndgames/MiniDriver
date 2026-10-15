"""The safety net that stops the box stranding itself on WiFi.

The daemon takes wlan0 off NetworkManager and parks it unassociated to scan, so
any daemon fault -- a crash loop, a hang, a home SSID that never matches -- leaves
a WiFi-only box with no route in. This runs on a systemd timer *outside* the
daemon, which is the point: a broken daemon cannot defeat its own watchdog.

The hard part is telling "driving around with no network in sight", which is the
normal working state, apart from "stranded". The one signal that separates them is
whether a configured home network is within reach while we still have no address.
"""
import logging
import subprocess
import sys
import time

from minidriver import nm, wifi
from minidriver.config import DEFAULT_CONFIG_PATH, load_config
from minidriver.station import link_argv, parse_ipv4, parse_link_ssid

log = logging.getLogger(__name__)

SERVICE = "minidriver.service"
# Sentinel for "the link is down", distinct from "we did not look".
DOWN = False
# Long enough to cover associating plus the DHCP timeout, so a box that is simply
# mid-handshake is never mistaken for a stranded one.
CONFIRM_DELAY = 30.0


def scan_dump_argv(iface: str) -> list:
    """Read the kernel's scan cache rather than triggering a fresh scan.

    A real scan would race the daemon's own scanning and can fail with EBUSY;
    the daemon keeps this cache warm every few seconds anyway.
    """
    return ["iw", "dev", iface, "scan", "dump"]


def address_argv(iface: str) -> list:
    return ["ip", "-o", "-4", "addr", "show", "dev", iface]


def stranded(home_ssids, visible_ssids, address, linked=None) -> bool:
    """Is the box unable to get itself back on the network?

    With no home network configured the daemon can never make the box reachable,
    so NetworkManager is the only way back in regardless of what is in range.

    `linked` is the SSID actually associated, or None when the link is down. An
    address alone is NOT proof of a link: when an access point disappears the
    interface keeps its configured IP, so everything looks healthy while nothing
    is reachable. A phone hotspot being switched off, with home WiFi still in
    range, got straight past an earlier version of this check -- so a link known
    to be down counts as having no address.
    """
    reachable = bool(address) and linked is not DOWN
    if not home_ssids:
        return not reachable
    in_range = bool(set(home_ssids) & set(visible_ssids or []))
    return in_range and not reachable


def _runner(argv) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return result.stdout or ""
    except (OSError, subprocess.SubprocessError):
        log.debug("could not run %s", " ".join(argv), exc_info=True)
        return ""


def reason(home_ssids, visible_ssids) -> str:
    """Why we are recovering, for the journal -- this line is the only
    explanation the operator gets for why their daemon was stopped."""
    if not home_ssids:
        return ("no home networks are configured, so the daemon can never make "
                "the box reachable")
    found = sorted(set(home_ssids) & set(visible_ssids or []))
    return f"home network {found} is in range but there is no address"


def clear_argvs(iface: str) -> list:
    """Commands that leave *iface* free for NetworkManager to pick up.

    Stopping the daemon is not enough. wpa_supplicant is started with -B, so it
    is a separate process that outlives the daemon -- and a daemon hung badly
    enough to need recovering is a daemon systemd ends up SIGKILLing, which skips
    its teardown entirely. The orphan keeps the interface, so NetworkManager can
    be handed a device it is then unable to associate: managed, and still dark.
    """
    return [
        ["dhclient", "-r", iface],
        ["pkill", "-f", f"wpa_supplicant.*-i *{iface}"],
        ["ip", "addr", "flush", "dev", iface],
    ]


def recover(iface: str, why: str, runner=None) -> None:
    """Stop the daemon and give the radio back, so the box comes back on WiFi."""
    runner = runner or _runner
    log.error("recovering %s: %s -- stopping %s and handing the radio back to "
              "NetworkManager", iface, why, SERVICE)
    runner(["systemctl", "stop", SERVICE])
    for argv in clear_argvs(iface):
        runner(argv)
    nm.release(iface, runner=runner)
    # Handing the device back normally triggers an autoconnect on its own; asking
    # explicitly costs one call and covers the case where it does not. This is
    # the last thing standing between the box and a trip to the SD card reader.
    runner(["nmcli", "device", "connect", iface])


def probe(iface: str, runner=None):
    """What the interface looks like now: (visible SSIDs, address, linked)."""
    runner = runner or _runner
    address = parse_ipv4(runner(address_argv(iface)))
    linked = parse_link_ssid(runner(link_argv(iface))) or DOWN
    visible = wifi.visible_ssids(wifi.parse_scan(runner(scan_dump_argv(iface))))
    return visible, address, linked


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(DEFAULT_CONFIG_PATH)
    network = cfg.get("network", {})
    iface = network.get("interface", "wlan0")
    home_ssids = [n.get("ssid") for n in network.get("home_networks", [])
                  if n.get("ssid")]

    visible, address, linked = probe(iface)
    if not stranded(home_ssids, visible, address, linked):
        log.info("%s healthy (address=%s, link=%s, home in range=%s)", iface, address, linked or "down",
                 bool(set(home_ssids) & set(visible)))
        return 0

    # Arriving home, the daemon spends a few seconds associating and up to the
    # DHCP timeout getting an address, and during that window it looks exactly
    # like a stranded box. Confirm before stopping anything -- a false positive
    # here would kill a working daemon roughly every tenth trip home.
    log.warning("%s looks stranded; re-checking in %.0fs before acting",
                iface, CONFIRM_DELAY)
    time.sleep(CONFIRM_DELAY)
    visible, address, linked = probe(iface)
    if not stranded(home_ssids, visible, address, linked):
        log.info("%s came up on its own (address=%s); leaving it alone",
                 iface, address)
        return 0

    recover(iface, reason(home_ssids, visible))
    return 0


if __name__ == "__main__":
    sys.exit(main())
