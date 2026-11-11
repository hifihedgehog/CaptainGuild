"""Welcome, farewell and ban messages, the join DM, and birthdays.

A welcome, a goodbye and a ban message are posts in a channel. Each one the bot makes is noted in greeting_posts for 30 days, which is what
lets a post be deleted after a lifetime, and a welcome be deleted when its member leaves. A greeting can show two values of its own: {joined},
a count of everyone who ever joined, kept in greeting_counts, and {invite}, the invite a join came through, read by core/invitewatch.py.
The welcome image is another module's work. This one asks it for a picture and attaches what comes back.
"""
from __future__ import annotations

import asyncio
import bisect
import calendar
import collections
import datetime
import io
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Literal

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, delivery, invitewatch, messages
from ..core.durations import Duration, humanize
from ..core.module import Module, requires
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import role_is_safe
from ..core.tagscript import OverBudget, Scope, budget, budgeted_render, doublebrace, objects, render_message
from ..core.tagscript.objects import plain_name
from ..core.tagscript.text import plain, written_by_template
from ..core.tagscript.preview import preview_scope
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, choice, integer, optional, snowflake
from ..web.api import ApiError

log = logging.getLogger(__name__)

TICK = "greetings.birthday"
ROLE_ENDS = "greetings.birthday_role"
DAY = 86400
# A member's own name can land in the combined announcement, and it must not reach the template language as a block.
_BRACES = str.maketrans({"{": "(", "}": ")"})
_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)
_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name})
_NUMBERS = re.compile(r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})")
_DAY_MONTH = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)")
_MONTH_DAY = re.compile(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?")
KINDS = ("welcome", "farewell", "ban", "dm", "birthday")
GREETS = ("welcome", "farewell", "ban", "dm")  # the messages that show {joined} and {invite}
BURST_SECONDS = 60
LEAVING = "leaving"
SCREENING = "screening"
MAX_LATE_DAYS = 7  # how far back a tick looks after an outage, since a birthday weeks ago is no longer news
COMBINED_PEOPLE = 50  # a message holds 2,000 characters, which is about 80 mentions, and a cut one wouldn't ping anybody

POSTED = ("welcome", "farewell", "ban")  # the greetings that are posts in a channel, in the order the page shows them
DELETE = {kind: f"greetings.delete_{kind}" for kind in POSTED}  # each kind's timer, so a change to one lifetime moves only its own posts
LEAVE_RETRY = "greetings.delete_left"  # another try at deleting the welcome of someone who left. No change to a lifetime moves or removes it.
KEEP_POSTS = 30 * DAY          # how long a post stays noted
MAX_DELETE_AFTER = 30 * DAY    # the longest lifetime, which is as long as a post stays noted
MAX_AGAIN_MINUTES = 1440
MAX_POSTS = 20_000             # posts noted for one server. Past it the oldest go, and a raid can't fill the table.
MAX_HELD = 5_000               # join numbers and rejoin times held in memory across every server
MAX_JOINED = 10 ** 12
DELETE_SPACING = 1.0           # seconds between two delete timers of one server, so a burst of posts doesn't come due in one instant
BACKLOG_SPACING = 5.0          # the same for posts already past a new lifetime. Discord takes deletes of old messages slowly.
DELETE_RETRY = 120.0
DELETE_WINDOW = 60.0           # the span DELETE_HOLD is measured over
FORBIDDEN_RETRY = 3600.0       # a delete Discord forbids comes back this much later, while its post is noted: the bot may get the channel back
CONVERT_MAX = 50_000           # characters the Import box takes
CARDS = "welcomecard"          # the module that draws the welcome image
INVITE_BLOCK = "{invite"
_ROLE_MENTION = re.compile(r"<@&(\d{15,21})>")
_MEMBER_MENTION = re.compile(r"<@!?(\d{15,21})>")
_EVERYONE = re.compile(r"@everyone|@here")
_DIGITS = re.compile(r"\d+")
_BIGGEST_ID = (1 << 63) - 1  # the largest ID a ping list takes, as payload.ids_from caps one. A larger number in a typed mention is nobody's, and it stays out.
_PICTURE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,60}\.(?:png|gif)")
_MESSAGE_KEYS = frozenset({"content", "embeds", "embed", "components", "buttons", "messages", "title", "description", "fields", "image", "thumbnail", "footer", "author"})

# What /greetings switch takes: the setting it changes, and what to say when that goes on and off.
SWITCHES = {
    "welcome": ("welcome_on", "Welcome messages are on.", "Welcome messages are off. The message stays saved."),
    "joindm": ("dm_on", "The join DM is on.", "The join DM is off. The message stays saved."),
    "farewell": ("farewell_on", "Leave messages are on.", "Leave messages are off. The message stays saved."),
    "ban": ("ban_on", "Ban messages are on.", "Ban messages are off. The message stays saved."),
    "joindm_same_as_welcome": ("dm_same_as_welcome", "The join DM now sends the welcome message.", "The join DM sends its own message again."),
    "welcome_bots": ("welcome_bots", "A bot that joins gets the welcome message.", "A bot that joins gets no welcome message."),
    "delete_welcome_on_leave": ("welcome_delete_on_leave", "A member's welcome message is deleted when they leave or are banned.", "A welcome message stays when its member leaves."),
}
Switch = Literal["welcome", "joindm", "farewell", "ban", "joindm_same_as_welcome", "welcome_bots", "delete_welcome_on_leave"]
# The messages with a choice of who they ping, each in its own `<kind>_mentions`: what each is called, and who it is about.
_PINGED = {"welcome": ("The welcome message", "the member it greets"), "farewell": ("The leave message", "the member who left"),
           "ban": ("The ban message", "the person who was banned")}
MENTIONED = tuple(_PINGED)


def ping_sentence(message: str, who: str) -> str:
    """What a choice of who to ping does, said for the welcome, leave or ban message."""
    name, person = _PINGED[message]
    return {
        "member": f"{name} pings {person}.",
        "roles": f"{name} pings {person} and the roles its text mentions.",
        "all": f"{name} pings {person}, the roles its text mentions, and @everyone or @here when its text has them.",
    }[who]


PINGS = {who: ping_sentence("welcome", who) for who in ("member", "roles", "all")}


def parse_birthday(text: str) -> tuple[int, int] | None:
    """(month, day) from 25/12, 25-12, 25 December or Dec 25. Numbers are day first, and a date that doesn't exist gives None."""
    cleaned = " ".join((text or "").lower().replace(",", " ").split())
    month: int | None = None
    if match := _NUMBERS.fullmatch(cleaned):
        day, month = int(match.group(1)), int(match.group(2))
    elif match := _DAY_MONTH.fullmatch(cleaned):
        day, month = int(match.group(1)), _MONTHS.get(match.group(2))
    elif match := _MONTH_DAY.fullmatch(cleaned):
        month, day = _MONTHS.get(match.group(1)), int(match.group(2))
    else:
        return None
    # 2024 was a leap year, so February 29 counts as a real birthday.
    if month is None or not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(2024, month)[1]:
        return None
    return month, day


def birthday_text(month: int, day: int) -> str:
    return f"{day} {calendar.month_name[month]}"


def dates_for(moment: datetime.datetime) -> list[tuple[int, int]]:
    """The birthdays that fall on this date. February 29 is celebrated on the 28th when the year has no 29th."""
    today = [(moment.month, moment.day)]
    if moment.month == 2 and moment.day == 28 and not calendar.isleap(moment.year):
        today.append((2, 29))
    return today


def next_run(hour: int, now: float) -> float:
    moment = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0)
    if moment.timestamp() <= now:
        moment += datetime.timedelta(days=1)
    return moment.timestamp()


def _default_birthday() -> dict:
    return {"content": "\N{PARTY POPPER} Happy Birthday {mention}!", "embeds": [], "buttons": []}


def _stored_message(value: Any) -> dict:
    return messages.clean_payload(value, templates=True)


def _lifetime(value: Any) -> int:
    """Seconds a post stays up before the bot deletes it, in whole minutes. 0 keeps it. The page shows a lifetime in days, hours and minutes,
    where one under a minute would read as all zeros, which keep every post, and seconds left over wouldn't show. The refusals speak in days
    and minutes too."""
    seconds = integer(0)(value)
    if seconds > MAX_DELETE_AFTER:
        raise Invalid("A post can stay up to 30 days.")
    if seconds % 60:
        raise Invalid("A post stays up for whole minutes, from 1 minute to 30 days.")
    return seconds


def _holds(value: Any, needle: str) -> bool:
    """Whether any text in a stored setting holds this."""
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_holds(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(_holds(item, needle) for item in value)
    return False


def _template_mentions(stored: dict, shown: str, marked: str) -> tuple[list[int], list[int], set[str], set[str]]:
    """In a post's finished text, the people and the roles the template itself mentions, then the @everyone and @here words the template wrote and
    the ones that came from outside it.

    `marked` is the same text with what came from outside the template marked. A mention is the template's when none of its characters came from
    outside, and then a person's when their ID is a number the stored text holds, written anywhere in it, as in a variable a mention is built
    from. A role's is the template's only when the stored text holds that role's mention typed whole, since that is what the check on the
    saver's right to ping reads (pinged_channels)."""
    marked = marked[:len(shown)]
    if plain(marked) != shown:  # the two have to line up character for character, and otherwise nothing counts as the template's
        return [], [], set(), set(_EVERYONE.findall(shown))
    written = stored.get("content") or ""
    held = {int(number) for number in _DIGITS.findall(written) if 15 <= len(number) <= 21}
    typed = {int(number) for number in _ROLE_MENTION.findall(written)}
    users = [int(found.group(1)) for found in _MEMBER_MENTION.finditer(shown) if written_by_template(marked, found.start(), found.end()) and int(found.group(1)) in held]
    roles = [int(found.group(1)) for found in _ROLE_MENTION.finditer(shown) if written_by_template(marked, found.start(), found.end()) and int(found.group(1)) in typed]
    ours: set[str] = set()
    theirs: set[str] = set()
    for found in _EVERYONE.finditer(shown):
        if written_by_template(marked, found.start(), found.end()) and found.group() in written:
            ours.add(found.group())
        else:
            theirs.add(found.group())
    return users, roles, ours, theirs


def _with_picture(payload: dict, picture: dict | None) -> dict:
    """The message with room made for its picture. Inside the embed, the picture is the first embed's image, and a message with no embed gets one that holds only that."""
    if picture is None or not picture["in_embed"]:
        return payload
    embeds = [dict(embed) for embed in payload.get("embeds") or []] or [{}]
    embeds[0]["image"] = {"url": f"attachment://{picture['filename']}"}
    return {**payload, "embeds": embeds}


def _outgoing(payload: dict, picture: dict | None, mentions: discord.AllowedMentions) -> dict:
    """What to hand to send: the message, and its picture as a file when it has one."""
    kwargs = messages.send_kwargs(_with_picture(payload, picture), allowed_mentions=mentions)
    if picture is not None:
        kwargs["file"] = discord.File(io.BytesIO(picture["data"]), picture["filename"])
    return kwargs


def _is_message(data: Any) -> bool:
    """Whether pasted JSON is a message or an embed, or a list of embeds, going by the names of what it holds."""
    if isinstance(data, list):
        return bool(data) and all(isinstance(item, dict) and _MESSAGE_KEYS & set(item) for item in data)
    return isinstance(data, dict) and bool(_MESSAGE_KEYS & set(data))


def _converted(text: str, number_format: str) -> tuple[dict, list[str]]:
    """Pasted text as a greeting: a message's JSON when that's what it is, and otherwise the message's own text."""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            data = json.loads(stripped)
        except (ValueError, RecursionError):
            data = None
        if _is_message(data):
            return doublebrace.convert_message(data, number_format=number_format)
    if len(text) > messages.CONTENT_MAX:
        # Refused before any work, so a paste built to be slow costs nothing.
        raise Invalid(f"The message text can be at most {messages.CONTENT_MAX} characters. This is {len(text)}.")
    made, notes = doublebrace.convert_text(text, number_format=number_format, message=True)  # it's a message's text
    return doublebrace.fitted_message({"content": made}, notes), notes


class Greetings(Module):
    key = "greetings"
    category = "Greetings"
    role_timers = (ROLE_ENDS,)
    row_limits = {"greeting_posts": MAX_POSTS}
    schema = Schema({
        "welcome_channel": Field(None, optional(snowflake)),
        "farewell_channel": Field(None, optional(snowflake)),
        "ban_channel": Field(None, optional(snowflake)),
        "welcome": Field(messages.empty_payload(), _stored_message),
        "farewell": Field(messages.empty_payload(), _stored_message),
        "ban": Field(messages.empty_payload(), _stored_message),
        "dm": Field(messages.empty_payload(), _stored_message),
        "wait_for_rules": Field(False, boolean),
        "birthday_channel": Field(None, optional(snowflake)),
        "birthday": Field(_default_birthday(), _stored_message),
        "birthday_hour": Field(0, integer(0, 23)),
        "birthday_role": Field(None, optional(snowflake)),
        "birthday_combine": Field(False, boolean),
        "burst_joins": Field(10, integer(0, 1000)),
        # Off keeps the stored message and sends nothing. A test still sends.
        "welcome_on": Field(True, boolean),
        "dm_on": Field(True, boolean),
        "farewell_on": Field(True, boolean),
        "ban_on": Field(True, boolean),
        "dm_same_as_welcome": Field(False, boolean),
        # Seconds after which that post is deleted. 0 never deletes.
        "welcome_delete_after": Field(0, _lifetime),
        "farewell_delete_after": Field(0, _lifetime),
        "ban_delete_after": Field(0, _lifetime),
        "welcome_delete_on_leave": Field(False, boolean),
        # Minutes in which someone who rejoins gets no second welcome or DM. 0 welcomes every join.
        "welcome_again_after": Field(0, integer(0, MAX_AGAIN_MINUTES)),
        "welcome_bots": Field(True, boolean),
        "welcome_mentions": Field("member", choice(*PINGS)),
        # Who the leave and ban messages ping, by the welcome's three choices. Member is what they always did.
        "farewell_mentions": Field("member", choice(*PINGS)),
        "ban_mentions": Field("member", choice(*PINGS)),
    })
    migrations = [
        """
        CREATE TABLE birthdays (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            month INTEGER NOT NULL,
            day INTEGER NOT NULL,
            announced_year INTEGER,
            updated_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE INDEX birthdays_date ON birthdays (guild_id, month, day);
        """,
        """
        CREATE TABLE greeting_posts (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            posted_at REAL NOT NULL,
            PRIMARY KEY (guild_id, message_id)
        );
        CREATE INDEX greeting_posts_person ON greeting_posts (guild_id, user_id);
        CREATE INDEX greeting_posts_age ON greeting_posts (posted_at);
        CREATE TABLE greeting_counts (
            guild_id INTEGER NOT NULL PRIMARY KEY,
            joined INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );
        """,
    ]

    # Seconds to wait before a farewell, for the ban event of the same person to arrive.
    BAN_WAIT = 2.0

    # Seconds with no new join before a burst of joins gets its one line.
    BURST_QUIET = float(BURST_SECONDS)

    # Seconds a welcome waits to learn which invite its member used. Past that it goes out with the invite unknown.
    INVITE_WAIT = 5.0

    # Seconds to wait for the welcome image. The module that draws it answers within 15, and a welcome must never hang on it.
    PICTURE_WAIT = 20.0

    # Seconds one delete can take before it steps aside for the timers behind it and tries again later.
    DELETE_PATIENCE = 5.0

    # Seconds deletes may hold the shared timer loop in any DELETE_WINDOW. Every other feature's timers wait behind them, so past this a due
    # delete steps aside without asking Discord, and comes back later.
    DELETE_HOLD = 5.0

    # Seconds a welcome waits for its join to be counted before it goes out with the count as last read.
    COUNT_PATIENCE = 10.0

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._recent_bans: dict[tuple[int, int], float] = {}
        self._joins: dict[int, collections.deque] = {}
        self._burst_held: dict[int, int] = {}
        self._burst_tasks: dict[int, asyncio.Task] = {}
        self._problems: dict[tuple[int, str], str] = {}
        self.invites = invitewatch.InviteWatch()
        self._invite_servers: set[int] = set()                    # servers where a greeting or the welcome image shows the invite
        self._join_numbers: dict[tuple[int, int], int] = {}        # the count at the join of each member still on the rules screen
        self._uncounted: dict[int, int] = {}                       # joins that got no welcome and are still to be added to their server's count
        self._newest_uncounted: dict[int, tuple] = {}              # the latest of those in the order of joins
        self._uncounted_ids: dict[int, set[int]] = {}              # and who they are
        self._pending: dict[int, set[int]] = {}                    # each server's joins handed to writes that haven't landed
        self._placed: dict[int, dict[int, tuple[int, bool]]] = {}  # each join's place, kept by a server's first count for its own write, and whether that write adds it
        self._counting: set[int] = set()                           # servers with a write of those under way
        self._welcomed: dict[tuple[int, int], float] = {}          # when each member was last welcomed, where a rejoin gets no second welcome
        self._lifetimes: dict[int, tuple[int, ...]] = {}           # each server's delete-after settings as its timers last followed them
        self._delete_waits: collections.deque = collections.deque()  # (when, seconds) a delete held the timer loop, over the last DELETE_WINDOW
        self._next_put_off: dict[int, float] = {}  # each server's next slot for a delete put off, so one server's backlog never delays another's
        self._next_delete: dict[tuple[int, str], float] = {}       # the earliest time the next delete timer of a server and kind may be due
        self._noted: set[int] = set()                              # servers that noted a post since old posts were last cleared out
        self._cleared_at = 0.0
        self._tasks: set[asyncio.Future] = set()
        self._converting = asyncio.Lock()  # one pasted message converted at a time, across the bot

    def roles_given(self, settings: dict) -> dict[str, set[int]]:
        return {"birthday_role": {settings["birthday_role"]} if settings["birthday_role"] else set()}

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {name: {settings[name]} if settings[name] else set() for name in ("welcome_channel", "farewell_channel", "ban_channel", "birthday_channel")}

    @staticmethod
    def _posts_in(settings: dict, kind: str) -> int | None:
        """The channel a welcome, leave or ban message posts in. A leave message uses the welcome's when it has none, and a ban message the leave message's."""
        names = {"welcome": ("welcome_channel",), "farewell": ("farewell_channel", "welcome_channel"), "ban": ("ban_channel", "farewell_channel", "welcome_channel")}
        return next((settings[name] for name in names[kind] if settings[name]), None)

    def pinged_channels(self, settings: dict) -> dict[str, set[int]]:
        """What the welcome, leave and ban messages can ping beyond the person each is about, each under its own name, so a new one takes the
        right to send it and editing the words around an old one doesn't."""
        found: dict[str, set[int]] = {}
        for kind in MENTIONED:
            channel, mode = self._posts_in(settings, kind), settings[f"{kind}_mentions"]
            if not channel or mode == "member":
                continue
            found[f"{kind}_mentions:{mode}"] = {channel}
            guild = getattr(self.bot.get_channel(channel), "guild", None)
            for role_id in _ROLE_MENTION.findall(settings[kind].get("content") or ""):
                if delivery.needs_ping_right(guild, int(role_id)):
                    found[f"{kind}:{role_id}"] = {channel}
            if mode == "all":
                for word in messages.ping_words(settings[kind], everyone_only=True):
                    found[f"{kind}:{word}"] = {channel}
        return found

    def transient_roles(self, guild_id: int) -> set[int]:
        role = self.cfg(guild_id)["birthday_role"]
        return {role} if role else set()

    def cog_unload(self) -> None:
        for task in [*self._burst_tasks.values(), *self._tasks]:
            task.cancel()

    def _note_problem(self, guild_id: int, kind: str | None, problem: str | None) -> None:
        """What last went wrong with a message, for the page to show with its remedy. A message that goes out clears it."""
        if kind is None:
            return
        if problem is None:
            self._problems.pop((guild_id, kind), None)
        else:
            self._problems[(guild_id, kind)] = problem

    def _spawn(self, work: Awaitable[Any]) -> None:
        """Work nobody waits for, held here so it isn't dropped halfway."""
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _hold(held: dict, key: Any, value: Any) -> None:
        """Keep one more thing in memory, and let the oldest go past the ceiling."""
        held.pop(key, None)
        while len(held) >= MAX_HELD:
            held.pop(next(iter(held)))
        held[key] = value

    async def on_load(self) -> None:
        self.bot.timers.handler(TICK, self._tick_due)
        self.bot.timers.handler(ROLE_ENDS, self._role_ends_due)
        for kind in POSTED:
            self.bot.timers.handler(DELETE[kind], self._delete_due)
        self.bot.timers.handler(LEAVE_RETRY, self._delete_due)
        self.bot.settings.listen(self.key, self._settings_changed)
        self.bot.settings.listen(CARDS, self._picture_settings_changed)
        for guild_id in self.bot.settings.guilds_with(self.key):
            cfg = self.cfg(guild_id)
            self._lifetimes[guild_id] = tuple(cfg[f"{kind}_delete_after"] for kind in POSTED)
            if await self.bot.timers.get_key(TICK, str(guild_id)) is None:
                await self._schedule_tick(guild_id, cfg)
        for guild_id in {*self.bot.settings.guilds_with(self.key), *self.bot.settings.guilds_with(CARDS)}:
            if self._shows_invite(guild_id):
                self._invite_servers.add(guild_id)
        await self._clear_old_posts(time.time())

    async def on_guild_restored(self, guild_id: int) -> None:
        """A backup brought back the posts as they were noted then. Their timers are made again from them, under the lifetimes as they are now."""
        cfg = self.cfg(guild_id)
        self._lifetimes[guild_id] = tuple(cfg[f"{kind}_delete_after"] for kind in POSTED)
        for kind in POSTED:
            await self._move_delete_timers(guild_id, kind)
        self._follow_invites(guild_id)

    async def _settings_changed(self, guild_id: int, settings: dict) -> None:
        await self._schedule_tick(guild_id, settings)
        lifetimes = tuple(settings[f"{kind}_delete_after"] for kind in POSTED)
        before = self._lifetimes.get(guild_id, (0,) * len(POSTED))
        self._lifetimes[guild_id] = lifetimes
        for place, kind in enumerate(POSTED):
            if lifetimes[place] == before[place]:
                continue
            try:
                await self._move_delete_timers(guild_id, kind)
            except Exception:
                # The setting is saved either way. This kind is marked as not followed, so the next save of this page moves its timers.
                log.exception("Could not move the %s delete timers of %s", kind, guild_id)
                followed = list(self._lifetimes.get(guild_id, lifetimes))
                followed[place] = -1
                self._lifetimes[guild_id] = tuple(followed)
        self._follow_invites(guild_id)

    async def _picture_settings_changed(self, guild_id: int, settings: dict) -> None:
        self._follow_invites(guild_id)

    # ---- the invite a join came through ----

    def _shows_invite(self, guild_id: int) -> bool:
        """Whether anything this server sends at a join shows the invite: a welcome or a join DM that's switched on, or the welcome image's own settings."""
        cfg = self.cfg(guild_id)
        in_welcome = _holds(cfg["welcome"], INVITE_BLOCK)
        if cfg["welcome_on"] and in_welcome:
            return True
        if cfg["dm_on"] and (in_welcome if cfg["dm_same_as_welcome"] else _holds(cfg["dm"], INVITE_BLOCK)):
            return True
        return self.bot.module(CARDS) is not None and _holds(self.bot.settings.get(guild_id, CARDS), INVITE_BLOCK)

    def _follow_invites(self, guild_id: int) -> None:
        """Keep a server's invite counts for as long as something it sends shows the invite, and no longer."""
        if not self._shows_invite(guild_id):
            self._invite_servers.discard(guild_id)
            self.invites.stop(guild_id)
            return
        self._invite_servers.add(guild_id)
        guild = self.bot.get_guild(guild_id)
        if guild is not None and not self.invites.watching(guild_id):
            self._spawn(self._read_invites(guild))

    async def _read_invites(self, guild: Any) -> None:
        try:
            await self.invites.watch(guild)
        except Exception:
            log.exception("Could not read the invites of %s", guild.id)

    async def _used_invite(self, member: Any) -> Any:
        try:
            return await self.invites.used_by(member)
        except Exception:
            log.exception("Could not tell which invite %s used in %s", member.id, member.guild.id)
            return None

    async def _invite_for(self, member: Any, joined_now: bool) -> Any:
        """The invite this member joined through. At the join itself it's read from Discord, for at most INVITE_WAIT. Later it's whatever was noted at their join, while that's still held."""
        if not joined_now:
            return self.invites.kept(member.guild.id, member.id)[1]
        reading = asyncio.ensure_future(self._used_invite(member))
        self._tasks.add(reading)
        reading.add_done_callback(self._tasks.discard)
        try:
            return await asyncio.wait_for(asyncio.shield(reading), self.INVITE_WAIT)
        except asyncio.TimeoutError:
            log.info("Reading the invites of %s took too long, so a welcome went out without its invite", member.guild.id)
            return None

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Invites were made and used while the bot was away, so each server's counts start over from what Discord says now."""
        for guild_id in sorted(self._invite_servers):
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                await self._read_invites(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: Any) -> None:
        if guild.id in self._invite_servers:
            await self._read_invites(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: Any) -> None:
        self.invites.stop(guild.id)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: Any) -> None:
        self.invites.created(invite)

    # ---- the joined count ----

    @staticmethod
    def _members(guild: Any) -> int:
        count = getattr(guild, "member_count", None)
        return count if count is not None else len(getattr(guild, "members", ()))

    @staticmethod
    def _join_key(member: Any) -> tuple | None:
        """Where a join stands in the order of joins: when it happened, then the member's ID, so two joins Discord stamped with one millisecond
        still get distinct places."""
        joined = getattr(member, "joined_at", None)
        return None if joined is None else (joined, member.id)

    def _first_count(self, guild: Any, newest: tuple | None, together: Any) -> tuple[int, int]:
        """A server's count when its first join is counted, and the number of the join at `newest`.

        discord.py puts a joining member in the member list and raises the member count before it starts any handler, and takes a leaving one
        off both (state.py, parse_guild_member_add and parse_guild_member_remove). So the count starts from those, less every listed join
        another write still has to count: those handed to writes that haven't landed (_pending), and those after `newest` in the order of joins,
        whose handlers haven't run. This write's own joins that are no longer listed left before it landed, and the member count went down with
        them, so they're added back. Every join this count leaves to another write keeps its place in the order of joins, for that write to
        give. Joins of `newest`'s millisecond that come before it and that no write holds are counted here, with their places kept too, so their
        own writes give them and add nothing."""
        count = self._members(guild)
        keys = {member.id: key for member in getattr(guild, "members", ()) if (key := self._join_key(member)) is not None}
        left = sum(1 for member_id in together if member_id not in keys)
        pending = self._pending.get(guild.id, set())
        still = {member_id for member_id, key in keys.items() if member_id not in together and (member_id in pending or (newest is not None and key > newest))}
        first = count - len(still) + left
        self._placed.pop(guild.id, None)
        if newest is None:
            return first, first
        ordered = sorted(keys.values())
        unlisted = count - len(ordered)  # members Discord counts that the list doesn't hold, as while a chunk is still coming in

        def place(key: tuple) -> int:
            return unlisted + bisect.bisect_right(ordered, key) + (left if key >= newest else 0)

        placed = {member_id: (place(keys[member_id]), True) for member_id in still}
        for member_id, key in keys.items():
            if member_id not in together and member_id not in still and key[0] == newest[0] and key < newest:
                placed[member_id] = (place(key), False)
        if placed:
            self._placed[guild.id] = placed
        return first, place(newest)

    async def _add_joins(self, guild: Any, joins: int, *, newest: tuple | None = None, together: Any = frozenset()) -> int:
        """Add joins to a server's count, and give the number the newest of them got. `newest` is that join's place in the order of joins
        (_join_key), and `together` is who the joins are.

        A server counted for the first time starts from _first_count, read inside the write, so joins that arrive in the same moment get the next
        numbers. A join whose place that count kept gets that place here, and adds itself only when that count left it to this write."""
        known: dict[int, tuple[int, bool]] = {}
        async with self.db.transaction():
            current = await self.db.scalar("SELECT joined FROM greeting_counts WHERE guild_id = ?", (guild.id,))
            if current is None:
                first, number = self._first_count(guild, newest, together)
                await self.db.run("INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, ?, ?)", (guild.id, first, time.time()))
            else:
                placed = self._placed.get(guild.id, {})
                known = {member_id: placed[member_id] for member_id in together if member_id in placed}
                adding = joins - sum(1 for _, adds in known.values() if not adds)
                if adding > 0:
                    await self.db.run("UPDATE greeting_counts SET joined = joined + ?, updated_at = ? WHERE guild_id = ?", (adding, time.time(), guild.id))
                number = known[newest[1]][0] if newest is not None and newest[1] in known else current + max(adding, 0)
        self._landed(guild.id, together, known)
        return number

    def _landed(self, guild_id: int, together: Any, known: dict) -> None:
        """A write landed: its joins are no longer waiting, and the places it gave are used."""
        for held, done in ((self._pending, together), (self._placed, known)):
            waiting = held.get(guild_id)
            if waiting is not None:
                for member_id in done:
                    if isinstance(waiting, set):
                        waiting.discard(member_id)
                    else:
                        waiting.pop(member_id, None)
                if not waiting:
                    del held[guild_id]

    def _put_back(self, guild: Any, joins: int, ids: Any = ()) -> None:
        """Joins a write couldn't count. The next join that's counted takes these along, and until then they're still waiting."""
        self._uncounted[guild.id] = self._uncounted.get(guild.id, 0) + joins
        if ids:
            self._uncounted_ids.setdefault(guild.id, set()).update(ids)

    async def _count_join(self, member: Any) -> int | None:
        """Count this join, with any before it that are still waiting to be counted, and give the number this member got.

        None when the database couldn't say in time. The welcome then shows the count as last read, since a storage error or a busy database must
        never cost anyone their welcome. A write that takes longer than COUNT_PATIENCE runs on by itself, so a join is never counted twice."""
        guild = member.guild
        joins = self._uncounted.pop(guild.id, 0) + 1
        together = self._uncounted_ids.pop(guild.id, set()) | {member.id}  # the skipped joins this write takes along, and this one
        self._newest_uncounted.pop(guild.id, None)  # they came before this one
        self._pending.setdefault(guild.id, set()).update(together)
        writing = asyncio.ensure_future(self._add_joins(guild, joins, newest=self._join_key(member), together=together))
        self._tasks.add(writing)
        writing.add_done_callback(self._tasks.discard)
        try:
            # A write waits its turn behind every other write. The welcome waits this long for it and no longer.
            return await asyncio.wait_for(asyncio.shield(writing), self.COUNT_PATIENCE)
        except asyncio.TimeoutError:
            log.error("Counting a join in %s is taking a while, so the welcome shows the count as last read", guild.id)
            writing.add_done_callback(lambda task: self._put_back(guild, joins, together) if task.cancelled() or task.exception() is not None else None)
            return None
        except Exception:
            log.exception("Could not count a join in %s", guild.id)
            self._put_back(guild, joins, together)
            return None

    def _count_later(self, member: Any) -> None:
        """Count a join that gets no welcome. In a raid that's all a join costs: one more on a number in memory, which a single write adds to the server's count."""
        guild = member.guild
        self._uncounted[guild.id] = self._uncounted.get(guild.id, 0) + 1
        self._uncounted_ids.setdefault(guild.id, set()).add(member.id)
        key = self._join_key(member)
        if key is not None and (self._newest_uncounted.get(guild.id) is None or key > self._newest_uncounted[guild.id]):
            self._newest_uncounted[guild.id] = key
        if guild.id not in self._counting:
            self._counting.add(guild.id)
            self._spawn(self._write_counts(guild))

    async def _write_counts(self, guild: Any) -> None:
        try:
            while waiting := self._uncounted.pop(guild.id, 0):
                newest = self._newest_uncounted.pop(guild.id, None)
                together = self._uncounted_ids.pop(guild.id, set())
                try:
                    await self._add_joins(guild, waiting, newest=newest, together=together)
                except Exception:
                    log.exception("Could not count %d joins in %s", waiting, guild.id)
                    self._put_back(guild, waiting, together)
                    return
        finally:
            self._counting.discard(guild.id)

    async def _stored_count(self, guild_id: int) -> int | None:
        try:
            return await self.db.scalar("SELECT joined FROM greeting_counts WHERE guild_id = ?", (guild_id,))
        except Exception:
            log.exception("Could not read the joined count of %s", guild_id)
            return None

    async def _joined_count(self, guild: Any) -> int:
        """The count as it stands, for a goodbye, a test or a preview. A server that was never counted reads as its member count, which is where its count will start."""
        stored = await self._stored_count(guild.id)
        return stored if stored is not None else self._members(guild)

    async def _set_joined(self, guild_id: int, number: int) -> None:
        await self.db.run(
            "INSERT INTO greeting_counts (guild_id, joined, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE SET joined = excluded.joined, updated_at = excluded.updated_at",
            (guild_id, number, time.time()),
        )

    # ---- what a greeting shows ----

    @staticmethod
    def _values(joined: int, invite: Any) -> dict:
        return {"joined": str(joined), "invite": objects.invite(invite)}

    def _values_now(self, guild: Any) -> Callable[[], Awaitable[dict]]:
        """The values for a goodbye or a ban message: the count as it stands, and no invite."""
        async def values() -> dict:
            return self._values(await self._joined_count(guild), None)
        return values

    async def scope_for(self, member: Any, channel: Any = None, preview: bool = False) -> Scope:
        """What a greeting about this member can show, the joined count and the invite included. The welcome image draws its text from the same values.

        With `preview`, the invite is a sample one, because nobody joined. Without it, the count and the invite are the ones noted at this member's own join
        while those are still held, and otherwise the count as it stands and no invite. This never counts a join."""
        guild = member.guild
        if preview:
            invite = invitewatch.sample(guild, channel)
            joined = await self._joined_count(guild)
        else:
            invite = self.invites.kept(guild.id, member.id)[1]
            joined = self._join_numbers.get((guild.id, member.id))
            if joined is None:
                joined = await self._joined_count(guild)
        return Scope(guild=guild, member=member, channel=channel, extra=self._values(joined, invite))

    async def _picture(self, member: Any, kind: str, scope: Scope) -> dict | None:
        """The welcome image for this member, from the module that draws it, or None when there's none to attach. Whatever goes wrong there, the greeting still goes out."""
        cards = self.bot.module(CARDS)
        if cards is None:
            return None
        try:
            picture = await asyncio.wait_for(cards.picture_for(member, kind, scope), self.PICTURE_WAIT)
        except Exception as error:  # the picture is the other module's to get right, and a welcome can't wait on it or fail with it
            log.warning("The welcome image for %s in %s didn't come: %r", member.id, member.guild.id, error)
            return None
        if not isinstance(picture, dict) or not isinstance(picture.get("data"), (bytes, bytearray)) or not picture["data"]:
            return None
        name = picture.get("filename")
        if not isinstance(name, str) or not _PICTURE_NAME.fullmatch(name):
            name = "welcome.gif" if bytes(picture["data"][:4]) == b"GIF8" else "welcome.png"
        return {"filename": name, "data": bytes(picture["data"]), "in_embed": bool(picture.get("in_embed"))}

    @staticmethod
    def _pings(mode: str, stored: dict, shown: str, marked: str, member: Any, *, kept: bool = False) -> discord.AllowedMentions:
        """Who a post about this member pings: the member, the people the template mentions, and, as the server chose in `mode`, the roles and the
        @everyone or @here it mentions.

        Discord reads pings from the finished text, so this reads it too, with every character that came from outside the template marked (`marked`,
        from render_message). A mention counts only when the template wrote all of it, in the branch it took, and its ID is one the stored text holds.
        So a name can't ping anyone, a branch the template didn't take can't either, and a mention the template builds from its own text pings as it
        always did."""
        users, roles, ours, theirs = _template_mentions(stored, shown, marked)
        people = [discord.Object(user_id) for user_id in dict.fromkeys([member.id, *users]) if user_id <= _BIGGEST_ID][:100]
        if mode == "member" or kept:
            # `kept`: the text went out as written, so none of its blocks ran and no branch was taken. Its people still ping, as they always did.
            return discord.AllowedMentions(everyone=False, roles=False, users=people)
        groups = [discord.Object(role_id) for role_id in dict.fromkeys(roles) if role_id <= _BIGGEST_ID][:100]
        # Discord pings for any @everyone in the text once everyone is allowed, one inside a name too. So it's allowed only when the template asks for at least that much.
        everyone = mode == "all" and bool(ours) and ("@everyone" in ours or theirs <= ours)
        return discord.AllowedMentions(everyone=everyone, roles=groups, users=people)

    @staticmethod
    def _can_attach(guild: Any, channel: Any) -> bool:
        try:
            return guild.me is None or bool(channel.permissions_for(guild.me).attach_files)
        except Exception:  # a thread whose parent isn't known has no answer, and Discord gives the real one
            return True

    # ---- posting ----

    async def _post(self, guild: Any, channel_id: int | None, stored: dict, member: Any, kind: str | None = None, *, cfg: dict | None = None,
                    values: Callable[[], Awaitable[dict]] | None = None, picture: bool = False) -> Any:
        """Post one greeting in its channel and note it. Gives the message that went out, or None."""
        # Settings hold whatever ID was sent, so a channel is only ever looked up inside its own server.
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        draws = picture and self.bot.module(CARDS) is not None
        if not messages.has_content(stored) and not draws:
            return None
        if not hasattr(channel, "send"):
            if channel_id and messages.has_content(stored):
                self._note_problem(guild.id, kind, f"The {kind} message's channel is gone. Pick another one above.")
            return None
        scope = Scope(guild=guild, member=member, channel=channel, extra=await values() if values is not None else {})
        marks: dict = {}
        payload = await render_message(stored, scope, marks=marks)
        drawn = await self._picture(member, "welcome", scope) if draws else None
        if drawn is not None and not self._can_attach(guild, channel):
            self._note_problem(guild.id, "welcome image", f"The bot can't attach the welcome image in #{channel.name}. Give it Attach Files there.")
            drawn = None
        if not messages.has_content(payload) and drawn is None:
            return None
        mode = cfg[f"{kind}_mentions"] if kind in MENTIONED and cfg is not None else "member"
        mentions = self._pings(mode, stored, payload["content"], marks.get("content", ""), member, kept=marks.get("kept", False))
        try:
            sent = await channel.send(**_outgoing(payload, drawn, mentions))
        except discord.Forbidden as error:
            log.warning("Could not post a greeting in %s: %s", channel_id, error)
            needs = "View Channel, Send Messages, Embed Links and Attach Files" if drawn is not None else "View Channel, Send Messages and Embed Links"
            self._note_problem(guild.id, kind, f"The bot can't post the {kind} message in #{channel.name}. Give it {needs} there.")
            return None
        except discord.HTTPException as error:
            log.warning("Could not post a greeting in %s: %s", channel_id, error)
            self._note_problem(guild.id, kind, f"Discord refused the {kind} message: {error.text or error.status}")
            return None
        self._note_problem(guild.id, kind, None)
        if drawn is not None:
            self._note_problem(guild.id, "welcome image", None)
        if kind in DELETE and cfg is not None:
            await self._note_post(guild, member.id, kind, sent)
            if kind == "welcome" and cfg["welcome_delete_on_leave"] and guild.get_member(member.id) is None:
                await self._delete_welcomes(guild, member.id)  # they left while their welcome was on its way
        return sent

    async def _send_join_dm(self, member: Any, stored: dict, *, to: Any = None, values: Callable[[], Awaitable[dict]] | None = None, channel: Any = None,
                            note: bool = False) -> bool:
        """Whether it went out. `to` is who gets it when that isn't the member it's about, which is how a test reaches the person testing."""
        recipient = member if to is None else to
        draws = self.bot.module(CARDS) is not None
        if recipient.bot or (not messages.has_content(stored) and not draws):
            return False
        scope = Scope(guild=member.guild, member=member, channel=channel, extra=await values() if values is not None else {})
        payload = await render_message(stored, scope)
        drawn = await self._picture(member, "dm", scope) if draws else None
        if not messages.has_content(payload) and drawn is None:
            return False
        try:
            sent = await recipient.send(**_outgoing(payload, drawn, discord.AllowedMentions.none()))
        except discord.HTTPException:
            return False  # plenty of people close their DMs
        if note:
            await self._note_post(member.guild, member.id, "dm", sent)
        return True

    def _in_burst(self, guild_id: int, limit: int) -> bool:
        """Whether this join is past the server's limit for joins in a minute. A raid shouldn't fill the welcome channel, and that many unasked-for DMs can get a bot's DMs restricted."""
        if not limit:
            return False
        now = time.monotonic()
        joins = self._joins.setdefault(guild_id, collections.deque())
        joins.append(now)
        while joins and (now - joins[0] > BURST_SECONDS or len(joins) > limit + 1):
            joins.popleft()
        return len(joins) > limit

    def _leaving_in_burst(self, guild: Any, cfg: dict, channel_id: int | None) -> bool:
        """The same limit for goodbye and ban messages, with its own count. A massban or a raid leaving would otherwise put one post per person in a channel, at Discord's pace, for as long as that takes."""
        key = (guild.id, LEAVING)
        if not self._in_burst(key, cfg["burst_joins"]):
            return False
        self._burst_held[key] = self._burst_held.get(key, 0) + 1
        if key not in self._burst_tasks:
            self._burst_tasks[key] = asyncio.ensure_future(self._end_burst(guild, key, channel_id))
        return True

    async def _end_burst(self, guild: Any, key: Any = None, channel_id: int | None = None) -> None:
        """Once things have gone quiet, one line says how many weren't posted one by one."""
        key = guild.id if key is None else key
        try:
            while True:
                held = self._burst_held.get(key, 0)
                await asyncio.sleep(self.BURST_QUIET)
                if self._burst_held.get(key, 0) == held:
                    break
            count = self._burst_held.pop(key, 0)
            channel_id = channel_id or self.cfg(guild.id)["welcome_channel"]
            channel = guild.get_channel_or_thread(channel_id) if channel_id else None
            if count and hasattr(channel, "send"):
                try:
                    what = "left or were banned" if key != guild.id else "joined"
                    await channel.send(f"{count} more {'person' if count == 1 else 'people'} {what}.", allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as error:
                    log.warning("Could not post the end of a burst of joins in %s: %s", channel_id, error)
        finally:
            self._burst_tasks.pop(key, None)

    async def _welcomed_lately(self, key: tuple[int, int], seconds: int) -> bool:
        """Whether this member was welcomed within the server's rejoin time. If not, this welcome is held as the one the next rejoin is measured from,
        and `_welcome` lets it go again when nothing reached them."""
        now = time.monotonic()
        last = self._welcomed.get(key)
        if last is not None and now - last < seconds:
            return True
        try:
            # What memory holds is gone after a restart. The posts the bot noted aren't.
            noted = await self.db.scalar("SELECT MAX(posted_at) FROM greeting_posts WHERE guild_id = ? AND user_id = ? AND kind IN ('welcome', 'dm')", key)
        except Exception:
            log.exception("Could not read when %s was last welcomed in %s", key[1], key[0])
            noted = None
        if noted is not None and time.time() - noted < seconds:
            return True
        self._hold(self._welcomed, key, now)
        return False

    async def _welcome(self, member: Any, cfg: dict, *, joined_now: bool = False) -> None:
        """Welcome one member. `joined_now` says this is their join itself, which is when they're counted and their invite can be told.
        Without it they joined earlier and waited on the rules screen, and what was noted for them then is used."""
        guild = member.guild
        if self._in_burst(guild.id, cfg["burst_joins"]):
            if joined_now:
                self._count_later(member)
            else:
                self._join_numbers.pop((guild.id, member.id), None)  # no welcome will show the number held for them
            self._burst_held[guild.id] = self._burst_held.get(guild.id, 0) + 1
            if guild.id not in self._burst_tasks:
                self._burst_tasks[guild.id] = asyncio.ensure_future(self._end_burst(guild))
            return
        key = (guild.id, member.id)
        held = self._join_numbers.pop(key, None)
        number = await self._count_join(member) if joined_now else held
        posts = cfg["welcome_on"] and (cfg["welcome_bots"] or not member.bot)
        dms = cfg["dm_on"] and not member.bot
        if not posts and not dms:
            return
        if cfg["welcome_again_after"] and await self._welcomed_lately(key, cfg["welcome_again_after"] * 60):
            return
        shared: dict = {}

        async def values() -> dict:
            """The count and the invite, worked out once for the post, the DM and both pictures, and only when one of them goes out."""
            if not shared:
                joined = number if number is not None else await self._joined_count(guild)
                invite = await self._invite_for(member, joined_now) if guild.id in self._invite_servers else None
                shared.update(self._values(joined, invite))
            return shared

        held = self._welcomed.get(key) if cfg["welcome_again_after"] else None
        posted = None
        if posts:
            posted = await self._post(guild, cfg["welcome_channel"], cfg["welcome"], member, "welcome", cfg=cfg, values=values, picture=True)
        sent = False
        if dms:
            same = cfg["dm_same_as_welcome"]
            # The welcome message sent as the DM reads the welcome channel too, so it says what the post says.
            channel = guild.get_channel_or_thread(cfg["welcome_channel"]) if same and cfg["welcome_channel"] else None
            sent = await self._send_join_dm(member, cfg["welcome"] if same else cfg["dm"], values=values, channel=channel, note=bool(cfg["welcome_again_after"]))
        if held is not None and posted is None and not sent and self._welcomed.get(key) == held:
            del self._welcomed[key]  # nothing reached them, so a rejoin isn't measured from this join

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        cfg = self.cfg(guild.id)
        # Every join is counted for the invite reader, a skipped one too, so its use can't be pinned on the next person.
        self.invites.arrived(member)
        if cfg["wait_for_rules"] and member.pending:
            # The welcome comes once they accept the rules. Their number and their invite can only be told now, so both are noted for it.
            # Past the limit for joins in a minute they're only counted, as a join that gets no welcome is.
            if self._in_burst((guild.id, SCREENING), cfg["burst_joins"]):
                self._count_later(member)
                return
            number = await self._count_join(member)
            if number is not None:
                self._hold(self._join_numbers, (guild.id, member.id), number)
            if guild.id in self._invite_servers:
                await self._used_invite(member)
            return
        await self._welcome(member, cfg, joined_now=True)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.pending and not after.pending:
            cfg = self.cfg(after.guild.id)
            if cfg["wait_for_rules"]:
                await self._welcome(after, cfg)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        cfg = self.cfg(member.guild.id)
        channel_id = cfg["farewell_channel"] or cfg["welcome_channel"]
        if not channel_id or not cfg["farewell_on"] or not messages.has_content(cfg["farewell"]):
            return
        if cfg["wait_for_rules"] and member.pending:
            return  # never welcomed, so no goodbye either
        if cfg["ban_on"] and messages.has_content(cfg["ban"]):
            # Discord sends a ban and a leave for the same person, in either order, and one post is enough.
            await asyncio.sleep(self.BAN_WAIT)
            if (member.guild.id, member.id) in self._recent_bans:
                return
        if self._leaving_in_burst(member.guild, cfg, channel_id):
            return
        await self._post(member.guild, channel_id, cfg["farewell"], member, "farewell", cfg=cfg, values=self._values_now(member.guild))

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: Any) -> None:
        """Someone left, was kicked or was banned. What was held for their join goes, and so do their welcome posts where the server asked for that."""
        user_id = payload.user.id
        self._join_numbers.pop((payload.guild_id, user_id), None)
        self.invites.forget(payload.guild_id, user_id)
        if self.cfg(payload.guild_id)["welcome_delete_on_leave"]:
            guild = self.bot.get_guild(payload.guild_id)
            if guild is not None:
                await self._delete_welcomes(guild, user_id)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: Any) -> None:
        now = time.monotonic()
        self._recent_bans = {key: at for key, at in self._recent_bans.items() if now - at < 60}
        self._recent_bans[(guild.id, user.id)] = now
        cfg = self.cfg(guild.id)
        channel_id = cfg["ban_channel"] or cfg["farewell_channel"] or cfg["welcome_channel"]
        if cfg["ban_on"] and messages.has_content(cfg["ban"]) and channel_id and not self._leaving_in_burst(guild, cfg, channel_id):
            await self._post(guild, channel_id, cfg["ban"], user, "ban", cfg=cfg, values=self._values_now(guild))
        if cfg["welcome_delete_on_leave"]:
            await self._delete_welcomes(guild, user.id)  # a ban of someone who left earlier has no leave event to do this

    # ---- deleting posts later ----

    async def _note_post(self, guild: Any, user_id: int, kind: str, sent: Any) -> None:
        """Note a post the bot just made, and set its timer when its kind has a lifetime. The post is already out, so nothing here may raise."""
        message_id = getattr(sent, "id", None)
        if message_id is None:
            return
        channel_id = getattr(getattr(sent, "channel", None), "id", None) or 0
        now = time.time()
        try:
            async with self.db.transaction():
                await self.db.run(
                    "INSERT OR REPLACE INTO greeting_posts (guild_id, user_id, kind, channel_id, message_id, posted_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (guild.id, user_id, kind, channel_id, message_id, now),
                )
                # The lifetime is read inside the same write as the note. A change to it either finds this post noted and moves its timer, or is already what's read here.
                seconds = self.cfg(guild.id)[f"{kind}_delete_after"] if kind in DELETE else 0
                if seconds:
                    due = max(now + seconds, self._next_delete.get((guild.id, kind), 0.0))
                    self._next_delete[(guild.id, kind)] = due + DELETE_SPACING
                    await self.bot.timers.schedule(DELETE[kind], due, {"guild_id": guild.id, "channel_id": channel_id, "message_id": message_id}, guild_id=guild.id, key=str(message_id))
            self._noted.add(guild.id)
            await self._clear_old_posts(now)
        except Exception:
            log.exception("Could not note the %s post %s in %s", kind, message_id, guild.id)

    async def _clear_old_posts(self, now: float) -> None:
        """Once an hour at most: posts noted over 30 days ago go, and so do a server's oldest past the most it can have noted."""
        if now - self._cleared_at < 3600:
            return
        self._cleared_at = now
        try:
            await self.db.run("DELETE FROM greeting_posts WHERE posted_at < ?", (now - KEEP_POSTS,))
            for guild_id in sorted(self._noted):
                await self.db.run(
                    "DELETE FROM greeting_posts WHERE guild_id = ? AND message_id IN (SELECT message_id FROM greeting_posts WHERE guild_id = ? ORDER BY posted_at DESC LIMIT -1 OFFSET ?)",
                    (guild_id, guild_id, MAX_POSTS),
                )
            self._noted.clear()
        except Exception:
            log.exception("Could not clear out old greeting posts")

    async def _move_delete_timers(self, guild_id: int, kind: str) -> None:
        """Make every noted post of one kind follow its lifetime as it is now: its timer moves, or goes when the lifetime is 0.

        Posts already past the new lifetime go one at a time, oldest first, a few seconds apart. A timer whose post is no longer noted stays as it is
        while there's a lifetime, since the delete it holds is still wanted."""
        now = time.time()
        async with self.db.transaction():
            # The lifetime and the posts are read inside the one write that replaces the timers, so a post noted at the same moment is in it or follows it, never lost between.
            seconds = self.cfg(guild_id)[f"{kind}_delete_after"]
            if not seconds:
                await self.db.run("DELETE FROM timers WHERE kind = ? AND guild_id = ?", (DELETE[kind], guild_id))
                self._next_delete.pop((guild_id, kind), None)
                return
            rows = await self.db.all("SELECT channel_id, message_id, posted_at FROM greeting_posts WHERE guild_id = ? AND kind = ? ORDER BY posted_at", (guild_id, kind))
            slot = now
            timers = []
            for row in rows:
                due = row["posted_at"] + seconds
                if due <= slot:
                    due, slot = slot, slot + BACKLOG_SPACING
                payload = {"guild_id": guild_id, "channel_id": row["channel_id"], "message_id": row["message_id"]}
                timers.append((guild_id, DELETE[kind], str(row["message_id"]), due, json.dumps(payload), now))
            await self.db.run(
                "DELETE FROM timers WHERE kind = ? AND guild_id = ? AND key IN (SELECT CAST(message_id AS TEXT) FROM greeting_posts WHERE guild_id = ? AND kind = ?)",
                (DELETE[kind], guild_id, guild_id, kind),
            )
            if timers:
                await self.db.many("INSERT INTO timers (guild_id, kind, key, due_at, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)", timers)
            # A post made from now on is spaced after the backlog, and no longer after timers the old lifetime had set.
            self._next_delete[(guild_id, kind)] = slot
        self.bot.timers.nudge()

    async def _delete_post(self, guild: Any, channel_id: int, message_id: int, patience: float | None = None) -> None:
        """Delete one of the bot's own posts. Coming back means there's nothing left to do: it's gone, or it can never be deleted.
        It raises when Discord is having a bad minute, keeps the delete waiting, or forbids it (discord.Forbidden), so the caller can try again later.
        Looking the channel up and deleting share the patience, since Discord can keep either waiting: an archived thread is looked up there."""
        try:
            await asyncio.wait_for(self._find_and_delete(guild, channel_id, message_id), self.DELETE_PATIENCE if patience is None else patience)
        except discord.NotFound:
            pass  # someone deleted it already
        except discord.HTTPException as error:
            if error.status >= 500 or error.status == 429 or isinstance(error, discord.Forbidden):
                raise  # Discord's own trouble, which passes, or a channel the bot can't see right now and may again
            log.info("Could not delete the greeting %s in %s, and asking again won't help: %s", message_id, channel_id, error)

    @staticmethod
    async def _find_and_delete(guild: Any, channel_id: int, message_id: int) -> None:
        channel = await delivery.find_channel(guild, channel_id)
        if channel is None or not hasattr(channel, "get_partial_message"):
            return  # the channel is gone, and its messages went with it
        await channel.get_partial_message(message_id).delete()

    def _delete_patience(self, now: float | None = None) -> float:
        """How long the next delete may hold the timer loop: its own patience, cut to what's left of DELETE_HOLD over the last DELETE_WINDOW."""
        now = time.monotonic() if now is None else now
        while self._delete_waits and now - self._delete_waits[0][0] > DELETE_WINDOW:
            self._delete_waits.popleft()
        return min(self.DELETE_PATIENCE, self.DELETE_HOLD - sum(seconds for _, seconds in self._delete_waits))

    def _put_off_slot(self, guild_id: int) -> float:
        """When the next delete put off comes back: DELETE_RETRY from now, spaced after the others put off, so they don't all come due together."""
        due = max(time.time() + DELETE_RETRY, self._next_put_off.get(guild_id, 0.0))
        self._next_put_off[guild_id] = due + DELETE_SPACING
        return due

    async def _put_off(self, timer: dict) -> None:
        """Put one delete back for later, in one write."""
        await self.db.run("UPDATE timers SET due_at = ? WHERE id = ? AND due_at = ?", (self._put_off_slot(timer["guild_id"]), timer["id"], timer["due_at"]))

    async def _put_off_due(self, timer: dict) -> None:
        """Put this delete back for later, with every other one due before the hold could be whole again, spaced, in one write, so they hold the
        timers behind them no longer than that. This one is always among them, since a handled row whose time doesn't move is one the loop deletes."""
        kinds = (*DELETE.values(), LEAVE_RETRY)
        until = max(time.time(), timer["due_at"]) + DELETE_WINDOW
        rows = await self.db.all(f"SELECT id, guild_id, due_at FROM timers WHERE kind IN ({', '.join('?' * len(kinds))}) AND due_at <= ? ORDER BY due_at, id", (*kinds, until))
        if rows:
            await self.db.many("UPDATE timers SET due_at = ? WHERE id = ? AND due_at = ?", [(self._put_off_slot(row["guild_id"]), row["id"], row["due_at"]) for row in rows])

    async def _delete_due(self, payload: dict, timer: dict) -> None:
        """A post's lifetime is over. A second run does no harm, since a message that's already gone counts as deleted.
        All of it, the lookup, the delete and putting it back, counts toward DELETE_HOLD, since every other feature's timers wait behind it."""
        began = time.monotonic()
        try:
            await self._delete_in_turn(payload, timer, self._delete_patience(began))
        finally:
            self._delete_waits.append((began, time.monotonic() - began))

    async def _delete_in_turn(self, payload: dict, timer: dict, patience: float) -> None:
        guild = self.bot.get_guild(payload["guild_id"])
        if guild is not None:
            if patience <= 0:
                # Deletes have held every other feature's timers long enough this minute. This one and every other due now come back later.
                await self._put_off_due(timer)
                return
            try:
                await self._delete_post(guild, payload["channel_id"], payload["message_id"], patience)
            except asyncio.TimeoutError:
                # Discord is making deletes wait. This one comes back in a while, so the timers behind it aren't held up.
                await self._put_off(timer)
                return
            except discord.Forbidden as error:
                posted = await self.db.scalar("SELECT posted_at FROM greeting_posts WHERE guild_id = ? AND message_id = ?", (payload["guild_id"], payload["message_id"]))
                if posted is not None and time.time() < posted + KEEP_POSTS:
                    log.info("Discord forbids deleting the greeting %s in %s for now, so it comes back in an hour: %s", payload["message_id"], payload["channel_id"], error)
                    await self.bot.timers.schedule(timer["kind"], time.time() + FORBIDDEN_RETRY, payload, guild_id=payload["guild_id"], key=timer["key"])
                    return
                log.info("Deleting the greeting %s in %s stayed forbidden until it was no longer noted, so it's left: %s", payload["message_id"], payload["channel_id"], error)
        await self.db.run("DELETE FROM greeting_posts WHERE guild_id = ? AND message_id = ?", (payload["guild_id"], payload["message_id"]))

    async def _delete_welcomes(self, guild: Any, user_id: int) -> None:
        """Delete every welcome post noted for one member. One that can't be deleted right now goes on a timer, which tries again in a while."""
        try:
            rows = await self.db.all(
                "SELECT channel_id, message_id FROM greeting_posts WHERE guild_id = ? AND user_id = ? AND kind = 'welcome' ORDER BY posted_at LIMIT 50", (guild.id, user_id),
            )
            for row in rows:
                key = str(row["message_id"])
                try:
                    await self._delete_post(guild, row["channel_id"], row["message_id"])
                except (discord.HTTPException, asyncio.TimeoutError) as error:
                    log.info("Could not delete the welcome %s of %s, who left %s: %r", row["message_id"], user_id, guild.id, error)
                    payload = {"guild_id": guild.id, "channel_id": row["channel_id"], "message_id": row["message_id"]}
                    await self.bot.timers.schedule(LEAVE_RETRY, time.time() + DELETE_RETRY, payload, guild_id=guild.id, key=key)
                    continue
                await self.db.run("DELETE FROM greeting_posts WHERE guild_id = ? AND message_id = ?", (guild.id, row["message_id"]))
                await self.bot.timers.cancel_key(DELETE["welcome"], key)
                await self.bot.timers.cancel_key(LEAVE_RETRY, key)
        except Exception:
            log.exception("Could not delete the welcome posts of %s in %s", user_id, guild.id)

    # ---- birthdays ----

    async def _schedule_tick(self, guild_id: int, cfg: dict, *, soon: bool = False) -> None:
        if not cfg["birthday_channel"] and not cfg["birthday_role"]:
            await self.bot.timers.cancel_key(TICK, str(guild_id))
            return
        now = time.time()
        due = now + 3600 if soon else next_run(cfg["birthday_hour"], now)
        await self.bot.timers.schedule(TICK, due, {"guild_id": guild_id}, guild_id=guild_id, key=str(guild_id))

    async def _tick_due(self, payload: dict, timer: dict) -> None:
        guild_id = payload["guild_id"]
        cfg = self.cfg(guild_id)
        guild = self.bot.get_guild(guild_id)
        retry = await self._announce(guild, cfg, since=timer.get("due_at")) if guild is not None else False
        await self._schedule_tick(guild_id, cfg, soon=retry)

    async def _announce(self, guild: Any, cfg: dict, *, since: float | None = None) -> bool:
        """Announce today's birthdays and hand out the role, and those of any days the bot missed since `since`, up to a week back. True means a post failed and the rest of the day can try again."""
        now = datetime.datetime.now(datetime.timezone.utc)
        missed = 0
        if since is not None:
            missed = min((now.date() - datetime.datetime.fromtimestamp(since, datetime.timezone.utc).date()).days, MAX_LATE_DAYS)
        failed = False
        for back in range(max(missed, 0), -1, -1):
            if await self._announce_day(guild, cfg, now - datetime.timedelta(days=back)):
                failed = True
        return failed

    async def _announce_day(self, guild: Any, cfg: dict, moment: datetime.datetime) -> bool:
        marks = " OR ".join("(month = ? AND day = ?)" for _ in dates_for(moment))
        dates = [part for date in dates_for(moment) for part in date]
        rows = await self.db.all(
            f"SELECT user_id FROM birthdays WHERE guild_id = ? AND ({marks}) AND (announced_year IS NULL OR announced_year != ?)",
            (guild.id, *dates, moment.year),
        )
        people = []
        for row in rows:
            member = guild.get_member(row["user_id"])
            if member is None:
                if getattr(guild, "chunked", False):  # the member list is whole, so they've left. Their row stays, since people come back.
                    continue
                try:
                    member = await guild.fetch_member(row["user_id"])
                except discord.HTTPException:
                    continue
            people.append(member)
        if not people:
            return False
        channel = guild.get_channel_or_thread(cfg["birthday_channel"]) if cfg["birthday_channel"] else None
        if not hasattr(channel, "send"):
            announced, failed = people, False
            if cfg["birthday_channel"]:
                self._note_problem(guild.id, "birthday", "The birthday message's channel is gone. Pick another one below.")
        elif cfg["birthday_combine"]:
            announced, failed = [], False
            for start in range(0, len(people), COMBINED_PEOPLE):
                group = people[start:start + COMBINED_PEOPLE]
                if await self._post_birthday(channel, guild, group, cfg["birthday"]):
                    announced.extend(group)
                else:
                    failed = True
        else:
            announced, failed = [], False
            for member in people:
                if await self._post_birthday(channel, guild, [member], cfg["birthday"]):
                    announced.append(member)
                else:
                    failed = True
        for member in announced:
            await self.db.run("UPDATE birthdays SET announced_year = ? WHERE guild_id = ? AND user_id = ?", (moment.year, guild.id, member.id))
            await self._give_birthday_role(guild, member, cfg)
        return failed

    async def _post_birthday(self, channel: Any, guild: Any, people: list, stored: dict) -> bool:
        if not messages.has_content(stored):
            return True
        if len(people) > 1:
            mentions = ", ".join(member.mention for member in people)
            names = ", ".join(plain_name(member).translate(_BRACES) for member in people)
            stored = messages.map_text(stored, lambda text: text.replace("{mention}", mentions).replace("{user}", names))
        payload = await render_message(stored, Scope(guild=guild, member=people[0], channel=channel))
        if not messages.has_content(payload):
            return True  # the template came out empty, and that's no reason to hold back the role or try again all day
        try:
            await channel.send(**messages.send_kwargs(payload, allowed_mentions=_MENTIONS))
        except discord.HTTPException as error:
            log.warning("Could not post a birthday in %s: %s", channel.id, error)
            if isinstance(error, discord.Forbidden):
                self._note_problem(guild.id, "birthday", f"The bot can't post the birthday message in #{channel.name}. Give it View Channel, Send Messages and Embed Links there.")
            else:
                self._note_problem(guild.id, "birthday", f"Discord refused the birthday message: {error.text or error.status}")
            return False
        self._note_problem(guild.id, "birthday", None)
        return True

    async def _give_birthday_role(self, guild: Any, member: Any, cfg: dict) -> None:
        if not cfg["birthday_role"]:
            return
        role = guild.get_role(cfg["birthday_role"])
        problem = None
        if role is None or role.is_default() or role.managed:
            problem = "The birthday role is gone or can't be handed out. Pick another one below."
        elif role >= guild.me.top_role:
            problem = f"{role.name} sits at or above the bot's highest role, so nobody gets it. Move the bot's role above it."
        # Anyone can claim a birthday, so this role counts as one members choose for themselves.
        elif not role_is_safe([name for name, on in role.permissions if on], self.bot.settings.get(guild.id, COMMANDS_MODULE)):
            problem = f"{role.name} holds moderator permissions, so the bot won't hand it out while elevated roles are off on the Commands page."
        if problem:
            log.info("Birthday role in %s: %s", guild.id, problem)
            self._note_problem(guild.id, "birthday role", problem)
            return
        try:
            await member.add_roles(role, reason="Birthday")
        except discord.HTTPException as error:
            log.warning("Could not give the birthday role to %s: %s", member.id, error)
            self._note_problem(guild.id, "birthday role", f"Discord refused the birthday role: {error.text or error.status}")
            return
        self._note_problem(guild.id, "birthday role", None)
        await self.bot.timers.schedule(
            ROLE_ENDS, time.time() + DAY, {"guild_id": guild.id, "user_id": member.id, "role_id": role.id}, guild_id=guild.id, key=f"{guild.id}:{member.id}",
        )

    async def _role_ends_due(self, payload: dict, timer: dict) -> None:
        guild = self.bot.get_guild(payload["guild_id"])
        member = guild.get_member(payload["user_id"]) if guild else None
        role = member.get_role(payload["role_id"]) if member else None
        if role is not None:
            try:
                await member.remove_roles(role, reason="Birthday is over")
            except (discord.NotFound, discord.Forbidden) as error:
                # Asking again won't help. Anything else, such as Discord having a bad minute, goes back to the timer to retry.
                log.info("Could not take the birthday role from %s: %s", member.id, error)

    async def _set_birthday(self, guild_id: int, user_id: int, month: int, day: int) -> None:
        # The announced year stays on purpose, so changing a birthday can't earn a second announcement this year.
        await self.db.run(
            "INSERT INTO birthdays (guild_id, user_id, month, day, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET month = excluded.month, day = excluded.day, updated_at = excluded.updated_at",
            (guild_id, user_id, month, day, time.time()),
        )

    async def _upcoming(self, guild_id: int) -> list[dict]:
        rows = await self.db.all("SELECT user_id, month, day FROM birthdays WHERE guild_id = ? ORDER BY month, day", (guild_id,))
        moment = datetime.datetime.now(datetime.timezone.utc)
        today = (moment.month, moment.day)
        return [r for r in rows if (r["month"], r["day"]) >= today] + [r for r in rows if (r["month"], r["day"]) < today]

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.post("/greetings/test", self._test_endpoint)
        router.get("/greetings/problems", self._problems_endpoint)
        router.get("/greetings/joined", self._joined_endpoint)
        router.put("/greetings/joined", self._set_joined_endpoint)
        router.post("/greetings/convert", self._convert_endpoint)
        router.get("/greetings/birthdays", self._birthdays_endpoint)
        router.put(r"/greetings/birthdays/{user_id:\d+}", self._set_birthday_endpoint)
        router.delete(r"/greetings/birthdays/{user_id:\d+}", self._remove_birthday_endpoint)

    async def _test_values(self, req: Any, which: str, channel_id: int | None) -> tuple[Any, Scope]:
        """The member a test is about and what it shows. With the server at hand that's the person pressing the button, as a real welcome would see them.
        Without it, as in the demo, it's a stand-in built from what the dashboard knows. Only a greeting shows the joined count and the invite: a
        birthday's test reads them as the real announcement does, as written."""
        greets = which in GREETS
        guild = self.bot.get_guild(req.guild_id)
        member = guild.get_member(req.user.id) if guild is not None else None
        if member is not None:
            channel = guild.get_channel_or_thread(channel_id) if channel_id else None
            if greets:
                return member, await self.scope_for(member, channel, preview=True)
            return member, Scope(guild=guild, member=member, channel=channel)
        scope = preview_scope(req.ops, req.guild_id, req.user.id, channel_id=channel_id)
        if greets:
            stored = await self._stored_count(req.guild_id)
            joined = stored if stored is not None else (getattr(scope.guild, "member_count", None) or 0)
            scope.extra = self._values(joined, invitewatch.sample(scope.guild, scope.channel))
        return None, scope

    async def _test_endpoint(self, req: Any) -> dict:
        form = await req.json()
        which = form.get("which")
        if which not in KINDS:
            raise ValidationError({"which": "Pick a message to test."})
        payload = messages.required_payload(form.get("payload"), templates=True)
        channel_id = None if which == "dm" else req.channel(form.get("channel_id"), posting=True)
        member, scope = await self._test_values(req, which, channel_id)
        picture = await self._picture(member, which, scope) if member is not None and which in ("welcome", "dm") else None
        shown = _with_picture(await render_message(payload, scope, guild_id=req.guild_id), picture)
        files = [(picture["filename"], picture["data"])] if picture is not None else None
        if which == "dm":
            await req.ops.send_dm(req.user.id, shown, files=files)
            return {"sent_to": "dm"}
        # A test must not ping anybody, so it goes out silent even though the real message wouldn't.
        sent = await req.ops.send_message(channel_id, shown, silent=True, files=files)
        await req.record("greetings.test", detail=which)
        return {"sent_to": "channel", "url": sent["url"]}

    def _ping_problems(self, guild: Any) -> list[str]:
        """Each welcome, leave or ban message set to ping a role or everyone where Discord won't let the bot send that ping, said with the remedy."""
        cfg = self.cfg(guild.id)
        pinged = self.pinged_channels(cfg)
        found = []
        for kind in MENTIONED:
            channel_id = self._posts_in(cfg, kind)
            channel = guild.get_channel_or_thread(channel_id) if channel_id else None
            if channel is None or guild.me is None or not cfg[f"{kind}_on"]:
                continue
            if not any(name.startswith(f"{kind}:") for name in pinged):
                continue  # the member, and roles anyone can mention, take no permission
            try:
                if channel.permissions_for(guild.me).mention_everyone:
                    continue
            except Exception:  # a thread whose parent isn't known has no answer, and Discord gives the real one
                continue
            found.append(f"{_PINGED[kind][0]} pings a role or everyone, and the bot can't send that ping in #{channel.name}. Give it Mention Everyone there.")
        return found

    async def _problems_endpoint(self, req: Any) -> list[str]:
        found = [text for (guild_id, _), text in sorted(self._problems.items()) if guild_id == req.guild_id]
        guild = self.bot.get_guild(req.guild_id)
        if req.guild_id in self._invite_servers and guild is not None and guild.me is not None and not guild.me.guild_permissions.manage_guild:
            found.append("A greeting here shows the invite someone joined by, and the bot can't read this server's invites, so it reads Unknown. Give the bot Manage Server.")
        if guild is not None:
            found.extend(self._ping_problems(guild))
        return found

    async def _joined_endpoint(self, req: Any) -> dict:
        stored = await self._stored_count(req.guild_id)
        if stored is not None:
            return {"joined": stored, "counted": True}
        info = req.ops.describe_guild(req.guild_id) or {}
        return {"joined": info.get("member_count") or 0, "counted": False}

    async def _set_joined_endpoint(self, req: Any) -> dict:
        form = await req.json()
        try:
            number = integer(0, MAX_JOINED)(form.get("joined"))
        except Invalid as error:
            raise ValidationError({"joined": f"The joined count is a whole number from 0 to {MAX_JOINED:,}."}) from error
        await self._set_joined(req.guild_id, number)
        await req.record("greetings.joined", detail=str(number))
        return {"joined": number, "counted": True}

    async def _convert_endpoint(self, req: Any) -> dict:
        form = await req.json()
        text = form.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError({"text": "Paste the message's text, or its JSON, first."})
        if len(text) > CONVERT_MAX:
            raise ValidationError({"text": f"That's {len(text):,} characters. This takes up to {CONVERT_MAX:,}."})
        number_format = form.get("number_format", "plain")
        if not isinstance(number_format, str) or number_format not in doublebrace.LOCALES:
            raise ValidationError({"number_format": "Pick one of the listed number formats."})
        try:
            # In the person's share of template time, the way tag and trigger previews run: off the event loop, and a person who pastes again
            # and again soon waits for the next minute. One conversion runs at a time, since a paste can take a whole second, four times a
            # template's longest run: it never holds both of the bot's template slots, so greetings, tags and triggers always have one.
            async with self._converting:
                message, notes = await budgeted_render(lambda: _converted(text, number_format), req.guild_id, pool=budget.PREVIEWS, member_id=req.user.id)
        except OverBudget:
            raise ValidationError({"text": "Too much template work this minute. Try again in a minute."}) from None
        except Invalid as error:
            raise ValidationError({"text": str(error)}) from error
        except Exception:
            # Whatever a paste does to the converter, the person gets a sentence and the log gets the rest.
            log.exception("Converting a paste in %s failed", req.guild_id)
            raise ValidationError({"text": "This paste couldn't be converted. Paste the message's text, or its JSON, on its own."}) from None
        return {"message": message, "notes": notes}

    async def _birthdays_endpoint(self, req: Any) -> list[dict]:
        out = []
        for row in await self._upcoming(req.guild_id):
            member = req.ops.member(req.guild_id, row["user_id"])
            out.append({
                "user_id": row["user_id"], "month": row["month"], "day": row["day"], "date": birthday_text(row["month"], row["day"]),
                "name": member["display_name"] if member else None, "avatar": member["avatar"] if member else None,
            })
        return out

    async def _set_birthday_endpoint(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        form = await req.json()
        date = parse_birthday(form.get("birthday") if isinstance(form.get("birthday"), str) else "")
        if date is None:
            raise ValidationError({"birthday": "Write the date as day/month, such as 25/12."})
        if req.ops.member(req.guild_id, user_id) is None:
            raise ValidationError({"user_id": "That person isn't in this server."})
        await self._set_birthday(req.guild_id, user_id, *date)
        await req.record("birthday.set", detail=f"{user_id}: {birthday_text(*date)}")
        return {"user_id": user_id, "month": date[0], "day": date[1], "date": birthday_text(*date)}

    async def _remove_birthday_endpoint(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        if not await self.db.run("DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?", (req.guild_id, user_id)):
            raise ApiError(404, "No birthday is set for that person.")
        await req.record("birthday.remove", detail=str(user_id))
        return {"ok": True}

    # ---- /greetings ----

    def _ping_refusal(self, member: Any, form: dict) -> str | None:
        """The dashboard's check on a saved ping, for a command: a change that makes the welcome ping a role or everyone takes the right to send that ping in the welcome channel."""
        before = self.cfg(member.guild.id)
        try:
            after = self.schema.clean(form, base=before)
        except ValidationError:
            return None  # the save says what's wrong with it
        had = self.pinged_channels(before)
        for field, channels in self.pinged_channels(after).items():
            for channel_id in sorted(channels - had.get(field, set())):
                channel = member.guild.get_channel_or_thread(channel_id)
                refusal = access.ping_refusal(member, channel) if channel is not None else None
                if refusal:
                    return refusal
        return None

    async def _save(self, ctx: commands.Context, action: str, form: dict) -> bool:
        refusal = self._ping_refusal(ctx.author, form)
        if refusal:
            await ctx.fail(refusal)
            return False
        try:
            await self.bot.settings.save(ctx.guild.id, self.key, form, actor=self.bot.actor(ctx.author), action=action)
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values()), "That can't be saved."))
            return False
        return True

    @commands.hybrid_group(name="greetings", fallback="show", description="Show the welcome, farewell and ban setup")
    @commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @requires("manage_guild")
    async def greetings(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)

        def line(name: str, kind: str, channel_id: int | None, stored: dict) -> str:
            notes = [f"<#{channel_id}>" if channel_id else "no channel", "set" if messages.has_content(stored) else "no message"]
            if not cfg[f"{kind}_on"]:
                notes.append("switched off")
            if cfg[f"{kind}_delete_after"]:
                notes.append(f"deleted after {humanize(cfg[f'{kind}_delete_after'], parts=2)}")
            return f"**{name}:** {', '.join(notes)}"

        farewell = cfg["farewell_channel"] or cfg["welcome_channel"]
        dm = ["sends the welcome message" if cfg["dm_same_as_welcome"] else "set" if messages.has_content(cfg["dm"]) else "no message"]
        if not cfg["dm_on"]:
            dm.append("switched off")
        lines = [
            line("Welcome", "welcome", cfg["welcome_channel"], cfg["welcome"]),
            line("Farewell", "farewell", farewell, cfg["farewell"]),
            line("Ban", "ban", cfg["ban_channel"] or farewell, cfg["ban"]),
            f"**Join DM:** {', '.join(dm)}",
            PINGS[cfg["welcome_mentions"]],
            *(ping_sentence(kind, cfg[f"{kind}_mentions"]) for kind in ("farewell", "ban") if cfg[f"{kind}_mentions"] != "member"),
            f"**Joined count:** {await self._joined_count(ctx.guild):,}",
        ]
        if cfg["welcome_again_after"]:
            lines.append(f"Someone who rejoins within {humanize(cfg['welcome_again_after'] * 60, parts=2)} of a welcome gets no second one.")
        if not cfg["welcome_bots"]:
            lines.append(SWITCHES["welcome_bots"][2])
        if cfg["welcome_delete_on_leave"]:
            lines.append(SWITCHES["delete_welcome_on_leave"][1])
        lines.append(f"Full settings: {self.bot.public_url}/dashboard/{ctx.guild.id}/greetings")
        await ctx.send("\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _may_post_in(self, ctx: commands.Context, channel: Any) -> bool:
        """The same check the dashboard makes on a channel that will carry a person's text."""
        refusal = access.post_refusal(ctx.author, channel)
        if refusal:
            await ctx.fail(refusal)
            return False
        return True

    async def _set_text(self, ctx: commands.Context, field: str, action: str, text: str | None, set_note: str, clear_note: str) -> None:
        # With no text, the whole message goes, or an embed set on the dashboard would keep posting after it reads as cleared.
        stored = {**self.cfg(ctx.guild.id)[field], "content": text} if text else messages.empty_payload()
        if await self._save(ctx, action, {field: stored}):
            await ctx.ok(set_note if text else clear_note)

    @greetings.command(name="welcome_channel", description="Set the channel for welcome messages")
    @requires("manage_guild")
    async def greetings_welcome_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        if await self._may_post_in(ctx, channel) and await self._save(ctx, "greetings.welcome_channel", {"welcome_channel": channel.id}):
            await ctx.ok(f"Welcome messages now go to {channel.mention}.")

    @greetings.command(name="welcome", description="Set the message posted when someone joins. Leave it out to clear it.")
    @requires("manage_guild")
    async def greetings_welcome(self, ctx: commands.Context, *, message: str | None = None) -> None:
        await self._set_text(ctx, "welcome", "greetings.welcome", message, "The welcome message is set.", "The welcome message is cleared.")

    @greetings.command(name="joindm", description="Set the DM sent when someone joins. Leave it out to clear it.")
    @requires("manage_guild")
    async def greetings_joindm(self, ctx: commands.Context, *, message: str | None = None) -> None:
        await self._set_text(ctx, "dm", "greetings.joindm", message, "The join DM is set.", "The join DM is cleared.")

    @greetings.command(name="set_welcome", with_app_command=False, description="Set one channel for welcome, leave and ban messages, or clear all three")
    @requires("manage_guild")
    async def greetings_set_welcome(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        value = channel.id if channel else None
        if (channel is None or await self._may_post_in(ctx, channel)) and await self._save(ctx, "greetings.welcome_channel", {"welcome_channel": value, "farewell_channel": value, "ban_channel": value}):
            await ctx.ok(f"Welcome, leave and ban messages now go to {channel.mention}." if channel else "The welcome, leave and ban channels are cleared.")

    @greetings.command(name="set_farewell", with_app_command=False, description="Set one channel for leave and ban messages, or clear both")
    @requires("manage_guild")
    async def greetings_set_farewell(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        value = channel.id if channel else None
        if (channel is None or await self._may_post_in(ctx, channel)) and await self._save(ctx, "greetings.farewell_channel", {"farewell_channel": value, "ban_channel": value}):
            await ctx.ok(f"Leave and ban messages now go to {channel.mention}." if channel else "The leave and ban channels are cleared.")

    @greetings.command(name="farewell_channel", description="Set the channel for leave messages")
    @requires("manage_guild")
    async def greetings_farewell_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        if await self._may_post_in(ctx, channel) and await self._save(ctx, "greetings.farewell_channel", {"farewell_channel": channel.id}):
            await ctx.ok(f"Leave messages now go to {channel.mention}.")

    @greetings.command(name="farewell", description="Set the message posted when someone leaves. Leave it out to clear it.")
    @requires("manage_guild")
    async def greetings_farewell(self, ctx: commands.Context, *, message: str | None = None) -> None:
        await self._set_text(ctx, "farewell", "greetings.farewell", message, "The leave message is set.", "The leave message is cleared.")

    @greetings.command(name="ban_channel", description="Set the channel for ban messages")
    @requires("manage_guild")
    async def greetings_ban_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        if await self._may_post_in(ctx, channel) and await self._save(ctx, "greetings.ban_channel", {"ban_channel": channel.id}):
            await ctx.ok(f"Ban messages now go to {channel.mention}.")

    @greetings.command(name="banmessage", description="Set the message posted when someone is banned. Leave it out to clear it.")
    @requires("manage_guild")
    async def greetings_banmessage(self, ctx: commands.Context, *, message: str | None = None) -> None:
        await self._set_text(ctx, "ban", "greetings.banmessage", message, "The ban message is set.", "The ban message is cleared.")

    @greetings.command(name="switch", description="Switch a greeting, or one of the welcome's options, on or off")
    @requires("manage_guild")
    @app_commands.describe(what="The message or the option to switch", on="On or off")
    async def greetings_switch(self, ctx: commands.Context, what: Switch, on: bool) -> None:
        field, turned_on, turned_off = SWITCHES[what]
        # The Activity page reads the action's own words, so each switch says which way it went.
        if await self._save(ctx, f"greetings.{what}_{'on' if on else 'off'}", {field: on}):
            await ctx.ok(turned_on if on else turned_off)

    @greetings.command(name="delete_after", description="Delete each welcome, leave or ban message after a while. Leave the time out to keep them.")
    @requires("manage_guild")
    @app_commands.describe(message="Which message", duration="How long each post stays in whole minutes, such as 10m or 2 days, up to 30 days")
    async def greetings_delete_after(self, ctx: commands.Context, message: Literal["welcome", "farewell", "ban"], duration: Duration | None = None) -> None:
        seconds = duration or 0
        name = {"welcome": "Welcome", "farewell": "Leave", "ban": "Ban"}[message]
        if await self._save(ctx, f"greetings.{message}_delete_after", {f"{message}_delete_after": seconds}):
            await ctx.ok(f"{name} messages are deleted {humanize(seconds, parts=2)} after they go out. Posts already made follow." if seconds else f"{name} messages stay until someone deletes them.")

    @greetings.command(name="joined", description="Show how many people have joined so far, or set the count")
    @requires("manage_guild")
    @app_commands.describe(number="The count to carry on from. Leave it out to see the count.")
    async def greetings_joined(self, ctx: commands.Context, number: commands.Range[int, 0, MAX_JOINED] | None = None) -> None:
        if number is None:
            await ctx.send(f"{await self._joined_count(ctx.guild):,} people have joined so far. A greeting shows the count as `{{joined}}`.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        await self._set_joined(ctx.guild.id, number)
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "greetings.joined", detail=str(number))
        await ctx.ok(f"The joined count is {number:,}. The next person to join makes it {number + 1:,}.")

    @greetings.command(name="mentions", description="Choose who a welcome, leave or ban message pings: the member, roles in its text, or everyone too")
    @requires("manage_guild")
    @app_commands.describe(who="member: only the person it is about. roles: also roles in the text. all: also @everyone and @here.",
                           message="Which message. Left out, the welcome message.")
    async def greetings_mentions(self, ctx: commands.Context, who: Literal["member", "roles", "all"], message: Literal["welcome", "farewell", "ban"] = "welcome") -> None:
        # The message comes second, since Discord takes an option that can be left out only after the ones that can't.
        if await self._save(ctx, "greetings.mentions" if message == "welcome" else f"greetings.{message}_mentions", {f"{message}_mentions": who}):
            await ctx.ok(ping_sentence(message, who))

    @greetings.command(name="testgreet", description="Post the welcome, leave and ban messages here, and DM you the join DM")
    @requires("manage_guild")
    @app_commands.describe(member="Who the messages are about. Leave it out and they're about you.")
    async def greetings_testgreet(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        cfg = self.cfg(ctx.guild.id)
        about = member or ctx.author
        draws = self.bot.module(CARDS) is not None
        dm = cfg["welcome"] if cfg["dm_same_as_welcome"] else cfg["dm"]
        if not draws and not any(messages.has_content(stored) for stored in (cfg["welcome"], cfg["farewell"], cfg["ban"], dm)):
            await ctx.fail("No greeting messages are set yet.")
            return
        await ctx.defer()
        # A test shows what a join would, and counts nothing: the count as it stands, and a sample invite.
        scope = await self.scope_for(about, ctx.channel, preview=True)
        tried = 0
        for kind in POSTED:
            picture = await self._picture(about, "welcome", scope) if kind == "welcome" else None
            if picture is not None and not self._can_attach(ctx.guild, ctx.channel):
                picture = None
            payload = await render_message(cfg[kind], scope) if messages.has_content(cfg[kind]) else messages.empty_payload()
            if not messages.has_content(payload) and picture is None:
                continue
            # The test stays quiet, so trying a message that mentions people doesn't ping them.
            await ctx.send(**_outgoing(payload, picture, discord.AllowedMentions.none()))
            tried += 1
        wants_dm = messages.has_content(dm) or draws

        async def values() -> dict:
            return scope.extra

        dm_sent = wants_dm and await self._send_join_dm(about, dm, to=ctx.author, values=values, channel=ctx.channel if cfg["dm_same_as_welcome"] else None)
        tried += dm_sent
        if not tried and not messages.has_content(dm):
            await ctx.fail("No greeting messages are set yet.")
            return
        note = " Your DMs are closed, so the join DM didn't reach you." if messages.has_content(dm) and not dm_sent else ""
        await ctx.ok(f"Sent {tried} test message{'' if tried == 1 else 's'}.{note}")

    # ---- /birthday ----

    @commands.hybrid_group(name="birthday", fallback="view", description="See someone's birthday")
    @commands.guild_only()
    @app_commands.describe(member="Whose birthday. Defaults to yours.")
    async def birthday(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = member or ctx.author
        row = await self.db.one("SELECT month, day FROM birthdays WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, target.id))
        if row is None:
            await ctx.send(f"{target.mention} has no birthday set." if member else "You have no birthday set. Set it with `/birthday set`.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        await ctx.send(f"{target.mention}'s birthday is {birthday_text(row['month'], row['day'])}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _pick_person(self, ctx: commands.Context, member: Any) -> Any:
        """The person a birthday command acts on. Someone else's birthday takes Manage Server."""
        if member is None or member.id == ctx.author.id:
            return ctx.author
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.fail("You need Manage Server to change someone else's birthday.")
            return None
        return member

    @birthday.command(name="set", description="Set your birthday, as day/month")
    @app_commands.describe(birthday="Day then month, such as 25/12", member="Someone else, if you have Manage Server")
    async def birthday_set(self, ctx: commands.Context, birthday: str, member: discord.Member | None = None) -> None:
        target = await self._pick_person(ctx, member)
        if target is None:
            return
        date = parse_birthday(birthday)
        if date is None:
            await ctx.fail("Write the date as day/month, such as 25/12.")
            return
        await self._set_birthday(ctx.guild.id, target.id, *date)
        who = "Your birthday" if target.id == ctx.author.id else f"{target.mention}'s birthday"
        await ctx.ok(f"{who} is set to {birthday_text(*date)}.")

    @birthday.command(name="remove", description="Remove your birthday")
    @app_commands.describe(member="Someone else, if you have Manage Server")
    async def birthday_remove(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = await self._pick_person(ctx, member)
        if target is None:
            return
        removed = await self.db.run("DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, target.id))
        await ctx.ok("That birthday is removed." if removed else "No birthday was set.")

    @birthday.command(name="channel", description="Set where birthdays are announced. Leave it out to turn announcements off.")
    @requires("manage_guild")
    async def birthday_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        if (channel is None or await self._may_post_in(ctx, channel)) and await self._save(ctx, "birthday.channel", {"birthday_channel": channel.id if channel else None}):
            await ctx.ok(f"Birthdays are announced in {channel.mention}." if channel else "Birthday announcements are off.")

    @birthday.command(name="config", description="Show how birthdays are set up")
    async def birthday_config(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)
        channel = f"<#{cfg['birthday_channel']}>" if cfg["birthday_channel"] else "off"
        role = f"<@&{cfg['birthday_role']}>" if cfg["birthday_role"] else "none"
        await ctx.send(
            f"Announcements: {channel} at {cfg['birthday_hour']:02d}:00 UTC\nBirthday role: {role}\nCombine same-day birthdays: {'yes' if cfg['birthday_combine'] else 'no'}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @birthday.command(name="list", description="Show every birthday set in this server")
    async def birthday_list(self, ctx: commands.Context) -> None:
        rows = await self._upcoming(ctx.guild.id)
        if not rows:
            await ctx.send("No birthdays are set.", ephemeral=True)
            return
        lines = [f"{birthday_text(row['month'], row['day'])}: <@{row['user_id']}>" for row in rows]
        text = "\n".join(lines)
        if len(text) <= 1900:
            await ctx.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        plain = []
        for row in rows:
            member = ctx.guild.get_member(row["user_id"])
            plain.append(f"{birthday_text(row['month'], row['day'])}: {member.display_name if member else row['user_id']}")
        await ctx.send(f"{len(rows)} birthdays, soonest first.", file=discord.File(io.BytesIO("\n".join(plain).encode("utf-8")), "birthdays.txt"), ephemeral=True)
