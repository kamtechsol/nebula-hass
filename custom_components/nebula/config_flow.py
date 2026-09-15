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
from homeassistant.helpers import selector

from .const import (
    CONF_ASSIST_ENABLED,
    CONF_ASSIST_FALLBACK_AGENT,
    CONF_ASSIST_LOCAL_FIRST,
    CONF_ASSIST_PERSONA,
    CONF_PANEL_TOKEN,
    CONF_SPOTIFY_CLIENT_ID,
    CONF_SPOTIFY_CLIENT_SECRET,
    DEFAULT_PERSONA,
    DOMAIN,
)


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
        return NebulaOptionsFlow()


class NebulaOptionsFlow(OptionsFlow):
    """Show / regenerate the panel token."""

    # `self.config_entry` is provided by the base OptionsFlow — do not assign it
    # (newer Home Assistant makes it a read-only property and setting it raises).

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        opts = self.config_entry.options
        token = opts.get(CONF_PANEL_TOKEN, "")

        if user_input is not None:
            if user_input.get("regenerate"):
                token = secrets.token_hex(16)
            # Persist and reload so the panel channel + Spotify broker pick up
            # any changes.
            return self.async_create_entry(
                title="",
                data={
                    **opts,
                    CONF_PANEL_TOKEN: token,
                    CONF_SPOTIFY_CLIENT_ID: (user_input.get(CONF_SPOTIFY_CLIENT_ID) or "").strip(),
                    CONF_SPOTIFY_CLIENT_SECRET: (
                        user_input.get(CONF_SPOTIFY_CLIENT_SECRET) or ""
                    ).strip(),
                    CONF_ASSIST_ENABLED: user_input.get(CONF_ASSIST_ENABLED, True),
                    CONF_ASSIST_LOCAL_FIRST: user_input.get(CONF_ASSIST_LOCAL_FIRST, True),
                    CONF_ASSIST_FALLBACK_AGENT: (
                        user_input.get(CONF_ASSIST_FALLBACK_AGENT) or ""
                    ).strip(),
                    CONF_ASSIST_PERSONA: (
                        user_input.get(CONF_ASSIST_PERSONA) or DEFAULT_PERSONA
                    ).strip(),
                },
            )

        try:
            from homeassistant.helpers import network

            base = network.get_url(
                self.hass, allow_internal=False, allow_external=True, require_ssl=True
            )
            redirect = base.rstrip("/") + "/api/nebula/spotify/callback"
        except Exception:  # noqa: BLE001
            redirect = "https://<your external HA URL>/api/nebula/spotify/callback"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional("regenerate", default=False): bool,
                    vol.Optional(
                        CONF_SPOTIFY_CLIENT_ID,
                        default=opts.get(CONF_SPOTIFY_CLIENT_ID, ""),
                    ): str,
                    vol.Optional(
                        CONF_SPOTIFY_CLIENT_SECRET,
                        default=opts.get(CONF_SPOTIFY_CLIENT_SECRET, ""),
                    ): str,
                    vol.Optional(
                        CONF_ASSIST_ENABLED,
                        default=opts.get(CONF_ASSIST_ENABLED, True),
                    ): bool,
                    vol.Optional(
                        CONF_ASSIST_LOCAL_FIRST,
                        default=opts.get(CONF_ASSIST_LOCAL_FIRST, True),
                    ): bool,
                    vol.Optional(
                        CONF_ASSIST_FALLBACK_AGENT,
                        default=opts.get(CONF_ASSIST_FALLBACK_AGENT, ""),
                    ): selector.selector(
                        {"entity": {"domain": "conversation"}}
                    ),
                    vol.Optional(
                        CONF_ASSIST_PERSONA,
                        default=opts.get(CONF_ASSIST_PERSONA, DEFAULT_PERSONA),
                    ): selector.selector({"text": {"multiline": True}}),
                }
            ),
            description_placeholders={
                "token": token or "(generated on first start)",
                "redirect_uri": redirect,
            },
        )
