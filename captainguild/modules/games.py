"""World of Warcraft timers and game profile lookups. These commands join /games, which belongs to the fun module."""
from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
import math
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw

from ..core.module import Module
from ..core.outside import Outside, OutsideError, Reply

log = logging.getLogger(__name__)

BLUE = 0x00A2F8
NO_PINGS = discord.AllowedMentions.none()
UTC = datetime.timezone.utc
HOUR = 3600
DAY = 24 * HOUR
MAX_JSON_BYTES = 512 * 1024
LOOKUP_TIMEOUT = 10.0
NAME_MAX = 64
ABOUT_MAX = 300


def _utc(*parts: int) -> float:
    return datetime.datetime(*parts, tzinfo=UTC).timestamp()


@dataclass(frozen=True)
class Schedule:
    """Something in the game that repeats on a fixed cycle in each region, counted from one known start."""
    cycle: int
    active: int
    anchors: dict[str, float]

    def state(self, region: str, now: float) -> tuple[bool, float]:
        """Whether a window is open, and when that changes."""
        into = (now - self.anchors[region]) % self.cycle
        start = now - into
        return (True, start + self.active) if into < self.active else (False, start + self.cycle)

    def next_start(self, region: str, now: float) -> float:
        """When the next window opens. One already open doesn't count."""
        return now - (now - self.anchors[region]) % self.cycle + self.cycle


INCURSION = Schedule(19 * HOUR, 7 * HOUR, {"EU": _utc(2019, 1, 20, 16), "NA": _utc(2019, 1, 21, 1)})
INVASION = Schedule(18 * HOUR + 30 * 60, 6 * HOUR, {"EU": _utc(2017, 5, 26, 22, 30), "NA": _utc(2017, 5, 26, 12)})
RESET_HOURS = {"EU": 4, "NA": 15}  # hours of the day, UTC


def next_reset(hour: int, now: float) -> float:
    """The next time the clock reaches this hour, UTC. Exactly on the hour counts as gone."""
    return now + DAY - (now - hour * HOUR) % DAY


CLASSES = ("Death Knight", "Demon Hunter", "Druid", "Evoker", "Hunter", "Mage", "Monk", "Paladin", "Priest", "Rogue", "Shaman", "Warlock", "Warrior")
SPECS = (
    "Blood Death Knight", "Frost Death Knight", "Unholy Death Knight", "Havoc Demon Hunter", "Vengeance Demon Hunter", "Devourer Demon Hunter",
    "Balance Druid", "Feral Druid", "Guardian Druid", "Restoration Druid", "Augmentation Evoker", "Devastation Evoker", "Preservation Evoker",
    "Beast Mastery Hunter", "Marksmanship Hunter", "Survival Hunter", "Arcane Mage", "Fire Mage", "Frost Mage", "Brewmaster Monk", "Mistweaver Monk", "Windwalker Monk",
    "Holy Paladin", "Protection Paladin", "Retribution Paladin", "Discipline Priest", "Holy Priest", "Shadow Priest", "Assassination Rogue", "Outlaw Rogue", "Subtlety Rogue",
    "Elemental Shaman", "Enhancement Shaman", "Restoration Shaman", "Affliction Warlock", "Demonology Warlock", "Destruction Warlock", "Arms Warrior", "Fury Warrior", "Protection Warrior",
)

FORTNITE_STATS = "https://fortnite-api.com/v2/stats/br/v2"
ROBLOX_NAMES = "https://users.roblox.com/v1/usernames/users"
ROBLOX_USER = "https://users.roblox.com/v1/users/{id}"
ROBLOX_COUNTS = {
    "Friends": "https://friends.roblox.com/v1/users/{id}/friends/count",
    "Followers": "https://friends.roblox.com/v1/users/{id}/followers/count",
    "Following": "https://friends.roblox.com/v1/users/{id}/followings/count",
}
ROBLOX_HEADSHOT = "https://thumbnails.roblox.com/v1/users/avatar-headshot"
ROBLOX_PROFILE = "https://www.roblox.com/users/{id}/profile"
ROBLOX_NAME = re.compile(r"[A-Za-z0-9_]{1,32}")

_INVISIBLE = re.compile(r"[\u0000-\u0008\u000e-\u001f\u007f-\u009f\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufe00-\ufe0f\ufeff]")
_SPACES = re.compile(r"\s+")


def clean(value: Any, limit: int) -> str:
    """Text from another site, made safe to show: one line, no invisible characters, markdown escaped, and no longer than `limit`. Anything that isn't text comes back empty."""
    if not isinstance(value, str):
        return ""
    flat = _SPACES.sub(" ", _INVISIBLE.sub("", value)).strip()
    room = limit
    while room > 0:  # escaping adds characters, so cut until the escaped text fits
        shown = discord.utils.escape_markdown(flat[:room])
        if len(shown) <= limit and not shown.endswith("\\"):
            return shown
        room = min(room - 1, room * limit // max(len(shown), 1))
    return ""


def _number(value: Any) -> int | float | None:
    """A real number from another site. True counts as a number in Python and isn't one here."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _dig(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def draw_snow_globe(seed: int) -> bytes:
    """A snow globe on its stand, with the snow scattered by `seed`. Touches nothing but Pillow, so call it through asyncio.to_thread."""
    size, center, radius = 480, 240, 200
    picker = random.Random(seed)
    image = Image.new("RGB", (size, size), (16, 20, 26))
    draw = ImageDraw.Draw(image)
    draw.ellipse((center - radius, center - radius - 20, center + radius, center + radius - 20), fill=(34, 58, 79), outline=(150, 190, 220), width=6)
    draw.pieslice((center - radius + 3, center - radius - 17, center + radius - 3, center + radius - 23), 20, 160, fill=(232, 240, 246))
    for left, height in ((150, 110), (240, 150), (320, 90)):
        base = 300
        draw.polygon(((left - height // 3, base), (left + height // 3, base), (left, base - height)), fill=(30, 110, 90))
        draw.rectangle((left - 6, base, left + 6, base + 16), fill=(90, 62, 40))
    draw.polygon(((110, 480), (370, 480), (340, 408), (140, 408)), fill=(0, 162, 248))
    for _ in range(90):
        angle, reach = picker.uniform(0, math.tau), radius * 0.95 * math.sqrt(picker.random())
        x, y, flake = center + reach * math.cos(angle), center - 20 + reach * math.sin(angle), picker.uniform(1.5, 4.5)
        draw.ellipse((x - flake, y - flake, x + flake, y + flake), fill=(255, 255, 255))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class Games(Module):
    key = "games"
    category = "Games"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.outside = Outside()

    async def on_load(self) -> None:
        self._attach()

    async def cog_unload(self) -> None:
        await self.outside.close()

    def _attach(self) -> None:
        """/games belongs to the fun module, so these join that group once every module has loaded. Without the fun module there is no group to join."""
        parent = self.bot.get_command("games")
        if parent is None or parent.get_command("incursion") is not None:
            return
        for name, callback, description in (
            ("list", Games._list, "Show the games and game lookups this bot has"),
            ("play", Games._play, "Start a game"),
            ("incursion", Games._incursion, "Show the World of Warcraft incursion timers"),
            ("invasion", Games._invasion, "Show the World of Warcraft invasion timers"),
            ("reset", Games._reset, "Show how long until World of Warcraft resets in EU and NA"),
            ("pickmyclass", Games._pick_class, "Pick a random World of Warcraft class"),
            ("pickmyspec", Games._pick_spec, "Pick a random World of Warcraft spec"),
            ("roblox", Games._roblox, "Look up a public Roblox profile by user name"),
        ):
            command = commands.HybridCommand(callback, name=name, description=description)
            command.cog = self
            parent.add_command(command)
        fortnite = commands.HybridGroup(Games._fortnite_root, name="fortnite", description="Show a player's Fortnite stats")
        fortnite.cog = self
        for name, callback, description in (
            ("pc", Games._fortnite_pc, "Show the Fortnite stats of a player on PC"),
            ("playstation", Games._fortnite_playstation, "Show the Fortnite stats of a player on PlayStation"),
            ("xbox", Games._fortnite_xbox, "Show the Fortnite stats of a player on Xbox"),
        ):
            command = commands.HybridCommand(callback, name=name, description=description)
            command.cog = self
            fortnite.add_command(command)
        parent.add_command(fortnite)

    # ---- /games list and /games play ----

    async def _list(self, ctx: commands.Context) -> None:
        await ctx.send(
            "**Play:** `/games toproll`, `/games leaderboard`\n"
            "**Look up:** `/games fortnite`, `/games roblox`\n"
            "**World of Warcraft:** `/games incursion`, `/games invasion`, `/games reset`, `/games pickmyclass`, `/games pickmyspec`\n"
            "**Alerts:** `/games alerts`",
            ephemeral=True, allowed_mentions=NO_PINGS,
        )

    @app_commands.describe(game="Which game. TopRoll is the only one")
    async def _play(self, ctx: commands.Context, game: Literal["toproll"] = "toproll") -> None:
        command = self.bot.get_command("games toproll")
        await command.callback(command.cog, ctx)

    # ---- World of Warcraft ----

    @staticmethod
    def _windows(schedule: Schedule) -> str:
        now = time.time()
        lines = []
        for region in schedule.anchors:
            active, change = schedule.state(region, now)
            lines.append(f"**{region}:** on now, ends <t:{int(change)}:R>" if active else f"**{region}:** next one starts <t:{int(change)}:R>")
        return "\n".join(lines)

    async def _incursion(self, ctx: commands.Context) -> None:
        embed = discord.Embed(title="Incursions", description=self._windows(INCURSION), color=BLUE)
        embed.set_footer(text="Get a DM before the next one with /reminder incursion.")
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    async def _invasion(self, ctx: commands.Context) -> None:
        await ctx.send(embed=discord.Embed(title="Invasions", description=self._windows(INVASION), color=BLUE), allowed_mentions=NO_PINGS)

    async def _reset(self, ctx: commands.Context) -> None:
        now = time.time()
        lines = [f"**{region}:** <t:{int(next_reset(hour, now))}:R>" for region, hour in RESET_HOURS.items()]
        await ctx.send(embed=discord.Embed(title="Next reset", description="\n".join(lines), color=BLUE), allowed_mentions=NO_PINGS)

    async def _pick_class(self, ctx: commands.Context) -> None:
        await ctx.send(random.choice(CLASSES), allowed_mentions=NO_PINGS)

    async def _pick_spec(self, ctx: commands.Context) -> None:
        await ctx.send(random.choice(SPECS), allowed_mentions=NO_PINGS)

    # ---- Fortnite ----

    async def _fortnite_root(self, ctx: commands.Context) -> None:
        await ctx.fail("That needs a player's name. A platform can go before it: `pc`, `playstation` or `xbox`.")

    @commands.cooldown(2, 10, commands.BucketType.user)
    @app_commands.describe(name="The player's name")
    async def _fortnite_pc(self, ctx: commands.Context, *, name: str) -> None:
        await self._fortnite(ctx, "epic", name)

    @commands.cooldown(2, 10, commands.BucketType.user)
    @app_commands.describe(name="The player's name")
    async def _fortnite_playstation(self, ctx: commands.Context, *, name: str) -> None:
        await self._fortnite(ctx, "psn", name)

    @commands.cooldown(2, 10, commands.BucketType.user)
    @app_commands.describe(name="The player's name")
    async def _fortnite_xbox(self, ctx: commands.Context, *, name: str) -> None:
        await self._fortnite(ctx, "xbl", name)

    async def _fortnite(self, ctx: commands.Context, account_type: str, name: str) -> None:
        key = os.environ.get("FORTNITE_API_KEY", "").strip()
        if not key:
            await ctx.fail("The bot's host hasn't added a Fortnite API key.")
            return
        if len(name) > NAME_MAX:
            await ctx.fail(f"A player's name can be at most {NAME_MAX} characters.")
            return
        await ctx.defer()
        try:
            reply = await self.outside.get(FORTNITE_STATS, params={"name": name, "accountType": account_type, "timeWindow": "lifetime"}, headers={"Authorization": key}, max_bytes=MAX_JSON_BYTES, timeout=LOOKUP_TIMEOUT)
            refusal = {
                200: None,
                401: "Fortnite-API refused the key the bot's host added.",
                403: "Fortnite-API won't show that player's stats, which is what it does when they're private.",
                404: f"There's no player called {clean(name, NAME_MAX)} on that platform.",
            }.get(reply.status, "Fortnite-API didn't answer. Try again in a bit.")
            data = reply.json() if refusal is None else None
        except OutsideError as error:
            log.info("Fortnite lookup failed: %s", error)
            refusal, data = "Fortnite-API didn't answer. Try again in a bit.", None
        if refusal:
            await ctx.fail(refusal)
            return
        overall = _dig(data, "data", "stats", "all", "overall")
        if not isinstance(overall, dict):
            await ctx.fail(f"Fortnite-API has no stats for {clean(name, NAME_MAX)}.")
            return
        embed = discord.Embed(title=clean(_dig(data, "data", "account", "name"), NAME_MAX) or clean(name, NAME_MAX), color=BLUE)
        minutes = _number(overall.get("minutesPlayed"))
        rows = (
            ("Matches", _number(overall.get("matches")), "{:,}"), ("Wins", _number(overall.get("wins")), "{:,}"), ("Win rate", _number(overall.get("winRate")), "{:g}%"),
            ("Kills", _number(overall.get("kills")), "{:,}"), ("K/D", _number(overall.get("kd")), "{:.2f}"), ("Hours played", None if minutes is None else minutes // 60, "{:,.0f}"),
            ("Battle pass", _number(_dig(data, "data", "battlePass", "level")), "{:,}"),
        )
        for label, value, shape in rows:
            if value is not None:
                embed.add_field(name=label, value=shape.format(value))
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    # ---- Roblox ----

    async def _roblox_json(self, reply: Reply) -> Any:
        if reply.status != 200:
            raise OutsideError(f"Roblox answered {reply.status}.")
        return reply.json()

    async def _roblox_count(self, url: str) -> int | None:
        return _number(_dig(await self._roblox_json(await self.outside.get(url, max_bytes=MAX_JSON_BYTES, timeout=LOOKUP_TIMEOUT)), "count"))

    async def _roblox_headshot(self, user_id: int) -> str | None:
        data = await self._roblox_json(await self.outside.get(ROBLOX_HEADSHOT, params={"userIds": str(user_id), "size": "150x150", "format": "Png"}, max_bytes=MAX_JSON_BYTES, timeout=LOOKUP_TIMEOUT))
        rows = _dig(data, "data")
        url = rows[0].get("imageUrl") if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if not isinstance(url, str) or len(url) > 512:
            return None
        parts = urlsplit(url)
        return url if parts.scheme == "https" and (parts.hostname or "").endswith(".rbxcdn.com") else None

    @commands.cooldown(2, 10, commands.BucketType.user)
    @app_commands.describe(name="The Roblox user name")
    async def _roblox(self, ctx: commands.Context, *, name: str) -> None:
        name = name.strip()
        if not ROBLOX_NAME.fullmatch(name):
            await ctx.fail("A Roblox user name holds only letters, numbers and the _ character.")
            return
        await ctx.defer()
        try:
            found = await self._roblox_json(await self.outside.post(ROBLOX_NAMES, data=json.dumps({"usernames": [name], "excludeBannedUsers": True}), headers={"Content-Type": "application/json"}, max_bytes=MAX_JSON_BYTES, timeout=LOOKUP_TIMEOUT))
            rows = _dig(found, "data")
            if isinstance(rows, list) and not rows:
                await ctx.fail(f"There's no Roblox user called {clean(name, NAME_MAX)}.")
                return
            user_id = rows[0].get("id") if isinstance(rows, list) and isinstance(rows[0], dict) else None
            if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
                raise OutsideError("Roblox answered with no usable ID.")
            profile = await self._roblox_json(await self.outside.get(ROBLOX_USER.format(id=user_id), max_bytes=MAX_JSON_BYTES, timeout=LOOKUP_TIMEOUT))
        except OutsideError as error:
            log.info("Roblox lookup failed: %s", error)
            await ctx.fail("Roblox didn't answer. Try again in a bit.")
            return
        counts = await asyncio.gather(*(self._roblox_count(url.format(id=user_id)) for url in ROBLOX_COUNTS.values()), return_exceptions=True)
        headshot = (await asyncio.gather(self._roblox_headshot(user_id), return_exceptions=True))[0]
        display, username = clean(_dig(profile, "displayName"), NAME_MAX), clean(_dig(profile, "name"), NAME_MAX) or clean(name, NAME_MAX)
        title = f"{display} ({username})" if display and display != username else username
        embed = discord.Embed(title=title, url=ROBLOX_PROFILE.format(id=user_id), description=clean(_dig(profile, "description"), ABOUT_MAX) or None, color=BLUE)
        for label, count in zip(ROBLOX_COUNTS, counts):
            if isinstance(count, int):
                embed.add_field(name=label, value=f"{count:,}")
        joined = _dig(profile, "created")
        try:
            embed.add_field(name="Joined", value=f"<t:{int(datetime.datetime.fromisoformat(joined).timestamp())}:D>")
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        if isinstance(headshot, str):
            embed.set_thumbnail(url=headshot)
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)
