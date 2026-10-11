"""Active-scan parsing: `iw scan` text into AP dicts, and the helpers that shape
them for the survey. Ported from m1ndb0x's bettercap survey, which proved this
parsing against real `iw` output."""
from minidriver import wifi

SCAN_TEXT = """\
BSS aa:bb:cc:00:00:01(on wlan0)
	freq: 2412.0
	signal: -84.00 dBm
	last seen: 2800 ms ago
	SSID: HomeNet
	DS Parameter set: channel 1
	capability: ESS Privacy (0x0411)
	RSN:	 * Version: 1
		 * Group cipher: CCMP
		 * Pairwise ciphers: CCMP
		 * Authentication suites: PSK
	WPS:	 * Version: 1.0
		 * Device name: FBox
BSS aa:bb:cc:00:00:02(on wlan0)
	freq: 6295.0
	signal: -70.00 dBm
	SSID: SixE
	capability: ESS Privacy (0x1111)
	RSN:	 * Version: 1
		 * Group cipher: CCMP
		 * Pairwise ciphers: CCMP
		 * Authentication suites: SAE
BSS aa:bb:cc:00:00:03(on wlan0)
	freq: 2437.0
	signal: -75.00 dBm
	SSID: OldTkip
	capability: ESS Privacy (0x0411)
	WPA:	 * Version: 1
		 * Group cipher: TKIP
		 * Pairwise ciphers: TKIP CCMP
		 * Authentication suites: PSK
	RSN:	 * Version: 1
		 * Group cipher: TKIP
		 * Pairwise ciphers: TKIP CCMP
		 * Authentication suites: PSK
BSS aa:bb:cc:00:00:04(on wlan0)
	freq: 5180.0
	signal: -50.00 dBm
	SSID: Cafe, upstairs
	capability: ESS (0x0001)
BSS aa:bb:cc:00:00:05(on wlan0)
	freq: 5200.0
	signal: -60.00 dBm
	SSID: 
	capability: ESS Privacy (0x0411)
	RSN:	 * Version: 1
		 * Pairwise ciphers: CCMP
		 * Authentication suites: PSK
"""


def scanned(mac):
    return next(a for a in wifi.parse_scan(SCAN_TEXT) if a["mac"] == mac)


class TestChannelAndFrequency:
    def test_channel_off_each_band_base(self):
        assert wifi.channel_for(2412) == "1"
        assert wifi.channel_for(2484) == "14"
        assert wifi.channel_for(5180) == "36"
        assert wifi.channel_for(6295) == "69"      # 6 GHz, not 2.4 ch 69

    def test_channel_unknown_is_blank(self):
        assert wifi.channel_for(None) == ""
        assert wifi.channel_for("x") == ""

    def test_frequency_from_channel(self):
        assert wifi.frequency_for(1) == 2412
        assert wifi.frequency_for(14) == 2484
        assert wifi.frequency_for("x") == ""


class TestAuthMode:
    def test_wpa2_psk(self):
        assert wifi.auth_mode(scanned("aa:bb:cc:00:00:01")) == "[WPA2-PSK-CCMP][ESS]"

    def test_open_is_only_ess(self):
        assert wifi.auth_mode(scanned("aa:bb:cc:00:00:04")) == "[ESS]"

    def test_nothing_known_is_valid(self):
        assert wifi.auth_mode({}) == "[ESS]"


class TestParseScan:
    def test_one_entry_per_bss(self):
        assert len(wifi.parse_scan(SCAN_TEXT)) == 5

    def test_wpa3_6ghz(self):
        ap = scanned("aa:bb:cc:00:00:02")
        assert ap["channel"] == "69"
        assert wifi.auth_mode(ap) == "[WPA3-SAE-CCMP][ESS]"

    def test_tkip_rsn_prefers_ccmp(self):
        assert wifi.auth_mode(scanned("aa:bb:cc:00:00:03")) == "[WPA2-PSK-CCMP][ESS]"

    def test_signal_and_frequency(self):
        ap = scanned("aa:bb:cc:00:00:01")
        assert ap["frequency"] == 2412 and ap["rssi"] == -84

    def test_hidden_network_has_no_name(self):
        assert wifi.ssid(scanned("aa:bb:cc:00:00:05")) == ""

    def test_scanned_ap_is_a_fresh_sighting(self):
        now = 1_790_000_000.0
        ap = wifi.parse_scan(SCAN_TEXT, now_wall=now)[0]
        assert wifi.seen_at(ap["last_seen"]) == now


class TestVisibleSsids:
    def test_lists_named_networks_only(self):
        # The hidden network (empty SSID) is not a name we can match a home
        # network against, so it is left out.
        assert wifi.visible_ssids(wifi.parse_scan(SCAN_TEXT)) == {
            "HomeNet", "SixE", "OldTkip", "Cafe, upstairs"}


class TestScanArgv:
    def test_targets_the_interface(self):
        argv = wifi.scan_argv("wlan0")
        assert argv[0] == "iw" and "wlan0" in argv and "scan" in argv
