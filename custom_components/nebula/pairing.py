"""Shared helpers for the Nebula QR pairing flow.

The QR encodes a `nebula://pair?...` deep link the app already understands:

    nebula://pair?host=<lan-ip>&port=<port>&code=<single-use code>&name=<location>

The app scans it, POSTs the code to `/api/nebula/pair`, and gets a long-lived
token back — so the code (not a token) is what sits on screen.
"""

from __future__ import annotations

import io
import socket
from urllib.parse import urlencode

from homeassistant.core import HomeAssistant


async def async_get_source_ip(hass: HomeAssistant) -> str | None:
    """This host's LAN IP, as seen from the default route."""
    try:
        from homeassistant.helpers.network import async_get_source_ip as _get

        return await _get(hass)
    except Exception:  # noqa: BLE001
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None


async def async_lan_host_port(hass: HomeAssistant) -> tuple[str, int]:
    """Best-guess LAN address for the app to reach this Home Assistant."""
    port = hass.http.server_port or 8123
    host = await async_get_source_ip(hass) or "homeassistant.local"
    return host, port


def pair_uri(host: str, port: int, code: str, name: str) -> str:
    """Build the `nebula://pair?...` deep link embedded in the QR."""
    query = urlencode(
        {"host": host, "port": port, "code": code, "name": name or "Home Assistant"}
    )
    return f"nebula://pair?{query}"


def qr_png(data: str, *, scale: int = 6, border: int = 4) -> bytes:
    """Render `data` to a PNG QR code. Requires `segno` (see manifest)."""
    import segno

    buff = io.BytesIO()
    segno.make(data, error="m").save(
        buff, kind="png", scale=scale, border=border, dark="#0b0b0f", light="#ffffff"
    )
    return buff.getvalue()
