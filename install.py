#!/usr/bin/env python3
"""Install MiniDriver as a system service on Raspberry Pi OS.

    sudo python3 install.py

Idempotent: safe to re-run to deploy a new version. An existing
/etc/minidriver/config.yaml is never overwritten -- it is the operator's file,
holding the WiFi keys and service tokens.

What it does:
  1. apt dependencies (iw, wpa_supplicant, a DHCP client, gpsd, bluez, the
     e-paper Python stack).
  2. Boot config: enable the UART, keep Bluetooth but move it to the mini-UART so
     the GPS HAT gets the stable PL011 UART, enable SPI for the panel, and take
     the login console off the serial port.
  3. wlan0 ownership: leave the interface with NetworkManager -- the daemon now
     claims the radio at runtime, which a power cycle undoes -- and delete the
     persistent "unmanaged" drop-in older versions wrote, which could strand a
     WiFi-only box for good. Mask ModemManager so it cannot grab the GPS serial
     device.
  4. Make the journal persistent but capped, so a reboot stops erasing the only
     evidence of why it rebooted.
  5. Point gpsd at the GPS (works for both the L76K HAT on /dev/serial0 and a USB
     dongle via USBAUTO).
  6. Install the code to /opt/minidriver in a venv, fetch the Waveshare panel
     driver (pinned; Waveshare ship it only in their git repo), seed the config,
     and install the systemd units: the daemon plus minidriver-recover.timer,
     the watchdog that hands wlan0 back if the daemon ever fails to get home.

Step 2 changes kernel/boot settings, so a reboot is needed afterwards.
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from minidriver.provision import ensure_lines, serial_console_off

PREFIX = Path("/opt/minidriver")
CONFIG_DIR = Path("/etc/minidriver")
UNIT_DIR = Path("/etc/systemd/system")
UNITS = ("minidriver.service", "minidriver-recover.service",
         "minidriver-recover.timer",
         # PiSugar safety nets. Harmless on a box with no UPS: every task
         # treats an unreachable socket as a normal state and exits quietly.
         "minidriver-lowbattery.service", "minidriver-lowbattery.timer",
         "minidriver-rtc-restore.service",
         "minidriver-rtc-save.service", "minidriver-rtc-save.timer")
SRC = Path(__file__).resolve().parent

APT_PACKAGES = [
    "iw", "wpasupplicant", "isc-dhcp-client", "gpsd", "gpsd-clients",
    "bluez", "python3-dbus", "python3-pil", "python3-numpy",
    # gpiozero is what the Waveshare panel driver talks to, backed by lgpio.
    # It ships on the Pi OS image, but relying on that leaves the panel dead on
    # any image that drops it.
    "python3-spidev", "python3-lgpio", "python3-gpiozero", "python3-bleak",
    "python3-yaml",
]

# Waveshare publish the panel driver only in their git repo -- no apt package and
# no wheel -- so it is fetched at a pinned commit. Only the three files this
# panel needs are taken: the repo is hundreds of megabytes of demo images for
# dozens of panels, and /tmp here is a ~200MB tmpfs on a 415MB board, so pulling
# the whole archive is enough to OOM the box into a watchdog reboot.
EPAPER_COMMIT = "a794fbc39656b0f93938d1ffb3fdc77eaed9e9fc"
EPAPER_BASE = ("https://raw.githubusercontent.com/waveshareteam/e-Paper/"
               f"{EPAPER_COMMIT}/RaspberryPi_JetsonNano/python/lib/waveshare_epd")
EPAPER_FILES = ("__init__.py", "epdconfig.py", "epd2in13_V4.py")

BOOT_LINES = [
    "enable_uart=1",
    "dtoverlay=miniuart-bt",
    "dtparam=spi=on",
]


def run(argv, **kw):
    print("    $", " ".join(argv))
    return subprocess.run(argv, **kw)


def need_root():
    if os.geteuid() != 0:
        sys.exit("run as root: sudo python3 install.py")


def boot_dir() -> Path:
    """Bookworm moved the boot partition to /boot/firmware; fall back to /boot."""
    firmware = Path("/boot/firmware")
    return firmware if firmware.is_dir() else Path("/boot")


def edit_boot():
    boot = boot_dir()
    config = boot / "config.txt"
    if config.is_file():
        new, changed = ensure_lines(config.read_text(), BOOT_LINES)
        if changed:
            config.write_text(new)
            print(f"    updated {config}")
        else:
            print(f"    {config} already set")
    else:
        print(f"    !! {config} not found; set {BOOT_LINES} by hand")

    cmdline = boot / "cmdline.txt"
    if cmdline.is_file():
        new, changed = serial_console_off(cmdline.read_text().strip())
        if changed:
            cmdline.write_text(new + "\n")
            print(f"    took the serial console off {cmdline}")
        else:
            print(f"    {cmdline} already free of a serial console")


LEGACY_UNMANAGED = Path(
    "/etc/NetworkManager/conf.d/10-minidriver-unmanaged.conf")


def drop_legacy_unmanaged():
    """Remove the persistent "wlan0 is unmanaged" drop-in earlier versions wrote.

    That file is why a box could strand itself for good: it survives reboots, so
    once the daemon failed to associate there was no route back in at all. The
    daemon now takes the radio at runtime instead (minidriver.nm), which a power
    cycle undoes. Deleting it here is what repairs an already-bricked card.
    """
    if LEGACY_UNMANAGED.exists():
        LEGACY_UNMANAGED.unlink()
        print(f"    removed {LEGACY_UNMANAGED} (it could strand the box)")
        if shutil.which("systemctl"):
            run(["systemctl", "restart", "NetworkManager"], capture_output=True)
    else:
        print("    no persistent unmanaged drop-in; NetworkManager keeps wlan0"
              " until the daemon asks for it")


def mask_modemmanager():
    """ModemManager probes serial devices on plug-in and can seize the GPS port.
    This box has no cellular modem, so masking it is the clean fix."""
    if shutil.which("systemctl"):
        run(["systemctl", "disable", "--now", "ModemManager"],
            capture_output=True)
        run(["systemctl", "mask", "ModemManager"], capture_output=True)


def gps_on_serial0() -> bool:
    """Whether a GPS HAT looks present on the Pi's UART.

    Only a guess, but a cheap one: the UART exists whenever enable_uart is set,
    attached or not, so presence of the device node proves nothing. A HAT that
    is actually talking shows up as readable NMEA.
    """
    port = "/dev/serial0"
    if not Path(port).exists():
        return False
    descriptor = None
    try:
        # O_NONBLOCK and O_NOCTTY matter: opening a tty can otherwise block
        # waiting for carrier detect, and the installer must never hang on a
        # dangling UART. O_NONBLOCK also means read() returns None rather than
        # waiting, so the loop polls to a deadline instead.
        descriptor = os.open(port, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        heard = b""
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                heard += os.read(descriptor, 256) or b""
            except BlockingIOError:
                pass
            if b"$G" in heard:          # any NMEA talker: GP, GN, GL, GB
                return True
            time.sleep(0.1)
        return False
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def configure_gpsd():
    """Point gpsd at the GPS, and at nothing else.

    Listing /dev/serial0 when no HAT is attached is not harmless. With the UART
    empty and a USB dongle plugged in, gpsd manages two devices, and the
    resulting mix made it report 0,0 for a receiver that was computing a
    perfectly good position -- clients even error with "No path specified in
    DEVICE, but multiple devices are attached". Hours went into blaming the
    dongle for that. So the UART is listed only when something is actually
    talking on it; USBAUTO picks up a dongle either way.
    """
    defaults = Path("/etc/default/gpsd")
    devices = "/dev/serial0" if gps_on_serial0() else ""
    print(f"    serial0 {'has a GPS talking on it' if devices else 'is silent'}"
          f"; DEVICES={devices!r}")
    defaults.write_text(
        "# Managed by minidriver install.\n"
        "# Only devices that actually carry a GPS belong here: an empty UART\n"
        "# alongside a USB dongle makes gpsd report 0,0 for a working receiver.\n"
        f'DEVICES="{devices}"\n'
        'USBAUTO="true"\n'
        'GPSD_OPTIONS="-n"\n'
        'START_DAEMON="true"\n')
    print(f"    wrote {defaults}")
    if shutil.which("systemctl"):
        run(["systemctl", "enable", "gpsd.socket"], capture_output=True)
        run(["systemctl", "restart", "gpsd.socket"], capture_output=True)


def apt_install():
    run(["apt-get", "update"], check=False)
    run(["apt-get", "install", "-y", *APT_PACKAGES], check=False)


def install_code():
    PREFIX.mkdir(parents=True, exist_ok=True)
    for item in ("minidriver", "pyproject.toml"):
        dest = PREFIX / item
        if dest.is_dir():
            shutil.rmtree(dest)
        elif dest.exists():
            dest.unlink()
        src = SRC / item
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
    print(f"    copied sources to {PREFIX}")

    venv = PREFIX / ".venv"
    if not (venv / "bin" / "python").exists():
        # --system-site-packages: Pillow, spidev, lgpio, bleak and dbus come from
        # the OS and must match the system libraries, so the venv borrows them.
        run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
            check=True)
    py = venv / "bin" / "python"
    installed = run([str(py), "-m", "pip", "install", "--quiet", "-e", str(PREFIX)],
                    capture_output=True)
    if installed.returncode != 0:
        # A box on a hotspot may have no package index. The only thing missing is
        # our own package on the path; a .pth provides it with no index at all.
        site = subprocess.run(
            [str(py), "-c", "import site; print(site.getsitepackages()[0])"],
            capture_output=True, text=True).stdout.strip()
        if site:
            (Path(site) / "minidriver.pth").write_text(str(PREFIX) + "\n")
            print("    no package index; put the source on the path instead")

    # Confirm the package imports from the venv before we enable a service on it.
    check = run([str(py), "-c", "import minidriver, yaml"], capture_output=True)
    if check.returncode != 0:
        sys.exit("    FAIL: the venv cannot import minidriver or yaml")
    print("    ok: minidriver importable from the venv")


# The 99- prefix is load-bearing, not decoration. journald applies drop-ins in
# lexicographic order of filename across every config directory, and Raspberry
# Pi OS ships /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf. A
# lower number loses to it and the setting silently does nothing.
BRCMFMAC_CONF = Path("/etc/modprobe.d/brcmfmac-minidriver.conf")
BRCMFMAC_OPTIONS = "options brcmfmac roamoff=1 feature_disable=0x82000\n"

JOURNAL_DROP_IN = Path(
    "/etc/systemd/journald.conf.d/99-minidriver-persistent.conf")

JOURNAL_CONF = """\
# Managed by minidriver install.
[Journal]
# Raspberry Pi OS ships Storage=volatile to spare the SD card, which means a
# reboot destroys the log. This box reboots for reasons worth reading about --
# a full tmpfs once starved systemd until the 1-minute hardware watchdog reset
# the board, leaving nothing behind to explain it.
Storage=persistent
# The other half of that trade: the reason the distro default is volatile is to
# keep writes off the SD card, so bound the volume rather than ignore it. A few
# tens of megabytes still spans many boots of a single-purpose box.
SystemMaxUse=64M
SystemMaxFileSize=8M
Compress=yes
"""


def configure_wifi_firmware():
    """Work around the Zero 2 W brcmfmac association failure.

    The onboard CYW43436 firmware fails to associate with *some* access points,
    which presents as a box that boots fine and never gets a lease -- and it is
    intermittent, so a build that worked yesterday proves nothing. Tracked
    upstream as raspberrypi/trixie-feedback#23, carried over from
    bookworm-feedback#279, so it is not specific to one Raspberry Pi OS release.

    roamoff=1 stops the firmware roaming between BSSIDs; feature_disable turns
    off the feature bits implicated in the reports. Needs a reboot (or a module
    reload) to take effect, which the installer already asks for.
    """
    existing = sorted(Path("/etc/modprobe.d").glob("*brcmfmac*"))
    for path in existing:
        if path != BRCMFMAC_CONF:
            print(f"    note: {path} also sets brcmfmac options")

    if BRCMFMAC_CONF.is_file() and BRCMFMAC_CONF.read_text() == BRCMFMAC_OPTIONS:
        print(f"    {BRCMFMAC_CONF} already set")
        return
    BRCMFMAC_CONF.parent.mkdir(parents=True, exist_ok=True)
    BRCMFMAC_CONF.write_text(BRCMFMAC_OPTIONS)
    print(f"    wrote {BRCMFMAC_CONF}")


def configure_journal():
    """Make the journal survive reboots, with a size cap to limit card wear."""
    JOURNAL_DROP_IN.parent.mkdir(parents=True, exist_ok=True)
    if JOURNAL_DROP_IN.is_file() and JOURNAL_DROP_IN.read_text() == JOURNAL_CONF:
        print(f"    {JOURNAL_DROP_IN} already set")
    else:
        JOURNAL_DROP_IN.write_text(JOURNAL_CONF)
        print(f"    wrote {JOURNAL_DROP_IN}")

    if not shutil.which("systemctl"):
        return
    # journald only picks up the storage change on restart, and --flush is what
    # moves what is already in /run onto the disk.
    run(["systemd-tmpfiles", "--create", "--prefix", "/var/log/journal"],
        capture_output=True)
    run(["systemctl", "restart", "systemd-journald"], capture_output=True)
    run(["journalctl", "--flush"], capture_output=True)


def venv_python() -> Path:
    return PREFIX / ".venv" / "bin" / "python"


def install_epaper_driver():
    """Put the Waveshare panel driver on the venv's path.

    Idempotent and non-fatal: a box installed off a hotspot with no route to
    GitHub still gets a working survey, just a dark screen -- display.ui already
    treats an unavailable panel as a logged non-event.
    """
    py = venv_python()
    # Locate the module without importing it. Waveshare's epdconfig instantiates
    # its RaspberryPi class at import time, which claims the panel's GPIO pins --
    # so actually importing it here would fail with 'GPIO busy' whenever the
    # daemon is running, and wrongly report a healthy install as broken.
    probe = [str(py), "-c", "import importlib.util as u, sys; "
                            "sys.exit(0 if u.find_spec('waveshare_epd.epd2in13_V4')"
                            " else 1)"]
    if run(probe, capture_output=True).returncode == 0:
        print("    waveshare_epd already present")
        return

    site = subprocess.run(
        [str(py), "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True, text=True).stdout.strip()
    if not site:
        print("    !! cannot locate site-packages; the panel will stay dark")
        return

    target = Path(site) / "waveshare_epd"
    target.mkdir(parents=True, exist_ok=True)
    for name in EPAPER_FILES:
        url = f"{EPAPER_BASE}/{name}"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                (target / name).write_bytes(response.read())
        except (urllib.error.URLError, OSError) as exc:
            print(f"    !! could not fetch {name} ({exc}); the panel will stay dark")
            return
    print(f"    fetched the panel driver into {target}")

    if run(probe, capture_output=True).returncode == 0:
        print("    ok: waveshare_epd on the venv path")
    else:
        print("    !! waveshare_epd still not found; the panel will stay dark")


def seed_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target = CONFIG_DIR / "config.yaml"
    if target.exists():
        print(f"    keeping existing {target}")
    else:
        shutil.copy2(SRC / "config.example.yaml", target)
        print(f"    wrote {target} from the example")
    target.chmod(0o600)   # it holds WiFi keys and service tokens


def install_service():
    for name in UNITS:
        shutil.copy2(SRC / "deploy" / name, UNIT_DIR / name)
        print(f"    installed {UNIT_DIR / name}")
    if shutil.which("systemctl"):
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "minidriver.service"])
        # The recovery timer is what gets the box back when the daemon is the
        # broken part, so it is enabled and started independently of it.
        run(["systemctl", "enable", "--now", "minidriver-recover.timer"])
        # The low-battery guard is what stops an abrupt power cut, and the RTC
        # pair is what keeps timestamps sane across one; both run on their own
        # timers so a wedged daemon cannot take them with it.
        run(["systemctl", "enable", "--now", "minidriver-lowbattery.timer"])
        run(["systemctl", "enable", "--now", "minidriver-rtc-save.timer"])
        run(["systemctl", "enable", "minidriver-rtc-restore.service"])


def main() -> int:
    need_root()
    print("==> apt dependencies");       apt_install()
    print("==> boot configuration");     edit_boot()
    print("==> wlan0 ownership");        drop_legacy_unmanaged()
    print("==> ModemManager");           mask_modemmanager()
    print("==> wifi firmware");          configure_wifi_firmware()
    print("==> journal");                configure_journal()
    print("==> gpsd");                    configure_gpsd()
    print("==> code");                    install_code()
    print("==> e-paper driver");         install_epaper_driver()
    print("==> config");                 seed_config()
    print("==> service");                install_service()
    print()
    print("installed. Edit /etc/minidriver/config.yaml (home WiFi + service")
    print("tokens), then reboot to apply the UART/SPI/console changes:")
    print("    sudo nano /etc/minidriver/config.yaml")
    print("    sudo reboot")
    print("After reboot:  journalctl -fu minidriver")
    return 0


if __name__ == "__main__":
    sys.exit(main())
