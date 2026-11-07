"""Everything the dashboard asks of Discord, in one interface.

Dashboard endpoints call `bot.ops` and never touch a discord.py object, so the same endpoints run against this real
version, the demo in demo.py and the fake in the tests. Add a method here, in demo.py and in tests/fakes.py together.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import io
import logging
import re
from typing import Any, AsyncIterator, Iterable, Protocol

import aiohttp
import discord

from . import messages
from .validate import Invalid, ValidationError, snowflake, whole_number


log = logging.getLogger(__name__)

MAX_ARCHIVED_THREADS = 256
THREAD_TYPES = (discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread)
TOO_MANY_CONTROLS = "This message has more buttons than Discord allows. Remove a link button."
# Discord's default upload limit for bots and webhooks since September 3, 2026 (its change log). discord.py 2.7.1's table still gives 10 MiB below boost level 2.
DEFAULT_UPLOAD_BYTES = 20 * 1024 * 1024


def upload_limit(guild: Any) -> int:
    """The largest file the bot can upload in a server: its boost level's limit, and never less than Discord's default."""
    return max(getattr(guild, "filesize_limit", 0) or 0, DEFAULT_UPLOAD_BYTES)


class OpsError(Exception):
    """Discord refused or the target is gone. The text is shown to the person."""


class MessageGone(OpsError):
    """The message no longer exists, as opposed to Discord refusing for some other reason."""


class SendUnknown(OpsError):
    """The connection dropped during a send, so the message may or may not have gone out, and trying again could double it."""


def _channel_row(channel: Any) -> dict:
    return {
        "id": channel.id,
        "name": channel.name,
        "type": channel.type.value,
        "parent_id": getattr(channel, "category_id", None),
        "position": getattr(channel, "position", 0),  # threads have none
        "nsfw": bool(getattr(channel, "nsfw", False)),
        "topic": getattr(channel, "topic", None),
        "slowmode": getattr(channel, "slowmode_delay", 0),
    }


def _role_row(role: discord.Role, members: int | None = None) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "color": role.color.value,
        "position": role.position,
        "managed": role.managed,
        "default": role.is_default(),
        "permissions": [name for name, on in role.permissions if on],
        "members": len(role.members) if members is None else members,
    }


def _members_per_role(guild: Any) -> collections.Counter:
    """One pass over the members. Asking each role in turn walks every member once per role, which took 2.2 seconds with 43,000 members and 250 roles, and nothing else in the bot runs meanwhile."""
    counts: collections.Counter = collections.Counter()
    for member in guild.members:
        counts.update(getattr(member, "_roles", None) or (r.id for r in member.roles if not r.is_default()))
    return counts


def require_channel(ops: Any, guild_id: int, value: Any, field: str = "channel_id") -> int:
    """The ID of a channel a browser named, once it's known to belong to this server. Refuses any other server's channel."""
    try:
        channel_id = snowflake(value)
    except Invalid as error:
        raise ValidationError({field: "Pick a channel."}) from error
    if ops.channel(guild_id, channel_id) is None:
        raise ValidationError({field: "That channel isn't in this server, or the bot can't see it."})
    return channel_id


_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):(\d+)>")
# A file's CDN link: /attachments/{channel}/{attachment}/{name}.
_ATTACHMENT_LINK = re.compile(r"/attachments/\d+/(\d+)/")
_EMBEDDABLE_FILE = re.compile(r"\.(?:jpe?g|png|webp|gif)$", re.IGNORECASE)


def _point_at_uploads(payload: dict, uploads: dict[int, str]) -> dict:
    """A copy's embed images that lived in the original's files, pointed at the same files uploaded with the copy."""
    def fix(part: dict, key: str) -> dict:
        match = _ATTACHMENT_LINK.search(part.get(key) or "")
        name = uploads.get(int(match.group(1))) if match else None
        return {**part, key: f"attachment://{name}"} if name else part

    embeds = []
    for embed in payload.get("embeds", []):
        copy = dict(embed)
        for part, key in (("image", "url"), ("thumbnail", "url"), ("footer", "icon_url"), ("author", "icon_url")):
            if part in copy:
                copy[part] = fix(copy[part], key)
        embeds.append(copy)
    return {**payload, "embeds": embeds}


def _member_row(member: Any) -> dict:
    roles = getattr(member, "roles", [])
    return {
        "id": member.id,
        "name": member.name,
        "display_name": member.display_name,
        "avatar": member.display_avatar.url,
        "bot": member.bot,
        "joined_at": member.joined_at.timestamp() if getattr(member, "joined_at", None) else None,
        "created_at": member.created_at.timestamp(),
        "role_ids": [r.id for r in roles if not r.is_default()],
    }


class Ops(Protocol):
    def guild_ids(self) -> list[int]: ...
    def describe_guild(self, guild_id: int) -> dict | None: ...
    def guild_summary(self, guild_id: int) -> dict | None: ...
    def access_for(self, guild_id: int, user_id: int) -> str | None: ...
    def member(self, guild_id: int, user_id: int) -> dict | None: ...
    def search_members(self, guild_id: int, query: str, limit: int = 25) -> list[dict]: ...
    def list_bots(self, guild_id: int, limit: int = 200) -> list[dict]: ...
    async def create_private_category(self, guild_id: int, category: str, names: list[str], *, viewer_roles: list[int] | None = None) -> dict: ...
    def channel(self, guild_id: int, channel_id: int) -> dict | None: ...
    async def channel_messages(self, guild_id: int, channel_id: int, *, limit: int = 50, before: int | None = None) -> dict: ...
    async def send_message(self, channel_id: int, payload: dict, *, ping: bool = False, silent: bool = False, components: list[dict] | None = None, files: list[tuple[str, bytes]] | None = None) -> dict: ...
    async def repost_message(self, source_channel_id: int, source_message_id: int, channel_id: int, payload: dict) -> dict: ...
    async def send_dm(self, user_id: int, payload: dict) -> dict: ...
    async def start_thread(self, channel_id: int, message_id: int, name: str) -> dict: ...
    async def create_channel(self, guild_id: int, name: str, *, read_only: bool = False, private: bool = False, viewer_roles: Iterable[int] = ()) -> dict: ...
    async def create_role(self, guild_id: int, name: str, *, color: int = 0) -> dict: ...
    async def create_mute_role(self, guild_id: int, name: str = "Muted") -> dict: ...
    async def edit_message(self, channel_id: int, message_id: int, payload: dict, *, components: list[dict] | None = None, current: dict | None = None) -> None: ...
    async def find_thread(self, guild_id: int, channel_id: int) -> bool: ...
    async def place_state(self, guild_id: int, channel_id: int) -> str: ...
    def known_thread(self, guild_id: int, channel_id: int) -> Any: ...
    async def delete_message(self, channel_id: int, message_id: int) -> None: ...
    async def fetch_message(self, channel_id: int, message_id: int) -> dict: ...
    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> None: ...
    async def remove_reaction(self, channel_id: int, message_id: int, emoji: str, user_id: int | None = None) -> None: ...
    async def clear_reaction(self, channel_id: int, message_id: int, emoji: str) -> None: ...
    async def clear_reactions(self, channel_id: int, message_id: int) -> None: ...
    async def reaction_users(self, channel_id: int, message_id: int, emoji: str, *, after: int | None = None, limit: int = 100, burst: bool = False) -> list[dict]: ...
    async def set_nickname(self, guild_id: int, nick: str | None) -> None: ...
    async def set_bot_profile(self, guild_id: int, *, avatar: bytes | None = ..., banner: bytes | None = ..., bio: str | None = ...) -> None: ...
    async def ban_list(self, guild_id: int, limit: int = 1000) -> list[dict]: ...
    async def add_member_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str | None = None) -> None: ...
    async def remove_member_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str | None = None) -> None: ...
    async def edit_member_voice(self, guild_id: int, user_id: int, *, mute: bool | None = None, deafen: bool | None = None, disconnect: bool = False, reason: str | None = None) -> bool: ...
    async def open_review(self, guild_id: int, user_id: int, report_id: int | None = None) -> dict: ...


def access_level(*, is_owner: bool, permissions: set[str], role_ids: set[int], staff_roles: set[int], mod_role: int | None) -> str | None:
    """"full" can change settings. "staff" can see cases, reports and logs. None can't open this server at all."""
    if is_owner or "administrator" in permissions or "manage_guild" in permissions:
        return "full"
    if mod_role and mod_role in role_ids:
        return "staff"
    if role_ids & staff_roles:
        return "staff"
    return None


class DiscordOps:
    """The real one, backed by a logged-in bot."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._archived_threads: dict[int, Any] = {}

    def guild_ids(self) -> list[int]:
        return [guild.id for guild in self.bot.guilds]

    def guild_summary(self, guild_id: int) -> dict | None:
        """A server's name and icon, for a list of servers. describe_guild walks every member, and this is read on every page load."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        return {"id": guild.id, "name": guild.name, "icon": guild.icon.url if guild.icon else None}

    def describe_guild(self, guild_id: int) -> dict | None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        me = guild.me
        channels = [_channel_row(c) for c in guild.channels]
        channels.sort(key=lambda c: (c["position"], c["id"]))
        channels += [_channel_row(t) for t in getattr(guild, "threads", [])]  # a picker lists them only when it asks for their types
        counts = _members_per_role(guild)
        roles = [_role_row(r, guild.member_count if r.is_default() else counts[r.id]) for r in reversed(guild.roles)]
        emojis = [{"id": e.id, "name": e.name, "animated": e.animated, "url": e.url, "available": e.available} for e in guild.emojis]
        return {
            "id": guild.id,
            "name": guild.name,
            "icon": guild.icon.url if guild.icon else None,
            "member_count": guild.member_count,
            "owner_id": guild.owner_id,
            "channels": channels,
            "roles": roles,
            "emojis": emojis,
            "me": {
                "id": me.id,
                "name": me.name,
                "nick": me.nick,
                "avatar": me.display_avatar.url,
                "top_role_position": me.top_role.position,
                "permissions": [name for name, on in me.guild_permissions if on],
            },
        }

    def access_for(self, guild_id: int, user_id: int) -> str | None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        member = guild.get_member(user_id)
        if member is None:
            return None
        core = self.bot.settings.get(guild_id, "core")
        commands_settings = self.bot.settings.get(guild_id, "commands")
        return access_level(
            is_owner=guild.owner_id == user_id,
            permissions={name for name, on in member.guild_permissions if on},
            role_ids={r.id for r in member.roles},
            staff_roles=set(core.get("dashboard_staff_roles") or []),
            mod_role=commands_settings.get("mod_role"),
        )

    def member(self, guild_id: int, user_id: int) -> dict | None:
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        return _member_row(member) if member else None

    def search_members(self, guild_id: int, query: str, limit: int = 25) -> list[dict]:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return []
        query = query.strip().lower()
        number = whole_number(query)
        if number is not None:
            member = guild.get_member(number)
            return [_member_row(member)] if member else []
        out = []
        for member in guild.members:
            if query in member.name.lower() or query in member.display_name.lower():
                out.append(_member_row(member))
                if len(out) >= limit:
                    break
        return out

    def list_bots(self, guild_id: int, limit: int = 200) -> list[dict]:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return []
        rows = [_member_row(member) for member in guild.members if member.bot]
        rows.sort(key=lambda row: row["name"].lower())
        return rows[:limit]

    async def create_private_category(self, guild_id: int, category: str, names: list[str], *, viewer_roles: list[int] | None = None) -> dict:
        """A category and text channels only the bot and these roles can see. Everything made is removed again if a step fails."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise OpsError("The bot isn't in that server.")
        # Discord refuses an overwrite that grants a permission the bot doesn't hold itself, so it only asks for what it has.
        held = guild.me.guild_permissions
        wanted = ("view_channel", "send_messages", "embed_links", "attach_files", "read_message_history", "manage_webhooks")
        overwrites: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(**{name: True for name in wanted if getattr(held, name)}),
        }
        for role_id in viewer_roles or []:
            role = guild.get_role(role_id)
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(**{name: True for name in ("view_channel", "read_message_history") if getattr(held, name)})
        made: list[Any] = []
        try:
            parent = await guild.create_category(category, overwrites=overwrites, reason="Set up by a server manager")
            made.append(parent)
            channels: dict[str, int] = {}
            for name in names:
                # Given again here, so channels holding logs are hidden by their own settings and not only by the category's.
                channel = await guild.create_text_channel(name, category=parent, overwrites=overwrites, reason="Set up by a server manager")
                made.append(channel)
                channels[name] = channel.id
        except discord.HTTPException as error:
            for leftover in reversed(made):
                try:
                    await leftover.delete(reason="Setup failed partway")
                except discord.HTTPException:
                    pass
            if isinstance(error, discord.Forbidden):
                raise OpsError("The bot can't create channels here. It needs Manage Channels.") from error
            raise OpsError(f"Discord refused the channel: {error.text or error.status}") from error
        return {"category_id": parent.id, "channels": channels}

    def _messageable(self, channel_id: int) -> Any:
        channel = self.bot.get_channel(channel_id) or self._archived_threads.get(channel_id)
        if channel is None or not hasattr(channel, "send"):
            raise OpsError("That channel doesn't exist or the bot can't see it.")
        return channel

    def channel(self, guild_id: int, channel_id: int) -> dict | None:
        """A channel or thread, only when it belongs to this server. An ID that came from a browser must pass through here."""
        guild = self.bot.get_guild(guild_id)
        channel = (guild.get_channel_or_thread(channel_id) or self.known_thread(guild_id, channel_id)) if guild else None
        return _channel_row(channel) if channel else None

    def known_thread(self, guild_id: int, channel_id: int) -> Any:
        """An archived thread `find_thread` found, which the library's cache doesn't hold."""
        thread = self._archived_threads.get(channel_id)
        return thread if thread is not None and thread.guild.id == guild_id else None

    async def find_thread(self, guild_id: int, channel_id: int) -> bool:
        """Whether this ID is a channel of this server, asking Discord when the cache doesn't hold it. discord.py drops a thread from its cache when the thread archives, and threads archive on their own, so a post in one has to stay reachable."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        if guild.get_channel_or_thread(channel_id) is not None:
            return True
        try:
            found = await guild.fetch_channel(channel_id)
        except (discord.HTTPException, discord.InvalidData):
            return False
        if found.type not in THREAD_TYPES or found.guild.id != guild_id:
            return False
        self._keep_thread(channel_id, found)
        return True

    def _keep_thread(self, channel_id: int, found: Any) -> None:
        self._archived_threads[channel_id] = found
        while len(self._archived_threads) > MAX_ARCHIVED_THREADS:
            self._archived_threads.pop(next(iter(self._archived_threads)))

    async def place_state(self, guild_id: int, channel_id: int) -> str:
        """"here" for a channel or thread of this server, "gone" when Discord says it doesn't exist or it's another server's, and "unknown" when Discord
        couldn't say, as with a refusal or an outage. find_thread reads both of the last two as no. Something that forgets rows for good forgets only on "gone"."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return "unknown"
        if guild.get_channel_or_thread(channel_id) is not None:
            return "here"
        try:
            found = await guild.fetch_channel(channel_id)
        except discord.NotFound:
            return "gone"
        except (discord.HTTPException, discord.InvalidData):
            return "unknown"
        if found.guild.id != guild_id:
            return "gone"
        if found.type in THREAD_TYPES:
            self._keep_thread(channel_id, found)
        return "here"

    @contextlib.asynccontextmanager
    async def _thread_open(self, channel: Any) -> AsyncIterator[None]:
        """Discord refuses a change to a message in an archived thread, so the thread opens for the change and archives again after it."""
        if not getattr(channel, "archived", False):
            yield
            return
        try:
            await channel.edit(archived=False, reason="Changing a message in this thread")
        except discord.Forbidden as error:
            raise OpsError("That thread is archived, and the bot needs Manage Threads to open it for this change.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused to open that thread: {error.text or error.status}") from error
        channel.archived = False
        try:
            yield
        finally:
            try:
                await channel.edit(archived=True, reason="Changing a message in this thread")
            except discord.HTTPException as error:
                log.info("Could not archive thread %s again: %s", channel.id, error)

    async def channel_messages(self, guild_id: int, channel_id: int, *, limit: int = 50, before: int | None = None) -> dict:
        """A channel's messages, newest first, and what the bot may do there."""
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel_or_thread(channel_id) if guild else None
        if channel is None or not hasattr(channel, "history"):
            raise OpsError("That channel doesn't exist or the bot can't see it.")
        rows = []
        try:
            async for message in channel.history(limit=limit, before=discord.Object(before) if before else None):
                rows.append(self._message_row(message, channel_id))
        except discord.Forbidden as error:
            raise OpsError("The bot can't read that channel's history.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused: {error.text or error.status}") from error
        allowed = channel.permissions_for(guild.me)
        return {"messages": rows, "can_delete": allowed.manage_messages, "can_post": allowed.send_messages and allowed.embed_links}

    async def send_message(self, channel_id: int, payload: dict, *, ping: bool = False, silent: bool = False, components: list[dict] | None = None, files: list[tuple[str, bytes]] | None = None) -> dict:
        """`ping` lets the text mention roles and everyone. `silent` mentions nobody, for a message that goes out again and again.

        `components` are a feature's own buttons and menus, in the form messages.component_view reads. `files` are (name, bytes) to upload with it.
        """
        channel = self._messageable(channel_id)
        if silent:
            mentions = discord.AllowedMentions.none()
        else:
            mentions = discord.AllowedMentions.all() if ping else discord.AllowedMentions(everyone=False, roles=False, users=True)
        try:
            kwargs = messages.send_kwargs(payload, allowed_mentions=mentions)
            if components:
                kwargs["view"] = messages.component_view(payload, components)
            if files:
                kwargs["files"] = [discord.File(io.BytesIO(body), name) for name, body in files]
            sent = await channel.send(**kwargs)
        except discord.Forbidden as error:
            raise OpsError("The bot can't post in that channel. Check its View Channel, Send Messages and Embed Links permissions.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the message: {error.text or error.status}") from error
        except ValueError as error:  # the library counts a view's items as they're added
            raise OpsError(TOO_MANY_CONTROLS) from error
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            raise SendUnknown("The connection to Discord dropped during the send, so it may or may not have gone out.") from error
        return {"id": sent.id, "channel_id": channel_id, "url": sent.jump_url}

    async def repost_message(self, source_channel_id: int, source_message_id: int, channel_id: int, payload: dict) -> dict:
        """Post a copy of a message as this bot, pinging nobody. Its files are uploaded again, because links to them stop working once the original is deleted."""
        source = self._messageable(source_channel_id)
        channel = self._messageable(channel_id)
        limit = upload_limit(getattr(channel, "guild", None))
        files: list[discord.File] = []
        uploads: dict[int, str] = {}
        skipped: list[str] = []
        try:
            original = await source.fetch_message(source_message_id)
            for attachment in original.attachments:
                if attachment.size > limit:
                    skipped.append(attachment.filename)
                    continue
                try:
                    files.append(await attachment.to_file(spoiler=attachment.is_spoiler()))  # discord.py takes the spoiler off unless told
                except discord.HTTPException:
                    skipped.append(attachment.filename)
                    continue
                if _EMBEDDABLE_FILE.search(attachment.filename):
                    uploads[attachment.id] = attachment.filename
            kwargs = messages.send_kwargs(_point_at_uploads(payload, uploads), allowed_mentions=discord.AllowedMentions.none())
            if files:
                kwargs["files"] = files
            sent = await channel.send(**kwargs)
        except discord.NotFound as error:
            raise MessageGone("The original message no longer exists.") from error
        except discord.Forbidden as error:
            raise OpsError("The bot can't post in that channel or read the original. Check its View Channel, Read Message History, Send Messages, Embed Links and Attach Files permissions.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the message: {error.text or error.status}") from error
        return {"id": sent.id, "channel_id": channel_id, "url": sent.jump_url, "payload": messages.from_message(sent), "skipped": skipped}

    async def start_thread(self, channel_id: int, message_id: int, name: str) -> dict:
        """A public thread on a message."""
        channel = self._messageable(channel_id)
        try:
            thread = await channel.get_partial_message(message_id).create_thread(name=name[:100])
        except discord.Forbidden as error:
            raise OpsError("The bot can't open a thread there. It needs Create Public Threads in that channel.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the thread: {error.text or error.status}") from error
        return {"id": thread.id, "name": thread.name}

    async def send_dm(self, user_id: int, payload: dict) -> dict:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            sent = await user.send(**messages.send_kwargs(payload, allowed_mentions=discord.AllowedMentions.none()))
        except discord.Forbidden as error:
            raise OpsError("That person doesn't take DMs from this server.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the DM: {error.text or error.status}") from error
        return {"id": sent.id}

    async def _new_role(self, guild: Any, name: str, color: int, reason: str) -> discord.Role:
        try:
            return await guild.create_role(name=name, color=discord.Color(color), permissions=discord.Permissions.none(), reason=reason)
        except discord.Forbidden as error:
            raise OpsError("The bot can't create roles here. It needs Manage Roles.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the role: {error.text or error.status}") from error

    async def create_role(self, guild_id: int, name: str, *, color: int = 0) -> dict:
        """A new role with no permissions of its own, at the bottom of the list."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise OpsError("The bot isn't in that server.")
        return _role_row(await self._new_role(guild, name, color, "Quick setup"))

    async def create_mute_role(self, guild_id: int, name: str = "Muted") -> dict:
        """A new role with no permissions, held back from talking in every channel the same way /muterole create does it."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise OpsError("The bot isn't in that server.")
        role = await self._new_role(guild, name, 0, "Quick setup")
        moderation = self.bot.module("moderation")
        if moderation is not None:
            await moderation.apply_mute_everywhere(guild, role, "Quick setup")
        return _role_row(role)

    async def create_channel(self, guild_id: int, name: str, *, read_only: bool = False, private: bool = False, viewer_roles: Iterable[int] = ()) -> dict:
        """A new text channel. `read_only` leaves members able to read and react, and only the bot able to post. `private` hides it from everyone but the bot, those who can see all channels, and the `viewer_roles`."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise OpsError("The bot isn't in that server.")
        overwrites = {}
        if private:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True),
            }
            for role_id in viewer_roles:
                role = guild.get_role(int(role_id))
                if role is not None and not role.is_default():
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
        elif read_only:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(send_messages=False, send_messages_in_threads=False, create_public_threads=False, create_private_threads=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, read_message_history=True),
            }
        try:
            channel = await guild.create_text_channel(name, overwrites=overwrites)
        except discord.Forbidden as error:
            raise OpsError("The bot can't create channels here. It needs Manage Channels.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the channel: {error.text or error.status}") from error
        return _channel_row(channel)

    async def edit_message(self, channel_id: int, message_id: int, payload: dict, *, components: list[dict] | None = None, current: dict | None = None) -> None:
        """Discord replaces a message's whole set of controls on an edit. Left out, `components` become whatever reaction roles keep on this message, so an edit from any page leaves them working.

        `current` is the message as `fetch_message` read it. Pass it from anywhere that edits a message it knows nothing else about: a giveaway's Enter button or a vote's buttons then stay, since the edit leaves the controls alone.
        """
        channel = self._messageable(channel_id)
        if components is None:
            keeper = self.bot.module("reactionroles")
            components = await keeper.components_for(message_id) if keeper else []
        foreign_controls = bool(not components and current and current.get("controls"))
        if foreign_controls and (payload.get("buttons") or []) != (current["payload"].get("buttons") or []):
            raise OpsError("This message also has buttons or menus from another feature, and Discord replaces them all together. Leave its link buttons as they are, and the rest will keep working.")
        try:
            message = channel.get_partial_message(message_id)
            kwargs = messages.send_kwargs(payload, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
            if foreign_controls:
                kwargs.pop("view", None)
            else:
                kwargs["view"] = messages.component_view(payload, components) if components else messages.link_view(payload)
            async with self._thread_open(channel):
                await message.edit(**kwargs)
        except discord.NotFound as error:
            raise MessageGone("That message no longer exists.") from error
        except discord.Forbidden as error:
            raise OpsError("Discord only lets a bot edit its own messages, and this one has another author. Repost it with the bot instead.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the edit: {error.text or error.status}") from error
        except ValueError as error:  # the library counts a view's items as they're added
            raise OpsError(TOO_MANY_CONTROLS) from error

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        channel = self._messageable(channel_id)
        try:
            async with self._thread_open(channel):
                await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass
        except discord.Forbidden as error:
            raise OpsError("The bot can't delete that message. It needs Manage Messages in that channel.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the delete: {error.text or error.status}") from error

    async def fetch_message(self, channel_id: int, message_id: int) -> dict:
        channel = self._messageable(channel_id)
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound as error:
            raise MessageGone("That message doesn't exist.") from error
        except discord.Forbidden as error:
            raise OpsError("The bot can't read that channel's history.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused: {error.text or error.status}") from error
        return self._message_row(message, channel_id)

    def _message_row(self, message: Any, channel_id: int) -> dict:
        controls = 0  # buttons and menus answer to the bot that made them, so a repost can't carry them
        for row in getattr(message, "components", None) or []:
            for child in getattr(row, "children", [row]):
                if not getattr(child, "url", None):
                    controls += 1
        return {
            "id": message.id,
            "channel_id": channel_id,
            # A thread isn't in the channel list the dashboard holds, so the row says what it is called.
            "channel_name": getattr(message.channel, "name", None),
            "author_id": message.author.id,
            "author_name": message.author.display_name,
            "author_bot": bool(message.author.bot),
            "mine": message.author.id == self.bot.user.id,
            "system": message.type not in (discord.MessageType.default, discord.MessageType.reply),
            "created_at": message.created_at.timestamp(),
            "url": message.jump_url,
            "payload": messages.from_message(message),
            "reactions": [{"emoji": str(r.emoji), "count": r.count, "me": bool(r.me)} for r in message.reactions],
            "attachments": [{"filename": a.filename, "url": a.url, "size": a.size, "content_type": a.content_type} for a in message.attachments],
            "controls": controls,
            "stickers": len(getattr(message, "stickers", None) or []),
        }

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        channel = self._messageable(channel_id)
        try:
            await channel.get_partial_message(message_id).add_reaction(emoji)
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the reaction {emoji}: {error.text or error.status}") from error

    async def remove_reaction(self, channel_id: int, message_id: int, emoji: str, user_id: int | None = None) -> None:
        """Take one person's reaction off a message, or the bot's own when no person is named. A reaction already gone counts as done."""
        channel = self._messageable(channel_id)
        who = discord.Object(user_id) if user_id else self.bot.user
        try:
            await channel.get_partial_message(message_id).remove_reaction(emoji, who)
        except discord.NotFound:
            pass
        except discord.Forbidden as error:
            raise OpsError("The bot can't remove reactions there. It needs Manage Messages in that channel.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused to remove the reaction {emoji}: {error.text or error.status}") from error

    async def clear_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        """Take everyone's reaction with this emoji off a message. Without Manage Messages, only the bot's own comes off."""
        channel = self._messageable(channel_id)
        try:
            await channel.get_partial_message(message_id).clear_reaction(emoji)
        except discord.NotFound:
            pass
        except discord.Forbidden:
            await self.remove_reaction(channel_id, message_id, emoji)
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused to clear the reaction {emoji}: {error.text or error.status}") from error

    async def clear_reactions(self, channel_id: int, message_id: int) -> None:
        channel = self._messageable(channel_id)
        try:
            await channel.get_partial_message(message_id).clear_reactions()
        except discord.NotFound:
            pass
        except discord.Forbidden as error:
            raise OpsError("The bot can't clear reactions there. It needs Manage Messages in that channel.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused to clear the reactions: {error.text or error.status}") from error

    async def reaction_users(self, channel_id: int, message_id: int, emoji: str, *, after: int | None = None, limit: int = 100, burst: bool = False) -> list[dict]:
        """One page of who reacted with this emoji, lowest ID first. Discord lists super reactions separately."""
        self._messageable(channel_id)
        custom = _CUSTOM_EMOJI.fullmatch(emoji)
        text = f"{custom.group(1)}:{custom.group(2)}" if custom else emoji
        try:
            rows = await self.bot.http.get_reaction_users(channel_id, message_id, text, limit, after=after, type=1 if burst else 0)
        except discord.NotFound as error:
            raise MessageGone("That message or reaction no longer exists.") from error
        except discord.Forbidden as error:
            raise OpsError("The bot can't read that channel's history.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused: {error.text or error.status}") from error
        return [{"id": int(row["id"]), "bot": bool(row.get("bot"))} for row in rows]

    async def set_nickname(self, guild_id: int, nick: str | None) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise OpsError("The bot isn't in that server.")
        try:
            await guild.me.edit(nick=nick or None)
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the nickname: {error.text or error.status}") from error

    async def set_bot_profile(self, guild_id: int, *, avatar: Any = ..., banner: Any = ..., bio: Any = ...) -> None:
        """Modify Current Member takes avatar, banner and bio since September 2025. discord.py 2.7 has no helper for it, so this calls the route."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise OpsError("The bot isn't in that server.")
        body: dict[str, Any] = {}
        if avatar is not ...:
            body["avatar"] = discord.utils._bytes_to_base64_data(avatar) if avatar else None
        if banner is not ...:
            body["banner"] = discord.utils._bytes_to_base64_data(banner) if banner else None
        if bio is not ...:
            body["bio"] = bio or None
        route = discord.http.Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild_id)
        try:
            await self.bot.http.request(route, json=body)
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the profile change: {error.text or error.status}") from error

    async def ban_list(self, guild_id: int, limit: int = 1000) -> list[dict]:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return []
        out = []
        async for entry in guild.bans(limit=limit):
            out.append({"id": entry.user.id, "name": entry.user.name, "reason": entry.reason, "avatar": entry.user.display_avatar.url})
        return out

    def _member_and_role(self, guild_id: int, user_id: int, role_id: int) -> tuple[Any, Any]:
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        if member is None:
            raise OpsError("That person isn't in this server.")
        role = guild.get_role(role_id)
        if role is None:
            raise OpsError("That role no longer exists.")
        return member, role

    async def add_member_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str | None = None) -> None:
        member, role = self._member_and_role(guild_id, user_id, role_id)
        try:
            await member.add_roles(role, reason=reason)
        except discord.Forbidden as error:
            raise OpsError("The bot can't give that role. It needs Manage Roles, and its own role must sit above that one.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the role: {error.text or error.status}") from error

    async def remove_member_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str | None = None) -> None:
        member, role = self._member_and_role(guild_id, user_id, role_id)
        try:
            await member.remove_roles(role, reason=reason)
        except discord.Forbidden as error:
            raise OpsError("The bot can't take that role. It needs Manage Roles, and its own role must sit above that one.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the role: {error.text or error.status}") from error

    async def edit_member_voice(self, guild_id: int, user_id: int, *, mute: bool | None = None, deafen: bool | None = None, disconnect: bool = False, reason: str | None = None) -> bool:
        """Change someone's voice state. Discord only allows this while they're connected, so False means they weren't."""
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        if member is None or member.voice is None:
            return False
        changes: dict[str, Any] = {}
        if mute is not None:
            changes["mute"] = mute
        if deafen is not None:
            changes["deafen"] = deafen
        if disconnect:
            changes["voice_channel"] = None
        try:
            if changes:
                await member.edit(reason=reason, **changes)
        except discord.Forbidden as error:
            raise OpsError("The bot can't do that. It needs Mute Members, Deafen Members and Move Members.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused: {error.text or error.status}") from error
        return True

    async def open_review(self, guild_id: int, user_id: int, report_id: int | None = None) -> dict:
        """Open a member's review thread, which takes several Discord calls that belong to the reports feature."""
        reports = self.bot.module("reports")
        if reports is None:
            raise OpsError("The reports feature isn't loaded.")
        return await reports.open_review_by_id(guild_id, user_id, report_id)
