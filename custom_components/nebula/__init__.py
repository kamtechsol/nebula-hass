"""The Nebula integration.

Gives the Nebula app + panel one place to talk to: a combined snapshot and
delta stream over Home Assistant's authenticated WebSocket API, a small REST
surface, LAN discovery via zeroconf, and connection-state sensors.
"""

from __future__ import annotations

import logging
import socket

import voluptuous as vol

from homeassistant.components import persistent_notification, zeroconf
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import network

from .api import async_register_http
from .const import DATA_MANAGER, DOMAIN, ZEROCONF_TYPE
from .manager import NebulaManager
from .websocket_api import async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Nebula from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})

    manager = NebulaManager(hass, entry.entry_id)
    manager.async_start()
    domain_data[entry.entry_id] = {DATA_MANAGER: manager}

    # HTTP + WS command surfaces are process-wide; register once.
    if not domain_data.get("_http_registered"):
        async_register_http(hass)
        async_register_websocket(hass)
        _async_register_services(hass)
        domain_data["_http_registered"] = True

    await _async_advertise(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_reload))

    async def _stop(_event) -> None:
        manager.async_stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = hass.data[DOMAIN].pop(entry.entry_id, None)
    if data:
        data[DATA_MANAGER].async_stop()

    aiozc = await zeroconf.async_get_async_instance(hass)
    info = hass.data[DOMAIN].pop("_zc_info", None)
    if info is not None:
        await aiozc.async_unregister_service(info)

    return unload_ok


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """`nebula.pair_code` — get a pairing PIN without needing the panel."""

    async def _pair_code(call: ServiceCall) -> ServiceResponse:
        manager = None
        for data in hass.data.get(DOMAIN, {}).values():
            if isinstance(data, dict) and DATA_MANAGER in data:
                manager = data[DATA_MANAGER]
                break
        if manager is None or call.context.user_id is None:
            return {"error": "unavailable"}
        pin = manager.new_pin(call.context.user_id)
        persistent_notification.async_create(
            hass,
            f"Enter this in the Nebula app within 5 minutes:\n\n# {pin}",
            title="Nebula pairing PIN",
            notification_id="nebula_pair_code",
        )
        return {"pin": pin, "expires_in": 300}

    hass.services.async_register(
        DOMAIN,
        "pair_code",
        _pair_code,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.OPTIONAL,
    )


async def _async_advertise(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Publish `_nebula._tcp` so the app can find this instance with no typing."""
    try:
        from zeroconf import ServiceInfo

        aiozc = await zeroconf.async_get_async_instance(hass)
        try:
            base_url = network.get_url(
                hass, allow_internal=True, allow_external=False, prefer_external=False
            )
        except network.NoURLAvailableError:
            base_url = network.get_url(hass, allow_external=True)

        port = hass.http.server_port or 8123
        host_ip = await async_get_source_ip(hass)
        instance = hass.config.location_name or "Home Assistant"

        info = ServiceInfo(
            ZEROCONF_TYPE,
            name=f"{instance}.{ZEROCONF_TYPE}",
            addresses=[socket.inet_aton(host_ip)] if host_ip else [],
            port=port,
            properties={
                "base_url": base_url,
                "uuid": entry.entry_id,
                "location": instance,
                "auth": "pin",  # /api/nebula/pair PIN exchange available
                "version": "0.1.0",
            },
            server=f"nebula-{entry.entry_id[:8]}.local.",
        )
        await aiozc.async_register_service(info, allow_name_change=True)
        hass.data[DOMAIN]["_zc_info"] = info
        _LOGGER.debug("Advertised %s at %s", ZEROCONF_TYPE, base_url)
    except Exception:  # noqa: BLE001 - discovery is best-effort
        _LOGGER.warning("Nebula: could not advertise over zeroconf", exc_info=True)


async def async_get_source_ip(hass: HomeAssistant) -> str | None:
    try:
        from homeassistant.helpers.network import async_get_source_ip as _get

        return await _get(hass)
    except Exception:  # noqa: BLE001
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None
