"""Stat roles: a role for the members whose statistics pass a rule's conditions, given and taken on a schedule.

A rule's conditions are a tree. A group passes when all of its conditions pass, or any of them. A condition reads one statistic per member
(messages, voice minutes, minutes in apps, people invited, or emoji used or received) and tests it with a limit:
- threshold: from a minimum to an optional maximum over the last N hours or days, since a date, or all time. Every bound counts.
- top: a place in the ranking, by rank or by percentile, among the members the rule applies to (people, bots or both), or among those of
  them who pass the condition's own filters. Someone with nothing in the period has no place.
- days: at least N on M of the last K days, days ending at midnight in the server's zone. A day still running counts when it already
  reaches N, and a rule passed yesterday keeps passing until today's tally is in.
Joined age and account age compare a date with now. Compare weighs two lists of statistics against each other. A stat role condition
passes for whoever another rule's conditions pass, read fresh in the same run, with none of that rule's ignored members or permanence.

Each condition can name channels (categories cover their channels) and the members and roles it applies to. Someone a member or role filter
leaves out fails the condition. Inverting flips pass and fail as the very last step.

Schedules are five-field cron lines read in the server's time zone, as cronie reads them: minute, hour, day of the month, month, day of the
week, with lists, ranges, steps, three-letter names, Sunday as 0 or 7, and the @hourly, @daily, @weekly, @monthly and @yearly words. When
both day fields name days, either one is enough. When either starts with *, both must match. A line must be at least ten minutes between
runs on the clock, and a rule never starts a scheduled run within ten minutes of its last start, which a clock change could otherwise bring.

An age condition sits in a group that matches all of its conditions, beside a condition that reads statistics, so a stat role is never
handed out on age alone.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from ..validate import Invalid, number, snowflake, text, whole_number
from . import counters, invites as invite_rules, store
from .timeframe import MAX_DAYS, SLOT_SECONDS, slot_of

# ---- limits ----

MAX_NODES = 50
MAX_DEPTH = 6
MAX_COMPARE_ITEMS = 10
MAX_DAY_RANGE = 366
MAX_IGNORED = 1000
NAME_LIMIT = 100
MIN_GAP_MINUTES = 10
SEARCH_DAYS = 5 * 366  # a schedule that doesn't run within this many days never runs
LEAF_KINDS = {"message": "messages", "voice": "voice", "activity": "activities", "invite": "invites", "emoji": "emoji"}
UNITS = {"message": "messages", "voice": "minutes", "activity": "minutes", "invite": "people invited", "emoji": "uses"}
AGE_TYPES = ("joined", "account")
TYPES = (*LEAF_KINDS, *AGE_TYPES, "compare", "statrole", "match")
PRESETS = {"10m": "*/10 * * * *", "hourly": "0 * * * *", "3h": "0 */3 * * *", "daily": "0 0 * * *", "monthly": "0 0 1 * *", "fridays": "0 0 * * 5"}
PRESET_LABELS = {"10m": "Every 10 minutes", "hourly": "Every hour", "3h": "Every 3 hours", "daily": "Every day at midnight", "monthly": "On the 1st of each month",
                 "fridays": "Fridays at midnight", "custom": "Custom"}
DEFAULT_SCHEDULE = {"preset": "3h", "cron": PRESETS["3h"]}
SEND_MODES = ("channel", "dm", "dm_fallback")
DEFAULT_GIVE = "{mention} now has the {role(name)} role."
DEFAULT_REMOVE = "{mention} no longer has the {role(name)} role."


# ---- cron ----

MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
ALIASES = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0", "@daily": "0 0 * * *",
           "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}


@dataclass(frozen=True)
class Cron:
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # Sunday 0
    day_star: bool
    weekday_star: bool
    fixed_time: bool = False  # neither the minute nor the hour field starts with *

    def runs_on(self, day: dt.date) -> bool:
        if day.month not in self.months:
            return False
        on_day = day.day in self.days
        on_weekday = (day.weekday() + 1) % 7 in self.weekdays
        if self.day_star or self.weekday_star:
            return on_day and on_weekday
        return on_day or on_weekday

    def minutes_of_day(self) -> list[int]:
        return sorted(hour * 60 + minute for hour in self.hours for minute in self.minutes)


def _cron_number(word: str, low: int, names: tuple[str, ...] | None, where: str) -> int:
    if re.fullmatch(r"\d{1,2}", word):
        return int(word)
    if names and word.lower() in names:
        return names.index(word.lower()) + low
    raise Invalid(f"The {where} field can't read {word}.")


def _cron_field(part: str, low: int, high: int, names: tuple[str, ...] | None, where: str) -> set[int]:
    found: set[int] = set()
    for piece in part.split(","):
        if not piece:
            raise Invalid(f"The {where} field has an empty entry.")
        body, _, step_text = piece.partition("/")
        step = 1
        if step_text:
            if not re.fullmatch(r"\d{1,3}", step_text) or int(step_text) == 0:
                raise Invalid(f"The {where} field's step must be a whole number above 0.")
            step = int(step_text)
        if body == "*":
            start, end = low, high
        elif "-" in body:
            first, _, last = body.partition("-")
            start, end = _cron_number(first, low, names, where), _cron_number(last, low, names, where)
            if where == "day of the week" and end == 0 and start > 0:
                end = 7  # 5-0 means Friday to Sunday
            if start > end:
                raise Invalid(f"The {where} field's range {body} runs backward.")
        else:
            start = _cron_number(body, low, names, where)
            end = high if step_text else start  # 10/15 counts from 10 to the end
        if not low <= start <= high or not low <= end <= high:
            raise Invalid(f"The {where} field takes {low} to {high}.")
        found.update(range(start, end + 1, step))
    return found


def parse_cron(line: Any) -> Cron:
    """A five-field cron line. Raises Invalid with the sentence to show."""
    if not isinstance(line, str) or not line.strip():
        raise Invalid("Write a schedule of five fields: minute, hour, day of the month, month and day of the week.")
    cleaned = " ".join(line.split())
    cleaned = ALIASES.get(cleaned.lower(), cleaned)
    parts = cleaned.split(" ")
    if len(parts) != 5:
        raise Invalid("A schedule has five fields: minute, hour, day of the month, month and day of the week.")
    minutes = _cron_field(parts[0], 0, 59, None, "minute")
    hours = _cron_field(parts[1], 0, 23, None, "hour")
    days = _cron_field(parts[2], 1, 31, None, "day of the month")
    months = _cron_field(parts[3], 1, 12, MONTH_NAMES, "month")
    weekdays = _cron_field(parts[4], 0, 7, DAY_NAMES, "day of the week")
    if 7 in weekdays or 0 in weekdays:
        weekdays |= {0, 7}
    return Cron(tuple(sorted(minutes)), tuple(sorted(hours)), frozenset(days), frozenset(months), frozenset(day % 7 for day in weekdays),
                parts[2].startswith("*"), parts[4].startswith("*"), not parts[0].startswith("*") and not parts[1].startswith("*"))


def _local_moments(day: dt.date, minute_of_day: int, tz: dt.tzinfo, *, fixed_time: bool) -> list[float]:
    """When a wall-clock time on a day happens in a zone, as cronie runs it through a clock change (src/cron.c, its main loop). A time the
    clock skips runs as the clock jumps past it for a fixed-time schedule, and not at all for one with * in its minute or hour. A time that
    happens twice runs the first time for a fixed-time schedule, and both times for one with *, which cronie keeps running through the
    repeated hour."""
    wall = dt.datetime(day.year, day.month, day.day, minute_of_day // 60, minute_of_day % 60, tzinfo=tz)
    moment = wall.timestamp()
    back = dt.datetime.fromtimestamp(moment, tz)
    if (back.hour, back.minute) == (wall.hour, wall.minute):
        again = wall.replace(fold=1).timestamp()
        return [moment, again] if not fixed_time and again != moment else [moment]
    if not fixed_time:
        return []
    # The later offset reads the skipped time as before the jump. The jump is the first minute after that whose wall clock is past it.
    early = int(wall.replace(fold=1).timestamp()) // 60 * 60
    target = (wall.hour, wall.minute)
    for step in range(0, 6 * 60):
        probe = early + step * 60
        local = dt.datetime.fromtimestamp(probe, tz)
        if local.date() > day or (local.hour, local.minute) > target:
            return [float(probe)]
    return []


def cron_next(cron: Cron, after: float, tz: dt.tzinfo) -> float | None:
    """The first time the schedule runs after `after`, or None when it doesn't within five years. Clock changes work as cronie handles
    them: see _local_moments."""
    day = dt.datetime.fromtimestamp(after, tz).date()
    times = cron.minutes_of_day()
    for _ in range(SEARCH_DAYS):
        if cron.runs_on(day):
            found = [moment for minute in times for moment in _local_moments(day, minute, tz, fixed_time=cron.fixed_time) if moment > after]
            if found:
                return min(found)
        day += dt.timedelta(days=1)
    return None


def cron_gap(cron: Cron) -> int | None:
    """The shortest wait in minutes between two runs, or None when it never runs."""
    times = cron.minutes_of_day()
    start = dt.date(2026, 1, 1)
    days = [start + dt.timedelta(days=offset) for offset in range(8 * 366)]  # every pairing of dates and weekdays, leap days included
    running = [cron.runs_on(day) for day in days]
    if not any(running):
        return None
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    if any(first and second for first, second in zip(running, running[1:])):
        gaps.append(times[0] + 1440 - times[-1])
    if len(times) == 1 and not gaps:
        return 1440
    return min(gaps) if gaps else 1440


def clean_schedule(value: Any) -> dict:
    """{"preset": one of PRESETS or "custom", "cron": the line}. A custom line must not run more often than every ten minutes."""
    if value in (None, ""):
        return dict(DEFAULT_SCHEDULE)
    if not isinstance(value, dict):
        raise Invalid("Pick a schedule.")
    preset = value.get("preset", "custom")
    if preset in PRESETS:
        return {"preset": preset, "cron": PRESETS[preset]}
    if preset != "custom":
        raise Invalid("Pick a schedule from the list.")
    line = " ".join(str(value.get("cron") or "").split())
    cron = parse_cron(line)
    gap = cron_gap(cron)
    if gap is None:
        raise Invalid("That schedule never runs.")
    if gap < MIN_GAP_MINUTES:
        raise Invalid(f"A schedule can run at most every {MIN_GAP_MINUTES} minutes. This one runs {gap} minute{'s' if gap != 1 else ''} apart.")
    return {"preset": "custom", "cron": line}


def next_run(schedule: dict, after: float, tz: dt.tzinfo) -> float | None:
    return cron_next(parse_cron(schedule["cron"]), after, tz)


# ---- checking a rule ----

def _limit(value: Any, kind: str) -> dict:
    if not isinstance(value, dict):
        raise Invalid("Pick a limit type.")
    style = value.get("kind")
    if style == "threshold":
        low = number(0, 10 ** 12)(value.get("min", 1) if value.get("min") is not None else 0)
        high = value.get("max")
        high = None if high in (None, "") else number(0, 10 ** 12)(high)
        if high is not None and high < low:
            raise Invalid("The maximum can't be below the minimum.")
        if low == 0 and high is None:
            raise Invalid("A minimum of 0 needs a maximum, or everyone passes.")
        return {"kind": "threshold", "min": low, "max": high, "period": counters.clean_period(value.get("period") or {"amount": 30, "unit": "days"})}
    if style == "top":
        by = value.get("by", "rank")
        if by not in ("rank", "percent"):
            raise Invalid("Rank by place or by percentile.")
        period = counters.clean_period(value.get("period") or {"amount": 30, "unit": "days"})
        if by == "rank":
            first = whole_number(value.get("from", 1))
            last = whole_number(value.get("to", 10))
            if not first or not last or first > last or last > 100_000:
                raise Invalid("Give places from 1 up, the first no later than the last.")
        else:
            first = number(0, 100)(value.get("from", 0))
            last = number(0, 100)(value.get("to", 10))
            if first >= last:
                raise Invalid("The top percentile has to come before the bottom one, as in 0 to 10.")
        return {"kind": "top", "by": by, "from": first, "to": last, "matching_only": bool(value.get("matching_only")), "period": period}
    if style == "days":
        per_day = number(0, 10 ** 9)(value.get("per_day", 1))
        if per_day <= 0:
            raise Invalid("The amount each day has to be above 0.")
        of = whole_number(value.get("of", 7))
        days = whole_number(value.get("days", of))
        if not of or of > MAX_DAY_RANGE:
            raise Invalid(f"Look back from 1 to {MAX_DAY_RANGE} days.")
        if not days or days > of:
            raise Invalid("The number of days needed can't be more than the days looked at.")
        return {"kind": "days", "per_day": per_day, "days": days, "of": of}
    raise Invalid("Pick threshold, top or days.")


def _people(value: dict) -> dict:
    """The member and role filters a condition applies to whoever it tests."""
    mode = value.get("member_mode", "block")
    if mode not in ("allow", "block"):
        raise Invalid("Pick allow or block for the members.")
    return {
        "member_mode": mode,
        "members": counters._ids(value.get("members"), "Members", MAX_IGNORED),
        "roles_allow": counters._ids(value.get("roles_allow"), "Roles needed"),
        "roles_block": counters._ids(value.get("roles_block"), "Roles that fail it"),
    }


def _stat_part(value: dict) -> dict:
    """A condition's statistic filters, which a saved rule keeps under "stat" and a form can also send beside the rest."""
    return value["stat"] if isinstance(value.get("stat"), dict) else {}


def _compare_item(value: Any) -> dict:
    if not isinstance(value, dict):
        raise Invalid("Each item to compare needs a statistic.")
    kind = value.get("kind")
    if kind not in ("message", "voice", "activity"):
        raise Invalid("Compare messages, voice minutes or minutes in apps.")
    stat = counters.clean_stat({**value, **_stat_part(value), "kind": LEAF_KINDS[kind], "include": "both"}, kinds=("messages", "voice", "activities"), people=False)
    weight = number(-10 ** 6, 10 ** 6)(value.get("weight", 1))
    return {"kind": kind, "stat": stat, "weight": weight, "subtract": bool(value.get("subtract"))}


def clean_condition(value: Any, depth: int = 1, count: list[int] | None = None) -> dict:
    """One condition or group, with its children checked. Raises Invalid with the sentence to show."""
    count = count if count is not None else [0]
    count[0] += 1
    if count[0] > MAX_NODES:
        raise Invalid(f"A rule can hold at most {MAX_NODES} conditions and groups.")
    if depth > MAX_DEPTH:
        raise Invalid(f"Groups can go at most {MAX_DEPTH} deep.")
    if not isinstance(value, dict) or value.get("type") not in TYPES:
        raise Invalid("Pick a condition type.")
    kind = value["type"]
    invert = bool(value.get("invert"))
    if kind == "match":
        mode = value.get("mode", "all")
        if mode not in ("all", "any"):
            raise Invalid("A group matches all of its conditions or any of them.")
        children = value.get("conditions")
        if not isinstance(children, list) or not children:
            raise Invalid("A group needs at least one condition.")
        return {"type": "match", "mode": mode, "conditions": [clean_condition(child, depth + 1, count) for child in children], "invert": invert}
    if kind == "statrole":
        target = whole_number(value.get("rule"))
        if not target:
            raise Invalid("Pick the stat role this condition follows.")
        return {"type": "statrole", "rule": target, "invert": invert}
    out: dict[str, Any] = {"type": kind, "invert": invert, **_people(value)}
    if kind in AGE_TYPES:
        compare = value.get("compare", "more")
        if compare not in ("more", "less", "before", "after"):
            raise Invalid("Pick more than, less than, before or on and after.")
        out["compare"] = compare
        if compare in ("more", "less"):
            unit = value.get("unit", "days")
            if unit not in ("days", "hours"):
                raise Invalid("Count the age in days or hours.")
            most = MAX_DAYS * (24 if unit == "hours" else 1)
            amount = whole_number(value.get("amount"), most)
            if amount is None:
                raise Invalid(f"Give an age from 0 to {most:,} {unit}.")
            out.update(amount=amount, unit=unit)
        else:
            try:
                out["date"] = dt.date.fromisoformat(str(value.get("date") or "").strip()).isoformat()
            except ValueError as error:
                raise Invalid("Give the date as YYYY-MM-DD.") from error
        return out
    if kind == "compare":
        left = value.get("left") or []
        right = value.get("right") or []
        if not isinstance(left, list) or not isinstance(right, list) or len(left) > MAX_COMPARE_ITEMS or len(right) > MAX_COMPARE_ITEMS:
            raise Invalid(f"Each side compares at most {MAX_COMPARE_ITEMS} statistics.")
        if not left and not right:
            raise Invalid("Add a statistic to at least one side.")
        operator = value.get("operator", "greater")
        if operator not in ("less", "greater"):
            raise Invalid("Pick less than or greater than.")
        out.update(left=[_compare_item(item) for item in left], right=[_compare_item(item) for item in right], operator=operator)
        return out
    stat = counters.clean_stat({**value, **_stat_part(value), "kind": LEAF_KINDS[kind], "include": "both", "period": {"all": True}}, kinds=(LEAF_KINDS[kind],), people=False)
    stat.pop("period", None)
    stat.pop("include", None)
    out["stat"] = stat
    out["limit"] = _limit(value.get("limit"), kind)
    return out


def _leaves(node: dict) -> Iterable[dict]:
    if node["type"] == "match":
        for child in node["conditions"]:
            yield from _leaves(child)
    else:
        yield node


def _reads_statistics(node: dict) -> bool:
    if node["type"] == "match":
        return any(_reads_statistics(child) for child in node["conditions"])
    return node["type"] in LEAF_KINDS or node["type"] in ("compare", "statrole")


def _ages_beside_statistics(node: dict) -> None:
    """Refuses an age condition that isn't in a group matching all of its conditions beside one that reads statistics."""
    if node["type"] != "match":
        return
    for child in node["conditions"]:
        if child["type"] in AGE_TYPES and (node["mode"] != "all" or not any(_reads_statistics(other) for other in node["conditions"] if other is not child)):
            raise Invalid("An age condition goes in a group that matches all of its conditions, beside a condition on messages, voice, apps, invites, "
                          "emoji, a comparison or another stat role, so a stat role is never given for age alone.")
        _ages_beside_statistics(child)


def clean_notification(value: Any, default_text: str) -> dict | None:
    """A message for each member who gains or loses the role, or None for no message. The message is the Embeds feature's format, and its
    blocks are the template language's, so {mention}, {user} and {role(name)} fill in."""
    from ..messages import clean_payload, has_content
    if value in (None, "") or (isinstance(value, dict) and not value.get("on", True)):
        return None
    if not isinstance(value, dict):
        raise Invalid("A notification needs a message and where it goes.")
    payload = clean_payload(value.get("message") or {"content": default_text}, templates=True)
    if not has_content(payload):
        raise Invalid("Write the notification's message, or switch the notification off.")
    send = value.get("send", "channel")
    if send not in SEND_MODES:
        raise Invalid("Send it to a channel, by DM, or by DM with a channel when DMs are closed.")
    channel_id = value.get("channel_id")
    channel_id = None if channel_id in (None, "") else snowflake(channel_id)
    if send in ("channel", "dm_fallback") and channel_id is None:
        raise Invalid("Pick the channel the notification goes to." if send == "channel" else "Pick the channel used when a DM can't be sent.")
    group = bool(value.get("group")) and send == "channel"
    return {"on": True, "message": payload, "send": send, "channel_id": channel_id, "suppress": bool(value.get("suppress")), "group": group}


def clean_rule(value: Any) -> dict:
    """A whole rule's settings, besides its name and role, which the table holds. Raises Invalid with the sentence to show."""
    if not isinstance(value, dict):
        raise Invalid("A stat role needs its conditions.")
    root = value.get("conditions")
    if isinstance(root, list):
        root = {"type": "match", "mode": "all", "conditions": root}
    if not isinstance(root, dict):
        raise Invalid("Add at least one condition.")
    if root.get("type") != "match":
        root = {"type": "match", "mode": "all", "conditions": [root]}
    cleaned = clean_condition(root)
    if not any(leaf["type"] != "match" for leaf in _leaves(cleaned)):
        raise Invalid("Add at least one condition that isn't a group.")
    _ages_beside_statistics(cleaned)
    apply_to = value.get("apply_to", "humans")
    if apply_to not in ("humans", "bots", "both"):
        raise Invalid("Apply it to humans, bots or both.")
    ignore = counters._ids(value.get("ignore"), "Ignored members", MAX_IGNORED)
    return {
        "conditions": cleaned,
        "apply_to": apply_to,
        "ignore": ignore,
        "permanent": bool(value.get("permanent")),
        "schedule": clean_schedule(value.get("schedule")),
        "give": clean_notification(value.get("give"), DEFAULT_GIVE),
        "remove": clean_notification(value.get("remove"), DEFAULT_REMOVE),
    }


def followed_rules(rule: dict) -> set[int]:
    """The other rules a rule's stat role conditions read."""
    return {leaf["rule"] for leaf in _leaves(rule["conditions"]) if leaf["type"] == "statrole"}


def missing_rules(rule: dict, rules: dict[int, dict]) -> set[int]:
    """The stat roles a rule follows, directly or through others, that aren't in `rules`: deleted, or stored in a way that can't be read."""
    missing: set[int] = set()
    seen: set[int] = set()
    waiting = list(followed_rules(rule))
    while waiting:
        target = waiting.pop()
        if target in seen:
            continue
        seen.add(target)
        if target in rules:
            waiting.extend(followed_rules(rules[target]))
        else:
            missing.add(target)
    return missing


def loop_through(number_: int, rules: dict[int, dict]) -> list[int] | None:
    """The chain of rules leading back to this one, when its stat role conditions loop. None when they don't."""
    def walk(current: int, path: list[int]) -> list[int] | None:
        for target in sorted(followed_rules(rules[current])) if current in rules else []:
            if target == number_:
                return [*path, target]
            if target in path:
                continue
            found = walk(target, [*path, target])
            if found:
                return found
        return None
    return walk(number_, [number_])


def channels_named(rule: dict) -> set[int]:
    """Every channel a rule's notifications post in."""
    found = set()
    for key in ("give", "remove"):
        note = rule.get(key)
        if note and note.get("channel_id"):
            found.add(note["channel_id"])
    return found


# ---- who the rule looks at ----

@dataclass(frozen=True)
class Person:
    id: int
    bot: bool
    roles: frozenset[int]
    joined_at: float | None
    created_at: float | None
    pending: bool = False


def person_from(member: Any) -> Person:
    joined = getattr(member, "joined_at", None)
    created = getattr(member, "created_at", None)
    raw = getattr(member, "_roles", None)
    roles = frozenset(raw) if raw is not None else frozenset(role.id for role in getattr(member, "roles", []))
    return Person(member.id, bool(member.bot), roles, joined.timestamp() if joined else None, created.timestamp() if created else None,
                  bool(getattr(member, "pending", False)))


@dataclass
class Snapshot:
    """The server's members as they were when a run began, which every condition reads."""
    guild_id: int
    now: float
    tz: ZoneInfo
    people: dict[int, Person]
    private: frozenset[int] = frozenset()
    settings: dict = field(default_factory=dict)
    guild: Any = None


@dataclass
class Result:
    """Who passes one condition, and the value each member reached for it."""
    passing: set[int]
    values: dict[int, float] = field(default_factory=dict)
    note: str | None = None


@dataclass
class Evaluation:
    passing: set[int]
    results: dict[str, Result]  # by the condition's place in the tree, as "0", "0.1" and so on
    rules: dict[int, set[int]] = field(default_factory=dict)  # other rules read along the way


def _filter_ok(node: dict, person: Person) -> bool:
    members = node.get("members") or []
    if members:
        listed = person.id in members
        if (node.get("member_mode") == "allow") != listed:
            return False
    allow = node.get("roles_allow") or []
    if allow and not person.roles & set(allow):
        return False
    block = node.get("roles_block") or []
    if block and person.roles & set(block):
        return False
    return True


def _rank_pass(values: dict[int, float], candidates: Iterable[int], limit: dict, population: int) -> set[int]:
    """Who sits within the places or percentiles asked for. Equal values share the best place among them, and nobody with nothing is placed.
    A percentile range takes its end and leaves its start to the range before it, so 0 to 10 and 10 to 20 never share a member."""
    ranked = sorted(((values[person], person) for person in candidates if values.get(person, 0) > 0), reverse=True)
    passing: set[int] = set()
    place = 0
    previous = None
    for position, (value, person) in enumerate(ranked, 1):
        if value != previous:
            place = position
            previous = value
        if limit["by"] == "rank":
            if limit["from"] <= place <= limit["to"]:
                passing.add(person)
            elif place > limit["to"]:
                break
        else:
            share = place / max(population, 1) * 100
            if limit["from"] < share <= limit["to"]:
                passing.add(person)
            elif share > limit["to"]:
                break
    return passing


def day_edges(now: float, tz: ZoneInfo, days: int) -> list[int]:
    """The slots the last `days` days start at, oldest first, today last."""
    today = dt.datetime.fromtimestamp(now, tz).date()
    edges = []
    for back in range(days, -1, -1):
        day = today - dt.timedelta(days=back)
        edges.append(slot_of(dt.datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()))
    return edges


class Evaluator:
    """Works out who passes a rule, reading each statistic once per run however many conditions and rules ask for it."""

    def __init__(self, db: Any, snapshot: Snapshot, rules: dict[int, dict]) -> None:
        self.db = db
        self.snap = snapshot
        self.rules = rules
        self._sums: dict[str, dict[int, float]] = {}
        self._daily: dict[str, dict[int, list[float]]] = {}
        self._rule_cache: dict[int, set[int]] = {}
        self._rule_stack: list[int] = []
        self._applies: list[str] = ["both"]  # whom the rule being worked out applies to: humans, bots or both

    @property
    def population(self) -> set[int]:
        return set(self.snap.people)

    def _ranked(self) -> list[int]:
        """The members a top limit places: the kind the rule applies to, so a rule for people never hands a place to a bot."""
        kind = self._applies[-1]
        return [person.id for person in self.snap.people.values() if kind == "both" or (kind == "bots") == person.bot]

    async def evaluate(self, rule: dict) -> Evaluation:
        results: dict[str, Result] = {}
        self._applies.append(rule.get("apply_to", "humans"))
        try:
            passing = await self._node(rule["conditions"], "0", results)
        finally:
            self._applies.pop()
        return Evaluation(passing, results, dict(self._rule_cache))

    async def _node(self, node: dict, place: str, results: dict[str, Result] | None) -> set[int]:
        kind = node["type"]
        if kind == "match":
            sets = [await self._node(child, f"{place}.{index}", results) for index, child in enumerate(node["conditions"])]
            passing = set.intersection(*sets) if node["mode"] == "all" else set().union(*sets)
            result = Result(passing)
        elif kind == "statrole":
            result = await self._statrole(node)
        elif kind in AGE_TYPES:
            result = self._age(node)
        elif kind == "compare":
            result = await self._compare(node)
        else:
            result = await self._stat(node)
        passing = result.passing
        if node.get("invert"):
            passing = self.population - passing
        if results is not None:
            results[place] = Result(passing, result.values, result.note)
        return passing

    async def _statrole(self, node: dict) -> Result:
        target = node["rule"]
        if target in self._rule_cache:
            return Result(set(self._rule_cache[target]))
        rule = self.rules.get(target)
        if rule is None or target in self._rule_stack:
            return Result(set(), note="That stat role is gone." if rule is None else "These stat roles follow each other in a loop.")
        self._rule_stack.append(target)
        self._applies.append(rule.get("apply_to", "humans"))
        try:
            passing = await self._node(rule["conditions"], "x", None)
        finally:
            self._rule_stack.pop()
            self._applies.pop()
        self._rule_cache[target] = passing
        return Result(set(passing))

    def _age(self, node: dict) -> Result:
        now = self.snap.now
        passing: set[int] = set()
        values: dict[int, float] = {}
        cutoff = None
        if node["compare"] in ("before", "after"):
            day = dt.date.fromisoformat(node["date"])
            cutoff = dt.datetime(day.year, day.month, day.day, tzinfo=self.snap.tz).timestamp()
        size = 3600 if node.get("unit") == "hours" else 86400
        for person in self.snap.people.values():
            moment = person.joined_at if node["type"] == "joined" else person.created_at
            if moment is None or not _filter_ok(node, person):
                continue
            age = (now - moment) / size
            values[person.id] = age
            compare = node["compare"]
            if (compare == "more" and age > node["amount"]) or (compare == "less" and age < node["amount"]) \
                    or (compare == "before" and moment < cutoff) or (compare == "after" and moment >= cutoff):
                passing.add(person.id)
        return Result(passing, values)

    async def _compare(self, node: dict) -> Result:
        totals = {"left": {}, "right": {}}
        for side in ("left", "right"):
            for item in node[side]:
                values = await self.sums(item["stat"])
                weight = item["weight"] * (-1 if item["subtract"] else 1)
                scale = 60 if item["kind"] in ("voice", "activity") else 1
                for person, value in values.items():
                    totals[side][person] = totals[side].get(person, 0.0) + weight * value / scale
        passing = set()
        values = {}
        for person in self.snap.people.values():
            left = totals["left"].get(person.id, 0.0)
            right = totals["right"].get(person.id, 0.0)
            values[person.id] = left - right
            if not _filter_ok(node, person):
                continue
            if (node["operator"] == "less" and left < right) or (node["operator"] == "greater" and left > right):
                passing.add(person.id)
        return Result(passing, values)

    async def _stat(self, node: dict) -> Result:
        limit = node["limit"]
        stat = dict(node["stat"], kind=LEAF_KINDS[node["type"]], include="both")
        scale = 60 if node["type"] in ("voice", "activity") else 1
        people = self.snap.people
        if limit["kind"] == "days":
            daily = await self.daily(stat, limit["of"])
            passing = set()
            values = {}
            need = limit["per_day"] * scale
            for person, days in daily.items():
                hits_to_today = sum(1 for amount in days[1:] if amount >= need)
                hits_to_yesterday = sum(1 for amount in days[:-1] if amount >= need)
                values[person] = max(hits_to_today, hits_to_yesterday)
                if person in people and values[person] >= limit["days"] and _filter_ok(node, people[person]):
                    passing.add(person)
            return Result(passing, values)
        stat["period"] = limit["period"]
        values = {person: amount / scale for person, amount in (await self.sums(stat)).items() if person in people}
        if limit["kind"] == "threshold":
            low, high = limit["min"], limit["max"]
            if low > 0:
                passing = {person for person, amount in values.items() if amount >= low and (high is None or amount <= high)}
            else:
                passing = {person for person in people if values.get(person, 0) <= high}
        else:
            candidates = self._ranked()
            if limit["matching_only"]:
                candidates = [person for person in candidates if _filter_ok(node, people[person])]
            passing = _rank_pass(values, candidates, limit, len(candidates))
        passing = {person for person in passing if _filter_ok(node, people[person])}
        return Result(passing, values)

    def _key(self, stat: dict, extra: str = "") -> str:
        import json
        return json.dumps(stat, sort_keys=True, default=str) + extra

    async def sums(self, stat: dict) -> dict[int, float]:
        """Each member's total of a statistic over its period. Voice and app time in seconds."""
        key = self._key(stat)
        if key in self._sums:
            return self._sums[key]
        snap = self.snap
        start, end = counters.period_slots(stat.get("period") or {"all": True}, snap.now, snap.tz)
        totals: dict[int, float] = {}
        if stat["kind"] == "invites":
            for row in await counters.invite_joins(self.db, stat, snap.guild, snap.guild_id, snap.settings, snap.now, start, end):
                totals[row["inviter_id"]] = totals.get(row["inviter_id"], 0.0) + 1
        else:
            for person, amount in await counters.stat_rows(self.db, stat, snap.guild, snap.guild_id, start, end, ["user_id"]):
                if person and amount:
                    totals[person] = totals.get(person, 0.0) + amount
        self._sums[key] = totals
        return totals

    async def daily(self, stat: dict, days: int) -> dict[int, list[float]]:
        """Each member's total on each of the last `days` days, oldest first, with today's tally so far last."""
        key = self._key(stat, f"|days:{days}")
        if key in self._daily:
            return self._daily[key]
        snap = self.snap
        edges = day_edges(snap.now, snap.tz, days)
        end = slot_of(snap.now) + 1
        out: dict[int, list[float]] = {}
        if stat["kind"] == "invites":
            for row in await counters.invite_joins(self.db, stat, snap.guild, snap.guild_id, snap.settings, snap.now, edges[0], end):
                index = _fold(edges, slot_of(row["at"]))
                if index is not None:
                    out.setdefault(row["inviter_id"], [0.0] * len(edges))[index] += 1
        else:
            for person, slot, amount in await counters.stat_rows(self.db, stat, snap.guild, snap.guild_id, edges[0], end, ["user_id", "slot"]):
                index = _fold(edges, slot)
                if person and amount and index is not None:
                    out.setdefault(person, [0.0] * len(edges))[index] += amount
        self._daily[key] = out
        return out


def _fold(edges: list[int], slot: int) -> int | None:
    import bisect
    if slot < edges[0]:
        return None
    return bisect.bisect_right(edges, slot) - 1


# ---- what a run changes ----

@dataclass
class Plan:
    give: list[int]
    take: list[int]
    matching: int
    left_alone: int  # members who chose privacy, so stat roles leave them as they are


def plan(rule: dict, evaluation: Evaluation, snapshot: Snapshot, role_id: int) -> Plan:
    """Who gains and who loses the role. Members outside the rule's reach (the wrong kind, ignored, still on the rules screen, or private)
    keep what they have."""
    ignored = set(rule.get("ignore") or [])
    apply_to = rule.get("apply_to", "humans")
    give, take = [], []
    left = 0
    for person in snapshot.people.values():
        if person.id in ignored or person.pending:
            continue
        if apply_to == "humans" and person.bot or apply_to == "bots" and not person.bot:
            continue
        if person.id in snapshot.private:
            left += 1
            continue
        holds = role_id in person.roles
        passes = person.id in evaluation.passing
        if passes and not holds:
            give.append(person.id)
        elif holds and not passes and not rule.get("permanent"):
            take.append(person.id)
    return Plan(sorted(give), sorted(take), len(evaluation.passing & set(snapshot.people)), left)


# ---- describing a rule ----

def _period_text(period: dict) -> str:
    if period.get("all"):
        return "all time"
    if period.get("since"):
        return f"since {period['since']}"
    amount, unit = period["amount"], period["unit"]
    return f"the last {amount} {unit[:-1] if amount == 1 else unit}"


def _amount_text(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}".rstrip("0").rstrip(".")


def describe(node: dict, names: dict[int, str] | None = None) -> str:
    """One condition in a line, as the dashboard and the commands show it."""
    kind = node["type"]
    lead = "Not: " if node.get("invert") else ""
    if kind == "match":
        return f"{lead}{'All' if node['mode'] == 'all' else 'Any'} of {len(node['conditions'])} conditions"
    if kind == "statrole":
        return f"{lead}Passes {(names or {}).get(node['rule'], 'another stat role')}"
    if kind in AGE_TYPES:
        what = "Joined" if kind == "joined" else "Account made"
        if node["compare"] in ("more", "less"):
            unit = node["unit"] if node["amount"] != 1 else node["unit"][:-1]
            return f"{lead}{what} {node['compare']} than {node['amount']:,} {unit} ago"
        return f"{lead}{what} {'before' if node['compare'] == 'before' else 'on or after'} {node['date']}"
    if kind == "compare":
        return f"{lead}{len(node['left'])} statistics {'less' if node['operator'] == 'less' else 'greater'} than {len(node['right'])} others"
    limit = node["limit"]
    unit = UNITS[kind]
    if kind == "emoji":
        unit = "emoji reactions received" if node["stat"].get("measure") == "received" else "emoji uses"
    if limit["kind"] == "threshold":
        span = f"at least {_amount_text(limit['min'])}" if limit["max"] is None else f"{_amount_text(limit['min'])} to {_amount_text(limit['max'])}"
        return f"{lead}{span} {unit} in {_period_text(limit['period'])}"
    if limit["kind"] == "top":
        if limit["by"] == "rank":
            span = f"places {limit['from']:,} to {limit['to']:,}"
        else:
            span = f"the top {_amount_text(limit['from'])}% to {_amount_text(limit['to'])}%"
        return f"{lead}{span} by {unit} in {_period_text(limit['period'])}"
    return f"{lead}{_amount_text(limit['per_day'])} {unit} a day on {limit['days']} of the last {limit['of']} days"


def explain(rule: dict, evaluation: Evaluation, person_id: int, names: dict[int, str] | None = None) -> list[dict]:
    """A member's result for each condition, as a nested list for the dashboard: {"text", "passed", "value", "children"}."""
    def walk(node: dict, place: str) -> dict:
        result = evaluation.results.get(place)
        entry = {"text": describe(node, names), "passed": bool(result and person_id in result.passing), "value": None, "children": []}
        if result is not None and person_id in result.values:
            entry["value"] = round(result.values[person_id], 2)
        if result is not None and result.note:
            entry["note"] = result.note
        if node["type"] == "match":
            entry["children"] = [walk(child, f"{place}.{index}") for index, child in enumerate(node["conditions"])]
        return entry
    return [walk(rule["conditions"], "0")]


# ---- notifications ----

def group_chunks(mentions: list[str], room: int) -> list[list[str]]:
    """Mentions split into lists that each fit in `room` characters when joined with commas."""
    chunks: list[list[str]] = []
    current: list[str] = []
    used = 0
    for mention in mentions:
        extra = len(mention) + (2 if current else 0)
        if current and used + extra > room:
            chunks.append(current)
            current, used = [], 0
            extra = len(mention)
        current.append(mention)
        used += extra
    if current:
        chunks.append(current)
    return chunks
