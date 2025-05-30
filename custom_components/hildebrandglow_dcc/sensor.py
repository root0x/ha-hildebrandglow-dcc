"""Platform for sensor integration."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
import logging
import itertools
import statistics
import requests

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from homeassistant.components import recorder
from homeassistant.components.recorder import statistics
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dtutil

from homeassistant_historical_sensor import (
    HistoricalSensor,
    HistoricalState,
    PollUpdateMixin,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(minutes=5)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Callable
) -> bool:
    """Set up the sensor platform."""
    entities: list = []
    meters: dict = {}

    # Get API object from the config flow
    glowmarkt = hass.data[DOMAIN][entry.entry_id]

    # Gather all virtual entities on the account
    virtual_entities: dict = {}
    try:
        virtual_entities = await hass.async_add_executor_job(
            glowmarkt.get_virtual_entities
        )
        _LOGGER.debug("Successful GET to %svirtualentity", glowmarkt.url)
    except requests.Timeout as ex:
        _LOGGER.error("Timeout: %s", ex)
    except requests.exceptions.ConnectionError as ex:
        _LOGGER.error("Cannot connect: %s", ex)
    # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
    except Exception as ex:  # pylint: disable=broad-except
        if "Request failed" in str(ex):
            _LOGGER.error(
                "Non-200 Status Code. The Glow API may be experiencing issues"
            )
        else:
            _LOGGER.exception("Unexpected exception: %s. Please open an issue", ex)

    for virtual_entity in virtual_entities:
        # Gather all resources for each virtual entity
        resources: dict = {}
        try:
            resources = await hass.async_add_executor_job(virtual_entity.get_resources)
            _LOGGER.debug(
                "Successful GET to %svirtualentity/%s/resources",
                glowmarkt.url,
                virtual_entity.id,
            )
        except requests.Timeout as ex:
            _LOGGER.error("Timeout: %s", ex)
        except requests.exceptions.ConnectionError as ex:
            _LOGGER.error("Cannot connect: %s", ex)
        # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
        except Exception as ex:  # pylint: disable=broad-except
            if "Request failed" in str(ex):
                _LOGGER.error(
                    "Non-200 Status Code. The Glow API may be experiencing issues"
                )
            else:
                _LOGGER.exception("Unexpected exception: %s. Please open an issue", ex)

        # Loop through all resources and create sensors
        for resource in resources:
            if resource.classifier in ["electricity.consumption", "gas.consumption"]:
                usage_sensor = Usage(hass, resource, virtual_entity)
                entities.append(usage_sensor)
                # Save the usage sensor as a meter so that the cost sensor can reference it
                meters[resource.classifier] = usage_sensor

                # Standing and Rate sensors are handled by the coordinator
                coordinator = TariffCoordinator(hass, resource)
                standing_sensor = Standing(coordinator, resource, virtual_entity)
                entities.append(standing_sensor)
                rate_sensor = Rate(coordinator, resource, virtual_entity)
                entities.append(rate_sensor)

        # Cost sensors must be created after usage sensors as they reference them as a meter
        for resource in resources:
            if resource.classifier == "gas.consumption.cost":
                cost_sensor = Cost(hass, resource, virtual_entity)
                cost_sensor.meter = meters["gas.consumption"]
                entities.append(cost_sensor)
            elif resource.classifier == "electricity.consumption.cost":
                cost_sensor = Cost(hass, resource, virtual_entity)
                cost_sensor.meter = meters["electricity.consumption"]
                entities.append(cost_sensor)

    # Get data for all entities on initial startup
    async_add_entities(entities, update_before_add=True)

    return True


def supply_type(resource) -> str:
    """Return supply type."""
    if "electricity.consumption" in resource.classifier:
        return "electricity"
    if "gas.consumption" in resource.classifier:
        return "gas"
    _LOGGER.error("Unknown classifier: %s. Please open an issue", resource.classifier)
    return "unknown"


def device_name(resource, virtual_entity) -> str:
    """Return device name. Includes name of virtual entity if it exists."""
    supply = supply_type(resource)
    # First letter of device name should be capitalised
    if virtual_entity.name is not None:
        name = f"{virtual_entity.name} smart {supply} meter"
    else:
        name = f"Smart {supply} meter"
    return name


async def should_update() -> bool:
    """Check if time is between 1-5 or 31-35 minutes past the hour."""
    return True


async def daily_data(hass: HomeAssistant, resource, t_from: datetime = None, precision: str = "PT30M") -> (float, str):
    """Get daily usage from the API."""
    # Always pull down the last 6 hours of readings
    now = datetime.now()
    # Round to the day to set time to 00:00:00
    t_from = await hass.async_add_executor_job(resource.round, datetime.now() - timedelta(hours=12), "PT1M")
    # Round to the minute subtract 1 hour to account for non complete hours
    t_to = await hass.async_add_executor_job(resource.round, now, "PT1M")#await hass.async_add_executor_job(resource.round, (now - timedelta(hours=1)).replace(minute= 59, second=59), "PT1M")
    # Tell Hildebrand to pull latest DCC data
    try:
        await hass.async_add_executor_job(resource.catchup)
        _LOGGER.debug(
            "Successful GET to https://api.glowmarkt.com/api/v0-1/resource/%s/catchup",
            resource.id,
        )
    except requests.Timeout as ex:
        _LOGGER.error("Timeout: %s", ex)
    except requests.exceptions.ConnectionError as ex:
        _LOGGER.error("Cannot connect: %s", ex)
    # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
    except Exception as ex:  # pylint: disable=broad-except
        if "Request failed" in str(ex):
            _LOGGER.warning(
                "Non-200 Status Code. The Glow API may be experiencing issues"
            )
        else:
            _LOGGER.exception("Unexpected exception: %s. Please open an issue", ex)

    try:
        readings = await hass.async_add_executor_job(
            resource.get_readings, t_from, t_to, precision, "sum", True
        )
        _LOGGER.debug("Successfully got daily usage for resource id %s", resource.id)
        return readings
    except requests.Timeout as ex:
        _LOGGER.error("Timeout: %s", ex)
    except requests.exceptions.ConnectionError as ex:
        _LOGGER.error("Cannot connect: %s", ex)
    # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
    except Exception as ex:  # pylint: disable=broad-except
        if "Request failed" in str(ex):
            _LOGGER.warning(
                "Non-200 Status Code. The Glow API may be experiencing issues"
            )
        else:
            _LOGGER.exception("Unexpected exception: %s. Please open an issue", ex)
    return None


async def tariff_data(hass: HomeAssistant, resource) -> float:
    """Get tariff data from the API."""
    try:
        tariff = await hass.async_add_executor_job(resource.get_tariff)
        _LOGGER.debug(
            "Successful GET to https://api.glowmarkt.com/api/v0-1/resource/%s/tariff",
            resource.id,
        )
        return tariff
    except UnboundLocalError:
        supply = supply_type(resource)
        _LOGGER.warning(
            "No tariff data found for %s meter (id: %s). If you don't see tariff data for this meter in the Bright app, please disable the associated rate and standing charge sensors",
            supply,
            resource.id,
        )
    except requests.Timeout as ex:
        _LOGGER.error("Timeout: %s", ex)
    except requests.exceptions.ConnectionError as ex:
        _LOGGER.error("Cannot connect: %s", ex)
    # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
    except Exception as ex:  # pylint: disable=broad-except
        if "Request failed" in str(ex):
            _LOGGER.warning(
                "Non-200 Status Code. The Glow API may be experiencing issues"
            )
        else:
            _LOGGER.exception("Unexpected exception: %s. Please open an issue", ex)
    return None

class HistoricalSensorMixin(PollUpdateMixin, HistoricalSensor, SensorEntity):
    @property
    def statistic_id(self) -> str:
        return self.entity_id
    
    def get_statistic_metadata(self) -> StatisticMetaData:
        meta = super().get_statistic_metadata()
        meta["has_sum"] = True
        meta["has_mean"] = True

        return meta

    async def async_calculate_statistic_data(
        self, hist_states: list[HistoricalState], *, latest: dict | None = None
    ) -> list[StatisticData]:
        #
        # Group historical states by hour
        # Calculate sum, mean, etc...
        #

        accumulated = latest["sum"] if latest else 0

        def hour_block_for_hist_state(hist_state: HistoricalState) -> datetime:
            # XX:00:00 states belongs to previous hour block
            return hist_state.dt.replace(minute=0, second=0, microsecond=0)

        ret = []
        for dt, collection_it in itertools.groupby(
            hist_states, key=hour_block_for_hist_state
        ):
            collection = list(collection_it)

            if len(collection) < 2:
                continue
            
            mean = statistics.mean([x.state for x in collection])
            partial_sum = sum([x.state for x in collection])
            accumulated = accumulated + partial_sum

            ret.append(
                StatisticData(
                    start=dt,
                    state=partial_sum,
                    mean=mean,
                    sum=accumulated,
                )
            )
        return ret

class Usage(PollUpdateMixin, HistoricalSensor, SensorEntity):
    """Sensor object for daily usage."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_has_entity_name = True
    _attr_name = "Usage (today)"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, hass: HomeAssistant, resource, virtual_entity) -> None:
        """Initialize the sensor."""
        self._attr_unique_id = resource.id
        self.UPDATE_INTERVAL = timedelta(minutes = 15)
        self.hass = hass
        self.initialised = False
        self.resource = resource
        self.virtual_entity = virtual_entity

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )

    @property
    def icon(self) -> str | None:
        """Icon to use in the frontend."""
        # Only the gas usage sensor needs an icon as the others inherit from their device class
        if self.resource.classifier == "gas.consumption":
            return "mdi:fire"

    async def async_update_historical(self) -> None:
        """Fetch new data for the sensor."""
        if not self.initialised:
            readings = await daily_data(self.hass, self.resource)
            hist_states = []
            for reading in readings:
                hist_states.append(HistoricalState(  # noqa: PERF401
                    state = reading[1].value,
                    # add 1 minute to date so it can correctly call into the hour group
                    dt = dtutil.as_local(reading[0] + timedelta(minutes=1))
                ))
            self._attr_historical_states = hist_states
            self.initialised = True
        else:
            if await should_update():
                # Only update the sensor if it's between 1-5 or 31-35minutes past the hour
                readings = await daily_data(self.hass, self.resource)
                hist_states = []
                for reading in readings:
                    hist_states.append(HistoricalState(  # noqa: PERF401
                        state = reading[1].value,
                        dt = dtutil.as_local(reading[0] + timedelta(minutes=1))
                    ))
                self._attr_historical_states = hist_states

    @property
    def statistic_id(self) -> str:
        return self.entity_id

    def get_statistic_metadata(self) -> StatisticMetaData:
        meta = super().get_statistic_metadata()
        meta["has_sum"] = True

        return meta

    async def async_calculate_statistic_data(
        self, hist_states: list[HistoricalState], *, latest: dict | None = None
    ) -> list[StatisticData]:
        #
        # Group historical states by hour
        # Calculate sum, mean, etc...
        #
        accumulated = latest["sum"] if latest else 0

        def hour_block_for_hist_state(hist_state: HistoricalState) -> datetime:
            return hist_state.dt.replace(minute=0, second=0, microsecond=0)

        ret = []
        for dt, collection_it in itertools.groupby(
            hist_states, key=hour_block_for_hist_state
        ):
            collection = list(collection_it)
            # For each hour we need two meansurements (HH:00, HH:30) before calculating statistics
            if len(collection) < 2:
                continue
            mean = statistics.mean([x.state for x in collection])
            partial_sum = sum([x.state for x in collection])
            accumulated = accumulated + partial_sum

            ret.append(
                StatisticData(
                    start=dt,
                    state=partial_sum,
                    sum=accumulated,
                )
            )
        return ret


class Cost(HistoricalSensorMixin):
    """Sensor usage for daily cost."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_has_entity_name = True
    _attr_name = "Cost (today)"
    _attr_native_unit_of_measurement = "GBP"

    def __init__(self, hass: HomeAssistant, resource, virtual_entity) -> None:
        """Initialize the sensor."""
        self._attr_unique_id = resource.id
        self.UPDATE_INTERVAL = timedelta(minutes = 15)
        self.hass = hass
        self.initialised = False
        self.meter = None
        self.resource = resource
        self.virtual_entity = virtual_entity

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            # Get the identifier from the meter so that the cost sensors have the same device
            identifiers={(DOMAIN, self.meter.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )

    async def async_update_historical(self) -> None:
        """Fetch new data for the sensor."""
        # Get data on initial startup
        if not self.initialised:
            readings = await daily_data(self.hass, self.resource)
            hist_states = []
            for reading in readings:
                hist_states.append(HistoricalState(  # noqa: PERF401
                    state = reading[1].value / 100,
                    dt = dtutil.as_local(reading[0] + timedelta(minutes=1)))
                )
            self._attr_historical_states = hist_states
            self.initialised = True
        elif await should_update():
            # Only update the sensor if it's between 1-5 or 31-35minutes past the hour
            readings = await daily_data(self.hass, self.resource)
            hist_states = []
            for reading in readings:
                hist_states.append(HistoricalState(  # noqa: PERF401
                    state = reading[1].value / 100,
                    dt = dtutil.as_local(reading[0] + timedelta(minutes=1))
                ))
            self._attr_historical_states = hist_states
        _LOGGER.debug(self._attr_historical_states)

class TariffCoordinator(DataUpdateCoordinator):
    """Data update coordinator for the tariff sensors."""

    def __init__(self, hass: HomeAssistant, resource) -> None:
        """Initialize tariff coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="tariff",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(minutes=15),
        )

        self.rate_initialised = False
        self.standing_initialised = False
        self.resource = resource

    async def _async_update_data(self):
        """Fetch data from tariff API endpoint."""
        # This needs 2 loops to ensure both the rate and the standing sensors get initial values
        if not self.standing_initialised:
            if not self.rate_initialised:
                self.rate_initialised = True
                return await tariff_data(self.hass, self.resource)
            self.standing_initialised = True
            return await tariff_data(self.hass, self.resource)
        # Only poll when updated data might be available
        if await should_update():
            tariff = await tariff_data(self.hass, self.resource)
            return tariff


class Standing(CoordinatorEntity, SensorEntity):
    """An entity using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available

    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_has_entity_name = True
    _attr_name = "Standing charge"
    _attr_native_unit_of_measurement = "GBP"
    _attr_entity_registry_enabled_default = (
        False  # Don't enable by default as less commonly used
    )

    def __init__(self, coordinator, resource, virtual_entity) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)

        self._attr_unique_id = resource.id + "-tariff"

        self.resource = resource
        self.virtual_entity = virtual_entity

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data:
            value = (
                float(self.coordinator.data.current_rates.standing_charge.value) / 100
            )
            self._attr_native_value = round(value, 4)
            self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )


class Rate(CoordinatorEntity, SensorEntity):
    """An entity using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available

    """

    _attr_device_class = None
    _attr_has_entity_name = True
    _attr_icon = (
        "mdi:cash-multiple"  # Need to provide an icon as doesn't have a device class
    )
    _attr_name = "Rate"
    _attr_native_unit_of_measurement = "GBP/kWh"
    _attr_entity_registry_enabled_default = (
        False  # Don't enable by default as less commonly used
    )

    def __init__(self, coordinator, resource, virtual_entity) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)

        self._attr_unique_id = resource.id + "-rate"

        self.resource = resource
        self.virtual_entity = virtual_entity

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data:
            value = float(self.coordinator.data.current_rates.rate.value) / 100
            self._attr_native_value = round(value, 4)
            self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )
