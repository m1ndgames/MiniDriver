"""Reversible handoff of the radio between NetworkManager and the daemon.

The daemon can only own wlan0 safely if the ownership can be given back, so the
handoff is a runtime nmcli call rather than a config file: a reboot alone returns
the radio to NetworkManager and makes the box reachable again.
"""
from minidriver import nm


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return ""

    def ran(self, token):
        return [c for c in self.calls if token in c]


class TestSetManagedArgv:
    def test_names_the_interface_and_the_state(self):
        assert nm.set_managed_argv("wlan0", False) == [
            "nmcli", "device", "set", "wlan0", "managed", "no"]
        assert nm.set_managed_argv("wlan0", True) == [
            "nmcli", "device", "set", "wlan0", "managed", "yes"]


class TestHandoff:
    def test_take_asks_networkmanager_to_let_go(self):
        runner = Runner()
        nm.take("wlan0", runner=runner)
        assert runner.calls == [["nmcli", "device", "set", "wlan0",
                                 "managed", "no"]]

    def test_release_hands_the_radio_back(self):
        runner = Runner()
        nm.release("wlan0", runner=runner)
        assert runner.calls == [["nmcli", "device", "set", "wlan0",
                                 "managed", "yes"]]

    def test_a_missing_nmcli_is_not_fatal(self):
        """A box without NetworkManager simply owns the radio outright; failing
        to hand off must never take the daemon down."""
        def explode(argv):
            raise OSError("no nmcli")

        nm.take("wlan0", runner=explode)
        nm.release("wlan0", runner=explode)
