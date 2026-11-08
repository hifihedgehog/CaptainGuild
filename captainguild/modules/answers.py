"""Wiki answers: AI answers from copies of the server's wikis and documentation sites, on the bot host's own Anthropic account.

/how_do_i answers from the server's sources alone. /brainstorm plans a print or laser project, checking product facts in the sources,
and draws a sketch when asked. /ask adds web search for prices, other brands and anything the sources don't cover. /make_model writes
the part in OpenSCAD and posts a 3MF file, its source and a preview. The four share each person's limits: three requests a minute, and
a daily number the server sets, 25 unless it changes that. A request nothing was billed for doesn't count, and neither do the first
three a day that /how_do_i or /brainstorm sends to /ask. New posts in chosen forums get one answer, up to a daily number for the
whole server.

A server names its sources, each a sitemap or a start page to crawl, and its subject, which fills the wording the model is given. Every
answer's footer names the sources it drew from.

Every request is checked against the server's word lists before it goes out, and every answer before it's posted: through a
term-matching module the host names when the bot starts, which also censors the answer card, when its folder has this server's
lists, or else through this server's automod word rule, which can't censor and so holds back an answer it would flag.

Each request costs the host money. The host names the servers that may use its key when the bot starts (--answers-servers), and in
any other server the pack can't be switched on. Within a server, only the owner or an Administrator changes these settings, and a
monthly budget, when the server sets one, stops answers once the month's spend reaches it. The key is the only secret: the host puts
it in .env as ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
import types
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import anthropic
import discord
import soupsieve
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.answers import engine, modelmaker, prompts, screening, servers
from ..core.answers.wiki import CRAWL, SITEMAP, Copies, Library, Shelf, Source
from ..core.answers.wiki import MIGRATIONS as WIKI_MIGRATIONS
from ..core.module import Module, requires
from ..core.outside import Outside
from ..core.validate import Field, Invalid, Schema, boolean, choice, http_url, integer, list_of, snowflakes, text, whole_number
from ..web.api import ApiError

log = logging.getLogger(__name__)

COMMANDS = ("how_do_i", "brainstorm", "ask", "make_model")
ROUNDS = {"how_do_i": 8, "brainstorm": 6, "ask": 8, "make_model": modelmaker.MAX_ROUNDS}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
BURST_COUNT = 3
BURST_SECONDS = 60.0
BURSTS_REMEMBERED = 10_000
CONCURRENCY = 3  # requests answered at once across every server, and the rest wait their turn
REQUEST_SCREEN_SECONDS = 2.0  # Discord drops an interaction with no answer in 3 seconds, so a slow check lets the request through
REFRESH_KIND = "answers.refresh"
REFRESH_CHECK_SECONDS = 3600  # how often the bot looks for a source whose update is due
FIRST_REFRESH_SECONDS = 60
COSTS_KEEP_SECONDS = 70 * 86400
THREADS_REMEMBERED = 2000
POST_MIN_WORDS = 5  # "Help" and a photo gives the model nothing to search for
POST_MAX_CHARS = 1500
SOURCE_LABEL_MAX = 60
FIELD_MAX = 1024
EMBED_DESCRIPTION_MAX = 4096
EMBED_TOTAL_MAX = 6000  # Discord's cap on all the text in one message's embeds
PROMPT_MAX = 20_000
MAX_SOURCES = 5  # each copy can hold 25 million characters, about 250 MB of index in memory
REDIRECTS_FREE = 3  # questions a day /how_do_i and /brainstorm can send to /ask without counting toward the person's daily limit

NO_KEY = "The bot's host hasn't added an API key for AI answers."
NOT_ALLOWED = "The bot's host hasn't allowed Wiki answers in this server, since each answer is billed to the host's own AI account."
MONTH_SPENT = "This server has spent its ${limit} for AI answers this month. They start again on the 1st, UTC."
NO_WIKI = "This server hasn't named a wiki to answer from yet. An Administrator can add one on the Wiki answers page."
BUILDING = "The bot's copy of the wiki is still being built. Try again in a few minutes."
RULES_NOTICE = "I can't help with that, because it would break the server rules."  # it never repeats the request, whose wording broke them
NO_MODELS = "Making models isn't available here, because the bot's host hasn't installed OpenSCAD, Xvfb and prlimit."
BURST = "You're sending requests faster than the bot can answer. Try again in a minute."
DAILY = "You've used today's {limit} requests. /ask, /how_do_i, /brainstorm, and /make_model share this limit, and it resets at midnight UTC."
REDIRECT = "That one fits {ask} better. It can search the web for prices, other brands, and anything the wiki doesn't cover."
NOT_COUNTED = "It didn't count toward your daily limit."
UPDATE_RUNNING = "A wiki update is already running."
LATE_REPLY_MARGIN = timedelta(seconds=30)  # time to upload an answer's files before Discord stops taking the command's reply

STATUS_TEXT = {
    "refused": "The model's safety filter declined that question. Try rewording it.",
    "declined": RULES_NOTICE,
    "blocked_reply": RULES_NOTICE,
    "too_long": "That was too much to finish in one answer. Ask for one part or one question at a time.",
    "too_slow": "That took too long to finish. Try a simpler request.",
    "busy": "Too many questions at once right now. Try again in a minute.",
    "unavailable": "The answer service isn't responding. Try again in a few minutes.",
    "key_refused": "The AI service refused the bot's API key, so the bot's host has to replace it.",
    "error": "Something went wrong while answering. Try rephrasing the question.",
}

MIGRATIONS = [
    """
    CREATE TABLE answer_uses (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        day INTEGER NOT NULL,
        uses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id, day)
    );
    CREATE TABLE answer_auto_days (
        guild_id INTEGER NOT NULL,
        day INTEGER NOT NULL,
        answers INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, day)
    );
    CREATE TABLE answer_costs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER,
        command TEXT NOT NULL,
        status TEXT NOT NULL,
        model TEXT NOT NULL,
        rounds INTEGER NOT NULL DEFAULT 0,
        tool_calls INTEGER NOT NULL DEFAULT 0,
        web_searches INTEGER NOT NULL DEFAULT 0,
        image INTEGER NOT NULL DEFAULT 0,
        files INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read INTEGER NOT NULL DEFAULT 0,
        cache_write INTEGER NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0,
        seconds REAL NOT NULL DEFAULT 0,
        at REAL NOT NULL
    );
    CREATE INDEX answer_costs_guild ON answer_costs (guild_id, at);
    """,
    "ALTER TABLE answer_uses ADD COLUMN redirects INTEGER NOT NULL DEFAULT 0;",
]

MODEL_CHOICES = {command: engine.WEB_SEARCH_MODELS if command == "ask" else tuple(engine.PRICES) for command in COMMANDS}


def path_prefix(value: Any) -> str:
    cleaned = text(200, minimum=1)(value)
    if not cleaned.startswith("/"):
        raise Invalid("Start the path with a slash, such as /en/.")
    return cleaned


def address_pattern(value: Any) -> str:
    cleaned = text(300, minimum=1)(value)
    if any(letter.isspace() for letter in cleaned):
        raise Invalid("An address pattern can't hold spaces.")
    return cleaned


def css_selector(value: Any) -> str:
    cleaned = text(200)(value)
    if cleaned:
        try:
            soupsieve.compile(cleaned)
        except Exception as error:  # soupsieve's own errors, for a selector it can't read
            raise Invalid(f"{cleaned} isn't a CSS selector the bot can use.") from error
    return cleaned


def source_entry(value: Any) -> dict:
    if not isinstance(value, dict):
        raise Invalid("Each source needs a name and an address.")
    return {
        "name": text(100, minimum=1)(value.get("name", "")),
        "kind": choice(SITEMAP, CRAWL)(value.get("kind", SITEMAP)),
        "address": http_url(500)(value.get("address", "")),
        "prefix": path_prefix(value.get("prefix", "/")),
        "include": list_of(address_pattern, 20)(value.get("include", [])),
        "exclude": list_of(address_pattern, 50)(value.get("exclude", [])),
        "selector": css_selector(value.get("selector", "")),
        "refresh_hours": integer(1, 720)(value.get("refresh_hours", 24)),
    }


def source_list(value: Any) -> list[dict]:
    rows = list_of(source_entry, MAX_SOURCES, unique=False)(value)
    names: set[str] = set()
    keys: set[str] = set()
    for row in rows:
        if row["name"].casefold() in names:
            raise Invalid(f"Two sources are named {row['name']}. Give each its own name, since answers name them.")
        if Source.of(row).key in keys:
            raise Invalid(f"{row['name']} reads the same pages as another source.")
        names.add(row["name"].casefold())
        keys.add(Source.of(row).key)
    return rows


SCHEMA = Schema({
    "enabled": Field(False, boolean),
    "sources": Field([], source_list),
    "subject": Schema({name: Field(default, text(1000, minimum=1)) for name, default in prompts.SUBJECT.items()}),
    "rules": Field(prompts.RULES, text(PROMPT_MAX)),
    "daily_limit": Field(25, integer(1, 1000)),
    "auto_answer_forums": Field([], snowflakes(50)),
    "auto_answer_daily_limit": Field(200, integer(1, 5000)),
    "monthly_limit": Field(200, integer(0, 100_000)),  # dollars a month, across every command and automatic answers. 0 is no budget.
    # Only models the bot knows the price of, so the cost log and the monthly budget read right.
    "models": Schema({
        "how_do_i": Field("claude-haiku-5-5", choice(*MODEL_CHOICES["how_do_i"])),
        "brainstorm": Field("claude-sonnet-5-5", choice(*MODEL_CHOICES["brainstorm"])),
        "ask": Field("claude-sonnet-5-5", choice(*MODEL_CHOICES["ask"])),
        "make_model": Field("claude-sonnet-5-5", choice(*MODEL_CHOICES["make_model"])),
    }),
    "efforts": Schema({
        "how_do_i": Field("high", choice(*EFFORTS)),
        "brainstorm": Field("high", choice(*EFFORTS)),
        "ask": Field("high", choice(*EFFORTS)),
        "make_model": Field("medium", choice(*EFFORTS)),
    }),
    "footers": Schema({command: Field(prompts.FOOTERS[command], text(500)) for command in COMMANDS}),
    "auto_intro": Field(prompts.AUTO_INTRO, text(1000)),
})


def utc_day(moment: float | None = None) -> int:
    return int((time.time() if moment is None else moment) // 86400)


def shorten(words: str, limit: int) -> str:
    return words if len(words) <= limit else words[:limit - 1].rstrip() + "…"


def fit_text(words: str, limit: int) -> str:
    """Cut text to limit characters, at a line break when one is near the end and otherwise at a space."""
    if len(words) <= limit:
        return words
    cut = words[:limit - 1]
    for mark in ("\n", " "):
        at = cut.rfind(mark)
        if at > limit * 0.6:
            cut = cut[:at]
            break
    return cut.rstrip() + "…"


def answer_card(title: str, result: engine.Answer, footer: str) -> discord.Embed:
    """The card an answer posts on: the reply, its sources, and the sketch or the model's preview when there is one."""
    # A wiki's FAQ titles run past 150 characters, and a full-length list once filled a field after five of seven sources.
    fields: list[list[str]] = []
    current: list[str] = []
    for number, (source_title, url) in enumerate(result.sources, 1):
        label = shorten(source_title.replace("[", "(").replace("]", ")"), SOURCE_LABEL_MAX)
        line = f"[{number}] [{label}]({url})"
        if len(line) > FIELD_MAX:
            continue  # an address that long can't be a link in a field
        if current and len("\n".join(current + [line])) > FIELD_MAX:
            fields.append(current)
            current = []
        current.append(line)
    if current:
        fields.append(current)
    named = [("Sources" if index == 0 else "More sources", "\n".join(lines)) for index, lines in enumerate(fields[:2])]
    if result.image is not None and result.image_caption:
        named.append((result.image_label, shorten(result.image_caption, engine.CAPTION_MAX)))
    title = shorten(title, 256)
    # The reply gets what the total leaves after everything else.
    used = len(title) + len(footer) + sum(len(name) + len(value) for name, value in named)
    embed = discord.Embed(title=title, description=fit_text(result.text, max(100, min(EMBED_DESCRIPTION_MAX, EMBED_TOTAL_MAX - used - 20))), color=discord.Color.blurple())
    for name, value in named:
        embed.add_field(name=name, value=value, inline=False)
    if footer:
        embed.set_footer(text=footer)
    if result.image is not None:
        embed.set_image(url=f"attachment://{result.image_name}")
    return embed


def answer_files(result: engine.Answer) -> list[discord.File]:
    files = []
    if result.image is not None:
        files.append(discord.File(io.BytesIO(result.image), filename=result.image_name))
    files += [discord.File(io.BytesIO(data), filename=name) for name, data in result.files]
    return files


def question_from_post(title: str, content: str, attachments: int = 0) -> str | None:
    """A forum post's title and first message as a question, or None when there isn't enough text to answer from. Custom emoji and
    mentions are left out."""
    words = re.sub(r"<a?:\w+:\d+>", "", content or "")
    words = re.sub(r"<(?:@[!&]?|#)\d+>", "", words)
    words = re.sub(r"\s+", " ", words).strip()
    title = re.sub(r"\s+", " ", title or "").strip()
    question = f"{title}: {words}" if words and words.lower() != title.lower() else (title or words)
    if len(re.findall(r"\w+", question)) < POST_MIN_WORDS:
        return None
    question = question[:POST_MAX_CHARS]
    if attachments:
        question += f" (The member attached {attachments} photo or video file{'s' if attachments > 1 else ''} that you can't see.)"
    return question


class _nothing:
    """An empty `async with`, for a slash command, which shows the bot thinking once deferred."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _month_start(moment: float, back: int = 0) -> float:
    now = datetime.fromtimestamp(moment, timezone.utc)
    year, month = now.year, now.month - back
    while month < 1:
        month += 12
        year -= 1
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


class Answers(Module):
    key = "answers"
    category = "Wiki answers"
    command_pack = True
    schema = SCHEMA
    # Each answer is billed to the bot host's account, and these settings decide how many there are and what they say.
    admin_fields = frozenset(SCHEMA.fields)
    migrations = MIGRATIONS
    message_migrations = WIKI_MIGRATIONS

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.outside = Outside()
        self.copies = Copies(bot.message_db)
        self.client: Any = None  # the Anthropic client, made on first use
        self.slots = asyncio.Semaphore(CONCURRENCY)
        self._bursts: dict[int, deque] = {}
        self._threads: OrderedDict[int, None] = OrderedDict()  # forum posts already handled, newest last
        self._mentions: dict[int, dict[str, str]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._refreshing: asyncio.Task | None = None
        self._warmed = False

    # ---- start and stop ----

    async def on_load(self) -> None:
        self.bot.timers.handler(REFRESH_KIND, self._refresh_due, anywhere=True)
        if await self.bot.timers.get_key(REFRESH_KIND, "all") is None:
            await self.bot.timers.schedule(REFRESH_KIND, time.time() + FIRST_REFRESH_SECONDS, {}, key="all")
        self.bot.settings.listen(self.key, self._settings_saved)
        self._spawn(self._load_copies())

    async def cog_unload(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.outside.close()
        if self.client is not None:
            await self.client.close()

    def _spawn(self, work: Any) -> asyncio.Task:
        """Work that runs on its own. The set keeps each task referenced until it finishes."""
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._report)
        return task

    @staticmethod
    def _report(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("Wiki answers work failed", exc_info=task.exception())

    def on_in(self, guild_id: int | None) -> bool:
        """Switched on, in a server the host lets use its key."""
        return servers.allowed(guild_id) and super().on_in(guild_id)

    def settings_refusal(self, guild_id: int, before: dict, after: dict) -> dict[str, str] | None:
        if after.get("enabled") and not before.get("enabled") and not servers.allowed(guild_id):
            return {"enabled": NOT_ALLOWED}
        return None

    def sources_in_use(self) -> dict[str, tuple[Source, int]]:
        """The sources of every server with this pack on, by key, with the shortest time between updates any of them asks for."""
        found: dict[str, tuple[Source, int]] = {}
        for guild_id in self.bot.settings.guilds_with(self.key):
            if not self.on_in(guild_id):
                continue
            settings = self.cfg(guild_id)
            for row in settings["sources"]:
                source = Source.of(row)
                hours = min(row["refresh_hours"], found[source.key][1]) if source.key in found else row["refresh_hours"]
                found[source.key] = (source, hours)
        return found

    async def _load_copies(self) -> None:
        for source, _ in self.sources_in_use().values():
            await self.copies.load(source)

    async def _settings_saved(self, guild_id: int, settings: dict) -> None:
        """A server switched the pack or changed its sources: build or load each copy it needs, and let go of indexes nobody reads."""
        self.copies.forget(set(self.sources_in_use()))
        if not self.on_in(guild_id):
            return
        for row in settings["sources"]:
            source = Source.of(row)
            if self.copies.index(source) is None and not self.copies.running(source):
                self._spawn(self._ready_copy(source))

    def wanted(self, source: Source) -> Callable[[], bool]:
        """Whether a server still reads this source, which an update asks before each page."""
        return lambda: source.key in self.sources_in_use()

    async def _ready_copy(self, source: Source) -> None:
        status = await self.copies.status(source)
        if status["pages"]:
            await self.copies.load(source)
        else:
            await self.copies.refresh(source, self.outside, wanted=self.wanted(source))

    # ---- keeping the copies up to date ----

    async def _refresh_due(self, payload: dict, row: dict) -> None:
        """Run in the background, because the timer loop runs one handler at a time and a full download takes a while."""
        await self.bot.timers.schedule(REFRESH_KIND, time.time() + REFRESH_CHECK_SECONDS, {}, key="all")
        if self._refreshing is None or self._refreshing.done():
            self._refreshing = self._spawn(self.refresh_due_sources())

    async def refresh_due_sources(self, now: float | None = None) -> None:
        """Update each source in use whose time has come, one after another, then clear what's past keeping."""
        now = time.time() if now is None else now
        in_use = self.sources_in_use()
        for source, hours in in_use.values():
            last = await self.copies.last_try(source)
            if last is not None and now - last < hours * 3600 - 60:
                continue
            try:
                summary = await self.copies.refresh(source, self.outside, wanted=self.wanted(source))
            except Exception:
                log.exception("The update of %s failed", source.address)
                continue
            if not summary.get("ok") and not summary.get("already_running"):
                log.warning("The update of %s failed: %s", source.address, summary.get("error"))
        removed = await self.copies.drop_unused(set(in_use))
        if removed:
            log.info("Removed %d copies no server reads now", removed)
        today = utc_day(now)
        await self.db.run("DELETE FROM answer_uses WHERE day < ?", (today - 1,))
        await self.db.run("DELETE FROM answer_auto_days WHERE day < ?", (today - 1,))
        await self.db.run("DELETE FROM answer_costs WHERE at < ?", (now - COSTS_KEEP_SECONDS,))

    # ---- what a request needs ----

    @staticmethod
    def api_key() -> str:
        return os.environ.get("ANTHROPIC_API_KEY", "").strip()

    def _client(self) -> Any:
        if self.client is None:
            self.client = anthropic.AsyncAnthropic(api_key=self.api_key(), max_retries=engine.CLIENT_RETRIES, timeout=engine.client_timeout())
        return self.client

    def library(self, settings: dict) -> Library:
        """The server's sources that have a copy ready, in the order the server lists them."""
        shelves = []
        for row in settings["sources"]:
            index = self.copies.index(Source.of(row))
            if index is not None:
                shelves.append(Shelf(row["name"], index))
        return Library(shelves)

    def unready(self, settings: dict) -> str | None:
        """Why no request can run in this server now, or None."""
        if not self.api_key():
            return NO_KEY
        if not settings["sources"]:
            return NO_WIKI
        if not self.library(settings).shelves:
            return BUILDING
        return None

    @staticmethod
    def wording(settings: dict) -> dict[str, str]:
        """What fills the placeholders in the model's wording: the server's subject, and its sources' names."""
        return {**settings["subject"], "wiki": prompts.names([row["name"] for row in settings["sources"]])}

    def footer(self, template: str, result: engine.Answer, library: Library, settings: dict) -> str:
        """A line under an answer, naming the sources it cited, or all of the server's when it cited none of them."""
        cited: list[str] = []
        for _, url in result.sources:
            name = library.name_of(url)
            if name is not None and name not in cited:
                cited.append(name)
        return prompts.fill(template, {"sources": prompts.names(cited or [row["name"] for row in settings["sources"]])})

    def _tools(self, command: str, settings: dict) -> list[dict]:
        tools = prompts.wiki_tools(self.wording(settings))
        if command in ("brainstorm", "ask"):
            tools.append(prompts.SKETCH_TOOL)
        if command == "ask":
            tools.append(prompts.web_search_tool([row["address"] for row in settings["sources"]]))
        if command == "make_model":
            tools.append(prompts.RENDER_TOOL)
        return tools

    async def ask_model(self, command: str, question: str, settings: dict, library: Library, *, auto: bool = False) -> engine.Answer:
        """Run one request. /how_do_i and /brainstorm send off-purpose questions to /ask, and the last two can draw."""
        model, effort = settings["models"][command], settings["efforts"][command]
        if not library.shelves:
            return engine.Answer(status="unavailable", model=model)
        values = self.wording(settings)
        parts = [prompts.fill(prompts.PROMPTS[command], values), prompts.fill(settings["rules"], values)]
        if auto:
            parts.append(prompts.fill(prompts.AUTO_NOTE, values))
        handlers: dict[str, engine.Handler] = {}
        if command in ("brainstorm", "ask"):
            handlers["draw_sketch"] = engine.draw_sketch
        if command == "make_model":
            handlers["render_model"] = modelmaker.render_model
        return await engine.converse(
            self._client(), library, question, system=prompts.joined(*parts), tools=self._tools(command, settings), model=model, effort=effort,
            max_rounds=ROUNDS[command], auto=auto, redirect=command in ("how_do_i", "brainstorm"), handlers=handlers,
            max_tokens=modelmaker.MAX_TOKENS if command == "make_model" else engine.MAX_TOKENS, tool_limits={"render_model": modelmaker.MAX_RENDERS},
        )

    # ---- limits ----

    async def take_slot(self, guild_id: int, user_id: int, limit: int) -> tuple[str | None, int]:
        """(why the person can't ask now, the day the request counted on). Counts the request when they can."""
        now = time.monotonic()
        if len(self._bursts) > BURSTS_REMEMBERED:
            for person in [person for person, times in self._bursts.items() if not times or now - times[-1] > BURST_SECONDS]:
                del self._bursts[person]
        times = self._bursts.setdefault(user_id, deque())
        while times and now - times[0] > BURST_SECONDS:
            times.popleft()
        if len(times) >= BURST_COUNT:
            return BURST, 0
        times.append(now)  # before the wait below, so a second request at the same moment counts this one
        day = utc_day()
        counted = await self.db.run(
            "INSERT INTO answer_uses (guild_id, user_id, day, uses) VALUES (?, ?, ?, 1) "
            "ON CONFLICT (guild_id, user_id, day) DO UPDATE SET uses = uses + 1 WHERE answer_uses.uses < ?",
            (guild_id, user_id, day, limit),
        )
        if not counted:
            try:
                times.remove(now)
            except ValueError:
                pass
            return DAILY.format(limit=limit), day
        return None, day

    async def refund(self, guild_id: int, user_id: int, day: int) -> bool:
        """Give back the daily slot of a request nothing was billed for. Returns whether it came back."""
        return bool(await self.db.run("UPDATE answer_uses SET uses = uses - 1 WHERE guild_id = ? AND user_id = ? AND day = ? AND uses > 0", (guild_id, user_id, day)))

    async def refund_redirect(self, guild_id: int, user_id: int, day: int) -> bool:
        """Give back the slot of a question sent to /ask, for the first REDIRECTS_FREE of them in the person's day. Each one was billed, so
        past those it counts. Returns whether it came back."""
        return bool(await self.db.run(
            "UPDATE answer_uses SET uses = uses - 1, redirects = redirects + 1 WHERE guild_id = ? AND user_id = ? AND day = ? AND uses > 0 AND redirects < ?",
            (guild_id, user_id, day, REDIRECTS_FREE)))

    async def take_auto_slot(self, guild_id: int, limit: int) -> bool:
        """Count one automatic answer against the server's daily number. It isn't given back, whatever the answer turns out to be."""
        return bool(await self.db.run(
            "INSERT INTO answer_auto_days (guild_id, day, answers) VALUES (?, ?, 1) "
            "ON CONFLICT (guild_id, day) DO UPDATE SET answers = answers + 1 WHERE answer_auto_days.answers < ?",
            (guild_id, utc_day(), limit),
        ))

    # ---- screening ----

    @staticmethod
    def _anyone(guild: Any) -> Any:
        """An ordinary member, holding only @everyone, for automod's word rule to read text as. The model writes the answer, so whoever
        asked, it's screened as anyone's message would be, staff and exempt roles included, as the old bot's screen was."""
        return types.SimpleNamespace(id=0, guild=guild, roles=[guild.default_role], guild_permissions=guild.default_role.permissions)

    async def _automod_hit(self, member: Any, texts: list[str]) -> bool:
        automod = self.bot.module("automod")
        if automod is None:
            return False
        for words in texts:
            if words and words.strip() and await automod.word_problem(member, words):
                return True
        return False

    @staticmethod
    def term_lists(guild_id: int) -> screening.TermLists | None:
        """The host's term lists, when their folder has this server's file. Otherwise None, and the server's automod word rule screens."""
        lists = screening.active()
        return lists if lists is not None and lists.config(guild_id) is not None else None

    async def request_blocked(self, guild: Any, question: str) -> set[str] | bool:
        """Whether the server's word lists refuse a request, checked for at most REQUEST_SCREEN_SECONDS."""
        lists = self.term_lists(guild.id)
        try:
            if lists is not None:
                return await asyncio.wait_for(lists.blocked(guild.id, [question]), REQUEST_SCREEN_SECONDS)
            return await asyncio.wait_for(self._automod_hit(self._anyone(guild), [question]), REQUEST_SCREEN_SECONDS)
        except TimeoutError:
            return False

    async def screen_answer(self, guild: Any, result: engine.Answer, title: str, *, card_title: bool = True) -> tuple[engine.Answer, str]:
        """Check an answer before it's posted. A drawing or a model file can't be censored, so a blocked word in one stops the post, and
        the card's text gets what the filter does to a member's message. Without the host's term lists for this server nothing can be
        censored, so a hit anywhere stops the post. Returns the answer and the card title to use."""
        lists = self.term_lists(guild.id)
        attached = [*result.screen_text, *(name for name, _ in result.files)]
        titles = [source_title for source_title, _ in result.sources]
        if lists is None:
            if await self._automod_hit(self._anyone(guild), [*attached, *([title] if card_title else []), result.text, result.image_caption, *titles]):
                result.status = "blocked_reply"
            return result, title
        found = await lists.blocked(guild.id, attached)
        if found:
            log.info("An answer in %s was held back for %s", guild.id, sorted(found)[:5])
            result.status = "blocked_reply"
            return result, title
        if card_title:
            title, result.text, result.image_caption, *titles = await lists.censor(guild.id, [title, result.text, result.image_caption, *titles])
        else:
            result.text, *titles = await lists.censor(guild.id, [result.text, *titles])
        result.sources = [(new_title, url) for new_title, (_, url) in zip(titles, result.sources)]
        return result, title

    async def screened(self, guild: Any, result: engine.Answer, title: str, *, card_title: bool = True) -> tuple[engine.Answer, str]:
        """screen_answer, where a screen that fails holds the answer back instead of ending the request before its cost is logged."""
        try:
            return await self.screen_answer(guild, result, title, card_title=card_title)
        except Exception:
            log.exception("Screening an answer in %s failed, so it was held back", guild.id)
            result.status = "blocked_reply"
            return result, title

    def _warm_once(self) -> None:
        lists = screening.active()
        if lists is None or self._warmed:
            return
        self._warmed = True
        guild_ids = [guild.id for guild in self.bot.guilds if self.on_in(guild.id)]
        self._spawn(lists.warm(guild_ids))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._warm_once()

    # ---- the cost log ----

    async def record(self, guild_id: int, user_id: int | None, command: str, result: engine.Answer, seconds: float, *, where: str) -> None:
        used = result.usage
        log.info(
            "%s %s status=%s model=%s rounds=%d tools=%d web=%d image=%s files=%d in=%d out=%d cache_read=%d cache_write=%d cost=$%.4f %.1fs",
            command, where, result.status, result.model, result.rounds, result.tool_calls, used["web_search_requests"], result.image is not None,
            len(result.files), used["input_tokens"], used["output_tokens"], used["cache_read_input_tokens"], used["cache_creation_input_tokens"],
            result.cost, seconds,
        )
        await self.db.run(
            "INSERT INTO answer_costs (guild_id, user_id, command, status, model, rounds, tool_calls, web_searches, image, files, input_tokens, "
            "output_tokens, cache_read, cache_write, cost, seconds, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, command, result.status, result.model, result.rounds, result.tool_calls, used["web_search_requests"], int(result.image is not None),
             len(result.files), used["input_tokens"], used["output_tokens"], used["cache_read_input_tokens"], used["cache_creation_input_tokens"],
             round(result.cost, 6), round(seconds, 1), time.time()),
        )

    async def month_spent(self, guild_id: int) -> float:
        """What this server's answers have cost since the month began, UTC."""
        return await self.db.scalar("SELECT COALESCE(SUM(cost), 0) FROM answer_costs WHERE guild_id = ? AND at >= ?", (guild_id, _month_start(time.time())))

    async def spend(self, guild_id: int, now: float | None = None) -> dict:
        """What this server's answers cost this month and last month, from the cost log."""
        now = time.time() if now is None else now
        this_month, last_month = _month_start(now), _month_start(now, 1)
        rows = await self.db.all(
            "SELECT command, COUNT(*) AS requests, COALESCE(SUM(cost), 0) AS cost FROM answer_costs WHERE guild_id = ? AND at >= ? GROUP BY command ORDER BY command",
            (guild_id, this_month))
        before = await self.db.one(
            "SELECT COUNT(*) AS requests, COALESCE(SUM(cost), 0) AS cost FROM answer_costs WHERE guild_id = ? AND at >= ? AND at < ?", (guild_id, last_month, this_month))
        return {
            "month": {"cost": round(sum(row["cost"] for row in rows), 4), "requests": sum(row["requests"] for row in rows),
                      "by_command": [{"command": row["command"], "requests": row["requests"], "cost": round(row["cost"], 4)} for row in rows]},
            "last_month": {"cost": round(before["cost"], 4), "requests": before["requests"]},
        }

    # ---- running a request ----

    async def run_request(self, ctx: commands.Context, command: str, question: str) -> None:
        """The steps every AI command shares: checks, limits, the answer, the cost log and the reply."""
        question = question.strip()
        guild = ctx.guild
        # A slash command works where its user can't post, and the answer card carries their words in the bot's name.
        refusal = access.post_refusal(ctx.author, ctx.channel)
        if refusal:
            await ctx.fail(refusal)
            return
        settings = self.cfg(guild.id)
        problem = self.unready(settings)
        if problem is None and command == "make_model" and not modelmaker.available():
            problem = NO_MODELS
        if problem:
            await ctx.fail(problem)
            return
        if settings["monthly_limit"] and await self.month_spent(guild.id) >= settings["monthly_limit"]:
            await ctx.fail(MONTH_SPENT.format(limit=settings["monthly_limit"]))
            return
        blocked = await self.request_blocked(guild, question)
        if blocked:
            log.info("%s user=%s status=blocked_request terms=%s", command, ctx.author.id, sorted(blocked)[:5] if isinstance(blocked, set) else "automod")
            await ctx.fail(RULES_NOTICE)
            return
        refusal, day = await self.take_slot(guild.id, ctx.author.id, settings["daily_limit"])
        if refusal:
            await ctx.fail(refusal)
            return
        started = time.monotonic()
        try:
            await ctx.defer()  # a slash command shows the bot thinking, and a typed one gets the typing mark below
        except discord.HTTPException:
            await self.refund(guild.id, ctx.author.id, day)  # the interaction ran out before anything was asked
            raise
        library = self.library(settings)
        async with ctx.typing() if ctx.interaction is None else _nothing():
            async with self.slots:
                result = await self.ask_model(command, question, settings, library)
            title = question
            if result.status == "ok":
                result, title = await self.screened(guild, result, question)
        await self.record(guild.id, ctx.author.id, command, result, time.monotonic() - started, where=f"user={ctx.author.id}")
        if result.status == "redirect":
            refunded = await self.refund_redirect(guild.id, ctx.author.id, day)
            words = REDIRECT.format(ask=await self._mention(guild, "ask"))
            await self.send_back(ctx, f"{words} {NOT_COUNTED}" if refunded else words)
            return
        if result.status != "ok":
            # Only a request nothing was billed for gives its slot back. One that ran to the token cap or failed after a billed round counts.
            refunded = result.cost == 0 and await self.refund(guild.id, ctx.author.id, day)
            words = STATUS_TEXT.get(result.status, STATUS_TEXT["error"])
            await self.send_back(ctx, f"{words} {NOT_COUNTED}" if refunded else words)
            return
        footer = self.footer(settings["footers"][command], result, library, settings)
        await self.send_back(ctx, None, embed=answer_card(title, result, footer), files=answer_files(result))

    @staticmethod
    async def send_back(ctx: commands.Context, content: str | None, **parts: Any) -> None:
        """The reply to a request. Discord takes a slash command's reply for 15 minutes, and a long answer can outlast that. Near the end
        the reply goes to the channel and names the person, since the command itself only shows that it didn't respond, and a reply sent
        in the last seconds can fail partway through its upload."""
        interaction = ctx.interaction
        if interaction is None or discord.utils.utcnow() < interaction.expires_at - LATE_REPLY_MARGIN:
            await ctx.send(content, allowed_mentions=discord.AllowedMentions.none(), **parts)
            return
        await ctx.channel.send(f"{ctx.author.mention} {content or ''}".strip(), allowed_mentions=discord.AllowedMentions(users=[ctx.author]), **parts)

    async def _mention(self, guild: Any, name: str) -> str:
        """A command as a link people can click, once its ID is known."""
        known = self._mentions.get(guild.id) or {}
        if name not in known:
            try:
                registered = await self.bot.tree.fetch_commands(guild=discord.Object(guild.id))
                known = {command.name: command.mention for command in registered}
                self._mentions[guild.id] = known
            except discord.HTTPException:
                known = {}
        return known.get(name, f"/{name}")

    @commands.hybrid_command(name="how_do_i", description="Ask how to do something, answered from the server's wiki")
    @commands.guild_only()
    @app_commands.describe(question="Your question, with your model if it matters, such as: how do I replace the nozzle?")
    async def how_do_i(self, ctx: commands.Context, *, question: commands.Range[str, 5, 1000]) -> None:
        await self.run_request(ctx, "how_do_i", question)

    @commands.hybrid_command(name="brainstorm", description="Plan a 3D printing or laser project: approaches, filament, and print steps")
    @commands.guild_only()
    @app_commands.describe(idea="What you want to make and what it's for. Ask for a sketch if you want a drawing.")
    async def brainstorm(self, ctx: commands.Context, *, idea: commands.Range[str, 10, 1500]) -> None:
        await self.run_request(ctx, "brainstorm", idea)

    @commands.hybrid_command(name="ask", description="Ask anything about 3D printing or lasers: buying advice, other brands, settings, and more")
    @commands.guild_only()
    @app_commands.describe(question="Your question, such as: which filament holds up best outdoors?")
    async def ask(self, ctx: commands.Context, *, question: commands.Range[str, 5, 1500]) -> None:
        await self.run_request(ctx, "ask", question)

    @commands.hybrid_command(name="make_model", description="Get a 3D-printable model file made from your description")
    @commands.guild_only()
    @app_commands.describe(description="What to make, with sizes if you know them, such as: a 70 mm coaster with a raised star")
    async def make_model(self, ctx: commands.Context, *, description: commands.Range[str, 10, 1000]) -> None:
        await self.run_request(ctx, "make_model", description)

    # ---- the copies ----

    @staticmethod
    def refresh_text(summary: dict) -> str:
        if summary.get("already_running"):
            return UPDATE_RUNNING
        if not summary.get("ok"):
            return f"The wiki update failed. {summary.get('error') or 'Something went wrong.'}"
        failed = sum((summary.get("failed") or {}).values())
        return (f"Wiki updated in {summary['seconds']}s: {summary['updated']} pages downloaded, {summary['unchanged']} unchanged, "
                f"{summary['removed']} removed, {failed} failed. The copy now holds {summary['pages']} pages.")

    async def refresh_sources(self, rows: list[dict]) -> list[str]:
        """Update these sources one after another. One line about each, named when there's more than one."""
        lines = []
        for row in rows:
            try:
                source = Source.of(row)
                summary = await self.copies.refresh(source, self.outside, wanted=self.wanted(source))
            except Exception:
                log.exception("An update of %s failed", row["address"])
                summary = {"ok": False, "error": "Something went wrong. It's been logged."}
            lines.append(self.refresh_text(summary) if len(rows) == 1 else f"{row['name']}: {self.refresh_text(summary)}")
        return lines

    @commands.hybrid_command(name="refresh_wiki", description="Update the bot's copies of the server's wikis now")
    @commands.guild_only()
    @requires("administrator")
    async def refresh_wiki(self, ctx: commands.Context) -> None:
        if not access.is_admin(ctx.author):
            await ctx.fail(access.ONLY_ADMINS)
            return
        rows = self.cfg(ctx.guild.id)["sources"]
        if not rows:
            await ctx.fail(NO_WIKI)
            return
        if any(self.copies.running(Source.of(row)) for row in rows):
            await ctx.fail(UPDATE_RUNNING)
            return
        await ctx.defer(ephemeral=True)
        message = "\n".join(await self.refresh_sources(rows))
        try:
            await ctx.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())  # the text names addresses a site wrote
        except discord.HTTPException:
            log.info("refresh_wiki in %s: %s", ctx.guild.id, message)  # a full update can outlast the 15 minutes an answer has

    # ---- automatic answers ----

    @commands.hybrid_command(name="auto_answer", description="Turn automatic wiki answers to new posts in a forum on or off")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(forum="The forum channel", enabled="On to answer each new post from the wiki, off to stop")
    async def auto_answer(self, ctx: commands.Context, forum: discord.ForumChannel, enabled: bool) -> None:
        if not access.is_admin(ctx.author):
            await ctx.fail(access.ONLY_ADMINS)
            return
        forums = list(self.cfg(ctx.guild.id)["auto_answer_forums"])
        if enabled and forum.id not in forums:
            forums.append(forum.id)
        if not enabled:
            forums = [found for found in forums if found != forum.id]
        saved = await self.bot.settings.save(ctx.guild.id, self.key, {"auto_answer_forums": sorted(forums)}, actor=self.bot.actor(ctx.author),
                                             action="answers.auto_answer")
        lines = [f"Automatic answers are now **{'on' if enabled else 'off'}** in {forum.mention}."]
        if enabled:
            allowed = forum.permissions_for(ctx.guild.me)
            if not (allowed.send_messages_in_threads and allowed.embed_links and allowed.read_message_history):
                lines.append("I'm missing Send Messages in Threads, Embed Links or Read Message History there, so I can't post answers until that's fixed.")
            if not self.bot.reads_message_text:
                lines.append("Discord hasn't given the bot Message Content, so it can't read new posts to answer them.")
            unready = self.unready(saved)
            if unready in (NO_KEY, NO_WIKI):
                lines.append(f"{unready} Nothing will be posted until then.")
        shown = [f"<#{found}>" for found in saved["auto_answer_forums"]]
        lines.append(f"Forums with automatic answers: {', '.join(shown) if shown else 'none'}.")
        await ctx.send("\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener("on_message")
    async def answer_new_post(self, message: discord.Message) -> None:
        """A forum post's first message has the same ID as its thread."""
        if message.guild is None or message.author.bot or message.id != message.channel.id:
            return
        if not self.on_in(message.guild.id) or not self.bot.reads_message_text:
            return  # without Message Content the post's text reads as empty, which says nothing about the post
        forums = self.cfg(message.guild.id)["auto_answer_forums"]
        parent = getattr(message.channel, "parent_id", None)
        if not forums or (parent is not None and parent not in forums):
            return
        self._spawn(self.answer_post(message))

    async def answer_post(self, message: discord.Message) -> None:
        """One reply to a new post in a chosen forum, built from its title and first message, posted only when it cites a source."""
        guild = message.guild
        channel = message.channel
        if not isinstance(channel, discord.Thread):
            try:
                channel = await self.bot.fetch_channel(message.channel.id)
            except discord.HTTPException:
                return
        if not isinstance(channel, discord.Thread):
            return
        settings = self.cfg(guild.id)
        if channel.parent_id not in settings["auto_answer_forums"] or channel.id in self._threads:
            return
        self._threads[channel.id] = None
        while len(self._threads) > THREADS_REMEMBERED:
            self._threads.popitem(last=False)
        if any(tag.moderated for tag in channel.applied_tags):
            return  # staff-only tags mark guides and announcements, not questions
        if self.unready(settings):
            return
        question = question_from_post(channel.name, message.content, len(message.attachments))
        if question is None:
            log.info("auto_answer thread=%s skipped: too little text", channel.id)
            return
        if settings["monthly_limit"] and await self.month_spent(guild.id) >= settings["monthly_limit"]:
            log.info("auto_answer thread=%s skipped: the server spent its monthly budget of $%d", channel.id, settings["monthly_limit"])
            return
        if not await self.take_auto_slot(guild.id, settings["auto_answer_daily_limit"]):
            log.info("auto_answer thread=%s skipped: the daily number of %d is used up", channel.id, settings["auto_answer_daily_limit"])
            return
        started = time.monotonic()
        library = self.library(settings)
        async with self.slots:
            result = await self.ask_model("how_do_i", question, settings, library, auto=True)
        await self.record(guild.id, None, "auto_answer", result, time.monotonic() - started, where=f"thread={channel.id}")
        if result.status != "ok" or not result.sources:
            return  # nothing from the sources, so the post is left to the helpers
        result, _ = await self.screened(guild, result, channel.name, card_title=False)
        if result.status != "ok":
            return
        intro = self.footer(settings["auto_intro"], result, library, settings)
        try:
            await message.reply(content=intro or None, embed=answer_card(channel.name, result, self.footer(settings["footers"]["how_do_i"], result, library, settings)),
                                mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            log.info("auto_answer thread=%s reply failed: %s", channel.id, error)

    # ---- the dashboard ----

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {"auto_answer_forums": set(settings.get("auto_answer_forums") or [])}

    def api(self, router: Any) -> None:
        router.get("/answers/status", self._status)
        router.post("/answers/refresh", self._refresh_now)

    async def _status(self, req: Any) -> dict:
        settings = self.cfg(req.guild_id)
        automod = self.bot.module("automod")
        words = automod.cfg(req.guild_id)["words"] if automod is not None else None
        return {
            "allowed": servers.allowed(req.guild_id),
            "model_choices": {command: list(models) for command, models in MODEL_CHOICES.items()},
            "key_set": bool(self.api_key()),
            "message_text": bool(self.bot.reads_message_text),
            "models_ready": modelmaker.available(),
            "sketches_ready": engine.sketches_available(),
            "screening": {"mode": "lists" if self.term_lists(req.guild_id) is not None else "automod", "load_failed": bool(screening.problem()),  # the reason names a path on the host, so it stays in the host's log
                          "lists_loaded": screening.active() is not None, "word_rule": bool(words and words["enabled"] and not words["dry_run"])},
            "sources": [{"name": row["name"], **(await self.copies.status(Source.of(row)))} for row in settings["sources"]],
            "spend": await self.spend(req.guild_id),
            "is_admin": req.is_admin,
        }

    async def _refresh_now(self, req: Any) -> dict:
        """Update one source, named by its place in the list, or all of the server's."""
        if not req.is_admin:
            raise ApiError(403, access.ONLY_ADMINS)
        rows = self.cfg(req.guild_id)["sources"]
        form = await req.json()
        if form.get("source") is not None:
            number = whole_number(form.get("source"), len(rows) - 1) if rows else None
            if number is None:
                raise ApiError(404, "That source isn't on the list.")
            rows = [rows[number]]
        if not rows:
            raise ApiError(400, NO_WIKI)
        if any(self.copies.running(Source.of(row)) for row in rows):
            raise ApiError(409, UPDATE_RUNNING)
        self._spawn(self.refresh_sources(rows))
        await req.record("answers.refresh", detail=", ".join(row["name"] for row in rows))
        return {"started": True}

    async def seed_demo(self, guild_id: int) -> None:
        """A sample source with its copy, an automatic-answer forum and a month of costs, so the demo page opens with something on it. The
        demo's host lets its sample server use the pack."""
        servers.use(servers.listed() | {guild_id})
        forum = next((row["id"] for row in getattr(self.bot.ops, "channels", []) if row.get("type") == 15), None)
        row = {"name": "the Sample Wiki", "kind": SITEMAP, "address": "https://example.com/sitemap.xml", "prefix": "/en/", "include": [], "exclude": ["/en/drafts/*"],
               "selector": "", "refresh_hours": 24}
        source = Source.of(row)
        copy_id = await self.copies._copy_id(source, create=True)  # the pages go in first, so the save below finds a copy and doesn't fetch one
        now = time.time()
        pages = [
            ("https://example.com/en/printer/nozzle", "Replacing the nozzle", "## Before you start {#before}\nLet the hotend cool and power off the printer.\n## Steps {#steps}\nUnscrew the nozzle with the wrench and fit the new one."),
            ("https://example.com/en/printer/plate", "Cleaning the build plate", "Wash the plate with dish soap and warm water, then dry it with a lint-free cloth."),
        ]
        await self.copies.db.many("INSERT OR REPLACE INTO wiki_pages (copy_id, url, title, content, lastmod, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                                  [(copy_id, url, title, content, "2026-10-01", now) for url, title, content in pages])
        summary = {"ok": True, "sitemap_pages": 2, "updated": 2, "unchanged": 0, "removed": 0, "failed": {}, "left_out": 0, "pages": 2, "sections": 3, "seconds": 1.4}
        await self.copies.db.run("UPDATE wiki_copies SET refreshed_at = ?, summary = ? WHERE id = ?", (now - 3600, json.dumps(summary), copy_id))
        await self.bot.settings.save(guild_id, self.key, {"enabled": True, "sources": [row], "auto_answer_forums": [forum] if forum else [],
                                                          "subject": {**prompts.SUBJECT, "community": "the Demo Server", "maker": "Sample"}})
        await self.copies.load(source)
        samples = [("how_do_i", "ok", "claude-haiku-5-5", 0.0029), ("ask", "ok", "claude-sonnet-5-5", 0.0912), ("brainstorm", "redirect", "claude-sonnet-5-5", 0.0061),
                   ("auto_answer", "ok", "claude-haiku-5-5", 0.0035), ("make_model", "ok", "claude-sonnet-5-5", 0.0442)]
        for number, (command, status, model, cost) in enumerate(samples):
            await self.db.run(
                "INSERT INTO answer_costs (guild_id, user_id, command, status, model, rounds, tool_calls, input_tokens, output_tokens, cost, seconds, at) "
                "VALUES (?, ?, ?, ?, ?, 3, 4, 9000, 700, ?, 12.5, ?)",
                (guild_id, None if command == "auto_answer" else 900000000000000003, command, status, model, cost, max(_month_start(now), now - 3600 * (number + 1))))
