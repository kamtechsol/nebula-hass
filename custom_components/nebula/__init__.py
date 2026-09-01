"""The Nebula integration.

Gives the Nebula app + panel one place to talk to: a combined snapshot and
delta stream over Home Assistant's authenticated WebSocket API, a small REST
surface, LAN discovery via zeroconf, and connection-state sensors.
"""

from __future__ import annotations

import logging
import secrets
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
    callback,
)
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    network,
)

from .api import async_register_http
from .const import CONF_PANEL_TOKEN, DATA_MANAGER, DATA_PANEL, DOMAIN, ZEROCONF_TYPE
from .device import panel_device_info
from .manager import NebulaManager
from .panel import NebulaPanelView, PanelChannel
from .websocket_api import async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.MEDIA_PLAYER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Nebula from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})

    # Panel <-> integration shared secret. Auto-generate + persist once.
    panel_token = entry.options.get(CONF_PANEL_TOKEN)
    if not panel_token:
        panel_token = secrets.token_hex(16)
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PANEL_TOKEN: panel_token}
        )
    _LOGGER.info("Nebula panel token: %s", panel_token)

    # One process-wide panel channel.
    panel: PanelChannel = domain_data.get(DATA_PANEL) or PanelChannel(panel_token)
    panel.set_token(panel_token)
    domain_data[DATA_PANEL] = panel

    manager = NebulaManager(hass, entry.entry_id)
    manager.async_set_panel(panel)
    manager.async_start()
    domain_data[entry.entry_id] = {DATA_MANAGER: manager}

    # HTTP + WS command surfaces are process-wide; register once.
    if not domain_data.get("_http_registered"):
        async_register_http(hass)
        async_register_websocket(hass)
        _async_register_services(hass)
        hass.http.register_view(NebulaPanelView(panel))
        domain_data["_http_registered"] = True

    await _async_advertise(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the "Nebula Panel" device and hand its id to the panel channel.
    # The panel sends this device_id on every Assist pipeline run so HA's built-in
    # intents (timers/alarms, room-aware media) have a device to resolve against.
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, **panel_device_info()
    )
    panel.set_device_id(device.id)
    _bind_voice(hass, entry, panel, device.id)

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


@callback
def _bind_voice(
    hass: HomeAssistant, entry: ConfigEntry, panel: PanelChannel, device_id: str
) -> None:
    """Make HA's voice intents work *on the panel*:

    * expose ``media_player.nebula`` to the conversation agent so "play <song>"
      has a target, and
    * register a timer handler for the panel device so HA stops answering
      "this device is not able to set or manage timers/alarms" — timer events
      are forwarded down to the panel so it can show/ring them.
    """
    # --- expose the media player to Assist -------------------------------------
    try:
        from homeassistant.components.homeassistant.exposed_entities import (
            async_expose_entity,
        )

        ent_reg = er.async_get(hass)
        mp_id = ent_reg.async_get_entity_id(
            "media_player", DOMAIN, f"{entry.entry_id}_media_player"
        )
        if mp_id:
            for assistant in ("conversation", "cloud.alexa", "cloud.google_assistant"):
                try:
                    async_expose_entity(hass, assistant, mp_id, assistant == "conversation")
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Nebula: could not auto-expose media_player", exc_info=True)

    # --- timer / alarm handler ----------------------------------------------------
    try:
        from homeassistant.components.intent import async_register_timer_handler
    except ImportError:
        _LOGGER.debug("Nebula: this HA has no timer intent support")
        return

    @callback
    def _on_timer(event, timer) -> None:
        ev = getattr(event, "value", str(event)).lower()
        label = getattr(timer, "name", None) or ""
        if ev in ("started", "updated"):
            secs = int(getattr(timer, "seconds_left", 0) or getattr(timer, "seconds", 0) or 0)
            if secs <= 0:
                return
            hass.async_create_task(
                panel.send_command("timer_add", seconds=secs, label=label)
            )
        elif ev in ("cancelled", "finished"):
            hass.async_create_task(panel.send_command("timer_cancel"))

    try:
        entry.async_on_unload(
            async_register_timer_handler(hass, device_id, _on_timer)
        )
        _LOGGER.info("Nebula: timer handler registered for panel device %s", device_id)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Nebula: could not register timer handler", exc_info=True)


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

    async def _panel_command(call: ServiceCall) -> None:
        """Send a raw command frame down to the panel over its WebSocket.

        `action` picks the panel handler (transport, volume, source, play_query,
        timer_add, timer_cancel, alarm_add, alarm_cancel, dismiss, …); every
        other field is passed through as-is. Used by voice automations that used
        to poke the panel through the now-retired HA Companion app.
        """
        panel = hass.data.get(DOMAIN, {}).get(DATA_PANEL)
        if panel is None:
            _LOGGER.warning("nebula.panel_command: no panel channel")
            return
        data = dict(call.data)
        action = data.pop("action")
        ok = await panel.send_command(action, **data)
        if not ok:
            _LOGGER.warning("nebula.panel_command: panel offline, dropped %s", action)

    hass.services.async_register(
        DOMAIN,
        "panel_command",
        _panel_command,
        schema=vol.Schema(
            {vol.Required("action"): cv.string}, extra=vol.ALLOW_EXTRA
        ),
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

        from .manager import remote_ui_url

        props = {
            "base_url": base_url,
            "uuid": entry.entry_id,
            "location": instance,
            "auth": "pin",           # POST /api/nebula/pair
            "app_path": "/api/websocket",   # then: nebula/subscribe
            "panel_path": "/api/nebula/panel",
            "version": "0.5.0",
        }
        cloud = remote_ui_url(hass)
        if cloud:
            props["remote_url"] = cloud   # reachable from off-LAN via Nabu Casa

        info = ServiceInfo(
            ZEROCONF_TYPE,
            name=f"{instance}.{ZEROCONF_TYPE}",
            addresses=[socket.inet_aton(host_ip)] if host_ip else [],
            port=port,
            properties=props,
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
