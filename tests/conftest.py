"""Home Assistant with a mocked MQTT broker, and the integration's clocks."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.i3_atom.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def mqtt_mock(hass: HomeAssistant, mqtt_mock):
    """The harness's MQTT mock, minus the timer it leaves behind.

    The mocked client keeps the real one's one-second misc loop running after
    the test, and the harness's own lingering-timer check then fails on it - a
    test that requests nothing but `mqtt_mock` does it, with this integration
    not loaded at all.
    """
    yield mqtt_mock
    client = getattr(hass.data.get("mqtt"), "client", None)
    if client is not None and client._misc_timer is not None:
        client._misc_timer.cancel()
        client._misc_timer = None


@dataclass
class Clock:
    """The monotonic and wall clocks the hub judges liveness and boots by."""

    now: float = 1_000.0
    wall: float = 1_760_000_000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr("custom_components.i3_atom._now", lambda: c.now)
    monkeypatch.setattr("custom_components.i3_atom._wall", lambda: c.wall)
    return c


@pytest.fixture
async def setup_i3(
    hass: HomeAssistant, mqtt_mock, clock
) -> Callable[[dict], Awaitable[ConfigEntry]]:
    """Set the integration up from an `i3_atom:` block, as YAML would."""

    async def _setup(conf: dict) -> ConfigEntry:
        assert await async_setup_component(hass, DOMAIN, {DOMAIN: conf})
        await hass.async_block_till_done()
        entries = hass.config_entries.async_entries(DOMAIN)
        assert len(entries) == 1
        return entries[0]

    return _setup
