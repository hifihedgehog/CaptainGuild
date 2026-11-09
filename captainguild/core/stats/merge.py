"""Merged channels: many channels' counts folded into one channel that exists only in statistics.

A merged channel has a number of its own, far below any Discord ID. It lives in the server's `aggregates` setting and in stat_channels with
type 999, so lists and filters treat it like any other channel, filed under its category when it has one. Its counts arrive two ways: new
activity in the categories it auto-merges, which tracking sends there as it happens, and a merge, which moves what's already recorded.

A merge moves one channel's rows at a time in batches, each in a short transaction of its own, so the bot's other writes never wait long. A
moved row adds its count to the merged channel's row for the same quarter hour, person and kind, so every total stays as it was. Rows from
after the merge started stay where they are, so a channel still in use keeps its new activity.

A channel deleted in Discord that holds nothing after the merge can be forgotten: its name leaves the channel lists. A channel that still
exists in Discord is never forgotten.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from ...db import Database

AGGREGATE_TYPE = 999
CATEGORY_TYPE = 4
BATCH_ROWS = 5000
COUNT_TABLES = ("stat_messages", "stat_voice", "stat_emoji")
KIND_TABLES = {"messages": ("stat_messages", "stat_emoji"), "voice": ("stat_voice",), "both": ("stat_messages", "stat_emoji", "stat_voice")}


@dataclass(frozen=True)
class Spec:
    columns: tuple[str, ...]
    key: tuple[str, ...]
    value: str


SPECS = {
    "stat_messages": Spec(("guild_id", "slot", "channel_id", "user_id", "flags", "count"), ("guild_id", "slot", "channel_id", "user_id", "flags"), "count"),
    "stat_voice": Spec(("guild_id", "slot", "channel_id", "user_id", "state", "flags", "seconds"), ("guild_id", "slot", "channel_id", "user_id", "state", "flags"), "seconds"),
    "stat_emoji": Spec(("guild_id", "slot", "channel_id", "user_id", "target_id", "emoji", "use", "flags", "count"),
                       ("guild_id", "slot", "channel_id", "user_id", "target_id", "emoji", "use", "flags"), "count"),
}


def name_pattern(text: str) -> re.Pattern:
    """A channel name pattern: * stands for any run of characters, a backslash takes the next character as it is, and case doesn't matter.
    `ticket-*` takes ticket-1 and Ticket-Archive. `\\*important` takes a channel named *important."""
    out: list[str] = []
    escaped = False
    for character in text:
        if escaped:
            out.append(re.escape(character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "*":
            out.append(".*")
        else:
            out.append(re.escape(character))
    if escaped:  # a backslash at the end has nothing to escape, so it stands for itself
        out.append(re.escape("\\"))
    return re.compile("".join(out), re.IGNORECASE | re.DOTALL)


def fits(name: str, patterns: Iterable[re.Pattern]) -> bool:
    return any(pattern.fullmatch(name or "") for pattern in patterns)


async def choose(db: Database, guild_id: int, *, target_id: int, categories: Iterable[int] = (), patterns: Iterable[str] = (),
                 picked: Iterable[int] = ()) -> list[dict]:
    """The channels a merge takes: those filed under the categories, those whose names fit a pattern, and those picked one by one, deleted
    channels included. Never the destination itself or a category, which holds no counts of its own. Oldest first."""
    categories, picked = set(categories), set(picked)
    compiled = [name_pattern(text) for text in patterns if text]
    rows = await db.all("SELECT channel_id, name, type, parent_id, created_at, deleted_at FROM stat_channels WHERE guild_id = ?", (guild_id,))
    chosen = [row for row in rows if row["channel_id"] != target_id and row["type"] != CATEGORY_TYPE
              and (row["channel_id"] in picked or (row["parent_id"] is not None and row["parent_id"] in categories) or fits(row["name"], compiled))]
    return sorted(chosen, key=lambda row: (row["created_at"] or 0, row["channel_id"]))


async def holds_counts(db: Database, guild_id: int, channel_id: int, tables: Iterable[str] = COUNT_TABLES) -> bool:
    """Whether any count is recorded under this channel. Each look is one step down the table's per-channel index."""
    for table in tables:
        if await db.scalar(f"SELECT 1 FROM {table} WHERE guild_id = ? AND channel_id = ? LIMIT 1", (guild_id, channel_id)):
            return True
    return False


async def rows_in(db: Database, guild_id: int, channel_ids: Iterable[int], tables: Iterable[str], until_slot: int) -> dict[int, int]:
    """How many rows from before `until_slot` each channel holds in the tables, read 500 channels at a time down the per-channel indexes."""
    channel_ids = sorted(set(channel_ids))
    out = {channel_id: 0 for channel_id in channel_ids}
    for table in tables:
        for start in range(0, len(channel_ids), 500):
            chunk = channel_ids[start:start + 500]
            marks = ", ".join("?" for _ in chunk)
            for row in await db.all(f"SELECT channel_id, COUNT(*) AS n FROM {table} WHERE guild_id = ? AND channel_id IN ({marks}) AND slot < ? GROUP BY channel_id",
                                    (guild_id, *chunk, until_slot)):
                out[row["channel_id"]] += row["n"]
    return out


async def move_batch(db: Database, guild_id: int, source_id: int, target_id: int, table: str, until_slot: int, *,
                     size: int | None = None, then: Callable[[int], Awaitable[None]] | None = None) -> int:
    """Moves up to about `size` of one channel's rows from before `until_slot` to another channel, in one transaction, and returns how many
    moved. 0 means none are left. `then` runs inside the same transaction with that number, so progress saved there can't drift from the rows."""
    spec = SPECS[table]
    size = size or BATCH_ROWS
    async with db.transaction():
        first = await db.scalar(f"SELECT slot FROM {table} WHERE guild_id = ? AND channel_id = ? AND slot < ? ORDER BY slot LIMIT 1",
                                (guild_id, source_id, until_slot))
        if first is None:
            return 0
        edge = await db.scalar(f"SELECT slot FROM {table} WHERE guild_id = ? AND channel_id = ? AND slot < ? ORDER BY slot LIMIT 1 OFFSET ?",
                               (guild_id, source_id, until_slot, size))
        if edge is None:
            edge = until_slot
        elif edge == first:  # more than a batch in one quarter hour, which goes whole
            edge = first + 1
        names = ", ".join(spec.columns)
        picked = ", ".join("?" if column == "channel_id" else column for column in spec.columns)
        await db.run(f"INSERT INTO {table} ({names}) SELECT {picked} FROM {table} WHERE guild_id = ? AND channel_id = ? AND slot >= ? AND slot < ? "
                     f"ON CONFLICT ({', '.join(spec.key)}) DO UPDATE SET {spec.value} = {spec.value} + excluded.{spec.value}",
                     (target_id, guild_id, source_id, first, edge))
        moved = await db.run(f"DELETE FROM {table} WHERE guild_id = ? AND channel_id = ? AND slot >= ? AND slot < ?", (guild_id, source_id, first, edge))
        if then is not None:
            await then(moved)
    return moved


async def forget_if_empty(db: Database, guild_id: int, channel_id: int, *, deleted_in_discord: bool) -> bool:
    """Removes a channel from the lists when Discord no longer has it and it holds no counts. True when it went."""
    if not deleted_in_discord:
        return False
    async with db.transaction():
        if await holds_counts(db, guild_id, channel_id):
            return False
        return bool(await db.run("DELETE FROM stat_channels WHERE guild_id = ? AND channel_id = ? AND type != ?", (guild_id, channel_id, AGGREGATE_TYPE)))


def channel_summary(rows: list[dict], live_ids: set[int] | None) -> dict[str, int]:
    """How many channels a server keeps in its statistics, and how many of them Discord no longer has."""
    deleted = 0
    for row in rows:
        if row["type"] == AGGREGATE_TYPE:
            continue
        gone = row["deleted_at"] is not None or (live_ids is not None and row["channel_id"] not in live_ids)
        deleted += gone
    return {"channels": sum(1 for row in rows if row["type"] != AGGREGATE_TYPE), "deleted": deleted}


def kinds_label(kinds: str) -> str:
    return {"messages": "messages", "voice": "voice time", "both": "messages and voice time"}.get(kinds, kinds)


def describe(row: dict[str, Any]) -> dict[str, Any]:
    """A merge's row as the page shows it."""
    return {name: row[name] for name in ("target_id", "kinds", "forget", "state", "total", "done", "rows_moved", "forgotten", "note", "started_by_name", "created_at", "finished_at")}
