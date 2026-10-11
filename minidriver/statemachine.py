"""The field<->home mode machine for the single radio.

The box cannot scan and stay associated at once, so it is always in exactly one
of two modes, and this object decides which. It is fed the set of SSIDs each scan
saw and reports the mode; the daemon does the radio work each mode implies
(scanning and writing the survey in the FIELD; associating, getting an address
and uploading at HOME).

Both transitions are debounced so the mode does not flap:
  - FIELD -> HOME only after a home network is seen in `confirm_scans` scans in a
    row, so a single beacon caught off a passing car does not end a drive.
  - HOME -> FIELD only after home is missing for `lost_checks` scans in a row, so
    one missed scan does not drop the uplink in the middle of an upload.

Pure and clock-free: it owns no radio and no timer, which is what makes the
switch logic testable without hardware.
"""

FIELD = "field"
HOME = "home"


class ModeMachine:
    def __init__(self, home_ssids, confirm_scans: int = 2, lost_checks: int = 3):
        self._home = set(home_ssids or [])
        # A threshold below one would transition before any observation; clamp
        # so a mis-set config cannot make the switch meaningless.
        self._confirm = max(1, int(confirm_scans))
        self._lost = max(1, int(lost_checks))
        self.mode = FIELD
        self.present = set()        # configured home networks in the last scan
        self.changed = False        # did the last observe() flip the mode
        self._seen_streak = 0       # consecutive scans with home present (FIELD)
        self._gone_streak = 0       # consecutive scans with home absent (HOME)

    def observe(self, visible_ssids) -> str:
        """Feed one scan's SSIDs; return the resulting mode."""
        self.present = self._home & set(visible_ssids or [])
        home_in_range = bool(self.present)
        before = self.mode

        if self.mode == FIELD:
            self._seen_streak = self._seen_streak + 1 if home_in_range else 0
            if self._seen_streak >= self._confirm:
                self._enter(HOME)
        else:
            self._gone_streak = 0 if home_in_range else self._gone_streak + 1
            if self._gone_streak >= self._lost:
                self._enter(FIELD)

        self.changed = self.mode != before
        return self.mode

    def _enter(self, mode: str) -> None:
        self.mode = mode
        self._seen_streak = 0
        self._gone_streak = 0
