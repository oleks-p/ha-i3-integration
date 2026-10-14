"""Binary sensor adapter.

Two shapes, told apart by whether the entity binds a point at all:

  * a CONTROLLER entity, declared with no reads and no writes and
    `options.controller` naming one. Its state is that controller's liveness,
    taken from the uptime counter rather than from the per-channel
    availability topics, which are published but never retained.
  * a POINT entity, which reads `state` and reports non-zero as on.

Roles:

    reads.state      the value, for a point entity

Options:

    controller       makes this a controller-liveness entity
    device_class     passed through
    invert           report zero as on
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    DEVICE_CLASSES_SCHEMA,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import I3ConfigEntry
from .const import options_schema, vol
from .entity import I3Entity, async_add_adapter_entities
from .model import parse_number


async def async_setup_entry(
    hass: HomeAssistant,
    entry: I3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    def build(hub, spec, *, claimed):
        cls = I3ControllerOnline if spec.options.get("controller") else I3BinarySensor
        return cls(hub, spec, claimed=claimed)

    async_add_adapter_entities(hass, entry.runtime_data, "binary_sensor", build,
                               async_add_entities)


class I3BinarySensor(I3Entity, BinarySensorEntity):
    OPTIONS = options_schema({
        vol.Optional("device_class"): DEVICE_CLASSES_SCHEMA,
        vol.Optional("invert", default=False): cv.boolean,
    })

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        self._attr_device_class = self._options.get("device_class")
        self._invert = self._options["invert"]
        self._is_on: bool | None = None

    @property
    def is_on(self) -> bool | None:
        return self._is_on

    def _read_points(self) -> None:
        value = parse_number(self._payload("state") or "")
        if value is None:
            return
        self._is_on = (value <= 0) if self._invert else (value > 0)

    def _restore(self, last) -> None:
        self._is_on = last.state == "on"


class I3ControllerOnline(I3Entity, BinarySensorEntity):
    """Whether a controller has been heard from inside its heartbeat timeout.

    Every entity on that controller takes its availability from the same
    object, so this adds no judgement - it makes the judgement readable, which
    matters because a controller that has gone quiet explains every unresponsive
    entity on it at once.

    It shows when the controller booted rather than its uptime counter. The
    counter changes with every heartbeat, which made this a new state - and a
    new recorder row - every minute for every controller; the boot time
    changes only when the controller reboots, and the uptime is one
    subtraction away.
    """

    OPTIONS = options_schema({vol.Required("controller"): cv.string})

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub, spec, *, claimed: bool = True) -> None:
        super().__init__(hub, spec, claimed=claimed)
        self._controller = self._options["controller"]

    @property
    def available(self) -> bool:
        # Reporting a liveness entity as unavailable because the thing it
        # reports on is unavailable says nothing twice and hides the answer.
        return True

    @property
    def is_on(self) -> bool | None:
        # Asked by name: the question is whether this one controller is up,
        # not whether everything the entity depends on is.
        return self._hub.controller_online(self._controller)

    @property
    def extra_state_attributes(self) -> dict:
        health = self._hub.controllers.get(self._controller)
        if health is None:
            return {}
        booted = (None if health.booted_at is None
                  else dt_util.utc_from_timestamp(round(health.booted_at)))
        return {"booted_at": booted, "heartbeat_timeout_s": health.timeout_s}

    # No points and nothing worth restoring: liveness is a property of the
    # hub, and the controller's heartbeat is what wakes this entity.
    def _read_points(self) -> None:
        return

    def _restore(self, last) -> None:
        return
