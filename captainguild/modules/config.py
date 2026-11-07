"""The bot's own settings as commands: prefixes, command rules, dashboard sign-in, /help, /ping and /about."""
from __future__ import annotations

import math
import re
import time
from typing import Any, Iterable

import discord
from discord import app_commands
from discord.ext import commands

from .. import __version__, brand
from ..core import access
from ..core.durations import humanize
from ..core.module import Module, requires
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import NEVER_OFF
from ..core.shortcuts import SHORTCUTS, TAG_FIRST_SHORTCUTS, named_command
from ..core.signin import LINK_SECONDS


HELP_ROOM = 4000  # an embed's description holds 4,096 characters
# What makes a command a moderation command, for `disable mod` and `enable mod`.
MODERATION_PERMISSIONS = {"ban_members", "kick_members", "moderate_members", "manage_messages", "manage_nicknames", "mute_members", "deafen_members", "move_members"}
# Settings fields that hold IDs of channels or roles under a name that doesn't say so, by module and field path.
CHANNEL_FIELDS = {("starboard", "blacklist")}
IGNORE_WORDS = ("all", "server", "channel")
SNOWFLAKE_FROM = 10 ** 15  # smaller numbers are counts, not IDs
ROLE_FIELDS = {("autoroles", "blacklist")}


def fit_list(items: list[str], room: int) -> str:
    """A comma-separated list that fits, saying how many more there are rather than cutting one in half."""
    shown: list[str] = []
    for index, item in enumerate(items):
        room -= len(item) + 2
        if room < 0:
            return f"{', '.join(shown)} and {len(items) - index} more"
        shown.append(item)
    return ", ".join(shown)


def _id_fields(schema: Any, stored: dict, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    """Every field in these settings with its path and value, following nested schemas."""
    for name, field in schema.fields.items():
        if hasattr(field, "fields"):
            yield from _id_fields(field, stored.get(name) or {}, (*path, name))
        else:
            yield (*path, name), stored.get(name)


class Config(Module):
    key = "config"
    category = "Config"

    # ---- prefix ----

    @commands.hybrid_group(name="prefix", fallback="list", description="Show the prefixes this server uses for tags and text commands")
    @commands.guild_only()
    async def prefix(self, ctx: commands.Context) -> None:
        prefixes = self.bot.settings.get(ctx.guild.id, "core")["prefixes"]
        listed = ", ".join(f"`{p}`" for p in prefixes) or "none, so only mentioning the bot works"
        await ctx.send(f"Prefixes: {listed}", allowed_mentions=discord.AllowedMentions.none())

    @prefix.command(name="add", description="Add a prefix")
    @requires("manage_guild")
    async def prefix_add(self, ctx: commands.Context, prefix: str) -> None:
        current = self.bot.settings.get(ctx.guild.id, "core")["prefixes"]
        if prefix in current:
            await ctx.fail("That prefix is already set.")
            return
        await self._save_core(ctx, "prefix.add", {"prefixes": [*current, prefix]})
        await ctx.ok(f"Added `{prefix}`.")

    @prefix.command(name="set", description="Make this the only prefix")
    @requires("manage_guild")
    async def prefix_set(self, ctx: commands.Context, prefix: str) -> None:
        await self._save_core(ctx, "prefix.set", {"prefixes": [prefix]})
        await ctx.ok(f"The prefix is now `{prefix}`.")

    @prefix.command(name="remove", description="Remove a prefix")
    @requires("manage_guild")
    async def prefix_remove(self, ctx: commands.Context, prefix: str) -> None:
        current = self.bot.settings.get(ctx.guild.id, "core")["prefixes"]
        if prefix not in current:
            await ctx.fail("That prefix isn't set.")
            return
        await self._save_core(ctx, "prefix.remove", {"prefixes": [p for p in current if p != prefix]})
        await ctx.ok(f"Removed `{prefix}`.")

    @prefix.command(name="clear", description="Remove every prefix. Mentioning the bot and slash commands keep working.")
    @requires("manage_guild")
    async def prefix_clear(self, ctx: commands.Context) -> None:
        await self._save_core(ctx, "prefix.clear", {"prefixes": []})
        await ctx.ok("Removed every prefix. Mentioning the bot and slash commands keep working.")

    # ---- /config ----

    @commands.hybrid_group(name="config", fallback="show", description="Show how commands are set up in this server")
    @commands.guild_only()
    @requires("manage_guild")
    async def config(self, ctx: commands.Context) -> None:
        cfg = self.bot.settings.get(ctx.guild.id, COMMANDS_MODULE)
        off = [name for name, row in cfg["overwrites"].items() if row.get("enabled") is False]
        manager = f"<@&{cfg['mod_role']}>" if cfg["mod_role"] else "none"
        ignored = ", ".join(f"<#{c}>" for c in cfg["channel_bl"]) or "none"
        await ctx.send(
            f"Bot manager role: {manager}\nIgnored channels: {ignored}\nTurned off: {', '.join(off) or 'nothing'}\n"
            f"Full settings: {self.bot.public_url}/dashboard/{ctx.guild.id}/commands",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _named_commands(self, words: str) -> tuple[list[tuple[Any, str | None, bool]], list[str]]:
        """What a typed list of command names means: each command with its rule name and whether one word named it, and the words that name nothing. The longest run of words that names a command wins, so `tag create` is one subcommand and `ping about` is two commands."""
        parts = [part.strip("\"'") for part in words.split()]
        found: list[tuple[Any, str | None, bool]] = []
        unknown: list[str] = []
        index = 0
        while index < len(parts):
            for count in (3, 2, 1):
                named = named_command(self.bot, " ".join(parts[index:index + count])) if index + count <= len(parts) else None
                if named is not None:
                    found.append((*named, count == 1))
                    index += count
                    break
            else:
                unknown.append(parts[index])
                index += 1
        return found, unknown

    def _keyword_commands(self, word: str) -> list[Any] | None:
        """The commands `all`, `mod` and `animal` stand for. None reaches a command that can't be turned off, and None means the word is none of them."""
        roots = [command for command in self.bot.commands if command.qualified_name not in NEVER_OFF]
        if word == "all":
            return roots
        if word == "animal":
            return [command for command in roots if command.qualified_name == "cute"]
        if word == "mod":
            return [command for command in roots if getattr(command.cog, "key", "") == "moderation" or self.bot.command_permissions(command.qualified_name) & MODERATION_PERMISSIONS]
        return None

    async def _flag(self, ctx: commands.Context, words: str, field: str, value: Any, action: str, done: str, *, keywords: tuple[str, ...] = ("all",)) -> None:
        """Set a rule on the commands named. A subcommand has its own rule only for being on or off, and the Commands page lists the same."""
        if words.split()[:1] == ["command"]:  # `disable command ping` is the longer spelling of `disable ping`
            words = words.split(None, 1)[1] if len(words.split()) > 1 else ""
        word = words.strip().lower()
        chosen = self._keyword_commands(word) if word in keywords else None
        leaves: dict[str, list[str]] = {}
        if chosen is not None:
            changed = {command.qualified_name: None for command in chosen}
            said = {"all": f"All {len(chosen)} commands", "mod": f"The {len(chosen)} moderation commands"}.get(word, "The animal commands")
            count = len(chosen)
        else:
            found, unknown = self._named_commands(words)
            if unknown:
                await ctx.fail(f"There is no command called `{unknown[0].replace('`', '')[:60]}`, so nothing changed.")
                return
            if not found:
                await ctx.fail("Name a command.")
                return
            changed = {}
            for command, leaf, one_word in found:
                root = (command.root_parent or command).qualified_name
                if field == "enabled" and leaf is not None:
                    leaves.setdefault(root, []).append(leaf)
                elif leaf is None or one_word:  # `info` is short for a subcommand, and the answer names the whole command this rule goes on
                    changed[root] = None
                else:
                    await ctx.fail(f"That rule covers a whole command, so name `{root}` rather than `{leaf}`. Nothing changed.")
                    return
            names = [*changed, *(leaf for rows in leaves.values() for leaf in rows)]
            said = ", ".join(f"`{name}`" for name in names)
            count = len(names)
        if field == "enabled" and value is False:
            for root in [*changed, *leaves]:  # a subcommand of one of these is part of the way back in
                if root in NEVER_OFF:
                    await ctx.fail(f"`{root}` can't be turned off, because it's the way back in.")
                    return
        cfg = self.bot.settings.get(ctx.guild.id, COMMANDS_MODULE)
        overwrites = dict(cfg["overwrites"])
        for root in changed:
            row = {**overwrites.get(root, {}), field: value}
            if field == "enabled" and value is True:  # turning a whole command on brings back its subcommands too
                row["disabled_subcommands"] = []
            overwrites[root] = row
        for root, names in leaves.items():
            row = dict(overwrites.get(root, {}))
            current = list(row.get("disabled_subcommands") or [])
            row["disabled_subcommands"] = [*current, *(name for name in names if name not in current)] if value is False else [name for name in current if name not in names]
            overwrites[root] = row
        await self.bot.settings.save(ctx.guild.id, COMMANDS_MODULE, {"overwrites": overwrites}, actor=self.bot.actor(ctx.author), action=action)
        await ctx.ok(done.format(said, be="is" if count == 1 else "are"))

    @config.command(name="diagnose", description="Show whether a command would run for someone in this channel, and why")
    @requires("manage_guild")
    @app_commands.describe(command="The command's name", member="Who to check. Defaults to you.")
    async def config_diagnose(self, ctx: commands.Context, command: str, member: discord.Member | None = None) -> None:
        named = named_command(self.bot, command)
        if named is None:
            await ctx.fail(f"There is no command called `{command.replace('`', '')[:60]}`.")
            return
        found = named[0]
        person = member or ctx.author
        decision = self.bot.command_decision(found, person, ctx.channel)
        answer = "would run" if decision.allowed else f"wouldn't run: {decision.reason or 'the rules here stop it'}"
        notes = [text_ for text_, on in (("its answer is hidden", decision.muted), ("the typed command gets deleted", decision.delete), ("its answer goes elsewhere", bool(decision.redirect or decision.restricted_channel))) if on]
        extra = f" Also, {', and '.join(notes)}." if decision.allowed and notes else ""
        await ctx.send(f"`{found.qualified_name}` {answer} for {person.mention} in {ctx.channel.mention}.{extra}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @config.command(name="disable", description="Turn commands off for everyone, even moderators")
    @requires("manage_guild")
    @app_commands.describe(command="Command names, or all, or mod for the moderation commands")
    async def config_disable(self, ctx: commands.Context, *, command: str) -> None:
        if command.strip().lower() == "list":
            await self._list_on_and_off(ctx)
            return
        await self._flag(ctx, command, "enabled", False, "command.disable", "{} {be} now off for everyone.", keywords=("all", "mod", "animal"))

    @config.command(name="enable", description="Turn commands back on")
    @requires("manage_guild")
    @app_commands.describe(command="Command names, or all, or mod for the moderation commands, or list to see what's on and off")
    async def config_enable(self, ctx: commands.Context, *, command: str) -> None:
        if command.strip().lower() == "list":
            await self._list_on_and_off(ctx)
            return
        await self._flag(ctx, command, "enabled", True, "command.enable", "{} {be} on again.", keywords=("all", "mod", "animal"))

    async def _list_on_and_off(self, ctx: commands.Context) -> None:
        overwrites = self.bot.settings.get(ctx.guild.id, COMMANDS_MODULE)["overwrites"]
        on, off = [], []
        for name in sorted(command.qualified_name for command in self.bot.commands if not command.extras.get("rules_of")):  # a typed twin is on or off with the command it follows
            (off if (overwrites.get(name) or {}).get("enabled") is False else on).append(f"`{name}`")
        off += [f"`{leaf}`" for row in overwrites.values() for leaf in row.get("disabled_subcommands") or []]
        embed = discord.Embed(title="Commands in this server", color=0x00A2F8)
        embed.add_field(name="On", value=fit_list(on, 1000) or "none", inline=False)
        embed.add_field(name="Off", value=fit_list(off, 1000) or "none", inline=False)
        await ctx.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @config.command(name="restrict", description="Send a command's output to the bot channel")
    @requires("manage_guild")
    @app_commands.describe(command="Command names, or all")
    async def config_restrict(self, ctx: commands.Context, *, command: str) -> None:
        await self._flag(ctx, command, "restricted", True, "command.restrict", "Output from {} now goes to the bot channel.")

    @config.command(name="unrestrict", description="Let a command answer where it was used")
    @requires("manage_guild")
    @app_commands.describe(command="Command names, or all")
    async def config_unrestrict(self, ctx: commands.Context, *, command: str) -> None:
        await self._flag(ctx, command, "restricted", False, "command.unrestrict", "Output from {} stays where the command is used again.")

    @config.command(name="modonly", description="Limit a command to moderators")
    @requires("manage_guild")
    @app_commands.describe(command="Command names, or all")
    async def config_modonly(self, ctx: commands.Context, *, command: str) -> None:
        await self._flag(ctx, command, "mod_only", True, "command.modonly", "{} {be} now limited to moderators.")

    @config.command(name="unmodonly", description="Open a command back up")
    @requires("manage_guild")
    @app_commands.describe(command="Command names, or all")
    async def config_unmodonly(self, ctx: commands.Context, *, command: str) -> None:
        await self._flag(ctx, command, "mod_only", False, "command.unmodonly", "{} {be} no longer limited to moderators.")

    def _channels_named(self, ctx: commands.Context, words: list[str]) -> tuple[list[Any], list[str]]:
        found, unknown = [], []
        for word in words:
            matched = re.fullmatch(r"<#(\d+)>|(\d{15,21})", word)
            channel = ctx.guild.get_channel(int(matched.group(1) or matched.group(2))) if matched else discord.utils.get(ctx.guild.channels, name=word.lstrip("#"))
            if channel is None or not access.can_view(ctx.author, channel):  # a hidden channel answers the same as one that isn't there
                unknown.append(word)
            else:
                found.append(channel)
        return found, unknown

    @staticmethod
    def _ignore_words(ctx: commands.Context, channel: Any, more: str) -> list[str]:
        """The words after `ignore`. Typed, the first one lands on `channel` when it matches a channel's name, so `ignore all` stays the keyword even with a channel called all. A mention or a slash pick is that channel."""
        words = more.split()
        if channel is not None:
            content = ctx.message.content if ctx.interaction is None else ""
            by_name = ctx.interaction is None and channel.name.lower() in IGNORE_WORDS and channel.mention not in content and str(channel.id) not in content
            words.insert(0, channel.name if by_name else channel.mention)
        if words[:1] == ["channel"]:  # `ignore channel #a` is the longer spelling of `ignore #a`
            words = words[1:]
        return words

    @config.command(name="ignore", description="Ignore commands in channels. Manage Server still works there.")
    @requires("manage_guild")
    @app_commands.describe(channel="The channel. Leave out for this one.", more="More channels, or all for every channel, or server to limit the whole bot to moderators")
    async def config_ignore(self, ctx: commands.Context, channel: discord.TextChannel | None = None, *, more: str = "") -> None:
        words = self._ignore_words(ctx, channel, more)
        current = self.bot.settings.get(ctx.guild.id, COMMANDS_MODULE)["channel_bl"]
        if words and words[0].lower() == "server":
            await self.bot.settings.save(ctx.guild.id, COMMANDS_MODULE, {"mod_only": True}, actor=self.bot.actor(ctx.author), action="command.ignore")
            await ctx.ok("Commands in this server are now limited to moderators.")
            return
        if words and words[0].lower() == "all":
            shown = [channel.id for channel in ctx.guild.text_channels if access.can_view(ctx.author, channel)]
            await self.bot.settings.save(ctx.guild.id, COMMANDS_MODULE, {"channel_bl": list(dict.fromkeys([*current, *shown]))}, actor=self.bot.actor(ctx.author), action="command.ignore")
            await ctx.ok(f"Commands are now ignored in {len(shown)} channels.")
            return
        targets, unknown = self._channels_named(ctx, words)
        if unknown:
            await ctx.fail(f"I can't find a channel called `{unknown[0].replace('`', '')[:60]}`, so nothing changed.")
            return
        targets = targets or [ctx.channel]
        await self.bot.settings.save(ctx.guild.id, COMMANDS_MODULE, {"channel_bl": list(dict.fromkeys([*current, *(target.id for target in targets)]))}, actor=self.bot.actor(ctx.author), action="command.ignore")
        await ctx.ok(f"Commands are now ignored in {', '.join(target.mention for target in targets)}.")

    @config.command(name="unignore", description="Stop ignoring commands in channels")
    @requires("manage_guild")
    @app_commands.describe(channel="The channel. Leave out for this one.", more="More channels, or all for every channel, or server to open the bot to everyone again")
    async def config_unignore(self, ctx: commands.Context, channel: discord.TextChannel | None = None, *, more: str = "") -> None:
        words = self._ignore_words(ctx, channel, more)
        current = self.bot.settings.get(ctx.guild.id, COMMANDS_MODULE)["channel_bl"]
        if words and words[0].lower() == "server":
            await self.bot.settings.save(ctx.guild.id, COMMANDS_MODULE, {"mod_only": False}, actor=self.bot.actor(ctx.author), action="command.unignore")
            await ctx.ok("Commands in this server are no longer limited to moderators.")
            return
        if words and words[0].lower() == "all":
            await self.bot.settings.save(ctx.guild.id, COMMANDS_MODULE, {"channel_bl": []}, actor=self.bot.actor(ctx.author), action="command.unignore")
            await ctx.ok(f"Commands work again in the {len(current)} channels that were ignored.")
            return
        targets, unknown = self._channels_named(ctx, words)
        if unknown:
            await ctx.fail(f"I can't find a channel called `{unknown[0].replace('`', '')[:60]}`, so nothing changed.")
            return
        targets = targets or [ctx.channel]
        gone = {target.id for target in targets}
        await self.bot.settings.save(ctx.guild.id, COMMANDS_MODULE, {"channel_bl": [c for c in current if c not in gone]}, actor=self.bot.actor(ctx.author), action="command.unignore")
        await ctx.ok(f"Commands work in {', '.join(target.mention for target in targets)} again.")

    @config.command(name="modrole", description="Set the bot manager role, whose members count as moderators to the bot")
    @requires("manage_guild")
    @app_commands.describe(role="The role. Leave it out to see the current one.")
    async def config_modrole(self, ctx: commands.Context, role: discord.Role | None = None) -> None:
        if role is None:
            current = self.bot.settings.get(ctx.guild.id, COMMANDS_MODULE)["mod_role"]
            await ctx.send(f"The bot manager role is <@&{current}>." if current else "No bot manager role is set.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        refusal = access.staff_role_refusal(ctx.author, role)
        if refusal:
            await ctx.fail(refusal)
            return
        await self.bot.settings.save(ctx.guild.id, COMMANDS_MODULE, {"mod_role": role.id}, actor=self.bot.actor(ctx.author), action="command.modrole")
        await ctx.ok(f"{role.mention} is now the bot manager role.")

    @config.command(name="clearmodrole", description="Remove the bot manager role")
    @requires("manage_guild")
    async def config_clearmodrole(self, ctx: commands.Context) -> None:
        refusal = access.staff_role_refusal(ctx.author, None)
        if refusal:
            await ctx.fail(refusal)
            return
        await self.bot.settings.save(ctx.guild.id, COMMANDS_MODULE, {"mod_role": None}, actor=self.bot.actor(ctx.author), action="command.modrole")
        await ctx.ok("The bot manager role is cleared.")

    @config.command(name="cleanconfig", description="Remove channels and roles that no longer exist from the settings")
    @requires("manage_guild")
    async def config_cleanconfig(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        cleaned: dict[str, int] = {}
        known: dict[int, bool] = {}
        for module, schema in self.bot.settings.schemas.items():
            stored = self.bot.settings.get(ctx.guild.id, module)
            form, dropped = await self._without_dead_ids(ctx.guild, module, schema, stored, known)
            if dropped:
                await self.bot.settings.save(ctx.guild.id, module, form, actor=self.bot.actor(ctx.author), action="config.cleanconfig")
                cleaned[module] = dropped
        if not cleaned:
            await ctx.ok("Every channel and role in the settings still exists.")
            return
        listed = ", ".join(f"{count} from {module}" for module, count in sorted(cleaned.items()))
        await ctx.ok(f"Removed deleted channels and roles: {listed}.")

    @staticmethod
    async def _channel_exists(guild: Any, channel_id: int, known: dict[int, bool]) -> bool:
        """Whether a channel is still there. An archived thread isn't in the cache, so Discord has the last word on a channel the cache doesn't hold, and only its plain No lets one go."""
        if guild.get_channel_or_thread(channel_id) is not None:
            return True
        if channel_id not in known:
            try:
                await guild.fetch_channel(channel_id)
                known[channel_id] = True
            except discord.NotFound:
                known[channel_id] = False
            except discord.HTTPException:
                known[channel_id] = True
        return known[channel_id]

    async def _without_dead_ids(self, guild: Any, module: str, schema: Any, stored: dict, known: dict[int, bool]) -> tuple[dict, int]:
        """A form holding only the fields that change, and how many IDs came out. A field that names neither channels nor roles stays as it is, because an ID there can be a member or a message."""
        form: dict = {}
        dropped = 0

        async def alive(item: int, is_channel: bool) -> bool:
            return await self._channel_exists(guild, item, known) if is_channel else guild.get_role(item) is not None

        for path, value in _id_fields(schema, stored):
            name = path[-1]
            spot = (module, ".".join(path))
            is_channel = "channel" in name or spot in CHANNEL_FIELDS
            is_role = "role" in name or spot in ROLE_FIELDS
            if not (is_channel or is_role) or not value:
                continue
            if isinstance(value, list) and all(isinstance(item, int) and item >= SNOWFLAKE_FROM for item in value):
                kept: Any = [item for item in value if await alive(item, is_channel)]
                gone = len(value) - len(kept)
            elif isinstance(value, int) and not isinstance(value, bool) and value >= SNOWFLAKE_FROM:
                kept = value if await alive(value, is_channel) else None
                gone = 0 if kept else 1
            else:
                continue
            if gone:
                dropped += gone
                holder = form
                for part in path[:-1]:
                    holder = holder.setdefault(part, {})
                holder[name] = kept
        if module == COMMANDS_MODULE:
            overwrites, gone = await self._live_overwrites(guild, stored.get("overwrites") or {}, known)
            if gone:
                form["overwrites"] = overwrites
                dropped += gone
        return form, dropped

    async def _live_overwrites(self, guild: Any, overwrites: dict, known: dict[int, bool]) -> tuple[dict, int]:
        """The per-command rules with deleted channels and roles taken out."""
        out: dict = {}
        gone = 0
        for command, row in overwrites.items():
            fresh = dict(row)
            for field in ("channel_wl", "channel_bl"):
                kept = [item for item in row.get(field) or [] if await self._channel_exists(guild, item, known)]
                gone += len(row.get(field) or []) - len(kept)
                if field in row:
                    fresh[field] = kept
            for field in ("role_wl", "role_bl", "bypass_roles"):
                kept = [item for item in row.get(field) or [] if guild.get_role(item) is not None]
                gone += len(row.get(field) or []) - len(kept)
                if field in row:
                    fresh[field] = kept
            redirect = row.get("redirect")
            if isinstance(redirect, int) and not await self._channel_exists(guild, redirect, known):
                fresh["redirect"] = None
                gone += 1
            out[command] = fresh
        return out, gone

    @commands.command(name="set", description="Set the channel for welcome or leave messages, or for Twitch alerts")
    @commands.guild_only()
    @requires("manage_guild")
    async def set_channel(self, ctx: commands.Context, *, word: str = "") -> None:
        """`set welcome`, `set farewell` and `set twitch` go to their commands through core.shortcuts. Anything else lands here."""
        await ctx.fail("Use `set welcome`, `set farewell` or `set twitch`, each with a channel, or with nothing to clear it.")

    # ---- signing in to the dashboard ----

    @commands.hybrid_command(name="dashboard", description="Get a private link that signs you in to the dashboard")
    async def dashboard(self, ctx: commands.Context) -> None:
        has_access = ctx.guild is not None and self.bot.ops.access_for(ctx.guild.id, ctx.author.id) is not None
        await self._send_link(ctx, f"/dashboard/{ctx.guild.id}" if has_access else "/servers", "Open the dashboard")

    @commands.hybrid_command(name="my_data", description="See, download or delete what the bot holds about you")
    async def my_data(self, ctx: commands.Context) -> None:
        await self._send_link(ctx, "/my-data", "See my data")

    async def _send_link(self, ctx: commands.Context, target: str, label: str) -> None:
        if not self.bot.public_url:
            await ctx.fail("This bot has no web address set.")
            return
        author = ctx.author
        name = author.global_name or author.name
        avatar = author.avatar.replace(size=128, format="png").url if author.avatar else None
        token = self.bot.sign_in_links.create(author.id, name, avatar, target)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label=label, url=f"{self.bot.public_url}/login/link/{token}"))
        text_ = (f"This link signs you in as **{discord.utils.escape_markdown(name)}**. "
                 f"It works once and expires in {LINK_SECONDS // 60} minutes. Don't share it.")
        # Anyone who clicked this link in a channel could sign in as this person. ctx.send can move a command's output to a channel, so the link never goes through it.
        if ctx.interaction is not None:
            await ctx.interaction.response.send_message(text_, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            await author.send(text_, view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            self.bot.sign_in_links.cancel(token)
            await ctx.fail("I can't DM you. Allow DMs from this server, or use the slash command `/dashboard`, which answers privately.")
            return
        await ctx.ok("I sent you a sign-in link in a DM.")

    # ---- help, ping, about ----

    def _typed_names(self, command: Any) -> list[str]:
        """The other names a command answers to when typed."""
        names = list(command.aliases)
        full = command.qualified_name
        names += [short for table in (SHORTCUTS, TAG_FIRST_SHORTCUTS) for short, target in table.items() if target == full]
        return sorted(set(names))

    def _help_lines(self, ctx: commands.Context, command: Any) -> str:
        """How to type a command: its usage with the server's prefix, and its other names. On a slash command, the slash path says all of that."""
        if ctx.interaction is not None:
            return ""
        prefix = ctx.clean_prefix or ""
        usage = f"Type it: `{(prefix + command.qualified_name + ' ' + command.signature).strip()}`"
        names = self._typed_names(command)
        return usage + (f"\nAlso answers to: {', '.join(f'`{name}`' for name in names)}" if names else "")

    def _group_help(self, ctx: commands.Context, found: commands.Group, about: str) -> discord.Embed:
        typed = ctx.interaction is None
        lines = []
        for child in sorted(found.commands, key=lambda c: c.name):
            shown = f"`{ctx.clean_prefix}{child.qualified_name}`" if typed else f"`/{child.qualified_name}`"
            aliases = f" ({', '.join(child.aliases)})" if typed and child.aliases else ""
            lines.append(f"{shown}{aliases}: {child.description or child.short_doc}")
        head = "\n".join(part for part in (about, self._help_lines(ctx, found)) if part)
        room = HELP_ROOM - len(head)
        shown_lines: list[str] = []
        for line in lines:
            room -= len(line) + 1
            if room < 0:
                break
            shown_lines.append(line)
        if len(shown_lines) < len(lines):
            shown_lines.append(f"And {len(lines) - len(shown_lines)} more.")
        return discord.Embed(title=f"/{found.qualified_name}", description="\n".join([head, "", *shown_lines]), color=0x00A2F8)

    @commands.hybrid_command(name="help", description="List commands, or show what one does")
    async def help(self, ctx: commands.Context, *, command: str | None = None) -> None:
        if command:
            named = named_command(self.bot, command)
            if named is None:
                await ctx.fail(f"There is no command called `{command.replace('`', '')[:60]}`.")
                return
            found = named[0]
            about = found.description or found.help or "No description."
            if isinstance(found, commands.Group):  # a big group's list runs past what a plain message can hold
                await ctx.send(embed=self._group_help(ctx, found, about), ephemeral=True)
                return
            typed = self._help_lines(ctx, found)
            await ctx.send(f"**/{found.qualified_name}**\n{about}" + (f"\n{typed}" if typed else ""), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        embed = discord.Embed(title=f"{brand.NAME} commands", description=f"Use `/help command` for details. Full settings: {self.bot.public_url}", color=0x00A2F8)
        for group in self.bot.command_catalog():
            names = ", ".join(f"`{row['name']}`" for row in group["commands"])
            embed.add_field(name=group["category"], value=names[:1024] or "none", inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="ping", description="Check that the bot is alive")
    async def ping(self, ctx: commands.Context) -> None:
        latency = self.bot.latency
        # Latency reads as not-a-number until the first heartbeat comes back.
        await ctx.send("Pong." if math.isnan(latency) else f"Pong. {round(latency * 1000)} ms.", ephemeral=True)

    @commands.hybrid_command(name="about", description="Facts about this bot")
    async def about(self, ctx: commands.Context) -> None:
        uptime = humanize(time.time() - self.bot.started_at)
        lines = [f"**{brand.NAME}** {__version__}", brand.TAGLINE, f"Up for {uptime}."]
        if brand.REPO_URL:
            lines.append(f"Source: {brand.REPO_URL}")
        await ctx.send("\n".join(lines), ephemeral=True)

    async def _save_core(self, ctx: commands.Context, action: str, form: dict) -> None:
        await self.bot.settings.save(ctx.guild.id, "core", form, actor=self.bot.actor(ctx.author), action=action)
