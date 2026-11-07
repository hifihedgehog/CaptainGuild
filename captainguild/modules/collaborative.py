"""Collaborative messages: posts the bot makes that named members and roles may edit, with every earlier text kept and revertible.

A collaborative message is a post in the Embeds library (posted_messages) with a list of the people and roles allowed to change it. Its history
is the library's own version history, so the Embeds page shows and reverts it too. The server's owner and Administrators can change any of them.
The commands and the right-click menus go only into servers that switch the pack on.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, messages
from ..core.module import Module, requires, staff_only
from ..core.ops import MessageGone, OpsError
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, hex_color, number, optional, snowflake, snowflakes
from ..web.api import ApiError
from .embeds import MESSAGE_LINK, jump_url

log = logging.getLogger(__name__)

MAX_POSTS = 1000
MAX_USERS = 100
MAX_ROLES = 50
MAX_CREATOR_ROLES = 50
PAGE_SIZE = 10
SNIPPET = 100
CONTENT_FORM_MAX = 2000
FORM_MAX = 4000  # the longest text a Discord form takes
PANEL_TEXT = 3800  # what a revision panel shows of one text, with room left for its labels
PRUNE_SECONDS = 600  # a prune answers through the command's own reply, which lasts 15 minutes
SOURCE = "collaborative"
EARLIER_NOTE = "An earlier revision, brought over with the post"
REVERTED = re.compile(r"^Reverted to version (\d+)$")
OFF = "That's switched off in this server. A manager can switch it on from the dashboard."
NOT_COLLABORATIVE = "This is not a collaborative message."
NOT_COLLABORATIVE_HERE = "This message is not a collaborative message."
NO_PERMISSION = "You don't have permission to modify this message."
PRUNE_REFUSED = "Only the server owner or an Administrator can prune these records."  # the mover's sentence, for the same kind of cleanup
INVALID_COLOR = "Invalid color format. Please provide a valid hex code or color name."
TOO_MANY = f"A collaborative message can have up to {MAX_USERS} people and {MAX_ROLES} roles."


def _color_makers() -> dict[str, Any]:
    """discord.py's named colors, such as red, blurple and dark_teal: every class method of discord.Color that takes nothing."""
    found: dict[str, Any] = {}
    for name in dir(discord.Color):
        if name.startswith("_"):
            continue
        maker = getattr(discord.Color, name)
        if not inspect.ismethod(maker) or maker.__self__ is not discord.Color:
            continue
        try:
            needed = [p for p in inspect.signature(maker).parameters.values() if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        except (TypeError, ValueError):
            continue
        if not needed:
            found[name] = maker
    return found


COLOR_MAKERS = _color_makers()


def color_value(text: str) -> int:
    """A color typed as six hex digits, with or without #, or as a color name such as red or blurple."""
    cleaned = str(text or "").strip()
    maker = COLOR_MAKERS.get(cleaned.lower().replace(" ", "_"))
    if maker is not None:
        return maker().value
    try:
        return hex_color(cleaned)
    except Invalid as error:
        raise Invalid(INVALID_COLOR) from error


class ColorArgument(commands.Converter):
    """A color for /create_collaborative_message. A typed word that isn't one is left for the options after it."""

    async def convert(self, ctx: commands.Context, argument: str) -> int:
        try:
            return color_value(argument)
        except Invalid as error:
            raise commands.BadArgument(INVALID_COLOR) from error


def text_of(payload: dict) -> tuple[str, str]:
    """Which text a collaborator edits, and what it says now: the first embed's description on a post that is only an embed, and the message text otherwise."""
    if not payload.get("content") and payload.get("embeds"):
        return "description", payload["embeds"][0].get("description") or ""
    return "content", payload.get("content") or ""


def with_text(payload: dict, text: str) -> dict:
    """The post with its editable text changed. Raises Invalid with a sentence when Discord wouldn't take it."""
    kind, _ = text_of(payload)
    if kind == "description":
        first = {**payload["embeds"][0], "description": text}
        changed = {**payload, "embeds": [first, *payload["embeds"][1:]]}
    else:
        changed = {**payload, "content": text}
    clean = messages.clean_payload(changed)
    if not messages.has_content(clean):
        raise Invalid("A message can't be empty.")
    return clean


def shown_text(payload: dict) -> str:
    """What a revision says, for the history panel."""
    kind, text = text_of(payload)
    if not text and kind == "description":
        text = payload["embeds"][0].get("title") or ""
    return text or "(no text)"


def previous_version(versions: list[dict]) -> int | None:
    """The version before the text the message shows now, for Revert to Previous Revision. A revert shows an older version's text, so the
    walk carries on from that one, and reverting again keeps going back the way it did when each revert used up the revision it restored."""
    if not versions:
        return None
    by_number = {row["number"]: row for row in versions}
    current = versions[-1]["number"]
    seen: set[int] = set()
    while current not in seen:
        seen.add(current)
        found = REVERTED.match(by_number[current]["note"] or "")
        target = int(found.group(1)) if found else None
        if target is None or target >= current or target not in by_number:
            break
        current = target
    earlier = [number for number in by_number if number < current]
    return max(earlier) if earlier else None


def _unique(values: Any) -> list[int]:
    out: list[int] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _plural(word: str, count: int) -> str:
    return f"{word}{'s' if count > 1 else ''}"


def _names(word: str, names: list[str]) -> str:
    return f"{_plural(word, len(names))} {', '.join(names)}"


def _reply_parts(users: list[str], roles: list[str]) -> str:
    """"Users A, B and Role X", the way the old replies read."""
    parts = []
    if users:
        parts.append(_names("User", users))
    if roles:
        parts.append(_names("Role", roles))
    return " and ".join(parts)


class EditForm(discord.ui.Modal):
    """Edit Collaborative Message: the post's text in one box."""

    def __init__(self, cog: "Collaborative", post: dict, text: str, limit: int) -> None:
        super().__init__(title="Edit Collaborative Message")
        self.cog = cog
        self.post = post
        self.box = discord.ui.TextInput(label="New Content", style=discord.TextStyle.paragraph, default=text or None, max_length=limit, required=True)
        self.add_item(self.box)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_edit(interaction, self.post, self.box.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("The collaborative edit form failed", exc_info=error)
        await _answer(interaction, "Something went wrong with that form. It's been logged.")


async def _answer(interaction: discord.Interaction, text: str, **kwargs: Any) -> None:
    """A private answer, whether or not the interaction has had its first one."""
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, **kwargs)
        else:
            await interaction.response.send_message(text, ephemeral=True, **kwargs)
    except discord.HTTPException as error:
        log.info("Could not answer a collaborative message action: %s", error)


class RevisionPanel(discord.ui.View):
    """View Revision History: the earlier texts one at a time, oldest first, with Revert for those who may change the post."""

    def __init__(self, cog: "Collaborative", post: dict, revisions: list[dict], *, may_revert: bool, user_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.post = post
        self.revisions = revisions
        self.user_id = user_id
        self.index = 0
        self.revert.disabled = not may_revert

    def embed(self) -> discord.Embed:
        row = self.revisions[self.index]
        text = shown_text(row["payload"])
        if len(text) > PANEL_TEXT:
            text = text[:PANEL_TEXT - 3].rstrip() + "..."
        lines = [f"**Content:**\n{text}", f"**Created At:** <t:{int(row['saved_at'])}:f>"]
        if row.get("saved_by_name"):
            lines.append(f"**By:** {discord.utils.escape_markdown(row['saved_by_name'])}")
        return discord.Embed(title=f"Revision {self.index + 1}/{len(self.revisions)}", description="\n".join(lines))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("Only the person who ran this command can use this.", ephemeral=True)
        return False

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.primary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.index <= 0:
            await interaction.response.send_message("This is the first revision.", ephemeral=True)
            return
        self.index -= 1
        await interaction.response.edit_message(embed=self.embed(), view=self)
        await self.cog.log_line(self.post["guild_id"], f"{self.cog.who(interaction.user)} viewed previous revision ({self.index + 1}) of message {self.cog.link(self.post)}.")

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.index >= len(self.revisions) - 1:
            await interaction.response.send_message("This is the latest revision.", ephemeral=True)
            return
        self.index += 1
        await interaction.response.edit_message(embed=self.embed(), view=self)
        await self.cog.log_line(self.post["guild_id"], f"{self.cog.who(interaction.user)} viewed next revision ({self.index + 1}) of message {self.cog.link(self.post)}.")

    @discord.ui.button(label="Revert", style=discord.ButtonStyle.danger)
    async def revert(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.revert_from_panel(interaction, self.post, self.revisions[self.index], self.index + 1)


class ListView(discord.ui.View):
    """/list_collaborative_messages, ten to a page."""

    def __init__(self, pages: list[list[str]], user_id: int) -> None:
        super().__init__(timeout=180)
        self.pages = pages
        self.page = 0
        self.user_id = user_id

    def embed(self) -> discord.Embed:
        return discord.Embed(title=f"Collaborative Messages (Page {self.page + 1}/{len(self.pages)})", description="\n\n".join(self.pages[self.page]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("Only the person who ran this command can use this.", ephemeral=True)
        return False

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.primary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page <= 0:
            await interaction.response.send_message("This is the first page.", ephemeral=True)
            return
        self.page -= 1
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page >= len(self.pages) - 1:
            await interaction.response.send_message("This is the last page.", ephemeral=True)
            return
        self.page += 1
        await interaction.response.edit_message(embed=self.embed(), view=self)


PEOPLE_HELP = {f"user{n}": f"{word} user to give edit permission" for n, word in enumerate(("First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth", "Tenth"), 1)}
ROLE_HELP = {f"role{n}": f"{word} role to give edit permission" for n, word in enumerate(("First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth", "Tenth"), 1)}
REMOVE_PEOPLE_HELP = {key: value.replace("give", "remove") for key, value in PEOPLE_HELP.items()}
REMOVE_ROLE_HELP = {key: value.replace("give", "remove") for key, value in ROLE_HELP.items()}


class Collaborative(Module):
    key = "collaborative"
    category = "Collaborative messages"
    command_pack = True
    schema = Schema({
        "enabled": Field(False, boolean),
        "creator_roles": Field([], snowflakes(MAX_CREATOR_ROLES)),
        "log_channel": Field(None, optional(snowflake)),
        "log_enabled": Field(False, boolean),
    })
    child_links = {"collab_posts": {"posted_id": "posted_messages.id"}, "collab_editors": {"posted_id": "posted_messages.id"}}
    row_limits = {"collab_posts": MAX_POSTS, "collab_editors": MAX_POSTS * (MAX_USERS + MAX_ROLES)}
    migrations = [
        """
        CREATE TABLE collab_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            posted_id INTEGER NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX collab_posts_post ON collab_posts (guild_id, posted_id);
        CREATE TABLE collab_editors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            posted_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            target_id INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX collab_editors_target ON collab_editors (guild_id, posted_id, kind, target_id);
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._menus = [
            app_commands.ContextMenu(name="Edit Collaborative Message", callback=self.menu_edit),
            app_commands.ContextMenu(name="View Revision History", callback=self.menu_history),
            app_commands.ContextMenu(name="Revert to Previous Revision", callback=self.menu_revert),
            app_commands.ContextMenu(name="Delete Collaborative Message", callback=self.menu_delete),
            app_commands.ContextMenu(name="See Current Collaborators", callback=self.menu_collaborators),
        ]

    async def on_load(self) -> None:
        for menu in self._menus:
            self.bot.tree.add_command(menu)

    def cog_unload(self) -> None:
        for menu in self._menus:
            self.bot.tree.remove_command(menu.name, type=menu.type)

    # ---- what the dashboard checks on a save ----

    def staff_roles(self, settings: dict) -> dict[str, set[int]]:
        """A creator role lets its members post through the bot, so only the owner or an Administrator names one, as the commands ask."""
        return {"creator_roles": set(settings.get("creator_roles") or [])}

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {"log_channel": {settings["log_channel"]} if settings.get("log_channel") else set()}

    # ---- the store ----

    @property
    def embeds(self) -> Any:
        found = self.bot.module("embeds")
        if found is None:
            raise OpsError("Collaborative messages need the Embeds feature, which isn't loaded.")
        return found

    async def post_of(self, guild_id: int, message_id: int) -> dict | None:
        """The collaborative post at this message, or None when it isn't one."""
        return await self.db.one(
            "SELECT p.id AS posted_id, p.guild_id, p.channel_id, p.message_id, p.summary, p.deleted_at FROM collab_posts c "
            "JOIN posted_messages p ON p.id = c.posted_id AND p.guild_id = c.guild_id WHERE c.guild_id = ? AND p.message_id = ?",
            (guild_id, message_id),
        )

    async def post_by_number(self, guild_id: int, posted_id: int) -> dict | None:
        return await self.db.one(
            "SELECT p.id AS posted_id, p.guild_id, p.channel_id, p.message_id, p.summary, p.deleted_at FROM collab_posts c "
            "JOIN posted_messages p ON p.id = c.posted_id AND p.guild_id = c.guild_id WHERE c.guild_id = ? AND c.posted_id = ?",
            (guild_id, posted_id),
        )

    async def collaborators(self, guild_id: int, posted_id: int) -> tuple[list[int], list[int]]:
        """The people and the roles who may change a post, in the order they were added."""
        rows = await self.db.all("SELECT kind, target_id FROM collab_editors WHERE guild_id = ? AND posted_id = ? ORDER BY id", (guild_id, posted_id))
        return [row["target_id"] for row in rows if row["kind"] == "user"], [row["target_id"] for row in rows if row["kind"] == "role"]

    async def _count(self, guild_id: int) -> int:
        return await self.db.scalar(
            "SELECT COUNT(*) FROM collab_posts c JOIN posted_messages p ON p.id = c.posted_id AND p.guild_id = c.guild_id WHERE c.guild_id = ? AND p.deleted_at IS NULL", (guild_id,),
        )

    async def make_collaborative(self, guild_id: int, posted_id: int, users: list[int], roles: list[int]) -> None:
        async with self.db.transaction():
            await self.db.run("INSERT OR IGNORE INTO collab_posts (guild_id, posted_id, created_at) VALUES (?, ?, ?)", (guild_id, posted_id, time.time()))
            await self._add(guild_id, posted_id, users, roles)

    async def _add(self, guild_id: int, posted_id: int, users: list[int], roles: list[int]) -> None:
        rows = [(guild_id, posted_id, "user", user) for user in users] + [(guild_id, posted_id, "role", role) for role in roles]
        if rows:
            await self.db.many("INSERT OR IGNORE INTO collab_editors (guild_id, posted_id, kind, target_id) VALUES (?, ?, ?, ?)", rows)

    async def _remove(self, guild_id: int, posted_id: int, users: list[int], roles: list[int]) -> None:
        rows = [(guild_id, posted_id, "user", user) for user in users] + [(guild_id, posted_id, "role", role) for role in roles]
        if rows:
            await self.db.many("DELETE FROM collab_editors WHERE guild_id = ? AND posted_id = ? AND kind = ? AND target_id = ?", rows)

    async def set_collaborators(self, guild_id: int, posted_id: int, users: list[int], roles: list[int]) -> None:
        async with self.db.transaction():
            await self.db.run("DELETE FROM collab_editors WHERE guild_id = ? AND posted_id = ?", (guild_id, posted_id))
            await self._add(guild_id, posted_id, users, roles)

    async def forget(self, guild_id: int, posted_id: int) -> None:
        """Drops a post's collaborators and its place in the library."""
        async with self.db.transaction():
            await self.db.run("DELETE FROM collab_editors WHERE guild_id = ? AND posted_id = ?", (guild_id, posted_id))
            await self.db.run("DELETE FROM collab_posts WHERE guild_id = ? AND posted_id = ?", (guild_id, posted_id))
            await self.embeds.forget_post(guild_id, posted_id)

    # ---- who may do what ----

    def may_create(self, member: Any) -> bool:
        """An Administrator, or someone holding a creator role."""
        if access.is_admin(member):
            return True
        allowed = set(self.cfg(member.guild.id)["creator_roles"])
        return any(role.id in allowed for role in member.roles)

    async def place(self, guild: Any, channel_id: int) -> Any:
        """A channel or thread of this server, an archived thread included."""
        found = guild.get_channel_or_thread(channel_id)
        if found is not None:
            return found
        if await self.bot.ops.find_thread(guild.id, channel_id):
            return guild.get_channel_or_thread(channel_id) or self.bot.ops.known_thread(guild.id, channel_id)
        return None

    async def change_refusal(self, member: Any, post: dict) -> str | None:
        """Why this person can't change this post, or None. A collaborator by name or by role may, and so may the owner and Administrators."""
        if access.is_admin(member):
            return None
        users, roles = await self.collaborators(post["guild_id"], post["posted_id"])
        if member.id not in users and not any(role.id in roles for role in getattr(member, "roles", [])):
            return NO_PERMISSION
        channel = await self.place(member.guild, post["channel_id"])
        return access.view_refusal(member, channel) if channel is not None else None

    async def automod_problem(self, member: Any, text: str) -> str | None:
        """Automod never scans the bot's own posts, so a member's text meets the server's word and link rules here before the bot posts it for them,
        as a tag's text does."""
        automod = self.bot.module("automod")
        return await automod.output_problem(member, text) if automod is not None and text else None

    @staticmethod
    def who(user: Any) -> str:
        return f"{discord.utils.escape_markdown(str(user))} ({user.id})"

    @staticmethod
    def link(post: dict) -> str:
        return jump_url(post["guild_id"], post["channel_id"], post["message_id"])

    async def log_line(self, guild_id: int, text: str) -> None:
        """One line in the log channel, when logging is on and a channel is set."""
        settings = self.cfg(guild_id)
        if not settings["log_enabled"] or not settings["log_channel"]:
            return
        channel = self.bot.get_channel(settings["log_channel"])
        if channel is None or not hasattr(channel, "send"):
            return
        try:
            await channel.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            log.info("Could not post in the collaborative message log of %s: %s", guild_id, error)

    async def _record(self, guild_id: int, user: Any, action: str, detail: str) -> None:
        await self.bot.activity_log.record(guild_id, self.bot.actor(user), f"collaborative.{action}", detail=detail[:200])

    # ---- naming a post in a command ----

    async def _named(self, ctx: commands.Context, identifier: str) -> dict | None:
        """The collaborative post a link or an ID names, or None after saying why not."""
        found = MESSAGE_LINK.search(identifier or "")
        if found:
            guild_id, _, message_id = (int(part) for part in found.groups())
            if guild_id != ctx.guild.id:
                await ctx.fail("That link points to a message outside this server.")
                return None
        else:
            try:
                message_id = snowflake(identifier)
            except Invalid:
                await ctx.fail("That isn't a message ID or link.")
                return None
        post = await self.post_of(ctx.guild.id, message_id)
        if post is None:
            await ctx.fail(NOT_COLLABORATIVE if found else f"Message ID {message_id} not found.")
        return post

    def _check_lists(self, guild: Any, users: list[int], roles: list[int]) -> str | None:
        if len(users) > MAX_USERS or len(roles) > MAX_ROLES:
            return TOO_MANY
        if guild.id in roles or (guild.default_role is not None and guild.default_role.id in roles):
            return access.NOT_EVERYONE
        return None

    async def _names_of_users(self, guild: Any, user_ids: list[int]) -> list[str]:
        out = []
        for user_id in user_ids:
            member = guild.get_member(user_id)
            out.append(discord.utils.escape_markdown(member.display_name) if member is not None else f"<@{user_id}>")
        return out

    @staticmethod
    def _names_of_roles(guild: Any, role_ids: list[int]) -> list[str]:
        return [discord.utils.escape_markdown(role.name) for role in (guild.get_role(role_id) for role_id in role_ids) if role is not None]

    # ---- /create_collaborative_message ----

    @commands.hybrid_command(name="create_collaborative_message", description="Create a collaborative message with permissions")
    @commands.guild_only()
    @staff_only("Administrators and creator roles")
    @app_commands.describe(content="The content of the message", color="Optional color for the embed (hex code or color name)", **PEOPLE_HELP, **ROLE_HELP)
    async def create_collaborative_message(
        self, ctx: commands.Context, content: str, color: ColorArgument | None = None,
        user1: discord.Member | None = None, user2: discord.Member | None = None, user3: discord.Member | None = None, user4: discord.Member | None = None, user5: discord.Member | None = None,
        user6: discord.Member | None = None, user7: discord.Member | None = None, user8: discord.Member | None = None, user9: discord.Member | None = None, user10: discord.Member | None = None,
        role1: discord.Role | None = None, role2: discord.Role | None = None, role3: discord.Role | None = None, role4: discord.Role | None = None, role5: discord.Role | None = None,
        role6: discord.Role | None = None, role7: discord.Role | None = None, role8: discord.Role | None = None, role9: discord.Role | None = None, role10: discord.Role | None = None,
    ) -> None:
        await ctx.defer(ephemeral=True)
        member = ctx.author
        if not self.may_create(member):
            await ctx.fail("You do not have permission to create collaborative messages.")
            return
        refusal = access.post_refusal(member, ctx.channel)
        if refusal:
            await ctx.fail(refusal)
            return
        users = _unique([member.id, *(user.id for user in (user1, user2, user3, user4, user5, user6, user7, user8, user9, user10) if user is not None)])
        roles = _unique(role.id for role in (role1, role2, role3, role4, role5, role6, role7, role8, role9, role10) if role is not None)
        problem = self._check_lists(ctx.guild, users, roles)
        if problem:
            await ctx.fail(problem)
            return
        try:
            payload = messages.clean_payload({"embeds": [{"description": content, "color": color}]} if color is not None else {"content": content})
        except Invalid as error:
            await ctx.fail(str(error))
            return
        if not messages.has_content(payload):
            await ctx.fail("Write something for the message to say.")
            return
        problem = await self.automod_problem(member, content)
        if problem:
            await ctx.fail(problem)
            return
        if await self._count(ctx.guild.id) >= MAX_POSTS:
            await ctx.fail(f"This server has {MAX_POSTS:,} collaborative messages. Delete one first.")
            return
        try:
            result = await self.embeds.post_message(ctx.guild.id, ctx.channel.id, payload, actor=self.bot.actor(member), source=SOURCE)
        except OpsError as error:
            await ctx.fail(str(error))
            return
        await self.make_collaborative(ctx.guild.id, result["id"], users, roles)
        await self._record(ctx.guild.id, member, "create", messages.summary(payload, 80))
        await ctx.ok("Collaborative message created successfully!")
        await self.log_line(ctx.guild.id, f"{self.who(member)} created a new collaborative message {result['url']}.")

    # ---- /add_collaborators and /remove_collaborators ----

    @commands.hybrid_command(name="add_collaborators", description="Add users or roles as collaborators to a collaborative message")
    @commands.guild_only()
    @staff_only("Collaborators")
    @app_commands.describe(message_identifier="The message link or ID to add collaborators to", **PEOPLE_HELP, **ROLE_HELP)
    async def add_collaborators(
        self, ctx: commands.Context, message_identifier: str,
        user1: discord.Member | None = None, user2: discord.Member | None = None, user3: discord.Member | None = None, user4: discord.Member | None = None, user5: discord.Member | None = None,
        user6: discord.Member | None = None, user7: discord.Member | None = None, user8: discord.Member | None = None, user9: discord.Member | None = None, user10: discord.Member | None = None,
        role1: discord.Role | None = None, role2: discord.Role | None = None, role3: discord.Role | None = None, role4: discord.Role | None = None, role5: discord.Role | None = None,
        role6: discord.Role | None = None, role7: discord.Role | None = None, role8: discord.Role | None = None, role9: discord.Role | None = None, role10: discord.Role | None = None,
    ) -> None:
        await ctx.defer(ephemeral=True)
        post = await self._named(ctx, message_identifier)
        if post is None:
            return
        refusal = await self.change_refusal(ctx.author, post)
        if refusal:
            await ctx.fail(refusal)
            return
        users = _unique(user.id for user in (user1, user2, user3, user4, user5, user6, user7, user8, user9, user10) if user is not None)
        roles = _unique(role.id for role in (role1, role2, role3, role4, role5, role6, role7, role8, role9, role10) if role is not None)
        if not users and not roles:
            await ctx.fail("Name at least one user or role to add.")
            return
        had_users, had_roles = await self.collaborators(ctx.guild.id, post["posted_id"])
        added_users = [user for user in users if user not in had_users]
        added_roles = [role for role in roles if role not in had_roles]
        problem = self._check_lists(ctx.guild, had_users + added_users, had_roles + added_roles)
        if not problem and (added_users or added_roles):
            # Adding someone hands on the right to have the bot post there, so it takes being able to post there, as the page asks.
            channel = await self.place(ctx.guild, post["channel_id"])
            problem = access.post_refusal(ctx.author, channel) if channel is not None else f"Channel not found for message {post['message_id']}."
        if problem:
            await ctx.fail(problem)
            return
        await self._add(ctx.guild.id, post["posted_id"], added_users, added_roles)
        answer = []
        added = _reply_parts(await self._names_of_users(ctx.guild, added_users), self._names_of_roles(ctx.guild, added_roles))
        if added:
            answer.append(f"{added} successfully added as collaborators.")
        already = _reply_parts(await self._names_of_users(ctx.guild, [u for u in users if u in had_users]), self._names_of_roles(ctx.guild, [r for r in roles if r in had_roles]))
        if already:
            answer.append(f"{already} are already collaborators. No changes made.")
        if added_users or added_roles:
            await self._record(ctx.guild.id, ctx.author, "add", post["summary"][:80])
        await ctx.ok(" ".join(answer))
        if added_users or added_roles:
            await self.log_line(ctx.guild.id, f"{self.who(ctx.author)} added collaborators to message {self.link(post)}. Users: {', '.join(map(str, added_users))}. Roles: {', '.join(map(str, added_roles))}.")

    @commands.hybrid_command(name="remove_collaborators", description="Remove users or roles as collaborators from a collaborative message")
    @commands.guild_only()
    @staff_only("Collaborators")
    @app_commands.describe(message_identifier="The message link or ID to remove collaborators from", **REMOVE_PEOPLE_HELP, **REMOVE_ROLE_HELP)
    async def remove_collaborators(
        self, ctx: commands.Context, message_identifier: str,
        user1: discord.Member | None = None, user2: discord.Member | None = None, user3: discord.Member | None = None, user4: discord.Member | None = None, user5: discord.Member | None = None,
        user6: discord.Member | None = None, user7: discord.Member | None = None, user8: discord.Member | None = None, user9: discord.Member | None = None, user10: discord.Member | None = None,
        role1: discord.Role | None = None, role2: discord.Role | None = None, role3: discord.Role | None = None, role4: discord.Role | None = None, role5: discord.Role | None = None,
        role6: discord.Role | None = None, role7: discord.Role | None = None, role8: discord.Role | None = None, role9: discord.Role | None = None, role10: discord.Role | None = None,
    ) -> None:
        await ctx.defer(ephemeral=True)
        post = await self._named(ctx, message_identifier)
        if post is None:
            return
        refusal = await self.change_refusal(ctx.author, post)
        if refusal:
            await ctx.fail(refusal)
            return
        users = _unique(user.id for user in (user1, user2, user3, user4, user5, user6, user7, user8, user9, user10) if user is not None)
        roles = _unique(role.id for role in (role1, role2, role3, role4, role5, role6, role7, role8, role9, role10) if role is not None)
        if ctx.author.id in users:
            await ctx.fail("You cannot remove yourself as a collaborator.")
            return
        if not users and not roles:
            await ctx.fail("Name at least one user or role to remove.")
            return
        had_users, had_roles = await self.collaborators(ctx.guild.id, post["posted_id"])
        removed_users = [user for user in users if user in had_users]
        removed_roles = [role for role in roles if role in had_roles]
        await self._remove(ctx.guild.id, post["posted_id"], removed_users, removed_roles)
        answer = []
        removed = _reply_parts(await self._names_of_users(ctx.guild, removed_users), self._names_of_roles(ctx.guild, removed_roles))
        if removed:
            answer.append(f"{removed} successfully removed as collaborators.")
        missing = _reply_parts(await self._names_of_users(ctx.guild, [u for u in users if u not in had_users]), self._names_of_roles(ctx.guild, [r for r in roles if r not in had_roles]))
        if missing:
            answer.append(f"{missing} are not yet collaborators and could not be removed.")
        if removed_users or removed_roles:
            await self._record(ctx.guild.id, ctx.author, "remove", post["summary"][:80])
        await ctx.ok(" ".join(answer) or "Nothing changed.")
        if removed_users or removed_roles:
            await self.log_line(ctx.guild.id, f"{self.who(ctx.author)} removed collaborators from message {self.link(post)}. Users: {', '.join(map(str, removed_users))}. Roles: {', '.join(map(str, removed_roles))}.")

    # ---- /move_collaborative_message and /copy_collaborative_message ----

    async def _transfer(self, ctx: commands.Context, identifier: str, destination: Any, *, copy: bool) -> None:
        await ctx.defer(ephemeral=True)
        if getattr(getattr(destination, "guild", None), "id", None) != ctx.guild.id:
            await ctx.fail("That channel isn't in this server.")
            return
        post = await self._named(ctx, identifier)
        if post is None:
            return
        ops = self.bot.ops
        source = await self.place(ctx.guild, post["channel_id"])
        if source is None:
            await ctx.fail(f"Channel not found for message {post['message_id']}.")
            return
        try:
            current = await ops.fetch_message(post["channel_id"], post["message_id"])
        except MessageGone:
            await ctx.fail(f"Message {post['message_id']} not found.")
            return
        except OpsError as error:
            await ctx.fail(str(error))
            return
        refusal = await self.change_refusal(ctx.author, post) or access.read_refusal(ctx.author, source) or access.post_refusal(ctx.author, destination)
        if refusal:
            await ctx.fail(refusal)
            return
        if copy and await self._count(ctx.guild.id) >= MAX_POSTS:
            await ctx.fail(f"This server has {MAX_POSTS:,} collaborative messages. Delete one first.")
            return
        try:
            sent = await ops.repost_message(post["channel_id"], post["message_id"], destination.id, current["payload"])
        except OpsError as error:
            await ctx.fail(str(error))
            return
        actor = self.bot.actor(ctx.author)
        if copy:
            copied = await self.embeds.copy_post(ctx.guild.id, post["posted_id"], destination.id, sent["id"], actor=actor, source=SOURCE)
            users, roles = await self.collaborators(ctx.guild.id, post["posted_id"])
            await self.make_collaborative(ctx.guild.id, copied, users, roles)
            await self._record(ctx.guild.id, ctx.author, "copy", post["summary"][:80])
            await ctx.ok(f"Message successfully copied to {destination.mention}.")
            await self.log_line(ctx.guild.id, f"{self.who(ctx.author)} copied collaborative message {self.link(post)} to {destination.mention}.")
            return
        await self.embeds.move_post(ctx.guild.id, post["posted_id"], destination.id, sent["id"])
        await self._record(ctx.guild.id, ctx.author, "move", post["summary"][:80])
        note = ""
        try:
            await ops.delete_message(post["channel_id"], post["message_id"])
        except OpsError as error:
            note = f" The original couldn't be deleted: {error}"
        await ctx.ok(f"Message successfully moved to {destination.mention}.{note}")
        await self.log_line(ctx.guild.id, f"{self.who(ctx.author)} moved collaborative message {post['message_id']} to {destination.mention}.")

    @commands.hybrid_command(name="move_collaborative_message", description="Move a collaborative message to another channel or thread")
    @commands.guild_only()
    @staff_only("Collaborators")
    @app_commands.describe(message_identifier="The message link or ID to move", destination="The destination channel or thread")
    async def move_collaborative_message(self, ctx: commands.Context, message_identifier: str, destination: discord.TextChannel | discord.Thread) -> None:
        await self._transfer(ctx, message_identifier, destination, copy=False)

    @commands.hybrid_command(name="copy_collaborative_message", description="Copy a collaborative message to another channel or thread")
    @commands.guild_only()
    @staff_only("Collaborators")
    @app_commands.describe(message_identifier="The message link or ID to copy", destination="The destination channel or thread")
    async def copy_collaborative_message(self, ctx: commands.Context, message_identifier: str, destination: discord.TextChannel | discord.Thread) -> None:
        await self._transfer(ctx, message_identifier, destination, copy=True)

    # ---- /list_collaborative_messages and /prune ----

    def _entry(self, guild: Any, row: dict, channel: Any) -> str:
        url = jump_url(guild.id, row["channel_id"], row["message_id"])
        snippet = (row["summary"] or "")[:SNIPPET]
        if isinstance(channel, discord.Thread):
            parent = channel.parent
            where = f"in thread **{discord.utils.escape_markdown(channel.name)}**" + (f" (in **#{discord.utils.escape_markdown(parent.name)}**)" if parent is not None else "")
        elif channel is not None:
            where = f"in **#{discord.utils.escape_markdown(channel.name)}**"
        else:
            where = f"in <#{row['channel_id']}>"
        return f"[Message Link]({url}) {where}: {snippet}"

    @commands.hybrid_command(name="list_collaborative_messages", description="List all collaborative messages on the server")
    @commands.guild_only()
    @requires("administrator")
    async def list_collaborative_messages(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        rows = await self.db.all(
            "SELECT p.channel_id, p.message_id, p.summary, p.deleted_at FROM collab_posts c JOIN posted_messages p ON p.id = c.posted_id AND p.guild_id = c.guild_id "
            "WHERE c.guild_id = ? ORDER BY p.message_id", (ctx.guild.id,),
        )
        if not rows:
            await ctx.send("There are no collaborative messages on the server.", ephemeral=True)
            return
        entries = []
        for row in rows:
            if row["deleted_at"] is not None:
                continue
            # An archived thread isn't in the cache, so it's looked up, and a row whose channel can't be found or read stays out.
            channel = await self.place(ctx.guild, row["channel_id"])
            if channel is not None and access.can_read(ctx.author, channel):
                entries.append(self._entry(ctx.guild, row, channel))
        if not entries:
            await ctx.send("No accessible collaborative messages found on the server.", ephemeral=True)
            return
        pages = [entries[start:start + PAGE_SIZE] for start in range(0, len(entries), PAGE_SIZE)]
        view = ListView(pages, ctx.author.id)
        await ctx.send(embed=view.embed(), view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def prune(self, guild_id: int, *, seconds: float = PRUNE_SECONDS) -> tuple[int, int, bool]:
        """Forgets collaborative posts whose message or channel is gone. Only Discord's answer that one doesn't exist counts, so a message the bot can't read,
        or one in a thread Discord refused or failed to look up, stays, since it may still be there. The library's deleted mark doesn't count either, because a
        refused lookup can set it. Returns (posts pruned, orphaned records removed, whether it finished in time)."""
        ops = self.bot.ops
        deadline = time.monotonic() + seconds
        pruned = 0
        finished = True
        rows = await self.db.all(
            "SELECT p.id AS posted_id, p.channel_id, p.message_id FROM collab_posts c JOIN posted_messages p ON p.id = c.posted_id AND p.guild_id = c.guild_id WHERE c.guild_id = ? ORDER BY c.id",
            (guild_id,),
        )
        for row in rows:
            if time.monotonic() > deadline:
                finished = False
                break
            state = await ops.place_state(guild_id, row["channel_id"])
            gone = state == "gone"
            if state == "here":
                try:
                    await ops.fetch_message(row["channel_id"], row["message_id"])
                except MessageGone:
                    gone = True
                except OpsError:
                    gone = False
            if gone:
                await self.forget(guild_id, row["posted_id"])
                pruned += 1
        async with self.db.transaction():
            orphans = await self.db.run("DELETE FROM collab_posts WHERE guild_id = ? AND posted_id NOT IN (SELECT id FROM posted_messages WHERE guild_id = ?)", (guild_id, guild_id))
            orphans += await self.db.run("DELETE FROM collab_editors WHERE guild_id = ? AND posted_id NOT IN (SELECT posted_id FROM collab_posts WHERE guild_id = ?)", (guild_id, guild_id))
        return pruned, orphans, finished

    @commands.hybrid_command(name="prune", description="Prune collaborative message records for messages that no longer exist")
    @commands.guild_only()
    @requires("administrator")
    @staff_only("Owner and Administrators")
    async def prune_command(self, ctx: commands.Context) -> None:
        # The old bot checked Administrator itself. The bot manager role and bypass roles pass an Administrator command, so this checks for itself, as the
        # mover's prune does to match its page.
        if not access.is_admin(ctx.author):
            await ctx.fail(PRUNE_REFUSED)
            return
        await ctx.defer(ephemeral=True)
        pruned, orphans, finished = await self.prune(ctx.guild.id)
        more = "" if finished else f" It stopped after {PRUNE_SECONDS // 60} minutes, so run it again to check the rest."
        await ctx.ok(f"Pruned {pruned} message(s) and {orphans} orphaned record(s) from the database.{more}")
        await self.log_line(ctx.guild.id, f"{self.who(ctx.author)} pruned {pruned} collaborative message record(s).")

    # ---- creator roles and the log channel ----

    async def _role_names(self, guild: Any) -> str:
        return ", ".join(discord.utils.escape_markdown(role.name) for role in (guild.get_role(role_id) for role_id in self.cfg(guild.id)["creator_roles"]) if role is not None)

    @commands.hybrid_command(name="allow_collaboration_creation", description="Allow a role to create collaborative messages")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(role="The role to allow access to collaborative message creation")
    async def allow_collaboration_creation(self, ctx: commands.Context, role: discord.Role) -> None:
        refusal = access.staff_role_refusal(ctx.author, role)
        if refusal:
            await ctx.fail(refusal)
            return
        current = list(self.cfg(ctx.guild.id)["creator_roles"])
        if role.id not in current:
            if len(current) >= MAX_CREATOR_ROLES:
                await ctx.fail(f"Up to {MAX_CREATOR_ROLES} roles can create collaborative messages.")
                return
            await self.bot.settings.save(ctx.guild.id, self.key, {"creator_roles": [*current, role.id]}, actor=self.bot.actor(ctx.author), action="collaborative.allow")
        name = discord.utils.escape_markdown(role.name)
        await ctx.ok(f"Role {name} has been granted access to create collaborative messages.\nCurrent roles with access: {await self._role_names(ctx.guild)}")
        await self.log_line(ctx.guild.id, f"{self.who(ctx.author)} allowed role {name} to create collaborative messages.")

    @commands.hybrid_command(name="deny_collaboration_creation", description="Deny a role access to create collaborative messages")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(role="The role to remove access to collaborative message creation")
    async def deny_collaboration_creation(self, ctx: commands.Context, role: discord.Role) -> None:
        refusal = access.staff_role_refusal(ctx.author, None)
        if refusal:
            await ctx.fail(refusal)
            return
        current = list(self.cfg(ctx.guild.id)["creator_roles"])
        if role.id in current:
            await self.bot.settings.save(ctx.guild.id, self.key, {"creator_roles": [r for r in current if r != role.id]}, actor=self.bot.actor(ctx.author), action="collaborative.deny")
        name = discord.utils.escape_markdown(role.name)
        await ctx.ok(f"Role {name} has been denied access to create collaborative messages.\nCurrent roles with access: {await self._role_names(ctx.guild)}")
        await self.log_line(ctx.guild.id, f"{self.who(ctx.author)} denied role {name} access to create collaborative messages.")

    @commands.hybrid_command(name="collaborative_log_channel", description="Set the log channel and enable/disable logging")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(channel="The channel where logs should be sent", enabled="0 to disable logging, 1 to enable logging")
    async def collaborative_log_channel(self, ctx: commands.Context, channel: discord.TextChannel, enabled: commands.Range[int, 0, 1]) -> None:
        refusal = access.post_refusal(ctx.author, channel)
        if refusal:
            await ctx.fail(refusal)
            return
        await self.bot.settings.save(ctx.guild.id, self.key, {"log_channel": channel.id, "log_enabled": bool(enabled)}, actor=self.bot.actor(ctx.author), action="collaborative.log_channel")
        status = "enabled" if enabled else "disabled"
        await self.log_line(ctx.guild.id, f"{self.who(ctx.author)} set log channel to {channel.mention} and logging {status}.")
        await ctx.ok(f"Log channel set to {channel.mention} and logging {status}.")

    # ---- the right-click menus ----

    async def _menu_post(self, interaction: discord.Interaction, message: discord.Message, refusal: str = NOT_COLLABORATIVE) -> dict | None:
        """The collaborative post a menu was used on, or None after answering why not."""
        if interaction.guild is None or not self.on_in(interaction.guild.id):
            await _answer(interaction, OFF)
            return None
        post = await self.post_of(interaction.guild.id, message.id)
        if post is None:
            await _answer(interaction, refusal)
        return post

    async def menu_edit(self, interaction: discord.Interaction, message: discord.Message) -> None:
        post = await self._menu_post(interaction, message)
        if post is None:
            return
        refusal = await self.change_refusal(interaction.user, post)
        if refusal:
            await _answer(interaction, refusal)
            return
        kind, text = text_of(messages.from_message(message))
        limit = CONTENT_FORM_MAX if kind == "content" else FORM_MAX
        if len(text) > limit:
            await _answer(interaction, "This message's text is longer than a form can hold. Edit it on the dashboard's Embeds page.")
            return
        await interaction.response.send_modal(EditForm(self, post, text, limit))

    async def submit_edit(self, interaction: discord.Interaction, post: dict, text: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        post = await self.post_by_number(post["guild_id"], post["posted_id"])
        if post is None:
            await _answer(interaction, NOT_COLLABORATIVE)
            return
        refusal = await self.change_refusal(interaction.user, post) or await self.automod_problem(interaction.user, text)
        if refusal:
            await _answer(interaction, refusal)
            return
        # The thread can archive while the form is open, and an Administrator's check above doesn't look it up.
        await self.bot.ops.find_thread(post["guild_id"], post["channel_id"])
        try:
            current = await self.bot.ops.fetch_message(post["channel_id"], post["message_id"])
            payload = with_text(current["payload"], text)
            result = await self.embeds.edit_post(post["guild_id"], post["channel_id"], post["message_id"], payload, actor=self.bot.actor(interaction.user))
        except (OpsError, Invalid) as error:
            await _answer(interaction, str(error))
            return
        await _answer(interaction, f"Message {post['message_id']} successfully updated!")
        if result["changed"]:
            await self._record(post["guild_id"], interaction.user, "edit", messages.summary(payload, 80))
            await self.log_line(post["guild_id"], f"{self.who(interaction.user)} edited collaborative message {self.link(post)}, now version {result['version']}.")

    async def menu_history(self, interaction: discord.Interaction, message: discord.Message) -> None:
        post = await self._menu_post(interaction, message)
        if post is None:
            return
        member = interaction.user
        channel = await self.place(interaction.guild, post["channel_id"])
        refusal = access.read_refusal(member, channel) if channel is not None else None
        if refusal:
            await _answer(interaction, refusal)
            return
        revisions = (await self.embeds.versions(post["guild_id"], post["posted_id"]))[:-1]
        if not revisions:
            await _answer(interaction, "No revisions found for this message.")
            return
        panel = RevisionPanel(self, post, revisions, may_revert=await self.change_refusal(member, post) is None, user_id=member.id)
        await interaction.response.send_message(embed=panel.embed(), view=panel, ephemeral=True)
        await self.log_line(post["guild_id"], f"{self.who(member)} viewed the revision history for message {self.link(post)}.")

    async def _revert(self, interaction: discord.Interaction, post: dict, version: dict) -> dict | None:
        """Puts an earlier text back as a new version, so the text it replaces stays in the history too. None after answering why it couldn't."""
        refusal = await self.change_refusal(interaction.user, post)
        if refusal:
            await _answer(interaction, refusal)
            return None
        try:
            return await self.embeds.edit_post(post["guild_id"], post["channel_id"], post["message_id"], version["payload"], actor=self.bot.actor(interaction.user), note=f"Reverted to version {version['number']}")
        except OpsError as error:
            await _answer(interaction, str(error))
            return None

    async def revert_from_panel(self, interaction: discord.Interaction, post: dict, version: dict, shown: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        current = await self.post_by_number(post["guild_id"], post["posted_id"])
        if current is None:
            await _answer(interaction, NOT_COLLABORATIVE)
            return
        if await self._revert(interaction, current, version) is None:
            return
        await _answer(interaction, f"Reverted message {current['message_id']} to revision {shown}.")
        await self._record(current["guild_id"], interaction.user, "revert", f"{current['summary'][:80]} (version {version['number']})")
        await self.log_line(current["guild_id"], f"{self.who(interaction.user)} reverted message {self.link(current)} to revision {shown}.")

    async def menu_revert(self, interaction: discord.Interaction, message: discord.Message) -> None:
        post = await self._menu_post(interaction, message)
        if post is None:
            return
        refusal = await self.change_refusal(interaction.user, post)
        if refusal:
            await _answer(interaction, refusal)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        versions = await self.embeds.versions(post["guild_id"], post["posted_id"])
        target = previous_version(versions)
        if target is None:
            await _answer(interaction, "No previous revisions exist for this message.")
            return
        if await self._revert(interaction, post, next(row for row in versions if row["number"] == target)) is None:
            return
        await _answer(interaction, "Message successfully reverted to the previous revision.")
        await self._record(post["guild_id"], interaction.user, "revert", f"{post['summary'][:80]} (version {target})")
        await self.log_line(post["guild_id"], f"{self.who(interaction.user)} reverted message {self.link(post)} to the previous revision.")

    async def menu_delete(self, interaction: discord.Interaction, message: discord.Message) -> None:
        post = await self._menu_post(interaction, message)
        if post is None:
            return
        refusal = await self.change_refusal(interaction.user, post)
        if refusal:
            await _answer(interaction, refusal)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.ops.delete_message(post["channel_id"], post["message_id"])
        except OpsError as error:
            await _answer(interaction, str(error))
            return
        await self.forget(post["guild_id"], post["posted_id"])
        await _answer(interaction, "Message and all associated revisions successfully deleted.")
        await self._record(post["guild_id"], interaction.user, "delete", post["summary"][:80])
        await self.log_line(post["guild_id"], f"{self.who(interaction.user)} deleted collaborative message {post['message_id']}.")

    async def menu_collaborators(self, interaction: discord.Interaction, message: discord.Message) -> None:
        post = await self._menu_post(interaction, message, NOT_COLLABORATIVE_HERE)
        if post is None:
            return
        users, roles = await self.collaborators(post["guild_id"], post["posted_id"])
        user_text = ", ".join(await self._names_of_users(interaction.guild, users)) or "None"
        role_text = ", ".join(self._names_of_roles(interaction.guild, roles)) or "None"
        head = f"**Current Collaborators for Message {message.id}:**\n\n"
        roles_line = f"\n**Roles:** {role_text}"
        room = 2000 - len(head) - len(roles_line) - len("**Users:** ")
        if len(user_text) > room:
            user_text = user_text[:room - 3].rsplit(", ", 1)[0] + "..."
        await _answer(interaction, f"{head}**Users:** {user_text}{roles_line}"[:2000])

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/collaborative/posts", self._posts_endpoint)
        router.put(r"/collaborative/posts/{posted_id:\d+}", self._collaborators_endpoint)
        router.get("/collaborative/import", self._import_status_endpoint)
        router.post("/collaborative/import", self._import_endpoint)

    async def _posts_endpoint(self, req: Any) -> dict:
        rows = await self.db.all(
            "SELECT c.posted_id, p.channel_id, p.message_id, p.summary, p.deleted_at, p.posted_by_name, p.posted_at, p.edited_at FROM collab_posts c "
            "JOIN posted_messages p ON p.id = c.posted_id AND p.guild_id = c.guild_id WHERE c.guild_id = ? ORDER BY c.id DESC LIMIT ?", (req.guild_id, MAX_POSTS),
        )
        guild = self.bot.get_guild(req.guild_id)
        for channel_id in {row["channel_id"] for row in rows}:
            if guild is not None and guild.get_channel_or_thread(channel_id) is None:
                await req.ops.find_thread(req.guild_id, channel_id)  # an archived thread, so a private one is judged as one
        rows = [row for row in rows if req.can_read(row["channel_id"])]
        editors: dict[int, dict[str, list[int]]] = {}
        for row in await self.db.all("SELECT posted_id, kind, target_id FROM collab_editors WHERE guild_id = ? ORDER BY id", (req.guild_id,)):
            editors.setdefault(row["posted_id"], {"user": [], "role": []}).setdefault(row["kind"], []).append(row["target_id"])
        for row in rows:
            found = editors.get(row["posted_id"], {})
            row["users"] = found.get("user", [])
            row["roles"] = found.get("role", [])
            row["url"] = jump_url(req.guild_id, row["channel_id"], row["message_id"])
        return {"rows": rows, "limits": {"users": MAX_USERS, "roles": MAX_ROLES}}

    def _people_form(self, req: Any, form: dict, had_users: list[int], had_roles: list[int]) -> tuple[list[int], list[int]]:
        """The lists a form asks for, checked. Someone added has to be in the server, and a role has to exist and can't be @everyone."""
        try:
            users = snowflakes(MAX_USERS)(form.get("users") if form.get("users") is not None else [])
        except Invalid as error:
            raise ValidationError({"users": str(error)}) from error
        try:
            roles = snowflakes(MAX_ROLES)(form.get("roles") if form.get("roles") is not None else [])
        except Invalid as error:
            raise ValidationError({"roles": str(error)}) from error
        guild = self.bot.get_guild(req.guild_id)
        for role_id in roles:
            if role_id in had_roles:
                continue
            if role_id == req.guild_id:
                raise ValidationError({"roles": access.NOT_EVERYONE})
            if guild is not None and guild.get_role(role_id) is None:
                raise ValidationError({"roles": "That role isn't in this server."})
        for user_id in users:
            if user_id not in had_users and req.ops.member(req.guild_id, user_id) is None:
                raise ValidationError({"users": "That person isn't in this server."})
        return users, roles

    async def _collaborators_endpoint(self, req: Any) -> dict:
        post = await self.post_by_number(req.guild_id, req.int_param("posted_id"))
        if post is not None:
            await req.ops.find_thread(req.guild_id, post["channel_id"])  # an archived thread, so a private one is judged as one, as the list does
        if post is None or not req.can_read(post["channel_id"]):
            raise ApiError(404, "That collaborative message isn't in the list anymore.")
        form = await req.json()
        had_users, had_roles = await self.collaborators(req.guild_id, post["posted_id"])
        users, roles = self._people_form(req, form, had_users, had_roles)
        if set(users) - set(had_users) or set(roles) - set(had_roles):
            # Letting someone change a post the bot made there is handing on the right to post in that channel.
            req.channel(post["channel_id"], "users", posting=True)
        await self.set_collaborators(req.guild_id, post["posted_id"], users, roles)
        await req.record("collaborative.collaborators", detail=post["summary"][:80])
        await self.log_line(req.guild_id, f"{discord.utils.escape_markdown(req.actor.name)} ({req.actor.id}) changed the collaborators of message {self.link(post)} on the dashboard.")
        return {"posted_id": post["posted_id"], "users": users, "roles": roles}

    # ---- bringing posts over from another bot ----

    async def _adopted(self, guild_id: int, source_message_id: int) -> dict | None:
        return await self.db.one(
            "SELECT * FROM posted_messages WHERE guild_id = ? AND source_message_id = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1", (guild_id, source_message_id),
        )

    @staticmethod
    def _only_admins(req: Any) -> None:
        """Making a collaborative message takes an Administrator or a creator role, and bringing one over also writes its whole history, so the routes for
        it are the owner's and Administrators'."""
        if not req.is_admin:
            raise ApiError(403, "Only the server owner or an Administrator can bring collaborative messages over.")

    async def _import_status_endpoint(self, req: Any) -> dict:
        """For each message another bot posted, whether it was reposted here and whether its repost is a collaborative message yet."""
        self._only_admins(req)
        wanted = [part for part in req.query.get("ids", "").split(",") if part]
        if len(wanted) > MAX_POSTS:
            raise ApiError(400, f"One call can ask about at most {MAX_POSTS:,} messages.")
        out = []
        for part in wanted:
            try:
                source = snowflake(part)
            except Invalid as error:
                raise ValidationError({"ids": str(error)}) from error
            row = await self._adopted(req.guild_id, source)
            out.append({"source_message_id": source, "posted_id": row["id"] if row else None, "message_id": row["message_id"] if row else None,
                        "collaborative": bool(row and await self.post_by_number(req.guild_id, row["id"]))})
        return {"rows": out}

    async def _import_endpoint(self, req: Any) -> dict:
        """Makes a message reposted from another bot a collaborative message, with its earlier texts in front of its history and its collaborators.
        Each earlier text takes the kind of the post: an embed's description when the post is only an embed, the message text otherwise."""
        self._only_admins(req)
        form = await req.json()
        try:
            source = snowflake(form.get("source_message_id"))
        except Invalid as error:
            raise ValidationError({"source_message_id": str(error)}) from error
        row = await self._adopted(req.guild_id, source)
        if row is None:
            raise ApiError(404, "That message hasn't been reposted here yet.")
        if await self.post_by_number(req.guild_id, row["id"]) is not None:
            raise ApiError(409, "That message is already a collaborative message.")
        await req.ops.find_thread(req.guild_id, row["channel_id"])
        req.channel(row["channel_id"], posting=True)
        try:
            written = number(0)(form.get("created_at", 0))
        except Invalid as error:
            raise ValidationError({"created_at": str(error)}) from error
        revisions = form.get("revisions") or []
        if not isinstance(revisions, list) or len(revisions) > 10_000:
            raise ValidationError({"revisions": "Send the revisions as a list."})
        versions = await self.embeds.versions(req.guild_id, row["id"])
        base = versions[-1]["payload"] if versions else messages.empty_payload()
        earlier: list[tuple[dict, float]] = []
        skipped: list[dict] = []
        # A revision is the text an edit replaced, stamped with the time of that edit. So each text was written when the one before it was replaced, and the first when the message was posted.
        when = written
        for index, revision in enumerate(revisions, 1):
            if not isinstance(revision, dict):
                skipped.append({"revision": index, "reason": "It isn't a revision."})
                continue
            try:
                replaced = number(0)(revision.get("replaced_at", when))
            except Invalid:
                replaced = when
            text = revision.get("text")
            try:
                if not isinstance(text, str) or not text.strip():
                    raise Invalid("It holds no text.")
                earlier.append((with_text(base, text), when))
            except Invalid as error:
                skipped.append({"revision": index, "reason": str(error)})
            when = replaced
        try:
            users = _unique(snowflakes(10_000)(form.get("users") or []))
            asked_roles = _unique(snowflakes(10_000)(form.get("roles") or []))
        except Invalid as error:
            raise ValidationError({"users": str(error)}) from error
        guild = self.bot.get_guild(req.guild_id)
        roles = [role_id for role_id in asked_roles if role_id != req.guild_id and (guild is None or guild.get_role(role_id) is not None)]
        gone = [role_id for role_id in asked_roles if role_id not in roles]
        if await self._count(req.guild_id) >= MAX_POSTS:
            raise ApiError(409, f"This server has {MAX_POSTS:,} collaborative messages. Delete one first.")
        # Both or neither, so a run stopped halfway leaves no history for the next run to add again.
        async with self.db.transaction():
            added = await self.embeds.add_earlier_versions(req.guild_id, row["id"], earlier, note=EARLIER_NOTE)
            await self.make_collaborative(req.guild_id, row["id"], users[:MAX_USERS], roles[:MAX_ROLES])
        await req.record("collaborative.import", detail=row["summary"][:80])
        return {
            "posted_id": row["id"], "message_id": row["message_id"], "versions": added, "skipped": skipped, "users": len(users[:MAX_USERS]), "roles": len(roles[:MAX_ROLES]),
            "roles_left_out": gone, "over_the_limit": {"users": users[MAX_USERS:], "roles": roles[MAX_ROLES:]},
        }
