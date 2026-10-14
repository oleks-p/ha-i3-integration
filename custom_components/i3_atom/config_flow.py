"""The config entry, created from YAML and never from the UI.

The entry holds nothing. It exists because Home Assistant attaches devices
only to entities that belong to an entry, and because an entry can be
reloaded. The configuration itself stays in YAML: the entity list is long and
often generated, and a form is the wrong place to edit it.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class I3AtomConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="i3 Atom", data={})

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(reason="yaml_only")
