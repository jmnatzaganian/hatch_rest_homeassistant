"""Hatch Rest media player."""

import logging

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import PyHatchBabyRestSound
from .coordinator import HatchBabyRestEntity, HatchBabyRestUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hatch Rest media player."""
    coordinator = config_entry.runtime_data
    # only need to update_before_add on one entity -- switch is "master" entity
    async_add_entities([HatchBabyRestMediaPlayer(coordinator)], update_before_add=False)


class HatchBabyRestMediaPlayer(HatchBabyRestEntity, MediaPlayerEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Hatch Rest media player entity."""

    def __init__(self, coordinator: HatchBabyRestUpdateCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)

        self._previous_sound: PyHatchBabyRestSound | None = None

    @property
    def extra_state_attributes(self) -> dict | None:
        """Expose schedules and active favorite as state attributes."""
        device = self._hatch_rest_device
        schedules = []
        for slot, data in sorted(device.schedules.items()):
            sound = data.get("sound")
            schedules.append({
                "slot": slot,
                "name": data.get("name", f"Schedule {slot}"),
                "hour": data.get("hour"),
                "minute": data.get("minute"),
                "days": data.get("days"),
                "sound": sound.name if hasattr(sound, "name") else sound,
                "volume": data.get("volume"),
                "brightness": data.get("brightness"),
                "color": data.get("color"),
            })
        return {
            "schedules": schedules,
            "active_favorite": device.active_favorite,
        }

    @property
    def device_class(self) -> MediaPlayerDeviceClass | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the device class of the media player."""
        return MediaPlayerDeviceClass.SPEAKER

    @property
    def name(self) -> str | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the name of the entity."""
        if self._hatch_rest_device.name:
            return f"{self._hatch_rest_device.name.title()} Media Player"
        return None

    @property
    def source(self) -> str | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the current source of the media player."""
        sound = self.coordinator.data.get("sound")
        if sound is None or sound == PyHatchBabyRestSound.none:
            return None
        if hasattr(sound, "name"):
            return sound.name.capitalize()
        return f"Unknown ({sound})"

    @property
    def source_list(self) -> list[str] | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return a list of available sources."""
        favorites = [f"Favorite {i}" for i in range(1, 7)]
        sounds = [sound.name.capitalize() for sound in PyHatchBabyRestSound if sound.name != "none"]
        return favorites + sounds

    @property
    def state(self) -> MediaPlayerState | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the current state of the media player."""
        # if power is off, then it's off
        if self.coordinator.data.get("power") is False:
            return MediaPlayerState.OFF

        if self.coordinator.data.get("sound") == PyHatchBabyRestSound.none:
            return MediaPlayerState.PAUSED

        return MediaPlayerState.PLAYING

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return a set of supported features."""
        return (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.SELECT_SOURCE
        )

    @property
    def volume_level(self) -> float | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the volume level of the media player."""
        _LOGGER.debug(
            "media_player volume_level = %s", self.coordinator.data.get("volume")
        )
        volume = self.coordinator.data.get("volume")
        if volume:
            return float(volume / 255)
        return None

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the volume level of the media player."""
        _LOGGER.debug("media_player setting volume_level = %s", int(255 * volume))
        await self._hatch_rest_device.set_volume(int(255 * volume))

        # https://developers.home-assistant.io/docs/integration_fetching_data/
        # If this method is used on a coordinator that polls, it will reset the time until the next time it will poll for data.
        # each _send_command calls _refresh_data and updates API data states, so use that
        self.coordinator.async_set_updated_data(self.coordinator.get_current_data())

    async def async_select_source(self, source: str) -> None:
        """Select a source from the list of available sources."""
        _LOGGER.debug("media_player selecting source: %s", source)

        if source.startswith("Favorite "):
            try:
                index = int(source.split(" ")[1])
                await self._hatch_rest_device.select_favorite(index)
                await self.coordinator.async_refresh()
                return
            except (ValueError, IndexError):
                _LOGGER.error("Invalid favorite source: %s", source)
                return

        try:
            source_number = PyHatchBabyRestSound[source.lower()]
            self._previous_sound = PyHatchBabyRestSound(source_number)
            _LOGGER.debug(
                "media_player setting sound = %d (%s) ",
                source_number,
                PyHatchBabyRestSound(source_number).name,
            )
            await self._hatch_rest_device.set_sound(source_number)
        except KeyError:
            _LOGGER.error("Invalid source selected: %s", source)

        # https://developers.home-assistant.io/docs/integration_fetching_data/
        # If this method is used on a coordinator that polls, it will reset the time until the next time it will poll for data.
        # each _send_command calls _refresh_data and updates API data states, so use that
        self.coordinator.async_set_updated_data(self.coordinator.get_current_data())

    async def async_media_pause(self) -> None:
        """Pause the media player."""
        current_sound = self._hatch_rest_device.sound
        if hasattr(current_sound, "name"):
            self._previous_sound = current_sound
        else:
            _LOGGER.warning(
                "media_player pause: device reported unrecognized sound value "
                "%s; keeping previous resumable sound instead",
                current_sound,
            )
        _LOGGER.debug(
            "media_player setting source = %d (%s)",
            PyHatchBabyRestSound.none,
            PyHatchBabyRestSound.none.name,
        )
        await self._hatch_rest_device.set_sound(PyHatchBabyRestSound.none)

        # https://developers.home-assistant.io/docs/integration_fetching_data/
        # If this method is used on a coordinator that polls, it will reset the time until the next time it will poll for data.
        # each _send_command calls _refresh_data and updates API data states, so use that
        self.coordinator.async_set_updated_data(self.coordinator.get_current_data())

    async def async_media_play(self) -> None:
        """Play the media player."""
        if not self._hatch_rest_device.power:
            _LOGGER.debug("media_player _hatch_rest_device power not on -- turning on")
            await self._hatch_rest_device.turn_power_on()
        if previous_sound := self._previous_sound:
            try:
                sound_name = PyHatchBabyRestSound(previous_sound).name
            except ValueError:
                _LOGGER.error(
                    "media_player cannot resume unrecognized sound value %s; skipping",
                    previous_sound,
                )
            else:
                _LOGGER.debug(
                    "media_player setting source = %d (%s)",
                    previous_sound,
                    sound_name,
                )
                await self._hatch_rest_device.set_sound(previous_sound)

        # https://developers.home-assistant.io/docs/integration_fetching_data/
        # If this method is used on a coordinator that polls, it will reset the time until the next time it will poll for data.
        # each _send_command calls _refresh_data and updates API data states, so use that
        self.coordinator.async_set_updated_data(self.coordinator.get_current_data())
