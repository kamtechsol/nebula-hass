"""`media_player.nebula` — the panel as a Home Assistant media player.

This is what makes voice work: "Hey Jarvis, play <song>" resolves to this entity
(HA's built-in media intents need a media_player), and so do pause / next /
volume / "switch to Bluetooth".
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_MANAGER, DOMAIN

_SUPPORT = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.PLAY_MEDIA
)

# app "selectedSource" value  <->  HA source label
_SOURCES = {"auto": "Auto", "spotify": "Spotify", "bluetooth": "Bluetooth", "radio": "Radio"}
_SOURCES_REV = {v: k for k, v in _SOURCES.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = hass.data[DOMAIN][entry.entry_id][DATA_MANAGER]
    async_add_entities([NebulaMediaPlayer(entry, manager)])


class NebulaMediaPlayer(MediaPlayerEntity):
    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Nebula"
    _attr_supported_features = _SUPPORT
    _attr_source_list = list(_SOURCES.values())

    def __init__(self, entry: ConfigEntry, manager) -> None:
        self._entry = entry
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_media_player"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._manager.async_add_internal_listener(self._on_event)
        )

    @callback
    def _on_event(self, event: dict) -> None:
        if event.get("type") in ("media", "snapshot", "panel_status"):
            self.async_write_ha_state()

    # ------------------------------------------------------------------ helpers

    @property
    def _media(self) -> dict[str, Any]:
        return self._manager.panel.media if self._manager.panel else {}

    async def _cmd(self, action: str, **fields: Any) -> None:
        if self._manager.panel:
            await self._manager.panel.send_command(action, **fields)

    # ------------------------------------------------------------------ state

    @property
    def available(self) -> bool:
        return bool(self._manager.panel and self._manager.panel.connected)

    @property
    def state(self) -> MediaPlayerState:
        m = self._media
        if not self.available:
            return MediaPlayerState.OFF
        if m.get("playing"):
            return MediaPlayerState.PLAYING
        if m.get("track", {}).get("title"):
            return MediaPlayerState.PAUSED
        return MediaPlayerState.IDLE

    @property
    def media_title(self) -> str | None:
        return (self._media.get("track") or {}).get("title") or None

    @property
    def media_artist(self) -> str | None:
        return (self._media.get("track") or {}).get("artist") or None

    @property
    def media_album_name(self) -> str | None:
        return (self._media.get("track") or {}).get("album") or None

    @property
    def media_image_url(self) -> str | None:
        return (self._media.get("track") or {}).get("artUrl") or None

    @property
    def media_content_type(self) -> str:
        return MediaType.MUSIC

    @property
    def volume_level(self) -> float | None:
        v = self._media.get("volume")
        return v / 100 if isinstance(v, (int, float)) else None

    @property
    def is_volume_muted(self) -> bool:
        return self._media.get("volume") == 0

    @property
    def shuffle(self) -> bool:
        return bool(self._media.get("shuffle"))

    @property
    def source(self) -> str | None:
        return _SOURCES.get(self._media.get("selectedSource") or self._media.get("source"))

    # ---------------------------------------------------------------- commands

    async def async_media_play(self) -> None:
        await self._cmd("transport", value="play")

    async def async_media_pause(self) -> None:
        await self._cmd("transport", value="pause")

    async def async_media_stop(self) -> None:
        await self._cmd("transport", value="stop")

    async def async_media_next_track(self) -> None:
        await self._cmd("transport", value="next")

    async def async_media_previous_track(self) -> None:
        await self._cmd("transport", value="prev")

    async def async_set_volume_level(self, volume: float) -> None:
        await self._cmd("volume", value=int(round(volume * 100)))

    async def async_volume_up(self) -> None:
        cur = self._media.get("volume") or 0
        await self._cmd("volume", value=min(100, int(cur) + 10))

    async def async_volume_down(self) -> None:
        cur = self._media.get("volume") or 0
        await self._cmd("volume", value=max(0, int(cur) - 10))

    async def async_set_shuffle(self, shuffle: bool) -> None:
        await self._cmd("shuffle", value=shuffle)

    async def async_select_source(self, source: str) -> None:
        await self._cmd("source", value=_SOURCES_REV.get(source, "auto"))

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """Voice "play <song>" lands here as a free-text query."""
        if media_id.startswith(("http://", "https://")):
            await self._cmd("radio_play", url=media_id, name="Radio")
        else:
            await self._cmd("play_query", query=media_id)
