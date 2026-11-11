"""Dates for the strf and td blocks, all in UTC.

Formatting is written out here instead of calling strftime, because the C library's version differs by system: Windows refuses %-d and %F,
and any of them could pick up a locale. A template produces the same text on every machine.
"""
from __future__ import annotations

import calendar
import datetime
import re

UTC = datetime.timezone.utc
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=UTC)
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")

_FIELD = re.compile(r"%([-_0^#]*)(\d{0,2})[EO]?(:z|[A-Za-z%+])")
_WHEN = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})"
    r"(?:[ T](\d{1,2})[:.](\d{1,2})(?:[:.](\d{1,2}))?(?:\.\d+)?)?"
    r"\s*(?:Z|UTC|\+00:?00)?",
    re.IGNORECASE,
)
_UNIX = re.compile(r"-?\d+(?:\.\d+)?")

# Codes that stand for a longer format.
_SHORTHAND = {
    "c": "%a %b %e %H:%M:%S %Y",
    "x": "%Y/%m/%d",
    "X": "%H:%M:%S",
    "D": "%m/%d/%y",
    "F": "%Y-%m-%d",
    "T": "%H:%M:%S",
    "R": "%H:%M",
    "r": "%I:%M:%S %p",
}


def _number(moment: datetime.datetime, code: str) -> tuple[int, int, str] | None:
    """A numeric code as (value, width, padding)."""
    hour12 = moment.hour % 12 or 12
    sunday_first = (moment.weekday() + 1) % 7
    day_of_year = moment.timetuple().tm_yday
    if code == "d":
        return moment.day, 2, "0"
    if code == "e":
        return moment.day, 2, " "
    if code == "H":
        return moment.hour, 2, "0"
    if code == "k":
        return moment.hour, 2, " "
    if code == "I":
        return hour12, 2, "0"
    if code == "l":
        return hour12, 2, " "
    if code == "j":
        return day_of_year, 3, "0"
    if code == "m":
        return moment.month, 2, "0"
    if code == "M":
        return moment.minute, 2, "0"
    if code == "S":
        return moment.second, 2, "0"
    if code == "y":
        return moment.year % 100, 2, "0"
    if code == "Y":
        return moment.year, 1, "0"
    if code == "C":
        return moment.year // 100, 2, "0"
    if code == "u":
        return moment.isoweekday(), 1, "0"
    if code == "w":
        return sunday_first, 1, "0"
    if code == "U":
        return (day_of_year - 1 + 7 - sunday_first) // 7, 2, "0"
    if code == "W":
        return (day_of_year - 1 + 7 - moment.weekday()) // 7, 2, "0"
    if code == "V":
        return moment.isocalendar().week, 2, "0"
    if code == "G":
        return moment.isocalendar().year, 1, "0"
    if code == "g":
        return moment.isocalendar().year % 100, 2, "0"
    if code == "s":
        return int((moment - EPOCH).total_seconds()), 1, "0"
    if code == "f":
        return moment.microsecond, 6, "0"
    return None


def _word(moment: datetime.datetime, code: str) -> str | None:
    if code == "a":
        return _DAYS[moment.weekday()][:3]
    if code == "A":
        return _DAYS[moment.weekday()]
    if code in ("b", "h"):
        return _MONTHS[moment.month - 1][:3]
    if code == "B":
        return _MONTHS[moment.month - 1]
    if code == "p":
        return "AM" if moment.hour < 12 else "PM"
    if code == "P":
        return "am" if moment.hour < 12 else "pm"
    if code == "Z":
        return "UTC"
    if code == "z":
        return "+0000"
    if code == ":z":
        return "+00:00"
    if code == "n":
        return "\n"
    if code == "t":
        return "\t"
    if code == "%":
        return "%"
    return None


def _field_text(moment: datetime.datetime, match: re.Match) -> str:
    """What one code of a format gives, read from its match of _FIELD."""
    flags, width, code = match.group(1), match.group(2), match.group(3)
    shorthand = _SHORTHAND.get(code)
    number = None if shorthand else _number(moment, code)
    if number is not None:
        value, default_width, padding = number
        if "-" in flags:
            return str(value)
        if "_" in flags:
            padding = " "
        elif "0" in flags:
            padding = "0"
        return str(value).rjust(int(width) if width else default_width, padding)
    text = format_time(moment, shorthand) if shorthand else _word(moment, code)
    if text is None:
        return match.group(0)
    if "#" in flags:
        text = text.lower() if code in ("p", "Z") else text.upper()
    if "^" in flags:
        text = text.upper()
    if width:
        text = text.rjust(int(width), "0" if "0" in flags else " ")
    return text


def format_time(moment: datetime.datetime, pattern: str, codes: dict | None = None, literal: str | None = None) -> str:
    """Like Linux's strftime in the C locale, with its flags: %-d drops padding, %_d pads with spaces, %^a is upper case. A code that comes
    again gives the same text, made once. With `codes`, a str.translate table, what each code gives goes through it. With `literal`, a text as
    long as the pattern, the pattern's own text is taken from it, character for character. Either way it's one pass over the pattern."""
    made: dict[str, str] = {}

    def field(match: re.Match) -> str:
        whole = match.group(0)
        text = made.get(whole)
        if text is None:
            text = _field_text(moment, match)
            text = made[whole] = text if codes is None else text.translate(codes)
        return text

    if literal is None:
        return _FIELD.sub(field, pattern)
    pieces, at = [], 0
    for match in _FIELD.finditer(pattern):
        pieces += (literal[at:match.start()], field(match))
        at = match.end()
    pieces.append(literal[at:])
    return "".join(pieces)


def parse_when(text: str) -> datetime.datetime | None:
    """A date and time, with colons or periods between the time's parts, or a Unix time."""
    cleaned = text.strip()
    try:
        if _UNIX.fullmatch(cleaned):
            return EPOCH + datetime.timedelta(seconds=float(cleaned))
        match = _WHEN.fullmatch(cleaned)
        if match is None:
            return None
        parts = [int(part) if part else 0 for part in match.groups()]
        return datetime.datetime(*parts, tzinfo=UTC)
    except (ValueError, OverflowError):
        return None


def from_unix(seconds: float) -> datetime.datetime:
    return EPOCH + datetime.timedelta(seconds=seconds)


def stamp(moment: datetime.datetime | None) -> str:
    """The DateTime form a template reads back. The bot's own dates are always aware, and the guard is for a member who has none."""
    if moment is None:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime(DATETIME_FORMAT)


def relative(moment: datetime.datetime | None) -> str:
    """A Discord timestamp that reads as "3 years ago" or "in 5 days" and keeps itself current. Nothing when there's no date."""
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"<t:{int(moment.timestamp())}:R>"


def _plus_months(moment: datetime.datetime, months: int) -> datetime.datetime:
    year, month = divmod(moment.year * 12 + moment.month - 1 + months, 12)
    return moment.replace(year=year, month=month + 1, day=min(moment.day, calendar.monthrange(year, month + 1)[1]))


def describe_gap(first: datetime.datetime, second: datetime.datetime, units: int = 3) -> str:
    """The time between two moments in calendar terms, largest three units: "1 month, 5 days and 21 hours". `units` is how many it names, so 1 gives "1 month"."""
    early, late = sorted((first.replace(microsecond=0), second.replace(microsecond=0)))
    months = (late.year - early.year) * 12 + late.month - early.month
    if _plus_months(early, months) > late:
        months -= 1
    rest = late - _plus_months(early, months)
    years, months = divmod(months, 12)
    weeks, days = divmod(rest.days, 7)
    hours, remainder = divmod(rest.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = [(years, "year"), (months, "month"), (weeks, "week"), (days, "day"), (hours, "hour"), (minutes, "minute"), (seconds, "second")]
    words = [f"{amount} {name}{'' if amount == 1 else 's'}" for amount, name in parts if amount][:max(units, 1)]
    if not words:
        return "now"
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]
