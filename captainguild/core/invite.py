"""The link that adds the bot to a server, and the permissions it asks for."""
from __future__ import annotations

import discord

# The bot asks for Administrator. It works across the whole server (roles, channels, messages, members, webhooks and the audit log), and a
# channel hidden from it would quietly lose its logs, its lockdowns and the mute role there. Administrator doesn't change Discord's role
# order: the bot still gives and takes only roles below its own.
ADD_PERMISSIONS = discord.Permissions(administrator=True)


def invite_url(client_id: int | str) -> str:
    return discord.utils.oauth_url(client_id, permissions=ADD_PERMISSIONS, scopes=("bot", "applications.commands"))
