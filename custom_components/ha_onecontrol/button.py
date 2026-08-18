"""Button platform for OneControl BLE integration.

MyRVLink gateways expose buttons for:
    - Clear In-Motion Lockout (sends 0x55 arm → 100ms → 0xAA clear)
    - Refresh Metadata (re-requests MyRVLink device metadata tables)

These are intentionally unavailable for IDS-CAN BLE gateways: metadata is
learned from DEVICE_ID broadcasts, and the 0x55/0xAA clear sequence is a
MyRVLink maintenance operation rather than a valid IDS-CAN command frame.

Reference: INTERNALS.md § In-Motion Lockout, Android requestLockoutClear()
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OneControlCoordinator
from .protocol.events import CoverStatus

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OneControl button entities from a config entry."""
    coordinator: OneControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    async_add_entities([
        OneControlClearLockoutButton(coordinator, address),
        OneControlRefreshMetadataButton(coordinator, address),
    ])

    # H-bridge (cover) test-command buttons — one set per discovered device,
    # same dynamic-discovery pattern as cover.py/sensor.py.
    discovered_hbridge_test: set[str] = set()

    @callback
    def _on_event(event: Any) -> None:
        if isinstance(event, CoverStatus):
            key = f"{event.table_id:02x}:{event.device_id:02x}"
            if key not in discovered_hbridge_test:
                discovered_hbridge_test.add(key)
                async_add_entities([
                    OneControlHBridgeTestStopButton(
                        coordinator, address, event.table_id, event.device_id
                    ),
                    OneControlHBridgeTestReverseButton(
                        coordinator, address, event.table_id, event.device_id
                    ),
                    OneControlHBridgeTestForwardButton(
                        coordinator, address, event.table_id, event.device_id
                    ),
                    OneControlHBridgeTestOpenAutoStopButton(
                        coordinator, address, event.table_id, event.device_id
                    ),
                ])

    coordinator.register_event_callback(_on_event)

    for key, cov in coordinator.covers.items():
        if key not in discovered_hbridge_test:
            discovered_hbridge_test.add(key)
            async_add_entities([
                OneControlHBridgeTestStopButton(
                    coordinator, address, cov.table_id, cov.device_id
                ),
                OneControlHBridgeTestReverseButton(
                    coordinator, address, cov.table_id, cov.device_id
                ),
                OneControlHBridgeTestForwardButton(
                    coordinator, address, cov.table_id, cov.device_id
                ),
                OneControlHBridgeTestOpenAutoStopButton(
                    coordinator, address, cov.table_id, cov.device_id
                ),
            ])


class OneControlClearLockoutButton(
    CoordinatorEntity[OneControlCoordinator], ButtonEntity
):
    """Button to clear the in-motion lockout on the gateway.

    Sends the arm (0x55) + clear (0xAA) sequence via CAN_WRITE or
    DATA_WRITE fallback.  Throttled to one press per 5 seconds.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Clear In-Motion Lockout"
    _attr_icon = "mdi:car-brake-hold"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_clear_lockout"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )

    @property
    def available(self) -> bool:
        """Available on MyRVLink gateways when connected and lockout state is known."""
        if self.coordinator.is_can_ble_gateway:
            return False
        return (
            self.coordinator.connected
            and self.coordinator.system_lockout_level is not None
        )

    async def async_press(self) -> None:
        """Send lockout clear sequence to gateway."""
        _LOGGER.info("Lockout clear button pressed")
        await self.coordinator.async_clear_lockout()


class OneControlRefreshMetadataButton(
    CoordinatorEntity[OneControlCoordinator], ButtonEntity
):
    """Button to re-request device metadata (friendly names) from the gateway.

    If some devices show as "Device 0B:06" instead of their friendly name,
    press this button to re-fetch metadata.  Matches the Android app's
    "Refresh Metadata" feature.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Refresh Metadata"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: OneControlCoordinator, address: str) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_refresh_metadata"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )

    @property
    def available(self) -> bool:
        """Available on MyRVLink gateways when connected and authenticated."""
        return (
            not self.coordinator.is_can_ble_gateway
            and self.coordinator.connected
            and self.coordinator.authenticated
        )

    async def async_press(self) -> None:
        """Re-request device metadata from the gateway."""
        _LOGGER.info("Refresh Metadata button pressed")
        await self.coordinator.async_refresh_metadata()


class OneControlHBridgeTestStopButton(
    CoordinatorEntity[OneControlCoordinator], ButtonEntity
):
    """DIAGNOSTIC — sends a bare Stop command to one H-bridge device (awning/slide).

    This is the first-ever command path for H-bridge devices in this
    integration. The payload shape is UNVERIFIED — see the note on
    protocol.commands.CommandBuilder.build_action_hbridge. This button
    exists purely to test whether sending it actually halts the motor;
    it is intentionally separate from the cover entity, which still has
    all control blocked. Disabled by default — enable deliberately before
    testing, don't leave it live on a dashboard by accident.

    Reference: safety note in cover.py — H-bridge motors have no limit
    switches, so verify this against a real motion before trusting it.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:flask-outline"

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
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_cover_test_stop_{device_id:02x}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Test Stop Command (Debug)"

    @property
    def available(self) -> bool:
        return self.coordinator.connected

    async def async_press(self) -> None:
        """Send the bare Stop command — see class docstring, unverified payload."""
        await self.coordinator.async_test_hbridge_stop(self._table_id, self._device_id)


class OneControlHBridgeTestReverseButton(
    CoordinatorEntity[OneControlCoordinator], ButtonEntity
):
    """DIAGNOSTIC — sends a bare Reverse (retract) command. STARTS REAL MOTION.

    Isolated test only, no auto-stop attached. Only press this while ready
    to immediately send Stop (the sibling test button, or the wall switch)
    — this is a bare motor command with none of the safety gating the
    vendor app applies (InTransitLockout, hazard flags — see
    protocol.commands.CommandBuilder module note). Disabled by default.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:flask-outline"

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
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_cover_test_reverse_{device_id:02x}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Test Reverse Command (Debug — Starts Motion)"

    @property
    def available(self) -> bool:
        return self.coordinator.connected

    async def async_press(self) -> None:
        """Send the bare Reverse command — see class docstring, starts real motion."""
        await self.coordinator.async_test_hbridge_reverse(self._table_id, self._device_id)


class OneControlHBridgeTestForwardButton(
    CoordinatorEntity[OneControlCoordinator], ButtonEntity
):
    """DIAGNOSTIC — starts CONTINUOUS Forward (extend) resend. STARTS REAL MOTION
    and, unlike the other test buttons, does NOT stop on its own beyond a
    32s watchdog of last resort.

    Purely for capturing the extend current profile — there is deliberately
    no current-based auto-stop for extend yet (that's an open question, unlike
    retract which has a verified stall signature). There is NO auto-reverse
    safety behavior on this hardware — driven past full extension, the motor
    keeps commanding Forward and the fabric wraps backward on the roller
    toward the RV sidewall, with nothing to stop it. Press the sibling Test
    Stop button (or the real cover's Stop) the moment it looks fully
    extended; do not wait to see what happens next. Disabled by default.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:flask-outline"

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
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_cover_test_forward_{device_id:02x}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Test Forward Continuous (Debug — Starts Motion, No Auto-Stop)"

    @property
    def available(self) -> bool:
        return self.coordinator.connected

    async def async_press(self) -> None:
        """Start continuous Forward resend — see class docstring. Use the
        sibling Test Stop button (or cover.stop_cover) to end it."""
        await self.coordinator.async_start_hbridge_test_forward(self._table_id, self._device_id)


class OneControlHBridgeTestOpenAutoStopButton(
    CoordinatorEntity[OneControlCoordinator], ButtonEntity
):
    """DIAGNOSTIC — manually triggers the same auto-extend used by the real
    cover's open_cover (coordinator.async_start_hbridge_open): resends
    Forward every ~500ms until a current-based full-extension detector fires
    (4.0A, single-sample confirm, 2-sample warm-up to clear the startup
    inrush spike — see protocol/cover_inference.py
    DEFAULT_EXTEND_STALL_CURRENT_A), with a 40s watchdog ceiling as backstop.
    STARTS REAL MOTION.

    A current-dip trigger was tried ahead of this ramp detector and reverted
    after a live false-early-stop (temp/logs-27, see coordinator.py) — the
    awning stopped ~2s short of full extension. Ramp-only; a small overshoot
    is the accepted tradeoff over stopping short.

    There is NO auto-reverse safety behavior on this hardware; if the
    detector fails to fire, nothing but the 40s watchdog and your own
    attention stands between continued motion and the RV sidewall. Watch
    closely and be ready to press the sibling Test Stop button regardless.
    Disabled by default — this exists for direct testing/observation of the
    auto-extend without going through the cover entity, same as the other
    test buttons coexist with the real close_cover.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:flask-outline"

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
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_cover_test_open_autostop_{device_id:02x}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"OneControl {address}",
            manufacturer="Lippert / LCI",
            model="BLE Gateway",
            connections={("bluetooth", address)},
        )

    @property
    def name(self) -> str:
        base = self.coordinator.device_name(self._table_id, self._device_id)
        return f"{base} Test Open Auto-Stop (Debug — Starts Motion)"

    @property
    def available(self) -> bool:
        return self.coordinator.connected

    async def async_press(self) -> None:
        """Start continuous Forward resend with the extend-impact auto-stop —
        see class docstring. Use the sibling Test Stop button (or
        cover.stop_cover) to end it early."""
        await self.coordinator.async_start_hbridge_open(
            self._table_id, self._device_id
        )
