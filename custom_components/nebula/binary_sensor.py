"""Connection-state sensors: is the Nebula app / panel currently talking to us?"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CLIENT_APP, CLIENT_PANEL, DATA_MANAGER, DOMAIN, SIGNAL_CLIENTS_CHANGED
from .device import panel_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = hass.data[DOMAIN][entry.entry_id][DATA_MANAGER]
    async_add_entities(
        [
            NebulaClientSensor(entry, manager, CLIENT_APP, "App"),
            NebulaClientSensor(entry, manager, CLIENT_PANEL, "Panel"),
        ]
    )


class NebulaClientSensor(BinarySensorEntity):
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, manager, kind: str, label: str) -> None:
        self._entry = entry
        self._manager = manager
        self._kind = kind
        self._attr_name = f"Nebula {label} connected"
        self._attr_unique_id = f"{entry.entry_id}_{kind}_connected"
        self._attr_device_info = panel_device_info()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CLIENTS_CHANGED, self._on_change
            )
        )

    @callback
    def _on_change(self, entry_id: str) -> None:
        if entry_id == self._entry.entry_id:
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._kind in self._manager.connected_kinds()

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "clients": [
                c for c in self._manager.client_details() if c["kind"] == self._kind
            ]
        }
