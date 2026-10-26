"""i3 Atom: a controller's channels as Home Assistant entities.

WHY THIS EXISTS, and why it is not a package of `mqtt:` YAML.

The controller publishes a topic only when its value changes, and retains
nothing. Declared as MQTT entities, everything it exposes therefore has no
value after any restart and stays blank until something moves - indefinitely,
for anything nobody touches.

Blank is not merely untidy. Home Assistant's toggle treats `unknown` as ON, so
the first press on a blank switchable entity sends an off command to something
already off, appears to do nothing, and only the second press works.

An entity here restores its last known value instead, so there is no `unknown`
to be misread. Nothing is mirrored onto other topics, and nothing is written
into the controller's own tree unless `reannounce` is switched on.

A restored value is LAST KNOWN, not confirmed: something operated locally while
Home Assistant was down reads stale until that topic next changes. Better than
blank, which is both wrong and mishandled, but not the same as true - so a
controller restart is logged, including one that happened while Home Assistant
was not running.

WHAT IS AND IS NOT IN HERE. This module and `model.py` know about controllers,
channels, topic leaves and entity registry identity. They do not know what a
light is. An entity declares the platform it is presented as and the leaves it
binds; a small adapter per platform - `light.py`, `binary_sensor.py` - turns
those leaves into that domain's contract. The same relay is a lamp in one
installation and a pump in another, so the domain is a configuration decision
and never an inference from the channel type.

Configuration is YAML, which suits an installation with a long entity list,
often generated rather than written by hand. The YAML is imported into a single
config entry, which holds nothing of its own: it exists because Home Assistant
attaches devices only to entities that belong to an entry, and because an
entry can be reloaded. `i3_atom.reload` re-reads the YAML and reloads it.

With `group_by_controller`, each controller is a device and every entity
joins its controller's. Off by default, because Home Assistant then puts the
device's name in front of each entity's own - every entity is renamed.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import timedelta

from homeassistant.components import mqtt
from homeassistant.components.mqtt.const import (
    CONF_DISCOVERY_PREFIX,
    DEFAULT_DISCOVERY,
    DEFAULT_PREFIX,
)
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    CONF_DISCOVERY,
    CONF_ICON,
    CONF_ID,
    CONF_NAME,
    CONF_PLATFORM,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_STOP,
    SERVICE_RELOAD,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.reload import async_integration_yaml_config
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ALIASES,
    CONF_AREA,
    CONF_CLIENT_ID,
    CONF_CONTROLLERS,
    CONF_ENTITIES,
    CONF_GROUP_BY_CONTROLLER,
    CONF_HEARTBEAT_TIMEOUT,
    CONF_OBJECT_ID,
    CONF_OPTIONS,
    CONF_READS,
    CONF_REANNOUNCE,
    CONF_SUPPRESS_VENDOR_DISCOVERY,
    CONF_TRANSLATION_KEY,
    CONF_WRITES,
    CONFIG_SCHEMA,  # noqa: F401  - Home Assistant reads this off the module
    DOMAIN,
)
from .model import (
    ControllerHealth,
    EntitySpec,
    Owned,
    command_topic,
    controller_of,
    discovery_topics,
    event_topic,
    heartbeat_topic,
    merge_specs,
)

_LOGGER = logging.getLogger(__name__)

#: The validated YAML block, kept between `async_setup` and the entry's setup,
#: and replaced by `i3_atom.reload`.
DATA_YAML = f"{DOMAIN}_yaml"
#: The store for controller boot times. One instance however often the entry
#: reloads, so a delayed write from the last run is never raced by a load.
DATA_STORE = f"{DOMAIN}_store"
STORAGE_KEY = f"{DOMAIN}.controllers"
STORAGE_VERSION = 1

#: How often to re-ask whether each controller is still live.
#:
#: Everything else here is driven by arriving messages, and the one condition a
#: heartbeat timeout detects - a controller that has gone quiet - produces no
#: message. Without a clock nothing re-asks, so an entity keeps whatever state
#: it was last written with: a controller that has gone silent still reads
#: `on`, with every entity on it still showing pre-silence values and still
#: claiming to be available.
#:
#: Well under the 180 s floor on a heartbeat timeout, so the detection is
#: bounded by the timeout rather than by this, and it costs one comparison per
#: controller per tick - a state is written only when liveness actually flips.
LIVENESS_TICK = timedelta(seconds=30)

#: How often the remembered boot times are refreshed on disk. They are also
#: written whenever a controller is first heard from or restarts; this only
#: keeps the controller's clock drift out of the comparison after a long run.
MEMORY_REFRESH = timedelta(hours=1)

#: Between deciding to ask for a re-announce and asking. Home Assistant batches
#: subscriptions, so a request sent the moment the integration subscribes, or
#: the moment the broker reconnects, can be answered before anything listens.
REANNOUNCE_DELAY_S = 2.0

#: The clocks, as functions so that tests can move them. Liveness is judged on
#: the monotonic one; boot times survive a restart, so they use the wall clock.
_now = time.monotonic
_wall = time.time

type I3ConfigEntry = ConfigEntry[Hub]


class Hub:
    """Subscriptions, last-seen payloads, controller liveness, commands.

    Entities read from here rather than holding their own subscriptions: one
    leaf can back several entities, and a leaf should be subscribed once
    however many ways the configuration refers to it.

    An entity is told two different things, and the difference matters. A
    point it reads has a new payload: it re-reads its state. Or the
    availability of a controller it depends on may have changed: it only
    writes. Heartbeats are neither - they arrive every minute and change
    nothing an entity shows, and re-reading on one put the pre-command payload
    back over a value that had just been commanded.
    """

    def __init__(self, hass: HomeAssistant, conf: ConfigType, store: Store) -> None:
        self.hass = hass
        self.client_id: str = conf[CONF_CLIENT_ID]
        self.suppress_discovery: bool = conf[CONF_SUPPRESS_VENDOR_DISCOVERY]
        self.reannounce: bool = conf[CONF_REANNOUNCE]
        self.group_by_controller: bool = conf[CONF_GROUP_BY_CONTROLLER]
        self.specs: dict[str, EntitySpec] = {}
        # Every declared controller up front, so its configured timeout holds;
        # `add` creates any other an entity names, on the default.
        self.controllers: dict[str, ControllerHealth] = {
            c: ControllerHealth(c, timeout_s=t)
            for c, t in _heartbeat_timeouts(conf[CONF_CONTROLLERS]).items()}
        self.payloads: dict[str, str] = {}
        self.owned = Owned()
        self._subscribers: dict[str, set[str]] = {}
        self._listeners: dict[str, list[Callable[[str | None], None]]] = {}
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._store = store
        self._memory: dict[str, float] = {}
        self._reannounce_queue: set[str] = set()
        self._reannounce_handle: CALLBACK_TYPE | None = None
        self._stopped = False

    # ── wiring ─────────────────────────────────────────────────────────────

    def add(self, spec: EntitySpec) -> None:
        spec.validate()
        self.specs[spec.unique_id] = spec
        for controller in spec.controllers():
            self.controllers.setdefault(controller, ControllerHealth(controller))
        for point in spec.reads.values():
            self._subscribers.setdefault(point, set()).add(spec.unique_id)

    def platforms(self) -> list[str]:
        return sorted({s.platform for s in self.specs.values()})

    def specs_for(self, platform: str) -> list[EntitySpec]:
        return [s for s in self.specs.values() if s.platform == platform]

    def listen(self, unique_id: str, cb: Callable[[str | None], None]) -> CALLBACK_TYPE:
        """Call `cb(point)` when a point the entity reads changes, `cb(None)`
        when its availability may have. Returns the way to stop."""
        listeners = self._listeners.setdefault(unique_id, [])
        listeners.append(cb)

        @callback
        def remove() -> None:
            if cb in listeners:
                listeners.remove(cb)

        return remove

    def device_info(self, controller: str) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, controller)},
            name=f"i3 {controller}",
            manufacturer="i3 Engineering",
            model="Atom",
        )

    async def async_start(self) -> None:
        self.owned = Owned(frozenset(self.specs))
        self._memory = {
            controller: float(boot)
            for controller, boot in ((await self._store.async_load()) or {}).items()
            if isinstance(boot, (int, float))
        }
        for controller, health in self.controllers.items():
            health.remembered_boot = self._memory.get(controller)

        for point in self._subscribers:
            self._unsubscribe.append(
                await mqtt.async_subscribe(
                    self.hass, event_topic(self.client_id, point),
                    self._make_point_handler(point), qos=0)
            )
        for controller in self.controllers:
            self._unsubscribe.append(
                await mqtt.async_subscribe(
                    self.hass, heartbeat_topic(self.client_id, controller),
                    self._make_heartbeat_handler(controller), qos=0)
            )
        prefix = self._watched_discovery_prefix()
        if prefix is not None:
            for topic in discovery_topics(prefix):
                self._unsubscribe.append(
                    await mqtt.async_subscribe(
                        self.hass, topic, self._on_vendor_discovery, qos=1)
                )
        self._unsubscribe.append(
            async_track_time_interval(self.hass, self._check_liveness, LIVENESS_TICK))
        self._unsubscribe.append(
            async_track_time_interval(self.hass, self._save_memory_soon, MEMORY_REFRESH))
        if self.reannounce:
            self._unsubscribe.append(
                mqtt.async_subscribe_connection_status(self.hass, self._on_connection))
            if mqtt.is_connected(self.hass):
                self._request_reannounce(self.controllers)

    async def async_stop(self) -> None:
        """Stop listening and remember the controllers' boot times.

        Called on unload and when Home Assistant stops, whichever comes first.
        """
        if self._stopped:
            return
        self._stopped = True
        for unsub in self._unsubscribe:
            unsub()
        self._unsubscribe.clear()
        if self._reannounce_handle is not None:
            self._reannounce_handle()
            self._reannounce_handle = None
        await self._store.async_save(self._memory_snapshot())

    # ── incoming ───────────────────────────────────────────────────────────

    def _make_point_handler(self, point: str):
        @callback
        def handle(msg) -> None:
            payload = _as_text(msg.payload)
            if not payload:
                # A zero-length publish asks the controller to re-announce -
                # ours, with `reannounce` on, or anybody's. It carries no value,
                # and caching it would put nothing in place of the last real
                # one: a thermostat that reads its mode from whether a setpoint
                # is present would switch itself off on every request.
                return
            self.payloads[point] = payload
            for unique_id in self._subscribers.get(point, ()):
                self._notify(unique_id, point)

        return handle

    def _make_heartbeat_handler(self, controller: str):
        @callback
        def handle(msg) -> None:
            health = self.controllers[controller]
            first = health.last_uptime is None
            restarted = health.saw_heartbeat(_as_text(msg.payload), _now(), _wall())
            if restarted:
                # Logged rather than acted on: a restored value is still the
                # best answer available, and discarding it would put entities
                # back in exactly the blank state this integration removes.
                _LOGGER.warning(
                    "controller %s restarted%s; values on it are last known, not "
                    "confirmed, until each topic next changes", controller,
                    " while Home Assistant was not running" if first else "")
                if self.reannounce:
                    self._request_reannounce([controller])
            if restarted or (first and health.last_uptime is not None):
                # The boot time moved: remember it, and let the liveness
                # entity show it.
                self._save_memory_soon()
                self._notify_controller(controller)
            self._check_controller(controller, _now())

        return handle

    @callback
    def _check_liveness(self, _now_dt=None) -> None:
        """Re-ask the question nothing else will ask. See LIVENESS_TICK."""
        now = _now()
        for controller in self.controllers:
            self._check_controller(controller, now)

    @callback
    def _check_controller(self, controller: str, now: float) -> None:
        health = self.controllers[controller]
        was = health.reported
        if not health.liveness_changed(now):
            return
        if health.reported and was is False:
            _LOGGER.info("controller %s is live again", controller)
        elif not health.reported:
            _LOGGER.warning(
                "controller %s has sent no heartbeat for more than %ss; every "
                "entity on it is now unavailable and the values they were "
                "showing were from before it went quiet",
                controller, health.timeout_s)
        self._notify_controller(controller)

    def _watched_discovery_prefix(self) -> str | None:
        """Where duplicates of our entities could come from, or None.

        A duplicate is made by Home Assistant's own MQTT integration, from a
        discovery config on the prefix it listens on. So that prefix, read
        from its entry, is the only one worth watching - and when it consumes
        no discovery at all, nothing can be duplicated and nothing is watched.

        What the controller's app is set to does not need saying here. A
        controller publishing elsewhere, or not at all, sends nothing to this
        prefix, so nothing matches and nothing is deleted; one publishing here
        is exactly the case this exists for. A setting of our own could only
        repeat Home Assistant's, or get it wrong.

        Read once, at start: after changing the MQTT integration's discovery
        settings, reload this integration too.
        """
        if not self.suppress_discovery:
            return None
        entries = self.hass.config_entries.async_entries(mqtt.DOMAIN)
        if not entries:
            return None
        conf = {**entries[0].data, **entries[0].options}
        if not conf.get(CONF_DISCOVERY, DEFAULT_DISCOVERY):
            _LOGGER.debug("MQTT discovery is off; no duplicates to suppress")
            return None
        return conf.get(CONF_DISCOVERY_PREFIX, DEFAULT_PREFIX)

    @callback
    def _on_vendor_discovery(self, msg) -> None:
        raw = _as_text(msg.payload)
        if not raw:
            return                      # already removed; do not answer a delete
        try:
            payload = json.loads(raw)
        except ValueError:
            return                      # not ours to interpret, let alone delete
        if not self.owned.should_delete_discovery(payload):
            return
        _LOGGER.info("removing the controller's duplicate discovery at %s", msg.topic)
        self.hass.async_create_task(
            mqtt.async_publish(self.hass, msg.topic, "", qos=1, retain=True))

    def _notify(self, unique_id: str, point: str | None) -> None:
        for cb in list(self._listeners.get(unique_id, ())):
            cb(point)

    def _notify_controller(self, controller: str) -> None:
        for unique_id, spec in self.specs.items():
            if controller in spec.controllers():
                self._notify(unique_id, None)

    # ── re-announce ────────────────────────────────────────────────────────

    @callback
    def _on_connection(self, connected: bool) -> None:
        # Anything may have changed while the broker was unreachable, and the
        # controller will not say so until it changes again.
        if connected:
            self._request_reannounce(self.controllers)

    @callback
    def _request_reannounce(self, controllers) -> None:
        """Ask the controller to publish the current value of every bound leaf.

        A zero-length publish to a leaf's event topic is how the controller is
        asked to re-announce it. That turns a restored, last-known value into a
        confirmed one within a tick, after a start, after a broker reconnect
        and after the controller itself restarts. It is also the only thing
        here that writes into the controller's tree, which is why it is opt-in.
        """
        self._reannounce_queue.update(controllers)
        if self._reannounce_handle is None:
            self._reannounce_handle = async_call_later(
                self.hass, REANNOUNCE_DELAY_S, self._reannounce)

    async def _reannounce(self, _now_dt=None) -> None:
        self._reannounce_handle = None
        controllers, self._reannounce_queue = self._reannounce_queue, set()
        points = [p for p in self._subscribers if controller_of(p) in controllers]
        for point in points:
            try:
                await mqtt.async_publish(
                    self.hass, event_topic(self.client_id, point), "", qos=0, retain=False)
            except HomeAssistantError as exc:
                _LOGGER.debug("re-announce request not sent: %s", exc)
                return
        _LOGGER.debug("asked %s to re-announce %d leaves",
                      ", ".join(sorted(controllers)), len(points))

    # ── boot memory ────────────────────────────────────────────────────────

    def _memory_snapshot(self) -> dict[str, float]:
        for controller, health in self.controllers.items():
            boot = health.boot_estimate()
            if boot is not None:
                self._memory[controller] = boot
        return dict(self._memory)

    @callback
    def _save_memory_soon(self, _now_dt=None) -> None:
        self._store.async_delay_save(self._memory_snapshot, 10)

    # ── asked by entities ──────────────────────────────────────────────────

    def online(self, spec: EntitySpec) -> bool:
        """Whether every controller this entity depends on is live.

        The answer for availability. "Is that one controller up" is a
        different question and is asked by name: see `controller_online`.
        """
        now = _now()
        return all(self.controllers[c].online(now) for c in spec.controllers()
                   if c in self.controllers)

    def controller_online(self, controller: str) -> bool | None:
        health = self.controllers.get(controller)
        return None if health is None else health.online(_now())

    def publish(self, spec: EntitySpec, role: str, payload: str):
        point = spec.writes.get(role)
        if point is None:
            raise KeyError(f"{spec.unique_id} has no write role {role!r}")
        return mqtt.async_publish(
            self.hass, command_topic(self.client_id, point), payload,
            qos=0, retain=False)


def _heartbeat_timeouts(controllers: list[dict]) -> dict[str, int]:
    """One heartbeat timeout per declared controller.

    Home Assistant merges this block across packages, so a controller that
    several of them touch is listed several times. Agreeing duplicates are the
    normal case and collapse silently; a disagreement is a configuration fault
    and is said out loud, because the alternative is whichever package loaded
    last quietly setting the availability timeout for the whole site.
    """
    timeouts: dict[str, int] = {}
    for c in controllers:
        cid, timeout = c[CONF_ID], c[CONF_HEARTBEAT_TIMEOUT]
        previous = timeouts.get(cid)
        if previous is not None and previous != timeout:
            _LOGGER.error(
                "controller %s is declared with two heartbeat timeouts, %s and "
                "%s; using %s", cid, previous, timeout, max(previous, timeout))
            timeout = max(previous, timeout)
        timeouts[cid] = timeout
    return timeouts


def _as_text(payload) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace").strip()
    return str(payload).strip()


def _specs(conf: ConfigType) -> list[EntitySpec]:
    specs, problems = merge_specs([
        EntitySpec(
            platform=raw[CONF_PLATFORM],
            unique_id=raw[CONF_UNIQUE_ID],
            object_id=raw[CONF_OBJECT_ID],
            name=raw[CONF_NAME],
            reads=raw[CONF_READS],
            writes=raw[CONF_WRITES],
            options=raw[CONF_OPTIONS],
            icon=raw.get(CONF_ICON),
            area=raw.get(CONF_AREA),
            aliases=tuple(raw.get(CONF_ALIASES) or ()),
            translation_key=raw.get(CONF_TRANSLATION_KEY),
        )
        for raw in conf[CONF_ENTITIES]
    ])
    for problem in problems:
        _LOGGER.error(problem)
    return specs


@callback
def _async_import(hass: HomeAssistant) -> None:
    """Create the config entry for the YAML block, once."""
    if not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data={}))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.data[DATA_YAML] = config.get(DOMAIN)

    async def _reload(call: ServiceCall) -> None:
        reloaded = await async_integration_yaml_config(hass, DOMAIN)
        if reloaded is None:
            return                      # invalid; Home Assistant has said why
        hass.data[DATA_YAML] = reloaded.get(DOMAIN)
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            if hass.data[DATA_YAML]:
                _async_import(hass)
            return
        for entry in entries:
            await hass.config_entries.async_reload(entry.entry_id)

    async_register_admin_service(hass, DOMAIN, SERVICE_RELOAD, _reload)

    if hass.data[DATA_YAML]:
        _async_import(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: I3ConfigEntry) -> bool:
    conf = hass.data.get(DATA_YAML)
    if not conf:
        # Not removed automatically: deleting the entry deletes every entity
        # row with it, and a package that failed to load for one restart is
        # not a reason to lose the registry.
        raise ConfigEntryError(
            "configuration.yaml has no i3_atom: block; add it back, or delete "
            "this entry to remove the integration")
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("the MQTT integration is not available")

    store = hass.data.get(DATA_STORE)
    if store is None:
        store = hass.data[DATA_STORE] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    hub = Hub(hass, conf, store)
    for spec in _specs(conf):
        hub.add(spec)
    await hub.async_start()
    entry.runtime_data = hub

    async def _on_stop(_event: Event) -> None:
        await hub.async_stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))

    # Only the platforms this configuration actually uses. A site with no
    # dimmers loads no light platform.
    await hass.config_entries.async_forward_entry_setups(entry, hub.platforms())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: I3ConfigEntry) -> bool:
    hub = entry.runtime_data
    unloaded = await hass.config_entries.async_unload_platforms(entry, hub.platforms())
    if unloaded:
        await hub.async_stop()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: I3ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """A controller's device can be deleted once the configuration no longer
    names that controller; while it does, the next reload would bring it back."""
    hub = entry.runtime_data
    return not any(domain == DOMAIN and ident in hub.controllers
                   for domain, ident in device.identifiers)
