"""Sensor adapter: a numeric reading from one leaf.

Roles:

    reads.state      the reading

Options:

    unit, device_class, state_class, precision   passed through
    map             payload -> word, for a register whose value is a code
                    rather than a quantity. With a map the reading is a word
                    and no range guard applies; without one it is a number.
    sane            [low, high] outside which a payload is data rather than a
                    reading. An unconfigured channel publishes `-nan`, and on
                    some channel types a value far outside the plausible range
                    is how the channel says it has nothing.

WHAT IS RESTORED, and the line that actually matters. It is not sensor versus
control - it is whether the value moves on its own.

A QUANTITY has its own cadence and will be replaced shortly, so it is not
restored: presenting a reading from before a restart as the value *now* is a
different kind of wrong from presenting none, and it writes a flat line into
history that never happened. A sensor of that kind starts empty and waits, and
the wait is short.

A CODE does not move on its own. A valve's direction, a fan's actual speed, a
unit's heat-or-cool - each changes only when something acts, so it behaves like
a control and last-known is the best answer available. Left unrestored it is
not "empty for a moment", it is empty until the next time that thing happens:
a valve that finished opening before a restart reads `unknown` for as long as
it then sits still, which on a mixing valve can be days.

So: a mapped reading restores, a numeric one does not. `map` is the declaration
of which kind this is, which is why nothing else needs to be configured.
"""
from __future__ import annotations

from homeassistant.components.sensor import (
    DEVICE_CLASSES_SCHEMA,
    STATE_CLASSES_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import I3ConfigEntry
from .const import SANE, options_schema, payload_map, vol
from .entity import I3Entity, async_add_adapter_entities
from .model import decode, parse_bounded


async def async_setup_entry(
    hass: HomeAssistant,
    entry: I3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_adapter_entities(hass, entry.runtime_data, "sensor", I3Sensor,
                               async_add_entities)


class I3Sensor(I3Entity, SensorEntity):
    OPTIONS = options_schema({
        vol.Optional("unit"): cv.string,
        vol.Optional("device_class"): DEVICE_CLASSES_SCHEMA,
        vol.Optional("state_class"): STATE_CLASSES_SCHEMA,
        vol.Optional("precision"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("map"): payload_map(),
        vol.Optional("sane"): SANE,
    })

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        opts = self._options
        self._map: dict[str, str] = opts.get("map") or {}
        self._sane = opts.get("sane")
        if self._map:
            # A mapped reading is an ENUM sensor, and Home Assistant refuses
            # the combination of options without that device class - it raises
            # "providing enum options, but is missing the enum device class"
            # every time the state is written, so the value never lands and the
            # entity reads `unknown` while its topic is publishing normally.
            # An enum has no unit and no state class either; carrying one is
            # the other half of the same refusal, so neither is taken from the
            # options.
            self._attr_options = sorted(set(self._map.values()))
            self._attr_device_class = SensorDeviceClass.ENUM
        else:
            self._attr_native_unit_of_measurement = opts.get("unit")
            self._attr_device_class = opts.get("device_class")
            self._attr_state_class = opts.get("state_class")
        if opts.get("precision") is not None:
            self._attr_suggested_display_precision = opts["precision"]
        self._value: float | str | None = None

    @property
    def native_value(self) -> float | str | None:
        return self._value

    def _read_points(self) -> None:
        payload = self._payload("state")
        if payload is None:
            return
        if self._map:
            # A code, not a quantity: an unmapped value is left alone rather
            # than shown raw, because a bare register number in a field that
            # otherwise reads "heating" is worse than the previous word.
            word = decode(payload, self._map)
            if word is not None:
                self._value = word
            return
        value = parse_bounded(payload, self._sane)
        if value is not None:
            self._value = value

    def _restore(self, last) -> None:
        # A code, not a quantity. See the module docstring for why that is the
        # line. `last.state` is already the mapped word, and it is only taken
        # back if it is still one this entity offers - a map that has since
        # gained or lost a value should not resurrect a word that no longer
        # means anything.
        if self._map and last.state in set(self._map.values()):
            self._value = last.state
