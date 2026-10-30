"""Cover adapter: a blind, a shutter or a curtain track on a composite block.

Roles:

    reads.state      the position, in the channel's own scale
    reads.direction  which way it is travelling, if the channel says
    reads.tilt       the slat angle, in the channel's own scale
    writes.command   the new position; without it the cover is shown, not driven
    writes.stop      halt where it is
    writes.tilt      the new slat angle

Options:

    position_scale   the position channel's full-scale value (default 100)
    tilt_scale       the angle channel's full-scale value (default 90)
    device_class     `blind`, `curtain`, `shade`, `shutter`, ...

The channel behind this is not a terminal. An i3 `blinds` block drives a PAIR
of relay outputs - one to open, one to close - and reports one aggregate
position, the same arrangement `valve.py`'s three-way valve has. The pair is in
the site descriptor and the electrician's schedule; nothing here knows about it,
and nothing here should: the interlock that stops both relays being energised at
once is the controller's, which is the only place it can be made reliable.

`position` and not the channel's `main` leaf. The vendor documents `main` on a
roller blind as 1-100 %, so it cannot express a fully closed blind at all, and
on the two rotation types `main` is not a percentage - it is two packed bytes.
Which leaf is read is the site configuration's decision; this notes it because
a cover whose closed position is unreachable looks like a limit that was never
calibrated.

Restored, because a cover that has finished travelling publishes nothing until
it next moves, which for a blind left open is weeks.
"""
from __future__ import annotations

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    DEVICE_CLASSES_SCHEMA,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import I3ConfigEntry
from .const import FULL_SCALE, options_schema, vol
from .entity import I3Entity, async_add_adapter_entities
from .model import parse_number, scale_to

#: What the `direction` leaf means. The vendor documents 1 as opening and 2 as
#: closing on every covering type it defines. The rotation types add 3 and 4
#: for the slats, which are deliberately not here: they are not the body of the
#: blind moving, and reporting them as travel would make a cover that is
#: sitting still at a known position read as one in motion to an unknown one.
#: Anything else - including the 0 a stationary blind spends almost all of its
#: time publishing - is "not travelling".
OPENING, CLOSING = 1, 2


async def async_setup_entry(
    hass: HomeAssistant,
    entry: I3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_adapter_entities(hass, entry.runtime_data, "cover", I3Cover,
                               async_add_entities)


class I3Cover(I3Entity, CoverEntity):
    OPTIONS = options_schema({
        vol.Optional("position_scale", default=100): FULL_SCALE,
        vol.Optional("tilt_scale", default=90): FULL_SCALE,
        vol.Optional("device_class"): DEVICE_CLASSES_SCHEMA,
    })

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        self._scale = self._options["position_scale"]
        self._tilt_scale = self._options["tilt_scale"]
        self._attr_device_class = self._options.get("device_class")
        features = CoverEntityFeature(0)
        if "command" in spec.writes:
            features |= (CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
                         | CoverEntityFeature.SET_POSITION)
        if "stop" in spec.writes:
            features |= CoverEntityFeature.STOP
        if "tilt" in spec.writes:
            features |= (CoverEntityFeature.OPEN_TILT | CoverEntityFeature.CLOSE_TILT
                         | CoverEntityFeature.SET_TILT_POSITION)
        self._attr_supported_features = features
        self._position: int | None = None
        self._tilt: int | None = None
        self._direction: int | None = None

    # ── reading ────────────────────────────────────────────────────────────

    @property
    def current_cover_position(self) -> int | None:
        return self._position

    @property
    def current_cover_tilt_position(self) -> int | None:
        return self._tilt

    @property
    def is_opening(self) -> bool:
        return self._direction == OPENING

    @property
    def is_closing(self) -> bool:
        return self._direction == CLOSING

    @property
    def is_closed(self) -> bool | None:
        # None, not False, while the position is unknown. Home Assistant draws
        # a cover it believes to be open with an enabled close button and a
        # disabled open one, so guessing here hands somebody a control that
        # does nothing on the one occasion they most need it.
        if self._position is None:
            return None
        return self._position == 0

    # ── driving ────────────────────────────────────────────────────────────

    async def async_set_cover_position(self, **kwargs) -> None:
        await self._set_position(kwargs["position"])

    async def async_open_cover(self, **kwargs) -> None:
        await self._set_position(100)

    async def async_close_cover(self, **kwargs) -> None:
        await self._set_position(0)

    async def async_stop_cover(self, **kwargs) -> None:
        # No assumed position: the blind stops where it happens to be, and the
        # channel reports it. Claiming a number here would be inventing one.
        await self._command("stop", "1")

    async def async_set_cover_tilt_position(self, **kwargs) -> None:
        await self._set_tilt(kwargs["tilt_position"])

    async def async_open_cover_tilt(self, **kwargs) -> None:
        await self._set_tilt(100)

    async def async_close_cover_tilt(self, **kwargs) -> None:
        await self._set_tilt(0)

    async def _set_position(self, position: int) -> None:
        level = scale_to(position, 100, self._scale)
        self._position = scale_to(level, self._scale, 100)
        # Whatever it was doing, it is now doing what it was just told. The
        # direction leaf answers within the controller's publish tick; until
        # then a stale `closing` beside a rising position is the kind of
        # disagreement that reads as a broken cover.
        self._direction = None
        await self._command("command", str(level))

    async def _set_tilt(self, tilt: int) -> None:
        level = scale_to(tilt, 100, self._tilt_scale)
        self._tilt = scale_to(level, self._tilt_scale, 100)
        await self._command("tilt", str(level))

    # ── state ──────────────────────────────────────────────────────────────

    def _read_points(self) -> None:
        position = parse_number(self._payload("state") or "")
        if position is not None:
            self._position = scale_to(position, self._scale, 100)
        tilt = parse_number(self._payload("tilt") or "")
        if tilt is not None:
            self._tilt = scale_to(tilt, self._tilt_scale, 100)
        direction = parse_number(self._payload("direction") or "")
        if direction is not None:
            self._direction = int(direction)

    def _restore(self, last) -> None:
        # Position and tilt only. Travel is not restored on purpose: a blind
        # that was moving when Home Assistant stopped is not moving now, and a
        # restart that brings back a permanent "Opening" is a cover the
        # interface never lets go of.
        position = last.attributes.get(ATTR_CURRENT_POSITION)
        if position is not None:
            self._position = int(position)
        tilt = last.attributes.get(ATTR_CURRENT_TILT_POSITION)
        if tilt is not None:
            self._tilt = int(tilt)
