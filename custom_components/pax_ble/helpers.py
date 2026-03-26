import logging

from .const import (
    CONF_NAME,
    CONF_MODEL,
    CONF_MAC,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL_FAST,
)
from .const import DeviceModel
from .coordinator_calima import CalimaCoordinator
from .coordinator_svensa import SvensaCoordinator

_LOGGER = logging.getLogger(__name__)


def getCoordinator(hass, device_data, dev):
    name = device_data[CONF_NAME]
    model = device_data.get(CONF_MODEL, "Calima")
    mac = device_data[CONF_MAC]
    pin = device_data[CONF_PIN]
    scan_interval = device_data[CONF_SCAN_INTERVAL]
    scan_interval_fast = device_data[CONF_SCAN_INTERVAL_FAST]

    # Set up coordinator
    try:
        device_model = DeviceModel(model)
    except ValueError:
        _LOGGER.error("Unknown fan model: %s", model)
        return None

    match device_model:
        case DeviceModel.CALIMA | DeviceModel.SVARA | DeviceModel.LEVANTE:
            return CalimaCoordinator(
                hass, dev, model, mac, pin, scan_interval, scan_interval_fast
            )
        case DeviceModel.SVENSA:
            return SvensaCoordinator(
                hass, dev, model, mac, pin, scan_interval, scan_interval_fast
            )
        case _:
            _LOGGER.error("Unhandled fan model: %s", model)
            return None
