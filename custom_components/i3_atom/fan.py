"""Fan adapter: one leaf carrying a percentage.

Roles:

    reads.state      the current level
    writes.command   the new level

Options:

    speed_range_max   the channel's own full-scale value (default 100)

On and off are derived from the level rather than from a second leaf, because
the channel has only one: zero is off and anything above it is on. Turning on
without a percentage restores the last non-zero level, so a control that was
set to a third does not jump to full the next time it is used.

A channel coarser than a percent offers only the steps it has: a fan whose
full scale is 10 moves in tenths, rather than accepting 37% and quietly
running at 40.
"""
from __future__ import annotations

from homeassistant.components.fan import FanEntity, FanEntityFeature
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
    async_add_adapter_entities(hass, entry.runtime_data, "fan", I3Fan,
                               async_add_entities)


class I3Fan(I3Entity, FanEntity):
    OPTIONS = options_schema({vol.Optional("speed_range_max", default=100): FULL_SCALE})

    _attr_supported_features = (FanEntityFeature.SET_SPEED
                                | FanEntityFeature.TURN_ON
                                | FanEntityFeature.TURN_OFF)

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        self._max = self._options["speed_range_max"]
        self._attr_speed_count = min(100, int(self._max))
        self._percentage: int | None = None
        self._last_on: int = 100

    @property
    def percentage(self) -> int | None:
        return self._percentage

    @property
    def is_on(self) -> bool | None:
        return None if self._percentage is None else self._percentage > 0

    async def async_set_percentage(self, percentage: int) -> None:
        level = scale_to(percentage, 100, self._max)
        # Shown as the level the channel will actually hold.
        self._percentage = scale_to(level, self._max, 100)
        if self._percentage:
            self._last_on = self._percentage
        await self._command("command", str(level))

    async def async_turn_on(self, percentage: int | None = None, preset_mode=None,
                            **kwargs) -> None:
        await self.async_set_percentage(
            self._last_on if percentage is None else percentage)

    async def async_turn_off(self, **kwargs) -> None:
        await self.async_set_percentage(0)

    def _read_points(self) -> None:
        payload = self._payload("state")
        if payload is None:
            return
        value = parse_number(payload)
        if value is None:
            return
        self._percentage = scale_to(value, self._max, 100)
        if self._percentage:
            self._last_on = self._percentage

    def _restore(self, last) -> None:
        pct = last.attributes.get("percentage")
        if pct is not None:
            self._percentage = int(pct)
            if self._percentage:
                self._last_on = self._percentage
        elif last.state == "off":
            self._percentage = 0
