"""Shared device identity for the Nebula panel.

Every Nebula entity (media player, sensors, connection binary-sensors) hangs off
one Home Assistant *device* — "Nebula Panel". That device is what voice targets:
the panel passes its `device_id` on every `assist_pipeline/run`, so HA's built-in
intents ("play <song>", "set a timer", "what's the volume") resolve against this
device's entities and its registered timer handler.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

PANEL_DEVICE_ID = "panel"


def panel_device_info() -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, PANEL_DEVICE_ID)},
        name="Nebula Panel",
        manufacturer="Nebula",
        model="Voice Panel",
    )
