"""The e-paper status dashboard.

The 2.13" panel is 250x122, monochrome and has no buttons, so the UI is
output-only: a handful of status lines. PIL and the Waveshare driver are
hardware; what is tested is how a status snapshot becomes those lines.
"""
from minidriver.display.ui import status_lines


def status(**over):
    base = {
        "mode": "field",
        "gps": {"fix": True, "mode": 3, "sats_used": 7, "sats_seen": 11},
        "wifi_aps": 42, "ble_devices": 5, "rows": 318,
        "pending": {"wigle": 2, "wdgwars": 2, "wardrift": 2},
        "last_upload": None,
        "ip": None,
    }
    base.update(over)
    return base


def joined(**over):
    return "\n".join(status_lines(status(**over)))


def test_shows_the_mode():
    assert "FIELD" in joined(mode="field")
    assert "HOME" in joined(mode="home")


def test_shows_a_gps_fix_with_satellites():
    text = joined(gps={"fix": True, "mode": 3, "sats_used": 7, "sats_seen": 11})
    assert "7" in text and "11" in text


def test_shows_no_fix_when_there_is_none():
    text = joined(gps={"fix": False, "mode": 1, "sats_used": 0, "sats_seen": 9})
    assert "no fix" in text.lower()


def test_shows_wifi_and_ble_counts():
    text = joined(wifi_aps=42, ble_devices=5)
    assert "42" in text and "5" in text


def test_shows_the_lan_ip_when_home():
    assert "192.168.1.9" in joined(mode="home", ip="192.168.1.9")


def test_shows_pending_upload_total_in_the_field():
    # Six captures waiting across services should surface as a number to upload.
    assert "6" in joined(pending={"wigle": 2, "wdgwars": 2, "wardrift": 2})


def test_survives_a_sparse_status():
    # A status taken before the first scan must still render without raising.
    lines = status_lines({"mode": "field"})
    assert isinstance(lines, list) and lines
