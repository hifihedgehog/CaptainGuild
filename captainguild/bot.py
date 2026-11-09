"""The Discord client: gateway events, command checks and the loading of modules."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
import traceback
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from .core import access
from .core import members as member_lists
from .core.activity import Activity
from .core.audit import AuditWatcher
from .core.context import Context
from .core.logsink import LogSink
from .core.module import Module, required_permissions, staff_only_label
from .core.ops import DiscordOps, OpsError
from .core.perms import MODULE as COMMANDS_MODULE
from .core.perms import SETTINGS_SCHEMA as COMMANDS_SCHEMA
from .core.perms import ADMINS_KEEP, ALWAYS_OPEN, Decision, Who, evaluate
from .core.settings import Actor, Settings
from .core.shortcuts import SHORTCUTS, expand_line
from .core.signin import SignInLinks
from .core.timers import Timers
from .core.tree import Tree
from .core.webhooks import Webhooks
from .core.usage import plain_error
from .core.validate import Field, Invalid, Schema, ValidationError, list_of, snowflakes, text
from .db import Database
from .dbtools import FILE_RESTORED_KEY

log = logging.getLogger(__name__)

# discord.py waits only about 5 seconds for a big server's member list before it reports ready. The bot gives the rest this long before its timers run on what it has.
MEMBER_LIST_SECONDS = 120.0
MENTION_ANSWER_GAP = 30.0

CORE_SCHEMA = Schema({
    "prefixes": Field(["!", "?"], list_of(text(10, minimum=1, strip=False), 15)),
    "dashboard_staff_roles": Field([], snowflakes(50)),
})

CORE_MIGRATIONS = [
    """
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE web_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        user_name TEXT NOT NULL,
        avatar TEXT,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );
    CREATE INDEX web_sessions_expiry ON web_sessions (expires_at);
    CREATE TABLE command_uses (
        guild_id INTEGER NOT NULL,
        day INTEGER NOT NULL,
        command TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        uses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, day, command, user_id)
    );
    """,
    # Whether the account had two-factor authentication when it signed in with Discord. A sign-in link from the bot can't tell, so it stays 0.
    "ALTER TABLE web_sessions ADD COLUMN mfa INTEGER NOT NULL DEFAULT 0;",
]

FILE_RESTORED_KEY = "file_restored_at"
# Who may run these depends on the channel it's used in, since a server can grant or deny them on one channel. The rest of a command's permissions are server-wide.
CHANNEL_SCOPED = ("manage_channels", "manage_messages")

UnknownCommandHandler = Callable[[Context], Awaitable[bool]]


def sentence_for(error: BaseException) -> str | None:
    """What to tell the person who ran a command, when the error carries a sentence written for them."""
    if isinstance(error, ValidationError):
        return next(iter(error.fields.values()), "That setting was refused.")
    if isinstance(error, (OpsError, Invalid)):
        return str(error)
    return None


def who_from(member: Any) -> Who:
    permissions = {name for name, on in member.guild_permissions if on}
    return Who(
        role_ids={role.id for role in member.roles},
        permissions=permissions,
        is_owner=member.guild.owner_id == member.id,
    )


# The Commands page's tabs, in a fixed order. Tabs not named here follow, by name.
TAB_ORDER = ["Config", "Automod", "Feeds", "Fun", "Greetings", "Levels", "Logging", "Misc", "Moderation", "Roles", "Stats", "Notifications", "Suggestions",
             "Tags & Triggers", "Utilities", "Embeds", "Starboard", "Sticky messages", "Games"]
# Commands that get a tab of their own instead of sitting under their module's.
TAB_OF = {"misc": "Misc", "stats": "Stats", "games": "Games"}


class Bot(commands.Bot):
    def __init__(self, db: Database, message_db: Database, *, public_url: str = "", read_message_text: bool = True, read_presences: bool = False) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = read_message_text
        intents.presences = read_presences  # statistics read statuses and games with it, and the rest of the bot works without it
        super().__init__(
            command_prefix=self._prefix_for,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            strip_after_prefix=True,
            max_messages=None,  # deleted and edited messages come from our own store, through raw events
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, replied_user=False),
            activity=discord.CustomActivity(name="/help"),
            tree_cls=Tree,
        )
        self.db = db
        self.message_db = message_db
        self.public_url = public_url
        self.reads_message_text = read_message_text
        self.reads_presences = read_presences
        self.sign_in_links = SignInLinks()
        self.started_at = time.time()
        self.activity_log = Activity(db)
        self.settings = Settings(db, self.activity_log)
        self.timers = Timers(db, server_here=self._server_here, server_left=self._server_left)
        self._mention_answered: dict[int, float] = {}
        self.members_settled = asyncio.Event()  # clear from when Discord sends READY until the member lists have come in
        self.members_settled.set()
        self._connects = 0
        self._resumes = 0
        self._member_list_task: asyncio.Future | None = None
        self._timer_start: asyncio.Future | None = None
        self.logs = LogSink(self)
        self.webhooks = Webhooks(self)
        self.audit = AuditWatcher()
        self.ops = DiscordOps(self)
        self.modules: list[Module] = []
        self.unknown_command_handlers: list[UnknownCommandHandler] = []
        # A command pack's slash commands and menus, by module key. They go into each server that switches the pack on.
        self.pack_commands: dict[str, list[Any]] = {}
        self._pack_locks: dict[int, asyncio.Lock] = {}
        self._pack_keys: dict[str, str] = {}  # each pack's Python module, to its key
        self.tree.pack_of = lambda command: self._pack_keys.get(getattr(command, "module", None) or "")
        self.add_check(self._check_command)
        self.tree.on_error = self._on_tree_error
        self.after_invoke(self._after_command)

    # ---- startup ----

    async def prepare(self, module_classes: list[type[Module]]) -> None:
        """Everything that happens before the gateway connects: tables, settings and modules."""
        await self.db.migrate("core", CORE_MIGRATIONS)
        await self.activity_log.load()
        await self.settings.load()
        await self.timers.load()
        self.settings.register("core", CORE_SCHEMA)
        self.settings.register(COMMANDS_MODULE, COMMANDS_SCHEMA)
        for module_class in module_classes:
            module = module_class(self)
            if module.schema is not None:
                self.settings.register(module.key, module.schema)
            await self.db.migrate(f"module:{module.key}", list(module.migrations))
            await self.message_db.migrate(f"module:{module.key}", list(module.message_migrations))
            if module.command_pack:
                for name in (type(module).__module__, *module.pack_modules):
                    self._pack_keys[name] = module.key  # so the tree holds its commands apart from the start
            await self.add_cog(module)
            self.modules.append(module)
        for module in self.modules:
            try:
                await module.on_load()
            except Exception:  # one server's bad row costs one feature its startup work, and every other feature and server still come up
                log.exception("%s didn't finish loading. Everything else carries on.", module.key)
        for command in self.walk_commands():  # after on_load, since a feature can add a subcommand there
            # A word a typed command can't use is refused, so a typo can't change the wrong thing. A command that reads the leftover words itself says so with ignore_extra=True.
            command.ignore_extra = command.__original_kwargs__.get("ignore_extra", False)
        self._take_pack_commands()  # after on_load, since that's where a feature adds its menus
        await self._finish_file_restore()

    async def _finish_file_restore(self) -> None:
        """The database file was put back from an older copy with `dbtools restore`. What it holds is out of date in the same ways a restored backup is, so the same things happen: role changes already past due don't run, and each feature treats its rows as restored rather than as time gone by."""
        stamp = await self.db.scalar("SELECT value FROM kv WHERE key = ?", (FILE_RESTORED_KEY,))
        if stamp is None:
            return
        kinds = sorted({kind for module in self.modules for kind in module.role_timers})
        if kinds:
            marks = ", ".join("?" for _ in kinds)
            await self.db.run(f"DELETE FROM timers WHERE due_at <= ? AND kind IN ({marks})", (time.time(), *kinds))
        guilds = {row["guild_id"] for row in await self.db.all("SELECT DISTINCT guild_id FROM guild_settings")}
        guilds |= {row["guild_id"] for row in await self.db.all("SELECT DISTINCT guild_id FROM timers WHERE guild_id IS NOT NULL")}
        for guild_id in sorted(guilds):
            await self.db.run("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (f"role_bulk_restored:{guild_id}", str(time.time())))
            for hook in ("before_restore", "on_guild_restored", "after_restore"):
                for module in self.modules:
                    try:
                        await getattr(module, hook)(guild_id)
                    except Exception:
                        log.exception("%s failed at %s for %s after a file restore", module.key, hook, guild_id)
        await self.db.run("DELETE FROM kv WHERE key = ?", (FILE_RESTORED_KEY,))
        log.info("Finished a restored database file for %d servers.", len(guilds))

    def module(self, key: str) -> Any:
        """Another feature by its key, for features that work together. None if it isn't loaded."""
        return next((m for m in self.modules if m.key == key), None)

    def transient_roles(self, guild_id: int) -> set[int]:
        """What every feature says it holds only for a limited time or under its own rule. See Module.transient_roles."""
        return {role for module in self.modules for role in module.transient_roles(guild_id)}

    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        self.audit.feed(entry)

    async def setup_hook(self) -> None:
        self._timer_start = asyncio.ensure_future(self._start_timers())
        await self._sync_commands()

    async def _start_timers(self) -> None:
        # Work that came due during downtime looks up its server, and that cache only exists once the gateway is ready.
        await self.wait_until_ready()
        self.timers.start()

    def _server_here(self, guild_id: int) -> bool:
        guild = self.get_guild(guild_id)
        return guild is not None and not getattr(guild, "unavailable", False)

    def _server_left(self, guild_id: int) -> bool:
        """Whether the bot is out of this server, as opposed to its lists being short for now."""
        return self.members_settled.is_set() and self.get_guild(guild_id) is None

    async def on_guild_join(self, guild: Any) -> None:
        await self.timers.server_back(guild.id)
        self.sync_server_soon(guild.id)
        await self._tell_who_added_the_bot(guild)

    async def _tell_who_added_the_bot(self, guild: Any) -> None:
        """Whoever just added the bot is the one who has to set it up. Without View Audit Log or with closed DMs, nothing is lost: /help and the bare mention point the same way."""
        if self.user is None:
            return
        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
                if entry.target is not None and entry.target.id == self.user.id and entry.user is not None:
                    where = f"Open {self.public_url} and sign in" if self.public_url else "Run /dashboard there"
                    await entry.user.send(f"I'm in {discord.utils.escape_markdown(guild.name)} now. {where}, and the dashboard shows where to start.")
                    return
        except discord.HTTPException as error:
            log.info("Could not tell who added the bot to %s: %s", guild.id, error)

    async def on_guild_available(self, guild: Any) -> None:
        await self.timers.server_back(guild.id)

    async def on_connect(self) -> None:
        """READY comes at the start and after a session that can't resume, and it empties the member and channel lists until the servers arrive again."""
        self._connects += 1
        self.members_settled.clear()
        self.timers.pause()

    async def on_ready(self) -> None:
        connect = self._connects
        try:
            while connect == self._connects:
                resumes = self._resumes
                await self._wait_for_member_lists()
                if resumes == self._resumes:  # a resume lets go of the request this was waiting on, with the list still short
                    break
        finally:
            if connect == self._connects:  # a newer connection has its own ready coming, and its lists aren't in yet
                self.members_settled.set()
                self.timers.resume()
                for guild in self.guilds:
                    self.sync_server_soon(guild.id)

    async def on_resumed(self) -> None:
        """A member list request sent on a connection that died gets no answer after a resume, and discord.py keeps it until the next READY, sending no other for that server. So it goes, and a new one goes out."""
        short = [guild for guild in self.guilds if not guild.unavailable and not member_lists._looks_whole(guild)]
        if not short:
            return
        self._resumes += 1
        for guild in short:
            member_lists.forget_chunk_request(guild)
        self._member_list_task = asyncio.ensure_future(self._wait_for_member_lists())

    async def _wait_for_member_lists(self) -> None:
        waiting = [guild.chunk() for guild in self.guilds if not guild.unavailable and not member_lists._looks_whole(guild)]
        if not waiting:
            return
        try:
            async with asyncio.timeout(MEMBER_LIST_SECONDS):
                await asyncio.gather(*waiting, return_exceptions=True)
        except TimeoutError:
            log.warning("Member lists were still arriving after %d seconds, so work goes on with what has come in.", MEMBER_LIST_SECONDS)

    async def _sync_commands(self) -> None:
        """Discord limits how often commands can be registered, so this only happens when they changed."""
        payload = [command.to_dict(self.tree) for command in self.tree.get_commands()]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        known = await self.db.scalar("SELECT value FROM kv WHERE key = 'commands_digest'")
        if known == digest:
            return
        await self.tree.sync()
        await self.db.run("INSERT INTO kv (key, value) VALUES ('commands_digest', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (digest,))
        log.info("Registered %d commands with Discord", len(payload))

    # ---- command packs ----

    def _take_pack_commands(self) -> None:
        """A command pack's slash commands and menus leave the list every server shares, to go into each server that switches the pack on."""
        packs = {name: module for module in self.modules if module.command_pack for name in (type(module).__module__, *module.pack_modules)}

        async def switched(guild_id: int, _settings: dict) -> None:
            self.sync_server_soon(guild_id)

        for module in packs.values():
            self.pack_commands[module.key] = list(self.tree.held.get(module.key, []))
            self.settings.listen(module.key, switched)
        for kind in (discord.AppCommandType.chat_input, discord.AppCommandType.message, discord.AppCommandType.user):
            for command in self.tree.get_commands(type=kind):
                module = packs.get(command.module or "")
                if module is not None:
                    self.tree.remove_command(command.name, type=kind)
                    self.pack_commands[module.key].append(command)

    def sync_server_soon(self, guild_id: int) -> None:
        """Put a server's pack commands in step with which packs it has on. Before the bot is connected there's nothing to tell Discord, and the ready that comes next does it."""
        if not self.pack_commands or not self.is_ready():
            return
        asyncio.ensure_future(self._sync_server_logged(guild_id))

    async def _sync_server_logged(self, guild_id: int) -> None:
        try:
            await self.sync_server_commands(guild_id)
        except Exception:
            log.exception("Couldn't register the command packs in %s. The next start or settings save tries again.", guild_id)

    async def sync_server_commands(self, guild_id: int) -> bool:
        """Registers the slash commands and menus of the packs a server has on, and only when they changed, since Discord limits how often that can happen. Returns whether it told Discord."""
        async with self._pack_locks.setdefault(guild_id, asyncio.Lock()):
            place = discord.Object(guild_id)
            self.tree.clear_commands(guild=place)
            for module in self.modules:
                if module.command_pack and module.on_in(guild_id):
                    for command in self.pack_commands.get(module.key, []):
                        self.tree.add_command(command, guild=place, override=True)
            payload = sorted((command.to_dict(self.tree) for kind in (discord.AppCommandType.chat_input, discord.AppCommandType.message, discord.AppCommandType.user)
                              for command in self.tree.get_commands(guild=place, type=kind)), key=lambda data: (data.get("type", 1), data["name"]))
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
            key = f"commands_digest:{guild_id}"
            if await self.db.scalar("SELECT value FROM kv WHERE key = ?", (key,)) == digest:
                return False
            await self.tree.sync(guild=place)
            await self.db.run("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, digest))
            log.info("Registered %d pack commands in %s", len(payload), guild_id)
            return True

    async def close(self) -> None:
        if self._timer_start is not None:
            self._timer_start.cancel()  # a READY during the stop below would start the timers back up
        if self._member_list_task is not None:
            self._member_list_task.cancel()
        await self.timers.stop()
        await self.logs.close()
        await super().close()

    # ---- commands ----

    async def _prefix_for(self, bot: Any, message: discord.Message) -> list[str]:
        base = [f"<@{self.user.id}> ", f"<@!{self.user.id}> "] if self.user else []
        if message.guild is None:
            return base
        return base + list(self.settings.get(message.guild.id, "core")["prefixes"])

    async def get_context(self, origin: Any, /, *, cls: type = Context) -> Any:
        return await super().get_context(origin, cls=cls)

    async def process_commands(self, message: discord.Message, /) -> None:
        if message.author.bot or message.guild is None:
            return
        ctx = await self.get_context(message)
        if ctx.command is not None and not self.runs_in(ctx.command, message.guild.id):
            ctx.command = None  # a pack switched off here that steps aside, so its word reaches tags and short names as if it weren't a command
        if ctx.command is not None:
            await self.invoke(await self._through_long_shortcut(ctx))
            return
        if ctx.prefix is None or not ctx.invoked_with:
            if self.user is not None and message.content.strip() in (f"<@{self.user.id}>", f"<@!{self.user.id}>"):
                await self._answer_bare_mention(message)
            return
        for handler in self.unknown_command_handlers:  # tags answer here
            if await handler(ctx):
                return
        await self._run_shortcut(ctx)

    async def _answer_bare_mention(self, message: discord.Message) -> None:
        """Someone naming the bot and nothing else wants to know what it does. One answer per channel every so often, so a row of mentions can't make it talk."""
        now = time.monotonic()
        if now - self._mention_answered.get(message.channel.id, -MENTION_ANSWER_GAP) < MENTION_ANSWER_GAP:
            return
        self._mention_answered[message.channel.id] = now
        try:
            await message.channel.send("Use /help to see what I can do.", allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            log.info("Could not answer a mention in %s: %s", message.channel.id, error)

    async def _run_shortcut(self, ctx: Context) -> None:
        """A short name for a command in a group. It runs as if the person had typed the whole thing, so the server's rules for that command apply."""
        fresh = await self._through_long_shortcut(ctx)
        if fresh.command is not None and self.runs_in(fresh.command, ctx.guild.id):
            await self.invoke(fresh)

    @staticmethod
    def runs_in(command: Any, guild_id: int | None) -> bool:
        """Whether a typed command answers in this server. One from a pack that steps aside where it's off doesn't, as if it weren't there."""
        cog = getattr(command, "cog", None)
        return not (isinstance(cog, Module) and cog.command_pack and cog.steps_aside_when_off and not cog.on_in(guild_id))

    def typed_command(self, name: str, guild_id: int | None) -> Any:
        """The command a typed word runs in this server, or None, the way process_commands reads it."""
        command = self.all_commands.get(name)
        return command if command is not None and self.runs_in(command, guild_id) else None

    async def _through_long_shortcut(self, ctx: Context) -> Context:
        """The line spelled the long way, by core.shortcuts.expand_line. The first word can be a real command, so this looks before it runs."""
        body = ctx.message.content[len(ctx.prefix):].lstrip()
        line = expand_line(self, body)
        if line == body:
            return ctx
        message = copy.copy(ctx.message)
        message.content = f"{ctx.prefix}{line}"
        return await self.get_context(message)

    async def _check_command(self, ctx: Context) -> bool:
        if ctx.guild is None or ctx.command is None:
            return True
        cog = getattr(ctx.command, "cog", None)
        if isinstance(cog, Module) and not cog.on_in(ctx.guild.id):
            raise commands.CheckFailure("That's switched off in this server. A manager can switch it on from the dashboard.")
        member = ctx.author if isinstance(ctx.author, discord.Member) else ctx.guild.get_member(ctx.author.id)
        if member is None:
            return False
        decision = self.command_decision(ctx.command, member, ctx.channel, moderator=getattr(ctx, "as_moderator", False))
        ctx.decision = decision
        if not decision.allowed:
            raise commands.CheckFailure(decision.reason or "You can't use this command here.")
        return True

    def command_decision(self, command: Any, member: Any, channel: Any, *, moderator: bool = False) -> Any:
        """Whether this person may run this command in this channel, under the server's rules. A typed name that's another spelling of a subcommand answers to that subcommand's rules, or turning off /notes would leave `setnote` running."""
        needed = required_permissions(command)
        command = self.get_command(command.extras.get("rules_of") or "") or command
        parent = getattr(channel, "parent", None)  # a thread takes its parent's rules
        place = parent if isinstance(channel, discord.Thread) and parent else channel
        root = command.root_parent or command
        who = who_from(member)
        who.moderator = moderator
        if root.qualified_name in ALWAYS_OPEN or (root.qualified_name in ADMINS_KEEP and access.is_admin(member)):
            return Decision()
        if hasattr(channel, "permissions_for"):  # a dashboard page asks with no real channel, and server-wide permissions answer it
            here = access.permissions_in(member, channel)
            for name in CHANNEL_SCOPED:
                if getattr(here, name):
                    who.permissions.add(name)
                else:
                    who.permissions.discard(name)
        fallback = getattr(command, "fallback", None)  # a group's own callback shows in Discord as this subcommand, so it can be turned off under that name
        return evaluate(
            self.settings.get(member.guild.id, COMMANDS_MODULE),
            root=root.qualified_name,
            leaf=command.qualified_name if command.parent else (f"{root.qualified_name} {fallback}" if fallback else None),
            required=needed,
            who=who,
            channel_id=place.id,
            category_id=getattr(place, "category_id", None),
        )

    async def _after_command(self, ctx: Context) -> None:
        if ctx.guild is None or ctx.command is None:
            return
        if ctx.command_failed:
            return
        root = (ctx.command.root_parent or ctx.command).qualified_name
        day = int(time.time() // 86400)
        await self.db.run(
            "INSERT INTO command_uses (guild_id, day, command, user_id, uses) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT (guild_id, day, command, user_id) DO UPDATE SET uses = uses + 1",
            (ctx.guild.id, day, root, ctx.author.id),
        )
        if ctx.decision and ctx.decision.delete and ctx.interaction is None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    async def on_command_error(self, ctx: Context, error: Exception, /) -> None:
        if isinstance(error, commands.CommandNotFound) or getattr(ctx, "error_answered", False):  # a command's own handler has said what it needs to
            return
        original = getattr(error, "original", error)
        if isinstance(original, app_commands.CommandInvokeError):
            original = original.original
        if isinstance(error, (commands.CheckFailure, commands.UserInputError, commands.ConversionError, commands.CommandOnCooldown, commands.DisabledCommand, commands.MaxConcurrencyReached)):
            await self._tell(ctx, plain_error(ctx, error))
            return
        if isinstance(original, app_commands.TransformerError):
            await self._tell(ctx, f"I can't use `{str(original.value)[:60]}` there.")
            return
        if isinstance(original, discord.Forbidden):
            await self._tell(ctx, "I don't have the Discord permission to do that. Check my role and where it sits in the role list.")
            return
        sentence = sentence_for(original)
        if sentence is not None:
            await self._tell(ctx, sentence)
            return
        log.error("Command %s failed:\n%s", ctx.command, "".join(traceback.format_exception(original)))
        await self._tell(ctx, "Something went wrong running that command. It's been logged.")

    async def _tell(self, ctx: Context, text_: str) -> None:
        try:
            await ctx.fail(text_)
        except discord.HTTPException:
            pass

    async def _on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        sentence = sentence_for(getattr(error, "original", error))
        if isinstance(error, app_commands.TransformerError):
            sentence = f"I can't use `{str(error.value)[:60]}` there."
        if sentence is None and not isinstance(error, app_commands.CheckFailure):
            log.error("App command failed: %s", "".join(traceback.format_exception(error)))
        message = sentence or "Something went wrong running that command. It's been logged."
        if isinstance(error, app_commands.CheckFailure):
            message = plain_error(None, error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    def actor(self, user: Any) -> Actor:
        return Actor(user.id, getattr(user, "name", str(user)))

    def command_permissions(self, name: str) -> set[str]:
        """The Discord permissions a command and everything under it need. A bypass role on a group's name covers them all."""
        command = self.get_command(name)
        if command is None:
            return set()
        needed = set(required_permissions(command))
        if isinstance(command, commands.Group):
            for child in command.walk_commands():
                needed |= set(required_permissions(child))
        return needed

    @staticmethod
    def _catalog_leaf(command: Any) -> dict:
        """What one runnable command needs, as the docs and the Commands page show it. Checks run per subcommand, so a group's row can't speak for all of them."""
        return {
            "name": command.qualified_name,
            "permissions": sorted(required_permissions(command)),
            "staff": staff_only_label(command),
            "text_only": getattr(command, "app_command", None) is None,
        }

    def command_catalog(self) -> list[dict]:
        """Every command, grouped by tab for the Commands page, in the order of TAB_ORDER. Each row lists the names that reach it besides its own."""
        groups: dict[str, list[dict]] = {}
        shortcuts: dict[str, set[str]] = {}
        for short, target in SHORTCUTS.items():
            shortcuts.setdefault(target.split()[0], set()).add(short)
        for command in self.commands:  # a typed twin has no rules of its own, so it's a name of the row it follows
            if command.extras.get("rules_of"):
                shortcuts.setdefault(command.extras["rules_of"].split()[0], set()).add(command.name)
        for command in sorted((c for c in self.commands if not c.extras.get("rules_of")), key=lambda c: c.qualified_name):
            category = TAB_OF.get(command.qualified_name) or (command.cog.category if isinstance(command.cog, Module) else "Misc")
            subcommands = []
            names = set(command.aliases) | shortcuts.get(command.qualified_name, set())
            slash = command.extras.get("slash_name")  # a typed group whose slash form goes by another name, such as statsinfo and /info
            if isinstance(command, commands.Group):
                subcommands = sorted(c.qualified_name for c in command.walk_commands() if not isinstance(c, commands.Group))
                for child in command.walk_commands():
                    names |= set(child.aliases)
                    if slash and not isinstance(child, commands.Group):
                        names.add(f"/{slash} {child.name}")
            children = [self._catalog_leaf(c) for c in sorted(command.walk_commands(), key=lambda c: c.qualified_name) if not isinstance(c, commands.Group)] if isinstance(command, commands.Group) else []
            own = self._catalog_leaf(command)
            # A group's own function runs only when it has a fallback, so a group with none is judged by its subcommands alone.
            runs_itself = not children or getattr(command, "fallback", None) is not None
            needs = {(tuple(row["permissions"]), row["staff"]) for row in [*children, *([own] if runs_itself else [])]}
            groups.setdefault(category, []).append({
                "name": command.qualified_name,
                "description": command.description or command.help or command.short_doc or "",
                "permissions": sorted(self.command_permissions(command.qualified_name)),
                "subcommands": subcommands,
                "aliases": sorted(names - {command.qualified_name}),
                "staff": own["staff"],
                "text_only": own["text_only"],
                "children": children,
                "varies": len(needs) > 1,
            })
        rank = {name: index for index, name in enumerate(TAB_ORDER)}
        return [{"category": name, "commands": rows} for name, rows in sorted(groups.items(), key=lambda g: (rank.get(g[0], len(rank)), g[0]))]
