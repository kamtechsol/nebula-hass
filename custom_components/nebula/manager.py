"""Connection manager + snapshot builder for the Nebula integration.

Owns the single source of truth the Nebula app and panel consume:
a combined snapshot (rooms with their entities pre-grouped, scenes,
automations) plus a live delta stream, and bookkeeping of which clients
are currently connected.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from datetime import timedelta

from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CLIENT_KINDS,
    CLIENT_TIMEOUT,
    INTERESTING_DOMAINS,
    PAIR_PIN_TTL,
    SIGNAL_CLIENTS_CHANGED,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class _Client:
    kind: str
    name: str
    last_seen: float = field(default_factory=time.monotonic)


@dataclass
class _Pin:
    code: str
    created: float
    user_id: str


class NebulaManager:
    """Tracks subscribers, builds snapshots, brokers pairing PINs."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._clients: dict[str, _Client] = {}
        self._listeners: set[Callable[[dict[str, Any]], None]] = set()
        self._pins: list[_Pin] = []
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []

    # ------------------------------------------------------------------ setup

    @callback
    def async_start(self) -> None:
        """Begin watching state so we can push deltas to subscribers."""
        self._unsub_state = async_track_state_change_event(
            self.hass, self._interesting_entity_ids(), self._on_state_event
        )
        # Re-evaluate the tracked set whenever the entity registry changes.
        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_event
            )
        )
        # Age out stale clients so the connectivity sensors go off on their own.
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._on_tick, timedelta(seconds=30)
            )
        )

    @callback
    def async_stop(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._listeners.clear()
        self._clients.clear()

    @callback
    def _on_tick(self, _now) -> None:
        self._notify_clients_changed()

    # -------------------------------------------------------------- subscribers

    @callback
    def async_add_listener(
        self, cb: Callable[[dict[str, Any]], None], *, kind: str, name: str
    ) -> CALLBACK_TYPE:
        """Register a delta listener and mark that client connected."""
        self._listeners.add(cb)
        cid = f"{kind}:{name}"
        self._clients[cid] = _Client(kind=kind, name=name[:64])
        self._notify_clients_changed()

        @callback
        def _remove() -> None:
            self._listeners.discard(cb)
            self._clients.pop(cid, None)
            self._notify_clients_changed()

        return _remove

    @callback
    def async_heartbeat(self, *, kind: str, name: str) -> None:
        cid = f"{kind}:{name}"
        if cid in self._clients:
            self._clients[cid].last_seen = time.monotonic()

    @callback
    def connected_kinds(self) -> set[str]:
        """Kinds with at least one non-stale client."""
        now = time.monotonic()
        alive = {
            c.kind
            for c in self._clients.values()
            if now - c.last_seen < CLIENT_TIMEOUT
        }
        return alive & set(CLIENT_KINDS)

    @callback
    def client_details(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [
            {
                "kind": c.kind,
                "name": c.name,
                "stale": (now - c.last_seen) >= CLIENT_TIMEOUT,
                "age": round(now - c.last_seen, 1),
            }
            for c in self._clients.values()
        ]

    @callback
    def _notify_clients_changed(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_CLIENTS_CHANGED, self.entry_id)

    # ------------------------------------------------------------------ pairing

    @callback
    def new_pin(self, user_id: str) -> str:
        """Mint a short pairing PIN owned by `user_id`, pruning expired ones."""
        self._prune_pins()
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._pins.append(_Pin(code=code, created=time.monotonic(), user_id=user_id))
        return code

    @callback
    def consume_pin(self, code: str) -> str | None:
        """Validate and burn a PIN. Returns the owning user_id, or None."""
        self._prune_pins()
        for pin in self._pins:
            if secrets.compare_digest(pin.code, code):
                self._pins.remove(pin)
                return pin.user_id
        return None

    @callback
    def _prune_pins(self) -> None:
        cutoff = time.monotonic() - PAIR_PIN_TTL
        self._pins = [p for p in self._pins if p.created > cutoff]

    # ------------------------------------------------------------------ state

    @callback
    def _interesting_entity_ids(self) -> list[str]:
        return [
            state.entity_id
            for state in self.hass.states.async_all()
            if state.domain in INTERESTING_DOMAINS
        ] or [f"{d}.*" for d in INTERESTING_DOMAINS]

    @callback
    def _on_registry_event(self, _event: Event) -> None:
        if self._unsub_state:
            self._unsub_state()
        self._unsub_state = async_track_state_change_event(
            self.hass, self._interesting_entity_ids(), self._on_state_event
        )

    @callback
    def _on_state_event(self, event: Event) -> None:
        new = event.data.get("new_state")
        entity_id = event.data["entity_id"]
        payload = {
            "type": "delta",
            "entity": self._entity_dict(new) if new else None,
            "entity_id": entity_id,
            "ts": dt_util.utcnow().isoformat(),
        }
        for cb in list(self._listeners):
            try:
                cb(payload)
            except Exception:  # noqa: BLE001 - never let one bad listener break the rest
                _LOGGER.exception("Nebula listener raised")

    # --------------------------------------------------------------- snapshots

    @callback
    def build_snapshot(self) -> dict[str, Any]:
        """The full picture the app renders its Home / Scenes / Automations from."""
        area_reg = ar.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)

        # entity_id -> area_id
        area_of: dict[str, str] = {}
        for entry in ent_reg.entities.values():
            if entry.area_id:
                area_of[entry.entity_id] = entry.area_id
            elif entry.device_id and (dev := dev_reg.async_get(entry.device_id)):
                if dev.area_id:
                    area_of[entry.entity_id] = dev.area_id

        rooms: dict[str, dict[str, Any]] = {
            area.id: {"area_id": area.id, "name": area.name, "entities": []}
            for area in area_reg.async_list_areas()
        }
        unassigned: list[dict[str, Any]] = []
        scenes: list[dict[str, Any]] = []
        scripts: list[dict[str, Any]] = []
        automations: list[dict[str, Any]] = []

        for state in self.hass.states.async_all():
            if state.domain not in INTERESTING_DOMAINS:
                continue
            ent = self._entity_dict(state)
            if state.domain == "scene":
                scenes.append(ent)
            elif state.domain == "script":
                scripts.append(ent)
            elif state.domain == "automation":
                automations.append(ent)
            else:
                area_id = area_of.get(state.entity_id)
                if area_id and area_id in rooms:
                    rooms[area_id]["entities"].append(ent)
                elif state.domain in ("light", "switch", "fan"):
                    unassigned.append(ent)

        return {
            "type": "snapshot",
            "ha_version": self.hass.config.as_dict().get("version"),
            "location_name": self.hass.config.location_name,
            "rooms": sorted(rooms.values(), key=lambda r: r["name"].lower()),
            "unassigned": unassigned,
            "scenes": scenes,
            "scripts": scripts,
            "automations": automations,
            "ts": dt_util.utcnow().isoformat(),
        }

    @staticmethod
    @callback
    def _entity_dict(state) -> dict[str, Any]:
        return {
            "entity_id": state.entity_id,
            "state": state.state,
            "attributes": dict(state.attributes),
            "last_changed": state.last_changed.isoformat(),
        }
