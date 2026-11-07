"""Looking people up without mistaking a gap in the cache for someone leaving.

The member cache can be short for a while: the gateway's ready event doesn't wait past a few seconds for a big server's member list, and an outage leaves a server with no members at all. Code that decides someone has gone, or works out who a change applies to, goes through here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

log = logging.getLogger(__name__)

CHUNK_WAIT = 120.0
# Discord's member count and the list it sends can differ by a few, so an exact match would never come on a big server.
WHOLE_SHARE = 0.99


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
