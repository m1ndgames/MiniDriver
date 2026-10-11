"""Configuration: one global config.yaml, deep-merged over DEFAULTS.

The real file is gitignored and hand-edited on the device over SSH, so a missing
or half-saved file must still yield a complete, working config -- values are
merged over the defaults rather than replacing the whole tree. Reading never
raises: an unreadable config is a reason to fall back to defaults and keep the
box capturing, not to crash the daemon.
"""
from copy import deepcopy
from pathlib import Path

import logging

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("/etc/minidriver/config.yaml")

DEFAULTS: dict = {
    "network": {
        # The onboard 2.4 GHz radio. The daemon owns it directly: it scans in
        # the field and associates to one of the home networks when one is in
        # range. There is no second radio.
        "interface": "wlan0",
        # Regulatory domain: channel availability and legal transmit power.
        "country": "DE",
        # [{ssid, psk | psk_hash, priority, hidden}]. When any of these is in
        # range the box switches out of the field and joins it to upload.
        "home_networks": [],
    },
    "services": {
        # Cannot be hashed -- the APIs need the literal value. Protected by the
        # 0600 file and by never being logged.
        "wigle_token": "",          # the "Encoded for use" value from WiGLE
        "wdgwars_key": "",          # 64-character WDGWars profile key
        "wardrift_username": "",
        "wardrift_password": "",
    },
    "scan": {
        # Seconds between active scans while in the field.
        "field_scan_seconds": 3.0,
        # A home network must be seen in this many consecutive scans before the
        # box leaves the field -- a single stray beacon off a passing car should
        # not end a drive.
        "home_confirm_scans": 2,
        # ...and must be missing this many checks running before the box gives up
        # on it and returns to the field.
        "home_lost_checks": 3,
        # How long to wait for DHCP to produce an address once associated.
        "dhcp_timeout_seconds": 20.0,
    },
    "ble": {
        # Scan Bluetooth LE advertisements on the internal radio and fold them
        # into the same survey. Off makes the box WiFi-only.
        "enabled": True,
    },
    "display": {
        # 0/90/180/270. The 2.13" panel is 250x122; rotation depends on how the
        # case mounts it.
        "rotate": 0,
        # Seconds between panel redraws. E-paper wears with every refresh and a
        # partial refresh takes time, so status is redrawn on a cadence rather
        # than on every change.
        "refresh_seconds": 30.0,
    },
    "paths": {
        "data_dir": "/var/lib/minidriver",
        "log_dir": "/var/log/minidriver",
    },
}


def _deep_merge(base: dict, overrides: dict) -> dict:
    """overrides on top of base, recursing into nested mappings.

    A non-mapping value (a list of home networks, a scalar) replaces wholesale;
    only dict-into-dict recurses, so a partial `network:` block keeps the
    default keys it does not mention.
    """
    merged = deepcopy(base)
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path=DEFAULT_CONFIG_PATH) -> dict:
    """Load config.yaml merged over DEFAULTS. A missing file yields defaults."""
    path = Path(path)
    if not path.is_file():
        return deepcopy(DEFAULTS)
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        log.exception("cannot read %s; using defaults", path)
        return deepcopy(DEFAULTS)
    if not isinstance(loaded, dict):
        log.error("%s is not a mapping; using defaults", path)
        return deepcopy(DEFAULTS)
    return _deep_merge(DEFAULTS, loaded)
