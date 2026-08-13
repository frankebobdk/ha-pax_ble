import asyncio
import datetime as dt
import logging

from abc import ABC, abstractmethod
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .devices.base_device import BaseDevice

_LOGGER = logging.getLogger(__name__)


class BaseCoordinator(DataUpdateCoordinator, ABC):

    def __init__(
        self,
        hass,
        device: DeviceEntry,
        model: str,
        scan_interval: int,
        scan_interval_fast: int,
    ):
        """Initialize coordinator parent"""
        super().__init__(
            hass,
            _LOGGER,
            name=model + ": " + device.name,
            update_interval=dt.timedelta(seconds=scan_interval),
        )

        self._fan: BaseDevice | None = None  # Set by subclass
        self._device = device
        self._model = model

        self._fast_poll_enabled = False
        self._fast_poll_count = 0
        self._normal_poll_interval = scan_interval
        self._fast_poll_interval = scan_interval_fast

        self._deviceInfoLoaded = False
        self._last_config_timestamp = None

        # Adaptive poll backoff: tracks consecutive failed poll cycles
        self._consecutive_poll_failures = 0
        self._max_backoff = 600  # 10 minutes max poll interval
        # Report failures to the coordinator (-> entities unavailable) only
        # after this many consecutive failed polls. An occasional missed poll
        # is normal for BLE and self-corrects on the next cycle; flapping
        # every entity unavailable on one blip would make the signal useless.
        self._failure_report_threshold = 3

        # Initialize state in case of new integration
        self._state = {}
        self._state["boostmodespeedwrite"] = 2400
        self._state["boostmodesecwrite"] = 600

    @property
    def fan(self) -> BaseDevice:
        return self._fan

    @property
    def device_id(self):
        return self._device.id

    @property
    def devicename(self):
        return self._device.name

    @property
    def identifiers(self):
        return self._device.identifiers

    def setFastPollMode(self):
        """Enable fast polling after a successful write."""
        _LOGGER.debug("Enabling fast poll mode for %s", self.devicename)
        self._fast_poll_enabled = True
        self._fast_poll_count = 0
        self.update_interval = dt.timedelta(seconds=self._fast_poll_interval)

        # Assigning update_interval only stores the value - the coordinator's
        # setter does not re-arm the already-scheduled refresh, so the next poll
        # would still land at the old interval (up to scan_interval away) and
        # values just written to the device would keep reading stale.
        # Deliberately NOT async_request_refresh(): polling immediately reads
        # the device before it has applied the write, and the stale result
        # overwrites the optimistically published state.
        self._schedule_refresh()

    def setNormalPollMode(self):
        """Return to normal polling, with backoff if there have been failures."""
        self._fast_poll_enabled = False
        if self._consecutive_poll_failures > 0:
            interval = min(
                self._normal_poll_interval * (2 ** self._consecutive_poll_failures),
                self._max_backoff,
            )
            _LOGGER.debug("Setting backoff poll interval %ds for %s (failures: %d)",
                         interval, self.devicename, self._consecutive_poll_failures)
        else:
            interval = self._normal_poll_interval
        self.update_interval = dt.timedelta(seconds=interval)

        # Same re-arm as setFastPollMode(): assigning update_interval only
        # stores the value, so the pending tick would still fire once at
        # the old (fast) interval before the normal interval takes effect.
        self._schedule_refresh()

    async def disconnect(self):
        """Disconnect from device."""
        if self._fan:
            await self._fan.disconnect()

    async def _safe_connect(self) -> bool:
        """Connect with validation of existing connections. Returns True if connected."""
        if not self._fan:
            return False

        # Validate existing connection (may be stale)
        if self._fan.isConnected():
            if await self._fan.validate_connection():
                return True
            _LOGGER.debug("Existing connection to %s failed validation", self.devicename)

        # Fresh connection — establish_connection handles retries internally
        timeout = 45 if self._consecutive_poll_failures > 2 else 30
        if await self._fan.connect(timeout=timeout):
            return True

        _LOGGER.warning("Failed to connect to %s", self.devicename)
        return False

    async def _async_update_data(self):
        _LOGGER.debug("Coordinator updating data for %s", self.devicename)

        self._update_poll_counter()

        # Fetch device info (once, on first successful poll)
        if not self._deviceInfoLoaded:
            try:
                async with asyncio.timeout(45):
                    if await self.read_deviceinfo(disconnect=False):
                        await self._async_update_device_info()
                        self._deviceInfoLoaded = True
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("Failed loading device info for %s: %s", self.devicename, err)

        # Fetch config data (once per day)
        if dt.datetime.now().date() != self._last_config_timestamp:
            try:
                async with asyncio.timeout(45):
                    if await self.read_configdata(disconnect=False):
                        self._last_config_timestamp = dt.datetime.now().date()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("Failed loading config for %s: %s", self.devicename, err)

        # Fetch sensor data (every poll)
        try:
            async with asyncio.timeout(30):
                success = await self.read_sensordata(disconnect=not self._fast_poll_enabled)
                if success:
                    if self._consecutive_poll_failures > 0:
                        _LOGGER.info("Successful read from %s, resetting backoff", self.devicename)
                        self._consecutive_poll_failures = 0
                        self.setNormalPollMode()
                    return self._state
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("Failed fetching sensor data for %s: %s", self.devicename, err)

        # If we reach here, sensor read failed — increase backoff
        self._consecutive_poll_failures = min(self._consecutive_poll_failures + 1, 20)
        _LOGGER.debug("Poll failure %d for %s", self._consecutive_poll_failures, self.devicename)
        self.setNormalPollMode()

        # Below the report threshold, return the last known state instead of
        # raising: returning is how the coordinator signals a successful poll,
        # so entities stay available on the value last read. The staleness is
        # bounded to threshold-1 polls; a sustained outage is reported below.
        if self._consecutive_poll_failures < self._failure_report_threshold:
            return self._state
        raise UpdateFailed(
            "No successful read from %s in %d consecutive attempts"
            % (self.devicename, self._consecutive_poll_failures)
        )

    async def _async_update_device_info(self) -> None:
        device_registry = dr.async_get(self.hass)
        device_registry.async_update_device(
            self.device_id,
            manufacturer=self.get_data("manufacturer"),
            model=self.get_data("model"),
            hw_version=self.get_data("hw_rev"),
            sw_version=self.get_data("sw_rev"),
        )
        _LOGGER.debug("Updated device data for: %s", self.devicename)

    def _update_poll_counter(self):
        if self._fast_poll_enabled:
            self._fast_poll_count += 1
            if self._fast_poll_count > 10:
                self.setNormalPollMode()

    def get_data(self, key):
        if key in self._state:
            return self._state[key]
        return None

    def set_data(self, key, value):
        _LOGGER.debug("Set_Data: %s %s", key, value)
        self._state[key] = value

        # Publish straight away. Callers set the value here and only tell Home
        # Assistant after the device write returns, which leaves the state
        # machine reporting the old value for the whole duration of that write
        # (0.4-0.8s measured upstream) - the frontend toggle visibly snaps
        # back and forth without this.
        self.async_update_listeners()

    async def read_deviceinfo(self, disconnect=False) -> bool:
        _LOGGER.debug("Reading device information")
        if not await self._safe_connect():
            _LOGGER.warning("Cannot read device info: not connected to %s", self.devicename)
            return False

        # Fetch data. Some data may not be availiable, that's okay.
        try:
            self._state["manufacturer"] = await self._fan.getManufacturer()
        except Exception as err:
            _LOGGER.debug("Couldn't read manufacturer! %s", str(err))
        try:
            self._state["model"] = await self._fan.getDeviceName()
        except Exception as err:
            _LOGGER.debug("Couldn't read device name! %s", str(err))
        try:
            self._state["fw_rev"] = await self._fan.getFirmwareRevision()
        except Exception as err:
            _LOGGER.debug("Couldn't read firmware revision! %s", str(err))
        try:
            self._state["hw_rev"] = await self._fan.getHardwareRevision()
        except Exception as err:
            _LOGGER.debug("Couldn't read hardware revision! %s", str(err))
        try:
            self._state["sw_rev"] = await self._fan.getSoftwareRevision()
        except Exception as err:
            _LOGGER.debug("Couldn't read software revision! %s", str(err))

        if not self._fan.isConnected():
            return False
        elif disconnect:
            await self._fan.disconnect()
        return True

    # Must be overridden by subclass
    @abstractmethod
    async def read_sensordata(self, disconnect=False) -> bool:
        _LOGGER.debug("Reading sensor data")

    # Must be overridden by subclass
    @abstractmethod
    async def write_data(self, key) -> bool:
        _LOGGER.debug("Write_Data: %s", key)

    # Must be overridden by subclass
    @abstractmethod
    async def read_configdata(self, disconnect=False) -> bool:
        _LOGGER.debug("Reading config data")
