"""Config flow for Nebula.

Setup takes no input (single instance). The options flow shows the panel token
(the shared secret the Nebula panel uses on /api/nebula/panel) and lets you
regenerate it.
"""

from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_PANEL_TOKEN, DOMAIN


class NebulaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nebula."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Nebula", data={})

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return NebulaOptionsFlow(config_entry)


class NebulaOptionsFlow(OptionsFlow):
    """Show / regenerate the panel token."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        token = self.config_entry.options.get(CONF_PANEL_TOKEN, "")

        if user_input is not None:
            if user_input.get("regenerate"):
                token = secrets.token_hex(16)
            # Persist and reload so the panel channel picks up a new token.
            return self.async_create_entry(
                title="", data={**self.config_entry.options, CONF_PANEL_TOKEN: token}
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Optional("regenerate", default=False): bool}),
            description_placeholders={"token": token or "(generated on first start)"},
        )
