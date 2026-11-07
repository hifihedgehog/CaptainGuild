"""Posting through the bot on a schedule: finding where a post goes, getting a role ready to ping, telling a refusal that lasts from one that can clear up, and checking a channel a form names."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from typing import Any, AsyncIterator, Iterable

import aiohttp
import discord

from .validate import ValidationError

log = logging.getLogger(__name__)

# What the channel pickers offer, plus threads and the text chat inside voice channels. A category or a forum takes no messages.
POSTING_TYPES = {
    discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.voice, discord.ChannelType.stage_voice,
    discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread,
}
NOT_POSTABLE = "That kind of channel can't hold messages. Pick another one."
# Discord's error codes for a refusal of the message itself: an empty message, a bad field, its AutoMod or its link filter. Sending the same thing again can't help.
REFUSED_MESSAGE_CODES = {50006, 50035, 200000, 200001, 240000}
HELD_KEY = "role_mentionable:{}:{}"
# Seconds to wait before each further try at turning a role's mentions back off. A role left open lets any member ping it.
TURN_BACK_WAITS = (2.0, 10.0)
# Discord's update for the turn back reaches the cache a moment after its answer, so for this long the cache can still call the role open.
CACHE_BEHIND_SECONDS = 10.0
TRANSIENT = (discord.HTTPException, aiohttp.ClientError, OSError, asyncio.TimeoutError)

_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()
_turned_off: dict[int, float] = {}


def holds_messages(channel: Any) -> bool:
    return channel.type in POSTING_TYPES


def refused_for_good(error: Exception) -> bool:
    """Whether Discord turned down this message for what it holds. A missing permission, an outage or a lost connection can all clear up."""
    return isinstance(error, discord.HTTPException) and error.code in REFUSED_MESSAGE_CODES


async def find_channel(guild: Any, channel_id: int) -> Any:
    """A channel or thread. An archived thread isn't in the bot's lists, so one that's missing gets one look on Discord before it counts as gone."""
    channel = guild.get_channel_or_thread(channel_id)
    if channel is not None:
        return channel
    try:
        return await guild.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.InvalidData):
        return None


def form_channel(req: Any, value: Any, field: str = "channel_id", *, keeping: int | None = None) -> int:
    """The ID of a channel a dashboard form names for later posts. It must take messages, and they must be able to post there. The channel a row already has only has to be one they can see, so someone can fix a typo on a row an Administrator set up."""
    channel_id = req.channel(value, field)
    if channel_id != keeping:
        req.channel(channel_id, field, posting=True)
        if req.ops.channel(req.guild_id, channel_id)["type"] not in {kind.value for kind in POSTING_TYPES}:
            raise ValidationError({field: NOT_POSTABLE})
    return channel_id


def clipped(lines: list[str], limit: int = 3900) -> str:
    """As many whole lines as fit an embed, and a count of the rest."""
    kept, used = [], 0
    for line in lines:
        used += len(line) + 1
        if used > limit:
            break
        kept.append(line)
    left = len(lines) - len(kept)
    return "\n".join(kept) + (f"\nAnd {left} more. The dashboard lists them all." if left else "")


def needs_ping_right(guild: Any, role_id: int) -> bool:
    """Whether pinging this role takes Mention Everyone. A role that's gone pings nobody, so it doesn't."""
    role = guild.get_role(role_id) if guild else None
    return role is not None and not role.mentionable


def _lock_for(role_id: int) -> asyncio.Lock:
    lock = _locks.get(role_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[role_id] = lock
    return lock


async def _make_mentionable(db: Any, guild: Any, channel: Any, role: Any) -> tuple[bool, bool]:
    """Whether the role can be pinged here, and whether it was changed and has to be changed back."""
    me = guild.me
    key = HELD_KEY.format(guild.id, role.id)
    if await db.scalar("SELECT 1 FROM kv WHERE key = ?", (key,)) is not None and role.mentionable:
        return True, True  # an earlier turn back failed, so this send finishes it
    open_to_all = role.mentionable and time.monotonic() - _turned_off.get(role.id, -CACHE_BEHIND_SECONDS) >= CACHE_BEHIND_SECONDS
    if open_to_all or channel.permissions_for(me).mention_everyone:
        return True, False
    if not me.guild_permissions.manage_roles or role >= me.top_role:
        return False, False
    await db.run("INSERT INTO kv (key, value) VALUES (?, '1') ON CONFLICT (key) DO NOTHING", (key,))  # a process killed before it turns this back off finds it on the next start
    try:
        await role.edit(mentionable=True, reason="Sending an announcement")
    except TRANSIENT as error:
        log.info("Could not make role %s mentionable in %s: %s", role.id, guild.id, error)
        await db.run("DELETE FROM kv WHERE key = ?", (key,))
        return False, False
    return True, True


async def _turn_back_off(db: Any, guild: Any, role: Any) -> None:
    """A failure keeps the mark in kv, so the next send to this role or the next start finishes the job."""
    for wait in (*TURN_BACK_WAITS, None):
        try:
            await role.edit(mentionable=False, reason="Announcement sent")
            break
        except TRANSIENT as error:
            log.warning("Could not turn mentions back off on role %s in %s: %s", role.id, guild.id, error)
            if wait is None:
                return
            await asyncio.sleep(wait)
    now = time.monotonic()
    if len(_turned_off) > 1000:
        for role_id in [role_id for role_id, at in _turned_off.items() if now - at >= CACHE_BEHIND_SECONDS]:
            del _turned_off[role_id]
    _turned_off[role.id] = now
    await db.run("DELETE FROM kv WHERE key = ?", (HELD_KEY.format(guild.id, role.id),))


@contextlib.asynccontextmanager
async def role_pings(db: Any, guild: Any, channel: Any, role_ids: Iterable[int]) -> AsyncIterator[bool]:
    """Holds these roles ready to ping while the body sends, and gives whether every one of them can be pinged.

    A role members can't ping is made mentionable for just that long, unless the bot holds Mention Everyone in the channel. Two announcements to one role at once must not turn its mentions off under each other, so each role has one lock for the whole bot. They're taken in order, so two sends can't wait on each other.
    """
    wanted = sorted(set(role_ids))
    locks = [_lock_for(role_id) for role_id in wanted]
    taken: list[asyncio.Lock] = []
    changed: list[Any] = []
    ready = True
    try:
        for lock, role_id in zip(locks, wanted):
            await lock.acquire()
            taken.append(lock)
            role = guild.get_role(role_id)
            if role is None:
                ready = False
                continue
            pingable, was_changed = await _make_mentionable(db, guild, channel, role)
            ready = ready and pingable
            if was_changed:
                changed.append(role)
        yield ready
    finally:
        try:
            for role in changed:
                await _turn_back_off(db, guild, role)
        finally:  # a lock kept here would stop every later send to this role, and a timer waiting on it would stop every timer
            for lock in taken:
                lock.release()


async def restore_held_roles(db: Any, guild: Any) -> None:
    """Turns mentions back off on any role a process that was killed left mentionable in this server."""
    for row in await db.all("SELECT key FROM kv WHERE key LIKE ?", (HELD_KEY.format(guild.id, "%"),)):
        role_id = int(row["key"].rpartition(":")[2])
        role = guild.get_role(role_id)
        if role is None:
            await db.run("DELETE FROM kv WHERE key = ?", (row["key"],))
            continue
        async with _lock_for(role_id):
            await _turn_back_off(db, guild, role)
