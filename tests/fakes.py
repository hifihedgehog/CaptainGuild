"""Stand-ins for Discord objects, so features can be tested without a connection.

A test builds a real Bot with a temporary encrypted database and the module under test, then calls command callbacks and
listeners directly:

    class AutorolesTest(BotTest):
        modules = [Autoroles]

        async def test_add(self):
            ctx = self.context(author=self.admin)
            await self.cog(Autoroles).autorole_add.callback(self.cog(Autoroles), ctx, self.role_member)
            self.assertEqual(self.settings("autoroles")["roles"], [self.role_member.id])

Dashboard endpoints are tested through `await self.client()`, an aiohttp test client signed in as an admin.
"""
from __future__ import annotations

import asyncio
import datetime
import itertools
import time
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any

import discord
from aiohttp.test_utils import TestClient, TestServer

from captainguild.bot import Bot
from captainguild.core.outside import Outside
from captainguild.db import Database
from captainguild.demo import DemoOps
from captainguild.web.api import User
from captainguild.web.app import build_app

Outside.offline = True  # nothing a test runs may reach the internet
KEY = "1" * 64
NOW = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)
_ids = itertools.count(100000000000000000)


def new_id() -> int:
    return next(_ids)


class FakePermissions:
    """Reads like discord.Permissions: attributes, and iteration as (name, value)."""
    ALL = [name for name, _ in discord.Permissions.all()]

    def __init__(self, *names: str) -> None:
        self.names = set(names)
        if "administrator" in self.names:
            self.names |= set(self.ALL)

    def __iter__(self):
        for name in self.ALL:
            yield name, name in self.names

    def __getattr__(self, name: str) -> bool:
        # discord.Permissions lists view_channel under its other name when iterated, and answers to both.
        return name in self.names or {"view_channel": "read_messages", "read_messages": "view_channel"}.get(name) in self.names


class FakeRole:
    def __init__(self, guild: "FakeGuild", name: str, position: int, *permissions: str, managed: bool = False, color: int = 0) -> None:
        self.guild = guild
        self.id = new_id()
        self.name = name
        self.position = position
        self.managed = managed
        self.permissions = FakePermissions(*permissions)
        self.color = discord.Color(color)
        self.mention = f"<@&{self.id}>"
        self.hoist = False
        self.mentionable = False
        self.premium = False
        self.integration = False
        self.created_at = NOW - datetime.timedelta(days=30)

    def is_default(self) -> bool:
        return self.position == 0

    def is_premium_subscriber(self) -> bool:
        return self.premium

    def is_integration(self) -> bool:
        return self.integration

    def is_bot_managed(self) -> bool:
        return self.managed and not self.premium and not self.integration

    @property
    def members(self) -> list["FakeMember"]:
        return [m for m in self.guild.members if self in m.roles]

    def __ge__(self, other: "FakeRole") -> bool:
        return self.position >= other.position

    def __gt__(self, other: "FakeRole") -> bool:
        return self.position > other.position

    def __le__(self, other: "FakeRole") -> bool:
        return self.position <= other.position

    def __lt__(self, other: "FakeRole") -> bool:
        return self.position < other.position

    async def edit(self, *, permissions: Any = None, name: str | None = None, reason: str | None = None, **kwargs: Any) -> None:
        if permissions is not None:
            self.permissions = FakePermissions(*[n for n, on in permissions if on])
        if name is not None:
            self.name = name
        if "color" in kwargs:
            self.color = kwargs["color"]

    def __str__(self) -> str:
        return self.name


class FakeAsset:
    def __init__(self, url: str = "https://cdn.example/avatar.png") -> None:
        self.url = url

    def __str__(self) -> str:
        return self.url


class FakeMember:
    def __init__(self, guild: "FakeGuild", name: str, *roles: FakeRole, bot: bool = False, pending: bool = False, joined: datetime.datetime = NOW, created: datetime.datetime | None = None) -> None:
        self.guild = guild
        self.id = new_id()
        self.name = name
        self.global_name = name
        self.nick = None
        self.bot = bot
        self.pending = pending
        self.joined_at = joined
        self.created_at = created or joined - datetime.timedelta(days=400)
        self.roles = [guild.default_role, *roles]
        self.mention = f"<@{self.id}>"
        self.display_avatar = FakeAsset()
        self.avatar = self.display_avatar
        self.timed_out_until = None
        self.voice = None
        self.dms: list[dict] = []
        self.calls: list[tuple] = []
        guild.members.append(self)

    @property
    def display_name(self) -> str:
        return self.nick or self.name

    @property
    def top_role(self) -> FakeRole:
        return max(self.roles, key=lambda r: r.position)

    @property
    def guild_permissions(self) -> FakePermissions:
        names: set[str] = set()
        for role in self.roles:
            names |= role.permissions.names
        if self.guild.owner_id == self.id:
            names |= set(FakePermissions.ALL)
        return FakePermissions(*names)

    def is_timed_out(self) -> bool:
        return self.timed_out_until is not None

    def get_role(self, role_id: int) -> FakeRole | None:
        return next((r for r in self.roles if r.id == role_id), None)

    async def add_roles(self, *roles: FakeRole, reason: str | None = None, atomic: bool = True) -> None:
        self.calls.append(("add_roles", [r.id for r in roles], reason))
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles: FakeRole, reason: str | None = None, atomic: bool = True) -> None:
        self.calls.append(("remove_roles", [r.id for r in roles], reason))
        self.roles = [r for r in self.roles if r not in roles]

    async def edit(self, **kwargs: Any) -> None:
        self.calls.append(("edit", kwargs))
        if "nick" in kwargs:
            self.nick = kwargs["nick"]
        if "roles" in kwargs:
            self.roles = [self.guild.default_role, *[r for r in kwargs["roles"] if not r.is_default()]]

    async def timeout(self, until: Any, *, reason: str | None = None) -> None:
        self.calls.append(("timeout", until, reason))
        self.timed_out_until = until

    async def send(self, content: str | None = None, **kwargs: Any) -> "FakeMessage":
        self.dms.append({"content": content, **kwargs})
        return FakeMessage(None, self.guild.me, content, **kwargs)

    async def kick(self, *, reason: str | None = None) -> None:
        self.calls.append(("kick", reason))
        self.guild.members.remove(self)

    async def ban(self, *, reason: str | None = None, delete_message_days: int = 0, delete_message_seconds: int = 0) -> None:
        self.calls.append(("ban", reason, delete_message_days or delete_message_seconds))
        self.guild.banned.append(self.id)
        if self in self.guild.members:
            self.guild.members.remove(self)

    def __str__(self) -> str:
        return self.name


class FakeMessage:
    _state = None  # discord.py's real Context reads this when a test runs a command through bot.invoke

    def __init__(self, channel: "FakeChannel | None", author: Any, content: str | None = None, *, embeds: list | None = None, embed: Any = None, **kwargs: Any) -> None:
        self.id = new_id()
        self.channel = channel
        self.guild = channel.guild if channel else None
        self.author = author
        self.content = content or ""
        self.embeds = embeds if embeds is not None else ([embed] if embed else [])
        self.attachments: list = kwargs.get("attachments", [])
        self.stickers: list = []
        self.reactions: list = []
        self.mentions: list = kwargs.get("mentions", [])
        self.role_mentions: list = kwargs.get("role_mentions", [])
        self.mention_everyone = False
        self.components: list = []
        self.created_at = NOW
        self.edited_at = None
        self.pinned = False
        self.webhook_id = None
        self.type = discord.MessageType.default
        self.flags = discord.MessageFlags._from_value(kwargs.get("flags", 0))  # a real Message always has these
        self.reference = None
        self.kwargs = kwargs
        self.jump_url = f"https://discord.com/channels/{self.guild.id if self.guild else '@me'}/{channel.id if channel else 0}/{self.id}"
        self.deleted = False
        self.added_reactions: list = []

    async def edit(self, **kwargs: Any) -> "FakeMessage":
        if "content" in kwargs:
            self.content = kwargs["content"] or ""
        if "embeds" in kwargs:
            self.embeds = kwargs["embeds"]
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]] if kwargs["embed"] else []
        self.kwargs.update(kwargs)
        return self

    async def delete(self, *, delay: float | None = None) -> None:
        self.deleted = True
        if self.channel and self in self.channel.sent:
            self.channel.sent.remove(self)

    async def add_reaction(self, emoji: Any) -> None:
        self.added_reactions.append(str(emoji))

    async def remove_reaction(self, emoji: Any, member: Any) -> None:
        self.added_reactions.append(("removed", str(emoji), member.id))

    async def clear_reactions(self) -> None:
        self.added_reactions = []

    async def pin(self, *, reason: str | None = None) -> None:
        self.pinned = True


class FakeChannel:
    def __init__(self, guild: "FakeGuild", name: str, kind: discord.ChannelType = discord.ChannelType.text, category: "FakeChannel | None" = None, nsfw: bool = False) -> None:
        self.guild = guild
        self.id = new_id()
        self.name = name
        self.type = kind
        self.category = category
        self.category_id = category.id if category else None
        self.position = len(guild.channels)
        self.nsfw = nsfw
        self.topic = None
        self.overwrites: dict = {}
        self.created_at = NOW - datetime.timedelta(days=30)
        self.mention = f"<#{self.id}>"
        self.sent: list[FakeMessage] = []
        self.members: list[FakeMember] = []
        self.calls: list[tuple] = []
        guild.channels.append(self)

    async def send(self, content: str | None = None, **kwargs: Any) -> FakeMessage:
        message = FakeMessage(self, self.guild.me, content, **kwargs)
        self.sent.append(message)
        return message

    def get_partial_message(self, message_id: int) -> FakeMessage:
        return next((m for m in self.sent if m.id == message_id), FakeMessage(self, self.guild.me))

    async def fetch_message(self, message_id: int) -> FakeMessage:
        for message in self.sent:
            if message.id == message_id:
                return message
        raise discord.NotFound(_FakeResponse(404), "Unknown Message")

    async def history(self, limit: int | None = 100, before: Any = None, **kwargs: Any):
        found = [m for m in self.sent if before is None or m.id < before.id]
        for message in reversed(found[-limit:] if limit else found):
            yield message

    async def purge(self, **kwargs: Any) -> list[FakeMessage]:
        self.calls.append(("purge", kwargs))
        return []

    def permissions_for(self, member: Any) -> FakePermissions:
        return member.permissions if isinstance(member, FakeRole) else member.guild_permissions

    async def set_permissions(self, target: Any, **kwargs: Any) -> None:
        self.calls.append(("set_permissions", target, kwargs))
        if "overwrite" in kwargs:
            if kwargs["overwrite"] is None:
                self.overwrites.pop(target, None)
            else:
                self.overwrites[target] = kwargs["overwrite"]

    def overwrites_for(self, target: Any) -> discord.PermissionOverwrite:
        found = self.overwrites.get(target)
        return discord.PermissionOverwrite.from_pair(*found.pair()) if found else discord.PermissionOverwrite()

    def is_nsfw(self) -> bool:
        return self.nsfw

    async def delete(self, *, reason: str | None = None) -> None:
        self.calls.append(("delete", reason))
        if self in self.guild.channels:
            self.guild.channels.remove(self)

    def __str__(self) -> str:
        return self.name


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"


class FakeGuild:
    def __init__(self, name: str = "Test Server") -> None:
        self.id = new_id()
        self.name = name
        self.icon = None
        self.members: list[FakeMember] = []
        self.channels: list[FakeChannel] = []
        self.emojis: list = []
        self.banned: list[int] = []
        self.ban_users: dict[int, Any] = {}
        self.threads: list = []
        self.owner_id = 0
        self.default_role = FakeRole(self, "@everyone", 0)
        self.roles = [self.default_role]
        self.created_at = NOW - datetime.timedelta(days=1000)
        self.me: FakeMember | None = None
        self.audit_entries: list = []
        self.filesize_limit = 10 * 1024 * 1024
        self.unavailable = False

    async def audit_logs(self, *, limit: int | None = 100, action: Any = None, **kwargs: Any):
        """Newest first, as Discord answers. A test appends to audit_entries to say what the log holds."""
        found = [e for e in reversed(self.audit_entries) if action is None or e.action == action]
        for entry in found[:limit]:
            yield entry

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def text_channels(self) -> list[FakeChannel]:
        return [c for c in self.channels if c.type == discord.ChannelType.text]

    @property
    def owner(self) -> FakeMember | None:
        return self.get_member(self.owner_id)

    def role(self, name: str, position: int, *permissions: str, **kwargs: Any) -> FakeRole:
        role = FakeRole(self, name, position, *permissions, **kwargs)
        self.roles.append(role)
        return role

    def get_role(self, role_id: int) -> FakeRole | None:
        return next((r for r in self.roles if r.id == role_id), None)

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return next((c for c in self.channels if c.id == channel_id), None)

    def get_channel_or_thread(self, channel_id: int) -> FakeChannel | None:
        return self.get_channel(channel_id)

    def get_thread(self, thread_id: int) -> Any:
        return next((t for t in self.threads if t.id == thread_id), None)

    async def fetch_channel(self, channel_id: int) -> Any:
        channel = self.get_channel_or_thread(channel_id)
        if channel is None:
            raise discord.NotFound(_FakeResponse(404), "Unknown Channel")
        return channel

    def get_member(self, user_id: int) -> FakeMember | None:
        return next((m for m in self.members if m.id == user_id), None)

    async def fetch_member(self, user_id: int) -> FakeMember:
        member = self.get_member(user_id)
        if member is None:
            raise discord.NotFound(_FakeResponse(404), "Unknown Member")
        return member

    async def ban(self, user: Any, *, reason: str | None = None, delete_message_days: int = 0, delete_message_seconds: int = 0) -> None:
        self.banned.append(user.id)

    async def unban(self, user: Any, *, reason: str | None = None) -> None:
        if user.id not in self.banned:
            raise discord.NotFound(_FakeResponse(404), "Unknown Ban")
        self.banned = [u for u in self.banned if u != user.id]

    async def fetch_ban(self, user: Any) -> Any:
        if user.id not in self.banned:
            raise discord.NotFound(_FakeResponse(404), "Unknown Ban")
        return types.SimpleNamespace(user=user, reason=None)

    async def bans(self, *, limit: int | None = 1000, **kwargs: Any):
        for user_id in self.banned[:limit]:
            yield types.SimpleNamespace(user=self.ban_users.get(user_id) or discord.Object(user_id), reason=None)

    async def bulk_ban(self, users: Any, *, reason: str | None = None, delete_message_seconds: int = 86400) -> Any:
        done = [u for u in users if u.id not in self.banned]
        self.banned.extend(u.id for u in done)
        return types.SimpleNamespace(banned=done, failed=[u for u in users if u not in done])

    async def create_role(self, *, name: str = "new role", permissions: Any = None, reason: str | None = None, **kwargs: Any) -> "FakeRole":
        role = self.role(name, 1)
        role.color = kwargs.get("color", role.color)
        role.hoist = kwargs.get("hoist", role.hoist)
        role.mentionable = kwargs.get("mentionable", role.mentionable)
        return role

    async def create_text_channel(self, name: str, *, overwrites: Any = None, reason: str | None = None, **kwargs: Any) -> FakeChannel:
        channel = FakeChannel(self, name, category=kwargs.get("category"))
        channel.overwrites = overwrites or {}
        return channel

    async def create_category(self, name: str, *, overwrites: Any = None, reason: str | None = None, **kwargs: Any) -> FakeChannel:
        channel = FakeChannel(self, name, discord.ChannelType.category)
        channel.overwrites = overwrites or {}
        return channel

    async def create_voice_channel(self, name: str, *, overwrites: Any = None, reason: str | None = None, **kwargs: Any) -> FakeChannel:
        channel = FakeChannel(self, name, discord.ChannelType.voice, category=kwargs.get("category"))
        channel.overwrites = overwrites or {}
        return channel


class FakeContext:
    """What a command callback needs from its context. `sent` collects everything the command replied."""
    clean_prefix = "!"

    def __init__(self, bot: Any, guild: FakeGuild, author: FakeMember, channel: FakeChannel) -> None:
        self.bot = bot
        self.guild = guild
        self.author = author
        self.channel = channel
        self.interaction = None
        self.prefix = "!"
        self.me = guild.me
        self.message = FakeMessage(channel, author, "")
        self.sent: list[dict] = []
        self.oks: list[str] = []
        self.fails: list[str] = []
        self.reply_options: list[dict] = []  # what each ok or fail asked for besides its text, such as delete_after
        self.decision = None
        self.command = None

    async def send(self, content: str | None = None, **kwargs: Any) -> FakeMessage:
        self.sent.append({"content": content, **kwargs})
        return FakeMessage(self.channel, self.me, content, **kwargs)

    async def ok(self, text: str, **kwargs: Any) -> None:
        self.oks.append(text)
        self.reply_options.append(kwargs)

    async def fail(self, text: str, **kwargs: Any) -> None:
        self.fails.append(text)
        self.reply_options.append(kwargs)

    async def defer(self, *, ephemeral: bool = False) -> None:
        pass

    async def reply(self, content: str | None = None, **kwargs: Any) -> FakeMessage:
        return await self.send(content, **kwargs)


class FakeLogSink:
    def __init__(self) -> None:
        self.posted: list[tuple[int, dict, Any]] = []

    def post(self, channel_id: int | None, embed: Any, *, file: Any = None) -> None:
        if channel_id:
            self.posted.append((channel_id, embed.to_dict() if isinstance(embed, discord.Embed) else embed, file))

    def queued_files(self) -> int:
        return 0

    def queued_bytes(self) -> int:
        return 0

    async def close(self) -> None:
        pass


class TestBot(Bot):
    """The real Bot with no connection. Discord lookups read the fake guild."""

    __test__ = False

    def __init__(self, db: Database, message_db: Database) -> None:
        super().__init__(db, message_db, public_url="https://bot.example")
        self.fake_guilds: list[FakeGuild] = []
        self.logs = FakeLogSink()
        self.ops = DemoOps()
        self.added_views: list = []

    def get_guild(self, guild_id: int, /) -> Any:
        return next((g for g in self.fake_guilds if g.id == guild_id), None)

    def get_channel(self, channel_id: int, /) -> Any:
        for guild in self.fake_guilds:
            channel = guild.get_channel(channel_id)
            if channel:
                return channel
        return None

    @property
    def guilds(self) -> list:
        return self.fake_guilds

    def get_user(self, user_id: int, /) -> Any:
        for guild in self.fake_guilds:
            member = guild.get_member(user_id)
            if member:
                return member
        return None

    def add_view(self, view: Any, *, message_id: int | None = None) -> None:
        self.added_views.append(view)


class BotTest(unittest.IsolatedAsyncioTestCase):
    modules: list = []

    async def asyncSetUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        path = Path(self.folder.name)
        self.db = Database(path / "main.db", KEY)
        self.message_db = Database(path / "messages.db", KEY)
        await self.db.open()
        await self.message_db.open()
        self.bot = TestBot(self.db, self.message_db)
        await self.bot.prepare(self.modules)
        self.guild = FakeGuild()
        self.bot.fake_guilds.append(self.guild)
        self.role_admin = self.guild.role("Admin", 10, "administrator")
        self.role_bot = self.guild.role("Bot", 9, "administrator", managed=True)
        self.role_mod = self.guild.role("Moderator", 5, "manage_messages")
        self.role_member = self.guild.role("Member", 2)
        self.role_muted = self.guild.role("Muted", 1)
        self.me = FakeMember(self.guild, "Bot", self.role_bot, bot=True)
        self.guild.me = self.me
        self.owner = FakeMember(self.guild, "owner", self.role_admin)
        self.guild.owner_id = self.owner.id
        self.admin = FakeMember(self.guild, "admin", self.role_admin)
        self.mod = FakeMember(self.guild, "mod", self.role_mod)
        self.member = FakeMember(self.guild, "member", self.role_member)
        self.channel = FakeChannel(self.guild, "general")
        self._clients: list[TestClient] = []

    async def asyncTearDown(self) -> None:
        for client in self._clients:
            await client.close()
        await self.bot.timers.stop()
        await self.db.close()
        await self.message_db.close()
        self.folder.cleanup()

    def cog(self, module_class: type) -> Any:
        return next(m for m in self.bot.modules if isinstance(m, module_class))

    def context(self, author: FakeMember | None = None, channel: FakeChannel | None = None) -> FakeContext:
        return FakeContext(self.bot, self.guild, author or self.admin, channel or self.channel)

    def settings(self, module: str) -> dict:
        return self.bot.settings.get(self.guild.id, module)

    async def client(self, *, access: str = "full") -> TestClient:
        """An API client signed in as a person with this access to the fake guild."""
        ops = self.bot.ops
        ops.access_for = lambda guild_id, user_id: access if guild_id == self.guild.id else None
        user = User(self.admin.id, self.admin.name, None)
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=user)
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def wait_for_job(self, client: TestClient, tail: str = "/backup/job", seconds: float = 120.0) -> dict:
        """Asks for a background job every 20 ms until it stops running. A slow machine can keep a job going past the dashboard's limit
        on reads, so a refusal waits for the limit's window to pass instead of failing the test."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            response = await client.get(self.path(tail))
            if response.status == 429:
                await asyncio.sleep(1)
                continue
            job = (await response.json())["job"]
            if job and not job["running"]:
                return job
            await asyncio.sleep(0.02)
        self.fail("The job never finished")

    def path(self, tail: str) -> str:
        return f"/api/g/{self.guild.id}{tail}"
