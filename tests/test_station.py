"""Daemon-owned control of the single radio.

The daemon owns wlan0 directly (NetworkManager is told not to manage it): it
scans unassociated in the field, and brings up wpa_supplicant + DHCP itself when
a home network is in range. The subprocess calls take an injected runner, so the
command sequence and the IP parsing are tested without a radio.
"""
from pathlib import Path

from minidriver import station
from minidriver.station import Station, parse_ipv4


class Runner:
    """Records argv it is asked to run and returns canned stdout per command."""

    def __init__(self, outputs=None):
        self.calls = []
        self._outputs = outputs or {}

    def __call__(self, argv):
        self.calls.append(argv)
        for key, out in self._outputs.items():
            if key in argv:
                return out
        return ""

    def ran(self, token):
        return [c for c in self.calls if token in c]


class TestParseIpv4:
    def test_extracts_the_address(self):
        text = ("2: wlan0    inet 192.168.1.42/24 brd 192.168.1.255 scope "
                "global dynamic noprefixroute wlan0")
        assert parse_ipv4(text) == "192.168.1.42"

    def test_no_address_is_none(self):
        assert parse_ipv4("2: wlan0    (no inet)") is None
        assert parse_ipv4("") is None


class TestArgvBuilders:
    def test_scan_targets_the_interface(self):
        assert station.scan_argv("wlan0") == ["iw", "dev", "wlan0", "scan"]

    def test_wpa_supplicant_names_interface_and_config(self, tmp_path):
        conf = tmp_path / "wpa.conf"
        argv = station.wpa_supplicant_argv("wlan0", conf)
        assert "wlan0" in argv and str(conf) in argv


class TestAssociate:
    def test_writes_config_then_starts_supplicant_then_dhcp(self, tmp_path):
        runner = Runner()
        st = Station("wlan0", supplicant_path=tmp_path / "wpa.conf", runner=runner)
        st.associate([{"ssid": "home", "psk": "12345678"}], country="DE")
        # The config was written with the derived key (not the passphrase).
        text = Path(tmp_path / "wpa.conf").read_text()
        assert '"home"' in text and "12345678" not in text
        # ...and wpa_supplicant was started before an address was asked for.
        # The lease *release* comes earlier, as part of replacing the old
        # association, so only the acquiring call counts here.
        order = [c for c in runner.calls
                 if ("wpa_supplicant" in c
                     or (any(t in c for t in ("dhclient", "udhcpc", "dhcpcd"))
                         and "-r" not in c))]
        assert "wpa_supplicant" in order[0]

    def test_associate_replaces_a_running_supplicant(self, tmp_path):
        """A second wpa_supplicant on the same interface just fails to start,
        and the old one keeps its old config -- which made every attempt to
        switch networks a silent no-op. The lease must go too, or the box keeps
        an address from the network it just left."""
        runner = Runner()
        st = Station("wlan0", supplicant_path=tmp_path / "wpa.conf", runner=runner)
        st.associate([{"ssid": "home", "psk": "12345678"}])
        order = [" ".join(c) for c in runner.calls]
        terminate = next(i for i, c in enumerate(order) if "wpa_cli" in c)
        start = next(i for i, c in enumerate(order) if "wpa_supplicant" in c)
        assert terminate < start, "must stop the old supplicant before starting"
        assert any("-r" in c for c in runner.calls), "must release the lease"

    def test_stopping_an_association_does_not_rely_on_the_control_socket(self, tmp_path):
        """`wpa_cli terminate` only reaches a supplicant whose control socket is
        where we expect it. A survivor keeps its old config and makes the next
        association a silent no-op, so there has to be a kill by pid too."""
        runner = Runner()
        st = Station("wlan0", supplicant_path=tmp_path / "wpa.conf", runner=runner)
        st.associate([{"ssid": "home", "psk": "12345678"}])
        killed = [c for c in runner.calls if "pkill" in c]
        assert killed, "must also stop the supplicant by pid"
        assert any("wpa_supplicant" in a and "wlan0" in a
                   for c in killed for a in c)

    def test_enter_field_tears_down_any_association(self, tmp_path):
        runner = Runner()
        st = Station("wlan0", supplicant_path=tmp_path / "wpa.conf", runner=runner)
        st.enter_field()
        # Something must stop wpa_supplicant so the radio is free to scan.
        assert runner.ran("wpa_cli") or any(
            "wpa_supplicant" in c and ("kill" in " ".join(c).lower()
                                       or "terminate" in " ".join(c).lower()
                                       or "-k" in c) for c in runner.calls) \
            or any("pkill" in c or "killall" in c for c in runner.calls)

    def test_entering_the_field_claims_the_radio_from_networkmanager(self, tmp_path):
        runner = Runner()
        st = Station("wlan0", supplicant_path=tmp_path / "wpa.conf", runner=runner)
        st.enter_field()
        assert ["nmcli", "device", "set", "wlan0", "managed", "no"] in runner.calls

    def test_teardown_hands_the_radio_back(self, tmp_path):
        """The only route into a WiFi-only box is NetworkManager reconnecting,
        so giving up the radio has to be part of shutting down."""
        runner = Runner()
        st = Station("wlan0", supplicant_path=tmp_path / "wpa.conf", runner=runner)
        st.teardown()
        assert ["nmcli", "device", "set", "wlan0", "managed", "yes"] in runner.calls

    def test_address_reads_the_current_ip(self, tmp_path):
        runner = Runner(outputs={"addr": "3: wlan0 inet 10.0.0.5/24 scope global"})
        st = Station("wlan0", supplicant_path=tmp_path / "wpa.conf", runner=runner)
        assert st.address() == "10.0.0.5"
