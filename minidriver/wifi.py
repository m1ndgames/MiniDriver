"""Active WiFi scanning: run `iw scan` and parse its output into AP dicts.

This is the box's whole capture mechanism. There is no monitor mode and no
packet database -- a managed-mode active scan sends broadcast probe requests
(like any phone looking for networks), and the beacons and probe responses that
come back carry everything the upload services want: BSSID, SSID, channel,
frequency, signal, and the security suite.

The parsing is ported from m1ndb0x's bettercap survey, where it was validated
against real `iw scan` output across all three bands. The AP dicts it produces
are shaped so the survey writer and auth_mode() treat a scanned network exactly
like one heard any other way.
"""
import re
import time
from datetime import datetime, timezone

SCAN_BINARY = "iw"
# A scan itself takes a few seconds; the timeout guards a wedged driver.
SCAN_TIMEOUT = 15.0


def scan_argv(iface: str, binary: str = SCAN_BINARY) -> list:
    """The command line for one active scan of *iface*."""
    return [binary, "dev", iface, "scan"]


def channel_for(freq) -> str:
    """The channel number for a centre frequency in MHz, or "" if unknown.

    Each band numbers its channels off a different base, and 6 GHz reuses the
    low numbers 2.4 GHz already owns -- so this keys on the frequency, which is
    unambiguous, not on a bare channel number.
    """
    try:
        mhz = int(float(freq))
    except (TypeError, ValueError):
        return ""
    if mhz == 2484:
        return "14"
    if 2412 <= mhz <= 2472:
        return str((mhz - 2407) // 5)
    if 5945 <= mhz <= 7115:                 # 6 GHz (Wi-Fi 6E/7)
        return str((mhz - 5950) // 5)
    if 5000 <= mhz < 5900:
        return str((mhz - 5000) // 5)
    return ""


def frequency_for(channel) -> str:
    """The centre frequency in MHz for a 2.4/5 GHz channel, or "" if unknown.

    Blank rather than a guess: a wrong frequency would be believed, where a
    missing one is obviously missing.
    """
    try:
        number = int(str(channel).strip())
    except (TypeError, ValueError):
        return ""
    if number == 14:
        return 2484
    if 1 <= number <= 13:
        return 2407 + number * 5
    if 32 <= number <= 196:
        return 5000 + number * 5
    return ""


def auth_mode(ap: dict) -> str:
    """WiGLE's capability string, e.g. "[WPA2-PSK-CCMP][ESS]".

    Encryption, authentication and cipher are reported separately, and
    encryption may name more than one protocol ("WPA2, WPA3").
    """
    encryption = (ap.get("encryption") or "").replace(",", " ").split()
    auth = (ap.get("authentication") or "").strip()
    cipher = (ap.get("cipher") or "").strip()
    parts = []
    for protocol in encryption:
        protocol = protocol.upper()
        if protocol == "OPEN":
            continue
        if protocol == "WEP":
            parts.append("[WEP]")
            continue
        parts.append("[" + "-".join(p for p in (protocol, auth, cipher) if p) + "]")
    return "".join(parts) + "[ESS]"


def short_crypt(ap: dict) -> str:
    """The headline protocol: "open", "WPA", "WPA2", "WPA3" -- strongest named."""
    named = (ap.get("encryption") or "").upper()
    for marker in ("WPA3", "WPA2", "WPA", "WEP"):
        if marker in named:
            return marker
    return "open"


def ssid(ap: dict) -> str:
    """The network name, blank when hidden."""
    name = ap.get("hostname") or ""
    return "" if name == "<hidden>" else name


def seen_at(stamp):
    """Epoch seconds from an RFC 3339 timestamp, or None."""
    if not stamp or not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def has_fix(gps: dict) -> bool:
    """Whether a gps snapshot carries a usable 2D-or-better position."""
    return ((gps or {}).get("mode") or 0) >= 2 and gps.get("latitude") is not None


def _security_fields(rsn: dict, wpa: dict, private: bool) -> dict:
    """encryption/authentication/cipher from a scan's RSN and WPA blocks, so a
    scanned AP feeds auth_mode() the same as one heard any other way.

    WPA3 is RSN with SAE; WPA2 is RSN with PSK/802.1X; WPA is the old WPA-only
    block; Privacy with neither is WEP; nothing is an open network.
    """
    block = rsn or wpa
    if not block:
        return {"encryption": "WEP" if private else "OPEN",
                "authentication": "", "cipher": ""}
    auth = block.get("auth", "")
    if "SAE" in auth:
        encryption, authentication = "WPA3", "SAE"
    elif "PSK" in auth:
        encryption = "WPA2" if rsn else "WPA"
        authentication = "PSK"
    elif "802.1X" in auth or "EAP" in auth:
        encryption, authentication = ("WPA2" if rsn else "WPA"), "MGT"
    else:
        encryption = "WPA2" if rsn else "WPA"
        authentication = auth.split()[0] if auth else ""
    # CCMP is the real cipher wherever a network offers both for legacy clients.
    cipher = "CCMP" if "CCMP" in block.get("cipher", "") else \
        (block.get("cipher", "").split() or [""])[0]
    return {"encryption": encryption, "authentication": authentication,
            "cipher": cipher}


_BSS_RE = re.compile(r"^BSS ([0-9a-fA-F:]{17})")


def parse_scan(text: str, now_wall: float = None) -> list:
    """`iw scan` output into AP dicts, one per BSS.

    A scan result was, by definition, just heard, so last_seen is set to now:
    the survey writer treats every scanned AP as a fresh sighting.
    """
    now_wall = time.time() if now_wall is None else now_wall
    stamp = datetime.fromtimestamp(now_wall, timezone.utc).isoformat()
    out = []
    current = None
    section = None          # which security block trailing lines belong to
    for raw in (text or "").splitlines():
        head = _BSS_RE.match(raw)
        if head:
            if current:
                out.append(current)
            current = {"mac": head.group(1).lower(), "hostname": "",
                       "frequency": None, "rssi": None, "channel": "",
                       "last_seen": stamp, "first_seen": stamp,
                       "_private": False, "_rsn": {}, "_wpa": {}}
            section = None
            continue
        if current is None:
            continue
        line = raw.strip()
        if line.startswith("freq:"):
            try:
                current["frequency"] = int(float(line.split(":", 1)[1]))
            except ValueError:
                pass
        elif line.startswith("signal:"):
            try:
                current["rssi"] = int(round(float(line.split(":", 1)[1].split()[0])))
            except (ValueError, IndexError):
                pass
        elif line.startswith("capability:"):
            current["_private"] = "Privacy" in line
        elif line.startswith("SSID:"):
            # Only the beacon/probe SSID, never the ones nested in WPS or mesh
            # elements; those strip to the same prefix, so guard on not being
            # inside a block and on not already having a name.
            if not current["hostname"]:
                current["hostname"] = raw.split("SSID:", 1)[1].strip()
        elif line.startswith("RSN:"):
            section = current["_rsn"]
        elif line.startswith("WPA:"):
            section = current["_wpa"]
        elif line.startswith("WMM") or line.startswith("WPS"):
            section = None
        elif section is not None and line.startswith("* Authentication suites:"):
            section["auth"] = line.split(":", 1)[1].strip()
        elif section is not None and line.startswith("* Pairwise ciphers:"):
            section["cipher"] = line.split(":", 1)[1].strip()
    if current:
        out.append(current)

    for ap in out:
        ap.update(_security_fields(ap.pop("_rsn"), ap.pop("_wpa"),
                                   ap.pop("_private")))
        if not ap["channel"]:
            ap["channel"] = channel_for(ap["frequency"])
    return out


def visible_ssids(aps) -> set:
    """The named networks in a parsed scan.

    Used to decide whether a configured home network is in range. Hidden
    networks (an empty SSID) carry no name to match against, so they are left
    out -- a home network is matched by the SSID the operator typed.
    """
    return {ssid(ap) for ap in (aps or []) if ssid(ap)}
