"""The field<->home mode machine.

The box has one radio. It scans in the FIELD; when a configured home network is
in range it switches to HOME to associate and upload, and when home drops out of
range for long enough it returns to the FIELD. The switch is debounced in both
directions: a single stray beacon off a passing car must not end a drive, and a
single missed scan at home must not abandon the uplink mid-upload.

This machine is pure: it is fed the SSIDs each scan saw and reports the mode. The
daemon does the radio work the mode implies.
"""
from minidriver.statemachine import FIELD, HOME, ModeMachine


def machine(confirm=2, lost=3, home=("home",)):
    return ModeMachine(home_ssids=home, confirm_scans=confirm, lost_checks=lost)


class TestFieldToHome:
    def test_starts_in_the_field(self):
        assert machine().mode == FIELD

    def test_one_sighting_is_not_enough_to_switch(self):
        m = machine(confirm=2)
        assert m.observe({"home", "neighbour"}) == FIELD

    def test_two_consecutive_sightings_switch_to_home(self):
        m = machine(confirm=2)
        m.observe({"home"})
        assert m.observe({"home"}) == HOME

    def test_a_gap_resets_the_count(self):
        m = machine(confirm=2)
        m.observe({"home"})          # 1
        m.observe({"neighbour"})     # gap -> reset
        assert m.observe({"home"}) == FIELD   # only 1 again
        assert m.observe({"home"}) == HOME    # now 2 in a row

    def test_any_configured_home_network_counts(self):
        m = machine(confirm=1, home=("home", "hotspot"))
        assert m.observe({"hotspot"}) == HOME

    def test_confirm_of_one_switches_immediately(self):
        assert machine(confirm=1).observe({"home"}) == HOME


class TestHomeToField:
    def _at_home(self, lost=3):
        m = machine(confirm=1, lost=lost)
        m.observe({"home"})
        assert m.mode == HOME
        return m

    def test_one_missed_scan_does_not_leave_home(self):
        m = self._at_home(lost=3)
        assert m.observe({"neighbour"}) == HOME

    def test_home_lost_for_enough_checks_returns_to_field(self):
        m = self._at_home(lost=3)
        m.observe(set())
        m.observe(set())
        assert m.observe(set()) == FIELD

    def test_seeing_home_again_resets_the_loss_count(self):
        m = self._at_home(lost=3)
        m.observe(set())
        m.observe(set())
        m.observe({"home"})          # back in range -> reset
        assert m.observe(set()) == HOME


class TestTransitionReporting:
    def test_observe_reports_when_the_mode_changed(self):
        m = machine(confirm=1)
        assert m.observe({"home"}) == HOME and m.changed is True
        assert m.observe({"home"}) == HOME and m.changed is False

    def test_present_home_lists_which_networks_are_in_range(self):
        m = machine(home=("home", "hotspot"))
        m.observe({"home", "stranger"})
        assert m.present == {"home"}
