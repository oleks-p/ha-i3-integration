# i3 Atom

A Home Assistant integration for i3 Engineering Atom controllers over MQTT.

## Why it exists

The controller publishes a topic **only when its value changes** and **retains
nothing**. If you declare its channels as `mqtt:` entities, every one of them
is blank after a restart, and stays blank until something changes it. Anything
nobody touches stays blank indefinitely.

The blank state also breaks controls. Home Assistant's toggle treats `unknown`
as **on**, so the first press on a blank light sends *off* to something that is
already off. Nothing happens, and only the second press works.

An entity here restores its last known value instead, so no `unknown` is left
to be misread. Nothing is mirrored onto other topics, and nothing is written
into the controller's own tree unless you turn on [re-announce](#re-announce).

A restored value is *last known*, not confirmed. If something was operated
locally while Home Assistant was down, it reads stale until that topic next
changes. That beats blank, which is both wrong and mishandled, but it is not
the same as true. A controller restart is therefore logged as a warning,
including one that happened while Home Assistant was not running.

## Design

The core (`__init__.py`, `model.py`, `entity.py`) knows about controllers,
channels, topic leaves and entity registry identity. **It does not know what a
light is.** An entity declares the platform it is presented as and the leaves
it binds. A small adapter per platform turns those leaves into that domain's
contract.

The split matters because the same relay is a lamp in one installation and a
pump in another. The Home Assistant domain is a configuration decision and is
never inferred from the channel type. Adding a domain means adding a file next
to `light.py`; the list of platforms is read from the files that exist, so the
core does not change.

Everything that can be decided without Home Assistant lives in `model.py`. The
Home Assistant glue is deliberately thin.

```
custom_components/i3_atom/
  __init__.py      Hub: one MQTT subscription per leaf, payload cache, liveness,
                   discovery suppression, re-announce, the config entry
  model.py         Pure logic: topics, payload parsing, heartbeats and boots,
                   entity_id claim planning
  entity.py        Base entity: availability, restore-on-start, the command echo window
  const.py         Configuration schema and the validators adapters build options from
  config_flow.py   The config entry, created from YAML
  binary_sensor.py climate.py fan.py light.py number.py
  select.py sensor.py switch.py valve.py              Platform adapters
  brand/           Icon and logo, light and dark, from i3 Engineering's own logo
```

## Behaviour the controller forces

| Observation | What the integration does about it |
|---|---|
| Publishes on change and retains nothing | Restores the last known value of anything that changes only when acted on (see [What is restored](#what-is-restored)) |
| Command-to-state echo takes about one second (the controller's publish tick, not network jitter) | Shows the commanded value immediately. The echo confirms it; with no echo within about 3 s, the entity goes back to what the controller last said. See [Commands](#commands) |
| Per-channel `.../available` topics are published but never retained | Availability comes from the controller's uptime counter, which is published unconditionally |
| Heartbeats arrive about every 60 s, round-robin, with a per-device phase offset | Heartbeat timeouts have a floor of 180 s, because anything lower flaps |
| A controller that goes quiet sends no message | Liveness is rechecked every 30 s, so a silent controller does go unavailable |
| The uptime counter going backwards is the only sign of a restart | Logged as a warning. Boot times are also remembered across Home Assistant restarts, so a reboot that happened while Home Assistant was down is caught on the first heartbeat |
| An unconfigured channel publishes `-nan` | Treated as silence: the last value stays, rather than being read as a number |
| An empty payload is a request to re-announce | Ignored entirely: it is never cached and never read as a value |
| The leaf is not always `value`: one family publishes `position`, and a thermostat channel publishes a mode, two setpoints and a temperature | The unit of subscription is a leaf, and an entity may bind several |
| Some channels are announced over MQTT discovery under the same unique_ids | Where Home Assistant consumes that discovery, duplicates of entities declared here are deleted, repeatedly, because the controller re-announces on every reconnect. See [Vendor discovery](#vendor-discovery) |

## Topic grammar

Note the leading slash, which creates an empty first topic level. The controller
publishes that way, so the integration reproduces it exactly.

```
/devices/<client>/events/<DEV>/<type>/<n>/<leaf>      state
/devices/<client>/commands/<DEV>/<type>/<n>/<leaf>    command
/devices/<client>/events/<DEV>/system/1/uptime        heartbeat
```

`<client>` is the MQTT client id configured in the controller. It sets both the
topic root and the unique_id stem. If it is wrong, the integration subscribes
to a tree that does not exist, and no unique_id matches an entity.

Leaves seen on one controller family:

| Channel type | Leaves |
|---|---|
| `out`, `pwm`, `adc_tempr`, `mb_float`, `mb_u16`, `var_int`, `0-10v` | `value` (state), `set` (command) |
| `twv` | `position` |
| `comparator` | `value`, `set`, `enable` |
| `comparator_v2` | `mode`, `setpoint_heating`, `setpoint_cooling`, `temp` |

## Install

Requirements: Home Assistant 2026.8.0 or later, with the MQTT integration
configured and connected to the broker the controller publishes to.

- **HACS:** add `https://github.com/oleks-p/ha-i3-integration` as a custom
  repository (category *Integration*), install **i3 Atom**, then restart Home
  Assistant.
- **Manually:** copy `custom_components/i3_atom/` into your configuration's
  `custom_components/` directory, then restart.

Configuration is YAML. On the first start with an `i3_atom:` block, the block
is imported into a single **i3 Atom** entry under *Settings → Devices &
services*. The entry holds nothing of its own; the YAML stays the source of
truth. Adding the integration from the UI only points you back to YAML.

After you change the YAML, call the **`i3_atom.reload`** action (or restart).
It re-reads the block and reloads the entry.

## Configuration

```yaml
i3_atom:
  client_id: i3
  controllers:
    - id: A1B2
      heartbeat_timeout_s: 240
  entities:
    - platform: light                       # relay: on/off
      unique_id: i3_A1B2_out_9
      object_id: hall_sconce
      name: Hall sconce
      icon: mdi:wall-sconce
      reads:  {state: A1B2/out/9/value}
      writes: {command: A1B2/out/9/set}

    - platform: light                       # dimmer: the channel's full scale is 100
      unique_id: i3_A1B2_pwm_24
      object_id: hall_strip
      name: Hall LED strip
      options: {brightness_scale: 100}
      reads:  {state: A1B2/pwm/24/value}
      writes: {command: A1B2/pwm/24/set}

    - platform: binary_sensor               # controller liveness: binds no points
      unique_id: i3_A1B2_online
      object_id: i3_a1b2_online
      name: i3 A1B2 online
      options: {controller: A1B2}
```

### Top-level keys

| Key | Default | Meaning |
|---|---|---|
| `client_id` | `i3` | The MQTT client id configured in the controller (see [Topic grammar](#topic-grammar)) |
| `controllers` | `[]` | Controllers and their heartbeat settings. A controller that an entity references but that is not listed here gets the default timeout |
| `controllers[].id` | required | Controller id, e.g. `A1B2` |
| `controllers[].heartbeat_timeout_s` | `240` | Seconds without a heartbeat before the controller's entities become unavailable. Minimum `180` |
| `entities` | `[]` | The entities, described below |
| `suppress_vendor_discovery` | `true` | Delete the controller's own retained MQTT discovery configs for unique_ids declared here. Does nothing where Home Assistant consumes no discovery. See [Vendor discovery](#vendor-discovery) |
| `reannounce` | `false` | Ask the controller to re-publish every bound leaf. See [Re-announce](#re-announce) |
| `group_by_controller` | `false` | Put each entity on a device per controller. See [Devices](#devices) |

Home Assistant merges this block across [packages](https://www.home-assistant.io/docs/configuration/packages/),
so one entity or controller can be listed in several of them:

- Identical duplicates collapse silently.
- An entity declared twice with different settings is logged as an error, and
  the first declaration is used.
- Two entities that want the same entity_id are logged as an error. The second
  gets a generated id.
- A controller listed with different timeouts gets the larger one, and an error
  is logged.

### Entity keys

| Key | Required | Meaning |
|---|---|---|
| `platform` | yes | One of `binary_sensor`, `climate`, `fan`, `light`, `number`, `select`, `sensor`, `switch`, `valve`. Anything else fails the configuration check |
| `unique_id` | yes | Registry identity. Reuse the unique_id of the `mqtt:` entity being replaced: its old row is then recognised and released, and the entity_id, with its history, carries over |
| `object_id` | yes | Fixes the entity_id as `<platform>.<object_id>` (see [Entity IDs](#entity-ids)) |
| `name` | yes | Friendly name. Used as written unless `translation_key` is set, and then only as the fallback when no translation resolves |
| `translation_key` | no | Names the entity from this integration's translation bundle instead, in the language of whoever is looking (see [Translated entity names](#translated-entity-names)) |
| `icon` | no | `mdi:` icon |
| `reads` | no | Role → point to subscribe to |
| `writes` | no | Role → point to publish commands on |
| `options` | no | Platform-specific settings, listed below |

A point is `<CONTROLLER>/<channel type>/<number>/<leaf>`, such as
`A1B2/out/9/value` or `E5F6/0-10v/1/set`. It is validated when the
configuration is loaded, and an invalid point is refused rather than repaired:
a command sent to the wrong channel type is accepted by the broker and does
nothing.

`reads` and `writes` are separate maps because the core has to know which
points to subscribe to and which to publish on. Inferring that from a role name
is a rule somebody eventually breaks. The role names belong to the platform
that consumes them and mean nothing to the core.

Each platform validates its own `options` when its entities are created.
Unknown keys are refused, so a misspelt option does not silently become its
default. An entity with invalid options is left out and named in the log; the
rest of its platform still loads. Any entity may also set `controller: <ID>`
to depend on a controller it binds no point on.

> Quote `"off"`, `"on"`, `"yes"` and `"no"` wherever they are values (mode
> lists, maps, options). YAML otherwise reads them as booleans. They are refused
> with a message that says so.

### Platforms

| Platform | Reads | Writes | Options |
|---|---|---|---|
| `light` | `state` | `command` | `brightness_scale`: the channel's full-scale value. If set, the light is dimmable; if not, it is on/off |
| `switch` | `state` | `command` | `device_class` (`outlet`, `switch`), `invert` (on at zero) |
| `binary_sensor` | `state` | none | `device_class`, `invert` (report zero as on). With `controller: <ID>` and no reads, it reports that controller's liveness instead |
| `sensor` | `state` | none | `unit`, `device_class`, `state_class`, `precision`; `sane: [low, high]` to discard implausible readings; `map: {payload: word}` for a coded value, which turns it into an enum sensor (unit and state class are then dropped) |
| `number` | `state` | `command` | `min` (0), `max` (100), `step` (1), `unit`, `device_class`, `mode` (`auto`, `box`, `slider`), `sane` |
| `select` | `state` | `command` | `options`: the words offered; `map`: `{payload: word}`, used in both directions, and every word it gives must be in `options` |
| `fan` | `state` | `command` | `speed_range_max`: the channel's full-scale value (default `100`). Zero is off; turning on without a percentage restores the last non-zero level. A channel coarser than a percent offers only its own steps |
| `valve` | `state` | `command` (optional) | `position_scale`: the channel's full-scale value (default `100`); `device_class` (`water`, `gas`). Without `command` the valve is shown but not driven |
| `climate` | `mode`, `setpoint`, `setpoint_low`, `setpoint_high`, `current_temperature`, `fan_mode` | `mode`, `power`, `setpoint`, `setpoint_low`, `setpoint_high`, `fan_mode` | `modes`, `mode_map`, `mode_from_presence` + `on_mode`, `fan_modes`, `fan_mode_map`, `min_temp` (7), `max_temp` (35), `temp_step` (0.5), `sane` |

A non-zero level never rounds to zero: the lowest brightness on a coarse dimmer
turns it on at its lowest step, not off.

The climate adapter covers three thermostat shapes, told apart by which roles
are bound:

- **Mode and setpoints on separate leaves** (`comparator_v2`): `mode` through
  `mode_map`, plus `setpoint_low`/`setpoint_high` for a dual-setpoint
  thermostat or `setpoint` for a single target. A `mode_map` may name modes the
  thermostat does not offer; such a payload is ignored, with one warning.
- **Power only:** `writes.power` takes `1`/`0` where the mode cannot be written
  directly.
- **Mode from presence** (`comparator`): no mode leaf at all. A readable
  setpoint means `on_mode`, and `-nan` means off. Set `mode_from_presence: true`.

Each adapter's module docstring is the authoritative list of its roles and
options.

<details>
<summary>More examples</summary>

```yaml
    - platform: sensor                      # a measurement
      unique_id: i3_A1B2_adc_tempr_3
      object_id: hall_temperature
      name: Hall temperature
      reads: {state: A1B2/adc_tempr/3/value}
      options: {unit: "°C", device_class: temperature, state_class: measurement,
                precision: 1, sane: [-30, 60]}

    - platform: sensor                      # a code: restored after a restart
      unique_id: i3_C3D4_mb_u16_12
      object_id: mixing_valve_direction
      name: Mixing valve direction
      reads: {state: C3D4/mb_u16/12/value}
      options:
        map: {"0": idle, "1": opening, "2": closing}

    - platform: switch                      # the same relay type, driving a pump
      unique_id: i3_A1B2_out_3
      object_id: circulation_pump
      name: Circulation pump
      reads:  {state: A1B2/out/3/value}
      writes: {command: A1B2/out/3/set}

    - platform: number
      unique_id: i3_C3D4_var_int_7
      object_id: dhw_setpoint
      name: Hot water setpoint
      reads:  {state: C3D4/var_int/7/value}
      writes: {command: C3D4/var_int/7/set}
      options: {min: 30, max: 65, step: 0.5, unit: "°C", mode: box}

    - platform: valve
      unique_id: i3_C3D4_twv_1
      object_id: mixing_valve
      name: Mixing valve
      reads: {state: C3D4/twv/1/position}
      options: {position_scale: 100, device_class: water}

    - platform: select
      unique_id: i3_C3D4_var_int_1
      object_id: house_mode
      name: House mode
      reads:  {state: C3D4/var_int/1/value}
      writes: {command: C3D4/var_int/1/set}
      options:
        options: [home, away, vacation]
        map: {"0": home, "1": away, "2": vacation}

    - platform: fan
      unique_id: i3_E5F6_0-10v_1
      object_id: supply_fan
      name: Supply fan
      reads:  {state: E5F6/0-10v/1/value}
      writes: {command: E5F6/0-10v/1/set}
      options: {speed_range_max: 100}

    - platform: climate                     # comparator_v2, dual setpoint
      unique_id: i3_C3D4_comparator_v2_3
      object_id: living_room
      name: Living room
      reads:
        mode: C3D4/comparator_v2/3/mode
        setpoint_low: C3D4/comparator_v2/3/setpoint_heating
        setpoint_high: C3D4/comparator_v2/3/setpoint_cooling
        current_temperature: C3D4/comparator_v2/3/temp
      writes:
        mode: C3D4/comparator_v2/3/mode
        setpoint_low: C3D4/comparator_v2/3/setpoint_heating
        setpoint_high: C3D4/comparator_v2/3/setpoint_cooling
      options:
        modes: ["off", heat, cool, heat_cool]
        mode_map: {"0": "off", "1": heat, "2": cool, "3": heat_cool}
        min_temp: 5
        max_temp: 35
        sane: [5, 40]
```

</details>

### Generating the configuration

For a large installation the block is easier to generate than to write by
hand. It is plain data, and everything in it is validated when Home Assistant
loads it: an invalid point or platform fails the configuration check, and an
entity with invalid options is named in the log.

## What is restored

Everything an entity last held comes back, measurements included.

The reason is the transport. This controller publishes a leaf **only when that
leaf changes** and retains nothing, so silence does not mean "no data", it
means "unchanged" - and a restored reading is therefore not a stale guess about
the present, it is the present, for as long as the controller stays quiet. The
flat line a restored measurement draws into history is the one that actually
happened.

Until 0.3.0 a measurement was left blank to wait for its own cadence. On a
steady house that cadence is hours: at one installation, twenty minutes after a
restart, 17 of 22 numeric sensors were still blank and all ten underfloor
thermostats were showing a setpoint with no room temperature beside it, because
nothing in the house had changed enough to publish.

Silence cannot distinguish a quiet controller from a dead one, and nothing here
has to: availability follows the controller's heartbeat, so a controller that
goes away takes its entities unavailable whatever they last held.

| Entity | Restored after a restart |
|---|---|
| `light` | On/off and brightness |
| `switch` | On/off |
| `binary_sensor` (point) | On/off |
| `binary_sensor` (controller liveness) | Nothing: it follows the heartbeat |
| `sensor` with `map` | The word, if the map still offers it |
| `sensor` without `map` | The reading, if `sane` still admits it |
| `number` | The value, in the channel's own unit |
| `select` | The option, if it is still offered |
| `fan` | Percentage |
| `valve` | Position |
| `climate` | Mode, setpoints, fan mode and `current_temperature` |

The restored value is applied first. A live payload that has already arrived
then replaces it, unless that payload is only silence (`-nan`).

## Commands

A command shows its value immediately and sets `assumed_state` until the
controller answers on the same channel (a relay commanded on `set` answers on
`value`). If no answer arrives within three publish ticks (about 3 s), the
entity drops `assumed_state` and goes back to the last value the controller
actually published. The usual reason for no answer is that nothing changed:
the controller publishes only on change, so commanding the value a channel
already holds is never echoed. Heartbeats and other channels do not disturb a
command in flight.

## Availability

An entity is available while every controller it depends on is live: the
controllers its points address, plus `options.controller` if set. A controller
is live while its last heartbeat is younger than `heartbeat_timeout_s`. Before
the first heartbeat after a start, it counts as live, so that entities do not
all go unavailable for a minute after every restart.

The controller-liveness binary sensor is always available itself. Its
attributes are `booted_at` (when the controller last booted) and
`heartbeat_timeout_s`. It does not show the raw uptime counter, which changes
every heartbeat and would write a new state, and a new recorder row, every
minute.

Boot times are kept in `.storage/i3_atom.controllers`. On the first heartbeat
after Home Assistant starts, a boot later than the remembered one means the
controller restarted while Home Assistant was down, and that is logged.

## Re-announce

With `reannounce: true`, the integration publishes an empty payload to the
event topic of every leaf it reads, which asks the controller to publish the
current value. That turns a restored, last-known value into a confirmed one
shortly after:

- Home Assistant starts or the entry reloads
- the MQTT broker reconnects
- a controller restarts

It is off by default because it is the only thing that writes into the
controller's tree. Empty payloads coming back on those topics are ignored, so a
request is never mistaken for a value. Try it on one site and watch the broker
before relying on it.

## Vendor discovery

The controller can announce some of its channels over MQTT discovery, under the
same unique_ids this integration uses. None of the entities here depend on it:
they are built from the YAML and read the state topics directly.

It matters only because Home Assistant's own MQTT integration turns those
announcements into entities, which would duplicate ours. So the integration
watches exactly what Home Assistant's MQTT integration consumes, as set under
*Settings → Devices & services → MQTT → Configure*:

| Home Assistant MQTT discovery | Controller's discovery | What happens |
|---|---|---|
| Off | Anything | Nothing is watched. Nothing can be duplicated |
| On, prefix P | Off, or another prefix | P is watched, but nothing of the controller's arrives there, so nothing is deleted |
| On, prefix P | On, prefix P | Configs on P for unique_ids declared here are deleted. Anything else is left alone |

So there is nothing to keep in step with the controller's app. Changing Home
Assistant's MQTT discovery settings takes effect after `i3_atom.reload`.

Channels the controller announces but the YAML does not declare are never
touched, and stay as the MQTT integration's entities. The deletion is a
retained empty publish, so it affects the whole broker. If you remove this
integration, the controller's entities for those channels come back the next
time it re-announces, which it does when it reconnects.

## Devices

With `group_by_controller: true`, each controller is a device (`i3 A1B2`), and
every entity joins the device of the controller it commands, or else the one it
reads. You can delete a device from the UI once no controller in the YAML
refers to it.

It is off by default because Home Assistant prefixes the friendly name of an
entity that has a device with the device's name. Turning it on renames every
entity wherever a dashboard shows the name: `Hall sconce` becomes
`i3 A1B2 Hall sconce`, and `i3 A1B2 online` becomes `i3 A1B2 Online`. Entity
ids do not change.

## Entity IDs

`object_id` fixes the entity_id instead of letting Home Assistant generate one,
because dashboards and automations reference entities by id.

Taking over a point that an `mqtt:` entity used to declare creates a second
registry row, because unique_ids are unique per platform. The old row keeps the
entity_id, and Home Assistant would name the new entity `..._2`. To prevent
this, each platform reconciles the registry before it creates its entities:

- A row on **another platform** with one of our unique_ids is that point's
  previous declaration. It is removed.
- A row of **ours** parked on a suffixed id is renamed to the configured id,
  keeping its history.
- An id held by **something unrelated** is left alone. The clash is logged as
  an error, and our entity gets a generated id until the configuration is
  fixed.

The pass makes no changes once the registry is settled.

## Logs worth knowing

| Level | Message starts with | Meaning |
|---|---|---|
| WARNING | `controller X restarted` | The uptime counter went backwards, or, with `while Home Assistant was not running`, the controller booted during the outage. Values on X are last known until each topic next changes |
| WARNING | `controller X has sent no heartbeat for more than Ns` | X is offline. Its entities are unavailable |
| INFO | `controller X is live again` | Heartbeats resumed |
| WARNING | `<entity>: mode payload … is not one of its modes` | The thermostat reported a mode it was not declared with. The last mode stands |
| ERROR | `<unique_id> is not created: its options are invalid` | Fix that entity's `options`. The rest of its platform loaded |
| ERROR | `<unique_id> is declared twice with different settings` | Two packages disagree. The first declaration is used |
| ERROR | `… both want <entity_id>` | Two entities are configured with the same entity_id |
| ERROR | `controller X is declared with two heartbeat timeouts` | Packages disagree. The larger timeout is used |
| ERROR | `cannot take <entity_id>` | The configured entity_id belongs to an unrelated entity. Rename one of them |
| INFO | `releasing` / `renaming` | The entity_id reconciliation described above |
| INFO | `removing the controller's duplicate discovery` | Vendor discovery suppression at work |

To see the INFO lines:

```yaml
logger:
  logs:
    custom_components.i3_atom: info
```

## Areas and aliases

An entity's **area** and its voice **aliases** live in Home Assistant's entity
registry, and no YAML platform can set either. An integration that owns its
entities can, so both are declared per entity and applied when the entity is
added:

```yaml
i3_atom:
  entities:
    - platform: light
      unique_id: i3_A1B2_pwm_24
      object_id: hall_strip
      name: Світильники передпокій
      area: Передпокій
      aliases: ["Hallway lights"]
      reads: {state: A1B2/pwm/24/value}
      writes: {command: A1B2/pwm/24/set}
```

- The area is named, not identified. A name that does not exist yet is
  created; one that does is reused, so a room somebody made by hand is the
  room these entities join.
- The configuration is the source of truth, the same way it is for `name`: a
  value that differs is written on the next start. A change made by hand in
  the interface is therefore put back.
- **An absent key changes nothing.** Most entities name no area, and silence
  is not an instruction to clear one - an assignment made deliberately in the
  interface survives.

Before this, the only way to set them was a separate tool talking to the
websocket API after the entities existed. That made a rebuild a procedure whose
last step is the one people forget, and an installation that skipped it had
every entity in no area at all.

## Upgrading from 0.3

- `area` and `aliases` are accepted per entity and applied to the registry on
  start. Nothing changes for an entity that declares neither.

## Upgrading from 0.2

- Measurements are restored on start. A `sensor` without `map` comes back with
  its last reading instead of `unknown`, and a `climate` entity comes back with
  its `current_temperature`. A restored reading still has to pass `sane`.
- Nothing else changes. No entity_id, unique_id or option moves.

## Upgrading from 0.1

- An **i3 Atom** entry is created on the first start. Existing registry rows
  are adopted by it and keep their entity_ids and history.
- The liveness sensor's `uptime_s` attribute is replaced by `booted_at`.
- Options are now validated. An entity with an unknown or invalid option is
  left out and logged instead of loading half-configured. An unknown
  `platform` fails the configuration check.
- `light`, `fan` and `valve` never round a non-zero level down to zero.

## Limitations

- YAML only: there is no options UI, and the UI flow only points to YAML.
- `reannounce` relies on the controller treating an empty publish as a
  re-announce request. Verify that on your firmware.

## Development

The tests run against a real Home Assistant (pinned by
`pytest-homeassistant-custom-component`) with a mocked MQTT broker, and need
Python 3.14:

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check custom_components tests
```

- `tests/test_model.py` covers the pure logic in `model.py`. Each test there
  corresponds to a failure that would otherwise be silent in the field.
- `tests/test_integration.py` runs the integration in Home Assistant: setup,
  restore, the command echo window, liveness, entity_id claiming, discovery
  suppression, re-announce, every platform and reload.
- `tests/test_adapters.py` checks the adapter sources statically, for
  declaration faults that are cheaper to catch there.

CI (`.github/workflows/`) runs hassfest, HACS validation, pytest and ruff.

### Translated entity names

Home Assistant stores one display name per entity, so by default `name` decides
what every viewer reads, whatever language they use the interface in. An
integration can do better: with `translation_key` set, Home Assistant resolves
the name from this integration's own bundle, per the profile language of
whoever is looking.

```yaml
i3_atom:
  entities:
    - platform: light
      unique_id: i3_A1B2_out_9
      object_id: hall_sconce
      name: Hall sconce
      translation_key: hall_sconce
      writes: {command: A1B2/out/9/set}
```

```json
// custom_components/i3_atom/translations/uk.json
{"entity": {"light": {"hall_sconce": {"name": "Світильники передпокій"}}}}
```

The key is scoped to the platform, so `light.hall_sconce` and
`sensor.hall_sconce` are separate keys. Using the `object_id` as the key is the
obvious choice and keeps the two from drifting.

**`name` and `translation_key` are not alternatives you pick between at
runtime.** Home Assistant returns `_attr_name` before it ever consults a
bundle, so an entity cannot carry both - with a key, the configured `name` is
held back and used only if the lookup produces nothing. That fallback matters
more than it sounds: the bundle lives in this directory, so **updating the
integration replaces it**, and without the fallback every entity would come
back as its object id with the underscores turned into spaces, with
`friendly_name` dropped from the state entirely. With it, a missing bundle is
simply the old behaviour - one language for everybody - which is a soft
failure you can see and fix rather than a board full of `hall sconce`.

So a bundle that is generated per installation has to be reinstalled after
every update of this integration. If you generate yours, make that check part
of the upgrade.

### Releasing

HACS offers the newest **release**, not the newest tag. A tag pushed on its own
leaves every installation still being offered the previous version - or the
default branch, if nothing has been released yet - and nothing reports that as
a problem, so the version people actually get is whatever was last published.

So the tag is the whole procedure:

```sh
# bump "version" in custom_components/i3_atom/manifest.json first
git tag v0.3.0
git push origin v0.3.0
```

`release.yaml` checks the tag against the manifest version, refuses the release
if they disagree, and publishes it with generated notes if they match. A release
created by hand in the web UI is gated by the same check.
