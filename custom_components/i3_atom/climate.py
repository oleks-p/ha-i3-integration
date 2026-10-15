"""Climate adapter.

One adapter, three thermostat shapes, told apart entirely by which roles the
configuration binds and which options it sets. Nothing here knows a channel
type.

Roles:

    reads.mode                 the mode, decoded through `mode_map`
    reads.setpoint             a single target
    reads.setpoint_low         the heating target of a dual-setpoint thermostat
    reads.setpoint_high        the cooling target
    reads.current_temperature  the measured temperature
    reads.fan_mode             the fan setting, decoded through `fan_mode_map`

    writes.mode                the mode
    writes.power               on/off where the mode and the power are separate
                               leaves and the mode cannot be written directly
    writes.setpoint            }
    writes.setpoint_low        } whichever the thermostat has
    writes.setpoint_high       }
    writes.fan_mode            the fan setting

Options:

    modes               the mode words this thermostat offers
    mode_map            payload -> word, used in both directions. A word that
                        is not in `modes` is ignored when it arrives, so one
                        map can serve thermostats that offer fewer modes.
    mode_from_presence  the mode is not a value of its own: a readable setpoint
                        means `on_mode`, silence means off. Some thermostats
                        carry the setpoint and the fact of having one on the
                        same leaf.
    on_mode             which word `mode_from_presence` reports when on
    fan_modes, fan_mode_map
    min_temp, max_temp, temp_step
    sane                [low, high] outside which a setpoint payload is data
                        rather than a reading

WHAT IS RESTORED: the mode, the setpoints, the fan mode and, since 0.3.0, the
measured temperature.

`current_temperature` used to be left out, on the reasoning that a measurement
has its own cadence and showing a reading from before the restart as the
temperature now is a different kind of wrong than showing none. See
`sensor.py`'s docstring for why that premise does not hold on this controller:
it publishes a leaf only when the leaf changes, so a temperature it has not
replaced is still the temperature.

The cost of leaving it out was concentrated here rather than spread. A sensor
with no value is one blank row; a thermostat with no `current_temperature` is a
card that shows the setpoint and nothing else, and reads as broken. Measured at
a reference installation 2026-10-15, twenty minutes after a restart on a steady
midday house: every underfloor thermostat in the house, ten of them, reading
`current_temperature: null`, with a room that had not changed temperature since
10:21 and had no reason to publish.
"""
from __future__ import annotations

import logging

from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.unit_conversion import TemperatureConverter

from . import I3ConfigEntry
from .const import SANE, options_schema, payload_map, vol, word
from .entity import I3Entity, async_add_adapter_entities
from .model import decode, encode, format_number, parse_bounded

_LOGGER = logging.getLogger(__name__)

HVAC_MODE = vol.All(word, vol.Coerce(HVACMode))
TEMPERATURE = vol.Coerce(float)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: I3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_adapter_entities(hass, entry.runtime_data, "climate", I3Climate,
                               async_add_entities)


class I3Climate(I3Entity, ClimateEntity):
    OPTIONS = options_schema({
        vol.Optional("modes", default=[HVACMode.OFF]): vol.All([HVAC_MODE], vol.Length(min=1)),
        vol.Optional("mode_map"): payload_map(HVAC_MODE),
        vol.Optional("mode_from_presence", default=False): cv.boolean,
        vol.Optional("on_mode", default=HVACMode.HEAT): HVAC_MODE,
        vol.Optional("fan_modes"): [word],
        vol.Optional("fan_mode_map"): payload_map(),
        vol.Optional("min_temp", default=7): TEMPERATURE,
        vol.Optional("max_temp", default=35): TEMPERATURE,
        vol.Optional("temp_step", default=0.5): TEMPERATURE,
        vol.Optional("sane"): SANE,
    })

    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        opts = self._options
        self._mode_map: dict[str, HVACMode] = opts.get("mode_map") or {}
        self._fan_mode_map: dict[str, str] = opts.get("fan_mode_map") or {}
        self._from_presence: bool = opts["mode_from_presence"]
        self._on_mode: HVACMode = opts["on_mode"]
        self._sane = opts.get("sane")
        self._fan_modes: list[str] = list(opts.get("fan_modes") or [])

        self._attr_hvac_modes = list(opts["modes"])
        self._attr_min_temp = opts["min_temp"]
        self._attr_max_temp = opts["max_temp"]
        self._attr_target_temperature_step = opts["temp_step"]

        features = ClimateEntityFeature(0)
        # The dual-setpoint form and the single-setpoint form are mutually
        # exclusive in Home Assistant's own contract: offering both makes a card
        # with three temperature controls, two of which do nothing.
        self._dual = "setpoint_low" in spec.writes or "setpoint_high" in spec.writes
        if self._dual:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        elif "setpoint" in spec.writes:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if "fan_mode" in spec.writes or "fan_mode" in spec.reads:
            features |= ClimateEntityFeature.FAN_MODE
            self._attr_fan_modes = self._fan_modes
        if "mode" in spec.writes or "power" in spec.writes:
            features |= (ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF)
        self._attr_supported_features = features

        self._mode: HVACMode | None = None
        self._setpoint: float | None = None
        self._low: float | None = None
        self._high: float | None = None
        self._current: float | None = None
        self._fan: str | None = None
        self._ignored: set[str] = set()

    # ── state ──────────────────────────────────────────────────────────────

    @property
    def hvac_mode(self) -> HVACMode | None:
        return self._mode

    @property
    def current_temperature(self) -> float | None:
        return self._current

    @property
    def target_temperature(self) -> float | None:
        return None if self._dual else self._setpoint

    @property
    def target_temperature_low(self) -> float | None:
        return self._low if self._dual else None

    @property
    def target_temperature_high(self) -> float | None:
        return self._high if self._dual else None

    @property
    def fan_mode(self) -> str | None:
        return self._fan

    # ── commands ───────────────────────────────────────────────────────────

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        mode = HVACMode(hvac_mode)
        if "mode" in self._spec.writes:
            payload = encode(mode, self._mode_map)
            if payload is None:
                raise ServiceValidationError(
                    f"{self.entity_id}: no payload maps to {mode.value!r}")
            self._mode = mode
            await self._command("mode", payload)
            return
        if "power" in self._spec.writes:
            # Mode and power on separate leaves: the thermostat can only be
            # told on or off, and which kind of on it does is its own business.
            self._mode = mode
            await self._command("power", "0" if mode == HVACMode.OFF else "1")
            return
        raise ServiceValidationError(f"{self.entity_id} cannot be switched")

    async def async_set_temperature(self, **kwargs) -> None:
        # Home Assistant passes a mode given alongside the temperature through
        # to here rather than applying it itself.
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(mode)
        for key, role, attr in ((ATTR_TARGET_TEMP_LOW, "setpoint_low", "_low"),
                                (ATTR_TARGET_TEMP_HIGH, "setpoint_high", "_high"),
                                (ATTR_TEMPERATURE, "setpoint", "_setpoint")):
            value = kwargs.get(key)
            if value is not None and role in self._spec.writes:
                setattr(self, attr, float(value))
                await self._command(role, format_number(value))

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        payload = encode(fan_mode, self._fan_mode_map)
        if payload is None:
            raise ServiceValidationError(f"{self.entity_id}: no payload maps to {fan_mode!r}")
        self._fan = fan_mode
        await self._command("fan_mode", payload)

    # ── incoming ───────────────────────────────────────────────────────────

    def _read_points(self) -> None:
        setpoint_payload = self._payload("setpoint")
        if setpoint_payload is not None:
            value = parse_bounded(setpoint_payload, self._sane)
            if value is not None:
                self._setpoint = value
            if self._from_presence:
                # The setpoint and the fact of having one share a leaf: a
                # reading means the thermostat is regulating, silence means it
                # is not. This is the only mode information such a channel has,
                # so it is read even when the value itself is rejected.
                self._mode = self._on_mode if value is not None else HVACMode.OFF

        for role, attr in (("setpoint_low", "_low"), ("setpoint_high", "_high")):
            payload = self._payload(role)
            if payload is None:
                continue
            value = parse_bounded(payload, self._sane)
            if value is not None:
                setattr(self, attr, value)

        mode_payload = self._payload("mode")
        if mode_payload is not None and not self._from_presence:
            mode = self._decode_mode(mode_payload)
            if mode is not None:
                self._mode = mode

        fan_payload = self._payload("fan_mode")
        if fan_payload is not None:
            fan = decode(fan_payload, self._fan_mode_map)
            if fan is not None and (not self._fan_modes or fan in self._fan_modes):
                self._fan = fan

        current = self._payload("current_temperature")
        if current is not None:
            value = parse_bounded(current, self._sane)
            if value is not None:
                self._current = value

    def _decode_mode(self, payload: str) -> HVACMode | None:
        """A mode payload as one of this thermostat's modes, or None.

        A map shared by several thermostats can name modes one of them does
        not offer, and showing such a mode puts the entity in a state its own
        card cannot select or leave. Ignored instead - the last mode stands -
        and said once, because a thermostat that reports a mode it does not
        have is worth knowing about.
        """
        word_ = decode(payload, self._mode_map)
        if word_ is None:
            return None
        try:
            mode = HVACMode(word_)
        except ValueError:
            mode = None
        if mode is None or mode not in self._attr_hvac_modes:
            if payload not in self._ignored:
                self._ignored.add(payload)
                _LOGGER.warning("%s: mode payload %r means %r, which is not one of "
                                "its modes %s; ignored", self.entity_id, payload,
                                word_, [m.value for m in self._attr_hvac_modes])
            return None
        return mode

    def _restore(self, last) -> None:
        if last.state in self._attr_hvac_modes:
            self._mode = HVACMode(last.state)
        # A climate entity's attributes are in the unit the installation
        # displays, not the thermostat's own.
        shown = self.hass.config.units.temperature_unit
        for key, attr in ((ATTR_TARGET_TEMP_LOW, "_low"),
                          (ATTR_TARGET_TEMP_HIGH, "_high"),
                          (ATTR_TEMPERATURE, "_setpoint")):
            value = last.attributes.get(key)
            if value is not None:
                setattr(self, attr, TemperatureConverter.convert(
                    float(value), shown, UnitOfTemperature.CELSIUS))
        fan = last.attributes.get(ATTR_FAN_MODE)
        if fan is not None and (not self._fan_modes or fan in self._fan_modes):
            self._fan = fan
        # The measurement, through the same guard and the same unit conversion
        # as the setpoints above: it is an attribute in the shown unit, and
        # `sane` is in Celsius like every other range in the configuration.
        current = last.attributes.get(ATTR_CURRENT_TEMPERATURE)
        if current is not None:
            value = parse_bounded(
                str(TemperatureConverter.convert(
                    float(current), shown, UnitOfTemperature.CELSIUS)),
                self._sane)
            if value is not None:
                self._current = value
