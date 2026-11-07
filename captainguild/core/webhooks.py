"""Posting under another name and picture: a moved message under its author's, a feed entry under the feed's, a copied channel under each
author's. Each channel gets one webhook of the bot's own, found again or made on first use and then kept. A thread's posts go through its
parent channel's webhook with the thread named. A webhook someone deleted is made again once.

A message a webhook posted can be edited or deleted later only through that same webhook, so `partial` rebuilds one from a stored ID and token.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Iterable

import discord
from discord.utils import MISSING

from .. import brand
from .ops import OpsError

NONE = discord.AllowedMentions.none()
NAME_LENGTH = 80
UNKNOWN_WEBHOOK = 10015
# Discord refuses a webhook post whose name holds either word, in any case.
_RESERVED = {"discord": "disc0rd", "clyde": "clyd3"}
_RESERVED_PATTERN = re.compile("|".join(_RESERVED), re.I)


def safe_name(name: str | None, fallback: str = "Member") -> str:
    """A name Discord takes for a webhook post: 1 to 80 characters, with the reserved words spelled so they pass."""
    cleaned = _RESERVED_PATTERN.sub(lambda match: _RESERVED[match.group(0).lower()], (name or "").strip())
    return cleaned[:NAME_LENGTH] or fallback


def parent_of(channel: Any) -> Any:
    return channel.parent if isinstance(channel, discord.Thread) else channel


class Webhooks:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._hooks: dict[int, discord.Webhook] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def hook(self, channel: Any) -> discord.Webhook:
        """The bot's own webhook in this channel, or in a thread's parent. Raises OpsError with a sentence for a person."""
        parent = parent_of(channel)
        if parent is None:
            raise OpsError("That thread's channel is gone.")
        async with self._locks.setdefault(parent.id, asyncio.Lock()):
            found = self._hooks.get(parent.id)
            if found is not None:
                return found
            me = self.bot.user
            try:
                for existing in await parent.webhooks():
                    if existing.user is not None and me is not None and existing.user.id == me.id and existing.token:
                        found = existing
                        break
                if found is None:
                    found = await parent.create_webhook(name=brand.NAME, reason="For posts under another name")
            except discord.Forbidden as error:
                raise OpsError("The bot needs Manage Webhooks in that channel.") from error
            except discord.HTTPException as error:
                raise OpsError(f"Discord refused the webhook: {error.text or error.status}") from error
            self._hooks[parent.id] = found
            return found

    def forget(self, channel: Any) -> None:
        parent = parent_of(channel)
        if parent is not None:
            self._hooks.pop(parent.id, None)

    async def send(self, channel: Any, *, name: str | None, avatar_url: str | None = None, content: str | None = None, embeds: Iterable[discord.Embed] = (),
                   files: Iterable[discord.File] = (), allowed_mentions: discord.AllowedMentions = NONE, poll: Any = MISSING) -> discord.WebhookMessage:
        """Posts in a channel or thread under this name and picture, pinging nobody unless told to. Raises OpsError with a sentence for a person."""
        embeds, files = list(embeds), list(files)
        thread = channel if isinstance(channel, discord.Thread) else MISSING
        for attempt in (1, 2):
            hook = await self.hook(channel)
            try:
                return await hook.send(
                    content=content or MISSING, username=safe_name(name, brand.NAME), avatar_url=avatar_url or MISSING, embeds=embeds, files=files,
                    thread=thread, allowed_mentions=allowed_mentions, poll=poll, wait=True,
                )
            except discord.NotFound as error:
                if error.code != UNKNOWN_WEBHOOK or attempt == 2:
                    raise OpsError(f"Discord refused the post: {error.text or error.status}") from error
                self.forget(channel)  # someone deleted the webhook, so one more is made
                for file in files:
                    file.reset()
            except discord.HTTPException as error:
                raise OpsError(f"Discord refused the post: {error.text or error.status}") from error
        raise AssertionError("unreachable")

    def partial(self, webhook_id: int, token: str) -> discord.Webhook:
        """A webhook from its stored ID and token, to edit or delete what it posted. Any webhook works, the bot's own or one another bot made."""
        return discord.Webhook.partial(webhook_id, token, client=self.bot)

    async def edit(self, webhook: discord.Webhook, message_id: int, *, thread_id: int | None = None, content: Any = MISSING, embeds: Any = MISSING,
                   attachments: Any = MISSING, allowed_mentions: discord.AllowedMentions = NONE) -> discord.WebhookMessage:
        try:
            return await webhook.edit_message(message_id, content=content, embeds=embeds, attachments=attachments, allowed_mentions=allowed_mentions,
                                              thread=discord.Object(thread_id) if thread_id else MISSING)
        except discord.NotFound as error:
            raise OpsError("That message or the webhook that posted it is gone.") from error
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused the edit: {error.text or error.status}") from error

    async def delete(self, webhook: discord.Webhook, message_id: int, *, thread_id: int | None = None) -> None:
        """Deletes a message the webhook posted. One already gone counts as done."""
        try:
            await webhook.delete_message(message_id, thread=discord.Object(thread_id) if thread_id else MISSING)
        except discord.NotFound:
            pass
        except discord.HTTPException as error:
            raise OpsError(f"Discord refused to delete it: {error.text or error.status}") from error
