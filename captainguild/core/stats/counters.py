"""Counter channels: what each kind of counter counts, its options, and how a channel's name is made from text and counters.

A counter channel's name is text the server writes with numbered places for its counters, as in "Members: {1}". Each counter has a type and
options. Reading a counter gives a Reading: a number or words, and a note when the value is old, approximate or missing. Rendering puts the
readings into the text, formats numbers the way each counter asks, and keeps the name within Discord's 100 characters.

Updates line up with the clock. A setup updates every N minutes, counted from midnight in the zone of its first date counter, or in the
server's own zone when it has none, so a daily date counter turns over at midnight there. Discord lets a bot rename a channel twice in ten
minutes, and nothing here ever asks for a third.

Dates follow the dayjs format tokens with its advanced-format plugin, read the same way: the plugin's tokens first, then the core ones, with
text in square brackets left as written. The en locale's names and ordinals are used.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from ..validate import Invalid, integer, snowflake, text, whole_number
from . import invites as invite_rules
from . import query, store
from .timeframe import MAX_DAYS, SLOT_SECONDS, hour_and_weekday, slot_of, zone

# ---- limits and fixed values ----

FREQUENCIES = (6, 10, 15, 20, 30, 45, 60, 120, 180, 240, 360, 480, 720, 1440)  # minutes
DEFAULT_FREQUENCY = 10
NAME_LIMIT = 100  # Discord's longest channel name
TEXT_LIMIT = 200  # what a person types, before the counters go in
LABEL_LIMIT = 100
MAX_COUNTERS = 50  # as many as the text can name: {1} to {50} take 191 of its 200 characters, so the name's length is the real limit
MAX_RANK = 50
MAX_DECIMALS = 4
MAX_GOAL = 10 ** 12
RENAMES_ALLOWED = 2
RENAME_WINDOW = 600.0  # seconds: Discord allows two renames of one channel in this long
PLACEHOLDER = re.compile(r"\{(\d{1,2})\}")
DEFAULT_END_TEXT = "Countdown over"
UNKNOWN = "?"
STATUS_NAMES = ("online", "idle", "dnd", "offline")
NOT_OFFLINE = frozenset({"online", "idle", "dnd"})
CHANNEL_TYPES = {"text": 0, "voice": 2, "category": 4, "announcement": 5, "stage": 13, "forum": 15, "media": 16}
TEXT_LIKE = (0, 5, 15)  # text, announcement and forum channels
VOICE_LIKE = (2, 13)
STYLES = ("plain", "separated", "short")
STAT_KINDS = ("messages", "voice", "activities", "emoji", "invites", "statuses", "members")
TIME_UNITS = {"minutes": 60, "hours": 3600, "days": 86400}
NAME_STYLES = ("username", "display", "nickname")
TOP_OF = ("member", "channel", "emoji", "activity")
EMOJI_STATES = ("used", "deleted", "unavailable")
EMOJI_TYPES = ("server", "animated", "managed", "restricted", "super")
ZONE_OFFSET = re.compile(r"UTC([+-])(\d{1,2}):(\d{2})")
SERVER_ZONE = "server"

# Which counter types count from what the bot sees in Discord, from stored statistics, or from another site.
LIVE_TYPES = ("members", "channels", "statuses", "role_members", "roles", "voice")
TIME_TYPES = ("countdown", "datetime")
STAT_TYPES = ("stat_count", "stat_average", "stat_change", "stat_top", "invites")
SOCIAL_TYPES = ("youtube", "instagram", "tiktok")
TYPES = LIVE_TYPES + TIME_TYPES + STAT_TYPES + SOCIAL_TYPES
WORD_TYPES = frozenset({"countdown", "datetime", "stat_top"})  # these show words, so they take no number style
GOAL_TYPES = frozenset({*LIVE_TYPES, "stat_count", "stat_average", "stat_change", "invites", *SOCIAL_TYPES})  # a goal shows how far a number is from it
LABELS = {
    "members": "Members", "channels": "Channels", "statuses": "Members with a status", "role_members": "Members in a role", "roles": "Roles",
    "voice": "Members in voice", "countdown": "Countdown", "datetime": "Date and time", "stat_count": "Stat count", "stat_average": "Stat average",
    "stat_change": "Stat change", "stat_top": "Stat top", "invites": "Invite joins", "youtube": "YouTube subscribers", "instagram": "Instagram followers",
    "tiktok": "TikTok followers",
}
DATE_PRESETS = ("h:mma", "H:mm", "M/D/YYYY", "dddd, MMM Do", "MMMM Do", "MMMM Do, YYYY", "YYYY-MM-DD")
NO_PRESENCE = "The bot doesn't get each member's status from Discord, so this keeps its last value."
APPROXIMATE = "Counts everyone online, idle or on do not disturb, from Discord's count of members present, since the bot doesn't get each member's status."
SPLIT = ("Discord's count of members present, split by this server's statuses at this hour of the week over the last four weeks it has real "
         "statuses for, since the bot doesn't get each member's status.")


# ---- checking what a person saved ----

def _choice(value: Any, options: Iterable[Any], what: str) -> Any:
    if value not in tuple(options):
        raise Invalid(f"Pick {what} from the list.")
    return value


def _ids(value: Any, what: str, maximum: int = 250) -> list[int]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise Invalid(f"{what} must be a list.")
    if len(value) > maximum:
        raise Invalid(f"{what} can hold at most {maximum}.")
    out: list[int] = []
    for item in value:
        try:
            number = snowflake(item)
        except Invalid as error:
            raise Invalid(f"{what}: {error}") from error
        if number not in out:
            out.append(number)
    return out


def _names(value: Any, allowed: Iterable[str], what: str, *, empty_ok: bool = True) -> list[str]:
    allowed = tuple(allowed)
    if value in (None, ""):
        value = []
    if not isinstance(value, list) or any(item not in allowed for item in value):
        raise Invalid(f"Pick {what} from the list.")
    out = [item for item in allowed if item in value]
    if not out and not empty_ok:
        raise Invalid(f"Pick at least one of {what}.")
    return out


def _goal(value: Any) -> int | None:
    if value in (None, ""):
        return None
    number = whole_number(value, MAX_GOAL)
    if number is None:
        raise Invalid(f"A goal is a whole number from 0 to {MAX_GOAL:,}.")
    return number


def _decimals(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return integer(0, MAX_DECIMALS)(value)
    except Invalid as error:
        raise Invalid(f"Decimals go from 0 to {MAX_DECIMALS}.") from error


def _flag(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise Invalid("A switch must be on or off.")
    return value


def clean_period(value: Any, *, allow_all: bool = True) -> dict:
    """How far back a statistic reaches: {"amount": 24, "unit": "hours"}, {"amount": 7, "unit": "days"}, {"since": "2025-03-01"} or {"all": true}."""
    if not isinstance(value, dict):
        raise Invalid("Pick how far back to count.")
    if value.get("all"):
        if not allow_all:
            raise Invalid("Pick a number of hours or days, or a start date.")
        return {"all": True}
    if value.get("since"):
        since = value["since"]
        try:
            day = dt.date.fromisoformat(str(since).strip())
        except ValueError as error:
            raise Invalid("Give the start date as YYYY-MM-DD.") from error
        return {"since": day.isoformat()}
    unit = value.get("unit", "days")
    if unit not in ("hours", "days"):
        raise Invalid("Count back in hours or days.")
    most = MAX_DAYS * (24 if unit == "hours" else 1)
    amount = whole_number(value.get("amount"), most)
    if not amount:
        raise Invalid(f"Count back from 1 to {most:,} {unit}.")
    return {"amount": amount, "unit": unit}


def _match_list(value: Any, allowed: Iterable[str], what: str) -> dict:
    """A list of types or reasons with how they match: {"types": [...], "match": "any" or "all"}."""
    if value in (None, ""):
        value = {}
    if not isinstance(value, dict):
        raise Invalid(f"Pick {what} from the list.")
    return {"types": _names(value.get("types"), allowed, what), "match": _choice(value.get("match", "any"), ("any", "all"), "any or all")}


def clean_stat(value: Any, *, kinds: Iterable[str] = STAT_KINDS, people: bool = True) -> dict:
    """What a stat counter or a stat role condition reads: the kind of statistic, how far back, and its filters."""
    if not isinstance(value, dict):
        raise Invalid("Pick a statistic.")
    kind = _choice(value.get("kind"), kinds, "a statistic")
    out: dict[str, Any] = {"kind": kind, "period": clean_period(value.get("period") or {"amount": 1, "unit": "days"})}
    if kind in ("messages", "voice", "activities", "emoji", "invites"):
        out["include"] = _choice(value.get("include", "humans"), ("humans", "bots", "both"), "humans, bots or both")
    if kind in ("messages", "voice", "emoji"):
        out["channel_mode"] = _choice(value.get("channel_mode", "block"), ("allow", "block"), "allow or block")
        out["channels"] = _ids(value.get("channels"), "Channels", 500)
    if people and kind in ("messages", "voice", "activities", "emoji", "invites"):
        out["member_mode"] = _choice(value.get("member_mode", "block"), ("allow", "block"), "allow or block")
        out["members"] = _ids(value.get("members"), "Members", 1000)
        out["roles_allow"] = _ids(value.get("roles_allow"), "Roles to count", 250)
        out["roles_block"] = _ids(value.get("roles_block"), "Roles to leave out", 250)
    if kind == "voice":
        states = value.get("states")
        out["states"] = _names(sorted(store.VOICE_STATES) if states is None else states, store.VOICE_STATES, "voice states", empty_ok=False)
    if kind == "activities":
        out["activities"] = [_activity(item) for item in (value.get("activities") or [])][:100]
        kinds_given = value.get("activity_kinds")
        out["activity_kinds"] = _names(list(store.ACTIVITY_KINDS) if kinds_given is None else kinds_given, store.ACTIVITY_KINDS, "activity types", empty_ok=False)
    if kind == "emoji":
        out["emoji_mode"] = _choice(value.get("emoji_mode", "block"), ("allow", "block"), "allow or block")
        emoji = value.get("emoji") or []
        if not isinstance(emoji, list) or len(emoji) > 25:
            raise Invalid("Pick at most 25 emoji.")
        out["emoji"] = list(dict.fromkeys(_emoji_key(item) for item in emoji))
        uses = value.get("emoji_uses")
        out["emoji_uses"] = _names(list(store.EMOJI_USES) if uses is None else uses, store.EMOJI_USES, "uses", empty_ok=False)
        states = value.get("emoji_states")
        out["emoji_states"] = _names(list(EMOJI_STATES) if states is None else states, EMOJI_STATES, "emoji states", empty_ok=False)
        out["type_allow"] = _match_list(value.get("type_allow"), EMOJI_TYPES, "emoji types")
        out["type_block"] = _match_list(value.get("type_block"), EMOJI_TYPES, "emoji types")
        out["measure"] = _choice(value.get("measure", "used"), ("used", "received"), "used or received")
    if kind == "invites":
        out["invite_mode"] = _choice(value.get("invite_mode", "block"), ("allow", "block"), "allow or block")
        codes = value.get("invites") or []
        if not isinstance(codes, list) or len(codes) > 500:
            raise Invalid("Pick at most 500 invites.")
        out["invites"] = list(dict.fromkeys(_invite_code(code) for code in codes))
        out["fake_allow"] = _match_list(_reasons(value.get("fake_allow")), invite_rules.FAKE_REASONS, "fake reasons")
        out["fake_block"] = _match_list(_reasons(value.get("fake_block")), invite_rules.FAKE_REASONS, "fake reasons")
    if kind == "statuses":
        out["statuses"] = _names(value.get("statuses") or list(NOT_OFFLINE), ("online", "idle", "dnd"), "statuses", empty_ok=False)
    return out


def _reasons(value: Any) -> Any:
    """Fake reasons are kept under "types" like emoji types, so one checker serves both."""
    if isinstance(value, dict) and "reasons" in value and "types" not in value:
        return {"types": value.get("reasons"), "match": value.get("match", "any")}
    return value


def _activity(value: Any) -> int:
    number = whole_number(value, 2 ** 63 - 1)
    if number is None:
        raise Invalid("That isn't an activity.")
    return number


def _emoji_key(value: Any) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        if re.fullmatch(r"[cs]:\d{15,21}", cleaned):
            return cleaned
        if cleaned.startswith("u:") and 2 < len(cleaned) <= 40:
            from . import emoji as emoji_text
            return store.unicode_key(emoji_text.bare(cleaned[2:]))
    raise Invalid("Pick an emoji from the list.")


def _invite_code(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"(?:vanity:)?[A-Za-z0-9-]{2,32}", value.strip()):
        return value.strip()
    raise Invalid("That isn't an invite code.")


def clean_zone(value: Any) -> str:
    """"server" for the server's own zone, an IANA zone such as Europe/Paris, or a fixed offset such as UTC+05:30."""
    if value in (None, "", SERVER_ZONE):
        return SERVER_ZONE
    if not isinstance(value, str):
        raise Invalid("Pick a time zone.")
    cleaned = value.strip()
    found = ZONE_OFFSET.fullmatch(cleaned)
    if found:
        hours, minutes = int(found.group(2)), int(found.group(3))
        total = (hours * 60 + minutes) * (-1 if found.group(1) == "-" else 1)
        if minutes % 15 or not -12 * 60 <= total <= 14 * 60:
            raise Invalid("A fixed offset goes from UTC-12:00 to UTC+14:00 in quarter hours.")
        return offset_name(total)
    from .timeframe import time_zone
    try:
        return time_zone(cleaned)
    except Invalid as error:
        raise Invalid("Pick a time zone from the list, the server's zone, or a fixed offset such as UTC+05:30.") from error


def offset_name(minutes: int) -> str:
    sign = "-" if minutes < 0 else "+"
    minutes = abs(minutes)
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def clean_counter(value: Any) -> dict:
    """One counter's type and options, with every default filled in. Raises Invalid with the sentence to show."""
    if not isinstance(value, dict):
        raise Invalid("Each counter needs a type.")
    kind = _choice(value.get("type"), TYPES, "a counter type")
    out: dict[str, Any] = {"type": kind}
    if kind not in WORD_TYPES:
        out["style"] = _choice(value.get("style", "plain"), STYLES, "a number style")
    if kind in GOAL_TYPES:
        out["goal"] = _goal(value.get("goal"))
    if kind in ("members", "statuses", "role_members", "voice"):
        out["include"] = _choice(value.get("include", "both"), ("humans", "bots", "both"), "humans, bots or both")
    if kind == "channels":
        types = value.get("channel_types")
        if types is None:
            types = [*TEXT_LIKE, *VOICE_LIKE, 4]
        if not isinstance(types, list) or not types or any(item not in CHANNEL_TYPES.values() for item in types):
            raise Invalid("Pick at least one kind of channel to count.")
        out["channel_types"] = sorted(set(types))
    elif kind == "statuses":
        out["statuses"] = _names(value.get("statuses"), STATUS_NAMES, "statuses", empty_ok=False)
    elif kind == "role_members":
        out["roles"] = _ids(value.get("roles"), "Roles")
        if not out["roles"]:
            raise Invalid("Pick at least one role to count members in.")
        out["statuses"] = _names(value.get("statuses"), STATUS_NAMES, "statuses")
    elif kind == "roles":
        out["managed"] = _flag(value.get("managed"), True)
        out["unmanaged"] = _flag(value.get("unmanaged"), True)
        if not out["managed"] and not out["unmanaged"]:
            raise Invalid("Count managed roles, other roles or both.")
    elif kind == "voice":
        out["channel_mode"] = _choice(value.get("channel_mode", "block"), ("allow", "block"), "allow or block")
        out["channels"] = _ids(value.get("channels"), "Channels", 500)
    elif kind == "countdown":
        end = value.get("end")
        if isinstance(end, bool) or not isinstance(end, (int, float)) or not 0 < end < 32503680000:
            raise Invalid("Pick when the countdown ends.")
        out["end"] = float(end)
        units = value.get("units")
        if units is None:
            units = {"days": "d ", "hours": "h ", "minutes": "m"}
        if not isinstance(units, dict) or not units or any(key not in ("days", "hours", "minutes") for key in units):
            raise Invalid("Show at least one of days, hours and minutes.")
        out["units"] = {key: text(20, strip=False)(units[key] if units[key] is not None else "") for key in ("days", "hours", "minutes") if key in units}
        out["end_text"] = text(NAME_LIMIT)(value.get("end_text") or DEFAULT_END_TEXT) or DEFAULT_END_TEXT
    elif kind == "datetime":
        fmt = value.get("format") or "MMMM D, YYYY"
        out["format"] = text(80, minimum=1, strip=False)(fmt)
        out["zone"] = clean_zone(value.get("zone"))
    elif kind in ("stat_count", "stat_average", "stat_change", "stat_top"):
        allowed = {"stat_count": ("messages", "voice", "activities", "emoji", "invites"),
                   "stat_average": ("messages", "voice", "activities", "emoji", "invites", "statuses", "members"),
                   "stat_change": ("messages", "voice", "activities", "emoji", "invites", "statuses", "members"),
                   "stat_top": ("messages", "voice", "activities", "emoji", "invites")}[kind]
        out["stat"] = clean_stat(value.get("stat"), kinds=allowed)
        if out["stat"]["kind"] in ("voice", "activities") and kind != "stat_top":
            out["unit"] = _choice(value.get("unit", "hours"), TIME_UNITS, "minutes, hours or days")
        if kind == "stat_average":
            out["per"] = _choice(value.get("per", "day"), ("hour", "day", "week"), "per hour, day or week")
        if kind == "stat_change":
            if out["stat"]["period"].get("all"):
                raise Invalid("A change compares one stretch of time with the one before it, so pick a number of hours or days, or a start date.")
            out["as"] = _choice(value.get("as", "value"), ("value", "percent"), "a number or a percentage")
            out["plus"] = _flag(value.get("plus"), True)
        if kind in ("stat_count", "stat_average", "stat_change"):
            whole = kind == "stat_count" or (kind == "stat_change" and out["as"] == "value")  # a count or a change in a count is whole
            out["decimals"] = _decimals(value.get("decimals"), 0 if whole else 1)
        if kind == "stat_top":
            stat_kind = out["stat"]["kind"]
            choices = {"messages": ("member", "channel"), "voice": ("member", "channel"), "activities": ("member", "activity"),
                       "emoji": ("emoji", "member", "channel"), "invites": ("member",)}[stat_kind]
            out["of"] = _choice(value.get("of", choices[0]), choices, "what to rank")
            rank = whole_number(value.get("rank", 1), MAX_RANK)
            if not rank:
                raise Invalid(f"Pick a place from 1 to {MAX_RANK}.")
            out["rank"] = rank
            out["name"] = _choice(value.get("name", "display"), NAME_STYLES, "how names show")
            out["show_value"] = _flag(value.get("show_value"), False)
            out["empty_text"] = text(40)(value.get("empty_text") or "Nobody yet")
    elif kind == "invites":
        stat = dict(value.get("stat") or {})
        stat["kind"] = "invites"
        stat.setdefault("period", {"all": True})
        out["stat"] = clean_stat(stat, kinds=("invites",))
    elif kind == "youtube":
        out["account"] = _youtube_account(value.get("account"))
    elif kind in ("instagram", "tiktok"):
        out["account"] = _social_account(value.get("account"), kind)
    return out


def _youtube_account(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Invalid("Give the YouTube channel's ID (it starts with UC) or its @handle.")
    cleaned = value.strip()
    found = re.search(r"(UC[0-9A-Za-z_-]{22})", cleaned)
    if found:
        return found.group(1)
    handle = re.search(r"@([0-9A-Za-z._-]{3,30})", cleaned)
    if handle:
        return "@" + handle.group(1)
    if re.fullmatch(r"[0-9A-Za-z._-]{3,30}", cleaned):
        return "@" + cleaned
    raise Invalid("Give the YouTube channel's ID (it starts with UC) or its @handle.")


def _social_account(value: Any, site: str) -> str:
    if not isinstance(value, str):
        raise Invalid("Give the account's username.")
    cleaned = value.strip()
    found = re.search(r"(?:instagram\.com|tiktok\.com)/@?([0-9A-Za-z._]{1,30})", cleaned)
    name = found.group(1) if found else cleaned.lstrip("@")
    limit = 30 if site == "instagram" else 24
    if not re.fullmatch(rf"[0-9A-Za-z._]{{1,{limit}}}", name):
        raise Invalid("Give the account's username, without spaces.")
    return name.lower()


def clean_setup(value: Any) -> dict:
    """A counter channel's saved setup: {"name", "text", "counters", "frequency"}. Raises Invalid with the sentence to show."""
    if not isinstance(value, dict):
        raise Invalid("A counter channel needs its text.")
    label = text(LABEL_LIMIT)(value.get("name") or "")
    words = value.get("text")
    if not isinstance(words, str) or not words.strip():
        raise Invalid("Write the channel's text, with a place for each counter such as {1}.")
    if len(words) > TEXT_LIMIT or "\n" in words:
        raise Invalid(f"The channel's text is one line of at most {TEXT_LIMIT} characters.")
    raw = value.get("counters")
    if not isinstance(raw, list) or not raw:
        raise Invalid("Add at least one counter.")
    if len(raw) > MAX_COUNTERS:
        raise Invalid(f"A channel can hold at most {MAX_COUNTERS} counters.")
    counters = []
    for index, item in enumerate(raw, 1):
        try:
            counters.append(clean_counter(item))
        except Invalid as error:
            raise Invalid(f"Counter {index}: {error}") from error
    used = {int(found) for found in PLACEHOLDER.findall(words)}
    for number in sorted(used):
        if not 1 <= number <= len(counters):
            raise Invalid(f"The text names counter {{{number}}}, and there are {len(counters)}.")
    for number in range(1, len(counters) + 1):
        if number not in used:
            raise Invalid(f"Counter {number} isn't in the text. Put {{{number}}} where it goes, or remove it.")
    frequency = whole_number(value.get("frequency", DEFAULT_FREQUENCY))
    if frequency not in FREQUENCIES:
        raise Invalid("Pick how often it updates from the list.")
    return {"name": label, "text": words.strip(), "counters": counters, "frequency": frequency}


# ---- numbers ----

def format_number(value: float, style: str = "plain", decimals: int = 0) -> str:
    """13742 as 13742, 13,742 or 13.7k."""
    if value is None or isinstance(value, float) and not math.isfinite(value):
        return UNKNOWN
    if style == "short" and abs(value) >= 1000:
        return _short(value)
    rounded = round(value, decimals)
    if decimals == 0:
        rounded = int(rounded)
        return f"{rounded:,}" if style == "separated" else str(rounded)
    return f"{rounded:,.{decimals}f}" if style == "separated" else f"{rounded:.{decimals}f}"


def _short(value: float) -> str:
    sign = "-" if value < 0 else ""
    size = abs(value)
    for step, (divisor, letter) in enumerate(((1e3, "k"), (1e6, "M"), (1e9, "B"), (1e12, "T"))):
        scaled = size / divisor
        if scaled >= 1000 and step < 3:
            continue
        shown = f"{scaled:.1f}" if scaled < 100 else f"{scaled:.0f}"
        if float(shown) >= 1000 and step < 3:
            continue  # 999,960 rounds up to the next letter
        return sign + shown.removesuffix(".0") + letter
    return sign + str(int(size))


def short_count(text_value: str) -> int | None:
    """A count written the short way, as sites show them: "246K", "46.7M subscribers", "1,234". None when it isn't one. A leading word
    goes, a count with no letter loses its separators, and a comma before a letter is a decimal point."""
    cleaned = re.sub(r"^[^\d]+\s", "", text_value.replace("\u00a0", " ")).strip()
    found = re.match(r"([\d.,]+)\s*([KkMmBb]?)\b", cleaned)
    if not found:
        return None
    digits, letter = found.groups()
    if not letter:
        plain = digits.replace(",", "").replace(".", "")
        return int(plain) if plain.isdigit() else None
    try:
        number = float(digits.replace(",", "."))
    except ValueError:
        return None
    return round(number * {"k": 1e3, "m": 1e6, "b": 1e9}[letter.lower()])


# ---- dates, the dayjs way ----

WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
_ADVANCED = re.compile(r"\[([^\]]+)]|Q|wo|ww|w|WW|W|zzz|z|gggg|GGGG|Do|X|x|k{1,2}|S")
_CORE = re.compile(r"\[([^\]]+)]|YYYY|YY|M{1,4}|D{1,2}|d{1,4}|H{1,2}|h{1,2}|a|A|m{1,2}|s{1,2}|Z{1,2}|SSS")


def ordinal(number: int) -> str:
    """The en locale's ordinal, bracketed the way the format reads it: [1st], [22nd], [11th]."""
    suffixes = ("th", "st", "nd", "rd")
    v = number % 100
    d = v - 20
    remainder = int(math.fmod(d, 10))  # JavaScript's %, which keeps the sign
    if d >= 0 and remainder < 4:
        suffix = suffixes[remainder]
    elif d < 0 and remainder == 0:
        suffix = suffixes[0]
    else:
        suffix = suffixes[v] if v < 4 else suffixes[0]
    return f"[{number}{suffix}]"


def _js_weekday(day: dt.date) -> int:
    return (day.weekday() + 1) % 7  # Sunday 0


def locale_week(day: dt.date) -> int:
    """The en locale's week of the year: weeks start on Sunday, and week 1 holds January 1."""
    if day.month == 12 and day.day > 25:
        saturday = day + dt.timedelta(days=6 - _js_weekday(day))
        if dt.date(day.year + 1, 1, 1) <= saturday:
            return 1
    first = dt.date(day.year, 1, 1)
    start = first - dt.timedelta(days=_js_weekday(first))
    return (day - start).days // 7 + 1


def week_year(day: dt.date) -> int:
    week = locale_week(day)
    if week == 1 and day.month == 12:
        return day.year + 1
    if day.month == 1 and week >= 52:
        return day.year - 1
    return day.year


def zone_for(name: str, server_zone: ZoneInfo) -> dt.tzinfo:
    """A counter's zone setting as a tzinfo: the server's zone, a named zone, or a fixed offset."""
    if name == SERVER_ZONE:
        return server_zone
    found = ZONE_OFFSET.fullmatch(name or "")
    if found:
        minutes = (int(found.group(2)) * 60 + int(found.group(3))) * (-1 if found.group(1) == "-" else 1)
        return dt.timezone(dt.timedelta(minutes=minutes), name)
    return zone(name)


def _zone_text(moment: dt.datetime) -> str:
    offset = moment.utcoffset() or dt.timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def _zone_abbreviation(moment: dt.datetime, long: bool) -> str:
    tz = moment.tzinfo
    if isinstance(tz, ZoneInfo):
        return tz.key if long else (moment.tzname() or tz.key)
    minutes = int((moment.utcoffset() or dt.timedelta(0)).total_seconds() // 60)
    return "UTC" if minutes == 0 else offset_name(minutes)


def format_date(fmt: str, moment: dt.datetime) -> str:
    """A moment, already in its zone, written with dayjs format tokens and the advanced-format plugin's."""
    def advanced(match: re.Match) -> str:
        token = match.group(0)
        day = moment.date()
        if token == "Q":
            return str((moment.month - 1) // 3 + 1)
        if token == "Do":
            return ordinal(moment.day)
        if token == "gggg":
            return str(week_year(day))
        if token == "GGGG":
            return str(day.isocalendar()[0])
        if token == "wo":
            return ordinal(locale_week(day))
        if token in ("w", "ww"):
            return str(locale_week(day)).rjust(1 if token == "w" else 2, "0")
        if token in ("W", "WW"):
            return str(day.isocalendar()[1]).rjust(1 if token == "W" else 2, "0")
        if token in ("k", "kk"):
            return str(moment.hour or 24).rjust(len(token), "0")
        if token == "X":
            return str(math.floor(moment.timestamp()))
        if token == "x":
            return str(math.floor(moment.timestamp() * 1000))
        if token == "z":
            return f"[{_zone_abbreviation(moment, False)}]"
        if token == "zzz":
            return f"[{_zone_abbreviation(moment, True)}]"
        return token

    zone_text = _zone_text(moment)
    weekday = _js_weekday(moment.date())
    hour12 = moment.hour % 12 or 12

    def core(match: re.Match) -> str:
        if match.group(1):
            return match.group(1)
        token = match.group(0)
        found = {
            "YY": str(moment.year)[-2:], "YYYY": str(moment.year).rjust(4, "0"),
            "M": str(moment.month), "MM": f"{moment.month:02d}", "MMM": MONTHS[moment.month - 1][:3], "MMMM": MONTHS[moment.month - 1],
            "D": str(moment.day), "DD": f"{moment.day:02d}",
            "d": str(weekday), "dd": WEEKDAYS[weekday][:2], "ddd": WEEKDAYS[weekday][:3], "dddd": WEEKDAYS[weekday],
            "H": str(moment.hour), "HH": f"{moment.hour:02d}", "h": str(hour12), "hh": f"{hour12:02d}",
            "a": "am" if moment.hour < 12 else "pm", "A": "AM" if moment.hour < 12 else "PM",
            "m": str(moment.minute), "mm": f"{moment.minute:02d}", "s": str(moment.second), "ss": f"{moment.second:02d}",
            "SSS": f"{moment.microsecond // 1000:03d}", "Z": zone_text,
        }.get(token)
        return found if found else zone_text.replace(":", "")  # ZZ, as dayjs reads it

    return _CORE.sub(core, _ADVANCED.sub(advanced, fmt))


# ---- countdowns ----

def countdown_text(counter: dict, now: float) -> tuple[str, bool]:
    """(what the countdown shows, whether it has ended). A unit left out rolls into the next smaller one shown."""
    left = int(counter["end"] - now)
    if left <= 0:
        return counter.get("end_text") or DEFAULT_END_TEXT, True
    units = counter.get("units") or {}
    parts = []
    rest = left
    for key, size in (("days", 86400), ("hours", 3600), ("minutes", 60)):
        if key in units:
            amount, rest = divmod(rest, size)
            parts.append(f"{amount}{units[key]}")
    return "".join(parts).strip() or "0", False


# ---- the schedule and Discord's rename limit ----

def schedule_zone(setup: dict, server_zone: ZoneInfo) -> dt.tzinfo:
    """The zone a setup's updates line up in: its first date counter's zone, or the server's."""
    for counter in setup.get("counters") or []:
        if counter.get("type") == "datetime":
            return zone_for(counter.get("zone") or SERVER_ZONE, server_zone)
    return server_zone


def _offset(tz: dt.tzinfo, moment: float) -> int:
    return int((dt.datetime.fromtimestamp(moment, tz).utcoffset() or dt.timedelta(0)).total_seconds())


def last_boundary(frequency: int, now: float, tz: dt.tzinfo) -> float:
    """The latest update time at or before now: a whole number of `frequency` minutes after midnight in the zone. Where a clock change
    falls inside the step, the boundary that's right on the clock it lands in wins."""
    step = frequency * 60
    candidates = []
    for offset in {_offset(tz, now), _offset(tz, now - step), _offset(tz, now - 86400)}:
        moment = (now + offset) // step * step - offset
        if moment <= now and _offset(tz, moment) == offset:
            candidates.append(moment)
    if candidates:
        return max(candidates)
    offset = _offset(tz, now)
    return (now + offset) // step * step - offset


def next_boundary(frequency: int, now: float, tz: dt.tzinfo) -> float:
    """The first update time after now."""
    step = frequency * 60
    probe = now + step
    found = last_boundary(frequency, probe, tz)
    while found <= now:  # a clock change can leave a long step
        probe += step
        found = last_boundary(frequency, probe, tz)
    return found


def recent_renames(renames: Iterable[float], now: float) -> list[float]:
    return sorted(moment for moment in renames if now - moment < RENAME_WINDOW)


def rename_allowed_at(renames: Iterable[float], now: float) -> float:
    """When the next rename fits within Discord's two per ten minutes. Now, when it already does."""
    recent = recent_renames(renames, now)
    if len(recent) < RENAMES_ALLOWED:
        return now
    return recent[-RENAMES_ALLOWED] + RENAME_WINDOW


# ---- rendering ----

@dataclass
class Reading:
    """What one counter reads. `value` for a number, `words` for a counter that shows words. `note` says why it's old, approximate or missing."""
    value: float | None = None
    words: str | None = None
    note: str | None = None
    ended: bool = False
    kept: bool = False

    def as_json(self) -> dict:
        return {"value": self.value, "words": self.words, "note": self.note, "ended": self.ended, "kept": self.kept}


def shown(counter: dict, reading: Reading) -> str:
    """One counter as it appears in the name."""
    if reading.words is not None:
        return reading.words
    if reading.value is None:
        return UNKNOWN
    value = reading.value
    goal = counter.get("goal")
    if goal is not None:
        value = max(goal - value, 0)
    if counter.get("type") == "stat_change":
        return _change_text(counter, value)
    decimals = counter.get("decimals", 0)
    return format_number(value, counter.get("style", "plain"), decimals)


def _change_text(counter: dict, value: float) -> str:
    if counter.get("as") == "percent":
        if value == math.inf:
            return "+∞%" if counter.get("plus") else "∞%"
        body = format_number(value, "separated" if counter.get("style") == "separated" else "plain", counter.get("decimals", 1)) + "%"
    else:
        body = format_number(value, counter.get("style", "plain"), counter.get("decimals", 0))
    if value > 0 and counter.get("plus") and not body.startswith("+"):
        body = "+" + body
    return body


def clean_name(name: str) -> str:
    """A name Discord takes: one line, no more than 100 characters, never empty."""
    flat = " ".join(name.replace("\n", " ").split())
    if len(flat) > NAME_LIMIT:
        flat = flat[:NAME_LIMIT].rstrip()
    return flat or "Counter"


def render(setup: dict, readings: list[Reading]) -> str:
    """The channel name from the setup's text and each counter's reading. An ended countdown's text takes the whole name."""
    for counter, reading in zip(setup["counters"], readings):
        if counter["type"] == "countdown" and reading.ended:
            return clean_name(reading.words or DEFAULT_END_TEXT)

    def put(match: re.Match) -> str:
        index = int(match.group(1)) - 1
        if 0 <= index < len(readings):
            return shown(setup["counters"][index], readings[index])
        return match.group(0)

    return clean_name(PLACEHOLDER.sub(put, setup["text"]))


def values_from_name(setup: dict, name: str) -> list[float | None] | None:
    """Each number counter's value as an existing channel name shows it, when the name fits the setup's text. Used when adopting a
    channel, so a counter that can't be read yet keeps the number the channel already shows."""
    parts = PLACEHOLDER.split(setup["text"])
    if any(not part.strip() for part in parts[2:-1:2]):
        return None  # two counters with nothing between them can be split more than one way
    pattern = ""
    order: list[int] = []
    for index, part in enumerate(parts):
        if index % 2 == 0:
            pattern += re.escape(" ".join(part.split())).replace(r"\ ", r"\s*")
        else:
            pattern += r"(.+?)"
            order.append(int(part) - 1)
    found = re.fullmatch(r"\s*" + pattern + r"\s*", " ".join(name.split()))
    if not found:
        return None
    values: list[float | None] = [None] * len(setup["counters"])
    for position, raw in zip(order, found.groups()):
        number = short_count(raw.strip().lstrip("+"))
        if number is not None and setup["counters"][position]["type"] not in WORD_TYPES:
            values[position] = float(number)
    return values


# ---- reading counters from Discord ----

def count_members(guild: Any, include: str) -> int:
    total = guild.member_count or len(guild.members)
    if include == "both":
        return total
    bots = sum(1 for member in guild.members if member.bot)
    return bots if include == "bots" else max(total - bots, 0)


def _kept(member: Any, include: str) -> bool:
    return include == "both" or (include == "bots") == bool(member.bot)


def count_channels(guild: Any, types: Iterable[int]) -> int:
    wanted = set(types)
    return sum(1 for channel in guild.channels if getattr(getattr(channel, "type", None), "value", None) in wanted)


def count_roles(guild: Any, managed: bool, unmanaged: bool) -> int:
    return sum(1 for role in guild.roles if not role.is_default() and ((managed and role.managed) or (unmanaged and not role.managed)))


def status_of(member: Any) -> str:
    status = str(getattr(member, "status", "offline"))
    return status if status in STATUS_NAMES else "offline"


def count_statuses(guild: Any, statuses: Iterable[str], include: str) -> int:
    wanted = set(statuses)
    return sum(1 for member in guild.members if _kept(member, include) and status_of(member) in wanted)


def count_in_roles(guild: Any, roles: Iterable[int], statuses: Iterable[str], include: str) -> int:
    wanted = set(roles)
    states = set(statuses)
    found = 0
    for member in guild.members:
        if not _kept(member, include):
            continue
        if not any(role.id in wanted for role in member.roles):
            continue
        if states and status_of(member) not in states:
            continue
        found += 1
    return found


def count_in_voice(guild: Any, mode: str, channels: Iterable[int], include: str) -> int:
    listed = set(channels)
    found = 0
    for channel in [*getattr(guild, "voice_channels", []), *getattr(guild, "stage_channels", [])]:
        named = channel.id in listed or getattr(channel, "category_id", None) in listed
        if listed and (named != (mode == "allow")):
            continue
        found += sum(1 for member in getattr(channel, "members", []) if _kept(member, include))
    return found


def statuses_without_presence(statuses: list[str], counts: tuple[int, int] | None, share: dict[str, float] | None = None) -> Reading:
    """A status counter when the bot doesn't get statuses one by one. Not offline is Discord's count of members present, and offline the
    members who aren't. Online, idle and do not disturb, alone or mixed, split the present count by `share`: how this server's members split
    at this hour of the week, from its last four weeks of real statuses (query.status_shares). With no such split, a count with online in it
    shows the present count, and idle or do not disturb alone keep their last value."""
    wanted = set(statuses)
    if counts is None:
        return Reading(note="Discord's count of members present couldn't be read just now, so this keeps its last value.", kept=True)
    members, present = counts
    if wanted == NOT_OFFLINE:
        return Reading(present, note="From Discord's count of members present, which includes bots.")
    if wanted == {"offline"}:
        return Reading(max(members - present, 0), note="From Discord's counts of members and of members present, which include bots.")
    if wanted == set(STATUS_NAMES):
        return Reading(members)
    if share is not None:
        offline = max(members - present, 0) if "offline" in wanted else 0
        return Reading(offline + round(present * sum(share.get(name, 0.0) for name in wanted & NOT_OFFLINE)), note=SPLIT)
    if "online" in wanted and "offline" not in wanted:
        return Reading(present, note=APPROXIMATE)
    return Reading(note=NO_PRESENCE, kept=True)


# ---- reading counters from stored statistics ----

def period_slots(period: dict, now: float, tz: ZoneInfo) -> tuple[int | None, int]:
    """[first slot, slot after the last) of a period ending now."""
    end = slot_of(now) + 1
    if period.get("all"):
        return None, end
    if period.get("since"):
        day = dt.date.fromisoformat(period["since"])
        start = dt.datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()
        return slot_of(start), end
    seconds = period["amount"] * (3600 if period.get("unit") == "hours" else 86400)
    return slot_of(now - seconds), end


def period_seconds(period: dict, now: float, tz: ZoneInfo, first: int | None) -> float:
    start, end = period_slots(period, now, tz)
    start = first if start is None else start
    if start is None:
        return 0.0
    return max(now - start * SLOT_SECONDS, 1.0)


def people_filters(stat: dict, guild: Any) -> tuple[frozenset[int] | None, frozenset[int]]:
    """(the only people counted, or None for everyone, and the people left out), from the member lists and the roles of current members."""
    allowed: set[int] | None = None
    blocked: set[int] = set()
    members = set(stat.get("members") or [])
    if members:
        if stat.get("member_mode") == "allow":
            allowed = set(members)
        else:
            blocked |= members
    roles_allow = set(stat.get("roles_allow") or [])
    roles_block = set(stat.get("roles_block") or [])
    if (roles_allow or roles_block) and guild is not None:
        holders: set[int] = set()
        for member in guild.members:
            held = {role.id for role in member.roles}
            if roles_allow and held & roles_allow:
                holders.add(member.id)
            if roles_block and held & roles_block:
                blocked.add(member.id)
        if roles_allow:
            allowed = holders if allowed is None else allowed & holders
    return (frozenset(allowed) if allowed is not None else None), frozenset(blocked)


def filters_for(stat: dict, guild: Any) -> query.Filters:
    members_in, members_out = people_filters(stat, guild)
    activities = frozenset(stat.get("activities") or [])
    emoji = frozenset(stat.get("emoji") or [])
    return query.Filters(
        include=stat.get("include", "both"),
        voice_states=frozenset(store.VOICE_STATES[name] for name in stat.get("states") or store.VOICE_STATES),
        channel_mode=stat.get("channel_mode", "block"),
        channels=frozenset(stat.get("channels") or []),
        members_in=members_in,
        members_out=members_out,
        activity_mode="allow" if activities else "block",
        activities=activities,
        emoji_mode=stat.get("emoji_mode", "block"),
        emoji=emoji,
        emoji_uses=frozenset(store.EMOJI_USES[name] for name in stat.get("emoji_uses") or store.EMOJI_USES),
    )


def emoji_clause(stat: dict, guild_id: int) -> tuple[str, list]:
    """SQL for the emoji filters a plain Filters can't hold: types, states and the activity kinds' counterpart for emoji."""
    parts: list[str] = []
    args: list[Any] = []
    states = set(stat.get("emoji_states") or EMOJI_STATES)
    if states != set(EMOJI_STATES):
        conditions = []
        if "used" in states:
            conditions.append("(emoji LIKE 'u:%' OR emoji IN (SELECT emoji FROM stat_emoji_info WHERE guild_id = ? AND deleted_at IS NULL AND available = 1))")
            args.append(guild_id)
        if "deleted" in states:
            conditions.append("emoji IN (SELECT emoji FROM stat_emoji_info WHERE guild_id = ? AND deleted_at IS NOT NULL)")
            args.append(guild_id)
        if "unavailable" in states:
            conditions.append("emoji IN (SELECT emoji FROM stat_emoji_info WHERE guild_id = ? AND deleted_at IS NULL AND available = 0)")
            args.append(guild_id)
        parts.append("(" + " OR ".join(conditions) + ")" if conditions else "0")

    def has(kind: str) -> tuple[str, list]:
        if kind == "server":
            return "(emoji LIKE 'c:%' OR emoji LIKE 's:%')", []
        if kind == "super":
            return "(flags & 1 = 1)", []
        column = {"animated": "animated", "managed": "managed", "restricted": "restricted"}[kind]
        return f"emoji IN (SELECT emoji FROM stat_emoji_info WHERE guild_id = ? AND {column} = 1)", [guild_id]

    allow = stat.get("type_allow") or {}
    if allow.get("types"):
        pieces = [has(kind) for kind in allow["types"]]
        joined = (" AND " if allow.get("match") == "all" else " OR ").join(piece for piece, _ in pieces)
        sticker = "" if "super" in allow["types"] else " OR emoji LIKE 's:%'"  # types never leave stickers out, unless super reactions are asked for
        parts.append(f"(({joined}){sticker})")
        for _, piece_args in pieces:
            args.extend(piece_args)
    block = stat.get("type_block") or {}
    if block.get("types"):
        pieces = [has(kind) for kind in block["types"]]
        joined = (" AND " if block.get("match") == "all" else " OR ").join(piece for piece, _ in pieces)
        parts.append(f"(NOT ({joined}) OR emoji LIKE 's:%')")
        for _, piece_args in pieces:
            args.extend(piece_args)
    return " AND ".join(parts), args


def activity_clause(stat: dict, guild_id: int) -> tuple[str, list]:
    kinds = stat.get("activity_kinds") or list(store.ACTIVITY_KINDS)
    if set(kinds) == set(store.ACTIVITY_KINDS):
        return "", []
    numbers = [store.ACTIVITY_KINDS[name] for name in kinds]
    marks = ", ".join("?" for _ in numbers)
    return f"activity_id IN (SELECT activity_id FROM stat_activity_names WHERE guild_id = ? AND kind IN ({marks}))", [guild_id, *numbers]


async def stat_rows(db: Any, stat: dict, guild: Any, guild_id: int, start: int | None, end: int, columns: list[str], *,
                    only_channels: frozenset[int] | None = None) -> list[tuple]:
    """Sums of a messages, voice, activities or emoji statistic grouped by `columns`, under every filter the statistic names. With
    `only_channels`, messages, voice and emoji count in those channels alone. Activities have no channel."""
    kind = stat["kind"]
    f = filters_for(stat, guild)
    extra, args = "", []
    if kind == "emoji":
        extra, args = emoji_clause(stat, guild_id)
    elif kind == "activities":
        extra, args = activity_clause(stat, guild_id)
    if only_channels is not None and kind in ("messages", "voice", "emoji"):
        marks = ", ".join("?" for _ in only_channels)
        clause = f"channel_id IN ({marks})" if only_channels else "0"
        extra, args = (f"{extra} AND {clause}" if extra else clause), [*args, *sorted(only_channels)]
    if kind == "emoji":
        if stat.get("measure") == "received":
            people_in, people_out = f.members_in, f.members_out
            f = replace(f, members_in=None, members_out=frozenset())
            clause, clause_args = f"use = {store.REACTION} AND target_id != 0", []
            extra = f"{extra} AND {clause}" if extra else clause
            mapped = ["target_id" if column == "user_id" else column for column in columns]
            rows = await query._grouped(db, "emoji", guild_id, f, start, end, mapped, extra=extra, extra_args=[*args, *clause_args])
            if "user_id" in columns and (people_in is not None or people_out):
                at = columns.index("user_id")
                rows = [row for row in rows if (people_in is None or row[at] in people_in) and row[at] not in people_out]
            return rows
    return await query._grouped(db, kind, guild_id, f, start, end, columns, extra=extra, extra_args=args)


async def invite_joins(db: Any, stat: dict, guild: Any, guild_id: int, settings: dict, now: float, start: int | None, end: int | None) -> list[dict]:
    """The joins an invite statistic counts: in the period, through a code its list keeps, by a person its people filters keep, and with
    fake reasons its filters keep. Each row has the inviter's ID."""
    clauses, args = ["guild_id = ?"], [guild_id]
    if start is not None:
        clauses.append("at >= ?")
        args.append(start * SLOT_SECONDS)
    if end is not None:
        clauses.append("at < ?")
        args.append(end * SLOT_SECONDS)
    codes = stat.get("invites") or []
    if codes:
        marks = ", ".join("?" for _ in codes)
        clauses.append(f"code IN ({marks})" if stat.get("invite_mode") == "allow" else f"(code IS NULL OR code NOT IN ({marks}))")
        args.extend(codes)
    rows = await db.all(f"SELECT * FROM stat_joins WHERE {' AND '.join(clauses)}", args)
    members_in, members_out = people_filters(stat, guild)
    include = stat.get("include", "humans")
    allow, block = stat.get("fake_allow") or {}, stat.get("fake_block") or {}
    allow_bits = sum(invite_rules.FAKE_REASONS[name] for name in allow.get("types") or [])
    block_bits = sum(invite_rules.FAKE_REASONS[name] for name in block.get("types") or [])
    out = []
    for row in rows:
        inviter = row["inviter_id"]
        if not inviter:
            continue
        if (members_in is not None and inviter not in members_in) or inviter in members_out:
            continue
        is_bot = bool((row["facts"] or 0) & store.JOIN_BOT)
        if include == "humans" and is_bot or include == "bots" and not is_bot:
            continue
        if allow_bits or block_bits:
            member = guild.get_member(row["user_id"]) if guild is not None and row["user_id"] else None
            bits = invite_rules.reasons(row, settings, now, member)
            if allow_bits and not (bits & allow_bits == allow_bits if allow.get("match") == "all" else bits & allow_bits):
                continue
            if block_bits and (bits & block_bits == block_bits if block.get("match") == "all" else bits & block_bits):
                continue
        out.append(row)
    return out


def to_unit(seconds: float, unit: str) -> float:
    return seconds / TIME_UNITS.get(unit, 60)


# ---- reading any counter ----

@dataclass
class Place:
    """Everything reading a counter can need. `present` is Discord's approximate (members, present) counts, read when the bot doesn't get
    each member's status. `visible` says whether a channel's name may appear in a counter, which everyone in the server sees. `public` is
    the server's channels everyone can see, for a counter whose channel everyone can see: its statistics count in those channels alone, so it
    never shows activity from a channel some members can't see. None for a counter whose channel only some members see."""
    guild: Any
    guild_id: int
    now: float
    tz: ZoneInfo
    db: Any = None
    settings: dict = field(default_factory=dict)
    presences: bool = False
    present: tuple[int, int] | None = None
    social: Any = None
    visible: Any = None
    public: frozenset[int] | None = None
    shares: dict | None = None  # query.status_shares, read when the bot doesn't get each member's status
    memo: dict = field(default_factory=dict)  # rows read for one counter that the next can use, such as the 1st, 2nd and 3rd top chatters

    def share_now(self) -> dict[str, float] | None:
        """The status split for the hour of the week this reads at, in the server's zone, or over every hour when this one has none."""
        if not self.shares:
            return None
        return self.shares.get(hour_and_weekday(slot_of(self.now), self.tz)) or self.shares.get(None)


async def place_rows(place: Place, stat: dict, start: int | None, end: int, columns: list[str]) -> list[tuple]:
    """stat_rows, read once per update for every counter in the server that asks the same thing."""
    key = ("rows", json.dumps(stat, sort_keys=True, default=str), start, end, tuple(columns), place.public is not None)
    if key not in place.memo:
        place.memo[key] = await stat_rows(place.db, stat, place.guild, place.guild_id, start, end, columns, only_channels=place.public)
    return place.memo[key]


async def read(counter: dict, place: Place) -> Reading:
    """What one counter shows now. A counter that can't be read says why, and its caller keeps the value it had."""
    kind = counter["type"]
    guild = place.guild
    if kind == "countdown":
        words, ended = countdown_text(counter, place.now)
        return Reading(words=words, ended=ended)
    if kind == "datetime":
        moment = dt.datetime.fromtimestamp(place.now, zone_for(counter["zone"], place.tz))
        return Reading(words=format_date(counter["format"], moment))
    if kind in SOCIAL_TYPES:
        if place.social is None:
            return Reading(note="Follower counts aren't read here.", kept=True)
        found = await place.social.count(kind, counter["account"], place.now)
        if found.value is None:
            return Reading(note=found.problem or "The count couldn't be read.", kept=True)
        return Reading(float(found.value), note=found.problem)
    if kind in STAT_TYPES:
        return await read_stat(counter, place)
    if guild is None:
        return Reading(note="The bot isn't connected to this server right now.", kept=True)
    if kind == "members":
        return Reading(count_members(guild, counter["include"]))
    if kind == "channels":
        return Reading(count_channels(guild, counter["channel_types"]))
    if kind == "roles":
        return Reading(count_roles(guild, counter["managed"], counter["unmanaged"]))
    if kind == "voice":
        return Reading(count_in_voice(guild, counter["channel_mode"], counter["channels"], counter["include"]))
    if kind == "statuses":
        if place.presences:
            return Reading(count_statuses(guild, counter["statuses"], counter["include"]))
        return statuses_without_presence(counter["statuses"], place.present, place.share_now())
    if kind == "role_members":
        if counter["statuses"] and not place.presences:
            return Reading(note=NO_PRESENCE, kept=True)
        return Reading(count_in_roles(guild, counter["roles"], counter["statuses"], counter["include"]))
    return Reading(note="This counter type isn't known.", kept=True)


async def read_stat(counter: dict, place: Place) -> Reading:
    if place.db is None:
        return Reading(note="Stored statistics can't be read here.", kept=True)
    stat = counter["stat"]
    kind = counter["type"]
    if kind in ("stat_count", "invites"):
        start, end = period_slots(stat["period"], place.now, place.tz)
        value = await stat_total(stat, place, start, end)
        return Reading(_in_unit(stat, counter, value))
    if kind == "stat_average":
        return Reading(await stat_average(counter, place))
    if kind == "stat_change":
        return Reading(await stat_change(counter, place))
    return await stat_top(counter, place)


def _in_unit(stat: dict, counter: dict, value: float) -> float:
    return to_unit(value, counter.get("unit", "hours")) if stat["kind"] in ("voice", "activities") else value


async def stat_total(stat: dict, place: Place, start: int | None, end: int) -> float:
    """The statistic's total over [start, end): messages, seconds of voice or activity, emoji uses, or joins. Statuses and member counts
    read as their average over the stretch."""
    kind = stat["kind"]
    if kind == "invites":
        return float(len(await invite_joins(place.db, stat, place.guild, place.guild_id, place.settings, place.now, start, end)))
    if kind == "statuses":
        return await status_average(place.db, place.guild_id, stat["statuses"], start, end)
    if kind == "members":
        return await member_average(place.db, place.guild_id, start, end)
    rows = await place_rows(place, stat, start, end, [])
    return float(sum(row[-1] or 0 for row in rows))


async def status_average(db: Any, guild_id: int, statuses: Iterable[str], start: int | None, end: int) -> float:
    """The average number of members with these statuses over the stretch. Where only Discord's count of members present was read, it
    stands in when all three statuses are asked for."""
    wanted = [name for name in ("online", "idle", "dnd") if name in set(statuses)]
    clause = "guild_id = ? AND slot < ?" + ("" if start is None else " AND slot >= ?")
    args = [guild_id, end] + ([] if start is None else [start])
    rows = await db.all(f"SELECT samples, online, idle, dnd, present_samples, present FROM stat_status WHERE {clause}", args)
    readings = []
    for row in rows:
        if row["samples"]:
            readings.append(sum(row[name] for name in wanted) / row["samples"])
        elif row["present_samples"] and len(wanted) == 3:
            readings.append(row["present"] / row["present_samples"])
    return sum(readings) / len(readings) if readings else 0.0


async def member_average(db: Any, guild_id: int, start: int | None, end: int) -> float:
    clause = "guild_id = ? AND slot < ? AND members IS NOT NULL" + ("" if start is None else " AND slot >= ?")
    args = [guild_id, end] + ([] if start is None else [start])
    found = await db.one(f"SELECT AVG(members) AS average FROM stat_members WHERE {clause}", args)
    if found and found["average"] is not None:
        return float(found["average"])
    return float(await query.member_count_at(db, guild_id, end) or 0)


async def _first_slot(place: Place, stat: dict) -> int | None:
    kind = stat["kind"]
    if kind == "invites":
        first = await place.db.scalar("SELECT MIN(at) FROM stat_joins WHERE guild_id = ?", (place.guild_id,))
        return slot_of(first) if first is not None else None
    return await query.first_slot(place.db, kind, place.guild_id)


PER_SECONDS = {"hour": 3600, "day": 86400, "week": 7 * 86400}


async def stat_average(counter: dict, place: Place) -> float:
    """Statuses and member counts average their readings. Everything else is its total per hour, day or week of the period."""
    stat = counter["stat"]
    start, end = period_slots(stat["period"], place.now, place.tz)
    total = await stat_total(stat, place, start, end)
    if stat["kind"] in ("statuses", "members"):
        return total
    if start is None:
        start = await _first_slot(place, stat)
        if start is None:
            return 0.0
    seconds = max(place.now - start * SLOT_SECONDS, 1.0)
    per = total / (seconds / PER_SECONDS[counter.get("per", "day")])
    return _in_unit(stat, counter, per)


async def stat_change(counter: dict, place: Place) -> float:
    """The period's value less the one of the same length just before it, or that change as a percentage of the earlier value."""
    stat = counter["stat"]
    start, end = period_slots(stat["period"], place.now, place.tz)
    start = end - 96 if start is None else start
    length = max(end - start, 1)
    earlier = start - length
    if stat["kind"] == "members":
        current = float(await query.member_count_at(place.db, place.guild_id, end) or 0)
        previous = float(await query.member_count_at(place.db, place.guild_id, start) or 0)
    else:
        current = await stat_total(stat, place, start, end)
        previous = await stat_total(stat, place, earlier, start)
    if counter.get("as") == "percent":
        if previous == 0:
            return 0.0 if current == 0 else math.inf
        return (current - previous) / previous * 100
    return _in_unit(stat, counter, current - previous)


def person_name(style: str, member: Any = None, row: dict | None = None) -> str:
    """Someone's name as a counter shows it: their username, their display name, or their nickname here (their display name when they have none)."""
    if member is not None:
        username = member.name
        display = getattr(member, "global_name", None) or member.name
        nick = getattr(member, "nick", None) or display
    elif row is not None:
        username = row["name"]
        display = row.get("display_name") or row["name"]
        nick = row.get("nick") or display
    else:
        return "Someone"
    return {"username": username, "display": display, "nickname": nick}.get(style, display)


async def stat_top(counter: dict, place: Place) -> Reading:
    """The member, channel, emoji or app at a place in the ranking. Ties go to the lower ID, so the place doesn't flicker between updates."""
    stat = counter["stat"]
    start, end = period_slots(stat["period"], place.now, place.tz)
    of = counter["of"]
    totals: dict[Any, float] = {}
    if stat["kind"] == "invites":
        for row in await invite_joins(place.db, stat, place.guild, place.guild_id, place.settings, place.now, start, end):
            totals[row["inviter_id"]] = totals.get(row["inviter_id"], 0) + 1
    else:
        column = {"member": "user_id", "channel": "channel_id", "emoji": "emoji", "activity": "activity_id"}[of]
        for row in await place_rows(place, stat, start, end, [column]):
            if row[-1]:
                totals[row[0]] = totals.get(row[0], 0) + row[-1]
    if of == "member":
        totals.pop(0, None)  # people who chose privacy aren't ranked
    if of == "channel" and place.visible is not None:
        totals = {key: value for key, value in totals.items() if place.visible(key)}
    ordered = sorted(totals.items(), key=lambda item: (-item[1], str(item[0]) if of == "emoji" else item[0]))
    rank = counter["rank"]
    if len(ordered) < rank:
        return Reading(words=counter.get("empty_text") or "Nobody yet")
    key, value = ordered[rank - 1]
    name = await _top_name(of, key, counter, place)
    if counter.get("show_value"):
        timed = stat["kind"] in ("voice", "activities")
        amount = to_unit(value, "hours") if timed else value
        name = f"{name} ({format_number(amount, 'plain', 1 if timed else 0)}{'h' if timed else ''})"
    return Reading(words=name, value=float(value))


async def _top_name(of: str, key: Any, counter: dict, place: Place) -> str:
    guild = place.guild
    if of == "member":
        member = guild.get_member(key) if guild is not None else None
        if member is not None:
            return person_name(counter.get("name", "display"), member)
        rows = await query.people(place.db, place.guild_id, [key])
        return person_name(counter.get("name", "display"), row=rows.get(key))
    if of == "channel":
        channel = guild.get_channel(key) if guild is not None else None
        if channel is not None:
            return channel.name
        rows = await query.channels(place.db, place.guild_id, [key])
        return rows[key]["name"] if key in rows else "A deleted channel"
    if of == "emoji":
        if isinstance(key, str) and key.startswith("u:"):
            from . import emoji as emoji_text
            return emoji_text.display(key[2:])
        info = (await query.emoji_info(place.db, place.guild_id)).get(key)
        return f":{info['name']}:" if info else "A deleted emoji"
    names = await query.activity_names(place.db, place.guild_id, [key])
    return names[key]["name"] if key in names else "An app"
