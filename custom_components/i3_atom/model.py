"""The controller's behaviour, expressed without reference to any entity domain.

Nothing here knows what a light is. A controller exposes numbered channels;
each channel exposes one or more topic leaves; an entity is a named bundle of
leaves. Which Home Assistant domain a bundle becomes is a decision made in the
configuration, not here, because the same relay is a lamp in one installation
and a pump in another.

Kept separate from the Home Assistant glue so it can be tested on its own.

TOPIC GRAMMAR, verbatim - note the leading slash, which creates an empty first
topic level. The controller publishes that way, so it is reproduced exactly:

    /devices/<client>/events/<DEV>/<type>/<n>/<leaf>
    /devices/<client>/commands/<DEV>/<type>/<n>/<leaf>
    /devices/<client>/events/<DEV>/system/1/uptime

The leaf is NOT always `value`. Observed across one controller family:

    out, pwm, adc_tempr, mb_float, mb_u16, var_int   value
    twv                                              position
    comparator                                       value / set / enable
    comparator_v2      mode, setpoint_heating, setpoint_cooling, temp

So a channel is not the unit of subscription; a leaf is. An entity that binds
four leaves of one channel and an entity that binds one leaf are the same shape
to everything in this module.

WHAT EVERY LEAF HAS IN COMMON, and why this module exists:

* The controller publishes a leaf ONLY WHEN IT CHANGES, and retains nothing.
  After any restart Home Assistant therefore knows nothing until something
  moves, and a leaf nobody touches stays blank indefinitely.
* `unknown` is not merely untidy. Home Assistant's toggle treats it as ON, so
  the first press on a blank switchable entity sends an off command to
  something already off, appears to do nothing, and only the second press
  works. Restoring the last known value removes the ambiguity.
* A restored value is LAST KNOWN, not confirmed. If something was operated
  locally while Home Assistant was down, the value is wrong until that leaf
  next changes.
* Command to state echo is about one second, which is the controller's publish
  tick rather than network jitter. An entity that waits for it looks
  unresponsive, so the commanded value is shown immediately and corrected when
  the echo lands.
* Heartbeats arrive roughly every 60 s, round-robin, with a fixed phase offset
  per device, so an availability timeout below 180 s flaps.
* The per-channel `.../available` topics are published but NOT retained, so
  binding one leaves an entity unavailable until the next publish. Liveness
  comes from the uptime counter, which is published unconditionally.
* An unconfigured channel publishes `-nan`, not `nan`. An empty payload is
  also not a value: publishing nothing to a state topic is how a controller is
  asked to re-announce, and reading that as a number turns things on.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

#: What a leaf emits when it carries no value. The empty string is included
#: deliberately: a zero-length publish is a request to re-announce, not a zero.
NAN_TOKENS = ("", "nan", "-nan", "none", "null")

#: Below this the round-robin heartbeat flaps. A floor, not a suggestion.
MIN_HEARTBEAT_TIMEOUT_S = 180
DEFAULT_HEARTBEAT_TIMEOUT_S = 240

#: The controller's publish tick: how long a command's echo takes to land.
COMMAND_ECHO_S = 1.011

#: How long a commanded value is shown before the controller has confirmed it.
#:
#: Three publish ticks. An echo later than that is not coming, and the usual
#: reason is not a lost message: the controller publishes only on change, so
#: commanding the value a channel already holds produces no echo at all. When
#: the window closes the entity goes back to the last thing the controller
#: actually said, which for that case is the same value, and stops claiming to
#: be assumed.
ASSUMED_WINDOW_S = 3 * COMMAND_ECHO_S

#: Slack when judging, from an uptime counter and the wall clock, whether a
#: controller rebooted while Home Assistant was not running. Covers clock
#: adjustments and the counter's own drift between two Home Assistant runs.
RESTART_SLACK_S = 300

#: `<DEV>/<type>/<n>/<leaf>`, which is how a point is written in configuration.
#: A channel type can contain a hyphen and can begin with a digit - `0-10v` is
#: a real one - so the type is not an identifier and must not be matched as
#: though it were.
POINT = re.compile(r"^(?P<controller>[A-Za-z0-9]+)/(?P<channel>[a-z0-9_-]+)"
                   r"/(?P<number>\d+)/(?P<leaf>[a-z0-9_]+)$")


def topic_root(client_id: str) -> str:
    return f"/devices/{client_id}"


def event_topic(client_id: str, point: str) -> str:
    return f"{topic_root(client_id)}/events/{point}"


def command_topic(client_id: str, point: str) -> str:
    return f"{topic_root(client_id)}/commands/{point}"


def heartbeat_topic(client_id: str, controller: str) -> str:
    return f"{topic_root(client_id)}/events/{controller}/system/1/uptime"


def discovery_topics(prefix: str) -> tuple[str, str]:
    """Every MQTT discovery config topic under `prefix`.

    Discovery allows an optional node level - `<prefix>/<component>/[<node>/]
    <object>/config` - and one wildcard pattern cannot match both depths. A
    controller that announces with a node id would otherwise never be seen, and
    its duplicates would keep the entity_ids this integration is meant to own.
    """
    return (f"{prefix}/+/+/config", f"{prefix}/+/+/+/config")


def parse_point(point: str) -> tuple[str, str, int, str]:
    """`A1B2/out/9/value` -> ('A1B2', 'out', 9, 'value').

    The channel type is part of the address and is never inferred. Channel
    types are not interchangeable - a command sent to the wrong one is accepted
    by the broker and produces no echo and no effect - so a malformed point is
    refused rather than guessed at.
    """
    m = POINT.match(point)
    if not m:
        raise ValueError(f"not a point: {point!r}")
    return (m["controller"], m["channel"], int(m["number"]), m["leaf"])


def controller_of(point: str) -> str:
    return parse_point(point)[0]


def channel_of(point: str) -> str:
    """`A1B2/out/9/set` -> 'A1B2/out/9': the point without its leaf.

    A command and its echo are different leaves of one channel - a relay is
    commanded on `set` and answers on `value`, a comparator is switched on
    `enable` and answers on `value` - so the channel is what ties a command to
    the publish that confirms it, without knowing what either leaf means.
    """
    controller, channel, number, _ = parse_point(point)
    return f"{controller}/{channel}/{number}"


def is_blank(payload: str) -> bool:
    return payload.strip().lower() in NAN_TOKENS


def parse_number(payload: str) -> float | None:
    """A payload as a number, or None when it carries no value.

    None and 0 are different answers and callers must not conflate them: 0 is a
    reading, None is silence, and only silence should leave a restored value in
    place. Infinity is silence too: no channel holds it, and scaling it raises
    inside the MQTT callback instead of leaving the last value alone.
    """
    if is_blank(payload):
        return None
    try:
        value = float(payload)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def scale_to(value: float, src_max: float, dst_max: float) -> int:
    """Rescale a level between two ranges, rounding once.

    Rounding at both ends of a round trip walks a level by a point per
    operation, which over a session drifts away from what was set. Both
    directions therefore go through here.

    A non-zero level never rounds to zero. Zero means off on every channel this
    is used for, so the lowest brightness on a coarse dimmer would otherwise
    switch the light off, and a channel reading 1 of 1000 would show as a light
    that is on at no brightness at all.
    """
    if src_max <= 0:
        raise ValueError("source range must be positive")
    scaled = max(0, min(int(dst_max), round(value / src_max * dst_max)))
    return max(1, scaled) if value > 0 else scaled


def decode(payload: str, mapping: dict[str, str] | None) -> str | None:
    """A payload as one of a declared set of words.

    `mapping` is payload -> word, as configuration. An unmapped payload is
    None rather than a guess: a firmware that grows a fifth mode should leave
    the entity on its last known one, not silently report the nearest word.
    """
    if is_blank(payload):
        return None
    if not mapping:
        return payload.strip()
    return mapping.get(payload.strip())


def encode(word: str, mapping: dict[str, str] | None) -> str | None:
    """The reverse of `decode`, built from the same map so the two cannot drift."""
    if not mapping:
        return word
    for payload, mapped in mapping.items():
        if mapped == word:
            return payload
    return None


def parse_bounded(payload: str, sane: tuple[float, float] | None) -> float | None:
    """A number, or None if it is outside what the channel could really hold.

    The range check is not decoration. A channel that has never been configured
    publishes `-nan`, and on some channel types the same leaf carries both a
    value and the fact that there is none - so a payload outside what any
    thermostat or probe in the installation could report is data, not a
    reading. Without the guard it reaches the UI as a temperature of several
    thousand degrees or a dash, depending on which way the float cast failed.
    """
    value = parse_number(payload)
    if value is None:
        return None
    if sane is not None and not (sane[0] <= value <= sane[1]):
        return None
    return value


def format_number(value: float) -> str:
    """A number as the controller expects it: a plain decimal, no exponent."""
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def restarted_while_away(remembered_boot: float, uptime: float, wall: float,
                         slack: float = RESTART_SLACK_S) -> bool:
    """Whether a controller booted again while nothing was listening.

    Within one run a restart is the uptime counter going backwards. Across a
    Home Assistant restart there is no previous beat to compare with, so the
    first beat could never count as one - while a controller restarted during
    the outage is exactly the case whose values are most likely stale. What
    survives the outage is when the controller booted, by the wall clock; a
    boot time later than the remembered one is a reboot nobody saw.
    """
    return wall - uptime > remembered_boot + slack


@dataclass
class ControllerHealth:
    """Liveness for one controller, from its uptime counter."""

    controller: str
    timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S
    last_seen: float | None = None
    last_uptime: int | None = None
    #: What was last reported to Home Assistant, so a change can be told from
    #: a repeat. See `liveness_changed`.
    reported: bool | None = None
    #: When the controller booted, by the wall clock, as of the first beat
    #: after a start or a restart. It moves only when the controller reboots,
    #: so unlike the raw counter it can be shown without a new state every
    #: minute.
    booted_at: float | None = None
    #: The boot time remembered from before Home Assistant started, until the
    #: first beat has been judged against it. See `restarted_while_away`.
    remembered_boot: float | None = None
    #: The wall clock at the last numbered beat, for `boot_estimate`.
    last_wall: float | None = None

    def __post_init__(self) -> None:
        self.timeout_s = max(float(self.timeout_s), MIN_HEARTBEAT_TIMEOUT_S)

    def saw_heartbeat(self, payload: str, now: float, wall: float | None = None) -> bool:
        """Record a beat. True if the controller appears to have RESTARTED.

        Worth knowing, because every leaf on it now holds whatever it booted
        into and nothing will say so until it changes. The counter going
        backwards is the only announcement there is; `wall`, when given, also
        catches a reboot that happened while Home Assistant was not running.
        """
        self.last_seen = now
        value = parse_number(payload)
        if value is None:
            return False
        uptime = int(value)
        if self.last_uptime is not None:
            restarted = uptime < self.last_uptime
        elif self.remembered_boot is not None and wall is not None:
            restarted = restarted_while_away(self.remembered_boot, uptime, wall)
        else:
            restarted = False
        if wall is not None:
            if restarted or self.last_uptime is None:
                self.booted_at = wall - uptime
            self.last_wall = wall
        self.remembered_boot = None
        self.last_uptime = uptime
        return restarted

    def boot_estimate(self) -> float | None:
        """When the controller booted, from the latest beat: what to remember."""
        if self.last_uptime is None or self.last_wall is None:
            return None
        return self.last_wall - self.last_uptime

    def liveness_changed(self, now: float) -> bool:
        """Whether `online` has flipped since it was last reported.

        THE BUG THIS EXISTS FOR. Everything else here is driven by arriving
        messages, and the one condition a heartbeat timeout detects - a
        controller that has gone quiet - produces no message at all. So nothing
        ever asked the question again: an entity kept whatever state it was
        last written with, and a controller that had stopped publishing
        still read `on`, with every entity on it still showing values from
        before it went quiet and still claiming to be available.

        A timeout needs a clock, not an event. The caller ticks; this says
        whether anything is worth writing, so a quiet minute costs one
        comparison per controller instead of a state write per entity.
        """
        now_online = self.online(now)
        if now_online == self.reported:
            return False
        self.reported = now_online
        return True

    def online(self, now: float) -> bool:
        if self.last_seen is None:
            # Nothing heard yet. Optimistic on purpose: otherwise every entity
            # is unavailable for up to a minute after a start, which looks
            # exactly like the fault this integration exists to remove.
            return True
        return (now - self.last_seen) <= self.timeout_s


@dataclass
class EntitySpec:
    """A named bundle of leaves, and the domain it is presented as.

    `reads` and `writes` are role -> point. The roles are the vocabulary of the
    platform that consumes them and mean nothing here; this module only needs
    to know which points to subscribe to and which to publish on. Keeping the
    two maps separate is what lets the core stay domain-free: there is no
    naming convention to interpret and no direction to infer.
    """

    platform: str
    unique_id: str
    object_id: str
    name: str
    reads: dict[str, str] = field(default_factory=dict)
    writes: dict[str, str] = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    icon: str | None = None
    #: Entity REGISTRY facts, not entity state: the area this entity belongs to
    #: (by NAME, because that is what a person writes and what a descriptor
    #: carries) and its voice aliases. Applied when the entity is added.
    area: str | None = None
    aliases: tuple[str, ...] = ()
    #: Key into `translations/<lang>.json` under `entity.<platform>`. Where it
    #: is set the entity is named by Home Assistant from that bundle, in the
    #: language of whoever is looking; where it is not, `name` is used as
    #: written. See I3Entity.__init__ for why the two are exclusive.
    translation_key: str | None = None

    def controllers(self) -> set[str]:
        """Every controller this entity depends on.

        Usually the ones its points address. An entity can also name a
        controller in its options without binding a point on it - a liveness
        entity is exactly that - and it still depends on it: it is woken by
        that controller's heartbeat and by nothing else. Leaving options out
        made such an entity depend on nothing, so it was never notified, and
        its state was written once at startup and never again.
        """
        named = {controller_of(p)
                 for p in (*self.reads.values(), *self.writes.values())}
        own = self.options.get("controller")
        if isinstance(own, str) and own:
            named.add(own)
        return named

    def primary_controller(self) -> str | None:
        """The controller this entity is listed under as a device.

        An entity can depend on two - a thermostat whose probe is on another
        module - but belongs to one: the one it commands, else the one it
        reads, else the one it names.
        """
        for point in (*self.writes.values(), *self.reads.values()):
            return controller_of(point)
        own = self.options.get("controller")
        return own if isinstance(own, str) and own else None

    def validate(self) -> None:
        for role, point in (*self.reads.items(), *self.writes.items()):
            try:
                parse_point(point)
            except ValueError as exc:
                raise ValueError(f"{self.unique_id}.{role}: {exc}") from exc


def merge_specs(specs: list[EntitySpec]) -> tuple[list[EntitySpec], list[str]]:
    """One spec per unique_id, and what was wrong with the list.

    Home Assistant merges this integration's block across packages, so an
    entity two packages both declare arrives twice. Identical declarations are
    the normal case and collapse silently. Two different ones under one
    unique_id are a fault that used to pass without a word - the later one
    replaced the earlier, so an entity quietly lost its points or its options
    depending on which package loaded last. The first is kept and the
    conflict is reported.

    Two unique_ids wanting the same entity_id are reported too: Home Assistant
    gives the second a `_2` suffix, and nothing would say why.
    """
    kept: dict[str, EntitySpec] = {}
    problems: list[str] = []
    for spec in specs:
        held = kept.get(spec.unique_id)
        if held is None:
            kept[spec.unique_id] = spec
        elif held != spec:
            problems.append(
                f"{spec.unique_id} is declared twice with different settings; "
                f"the first declaration is used")
    wanted: dict[str, str] = {}
    for spec in kept.values():
        entity_id = f"{spec.platform}.{spec.object_id}"
        other = wanted.setdefault(entity_id, spec.unique_id)
        if other != spec.unique_id:
            problems.append(
                f"{other} and {spec.unique_id} both want {entity_id}; the second "
                f"will get a generated id")
    return list(kept.values()), problems


@dataclass(frozen=True)
class RegistryRow:
    """The part of an entity registry row this module needs in order to decide."""

    entity_id: str
    platform: str
    unique_id: str


@dataclass(frozen=True)
class Claim:
    """One registry edit needed before an entity can own its entity_id."""

    action: str          # "remove", "rename" or "conflict"
    entity_id: str
    new_entity_id: str | None = None
    reason: str = ""


def plan_entity_id_claims(
    specs: list[EntitySpec], rows: list[RegistryRow], domain: str
) -> list[Claim]:
    """Take back the entity_id each entity is configured to own.

    A `unique_id` is unique per (platform, unique_id), so adopting a channel
    that another platform declared always produces a SECOND registry row. The
    old row keeps the entity_id, and Home Assistant's own id generator AVOIDS
    the collision rather than resolving it - the new entity is created with a
    `_2` suffix while every reference elsewhere still names the original.

    The old row has to go before the new entity is created. That is a decision
    about identity rather than about Home Assistant, so it is made here.

    Three cases, in this order:

      * another platform holds a row for one of our unique_ids: remove it. It
        is a previous declaration of the same physical point and nothing will
        update it again.
      * something holds the entity_id we need and is not this entity: leave it
        alone and report the conflict. A name collision with an unrelated
        entity is a configuration mistake, and deleting somebody's entity to
        make room for ours is never the fix.
      * one of our own rows sits on the wrong id, from a start that ran before
        this pass existed: rename it, so its history follows.
    """
    by_id = {r.entity_id: r for r in rows}
    mine_by_uid = {r.unique_id: r for r in rows if r.platform == domain}
    foreign_by_uid: dict[str, list[RegistryRow]] = {}
    for r in rows:
        if r.platform != domain:
            foreign_by_uid.setdefault(r.unique_id, []).append(r)

    plan: list[Claim] = []
    for spec in specs:
        wanted = f"{spec.platform}.{spec.object_id}"

        for row in foreign_by_uid.get(spec.unique_id, ()):
            plan.append(Claim("remove", row.entity_id,
                              reason=f"{row.platform} declared {spec.unique_id} before"))

        holder = by_id.get(wanted)
        if holder is not None and holder.unique_id != spec.unique_id:
            plan.append(Claim("conflict", wanted,
                              reason=f"held by {holder.platform}/{holder.unique_id}"))
            continue

        row = mine_by_uid.get(spec.unique_id)
        if row is not None and row.entity_id != wanted:
            plan.append(Claim("rename", row.entity_id, new_entity_id=wanted,
                              reason="ours, parked on a suffixed id"))

    # A remove must land before a rename that needs the id it holds. Within one
    # entity that is already the order above; across entities it is not,
    # because one entity's stale row can hold another's wanted id.
    return sorted(plan, key=lambda c: {"remove": 0, "rename": 1, "conflict": 2}[c.action])


@dataclass
class Owned:
    """The unique_ids this integration declares, for discovery suppression."""

    unique_ids: frozenset[str] = field(default_factory=frozenset)

    def should_delete_discovery(self, payload: dict | None) -> bool:
        """Is this retained discovery config a duplicate of one we declare?

        The controller publishes its own MQTT discovery for some channels,
        retained, using the same unique_id grammar this integration addresses
        them by. While those entities were declared as `mqtt:` YAML with the
        same ids, Home Assistant refused the discovered duplicates. An entity
        owned by this integration is a different platform, so that no longer
        holds: the discovered copy would be created too and would claim the
        entity_id.

        So a duplicate of something we declare is removed, by publishing an
        empty retained payload to its config topic. It has to keep happening -
        the controller re-announces its discovery on every reconnect, which is
        it doing its job rather than a fault to work around.

        Narrow on purpose: only configs whose unique_id is one we declare.
        Anything the configuration does not mention is left alone, and an empty
        payload - which is how discovery says "this entity is gone" - passes
        through instead of being answered with another delete.
        """
        if not isinstance(payload, dict):
            return False
        uid = payload.get("unique_id")
        return isinstance(uid, str) and uid in self.unique_ids
