"""Translation and conversions, a command pack.

/translate, the Translate menu, messages that start with "translate" and reactions with a country's flag translate through DeepL
(core/translator.py). /currency and /units convert, and so do messages that ask, such as "20 bucks in euros" or "5 km to miles"
(core/conversions.py). Each member picks the language their one-tap translations go to, and /export_emojis downloads the server's custom
emojis as ZIP files.

The answers to messages and flags come from the bot reading what members type, so they wait for Message Content, and a server can switch
each kind off or leave channels out. The bot never answers its own messages, or what its own webhooks post.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import re
import time
import zipfile
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core import conversions as convert
from ..core import translator as tr
from ..core.module import Module, requires
from ..core.outside import Outside, OutsideError
from ..core.shortcuts import SHORTCUTS, TAG_FIRST_SHORTCUTS
from ..core.validate import Field, Schema, boolean, integer, snowflakes, text

log = logging.getLogger(__name__)

NONE = discord.AllowedMentions.none()
RATE_SOURCES = ("https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1", "https://latest.currency-api.pages.dev/v1")
RATES_FRESH_SECONDS = 3600
RATES_RETRY_SECONDS = 300  # after a failed fetch, the rates on hand serve this long before the next try
RATES_TIMEOUT = 10.0
RATES_KEY = "translation_rates"  # the last rates fetched, for a start when the source is down
MOST_CURRENCIES = 5000
AUTOCOMPLETE_WAIT = 2.0  # Discord drops an autocomplete answer after 3 seconds
DELETE_KIND = "translation.delete"
LONGEST_DELETE = 3600
DEFAULT_DELETE = 120
RESET_WORDS = ("reset", "clear", "auto", "none", "default")
EXPORT_PART_BYTES = 24 * 1024 * 1024  # memory held for one ZIP at a time, below Discord's smallest upload limit
EXPORT_MARGIN = 64 * 1024
EMOJI_DOWNLOAD_SECONDS = 15.0  # one emoji that stalls is left out after this long, as before
GLOBE = "\U0001f310"


def _shown(value: str) -> str:
    """Something a person typed, for a refusal that repeats it."""
    return str(value).replace("`", "")[:60]


def _escaped(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


class Amount(commands.Converter):
    """A command's amount as people write it, such as 5, 2.5, 1,75, 3/4, 1 1/2 or 1-1/2. Becomes a number."""

    async def convert(self, ctx: commands.Context, argument: str) -> float:
        value = convert.parse_amount(str(argument).strip())
        if value is None:
            raise commands.BadArgument(f"`{_shown(argument)}` isn't an amount. Write it as 5, 2.5, 3/4 or 1 1/2.")
        return value


def emoji_file_type(data: bytes, animated: bool) -> str:
    """The file extension the bytes call for. Discord sends an animated emoji as WebP."""
    if data.startswith(b"GIF8"):
        return "gif"
    if data.startswith(b"\x89PNG"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    return "gif" if animated else "png"


def zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def clean_rates(data: Any) -> dict[str, float]:
    """Units of each currency per US dollar, keeping only positive numbers, since a zero rate would divide by zero."""
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for code, value in data.items():
        if isinstance(code, str) and 0 < len(code) <= 20 and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            out[code.lower()] = float(value)
            if len(out) >= MOST_CURRENCIES:
                break
    return out


def clean_date(value: Any) -> str | None:
    """The day a rate list is for, as the source writes it, such as 2026-11-06. Anything else shows no date."""
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None


def clean_names(data: Any) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    return {code.lower(): name[:100] for code, name in list(data.items())[:MOST_CURRENCIES] if isinstance(code, str) and isinstance(name, str)}


class Translation(Module):
    key = "translation"
    category = "Utilities"
    command_pack = True
    schema = Schema({
        "enabled": Field(False, boolean),
        "answer_translate": Field(True, boolean),
        "answer_currency": Field(True, boolean),
        "answer_units": Field(True, boolean),
        "flag_translations": Field(True, boolean),
        "flag_delete_seconds": Field(DEFAULT_DELETE, integer(0, LONGEST_DELETE)),
        "ignored_channels": Field([], snowflakes()),
        "deepl_token": Field("", text(4000), secret=True),
    })
    migrations = [
        """
        CREATE TABLE translate_languages (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            language TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.translator = tr.Translator(None)
        self.outside = Outside()
        self.throttle = tr.Throttle()
        self.flags = tr.FlagMarks()
        self._rates: dict[str, float] | None = None
        self._rate_date: str | None = None
        self._names: dict[str, str] | None = None
        self._fetched_at = 0.0
        self._failed_at = 0.0
        self._rates_lock = asyncio.Lock()
        self._exporting: set[int] = set()
        self._menu = app_commands.ContextMenu(name="Translate", callback=self.translate_menu)
        self._menu.guild_only = True

    @property
    def outside(self) -> Any:
        return self._outside

    @outside.setter
    def outside(self, value: Any) -> None:
        """The demo and the tests put a stand-in here, and translations go through the same one."""
        self._outside = value
        self.translator.outside = value

    async def on_load(self) -> None:
        self.bot.timers.handler(DELETE_KIND, self._delete_due)
        self.bot.tree.add_command(self._menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self._menu.name, type=self._menu.type)
        await self.outside.close()

    async def seed_demo(self, guild_id: int) -> None:
        staff = next((row["id"] for row in getattr(self.bot.ops, "channels", []) if row.get("name") == "mod-chat"), None)
        await self.bot.settings.save(guild_id, self.key, {"enabled": True, "ignored_channels": [staff] if staff else []})

    # ---- shared pieces ----

    @staticmethod
    def _places(channel: Any) -> set[int]:
        parent = getattr(channel, "parent", None)
        places = {channel.id} | ({parent.id} if parent is not None else set())
        category = getattr(parent if parent is not None else channel, "category_id", None)
        if category:
            places.add(category)
        return places

    def _left_out(self, guild_id: int, channel: Any) -> bool:
        return bool(self._places(channel) & set(self.cfg(guild_id)["ignored_channels"]))

    def _own(self, message: Any) -> bool:
        """The bot's own message, or one its own webhooks posted, such as a moved message or a feed entry."""
        me = self.bot.user
        if me is not None and message.author.id == me.id:
            return True
        return getattr(message, "webhook_id", None) is not None and getattr(message, "application_id", None) is not None and message.application_id == self.bot.application_id

    @staticmethod
    def _can_answer(channel: Any, *, embeds: bool = False) -> bool:
        """Whether the bot can reply in this channel, so nothing is asked of DeepL or Discord for a reply that can't go out."""
        me = getattr(getattr(channel, "guild", None), "me", None)
        if me is None:
            return False
        allowed = access.permissions_in(me, channel)
        posting = allowed.send_messages_in_threads if isinstance(channel, discord.Thread) else allowed.send_messages
        return bool(allowed.view_channel and posting and allowed.read_message_history and (allowed.embed_links or not embeds))

    def _token(self, guild_id: int) -> str | None:
        return self.cfg(guild_id)["deepl_token"] or None

    async def saved_language(self, guild_id: int, user_id: int) -> str | None:
        row = await self.db.one("SELECT language FROM translate_languages WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        code = row["language"].upper() if row and isinstance(row["language"], str) else None
        return code if code in tr.TARGET_CODES else None

    async def default_target(self, guild_id: int, user_id: int, locale: Any = None) -> str:
        """Where a translation with no language named goes: the member's saved language here, then their Discord language, then English."""
        return await self.saved_language(guild_id, user_id) or tr.locale_target(locale) or tr.DEFAULT_TARGET

    @staticmethod
    def _messages(result: dict, header: str, title: str) -> list[dict]:
        return tr.messages_for(f"{header}\n{result['text']}", title, result["text"])

    @staticmethod
    def _arrow(result: dict) -> tuple[str, str]:
        source, target = tr.language_name(result["source_lang"]), tr.language_name(result["target_lang"])
        return f"{GLOBE} **{source} → {target}**", f"{GLOBE} {source} → {target}"

    # ---- exchange rates ----

    async def _fetch_json(self, path: str) -> dict:
        last: Exception = OutsideError("No rate source answered.")
        for base in RATE_SOURCES:
            try:
                reply = await self.outside.get(f"{base}/{path}", timeout=RATES_TIMEOUT)
                if reply.status != 200:
                    raise OutsideError(f"{base} answered {reply.status}.")
                data = reply.json()
                if not isinstance(data, dict):
                    raise OutsideError(f"{base} sent something that isn't a rate list.")
                return data
            except OutsideError as error:
                last = error
        raise last

    async def ensure_rates(self) -> None:
        """Rates no older than an hour, fetched on first use. When the source is down, the rates on hand stay, or the last ones saved."""
        if self._rates is not None and time.time() - self._fetched_at < RATES_FRESH_SECONDS:
            return
        async with self._rates_lock:
            now = time.time()
            if self._rates is not None and now - self._fetched_at < RATES_FRESH_SECONDS:
                return
            if now - self._failed_at >= RATES_RETRY_SECONDS:
                try:
                    data = await self._fetch_json("currencies/usd.json")
                    rates = clean_rates(data.get("usd"))
                    if not rates:
                        raise OutsideError("The rate list was empty.")
                    self._rates, self._rate_date, self._fetched_at = rates, clean_date(data.get("date")), time.time()
                    if self._names is None:
                        try:
                            self._names = clean_names(await self._fetch_json("currencies.json"))
                        except OutsideError as error:
                            log.info("Currency names couldn't be fetched: %s", error)
                    await self.db.run("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                                      (RATES_KEY, json.dumps({"rates": self._rates, "date": self._rate_date, "names": self._names})))
                    return
                except OutsideError as error:
                    self._failed_at = time.time()
                    log.info("Exchange rates couldn't be fetched: %s", error)
            if self._rates is None:
                saved = await self.db.scalar("SELECT value FROM kv WHERE key = ?", (RATES_KEY,))
                try:
                    data = json.loads(saved) if saved else {}
                except ValueError:
                    data = {}
                rates = clean_rates(data.get("rates"))
                if rates:
                    self._rates, self._rate_date = rates, clean_date(data.get("date"))
                    self._names = self._names or clean_names(data.get("names"))

    def _rates_stale(self) -> bool:
        return self._rates is None or time.time() - self._fetched_at >= RATES_FRESH_SECONDS

    def rate(self, source: str, target: str) -> float | None:
        """How many of the target one of the source buys, when both are known."""
        rates = self._rates or {}
        if source not in rates or target not in rates:
            return None
        return rates[target] / rates[source]

    def _code(self, typed: str) -> str | None:
        """A currency from an option: a code, or a name such as euro."""
        code = typed.lower().strip()
        if self._rates and code in self._rates:
            return code
        return convert.currency_code(typed, self._rates)

    def _as_of(self) -> str:
        return f" (as of {self._rate_date})" if self._rate_date else ""

    # ---- /currency and /units ----

    @commands.hybrid_command(name="currency", description="Convert between currencies using current rates")
    @commands.guild_only()
    @app_commands.describe(amount="Amount to convert", from_currency="Source currency code (e.g., USD)", to_currency="Target currency code (e.g., EUR)")
    async def currency(self, ctx: commands.Context, amount: Amount, from_currency: str, to_currency: str) -> None:
        if not math.isfinite(amount):
            await ctx.fail("Give a number for the amount.")
            return
        public = access.can_post(ctx.author, ctx.channel)
        if self._rates_stale():
            await ctx.defer(ephemeral=not public)
        await self.ensure_rates()
        if not self._rates:
            await ctx.fail("The exchange rate service didn't answer. Try again in a few minutes.")
            return
        source, target = self._code(from_currency), self._code(to_currency)
        for typed, code in ((from_currency, source), (to_currency, target)):
            if code is None or code not in self._rates:
                await ctx.fail(f"`{_shown(typed)}` isn't a currency I know. Pick one from the list as you type.")
                return
        rate = self.rate(source, target)
        result = amount * rate
        if not math.isfinite(result):
            await ctx.fail("That amount is too large to convert.")
            return
        names = self._names
        source_label = _escaped(convert.currency_label(from_currency.strip(), source, names))
        target_label = _escaped(convert.currency_label(to_currency.strip(), target, names))
        await ctx.send(f"{convert.figure(amount, 2)} {source_label} = **{convert.figure(result, 2)}** {target_label}{self._as_of()}\n"
                       f"(1 {source_label} ≈ {rate} {target_label})", ephemeral=not public, allowed_mentions=NONE)

    @currency.autocomplete("from_currency")
    @currency.autocomplete("to_currency")
    async def _currency_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if self._rates is None:
            try:
                await asyncio.wait_for(asyncio.shield(self.ensure_rates()), AUTOCOMPLETE_WAIT)
            except asyncio.TimeoutError:
                return []
        typed = current.lower()
        found = []
        for code in sorted(self._rates or {}):
            shown = convert.currency_display(code, self._names)
            if typed in shown.lower():
                found.append(app_commands.Choice(name=shown[:100], value=code))
                if len(found) >= 25:
                    break
        return found

    @commands.hybrid_command(name="units", description="Convert units: temp, length, mass, volume, area, speed, time, storage, pressure, energy, and more")
    @commands.guild_only()
    @app_commands.describe(amount="Amount to convert", from_unit="Source unit (e.g., km, °F, kg, liters)", to_unit="Target unit (e.g., miles, °C, lb, gal)")
    async def units(self, ctx: commands.Context, amount: Amount, from_unit: str, to_unit: str) -> None:
        if not math.isfinite(amount):
            await ctx.fail("Give a number for the amount.")
            return
        result = convert.convert_units(amount, from_unit, to_unit)
        if result is None:
            await ctx.fail(convert.UNITS_HELP)
            return
        if not math.isfinite(result):
            await ctx.fail("That amount is too large to convert.")
            return
        public = access.can_post(ctx.author, ctx.channel)
        await ctx.send(f"{convert.figure(amount, 2)} {_escaped(from_unit.strip())} = **{convert.figure(result, 10)}** {_escaped(to_unit.strip())}",
                       ephemeral=not public, allowed_mentions=NONE)

    @units.autocomplete("from_unit")
    @units.autocomplete("to_unit")
    async def _unit_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        typed = current.lower()
        return [app_commands.Choice(name=unit, value=unit) for unit in convert.UNIT_CHOICES if typed in unit.lower()][:25]

    # ---- translating ----

    @commands.hybrid_command(name="translate", description="Translate text into another language with DeepL")
    @commands.guild_only()
    @app_commands.describe(text="The text to translate", to="Target language (e.g., Spanish, Japanese, French, or a code like ES)",
                           source="Source language (optional, defaults to auto-detect)")
    async def translate(self, ctx: commands.Context, text: commands.Range[str, 1, tr.MAX_INPUT], to: str, source: str = "auto") -> None:
        target = tr.resolve_target(to)
        if not target:
            await ctx.fail(f"Unknown target language: `{_shown(to)}`. Try a name like Spanish, Japanese or French, or a code like ES, JA or FR.")
            return
        if self.throttle.limited(ctx.author.id):
            await ctx.fail("You're translating too fast. Give it a few seconds.")
            return
        # A slash command works where its user can't post, and the translation is their text in the bot's name, so then only they see it.
        public = access.can_post(ctx.author, ctx.channel)
        await ctx.defer(ephemeral=not public)
        result = await self.translator.translate(text, target, tr.resolve_source(source), token=self._token(ctx.guild.id))
        if not result["ok"]:
            await ctx.send(tr.error_text(result), ephemeral=not public, allowed_mentions=NONE)
            return
        for message in self._messages(result, *self._arrow(result)):
            await ctx.send(**message, ephemeral=not public, allowed_mentions=NONE)

    @translate.autocomplete("to")
    async def _target_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return self._language_choices([(code, name) for code, name, _ in tr.LANGUAGES], current)

    @translate.autocomplete("source")
    async def _source_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return self._language_choices(tr.SOURCE_LANGUAGES, current, auto=True)

    @staticmethod
    def _language_choices(pairs: list[tuple[str, str]], current: str, *, auto: bool = False) -> list[app_commands.Choice[str]]:
        typed = current.lower().strip()
        choices = []
        if auto and (typed == "" or typed in "auto-detect"):
            choices.append(app_commands.Choice(name="Auto-detect", value="auto"))
        for code, name in pairs:
            label = f"{name} ({code})"
            if typed == "" or typed in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=code))
            if len(choices) >= 25:
                break
        return choices[:25]

    async def translate_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Right-click a message, then Apps, then Translate. Only the person who asked sees the translation."""
        if not self.on_in(interaction.guild_id):
            await interaction.response.send_message("That's switched off in this server. A manager can switch it on from the dashboard.", ephemeral=True)
            return
        text_ = tr.message_text(message)
        if not text_:
            await interaction.response.send_message("That message has no text to translate.", ephemeral=True)
            return
        if self.throttle.limited(interaction.user.id):
            await interaction.response.send_message(f"{GLOBE} You're translating too fast. Give it a few seconds.", ephemeral=True)
            return
        target = await self.default_target(interaction.guild_id, interaction.user.id, interaction.locale)
        await interaction.response.defer(ephemeral=True)
        result = await self.translator.translate(text_, target, token=self._token(interaction.guild_id))
        if not result["ok"]:
            await interaction.followup.send(tr.error_text(result), ephemeral=True, allowed_mentions=NONE)
            return
        for sent in self._messages(result, *self._arrow(result)):
            await interaction.followup.send(**sent, ephemeral=True, allowed_mentions=NONE)

    @commands.hybrid_command(name="translate_language", description="Set your preferred language for one-tap Translate (or 'auto' to use your Discord language)")
    @commands.guild_only()
    @app_commands.describe(language="A language (e.g., Spanish, Japanese), or 'auto' to clear and use your Discord language")
    async def translate_language(self, ctx: commands.Context, *, language: str) -> None:
        if language.strip().lower() in RESET_WORDS:
            await self.db.run("DELETE FROM translate_languages WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, ctx.author.id))
            await ctx.ok("Cleared. One-tap translations in this server will use your Discord language, or English.")
            return
        code = tr.resolve_target(language)
        if not code:
            await ctx.fail(f"Unknown language: `{_shown(language)}`. Try Spanish, Japanese or French, or a code like ES.")
            return
        await self.db.run(
            "INSERT INTO translate_languages (guild_id, user_id, language, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET language = excluded.language, updated_at = excluded.updated_at",
            (ctx.guild.id, ctx.author.id, code, time.time()),
        )
        await ctx.ok(f"Saved. One-tap translations in this server will go to **{tr.language_name(code)}**.")

    @translate_language.autocomplete("language")
    async def _saved_language_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        typed = current.lower().strip()
        choices = []
        if typed == "" or typed in "auto reset default":
            choices.append(app_commands.Choice(name="Auto (use my Discord language)", value="reset"))
        for code, name, _ in tr.LANGUAGES:
            if len(choices) >= 25:
                break
            label = f"{name} ({code})"
            if typed == "" or typed in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=code))
        return choices[:25]

    @commands.hybrid_command(name="translate_autodelete", description="How long flag translations stay before they're deleted, in seconds. 0 keeps them.")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(seconds=f"Seconds a flag translation stays up, from 0 to {LONGEST_DELETE}. 0 keeps it. The default is {DEFAULT_DELETE}.")
    async def translate_autodelete(self, ctx: commands.Context, seconds: int) -> None:
        seconds = max(0, min(seconds, LONGEST_DELETE))
        await self.bot.settings.save(ctx.guild.id, self.key, {"flag_delete_seconds": seconds}, actor=self.bot.actor(ctx.author))
        if seconds == 0:
            await ctx.ok("Flag-reaction translations will now stay in this server (auto-delete off).")
        else:
            await ctx.ok(f"Flag-reaction translations in this server will auto-delete after **{seconds}s**.")

    # ---- answers to messages ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or not self.bot.reads_message_text or not message.content or self._own(message):
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply) or not self.on_in(guild.id):
            return
        if self._left_out(guild.id, message.channel):
            return
        try:
            await self._answer(message)
        except Exception:
            log.exception("Answering a message in %s failed", guild.id)

    async def _answer(self, message: Any) -> None:
        cfg = self.cfg(message.guild.id)
        content = message.content
        if cfg["answer_translate"] and not message.author.bot:
            asked = tr.read_request(content, message.reference is not None)
            if asked is not None:
                await self._translate_message(message, asked)
                return
        if not (cfg["answer_currency"] or cfg["answer_units"]) or not convert.amounts(content) or await self._is_command(message):
            return
        if cfg["answer_currency"] and await self._answer_currency(message):
            return
        if cfg["answer_units"]:
            await self._answer_units(message)

    async def _is_command(self, message: Any) -> bool:
        """A command or a tag someone ran, which gets its own answer."""
        content = message.content
        for prefix in await self.bot.get_prefix(message):
            if content.startswith(prefix):
                words = content[len(prefix):].split(maxsplit=1)
                if not words:
                    return False
                if self.bot.typed_command(words[0], message.guild.id) is not None or words[0].lower() in SHORTCUTS or words[0].lower() in TAG_FIRST_SHORTCUTS:
                    return True
                tags = self.bot.module("tags")
                return tags is not None and await tags.find(message.guild.id, words[0]) is not None
        return False

    async def _reply(self, message: Any, **kwargs: Any) -> Any:
        try:
            return await message.reply(mention_author=False, allowed_mentions=NONE, **kwargs)
        except discord.HTTPException as error:
            log.info("Couldn't answer a message in %s: %s", message.channel.id, error)
            return None

    async def _replied_text(self, message: Any) -> str:
        """The text of the message this one replies to, from the same channel."""
        reference = message.reference
        if reference is None or reference.message_id is None or getattr(reference, "type", discord.MessageReferenceType.reply) != discord.MessageReferenceType.reply:
            return ""
        if reference.channel_id is not None and reference.channel_id != message.channel.id:
            return ""
        found = reference.resolved
        if not isinstance(found, discord.Message):
            try:
                found = await message.channel.fetch_message(reference.message_id)
            except discord.HTTPException:
                return ""
        return tr.message_text(found)

    async def _translate_message(self, message: Any, asked: tr.Request) -> None:
        if not self._can_answer(message.channel) or self.throttle.limited(message.author.id):
            return  # someone over the limit is passed over without a word, as the old answers were
        target = asked.target or await self.default_target(message.guild.id, message.author.id)
        source_text = (await self._replied_text(message) if message.reference is not None else "") if asked.wants_reply else asked.text
        if not source_text:
            await self._reply(message, content=f"{GLOBE} Give me something to translate: reply to a message, or type `translate hola to english`.")
            return
        result = await self.translator.translate(source_text, target, token=self._token(message.guild.id))
        if not result["ok"]:
            await self._reply(message, content=tr.error_text(result))
            return
        sending = self._messages(result, *self._arrow(result))
        if any("embeds" in part for part in sending) and not self._can_answer(message.channel, embeds=True):
            return
        for part in sending:
            await self._reply(message, **part)

    async def _answer_currency(self, message: Any) -> bool:
        """A conversion asked in words, then one asked with two codes. True when it answered. One currency on both sides is chat, as in
        "a 9 all in all", where ALL is the Albanian lek's code."""
        content = message.content
        asked = convert.currency_sentence(content)
        if asked is not None:
            amount = convert.parse_amount(asked.amount)
            if amount is not None:
                await self.ensure_rates()
                source, target = convert.currency_code(asked.source, self._rates), convert.currency_code(asked.target, self._rates)
                rate = self.rate(source, target) if source and target and source != target else None
                if rate is not None and math.isfinite(amount * rate) and self._can_answer(message.channel):
                    names = self._names
                    source_label = _escaped(convert.currency_label(asked.source, source, names))
                    target_label = _escaped(convert.currency_label(asked.target, target, names))
                    jokes = convert.currency_jokes(asked.source, asked.target)
                    await self._reply(message, content=f"{convert.figure(amount, 2)} {source_label} ≈ **{convert.figure(amount * rate, 2)}** {target_label}{jokes}{self._as_of()}\n"
                                                       f"(1 {source_label} ≈ {rate} {target_label})")
                    return True
        coded = convert.currency_codes(content)
        if coded is None:
            return False
        amount = convert.parse_amount(coded.amount)
        if amount is None:
            return False
        await self.ensure_rates()
        source, target = coded.source.lower(), coded.target.lower()
        rate = self.rate(source, target) if source != target else None
        if rate is None or not math.isfinite(amount * rate) or not self._can_answer(message.channel):
            return False
        names = self._names
        await self._reply(message, content=f"{convert.figure(amount, 2)} {_escaped(convert.currency_label(coded.source, source, names))} ≈ **{convert.figure(amount * rate, 2)}** "
                                           f"{_escaped(convert.currency_label(coded.target, target, names))}{self._as_of()}")
        return True

    async def _answer_units(self, message: Any) -> None:
        asked = convert.unit_sentence(message.content)
        if asked is None or convert.normalize_unit(asked.source) == convert.normalize_unit(asked.target):
            return  # one unit on both sides answers nothing worth saying
        amount = convert.parse_amount(asked.amount)
        result = convert.convert_units(amount, asked.source, asked.target) if amount is not None else None
        if result is None or not math.isfinite(result) or not self._can_answer(message.channel):
            return
        await self._reply(message, content=f"{convert.figure(amount, 2)} {_escaped(asked.source)} ≈ **{convert.figure(result, 10)}** {_escaped(asked.target)}")

    # ---- flag reactions ----

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or not self.on_in(payload.guild_id):
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return
        target = tr.flag_language(str(payload.emoji))
        cfg = self.cfg(payload.guild_id)
        if target is None or not cfg["flag_translations"] or not self.bot.reads_message_text:
            return
        try:
            await self._translate_flag(payload, target, cfg)
        except Exception:
            log.exception("A flag translation in %s failed", payload.guild_id)

    async def _translate_flag(self, payload: discord.RawReactionActionEvent, target: str, cfg: dict) -> None:
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel_or_thread(payload.channel_id)
        if channel is None:  # a thread the bot hasn't seen since it started
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return
            if getattr(getattr(channel, "guild", None), "id", None) != guild.id:
                return
        if self._left_out(payload.guild_id, channel) or not self._can_answer(channel):
            return
        ttl = cfg["flag_delete_seconds"] or None
        key = (payload.message_id, target)
        now = time.monotonic()
        # No await before the mark, so the same flag from two people at once makes one translation.
        if self.flags.live(key, ttl, now) or self.throttle.limited(payload.user_id):
            return
        self.flags.mark(key, now)
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            self.flags.unmark(key)
            return
        original = tr.message_text(message)
        if not original:
            self.flags.unmark(key)
            return
        result = await self.translator.translate(original, target, token=self._token(payload.guild_id))
        if not result["ok"]:
            self.flags.unmark(key)  # a later reaction can try again
            log.info("A flag translation into %s failed: %s %s", target, result.get("status"), result.get("message"))
            return
        if result["text"].strip() == original:
            return  # the flag's own language, such as an English flag on English text. The mark stays, so repeats stay quiet.
        flag = str(payload.emoji)
        source, into = tr.language_name(result["source_lang"]), tr.language_name(result["target_lang"])
        sending = self._messages(result, f"{flag} **{into}** (from {source})", f"{flag} {into} (from {source})")
        if any("embeds" in part for part in sending) and not self._can_answer(channel, embeds=True):
            return
        for part in sending:
            try:
                sent = await message.reply(mention_author=False, allowed_mentions=NONE, **part)
            except discord.Forbidden:
                return  # the mark stays, since another try would fail the same way
            except discord.HTTPException as error:
                self.flags.unmark(key)
                log.info("A flag translation reply failed in %s: %s", channel.id, error)
                return
            if ttl:
                await self.bot.timers.schedule(DELETE_KIND, time.time() + ttl, {"channel_id": sent.channel.id, "message_id": sent.id}, guild_id=payload.guild_id)

    async def _delete_due(self, payload: dict, row: dict) -> None:
        place = self.bot.get_partial_messageable(int(payload["channel_id"]), guild_id=row.get("guild_id"))
        try:
            await place.get_partial_message(int(payload["message_id"])).delete()
        except (discord.NotFound, discord.Forbidden):
            pass  # gone already, or the bot can't reach that channel now

    # ---- /export_emojis ----

    @commands.hybrid_command(name="export_emojis", description="Export all of this server's custom emojis as ZIP files")
    @commands.guild_only()
    @requires("administrator")
    async def export_emojis(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if not guild.emojis:
            await ctx.fail("This server has no custom emojis.")
            return
        if guild.id in self._exporting:
            await ctx.fail("An export is already running in this server. Try again when it finishes.")
            return
        self._exporting.add(guild.id)
        try:
            await ctx.defer(ephemeral=True)
            await self._export(ctx, guild)
        finally:
            self._exporting.discard(guild.id)

    async def _export(self, ctx: commands.Context, guild: Any) -> None:
        limit = getattr(ctx.interaction, "filesize_limit", None) or guild.filesize_limit
        budget = max(1024 * 1024, min(limit, EXPORT_PART_BYTES) - EXPORT_MARGIN)
        taken: dict[str, int] = {}
        part: list[tuple[str, bytes]] = []
        size = done = parts = skipped = 0

        async def send(entries: list[tuple[str, bytes]], number: int, last: bool) -> None:
            data = await asyncio.to_thread(zip_bytes, entries)
            name = "custom_emojis.zip" if number == 1 else f"custom_emojis-{number}.zip"
            if last and number == 1:
                note = f"Exported {done} custom emoji(s) to ZIP" + (f". {skipped} couldn't be downloaded." if skipped else ":")
            elif last:
                note = f"Exported {done} custom emoji(s) in {number} ZIP files. This is the last one." + (f" {skipped} couldn't be downloaded." if skipped else "")
            else:
                note = f"Part {number} of the export."
            await ctx.send(note, file=discord.File(io.BytesIO(data), filename=name), ephemeral=True, allowed_mentions=NONE)

        for emoji in list(guild.emojis):
            try:
                data = await asyncio.wait_for(self.bot.http.get_from_cdn(emoji.url), EMOJI_DOWNLOAD_SECONDS)
            except (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
                # Discord's own errors, and the connection's, which discord.py passes on as they are. One emoji is left out, and the rest go.
                log.info("Couldn't download emoji %s: %r", emoji.id, error)
                skipped += 1
                continue
            kind = emoji_file_type(data, emoji.animated)
            # Two emojis can share a name, and on Windows two names that differ only in case are one file. Discord itself tells them apart with ~1.
            folded = f"{emoji.name}.{kind}".lower()
            taken[folded] = seen = taken.get(folded, 0) + 1
            name = f"{emoji.name}.{kind}" if seen == 1 else f"{emoji.name}~{seen - 1}.{kind}"
            cost = len(data) + 2 * len(name) + 200
            if part and size + cost > budget:
                parts += 1
                await send(part, parts, last=False)
                part, size = [], 0
            part.append((name, data))
            size += cost
            done += 1
        if not part:
            await ctx.fail("No emojis could be downloaded.")
            return
        parts += 1
        await send(part, parts, last=True)
