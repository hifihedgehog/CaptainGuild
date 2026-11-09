"""Statistics: the commands, cards, buttons and menus people use in Discord. A mixin the Statistics pack module inherits, so its commands
belong to the pack.

Every card answers the same way. The statistics settings decide who may ask (`command_access`), where (`command_channel_mode` and
`command_channels`), and who skips both (Administrator, Manage Server, `override_roles`). The card is drawn on a worker thread and kept for
a minute, per server, view, period, language and audience. Under it sit Refresh, a switch between the card and its chart, and a period
menu. A button press from the person who asked changes their message. Anyone else gets their own copy, which only they see. The buttons
carry everything they need in their IDs, so they keep working across restarts.

A channel's name shows only to people who can see that channel (see core/stats/views.py). A card about one channel is refused in a channel
whose members can't all see it, unless the answer can go to the asker alone.

Three subcommands of the shared /stats belong to the person rather than the server, so they answer wherever the bot is, statistics on or
off: privacy, language and private replies. Their choices sit in stat_user_settings, one row per person for every server.
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import logging
import re
import time
import types
import zipfile
from dataclasses import dataclass, replace
from typing import Any, Literal, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.module import Module, requires
from ..core.perms import answer_place
from ..core.stats import cards, reads, store, views, words
from ..core.stats import emoji as emoji_text
from ..core.stats import timeframe as tf
from ..core.validate import Invalid, ValidationError

log = logging.getLogger(__name__)

COMMANDS_FIELDS: dict[str, Any] = {}
# The person's reply language, and when their name last came off their statistics.
COMMANDS_MIGRATIONS: list[Any] = [
    "ALTER TABLE stat_user_settings ADD COLUMN language TEXT;\nALTER TABLE stat_user_settings ADD COLUMN anonymized_at REAL;",
]
# The covering indexes that keep rankings over years quick (see core/stats/reads.py).
COMMANDS_MESSAGE_MIGRATIONS: list[Any] = []

CACHE_SECONDS = 60.0
CACHE_MOST = 300
PICTURE_SECONDS = 600.0
PICTURE_MOST = 600
PICTURE_WAIT = 4.0  # seconds a picture may take before the card draws without it
PRIVACY_GAP = 86400  # a person changes their privacy once a day at most
FILE_NAME = "statistics.png"
NO_PINGS = discord.AllowedMentions.none()
SELF_VIEWS = frozenset({"member", "chart_member"})
CHANNEL_VIEWS = frozenset({"channel", "chart_channel"})
VIEW_CODES = {"server": "sv", "member": "mb", "channel": "ch", "messages": "ms", "voice": "vc", "activity": "ac", "emoji": "em", "invites": "in",
              "chart_messages": "cm", "chart_voice": "cv", "chart_members": "cb", "chart_status": "cs", "chart_activity": "ca", "chart_emoji": "ce",
              "chart_invites": "ci", "chart_member": "cu", "chart_channel": "cc", "top": "tp"}
VIEW_NAMES = {code: name for name, code in VIEW_CODES.items()}
BUTTON_TEMPLATE = r"cgst:(?P<action>[rtfbnlx]):(?P<view>[a-z]{2}):(?P<target>\d{1,20}):(?P<period>[a-z0-9]{1,9}):(?P<kind>\d)\.(?P<page>\d{1,6}):(?P<owner>\d{1,20}):(?P<guild>\d{1,20})"
SELECT_TEMPLATE = r"cgst:(?P<action>[ps]):(?P<view>[a-z]{2}):(?P<target>\d{1,20}):(?P<period>[a-z0-9]{1,9}):(?P<kind>\d)\.(?P<page>\d{1,6}):(?P<owner>\d{1,20}):(?P<guild>\d{1,20})"
PRIVACY_TEMPLATE = r"cgpv:(?P<choice>on|off|no):(?P<user>\d{1,20})"
CUSTOM = "custom"
PERIOD_CHOICES = ("d1", "d7", "d30", "s", "a")
LANGUAGE_CHOICES = [app_commands.Choice(name="Follow my Discord language", value="auto")] + [
    app_commands.Choice(name=words.NAMES[code], value=code) for code in words.LANGUAGES]
COMMON_ZONES = ("UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Sao_Paulo", "Europe/London",
                "Europe/Paris", "Europe/Berlin", "Europe/Madrid", "Europe/Amsterdam", "Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "Australia/Sydney")
ChannelArg = Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel, discord.ForumChannel, discord.Thread]
MENTION = re.compile(r"<(@!?|@&|#)(\d{15,21})>|\b(\d{15,21})\b")
CUSTOM_EMOJI = re.compile(r"<a?:\w{1,32}:(\d{15,21})>")
LINES_IN_A_REPLY = 30
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")  # how a cell a spreadsheet reads as a formula begins
FIRST_DAY = dt.date(2015, 1, 1)  # Discord's first day: nothing on Discord is older


def as_text(value: Any) -> str:
    """A cell for a file someone opens in a spreadsheet. Excel, LibreOffice and Google Sheets run a cell starting with =, +, -, @, a tab or
    a carriage return as a formula, so a name could fetch a web address or start a program. A leading apostrophe keeps it text."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def start_problem(lookback: dict) -> str | None:
    """The words.py key saying why a typed period can't be used, when it starts before Discord's first day. parse_lookback already
    refuses a start that hasn't come yet."""
    since = lookback.get("since") if lookback.get("mode") == "since" else None
    if since:
        try:
            if dt.date.fromisoformat(since) < FIRST_DAY:
                return "custom_too_early"
        except ValueError:
            return "custom_no_date"
    return None


# ---- what a card's buttons carry ----

@dataclass(frozen=True)
class Request:
    """One card: what it's about, the period, the page of /top, who asked and where."""
    view: str
    target: int
    period: str
    kind: str
    page: int
    owner: int
    guild: int

    def custom_id(self, action: str) -> str:
        kind = views.TOP_PAGES.index(self.kind) if self.kind in views.TOP_PAGES else 0
        return f"cgst:{action}:{VIEW_CODES[self.view]}:{self.target}:{self.period}:{kind}.{min(self.page, 999_999)}:{self.owner}:{self.guild}"

    @classmethod
    def from_match(cls, match: re.Match) -> "Request":
        kind = int(match["kind"])
        return cls(view=VIEW_NAMES.get(match["view"], "server"), target=int(match["target"]), period=match["period"],
                   kind=views.TOP_PAGES[kind] if kind < len(views.TOP_PAGES) else "overview", page=int(match["page"]), owner=int(match["owner"]),
                   guild=int(match["guild"]))


class CardButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_TEMPLATE):
    """Refresh, the chart switch, and /top's page buttons."""

    def __init__(self, request: Request, action: str, *, label: str = "", style: discord.ButtonStyle = discord.ButtonStyle.secondary,
                 disabled: bool = False, row: int | None = None, item: discord.ui.Button | None = None) -> None:
        super().__init__(item or discord.ui.Button(custom_id=request.custom_id(action), label=label[:80] or "-", style=style, disabled=disabled), row=row)
        self.request = request
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Any, match: re.Match, /) -> "CardButton":
        return cls(Request.from_match(match), match["action"], item=item)

    async def callback(self, interaction: discord.Interaction) -> None:
        module = interaction.client.module("statistics")
        if module is not None:
            await module.card_pressed(interaction, self.request, self.action)


class CardSelect(discord.ui.DynamicItem[discord.ui.Select], template=SELECT_TEMPLATE):
    """The period menu, and /top's page menu."""

    def __init__(self, request: Request, action: str, *, options: list[discord.SelectOption] | None = None, placeholder: str | None = None,
                 row: int | None = None, item: discord.ui.Select | None = None) -> None:
        super().__init__(item or discord.ui.Select(custom_id=request.custom_id(action), options=options or [discord.SelectOption(label="-")],
                                                   placeholder=(placeholder or "")[:150] or None), row=row)
        self.request = request
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Any, match: re.Match, /) -> "CardSelect":
        return cls(Request.from_match(match), match["action"], item=item)

    async def callback(self, interaction: discord.Interaction) -> None:
        module = interaction.client.module("statistics")
        if module is not None:
            values = list(getattr(self.item, "values", []) or [])
            await module.card_pressed(interaction, self.request, self.action, values[0] if values else None)


class PrivacyButton(discord.ui.DynamicItem[discord.ui.Button], template=PRIVACY_TEMPLATE):
    """The confirm and cancel buttons under /stats privacy."""

    def __init__(self, choice: str, user_id: int, *, label: str = "", style: discord.ButtonStyle = discord.ButtonStyle.secondary,
                 item: discord.ui.Button | None = None) -> None:
        super().__init__(item or discord.ui.Button(custom_id=f"cgpv:{choice}:{user_id}", label=label[:80] or "-", style=style))
        self.choice = choice
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Any, match: re.Match, /) -> "PrivacyButton":
        return cls(match["choice"], int(match["user"]), item=item)

    async def callback(self, interaction: discord.Interaction) -> None:
        module = interaction.client.module("statistics")
        if module is not None:
            await module.privacy_pressed(interaction, self.choice, self.user_id)


class CustomPeriod(discord.ui.Modal):
    """The Custom choice of the period menu: days, a date, a timestamp or all."""

    def __init__(self, stats: Any, request: Request, lang: str) -> None:
        super().__init__(title=words.t(lang, "custom_title")[:45])
        self.stats = stats
        self.request = request
        self.lang = lang
        self.box = discord.ui.TextInput(label=words.t(lang, "custom_label")[:45], placeholder=words.t(lang, "custom_placeholder")[:100], max_length=40)
        self.add_item(self.box)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.stats.custom_period_submitted(interaction, self.request, self.box.value, self.lang)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("The custom period form failed", exc_info=error)
        await _answer(interaction, words.t(self.lang, "render_failed"))


async def _answer(interaction: discord.Interaction, text: str) -> None:
    """A private answer, whether or not the interaction has had its first one."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)
        else:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)
    except discord.HTTPException as error:
        log.info("Could not answer a statistics action: %s", error)


# ---- whoever is asking, and how the answer reaches them ----

class Ask:
    """One person asking for a card, from a command (typed or slash) or from a right-click menu."""

    def __init__(self, guild: Any, member: Any, channel: Any, lang: str, *, private: bool, ctx: Any = None, interaction: Any = None,
                 muted: bool = False) -> None:
        self.guild = guild
        self.member = member
        self.channel = channel  # where a public answer goes
        self.lang = lang
        self.private = private
        self.ctx = ctx
        self.interaction = interaction
        self.muted = muted  # the server's command rules silence this command's answers

    @property
    def typed(self) -> bool:
        return self.ctx is not None and getattr(self.ctx, "interaction", None) is None

    def t(self, key: str, **values: Any) -> str:
        return words.t(self.lang, key, **values)

    async def fail(self, text: str) -> None:
        if self.ctx is not None:
            await self.ctx.fail(text)
        else:
            await _answer(self.interaction, f"\N{CROSS MARK} {text}")

    async def defer(self) -> None:
        if self.ctx is not None:
            interaction = getattr(self.ctx, "interaction", None)
            if interaction is not None and not interaction.response.is_done():
                await self.ctx.defer(ephemeral=self.private)
        elif not self.interaction.response.is_done():
            await self.interaction.response.defer(ephemeral=self.private, thinking=True)

    async def send(self, content: str | None = None, *, png: bytes | None = None, view: Any = None, filename: str = FILE_NAME, file: Any = None,
                   embed: Any = None) -> None:
        kwargs: dict[str, Any] = {"allowed_mentions": NO_PINGS}
        if png is not None:
            kwargs["file"] = discord.File(io.BytesIO(png), filename=filename)
        if file is not None:
            kwargs["file"] = file
        if view is not None:
            kwargs["view"] = view
        if embed is not None:
            kwargs["embed"] = embed
        if self.ctx is not None:
            if self.typed and self.private and not self.muted:  # a muted typed command answers nowhere, as the server's rules say
                try:
                    await self.ctx.author.send(content, **kwargs)
                except (discord.HTTPException, AttributeError):
                    await self.ctx.fail(self.t("dm_failed"))
                return
            await self.ctx.send(content, ephemeral=self.private, **kwargs)
            return
        await self.interaction.followup.send(content, ephemeral=self.private, **kwargs)


class Anywhere(Module):
    """The statistics commands that answer in every server, with statistics on or off: a person's own choices. Not a feature of its own: the
    Statistics module hands it these commands, and it hands the work back."""
    key = "statistics"
    category = "Stats"

    def __init__(self, stats: Any) -> None:
        super().__init__(stats.bot)
        self.stats = stats

    async def _privacy(self, ctx: commands.Context) -> None:
        await self.stats.privacy_command(ctx)

    @app_commands.describe(language="The language for your statistics cards and replies")
    @app_commands.choices(language=LANGUAGE_CHOICES)
    async def _language(self, ctx: commands.Context, language: Optional[str] = None) -> None:
        await self.stats.language_command(ctx, language)

    @app_commands.describe(setting="On sends your statistics replies only to you")
    async def _private_replies(self, ctx: commands.Context, setting: Optional[Literal["on", "off"]] = None) -> None:
        await self.stats.private_replies_command(ctx, setting)


# ---- the mixin ----

class StatsCommands:
    # ---- start and stop ----

    async def _commands_load(self) -> None:
        state = self._cards_state()
        for row in await self.db.all("SELECT user_id, ephemeral, language, private, private_changed_at, anonymized_at FROM stat_user_settings"):
            state["choices"][row["user_id"]] = row
        self.bot.add_dynamic_items(CardButton, CardSelect, PrivacyButton)
        self.bot.settings.listen(self.key, self._cards_settings_saved)
        self._attach_stats_subcommands()
        for menu in self._menus():
            self.bot.tree.add_command(menu, override=True)
        self.bot.tree.add_command(self._info_slash().app_command, override=True)
        # Someone whose privacy came on while the bot stopped part way through has the rest of their name taken off now.
        unfinished = [row["user_id"] for row in state["choices"].values() if row.get("private") and (row.get("anonymized_at") or 0) < (row.get("private_changed_at") or 0)]
        if unfinished:
            state["tasks"].add(asyncio.ensure_future(self._finish_privacy(unfinished)))

    def _commands_routes(self, router: Any) -> None:
        pass

    async def _commands_unload(self) -> None:
        state = self._cards_state()
        for task in list(state["tasks"]):
            task.cancel()
        for menu in state["menus"] or []:
            self.bot.tree.remove_command(menu.name, type=menu.type)
        if state["info_slash"] is not None:
            self.bot.tree.remove_command(state["info_slash"].name)
        parent = self.bot.get_command("stats")
        if parent is not None:
            for name in ("message", "voice", "user", "channel", "activity", "invite", "emoji", "privacy", "language", "private_replies"):
                found = parent.get_command(name)
                if found is not None and getattr(found, "cog", None) in (self, state.get("anywhere")):
                    parent.remove_command(name)

    def _cards_state(self) -> dict:
        """What the commands keep in memory. Made on first use, since the Statistics module's own setup runs first."""
        state = getattr(self, "_cards_memory", None)
        if state is None:
            state = {"cards": {}, "locks": {}, "pictures": {}, "choices": {}, "tasks": set(), "anywhere": Anywhere(self), "menus": None, "privacy_busy": set(),
                     "privacy_epoch": 0, "info_slash": None}
            self._cards_memory = state
        return state

    async def _cards_settings_saved(self, guild_id: int, settings: dict) -> None:
        cache = self._cards_state()["cards"]
        for key in [key for key in cache if key[0] == guild_id]:
            cache.pop(key, None)

    def _privacy_changed(self) -> None:
        """Someone's privacy changed, or their counts finished moving: every kept card may name them, so none is kept, and a card being
        drawn now isn't kept either (card_png compares this count before and after)."""
        state = self._cards_state()
        state["privacy_epoch"] += 1
        state["cards"].clear()

    def _attach_stats_subcommands(self) -> None:
        """The card subcommands join the shared /stats, which belongs to the utilities feature. Without it there's no group to join."""
        parent = self.bot.get_command("stats")
        if parent is None or parent.get_command("message") is not None:
            return
        anywhere = self._cards_state()["anywhere"]
        for name, callback, description, cog in (
            ("message", StatsCommands._stats_message, "Show message statistics: totals, top members and top channels", self),
            ("voice", StatsCommands._stats_voice, "Show voice statistics: totals, top members and top channels", self),
            ("user", StatsCommands._stats_user, "Show a member's statistics card", self),
            ("channel", StatsCommands._stats_channel, "Show a channel's statistics card", self),
            ("activity", StatsCommands._stats_activity, "Show the top games and apps people use", self),
            ("invite", StatsCommands._stats_invite, "Show the top invites and inviters", self),
            ("emoji", StatsCommands._stats_emoji, "Show the most used emoji", self),
            ("privacy", Anywhere._privacy, "Take your name off your statistics in every server", anywhere),
            ("language", Anywhere._language, "Pick the language of your statistics cards and replies", anywhere),
            ("private_replies", Anywhere._private_replies, "Get your statistics replies where only you see them", anywhere),
        ):
            command = commands.HybridCommand(callback, name=name, description=description)
            command.cog = cog
            parent.add_command(command)

    def _info_slash(self) -> commands.HybridGroup:
        """/info activity, emoji, id and roles: the lookups under the slash names people know them by. Typed, `info` has long meant the
        member lookup (`members info`), so the typed lookups go by `statsinfo`, and `info activity` and the rest are short names for them
        (core/shortcuts.py). This group lives only on the slash side, in the pack's commands, and its subcommands follow the typed ones'
        rules on the Commands page."""
        state = self._cards_state()
        if state["info_slash"] is None:
            group = commands.HybridGroup(_info_slash_group, name="info", description=self.info_group.description)
            group.cog = self
            group.app_command.guild_only = True
            for typed in sorted(self.info_group.commands, key=lambda command: command.name):
                command = commands.HybridCommand(typed.callback, name=typed.name, description=typed.description, extras={"rules_of": typed.qualified_name})
                command.cog = self
                group.add_command(command)
            state["info_slash"] = group
        return state["info_slash"]

    def _menus(self) -> list[app_commands.ContextMenu]:
        state = self._cards_state()
        if state["menus"] is None:
            menus = [
                app_commands.ContextMenu(name="Channel Statistics", callback=self.channel_statistics_menu),
                app_commands.ContextMenu(name="Member Statistics", callback=self.member_statistics_message_menu),
                app_commands.ContextMenu(name="Member Statistics", callback=self.member_statistics_user_menu),
            ]
            for menu in menus:
                menu.guild_only = True
            state["menus"] = menus
        return state["menus"]

    # ---- the person's choices ----

    def _choice(self, user_id: int) -> dict:
        return self._cards_state()["choices"].get(user_id) or {}

    async def _save_choice(self, user_id: int, **values: Any) -> dict:
        names = sorted(values)
        marks = ", ".join("?" for _ in names)
        updates = ", ".join(f"{name} = excluded.{name}" for name in names)
        await self.db.run(f"INSERT INTO stat_user_settings (user_id, {', '.join(names)}) VALUES (?, {marks}) ON CONFLICT (user_id) DO UPDATE SET {updates}",
                          (user_id, *[values[name] for name in names]))
        row = await self.db.one("SELECT user_id, ephemeral, language, private, private_changed_at, anonymized_at FROM stat_user_settings WHERE user_id = ?", (user_id,))
        self._cards_state()["choices"][user_id] = row
        return row

    async def set_language(self, user_id: int, code: str | None) -> str | None:
        """Sets the language of someone's statistics cards and replies, or None to follow their Discord app. Returns the code kept.
        The My data page calls this too, so the commands' copy of the choice stays in step."""
        picked = None if code is None else words.pick(code)
        await self._save_choice(user_id, language=picked)
        return picked

    async def set_private_replies(self, user_id: int, on: bool) -> None:
        """Sends someone's statistics replies only to them, or back to the channel. The My data page calls this too."""
        await self._save_choice(user_id, ephemeral=1 if on else 0)

    async def reload_choices(self, user_id: int) -> None:
        """Reads someone's statistics choices again, after something other than these methods changed stat_user_settings."""
        row = await self.db.one("SELECT user_id, ephemeral, language, private, private_changed_at, anonymized_at FROM stat_user_settings WHERE user_id = ?", (user_id,))
        choices = self._cards_state()["choices"]
        if row is None:
            choices.pop(user_id, None)
        else:
            choices[user_id] = row

    def language_of(self, user_id: int, locale: Any = None, guild: Any = None) -> str:
        """The language for this person's cards and replies: their own pick, else their Discord app's, else the server's."""
        return words.pick(self._choice(user_id).get("language"), locale, getattr(guild, "preferred_locale", None))

    def replies_private(self, user_id: int) -> bool:
        return bool(self._choice(user_id).get("ephemeral"))

    # ---- who may ask, and where ----

    def _bypasses(self, member: Any) -> bool:
        if member is None:
            return False
        if access.is_admin(member) or member.guild_permissions.manage_guild:
            return True
        held = {role.id for role in getattr(member, "roles", [])}
        return bool(held & set(self.cfg(member.guild.id)["override_roles"]))

    def _refusal(self, member: Any, channel: Any, view: str, target: int, lang: str) -> str | None:
        """Why this person may not have this card here, or None."""
        if member is None:
            return words.t(lang, "left_server")
        if self._bypasses(member):
            return None
        cfg = self.cfg(member.guild.id)
        level = cfg["command_access"]
        own = view in SELF_VIEWS and target == member.id
        if level == "admins":
            return words.t(lang, "access_admins")
        if level == "self" and not own:
            return words.t(lang, "access_self")
        if level == "restricted" and view in SELF_VIEWS and not own:
            return words.t(lang, "access_restricted")
        return self._channel_refusal(member.guild.id, channel, lang)

    def _channel_refusal(self, guild_id: int, channel: Any, lang: str) -> str | None:
        """Why statistics commands don't answer in this channel, under the server's command channel list, or None."""
        cfg = self.cfg(guild_id)
        listed = set(cfg["command_channels"])
        if not listed or channel is None:
            return None
        place = views._place(channel)
        here = {place.id, getattr(place, "category_id", None)}
        if cfg["command_channel_mode"] == "block" and here & listed:
            return words.t(lang, "channel_blocked")
        if cfg["command_channel_mode"] == "allow" and not here & listed:
            # The answer lands where people who can't see every listed channel may read it, so it names only the ones they all can.
            guild = getattr(place, "guild", None)
            find = getattr(guild, "get_channel_or_thread", None) or getattr(guild, "get_channel", None)
            named = [channel_id for channel_id in sorted(listed) if find is not None and views.all_see(guild, place, find(channel_id))][:10]
            if not named:
                return words.t(lang, "channel_blocked")
            items = ", ".join(f"<#{channel_id}>" for channel_id in named)
            if len(listed) > len(named):
                items = words.t(lang, "and_more", items=items, count=words.number(lang, len(listed) - len(named)))
            return words.t(lang, "channel_only", channels=items)
        return None

    def _destination(self, ctx: Any) -> tuple[Any, bool, bool]:
        """Where a command's answer will land, whether only the asker sees it, and whether the server's command rules mute it."""
        decision = getattr(ctx, "decision", None)
        channel = ctx.channel
        if decision is None:
            return channel, False, False
        if getattr(decision, "muted", False):
            return channel, True, True
        target = decision.redirect or decision.restricted_channel
        if target == "dm":
            return None, True, False
        if target:
            place = answer_place(ctx.guild, target)
            if place is not None:
                return place, False, False
        return channel, False, False

    def _ask(self, ctx: Any) -> Ask:
        guild = ctx.guild
        member = ctx.author if hasattr(ctx.author, "roles") else guild.get_member(ctx.author.id)
        interaction = getattr(ctx, "interaction", None)
        lang = self.language_of(ctx.author.id, getattr(interaction, "locale", None), guild)
        channel, forced, muted = self._destination(ctx)
        return Ask(guild, member, channel, lang, private=forced or self.replies_private(ctx.author.id), ctx=ctx, muted=muted)

    def _ask_menu(self, interaction: discord.Interaction) -> Ask:
        guild = interaction.guild
        member = interaction.user if hasattr(interaction.user, "roles") else guild.get_member(interaction.user.id)
        lang = self.language_of(interaction.user.id, interaction.locale, guild)
        return Ask(guild, member, interaction.channel, lang, private=self.replies_private(interaction.user.id), interaction=interaction)

    # ---- drawing a card ----

    def _levels_here(self, guild_id: int) -> Any:
        levels = self.bot.module("levels")
        return levels if levels is not None and levels.cfg(guild_id).get("enabled") else None

    def _visibility(self, guild: Any, member: Any, channel: Any, private: bool) -> tuple[views.Visibility, tuple]:
        if private or channel is None:
            return views.Visibility(guild, member, None, False), ("private", getattr(member, "id", 0))
        place = getattr(channel, "parent", None) if isinstance(channel, discord.Thread) else channel
        return views.Visibility(guild, member, channel, True), ("public", getattr(place or channel, "id", 0))

    async def card_png(self, guild: Any, request: Request, target: Any, lang: str, visibility: views.Visibility, audience_key: tuple) -> tuple[bytes, dict]:
        """The card's PNG and the numbers behind it, from the minute-long cache when it's there."""
        state = self._cards_state()
        key = (guild.id, request.view, request.target, request.period, request.kind, request.page, lang, audience_key)
        cache = state["cards"]
        hit = cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1], hit[2]
        lock = state["locks"].setdefault(key, asyncio.Lock())
        try:
            async with lock:
                hit = cache.get(key)
                if hit is not None and hit[0] > time.monotonic():
                    return hit[1], hit[2]
                epoch = state["privacy_epoch"]
                await self.up_to_date()
                cfg = self.cfg(guild.id)
                scope = views.Scope(db=self.message_db, guild=guild, cfg=cfg, lang=lang, now=time.time(), lookback=views.lookback_of(request.period, cfg),
                                    filters=views.filters_for(cfg, guild), visibility=visibility, private=frozenset(self.private),
                                    levels=self._levels_here(guild.id))
                data = await views.gather(scope, request.view, target, page_kind=request.kind, page=request.page)
                pictures = await self._pictures(guild, data, target)
                png = await asyncio.to_thread(cards.render, data, lang, pictures)
                if state["privacy_epoch"] != epoch:
                    return png, data  # someone's privacy changed while this was drawn, so it may name them: it goes to its asker and no further
                if len(cache) >= CACHE_MOST:
                    now = time.monotonic()
                    for old in [old for old, value in cache.items() if value[0] <= now] or list(cache)[: CACHE_MOST // 4]:
                        cache.pop(old, None)
                cache[key] = (time.monotonic() + CACHE_SECONDS, png, data)
                return png, data
        finally:
            if not lock.locked():
                state["locks"].pop(key, None)

    async def _picture(self, url: str | None) -> bytes | None:
        """A picture from Discord's CDN, kept ten minutes. A slow or failed fetch leaves the card without it."""
        if not url:
            return None
        pictures = self._cards_state()["pictures"]
        hit = pictures.get(url)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        try:
            data = await asyncio.wait_for(self.bot.http.get_from_cdn(url), PICTURE_WAIT)
        except Exception as error:  # the card draws a letter or the emoji's name instead
            log.debug("No picture from %s: %s", url, error)
            data = None
        if len(pictures) >= PICTURE_MOST:
            pictures.clear()
        pictures[url] = (time.monotonic() + PICTURE_SECONDS, data)
        return data

    @staticmethod
    def _asset_url(asset: Any, size: int = 128) -> str | None:
        try:
            return asset.with_static_format("png").with_size(size).url
        except Exception:  # no picture, or one the library can't resize
            return None

    async def _pictures(self, guild: Any, data: dict, target: Any) -> dict:
        """Every picture a card names, fetched at once, so a slow one costs the card one wait rather than one each."""
        urls: dict[Any, str | None] = {}
        view = data["view"]
        if view in ("server", "top") and getattr(guild, "icon", None) is not None:
            urls["icon"] = self._asset_url(guild.icon)
        if view == "member" and target is not None and getattr(target, "display_avatar", None) is not None:
            urls["avatar"] = self._asset_url(target.display_avatar)
        keys: list[dict] = []
        for name in ("top", "top_emoji", "rows", "lines"):
            keys.extend(entry for entry in data.get(name) or [] if isinstance(entry, dict) and entry.get("key"))
        if data.get("emoji"):
            keys.append(data["emoji"])
        for entry in (data.get("sections") or {}).get("emoji") or []:
            keys.append(entry)
        for entry in keys:
            key = entry.get("key")
            if not isinstance(key, str) or key[:2] not in ("c:", "s:") or ("emoji", key) in urls:
                continue
            number = int(key[2:]) if key[2:].isdigit() else 0
            if key.startswith("c:"):
                urls[("emoji", key)] = f"https://cdn.discordapp.com/emojis/{number}.png?size=64"
            else:
                sticker = next((item for item in getattr(guild, "stickers", []) if item.id == number), None)
                if sticker is not None and not str(getattr(sticker, "format", "")).endswith("lottie"):
                    urls[("emoji", key)] = getattr(sticker, "url", None)
        found = await asyncio.gather(*(self._picture(url) for url in urls.values()))
        out: dict[str, Any] = {"emoji": {}}
        for name, data_bytes in zip(urls, found):
            if isinstance(name, tuple):
                out["emoji"][name[1]] = data_bytes
            else:
                out[name] = data_bytes
        return out

    def _card_view(self, request: Request, lang: str, data: dict | None = None) -> discord.ui.View:
        """The buttons and menus under a card."""
        view = discord.ui.View(timeout=None)
        period_row = 1
        if request.view == "top":
            current = request.kind
            options = [discord.SelectOption(label=words.t(lang, cards.TOP_TITLES[kind])[:100], value=kind, default=kind == current) for kind in views.TOP_PAGES]
            view.add_item(CardSelect(request, "s", options=options, row=0,
                                     placeholder=words.t(lang, "top_page_placeholder", label=words.t(lang, cards.TOP_TITLES[current]))))
            period_row = 1
            if current != "overview" and data is not None:
                pages = data.get("pages", 1)
                page = data.get("page", 0)
                for action, label, disabled in (("f", words.t(lang, "first"), page == 0), ("b", words.t(lang, "previous"), page == 0),
                                                ("x", f"{page + 1} / {pages}", True), ("n", words.t(lang, "next"), page >= pages - 1),
                                                ("l", words.t(lang, "last"), page >= pages - 1)):
                    view.add_item(CardButton(request, action, label=label, disabled=disabled, row=1))
                period_row = 2
            view.add_item(self._period_menu(request, lang, period_row))
            view.add_item(CardButton(request, "r", label=words.t(lang, "refresh"), row=period_row + 1))
            return view
        view.add_item(CardButton(request, "r", label=words.t(lang, "refresh"), row=0))
        if request.view in views.CHART_OF or request.view in views.OVERVIEW_OF:
            label = words.t(lang, "show_overview" if request.view.startswith("chart_") else "show_chart")
            view.add_item(CardButton(request, "t", label=label, row=0))
        view.add_item(self._period_menu(request, lang, period_row))
        return view

    def _period_menu(self, request: Request, lang: str, row: int) -> CardSelect:
        cfg = self.cfg(request.guild)
        server_label = words.period_label(lang, dict(cfg["lookback"]))
        labels = {"d1": words.t(lang, "last_day"), "d7": words.t(lang, "last_days", days=7), "d30": words.t(lang, "last_days", days=30),
                  "s": words.t(lang, "option_server", label=server_label), "a": words.t(lang, "all_time")}
        options = [discord.SelectOption(label=labels[code][:100], value=code, default=code == request.period) for code in PERIOD_CHOICES]
        if request.period not in PERIOD_CHOICES:
            current = words.period_label(lang, views.lookback_of(request.period, cfg))
            options.append(discord.SelectOption(label=current[:100], value=request.period, default=True))
        options.append(discord.SelectOption(label=words.t(lang, "option_custom")[:100], value=CUSTOM, description=words.t(lang, "option_custom_hint")[:100]))
        current_label = words.period_label(lang, views.lookback_of(request.period, cfg))
        return CardSelect(request, "p", options=options, row=row, placeholder=words.t(lang, "period_placeholder", label=current_label))

    # ---- answering a command or a menu ----

    async def _show(self, ctx: Any, view: str, target: Any = None, *, page_kind: str = "overview") -> None:
        ask = self._ask(ctx)
        if not self.on_in(ctx.guild.id):
            await ask.fail(ask.t("off"))
            return
        await self._serve(ask, view, target, page_kind=page_kind)

    async def _serve(self, ask: Ask, view: str, target: Any = None, *, page_kind: str = "overview") -> None:
        if view in SELF_VIEWS and target is not None:
            if target.id in self.private:
                await ask.fail(ask.t("you_private" if target.id == getattr(ask.member, "id", None) else "private_person"))
                return
        asked_in = ask.ctx.channel if ask.ctx is not None else ask.interaction.channel
        refusal = self._refusal(ask.member, asked_in, view, target.id if view in SELF_VIEWS and target is not None else 0, ask.lang)
        if refusal:
            await ask.fail(refusal)
            return
        if view in CHANNEL_VIEWS:
            if target is None or not hasattr(target, "type"):
                await ask.fail(ask.t("not_a_stats_channel"))
                return
            if isinstance(target, discord.Thread) or getattr(target, "type", None) in (discord.ChannelType.public_thread, discord.ChannelType.private_thread,
                                                                                        discord.ChannelType.news_thread):
                target = getattr(target, "parent", None) or target
            if getattr(getattr(target, "type", None), "value", None) not in views.CHANNEL_KINDS:
                await ask.fail(ask.t("not_a_stats_channel"))
                return
            if not access.can_view(ask.member, target):
                await ask.fail(ask.t("cant_see_channel"))
                return
            if not ask.private and ask.channel is not None and not views.all_see(ask.guild, ask.channel, target):
                if ask.typed:
                    await ask.fail(ask.t("channel_not_public"))
                    return
                ask.private = True
        request = Request(view=view, target=getattr(target, "id", 0) or 0, period="s", kind=page_kind, page=0, owner=ask.member.id, guild=ask.guild.id)
        await ask.defer()
        visibility, audience_key = self._visibility(ask.guild, ask.member, ask.channel, ask.private)
        try:
            png, data = await self.card_png(ask.guild, request, target, ask.lang, visibility, audience_key)
        except Exception:
            log.exception("A statistics card failed in %s", ask.guild.id)
            await ask.fail(ask.t("render_failed"))
            return
        await ask.send(png=png, view=self._card_view(request, ask.lang, data))

    async def channel_statistics_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        ask = self._ask_menu(interaction)
        if interaction.guild is None or not self.on_in(interaction.guild.id):
            await _answer(interaction, ask.t("off"))
            return
        await self._serve(ask, "channel", message.channel)

    async def member_statistics_message_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        ask = self._ask_menu(interaction)
        if interaction.guild is None or not self.on_in(interaction.guild.id):
            await _answer(interaction, ask.t("off"))
            return
        await self._serve(ask, "member", message.author)

    async def member_statistics_user_menu(self, interaction: discord.Interaction, user: discord.User) -> None:
        ask = self._ask_menu(interaction)
        if interaction.guild is None or not self.on_in(interaction.guild.id):
            await _answer(interaction, ask.t("off"))
            return
        await self._serve(ask, "member", user)

    # ---- the buttons ----

    def _target_of(self, guild: Any, request: Request) -> Any:
        """What a card's buttons name, found again: a member, someone who left, or a channel. None when a channel is gone."""
        if request.view in SELF_VIEWS:
            found = guild.get_member(request.target)
            if found is not None:
                return found
            user = self.bot.get_user(request.target)
            return user or types.SimpleNamespace(id=request.target, display_name=None, name=None, joined_at=None, created_at=None, bot=False)
        if request.view in CHANNEL_VIEWS:
            return guild.get_channel(request.target)
        return None

    async def card_pressed(self, interaction: discord.Interaction, request: Request, action: str, value: str | None = None) -> None:
        guild = self.bot.get_guild(request.guild)
        lang = self.language_of(interaction.user.id, interaction.locale, guild)
        if guild is None or not self.on_in(guild.id):
            await _answer(interaction, words.t(lang, "off"))
            return
        member = guild.get_member(interaction.user.id)
        new = request
        if action == "t":
            switched = views.CHART_OF.get(request.view) or views.OVERVIEW_OF.get(request.view) or request.view
            new = replace(request, view=switched, kind="overview", page=0)
        elif action == "p":
            if value == CUSTOM:
                try:
                    await interaction.response.send_modal(CustomPeriod(self, request, lang))
                except discord.HTTPException as error:
                    log.info("Could not open the custom period form: %s", error)
                return
            if value:
                new = replace(request, period=value if (value in PERIOD_CHOICES or re.fullmatch(r"d\d{1,5}|y\d{8}|a|s", value)) else "s")
        elif action == "s" and value in views.TOP_PAGES:
            new = replace(request, kind=value, page=0)
        elif action == "f":
            new = replace(request, page=0)
        elif action == "b":
            new = replace(request, page=max(0, request.page - 1))
        elif action == "n":
            new = replace(request, page=request.page + 1)
        elif action == "l":
            new = replace(request, page=999_999)
        await self._redraw(interaction, guild, member, new, lang)

    async def custom_period_submitted(self, interaction: discord.Interaction, request: Request, text: str, lang: str) -> None:
        guild = self.bot.get_guild(request.guild)
        if guild is None or not self.on_in(guild.id):
            await _answer(interaction, words.t(lang, "off"))
            return
        cfg = self.cfg(guild.id)
        try:
            lookback = tf.parse_lookback(text, tf.zone(cfg["timezone"]), time.time())
        except Invalid as error:
            await _answer(interaction, f"\N{CROSS MARK} {_custom_error(lang, str(error))}")
            return
        problem = start_problem(lookback)
        if problem:
            await _answer(interaction, f"\N{CROSS MARK} {words.t(lang, problem)}")
            return
        await self._redraw(interaction, guild, guild.get_member(interaction.user.id), replace(request, period=views.code_of(lookback), page=0), lang)

    async def _redraw(self, interaction: discord.Interaction, guild: Any, member: Any, request: Request, lang: str) -> None:
        """The card again after a press: in place for the person who asked, or as a copy only the presser sees."""
        mine = interaction.user.id == request.owner
        if not mine:
            request = replace(request, owner=interaction.user.id)
        target = self._target_of(guild, request)
        if request.view in CHANNEL_VIEWS and target is None:
            await _answer(interaction, words.t(lang, "channel_gone"))
            return
        if request.view in SELF_VIEWS and target is not None and target.id in self.private:
            await _answer(interaction, words.t(lang, "you_private" if target.id == interaction.user.id else "private_person"))
            return
        where = interaction.channel if interaction.guild is not None else None
        refusal = self._refusal(member, where, request.view, request.target, lang)
        if refusal:
            await _answer(interaction, f"\N{CROSS MARK} {refusal}")
            return
        if request.view in CHANNEL_VIEWS and not access.can_view(member, target):
            await _answer(interaction, f"\N{CROSS MARK} {words.t(lang, 'cant_see_channel')}")
            return
        message = interaction.message
        private = not mine or interaction.guild is None or bool(message is not None and message.flags.ephemeral)
        if not private and request.view in CHANNEL_VIEWS and not views.all_see(guild, where, target):
            private, mine = True, False
        try:
            if mine:
                await interaction.response.defer()
            else:
                await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException as error:
            log.info("Could not answer a statistics button: %s", error)
            return
        visibility, audience_key = self._visibility(guild, member, where, private)
        try:
            png, data = await self.card_png(guild, request, target, lang, visibility, audience_key)
        except Exception:
            log.exception("A statistics card failed in %s", guild.id)
            await _answer(interaction, words.t(lang, "render_failed"))
            return
        if request.view == "top":
            request = replace(request, page=data.get("page", 0))
        file = discord.File(io.BytesIO(png), filename=FILE_NAME)
        try:
            if mine:
                await interaction.edit_original_response(attachments=[file], view=self._card_view(request, lang, data))
            else:
                await interaction.followup.send(file=file, view=self._card_view(request, lang, data), ephemeral=True)
        except discord.HTTPException as error:
            log.info("Could not show a statistics card: %s", error)

    # ---- the cards ----

    @commands.hybrid_command(name="server", description="Show this server's statistics card")
    @commands.guild_only()
    async def server_card(self, ctx: commands.Context) -> None:
        await self._show(ctx, "server")

    @commands.hybrid_command(name="me", description="Show your own statistics card")
    @commands.guild_only()
    async def me_card(self, ctx: commands.Context) -> None:
        await self._show(ctx, "member", ctx.author)

    @commands.hybrid_group(name="user", fallback="stats", aliases=["u"], description="Show a member's statistics card or chart")
    @commands.guild_only()
    @app_commands.describe(member="Whose statistics. Leave it out for your own")
    async def user_card(self, ctx: commands.Context, member: Optional[discord.User] = None) -> None:
        await self._show(ctx, "member", member or ctx.author)

    @user_card.command(name="chart", description="Show a member's messages and voice over time")
    @app_commands.describe(member="Whose statistics. Leave it out for your own")
    async def user_chart(self, ctx: commands.Context, member: Optional[discord.User] = None) -> None:
        await self._show(ctx, "chart_member", member or ctx.author)

    @commands.hybrid_group(name="channel", fallback="stats", description="Show a channel's statistics card or chart")
    @commands.guild_only()
    @app_commands.describe(channel="The channel or category. Leave it out for this one")
    async def channel_card(self, ctx: commands.Context, channel: Optional[ChannelArg] = None) -> None:
        await self._show(ctx, "channel", channel or ctx.channel)

    @channel_card.command(name="chart", description="Show a channel's messages over time")
    @app_commands.describe(channel="The channel or category. Leave it out for this one")
    async def channel_chart(self, ctx: commands.Context, channel: Optional[ChannelArg] = None) -> None:
        await self._show(ctx, "chart_channel", channel or ctx.channel)

    @commands.hybrid_command(name="messages", aliases=["m"], description="Show message statistics: totals, top members and top channels")
    @commands.guild_only()
    async def messages_card(self, ctx: commands.Context) -> None:
        await self._show(ctx, "messages")

    @commands.hybrid_command(name="voice", description="Show voice statistics: totals, top members and top channels")
    @commands.guild_only()
    async def voice_card(self, ctx: commands.Context) -> None:
        await self._show(ctx, "voice")

    @commands.hybrid_command(name="activity", description="Show the top games and apps people use")
    @commands.guild_only()
    async def activity_card(self, ctx: commands.Context) -> None:
        await self._show(ctx, "activity")

    @commands.hybrid_command(name="invites", description="Show the top invites, the top inviters and joins over time")
    @commands.guild_only()
    async def invites_card(self, ctx: commands.Context) -> None:
        await self._show(ctx, "invites")

    @commands.hybrid_command(name="emoji", aliases=["emojis", "emo"], description="Show the most used emoji")
    @commands.guild_only()
    async def emoji_card(self, ctx: commands.Context) -> None:
        await self._show(ctx, "emoji")

    @commands.hybrid_command(name="top", description="Show the top members, channels and emoji, a page at a time")
    @commands.guild_only()
    async def top_card(self, ctx: commands.Context) -> None:
        await self._show(ctx, "top", page_kind="overview")

    # ---- the same cards on /stats ----

    async def _stats_message(self, ctx: commands.Context) -> None:
        await self._show(ctx, "messages")

    async def _stats_voice(self, ctx: commands.Context) -> None:
        await self._show(ctx, "voice")

    @app_commands.describe(member="Whose statistics. Leave it out for your own")
    async def _stats_user(self, ctx: commands.Context, member: Optional[discord.User] = None) -> None:
        await self._show(ctx, "member", member or ctx.author)

    @app_commands.describe(channel="The channel or category. Leave it out for this one")
    async def _stats_channel(self, ctx: commands.Context, channel: Optional[ChannelArg] = None) -> None:
        await self._show(ctx, "channel", channel or ctx.channel)

    async def _stats_activity(self, ctx: commands.Context) -> None:
        await self._show(ctx, "activity")

    async def _stats_invite(self, ctx: commands.Context) -> None:
        await self._show(ctx, "invites")

    async def _stats_emoji(self, ctx: commands.Context) -> None:
        await self._show(ctx, "emoji")

    # ---- charts ----

    @commands.hybrid_group(name="chart", description="Show statistics over time as a chart")
    @commands.guild_only()
    async def chart_group(self, ctx: commands.Context) -> None:
        ask = self._ask(ctx)
        await ask.fail(ask.t("chart_help"))

    @chart_group.command(name="message", description="Chart messages and the people sending them")
    async def chart_message(self, ctx: commands.Context) -> None:
        await self._show(ctx, "chart_messages")

    @chart_group.command(name="voice", description="Chart voice time and the people in voice")
    async def chart_voice(self, ctx: commands.Context) -> None:
        await self._show(ctx, "chart_voice")

    @chart_group.command(name="member", description="Chart the member count, joins and leaves")
    async def chart_member_count(self, ctx: commands.Context) -> None:
        await self._show(ctx, "chart_members")

    @chart_group.command(name="status", description="Chart how many members are online, idle and on do not disturb")
    async def chart_status(self, ctx: commands.Context) -> None:
        await self._show(ctx, "chart_status")

    @chart_group.command(name="activity", description="Chart the top games and apps over time")
    async def chart_activity(self, ctx: commands.Context) -> None:
        await self._show(ctx, "chart_activity")

    @chart_group.command(name="emoji", description="Chart the top emoji over time")
    async def chart_emoji(self, ctx: commands.Context) -> None:
        await self._show(ctx, "chart_emoji")

    @chart_group.command(name="invite", description="Chart joins through the top invites")
    async def chart_invite(self, ctx: commands.Context) -> None:
        await self._show(ctx, "chart_invites")

    @chart_group.command(name="user", description="Chart a member's messages and voice")
    @app_commands.describe(member="Whose statistics. Leave it out for your own")
    async def chart_user(self, ctx: commands.Context, member: Optional[discord.User] = None) -> None:
        await self._show(ctx, "chart_member", member or ctx.author)

    @chart_group.command(name="channel", description="Chart a channel's messages over time")
    @app_commands.describe(channel="The channel or category. Leave it out for this one")
    async def chart_channel(self, ctx: commands.Context, channel: Optional[ChannelArg] = None) -> None:
        await self._show(ctx, "chart_channel", channel or ctx.channel)

    # ---- the lookups: /info on the slash side (see _info_slash), statsinfo typed ----

    @commands.group(name="statsinfo", invoke_without_command=True, extras={"slash_name": "info"},
                    description="Look up IDs, members with roles, emoji use and what someone is playing")
    @commands.guild_only()
    async def info_group(self, ctx: commands.Context) -> None:
        ask = self._ask(ctx)
        await ask.fail(ask.t("info_help"))

    @info_group.command(name="activity", description="Show what a member is playing or listening to right now")
    @app_commands.describe(member="Whose activities")
    async def info_activity(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        ask = self._ask(ctx)
        if not self.on_in(ctx.guild.id):
            await ask.fail(ask.t("off"))
            return
        member = member or ctx.author
        refusal = self._refusal(ask.member, ctx.channel, "member", member.id, ask.lang)
        if refusal:
            await ask.fail(refusal)
            return
        if not self.bot.intents.presences:
            await ask.fail(ask.t("info_no_presence"))
            return
        found = [activity for activity in getattr(member, "activities", ()) or () if activity is not None]
        name = discord.utils.escape_markdown(member.display_name)
        if not found:
            await ask.fail(ask.t("info_no_activity", name=name))
            return
        from .statistics import activity_identity
        tracking = self.cfg(ctx.guild.id)["track_activities"]
        verify = self.cfg(ctx.guild.id)["verify_activities"]
        lines = []
        for activity in found:
            identity = activity_identity(activity)
            label = discord.utils.escape_markdown(getattr(activity, "name", None) or "?")
            if identity is None:
                lines.append(f"**{label}**: {ask.t('info_not_counted_kind')}")
                continue
            kind, application_id, _name, verified = identity
            verb = ask.t({0: "playing", 1: "streaming", 2: "listening", 3: "watching", 5: "competing"}.get(kind, "playing"))
            app_text = ask.t("info_app_id", id=application_id) if application_id else ask.t("info_no_app_id")
            if not tracking:
                state = ask.t("info_not_counted_off")
            elif verify and not verified:
                state = ask.t("info_not_counted_unverified")
            else:
                state = ask.t("info_counted")
            lines.append(f"**{verb} {label}** ({app_text}): {state}")
        embed = discord.Embed(title=ask.t("info_activity_title", name=member.display_name)[:256], description="\n".join(lines)[:4000], color=0x00A2F8)
        await ask.send(embed=embed)

    @info_group.command(name="emoji", aliases=["emojis"], description="List this server's emoji and stickers by how often they're used")
    async def info_emoji(self, ctx: commands.Context) -> None:
        ask = self._ask(ctx)
        if not self.on_in(ctx.guild.id):
            await ask.fail(ask.t("off"))
            return
        refusal = self._refusal(ask.member, ctx.channel, "emoji", 0, ask.lang)
        if refusal:
            await ask.fail(refusal)
            return
        guild = ctx.guild
        own = [(store.custom_key(item.id), item, "emoji") for item in guild.emojis] + [(store.sticker_key(item.id), item, "sticker") for item in getattr(guild, "stickers", [])]
        if not own:
            await ask.fail(ask.t("info_emoji_none"))
            return
        await ask.defer()
        await self.up_to_date()
        cfg = self.cfg(guild.id)
        lookback = dict(cfg["lookback"])
        zone = tf.zone(cfg["timezone"])
        period = tf.period_for(lookback, time.time(), cfg["interval"], zone)
        used = await reads.by_emoji(self.message_db, guild.id, views.filters_for(cfg, guild), period.first_slot(), period.end_slot())
        ranked = sorted(own, key=lambda entry: (-used.get(entry[0], 0), entry[1].name.lower()))
        lines = []
        for key, item, kind in ranked[:LINES_IN_A_REPLY]:
            count = used.get(key, 0)
            shown = str(item) if kind == "emoji" else f"`{item.name}`"
            amount = words.amount(ask.lang, count, "use") if count else ask.t("unused")
            lines.append(f"{shown} `:{item.name}:` {amount}" if kind == "emoji" else f"{shown} {amount}")
        if len(ranked) > LINES_IN_A_REPLY:
            lines.append(ask.t("info_emoji_more", count=words.number(ask.lang, len(ranked) - LINES_IN_A_REPLY)))
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["id", "name", "kind", "uses", "animated", "available"])
        for key, item, kind in ranked:
            writer.writerow([item.id, as_text(item.name), kind, used.get(key, 0), int(bool(getattr(item, "animated", False))), int(bool(getattr(item, "available", True)))])
        embed = discord.Embed(title=ask.t("info_emoji_title"), description="\n".join(lines)[:4000], color=0x00A2F8)
        embed.set_footer(text=words.period_label(ask.lang, lookback))
        await ask.send(embed=embed, file=discord.File(io.BytesIO(out.getvalue().encode("utf-8")), filename="emoji.csv"))

    @info_group.command(name="id", description="Show the server's ID and the IDs of the people, channels and roles you mention")
    @app_commands.describe(mentions="People, channels or roles, as mentions or IDs")
    async def info_id(self, ctx: commands.Context, *, mentions: str = "") -> None:
        ask = self._ask(ctx)
        if not self.on_in(ctx.guild.id):
            await ask.fail(ask.t("off"))
            return
        refusal = self._refusal(ask.member, ctx.channel, "server", 0, ask.lang)
        if refusal:
            await ask.fail(refusal)
            return
        guild = ctx.guild
        lines = [f"**{ask.t('info_server_id')}** {discord.utils.escape_markdown(guild.name)}: `{guild.id}`"]
        seen: set[int] = set()
        for found in MENTION.finditer(mentions or ""):
            kind, number = found.group(1), int(found.group(2) or found.group(3))
            if number in seen:
                continue
            seen.add(number)
            if kind in ("@", "@!") or (kind is None and guild.get_member(number) is not None):
                person = guild.get_member(number)
                lines.append(f"<@{number}>{' ' + discord.utils.escape_markdown(person.display_name) if person else ''}: `{number}`")
            elif kind == "@&" or (kind is None and guild.get_role(number) is not None):
                lines.append(f"<@&{number}>: `{number}`")
            else:
                lines.append(f"<#{number}>: `{number}`")
        if len(lines) == 1:
            lines.append(ask.t("info_ids_none"))
        await ask.send("\n".join(lines)[:2000])

    @info_group.command(name="roles", description="Get a file of the members who hold any of these roles")
    @requires("manage_roles")
    @app_commands.describe(role="A role", role2="Another role", role3="Another role", role4="Another role", role5="Another role")
    async def info_roles(self, ctx: commands.Context, role: discord.Role, role2: Optional[discord.Role] = None, role3: Optional[discord.Role] = None,
                         role4: Optional[discord.Role] = None, role5: Optional[discord.Role] = None) -> None:
        ask = self._ask(ctx)
        if not self.on_in(ctx.guild.id):
            await ask.fail(ask.t("off"))
            return
        refusal = self._refusal(ask.member, ctx.channel, "server", 0, ask.lang)
        if refusal:
            await ask.fail(refusal)
            return
        picked = [item for item in dict.fromkeys((role, role2, role3, role4, role5)) if item is not None]
        wanted = {item.id for item in picked}
        holders = [member for member in ctx.guild.members if wanted & {held.id for held in member.roles}]
        names = ", ".join(f"<@&{item.id}>" for item in picked)
        if not holders:
            await ask.fail(ask.t("info_roles_none", roles=names))
            return
        await ask.defer()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["id", "username", "display_name", "nickname", "joined_at", "account_created", "bot", "roles"])
        for member in sorted(holders, key=lambda item: (getattr(item, "joined_at", None) is None, getattr(item, "joined_at", None) or 0, item.id)):
            joined = getattr(member, "joined_at", None)
            writer.writerow([member.id, as_text(member.name), as_text(getattr(member, "global_name", None)), as_text(getattr(member, "nick", None)),
                             joined.isoformat() if joined else "", member.created_at.isoformat(), int(member.bot),
                             ";".join(str(held.id) for held in member.roles if held.id in wanted)])
        data = out.getvalue().encode("utf-8")
        limit = getattr(ctx.guild, "filesize_limit", 10 * 1024 * 1024) or 10 * 1024 * 1024
        if len(data) > limit * 0.95:
            packed = io.BytesIO()
            with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("roles.csv", data)
            file = discord.File(io.BytesIO(packed.getvalue()), filename="roles.zip")
        else:
            file = discord.File(io.BytesIO(data), filename="roles.csv")
        key = "info_roles_one" if len(holders) == 1 else "info_roles_done"
        await ask.send(ask.t(key, count=words.number(ask.lang, len(holders)), roles=names)[:2000], file=file)

    # ---- the person's own choices ----

    async def privacy_command(self, ctx: commands.Context) -> None:
        lang = self.language_of(ctx.author.id, getattr(getattr(ctx, "interaction", None), "locale", None), ctx.guild)
        row = self._choice(ctx.author.id)
        on = bool(row.get("private"))
        view = discord.ui.View(timeout=None)
        if on:
            view.add_item(PrivacyButton("off", ctx.author.id, label=words.t(lang, "privacy_turn_off"), style=discord.ButtonStyle.primary))
        else:
            view.add_item(PrivacyButton("on", ctx.author.id, label=words.t(lang, "privacy_turn_on"), style=discord.ButtonStyle.danger))
        view.add_item(PrivacyButton("no", ctx.author.id, label=words.t(lang, "cancel")))
        embed = discord.Embed(title=words.t(lang, "privacy_title"), description=words.t(lang, "privacy_on_text" if on else "privacy_off_text"), color=0x00A2F8)
        if getattr(ctx, "interaction", None) is None:
            try:
                await ctx.author.send(embed=embed, view=view)
                await ctx.ok(words.t(lang, "sent_by_dm"))
            except (discord.HTTPException, AttributeError):
                await ctx.fail(words.t(lang, "dm_failed"))
            return
        await ctx.send(embed=embed, view=view, ephemeral=True)

    async def privacy_pressed(self, interaction: discord.Interaction, choice: str, user_id: int) -> None:
        lang = self.language_of(interaction.user.id, interaction.locale, interaction.guild)
        if interaction.user.id != user_id:
            await _answer(interaction, words.t(lang, "canceled"))
            return
        if choice == "no":
            try:
                await interaction.response.edit_message(content=words.t(lang, "canceled"), embed=None, view=None)
            except discord.HTTPException:
                await _answer(interaction, words.t(lang, "canceled"))
            return
        try:
            await interaction.response.edit_message(content=words.t(lang, "privacy_working") if choice == "on" else None, embed=None, view=None)
        except discord.HTTPException as error:
            log.info("Could not answer the privacy button: %s", error)
        key, values = await self.set_privacy(user_id, choice == "on")
        try:
            await interaction.edit_original_response(content=words.t(lang, key, **values), embed=None, view=None)
        except discord.HTTPException:
            await _answer(interaction, words.t(lang, key, **values))

    async def set_privacy(self, user_id: int, on: bool, now: float | None = None) -> tuple[str, dict]:
        """Turns someone's privacy on or off, everywhere, at most once a day. On takes their name off everything recorded, which can't be
        undone: their counts are merged into no one's. Off puts their name on what's recorded from then on. Returns the reply's key in
        words.py and its values. The My data page calls this too."""
        now = time.time() if now is None else now
        row = self._choice(user_id)
        changed = row.get("private_changed_at")
        if bool(row.get("private")) == on:
            if on and (row.get("anonymized_at") or 0) < (changed or 0):
                # The move failed before. Asking again finishes it now rather than at the next restart.
                return ("privacy_done_on", {}) if await self._anonymize(user_id) else ("privacy_failed", {})
            return ("privacy_done_on" if on else "privacy_done_off"), {}
        if changed and now - changed < PRIVACY_GAP:
            return "privacy_wait", {"changed": int(changed), "next": int(changed + PRIVACY_GAP)}
        await self._save_choice(user_id, private=1 if on else 0, private_changed_at=now)
        if not on:
            self.private.discard(user_id)
            self._privacy_changed()
            return "privacy_done_off", {}
        self.private.add(user_id)
        self._privacy_changed()  # from here every card counts them as no one, whether or not the move below succeeds
        if not await self._anonymize(user_id):
            return "privacy_failed", {}
        return "privacy_done_on", {}

    async def _anonymize(self, user_id: int) -> bool:
        busy = self._cards_state()["privacy_busy"]
        if user_id in busy:
            return True
        busy.add(user_id)
        try:
            await self.up_to_date()  # counts taken under their name before now go to the database first, then move with the rest
            await store.anonymize(self.message_db, user_id)
            await self._save_choice(user_id, anonymized_at=time.time())
            self._privacy_changed()  # a card drawn while the counts moved may have counted some twice, so it isn't kept
            return True
        except Exception:
            log.exception("Could not take %s's name off their statistics. A restart tries again.", user_id)
            return False
        finally:
            busy.discard(user_id)

    async def _finish_privacy(self, user_ids: list[int]) -> None:
        for user_id in user_ids:
            await self._anonymize(user_id)

    async def language_command(self, ctx: commands.Context, language: str | None) -> None:
        interaction = getattr(ctx, "interaction", None)
        current = self.language_of(ctx.author.id, getattr(interaction, "locale", None), ctx.guild)
        if language is None:
            await ctx.ok(words.t(current, "language_now", name=words.NAMES[current]))
            return
        cleaned = language.strip()
        if cleaned.lower() in ("auto", "discord", "default", "follow"):
            await self.set_language(ctx.author.id, None)
            lang = self.language_of(ctx.author.id, getattr(interaction, "locale", None), ctx.guild)
            await ctx.ok(words.t(lang, "language_auto"))
            return
        picked = words.pick(cleaned) if cleaned.lower() in {code.lower() for code in words.LANGUAGES} | {"en", "es", "fr", "nl", "de"} else None
        if picked is None:
            await ctx.fail(", ".join(f"`{code}`" for code in ("auto", *words.LANGUAGES)))
            return
        await self.set_language(ctx.author.id, picked)
        await ctx.ok(words.t(picked, "language_set", name=words.NAMES[picked]))

    async def private_replies_command(self, ctx: commands.Context, setting: str | None) -> None:
        lang = self.language_of(ctx.author.id, getattr(getattr(ctx, "interaction", None), "locale", None), ctx.guild)
        if setting is None:
            await ctx.ok(words.t(lang, "private_replies_now_on" if self.replies_private(ctx.author.id) else "private_replies_now_off"))
            return
        on = setting.strip().lower() in ("on", "yes", "true", "1")
        await self.set_private_replies(ctx.author.id, on)
        await ctx.ok(words.t(lang, "private_replies_on" if on else "private_replies_off"))

    # ---- answers that name the server's settings ----

    async def _to_asker(self, ctx: Any, lang: str, content: str | None = None, *, embed: Any = None, file: Any = None) -> None:
        """An answer that names the server's settings, channels or people, for the person who asked alone. A slash command answers where
        only they see it, past any redirect the server's rules set, which would post it for everyone. A typed reply can't be private in a
        channel, so it goes by DM, with a short pointer where they typed. A command the server's rules mute answers nowhere."""
        kwargs: dict[str, Any] = {"allowed_mentions": NO_PINGS}
        if embed is not None:
            kwargs["embed"] = embed
        if file is not None:
            kwargs["file"] = file
        if getattr(ctx, "interaction", None) is not None:
            if isinstance(ctx, commands.Context):
                await commands.Context.send(ctx, content, ephemeral=True, **kwargs)
            else:
                await ctx.send(content, ephemeral=True, **kwargs)
            return
        decision = getattr(ctx, "decision", None)
        if decision is not None and getattr(decision, "muted", False):
            return
        try:
            await ctx.author.send(content, **kwargs)
        except (discord.HTTPException, AttributeError):
            await ctx.fail(words.t(lang, "dm_closed"))
            return
        await ctx.ok(words.t(lang, "sent_by_dm"))

    @staticmethod
    def _listed_channel(channel: Any) -> tuple[int, bool]:
        """The channel a list keeps for this one, and whether it was a thread. Statistics count a thread's messages under its channel and
        read a command in a thread as one in its channel, so a thread on a list would match nothing: its channel goes on instead."""
        if views._is_thread(channel):
            parent_id = getattr(channel, "parent_id", None) or getattr(getattr(channel, "parent", None), "id", None)
            if parent_id:
                return parent_id, True
        return channel.id, False

    # ---- /stats_filter ----

    async def _save_settings(self, ctx: commands.Context, form: dict, action: str, lang: str) -> bool:
        try:
            await self.bot.settings.save(ctx.guild.id, self.key, form, actor=self.bot.actor(ctx.author), action=action)
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values()), words.t(lang, "render_failed")))
            return False
        return True

    async def _list_change(self, ctx: commands.Context, field: str, item_id: Any, adding: bool, item_text: str, list_key: str, limit: int,
                           note: str = "") -> None:
        lang = self._ask(ctx).lang
        listed = list(self.cfg(ctx.guild.id)[field])
        list_name = words.t(lang, list_key)
        tail = f" {note}" if note else ""
        if adding:
            if item_id in listed:
                await self._to_asker(ctx, lang, f"\N{CROSS MARK} {words.t(lang, 'filter_already', item=item_text, list=list_name)}{tail}")
                return
            if len(listed) >= limit:
                await ctx.fail(words.t(lang, "filter_full", list=list_name, limit=words.number(lang, limit)))
                return
            listed.append(item_id)
        else:
            if item_id not in listed:
                await self._to_asker(ctx, lang, f"\N{CROSS MARK} {words.t(lang, 'filter_missing', item=item_text, list=list_name)}{tail}")
                return
            listed.remove(item_id)
        if await self._save_settings(ctx, {field: listed}, f"Statistics filter: {field}", lang):
            done = words.t(lang, "filter_added" if adding else "filter_removed", item=item_text, list=list_name)
            await self._to_asker(ctx, lang, f"\N{WHITE HEAVY CHECK MARK} {done}{tail}")

    async def _mode_change(self, ctx: commands.Context, field: str, mode: str, key: str) -> None:
        lang = self._ask(ctx).lang
        if await self._save_settings(ctx, {field: mode}, f"Statistics filter: {field}", lang):
            await self._to_asker(ctx, lang, f"\N{WHITE HEAVY CHECK MARK} {words.t(lang, f'{key}_{mode}')}")

    @commands.hybrid_group(name="stats_filter", description="Choose which channels, members, roles and emoji statistics views show")
    @commands.guild_only()
    @requires("manage_guild")
    async def filter_group(self, ctx: commands.Context) -> None:
        await self.settings_show(ctx)

    @filter_group.group(name="channels", description="Choose which channels statistics views show")
    @requires("manage_guild")
    async def filter_channels(self, ctx: commands.Context) -> None:
        await self.settings_show(ctx)

    @filter_channels.command(name="add", description="Put a channel or category on the channel filter")
    @requires("manage_guild")
    @app_commands.describe(channel="The channel or category")
    async def filter_channels_add(self, ctx: commands.Context, channel: Union[discord.abc.GuildChannel, discord.Thread]) -> None:
        channel_id, threaded = self._listed_channel(channel)
        note = words.t(self._ask(ctx).lang, "thread_means_channel", channel=f"<#{channel_id}>") if threaded else ""
        await self._list_change(ctx, "channels", channel_id, True, f"<#{channel_id}>", "list_channels", 500, note)

    @filter_channels.command(name="remove", description="Take a channel or category off the channel filter")
    @requires("manage_guild")
    @app_commands.describe(channel="The channel or category")
    async def filter_channels_remove(self, ctx: commands.Context, channel: Union[discord.abc.GuildChannel, discord.Thread]) -> None:
        channel_id, threaded = self._listed_channel(channel)
        note = words.t(self._ask(ctx).lang, "thread_means_channel", channel=f"<#{channel_id}>") if threaded else ""
        await self._list_change(ctx, "channels", channel_id, False, f"<#{channel_id}>", "list_channels", 500, note)

    @filter_channels.command(name="mode", description="Leave the listed channels out, or show only them")
    @requires("manage_guild")
    @app_commands.describe(mode="block leaves the listed channels out. allow shows only them")
    async def filter_channels_mode(self, ctx: commands.Context, mode: Literal["block", "allow"]) -> None:
        await self._mode_change(ctx, "channel_mode", mode, "mode_channels")

    @filter_group.group(name="members", description="Choose which members statistics views show")
    @requires("manage_guild")
    async def filter_members(self, ctx: commands.Context) -> None:
        await self.settings_show(ctx)

    @filter_members.command(name="add", description="Put a member on the member filter")
    @requires("manage_guild")
    @app_commands.describe(member="The member, or someone who left, by ID")
    async def filter_members_add(self, ctx: commands.Context, member: discord.User) -> None:
        await self._list_change(ctx, "members", member.id, True, f"<@{member.id}>", "list_members", 1000)

    @filter_members.command(name="remove", description="Take a member off the member filter")
    @requires("manage_guild")
    @app_commands.describe(member="The member, or someone who left, by ID")
    async def filter_members_remove(self, ctx: commands.Context, member: discord.User) -> None:
        await self._list_change(ctx, "members", member.id, False, f"<@{member.id}>", "list_members", 1000)

    @filter_members.command(name="mode", description="Leave the listed members out, or show only them")
    @requires("manage_guild")
    @app_commands.describe(mode="block leaves the listed members out. allow shows only them")
    async def filter_members_mode(self, ctx: commands.Context, mode: Literal["block", "allow"]) -> None:
        await self._mode_change(ctx, "member_mode", mode, "mode_members")

    @filter_group.group(name="roles", description="Show only members with some roles, or leave out members with others")
    @requires("manage_guild")
    async def filter_roles(self, ctx: commands.Context) -> None:
        await self.settings_show(ctx)

    @filter_roles.command(name="allow", description="Add or remove a role members need to show in statistics views")
    @requires("manage_guild")
    @app_commands.describe(action="add or remove", role="The role")
    async def filter_roles_allow(self, ctx: commands.Context, action: Literal["add", "remove"], role: discord.Role) -> None:
        await self._list_change(ctx, "roles_allow", role.id, action == "add", f"<@&{role.id}>", "list_roles_allow", 250)

    @filter_roles.command(name="block", description="Add or remove a role whose members statistics views leave out")
    @requires("manage_guild")
    @app_commands.describe(action="add or remove", role="The role")
    async def filter_roles_block(self, ctx: commands.Context, action: Literal["add", "remove"], role: discord.Role) -> None:
        await self._list_change(ctx, "roles_block", role.id, action == "add", f"<@&{role.id}>", "list_roles_block", 250)

    @filter_group.group(name="emoji", description="Choose which emoji statistics views show")
    @requires("manage_guild")
    async def filter_emoji(self, ctx: commands.Context) -> None:
        await self.settings_show(ctx)

    @filter_emoji.command(name="add", description="Put an emoji on the emoji filter")
    @requires("manage_guild")
    @app_commands.describe(emoji="A standard emoji, one of this server's emoji, or its ID")
    async def filter_emoji_add(self, ctx: commands.Context, emoji: str) -> None:
        await self._emoji_change(ctx, emoji, True)

    @filter_emoji.command(name="remove", description="Take an emoji off the emoji filter")
    @requires("manage_guild")
    @app_commands.describe(emoji="A standard emoji, one of this server's emoji, or its ID")
    async def filter_emoji_remove(self, ctx: commands.Context, emoji: str) -> None:
        await self._emoji_change(ctx, emoji, False)

    @filter_emoji.command(name="mode", description="Leave the listed emoji out, or show only them")
    @requires("manage_guild")
    @app_commands.describe(mode="block leaves the listed emoji out. allow shows only them")
    async def filter_emoji_mode(self, ctx: commands.Context, mode: Literal["block", "allow"]) -> None:
        await self._mode_change(ctx, "emoji_mode", mode, "mode_emoji")

    async def _emoji_change(self, ctx: commands.Context, text: str, adding: bool) -> None:
        lang = self._ask(ctx).lang
        key = emoji_key_of(ctx.guild, text)
        if key is None:
            await ctx.fail(words.t(lang, "bad_emoji"))
            return
        shown = emoji_text.display(key[2:]) if key.startswith("u:") else f"<:e:{key[2:]}>"
        await self._list_change(ctx, "emoji", key, adding, shown, "list_emoji", 25)

    # ---- /stats_settings ----

    @commands.hybrid_group(name="stats_settings", fallback="show", description="Show and change how statistics read time and who can ask")
    @commands.guild_only()
    @requires("manage_guild")
    async def settings_group(self, ctx: commands.Context) -> None:
        await self.settings_show(ctx)

    async def settings_show(self, ctx: commands.Context) -> None:
        ask = self._ask(ctx)
        await self._to_asker(ctx, ask.lang, embed=self._settings_embed(ctx.guild, ask.lang, ask.member))

    async def _show_saved(self, ctx: commands.Context, lang: str, note: str = "") -> None:
        text = words.t(lang, "saved") + (f" {note}" if note else "")
        await self._to_asker(ctx, lang, text, embed=self._settings_embed(ctx.guild, lang, self._ask(ctx).member))

    def _settings_embed(self, guild: Any, lang: str, viewer: Any = None) -> discord.Embed:
        """Every statistics setting, for `viewer`. A channel they can't see is counted, not named."""
        cfg = self.cfg(guild.id)
        zone = tf.zone(cfg["timezone"])
        find = getattr(guild, "get_channel_or_thread", None) or guild.get_channel

        def items_of(ids: list, mention: str) -> list[str]:
            if mention != "<#{}>":
                return [mention.format(item) for item in ids]
            shown: list[str] = []
            hidden = 0
            for channel_id in ids:
                found = find(channel_id)
                if found is not None and viewer is not None and not access.can_view(viewer, found):
                    hidden += 1
                else:
                    shown.append(f"<#{channel_id}>")
            if hidden:
                shown.append(words.t(lang, "hidden_from_you", amount=words.amount(lang, hidden, "channel")))
            return shown

        def listed(ids: list, mention: str, mode: str | None = None) -> str:
            if not ids:
                return words.t(lang, "everything") if mode else words.t(lang, "none")
            items = words.joined(lang, items_of(ids, mention), 8)
            if mode is None:
                return items
            return words.t(lang, "only_list" if mode == "allow" else "all_but_list", items=items)

        states = cfg["voice_states"]
        state_text = words.t(lang, "everything") if len(states) == len(store.VOICE_STATES) else ", ".join(words.t(lang, f"state_{name}") for name in states)
        emoji_items = [emoji_text.display(key[2:]) if key.startswith("u:") else f"<:e:{key[2:]}>" for key in cfg["emoji"]]
        time_lines = [
            words.t(lang, "set_zone", value=f"{cfg['timezone']} ({tf.zone_label(zone, time.time()).split(' ')[0]})"),
            words.t(lang, "set_lookback", value=words.period_label(lang, dict(cfg["lookback"]))),
            words.t(lang, "set_interval", value=words.t(lang, f"interval_{cfg['interval']}")),
            words.t(lang, "set_unit", value=words.unit(lang, cfg["time_unit"], 2)),
        ]
        view_lines = [
            words.t(lang, "set_include", value=words.t(lang, f"include_{cfg['include']}")),
            words.t(lang, "set_states", value=state_text),
            words.t(lang, "set_channels", value=listed(cfg["channels"], "<#{}>", cfg["channel_mode"])),
            words.t(lang, "set_voice_channels", value=words.t(lang, "same_as_text") if cfg["voice_mode"] == "same" else listed(cfg["voice_channels"], "<#{}>", cfg["voice_mode"])),
            words.t(lang, "set_members", value=listed(cfg["members"], "<@{}>", cfg["member_mode"])),
            words.t(lang, "set_roles_allow", value=listed(cfg["roles_allow"], "<@&{}>")),
            words.t(lang, "set_roles_block", value=listed(cfg["roles_block"], "<@&{}>")),
            words.t(lang, "set_emoji", value=(words.t(lang, "only_list" if cfg["emoji_mode"] == "allow" else "all_but_list", items=" ".join(emoji_items))
                                              if emoji_items else words.t(lang, "everything"))),
        ]
        access_words = {"all": "access_all", "restricted": "access_restricted_word", "self": "access_self_word", "admins": "access_admins_word"}
        access_lines = [
            words.t(lang, "set_access", value=words.t(lang, access_words[cfg["command_access"]])),
            words.t(lang, "set_command_channels", value=listed(cfg["command_channels"], "<#{}>", cfg["command_channel_mode"])),
            words.t(lang, "set_override", value=listed(cfg["override_roles"], "<@&{}>")),
        ]
        recorded_lines = [
            words.t(lang, "set_activities", value=words.t(lang, "on" if cfg["track_activities"] else "off_word")),
            words.t(lang, "set_verify", value=words.t(lang, "on" if cfg["verify_activities"] else "off_word")),
            words.t(lang, "set_untracked", value=listed(cfg["untracked_channels"], "<#{}>")),
        ]

        def minutes(value: int) -> str:
            return words.amount(lang, value, "minutes") if value else words.t(lang, "no_minimum")

        invite_lines = [
            words.t(lang, "set_invite_account", value=minutes(cfg["invite_min_account_minutes"])),
            words.t(lang, "set_invite_join", value=minutes(cfg["invite_min_join_minutes"])),
            words.t(lang, "set_invite_roles", match=words.t(lang, f"match_{cfg['invite_required_match']}"), value=listed(cfg["invite_required_roles"], "<@&{}>")),
        ]
        embed = discord.Embed(title=words.t(lang, "settings_title"), color=0x00A2F8)
        for name, lines in (("settings_time", time_lines), ("settings_view", view_lines), ("settings_access", access_lines), ("settings_recorded", recorded_lines),
                            ("settings_invites", invite_lines)):
            embed.add_field(name=words.t(lang, name), value="\n".join(lines)[:1024], inline=False)
        if self.bot.public_url:
            embed.set_footer(text=words.t(lang, "settings_more", link=f"{self.bot.public_url}/dashboard/{guild.id}/statssettings")[:2048])
        return embed

    @settings_group.command(name="time", description="Change the time zone, the period, the chart interval and the voice time unit")
    @requires("manage_guild")
    @app_commands.describe(timezone="A time zone such as America/Los_Angeles", lookback="Days (90), a start date (2025-03-01), a timestamp, or all",
                           interval="How long each point of a chart covers", unit="How voice and activity time is shown")
    async def settings_time(self, ctx: commands.Context, timezone: Optional[str] = None, lookback: Optional[str] = None,
                            interval: Optional[Literal["hour", "day", "week", "month"]] = None, unit: Optional[Literal["minutes", "hours", "days"]] = None) -> None:
        lang = self._ask(ctx).lang
        form: dict[str, Any] = {}
        if timezone is not None:
            name = next((zone for zone in tf.zone_names() if zone.lower() == timezone.strip().lower()), None)
            if name is None:
                await ctx.fail(words.t(lang, "bad_zone"))
                return
            form["timezone"] = name
        if lookback is not None:
            zone = tf.zone(form.get("timezone") or self.cfg(ctx.guild.id)["timezone"])
            try:
                form["lookback"] = tf.parse_lookback(lookback, zone, time.time())
            except Invalid as error:
                await ctx.fail(_custom_error(lang, str(error)))
                return
            problem = start_problem(form["lookback"])
            if problem:
                await ctx.fail(words.t(lang, problem))
                return
        if interval is not None:
            form["interval"] = interval
        if unit is not None:
            form["time_unit"] = unit
        if not form:
            await ctx.fail(words.t(lang, "nothing_to_change"))
            return
        if await self._save_settings(ctx, form, "Statistics time settings", lang):
            await self._show_saved(ctx, lang)

    @settings_time.autocomplete("timezone")
    async def settings_zone_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=name, value=name) for name in zone_matches(current)]

    @settings_group.command(name="view", description="Choose whether views count people, bots or both, and which voice states")
    @requires("manage_guild")
    @app_commands.describe(members="people, bots, or both", voice_states="all, or a list such as talking, self_muted")
    async def settings_view(self, ctx: commands.Context, members: Optional[Literal["humans", "bots", "both"]] = None, voice_states: Optional[str] = None) -> None:
        lang = self._ask(ctx).lang
        form: dict[str, Any] = {}
        if members is not None:
            form["include"] = members
        if voice_states is not None:
            typed = [part.strip().lower().replace(" ", "_") for part in re.split(r"[,\s]+", voice_states) if part.strip()]
            if typed == ["all"]:
                form["voice_states"] = sorted(store.VOICE_STATES)
            elif typed and all(part in store.VOICE_STATES for part in typed):
                form["voice_states"] = sorted(set(typed))
            else:
                await ctx.fail(words.t(lang, "bad_states", states=", ".join(sorted(store.VOICE_STATES))))
                return
        if not form:
            await ctx.fail(words.t(lang, "nothing_to_change"))
            return
        if await self._save_settings(ctx, form, "Statistics view settings", lang):
            await self._show_saved(ctx, lang)

    @settings_group.command(name="access", description="Choose who can use statistics commands, in which channels")
    @requires("manage_guild")
    @app_commands.describe(level="all, restricted (only about themselves), self (only /me-style commands) or admins", channel_mode="block or allow the listed channels",
                           add_channel="A channel to put on the command channel list", remove_channel="A channel to take off it",
                           add_role="A role that skips these rules. Owner or Administrator only", remove_role="A role to take off that list")
    async def settings_access(self, ctx: commands.Context, level: Optional[Literal["all", "restricted", "self", "admins"]] = None,
                              channel_mode: Optional[Literal["block", "allow"]] = None, add_channel: Optional[Union[discord.abc.GuildChannel, discord.Thread]] = None,
                              remove_channel: Optional[Union[discord.abc.GuildChannel, discord.Thread]] = None, add_role: Optional[discord.Role] = None,
                              remove_role: Optional[discord.Role] = None) -> None:
        lang = self._ask(ctx).lang
        cfg = self.cfg(ctx.guild.id)
        form: dict[str, Any] = {}
        if level is not None:
            form["command_access"] = level
        if channel_mode is not None:
            form["command_channel_mode"] = channel_mode
        notes: list[str] = []
        if add_channel is not None or remove_channel is not None:
            listed = list(cfg["command_channels"])
            for channel, adding in ((add_channel, True), (remove_channel, False)):
                if channel is None:
                    continue
                channel_id, threaded = self._listed_channel(channel)
                if threaded:
                    notes.append(words.t(lang, "thread_means_channel", channel=f"<#{channel_id}>"))
                if adding and channel_id not in listed:
                    listed.append(channel_id)
                elif not adding and channel_id in listed:
                    listed.remove(channel_id)
            form["command_channels"] = listed
        if add_role is not None or remove_role is not None:
            if not access.is_admin(ctx.author):
                await ctx.fail(words.t(lang, "only_admins_change"))
                return
            roles = list(cfg["override_roles"])
            if add_role is not None and add_role.id not in roles:
                if add_role.is_default():
                    await ctx.fail(access.NOT_EVERYONE)
                    return
                roles.append(add_role.id)
            if remove_role is not None and remove_role.id in roles:
                roles.remove(remove_role.id)
            form["override_roles"] = roles
        if not form:
            await ctx.fail(words.t(lang, "nothing_to_change"))
            return
        if await self._save_settings(ctx, form, "Statistics access settings", lang):
            await self._show_saved(ctx, lang, " ".join(notes))

    @settings_group.command(name="invites", description="Choose which joins through invites count")
    @requires("manage_guild")
    @app_commands.describe(account_minutes="How old an account must be when it joins, in minutes. 0 for any age",
                           stay_minutes="How long a member must stay before their join counts, in minutes. 0 for at once",
                           add_role="A role a join needs before it counts", remove_role="A role to take off that list",
                           match="Whether a join needs any of the roles or all of them")
    async def settings_invites(self, ctx: commands.Context, account_minutes: Optional[commands.Range[int, 0, 5_256_000]] = None,
                               stay_minutes: Optional[commands.Range[int, 0, 5_256_000]] = None, add_role: Optional[discord.Role] = None,
                               remove_role: Optional[discord.Role] = None, match: Optional[Literal["any", "all"]] = None) -> None:
        lang = self._ask(ctx).lang
        cfg = self.cfg(ctx.guild.id)
        form: dict[str, Any] = {}
        if account_minutes is not None:
            form["invite_min_account_minutes"] = account_minutes
        if stay_minutes is not None:
            form["invite_min_join_minutes"] = stay_minutes
        if add_role is not None or remove_role is not None:
            roles = list(cfg["invite_required_roles"])
            if add_role is not None and add_role.id not in roles:
                roles.append(add_role.id)
            if remove_role is not None and remove_role.id in roles:
                roles.remove(remove_role.id)
            form["invite_required_roles"] = roles
        if match is not None:
            form["invite_required_match"] = match
        if not form:
            await ctx.fail(words.t(lang, "nothing_to_change"))
            return
        if await self._save_settings(ctx, form, "Statistics invite settings", lang):
            await self._show_saved(ctx, lang)

    @settings_group.command(name="tracking", description="Choose what statistics record: activities, and channels left out")
    @requires("manage_guild")
    @app_commands.describe(activities="Record the games and apps members use", verify="Count only activities with an app ID Discord can check",
                           leave_out="A channel or category to stop recording", record_again="A channel or category to record again")
    async def settings_tracking(self, ctx: commands.Context, activities: Optional[Literal["on", "off"]] = None, verify: Optional[Literal["on", "off"]] = None,
                                leave_out: Optional[Union[discord.abc.GuildChannel, discord.Thread]] = None,
                                record_again: Optional[Union[discord.abc.GuildChannel, discord.Thread]] = None) -> None:
        lang = self._ask(ctx).lang
        cfg = self.cfg(ctx.guild.id)
        form: dict[str, Any] = {}
        if activities is not None:
            form["track_activities"] = activities == "on"
        if verify is not None:
            form["verify_activities"] = verify == "on"
        if leave_out is not None or record_again is not None:
            listed = list(cfg["untracked_channels"])
            if leave_out is not None and leave_out.id not in listed:
                listed.append(leave_out.id)
            if record_again is not None and record_again.id in listed:
                listed.remove(record_again.id)
            form["untracked_channels"] = listed
        if not form:
            await ctx.fail(words.t(lang, "nothing_to_change"))
            return
        if await self._save_settings(ctx, form, "Statistics tracking settings", lang):
            await self._show_saved(ctx, lang)

    # ---- /stats_check ----

    @commands.hybrid_command(name="stats_check", description="Check what statistics can't see or do here, feature by feature")
    @commands.guild_only()
    @requires("manage_guild")
    async def check_report(self, ctx: commands.Context) -> None:
        ask = self._ask(ctx)
        await ctx.defer(ephemeral=True)
        report = self.check_lines(ctx.guild, ctx.channel, ask.lang, clashes=await self.tag_clashes(ctx.guild.id))
        embed = discord.Embed(title=ask.t("check_title"), color=0xF86C6B if report["problems"] else 0x4DBD74)
        if report["problems"]:
            embed.add_field(name=ask.t("check_problems"), value="\n".join(f"\N{WARNING SIGN}\N{VARIATION SELECTOR-16} {line}" for line in report["problems"])[:1024],
                            inline=False)
        embed.add_field(name=ask.t("check_fine"), value="\n".join(f"\N{WHITE HEAVY CHECK MARK} {line}" for line in report["fine"] or [ask.t("check_ok")])[:1024],
                        inline=False)
        file = None
        if report["unseen"]:
            seen = [channel for channel in report["unseen"] if ask.member is not None and access.can_view(ask.member, channel)]
            lines = [f"{channel.id}\t{as_text(channel.name)}\t{channel.type.name}" for channel in seen]
            if len(seen) < len(report["unseen"]):
                lines.append(ask.t("hidden_from_you", amount=words.amount(ask.lang, len(report["unseen"]) - len(seen), "channel")))
            file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename="channels-not-seen.txt")
        await self._to_asker(ctx, ask.lang, embed=embed, file=file)

    async def tag_clashes(self, guild_id: int) -> list[str]:
        """This server's tags that typing a statistics command's name now reaches instead, so staff can rename them or use /tag get."""
        tags = self.bot.module("tags")
        if tags is None:
            return []
        return [name for name in await tags.shadowed(guild_id) if getattr(self.bot.typed_command(name, guild_id), "cog", None) is self]

    def check_lines(self, guild: Any, channel: Any, lang: str, clashes: list[str] | None = None) -> dict:
        """What statistics can't see or do in this server, feature by feature, and what works."""
        cfg = self.cfg(guild.id)
        me = guild.me
        server = me.guild_permissions
        intents = self.bot.intents
        problems: list[str] = []
        fine: list[str] = []
        if not cfg["enabled"]:
            problems.append(words.t(lang, "check_off"))
        if not intents.presences:
            problems.append(words.t(lang, "check_presence"))
        elif not cfg["track_activities"]:
            problems.append(words.t(lang, "check_activities_off"))
        if not intents.members:
            problems.append(words.t(lang, "check_members_intent"))
        if not intents.message_content:
            problems.append(words.t(lang, "check_content_intent"))
        if not server.manage_guild:
            problems.append(words.t(lang, "check_invites"))
        if channel is not None and hasattr(channel, "permissions_for"):
            here = channel.permissions_for(me)
            missing = [name for name, label in (("send_messages", "Send Messages"), ("embed_links", "Embed Links"), ("attach_files", "Attach Files"),
                                                ("read_message_history", "Read Message History")) if not getattr(here, name)]
            if missing:
                problems.append(words.t(lang, "check_reply_here", missing=", ".join(label for name, label in (("send_messages", "Send Messages"), ("embed_links", "Embed Links"),
                                                                                                                ("attach_files", "Attach Files"), ("read_message_history", "Read Message History")) if name in missing)))
        refusal = self._channel_refusal(guild.id, channel, lang)
        if refusal:
            problems.append(words.t(lang, "check_commands_here", why=refusal))
        if clashes:
            problems.append(words.t(lang, "check_tag_clash", names=", ".join(f"`{name}`" for name in clashes)))
        if not server.manage_threads:
            problems.append(words.t(lang, "check_threads"))
        if not server.manage_roles:
            problems.append(words.t(lang, "check_roles"))
        if not (server.manage_channels and server.connect):
            problems.append(words.t(lang, "check_counters"))
        untracked = set(cfg["untracked_channels"])
        countable = [item for item in guild.channels if getattr(item, "type", None) not in (discord.ChannelType.category,)
                     and getattr(getattr(item, "type", None), "value", None) in views.CHANNEL_KINDS]
        unseen = [item for item in countable if not item.permissions_for(me).view_channel and item.id not in untracked and getattr(item, "category_id", None) not in untracked]
        if unseen:
            problems.append(words.t(lang, "check_unseen", count=words.number(lang, len(unseen))))
        if untracked:
            fine.append(words.t(lang, "check_untracked", count=words.number(lang, len(untracked))))
        fine.append(words.t(lang, "check_messages_ok", count=words.number(lang, len(countable) - len(unseen))))
        return {"problems": problems, "fine": fine, "unseen": unseen}


def zone_matches(typed: str) -> list[str]:
    """Up to 25 time zones for what someone has typed so far: the common ones first when nothing is typed."""
    cleaned = (typed or "").strip().lower().replace(" ", "_")
    if not cleaned:
        return list(COMMON_ZONES)
    found = [name for name in tf.zone_names() if cleaned in name.lower()]
    found.sort(key=lambda name: (not name.lower().startswith(cleaned), not name.lower().rsplit("/", 1)[-1].startswith(cleaned), len(name), name))
    return found[:25]


def emoji_key_of(guild: Any, text: str) -> str | None:
    """An emoji as the statistics key it: one of this server's by mention or ID, or a standard emoji."""
    cleaned = (text or "").strip()
    found = CUSTOM_EMOJI.fullmatch(cleaned)
    number = int(found.group(1)) if found else int(cleaned) if cleaned.isdigit() and 15 <= len(cleaned) <= 21 else None
    if number is not None:
        own = guild.get_emoji(number) if hasattr(guild, "get_emoji") else next((item for item in guild.emojis if item.id == number), None)
        return store.custom_key(number) if own is not None else None
    standard = emoji_text.find(cleaned) or emoji_text.find(cleaned + emoji_text.SELECTOR)
    if len(standard) == 1:
        return store.unicode_key(next(iter(standard)))
    return None


def _custom_error(lang: str, sentence: str) -> str:
    """The reason a typed period was refused, in the person's language."""
    if "hasn't" in sentence:
        return words.t(lang, "custom_future")
    if "days" in sentence and "Pick from" in sentence:
        return words.t(lang, "custom_range")
    if "doesn't exist" in sentence:
        return words.t(lang, "custom_no_date")
    return words.t(lang, "custom_bad")


async def _info_slash_group(self: Any, ctx: Any) -> None:
    """/info's own function, which Discord never runs: a slash group answers only through its subcommands."""
