"""Mass mention: ping every member who holds all of the roles named.

The pings reach only those members. Each message lists exactly their IDs in its allowed mentions, so no role, @everyone or
@here written in a role's name can ping anyone.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.members import _looks_whole
from ..core.module import Module, requires
from .roles import Confirm

log = logging.getLogger(__name__)

MESSAGE_LIMIT = 2000
# Discord takes at most 100 users in one message's allowed mentions.
USERS_PER_MESSAGE = 100
# Above this many members the person confirms the count first.
CONFIRM_OVER = 50
# Above this many members it refuses, since a role ping reaches them in one message.
MOST_MEMBERS = 1000
MAX_ROLES = 25
HEADER_ROOM = 1500


def header_line(roles: list[Any]) -> str:
    """The first line, naming the roles without pinging them. A long list of long names ends with how many more there are."""
    names = [discord.utils.escape_mentions(discord.utils.escape_markdown(role.name)) for role in roles]
    lead = "Pinging every member with all of these roles: " if len(roles) > 1 else "Pinging every member with the role "
    shown: list[str] = []
    for index, name in enumerate(names):
        rest = len(names) - index - 1
        tail = f" and {rest} more" if rest else ""
        if len(lead) + len(", ".join([*shown, name])) + len(tail) + 1 > HEADER_ROOM:
            return f"{lead}{', '.join(shown)} and {len(names) - len(shown)} more."
        shown.append(name)
    return f"{lead}{', '.join(shown)}."


def pack(header: str, user_ids: list[int], limit: int = MESSAGE_LIMIT) -> list[tuple[str, list[int]]]:
    """The messages to send: the header and then mentions, each message under `limit` characters and 100 users, and no mention split across two."""
    out: list[tuple[str, list[int]]] = []
    text, ids = header, []
    for user_id in user_ids:
        mention = f"<@{user_id}>"
        joiner = "\n" if text == header else " "
        if len(text) + len(joiner) + len(mention) >= limit or len(ids) >= USERS_PER_MESSAGE:
            out.append((text, ids))
            text, ids, joiner = "", [], ""
        text = f"{text}{joiner}{mention}"
        ids.append(user_id)
    out.append((text, ids))
    return out


def holders(guild: Any, role_ids: set[int]) -> list[int]:
    """Members who hold every one of these roles, bots left out, in the order the member list gives them."""
    found = []
    for member in guild.members:
        if member.bot:
            continue
        held = getattr(member, "_roles", None)
        held_ids = set(held) if held is not None else {role.id for role in member.roles}
        held_ids.add(guild.id)  # everyone holds @everyone, which the gateway leaves out of a member's roles
        if role_ids <= held_ids:
            found.append(member.id)
    return found


class Mentions(Module):
    key = "mentions"
    category = "Roles"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._running: set[int] = set()

    def _list_ready(self, guild: Any) -> bool:
        return self.bot.members_settled.is_set() and not guild.unavailable and _looks_whole(guild)

    def _room(self, ctx: commands.Context) -> int:
        """How long a message can be. Moved to the bot channel, an answer starts with the author's mention, which takes its room from the text."""
        decision = getattr(ctx, "decision", None)
        if decision is not None and decision.restricted_channel and not decision.redirect:
            return MESSAGE_LIMIT - len(ctx.author.mention) - 1
        return MESSAGE_LIMIT

    def _typed_refusal(self, ctx: commands.Context, roles: list[Any]) -> str | None:
        """A typed name that several roles share could ping the wrong people, so it has to be a mention or an ID instead."""
        if ctx.interaction is not None:
            return None
        typed = ctx.message.content or ""
        for role in roles:
            if str(role.id) in typed:
                continue
            folded = role.name.casefold()
            if sum(1 for other in ctx.guild.roles if other.name.casefold() == folded) > 1:
                return f"Several roles are called {discord.utils.escape_markdown(role.name)[:100]}. Name that one by mention or ID."
        return None

    @commands.hybrid_command(name="mass_mention", aliases=["massmention", "mm"], description="Ping every member who holds all of the roles you name")
    @commands.guild_only()
    @requires("mention_everyone")
    @app_commands.describe(
        role1="Role 1", role2="Role 2", role3="Role 3", role4="Role 4", role5="Role 5", role6="Role 6", role7="Role 7", role8="Role 8",
        role9="Role 9", role10="Role 10", role11="Role 11", role12="Role 12", role13="Role 13", role14="Role 14", role15="Role 15",
        role16="Role 16", role17="Role 17", role18="Role 18", role19="Role 19", role20="Role 20", role21="Role 21", role22="Role 22",
        role23="Role 23", role24="Role 24", role25="Role 25",
    )
    async def mass_mention(
        self, ctx: commands.Context, role1: discord.Role, role2: Optional[discord.Role] = None, role3: Optional[discord.Role] = None,
        role4: Optional[discord.Role] = None, role5: Optional[discord.Role] = None, role6: Optional[discord.Role] = None, role7: Optional[discord.Role] = None,
        role8: Optional[discord.Role] = None, role9: Optional[discord.Role] = None, role10: Optional[discord.Role] = None, role11: Optional[discord.Role] = None,
        role12: Optional[discord.Role] = None, role13: Optional[discord.Role] = None, role14: Optional[discord.Role] = None, role15: Optional[discord.Role] = None,
        role16: Optional[discord.Role] = None, role17: Optional[discord.Role] = None, role18: Optional[discord.Role] = None, role19: Optional[discord.Role] = None,
        role20: Optional[discord.Role] = None, role21: Optional[discord.Role] = None, role22: Optional[discord.Role] = None, role23: Optional[discord.Role] = None,
        role24: Optional[discord.Role] = None, role25: Optional[discord.Role] = None,
    ) -> None:
        named = [role1, role2, role3, role4, role5, role6, role7, role8, role9, role10, role11, role12, role13, role14, role15, role16, role17,
                 role18, role19, role20, role21, role22, role23, role24, role25]
        roles: list[Any] = []
        for role in named:
            if role is not None and all(role.id != other.id for other in roles):
                roles.append(role)
        await self.run(ctx, roles)

    async def run(self, ctx: commands.Context, roles: list[Any]) -> None:
        guild = ctx.guild
        refusal = self._typed_refusal(ctx, roles)
        if refusal:
            await ctx.fail(refusal)
            return
        if guild.id in self._running:
            await ctx.fail("A mass mention is already running in this server. Try again when it finishes.")
            return
        self._running.add(guild.id)  # before the first await, so a second one stops at the check above
        try:
            await self._run(ctx, roles)
        finally:
            self._running.discard(guild.id)

    async def _run(self, ctx: commands.Context, roles: list[Any]) -> None:
        guild = ctx.guild
        # A slash command runs even where its user can't post, and the pings land in that channel.
        refusal = access.post_refusal(ctx.author, ctx.channel) or access.ping_refusal(ctx.author, ctx.channel)
        if refusal:
            await ctx.fail(refusal)
            return
        if not self._list_ready(guild):
            await ctx.fail("The bot is still loading this server's member list after a restart, so it can't tell who holds those roles yet. Try again in a few minutes.")
            return
        targets = holders(guild, {role.id for role in roles})
        if not targets:
            await ctx.fail("Nobody holds all of those roles.")
            return
        if len(targets) > MOST_MEMBERS:
            await ctx.fail(f"That's {len(targets):,} members. A mass mention stops at {MOST_MEMBERS:,}, so ping a role instead.")
            return
        if len(targets) > CONFIRM_OVER:
            view = Confirm(ctx.author.id, yes=f"Yes, ping {len(targets):,} members")
            sent = await ctx.send(f"That pings {len(targets):,} members. Go ahead?", view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            if sent is None and ctx.interaction is None:
                return  # the server mutes this command's answers, so nobody could press the button
            await view.wait()
            if not view.answer:
                await ctx.fail("Nobody was pinged.")
                return
        names = ", ".join(role.name for role in roles)
        await self.bot.activity_log.record(guild.id, self.bot.actor(ctx.author), "mass_mention", detail=f"{names[:400]}: {len(targets):,} members")
        for text, ids in pack(header_line(roles), targets, self._room(ctx)):
            await ctx.send(text, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, replied_user=False, users=[discord.Object(user_id) for user_id in ids]))
