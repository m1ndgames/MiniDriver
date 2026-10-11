# Testing

## Unit tests (any OS)

The package's logic imports cleanly anywhere — every hardware dependency (bleak,
Pillow, spidev, lgpio, gpsd sockets, `iw`/`wpa_supplicant` subprocesses) is
imported lazily inside the function that uses it. So the full suite runs on a dev
machine with no radio attached:

```
uv venv --python 3
uv pip install -e . pytest pyyaml
.venv/Scripts/python -m pytest        # Windows
.venv/bin/python   -m pytest          # Linux/macOS
```

What is covered:

| Area | Test |
|---|---|
| `iw scan` parsing, channels, auth modes | `tests/test_wifi.py` |
| WigleWifi CSV rows + the append-as-you-go writer | `tests/test_survey.py` |
| WPA-PSK derivation + supplicant rendering | `tests/test_netconfig.py` |
| Upload clients, multipart, the content-hash ledger | `tests/test_uploads.py` |
| The field↔home mode machine (debounced both ways) | `tests/test_statemachine.py` |
| The upload sweep (pending, dedupe, skip-empty) | `tests/test_sweep.py` |
| gpsd JSON folding | `tests/test_gps.py` |
| BLE sighting shaping + bookkeeping | `tests/test_ble.py` |
| Daemon-owned radio commands + IP parsing | `tests/test_station.py` |
| The daemon loop: field poll, home switch, sweep | `tests/test_daemon.py` |
| E-paper status lines | `tests/test_display.py` |
| Idempotent boot-config edits | `tests/test_provision.py` |
| `psk` helper output | `tests/test_psk.py` |

## Live device tests

Flash Raspberry Pi OS Lite, join it to WiFi + enable SSH via Imager, and plug in
a USB GPS dongle for the parts that need a fix. Then, on the box:

- **GPS:** `gpspipe -w` or `cgps` — confirm fixes are coming from the dongle via
  `gpsd`. (On the finished device the L76K HAT replaces the dongle with no code
  change — only `gpsd`'s device line differs.)
- **WiFi scan:** `sudo iw dev wlan0 scan | head -40` — confirm access points are
  enumerated; the same text feeds `minidriver.wifi.parse_scan`.
- **BLE:** `bluetoothctl` then `scan on` — confirm advertisements on the internal
  radio.
- **The daemon:** run it in the foreground and drive the mode switch by making the
  home SSID appear and disappear:

  ```
  sudo /opt/minidriver/.venv/bin/python -m minidriver
  ```

  Confirm it associates and gets an address at home, runs the upload sweep, and
  returns to scanning when home drops out.
- **Real uploads:** with tokens set in `config.yaml`, confirm a located capture
  reaches WiGLE/WDGWars/Wardrift. The ledger prevents duplicate submissions, so
  re-running is safe.

## Hardware-only (once the HATs are mounted)

E-paper rendering and the L76K on `/dev/serial0` are validated on the assembled
device; both are thin wrappers over tested logic.
