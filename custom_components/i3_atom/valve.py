"""Valve adapter: a valve whose opening is one leaf.

Roles:

    reads.state      the opening, in the channel's own scale
    writes.command   the new opening; without it the valve is shown, not driven

Options:

    position_scale   the channel's full-scale value (default 100)
    device_class     `water` or `gas`

Zero is closed and anything above it is open, so there is no separate open or
closed leaf to keep in step. A valve that only reports its opening - one the
controller drives itself - is still a valve rather than a bare number, and
reads as open or closed at a glance.

Restored, because a valve that has stopped moving publishes nothing until it
moves again, which for a valve nobody touches can be days.
"""
from __future__ import annotations

from homeassistant.components.valve import (
    ATTR_CURRENT_POSITION,
    DEVICE_CLASSES_SCHEMA,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import I3ConfigEntry
from .const import FULL_SCALE, options_schema, vol
from .entity import I3Entity, async_add_adapter_entities
from .model import parse_number, scale_to


async def async_setup_entry(
    hass: HomeAssistant,
    entry: I3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_adapter_entities(hass, entry.runtime_data, "valve", I3Valve,
                               async_add_entities)


class I3Valve(I3Entity, ValveEntity):
    OPTIONS = options_schema({
        vol.Optional("position_scale", default=100): FULL_SCALE,
        vol.Optional("device_class"): DEVICE_CLASSES_SCHEMA,
    })

    _attr_reports_position = True

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        self._scale = self._options["position_scale"]
        self._attr_device_class = self._options.get("device_class")
        features = ValveEntityFeature(0)
        if "command" in spec.writes:
            features |= (ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
                         | ValveEntityFeature.SET_POSITION)
        self._attr_supported_features = features
        self._position: int | None = None

    @property
    def current_valve_position(self) -> int | None:
        return self._position

    async def async_set_valve_position(self, position: int) -> None:
        level = scale_to(position, 100, self._scale)
        self._position = scale_to(level, self._scale, 100)
        await self._command("command", str(level))

    async def async_open_valve(self) -> None:
        await self.async_set_valve_position(100)

    async def async_close_valve(self) -> None:
        await self.async_set_valve_position(0)

    def _read_points(self) -> None:
        payload = self._payload("state")
        if payload is None:
            return
        value = parse_number(payload)
        if value is None:
            return
        self._position = scale_to(value, self._scale, 100)

    def _restore(self, last) -> None:
        position = last.attributes.get(ATTR_CURRENT_POSITION)
        if position is not None:
            self._position = int(position)
