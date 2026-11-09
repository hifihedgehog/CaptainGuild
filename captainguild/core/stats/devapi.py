"""The statistics API: a server's counts for its own scripts, read with the server's key.

Paths, query parameters and answers follow the statistics API servers moving here already use, so a script written for it works once its
base address changes. Every timestamp is in milliseconds, IDs are text, voice and activity time is in whole minutes, and `end` counts the
millisecond it names. A series lists only the buckets that hold something, oldest first unless `order=desc` says otherwise. A top list is
ranked from 1, ties going to the lower ID, and leaves out everyone who chose privacy.

Invites and activities have numbers of their own here: each one's is worked out from its invite code or the activity itself, so it stays
the same across restarts, restores and recounts, and fits in a JavaScript number.

Nothing here touches Discord. The live server is read for current roles, for names and pictures, and for whether a join still counts.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Sequence

from ...db import Database
from . import invites as invite_rules
from . import query, store
from .timeframe import INTERVALS, SLOT_SECONDS, slot_of

MAX_POINTS = 250_000  # the most data points one answer carries without a limit
STREAM_ROWS = 5000  # rows read from the database at a time for a series
LAST_MS = 253_402_300_799_999  # the last millisecond of the year 9999
TOO_MANY = f"This answer would hold more than {MAX_POINTS:,} points. Ask for a shorter period, a longer interval, fewer groups, or a limit."
MAX_PAGE_SIZE = 10_000
JS_SAFE = 2 ** 53 - 1
DIGITS = re.compile(r"[0-9]{1,20}")
STATE_NAMES = {"normal": store.TALKING, "afk": store.AFK, "self_mute": store.SELF_MUTED, "self_deaf": store.SELF_DEAFENED,
               "server_mute": store.SERVER_MUTED, "server_deaf": store.SERVER_DEAFENED}
CHANNEL_TYPES = (0, 2, 4, 5, 13, 15, 999)
AGGREGATE_TYPE = 999


class Problem(Exception):
    """A request that can't be answered as asked. The text goes back as the answer's message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Source:
    """What a request reads: the counts, the server they belong to, and its settings."""
    db: Database
    guild_id: int
    guild: Any | None  # the live server, or None when the bot isn't in it now
    settings: dict
    now: float
    hidden: frozenset[int] = frozenset()  # channels the key's maker can't see, which the key never reads


# ---- numbers for invites and activities ----

def invite_number(code: str) -> int:
    """An invite's number in this API, the same every time for the same code."""
    return int.from_bytes(hashlib.blake2b(f"invite:{code}".encode(), digest_size=8).digest(), "big") % JS_SAFE + 1


def activity_number(activity_id: int) -> int:
    """An activity's number in this API: the stored one, cut to fit in a JavaScript number."""
    return activity_id % JS_SAFE + 1


# ---- reading the query ----

def _values(query_args: Any, name: str) -> list[str]:
    """A list parameter, sent as name[]=1&name[]=2 or name=1&name=2."""
    return [*query_args.getall(f"{name}[]", []), *query_args.getall(name, [])]


def ids(query_args: Any, name: str) -> list[int] | None:
    found = _values(query_args, name)
    if not found:
        return None
    out = []
    for value in found:
        for part in value.split(","):
            part = part.strip()
            if not DIGITS.fullmatch(part) or int(part) >= 2 ** 63:
                raise Problem(400, f"{name}[] takes Discord IDs.")
            out.append(int(part))
    return out


def numbers(query_args: Any, name: str) -> list[int] | None:
    found = _values(query_args, name)
    if not found:
        return None
    out = []
    for value in found:
        for part in value.split(","):
            part = part.strip()
            if not DIGITS.fullmatch(part) or not 1 <= int(part) <= JS_SAFE:
                raise Problem(400, f"{name}[] takes whole numbers from 1.")
            out.append(int(part))
    return out


def flag(query_args: Any, name: str) -> bool | None:
    value = query_args.get(name)
    if value is None or value == "":
        return None
    text = value.strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise Problem(400, f"{name} is true or false.")


def whole(query_args: Any, name: str, minimum: int = 0) -> int | None:
    value = query_args.get(name)
    if value is None or value == "":
        return None
    text = value.strip()
    if not re.fullmatch(r"-?[0-9]{1,19}", text) or int(text) < minimum:
        raise Problem(400, f"{name} is a whole number from {minimum}.")
    return int(text)


def choice(query_args: Any, name: str, options: Sequence[str], default: str | None) -> str | None:
    value = query_args.get(name)
    if value is None or value == "":
        return default
    if value not in options:
        raise Problem(400, f"{name} is one of {', '.join(options)}.")
    return value


def _exclusive(query_args: Any, first: str, second: str) -> None:
    if _values(query_args, first) and _values(query_args, second):
        raise Problem(400, f"{first}[] can't be used with {second}[].")


# ---- time ----

@dataclass(frozen=True)
class Window:
    """The period asked for, as quarter-hour slots [first_slot, end_slot), and how it's cut into buckets."""
    start: float
    until: float  # the first moment past the period: `end` names the last millisecond counted
    first_slot: int
    end_slot: int
    offset: int  # seconds east of UTC
    interval: str


def window(query_args: Any, now: float) -> Window:
    start_ms = whole(query_args, "start") or 0
    end_ms = whole(query_args, "end")
    end_ms = int(now * 1000) if end_ms is None else end_ms
    if max(start_ms, end_ms) > LAST_MS:
        raise Problem(400, "start and end are milliseconds since 1970, before the year 10000.")
    if end_ms < start_ms:
        raise Problem(400, "end comes before start.")
    offset_text = query_args.get("timezone_offset")
    hours = 0.0
    if offset_text not in (None, ""):
        try:
            hours = float(offset_text)
        except ValueError as error:
            raise Problem(400, "timezone_offset is a number of hours from -12 to 14, in quarter hours.") from error
        if not math.isfinite(hours) or not -12 <= hours <= 14 or abs(hours * 4 - round(hours * 4)) > 1e-9:
            raise Problem(400, "timezone_offset is a number of hours from -12 to 14, in quarter hours.")
    interval = choice(query_args, "interval", INTERVALS, "day")
    start, end = start_ms / 1000, end_ms / 1000
    return Window(start, (end_ms + 1) / 1000, slot_of(start), slot_of(end) + 1, int(round(hours * 4)) * 900, interval)


@lru_cache(maxsize=4096)
def _month_start(local_day: int, offset: int) -> int:
    day = dt.date(1970, 1, 1) + dt.timedelta(days=local_day)
    first = day.replace(day=1)
    return (first - dt.date(1970, 1, 1)).days * 86400 - offset


def bucket_start(seconds: float, interval: str, offset: int) -> int:
    """When the bucket holding this moment begins, in Unix seconds, for a fixed offset from UTC. Weeks begin on Monday."""
    local = int(seconds) + offset
    if interval == "hour":
        return local // 3600 * 3600 - offset
    day = local // 86400
    if interval == "day":
        return day * 86400 - offset
    if interval == "week":
        return (day - (day + 3) % 7) * 86400 - offset  # January 1, 1970 was a Thursday
    return _month_start(day, offset)


def _bucketer(win: Window) -> Callable[[int], int]:
    cache: dict[int, int] = {}

    def of(slot: int) -> int:
        found = cache.get(slot)
        if found is None:
            found = cache[slot] = bucket_start(slot * SLOT_SECONDS, win.interval, win.offset)
        return found
    return of


# ---- filters ----

def _role_members(source: Source, role_ids: Iterable[int]) -> set[int]:
    guild = source.guild
    found: set[int] = set()
    if guild is None:
        return found
    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role is not None:
            found.update(member.id for member in role.members)
    return found


def people_filters(source: Source, query_args: Any) -> tuple[frozenset[int] | None, frozenset[int]]:
    """(who's in, or None for everyone, and who's out) from the member and role lists. Roles are the ones people hold now, so someone who has
    left matches no allowed role and no blocked one."""
    _exclusive(query_args, "whitelist_members", "blacklist_members")
    allowed, blocked = ids(query_args, "whitelist_members"), ids(query_args, "blacklist_members")
    allowed_roles, blocked_roles = ids(query_args, "whitelist_roles"), ids(query_args, "blacklist_roles")
    members_in: set[int] | None = set(allowed) if allowed is not None else None
    if allowed_roles is not None:
        holders = _role_members(source, allowed_roles)
        members_in = holders if members_in is None else members_in & holders
    members_out = set(blocked or [])
    if blocked_roles is not None:
        members_out |= _role_members(source, blocked_roles)
    return (frozenset(members_in) if members_in is not None else None), frozenset(members_out)


def filters(source: Source, query_args: Any, *, voice_states: bool = False) -> query.Filters:
    """The view the query asks for. The server's own view filters don't apply here: a script reads the counts as recorded."""
    _exclusive(query_args, "whitelist_channels", "blacklist_channels")
    _exclusive(query_args, "whitelist_voice_channels", "blacklist_voice_channels")
    bot = flag(query_args, "bot")
    members_in, members_out = people_filters(source, query_args)
    channel_allow, channel_block = ids(query_args, "whitelist_channels"), ids(query_args, "blacklist_channels")
    voice_allow, voice_block = ids(query_args, "whitelist_voice_channels"), ids(query_args, "blacklist_voice_channels")
    f = query.Filters(
        include="both" if bot is None else ("bots" if bot else "humans"),
        channel_mode="allow" if channel_allow is not None else "block",
        channels=frozenset(channel_allow if channel_allow is not None else channel_block or []),
        voice_mode="same" if voice_allow is None and voice_block is None else ("allow" if voice_allow is not None else "block"),
        voice_channels=frozenset(voice_allow if voice_allow is not None else voice_block or []),
        members_in=members_in,
        members_out=members_out,
        hidden_channels=source.hidden,
    )
    if voice_states:
        states = _values(query_args, "voice_states")
        if states:
            picked = set()
            for name in states:
                for part in name.split(","):
                    if part not in STATE_NAMES:
                        raise Problem(400, f"voice_states[] takes {', '.join(STATE_NAMES)}.")
                    picked.add(STATE_NAMES[part])
            f = replace(f, voice_states=frozenset(picked))
    return f


# ---- shaping answers ----

def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


def _minutes(seconds: float) -> int:
    return int((seconds + 30) // 60)


def _text_id(value: int | None) -> str | None:
    return str(value) if value else None


def _limited(rows: list[dict], query_args: Any) -> list[dict]:
    order = choice(query_args, "order", ("asc", "desc"), "asc")
    if order == "desc":
        rows.reverse()
    limit = whole(query_args, "limit", 1)
    if limit is None and len(rows) > MAX_POINTS:
        raise Problem(400, TOO_MANY)
    return rows[:limit] if limit is not None else rows


def _page(ranked: list[dict], query_args: Any, key: Callable[[dict], int]) -> list[dict]:
    """A top list's page: `limit`, `page_size` with `page`, or `select[]` for the rows of certain IDs. Ranks count from the whole list."""
    limit = whole(query_args, "limit", 1)
    page_size, page = whole(query_args, "page_size", 1), whole(query_args, "page", 1)
    picked = _values(query_args, "select")
    if limit is not None and (page_size is not None or page is not None or picked):
        raise Problem(400, "limit can't be used with page_size, page or select[].")
    if (page_size is None) != (page is None):
        raise Problem(400, "page_size and page go together.")
    if picked and page_size is not None:
        raise Problem(400, "select[] can't be used with page_size or page.")
    if choice(query_args, "order", ("asc", "desc"), "asc") == "desc":
        ranked = list(reversed(ranked))
    if picked:
        wanted = set(ids(query_args, "select") or [])
        return [row for row in ranked if key(row) in wanted]
    if page_size is not None:
        page_size = min(page_size, MAX_PAGE_SIZE)
        return ranked[(page - 1) * page_size:page * page_size]
    return ranked[:limit] if limit is not None else ranked


async def profiles(source: Source, user_ids: Iterable[int]) -> dict[int, dict]:
    """Each person's name fields, from what statistics keep and from the server as it is now. Empty text where nothing is known."""
    user_ids = [user_id for user_id in user_ids if user_id]
    kept = await query.people(source.db, source.guild_id, user_ids)
    out: dict[int, dict] = {}
    for user_id in user_ids:
        row = kept.get(user_id) or {}
        member = source.guild.get_member(user_id) if source.guild is not None else None
        guild_avatar = getattr(getattr(member, "guild_avatar", None), "key", None) if member is not None else None
        avatar = getattr(getattr(member, "avatar", None), "key", None) if member is not None else None
        bot = bool(row.get("bot")) or bool(getattr(member, "bot", False))
        out[user_id] = {
            "username": (getattr(member, "name", None) if member is not None else None) or row.get("name") or "",
            "type": "bot" if bot else "user",
            "globalName": (getattr(member, "global_name", None) if member is not None else None) or row.get("display_name") or "",
            "nick": (getattr(member, "nick", None) if member is not None else None) or row.get("nick") or "",
            "avatar": avatar or row.get("avatar") or "",
            "guildAvatar": guild_avatar or "",
        }
    return out


# ---- channels ----

async def channel_objects(source: Source, ids_wanted: Iterable[int] | None = None) -> dict[int, dict]:
    """Every channel statistics know, deleted ones and merged channels included, in this API's shape."""
    rows = await query.channels(source.db, source.guild_id, None if ids_wanted is None else list(ids_wanted))
    aggregates = {row["id"]: row for row in source.settings.get("aggregates") or []}
    live = {channel.id for channel in getattr(source.guild, "channels", [])} if source.guild is not None else None
    out: dict[int, dict] = {}
    for channel_id, row in rows.items():
        if channel_id in source.hidden:
            continue
        base = {"id": str(channel_id), "name": row["name"], "type": row["type"], "parentId": _text_id(row["parent_id"])}
        if row["type"] == AGGREGATE_TYPE:
            settings = aggregates.get(channel_id) or {}
            kinds = settings.get("kinds", "both")
            out[channel_id] = {**base, "aggregate": True, "autoMergeEnabled": bool(settings.get("auto_categories")),
                               "autoMergeChannelIds": [str(number) for number in settings.get("auto_categories") or []],
                               "autoMergeTypes": {"messages": ["text"], "voice": ["voice"]}.get(kinds, ["text", "voice"]),
                               "createdUnixTimestamp": _ms(row["created_at"] or 0)}
        else:
            active = row["deleted_at"] is None and (live is None or channel_id in live)
            out[channel_id] = {**base, "isActive": 1 if active else 0}
    return out


async def channels(source: Source, query_args: Any) -> list[dict]:
    types = numbers_or_zero(query_args, "types")
    wanted = ids(query_args, "ids")
    found = await channel_objects(source, wanted)
    rows = [row for row in found.values() if types is None or row["type"] in types]
    return sorted(rows, key=lambda row: int(row["id"]))


def numbers_or_zero(query_args: Any, name: str) -> set[int] | None:
    found = _values(query_args, name)
    if not found:
        return None
    out = set()
    for value in found:
        for part in value.split(","):
            if not re.fullmatch(r"[0-9]{1,4}", part.strip()) or int(part) not in CHANNEL_TYPES:
                raise Problem(400, f"{name}[] takes channel types {', '.join(str(kind) for kind in CHANNEL_TYPES)}.")
            out.add(int(part))
    return out


async def channel(source: Source, channel_id: int) -> dict:
    found = await channel_objects(source, [channel_id])
    if channel_id not in found:
        raise Problem(404, "No such channel.")
    return found[channel_id]


# ---- messages and voice ----

async def _stream(source: Source, kind: str, f: query.Filters, win: Window, columns: list[str]) -> AsyncIterator[list[tuple]]:
    """The kind's sums grouped by `columns`, slot first, a few thousand rows at a time with the people filters applied. Each chunk comes
    off the database's own thread, so a long period never holds up the bot's other work for long. Use it inside contextlib.aclosing."""
    table, value = query._TABLE[kind]
    python_people = f.in_python()
    group = list(columns) + (["user_id"] if python_people and "user_id" not in columns else [])
    clause = query.where(kind, source.guild_id, f, win.first_slot, win.end_slot)
    sql = (f"SELECT {', '.join(group)}, SUM({value}) FROM {table} WHERE {clause.text()} GROUP BY "
           + ", ".join(column if column == "slot" else f"+{column}" for column in group))
    user_at = group.index("user_id") if python_people else None
    keep_user = "user_id" in columns
    cursor = await source.db.conn.execute(sql, clause.args)
    try:
        while True:
            rows = await cursor.fetchmany(STREAM_ROWS)
            if not rows:
                return
            chunk = []
            for row in rows:
                row = tuple(row)
                if user_at is not None:
                    if not f.keeps(row[user_at]):
                        continue
                    if not keep_user:
                        row = row[:user_at] + row[user_at + 1:]
                chunk.append(row)
            yield chunk
    finally:
        await cursor.close()


async def _bucketed(source: Source, kind: str, f: query.Filters, win: Window, columns: list[str], query_args: Any) -> Counter:
    """{(bucket start, the columns' values...): sum}. Without a limit, an answer past MAX_POINTS is refused as soon as it gets there."""
    of = _bucketer(win)
    limited = whole(query_args, "limit", 1) is not None
    totals: Counter = Counter()
    async with contextlib.aclosing(_stream(source, kind, f, win, ["slot", *columns])) as chunks:
        async for chunk in chunks:
            for row in chunk:
                totals[(of(row[0]), *row[1:-1])] += row[-1] or 0
            if not limited and len(totals) > MAX_POINTS:
                raise Problem(400, TOO_MANY)
    return totals


async def series(source: Source, query_args: Any, kind: str) -> list[dict]:
    """Messages or voice minutes per bucket, grouped as asked."""
    win = window(query_args, source.now)
    f = filters(source, query_args, voice_states=kind == "voice")
    groups: list[tuple[str, str, Callable[[Any], Any]]] = []
    if flag(query_args, "by_member"):
        groups.append(("user_id", "memberId", _text_id))
    if flag(query_args, "by_channel"):
        groups.append(("channel_id", "channelId", str))
    if kind == "voice" and flag(query_args, "by_state"):
        groups.append(("state", "type", int))
    if flag(query_args, "by_flag"):
        groups.append(("flags", "flags", int))
    totals = await _bucketed(source, kind, f, win, [column for column, _, _ in groups], query_args)
    out = []
    for key in sorted(totals, key=lambda item: tuple(-1 if value is None else value for value in item)):
        value = totals[key]
        count = _minutes(value) if kind == "voice" else int(value)
        if not count:
            continue
        point: dict[str, Any] = {"unixTimestamp": _ms(key[0]), "count": count}
        for (column, name, shape), part in zip(groups, key[1:]):
            point[name] = shape(part)
        out.append(point)
    return _limited(out, query_args)


async def sums(source: Source, query_args: Any, kind: str) -> dict:
    win = window(query_args, source.now)
    f = filters(source, query_args, voice_states=kind == "voice")
    value = await query.total(source.db, kind, source.guild_id, f, win.first_slot, win.end_slot)
    return {"count": _minutes(value) if kind == "voice" else int(value)}


async def _per(source: Source, kind: str, f: query.Filters, win: Window, by: str) -> dict[int, tuple[int, int]]:
    """{person or channel: (total, how many of the other thing)}. Someone who chose privacy is no one: they aren't ranked, and they aren't a
    member of a channel's count."""
    other = "channel_id" if by == "user_id" else "user_id"
    table, value = query._TABLE[kind]
    if f.in_python():
        found: dict[int, list] = {}
        for first, second, total in await query._grouped(source.db, kind, source.guild_id, f, win.first_slot, win.end_slot, [by, other]):
            entry = found.setdefault(first, [0, 0])
            entry[0] += total or 0
            if other == "channel_id" or second:
                entry[1] += 1
        return {key: (int(total), count) for key, (total, count) in found.items()}
    clause = query.where(kind, source.guild_id, f, win.first_slot, win.end_slot)
    distinct = "channel_id" if other == "channel_id" else "NULLIF(user_id, 0)"
    rows = await source.db.all(f"SELECT {by} AS id, SUM({value}) AS total, COUNT(DISTINCT {distinct}) AS others FROM {table} WHERE {clause.text()} GROUP BY +{by}",
                               clause.args)
    return {row["id"]: (int(row["total"] or 0), int(row["others"] or 0)) for row in rows}


def _ranked(per: dict[int, tuple[int, int]], *, skip_nobody: bool) -> list[tuple[int, int, int, int]]:
    """[(rank, id, total, others)], biggest first, ties to the lower ID."""
    items = [(key, total, others) for key, (total, others) in per.items() if total and not (skip_nobody and key == 0)]
    items.sort(key=lambda item: (-item[1], item[0]))
    return [(rank, key, total, others) for rank, (key, total, others) in enumerate(items, 1)]


async def top_members(source: Source, query_args: Any, kind: str) -> list[dict]:
    win = window(query_args, source.now)
    f = filters(source, query_args, voice_states=kind == "voice")
    ranked = [{"id": str(key), "rank": rank, "count": _minutes(total) if kind == "voice" else total, "channelCount": others}
              for rank, key, total, others in _ranked(await _per(source, kind, f, win, "user_id"), skip_nobody=True)]
    if kind == "voice":
        ranked = [row for row in ranked if row["count"]]
    page = _page(ranked, query_args, key=lambda row: int(row["id"]))
    if flag(query_args, "full"):
        names = await profiles(source, [int(row["id"]) for row in page])
        page = [{**row, **names.get(int(row["id"]), {})} for row in page]
    return page


async def top_channels(source: Source, query_args: Any, kind: str) -> list[dict]:
    win = window(query_args, source.now)
    f = filters(source, query_args, voice_states=kind == "voice")
    ranked = [{"id": str(key), "rank": rank, "count": _minutes(total) if kind == "voice" else total, "memberCount": others}
              for rank, key, total, others in _ranked(await _per(source, kind, f, win, "channel_id"), skip_nobody=False)]
    if kind == "voice":
        ranked = [row for row in ranked if row["count"]]
    page = _page(ranked, query_args, key=lambda row: int(row["id"]))
    if flag(query_args, "full"):
        found = await channel_objects(source, [int(row["id"]) for row in page])
        page = [{**(found.get(int(row["id"])) or {"id": row["id"], "name": "", "type": 0, "parentId": None, "isActive": 0}), **row} for row in page]
    return page


async def member_counts(source: Source, query_args: Any) -> list[dict]:
    """Everyone with messages or voice time in the period, with how many channels each used, and their names."""
    win = window(query_args, source.now)
    f = filters(source, query_args, voice_states=True)
    messages = await _per(source, "messages", f, win, "user_id")
    voice = await _per(source, "voice", f, win, "user_id")
    people = sorted(key for key in {*messages, *voice} if key)
    names = await profiles(source, people)
    out = []
    for user_id in people:
        sent, used = messages.get(user_id, (0, 0))
        seconds, rooms = voice.get(user_id, (0, 0))
        out.append({"id": str(user_id), "messageCount": sent, "voiceCount": _minutes(seconds), "messageChannelCount": used, "voiceChannelCount": rooms,
                    **names[user_id]})
    return out


async def unique_series(source: Source, query_args: Any, what: str) -> list[dict]:
    """How many different people ("user_id") or channels ("channel_id") were active in each bucket, in the stats asked for."""
    win = window(query_args, source.now)
    f = filters(source, query_args, voice_states=True)
    stats = []
    for value in _values(query_args, "stats"):
        for part in value.split(","):
            if part not in ("text", "voice"):
                raise Problem(400, "stats[] takes text and voice.")
            stats.append(part)
    kinds = [{"text": "messages", "voice": "voice"}[name] for name in dict.fromkeys(stats or ["text", "voice"])]
    of = _bucketer(win)
    seen: dict[int, set] = defaultdict(set)
    for kind in kinds:
        async with contextlib.aclosing(_stream(source, kind, f, win, ["slot", what])) as chunks:
            async for chunk in chunks:
                for slot, found, _ in chunk:
                    if what == "user_id" and not found:
                        continue
                    seen[of(slot)].add(found)
    out = [{"unixTimestamp": _ms(start), "count": len(found)} for start, found in sorted(seen.items()) if found]
    return _limited(out, query_args)


# ---- members and statuses ----

async def member_count_series(source: Source, query_args: Any) -> list[dict]:
    """Per bucket: the member count at its last reading, and its joins and leaves."""
    win = window(query_args, source.now)
    of = _bucketer(win)
    before = await source.db.scalar("SELECT members FROM stat_members WHERE guild_id = ? AND slot < ? AND members IS NOT NULL ORDER BY slot DESC LIMIT 1",
                                    (source.guild_id, win.first_slot))
    rows = await source.db.all("SELECT slot, members, joins, leaves FROM stat_members WHERE guild_id = ? AND slot >= ? AND slot < ? ORDER BY slot",
                               (source.guild_id, win.first_slot, win.end_slot))
    buckets: dict[int, dict] = {}
    carried = before
    for row in rows:
        bucket = buckets.setdefault(of(row["slot"]), {"count": None, "joins": 0, "leaves": 0})
        if row["members"] is not None:
            bucket["count"] = carried = row["members"]
        elif bucket["count"] is None:
            bucket["count"] = carried
        bucket["joins"] += row["joins"]
        bucket["leaves"] += row["leaves"]
    out = [{"unixTimestamp": _ms(start), "count": bucket["count"] or 0, "joins": bucket["joins"], "leaves": bucket["leaves"]} for start, bucket in sorted(buckets.items())]
    return _limited(out, query_args)


async def status_series(source: Source, query_args: Any) -> list[dict]:
    """Per bucket, the average number of members online, on do not disturb and idle, bots included. Where only Discord's count of members
    present was read, that count shows as online."""
    win = window(query_args, source.now)
    of = _bucketer(win)
    rows = await source.db.all("SELECT * FROM stat_status WHERE guild_id = ? AND slot >= ? AND slot < ? ORDER BY slot", (source.guild_id, win.first_slot, win.end_slot))
    buckets: dict[int, list[list[float]]] = defaultdict(lambda: [[], [], [], []])
    for row in rows:
        bucket = buckets[of(row["slot"])]
        if row["samples"]:
            samples = row["samples"]
            bucket[0].append((row["online"] + row["bot_online"]) / samples)
            bucket[1].append((row["dnd"] + row["bot_dnd"]) / samples)
            bucket[2].append((row["idle"] + row["bot_idle"]) / samples)
        elif row["present_samples"]:
            bucket[3].append(row["present"] / row["present_samples"])
    out = []
    for start, (online, dnd, idle, present) in sorted(buckets.items()):
        if online:
            out.append({"unixTimestamp": _ms(start), "online": round(sum(online) / len(online)), "dnd": round(sum(dnd) / len(dnd)), "idle": round(sum(idle) / len(idle))})
        elif present:
            out.append({"unixTimestamp": _ms(start), "online": round(sum(present) / len(present)), "dnd": 0, "idle": 0})
    return _limited(out, query_args)


# ---- activities ----

async def _activity_numbers(source: Source) -> tuple[dict[int, dict], dict[int, int]]:
    """({stored activity ID: its row}, {this API's number: stored activity ID})."""
    names = await query.activity_names(source.db, source.guild_id)
    return names, {activity_number(activity_id): activity_id for activity_id in names}


def _activity_filter(f: query.Filters, query_args: Any, back: dict[int, int]) -> query.Filters:
    _exclusive(query_args, "whitelist_activities", "blacklist_activities")
    allowed, blocked = numbers(query_args, "whitelist_activities"), numbers(query_args, "blacklist_activities")
    if allowed is not None:
        stored = {back[number] for number in allowed if number in back}
        return replace(f, activity_mode="allow", activities=frozenset(stored or {-1}))
    if blocked is not None:
        return replace(f, activity_mode="block", activities=frozenset(back[number] for number in blocked if number in back))
    return f


async def activities(source: Source) -> list[dict]:
    names, _ = await _activity_numbers(source)
    rows = [{"id": activity_number(activity_id), "name": row["name"], "applicationId": _text_id(row["application_id"])} for activity_id, row in names.items()]
    return sorted(rows, key=lambda row: (row["name"].casefold(), row["id"]))


async def activity_series(source: Source, query_args: Any) -> list[dict]:
    win = window(query_args, source.now)
    names, back = await _activity_numbers(source)
    f = _activity_filter(filters(source, query_args), query_args, back)
    by_activity, by_member = flag(query_args, "by_activity"), flag(query_args, "by_member")
    totals = await _bucketed(source, "activities", f, win, [*(["activity_id"] if by_activity else []), *(["user_id"] if by_member else [])], query_args)
    out = []
    for key in sorted(totals, key=lambda item: tuple(-1 if value is None else value for value in item)):
        count = _minutes(totals[key])
        if not count:
            continue
        point: dict[str, Any] = {"unixTimestamp": _ms(key[0]), "count": count}
        rest = list(key[1:])
        if by_activity:
            activity_id = rest.pop(0)
            point["activityId"] = activity_number(activity_id)
            point["activityType"] = (names.get(activity_id) or {}).get("kind", 0)
        if by_member:
            point["memberId"] = _text_id(rest.pop(0))
        out.append(point)
    return _limited(out, query_args)


async def top_activities(source: Source, query_args: Any) -> list[dict]:
    win = window(query_args, source.now)
    names, back = await _activity_numbers(source)
    f = _activity_filter(filters(source, query_args), query_args, back)
    ranked_rows = await query.top_activities(source.db, source.guild_id, f, win.first_slot, win.end_slot)
    ranked = []
    for activity_id, seconds, _people in ranked_rows.rows:
        minutes = _minutes(seconds)
        if not minutes:
            continue
        row = names.get(activity_id) or {}
        ranked.append({"applicationId": _text_id(row.get("application_id")), "count": minutes, "id": activity_number(activity_id),
                       "name": row.get("name") or "", "rank": len(ranked) + 1})
    picked = _values(query_args, "select")
    if picked:
        wanted = set(numbers(query_args, "select") or [])
        limit = whole(query_args, "limit", 1)
        if limit is not None or whole(query_args, "page_size", 1) is not None:
            raise Problem(400, "select[] can't be used with limit, page_size or page.")
        if choice(query_args, "order", ("asc", "desc"), "asc") == "desc":
            ranked.reverse()
        return [row for row in ranked if row["id"] in wanted]
    return _page(ranked, query_args, key=lambda row: row["id"])


# ---- invites ----

async def _invite_rows(source: Source) -> dict[str, dict]:
    return {row["code"]: row for row in await source.db.all("SELECT * FROM stat_invites WHERE guild_id = ?", (source.guild_id,))}


def _invite_object(row: dict, labels: dict) -> dict:
    code = row["code"]
    vanity = bool(row["vanity"])
    shown = code[len(invite_rules.VANITY_PREFIX):] if code.startswith(invite_rules.VANITY_PREFIX) else code
    return {"id": invite_number(code), "memberId": _text_id(row["inviter_id"]), "channelId": _text_id(row["channel_id"]), "code": shown,
            "uses": row["uses"] or 0, "maxUses": row["max_uses"] or 0, "maxAge": row["max_age"] or 0, "isTemporary": int(bool(row["temporary"])),
            "isVanity": int(vanity), "isActive": int(row["deleted_at"] is None), "label": labels.get(shown) or labels.get(code) or ""}


async def invites(source: Source) -> list[dict]:
    labels = source.settings.get("invite_labels") or {}
    rows = [_invite_object(row, labels) for row in (await _invite_rows(source)).values()]
    return sorted(rows, key=lambda row: row["id"])


async def invite(source: Source, number: int) -> dict:
    labels = source.settings.get("invite_labels") or {}
    for row in (await _invite_rows(source)).values():
        if invite_number(row["code"]) == number:
            return _invite_object(row, labels)
    raise Problem(404, "No such invite.")


@dataclass
class Join:
    at: float
    invitee: int
    inviter: int | None
    code: str | None
    flags: int


async def _joins(source: Source, query_args: Any, win: Window, *, member_side: str) -> list[Join]:
    """The joins in the period that the query keeps. `member_side` says whom the member and role lists apply to: the invitee or the inviter."""
    rows = await source.db.all("SELECT * FROM stat_joins WHERE guild_id = ? AND at >= ? AND at < ? ORDER BY at",
                               (source.guild_id, win.first_slot * SLOT_SECONDS, win.end_slot * SLOT_SECONDS))
    bot = flag(query_args, "bot")
    _exclusive(query_args, "whitelist_invites", "blacklist_invites")
    allowed_invites, blocked_invites = numbers(query_args, "whitelist_invites"), numbers(query_args, "blacklist_invites")
    masks = []
    for side in ("whitelist", "blacklist"):
        mask = whole(query_args, f"{side}_invite_flags_mask")
        kind = choice(query_args, f"{side}_invite_flags_type", ("any", "all"), None)
        if mask is not None and kind is None:
            raise Problem(400, f"{side}_invite_flags_mask needs {side}_invite_flags_type.")
        if mask:
            masks.append((side, mask, kind))
    members_in, members_out = people_filters(source, query_args)
    guild = source.guild
    out = []
    for row in rows:
        at = row["at"]
        if not win.start <= at < win.until:
            continue
        member = guild.get_member(row["user_id"]) if guild is not None and row["user_id"] else None
        bits = invite_rules.reasons(row, source.settings, source.now, member)
        if bot is not None and bool(bits & invite_rules.FAKE_REASONS["bot"]) != bot:
            continue
        code = row["code"]
        number = invite_number(code) if code else None
        if allowed_invites is not None and number not in allowed_invites:
            continue
        if blocked_invites is not None and number in blocked_invites:
            continue
        kept = True
        for side, mask, kind in masks:
            hit = bits & mask == mask if kind == "all" else bool(bits & mask)
            if (side == "whitelist" and not hit) or (side == "blacklist" and hit):
                kept = False
        if not kept:
            continue
        who = row["user_id"] if member_side == "invitee" else row["inviter_id"]
        if members_in is not None and (who is None or who not in members_in):
            continue
        if who is not None and who in members_out:
            continue
        out.append(Join(at, row["user_id"], row["inviter_id"], code, bits))
    return out


async def invite_series(source: Source, query_args: Any) -> list[dict]:
    win = window(query_args, source.now)
    joins = await _joins(source, query_args, win, member_side="invitee")
    by_invite, by_inviter, by_invitee, by_flag = (flag(query_args, name) for name in ("by_invite", "by_inviter", "by_invitee", "by_flag"))
    totals: Counter = Counter()
    for join in joins:
        key = [bucket_start(join.at, win.interval, win.offset)]
        if by_invite:
            key.append(invite_number(join.code) if join.code else None)
        if by_inviter:
            key.append(join.inviter or None)
        if by_invitee:
            key.append(join.invitee or None)
        if by_flag:
            key.append(join.flags)
        totals[tuple(key)] += 1
    out = []
    for key in sorted(totals, key=lambda item: tuple(-1 if value is None else value for value in item)):
        point: dict[str, Any] = {"unixTimestamp": _ms(key[0]), "count": totals[key]}
        rest = list(key[1:])
        if by_invite:
            point["inviteId"] = rest.pop(0)
        if by_inviter:
            point["inviterId"] = _text_id(rest.pop(0))
        if by_invitee:
            point["inviteeId"] = _text_id(rest.pop(0))
        if by_flag:
            point["flags"] = rest.pop(0)
        out.append(point)
    return _limited(out, query_args)


async def top_invites(source: Source, query_args: Any) -> list[dict]:
    win = window(query_args, source.now)
    joins = await _joins(source, query_args, win, member_side="invitee")
    counts: Counter = Counter()
    people: dict[str, set] = defaultdict(set)
    for join in joins:
        if not join.code:
            continue
        counts[join.code] += 1
        if join.invitee:
            people[join.code].add(join.invitee)
    order = sorted(counts.items(), key=lambda item: (-item[1], invite_number(item[0])))
    ranked = [{"id": invite_number(code), "rank": rank, "count": count, "memberCount": len(people[code])} for rank, (code, count) in enumerate(order, 1)]
    picked = _values(query_args, "select")
    if picked:
        if whole(query_args, "limit", 1) is not None or whole(query_args, "page_size", 1) is not None:
            raise Problem(400, "select[] can't be used with limit, page_size or page.")
        wanted = set(numbers(query_args, "select") or [])
        if choice(query_args, "order", ("asc", "desc"), "asc") == "desc":
            ranked.reverse()
        page = [row for row in ranked if row["id"] in wanted]
    else:
        page = _page(ranked, query_args, key=lambda row: row["id"])
    if flag(query_args, "full"):
        stored = {invite_number(code): row for code, row in (await _invite_rows(source)).items()}
        labels = source.settings.get("invite_labels") or {}
        filled = []
        for row in page:
            found = stored.get(row["id"])
            shaped = _invite_object(found, labels) if found else {"code": "", "label": "", "memberId": None}
            filled.append({**row, "code": shaped["code"], "label": shaped["label"], "memberId": shaped["memberId"]})
        page = filled
    return page


async def top_inviters(source: Source, query_args: Any) -> list[dict]:
    win = window(query_args, source.now)
    joins = await _joins(source, query_args, win, member_side="inviter")
    counts: Counter = Counter(join.inviter for join in joins if join.inviter)
    order = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ranked = [{"id": str(inviter), "rank": rank, "count": count} for rank, (inviter, count) in enumerate(order, 1)]
    page = _page(ranked, query_args, key=lambda row: int(row["id"]))
    if flag(query_args, "full"):
        names = await profiles(source, [int(row["id"]) for row in page])
        page = [{**row, **names.get(int(row["id"]), {})} for row in page]
    return page


# ---- the routes ----

Handler = Callable[[Source, Any, dict], Awaitable[Any]]


def _route(work: Callable[..., Awaitable[Any]], *extra: Any, takes_query: bool = True) -> Handler:
    async def run(source: Source, query_args: Any, match: dict) -> Any:
        return await (work(source, query_args, *extra) if takes_query else work(source, *extra))
    return run


async def _one_channel(source: Source, query_args: Any, match: dict) -> Any:
    return await channel(source, _path_id(match, "channel_id"))


async def _one_invite(source: Source, query_args: Any, match: dict) -> Any:
    number = match.get("invite_id", "")
    if not DIGITS.fullmatch(number) or not 1 <= int(number) <= JS_SAFE:
        raise Problem(404, "No such invite.")
    return await invite(source, int(number))


def _path_id(match: dict, name: str) -> int:
    value = match.get(name, "")
    if not DIGITS.fullmatch(value) or int(value) >= 2 ** 63:
        raise Problem(404, "Not found.")
    return int(value)


# Paths under /v1/guilds/{guild_id}.
GUILD_ROUTES: dict[str, Handler] = {
    "/messages/series": _route(series, "messages"),
    "/messages/sums": _route(sums, "messages"),
    "/messages/tops/members": _route(top_members, "messages"),
    "/messages/tops/channels": _route(top_channels, "messages"),
    "/voice/series": _route(series, "voice"),
    "/voice/sums": _route(sums, "voice"),
    "/voice/tops/members": _route(top_members, "voice"),
    "/voice/tops/channels": _route(top_channels, "voice"),
    "/activities/series": _route(activity_series),
    "/activities/tops/activities": _route(top_activities),
    "/statuses/series": _route(status_series),
    "/membercounts/series": _route(member_count_series),
    "/counts/members/series": _route(unique_series, "user_id"),
    "/counts/channels/series": _route(unique_series, "channel_id"),
    "/counts/members": _route(member_counts),
    "/invites": _route(invites, takes_query=False),
    "/invites/series": _route(invite_series),
    "/invites/tops/invites": _route(top_invites),
    "/invites/tops/members": _route(top_inviters),
    "/channels": _route(channels),
    "/channels/{channel_id}": _one_channel,
    "/invites/{invite_id}": _one_invite,
}


# ---- keys and limits ----

KEY_PREFIX = "cgstats_"
KEY_PATTERN = re.compile(r"cgstats_[A-Za-z0-9_-]{43}")
REQUESTS_PER_WINDOW = 60
WINDOW_SECONDS = 60


def read_bearer(header: str | None) -> str | None:
    """The key from an Authorization header, or None."""
    if not header:
        return None
    scheme, _, value = header.strip().partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not KEY_PATTERN.fullmatch(value):
        return None
    return value


@dataclass
class Limiter:
    """A fixed window of requests per key, with what the rate limit headers say."""
    per_window: int = REQUESTS_PER_WINDOW
    seconds: int = WINDOW_SECONDS
    windows: dict[int, tuple[float, int]] = field(default_factory=dict)

    def take(self, owner: int, now: float) -> tuple[bool, dict[str, str]]:
        start, used = self.windows.get(owner, (now, 0))
        if now - start >= self.seconds:
            start, used = now, 0
        allowed = used < self.per_window
        used += 1 if allowed else 0
        self.windows[owner] = (start, used)
        if len(self.windows) > 10_000:
            self.windows.pop(next(iter(self.windows)))
        # Held to the window: near a power of two, (start + seconds) - now can round to a hair over the window and read one second too many.
        reset = max(1, min(self.seconds, math.ceil(start + self.seconds - now)))
        headers = {"x-ratelimit-limit": str(self.per_window), "x-ratelimit-remaining": str(max(0, self.per_window - used)), "x-ratelimit-reset": str(reset)}
        if not allowed:
            headers["retry-after"] = str(reset)
        return allowed, headers
