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

WHAT IS RESTORED: everything this entity last held, quantities included.

This reversed in 0.3.0, and the reasoning it replaced is worth keeping because
it sounds right. It said: a quantity has its own cadence and will be replaced
shortly, so restoring one presents a reading from before the restart as the
value *now*, and writes a flat line into history that never happened. Only a
mapped CODE - a valve's direction, a fan's actual speed - was restored, because
a code only moves when something acts.

That argument borrows its premise from a polled source, where silence means no
data. This controller is not polled. It publishes a leaf ONLY when that leaf
changes, so silence means the value has not changed - and the restored reading
is therefore not a guess about the present, it is the present, for exactly as
long as the controller stays quiet. The flat line is not one that never
happened; it is the one that did.

Measured at a reference installation 2026-10-15, after a restart at 11:42 on a
steady midday house: 17 of 22 numeric sensors still blank 20 minutes later,
every underfloor thermostat reading `current_temperature: null`, and the buffer
sensor - which had been publishing every 30 to 120 seconds while the heating
ran that morning - last heard from at 11:21. Nothing was wrong. The house was
simply not changing, which is the case where the old rule left a dashboard with
no numbers on it for hours.

The one thing silence cannot distinguish is a controller that has gone away,
and that is already answered elsewhere: every entity's availability follows its
controller's heartbeat, so a dead controller takes its entities unavailable
whatever they last held. Restoring a reading never has to carry that job.

`sane` still applies on the way back in, so a restored value that the current
configuration would reject as data is dropped rather than resurrected.
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
        # See the module docstring: the controller publishes on change, so a
        # value it has not replaced is still the value.
        if self._map:
            # `last.state` is already the mapped word, and it is only taken
            # back if it is still one this entity offers - a map that has since
            # gained or lost a value should not resurrect a word that no longer
            # means anything.
            if last.state in set(self._map.values()):
                self._value = last.state
            return
        # A quantity goes back through the same guard a live payload does, so
        # a reading the current `sane` range would refuse as data is dropped
        # rather than resurrected - the range is part of the configuration and
        # may have been tightened since this value was written.
        value = parse_bounded(last.state, self._sane)
        if value is not None:
            self._value = value
