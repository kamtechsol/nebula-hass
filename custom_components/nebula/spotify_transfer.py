"""Spotify Connect playback handoff — "move the music to the void".

Some zones (Control4-bridged ones especially) don't expose a working
`media_player.turn_on` / generic play-search through Home Assistant's intent
layer at all — confirmed directly against "the void" this session, which
fails both requests. But Control4 ships a native Spotify Connect bridge, and
this house already has the SpotifyPlus integration (`spotifyplus`) linked
and working — confirmed "the void" shows up as a real Connect device through
it. Rather than fighting HA's media_player intents for a device that doesn't
support them, or standing up a second, separate Spotify OAuth link just for
this (Nebula's own `spotify_link.py` broker was never actually completed on
this instance), this reuses SpotifyPlus's own `player_transfer_playback`
service — the same "transfer playback" mechanism the Spotify app's own
device picker uses.

`player_transfer_playback`'s `device_id` field docs say it accepts a plain
device name, but it turned out to need an EXACT match — "the void" is that
device's literal registered name (not "void" with a grammatical "the"), so
naively stripping a leading "the" as an article (as an early version of this
module's regex did) broke the match. Fetching the real device list via
`get_spotify_connect_devices` and fuzzy-matching locally sidesteps that
ambiguity entirely rather than guessing "the"-prefix variants.

Returns `None` whenever this isn't a handoff request, no SpotifyPlus account
entity exists, no device name matches, or the transfer call itself fails, so
the caller falls through to its normal handling exactly as if this module
didn't exist.
"""

from __future__ import annotations

import difflib
import logging
import re

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_DOMAIN = "spotifyplus"
_SERVICE_DEVICES = "get_spotify_connect_devices"
_SERVICE_TRANSFER = "player_transfer_playback"

# "move/send/transfer/switch/play this to/on <device>" — the target is
# fuzzy-matched against SpotifyPlus's own real device list below, so any
# Connect device it already knows about works, not just "the void".
_HANDOFF = re.compile(
    r"\b(?:move|send|transfer|switch|hand ?off|continue|play (?:this|it))\b"
    r".*?\b(?:to|on)\s+(?P<target>[a-z0-9' ]+?)\s*[.!]?$",
    re.IGNORECASE,
)


async def async_maybe_handoff(hass: HomeAssistant, text: str) -> str | None:
    """If `text` asks to move the active Spotify session to a named Connect
    device, do it and return a spoken confirmation. None if it isn't a
    handoff request or nothing matches, so the caller's normal handling
    picks it up untouched."""
    match = _HANDOFF.search(text)
    if not match:
        return None
    target_name = match.group("target").strip()
    if not target_name:
        return None

    entity_id = _spotifyplus_account(hass)
    if entity_id is None:
        return None

    devices = await _async_get_devices(hass, entity_id)
    if not devices:
        return None
    device_name = _best_match(target_name, devices)
    if device_name is None:
        return None

    try:
        await hass.services.async_call(
            _DOMAIN,
            _SERVICE_TRANSFER,
            {"entity_id": entity_id, "device_id": device_name, "play": True},
            blocking=True,
        )
    except Exception:  # noqa: BLE001 - degrade to "not a handoff", never raise
        _LOGGER.info(
            "Nebula Spotify handoff: transfer to %r failed", device_name, exc_info=True
        )
        return None

    return f"Moved your music to {device_name}."


async def _async_get_devices(hass: HomeAssistant, entity_id: str) -> list[str]:
    try:
        result = await hass.services.async_call(
            _DOMAIN,
            _SERVICE_DEVICES,
            {"entity_id": entity_id},
            blocking=True,
            return_response=True,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.info("Nebula Spotify handoff: device list failed", exc_info=True)
        return []
    items = ((result or {}).get("result") or {}).get("Items") or []
    return [str(i.get("Name") or "") for i in items if i.get("Name")]


def _best_match(target_name: str, device_names: list[str]) -> str | None:
    """Fuzzy-match a spoken device name against the real Connect device
    names — a substring hit first (handles "void" vs "the void" either
    direction), then a loose similarity match for near-miss phrasing."""
    target = target_name.lower().strip()
    if not target:
        return None
    for name in device_names:
        low = name.lower()
        if target in low or low in target:
            return name
    close = difflib.get_close_matches(target_name, device_names, n=1, cutoff=0.6)
    return close[0] if close else None


def _spotifyplus_account(hass: HomeAssistant) -> str | None:
    """The first SpotifyPlus account media_player entity, or None if the
    integration isn't set up at all."""
    if _DOMAIN not in hass.services.async_services():
        return None
    for entity_id in hass.states.async_entity_ids("media_player"):
        if entity_id.startswith("media_player.spotifyplus_"):
            return entity_id
    return None
