"""Idempotent edits to the boot config: the fiddly part of install, kept pure so
re-running the installer cannot pile up duplicate lines or strip the wrong token.
"""
from minidriver.provision import ensure_lines, serial_console_off

BOOT_LINES = [
    "enable_uart=1",
    "dtoverlay=miniuart-bt",   # keep BT, hand the GPS the stable PL011 UART
    "dtparam=spi=on",          # the e-paper HAT
]


class TestEnsureLines:
    def test_adds_missing_lines(self):
        new, changed = ensure_lines("dtparam=audio=on\n", BOOT_LINES)
        assert changed is True
        for line in BOOT_LINES:
            assert line in new

    def test_is_idempotent(self):
        once, _ = ensure_lines("", BOOT_LINES)
        twice, changed = ensure_lines(once, BOOT_LINES)
        assert changed is False
        assert twice == once

    def test_keeps_existing_content(self):
        new, _ = ensure_lines("# my notes\ndtparam=audio=on\n", BOOT_LINES)
        assert "# my notes" in new and "dtparam=audio=on" in new

    def test_does_not_duplicate_an_already_present_line(self):
        new, changed = ensure_lines("enable_uart=1\n", ["enable_uart=1"])
        assert changed is False
        assert new.count("enable_uart=1") == 1


class TestSerialConsoleOff:
    def test_removes_the_serial_console_token(self):
        cmdline = ("console=serial0,115200 console=tty1 "
                   "root=PARTUUID=abcd rootfstype=ext4 fsck.repair=yes rootwait")
        out, changed = serial_console_off(cmdline)
        assert "serial0" not in out and changed is True
        # Everything else is left intact.
        assert "console=tty1" in out and "rootwait" in out

    def test_is_idempotent_when_already_off(self):
        cmdline = "console=tty1 root=PARTUUID=abcd rootwait"
        out, changed = serial_console_off(cmdline)
        assert changed is False and out.strip() == cmdline.strip()
