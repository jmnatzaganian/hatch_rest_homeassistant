"""Hatch Rest coordinator."""

from datetime import timedelta
import logging
from time import monotonic

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import PyHatchBabyRestAsync
from .const import DOMAIN, PyHatchBabyRestSound

_LOGGER = logging.getLogger(__name__)

# After N consecutive failures, delay the next attempt by this many seconds.
# Progression: 1 min → 5 min → 15 min (capped). Resets on first success.
_BACKOFF_SCHEDULE = (60.0, 300.0, 900.0)


class HatchBabyRestUpdateCoordinator(DataUpdateCoordinator):
    """Hatch Rest data update coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        unique_id: str | None,
        hatch_rest_device: PyHatchBabyRestAsync,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # Sound machines don't change state on their own; optimistic updates
            # from commands keep entities current. Poll is a slow safety net only.
            update_interval=timedelta(minutes=10),
        )
        self.unique_id = unique_id
        self.hatch_rest_device = hatch_rest_device
        self._last_data: dict[
            str, int | tuple[int, int, int] | bool | PyHatchBabyRestSound | None
        ] = {}
        self._consecutive_failures: int = 0
        self._backoff_until: float = 0.0

    def get_current_data(
        self,
    ) -> dict[str, int | tuple[int, int, int] | bool | PyHatchBabyRestSound | None]:
        """Get the current state of the Hatch Rest device."""
        data: dict[
            str, int | tuple[int, int, int] | bool | PyHatchBabyRestSound | None
        ] = {
            "brightness": self.hatch_rest_device.brightness,
            "color": self.hatch_rest_device.color,
            "power": self.hatch_rest_device.power,
            "sound": self.hatch_rest_device.sound,
            "volume": self.hatch_rest_device.volume,
        }
        _LOGGER.debug("Data updated: %s", data)
        return data

    async def _async_update_data(
        self,
    ) -> dict[str, int | tuple[int, int, int] | bool | PyHatchBabyRestSound | None]:
        now = monotonic()
        if self._backoff_until > now:
            _LOGGER.debug(
                "Skipping poll: in backoff for %.0f more seconds",
                self._backoff_until - now,
            )
            return self._last_data if self._last_data else self.get_current_data()

        _LOGGER.debug("Starting coordinator async update")
        self._last_data = self.data if self.data else {}
        try:
            await self.hatch_rest_device.refresh_data()
        except Exception as e:
            self._consecutive_failures += 1
            backoff = _BACKOFF_SCHEDULE[
                min(self._consecutive_failures - 1, len(_BACKOFF_SCHEDULE) - 1)
            ]
            self._backoff_until = monotonic() + backoff
            _LOGGER.warning(
                "_async_update_data failed (failure #%d, backing off %.0fs): %r",
                self._consecutive_failures,
                backoff,
                e,
            )
            if self._last_data:
                return self._last_data
            raise UpdateFailed(f"Device update failed: {e}") from e
        else:
            if self._consecutive_failures:
                _LOGGER.info(
                    "Device responded after %d consecutive failure(s); resetting backoff",
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0
            self._backoff_until = 0.0
            return self.get_current_data()


class HatchBabyRestEntity(CoordinatorEntity[HatchBabyRestUpdateCoordinator]):
    """Hatch Rest entity."""

    def __init__(self, coordinator: HatchBabyRestUpdateCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._hatch_rest_device = coordinator.hatch_rest_device
        self._attr_unique_id = coordinator.unique_id

    @property
    def device_info(self) -> DeviceInfo:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return device specific attributes."""
        if not all((self._hatch_rest_device.address, self.unique_id)):
            raise ValueError("Missing bluetooth address for hatch rest device")

        assert self._hatch_rest_device.address
        assert self.unique_id

        return DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, self._hatch_rest_device.address)},
            identifiers={(DOMAIN, self.unique_id)},
            manufacturer="Hatch",
            model="Rest",
            name=self.device_name,
        )

    @property
    def device_name(self):
        """Return the name of the device."""
        return self._hatch_rest_device.name
