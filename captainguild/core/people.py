"""Who a typed command means. A mention or an ID is exact, and a name can fit several people at once.

Each class here is also a slash option of type user, so Discord's picker stays. Only typed text can be ambiguous.
"""
from __future__ import annotations

import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

_ID_WORD = re.compile(r"<@!?\d{15,21}>|\d{15,21}")
NAMES_LISTED = 5


def refuse_shared_name(ctx: Any, name: str) -> None:
    """The library takes the first member a name fits, and on a big server that can be the wrong person."""
    if _ID_WORD.fullmatch(name):
        return
    fits = [member for member in ctx.guild.members if name in (member.nick, member.global_name, member.name)]
    if len(fits) < 2:
        return
    shown = ", ".join(f"{discord.utils.escape_markdown(member.name)} ({member.id})" for member in fits[:NAMES_LISTED])
    more = f" and {len(fits) - NAMES_LISTED} more" if len(fits) > NAMES_LISTED else ""
    raise commands.BadArgument(f"More than one member goes by {discord.utils.escape_markdown(name)}: {shown}{more}. Use a mention or an ID to pick one.")


class OneMember(commands.MemberConverter, app_commands.Transformer):
    """A member of this server."""

    @property
    def type(self) -> discord.AppCommandOptionType:
        return discord.AppCommandOptionType.user

    async def transform(self, interaction: discord.Interaction, value: Any, /) -> discord.Member:
        if not isinstance(value, discord.Member):
            raise app_commands.TransformerError(value, self.type, self)
        return value

    async def convert(self, ctx: commands.Context, argument: str) -> discord.Member:
        refuse_shared_name(ctx, argument)
        return await super().convert(ctx, argument)


class OnePerson(OneMember):
    """A member, or anyone else by mention or ID, as for a ban on someone outside the server. A name only ever means a member."""

    async def transform(self, interaction: discord.Interaction, value: Any, /) -> Any:
        return value

    async def convert(self, ctx: commands.Context, argument: str) -> Any:
        try:
            return await super().convert(ctx, argument)
        except commands.MemberNotFound:
            if not _ID_WORD.fullmatch(argument):
                raise
        return await commands.UserConverter().convert(ctx, argument)
