"""Reading statistics back: totals, series, rankings and heatmaps, under the filters a server set.

Every function takes the database holding the counts (the bot's message database), a server, a Filters and a slot range [start, end),
where a start of None reaches back to the first row. A series takes the slots its buckets start at (timeframe.bucket_edges turned into
slots) and adds each slot into the bucket holding it. Nothing here touches Discord: the module turns roles into members first.
"""
from __future__ import annotations

import bisect
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from ...db import Database
from . import store
from .timeframe import SLOT_SECONDS, hour_and_weekday, slot_time

ALL_STATES = frozenset(store.VOICE_STATES.values())
SQL_LIST_LIMIT = 500  # a longer list of people is applied after the rows come back, which keeps statements short
KINDS = ("messages", "voice")
_TABLE = {"messages": ("stat_messages", "count"), "voice": ("stat_voice", "seconds"), "activities": ("stat_activities", "seconds"), "emoji": ("stat_emoji", "count")}


@dataclass(frozen=True)
class Filters:
    """What a view leaves in. An empty list filters nothing in either mode, and members_in of None means everyone.

    Channel lists can name categories, which cover every channel filed under them, deleted ones included. Voice reads its own list when
    voice_mode isn't "same".
    """
    include: str = "humans"  # humans, bots or both
    voice_states: frozenset[int] = ALL_STATES
    channel_mode: str = "block"
    channels: frozenset[int] = frozenset()
    voice_mode: str = "same"
    voice_channels: frozenset[int] = frozenset()
    members_in: frozenset[int] | None = None
    members_out: frozenset[int] = frozenset()
    hidden_channels: frozenset[int] = frozenset()  # left out whatever the lists say: channels the reader can't see
    activity_mode: str = "block"
    activities: frozenset[int] = frozenset()
    emoji_mode: str = "block"
    emoji: frozenset[str] = frozenset()
    emoji_uses: frozenset[int] = frozenset(store.EMOJI_USES.values())

    def only(self, user_id: int) -> "Filters":
        """The same filters narrowed to one person, who stays out if the filters leave them out."""
        if self.members_in is not None and user_id not in self.members_in or user_id in self.members_out:
            return replace(self, members_in=frozenset())
        return replace(self, members_in=frozenset({user_id}))

    def keeps(self, user_id: int) -> bool:
        return (self.members_in is None or user_id in self.members_in) and user_id not in self.members_out

    def in_python(self) -> bool:
        """Whether the people filters are too long to send in a statement, so rows come back per person and get filtered here."""
        return (self.members_in is not None and len(self.members_in) > SQL_LIST_LIMIT) or len(self.members_out) > SQL_LIST_LIMIT


@dataclass
class Clause:
    sql: list[str] = field(default_factory=list)
    args: list[Any] = field(default_factory=list)

    def add(self, sql: str, *args: Any) -> None:
        self.sql.append(sql)
        self.args.extend(args)

    def text(self) -> str:
        return " AND ".join(self.sql) if self.sql else "1"


def _marks(values: Iterable[Any]) -> tuple[str, list[Any]]:
    values = list(values)
    return ", ".join("?" for _ in values), values


def _channel_clause(clause: Clause, guild_id: int, mode: str, ids: frozenset[int]) -> None:
    if not ids:
        return
    marks, values = _marks(sorted(ids))
    inside = f"(channel_id IN ({marks}) OR channel_id IN (SELECT channel_id FROM stat_channels WHERE guild_id = ? AND parent_id IN ({marks})))"
    if mode == "allow":
        clause.add(inside, *values, guild_id, *values)
    else:
        clause.add(f"NOT {inside}", *values, guild_id, *values)


def where(kind: str, guild_id: int, f: Filters, start: int | None, end: int | None, *, people: bool = True) -> Clause:
    """The WHERE clause for one count table. `people` False leaves the per-person filters to the caller."""
    clause = Clause()
    clause.add("guild_id = ?", guild_id)
    if start is not None:
        clause.add("slot >= ?", start)
    if end is not None:
        clause.add("slot < ?", end)
    if kind in ("messages", "voice", "activities"):
        if f.include == "humans":
            clause.add("flags & 1 = 0")
        elif f.include == "bots":
            clause.add("flags & 1 = 1")
    if kind in ("messages", "emoji"):
        _channel_clause(clause, guild_id, f.channel_mode, f.channels)
    elif kind == "voice":
        if f.voice_mode == "same":
            _channel_clause(clause, guild_id, f.channel_mode, f.channels)
        else:
            _channel_clause(clause, guild_id, f.voice_mode, f.voice_channels)
        if f.voice_states != ALL_STATES:
            marks, values = _marks(sorted(f.voice_states))
            clause.add(f"state IN ({marks})", *values)
    if kind in ("messages", "emoji", "voice") and f.hidden_channels:
        marks, values = _marks(sorted(f.hidden_channels))
        clause.add(f"channel_id NOT IN ({marks})", *values)
    if kind == "activities" and f.activities:
        marks, values = _marks(sorted(f.activities))
        clause.add(f"activity_id {'IN' if f.activity_mode == 'allow' else 'NOT IN'} ({marks})", *values)
    if kind == "emoji":
        if f.emoji:
            marks, values = _marks(sorted(f.emoji))
            clause.add(f"emoji {'IN' if f.emoji_mode == 'allow' else 'NOT IN'} ({marks})", *values)
        if f.emoji_uses != frozenset(store.EMOJI_USES.values()):
            marks, values = _marks(sorted(f.emoji_uses))
            clause.add(f"use IN ({marks})", *values)
    if people and not f.in_python():
        if f.members_in is not None:
            marks, values = _marks(sorted(f.members_in))
            clause.add(f"user_id IN ({marks})" if values else "0", *values)
        if f.members_out:
            marks, values = _marks(sorted(f.members_out))
            clause.add(f"user_id NOT IN ({marks})", *values)
    return clause


async def _grouped(db: Database, kind: str, guild_id: int, f: Filters, start: int | None, end: int | None, columns: Sequence[str],
                   *, extra: str = "", extra_args: Sequence[Any] = ()) -> list[tuple]:
    """Sums of the kind's value grouped by `columns`, with the people filters applied wherever they're applied. Each row is the column
    values followed by the sum."""
    table, value = _TABLE[kind]
    python_people = f.in_python()
    group = list(columns) + (["user_id"] if python_people and "user_id" not in columns else [])
    clause = where(kind, guild_id, f, start, end)
    if extra:
        clause.add(extra, *extra_args)
    select = ", ".join(group) if group else ""
    sql = f"SELECT {select}{', ' if select else ''}SUM({value}) AS total FROM {table} WHERE {clause.text()}"
    if group:
        # A plain column here lets the planner walk the per-person or per-channel index to get rows in that order, decrypting a table page for every
        # row. Grouping by +column, which is the same value, keeps it on the table's own order: 1.5 million rows group in about 2 seconds, not 15.
        sql += " GROUP BY " + ", ".join(column if column == "slot" else f"+{column}" for column in group)
    rows = [tuple(row.values()) for row in await db.all(sql, clause.args)]
    if not python_people:
        return rows
    user_at = group.index("user_id")
    kept = [row for row in rows if f.keeps(row[user_at])]
    if "user_id" in columns:
        return kept
    folded: Counter = Counter()
    for row in kept:
        key = tuple(value for index, value in enumerate(row[:-1]) if index != user_at)
        folded[key] += row[-1] or 0
    return [(*key, total) for key, total in folded.items()]


async def total(db: Database, kind: str, guild_id: int, f: Filters, start: int | None, end: int | None) -> int:
    rows = await _grouped(db, kind, guild_id, f, start, end, [])
    return int(sum(row[-1] or 0 for row in rows))


def fold(edges: Sequence[int], slot: int) -> int | None:
    """Which bucket a slot falls in, or None before the first."""
    if not edges or slot < edges[0]:
        return None
    return bisect.bisect_right(edges, slot) - 1


async def series(db: Database, kind: str, guild_id: int, f: Filters, edges: Sequence[int], end: int) -> list[int]:
    """The kind's total in each bucket. `edges` are the slots the buckets start at, and the last bucket runs to `end`."""
    out = [0] * len(edges)
    if not edges:
        return out
    for slot, value in await _grouped(db, kind, guild_id, f, edges[0], end, ["slot"]):
        index = fold(edges, slot)
        if index is not None:
            out[index] += int(value or 0)
    return out


async def unique_series(db: Database, kinds: Sequence[str], what: str, guild_id: int, f: Filters, edges: Sequence[int], end: int) -> list[int]:
    """How many different people ("user_id") or channels ("channel_id") were active in each bucket, across the kinds named."""
    sets: list[set] = [set() for _ in edges]
    if not edges:
        return []
    for kind in kinds:
        for row in await _grouped(db, kind, guild_id, f, edges[0], end, ["slot", what]):
            index = fold(edges, row[0])
            if index is not None and (what != "user_id" or row[1] != 0):
                sets[index].add(row[1])
    return [len(found) for found in sets]


async def unique_count(db: Database, kinds: Sequence[str], what: str, guild_id: int, f: Filters, start: int | None, end: int | None) -> int:
    found: set = set()
    for kind in kinds:
        found.update(row[0] for row in await _grouped(db, kind, guild_id, f, start, end, [what]))
    if what == "user_id":
        found.discard(0)
    return len(found)


@dataclass
class Ranked:
    rows: list[tuple]  # (id, value, how many of the other thing: channels for a person, people for a channel)
    total: int         # how many were ranked


async def top_members(db: Database, kind: str, guild_id: int, f: Filters, start: int | None, end: int | None, *, limit: int | None = None,
                      offset: int = 0) -> Ranked:
    """People by the kind's total, most first. Ties go to the lower ID, so pages don't shift between reads. People who chose privacy aren't ranked."""
    per_pair = await _grouped(db, kind, guild_id, f, start, end, ["user_id", "channel_id"])
    totals: Counter = Counter()
    channels: Counter = Counter()
    for user_id, _channel_id, value in per_pair:
        if user_id == 0 or not value:
            continue
        totals[user_id] += value
        channels[user_id] += 1
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    page = ordered[offset:offset + limit] if limit is not None else ordered[offset:]
    return Ranked([(user_id, int(value), channels[user_id]) for user_id, value in page], len(ordered))


async def top_channels(db: Database, kind: str, guild_id: int, f: Filters, start: int | None, end: int | None, *, limit: int | None = None,
                       offset: int = 0) -> Ranked:
    per_pair = await _grouped(db, kind, guild_id, f, start, end, ["channel_id", "user_id"])
    totals: Counter = Counter()
    people: Counter = Counter()
    for channel_id, user_id, value in per_pair:
        if not value:
            continue
        totals[channel_id] += value
        if user_id != 0:
            people[channel_id] += 1
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    page = ordered[offset:offset + limit] if limit is not None else ordered[offset:]
    return Ranked([(channel_id, int(value), people[channel_id]) for channel_id, value in page], len(ordered))


async def rank_of(db: Database, kind: str, guild_id: int, f: Filters, start: int | None, end: int | None, user_id: int) -> tuple[int | None, int, int]:
    """(rank, their total, how many were ranked). Rank is None for someone with nothing in the period."""
    rows = await _grouped(db, kind, guild_id, f, start, end, ["user_id"])
    totals = {row[0]: row[1] for row in rows if row[0] != 0 and row[1]}
    mine = totals.get(user_id)
    if not mine:
        return None, 0, len(totals)
    ahead = sum(1 for other, value in totals.items() if value > mine or (value == mine and other < user_id))
    return ahead + 1, int(mine), len(totals)


async def favorite(db: Database, kind: str, guild_id: int, f: Filters, start: int | None, end: int | None, by: str) -> tuple[Any, int] | None:
    """The single biggest channel or person by the kind's total, or None."""
    rows = [row for row in await _grouped(db, kind, guild_id, f, start, end, [by]) if row[-1] and (by != "user_id" or row[0] != 0)]
    if not rows:
        return None
    best = min(rows, key=lambda row: (-row[-1], row[0]))
    return best[0], int(best[-1])


async def first_slot(db: Database, kind: str, guild_id: int) -> int | None:
    table, _ = _TABLE.get(kind, ("stat_members", None))
    return await db.scalar(f"SELECT MIN(slot) FROM {table} WHERE guild_id = ?", (guild_id,))


async def last_seen(db: Database, kind: str, guild_id: int, f: Filters, user_id: int) -> tuple[int, int] | None:
    """(slot, channel) of someone's most recent message or voice time."""
    clause = where(kind, guild_id, f.only(user_id), None, None)
    table, _ = _TABLE[kind]
    row = await db.one(f"SELECT slot, channel_id FROM {table} WHERE {clause.text()} ORDER BY slot DESC LIMIT 1", clause.args)
    return (row["slot"], row["channel_id"]) if row else None


async def first_seen(db: Database, kind: str, guild_id: int, f: Filters, user_id: int) -> tuple[int, int] | None:
    clause = where(kind, guild_id, f.only(user_id), None, None)
    table, _ = _TABLE[kind]
    row = await db.one(f"SELECT slot, channel_id FROM {table} WHERE {clause.text()} ORDER BY slot LIMIT 1", clause.args)
    return (row["slot"], row["channel_id"]) if row else None


async def channel_last_seen(db: Database, kind: str, guild_id: int, f: Filters, channel_id: int) -> tuple[int, int] | None:
    """(slot, person) of a channel's most recent message or voice time."""
    clause = where(kind, guild_id, f, None, None)
    clause.add("channel_id = ?", channel_id)
    table, _ = _TABLE[kind]
    row = await db.one(f"SELECT slot, user_id FROM {table} WHERE {clause.text()} ORDER BY slot DESC LIMIT 1", clause.args)
    return (row["slot"], row["user_id"]) if row else None


# ---- member counts and statuses ----

async def member_counts(db: Database, guild_id: int, edges: Sequence[int], end: int) -> list[dict]:
    """Per bucket: the member count at the bucket's last reading (carried forward through buckets with none), and joins and leaves."""
    out = [{"members": None, "humans": None, "bots": None, "joins": 0, "leaves": 0} for _ in edges]
    if not edges:
        return out
    before = await db.one("SELECT members, humans, bots FROM stat_members WHERE guild_id = ? AND slot < ? AND members IS NOT NULL ORDER BY slot DESC LIMIT 1",
                          (guild_id, edges[0]))
    rows = await db.all("SELECT slot, members, humans, bots, joins, leaves FROM stat_members WHERE guild_id = ? AND slot >= ? AND slot < ? ORDER BY slot",
                        (guild_id, edges[0], end))
    for row in rows:
        index = fold(edges, row["slot"])
        if index is None:
            continue
        bucket = out[index]
        if row["members"] is not None:
            bucket["members"], bucket["humans"], bucket["bots"] = row["members"], row["humans"], row["bots"]
        bucket["joins"] += row["joins"]
        bucket["leaves"] += row["leaves"]
    carried = dict(before) if before else None
    for bucket in out:
        if bucket["members"] is None and carried is not None:
            bucket["members"], bucket["humans"], bucket["bots"] = carried["members"], carried["humans"], carried["bots"]
        elif bucket["members"] is not None:
            carried = {"members": bucket["members"], "humans": bucket["humans"], "bots": bucket["bots"]}
    return out


async def member_count_at(db: Database, guild_id: int, slot: int) -> int | None:
    return await db.scalar("SELECT members FROM stat_members WHERE guild_id = ? AND slot <= ? AND members IS NOT NULL ORDER BY slot DESC LIMIT 1", (guild_id, slot))


async def joins_and_leaves(db: Database, guild_id: int, start: int | None, end: int | None) -> tuple[int, int]:
    clause = where("members", guild_id, Filters(), start, end, people=False)
    row = await db.one(f"SELECT COALESCE(SUM(joins), 0) AS joins, COALESCE(SUM(leaves), 0) AS leaves FROM stat_members WHERE {clause.text()}", clause.args)
    return int(row["joins"]), int(row["leaves"])


STATUS_NAMES = ("online", "idle", "dnd")


async def status_series(db: Database, guild_id: int, include: str, edges: Sequence[int], end: int) -> list[dict]:
    """Per bucket, the average number of people online, idle and on do not disturb, and not offline. A bucket read only from Discord's
    count of members present has "present" and no breakdown, and the count includes bots."""
    sums = [defaultdict(float) for _ in edges]
    if not edges:
        return []
    rows = await db.all("SELECT * FROM stat_status WHERE guild_id = ? AND slot >= ? AND slot < ?", (guild_id, edges[0], end))
    for row in rows:
        index = fold(edges, row["slot"])
        if index is None:
            continue
        bucket = sums[index]
        if row["samples"]:
            bucket["samples"] += 1
            for name in STATUS_NAMES:
                people = row[name] / row["samples"]
                bots = row[f"bot_{name}"] / row["samples"]
                bucket[name] += people if include == "humans" else bots if include == "bots" else people + bots
        if row["present_samples"]:
            bucket["present_samples"] += 1
            bucket["present_only"] += row["present"] / row["present_samples"]
    out = []
    for bucket in sums:
        entry: dict[str, float | None] = {name: None for name in STATUS_NAMES}
        entry["present"] = None
        if bucket["samples"]:
            for name in STATUS_NAMES:
                entry[name] = bucket[name] / bucket["samples"]
            entry["present"] = sum(entry[name] for name in STATUS_NAMES)
        elif bucket["present_samples"]:
            entry["present"] = bucket["present_only"] / bucket["present_samples"]
        out.append(entry)
    return out


SPLIT_DAYS = 28  # how far back from its latest real statuses a server's status split reads


async def status_shares(db: Database, guild_id: int, tz: Any) -> dict[tuple[int, int] | None, dict[str, float]]:
    """How the server's members who aren't offline split into online, idle and do not disturb, bots included, for each hour of the week
    as (weekday, hour) in the server's zone, and over all of them under None. It reads the rows that hold real statuses (samples above 0),
    in the 28 days up to the latest of them, so a server whose statuses stopped arriving keeps its last four weeks. Each row counts once,
    whatever its number of samples. One read on the table's (guild_id, slot) key. Empty when the server has no real statuses."""
    span = SPLIT_DAYS * 86400 // SLOT_SECONDS
    rows = await db.all(
        "SELECT slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd FROM stat_status WHERE guild_id = ? AND samples > 0 "
        "AND slot > (SELECT MAX(slot) FROM stat_status WHERE guild_id = ? AND samples > 0) - ?", (guild_id, guild_id, span))
    sums: dict[tuple[int, int] | None, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for row in rows:
        cells = (sums[hour_and_weekday(row["slot"], tz)], sums[None])
        for index, name in enumerate(STATUS_NAMES):
            value = (row[name] + row[f"bot_{name}"]) / row["samples"]
            for cell in cells:
                cell[index] += value
    shares: dict[tuple[int, int] | None, dict[str, float]] = {}
    for key, values in sums.items():
        total = sum(values)
        if total > 0:
            shares[key] = {name: value / total for name, value in zip(STATUS_NAMES, values)}
    return shares


# ---- heatmaps ----

async def heatmap(db: Database, kind: str, guild_id: int, f: Filters, start: int, end: int, tz: Any) -> list[list[float]]:
    """A 7 by 24 grid, Monday first, of the kind's average per hour at each hour of the week, in the server's time zone. Status reads
    the average number of people online."""
    cells = [[0.0] * 24 for _ in range(7)]
    hours_seen = [[0] * 24 for _ in range(7)]
    for slot in range(start, end, 4):  # every hour in the period, counted once, so an hour with nothing still lowers the average
        day, hour = hour_and_weekday(slot, tz)
        hours_seen[day][hour] += 1
    if kind == "status":
        rows = await db.all("SELECT slot, samples, online, idle, dnd FROM stat_status WHERE guild_id = ? AND slot >= ? AND slot < ? AND samples > 0", (guild_id, start, end))
        per_hour: dict[tuple[int, int], list[float]] = defaultdict(list)
        for row in rows:
            per_hour[hour_and_weekday(row["slot"], tz)].append((row["online"] + row["idle"] + row["dnd"]) / row["samples"])
        for (day, hour), readings in per_hour.items():
            cells[day][hour] = sum(readings) / len(readings)
        return cells
    for slot, value in await _grouped(db, kind, guild_id, f, start, end, ["slot"]):
        day, hour = hour_and_weekday(slot, tz)
        cells[day][hour] += value or 0
    for day in range(7):
        for hour in range(24):
            if hours_seen[day][hour]:
                cells[day][hour] /= hours_seen[day][hour]
    return cells


# ---- activities ----

async def top_activities(db: Database, guild_id: int, f: Filters, start: int | None, end: int | None, *, limit: int | None = None, offset: int = 0) -> Ranked:
    rows = await _grouped(db, "activities", guild_id, f, start, end, ["activity_id", "user_id"])
    totals: Counter = Counter()
    people: Counter = Counter()
    for activity, user_id, seconds in rows:
        totals[activity] += seconds or 0
        if user_id != 0:
            people[activity] += 1
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    page = ordered[offset:offset + limit] if limit is not None else ordered[offset:]
    return Ranked([(activity, int(seconds), people[activity]) for activity, seconds in page], len(ordered))


async def activity_series(db: Database, guild_id: int, f: Filters, activities: Sequence[int], edges: Sequence[int], end: int) -> dict[int, list[int]]:
    out = {activity: [0] * len(edges) for activity in activities}
    if not edges or not activities:
        return out
    marks, values = _marks(activities)
    for slot, activity, seconds in await _grouped(db, "activities", guild_id, f, edges[0], end, ["slot", "activity_id"], extra=f"activity_id IN ({marks})", extra_args=values):
        index = fold(edges, slot)
        if index is not None and activity in out:
            out[activity][index] += int(seconds or 0)
    return out


async def activity_names(db: Database, guild_id: int, ids: Iterable[int] | None = None) -> dict[int, dict]:
    if ids is None:
        rows = await db.all("SELECT * FROM stat_activity_names WHERE guild_id = ?", (guild_id,))
    else:
        ids = list(ids)
        if not ids:
            return {}
        marks, values = _marks(ids)
        rows = await db.all(f"SELECT * FROM stat_activity_names WHERE guild_id = ? AND activity_id IN ({marks})", (guild_id, *values))
    return {row["activity_id"]: row for row in rows}


# ---- emoji ----

async def top_emoji(db: Database, guild_id: int, f: Filters, start: int | None, end: int | None, *, limit: int | None = None, offset: int = 0) -> Ranked:
    """Emoji by uses, most first."""
    rows = await _grouped(db, "emoji", guild_id, f, start, end, ["emoji"])
    ordered = sorted(((emoji, int(count or 0)) for emoji, count in rows), key=lambda item: (-item[1], item[0]))
    page = ordered[offset:offset + limit] if limit is not None else ordered[offset:]
    return Ranked([(emoji, count, 0) for emoji, count in page], len(ordered))


async def emoji_series(db: Database, guild_id: int, f: Filters, emoji: Sequence[str], edges: Sequence[int], end: int) -> dict[str, list[int]]:
    out = {key: [0] * len(edges) for key in emoji}
    if not edges or not emoji:
        return out
    marks, values = _marks(emoji)
    for slot, key, count in await _grouped(db, "emoji", guild_id, f, edges[0], end, ["slot", "emoji"], extra=f"emoji IN ({marks})", extra_args=values):
        index = fold(edges, slot)
        if index is not None and key in out:
            out[key][index] += int(count or 0)
    return out


async def emoji_received(db: Database, guild_id: int, f: Filters, user_id: int, start: int | None, end: int | None) -> int:
    """Reactions others put on someone's messages."""
    clause = where("emoji", guild_id, replace(f, members_in=None, members_out=frozenset()), start, end)
    clause.add("target_id = ?", user_id)
    clause.add("use = ?", store.REACTION)
    return int(await db.scalar(f"SELECT COALESCE(SUM(count), 0) FROM stat_emoji WHERE {clause.text()}", clause.args))


async def emoji_info(db: Database, guild_id: int) -> dict[str, dict]:
    return {row["emoji"]: row for row in await db.all("SELECT * FROM stat_emoji_info WHERE guild_id = ?", (guild_id,))}


# ---- names ----

async def people(db: Database, guild_id: int, ids: Iterable[int]) -> dict[int, dict]:
    ids = [user_id for user_id in ids if user_id]
    out: dict[int, dict] = {}
    for chunk_start in range(0, len(ids), SQL_LIST_LIMIT):
        chunk = ids[chunk_start:chunk_start + SQL_LIST_LIMIT]
        marks, values = _marks(chunk)
        for row in await db.all(f"SELECT * FROM stat_people WHERE guild_id = ? AND user_id IN ({marks})", (guild_id, *values)):
            out[row["user_id"]] = row
    return out


async def channels(db: Database, guild_id: int, ids: Iterable[int] | None = None) -> dict[int, dict]:
    if ids is None:
        rows = await db.all("SELECT * FROM stat_channels WHERE guild_id = ?", (guild_id,))
        return {row["channel_id"]: row for row in rows}
    out: dict[int, dict] = {}
    ids = list(ids)
    for chunk_start in range(0, len(ids), SQL_LIST_LIMIT):
        chunk = ids[chunk_start:chunk_start + SQL_LIST_LIMIT]
        marks, values = _marks(chunk)
        for row in await db.all(f"SELECT * FROM stat_channels WHERE guild_id = ? AND channel_id IN ({marks})", (guild_id, *values)):
            out[row["channel_id"]] = row
    return out


def slot_edges(edges: Sequence[float]) -> list[int]:
    """Bucket starts in Unix seconds, as slots."""
    return [int(edge // SLOT_SECONDS) for edge in edges]


def edge_times(edges: Sequence[int]) -> list[int]:
    return [slot_time(slot) for slot in edges]
