"""Automatic responses: a message that matches, or a role that changes, sets off a stored message."""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, messages
from ..core.members import find_member
from ..core.module import Module, requires
from ..core.context import unused_words
from ..core.shortcuts import SHORTCUTS, TAG_FIRST_SHORTCUTS
from ..core.tagscript import LimitReached, OverBudget, RegexGuard, Scope, budget, budgeted_render, clean_template_message, ids_from, read_json_text, render_strict, run_tag
from ..core.tagscript.preview import describe_actions, preview_scope
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, choice, integer, optional, snowflake, snowflakes, text
from ..web.api import ApiError, Req

log = logging.getLogger(__name__)

NORMAL, STRICT, CHANNEL, EXACT, STARTSWITH, ENDSWITH, REGEX, ROLE = range(1, 9)
MATCH_NAMES = {
    NORMAL: "Normal", STRICT: "Strict", CHANNEL: "Channel specific", EXACT: "Exact", STARTSWITH: "Startswith", ENDSWITH: "Endswith", REGEX: "Regex", ROLE: "Role Assignment",
}

TRIGGER_MIN = 2
TRIGGER_MAX = 1000
TRIGGER_LIMIT = 500
REGEX_LIMIT = 50                # each pattern can spend a second on one message, and every server shares the worker that runs them
IMPORT_LIMIT = 1000
MAX_COOLDOWN = 86400
MAX_RESPONSES_PER_MESSAGE = 3   # one message can match many triggers, and it shouldn't turn into a wall of replies
ROLE_COOLDOWN = 60              # how soon the same role change on the same member can fire the same trigger again
ROLE_POSTS_PER_MINUTE = 30      # what one role trigger can post, since one bulk role change on a big server would otherwise queue hours of posts in one channel
WRITER_RECHECK = 300.0          # how long someone Discord says has left isn't looked up again
LIST_ROOM = 1900                # what a list command puts in one message, which Discord caps at 2000 characters
EXPORT_VERSION = 1
TRUST_REFUSED = "Only the server owner or an Administrator can trust a trigger."
TOO_MANY_PATTERNS = f"A server can hold at most {REGEX_LIMIT} regex triggers."
WRITER_CANT = "Whoever wrote this trigger has left, or can't read and post in that channel. Trust it, or have someone who can save it again."

_LIST_FIELDS = ("role_wl", "role_bl", "channel_wl", "channel_bl")
_PUNCTUATION = re.compile(r"[^\w\s]")


def _loose(value: str) -> str:
    """How exact matching compares: capitals, punctuation and extra spaces don't count."""
    return " ".join(_PUNCTUATION.sub("", value.casefold()).split())


def _fit(items: list[str], room: int, joiner: str) -> tuple[str, int]:
    """As many items as fit in `room` characters, and how many were left out."""
    used, kept = 0, []
    for item in items:
        if used + len(item) + len(joiner) > room:
            break
        kept.append(item)
        used += len(item) + len(joiner)
    return joiner.join(kept), len(items) - len(kept)


def _ignored_text(cfg: dict, room: int) -> str:
    """The two ignore lists. A hundred channels run past what one message holds."""
    lines = []
    for label, items in (("channels", [f"<#{c}>" for c in cfg["ignored_channels"]]), ("members", [f"<@{m}>" for m in cfg["ignored_members"]])):
        shown, left = _fit(items, room // 2, ", ")
        lines.append(f"Ignored {label}: {shown or 'none'}" + (f" and {left} more on the dashboard" if left else ""))
    return "\n".join(lines)


def _message_form(value: Any) -> dict:
    clean = clean_template_message(value)
    if not messages.has_content(clean):
        raise Invalid("Write a response or fill in an embed first.")
    return clean


TRIGGER_FORM = Schema({
    "match_type": Field(NORMAL, choice(*MATCH_NAMES)),
    "trigger": Field("", text(TRIGGER_MAX)),
    "message": Field(messages.empty_payload(), _message_form),
    "channel_id": Field(None, optional(snowflake)),
    "role_wl": Field([], snowflakes()),
    "role_bl": Field([], snowflakes()),
    "channel_wl": Field([], snowflakes()),
    "channel_bl": Field([], snowflakes()),
    "cooldown": Field(0, integer(0, MAX_COOLDOWN)),
    "trusted": Field(False, boolean),
})


def form_problems(form: dict) -> dict[str, str]:
    """What's wrong across a trigger's fields, by field. A pattern's own check needs the worker, so `_all_problems` adds that one."""
    problems: dict[str, str] = {}
    kind = form["match_type"]
    if kind != ROLE and len(form["trigger"]) < TRIGGER_MIN:
        problems["trigger"] = f"A trigger needs at least {TRIGGER_MIN} characters."
    if len(form["trigger"]) > TRIGGER_MAX:
        problems["trigger"] = f"A trigger can be at most {TRIGGER_MAX} characters."
    if kind == CHANNEL and not form["channel_id"]:
        problems["channel_id"] = "Pick the channel this trigger listens in."
    if kind == ROLE and not form["channel_id"]:
        problems["channel_id"] = "Pick the channel this trigger posts in."
    if not messages.has_content(form["message"]):
        problems["message"] = "Write a response or fill in an embed first."
    return problems


def imported_trigger(item: Any) -> dict:
    """One trigger from a file, in our export form or the shape other dashboards hand out. Raises Invalid with a sentence."""
    if not isinstance(item, dict):
        raise Invalid("Each trigger must be a set of fields.")
    kind = item.get("match_type")
    if not isinstance(kind, int) or kind not in MATCH_NAMES:
        raise Invalid("That trigger has no match type this bot knows.")
    phrase = "" if kind == ROLE else str(item.get("trigger") or "").strip()
    embeds = item.get("embeds")
    if embeds is None:
        try:
            legacy = read_json_text(item.get("embed") or "{}")
        except ValueError as error:
            raise Invalid(f"The embed of {phrase or 'a role trigger'} isn't valid JSON.") from error
        embeds = [legacy] if isinstance(legacy, dict) and legacy else []
    message = clean_template_message({"content": item.get("response") or item.get("content") or "", "embeds": embeds, "buttons": item.get("buttons") or []})
    channel = item.get("channel_id")
    if channel is None:
        channel = item.get("specific_channel") if kind == CHANNEL else item.get("channel") if kind == ROLE else None
    channels = ids_from(channel)
    form = {
        "match_type": kind, "trigger": phrase, "message": message, "channel_id": channels[0] if channels else None,
        "role_wl": ids_from(item.get("role_wl") or item.get("roles_wl")), "role_bl": ids_from(item.get("role_bl") or item.get("roles_bl")),
        "channel_wl": ids_from(item.get("channel_wl")), "channel_bl": ids_from(item.get("channel_bl")),
        "cooldown": item["cooldown"] if isinstance(item.get("cooldown"), int) and 0 <= item["cooldown"] <= MAX_COOLDOWN else 0,
    }
    problems = form_problems(form)
    if problems:
        raise Invalid(next(iter(problems.values())))
    return form


class ClearView(discord.ui.View):
    """The prompt `triggers clear` asks for."""

    def __init__(self, cog: "Triggers", guild_id: int, author: Any) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.guild_id = guild_id
        self.author = author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author.id:
            return True
        await interaction.response.send_message("This prompt belongs to whoever ran the command.", ephemeral=True)
        return False

    @discord.ui.button(label="Remove every trigger", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        count = await self.cog.clear(self.guild_id, self.author)
        self.stop()
        await interaction.response.edit_message(content=f"\N{WHITE HEAVY CHECK MARK} Removed {count} trigger{'' if count == 1 else 's'}.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Nothing was removed.", view=None)


class Triggers(Module):
    key = "triggers"
    row_limits = {"triggers": TRIGGER_LIMIT}
    category = "Tags & Triggers"
    schema = Schema({
        "ignored_channels": Field([], snowflakes()),
        "ignored_members": Field([], snowflakes()),
    })
    migrations = [
        """
        CREATE TABLE triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            match_type INTEGER NOT NULL,
            phrase TEXT NOT NULL,
            content TEXT NOT NULL,
            extra TEXT NOT NULL DEFAULT '{}',
            channel_id INTEGER,
            role_wl TEXT NOT NULL DEFAULT '[]',
            role_bl TEXT NOT NULL DEFAULT '[]',
            channel_wl TEXT NOT NULL DEFAULT '[]',
            channel_bl TEXT NOT NULL DEFAULT '[]',
            cooldown INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX triggers_guild ON triggers (guild_id, phrase);
        """,
        "ALTER TABLE triggers ADD COLUMN trusted INTEGER NOT NULL DEFAULT 0;",
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.guard = RegexGuard()
        self._rows_by_guild: dict[int, list[dict]] = {}
        self._versions: dict[int, int] = {}
        self._fired: dict[tuple, float] = {}
        self._role_posts: dict[int, deque[float]] = {}
        self._role_told: dict[int, float] = {}
        self._problems: dict[int, tuple[str, float]] = {}   # each trigger's last problem, for the page
        self._writers_gone: dict[tuple[int, int], float] = {}

    async def cog_unload(self) -> None:
        await self.guard.close()

    # ---- storage ----

    @staticmethod
    def _prepared(row: dict) -> dict:
        """A row made ready for every message: its JSON read, and its phrase folded the way matching needs it."""
        extra = json.loads(row.pop("extra") or "{}")
        row["message"] = {"content": row["content"], "embeds": extra.get("embeds", []), "buttons": extra.get("buttons", [])}
        for field in _LIST_FIELDS:
            row[field] = json.loads(row[field] or "[]")
        phrase = row["phrase"]
        row["folded"] = phrase.casefold().strip()
        row["loose"] = _loose(phrase)
        if row["match_type"] == STRICT:
            words = phrase.split()
            row["pattern"] = re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)", re.IGNORECASE)
        return row

    async def rows(self, guild_id: int) -> list[dict]:
        """Every trigger in a server, alphabetically. Held in memory, because this runs on every message and they rarely change."""
        rows = self._rows_by_guild.get(guild_id)
        if rows is None:
            version = self._versions.get(guild_id, 0)
            rows = [self._prepared(row) for row in await self.db.all("SELECT * FROM triggers WHERE guild_id = ? ORDER BY phrase, id", (guild_id,))]
            if self._versions.get(guild_id, 0) == version:  # a save that landed during the read would otherwise be forgotten until the next save
                self._rows_by_guild[guild_id] = rows
        return rows

    async def on_guild_restored(self, guild_id: int) -> None:
        self._changed(guild_id)

    def _changed(self, guild_id: int) -> None:
        self._versions[guild_id] = self._versions.get(guild_id, 0) + 1
        self._rows_by_guild.pop(guild_id, None)

    async def _insert(self, guild_id: int, form: dict, user_id: int | None) -> int:
        now = time.time()
        message = form["message"]
        trigger_id = await self.db.insert(
            "INSERT INTO triggers (guild_id, match_type, phrase, content, extra, channel_id, role_wl, role_bl, channel_wl, channel_bl, cooldown, trusted, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, form["match_type"], form["trigger"], message["content"], json.dumps({"embeds": message["embeds"], "buttons": message["buttons"]}), form["channel_id"],
             json.dumps(form["role_wl"]), json.dumps(form["role_bl"]), json.dumps(form["channel_wl"]), json.dumps(form["channel_bl"]), form["cooldown"], int(form.get("trusted", False)), user_id, now, now),
        )
        self._changed(guild_id)
        return trigger_id

    async def _replace(self, guild_id: int, trigger_id: int, form: dict, writer: int) -> None:
        message = form["message"]
        await self.db.run(
            "UPDATE triggers SET match_type = ?, phrase = ?, content = ?, extra = ?, channel_id = ?, role_wl = ?, role_bl = ?, channel_wl = ?, channel_bl = ?, cooldown = ?, trusted = ?, created_by = ?, updated_at = ? "
            "WHERE guild_id = ? AND id = ?",
            (form["match_type"], form["trigger"], message["content"], json.dumps({"embeds": message["embeds"], "buttons": message["buttons"]}), form["channel_id"],
             json.dumps(form["role_wl"]), json.dumps(form["role_bl"]), json.dumps(form["channel_wl"]), json.dumps(form["channel_bl"]), form["cooldown"], int(form.get("trusted", False)), writer, time.time(), guild_id, trigger_id),
        )
        self._changed(guild_id)

    async def _delete(self, guild_id: int, trigger_id: int) -> int:
        self._problems.pop(trigger_id, None)
        count = await self.db.run("DELETE FROM triggers WHERE guild_id = ? AND id = ?", (guild_id, trigger_id))
        self._changed(guild_id)
        return count

    async def clear(self, guild_id: int, author: Any) -> int:
        for row in await self.rows(guild_id):
            self._problems.pop(row["id"], None)
        count = await self.db.run("DELETE FROM triggers WHERE guild_id = ?", (guild_id,))
        self._changed(guild_id)
        await self.bot.activity_log.record(guild_id, self.bot.actor(author), "trigger.clear", detail=str(count))
        return count

    async def _all_problems(self, form: dict) -> dict[str, str]:
        problems = form_problems(form)
        if form["match_type"] == REGEX and "trigger" not in problems:
            problem = await self.guard.problem(form["trigger"])
            if problem:
                problems["trigger"] = problem
        return problems

    async def _twin(self, guild_id: int, form: dict, ignoring: int | None = None) -> dict | None:
        """A trigger already doing the same job. Role triggers can repeat, since each can post something different. Case changes what a pattern means, so patterns compare exactly."""
        if form["match_type"] == ROLE:
            return None
        for row in await self.rows(guild_id):
            if row["id"] == ignoring or row["match_type"] != form["match_type"] or row["channel_id"] != form["channel_id"]:
                continue
            if self._same_text_key(form["match_type"], row["phrase"]) == self._same_text_key(form["match_type"], form["trigger"]):
                return row
        return None

    @staticmethod
    def _same_text_key(kind: int, phrase: str) -> str:
        return phrase if kind == REGEX else phrase.casefold().strip()

    async def _pattern_problem(self, guild_id: int, form: dict, ignoring: int | None = None) -> str | None:
        if form["match_type"] != REGEX:
            return None
        count = sum(1 for row in await self.rows(guild_id) if row["match_type"] == REGEX and row["id"] != ignoring)
        return TOO_MANY_PATTERNS if count >= REGEX_LIMIT else None

    # ---- messages ----

    @staticmethod
    def _places(channel: Any) -> set[int]:
        parent = getattr(channel, "parent", None)
        places = {channel.id} | ({parent.id} if parent is not None else set())
        category = getattr(parent if parent is not None else channel, "category_id", None)
        if category:
            places.add(category)
        return places

    def _allowed(self, row: dict, member: Any, channel: Any, places: set[int]) -> bool:
        role_ids = {role.id for role in getattr(member, "roles", [])}
        if row["role_wl"] and not role_ids & set(row["role_wl"]):
            return False
        if role_ids & set(row["role_bl"]):
            return False
        if row["match_type"] == CHANNEL:
            return row["channel_id"] in places
        if row["channel_wl"] and not places & set(row["channel_wl"]):
            return False
        return not places & set(row["channel_bl"])

    async def _matches(self, row: dict, content: str) -> bool:
        kind = row["match_type"]
        if kind == REGEX:
            return await self.guard.search(row["phrase"], content, server=row["guild_id"])
        folded = content.casefold()
        if kind in (NORMAL, CHANNEL):
            return row["folded"] in folded
        if kind == STRICT:
            return row["pattern"].search(content) is not None
        if kind == EXACT:
            return _loose(content) == row["loose"] if row["loose"] else folded.strip() == row["folded"]
        if kind == STARTSWITH:
            return folded.lstrip().startswith(row["folded"])
        if kind == ENDSWITH:
            return folded.rstrip().endswith(row["folded"])
        return False

    async def _is_command(self, message: Any) -> bool:
        """A command or a tag being run isn't chat, so it doesn't set off triggers."""
        content = message.content
        for prefix in await self.bot.get_prefix(message):
            if content.startswith(prefix):
                words = content[len(prefix):].split(maxsplit=1)
                if not words:
                    return False
                if self.bot.typed_command(words[0], message.guild.id) is not None or words[0].lower() in SHORTCUTS or words[0].lower() in TAG_FIRST_SHORTCUTS:
                    return True
                tags = self.bot.module("tags")
                return tags is not None and await tags.find(message.guild.id, words[0]) is not None
        return False

    def _cooled_down(self, key: tuple, seconds: float) -> bool:
        """True once `seconds` have passed since this key last fired, and marks it as firing now."""
        now = time.monotonic()
        if seconds and now - self._fired.get(key, -seconds - 1) < seconds:
            return False
        if seconds:
            if len(self._fired) > 20_000:
                self._fired.clear()
            self._fired[key] = now
        return True

    async def _writer(self, guild: Any, row: dict) -> Any:
        """Whoever wrote this trigger, or None when they've gone. A gap in the cache isn't someone leaving, and someone who left isn't looked up again on every message."""
        user_id = row["created_by"]
        if not user_id:
            return None
        found = guild.get_member(user_id)
        if found is not None:
            return found
        key, now = (guild.id, user_id), time.monotonic()
        if now < self._writers_gone.get(key, 0.0):
            return None
        try:
            found = await find_member(guild, user_id)
        except (discord.HTTPException, OSError):
            return None
        if found is None:
            if len(self._writers_gone) > 5000:
                self._writers_gone.clear()
            self._writers_gone[key] = now + WRITER_RECHECK
        return found

    async def _writer_reaches(self, guild: Any, row: dict, channel: Any, *, reads: bool) -> bool:
        """The bot acts with its own permissions, so an untrusted trigger reads and posts only where the person who wrote it could. Someone who vouched for a trusted one answers for it."""
        if row["trusted"]:
            return True
        writer = await self._writer(guild, row)
        if writer is not None and access.can_post(writer, channel) and (not reads or access.can_read(writer, channel)):
            return True
        self._problems[row["id"]] = (WRITER_CANT, time.time())
        return False

    def _after_run(self, guild: Any, row: dict, run: Any) -> None:
        """Nobody is there to hear what went wrong with a trigger, so the page holds it."""
        if run.problem:
            self._problems[row["id"]] = (run.problem, time.time())
        elif run.ran:
            self._problems.pop(row["id"], None)
        if run.benched:
            logs = self.bot.module("logging")
            if logs is not None:
                minutes = int(budget.BENCH_SECONDS // 60)
                logs.staff_notice(guild, "A trigger did too much work", f"Trigger {row['id']} ran out of time, so it's off for {minutes} minutes. Changing its response turns it back on. {row['phrase'][:200]}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id is not None or not message.content:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        try:
            await self._handle_message(message)
        except Exception:
            log.exception("Checking triggers failed in guild %s", message.guild.id)

    async def _handle_message(self, message: Any) -> None:
        rows = await self.rows(message.guild.id)
        if not rows:
            return
        cfg = self.cfg(message.guild.id)
        if message.author.id in cfg["ignored_members"] or await self._is_command(message):
            return
        channel = message.channel
        places = self._places(channel)
        ignored_here = bool(places & set(cfg["ignored_channels"]))
        responses = 0
        for row in rows:
            kind = row["match_type"]
            if kind == ROLE or (ignored_here and kind != CHANNEL):  # a channel-specific trigger was set up for exactly this channel, so it listens through an ignore
                continue
            if not self._allowed(row, message.author, channel, places) or not await self._matches(row, message.content):
                continue
            if not await self._writer_reaches(message.guild, row, channel, reads=True):
                continue
            if not self._cooled_down((row["id"], channel.id), row["cooldown"]):
                continue
            target = next(iter(getattr(message, "mentions", []) or []), None)
            scope = Scope(guild=message.guild, member=message.author, channel=channel, target=target, args=message.content)
            run = await run_tag(self.bot, row["message"], scope, message=message, trusted=bool(row["trusted"]), staff_wrote=True, written_by=row["created_by"], label=f"Trigger {row['id']}")
            self._after_run(message.guild, row, run)
            responses += 1
            if responses >= MAX_RESPONSES_PER_MESSAGE:
                return

    # ---- role changes ----

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        try:
            await self._handle_role_change(before, after)
        except Exception:
            log.exception("Checking role triggers failed in guild %s", after.guild.id)

    async def _handle_role_change(self, before: Any, after: Any) -> None:
        if after.bot:
            return
        had, has = {role.id for role in before.roles}, {role.id for role in after.roles}
        if had == has or after.id in self.cfg(after.guild.id)["ignored_members"]:
            return
        rows = [row for row in await self.rows(after.guild.id) if row["match_type"] == ROLE]
        if not rows:
            return
        for role_id, added in [(role_id, True) for role_id in has - had] + [(role_id, False) for role_id in had - has]:
            role = after.guild.get_role(role_id)
            if role is None:
                continue
            for row in rows:
                # For a role trigger the lists look at the role that changed as well as the roles held, so "only for this role" can be said with a whitelist.
                considered = has | {role_id}
                if row["role_wl"] and not considered & set(row["role_wl"]):
                    continue
                if considered & set(row["role_bl"]):
                    continue
                if not self._cooled_down((row["id"], after.id, role_id), max(ROLE_COOLDOWN, row["cooldown"])):
                    continue
                channel = after.guild.get_channel_or_thread(row["channel_id"]) if row["channel_id"] else None
                if channel is None or not hasattr(channel, "send"):
                    continue
                if not await self._writer_reaches(after.guild, row, channel, reads=False) or not await self._role_post_room(row, channel):
                    continue
                actor = await self._who_changed(after, row)
                # {user} reads the person who changed the role. What the blocks do acts for the member it changed on, whatever the text happens to say.
                scope = Scope(guild=after.guild, member=actor, channel=channel, target=after, role=role, role_added=added, acting_member=after)
                run = await run_tag(self.bot, row["message"], scope, trusted=bool(row["trusted"]), staff_wrote=True, written_by=row["created_by"], label=f"Trigger {row['id']}")
                self._after_run(after.guild, row, run)

    async def _role_post_room(self, row: dict, channel: Any) -> bool:
        """Whether this trigger can post again this minute. The first change it drops gets one line in the log and one note in the channel."""
        now = time.monotonic()
        times = self._role_posts.setdefault(row["id"], deque(maxlen=ROLE_POSTS_PER_MINUTE))
        if len(times) < ROLE_POSTS_PER_MINUTE or now - times[0] >= 60:
            times.append(now)
            return True
        if now - self._role_told.get(row["id"], -60.0) >= 60:
            self._role_told[row["id"]] = now
            log.info("Role trigger %s in server %s reached its limit of posts a minute", row["id"], row["guild_id"])
            try:
                await channel.send(f"This trigger reached its limit of {ROLE_POSTS_PER_MINUTE} posts a minute, so some role changes won't be announced.", allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as error:
                log.info("Couldn't post a trigger's note: %s", error)
        return False

    async def _who_changed(self, member: Any, row: dict) -> Any:
        """{user} in a role trigger is whoever made the change. The audit log can lag a moment, so it's only waited for when the template asks."""
        wanted = row["message"]["content"] + json.dumps(row["message"]["embeds"])
        if "{user" not in wanted and "{mention" not in wanted:
            return member
        entry = await self.bot.audit.find(member.guild.id, discord.AuditLogAction.member_role_update, member.id)
        return entry.user if entry is not None and entry.user is not None else member

    # ---- commands ----

    @commands.hybrid_group(name="triggers", fallback="list", description="List this server's triggers and what they ignore")
    @commands.guild_only()
    @requires("manage_guild")
    @app_commands.default_permissions(manage_guild=True)
    async def triggers(self, ctx: commands.Context) -> None:
        rows = await self.rows(ctx.guild.id)
        lines = []
        for row in rows:
            shown = discord.utils.escape_markdown(row["phrase"][:60]) or f"<#{row['channel_id']}>"
            lines.append(f"`{row['id']}` {MATCH_NAMES[row['match_type']]}: {shown}")
        body, left = _fit(lines, LIST_ROOM // 2, "\n")
        if left:
            body += f"\nAnd {left} more on the dashboard."
        await ctx.send(f"{body or 'No triggers yet.'}\n\n{_ignored_text(self.cfg(ctx.guild.id), LIST_ROOM // 2)}", allowed_mentions=discord.AllowedMentions.none(), ephemeral=True)

    async def _add(self, ctx: Any, kind: int, trigger: str, response: str, channel_id: int | None = None) -> None:
        phrase = trigger.strip()
        form = {
            "match_type": kind, "trigger": phrase, "message": {"content": response, "embeds": [], "buttons": []}, "channel_id": channel_id,
            "role_wl": [], "role_bl": [], "channel_wl": [], "channel_bl": [], "cooldown": 0,
        }
        try:
            form["message"] = _message_form(form["message"])
        except Invalid as error:
            await ctx.fail(str(error))
            return
        problems = await self._all_problems(form)
        if problems:
            await ctx.fail(next(iter(problems.values())))
            return
        place = ctx.guild.get_channel_or_thread(channel_id) if channel_id else None
        problem = (access.post_refusal(ctx.author, place) if place is not None else None) or await self._pattern_problem(ctx.guild.id, form)
        if problem:
            await ctx.fail(problem)
            return
        if await self._twin(ctx.guild.id, form) is not None:
            await ctx.fail("A trigger like that already exists.")
            return
        if len(await self.rows(ctx.guild.id)) >= TRIGGER_LIMIT:
            await ctx.fail("This server has reached the limit on triggers.")
            return
        trigger_id = await self._insert(ctx.guild.id, form, ctx.author.id)
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "trigger.create", detail=phrase or MATCH_NAMES[kind])
        await ctx.ok(f"Created trigger {trigger_id}.")

    @triggers.command(name="create", aliases=["add"], description="Respond when a message holds this text anywhere")
    @requires("manage_guild")
    @app_commands.describe(trigger="The text to look for", response="What to respond with")
    async def triggers_create(self, ctx: commands.Context, trigger: str, *, response: str) -> None:
        await self._add(ctx, NORMAL, trigger, response)

    @triggers.command(name="strict", aliases=["s"], description="Respond when a message holds these exact words")
    @requires("manage_guild")
    @app_commands.describe(trigger="The words to look for", response="What to respond with")
    async def triggers_strict(self, ctx: commands.Context, trigger: str, *, response: str) -> None:
        await self._add(ctx, STRICT, trigger, response)

    @triggers.command(name="exact", aliases=["e"], description="Respond when a whole message is this text")
    @requires("manage_guild")
    @app_commands.describe(trigger="The whole message to look for", response="What to respond with")
    async def triggers_exact(self, ctx: commands.Context, trigger: str, *, response: str) -> None:
        await self._add(ctx, EXACT, trigger, response)

    @triggers.command(name="startswith", aliases=["sw"], description="Respond when a message starts with this text")
    @requires("manage_guild")
    @app_commands.describe(trigger="The start to look for", response="What to respond with")
    async def triggers_startswith(self, ctx: commands.Context, trigger: str, *, response: str) -> None:
        await self._add(ctx, STARTSWITH, trigger, response)

    @triggers.command(name="endswith", aliases=["ew"], description="Respond when a message ends with this text")
    @requires("manage_guild")
    @app_commands.describe(trigger="The end to look for", response="What to respond with")
    async def triggers_endswith(self, ctx: commands.Context, trigger: str, *, response: str) -> None:
        await self._add(ctx, ENDSWITH, trigger, response)

    @triggers.command(name="channel", aliases=["cs"], description="Like create, but only listens in this channel, even if it's ignored")
    @requires("manage_guild")
    @app_commands.describe(trigger="The text to look for", response="What to respond with")
    async def triggers_channel(self, ctx: commands.Context, trigger: str, *, response: str) -> None:
        await self._add(ctx, CHANNEL, trigger, response, channel_id=ctx.channel.id)

    @triggers.command(name="regex", description="Respond when a message matches a regular expression")
    @requires("manage_guild")
    @app_commands.describe(trigger="A Python regular expression", response="What to respond with")
    async def triggers_regex(self, ctx: commands.Context, trigger: str, *, response: str) -> None:
        await self._add(ctx, REGEX, trigger, response)

    @triggers.command(name="role", description="Respond when a member gains or loses a role")
    @requires("manage_guild")
    @app_commands.describe(response="What to respond with", channel="Where to post. This channel if you leave it out.")
    async def triggers_role(self, ctx: commands.Context, *, response: str, channel: discord.TextChannel | None = None) -> None:
        if channel is None and ctx.interaction is None:  # typed, a channel can end the line
            at_end = re.search(r"\s*<#(\d+)>\s*$", response)
            found = ctx.guild.get_channel(int(at_end.group(1))) if at_end else None
            if found is not None and hasattr(found, "send"):
                channel, response = found, response[:at_end.start()]
        await self._add(ctx, ROLE, "", response, channel_id=(channel or ctx.channel).id)

    @triggers.command(name="remove", aliases=["del", "delete", "-"], description="Remove a trigger by its text or ID")
    @requires("manage_guild")
    @app_commands.describe(trigger="The trigger's text", trigger_id="The trigger's ID, shown in the list")
    async def triggers_remove(self, ctx: commands.Context, trigger: str | None = None, trigger_id: int | None = None) -> None:
        rows = await self.rows(ctx.guild.id)
        chosen = [row for row in rows if row["id"] == trigger_id] if trigger_id is not None else []
        if not chosen and trigger:
            wanted = trigger.strip()
            chosen = [row for row in rows if str(row["id"]) == wanted] or [row for row in rows if row["phrase"].casefold() == wanted.casefold()]
        if not chosen:
            await ctx.fail("No trigger matches that. `/triggers list` shows their IDs.")
            return
        if len(chosen) > 1:
            await ctx.fail("More than one trigger has that text. Name it by ID: " + ", ".join(str(row["id"]) for row in chosen))
            return
        await self._delete(ctx.guild.id, chosen[0]["id"])
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "trigger.remove", detail=chosen[0]["phrase"] or MATCH_NAMES[ROLE])
        await ctx.ok(f"Removed trigger {chosen[0]['id']}.")

    @triggers.command(name="clear", description="Remove every trigger, after a prompt")
    @requires("manage_guild")
    async def triggers_clear(self, ctx: commands.Context) -> None:
        count = len(await self.rows(ctx.guild.id))
        if not count:
            await ctx.fail("There are no triggers to remove.")
            return
        await ctx.send(f"Remove all {count} triggers? This can't be undone.", view=ClearView(self, ctx.guild.id, ctx.author), ephemeral=True)

    async def _toggle_role(self, ctx: Any, field: str, role: Any, trigger_id: int, listed: str) -> None:
        row = next((row for row in await self.rows(ctx.guild.id) if row["id"] == trigger_id), None)
        if row is None:
            await ctx.fail("There's no trigger with that ID.")
            return
        current = list(row[field])
        added = role.id not in current
        current = [*current, role.id] if added else [r for r in current if r != role.id]
        # Who can set it off is part of what someone vouched for.
        trusted = row["trusted"] and (ctx.guild.owner_id == ctx.author.id or ctx.author.guild_permissions.administrator)
        await self.db.run(f"UPDATE triggers SET {field} = ?, trusted = ?, updated_at = ? WHERE guild_id = ? AND id = ?", (json.dumps(current), int(trusted), time.time(), ctx.guild.id, trigger_id))
        self._changed(ctx.guild.id)
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), f"trigger.{field}", detail=row["phrase"] or MATCH_NAMES[ROLE])
        await ctx.ok(f"{role.mention} is {'now' if added else 'no longer'} on the {listed} of trigger {trigger_id}.")

    @triggers.command(name="role_blacklist", aliases=["role_bl"], description="Stop members with this role from setting off a trigger")
    @requires("manage_guild")
    @app_commands.describe(role="The role", trigger_id="The trigger's ID, shown in the list")
    async def triggers_role_blacklist(self, ctx: commands.Context, role: discord.Role, trigger_id: int) -> None:
        await self._toggle_role(ctx, "role_bl", role, trigger_id, "blacklist")

    @triggers.command(name="role_whitelist", aliases=["role_wl"], description="Only let members with this role set off a trigger")
    @requires("manage_guild")
    @app_commands.describe(role="The role", trigger_id="The trigger's ID, shown in the list")
    async def triggers_role_whitelist(self, ctx: commands.Context, role: discord.Role, trigger_id: int) -> None:
        await self._toggle_role(ctx, "role_wl", role, trigger_id, "whitelist")

    async def _set_ignored(self, ctx: Any, member: Any, channel: Any, *, ignore: bool) -> None:
        """Typed, these take any mix of members and channels in any order, so what the two named options didn't take is read from the rest of the line."""
        things = [thing for thing in (member, channel) if thing is not None]
        for word in unused_words(ctx).split():
            found = await self._member_or_channel(ctx, word)
            if found is None:
                await ctx.fail(f"I can't find a member or channel called `{word.replace('`', '')[:60]}`, so nothing changed.")
                return
            things.append(found)
        if not things:
            await ctx.fail("Name a member, a channel or both.")
            return
        cfg = self.cfg(ctx.guild.id)
        members = {thing.id for thing in things if isinstance(thing, (discord.Member, discord.User)) or thing is member}
        channels = {thing.id for thing in things} - members
        for thing in things:
            refusal = access.view_refusal(ctx.author, thing) if ignore and thing.id in channels else None
            if refusal:
                await ctx.fail(refusal)
                return
        form: dict[str, list[int]] = {}
        for field, chosen in (("ignored_members", members), ("ignored_channels", channels)):
            if chosen:
                others = [i for i in cfg[field] if i not in chosen]
                form[field] = [*others, *sorted(chosen)] if ignore else others
        await self.bot.settings.save(ctx.guild.id, self.key, form, actor=self.bot.actor(ctx.author), action="trigger.ignore" if ignore else "trigger.unignore")
        await ctx.ok("Triggers ignore that now." if ignore else "Triggers listen to that again.")

    @triggers.group(name="ignore", fallback="list", description="Show who and where triggers ignore")
    @requires("manage_guild")
    async def triggers_ignore(self, ctx: commands.Context) -> None:
        await ctx.send(_ignored_text(self.cfg(ctx.guild.id), LIST_ROOM), allowed_mentions=discord.AllowedMentions.none(), ephemeral=True)

    @staticmethod
    async def _member_or_channel(ctx: Any, word: str) -> Any:
        for converter in (commands.MemberConverter(), commands.TextChannelConverter()):
            try:
                return await converter.convert(ctx, word)
            except commands.BadArgument:
                continue
        return None

    @triggers_ignore.command(name="add", ignore_extra=True, description="Stop a member or channel from setting off triggers")
    @requires("manage_guild")
    async def triggers_ignore_add(self, ctx: commands.Context, member: discord.Member | None = None, channel: discord.TextChannel | None = None) -> None:
        await self._set_ignored(ctx, member, channel, ignore=True)

    @triggers_ignore.command(name="remove", ignore_extra=True, description="Let a member or channel set off triggers again")
    @requires("manage_guild")
    async def triggers_ignore_remove(self, ctx: commands.Context, member: discord.Member | None = None, channel: discord.TextChannel | None = None) -> None:
        await self._set_ignored(ctx, member, channel, ignore=False)

    @triggers.command(name="unignore", ignore_extra=True, description="Let a member or channel set off triggers again")
    @requires("manage_guild")
    async def triggers_unignore(self, ctx: commands.Context, member: discord.Member | None = None, channel: discord.TextChannel | None = None) -> None:
        await self._set_ignored(ctx, member, channel, ignore=False)

    # ---- when something a trigger names goes away ----

    async def _thing_gone(self, guild: Any, thing_id: int, fields: tuple[str, ...], what: str) -> None:
        """The ID stays on the row. An emptied whitelist would mean everyone, which would open the gate. The page marks it, and staff hear once."""
        named = [row for row in await self.rows(guild.id) if any(thing_id == row[field] or (isinstance(row[field], list) and thing_id in row[field]) for field in fields)]
        if not named:
            return
        names = ", ".join(row["phrase"][:60] or f"role trigger {row['id']}" for row in named[:20])
        log.info("%s was deleted in server %s, and triggers still name it: %s", what, guild.id, names)
        logs = self.bot.module("logging")
        if logs is not None:
            logs.staff_notice(guild, "A trigger names something deleted", f"{what} was deleted, and these triggers still name it: {names}. The Triggers page marks them.")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: Any) -> None:
        await self._thing_gone(role.guild, role.id, ("role_wl", "role_bl"), f"The role {role.name}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        await self._thing_gone(channel.guild, channel.id, ("channel_id", "channel_wl", "channel_bl"), f"The channel {channel.name}")

    def _gone(self, row: dict) -> dict:
        """What a row names that no longer exists. A trigger in that state goes quiet, so the page has to show why."""
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None:
            return {"gone_roles": [], "gone_channels": [], "writer_gone": False}
        channels = [*([row["channel_id"]] if row["channel_id"] else []), *row["channel_wl"], *row["channel_bl"]]
        return {
            "gone_roles": [i for i in (*row["role_wl"], *row["role_bl"]) if guild.get_role(i) is None],
            "gone_channels": [i for i in channels if guild.get_channel_or_thread(i) is None],
            "writer_gone": not row["trusted"] and (not row["created_by"] or guild.get_member(row["created_by"]) is None),
        }

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/triggers", self.api_list)
        router.post("/triggers", self.api_create)
        router.get("/triggers/export", self.api_export)
        router.post("/triggers/import", self.api_import)
        router.post("/triggers/preview", self.api_preview)
        router.put("/triggers/{trigger_id:\\d+}", self.api_update)
        router.delete("/triggers/{trigger_id:\\d+}", self.api_delete)

    def _api_row(self, row: dict) -> dict:
        problem = self._problems.get(row["id"])
        return {
            "id": row["id"], "match_type": row["match_type"], "trigger": row["phrase"], "message": row["message"], "channel_id": row["channel_id"],
            **{field: row[field] for field in _LIST_FIELDS}, "cooldown": row["cooldown"], "skipped": row["match_type"] == REGEX and self.guard.took_too_long(row["phrase"], row["guild_id"]), "trusted": bool(row["trusted"]),
            "problem": problem[0] if problem else None, "problem_at": problem[1] if problem else None, **self._gone(row),
        }

    async def api_list(self, req: Req) -> dict:
        return {
            "triggers": [self._api_row(row) for row in await self.rows(req.guild_id)],
            "match_types": [{"id": key, "name": name} for key, name in MATCH_NAMES.items()],
            "can_trust": req.is_admin,
        }

    async def _checked_form(self, form: dict) -> dict:
        problems = await self._all_problems(form)
        if problems:
            raise ValidationError(problems)
        return form

    async def api_create(self, req: Req) -> dict:
        form = await self._checked_form(TRIGGER_FORM.clean(await req.json()))
        req.refuse_other_servers({field: form[field] for field in ("channel_id", *_LIST_FIELDS)})
        if form["channel_id"]:
            req.channel(form["channel_id"], posting=True)
        if form["trusted"] and not req.is_admin:
            raise ValidationError({"trusted": TRUST_REFUSED})
        problem = await self._pattern_problem(req.guild_id, form)
        if problem:
            raise ValidationError({"trigger": problem})
        if await self._twin(req.guild_id, form) is not None:
            raise ValidationError({"trigger": "A trigger like that already exists."})
        if len(await self.rows(req.guild_id)) >= TRIGGER_LIMIT:
            raise ApiError(400, "This server has reached the limit on triggers.")
        trigger_id = await self._insert(req.guild_id, form, req.user.id)
        await req.record("trigger.create", detail=form["trigger"] or MATCH_NAMES[ROLE])
        return {"id": trigger_id}

    async def _api_trigger(self, req: Req) -> dict:
        wanted = req.int_param("trigger_id")
        row = next((row for row in await self.rows(req.guild_id) if row["id"] == wanted), None)
        if row is None:
            raise ApiError(404, "That trigger no longer exists.")
        return row

    async def api_update(self, req: Req) -> dict:
        row = await self._api_trigger(req)
        base = {"match_type": row["match_type"], "trigger": row["phrase"], "message": row["message"], "channel_id": row["channel_id"], "cooldown": row["cooldown"], "trusted": bool(row["trusted"]), **{f: row[f] for f in _LIST_FIELDS}}
        form = await self._checked_form(TRIGGER_FORM.clean(await req.json(), base=base))
        req.refuse_other_servers({field: form[field] for field in ("channel_id", *_LIST_FIELDS)})
        if form["channel_id"] and form["channel_id"] != row["channel_id"]:
            req.channel(form["channel_id"], posting=True)
        if form["trusted"] and not row["trusted"] and not req.is_admin:
            raise ValidationError({"trusted": TRUST_REFUSED})
        problem = await self._pattern_problem(req.guild_id, form, ignoring=row["id"])
        if problem:
            raise ValidationError({"trigger": problem})
        # Trust goes with the words someone vouched for and what sets them off, so anyone who couldn't vouch takes it away by changing either.
        unchanged = form["match_type"] == row["match_type"] and form["trigger"] == row["phrase"] and form["channel_id"] == row["channel_id"] and all(form[key] == row[key] for key in ("message", *_LIST_FIELDS))
        kept = form["trusted"]
        form["trusted"] = form["trusted"] and (req.is_admin or unchanged)
        if await self._twin(req.guild_id, form, ignoring=row["id"]) is not None:
            raise ValidationError({"trigger": "A trigger like that already exists."})
        # An untrusted trigger reads and posts only where its writer could, so new words are the writer's own. Saving it unchanged keeps the writer unless that person has gone or an Administrator takes it on.
        guild = self.bot.get_guild(req.guild_id)
        writer_gone = guild is not None and (not row["created_by"] or guild.get_member(row["created_by"]) is None)
        await self._replace(req.guild_id, row["id"], form, req.user.id if not unchanged or req.is_admin or writer_gone else row["created_by"])
        await req.record("trigger.edit", detail=form["trigger"] or MATCH_NAMES[ROLE])
        if form["trusted"] != bool(row["trusted"]):
            await req.record("trigger.trust" if form["trusted"] else "trigger.untrust", detail=form["trigger"] or MATCH_NAMES[ROLE])
        if kept and not form["trusted"]:  # they left the switch on and it went off anyway, so the page has to say
            return {"id": row["id"], "trust_removed": True}
        return {"id": row["id"]}

    async def api_delete(self, req: Req) -> dict:
        row = await self._api_trigger(req)
        await self._delete(req.guild_id, row["id"])
        await req.record("trigger.remove", detail=row["phrase"] or MATCH_NAMES[ROLE])
        return {"ok": True}

    async def api_export(self, req: Req) -> dict:
        cfg = self.cfg(req.guild_id)
        rows = [
            {"match_type": row["match_type"], "trigger": row["phrase"], "content": row["message"]["content"], "embeds": row["message"]["embeds"], "buttons": row["message"]["buttons"],
             "channel_id": row["channel_id"], **{field: row[field] for field in _LIST_FIELDS}, "cooldown": row["cooldown"]}
            for row in await self.rows(req.guild_id)
        ]
        return {"kind": "triggers", "version": EXPORT_VERSION, "ignored_channels": cfg["ignored_channels"], "ignored_members": cfg["ignored_members"], "triggers": rows}

    async def api_preview(self, req: Req) -> dict:
        form = await req.json()
        message = clean_template_message(form.get("message") if isinstance(form, dict) else None)
        args = form.get("args") if isinstance(form.get("args"), str) else ""
        scope = preview_scope(req.ops, req.guild_id, req.user.id, args=args[:2000], role_id=self._sample_role(req) if form.get("match_type") == ROLE else None)
        try:
            rendered, actions, _ = await budgeted_render(lambda: render_strict(message, scope), req.guild_id, pool=budget.PREVIEWS, member_id=req.user.id)
        except LimitReached:
            return {"message": messages.empty_payload(), "does": [], "error": "This template does too much work, so it would be stopped."}
        except OverBudget:
            return {"message": messages.empty_payload(), "does": [], "error": "Too much template work this minute. Try again in a moment."}
        return {"message": rendered, "does": describe_actions(actions, trusted=form.get("trusted") is True, kind="trigger"), "error": None}

    @staticmethod
    def _sample_role(req: Req) -> int | None:
        """A role trigger exists for {role}, so its preview fills that in with the server's highest role."""
        roles = (req.ops.describe_guild(req.guild_id) or {}).get("roles") or []
        return max(roles, key=lambda role: role["position"])["id"] if roles else None

    async def api_import(self, req: Req) -> dict:
        """Triggers from a file. With "preview" on, it only reports what would happen. One that repeats an existing trigger is skipped, or replaced with "replace" on."""
        form = await req.json()
        if not isinstance(form, dict):
            raise ApiError(400, "Send the triggers to import.")
        try:
            data = read_json_text(form.get("data"))
        except ValueError as error:
            raise ValidationError({"data": "That isn't valid JSON."}) from error
        items = data.get("triggers") if isinstance(data, dict) and "triggers" in data else data
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            raise ValidationError({"data": "That file doesn't hold any triggers."})
        if len(items) > IMPORT_LIMIT:
            raise ValidationError({"data": f"One import can hold at most {IMPORT_LIMIT} triggers."})
        replace = form.get("replace") is True
        rows, ready, seen = [], [], set()
        patterns = sum(1 for row in await self.rows(req.guild_id) if row["match_type"] == REGEX)
        for item in items:
            label = str(item.get("trigger") or "") if isinstance(item, dict) else ""
            try:
                parsed = imported_trigger(item)
            except Invalid as error:
                rows.append({"trigger": label[:100], "action": "skip", "reason": str(error)})
                continue
            problems = await self._all_problems(parsed)
            if problems:
                rows.append({"trigger": label[:100], "action": "skip", "reason": next(iter(problems.values()))})
                continue
            try:
                req.refuse_other_servers({field: parsed[field] for field in ("channel_id", *_LIST_FIELDS)})
            except ValidationError:
                rows.append({"trigger": label[:100], "action": "skip", "reason": "That trigger names a channel or role of another server."})
                continue
            if parsed["channel_id"] and not req.can_see(parsed["channel_id"]):
                rows.append({"trigger": label[:100], "action": "skip", "reason": "You can't see that trigger's channel."})
                continue
            if parsed["channel_id"] and not req.can_post(parsed["channel_id"]):
                rows.append({"trigger": label[:100], "action": "skip", "reason": "You can't post in that trigger's channel."})
                continue
            key = (parsed["match_type"], self._same_text_key(parsed["match_type"], parsed["trigger"]), parsed["channel_id"])
            twin = await self._twin(req.guild_id, parsed)
            if parsed["match_type"] != ROLE and key in seen:
                rows.append({"trigger": parsed["trigger"][:100], "action": "skip", "reason": "That trigger appears twice in this file."})
                continue
            seen.add(key)
            if twin is not None and not replace:
                rows.append({"trigger": parsed["trigger"][:100], "action": "skip", "reason": "A trigger like that already exists."})
                continue
            if parsed["match_type"] == REGEX and twin is None:
                if patterns >= REGEX_LIMIT:
                    rows.append({"trigger": parsed["trigger"][:100], "action": "skip", "reason": TOO_MANY_PATTERNS})
                    continue
                patterns += 1
            rows.append({"trigger": parsed["trigger"][:100], "action": "replace" if twin else "create", "reason": None})
            ready.append((parsed, twin["id"] if twin else None))
        if form.get("preview") is True:
            return {"rows": rows, "imported": 0}
        if isinstance(data, dict):
            req.refuse_other_servers({"ignored_channels": ids_from(data.get("ignored_channels"))})
        if len(await self.rows(req.guild_id)) + sum(1 for _, twin in ready if twin is None) > TRIGGER_LIMIT:
            raise ApiError(400, "That would pass the limit on triggers.")
        for parsed, existing in ready:
            if existing is None:
                await self._insert(req.guild_id, parsed, req.user.id)
            else:
                await self._replace(req.guild_id, existing, parsed, req.user.id)
        if isinstance(data, dict):
            cfg = self.cfg(req.guild_id)
            merged = {field: sorted(set(cfg[field]) | set(ids_from(data.get(field)))) for field in ("ignored_channels", "ignored_members")}
            await req.settings.save(req.guild_id, self.key, merged, actor=req.actor, action="trigger.import")
        await req.record("trigger.import", detail=f"{len(ready)} triggers")
        return {"rows": rows, "imported": len(ready)}
