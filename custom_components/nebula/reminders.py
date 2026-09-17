"""`todo.nebula_reminders` — upcoming reminders/events synced from the app.

Same shape as `todo.py` (the shopping list): a persistent `TodoListEntity` so
it shows up in the HA app, Assist, and anything else pointed at a `todo.*`
entity, while the panel renders it on screen. The difference is where items
come from: the phone app reads the user's own iOS EventKit (which already
unifies iCloud/iCal *and* any Google/Outlook calendar the user has added in
Settings -> Calendar — no separate Google OAuth needed) and pushes upcoming
items here via `NebulaReminders.async_sync_external` / the sync HTTP view in
api.py, instead of the user adding them by hand.

Sync is idempotent and one-way for anything from the app: each externally
synced item's `uid` is deterministically derived from its EventKit
identifier (`ext:<id>`), so re-syncing updates in place rather than
duplicating, and a since-deleted source item is removed on the next sync. A
reminder added by voice or on the panel gets a random uid instead, so it's
never touched by a sync — the two coexist in the same list.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .device import panel_device_info

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.reminders"

EXTERNAL_PREFIX = "ext:"

_SUPPORTED = (
    TodoListEntityFeature.CREATE_TODO_ITEM
    | TodoListEntityFeature.UPDATE_TODO_ITEM
    | TodoListEntityFeature.DELETE_TODO_ITEM
    | TodoListEntityFeature.MOVE_TODO_ITEM
    | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
    | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
)


# Entity setup lives in todo.py's async_setup_entry -- HA only forwards one
# <domain>/todo.py per config entry, so this module is instantiated from
# there rather than getting its own async_setup_entry.
class NebulaReminders(TodoListEntity):
    """Reminders/events — a mix of app-synced calendar items and anything
    added directly by voice or on the panel."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Nebula Reminders"
    _attr_icon = "mdi:calendar-clock"
    _attr_supported_features = _SUPPORTED

    def __init__(self, entry: ConfigEntry, store: Store) -> None:
        self._entry = entry
        self._store = store
        self._attr_unique_id = f"{entry.entry_id}_reminders"
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
            due_raw = row.get("due")
            due = dt_util.parse_datetime(due_raw) if due_raw else None
            items.append(
                TodoItem(
                    summary=summary,
                    uid=row.get("uid") or uuid.uuid4().hex,
                    status=(
                        TodoItemStatus.COMPLETED
                        if row.get("status") == "completed"
                        else TodoItemStatus.NEEDS_ACTION
                    ),
                    due=due,
                    description=row.get("description") or None,
                )
            )
        self._attr_todo_items = items

    async def _persist(self) -> None:
        await self._store.async_save(
            [
                {
                    "uid": i.uid,
                    "summary": i.summary,
                    "status": i.status.value,
                    "due": i.due.isoformat() if isinstance(i.due, datetime) else None,
                    "description": i.description,
                }
                for i in self._attr_todo_items
            ]
        )
        self.async_write_ha_state()

    # ------------------------------------------------------------------ todo API

    async def async_create_todo_item(self, item: TodoItem) -> None:
        summary = (item.summary or "").strip()
        if not summary:
            return
        self._attr_todo_items.append(
            TodoItem(
                summary=summary,
                uid=item.uid or uuid.uuid4().hex,
                status=item.status or TodoItemStatus.NEEDS_ACTION,
                due=item.due,
                description=item.description,
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
                    due=item.due if item.due is not None else existing.due,
                    description=(
                        item.description
                        if item.description is not None
                        else existing.description
                    ),
                )
                await self._persist()
                return

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        drop = set(uids)
        self._attr_todo_items = [i for i in self._attr_todo_items if i.uid not in drop]
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

    # ------------------------------------------------------------------ app sync

    async def async_sync_external(self, items: list[dict[str, Any]]) -> dict[str, int]:
        """Replace every externally-sourced item with this batch. `items` is
        `[{"external_id": str, "summary": str, "due": iso str|None,
        "description": str|None}, ...]` — one-way from the app's calendar.
        Anything not prefixed `ext:` (added by voice/panel) is left alone.
        Returns a small `{added, updated, removed}` count for the app to show.
        """
        wanted: dict[str, dict[str, Any]] = {}
        for raw in items:
            ext_id = str(raw.get("external_id") or "").strip()
            summary = str(raw.get("summary") or "").strip()
            if not ext_id or not summary:
                continue
            wanted[f"{EXTERNAL_PREFIX}{ext_id}"] = raw

        existing_ext = {
            i.uid: i for i in self._attr_todo_items if i.uid.startswith(EXTERNAL_PREFIX)
        }
        manual = [i for i in self._attr_todo_items if not i.uid.startswith(EXTERNAL_PREFIX)]

        added = updated = 0
        next_items: list[TodoItem] = []
        for uid, raw in wanted.items():
            due_raw = raw.get("due")
            due = dt_util.parse_datetime(due_raw) if due_raw else None
            new_item = TodoItem(
                summary=str(raw.get("summary")).strip(),
                uid=uid,
                status=TodoItemStatus.NEEDS_ACTION,
                due=due,
                description=(raw.get("description") or None),
            )
            if uid in existing_ext:
                if existing_ext[uid] != new_item:
                    updated += 1
                del existing_ext[uid]
            else:
                added += 1
            next_items.append(new_item)

        removed = len(existing_ext)  # whatever's left in existing_ext wasn't re-sent
        self._attr_todo_items = manual + next_items
        await self._persist()
        return {"added": added, "updated": updated, "removed": removed}


@callback
def _expose_to_conversation(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Let HA's built-in list intents ("what are my reminders") target this
    entity too, not just the panel's own on-device matcher."""
    try:
        from homeassistant.components.homeassistant.exposed_entities import (
            async_expose_entity,
        )
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        todo_id = ent_reg.async_get_entity_id(
            "todo", DOMAIN, f"{entry.entry_id}_reminders"
        )
        if todo_id:
            async_expose_entity(hass, "conversation", todo_id, True)
    except Exception:  # noqa: BLE001 - best effort
        pass
