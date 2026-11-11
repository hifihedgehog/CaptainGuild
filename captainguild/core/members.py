"""Looking people up without mistaking a gap in the cache for someone leaving.

The member cache can be short for a while: the gateway's ready event doesn't wait past a few seconds for a big server's member list, and an outage leaves a server with no members at all. Code that decides someone has gone, or works out who a change applies to, goes through here.

The list can also run ahead of the events. `State` makes sure a join it already shows still reaches every feature.
"""
from __future__ import annotations

import asyncio
import collections
import datetime
import logging
from typing import Any

import discord
from discord.member import Member
from discord.state import ConnectionState
from discord.utils import parse_time, utcnow

log = logging.getLogger(__name__)

CHUNK_WAIT = 120.0
# Discord's member count and the list it sends can differ by a few, so an exact match would never come on a big server.
WHOLE_SHARE = 0.99
JOINS_KEPT = 50_000  # joins passed on, remembered so a second event for one of them is never passed on again


class State(ConnectionState):
    """discord.py's connection state, with one change. discord.py drops a join event for someone its member list already holds, with no join
    passed on and no count raised (state.py, parse_guild_member_add). A member list that answers before the join's own event, at startup or
    after a reconnect, then hides that join from every feature, and so does a list built before a leave that lists the person again.

    Here such an event is a join, passed on once, when the join came after the server's list was first sent (its GUILD_CREATE): one the list
    shows with the same join time that nothing passed on yet, or one newer than the join the list shows. An event for an older join than the
    list's is dropped. So is one for someone who was in the server before then, since Discord's docs say the event can come for people who
    are already members (gateway-events.mdx, Guild Member Add)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.joins_passed_on: collections.OrderedDict[tuple[int, int, datetime.datetime], None] = collections.OrderedDict()
        self.lists_sent_at: dict[int, datetime.datetime] = {}  # when each server's GUILD_CREATE last came in

    def parse_guild_create(self, data: Any) -> None:
        self.lists_sent_at[int(data["id"])] = utcnow()
        super().parse_guild_create(data)

    def parse_guild_member_add(self, data: Any) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        member_id = int(data["user"]["id"])
        listed = guild.get_member(member_id) if guild is not None else None
        joined = parse_time(data.get("joined_at"))
        if listed is None:
            super().parse_guild_member_add(data)
            if guild is not None and joined is not None:
                self._passed_on((guild.id, member_id, joined))
            return
        sent_at = self.lists_sent_at.get(guild.id)
        if joined is None or sent_at is None or joined <= sent_at or (guild.id, member_id, joined) in self.joins_passed_on:
            log.debug("A join event for %s in %s, who the member list holds from before, was dropped.", member_id, guild.id)
            return
        if listed.joined_at is not None and listed.joined_at > joined:
            log.debug("A join event for %s in %s, older than the join the member list holds, was dropped.", member_id, guild.id)
            return
        member = Member(guild=guild, data=data, state=self)
        if self.member_cache_flags.joined:
            guild._add_member(member)
        if guild._member_count is not None:
            guild._member_count += 1
        self._passed_on((guild.id, member_id, joined))
        self.dispatch("member_join", member)

    def _passed_on(self, join: tuple[int, int, datetime.datetime]) -> None:
        self.joins_passed_on[join] = None
        while len(self.joins_passed_on) > JOINS_KEPT:
            self.joins_passed_on.popitem(last=False)


async def find_member(guild: Any, user_id: int) -> Any:
    """The member, from the cache or else from Discord. None only when Discord says they aren't in the server. Anything else raises, because it says nothing about whether they left."""
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


def _looks_whole(guild: Any) -> bool:
    count = guild.member_count
    return count is not None and len(guild.members) >= count * WHOLE_SHARE


def forget_chunk_request(guild: Any) -> None:
    """discord.py keeps one member list request per server and clears them only on READY. One nothing will answer blocks every later request, so it has to go. Whoever waits on it gets what has arrived so far."""
    requests = getattr(getattr(guild, "_state", None), "_chunk_requests", None)
    request = requests.pop(guild.id, None) if requests else None
    if request is not None:
        request.done()


async def whole_member_list(guild: Any) -> bool:
    """Whether the cache holds the whole server, asking Discord for the rest if it doesn't."""
    if guild.unavailable:
        return False
    if _looks_whole(guild):
        return True
    try:
        await asyncio.wait_for(guild.chunk(), CHUNK_WAIT)
    except (asyncio.TimeoutError, discord.ClientException, OSError) as error:
        log.warning("Couldn't load the member list for %s: %s", guild.id, error)
        forget_chunk_request(guild)
        return False
    return guild.member_count is None or _looks_whole(guild)
