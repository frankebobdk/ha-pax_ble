import logging

from collections import namedtuple
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.const import UnitOfVolumeFlowRate, UnitOfTemperature, UnitOfTime
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
)

from .const import DeviceModel, PARTS_PER_MILLION
from .entity import PaxCalimaEntity

_LOGGER = logging.getLogger(__name__)

PaxEntity = namedtuple(
    "PaxEntity", ["key", "entityName", "units", "deviceClass", "category", "icon"]
)
ENTITIES = [
    PaxEntity(
        "humidity", "Humidity", PERCENTAGE, SensorDeviceClass.HUMIDITY, None, None
    ),
    PaxEntity(
        "temperature",
        "Temperature",
        UnitOfTemperature.CELSIUS,
        SensorDeviceClass.TEMPERATURE,
        None,
        None,
    ),
    PaxEntity("light", "Light", LIGHT_LUX, SensorDeviceClass.ILLUMINANCE, None, None),
    PaxEntity("rpm", "RPM", REVOLUTIONS_PER_MINUTE, None, None, "mdi:engine"),
    PaxEntity(
        "flow",
        "Flow",
        UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
        None,
        None,
        "mdi:weather-windy",
    ),
    PaxEntity("state", "State", None, None, None, None),
    PaxEntity("mode", "Mode", None, None, EntityCategory.DIAGNOSTIC, None),
]
SVENSA_ENTITIES = [
    PaxEntity(
        "airquality",
        "Air Quality",
        PARTS_PER_MILLION,
        SensorDeviceClass.CO2,
        None,
        None,
    ),
    PaxEntity(
        "pauseminread",
        "Pause Remaining",
        UnitOfTime.MINUTES,
        SensorDeviceClass.DURATION,
        None,
        "mdi:pause",
    ),
]


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Setup sensors from a config entry created in the integrations UI."""
    # Create entities
    ha_entities = []

    for device_id, coordinator in config_entry.runtime_data.devices.items():
        _LOGGER.debug("Starting paxcalima sensors: %s", coordinator.devicename)

        # Create entities for this device
        for paxentity in ENTITIES:
            ha_entities.append(PaxCalimaSensorEntity(coordinator, paxentity))

        # Device specific entities
        match coordinator._model:
            case DeviceModel.SVENSA.value:
                for paxentity in SVENSA_ENTITIES:
                    ha_entities.append(PaxCalimaSensorEntity(coordinator, paxentity))

    # No update_before_add: the coordinators may not have connected yet at
    # setup, and a forced pre-add update would run the first BLE connection
    # inside platform setup - blocking startup. Entities populate when the
    # background initial refresh reaches their device.
    async_add_entities(ha_entities, False)


class PaxCalimaSensorEntity(PaxCalimaEntity, SensorEntity):
    """Representation of a Sensor."""

    def __init__(self, coordinator, paxentity):
        """Pass coordinator to PaxCalimaEntity."""
        super().__init__(coordinator, paxentity)

        """Sensor Entity properties"""
        self._attr_device_class = paxentity.deviceClass
        self._attr_native_unit_of_measurement = paxentity.units

    @property
    def native_value(self):
        """Return the value of the sensor."""
        return self.coordinator.get_data(self._key)
