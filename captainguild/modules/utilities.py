"""Highlights, reminders, giveaways, polls, temporary channels, member lookups, stats and small tools."""
from __future__ import annotations

import asyncio
import calendar
import datetime
import functools
import heapq
import io
import json
import logging
import random
import re
import shlex
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, NamedTuple, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, delivery
from ..core.context import unused_words
from ..core.durations import Duration, humanize, ordinal, parse_duration
from ..core.module import Module, requires
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import Who, evaluate
from .games import INCURSION

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
BLUE = 0x00A2F8
GREEN = 0x53DDAC
RED = 0xDD5F53
DISCORD_TEXT_MAX = 2000

UTC = datetime.timezone.utc
DAY = 86400
TEN_YEARS = 3650 * DAY


# ---- dates a person types ----

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
SHORT_WEEKDAYS = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_CLOCK = re.compile(
    r"(?:^|(?<=\s))(?:at\s+)?(?:"
    r"(?P<word>noon|midnight)"
    r"|(?P<h12>\d{1,2})(?::(?P<m12>\d{2}))?\s*(?P<ampm>[ap])\.?m\.?"
    r"|(?P<h24>\d{1,2}):(?P<m24>\d{2})"
    r")(?=\s|$)"
    r"|(?:^|(?<=\s))at\s+(?P<bare>\d{1,2})(?=\s|$)"
)
_ORDINAL = r"(\d{1,2})(?:st|nd|rd|th)?"
_MONTH_FIRST = re.compile(rf"([a-z]+)\s+{_ORDINAL}(?:\s+(\d{{4}}))?")
_DAY_FIRST = re.compile(rf"{_ORDINAL}\s+(?:of\s+)?([a-z]+)(?:\s+(\d{{4}}))?")
_WINDOW = 8
# Where a date can start in the middle of a sentence, as in "call bob tomorrow about the taxes".
_MARKERS = {"in", "at", "on", "next", "this", "today", "tonight", "tomorrow", "tmrw", "tmr", "noon", "midnight"}


def _read_clock(match: re.Match) -> tuple[int, int] | None:
    if match["word"]:
        return (12, 0) if match["word"] == "noon" else (0, 0)
    if match["ampm"]:
        hour, minute = int(match["h12"]), int(match["m12"] or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return None
        return hour % 12 + (12 if match["ampm"] == "p" else 0), minute
    hour, minute = int(match["h24"] or match["bare"]), int(match["m24"] or 0)
    return (hour, minute) if hour <= 23 and minute <= 59 else None


def _read_span(text: str) -> int | None:
    """Seconds from "in 3 days", "2h30m" or "an hour from now"."""
    text = re.sub(r"^in\s+", "", text)
    text = re.sub(r"\s+from now$", "", text)
    text = re.sub(r"^half an? hour$", "30 minutes", text)
    text = re.sub(r"^an?\s+", "1 ", text)
    return parse_duration(text)


def _add_months(day: datetime.date, months: int) -> datetime.date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _named_date(body: str) -> tuple[int, int, int | None] | None:
    """(month, day, year) from "dec 25", "december 25th 2027" or "25th of december"."""
    match = _MONTH_FIRST.fullmatch(body)
    if match and match.group(1) in MONTHS:
        return MONTHS[match.group(1)], int(match.group(2)), int(match.group(3)) if match.group(3) else None
    match = _DAY_FIRST.fullmatch(body)
    if match and match.group(2) in MONTHS:
        return MONTHS[match.group(2)], int(match.group(1)), int(match.group(3)) if match.group(3) else None
    return None


def _read_day(text: str, now: datetime.datetime, has_clock: bool) -> tuple[datetime.date, str | None] | None:
    """A calendar day and how it moves if it has already passed: by a "week", a "year" or not at all."""
    today = now.date()
    if text == "today":
        return today, None
    if text in ("tomorrow", "tmrw", "tmr"):
        return today + datetime.timedelta(days=1), None
    if text == "next week":
        return today + datetime.timedelta(days=7), None
    if text == "next month":
        return _add_months(today, 1), None
    if text == "next year":
        return _add_months(today, 12), None
    words = text.split()
    modifier = None
    if words and words[0] in ("on", "this", "next"):
        modifier, words = words[0], words[1:]
    if len(words) == 1:
        weekday = WEEKDAYS.get(words[0])
        # "sun" and "sat" are ordinary words, so the short forms need something beside them that says this is a date.
        if weekday is None and (modifier or has_clock):
            weekday = SHORT_WEEKDAYS.get(words[0])
        if weekday is not None:
            if modifier == "next":  # the same weekday in the next calendar week
                return today + datetime.timedelta(days=weekday - now.weekday() + 7), None
            return today + datetime.timedelta(days=(weekday - now.weekday()) % 7), "week"
    if modifier == "next":
        return None
    body = " ".join(words)
    try:
        match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", body)
        if match:
            return datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3))), None
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?", body)
        if match:
            month, day = int(match.group(1)), int(match.group(2))
            if match.group(3):
                year = int(match.group(3))
                return datetime.date(year + 2000 if year < 100 else year, month, day), None
            return datetime.date(today.year, month, day), "year"
        named = _named_date(body)
        if named:
            month, day, year = named
            return datetime.date(year or today.year, month, day), None if year else "year"
    except ValueError:  # a date that doesn't exist, such as February 30
        return None
    return None


def parse_moment(phrase: str, now: datetime.datetime) -> datetime.datetime | None:
    """A whole phrase as one moment in UTC. None if any word of it isn't part of a date or a time."""
    text = " ".join(phrase.lower().replace(",", " ").split())
    if not text:
        return None
    text = re.sub(r"(\d{4}-\d{1,2}-\d{1,2})t(?=\d)", r"\1 ", text)
    span = _read_span(text)
    if span is not None:
        try:
            return now + datetime.timedelta(seconds=span) if span > 0 else None
        except OverflowError:  # a length that runs past the last date there is
            return None
    clock = None
    match = _CLOCK.search(text)
    if match:
        clock = _read_clock(match)
        if clock is None:
            return None
        text = " ".join((text[:match.start()] + " " + text[match.end():]).split())
    if not text:  # only a time: the next time the clock shows it
        moment = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        return moment if moment > now else moment + datetime.timedelta(days=1)
    if text == "tonight":
        hour, minute = clock or (21, 0)
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return moment if moment > now else moment + datetime.timedelta(days=1)
    day = _read_day(text, now, clock is not None)
    if day is None:
        return None
    date, moves = day
    if clock:
        at = datetime.time(clock[0], clock[1], tzinfo=UTC)
    else:
        at = datetime.time(now.hour, now.minute, now.second, tzinfo=UTC)
    moment = datetime.datetime.combine(date, at)
    if moment <= now and moves == "week":
        moment += datetime.timedelta(days=7)
    if moment <= now and moves == "year":
        try:
            moment = moment.replace(year=moment.year + 1)
        except ValueError:  # February 29
            moment = moment.replace(year=moment.year + 1, day=28)
    return moment


def _tidy_about(text: str) -> str:
    """What's left around a date: "remind me to call bob" reads better as "call bob"."""
    text = re.sub(r"^(?:remind\s+me\s+)?(?:to|about|that)\s+", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"^remind\s+me\s*", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+(?:to|about|on|at|in|by)$", "", text, flags=re.IGNORECASE).strip()


def parse_when(text: str, now: datetime.datetime) -> tuple[datetime.datetime, str] | None:
    """Find a date or length of time in a sentence and return it with the words left over. All times are UTC."""
    words = text.split()
    best: tuple[int, datetime.datetime, str] | None = None
    limit = min(len(words), _WINDOW)
    for count in range(limit, 0, -1):
        moment = parse_moment(" ".join(words[:count]), now)
        if moment is not None:
            best = (count, moment, " ".join(words[count:]))
            break
    for count in range(limit, 0, -1):
        if best is not None and count <= best[0]:
            break
        moment = parse_moment(" ".join(words[-count:]), now)
        if moment is not None:
            best = (count, moment, " ".join(words[:-count]))
            break
    if best is None:
        for start in range(1, len(words) - 1):
            if words[start].lower() not in _MARKERS:
                continue
            for count in range(min(len(words) - start, _WINDOW), 0, -1):
                moment = parse_moment(" ".join(words[start:start + count]), now)
                if moment is not None:
                    left = " ".join(words[:start])
                    right = " ".join(words[start + count:])
                    return moment, _tidy_about(f"{left} {right}")
    if best is None:
        return None
    return best[1], _tidy_about(best[2])


# ---- highlight word matching ----

_LINK = re.compile(r"https?://\S+")
_DISCORD_MARKUP = re.compile(r"<[^<>\s]+>")  # mentions, custom emoji and timestamps aren't words someone wrote
_WORD = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*")
_VOWELS = "aeiou"
MAX_HIGHLIGHT_WORDS = 100
MAX_HIGHLIGHT_LENGTH = 100
QUIET_SECONDS = 300
HIGHLIGHT_COOLDOWN = 60


def highlight_tokens(text: str) -> list[str]:
    """A message's words the way a search index sees them: no links, accents, case or punctuation."""
    cleaned = _DISCORD_MARKUP.sub(" ", _LINK.sub(" ", text or ""))
    folded = "".join(c for c in unicodedata.normalize("NFKD", cleaned) if not unicodedata.combining(c)).casefold()
    return [re.sub(r"['\u2019]", "", word) for word in _WORD.findall(folded)]


@functools.lru_cache(maxsize=65536)
def word_forms(word: str) -> frozenset[str]:
    """Spellings that share a root with this word, so "print" finds "prints", "printed" and "printing"."""
    forms = {word}
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        if word.endswith("ies") and len(word) > 4:
            forms.add(word[:-3] + "y")
        else:
            forms.add(word[:-1])
            if word.endswith(("ses", "xes", "zes", "ches", "shes")):
                forms.add(word[:-2])
    for suffix in ("ing", "ed"):
        if not word.endswith(suffix) or len(word) - len(suffix) < 3:
            continue
        stem = word[:-len(suffix)]
        if stem[-1] == stem[-2]:  # running comes from run, and calling from call
            forms.update((stem, stem[:-1]))
        elif len(stem) == 3 and stem[0] not in _VOWELS and stem[1] in _VOWELS and stem[2] not in _VOWELS + "wxy":
            # Short words double the last letter before these endings, so "caring" comes from care and never from car.
            forms.add(stem + "e")
        else:
            forms.update((stem, stem + "e"))
    return frozenset(forms)


class HighlightWord(NamedTuple):
    user_id: int
    word: str
    forms: tuple[frozenset[str], ...]


def compile_word(user_id: int, word: str) -> HighlightWord | None:
    tokens = highlight_tokens(word)
    if not tokens:
        return None
    return HighlightWord(user_id, word, tuple(word_forms(t) for t in tokens))


def build_index(entries: Iterable[HighlightWord]) -> dict[str, list[HighlightWord]]:
    index: dict[str, list[HighlightWord]] = {}
    for entry in entries:
        for form in entry.forms[0]:
            index.setdefault(form, []).append(entry)
    return index


def matching_words(tokens: list[str], index: dict[str, list[HighlightWord]]) -> set[HighlightWord]:
    """Which words appear in these tokens. A word of several parts has to appear in sequence, not as a substring."""
    found: set[HighlightWord] = set()
    for position, token in enumerate(tokens):
        for form in word_forms(token):
            for entry in index.get(form, ()):
                if entry in found or position + len(entry.forms) > len(tokens):
                    continue
                if all(word_forms(tokens[position + step]) & entry.forms[step] for step in range(1, len(entry.forms))):
                    found.add(entry)
    return found


# ---- member dump ----

DEFAULT_DUMP_FORMAT = "%u (%i)"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_DUMP_VARIABLE = re.compile(r"%([unicj%])")
_DUMP_FLAGS = {
    "--format": "format", "-f": "format", "--desc": "desc", "-d": "desc", "--has-roles": "has_roles", "-r": "has_roles",
    "--order": "order", "-o": "order", "--limit": "limit", "-l": "limit", "--has-all-roles": "has_all_roles", "--no-roles": "no_roles",
    "--enumerate": "enumerate", "-e": "enumerate", "--separator": "separator", "-s": "separator", "--dateformat": "dateformat",
}
_DUMP_ORDERS = ("name", "id", "created_at", "joined_at", "nick")


class DumpError(Exception):
    """The query can't run. The text is for the person who typed it."""


@dataclass
class DumpOptions:
    format: str = DEFAULT_DUMP_FORMAT
    desc: bool = False
    has_roles: list[str] = field(default_factory=list)
    has_all_roles: list[str] = field(default_factory=list)
    no_roles: bool = False
    order: str = "name"
    limit: int | None = None
    enumerate: bool = False
    separator: str = "\n"
    dateformat: str = DEFAULT_DATE_FORMAT


def _tokens(query: str) -> list[str]:
    splitter = shlex.shlex(query, posix=True)
    splitter.whitespace_split = True
    splitter.commenters = ""
    splitter.escape = ""  # a format or a role name is free to hold a backslash
    try:
        return list(splitter)
    except ValueError:  # a stray quote, as in a role called Don't
        return query.split()


MAX_NAMED = 25  # these scan every role or member, and share one thread with the whole bot
MAX_SEPARATOR = 10


def parse_dump_query(query: str) -> DumpOptions:
    options = DumpOptions()
    words = _tokens(query or "")
    index = 0
    while index < len(words):
        word = words[index]
        name = _DUMP_FLAGS.get(word)
        if name is None:
            raise DumpError(f"`{word}` isn't a flag I know. Flags include --format, --order, --limit, --has-roles and --no-roles.")
        index += 1
        if name in ("desc", "no_roles", "enumerate"):
            setattr(options, name, True)
            continue
        values: list[str] = []
        while index < len(words) and words[index] not in _DUMP_FLAGS:
            values.append(words[index])
            index += 1
        if not values:
            raise DumpError(f"`{word}` needs a value after it.")
        if name == "limit":
            if len(values) != 1 or not values[0].isdigit() or int(values[0]) < 1:
                raise DumpError("--limit takes a whole number above zero.")
            options.limit = int(values[0])
        elif name == "order":
            if len(values) != 1 or values[0] not in _DUMP_ORDERS:
                raise DumpError(f"--order takes one of {', '.join(_DUMP_ORDERS)}.")
            options.order = values[0]
        elif name in ("has_roles", "has_all_roles"):
            setattr(options, name, values)
        elif name == "separator":
            options.separator = " ".join(values).replace("\\n", "\n").replace("\\t", "\t")
            if len(options.separator) > MAX_SEPARATOR:  # it repeats once per member, which adds up on a big server
                raise DumpError(f"--separator takes at most {MAX_SEPARATOR} characters.")
        elif name == "dateformat":
            options.dateformat = " ".join(values)[:100]
        else:
            options.format = " ".join(values)[:500]
    return options


def _format_date(moment: datetime.datetime | None, pattern: str) -> str:
    if moment is None:
        return "unknown"
    try:
        return moment.strftime(pattern)
    except ValueError as error:  # a format code this platform refuses
        raise DumpError("--dateformat holds a code I can't use. See strftime.org for the list.") from error


def dump_line(member: Any, options: DumpOptions) -> str:
    # One pass, so a member named "%i" can't expand into anything.
    def fill(match: re.Match) -> str:
        code = match.group(1)
        if code == "u":
            return str(member)
        if code == "n":
            return member.display_name
        if code == "i":
            return str(member.id)
        if code == "c":
            return _format_date(member.created_at, options.dateformat)
        if code == "j":
            return _format_date(getattr(member, "joined_at", None), options.dateformat)
        return "%"
    return _DUMP_VARIABLE.sub(fill, options.format)


def _sort_key(order: str) -> Any:
    far_future = datetime.datetime.max.replace(tzinfo=UTC)
    if order == "id":
        return lambda m: m.id
    if order == "created_at":
        return lambda m: m.created_at
    if order == "joined_at":
        return lambda m: getattr(m, "joined_at", None) or far_future
    if order == "nick":
        return lambda m: (m.nick or "").casefold()
    return lambda m: m.name.casefold()


def _find_roles(guild: Any, words: list[str]) -> list[Any]:
    """Roles named by mention, ID or name. An unquoted name of several words is matched longest first."""
    if len(words) > MAX_NAMED:
        raise DumpError(f"Name at most {MAX_NAMED} words of roles at a time.")
    by_name: dict[str, Any] = {}
    for candidate in guild.roles:
        by_name.setdefault(candidate.name.casefold(), candidate)
    roles: list[Any] = []
    index = 0
    while index < len(words):
        match = re.fullmatch(r"<@&(\d+)>|(\d{15,21})", words[index])
        role = guild.get_role(int(match.group(1) or match.group(2))) if match else None
        used = 1
        if role is None:
            for count in range(len(words) - index, 0, -1):
                role = by_name.get(" ".join(words[index:index + count]).lstrip("@").casefold())
                if role is not None:
                    used = count
                    break
        if role is None:
            raise DumpError(f"I can't find a role called `{words[index]}`.")
        roles.append(role)
        index += used
    return roles


def select_members(guild: Any, role: Any, options: DumpOptions) -> list[Any]:
    has_any = {r.id for r in _find_roles(guild, options.has_roles)}
    has_all = {r.id for r in _find_roles(guild, options.has_all_roles)}
    chosen = []
    for member in guild.members:
        held = {r.id for r in member.roles if not r.is_default()}
        if role is not None and role.id not in held:
            continue
        if options.no_roles and held:
            continue
        if has_any and not held & has_any:
            continue
        if has_all - held:
            continue
        chosen.append(member)
    chosen.sort(key=_sort_key(options.order), reverse=options.desc)
    return chosen[:options.limit] if options.limit else chosen


# ---- small helpers ----

_ID_IN_TEXT = re.compile(r"<@!?(\d+)>|<#(\d+)>|\b(\d{15,21})\b")
MIN_REPEAT = 3600
MAX_REMINDERS = 200
MAX_REPEATING = 10  # delivery shares one line with every other timer, so these have the tighter limit
MAX_TEMP_CHANNELS = 25
STATS_COOLDOWN = 15
POLL_CHOICE_MAX = 150
ABOUT_MAX = 1500
REFRESH_SECONDS = 15


def ids_in(text: str | None) -> list[int]:
    out = []
    for match in _ID_IN_TEXT.finditer(text or ""):
        value = int(match.group(1) or match.group(2) or match.group(3))
        if value not in out:
            out.append(value)
    return out


def stamp(seconds: float, style: str = "f") -> str:
    return f"<t:{int(seconds)}:{style}>"


def permission_name(name: str) -> str:
    return name.replace("_", " ").title()


def split_poll(text: str) -> tuple[str, list[str]]:
    """Split one line of text into a question and its choices. The choices follow a question mark, or the separators divide everything."""
    text = text.strip()
    if "?" in text:
        question, rest = text.split("?", 1)
        return question.strip() + "?", split_choices(rest)
    parts = split_choices(text)
    return (parts[0], parts[1:]) if parts else ("", [])


def split_choices(text: str | None) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    separator = "|" if "|" in text else ","
    return [part.strip() for part in text.split(separator) if part.strip()]


class GiveawayView(discord.ui.View):
    """The enter button. The giveaway is found by the message it sits on, so this works for all of them and after a restart."""

    def __init__(self, cog: "Utilities") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Enter giveaway", emoji="\N{PARTY POPPER}", style=discord.ButtonStyle.success, custom_id="captainguild:giveaway:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.giveaway_pressed(interaction)


class EndGiveawayView(discord.ui.View):
    """Asks whether ending early should announce the winners."""

    def __init__(self, cog: "Utilities", giveaway_id: int, author_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.giveaway_id = giveaway_id
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("Only the person who ran this command can use this.", ephemeral=True)
        return False

    async def _finish(self, interaction: discord.Interaction, announce: bool) -> None:
        self.stop()
        row = await self.cog.db.one("SELECT * FROM giveaways WHERE id = ? AND guild_id = ?", (self.giveaway_id, interaction.guild_id))
        if row is None or row["ended_at"]:
            await interaction.response.edit_message(content="That giveaway has already ended.", view=None)
            return
        await interaction.response.defer()
        winners = await self.cog.finish_giveaway(row, announce=announce)
        names = ", ".join(f"<@{w}>" for w in winners) or "nobody, because no one entered"
        await interaction.edit_original_response(content=f"Giveaway ended. Winners: {names}", view=None, allowed_mentions=NO_PINGS)

    @discord.ui.button(label="Announce winners", style=discord.ButtonStyle.success)
    async def announce(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, True)

    @discord.ui.button(label="End without announcing", style=discord.ButtonStyle.secondary)
    async def quietly(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, False)


class Utilities(Module):
    key = "utilities"
    category = "Utilities"
    migrations = [
        """
        CREATE TABLE highlight_words (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id, word)
        );
        CREATE TABLE highlight_blocks (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, target_id)
        );
        CREATE TABLE reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            channel_id INTEGER,
            user_id INTEGER NOT NULL,
            about TEXT NOT NULL,
            created_at REAL NOT NULL,
            due_at REAL NOT NULL,
            repeat_seconds INTEGER,
            copied_from INTEGER,
            jump_url TEXT
        );
        CREATE INDEX reminders_user ON reminders (user_id, due_at);
        CREATE INDEX reminders_guild ON reminders (guild_id, due_at);
        CREATE TABLE giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER,
            host_id INTEGER NOT NULL,
            prize TEXT NOT NULL,
            winners INTEGER NOT NULL,
            created_at REAL NOT NULL,
            ends_at REAL NOT NULL,
            ended_at REAL,
            winner_ids TEXT NOT NULL DEFAULT '[]',
            announced INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX giveaways_guild ON giveaways (guild_id, ended_at, ends_at);
        CREATE INDEX giveaways_message ON giveaways (message_id);
        CREATE TABLE giveaway_entries (
            giveaway_id INTEGER NOT NULL REFERENCES giveaways (id) ON DELETE CASCADE,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            entered_at REAL NOT NULL,
            PRIMARY KEY (giveaway_id, user_id)
        );
        CREATE INDEX giveaway_entries_guild ON giveaway_entries (guild_id);
        CREATE TABLE temp_channels (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            created_by INTEGER NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE INDEX temp_channels_guild ON temp_channels (guild_id, expires_at);
        """,
        # Everyone a reroll has replaced, and who may enter a giveaway.
        """
        ALTER TABLE giveaways ADD COLUMN past_winners TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE giveaways ADD COLUMN required_role INTEGER;
        ALTER TABLE giveaways ADD COLUMN account_days INTEGER;
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._words: dict[int, dict[int, list[HighlightWord]]] = {}
        self._indexes: dict[int, dict[str, list[HighlightWord]]] = {}
        self._blocks: dict[tuple[int, int], set[int]] = {}
        self._last_post: dict[tuple[int, int], float] = {}
        self._last_ping: dict[tuple[int, int, int], float] = {}
        self._refreshing: set[int] = set()
        self._ended_before_restore: dict[int, list[dict]] = {}
        self._tasks: set[asyncio.Task] = set()

    async def on_load(self) -> None:
        self.bot.timers.handler("reminder", self._reminder_due, anywhere=True)  # a reminder goes by DM, so it doesn't need its server
        self.bot.timers.handler("giveaway", self._giveaway_due)
        self.bot.timers.handler("tempchannel", self._tempchannel_due)
        self.bot.add_view(GiveawayView(self))
        self._add_prefix_names()
        await self._load_highlights()
        await self._arm_missing()

    def _add_prefix_names(self) -> None:
        """Lets `.members info @someone` and `.stats usage week` work as they do in slash, where those names come from each group's fallback.

        These join after the cog is built, because a subcommand declared in the class with the fallback's name would take the fallback out of slash as the cog loads.
        """
        for group, name, callback, description in (
            (self.members, "info", Utilities._members_info_text, "Look up a member"),
            (self.stats, "usage", Utilities._stats_usage_text, "Show how often commands are used"),
        ):
            command = commands.HybridCommand(callback, name=name, description=description, with_app_command=False)
            command.cog = self
            group.add_command(command)

    async def before_restore(self, guild_id: int) -> None:
        self._ended_before_restore[guild_id] = await self.db.all("SELECT id, ended_at, winner_ids, announced FROM giveaways WHERE guild_id = ? AND ended_at IS NOT NULL", (guild_id,))

    async def on_guild_restored(self, guild_id: int) -> None:
        # A giveaway that ended after this file was made stays ended, so a restore can't draw its winners a second time.
        for row in self._ended_before_restore.pop(guild_id, []):
            await self.db.run("UPDATE giveaways SET ended_at = ?, winner_ids = ?, announced = ? WHERE guild_id = ? AND id = ? AND ended_at IS NULL", (row["ended_at"], row["winner_ids"], row["announced"], guild_id, row["id"]))
        await self._load_highlights()
        await self._arm_missing()

    async def _arm_missing(self) -> None:
        """A restored backup can bring these back without their timers, and then a reminder would never go off and a giveaway would never end."""
        timers = self.bot.timers
        for row in await self.db.all("SELECT r.id, r.guild_id, r.due_at FROM reminders r LEFT JOIN timers t ON t.kind = 'reminder' AND t.key = 'reminder:' || r.id WHERE t.id IS NULL"):
            await timers.schedule("reminder", row["due_at"], {"id": row["id"]}, guild_id=row["guild_id"], key=f"reminder:{row['id']}")
        for row in await self.db.all("SELECT g.id, g.guild_id, g.ends_at FROM giveaways g LEFT JOIN timers t ON t.kind = 'giveaway' AND t.key = 'giveaway:' || g.id WHERE t.id IS NULL AND (g.ended_at IS NULL OR g.announced = 0)"):
            await timers.schedule("giveaway", row["ends_at"], {"id": row["id"]}, guild_id=row["guild_id"], key=f"giveaway:{row['id']}")
        for row in await self.db.all("SELECT c.channel_id, c.guild_id, c.expires_at FROM temp_channels c LEFT JOIN timers t ON t.kind = 'tempchannel' AND t.key = 'tempchannel:' || c.channel_id WHERE t.id IS NULL"):
            await timers.schedule("tempchannel", row["expires_at"], {"guild_id": row["guild_id"], "channel_id": row["channel_id"]}, guild_id=row["guild_id"], key=f"tempchannel:{row['channel_id']}")

    def cog_unload(self) -> None:
        for task in self._tasks:
            task.cancel()

    # ---- highlight ----

    async def _load_highlights(self) -> None:
        """Everything is read before anything changes, so a failure leaves every server's highlights as they were, and one bad word costs only that word."""
        words: dict = {}
        blocks: dict = {}
        word_rows = await self.db.all("SELECT guild_id, user_id, word FROM highlight_words")
        block_rows = await self.db.all("SELECT guild_id, user_id, target_id FROM highlight_blocks")
        for row in word_rows:
            try:
                entry = compile_word(row["user_id"], row["word"])
            except (TypeError, ValueError):
                log.warning("Skipped a highlight word that can't be read in %s", row["guild_id"])
                continue
            if entry is not None:
                words.setdefault(row["guild_id"], {}).setdefault(row["user_id"], []).append(entry)
        for row in block_rows:
            blocks.setdefault((row["guild_id"], row["user_id"]), set()).add(row["target_id"])
        old = self._words, self._indexes, self._blocks
        self._words, self._indexes, self._blocks = words, {}, blocks
        try:
            for guild_id in list(words):
                self._reindex(guild_id)
        except Exception:
            self._words, self._indexes, self._blocks = old
            raise

    def _reindex(self, guild_id: int) -> None:
        people = self._words.get(guild_id) or {}
        entries = [entry for rows in people.values() for entry in rows]
        if entries:
            self._indexes[guild_id] = build_index(entries)
        else:
            self._indexes.pop(guild_id, None)
            self._words.pop(guild_id, None)

    def _highlight_allowed(self, member: Any, channel: Any) -> bool:
        """Seeing the channel is always needed, and for a private thread that means being in it or holding Manage Threads, since Discord's permissions come from the parent. Past that, the same rule as running /highlight there, with the person's permissions in that channel."""
        permissions = access.permissions_in(member, channel)
        if not access.can_view(member, channel) and not (channel.type == discord.ChannelType.private_thread and permissions.view_channel and channel.get_member(member.id) is not None):
            return False
        parent = getattr(channel, "parent", None) if isinstance(channel, discord.Thread) else None
        place = parent or channel
        who = Who(
            role_ids={role.id for role in member.roles},
            permissions={name for name, on in permissions if on},
            is_owner=member.guild.owner_id == member.id,
        )
        return evaluate(
            self.bot.settings.get(member.guild.id, COMMANDS_MODULE),
            root="highlight",
            required=("manage_messages",),
            who=who,
            channel_id=place.id,
            category_id=getattr(place, "category_id", None),
        ).allowed

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot:
            return
        index = self._indexes.get(guild.id)
        if index is None:
            return
        now = time.time()
        if message.author.id in self._words.get(guild.id, ()):
            self._last_post[(guild.id, message.author.id)] = now
        prefixes = tuple(self.bot.settings.get(guild.id, "core")["prefixes"])
        if prefixes and message.content.startswith(prefixes):  # someone running a command isn't talking about the word
            return
        tokens = highlight_tokens(message.content)
        if not tokens:
            return
        hits: dict[int, list[str]] = {}
        for entry in matching_words(tokens, index):
            hits.setdefault(entry.user_id, []).append(entry.word)
        hits.pop(message.author.id, None)
        for user_id, words in hits.items():
            await self._notify_highlight(message, user_id, sorted(words), now)

    async def _notify_highlight(self, message: discord.Message, user_id: int, words: list[str], now: float) -> None:
        guild = message.guild
        channel = message.channel
        if now - self._last_post.get((guild.id, user_id), 0) < QUIET_SECONDS:
            return
        blocked = self._blocks.get((guild.id, user_id)) or ()
        parent_id = getattr(channel, "parent_id", None)
        if message.author.id in blocked or channel.id in blocked or (parent_id and parent_id in blocked):
            return
        member = guild.get_member(user_id)
        if member is None or not self._highlight_allowed(member, channel):
            return
        cooldown_key = (guild.id, user_id, channel.id)
        if now - self._last_ping.get(cooldown_key, 0) < HIGHLIGHT_COOLDOWN:
            return
        self._last_ping[cooldown_key] = now
        if len(self._last_ping) > 10000:
            cutoff = now - HIGHLIGHT_COOLDOWN
            self._last_ping = {key: at for key, at in self._last_ping.items() if at >= cutoff}
        embed = discord.Embed(title=", ".join(words)[:256], description=await self._highlight_context(message, member), color=BLUE, timestamp=message.created_at)
        embed.add_field(name="Message", value=f"[Jump to it]({message.jump_url})")
        try:
            await member.send(f"A word you follow came up in {channel.mention} in {guild.name}.", embed=embed, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass  # closed DMs are the person's choice

    async def _highlight_context(self, message: discord.Message, member: Any) -> str:
        """The few messages before this one, so the notice makes sense on its own. Someone without Read Message History only gets the one that set it off, which Discord would show them anyway."""
        lines: list[str] = []
        if access.permissions_in(member, message.channel).read_message_history:
            try:
                async for earlier in message.channel.history(limit=3, before=message):
                    lines.append(self._context_line(earlier))
            except discord.HTTPException:
                pass
        lines.reverse()
        lines.append(self._context_line(message))
        return "\n".join(lines)[:4000]

    @staticmethod
    def _context_line(message: Any) -> str:
        text = message.content or ("(attachment)" if message.attachments else "(no text)")
        name = discord.utils.escape_markdown(message.author.display_name)
        return f"**[{message.created_at.strftime('%H:%M:%S')}] {name}:** {text[:300]}"

    @commands.hybrid_group(name="highlight", aliases=["hl"], fallback="show", description="Get a DM when a word you follow comes up")
    @commands.guild_only()
    @requires("manage_messages")
    async def highlight(self, ctx: commands.Context) -> None:
        words = await self.db.all("SELECT word FROM highlight_words WHERE guild_id = ? AND user_id = ? ORDER BY word", (ctx.guild.id, ctx.author.id))
        blocks = await self.db.all("SELECT target_id, kind FROM highlight_blocks WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, ctx.author.id))
        if not words and not blocks:
            await ctx.send("You don't follow any words yet. Add one with `/highlight add`.", ephemeral=True)
            return
        lines = ["**Your words**", *(row["word"] for row in words)]
        if blocks:
            lines.append("**Blocked**")
            lines += [f"<#{row['target_id']}>" if row["kind"] == "channel" else f"<@{row['target_id']}>" for row in blocks]
        await ctx.send("\n".join(lines)[:DISCORD_TEXT_MAX], ephemeral=True, allowed_mentions=NO_PINGS)

    @highlight.command(name="add", aliases=["+"], description="Get a DM when this word or phrase comes up")
    @requires("manage_messages")
    @app_commands.describe(word="A word, or several words in a row")
    async def highlight_add(self, ctx: commands.Context, *, word: str) -> None:
        word = " ".join(word.split())
        if _ID_IN_TEXT.search(word) or "@everyone" in word or "@here" in word:
            await ctx.fail("A highlight can't hold a mention.")
            return
        tokens = highlight_tokens(word)
        if len("".join(tokens)) < 2 or len(word) > MAX_HIGHLIGHT_LENGTH:
            await ctx.fail(f"A highlight needs 2 to {MAX_HIGHLIGHT_LENGTH} characters, with letters or numbers in it.")
            return
        mine = self._words.get(ctx.guild.id, {}).get(ctx.author.id, [])
        if len(mine) >= MAX_HIGHLIGHT_WORDS:
            await ctx.fail(f"You can follow up to {MAX_HIGHLIGHT_WORDS} words. Remove one first.")
            return
        if any(entry.word.casefold() == word.casefold() for entry in mine):
            await ctx.fail("You already follow that.")
            return
        await self.db.run(
            "INSERT INTO highlight_words (guild_id, user_id, word, created_at) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, ctx.author.id, word, time.time()),
        )
        self._words.setdefault(ctx.guild.id, {}).setdefault(ctx.author.id, []).append(compile_word(ctx.author.id, word))
        self._reindex(ctx.guild.id)
        await ctx.ok(f"You'll get a DM when `{discord.utils.escape_markdown(word)}` comes up.")

    @highlight.command(name="remove", aliases=["-", "del"], description="Stop following a word")
    @requires("manage_messages")
    @app_commands.describe(word="The word to stop following")
    async def highlight_remove(self, ctx: commands.Context, *, word: str) -> None:
        mine = self._words.get(ctx.guild.id, {}).get(ctx.author.id, [])
        wanted = " ".join(word.split()).casefold()
        target = next((entry for entry in mine if entry.word.casefold() == wanted), None)
        if target is None:
            await ctx.fail("You don't follow that word.")
            return
        await self.db.run("DELETE FROM highlight_words WHERE guild_id = ? AND user_id = ? AND word = ?", (ctx.guild.id, ctx.author.id, target.word))
        mine.remove(target)
        self._reindex(ctx.guild.id)
        await ctx.ok("Removed.")

    @highlight_remove.autocomplete("word")
    async def _highlight_remove_names(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        mine = self._words.get(interaction.guild_id, {}).get(interaction.user.id, [])
        needle = current.casefold()
        return [app_commands.Choice(name=entry.word[:100], value=entry.word[:100]) for entry in mine if needle in entry.word.casefold()][:25]

    @highlight.command(name="clear", description="Stop following all your words")
    @requires("manage_messages")
    async def highlight_clear(self, ctx: commands.Context) -> None:
        await self.db.run("DELETE FROM highlight_words WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, ctx.author.id))
        self._words.get(ctx.guild.id, {}).pop(ctx.author.id, None)
        self._reindex(ctx.guild.id)
        await ctx.ok("You no longer follow any words here.")

    @highlight.command(name="matches", aliases=["m", "match"], description="Test which of your words a sentence would set off")
    @requires("manage_messages")
    @app_commands.describe(words="The sentence to test")
    async def highlight_matches(self, ctx: commands.Context, *, words: str) -> None:
        mine = self._words.get(ctx.guild.id, {}).get(ctx.author.id, [])
        found = matching_words(highlight_tokens(words), build_index(mine))
        if found:
            await ctx.send("That would set off: " + ", ".join(sorted(entry.word for entry in found)), ephemeral=True, allowed_mentions=NO_PINGS)
        else:
            await ctx.send("That wouldn't set off any of your words.", ephemeral=True)

    def _highlight_targets(self, ctx: commands.Context, text: str | None) -> list[tuple[int, str]] | None:
        """Channels and people named in text. Nothing named means this channel. None means a name wasn't found."""
        if not (text or "").strip():
            return [(ctx.channel.id, "channel")]
        targets: list[tuple[int, str]] = []
        leftover = _ID_IN_TEXT.sub(" ", text).split()
        for value in ids_in(text):
            targets.append((value, "channel" if ctx.guild.get_channel_or_thread(value) is not None else "user"))
        channels: dict[str, int] = {}
        for channel in ctx.guild.channels:
            channels.setdefault(channel.name.casefold(), channel.id)
        people: dict[str, int] = {}
        if any(word.lstrip("#@").casefold() not in channels for word in leftover):  # one pass over the member list however many names there are
            for member in ctx.guild.members:
                people.setdefault(member.name.casefold(), member.id)
                people.setdefault(member.display_name.casefold(), member.id)
        for word in leftover:
            name = word.lstrip("#@").casefold()
            if name in channels:
                targets.append((channels[name], "channel"))
            elif name in people:
                targets.append((people[name], "user"))
            else:
                return None
        return targets

    async def _refuse_many_targets(self, ctx: commands.Context, text: str | None) -> bool:
        if len((text or "").split()) <= MAX_NAMED:
            return False
        await ctx.fail(f"Name at most {MAX_NAMED} channels or people at a time.")
        return True

    @highlight.command(name="block", description="Stop notices from a channel or person")
    @requires("manage_messages")
    @app_commands.describe(blocks="Channels or people, by mention, ID or name. Leave empty for this channel.")
    async def highlight_block(self, ctx: commands.Context, *, blocks: str | None = None) -> None:
        if await self._refuse_many_targets(ctx, blocks):
            return
        targets = self._highlight_targets(ctx, blocks)
        if targets is None:
            await ctx.fail("I can't find one of those channels or people.")
            return
        await self.db.many(
            "INSERT INTO highlight_blocks (guild_id, user_id, target_id, kind) VALUES (?, ?, ?, ?) ON CONFLICT (guild_id, user_id, target_id) DO NOTHING",
            [(ctx.guild.id, ctx.author.id, target, kind) for target, kind in targets],
        )
        self._blocks.setdefault((ctx.guild.id, ctx.author.id), set()).update(target for target, _ in targets)
        await ctx.ok("Blocked. " + ", ".join(f"<#{t}>" if kind == "channel" else f"<@{t}>" for t, kind in targets))

    @highlight.command(name="unblock", description="Allow notices from a channel or person again")
    @requires("manage_messages")
    @app_commands.describe(blocks="Channels or people, by mention, ID or name. Leave empty for this channel.")
    async def highlight_unblock(self, ctx: commands.Context, *, blocks: str | None = None) -> None:
        if await self._refuse_many_targets(ctx, blocks):
            return
        targets = self._highlight_targets(ctx, blocks)
        if targets is None:
            await ctx.fail("I can't find one of those channels or people.")
            return
        await self.db.many(
            "DELETE FROM highlight_blocks WHERE guild_id = ? AND user_id = ? AND target_id = ?",
            [(ctx.guild.id, ctx.author.id, target) for target, _ in targets],
        )
        self._blocks.get((ctx.guild.id, ctx.author.id), set()).difference_update(target for target, _ in targets)
        await ctx.ok("Unblocked.")

    # ---- reminders ----

    @commands.hybrid_group(name="reminder", aliases=["remind", "remindme"], fallback="remindme", description="Get a DM about something later")
    @app_commands.describe(about="What to remind you about. The date can go here too", when="When, such as 2 days, tomorrow at noon or 2026-12-25. Times are UTC")
    async def reminder(self, ctx: commands.Context, *, about: str, when: Optional[str] = None) -> None:
        now = datetime.datetime.now(UTC)
        about = re.sub(r"^remindme\s+", "", about.strip())
        if when:
            due = parse_moment(when, now)
            if due is None:
                await ctx.fail("I can't read that date. Try something like `in 3 days`, `tomorrow at noon` or `2026-12-25`.")
                return
        else:
            found = parse_when(about, now)
            if found is None:
                await ctx.fail("I can't find a date in that. Try something like `call bob in 3 days` or `tomorrow at noon call bob`.")
                return
            due, about = found
        if due <= now:
            await ctx.fail("That time has already passed.")
            return
        if due.timestamp() - now.timestamp() > TEN_YEARS:
            await ctx.fail("A reminder can be at most ten years away.")
            return
        about = about.strip() or "something"
        if len(about) > ABOUT_MAX:
            await ctx.fail(f"A reminder can hold up to {ABOUT_MAX} characters.")
            return
        if await self._refuse_reminder_room(ctx, repeating=False):
            return
        message = getattr(ctx, "message", None)
        jump = getattr(message, "jump_url", None) if ctx.interaction is None else None
        reminder_id = await self._add_reminder(
            guild_id=ctx.guild.id if ctx.guild else None, channel_id=ctx.channel.id if ctx.guild else None,
            user_id=ctx.author.id, about=about, due_at=due.timestamp(), jump_url=jump,
        )
        await ctx.ok(f"Reminder {reminder_id} is set for {stamp(due.timestamp())} ({stamp(due.timestamp(), 'R')}): {about}")

    async def _add_reminder(self, *, guild_id: int | None, channel_id: int | None, user_id: int, about: str, due_at: float,
                            jump_url: str | None = None, repeat_seconds: int | None = None, copied_from: int | None = None) -> int:
        reminder_id = await self.db.insert(
            "INSERT INTO reminders (guild_id, channel_id, user_id, about, created_at, due_at, repeat_seconds, copied_from, jump_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, user_id, about, time.time(), due_at, repeat_seconds, copied_from, jump_url),
        )
        await self.bot.timers.schedule("reminder", due_at, {"id": reminder_id}, guild_id=guild_id, key=f"reminder:{reminder_id}")
        return reminder_id

    async def _find_user(self, reminder: dict) -> Any:
        guild = self.bot.get_guild(reminder["guild_id"]) if reminder["guild_id"] else None
        user = guild.get_member(reminder["user_id"]) if guild else None
        user = user or self.bot.get_user(reminder["user_id"])
        if user is not None:
            return user
        try:
            return await self.bot.fetch_user(reminder["user_id"])
        except discord.NotFound:
            return None

    async def _deliver_reminder(self, reminder: dict) -> None:
        user = await self._find_user(reminder)
        if user is None:
            return
        embed = discord.Embed(title="Reminder", description=reminder["about"], color=BLUE)
        embed.add_field(name="Set", value=stamp(reminder["created_at"], "R"))
        if reminder["jump_url"]:
            embed.add_field(name="Where", value=f"[Jump to it]({reminder['jump_url']})")
        try:
            await user.send(embed=embed, allowed_mentions=NO_PINGS)
            return
        except (discord.Forbidden, discord.NotFound):
            pass  # closed DMs, so they get word in the channel it was set in
        await self._tell_dms_closed(reminder)

    async def _tell_dms_closed(self, reminder: dict) -> None:
        """A fixed sentence in the channel, and only where the person could post themselves. Their own words never go there, or a reminder would be a way to post past a mute or a read-only channel."""
        guild = self.bot.get_guild(reminder["guild_id"]) if reminder["guild_id"] else None
        member = guild.get_member(reminder["user_id"]) if guild else None
        channel = await delivery.find_channel(guild, reminder["channel_id"]) if member and reminder["channel_id"] else None
        if channel is None or not access.can_post(member, channel):
            log.info("Reminder %s had nowhere to go", reminder["id"])
            return
        where = f" You set it here: {reminder['jump_url']}" if reminder["jump_url"] else ""
        try:
            await channel.send(f"{member.mention} A reminder of yours went off, but your DMs are closed, so the bot couldn't send it.{where}", allowed_mentions=discord.AllowedMentions(users=[member], everyone=False, roles=False))
        except (discord.Forbidden, discord.NotFound):
            log.info("Reminder %s had nowhere to go", reminder["id"])

    async def _reminder_due(self, payload: dict, row: dict) -> None:
        reminder = await self.db.one("SELECT * FROM reminders WHERE id = ?", (payload["id"],))
        if reminder is None:
            return
        await self._deliver_reminder(reminder)
        repeat = reminder["repeat_seconds"]
        if not repeat or repeat < MIN_REPEAT:  # nothing shorter can be set here, so only an old or edited backup file says otherwise
            await self.db.run("DELETE FROM reminders WHERE id = ?", (reminder["id"],))
            return
        now = time.time()
        next_due = reminder["due_at"] + repeat
        if next_due <= now:  # the bot was down, so skip the ones it missed
            next_due += repeat * (int((now - next_due) // repeat) + 1)
        await self.db.run("UPDATE reminders SET due_at = ? WHERE id = ?", (next_due, reminder["id"]))
        await self.bot.timers.schedule("reminder", next_due, {"id": reminder["id"]}, guild_id=reminder["guild_id"], key=f"reminder:{reminder['id']}")

    async def _visible_reminder(self, ctx: commands.Context, reminder_id: int) -> dict | None:
        """Your own. Past that, one typed in the open in this server, in a channel you can read, since the command itself is sitting there. A reminder made with the slash command was only ever shown to the person who made it, so IDs can't be counted to read it."""
        if ctx.guild is None:
            return await self.db.one("SELECT * FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, ctx.author.id))
        row = await self.db.one("SELECT * FROM reminders WHERE id = ? AND guild_id = ?", (reminder_id, ctx.guild.id))
        if row is None or row["user_id"] == ctx.author.id:
            return row
        channel = ctx.guild.get_channel_or_thread(row["channel_id"]) if row["jump_url"] and row["channel_id"] else None
        return row if channel is not None and access.can_read(ctx.author, channel) else None

    async def _refuse_reminder_room(self, ctx: commands.Context, *, repeating: bool) -> bool:
        """Whether this person has no room for one more reminder, or one more that repeats."""
        if await self.db.scalar("SELECT COUNT(*) FROM reminders WHERE user_id = ?", (ctx.author.id,)) >= MAX_REMINDERS:
            await ctx.fail(f"You have {MAX_REMINDERS} reminders waiting. Cancel some first.")
            return True
        if repeating and await self.db.scalar("SELECT COUNT(*) FROM reminders WHERE user_id = ? AND repeat_seconds IS NOT NULL", (ctx.author.id,)) >= MAX_REPEATING:
            await ctx.fail(f"You have {MAX_REPEATING} repeating reminders. Stop one first.")
            return True
        return False

    @reminder.command(name="incursion", description="Get a DM when the next World of Warcraft incursion starts")
    @app_commands.describe(region="Which region's timer")
    async def reminder_incursion(self, ctx: commands.Context, region: Literal["eu", "na"]) -> None:
        if await self._refuse_reminder_room(ctx, repeating=False):
            return
        name = region.upper()
        due_at = INCURSION.next_start(name, time.time())
        about = f"The next {name} World of Warcraft incursion is starting."
        reminder_id = await self._add_reminder(guild_id=ctx.guild.id if ctx.guild else None, channel_id=ctx.channel.id if ctx.guild else None, user_id=ctx.author.id, about=about, due_at=due_at)
        await ctx.ok(f"Reminder {reminder_id} is set for {stamp(due_at)} ({stamp(due_at, 'R')}): {about}")

    @reminder.command(name="mine", description="Show your reminders")
    async def reminder_mine(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            rows = await self.db.all("SELECT * FROM reminders WHERE user_id = ? ORDER BY due_at LIMIT 25", (ctx.author.id,))
        else:
            rows = await self.db.all("SELECT * FROM reminders WHERE user_id = ? AND guild_id = ? ORDER BY due_at LIMIT 25", (ctx.author.id, ctx.guild.id))
        if not rows:
            await ctx.send("You have no reminders waiting here.", ephemeral=True)
            return
        lines = []
        for row in rows:
            repeat = f" (repeats every {humanize(row['repeat_seconds'])})" if row["repeat_seconds"] else ""
            lines.append(f"**{row['id']}** {stamp(row['due_at'], 'R')}{repeat}: {row['about'][:120]}")
        embed = discord.Embed(title="Your reminders", description="\n".join(lines)[:4000], color=BLUE)
        await ctx.send(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @reminder.command(name="cancel", aliases=["-", "remove", "del"], description="Remove one of your reminders")
    @app_commands.describe(id="The reminder's ID, from /reminder mine")
    async def reminder_cancel(self, ctx: commands.Context, id: int) -> None:
        removed = await self.db.run("DELETE FROM reminders WHERE id = ? AND user_id = ?", (id, ctx.author.id))
        if not removed:
            await ctx.fail("You don't have a reminder with that ID.")
            return
        await self.bot.timers.cancel_key("reminder", f"reminder:{id}")
        await ctx.ok(f"Reminder {id} is canceled.")

    @reminder.command(name="clear", description="Remove all your reminders in this server, or everywhere when used in DMs")
    async def reminder_clear(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            rows = await self.db.all("SELECT id FROM reminders WHERE user_id = ?", (ctx.author.id,))
        else:
            rows = await self.db.all("SELECT id FROM reminders WHERE user_id = ? AND guild_id = ?", (ctx.author.id, ctx.guild.id))
        for row in rows:
            await self.db.run("DELETE FROM reminders WHERE id = ?", (row["id"],))
            await self.bot.timers.cancel_key("reminder", f"reminder:{row['id']}")
        await ctx.ok(f"Removed {len(rows)} reminder{'' if len(rows) == 1 else 's'}.")

    @reminder.command(name="subscribe", description="Copy a reminder someone typed in a channel you can read")
    @app_commands.describe(id="The reminder's ID")
    async def reminder_subscribe(self, ctx: commands.Context, id: int) -> None:
        source = await self._visible_reminder(ctx, id)
        if source is None:
            await ctx.fail("There's no reminder with that ID here.")
            return
        if source["user_id"] == ctx.author.id:
            await ctx.fail("That reminder is already yours.")
            return
        already = await self.db.scalar("SELECT id FROM reminders WHERE user_id = ? AND copied_from = ?", (ctx.author.id, id))
        if already is not None:
            await ctx.fail(f"You already have a copy of it, reminder {already}.")
            return
        if await self._refuse_reminder_room(ctx, repeating=bool(source["repeat_seconds"])):
            return
        copied = await self._add_reminder(
            guild_id=source["guild_id"], channel_id=source["channel_id"], user_id=ctx.author.id, about=source["about"],
            due_at=source["due_at"], jump_url=source["jump_url"], repeat_seconds=source["repeat_seconds"], copied_from=id,
        )
        await ctx.ok(f"Reminder {copied} is your copy. It stays even if the original is removed.")

    @reminder.command(name="repeat", description="Make one of your reminders repeat")
    @app_commands.describe(rm_id="The reminder's ID", duration="How often, such as 1 week. Use off to stop. Empty means the wait you set first.")
    async def reminder_repeat(self, ctx: commands.Context, rm_id: int, *, duration: Optional[str] = None) -> None:
        row = await self.db.one("SELECT * FROM reminders WHERE id = ? AND user_id = ?", (rm_id, ctx.author.id))
        if row is None:
            await ctx.fail("You don't have a reminder with that ID.")
            return
        if duration and duration.strip().lower() in ("off", "stop", "none", "never"):
            await self.db.run("UPDATE reminders SET repeat_seconds = NULL WHERE id = ?", (rm_id,))
            await ctx.ok(f"Reminder {rm_id} no longer repeats.")
            return
        if duration:
            seconds = parse_duration(duration)
            if seconds is None:
                await ctx.fail("That isn't a length of time. Try something like `1 week` or `12h`.")
                return
        else:
            seconds = int(row["due_at"] - row["created_at"])
        if seconds < MIN_REPEAT:
            await ctx.fail(f"A reminder can repeat at most once every {humanize(MIN_REPEAT)}.")
            return
        if not row["repeat_seconds"] and await self._refuse_reminder_room(ctx, repeating=True):
            return
        await self.db.run("UPDATE reminders SET repeat_seconds = ? WHERE id = ?", (seconds, rm_id))
        await ctx.ok(f"Reminder {rm_id} now repeats every {humanize(seconds)}. The next one is {stamp(row['due_at'], 'R')}.")

    @reminder.command(name="when", description="Show details of your reminder, or one typed in a channel you can read")
    @app_commands.describe(reminder_id="The reminder's ID")
    async def reminder_when(self, ctx: commands.Context, reminder_id: int) -> None:
        row = await self._visible_reminder(ctx, reminder_id)
        if row is None:
            await ctx.fail("There's no reminder with that ID here.")
            return
        if row["user_id"] != ctx.author.id and ctx.interaction is None and ctx.channel.id != row["channel_id"]:
            await ctx.fail(f"Ask in <#{row['channel_id']}>, or use the slash command, so the answer doesn't carry it into this channel.")  # a typed command answers in public
            return
        embed = discord.Embed(title=f"Reminder {row['id']}", description=row["about"], color=BLUE)
        embed.add_field(name="Goes off", value=f"{stamp(row['due_at'])} ({stamp(row['due_at'], 'R')})")
        embed.add_field(name="Set by", value=f"<@{row['user_id']}>")
        embed.add_field(name="Set", value=stamp(row["created_at"], "R"))
        if row["repeat_seconds"]:
            embed.add_field(name="Repeats every", value=humanize(row["repeat_seconds"]))
        await ctx.send(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    # ---- giveaways ----

    async def _list_subcommands(self, ctx: commands.Context, group: commands.Group) -> None:
        lines = [f"`/{c.qualified_name}`: {c.description or c.short_doc}" for c in sorted(group.commands, key=lambda c: c.name)]
        await ctx.send("\n".join(lines)[:DISCORD_TEXT_MAX], ephemeral=True, allowed_mentions=NO_PINGS)

    @staticmethod
    def _giveaway_embed(row: dict, entries: int, winners: list[int] | None = None) -> discord.Embed:
        ended = row["ended_at"] is not None or winners is not None
        lines = []
        if ended:
            names = ", ".join(f"<@{w}>" for w in winners or []) or "nobody"
            lines.append(f"Ended {stamp(row['ended_at'] or time.time(), 'R')}. Winner{'' if len(winners or []) == 1 else 's'}: {names}")
        else:
            lines.append(f"Press the button to enter. Press it again to leave.\nEnds {stamp(row['ends_at'], 'R')} ({stamp(row['ends_at'])})")
            lines.append(f"Winners: {row['winners']}")
            if row.get("required_role"):
                lines.append(f"Needs <@&{row['required_role']}>")
            if row.get("account_days"):
                lines.append(f"Accounts must be at least {row['account_days']} days old")
        lines.append(f"Hosted by <@{row['host_id']}>")
        lines.append(f"Entries: {entries}")
        embed = discord.Embed(title=row["prize"][:256], description="\n".join(lines), color=RED if ended else GREEN)
        embed.set_footer(text=f"Giveaway {row['id']}")
        return embed

    async def _entry_count(self, giveaway_id: int) -> int:
        return await self.db.scalar("SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,))

    async def _giveaway_channel(self, row: dict) -> Any:
        guild = self.bot.get_guild(row["guild_id"])
        return await delivery.find_channel(guild, row["channel_id"]) if guild else None

    @commands.hybrid_group(name="giveaway", description="Run giveaways with an enter button")
    @commands.guild_only()
    @requires("manage_guild")
    async def giveaway(self, ctx: commands.Context) -> None:
        await self._list_subcommands(ctx, self.giveaway)

    @giveaway.command(name="create", description="Start a giveaway")
    @requires("manage_guild")
    @app_commands.describe(duration="How long it runs, such as 2 days", winners="How many people win", prize="What they win", channel="Where to post it. Leave empty for this channel.", role="Only people with this role can enter", account_days="Only accounts at least this many days old can enter")
    async def giveaway_create(self, ctx: commands.Context, duration: Duration, winners: commands.Range[int, 1, 50], *, prize: str, channel: Optional[discord.TextChannel] = None, role: Optional[discord.Role] = None, account_days: Optional[commands.Range[int, 1, 3650]] = None) -> None:
        target = channel
        if target is None and ctx.interaction is None:  # prefix text ends in the channel: .giveaway create 1d 1 A prize #channel
            match = re.search(r"\s*<#(\d+)>\s*$", prize)
            found = ctx.guild.get_channel(int(match.group(1))) if match else None
            if found is not None and hasattr(found, "send"):
                target = found
                prize = prize[:match.start()]
        target = target or ctx.channel
        prize = prize.strip()
        if not prize or len(prize) > 256:
            await ctx.fail("A prize needs 1 to 256 characters.")
            return
        if not 60 <= duration <= 365 * DAY:
            await ctx.fail("A giveaway can run from one minute to one year.")
            return
        refusal = access.post_refusal(ctx.author, target)
        if refusal:
            await ctx.fail(refusal)
            return
        now = time.time()
        ends_at = now + duration
        giveaway_id = await self.db.insert(
            "INSERT INTO giveaways (guild_id, channel_id, host_id, prize, winners, created_at, ends_at, required_role, account_days) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ctx.guild.id, target.id, ctx.author.id, prize, winners, now, ends_at, role.id if role and not role.is_default() else None, account_days),
        )
        row = await self.db.one("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
        try:
            message = await target.send(embed=self._giveaway_embed(row, 0), view=GiveawayView(self), allowed_mentions=NO_PINGS)
        except discord.HTTPException as error:
            await self.db.run("DELETE FROM giveaways WHERE id = ?", (giveaway_id,))
            if isinstance(error, discord.Forbidden):
                await ctx.fail(f"I can't post in {target.mention}. I need View Channel, Send Messages and Embed Links there.")
            else:
                await ctx.fail(f"Discord refused the giveaway: {error.text or error.status}. Try again.")
            return
        await self.db.run("UPDATE giveaways SET message_id = ? WHERE id = ?", (message.id, giveaway_id))
        await self.bot.timers.schedule("giveaway", ends_at, {"id": giveaway_id}, guild_id=ctx.guild.id, key=f"giveaway:{giveaway_id}")
        await ctx.ok(f"Giveaway {giveaway_id} is running in {target.mention} until {stamp(ends_at)}.")

    async def giveaway_pressed(self, interaction: discord.Interaction) -> None:
        row = await self.db.one("SELECT * FROM giveaways WHERE message_id = ? AND guild_id = ?", (interaction.message.id, interaction.guild_id))
        if row is None:
            await interaction.response.send_message("This giveaway no longer exists.", ephemeral=True)
            return
        if row["ended_at"] or time.time() >= row["ends_at"]:
            await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
            return
        already = await self.db.scalar("SELECT 1 FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?", (row["id"], interaction.user.id))
        refusal = None if already else self._entry_refusal(row, interaction.user)  # someone who no longer qualifies can still leave
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True, allowed_mentions=NO_PINGS)
            return
        added = await self.db.run(
            "INSERT INTO giveaway_entries (giveaway_id, guild_id, user_id, entered_at) VALUES (?, ?, ?, ?) ON CONFLICT (giveaway_id, user_id) DO NOTHING",
            (row["id"], row["guild_id"], interaction.user.id, time.time()),
        )
        if added:
            await interaction.response.send_message("You're in. Press the button again to leave.", ephemeral=True)
        else:
            await self.db.run("DELETE FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?", (row["id"], interaction.user.id))
            await interaction.response.send_message("You've left this giveaway.", ephemeral=True)
        self._refresh_soon(row["id"])

    @staticmethod
    def _entry_refusal(row: dict, member: Any) -> str | None:
        if row["required_role"] and not any(role.id == row["required_role"] for role in getattr(member, "roles", ())):
            return f"This giveaway is for people with <@&{row['required_role']}>."
        if row["account_days"] and (discord.utils.utcnow() - member.created_at).days < row["account_days"]:
            return f"This giveaway is for accounts at least {row['account_days']} days old."
        return None

    def _refresh_soon(self, giveaway_id: int) -> None:
        """Keep the entry count fresh, without one edit per press. A busy giveaway would hit Discord's limit on edits."""
        if giveaway_id in self._refreshing:
            return
        self._refreshing.add(giveaway_id)
        task = asyncio.ensure_future(self._refresh_later(giveaway_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _refresh_later(self, giveaway_id: int) -> None:
        try:
            await asyncio.sleep(REFRESH_SECONDS)
        finally:
            self._refreshing.discard(giveaway_id)
        row = await self.db.one("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
        if row is None or row["ended_at"]:
            return
        try:
            await self._edit_giveaway(row, self._giveaway_embed(row, await self._entry_count(giveaway_id)), keep_button=True)
        except discord.HTTPException:  # only a count on a message, and the next press or the end of the giveaway fixes it
            pass

    async def _edit_giveaway(self, row: dict, embed: discord.Embed, *, keep_button: bool) -> bool:
        channel = await self._giveaway_channel(row)
        if channel is None or not row["message_id"]:
            return False
        kwargs: dict[str, Any] = {"embed": embed, "allowed_mentions": NO_PINGS}
        if not keep_button:
            kwargs["view"] = None
        try:
            await channel.get_partial_message(row["message_id"]).edit(**kwargs)
            return True
        except (discord.NotFound, discord.Forbidden):
            return False

    async def _draw(self, row: dict, count: int, exclude: set[int] = frozenset()) -> list[int]:
        """Pick from those who entered and are still in the server. One lost a winner slot to a person who left isn't fair to anyone."""
        entries = await self.db.all("SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?", (row["id"],))
        guild = self.bot.get_guild(row["guild_id"])
        pool = [e["user_id"] for e in entries if e["user_id"] not in exclude and (guild is None or guild.get_member(e["user_id"]) is not None)]
        return random.SystemRandom().sample(pool, min(count, len(pool)))

    async def finish_giveaway(self, row: dict, *, announce: bool = True) -> list[int]:
        """End it and pick winners. Winners are stored before anything is posted, so a retry after a crash announces the same people."""
        if row["ended_at"]:
            winners = json.loads(row["winner_ids"])
        else:
            winners = await self._draw(row, row["winners"])
            await self.db.run(
                "UPDATE giveaways SET ended_at = ?, winner_ids = ?, announced = ? WHERE id = ? AND ended_at IS NULL",
                (time.time(), json.dumps(winners), 0 if announce else 1, row["id"]),
            )
            row = await self.db.one("SELECT * FROM giveaways WHERE id = ?", (row["id"],))
            winners = json.loads(row["winner_ids"])
        embed = self._giveaway_embed(row, await self._entry_count(row["id"]), winners)
        await self._edit_giveaway(row, embed, keep_button=False)
        if not row["announced"]:
            await self._announce(row, winners)
            await self.db.run("UPDATE giveaways SET announced = 1 WHERE id = ?", (row["id"],))
        return winners

    async def _announce(self, row: dict, winners: list[int], *, reroll: bool = False) -> None:
        channel = await self._giveaway_channel(row)
        if channel is None:
            return
        if winners:
            names = " ".join(f"<@{w}>" for w in winners)
            lead = "New winner" if reroll else "Congratulations"
            text = f"{lead}: {names}. Prize: **{row['prize']}**"
        else:
            text = f"Nobody entered the giveaway for **{row['prize']}**, so it has no winner."
        try:
            await channel.send(text[:DISCORD_TEXT_MAX], allowed_mentions=discord.AllowedMentions(users=[discord.Object(w) for w in winners], everyone=False, roles=False))
        except (discord.NotFound, discord.Forbidden):
            log.info("Could not announce giveaway %s in its channel", row["id"])

    async def _giveaway_due(self, payload: dict, row: dict) -> None:
        giveaway = await self.db.one("SELECT * FROM giveaways WHERE id = ?", (payload["id"],))
        if giveaway is None or (giveaway["ended_at"] and giveaway["announced"]):
            return
        await self.finish_giveaway(giveaway)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self._giveaway_messages_gone(payload.guild_id, [payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        await self._giveaway_messages_gone(payload.guild_id, list(payload.message_ids))

    async def _giveaway_messages_gone(self, guild_id: int | None, message_ids: list[int]) -> None:
        """With its message gone, nobody can enter a giveaway or see who won, so it ends with no winner and the host hears why."""
        if guild_id is None:
            return
        marks = ",".join("?" * len(message_ids))
        rows = await self.db.all(f"SELECT * FROM giveaways WHERE guild_id = ? AND ended_at IS NULL AND message_id IN ({marks})", (guild_id, *message_ids))
        for row in rows:
            await self.db.run("UPDATE giveaways SET ended_at = ?, announced = 1 WHERE id = ?", (time.time(), row["id"]))
            await self.bot.timers.cancel_key("giveaway", f"giveaway:{row['id']}")
            guild = self.bot.get_guild(guild_id)
            host = guild.get_member(row["host_id"]) if guild else None
            if host is not None:
                try:
                    await host.send(f"The message for your giveaway in {guild.name} was deleted, so it ended with no winner. The prize was: {row['prize']}", allowed_mentions=NO_PINGS)
                except discord.HTTPException:
                    log.info("Could not tell the host of giveaway %s that its message was deleted", row["id"])

    async def _own_giveaway(self, ctx: commands.Context, giveaway_id: int) -> dict | None:
        row = await self.db.one("SELECT * FROM giveaways WHERE id = ? AND guild_id = ?", (giveaway_id, ctx.guild.id))
        if row is None:
            await ctx.fail("There's no giveaway with that ID here.")
        return row

    @giveaway.command(name="end", description="End a running giveaway early")
    @requires("manage_guild")
    @app_commands.describe(giveaway_id="The giveaway's ID, from /giveaway list")
    async def giveaway_end(self, ctx: commands.Context, giveaway_id: int) -> None:
        row = await self._own_giveaway(ctx, giveaway_id)
        if row is None:
            return
        if row["ended_at"]:
            await ctx.fail("That giveaway has already ended.")
            return
        view = EndGiveawayView(self, giveaway_id, ctx.author.id)
        await ctx.send(f"End the giveaway for **{row['prize']}** now?", view=view, ephemeral=True, allowed_mentions=NO_PINGS)

    @giveaway.command(name="reroll", description="Pick new winners for a giveaway that has ended")
    @requires("manage_guild")
    @app_commands.describe(giveaway_id="The giveaway's ID", replace="The winners to replace. Empty replaces all of them.", exclude="People who can't win this reroll")
    async def giveaway_reroll(self, ctx: commands.Context, giveaway_id: int, replace: Optional[str] = None, *, exclude: Optional[str] = None) -> None:
        row = await self._own_giveaway(ctx, giveaway_id)
        if row is None:
            return
        if not row["ended_at"]:
            await ctx.fail("That giveaway is still running. End it first.")
            return
        current = json.loads(row["winner_ids"])
        chosen = ids_in(replace)
        strangers = [user for user in chosen if user not in current]
        if strangers:
            await ctx.fail(f"<@{strangers[0]}> isn't a winner of this giveaway.")
            return
        leaving = chosen or current
        staying = [winner for winner in current if winner not in leaving]
        wanted = len(leaving) or row["winners"]
        replaced = json.loads(row["past_winners"])  # someone already replaced can't come back in a later reroll
        fresh = await self._draw(row, wanted, set(current) | set(replaced) | set(ids_in(exclude)))
        if not fresh:
            await ctx.fail("Nobody else is left to pick from.")
            return
        winners = staying + fresh
        await self.db.run("UPDATE giveaways SET winner_ids = ?, past_winners = ? WHERE id = ?", (json.dumps(winners), json.dumps(sorted(set(replaced) | set(leaving))), row["id"]))
        row["winner_ids"] = json.dumps(winners)
        await self._edit_giveaway(row, self._giveaway_embed(row, await self._entry_count(row["id"]), winners), keep_button=False)
        await self._announce(row, fresh, reroll=True)
        short = f" Only {len(fresh)} of the {wanted} could be filled." if len(fresh) < wanted else ""
        await ctx.ok(f"Rerolled.{short} New winner{'' if len(fresh) == 1 else 's'}: " + " ".join(f"<@{w}>" for w in fresh))

    @giveaway.command(name="list", description="Show active or finished giveaways")
    @requires("manage_guild")
    @app_commands.describe(choice="Which giveaways to show")
    async def giveaway_list(self, ctx: commands.Context, choice: Literal["active", "inactive"] = "active") -> None:
        if choice == "active":
            rows = await self.db.all("SELECT * FROM giveaways WHERE guild_id = ? AND ended_at IS NULL ORDER BY ends_at LIMIT 25", (ctx.guild.id,))
        else:
            rows = await self.db.all("SELECT * FROM giveaways WHERE guild_id = ? AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 25", (ctx.guild.id,))
        if not rows:
            await ctx.send(f"There are no {choice} giveaways.", ephemeral=True)
            return
        lines = []
        for row in rows:
            moment = f"ended {stamp(row['ended_at'], 'R')}" if row["ended_at"] else f"ends {stamp(row['ends_at'], 'R')}"
            lines.append(f"**{row['id']}** {row['prize'][:80]} in <#{row['channel_id']}>, {moment}")
        await ctx.send(embed=discord.Embed(title=f"{choice.title()} giveaways", description="\n".join(lines)[:4000], color=BLUE), ephemeral=True, allowed_mentions=NO_PINGS)

    @giveaway.command(name="participants", description="Show who entered a giveaway")
    @requires("manage_guild")
    @app_commands.describe(giveaway_id="The giveaway's ID")
    async def giveaway_participants(self, ctx: commands.Context, giveaway_id: int) -> None:
        row = await self._own_giveaway(ctx, giveaway_id)
        if row is None:
            return
        entries = await self.db.all("SELECT user_id FROM giveaway_entries WHERE giveaway_id = ? ORDER BY entered_at", (giveaway_id,))
        if not entries:
            await ctx.send("Nobody has entered yet.", ephemeral=True)
            return
        lines = []
        for entry in entries:
            member = ctx.guild.get_member(entry["user_id"])
            lines.append(f"{member} ({entry['user_id']})" if member else f"left the server ({entry['user_id']})")
        await self._send_lines(ctx, f"{len(entries)} {'person' if len(entries) == 1 else 'people'} entered:", lines, filename=f"giveaway-{giveaway_id}.txt")

    async def _send_lines(self, ctx: commands.Context, heading: str, lines: list[str], *, separator: str = "\n", filename: str = "list.txt") -> None:
        """A list in a code block when it fits, and as a file when it doesn't. A member's name can't break out of a file."""
        body = separator.join(lines)
        block = f"{heading}\n```\n{body}\n```"
        if len(block) <= DISCORD_TEXT_MAX and "```" not in body:
            await ctx.send(block, ephemeral=True, allowed_mentions=NO_PINGS)
            return
        data = body.encode("utf-8")
        if len(data) > ctx.guild.filesize_limit:
            await ctx.fail(f"That list is too large to upload here, at {len(data) // (1024 * 1024) + 1} MB. Ask for fewer members, or a shorter format.")
            return
        await ctx.send(heading, file=discord.File(io.BytesIO(data), filename=filename), ephemeral=True, allowed_mentions=NO_PINGS)

    # ---- polls ----

    @commands.hybrid_command(name="poll", description="Post a poll people vote on with reactions")
    @commands.guild_only()
    @requires("manage_guild")
    @app_commands.describe(question="The question", choices="Choices, separated by | or commas. Empty makes a thumbs up and down poll.")
    async def poll(self, ctx: commands.Context, *, question: str, choices: Optional[str] = None) -> None:
        if choices is None and ctx.interaction is None:
            question, options = split_poll(question)
        else:
            options = split_choices(choices)
        question = question.strip()
        if not question or len(question) > 1000:
            await ctx.fail("A poll question needs 1 to 1000 characters.")
            return
        if len(options) == 1 or len(options) > 20:
            await ctx.fail("A poll takes 2 to 20 choices, or none for thumbs up and down.")
            return
        if any(len(option) > POLL_CHOICE_MAX for option in options):  # at this length 20 choices and the longest question still fit one embed
            await ctx.fail(f"A poll choice can hold up to {POLL_CHOICE_MAX} characters.")
            return
        refusal = access.post_refusal(ctx.author, ctx.channel)
        if refusal:
            await ctx.fail(refusal)
            return
        if options:
            marks = [chr(0x1F1E6 + index) for index in range(len(options))]
            body = "\n".join(f"{mark} {option}" for mark, option in zip(marks, options))
        else:
            marks = ["\N{THUMBS UP SIGN}", "\N{THUMBS DOWN SIGN}"]
            body = ""
        embed = discord.Embed(description=f"**{question}**\n\n{body}".strip()[:4096], color=BLUE)
        embed.set_footer(text=f"Poll by {ctx.author.display_name}")
        await ctx.defer(ephemeral=True)  # 20 reactions take longer than Discord waits for an answer
        try:
            message = await ctx.channel.send(embed=embed, allowed_mentions=NO_PINGS)
        except discord.HTTPException as error:
            if isinstance(error, discord.Forbidden):
                await ctx.fail("I can't post the poll here. I need View Channel, Send Messages and Embed Links.")
            else:
                await ctx.fail(f"Discord refused the poll: {error.text or error.status}.")
            return
        try:
            for mark in marks:
                await message.add_reaction(mark)
        except discord.HTTPException as error:
            if isinstance(error, discord.Forbidden):
                await ctx.fail("The poll is posted, but I can't add its reactions. I need Add Reactions and Read Message History here.")
            else:
                await ctx.fail(f"The poll is posted, but Discord refused a reaction: {error.text or error.status}.")
            return
        await ctx.ok("Poll posted.")

    # ---- temporary channels ----

    @commands.hybrid_command(name="tempchannel", description="Make a text channel that deletes itself after a while")
    @commands.guild_only()
    @requires("manage_channels")
    @app_commands.describe(duration="How long it lasts, such as 3h or 2 days", name="The channel's name")
    async def tempchannel(self, ctx: commands.Context, duration: Duration, *, name: str = "temp-chat") -> None:
        if not 60 <= duration <= 365 * DAY:
            await ctx.fail("A temporary channel can last from one minute to one year.")
            return
        if await self.db.scalar("SELECT COUNT(*) FROM temp_channels WHERE guild_id = ?", (ctx.guild.id,)) >= MAX_TEMP_CHANNELS:
            await ctx.fail(f"A server can hold at most {MAX_TEMP_CHANNELS} temporary channels at once.")
            return
        clean_name = re.sub(r"[^\w-]+", "-", name.strip().lower()).strip("-")[:100] or "temp-chat"
        now = time.time()
        expires = now + duration
        try:
            channel = await ctx.guild.create_text_channel(
                clean_name, category=getattr(ctx.channel, "category", None), topic=f"Temporary channel, made by {ctx.author}. It goes away <t:{int(expires)}:R>.",
                reason=f"Temporary channel for {humanize(duration)}, asked for by {ctx.author}",
            )
        except discord.Forbidden:
            await ctx.fail("I need Manage Channels to make that.")
            return
        except discord.HTTPException as error:
            await ctx.fail(f"Discord refused the channel: {error.text or error.status}")
            return
        await self.db.run(
            "INSERT INTO temp_channels (channel_id, guild_id, created_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (channel.id, ctx.guild.id, ctx.author.id, now, expires),
        )
        await self.bot.timers.schedule("tempchannel", expires, {"guild_id": ctx.guild.id, "channel_id": channel.id}, guild_id=ctx.guild.id, key=f"tempchannel:{channel.id}")
        await ctx.ok(f"{channel.mention} is open until {stamp(expires)}.")

    async def _tempchannel_due(self, payload: dict, row: dict) -> None:
        guild = self.bot.get_guild(payload["guild_id"])
        channel = guild.get_channel(payload["channel_id"]) if guild else None
        if channel is not None:
            try:
                await channel.delete(reason="Temporary channel ran out")
            except discord.NotFound:
                pass
            except discord.Forbidden:
                logs = self.bot.module("logging")
                if logs is not None and not row["tries"]:
                    logs.staff_notice(guild, "A temporary channel needs a look", f"The bot can't delete {channel.mention}, which ran out. It needs Manage Channels there, and it keeps trying.")
                raise  # the row and the timer stay, so the delete goes through once the permission is back
        await self.db.run("DELETE FROM temp_channels WHERE channel_id = ? AND guild_id = ?", (payload["channel_id"], payload["guild_id"]))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        removed = await self.db.run("DELETE FROM temp_channels WHERE channel_id = ? AND guild_id = ?", (channel.id, channel.guild.id))
        if removed:
            await self.bot.timers.cancel_key("tempchannel", f"tempchannel:{channel.id}")

    # ---- members ----

    @commands.hybrid_group(name="members", fallback="info", description="Look up a member, or rank members by age")
    @commands.guild_only()
    @app_commands.describe(member="Who to look up. Empty means you.")
    async def members(self, ctx: commands.Context, *, member: Optional[discord.Member] = None) -> None:
        await self._member_card(ctx, member or ctx.author)

    async def _members_info_text(self, ctx: commands.Context, *, member: Optional[discord.Member] = None) -> None:
        await self._member_card(ctx, member or ctx.author)

    async def _member_card(self, ctx: commands.Context, target: Any) -> None:
        created = target.created_at.timestamp()
        embed = discord.Embed(title=str(target), color=target.top_role.color.value or BLUE)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="ID", value=str(target.id))
        embed.add_field(name="Display name", value=target.display_name)
        embed.add_field(name="Avatar", value=f"[Open]({target.display_avatar.url})")
        embed.add_field(name="Account made", value=f"{stamp(created, 'D')} ({stamp(created, 'R')})")
        joined = getattr(target, "joined_at", None)
        if joined is not None:
            earlier = sum(1 for m in ctx.guild.members if m.joined_at is not None and m.joined_at < joined)
            embed.add_field(name="Joined", value=f"{stamp(joined.timestamp(), 'D')} ({stamp(joined.timestamp(), 'R')})")
            embed.add_field(name="Join position", value=ordinal(earlier + 1))
        roles = [role for role in reversed(target.roles) if not role.is_default()]
        listed = " ".join(role.mention for role in roles)
        if len(listed) > 1024:
            listed = listed[:1000].rsplit(" ", 1)[0] + " and more"
        embed.add_field(name=f"Roles ({len(roles)})", value=listed or "none", inline=False)
        boosted = getattr(target, "premium_since", None)
        if boosted is not None:
            embed.add_field(name="Boosting since", value=stamp(boosted.timestamp(), "D"))
        if target.timed_out_until is not None:
            embed.add_field(name="Timed out until", value=stamp(target.timed_out_until.timestamp()))
        if target.bot:
            embed.set_footer(text="This is a bot.")
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    async def _rank(self, ctx: commands.Context, count: int, title: str, moment: Any, *, newest: bool) -> None:
        """Rank without sorting everyone: on a big server only the top few matter."""
        people = [m for m in ctx.guild.members if moment(m) is not None]
        pick = heapq.nlargest if newest else heapq.nsmallest
        rows = pick(count, people, key=moment)
        lines = [f"{index}. {m.mention} `{m.id}` {stamp(moment(m).timestamp(), 'R')}" for index, m in enumerate(rows, 1)]
        await ctx.send(embed=discord.Embed(title=title, description="\n".join(lines) or "Nobody.", color=BLUE), allowed_mentions=NO_PINGS)

    @members.command(name="youngest", description="Rank members with the newest accounts")
    @requires("manage_guild")
    @app_commands.describe(count="How many to show, up to 25")
    async def members_youngest(self, ctx: commands.Context, count: commands.Range[int, 1, 25] = 5) -> None:
        await self._rank(ctx, count, "Newest accounts", lambda m: m.created_at, newest=True)

    @members.command(name="oldest", description="Rank members with the oldest accounts")
    @requires("manage_guild")
    @app_commands.describe(count="How many to show, up to 25")
    async def members_oldest(self, ctx: commands.Context, count: commands.Range[int, 1, 25] = 5) -> None:
        await self._rank(ctx, count, "Oldest accounts", lambda m: m.created_at, newest=False)

    @members.command(name="newusers", aliases=["newmembers"], description="Rank the people who joined most recently")
    @requires("manage_guild")
    @app_commands.describe(count="How many to show, up to 25")
    async def members_newusers(self, ctx: commands.Context, count: commands.Range[int, 1, 25] = 5) -> None:
        await self._rank(ctx, count, "Newest members", lambda m: m.joined_at, newest=True)

    @members.command(name="oldusers", aliases=["oldmembers"], description="Rank the people who joined first")
    @requires("manage_guild")
    @app_commands.describe(count="How many to show, up to 25")
    async def members_oldusers(self, ctx: commands.Context, count: commands.Range[int, 1, 25] = 5) -> None:
        await self._rank(ctx, count, "Longest-standing members", lambda m: m.joined_at, newest=False)

    @commands.hybrid_command(name="avatars", aliases=["avatar"], ignore_extra=True, description="Show someone's avatar")
    @commands.guild_only()
    @app_commands.rename(use_global="global")
    @app_commands.describe(user="Whose avatar. Empty means you.", use_global="Show their account avatar instead of their server avatar")
    async def avatars(self, ctx: commands.Context, user: Optional[discord.Member | discord.User] = None, use_global: Optional[bool] = None) -> None:
        typed = unused_words(ctx).lower()
        if typed:  # typed, the words `global` and `server` choose, and they aren't a yes or no
            if typed not in ("global", "server"):
                await ctx.fail(f"I can't find a member called `{typed.replace('`', '')[:60]}`.")
                return
            use_global = typed == "global"
        target = user or ctx.author
        fallback = getattr(target, "default_avatar", None)
        asset = (getattr(target, "avatar", None) or fallback or target.display_avatar) if use_global else target.display_avatar
        embed = discord.Embed(title=f"{target}'s {'account' if use_global else 'server'} avatar" if getattr(target, "guild_avatar", None) else f"{target}'s avatar", color=BLUE)
        embed.set_image(url=asset.url)
        if fallback is None or asset != fallback:
            links = self._avatar_links(asset)
            if links:
                embed.description = " | ".join(links)
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    @staticmethod
    def _avatar_links(asset: Any) -> list[str]:
        if not hasattr(asset, "with_format"):
            return []
        formats = ["png", "jpg", "webp"] + (["gif"] if asset.is_animated() else [])
        return [f"[{name.upper()}]({asset.with_format(name).with_size(1024).url})" for name in formats]

    # ---- stats ----

    async def _usage(self, ctx: commands.Context, title: str, *, guild_id: int | None, member: Any = None, since_day: int | None = None) -> None:
        await ctx.defer()  # a big table can take longer than Discord waits for an answer
        where, args = ["1 = 1"], []
        if guild_id is not None:
            where.append("guild_id = ?")
            args.append(guild_id)
        if member is not None:
            where.append("user_id = ?")
            args.append(member.id)
        if since_day is not None:
            where.append("day >= ?")
            args.append(since_day)
        clause = " AND ".join(where)
        total = await self.db.scalar(f"SELECT COALESCE(SUM(uses), 0) FROM command_uses WHERE {clause}", args)
        top = await self.db.all(f"SELECT command, SUM(uses) AS total FROM command_uses WHERE {clause} GROUP BY command ORDER BY total DESC, command LIMIT 15", args)
        embed = discord.Embed(title=title, description=f"{total:,} command{'' if total == 1 else 's'} used.", color=BLUE)
        if top:
            embed.add_field(name="Top commands", value="\n".join(f"`{row['command']}`: {row['total']:,}" for row in top), inline=False)
        if member is None and guild_id is not None:
            people = await self.db.all(f"SELECT user_id, SUM(uses) AS total FROM command_uses WHERE {clause} GROUP BY user_id ORDER BY total DESC LIMIT 5", args)
            if people:
                embed.add_field(name="Top members", value="\n".join(f"<@{row['user_id']}>: {row['total']:,}" for row in people), inline=False)
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    @commands.hybrid_group(name="stats", fallback="usage", description="Show command use, server facts and uptime")
    @commands.guild_only()
    @commands.cooldown(1, STATS_COOLDOWN, commands.BucketType.user)
    @commands.max_concurrency(1, commands.BucketType.guild)
    @app_commands.describe(member="Only count this person", period="How far back to count")
    async def stats(self, ctx: commands.Context, member: Optional[discord.Member] = None, period: Literal["all", "today", "week", "month"] = "all") -> None:
        await self._usage_for(ctx, member, period)

    @commands.cooldown(1, STATS_COOLDOWN, commands.BucketType.user)
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def _stats_usage_text(self, ctx: commands.Context, member: Optional[discord.Member] = None, period: Literal["all", "today", "week", "month"] = "all") -> None:
        await self._usage_for(ctx, member, period)

    async def _usage_for(self, ctx: commands.Context, member: Any, period: str) -> None:
        today = int(time.time() // DAY)
        since = {"all": None, "today": today, "week": today - 6, "month": today - 29}[period]
        scope = member.display_name if member else ctx.guild.name
        label = {"all": "the last 90 days", "today": "today", "week": "the last 7 days", "month": "the last 30 days"}[period]
        await self._usage(ctx, f"Command use in {scope}, {label}", guild_id=ctx.guild.id, member=member, since_day=since)

    @stats.command(name="today", description="Show today's command use for the server or a member")
    @commands.cooldown(1, STATS_COOLDOWN, commands.BucketType.user)
    @commands.max_concurrency(1, commands.BucketType.guild)
    @app_commands.describe(member="Only count this person")
    async def stats_today(self, ctx: commands.Context, *, member: Optional[discord.Member] = None) -> None:
        scope = member.display_name if member else ctx.guild.name
        await self._usage(ctx, f"Command use in {scope} today", guild_id=ctx.guild.id, member=member, since_day=int(time.time() // DAY))

    @stats.command(name="global", description="Show command use across every server this bot is in")
    @commands.is_owner()
    @commands.cooldown(1, STATS_COOLDOWN, commands.BucketType.user)
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def stats_global(self, ctx: commands.Context) -> None:
        await self._usage(ctx, "Command use across every server", guild_id=None)

    @stats.command(name="uptime", description="Show how long the bot has been up")
    async def stats_uptime(self, ctx: commands.Context) -> None:
        await ctx.send(f"Up for {humanize(time.time() - self.bot.started_at)}.", ephemeral=True)

    @stats.command(name="serverinfo", description="Show facts about this server")
    async def stats_serverinfo(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        kinds = {discord.ChannelType.text: 0, discord.ChannelType.voice: 0, discord.ChannelType.category: 0, discord.ChannelType.forum: 0}
        for channel in guild.channels:
            if channel.type in kinds:
                kinds[channel.type] += 1
        people = len(guild.members)
        bots = sum(1 for m in guild.members if m.bot)
        created = guild.created_at.timestamp()
        embed = discord.Embed(title=guild.name, color=BLUE)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="ID", value=str(guild.id))
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>")
        embed.add_field(name="Created", value=f"{stamp(created, 'D')} ({stamp(created, 'R')})")
        embed.add_field(name="Members", value=f"{guild.member_count or people:,} ({bots:,} bots in the cache)")
        embed.add_field(name="Channels", value=(
            f"{kinds[discord.ChannelType.text]} text, {kinds[discord.ChannelType.voice]} voice, "
            f"{kinds[discord.ChannelType.forum]} forum, {kinds[discord.ChannelType.category]} categories"
        ))
        embed.add_field(name="Roles", value=str(len(guild.roles)))
        embed.add_field(name="Emoji", value=str(len(guild.emojis)))
        boosts = getattr(guild, "premium_subscription_count", 0) or 0
        embed.add_field(name="Boosts", value=f"{boosts} (level {getattr(guild, 'premium_tier', 0)})")
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    @stats.command(name="dump", description="List members with flags for roles, order, format and limit")
    @requires("manage_roles")
    @app_commands.describe(role="Only members with this role", query="Flags such as --order id --limit 10 --no-roles -f %n %c")
    async def stats_dump(self, ctx: commands.Context, role: Optional[discord.Role] = None, *, query: str = "") -> None:
        await ctx.defer(ephemeral=True)
        try:
            options = parse_dump_query(query)
            chosen = select_members(ctx.guild, role, options)
            if not chosen:
                await ctx.send("No members match that.", ephemeral=True)
                return
            lines = []
            for number, member in enumerate(chosen, 1):
                line = dump_line(member, options)
                lines.append(f"{number}. {line}" if options.enumerate else line)
                if number % 5000 == 0:  # on a very large server, leave room for everything else the bot is doing
                    await asyncio.sleep(0)
        except DumpError as error:
            await ctx.fail(str(error))
            return
        await self._send_lines(ctx, f"{len(lines):,} member{'' if len(lines) == 1 else 's'}:", lines, separator=options.separator, filename="members.txt")

    # ---- misc ----

    @commands.hybrid_group(name="misc", description="Small tools: characters, permissions and invite links")
    @commands.guild_only()
    async def misc(self, ctx: commands.Context) -> None:
        await self._list_subcommands(ctx, self.misc)

    @misc.command(name="charinfo", description="Show facts about up to 15 characters")
    @app_commands.describe(characters="The characters to look up")
    async def misc_charinfo(self, ctx: commands.Context, *, characters: str) -> None:
        if len(characters) > 15:
            await ctx.fail("That's more than 15 characters.")
            return
        lines = []
        for character in characters:
            name = unicodedata.name(character, "no name")
            lines.append(f"`U+{ord(character):04X}` {name}: {character}" if not character.isspace() else f"`U+{ord(character):04X}` {name}")
        await ctx.send("\n".join(lines), allowed_mentions=NO_PINGS)

    async def _permissions_embed(self, ctx: commands.Context, member: Any, channel: Any) -> discord.Embed:
        permissions = channel.permissions_for(member)
        allowed = [permission_name(name) for name, on in permissions if on]
        denied = [permission_name(name) for name, on in permissions if not on]
        embed = discord.Embed(title=f"{member} in {channel.name}"[:256], color=BLUE)
        embed.add_field(name="Allowed", value=", ".join(allowed)[:1024] or "nothing", inline=False)
        embed.add_field(name="Denied", value=", ".join(denied)[:1024] or "nothing", inline=False)
        return embed

    @staticmethod
    async def _refuse_unseen(ctx: commands.Context, channel: Any) -> bool:
        """A channel this person can't see gets the same answer as a name that matches nothing, so asking can't confirm one exists. These commands read their own leftover words for that reason."""
        if not unused_words(ctx) and (channel is None or access.can_view(ctx.author, channel)):
            return False
        await ctx.fail("I can't find that channel.")
        return True

    @misc.command(name="permissions", ignore_extra=True, description="Show what a member can do in a channel")
    @app_commands.describe(member="Who to check. Empty means you.", channel="Where to check. Empty means this channel.")
    async def misc_permissions(self, ctx: commands.Context, member: Optional[discord.Member] = None, channel: Optional[discord.abc.GuildChannel] = None) -> None:
        if await self._refuse_unseen(ctx, channel):
            return
        embed = await self._permissions_embed(ctx, member or ctx.author, channel or ctx.channel)
        await ctx.send(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @misc.command(name="botpermissions", ignore_extra=True, description="Show what the bot can do in a channel")
    @requires("manage_roles")
    @app_commands.describe(channel="Where to check. Empty means this channel.")
    async def misc_botpermissions(self, ctx: commands.Context, *, channel: Optional[discord.abc.GuildChannel] = None) -> None:
        if await self._refuse_unseen(ctx, channel):
            return
        embed = await self._permissions_embed(ctx, ctx.guild.me, channel or ctx.channel)
        await ctx.send(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @misc.command(name="wiki", description="Show a link to this bot's documentation")
    async def misc_wiki(self, ctx: commands.Context) -> None:
        if not self.bot.public_url:
            await ctx.fail("This copy of the bot has no public address, so there's no docs page to link to.")
            return
        await ctx.send(f"{self.bot.public_url}/docs", allowed_mentions=NO_PINGS)

    @misc.command(name="invite", description="Show the links that add this bot to a server")
    async def misc_invite(self, ctx: commands.Context) -> None:
        client_id = self.bot.application_id or (self.bot.user.id if self.bot.user else None)
        if client_id is None:
            await ctx.fail("The bot hasn't finished starting. Try again in a moment.")
            return
        scopes = ("bot", "applications.commands")
        full = discord.utils.oauth_url(client_id, permissions=discord.Permissions(administrator=True), scopes=scopes)
        plain = discord.utils.oauth_url(client_id, permissions=discord.Permissions.none(), scopes=scopes)
        await ctx.send(f"[With Administrator]({full})\n[With no permissions, to set them up by hand]({plain})", ephemeral=True, allowed_mentions=NO_PINGS)
