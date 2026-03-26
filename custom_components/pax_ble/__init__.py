"""Support for Pax fans."""

import asyncio
import logging

from functools import partial
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers import device_registry as dr
from homeassistant.const import CONF_DEVICES
from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_NAME,
    CONF_MAC,
)
from .data import PaxBleData
from .helpers import getCoordinator

_LOGGER = logging.getLogger(__name__)

type PaxBleConfigEntry = ConfigEntry[PaxBleData]


async def async_setup_entry(hass: HomeAssistant, entry: PaxBleConfigEntry) -> bool:
    """Set up Pax BLE from a config entry."""
    _LOGGER.debug("Setting up configuration for Pax BLE!")

    runtime_data = PaxBleData()

    # Create one coordinator for each device
    first_iteration = True
    for device_id in entry.data[CONF_DEVICES]:
        if not first_iteration:
            await asyncio.sleep(10)
        first_iteration = False

        device_data = entry.data[CONF_DEVICES][device_id]
        name = device_data[CONF_NAME]
        mac = device_data[CONF_MAC]

        # Register device
        device_registry = dr.async_get(hass)
        dev = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, mac)},
            name=name
        )

        coordinator = getCoordinator(hass, device_data, dev)
        if coordinator is None:
            _LOGGER.error("Skipping device %s: unsupported model", name)
            continue

        # Don't block setup on initial connection - let it happen in background
        try:
            await asyncio.wait_for(coordinator.async_request_refresh(), timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            _LOGGER.warning("Initial connection to %s timed out or was cancelled, will retry in background: %s", name, e)
        except Exception as e:
            _LOGGER.warning("Initial connection to %s failed, will retry in background: %s", name, e)

        runtime_data.devices[device_id] = coordinator

    entry.runtime_data = runtime_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up update listener
    entry.async_on_unload(entry.add_update_listener(update_listener))

    # Register services (only once across all entries)
    if not hass.services.has_service(DOMAIN, "request_update"):
        hass.services.async_register(DOMAIN, "request_update", partial(service_request_update, hass))

    return True

# Service-call to update values
async def service_request_update(hass, call: ServiceCall):
    """Handle the service call to update entities for a specific device."""
    device_id = call.data.get("device_id")
    if not device_id:
        _LOGGER.error("Device ID is required")
        return

    # Get the device entry from the device registry
    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get(device_id)
    if not device_entry:
        _LOGGER.error("No device entry found for device ID %s", device_id)
        return

    # Find the coordinator corresponding to the given device ID
    for entry in hass.config_entries.async_entries(DOMAIN):
        if not hasattr(entry, "runtime_data") or entry.runtime_data is None:
            continue
        for coordinator in entry.runtime_data.devices.values():
            if getattr(coordinator, "device_id", None) == device_id:
                await coordinator.async_request_refresh()
                return

    _LOGGER.warning("No coordinator found for device ID %s", device_id)


# Example migration function
async def async_migrate_entry(hass, config_entry: ConfigEntry):
    if config_entry.version == 1:
        _LOGGER.error(
            "You have an old PAX configuration, please remove and add again. Sorry for the inconvenience!"
        )
        return False

    return True


async def update_listener(hass: HomeAssistant, entry: PaxBleConfigEntry):
    _LOGGER.debug("Updating Pax BLE entry!")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PaxBleConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading Pax BLE entry!")

    # Make sure we are disconnected
    for coordinator in entry.runtime_data.devices.values():
        await coordinator.disconnect()

    # Unload entries
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: PaxBleConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove device from config entry. HA handles entity/device registry cleanup."""
    # Find MAC(s) matching this device entry via identifiers
    macs_to_remove = []
    for dev_id, dev_config in config_entry.data[CONF_DEVICES].items():
        if (DOMAIN, dev_config[CONF_MAC]) in device_entry.identifiers:
            macs_to_remove.append(dev_config[CONF_MAC])

    new_data = dict(config_entry.data)
    new_data[CONF_DEVICES] = dict(new_data[CONF_DEVICES])
    for mac in macs_to_remove:
        new_data[CONF_DEVICES].pop(mac, None)
    hass.config_entries.async_update_entry(config_entry, data=new_data)

    return True
