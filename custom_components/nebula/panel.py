"""The `/api/nebula/panel` channel.

The Nebula panel dials *out* to Home Assistant and holds one WebSocket open. It
streams its media snapshot up (same shape as the panel's `ControlState`), and
the integration sends media commands down. Nothing connects *to* the panel.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)


def empty_media() -> dict[str, Any]:
    return {
        "name": "Nebula",
        "source": "spotify",
        "selectedSource": "auto",
        "playing": False,
        "connected": False,
        "volume": 0,
        "shuffle": False,
        "track": {},
        "queue": [],
        "radio": {},
        "bluetooth": {},
        "eq": {},
        "timers": [],
        "alarms": [],
        "voiceListening": False,
        "wakeWord": "Hey Jarvis",
        "ringing": False,
    }


class PanelChannel:
    """Holds the single panel WebSocket + the latest media snapshot."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._ws: web.WebSocketResponse | None = None
        self._media: dict[str, Any] = empty_media()
        self._last_seen = 0.0
        self._listeners: set[Callable[[dict[str, Any]], None]] = set()

    def set_token(self, token: str) -> None:
        self._token = token

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def media(self) -> dict[str, Any]:
        return self._media

    @callback
    def add_listener(self, cb: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.add(cb)
        return lambda: self._listeners.discard(cb)

    @callback
    def _emit(self, event: dict[str, Any]) -> None:
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Nebula panel listener raised")

    async def send_command(self, action: str, **fields: Any) -> bool:
        """Forward a media command to the panel. False if the panel is offline."""
        if not self.connected:
            return False
        await self._ws.send_json({"type": "command", "action": action, **fields})
        return True

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        if not await self._authorized(request):
            raise web.HTTPForbidden(text="unauthorized")

        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=0)
        await ws.prepare(request)

        if self._ws is not None and not self._ws.closed:
            await self._ws.close(code=4000, message=b"replaced")
        self._ws = ws
        _LOGGER.info("Nebula panel connected from %s", request.remote)
        self._emit({"type": "panel_status", "connected": True})

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    self._on_message(msg.json())
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        finally:
            if self._ws is ws:
                self._ws = None
                self._media = empty_media()
                _LOGGER.info("Nebula panel disconnected")
                self._emit({"type": "panel_status", "connected": False})
                self._emit({"type": "media", "media": self._media})
        return ws

    async def _authorized(self, request: web.Request) -> bool:
        """Accept the panel token, OR any valid Home Assistant access token — so
        a panel that already has HA credentials (for voice) needs no extra setup."""
        tok = request.query.get("token") or ""
        if not tok:
            auth = request.headers.get("Authorization", "")
            tok = auth[7:] if auth.startswith("Bearer ") else ""
        if tok and self._token and tok == self._token:
            return True
        hass = request.app["hass"]
        try:
            result = hass.auth.async_validate_access_token(tok)
            if hasattr(result, "__await__"):
                result = await result
        except Exception:  # noqa: BLE001
            result = None
        return result is not None

    def _on_message(self, msg: dict[str, Any]) -> None:
        self._last_seen = time.monotonic()
        if msg.get("type") in ("hello", "snapshot", "state"):
            payload = msg.get("state") or msg.get("payload") or dict(msg)
            payload.pop("type", None)
            self._media = {**empty_media(), **payload}
            self._emit({"type": "media", "media": self._media})


class NebulaPanelView(HomeAssistantView):
    """Unauthenticated (panel-token-gated) WebSocket endpoint for the panel."""

    url = "/api/nebula/panel"
    name = "api:nebula:panel"
    requires_auth = False

    def __init__(self, channel: PanelChannel) -> None:
        self._channel = channel

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        return await self._channel.handle(request)
