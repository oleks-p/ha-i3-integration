"""The integration running in Home Assistant, against a mocked MQTT broker.

Each test is a behaviour the controller forces, or a failure that was found in
the field, played through the real entity platforms rather than the pure model.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ASSUMED_STATE, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.i3_atom import STORAGE_KEY
from custom_components.i3_atom.const import DOMAIN

EVENTS = "/devices/i3/events"
COMMANDS = "/devices/i3/commands"

RELAY = {
    "platform": "light", "unique_id": "i3_A1B2_out_9", "object_id": "hall_sconce",
    "name": "Hall sconce",
    "reads": {"state": "A1B2/out/9/value"}, "writes": {"command": "A1B2/out/9/set"},
}
DIMMER = {
    "platform": "light", "unique_id": "i3_A1B2_pwm_24", "object_id": "hall_strip",
    "name": "Hall strip", "options": {"brightness_scale": 100},
    "reads": {"state": "A1B2/pwm/24/value"}, "writes": {"command": "A1B2/pwm/24/set"},
}
ONLINE = {
    "platform": "binary_sensor", "unique_id": "i3_A1B2_online",
    "object_id": "i3_a1b2_online", "name": "i3 A1B2 online",
    "options": {"controller": "A1B2"},
}
THERMOSTAT = {
    "platform": "climate", "unique_id": "i3_C3D4_comparator_v2_3",
    "object_id": "living_room", "name": "Living room",
    "reads": {"mode": "C3D4/comparator_v2/3/mode",
              "setpoint_low": "C3D4/comparator_v2/3/setpoint_heating",
              "setpoint_high": "C3D4/comparator_v2/3/setpoint_cooling",
              "current_temperature": "C3D4/comparator_v2/3/temp"},
    "writes": {"mode": "C3D4/comparator_v2/3/mode",
               "setpoint_low": "C3D4/comparator_v2/3/setpoint_heating",
               "setpoint_high": "C3D4/comparator_v2/3/setpoint_cooling"},
    "options": {"modes": ["off", "heat", "cool", "heat_cool"],
                "mode_map": {"0": "off", "1": "heat", "2": "cool", "3": "heat_cool"},
                "sane": [5, 40]},
}


def _conf(*entities, **extra) -> dict:
    return {"controllers": [{"id": "A1B2"}], "entities": list(entities), **extra}


def _published(mqtt_mock, topic: str) -> list[str]:
    return [c.args[1] for c in mqtt_mock.async_publish.call_args_list if c.args[0] == topic]


async def _beat(hass: HomeAssistant, controller: str, uptime: int) -> None:
    async_fire_mqtt_message(hass, f"{EVENTS}/{controller}/system/1/uptime", str(uptime))
    await hass.async_block_till_done()


async def _payload(hass: HomeAssistant, point: str, payload: str) -> None:
    async_fire_mqtt_message(hass, f"{EVENTS}/{point}", payload)
    await hass.async_block_till_done()


async def _tick(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


# ── setup ───────────────────────────────────────────────────────────────────

async def test_yaml_becomes_one_entry_with_the_configured_entity_ids(hass, setup_i3):
    entry = await setup_i3(_conf(RELAY, DIMMER, ONLINE))
    assert entry.state is ConfigEntryState.LOADED
    for entity_id in ("light.hall_sconce", "light.hall_strip", "binary_sensor.i3_a1b2_online"):
        assert hass.states.get(entity_id) is not None, entity_id


def _device_entities(hass, entry) -> set[str]:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "A1B2"), entry.entry_id)
    assert device is not None and device.name == "i3 A1B2"
    return {e.entity_id for e in er.async_entries_for_device(er.async_get(hass), device.id)}


async def test_by_default_no_entity_is_put_on_a_device_or_renamed(hass, setup_i3):
    await setup_i3(_conf(RELAY, ONLINE))
    assert not dr.async_entries_for_config_entry(
        dr.async_get(hass), hass.config_entries.async_entries(DOMAIN)[0].entry_id)
    assert hass.states.get("light.hall_sconce").name == "Hall sconce"
    assert hass.states.get("binary_sensor.i3_a1b2_online").name == "i3 A1B2 online"


async def test_grouping_by_controller_puts_each_entity_on_its_device(hass, setup_i3):
    """Possible only since the YAML became an entry: Home Assistant attaches
    devices to entities that belong to one, and to no others. It also prefixes
    each friendly name with the device's, which is why this is asked for."""
    entry = await setup_i3(_conf(RELAY, ONLINE, group_by_controller=True))
    assert _device_entities(hass, entry) == {"light.hall_sconce",
                                            "binary_sensor.i3_a1b2_online"}
    assert hass.states.get("light.hall_sconce").name == "i3 A1B2 Hall sconce"


async def test_a_platform_this_build_does_not_ship_is_a_configuration_error(hass, mqtt_mock):
    bad = {**RELAY, "platform": "cover"}
    assert not await async_setup_component(hass, DOMAIN, {DOMAIN: _conf(bad)})


async def test_an_entry_without_yaml_says_so(hass, mqtt_mock):
    """Not deleted: that would take every entity row with it."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_one_bad_option_costs_one_entity_not_the_platform(hass, setup_i3, caplog):
    """A misspelt device class used to take every binary sensor down with it,
    the controller-liveness one included."""
    bad = {"platform": "binary_sensor", "unique_id": "i3_A1B2_out_3", "object_id": "pump",
           "name": "Pump", "reads": {"state": "A1B2/out/3/value"},
           "options": {"device_class": "nonsense"}}
    await setup_i3(_conf(bad, ONLINE))
    assert hass.states.get("binary_sensor.i3_a1b2_online") is not None
    assert hass.states.get("binary_sensor.pump") is None
    assert "i3_A1B2_out_3 is not created" in caplog.text


async def test_a_conflicting_duplicate_is_reported(hass, setup_i3, caplog):
    await setup_i3(_conf(RELAY, {**RELAY, "name": "Something else"}))
    assert "declared twice with different settings" in caplog.text
    assert hass.states.get("light.hall_sconce").name == "Hall sconce"


# ── restore ─────────────────────────────────────────────────────────────────

async def test_a_measurement_comes_back_like_a_control_does(hass, setup_i3):
    """A quantity restores too, and the transport is why.

    The controller publishes a leaf only when that leaf changes, so silence
    means the value has not changed and a restored reading is the reading. The
    rule used to be the other way round - a measurement was left blank to wait
    for its own cadence - and on a steady house that cadence is hours: measured
    at a reference installation, 17 of 22 numeric sensors still blank 20
    minutes after a restart, with a buffer sensor last heard from 21 minutes
    before it.
    """
    mock_restore_cache(hass, [
        State("light.hall_strip", STATE_ON, {"brightness": 128}),
        State("sensor.valve_direction", "opening"),
        State("sensor.hall_temperature", "21.5"),
    ])
    coded = {"platform": "sensor", "unique_id": "i3_C3D4_mb_u16_12",
             "object_id": "valve_direction", "name": "Valve direction",
             "reads": {"state": "C3D4/mb_u16/12/value"},
             "options": {"map": {"0": "idle", "1": "opening"}}}
    measured = {"platform": "sensor", "unique_id": "i3_A1B2_adc_tempr_3",
                "object_id": "hall_temperature", "name": "Hall temperature",
                "reads": {"state": "A1B2/adc_tempr/3/value"},
                "options": {"unit": "°C", "device_class": "temperature"}}
    await setup_i3(_conf(DIMMER, coded, measured))
    strip = hass.states.get("light.hall_strip")
    assert strip.state == STATE_ON and strip.attributes["brightness"] == 128
    assert hass.states.get("sensor.valve_direction").state == "opening"
    assert hass.states.get("sensor.hall_temperature").state == "21.5"


async def test_a_restored_measurement_still_goes_through_the_range_guard(
        hass, setup_i3):
    """`sane` is configuration, and it may have been tightened since the value
    was written. A reading the current range would refuse as data must not come
    back through the restore path when it could not come in through the live
    one - an unconfigured channel's `-nan` is the case that matters.
    """
    mock_restore_cache(hass, [State("sensor.hall_temperature", "-500")])
    measured = {"platform": "sensor", "unique_id": "i3_A1B2_adc_tempr_3",
                "object_id": "hall_temperature", "name": "Hall temperature",
                "reads": {"state": "A1B2/adc_tempr/3/value"},
                "options": {"unit": "°C", "device_class": "temperature",
                            "sane": [5, 40]}}
    await setup_i3(_conf(measured))
    assert hass.states.get("sensor.hall_temperature").state == "unknown"


async def test_an_entity_lands_in_its_declared_area(hass, setup_i3):
    """The area comes from the configuration, not from a later sync.

    Home Assistant keeps the area in the entity registry and no YAML platform
    can set one, so this used to be applied afterwards by a separate tool over
    the websocket API - a step that is easy to skip, and an installation that
    skipped it had every entity in no area, which breaks area-scoped views,
    automations and voice.

    The area is named rather than identified, and one that does not exist yet
    is created.
    """
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import entity_registry as er

    lamp = {**DIMMER, "area": "Hallway", "aliases": ["Hall light", "Передпокій"]}
    await setup_i3(_conf(lamp))

    entry = er.async_get(hass).async_get("light.hall_strip")
    assert entry is not None
    area = ar.async_get(hass).async_get_area(entry.area_id)
    assert area is not None and area.name == "Hallway"
    assert set(entry.aliases) == {"Hall light", "Передпокій"}


async def test_an_existing_area_is_reused_not_duplicated(hass, setup_i3):
    """Two entities in one room must not make two rooms, and a room somebody
    already made by hand is the one they join."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import entity_registry as er

    areas = ar.async_get(hass)
    made = areas.async_create("Hallway")

    second = {**DIMMER, "unique_id": "i3_A1B2_pwm_25",
              "object_id": "hall_strip_2", "name": "Hall strip 2",
              "reads": {"state": "A1B2/pwm/25/value"},
              "writes": {"command": "A1B2/pwm/25/set"}, "area": "Hallway"}
    await setup_i3(_conf({**DIMMER, "area": "Hallway"}, second))

    registry = er.async_get(hass)
    ids = {registry.async_get(e).area_id
           for e in ("light.hall_strip", "light.hall_strip_2")}
    assert ids == {made.id}
    assert len([a for a in areas.async_list_areas() if a.name == "Hallway"]) == 1


async def test_an_entity_with_no_area_declared_is_left_alone(hass, setup_i3):
    """Silence in the configuration is not an instruction to clear anything.

    Most entities here name no area, and an absent key must not be read as
    "no area" - that would have this integration wipe an assignment somebody
    made deliberately in the interface.
    """
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import entity_registry as er

    await setup_i3(_conf(DIMMER))
    registry = er.async_get(hass)
    chosen = ar.async_get(hass).async_create("Somewhere else")
    registry.async_update_entity("light.hall_strip", area_id=chosen.id)

    await setup_i3(_conf(DIMMER))
    assert registry.async_get("light.hall_strip").area_id == chosen.id


async def test_a_mapped_sensor_is_an_enum_and_its_value_lands(hass, setup_i3):
    """Options without the enum device class were refused on every write, so
    the entity read `unknown` while its topic published normally."""
    coded = {"platform": "sensor", "unique_id": "i3_C3D4_mb_u16_12",
             "object_id": "valve_direction", "name": "Valve direction",
             "reads": {"state": "C3D4/mb_u16/12/value"},
             "options": {"map": {"0": "idle", "1": "opening"}, "unit": "x"}}
    await setup_i3(_conf(coded))
    await _payload(hass, "C3D4/mb_u16/12/value", "1")
    state = hass.states.get("sensor.valve_direction")
    assert state.state == "opening"
    assert state.attributes["device_class"] == "enum"
    assert "unit_of_measurement" not in state.attributes


async def _setup_with_early_payload(hass, setup_i3, monkeypatch, point, payload, conf):
    """Set up with `payload` arriving after subscribing but before the
    platforms load - the window a live value can beat a restored one in."""
    real = hass.config_entries.async_forward_entry_setups

    async def forward(entry, platforms):
        async_fire_mqtt_message(hass, f"{EVENTS}/{point}", payload)
        await real(entry, platforms)

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    return await setup_i3(conf)


async def test_silence_heard_first_does_not_block_the_restore(hass, setup_i3, monkeypatch):
    """The early payload decided between live and restored once, up front: a
    `-nan` arriving first left the entity `unknown`."""
    mock_restore_cache(hass, [State("light.hall_sconce", STATE_ON)])
    await _setup_with_early_payload(hass, setup_i3, monkeypatch,
                                    "A1B2/out/9/value", "-nan", _conf(RELAY))
    assert hass.states.get("light.hall_sconce").state == STATE_ON


async def test_a_live_value_beats_the_remembered_one(hass, setup_i3, monkeypatch):
    mock_restore_cache(hass, [State("light.hall_sconce", STATE_ON)])
    await _setup_with_early_payload(hass, setup_i3, monkeypatch,
                                    "A1B2/out/9/value", "0", _conf(RELAY))
    assert hass.states.get("light.hall_sconce").state == STATE_OFF


# ── commands and their echo ─────────────────────────────────────────────────

async def test_a_command_shows_at_once_and_the_echo_confirms_it(hass, setup_i3, mqtt_mock):
    await setup_i3(_conf(RELAY))
    await _payload(hass, "A1B2/out/9/value", "0")
    await hass.services.async_call("light", "turn_on", {"entity_id": "light.hall_sconce"},
                                   blocking=True)
    state = hass.states.get("light.hall_sconce")
    assert state.state == STATE_ON and state.attributes.get(ATTR_ASSUMED_STATE)
    assert _published(mqtt_mock, f"{COMMANDS}/A1B2/out/9/set") == ["1"]

    await _payload(hass, "A1B2/out/9/value", "1")
    state = hass.states.get("light.hall_sconce")
    assert state.state == STATE_ON and not state.attributes.get(ATTR_ASSUMED_STATE)


async def test_a_command_with_no_echo_stops_being_assumed(hass, setup_i3):
    """The controller publishes only on change, so commanding the value a
    channel already holds is never echoed - and the entity used to claim
    `assumed_state` until something else happened to write it."""
    await setup_i3(_conf(RELAY))
    await _payload(hass, "A1B2/out/9/value", "1")
    await hass.services.async_call("light", "turn_on", {"entity_id": "light.hall_sconce"},
                                   blocking=True)
    assert hass.states.get("light.hall_sconce").attributes.get(ATTR_ASSUMED_STATE)
    await _tick(hass, 4)
    state = hass.states.get("light.hall_sconce")
    assert state.state == STATE_ON and not state.attributes.get(ATTR_ASSUMED_STATE)


async def test_a_lost_command_goes_back_to_what_the_controller_said(hass, setup_i3):
    await setup_i3(_conf(RELAY))
    await _payload(hass, "A1B2/out/9/value", "0")
    await hass.services.async_call("light", "turn_on", {"entity_id": "light.hall_sconce"},
                                   blocking=True)
    await _tick(hass, 4)
    assert hass.states.get("light.hall_sconce").state == STATE_OFF


async def test_a_heartbeat_does_not_undo_a_command(hass, setup_i3):
    """Every heartbeat used to re-read every entity on the controller, which
    put the pre-command payload back over the value just commanded."""
    await setup_i3(_conf(RELAY))
    await _payload(hass, "A1B2/out/9/value", "0")
    await hass.services.async_call("light", "turn_on", {"entity_id": "light.hall_sconce"},
                                   blocking=True)
    await _beat(hass, "A1B2", 500)
    assert hass.states.get("light.hall_sconce").state == STATE_ON
    await _payload(hass, "A1B2/out/9/value", "1")


async def test_the_lowest_brightness_is_not_off(hass, setup_i3, mqtt_mock):
    await setup_i3(_conf(DIMMER))
    await hass.services.async_call("light", "turn_on",
                                   {"entity_id": "light.hall_strip", "brightness": 1},
                                   blocking=True)
    assert _published(mqtt_mock, f"{COMMANDS}/A1B2/pwm/24/set") == ["1"]
    assert hass.states.get("light.hall_strip").state == STATE_ON
    await _tick(hass, 4)


# ── liveness ────────────────────────────────────────────────────────────────

async def test_a_quiet_controller_goes_unavailable_and_comes_back(hass, setup_i3, clock):
    await setup_i3(_conf(RELAY, ONLINE))
    await _beat(hass, "A1B2", 500)
    assert hass.states.get("binary_sensor.i3_a1b2_online").state == STATE_ON

    clock.advance(241)
    await _tick(hass, 31)
    assert hass.states.get("light.hall_sconce").state == STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.i3_a1b2_online").state == STATE_OFF

    await _beat(hass, "A1B2", 741)
    assert hass.states.get("light.hall_sconce").state != STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.i3_a1b2_online").state == STATE_ON


async def test_the_liveness_entity_shows_the_boot_not_the_counter(hass, setup_i3, clock):
    """The counter changed every minute and wrote a new state each time."""
    await setup_i3(_conf(ONLINE))
    await _beat(hass, "A1B2", 600)
    first = hass.states.get("binary_sensor.i3_a1b2_online")
    assert "uptime_s" not in first.attributes
    assert first.attributes["booted_at"] == dt_util.utc_from_timestamp(round(clock.wall - 600))

    clock.advance(60)
    await _beat(hass, "A1B2", 661)
    assert hass.states.get("binary_sensor.i3_a1b2_online").last_updated == first.last_updated


async def test_a_restart_is_logged(hass, setup_i3, caplog):
    await setup_i3(_conf(RELAY))
    await _beat(hass, "A1B2", 500)
    await _beat(hass, "A1B2", 12)
    assert "controller A1B2 restarted;" in caplog.text


async def test_a_restart_while_home_assistant_was_down_is_logged(
    hass, setup_i3, clock, hass_storage, caplog
):
    hass_storage[STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": STORAGE_KEY,
                                 "data": {"A1B2": clock.wall - 100_000}}
    await setup_i3(_conf(RELAY))
    await _beat(hass, "A1B2", 600)
    assert "restarted while Home Assistant was not running" in caplog.text


async def test_boot_times_are_remembered_on_unload(hass, setup_i3, clock, hass_storage):
    entry = await setup_i3(_conf(RELAY))
    await _beat(hass, "A1B2", 600)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[STORAGE_KEY]["data"]["A1B2"] == pytest.approx(clock.wall - 600)


# ── identity ────────────────────────────────────────────────────────────────

async def test_a_previous_mqtt_declaration_gives_up_its_entity_id(hass, setup_i3):
    er.async_get(hass).async_get_or_create(
        "light", "mqtt", "i3_A1B2_out_9", suggested_object_id="hall_sconce")
    await setup_i3(_conf(RELAY))
    entry = er.async_get(hass).async_get("light.hall_sconce")
    assert entry.platform == DOMAIN


async def test_a_yaml_era_row_is_adopted_by_the_entry(hass, setup_i3):
    """Rows created before there was an entry keep their id and history."""
    er.async_get(hass).async_get_or_create(
        "light", DOMAIN, "i3_A1B2_out_9", suggested_object_id="hall_sconce")
    entry = await setup_i3(_conf(RELAY))
    row = er.async_get(hass).async_get("light.hall_sconce")
    assert row.config_entry_id == entry.entry_id


async def test_vendor_duplicates_are_deleted_at_either_depth(hass, setup_i3, mqtt_mock):
    await setup_i3(_conf(RELAY))
    for topic in ("homeassistant/light/i3_A1B2_out_9/config",
                  "homeassistant/light/A1B2/i3_A1B2_out_9/config"):
        async_fire_mqtt_message(hass, topic, '{"unique_id": "i3_A1B2_out_9"}', retain=True)
    async_fire_mqtt_message(hass, "homeassistant/light/other/config",
                            '{"unique_id": "someone_else"}', retain=True)
    await hass.async_block_till_done()
    deletes = _discovery_deletes(mqtt_mock)
    assert sorted(c.args[0] for c in deletes) == [
        "homeassistant/light/A1B2/i3_A1B2_out_9/config",
        "homeassistant/light/i3_A1B2_out_9/config"]
    assert all(c.args[2:] == (1, True) for c in deletes)        # qos 1, retained


def _discovery_deletes(mqtt_mock):
    return [c for c in mqtt_mock.async_publish.call_args_list
            if c.args[0].endswith("/config") and c.args[1] == ""]


async def _announce(hass, topic: str) -> None:
    async_fire_mqtt_message(hass, topic, '{"unique_id": "i3_A1B2_out_9"}', retain=True)
    await hass.async_block_till_done()


@pytest.mark.parametrize("mqtt_config_entry_options", [{"birth_message": {}, "discovery": False}])
async def test_nothing_is_watched_where_home_assistant_consumes_no_discovery(
    hass, setup_i3, mqtt_mock
):
    """No discovery consumed, so nothing can be duplicated - and the
    controller's configs are left on the broker for whoever does want them."""
    await setup_i3(_conf(RELAY))
    await _announce(hass, "homeassistant/light/i3_A1B2_out_9/config")
    assert _discovery_deletes(mqtt_mock) == []


@pytest.mark.parametrize("mqtt_config_entry_options",
                         [{"birth_message": {}, "discovery": True, "discovery_prefix": "hvacpro"}])
async def test_the_watched_prefix_is_home_assistants_own(hass, setup_i3, mqtt_mock):
    """A controller announcing where Home Assistant does not listen makes no
    duplicate, so its config is not touched; one announcing where it does is
    deleted. Nothing about the controller's app needs configuring here."""
    await setup_i3(_conf(RELAY))
    await _announce(hass, "homeassistant/light/i3_A1B2_out_9/config")
    await _announce(hass, "hvacpro/light/i3_A1B2_out_9/config")
    assert [c.args[0] for c in _discovery_deletes(mqtt_mock)] == [
        "hvacpro/light/i3_A1B2_out_9/config"]


async def test_suppression_can_be_switched_off(hass, setup_i3, mqtt_mock):
    await setup_i3(_conf(RELAY, suppress_vendor_discovery=False))
    await _announce(hass, "homeassistant/light/i3_A1B2_out_9/config")
    assert _discovery_deletes(mqtt_mock) == []


# ── re-announce ─────────────────────────────────────────────────────────────

async def test_reannounce_is_off_unless_asked_for(hass, setup_i3, mqtt_mock):
    await setup_i3(_conf(RELAY))
    await _tick(hass, 3)
    assert _published(mqtt_mock, f"{EVENTS}/A1B2/out/9/value") == []


async def test_reannounce_asks_for_every_bound_leaf(hass, setup_i3, mqtt_mock):
    await setup_i3(_conf(RELAY, DIMMER, reannounce=True))
    await _tick(hass, 3)
    assert _published(mqtt_mock, f"{EVENTS}/A1B2/out/9/value") == [""]
    assert _published(mqtt_mock, f"{EVENTS}/A1B2/pwm/24/value") == [""]


async def test_reannounce_follows_a_controller_restart(hass, setup_i3, mqtt_mock):
    await setup_i3(_conf(RELAY, reannounce=True))
    await _tick(hass, 3)
    await _beat(hass, "A1B2", 500)
    await _beat(hass, "A1B2", 5)
    await _tick(hass, 3)
    assert _published(mqtt_mock, f"{EVENTS}/A1B2/out/9/value") == ["", ""]


async def test_a_reannounce_request_is_not_a_value(hass, setup_i3):
    """The request comes back to us on the same topic. A thermostat that reads
    its mode from whether a setpoint is present switched itself off on it."""
    presence = {"platform": "climate", "unique_id": "i3_C3D4_comparator_2",
                "object_id": "zone_2", "name": "Zone 2",
                "reads": {"setpoint": "C3D4/comparator/2/value"},
                "writes": {"setpoint": "C3D4/comparator/2/set",
                           "power": "C3D4/comparator/2/enable"},
                "options": {"modes": ["off", "heat"], "mode_from_presence": True,
                            "on_mode": "heat", "sane": [5, 40]}}
    await setup_i3(_conf(presence))
    await _payload(hass, "C3D4/comparator/2/value", "21")
    assert hass.states.get("climate.zone_2").state == "heat"
    await _payload(hass, "C3D4/comparator/2/value", "")
    assert hass.states.get("climate.zone_2").state == "heat"
    await _payload(hass, "C3D4/comparator/2/value", "-nan")
    assert hass.states.get("climate.zone_2").state == "off"


# ── climate ─────────────────────────────────────────────────────────────────

async def test_a_mode_given_with_a_temperature_is_applied(hass, setup_i3, mqtt_mock):
    await setup_i3(_conf(THERMOSTAT))
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": "climate.living_room", "hvac_mode": "heat",
         "target_temp_low": 20.5, "target_temp_high": 25},
        blocking=True)
    assert _published(mqtt_mock, f"{COMMANDS}/C3D4/comparator_v2/3/mode") == ["1"]
    assert _published(mqtt_mock, f"{COMMANDS}/C3D4/comparator_v2/3/setpoint_heating") == ["20.5"]
    assert _published(mqtt_mock, f"{COMMANDS}/C3D4/comparator_v2/3/setpoint_cooling") == ["25"]
    state = hass.states.get("climate.living_room")
    assert state.state == "heat" and state.attributes["target_temp_low"] == 20.5
    await _tick(hass, 4)


async def test_a_mode_the_thermostat_does_not_offer_is_ignored(hass, setup_i3, caplog):
    """One mode map serves thermostats that offer fewer modes."""
    narrow = {**THERMOSTAT, "options": {**THERMOSTAT["options"], "modes": ["off", "heat"]}}
    await setup_i3(_conf(narrow))
    await _payload(hass, "C3D4/comparator_v2/3/mode", "1")
    await _payload(hass, "C3D4/comparator_v2/3/mode", "2")
    assert hass.states.get("climate.living_room").state == "heat"
    assert "not one of its modes" in caplog.text


async def test_a_setpoint_echo_ends_the_assumption(hass, setup_i3):
    """Only some reads used to end it, so a single-setpoint thermostat stayed
    `assumed` after every change until something else wrote it."""
    single = {"platform": "climate", "unique_id": "i3_A1B2_var_int_4",
              "object_id": "bathroom", "name": "Bathroom",
              "reads": {"setpoint": "A1B2/var_int/4/value"},
              "writes": {"setpoint": "A1B2/var_int/4/set"},
              "options": {"modes": ["heat"]}}
    await setup_i3(_conf(single))
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": "climate.bathroom", "temperature": 22},
                                   blocking=True)
    assert hass.states.get("climate.bathroom").attributes.get(ATTR_ASSUMED_STATE)
    await _payload(hass, "A1B2/var_int/4/value", "22")
    state = hass.states.get("climate.bathroom")
    assert state.attributes["temperature"] == 22
    assert not state.attributes.get(ATTR_ASSUMED_STATE)


async def test_climate_restores_its_targets_and_its_measurement(hass, setup_i3):
    """The measured temperature comes back with the setpoints.

    A sensor left blank is one empty row; a thermostat with no
    `current_temperature` is a card showing a setpoint and nothing else, which
    is what a reference installation had on ten thermostats at once twenty
    minutes after a restart, on rooms that had no reason to publish.
    """
    mock_restore_cache(hass, [State("climate.living_room", "heat_cool",
                                    {"target_temp_low": 19, "target_temp_high": 24,
                                     "current_temperature": 30})])
    await setup_i3(_conf(THERMOSTAT))
    state = hass.states.get("climate.living_room")
    assert state.state == "heat_cool"
    assert (state.attributes["target_temp_low"], state.attributes["target_temp_high"]) == (19, 24)
    assert state.attributes["current_temperature"] == 30


# ── the other platforms ─────────────────────────────────────────────────────

async def test_a_select_refuses_an_option_with_no_payload(hass, setup_i3):
    from homeassistant.exceptions import ServiceValidationError

    chooser = {"platform": "select", "unique_id": "i3_C3D4_var_int_1",
               "object_id": "house_mode", "name": "House mode",
               "reads": {"state": "C3D4/var_int/1/value"},
               "writes": {"command": "C3D4/var_int/1/set"},
               "options": {"options": ["home", "away", "party"],
                           "map": {"0": "home", "1": "away"}}}
    await setup_i3(_conf(chooser))
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("select", "select_option",
                                       {"entity_id": "select.house_mode", "option": "party"},
                                       blocking=True)


async def test_a_coarse_fan_offers_only_its_own_steps(hass, setup_i3, mqtt_mock):
    blower = {"platform": "fan", "unique_id": "i3_E5F6_0-10v_1", "object_id": "supply",
              "name": "Supply", "reads": {"state": "E5F6/0-10v/1/value"},
              "writes": {"command": "E5F6/0-10v/1/set"},
              "options": {"speed_range_max": 10}}
    await setup_i3(_conf(blower))
    assert hass.states.get("fan.supply").attributes["percentage_step"] == 10
    await hass.services.async_call("fan", "set_percentage",
                                   {"entity_id": "fan.supply", "percentage": 37}, blocking=True)
    assert _published(mqtt_mock, f"{COMMANDS}/E5F6/0-10v/1/set") == ["4"]
    assert hass.states.get("fan.supply").attributes["percentage"] == 40
    await _tick(hass, 4)


async def test_a_relay_can_be_a_switch(hass, setup_i3, mqtt_mock):
    pump = {"platform": "switch", "unique_id": "i3_A1B2_out_3", "object_id": "pump",
            "name": "Pump", "reads": {"state": "A1B2/out/3/value"},
            "writes": {"command": "A1B2/out/3/set"}, "options": {"invert": True}}
    mock_restore_cache(hass, [State("switch.pump", STATE_ON)])
    await setup_i3(_conf(pump))
    assert hass.states.get("switch.pump").state == STATE_ON
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.pump"},
                                   blocking=True)
    assert _published(mqtt_mock, f"{COMMANDS}/A1B2/out/3/set") == ["1"]
    await _payload(hass, "A1B2/out/3/value", "1")
    assert hass.states.get("switch.pump").state == STATE_OFF


async def test_a_number_writes_plain_decimals_and_restores_its_value(hass, setup_i3, mqtt_mock):
    setpoint = {"platform": "number", "unique_id": "i3_C3D4_var_int_7",
                "object_id": "dhw_setpoint", "name": "DHW setpoint",
                "reads": {"state": "C3D4/var_int/7/value"},
                "writes": {"command": "C3D4/var_int/7/set"},
                "options": {"min": 30, "max": 65, "step": 0.5, "unit": "°C"}}
    mock_restore_cache_with_extra_data(hass, [(
        State("number.dhw_setpoint", "48"),
        {"native_value": 48.0, "native_unit_of_measurement": "°C",
         "native_min_value": 30.0, "native_max_value": 65.0, "native_step": 0.5})])
    await setup_i3(_conf(setpoint))
    assert hass.states.get("number.dhw_setpoint").state == "48.0"
    await hass.services.async_call("number", "set_value",
                                   {"entity_id": "number.dhw_setpoint", "value": 52.5},
                                   blocking=True)
    assert _published(mqtt_mock, f"{COMMANDS}/C3D4/var_int/7/set") == ["52.5"]
    await _tick(hass, 4)


async def test_a_valve_reports_its_opening_on_its_own_scale(hass, setup_i3, mqtt_mock):
    mixer = {"platform": "valve", "unique_id": "i3_C3D4_twv_1", "object_id": "mixer",
             "name": "Mixer", "reads": {"state": "C3D4/twv/1/position"},
             "writes": {"command": "C3D4/twv/1/set"}, "options": {"position_scale": 1000}}
    await setup_i3(_conf(mixer))
    await _payload(hass, "C3D4/twv/1/position", "250")
    state = hass.states.get("valve.mixer")
    assert state.state == "open" and state.attributes["current_position"] == 25
    await hass.services.async_call("valve", "close_valve", {"entity_id": "valve.mixer"},
                                   blocking=True)
    assert _published(mqtt_mock, f"{COMMANDS}/C3D4/twv/1/set") == ["0"]
    await _payload(hass, "C3D4/twv/1/position", "0")
    assert hass.states.get("valve.mixer").state == "closed"


# ── reload and unload ───────────────────────────────────────────────────────

async def test_reload_picks_up_a_changed_yaml(hass, setup_i3):
    entry = await setup_i3(_conf(RELAY))
    with patch("homeassistant.config.load_yaml_config_file",
               return_value={DOMAIN: _conf(RELAY, DIMMER)}):
        await hass.services.async_call(DOMAIN, "reload", blocking=True)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("light.hall_strip") is not None
    assert er.async_get(hass).async_get("light.hall_sconce") is not None


async def test_after_unload_nothing_is_listening(hass, setup_i3, caplog):
    entry = await setup_i3(_conf(RELAY))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    with caplog.at_level(logging.ERROR):
        await _payload(hass, "A1B2/out/9/value", "1")
        await _beat(hass, "A1B2", 10)
    assert "Traceback" not in caplog.text
