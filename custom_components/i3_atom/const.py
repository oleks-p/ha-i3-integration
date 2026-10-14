"""Configuration keys, schema, and the validators adapters build options from.

The schema is deliberately domain-free: an entity declares which platform it is
presented as and which topic leaves it binds, and nothing here knows what any
platform does with them. Adding support for another domain is a new file beside
`light.py`, not a change to this one - the platform list below is read from the
files that exist.
"""
from __future__ import annotations

from pathlib import Path

try:
    # Home Assistant validates configuration with probatio from 2026.9 and no
    # longer depends on voluptuous itself; it is importable there only because
    # other packages happen to pull it in. The two share an API, and mixing
    # them inside one schema would hand Home Assistant's own validators to the
    # wrong library.
    import probatio as vol
except ImportError:  # Home Assistant 2026.8
    import voluptuous as vol

from homeassistant.const import (
    CONF_ICON,
    CONF_ID,
    CONF_NAME,
    CONF_PLATFORM,
    CONF_UNIQUE_ID,
    Platform,
)
from homeassistant.helpers import config_validation as cv

from .model import DEFAULT_HEARTBEAT_TIMEOUT_S, MIN_HEARTBEAT_TIMEOUT_S

__all__ = ["vol"]

DOMAIN = "i3_atom"

CONF_CLIENT_ID = "client_id"
CONF_CONTROLLERS = "controllers"
CONF_ENTITIES = "entities"
CONF_GROUP_BY_CONTROLLER = "group_by_controller"
CONF_HEARTBEAT_TIMEOUT = "heartbeat_timeout_s"
CONF_OBJECT_ID = "object_id"
CONF_OPTIONS = "options"
CONF_READS = "reads"
CONF_REANNOUNCE = "reannounce"
CONF_SUPPRESS_VENDOR_DISCOVERY = "suppress_vendor_discovery"
CONF_WRITES = "writes"

#: The MQTT client id configured in the controller. It is the one setting behind
#: both installation-specific strings: the topic root and the unique_id stem.
#: Subscribing with the wrong one means subscribing to a tree that does not
#: exist, while every unique_id belongs to no entity.
DEFAULT_CLIENT_ID = "i3"

#: The platforms this build ships: every Home Assistant platform with an
#: adapter file beside this one. Checked when the configuration is loaded, so
#: an entity declared as something this build cannot present is refused by
#: `check_config` with its name, rather than failing a platform load later.
PLATFORMS: tuple[str, ...] = tuple(sorted(
    p.value for p in Platform if Path(__file__).with_name(f"{p.value}.py").exists()))

#: `<DEV>/<type>/<n>/<leaf>`. Validated properly in model.parse_point; this is
#: only the shape check the configuration layer can give in a useful message.
#: A channel type can contain a hyphen and can begin with a digit - `0-10v` is
#: a real one.
POINT = vol.Match(r"^[A-Za-z0-9]+/[a-z0-9_-]+/\d+/[a-z0-9_]+$",
                  msg="expected <CONTROLLER>/<channel>/<number>/<leaf>")

POINT_MAP = vol.Schema({cv.string: POINT})

ENTITY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PLATFORM): vol.In(PLATFORMS),
        vol.Required(CONF_UNIQUE_ID): cv.string,
        vol.Required(CONF_OBJECT_ID): cv.string,
        vol.Required(CONF_NAME): cv.string,
        # Separate maps rather than one with a naming convention: the core has
        # to know which points to subscribe to and which to publish on, and
        # inferring that from a role name is a rule somebody eventually breaks.
        vol.Optional(CONF_READS, default={}): POINT_MAP,
        vol.Optional(CONF_WRITES, default={}): POINT_MAP,
        # Whatever the platform needs and the core does not read. Each adapter
        # validates its own, as its OPTIONS, when its entities are built.
        vol.Optional(CONF_OPTIONS, default={}): dict,
        vol.Optional(CONF_ICON): cv.icon,
    }
)

CONTROLLER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ID): cv.string,
        vol.Optional(
            CONF_HEARTBEAT_TIMEOUT, default=DEFAULT_HEARTBEAT_TIMEOUT_S
        ): vol.All(int, vol.Range(min=MIN_HEARTBEAT_TIMEOUT_S)),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_CLIENT_ID, default=DEFAULT_CLIENT_ID): cv.string,
                vol.Optional(CONF_CONTROLLERS, default=[]): [CONTROLLER_SCHEMA],
                vol.Optional(CONF_ENTITIES, default=[]): [ENTITY_SCHEMA],
                # Default on: an installation that declares entities here and
                # leaves the controller's duplicates in place gets two per
                # channel and the wrong one wins. Switch it off where the
                # controller's own entities are the ones in use. Where Home
                # Assistant consumes no discovery it does nothing either way;
                # see Hub._watched_discovery_prefix.
                vol.Optional(CONF_SUPPRESS_VENDOR_DISCOVERY, default=True): cv.boolean,
                # Default off: Home Assistant prefixes the friendly name of an
                # entity that has a device with the device's name, so turning
                # this on renames every entity - `Hall sconce` becomes
                # `i3 A1B2 Hall sconce`, and even `i3 A1B2 online` becomes
                # `i3 A1B2 Online` - wherever a dashboard shows the name.
                vol.Optional(CONF_GROUP_BY_CONTROLLER, default=False): cv.boolean,
                # Default off: asking the controller to re-announce writes to
                # its event topics, which nothing else here does. See
                # Hub._request_reannounce.
                vol.Optional(CONF_REANNOUNCE, default=False): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


# ── validators for adapter options ──────────────────────────────────────────

def word(value):
    """A word from configuration: a mode, an option, a mapped value.

    YAML reads a bare `off`, `on`, `yes` or `no` as a boolean, and the string
    of that is `False` - a mode nobody declared, which then fails somewhere far
    from the line that caused it. Refused here, with the fix in the message.
    """
    if isinstance(value, bool):
        raise vol.Invalid("a bare on/off/yes/no is read by YAML as a boolean; quote it")
    return cv.string(value)


def payload_key(value) -> str:
    """A payload as a map key: the text the controller publishes.

    `{0: idle}` and `{"0": idle}` mean the same thing to whoever wrote them,
    and only the second matches what arrives on the topic.
    """
    if isinstance(value, bool):
        raise vol.Invalid("a bare on/off/yes/no is read by YAML as a boolean; quote it")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def payload_map(values=word):
    """payload -> word, as configuration writes a coded value."""
    return vol.Schema({payload_key: values})


def _ordered(pair):
    low, high = pair
    if low > high:
        raise vol.Invalid(f"sane range [{low}, {high}] is upside down")
    return (low, high)


#: [low, high] outside which a payload is data rather than a reading.
SANE = vol.All(vol.ExactSequence([vol.Coerce(float), vol.Coerce(float)]), _ordered)

#: A channel's own full-scale value. At least 1: zero would divide, and below
#: one no level could be expressed.
FULL_SCALE = vol.All(vol.Coerce(float), vol.Range(min=1))


def options_schema(fields: dict):
    """An adapter's options: its own fields, plus what the core reads.

    `controller` is the core's: it makes an entity depend on a controller it
    binds no point on (see EntitySpec.controllers), whatever its platform.
    Anything else not listed is refused, because a misspelt option is
    otherwise silently a default. An adapter that declares `controller`
    itself - as required, say - gets its own declaration: markers compare
    equal by name, so merging the two would quietly keep the first.
    """
    own = any(str(key) == "controller" for key in fields)
    base = {} if own else {vol.Optional("controller"): cv.string}
    return vol.Schema({**base, **fields})
