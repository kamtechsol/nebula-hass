"""Spotify account link — the "open a link / scan a QR, sign in once" flow.

The panel (and the Nebula app) never see a Spotify client secret and never run
the OAuth round-trip themselves. This module is the broker:

* ``GET  /api/nebula/spotify/start``     (no auth) -> 302 to accounts.spotify.com
* ``GET  /api/nebula/spotify/callback``  (no auth) <- Spotify redirects here
* ``GET  /api/nebula/spotify/status``    (panel-token or HA auth) -> link state
                                          + a one-time token bundle for the panel
* ``GET  /api/nebula/spotify/qr``        (no auth) -> PNG QR of the start URL
* ``POST /api/nebula/spotify/unlink``    (HA auth) -> forget the account

The refresh token lives here, in HA storage (``.storage/nebula_spotify``), so a
full panel wipe + OS re-flash is a 15-second re-scan — or nothing at all: after
the panel re-pairs to HA it calls ``/status`` with no flow id and silently
re-hydrates from the stored account.

Auth is Authorization Code + PKCE. The bundle handed to the panel carries a
fresh access token (we refresh server-side), the refresh token (encrypted at
rest on the panel, only as a fallback), the granted scopes and the Spotify
profile. The access token also bootstraps the on-device librespot Connect
receiver (``AUTHENTICATION_SPOTIFY_TOKEN``) so "play to this display" works with
no "pick the device in Spotify" step.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import urlencode

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import network
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import (
    CONF_PANEL_TOKEN,
    CONF_SPOTIFY_CLIENT_ID,
    CONF_SPOTIFY_CLIENT_SECRET,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DATA_SPOTIFY = "spotify"

_AUTH_BASE = "https://accounts.spotify.com/authorize"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_ME_URL = "https://api.spotify.com/v1/me"

_CALLBACK_PATH = "/api/nebula/spotify/callback"

# Everything the panel needs: playback + Connect, the library/playlists that back
# the tracklist view, and the profile ("user data").
SCOPES = (
    "user-read-private user-read-email "
    "user-read-playback-state user-modify-playback-state user-read-currently-playing "
    "streaming app-remote-control "
    "playlist-read-private playlist-read-collaborative "
    "user-library-read user-library-modify "
    "user-top-read user-read-recently-played "
    "user-follow-read"
)

_FLOW_TTL = 600          # seconds a started flow may sit unfinished
_BUNDLE_TTL = 120        # seconds the panel has to collect a freshly-linked bundle
_STORAGE_KEY = "nebula_spotify"
_STORAGE_VERSION = 1


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass
class _Flow:
    verifier: str
    panel: str
    created: float = field(default_factory=time.monotonic)
    state: str = "pending"           # pending | linked | error
    error: str | None = None
    bundle: dict[str, Any] | None = None
    bundle_at: float = 0.0


class SpotifyLink:
    """Owns the OAuth broker and the persisted Spotify account."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self._entry = entry
        self._store: Store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY, private=True)
        self._data: dict[str, Any] = {}
        self._flows: dict[str, _Flow] = {}
        # cached access token (not persisted — cheap to re-mint from the refresh)
        self._access: str | None = None
        self._access_exp: float = 0.0

    # ---- config ---------------------------------------------------------- #

    @property
    def client_id(self) -> str:
        return (self._entry.options.get(CONF_SPOTIFY_CLIENT_ID) or "").strip()

    @property
    def client_secret(self) -> str:
        return (self._entry.options.get(CONF_SPOTIFY_CLIENT_SECRET) or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def linked(self) -> bool:
        return bool(self._data.get("refresh_token"))

    # ---- lifecycle ----------------------------------------------------- #

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    async def _async_save(self) -> None:
        await self._store.async_save(self._data)

    async def async_unlink(self) -> None:
        self._data = {}
        self._access = None
        self._access_exp = 0.0
        await self._async_save()

    # ---- snapshot ---------------------------------------------------- #

    @callback
    def snapshot(self) -> dict[str, Any]:
        user = self._data.get("user") or {}
        return {
            "configured": self.configured,
            "linked": self.linked,
            "user": {
                "id": user.get("id"),
                "name": user.get("display_name") or user.get("id"),
                "product": user.get("product"),          # "premium" | "free"
            }
            if self.linked
            else None,
            "scopes": self._data.get("scopes", "").split() if self.linked else [],
            "linked_at": self._data.get("linked_at"),
        }

    # ---- the OAuth round-trip --------------------------------------- #

    def _redirect_uri(self) -> str | None:
        try:
            base = network.get_url(
                self.hass,
                allow_internal=False,
                allow_external=True,
                prefer_external=True,
                require_ssl=True,
            )
        except network.NoURLAvailableError:
            return None
        return base.rstrip("/") + _CALLBACK_PATH

    @callback
    def _reap_flows(self) -> None:
        now = time.monotonic()
        for nonce in [
            n
            for n, f in self._flows.items()
            if now - f.created > _FLOW_TTL
            or (f.bundle_at and now - f.bundle_at > _BUNDLE_TTL)
        ]:
            self._flows.pop(nonce, None)

    def begin(self, panel: str, nonce: str | None = None) -> tuple[str, str] | None:
        """Return (nonce, authorize_url) for a new sign-in, or None if unusable.

        A caller (the panel QR screen) may supply its own high-entropy `nonce`
        so it can poll `/status` without a pre-flight round-trip; otherwise one
        is generated (the browser / "type the short link" path).
        """
        self._reap_flows()
        redirect = self._redirect_uri()
        if not self.configured or not redirect:
            return None
        if nonce and (len(nonce) < 16 or not nonce.replace("-", "").replace("_", "").isalnum()):
            nonce = None
        nonce = nonce or secrets.token_urlsafe(24)
        # Re-issuing the same flow id just refreshes its verifier.
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        self._flows[nonce] = _Flow(verifier=verifier, panel=panel or "")
        url = _AUTH_BASE + "?" + urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": redirect,
                "state": nonce,
                "scope": SCOPES,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "show_dialog": "false",
            }
        )
        return nonce, url

    async def async_complete(self, nonce: str, code: str) -> _Flow:
        flow = self._flows.get(nonce)
        if flow is None:
            raise KeyError("unknown or expired sign-in")
        redirect = self._redirect_uri()
        session = async_get_clientsession(self.hass)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": self.client_id,
            "code_verifier": flow.verifier,
        }
        auth = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        async with session.post(
            _TOKEN_URL,
            data=form,
            headers={"Authorization": f"Basic {auth}"},
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != HTTPStatus.OK or "access_token" not in body:
                flow.state = "error"
                flow.error = body.get("error_description") or body.get("error") or f"HTTP {resp.status}"
                _LOGGER.warning("Nebula Spotify: token exchange failed: %s", flow.error)
                return flow

        access = body["access_token"]
        refresh = body.get("refresh_token")
        scopes = body.get("scope", SCOPES)

        profile: dict[str, Any] = {}
        async with session.get(
            _ME_URL, headers={"Authorization": f"Bearer {access}"}
        ) as resp:
            if resp.status == HTTPStatus.OK:
                profile = await resp.json(content_type=None)

        self._data = {
            "refresh_token": refresh,
            "scopes": scopes,
            "user": {
                "id": profile.get("id"),
                "display_name": profile.get("display_name"),
                "product": profile.get("product"),
            },
            "linked_at": int(time.time()),
        }
        self._access = access
        self._access_exp = time.monotonic() + int(body.get("expires_in", 3600)) - 60
        await self._async_save()

        flow.state = "linked"
        flow.bundle = self._bundle(access, int(body.get("expires_in", 3600)))
        flow.bundle_at = time.monotonic()
        _LOGGER.info(
            "Nebula Spotify: linked %s (%s)",
            self._data["user"].get("display_name") or self._data["user"].get("id"),
            self._data["user"].get("product"),
        )
        return flow

    def _bundle(self, access: str, expires_in: int) -> dict[str, Any]:
        user = self._data.get("user") or {}
        return {
            "access_token": access,
            "refresh_token": self._data.get("refresh_token"),
            "expires_in": expires_in,
            "scopes": self._data.get("scopes", ""),
            "user": {
                "id": user.get("id"),
                "name": user.get("display_name") or user.get("id"),
                "product": user.get("product"),
            },
            "linked_at": self._data.get("linked_at"),
        }

    async def async_access_token(self) -> str | None:
        """A valid access token, refreshed server-side from the stored refresh."""
        if self._access and time.monotonic() < self._access_exp:
            return self._access
        refresh = self._data.get("refresh_token")
        if not refresh or not self.configured:
            return None
        session = async_get_clientsession(self.hass)
        auth = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        async with session.post(
            _TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            headers={"Authorization": f"Basic {auth}"},
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != HTTPStatus.OK or "access_token" not in body:
                _LOGGER.warning(
                    "Nebula Spotify: refresh failed (%s) — account may need re-linking",
                    body.get("error"),
                )
                return None
        self._access = body["access_token"]
        self._access_exp = time.monotonic() + int(body.get("expires_in", 3600)) - 60
        if body.get("refresh_token"):
            self._data["refresh_token"] = body["refresh_token"]
            await self._async_save()
        return self._access

    async def async_bundle_for_panel(self) -> dict[str, Any] | None:
        """The relink bundle for a panel that already knows it's linked (no flow)."""
        if not self.linked:
            return None
        access = await self.async_access_token()
        if not access:
            return None
        remaining = max(60, int(self._access_exp - time.monotonic()))
        return self._bundle(access, remaining)

    @callback
    def flow_status(self, nonce: str) -> dict[str, Any] | None:
        self._reap_flows()
        flow = self._flows.get(nonce)
        if flow is None:
            return None
        out: dict[str, Any] = {"state": flow.state}
        if flow.state == "error":
            out["error"] = flow.error
        elif flow.state == "linked" and flow.bundle is not None:
            out["bundle"] = flow.bundle
            # one-shot: the panel has it now
            flow.bundle = None
            flow.bundle_at = time.monotonic()
        return out


# --------------------------------------------------------------------------- #
#  wiring                                                                      #
# --------------------------------------------------------------------------- #


def _link(hass: HomeAssistant) -> SpotifyLink | None:
    return hass.data.get(DOMAIN, {}).get(DATA_SPOTIFY)


async def async_setup(hass: HomeAssistant, entry) -> SpotifyLink:
    link = SpotifyLink(hass, entry)
    await link.async_load()
    hass.data.setdefault(DOMAIN, {})[DATA_SPOTIFY] = link
    return link


@callback
def async_register_http(hass: HomeAssistant) -> None:
    hass.http.register_view(SpotifyStartView())
    hass.http.register_view(SpotifyBeginView())
    hass.http.register_view(SpotifyCallbackView())
    hass.http.register_view(SpotifyStatusView())
    hass.http.register_view(SpotifyQRView())
    hass.http.register_view(SpotifyUnlinkView())


def _panel_authorised(hass: HomeAssistant, request: web.Request) -> bool:
    want = None
    for e in hass.config_entries.async_entries(DOMAIN):
        want = e.options.get(CONF_PANEL_TOKEN)
        if want:
            break
    got = request.headers.get("X-Nebula-Panel-Token")
    return bool(want) and secrets.compare_digest(str(got or ""), str(want))


class SpotifyStartView(HomeAssistantView):
    url = "/api/nebula/spotify/start"
    name = "api:nebula:spotify:start"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        link = _link(hass)
        if link is None:
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE, text="Nebula not set up")
        started = link.begin(
            request.query.get("panel", ""), request.query.get("flow") or None
        )
        if started is None:
            hint = (
                "Add a Spotify Client ID and Secret in the Nebula integration options"
                if not link.configured
                else "Home Assistant needs an external HTTPS URL (Nabu Casa or a reverse "
                "proxy) so Spotify can redirect back"
            )
            return web.Response(status=HTTPStatus.BAD_REQUEST, text=f"Spotify link unavailable — {hint}.")
        _nonce, url = started
        raise web.HTTPFound(url)


class SpotifyBeginView(HomeAssistantView):
    """JSON sibling of /start for the panel/app: returns the flow id + auth URL
    instead of redirecting, so the client can render its own QR and poll."""

    url = "/api/nebula/spotify/begin"
    name = "api:nebula:spotify:begin"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        link = _link(hass)
        if link is None:
            return self.json_message("Nebula not set up", HTTPStatus.SERVICE_UNAVAILABLE)
        started = link.begin(
            request.query.get("panel", ""), request.query.get("flow") or None
        )
        if started is None:
            return self.json(
                {
                    "configured": link.configured,
                    "reason": "no_client"
                    if not link.configured
                    else "no_external_https_url",
                }
            )
        nonce, url = started
        return self.json({"configured": True, "flow": nonce, "auth_url": url})


class SpotifyCallbackView(HomeAssistantView):
    url = _CALLBACK_PATH
    name = "api:nebula:spotify:callback"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        link = _link(hass)
        nonce = request.query.get("state", "")
        err = request.query.get("error")
        code = request.query.get("code")
        if link is None:
            return _page("Nebula isn’t set up.", ok=False)
        if err:
            flow = link._flows.get(nonce)  # noqa: SLF001
            if flow:
                flow.state, flow.error = "error", err
            return _page(f"Sign-in was cancelled ({err}).", ok=False)
        if not code:
            return _page("No authorization code came back from Spotify.", ok=False)
        try:
            flow = await link.async_complete(nonce, code)
        except KeyError:
            return _page("This sign-in link has expired — start again from your display.", ok=False)
        if flow.state != "linked":
            return _page(f"Spotify sign-in failed: {flow.error}", ok=False)
        return _page("You’re signed in. Head back to your Nebula display — it’ll pick this up in a moment.", ok=True)


class SpotifyStatusView(HomeAssistantView):
    url = "/api/nebula/spotify/status"
    name = "api:nebula:spotify:status"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        link = _link(hass)
        if link is None:
            return self.json_message("Nebula not set up", HTTPStatus.SERVICE_UNAVAILABLE)

        # Panel-token only — NOT "any signed-in HA user" — same restriction the
        # WS command (ws_spotify_status) already enforces ("the app never needs
        # the token bundle"). This used to also accept a bare HA user auth,
        # which meant it never actually applied since every request here comes
        # from an authenticated HA session; any paired non-admin user could
        # pull a durable Spotify refresh token with no panel-token needed.
        authed = _panel_authorised(hass, request)
        nonce = request.query.get("flow", "")

        if nonce:
            status = link.flow_status(nonce)
            if status is None:
                return self.json({"state": "expired"})
            if status.get("bundle") and not authed:
                # never hand a refresh token to an unauthenticated caller
                status = {"state": status["state"], "linked_pending_auth": True}
            return self.json(status)

        # No flow id: a re-hydrate probe from an already-paired panel.
        if not authed:
            return self.json_message("Unauthorized", HTTPStatus.UNAUTHORIZED)
        bundle = await link.async_bundle_for_panel()
        return self.json(
            {"state": "linked", "bundle": bundle} if bundle else {"state": "unlinked"}
        )


class SpotifyQRView(HomeAssistantView):
    url = "/api/nebula/spotify/qr"
    name = "api:nebula:spotify:qr"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        link = _link(hass)
        if link is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        started = link.begin(
            request.query.get("panel", ""), request.query.get("flow") or None
        )
        if started is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        _nonce, url = started
        from .pairing import qr_png

        try:
            png = await hass.async_add_executor_job(qr_png, url)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Nebula Spotify: QR render failed")
            return web.Response(status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return web.Response(
            body=png, content_type="image/png", headers={"Cache-Control": "no-store"}
        )


class SpotifyUnlinkView(HomeAssistantView):
    url = "/api/nebula/spotify/unlink"
    name = "api:nebula:spotify:unlink"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        link = _link(hass)
        if link is None:
            return self.json_message("Nebula not set up", HTTPStatus.SERVICE_UNAVAILABLE)
        await link.async_unlink()
        return self.json({"ok": True})


def _page(msg: str, *, ok: bool) -> web.Response:
    mark = "✓" if ok else "×"
    tint = "#1DB954" if ok else "#E22134"
    html = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Nebula · Spotify</title>
<style>
 html,body{{margin:0;height:100%;background:#0f1014;color:#e9e9ee;
  font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
 .wrap{{display:flex;flex-direction:column;align-items:center;justify-content:center;
  height:100%;padding:32px;text-align:center;box-sizing:border-box}}
 .mark{{width:64px;height:64px;border-radius:50%;display:grid;place-items:center;
  font-size:34px;font-weight:700;color:#fff;background:{tint};margin-bottom:20px}}
 p{{max-width:22rem;color:#b9b9c3}}
 h1{{font-size:19px;margin:0 0 6px}}
</style>
<div class=wrap><div class=mark>{mark}</div>
<h1>{'Signed in to Spotify' if ok else 'Spotify sign-in'}</h1>
<p>{msg}</p></div>"""
    return web.Response(text=html, content_type="text/html")
