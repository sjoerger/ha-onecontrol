"""Tests for H-Bridge / cover status parsing (event 0x0D/0x0E).

Current-draw byte layout (payload bytes 2-3 of RELAY_TYPE_2_STATUS_PARAMS,
frame offset 5-6) verified against:
  temp/decompiled_source/assembly_0091/IDS.Core.IDS_CAN.Devices/
  RELAY_TYPE_2_STATUS_PARAMS.cs

The 0x0000 idle, 0xFFFF sentinel, and 0x10dc stall vectors below are taken
verbatim from a real extend/retract capture on hardware (see conversation
notes) rather than invented — the stall value is the peak of a sustained
~16-17A signature observed right at full retraction.
"""

from custom_components.ha_onecontrol.const import EVENT_HBRIDGE_2
from custom_components.ha_onecontrol.protocol.events import parse_cover_status, parse_event


def _frame(status: int, position: int, current: int, dtc: int = 0, table=0x16, device=0x02) -> bytes:
    return bytes(
        [
            EVENT_HBRIDGE_2,
            table,
            device,
            status,
            position,
            (current >> 8) & 0xFF,
            current & 0xFF,
            (dtc >> 8) & 0xFF,
            dtc & 0xFF,
        ]
    )


class TestCoverStatusCurrentDraw:
    def test_idle_zero_current(self):
        """Real capture: awning idle, status=stopped, current=0.00A (live zero, not sentinel)."""
        cov = parse_cover_status(bytes.fromhex("0e1602c0ff00000000"))
        assert cov is not None
        assert cov.status == 0xC0
        assert cov.position is None
        assert cov.current_draw == 0.0

    def test_retract_stall_spike(self):
        """Real capture: peak of the retract stall against the fully-retracted stop."""
        cov = parse_cover_status(bytes.fromhex("0e1602c3ff10dc0000"))
        assert cov is not None
        assert cov.status == 0xC3  # closing
        assert cov.current_draw == 4316 / 256.0
        assert round(cov.current_draw, 2) == 16.86

    def test_extend_running_current(self):
        """Real capture: mid-extend running current."""
        cov = parse_cover_status(bytes.fromhex("0e1602c2ff02ad0000"))
        assert cov is not None
        assert cov.status == 0xC2  # opening
        assert round(cov.current_draw, 3) == round(685 / 256.0, 3)

    def test_unsupported_sentinel_is_none(self):
        """Real capture: Slide device reports 0xFFFF — must decode as None, not 255.99A."""
        cov = parse_cover_status(bytes.fromhex("0e160cc0ffffff0000"))
        assert cov is not None
        assert cov.current_draw is None

    def test_near_max_non_sentinel_value(self):
        """0xFFFE is a real (absurd but valid) value, distinct from the 0xFFFF sentinel."""
        cov = parse_cover_status(_frame(status=0xC0, position=0xFF, current=0xFFFE))
        assert cov.current_draw == 0xFFFE / 256.0

    def test_dtc_field_decoded(self):
        cov = parse_cover_status(_frame(status=0xC0, position=0xFF, current=0, dtc=1852))
        # CoverStatus doesn't carry dtc today — this only confirms parsing doesn't
        # choke on a non-zero DTC field; no wind sensor was present to verify
        # the dtc-1851 wind-level mapping against real hardware.
        assert cov is not None

    def test_short_frame_backward_compatible(self):
        """Legacy 5-byte frame (status+position only) must still parse with current_draw=None."""
        cov = parse_cover_status(bytes([EVENT_HBRIDGE_2, 0x16, 0x02, 0xC0, 0xFF]))
        assert cov is not None
        assert cov.status == 0xC0
        assert cov.position is None
        assert cov.current_draw is None

    def test_position_live_when_not_sentinel(self):
        cov = parse_cover_status(_frame(status=0xC2, position=42, current=0))
        assert cov.position == 42

    def test_dispatch_via_parse_event(self):
        """End-to-end through the event dispatcher, not just the direct parser."""
        event = parse_event(bytes.fromhex("0e1602c3ff10dc0000"))
        assert event is not None
        assert round(event.current_draw, 2) == 16.86
