"""Hatch Rest switch."""

import logging

import voluptuous as vol

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import HatchBabyRestEntity, HatchBabyRestUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hatch Rest switch."""
    coordinator = config_entry.runtime_data
    # only need to update_before_add on one entity -- switch is "master" entity
    entities = [HatchBabyRestSwitch(coordinator)]
    
    # Add favorite toggle switches
    for i in range(1, 7):
        entities.append(HatchBabyRestFavoriteEnabledSwitch(coordinator, i))
    
    # Add schedule toggle switches
    for i in range(1, 11):
        entities.append(HatchBabyRestScheduleEnabledSwitch(coordinator, i))

    async_add_entities(entities, update_before_add=False)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "save_to_favorite",
        {vol.Required("index"): vol.All(cv.positive_int, vol.Range(min=1, max=6))},
        "async_save_to_favorite",
    )
    platform.async_register_entity_service(
        "select_favorite",
        {vol.Required("index"): vol.All(cv.positive_int, vol.Range(min=1, max=6))},
        "async_select_favorite",
    )
    platform.async_register_entity_service(
        "send_command",
        {
            vol.Required("command"): cv.string,
            vol.Optional("raw", default=False): cv.boolean,
        },
        "async_send_command",
    )


class HatchBabyRestSwitch(HatchBabyRestEntity, SwitchEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Hatch Rest switch entity."""

    @property
    def is_on(self) -> bool | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return whether the switch is on or not."""
        _LOGGER.debug("switch is_on = %s", self.coordinator.data.get("power"))
        return self.coordinator.data.get("power")

    @property
    def name(self) -> str | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the name of the entity."""
        if self._hatch_rest_device.name:
            return f"{self._hatch_rest_device.name.title()} Switch"
        return None

    async def async_turn_on(self, **_):
        """Turn on the Hatch Rest device."""
        if not self.is_on:
            _LOGGER.debug("switch setting on")
            await self._hatch_rest_device.turn_power_on()

            # https://developers.home-assistant.io/docs/integration_fetching_data/
            # If this method is used on a coordinator that polls, it will reset the time until the next time it will poll for data.
            # each _send_command calls _refresh_data and updates API data states, so use that
            self.coordinator.async_set_updated_data(self.coordinator.get_current_data())

    async def async_turn_off(self, **_):
        """Turn off the Hatch Rest device."""
        if self.is_on:
            _LOGGER.debug("switch setting off")
            await self._hatch_rest_device.turn_power_off()

            # https://developers.home-assistant.io/docs/integration_fetching_data/
            # If this method is used on a coordinator that polls, it will reset the time until the next time it will poll for data.
            # each _send_command calls _refresh_data and updates API data states, so use that
            self.coordinator.async_set_updated_data(self.coordinator.get_current_data())

    async def async_save_to_favorite(self, index: int) -> None:
        """Save current device state to a favorite slot."""
        _LOGGER.debug("Service: save_to_favorite index=%d", index)
        await self._hatch_rest_device.save_to_favorite(index)
        self.coordinator.async_set_updated_data(self.coordinator.get_current_data())

    async def async_select_favorite(self, index: int) -> None:
        """Select a favorite slot by index."""
        _LOGGER.debug("Service: select_favorite index=%d", index)
        await self._hatch_rest_device.select_favorite(index)
        await self.coordinator.async_refresh()

    async def async_send_command(self, command: str, raw: bool = False) -> None:
        """Send a command to the device."""
        _LOGGER.debug("Service: send_command '%s' raw=%s", command, raw)
        await self._hatch_rest_device._send_command(command, raw=raw)


class HatchBabyRestFavoriteEnabledSwitch(HatchBabyRestEntity, SwitchEntity):
    """Hatch Rest favorite enabled switch."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HatchBabyRestUpdateCoordinator, index: int) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._index = index

    @property
    def name(self) -> str | None:
        """Return the name of the entity."""
        fav_info = self._hatch_rest_device.favorites.get(self._index, {})
        base_name = fav_info.get("name", f"Favorite {self._index}")
        if self._hatch_rest_device.name:
            return f"{self._hatch_rest_device.name.title()} {base_name} Enabled"
        return f"{base_name} Enabled"

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID."""
        return f"{self._attr_unique_id}_favorite_{self._index}_enabled"

    @property
    def is_on(self) -> bool:
        """Return true if favorite is enabled."""
        fav_info = self._hatch_rest_device.favorites.get(self._index, {})
        return fav_info.get("enabled", False)

    async def async_turn_on(self, **kwargs) -> None:
        """Enable the favorite."""
        await self._hatch_rest_device.toggle_favorite(self._index, True)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable the favorite."""
        await self._hatch_rest_device.toggle_favorite(self._index, False)
        await self.coordinator.async_refresh()


class HatchBabyRestScheduleEnabledSwitch(HatchBabyRestEntity, SwitchEntity):
    """Hatch Rest schedule enabled switch."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HatchBabyRestUpdateCoordinator, index: int) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._index = index

    @property
    def name(self) -> str | None:
        """Return the name of the entity."""
        sched_info = self._hatch_rest_device.schedules.get(self._index, {})
        base_name = sched_info.get("name", f"Schedule {self._index}")
        if self._hatch_rest_device.name:
            return f"{self._hatch_rest_device.name.title()} {base_name} Enabled"
        return f"{base_name} Enabled"

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID."""
        return f"{self._attr_unique_id}_schedule_{self._index}_enabled"

    @property
    def is_on(self) -> bool:
        """Return true if schedule is enabled."""
        sched_info = self._hatch_rest_device.schedules.get(self._index, {})
        return sched_info.get("enabled", False)

    async def async_turn_on(self, **kwargs) -> None:
        """Enable the schedule."""
        await self._hatch_rest_device.toggle_schedule(self._index, True)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable the schedule."""
        await self._hatch_rest_device.toggle_schedule(self._index, False)
        await self.coordinator.async_refresh()
