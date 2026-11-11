"""The EcoFlow app's "Energy value" for a STREAM system: today and in total.

EcoFlow computes this amount in its cloud from the tariff set in the app; it
cannot be rebuilt from the power readings. The sensors only read it. The cloud
answers per day, so the total is the sum of every finished day, read once and
kept in Home Assistant, plus today.
"""

from __future__ import annotations

import asyncio
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN
from .ecoflow.app_api import AppApiClient, SummaryLoginError
from .ecoflow.app_summary import (
    ENERGY_VALUE_PAGE,
    day_is_empty,
    energy_value_total,
    month_totals,
    valid_saved_days,
)
from .ecoflow.charging_history import identity
from .ecoflow.const import device_log_tag

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=10)
LOGIN_BACKOFF = timedelta(hours=1)
# After a failed read, ask again after a minute this many times; the last
# amount stays on display meanwhile.
RETRY_INTERVAL = timedelta(minutes=1)
QUICK_RETRIES = 5
# A finished day is read again this long after midnight, so the cloud has
# closed it; until then the last amount seen that day stands in.
SETTLE_AFTER = timedelta(hours=1)
# History is read backwards until this many days in a row hold nothing.
EMPTY_DAYS_END_HISTORY = 14
MAX_HISTORY_DAYS = 730
HISTORY_PAUSE_S = 2
RECENT_DAYS_CHECKED = 7


class EnergyValueCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """One cloud read per system every ten minutes, plus a one-time history read."""

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
        self.store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}_energy_value_{entry.entry_id}_{identity(serial)}"
        )
        # Finished days, ISO date -> amount. ``provisional`` names the days
        # whose amount is the last one seen live and still has to be confirmed.
        self.days: dict[str, float] = {}
        self.provisional: set[str] = set()
        # Newest live amount, kept across restarts so a day that turns while
        # Home Assistant is off is still carried into the total.
        self.last_seen: dict[str, Any] | None = None
        self.history_complete = False
        self.restored = False
        self.failures = 0
        self._history_running = False

    async def async_restore(self) -> None:
        """Load the finished days; a damaged store starts empty and is re-read."""
        try:
            saved = await self.store.async_load()
        except (OSError, ValueError, HomeAssistantError):
            _LOGGER.warning(
                "Could not restore the energy value history (%s)",
                device_log_tag(self.serial),
            )
            return
        self.restored = True
        if saved is None or not valid_saved_days(saved):
            return
        self.days = dict(saved["days"])
        self.provisional = set(saved["provisional"])
        self.last_seen = saved["last_seen"]
        self.history_complete = saved["complete"]

    async def _async_save(self) -> None:
        if not self.restored:
            return
        await self.store.async_save(
            {
                # Copies: the write runs later, while reads keep adding days.
                "days": dict(self.days),
                "provisional": sorted(self.provisional),
                "last_seen": dict(self.last_seen) if self.last_seen else None,
                "complete": self.history_complete,
            }
        )

    @property
    def total(self) -> float | None:
        """Every finished day plus today, or None before the first read."""
        today = self.data.get(ENERGY_VALUE_PAGE)
        if today is None or self.day is None:
            return None
        return energy_value_total(self.days, self.day, today["value"])

    async def _async_read(self, day: str) -> dict[str, dict[str, Any]]:
        return await self.api.get_stream_day_summary(
            self.serial, day, self.hass.config.time_zone
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """A failed read keeps the last rows but marks the sensors unavailable."""
        now = dt_util.now()
        day = now.date().isoformat()
        try:
            rows = await self._async_read(day)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            self.failures += 1
            refused = isinstance(err, SummaryLoginError)
            if refused:
                self.update_interval = LOGIN_BACKOFF
            elif self.failures <= QUICK_RETRIES:
                self.update_interval = RETRY_INTERVAL
            else:
                self.update_interval = POLL_INTERVAL
            if self.failures == 1:
                _LOGGER.warning(
                    "Could not read the energy value (%s): %s",
                    device_log_tag(self.serial),
                    err,
                )
            # A short network hiccup keeps the last amount on display; only a
            # refusal or a run of failures makes the sensors unavailable.
            if refused or self.failures > QUICK_RETRIES:
                self.readable = False
            self.day = self.day or day
            return self.data
        if self.failures:
            _LOGGER.info(
                "Energy value readable again (%s)", device_log_tag(self.serial)
            )
        self.failures = 0
        self.update_interval = POLL_INTERVAL

        changed = False
        seen = self.last_seen
        if seen is not None and seen["day"] < day and seen["day"] not in self.days:
            # The day turned, possibly while Home Assistant was off: keep its
            # last amount until the cloud confirms it.
            self.days[seen["day"]] = seen["value"]
            self.provisional.add(seen["day"])
            changed = True
        current = rows.get(ENERGY_VALUE_PAGE)
        if current is not None:
            now_seen = {"day": day, "value": current["value"]}
            if now_seen != seen:
                self.last_seen = now_seen
                changed = True
        settled_before = (now - SETTLE_AFTER).date().isoformat()
        for pending in sorted(d for d in self.provisional if d < settled_before):
            try:
                closed = (await self._async_read(pending)).get(ENERGY_VALUE_PAGE)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                break
            if closed is not None:
                self.days[pending] = closed["value"]
            self.provisional.discard(pending)
            changed = True
        if self.history_complete:
            # A restart across midnight never saw the day turn: fill such gaps.
            for back in range(1, RECENT_DAYS_CHECKED + 1):
                missing = (now.date() - timedelta(days=back)).isoformat()
                if missing in self.days or missing >= settled_before:
                    continue
                try:
                    closed = (await self._async_read(missing)).get(ENERGY_VALUE_PAGE)
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    break
                self.days[missing] = closed["value"] if closed is not None else 0.0
                changed = True
        if changed:
            await self._async_save()

        if (
            not self.history_complete
            and not self._history_running
            and self.restored
            and self.config_entry is not None
        ):
            # Also the retry after a pass that a network error cut short.
            self.config_entry.async_create_background_task(
                self.hass, self.async_read_history(), "EcoFlow energy value history"
            )

        self.readable = True
        self.day = day
        return rows

    async def async_read_history(self) -> None:
        """Run one history pass unless one is running or nothing is left."""
        if self.history_complete or not self.restored or self._history_running:
            return
        self._history_running = True
        try:
            await self._async_history_pass()
        finally:
            self._history_running = False

    async def _async_history_pass(self) -> None:
        """Read every earlier day once, newest first; resumes where it stopped."""
        # Start at the newest day the cloud has closed; the days after it are
        # handled by the regular read.
        cursor = (dt_util.now() - SETTLE_AFTER).date() - timedelta(days=1)
        empty_run = 0
        unsaved = 0
        for _ in range(MAX_HISTORY_DAYS):
            day = cursor.isoformat()
            cursor -= timedelta(days=1)
            if day in self.days:
                empty_run = 0
                continue
            try:
                rows = await self._async_read(day)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                # A refused or failed read is not an empty day: stop without
                # marking the history complete, so the next pass asks again.
                await self._async_save()
                return
            if day_is_empty(rows):
                empty_run += 1
                if empty_run >= EMPTY_DAYS_END_HISTORY:
                    break
            else:
                empty_run = 0
                value = rows.get(ENERGY_VALUE_PAGE)
                if value is not None:
                    self.days[day] = value["value"]
                    unsaved += 1
            if unsaved >= 10:
                await self._async_save()
                unsaved = 0
                self.async_update_listeners()
            await asyncio.sleep(HISTORY_PAUSE_S)
        self.history_complete = True
        await self._async_save()
        self.async_update_listeners()
        _LOGGER.info(
            "Energy value history read: %d days (%s)",
            len(self.days),
            device_log_tag(self.serial),
        )


class _EnergyValueSensor(CoordinatorEntity[EnergyValueCoordinator], SensorEntity):
    """Shared shape of the two amounts."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: EnergyValueCoordinator, device_info: DeviceInfo, key: str
    ) -> None:
        """Use the currency Home Assistant is configured for."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial}_{key}"
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


class EnergyValueTodaySensor(_EnergyValueSensor):
    """Today's amount exactly as the app prints it; can be negative."""

    _attr_translation_key = "energy_value_today"

    def __init__(
        self, coordinator: EnergyValueCoordinator, device_info: DeviceInfo
    ) -> None:
        """Keep the unique ID of the first test build."""
        super().__init__(coordinator, device_info, "energy_value_today")

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


class EnergyValueTotalSensor(_EnergyValueSensor):
    """Sum of every day the cloud holds, including today."""

    _attr_translation_key = "energy_value_total"

    def __init__(
        self, coordinator: EnergyValueCoordinator, device_info: DeviceInfo
    ) -> None:
        """One total per system."""
        super().__init__(coordinator, device_info, "energy_value_total")

    @property
    def native_value(self) -> float | None:
        """Return the total energy value."""
        total = self.coordinator.total
        return round(total, 4) if total is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Say how much history the total covers."""
        coordinator = self.coordinator
        days = coordinator.days
        today = coordinator.data.get(ENERGY_VALUE_PAGE)
        return {
            "days_counted": len(days) + 1,
            "first_day": min(days) if days else coordinator.day,
            "history_complete": coordinator.history_complete,
            "months": month_totals(
                days, coordinator.day, today["value"] if today is not None else None
            ),
        }


async def async_setup_energy_value(
    hass: HomeAssistant,
    entry: ConfigEntry,
    serial: str,
    device_info: DeviceInfo,
    async_add_entities: AddEntitiesCallback,
    api: AppApiClient,
) -> EnergyValueCoordinator:
    """Create the sensors at once; the cloud reads run in the background."""
    coordinator = EnergyValueCoordinator(hass, entry, serial, api)
    await coordinator.async_restore()
    async_add_entities(
        [
            EnergyValueTodaySensor(coordinator, device_info),
            EnergyValueTotalSensor(coordinator, device_info),
        ]
    )
    entry.async_on_unload(coordinator.async_shutdown)

    entry.async_create_background_task(
        hass, coordinator.async_refresh(), "EcoFlow initial energy value"
    )
    return coordinator
