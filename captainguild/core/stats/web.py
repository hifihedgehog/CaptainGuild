"""What the dashboard's statistics pages show.

A view is the question a page asks: a period, a bucket size, a unit for time, and filters. It's read from the page's address over the
server's own settings, so a view the address leaves alone reads as the server set it, and a link shows the same numbers to whoever opens
it. A preset is a view's address, saved under a name.

Each page's numbers come from one or two passes over the counts, read on a connection of their own (Database.reader), so a long read
never holds up the bot's writes. Results carry IDs only. The module names them for whoever is looking, which is where hidden channels and
member data are left out.
"""
from __future__ import annotations

import asyncio
import bisect
import calendar
import datetime as dt
import hashlib
import json
import re
import sys
import time
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from . import emoji as emoji_text
from . import invites as invite_rules
from . import query, store
from .timeframe import (INTERVALS, SLOT_SECONDS, bucket_edges, bucket_label, day_label, fitting_interval, floor_bucket, period_for, slot_of,
                        slot_time, zone, zone_label)

# The same names modules/statistics.py gives its settings.
EMOJI_STATES = ("used", "unused", "deleted", "unavailable")
EMOJI_TYPES = ("server", "animated", "managed", "restricted", "super")
UNITS = ("minutes", "hours", "days")
STATS = ("messages", "voice", "both")
TABLE = {"messages": ("stat_messages", "count"), "voice": ("stat_voice", "seconds"), "activities": ("stat_activities", "seconds"),
         "emoji": ("stat_emoji", "count")}

# Every field a view has, in the order an address lists them. The names match the settings they start from, apart from `unit`.
FIELDS = ("period", "interval", "unit", "include", "stat", "voice_states", "channel_mode", "channels", "voice_mode", "voice_channels",
          "member_mode", "members", "roles_allow", "roles_block", "activity_mode", "activities", "invite_mode", "invites", "fake_mode",
          "fake_match", "fake_reasons", "emoji_mode", "emoji", "emoji_states", "emoji_type_mode", "emoji_type_match", "emoji_types",
          "emoji_uses")
SETTING_NAME = {"unit": "time_unit"}
CHOICES = {
    "interval": INTERVALS, "unit": UNITS, "include": ("humans", "bots", "both"), "stat": STATS,
    "channel_mode": ("block", "allow"), "voice_mode": ("same", "block", "allow"), "member_mode": ("block", "allow"),
    "activity_mode": ("block", "allow"), "invite_mode": ("block", "allow"), "fake_mode": ("block", "allow"), "fake_match": ("any", "all"),
    "emoji_mode": ("block", "allow"), "emoji_type_mode": ("block", "allow"), "emoji_type_match": ("any", "all"),
}
NAMED = {"voice_states": tuple(store.VOICE_STATES), "fake_reasons": tuple(invite_rules.FAKE_REASONS), "emoji_states": EMOJI_STATES,
         "emoji_types": EMOJI_TYPES, "emoji_uses": tuple(store.EMOJI_USES)}
ID_LISTS = ("channels", "voice_channels", "members", "roles_allow", "roles_block", "activities")
VIEW_LIST_LIMIT = 500  # IDs in one list of an address
EMOJI_LIMIT = 25
PERIOD_TEXT = re.compile(r"(\d{1,6})(h|d|w|mo)")
PERIOD_LIMITS = {"h": 876_000, "d": 36_500, "w": 5_214, "mo": 1_200}  # a hundred years of each
PERIOD_WORDS = {"h": "hour", "d": "day", "w": "week", "mo": "month"}
DATE_TEXT = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
ID_TEXT = re.compile(r"\d{1,20}")
INVITE_TEXT = re.compile(r"(?:vanity:)?[A-Za-z0-9-]{2,32}")
CUSTOM_KEY = re.compile(r"[cs]:\d{15,21}")
TOP_SERIES = 7  # named lines on a chart of many things, before the rest fold into one. Seven and "Other" use the eight chart colors.
RECENT_JOINS = 10_000  # the newest joins a page lists. The counts above it cover every join in the period.
BREAKDOWN_LIMIT = 2_000  # people listed per channel in a channel drilldown, most active first
DRILL_LIMIT = 25
UNKNOWN_INVITE = ""
DISCORD_START = dt.datetime(2015, 1, 1, tzinfo=dt.timezone.utc).timestamp()  # Discord opened in 2015, so nothing counted is older


# ---- the view ----

def _period_text(raw: str) -> str | None:
    text = raw.strip().lower()
    if text in ("", "default"):
        return ""
    if text == "all":
        return "all"
    if text.startswith("since:"):
        found = DATE_TEXT.fullmatch(text[6:].strip())
        if not found:
            return None
        try:
            return "since:" + dt.date(*(int(part) for part in found.groups())).isoformat()
        except ValueError:
            return None
    found = PERIOD_TEXT.fullmatch(text)
    if not found:
        return None
    amount, unit = int(found.group(1)), found.group(2)
    if not 1 <= amount <= PERIOD_LIMITS[unit]:
        return None
    return f"{amount}{unit}"


def _emoji_key(raw: str) -> str | None:
    cleaned = raw.strip()
    if CUSTOM_KEY.fullmatch(cleaned):
        return cleaned
    if cleaned.startswith("u:") and 1 < len(cleaned) <= 40:
        bare = emoji_text.bare(cleaned[2:])
        return store.unicode_key(bare) if bare else None
    return None


def id_of(raw: str) -> int | None:
    cleaned = raw.strip()
    if not ID_TEXT.fullmatch(cleaned):
        return None
    number = int(cleaned)
    return number if 0 < number < 2 ** 63 else None


def _parts(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(part) for part in raw]
    text = str(raw)
    return [part for part in text.split(",") if part.strip()] if text.strip() else []


def read_field(name: str, raw: Any) -> Any:
    """One field of a view from an address or a preset, normalized, or None when it can't be read. An empty list is a value: it means none."""
    if raw is None:
        return None
    if name == "period":
        return _period_text(str(raw))
    if name in CHOICES:
        text = str(raw).strip()
        return text if text in CHOICES[name] else None
    if name in NAMED:
        wanted = {part.strip() for part in _parts(raw)}
        if not wanted <= set(NAMED[name]):
            return None
        return [item for item in NAMED[name] if item in wanted]
    if name in ID_LISTS:
        out: list[int] = []
        for part in _parts(raw):
            number = id_of(part)
            if number is None:
                return None
            if number not in out:
                out.append(number)
        return out[:VIEW_LIST_LIMIT]
    if name == "invites":
        out_codes: list[str] = []
        for part in _parts(raw):
            if not INVITE_TEXT.fullmatch(part.strip()):
                return None
            if part.strip() not in out_codes:
                out_codes.append(part.strip())
        return out_codes[:VIEW_LIST_LIMIT]
    if name == "emoji":
        keys: list[str] = []
        for part in _parts(raw):
            key = _emoji_key(part)
            if key is None:
                return None
            if key not in keys:
                keys.append(key)
        return keys[:EMOJI_LIMIT]
    return None


class View:
    """A page's question: every field, the address over the server's settings. `asked` is what the address itself set."""

    def __init__(self, values: dict, asked: dict) -> None:
        self.values = values
        self.asked = asked

    def __getitem__(self, name: str) -> Any:
        return self.values[name]

    def key(self) -> str:
        return json.dumps(self.values, sort_keys=True, default=str)

    def address(self) -> dict[str, str]:
        return address_of(self.asked)


def default_of(name: str, cfg: dict) -> Any:
    if name == "period":
        return ""
    if name == "stat":
        return "both"
    value = cfg.get(SETTING_NAME.get(name, name))
    return list(value) if isinstance(value, (list, tuple)) else value


def read_view(params: Mapping[str, Any], cfg: dict) -> View:
    """A view from an address's query. A field it doesn't set, or sets to something unreadable, keeps the server's setting."""
    asked = {}
    for name in FIELDS:
        if name in params:
            value = read_field(name, params[name])
            if value is not None:
                asked[name] = value
    values = {name: asked[name] if name in asked else default_of(name, cfg) for name in FIELDS}
    return View(values, asked)


def address_of(asked: Mapping[str, Any]) -> dict[str, str]:
    """The query an address carries for these fields, lists joined with commas."""
    out = {}
    for name in FIELDS:
        if name not in asked:
            continue
        value = asked[name]
        out[name] = ",".join(str(part) for part in value) if isinstance(value, list) else str(value)
    return out


def clean_address(params: Mapping[str, Any]) -> dict[str, str]:
    """What a preset keeps of a submitted address: the fields it can read, normalized. Anything else is dropped."""
    return address_of({name: value for name in FIELDS if name in params and (value := read_field(name, params[name])) is not None})


# ---- filters and periods ----

RoleHolders = Callable[[Sequence[int]], "set[int] | None"]


def filters_of(view: View, role_holders: RoleHolders) -> tuple[query.Filters, bool]:
    """The filters a view sets, and whether a role filter is among them. Roles become the people who hold them now, so someone who left
    drops out of a view that names a role they had. `role_holders` gives None when the server's members can't be read."""
    members_in: set[int] | None = None
    members_out: set[int] = set()
    if view["member_mode"] == "allow" and view["members"]:
        members_in = set(view["members"])
    elif view["member_mode"] == "block":
        members_out |= set(view["members"])
    roles_on = False
    if view["roles_allow"]:
        holders = role_holders(view["roles_allow"])
        if holders is not None:
            roles_on = True
            members_in = set(holders) if members_in is None else members_in & set(holders)
    if view["roles_block"]:
        holders = role_holders(view["roles_block"])
        if holders is not None:
            roles_on = True
            members_out |= set(holders)
    states = frozenset(store.VOICE_STATES[name] for name in view["voice_states"])
    uses = frozenset(store.EMOJI_USES[name] for name in view["emoji_uses"])
    built = query.Filters(
        include=view["include"], voice_states=states, channel_mode=view["channel_mode"], channels=frozenset(view["channels"]),
        voice_mode=view["voice_mode"], voice_channels=frozenset(view["voice_channels"]),
        members_in=frozenset(members_in) if members_in is not None else None, members_out=frozenset(members_out),
        activity_mode=view["activity_mode"], activities=frozenset(view["activities"]),
        emoji_mode=view["emoji_mode"], emoji=frozenset(view["emoji"]), emoji_uses=uses,
    )
    return built, roles_on


def _local_start(day: dt.date, tz: Any) -> float:
    return dt.datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()


def months_back(now: float, months: int, tz: Any) -> float:
    """The same local day and time `months` calendar months ago, or the month's last day when it's shorter."""
    local = dt.datetime.fromtimestamp(now, tz)
    total = local.year * 12 + local.month - 1 - months
    year, month = divmod(total, 12)
    month += 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    return dt.datetime(year, month, day, local.hour, local.minute, tzinfo=tz).timestamp()


def period_start(text: str, cfg: dict, now: float, interval: str, tz: Any) -> tuple[float | None, str]:
    """Where a view's period begins, before it's rounded down to a whole bucket, and its label. None reaches back to the first count."""
    if text == "":
        period = period_for(cfg.get("lookback") or {}, now, interval, tz)
        return period.start, period.label
    if text == "all":
        return None, "All time"
    if text.startswith("since:"):
        day = dt.date.fromisoformat(text[6:])
        return _local_start(day, tz), f"Since {day_label(day)}"
    found = PERIOD_TEXT.fullmatch(text)
    amount, unit = int(found.group(1)), found.group(2)
    plural = "" if amount == 1 else "s"
    label = f"Last {amount} {PERIOD_WORDS[unit]}{plural}"
    if unit == "h":
        return now - amount * 3600, label
    if unit == "d":
        return now - amount * 86400, label
    if unit == "w":
        return now - amount * 7 * 86400, label
    return months_back(now, amount, tz), label


@dataclass
class Frame:
    """A view made concrete: who, which filters, and the buckets of its period."""
    guild_id: int
    cfg: dict
    view: View
    filters: query.Filters
    roles_on: bool
    tz: Any
    now: float
    start: float
    end: float
    label: str
    interval: str
    asked_interval: str
    edges: list[int]
    end_slot: int
    first: int | None = None  # the first slot anything of the frame's kinds was counted in
    visible: frozenset[int] | None = None  # the only channels whose counts the reader may see, or None for every channel

    @property
    def start_slot(self) -> int:
        return self.edges[0] if self.edges else self.end_slot

    @property
    def broad(self) -> bool:
        """Whether the period holds most of what's counted, so a ranking reads every row of the server anyway. Such a read goes through
        the index that holds the rows grouped by person or channel, which takes a fraction of the time of sorting them."""
        if self.first is None:
            return False
        span = self.end_slot - self.first
        return span > 0 and (self.end_slot - max(self.start_slot, self.first)) * 4 >= span

    def describe(self) -> dict:
        return {
            "start": self.start, "end": self.end, "label": self.label, "interval": self.interval, "asked_interval": self.asked_interval,
            "zone": getattr(self.tz, "key", "UTC"), "zone_label": zone_label(self.tz, self.now), "unit": self.view["unit"],
            "buckets": [slot_time(slot) for slot in self.edges], "labels": [bucket_label(slot_time(slot), self.interval, self.tz) for slot in self.edges],
        }


async def first_counted(db: Any, guild_id: int, kinds: Iterable[str]) -> int | None:
    """The first slot anything of these kinds was counted in."""
    found = []
    for kind in kinds:
        table = TABLE[kind][0] if kind in TABLE else {"members": "stat_members", "status": "stat_status"}[kind]
        value = await db.scalar(f"SELECT MIN(slot) FROM {table} WHERE guild_id = ?", (guild_id,))
        if value is not None:
            found.append(value)
    return min(found) if found else None


async def frame_for(db: Any, guild_id: int, cfg: dict, view: View, now: float, role_holders: RoleHolders, kinds: Iterable[str],
                    visible: frozenset[int] | None = None) -> Frame:
    tz = zone(cfg.get("timezone"))
    asked = view["interval"]
    start, label = period_start(view["period"], cfg, now, asked, tz)
    first = await first_counted(db, guild_id, kinds)
    if start is None:
        start = slot_time(first) if first is not None else now - 86400
    # An address may ask for any period back to year 1. The buckets start at the first count, and never before Discord began, so what a
    # read costs follows what's stored.
    start = min(max(start, DISCORD_START, slot_time(first) if first is not None else DISCORD_START), now)
    interval = fitting_interval(start, now, asked)
    start = floor_bucket(start, interval, tz)
    edges = query.slot_edges(bucket_edges(start, now, interval, tz)) if start < now else []
    filters, roles_on = filters_of(view, role_holders)
    return Frame(guild_id, cfg, view, filters, roles_on, tz, now, start, now, label, interval, asked, edges, slot_of(now) + 1, first, visible)


# ---- reading ----

def _ids_json(ids: Iterable[int]) -> str:
    return json.dumps(sorted(ids))


CHANNEL_KINDS = ("messages", "voice", "emoji")  # the count tables kept per channel


def _seen_only(built: query.Clause, kind: str, fr: Frame) -> None:
    """Keeps a read to the channels the frame's reader may see. A filter naming any other channel then finds nothing."""
    if fr.visible is not None and kind in CHANNEL_KINDS:
        built.add("channel_id IN (SELECT value FROM json_each(?))", _ids_json(fr.visible))


def visible_key(visible: frozenset[int] | None) -> str | None:
    """A short name for a set of channels, for the key of an answer read under it."""
    return None if visible is None else hashlib.sha1(_ids_json(visible).encode()).hexdigest()


def clause(kind: str, fr: Frame, start: int | None = None, end: int | None = None, *, people: bool = True, ranged: bool = True) -> query.Clause:
    """The WHERE clause for one count table under a frame's filters. Long lists of people go in as one JSON value, so any number fits.

    `ranged` False leaves out the period's slots, for a read that joins each bucket's own range: with both, SQLite searches the key by the
    whole period for every bucket, and four years of weekly buckets took 53 seconds instead of one."""
    low = (fr.start_slot if start is None else start) if ranged else None
    high = (fr.end_slot if end is None else end) if ranged else None
    built = query.where(kind, fr.guild_id, fr.filters, low, high, people=False)
    _seen_only(built, kind, fr)
    f = fr.filters
    if people:
        if f.members_in is not None:
            if f.members_in:
                built.add("user_id IN (SELECT value FROM json_each(?))", _ids_json(f.members_in))
            else:
                built.add("0")
        if f.members_out:
            built.add("user_id NOT IN (SELECT value FROM json_each(?))", _ids_json(f.members_out))
    return built


async def rows(db: Any, sql: str, args: Sequence[Any] = ()) -> list:
    """Rows as tuples, which a read of hundreds of thousands builds far faster than dicts."""
    cursor = await db.conn.execute(sql, tuple(args))
    try:
        return await cursor.fetchall()
    finally:
        await cursor.close()


def _buckets(edges: Sequence[int], end: int) -> tuple[str, list]:
    marks = ", ".join("(?, ?, ?)" for _ in edges)
    args: list[int] = []
    for index, low in enumerate(edges):
        args += (index, low, edges[index + 1] if index + 1 < len(edges) else end)
    return f"WITH b(i, lo, hi) AS (VALUES {marks})", args


async def per_bucket(db: Any, fr: Frame, kind: str, select: str, *, select_args: Sequence[Any] = (), extra: tuple | None = None, group: str = "") -> list:
    """`select` per bucket of the frame, each bucket read straight off the table's key. Rows start with the bucket's index, then `group`."""
    if not fr.edges:
        return []
    table, _ = TABLE[kind]
    cte, args = _buckets(fr.edges, fr.end_slot)
    where = clause(kind, fr, ranged=False)
    if extra:
        where.add(*extra)
    grouped = f", {group}" if group else ""
    sql = (f"{cte} SELECT b.i{grouped}, {select} FROM b CROSS JOIN {table} ON {table}.guild_id = ? AND {table}.slot >= b.lo AND {table}.slot < b.hi "
           f"WHERE {where.text()} GROUP BY b.i{grouped}")
    return await rows(db, sql, [*args, *select_args, fr.guild_id, *where.args])


def moving_average(values: Sequence[float], size: int) -> list[float | None]:
    """The average of each point and the size-1 before it. The first size-1 points have no full window, so they're None."""
    out: list[float | None] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= size:
            running -= values[index - size]
        out.append(running / size if index >= size - 1 else None)
    return out


# ---- messages and voice ----

async def activity(db: Any, fr: Frame, kind: str, *, uniques: bool = True) -> dict:
    """The plot on the Messages and Voice pages: the count, and how many different people and channels, in each bucket. `uniques` False
    reads the count alone, for a panel that needs nothing more."""
    _, value = TABLE[kind]
    size = len(fr.edges)
    counts, people, places = [0] * size, [0] * size, [0] * size
    select = f"SUM({value})" + (", COUNT(DISTINCT NULLIF(user_id, 0)), COUNT(DISTINCT channel_id)" if uniques else "")
    for row in await per_bucket(db, fr, kind, select):
        counts[row[0]] = int(row[1] or 0)
        if uniques:
            people[row[0]], places[row[0]] = row[2], row[3]
    where = clause(kind, fr)
    if uniques:
        # Grouped by person, then by channel, each through the index that already holds that order: two short reads instead of one long
        # sort of every row.
        total, unique_members = (await rows(db, f"SELECT COALESCE(SUM(n), 0), COUNT(CASE WHEN user_id != 0 THEN 1 END) FROM (SELECT user_id, SUM({value}) AS n "
                                                f"FROM {source(kind, 'user_id', fr)} WHERE {where.text()} GROUP BY user_id)", where.args))[0]
        unique_channels = (await rows(db, f"SELECT COUNT(*) FROM (SELECT channel_id FROM {source(kind, 'channel_id', fr)} WHERE {where.text()} GROUP BY channel_id)",
                                      where.args))[0][0]
    else:
        table, _ = TABLE[kind]
        total = (await rows(db, f"SELECT COALESCE(SUM({value}), 0) FROM {table} WHERE {where.text()}", where.args))[0][0]
        unique_members = unique_channels = None
    return {"count": counts, "members": people if uniques else None, "channels": places if uniques else None, "total": int(total),
            "unique_members": unique_members, "unique_channels": unique_channels, "average": (sum(counts) / size) if size else 0}


# The covering indexes of the base migration (core/stats/store.py), by table and what a ranking groups by.
GROUPED = {("messages", "user_id"): "stat_messages_user", ("messages", "channel_id"): "stat_messages_channel", ("voice", "user_id"): "stat_voice_user",
           ("voice", "channel_id"): "stat_voice_channel", ("emoji", "emoji"): "stat_emoji_by_use", ("activities", "activity_id"): "stat_activities_activity"}


def source(kind: str, by: str, fr: Frame) -> str:
    """The table a ranking reads, through its covering index when the period holds most of the rows."""
    table, _ = TABLE[kind]
    index = GROUPED.get((kind, by))
    return f"{table} INDEXED BY {index}" if index and fr.broad else table


async def top_members(db: Any, fr: Frame, kind: str) -> list[tuple[int, int, int]]:
    """Everyone with something in the period, most first: (ID, total, channels). Privacy's nobody isn't ranked."""
    _, value = TABLE[kind]
    where = clause(kind, fr)
    where.add("user_id != 0")
    found = await rows(db, f"SELECT user_id, SUM({value}), COUNT(DISTINCT channel_id) FROM {source(kind, 'user_id', fr)} WHERE {where.text()} GROUP BY user_id", where.args)
    ranked = [(row[0], int(row[1] or 0), row[2]) for row in found if row[1]]
    ranked.sort(key=lambda row: (-row[1], row[0]))
    return ranked


async def top_channels(db: Any, fr: Frame, kind: str) -> list[tuple[int, int, int]]:
    """Every channel with something in the period, most first: (ID, total, people)."""
    _, value = TABLE[kind]
    where = clause(kind, fr)
    found = await rows(db, f"SELECT channel_id, SUM({value}), COUNT(DISTINCT NULLIF(user_id, 0)) FROM {source(kind, 'channel_id', fr)} WHERE {where.text()} "
                           f"GROUP BY channel_id", where.args)
    ranked = [(row[0], int(row[1] or 0), row[2]) for row in found if row[1]]
    ranked.sort(key=lambda row: (-row[1], row[0]))
    return ranked


WEEK_SLOTS = 7 * 24 * 3600 // SLOT_SECONDS
UTC_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _utc_offset(tz: Any, slot: int) -> int:
    """The zone's offset from UTC in seconds at a slot. Worked out from UTC by Python itself, so any date reads, before 1970 too."""
    offset = (UTC_EPOCH + dt.timedelta(seconds=slot * SLOT_SECONDS)).astimezone(tz).utcoffset()
    return int(offset.total_seconds()) if offset is not None else 0


def offset_runs(tz: Any, start_slot: int, end_slot: int) -> list[tuple[int, int]]:
    """The stretches of one UTC offset between two slots: (first slot, offset in seconds) for each. The offset is looked up a week apart,
    and between two lookups that differ the change is halved down to its slot, so years of hours take a few hundred lookups."""
    runs = [(start_slot, _utc_offset(tz, start_slot))]
    low = start_slot
    while low < end_slot:
        high = min(low + WEEK_SLOTS, end_slot)
        probe = high if high < end_slot else high - 1  # the next stretch's first slot, so a change right there isn't missed
        current = runs[-1][1]
        if _utc_offset(tz, probe) == current:
            low = high
            continue
        same, other = low, probe
        while other - same > 1:
            middle = (same + other) // 2
            if _utc_offset(tz, middle) == current:
                same = middle
            else:
                other = middle
        runs.append((other, _utc_offset(tz, other)))
        low = other
    return runs


class LocalHours:
    """The local weekday (Monday first) and hour of each slot. A slot is a quarter hour of UTC and every zone sits a whole number of
    quarter hours off UTC, so each slot lies in one local hour, also where half an hour or 45 minutes splits a UTC hour in two."""

    def __init__(self, tz: Any, start_slot: int, end_slot: int) -> None:
        self.runs = offset_runs(tz, start_slot, max(end_slot, start_slot))
        self.starts = [first for first, _ in self.runs]

    def __call__(self, slot: int) -> tuple[int, int]:
        index = max(bisect.bisect_right(self.starts, slot) - 1, 0)
        local = slot * SLOT_SECONDS + self.runs[index][1]
        return (local // 86400 + 3) % 7, local // 3600 % 24  # 1970-01-01 was a Thursday


def hours_of_week(start_slot: int, end_slot: int, cell: LocalHours) -> list[list[float]]:
    """How many hours of each weekday and hour lie in [start_slot, end_slot), a quarter hour at a time. In a stretch of one UTC offset,
    every whole week holds one hour of each, so only the slots after the last whole week of a stretch are looked at one by one."""
    seen = [[0.0] * 24 for _ in range(7)]
    for index, (first, offset) in enumerate(cell.runs):
        low = max(first, start_slot)
        high = min(cell.starts[index + 1] if index + 1 < len(cell.starts) else end_slot, end_slot)
        if high <= low:
            continue
        weeks, extra = divmod(high - low, WEEK_SLOTS)
        if weeks:
            for row in seen:
                for hour in range(24):
                    row[hour] += weeks
        for slot in range(high - extra, high):
            local = slot * SLOT_SECONDS + offset
            seen[(local // 86400 + 3) % 7][local // 3600 % 24] += 0.25
    return seen


async def heatmap(db: Any, fr: Frame, kind: str) -> dict:
    """A 7 by 24 grid, Monday first, of the average per hour at each hour of the week, in the server's time zone. For statuses, the
    average number of people not offline."""
    cell = LocalHours(fr.tz, fr.start_slot, fr.end_slot)
    sums = [[0.0] * 24 for _ in range(7)]
    if kind == "status":
        readings: dict[tuple[int, int], list[float]] = defaultdict(list)
        for slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present in await rows(
                db, "SELECT slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present FROM stat_status "
                    "WHERE guild_id = ? AND slot >= ? AND slot < ?", (fr.guild_id, fr.start_slot, fr.end_slot)):
            if samples:
                people = (online + idle + dnd) / samples
                bots = (bot_online + bot_idle + bot_dnd) / samples
                include = fr.view["include"]
                readings[cell(slot)].append(people if include == "humans" else bots if include == "bots" else people + bots)
            elif present_samples:
                readings[cell(slot)].append(present / present_samples)
        for (day, hour), values in readings.items():
            sums[day][hour] = sum(values) / len(values)
        return {"cells": sums}
    table, value = TABLE[kind]
    where = clause(kind, fr)
    for slot, total in await rows(db, f"SELECT slot, SUM({value}) FROM {table} WHERE {where.text()} GROUP BY slot", where.args):
        day, hour = cell(slot)
        sums[day][hour] += total or 0
    seen = hours_of_week(fr.start_slot, fr.end_slot, cell)
    return {"cells": [[sums[day][hour] / seen[day][hour] if seen[day][hour] else 0.0 for hour in range(24)] for day in range(7)]}


# ---- members and statuses ----

async def member_series(db: Any, fr: Frame) -> dict:
    buckets = await query.member_counts(db, fr.guild_id, fr.edges, fr.end_slot)
    include = fr.view["include"]
    pick = "humans" if include == "humans" else "bots" if include == "bots" else "members"
    return {
        "members": [bucket[pick] if bucket[pick] is not None else bucket["members"] for bucket in buckets],
        "joins": [bucket["joins"] for bucket in buckets],
        "leaves": [bucket["leaves"] for bucket in buckets],
    }


async def overview(db: Any, fr: Frame) -> dict:
    """The member count with joins and leaves, and the period's headline numbers beside the period before it."""
    members = await member_series(db, fr)
    length = fr.end_slot - fr.start_slot
    before = (fr.start_slot - length, fr.start_slot)
    out: dict[str, Any] = {"series": members}
    totals: dict[str, Any] = {}
    for kind in ("messages", "voice"):
        table, value = TABLE[kind]
        for label, (low, high) in (("now", (fr.start_slot, fr.end_slot)), ("before", before)):
            where = clause(kind, fr, low, high)
            row = (await rows(db, f"SELECT COALESCE(SUM({value}), 0) FROM {table} WHERE {where.text()}", where.args))[0]
            totals[f"{kind}_{label}"] = int(row[0])
    for label, (low, high) in (("now", (fr.start_slot, fr.end_slot)), ("before", before)):
        totals[f"contributors_{label}"] = await _contributors(db, fr, low, high)
    joins, leaves = await query.joins_and_leaves(db, fr.guild_id, fr.start_slot, fr.end_slot)
    joins_before, leaves_before = await query.joins_and_leaves(db, fr.guild_id, *before)
    totals.update({"joins_now": joins, "leaves_now": leaves, "joins_before": joins_before, "leaves_before": leaves_before})
    known = [value for value in members["members"] if value is not None]
    totals["members_now"] = known[-1] if known else None
    totals["members_start"] = known[0] if known else None
    latest = await db.one("SELECT samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present FROM stat_status "
                          "WHERE guild_id = ? ORDER BY slot DESC LIMIT 1", (fr.guild_id,))
    totals["online_now"] = _present(latest, fr.view["include"]) if latest else None
    totals["online_breakdown"] = bool(latest and latest["samples"])  # read member by member, rather than Discord's count of members present
    out["totals"] = totals
    return out


def _present(row: dict, include: str) -> float | None:
    if row["samples"]:
        people = (row["online"] + row["idle"] + row["dnd"]) / row["samples"]
        bots = (row["bot_online"] + row["bot_idle"] + row["bot_dnd"]) / row["samples"]
        return people if include == "humans" else bots if include == "bots" else people + bots
    if row["present_samples"]:
        return row["present"] / row["present_samples"]
    return None


async def _contributors(db: Any, fr: Frame, low: int, high: int) -> int:
    """Different people with a message or voice time in [low, high). Each table's people come grouped off its per-person index first, so
    the union holds tens of thousands of IDs rather than every row."""
    message_where = clause("messages", fr, low, high)
    voice_where = clause("voice", fr, low, high)
    broad = fr.broad and low <= fr.start_slot
    messages_from = "stat_messages INDEXED BY stat_messages_user" if broad else "stat_messages"
    voice_from = "stat_voice INDEXED BY stat_voice_user" if broad else "stat_voice"
    found = await rows(db, f"SELECT COUNT(*) FROM (SELECT user_id FROM {messages_from} WHERE {message_where.text()} AND user_id != 0 GROUP BY user_id "
                           f"UNION SELECT user_id FROM {voice_from} WHERE {voice_where.text()} AND user_id != 0 GROUP BY user_id)",
                       [*message_where.args, *voice_where.args])
    return found[0][0]


async def statuses(db: Any, fr: Frame) -> dict:
    """Online, idle, do not disturb, not offline and offline in each bucket. A server with only Discord's count of members present has
    no breakdown, so those buckets have "present" alone."""
    include = fr.view["include"]
    series = await query.status_series(db, fr.guild_id, include, fr.edges, fr.end_slot)
    counts = await query.member_counts(db, fr.guild_id, fr.edges, fr.end_slot)
    pick = "humans" if include == "humans" else "bots" if include == "bots" else "members"
    out: dict[str, list] = {name: [] for name in ("online", "idle", "dnd", "present", "offline")}
    breakdown = present_only = False
    for entry, bucket in zip(series, counts):
        for name in ("online", "idle", "dnd", "present"):
            out[name].append(entry[name])
        offline = None
        if entry["online"] is not None:
            breakdown = True
            count = bucket[pick] if bucket[pick] is not None else bucket["members"]
            offline = max(count - entry["present"], 0) if count is not None else None
        elif entry["present"] is not None:
            present_only = True
            # Discord's count of members present takes in bots, so it comes off everyone, bots included. Bots alone can't be told apart in it.
            if include != "bots" and bucket["members"] is not None:
                offline = max(bucket["members"] - entry["present"], 0)
        out["offline"].append(offline)
    return {**out, "breakdown": breakdown, "present_only": present_only and not breakdown}


# ---- activities ----

async def activities(db: Any, fr: Frame) -> dict:
    """Every app played or listened to in the period, most time first, and the top ten over time."""
    where = clause("activities", fr)
    found = await rows(db, f"SELECT activity_id, SUM(seconds), COUNT(DISTINCT NULLIF(user_id, 0)) FROM {source('activities', 'activity_id', fr)} "
                           f"WHERE {where.text()} GROUP BY activity_id", where.args)
    ranked = sorted(((row[0], int(row[1] or 0), row[2]) for row in found if row[1]), key=lambda row: (-row[1], row[0]))
    names = await query.activity_names(db, fr.guild_id, [row[0] for row in ranked])
    top = [row[0] for row in ranked[:10]]
    series = {number: [0] * len(fr.edges) for number in top}
    if top:
        marks = ", ".join("?" for _ in top)
        for index, number, seconds in await per_bucket(db, fr, "activities", "SUM(seconds)", extra=(f"activity_id IN ({marks})", *top), group="activity_id"):
            series[number][index] += int(seconds or 0)
    rows_out = []
    for number, seconds, people in ranked:
        info = names.get(number) or {}
        rows_out.append({"id": number, "name": info.get("name") or "Unknown app", "kind": activity_kind(info.get("kind")),
                         "application_id": info.get("application_id"), "verified": bool(info.get("verified")), "seconds": seconds, "people": people,
                         "first_seen": info.get("first_seen")})
    return {"rows": rows_out, "series": [{"id": number, "values": series[number]} for number in top]}


def activity_kind(kind: Any) -> str:
    return next((name for name, number in store.ACTIVITY_KINDS.items() if number == kind), "playing")


# ---- emoji ----

def _emoji_types(key: str, info: dict | None, super_use: bool) -> set[str]:
    kinds = set()
    if key.startswith(("c:", "s:")):
        kinds.add("server")
    if info:
        for name in ("animated", "managed", "restricted"):
            if info.get(name):
                kinds.add(name)
    if super_use:
        kinds.add("super")
    return kinds


def _types_pass(kinds: set[str], view: View) -> bool:
    picked = set(view["emoji_types"])
    if not picked:
        return True
    hit = picked <= kinds if view["emoji_type_match"] == "all" else bool(picked & kinds)
    return hit if view["emoji_type_mode"] == "allow" else not hit


async def emoji(db: Any, fr: Frame) -> dict:
    """Each emoji and sticker: its uses in the period by kind, the people who used it, and its state. A server emoji nobody used in the
    period is listed as unused, and one deleted from the server keeps the uses it had. A type filter reads each use, since a super reaction
    is a kind of use rather than a kind of emoji."""
    where = clause("emoji", fr)
    found = await rows(db, f"SELECT emoji, flags, use, SUM(count) FROM {source('emoji', 'emoji', fr)} WHERE {where.text()} GROUP BY emoji, flags, use", where.args)
    info = await query.emoji_info(db, fr.guild_id)
    by_use = {number: name for name, number in store.EMOJI_USES.items()}
    table: dict[str, dict] = {}
    for key, flags, use, total in found:
        if not total or not _types_pass(_emoji_types(key, info.get(key), bool(flags & store.SUPER)), fr.view):
            continue
        row = table.setdefault(key, _blank_emoji(key))
        row["uses"] += int(total)
        row[by_use.get(use, "messages")] += int(total)
        if flags & store.SUPER:
            row["super"] += int(total)
    if table:
        marks = ", ".join("?" for _ in table)
        for key, count in await rows(db, f"SELECT emoji, COUNT(DISTINCT NULLIF(user_id, 0)) FROM {source('emoji', 'emoji', fr)} WHERE {where.text()} "
                                         f"AND emoji IN ({marks}) GROUP BY emoji", [*where.args, *table]):
            table[key]["people"] = count
    listed = set(fr.view["emoji"])
    for key, detail in info.items():
        if key in table or not _types_pass(_emoji_types(key, detail, False), fr.view):
            continue
        if listed and (key in listed) != (fr.view["emoji_mode"] == "allow"):
            continue
        table[key] = _blank_emoji(key)
    states = set(fr.view["emoji_states"])
    out = []
    for key, row in table.items():
        detail = info.get(key)
        row["state"] = _emoji_state(row, detail)
        if row["state"] not in states:
            continue
        row.update(emoji_face(key, detail))
        out.append(row)
    out.sort(key=lambda row: (-row["uses"], row["name"].casefold(), row["key"]))
    top = [row["key"] for row in out if row["uses"]][:TOP_SERIES]
    series = await _emoji_series(db, fr, top)
    return {"rows": out, "series": [{"key": key, "values": series[key]} for key in top]}


def _blank_emoji(key: str) -> dict:
    return {"key": key, "uses": 0, "reactions": 0, "self_reactions": 0, "messages": 0, "stickers": 0, "super": 0, "people": 0}


async def _emoji_series(db: Any, fr: Frame, keys: list[str]) -> dict[str, list[int]]:
    out = {key: [0] * len(fr.edges) for key in keys}
    if not keys:
        return out
    marks = ", ".join("?" for _ in keys)
    for index, key, total in await per_bucket(db, fr, "emoji", "SUM(count)", extra=(f"emoji IN ({marks})", *keys), group="emoji"):
        out[key][index] += int(total or 0)
    return out


def _emoji_state(row: dict, detail: dict | None) -> str:
    """One state per emoji, the first that applies: deleted, unavailable, then used or unused in the period."""
    if detail and detail.get("deleted_at") is not None:
        return "deleted"
    if detail and not detail.get("available", 1):
        return "unavailable"
    return "used" if row["uses"] else "unused"


def emoji_face(key: str, detail: dict | None) -> dict:
    """What a page shows for an emoji: a picture's address for this server's own, the character for a standard one."""
    if key.startswith("u:"):
        return {"name": key[2:], "text": emoji_text.display(key[2:]), "image": None, "kind": "standard", "animated": False}
    number = key[2:]
    animated = bool(detail and detail.get("animated"))
    if key.startswith("s:"):
        return {"name": (detail or {}).get("name") or "Sticker", "text": None, "image": f"https://media.discordapp.net/stickers/{number}.png?size=64",
                "kind": "sticker", "animated": animated}
    return {"name": (detail or {}).get("name") or "Emoji", "text": None, "image": f"https://cdn.discordapp.com/emojis/{number}.{'gif' if animated else 'png'}?size=64",
            "kind": "emoji", "animated": animated, "managed": bool(detail and detail.get("managed")), "restricted": bool(detail and detail.get("restricted"))}


# ---- invites ----

@dataclass
class JoinFacts:
    """What reading a join needs from the server now: the settings fake reasons use, and the member object of each person still here."""
    settings: dict
    member: Callable[[int], Any]


def _join_kept(row: dict, bits: int, view: View) -> bool:
    if view["include"] == "humans" and row["facts"] & store.JOIN_BOT:
        return False
    if view["include"] == "bots" and not row["facts"] & store.JOIN_BOT:
        return False
    code = row["code"] or UNKNOWN_INVITE
    if view["invites"]:
        listed = code in view["invites"]
        if listed != (view["invite_mode"] == "allow"):
            return False
    return invite_rules.passes(bits, {"fake_reasons": view["fake_reasons"], "fake_mode": view["fake_mode"], "fake_match": view["fake_match"]})


def reason_names(bits: int) -> list[str]:
    return [name for name, bit in invite_rules.FAKE_REASONS.items() if bits & bit]


class _Unknown:
    """Someone who chose privacy and hasn't left, as far as the counts show. What they hold now can't be told, so the role check passes."""

    pending = False
    flags = None

    def __init__(self, required: Iterable[int]) -> None:
        self.roles = [_Held(role_id) for role_id in required]


@dataclass(frozen=True)
class _Held:
    id: int


async def joins(db: Any, fr: Frame, facts: JoinFacts) -> list[dict]:
    """The joins in the period the view keeps, newest first, each with the fake reasons that apply now."""
    found = await db.all("SELECT user_id, at, code, inviter_id, account_created, facts, role_met_at, left_at FROM stat_joins "
                         "WHERE guild_id = ? AND at >= ? AND at < ? ORDER BY at DESC", (fr.guild_id, fr.start, fr.now + SLOT_SECONDS))
    out = []
    f = fr.filters
    unknown = _Unknown(facts.settings.get("invite_required_roles") or [])
    for row in found:
        if row["user_id"] and not f.keeps(row["user_id"]):
            continue
        if row["left_at"] is not None:
            member = None
        else:
            member = facts.member(row["user_id"]) if row["user_id"] else unknown
        bits = invite_rules.reasons(row, facts.settings, fr.now, member)
        if not _join_kept(row, bits, fr.view):
            continue
        out.append({**row, "reasons": bits})
    return out


async def invites(db: Any, fr: Frame, facts: JoinFacts) -> dict:
    """Joins over time by invite, every invite with its joins in the period, the people who invited, and the joins themselves."""
    kept = await joins(db, fr, facts)
    codes: Counter = Counter()
    per_code: dict[str, dict] = {}
    inviters: dict[int, dict] = {}
    for row in kept:
        code = row["code"] or UNKNOWN_INVITE
        codes[code] += 1
        entry = per_code.setdefault(code, {"joins": 0, "fake": 0, "left": 0, "people": set()})
        entry["joins"] += 1
        entry["fake"] += 1 if row["reasons"] else 0
        entry["left"] += 1 if row["left_at"] is not None else 0
        if row["user_id"]:
            entry["people"].add(row["user_id"])
        if row["inviter_id"]:
            mine = inviters.setdefault(row["inviter_id"], {"joins": 0, "fake": 0, "codes": set()})
            mine["joins"] += 1
            mine["fake"] += 1 if row["reasons"] else 0
            mine["codes"].add(code)
    ordered = [code for code, _ in sorted(codes.items(), key=lambda item: (-item[1], item[0])) if code != UNKNOWN_INVITE]
    shown = ordered[:TOP_SERIES - 1]  # with "other" and the joins no invite explains, eight colors in all
    series = {code: [0] * len(fr.edges) for code in [*shown, "other", UNKNOWN_INVITE]}
    starts = [slot_time(slot) for slot in fr.edges]
    for row in kept:
        index = _bucket_of(starts, row["at"])
        if index is None:
            continue
        code = row["code"] or UNKNOWN_INVITE
        series[code if code in shown or code == UNKNOWN_INVITE else "other"][index] += 1
    stored = {row["code"]: row for row in await db.all("SELECT * FROM stat_invites WHERE guild_id = ?", (fr.guild_id,))}
    labels = fr.cfg.get("invite_labels") or {}
    table = []
    for code in sorted(set(stored) | set(per_code), key=lambda code: (-(per_code.get(code) or {}).get("joins", 0), code)):
        info = stored.get(code) or {}
        entry = per_code.get(code) or {"joins": 0, "fake": 0, "left": 0, "people": set()}
        if fr.view["invites"] and (code in fr.view["invites"]) != (fr.view["invite_mode"] == "allow"):
            continue
        table.append({"code": code, "label": labels.get(code), "inviter_id": info.get("inviter_id"), "channel_id": info.get("channel_id"),
                      "uses": info.get("uses", 0), "max_uses": info.get("max_uses"), "max_age": info.get("max_age"), "temporary": bool(info.get("temporary")),
                      "vanity": bool(info.get("vanity")), "created_at": info.get("created_at"), "deleted_at": info.get("deleted_at"),
                      "joins": entry["joins"], "fake": entry["fake"], "real": entry["joins"] - entry["fake"], "left": entry["left"], "people": len(entry["people"])})
    inviter_rows = [{"user_id": user_id, "joins": entry["joins"], "fake": entry["fake"], "real": entry["joins"] - entry["fake"], "invites": len(entry["codes"])}
                    for user_id, entry in inviters.items()]
    inviter_rows.sort(key=lambda row: (-row["real"], -row["joins"], row["user_id"]))
    recent = [{"at": row["at"], "user_id": row["user_id"], "code": row["code"], "inviter_id": row["inviter_id"], "reasons": reason_names(row["reasons"]),
               "left_at": row["left_at"]} for row in kept[:RECENT_JOINS]]
    return {
        "series": [{"code": code, "values": series[code]} for code in [*shown, "other", UNKNOWN_INVITE] if any(series[code])],
        "invites": table, "inviters": inviter_rows, "joins": recent, "total": len(kept), "fake": sum(1 for row in kept if row["reasons"]),
        "truncated": len(kept) > RECENT_JOINS,
    }


def _bucket_of(starts: Sequence[float], moment: float) -> int | None:
    if not starts or moment < starts[0]:
        return None
    low, high = 0, len(starts)
    while low < high:
        middle = (low + high) // 2
        if starts[middle] <= moment:
            low = middle + 1
        else:
            high = middle
    return low - 1


# ---- analytics ----

async def analytics(db: Any, fr: Frame, members_now: frozenset[int] | None) -> dict:
    """Messages and voice in each bucket, how many different people and channels had each, and how much came from people who have
    left the server. That share is left out under a role filter, which only sees people who are still here."""
    size = len(fr.edges)
    out: dict[str, Any] = {}
    gone = members_now is not None and not fr.roles_on
    for kind in ("messages", "voice"):
        _, value = TABLE[kind]
        select = f"SUM({value}), COUNT(DISTINCT NULLIF(user_id, 0)), COUNT(DISTINCT channel_id)"
        args: list[Any] = []
        if gone:
            select += f", SUM(CASE WHEN user_id != 0 AND user_id NOT IN (SELECT value FROM json_each(?)) THEN {value} ELSE 0 END)"
            args.append(_ids_json(members_now or ()))
        totals, people, places, left = [0] * size, [0] * size, [0] * size, [0] * size
        for row in await per_bucket(db, fr, kind, select, select_args=args):
            index = row[0]
            totals[index], people[index], places[index] = int(row[1] or 0), row[2], row[3]
            if gone:
                left[index] = int(row[4] or 0)
        out[kind] = {"count": totals, "members": people, "channels": places, "not_in_server": left if gone else None}
    return out


async def analytics_unique(db: Any, fr: Frame) -> dict:
    """Different people and channels in each bucket across messages and voice together."""
    size = len(fr.edges)
    people, places = [0] * size, [0] * size
    if fr.edges:
        cte, args = _buckets(fr.edges, fr.end_slot)
        message_where = clause("messages", fr, ranged=False)
        voice_where = clause("voice", fr, ranged=False)
        sql = (f"{cte} SELECT i, COUNT(DISTINCT u), COUNT(DISTINCT c) FROM ("
               f"SELECT b.i AS i, NULLIF(stat_messages.user_id, 0) AS u, stat_messages.channel_id AS c FROM b CROSS JOIN stat_messages "
               f"ON stat_messages.guild_id = ? AND stat_messages.slot >= b.lo AND stat_messages.slot < b.hi WHERE {message_where.text()} "
               f"UNION ALL SELECT b.i, NULLIF(stat_voice.user_id, 0), stat_voice.channel_id FROM b CROSS JOIN stat_voice "
               f"ON stat_voice.guild_id = ? AND stat_voice.slot >= b.lo AND stat_voice.slot < b.hi WHERE {voice_where.text()}) GROUP BY i")
        for index, who, where_ in await rows(db, sql, [*args, fr.guild_id, *message_where.args, fr.guild_id, *voice_where.args]):
            people[index], places[index] = who, where_
    return {"members": people, "channels": places}


async def analytics_people(db: Any, fr: Frame) -> list[dict]:
    """Each person's messages and voice time in the period, with how many channels each covered."""
    table: dict[int, dict] = {}
    for kind in ("messages", "voice"):
        for user_id, total, channels in await top_members(db, fr, kind):
            row = table.setdefault(user_id, {"user_id": user_id, "messages": 0, "voice": 0, "message_channels": 0, "voice_channels": 0})
            row[kind] = total
            row["message_channels" if kind == "messages" else "voice_channels"] = channels
    out = list(table.values())
    out.sort(key=lambda row: (-row["messages"], -row["voice"], row["user_id"]))
    return out


async def analytics_channels(db: Any, fr: Frame) -> list[dict]:
    """Each channel's messages and voice time in the period, with how many people each had."""
    table: dict[int, dict] = {}
    for kind in ("messages", "voice"):
        for channel_id, total, people in await top_channels(db, fr, kind):
            row = table.setdefault(channel_id, {"channel_id": channel_id, "messages": 0, "voice": 0, "message_members": 0, "voice_members": 0})
            row[kind] = total
            row["message_members" if kind == "messages" else "voice_members"] = people
    out = list(table.values())
    out.sort(key=lambda row: (-row["messages"], -row["voice"], row["channel_id"]))
    return out


# ---- drilldowns ----

PER_UNIT = {"hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30.436875 * 86400}


def averages(total: int, seconds: float) -> dict[str, float]:
    """A period's total spread over its hours, days, weeks and months."""
    return {unit: (total / (seconds / length) if seconds > 0 else 0.0) for unit, length in PER_UNIT.items()}


async def _last(db: Any, fr: Frame, kind: str, column: str, value: int, other: str) -> tuple[int, int] | None:
    """The newest slot something counted for this person or channel, and the channel or person it was with, under the view's filters."""
    table, _ = TABLE[kind]
    where = query.where(kind, fr.guild_id, fr.filters, None, None, people=False)
    _seen_only(where, kind, fr)
    where.add(f"{column} = ?", value)
    found = await rows(db, f"SELECT slot, {other} FROM {table} WHERE {where.text()} ORDER BY slot DESC LIMIT 1", where.args)
    return (found[0][0], found[0][1]) if found else None


async def member_drilldown(db: Any, fr: Frame, ids: Sequence[int]) -> dict:
    """Several people side by side: when they last spoke and were in voice, and where, their period by channel and over time, their
    averages per hour, day, week and month, and how many people they invited."""
    ids = [user_id for user_id in ids if user_id][:DRILL_LIMIT]
    out: dict[str, Any] = {"members": []}
    if not ids:
        return out
    seconds = fr.end - fr.start
    marks = ", ".join("?" for _ in ids)
    per_channel: dict[int, dict[int, dict]] = {user_id: {} for user_id in ids}
    totals = {user_id: {"messages": 0, "voice": 0} for user_id in ids}
    series = {user_id: {"messages": [0] * len(fr.edges), "voice": [0] * len(fr.edges)} for user_id in ids}
    for kind in ("messages", "voice"):
        table, value = TABLE[kind]
        where = clause(kind, fr)
        where.add(f"user_id IN ({marks})", *ids)
        for user_id, channel_id, total in await rows(db, f"SELECT user_id, channel_id, SUM({value}) FROM {table} WHERE {where.text()} GROUP BY user_id, channel_id", where.args):
            row = per_channel[user_id].setdefault(channel_id, {"channel_id": channel_id, "messages": 0, "voice": 0})
            row[kind] += int(total or 0)
            totals[user_id][kind] += int(total or 0)
        for index, user_id, total in await per_bucket(db, fr, kind, f"SUM({value})", extra=(f"user_id IN ({marks})", *ids), group="user_id"):
            series[user_id][kind][index] += int(total or 0)
    invited = {row["inviter_id"]: row for row in await db.all(
        f"SELECT inviter_id, COUNT(*) AS joins, SUM(CASE WHEN left_at IS NULL THEN 0 ELSE 1 END) AS gone FROM stat_joins "
        f"WHERE guild_id = ? AND at >= ? AND at < ? AND inviter_id IN ({marks}) GROUP BY inviter_id", (fr.guild_id, fr.start, fr.now + SLOT_SECONDS, *ids))}
    for user_id in ids:
        last_message = await _last(db, fr, "messages", "user_id", user_id, "channel_id")
        last_voice = await _last(db, fr, "voice", "user_id", user_id, "channel_id")
        out["members"].append({
            "user_id": user_id, "messages": totals[user_id]["messages"], "voice": totals[user_id]["voice"],
            "last_message": {"at": slot_time(last_message[0]), "channel_id": last_message[1]} if last_message else None,
            "last_voice": {"at": slot_time(last_voice[0]), "channel_id": last_voice[1]} if last_voice else None,
            "channels": sorted(per_channel[user_id].values(), key=lambda row: (-row["messages"], -row["voice"], row["channel_id"])),
            "averages": {"messages": averages(totals[user_id]["messages"], seconds), "voice": averages(totals[user_id]["voice"], seconds)},
            "invited": int((invited.get(user_id) or {}).get("joins") or 0), "invited_left": int((invited.get(user_id) or {}).get("gone") or 0),
            "series": series[user_id],
        })
    return out


async def channel_drilldown(db: Any, fr: Frame, ids: Sequence[int]) -> dict:
    """Several channels side by side: who spoke and was in voice there last and when, their period by person and over time. A category
    covers the channels filed under it."""
    ids = [channel_id for channel_id in ids if channel_id][:DRILL_LIMIT]
    out: dict[str, Any] = {"channels": []}
    if not ids:
        return out
    known = await query.channels(db, fr.guild_id)
    members: dict[int, list[int]] = {}
    for channel_id in ids:
        info = known.get(channel_id)
        if info is not None and info["type"] == 4:
            members[channel_id] = [channel_id, *(row["channel_id"] for row in known.values() if row["parent_id"] == channel_id)]
        else:
            members[channel_id] = [channel_id]
    # A channel can be picked on its own and inside a picked category at once, and then it counts toward both.
    owners: dict[int, list[int]] = defaultdict(list)
    for channel_id, inner_ids in members.items():
        for inner in inner_ids:
            owners[inner].append(channel_id)
    every = list(owners)
    marks = ", ".join("?" for _ in every)
    per_person: dict[int, dict[int, dict]] = {channel_id: {} for channel_id in ids}
    totals = {channel_id: {"messages": 0, "voice": 0} for channel_id in ids}
    series = {channel_id: {"messages": [0] * len(fr.edges), "voice": [0] * len(fr.edges)} for channel_id in ids}
    for kind in ("messages", "voice"):
        table, value = TABLE[kind]
        where = clause(kind, fr)
        where.add(f"channel_id IN ({marks})", *every)
        for channel_id, user_id, total in await rows(db, f"SELECT channel_id, user_id, SUM({value}) FROM {table} WHERE {where.text()} GROUP BY channel_id, user_id", where.args):
            for target in owners[channel_id]:
                totals[target][kind] += int(total or 0)
                if user_id:
                    row = per_person[target].setdefault(user_id, {"user_id": user_id, "messages": 0, "voice": 0})
                    row[kind] += int(total or 0)
        for index, channel_id, total in await per_bucket(db, fr, kind, f"SUM({value})", extra=(f"channel_id IN ({marks})", *every), group="channel_id"):
            for target in owners[channel_id]:
                series[target][kind][index] += int(total or 0)
    seconds = fr.end - fr.start
    for channel_id in ids:
        last_message = last_voice = None
        for inner in members[channel_id]:
            found = await _last(db, fr, "messages", "channel_id", inner, "user_id")
            if found and (last_message is None or found[0] > last_message[0]):
                last_message = found
            found = await _last(db, fr, "voice", "channel_id", inner, "user_id")
            if found and (last_voice is None or found[0] > last_voice[0]):
                last_voice = found
        out["channels"].append({
            "channel_id": channel_id, "messages": totals[channel_id]["messages"], "voice": totals[channel_id]["voice"],
            "last_message": {"at": slot_time(last_message[0]), "user_id": last_message[1] or None} if last_message else None,
            "last_voice": {"at": slot_time(last_voice[0]), "user_id": last_voice[1] or None} if last_voice else None,
            "members": sorted(per_person[channel_id].values(), key=lambda row: (-row["messages"], -row["voice"], row["user_id"]))[:BREAKDOWN_LIMIT],
            "members_total": len(per_person[channel_id]),
            "averages": {"messages": averages(totals[channel_id]["messages"], seconds), "voice": averages(totals[channel_id]["voice"], seconds)},
            "series": series[channel_id],
        })
    return out


# ---- personal panels ----

PANEL_STATS = ("members", "messages", "voice")
PANEL_VALUES = ("count", "change", "sum", "average", "unique")
PANEL_LIMIT = 12  # panels one person keeps in one server


def clean_panel(value: Any) -> dict:
    """One panel a person built, checked: {"title", "stat", "value", "unique", "percent", "period", "interval"}. Raises ValueError with the sentence to show."""
    if not isinstance(value, dict):
        raise ValueError("Each panel needs a stat and a value.")
    stat = value.get("stat")
    kind = value.get("value")
    if stat not in PANEL_STATS:
        raise ValueError("Pick members, messages or voice.")
    if kind not in PANEL_VALUES:
        raise ValueError("Pick count, change, sum, average or unique.")
    if kind == "unique" and stat == "members":
        raise ValueError("Unique counts people or channels with messages or voice time. Pick messages or voice.")
    unique = value.get("unique", "members")
    if unique not in ("members", "channels"):
        raise ValueError("Count unique members or channels.")
    period = _period_text(str(value.get("period") or "7d"))
    if not period:
        raise ValueError("Give the panel a period such as 7d, 4w, 3mo, since:2025-03-01 or all.")
    interval = value.get("interval") or "day"
    if interval not in INTERVALS:
        raise ValueError("Pick hour, day, week or month.")
    title = str(value.get("title") or "").strip()[:60]
    return {"title": title, "stat": stat, "value": kind, "unique": unique, "percent": bool(value.get("percent")) and kind in ("change", "unique"),
            "period": period, "interval": interval}


def clean_panels(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("Panels come as a list.")
    if len(value) > PANEL_LIMIT:
        raise ValueError(f"Keep it to {PANEL_LIMIT} panels.")
    return [clean_panel(item) for item in value]


async def panel_value(db: Any, guild_id: int, cfg: dict, panel: dict, now: float, role_holders: RoleHolders, channels_known: int,
                      visible: frozenset[int] | None = None) -> dict:
    """A panel's number over its own period, read under the server's own filters, and the series behind it."""
    view = read_view({"period": panel["period"], "interval": panel["interval"]}, cfg)
    kinds = ("members",) if panel["stat"] == "members" else (panel["stat"],)
    fr = await frame_for(db, guild_id, cfg, view, now, role_holders, kinds, visible)
    if panel["stat"] == "members":
        counted = await member_series(db, fr)
        members = counted["members"]
        known = [value for value in members if value is not None]
        series = [value if value is not None else 0 for value in members]
        latest = known[-1] if known else None
        first = known[0] if known else None
        if panel["value"] == "change":
            number = (latest - first) if latest is not None and first is not None else None
            if panel["percent"] and number is not None:
                number = (number / first * 100) if first else None
        elif panel["value"] == "average":
            number = sum(known) / len(known) if known else None
        elif panel["value"] == "sum":
            number = sum(counted["joins"])  # every join in the period, someone who came back counted each time
            series = counted["joins"]
        else:
            number = latest
        return {"number": number, "series": series, "label": fr.label}
    # Only what the panel shows, so a person's panels over all time cost a fraction of the Messages page they summarize.
    kind = panel["stat"]
    _, column = TABLE[kind]
    size = len(fr.edges)
    series: list = [0] * size
    if panel["value"] == "unique":
        by = "user_id" if panel["unique"] == "members" else "channel_id"
        for index, found in await per_bucket(db, fr, kind, "COUNT(DISTINCT NULLIF(user_id, 0))" if by == "user_id" else "COUNT(DISTINCT channel_id)"):
            series[index] = found
        where = clause(kind, fr)
        if by == "user_id":
            where.add("user_id != 0")
        number: Any = (await rows(db, f"SELECT COUNT(*) FROM (SELECT {by} FROM {source(kind, by, fr)} WHERE {where.text()} GROUP BY {by})", where.args))[0][0]
        if panel["percent"]:
            if by == "user_id":
                count = await query.member_count_at(db, guild_id, fr.end_slot)
                number = (number / count * 100) if count else None
            else:
                number = (number / channels_known * 100) if channels_known else None
        return {"number": number, "series": series, "label": fr.label}
    for index, total in await per_bucket(db, fr, kind, f"SUM({column})"):
        series[index] = int(total or 0)
    total = sum(series)  # the buckets cover the period exactly
    if panel["value"] == "count":
        number = series[-1] if series else 0
    elif panel["value"] == "sum":
        number = total
    elif panel["value"] == "average":
        number = total / size if size else 0
    else:
        length = fr.end_slot - fr.start_slot
        table, _ = TABLE[kind]
        where = clause(kind, fr, fr.start_slot - length, fr.start_slot)
        before = (await rows(db, f"SELECT COALESCE(SUM({column}), 0) FROM {table} WHERE {where.text()}", where.args))[0][0]
        number = total - before
        if panel["percent"]:
            number = (number / before * 100) if before else None
    return {"number": number, "series": series, "label": fr.label}


# ---- remembering answers ----

MISSING = object()
CACHE_BYTES = 128 * 2 ** 20  # about what the answers kept may hold in all. An all-time ranking of 40,000 people is about five MiB.


def weight_of(value: Any, depth: int = 0) -> int:
    """About how many bytes an answer holds, objects inside it included. A long list is weighed by an even sample of 32 of its items, so
    weighing a ranking of 40,000 people costs no more than weighing a short one."""
    if depth > 8:
        return sys.getsizeof(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value) if isinstance(value, (set, frozenset)) else value
        if not items:
            return sys.getsizeof(value)
        sample = items if len(items) <= 32 else [items[index * len(items) // 32] for index in range(32)]
        return sys.getsizeof(value) + sum(weight_of(item, depth + 1) for item in sample) * len(items) // len(sample)
    if isinstance(value, dict):
        if not value:
            return sys.getsizeof(value)
        pairs = list(value.items())
        sample = pairs if len(pairs) <= 32 else [pairs[index * len(pairs) // 32] for index in range(32)]
        return sys.getsizeof(value) + sum(weight_of(key, depth + 1) + weight_of(item, depth + 1) for key, item in sample) * len(pairs) // len(sample)
    if isinstance(value, (str, bytes, int, float, bool)) or value is None:
        return sys.getsizeof(value)
    inside = getattr(value, "__dict__", None)
    return sys.getsizeof(value) + (weight_of(inside, depth + 1) if isinstance(inside, dict) else 0)


class Cache:
    """Answers kept for a minute per server and question, so a page that several people open, or one opened again, reads the counts once.
    Two asks for the same answer at once share one read, which carries on for the others when one of them leaves. What expired goes at
    the next answer kept, and what's kept stays within `budget` bytes, the oldest leaving first. An answer over a quarter of that is
    read each time it's asked for."""

    def __init__(self, seconds: float = 60.0, size: int = 400, budget: int = CACHE_BYTES) -> None:
        self.seconds = seconds
        self.size = size
        self.budget = budget
        self.kept: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self.weights: dict[tuple, int] = {}
        self.held = 0
        self.running: dict[tuple, asyncio.Future] = {}

    async def get(self, key: tuple, work: Callable[[], Awaitable[Any]]) -> Any:
        found = self.kept.get(key)
        if found is not None and found[0] > time.monotonic():
            self.kept.move_to_end(key)
            return found[1]
        reading = self.running.get(key)
        if reading is None:
            reading = self.running[key] = asyncio.ensure_future(self._read(key, work))
            reading.add_done_callback(_retrieved)
        return await asyncio.shield(reading)

    async def _read(self, key: tuple, work: Callable[[], Awaitable[Any]]) -> Any:
        try:
            value = await work()
        finally:
            self.running.pop(key, None)
        self._keep(key, value)
        return value

    def _keep(self, key: tuple, value: Any) -> None:
        now = time.monotonic()
        for old in [old for old, (until, _) in self.kept.items() if until <= now]:
            self._drop(old)
        weight = weight_of(value)
        if weight > self.budget // 4:
            self._drop(key)
            return
        self._drop(key)
        self.kept[key] = (now + self.seconds, value)
        self.weights[key] = weight
        self.held += weight
        while self.kept and (len(self.kept) > self.size or self.held > self.budget):
            self._drop(next(iter(self.kept)))

    def _drop(self, key: tuple) -> None:
        if self.kept.pop(key, None) is not None:
            self.held -= self.weights.pop(key, 0)

    def peek(self, key: tuple) -> Any:
        """The answer kept for a key, or MISSING when there's none from the last minute."""
        found = self.kept.get(key)
        return found[1] if found is not None and found[0] > time.monotonic() else MISSING

    def forget(self, guild_id: int) -> None:
        for key in [key for key in self.kept if key[0] == guild_id]:
            self._drop(key)


def _retrieved(reading: asyncio.Future) -> None:
    """Reads a shared read's failure, so one whose askers all left isn't reported as never retrieved."""
    if not reading.cancelled():
        reading.exception()
