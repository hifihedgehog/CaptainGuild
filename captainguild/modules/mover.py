"""The message mover: members move or copy their own message, and moderators any message, to another channel or thread.

The copy is posted through one of the bot's own webhooks under the author's name and picture, with its text, files and embeds, and it pings
nobody. Whoever moved or copied it can edit or delete it later through that same webhook, so each copy keeps the webhook's ID and token. The
token never leaves the database: backups seal it, and nothing shows it on a page, in a log or in a person's data download.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.module import Module, requires, staff_only
from ..core.ops import MessageGone, OpsError, upload_limit
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.validate import Field, Schema, boolean, optional, snowflake
from ..web.api import ApiError
from .embeds import jump_url

log = logging.getLogger(__name__)

PRUNE_SECONDS = 600  # a prune answers through the command's own reply, which lasts 15 minutes
PAGE_PRUNE_SECONDS = 50  # the dashboard waits on its answer
PRUNE_REFUSED = "Only the server owner or an Administrator can prune these records."
LISTED = 500
EDIT_FORM_MAX = 2000
PICK_SECONDS = 60
OFF = "That's switched off in this server. A manager can switch it on from the dashboard."
DESTINATIONS = [discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread]


async def _answer(interaction: discord.Interaction, text: str) -> None:
    """A private answer, whether or not the interaction has had its first one."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as error:
        log.info("Could not answer a message mover action: %s", error)


def what_carries(message: Any) -> tuple[list[discord.Embed], list[str]]:
    """The embeds a repost carries, and the parts of the message it can't. A link's preview isn't carried, since Discord makes it again from the link."""
    embeds = [embed for embed in message.embeds if embed.type == "rich"]
    lost = []
    if getattr(message, "stickers", None):
        lost.append("its sticker")
    if getattr(message, "poll", None) is not None:
        lost.append("its poll")
    if getattr(message, "components", None):
        lost.append("its buttons")
    return embeds, lost


def carried_text(content: str | None, embeds: list[discord.Embed]) -> str:
    """Every piece of text a copy shows, for automod's rules."""
    parts = [content or ""]
    for embed in embeds:
        parts += [embed.title or "", embed.description or "", embed.footer.text or "", embed.author.name or ""]
        parts += [part for field in embed.fields for part in (field.name or "", field.value or "")]
    return "\n".join(part for part in parts if part)


class DestinationSelect(discord.ui.ChannelSelect):
    def __init__(self, cog: "Mover", message: discord.Message, copy: bool) -> None:
        super().__init__(channel_types=DESTINATIONS, placeholder="Select a destination channel or thread", min_values=1, max_values=1)
        self.cog = cog
        self.message = message
        self.copy = copy
        self.used = False

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.used:  # a second pick while the first one runs
            await _answer(interaction, "That message is already on its way.")
            return
        if not self.values:
            await _answer(interaction, "Pick a channel or thread.")
            return
        self.used = True
        target = await self.cog.place(interaction.guild, self.values[0].id)
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            self.used = False
            await _answer(interaction, "Invalid target type. Please select a text channel or thread.")
            return
        if self.view is not None:
            self.view.stop()
        await interaction.response.edit_message(content=f"{'Copying' if self.copy else 'Moving'} the message to {target.mention}...", view=None)
        await _answer(interaction, await self.cog.transfer(interaction.user, self.message, target, copy=self.copy))


class DestinationView(discord.ui.View):
    def __init__(self, cog: "Mover", message: discord.Message, copy: bool) -> None:
        super().__init__(timeout=PICK_SECONDS)
        self.add_item(DestinationSelect(cog, message, copy))


class EditForm(discord.ui.Modal):
    """Edit Moved Message: the copy's text in one box. Leaving it empty takes the text off, when the copy has something else to show."""

    def __init__(self, cog: "Mover", message: discord.Message) -> None:
        super().__init__(title="Edit Moved Message")
        self.cog = cog
        self.message = message
        self.box = discord.ui.TextInput(label="New Message Content", style=discord.TextStyle.paragraph, default=(message.content or "")[:EDIT_FORM_MAX] or None, max_length=EDIT_FORM_MAX, required=False)
        self.add_item(self.box)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_edit(interaction, self.message, self.box.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("The moved message edit form failed", exc_info=error)
        await _answer(interaction, "Something went wrong with that form. It's been logged.")


class Mover(Module):
    key = "mover"
    category = "Message mover"
    command_pack = True
    schema = Schema({
        "enabled": Field(False, boolean),
        "log_channel": Field(None, optional(snowflake)),
    })
    migrations = [
        """
        CREATE TABLE moved_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            thread_id INTEGER,
            user_id INTEGER NOT NULL,
            author_id INTEGER,
            source_channel_id INTEGER,
            source_message_id INTEGER,
            copied INTEGER NOT NULL DEFAULT 0,
            webhook_id INTEGER NOT NULL,
            webhook_token TEXT NOT NULL,
            imported INTEGER NOT NULL DEFAULT 0,
            moved_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX moved_messages_message ON moved_messages (guild_id, message_id);
        CREATE INDEX moved_messages_channel ON moved_messages (guild_id, channel_id);
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._menus = [
            app_commands.ContextMenu(name="Move Message", callback=self.menu_move),
            app_commands.ContextMenu(name="Copy Message", callback=self.menu_copy),
            app_commands.ContextMenu(name="Edit Moved Message", callback=self.menu_edit),
            app_commands.ContextMenu(name="Delete Moved Message", callback=self.menu_delete),
        ]

    async def on_load(self) -> None:
        for menu in self._menus:
            self.bot.tree.add_command(menu)

    def cog_unload(self) -> None:
        for menu in self._menus:
            self.bot.tree.remove_command(menu.name, type=menu.type)

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {"log_channel": {settings["log_channel"]} if settings.get("log_channel") else set()}

    # ---- the store ----

    async def record_of(self, guild_id: int, message_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM moved_messages WHERE guild_id = ? AND message_id = ?", (guild_id, message_id))

    async def _forget(self, guild_id: int, message_ids: list[int]) -> None:
        if message_ids:
            marks = ",".join("?" * len(message_ids))
            await self.db.run(f"DELETE FROM moved_messages WHERE guild_id = ? AND message_id IN ({marks})", (guild_id, *message_ids))

    # A copy that's deleted, or whose channel or thread is deleted, can't be edited again, so its record goes. This is the pack's own bookkeeping, so it runs with the pack off too.
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id:
            await self._forget(payload.guild_id, [payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id:
            await self._forget(payload.guild_id, list(payload.message_ids))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        await self.db.run("DELETE FROM moved_messages WHERE guild_id = ? AND channel_id = ?", (channel.guild.id, channel.id))

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        await self.db.run("DELETE FROM moved_messages WHERE guild_id = ? AND thread_id = ?", (payload.guild_id, payload.thread_id))

    # ---- who may do what ----

    async def place(self, guild: Any, channel_id: int) -> Any:
        """A channel or thread of this server, an archived thread included."""
        found = guild.get_channel_or_thread(channel_id)
        if found is not None:
            return found
        if await self.bot.ops.find_thread(guild.id, channel_id):
            return guild.get_channel_or_thread(channel_id) or self.bot.ops.known_thread(guild.id, channel_id)
        return None

    def is_moderator(self, member: Any, channel: Any) -> bool:
        """Who may move, copy, edit and delete anyone's message: someone who could delete it by hand, with Manage Messages there, or the bot manager role."""
        if channel is not None and access.can_manage_messages(member, channel):
            return True
        manager = self.bot.settings.get(member.guild.id, COMMANDS_MODULE).get("mod_role")
        return bool(manager) and any(role.id == manager for role in getattr(member, "roles", []))

    @staticmethod
    def owner_of(message: Any, record: dict | None) -> int | None:
        """Whose message it is: its author, or for a copy this bot posted, whoever moved or copied it."""
        return record["user_id"] if record is not None else message.author.id

    def taking_refusal(self, member: Any, message: Any, record: dict | None, source: Any, *, copy: bool) -> str | None:
        """Why this person can't move or copy this message, or None. Their own message they can. Someone else's takes being a moderator there,
        and being able to read that channel, since the copy shows what was said in it to wherever it goes."""
        if source is None:
            return "That channel is gone."
        if self.bot.user is not None and message.author.id == self.bot.user.id and not message.webhook_id:
            # The bot's own post lives in the Embeds library under its message. A webhook copy would leave that post deleted and the copy editable by no one.
            return "That's one of the bot's own posts, which this can't move or copy. A collaborative message moves with /move_collaborative_message and copies with /copy_collaborative_message."
        if self.owner_of(message, record) == member.id:
            return None
        if not self.is_moderator(member, source):
            return f"You can only {'copy' if copy else 'move'} your own messages."
        return access.read_refusal(member, source)

    async def automod_problem(self, member: Any, text: str) -> str | None:
        """Automod never scans a webhook's posts, so what a member has the bot post under their name meets the server's word and link rules here, as a
        tag's text does. A channel's exemption doesn't count, since a copy carries the text to wherever it goes."""
        automod = self.bot.module("automod")
        return await automod.output_problem(member, text) if automod is not None and text else None

    # ---- the log channel ----

    async def log_line(self, guild_id: int, member: Any, channel: Any, command: str, details: str | None = None) -> None:
        """MV-10: `Command `name` used by @someone in #channel`, and the details on a second line. It mentions people without pinging them."""
        target = self.cfg(guild_id)["log_channel"]
        found = self.bot.get_channel(target) if target else None
        if found is None or not hasattr(found, "send"):
            return
        text = f"Command `{command}` used by {member.mention} in {channel.mention if channel is not None else 'a channel that is gone'}"
        if details:
            text += f"\nDetails: {details}"
        try:
            await found.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            log.info("Could not post in the message mover log of %s: %s", guild_id, error)

    # ---- moving and copying ----

    async def _start(self, interaction: discord.Interaction, message: discord.Message, *, copy: bool) -> None:
        if interaction.guild is None or not self.on_in(interaction.guild.id):
            await _answer(interaction, OFF)
            return
        member = interaction.user
        record = await self.record_of(interaction.guild.id, message.id)
        source = await self.place(interaction.guild, message.channel.id)
        refusal = self.taking_refusal(member, message, record, source, copy=copy)
        if refusal:
            await _answer(interaction, refusal)
            return
        await interaction.response.send_message(f"Select the channel or thread to {'copy' if copy else 'move'} this message to:", view=DestinationView(self, message, copy), ephemeral=True)

    async def menu_move(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await self._start(interaction, message, copy=False)

    async def menu_copy(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await self._start(interaction, message, copy=True)

    async def transfer(self, member: Any, message: discord.Message, target: Any, *, copy: bool) -> str:
        """MV-6: posts the copy under the author's name and picture, keeps its webhook so it can be edited later, and for a move deletes the original. Returns the answer for the person."""
        guild = member.guild
        if getattr(getattr(target, "guild", None), "id", None) != guild.id:
            return "That channel isn't in this server."
        record = await self.record_of(guild.id, message.id)
        source = await self.place(guild, message.channel.id)
        if source is not None:
            try:
                # The picker stays open a minute, and the message may have been deleted or edited since the menu read it.
                message = await source.fetch_message(message.id)
            except discord.NotFound:
                return "That message is gone."
            except discord.Forbidden:
                return "The bot can't read that channel's history."
            except discord.HTTPException as error:
                return f"Failed to move/copy message: {error.text or error.status}"
        refusal = self.taking_refusal(member, message, record, source, copy=copy) or access.post_refusal(member, target)
        if refusal:
            return refusal
        embeds, lost = what_carries(message)
        limit = upload_limit(guild)
        too_big = [attachment.filename for attachment in message.attachments if attachment.size > limit]
        if too_big:
            lost.append(f"{'its file' if len(too_big) == 1 else 'its files'} {', '.join(too_big)}, too big for the bot to upload here")
        if not copy and lost:
            return f"Moving it would lose {' and '.join(lost)}, so it stays where it is. Copy it instead."
        if not message.content and not embeds and len(too_big) == len(message.attachments):
            return "That message has nothing the bot can repost."
        problem = await self.automod_problem(member, carried_text(message.content, embeds))
        if problem:
            return problem
        try:
            # discord.py takes the spoiler off unless told, and the copy would show it unblurred.
            files = [await attachment.to_file(spoiler=attachment.is_spoiler()) for attachment in message.attachments if attachment.size <= limit]
        except discord.HTTPException as error:
            return f"Failed to move/copy message: a file couldn't be read ({error.text or error.status})."
        author = message.author
        try:
            sent = await self.bot.webhooks.send(target, name=getattr(author, "display_name", None) or author.name, avatar_url=author.display_avatar.url,
                                                content=message.content or None, embeds=embeds, files=files)
            hook = await self.bot.webhooks.hook(target)
        except OpsError as error:
            return f"Failed to move/copy message: {error}"
        thread = target if isinstance(target, discord.Thread) else None
        if hook.id == sent.webhook_id and hook.token:
            await self.db.run(
                "INSERT OR REPLACE INTO moved_messages (guild_id, message_id, channel_id, thread_id, user_id, author_id, source_channel_id, source_message_id, copied, webhook_id, webhook_token, moved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild.id, sent.id, thread.parent_id if thread else target.id, thread.id if thread else None, member.id, record["author_id"] if record else author.id,
                 message.channel.id, message.id, int(copy), hook.id, hook.token, time.time()),
            )
        else:
            log.warning("The webhook that posted %s in %s changed during the post, so it can't be edited later", sent.id, guild.id)
        new_link = jump_url(guild.id, target.id, sent.id)
        answer = f"Message {'copied' if copy else 'moved'} successfully. {new_link}"
        if lost:
            answer += f"\nThe copy is missing {' and '.join(lost)}."
        if not copy:
            problem = await self._delete_original(message, record)
            if problem:
                answer += f"\nThe original couldn't be deleted: {problem}"
        await self.bot.activity_log.record(guild.id, self.bot.actor(member), f"mover.{'copy' if copy else 'move'}", detail=f"{message.jump_url} to {new_link}")
        await self.log_line(guild.id, member, source, "Copy Message" if copy else "Move Message", f"{message.jump_url} to {new_link}")
        return answer

    async def _delete_original(self, message: discord.Message, record: dict | None) -> str | None:
        """Deletes what was moved. A copy this bot posted goes through its own webhook, and anything else needs Manage Messages. Returns why it couldn't, or None."""
        try:
            if record is not None:
                await self.bot.webhooks.delete(self.bot.webhooks.partial(record["webhook_id"], record["webhook_token"]), message.id, thread_id=record["thread_id"])
                await self._forget(record["guild_id"], [message.id])
            else:
                await message.delete()
        except OpsError as error:
            return str(error)
        except discord.NotFound:
            return None
        except discord.Forbidden:
            return "The bot needs Manage Messages in that channel."
        except discord.HTTPException as error:
            return f"Discord refused: {error.text or error.status}"
        return None

    # ---- editing and deleting a copy ----

    async def _owned(self, interaction: discord.Interaction, message: discord.Message, verb: str) -> dict | None:
        """The record of a copy this person may change, or None after saying why not."""
        if interaction.guild is None or not self.on_in(interaction.guild.id):
            await _answer(interaction, OFF)
            return None
        record = await self.record_of(interaction.guild.id, message.id)
        if record is None:
            await _answer(interaction, f"This message cannot be {verb} using this command.")
            return None
        member = interaction.user
        channel = await self.place(interaction.guild, record["thread_id"] or record["channel_id"])
        if record["user_id"] != member.id and not self.is_moderator(member, channel):
            await _answer(interaction, f"You can only {'edit' if verb == 'edited' else 'delete'} your own moved/copied messages.")
            return None
        if verb == "edited":
            refusal = access.post_refusal(member, channel) if channel is not None else "That channel is gone."
            if refusal:
                await _answer(interaction, refusal)
                return None
        return record

    async def menu_edit(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if await self._owned(interaction, message, "edited") is None:
            return
        await interaction.response.send_modal(EditForm(self, message))

    async def submit_edit(self, interaction: discord.Interaction, message: discord.Message, text: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._owned(interaction, message, "edited")
        if record is None:
            return
        if not text and not what_carries(message)[0] and not message.attachments:
            await _answer(interaction, "A message can't be empty. Use Delete Moved Message to take it down.")
            return
        problem = await self.automod_problem(interaction.user, text)
        if problem:
            await _answer(interaction, problem)
            return
        hooks = self.bot.webhooks
        try:
            await hooks.edit(hooks.partial(record["webhook_id"], record["webhook_token"]), message.id, thread_id=record["thread_id"], content=text or None)
        except OpsError as error:
            await _answer(interaction, f"Failed to edit message: {error}")
            return
        await _answer(interaction, "Message edited successfully.")
        place = await self.place(interaction.guild, record["thread_id"] or record["channel_id"])
        await self.bot.activity_log.record(record["guild_id"], self.bot.actor(interaction.user), "mover.edit", detail=message.jump_url)
        await self.log_line(record["guild_id"], interaction.user, place, "Edit Moved Message", f"Edited Message ID: {message.id} in {'Thread' if record['thread_id'] else 'Channel'}")

    async def menu_delete(self, interaction: discord.Interaction, message: discord.Message) -> None:
        record = await self._owned(interaction, message, "deleted")
        if record is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        hooks = self.bot.webhooks
        try:
            await hooks.delete(hooks.partial(record["webhook_id"], record["webhook_token"]), message.id, thread_id=record["thread_id"])
        except OpsError as error:
            await _answer(interaction, f"Failed to delete message: {error}")
            return
        await self._forget(record["guild_id"], [message.id])
        await _answer(interaction, "Message deleted successfully.")
        place = await self.place(interaction.guild, record["thread_id"] or record["channel_id"])
        await self.bot.activity_log.record(record["guild_id"], self.bot.actor(interaction.user), "mover.delete", detail=message.jump_url)
        await self.log_line(record["guild_id"], interaction.user, place, "Delete Moved Message", f"Deleted Message ID: {message.id} in {'Thread' if record['thread_id'] else 'Channel'}")

    # ---- slash commands ----

    @commands.hybrid_command(name="mover_log_channel", description="Set the channel that logs moved, copied, edited and deleted messages")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(channel="The channel where logs should be sent")
    async def mover_log_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        refusal = access.post_refusal(ctx.author, channel)
        if refusal:
            await ctx.fail(refusal)
            return
        await self.bot.settings.save(ctx.guild.id, self.key, {"log_channel": channel.id}, actor=self.bot.actor(ctx.author), action="mover.log_channel")
        await ctx.ok(f"Log channel set to {channel.mention}")

    async def prune(self, guild_id: int, *, seconds: float = PRUNE_SECONDS) -> tuple[int, int, bool]:
        """MV-9: forgets copies whose message, channel or thread is gone. Only Discord's answer that one doesn't exist counts, so a copy the bot can't read,
        or one in a thread Discord refused or failed to look up, stays, since it may still be there and its record holds the only stored webhook token.
        Returns (pruned, total, whether it finished in time)."""
        ops = self.bot.ops
        deadline = time.monotonic() + seconds
        rows = await self.db.all("SELECT message_id, channel_id, thread_id FROM moved_messages WHERE guild_id = ? ORDER BY id", (guild_id,))
        gone: list[int] = []
        finished = True
        for row in rows:
            if time.monotonic() > deadline:
                finished = False
                break
            place = row["thread_id"] or row["channel_id"]
            state = await ops.place_state(guild_id, place)
            if state == "gone":
                gone.append(row["message_id"])
            if state != "here":
                continue
            try:
                await ops.fetch_message(place, row["message_id"])
            except MessageGone:
                gone.append(row["message_id"])
            except OpsError:
                continue
        await self._forget(guild_id, gone)
        return len(gone), len(rows), finished

    @commands.hybrid_command(name="prune_messages", description="Forget moved and copied messages that no longer exist")
    @commands.guild_only()
    @requires("administrator")
    @staff_only("Owner and Administrators")
    async def prune_messages(self, ctx: commands.Context) -> None:
        # The bot manager role and bypass roles pass an Administrator command, and the page's Prune button is the owner's and Administrators' alone.
        if not access.is_admin(ctx.author):
            await ctx.fail(PRUNE_REFUSED)
            return
        await ctx.defer(ephemeral=True)
        pruned, total, finished = await self.prune(ctx.guild.id)
        more = "" if finished else f" It stopped after {PRUNE_SECONDS // 60} minutes, so run it again to check the rest."
        await ctx.ok(f"Pruned {pruned} out of {total} messages.{more}")
        await self.log_line(ctx.guild.id, ctx.author, ctx.channel, "Prune Messages")

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/mover/messages", self._list_endpoint)
        router.post("/mover/prune", self._prune_endpoint)

    async def _list_endpoint(self, req: Any) -> dict:
        # The webhook's token stays out of everything sent to a browser.
        rows = await self.db.all(
            "SELECT id, message_id, channel_id, thread_id, user_id, author_id, source_channel_id, source_message_id, copied, imported, moved_at FROM moved_messages "
            "WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (req.guild_id, LISTED),
        )
        total = await self.db.scalar("SELECT COUNT(*) FROM moved_messages WHERE guild_id = ?", (req.guild_id,))
        guild = self.bot.get_guild(req.guild_id)
        for thread_id in {row["thread_id"] for row in rows if row["thread_id"]}:
            if guild is not None and guild.get_channel_or_thread(thread_id) is None:
                await req.ops.find_thread(req.guild_id, thread_id)  # an archived thread, so a private one is judged as one
        rows = [row for row in rows if req.can_read(row["channel_id"]) and (not row["thread_id"] or req.can_read(row["thread_id"]))]
        for row in rows:
            row["copied"] = bool(row["copied"])
            row["imported"] = bool(row["imported"])
            row["url"] = jump_url(req.guild_id, row["thread_id"] or row["channel_id"], row["message_id"])
        return {"rows": rows, "total": total}

    async def _prune_endpoint(self, req: Any) -> dict:
        if not req.is_admin:
            raise ApiError(403, PRUNE_REFUSED)
        pruned, total, finished = await self.prune(req.guild_id, seconds=PAGE_PRUNE_SECONDS)
        await req.record("mover.prune", detail=f"{pruned} of {total}")
        return {"pruned": pruned, "total": total, "finished": finished}
