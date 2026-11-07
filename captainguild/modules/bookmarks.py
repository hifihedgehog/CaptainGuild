"""Bookmarks: right-click a message, pick Bookmark This Message, and the bot sends you a copy of it in your DMs.

The copy carries the message's text, its first picture and where it came from. The Delete Bookmark button under it deletes it, and so does
a trash can or red X reaction on it. Nothing is stored: everything the bot needs is in the menu's interaction and in the DM itself.

The menu goes only into servers that switch bookmarks on. The button and the reactions work in DMs, where there's no server to ask, so a
bookmark someone already has can always be deleted.
"""
from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..core.module import Module
from ..core.validate import Field, Schema, boolean

log = logging.getLogger(__name__)

MENU_NAME = "Bookmark This Message"
DELETE_ID = "captainguild:bookmark:delete"
# A reaction can carry the emoji presentation selector or not, so it's taken off before comparing.
SELECTOR = "\N{VARIATION SELECTOR-16}"
DELETE_EMOJI = frozenset({"\N{WASTEBASKET}", "\N{CROSS MARK}"})
IMAGE_TYPES = ("image/png", "image/gif", "image/jpeg", "image/jpg")
INFO_COLOR = 0x23272A
DESCRIPTION_LIMIT = 4096
SWITCHED_OFF = "That's switched off in this server. A manager can switch it on from the dashboard."


def link_text(text: str) -> str:
    """Text for the inside of a [link](url), so a bracket in a server's name can't end the link early."""
    return discord.utils.escape_markdown(text).replace("[", "\\[").replace("]", "\\]")


def bookmark_embeds(message: Any, guild: Any) -> list[discord.Embed]:
    """The copy and its information embed, as the bookmark DM shows them."""
    text = message.content or ""
    copy = discord.Embed(description=text[:DESCRIPTION_LIMIT] if text else "`No message found`", color=discord.Color.blurple())
    copy.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
    info = discord.Embed(title="Information", color=discord.Color(INFO_COLOR), timestamp=discord.utils.utcnow())
    info.add_field(name="Original Message", value=f"[Jump to message]({message.jump_url})", inline=True)
    info.add_field(name="Embeds", value=f"[{len(message.embeds)}]", inline=True)
    info.add_field(name="From", value=f"[{link_text(guild.name)}](https://discord.com/channels/{guild.id}) in <#{message.channel.id}>", inline=True)
    if message.attachments:
        first = message.attachments[0]
        if first.content_type and first.content_type.startswith(IMAGE_TYPES):
            copy.set_image(url=first.url)
        else:
            copy.add_field(name="Attachment included that couldn't be bookmarked", value=f"[Attachment]({first.url})", inline=True)
    return [copy, info]


def done_embed(message: Any) -> discord.Embed:
    embed = discord.Embed(title="Bookmarked!", description="Check Your DMs", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Bookmarked Message", value="Sent to your DM.", inline=True)
    embed.add_field(name="Original Message", value=f"[Jump to message]({message.jump_url})", inline=True)
    return embed


def failed_embed(reason: str) -> discord.Embed:
    return discord.Embed(title="Unable to bookmark this message", description=reason, color=discord.Color.red(), timestamp=discord.utils.utcnow())


def is_bookmark(message: Any) -> bool:
    """Whether a DM the bot sent is a bookmark, by the Delete Bookmark button under it."""
    for row in message.components:
        for item in getattr(row, "children", [row]):
            if getattr(item, "custom_id", None) == DELETE_ID:
                return True
    return False


class DeleteBookmark(discord.ui.View):
    """The button under every bookmark. It has no timeout and is registered once at start, so it works on bookmarks sent before a restart."""

    def __init__(self, module: "Bookmarks") -> None:
        super().__init__(timeout=None)
        self.module = module

    @discord.ui.button(label="Delete Bookmark", style=discord.ButtonStyle.danger, emoji="\N{WASTEBASKET}", custom_id=DELETE_ID)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.module.delete_pressed(interaction)


class Bookmarks(Module):
    key = "bookmarks"
    category = "Utilities"
    command_pack = True
    schema = Schema({"enabled": Field(False, boolean)})

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.menu = app_commands.ContextMenu(name=MENU_NAME, callback=self.bookmark_menu)
        self.menu.guild_only = True

    async def on_load(self) -> None:
        self.bot.tree.add_command(self.menu)
        self.bot.add_view(DeleteBookmark(self))

    def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)

    async def bookmark_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Sends the person a copy of the message. The message is the one Discord resolved for the menu, whose text arrives even without the Message Content intent."""
        if interaction.guild is None or not self.on_in(interaction.guild_id):
            await interaction.response.send_message(SWITCHED_OFF, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.user.send(embeds=bookmark_embeds(message, interaction.guild), view=DeleteBookmark(self))
        except discord.Forbidden:
            await interaction.followup.send(embed=failed_embed("Are your DMs enabled?"), ephemeral=True)
            return
        except discord.HTTPException as error:
            log.info("A bookmark DM to %s was refused: %s", interaction.user.id, error)
            await interaction.followup.send(embed=failed_embed("An unexpected error occurred."), ephemeral=True)
            return
        await interaction.followup.send(embed=done_embed(message), ephemeral=True)

    async def delete_pressed(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.message.delete()
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to delete this message.", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.response.send_message("Failed to delete the bookmark due to an HTTP error.", ephemeral=True)
            return
        await interaction.response.send_message("Bookmark deleted successfully.", ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """A trash can or red X on a bookmark in DMs deletes it. The bot keeps no message cache, so this reads the raw event and asks Discord for the message, which also covers bookmarks sent before a restart."""
        me = self.bot.user
        if payload.guild_id is not None or me is None or payload.user_id == me.id or payload.emoji.id is not None:
            return
        if (payload.emoji.name or "").replace(SELECTOR, "") not in DELETE_EMOJI:
            return
        if payload.message_author_id is not None and payload.message_author_id != me.id:
            return
        channel = self.bot.get_partial_messageable(payload.channel_id, type=discord.ChannelType.private)
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        if message.author.id != me.id or not is_bookmark(message):
            return
        try:
            await message.delete()
        except discord.HTTPException as error:
            log.info("Could not delete bookmark %s: %s", payload.message_id, error)
