"""Role commands, ranks and temporary roles.

Bulk changes run as one job per server with a row in the database. The list of who still needs the change comes from the
members themselves each time a job starts, so a restart picks up where it stopped without replaying anything. A job that's been
stopped for hours waits to be asked for again.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import converter as _converter

from ..core.durations import humanize, parse_duration, split_leading_duration
from ..core.members import find_member, whole_member_list
from ..core.module import Module, requires
from ..core.perms import DANGEROUS_PERMISSIONS
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import answer_place, role_is_safe
from ..core.validate import Field, Invalid, Schema, hex_color, snowflakes

log = logging.getLogger(__name__)

# Adding and removing roles shares one Discord rate limit per server with everything else that changes roles, including the
# verify gate. This spacing keeps a job under that limit, so a member verifying during a long job doesn't queue behind it.
BULK_SPACING = 1.1
BULK_POST_SECONDS = 15
BULK_SAVE_EVERY = 25
BULK_FAILURES_TO_STOP = 10
# How long a job waits between looks while the member list is short, and the longest it waits out an outage before trying the same member again.
BULK_WAIT = 30.0
BULK_OUTAGE_PAUSE = 300.0
# A job that hasn't saved for this long was stopped for a good while, or came back from an old copy of the database. Nobody may want it now, so it waits to be asked for again.
BULK_RESUME_LIMIT = 6 * 3600
MAX_TEMP_SECONDS = 365 * 86400
EMBED_COLOR = 0x00A2F8
PAGE_CHARACTERS = 3800
# The most words a command reads as role names. Any member can send text to /rank, and this keeps a long message from costing anything.
MAX_ROLE_WORDS = 60
TOO_MANY_WORDS = f"That's too many words to read as roles. Name up to {MAX_ROLE_WORDS}."
# A bulk change this large asks first, since a slip takes a long time to undo.
BULK_CONFIRM_OVER = 100
# How long the same warning to staff stays quiet.
STAFF_NOTICE_GAP = 3600.0
# How often one member can use /rank. Each use changes roles, which shares a rate limit per server, and listing ranks walks the member list.
RANK_USES = 3
RANK_SECONDS = 10


_NAMED_ROLE = re.compile(r"<@&(\d{15,25})>|(\d{15,25})")


class TemporaryRoleBlocked(Exception):
    """A temporary role's time ended and the bot can't change it back yet."""


class RoleNames:
    """Role lookups for one piece of typed text. `guild.roles` sorts every role each time it's read, so this reads it once and looks names up in dicts."""

    def __init__(self, guild: Any) -> None:
        self.guild = guild
        self.roles = list(guild.roles)
        self.exact: dict[str, Any] = {}
        self.folded: dict[str, Any] = {}
        self.longest = 1
        for role in self.roles:
            self.exact.setdefault(role.name, role)
            self.folded.setdefault(role.name.casefold(), role)
            self.longest = max(self.longest, len(role.name.split()))

    def find(self, text: str, *, loose: bool = True) -> Any:
        text = text.strip().strip('"')
        if not text:
            return None
        match = _NAMED_ROLE.fullmatch(text)
        if match:
            return self.guild.get_role(int(match.group(1) or match.group(2)))
        exact = self.exact.get(text)
        if exact is not None:
            return exact
        folded = text.casefold()
        same = self.folded.get(folded)
        if same is not None or not loose:
            return same
        near = [r for r in self.roles if r.name.casefold().startswith(folded)]
        return near[0] if len(near) == 1 else None


def find_role(guild: Any, text: str, *, loose: bool = True) -> Any:
    """A role from what someone typed: a mention, an ID or a name in any case. Loose also takes the start of a name when only one role fits."""
    return RoleNames(guild).find(text, loose=loose)


def find_roles(guild: Any, text: str, names: RoleNames | None = None) -> tuple[list, list[str]]:
    """Every role named in free text, with what couldn't be matched. Names can hold spaces, so the longest run of words that names a role wins. No run is tried past the longest name the server has.

    Anyone can reach this through /rank, so text past MAX_ROLE_WORDS words raises Invalid before any matching.
    """
    words = text.split()
    if len(words) > MAX_ROLE_WORDS or text.count(",") > MAX_ROLE_WORDS:
        raise Invalid(TOO_MANY_WORDS)
    names = names or RoleNames(guild)
    found: list = []
    missing: list[str] = []
    if '"' in text:  # what's in quotes is one name, and the words around it read as usual
        for quoted, plain in re.findall(r'"([^"]*)"|([^"]+)', text):
            more, unmatched = ([names.find(quoted)], [quoted]) if quoted else find_roles(guild, plain, names)
            found += [role for role in more if role is not None and role not in found]
            missing += unmatched if not quoted or more[0] is None else []
        return found, missing

    def add(role: Any) -> None:
        if role not in found:
            found.append(role)

    if "," in text:
        for piece in text.split(","):
            if piece.strip():
                role = names.find(piece)
                if role is None:
                    missing.append(piece.strip())
                else:
                    add(role)
        return found, missing
    index = 0
    while index < len(words):
        for end in range(min(len(words), index + names.longest), index, -1):
            role = names.find(" ".join(words[index:end]), loose=end == index + 1)
            if role is not None:
                add(role)
                index = end
                break
        else:
            missing.append(words[index])
            index += 1
    return found, missing


_SIGN = re.compile(r"(?:^|(?<=\s))([+-])(?=\S)")


def parse_signed(guild: Any, text: str) -> tuple[list, list, list, list[str]]:
    """`+bot -human plain` as roles to add, roles to remove and roles to flip. Anything without a sign flips."""
    if len(text.split()) > MAX_ROLE_WORDS:
        raise Invalid(TOO_MANY_WORDS)
    names = RoleNames(guild)
    adds: list = []
    removes: list = []
    flips: list = []
    missing: list[str] = []
    parts = _SIGN.split(text.strip())
    unsigned, *rest = parts
    if unsigned.strip():
        found, lost = find_roles(guild, unsigned, names)
        flips.extend(found)
        missing.extend(lost)
    for sign, body in zip(rest[::2], rest[1::2]):
        found, lost = find_roles(guild, body, names)
        missing.extend(lost)
        (adds if sign == "+" else removes).extend(found)
    return adds, removes, flips, missing


def split_time(text: str) -> tuple[int | None, str]:
    """A length of time from either end of the text, and what's left. "2h bot" and "Some Role 2h" both work."""
    seconds, rest = split_leading_duration(text)
    if seconds is not None:
        return seconds, rest
    words = text.split()
    for count in range(min(len(words), 6), 0, -1):
        seconds = parse_duration(" ".join(words[-count:]))
        if seconds is not None:
            return seconds, " ".join(words[:-count])
    return None, text


class _LooseRole(commands.RoleConverter):
    """Text commands take a role's name in any case, as staff type it. Discord's own role picker covers slash commands."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.Role:
        try:
            return await super().convert(ctx, argument)
        except commands.RoleNotFound:
            role = find_role(ctx.guild, argument) if ctx.guild else None
            if role is None:
                raise
            return role


_converter.CONVERTER_MAPPING[discord.Role] = _LooseRole


async def role_counts(guild: Any) -> collections.Counter:
    """Members per role in one pass, which on a large server costs far less than asking each role for its members. It yields so it can't hold up reactions."""
    counts: collections.Counter = collections.Counter()
    for index, member in enumerate(guild.members):
        for role in member.roles:
            counts[role.id] += 1
        if index % 5000 == 4999:
            await asyncio.sleep(0)
    return counts


def tell_staff(module: Any, guild: Any, topic: str, title: str, text: str) -> None:
    """Something only a person can sort out. It goes to the log channel, once an hour per topic."""
    told = module.__dict__.setdefault("_staff_told", {})
    now = time.monotonic()
    last = told.get((guild.id, topic))
    if last is not None and now - last < STAFF_NOTICE_GAP:
        return
    if len(told) > 1000:
        told.clear()
    told[(guild.id, topic)] = now
    log.warning("%s in %s: %s", title, guild.id, text)
    logs = module.bot.module("logging")
    if logs is not None:
        logs.staff_notice(guild, title, text)


def missing_roles_sentence(missing: list[str]) -> str:
    """Staff and members both type this list, and a reply past Discord's limit would fail after the work is done."""
    return f"Couldn't find these roles: {', '.join(word[:60] for word in missing)}."[:1900]


async def rank_counts(guild: Any, role_ids: set[int]) -> collections.Counter:
    """Members for only these roles, with no role objects built for each member, since any member can ask for this."""
    counts: collections.Counter = collections.Counter()
    for index, member in enumerate(guild.members):
        held = getattr(member, "_roles", None)
        for role_id in held if held is not None else [role.id for role in member.roles]:
            if role_id in role_ids:
                counts[role_id] += 1
        if index % 5000 == 4999:
            await asyncio.sleep(0)
    return counts


async def send_pages(ctx: commands.Context, title: str, lines: list[str], *, color: int = EMBED_COLOR, empty: str = "Nothing to show.") -> None:
    """Long lists as as many embeds as they need. Role mentions in them never ping."""
    pages: list[str] = []
    current = ""
    for line in lines or [empty]:
        if current and len(current) + len(line) + 1 > PAGE_CHARACTERS:
            pages.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    pages.append(current)
    for index, page in enumerate(pages):
        name = title if len(pages) == 1 else f"{title} ({index + 1}/{len(pages)})"
        await ctx.send(embed=discord.Embed(title=name, description=page, color=color), allowed_mentions=discord.AllowedMentions.none())


def color_text(role: Any) -> str:
    value = role.color.value
    return f"#{value:06x}" if value else "none"


@dataclass
class BulkJob:
    guild_id: int
    action: str
    scope: str
    role_id: int
    base_role_id: int | None
    user_id: int
    channel_id: int | None
    message_id: int | None
    done: int = 0
    failed: int = 0
    total: int = 0
    started_at: float = 0.0
    run_started_at: float = 0.0
    run_done: int = 0
    canceled_by: str | None = None
    task: asyncio.Task | None = None
    pending_skipped: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.done)

    def seconds_left(self) -> float:
        """From how fast this run has gone, or the planned spacing until there's a measure."""
        each = (time.time() - self.run_started_at) / self.run_done if self.run_done >= 5 else BULK_SPACING
        return self.remaining * each


_BULK_WORDS = {
    ("add", "all"): "every member", ("add", "humans"): "every human", ("add", "bots"): "every bot", ("add", "in"): "everyone with",
    ("remove", "all"): "every member", ("remove", "humans"): "every human", ("remove", "bots"): "every bot", ("remove", "in"): "everyone with",
}


class Confirm(discord.ui.View):
    def __init__(self, author_id: int, yes: str = "Yes, remove them all") -> None:
        super().__init__(timeout=60)
        self.author_id = author_id
        self.answer = False
        self.yes.label = yes

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("Only the person who ran this command can use this.", ephemeral=True)
        return False

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.answer = True
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(view=None)
        self.stop()


class BulkCancel(discord.ui.View):
    """The button on a bulk job's progress post. It finds the job by server, so it still works after a restart."""

    def __init__(self, module: "Roles") -> None:
        super().__init__(timeout=None)
        self.module = module

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="role:bulk:cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.module.cancel_clicked(interaction)


class Roles(Module):
    key = "roles"
    category = "Roles"
    role_timers = ("temprole",)
    schema = Schema({
        "ranks": Field([], snowflakes()),
    })
    migrations = [
        """
        CREATE TABLE role_bulk_jobs (
            guild_id INTEGER PRIMARY KEY,
            action TEXT NOT NULL,
            scope TEXT NOT NULL,
            role_id INTEGER NOT NULL,
            base_role_id INTEGER,
            user_id INTEGER NOT NULL,
            channel_id INTEGER,
            message_id INTEGER,
            done INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL
        );
        """,
        "ALTER TABLE role_bulk_jobs ADD COLUMN saved_at REAL;",
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.jobs: dict[int, BulkJob] = {}
        self._cancel_view = BulkCancel(self)

    def roles_given(self, settings: dict) -> dict[str, set[int]]:
        return {"ranks": set(settings["ranks"])}

    def role_refusal(self, guild: Any, role: Any) -> str | None:
        return None if self._safe(guild, role) else f"{role.name} holds permissions that can do real damage, so the bot won't hand it out as a rank. To allow it, turn on elevated roles on the Commands page."

    async def on_load(self) -> None:
        self.bot.timers.handler("temprole", self._temp_due)
        self.bot.add_view(self._cancel_view)

    async def cog_unload(self) -> None:
        tasks = [job.task for job in self.jobs.values() if job.task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.resume_jobs()

    async def on_guild_restored(self, guild_id: int) -> None:
        """A backup can hold a bulk change that was running back then. Nobody asked for it now, so nothing older than this moment picks up again."""
        await self.db.run("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (f"role_bulk_restored:{guild_id}", str(time.time())))

    # ---- who may do what ----

    def _role_refusal(self, ctx: commands.Context, role: Any, *, as_author: bool = True) -> str | None:
        """Why this role can't be handed out or taken here. The bot has to outrank it, and so does the person asking, unless they own the server."""
        guild = ctx.guild
        if role.is_default():
            return "Everyone always has @everyone, so it can't be given or taken."
        if role.managed:
            return f"{role.mention} belongs to an integration, and Discord doesn't let anyone hand it out."
        if not guild.me.guild_permissions.manage_roles:
            return "The bot needs the Manage Roles permission for that."
        if role >= guild.me.top_role:
            return f"{role.mention} sits at or above the bot's highest role, so the bot can't touch it. Move the bot's role above it first."
        if as_author and ctx.author.id != guild.owner_id and role >= ctx.author.top_role:
            return f"{role.mention} sits at or above your highest role."
        return None

    def _member_refusal(self, ctx: commands.Context, member: Any) -> str | None:
        guild = ctx.guild
        if ctx.author.id == guild.owner_id or member.id == ctx.author.id:
            return None
        if member.id == guild.owner_id:
            return "Nobody can change the server owner's roles with this command."
        if member.top_role >= ctx.author.top_role:
            return f"{member.mention} is at or above your highest role."
        return None

    def _safe(self, guild: Any, role: Any) -> bool:
        return role_is_safe([name for name, on in role.permissions if on], self.bot.settings.get(guild.id, COMMANDS_MODULE))

    def _unsafe_note(self, role: Any) -> str:
        return (f"{role.mention} holds permissions that can do real damage, so the bot won't hand it out in bulk or as a rank. "
                "To allow it, turn on elevated roles on the Commands page.")

    async def _end_timer(self, guild_id: int, member_id: int, role_id: int) -> None:
        await self.bot.timers.cancel_key("temprole", f"{guild_id}:{member_id}:{role_id}")

    # ---- /role ----

    @commands.hybrid_group(name="role", description="Give, take and look up roles")
    @commands.guild_only()
    @requires("manage_roles")
    async def role_group(self, ctx: commands.Context, member: Optional[discord.Member] = None, *, role: Optional[str] = None) -> None:
        if member is None or not role:
            await ctx.send("Use `/role add`, `remove`, `custom`, `info`, `allroles` or `diagnose`, or one of the bulk commands: `all`, `humans`, `in`, `rall`.", ephemeral=True)
            return
        found = find_role(ctx.guild, role)
        if found is None:
            await ctx.fail(f"There's no role called `{role[:60]}`.")
            return
        await self._change(ctx, member, [], [], [found])

    async def _change(self, ctx: commands.Context, member: Any, adds: list, removes: list, flips: list) -> None:
        refusal = self._member_refusal(ctx, member)
        if refusal:
            await ctx.fail(refusal)
            return
        adds = list(adds)
        removes = list(removes)
        for role in flips:
            (removes if member.get_role(role.id) else adds).append(role)
        for role in [*adds, *removes]:
            refusal = self._role_refusal(ctx, role)
            if refusal:
                await ctx.fail(refusal)
                return
        reason = f"Used by {ctx.author} ({ctx.author.id})"
        if adds:
            await member.add_roles(*adds, reason=reason)
        if removes:
            await member.remove_roles(*removes, reason=reason)
        for role in [*adds, *removes]:
            await self._end_timer(ctx.guild.id, member.id, role.id)
        parts = []
        if adds:
            parts.append("gave " + ", ".join(r.mention for r in adds))
        if removes:
            parts.append("took " + ", ".join(r.mention for r in removes))
        await ctx.ok(f"{member.mention}: {' and '.join(parts) or 'nothing changed'}.")

    @role_group.command(name="add", description="Give a role to a member")
    @requires("manage_roles")
    @app_commands.describe(user="Who gets the role", role="The role to give")
    async def role_add(self, ctx: commands.Context, user: discord.Member, *, role: discord.Role) -> None:
        await self._change(ctx, user, [role], [], [])

    @role_group.command(name="remove", description="Take a role from a member")
    @requires("manage_roles")
    @app_commands.describe(user="Who loses the role", role="The role to take")
    async def role_remove(self, ctx: commands.Context, user: discord.Member, *, role: discord.Role) -> None:
        await self._change(ctx, user, [], [role], [])

    @role_group.command(name="custom", aliases=["c"], description="Add and remove several roles at once, such as +member -pending")
    @requires("manage_roles")
    @app_commands.describe(member="Whose roles change", roles="Roles with + to add and - to remove, such as +member -pending")
    async def role_custom(self, ctx: commands.Context, member: discord.Member, *, roles: str) -> None:
        adds, removes, flips, missing = parse_signed(ctx.guild, roles)
        if missing:
            await ctx.fail(missing_roles_sentence(missing))
            return
        if not adds and not removes and not flips:
            await ctx.fail("Name at least one role, such as `+member -pending`.")
            return
        await self._change(ctx, member, adds, removes, flips)

    @role_group.command(name="removeall", aliases=["purge", "clear"], description="Take every role the bot can from a member")
    @requires("manage_roles")
    @app_commands.describe(user="Who loses their roles")
    async def role_removeall(self, ctx: commands.Context, user: discord.Member) -> None:
        await ctx.defer(ephemeral=True)  # one call to Discord per role, which can pass the three seconds an answer gets
        refusal = self._member_refusal(ctx, user)
        if refusal:
            await ctx.fail(refusal)
            return
        taken = [r for r in user.roles if self._role_refusal(ctx, r) is None]
        if not taken:
            await ctx.fail(f"{user.mention} holds no role the bot and you can take.")
            return
        await user.remove_roles(*taken, reason=f"Used by {ctx.author} ({ctx.author.id})")
        for role in taken:
            await self._end_timer(ctx.guild.id, user.id, role.id)
        await ctx.ok(f"Took {len(taken)} roles from {user.mention}.")

    @role_group.command(name="create", description="Create a role")
    @requires("manage_roles")
    @app_commands.describe(name="The role's name", color="Six hex digits, such as FF0000", mentionable="Anyone can ping it", hoist="Show its members apart in the sidebar")
    async def role_create(self, ctx: commands.Context, name: str, color: Optional[str] = None, mentionable: bool = False, hoist: bool = False) -> None:
        if not 1 <= len(name) <= 100:
            await ctx.fail("A role's name can be 1 to 100 characters.")
            return
        value = hex_color(color) if color else 0
        role = await ctx.guild.create_role(name=name, color=discord.Color(value), mentionable=mentionable, hoist=hoist, reason=f"Used by {ctx.author} ({ctx.author.id})")
        await ctx.ok(f"Created {role.mention}.")

    @role_group.command(name="color", aliases=["colour"], description="Change a role's color")
    @requires("manage_roles")
    @app_commands.describe(role="The role to recolor", color="Six hex digits, such as FF0000")
    async def role_color(self, ctx: commands.Context, role: discord.Role, color: str) -> None:
        refusal = self._role_refusal(ctx, role)
        if refusal:
            await ctx.fail(refusal)
            return
        await role.edit(color=discord.Color(hex_color(color)), reason=f"Used by {ctx.author} ({ctx.author.id})")
        await ctx.ok(f"{role.mention} is now {color}.")

    @role_group.command(name="info", description="Show a role's name, member count, color and age")
    @app_commands.describe(role="The role to look up")
    async def role_info(self, ctx: commands.Context, *, role: discord.Role) -> None:
        embed = discord.Embed(title=role.name, color=role.color.value or EMBED_COLOR)
        embed.add_field(name="Members", value=f"{len(role.members):,}")
        embed.add_field(name="Color", value=color_text(role))
        embed.add_field(name="Created", value=discord.utils.format_dt(role.created_at, "D"))
        embed.add_field(name="Position", value=str(role.position))
        embed.add_field(name="Shown apart", value="yes" if role.hoist else "no")
        embed.add_field(name="Anyone can ping it", value="yes" if role.mentionable else "no")
        held = [name.replace("_", " ").title() for name, on in role.permissions if on]
        if held:
            embed.add_field(name="Permissions", value=", ".join(held)[:1024], inline=False)
        embed.set_footer(text=f"Role ID: {role.id}")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _show_all_roles(self, ctx: commands.Context) -> None:
        counts = await role_counts(ctx.guild)
        lines = [f"{role.mention} `{role.id}` {counts[role.id]:,} members" for role in reversed(ctx.guild.roles) if not role.is_default()]
        await send_pages(ctx, f"Roles in {ctx.guild.name}", lines, empty="This server has no roles yet.")

    @role_group.command(name="allroles", description="List every role in the server")
    async def role_allroles(self, ctx: commands.Context) -> None:
        await self._show_all_roles(ctx)

    async def _diagnose(self, ctx: commands.Context, role: Any) -> None:
        guild = ctx.guild
        me = guild.me
        lines = []
        if me.guild_permissions.manage_roles:
            lines.append("\N{WHITE HEAVY CHECK MARK} The bot has Manage Roles.")
        else:
            lines.append("\N{CROSS MARK} The bot doesn't have Manage Roles, so it can't change anyone's roles.")
        lines.append(f"The bot's highest role is {me.top_role.mention}.")
        if role is None:
            above = [r for r in guild.roles if r >= me.top_role and not r.is_default() and r != me.top_role]
            lines.append(f"{len(above)} roles sit above it. The bot can't hand those out.")
            await send_pages(ctx, "Role check", lines)
            return
        if role.is_default():
            lines.append("\N{CROSS MARK} Everyone always has @everyone.")
        elif role.managed:
            lines.append("\N{CROSS MARK} An integration owns this role, so nobody can hand it out.")
        elif role >= me.top_role:
            lines.append(f"\N{CROSS MARK} {role.mention} sits at or above the bot's highest role. Move the bot's role above it.")
        else:
            lines.append(f"\N{WHITE HEAVY CHECK MARK} {role.mention} sits below the bot's highest role.")
        if ctx.author.id != guild.owner_id and role >= ctx.author.top_role:
            lines.append("\N{CROSS MARK} It sits at or above your highest role, so you can't hand it out.")
        risky = [name.replace("_", " ").title() for name, on in role.permissions if on and name in DANGEROUS_PERMISSIONS]
        if risky:
            state = "on, so the bot hands it out" if self.bot.settings.get(guild.id, COMMANDS_MODULE)["elevated_roles"] else "off, so features that let members pick roles refuse it"
            lines.append(f"\N{WARNING SIGN} It holds {', '.join(risky)}. Elevated roles are {state}.")
        if role.id in self.cfg(guild.id)["ranks"]:
            lines.append("It's on the rank list.")
        reactions = self.bot.module("reactionroles")
        if reactions:
            used = await reactions.messages_using_role(guild.id, role.id)
            if used:
                lines.append(f"{len(used)} reaction role messages hand it out.")
        lines.append(f"{len(role.members):,} members hold it.")
        await send_pages(ctx, f"Role check: {role.name}", lines)

    @role_group.command(name="diagnose", description="Check why the bot can or can't hand out a role")
    @app_commands.describe(role="The role to check. Leave it out for an overall check.")
    async def role_diagnose(self, ctx: commands.Context, *, role: Optional[discord.Role] = None) -> None:
        await self._diagnose(ctx, role)

    # ---- bulk ----

    def _targets(self, guild: Any, job: BulkJob) -> list[int]:
        """Who still needs the change, by ID, so a member who left by the time their turn comes is simply skipped."""
        adding = job.action == "add"
        out = []
        job.pending_skipped = 0
        for member in guild.members:
            if job.scope == "humans" and member.bot:
                continue
            if job.scope == "bots" and not member.bot:
                continue
            if job.scope == "in" and member.get_role(job.base_role_id) is None:
                continue
            holds = member.get_role(job.role_id) is not None
            if holds == adding:
                continue
            if adding and member.pending:  # Discord refuses roles for someone still on the rules screen
                job.pending_skipped += 1
                continue
            out.append(member.id)
        return out

    def _bulk_title(self, guild: Any, job: BulkJob) -> str:
        role = guild.get_role(job.role_id)
        name = role.name if role else "a deleted role"
        return f"Adding {name}" if job.action == "add" else f"Removing {name}"

    def _bulk_line(self, guild: Any, job: BulkJob) -> str:
        role = f"<@&{job.role_id}>"
        who = _BULK_WORDS[(job.action, job.scope)]
        if job.scope == "in":
            who = f"{who} <@&{job.base_role_id}>"
        return f"{'Gives' if job.action == 'add' else 'Takes'} {role} {'to' if job.action == 'add' else 'from'} {who}."

    def _progress_embed(self, guild: Any, job: BulkJob, *, ending: str | None = None) -> discord.Embed:
        total = max(job.total, job.done)
        share = job.done / total if total else 1
        filled = round(share * 20)
        lines = [self._bulk_line(guild, job), f"`{'#' * filled}{'.' * (20 - filled)}` {job.done:,} of {total:,}"]
        if job.failed:
            lines.append(f"{job.failed:,} failed.")
        if ending:
            lines.append(ending)
        else:
            lines.append(f"About {humanize(job.seconds_left(), parts=2)} left. Cancel with the button or `/role bulk-cancel`.")
        color = 0x53DDAC if ending and not job.canceled_by else EMBED_COLOR
        return discord.Embed(title=self._bulk_title(guild, job), description="\n".join(lines), color=color)

    def _post_channel(self, ctx: commands.Context) -> Any:
        """Where the progress post goes, following the server's mute and redirect choices. The job outlasts a slash reply, which Discord stops letting a bot edit after 15 minutes, so the post is a plain message."""
        decision = getattr(ctx, "decision", None)
        if decision is None:
            return ctx.channel
        if decision.muted:
            return None
        target = decision.redirect or decision.restricted_channel
        if target and target != "dm":
            return answer_place(ctx.guild, target) or ctx.channel
        return ctx.channel

    async def _start_bulk(self, ctx: commands.Context, action: str, scope: str, role: Any, base: Any = None) -> None:
        guild = ctx.guild
        running = self.jobs.get(guild.id)
        if running:
            await ctx.fail(f"A bulk change is already running: {self._bulk_line(guild, running)} Only one runs at a time. Cancel it with `/role bulk-cancel`.")
            return
        refusal = self._role_refusal(ctx, role)
        if refusal:
            await ctx.fail(refusal)
            return
        if action == "add" and not self._safe(guild, role):
            await ctx.fail(self._unsafe_note(role))
            return
        now = time.time()
        job = BulkJob(guild.id, action, scope, role.id, base.id if base else None, ctx.author.id, None, None, started_at=now, run_started_at=now)
        # Registered before the first await, so a second command in the steps below finds it and stops at the check above.
        self.jobs[guild.id] = job
        try:
            started = await self._prepare_bulk(ctx, job)
        except BaseException:
            started = False
            raise
        finally:
            if not started and self.jobs.get(guild.id) is job:
                del self.jobs[guild.id]
        if started and ctx.interaction is not None:
            await ctx.ok(f"Started. {self._bulk_line(guild, job)} That's {job.total:,} members, about {humanize(job.seconds_left(), parts=2)}.")

    async def _prepare_bulk(self, ctx: commands.Context, job: BulkJob) -> bool:
        """Everything that can stop a job before it runs. Returns whether it's running."""
        guild = ctx.guild
        await ctx.defer(ephemeral=True)
        if not await whole_member_list(guild):
            await ctx.fail("The bot's member list for this server isn't complete yet, so it can't tell who needs that. Try again in a minute.")
            return False
        job.total = len(self._targets(guild, job))
        if job.total == 0:
            await ctx.ok("Nobody needs that change.")
            return False
        if job.total >= BULK_CONFIRM_OVER and not await self._confirm_bulk(ctx, job):
            await ctx.fail("Left everything as it was.")
            return False
        if job.canceled_by:
            await ctx.fail(f"Canceled by {job.canceled_by}.")
            return False
        target = self._post_channel(ctx)
        if target is not None:
            sent = await target.send(embed=self._progress_embed(guild, job), view=self._cancel_view, allowed_mentions=discord.AllowedMentions.none())
            job.channel_id, job.message_id = target.id, sent.id
        await self.db.run(
            "INSERT OR REPLACE INTO role_bulk_jobs (guild_id, action, scope, role_id, base_role_id, user_id, channel_id, message_id, total, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",  # a job that stopped on an error may still have its row
            (guild.id, job.action, job.scope, job.role_id, job.base_role_id, ctx.author.id, job.channel_id, job.message_id, job.total, job.started_at),
        )
        job.task = asyncio.create_task(self._run_job(guild.id, job), name=f"role-bulk-{guild.id}")
        return True

    async def _confirm_bulk(self, ctx: commands.Context, job: BulkJob) -> bool:
        view = Confirm(ctx.author.id, yes="Yes, start it")
        prompt = f"{self._bulk_line(ctx.guild, job)} That's {job.total:,} members and takes about {humanize(job.seconds_left(), parts=2)}. Start it?"
        sent = await ctx.send(prompt, view=view, allowed_mentions=discord.AllowedMentions.none())
        if sent is None:
            return False
        await view.wait()
        return view.answer

    async def _wait_for_members(self, guild_id: int, job: BulkJob) -> Any:
        """The server once its member list is whole. A list made from part of it would end the job early and call it finished. The job's row stays put through an outage."""
        while True:
            guild = self.bot.get_guild(guild_id)
            if guild is None or job.canceled_by or await whole_member_list(guild):
                return guild
            await asyncio.sleep(BULK_WAIT)

    async def _change_one(self, job: BulkJob, member: Any, role: Any, reason: str) -> None:
        """One member's change, waiting out an outage rather than ending a job that may have hours left. Raises what trying again wouldn't fix."""
        pauses = 0
        while not job.canceled_by:
            try:
                if job.action == "add":
                    await member.add_roles(role, reason=reason)
                else:
                    await member.remove_roles(role, reason=reason)
                return
            except discord.HTTPException as error:
                if error.status < 500 and error.status != 429:
                    raise
                log.warning("Bulk role change in %s is waiting out a Discord problem: %s", job.guild_id, error)
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:
                log.warning("Bulk role change in %s is waiting out a network problem: %s", job.guild_id, error)
            pauses += 1
            await asyncio.sleep(min(BULK_OUTAGE_PAUSE, BULK_SPACING * 2 ** min(pauses, 10)))

    async def _run_job(self, guild_id: int, job: BulkJob) -> None:
        guild = self.bot.get_guild(guild_id)
        ending = "Finished."
        keep_row = False
        try:
            guild = await self._wait_for_members(guild_id, job)
            if job.canceled_by:
                ending = f"Canceled by {job.canceled_by}."
                return
            if guild is None or guild.get_role(job.role_id) is None:
                ending = "Stopped, because the role no longer exists."
                return
            targets = self._targets(guild, job)
            job.total = job.done + len(targets)
            job.run_started_at = time.time()
            job.run_done = 0
            last_post = time.monotonic()
            failures_in_a_row = 0
            reason = f"Bulk role change started by {job.user_id}"
            for index, user_id in enumerate(targets):
                if job.canceled_by:
                    ending = f"Canceled by {job.canceled_by}."
                    return
                role = guild.get_role(job.role_id)
                if role is None:
                    ending = "Stopped, because the role no longer exists."
                    return
                member = guild.get_member(user_id)
                if member is not None:
                    try:
                        await self._change_one(job, member, role, reason)
                        failures_in_a_row = 0
                    except discord.NotFound:
                        pass  # they left between the list and their turn
                    except discord.HTTPException as error:
                        job.failed += 1
                        failures_in_a_row += 1
                        log.warning("Bulk role change in %s failed for %s: %s", guild_id, user_id, error)
                        if failures_in_a_row >= BULK_FAILURES_TO_STOP:
                            ending = f"Stopped after {BULK_FAILURES_TO_STOP} failures in a row. Check the bot's permissions and where its role sits, then start it again."
                            return
                job.done += 1
                job.run_done += 1
                if index % BULK_SAVE_EVERY == BULK_SAVE_EVERY - 1:
                    await self.db.run("UPDATE role_bulk_jobs SET done = ?, failed = ?, total = ?, saved_at = ? WHERE guild_id = ?", (job.done, job.failed, job.total, time.time(), guild_id))
                if time.monotonic() - last_post >= BULK_POST_SECONDS:
                    last_post = time.monotonic()
                    await self._update_post(guild, job)
                await asyncio.sleep(BULK_SPACING)
            if job.canceled_by:
                ending = f"Canceled by {job.canceled_by}."
            elif job.pending_skipped:
                ending = f"Finished. {job.pending_skipped:,} member{' was' if job.pending_skipped == 1 else 's were'} skipped for being on the rules screen."
        except asyncio.CancelledError:
            # The bot is stopping. The row stays, so the job picks up at the next start.
            ending = None
            raise
        except Exception:
            log.exception("Bulk role change in %s failed", guild_id)
            ending = "Stopped by an error. It's been logged, and it picks up at the next start."
            keep_row = True
        finally:
            if self.jobs.get(guild_id) is job:
                del self.jobs[guild_id]
            if ending is not None:
                if not keep_row:
                    await self.db.run("DELETE FROM role_bulk_jobs WHERE guild_id = ?", (guild_id,))
                if guild is not None:
                    await self._update_post(guild, job, ending=ending)

    async def _update_post(self, guild: Any, job: BulkJob, *, ending: str | None = None) -> None:
        if not job.message_id:
            return
        channel = guild.get_channel_or_thread(job.channel_id)
        if channel is None:
            return
        try:
            await channel.get_partial_message(job.message_id).edit(embed=self._progress_embed(guild, job, ending=ending), view=None if ending else self._cancel_view, allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            job.message_id = None
        except (discord.HTTPException, aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:
            log.warning("Couldn't update a bulk role post in %s: %s", guild.id, error)

    async def resume_jobs(self) -> None:
        """Pick up any job a restart interrupted."""
        for row in await self.db.all("SELECT * FROM role_bulk_jobs"):
            try:
                await self._resume_job(row)
            except Exception:  # one bad row must not keep another server's job from picking up
                log.exception("Could not resume the bulk role job of %s", row["guild_id"])

    async def _resume_job(self, row: dict) -> None:
        guild_id = row["guild_id"]
        if guild_id in self.jobs or self.bot.get_guild(guild_id) is None:
            return
        restored = await self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"role_bulk_restored:{guild_id}",))
        if restored is not None and row["started_at"] < float(restored):
            await self.db.run("DELETE FROM role_bulk_jobs WHERE guild_id = ?", (guild_id,))
            return
        job = BulkJob(guild_id, row["action"], row["scope"], row["role_id"], row["base_role_id"], row["user_id"], row["channel_id"], row["message_id"],
                      done=row["done"], failed=row["failed"], total=row["total"], started_at=row["started_at"], run_started_at=time.time())
        if time.time() - (row["saved_at"] or row["started_at"]) > BULK_RESUME_LIMIT:
            await self.db.run("DELETE FROM role_bulk_jobs WHERE guild_id = ?", (guild_id,))
            await self._update_post(self.bot.get_guild(guild_id), job, ending="Stopped. It was interrupted a long time ago, so it didn't pick up by itself. Start it again if it's still wanted.")
            return
        self.jobs[guild_id] = job
        job.task = asyncio.create_task(self._run_job(guild_id, job), name=f"role-bulk-{guild_id}")

    async def cancel_clicked(self, interaction: discord.Interaction) -> None:
        job = self.jobs.get(interaction.guild_id) if interaction.guild_id else None
        if job is None:
            try:
                await interaction.response.edit_message(view=None)
            except discord.HTTPException:
                pass
            return
        if job.message_id and interaction.message is not None and interaction.message.id != job.message_id:
            # A post left behind when its last edit failed. The job running now belongs to a different one.
            await interaction.response.edit_message(view=None)
            await interaction.followup.send("This post's job is over. The one running now has its own post.", ephemeral=True)
            return
        allowed = interaction.user.id == job.user_id or getattr(interaction.user.guild_permissions, "manage_roles", False)
        if not allowed:
            await interaction.response.send_message("You need Manage Roles to cancel this.", ephemeral=True)
            return
        job.canceled_by = interaction.user.display_name
        await interaction.response.send_message("Canceling. It stops after the member it's working on.", ephemeral=True)

    @role_group.command(name="bulk-cancel", description="Stop the running bulk role change")
    @requires("manage_roles")
    async def role_bulk_cancel(self, ctx: commands.Context) -> None:
        job = self.jobs.get(ctx.guild.id)
        if job is None:
            await ctx.fail("No bulk role change is running.")
            return
        job.canceled_by = ctx.author.display_name
        await ctx.ok("Canceling. It stops after the member it's working on.")

    @role_group.command(name="all", description="Add a role to every member")
    @requires("manage_roles")
    @app_commands.describe(role="The role to give")
    async def role_all(self, ctx: commands.Context, *, role: discord.Role) -> None:
        await self._start_bulk(ctx, "add", "all", role)

    @role_group.command(name="humans", description="Add a role to every member who isn't a bot")
    @requires("manage_roles")
    @app_commands.describe(role="The role to give")
    async def role_humans(self, ctx: commands.Context, *, role: discord.Role) -> None:
        await self._start_bulk(ctx, "add", "humans", role)

    @role_group.command(name="bots", description="Add a role to every bot")
    @requires("manage_roles")
    @app_commands.describe(role="The role to give")
    async def role_bots(self, ctx: commands.Context, *, role: discord.Role) -> None:
        await self._start_bulk(ctx, "add", "bots", role)

    @role_group.command(name="in", description="Add a role to everyone who has another role")
    @requires("manage_roles")
    @app_commands.describe(base_role="Who gets it: members with this role", new_role="The role to give")
    async def role_in(self, ctx: commands.Context, base_role: discord.Role, *, new_role: discord.Role) -> None:
        await self._start_bulk(ctx, "add", "in", new_role, base_role)

    @role_group.command(name="rall", description="Remove a role from every member")
    @requires("manage_roles")
    @app_commands.describe(role="The role to take")
    async def role_rall(self, ctx: commands.Context, *, role: discord.Role) -> None:
        await self._start_bulk(ctx, "remove", "all", role)

    @role_group.command(name="removebots", aliases=["rbots"], description="Remove a role from every bot")
    @requires("manage_roles")
    @app_commands.describe(role="The role to take")
    async def role_removebots(self, ctx: commands.Context, *, role: discord.Role) -> None:
        await self._start_bulk(ctx, "remove", "bots", role)

    @role_group.command(name="removehumans", aliases=["rhumans"], description="Remove a role from every member who isn't a bot")
    @requires("manage_roles")
    @app_commands.describe(role="The role to take")
    async def role_removehumans(self, ctx: commands.Context, *, role: discord.Role) -> None:
        await self._start_bulk(ctx, "remove", "humans", role)

    @role_group.command(name="removein", aliases=["rin"], description="Remove a role from everyone who has another role")
    @requires("manage_roles")
    @app_commands.describe(base_role="Who loses it: members with this role", new_role="The role to take")
    async def role_removein(self, ctx: commands.Context, base_role: discord.Role, *, new_role: discord.Role) -> None:
        await self._start_bulk(ctx, "remove", "in", new_role, base_role)

    # ---- /temprole ----

    @commands.hybrid_group(name="temprole", description="Give or take roles for a limited time")
    @commands.guild_only()
    @requires("manage_roles")
    async def temprole(self, ctx: commands.Context, member: Optional[discord.Member] = None, *, rest: Optional[str] = None) -> None:
        if member is None or not rest:
            await ctx.send("Use `/temprole add`, `custom` or `list`.", ephemeral=True)
            return
        await self._temp_add(ctx, member, rest)

    async def _temp_add(self, ctx: commands.Context, member: Any, text: str) -> None:
        seconds, rest = split_time(text)
        if seconds is None:
            await ctx.fail("That needs a length of time, such as `2h` or `3 days`.")
            return
        role = find_role(ctx.guild, rest)
        if role is None:
            await ctx.fail(f"There's no role called `{rest[:60]}`.")
            return
        await self._temp_change(ctx, member, seconds, [role], [])

    async def _temp_change(self, ctx: commands.Context, member: Any, seconds: int, adds: list, removes: list) -> None:
        if not 60 <= seconds <= MAX_TEMP_SECONDS:
            await ctx.fail("A temporary role can last from one minute to one year.")
            return
        refusal = self._member_refusal(ctx, member)
        if refusal:
            await ctx.fail(refusal)
            return
        for role in [*adds, *removes]:
            refusal = self._role_refusal(ctx, role)
            if refusal:
                await ctx.fail(refusal)
                return
        # A timer only ever undoes a change it made. A later run on a role still counting down moves the end of that same timer.
        for role, wanted, sentence in [*((r, True, "already has") for r in adds), *((r, False, "doesn't have") for r in removes)]:
            if (member.get_role(role.id) is not None) == wanted and await self.bot.timers.get_key("temprole", f"{ctx.guild.id}:{member.id}:{role.id}") is None:
                await ctx.fail(f"{member.mention} {sentence} {role.mention}, so there's nothing to undo when the time ends.")
                return
        reason = f"Temporary, used by {ctx.author} ({ctx.author.id})"
        adding = [role for role in adds if member.get_role(role.id) is None]
        removing = [role for role in removes if member.get_role(role.id) is not None]
        if adding:
            await member.add_roles(*adding, reason=reason)
        if removing:
            await member.remove_roles(*removing, reason=reason)
        due = time.time() + seconds
        for role, undo in [*((r, "remove") for r in adds), *((r, "add") for r in removes)]:
            await self.bot.timers.schedule(
                "temprole", due, {"guild_id": ctx.guild.id, "user_id": member.id, "role_id": role.id, "undo": undo},
                guild_id=ctx.guild.id, key=f"{ctx.guild.id}:{member.id}:{role.id}",
            )
        parts = []
        if adds:
            parts.append("gave " + ", ".join(r.mention for r in adds))
        if removes:
            parts.append("took " + ", ".join(r.mention for r in removes))
        await ctx.ok(f"{member.mention}: {' and '.join(parts)} for {humanize(seconds)}.")

    @temprole.command(name="add", description="Give a role for a set time, then take it back")
    @requires("manage_roles")
    @app_commands.describe(member="Who gets the role", duration="How long, such as 2h or 3 days", role="The role to give")
    async def temprole_add(self, ctx: commands.Context, member: discord.Member, duration: str, *, role: str) -> None:
        # Staff type the time before or after the role name, so a text command reads the two as one string.
        await self._temp_add(ctx, member, f"{duration} {role}")

    @temprole_add.autocomplete("role")
    async def _role_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        needle = current.casefold()
        rows = [r for r in reversed(guild.roles) if not r.is_default() and not r.managed and needle in r.name.casefold()]
        return [app_commands.Choice(name=r.name[:100], value=str(r.id)) for r in rows[:25]]

    @temprole.command(name="custom", aliases=["c"], description="Add and remove roles for a set time, such as 3h +muted -member")
    @requires("manage_roles")
    @app_commands.describe(member="Whose roles change", duration="How long, such as 3h", roles="Roles with + to add and - to remove, such as +muted -member")
    async def temprole_custom(self, ctx: commands.Context, member: discord.Member, duration: str, *, roles: str) -> None:
        seconds = parse_duration(duration)
        if seconds is None:
            await ctx.fail(f"`{duration[:60]}` isn't a length of time. Try something like 3h or 2 days.")
            return
        adds, removes, flips, missing = parse_signed(ctx.guild, roles)
        if missing:
            await ctx.fail(missing_roles_sentence(missing))
            return
        adds = list(adds)
        removes = list(removes)
        for role in flips:
            (removes if member.get_role(role.id) else adds).append(role)
        if not adds and not removes:
            await ctx.fail("Name at least one role, such as `+muted -member`.")
            return
        await self._temp_change(ctx, member, seconds, adds, removes)

    @temprole.command(name="list", description="Show temporary roles still running")
    @requires("manage_roles")
    async def temprole_list(self, ctx: commands.Context) -> None:
        rows = await self.bot.timers.list_kind("temprole", ctx.guild.id)
        lines = []
        for row in rows:
            payload = row["payload"]
            verb = "loses" if payload.get("undo") == "remove" else "gets back"
            lines.append(f"<@{payload['user_id']}> {verb} <@&{payload['role_id']}> <t:{int(row['due_at'])}:R>")
        await send_pages(ctx, "Temporary roles", lines, empty="No temporary roles are running.")

    async def _temp_due(self, payload: dict, row: dict) -> None:
        """A change the bot can't undo right now stays due. A handler that raises keeps its row and tries again later, and a temporary role must not become a lasting one because Manage Roles was off for a minute."""
        guild = self.bot.get_guild(payload["guild_id"])
        member = await find_member(guild, payload["user_id"]) if guild else None
        role = guild.get_role(payload["role_id"]) if guild else None
        if member is None or role is None:
            return
        giving_back = payload.get("undo") == "add"
        if (member.get_role(role.id) is not None) == giving_back:
            return
        if role >= guild.me.top_role:
            self._tell_temp_blocked(guild, member, role, "it sits at or above the bot's highest role. Move the bot's role above it")
            raise TemporaryRoleBlocked(f"Temporary role {role.id} in {guild.id} sits above the bot")
        try:
            if giving_back:
                await member.add_roles(role, reason="Temporary role change ended")
            else:
                await member.remove_roles(role, reason="Temporary role ended")
        except discord.NotFound:
            log.info("A temporary role's member or role in %s is gone", guild.id)
        except discord.Forbidden:
            self._tell_temp_blocked(guild, member, role, "Discord refused. Check the bot's Manage Roles permission and where its role sits")
            raise

    def _tell_temp_blocked(self, guild: Any, member: Any, role: Any, why: str) -> None:
        tell_staff(self, guild, f"temp:{role.id}", "Temporary role is overdue", f"The time ended for {role.mention} on {member.mention}, and the bot can't change it back: {why}. It keeps trying.")

    # ---- /rank ----

    @commands.hybrid_group(name="rank", description="Give yourself roles from the server's list")
    @commands.guild_only()
    @commands.cooldown(RANK_USES, RANK_SECONDS, commands.BucketType.member)
    async def rank(self, ctx: commands.Context, *, role: Optional[str] = None) -> None:
        if role:
            await self._apply_rank(ctx, role)
        else:
            await self._show_ranks(ctx)

    async def _show_ranks(self, ctx: commands.Context) -> None:
        ranks = [ctx.guild.get_role(role_id) for role_id in self.cfg(ctx.guild.id)["ranks"]]
        counts = await rank_counts(ctx.guild, {role.id for role in ranks if role is not None})
        lines = [f"{role.mention} {counts[role.id]:,} members" for role in ranks if role is not None]
        await send_pages(ctx, "Ranks", lines, empty="This server has no ranks yet.")

    async def _apply_rank(self, ctx: commands.Context, text: str) -> None:
        adds, removes, flips, missing = parse_signed(ctx.guild, text)
        if missing:
            await ctx.fail(missing_roles_sentence(missing))
            return
        allowed = set(self.cfg(ctx.guild.id)["ranks"])
        member = ctx.author
        adds = list(adds)
        removes = list(removes)
        for role in flips:
            (removes if member.get_role(role.id) else adds).append(role)
        chosen = [*adds, *removes]
        if not chosen:
            await ctx.fail("Name at least one rank.")
            return
        for role in chosen:
            if role.id not in allowed:
                await ctx.fail(f"{role.mention} isn't on the rank list. `/rank allranks` shows the list.")
                return
            refusal = self._role_refusal(ctx, role, as_author=False)
            if refusal:
                await ctx.fail(refusal)
                return
        for role in adds:
            if not self._safe(ctx.guild, role):
                await ctx.fail(self._unsafe_note(role))
                return
        if adds:
            await member.add_roles(*adds, reason="Rank")
        if removes:
            await member.remove_roles(*removes, reason="Rank")
        parts = []
        if adds:
            parts.append("You now have " + ", ".join(r.mention for r in adds))
        if removes:
            parts.append(("you no longer have " if adds else "You no longer have ") + ", ".join(r.mention for r in removes))
        await ctx.ok(" and ".join(parts) + ".")

    async def _save_ranks(self, ctx: commands.Context, action: str, ranks: list[int]) -> None:
        await self.bot.settings.save(ctx.guild.id, self.key, {"ranks": ranks}, actor=self.bot.actor(ctx.author), action=action)

    async def _add_ranks(self, ctx: commands.Context, text: str) -> None:
        found, missing = find_roles(ctx.guild, text)
        if missing:
            await ctx.fail(missing_roles_sentence(missing))
            return
        if not found:
            await ctx.fail("Name at least one role.")
            return
        for role in found:
            refusal = self._role_refusal(ctx, role)
            if refusal:
                await ctx.fail(refusal)
                return
            if not self._safe(ctx.guild, role):
                await ctx.fail(self._unsafe_note(role))
                return
        current = self.cfg(ctx.guild.id)["ranks"]
        await self._save_ranks(ctx, "rank.add", [*current, *(r.id for r in found if r.id not in current)])
        await ctx.ok(f"Members can now give themselves {', '.join(r.mention for r in found)}.")

    async def _remove_ranks(self, ctx: commands.Context, text: str) -> None:
        found, missing = find_roles(ctx.guild, text)
        if missing:
            await ctx.fail(missing_roles_sentence(missing))
            return
        if not found:
            await ctx.fail("Name at least one role.")
            return
        gone = {r.id for r in found}
        await self._save_ranks(ctx, "rank.remove", [r for r in self.cfg(ctx.guild.id)["ranks"] if r not in gone])
        await ctx.ok(f"{', '.join(r.mention for r in found)} can no longer be self-assigned.")

    @rank.command(name="allranks", description="List the roles members can give themselves")
    @commands.cooldown(RANK_USES, RANK_SECONDS, commands.BucketType.member)
    async def rank_allranks(self, ctx: commands.Context) -> None:
        await self._show_ranks(ctx)

    @rank.command(name="addrank", description="Let members give themselves these roles")
    @requires("manage_roles")
    @app_commands.describe(roles="The roles, as mentions, IDs or names")
    async def rank_addrank(self, ctx: commands.Context, *, roles: str) -> None:
        await self._add_ranks(ctx, roles)

    @rank.command(name="removerank", description="Stop letting members give themselves these roles")
    @requires("manage_roles")
    @app_commands.describe(roles="The roles, as mentions, IDs or names")
    async def rank_removerank(self, ctx: commands.Context, *, roles: str) -> None:
        await self._remove_ranks(ctx, roles)

    @rank.command(name="custom", aliases=["c"], description="Add or remove your own ranks, such as +pro -newbie")
    @commands.cooldown(RANK_USES, RANK_SECONDS, commands.BucketType.member)
    @app_commands.describe(roles="Ranks with + to add and - to remove, such as +pro -newbie")
    async def rank_custom(self, ctx: commands.Context, *, roles: str) -> None:
        await self._apply_rank(ctx, roles)

    # ---- the short text forms ----

    @commands.command(name="allroles", extras={"rules_of": "role allroles"}, description="List every role in the server")
    @commands.guild_only()
    @requires("manage_roles")
    async def allroles(self, ctx: commands.Context) -> None:
        await self._show_all_roles(ctx)

    @commands.command(name="rolediagnose", extras={"rules_of": "role diagnose"}, description="Check why the bot can or can't hand out a role")
    @commands.guild_only()
    async def rolediagnose(self, ctx: commands.Context, *, role: Optional[discord.Role] = None) -> None:
        await self._diagnose(ctx, role)

    @commands.command(name="ranks", extras={"rules_of": "rank"}, description="List the roles members can give themselves")
    @commands.guild_only()
    @commands.cooldown(RANK_USES, RANK_SECONDS, commands.BucketType.member)
    async def ranks(self, ctx: commands.Context) -> None:
        await self._show_ranks(ctx)

    @commands.command(name="addrank", extras={"rules_of": "rank addrank"}, description="Let members give themselves these roles")
    @commands.guild_only()
    @requires("manage_roles")
    async def addrank(self, ctx: commands.Context, *, roles: str) -> None:
        await self._add_ranks(ctx, roles)

    @commands.command(name="removerank", extras={"rules_of": "rank removerank"}, description="Stop letting members give themselves these roles")
    @commands.guild_only()
    @requires("manage_roles")
    async def removerank(self, ctx: commands.Context, *, roles: str) -> None:
        await self._remove_ranks(ctx, roles)
