"""Panel state surfaced as Home Assistant sensors: wake word, now playing, volume."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_MANAGER, DOMAIN, SIGNAL_CLIENTS_CHANGED
from .device import panel_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = hass.data[DOMAIN][entry.entry_id][DATA_MANAGER]
    async_add_entities(
        [
            NebulaPanelSensor(entry, manager, "wake_word", "Nebula wake word",
                              lambda m: m.get("wakeWord") or "—",
                              icon="mdi:microphone-message", diagnostic=True),
            NebulaPanelSensor(entry, manager, "now_playing", "Nebula now playing",
                              _now_playing, icon="mdi:music"),
            NebulaPanelSensor(entry, manager, "volume", "Nebula volume",
                              lambda m: m.get("volume"), icon="mdi:volume-high",
                              unit="%"),
            NebulaPanelSensor(entry, manager, "source", "Nebula source",
                              lambda m: (m.get("source") or "").title() or "—",
                              icon="mdi:import"),
        ]
    )


def _now_playing(media: dict[str, Any]) -> str:
    track = media.get("track") or {}
    title = track.get("title")
    if not title:
        return "Nothing playing"
    artist = track.get("artist")
    return f"{title} — {artist}" if artist else title


class NebulaPanelSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, entry, manager, key, name, getter, *,
                 icon=None, unit=None, diagnostic=False) -> None:
        self._entry = entry
        self._manager = manager
        self._getter = getter
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = panel_device_info()
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_CLIENTS_CHANGED, self._changed)
        )
        # Media deltas arrive through the manager's listener fan-out.
        self.async_on_remove(
            self._manager.async_add_internal_listener(self._on_event)
        )

    @callback
    def _changed(self, entry_id: str) -> None:
        if entry_id == self._entry.entry_id:
            self.async_write_ha_state()

    @callback
    def _on_event(self, event: dict) -> None:
        if event.get("type") in ("media", "snapshot"):
            self.async_write_ha_state()

    @property
    def native_value(self):
        media = self._manager.panel.media if self._manager.panel else {}
        return self._getter(media)
