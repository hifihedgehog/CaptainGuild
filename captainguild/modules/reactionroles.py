"""Roles members pick up from a message, by reaction, button or select menu.

Every event for a member goes through that member's own queue, so reaction spam can't scramble the order of role changes.
The queue remembers what it just changed until the gateway's copy of the member shows it, because that copy lags behind the bot's own changes.
Events still waiting merge, a queue has a limit, and each member takes one event per turn with servers in rotation, so spam costs one person's own pace and nobody else's.

A reaction stays on a message once it's been acted on, so a separate table records which ones have been. At startup the two
are compared: a reaction with no record arrived while the bot was down, and a record with no reaction left while it was down.
A verify or drop message takes each reaction off as it handles it, so any reaction still there hasn't been handled, unless it's on
record as being there before the message took reactions off. Nothing is marked done until the role change went through, which is
what makes a crash or a Discord outage lose nothing.

When something is in doubt, nobody's roles change, the reaction stays, and staff hear about it in the log channel.
"""
from __future__ import annotations

import asyncio
import collections
import copy
import json
import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, messages
from ..core.durations import humanize, parse_duration
from ..core.members import find_member
from ..core.module import Module, requires
from ..core.ops import MessageGone, OpsError
from ..core.perms import DANGEROUS_PERMISSIONS
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import role_is_safe
from ..core.validate import Invalid, ValidationError, hex_color, integer, snowflake, snowflakes, whole_number
from ..web.api import ApiError
from .roles import Confirm, RoleNames, TemporaryRoleBlocked, find_role, find_roles, missing_roles_sentence, send_pages

log = logging.getLogger(__name__)

TYPES = ("normal", "unique", "verify", "drop", "reversed", "limit", "binding", "temp", "lock")
MODES = ("reactions", "buttons", "select")
STYLES = ("primary", "secondary", "success", "danger")
# These types take the reaction off as soon as it's handled.
CLEARING = ("verify", "drop")
MAX_REACTIONS = 20  # Discord's error 30010
MAX_COMPONENTS = 25
MAX_ROWS = 5
BUTTONS_PER_ROW = 5
MAX_MENU_OPTIONS = 25
MOST_OPTIONS = MAX_ROWS * MAX_MENU_OPTIONS  # five menus of 25
MAX_LIMIT = 250
MIN_TEMP = 60
MAX_TEMP = 365 * 86400
MAX_DESTRUCT = 365 * 86400
# How long a queue remembers what it changed. The gateway has reported the change well before this runs out.
QUEUE_MEMORY = 10.0
# How many events run at once, and how many of those one server can hold. Discord's rate limits set the real pace. A server waiting out its own limit holds its turns the whole time, so it never holds them all.
PARALLEL_MEMBERS = 8
PARALLEL_PER_SERVER = 4
# Role changes share one rate limit per server, about one a second. A member can make this many at once, and then one per this many seconds, so one person can't spend a server's budget. Their events merge while they wait.
MEMBER_ROLE_BURST = 8
MEMBER_ROLE_GAP = 3.0
# How many events can wait for one member. Past this they're turned away, and the message is read back once the queue has run.
MAX_QUEUED = 50
RETRY_LIMIT = 8
# A deleted message's reaction roles stay this long, so `move` can still bring them back.
TRASH_SECONDS = 7 * 86400
# Reactions gone for more than this many people at startup means someone cleared the message, not that this many members changed their minds.
MASS_CLEAR = 25
# How long after the bot clears a message's reactions to put them in order it takes Discord's report of that clear as its own.
ORDER_GRACE = 60.0
OLD_ORDER = "The reactions stay in their old order."
REORDER_NEEDS = "Putting the reactions in order takes everyone's reactions off the message, and that takes Manage Messages in its channel."
# How long the same warning to staff stays quiet, so a busy gate can't flood the log channel.
STAFF_NOTICE_GAP = 3600.0
SETUP_TIMEOUT = 120.0
# A restore is still finishing when it tells modules, and catching up reads Discord for every message, so that waits this long.
RESTORE_SETTLE = 30.0
TRANSIENT = (discord.HTTPException, aiohttp.ClientError, OSError, asyncio.TimeoutError)
NONE = discord.AllowedMentions.none()
REASON = "Reaction role"
NEEDS_MANAGE_MESSAGES = "A message that deletes itself takes Manage Messages in that channel, since the bot deletes it for you."
# Text commands can run to several thousand characters, and a whole message's pairs fit well inside this.
MAX_PAIR_WORDS = 300

_CUSTOM_EMOJI = re.compile(r"<(a?):(\w{1,32}):(\d{15,25})>")
_NAME_ID_EMOJI = re.compile(r"(?:(a):)?:?(\w{1,32}):(\d{15,25})")
_BARE_ID = re.compile(r"\d{15,25}")
_MESSAGE_LINK = re.compile(r"https?://(?:\w+\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
_CHANNEL_MENTION = re.compile(r"<#(\d{15,25})>")
_SYMBOLS = set("\u00a9\u00ae\u203c\u2049\u2122\u2139\u3030\u303d\u3297\u3299")
VARIATION_SELECTOR = "\ufe0f"
KEYCAP = "\u20e3"


def looks_like_emoji(text: str) -> bool:
    """A custom emoji or something that could be a Unicode one. Discord has the final say when the bot reacts with it."""
    if _CUSTOM_EMOJI.fullmatch(text):
        return True
    if not text or len(text) > 16 or any(c.isspace() for c in text):
        return False
    if KEYCAP in text:
        return True
    if any(c.isascii() and c.isalnum() for c in text):
        return False
    return any(0x1F000 <= ord(c) <= 0x1FAFF or 0x2190 <= ord(c) <= 0x2BFF or c in _SYMBOLS or unicodedata.category(c) == "So" for c in text)


def clean_emoji(value: Any) -> str:
    """The form an emoji is stored in: <:name:id>, <a:name:id>, or the character itself."""
    if not isinstance(value, str) or not value.strip():
        raise Invalid("Pick an emoji.")
    text = value.strip()
    if _CUSTOM_EMOJI.fullmatch(text):
        return text
    match = _NAME_ID_EMOJI.fullmatch(text)
    if match:
        return f"<{match.group(1) or ''}:{match.group(2)}:{match.group(3)}>"
    if looks_like_emoji(text):
        return text
    raise Invalid(f"`{text[:40]}` isn't an emoji.")


def emoji_key(emoji: str) -> str:
    """What identifies an emoji in a Discord event: a custom emoji's ID, or the character with no variation selector, since clients differ on sending one."""
    match = _CUSTOM_EMOJI.fullmatch(emoji)
    if match:
        return match.group(3)
    return emoji.replace(VARIATION_SELECTOR, "")


def key_of(partial: discord.PartialEmoji) -> str:
    return str(partial.id) if partial.id else (partial.name or "").replace(VARIATION_SELECTOR, "")


def parse_message_ref(text: str) -> tuple[int | None, int | None, int] | None:
    """A message from an ID or a link, as (guild, channel, message). Links carry the first two."""
    text = text.strip()
    link = _MESSAGE_LINK.fullmatch(text)
    if link:
        return int(link.group(1)), int(link.group(2)), int(link.group(3))
    if re.fullmatch(r"\d{15,25}", text):
        return None, None, int(text)
    return None


def parse_pairs(guild: Any, text: str, names: RoleNames | None = None) -> tuple[list[tuple[str, Any]], str | None]:
    """Emoji and role pairs from free text, with the reason if some of it doesn't read. A role's name can hold spaces and even emoji."""
    spaced = _CUSTOM_EMOJI.sub(lambda m: f" {m.group(0)} ", text)
    spaced = re.sub(r"<@&\d+>", lambda m: f" {m.group(0)} ", spaced)
    words = spaced.split()
    if len(words) > MAX_PAIR_WORDS:
        return [], f"That's too many words to read as emoji and roles. Use up to {MAX_PAIR_WORDS}."
    names = names or RoleNames(guild)
    pairs: list[tuple[str, Any]] = []
    index = 0
    while index < len(words):
        word = words[index]
        if not looks_like_emoji(word):
            return pairs, f"`{word[:40]}` isn't an emoji. Write each pair as an emoji and then its role."
        found = None
        end = index + 1
        name: list[str] = []
        while end < min(len(words), index + 1 + names.longest):
            name.append(words[end])
            end += 1
            role = names.find(" ".join(name), loose=False)
            if role is not None:
                found = (role, end)
                if end >= len(words) or looks_like_emoji(words[end]):
                    break
        if found is None:
            shown = " ".join(words[index + 1:])[:60]
            return pairs, f"There's no role called `{shown}`." if shown else f"{word} needs a role after it."
        pairs.append((word, found[0]))
        index = found[1]
    return pairs, None


def split_text_and_pairs(guild: Any, text: str) -> tuple[str, list[tuple[str, Any]]]:
    """The all-in-one form: `title | description` and then the pairs, on one line or several. The title can be in double quotes, and it can hold emoji of its own."""
    stripped = text.strip()
    names = RoleNames(guild)
    if stripped.startswith('"'):
        end = stripped.find('"', 1)
        if end > 0:
            pairs, problem = parse_pairs(guild, stripped[end + 1:], names)
            if problem or not pairs:
                raise Invalid(problem or "Add at least one emoji and role after the text.")
            return stripped[1:end], pairs
    words = stripped.split()
    last_problem = None
    for index, word in enumerate(words):
        if looks_like_emoji(word):
            pairs, problem = parse_pairs(guild, " ".join(words[index:]), names)
            if pairs and not problem:
                return " ".join(words[:index]), pairs
            last_problem = problem
    raise Invalid(last_problem or "Add at least one emoji and role after the text.")


def title_payload(text: str, color: int | None) -> dict:
    """A message from `title | description`, the short form commands take."""
    title, _, description = text.replace("\\|", "|").partition("|")
    embed: dict[str, Any] = {"title": title.strip(), "description": description.strip()}
    if color is not None:
        embed["color"] = color
    payload = messages.clean_payload({"content": "", "embeds": [embed]})
    if not messages.has_content(payload):
        raise Invalid("Write a title or a description, such as `Games | Pick your games`.")
    return payload


ROLES_BLOCK = "{roles}"


def holds_roles_block(payload: dict) -> bool:
    if ROLES_BLOCK in (payload.get("content") or ""):
        return True
    for embed in payload.get("embeds") or []:
        if ROLES_BLOCK in (embed.get("title") or "") or ROLES_BLOCK in (embed.get("description") or ""):
            return True
        if any(ROLES_BLOCK in (row.get("value") or "") for row in embed.get("fields") or []):
            return True
    return False


@dataclass
class Option:
    """One emoji row on a message: the roles it hands out, and how it looks as a button or menu entry."""
    slot: int
    emoji: str
    key: str
    role_ids: list[int] = field(default_factory=list)
    limits: dict[int, int] = field(default_factory=dict)
    label: str = ""
    description: str = ""
    style: str = "secondary"


@dataclass
class RR:
    message_id: int
    guild_id: int
    channel_id: int
    kind: str = "normal"
    mode: str = "reactions"
    limit: int = 0
    temp_seconds: int = 0
    whitelist: list[int] = field(default_factory=list)
    blacklist: list[int] = field(default_factory=list)
    group_id: int | None = None
    destruct_at: float | None = None
    foreign: bool = False
    placeholder: str = ""
    created_at: float = 0.0
    options: list[Option] = field(default_factory=list)
    template: dict | None = None  # the message as written, when it holds {roles}
    cleared_at: float | None = None  # when the bot last saw every reaction on it cleared

    def by_key(self, key: str | None) -> Option | None:
        return next((o for o in self.options if o.key == key), None)

    def by_slot(self, slot: int) -> Option | None:
        return next((o for o in self.options if o.slot == slot), None)

    @property
    def role_ids(self) -> set[int]:
        return {role_id for option in self.options for role_id in option.role_ids}

    def copied(self) -> "RR":
        return copy.deepcopy(self)

    def url(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"


def fill_roles_block(payload: dict, rr: RR, names: dict[int, str]) -> dict:
    """The message with {roles} replaced by each emoji and its roles. A title can't show mentions, so it gets their names."""
    lines = "\n".join(f"{option.emoji} {' '.join(f'<@&{role_id}>' for role_id in option.role_ids)}".strip() for option in rr.options)
    plain = ", ".join(f"{option.emoji} {' '.join(names.get(role_id, 'Role') for role_id in option.role_ids)}".strip() for option in rr.options)
    filled = copy.deepcopy(payload)
    if filled.get("content"):
        filled["content"] = filled["content"].replace(ROLES_BLOCK, lines)
    for embed in filled.get("embeds") or []:
        if embed.get("title"):
            embed["title"] = embed["title"].replace(ROLES_BLOCK, plain)[:256]
        if embed.get("description"):
            embed["description"] = embed["description"].replace(ROLES_BLOCK, lines)
        for row in embed.get("fields") or []:
            if row.get("value"):
                row["value"] = row["value"].replace(ROLES_BLOCK, lines)
    return messages.clean_payload(filled)


@dataclass
class Outcome:
    """What one event did. `retry` means Discord failed in a way that may pass, so the event stays unfinished. `blocked` means the bot's own setup stopped it, so the member did nothing wrong and the reaction stays."""
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    refused: str | None = None
    note: str | None = None
    retry: bool = False
    blocked: bool = False


@dataclass
class Work:
    action: str  # added, removed or click
    message_id: int
    key: str | None = None
    slots: list[int] | None = None
    member: Any = None
    done: list[asyncio.Future] = field(default_factory=list)
    tries: int = 0
    added_first: bool = False  # a reaction went on before this event, and the two merged
    at: float = field(default_factory=time.time)  # when the event happened. A retry keeps it, so a clear after it is known to be later.


class _Queue:
    __slots__ = ("items", "task", "changes", "idle_since", "turned_away")

    def __init__(self) -> None:
        self.items: collections.deque[Work] = collections.deque()
        self.task: asyncio.Task | None = None
        self.changes: dict[int, bool] = {}
        self.idle_since = 0.0
        self.turned_away: set[int] = set()


class Turns:
    """Who runs next. Servers take turns in rotation and none holds more than its share, so one server's rush or rate limit never reaches another. Inside a server, whoever asked first goes first, and a member asks again for each event, so a member with many events goes behind everyone waiting."""

    def __init__(self, limit: int, per_server: int) -> None:
        self.limit = limit
        self.per_server = per_server
        self.total = 0
        self.running: dict[int, int] = {}
        self.waiting: dict[int, collections.deque[asyncio.Future]] = {}  # the order of the keys is the rotation

    def _start(self, guild_id: int) -> None:
        self.total += 1
        self.running[guild_id] = self.running.get(guild_id, 0) + 1

    async def acquire(self, guild_id: int) -> None:
        if self.total < self.limit and self.running.get(guild_id, 0) < self.per_server and guild_id not in self.waiting:
            self._start(guild_id)
            return
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.waiting.setdefault(guild_id, collections.deque()).append(future)
        try:
            await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                self.release(guild_id)  # the turn came as this was canceled, so the next one gets it
            else:
                queue = self.waiting.get(guild_id)
                if queue is not None and future in queue:
                    queue.remove(future)
                    if not queue:
                        del self.waiting[guild_id]
            raise

    def release(self, guild_id: int) -> None:
        self.total -= 1
        left = self.running.get(guild_id, 0) - 1
        if left > 0:
            self.running[guild_id] = left
        else:
            self.running.pop(guild_id, None)
        self._hand_out()

    def _hand_out(self) -> None:
        while self.total < self.limit:
            guild_id = next((g for g in self.waiting if self.running.get(g, 0) < self.per_server), None)
            if guild_id is None:
                return
            queue = self.waiting.pop(guild_id)
            future = queue.popleft()
            if queue:
                self.waiting[guild_id] = queue  # back to the end of the rotation
            if not future.done():
                self._start(guild_id)
                future.set_result(None)


def _build(row: dict, pairs: list[dict]) -> RR:
    rr = RR(
        message_id=row["message_id"], guild_id=row["guild_id"], channel_id=row["channel_id"], kind=row["kind"], mode=row["mode"],
        limit=row["limit_count"], temp_seconds=row["temp_seconds"], whitelist=json.loads(row["whitelist"]), blacklist=json.loads(row["blacklist"]),
        group_id=row["group_id"], destruct_at=row["destruct_at"], foreign=bool(row["foreign_author"]), placeholder=row["placeholder"], created_at=row["created_at"],
        template=json.loads(row["template"]) if row["template"] else None, cleared_at=row["cleared_at"],
    )
    options: dict[int, Option] = {}
    for pair in pairs:
        option = options.get(pair["slot"])
        if option is None:
            option = options[pair["slot"]] = Option(pair["slot"], pair["emoji"], pair["emoji_key"], label=pair["label"], description=pair["description"], style=pair["style"])
        option.role_ids.append(pair["role_id"])
        if pair["max_holders"]:
            option.limits[pair["role_id"]] = pair["max_holders"]
    rr.options = [options[slot] for slot in sorted(options)]
    return rr


def rr_json(rr: RR) -> dict:
    return {
        "message_id": rr.message_id, "channel_id": rr.channel_id, "type": rr.kind, "mode": rr.mode, "limit": rr.limit, "temp_seconds": rr.temp_seconds,
        "whitelist": rr.whitelist, "blacklist": rr.blacklist, "group_id": rr.group_id, "destruct_at": rr.destruct_at, "mine": not rr.foreign,
        "placeholder": rr.placeholder, "url": rr.url(),
        "rows": [{"emoji": o.emoji, "roles": o.role_ids, "max": {str(k): v for k, v in o.limits.items()}, "label": o.label, "description": o.description, "style": o.style} for o in rr.options],
    }


def clean_settings(form: dict, info: dict, *, foreign: bool, link_buttons: int) -> dict:
    """Check everything a person can set on a message, against the server it's for. Raises ValidationError naming each field."""
    errors: dict[str, str] = {}
    roles = {r["id"]: r for r in info["roles"]}
    kind = form.get("type", "normal")
    if kind not in TYPES:
        errors["type"] = "Pick one of the listed types."
    mode = form.get("mode", "reactions")
    if mode not in MODES:
        errors["mode"] = "Pick one of the listed modes."
        mode = "reactions"
    if foreign and mode != "reactions":
        errors["mode"] = "Another bot wrote this message, and Discord only lets a bot add buttons and menus to its own. Use reactions, or repost the message first."
    limit = temp_seconds = 0
    if kind == "limit":
        try:
            limit = integer(1, MAX_LIMIT)(form.get("limit"))
        except Invalid:
            errors["limit"] = f"Pick a limit from 1 to {MAX_LIMIT}."
    if kind == "temp":
        try:
            temp_seconds = integer(MIN_TEMP, MAX_TEMP)(form.get("temp_seconds"))
        except Invalid:
            errors["temp_seconds"] = "Pick a time from one minute to one year."

    def role_list(name: str) -> list[int]:
        try:
            ids = snowflakes()(form.get(name) or [])
        except Invalid as error:
            errors[name] = str(error)
            return []
        if any(i not in roles for i in ids):
            errors[name] = "One of these roles no longer exists. Remove it."
        return ids

    whitelist = role_list("whitelist")
    blacklist = role_list("blacklist")
    placeholder = form.get("placeholder") or ""
    if not isinstance(placeholder, str) or len(placeholder) > 150:
        errors["placeholder"] = "The menu's prompt can be at most 150 characters."
        placeholder = ""
    rows = form.get("rows")
    options: list[Option] = []
    if not isinstance(rows, list) or not rows:
        errors["rows"] = "Add at least one emoji and role."
        rows = []
    used: set[str] = set()
    for index, row in enumerate(rows):
        name = f"rows.{index}"
        if not isinstance(row, dict):
            errors[name] = "Each row needs an emoji and roles."
            continue
        emoji = ""
        if row.get("emoji") or mode == "reactions":
            try:
                emoji = clean_emoji(row.get("emoji"))
            except Invalid as error:
                errors[f"{name}.emoji"] = str(error)
        key = emoji_key(emoji) if emoji else f"row{index}"
        if key in used:
            errors[f"{name}.emoji"] = "That emoji is on another row. Put several roles on one row instead."
        used.add(key)
        try:
            role_ids = snowflakes(25)(row.get("roles") or [])
        except Invalid as error:
            errors[f"{name}.roles"] = str(error)
            continue
        if not role_ids:
            errors[f"{name}.roles"] = "Pick at least one role."
        for role_id in role_ids:
            role = roles.get(role_id)
            if role is None:
                errors[f"{name}.roles"] = "One of these roles no longer exists. Remove it."
            elif role["default"] or role["managed"]:
                errors[f"{name}.roles"] = f"Nobody can hand out {role['name']}."
        limits: dict[int, int] = {}
        caps = row.get("max") or {}
        if isinstance(caps, dict):
            for role_id in role_ids:
                raw = caps.get(str(role_id), caps.get(role_id))
                if raw in (None, "", 0):
                    continue
                try:
                    limits[role_id] = integer(1, 10_000_000)(raw)
                except Invalid:
                    errors[f"{name}.max"] = "A role's maximum must be a whole number above zero."
        label = row.get("label") or ""
        description = row.get("description") or ""
        style = row.get("style") or "secondary"
        # A button label holds 80 characters and a menu option's holds 100, which is what render() cuts them to.
        label_max = 100 if mode == "select" else 80
        if not isinstance(label, str) or len(label) > label_max:
            errors[f"{name}.label"] = f"A label can be at most {label_max} characters."
            label = ""
        if not isinstance(description, str) or len(description) > 100:
            errors[f"{name}.description"] = "A description can be at most 100 characters."
            description = ""
        if style not in STYLES:
            errors[f"{name}.style"] = "Pick one of the listed button colors."
            style = "secondary"
        options.append(Option(index, emoji, key, role_ids, limits, label, description, style))
    capacity = capacity_problem(mode, len(rows), link_buttons)
    if capacity:
        errors["rows"] = capacity
    if errors:
        raise ValidationError(errors)
    return {"kind": kind, "mode": mode, "limit": limit, "temp_seconds": temp_seconds, "whitelist": whitelist, "blacklist": blacklist, "placeholder": placeholder.strip(), "options": options}


def opens_up(rr: RR, whitelist: list[int], blacklist: list[int], kind: str) -> str | None:
    """Which part of a change lets more people pick up this message's roles, if any. Emptying a whitelist or adding to one lets more in, and taking a role off one lets fewer."""
    if set(rr.blacklist) - set(blacklist):
        return "blacklist"
    if rr.whitelist and (not whitelist or set(whitelist) - set(rr.whitelist)):
        return "whitelist"
    if rr.kind in ("lock", "drop") and kind not in ("lock", "drop"):
        return "type"
    return None


def capacity_problem(mode: str, rows: int, link_buttons: int) -> str | None:
    if mode == "reactions" and rows > MAX_REACTIONS:
        return f"Discord allows {MAX_REACTIONS} different reactions on a message. This has {rows}. Buttons and menus can hold more."
    if mode == "buttons" and rows + link_buttons > MAX_COMPONENTS:
        return f"A message can hold {MAX_COMPONENTS} buttons in all. This has {rows} role buttons and {link_buttons} link buttons."
    if mode == "select":
        menus = math.ceil(rows / MAX_MENU_OPTIONS)
        if menus + math.ceil(link_buttons / BUTTONS_PER_ROW) > MAX_ROWS:
            return f"That many options and link buttons don't fit on one message. A menu holds {MAX_MENU_OPTIONS} options and each takes a whole row of the {MAX_ROWS} a message has."
    return None


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rr:b:(?P<slot>\d{1,4})"):
    """A role button. The message it's on says which reaction roles it belongs to, so the ID only names the row."""

    def __init__(self, slot: int, item: discord.ui.Button | None = None) -> None:
        super().__init__(item or discord.ui.Button(custom_id=f"rr:b:{slot}"))
        self.slot = slot

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Any, match: re.Match, /) -> "RoleButton":
        return cls(int(match["slot"]), item)

    async def callback(self, interaction: discord.Interaction) -> None:
        module = interaction.client.module("reactionroles")
        if module is not None:
            await module.clicked(interaction, [self.slot])


class RoleMenu(discord.ui.DynamicItem[discord.ui.Select], template=r"rr:s:(?P<index>\d{1,2})"):
    def __init__(self, index: int, item: discord.ui.Select | None = None) -> None:
        super().__init__(item or discord.ui.Select(custom_id=f"rr:s:{index}", options=[discord.SelectOption(label="-")]))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Any, match: re.Match, /) -> "RoleMenu":
        return cls(int(match["index"]), item)

    async def callback(self, interaction: discord.Interaction) -> None:
        module = interaction.client.module("reactionroles")
        if module is not None:
            slots = [int(value) for value in self.item.values if value.isdigit()]
            await module.clicked(interaction, slots, view=self.view)


TypeChoice = Literal["normal", "unique", "verify", "drop", "reversed", "limit", "binding", "temp", "lock"]
ListChoice = Literal["add", "remove", "clear", "show"]


class ReactionRoles(Module):
    key = "reactionroles"
    category = "Roles"
    role_timers = ("rr_temp", "rr_retry")
    child_links = {"rr_pairs": {"message_id": "rr_messages.message_id"}}
    migrations = [
        """
        CREATE TABLE rr_messages (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'normal',
            mode TEXT NOT NULL DEFAULT 'reactions',
            limit_count INTEGER NOT NULL DEFAULT 0,
            temp_seconds INTEGER NOT NULL DEFAULT 0,
            whitelist TEXT NOT NULL DEFAULT '[]',
            blacklist TEXT NOT NULL DEFAULT '[]',
            group_id INTEGER,
            destruct_at REAL,
            foreign_author INTEGER NOT NULL DEFAULT 0,
            placeholder TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            deleted_at REAL
        );
        CREATE INDEX rr_messages_guild ON rr_messages (guild_id, channel_id);
        CREATE INDEX rr_messages_group ON rr_messages (guild_id, group_id);
        CREATE TABLE rr_pairs (
            guild_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            slot INTEGER NOT NULL,
            emoji TEXT NOT NULL,
            emoji_key TEXT NOT NULL,
            role_id INTEGER NOT NULL,
            max_holders INTEGER NOT NULL DEFAULT 0,
            label TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            style TEXT NOT NULL DEFAULT 'secondary',
            PRIMARY KEY (message_id, emoji_key, role_id)
        );
        CREATE INDEX rr_pairs_role ON rr_pairs (guild_id, role_id);
        CREATE TABLE rr_seen (
            message_id INTEGER NOT NULL,
            emoji_key TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (message_id, emoji_key, user_id)
        ) WITHOUT ROWID;
        CREATE INDEX rr_seen_user ON rr_seen (user_id, message_id);
        CREATE TABLE rr_synced (message_id INTEGER PRIMARY KEY, synced_at REAL NOT NULL DEFAULT 0);
        """,
        "ALTER TABLE rr_messages ADD COLUMN template TEXT;",
        "ALTER TABLE rr_messages ADD COLUMN cleared_at REAL;",
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._queues: dict[tuple[int, int], _Queue] = {}
        self._turns = Turns(PARALLEL_MEMBERS, PARALLEL_PER_SERVER)
        # Each member's pace: the moment their role changes so far would have used up at one per MEMBER_ROLE_GAP. It outlasts their queue.
        self._paced: dict[tuple[int, int], float] = {}
        # One lock per role with a maximum, held across the count and the change. Grants the gateway's copy doesn't show yet count too.
        self._cap_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._cap_grants: dict[tuple[int, int], dict[int, float]] = {}
        # Reactions the bot is taking off itself. Discord reports those like any other removal, and they must not cost anyone a role.
        self._own_removals: dict[tuple[int, str, int], float] = {}
        self._background: set[asyncio.Task] = set()
        self._reconciling: asyncio.Task | None = None
        self._caught_up = False
        self._told: dict[tuple[int, str], float] = {}
        # Servers whose reactions went unread during an outage. They get a full read when they come back.
        self._unread: set[int] = set()
        # Live events for a reaction whose list is being read, as user and whether it's on now. A read can take minutes, and it trusts these over the list for anyone who acted meanwhile.
        self._reading: dict[tuple[int, str], list[dict[int, bool]]] = {}
        self._ready_again = False
        # Messages whose reactions the bot cleared to put them in order, and until when Discord's report of that clear counts as the bot's own.
        self._reordering: dict[int, float] = {}
        # How many times each message's reactions were cleared while the bot ran. A read of who reacted that spans a clear can't be trusted.
        self._clears: dict[int, int] = {}

    async def on_load(self) -> None:
        self.bot.timers.handler("rr_temp", self._temp_due)
        self.bot.timers.handler("rr_destruct", self._destruct_due)
        self.bot.timers.handler("rr_retry", self._retry_due)
        self.bot.timers.handler("rr_purge", self._purge_due)
        self.bot.timers.handler("rr_sync", self._sync_due)
        self.bot.add_dynamic_items(RoleButton, RoleMenu)

    async def cog_unload(self) -> None:
        tasks = [q.task for q in self._queues.values() if q.task] + list(self._background)
        if self._reconciling:
            tasks.append(self._reconciling)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _tell_staff(self, guild_id: int, topic: str, text: str) -> None:
        """Something only a person can sort out, which members can't see. It goes to the log channel, once an hour per topic."""
        now = time.monotonic()
        last = self._told.get((guild_id, topic))
        if last is not None and now - last < STAFF_NOTICE_GAP:
            return
        if len(self._told) > 1000:
            self._told.clear()
        self._told[(guild_id, topic)] = now
        log.warning("Reaction roles in %s: %s", guild_id, text)
        logs = self.bot.module("logging")
        guild = self.bot.get_guild(guild_id)
        if logs is not None and guild is not None:
            logs.staff_notice(guild, "Reaction roles need a look", text)

    def _sync_soon(self, message_id: int, *, thorough: bool = False, tries: int = 0) -> asyncio.Task:
        async def guarded() -> None:
            try:
                await self.reconcile_message(message_id, thorough=thorough, tries=tries)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Catching up message %s failed", message_id)

        task = asyncio.create_task(guarded(), name=f"rr-sync-{message_id}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _sync_due(self, payload: dict, row: dict) -> None:
        self._sync_soon(payload["message_id"], thorough=payload["thorough"], tries=payload["tries"])

    async def _look_again(self, rr: RR, problem: str, thorough: bool, tries: int) -> None:
        """A read failed, so nothing changed and whatever was waiting still waits. It tries again later, and staff hear about it if it goes on."""
        tries += 1
        if tries >= 2:
            self._tell_staff(rr.guild_id, f"read:{rr.message_id}", f"{problem} Anyone who reacted there while the bot was away is still waiting, and nobody's roles changed.")
        if tries > RETRY_LIMIT:
            log.warning("Giving up on reading message %s after %d tries. The next start looks at it again.", rr.message_id, tries)
            return
        await self.bot.timers.schedule(
            "rr_sync", time.time() + min(3600, 60 * 2 ** (tries - 1)), {"message_id": rr.message_id, "thorough": thorough, "tries": tries},
            guild_id=rr.guild_id, key=str(rr.message_id),
        )

    async def _reconcile_guild(self, guild_id: int, *, thorough: bool) -> None:
        for rr in sorted(await self.for_guild(guild_id), key=lambda rr: rr.kind != "verify"):
            try:
                await self.reconcile_message(rr.message_id, thorough=thorough)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Catching up message %s failed", rr.message_id)

    async def _mark_restored(self, guild_id: int) -> None:
        await self.db.run("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (f"rr_restored:{guild_id}", str(time.time())))

    async def before_restore(self, guild_id: int) -> None:
        await self._mark_restored(guild_id)  # a catch-up starting while the rows change takes a baseline, and changes nobody's roles

    async def on_guild_restored(self, guild_id: int) -> None:
        """A backup is an older picture of who had reacted. Reactions on Discord are current, so they go back on record without changing anyone's roles."""
        await self.db.run("DELETE FROM rr_seen WHERE message_id NOT IN (SELECT message_id FROM rr_messages)")
        await self.db.run("DELETE FROM rr_synced WHERE message_id NOT IN (SELECT message_id FROM rr_messages)")
        await self._mark_restored(guild_id)

        async def later() -> None:
            await asyncio.sleep(RESTORE_SETTLE)
            await self._reconcile_guild(guild_id, thorough=False)

        task = asyncio.create_task(later(), name=f"rr-restored-{guild_id}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _mark_synced(self, message_id: int) -> None:
        await self.db.run("INSERT INTO rr_synced (message_id, synced_at) VALUES (?, ?) ON CONFLICT (message_id) DO UPDATE SET synced_at = excluded.synced_at", (message_id, time.time()))

    async def _needs_baseline(self, rr: RR) -> bool:
        """True for a message with reactions from before it had reaction roles, or one whose record came back from a backup."""
        synced_at = await self.db.scalar("SELECT synced_at FROM rr_synced WHERE message_id = ?", (rr.message_id,))
        if synced_at is None:
            return True
        restored = await self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"rr_restored:{rr.guild_id}",))
        return restored is not None and synced_at < float(restored)

    async def settle(self) -> None:
        """Wait until nothing is queued or running. A task that has just finished stays listed until its callback runs, and gather() over
        finished tasks returns without letting that callback run, so each round yields first and waits only on unfinished tasks."""
        while True:
            await asyncio.sleep(0)
            tasks = [q.task for q in self._queues.values() if q.task and not q.task.done()] + [t for t in self._background if not t.done()]
            if not tasks:
                return
            await asyncio.wait(tasks)

    # ---- storage ----

    async def get(self, message_id: int, *, trashed: bool = False) -> RR | None:
        extra = "" if trashed else " AND deleted_at IS NULL"
        row = await self.db.one(f"SELECT * FROM rr_messages WHERE message_id = ?{extra}", (message_id,))
        if row is None:
            return None
        return _build(row, await self.db.all("SELECT * FROM rr_pairs WHERE message_id = ? AND guild_id = ? ORDER BY slot, role_id", (message_id, row["guild_id"])))

    async def for_guild(self, guild_id: int) -> list[RR]:
        rows = await self.db.all("SELECT * FROM rr_messages WHERE guild_id = ? AND deleted_at IS NULL ORDER BY message_id", (guild_id,))
        pairs: dict[int, list[dict]] = {}
        for pair in await self.db.all("SELECT * FROM rr_pairs WHERE guild_id = ? ORDER BY slot, role_id", (guild_id,)):
            pairs.setdefault(pair["message_id"], []).append(pair)
        return [_build(row, pairs.get(row["message_id"], [])) for row in rows]

    async def holds_message(self, guild_id: int, message_id: int) -> bool:
        return await self.db.scalar("SELECT 1 FROM rr_messages WHERE message_id = ? AND guild_id = ? AND deleted_at IS NULL", (message_id, guild_id)) is not None

    async def messages_using_role(self, guild_id: int, role_id: int) -> list[int]:
        rows = await self.db.all(
            "SELECT DISTINCT p.message_id FROM rr_pairs p JOIN rr_messages m ON m.message_id = p.message_id WHERE p.guild_id = ? AND p.role_id = ? AND m.deleted_at IS NULL",
            (guild_id, role_id),
        )
        return [row["message_id"] for row in rows]

    async def _write(self, rr: RR) -> None:
        async with self.db.transaction():
            await self.db.run(
                "INSERT INTO rr_messages (message_id, guild_id, channel_id, kind, mode, limit_count, temp_seconds, whitelist, blacklist, group_id, destruct_at, foreign_author, placeholder, created_at, template) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (message_id) DO UPDATE SET guild_id = excluded.guild_id, channel_id = excluded.channel_id, kind = excluded.kind, mode = excluded.mode, limit_count = excluded.limit_count, "
                "temp_seconds = excluded.temp_seconds, whitelist = excluded.whitelist, blacklist = excluded.blacklist, group_id = excluded.group_id, destruct_at = excluded.destruct_at, "
                "foreign_author = excluded.foreign_author, placeholder = excluded.placeholder, template = excluded.template, deleted_at = NULL",
                (rr.message_id, rr.guild_id, rr.channel_id, rr.kind, rr.mode, rr.limit, rr.temp_seconds, json.dumps(rr.whitelist), json.dumps(rr.blacklist), rr.group_id, rr.destruct_at,
                 int(rr.foreign), rr.placeholder, rr.created_at or time.time(), json.dumps(rr.template) if rr.template else None),
            )
            # Whoever sets this up has shown the message is in their server, so a row a backup file left for it under another server gives way.
            await self.db.run("DELETE FROM rr_pairs WHERE message_id = ? AND guild_id != ?", (rr.message_id, rr.guild_id))
            rows = []
            for option in rr.options:
                for role_id in option.role_ids:
                    rows.append((rr.guild_id, rr.message_id, option.slot, option.emoji, option.key, role_id, option.limits.get(role_id, 0), option.label, option.description, option.style))
            # Reactions are read on this same connection while this runs, so the new rows go in before the old ones leave. A reader in between never sees a message with no options.
            old = {(row["emoji_key"], row["role_id"]) for row in await self.db.all("SELECT emoji_key, role_id FROM rr_pairs WHERE message_id = ? AND guild_id = ?", (rr.message_id, rr.guild_id))}
            if rows:
                await self.db.many(
                    "INSERT INTO rr_pairs (guild_id, message_id, slot, emoji, emoji_key, role_id, max_holders, label, description, style) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (message_id, emoji_key, role_id) DO UPDATE SET slot = excluded.slot, emoji = excluded.emoji, max_holders = excluded.max_holders, label = excluded.label, description = excluded.description, style = excluded.style",
                    rows,
                )
            stale = old - {(row[4], row[5]) for row in rows}
            if stale:
                await self.db.many("DELETE FROM rr_pairs WHERE message_id = ? AND guild_id = ? AND emoji_key = ? AND role_id = ?", [(rr.message_id, rr.guild_id, key, role_id) for key, role_id in stale])

    async def _erase(self, message_id: int) -> None:
        """Remove every trace of a message's reaction roles."""
        row = await self.db.one("SELECT guild_id, group_id FROM rr_messages WHERE message_id = ?", (message_id,))

        def erase(connection: Any) -> None:
            for table in ("rr_messages", "rr_pairs", "rr_seen", "rr_synced"):
                connection.execute(f"DELETE FROM {table} WHERE message_id = ?", (message_id,))

        await self.db.exclusive(erase)
        await self.bot.timers.cancel_key("rr_destruct", str(message_id))
        await self.bot.timers.cancel_key("rr_purge", str(message_id))
        if row and row["group_id"] is not None:
            await self._tidy_group(row["guild_id"], row["group_id"])

    async def _trash(self, message_id: int) -> None:
        """The message was deleted. Its reaction roles stop working now and go for good in a week, so `move` can still rescue them."""
        row = await self.db.one("SELECT guild_id FROM rr_messages WHERE message_id = ? AND deleted_at IS NULL", (message_id,))
        if row is None:
            return
        now = time.time()

        def trash(connection: Any) -> None:
            connection.execute("UPDATE rr_messages SET deleted_at = ? WHERE message_id = ?", (now, message_id))
            connection.execute("DELETE FROM rr_seen WHERE message_id = ?", (message_id,))
            connection.execute("DELETE FROM rr_synced WHERE message_id = ?", (message_id,))

        await self.db.exclusive(trash)
        await self.bot.timers.cancel_key("rr_destruct", str(message_id))
        await self.bot.timers.schedule("rr_purge", now + TRASH_SECONDS, {"message_id": message_id}, guild_id=row["guild_id"], key=str(message_id))

    async def _tidy_group(self, guild_id: int, group_id: int) -> None:
        count = await self.db.scalar("SELECT COUNT(*) FROM rr_messages WHERE guild_id = ? AND group_id = ? AND deleted_at IS NULL", (guild_id, group_id))
        if count is not None and count < 2:
            await self.db.run("UPDATE rr_messages SET group_id = NULL WHERE guild_id = ? AND group_id = ?", (guild_id, group_id))

    async def _purge_due(self, payload: dict, row: dict) -> None:
        still = await self.db.scalar("SELECT 1 FROM rr_messages WHERE message_id = ? AND deleted_at IS NOT NULL", (payload["message_id"],))
        if still:
            await self._erase(payload["message_id"])

    # ---- keeping the message itself in step ----

    def _role_names(self, guild_id: int) -> dict[int, str]:
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            return {role.id: role.name for role in guild.roles}
        info = self.bot.ops.describe_guild(guild_id) or {}
        return {row["id"]: row["name"] for row in info.get("roles", [])}

    def render(self, rr: RR) -> list[dict]:
        """The buttons or menus this message carries, in the form messages.component_view reads."""
        if rr.mode == "reactions" or not rr.options:
            return []
        names = self._role_names(rr.guild_id)

        def label(option: Option, width: int) -> str:
            return (option.label or ", ".join(names.get(role_id, "Role") for role_id in option.role_ids))[:width] or "Role"

        if rr.mode == "buttons":
            return [{"type": "button", "custom_id": f"rr:b:{o.slot}", "label": label(o, 80), "emoji": o.emoji or None, "style": o.style} for o in rr.options]
        menus = []
        for index in range(0, len(rr.options), MAX_MENU_OPTIONS):
            chunk = rr.options[index:index + MAX_MENU_OPTIONS]
            if rr.kind in ("unique", "binding") or rr.group_id is not None:
                most = 1
            elif rr.kind == "limit":
                most = max(1, min(rr.limit, len(chunk)))
            else:
                most = len(chunk)
            menus.append({
                "type": "select", "custom_id": f"rr:s:{index // MAX_MENU_OPTIONS}", "placeholder": rr.placeholder or "Pick a role", "min": 1, "max": most,
                "options": [{"label": label(o, 100), "value": str(o.slot), "description": o.description or None, "emoji": o.emoji or None} for o in chunk],
            })
        return menus

    async def components_for(self, message_id: int) -> list[dict]:
        """What any edit to this message has to carry over."""
        rr = await self.get(message_id)
        if rr is None or rr.foreign:
            return []
        return self.render(rr)

    async def _save(self, rr: RR, previous: RR | None = None, *, payload: dict | None = None, fresh: bool = False, reorder: bool = False) -> list[str]:
        """Store a message's reaction roles and make the message match. Returns anything the person should know that didn't stop the save.

        The edit comes first, so a refused edit leaves the stored setup as it was. Reactions come after, one emoji failing doesn't undo the rest.
        With `reorder`, a reaction message's reactions are cleared and the bot's go back in the order of its rows.
        """
        ops = self.bot.ops
        controls = self.render(rr)
        if rr.template and not rr.foreign and not fresh and payload is None and (previous is None or [(o.emoji, o.role_ids) for o in previous.options] != [(o.emoji, o.role_ids) for o in rr.options]):
            payload = self._filled(rr, rr.template)  # the rows changed, so the list the message shows changes with them
        if not rr.foreign and not fresh:
            changed = previous is None or self.render(previous) != controls
            if payload is not None:
                await ops.edit_message(rr.channel_id, rr.message_id, payload, components=controls)
            elif changed and (controls or (previous is not None and previous.mode != "reactions")):
                current = await ops.fetch_message(rr.channel_id, rr.message_id)
                await ops.edit_message(rr.channel_id, rr.message_id, current["payload"], components=controls)
        await self._write(rr)
        await self._set_destruct_timer(rr)
        if fresh:
            await self._mark_synced(rr.message_id)
        await self._end_old_temp_timers(rr, previous)
        warnings: list[str] = []
        had = {o.key: o for o in previous.options} if previous is not None and previous.mode == "reactions" else {}
        if rr.mode == "reactions":
            if reorder:
                try:
                    warnings.extend(await self._put_in_order(rr))
                except OpsError as error:
                    warnings.append(f"{OLD_ORDER} {error}")
                    reorder = False
            if not reorder:
                now = {o.key: o for o in rr.options}
                for key, option in had.items():
                    if key not in now:
                        await self._clear_emoji(rr, option)
                        await self.db.run("DELETE FROM rr_seen WHERE message_id = ? AND emoji_key = ?", (rr.message_id, key))
                if previous is not None and previous.kind not in CLEARING and rr.kind in CLEARING:
                    # Reactions left from the old type would all look unhandled at the next start.
                    for key, option in now.items():
                        if key in had:
                            await self._clear_emoji(rr, option)
                    await self.db.run("DELETE FROM rr_seen WHERE message_id = ?", (rr.message_id,))
                    had = {}
                added = [o for o in rr.options if o.key not in had]
                for option in added:
                    problem = await self._react(rr, option)
                    if problem:
                        warnings.append(problem)
                if added and not fresh:
                    await self.db.run("DELETE FROM rr_synced WHERE message_id = ?", (rr.message_id,))
                    self._sync_soon(rr.message_id)
        elif previous is not None and previous.mode == "reactions":
            try:
                await ops.clear_reactions(rr.channel_id, rr.message_id)
            except OpsError:
                for option in previous.options:
                    await self._clear_emoji(rr, option)
            await self.db.run("DELETE FROM rr_seen WHERE message_id = ?", (rr.message_id,))
        return warnings

    def _filled(self, rr: RR, payload: dict) -> dict:
        return fill_roles_block(payload, rr, self._role_names(rr.guild_id)) if holds_roles_block(payload) else payload

    def _take_template(self, rr: RR, payload: dict | None) -> dict | None:
        """Note what a person wrote, and return what the message should say now. A message with {roles} keeps its wording here, so the list can follow its rows."""
        if payload is None:
            return None
        rr.template = payload if holds_roles_block(payload) else None
        return self._filled(rr, payload)

    async def _end_old_temp_timers(self, rr: RR, previous: RR | None) -> None:
        """A role that stopped being temporary here stops counting down, or someone who kept it on purpose loses it later. It keeps counting down if another temp message hands it out."""
        if previous is None or previous.kind != "temp":
            return
        ending = previous.role_ids - (rr.role_ids if rr.kind == "temp" else set())
        for role_id in ending:
            still = await self.db.scalar(
                "SELECT 1 FROM rr_pairs p JOIN rr_messages m ON m.message_id = p.message_id WHERE p.guild_id = ? AND p.role_id = ? AND m.kind = 'temp' AND m.deleted_at IS NULL AND m.message_id != ? LIMIT 1",
                (rr.guild_id, role_id, rr.message_id),
            )
            if not still:
                await self.db.run("DELETE FROM timers WHERE kind = 'rr_temp' AND guild_id = ? AND key LIKE ?", (rr.guild_id, f"{rr.guild_id}:%:{role_id}"))

    async def _mark_cleared(self, message_id: int) -> None:
        """Every reaction on the message is gone. A reaction event from before now must not go on record afterward, and a read of who reacted that spans this can't be trusted."""
        self._clears[message_id] = self._clears.get(message_id, 0) + 1
        await self.db.run("UPDATE rr_messages SET cleared_at = ? WHERE message_id = ?", (time.time(), message_id))

    async def _put_in_order(self, rr: RR) -> list[str]:
        """Discord lists a message's reactions in the order each emoji first went on, so the only way to change the order is to take every reaction off
        and add the bot's back in the order of the rows. Members keep their roles. Their reactions go, and so does the record of who reacted, so
        dropping a role afterward takes reacting and then taking the reaction off. Raises OpsError, with nothing changed, if the clear is refused."""
        now = time.monotonic()
        self._reordering = {message_id: until for message_id, until in self._reordering.items() if until > now}
        self._reordering[rr.message_id] = now + ORDER_GRACE
        try:
            await self.bot.ops.clear_reactions(rr.channel_id, rr.message_id)
        except OpsError:
            self._reordering.pop(rr.message_id, None)
            raise
        await self._mark_cleared(rr.message_id)
        await self.db.run("DELETE FROM rr_seen WHERE message_id = ?", (rr.message_id,))
        warnings: list[str] = []
        for option in rr.options:
            problem = await self._react(rr, option)
            if problem:
                warnings.append(problem)
        return warnings

    @staticmethod
    def unusable_refusal(unusable: list[str]) -> str:
        one = len(unusable) == 1
        return (f"The bot can't react with {' '.join(unusable)}, because it isn't in the server {'that emoji comes' if one else 'those emoji come'} from. "
                f"Clearing the reactions would leave {'it' if one else 'them'} off for good, so swap {'it' if one else 'them'} for {'an emoji' if one else 'emoji'} the bot can use first.")

    def unusable_emoji(self, rr: RR) -> list[str]:
        """The emoji on a message the bot can't react with: a custom emoji from a server it isn't in, or one Discord has switched off.
        Clearing reactions takes those off for good, since nothing can put them back in their place."""
        found = []
        for option in rr.options:
            if option.key.isdigit():
                emoji = self.bot.get_emoji(int(option.key))
                if emoji is None or not emoji.available:
                    found.append(option.emoji)
        return found

    async def _react(self, rr: RR, option: Option) -> str | None:
        try:
            await self.bot.ops.add_reaction(rr.channel_id, rr.message_id, option.emoji)
        except OpsError as error:
            return (f"The bot couldn't react with {option.emoji}: {error} The emoji still works for anyone who adds it by hand. "
                    "If it's from another server, react with it once yourself and run `/reactionrole fixforeign`.")
        return None

    async def _clear_emoji(self, rr: RR, option: Option) -> None:
        if not option.emoji:
            return
        try:
            await self.bot.ops.clear_reaction(rr.channel_id, rr.message_id, option.emoji)
        except OpsError as error:
            log.info("Couldn't clear %s from %s: %s", option.emoji, rr.message_id, error)

    async def _strip_message(self, rr: RR) -> None:
        """Take this feature's reactions or controls off a message, as far as Discord allows."""
        try:
            if rr.mode == "reactions":
                for option in rr.options:
                    await self._clear_emoji(rr, option)
            elif not rr.foreign:
                current = await self.bot.ops.fetch_message(rr.channel_id, rr.message_id)
                await self.bot.ops.edit_message(rr.channel_id, rr.message_id, current["payload"], components=[])
        except OpsError as error:
            log.info("Couldn't tidy message %s: %s", rr.message_id, error)

    async def _set_destruct_timer(self, rr: RR) -> None:
        key = str(rr.message_id)
        if rr.destruct_at is None:
            await self.bot.timers.cancel_key("rr_destruct", key)
        else:
            await self.bot.timers.schedule("rr_destruct", rr.destruct_at, {"message_id": rr.message_id}, guild_id=rr.guild_id, key=key)

    async def _destruct_due(self, payload: dict, row: dict) -> None:
        rr = await self.get(payload["message_id"])
        if rr is None:
            return
        try:
            await self.bot.ops.delete_message(rr.channel_id, rr.message_id)
        except OpsError as error:
            log.warning("Self-destruct couldn't delete message %s: %s", rr.message_id, error)
        await self._erase(rr.message_id)

    async def remove(self, rr: RR) -> None:
        """A person took the reaction roles off this message. The message stays. The record goes first, or the clear's own event would put the reactions back."""
        await self._erase(rr.message_id)
        await self._strip_message(rr)

    async def redraw(self, rr: RR) -> list[str]:
        """Put back reactions someone cleared, or buttons and menus that went missing."""
        warnings: list[str] = []
        current = await self.bot.ops.fetch_message(rr.channel_id, rr.message_id)
        if rr.mode == "reactions":
            present = {emoji_key(r["emoji"]) for r in current["reactions"] if r.get("me")}
            for option in rr.options:
                if option.key not in present:
                    problem = await self._react(rr, option)
                    if problem:
                        warnings.append(problem)
        elif current["mine"]:
            await self.bot.ops.edit_message(rr.channel_id, rr.message_id, current["payload"], components=self.render(rr))
        return warnings

    async def move_message(self, guild_id: int, old_message_id: int, new_channel_id: int, new_message_id: int) -> dict | None:
        """Carry reaction roles to another message, such as a repost. Returns None when the old message holds none.

        Raises Invalid or OpsError with a sentence for a person. A message deleted in the last week still counts as holding them.
        """
        rr = await self.get(old_message_id, trashed=True)
        if rr is None or rr.guild_id != guild_id:
            return None
        if old_message_id == new_message_id:
            return {"message_id": new_message_id, "warnings": []}
        if await self.get(new_message_id) is not None:
            raise Invalid("That message already has reaction roles. Remove them first, or pick another message.")
        target = await self.bot.ops.fetch_message(new_channel_id, new_message_id)
        if not target["mine"] and rr.mode != "reactions":
            raise Invalid("Another bot wrote that message, and Discord only lets a bot add buttons and menus to its own. Switch this one to reactions first.")
        problem = capacity_problem(rr.mode, len(rr.options), len(target["payload"].get("buttons") or []))
        if problem:
            raise Invalid(problem)
        old = rr.copied()
        rr.message_id = new_message_id
        rr.channel_id = new_channel_id
        rr.foreign = not target["mine"]
        if rr.destruct_at is not None and rr.destruct_at <= time.time():
            rr.destruct_at = None  # a rescued message's time ran out while it was gone, and the new one shouldn't pay for that
        had_reactions = bool(target["reactions"])
        if rr.mode != "reactions":
            # The edit comes first, so one Discord refuses leaves the stored setup where it was.
            await self.bot.ops.edit_message(new_channel_id, new_message_id, target["payload"], components=self.render(rr))

        def carry(connection: Any) -> None:
            connection.execute("UPDATE rr_messages SET message_id = ?, channel_id = ?, foreign_author = ?, destruct_at = ?, deleted_at = NULL WHERE message_id = ?", (new_message_id, new_channel_id, int(rr.foreign), rr.destruct_at, old_message_id))
            connection.execute("UPDATE rr_pairs SET message_id = ? WHERE message_id = ? AND guild_id = ?", (new_message_id, old_message_id, rr.guild_id))
            connection.execute("DELETE FROM rr_seen WHERE message_id = ?", (old_message_id,))
            connection.execute("DELETE FROM rr_synced WHERE message_id = ?", (old_message_id,))

        await self.db.exclusive(carry)
        await self.bot.timers.cancel_key("rr_destruct", str(old_message_id))
        await self.bot.timers.cancel_key("rr_purge", str(old_message_id))
        await self._set_destruct_timer(rr)
        warnings: list[str] = []
        if rr.mode == "reactions":
            for option in rr.options:
                problem = await self._react(rr, option)
                if problem:
                    warnings.append(problem)
            if had_reactions:
                self._sync_soon(new_message_id)
            else:
                await self._mark_synced(new_message_id)
        await self._strip_message(old)
        return {"message_id": new_message_id, "warnings": warnings}

    # ---- the queue ----

    def _enqueue(self, guild_id: int, user_id: int, work: Work) -> None:
        key = (guild_id, user_id)
        queue = self._queues.get(key)
        if queue is None:
            queue = self._queues[key] = _Queue()
        if not self._merge(queue, work):
            if len(queue.items) < MAX_QUEUED:
                queue.items.append(work)
            else:
                self._turn_away(queue, work)
        if queue.task is None:
            queue.task = asyncio.create_task(self._drain(key, queue), name=f"rr-queue-{user_id}")

    def _merge(self, queue: _Queue, work: Work) -> bool:
        """Join an event to one still waiting, when only the later one matters. Returns True when nothing more is needed.

        For a reaction, what it is now is all that counts, so the new event replaces the older one and goes to the back, which keeps the order of each emoji's last event. Pressing a button again before the first press has run is the same press, so both get its answer.
        """
        for index, waiting in enumerate(queue.items):
            if waiting.message_id != work.message_id or (waiting.action == "click") != (work.action == "click"):
                continue
            if work.action == "click":
                if waiting.slots == work.slots:
                    waiting.done.extend(work.done)
                    return True
            elif waiting.key == work.key:
                work.added_first = work.added_first or waiting.added_first or waiting.action == "added"
                work.member = work.member or waiting.member
                del queue.items[index]
                return False
        return False

    def _turn_away(self, queue: _Queue, work: Work) -> None:
        """Past the limit. A reaction turned away isn't lost: it isn't on record, so reading the message back finds it."""
        if work.action != "click":
            queue.turned_away.add(work.message_id)
        for future in work.done:
            if not future.done():
                future.set_result(Outcome(note="That's a lot at once. Wait a moment and try again."))

    def _pace_wait(self, key: tuple[int, int]) -> float:
        used_up_at = self._paced.get(key)
        if used_up_at is None:
            return 0.0
        return used_up_at - (MEMBER_ROLE_BURST - 1) * MEMBER_ROLE_GAP - time.monotonic()

    def _spend(self, guild_id: int, user_id: int, calls: int) -> None:
        """Note what a change cost. discord.py makes one call per role."""
        now = time.monotonic()
        if len(self._paced) > 10000:
            self._paced = {key: moment for key, moment in self._paced.items() if moment > now}
        key = (guild_id, user_id)
        self._paced[key] = max(self._paced.get(key, now), now) + calls * MEMBER_ROLE_GAP

    async def _drain(self, key: tuple[int, int], queue: _Queue) -> None:
        """One event per turn. Pacing comes before the turn, so someone waiting out their pace holds nothing, and an event leaves the queue only when it runs, so anything newer can still merge with it."""
        stopping = False
        try:
            while queue.items:
                while (wait := self._pace_wait(key)) > 0:
                    await asyncio.sleep(wait)
                await self._turns.acquire(key[0])
                try:
                    if queue.items:
                        await self._run_work(key, queue, queue.items.popleft())
                finally:
                    self._turns.release(key[0])
        except asyncio.CancelledError:
            stopping = True
            raise
        finally:
            queue.task = None
            queue.idle_since = time.monotonic()
            if queue.items and not stopping:
                queue.task = asyncio.create_task(self._drain(key, queue), name=f"rr-queue-{key[1]}")
            else:
                asyncio.get_running_loop().call_later(QUEUE_MEMORY, self._retire, key, queue)
                if not stopping:
                    for message_id in queue.turned_away:
                        self._sync_soon(message_id)
                    queue.turned_away.clear()

    def _retire(self, key: tuple[int, int], queue: _Queue) -> None:
        if self._queues.get(key) is queue and queue.task is None and not queue.items and time.monotonic() - queue.idle_since >= QUEUE_MEMORY - 0.01:
            del self._queues[key]

    async def _run_work(self, key: tuple[int, int], queue: _Queue, work: Work) -> None:
        outcome: Outcome | None = None
        try:
            outcome = await self._handle(key[0], key[1], queue, work)
        except asyncio.CancelledError:
            raise
        except TRANSIENT as error:
            log.warning("Reaction role change for %s on %s failed: %s", key[1], work.message_id, error)
            outcome = Outcome(retry=True)
        except Exception:
            log.exception("Reaction role change for %s on %s failed", key[1], work.message_id)
            outcome = Outcome(refused="Something went wrong. It's been logged.")
        if outcome is not None and outcome.retry and work.action != "click":
            await self._retry_later(key[0], key[1], work)
        for future in work.done:
            if not future.done():
                future.set_result(outcome)

    async def _retry_later(self, guild_id: int, user_id: int, work: Work) -> None:
        tries = work.tries + 1
        if tries > RETRY_LIMIT:
            log.warning("Giving up on a reaction role change for %s on %s after %d tries. The next start looks at it again.", user_id, work.message_id, tries)
            return
        await self.bot.timers.schedule(
            "rr_retry", time.time() + min(3600, 60 * 2 ** (tries - 1)),
            {"guild_id": guild_id, "user_id": user_id, "message_id": work.message_id, "key": work.key, "action": work.action, "tries": tries, "at": work.at},
            guild_id=guild_id, key=f"{work.message_id}:{user_id}:{work.key}:{work.action}",
        )

    async def _retry_due(self, payload: dict, row: dict) -> None:
        # A retry stored before events carried their time counts as older than any clear, so it gives the role and leaves no record.
        self._enqueue(payload["guild_id"], payload["user_id"], Work(payload["action"], payload["message_id"], key=payload["key"], tries=payload["tries"], at=payload.get("at", 0.0)))

    def _held(self, queue: _Queue, member: Any) -> set[int]:
        """What the member holds: the gateway's copy, corrected for changes it hasn't shown yet. A change it does show is forgotten, so a moderator's later change counts."""
        held = {role.id for role in member.roles}
        for role_id, present in list(queue.changes.items()):
            if (role_id in held) == present:
                del queue.changes[role_id]
            elif present:
                held.add(role_id)
            else:
                held.discard(role_id)
        return held

    # ---- gateway events ----

    def _is_me(self, user_id: int) -> bool:
        me = self.bot.user
        return me is not None and me.id == user_id

    # These two listeners queue the event before their first await, so events can't overtake each other on the way in.
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or self._is_me(payload.user_id):
            return
        key = key_of(payload.emoji)
        # A reaction going on again means the removal this token guarded is over. Discord sends nothing when the bot removes a reaction that was already gone, and a token left waiting would swallow this member's next real removal.
        self._own_removals.pop((payload.message_id, key, payload.user_id), None)
        self._note_live(payload.message_id, key, payload.user_id, True)
        self._enqueue(payload.guild_id, payload.user_id, Work("added", payload.message_id, key=key, member=payload.member))

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or self._is_me(payload.user_id):
            return
        key = key_of(payload.emoji)
        if self._own_removals.pop((payload.message_id, key, payload.user_id), None) is not None:
            return
        self._note_live(payload.message_id, key, payload.user_id, False)
        self._enqueue(payload.guild_id, payload.user_id, Work("removed", payload.message_id, key=key))

    def _note_live(self, message_id: int, key: str, user_id: int, present: bool) -> None:
        for acted in self._reading.get((message_id, key), ()):
            acted[user_id] = present

    @commands.Cog.listener()
    async def on_member_update(self, before: Any, after: Any) -> None:
        queue = self._queues.get((after.guild.id, after.id))
        if queue is not None and queue.changes:
            self._held(queue, after)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        until = self._reordering.pop(payload.message_id, None)
        if until is not None and until > time.monotonic():
            return  # the bot cleared them itself to put them in order, and it puts its own back
        rr = await self.get(payload.message_id)
        if rr is None:
            return
        await self._mark_cleared(rr.message_id)
        await self.db.run("DELETE FROM rr_seen WHERE message_id = ?", (rr.message_id,))
        if rr.mode == "reactions":
            for option in rr.options:
                problem = await self._react(rr, option)
                if problem:
                    log.info("After a clear on %s: %s", rr.message_id, problem)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        rr = await self.get(payload.message_id)
        if rr is None:
            return
        option = rr.by_key(key_of(payload.emoji))
        if option is None:
            return
        await self._mark_cleared(rr.message_id)  # one emoji, but a missed record only means a later catch-up looks again
        await self.db.run("DELETE FROM rr_seen WHERE message_id = ? AND emoji_key = ?", (rr.message_id, option.key))
        if rr.mode == "reactions":
            problem = await self._react(rr, option)
            if problem:
                log.info("After a clear on %s: %s", rr.message_id, problem)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self._trash(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        ids = list(payload.message_ids)
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        for row in await self.db.all(f"SELECT message_id FROM rr_messages WHERE message_id IN ({marks}) AND deleted_at IS NULL", ids):
            await self._trash(row["message_id"])

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        await self._channel_gone(channel.guild.id, channel.id)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        await self._channel_gone(payload.guild_id, payload.thread_id)

    async def _channel_gone(self, guild_id: int, channel_id: int) -> None:
        for row in await self.db.all("SELECT message_id FROM rr_messages WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)):
            await self._erase(row["message_id"])

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: Any) -> None:
        for message_id in await self.messages_using_role(role.guild.id, role.id):
            await self._drop_roles(message_id, {role.id})

    async def _drop_roles(self, message_id: int, role_ids: set[int]) -> None:
        """Take roles that no longer exist off a message. A message with no rows left stops being a reaction role message."""
        rr = await self.get(message_id)
        if rr is None:
            return
        previous = rr.copied()
        for option in rr.options:
            option.role_ids = [r for r in option.role_ids if r not in role_ids]
            for role_id in role_ids:
                option.limits.pop(role_id, None)
        rr.options = [o for o in rr.options if o.role_ids]
        try:
            if rr.options:
                await self._save(rr, previous)
            else:
                await self.remove(previous)
        except OpsError as error:
            log.warning("Couldn't update message %s after a role was deleted: %s", message_id, error)
            await self._write(rr)

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: Any, before: Any, after: Any) -> None:
        gone = {str(emoji.id) for emoji in before} - {str(emoji.id) for emoji in after}
        if not gone:
            return
        marks = ",".join("?" * len(gone))
        rows = await self.db.all(f"SELECT DISTINCT p.message_id, p.emoji FROM rr_pairs p JOIN rr_messages m ON m.message_id = p.message_id WHERE p.guild_id = ? AND p.emoji_key IN ({marks}) AND m.deleted_at IS NULL", (guild.id, *gone))
        for row in rows:
            rr = await self.get(row["message_id"])
            if rr is not None:
                self._tell_staff(guild.id, f"emoji:{rr.message_id}:{row['emoji']}", f"The emoji {row['emoji']} was deleted from this server, and {rr.url()} still uses it. Nobody can pick that row until you change it on the Reaction Roles page.")

    @commands.Cog.listener()
    async def on_guild_unavailable(self, guild: discord.Guild) -> None:
        self._unread.add(guild.id)

    @commands.Cog.listener()
    async def on_guild_available(self, guild: discord.Guild) -> None:
        # Reactions that came and went during an outage sent no events, and a ready during one couldn't read anything.
        if guild.id not in self._unread:
            return
        self._unread.discard(guild.id)
        task = asyncio.create_task(self._reconcile_guild(guild.id, thorough=True), name=f"rr-back-{guild.id}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # The first ready after a start reads every reaction, because one person adding and another removing the same emoji would leave its count unchanged.
        # A later ready only follows a short gateway gap, so there the count is enough.
        if self._reconciling is None or self._reconciling.done():
            self._reconciling = asyncio.create_task(self._reconcile_passes(thorough=not self._caught_up), name="rr-reconcile")
            self._caught_up = True
        else:
            self._ready_again = True  # the pass running now may already have read the messages this gap touched

    async def _reconcile_passes(self, *, thorough: bool) -> None:
        await self.reconcile_all(thorough=thorough)
        while self._ready_again:
            self._ready_again = False
            await self.reconcile_all(thorough=False)

    # ---- what each event does ----

    async def _handle(self, guild_id: int, user_id: int, queue: _Queue, work: Work) -> Outcome | None:
        rr = await self.get(work.message_id)
        if rr is None or rr.guild_id != guild_id:
            return None
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return Outcome(retry=True)
        option = None
        if work.action != "click":
            option = rr.by_key(work.key)
            if option is None or rr.mode != "reactions":
                return None  # someone reacted with an emoji this message doesn't use
            if work.added_first and rr.kind in CLEARING:
                work.action = "added"  # on these types a reaction asks once, however soon it comes off again
            # The member changed their mind, which makes a waiting retry of the opposite change out of date.
            opposite = "removed" if work.action == "added" else "added"
            await self.bot.timers.cancel_key("rr_retry", f"{work.message_id}:{user_id}:{work.key}:{opposite}")
        # The cached member, not the copy that came with the event. That copy is from before anything ahead of it in this queue ran, while the cache and queue.changes together cover every change so far.
        member = guild.get_member(user_id) or work.member
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                await self._member_gone(rr, option, user_id, work)
                return None
        if member.id == guild.me.id:
            return None
        if work.action == "click":
            return await self._clicked(queue, guild, member, rr, work.slots or [])
        if member.bot:
            # Another bot's reaction hands out nothing, but it counts toward the reaction's total, so it goes on record.
            if rr.kind not in CLEARING:
                await self._set_seen(rr.message_id, option.key, user_id, work.action == "added")
            return None
        if work.action == "added":
            return await self._reaction_added(queue, guild, member, rr, option, record=rr.cleared_at is None or work.at > rr.cleared_at)
        return await self._reaction_removed(queue, guild, member, rr, option)

    async def _member_gone(self, rr: RR, option: Option | None, user_id: int, work: Work) -> None:
        if option is None:
            return
        if rr.kind in CLEARING and work.action == "added":
            await self._take_reaction(rr, option, user_id)
        elif rr.kind not in CLEARING:
            await self._set_seen(rr.message_id, option.key, user_id, work.action == "added")

    async def _set_seen(self, message_id: int, key: str, user_id: int, present: bool) -> None:
        if present:
            await self.db.run("INSERT OR IGNORE INTO rr_seen (message_id, emoji_key, user_id) VALUES (?, ?, ?)", (message_id, key, user_id))
        else:
            await self.db.run("DELETE FROM rr_seen WHERE message_id = ? AND emoji_key = ? AND user_id = ?", (message_id, key, user_id))

    async def _reaction_added(self, queue: _Queue, guild: Any, member: Any, rr: RR, option: Option, *, record: bool = True) -> Outcome:
        if rr.kind == "lock":
            outcome = Outcome(refused="This message is locked.")
        elif rr.kind in ("drop", "reversed"):
            outcome = await self._put_down(queue, guild, member, rr, option)
        else:
            outcome = await self._pick_up(queue, guild, member, rr, option)
        if outcome.retry or outcome.blocked:
            return outcome  # still unhandled, so the next catch-up gets to it
        if outcome.refused or rr.kind in CLEARING:
            await self._take_reaction(rr, option, member.id)
        elif record:
            await self._set_seen(rr.message_id, option.key, member.id, True)
        return outcome

    async def _reaction_removed(self, queue: _Queue, guild: Any, member: Any, rr: RR, option: Option) -> Outcome:
        if rr.kind in ("normal", "unique", "limit", "temp"):
            outcome = await self._put_down(queue, guild, member, rr, option)
        elif rr.kind == "reversed":
            outcome = await self._pick_up(queue, guild, member, rr, option)
        else:
            outcome = Outcome()
        if not (outcome.retry or outcome.blocked):
            await self._set_seen(rr.message_id, option.key, member.id, False)  # still on record otherwise, so the next catch-up finds this person under who left
        return outcome

    async def _clicked(self, queue: _Queue, guild: Any, member: Any, rr: RR, slots: list[int]) -> Outcome:
        total = Outcome()
        notes = []
        for slot in slots:
            option = rr.by_slot(slot)
            if option is None:
                notes.append("That choice is out of date. Ask a moderator to run `/reactionrole fix`.")
                continue
            outcome = await self._click_one(queue, guild, member, rr, option)
            total.added.extend(outcome.added)
            total.removed.extend(outcome.removed)
            total.retry = total.retry or outcome.retry
            if outcome.refused or outcome.note:
                notes.append(outcome.refused or outcome.note)
        total.note = " ".join(dict.fromkeys(notes)) or None
        return total

    async def _click_one(self, queue: _Queue, guild: Any, member: Any, rr: RR, option: Option) -> Outcome:
        if rr.kind == "lock":
            return Outcome(refused="This message is locked.")
        held = self._held(queue, member)
        existing = [role_id for role_id in option.role_ids if guild.get_role(role_id) is not None]
        if rr.kind == "drop":
            if any(role_id in held for role_id in existing):
                return await self._put_down(queue, guild, member, rr, option)
            return Outcome(note="You don't have this, and nobody can pick it up here.")
        has_it = bool(existing) and all(role_id in held for role_id in existing)
        if has_it:
            if rr.kind == "verify":
                return Outcome(note="You already have this.")
            if rr.kind == "binding":
                return Outcome(refused="Your choice on this message is final.")
            return await self._put_down(queue, guild, member, rr, option)
        return await self._pick_up(queue, guild, member, rr, option)

    def _usable(self, guild: Any, option: Option) -> tuple[list, str | None]:
        """The option's roles the bot may hand out. A role can sit too high, or hold too much power for the server's settings."""
        settings = self.bot.settings.get(guild.id, COMMANDS_MODULE)
        usable = []
        problem = None
        for role_id in option.role_ids:
            role = guild.get_role(role_id)
            if role is None or role.is_default() or role.managed or role >= guild.me.top_role:
                problem = "The bot can't hand out that role right now. Ask a moderator to check it."
            elif not role_is_safe([name for name, on in role.permissions if on], settings):
                problem = "That role holds permissions the bot won't hand out until the server allows it."
            else:
                usable.append(role)
        return usable, problem

    async def _rivals(self, rr: RR, option: Option) -> list[tuple[RR, Option]]:
        """Options this pick competes with: the rest of a unique or binding message, and everything in a linked group."""
        rivals: list[tuple[RR, Option]] = []
        if rr.kind in ("unique", "binding") or rr.group_id is not None:
            rivals.extend((rr, other) for other in rr.options if other.slot != option.slot)
        if rr.group_id is not None:
            rows = await self.db.all("SELECT message_id FROM rr_messages WHERE guild_id = ? AND group_id = ? AND deleted_at IS NULL AND message_id != ?", (rr.guild_id, rr.group_id, rr.message_id))
            for row in rows:
                other_rr = await self.get(row["message_id"])
                if other_rr is not None:
                    rivals.extend((other_rr, other) for other in other_rr.options)
        return rivals

    async def _pick_up(self, queue: _Queue, guild: Any, member: Any, rr: RR, option: Option) -> Outcome:
        held = self._held(queue, member)
        if rr.blacklist and held & set(rr.blacklist):
            return Outcome(refused="You hold a role that can't pick up roles from this message.")
        if rr.whitelist and not held & set(rr.whitelist):
            return Outcome(refused="You need one of these roles first: " + ", ".join(f"<@&{r}>" for r in rr.whitelist))
        roles, problem = self._usable(guild, option)
        if problem:
            self._tell_staff(guild.id, f"unusable:{rr.message_id}", (
                f"A role on {rr.url()} can't be handed out. It may be gone, sit at or above the bot's highest role, or hold permissions the Commands page doesn't allow. "
                "Reactions stay on the message. Fix the role, then run `/reactionrole fix` to hand out what's waiting."
            ))
        if not roles:
            return Outcome(refused=problem, blocked=True)
        locks = [self._cap_locks.setdefault((guild.id, role_id), asyncio.Lock()) for role_id in sorted(role.id for role in roles if option.limits.get(role.id))]
        taken = []
        try:
            for lock in locks:
                await lock.acquire()
                taken.append(lock)
            return await self._change_for_pick(queue, guild, member, rr, option, roles, held)
        finally:
            for lock in reversed(taken):
                lock.release()

    def _holders(self, guild: Any, role: Any) -> int:
        """Who holds a role with a maximum: the gateway's copy, plus grants it doesn't show yet."""
        count = len(role.members)
        grants = self._cap_grants.get((guild.id, role.id))
        if grants:
            now = time.monotonic()
            for user_id, until in list(grants.items()):
                holder = guild.get_member(user_id)
                if until <= now or (holder is not None and holder.get_role(role.id) is not None):
                    del grants[user_id]
            count += len(grants)
        return count

    async def _change_for_pick(self, queue: _Queue, guild: Any, member: Any, rr: RR, option: Option, roles: list, held: set[int]) -> Outcome:
        wanted = {role.id for role in roles}
        for role in roles:
            cap = option.limits.get(role.id, 0)
            if cap and role.id not in held and self._holders(guild, role) >= cap:
                return Outcome(refused=f"{role.mention} is full.")
        if rr.kind == "verify" and wanted <= held:
            return Outcome(note="You already have this.")
        rivals = await self._rivals(rr, option)
        rival_ids = {role_id for _, other in rivals for role_id in other.role_ids} - wanted
        lost_ids = rival_ids & held
        if rr.kind == "binding" and lost_ids:
            return Outcome(refused="Your choice on this message is final.")
        if rr.kind == "limit" and len((held & rr.role_ids) | wanted) > rr.limit:
            return Outcome(refused=f"You can hold {rr.limit} roles from this message. Drop one first.")
        lost = [role for role in (guild.get_role(i) for i in lost_ids) if role is not None and role < guild.me.top_role]
        missing = [role for role in roles if role.id not in held]
        try:
            if lost:
                await member.remove_roles(*lost, reason=REASON)
            if missing:
                await member.add_roles(*missing, reason=REASON)
        except discord.NotFound:
            return Outcome()
        except discord.Forbidden as error:
            log.warning("Discord refused a reaction role on %s: %s", rr.message_id, error)
            self._tell_staff(guild.id, f"forbidden:{rr.message_id}", (
                f"Discord wouldn't let the bot change a role for {rr.url()}. Check its Manage Roles permission and where its role sits. "
                "Reactions stay on the message. Run `/reactionrole fix` afterward to hand out what's waiting."
            ))
            return Outcome(refused="The bot isn't allowed to change that role. Ask a moderator to check its permissions and where its role sits.", blocked=True)
        except TRANSIENT as error:
            log.warning("Reaction role on %s failed: %s", rr.message_id, error)
            return Outcome(retry=True, note="Discord had a problem changing your roles. Try again in a moment.")
        self._spend(guild.id, member.id, len(lost) + len(missing))
        queue.changes.update({role.id: False for role in lost})
        queue.changes.update({role.id: True for role in missing})
        for role in missing:
            if option.limits.get(role.id):
                self._cap_grants.setdefault((guild.id, role.id), {})[member.id] = time.monotonic() + QUEUE_MEMORY
        if rr.kind == "temp":
            due = time.time() + rr.temp_seconds
            for role in roles:
                # A role someone already held for good stays theirs. Only a role this message gave comes off later.
                if role.id in held and await self.bot.timers.get_key("rr_temp", f"{guild.id}:{member.id}:{role.id}") is None:
                    continue
                await self.bot.timers.schedule(
                    "rr_temp", due, {"guild_id": guild.id, "user_id": member.id, "role_id": role.id},
                    guild_id=guild.id, key=f"{guild.id}:{member.id}:{role.id}",
                )
        if rivals:
            await self._clear_rivals(member.id, rivals, lost_ids)
        return Outcome(added=missing, removed=lost)

    async def _put_down(self, queue: _Queue, guild: Any, member: Any, rr: RR, option: Option) -> Outcome:
        held = self._held(queue, member)
        existing = [role for role in (guild.get_role(i) for i in option.role_ids) if role is not None and role.id in held]
        roles = [role for role in existing if not role.managed and role < guild.me.top_role]
        if not roles:
            if not existing:
                if rr.kind == "temp":
                    await self._end_temp_timers(guild.id, member.id, option)
                return Outcome()
            self._tell_staff(guild.id, f"unusable:{rr.message_id}", f"A role on {rr.url()} can't be taken back, because it sits at or above the bot's highest role. Reactions stay on the message. Fix the role, then run `/reactionrole fix`.")
            return Outcome(refused="The bot can't change that role right now. Ask a moderator to check it.", blocked=True)
        try:
            await member.remove_roles(*roles, reason=REASON)
        except discord.NotFound:
            return Outcome()
        except discord.Forbidden as error:
            log.warning("Discord refused to remove a reaction role on %s: %s", rr.message_id, error)
            self._tell_staff(guild.id, f"forbidden:{rr.message_id}", (
                f"Discord wouldn't let the bot change a role for {rr.url()}. Check its Manage Roles permission and where its role sits. "
                "Reactions stay on the message. Run `/reactionrole fix` afterward."
            ))
            return Outcome(refused="The bot isn't allowed to change that role. Ask a moderator to check its permissions and where its role sits.", blocked=True)
        except TRANSIENT as error:
            log.warning("Removing a reaction role on %s failed: %s", rr.message_id, error)
            return Outcome(retry=True, note="Discord had a problem changing your roles. Try again in a moment.")
        self._spend(guild.id, member.id, len(roles))
        queue.changes.update({role.id: False for role in roles})
        for role in roles:
            self._cap_grants.get((guild.id, role.id), {}).pop(member.id, None)
        if rr.kind == "temp":
            await self._end_temp_timers(guild.id, member.id, option)
        return Outcome(removed=roles)

    async def _end_temp_timers(self, guild_id: int, user_id: int, option: Option) -> None:
        for role_id in option.role_ids:
            await self.bot.timers.cancel_key("rr_temp", f"{guild_id}:{user_id}:{role_id}")

    async def _clear_rivals(self, user_id: int, rivals: list[tuple[RR, Option]], lost_ids: set[int]) -> None:
        """A swap takes the old reaction off too, so what shows on the message matches what the member holds."""
        ids = list({rr.message_id for rr, _ in rivals})
        marks = ",".join("?" * len(ids))
        rows = await self.db.all(f"SELECT message_id, emoji_key FROM rr_seen WHERE user_id = ? AND message_id IN ({marks})", (user_id, *ids))
        seen = {(row["message_id"], row["emoji_key"]) for row in rows}
        for rr, option in rivals:
            if rr.mode == "reactions" and ((rr.message_id, option.key) in seen or set(option.role_ids) & lost_ids):
                await self._take_reaction(rr, option, user_id)

    async def _take_reaction(self, rr: RR, option: Option, user_id: int) -> None:
        now = time.monotonic()
        while self._own_removals:
            oldest = next(iter(self._own_removals))
            if self._own_removals[oldest] > now:
                break
            del self._own_removals[oldest]
        token = (rr.message_id, option.key, user_id)
        self._own_removals[token] = now + 60
        try:
            await self.bot.ops.remove_reaction(rr.channel_id, rr.message_id, option.emoji, user_id)
        except OpsError as error:
            self._own_removals.pop(token, None)
            log.warning("Couldn't take a reaction off %s: %s", rr.message_id, error)
            self._tell_staff(rr.guild_id, f"stuck:{rr.message_id}", f"The bot couldn't take a reaction off {rr.url()}. {error}")
            if rr.kind in CLEARING:
                # It's been handled and it won't come off. On record, a later start doesn't take it for a new one and give the role back to someone a moderator has since taken it from.
                await self._set_seen(rr.message_id, option.key, user_id, True)
            return
        await self._set_seen(rr.message_id, option.key, user_id, False)

    async def _temp_due(self, payload: dict, row: dict) -> None:
        """A role the bot can't take back right now stays due. A handler that raises keeps its row and tries again later, so a temporary role doesn't become a lasting one because Manage Roles was off for a minute. The reaction comes off only after the role has."""
        guild = self.bot.get_guild(payload["guild_id"])
        if guild is None:
            return
        user_id, role_id = payload["user_id"], payload["role_id"]
        member = await find_member(guild, user_id)
        role = guild.get_role(role_id)
        if member is not None and role is not None and member.get_role(role_id) is not None:
            if role >= guild.me.top_role:
                self._tell_staff(guild.id, f"temp:{role_id}", f"The time ended for {role.mention}, and the bot can't take it back because it sits at or above the bot's highest role. Move the bot's role above it. It keeps trying.")
                raise TemporaryRoleBlocked(f"Temporary reaction role {role_id} in {guild.id} sits above the bot")
            try:
                await member.remove_roles(role, reason="Temporary reaction role ended")
            except discord.NotFound as error:
                log.info("A temporary reaction role in %s is gone: %s", guild.id, error)
            except discord.Forbidden:
                self._tell_staff(guild.id, f"temp:{role_id}", f"The time ended for {role.mention}, and Discord wouldn't let the bot take it back. Check its Manage Roles permission and where its role sits. It keeps trying.")
                raise
        queue = self._queues.get((guild.id, user_id))
        if queue is not None:
            queue.changes[role_id] = False
        # The reaction comes off too, so the member can pick the role up again.
        for message_id in await self.messages_using_role(guild.id, role_id):
            rr = await self.get(message_id)
            if rr is None or rr.mode != "reactions":
                continue
            for option in rr.options:
                if role_id in option.role_ids:
                    await self._take_reaction(rr, option, user_id)

    # ---- buttons and menus ----

    async def clicked(self, interaction: discord.Interaction, slots: list[int], *, view: discord.ui.View | None = None) -> None:
        """A button or menu use. Discord allows three seconds for an answer, so it's acknowledged first and the result follows privately."""
        if interaction.guild_id is None or interaction.message is None:
            return
        try:
            if view is not None:
                # Sending the menu back clears the member's pick on their screen, so the same option can be picked again later.
                await interaction.response.edit_message(view=view)
            else:
                await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as error:
            log.info("A role control answered too late: %s", error)
            return
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        self._enqueue(interaction.guild_id, interaction.user.id, Work("click", interaction.message.id, slots=slots, member=interaction.user, done=[done]))
        text = self._reply_text(await done)
        try:
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=NONE)
        except discord.HTTPException as error:
            log.info("Couldn't answer a role control: %s", error)

    def _reply_text(self, outcome: Outcome | None) -> str:
        if outcome is None:
            return "This message doesn't hand out roles anymore."
        parts = []
        if outcome.added:
            parts.append("You now have " + ", ".join(r.mention for r in outcome.added) + ".")
        if outcome.removed:
            parts.append("You no longer have " + ", ".join(r.mention for r in outcome.removed) + ".")
        if outcome.note:
            parts.append(outcome.note)
        if outcome.refused:
            parts.append(outcome.refused)
        return " ".join(parts) or "Nothing changed."

    # ---- catching up after downtime ----

    async def reconcile_all(self, *, thorough: bool = True) -> None:
        """Runs at every gateway ready. A verify message comes first, since it can be holding people out of the server."""
        rows = await self.db.all("SELECT message_id FROM rr_messages WHERE deleted_at IS NULL ORDER BY CASE kind WHEN 'verify' THEN 0 ELSE 1 END, message_id")
        for row in rows:
            try:
                await self.reconcile_message(row["message_id"], thorough=thorough)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Catching up message %s failed", row["message_id"])

    async def reconcile_message(self, message_id: int, *, thorough: bool = False, tries: int = 0) -> None:
        rr = await self.get(message_id)
        if rr is None:
            return
        guild = self.bot.get_guild(rr.guild_id)
        if guild is not None and guild.unavailable:
            self._unread.add(rr.guild_id)  # nothing can be read during an outage, and on_guild_available comes back for it
            return
        if guild is not None:
            # A role deleted while the bot was away sent no event. Unless the server is out, its role list here is whole.
            deleted = {role_id for role_id in rr.role_ids if guild.get_role(role_id) is None}
            if deleted:
                await self._drop_roles(rr.message_id, deleted)
                self._tell_staff(rr.guild_id, f"deleted:{rr.message_id}", f"{len(deleted)} role{'s' if len(deleted) != 1 else ''} on {rr.url()} no longer exist{'s' if len(deleted) == 1 else ''}, so the bot took them off the message.")
                rr = await self.get(message_id)
                if rr is None:
                    return
        ops = self.bot.ops
        try:
            current = await ops.fetch_message(rr.channel_id, rr.message_id)
        except MessageGone:
            await self._trash(rr.message_id)
            return
        except OpsError as error:
            log.info("Couldn't read message %s to catch up: %s", rr.message_id, error)
            await self._look_again(rr, f"The bot can't read {rr.url()}. {error}", thorough, tries)
            return
        if rr.foreign == bool(current["mine"]):
            rr.foreign = not current["mine"]
            await self.db.run("UPDATE rr_messages SET foreign_author = ? WHERE message_id = ?", (int(rr.foreign), rr.message_id))
        if rr.mode != "reactions":
            # Discord tells the member when a click finds no bot, so clicks during downtime leave nothing to catch up on.
            if current["mine"] and not current.get("controls"):
                try:
                    await ops.edit_message(rr.channel_id, rr.message_id, current["payload"], components=self.render(rr))
                except OpsError as error:
                    log.info("Couldn't put the controls back on message %s: %s", rr.message_id, error)
            return
        me_id = guild.me.id if guild is not None else None
        reactions = {emoji_key(r["emoji"]): r for r in current["reactions"]}
        missing = {o.key for o in rr.options if not (reactions.get(o.key) or {}).get("me")}
        cant_react: set[str] = set()
        for option in rr.options:
            if option.key in missing:
                problem = await self._react(rr, option)
                if problem:
                    cant_react.add(option.key)
                    log.info("Catching up %s: %s", rr.message_id, problem)
        cleared = missing - cant_react
        if cleared:
            # The bot's own reaction was gone, so either someone cleared it or Discord's answer left things out. An answer read after putting it back has to show it.
            try:
                current = await ops.fetch_message(rr.channel_id, rr.message_id)
            except MessageGone:
                await self._trash(rr.message_id)
                return
            except OpsError as error:
                await self._look_again(rr, f"The bot can't read {rr.url()}. {error}", thorough, tries)
                return
            reactions = {emoji_key(r["emoji"]): r for r in current["reactions"]}
            if any(not (reactions.get(key) or {}).get("me") for key in cleared):
                await self._look_again(rr, f"Discord's answer for {rr.url()} left out the bot's own reaction, so it can't be trusted.", thorough, tries)
                return
        new_setup = await self.db.scalar("SELECT synced_at FROM rr_synced WHERE message_id = ?", (rr.message_id,)) is None
        first_time = await self._needs_baseline(rr)
        finished = True
        already_there = 0
        for option in rr.options:
            reaction = reactions.get(option.key)
            others = (reaction["count"] - (1 if reaction.get("me") else 0)) if reaction else 0
            try:
                if rr.kind in CLEARING and new_setup:
                    already_there += await self._baseline(rr, option, me_id, others)
                elif rr.kind in CLEARING:
                    if others > 0:
                        await self._handle_leftovers(rr, option, me_id)
                elif first_time:
                    await self._baseline(rr, option, me_id, others)
                else:
                    seen = await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ? AND emoji_key = ?", (rr.message_id, option.key))
                    if thorough or seen != others:
                        await self._catch_up(rr, option, me_id, others, cleared=option.key in cleared, unsure=option.key in cant_react)
            except OpsError as error:
                finished = False
                log.warning("Couldn't read reactions on message %s: %s", rr.message_id, error)
        if first_time and finished:
            await self._mark_synced(rr.message_id)
        if already_there:
            self._tell_staff(rr.guild_id, f"old:{rr.message_id}", (
                f"{rr.url()} already had {already_there} reactions when it became a {rr.kind} message. Nobody's roles changed and those reactions stay. "
                "Anyone who wants what it hands out can take theirs off and add it again."
            ))
        if not finished:
            await self._look_again(rr, f"The bot can't read who reacted on {rr.url()}.", thorough, tries)

    async def _reactors(self, rr: RR, option: Option, me_id: int | None) -> dict[int, bool]:
        """Everyone with this reaction on the message, as user ID and whether it's a bot. Raises OpsError rather than return a partial list, because a short list would read as people having left."""
        people: dict[int, bool] = {}
        for burst in (False, True):
            after = None
            while True:
                page = await self.bot.ops.reaction_users(rr.channel_id, rr.message_id, option.emoji, after=after, limit=100, burst=burst)
                if not page:
                    break
                for row in page:
                    people[row["id"]] = row["bot"]
                after = page[-1]["id"]
        people.pop(me_id, None)
        return people

    async def _read_reactors(self, rr: RR, option: Option, me_id: int | None) -> tuple[dict[int, bool], dict[int, bool]]:
        """Everyone with this reaction, and who acted on it while the list was being read."""
        token = (rr.message_id, option.key)
        acted: dict[int, bool] = {}
        self._reading.setdefault(token, []).append(acted)
        try:
            return await self._reactors(rr, option, me_id), acted
        finally:
            self._reading[token].remove(acted)
            if not self._reading[token]:
                del self._reading[token]

    async def _handle_leftovers(self, rr: RR, option: Option, me_id: int | None) -> None:
        """A reaction still here hasn't been handled, unless it's on record: one from before this message took reactions off, or one the bot couldn't remove."""
        clears = self._clears.get(rr.message_id, 0)
        rows = await self.db.all("SELECT user_id FROM rr_seen WHERE message_id = ? AND emoji_key = ?", (rr.message_id, option.key))
        known = {row["user_id"] for row in rows}
        people, acted = await self._read_reactors(rr, option, me_id)
        if self._clears.get(rr.message_id, 0) != clears:
            return  # the reactions were cleared while being read
        for user_id, is_bot in people.items():
            if not is_bot and user_id not in known and user_id not in acted:
                self._enqueue(rr.guild_id, user_id, Work("added", rr.message_id, key=option.key))

    async def _baseline(self, rr: RR, option: Option, me_id: int | None, others: int) -> int:
        """A message that already has reactions when it first gets reaction roles. They're on record from now on, and nobody's roles change, since a member may have lost that role on purpose since reacting. Returns how many are people rather than bots."""
        # A reaction that came or went during the read is the queue's to record, so these rows stay as they are.
        clears = self._clears.get(rr.message_id, 0)
        people, acted = await self._read_reactors(rr, option, me_id) if others else ({}, {})
        if self._clears.get(rr.message_id, 0) != clears:
            return 0  # the reactions were cleared while being read, and the clear emptied the record already
        rows = [(rr.message_id, option.key, user_id) for user_id in people if user_id not in acted]

        def replace(connection: Any) -> None:
            kept = ",".join("?" * len(acted))
            connection.execute(f"DELETE FROM rr_seen WHERE message_id = ? AND emoji_key = ? AND user_id NOT IN ({kept})", (rr.message_id, option.key, *acted))
            connection.executemany("INSERT OR IGNORE INTO rr_seen (message_id, emoji_key, user_id) VALUES (?, ?, ?)", rows)

        await self.db.exclusive(replace)
        return sum(1 for user_id, is_bot in people.items() if not is_bot and user_id not in acted)

    async def _catch_up(self, rr: RR, option: Option, me_id: int | None, others: int, *, cleared: bool = False, unsure: bool = False) -> None:
        """What changed on this emoji while the bot was away. `cleared` means the bot's own reaction had gone too, so reactions that are gone say nothing about what anyone wanted. `unsure` means the bot can't use this emoji, so it can't tell a clear from people leaving."""
        clears = self._clears.get(rr.message_id, 0)
        rows = await self.db.all("SELECT user_id FROM rr_seen WHERE message_id = ? AND emoji_key = ?", (rr.message_id, option.key))
        known = {row["user_id"] for row in rows}
        people, acted = await self._read_reactors(rr, option, me_id)
        if self._clears.get(rr.message_id, 0) != clears:
            log.info("The reactions on %s were cleared during a catch-up, so it changed nobody's roles", rr.message_id)
            return
        bots = [user_id for user_id, is_bot in people.items() if is_bot and user_id not in known]
        if bots:
            await self.db.many("INSERT OR IGNORE INTO rr_seen (message_id, emoji_key, user_id) VALUES (?, ?, ?)", [(rr.message_id, option.key, user_id) for user_id in bots])
        new = [user_id for user_id, is_bot in people.items() if user_id not in known and not is_bot and user_id not in acted]
        if len(new) > MASS_CLEAR and len(new) > len(people) / 2:
            # A crowd with no record reads as a lost record rather than a crowd picking a role, and some of them may have lost it on purpose.
            await self.db.many("INSERT OR IGNORE INTO rr_seen (message_id, emoji_key, user_id) VALUES (?, ?, ?)", [(rr.message_id, option.key, user_id) for user_id in new])
            self._tell_staff(rr.guild_id, f"crowd:{rr.message_id}", (
                f"{len(new)} reactions on {rr.url()} had no record, more than half of everyone on that emoji. That reads as a lost record, so they're on record now and nobody's roles changed. "
                "Anyone who wants the role can take the reaction off and add it again."
            ))
        else:
            for user_id in new:
                self._enqueue(rr.guild_id, user_id, Work("added", rr.message_id, key=option.key))
        gone = known - set(people) - set(acted)
        if len(people) + max(5, others // 100) < others:
            # Discord listed far fewer people than it counted, so the list can't be trusted to say who left.
            log.warning("Discord listed %d of %d reactions on message %s, so removals wait", len(people), others, rr.message_id)
            self._tell_staff(rr.guild_id, f"short:{rr.message_id}", f"Discord listed {len(people)} of the {others} reactions it counts on {rr.url()}, so nobody lost a role this time.")
            return
        if unsure or not gone:
            return
        if cleared or (len(gone) > MASS_CLEAR and len(gone) > len(known) / 2):
            await self.db.many("DELETE FROM rr_seen WHERE message_id = ? AND emoji_key = ? AND user_id = ?", [(rr.message_id, option.key, user_id) for user_id in gone])
            self._tell_staff(rr.guild_id, f"clear:{rr.message_id}", (
                f"{len(gone)} reactions disappeared from {rr.url()} while the bot was away. That reads as someone clearing them, so nobody lost a role. "
                "Anyone who wants to drop a role can react and then take the reaction off."
            ))
            return
        for user_id in gone:
            self._enqueue(rr.guild_id, user_id, Work("removed", rr.message_id, key=option.key))

    # ---- helpers for commands ----

    async def _record(self, ctx: commands.Context, action: str, detail: str) -> None:
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), action, detail=detail)

    async def _tracked(self, ctx: commands.Context, text: str | None) -> RR | None:
        """The reaction roles on a message someone named, or a refusal."""
        ref = parse_message_ref(text or "")
        if ref is None:
            await ctx.fail("That needs a message ID or a link to a message.")
            return None
        rr = await self.get(ref[2])
        if rr is None or rr.guild_id != ctx.guild.id:
            await ctx.fail("That message has no reaction roles. `/reactionrole show` lists the ones here.")
            return None
        return None if await self._refuse_hidden(ctx, rr.channel_id) else rr

    def _shows_to(self, member: Any, guild: Any, channel_id: int) -> bool:
        """A channel that's gone has nothing left to hide."""
        channel = guild.get_channel_or_thread(channel_id)
        return channel is None or access.can_view(member, channel)

    async def _refuse_hidden(self, ctx: commands.Context, channel_id: int) -> bool:
        if self._shows_to(ctx.author, ctx.guild, channel_id):
            return False
        await ctx.fail("You can't see that channel.")
        return True

    async def _refuse_posting(self, ctx: commands.Context, channel: Any) -> bool:
        refusal = access.post_refusal(ctx.author, channel)
        if refusal:
            await ctx.fail(refusal)
        return refusal is not None

    def _opening_refusal(self, ctx: commands.Context, rr: RR) -> str | None:
        """Taking a gate off a message is another way to reach its roles, so it takes the same rank as adding them."""
        if access.is_admin(ctx.author):
            return None
        for role_id in sorted(rr.role_ids):
            role = ctx.guild.get_role(role_id)
            if role is not None and role >= ctx.author.top_role:
                return f"{role.mention} sits at or above your highest role, so you can't open this message up to more people."
        return None

    async def _locate(self, ctx: commands.Context, text: str, channel: Any = None) -> tuple[int, int] | None:
        """A channel and message from an ID, a link or the word latest. A message in another server is refused."""
        word = text.strip().lower()
        place = channel or ctx.channel
        if await self._refuse_hidden(ctx, place.id):
            return None
        if word in ("latest", "last"):
            # A typed command is itself the newest message in its channel.
            before = ctx.message.id if ctx.interaction is None else None
            data = await self.bot.ops.channel_messages(ctx.guild.id, place.id, limit=1, before=before)
            if not data["messages"]:
                await ctx.fail("That channel has no messages yet.")
                return None
            return place.id, data["messages"][0]["id"]
        ref = parse_message_ref(text)
        if ref is None:
            await ctx.fail("That needs a message ID or a link to a message.")
            return None
        guild_id, channel_id, message_id = ref
        if guild_id is not None and guild_id != ctx.guild.id:
            await ctx.fail("That link points at another server.")
            return None
        if channel_id is not None and ctx.guild.get_channel_or_thread(channel_id) is None:
            await ctx.fail("That link points at a channel the bot can't see.")
            return None
        known = await self.get(message_id)
        if known is not None:
            if known.guild_id != ctx.guild.id:
                await ctx.fail("That link points at another server.")
                return None
            return None if await self._refuse_hidden(ctx, known.channel_id) else (known.channel_id, message_id)
        if channel_id is not None and await self._refuse_hidden(ctx, channel_id):
            return None
        return channel_id or place.id, message_id

    def _role_refusal(self, ctx: commands.Context, role: Any) -> str | None:
        guild = ctx.guild
        if role.is_default():
            return "Everyone always has @everyone, so it can't be handed out."
        if role.managed:
            return f"{role.mention} belongs to an integration, and Discord doesn't let anyone hand it out."
        if role >= guild.me.top_role:
            return f"{role.mention} sits at or above the bot's highest role, so the bot can't hand it out. Move the bot's role above it first."
        if ctx.author.id != guild.owner_id and role >= ctx.author.top_role:
            return f"{role.mention} sits at or above your highest role, so you can't set it up to be handed out."
        return None

    def _risk_note(self, guild: Any, role: Any) -> str | None:
        settings = self.bot.settings.get(guild.id, COMMANDS_MODULE)
        if role_is_safe([name for name, on in role.permissions if on], settings):
            return None
        return f"{role.mention} holds permissions the bot won't hand out while elevated roles are off on the Commands page."

    async def _add_pairs(self, ctx: commands.Context, message: str, pairs: list[tuple[str, Any]], channel: Any = None) -> None:
        place = await self._locate(ctx, message, channel)
        if place is None:
            return
        channel_id, message_id = place
        rr = await self.get(message_id)
        previous = rr.copied() if rr else None
        if rr is None:
            current = await self.bot.ops.fetch_message(channel_id, message_id)
            rr = RR(message_id, ctx.guild.id, channel_id, foreign=not current["mine"], created_at=time.time())
            if current["mine"] and holds_roles_block(current["payload"]):
                rr.template = current["payload"]
        notes = []
        for emoji_text, role in pairs:
            refusal = self._role_refusal(ctx, role)
            if refusal:
                await ctx.fail(refusal)
                return
            risk = self._risk_note(ctx.guild, role)
            if risk:
                notes.append(risk)
            emoji = clean_emoji(emoji_text)
            key = emoji_key(emoji)
            option = rr.by_key(key)
            if option is None:
                slot = max((o.slot for o in rr.options), default=-1) + 1
                option = Option(slot, emoji, key)
                rr.options.append(option)
            if role.id not in option.role_ids:
                option.role_ids.append(role.id)
        link_buttons = 0
        if rr.mode != "reactions":
            link_buttons = len((await self.bot.ops.fetch_message(rr.channel_id, rr.message_id))["payload"].get("buttons") or [])
        problem = capacity_problem(rr.mode, len(rr.options), link_buttons)
        if problem:
            await ctx.fail(problem)
            return
        notes.extend(await self._save(rr, previous))
        await self._record(ctx, "reactionrole.add", str(rr.message_id))
        added = ", ".join(f"{emoji} {role.mention}" for emoji, role in pairs)
        if rr.foreign:
            notes.append("Another bot wrote this message, so its text can't be edited from here until it's reposted. The roles work the same.")
        await ctx.ok(" ".join([f"Added {added} on {rr.url()}", *notes])[:1900])

    async def _set_kind(self, ctx: commands.Context, text: str, kind: str, *, limit: int = 0, temp_seconds: int = 0) -> None:
        rr = await self._tracked(ctx, text)
        if rr is None:
            return
        if opens_up(rr, rr.whitelist, rr.blacklist, kind):
            refusal = self._opening_refusal(ctx, rr)
            if refusal:
                await ctx.fail(refusal)
                return
        previous = rr.copied()
        rr.kind = kind
        rr.limit = limit
        rr.temp_seconds = temp_seconds
        await self._save(rr, previous)
        await self._record(ctx, "reactionrole.type", f"{rr.message_id}: {kind}")
        extra = f" with a limit of {limit}" if kind == "limit" else (f" lasting {humanize(temp_seconds)}" if kind == "temp" else "")
        await ctx.ok(f"That message is now type `{kind}`{extra}.")

    async def _edit_list(self, ctx: commands.Context, which: str, choice: str, message: str | None, roles: str | None) -> None:
        word = "blacklist" if which == "blacklist" else "whitelist"
        if choice == "show" and not message:
            lines = []
            for rr in await self.for_guild(ctx.guild.id):
                ids = getattr(rr, which)
                if ids and self._shows_to(ctx.author, ctx.guild, rr.channel_id):
                    lines.append(f"`{rr.message_id}`: {', '.join(f'<@&{i}>' for i in ids)}")
            await send_pages(ctx, word.title(), lines, empty=f"No message has a {word}.")
            return
        rr = await self._tracked(ctx, message)
        if rr is None:
            return
        current = list(getattr(rr, which))
        if choice == "show":
            await ctx.send(f"The {word}: {', '.join(f'<@&{i}>' for i in current) or 'empty'}.", allowed_mentions=NONE)
            return
        if choice == "clear":
            if await self._refuse_opening(ctx, rr, which, []):
                return
            setattr(rr, which, [])
            await self._write(rr)
            await self._record(ctx, f"reactionrole.{word}", f"{rr.message_id}: cleared")
            await ctx.ok(f"Cleared the {word}.")
            return
        found, missing = find_roles(ctx.guild, roles or "")
        if missing:
            await ctx.fail(missing_roles_sentence(missing))
            return
        if not found:
            await ctx.fail("Name at least one role.")
            return
        ids = [role.id for role in found]
        changed = [*current, *(i for i in ids if i not in current)] if choice == "add" else [i for i in current if i not in ids]
        if await self._refuse_opening(ctx, rr, which, changed):
            return
        setattr(rr, which, changed)
        await self._write(rr)
        await self._record(ctx, f"reactionrole.{word}", f"{rr.message_id}: {choice}")
        await ctx.ok(f"The {word} on that message now holds: {', '.join(f'<@&{i}>' for i in getattr(rr, which)) or 'nothing'}.")

    async def _refuse_opening(self, ctx: commands.Context, rr: RR, which: str, changed: list[int]) -> bool:
        lists = {"whitelist": rr.whitelist, "blacklist": rr.blacklist, which: changed}
        refusal = self._opening_refusal(ctx, rr) if opens_up(rr, lists["whitelist"], lists["blacklist"], rr.kind) else None
        if refusal:
            await ctx.fail(refusal)
        return refusal is not None

    async def _create_all_in_one(self, ctx: commands.Context, kind: str, color: str, text: str, channel: Any = None) -> None:
        """The whole setup from one command. Text commands can name the channel anywhere in the text."""
        mention = _CHANNEL_MENTION.search(text)
        if channel is None and mention:
            channel = ctx.guild.get_channel_or_thread(int(mention.group(1)))
            text = (text[:mention.start()] + text[mention.end():]).strip()
        target = channel or ctx.channel
        if await self._refuse_posting(ctx, target):
            return
        wording, pairs = split_text_and_pairs(ctx.guild, text)
        payload = title_payload(wording, None if color.lower() in ("none", "default", "-") else hex_color(color))
        notes = []
        for _, role in pairs:
            refusal = self._role_refusal(ctx, role)
            if refusal:
                await ctx.fail(refusal)
                return
            risk = self._risk_note(ctx.guild, role)
            if risk:
                notes.append(risk)
        rr = RR(0, ctx.guild.id, target.id, kind=kind, created_at=time.time())
        for slot, (emoji_text, role) in enumerate(pairs):
            emoji = clean_emoji(emoji_text)
            key = emoji_key(emoji)
            option = rr.by_key(key)
            if option is None:
                rr.options.append(Option(slot, emoji, key, [role.id]))
            elif role.id not in option.role_ids:
                option.role_ids.append(role.id)
        problem = capacity_problem(rr.mode, len(rr.options), 0)
        if problem:
            await ctx.fail(problem)
            return
        payload = self._take_template(rr, payload)
        sent = await self.bot.ops.send_message(target.id, payload)
        rr.message_id = sent["id"]
        await self._add_to_library(rr, payload, self.bot.actor(ctx.author))
        notes.extend(await self._save(rr, None, fresh=True))
        await self._record(ctx, "reactionrole.create", str(rr.message_id))
        await ctx.ok(" ".join([f"Posted it in {target.mention}. Message ID `{rr.message_id}`.", *notes])[:1900])

    async def _add_to_library(self, rr: RR, payload: dict, actor: Any, *, note: str = "Posted") -> None:
        """So a role post has a version history from its first version, like any other post. An edit adds a version."""
        embeds = self.bot.module("embeds")
        if embeds is None:
            return
        try:
            await embeds.record_post(rr.guild_id, rr.channel_id, rr.message_id, payload, actor=actor, source="reaction roles", note=note)
        except Exception:
            log.warning("Could not add reaction role post %s to the message library", rr.message_id, exc_info=True)  # the roles work without it

    def _describe(self, rr: RR) -> list[str]:
        head = f"`{rr.message_id}` in <#{rr.channel_id}>, type `{rr.kind}`"
        if rr.mode != "reactions":
            head += f", {rr.mode}"
        lines = [head]
        for option in rr.options:
            roles = ", ".join(f"<@&{role_id}>" for role_id in option.role_ids)
            lines.append(f"{option.emoji or option.label or '-'} {roles}")
        return lines

    # ---- /reactionrole ----

    @commands.hybrid_group(name="reactionrole", aliases=["rr", "reactrole"], description="Set up roles members pick up from a message")
    @commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def reactionrole(self, ctx: commands.Context) -> None:
        await self._show(ctx, None)

    async def _show(self, ctx: commands.Context, message: str | None) -> None:
        if not message:
            lines: list[str] = []
            for rr in await self.for_guild(ctx.guild.id):
                if self._shows_to(ctx.author, ctx.guild, rr.channel_id):
                    lines.extend(self._describe(rr))
                    lines.append("")
            await send_pages(ctx, "Reaction roles", lines[:-1], empty="This server has no reaction roles yet. `/reactionrole setup` walks through making one.")
            return
        rr = await self._tracked(ctx, message)
        if rr is None:
            return
        lines = self._describe(rr)
        lines.append(f"Message: {rr.url()}")
        if rr.kind == "limit":
            lines.append(f"Members can hold {rr.limit} roles from it.")
        if rr.kind == "temp":
            lines.append(f"Roles last {humanize(rr.temp_seconds)}.")
        if rr.whitelist:
            lines.append("Only for: " + ", ".join(f"<@&{i}>" for i in rr.whitelist))
        if rr.blacklist:
            lines.append("Not for: " + ", ".join(f"<@&{i}>" for i in rr.blacklist))
        caps = [f"<@&{role_id}> up to {count}" for option in rr.options for role_id, count in option.limits.items()]
        if caps:
            lines.append("Maximum holders: " + ", ".join(caps))
        if rr.group_id is not None:
            linked = await self.db.all("SELECT message_id FROM rr_messages WHERE guild_id = ? AND group_id = ? AND message_id != ? AND deleted_at IS NULL", (rr.guild_id, rr.group_id, rr.message_id))
            lines.append("Linked to: " + ", ".join(f"`{row['message_id']}`" for row in linked))
        if rr.destruct_at:
            lines.append(f"Deletes itself <t:{int(rr.destruct_at)}:R>.")
        if rr.foreign:
            lines.append("Another bot wrote this message, so its text can't be edited here until it's reposted.")
        await send_pages(ctx, "Reaction role", lines)

    @reactionrole.command(name="show", aliases=["list"], description="List reaction roles, or the details of one message")
    @app_commands.describe(message_id="A message's ID or link, for its details")
    async def rr_show(self, ctx: commands.Context, message_id: Optional[str] = None) -> None:
        await self._show(ctx, message_id)

    @reactionrole.command(name="add", description="Add an emoji and role to a message")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link, or the word latest", emoji="The emoji", role="The role it hands out", channel="Where the message is, if not here")
    async def rr_add(self, ctx: commands.Context, message_id: str, emoji: str, *, role: str = "", channel: Optional[discord.TextChannel] = None) -> None:
        await ctx.defer(ephemeral=True)  # one call to Discord per emoji, which can pass the three seconds an answer gets
        words = f"{message_id} {emoji} {role}".strip()
        lead = _CHANNEL_MENTION.match(words)
        if lead:  # the text form can name the channel first
            channel = ctx.guild.get_channel_or_thread(int(lead.group(1)))
            words = words[lead.end():].strip()
        trail = re.search(r"\s<#(\d{15,25})>\s*$", words)
        if trail and channel is None:
            channel = ctx.guild.get_channel_or_thread(int(trail.group(1)))
            words = words[:trail.start()]
        first, _, rest = words.partition(" ")
        if looks_like_emoji(first):
            target, pairs_text = "latest", words
        else:
            target, pairs_text = first, rest
        pairs, problem = parse_pairs(ctx.guild, pairs_text)
        if problem or not pairs:
            await ctx.fail(problem or "Name an emoji and a role.")
            return
        await self._add_pairs(ctx, target, pairs, channel)

    @reactionrole.command(name="addmany", description="Add several emoji and roles to a message at once")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link, or the word latest", pairs="Emoji and role pairs, such as: \N{SMILING FACE WITH OPEN MOUTH} Member \N{WAVING HAND SIGN} Guest", channel="Where the message is, if not here")
    async def rr_addmany(self, ctx: commands.Context, message_id: str, *, pairs: str, channel: Optional[discord.TextChannel] = None) -> None:
        await ctx.defer(ephemeral=True)
        mention = _CHANNEL_MENTION.fullmatch(message_id.strip())
        if mention:
            channel = ctx.guild.get_channel_or_thread(int(mention.group(1)))
            message_id, _, pairs = pairs.strip().partition(" ")
        trail = re.search(r"\s<#(\d{15,25})>\s*$", pairs)
        if trail and channel is None:
            channel = ctx.guild.get_channel_or_thread(int(trail.group(1)))
            pairs = pairs[:trail.start()]
        found, problem = parse_pairs(ctx.guild, pairs)
        if problem or not found:
            await ctx.fail(problem or "Name at least one emoji and role.")
            return
        await self._add_pairs(ctx, message_id, found, channel)

    @reactionrole.command(name="remove", description="Take a role off one message, or off every message")
    @requires("manage_guild")
    @app_commands.describe(role="The role to take off", message_id="Only from this message")
    async def rr_remove(self, ctx: commands.Context, *, role: str, message_id: Optional[str] = None) -> None:
        await ctx.defer(ephemeral=True)
        # The text form can put the message first or last, so everything arrives as one string.
        words = role.split()
        if message_id is None:
            for index in (0, len(words) - 1):
                if words and parse_message_ref(words[index]) and find_role(ctx.guild, words[index], loose=False) is None:
                    message_id = words.pop(index)
                    break
        found = find_role(ctx.guild, " ".join(words))
        if found is None:
            await ctx.fail(f"There's no role called `{' '.join(words)[:60]}`.")
            return
        if message_id:
            rr = await self._tracked(ctx, message_id)
            if rr is None:
                return
            targets = [rr]
        else:
            targets = [rr for rr in [await self.get(i) for i in await self.messages_using_role(ctx.guild.id, found.id)] if rr]
        changed = 0
        for rr in targets:
            if found.id not in rr.role_ids:
                continue
            previous = rr.copied()
            for option in rr.options:
                option.role_ids = [r for r in option.role_ids if r != found.id]
                option.limits.pop(found.id, None)
            rr.options = [o for o in rr.options if o.role_ids]
            if rr.options:
                await self._save(rr, previous)
            else:
                await self.remove(previous)
            changed += 1
        if not changed:
            await ctx.fail(f"{found.mention} isn't on {'that message' if message_id else 'any reaction role message'}.")
            return
        await self._record(ctx, "reactionrole.remove", f"role {found.id}")
        await ctx.ok(f"Took {found.mention} off {changed} message{'s' if changed != 1 else ''}.")

    async def _confirm(self, ctx: commands.Context, prompt: str) -> bool:
        view = Confirm(ctx.author.id)
        sent = await ctx.send(prompt, view=view, allowed_mentions=NONE)
        if sent is None:
            return False
        await view.wait()
        return view.answer

    @reactionrole.command(name="purge", aliases=["clear"], description="Remove reaction roles from one message, or from the whole server")
    @requires("manage_guild")
    @app_commands.describe(message_id="Only this message. Leave it out for the whole server.")
    async def rr_purge(self, ctx: commands.Context, message_id: Optional[str] = None) -> None:
        await ctx.defer(ephemeral=True)
        if message_id:
            rr = await self._tracked(ctx, message_id)
            if rr is None:
                return
            await self.remove(rr)
            await self._record(ctx, "reactionrole.delete", str(rr.message_id))
            await ctx.ok("Removed the reaction roles from that message. The message itself stays.")
            return
        every = await self.for_guild(ctx.guild.id)
        if not every:
            await ctx.fail("This server has no reaction roles.")
            return
        if not await self._confirm(ctx, f"Remove reaction roles from all {len(every)} messages in this server? Nobody will be able to pick up those roles. The messages stay."):
            await ctx.fail("Left everything as it was.")
            return
        for rr in every:
            await self.remove(rr)
        await self._record(ctx, "reactionrole.purge", f"{len(every)} messages")
        await ctx.ok(f"Removed reaction roles from {len(every)} messages.")

    @reactionrole.command(name="blacklist", description="Stop members with these roles from picking up roles from a message")
    @requires("manage_guild")
    @app_commands.describe(choice="What to do with the list", message_id="The message's ID or link", roles="The roles, as mentions, IDs or names")
    async def rr_blacklist(self, ctx: commands.Context, choice: ListChoice, message_id: Optional[str] = None, *, roles: Optional[str] = None) -> None:
        await self._edit_list(ctx, "blacklist", choice, message_id, roles)

    @reactionrole.command(name="whitelist", description="Only let members with these roles pick up roles from a message")
    @requires("manage_guild")
    @app_commands.describe(choice="What to do with the list", message_id="The message's ID or link", roles="The roles, as mentions, IDs or names")
    async def rr_whitelist(self, ctx: commands.Context, choice: ListChoice, message_id: Optional[str] = None, *, roles: Optional[str] = None) -> None:
        await self._edit_list(ctx, "whitelist", choice, message_id, roles)

    @reactionrole.command(name="selfdestruct", description="Delete a message and its reaction roles after some time")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", when="How long from now, such as 7d, or off to cancel")
    async def rr_selfdestruct(self, ctx: commands.Context, message_id: str, *, when: str) -> None:
        rr = await self._tracked(ctx, message_id)
        if rr is None:
            return
        if when.strip().lower() in ("off", "cancel", "never", "none"):
            rr.destruct_at = None
            await self._write(rr)
            await self._set_destruct_timer(rr)
            await self._record(ctx, "reactionrole.selfdestruct", f"{rr.message_id}: off")
            await ctx.ok("That message will stay.")
            return
        seconds = parse_duration(when)
        if seconds is None or not 60 <= seconds <= MAX_DESTRUCT:
            await ctx.fail("Give a time from one minute to one year, such as `7d`, or `off` to cancel.")
            return
        channel = ctx.guild.get_channel_or_thread(rr.channel_id)
        if channel is not None and not access.can_manage_messages(ctx.author, channel):
            await ctx.fail(NEEDS_MANAGE_MESSAGES)
            return
        rr.destruct_at = time.time() + seconds
        await self._write(rr)
        await self._set_destruct_timer(rr)
        await self._record(ctx, "reactionrole.selfdestruct", f"{rr.message_id}: {seconds}")
        await ctx.ok(f"That message and its reaction roles go in {humanize(seconds)}.")

    @reactionrole.command(name="edit", description="Change a message's title and description, as title | description")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", message_string="The new text, as title | description")
    async def rr_edit(self, ctx: commands.Context, message_id: str, *, message_string: str) -> None:
        await ctx.defer(ephemeral=True)
        rr = await self._tracked(ctx, message_id)
        if rr is None:
            return
        channel = ctx.guild.get_channel_or_thread(rr.channel_id)
        if channel is not None and await self._refuse_posting(ctx, channel):
            return
        current = await self.bot.ops.fetch_message(rr.channel_id, rr.message_id)
        if not current["mine"]:
            await ctx.fail("Another bot wrote that message, and Discord only lets a bot edit its own. Repost it from the Embeds page first.")
            return
        payload = dict(rr.template or current["payload"])
        embeds = [dict(e) for e in payload.get("embeds", [])] or [{}]
        title, has_bar, description = message_string.replace("\\|", "|").partition("|")
        embeds[0]["title"] = title.strip()
        if has_bar:
            embeds[0]["description"] = description.strip()
        payload["embeds"] = embeds
        filled = self._take_template(rr, messages.clean_payload(payload))
        await self.bot.ops.edit_message(rr.channel_id, rr.message_id, filled, components=self.render(rr))
        await self._write(rr)
        await self._add_to_library(rr, filled, self.bot.actor(ctx.author), note="Edited")
        await self._record(ctx, "reactionrole.edit", str(rr.message_id))
        await ctx.ok("Edited the message.")

    @reactionrole.command(name="color", aliases=["colour"], description="Change a message's accent color")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", color="Six hex digits, such as FF0000", channel="Where the message is, if not here")
    async def rr_color(self, ctx: commands.Context, message_id: str, color: str, channel: Optional[discord.TextChannel] = None) -> None:
        await ctx.defer(ephemeral=True)
        value = hex_color(color)
        place = await self._locate(ctx, message_id, channel)
        if place is None:
            return
        current = await self.bot.ops.fetch_message(*place)
        if not current["mine"]:
            await ctx.fail("Another bot wrote that message, and Discord only lets a bot edit its own. Repost it from the Embeds page first.")
            return
        if current.get("controls") and not await self.holds_message(ctx.guild.id, place[1]):
            await ctx.fail("That message has buttons or menus from another feature, and changing its color from here would take them off.")
            return
        embeds = current["payload"].get("embeds") or []
        if not embeds:
            await ctx.fail("That message has no embed, so it has no accent color to change.")
            return
        payload = {**current["payload"], "embeds": [{**embed, "color": value} for embed in embeds]}
        await self.bot.ops.edit_message(place[0], place[1], messages.clean_payload(payload))
        await self._record(ctx, "reactionrole.color", str(place[1]))
        await ctx.ok(f"Changed the color to #{value:06x}.")

    @reactionrole.command(name="channel", aliases=["cc"], description="Create a channel set up for reaction roles")
    @requires("manage_guild")
    @app_commands.describe(channel_name="The new channel's name")
    async def rr_channel(self, ctx: commands.Context, *, channel_name: str = "get-roles") -> None:
        guild = ctx.guild
        # Members can use the reactions already there and can't add their own or post, so the channel stays tidy.
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=False, read_message_history=True, send_messages_in_threads=False, create_public_threads=False, create_private_threads=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, add_reactions=True, read_message_history=True, manage_messages=True),
        }
        channel = await guild.create_text_channel(channel_name[:100], overwrites=overwrites, reason=f"Used by {ctx.author} ({ctx.author.id})")
        await self._record(ctx, "reactionrole.channel", channel.name)
        await ctx.ok(f"Created {channel.mention}. Members can read it and use the reactions in it, and can't post.")

    @reactionrole.command(name="fix", description="Put back reactions that are missing or were cleared")
    @requires("manage_guild")
    @app_commands.describe(message_id="Only this message. Leave it out for all of them.")
    async def rr_fix(self, ctx: commands.Context, message_id: Optional[str] = None) -> None:
        await ctx.defer(ephemeral=True)
        if message_id:
            rr = await self._tracked(ctx, message_id)
            if rr is None:
                return
            targets = [rr]
        else:
            targets = await self.for_guild(ctx.guild.id)
        notes: list[str] = []
        for rr in targets:
            try:
                notes.extend(await self.redraw(rr))
            except OpsError as error:
                notes.append(f"`{rr.message_id}`: {error}")
            self._sync_soon(rr.message_id)
        await ctx.ok(" ".join([f"Checked {len(targets)} message{'s' if len(targets) != 1 else ''}.", *notes])[:1900])

    @reactionrole.command(name="fixforeign", description="Make the bot join reactions with emoji it can't use on its own")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", channel="Where the message is, if not here")
    async def rr_fixforeign(self, ctx: commands.Context, message_id: str, channel: Optional[discord.TextChannel] = None) -> None:
        await ctx.defer(ephemeral=True)
        place = await self._locate(ctx, message_id, channel)
        if place is None:
            return
        current = await self.bot.ops.fetch_message(*place)
        added = 0
        for reaction in current["reactions"]:
            if not reaction.get("me"):
                await self.bot.ops.add_reaction(place[0], place[1], reaction["emoji"])
                added += 1
        if not added:
            await ctx.fail("The bot already has every reaction on that message. React with the emoji you want first, then run this.")
            return
        await ctx.ok(f"The bot joined {added} reaction{'s' if added != 1 else ''}.")

    @reactionrole.command(name="modify", description="Change how a message hands out roles")
    @requires("manage_guild")
    @app_commands.describe(choice="The type", message_id="The message's ID or link", value="The limit for limit, or the time for temp")
    async def rr_modify(self, ctx: commands.Context, choice: TypeChoice, message_id: str, *, value: Optional[str] = None) -> None:
        await ctx.defer(ephemeral=True)
        if choice == "limit":
            await self._set_limit(ctx, message_id, value)
        elif choice == "temp":
            await self._set_temp(ctx, message_id, value)
        else:
            await self._set_kind(ctx, message_id, choice)

    async def _set_limit(self, ctx: commands.Context, message: str, value: Any) -> None:
        try:
            limit = integer(1, MAX_LIMIT)(value)
        except Invalid:
            await ctx.fail(f"A limit type needs a number from 1 to {MAX_LIMIT}.")
            return
        await self._set_kind(ctx, message, "limit", limit=limit)

    async def _set_temp(self, ctx: commands.Context, message: str, value: str | None) -> None:
        seconds = parse_duration(value or "")
        if seconds is None or not MIN_TEMP <= seconds <= MAX_TEMP:
            await ctx.fail("A temp type needs a time from one minute to one year, such as `24h`.")
            return
        await self._set_kind(ctx, message, "temp", temp_seconds=seconds)

    @reactionrole.command(name="normal", description="Put a message back to the normal type")
    @requires("manage_guild")
    async def rr_normal(self, ctx: commands.Context, message_id: str) -> None:
        await ctx.defer(ephemeral=True)
        await self._set_kind(ctx, message_id, "normal")

    @reactionrole.command(name="temp", description="Make a message's roles last a set time")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", duration="How long, such as 24h")
    async def rr_temp(self, ctx: commands.Context, message_id: str, *, duration: str) -> None:
        await ctx.defer(ephemeral=True)
        await self._set_temp(ctx, message_id, duration)

    @reactionrole.command(name="limit", description="Limit how many roles a member can hold from a message")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", limit="The most roles a member can hold from it")
    async def rr_limit(self, ctx: commands.Context, message_id: str, limit: commands.Range[int, 1, MAX_LIMIT]) -> None:
        await ctx.defer(ephemeral=True)
        await self._set_limit(ctx, message_id, limit)

    @reactionrole.command(name="unique", with_app_command=False, description="Let a member hold one role from a message at a time")
    @requires("manage_guild")
    async def rr_unique(self, ctx: commands.Context, message_id: str) -> None:
        await self._set_kind(ctx, message_id, "unique")

    @reactionrole.command(name="binding", with_app_command=False, description="Let a member choose one role from a message, for good")
    @requires("manage_guild")
    async def rr_binding(self, ctx: commands.Context, message_id: str) -> None:
        await self._set_kind(ctx, message_id, "binding")

    @reactionrole.command(name="verify", with_app_command=False, description="Roles can only be picked up, and the reaction comes off")
    @requires("manage_guild")
    async def rr_verify(self, ctx: commands.Context, message_id: str) -> None:
        await self._set_kind(ctx, message_id, "verify")

    @reactionrole.command(name="drop", with_app_command=False, description="Roles can only be taken away")
    @requires("manage_guild")
    async def rr_drop(self, ctx: commands.Context, message_id: str) -> None:
        await self._set_kind(ctx, message_id, "drop")

    @reactionrole.command(name="reversed", with_app_command=False, description="Reacting takes the role away and unreacting gives it")
    @requires("manage_guild")
    async def rr_reversed(self, ctx: commands.Context, message_id: str) -> None:
        await self._set_kind(ctx, message_id, "reversed")

    @reactionrole.command(name="lock", with_app_command=False, description="Stop a message handing out roles")
    @requires("manage_guild")
    async def rr_lock(self, ctx: commands.Context, message_id: str) -> None:
        await self._set_kind(ctx, message_id, "lock")

    @reactionrole.command(name="bl", with_app_command=False, description="Add roles to a message's blacklist")
    @requires("manage_guild")
    async def rr_bl(self, ctx: commands.Context, message_id: str, *, roles: str) -> None:
        await self._edit_list(ctx, "blacklist", "add", message_id, roles)

    @reactionrole.command(name="wl", with_app_command=False, description="Add roles to a message's whitelist")
    @requires("manage_guild")
    async def rr_wl(self, ctx: commands.Context, message_id: str, *, roles: str) -> None:
        await self._edit_list(ctx, "whitelist", "add", message_id, roles)

    @reactionrole.command(name="unbl", aliases=["unblacklist"], with_app_command=False, description="Take roles off a message's blacklist")
    @requires("manage_guild")
    async def rr_unbl(self, ctx: commands.Context, message_id: str, *, roles: str) -> None:
        await self._edit_list(ctx, "blacklist", "remove", message_id, roles)

    @reactionrole.command(name="unwl", with_app_command=False, description="Take roles off a message's whitelist")
    @requires("manage_guild")
    async def rr_unwl(self, ctx: commands.Context, message_id: str, *, roles: str) -> None:
        await self._edit_list(ctx, "whitelist", "remove", message_id, roles)

    @reactionrole.command(name="clearbl", with_app_command=False, description="Empty a message's blacklist")
    @requires("manage_guild")
    async def rr_clearbl(self, ctx: commands.Context, message_id: str) -> None:
        await self._edit_list(ctx, "blacklist", "clear", message_id, None)

    @reactionrole.command(name="clearwl", with_app_command=False, description="Empty a message's whitelist")
    @requires("manage_guild")
    async def rr_clearwl(self, ctx: commands.Context, message_id: str) -> None:
        await self._edit_list(ctx, "whitelist", "clear", message_id, None)

    @reactionrole.command(name="links", description="Show which messages are linked")
    @requires("manage_guild")
    async def rr_links(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT message_id, group_id, channel_id FROM rr_messages WHERE guild_id = ? AND group_id IS NOT NULL AND deleted_at IS NULL ORDER BY group_id, message_id", (ctx.guild.id,))
        groups: dict[int, list[int]] = {}
        for row in rows:
            if self._shows_to(ctx.author, ctx.guild, row["channel_id"]):
                groups.setdefault(row["group_id"], []).append(row["message_id"])
        lines = [", ".join(f"`{message_id}`" for message_id in ids) for ids in groups.values()]
        await send_pages(ctx, "Linked messages", lines, empty="No messages are linked.")

    @reactionrole.command(name="link", description="Link messages, so a member can hold one role across all of them")
    @requires("manage_guild")
    @app_commands.describe(message_ids="Two or more message IDs or links")
    async def rr_link(self, ctx: commands.Context, *, message_ids: str) -> None:
        pieces = [piece for piece in re.split(r"[\s,]+", message_ids.strip()) if piece]
        if len(pieces) < 2:
            await ctx.fail("Name at least two messages.")
            return
        members: list[RR] = []
        for piece in pieces:
            rr = await self._tracked(ctx, piece)
            if rr is None:
                return
            if all(other.message_id != rr.message_id for other in members):
                members.append(rr)
        if len(members) < 2:
            await ctx.fail("Name at least two different messages.")
            return
        await self._link_group(ctx.guild.id, members)
        await self._record(ctx, "reactionrole.link", ", ".join(str(rr.message_id) for rr in members))
        await ctx.ok(f"Linked {len(members)} messages. A member can hold one role across them.")

    async def _link_group(self, guild_id: int, members: list[RR]) -> None:
        """A message already in a group brings that group along, so a link never splits one."""
        existing = {rr.group_id for rr in members if rr.group_id is not None}
        group_id = min(existing) if existing else min(rr.message_id for rr in members)
        for rr in members:
            rr.group_id = group_id

        def link(connection: Any) -> None:
            for old in existing:
                connection.execute("UPDATE rr_messages SET group_id = ? WHERE guild_id = ? AND group_id = ?", (group_id, guild_id, old))
            for rr in members:
                connection.execute("UPDATE rr_messages SET group_id = ? WHERE message_id = ?", (group_id, rr.message_id))

        await self.db.exclusive(link)
        await self._redraw_group_menus(guild_id, group_id)

    async def _redraw_group_menus(self, guild_id: int, group_id: int | None) -> None:
        """Joining or leaving a group changes how many options a select menu takes at once."""
        if group_id is None:
            return
        for row in await self.db.all("SELECT message_id FROM rr_messages WHERE guild_id = ? AND group_id = ? AND mode = 'select' AND deleted_at IS NULL", (guild_id, group_id)):
            rr = await self.get(row["message_id"])
            if rr is not None and not rr.foreign:
                try:
                    await self.redraw(rr)
                except OpsError as error:
                    log.info("Couldn't update the menu on %s: %s", rr.message_id, error)

    @reactionrole.command(name="unlink", description="Break apart the group a message is linked in")
    @requires("manage_guild")
    @app_commands.describe(message_id="Any message in the group")
    async def rr_unlink(self, ctx: commands.Context, message_id: str) -> None:
        rr = await self._tracked(ctx, message_id)
        if rr is None:
            return
        if rr.group_id is None:
            await ctx.fail("That message isn't linked to anything.")
            return
        rows = await self.db.all("SELECT message_id FROM rr_messages WHERE guild_id = ? AND group_id = ? AND mode = 'select' AND deleted_at IS NULL", (rr.guild_id, rr.group_id))
        await self.db.run("UPDATE rr_messages SET group_id = NULL WHERE guild_id = ? AND group_id = ?", (rr.guild_id, rr.group_id))
        for row in rows:
            other = await self.get(row["message_id"])
            if other is not None and not other.foreign:
                try:
                    await self.redraw(other)
                except OpsError as error:
                    log.info("Couldn't update the menu on %s: %s", other.message_id, error)
        await self._record(ctx, "reactionrole.unlink", str(rr.message_id))
        await ctx.ok("Broke up that group of linked messages.")

    @reactionrole.command(name="move", description="Move reaction roles from one message to another")
    @requires("manage_guild")
    @app_commands.describe(base_id="The message that has them now", target_id="The message that gets them", channel="Where the target is, if not here")
    async def rr_move(self, ctx: commands.Context, base_id: str, target_id: str, channel: Optional[discord.TextChannel] = None) -> None:
        await ctx.defer(ephemeral=True)
        ref = parse_message_ref(base_id)
        if ref is None:
            await ctx.fail("That needs a message ID or a link to a message.")
            return
        rr = await self.get(ref[2], trashed=True)
        if rr is None or rr.guild_id != ctx.guild.id:
            await ctx.fail("That message has no reaction roles. `/reactionrole show` lists the ones here.")
            return
        if await self._refuse_hidden(ctx, rr.channel_id):
            return
        place = await self._locate(ctx, target_id, channel)
        if place is None:
            return
        moved = await self.move_message(ctx.guild.id, rr.message_id, place[0], place[1])
        await self._record(ctx, "reactionrole.move", f"{rr.message_id} to {place[1]}")
        await ctx.ok(" ".join(["Moved them.", *(moved or {}).get("warnings", [])])[:1900])

    @reactionrole.command(name="reorder", aliases=["order"], description="Move an emoji to another place in a message's order")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", emoji="The emoji to move", place="Its new place, where 1 is first")
    async def rr_reorder(self, ctx: commands.Context, message_id: str, emoji: str, place: commands.Range[int, 1, MOST_OPTIONS]) -> None:
        """Discord shows reactions in the order each emoji first went on. Moving one clears every reaction and puts the bot's back in the new order. Members keep their roles."""
        await ctx.defer(ephemeral=True)
        rr = await self._tracked(ctx, message_id)
        if rr is None:
            return
        try:
            key = emoji_key(clean_emoji(emoji))
        except Invalid as error:
            await ctx.fail(str(error))
            return
        option = rr.by_key(key)
        if option is None:
            await ctx.fail(f"That emoji isn't on that message. `/reactionrole show {rr.message_id}` lists the ones that are.")
            return
        if place > len(rr.options):
            await ctx.fail(f"That message has {len(rr.options)} emoji, so pick a place from 1 to {len(rr.options)}.")
            return
        previous = rr.copied()
        rr.options.remove(option)
        rr.options.insert(place - 1, option)
        for slot, each in enumerate(rr.options):
            each.slot = slot
        reorder = False
        if rr.mode == "reactions":
            current = await self.bot.ops.fetch_message(rr.channel_id, rr.message_id)
            wanted = [o.key for o in rr.options]
            shown = [emoji_key(reaction["emoji"]) for reaction in current["reactions"]]
            reorder = [k for k in shown if k in wanted] != wanted
        if reorder:
            channel = ctx.guild.get_channel_or_thread(rr.channel_id)
            if channel is not None and not access.can_manage_messages(ctx.author, channel):
                await ctx.fail(REORDER_NEEDS)
                return
            unusable = self.unusable_emoji(rr)
            if unusable:
                await ctx.fail(self.unusable_refusal(unusable))
                return
            if not await self._confirm(ctx, f"This takes every reaction off {rr.url()} and puts the bot's back in the new order. Members keep their roles. Anyone who wants to drop one afterward reacts and then takes the reaction off. Go ahead?"):
                await ctx.fail("Left everything as it was.")
                return
            now = await self.get(rr.message_id)
            if now is None or rr_json(now) != rr_json(previous):
                await ctx.fail("That message's reaction roles changed while you were deciding, so nothing moved. Run the command again.")
                return
        notes = await self._save(rr, previous, reorder=reorder)
        await self._record(ctx, "reactionrole.reorder", str(rr.message_id))
        await ctx.ok(" ".join([f"{option.emoji} is number {place} on {rr.url()}.", *notes])[:1900])

    @reactionrole.command(name="aio", description="Post a message and set up its reaction roles in one command")
    @requires("manage_guild")
    @app_commands.describe(type="The type, or default for normal", color="Six hex digits, or none", text="title | description, then emoji and role pairs. {roles} in the text lists them.", channel="Where to post it, if not here")
    async def rr_aio(self, ctx: commands.Context, type: str, color: str, *, text: str, channel: Optional[discord.TextChannel] = None) -> None:
        await ctx.defer(ephemeral=True)
        mention = _CHANNEL_MENTION.fullmatch(type.strip())
        if mention:  # the text form leads with the channel and has no type
            channel = ctx.guild.get_channel_or_thread(int(mention.group(1)))
            await self._create_all_in_one(ctx, "normal", color, text, channel)
            return
        kind = "normal" if type.lower() == "default" else type.lower()
        if kind not in TYPES or kind in ("limit", "temp"):
            await ctx.fail("Pick default, unique, verify, drop, reversed or binding. Set limit and temp afterward with their own commands.")
            return
        await self._create_all_in_one(ctx, kind, color, text, channel)

    @reactionrole.command(name="aiou", with_app_command=False, description="Post a message and set up unique reaction roles in one command")
    @requires("manage_guild")
    async def rr_aiou(self, ctx: commands.Context, channel: discord.TextChannel, color: str, *, text: str) -> None:
        await self._create_all_in_one(ctx, "unique", color, text, channel)

    @reactionrole.command(name="aiov", with_app_command=False, description="Post a message and set up verify reaction roles in one command")
    @requires("manage_guild")
    async def rr_aiov(self, ctx: commands.Context, channel: discord.TextChannel, color: str, *, text: str) -> None:
        await self._create_all_in_one(ctx, "verify", color, text, channel)

    @reactionrole.command(name="aioi", with_app_command=False, description="Post a message and set up drop reaction roles in one command")
    @requires("manage_guild")
    async def rr_aioi(self, ctx: commands.Context, channel: discord.TextChannel, color: str, *, text: str) -> None:
        await self._create_all_in_one(ctx, "drop", color, text, channel)

    @reactionrole.command(name="maxroles", description="Limit how many members can hold each role on a message")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", roles_and_limits="Roles and counts, such as DPS 10 Healer 5. A count of 0 removes the limit.")
    async def rr_maxroles(self, ctx: commands.Context, message_id: str, *, roles_and_limits: Optional[str] = None) -> None:
        rr = await self._tracked(ctx, message_id)
        if rr is None:
            return
        if not roles_and_limits:
            caps = [f"<@&{role_id}> up to {count}" for option in rr.options for role_id, count in option.limits.items()]
            await ctx.send("Maximum holders: " + (", ".join(caps) or "none set") + ".", allowed_mentions=NONE)
            return
        pairs = re.findall(r"(.+?)\s+(\d{1,8})(?=\s|$)", roles_and_limits.strip())
        if not pairs:
            await ctx.fail("Write each role and then its count, such as `DPS 10 Healer 5`.")
            return
        for name, count in pairs:
            role = find_role(ctx.guild, name)
            if role is None:
                await ctx.fail(f"There's no role called `{name.strip()[:60]}`.")
                return
            holders = [option for option in rr.options if role.id in option.role_ids]
            if not holders:
                await ctx.fail(f"{role.mention} isn't on that message.")
                return
            for option in holders:
                if int(count):
                    option.limits[role.id] = int(count)
                else:
                    option.limits.pop(role.id, None)
        await self._write(rr)
        await self._record(ctx, "reactionrole.maxroles", str(rr.message_id))
        await ctx.ok("Updated the maximum holders for that message.")

    @reactionrole.command(name="mode", with_app_command=False, description="Choose reactions, buttons or a select menu for a message")
    @requires("manage_guild")
    @app_commands.describe(message_id="The message's ID or link", mode="How members pick roles")
    async def rr_mode(self, ctx: commands.Context, message_id: str, mode: Literal["reactions", "buttons", "select"]) -> None:
        await ctx.defer(ephemeral=True)
        rr = await self._tracked(ctx, message_id)
        if rr is None:
            return
        if mode == rr.mode:
            await ctx.ok(f"That message already uses {mode}.")
            return
        current = await self.bot.ops.fetch_message(rr.channel_id, rr.message_id)
        if mode != "reactions" and not current["mine"]:
            await ctx.fail("Another bot wrote that message, and Discord only lets a bot add buttons and menus to its own. Repost it from the Embeds page first.")
            return
        problem = capacity_problem(mode, len(rr.options), len(current["payload"].get("buttons") or []))
        if problem:
            await ctx.fail(problem)
            return
        if mode == "reactions" and any(not o.emoji for o in rr.options):
            await ctx.fail("Every row needs an emoji before it can use reactions. Add them on the dashboard first.")
            return
        previous = rr.copied()
        rr.mode = mode
        await self._save(rr, previous)
        await self._record(ctx, "reactionrole.mode", f"{rr.message_id}: {mode}")
        await ctx.ok(f"That message now uses {mode}.")

    @reactionrole.command(name="setup", aliases=["make"], description="Walk through making a reaction role message")
    @requires("manage_guild")
    async def rr_setup(self, ctx: commands.Context) -> None:
        if not self.bot.reads_message_text:
            # Discord blanks a reply that doesn't mention the bot, so there'd be no answers to read.
            await ctx.fail(f"Setup reads your typed answers, and this bot runs without the Message Content intent, so it can't read them. Make the message on the dashboard: {self.bot.public_url}/dashboard/{ctx.guild.id}/reactionroles")
            return
        target = await self._ask_channel(ctx)
        if target is None:
            return
        wording = await self._ask(ctx, "What should the message say? Write `title | description`. Say `cancel` any time to stop.")
        if wording is None:
            return
        color = await self._ask_color(ctx)
        if color is False:
            return
        try:
            payload = title_payload(wording, color)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        rr = RR(0, ctx.guild.id, target.id, created_at=time.time())
        if not await self._ask_pairs(ctx, rr):
            return
        if not await self._ask_kind(ctx, rr):
            return
        if not await self._ask_mode(ctx, rr):
            return
        payload = self._take_template(rr, payload)
        sent = await self.bot.ops.send_message(target.id, payload, components=self.render(rr))
        rr.message_id = sent["id"]
        await self._add_to_library(rr, payload, self.bot.actor(ctx.author))
        notes = await self._save(rr, None, fresh=True)
        await self._record(ctx, "reactionrole.create", str(rr.message_id))
        await ctx.ok(" ".join([f"Done. It's up in {target.mention}, message ID `{rr.message_id}`. Edit it any time at {self.bot.public_url}/dashboard/{ctx.guild.id}/reactionroles", *notes])[:1900])

    async def _ask(self, ctx: commands.Context, prompt: str) -> str | None:
        await ctx.send(prompt, allowed_mentions=NONE)

        def same(message: Any) -> bool:
            return message.author.id == ctx.author.id and message.channel.id == ctx.channel.id

        try:
            answer = await self.bot.wait_for("message", check=same, timeout=SETUP_TIMEOUT)
        except asyncio.TimeoutError:
            await ctx.fail("Setup timed out. Run it again when you're ready.")
            return None
        if answer.content.strip().lower() == "cancel":
            await ctx.ok("Setup canceled.")
            return None
        return answer.content.strip()

    async def _ask_channel(self, ctx: commands.Context) -> Any:
        prompt = "Which channel should the message go in? Mention it, or say `here`. Say `cancel` any time to stop."
        while True:
            answer = await self._ask(ctx, prompt)
            if answer is None:
                return None
            match = _CHANNEL_MENTION.fullmatch(answer)
            channel = ctx.channel if answer.lower() == "here" else ctx.guild.get_channel_or_thread(int(match.group(1))) if match else next((c for c in ctx.guild.text_channels if c.name == answer.lstrip("#").lower()), None)
            if channel is None or not hasattr(channel, "send"):
                prompt = "That isn't a channel here. Mention one, or say `here`."
                continue
            refusal = access.post_refusal(ctx.author, channel)
            if refusal is None:
                return channel
            prompt = f"{refusal} Mention another one, or say `here`."

    async def _ask_color(self, ctx: commands.Context) -> Any:
        prompt = "What accent color? Six hex digits such as `FF0000`, or `none`."
        while True:
            answer = await self._ask(ctx, prompt)
            if answer is None:
                return False
            if answer.lower() in ("none", "skip", "no"):
                return None
            try:
                return hex_color(answer)
            except Invalid as error:
                prompt = str(error)

    async def _ask_pairs(self, ctx: commands.Context, rr: RR) -> bool:
        prompt = "Now the roles. Send an emoji and a role, one pair per line or one pair per message, like `\N{WAVING HAND SIGN} @Role`. Say `done` when you have them all."
        while True:
            answer = await self._ask(ctx, prompt)
            if answer is None:
                return False
            if answer.lower() == "done":
                if rr.options:
                    return True
                prompt = "Add at least one emoji and role first."
                continue
            pairs, problem = parse_pairs(ctx.guild, answer)
            if problem or not pairs:
                prompt = (problem or "Send an emoji and then a role.") + " Try again, or say `done`."
                continue
            problem = None
            risks = []
            for emoji_text, role in pairs:
                problem = self._role_refusal(ctx, role)
                if problem:
                    break
                risk = self._risk_note(ctx.guild, role)
                if risk:
                    risks.append(risk)
                emoji = clean_emoji(emoji_text)
                key = emoji_key(emoji)
                option = rr.by_key(key)
                if option is None:
                    rr.options.append(Option(len(rr.options), emoji, key, [role.id]))
                elif role.id not in option.role_ids:
                    option.role_ids.append(role.id)
            if problem:
                prompt = problem + " Try another role, or say `done`."
                continue
            if len(rr.options) >= MAX_REACTIONS:
                await ctx.send(f"That's {MAX_REACTIONS} emoji, the most Discord allows on a message as reactions.", allowed_mentions=NONE)
                return True
            prompt = " ".join([*risks, f"Got it, {len(rr.options)} so far. Send more, or say `done`."])

    async def _ask_kind(self, ctx: commands.Context, rr: RR) -> bool:
        prompt = "Which type? `normal` is the usual. The others are `unique`, `verify`, `drop`, `reversed`, `binding`, `limit 3` and `temp 1h`."
        while True:
            answer = await self._ask(ctx, prompt)
            if answer is None:
                return False
            word, _, value = answer.lower().partition(" ")
            if word == "limit":
                try:
                    rr.kind, rr.limit = "limit", integer(1, MAX_LIMIT)(value.strip())
                    return True
                except Invalid:
                    prompt = f"Write `limit` and a number from 1 to {MAX_LIMIT}."
                    continue
            if word == "temp":
                seconds = parse_duration(value)
                if seconds is not None and MIN_TEMP <= seconds <= MAX_TEMP:
                    rr.kind, rr.temp_seconds = "temp", seconds
                    return True
                prompt = "Write `temp` and a time, such as `temp 1h`."
                continue
            if word in TYPES:
                rr.kind = word
                return True
            prompt = "Pick one of `normal`, `unique`, `verify`, `drop`, `reversed`, `binding`, `limit 3` or `temp 1h`."

    async def _ask_mode(self, ctx: commands.Context, rr: RR) -> bool:
        prompt = "How should members pick? `reactions`, `buttons` or `select`."
        while True:
            answer = await self._ask(ctx, prompt)
            if answer is None:
                return False
            if answer.lower() in MODES:
                rr.mode = answer.lower()
                return True
            prompt = "Pick `reactions`, `buttons` or `select`."

    @rr_add.autocomplete("role")
    @rr_remove.autocomplete("role")
    async def _role_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        needle = current.casefold()
        rows = [r for r in reversed(guild.roles) if not r.is_default() and not r.managed and needle in r.name.casefold()]
        return [app_commands.Choice(name=r.name[:100], value=str(r.id)) for r in rows[:25]]

    @rr_reorder.autocomplete("emoji")
    async def _emoji_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """The emoji on the message picked in the same command, each with the roles it hands out."""
        guild = interaction.guild
        ref = parse_message_ref(str(getattr(interaction.namespace, "message_id", "") or ""))
        rr = await self.get(ref[2]) if guild is not None and ref is not None else None
        if rr is None or rr.guild_id != guild.id or not self._shows_to(interaction.user, guild, rr.channel_id):
            return []
        needle = current.casefold()
        out = []
        for option in rr.options:
            custom = _CUSTOM_EMOJI.fullmatch(option.emoji)
            roles = ", ".join(role.name for role in (guild.get_role(i) for i in option.role_ids) if role)
            name = f"{f':{custom.group(2)}:' if custom else option.emoji} {roles}"[:100]
            if needle in name.casefold() or needle in option.emoji:
                out.append(app_commands.Choice(name=name, value=option.emoji))
        return out[:25]

    @rr_show.autocomplete("message_id")
    @rr_remove.autocomplete("message_id")
    @rr_purge.autocomplete("message_id")
    @rr_blacklist.autocomplete("message_id")
    @rr_whitelist.autocomplete("message_id")
    @rr_selfdestruct.autocomplete("message_id")
    @rr_edit.autocomplete("message_id")
    @rr_fix.autocomplete("message_id")
    @rr_modify.autocomplete("message_id")
    @rr_reorder.autocomplete("message_id")
    @rr_temp.autocomplete("message_id")
    @rr_limit.autocomplete("message_id")
    @rr_unlink.autocomplete("message_id")
    @rr_maxroles.autocomplete("message_id")
    @rr_normal.autocomplete("message_id")
    async def _message_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        needle = current.casefold()
        out = []
        for rr in await self.for_guild(guild.id):
            if not self._shows_to(interaction.user, guild, rr.channel_id):
                continue
            channel = guild.get_channel_or_thread(rr.channel_id)
            roles = ", ".join(role.name for role in (guild.get_role(i) for i in sorted(rr.role_ids)) if role)
            name = f"#{channel.name if channel else 'unknown'}: {roles or 'no roles'} ({rr.message_id})"[:100]
            if needle in name.casefold():
                out.append(app_commands.Choice(name=name, value=str(rr.message_id)))
            if len(out) >= 25:
                break
        return out

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/reactionroles", self._api_list)
        router.post("/reactionroles", self._api_create)
        router.get("/reactionroles/lookup", self._api_lookup)
        router.post("/reactionroles/import", self._api_import)
        router.get("/reactionroles/{message_id:\\d+}/message", self._api_message)
        router.patch("/reactionroles/{message_id:\\d+}", self._api_update)
        router.delete("/reactionroles/{message_id:\\d+}", self._api_delete)
        router.post("/reactionroles/{message_id:\\d+}/fix", self._api_fix)
        router.post("/reactionroles/{message_id:\\d+}/move", self._api_move)

    def _rules(self, req: Any) -> dict:
        return {"elevated_roles": bool(req.settings.get(req.guild_id, COMMANDS_MODULE)["elevated_roles"]), "dangerous": list(DANGEROUS_PERMISSIONS)}

    def _guild_info(self, req: Any) -> dict:
        info = req.ops.describe_guild(req.guild_id)
        if info is None:
            raise ApiError(404, "The bot isn't in that server.")
        return info

    async def _owned(self, req: Any) -> RR:
        rr = await self.get(req.int_param("message_id"))
        if rr is None or rr.guild_id != req.guild_id:
            raise ApiError(404, "That message has no reaction roles.")
        if not req.can_see(rr.channel_id):
            raise ApiError(403, "You can't see the channel that message is in.")
        return rr

    def _top_position(self, req: Any, info: dict) -> int | None:
        """How high the person asking ranks, or None when nothing is out of their reach."""
        if info.get("owner_id") == req.user.id:
            return None
        member = req.ops.member(req.guild_id, req.user.id)
        if member is None:
            return None
        roles = {role["id"]: role for role in info["roles"]}
        held = [roles[i] for i in member.get("role_ids", []) if i in roles]
        if any("administrator" in role["permissions"] for role in held):
            return None
        return max((role["position"] for role in held), default=0)

    def _check_actor(self, req: Any, info: dict, options: list[Option], *, already: set[int] = frozenset()) -> None:
        """Someone with Manage Server who ranks below a role can't add it to a message, which would be a way to hand it to themselves. Roles already there can stay, so they can still edit the message."""
        top = self._top_position(req, info)
        if top is None:
            return
        roles = {role["id"]: role for role in info["roles"]}
        for index, option in enumerate(options):
            for role_id in option.role_ids:
                role = roles.get(role_id)
                if role is not None and role_id not in already and role["position"] >= top:
                    raise ValidationError({f"rows.{index}.roles": f"{role['name']} sits at or above your highest role, so you can't set it up to be handed out."})

    def _check_opening(self, req: Any, info: dict, rr: RR, field: str) -> None:
        """Taking a gate off a message is another way to reach its roles, so it takes the same rank as adding them."""
        top = self._top_position(req, info)
        if top is None:
            return
        for role in info["roles"]:
            if role["id"] in rr.role_ids and role["position"] >= top:
                raise ValidationError({field: f"{role['name']} sits at or above your highest role, so you can't open this message up to more people."})

    def _destruct_from(self, req: Any, channel_id: int, form: dict, current: float | None) -> float | None:
        if "destruct_seconds" not in form:
            return current
        seconds = form["destruct_seconds"]
        if seconds in (None, "", 0):
            return None
        if not req.can_manage_messages(channel_id):
            raise ValidationError({"destruct_seconds": NEEDS_MANAGE_MESSAGES})
        try:
            return time.time() + integer(60, MAX_DESTRUCT)(seconds)
        except Invalid as error:
            raise ValidationError({"destruct_seconds": "Pick a time from one minute to one year."}) from error

    async def _read_links(self, req: Any, rr: RR, form: dict) -> list[RR] | None:
        """The other messages the form's `linked` list names, or None when it doesn't say. This runs before anything is posted or saved, so a bad list changes nothing."""
        if "linked" not in form:
            return None
        try:
            ids = snowflakes()(form["linked"] or [])
        except Invalid as error:
            raise ValidationError({"linked": str(error)}) from error
        others = []
        for message_id in ids:
            other = await self.get(message_id)
            if other is None or other.guild_id != req.guild_id:
                raise ValidationError({"linked": "One of those messages has no reaction roles here."})
            if other.message_id != rr.message_id:
                others.append(other)
        return others

    async def _apply_links(self, req: Any, rr: RR, others: list[RR] | None) -> None:
        """`others` share a group with this message from now on. Someone else's group comes along whole, so a link never splits one, and only this message's own group can lose a member here."""
        if others is None:
            return
        old_group = rr.group_id
        old_members = []
        if old_group is not None:
            old_members = [row["message_id"] for row in await self.db.all("SELECT message_id FROM rr_messages WHERE guild_id = ? AND group_id = ? AND message_id != ?", (req.guild_id, old_group, rr.message_id))]
        keep = {o.message_id for o in others}
        dropped = [message_id for message_id in old_members if message_id not in keep]
        if others:
            for message_id in dropped:
                await self.db.run("UPDATE rr_messages SET group_id = NULL WHERE message_id = ?", (message_id,))
            await self._link_group(req.guild_id, [rr, *others])
            current = await self.get(rr.message_id)
            rr.group_id = current.group_id if current else rr.group_id
        elif old_group is not None:
            rr.group_id = None
            await self.db.run("UPDATE rr_messages SET group_id = NULL WHERE message_id = ?", (rr.message_id,))
            await self._tidy_group(req.guild_id, old_group)
            dropped = [rr.message_id, *old_members]
        # A menu takes one pick in a group and many outside one, so whoever left has to be drawn again.
        for message_id in dropped:
            other = await self.get(message_id)
            if other is not None and other.mode == "select" and not other.foreign:
                try:
                    await self.redraw(other)
                except OpsError as error:
                    log.info("Couldn't update the menu on %s: %s", other.message_id, error)

    async def _api_list(self, req: Any) -> dict:
        return {"rr": [rr_json(rr) for rr in await self.for_guild(req.guild_id)], **self._rules(req)}

    async def _api_lookup(self, req: Any) -> dict:
        channel_id = req.channel(req.query.get("channel_id"))
        if not req.can_read(channel_id):
            raise ApiError(403, "You can't read the messages in that channel.")
        if req.query.get("latest"):
            found = (await req.ops.channel_messages(req.guild_id, channel_id, limit=1))["messages"]
            if not found:
                raise ApiError(404, "That channel has no messages yet.")
            current = found[0]
        else:
            current = await req.ops.fetch_message(channel_id, snowflake(req.query.get("message_id")))
        return {
            "message_id": current["id"], "channel_id": channel_id, "mine": bool(current["mine"]), "author_name": current["author_name"], "payload": current["payload"],
            "url": current["url"], "tracked": await self.holds_message(req.guild_id, current["id"]), "reactions": current["reactions"],
        }

    async def _api_message(self, req: Any) -> dict:
        rr = await self._owned(req)
        if not req.can_read(rr.channel_id):
            return {"readonly": True, "error": "You can't read the messages in that channel.", "payload": None, "url": rr.url(), "author_name": None}
        try:
            current = await req.ops.fetch_message(rr.channel_id, rr.message_id)
        except OpsError as error:
            return {"readonly": True, "error": str(error), "payload": None, "url": rr.url(), "author_name": None}
        if rr.foreign == bool(current["mine"]):
            await self.db.run("UPDATE rr_messages SET foreign_author = ? WHERE message_id = ?", (int(not current["mine"]), rr.message_id))
        return {
            "readonly": not current["mine"], "error": None, "payload": rr.template or current["payload"], "url": current["url"], "author_name": current["author_name"],
            # The order Discord shows the reactions in, which is the order each emoji first went on.
            "reactions": [emoji_key(reaction["emoji"]) for reaction in current["reactions"]],
        }

    async def _api_create(self, req: Any) -> dict:
        form = await req.json()
        if not isinstance(form, dict):
            raise ValidationError({"form": "Expected a set of fields."})
        info = self._guild_info(req)
        how = form.get("create_mode", "post")
        if how not in ("post", "id", "latest"):
            raise ValidationError({"create_mode": "Pick one of the listed modes."})
        channel_id = req.channel(form.get("channel_id"), posting=how == "post")
        payload = None
        message_id = None
        foreign = False
        link_buttons = 0
        if how == "post":
            payload = messages.clean_payload(form.get("payload") or {})
            if not messages.has_content(payload):
                raise ValidationError({"payload": "Write some text or an embed for the message."})
            link_buttons = len(payload["buttons"])
        else:
            if how == "latest":
                found = (await req.ops.channel_messages(req.guild_id, channel_id, limit=1))["messages"]
                if not found:
                    raise ValidationError({"channel_id": "That channel has no messages yet."})
                current = found[0]
            else:
                try:
                    wanted = snowflake(form.get("message_id"))
                except Invalid as error:
                    raise ValidationError({"message_id": "Enter the message's ID."}) from error
                current = await req.ops.fetch_message(channel_id, wanted)
            message_id = current["id"]
            foreign = not current["mine"]
            link_buttons = len(current["payload"].get("buttons") or [])
            if await self.get(message_id, trashed=True) is not None:
                raise ApiError(409, "That message already has reaction roles. Edit it instead.")
        settings = clean_settings(form, info, foreign=foreign, link_buttons=link_buttons)
        self._check_actor(req, info, settings["options"])
        rr = RR(message_id or 0, req.guild_id, channel_id, foreign=foreign, created_at=time.time(), **settings)
        others = await self._read_links(req, rr, form)
        if how != "post" and not foreign and holds_roles_block(current["payload"]):
            rr.template = current["payload"]
        rr.destruct_at = self._destruct_from(req, channel_id, form, None)
        if how == "post":
            payload = self._take_template(rr, payload)
            sent = await req.ops.send_message(channel_id, payload, components=self.render(rr))
            rr.message_id = sent["id"]
            await self._add_to_library(rr, payload, req.actor)
            warnings = await self._save(rr, None, fresh=True)
        else:
            warnings = await self._save(rr, None)
        await self._apply_links(req, rr, others)
        await req.record("reactionrole.create", detail=str(rr.message_id))
        return {**rr_json(await self.get(rr.message_id) or rr), "warnings": warnings}

    async def _api_update(self, req: Any) -> dict:
        rr = await self._owned(req)
        form = await req.json()
        if not isinstance(form, dict):
            raise ValidationError({"form": "Expected a set of fields."})
        info = self._guild_info(req)
        current = await req.ops.fetch_message(rr.channel_id, rr.message_id)
        foreign = not current["mine"]
        payload = None
        if "payload" in form and form["payload"] is not None:
            if foreign:
                raise ApiError(400, "Another bot wrote this message, and Discord only lets a bot edit its own. Repost it from the Embeds page first.")
            payload = messages.clean_payload(form["payload"])
            if not messages.has_content(payload):
                raise ValidationError({"payload": "Write some text or an embed for the message."})
            if payload == (rr.template or current["payload"]):
                payload = None  # the form sends the text back whether or not it changed
            elif not req.can_post(rr.channel_id):
                raise ValidationError({"payload": "You can't post in that channel."})
        link_buttons = len((payload or current["payload"]).get("buttons") or [])
        settings = clean_settings(form, info, foreign=foreign, link_buttons=link_buttons)
        self._check_actor(req, info, settings["options"], already=rr.role_ids)
        field = opens_up(rr, settings["whitelist"], settings["blacklist"], settings["kind"])
        if field:
            self._check_opening(req, info, rr, field)
        reorder = bool(form.get("reorder")) and settings["mode"] == "reactions"
        if reorder and not req.can_manage_messages(rr.channel_id):
            # Clearing reactions takes everyone's off, which Discord lets only those with Manage Messages do.
            raise ValidationError({"reorder": REORDER_NEEDS})
        others = await self._read_links(req, rr, form)
        previous = rr.copied()
        previous.foreign = rr.foreign = foreign
        for name, value in settings.items():
            setattr(rr, name, value)
        if reorder:
            unusable = self.unusable_emoji(rr)
            if unusable:
                raise ValidationError({"reorder": self.unusable_refusal(unusable)})
        rr.destruct_at = self._destruct_from(req, rr.channel_id, form, rr.destruct_at)
        edited = self._take_template(rr, payload)
        warnings = await self._save(rr, previous, payload=edited, reorder=reorder)
        if edited is not None:
            await self._add_to_library(rr, edited, req.actor, note="Edited")
        await self._apply_links(req, rr, others)
        await req.record("reactionrole.update", detail=str(rr.message_id))
        if reorder and not any(warning.startswith(OLD_ORDER) for warning in warnings):
            await req.record("reactionrole.reorder", detail=str(rr.message_id))
        return {**rr_json(await self.get(rr.message_id) or rr), "warnings": warnings}

    async def _api_delete(self, req: Any) -> dict:
        rr = await self._owned(req)
        await self.remove(rr)
        await req.record("reactionrole.delete", detail=str(rr.message_id))
        return {"ok": True}

    async def _api_fix(self, req: Any) -> dict:
        rr = await self._owned(req)
        warnings = await self.redraw(rr)
        self._sync_soon(rr.message_id)
        await req.record("reactionrole.fix", detail=str(rr.message_id))
        return {"warnings": warnings}

    async def _api_move(self, req: Any) -> dict:
        rr = await self._owned(req)
        form = await req.json()
        channel_id = req.channel(form.get("channel_id"))
        try:
            target = snowflake(form.get("message_id"))
        except Invalid as error:
            raise ValidationError({"message_id": "Enter the message's ID."}) from error
        moved = await self.move_message(req.guild_id, rr.message_id, channel_id, target)
        await req.record("reactionrole.move", detail=f"{rr.message_id} to {target}")
        return {"message_id": target, "warnings": (moved or {}).get("warnings", [])}

    async def _api_import(self, req: Any) -> dict:
        info = self._guild_info(req)
        if self._top_position(req, info) is not None:
            raise ApiError(403, "Only the server owner or an administrator can import reaction roles, since an import can hand out any role.")
        form = await req.json()
        text = form.get("text") if isinstance(form, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ValidationError({"text": "Paste the export first."})
        try:
            data = json.loads(text)
        except ValueError as error:
            raise ValidationError({"text": "That isn't valid JSON."}) from error
        result = await self.import_rows(req.guild_id, data)
        await req.record("reactionrole.import", detail=f"{result['imported']} messages")
        return result

    # ---- bringing reaction roles over from an export ----

    async def import_rows(self, guild_id: int, data: Any) -> dict:
        """Reaction roles in the shape another bot's dashboard exports: {"rr": [{"message_id", "extra": {...}, "messages": [{"channel_id", "reaction_emoji", "role_id"}]}]}.

        The messages keep working where they are. Reactions already on them go on record without changing anyone's roles.
        """
        entries = data.get("rr") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise Invalid("That isn't a reaction role export.")
        info = self.bot.ops.describe_guild(guild_id)
        if info is None:
            raise Invalid("The bot isn't in that server.")
        roles = {row["id"] for row in info["roles"] if not row["default"] and not row["managed"]}
        emojis = {str(e["id"]): f"<{'a' if e.get('animated') else ''}:{e['name']}:{e['id']}>" for e in info.get("emojis", [])}
        imported = pairs = 0
        skipped: list[dict] = []
        warnings: list[dict] = []
        for entry in entries:
            result = await self._import_one(guild_id, entry, roles, emojis, warnings)
            if isinstance(result, int):
                imported += 1
                pairs += result
            else:
                skipped.append(result)
        return {"imported": imported, "pairs": pairs, "skipped": skipped, "warnings": warnings}

    async def _import_one(self, guild_id: int, entry: Any, roles: set[int], emojis: dict[str, str], warnings: list[dict]) -> int | dict:
        def skip(reason: str) -> dict:
            return {"message_id": str(entry.get("message_id", "?")) if isinstance(entry, dict) else "?", "reason": reason}

        if not isinstance(entry, dict) or not isinstance(entry.get("messages"), list) or not entry["messages"]:
            return skip("It has no emoji and roles.")
        try:
            message_id = snowflake(entry.get("message_id"))
            channel_id = snowflake(entry["messages"][0].get("channel_id") if isinstance(entry["messages"][0], dict) else None)
        except Invalid:
            return skip("It has no usable message or channel ID.")
        if await self.get(message_id, trashed=True) is not None:
            return skip("That message already has reaction roles here.")
        if self.bot.ops.channel(guild_id, channel_id) is None:
            return skip("Its channel isn't in this server.")
        try:
            current = await self.bot.ops.fetch_message(channel_id, message_id)
        except OpsError as error:
            return skip(str(error))
        known = dict(emojis)
        known.update({emoji_key(r["emoji"]): r["emoji"] for r in current["reactions"]})
        extra = entry.get("extra") if isinstance(entry.get("extra"), dict) else {}
        kind_text = str(extra.get("type") or "normal").split()
        kind = kind_text[0] if kind_text and kind_text[0] in TYPES else "normal"
        amount = (whole_number(kind_text[1]) or 0) if len(kind_text) > 1 else 0
        if kind == "limit" and not 1 <= amount <= MAX_LIMIT:
            return skip("Its limit isn't usable.")
        if kind == "temp" and not MIN_TEMP <= amount <= MAX_TEMP:
            return skip("Its time isn't usable.")

        def id_list(name: str) -> list[int]:
            out = []
            values = extra.get(name)
            for value in values if isinstance(values, list) else []:
                try:
                    out.append(snowflake(value))
                except Invalid:
                    pass
            return out

        rr = RR(message_id, guild_id, channel_id, kind=kind, limit=amount if kind == "limit" else 0, temp_seconds=amount if kind == "temp" else 0,
                whitelist=id_list("whitelist"), blacklist=id_list("blacklist"), foreign=not current["mine"], created_at=time.time())
        group = extra.get("group_id")
        if group not in (None, ""):
            try:
                rr.group_id = snowflake(group)
            except Invalid:
                rr.group_id = None
        caps = _read_caps(extra.get("extra"))
        count = 0
        for row in entry["messages"]:
            try:
                role_id = snowflake(row.get("role_id") if isinstance(row, dict) else None)
            except Invalid:
                continue
            if role_id not in roles:
                continue
            raw = str(row.get("reaction_emoji") or "")
            if _BARE_ID.fullmatch(raw):
                emoji = known.get(raw) or f"<:emoji:{raw}>"
            else:
                try:
                    emoji = clean_emoji(raw)
                except Invalid:
                    continue
            key = emoji_key(emoji)
            option = rr.by_key(key)
            if option is None:
                option = Option(len(rr.options), emoji, key)
                rr.options.append(option)
            if role_id not in option.role_ids:
                option.role_ids.append(role_id)
                count += 1
            if role_id in caps:
                option.limits[role_id] = caps[role_id]
        if not rr.options:
            return skip("None of its roles exist in this server.")
        try:
            problems = await self._save(rr, None)
        except (OpsError, Invalid) as error:
            return skip(str(error))
        warnings.extend({"message_id": str(message_id), "reason": problem} for problem in problems)
        return count


def _read_caps(value: Any) -> dict[int, int]:
    """Per-role maximums from the export's free-form field, when it holds any: {"role id": count}."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, count in value.items():
        if str(key).isdigit() and isinstance(count, int) and count > 0:
            out[int(key)] = count
    return out
