"""Custom commands: stored messages a server names and members run by prefix or with /tag get."""
from __future__ import annotations

import datetime
import email.utils
import io
import json
import logging
import re
import secrets
import time
from collections import deque
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import who_from
from ..core import messages
from ..core.members import find_member
from ..core.module import Module, requires
from ..core.outside import Outside, OutsideError
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import Decision
from ..core.shortcuts import SHORTCUTS
from ..core.tagscript import TEMPLATE_MAX, LimitReached, OverBudget, Scope, budget, budgeted_render, clean_template_message, ids_from, read_json_text, render_strict, run_tag
from ..core.tagscript.preview import describe_actions, preview_scope
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, snowflakes, whole_number
from ..web.api import ApiError, Req

log = logging.getLogger(__name__)

NAME_MAX = 50
TAG_LIMIT = 10_000
MEMBERS_SHARE = 0.8             # members stop here, so accounts making tags can't leave staff unable to make one
MEMBER_TAG_LIMIT = 100          # what one member without moderator rights can own, so nobody can fill the server's tags from a slash command
CREATE_RATE = 10                # new tags and aliases one member can make in a window, since each can hold 25,000 characters
CREATE_WINDOW = 60.0
IMPORT_LIMIT = 2_000
EXPORT_VERSION = 1
PASTEBIN_TIMEOUT = 10
BURST_USES = 3
BURST_SECONDS = 5.0
RAW_INLINE_MAX = 1900
SUMMARY_MAX = 300               # characters of a tag's text the table shows
PAGE_SIZE = 25
PAGE_SIZE_MAX = 100
WAITING_MAX = 200               # tags the trust list shows at once
MAX_TIMESTAMP = 32_503_680_000  # the year 3000. A date past this in a file is a mistake, and Discord can't show it.

_PASTEBIN = re.compile(r"https?://(?:www\.)?pastebin\.com/(?:raw/)?([A-Za-z0-9]+)")
_MEMBER_MENTION = re.compile(r"<@!?(\d{15,21})>")


def clean_name(raw: Any) -> str:
    """A tag's name: what follows the prefix, so one word with no spaces, and case doesn't count."""
    if not isinstance(raw, str):
        raise Invalid("A tag needs a name.")
    name = raw.strip().lower()
    if not name:
        raise Invalid("A tag needs a name.")
    if len(name) > NAME_MAX:
        raise Invalid(f"A tag's name can be at most {NAME_MAX} characters.")
    if re.search(r"\s", name):
        raise Invalid("A tag's name can't hold spaces.")
    if "@everyone" in name or "@here" in name or name.startswith("<"):
        raise Invalid("That can't be a tag's name.")
    if name == "get":  # `/tag get` reads its first word as the name, so a tag called get could never receive its text
        raise Invalid("`get` is part of the tag command, so a tag can't take that name.")
    return name


def _message_form(value: Any) -> dict:
    clean = clean_template_message(value)
    if not messages.has_content(clean):
        raise Invalid("Write some text or fill in an embed first.")
    return clean


TAG_FORM = Schema({
    "name": Field("", clean_name),
    "message": Field(messages.empty_payload(), _message_form),
    "restricted": Field(False, boolean),
    "nsfw": Field(False, boolean),
    "channel_wl": Field([], snowflakes()),
    "channel_bl": Field([], snowflakes()),
    "role_wl": Field([], snowflakes()),
    "role_bl": Field([], snowflakes()),
    "trusted": Field(False, boolean),
})

_LIST_FIELDS = ("channel_wl", "channel_bl", "role_wl", "role_bl")
TRUST_REFUSED = "Only the server owner or an Administrator can trust a tag."
TOO_MANY_NEW = "You're making tags and aliases too fast. Wait a minute."


def _decode(row: dict) -> dict:
    """A row with its JSON columns read, and `message` holding the whole stored message."""
    extra = json.loads(row.pop("extra") or "{}")
    row["message"] = {"content": row["content"], "embeds": extra.get("embeds", []), "buttons": extra.get("buttons", [])}
    for field in _LIST_FIELDS:
        row[field] = json.loads(row[field] or "[]")
    row["nsfw"], row["restricted"], row["trusted"] = bool(row["nsfw"]), bool(row["restricted"]), bool(row["trusted"])
    return row


def _extra(message: dict) -> str:
    return json.dumps({"embeds": message.get("embeds", []), "buttons": message.get("buttons", [])})


def _portable(tag: dict, aliases: list[str]) -> dict:
    """A tag as an export file holds it."""
    return {
        "name": tag["name"], "content": tag["message"]["content"], "embeds": tag["message"]["embeds"], "buttons": tag["message"]["buttons"], "nsfw": tag["nsfw"],
        "restricted": tag["restricted"], "channel_wl": tag["channel_wl"], "channel_bl": tag["channel_bl"], "role_wl": tag["role_wl"], "role_bl": tag["role_bl"],
        "uses": tag["uses"], "owner_id": tag["owner_id"], "created_at": tag["created_at"], "aliases": aliases,
    }


def _when_from(value: Any) -> float | None:
    """A date in an imported file: seconds, or the text form other dashboards send. None for anything out of range, which Discord couldn't show."""
    moment: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = float(value)
    elif isinstance(value, str):
        for read in (email.utils.parsedate_to_datetime, lambda text: datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))):
            try:
                moment = read(value).timestamp()
                break
            except (TypeError, ValueError, OverflowError, OSError):  # a date near the ends of the calendar raises the last two
                continue
    return moment if moment is not None and 0 < moment < MAX_TIMESTAMP else None


def _unsigned_links(embeds: list[dict]) -> list[dict]:
    """Discord's signed file links expire. Stored bare, they renew when they're sent. A link that can't be read stays as it is, for the check after this to refuse."""
    out = []
    for embed in embeds:
        if not isinstance(embed, dict):
            out.append(embed)  # the check after this refuses it with a sentence for the person
            continue
        copy = dict(embed)
        for key in ("image", "thumbnail"):
            url = copy[key].get("url") if isinstance(copy.get(key), dict) else None
            if isinstance(url, str) and url:
                try:
                    copy[key] = {**copy[key], "url": messages.unsigned(url)}
                except ValueError:
                    pass
        out.append(copy)
    return out


def imported_tag(item: Any) -> dict:
    """One tag from a file, in our export form or the shape other dashboards hand out, with `embed` as JSON text. Raises Invalid with a sentence."""
    if not isinstance(item, dict):
        raise Invalid("Each tag must be a set of fields.")
    name = clean_name(item.get("name"))
    embeds = item.get("embeds")
    if embeds is None:
        try:
            legacy = read_json_text(item.get("embed") or "{}")
        except ValueError as error:
            raise Invalid(f"The embed of {name} isn't valid JSON.") from error
        embeds = [legacy] if isinstance(legacy, dict) and legacy else []
    if not isinstance(embeds, list):
        raise Invalid(f"The embeds of {name} must be a list.")
    try:
        message = _message_form({"content": item.get("content") or "", "embeds": _unsigned_links(embeds), "buttons": item.get("buttons") or []})
    except Invalid as error:
        raise Invalid(f"{name}: {error}") from error
    aliases = []
    listed = item.get("aliases")
    for alias in listed if isinstance(listed, list) else []:
        try:
            aliases.append(clean_name(alias))
        except Invalid:
            continue
    uses = item.get("uses")
    owner = ids_from(item.get("owner_id"))
    return {
        "name": name, "message": message, "nsfw": item.get("nsfw") is True, "restricted": item.get("restricted") is True,
        **{field: ids_from(item.get(field)) for field in _LIST_FIELDS},
        "uses": int(uses) if isinstance(uses, (int, float)) and not isinstance(uses, bool) and 0 < uses < 2 ** 53 else 0,
        "owner_id": owner[0] if owner else None, "created_at": _when_from(item.get("created_at")), "aliases": aliases,
    }


class ReplaceTagView(discord.ui.View):
    """The offer under a refusal to make a tag that exists: put the new text in the old one. A press goes through the same checks as an edit, since time has passed."""

    def __init__(self, cog: "Tags", ctx: Any, name: str, content: str) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.name = name
        self.content = content

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message("Only the person who ran this command can use this.", ephemeral=True)
        return False

    @discord.ui.button(label="Replace it", style=discord.ButtonStyle.primary)
    async def replace(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)
        tag = await self.cog.find(self.ctx.guild.id, self.name)
        if tag is None:
            await self.ctx.fail("That tag no longer exists.")
            return
        await self.cog._set_content(self.ctx, tag, self.content, action="tag.edit", done=f"Replaced the tag `{tag['name']}`.")


class TagForm(discord.ui.Modal):
    """/tag prompt: a slash option can't hold line breaks, and this form can."""

    def __init__(self, cog: "Tags") -> None:
        super().__init__(title="Create a tag")
        self.cog = cog
        self.tag_name = discord.ui.TextInput(label="Name", max_length=NAME_MAX, placeholder="One word, no spaces")
        self.content = discord.ui.TextInput(label="Content", style=discord.TextStyle.long, max_length=4000)
        self.add_item(self.tag_name)
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        problem = await self.cog.create(interaction.guild, interaction.user, self.tag_name.value, self.content.value)
        text = f"\N{CROSS MARK} {problem}" if problem else "\N{WHITE HEAVY CHECK MARK} Tag created."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class Tags(Module):
    key = "tags"
    row_limits = {"tags": TAG_LIMIT, "tag_aliases": TAG_LIMIT}
    child_links = {"tag_aliases": {"tag_id": "tags.id"}, "tag_shares": {"tag_id": "tags.id"}}
    category = "Tags & Triggers"
    schema = Schema({
        # On for a new server, because with it off any member could rewrite a tag staff rely on.
        "ownership": Field(True, boolean),
        "mod_only": Field(False, boolean),
    })
    migrations = [
        """
        CREATE TABLE tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            content TEXT NOT NULL,
            extra TEXT NOT NULL DEFAULT '{}',
            nsfw INTEGER NOT NULL DEFAULT 0,
            restricted INTEGER NOT NULL DEFAULT 0,
            channel_wl TEXT NOT NULL DEFAULT '[]',
            channel_bl TEXT NOT NULL DEFAULT '[]',
            role_wl TEXT NOT NULL DEFAULT '[]',
            role_bl TEXT NOT NULL DEFAULT '[]',
            uses INTEGER NOT NULL DEFAULT 0,
            owner_id INTEGER,
            trusted INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE (guild_id, name)
        );
        CREATE INDEX tags_owner ON tags (guild_id, owner_id);
        CREATE TABLE tag_aliases (
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            tag_id INTEGER NOT NULL,
            owner_id INTEGER,
            created_at REAL NOT NULL,
            PRIMARY KEY (guild_id, name)
        );
        CREATE INDEX tag_aliases_tag ON tag_aliases (guild_id, tag_id);
        CREATE TABLE tag_shares (
            guild_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            code TEXT NOT NULL UNIQUE,
            created_by INTEGER,
            created_at REAL NOT NULL,
            PRIMARY KEY (guild_id, tag_id)
        );
        """,
        # A flag stored before this step was set by any save from the dashboard, so it says nobody vouched for anything.
        "UPDATE tags SET trusted = 0;",
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.outside = Outside()
        self._recent: dict[tuple[int, int], deque[float]] = {}
        self._created: dict[tuple[int, int], deque[float]] = {}

    async def on_load(self) -> None:
        self.bot.unknown_command_handlers.append(self._on_unknown_command)

    async def cog_unload(self) -> None:
        if self._on_unknown_command in self.bot.unknown_command_handlers:
            self.bot.unknown_command_handlers.remove(self._on_unknown_command)
        await self.outside.close()

    # ---- reading ----

    async def find(self, guild_id: int, name: str) -> dict | None:
        """A tag by its name or an alias. Case doesn't count."""
        lookup = name.strip().lower()
        row = await self.db.one("SELECT * FROM tags WHERE guild_id = ? AND name = ?", (guild_id, lookup))
        if row is None:
            alias = await self.db.one("SELECT tag_id FROM tag_aliases WHERE guild_id = ? AND name = ?", (guild_id, lookup))
            if alias is None:
                return None
            row = await self.db.one("SELECT * FROM tags WHERE guild_id = ? AND id = ?", (guild_id, alias["tag_id"]))
            if row is None:
                return None
        return _decode(row)

    async def all_names(self, guild_id: int) -> set[str]:
        """Every tag name and alias in a server, in the lower case they're stored in."""
        rows = await self.db.all("SELECT name FROM tags WHERE guild_id = ? UNION SELECT name FROM tag_aliases WHERE guild_id = ?", (guild_id, guild_id))
        return {row["name"] for row in rows}

    async def _by_id(self, guild_id: int, tag_id: int) -> dict | None:
        row = await self.db.one("SELECT * FROM tags WHERE guild_id = ? AND id = ?", (guild_id, tag_id))
        return _decode(row) if row else None

    async def _aliases(self, guild_id: int, tag_id: int) -> list[str]:
        rows = await self.db.all("SELECT name FROM tag_aliases WHERE guild_id = ? AND tag_id = ? ORDER BY name", (guild_id, tag_id))
        return [row["name"] for row in rows]

    async def _name_taken(self, guild_id: int, name: str, *, ignoring: int | None = None) -> bool:
        row = await self.db.one("SELECT id FROM tags WHERE guild_id = ? AND name = ?", (guild_id, name))
        if row is not None and row["id"] != ignoring:
            return True
        alias = await self.db.one("SELECT tag_id FROM tag_aliases WHERE guild_id = ? AND name = ?", (guild_id, name))
        return alias is not None

    def _is_command(self, name: str) -> bool:
        return name in self.bot.all_commands or name in SHORTCUTS

    # ---- who may do what ----

    def is_mod(self, member: Any) -> bool:
        """Manage Server, the bot manager role, or a bypass role on /tag from the Commands page."""
        who = who_from(member)
        settings = self.bot.settings.get(member.guild.id, COMMANDS_MODULE)
        bypass = set((settings["overwrites"].get("tag") or {}).get("bypass_roles") or [])
        return who.is_admin or settings["mod_role"] in who.role_ids or bool(bypass & who.role_ids)

    def _manage_problem(self, member: Any, tag: dict | None = None) -> str | None:
        """Why this person can't change this tag, or can't manage tags at all."""
        if self.is_mod(member):
            return None
        cfg = self.cfg(member.guild.id)
        if cfg["mod_only"]:
            return "Only moderators can manage tags in this server."
        if tag is not None and cfg["ownership"] and tag["owner_id"] != member.id:
            return "That tag belongs to someone else."
        return None

    def _rate_problem(self, guild_id: int, member: Any) -> str | None:
        """A new tag or alias, and the fetch behind a Pastebin link, each count once against someone without moderator rights."""
        if self.is_mod(member):
            return None
        now = time.monotonic()
        if len(self._created) > 5000:
            self._created = {key: times for key, times in self._created.items() if times and now - times[-1] < CREATE_WINDOW}
        times = self._created.setdefault((guild_id, member.id), deque(maxlen=CREATE_RATE))
        if len(times) == CREATE_RATE and now - times[0] < CREATE_WINDOW:
            return TOO_MANY_NEW
        times.append(now)
        return None

    @staticmethod
    def can_trust(member: Any) -> bool:
        """A trusted tag acts with the bot's own permissions for whoever runs it, so only the server owner or an Administrator can vouch for one."""
        return member.guild.owner_id == member.id or member.guild_permissions.administrator

    async def _word_problem(self, member: Any, *texts: str) -> str | None:
        automod = self.bot.module("automod")
        return await automod.word_problem(member, "\n".join(texts)) if automod is not None else None

    async def _alias_limit_problem(self, guild_id: int, member: Any) -> str | None:
        """An alias takes a name like a tag does, so it has the same limits."""
        count = await self.db.scalar("SELECT COUNT(*) FROM tag_aliases WHERE guild_id = ?", (guild_id,))
        if count >= TAG_LIMIT:
            return "This server has reached the limit on aliases."
        if not self.is_mod(member):
            if count >= TAG_LIMIT * MEMBERS_SHARE:
                return "Members have made as many aliases as this server keeps room for. A moderator can still add one."
            owned = await self.db.scalar("SELECT COUNT(*) FROM tag_aliases WHERE guild_id = ? AND owner_id = ?", (guild_id, member.id))
            if owned >= MEMBER_TAG_LIMIT:
                return f"You've made {owned} aliases, which is the limit. Remove one first."
        return None

    @staticmethod
    def _places(channel: Any) -> set[int]:
        """The channel, its parent when it is a thread, and the category: every ID a channel list can name for it."""
        parent = getattr(channel, "parent", None)
        places = {channel.id} | ({parent.id} if parent is not None else set())
        category = getattr(parent if parent is not None else channel, "category_id", None)
        if category:
            places.add(category)
        return places

    @staticmethod
    def _age_problem(channel: Any, tag: dict) -> str | None:
        """NSFW is an age gate, so it holds for everyone and for anything that shows the tag."""
        if tag["nsfw"]:
            is_nsfw = getattr(channel, "is_nsfw", None)
            if not (is_nsfw() if callable(is_nsfw) else False):
                return "This tag only works in channels marked as NSFW."
        return None

    def _use_problem(self, member: Any, channel: Any, tag: dict) -> str | None:
        """Why this tag won't run here for this person. The lists are moderation, and moderators skip them."""
        problem = self._age_problem(channel, tag)
        if problem:
            return problem
        if self.is_mod(member):
            return None
        places = self._places(channel)
        if tag["channel_wl"] and not places & set(tag["channel_wl"]):
            return "This tag can't be used in this channel."
        if places & set(tag["channel_bl"]):
            return "This tag can't be used in this channel."
        role_ids = {role.id for role in member.roles}
        if tag["role_wl"] and not role_ids & set(tag["role_wl"]):
            return "You don't have a role that can use this tag."
        if role_ids & set(tag["role_bl"]):
            return "You have a role that can't use this tag."
        return None

    def _too_fast(self, guild_id: int, user_id: int) -> bool:
        """A tag can spend up to a quarter second of the bot's time, so one member gets a few runs every few seconds."""
        now = time.monotonic()
        if len(self._recent) > 5000:
            self._recent = {key: times for key, times in self._recent.items() if times and now - times[-1] < BURST_SECONDS}
        times = self._recent.setdefault((guild_id, user_id), deque(maxlen=BURST_USES))
        if len(times) == BURST_USES and now - times[0] < BURST_SECONDS:
            return True
        times.append(now)
        return False

    # ---- running ----

    async def _on_unknown_command(self, ctx: Any) -> bool:
        """`.support` on a server whose prefix is a dot. Whatever fails here stays here, so one bad tag can't stop the next handler or log a stack per message."""
        if ctx.guild is None or not ctx.invoked_with:
            return False
        try:
            tag = await self.find(ctx.guild.id, ctx.invoked_with)
            # A later release can give a short name to a word a member's tag already holds. A moderator typing it means the command, so only a tag someone vouched for stays in front.
            if tag is None or (ctx.invoked_with.lower() in SHORTCUTS and not tag["trusted"]):
                return False
            ctx.command = self.bot.get_command("tag")
            try:
                if not await self.bot.can_run(ctx):
                    return True
            except commands.CheckFailure:  # the Commands page's rules for /tag hold for tags run by name, quietly, since this was typed as chat
                return True
            await self.use_tag(ctx, tag, ctx.view.read_rest().strip())
            await self.bot._after_command(ctx)
        except Exception:
            log.exception("Running tag %r failed", ctx.invoked_with)
        return True

    def _target(self, ctx: Any, args: str) -> Any:
        """The first person mentioned after the tag's name, or nobody."""
        match = _MEMBER_MENTION.search(args)
        if match is None:
            return None
        user_id = int(match.group(1))
        found = ctx.guild.get_member(user_id)
        if found is not None:
            return found
        return next((m for m in getattr(ctx.message, "mentions", []) if m.id == user_id), None)

    @staticmethod
    def _can_post(ctx: Any) -> bool:
        """Typing a tag's name takes Send Messages and a slash command doesn't, so /tag get has to ask."""
        allowed = ctx.channel.permissions_for(ctx.author)
        return allowed.send_messages_in_threads if isinstance(ctx.channel, discord.Thread) else allowed.send_messages

    async def use_tag(self, ctx: Any, tag: dict, args: str) -> None:
        if ctx.interaction is not None and not self._can_post(ctx):
            await ctx.fail("You can't post in this channel, so the bot won't post a tag for you here.")
            return
        problem = self._use_problem(ctx.author, ctx.channel, tag)
        if problem:
            await ctx.fail(problem)
            return
        if self._too_fast(ctx.guild.id, ctx.author.id):
            if ctx.interaction is not None:
                await ctx.fail("You're using tags too fast. Wait a few seconds.")
            return
        await self.db.run("UPDATE tags SET uses = uses + 1 WHERE guild_id = ? AND id = ?", (ctx.guild.id, tag["id"]))
        if tag["restricted"] and not self.is_mod(ctx.author):
            bot_channels = self.bot.settings.get(ctx.guild.id, COMMANDS_MODULE)["bot_channels"]
            if bot_channels and not self._places(ctx.channel) & set(bot_channels):
                if ctx.decision is None:
                    ctx.decision = Decision()
                ctx.decision.restricted_channel = bot_channels[0]
        scope = Scope(guild=ctx.guild, member=ctx.author, channel=ctx.channel, target=self._target(ctx, args), args=args, uses=tag["uses"] + 1)
        run = await run_tag(self.bot, tag["message"], scope, ctx=ctx, owner_id=tag["owner_id"], trusted=tag["trusted"], label=tag["name"])
        if run.benched:
            await self._tell_owner(ctx.guild, tag)

    async def _tell_owner(self, guild: Any, tag: dict) -> None:
        """A tag that ran out of time is off for everyone for a while, and whoever owns it is the one who can fix it."""
        owner = guild.get_member(tag["owner_id"]) if tag["owner_id"] else None
        if owner is None:
            return
        minutes = int(budget.BENCH_SECONDS // 60)
        try:
            await owner.send(f"Your tag `{tag['name']}` in {guild.name} did too much work, so it's off for {minutes} minutes. Changing its text turns it back on.", allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass  # plenty of people close their DMs

    # ---- changing ----

    async def create(self, guild: Any, author: Any, raw_name: str, content: str, *, counted: bool = False) -> str | None:
        """Make a tag for someone in Discord. Returns the reason when it can't. `counted` says this attempt has already counted against their rate."""
        problem = self._manage_problem(author) or (None if counted else self._rate_problem(guild.id, author))
        if problem:
            return problem
        try:
            name = clean_name(raw_name)
            message = _message_form({"content": content})
        except Invalid as error:
            return str(error)
        problem = await self._word_problem(author, name, content)
        if problem:
            return problem
        if self._is_command(name):
            return f"`{name}` is one of the bot's commands, so a tag with that name would never run."
        if await self._name_taken(guild.id, name):
            return f"There's already a tag or alias called `{name}`."
        count = await self.db.scalar("SELECT COUNT(*) FROM tags WHERE guild_id = ?", (guild.id,))
        if count >= TAG_LIMIT:
            return "This server has reached the limit on tags."
        if not self.is_mod(author):
            if count >= TAG_LIMIT * MEMBERS_SHARE:
                return "Members have made as many tags as this server keeps room for. A moderator can still add one."
            owned = await self.db.scalar("SELECT COUNT(*) FROM tags WHERE guild_id = ? AND owner_id = ?", (guild.id, author.id))
            if owned >= MEMBER_TAG_LIMIT:
                return f"You own {owned} tags, which is the limit. Remove one first."
        await self._insert(guild.id, name, message, owner_id=author.id, trusted=False)
        await self.bot.activity_log.record(guild.id, self.bot.actor(author), "tag.create", detail=name)
        return None

    async def _insert(self, guild_id: int, name: str, message: dict, *, owner_id: int | None, trusted: bool, uses: int = 0, created_at: float | None = None, **flags: Any) -> int:
        now = time.time()
        return await self.db.insert(
            "INSERT INTO tags (guild_id, name, content, extra, nsfw, restricted, channel_wl, channel_bl, role_wl, role_bl, uses, owner_id, trusted, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, name, message["content"], _extra(message), int(flags.get("nsfw", False)), int(flags.get("restricted", False)),
             json.dumps(flags.get("channel_wl", [])), json.dumps(flags.get("channel_bl", [])), json.dumps(flags.get("role_wl", [])), json.dumps(flags.get("role_bl", [])),
             uses, owner_id, int(trusted), created_at or now, now),
        )

    async def _set_content(self, ctx: Any, tag: dict, content: str, *, action: str, done: str) -> None:
        """Every edit from Discord lands here. A trusted tag stays trusted only when someone who could trust it makes the change, so nobody else can borrow what it does.

        Anyone else who changes the words becomes the tag's owner. Command blocks run for their owner at the owner's permissions, so they must only ever run words the owner stands behind.
        """
        problem = self._manage_problem(ctx.author, tag) or await self._word_problem(ctx.author, content)
        if problem:
            await ctx.fail(problem)
            return
        if len(content) > TEMPLATE_MAX:
            await ctx.fail(f"A tag can be at most {TEMPLATE_MAX} characters. This is {len(content)}.")
            return
        if not content.strip() and not tag["message"]["embeds"]:
            await ctx.fail("A tag needs some text.")
            return
        await self.db.run(
            "UPDATE tags SET content = ?, owner_id = ?, trusted = ?, updated_at = ? WHERE guild_id = ? AND id = ?",
            (content, tag["owner_id"] if self.can_trust(ctx.author) else ctx.author.id, int(tag["trusted"] and self.can_trust(ctx.author)), time.time(), ctx.guild.id, tag["id"]),
        )
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), action, detail=tag["name"])
        await ctx.ok(done)

    async def _required(self, ctx: Any, name: str) -> dict | None:
        tag = await self.find(ctx.guild.id, name)
        if tag is None:
            await ctx.fail(f"There's no tag called `{name}`.")
        return tag

    async def _delete_row(self, guild_id: int, tag_id: int) -> None:
        async with self.db.transaction():
            await self.db.run("DELETE FROM tags WHERE guild_id = ? AND id = ?", (guild_id, tag_id))
            await self.db.run("DELETE FROM tag_aliases WHERE guild_id = ? AND tag_id = ?", (guild_id, tag_id))
            await self.db.run("DELETE FROM tag_shares WHERE guild_id = ? AND tag_id = ?", (guild_id, tag_id))

    async def _share_code(self, guild_id: int, tag_id: int, user_id: int | None) -> str:
        row = await self.db.one("SELECT code FROM tag_shares WHERE guild_id = ? AND tag_id = ?", (guild_id, tag_id))
        if row is not None:
            return row["code"]
        code = secrets.token_urlsafe(9)
        await self.db.run("INSERT INTO tag_shares (guild_id, tag_id, code, created_by, created_at) VALUES (?, ?, ?, ?, ?)", (guild_id, tag_id, code, user_id, time.time()))
        return code

    # ---- commands ----

    @commands.hybrid_group(name="tag", fallback="get", description="Run one of this server's tags")
    @commands.guild_only()
    @app_commands.describe(name="The tag's name", args="Text the tag can read")
    async def tag(self, ctx: commands.Context, name: str, *, args: str = "") -> None:
        if name.lower() == "get" and args:  # `.tag get support` reads naturally, and slash spells it that way
            name, _, args = args.partition(" ")
            args = args.strip()
        found = await self._required(ctx, name)
        if found is not None:
            await self.use_tag(ctx, found, args)

    @tag.command(name="create", aliases=["add", "+"], description="Create a tag")
    @app_commands.describe(name="One word, no spaces", content="What the tag posts")
    async def tag_create(self, ctx: commands.Context, name: str, *, content: str) -> None:
        problem = await self.create(ctx.guild, ctx.author, name, content)
        if problem:
            existing = await self.find(ctx.guild.id, name.lower())
            if existing is not None and existing["name"] == name.lower() and problem.startswith("There's already") and not self._manage_problem(ctx.author, existing):
                await ctx.fail(problem, view=ReplaceTagView(self, ctx, existing["name"], content))
                return
            await ctx.fail(problem)
            return
        await ctx.ok(f"Created the tag `{name.lower()}`.")

    @tag.command(name="procreate", aliases=["++"], description="Create a tag from a Pastebin link, for long content")
    @app_commands.describe(name="One word, no spaces", content="A Pastebin link, or the content itself")
    async def tag_procreate(self, ctx: commands.Context, name: str, *, content: str) -> None:
        await ctx.defer(ephemeral=True)
        match = _PASTEBIN.search(content)
        if match:
            # Someone who'd be refused doesn't get to make the bot open a connection first.
            problem = self._manage_problem(ctx.author) or self._rate_problem(ctx.guild.id, ctx.author)
            if problem:
                await ctx.fail(problem)
                return
            fetched = await self._fetch_pastebin(match.group(1))
            if fetched is None:
                await ctx.fail("I couldn't read that Pastebin link.")
                return
            content = fetched
        problem = await self.create(ctx.guild, ctx.author, name, content, counted=bool(match))
        if problem:
            await ctx.fail(problem)
            return
        await ctx.ok(f"Created the tag `{name.lower()}`.")

    async def _fetch_pastebin(self, code: str) -> str | None:
        """The whole paste, or None when it can't be had or runs past what a tag could hold."""
        try:
            reply = await self.outside.get(f"https://pastebin.com/raw/{code}", max_bytes=TEMPLATE_MAX * 4, timeout=PASTEBIN_TIMEOUT)
        except OutsideError as error:
            log.info("Reading a Pastebin link failed: %s", error)
            return None
        return reply.text() if reply.status == 200 else None

    @tag.command(name="prompt", description="Open a form to write a tag with line breaks")
    async def tag_prompt(self, ctx: commands.Context) -> None:
        if ctx.interaction is None:
            await ctx.fail(f"The form only opens from the slash command `/tag prompt`. Long tags are easiest on the dashboard: {self.bot.public_url}/dashboard/{ctx.guild.id}/tags")
            return
        await ctx.interaction.response.send_modal(TagForm(self))

    @tag.command(name="edit", aliases=["update", "&", "e"], description="Change a tag's content")
    @app_commands.describe(name="The tag's name", content="The new content")
    async def tag_edit(self, ctx: commands.Context, name: str, *, content: str) -> None:
        tag = await self._required(ctx, name)
        if tag is not None:
            await self._set_content(ctx, tag, content, action="tag.edit", done=f"Changed the tag `{tag['name']}`.")

    @tag.command(name="append", aliases=["+="], description="Add text to the end of a tag")
    @app_commands.describe(name="The tag's name", content="The text to add")
    async def tag_append(self, ctx: commands.Context, name: str, *, content: str) -> None:
        tag = await self._required(ctx, name)
        if tag is not None:
            await self._set_content(ctx, tag, f"{tag['message']['content']}\n{content}", action="tag.append", done=f"Added to the tag `{tag['name']}`.")

    @tag.command(name="replace", aliases=["sub"], description="Replace some text everywhere in a tag")
    @app_commands.describe(name="The tag's name", from_text="The text to find. With a prefix, put it in quotes if it has spaces.", to_text="What goes in its place")
    async def tag_replace(self, ctx: commands.Context, name: str, from_text: str, *, to_text: str = "") -> None:
        tag = await self._required(ctx, name)
        if tag is None:
            return
        content = tag["message"]["content"]
        if from_text not in content:
            await ctx.fail("That text isn't in the tag.")
            return
        if len(content) + content.count(from_text) * (len(to_text) - len(from_text)) > TEMPLATE_MAX:
            await ctx.fail(f"That would make the tag longer than {TEMPLATE_MAX} characters.")
            return
        await self._set_content(ctx, tag, content.replace(from_text, to_text), action="tag.replace", done=f"Changed the tag `{tag['name']}`.")

    @tag.command(name="remove", aliases=["delete", "-", "del"], description="Remove a tag or one of its aliases")
    @app_commands.describe(name="The tag or alias")
    async def tag_remove(self, ctx: commands.Context, name: str) -> None:
        lookup = name.strip().lower()
        tag = await self._required(ctx, lookup)
        if tag is None:
            return
        problem = self._manage_problem(ctx.author, tag)
        if problem and tag["name"] != lookup:  # whoever made an alias can take it off again, without any claim on the tag behind it
            alias = await self.db.one("SELECT owner_id FROM tag_aliases WHERE guild_id = ? AND name = ?", (ctx.guild.id, lookup))
            problem = self._manage_problem(ctx.author, {"owner_id": alias["owner_id"] if alias else None})
        if problem:
            await ctx.fail(problem)
            return
        if tag["name"] != lookup:
            await self.db.run("DELETE FROM tag_aliases WHERE guild_id = ? AND name = ?", (ctx.guild.id, lookup))
            await ctx.ok(f"Removed the alias `{lookup}`.")
        else:
            await self._delete_row(ctx.guild.id, tag["id"])
            await ctx.ok(f"Removed the tag `{lookup}` and its aliases.")
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "tag.remove", detail=lookup)

    @tag.command(name="alias", aliases=["a"], description="Give a tag a second name")
    @app_commands.describe(point="The new name", name="The tag it points to")
    async def tag_alias(self, ctx: commands.Context, point: str, name: str) -> None:
        # The two orders read equally well, and only one of the names can already exist.
        existing, new = name, point
        if await self.find(ctx.guild.id, existing) is None and await self.find(ctx.guild.id, new) is not None:
            existing, new = new, existing
        tag = await self._required(ctx, existing)
        if tag is None:
            return
        problem = self.cfg(ctx.guild.id)["mod_only"] and self._manage_problem(ctx.author)  # ownership doesn't stop aliases: anyone can make one to any tag
        if problem:
            await ctx.fail(problem)
            return
        try:
            alias = clean_name(new)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        if self._is_command(alias):
            await ctx.fail(f"`{alias}` is one of the bot's commands, so an alias with that name would never run.")
            return
        if await self._name_taken(ctx.guild.id, alias):
            await ctx.fail(f"There's already a tag or alias called `{alias}`.")
            return
        problem = await self._word_problem(ctx.author, alias) or await self._alias_limit_problem(ctx.guild.id, ctx.author) or self._rate_problem(ctx.guild.id, ctx.author)
        if problem:
            await ctx.fail(problem)
            return
        await self.db.run("INSERT INTO tag_aliases (guild_id, name, tag_id, owner_id, created_at) VALUES (?, ?, ?, ?, ?)", (ctx.guild.id, alias, tag["id"], ctx.author.id, time.time()))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "tag.alias", detail=f"{alias} points to {tag['name']}")
        await ctx.ok(f"`{alias}` now runs the tag `{tag['name']}`.")

    @tag.command(name="raw", description="Show a tag's source with formatting and blocks visible")
    @app_commands.describe(name="The tag's name")
    async def tag_raw(self, ctx: commands.Context, name: str) -> None:
        tag = await self._required(ctx, name)
        if tag is None:
            return
        problem = self._age_problem(ctx.channel, tag)
        if problem:
            await ctx.fail(problem)
            return
        message = tag["message"]
        none = discord.AllowedMentions.none()
        shown = discord.utils.escape_markdown(message["content"])
        if message["embeds"] or len(shown) > RAW_INLINE_MAX:
            body = message["content"]
            if message["embeds"]:
                body += ("\n\n" if body else "") + json.dumps(message["embeds"], indent=2, ensure_ascii=False)
            await ctx.send(file=discord.File(io.BytesIO(body.encode("utf-8")), f"{tag['id']}.txt"), allowed_mentions=none, ephemeral=True)
            return
        await ctx.send(shown or "This tag has no text.", allowed_mentions=none, ephemeral=True)

    @tag.command(name="info", aliases=["owner"], description="Show a tag's owner, uses and dates")
    @app_commands.describe(name="The tag's name")
    async def tag_info(self, ctx: commands.Context, name: str) -> None:
        tag = await self._required(ctx, name)
        if tag is None:
            return
        rank = await self.db.scalar("SELECT COUNT(*) FROM tags WHERE guild_id = ? AND uses > ?", (ctx.guild.id, tag["uses"]))
        aliases = await self._aliases(ctx.guild.id, tag["id"])
        embed = discord.Embed(title=tag["name"], color=0x00A2F8)
        embed.add_field(name="Owner", value=f"<@{tag['owner_id']}>" if tag["owner_id"] else "Nobody")
        embed.add_field(name="Uses", value=str(tag["uses"]))
        embed.add_field(name="Rank", value=str(rank + 1))
        embed.add_field(name="Created", value=f"<t:{int(tag['created_at'])}:f>")
        embed.add_field(name="Updated", value=f"<t:{int(tag['updated_at'])}:f>")
        flags = [word for word, on in (("NSFW", tag["nsfw"]), ("Restricted", tag["restricted"])) if on]
        embed.add_field(name="Flags", value=", ".join(flags) or "None")
        embed.add_field(name="Aliases", value=", ".join(aliases)[:1024] or "None", inline=False)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), ephemeral=True)

    @tag.command(name="nsfw", description="Turn on or off limiting a tag to NSFW channels")
    @requires("manage_guild")
    @app_commands.describe(name="The tag's name")
    async def tag_nsfw(self, ctx: commands.Context, name: str) -> None:
        tag = await self._required(ctx, name)
        if tag is None:
            return
        await self.db.run("UPDATE tags SET nsfw = ?, updated_at = ? WHERE guild_id = ? AND id = ?", (int(not tag["nsfw"]), time.time(), ctx.guild.id, tag["id"]))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "tag.nsfw", detail=tag["name"])
        await ctx.ok(f"`{tag['name']}` {'now only works' if not tag['nsfw'] else 'no longer only works'} in NSFW channels.")

    @tag.command(name="restrict", description="Turn on or off sending a tag's output to the bot channel")
    @app_commands.describe(name="The tag's name")
    async def tag_restrict(self, ctx: commands.Context, name: str) -> None:
        tag = await self._required(ctx, name)
        if tag is None:
            return
        problem = self._manage_problem(ctx.author, tag)
        if problem:
            await ctx.fail(problem)
            return
        await self.db.run("UPDATE tags SET restricted = ?, updated_at = ? WHERE guild_id = ? AND id = ?", (int(not tag["restricted"]), time.time(), ctx.guild.id, tag["id"]))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "tag.restrict", detail=tag["name"])
        if tag["restricted"]:
            await ctx.ok(f"`{tag['name']}` answers where it's used again.")
        else:
            await ctx.ok(f"`{tag['name']}` now answers in the bot channel when a member uses it.")

    @tag.command(name="ownership", description="Turn on or off members owning the tags they make")
    @requires("manage_guild")
    @app_commands.describe(status="On means only a tag's owner or a moderator can change it")
    async def tag_ownership(self, ctx: commands.Context, status: bool) -> None:
        await self.bot.settings.save(ctx.guild.id, self.key, {"ownership": status}, actor=self.bot.actor(ctx.author), action="tag.ownership")
        await ctx.ok("Only a tag's owner or a moderator can change it now." if status else "Anyone who can manage tags can change any tag now.")

    @tag.command(name="modonly", description="Turn on or off limiting tag changes to moderators")
    @requires("manage_guild")
    @app_commands.describe(status="On means only moderators can manage tags, and everyone can still use them")
    async def tag_modonly(self, ctx: commands.Context, status: bool) -> None:
        await self.bot.settings.save(ctx.guild.id, self.key, {"mod_only": status}, actor=self.bot.actor(ctx.author), action="tag.modonly")
        await ctx.ok("Only moderators can manage tags now." if status else "Members can manage tags again.")

    @tag.command(name="claim", description="Take over a tag whose owner has left the server")
    @app_commands.describe(name="The tag's name")
    async def tag_claim(self, ctx: commands.Context, name: str) -> None:
        tag = await self._required(ctx, name)
        if tag is None:
            return
        if self.cfg(ctx.guild.id)["mod_only"] and not self.is_mod(ctx.author):
            await ctx.fail("Only moderators can manage tags in this server.")
            return
        if tag["owner_id"]:
            try:
                owner = await find_member(ctx.guild, tag["owner_id"])
            except (discord.HTTPException, OSError):  # no answer says nothing about whether they left
                await ctx.fail("Discord didn't say whether that tag's owner is still here. Try again in a moment.")
                return
            if owner is not None:
                await ctx.fail("That tag's owner is still in the server.")
                return
        await self.db.run(
            "UPDATE tags SET owner_id = ?, trusted = ?, updated_at = ? WHERE guild_id = ? AND id = ?",
            (ctx.author.id, int(tag["trusted"] and self.can_trust(ctx.author)), time.time(), ctx.guild.id, tag["id"]),
        )
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "tag.claim", detail=tag["name"])
        await ctx.ok(f"You now own the tag `{tag['name']}`.")

    @tag.command(name="list", description="List this server's tags, or one member's")
    @app_commands.describe(member="Only this member's tags")
    async def tag_list(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if member is None:
            rows = await self.db.all("SELECT name FROM tags WHERE guild_id = ? ORDER BY name", (ctx.guild.id,))
        else:
            rows = await self.db.all("SELECT name FROM tags WHERE guild_id = ? AND owner_id = ? ORDER BY name", (ctx.guild.id, member.id))
        await self._send_names(ctx, [row["name"] for row in rows], "No tags yet.")

    @tag.command(name="search", description="Find tags with some text in their name")
    @app_commands.describe(query="Part of a tag's name")
    async def tag_search(self, ctx: commands.Context, *, query: str) -> None:
        pattern = "%" + query.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = await self.db.all("SELECT name FROM tags WHERE guild_id = ? AND name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 200", (ctx.guild.id, pattern))
        await self._send_names(ctx, [row["name"] for row in rows], "No tag name holds that.")

    async def _send_names(self, ctx: Any, names: list[str], empty: str) -> None:
        none = discord.AllowedMentions.none()
        if not names:
            await ctx.send(empty, allowed_mentions=none, ephemeral=True)
            return
        shown = ", ".join(discord.utils.escape_markdown(name) for name in names)
        if len(shown) > RAW_INLINE_MAX:
            await ctx.send(f"{len(names)} tags, listed in this file.", file=discord.File(io.BytesIO("\n".join(names).encode("utf-8")), "tags.txt"), allowed_mentions=none, ephemeral=True)
            return
        await ctx.send(shown, allowed_mentions=none, ephemeral=True)

    @tag.command(name="stats", description="Show how much tags get used, in this server or by one member")
    @app_commands.describe(member="Only this member's tags")
    async def tag_stats(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        where, values = ("guild_id = ?", (ctx.guild.id,)) if member is None else ("guild_id = ? AND owner_id = ?", (ctx.guild.id, member.id))
        totals = await self.db.one(f"SELECT COUNT(*) AS tags, COALESCE(SUM(uses), 0) AS uses FROM tags WHERE {where}", values)
        top = await self.db.all(f"SELECT name, uses FROM tags WHERE {where} ORDER BY uses DESC, name LIMIT 3", values)
        embed = discord.Embed(title=f"Tags of {member.display_name}" if member else "Tags in this server", color=0x00A2F8)
        embed.add_field(name="Tags", value=str(totals["tags"]))
        embed.add_field(name="Uses", value=str(totals["uses"]))
        lines = "\n".join(f"`{row['name']}`: {row['uses']}" for row in top)
        embed.add_field(name="Most used", value=lines or "None", inline=False)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), ephemeral=True)

    @tag.command(name="share", description="Get a tag as a file and a code that other servers can import")
    @requires("manage_guild")
    @app_commands.describe(name="The tag's name")
    async def tag_share(self, ctx: commands.Context, name: str) -> None:
        tag = await self._required(ctx, name)
        if tag is None:
            return
        code = await self._share_code(ctx.guild.id, tag["id"], ctx.author.id)
        body = json.dumps({"kind": "tags", "version": EXPORT_VERSION, "tags": [_portable(tag, await self._aliases(ctx.guild.id, tag["id"]))]}, indent=2, ensure_ascii=False)
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "tag.share", detail=tag["name"])
        await ctx.send(
            f"Share code for `{tag['name']}`: `{code}`\nEnter it under Import on the Tags page of any server this bot runs in, or import the attached file.",
            file=discord.File(io.BytesIO(body.encode("utf-8")), f"tag-{tag['id']}.json"), allowed_mentions=discord.AllowedMentions.none(), ephemeral=True,
        )

    @tag.command(name="unshare", description="Stop a tag's share code from working")
    @requires("manage_guild")
    @app_commands.describe(name="The tag's name")
    async def tag_unshare(self, ctx: commands.Context, name: str) -> None:
        tag = await self._required(ctx, name)
        if tag is None:
            return
        count = await self.db.run("DELETE FROM tag_shares WHERE guild_id = ? AND tag_id = ?", (ctx.guild.id, tag["id"]))
        if not count:
            await ctx.fail(f"`{tag['name']}` isn't shared.")
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "tag.unshare", detail=tag["name"])
        await ctx.ok(f"The share code for `{tag['name']}` no longer works.")

    @tag.command(name="unshareall", description="Stop every share code from this server from working")
    @requires("manage_guild")
    async def tag_unshareall(self, ctx: commands.Context) -> None:
        count = await self.db.run("DELETE FROM tag_shares WHERE guild_id = ?", (ctx.guild.id,))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "tag.unshareall", detail=str(count))
        await ctx.ok(f"Stopped {count} share code{'' if count == 1 else 's'}.")

    @tag.autocomplete("name")
    @tag_edit.autocomplete("name")
    @tag_append.autocomplete("name")
    @tag_replace.autocomplete("name")
    @tag_remove.autocomplete("name")
    @tag_raw.autocomplete("name")
    @tag_info.autocomplete("name")
    @tag_nsfw.autocomplete("name")
    @tag_restrict.autocomplete("name")
    @tag_claim.autocomplete("name")
    @tag_share.autocomplete("name")
    @tag_unshare.autocomplete("name")
    @tag_alias.autocomplete("name")
    async def complete_name(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        pattern = current.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = await self.db.all(
            "SELECT name FROM (SELECT name, uses FROM tags WHERE guild_id = ? AND name LIKE ? ESCAPE '\\' "
            "UNION ALL SELECT name, 0 AS uses FROM tag_aliases WHERE guild_id = ? AND name LIKE ? ESCAPE '\\') ORDER BY uses DESC, name LIMIT 25",
            (interaction.guild_id, pattern, interaction.guild_id, pattern),
        )
        return [app_commands.Choice(name=row["name"], value=row["name"]) for row in rows]

    # ---- when something a tag names goes away ----

    async def _thing_gone(self, guild: Any, thing_id: int, fields: tuple[str, str], what: str) -> None:
        """The ID stays on the row. An emptied whitelist would mean everyone, which would open the gate. The page marks it, and staff hear once."""
        where = " OR ".join(f"{field} LIKE ?" for field in fields)
        rows = await self.db.all(f"SELECT name FROM tags WHERE guild_id = ? AND ({where}) ORDER BY name LIMIT 20", (guild.id, *[f"%{thing_id}%"] * len(fields)))
        if not rows:
            return
        names = ", ".join(row["name"] for row in rows)
        log.info("%s was deleted in server %s, and tags still name it: %s", what, guild.id, names)
        logs = self.bot.module("logging")
        if logs is not None:
            logs.staff_notice(guild, "A tag names something deleted", f"{what} was deleted, and these tags still name it on their lists: {names}. The Tags page marks them.")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: Any) -> None:
        await self._thing_gone(role.guild, role.id, ("role_wl", "role_bl"), f"The role {role.name}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        await self._thing_gone(channel.guild, channel.id, ("channel_wl", "channel_bl"), f"The channel {channel.name}")

    def _gone(self, guild_id: int, row: dict) -> dict:
        """What a row names that no longer exists. A tag whose only whitelisted role went tells every member they lack it, so the page has to show why."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {"gone_roles": [], "gone_channels": []}
        return {
            "gone_roles": [i for field in ("role_wl", "role_bl") for i in json.loads(row[field] or "[]") if guild.get_role(i) is None],
            "gone_channels": [i for field in ("channel_wl", "channel_bl") for i in json.loads(row[field] or "[]") if guild.get_channel_or_thread(i) is None],
        }

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/tags", self.api_list)
        router.get("/tags/waiting", self.api_waiting)
        router.post("/tags/trust", self.api_trust)
        router.get("/tags/{tag_id:\\d+}", self.api_one)
        router.post("/tags", self.api_create)
        router.get("/tags/export", self.api_export)
        router.post("/tags/import", self.api_import)
        router.post("/tags/preview", self.api_preview)
        router.put("/tags/{tag_id:\\d+}", self.api_update)
        router.delete("/tags/{tag_id:\\d+}", self.api_delete)
        router.post("/tags/{tag_id:\\d+}/share", self.api_share)
        router.delete("/tags/{tag_id:\\d+}/share", self.api_unshare)

    async def _api_rows(self, guild_id: int) -> list[dict]:
        rows = [_decode(row) for row in await self.db.all("SELECT * FROM tags WHERE guild_id = ? ORDER BY name", (guild_id,))]
        aliases: dict[int, list[str]] = {}
        for row in await self.db.all("SELECT tag_id, name FROM tag_aliases WHERE guild_id = ? ORDER BY name", (guild_id,)):
            aliases.setdefault(row["tag_id"], []).append(row["name"])
        shares = {row["tag_id"]: row["code"] for row in await self.db.all("SELECT tag_id, code FROM tag_shares WHERE guild_id = ?", (guild_id,))}
        for row in rows:
            row["aliases"] = aliases.get(row["id"], [])
            row["share_code"] = shares.get(row["id"])
        return rows

    async def api_list(self, req: Req) -> dict:
        """One page of the table, with a short summary in place of each message. A server can hold 10,000 tags, and the editor fetches the one it opens."""
        page = max(1, whole_number(req.query.get("page", ""), 10 ** 6) or 1)
        size = min(PAGE_SIZE_MAX, max(1, whole_number(req.query.get("size", ""), 10 ** 6) or PAGE_SIZE))
        where, values = "guild_id = ?", [req.guild_id]
        wanted = req.query.get("q", "").strip().lower()
        if wanted:
            pattern = "%" + wanted.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where += " AND (name LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')"
            values += [pattern, pattern]
        column = "uses" if req.query.get("sort") == "uses" else "name"
        direction = "DESC" if req.query.get("down") == "1" else "ASC"
        rows = await self.db.all(
            f"SELECT id, name, substr(content, 1, {SUMMARY_MAX}) AS summary, length(content) AS length, extra, uses, trusted, channel_wl, channel_bl, role_wl, role_bl FROM tags "
            f"WHERE {where} ORDER BY {column} {direction}, name LIMIT ? OFFSET ?",
            (*values, size, (page - 1) * size),
        )
        total = await self.db.scalar(f"SELECT COUNT(*) FROM tags WHERE {where}", tuple(values))
        shown = [{
            "id": row["id"], "name": row["name"], "summary": row["summary"], "cut": row["length"] > SUMMARY_MAX, "has_embed": bool(json.loads(row["extra"] or "{}").get("embeds")),
            "uses": row["uses"], "trusted": bool(row["trusted"]), **self._gone(req.guild_id, row),
        } for row in rows]
        return {"tags": shown, "total": total, "page": page, "size": size, "prefixes": req.settings.get(req.guild_id, "core")["prefixes"], "can_trust": req.is_admin}

    async def api_one(self, req: Req) -> dict:
        row = await self.db.one("SELECT * FROM tags WHERE guild_id = ? AND id = ?", (req.guild_id, req.int_param("tag_id")))
        if row is None:
            raise ApiError(404, "That tag no longer exists.")
        gone = self._gone(req.guild_id, row)
        tag = _decode(row)
        share = await self.db.one("SELECT code FROM tag_shares WHERE guild_id = ? AND tag_id = ?", (req.guild_id, tag["id"]))
        return {**tag, **gone, "aliases": await self._aliases(req.guild_id, tag["id"]), "share_code": share["code"] if share else None}

    async def api_waiting(self, req: Req) -> dict:
        """Tags holding command blocks that only run for their owner until someone trusts them, as tags imported from elsewhere do. The page lists them for the person who can."""
        rows = await self.db.all(
            "SELECT id, name, content, extra FROM tags WHERE guild_id = ? AND trusted = 0 AND (content LIKE '%{%' OR extra LIKE '%{%{%') ORDER BY name", (req.guild_id,),
        )
        found = []
        for row in rows:
            commands_run = self._named_commands(f"{row['content']}\n{row['extra']}")
            if commands_run:
                found.append({"id": row["id"], "name": row["name"], "commands": commands_run})
        return {"tags": found[:WAITING_MAX], "total": len(found), "can_trust": req.is_admin}

    async def api_trust(self, req: Req) -> dict:
        if not req.is_admin:
            raise ApiError(403, TRUST_REFUSED)
        form = await req.json()
        ids = form.get("ids") if isinstance(form.get("ids"), list) else []
        trusted = 0
        for tag_id in ids[:WAITING_MAX]:
            number = whole_number(tag_id)
            tag = await self._by_id(req.guild_id, number) if number is not None else None
            if tag is None or tag["trusted"]:
                continue
            await self.db.run("UPDATE tags SET trusted = 1, updated_at = ? WHERE guild_id = ? AND id = ?", (time.time(), req.guild_id, tag["id"]))
            await req.record("tag.trust", detail=tag["name"])
            trusted += 1
        return {"trusted": trusted}

    def _form_problem(self, name: str) -> ValidationError | None:
        if self._is_command(name):
            return ValidationError({"name": f"{name} is one of the bot's commands, so a tag with that name would never run."})
        return None

    async def api_create(self, req: Req) -> dict:
        form = TAG_FORM.clean(await req.json())
        req.refuse_other_servers({field: form[field] for field in _LIST_FIELDS})
        if not form["name"]:
            raise ValidationError({"name": "A tag needs a name."})
        if not messages.has_content(form["message"]):
            raise ValidationError({"message": "Write some text or fill in an embed first."})
        problem = self._form_problem(form["name"])
        if problem:
            raise problem
        if await self._name_taken(req.guild_id, form["name"]):
            raise ValidationError({"name": "There's already a tag or alias with that name."})
        if await self.db.scalar("SELECT COUNT(*) FROM tags WHERE guild_id = ?", (req.guild_id,)) >= TAG_LIMIT:
            raise ApiError(400, "This server has reached the limit on tags.")
        if form["trusted"] and not req.is_admin:
            raise ValidationError({"trusted": TRUST_REFUSED})
        tag_id = await self._insert(
            req.guild_id, form["name"], form["message"], owner_id=req.user.id, trusted=form["trusted"],
            **{key: form[key] for key in ("nsfw", "restricted", *_LIST_FIELDS)},
        )
        await req.record("tag.create", detail=form["name"])
        return {"id": tag_id}

    async def _api_tag(self, req: Req) -> dict:
        tag = await self._by_id(req.guild_id, req.int_param("tag_id"))
        if tag is None:
            raise ApiError(404, "That tag no longer exists.")
        return tag

    async def api_update(self, req: Req) -> dict:
        tag = await self._api_tag(req)
        form = TAG_FORM.clean(await req.json(), base={"name": tag["name"], "message": tag["message"], **{key: tag[key] for key in ("nsfw", "restricted", "trusted", *_LIST_FIELDS)}})
        req.refuse_other_servers({field: form[field] for field in _LIST_FIELDS})
        if not messages.has_content(form["message"]):
            raise ValidationError({"message": "Write some text or fill in an embed first."})
        if form["trusted"] and not tag["trusted"] and not req.is_admin:
            raise ValidationError({"trusted": TRUST_REFUSED})
        # Trust goes with the words someone vouched for and who can run them, so anyone who couldn't vouch takes it away by changing either.
        trusted = form["trusted"] and (req.is_admin or all(form[key] == tag[key] for key in ("message", *_LIST_FIELDS)))
        if form["name"] != tag["name"]:
            problem = self._form_problem(form["name"])
            if problem:
                raise problem
            if await self._name_taken(req.guild_id, form["name"], ignoring=tag["id"]):
                raise ValidationError({"name": "There's already a tag or alias with that name."})
        # Command blocks run for their owner at the owner's permissions, so anyone who couldn't vouch for the words becomes their owner by changing them.
        owner_id = tag["owner_id"] if req.is_admin or form["message"] == tag["message"] else req.user.id
        await self.db.run(
            "UPDATE tags SET name = ?, content = ?, extra = ?, nsfw = ?, restricted = ?, channel_wl = ?, channel_bl = ?, role_wl = ?, role_bl = ?, owner_id = ?, trusted = ?, updated_at = ? "
            "WHERE guild_id = ? AND id = ?",
            (form["name"], form["message"]["content"], _extra(form["message"]), int(form["nsfw"]), int(form["restricted"]), json.dumps(form["channel_wl"]),
             json.dumps(form["channel_bl"]), json.dumps(form["role_wl"]), json.dumps(form["role_bl"]), owner_id, int(trusted), time.time(), req.guild_id, tag["id"]),
        )
        await req.record("tag.edit", detail=form["name"])
        if trusted != tag["trusted"]:
            await req.record("tag.trust" if trusted else "tag.untrust", detail=form["name"])
        if form["trusted"] and not trusted:  # they left the switch on and it went off anyway, so the page has to say
            return {"id": tag["id"], "trust_removed": True}
        return {"id": tag["id"]}

    async def api_delete(self, req: Req) -> dict:
        tag = await self._api_tag(req)
        await self._delete_row(req.guild_id, tag["id"])
        await req.record("tag.remove", detail=tag["name"])
        return {"ok": True}

    async def api_share(self, req: Req) -> dict:
        tag = await self._api_tag(req)
        code = await self._share_code(req.guild_id, tag["id"], req.user.id)
        await req.record("tag.share", detail=tag["name"])
        return {"code": code}

    async def api_unshare(self, req: Req) -> dict:
        tag = await self._api_tag(req)
        await self.db.run("DELETE FROM tag_shares WHERE guild_id = ? AND tag_id = ?", (req.guild_id, tag["id"]))
        await req.record("tag.unshare", detail=tag["name"])
        return {"ok": True}

    async def api_export(self, req: Req) -> dict:
        wanted = {part for part in req.query.get("ids", "").split(",") if part}
        rows = await self._api_rows(req.guild_id)
        return {"kind": "tags", "version": EXPORT_VERSION, "tags": [_portable(row, row["aliases"]) for row in rows if not wanted or str(row["id"]) in wanted]}

    async def api_preview(self, req: Req) -> dict:
        form = await req.json()
        message = clean_template_message(form.get("message") if isinstance(form, dict) else None)
        args = form.get("args") if isinstance(form.get("args"), str) else ""
        scope = preview_scope(req.ops, req.guild_id, req.user.id, args=args[:2000], channel_id=None)
        try:
            rendered, actions, _ = await budgeted_render(lambda: render_strict(message, scope), req.guild_id, pool=budget.PREVIEWS, member_id=req.user.id)
        except LimitReached:
            return {"message": messages.empty_payload(), "does": [], "error": "This template does too much work, so it would be stopped."}
        except OverBudget:
            return {"message": messages.empty_payload(), "does": [], "error": "Too much template work this minute. Try again in a moment."}
        return {"message": rendered, "does": describe_actions(actions, trusted=form.get("trusted") is True, kind="tag"), "error": None}

    async def _shared_tag(self, code: str) -> dict:
        row = await self.db.one("SELECT guild_id, tag_id FROM tag_shares WHERE code = ?", (code,))
        tag = await self._by_id(row["guild_id"], row["tag_id"]) if row else None
        if tag is None:
            raise ValidationError({"code": "That share code doesn't work. It may have been unshared."})
        # Another server's channels, roles and people mean nothing here, so only the tag itself travels.
        return _portable({**tag, "channel_wl": [], "channel_bl": [], "role_wl": [], "role_bl": [], "uses": 0, "owner_id": None, "created_at": None}, [])

    def _known_ids(self, req: Req) -> tuple[set[int], set[int]]:
        info = req.ops.describe_guild(req.guild_id) or {}
        return {int(c["id"]) for c in info.get("channels") or []}, {int(r["id"]) for r in info.get("roles") or []}

    async def api_import(self, req: Req) -> dict:
        """Tags from a file or a share code. With "preview" on, it only reports what would happen. Names already in use are skipped, or replaced with "replace" on."""
        form = await req.json()
        if not isinstance(form, dict):
            raise ApiError(400, "Send the tags to import.")
        code = form.get("code")
        if isinstance(code, str) and code.strip():
            items: Any = [await self._shared_tag(code.strip())]
        else:
            try:
                data = read_json_text(form.get("data"))
            except ValueError as error:
                raise ValidationError({"data": "That isn't valid JSON."}) from error
            items = data.get("tags") if isinstance(data, dict) and "tags" in data else data
            if isinstance(items, dict):
                items = [items]
        if not isinstance(items, list):
            raise ValidationError({"data": "That file doesn't hold any tags."})
        if len(items) > IMPORT_LIMIT:
            raise ValidationError({"data": f"One import can hold at most {IMPORT_LIMIT} tags."})
        replace = form.get("replace") is True
        preview = form.get("preview") is True
        channels, roles = self._known_ids(req)
        rows, ready, seen = [], [], set()
        for item in items:
            label = item.get("name") if isinstance(item, dict) else None
            try:
                tag = imported_tag(item)
            except Invalid as error:
                rows.append({"name": str(label or "unnamed")[:NAME_MAX], "action": "skip", "reason": str(error), "commands": []})
                continue
            if tag["name"] in seen:
                rows.append({"name": tag["name"], "action": "skip", "reason": "That name appears twice in this file.", "commands": []})
                continue
            seen.add(tag["name"])
            tag["channel_wl"] = [i for i in tag["channel_wl"] if i in channels]
            tag["channel_bl"] = [i for i in tag["channel_bl"] if i in channels]
            tag["role_wl"] = [i for i in tag["role_wl"] if i in roles]
            tag["role_bl"] = [i for i in tag["role_bl"] if i in roles]
            existing = await self.db.one("SELECT id FROM tags WHERE guild_id = ? AND name = ?", (req.guild_id, tag["name"]))
            alias = await self.db.one("SELECT tag_id FROM tag_aliases WHERE guild_id = ? AND name = ?", (req.guild_id, tag["name"]))
            action = "create"
            reason = None
            if tag["name"] in SHORTCUTS:
                action, reason = "skip", f"{tag['name']} is short for one of the bot's commands, so a tag can't take that name."
            elif alias is not None or (existing is not None and not replace):
                action, reason = "skip", "That name is already in use."
            elif existing is not None:
                action = "replace"
            elif self._is_command(tag["name"]):
                reason = "That's also a command's name, so the tag can only run with /tag get."
            left_out = [name for name in tag["aliases"] if name in SHORTCUTS]
            if left_out and action != "skip":
                tag["aliases"] = [name for name in tag["aliases"] if name not in SHORTCUTS]
                note = f"Left out the alias {', '.join(left_out)}, which is short for one of the bot's commands."
                reason = f"{reason} {note}" if reason else note
            commands_run = self._named_commands(tag["message"]["content"])
            rows.append({"name": tag["name"], "action": action, "reason": reason, "commands": commands_run})
            if action != "skip":
                ready.append((tag, existing["id"] if existing else None))
        if preview:
            return {"rows": rows, "imported": 0}
        count = await self.db.scalar("SELECT COUNT(*) FROM tags WHERE guild_id = ?", (req.guild_id,))
        if count + sum(1 for _, existing_id in ready if existing_id is None) > TAG_LIMIT:
            raise ApiError(400, "That would pass the limit on tags.")
        for tag, existing_id in ready:
            await self._import_one(req, tag, existing_id)
        await req.record("tag.import", detail=f"{len(ready)} tags")
        return {"rows": rows, "imported": len(ready)}

    @staticmethod
    def _named_commands(content: str) -> list[str]:
        """What a tag from elsewhere asks to run, shown before anyone accepts it. It comes in untrusted, so these only run for its owner until someone trusts it."""
        found = re.findall(r"\{\s*(?:command|cmd|c|cmdadmin)\s*:([^{}]{0,120})", content, re.IGNORECASE)
        return [text.strip() or "a command the tag builds when it runs" for text in found][:10]

    async def _import_one(self, req: Req, tag: dict, existing_id: int | None) -> None:
        async with self.db.transaction():
            if existing_id is not None:
                # New words under an old owner would run at that owner's permissions, so only someone who could vouch for them leaves the owner in place.
                owner_id = (await self.db.scalar("SELECT owner_id FROM tags WHERE guild_id = ? AND id = ?", (req.guild_id, existing_id))) if req.is_admin else req.user.id
                await self.db.run(
                    "UPDATE tags SET content = ?, extra = ?, nsfw = ?, restricted = ?, channel_wl = ?, channel_bl = ?, role_wl = ?, role_bl = ?, owner_id = ?, trusted = 0, updated_at = ? "
                    "WHERE guild_id = ? AND id = ?",
                    (tag["message"]["content"], _extra(tag["message"]), int(tag["nsfw"]), int(tag["restricted"]), json.dumps(tag["channel_wl"]), json.dumps(tag["channel_bl"]),
                     json.dumps(tag["role_wl"]), json.dumps(tag["role_bl"]), owner_id, time.time(), req.guild_id, existing_id),
                )
                tag_id = existing_id
            else:
                if await self._name_taken(req.guild_id, tag["name"]):  # an alias earlier in this same file took the name
                    return
                # An owner runs their tag's command blocks at their own permissions, so naming someone else in a file takes the standing to vouch for a tag.
                owner_id = (tag["owner_id"] or req.user.id) if req.is_admin else req.user.id
                tag_id = await self._insert(
                    req.guild_id, tag["name"], tag["message"], owner_id=owner_id, trusted=False, uses=tag["uses"], created_at=tag["created_at"],
                    **{key: tag[key] for key in ("nsfw", "restricted", *_LIST_FIELDS)},
                )
            for alias in tag["aliases"]:
                if not await self._name_taken(req.guild_id, alias):
                    await self.db.run(
                        "INSERT INTO tag_aliases (guild_id, name, tag_id, owner_id, created_at) VALUES (?, ?, ?, ?, ?)",
                        (req.guild_id, alias, tag_id, req.user.id, time.time()),
                    )
