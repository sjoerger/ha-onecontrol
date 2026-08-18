"""Cover platform for OneControl BLE integration.

Open, Close, and Stop are all enabled for the H-bridge cover (awning):

  - async_close_cover starts an auto-retract loop
    (coordinator.async_start_hbridge_close) that resends the Reverse
    command every ~500ms — matching the vendor app's own resend cadence,
    see coordinator._hbridge_close_loop — until the passive current-based
    stall detector confirms closed, a hard runtime watchdog fires, or
    async_stop_cover cancels it.
  - async_open_cover starts an auto-extend loop
    (coordinator.async_start_hbridge_open / _hbridge_open_loop), same
    resend cadence, until the passive current-based impact detector
    confirms full extension, a hard runtime watchdog fires, or
    async_stop_cover cancels it. Promoted after 6 hardware trials of
    tuning (see protocol/cover_inference.py DEFAULT_EXTEND_STALL_CURRENT_A
    for the history) — the detector produces a small, user-confirmed-
    acceptable overshoot with no false triggers on the last two trials.

There is NO auto-reverse safety behavior on this hardware for extend — if
the impact detector and the watchdog both fail, nothing stops the fabric
wrapping backward toward the RV sidewall. Per the original safety
rationale this integration shipped with:
  "RV awnings and slides have no automatic safety mechanisms. The 19A/39A
   H-bridge motors could cause damage or injury without manual supervision."
Both directions are only enabled because each now has a verified, repeatable
stop condition (retract stall / extend impact current), not because that
underlying hardware risk has changed.

Reference: INTERNALS.md § Cover / Slide / Awning
"""

from __future__ import annotations

from typing import Any
from collections.abc import Callable

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OneControlCoordinator
from .protocol.events import CoverStatus


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OneControl cover entities from a config entry."""
    coordinator: OneControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    discovered: set[str] = set()

    @callback
    def _on_event(event: Any) -> None:
        if isinstance(event, CoverStatus):
            key = f"{event.table_id:02x}:{event.device_id:02x}"
            if key not in discovered:
                discovered.add(key)
                async_add_entities(
                    [OneControlCover(coordinator, address, event.table_id, event.device_id)]
                )

    coordinator.register_event_callback(_on_event)

    for key, cov in coordinator.covers.items():
        if key not in discovered:
            discovered.add(key)
            async_add_entities(
                [OneControlCover(coordinator, address, cov.table_id, cov.device_id)]
            )


class OneControlCover(CoordinatorEntity[OneControlCoordinator], CoverEntity):
    """Cover entity — Open/Close/Stop all enabled. See module docstring.

    Shows opening / closing / stopped state from the H-Bridge status event.
    Position is exposed when available (0xFF means unknown).
    """

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.AWNING
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )
    # Position is never available on this hardware (always 0xFF), and
    # is_closed is a best-effort inference that only ever reads True after a
    # confirmed retract stall — it goes straight to False the instant any
    # opening motion is observed, with no concept of "partially extended".
    # Without assumed_state, the stock HA cover card treats is_closed=False
    # as "fully open" and greys out the Open button after any partial
    # extend, even though the awning could be anywhere along its travel.
    # assumed_state tells the frontend not to gate button availability on
    # inferred state — always show Open/Close/Stop regardless.
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: OneControlCoordinator,
        address: str,
        table_id: int,
        device_id: int,
    ) -> None:
        super().__init__(coordinator)
        self._table_id = table_id
        self._device_id = device_id
        self._key = f"{table_id:02x}:{device_id:02x}"
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_cover_{device_id:02x}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )
        self._unsub: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub = self.coordinator.register_event_callback(self._on_event)

    @property
    def name(self) -> str:
        return self.coordinator.device_name(self._table_id, self._device_id)

    @property
    def available(self) -> bool:
        return self.coordinator.data_healthy and self._key in self.coordinator.covers

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is closed/stowed, False if open, None if unknown.

        Position is not live on this hardware (always 0xFF), so this can't be
        derived from position. Instead it's inferred passively from a motor
        current-draw signature: True only after a closing motion ends in a
        confirmed stall (the fully-retracted mechanical stop draws ~15-17A vs.
        ~3-8A while running); False as soon as any opening motion is observed;
        None when genuinely unknown (e.g. right after startup/reconnect, before
        either has been observed). See coordinator._update_cover_stall_inference.
        This is a best guess from a single capture's thresholds, not a verified
        endstop sensor — no minimum close duration is enforced, so a mid-travel
        obstruction producing the same current spike would also read as closed.
        """
        cov = self.coordinator.covers.get(self._key)
        if not cov:
            return None
        if cov.ha_state in ("opening", "closing"):
            return False
        return self.coordinator.cover_inferred_closed(self._table_id, self._device_id)

    @property
    def is_opening(self) -> bool:
        cov = self.coordinator.covers.get(self._key)
        return cov.ha_state == "opening" if cov else False

    @property
    def is_closing(self) -> bool:
        cov = self.coordinator.covers.get(self._key)
        return cov.ha_state == "closing" if cov else False

    @property
    def current_cover_position(self) -> int | None:
        """Position 0-100, None if unknown."""
        cov = self.coordinator.covers.get(self._key)
        if not cov or cov.position == 0xFF:
            return None
        return cov.position

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cov = self.coordinator.covers.get(self._key)
        if not cov:
            return {}
        return {
            "raw_status": f"0x{cov.status:02X}",
            "motor_current_a": cov.current_draw,
            "safety_note": (
                "Extend (Open) auto-stops on a current-impact signature with no "
                "hardware backstop (no auto-reverse). Retract (Close) auto-stops on "
                "a verified current-stall signature."
            ),
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Start auto-extend — see coordinator.async_start_hbridge_open.

        No hardware auto-reverse backstop exists for extend (see module
        docstring) — the impact current detector and the runtime watchdog
        are the only things standing between this and the fabric wrapping
        backward toward the RV sidewall.
        """
        await self.coordinator.async_start_hbridge_open(self._table_id, self._device_id)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Start auto-retract — see coordinator.async_start_hbridge_close."""
        await self.coordinator.async_start_hbridge_close(self._table_id, self._device_id)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Cancel any active auto-extend/auto-retract, or send a stop burst if
        none is running (e.g. to interrupt wall-switch-driven motion)."""
        await self.coordinator.async_stop_hbridge_motion(self._table_id, self._device_id)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub is not None:
            self._unsub()

    @callback
    def _on_event(self, event: Any) -> None:
        if (
            isinstance(event, CoverStatus)
            and event.table_id == self._table_id
            and event.device_id == self._device_id
        ):
            self.async_write_ha_state()
