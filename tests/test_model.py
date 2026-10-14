"""Each test here corresponds to a failure that is silent in the field."""
from custom_components.i3_atom import model

# ── payloads ────────────────────────────────────────────────────────────────

def test_an_empty_payload_is_not_a_value():
    """Publishing nothing to a state topic is how a controller is asked to
    re-announce. Reading it as a number turns things on."""
    assert model.parse_number("") is None
    assert model.parse_number("   ") is None
    assert model.parse_number("0") == 0.0
    assert model.parse_number("1") == 1.0


def test_an_unconfigured_channel_says_nothing_rather_than_zero():
    """It publishes '-nan', not 'nan'. None and 0 have to stay different
    answers: 0 is a reading, None is silence, and only silence should leave a
    restored value in place."""
    for token in ("nan", "-nan", "NaN", "none", "null"):
        assert model.parse_number(token) is None, token


def test_a_level_round_trips_without_drifting():
    """Rounding at both ends of a round trip walks a level by a point per
    operation, which over a session drifts away from what was set."""
    for level in range(0, 101):
        ha = model.scale_to(level, 100, 255)
        assert 0 <= ha <= 255
        back = model.scale_to(ha, 255, 100)
        assert abs(back - level) <= 1, (level, ha, back)


def test_a_scale_must_be_positive():
    for bad in (0, -1):
        try:
            model.scale_to(5, bad, 255)
        except ValueError:
            continue
        raise AssertionError(bad)


# ── addressing ──────────────────────────────────────────────────────────────

def test_a_point_is_parsed_and_never_guessed():
    """Channel types are not interchangeable: a command sent to the wrong one
    is accepted by the broker and produces no echo and no effect. A malformed
    address is refused rather than repaired."""
    assert model.parse_point("A1B2/out/9/value") == ("A1B2", "out", 9, "value")
    assert model.parse_point("C3D4/comparator_v2/3/setpoint_heating") == \
        ("C3D4", "comparator_v2", 3, "setpoint_heating")
    for bad in ("A1B2/out/9", "/out/9/value", "A1B2/out/x/value", "", "A1B2//9/value"):
        try:
            model.parse_point(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_the_leaf_is_part_of_the_address():
    """Not every channel exposes `value`. One family publishes `position`, and
    a thermostat channel publishes a mode, two setpoints and a temperature as
    four separate leaves - so the unit of subscription is a leaf, not a
    channel, and an entity may bind several of them."""
    for point, leaf in (("C3D4/twv/1/position", "position"),
                        ("C3D4/comparator_v2/3/mode", "mode"),
                        ("C3D4/comparator_v2/3/temp", "temp"),
                        ("A1B2/adc_tempr/3/value", "value")):
        assert model.parse_point(point)[3] == leaf


def test_topics_keep_the_leading_slash():
    """The controller publishes with a leading slash, which creates an empty
    first topic level. Reproduce it exactly or subscribe to nothing."""
    assert model.event_topic("i3", "A1B2/out/9/value") == \
        "/devices/i3/events/A1B2/out/9/value"
    assert model.command_topic("i3", "A1B2/out/9/set") == \
        "/devices/i3/commands/A1B2/out/9/set"
    assert model.heartbeat_topic("other", "A7B8") == \
        "/devices/other/events/A7B8/system/1/uptime"


def test_an_entity_names_the_controllers_it_depends_on():
    """Availability is per controller, and an entity can bind leaves on more
    than one - a thermostat whose setpoint is on one module and whose probe is
    on another is unreachable if either goes quiet."""
    spec = model.EntitySpec(
        platform="climate", unique_id="x", object_id="x", name="x",
        reads={"mode": "C3D4/comparator_v2/3/mode",
               "temperature": "A1B2/adc_tempr/3/value"},
        writes={"mode": "C3D4/comparator_v2/3/mode"})
    assert spec.controllers() == {"C3D4", "A1B2"}
    spec.validate()


def test_a_malformed_point_is_reported_with_its_role():
    spec = model.EntitySpec(platform="light", unique_id="uid", object_id="o",
                            name="n", reads={"state": "nonsense"})
    try:
        spec.validate()
    except ValueError as exc:
        assert "uid.state" in str(exc), exc
        return
    raise AssertionError("accepted a malformed point")


# ── controller liveness ─────────────────────────────────────────────────────

def test_an_availability_timeout_below_the_heartbeat_period_is_refused():
    """Heartbeats are round-robin at roughly 60 s with a per-device phase
    offset, so anything under 180 s flaps. The floor is enforced, not
    documented."""
    assert model.ControllerHealth("X", timeout_s=30).timeout_s == \
        model.MIN_HEARTBEAT_TIMEOUT_S


def test_a_controller_that_has_said_nothing_yet_is_not_unavailable():
    """Marking every entity unavailable for the first minute after a start
    looks exactly like the fault this integration removes."""
    assert model.ControllerHealth("X").online(now=1000.0) is True


def test_a_quiet_controller_goes_unavailable_on_its_own_timeout():
    h = model.ControllerHealth("X", timeout_s=240)
    h.saw_heartbeat("49469", now=1000.0)
    assert h.online(now=1000.0 + 239) is True
    assert h.online(now=1000.0 + 241) is False


def test_a_restart_is_recognised_from_the_counter_going_backwards():
    """The only announcement a restart gets. Every value on that controller is
    then last known rather than confirmed."""
    h = model.ControllerHealth("X")
    assert h.saw_heartbeat("49400", now=1.0) is False
    assert h.saw_heartbeat("49460", now=61.0) is False
    assert h.saw_heartbeat("12", now=121.0) is True
    assert h.saw_heartbeat("72", now=181.0) is False


def test_a_heartbeat_without_a_number_still_counts_as_contact():
    """A beat that cannot be parsed is still a beat: the controller spoke."""
    h = model.ControllerHealth("X")
    assert h.saw_heartbeat("nan", now=10.0) is False
    assert h.last_seen == 10.0
    assert h.online(now=10.0) is True


# ── entity_id identity ──────────────────────────────────────────────────────

def _spec(platform, uid, object_id):
    return model.EntitySpec(platform=platform, unique_id=uid, object_id=object_id,
                            name=object_id, reads={"state": "A1B2/out/1/value"})


def test_a_previous_declaration_is_released_before_ours_is_named():
    """A unique_id is unique per (platform, unique_id), so adopting a point
    another platform declared always produces a SECOND registry row. The old
    row keeps the entity_id, and Home Assistant's id generator avoids the
    collision rather than resolving it - so the new entity is created with a
    `_2` suffix while every reference elsewhere still names the original."""
    specs = [_spec("light", "uid_a", "kitchen_strip")]
    rows = [
        model.RegistryRow("light.kitchen_strip", "mqtt", "uid_a"),
        model.RegistryRow("light.kitchen_strip_2", "i3_atom", "uid_a"),
    ]
    plan = model.plan_entity_id_claims(specs, rows, "i3_atom")
    assert [(c.action, c.entity_id) for c in plan] == [
        ("remove", "light.kitchen_strip"),
        ("rename", "light.kitchen_strip_2"),
    ]
    assert plan[1].new_entity_id == "light.kitchen_strip"


def test_a_remove_is_always_planned_before_a_rename():
    """Across entities the order is not natural: one entity's stale row can
    hold another entity's wanted id, so a rename applied first would collide."""
    specs = [_spec("light", "uid_a", "a"), _spec("light", "uid_b", "b")]
    rows = [
        model.RegistryRow("light.b", "mqtt", "uid_b"),
        model.RegistryRow("light.a_2", "i3_atom", "uid_a"),
        model.RegistryRow("light.a", "mqtt", "uid_a"),
    ]
    actions = [c.action for c in model.plan_entity_id_claims(specs, rows, "i3_atom")]
    assert actions == sorted(actions, key=lambda a: {"remove": 0, "rename": 1}[a])


def test_an_unrelated_entity_is_never_deleted_to_make_room():
    """The wanted id can be held by something nothing to do with this
    integration. Deleting it to free a name is never the fix: the clash is a
    configuration mistake and has to be said out loud."""
    plan = model.plan_entity_id_claims(
        [_spec("light", "uid_a", "porch")],
        [model.RegistryRow("light.porch", "hue", "abc-123")],
        "i3_atom")
    assert [c.action for c in plan] == ["conflict"]
    assert "hue" in plan[0].reason


def test_the_claim_is_per_domain_not_per_light():
    """The same identity problem applies to every platform this integration
    presents. Hard-coding one domain is how the second platform silently ships
    with suffixed ids."""
    specs = [_spec("binary_sensor", "uid_c", "a1b2_online")]
    rows = [model.RegistryRow("binary_sensor.a1b2_online", "template", "uid_c")]
    plan = model.plan_entity_id_claims(specs, rows, "i3_atom")
    assert [(c.action, c.entity_id) for c in plan] == [
        ("remove", "binary_sensor.a1b2_online")]


def test_a_settled_registry_needs_no_edits():
    """Every start after the first. The claim pass has to be a no-op once the
    ids are right, or it rewrites the registry on every restart."""
    specs = [_spec("light", "uid_a", "porch")]
    rows = [model.RegistryRow("light.porch", "i3_atom", "uid_a")]
    assert model.plan_entity_id_claims(specs, rows, "i3_atom") == []


# ── discovery suppression ───────────────────────────────────────────────────

def test_only_declared_duplicates_are_deleted():
    """A controller announces some channels over MQTT discovery using the same
    unique_id grammar this integration addresses them by. Those duplicates have
    to go or they claim the entity_ids. Nothing else does."""
    owned = model.Owned(frozenset({"uid_a", "uid_b"}))
    assert owned.should_delete_discovery({"unique_id": "uid_a"}) is True
    assert owned.should_delete_discovery({"unique_id": "uid_z"}) is False
    # an empty payload is how discovery says "gone"; do not answer it
    assert owned.should_delete_discovery(None) is False
    assert owned.should_delete_discovery({}) is False
    assert owned.should_delete_discovery({"unique_id": 7}) is False


# ── coded values ────────────────────────────────────────────────────────────

def test_a_mode_is_decoded_both_ways_from_one_map():
    """One map, used in both directions, so the two cannot drift apart. A
    separate encode table is how a mode reads correctly and writes the wrong
    number."""
    m = {"0": "off", "1": "heat", "2": "cool", "3": "heat_cool"}
    for payload, word in m.items():
        assert model.decode(payload, m) == word
        assert model.encode(word, m) == payload


def test_an_unmapped_payload_is_not_guessed():
    """A firmware that grows a fifth mode should leave the entity on its last
    known one rather than silently report the nearest word."""
    m = {"0": "off", "1": "heat"}
    assert model.decode("4", m) is None
    assert model.decode("", m) is None
    assert model.encode("cool", m) is None


def test_a_setpoint_outside_what_a_thermostat_could_hold_is_data_not_a_reading():
    """Some channel types carry the setpoint and the fact of having no setpoint
    on the same leaf, and an unconfigured one publishes `-nan`. Without the
    range guard that reaches the UI as a temperature of several thousand
    degrees, or as a dash, depending on which way the cast failed."""
    sane = (5.0, 40.0)
    assert model.parse_bounded("21.5", sane) == 21.5
    assert model.parse_bounded("-nan", sane) is None
    assert model.parse_bounded("nan", sane) is None
    assert model.parse_bounded("", sane) is None
    assert model.parse_bounded("1e9", sane) is None
    assert model.parse_bounded("-40", sane) is None
    # and with no range declared, any number is accepted
    assert model.parse_bounded("1e9", None) == 1e9


def test_the_range_guard_does_not_reject_the_bounds_themselves():
    assert model.parse_bounded("5", (5.0, 40.0)) == 5.0
    assert model.parse_bounded("40", (5.0, 40.0)) == 40.0


def test_a_channel_type_may_contain_a_hyphen_and_start_with_a_digit():
    """`0-10v` is a real channel type. Matching the type as if it were an
    identifier refused every analogue output on the site, and the refusal
    surfaced as a whole package being rejected rather than as one bad point."""
    assert model.parse_point("E5F6/0-10v/1/value") == ("E5F6", "0-10v", 1, "value")
    spec = model.EntitySpec(platform="fan", unique_id="u", object_id="o", name="n",
                            reads={"state": "E5F6/0-10v/1/value"},
                            writes={"command": "E5F6/0-10v/1/set"})
    spec.validate()
    assert spec.controllers() == {"E5F6"}


def test_an_entity_can_depend_on_a_controller_without_binding_a_point():
    """A liveness entity names its controller in options and binds nothing.

    It still depends on it: that controller's heartbeat is the only thing that
    wakes it. Counting only the points made it depend on nothing, so it was
    never notified - its state was written once at startup and never again, and
    it reported `uptime_s: null` for as long as it was up while the heartbeat
    was arriving the whole time.
    """
    spec = model.EntitySpec(platform="binary_sensor", unique_id="i3_A1B2_online",
                            object_id="i3_a1b2_online", name="n",
                            options={"controller": "A1B2"})
    assert spec.controllers() == {"A1B2"}
    spec.validate()
    # and a point-bound entity is unaffected
    other = model.EntitySpec(platform="light", unique_id="u", object_id="o", name="n",
                             reads={"state": "C3D4/out/1/value"})
    assert other.controllers() == {"C3D4"}


def test_a_controller_going_quiet_is_noticed_without_a_message():
    """Silence produces no event, and silence is the thing a timeout detects.

    Every other path here is driven by an arriving message, so when a
    controller stopped publishing nothing ever re-asked the question: its
    liveness entity kept reporting the last value it was written with, and
    every entity on that controller went on claiming to be available with
    values from before it went quiet.
    """
    h = model.ControllerHealth("X", timeout_s=240)
    # first tick after a beat: online, and worth reporting because nothing was
    assert h.saw_heartbeat("100", now=0.0) is False
    assert h.liveness_changed(now=1.0) is True
    # still online, nothing to say
    assert h.liveness_changed(now=100.0) is False
    assert h.liveness_changed(now=239.0) is False
    # past the timeout with no beat: a change, reported once
    assert h.liveness_changed(now=241.0) is True
    assert h.liveness_changed(now=400.0) is False
    # and back again when it speaks
    h.saw_heartbeat("160", now=401.0)
    assert h.liveness_changed(now=402.0) is True


# ── edge cases ──────────────────────────────────────────────────────────────

def test_infinity_is_silence_not_a_level():
    """No channel holds infinity, and scaling it raised OverflowError inside
    the MQTT callback instead of leaving the last value alone."""
    for token in ("inf", "-inf", "1e400", "Infinity"):
        assert model.parse_number(token) is None, token
    assert model.parse_bounded("inf", None) is None


def test_a_non_zero_level_never_rounds_to_off():
    """Brightness 1 on a channel whose full scale is 100 rounded to 0, so
    turning a light on at its lowest level switched it off."""
    assert model.scale_to(1, 255, 100) == 1
    assert model.scale_to(1, 1000, 255) == 1
    assert model.scale_to(0, 255, 100) == 0
    assert model.scale_to(255, 255, 100) == 100


def test_a_command_and_its_echo_share_a_channel():
    """A relay is commanded on `set` and answers on `value`; a comparator is
    switched on `enable`. The channel is what ties them together."""
    assert model.channel_of("A1B2/out/9/set") == model.channel_of("A1B2/out/9/value")
    assert model.channel_of("C3D4/comparator/2/enable") == "C3D4/comparator/2"
    assert model.channel_of("A1B2/out/9/value") != model.channel_of("A1B2/out/10/value")


def test_the_assumed_window_outlasts_the_echo():
    assert model.ASSUMED_WINDOW_S > 2 * model.COMMAND_ECHO_S


def test_a_number_is_written_without_an_exponent():
    assert model.format_number(21) == "21"
    assert model.format_number(21.0) == "21"
    assert model.format_number(21.5) == "21.5"
    assert "e" not in model.format_number(0.0001)


def test_discovery_is_matched_with_and_without_a_node_level():
    assert model.discovery_topics("homeassistant") == (
        "homeassistant/+/+/config", "homeassistant/+/+/+/config")


# ── restarts nobody saw ─────────────────────────────────────────────────────

def test_a_reboot_while_home_assistant_was_down_is_recognised():
    """Booted at t=1000 and remembered. Home Assistant comes back at t=100000;
    an uptime of 99000 means the same boot, 600 means a new one."""
    assert model.restarted_while_away(1000.0, uptime=99_000, wall=100_000.0) is False
    assert model.restarted_while_away(1000.0, uptime=600, wall=100_000.0) is True


def test_a_long_outage_does_not_hide_a_reboot():
    """Down for two days, the controller rebooted after one: its uptime is
    still larger than the last one seen, so only the boot time tells."""
    day = 86_400.0
    assert model.restarted_while_away(0.0, uptime=day, wall=2 * day) is True


def test_clock_jitter_is_not_a_reboot():
    assert model.restarted_while_away(1000.0, uptime=98_900, wall=100_000.0) is False
    # a clock that went backwards makes the boot look earlier, never later
    assert model.restarted_while_away(1000.0, uptime=99_000, wall=99_000.0) is False


def test_the_first_beat_is_judged_against_the_remembered_boot():
    h = model.ControllerHealth("X")
    h.remembered_boot = 1000.0
    assert h.saw_heartbeat("600", now=1.0, wall=100_000.0) is True
    assert h.booted_at == 100_000.0 - 600
    # and only the first: after that the counter itself is the judge
    assert h.saw_heartbeat("660", now=61.0, wall=100_060.0) is False


def test_without_a_memory_the_first_beat_is_not_a_restart():
    h = model.ControllerHealth("X")
    assert h.saw_heartbeat("600", now=1.0, wall=100_000.0) is False
    assert h.booted_at == 99_400.0
    assert h.boot_estimate() == 99_400.0


def test_the_boot_time_moves_only_on_a_reboot():
    """It is shown on the liveness entity in place of the raw counter, which
    changed every minute and wrote a new state each time."""
    h = model.ControllerHealth("X")
    h.saw_heartbeat("600", now=1.0, wall=10_000.0)
    h.saw_heartbeat("662", now=61.0, wall=10_060.0)     # two seconds of phase
    assert h.booted_at == 9_400.0
    h.saw_heartbeat("5", now=121.0, wall=10_120.0)
    assert h.booted_at == 10_115.0


# ── merged packages ─────────────────────────────────────────────────────────

def test_an_entity_declared_twice_the_same_way_is_one_entity():
    a = _spec("light", "uid_a", "a")
    specs, problems = model.merge_specs([a, _spec("light", "uid_a", "a")])
    assert specs == [a] and problems == []


def test_an_entity_declared_twice_differently_is_reported_and_the_first_kept():
    """The later declaration used to replace the earlier one without a word."""
    first = _spec("light", "uid_a", "a")
    second = _spec("light", "uid_a", "other_name")
    specs, problems = model.merge_specs([first, second])
    assert specs == [first]
    assert len(problems) == 1 and "uid_a" in problems[0]


def test_two_entities_wanting_one_entity_id_are_reported():
    specs, problems = model.merge_specs([_spec("light", "uid_a", "porch"),
                                         _spec("light", "uid_b", "porch")])
    assert len(specs) == 2
    assert len(problems) == 1 and "light.porch" in problems[0]


def test_an_entity_belongs_to_the_controller_it_commands():
    spec = model.EntitySpec(
        platform="climate", unique_id="x", object_id="x", name="x",
        reads={"current_temperature": "A1B2/adc_tempr/3/value"},
        writes={"mode": "C3D4/comparator_v2/3/mode"})
    assert spec.primary_controller() == "C3D4"
    liveness = model.EntitySpec(platform="binary_sensor", unique_id="y",
                                object_id="y", name="y", options={"controller": "A7B8"})
    assert liveness.primary_controller() == "A7B8"
