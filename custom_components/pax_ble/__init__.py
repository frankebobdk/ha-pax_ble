"""Support for Pax fans."""

import asyncio
import contextlib
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
    coordinators = []
    for device_id in entry.data[CONF_DEVICES]:
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

        # Polling stays disarmed until this device's first background refresh
        # has run: with a short scan_interval (the config flow allows 5s) the
        # first entity listener would otherwise schedule interval polls that
        # overlap the serial first-refresh chain below.
        coordinator.update_interval = None
        runtime_data.devices[device_id] = coordinator
        coordinators.append((name, coordinator))

    entry.runtime_data = runtime_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Initial connections run in the background so that setup - and with it
    # Home Assistant startup - is not gated on one serial BLE connection per
    # device (a 10s stagger per device makes that over a minute on a larger
    # installation). Entities are created immediately and fill in as each
    # device is read. The task is stored so that async_unload_entry can cancel
    # it before disconnecting.
    runtime_data.initial_refresh_task = entry.async_create_background_task(
        hass,
        _async_initial_refresh(coordinators),
        name="pax_ble initial refresh",
    )

    # Set up update listener
    entry.async_on_unload(entry.add_update_listener(update_listener))

    # Register services (only once across all entries)
    if not hass.services.has_service(DOMAIN, "request_update"):
        hass.services.async_register(DOMAIN, "request_update", partial(service_request_update, hass))

    return True

async def _async_initial_refresh(coordinators):
    """Perform each coordinator's first refresh, one device at a time.

    Serial on purpose: connecting to every device at once on startup can
    exhaust the connection slots of shared BLE proxies, which blocks all
    devices, not just the new ones. The pause between devices keeps the
    proxies free for whichever connection is in flight.
    """
    first_iteration = True
    for name, coordinator in coordinators:
        if not first_iteration:
            await asyncio.sleep(10)
        first_iteration = False

        try:
            # async_refresh() rather than async_request_refresh(): the latter
            # goes through the coordinator's debouncer and can return without
            # having refreshed, which would defeat the one-device-at-a-time
            # pacing this loop exists to provide.
            #
            # No outer timeout. This task is off the setup path, so nothing
            # is waiting on it, and a healthy first refresh can legitimately
            # take well over 30s (the coordinator budgets deviceinfo, config
            # and sensor reads separately, each over a multi-attempt
            # connect). An outer bound would cancel a slow-but-working
            # refresh mid-connect, and nothing on that cancellation path
            # disconnects - the abandoned client could keep holding a proxy
            # connection slot. The coordinator's own per-step timeouts
            # already bound how long this can wait.
            await coordinator.async_refresh()
        except Exception as e:
            # async_refresh() reports failure through last_update_success
            # rather than raising, so this only guards against an unexpected
            # error in one device ending the chain for the rest.
            # (CancelledError is a BaseException and propagates past this.)
            _LOGGER.warning("Initial connection to %s failed, will retry in background: %s", name, e)

        # Free the connection before moving to the next device,
        # unconditionally: no flag reliably marks the case that matters. A
        # partially failed refresh (connected, then a read timed out) stays
        # under the failure-report threshold, so last_update_success remains
        # True - yet the deviceinfo and config reads connect with
        # disconnect=False and the sensor read only disconnects on success,
        # leaving that established connection holding a shared proxy slot
        # into the next device's connect. After a fully successful refresh
        # this is an idempotent no-op. Deliberately not a finally: on
        # cancellation, async_unload_entry disconnects every coordinator
        # itself.
        with contextlib.suppress(Exception):
            await coordinator.disconnect()

        # First attempt is done, either way - arm regular polling. Assigning
        # update_interval alone does not re-arm an already-scheduled refresh;
        # setNormalPollMode() restores the configured interval and re-arms.
        coordinator.setNormalPollMode()


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

    # Stop the initial refresh chain before disconnecting. Core runs this
    # function first and only cancels the entry's background tasks afterwards,
    # so without this a reload can disconnect a device while its connect is
    # still in flight.
    initial_refresh_task = entry.runtime_data.initial_refresh_task
    entry.runtime_data.initial_refresh_task = None
    if initial_refresh_task is not None and not initial_refresh_task.done():
        initial_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await initial_refresh_task

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
