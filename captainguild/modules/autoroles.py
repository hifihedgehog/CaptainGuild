"""Roles given on join, roles put back when someone returns, and roles given after a delay."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ..core.durations import humanize
from ..core.members import find_member, whole_member_list
from ..core.module import Module, requires
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import role_is_safe
from ..core.validate import Field, Invalid, Schema, boolean, integer, snowflake, snowflakes
from .roles import find_role, find_roles, missing_roles_sentence, split_time, tell_staff

log = logging.getLogger(__name__)

DAY = 86400
MAX_DELAY = 365 * DAY
MAX_TIMED = 25
RETRY_SECONDS = 60
# How long the scan waits to try again when the member list wouldn't load.
SCAN_RETRY_SECONDS = 300
# How often a saved role the bot can't hand out yet is looked at again, for someone who's back in the server.
STICKY_RECHECK_SECONDS = 3600
NOTICE_TITLE = "Autoroles need a look"


def timed_rows(value: Any) -> list[dict]:
    """Each message names its row, since a page can hold 25. One role can only have one delay, because each member has one timer per role."""
    if not isinstance(value, list):
        raise Invalid("Must be a list.")
    if len(value) > MAX_TIMED:
        raise Invalid(f"Can hold at most {MAX_TIMED} entries.")
    rows: list[dict] = []
    for number, row in enumerate(value, 1):
        if not isinstance(row, dict):
            raise Invalid(f"Timed role {number} needs a role and a delay.")
        if not row.get("role_id"):
            raise Invalid(f"Timed role {number} needs a role.")
        try:
            role_id = snowflake(row.get("role_id"))
        except Invalid as error:
            raise Invalid(f"Timed role {number}: {error}") from error
        try:
            seconds = integer(60, MAX_DELAY)(row.get("seconds"))
        except Invalid as error:
            raise Invalid(f"Timed role {number} needs a delay from one minute to one year.") from error
        if any(other["role_id"] == role_id for other in rows):
            raise Invalid(f"Timed role {number} is already on this list. A role can have one delay.")
        rows.append({"role_id": role_id, "seconds": seconds})
    return rows


class Autoroles(Module):
    key = "autoroles"
    category = "Roles"
    role_timers = ("timedrole", "autorole_retry", "autorole_sticky")
    schema = Schema({
        "roles": Field([], snowflakes(25)),
        "reassign": Field(False, boolean),
        "reassign_days": Field(30, integer(1, 365)),
        "blacklist": Field([], snowflakes()),
        "timed": Field([], timed_rows),
        "ignore_bots": Field(False, boolean),
    })
    migrations = [
        """
        CREATE TABLE sticky_roles (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_ids TEXT NOT NULL,
            left_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """,
        # Who's still on the rules screen, so someone who passes it while the bot is away gets their roles at the next start.
        """
        CREATE TABLE autorole_waiting (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        # Servers whose scan has finished since this start. Until then the saved mark still has to say when the bot last knew who had joined.
        self._caught_up: set[int] = set()

    async def on_load(self) -> None:
        self.bot.timers.handler("timedrole", self._timed_role_due)
        self.bot.timers.handler("autorole_retry", self._retry_due)
        self.bot.timers.handler("autorole_sticky", self._sticky_recheck_due)
        self.bot.timers.handler("autorole_scan", self._scan_due)
        self.bot.settings.listen(self.key, self._settings_changed)

    def transient_roles(self, guild_id: int) -> set[int]:
        """A timed role comes from its timer, which starts again when someone returns. A role with a temporary timer is left out per person by `_passing_roles` instead, since one member's temporary role can be another's lasting one."""
        return {row["role_id"] for row in self.cfg(guild_id)["timed"]}

    def role_refusal(self, guild: Any, role: Any) -> str | None:
        return self._handing_refusal(guild, role, role.name)

    def _handing_refusal(self, guild: Any, role: Any, shown: str) -> str | None:
        if role.is_default():
            return "Everyone always has @everyone, so it can't be handed out."
        if role.managed:
            return f"{shown} belongs to an integration, and Discord doesn't let anyone hand it out."
        if not role_is_safe([name for name, on in role.permissions if on], self.bot.settings.get(guild.id, COMMANDS_MODULE)):
            return f"{shown} holds permissions the bot won't hand out while elevated roles are off on the Commands page."
        return None

    def roles_given(self, settings: dict) -> dict[str, set[int]]:
        return {"roles": set(settings["roles"]), "timed": {row["role_id"] for row in settings["timed"]}}

    def _in_use(self, cfg: dict) -> bool:
        return bool(cfg["roles"] or cfg["timed"] or cfg["reassign"])

    async def _handled_until(self, guild_id: int, moment: float) -> None:
        """Everyone who joined before this moment has been dealt with. What the next start looks for is whoever joined after it."""
        await self.db.run("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (f"autoroles_handled:{guild_id}", str(moment)))

    async def _live_event(self, guild_id: int, moment: float) -> None:
        """A live join or save moves the mark only once the scan is done. Discord sends joins before the bot's lists are ready, and moving it then would hide everyone who joined during the downtime."""
        if guild_id in self._caught_up:
            await self._handled_until(guild_id, moment)

    async def _settings_changed(self, guild_id: int, settings: dict) -> None:
        if guild_id in self._caught_up:
            await self._handled_until(guild_id, time.time())  # a new setting applies to people from now on
        else:
            await self.db.run("INSERT OR IGNORE INTO kv (key, value) VALUES (?, ?)", (f"autoroles_handled:{guild_id}", str(time.time())))
        # Someone who took a role off the list expects it to stop going out, up to a year of waiting timers included.
        kept = [row["role_id"] for row in settings["timed"]]
        marks = ",".join("?" * len(kept))
        await self.db.run(f"DELETE FROM timers WHERE kind = 'timedrole' AND guild_id = ? AND json_extract(payload, '$.role_id') NOT IN ({marks})", (guild_id, *kept))

    # ---- events ----

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild_id = member.guild.id
        cfg = self.cfg(guild_id)
        await self.db.run("DELETE FROM autorole_waiting WHERE guild_id = ? AND user_id = ?", (guild_id, member.id))
        await self.db.run("DELETE FROM timers WHERE kind = 'timedrole' AND guild_id = ? AND key LIKE ?", (guild_id, f"{guild_id}:{member.id}:%"))  # coming back starts them again
        # Rows past their time go here, since nothing else clears the row of someone who never returns.
        await self.db.run("DELETE FROM sticky_roles WHERE guild_id = ? AND left_at < ?", (guild_id, time.time() - cfg["reassign_days"] * DAY))
        if not cfg["reassign"]:
            return
        passing = await self._passing_roles(member)
        kept = {r.id for r in member.roles if not r.is_default() and not r.managed and r.id not in cfg["blacklist"] and r.id not in passing}
        # A role from their last visit that the bot couldn't hand out then is still theirs to get.
        earlier = await self.db.one("SELECT role_ids FROM sticky_roles WHERE guild_id = ? AND user_id = ?", (guild_id, member.id))
        if earlier is not None:
            kept |= set(json.loads(earlier["role_ids"])) - passing
        if not kept:
            return
        await self.db.run(
            "INSERT INTO sticky_roles (guild_id, user_id, role_ids, left_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET role_ids = excluded.role_ids, left_at = excluded.left_at",
            (member.guild.id, member.id, json.dumps(sorted(kept)), time.time()),
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        # A member still on the rules screen can't take roles, so they get theirs when they accept.
        if member.pending:
            if self._in_use(self.cfg(member.guild.id)):
                await self.db.run("INSERT OR IGNORE INTO autorole_waiting (guild_id, user_id) VALUES (?, ?)", (member.guild.id, member.id))
                await self._live_event(member.guild.id, time.time())
            return
        await self.greet_with_roles(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.pending and not after.pending:
            await self.db.run("DELETE FROM autorole_waiting WHERE guild_id = ? AND user_id = ?", (after.guild.id, after.id))
            await self.greet_with_roles(after)

    @commands.Cog.listener()
    async def on_guild_available(self, guild: discord.Guild) -> None:
        """Comes at every start and whenever a server is back from an outage. Nothing tells the bot who joined or passed the rules screen while it was away, so it looks."""
        await self._scan(guild)

    @commands.Cog.listener()
    async def on_guild_unavailable(self, guild: discord.Guild) -> None:
        self._caught_up.discard(guild.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        self._caught_up.add(guild.id)  # nobody here was ever waiting on this bot

    async def _scan_due(self, payload: dict, row: dict) -> None:
        guild = self.bot.get_guild(payload["guild_id"])
        if guild is not None:
            await self._scan(guild)

    async def _scan(self, guild: Any) -> None:
        cfg = self.cfg(guild.id)
        if not self._in_use(cfg):
            self._caught_up.add(guild.id)
            return
        since = await self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"autoroles_handled:{guild.id}",))
        if since is None:
            await self._handled_until(guild.id, time.time())
            self._caught_up.add(guild.id)
            return
        if not await whole_member_list(guild):
            # The mark hasn't moved, so nobody is lost. It looks again from the same moment.
            await self.bot.timers.schedule("autorole_scan", time.time() + SCAN_RETRY_SECONDS, {"guild_id": guild.id}, guild_id=guild.id, key=str(guild.id))
            return
        for row in await self.db.all("SELECT user_id FROM autorole_waiting WHERE guild_id = ?", (guild.id,)):
            member = guild.get_member(row["user_id"])
            if member is None:
                await self.db.run("DELETE FROM autorole_waiting WHERE guild_id = ? AND user_id = ?", (guild.id, row["user_id"]))
            elif not member.pending:
                await self.db.run("DELETE FROM autorole_waiting WHERE guild_id = ? AND user_id = ?", (guild.id, member.id))
                await self.greet_with_roles(member)
        for member in guild.members:
            if member.joined_at is None or member.joined_at.timestamp() <= float(since):
                continue
            if member.pending:
                await self.db.run("INSERT OR IGNORE INTO autorole_waiting (guild_id, user_id) VALUES (?, ?)", (guild.id, member.id))
            else:
                await self.greet_with_roles(member)
        # Written only here, so a crash partway through looks at everyone again at the next start.
        await self._handled_until(guild.id, time.time())
        self._caught_up.add(guild.id)
        await self.bot.timers.cancel_key("autorole_scan", str(guild.id))

    async def greet_with_roles(self, member: discord.Member) -> None:
        cfg = self.cfg(member.guild.id)
        if not self._in_use(cfg) or (member.bot and cfg["ignore_bots"]):
            return
        try:
            await self._give_roles(member, cfg)
        except (discord.HTTPException, aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:
            log.warning("Could not give roles to %s in %s, so it tries again: %s", member.id, member.guild.id, error)
            await self.bot.timers.schedule(
                "autorole_retry", time.time() + RETRY_SECONDS, {"guild_id": member.guild.id, "user_id": member.id},
                guild_id=member.guild.id, key=f"{member.guild.id}:{member.id}",
            )
        now = time.time()
        for row in cfg["timed"]:
            await self.bot.timers.schedule(
                "timedrole", now + row["seconds"], {"guild_id": member.guild.id, "user_id": member.id, "role_id": row["role_id"]},
                guild_id=member.guild.id, key=f"{member.guild.id}:{member.id}:{row['role_id']}",
            )
        await self._live_event(member.guild.id, now)

    async def _give_roles(self, member: discord.Member, cfg: dict, *, joining: bool = True) -> None:
        """The roles for joining and any put back. The record of what to put back goes only once Discord has made the change, so a failure can't lose it."""
        sticky = await self._sticky_roles(member, cfg)
        roles = self._assignable(member.guild, (set(cfg["roles"]) if joining else set()) | (sticky or set()))
        if roles:
            await member.add_roles(*roles, reason="Autoroles")
        if sticky is None:
            return
        given = {role.id for role in roles}
        waiting = [role_id for role_id in sorted(sticky - given) if self._can_come_back(member.guild, role_id)]
        guild_id, key = member.guild.id, f"{member.guild.id}:{member.id}"
        if waiting:
            # A role moved above the bot may come back down. The record stays until its time runs out, and the bot looks again.
            await self.db.run("UPDATE sticky_roles SET role_ids = ? WHERE guild_id = ? AND user_id = ?", (json.dumps(waiting), guild_id, member.id))
            await self.bot.timers.schedule("autorole_sticky", time.time() + STICKY_RECHECK_SECONDS, {"guild_id": guild_id, "user_id": member.id}, guild_id=guild_id, key=key)
        else:
            await self.db.run("DELETE FROM sticky_roles WHERE guild_id = ? AND user_id = ?", (guild_id, member.id))
            await self.bot.timers.cancel_key("autorole_sticky", key)

    def _can_come_back(self, guild: Any, role_id: int) -> bool:
        role = guild.get_role(role_id)
        return role is not None and not role.managed and not role.is_default()

    async def _sticky_recheck_due(self, payload: dict, row: dict) -> None:
        guild = self.bot.get_guild(payload["guild_id"])
        member = await find_member(guild, payload["user_id"]) if guild else None
        if member is not None:
            await self._give_roles(member, self.cfg(guild.id), joining=False)

    async def _retry_due(self, payload: dict, row: dict) -> None:
        guild = self.bot.get_guild(payload["guild_id"])
        member = await find_member(guild, payload["user_id"]) if guild else None
        if member is not None:
            await self._give_roles(member, self.cfg(guild.id))  # one that fails again raises, and the timer tries later

    async def _sticky_roles(self, member: discord.Member, cfg: dict) -> set[int] | None:
        """What to put back, or None if there's no record to clear."""
        if not cfg["reassign"]:
            return None
        row = await self.db.one("SELECT role_ids, left_at FROM sticky_roles WHERE guild_id = ? AND user_id = ?", (member.guild.id, member.id))
        if row is None:
            return None
        if time.time() - row["left_at"] > cfg["reassign_days"] * DAY:
            return set()
        transient = self.bot.transient_roles(member.guild.id)  # a record from before a role became one of these holds it too
        return {role_id for role_id in json.loads(row["role_ids"]) if role_id not in cfg["blacklist"] and role_id not in transient}

    async def _passing_roles(self, member: discord.Member) -> set[int]:
        """Roles only meant for a while. Putting them back on return would make them permanent, with nothing left to take them off: this member's temporary roles, and what every feature names, such as a mute role or a role held while in a voice channel. Moderation puts a mute still running back by itself."""
        rows = await self.db.all("SELECT key FROM timers WHERE kind IN ('temprole', 'rr_temp') AND key LIKE ?", (f"{member.guild.id}:{member.id}:%",))
        passing = {int(row["key"].rsplit(":", 1)[1]) for row in rows}
        passing |= self.bot.transient_roles(member.guild.id)
        return passing

    def _assignable(self, guild: discord.Guild, role_ids: set[int]) -> list[discord.Role]:
        """Roles that still exist, sit below the bot, and don't hand out power unless the server allowed that. Staff hear about any it skips, once an hour per role."""
        commands_settings = self.bot.settings.get(guild.id, COMMANDS_MODULE)
        roles = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None or role.is_default() or role.managed:
                continue
            if role >= guild.me.top_role:
                tell_staff(self, guild, f"above:{role_id}", NOTICE_TITLE, f"{role.mention} sits at or above the bot's highest role, so the bot skipped it for someone who joined or returned. Move the bot's role above it.")
                continue
            if not role_is_safe([name for name, on in role.permissions if on], commands_settings):
                tell_staff(self, guild, f"power:{role_id}", NOTICE_TITLE, f"{role.mention} holds permissions the bot won't hand out while elevated roles are off on the Commands page, so it skipped it for someone who joined or returned.")
                continue
            roles.append(role)
        return roles

    async def _timed_role_due(self, payload: dict, row: dict) -> None:
        guild = self.bot.get_guild(payload["guild_id"])
        member = await find_member(guild, payload["user_id"]) if guild else None
        if member is None or member.pending:  # someone back on the rules screen gets theirs when they accept
            return
        if all(row["role_id"] != payload["role_id"] for row in self.cfg(guild.id)["timed"]):
            return
        roles = self._assignable(guild, {payload["role_id"]})
        if roles:
            await member.add_roles(*roles, reason="Timed role")

    # ---- commands ----

    @commands.hybrid_group(name="autorole", aliases=["autoroles"], fallback="show", description="Show which roles people get when they join")
    @commands.guild_only()
    @requires("manage_roles")
    async def autorole(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)
        roles = ", ".join(f"<@&{r}>" for r in cfg["roles"]) or "none"
        await ctx.send(f"Roles on join: {roles}\nPut roles back on return: {'on' if cfg['reassign'] else 'off'}", allowed_mentions=discord.AllowedMentions.none())

    @autorole.command(name="add", description="Give this role to everyone who joins")
    @requires("manage_roles")
    async def autorole_add(self, ctx: commands.Context, role: discord.Role) -> None:
        refusal = self._refusal(ctx, role)
        if refusal:
            await ctx.fail(refusal)
            return
        roles = self.cfg(ctx.guild.id)["roles"]
        await self._save(ctx, "autorole.add", {"roles": [*roles, role.id]})
        await ctx.ok(f"{role.mention} is now given to everyone who joins.")

    @autorole.command(name="remove", description="Stop giving this role to people who join")
    @requires("manage_roles")
    async def autorole_remove(self, ctx: commands.Context, role: discord.Role) -> None:
        roles = self.cfg(ctx.guild.id)["roles"]
        await self._save(ctx, "autorole.remove", {"roles": [r for r in roles if r != role.id]})
        await ctx.ok(f"{role.mention} is no longer given on join.")

    @autorole.command(name="reassign", aliases=["readd"], description="Turn on or off putting roles back when someone leaves and returns")
    @requires("manage_roles")
    async def autorole_reassign(self, ctx: commands.Context) -> None:
        turned_on = not self.cfg(ctx.guild.id)["reassign"]
        await self._save(ctx, "autorole.reassign", {"reassign": turned_on})
        await ctx.ok(f"Putting roles back on return is now {'on' if turned_on else 'off'}.")

    async def _named_roles(self, ctx: commands.Context, role: Any, text: str | None) -> list | None:
        """The picked role and any more named in text. Typed, the first name lands on `role` when it's one word that matches."""
        found, missing = find_roles(ctx.guild, text or "")
        if missing:
            await ctx.fail(missing_roles_sentence(missing))
            return None
        found = [*([role] if role is not None else []), *(other for other in found if role is None or other.id != role.id)]
        if not found:
            await ctx.fail("Name at least one role.")
            return None
        return found

    @autorole.command(name="blacklist", aliases=["bl"], description="Never put these roles back when someone returns")
    @requires("manage_roles")
    @app_commands.describe(role="The role", more="More roles, by name or mention")
    async def autorole_blacklist(self, ctx: commands.Context, role: discord.Role | None = None, *, more: str | None = None) -> None:
        found = await self._named_roles(ctx, role, more)
        if found is None:
            return
        current = self.cfg(ctx.guild.id)["blacklist"]
        await self._save(ctx, "autorole.blacklist", {"blacklist": [*current, *(role.id for role in found if role.id not in current)]})
        await ctx.ok(f"{', '.join(role.mention for role in found)} won't be put back when someone returns.")

    @autorole.command(name="unblacklist", description="Allow these roles to be put back when someone returns")
    @requires("manage_roles")
    @app_commands.describe(role="The role", more="More roles, by name or mention")
    async def autorole_unblacklist(self, ctx: commands.Context, role: discord.Role | None = None, *, more: str | None = None) -> None:
        found = await self._named_roles(ctx, role, more)
        if found is None:
            return
        gone = {role.id for role in found}
        await self._save(ctx, "autorole.unblacklist", {"blacklist": [r for r in self.cfg(ctx.guild.id)["blacklist"] if r not in gone]})
        await ctx.ok(f"{', '.join(role.mention for role in found)} can be put back when someone returns.")

    @commands.hybrid_group(name="timedrole", aliases=["tr"], fallback="show", description="Show roles given some time after joining")
    @commands.guild_only()
    @requires("manage_roles")
    async def timedrole(self, ctx: commands.Context) -> None:
        rows = self.cfg(ctx.guild.id)["timed"]
        text = "\n".join(f"<@&{r['role_id']}> after {humanize(r['seconds'])}" for r in rows) or "No timed roles are set."
        await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())

    @timedrole.command(name="add", aliases=["+"], description="Give this role some time after someone joins")
    @requires("manage_roles")
    @app_commands.describe(role="The role to give", time="How long after joining, such as 2h")
    async def timedrole_add(self, ctx: commands.Context, role: discord.Role | None = None, *, time: str = "") -> None:
        # Staff type the time before or after the role name. A time first, or a name of several words, doesn't match a role, so it all arrives in `time`.
        seconds, rest = split_time(time.strip())
        if seconds is None:
            await ctx.fail("That needs a length of time, such as `2h` or `3 days`.")
            return
        if role is not None and rest:  # the first word of a longer name happened to be a role of its own
            rest = f"{role.name} {rest}"
        found = find_role(ctx.guild, rest) if rest else role
        if found is None:
            await ctx.fail(f"There's no role called `{rest[:60]}`." if rest else "That needs a role.")
            return
        refusal = self._refusal(ctx, found)
        if refusal:
            await ctx.fail(refusal)
            return
        rows = [r for r in self.cfg(ctx.guild.id)["timed"] if r["role_id"] != found.id]
        await self._save(ctx, "timedrole.add", {"timed": [*rows, {"role_id": found.id, "seconds": seconds}]})
        await ctx.ok(f"{found.mention} will be given {humanize(seconds)} after someone joins.")

    @timedrole.command(name="remove", aliases=["-"], description="Stop giving this timed role and cancel any waiting")
    @requires("manage_roles")
    async def timedrole_remove(self, ctx: commands.Context, role: discord.Role) -> None:
        rows = [r for r in self.cfg(ctx.guild.id)["timed"] if r["role_id"] != role.id]
        await self._save(ctx, "timedrole.remove", {"timed": rows})  # the save cancels any timers waiting on it
        await ctx.ok(f"{role.mention} is no longer a timed role.")

    def _refusal(self, ctx: commands.Context, role: Any) -> str | None:
        """Why this role can't go to everyone who joins. Someone who ranks below a role can't set it, which would be a way to give it to themselves."""
        guild = ctx.guild
        if role >= guild.me.top_role:
            return f"{role.mention} sits at or above the bot's highest role, so the bot can't hand it out. Move the bot's role above it first."
        if ctx.author.id != guild.owner_id and role >= ctx.author.top_role:
            return f"{role.mention} sits at or above your highest role, so you can't set it up to be handed out."
        return self._handing_refusal(guild, role, role.mention)

    async def _save(self, ctx: commands.Context, action: str, form: dict) -> None:
        await self.bot.settings.save(ctx.guild.id, self.key, form, actor=self.bot.actor(ctx.author), action=action)
