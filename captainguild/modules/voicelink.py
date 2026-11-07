"""Roles held while in a voice channel.

The table records which roles this feature gave and to whom. Only those come off on leaving, so someone who already held a
role keeps it. Each member is checked against where they are now, which leaves nothing to lose if events bunch up or arrive
while the bot is down: the next check, or the one at startup, sets things right. A role moved above the bot can't come off, so its
record stays until the role is back in reach, and staff hear about it once.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from ..core.members import find_member
from ..core.module import Module
from ..core.perms import DANGEROUS_PERMISSIONS
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import role_is_safe
from ..core.validate import Field, Invalid, Schema, list_of, snowflake, snowflakes

log = logging.getLogger(__name__)

MAX_LINKS = 500
# A restore is still finishing when it tells modules, and members' voice states don't change because of it, so the check can wait.
RESTORE_SETTLE = 30.0
# The gateway's copy of a member still shows a role for a moment after the bot takes it, and it has caught up well before this.
GATEWAY_LAG = 10.0
REASON = "Voice link"


def _link(value: Any) -> dict:
    if not isinstance(value, dict):
        raise Invalid("Each voice link needs a channel and roles.")
    roles = snowflakes(25)(value.get("roles") or [])
    if not roles:
        raise Invalid("Each voice link needs at least one role.")
    return {"channel_id": snowflake(value.get("channel_id")), "roles": roles}


def _links(value: Any) -> list[dict]:
    rows = list_of(_link, MAX_LINKS, unique=False)(value)
    channels = [row["channel_id"] for row in rows]
    if len(set(channels)) != len(channels):
        raise Invalid("A channel can have one voice link. Add more roles to the one it has.")
    return rows


class VoiceLink(Module):
    key = "voicelink"
    category = "Roles"
    schema = Schema({
        "links": Field([], _links),
    })
    migrations = [
        """
        CREATE TABLE voicelink_held (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id, role_id)
        );
        """,
    ]

    def roles_given(self, settings: dict) -> dict[str, set[int]]:
        return {f"links:{row['channel_id']}": set(row["roles"]) for row in settings["links"]}

    def transient_roles(self, guild_id: int) -> set[int]:
        return self.linked_roles(guild_id)

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._running: dict[tuple[int, int], asyncio.Task] = {}
        self._again: set[tuple[int, int]] = set()
        self._just_taken: dict[tuple[int, int, int], float] = {}
        self._startup: asyncio.Task | None = None
        self._after_restores: set[asyncio.Task] = set()
        self._out_of_reach: set[tuple[int, int]] = set()  # roles staff were told about, which also get a check once they're back in reach
        # Servers that couldn't be checked during an outage. They get checked when they come back.
        self._away: set[int] = set()

    async def on_load(self) -> None:
        self.bot.settings.listen(self.key, self._links_changed)

    async def cog_unload(self) -> None:
        tasks = [*self._running.values(), *self._after_restores]
        if self._startup:
            tasks.append(self._startup)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def settle(self) -> None:
        """Wait until every member's check has finished. A finished check leaves the map in its callback, so each round yields first and waits
        only on checks still running."""
        while True:
            await asyncio.sleep(0)
            running = [task for task in self._running.values() if not task.done()]
            if not running:
                return
            await asyncio.wait(running)

    def _touch(self, guild_id: int, user_id: int) -> None:
        """Check this member soon. A member hopping between channels gets one check at a time and one more after it, however fast they move."""
        key = (guild_id, user_id)
        if key in self._running:
            self._again.add(key)
            return
        self._running[key] = asyncio.create_task(self._run_checks(key), name=f"voicelink-{user_id}")

    async def _run_checks(self, key: tuple[int, int]) -> None:
        try:
            while True:
                self._again.discard(key)
                try:
                    await self._sync_member(*key)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Voice link check for %s failed", key[1])
                if key not in self._again:
                    return
        finally:
            self._running.pop(key, None)
            self._again.discard(key)

    def linked_roles(self, guild_id: int) -> set[int]:
        """Every role this server links to a voice channel."""
        return {role_id for row in self.cfg(guild_id)["links"] for role_id in row["roles"]}

    def _wanted(self, guild_id: int, channel_id: int | None) -> set[int]:
        if channel_id is None:
            return set()
        for row in self.cfg(guild_id)["links"]:
            if row["channel_id"] == channel_id:
                return set(row["roles"])
        return set()

    def _usable(self, guild: Any, role_id: int) -> Any:
        role = guild.get_role(role_id)
        if role is None or role.is_default() or role.managed or role >= guild.me.top_role:
            return None
        if not role_is_safe([name for name, on in role.permissions if on], self.bot.settings.get(guild.id, COMMANDS_MODULE)):
            return None
        return role

    def _tell_staff_once(self, guild: Any, role: Any) -> None:
        key = (guild.id, role.id)
        if key in self._out_of_reach:
            return
        self._out_of_reach.add(key)
        log.warning("Voice link role %s in %s sits at or above the bot's top role", role.id, guild.id)
        logs = self.bot.module("logging")
        if logs is not None:
            logs.staff_notice(guild, "A voice link role is out of reach", f"{role.mention} sits at or above the bot's top role, so the bot can't give it or take it off. Members who left voice keep it until you move the bot's role above it.")

    async def _sync_member(self, guild_id: int, user_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.unavailable:
            return
        rows = await self.db.all("SELECT role_id FROM voicelink_held WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        tracked = {row["role_id"] for row in rows}
        try:
            member = await find_member(guild, user_id)
        except (discord.HTTPException, aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:
            log.warning("Voice link couldn't look up %s: %s", user_id, error)  # the record stays, since this says nothing about whether they left
            return
        if member is None:
            if tracked:
                await self.db.run("DELETE FROM voicelink_held WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            return
        voice = member.voice
        channel = voice.channel if voice is not None else None
        wanted = self._wanted(guild_id, channel.id if channel is not None else None) if not member.bot else set()
        for role_id in tracked - wanted:
            role = guild.get_role(role_id)
            if role is not None and role >= guild.me.top_role:
                self._tell_staff_once(guild, role)  # the record stays, so it still comes off once the role is back in reach
                continue
            if role is not None:
                try:
                    await member.remove_roles(role, reason=REASON)
                except discord.NotFound:
                    pass
                except discord.HTTPException as error:
                    log.warning("Voice link couldn't take role %s from %s: %s", role_id, user_id, error)
                    continue
                if len(self._just_taken) > 1000:
                    self._just_taken.clear()
                self._just_taken[(guild_id, user_id, role_id)] = time.monotonic()
            await self.db.run("DELETE FROM voicelink_held WHERE guild_id = ? AND user_id = ? AND role_id = ?", (guild_id, user_id, role_id))
        holding = {role.id for role in member.roles}
        for role_id in wanted - tracked:
            taken_at = self._just_taken.pop((guild_id, user_id, role_id), None)
            stale_copy = taken_at is not None and time.monotonic() - taken_at < GATEWAY_LAG
            if role_id in holding and not stale_copy:  # they hold it some other way, so leaving the channel won't take it
                continue
            role = self._usable(guild, role_id)
            if role is None:
                above = guild.get_role(role_id)
                if above is not None and above >= guild.me.top_role:
                    self._tell_staff_once(guild, above)
                continue
            # The record goes first. A stop right after the role lands would otherwise leave it on for good, read as one they hold some other way. A record with no role behind it costs one harmless removal.
            await self.db.run("INSERT OR IGNORE INTO voicelink_held (guild_id, user_id, role_id) VALUES (?, ?, ?)", (guild_id, user_id, role_id))
            try:
                await member.add_roles(role, reason=REASON)
            except discord.HTTPException as error:
                log.warning("Voice link couldn't give role %s to %s: %s", role_id, user_id, error)
                await self.db.run("DELETE FROM voicelink_held WHERE guild_id = ? AND user_id = ? AND role_id = ?", (guild_id, user_id, role_id))

    async def _check_guild(self, guild_id: int) -> None:
        """Everyone who could be out of step: people in linked channels now, and people the feature gave a role to."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        if guild.unavailable:
            self._away.add(guild_id)
            return
        rows = await self.db.all("SELECT DISTINCT user_id FROM voicelink_held WHERE guild_id = ?", (guild_id,))
        people = {row["user_id"] for row in rows}
        for link in self.cfg(guild_id)["links"]:
            channel = guild.get_channel(link["channel_id"])
            # A text channel's members are everyone who can read it, which is nobody this feature cares about.
            if channel is not None and channel.type in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                people.update(member.id for member in channel.members)
                people.update(getattr(channel, "voice_states", {}))  # the member list can still be filling, and someone missing from it is still in the channel
        for user_id in people:
            self._touch(guild_id, user_id)

    async def _links_changed(self, guild_id: int, settings: dict) -> None:
        await self._check_guild(guild_id)

    async def on_guild_restored(self, guild_id: int) -> None:
        async def later() -> None:
            await asyncio.sleep(RESTORE_SETTLE)
            await self._check_guild(guild_id)

        task = asyncio.create_task(later(), name=f"voicelink-restored-{guild_id}")
        self._after_restores.add(task)
        task.add_done_callback(self._after_restores.discard)

    async def _startup_checks(self) -> None:
        rows = await self.db.all("SELECT DISTINCT guild_id FROM voicelink_held")
        for guild_id in {row["guild_id"] for row in rows} | set(self.bot.settings.guilds_with(self.key)):
            await self._check_guild(guild_id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._startup is None or self._startup.done():
            self._startup = asyncio.create_task(self._startup_checks(), name="voicelink-startup")

    @commands.Cog.listener()
    async def on_guild_unavailable(self, guild: Any) -> None:
        self._away.add(guild.id)

    @commands.Cog.listener()
    async def on_guild_available(self, guild: Any) -> None:
        if guild.id in self._away:
            self._away.discard(guild.id)
            await self._check_guild(guild.id)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: Any, after: Any) -> None:
        """A role back in reach comes off the people who left voice while it wasn't. The bot's own role moving up does the same."""
        guild = after.guild
        back = {key for key in self._out_of_reach if key[0] == guild.id and ((role := guild.get_role(key[1])) is None or role < guild.me.top_role)}
        if back:
            self._out_of_reach -= back
            await self._check_guild(guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        if before.channel == after.channel or not self.cfg(member.guild.id)["links"]:  # saving the last link away already checked everyone it held a role for
            return
        self._touch(member.guild.id, member.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: Any) -> None:
        await self.db.run("DELETE FROM voicelink_held WHERE guild_id = ? AND user_id = ?", (member.guild.id, member.id))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        links = self.cfg(channel.guild.id)["links"]
        kept = [row for row in links if row["channel_id"] != channel.id]
        if len(kept) != len(links):
            await self.bot.settings.save(channel.guild.id, self.key, {"links": kept}, action="voicelink.channel_deleted")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: Any) -> None:
        guild_id = role.guild.id
        links = self.cfg(guild_id)["links"]
        kept = []
        changed = False
        for row in links:
            roles = [r for r in row["roles"] if r != role.id]
            changed = changed or len(roles) != len(row["roles"])
            if roles:
                kept.append({**row, "roles": roles})
        if changed:
            await self.bot.settings.save(guild_id, self.key, {"links": kept}, action="voicelink.role_deleted")
        await self.db.run("DELETE FROM voicelink_held WHERE guild_id = ? AND role_id = ?", (guild_id, role.id))

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/voicelink/rules", self._api_rules)

    async def _api_rules(self, req: Any) -> dict:
        return {"elevated_roles": bool(req.settings.get(req.guild_id, COMMANDS_MODULE)["elevated_roles"]), "dangerous": list(DANGEROUS_PERMISSIONS)}
