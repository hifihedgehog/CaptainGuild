"""Time for statistics: the quarter-hour slots counts are kept in, and the time zones, periods and chart buckets they're read in.

Counts are stored per quarter hour of UTC, numbered from 1970, so slot 0 starts at midnight on January 1, 1970. Every time zone in use
today is a whole number of quarter hours from UTC, so the hours, days, weeks and months of any of them start on a slot's edge, and a chart
in any zone adds up whole slots.

A server reads its statistics in one time zone. Weeks start on Monday.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from ..validate import Invalid

SLOT_SECONDS = 900
INTERVALS = ("hour", "day", "week", "month")
INTERVAL_SECONDS = {"hour": 3600, "day": 86400, "week": 7 * 86400, "month": 31 * 86400}
MAX_DAYS = 36500
MAX_BUCKETS = 1000  # past this, a chart moves to the next longer interval
LOOKBACK_MODES = ("days", "since", "all")
DEFAULT_LOOKBACK = {"mode": "days", "days": 14, "since": None}
_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DISCORD_TIME = re.compile(r"<t:(-?\d{1,13})(?::[tTdDfFR])?>")
_AMOUNT = re.compile(r"(\d{1,6})\s*(d|day|days|w|week|weeks|m|mo|month|months|y|yr|year|years)?")
_UNIT_DAYS = {"d": 1, "day": 1, "days": 1, "w": 7, "week": 7, "weeks": 7, "m": 30, "mo": 30, "month": 30, "months": 30, "y": 365, "yr": 365, "year": 365, "years": 365}


def slot_of(timestamp: float) -> int:
    return int(timestamp // SLOT_SECONDS)


def slot_time(slot: int) -> int:
    return slot * SLOT_SECONDS


@lru_cache(maxsize=1)
def zone_names() -> frozenset[str]:
    return frozenset(available_timezones())


@lru_cache(maxsize=256)
def zone(name: str | None) -> ZoneInfo:
    """A server's time zone. A name the system doesn't know reads as UTC, so a zone removed from the database can't stop the charts."""
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def time_zone(value: Any) -> str:
    """A settings check: an IANA zone name such as America/Los_Angeles."""
    if not isinstance(value, str) or value.strip() not in zone_names():
        raise Invalid("Pick a time zone from the list, such as America/Los_Angeles or UTC.")
    return value.strip()


def _local(timestamp: float, tz: ZoneInfo) -> dt.datetime:
    return dt.datetime.fromtimestamp(timestamp, tz)


def _at(day: dt.date, tz: ZoneInfo) -> float:
    """The moment a local date begins. Where a clock change skips midnight, the day begins when the clock lands."""
    return dt.datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()


def floor_bucket(timestamp: float, interval: str, tz: ZoneInfo) -> float:
    """When the bucket holding this moment begins."""
    local = _local(timestamp, tz)
    if interval == "hour":
        return local.replace(minute=0, second=0, microsecond=0).timestamp()
    day = local.date()
    if interval == "week":
        day -= dt.timedelta(days=day.weekday())
    elif interval == "month":
        day = day.replace(day=1)
    return _at(day, tz)


def next_bucket(start: float, interval: str, tz: ZoneInfo) -> float:
    if interval == "hour":
        return start + 3600
    day = _local(start, tz).date()
    if interval == "day":
        following = day + dt.timedelta(days=1)
    elif interval == "week":
        following = day + dt.timedelta(days=7)
    else:
        following = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return _at(following, tz)


def bucket_edges(start: float, end: float, interval: str, tz: ZoneInfo) -> list[float]:
    """The start of every bucket that overlaps [start, end), oldest first."""
    edges = []
    edge = floor_bucket(start, interval, tz)
    while edge < end:
        edges.append(edge)
        following = next_bucket(edge, interval, tz)
        if following <= edge:  # a zone database oddity must not loop forever
            following = edge + 3600
        edge = following
    return edges


def fitting_interval(start: float, end: float, interval: str) -> str:
    """The interval asked for, or a longer one when the period would need more than MAX_BUCKETS points."""
    position = INTERVALS.index(interval) if interval in INTERVALS else 1
    for name in INTERVALS[position:]:
        if (end - start) / INTERVAL_SECONDS[name] <= MAX_BUCKETS:
            return name
    return "month"


def lookback_check(value: Any) -> dict:
    """A settings check for a lookback: {"mode": "days", "days": 14}, {"mode": "since", "since": "2025-03-01"} or {"mode": "all"}."""
    if not isinstance(value, dict) or value.get("mode") not in LOOKBACK_MODES:
        raise Invalid("Pick a number of days, a start date or all time.")
    mode = value["mode"]
    days = value.get("days", DEFAULT_LOOKBACK["days"])
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        if mode == "days":
            raise Invalid(f"Pick from 1 to {MAX_DAYS} days.")
        days = DEFAULT_LOOKBACK["days"]
    since = value.get("since")
    if mode == "since":
        since = _date_text(since)
    elif since is not None:
        try:
            since = _date_text(since)
        except Invalid:
            since = None
    return {"mode": mode, "days": days, "since": since}


def _date_text(value: Any) -> str:
    if not isinstance(value, str) or not _DATE.fullmatch(value.strip()):
        raise Invalid("Give the start date as YYYY-MM-DD.")
    year, month, day = (int(part) for part in _DATE.fullmatch(value.strip()).groups())
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError as error:
        raise Invalid("That date doesn't exist.") from error


@dataclass(frozen=True)
class Period:
    """[start, end) in Unix seconds. A start of None reaches back to the first thing recorded."""
    start: float | None
    end: float
    label: str

    def first_slot(self) -> int | None:
        return None if self.start is None else slot_of(self.start)

    def end_slot(self) -> int:
        """The first slot past the period. The slot holding `end` counts, since what's recorded so far in it happened before `end`."""
        return slot_of(self.end) + 1


def period_for(lookback: dict, now: float, interval: str, tz: ZoneInfo) -> Period:
    """The period a lookback setting names, ending now. A number of days starts at the beginning of the bucket that many days back,
    so a chart's first point is whole and its last one is the bucket still running."""
    mode = lookback.get("mode")
    if mode == "all":
        return Period(None, now, "All time")
    if mode == "since" and lookback.get("since"):
        day = dt.date.fromisoformat(lookback["since"])
        return Period(_at(day, tz), now, f"Since {day_label(day)}")
    days = lookback.get("days") or DEFAULT_LOOKBACK["days"]
    return Period(floor_bucket(now - days * 86400, interval, tz), now, f"Last {days} day{'s' if days != 1 else ''}")


def rolling(now: float, days: int) -> Period:
    """The last `days` days, to the quarter hour: what a card's 1-day and 7-day rows read."""
    return Period(slot_time(slot_of(now - days * 86400)), now, f"Last {days} day{'s' if days != 1 else ''}")


def day_label(day: dt.date) -> str:
    return f"{day.strftime('%b')} {day.day}, {day.year}"


def parse_lookback(text: str, tz: ZoneInfo, now: float) -> dict:
    """What a person typed for a custom period: a number of days ("90", "6 months"), a start date (2025-03-01), a Unix time, a Discord
    timestamp (<t:1735689600:D>) or "all". Raises Invalid with the sentence to show."""
    cleaned = (text or "").strip().lower()
    if cleaned in ("all", "all time", "everything", "forever"):
        return {"mode": "all", "days": DEFAULT_LOOKBACK["days"], "since": None}
    found = _DISCORD_TIME.fullmatch(cleaned)
    if found or re.fullmatch(r"\d{9,13}", cleaned):
        number = int(found.group(1) if found else cleaned)
        seconds = number / 1000 if number > 10 ** 11 else number
        if seconds > now:
            raise Invalid("That time hasn't happened yet.")
        return {"mode": "since", "days": DEFAULT_LOOKBACK["days"], "since": _local(seconds, tz).date().isoformat()}
    if _DATE.fullmatch(cleaned):
        since = _date_text(cleaned)
        if _at(dt.date.fromisoformat(since), tz) > now:
            raise Invalid("That date hasn't come yet.")
        return {"mode": "since", "days": DEFAULT_LOOKBACK["days"], "since": since}
    found = _AMOUNT.fullmatch(cleaned)
    if found:
        days = int(found.group(1)) * _UNIT_DAYS[found.group(2) or "d"]
        if not 1 <= days <= MAX_DAYS:
            raise Invalid(f"Pick from 1 to {MAX_DAYS} days.")
        return {"mode": "days", "days": days, "since": None}
    raise Invalid("Type a number of days (such as 90), a start date (such as 2025-03-01), a Discord timestamp, or all.")


def bucket_label(start: float, interval: str, tz: ZoneInfo) -> str:
    local = _local(start, tz)
    if interval == "hour":
        hour = local.hour % 12 or 12
        return f"{local.strftime('%b')} {local.day}, {hour} {'AM' if local.hour < 12 else 'PM'}"
    if interval == "day":
        return f"{local.strftime('%b')} {local.day}"
    if interval == "week":
        return f"Week of {local.strftime('%b')} {local.day}"
    return f"{local.strftime('%b')} {local.year}"


def zone_label(tz: ZoneInfo, now: float) -> str:
    """UTC-08:00 (America/Los_Angeles), as cards print it."""
    offset = _local(now, tz).utcoffset() or dt.timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    name = getattr(tz, "key", "UTC")
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}" + ("" if name == "UTC" else f" ({name})")


def hour_and_weekday(slot: int, tz: ZoneInfo) -> tuple[int, int]:
    """The local weekday (Monday 0) and hour a slot starts in, for heatmaps."""
    local = _local(slot_time(slot), tz)
    return local.weekday(), local.hour
