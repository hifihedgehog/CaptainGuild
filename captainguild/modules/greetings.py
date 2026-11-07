"""Welcome, farewell and ban messages, the join DM, and birthdays."""
from __future__ import annotations

import asyncio
import calendar
import collections
import datetime
import io
import logging
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, messages
from ..core.module import Module, requires
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import role_is_safe
from ..core.tagscript import Scope, render_message
from ..core.tagscript.objects import plain_name
from ..core.tagscript.preview import preview_scope
from ..core.validate import Field, Schema, ValidationError, boolean, integer, optional, snowflake
from ..web.api import ApiError

log = logging.getLogger(__name__)

TICK = "greetings.birthday"
ROLE_ENDS = "greetings.birthday_role"
DAY = 86400
# A member's own name can land in the combined announcement, and it must not reach the template language as a block.
_BRACES = str.maketrans({"{": "(", "}": ")"})
_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)
_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name})
_NUMBERS = re.compile(r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})")
_DAY_MONTH = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)")
_MONTH_DAY = re.compile(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?")
KINDS = ("welcome", "farewell", "ban", "dm", "birthday")
BURST_SECONDS = 60
LEAVING = "leaving"
MAX_LATE_DAYS = 7  # how far back a tick looks after an outage, since a birthday weeks ago is no longer news
COMBINED_PEOPLE = 50  # a message holds 2,000 characters, which is about 80 mentions, and a cut one wouldn't ping anybody


def parse_birthday(text: str) -> tuple[int, int] | None:
    """(month, day) from 25/12, 25-12, 25 December or Dec 25. Numbers are day first, and a date that doesn't exist gives None."""
    cleaned = " ".join((text or "").lower().replace(",", " ").split())
    month: int | None = None
    if match := _NUMBERS.fullmatch(cleaned):
        day, month = int(match.group(1)), int(match.group(2))
    elif match := _DAY_MONTH.fullmatch(cleaned):
        day, month = int(match.group(1)), _MONTHS.get(match.group(2))
    elif match := _MONTH_DAY.fullmatch(cleaned):
        month, day = _MONTHS.get(match.group(1)), int(match.group(2))
    else:
        return None
    # 2024 was a leap year, so February 29 counts as a real birthday.
    if month is None or not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(2024, month)[1]:
        return None
    return month, day


def birthday_text(month: int, day: int) -> str:
    return f"{day} {calendar.month_name[month]}"


def dates_for(moment: datetime.datetime) -> list[tuple[int, int]]:
    """The birthdays that fall on this date. February 29 is celebrated on the 28th when the year has no 29th."""
    today = [(moment.month, moment.day)]
    if moment.month == 2 and moment.day == 28 and not calendar.isleap(moment.year):
        today.append((2, 29))
    return today


def next_run(hour: int, now: float) -> float:
    moment = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0)
    if moment.timestamp() <= now:
        moment += datetime.timedelta(days=1)
    return moment.timestamp()


def _default_birthday() -> dict:
    return {"content": "\N{PARTY POPPER} Happy Birthday {mention}!", "embeds": [], "buttons": []}


def _stored_message(value: Any) -> dict:
    return messages.clean_payload(value, templates=True)


class Greetings(Module):
    key = "greetings"
    category = "Greetings"
    role_timers = (ROLE_ENDS,)
    schema = Schema({
        "welcome_channel": Field(None, optional(snowflake)),
        "farewell_channel": Field(None, optional(snowflake)),
        "ban_channel": Field(None, optional(snowflake)),
        "welcome": Field(messages.empty_payload(), _stored_message),
        "farewell": Field(messages.empty_payload(), _stored_message),
        "ban": Field(messages.empty_payload(), _stored_message),
        "dm": Field(messages.empty_payload(), _stored_message),
        "wait_for_rules": Field(False, boolean),
        "birthday_channel": Field(None, optional(snowflake)),
        "birthday": Field(_default_birthday(), _stored_message),
        "birthday_hour": Field(0, integer(0, 23)),
        "birthday_role": Field(None, optional(snowflake)),
        "birthday_combine": Field(False, boolean),
        "burst_joins": Field(10, integer(0, 1000)),
    })
    migrations = [
        """
        CREATE TABLE birthdays (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            month INTEGER NOT NULL,
            day INTEGER NOT NULL,
            announced_year INTEGER,
            updated_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE INDEX birthdays_date ON birthdays (guild_id, month, day);
        """,
    ]

    # Seconds to wait before a farewell, for the ban event of the same person to arrive.
    BAN_WAIT = 2.0

    # Seconds with no new join before a burst of joins gets its one line.
    BURST_QUIET = float(BURST_SECONDS)

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._recent_bans: dict[tuple[int, int], float] = {}
        self._joins: dict[int, collections.deque] = {}
        self._burst_held: dict[int, int] = {}
        self._burst_tasks: dict[int, asyncio.Task] = {}
        self._problems: dict[tuple[int, str], str] = {}

    def roles_given(self, settings: dict) -> dict[str, set[int]]:
        return {"birthday_role": {settings["birthday_role"]} if settings["birthday_role"] else set()}

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {name: {settings[name]} if settings[name] else set() for name in ("welcome_channel", "farewell_channel", "ban_channel", "birthday_channel")}

    def transient_roles(self, guild_id: int) -> set[int]:
        role = self.cfg(guild_id)["birthday_role"]
        return {role} if role else set()

    def cog_unload(self) -> None:
        for task in self._burst_tasks.values():
            task.cancel()

    def _note_problem(self, guild_id: int, kind: str | None, problem: str | None) -> None:
        """What last went wrong with a message, for the page to show with its remedy. A message that goes out clears it."""
        if kind is None:
            return
        if problem is None:
            self._problems.pop((guild_id, kind), None)
        else:
            self._problems[(guild_id, kind)] = problem

    async def on_load(self) -> None:
        self.bot.timers.handler(TICK, self._tick_due)
        self.bot.timers.handler(ROLE_ENDS, self._role_ends_due)
        self.bot.settings.listen(self.key, self._settings_changed)
        for guild_id in self.bot.settings.guilds_with(self.key):
            if await self.bot.timers.get_key(TICK, str(guild_id)) is None:
                await self._schedule_tick(guild_id, self.cfg(guild_id))

    async def _settings_changed(self, guild_id: int, settings: dict) -> None:
        await self._schedule_tick(guild_id, settings)

    # ---- posting ----

    async def _post(self, guild: Any, channel_id: int | None, stored: dict, member: Any, kind: str | None = None) -> None:
        # Settings hold whatever ID was sent, so a channel is only ever looked up inside its own server.
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        if not messages.has_content(stored):
            return
        if not hasattr(channel, "send"):
            if channel_id:
                self._note_problem(guild.id, kind, f"The {kind} message's channel is gone. Pick another one above.")
            return
        payload = await render_message(stored, Scope(guild=guild, member=member, channel=channel))
        if not messages.has_content(payload):
            return
        try:
            await channel.send(**messages.send_kwargs(payload, allowed_mentions=_MENTIONS))
        except discord.Forbidden as error:
            log.warning("Could not post a greeting in %s: %s", channel_id, error)
            self._note_problem(guild.id, kind, f"The bot can't post the {kind} message in #{channel.name}. Give it View Channel, Send Messages and Embed Links there.")
        except discord.HTTPException as error:
            log.warning("Could not post a greeting in %s: %s", channel_id, error)
            self._note_problem(guild.id, kind, f"Discord refused the {kind} message: {error.text or error.status}")
        else:
            self._note_problem(guild.id, kind, None)

    async def _send_join_dm(self, member: Any, stored: dict) -> bool:
        """Whether it went out."""
        if member.bot or not messages.has_content(stored):
            return False
        payload = await render_message(stored, Scope(guild=member.guild, member=member))
        if not messages.has_content(payload):
            return False
        try:
            await member.send(**messages.send_kwargs(payload, allowed_mentions=discord.AllowedMentions.none()))
        except discord.HTTPException:
            return False  # plenty of people close their DMs
        return True

    def _in_burst(self, guild_id: int, limit: int) -> bool:
        """Whether this join is past the server's limit for joins in a minute. A raid shouldn't fill the welcome channel, and that many unasked-for DMs can get a bot's DMs restricted."""
        if not limit:
            return False
        now = time.monotonic()
        joins = self._joins.setdefault(guild_id, collections.deque())
        joins.append(now)
        while joins and (now - joins[0] > BURST_SECONDS or len(joins) > limit + 1):
            joins.popleft()
        return len(joins) > limit

    def _leaving_in_burst(self, guild: Any, cfg: dict, channel_id: int | None) -> bool:
        """The same limit for goodbye and ban messages, with its own count. A massban or a raid leaving would otherwise put one post per person in a channel, at Discord's pace, for as long as that takes."""
        key = (guild.id, LEAVING)
        if not self._in_burst(key, cfg["burst_joins"]):
            return False
        self._burst_held[key] = self._burst_held.get(key, 0) + 1
        if key not in self._burst_tasks:
            self._burst_tasks[key] = asyncio.ensure_future(self._end_burst(guild, key, channel_id))
        return True

    async def _end_burst(self, guild: Any, key: Any = None, channel_id: int | None = None) -> None:
        """Once things have gone quiet, one line says how many weren't posted one by one."""
        key = guild.id if key is None else key
        try:
            while True:
                held = self._burst_held.get(key, 0)
                await asyncio.sleep(self.BURST_QUIET)
                if self._burst_held.get(key, 0) == held:
                    break
            count = self._burst_held.pop(key, 0)
            channel_id = channel_id or self.cfg(guild.id)["welcome_channel"]
            channel = guild.get_channel_or_thread(channel_id) if channel_id else None
            if count and hasattr(channel, "send"):
                try:
                    what = "left or were banned" if key != guild.id else "joined"
                    await channel.send(f"{count} more {'person' if count == 1 else 'people'} {what}.", allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as error:
                    log.warning("Could not post the end of a burst of joins in %s: %s", channel_id, error)
        finally:
            self._burst_tasks.pop(key, None)

    async def _welcome(self, member: Any, cfg: dict) -> None:
        if self._in_burst(member.guild.id, cfg["burst_joins"]):
            self._burst_held[member.guild.id] = self._burst_held.get(member.guild.id, 0) + 1
            if member.guild.id not in self._burst_tasks:
                self._burst_tasks[member.guild.id] = asyncio.ensure_future(self._end_burst(member.guild))
            return
        await self._post(member.guild, cfg["welcome_channel"], cfg["welcome"], member, "welcome")
        await self._send_join_dm(member, cfg["dm"])

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = self.cfg(member.guild.id)
        if cfg["wait_for_rules"] and member.pending:
            return
        await self._welcome(member, cfg)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.pending and not after.pending:
            cfg = self.cfg(after.guild.id)
            if cfg["wait_for_rules"]:
                await self._welcome(after, cfg)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        cfg = self.cfg(member.guild.id)
        channel_id = cfg["farewell_channel"] or cfg["welcome_channel"]
        if not channel_id or not messages.has_content(cfg["farewell"]):
            return
        if cfg["wait_for_rules"] and member.pending:
            return  # never welcomed, so no goodbye either
        if messages.has_content(cfg["ban"]):
            # Discord sends a ban and a leave for the same person, in either order, and one post is enough.
            await asyncio.sleep(self.BAN_WAIT)
            if (member.guild.id, member.id) in self._recent_bans:
                return
        if self._leaving_in_burst(member.guild, cfg, channel_id):
            return
        await self._post(member.guild, channel_id, cfg["farewell"], member, "farewell")

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: Any) -> None:
        now = time.monotonic()
        self._recent_bans = {key: at for key, at in self._recent_bans.items() if now - at < 60}
        self._recent_bans[(guild.id, user.id)] = now
        cfg = self.cfg(guild.id)
        channel_id = cfg["ban_channel"] or cfg["farewell_channel"] or cfg["welcome_channel"]
        if not messages.has_content(cfg["ban"]) or not channel_id or self._leaving_in_burst(guild, cfg, channel_id):
            return
        await self._post(guild, channel_id, cfg["ban"], user, "ban")

    # ---- birthdays ----

    async def _schedule_tick(self, guild_id: int, cfg: dict, *, soon: bool = False) -> None:
        if not cfg["birthday_channel"] and not cfg["birthday_role"]:
            await self.bot.timers.cancel_key(TICK, str(guild_id))
            return
        now = time.time()
        due = now + 3600 if soon else next_run(cfg["birthday_hour"], now)
        await self.bot.timers.schedule(TICK, due, {"guild_id": guild_id}, guild_id=guild_id, key=str(guild_id))

    async def _tick_due(self, payload: dict, timer: dict) -> None:
        guild_id = payload["guild_id"]
        cfg = self.cfg(guild_id)
        guild = self.bot.get_guild(guild_id)
        retry = await self._announce(guild, cfg, since=timer.get("due_at")) if guild is not None else False
        await self._schedule_tick(guild_id, cfg, soon=retry)

    async def _announce(self, guild: Any, cfg: dict, *, since: float | None = None) -> bool:
        """Announce today's birthdays and hand out the role, and those of any days the bot missed since `since`, up to a week back. True means a post failed and the rest of the day can try again."""
        now = datetime.datetime.now(datetime.timezone.utc)
        missed = 0
        if since is not None:
            missed = min((now.date() - datetime.datetime.fromtimestamp(since, datetime.timezone.utc).date()).days, MAX_LATE_DAYS)
        failed = False
        for back in range(max(missed, 0), -1, -1):
            if await self._announce_day(guild, cfg, now - datetime.timedelta(days=back)):
                failed = True
        return failed

    async def _announce_day(self, guild: Any, cfg: dict, moment: datetime.datetime) -> bool:
        marks = " OR ".join("(month = ? AND day = ?)" for _ in dates_for(moment))
        dates = [part for date in dates_for(moment) for part in date]
        rows = await self.db.all(
            f"SELECT user_id FROM birthdays WHERE guild_id = ? AND ({marks}) AND (announced_year IS NULL OR announced_year != ?)",
            (guild.id, *dates, moment.year),
        )
        people = []
        for row in rows:
            member = guild.get_member(row["user_id"])
            if member is None:
                if getattr(guild, "chunked", False):  # the member list is whole, so they've left. Their row stays, since people come back.
                    continue
                try:
                    member = await guild.fetch_member(row["user_id"])
                except discord.HTTPException:
                    continue
            people.append(member)
        if not people:
            return False
        channel = guild.get_channel_or_thread(cfg["birthday_channel"]) if cfg["birthday_channel"] else None
        if not hasattr(channel, "send"):
            announced, failed = people, False
            if cfg["birthday_channel"]:
                self._note_problem(guild.id, "birthday", "The birthday message's channel is gone. Pick another one below.")
        elif cfg["birthday_combine"]:
            announced, failed = [], False
            for start in range(0, len(people), COMBINED_PEOPLE):
                group = people[start:start + COMBINED_PEOPLE]
                if await self._post_birthday(channel, guild, group, cfg["birthday"]):
                    announced.extend(group)
                else:
                    failed = True
        else:
            announced, failed = [], False
            for member in people:
                if await self._post_birthday(channel, guild, [member], cfg["birthday"]):
                    announced.append(member)
                else:
                    failed = True
        for member in announced:
            await self.db.run("UPDATE birthdays SET announced_year = ? WHERE guild_id = ? AND user_id = ?", (moment.year, guild.id, member.id))
            await self._give_birthday_role(guild, member, cfg)
        return failed

    async def _post_birthday(self, channel: Any, guild: Any, people: list, stored: dict) -> bool:
        if not messages.has_content(stored):
            return True
        if len(people) > 1:
            mentions = ", ".join(member.mention for member in people)
            names = ", ".join(plain_name(member).translate(_BRACES) for member in people)
            stored = messages.map_text(stored, lambda text: text.replace("{mention}", mentions).replace("{user}", names))
        payload = await render_message(stored, Scope(guild=guild, member=people[0], channel=channel))
        if not messages.has_content(payload):
            return True  # the template came out empty, and that's no reason to hold back the role or try again all day
        try:
            await channel.send(**messages.send_kwargs(payload, allowed_mentions=_MENTIONS))
        except discord.HTTPException as error:
            log.warning("Could not post a birthday in %s: %s", channel.id, error)
            if isinstance(error, discord.Forbidden):
                self._note_problem(guild.id, "birthday", f"The bot can't post the birthday message in #{channel.name}. Give it View Channel, Send Messages and Embed Links there.")
            else:
                self._note_problem(guild.id, "birthday", f"Discord refused the birthday message: {error.text or error.status}")
            return False
        self._note_problem(guild.id, "birthday", None)
        return True

    async def _give_birthday_role(self, guild: Any, member: Any, cfg: dict) -> None:
        if not cfg["birthday_role"]:
            return
        role = guild.get_role(cfg["birthday_role"])
        problem = None
        if role is None or role.is_default() or role.managed:
            problem = "The birthday role is gone or can't be handed out. Pick another one below."
        elif role >= guild.me.top_role:
            problem = f"{role.name} sits at or above the bot's highest role, so nobody gets it. Move the bot's role above it."
        # Anyone can claim a birthday, so this role counts as one members choose for themselves.
        elif not role_is_safe([name for name, on in role.permissions if on], self.bot.settings.get(guild.id, COMMANDS_MODULE)):
            problem = f"{role.name} holds moderator permissions, so the bot won't hand it out while elevated roles are off on the Commands page."
        if problem:
            log.info("Birthday role in %s: %s", guild.id, problem)
            self._note_problem(guild.id, "birthday role", problem)
            return
        try:
            await member.add_roles(role, reason="Birthday")
        except discord.HTTPException as error:
            log.warning("Could not give the birthday role to %s: %s", member.id, error)
            self._note_problem(guild.id, "birthday role", f"Discord refused the birthday role: {error.text or error.status}")
            return
        self._note_problem(guild.id, "birthday role", None)
        await self.bot.timers.schedule(
            ROLE_ENDS, time.time() + DAY, {"guild_id": guild.id, "user_id": member.id, "role_id": role.id}, guild_id=guild.id, key=f"{guild.id}:{member.id}",
        )

    async def _role_ends_due(self, payload: dict, timer: dict) -> None:
        guild = self.bot.get_guild(payload["guild_id"])
        member = guild.get_member(payload["user_id"]) if guild else None
        role = member.get_role(payload["role_id"]) if member else None
        if role is not None:
            try:
                await member.remove_roles(role, reason="Birthday is over")
            except (discord.NotFound, discord.Forbidden) as error:
                # Asking again won't help. Anything else, such as Discord having a bad minute, goes back to the timer to retry.
                log.info("Could not take the birthday role from %s: %s", member.id, error)

    async def _set_birthday(self, guild_id: int, user_id: int, month: int, day: int) -> None:
        # The announced year stays on purpose, so changing a birthday can't earn a second announcement this year.
        await self.db.run(
            "INSERT INTO birthdays (guild_id, user_id, month, day, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET month = excluded.month, day = excluded.day, updated_at = excluded.updated_at",
            (guild_id, user_id, month, day, time.time()),
        )

    async def _upcoming(self, guild_id: int) -> list[dict]:
        rows = await self.db.all("SELECT user_id, month, day FROM birthdays WHERE guild_id = ? ORDER BY month, day", (guild_id,))
        moment = datetime.datetime.now(datetime.timezone.utc)
        today = (moment.month, moment.day)
        return [r for r in rows if (r["month"], r["day"]) >= today] + [r for r in rows if (r["month"], r["day"]) < today]

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.post("/greetings/test", self._test_endpoint)
        router.get("/greetings/problems", self._problems_endpoint)
        router.get("/greetings/birthdays", self._birthdays_endpoint)
        router.put(r"/greetings/birthdays/{user_id:\d+}", self._set_birthday_endpoint)
        router.delete(r"/greetings/birthdays/{user_id:\d+}", self._remove_birthday_endpoint)

    async def _test_endpoint(self, req: Any) -> dict:
        form = await req.json()
        if form.get("which") not in KINDS:
            raise ValidationError({"which": "Pick a message to test."})
        payload = messages.required_payload(form.get("payload"), templates=True)
        if form["which"] == "dm":
            await req.ops.send_dm(req.user.id, await render_message(payload, preview_scope(req.ops, req.guild_id, req.user.id), guild_id=req.guild_id))
            return {"sent_to": "dm"}
        channel_id = req.channel(form.get("channel_id"), posting=True)
        # A test must not ping anybody, so it goes out silent even though the real message wouldn't.
        sent = await req.ops.send_message(channel_id, await render_message(payload, preview_scope(req.ops, req.guild_id, req.user.id, channel_id=channel_id), guild_id=req.guild_id), silent=True)
        await req.record("greetings.test", detail=form["which"])
        return {"sent_to": "channel", "url": sent["url"]}

    async def _problems_endpoint(self, req: Any) -> list[str]:
        return [text for (guild_id, _), text in sorted(self._problems.items()) if guild_id == req.guild_id]

    async def _birthdays_endpoint(self, req: Any) -> list[dict]:
        out = []
        for row in await self._upcoming(req.guild_id):
            member = req.ops.member(req.guild_id, row["user_id"])
            out.append({
                "user_id": row["user_id"], "month": row["month"], "day": row["day"], "date": birthday_text(row["month"], row["day"]),
                "name": member["display_name"] if member else None, "avatar": member["avatar"] if member else None,
            })
        return out

    async def _set_birthday_endpoint(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        form = await req.json()
        date = parse_birthday(form.get("birthday") if isinstance(form.get("birthday"), str) else "")
        if date is None:
            raise ValidationError({"birthday": "Write the date as day/month, such as 25/12."})
        if req.ops.member(req.guild_id, user_id) is None:
            raise ValidationError({"user_id": "That person isn't in this server."})
        await self._set_birthday(req.guild_id, user_id, *date)
        await req.record("birthday.set", detail=f"{user_id}: {birthday_text(*date)}")
        return {"user_id": user_id, "month": date[0], "day": date[1], "date": birthday_text(*date)}

    async def _remove_birthday_endpoint(self, req: Any) -> dict:
        user_id = req.int_param("user_id")
        if not await self.db.run("DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?", (req.guild_id, user_id)):
            raise ApiError(404, "No birthday is set for that person.")
        await req.record("birthday.remove", detail=str(user_id))
        return {"ok": True}

    # ---- /greetings ----

    async def _save(self, ctx: commands.Context, action: str, form: dict) -> bool:
        try:
            await self.bot.settings.save(ctx.guild.id, self.key, form, actor=self.bot.actor(ctx.author), action=action)
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values()), "That can't be saved."))
            return False
        return True

    @commands.hybrid_group(name="greetings", fallback="show", description="Show the welcome, farewell and ban setup")
    @commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @requires("manage_guild")
    async def greetings(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)

        def line(name: str, channel_id: int | None, stored: dict) -> str:
            where = f"<#{channel_id}>" if channel_id else "no channel"
            return f"**{name}:** {where}, {'set' if messages.has_content(stored) else 'no message'}"

        farewell = cfg["farewell_channel"] or cfg["welcome_channel"]
        await ctx.send(
            "\n".join([
                line("Welcome", cfg["welcome_channel"], cfg["welcome"]),
                line("Farewell", farewell, cfg["farewell"]),
                line("Ban", cfg["ban_channel"] or farewell, cfg["ban"]),
                f"**Join DM:** {'set' if messages.has_content(cfg['dm']) else 'no message'}",
                f"Full settings: {self.bot.public_url}/dashboard/{ctx.guild.id}/greetings",
            ]),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _may_post_in(self, ctx: commands.Context, channel: Any) -> bool:
        """The same check the dashboard makes on a channel that will carry a person's text."""
        refusal = access.post_refusal(ctx.author, channel)
        if refusal:
            await ctx.fail(refusal)
            return False
        return True

    async def _set_text(self, ctx: commands.Context, field: str, action: str, text: str | None, set_note: str, clear_note: str) -> None:
        # With no text, the whole message goes, or an embed set on the dashboard would keep posting after it reads as cleared.
        stored = {**self.cfg(ctx.guild.id)[field], "content": text} if text else messages.empty_payload()
        if await self._save(ctx, action, {field: stored}):
            await ctx.ok(set_note if text else clear_note)

    @greetings.command(name="welcome_channel", description="Set the channel for welcome messages")
    @requires("manage_guild")
    async def greetings_welcome_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        if await self._may_post_in(ctx, channel) and await self._save(ctx, "greetings.welcome_channel", {"welcome_channel": channel.id}):
            await ctx.ok(f"Welcome messages now go to {channel.mention}.")

    @greetings.command(name="welcome", description="Set the message posted when someone joins. Leave it out to clear it.")
    @requires("manage_guild")
    async def greetings_welcome(self, ctx: commands.Context, *, message: str | None = None) -> None:
        await self._set_text(ctx, "welcome", "greetings.welcome", message, "The welcome message is set.", "The welcome message is cleared.")

    @greetings.command(name="joindm", description="Set the DM sent when someone joins. Leave it out to clear it.")
    @requires("manage_guild")
    async def greetings_joindm(self, ctx: commands.Context, *, message: str | None = None) -> None:
        await self._set_text(ctx, "dm", "greetings.joindm", message, "The join DM is set.", "The join DM is cleared.")

    @greetings.command(name="set_welcome", with_app_command=False, description="Set one channel for welcome, leave and ban messages, or clear all three")
    @requires("manage_guild")
    async def greetings_set_welcome(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        value = channel.id if channel else None
        if (channel is None or await self._may_post_in(ctx, channel)) and await self._save(ctx, "greetings.welcome_channel", {"welcome_channel": value, "farewell_channel": value, "ban_channel": value}):
            await ctx.ok(f"Welcome, leave and ban messages now go to {channel.mention}." if channel else "The welcome, leave and ban channels are cleared.")

    @greetings.command(name="set_farewell", with_app_command=False, description="Set one channel for leave and ban messages, or clear both")
    @requires("manage_guild")
    async def greetings_set_farewell(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        value = channel.id if channel else None
        if (channel is None or await self._may_post_in(ctx, channel)) and await self._save(ctx, "greetings.farewell_channel", {"farewell_channel": value, "ban_channel": value}):
            await ctx.ok(f"Leave and ban messages now go to {channel.mention}." if channel else "The leave and ban channels are cleared.")

    @greetings.command(name="farewell_channel", description="Set the channel for leave messages")
    @requires("manage_guild")
    async def greetings_farewell_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        if await self._may_post_in(ctx, channel) and await self._save(ctx, "greetings.farewell_channel", {"farewell_channel": channel.id}):
            await ctx.ok(f"Leave messages now go to {channel.mention}.")

    @greetings.command(name="farewell", description="Set the message posted when someone leaves. Leave it out to clear it.")
    @requires("manage_guild")
    async def greetings_farewell(self, ctx: commands.Context, *, message: str | None = None) -> None:
        await self._set_text(ctx, "farewell", "greetings.farewell", message, "The leave message is set.", "The leave message is cleared.")

    @greetings.command(name="ban_channel", description="Set the channel for ban messages")
    @requires("manage_guild")
    async def greetings_ban_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        if await self._may_post_in(ctx, channel) and await self._save(ctx, "greetings.ban_channel", {"ban_channel": channel.id}):
            await ctx.ok(f"Ban messages now go to {channel.mention}.")

    @greetings.command(name="banmessage", description="Set the message posted when someone is banned. Leave it out to clear it.")
    @requires("manage_guild")
    async def greetings_banmessage(self, ctx: commands.Context, *, message: str | None = None) -> None:
        await self._set_text(ctx, "ban", "greetings.banmessage", message, "The ban message is set.", "The ban message is cleared.")

    @greetings.command(name="testgreet", description="Post the welcome, leave and ban messages here, and DM you the join DM")
    @requires("manage_guild")
    async def greetings_testgreet(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)
        public = [cfg[field] for field in ("welcome", "farewell", "ban") if messages.has_content(cfg[field])]
        has_dm = messages.has_content(cfg["dm"])
        if not public and not has_dm:
            await ctx.fail("No greeting messages are set yet.")
            return
        await ctx.defer()
        for stored in public:
            payload = await render_message(stored, Scope(guild=ctx.guild, member=ctx.author, channel=ctx.channel))
            # The test stays quiet, so trying a message that mentions people doesn't ping them.
            await ctx.send(**messages.send_kwargs(payload, allowed_mentions=discord.AllowedMentions.none()))
        dm_sent = has_dm and await self._send_join_dm(ctx.author, cfg["dm"])
        tried = len(public) + dm_sent
        note = " Your DMs are closed, so the join DM didn't reach you." if has_dm and not dm_sent else ""
        await ctx.ok(f"Sent {tried} test message{'' if tried == 1 else 's'}.{note}")

    # ---- /birthday ----

    @commands.hybrid_group(name="birthday", fallback="view", description="See someone's birthday")
    @commands.guild_only()
    @app_commands.describe(member="Whose birthday. Defaults to yours.")
    async def birthday(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = member or ctx.author
        row = await self.db.one("SELECT month, day FROM birthdays WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, target.id))
        if row is None:
            await ctx.send(f"{target.mention} has no birthday set." if member else "You have no birthday set. Set it with `/birthday set`.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        await ctx.send(f"{target.mention}'s birthday is {birthday_text(row['month'], row['day'])}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _pick_person(self, ctx: commands.Context, member: Any) -> Any:
        """The person a birthday command acts on. Someone else's birthday takes Manage Server."""
        if member is None or member.id == ctx.author.id:
            return ctx.author
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.fail("You need Manage Server to change someone else's birthday.")
            return None
        return member

    @birthday.command(name="set", description="Set your birthday, as day/month")
    @app_commands.describe(birthday="Day then month, such as 25/12", member="Someone else, if you have Manage Server")
    async def birthday_set(self, ctx: commands.Context, birthday: str, member: discord.Member | None = None) -> None:
        target = await self._pick_person(ctx, member)
        if target is None:
            return
        date = parse_birthday(birthday)
        if date is None:
            await ctx.fail("Write the date as day/month, such as 25/12.")
            return
        await self._set_birthday(ctx.guild.id, target.id, *date)
        who = "Your birthday" if target.id == ctx.author.id else f"{target.mention}'s birthday"
        await ctx.ok(f"{who} is set to {birthday_text(*date)}.")

    @birthday.command(name="remove", description="Remove your birthday")
    @app_commands.describe(member="Someone else, if you have Manage Server")
    async def birthday_remove(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = await self._pick_person(ctx, member)
        if target is None:
            return
        removed = await self.db.run("DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, target.id))
        await ctx.ok("That birthday is removed." if removed else "No birthday was set.")

    @birthday.command(name="channel", description="Set where birthdays are announced. Leave it out to turn announcements off.")
    @requires("manage_guild")
    async def birthday_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        if (channel is None or await self._may_post_in(ctx, channel)) and await self._save(ctx, "birthday.channel", {"birthday_channel": channel.id if channel else None}):
            await ctx.ok(f"Birthdays are announced in {channel.mention}." if channel else "Birthday announcements are off.")

    @birthday.command(name="config", description="Show how birthdays are set up")
    async def birthday_config(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)
        channel = f"<#{cfg['birthday_channel']}>" if cfg["birthday_channel"] else "off"
        role = f"<@&{cfg['birthday_role']}>" if cfg["birthday_role"] else "none"
        await ctx.send(
            f"Announcements: {channel} at {cfg['birthday_hour']:02d}:00 UTC\nBirthday role: {role}\nCombine same-day birthdays: {'yes' if cfg['birthday_combine'] else 'no'}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @birthday.command(name="list", description="Show every birthday set in this server")
    async def birthday_list(self, ctx: commands.Context) -> None:
        rows = await self._upcoming(ctx.guild.id)
        if not rows:
            await ctx.send("No birthdays are set.", ephemeral=True)
            return
        lines = [f"{birthday_text(row['month'], row['day'])}: <@{row['user_id']}>" for row in rows]
        text = "\n".join(lines)
        if len(text) <= 1900:
            await ctx.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        plain = []
        for row in rows:
            member = ctx.guild.get_member(row["user_id"])
            plain.append(f"{birthday_text(row['month'], row['day'])}: {member.display_name if member else row['user_id']}")
        await ctx.send(f"{len(rows)} birthdays, soonest first.", file=discord.File(io.BytesIO("\n".join(plain).encode("utf-8")), "birthdays.txt"), ephemeral=True)
