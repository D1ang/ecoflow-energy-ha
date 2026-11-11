"""The EcoFlow app's "Energy value" of today for a STREAM system.

EcoFlow computes this amount in its cloud from the tariff set in the app; it
cannot be rebuilt from the power readings. The sensor only reads it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp
import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .ecoflow.app_api import AppApiClient, SummaryLoginError
from .ecoflow.app_summary import ENERGY_VALUE_PAGE
from .ecoflow.const import device_log_tag

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=10)
LOGIN_BACKOFF = timedelta(hours=1)


class EnergyValueCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """One cloud read per system every ten minutes."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, serial: str, api: AppApiClient
    ) -> None:
        """Poll slowly: the app itself only asks when the page is opened."""
        super().__init__(
            hass,
            _LOGGER,
            # HA prints this name in "Error fetching ... data" lines: tag, not serial.
            name=f"EcoFlow energy value ({device_log_tag(serial)})",
            config_entry=entry,
            update_interval=POLL_INTERVAL,
        )
        self.serial = serial
        self.api = api
        self.data = {}
        self.day: str | None = None
        self.readable = False

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """A failed read keeps the last rows but marks the sensor unavailable."""
        day = dt_util.now().date().isoformat()
        try:
            rows = await self.api.get_stream_day_summary(
                self.serial, day, self.hass.config.time_zone
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            self.update_interval = (
                LOGIN_BACKOFF if isinstance(err, SummaryLoginError) else POLL_INTERVAL
            )
            if self.readable or self.day is None:
                _LOGGER.warning(
                    "Could not read the energy value (%s): %s",
                    device_log_tag(self.serial),
                    err,
                )
            self.readable = False
            self.day = self.day or day
            return self.data
        if not self.readable and self.day is not None:
            _LOGGER.info(
                "Energy value readable again (%s)", device_log_tag(self.serial)
            )
        self.update_interval = POLL_INTERVAL
        self.readable = True
        self.day = day
        return rows


class EnergyValueTodaySensor(CoordinatorEntity[EnergyValueCoordinator], SensorEntity):
    """Today's amount exactly as the app prints it; can be negative."""

    _attr_has_entity_name = True
    _attr_translation_key = "energy_value_today"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: EnergyValueCoordinator, device_info: DeviceInfo
    ) -> None:
        """Use the currency Home Assistant is configured for."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial}_energy_value_today"
        self._attr_device_info = device_info
        self._attr_native_unit_of_measurement = coordinator.hass.config.currency

    @property
    def available(self) -> bool:
        """A failed cloud read must not present an old amount as current."""
        return (
            super().available
            and self.coordinator.readable
            and ENERGY_VALUE_PAGE in self.coordinator.data
        )

    @property
    def native_value(self) -> float | None:
        """Return today's energy value."""
        row = self.coordinator.data.get(ENERGY_VALUE_PAGE)
        return row["value"] if row is not None else None

    @property
    def last_reset(self) -> datetime | None:
        """The amount starts again at local midnight."""
        if self.coordinator.day is None:
            return None
        return dt_util.start_of_local_day(datetime.fromisoformat(self.coordinator.day))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Every row of the app's day summary, to compare with the app."""
        return {
            page.lower(): f"{row['value']:g} {row['unit']}".strip()
            for page, row in self.coordinator.data.items()
        }


async def async_setup_energy_value(
    hass: HomeAssistant,
    entry: ConfigEntry,
    serial: str,
    device_info: DeviceInfo,
    async_add_entities: AddEntitiesCallback,
    api: AppApiClient,
) -> EnergyValueCoordinator:
    """Create the sensor at once; the first read runs in the background."""
    coordinator = EnergyValueCoordinator(hass, entry, serial, api)
    async_add_entities([EnergyValueTodaySensor(coordinator, device_info)])
    entry.async_on_unload(coordinator.async_shutdown)
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), "EcoFlow initial energy value"
    )
    return coordinator
