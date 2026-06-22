"""Tests for Hatch Rest integration setup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.hatch_rest import (
    PLATFORMS,
    async_setup_entry,
    async_unload_entry,
    options_update_listener,
)
from custom_components.hatch_rest.const import PyHatchBabyRestSound


class TestAsyncSetupEntry:
    """Tests for async_setup_entry."""

    @pytest.fixture
    def mock_entry(self) -> MagicMock:
        """Create mock config entry."""
        entry = MagicMock(spec=ConfigEntry)
        entry.entry_id = "test_entry"
        entry.unique_id = "aabbccddeeff"
        entry.data = {CONF_ADDRESS: "AA:BB:CC:DD:EE:FF"}
        entry.options = {}
        entry.runtime_data = None
        return entry

    @pytest.mark.asyncio
    async def test_setup_entry_success(
        self, hass: HomeAssistant, mock_entry: MagicMock
    ):
        """Test successful setup entry."""
        mock_ble_device = MagicMock()
        mock_ble_device.address = "AA:BB:CC:DD:EE:FF"
        mock_ble_device.name = "Hatch Rest"

        mock_api = MagicMock()
        mock_api.device = mock_ble_device
        mock_api.address = mock_ble_device.address
        mock_api.name = "Hatch Rest"
        mock_api.brightness = 100
        mock_api.color = (255, 255, 255)
        mock_api.power = True
        mock_api.sound = PyHatchBabyRestSound.none
        mock_api._timer_expires_at = None
        mock_api.volume = 50
        mock_api.refresh_data = AsyncMock()
        mock_api.select_favorite = AsyncMock()

        with patch(
            "custom_components.hatch_rest.bluetooth.async_ble_device_from_address",
            return_value=mock_ble_device,
        ):
            with patch(
                "custom_components.hatch_rest.PyHatchBabyRestAsync",
                return_value=mock_api,
            ):
                with patch(
                    "custom_components.hatch_rest.coordinator.bluetooth.async_register_callback",
                    return_value=MagicMock(),
                ):
                    with patch(
                        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
                        new_callable=AsyncMock,
                    ) as mock_forward:
                        result = await async_setup_entry(hass, mock_entry)

        assert result is True
        assert mock_entry.runtime_data is not None
        mock_forward.assert_called_once()

    @pytest.mark.asyncio
    async def test_setup_entry_device_not_found(
        self, hass: HomeAssistant, mock_entry: MagicMock
    ):
        """Test setup entry fails when device not found."""
        with patch(
            "custom_components.hatch_rest.bluetooth.async_ble_device_from_address",
            return_value=None,
        ):
            with pytest.raises(ConfigEntryNotReady, match="Could not find"):
                await async_setup_entry(hass, mock_entry)

    @pytest.mark.asyncio
    async def test_setup_entry_succeeds_when_device_unreachable(
        self, hass: HomeAssistant, mock_entry: MagicMock
    ):
        """Setup must NOT block on / fail from an unreachable device.

        The startup fix replaced async_config_entry_first_refresh() (which awaited a
        full BLE round-trip and raised ConfigEntryNotReady on failure) with
        async_set_updated_data() seeded from all-None state. Entities come up
        ``unknown`` and populate later via advertisements/poll, so setup succeeds
        even when the device is unreachable and a refresh would fail.
        """
        mock_ble_device = MagicMock()
        mock_ble_device.address = "AA:BB:CC:DD:EE:FF"
        mock_ble_device.name = "Hatch Rest"

        mock_api = MagicMock()
        mock_api.device = mock_ble_device
        mock_api.address = mock_ble_device.address
        mock_api.name = "Hatch Rest"
        mock_api.brightness = None
        mock_api.color = None
        mock_api.power = None
        mock_api.sound = None
        mock_api.volume = None
        mock_api.timer_total = None
        mock_api.timer_remaining = None
        mock_api._timer_expires_at = None
        # Device is unreachable: any refresh would raise. Setup must not call it.
        mock_api.refresh_data = AsyncMock(side_effect=Exception("Connection failed"))

        with patch(
            "custom_components.hatch_rest.bluetooth.async_ble_device_from_address",
            return_value=mock_ble_device,
        ):
            with patch(
                "custom_components.hatch_rest.PyHatchBabyRestAsync",
                return_value=mock_api,
            ):
                with patch(
                    "custom_components.hatch_rest.coordinator.bluetooth.async_register_callback",
                    return_value=MagicMock(),
                ):
                    with patch(
                        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
                        new_callable=AsyncMock,
                    ) as mock_forward:
                        result = await async_setup_entry(hass, mock_entry)

        assert result is True
        assert mock_entry.runtime_data is not None
        mock_forward.assert_called_once()
        # Setup must not have awaited a blocking refresh against the dead device.
        mock_api.refresh_data.assert_not_called()


class TestAsyncUnloadEntry:
    """Tests for async_unload_entry."""

    @pytest.mark.asyncio
    async def test_unload_entry(self, hass: HomeAssistant):
        """Test unloading entry."""
        mock_entry = MagicMock(spec=ConfigEntry)
        mock_entry.entry_id = "test_entry"

        with patch(
            "homeassistant.config_entries.ConfigEntries.async_unload_platforms",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_unload:
            result = await async_unload_entry(hass, mock_entry)

        assert result is True
        mock_unload.assert_called_once_with(mock_entry, PLATFORMS)


class TestOptionsUpdateListener:
    """Tests for options_update_listener."""

    @pytest.mark.asyncio
    async def test_options_update_reloads_entry(self, hass: HomeAssistant):
        """Test options update triggers reload."""
        mock_entry = MagicMock(spec=ConfigEntry)
        mock_entry.entry_id = "test_entry"

        with patch(
            "homeassistant.config_entries.ConfigEntries.async_reload",
            new_callable=AsyncMock,
        ) as mock_reload:
            await options_update_listener(hass, mock_entry)

        mock_reload.assert_called_once_with("test_entry")
