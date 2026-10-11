"""Pure, idempotent edits to the boot configuration.

install.py runs these over /boot/firmware/config.txt and cmdline.txt. Keeping the
text transforms here, separate from the file and apt work, means the part most
likely to go wrong on a re-run -- adding a line that is already there, or stripping
the wrong token -- is tested rather than trusted.
"""

MANAGED_HEADER = "# --- added by minidriver install ---"


def ensure_lines(text: str, required) -> tuple:
    """Return (new_text, changed): *text* with every line in *required* present.

    A line already in the file (matched on its stripped form) is left alone, so
    re-running the installer adds nothing. Missing lines are appended under a
    header that marks where they came from.
    """
    existing = {line.strip() for line in (text or "").splitlines()}
    missing = [line for line in required if line.strip() not in existing]
    if not missing:
        return text, False

    body = text or ""
    if body and not body.endswith("\n"):
        body += "\n"
    if MANAGED_HEADER not in body:
        body += MANAGED_HEADER + "\n"
    body += "\n".join(missing) + "\n"
    return body, True


def serial_console_off(cmdline: str) -> tuple:
    """Return (new_cmdline, changed): the kernel cmdline with any serial-console
    token removed, so the login console does not fight the GPS for the UART.

    cmdline.txt is a single space-separated line; only the console=serial0... token
    is dropped and everything else is kept in order.
    """
    tokens = (cmdline or "").split()
    kept = [t for t in tokens if not t.startswith("console=serial0")]
    changed = len(kept) != len(tokens)
    return " ".join(kept), changed
