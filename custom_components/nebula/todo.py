"""`todo.nebula_shopping_list` — the panel's shopping list.

An Alexa-style shopping list that lives in Home Assistant so it shows up in the
HA app and anything else pointed at it, while the Nebula panel adds / removes /
reads it by voice and renders it on screen. Items persist to HA storage, so they
survive restarts.
"""

from __future__ import annotations

import uuid
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

from .const import DATA_REMINDERS, DOMAIN
from .device import panel_device_info
from .reminders import NebulaReminders
from .reminders import STORAGE_KEY as REMINDERS_STORAGE_KEY
from .reminders import STORAGE_VERSION as REMINDERS_STORAGE_VERSION
from .reminders import _expose_to_conversation as _expose_reminders_to_conversation

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.shopping_list"

_SUPPORTED = (
    TodoListEntityFeature.CREATE_TODO_ITEM
    | TodoListEntityFeature.UPDATE_TODO_ITEM
    | TodoListEntityFeature.DELETE_TODO_ITEM
    | TodoListEntityFeature.MOVE_TODO_ITEM
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    store: Store[list[dict[str, Any]]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    shopping = NebulaShoppingList(entry, store)
    await shopping.async_load()

    # Reminders (reminders.py) share this same "todo" platform-forward slot --
    # HA loads exactly one <domain>/todo.py per config entry, so a second file
    # of entities has to be wired in from here rather than being auto-found.
    reminders_store: Store[list[dict[str, Any]]] = Store(
        hass, REMINDERS_STORAGE_VERSION, REMINDERS_STORAGE_KEY
    )
    reminders = NebulaReminders(entry, reminders_store)
    await reminders.async_load()

    async_add_entities([shopping, reminders])
    _expose_to_conversation(hass, entry)
    _expose_reminders_to_conversation(hass, entry)
    # __init__.py already seeded hass.data[DOMAIN][entry.entry_id] with
    # DATA_MANAGER before forwarding platforms; add DATA_REMINDERS into that
    # same per-entry dict so api.py's sync view can reach this entity.
    hass.data[DOMAIN][entry.entry_id][DATA_REMINDERS] = reminders


class NebulaShoppingList(TodoListEntity):
    """A single, persistent shopping list."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Nebula Shopping List"
    _attr_icon = "mdi:cart"
    _attr_supported_features = _SUPPORTED

    def __init__(self, entry: ConfigEntry, store: Store) -> None:
        self._entry = entry
        self._store = store
        self._attr_unique_id = f"{entry.entry_id}_shopping_list"
        self._attr_device_info = panel_device_info()
        self._attr_todo_items: list[TodoItem] = []

    # ------------------------------------------------------------------ storage

    async def async_load(self) -> None:
        raw = await self._store.async_load() or []
        items: list[TodoItem] = []
        for row in raw:
            summary = (row.get("summary") or "").strip()
            if not summary:
                continue
            status = (
                TodoItemStatus.COMPLETED
                if row.get("status") == "completed"
                else TodoItemStatus.NEEDS_ACTION
            )
            items.append(
                TodoItem(
                    summary=summary,
                    uid=row.get("uid") or uuid.uuid4().hex,
                    status=status,
                )
            )
        self._attr_todo_items = items

    async def _persist(self) -> None:
        await self._store.async_save(
            [
                {"uid": i.uid, "summary": i.summary, "status": i.status.value}
                for i in self._attr_todo_items
            ]
        )
        self.async_write_ha_state()

    # ------------------------------------------------------------------ todo API

    async def async_create_todo_item(self, item: TodoItem) -> None:
        summary = (item.summary or "").strip()
        if not summary:
            return
        # De-dupe: bumping an existing (incomplete) line instead of stacking it.
        for existing in self._attr_todo_items:
            if (
                existing.summary.casefold() == summary.casefold()
                and existing.status == TodoItemStatus.NEEDS_ACTION
            ):
                return
        self._attr_todo_items.append(
            TodoItem(
                summary=summary,
                uid=item.uid or uuid.uuid4().hex,
                status=item.status or TodoItemStatus.NEEDS_ACTION,
            )
        )
        await self._persist()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        for idx, existing in enumerate(self._attr_todo_items):
            if existing.uid == item.uid:
                self._attr_todo_items[idx] = TodoItem(
                    summary=(item.summary or existing.summary).strip(),
                    uid=existing.uid,
                    status=item.status or existing.status,
                )
                await self._persist()
                return

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        drop = set(uids)
        self._attr_todo_items = [
            i for i in self._attr_todo_items if i.uid not in drop
        ]
        await self._persist()

    async def async_move_todo_item(
        self, uid: str, previous_uid: str | None = None
    ) -> None:
        items = self._attr_todo_items
        src = next((i for i, it in enumerate(items) if it.uid == uid), None)
        if src is None:
            return
        moved = items.pop(src)
        if previous_uid is None:
            items.insert(0, moved)
        else:
            dst = next((i for i, it in enumerate(items) if it.uid == previous_uid), None)
            items.insert((dst + 1) if dst is not None else len(items), moved)
        await self._persist()


@callback
def _expose_to_conversation(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Let HA's built-in list intents ("add milk to my shopping list") target
    this entity too, not just the panel's own on-device matcher."""
    try:
        from homeassistant.components.homeassistant.exposed_entities import (
            async_expose_entity,
        )
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        todo_id = ent_reg.async_get_entity_id(
            "todo", DOMAIN, f"{entry.entry_id}_shopping_list"
        )
        if todo_id:
            async_expose_entity(hass, "conversation", todo_id, True)
    except Exception:  # noqa: BLE001 - best effort
        pass
