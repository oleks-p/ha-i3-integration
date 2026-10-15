"""What every entity gets, whatever domain it is presented as.

Availability, the restore-on-start behaviour, the echo window after a command,
its device, and taking back the entity_id a previous declaration is holding. A
platform adapter subclasses this and adds only its own domain's contract.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, options_schema, vol
from .model import ASSUMED_WINDOW_S, EntitySpec, RegistryRow, channel_of, plan_entity_id_claims

if TYPE_CHECKING:
    from . import Hub

_LOGGER = logging.getLogger(__name__)


def claim_entity_ids(hass: HomeAssistant, specs: list[EntitySpec]) -> set[str]:
    """Free the entity_id each spec is configured to own, before any is created.

    Home Assistant's id generator avoids a collision rather than resolving one,
    and the row holding the id is usually this very point's previous
    declaration. The decision about which row belongs to which point is in
    `model.plan_entity_id_claims` and tested there; this applies it.

    Returns the entity_ids that could NOT be claimed, so an entity wanting one
    can fall back to generated naming instead of colliding on creation.
    """
    registry = er.async_get(hass)
    domains = {s.platform for s in specs}
    rows = [
        RegistryRow(e.entity_id, e.platform, e.unique_id)
        for e in registry.entities.values()
        if e.domain in domains and e.unique_id
    ]
    blocked: set[str] = set()
    for claim in plan_entity_id_claims(specs, rows, DOMAIN):
        if claim.action == "remove":
            _LOGGER.info("releasing %s (%s)", claim.entity_id, claim.reason)
            registry.async_remove(claim.entity_id)
        elif claim.action == "rename":
            _LOGGER.info("renaming %s -> %s (%s)", claim.entity_id,
                         claim.new_entity_id, claim.reason)
            registry.async_update_entity(claim.entity_id,
                                         new_entity_id=claim.new_entity_id)
        else:
            _LOGGER.error(
                "cannot take %s: %s. That entity belongs to something else, so "
                "it is left alone and this one gets a generated id - resolve "
                "the name clash in the configuration", claim.entity_id, claim.reason)
            blocked.add(claim.entity_id)
    return blocked


def async_add_adapter_entities(
    hass: HomeAssistant,
    hub: Hub,
    platform: str,
    build: Callable[..., I3Entity],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Claim the entity_ids for one platform and add its entities.

    An entity whose options do not validate is left out and named in the log.
    The alternative - raising out of the constructor - took every entity of
    the platform down with it, a controller-liveness sensor included, for one
    misspelt device class.
    """
    specs = hub.specs_for(platform)
    blocked = claim_entity_ids(hass, specs)
    entities = []
    for spec in specs:
        claimed = f"{platform}.{spec.object_id}" not in blocked
        try:
            entities.append(build(hub, spec, claimed=claimed))
        except (vol.Invalid, ValueError) as exc:
            _LOGGER.error("%s is not created: its options are invalid: %s",
                          spec.unique_id, exc)
    async_add_entities(entities)


class I3Entity(RestoreEntity):
    """Base for every entity this integration owns."""

    _attr_should_poll = False
    _attr_has_entity_name = False

    #: The options this platform accepts. Validated before anything else is
    #: built from them; see async_add_adapter_entities for what a failure does.
    OPTIONS = options_schema({})

    def __init__(self, hub: Hub, spec: EntitySpec, *, claimed: bool = True) -> None:
        self._options: dict = self.OPTIONS(spec.options)
        self._hub = hub
        self._spec = spec
        self._attr_unique_id = spec.unique_id
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        # On its controller's device only when asked: Home Assistant names an
        # entity that has a device after the device as well, so joining one
        # renames the entity everywhere its name is shown.
        controller = spec.primary_controller()
        if controller is not None and hub.group_by_controller:
            self._attr_device_info = hub.device_info(controller)
        # Set directly rather than generated: the id is named by the
        # configuration because other things reference it by name, and the case
        # where it is already taken is the case that matters - the row holding
        # it is this point's own previous declaration, which claim_entity_ids
        # has already released. Where it could not be released the id belongs to
        # something else, and generated naming is the honest fallback.
        if claimed:
            self.entity_id = f"{spec.platform}.{spec.object_id}"
        #: Channels with a command sent and no echo yet. The echo is the
        #: controller's publish tick rather than network jitter, and an entity
        #: that sits inert for it reads as a dropped press - which gets pressed
        #: again, and on a toggle that undoes the first one.
        self._pending: set[str] = set()
        self._expire_assumption: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._apply_registry_facts()
        self.async_on_remove(self._hub.listen(self._spec.unique_id, self._hub_changed))
        self.async_on_remove(self._cancel_assumption)
        # Restore first, then let any live payload replace it. Subscriptions
        # are made before platforms load, so a topic may already have arrived;
        # but a payload that is only silence (`-nan`) must not stand in for the
        # remembered value - deciding between them once, up front, left an
        # entity `unknown` whenever the first thing heard was silence.
        await self._async_restore()
        self._read_points()

    def _apply_registry_facts(self) -> None:
        """Put the configured area and aliases on this entity's registry row.

        The area and the voice aliases live in the entity REGISTRY, not on the
        entity, and no YAML platform can set either - which is why they used to
        be applied afterwards by a separate tool talking to the websocket API.
        That made a rebuild a four-step procedure in which the fourth step was
        the one everybody forgets, and an installation that skipped it had
        every entity in no area at all.

        The configuration is the source of truth, exactly as it is for the
        name: a value that differs is written. The cost is that a change made
        by hand in the interface is put back on the next start, which is the
        same bargain the entity's name has always made here.

        The area is named rather than identified, because a name is what a
        person writes; one that does not exist yet is created, which is what
        the tool this replaces did too.
        """
        entry = self.registry_entry
        if entry is None:
            return
        registry = er.async_get(self.hass)
        fields: dict = {}

        want_aliases = set(self._spec.aliases)
        if want_aliases and set(entry.aliases) != want_aliases:
            fields["aliases"] = want_aliases

        if self._spec.area:
            areas = ar.async_get(self.hass)
            area = areas.async_get_area_by_name(self._spec.area)
            if area is None:
                area = areas.async_create(self._spec.area)
                _LOGGER.info("created area %r for %s",
                             self._spec.area, entry.entity_id)
            if entry.area_id != area.id:
                fields["area_id"] = area.id

        if fields:
            registry.async_update_entity(entry.entity_id, **fields)

    @property
    def available(self) -> bool:
        """Follows the controller's heartbeat, not the point.

        The per-channel availability topics are published but not retained, so
        binding one leaves an entity unavailable until the next publish - the
        same trap in a different place. The uptime counter is published
        unconditionally, so a controller that has gone quiet past its timeout
        is the honest signal.
        """
        return self._hub.online(self._spec)

    @property
    def assumed_state(self) -> bool:
        """True only while a command is outstanding.

        Not a standing flag: a permanently assumed entity gets a split
        command pair in the UI instead of one control, which is the wrong
        affordance for something whose value is genuinely known the rest of
        the time.
        """
        return bool(self._pending)

    # ── for the platform adapter ────────────────────────────────────────────

    async def _command(self, role: str, payload: str) -> None:
        """Publish on a write role, showing the commanded value until the echo.

        The adapter sets its own state to the commanded value first. Until the
        channel answers, a payload already cached for it is the value from
        before the command and is not read back over it; when the window
        closes without an answer, it is - see model.ASSUMED_WINDOW_S.
        """
        point = self._spec.writes.get(role)
        if point is None:
            raise ServiceValidationError(f"{self.entity_id} has no {role!r} to write")
        self._pending.add(channel_of(point))
        self._cancel_assumption()
        self._expire_assumption = async_call_later(
            self.hass, ASSUMED_WINDOW_S, self._assumption_expired)
        self.async_write_ha_state()
        await self._hub.publish(self._spec, role, payload)

    def _payload(self, role: str) -> str | None:
        """The last payload of a read role, or None if there is nothing new.

        A role on a channel with a command outstanding has nothing new: its
        cached payload predates the command.
        """
        point = self._spec.reads.get(role)
        if point is None or channel_of(point) in self._pending:
            return None
        return self._hub.payloads.get(point)

    def _read_points(self) -> None:
        """Take state from the last payloads. Overridden per platform."""
        raise NotImplementedError

    def _restore(self, last) -> None:
        """Take state from the restored state. Overridden per platform."""
        raise NotImplementedError

    async def _async_restore(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self._restore(last)

    # ── from the hub ───────────────────────────────────────────────────────

    @callback
    def _hub_changed(self, point: str | None) -> None:
        # An entity that failed to be added can still be listening, and
        # writing state from one raises on every incoming message - which
        # buries the original failure under a traceback per publish.
        if self.hass is None or self.entity_id is None:
            return
        if point is not None:
            # The channel has spoken: whatever was assumed for it is now
            # confirmed or contradicted, and the payload is the answer.
            self._pending.discard(channel_of(point))
            if not self._pending:
                self._cancel_assumption()
            self._read_points()
        self.async_write_ha_state()

    @callback
    def _assumption_expired(self, _now) -> None:
        self._expire_assumption = None
        self._pending.clear()
        self._read_points()
        self.async_write_ha_state()

    @callback
    def _cancel_assumption(self) -> None:
        if self._expire_assumption is not None:
            self._expire_assumption()
            self._expire_assumption = None
