"""Reading and writing lengths of time: "3h42m", "2 days", "1w 3d".

Months are 30 days and years are 365 days, which is close enough for a length typed by a person.
"""
from __future__ import annotations

import re

from discord.ext import commands

UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "wks": 604800, "week": 604800, "weeks": 604800,
    "mo": 2592000, "mon": 2592000, "month": 2592000, "months": 2592000,
    "y": 31536000, "yr": 31536000, "yrs": 31536000, "year": 31536000, "years": 31536000,
}
LONGEST_TEXT = 100
_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)")
_NAMES = (("year", 31536000), ("month", 2592000), ("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1))


def parse_duration(text: str) -> int | None:
    """Seconds, or None if this isn't a length of time. A bare number isn't one, since it could mean anything."""
    cleaned = (text or "").strip().lower().replace(",", " ").replace(" and ", " ")
    if not cleaned or len(cleaned) > LONGEST_TEXT:  # a long run of digits costs quadratic time to search
        return None
    total = 0.0
    position = 0
    for match in _PART.finditer(cleaned):
        if cleaned[position:match.start()].strip():
            return None
        unit = UNITS.get(match.group(2))
        if unit is None:
            return None
        total += float(match.group(1)) * unit
        position = match.end()
    if position == 0 or cleaned[position:].strip() or total >= 1e15:  # hundreds of digits read as infinity, which has no whole number
        return None
    return int(total)


def split_leading_duration(text: str) -> tuple[int | None, str]:
    """Take a duration off the front of a sentence: "3h spamming" gives (10800, "spamming")."""
    words = (text or "").split()
    best, used = None, 0
    for count in range(1, min(len(words), 6) + 1):
        seconds = parse_duration(" ".join(words[:count]))
        if seconds is not None:
            best, used = seconds, count
    return best, " ".join(words[used:])


def humanize(seconds: float, *, parts: int = 3) -> str:
    """Written out: "2 days, 23 hours and 59 minutes"."""
    remaining = int(abs(seconds))
    chunks = []
    for name, size in _NAMES:
        amount, remaining = divmod(remaining, size)
        if amount:
            chunks.append(f"{amount} {name}{'' if amount == 1 else 's'}")
    chunks = chunks[:parts]
    if not chunks:
        return "0 seconds"
    if len(chunks) == 1:
        return chunks[0]
    return ", ".join(chunks[:-1]) + " and " + chunks[-1]


def ordinal(number: int) -> str:
    """8102 gives 8,102nd."""
    if 10 <= abs(number) % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(abs(number) % 10, "th")
    return f"{number:,}{suffix}"


class Duration(commands.Converter):
    """A command argument holding a length of time. Becomes seconds."""

    async def convert(self, ctx: commands.Context, argument: str) -> int:
        seconds = parse_duration(argument)
        if seconds is None:
            raise commands.BadArgument(f"`{argument}` isn't a length of time. Try something like 3h42m or 2 days.")
        return seconds
