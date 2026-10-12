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
| The daemon loop: field poll, home switch, sweep, radio release | `tests/test_daemon.py` |
| E-paper status lines, and that a wedged panel cannot block the caller | `tests/test_display.py` |
| Idempotent boot-config edits | `tests/test_provision.py` |
| `psk` helper output | `tests/test_psk.py` |
| Reversible NetworkManager handoff | `tests/test_nm.py` |
| The stranded-box recovery decision | `tests/test_recover.py` |

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

## Testing the recovery path

The daemon takes `wlan0` from NetworkManager, so on a WiFi-only box a daemon
fault is the difference between reachable and gone. That recovery must be
exercised, not assumed — an earlier version *claimed* `Restart=always` was the
mitigation, which was false and locked a box out completely.

Before trusting a deployment, confirm all three layers on the device:

- **The daemon comes home unaided.** `systemctl start minidriver`, then watch the
  journal: it should enter FIELD, recognise the home SSID and bind an address
  within ~15s.
- **The timer spares a healthy daemon.** `systemctl start
  minidriver-recover.service` while the box is home — it must log `healthy` and
  leave the service running.
- **The recovery action works.** Call it directly:

  ```
  sudo /opt/minidriver/.venv/bin/python -c \
    "from minidriver import recover; recover.recover('wlan0', 'manual test')"
  ```

  The daemon should stop and NetworkManager should reconnect within seconds.

When testing a daemon that owns the radio, arm an independent deadman first so a
failure cannot strand the box:

```
sudo systemd-run --on-active=180 --unit=deadman /bin/sh -c \
  'systemctl stop minidriver; nmcli device set wlan0 managed yes'
```

Cancel it with `systemctl stop deadman.timer` once the daemon has proven itself.

## Hardware-only (once the HATs are mounted)

The panel driver claims the GPIO pins at import time, so only one process can
import it — a manual panel test needs `minidriver.service` stopped first, and
presence should be checked with `importlib.util.find_spec`, never a real import.

Battery boards need their own test: confirm `wlan0` still associates on battery
before trusting one in the field (see the README).
