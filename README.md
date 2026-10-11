# MiniDriver

Wardriving in a pocket. A self-contained Raspberry Pi Zero 2 W survey box that
uses **only its internal radios** — no external WiFi adapter.

Out in the field it actively scans for WiFi access points and Bluetooth LE
devices and tags each sighting with a GPS fix, writing a WigleWifi CSV. When it
comes back into range of your home network it joins it, becomes reachable over
your LAN, and uploads everything it found to **WiGLE**, **WDGWars** and
**Wardrift**. One radio does both jobs, switching between *field* (scanning) and
*home* (associated and uploading) on its own.

## How it works

- **One 2.4 GHz radio, two modes.** It cannot scan and stay associated at once,
  so it is always in exactly one mode. The same scan that collects access points
  also watches for your home SSID; when home is in range it switches over,
  associates, and uploads, then switches back to scanning when home drops out.
  Both switches are debounced so a passing network or a single missed scan does
  not flap the mode.
- **Capture is an active scan** (`iw scan`) — no monitor mode, no handshakes. It
  records BSSID, SSID, channel, frequency, signal and the security suite, which
  is exactly what the upload services want.
- **BLE** advertisements are scanned on the internal Bluetooth radio and folded
  into the same survey file.
- **GPS** is read through `gpsd`, so the L76K HAT (`/dev/serial0`) and a USB GPS
  dongle used for testing are interchangeable — only `gpsd`'s device line
  changes.
- **The daemon owns the radio directly** (NetworkManager is told to leave wlan0
  alone). The systemd unit runs it `Restart=always` with a watchdog, so a crash
  cannot strand the box unreachable at home.

## Hardware

- Raspberry Pi Zero 2 W
- Waveshare 2.13" E-Paper HAT V4 (status display)
- Waveshare L76K GPS HAT
- UPS Power Board Lite V1.3 (battery)

## Software

Raspberry Pi OS Lite (64-bit).

## Install

1. Flash Raspberry Pi OS Lite with Raspberry Pi Imager, presetting your WiFi and
   enabling SSH so the box is reachable on first boot.
2. SSH in, clone this repository, and run the installer:

   ```
   sudo python3 install.py
   ```

   It installs dependencies, configures the boot settings (UART for the GPS, SPI
   for the panel, Bluetooth kept on the mini-UART), hands wlan0 to the daemon,
   points `gpsd` at the GPS, and installs the service.
3. Edit the config with your home WiFi and service credentials, then reboot:

   ```
   sudo nano /etc/minidriver/config.yaml
   sudo reboot
   ```

See [`config.example.yaml`](config.example.yaml) for every option. To keep your
WiFi passphrase off the box, generate a derived key instead:

```
python3 -m minidriver.psk <your-ssid>
```

and paste the `psk_hash:` line it prints under `network.home_networks`.

## Watching it

```
journalctl -fu minidriver
```

The e-paper panel shows the current mode, GPS fix, how many WiFi/BLE sightings
the session has, what is waiting to upload, and — when home — the box's LAN IP.

## Development

See [TESTING.md](TESTING.md). The logic runs and tests on any OS (hardware
imports are lazy); `pytest` covers the scan parsing, CSV, uploads, the mode
machine, the upload sweep and the daemon loop.
