"""What a person may do in a channel. Commands and dashboard routes both ask here, so the two give one answer.

The bot acts with its own permissions, which are usually far wider than the person asking. Anything it does for someone is first
checked against what Discord would let that person do themselves. The server's owner and Administrators pass every check.
"""
from __future__ import annotations

from typing import Any

import discord


ONLY_ADMINS = "Only the server owner or an Administrator can change this."
NOT_EVERYONE = "@everyone can't hold that. Pick a role."
READS_ALL = "This log copies what's said in every channel it covers, and you can't read all of them. Ask the owner or an Administrator to set it."


def is_admin(member: Any) -> bool:
    return member.guild.owner_id == member.id or member.guild_permissions.administrator


def permissions_in(member: Any, channel: Any) -> Any:
    return _allowed(member, channel)


def _allowed(member: Any, channel: Any) -> Any:
    try:
        return channel.permissions_for(member)
    except discord.ClientException:  # a thread whose parent channel isn't cached has no answer, so it reads as no
        return discord.Permissions.none()


def can_view(member: Any, channel: Any) -> bool:
    """A private thread also shows to whoever was added to it, but the bot doesn't keep that list, so it takes Manage Threads here."""
    if is_admin(member):
        return True
    allowed = _allowed(member, channel)
    return allowed.view_channel and (channel.type != discord.ChannelType.private_thread or allowed.manage_threads)


def can_read(member: Any, channel: Any) -> bool:
    """Seeing a channel isn't enough to read what was said in it before."""
    return can_view(member, channel) and (is_admin(member) or _allowed(member, channel).read_message_history)


def can_post(member: Any, channel: Any) -> bool:
    if is_admin(member):
        return True
    if not can_view(member, channel):
        return False
    allowed = _allowed(member, channel)
    if isinstance(channel, discord.Thread):
        return allowed.send_messages_in_threads and (not channel.locked or allowed.manage_threads)
    return allowed.send_messages


def can_ping(member: Any, channel: Any) -> bool:
    """Whether they could ping @everyone, @here and roles that aren't mentionable there themselves."""
    return is_admin(member) or (can_view(member, channel) and _allowed(member, channel).mention_everyone)


def can_manage_messages(member: Any, channel: Any) -> bool:
    return is_admin(member) or (can_view(member, channel) and _allowed(member, channel).manage_messages)


def view_refusal(member: Any, channel: Any) -> str | None:
    """The sentence to answer with, or None when they may."""
    return None if can_view(member, channel) else "You can't see that channel."


def read_refusal(member: Any, channel: Any) -> str | None:
    return view_refusal(member, channel) or (None if can_read(member, channel) else "You can't read the messages in that channel.")


def post_refusal(member: Any, channel: Any) -> str | None:
    return view_refusal(member, channel) or (None if can_post(member, channel) else "You can't post in that channel.")


def ping_refusal(member: Any, channel: Any) -> str | None:
    return view_refusal(member, channel) or (None if can_ping(member, channel) else "You need Mention Everyone in that channel to ping through the bot.")


def reads_all_but(member: Any, left_out: Any) -> bool:
    """Whether they could read every channel with messages in this server, apart from the ones `left_out` says a setting doesn't cover. A log copies from all of them, so this is what naming one takes."""
    return is_admin(member) or all(can_read(member, channel) for channel in member.guild.channels if hasattr(channel, "history") and not left_out(channel))


def staff_role_refusal(member: Any, role: Any) -> str | None:
    """Whether this person may name a role, or clear one when `role` is None, in a setting that makes it count as staff."""
    if role is not None and role.is_default():
        return NOT_EVERYONE
    return None if is_admin(member) else ONLY_ADMINS
