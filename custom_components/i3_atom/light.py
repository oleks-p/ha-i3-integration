"""Light adapter.

Roles it reads and writes, which are this file's vocabulary and mean nothing to
the core:

    reads.state      the level or on/off value
    writes.command   the same, in the other direction

Options:

    brightness_scale   the channel's own full-scale value. Present means the
                       entity is dimmable; absent means on/off.

Nothing about a channel type appears here. A relay and a dimmer differ by
whether a scale was configured, which is a fact about the circuit rather than
about the controller.
"""
from __future__ import annotations

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import I3ConfigEntry
from .const import FULL_SCALE, options_schema, vol
from .entity import I3Entity, async_add_adapter_entities
from .model import parse_number, scale_to

#: Home Assistant's brightness range. The channel's own is configuration.
HA_BRIGHTNESS_MAX = 255


async def async_setup_entry(
    hass: HomeAssistant,
    entry: I3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_adapter_entities(hass, entry.runtime_data, "light", I3Light,
                               async_add_entities)


class I3Light(I3Entity, LightEntity):
    OPTIONS = options_schema({vol.Optional("brightness_scale"): FULL_SCALE})

    # The FLAG type, not a plain 0. Home Assistant tests this with `in`, which
    # an int does not support - every light failed to add with "argument of
    # type 'int' is not a container or iterable", and because the registry row
    # is created before the capability check, the result was every light
    # existing and permanently unavailable rather than an obvious absence.
    _attr_supported_features = LightEntityFeature(0)

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        self._scale = self._options.get("brightness_scale")
        if self._scale:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        else:
            self._attr_color_mode = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._is_on: bool | None = None
        self._brightness: int | None = None

    @property
    def is_on(self) -> bool | None:
        return self._is_on

    @property
    def brightness(self) -> int | None:
        return self._brightness if self._scale else None

    async def async_turn_on(self, **kwargs) -> None:
        if self._scale:
            brightness = int(kwargs.get(ATTR_BRIGHTNESS,
                                        self._brightness or HA_BRIGHTNESS_MAX))
            level = scale_to(brightness, HA_BRIGHTNESS_MAX, self._scale)
            self._brightness = brightness
            # A dimmer commanded to zero is off, and reporting it as on would
            # claim a level the channel does not hold.
            self._is_on = level > 0
            payload = str(level)
        else:
            payload = "1"
            self._is_on = True
        await self._command("command", payload)

    async def async_turn_off(self, **kwargs) -> None:
        self._is_on = False
        await self._command("command", "0")

    # ── state ──────────────────────────────────────────────────────────────

    def _read_points(self) -> None:
        payload = self._payload("state")
        if payload is None:
            return
        value = parse_number(payload)
        if value is None:
            # Silence, or an unconfigured channel. Leaving the last value in
            # place is the point; overwriting it would reintroduce a blank
            # entity by another route.
            return
        self._is_on = value > 0
        if self._scale and value > 0:
            # Zero is off rather than on-at-zero, and keeping the last non-zero
            # level means the next press restores what somebody chose instead of
            # jumping to full.
            self._brightness = scale_to(value, self._scale, HA_BRIGHTNESS_MAX)

    def _restore(self, last) -> None:
        self._is_on = last.state == "on"
        if self._scale:
            restored = last.attributes.get(ATTR_BRIGHTNESS)
            self._brightness = int(restored) if restored is not None else None
