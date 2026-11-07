"""The command context every command receives, for slash, prefix and mention use alike.

It carries the permission decision, so a command just calls `ctx.send` and the server's choices apply: mute output,
redirect output to a channel or a DM, and restricted commands moving to the bot channel.
"""
from __future__ import annotations

from typing import Any

import discord
from discord.ext import commands

from .perms import Decision, answer_place


def unused_words(ctx: Any) -> str:
    """What a typed command left unread. An optional channel that fails to convert leaves its word here and comes through as None, which must not read as "no channel named"."""
    view = getattr(ctx, "view", None)
    return view.read_rest().strip() if view is not None and ctx.interaction is None else ""


CONTENT_ROOM = 2000


class Context(commands.Context):
    decision: Decision | None = None

    async def send(self, content: Any = None, **kwargs: Any) -> Any:
        decision = self.decision
        if decision is None:
            return await super().send(content, **kwargs)
        if decision.muted:
            if self.interaction is None:
                return None
            kwargs["ephemeral"] = True
            return await super().send(content, **kwargs)
        target = decision.redirect or decision.restricted_channel
        if target is None:
            return await super().send(content, **kwargs)
        kwargs.pop("ephemeral", None)
        destination: Any
        if target == "dm":
            destination = self.author
        else:
            destination = answer_place(self.guild, target)
            if destination is None:
                return await super().send(content, **kwargs)
            if decision.restricted_channel and not decision.redirect:
                # Discord refuses a message past its limit, so the mention takes its room from the text.
                mention = self.author.mention
                content = f"{mention} {str(content or '')[:CONTENT_ROOM - len(mention) - 1]}".strip()
        try:
            sent = await destination.send(content, **kwargs)
        except discord.HTTPException:
            return await super().send(content, **kwargs)
        if self.interaction is not None:
            # A deferred command shows "thinking" until something answers it, and an undeferred one fails without an answer.
            if self.interaction.response.is_done():
                await self.interaction.followup.send("Done.", ephemeral=True)
            else:
                await self.interaction.response.send_message("Done.", ephemeral=True)
        return sent

    async def ok(self, text: str, **kwargs: Any) -> Any:
        """A plain success reply. Only the person who ran it sees it on a slash command."""
        kwargs.setdefault("ephemeral", True)
        return await self.send(f"\N{WHITE HEAVY CHECK MARK} {text}", allowed_mentions=discord.AllowedMentions.none(), **kwargs)

    async def fail(self, text: str, **kwargs: Any) -> Any:
        """Tell the person why it didn't work. Bypasses redirects so the answer comes back to them."""
        kwargs.setdefault("ephemeral", True)
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        return await commands.Context.send(self, f"\N{CROSS MARK} {text}", **kwargs)
