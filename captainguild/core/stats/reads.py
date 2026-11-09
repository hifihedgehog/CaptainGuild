"""Reads behind the statistics cards, kept quick on a server with years of counts.

query.py leaves the choice of index to SQLite, which picks the slot range of the primary key even for one person's counts, and sorts every
row it reads to rank people or channels. On a server with a million and a half rows that costs over a second a ranking. These reads name the
index that already holds the rows in the order the answer needs:

- one person's or one channel's counts read that person's or channel's own index;
- a ranking of people or channels over a long period reads a covering index kept in that order, so nothing is sorted (the indexes are made
  in statistics_commands.py's message-database migrations);
- a chart reads each bucket's slot range once, adding up the counts and counting the different people in the same pass.

A long list of people to keep or leave out goes to SQLite as one JSON array, so a role filter that names forty thousand members costs one
lookup per row, not a statement too long to send.

The filters mean what they mean in query.py: query.where builds every clause except the people in it.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable, Sequence

from ...db import Database
from . import query

TABLES = {"messages": ("stat_messages", "count"), "voice": ("stat_voice", "seconds"), "activities": ("stat_activities", "seconds"), "emoji": ("stat_emoji", "count")}
LONG_SLOTS = 200 * 96  # past this, a ranking reads the covering index, whose cost doesn't grow with the period, instead of sorting the slot range.
# On a server with 1.6 million rows the two cost the same at about 200 to 250 days: 0.19 s and 0.23 s at 180 days, 0.35 s and 0.25 s at a year.
INLINE_PEOPLE = 200  # past this many, a list of people goes to SQLite as JSON
# One person's rows: the core's index by person and slot, or for a long period the covering one, which needs no trip to the table per row
PERSON_INDEX = {"messages": "stat_messages_user", "voice": "stat_voice_user", "activities": "stat_activities_user", "emoji": "stat_emoji_user"}
PERSON_LONG_INDEX = {"messages": "stat_messages_user"}
CHANNEL_INDEX = {"messages": "stat_messages_channel", "voice": "stat_voice_channel"}
CHANNEL_LONG_INDEX = {"messages": "stat_messages_channel"}
# Rankings over a long period
RANK_PEOPLE_INDEX = {"messages": "stat_messages_user"}
RANK_CHANNEL_INDEX = {"messages": "stat_messages_channel"}



def is_long(start: int | None, end: int | None) -> bool:
    return start is None or end is None or end - start > LONG_SLOTS


def grouping(column: str, table: str) -> str:
    """How a read groups by a column. With an index named, the rows come in whatever order that index holds and the column groups as it is.
    Left to itself, SQLite can walk a per-person or per-channel index to get rows in the column's order, which visits a table page for every
    row: grouping by +column, the same value, keeps it on the slot range and sorts the totals instead."""
    return column if " INDEXED BY " in table else f"+{column}"


def _marks(values: Iterable[Any]) -> tuple[str, list[Any]]:
    values = list(values)
    return ", ".join("?" for _ in values), values


def _people(clause: query.Clause, f: query.Filters) -> None:
    if f.members_in is not None:
        if not f.members_in:
            clause.add("0")
        elif len(f.members_in) > INLINE_PEOPLE:
            clause.add("user_id IN (SELECT value FROM json_each(?))", json.dumps(sorted(f.members_in)))
        else:
            marks, values = _marks(sorted(f.members_in))
            clause.add(f"user_id IN ({marks})", *values)
    if f.members_out:
        if len(f.members_out) > INLINE_PEOPLE:
            clause.add("user_id NOT IN (SELECT value FROM json_each(?))", json.dumps(sorted(f.members_out)))
        else:
            marks, values = _marks(sorted(f.members_out))
            clause.add(f"user_id NOT IN ({marks})", *values)


def where(kind: str, guild_id: int, f: query.Filters, start: int | None, end: int | None, *, user_id: int | None = None,
          channels: Iterable[int] | None = None, people: bool = True) -> query.Clause:
    """The WHERE clause for one count table: the server's filters, then one person or a set of channels when asked. `people` False leaves
    out the filters on who, as for reactions others put on someone's messages."""
    clause = query.where(kind, guild_id, f, start, end, people=False)
    if people:
        _people(clause, f)
    if user_id is not None:
        clause.add("user_id = ?", user_id)
    if channels is not None:
        channels = sorted(set(channels))
        if not channels:
            clause.add("0")
        else:
            marks, values = _marks(channels)
            clause.add(f"channel_id IN ({marks})", *values)
    return clause


def source(kind: str, start: int | None, end: int | None, *, user_id: int | None = None, channels: Any = None, rank: str | None = None) -> str:
    """The table, with the index this read should use."""
    table, _ = TABLES[kind]
    long = is_long(start, end)
    index = None
    if user_id is not None:
        index = (PERSON_LONG_INDEX.get(kind) if long else None) or PERSON_INDEX.get(kind)
    elif channels is not None:
        index = (CHANNEL_LONG_INDEX.get(kind) if long else None) or CHANNEL_INDEX.get(kind)
    elif rank == "user" and long:
        index = RANK_PEOPLE_INDEX.get(kind)
    elif rank == "channel" and long:
        index = RANK_CHANNEL_INDEX.get(kind)
    return f"{table} INDEXED BY {index}" if index else table


async def by_user(db: Database, kind: str, guild_id: int, f: query.Filters, start: int | None, end: int | None, *,
                  channels: Iterable[int] | None = None) -> dict[int, int]:
    """Each person's total, user 0 (everyone who chose privacy) included, so the values add up to the period's total."""
    channels = None if channels is None else set(channels)
    clause = where(kind, guild_id, f, start, end, channels=channels)
    _, value = TABLES[kind]
    table = source(kind, start, end, channels=channels, rank="user")
    rows = await db.all(f"SELECT user_id, SUM({value}) AS total FROM {table} WHERE {clause.text()} GROUP BY {grouping('user_id', table)}", clause.args)
    return {row["user_id"]: int(row["total"] or 0) for row in rows if row["total"]}


async def by_channel(db: Database, kind: str, guild_id: int, f: query.Filters, start: int | None, end: int | None, *,
                     user_id: int | None = None) -> dict[int, int]:
    clause = where(kind, guild_id, f, start, end, user_id=user_id)
    _, value = TABLES[kind]
    table = source(kind, start, end, user_id=user_id, rank="channel")
    rows = await db.all(f"SELECT channel_id, SUM({value}) AS total FROM {table} WHERE {clause.text()} GROUP BY {grouping('channel_id', table)}", clause.args)
    return {row["channel_id"]: int(row["total"] or 0) for row in rows if row["total"]}


async def by_channel_people(db: Database, kind: str, guild_id: int, f: query.Filters, start: int | None, end: int | None) -> dict[int, tuple[int, int]]:
    """Each channel's total and how many different people made it, for a ranking of channels."""
    clause = where(kind, guild_id, f, start, end)
    _, value = TABLES[kind]
    table = source(kind, start, end, rank="channel")
    rows = await db.all(f"SELECT channel_id, SUM({value}) AS total, COUNT(DISTINCT NULLIF(user_id, 0)) AS people FROM {table} "
                        f"WHERE {clause.text()} GROUP BY {grouping('channel_id', table)}", clause.args)
    return {row["channel_id"]: (int(row["total"] or 0), int(row["people"] or 0)) for row in rows if row["total"]}


async def windows(db: Database, kind: str, guild_id: int, f: query.Filters, starts: Sequence[int | None], end: int, *, user_id: int | None = None,
                  channels: Iterable[int] | None = None, people: bool = True) -> list[tuple[int, int]]:
    """(total, different people) since each start, all to `end`, in one read of the widest. Meant for short windows such as a day, a week
    and a month: a long one is quicker through by_user."""
    channels = None if channels is None else set(channels)
    earliest = None if any(start is None for start in starts) else min(starts)
    clause = where(kind, guild_id, f, earliest, end, user_id=user_id, channels=channels)
    _, value = TABLES[kind]
    parts: list[str] = []
    args: list[Any] = []
    for index, start in enumerate(starts):  # each column gets its own name, since a row comes back as a dict by name
        test = "1" if start is None else "slot >= ?"
        parts.append(f"COALESCE(SUM(CASE WHEN {test} THEN {value} ELSE 0 END), 0) AS total{index}")
        args.extend([] if start is None else [start])
        if people:
            parts.append(f"COUNT(DISTINCT CASE WHEN {test} AND user_id != 0 THEN user_id END) AS people{index}")
            args.extend([] if start is None else [start])
    table = source(kind, earliest, end, user_id=user_id, channels=channels)
    row = await db.one(f"SELECT {', '.join(parts)} FROM {table} WHERE {clause.text()}", [*args, *clause.args]) or {}
    return [(int(row.get(f"total{index}") or 0), int(row.get(f"people{index}") or 0)) for index in range(len(starts))]


def _buckets_cte(edges: Sequence[int], end: int) -> tuple[str, list[int]]:
    """The buckets as a table SQLite reads in order: (index, first slot, slot past the last). Queries join it with CROSS JOIN, which keeps it
    the outer loop: each bucket then reads its own slot range once, where the other way round every row would be checked against every bucket."""
    rows = []
    args: list[int] = []
    for index, low in enumerate(edges):
        high = edges[index + 1] if index + 1 < len(edges) else end
        rows.append("(?, ?, ?)")
        args.extend((index, low, high))
    return f"WITH b(idx, lo, hi) AS (VALUES {', '.join(rows)})", args


async def buckets(db: Database, kind: str, guild_id: int, f: query.Filters, edges: Sequence[int], end: int, *, user_id: int | None = None,
                  channels: Iterable[int] | None = None, people: bool = False) -> list[tuple[int, int]]:
    """(total, different people) in each bucket. `edges` are the slots the buckets start at, and the last one runs to `end`."""
    if not edges:
        return []
    channels = None if channels is None else set(channels)
    clause = where(kind, guild_id, f, None, None, user_id=user_id, channels=channels)
    table, value = TABLES[kind]
    index = PERSON_INDEX.get(kind) if user_id is not None else None
    joined = f"{table} AS m INDEXED BY {index}" if index else f"{table} AS m"
    cte, args = _buckets_cte(edges, end)
    counted = ", COUNT(DISTINCT NULLIF(m.user_id, 0)) AS people" if people else ""
    sql = (f"{cte} SELECT b.idx AS idx, SUM(m.{value}) AS total{counted} FROM b CROSS JOIN {joined} ON m.guild_id = ? AND m.slot >= b.lo AND m.slot < b.hi "
           f"WHERE {clause.text()} GROUP BY b.idx")
    out = [(0, 0)] * len(edges)
    for row in await db.all(sql, [*args, guild_id, *clause.args]):
        out[row["idx"]] = (int(row["total"] or 0), int(row.get("people") or 0))
    return out


async def bucket_series(db: Database, kind: str, guild_id: int, f: query.Filters, edges: Sequence[int], end: int, column: str, keys: Sequence[Any], *,
                        user_id: int | None = None, channels: Iterable[int] | None = None) -> dict[Any, list[int]]:
    """Each of these activities or emoji (by `column`) bucket by bucket, for a chart of the top few."""
    out = {key: [0] * len(edges) for key in keys}
    if not edges or not keys:
        return out
    clause = where(kind, guild_id, f, None, None, user_id=user_id, channels=channels)
    marks, values = _marks(keys)
    clause.add(f"{column} IN ({marks})", *values)
    table, value = TABLES[kind]
    cte, args = _buckets_cte(edges, end)
    sql = (f"{cte} SELECT b.idx AS idx, m.{column} AS key, SUM(m.{value}) AS total FROM b CROSS JOIN {table} AS m ON m.guild_id = ? AND m.slot >= b.lo "
           f"AND m.slot < b.hi WHERE {clause.text()} GROUP BY b.idx, m.{column}")
    for row in await db.all(sql, [*args, guild_id, *clause.args]):
        if row["key"] in out:
            out[row["key"]][row["idx"]] += int(row["total"] or 0)
    return out


async def by_emoji(db: Database, guild_id: int, f: query.Filters, start: int | None, end: int | None, *, user_id: int | None = None,
                   channels: Iterable[int] | None = None) -> dict[str, int]:
    """Uses of each emoji and sticker."""
    clause = where("emoji", guild_id, f, start, end, user_id=user_id, channels=channels)
    table = source("emoji", start, end, user_id=user_id)
    rows = await db.all(f"SELECT emoji, SUM(count) AS total FROM {table} WHERE {clause.text()} GROUP BY {grouping('emoji', table)}", clause.args)
    return {row["emoji"]: int(row["total"] or 0) for row in rows if row["total"]}


async def by_activity(db: Database, guild_id: int, f: query.Filters, start: int | None, end: int | None, *,
                      user_id: int | None = None) -> dict[int, tuple[int, int]]:
    """Seconds in each activity, and how many different people spent them."""
    clause = where("activities", guild_id, f, start, end, user_id=user_id)
    table = source("activities", start, end, user_id=user_id)
    rows = await db.all(f"SELECT activity_id, SUM(seconds) AS total, COUNT(DISTINCT NULLIF(user_id, 0)) AS people FROM {table} "
                        f"WHERE {clause.text()} GROUP BY {grouping('activity_id', table)}", clause.args)
    return {row["activity_id"]: (int(row["total"] or 0), int(row["people"] or 0)) for row in rows if row["total"]}


async def first_slot(db: Database, guild_id: int) -> int | None:
    """The first quarter hour with anything recorded: when statistics began, or the oldest imported or recounted count."""
    found = []
    for table in ("stat_members", "stat_messages", "stat_voice"):
        slot = await db.scalar(f"SELECT MIN(slot) FROM {table} WHERE guild_id = ?", (guild_id,))
        if slot is not None:
            found.append(slot)
    return min(found) if found else None


async def first_kind_slot(db: Database, kind: str, guild_id: int, *, user_id: int | None = None, channels: Iterable[int] | None = None) -> int | None:
    """The first slot holding this kind of count, for one person or some channels when asked."""
    table, _ = TABLES[kind]
    if user_id is not None and kind in PERSON_INDEX:
        return await db.scalar(f"SELECT MIN(slot) FROM {table} INDEXED BY {PERSON_INDEX[kind]} WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    if channels is not None:
        channels = sorted(set(channels))
        if not channels:
            return None
        marks, values = _marks(channels)
        index = CHANNEL_INDEX.get(kind)
        indexed = f" INDEXED BY {index}" if index else ""
        found = [await db.scalar(f"SELECT MIN(slot) FROM {table}{indexed} WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)) for channel_id in values]
        found = [slot for slot in found if slot is not None]
        return min(found) if found else None
    return await db.scalar(f"SELECT MIN(slot) FROM {table} WHERE guild_id = ?", (guild_id,))


async def member_buckets(db: Database, guild_id: int, edges: Sequence[int], end: int) -> list[dict]:
    """Per bucket: the member count at its last reading (carried forward from before), and its joins and leaves. The same answer as
    query.member_counts, read a bucket at a time."""
    if not edges:
        return []
    cte, args = _buckets_cte(edges, end)
    sql = (f"{cte} SELECT b.idx AS idx, "
           "(SELECT members FROM stat_members WHERE guild_id = ? AND slot < b.hi AND members IS NOT NULL ORDER BY slot DESC LIMIT 1) AS members, "
           "(SELECT humans FROM stat_members WHERE guild_id = ? AND slot < b.hi AND members IS NOT NULL ORDER BY slot DESC LIMIT 1) AS humans, "
           "(SELECT COALESCE(SUM(joins), 0) FROM stat_members WHERE guild_id = ? AND slot >= b.lo AND slot < b.hi) AS joins, "
           "(SELECT COALESCE(SUM(leaves), 0) FROM stat_members WHERE guild_id = ? AND slot >= b.lo AND slot < b.hi) AS leaves FROM b ORDER BY b.idx")
    rows = await db.all(sql, [*args, guild_id, guild_id, guild_id, guild_id])
    return [{"members": row["members"], "humans": row["humans"], "joins": int(row["joins"] or 0), "leaves": int(row["leaves"] or 0)} for row in rows]


async def status_buckets(db: Database, guild_id: int, include: str, edges: Sequence[int], end: int) -> list[dict]:
    """Per bucket, the average number of people online, idle and on do not disturb, and not offline: the same answer as
    query.status_series, added up in the database."""
    if not edges:
        return []
    cte, args = _buckets_cte(edges, end)

    def reading(name: str) -> str:
        people = f"s.{name} * 1.0 / s.samples"
        bots = f"s.bot_{name} * 1.0 / s.samples"
        return people if include == "humans" else bots if include == "bots" else f"({people} + {bots})"

    sql = (f"{cte} SELECT b.idx AS idx, SUM(CASE WHEN s.samples > 0 THEN 1 ELSE 0 END) AS n, "
           + ", ".join(f"SUM(CASE WHEN s.samples > 0 THEN {reading(name)} END) AS {name}" for name in query.STATUS_NAMES)
           + ", SUM(CASE WHEN s.present_samples > 0 THEN 1 ELSE 0 END) AS pn, SUM(CASE WHEN s.present_samples > 0 THEN s.present * 1.0 / s.present_samples END) AS present "
           "FROM b CROSS JOIN stat_status AS s ON s.guild_id = ? AND s.slot >= b.lo AND s.slot < b.hi GROUP BY b.idx")
    out = [{"online": None, "idle": None, "dnd": None, "present": None} for _ in edges]
    for row in await db.all(sql, [*args, guild_id]):
        entry = out[row["idx"]]
        if row["n"]:
            for name in query.STATUS_NAMES:
                entry[name] = (row[name] or 0) / row["n"]
            entry["present"] = sum(entry[name] for name in query.STATUS_NAMES)
        elif row["pn"]:
            entry["present"] = (row["present"] or 0) / row["pn"]
    return out


async def joins(db: Database, guild_id: int, start: float | None, end: float | None) -> list[dict]:
    """Every join in [start, end), in Unix seconds, with what decides whether it counts."""
    sql = "SELECT user_id, at, code, inviter_id, account_created, facts, role_met_at, left_at FROM stat_joins WHERE guild_id = ?"
    args: list[Any] = [guild_id]
    if start is not None:
        sql += " AND at >= ?"
        args.append(start)
    if end is not None:
        sql += " AND at < ?"
        args.append(end)
    return await db.all(sql + " ORDER BY at", args)


async def invites(db: Database, guild_id: int) -> dict[str, dict]:
    return {row["code"]: row for row in await db.all("SELECT * FROM stat_invites WHERE guild_id = ?", (guild_id,))}


def ranked(totals: dict[Any, int], *, skip_zero_key: bool = True) -> list[tuple[Any, int]]:
    """Most first, ties to the lower key, so pages don't shift between reads. User 0 (privacy) is never ranked."""
    return sorted(((key, value) for key, value in totals.items() if value and not (skip_zero_key and key == 0)), key=lambda item: (-item[1], item[0]))


def rank_in(totals: dict[int, int], user_id: int) -> tuple[int | None, int]:
    """(rank, how many were ranked). Rank is None for someone with nothing."""
    mine = totals.get(user_id, 0)
    ranked_count = sum(1 for key, value in totals.items() if key != 0 and value)
    if not mine or user_id == 0:
        return None, ranked_count
    ahead = sum(1 for key, value in totals.items() if key != 0 and (value > mine or (value == mine and key < user_id)))
    return ahead + 1, ranked_count


def merge_people(*totals: dict[int, int]) -> set[int]:
    found: set[int] = set()
    for one in totals:
        found.update(key for key, value in one.items() if key != 0 and value)
    return found


def folded(rows: Iterable[tuple[Any, int]]) -> dict[Any, int]:
    out: dict[Any, int] = defaultdict(int)
    for key, value in rows:
        out[key] += value
    return dict(out)
