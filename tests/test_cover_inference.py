"""Tests for passive open/closed stall inference (protocol/cover_inference.py).

The full-cycle test replays the actual extend/retract capture from real
hardware (see conversation notes) frame-by-frame through the state machine,
so it's exercised against real motor behavior, not just synthetic values.
"""

from custom_components.ha_onecontrol.protocol.cover_inference import CoverStallInference
from custom_components.ha_onecontrol.protocol.events import parse_cover_status

KEY = "16:02"

# Real captured hex, in order, from a full extend-then-retract cycle
# (idle -> opening -> stopped -> idle -> closing -> stopped).
_EXTEND_RETRACT_CAPTURE = [
    "0e1602c0ff00000000",
    "0e1602c0ff00000000",
    "0e1602c2ff00a00000",
    "0e1602c2ff02ad0000",
    "0e1602c2ff02760000",
    "0e1602c2ff02800000",
    "0e1602c2ff01f70000",
    "0e1602c2ff015d0000",
    "0e1602c0ff00be0000",
    "0e1602c0ff00000000",
    "0e1602c3ff00a00000",
    "0e1602c3ff02c80000",
    "0e1602c3ff04080000",
    "0e1602c3ff069e0000",
    "0e1602c3ff085f0000",
    "0e1602c3ff07760000",
    "0e1602c3ff06d60000",
    "0e1602c3ff05f70000",
    "0e1602c3ff054b0000",
    "0e1602c3ff04f00000",
    "0e1602c3ff04830000",
    "0e1602c3ff03ed0000",
    "0e1602c3ff02fa0000",
    "0e1602c3ff07380000",
    "0e1602c3ff0f5f0000",
    "0e1602c3ff10d00000",  # 4304/256 = 16.81A
    "0e1602c3ff105a0000",  # 4186/256 = 16.35A — 2nd consecutive stall sample
    "0e1602c0ff02480000",  # stopped
]


class TestCoverStallInference:
    def test_unknown_before_any_motion_observed(self):
        inf = CoverStallInference()
        assert inf.get(KEY) is None

    def test_opening_sets_open_immediately(self):
        inf = CoverStallInference()
        cov = parse_cover_status(bytes.fromhex("0e1602c2ff02ad0000"))
        inf.update(KEY, cov)
        assert inf.get(KEY) is False

    def test_stop_without_stall_stays_unknown(self):
        """Manual stop mid-travel (no sustained high current) must not claim closed."""
        inf = CoverStallInference()
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff00a00000")))  # 0.63A, closing
        confirmed = inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c0ff00000000")))  # stopped
        assert confirmed is None
        assert inf.get(KEY) is None

    def test_single_high_sample_not_enough(self):
        """Below the confirm-samples threshold (default 2), one spike shouldn't confirm closed."""
        inf = CoverStallInference()
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff10d00000")))  # 16.8A, closing
        confirmed = inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c0ff00000000")))  # stopped
        assert confirmed is None
        assert inf.get(KEY) is None

    def test_two_consecutive_stall_samples_confirms_closed(self):
        inf = CoverStallInference()
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff10d00000")))  # 16.8A
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff105a0000")))  # 16.35A
        confirmed = inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c0ff00000000")))  # stopped
        assert confirmed == 2
        assert inf.get(KEY) is True

    def test_non_consecutive_high_samples_do_not_accumulate(self):
        """A dip below threshold between two spikes resets the streak."""
        inf = CoverStallInference()
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff10d00000")))  # 16.8A — streak=1
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff02fa0000")))  # 2.98A — resets streak
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff10d00000")))  # 16.8A — streak=1 again
        confirmed = inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c0ff00000000")))  # stopped
        assert confirmed is None
        assert inf.get(KEY) is None

    def test_unsupported_current_never_confirms(self):
        """Device reporting the 0xFFFF sentinel (e.g. a Slide) can never trigger a stall."""
        inf = CoverStallInference()
        inf.update("16:0c", parse_cover_status(bytes.fromhex("0e160cc3ffffff0000")))
        inf.update("16:0c", parse_cover_status(bytes.fromhex("0e160cc3ffffff0000")))
        confirmed = inf.update("16:0c", parse_cover_status(bytes.fromhex("0e160cc0ffffff0000")))
        assert confirmed is None
        assert inf.get("16:0c") is None

    def test_full_real_capture_extend_then_retract(self):
        """Replay the actual capture: extend leaves it open, retract confirms closed."""
        inf = CoverStallInference()
        for hex_frame in _EXTEND_RETRACT_CAPTURE:
            cov = parse_cover_status(bytes.fromhex(hex_frame))
            inf.update(KEY, cov)
            if cov.ha_state == "opening":
                # Must read open (False) throughout the extend phase.
                assert inf.get(KEY) is False
        # After the full cycle (extend, then retract-to-stall), inferred closed.
        assert inf.get(KEY) is True

    def test_reopening_after_confirmed_close_clears_it(self):
        inf = CoverStallInference()
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff10d00000")))
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff105a0000")))
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c0ff00000000")))
        assert inf.get(KEY) is True
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c2ff02ad0000")))  # opening again
        assert inf.get(KEY) is False

    def test_is_stalled_true_while_still_reported_closing(self):
        """is_stalled() must not wait for a "stopped" frame — an active
        auto-retract loop needs to detect the stall while it's still the one
        causing "closing" to keep being reported. See test below for the
        real-hardware scenario this exists to fix."""
        inf = CoverStallInference()
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff10d00000")))
        assert inf.is_stalled(KEY) is False  # only 1 sample so far
        inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff105a0000")))
        assert inf.is_stalled(KEY) is True  # 2nd consecutive sample — status still "closing"
        assert inf.get(KEY) is None  # get()/is_closed must NOT have fired yet — no "stopped" frame

    def test_real_capture_stuck_closing_never_reports_stopped(self):
        """Real capture (full-logs-12): with an active auto-retract loop
        continuously resending Reverse, the device kept reporting "closing"
        for 4 consecutive stall-current samples spanning 6.5s and never
        transitioned to "stopped" on its own — get()/update() alone would
        never confirm closed here. is_stalled() must catch it well before
        the 4th sample, from real captured hex."""
        inf = CoverStallInference()
        frames = [
            "0e1602c3ff0cab0000",  # 12.67A
            "0e1602c3ff10390000",  # 16.22A — 2nd consecutive, is_stalled() should trip here
            "0e1602c3ff103f0000",  # 16.25A
            "0e1602c3ff0ffc0000",  # 15.98A
        ]
        tripped_at = None
        for i, hex_frame in enumerate(frames):
            inf.update(KEY, parse_cover_status(bytes.fromhex(hex_frame)))
            if inf.is_stalled(KEY) and tripped_at is None:
                tripped_at = i
            assert inf.get(KEY) is None  # never confirmed via get() — no "stopped" frame in this data
        assert tripped_at == 1  # tripped on the 2nd sample, not the 4th

    def test_custom_thresholds(self):
        inf = CoverStallInference(stall_current_a=5.0, confirm_samples=1)
        confirmed = inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c3ff069e0000")))  # 6.6A
        assert confirmed is None  # still closing, not stopped yet
        confirmed = inf.update(KEY, parse_cover_status(bytes.fromhex("0e1602c0ff00000000")))
        assert confirmed == 1
        assert inf.get(KEY) is True
