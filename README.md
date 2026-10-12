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
- **The daemon owns the radio directly.** It takes `wlan0` off NetworkManager at
  runtime rather than through a config file, so a power cycle always gives the
  radio back. On a box you reach over WiFi that ownership is also the only route
  in, so there are three layers of recovery: the daemon releases the radio when
  it stops, `ExecStopPost` releases it however the process died, and
  `minidriver-recover.timer` runs *outside* the daemon and hands `wlan0` back if
  a home network is in range while the box still has no address — so a
  crash-looping or wedged daemon cannot defeat its own watchdog. `Restart=always`
  is deliberately *not* treated as recovery: a restarted daemon re-enters the
  field and flushes the address again.

## Hardware

- Raspberry Pi Zero 2 W
- Waveshare 2.13" E-Paper HAT V4 (status display)
- Waveshare L76K GPS HAT
- A battery/UPS board

### Battery boards and WiFi

Mind what the battery sits on. A UPS Lite V1.3 stacked on top of this board
stopped `wlan0` associating entirely — the Pi booted normally and simply never
took a DHCP lease, with no undervoltage flagged. Removing it fixed it outright.
The likely causes are the cell's foil pouch sitting across the Zero 2 W's chip
antenna, or the boost converter sagging under the WiFi chip's association
current.

It is worth testing association on battery before trusting a board in the field,
and preferring one that mounts the cell away from the antenna end. This matters
more here than on most projects: a survey box that cannot do WiFi on battery has
nothing left to do.

## Software

Raspberry Pi OS Lite (64-bit). Developed against Bookworm and Trixie.

## Install

1. Flash Raspberry Pi OS Lite with Raspberry Pi Imager, presetting your WiFi and
   enabling SSH so the box is reachable on first boot.
2. SSH in, clone this repository, and run the installer:

   ```
   sudo python3 install.py
   ```

   It installs dependencies, configures the boot settings (UART for the GPS, SPI
   for the panel, Bluetooth kept on the mini-UART, serial console off), fetches
   the Waveshare panel driver (which Waveshare ship only in their git repo),
   points `gpsd` at the GPS, makes the journal persistent but size-capped, and
   installs the daemon plus its recovery timer.

   It also writes a `brcmfmac` workaround — the Zero 2 W's WiFi firmware fails to
   associate with some access points, tracked upstream as
   [trixie-feedback#23](https://github.com/raspberrypi/trixie-feedback/issues/23).

   Re-running it is safe: an existing `/etc/minidriver/config.yaml` is never
   overwritten, since that is the operator's file.
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
It is drawn on its own thread, because the panel driver waits on the display's
BUSY pin without a timeout and a dead panel must never stall the survey. Set
`display.enabled: false` to rule the panel out without unmounting the HAT.

Nothing is recorded without a GPS fix: `survey.py` drops positionless sightings,
since an unlocated observation is no use to any of the upload services. A
`rows: 0` panel with no fix is working as intended, not a fault.

## When the box seems unreachable

Its DHCP lease changes between flashes, and `wlan0` has a different MAC prefix
from the Bluetooth radio — so do not look for a remembered address or filter a
scan by the MAC you expect. Sweep and actually probe:

```
for i in $(seq 1 254); do ping -c1 -W1 192.168.178.$i >/dev/null 2>&1 & done; wait
ssh -o ConnectTimeout=4 m1nd@<each-responding-ip> hostname
```

A fresh flash means a new host key, so expect a key-mismatch warning rather than
a clean login. `minidriver.local` depends on mDNS working on your client and is
not evidence either way.

Two things that look like failures and are not: `usb0` is DOWN on a healthy box
unless you have configured the USB gadget, and `rows: 0` is correct without a GPS
fix.

If it is genuinely not booting, the boot partition is readable from any machine.
A first-boot script can be made to log there (`exec >/boot/firmware/x.log 2>&1`),
which survives a failed boot — that is how a `firstrun.sh` hang was found, caused
by it blocking on console output when `console=serial0` was set with no
`enable_uart=1`. The installer removes that serial console.

## Development

See [TESTING.md](TESTING.md). The logic runs and tests on any OS (hardware
imports are lazy); `pytest` covers the scan parsing, CSV, uploads, the mode
machine, the upload sweep and the daemon loop.
