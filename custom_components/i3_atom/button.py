"""Button adapter: a momentary output, pressed and then forgotten.

Roles:

    writes.command   the pulse

Options:

    payload          what to publish (default "1")
    device_class     `identify`, `restart` or `update`

An `out_pulse` channel closes a contact for a width configured in the
controller and opens it again by itself. Nothing is held and nothing is read
back: a gate's own controller decides what a pulse means, and the only thing
this end knows is that one was sent.

That is why it is a button and not a switch. A switch offers a state to set,
and a switch with no read answers with whatever it was last told - so a gate
opened from a car's remote would show as shut for as long as nobody touched
the interface, which is worse than showing nothing. A button claims nothing
between presses, and its state - the time of the last press - is the one fact
this integration actually has.

This adapter has no reads, and so nothing to restore: `async_press` is the
whole contract and Home Assistant keeps the timestamp of the last one.
"""
from __future__ import annotations

from homeassistant.components.button import DEVICE_CLASSES_SCHEMA, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import I3ConfigEntry
from .const import options_schema, vol
from .entity import I3Entity, async_add_adapter_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: I3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_adapter_entities(hass, entry.runtime_data, "button", I3Button,
                               async_add_entities)


class I3Button(I3Entity, ButtonEntity):
    OPTIONS = options_schema({
        vol.Optional("payload", default="1"): cv.string,
        vol.Optional("device_class"): DEVICE_CLASSES_SCHEMA,
    })

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        self._attr_device_class = self._options.get("device_class")
        # NOT `self._payload`: the base class has a method of that name, and
        # an attribute would shadow it for every other entity behaviour that
        # reads a role.
        self._press_payload = self._options["payload"]

    async def async_press(self) -> None:
        await self._command("command", self._press_payload)

    # A press is not a state, so there is neither a point to read nor a value
    # to bring back. Both are still defined: the base class raises
    # NotImplementedError, and it is called on every start.
    def _read_points(self) -> None:
        return

    def _restore(self, last) -> None:
        return
