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

import base64

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import CLIENT_KINDS, DATA_MANAGER, DOMAIN, PAIR_PIN_TTL, PANEL_CALL_DOMAINS


def _manager(hass: HomeAssistant):
    for data in hass.data.get(DOMAIN, {}).values():
        if isinstance(data, dict) and DATA_MANAGER in data:
            return data[DATA_MANAGER]
    return None


def _spotify(hass: HomeAssistant):
    return hass.data.get(DOMAIN, {}).get("spotify")


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_heartbeat)
    websocket_api.async_register_command(hass, ws_pair_code)
    websocket_api.async_register_command(hass, ws_call)
    websocket_api.async_register_command(hass, ws_spotify_link)
    websocket_api.async_register_command(hass, ws_spotify_status)
    websocket_api.async_register_command(hass, ws_spotify_unlink)


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
        vol.Optional("service"): str,
        vol.Optional("action"): str,
        vol.Optional("target"): dict,
        vol.Optional("data"): dict,
    }
)
@websocket_api.async_response
async def ws_call(hass, connection, msg) -> None:
    """One command channel for the app.

    * `domain` in ("panel", "media", "nebula")  -> forwarded to the panel as
      `{action: <service|action>, **data}`  (transport / volume / source /
      radio / timers / alarms / voice / eq / bt).
    * anything else -> Home Assistant `call_service` (lights, scenes, …).
    """
    domain = msg["domain"]
    data = msg.get("data", {}) or {}

    if domain in PANEL_CALL_DOMAINS:
        manager = _manager(hass)
        action = msg.get("service") or msg.get("action") or ""
        ok = bool(manager and manager.panel
                  and await manager.panel.send_command(action, **data))
        connection.send_result(msg["id"], {"delivered": ok})
        return

    await hass.services.async_call(
        domain,
        msg.get("service") or msg.get("action"),
        data,
        blocking=False,
        target=msg.get("target"),
        context=connection.context(msg),
    )
    connection.send_result(msg["id"])


# --------------------------------------------------------------------------- #
#  Spotify account link — drives the same broker the panel QR screen uses     #
# --------------------------------------------------------------------------- #


@websocket_api.websocket_command(
    {
        vol.Required("type"): "nebula/spotify_link",
        vol.Optional("panel", default="app"): str,
    }
)
@websocket_api.async_response
async def ws_spotify_link(hass, connection, msg) -> None:
    """Begin a sign-in: returns the authorize URL, a QR PNG (data URL) and the
    flow id the app polls with `nebula/spotify_status`."""
    link = _spotify(hass)
    if link is None:
        connection.send_error(msg["id"], "not_ready", "Nebula not set up")
        return
    started = link.begin(msg["panel"])
    if started is None:
        connection.send_result(
            msg["id"],
            {
                "configured": link.configured,
                "reason": "no_client"
                if not link.configured
                else "no_external_https_url",
            },
        )
        return
    nonce, url = started
    qr_data_url = None
    try:
        from .pairing import qr_png

        png = await hass.async_add_executor_job(qr_png, url)
        qr_data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    except Exception:  # noqa: BLE001
        pass
    connection.send_result(
        msg["id"],
        {"configured": True, "flow": nonce, "auth_url": url, "qr": qr_data_url},
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "nebula/spotify_status",
        vol.Optional("flow", default=""): str,
    }
)
@callback
def ws_spotify_status(hass, connection, msg) -> None:
    link = _spotify(hass)
    if link is None:
        connection.send_error(msg["id"], "not_ready", "Nebula not set up")
        return
    nonce = msg["flow"]
    if nonce:
        status = link.flow_status(nonce)
        # the app never needs the token bundle — the panel collects that.
        if status and status.get("bundle"):
            status = {"state": status["state"], "linked": True}
        connection.send_result(msg["id"], status or {"state": "expired"})
        return
    connection.send_result(msg["id"], link.snapshot())


@websocket_api.websocket_command({vol.Required("type"): "nebula/spotify_unlink"})
@websocket_api.async_response
async def ws_spotify_unlink(hass, connection, msg) -> None:
    link = _spotify(hass)
    if link is None:
        connection.send_error(msg["id"], "not_ready", "Nebula not set up")
        return
    await link.async_unlink()
    connection.send_result(msg["id"], {"ok": True})
