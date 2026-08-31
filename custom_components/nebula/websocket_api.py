"""WebSocket commands for Nebula, served over Home Assistant's own authed WS API.

Clients connect to `/api/websocket`, authenticate as normal, then:

    {"id": 1, "type": "nebula/subscribe", "client": "app", "name": "Barry's iPhone"}
        -> {"type": "snapshot", ...}          (immediately)
        -> {"type": "delta", ...}             (on every relevant state change)

    {"id": 2, "type": "nebula/heartbeat", "client": "app", "name": "..."}
    {"id": 3, "type": "nebula/pair_code"}     -> {"pin": "048213", "expires_in": 300}
    {"id": 4, "type": "nebula/call", "domain": "light", "service": "turn_on",
              "target": {"entity_id": "light.kitchen"}, "data": {"brightness": 180}}
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import CLIENT_KINDS, DATA_MANAGER, DOMAIN, PAIR_PIN_TTL


def _manager(hass: HomeAssistant):
    for data in hass.data.get(DOMAIN, {}).values():
        if isinstance(data, dict) and DATA_MANAGER in data:
            return data[DATA_MANAGER]
    return None


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_heartbeat)
    websocket_api.async_register_command(hass, ws_pair_code)
    websocket_api.async_register_command(hass, ws_call)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "nebula/subscribe",
        vol.Optional("client", default="app"): vol.In(CLIENT_KINDS),
        vol.Optional("name", default="Nebula client"): str,
    }
)
@callback
def ws_subscribe(hass, connection, msg) -> None:
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_ready", "Nebula not set up")
        return

    @callback
    def _forward(payload) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], payload))

    remove = manager.async_add_listener(_forward, kind=msg["client"], name=msg["name"])
    connection.subscriptions[msg["id"]] = remove
    connection.send_result(msg["id"])
    # Prime the client with the full picture.
    connection.send_message(websocket_api.event_message(msg["id"], manager.build_snapshot()))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "nebula/heartbeat",
        vol.Optional("client", default="app"): vol.In(CLIENT_KINDS),
        vol.Optional("name", default="Nebula client"): str,
    }
)
@callback
def ws_heartbeat(hass, connection, msg) -> None:
    manager = _manager(hass)
    if manager is not None:
        manager.async_heartbeat(kind=msg["client"], name=msg["name"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "nebula/pair_code"})
@callback
def ws_pair_code(hass, connection, msg) -> None:
    """Mint a single-use pairing PIN owned by the calling user."""
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_ready", "Nebula not set up")
        return
    pin = manager.new_pin(connection.user.id)
    connection.send_result(msg["id"], {"pin": pin, "expires_in": PAIR_PIN_TTL})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "nebula/call",
        vol.Required("domain"): str,
        vol.Required("service"): str,
        vol.Optional("target"): dict,
        vol.Optional("data"): dict,
    }
)
@websocket_api.async_response
async def ws_call(hass, connection, msg) -> None:
    """Thin pass-through to call_service, so the app has one message type."""
    await hass.services.async_call(
        msg["domain"],
        msg["service"],
        msg.get("data", {}),
        blocking=False,
        target=msg.get("target"),
        context=connection.context(msg),
    )
    connection.send_result(msg["id"])
