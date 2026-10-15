"""Daemon-owned control of the single WiFi radio.

This box drives wlan0 itself rather than leaving it to NetworkManager -- a
deliberate trade of the network-manager safety net for deterministic capture
control. The radio is taken at runtime (see minidriver.nm) and never by a
persistent config file, so a power cycle always returns it.

Restart=always in the unit is NOT a recovery mechanism: a restarted daemon runs
enter_field() again and flushes the address again, so it cannot by itself make a
stranded box reachable. The actual safety net is minidriver.recover, which runs
on its own timer outside this process.

Two modes:
  - enter_field(): stop any association so the radio is free, and keep the link
    up so `iw scan` works. The daemon then scans on its own cadence.
  - associate(): write the wpa_supplicant config from the home networks, start
    wpa_supplicant against it, and run a one-shot DHCP client for an address.

Every external command goes through an injected runner, so the command sequence
and the IP parsing are testable without a radio.
"""
import logging
import re
import subprocess

from minidriver import nm
from minidriver.netconfig import write_supplicant_conf

log = logging.getLogger(__name__)

SUPPLICANT_PATH = "/run/minidriver/wpa_supplicant.conf"
# One-shot DHCP: acquire a lease and exit, rather than a daemon that would fight
# the field/home switch. isc-dhcp-client is small and present; the command is a
# constructor argument so the box can be pointed at udhcpc or dhcpcd instead.
DHCP_CMD = ["dhclient", "-1"]
WPA_DRIVER = "nl80211"
ASSOCIATE_TIMEOUT = 20.0


def scan_argv(iface: str) -> list:
    return ["iw", "dev", iface, "scan"]


def wpa_supplicant_argv(iface: str, conf_path) -> list:
    return ["wpa_supplicant", "-B", "-D", WPA_DRIVER, "-i", iface,
            "-c", str(conf_path)]


def link_argv(iface: str) -> list:
    return ["iw", "dev", iface, "link"]


_SSID_RE = re.compile(r"^\s*SSID:\s*(.+?)\s*$", re.MULTILINE)


def parse_link_ssid(text: str):
    """The SSID actually associated, or None when the link is down.

    An address is not proof of a link: when an access point goes away the
    interface keeps its configured IP, so `ip addr` still looks healthy while
    nothing can be reached. This is the honest check.
    """
    match = _SSID_RE.search(text or "")
    return match.group(1) if match else None


_INET_RE = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})")


def parse_ipv4(text: str):
    """The first IPv4 address in `ip addr` output, or None."""
    match = _INET_RE.search(text or "")
    return match.group(1) if match else None


def _subprocess_runner(argv) -> str:
    """Run a command, returning stdout; never raises so one failed call cannot
    take the daemon down."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.debug("%s exited %s: %s", argv[0], result.returncode,
                      (result.stderr or "").strip()[:200])
        return result.stdout or ""
    except (OSError, subprocess.SubprocessError):
        log.debug("could not run %s", " ".join(argv), exc_info=True)
        return ""


class Station:
    def __init__(self, iface: str, supplicant_path=SUPPLICANT_PATH,
                 runner=_subprocess_runner, dhcp_cmd=None):
        self.iface = iface
        self.supplicant_path = supplicant_path
        self._run = runner
        self._dhcp = list(dhcp_cmd or DHCP_CMD)

    # --------------------------------------------------------------- field
    def enter_field(self) -> None:
        """Free the radio for scanning: stop any association, keep the link up."""
        nm.take(self.iface, runner=self._run)
        self._stop_association()
        # Bring the interface up but join nothing -- `iw scan` needs it up.
        self._run(["ip", "link", "set", self.iface, "up"])

    def scan(self) -> str:
        """One active scan of the interface, as raw `iw scan` text."""
        return self._run(scan_argv(self.iface))

    # ---------------------------------------------------------------- home
    def associate(self, networks, country=None) -> None:
        """Write the config and bring the home association up with an address."""
        from pathlib import Path
        Path(self.supplicant_path).parent.mkdir(parents=True, exist_ok=True)
        write_supplicant_conf(networks, self.supplicant_path, country=country)
        self._run(["ip", "link", "set", self.iface, "up"])
        self._run(wpa_supplicant_argv(self.iface, self.supplicant_path))
        self._run(self._dhcp + [self.iface])

    def link_ssid(self):
        """The SSID this interface is associated to, or None if it is not."""
        return parse_link_ssid(self._run(link_argv(self.iface)))

    def address(self):
        """The interface's current IPv4 address, or None."""
        return parse_ipv4(self._run(["ip", "-o", "-4", "addr", "show", "dev",
                                     self.iface]))

    def teardown(self) -> None:
        """Stop driving the radio and hand it back, so stopping the daemon makes
        the box reachable again instead of leaving it dark."""
        self._stop_association()
        nm.release(self.iface, runner=self._run)

    # ------------------------------------------------------------- helpers
    def _stop_association(self) -> None:
        # Release any DHCP lease first, then stop the supplicant for this iface.
        self._run(self._dhcp + ["-r", self.iface])
        self._run(["wpa_cli", "-i", self.iface, "terminate"])
        self._run(["ip", "addr", "flush", "dev", self.iface])
