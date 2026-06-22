"""Hatch Rest integration."""

from datetime import timedelta
import logging

from homeassistant import config_entries, core
from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.exceptions import ConfigEntryNotReady

from .api import PyHatchBabyRestAsync
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import HatchBabyRestUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.LIGHT,
    Platform.MEDIA_PLAYER,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
]


# async_setup_entry handles the setup of individual configuration
# entries created by users via the UI (i.e., Config Entry)
async def async_setup_entry(
    hass: core.HomeAssistant, entry: config_entries.ConfigEntry
) -> bool:
    """Set up the Hatch Rest component."""

    address = entry.data[CONF_ADDRESS]
    scan_interval_min = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    scan_interval = timedelta(minutes=scan_interval_min)

    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper())
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Hatch Rest device with address {address}"
        )
    hatch_rest_device = PyHatchBabyRestAsync(ble_device)
    hatch_rest_device.full_refresh_interval = (
        scan_interval_min * 60
    )  # convert to seconds
    coordinator = HatchBabyRestUpdateCoordinator(
        hass,
        entry.unique_id,
        hatch_rest_device,
        update_interval=scan_interval,
    )
    entry.runtime_data = coordinator

    # Register a callback to remove the coordinator listener on unload
    entry.async_on_unload(
        lambda: hatch_rest_device.remove_callback(coordinator._handle_api_update)
    )
    entry.async_on_unload(coordinator._cancel_bluetooth_advertisements)
    entry.async_on_unload(entry.add_update_listener(options_update_listener))

    # Set initial (empty) data immediately so entities can be created without blocking
    # on a BLE connect. Real state arrives via advertisements and the first poll.
    # Both async_config_entry_first_refresh and async_refresh await a full BLE round-trip
    # which can take 80-120s when a device is unreachable — unacceptable for HA startup.
    coordinator.async_set_updated_data(coordinator.get_current_data())
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(
    hass: core.HomeAssistant, entry: config_entries.ConfigEntry
) -> bool:
    """Unload Hatch Rest config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def options_update_listener(
    hass: core.HomeAssistant, config_entry: config_entries.ConfigEntry
):
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)
