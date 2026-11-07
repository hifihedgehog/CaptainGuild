"""Per-server settings for every module, held in memory and written through to the database.

`get` is synchronous, because listeners read settings on every message. It hands back the live dict, so treat
it as read-only and change settings with `save`. Every save lands in the activity log with its before and after,
which is what makes the dashboard's undo possible.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..db import Database
from .validate import Schema

MIGRATIONS = [
    """
    CREATE TABLE guild_settings (
        guild_id INTEGER NOT NULL,
        module TEXT NOT NULL,
        data TEXT NOT NULL,
        updated_at REAL NOT NULL,
        updated_by INTEGER,
        PRIMARY KEY (guild_id, module)
    );
    """,
]

Listener = Callable[[int, dict], Awaitable[None]]

# Commands renamed after servers saved rules for them, from the name a rule may be saved under to the name it answers to now.
FORMER_COMMAND_NAMES = {"clearwarns": "clearwarnings"}  # 1.2.2


def upgraded(module: str, data: Any) -> Any:
    """Saved settings in today's shape. A command rule saved under a former name moves to the command's current name."""
    if module != "commands" or not isinstance(data, dict) or not isinstance(data.get("overwrites"), dict):
        return data
    overwrites = dict(data["overwrites"])
    for old, new in FORMER_COMMAND_NAMES.items():
        if old in overwrites:
            rule = overwrites.pop(old)
            overwrites.setdefault(new, rule)
    return {**data, "overwrites": overwrites}


@dataclass(frozen=True)
class Actor:
    """Who did something on the dashboard or through a command."""
    id: int
    name: str


class Settings:
    def __init__(self, db: Database, activity: "Any") -> None:
        self.db = db
        self.activity = activity
        self.schemas: dict[str, Schema] = {}
        self._stored: dict[tuple[int, str], dict] = {}
        self._merged: dict[tuple[int, str], dict] = {}
        self._listeners: dict[str, list[Listener]] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}

    async def load(self) -> None:
        await self.db.migrate("settings", MIGRATIONS)
        for row in await self.db.all("SELECT guild_id, module, data FROM guild_settings"):
            self._stored[(row["guild_id"], row["module"])] = upgraded(row["module"], json.loads(row["data"]))

    def register(self, module: str, schema: Schema) -> None:
        self.schemas[module] = schema

    def listen(self, module: str, listener: Listener) -> None:
        """Run `listener(guild_id, new_settings)` after every save of this module."""
        self._listeners.setdefault(module, []).append(listener)

    def get(self, guild_id: int, module: str) -> dict:
        key = (guild_id, module)
        merged = self._merged.get(key)
        if merged is None:
            merged = self.schemas[module].merged(self._stored.get(key))
            self._merged[key] = merged
        return merged

    def guilds_with(self, module: str) -> list[int]:
        """Servers that have saved this module at least once. Timers and imports walk these."""
        return [guild_id for guild_id, name in self._stored if name == module]

    async def save(self, guild_id: int, module: str, form: dict, *, actor: Actor | None = None, action: str | None = None) -> dict:
        """Check a form against the schema, store it and log it. Fields it leaves out keep their current value.

        Saves of one module run one at a time, so a second can't start from what the first is replacing. Listeners run after that, so one can save again.
        """
        schema = self.schemas[module]
        async with self._locks.setdefault((guild_id, module), asyncio.Lock()):
            before = self.get(guild_id, module)
            after = schema.clean(form, base=before)
            changed = after != before
            await self._write(guild_id, module, after, actor)
            if changed:
                await self.activity.record(guild_id, actor, action or f"{module}.update", module=module, before=before, after=after)
        if changed:
            for listener in self._listeners.get(module, []):
                await listener(guild_id, after)
        return after

    async def restore(self, guild_id: int, module: str, data: dict, *, actor: Actor | None = None) -> dict:
        """Put back an earlier saved state, for undo and for imports."""
        schema = self.schemas[module]
        async with self._locks.setdefault((guild_id, module), asyncio.Lock()):
            before = self.get(guild_id, module)
            after = schema.merged(upgraded(module, data))
            await self._write(guild_id, module, after, actor)
            await self.activity.record(guild_id, actor, f"{module}.restore", module=module, before=before, after=after)
        for listener in self._listeners.get(module, []):
            await listener(guild_id, after)
        return after

    async def _write(self, guild_id: int, module: str, data: dict, actor: Actor | None) -> None:
        await self.db.run(
            "INSERT INTO guild_settings (guild_id, module, data, updated_at, updated_by) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, module) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (guild_id, module, json.dumps(data), time.time(), actor.id if actor else None),
        )
        key = (guild_id, module)
        self._stored[key] = data
        self._merged[key] = data
