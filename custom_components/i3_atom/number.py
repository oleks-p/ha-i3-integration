"""Number adapter: one leaf holding a value that somebody sets.

Roles:

    reads.state      the current value
    writes.command   the new value

Options:

    min, max, step   the range offered (defaults 0, 100 and 1)
    unit             the channel's unit
    device_class     passed through
    mode             `auto`, `box` or `slider`
    sane             [low, high] outside which a payload is data rather than a
                     reading

For a setpoint, an analogue output level, a controller variable: values that
change only when something sets them. So it is restored - from Home
Assistant's own record of the value in the channel's unit, not from the
displayed state, which is in whatever unit the installation shows.
"""
from __future__ import annotations

from homeassistant.components.number import (
    DEVICE_CLASSES_SCHEMA,
    NumberMode,
    RestoreNumber,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import I3ConfigEntry
from .const import SANE, options_schema, vol
from .entity import I3Entity, async_add_adapter_entities
from .model import format_number, parse_bounded


async def async_setup_entry(
    hass: HomeAssistant,
    entry: I3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_adapter_entities(hass, entry.runtime_data, "number", I3Number,
                               async_add_entities)


def _range_in_order(opts: dict) -> dict:
    if opts["min"] >= opts["max"]:
        raise vol.Invalid(f"min {opts['min']} is not below max {opts['max']}")
    return opts


class I3Number(I3Entity, RestoreNumber):
    OPTIONS = vol.All(
        options_schema({
            vol.Optional("min", default=0): vol.Coerce(float),
            vol.Optional("max", default=100): vol.Coerce(float),
            vol.Optional("step", default=1): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional("unit"): cv.string,
            vol.Optional("device_class"): DEVICE_CLASSES_SCHEMA,
            vol.Optional("mode", default=NumberMode.AUTO): vol.Coerce(NumberMode),
            vol.Optional("sane"): SANE,
        }),
        _range_in_order,
    )

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        opts = self._options
        self._attr_native_min_value = opts["min"]
        self._attr_native_max_value = opts["max"]
        self._attr_native_step = opts["step"]
        self._attr_native_unit_of_measurement = opts.get("unit")
        self._attr_device_class = opts.get("device_class")
        self._attr_mode = opts["mode"]
        self._sane = opts.get("sane")
        self._value: float | None = None

    @property
    def native_value(self) -> float | None:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = float(value)
        await self._command("command", format_number(value))

    def _read_points(self) -> None:
        payload = self._payload("state")
        if payload is None:
            return
        value = parse_bounded(payload, self._sane)
        if value is not None:
            self._value = value

    async def _async_restore(self) -> None:
        data = await self.async_get_last_number_data()
        if data is not None and data.native_value is not None:
            self._value = data.native_value

    def _restore(self, last) -> None:
        return                          # see _async_restore
