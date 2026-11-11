"""Which invite a join came through, for a greeting that names it.

Discord doesn't say. The bot keeps each invite's use count and reads them all again when someone joins: the invite whose count rose is
theirs. core/stats/invites.py holds that rule for the statistics pack, and this applies it for greetings, which work with the pack off.

Reading invites takes Manage Server and a call to Discord at every join, so counts are kept only for the servers a greeting asks about.
A server's reads go one at a time, and only a few joins wait for one. Past that a join gets no answer, so a raid can't queue calls.
An answer is kept for ten minutes, so the join DM, the picture and a welcome that waits for the rules screen all show the same invite.

A rise can be pinned on a join only when that join is the only one since the counts were last read. Greetings skips the read for a join it
doesn't welcome, so every join is counted as it arrives (`arrived`, which costs nothing), and a read that finds more than its own join since
the last one answers Unknown and keeps the fresh counts for the next. Discord raises an invite's count a moment before the join's event
reaches the bot, so a read first waits SETTLE_SECONDS for the events right behind its own, and counts them too.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime
import logging
import time
from typing import Any

import discord

from .stats.invites import VANITY_PREFIX, InviteState, attribute
from .tagscript.objects import profile_name

log = logging.getLogger(__name__)

KEEP_SECONDS = 600.0
MAX_WAITING = 4   # joins that can wait on one server's read
MAX_KEPT = 5000   # answers held across every server
SETTLE_SECONDS = 1.5  # what a join's read waits for the joins right behind it, well inside the five seconds a welcome waits for its invite


@dataclasses.dataclass(frozen=True)
class Invite:
    """What a greeting can show about an invite. tagscript's objects.invite reads these names."""
    code: str
    uses: int = 0
    max_uses: int = 0   # 0 means no limit
    max_age: int = 0    # seconds, and 0 means it never ends
    temporary: bool = False
    channel_id: int | None = None
    created_at: datetime.datetime | None = None
    expires_at: datetime.datetime | None = None
    inviter_id: int | None = None
    inviter_name: str = ""         # the name on the inviter's own profile: their global name, or their username when they set none
    inviter_username: str = ""
    inviter_global_name: str = ""  # empty when they set none


def _inviter(user: Any) -> dict:
    """The fields that say who made an invite. Nobody made a vanity link, and then they're all empty."""
    if user is None:
        return {}
    return {"inviter_id": getattr(user, "id", None), "inviter_name": profile_name(user), "inviter_username": str(user.name), "inviter_global_name": getattr(user, "global_name", None) or ""}


def _details(item: Any) -> Invite:
    created = getattr(item, "created_at", None)
    max_age = getattr(item, "max_age", None) or 0
    expires = getattr(item, "expires_at", None)
    if expires is None and created is not None and max_age:
        expires = created + datetime.timedelta(seconds=max_age)
    return Invite(
        code=str(item.code), uses=getattr(item, "uses", None) or 0, max_uses=getattr(item, "max_uses", None) or 0, max_age=max_age,
        temporary=bool(getattr(item, "temporary", False)), channel_id=getattr(getattr(item, "channel", None), "id", None), created_at=created, expires_at=expires,
        **_inviter(getattr(item, "inviter", None)),
    )


def _state(details: Invite) -> InviteState:
    return InviteState(details.uses, details.max_uses or None, details.inviter_id, details.temporary)


def sample(guild: Any, channel: Any = None) -> Invite:
    """An invite for a preview or a test, where nobody joined: made by the bot a week ago and used once."""
    made = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
    return Invite(code="sample", uses=1, channel_id=getattr(channel, "id", None), created_at=made, **_inviter(getattr(guild, "me", None)))


class InviteWatch:
    def __init__(self) -> None:
        self.clock = time.monotonic
        self.blocked: set[int] = set()  # servers where the last read was refused for want of Manage Server
        self._watched: set[int] = set()
        self._counts: dict[int, dict[str, InviteState]] = {}
        self._known: dict[int, dict[str, Invite]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._waiting: dict[int, int] = {}
        self._answers: dict[tuple[int, int], tuple[float, Invite | None]] = {}
        self._arrived: dict[int, int] = {}  # people who joined each server since its counts were last read
        self.settle = SETTLE_SECONDS

    def watching(self, guild_id: int) -> bool:
        return guild_id in self._watched

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def _read(self, guild: Any) -> tuple[dict[str, InviteState], dict[str, Invite]] | None:
        """Every invite's count and details, or None when they can't be read right now."""
        me = guild.me
        if me is None:
            return None  # the bot's own member isn't known yet, so its permissions aren't either. The next read finds out.
        if not me.guild_permissions.manage_guild:
            self.blocked.add(guild.id)
            return None
        try:
            found = await guild.invites()
            vanity = await guild.vanity_invite() if "VANITY_URL" in getattr(guild, "features", ()) else None
        except discord.Forbidden:
            self.blocked.add(guild.id)
            return None
        except discord.HTTPException as error:
            log.info("Could not read the invites of %s: %s", guild.id, error)
            return None
        self.blocked.discard(guild.id)
        known = {str(item.code): _details(item) for item in found}
        if vanity is not None and getattr(vanity, "code", None):
            known[VANITY_PREFIX + str(vanity.code)] = _details(vanity)
        return {code: _state(details) for code, details in known.items()}, known

    def _store(self, guild_id: int, read: tuple[dict[str, InviteState], dict[str, Invite]] | None) -> None:
        if read is not None and guild_id in self._watched:  # a server that stopped asking while Discord answered keeps nothing
            self._counts[guild_id], self._known[guild_id] = read

    def arrived(self, member: Any) -> None:
        """Someone joined. Call this for every join, whether or not anything reads the invites for it. A bot comes in without an invite, so it isn't counted."""
        guild_id = member.guild.id
        if guild_id in self._watched and not getattr(member, "bot", False):
            self._arrived[guild_id] = self._arrived.get(guild_id, 0) + 1

    def _since_read(self, guild_id: int) -> int:
        """The joins counted since the last read, which this read takes in."""
        return self._arrived.pop(guild_id, 0)

    def _not_read(self, guild_id: int, joins: int) -> None:
        """A read that failed takes in nothing, so its joins are still waiting for the next."""
        if joins and guild_id in self._watched:
            self._arrived[guild_id] = self._arrived.get(guild_id, 0) + joins

    async def watch(self, guild: Any) -> None:
        """Start keeping a server's counts, or bring them back in step after the bot was away."""
        self._watched.add(guild.id)
        async with self._lock(guild.id):
            joins = self._since_read(guild.id)
            read = await self._read(guild)
            self._store(guild.id, read)
            if read is None:
                self._not_read(guild.id, joins)

    def stop(self, guild_id: int) -> None:
        self._watched.discard(guild_id)
        self._counts.pop(guild_id, None)
        self._known.pop(guild_id, None)
        self._arrived.pop(guild_id, None)
        self.blocked.discard(guild_id)

    def created(self, item: Any) -> None:
        """A new invite, from Discord's own event, so the next join's read finds it in the earlier counts."""
        guild = getattr(item, "guild", None)
        if guild is None or guild.id not in self._counts:
            return
        details = _details(item)
        self._counts[guild.id][details.code] = _state(details)
        self._known[guild.id][details.code] = details

    async def used_by(self, member: Any) -> Invite | None:
        """The invite this member just joined through, or None when it can't be told. Either way the answer is kept for them.
        A bot comes in without an invite, so its answer is None at once, with no read, and the counts and the tally stay for the people's reads.
        Discord's API docs: "Bots are added to guilds through the OAuth2 API, and cannot accept normal invites." (developers/topics/oauth2.mdx,
        Bot vs User Accounts)"""
        guild = member.guild
        key = (guild.id, member.id)
        if getattr(member, "bot", False) or guild.id not in self._watched or self._waiting.get(guild.id, 0) >= MAX_WAITING:
            return self._keep(key, None)
        self._waiting[guild.id] = self._waiting.get(guild.id, 0) + 1
        try:
            # Someone who came in with no invite, through Server Discovery or added by an app, could otherwise be given the invite of whoever
            # joined a moment after them, whose use is already in the counts while their event isn't here yet. Waiting counts that join too.
            if self.settle:
                await asyncio.sleep(self.settle)
            async with self._lock(guild.id):
                before, earlier = self._counts.get(guild.id), self._known.get(guild.id) or {}
                joins = self._since_read(guild.id)
                read = await self._read(guild)
                self._store(guild.id, read)
                if read is None:
                    self._not_read(guild.id, joins)
        finally:
            left = self._waiting.get(guild.id, 1) - 1
            if left > 0:
                self._waiting[guild.id] = left
            else:
                self._waiting.pop(guild.id, None)
        if joins > 1:
            return self._keep(key, None)  # another join's use is in these counts too, and which rise is whose can't be told
        code = attribute(before, read[0] if read is not None else None)
        found = None
        if code is not None and read is not None:
            found = read[1].get(code)
            if found is None and code in earlier:
                # A one-use invite is deleted as it's used, so only the earlier list has it, one use short.
                found = dataclasses.replace(earlier[code], uses=earlier[code].uses + 1)
        return self._keep(key, found)

    def _keep(self, key: tuple[int, int], found: Invite | None) -> Invite | None:
        now = self.clock()
        if len(self._answers) >= MAX_KEPT:
            for old, (until, _) in list(self._answers.items()):
                if until <= now:
                    del self._answers[old]
            while len(self._answers) >= MAX_KEPT:
                del self._answers[next(iter(self._answers))]
        self._answers[key] = (now + KEEP_SECONDS, found)
        return found

    def kept(self, guild_id: int, user_id: int) -> tuple[bool, Invite | None]:
        """Whether an answer is still held for this member, and the answer."""
        held = self._answers.get((guild_id, user_id))
        if held is None:
            return False, None
        if held[0] <= self.clock():
            del self._answers[(guild_id, user_id)]
            return False, None
        return True, held[1]

    def forget(self, guild_id: int, user_id: int) -> None:
        self._answers.pop((guild_id, user_id), None)
