"""Config flow for Nebula. Single-instance, no user input required."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


class NebulaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nebula."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Nebula", data={})

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
