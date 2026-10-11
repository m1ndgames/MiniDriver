"""Home-network config: WPA-PSK derivation and the wpa_supplicant file the
daemon writes before it associates. Ported from m1ndb0x; the daemon here owns
the radio, so there is no systemd restart -- station.py drives wpa_supplicant."""
import os

import pytest

from minidriver.netconfig import (SUPPLICANT_HEADER, psk_hash,
                                   render_supplicant_conf, write_supplicant_conf)

HOME = {"ssid": "test-home-wifi", "psk": "hunter2hunter2"}
HOTSPOT = {"ssid": "phone", "psk": "hotspot-secret", "priority": 1}


class TestPskDerivation:
    def test_matches_the_ieee_test_vector(self):
        # WPA-PSK is PBKDF2-HMAC-SHA1 over the passphrase, salted with the SSID.
        # Pinned to the published vector so a wrong iteration count or salt
        # cannot pass quietly -- it would simply never associate.
        assert psk_hash("IEEE", "password") == (
            "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e")

    def test_same_passphrase_different_ssid_differs(self):
        assert psk_hash("one", "pass") != psk_hash("two", "pass")


class TestRender:
    def test_header_marks_the_file_generated(self):
        assert SUPPLICANT_HEADER.strip().startswith("#")

    def test_each_network_gets_a_block(self):
        assert render_supplicant_conf([HOME, HOTSPOT]).count("network={") == 2

    def test_ssid_is_quoted(self):
        assert '"test-home-wifi"' in render_supplicant_conf([HOME])

    def test_passphrase_is_not_written_in_clear(self):
        text = render_supplicant_conf([HOME])
        assert "hunter2hunter2" not in text
        assert psk_hash(HOME["ssid"], HOME["psk"]) in text

    def test_precomputed_key_is_used_as_given(self):
        key = psk_hash("somewhere", "secret")
        assert key in render_supplicant_conf([{"ssid": "somewhere", "psk_hash": key}])

    def test_open_network_has_key_mgmt_none(self):
        assert "key_mgmt=NONE" in render_supplicant_conf([{"ssid": "cafe"}])

    def test_priority_is_carried_through(self):
        assert "priority=1" in render_supplicant_conf([HOTSPOT])

    def test_hidden_network_is_marked_for_scanning(self):
        assert "scan_ssid=1" in render_supplicant_conf(
            [{"ssid": "ghost", "psk": "12345678", "hidden": True}])

    def test_country_is_declared(self):
        assert "country=DE" in render_supplicant_conf([HOME], country="DE")

    def test_a_network_with_no_ssid_is_refused(self):
        with pytest.raises(ValueError):
            render_supplicant_conf([{"psk": "orphaned"}])

    def test_empty_list_still_renders_a_valid_file(self):
        text = render_supplicant_conf([])
        assert "ctrl_interface" in text and "network={" not in text

    def test_quotes_in_an_ssid_cannot_break_out(self):
        text = render_supplicant_conf(
            [{"ssid": 'evil"\nnetwork={ssid="x', "psk": "12345678"}])
        assert text.count("network={") == 1


class TestWriteSupplicantConf:
    def test_writes_the_rendered_config(self, tmp_path):
        path = tmp_path / "wpa.conf"
        write_supplicant_conf([HOME], path, country="DE")
        assert '"test-home-wifi"' in path.read_text()

    def test_reports_change_then_no_change(self, tmp_path):
        path = tmp_path / "wpa.conf"
        assert write_supplicant_conf([HOME], path) is True
        # Rewriting identical content would needlessly bounce the association.
        assert write_supplicant_conf([HOME], path) is False

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
    def test_the_file_is_not_world_readable(self, tmp_path):
        path = tmp_path / "wpa.conf"
        write_supplicant_conf([HOME], path)
        assert oct(path.stat().st_mode & 0o777) == "0o600"
