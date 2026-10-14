"""Switch adapter: a channel that is on or off, and that something switches.

Roles:

    reads.state      the value: non-zero is on
    writes.command   1 or 0; without it the switch cannot be operated

Options:

    device_class     `outlet` or `switch`
    invert           the channel is on at zero

The same relay `light.py` presents as a lamp, presented as what it drives
here - a pump, an actuator, anything whose control is a switch rather than a
lamp. Until this existed such a relay could only be read, as a binary sensor.

Restored, because it changes only when something switches it.
"""
from __future__ import annotations

from homeassistant.components.switch import DEVICE_CLASSES_SCHEMA, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import I3ConfigEntry
from .const import options_schema, vol
from .entity import I3Entity, async_add_adapter_entities
from .model import parse_number


async def async_setup_entry(
    hass: HomeAssistant,
    entry: I3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_adapter_entities(hass, entry.runtime_data, "switch", I3Switch,
                               async_add_entities)


class I3Switch(I3Entity, SwitchEntity):
    OPTIONS = options_schema({
        vol.Optional("device_class"): DEVICE_CLASSES_SCHEMA,
        vol.Optional("invert", default=False): cv.boolean,
    })

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        self._attr_device_class = self._options.get("device_class")
        self._invert: bool = self._options["invert"]
        self._is_on: bool | None = None

    @property
    def is_on(self) -> bool | None:
        return self._is_on

    async def async_turn_on(self, **kwargs) -> None:
        self._is_on = True
        await self._command("command", "0" if self._invert else "1")

    async def async_turn_off(self, **kwargs) -> None:
        self._is_on = False
        await self._command("command", "1" if self._invert else "0")

    def _read_points(self) -> None:
        payload = self._payload("state")
        if payload is None:
            return
        value = parse_number(payload)
        if value is None:
            return
        self._is_on = (value <= 0) if self._invert else (value > 0)

    def _restore(self, last) -> None:
        self._is_on = last.state == "on"
