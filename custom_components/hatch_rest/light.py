"""Hatch Rest light."""

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.components.light.const import ColorMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import HatchBabyRestEntity
from .const import COLOR_GRADIENT

_LOGGER = logging.getLogger(__name__)

EFFECT_RAINBOW = "Rainbow"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hatch Rest light."""
    coordinator = config_entry.runtime_data
    # only need to update_before_add on one entity -- switch is "master" entity
    async_add_entities([HatchBabyRestLight(coordinator)], update_before_add=False)


class HatchBabyRestLight(HatchBabyRestEntity, LightEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Hatch Rest light entity."""

    _attr_effect_list = [EFFECT_RAINBOW]
    _attr_supported_features = LightEntityFeature.EFFECT

    @property
    def brightness(self) -> int | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the brightness of the light."""
        _LOGGER.debug("light brightness = %s", self.coordinator.data.get("brightness"))
        return self.coordinator.data.get("brightness")

    @property
    def color_mode(self) -> ColorMode:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return color mode."""
        return ColorMode.RGB

    @property
    def is_on(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return if the light is on."""
        # if power is off, then it's off
        if self.coordinator.data.get("power") is False:
            return False
        brightness = self.coordinator.data.get("brightness")
        if brightness:
            _LOGGER.debug("light is_on = %s", brightness > 0)
            # if brightness is greater than 0, then it's on
            return brightness > 0
        return False

    @property
    def name(self) -> str | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the name of the entity."""
        if self._hatch_rest_device.name:
            return f"{self._hatch_rest_device.name.title()} Light"
        return None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the RGB color of the light."""
        _LOGGER.debug("light rgb_color = %s", self.coordinator.data.get("color"))
        return self.coordinator.data.get("color")

    @property
    def supported_color_modes(self) -> set[ColorMode]:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return supported color modes."""
        return {ColorMode.RGB}

    @property
    def effect(self) -> str | None:
        """Return the current effect."""
        if self.coordinator.data.get("color") == COLOR_GRADIENT:
            return EFFECT_RAINBOW
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set the light on."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        rgb = kwargs.get(ATTR_RGB_COLOR)
        effect = kwargs.get(ATTR_EFFECT)

        # If the rainbow effect is requested, override the color
        if effect == EFFECT_RAINBOW:
            rgb = COLOR_GRADIENT

        # If the whole device is powered off, we must turn it on to see the light
        if not self._hatch_rest_device.power:
            _LOGGER.debug("light turning device power on")
            await self._hatch_rest_device.turn_power_on()

        # If we have specific attributes (color/brightness), apply them
        if brightness is not None or rgb is not None:
            _LOGGER.debug("light setting state: brightness=%s, rgb=%s", brightness, rgb)
            await self._hatch_rest_device.set_light_state(brightness=brightness, color=rgb)
        
        # If we are just toggling "On" and the current brightness is 0, we need to bring it up
        elif self._hatch_rest_device.brightness == 0:
            _LOGGER.debug("light was at 0 brightness, restoring to full")
            await self._hatch_rest_device.set_brightness(255)

        self.coordinator.async_set_updated_data(self.coordinator.get_current_data())

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Set the light off."""
        _LOGGER.debug("light dimming to 0 (off)")
        await self._hatch_rest_device.set_brightness(0)

        self.coordinator.async_set_updated_data(self.coordinator.get_current_data())
