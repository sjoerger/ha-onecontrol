"""Tests for CommandBuilder.build_action_hbridge.

The overall frame shape (pattern-matched from other single-device action
builders) is still unverified against real official-app BLE traffic — see
the note on build_action_hbridge itself. The command byte *values* tested
here (Stop/Forward/Reverse) are confirmed against real hardware behavior
across many trials — see docs/TECH_SPEC.md § H-Bridge Cover Control. These
tests exist so a future change to either is deliberate, not accidental.
"""

from custom_components.ha_onecontrol.protocol.commands import CommandBuilder


class TestBuildActionHBridge:
    def test_stop_command_shape(self):
        cb = CommandBuilder()
        cmd = cb.build_action_hbridge(0x16, 0x02, CommandBuilder.HBRIDGE_COMMAND_STOP)
        # [cmdid_lsb][cmdid_msb][0x41][table][device][command]
        assert len(cmd) == 6
        assert cmd[2] == CommandBuilder.CMD_ACTION_HBRIDGE
        assert cmd[3] == 0x16
        assert cmd[4] == 0x02
        assert cmd[5] == 0x00

    def test_stop_constant_is_zero(self):
        """Matches HBridgeCommand.Stop=0 from the decompiled vendor source."""
        assert CommandBuilder.HBRIDGE_COMMAND_STOP == 0x00

    def test_command_id_increments(self):
        cb = CommandBuilder()
        cmd1 = cb.build_action_hbridge(0x16, 0x02, 0x00)
        cmd2 = cb.build_action_hbridge(0x16, 0x02, 0x00)
        id1 = cmd1[0] | (cmd1[1] << 8)
        id2 = cmd2[0] | (cmd2[1] << 8)
        assert id2 == (id1 + 1) & 0xFFFF

    def test_device_and_table_masked_to_byte(self):
        cb = CommandBuilder()
        cmd = cb.build_action_hbridge(0x1FF, 0x2FF, 0x00)
        assert cmd[3] == 0xFF
        assert cmd[4] == 0xFF

    def test_reverse_command_shape(self):
        cb = CommandBuilder()
        cmd = cb.build_action_hbridge(0x16, 0x02, CommandBuilder.HBRIDGE_COMMAND_REVERSE)
        assert cmd[5] == 0x03

    def test_reverse_constant_matches_relayhbridgedirection_enum(self):
        """RelayHBridgeDirection.Reverse=3 — the hypothesis being tested after
        value 2 (HBridgeCommand.Reverse) was observed to extend, not retract,
        against real hardware."""
        assert CommandBuilder.HBRIDGE_COMMAND_REVERSE == 0x03
        assert CommandBuilder.HBRIDGE_COMMAND_REVERSE != CommandBuilder.HBRIDGE_COMMAND_STOP
        assert CommandBuilder.HBRIDGE_COMMAND_REVERSE != CommandBuilder.HBRIDGE_COMMAND_FORWARD

    def test_forward_constant_confirmed_by_observation(self):
        """Value 2 — confirmed on real hardware to extend the awning."""
        assert CommandBuilder.HBRIDGE_COMMAND_FORWARD == 0x02
