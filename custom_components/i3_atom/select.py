"""Select adapter: one leaf whose value is one of a declared set of words.

Roles:

    reads.state      the current value
    writes.command   the new value

Options:

    options     the words offered, in order
    map         payload -> word, used in both directions. Every word it gives
                must be one of `options`.

A word the map does not know is not reported. The alternative - showing the raw
payload - puts a number in a list of names and makes the control unusable,
because the number is not one of the options and Home Assistant will not select
it.
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import I3ConfigEntry
from .const import options_schema, payload_map, vol, word
from .entity import I3Entity, async_add_adapter_entities
from .model import decode, encode

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: I3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_adapter_entities(hass, entry.runtime_data, "select", I3Select,
                               async_add_entities)


def _map_within_options(opts: dict) -> dict:
    stray = set((opts.get("map") or {}).values()) - set(opts["options"])
    if stray:
        raise vol.Invalid(f"map gives {sorted(stray)}, which are not among the options")
    return opts


class I3Select(I3Entity, SelectEntity):
    OPTIONS = vol.All(
        options_schema({
            vol.Required("options"): vol.All([word], vol.Length(min=1)),
            vol.Optional("map"): payload_map(),
        }),
        _map_within_options,
    )

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        self._attr_options = list(self._options["options"])
        self._map: dict[str, str] = self._options.get("map") or {}
        self._current: str | None = None

    @property
    def current_option(self) -> str | None:
        return self._current

    async def async_select_option(self, option: str) -> None:
        payload = encode(option, self._map)
        if payload is None:
            raise ServiceValidationError(f"{self.entity_id}: no payload maps to {option!r}")
        self._current = option
        await self._command("command", payload)

    def _read_points(self) -> None:
        payload = self._payload("state")
        if payload is None:
            return
        word_ = decode(payload, self._map)
        if word_ is None or word_ not in self._attr_options:
            _LOGGER.debug("%s: payload %r maps to no option", self.entity_id, payload)
            return
        self._current = word_

    def _restore(self, last) -> None:
        if last.state in self._attr_options:
            self._current = last.state
