"""Who did it. Discord's audit log names the moderator behind a ban, a deleted message or a role change.

The gateway sends audit log entries as they happen, and an entry can arrive just after the event it explains. `find` waits a
moment for it. `recent` reads the log over REST, for cases the gateway doesn't announce, such as a count going up on an entry
for repeated message deletes.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import discord

KEEP_PER_GUILD = 500


def target_id_of(entry: Any) -> int | None:
    """The ID of what an entry acted on. discord.py builds `target` on first read, and for a channel entry Discord sent with no
    target ID that build fails, so the raw ID comes first and the failure means there is none."""
    raw = getattr(entry, "_target_id", None)
    if raw is not None:
        return raw
    try:
        return getattr(entry.target, "id", None)
    except (TypeError, ValueError):
        return None


@dataclass
class Entry:
    guild_id: int
    action: discord.AuditLogAction
    target_id: int | None
    user_id: int | None
    user: Any
    reason: str | None
    extra: Any
    changes: Any
    at: float
    id: int = 0


class AuditWatcher:
    def __init__(self) -> None:
        self._entries: dict[int, deque[Entry]] = {}

    def feed(self, entry: Any) -> Entry:
        """Called with discord.AuditLogEntry from on_audit_log_entry_create."""
        kept = Entry(
            guild_id=entry.guild.id,
            action=entry.action,
            target_id=target_id_of(entry),
            user_id=entry.user_id,
            user=entry.user,
            reason=entry.reason,
            extra=entry.extra,
            changes=entry.changes,
            at=time.time(),
            id=getattr(entry, "id", 0),
        )
        self._entries.setdefault(entry.guild.id, deque(maxlen=KEEP_PER_GUILD)).append(kept)
        return kept

    def _search(self, guild_id: int, action: discord.AuditLogAction, target_id: int | None, within: float, where: Callable[[Entry], bool] | None = None) -> Entry | None:
        cutoff = time.time() - within
        for entry in reversed(self._entries.get(guild_id, ())):
            if entry.at < cutoff:
                return None
            if entry.action == action and (target_id is None or entry.target_id == target_id) and (where is None or where(entry)):
                return entry
        return None

    async def find(self, guild_id: int, action: discord.AuditLogAction, target_id: int | None = None, *, within: float = 15.0, wait: float = 2.0,
                   where: Callable[[Entry], bool] | None = None) -> Entry | None:
        """The newest matching entry from the last `within` seconds, waiting up to `wait` seconds for it to arrive.

        `where` narrows the match, for when two changes to one target land close together and only the entry's changes tell them apart.
        """
        deadline = time.monotonic() + wait
        while True:
            entry = self._search(guild_id, action, target_id, within, where)
            if entry is not None or time.monotonic() >= deadline:
                return entry
            await asyncio.sleep(0.2)

    async def recent(self, guild: Any, action: discord.AuditLogAction, *, limit: int = 5) -> list[Any]:
        """Read the audit log directly. Empty if the bot lacks View Audit Log."""
        try:
            return [entry async for entry in guild.audit_logs(limit=limit, action=action)]
        except discord.HTTPException:
            return []
