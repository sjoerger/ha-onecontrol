"""Passive stall-current inference for H-Bridge cover devices (awning, slides).

Purely observational: reacts to CoverStatus events regardless of who drove
the motion (wall switch or, in future, HA), and never sends a command.

Retract: derived from a single real extend/retract capture on hardware —
idle/running retract current was 0.6-8.4A, the fully-retracted mechanical
stop held 15-17A for several seconds. No minimum close duration is
enforced — a mid-travel obstruction producing the same current spike will
also be inferred as "closed", so this is a best guess, not a verified
endstop detector.

Extend: unlike retract, hitting full extension has NO device-side backstop
— there is no auto-reverse; driven further, the fabric just keeps wrapping
backward on the roller toward the RV sidewall (see cover.py). The impact
signature is a current spike reaching ~10.2-10.3A on two clean, independent
wall-switch trials (full runs from fully-retracted, both landing at almost
exactly 27.06s), with a wider 8-13A range seen when the motor was
restarted from an already-extended position rather than ramping in fresh.
Normal running/inrush current across every capture on file never exceeded
4.66A.

DEFAULT_EXTEND_STALL_CONFIRM_SAMPLES is 1, not 2 like retract: the extend
ramp climbs very fast once it starts (~2-3A per ~300ms sample), unlike
retract's noisier signal that the 2-sample confirm exists to filter.
Requiring a 2nd confirmation sample on a ramp this steep meant the detector
only fired once current was already close to the ~10.2A unimpeded peak
(trial 1, 6.5A/2-sample: still overshot visibly). Firing on the first
sample trades a little noise-rejection margin for reacting earlier on an
already fast, low-noise signal. Revisit if single-sample false-positives
start showing up during normal mid-run operation.

DEFAULT_EXTEND_STALL_CURRENT_A was originally 6.5A, clear of the 4.66A
normal ceiling with margin below the lowest confirmed impact reading. Trial
2 (1-sample confirm, 6.5A) still overshot: current jumped 2.12A -> 5.28A ->
8.00A across two ~300ms samples, so the 5.28A sample (already well past
normal) landed *under* the 6.5A line and the detector had to wait for the
next sample, by which point current was already near the unimpeded peak.
Lowered to 5.0A to catch that intermediate sample instead — still ~0.3A
above the highest normal reading on file, but that margin is thin (only a
handful of trials establish the 4.66A ceiling).

Lowered again to 4.0A, this time paired with `warmup_samples=2`: a bare
4.0A threshold would fire on the ordinary startup inrush spike (2nd sample
of every run, regularly 3.2-4.66A) within the first ~0.3s of any extend
attempt — nowhere near full extension. `warmup_samples` skips threshold
checks for the first N samples of a run (the 1st is always a low ~0.6-1.1A
startup value, the 2nd is the inrush spike; by the 3rd, current has always
settled to the normal ~2A band in every trial on file), so 2 is enough
margin without needing a value-based gate. Checked against all 8
continuous-loop trials on file with this exact combination
(warmup_samples=2, 4.0A): zero false triggers, and it catches the real
ramp a full sample earlier than 5.0A did in 3 of the last 4 trials
(logs-21/23/25 all show a sample in the 4.0-5.0A range that 5.0A had to
skip past).

ExtendDipDetector (below) exists because even 5.0A/1-sample/event-driven
still overshot slightly (trial 3, logs-25) — current physically crosses
whatever absolute threshold sometime *between* two ~300ms samples, so by
the time a sample confirms it, current (and position) has already moved
on. Four independent trials (logs-21/22/23/25) all showed the *same*
precursor before the real ramp: current tapers down over several seconds,
bottoms out at ~0.96-1.00A, then recovers to the normal ~2A baseline and
holds there for almost exactly **1.0s** (0.958-1.021s across all four —
tight enough to not be coincidence) before the real ramp begins. That
matches the user's own direct physical observation from the very first
trial, made before any of this detection logic existed: the awning visibly
pauses for about 1 second at full extension before it starts wrapping
backward. The dip is very likely *reaching the limit itself* — current
drops because the motor briefly stops fighting normal extending
resistance — with the pause being that physical moment, and the ramp
being the wrap-backward motion starting. Triggering on the dip instead of
the ramp gains back nearly the full 1s the ramp-based detector was giving
away by construction. A control trial (logs-26, stopped manually at 22s,
just short of the dip window) showed the same taper trajectory as the
other four right up to the point of intervention, with no premature dip —
supporting but not proving the dip is specific to reaching the limit,
since that trial didn't cover the final ~2s before the dip itself.

ExtendDipDetector is NOT currently wired into the coordinator — reverted
after its first live trial (logs-27) produced a false-early-stop: that
run's normal pre-dip taper reached 1.26A, well past the ~1.49-1.62A taper
ceiling the 4 prior trials suggested, and crossed the 1.3A dip line before
the real dip ever occurred (confirmed real dips: 0.96-1.00A). Requiring 2
consecutive low samples doesn't fix this — the real dip only ever shows up
as a single low sample before recovering ~1s later, so a 2-sample confirm
would make the detector unable to ever confirm a genuine dip. With only
~0.3A separating the confirmed real-dip range from this confirmed false
trigger, and only 5-6 trials total behind either number, the margin isn't
trustworthy yet. User's call: a small ramp-detector overshoot is an
acceptable tradeoff (hasn't caused problems retracting afterward) versus
stopping short, which defeats the point of an auto-extend feature. The
class is left here, unused, in case it's worth revisiting with a lot more
trials establishing the taper's real floor.
"""

from __future__ import annotations

from .events import CoverStatus

DEFAULT_STALL_CURRENT_A = 10.0
DEFAULT_STALL_CONFIRM_SAMPLES = 2

DEFAULT_EXTEND_STALL_CURRENT_A = 4.0
DEFAULT_EXTEND_STALL_CONFIRM_SAMPLES = 1
DEFAULT_EXTEND_STALL_WARMUP_SAMPLES = 2

# Current must reach this level at least once during the run before dip
# detection arms — skips past the startup transient (first sample is
# always ~0.6A, which is itself below DEFAULT_EXTEND_DIP_CURRENT_A) without
# needing a sample-count or elapsed-time gate. The inrush spike (~4.3-4.6A,
# always the 2nd sample) clears this easily, arming detection from the 3rd
# sample onward — long before the real dip, which only ever appears after
# ~24s of steady running in every trial on file.
DEFAULT_EXTEND_DIP_ARM_CURRENT_A = 1.8
# Confirmed dip readings across 4 trials: 0.96, 0.96, 1.00, 0.97A. Highest
# point reached during the gradual taper leading into the dip, across the
# same trials: 1.49-1.62A around the 18-22s mark. 1.3A sits with margin
# below the taper's high point and above the dip's own range.
DEFAULT_EXTEND_DIP_CURRENT_A = 1.3


class CoverStallInference:
    """Tracks, per device key, a sustained-stall-current streak in one
    direction of travel (`target_state`: "closing" for retract, "opening"
    for extend).

    For the retract/"closing" instance, also exposes a best-guess stowed
    (closed) state via get(): True only after a closing motion ends in a
    confirmed stall, False as soon as any opening motion is observed, None
    (unknown) before either has been observed. That closed/stowed concept
    doesn't have an "open" analogue worth tracking — is_closed already
    reads False the instant any opening motion is seen — so an
    "opening"-direction instance only ever uses is_stalled(); get() will
    always return None for it.

    `warmup_samples` (default 0, i.e. no warm-up — retract's behavior is
    unchanged) skips the stall-current check for the first N samples of a
    `target_state` run, without resetting anything. Exists for the extend
    instance: the startup inrush spike (2nd sample of every run) regularly
    exceeds its low current threshold on its own, nowhere near full
    extension — see DEFAULT_EXTEND_STALL_CURRENT_A docstring.
    """

    def __init__(
        self,
        stall_current_a: float = DEFAULT_STALL_CURRENT_A,
        confirm_samples: int = DEFAULT_STALL_CONFIRM_SAMPLES,
        target_state: str = "closing",
        warmup_samples: int = 0,
    ) -> None:
        self._stall_current_a = stall_current_a
        self._confirm_samples = confirm_samples
        self._target_state = target_state
        self._opposite_state = "opening" if target_state == "closing" else "closing"
        self._warmup_samples = warmup_samples
        self._stall_streak: dict[str, int] = {}
        self._sample_count: dict[str, int] = {}
        self._inferred_closed: dict[str, bool | None] = {}

    def update(self, key: str, event: CoverStatus) -> int | None:
        """Process one CoverStatus event for device `key`.

        Returns the confirmed streak length if this call just confirmed a
        stall in `target_state`, else None.
        """
        state = event.ha_state

        if state == self._opposite_state:
            if self._target_state == "closing":
                # Moving out — can't be stowed, regardless of prior inference.
                self._inferred_closed[key] = False
            self._stall_streak[key] = 0
            self._sample_count[key] = 0
            return None

        if state == self._target_state:
            count = self._sample_count.get(key, 0) + 1
            self._sample_count[key] = count
            if (
                count > self._warmup_samples
                and event.current_draw is not None
                and event.current_draw >= self._stall_current_a
            ):
                self._stall_streak[key] = self._stall_streak.get(key, 0) + 1
            else:
                self._stall_streak[key] = 0
            return None

        if state == "stopped":
            streak = self._stall_streak.get(key, 0)
            self._stall_streak[key] = 0
            self._sample_count[key] = 0
            if streak >= self._confirm_samples:
                if self._target_state == "closing":
                    self._inferred_closed[key] = True
                return streak
            return None

        # "unknown" status: leave prior inference untouched.
        return None

    def get(self, key: str) -> bool | None:
        return self._inferred_closed.get(key)

    def is_stalled(self, key: str) -> bool:
        """True as soon as the confirm-samples streak is reached, without
        waiting for a "stopped" status frame.

        For an *active* loop that's the one continuously resending the
        direction command: on real hardware (confirmed for retract), the
        device kept reporting motion for 6+ seconds of sustained
        stall-level current because it kept receiving fresh direction
        commands — the "stopped" transition update() waits for never
        arrived until the loop stopped sending. A loop driving its own
        motion should act on sustained high current directly rather than
        wait for a status transition it may itself be suppressing;
        get()/update() stay unchanged for passively watching motion this
        integration didn't initiate (wall switch), where waiting for
        "stopped" is correct since nothing here is continuously
        re-triggering it.
        """
        return self._stall_streak.get(key, 0) >= self._confirm_samples


class ExtendDipDetector:
    """Detects the brief current dip that precedes an awning reaching full
    extension — see the module docstring for the evidence this is built on.

    Per device key: arms once current has reached `arm_current_a` at least
    once during a continuous "opening" run (clearing the ~0.6A startup
    sample without needing a separate warm-up counter), then reports a dip
    the first time current drops below `dip_current_a` afterward. Any
    non-"opening" frame (stopped/closing/unknown) resets both the arm state
    and the dip report for that key, so each run starts clean.

    Single-shot per run by design: only the *first* qualifying dip matters
    (there's no reason to keep re-triggering after the loop has already
    reacted to it), and update() is expected to be polled the same way as
    CoverStallInference.is_stalled() — checked directly by a motion loop,
    not gated behind a "stopped" transition, for the same reason: a loop
    continuously resending Forward may suppress that transition entirely.
    """

    def __init__(
        self,
        arm_current_a: float = DEFAULT_EXTEND_DIP_ARM_CURRENT_A,
        dip_current_a: float = DEFAULT_EXTEND_DIP_CURRENT_A,
    ) -> None:
        self._arm_current_a = arm_current_a
        self._dip_current_a = dip_current_a
        self._armed: dict[str, bool] = {}
        self._dipped: dict[str, bool] = {}

    def update(self, key: str, event: CoverStatus) -> None:
        """Process one CoverStatus event for device `key`."""
        if event.ha_state != "opening":
            self._armed[key] = False
            self._dipped[key] = False
            return

        current = event.current_draw
        if current is None:
            return

        if not self._armed.get(key, False):
            if current >= self._arm_current_a:
                self._armed[key] = True
            return

        if current < self._dip_current_a:
            self._dipped[key] = True

    def is_dipped(self, key: str) -> bool:
        return self._dipped.get(key, False)
