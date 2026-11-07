"""The record of who changed what from the dashboard or a settings command, per server.

The Activity page reads this. A row that holds `before` for a settings module can be undone with one click.
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..db import Database

MIGRATIONS = [
    """
    CREATE TABLE activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        at REAL NOT NULL,
        user_id INTEGER,
        user_name TEXT,
        action TEXT NOT NULL,
        detail TEXT,
        module TEXT,
        before_data TEXT,
        after_data TEXT
    );
    CREATE INDEX activity_guild ON activity (guild_id, id DESC);
    """,
]


ACTIVITY_PER_SERVER = 5000
COMMAND_USE_DAYS = 90
PRUNE_CHUNK = 2000


class Activity:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def load(self) -> None:
        await self.db.migrate("activity", MIGRATIONS)

    async def record(self, guild_id: int, actor: Any, action: str, *, detail: str | None = None, module: str | None = None,
                     before: Any = None, after: Any = None) -> None:
        await self.db.insert(
            "INSERT INTO activity (guild_id, at, user_id, user_name, action, detail, module, before_data, after_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, time.time(), actor.id if actor else None, actor.name if actor else None, action, detail, module,
             json.dumps(before) if before is not None else None, json.dumps(after) if after is not None else None),
        )

    async def prune(self, now: float | None = None) -> int:
        """Keep the newest rows of each server's activity, whole settings forms and all, and the last few months of command counts."""
        removed = 0
        guild_id = await self.db.scalar("SELECT MIN(guild_id) FROM activity")
        while guild_id is not None:
            oldest_kept = await self.db.scalar("SELECT id FROM activity WHERE guild_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?", (guild_id, ACTIVITY_PER_SERVER - 1))
            if oldest_kept is not None:
                removed += await self.db.run("DELETE FROM activity WHERE guild_id = ? AND id < ?", (guild_id, oldest_kept))
            guild_id = await self.db.scalar("SELECT MIN(guild_id) FROM activity WHERE guild_id > ?", (guild_id,))
        oldest_day = int((time.time() if now is None else now) // 86400) - COMMAND_USE_DAYS
        while True:
            count = await self.db.run("DELETE FROM command_uses WHERE rowid IN (SELECT rowid FROM command_uses WHERE day < ? LIMIT ?)", (oldest_day, PRUNE_CHUNK))
            removed += count
            if count < PRUNE_CHUNK:
                return removed

    async def recent(self, guild_id: int, *, limit: int = 200, before_id: int | None = None) -> list[dict]:
        rows = await self.db.all(
            "SELECT id, at, user_id, user_name, action, detail, module, before_data IS NOT NULL AS undoable FROM activity "
            "WHERE guild_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
            (guild_id, before_id if before_id is not None else 2 ** 62, min(limit, 1000)),
        )
        return rows

    async def one(self, guild_id: int, entry_id: int) -> dict | None:
        row = await self.db.one("SELECT * FROM activity WHERE guild_id = ? AND id = ?", (guild_id, entry_id))
        if row is None:
            return None
        row["before"] = json.loads(row.pop("before_data")) if row["before_data"] else None
        row["after"] = json.loads(row.pop("after_data")) if row["after_data"] else None
        return row
