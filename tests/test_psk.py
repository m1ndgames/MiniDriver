"""The `python3 -m minidriver.psk <ssid>` helper: turn a passphrase into the
paste-ready psk_hash line, so the passphrase need never be written into config."""
from minidriver.netconfig import psk_hash
from minidriver.psk import format_entry


def test_the_entry_carries_the_ssid_and_derived_key():
    text = format_entry("home", "hunter2hunter2")
    assert "home" in text
    assert psk_hash("home", "hunter2hunter2") in text


def test_the_passphrase_itself_is_not_in_the_output():
    assert "hunter2hunter2" not in format_entry("home", "hunter2hunter2")
