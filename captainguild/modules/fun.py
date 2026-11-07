"""Text toys, animal pictures, echo, emoji adding and the TopRoll game."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import re
import time
from typing import Any, Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.durations import humanize
from ..core.module import Module, requires
from ..core.outside import Outside, OutsideError
from .games import draw_snow_globe

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
BLUE = 0x00A2F8
GREEN = 0x53DDAC
DISCORD_TEXT_MAX = 2000


# ---- fancy letters ----

def _alphabet(upper: int, lower: int, moved: dict[str, int] | None = None, digits: int | None = None) -> dict[int, str]:
    """A translation table. Unicode lays these letters out from A to Z and a to z, except a few it had already placed elsewhere."""
    table = {}
    for index in range(26):
        table[ord("A") + index] = chr(upper + index)
        table[ord("a") + index] = chr(lower + index)
    if digits is not None:
        for index in range(10):
            table[ord("0") + index] = chr(digits + index)
    for letter, point in (moved or {}).items():
        table[ord(letter)] = chr(point)
    return table


FANCY = _alphabet(0x1D49C, 0x1D4B6, {"B": 0x212C, "E": 0x2130, "F": 0x2131, "H": 0x210B, "I": 0x2110, "L": 0x2112, "M": 0x2133, "R": 0x211B, "e": 0x212F, "g": 0x210A, "o": 0x2134})
BOLD_FANCY = _alphabet(0x1D4D0, 0x1D4EA)
FRAKTUR = _alphabet(0x1D504, 0x1D51E, {"C": 0x212D, "H": 0x210C, "I": 0x2111, "R": 0x211C, "Z": 0x2128})
BOLD_FRAKTUR = _alphabet(0x1D56C, 0x1D586)
DOUBLE = _alphabet(0x1D538, 0x1D552, {"C": 0x2102, "H": 0x210D, "N": 0x2115, "P": 0x2119, "Q": 0x211A, "R": 0x211D, "Z": 0x2124}, digits=0x1D7D8)
FULL_WIDTH = {**{point: chr(point + 0xFEE0) for point in range(0x21, 0x7F)}, 0x20: "\u3000"}
SMALL_CAPS = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz",
    "\u1d00\u0299\u1d04\u1d05\u1d07\ua730\u0262\u029c\u026a\u1d0a\u1d0b\u029f\u1d0d\u0274\u1d0f\u1d18\u01eb\u0280\ua731\u1d1b\u1d1c\u1d20\u1d21x\u028f\u1d22",
)
CLAP = "\N{CLAPPING HANDS SIGN}"
KEYCAP = "\ufe0f\u20e3"
EIGHT_BALL = [
    "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes, definitely.", "You may rely on it.", "As I see it, yes.", "Most likely.",
    "Outlook good.", "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.", "Better not tell you now.", "Cannot predict now.",
    "Concentrate and ask again.", "Don't count on it.", "My reply is no.", "My sources say no.", "Outlook not so good.", "Very doubtful.",
]


def wrap_words(sentence: str, mark: str) -> str:
    words = sentence.split()
    return mark + mark.join(words) + mark if words else ""


def emojify(sentence: str) -> str:
    out = []
    for character in sentence.lower():
        if "a" <= character <= "z":
            # A space between letters, or two regional indicators in a row would join into a flag.
            out.append(chr(0x1F1E6 + ord(character) - ord("a")) + " ")
        elif character.isdigit() and character.isascii():
            out.append(character + KEYCAP + " ")
        elif character.isspace():
            out.append("  ")
        else:
            out.append(character)
    return "".join(out).strip()


def owofy(sentence: str) -> str:
    text = re.sub(r"[rl]", "w", sentence)
    text = re.sub(r"[RL]", "W", text)
    text = re.sub(r"n(?=[aeiou])", "ny", text)
    text = re.sub(r"N(?=[aeiou])", "Ny", text)
    text = re.sub(r"N(?=[AEIOU])", "NY", text)
    return text.replace("ove", "uv")


def pick_from(sentence: str) -> list[str]:
    parts = sentence.split(",") if "," in sentence else sentence.split()
    return [part.strip() for part in parts if part.strip()]


def parse_roll_range(text: str | None) -> tuple[int, int] | None:
    """Reads "[[lower-]upper]". Empty means 1 to 100."""
    if not text or not text.strip():
        return 1, 100
    match = re.fullmatch(r"(?:(\d{1,9})\s*-\s*)?(\d{1,9})", text.strip())
    if match is None:
        return None
    lower, upper = int(match.group(1) or 1), int(match.group(2))
    return (lower, upper) if lower <= upper else None


# ---- outside services ----

JSON_TIMEOUT = 8.0
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_EMOJI_BYTES = 256 * 1024
BOMB_SIZE = 5


# Free services that need no key. Each kind has a second source where one exists, so one site being down doesn't end the command.
IMAGE_SOURCES: dict[str, list[tuple[str, Any]]] = {
    "cat": [("https://api.thecatapi.com/v1/images/search", lambda data: data[0]["url"]), ("https://cataas.com/cat?json=true", lambda data: data["url"])],
    "dog": [("https://dog.ceo/api/breeds/image/random", lambda data: data["message"]), ("https://random.dog/woof.json", lambda data: data["url"])],
    "fox": [("https://randomfox.ca/floof/", lambda data: data["image"])],
    "duck": [("https://random-d.uk/api/v2/random", lambda data: data["url"])],
}
_NOT_A_PICTURE = re.compile(r"\.(?:mp4|webm|mov)(?:\?|$)", re.IGNORECASE)


def usable_image(url: Any) -> str | None:
    """An https image link, or None. A service can answer with a video or plain http, and an embed can't show those."""
    if not isinstance(url, str) or _NOT_A_PICTURE.search(url):
        return None
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url if url.startswith("https://") else None


def image_kind(data: bytes) -> str | None:
    """Trust the file's first bytes, since a server can claim any content type."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) > 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return None


class EchoView(discord.ui.View):
    """Shows staff who had the bot say something. The sender is found by the message, so this works after a restart."""

    def __init__(self, cog: "Fun") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Who sent this?", style=discord.ButtonStyle.secondary, custom_id="captainguild:echo:who")
    async def who(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.echo_pressed(interaction)


class TopRollView(discord.ui.View):
    def __init__(self, cog: "Fun") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Roll", emoji="\N{GAME DIE}", style=discord.ButtonStyle.primary, custom_id="captainguild:toproll:roll")
    async def roll(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.toproll_pressed(interaction)


TOPROLL_TRIES = 6
TOPROLL_SECONDS = 60
TOPROLL_ROUND = 86400
TOPROLL_MAX = 100


class Fun(Module):
    key = "fun"
    category = "Fun"
    migrations = [
        """
        CREATE TABLE echo_messages (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX echo_messages_guild ON echo_messages (guild_id, created_at);
        CREATE TABLE toproll_rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            started_at REAL NOT NULL,
            ends_at REAL NOT NULL,
            ended_at REAL
        );
        CREATE INDEX toproll_rounds_guild ON toproll_rounds (guild_id, id);
        CREATE TABLE toproll_scores (
            round_id INTEGER NOT NULL REFERENCES toproll_rounds (id) ON DELETE CASCADE,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message_id INTEGER,
            started_at REAL NOT NULL,
            rolls TEXT NOT NULL DEFAULT '[]',
            total INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (round_id, user_id)
        );
        CREATE INDEX toproll_scores_guild ON toproll_scores (guild_id);
        CREATE INDEX toproll_scores_message ON toproll_scores (message_id);
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.outside = Outside()

    async def on_load(self) -> None:
        self.bot.add_view(EchoView(self))
        self.bot.add_view(TopRollView(self))
        self.bot.timers.handler("toproll", self._toproll_due)
        await self._arm_missing()

    async def on_guild_restored(self, guild_id: int) -> None:
        await self._arm_missing()

    async def _arm_missing(self) -> None:
        """A restored backup can bring an open round back without its timer, and it would never close."""
        for row in await self.db.all("SELECT r.id, r.guild_id, r.ends_at FROM toproll_rounds r LEFT JOIN timers t ON t.kind = 'toproll' AND t.key = 'toproll:' || r.id WHERE t.id IS NULL AND r.ended_at IS NULL"):
            await self.bot.timers.schedule("toproll", row["ends_at"], {"id": row["id"]}, guild_id=row["guild_id"], key=f"toproll:{row['id']}")

    async def cog_unload(self) -> None:
        await self.outside.close()

    async def get_json(self, url: str, *, params: dict | None = None) -> Any:
        """One JSON answer from a fixed public API. Raises OutsideError on anything else. A site being down never belongs in the error log."""
        reply = await self.outside.get(url, params=params, max_bytes=MAX_JSON_BYTES, timeout=JSON_TIMEOUT)
        if reply.status != 200:
            raise OutsideError(f"{url} answered {reply.status}.")
        return reply.json()

    # ---- /fun ----

    @commands.hybrid_group(name="fun", description="Text toys, coin flips and the magic 8 ball")
    @commands.guild_only()
    async def fun(self, ctx: commands.Context) -> None:
        await self._list_subcommands(ctx, self.fun)

    @staticmethod
    async def _list_subcommands(ctx: commands.Context, group: commands.Group) -> None:
        lines = [f"`/{c.qualified_name}`: {c.description or c.short_doc}" for c in sorted(group.commands, key=lambda c: c.name)]
        await ctx.send("\n".join(lines)[:DISCORD_TEXT_MAX], ephemeral=True, allowed_mentions=NO_PINGS)

    @staticmethod
    async def _say(ctx: commands.Context, text: str) -> None:
        if not text.strip():
            await ctx.fail("That didn't leave anything to post.")
            return
        refusal = access.post_refusal(ctx.author, ctx.channel)  # a slash command works where its user can't post, and this is their text in the bot's name
        if refusal:
            await ctx.fail(refusal)
            return
        await ctx.send(text[:DISCORD_TEXT_MAX], allowed_mentions=NO_PINGS)

    @fun.command(name="aesthetics", description="Write your text in full-width letters")
    @requires("manage_guild")
    @app_commands.describe(sentence="Your text")
    async def fun_aesthetics(self, ctx: commands.Context, *, sentence: str) -> None:
        await self._say(ctx, sentence.translate(FULL_WIDTH))

    @fun.command(name="boldfancy", description="Write your text in bold cursive")
    @requires("manage_guild")
    @app_commands.describe(sentence="Your text")
    async def fun_boldfancy(self, ctx: commands.Context, *, sentence: str) -> None:
        await self._say(ctx, sentence.translate(BOLD_FANCY))

    @fun.command(name="boldfraktur", description="Write your text in bold blackletter")
    @requires("manage_guild")
    @app_commands.describe(sentence="Your text")
    async def fun_boldfraktur(self, ctx: commands.Context, *, sentence: str) -> None:
        await self._say(ctx, sentence.translate(BOLD_FRAKTUR))

    @fun.command(name="clap", description="Put a clap between every word")
    @requires("manage_guild")
    @app_commands.describe(sentence="Your text")
    async def fun_clap(self, ctx: commands.Context, *, sentence: str) -> None:
        await self._say(ctx, wrap_words(sentence, CLAP))

    @fun.command(name="double", description="Write your text in double-struck letters")
    @requires("manage_guild")
    @app_commands.describe(sentence="Your text")
    async def fun_double(self, ctx: commands.Context, *, sentence: str) -> None:
        await self._say(ctx, sentence.translate(DOUBLE))

    @fun.command(name="emojify", description="Write your text in letter emoji")
    @requires("manage_guild")
    @app_commands.describe(sentence="Your text")
    async def fun_emojify(self, ctx: commands.Context, *, sentence: str) -> None:
        await self._say(ctx, emojify(sentence))

    @fun.command(name="fancy", description="Write your text in cursive")
    @requires("manage_guild")
    @app_commands.describe(sentence="Your text")
    async def fun_fancy(self, ctx: commands.Context, *, sentence: str) -> None:
        await self._say(ctx, sentence.translate(FANCY))

    @fun.command(name="fraktur", description="Write your text in blackletter")
    @requires("manage_guild")
    @app_commands.describe(sentence="Your text")
    async def fun_fraktur(self, ctx: commands.Context, *, sentence: str) -> None:
        await self._say(ctx, sentence.translate(FRAKTUR))

    @fun.command(name="owofy", description="Rewrite your text in the owo way of talking")
    @requires("manage_guild")
    @app_commands.describe(sentence="Your text")
    async def fun_owofy(self, ctx: commands.Context, *, sentence: str) -> None:
        await self._say(ctx, owofy(sentence))

    @fun.command(name="smallcaps", description="Write your text in small capitals")
    @requires("manage_guild")
    @app_commands.describe(sentence="Your text")
    async def fun_smallcaps(self, ctx: commands.Context, *, sentence: str) -> None:
        await self._say(ctx, sentence.lower().translate(SMALL_CAPS))

    @fun.command(name="space", description="Put a word or emoji of your choice between every word")
    @requires("manage_guild")
    @app_commands.describe(word="What goes between the words", sentence="Your text")
    async def fun_space(self, ctx: commands.Context, word: str, *, sentence: str) -> None:
        await self._say(ctx, wrap_words(sentence, word))

    @fun.command(name="pick", aliases=["choose"], description="Pick one of your choices. Use commas for choices of several words.")
    @requires("manage_guild")
    @app_commands.describe(sentence="The choices")
    async def fun_pick(self, ctx: commands.Context, *, sentence: str) -> None:
        choices = pick_from(sentence)
        if not choices:
            await ctx.fail("Give me something to pick from.")
            return
        await self._say(ctx, random.choice(choices))

    @fun.command(name="coinflip", description="Flip a coin")
    async def fun_coinflip(self, ctx: commands.Context) -> None:
        await ctx.send(random.choice(("Heads", "Tails")) + ".")

    @fun.command(name="eightball", aliases=["8ball"], description="Ask the magic 8 ball a question")
    @app_commands.describe(question="What you want to know")
    async def fun_eightball(self, ctx: commands.Context, *, question: str) -> None:
        answer = random.choice(EIGHT_BALL)
        if access.can_post(ctx.author, ctx.channel):
            await ctx.send(f"> {question[:1800]}\n{answer}", allowed_mentions=NO_PINGS)
        else:  # someone who can't post here gets the answer to themselves, with none of their words in the channel
            await ctx.send(answer, ephemeral=True)

    @fun.command(name="roll", description="Roll a number, from 1 to 100 unless you give a range")
    @app_commands.describe(limits="The top number, or a range such as 5-50")
    async def fun_roll(self, ctx: commands.Context, limits: Optional[str] = None) -> None:
        bounds = parse_roll_range(limits)
        if bounds is None:
            await ctx.fail("Give a top number or a range such as 5-50.")
            return
        await ctx.send(f"{ctx.author.display_name} rolls {random.randint(*bounds):,} ({bounds[0]:,} to {bounds[1]:,}).", allowed_mentions=NO_PINGS)

    @fun.command(name="urbandictionary", description="Look up a word on Urban Dictionary. NSFW channels only.")
    @requires("manage_guild")
    @app_commands.describe(word="The word to look up")
    async def fun_urbandictionary(self, ctx: commands.Context, *, word: str) -> None:
        if not ctx.channel.is_nsfw():
            await ctx.fail("This command only works in NSFW channels.")
            return
        await ctx.defer()
        try:
            data = await self.get_json("https://api.urbandictionary.com/v0/define", params={"term": word})
        except OutsideError as error:
            log.info("Urban Dictionary lookup failed: %s", error)
            await ctx.fail("Urban Dictionary didn't answer. Try again in a bit.")
            return
        entries = data.get("list") if isinstance(data, dict) else None
        if not entries:
            await ctx.fail("No definition found for that.")
            return
        entry = max(entries, key=lambda row: row.get("thumbs_up") or 0)
        link = entry.get("permalink")
        embed = discord.Embed(title=str(entry.get("word") or word)[:256], url=link if isinstance(link, str) and link.startswith("https://") else None,
                              description=self._plain_definition(entry.get("definition"))[:4000], color=BLUE)
        example = self._plain_definition(entry.get("example"))
        if example:
            embed.add_field(name="Example", value=example[:1024], inline=False)
        embed.set_footer(text=f"Thumbs up {entry.get('thumbs_up', 0):,}, thumbs down {entry.get('thumbs_down', 0):,}")
        await ctx.send(embed=embed, allowed_mentions=NO_PINGS)

    @staticmethod
    def _plain_definition(text: Any) -> str:
        """The site marks linked words with square brackets, which read as noise anywhere else."""
        return re.sub(r"\[([^\]]*)\]", r"\1", text).strip() if isinstance(text, str) else ""

    @fun.command(name="shake", description="Shake the snow globe")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def fun_shake(self, ctx: commands.Context) -> None:
        png = await asyncio.to_thread(draw_snow_globe, random.getrandbits(32))
        embed = discord.Embed(title="Snow day", color=BLUE).set_image(url="attachment://snow-globe.png")
        await ctx.send(embed=embed, file=discord.File(io.BytesIO(png), filename="snow-globe.png"), allowed_mentions=NO_PINGS)

    @fun.command(name="addemoji", description="Add an emoji to this server from an image, a link or another emoji")
    @requires("manage_expressions")
    @app_commands.describe(name="The new emoji's name", entity="An image link, an emoji or an emoji ID", attachment="An image file")
    async def fun_addemoji(self, ctx: commands.Context, name: str, entity: Optional[str] = None, attachment: Optional[discord.Attachment] = None) -> None:
        if not re.fullmatch(r"\w{2,32}", name, re.ASCII):
            await ctx.fail("An emoji's name takes 2 to 32 letters and numbers. It can also hold the _ character.")
            return
        if not ctx.guild.me.guild_permissions.manage_expressions:
            await ctx.fail("I need Manage Expressions to add an emoji.")
            return
        if attachment is not None and attachment.size > MAX_EMOJI_BYTES:
            await ctx.fail(f"Discord limits an emoji file to {MAX_EMOJI_BYTES // 1024} KB, and that file is larger.")
            return
        url = attachment.url if attachment is not None else self._emoji_source(entity)
        if url is None:
            await ctx.fail("Give me an image file, an image link, an emoji or an emoji ID.")
            return
        await ctx.defer()
        try:
            reply = await self.outside.get(url, stranger=True, max_bytes=MAX_EMOJI_BYTES, timeout=15.0)
        except OutsideError as error:
            await ctx.fail(f"I couldn't fetch that. {error}")
            return
        if reply.status != 200:
            await ctx.fail(f"That link answered with status {reply.status}.")
            return
        data = reply.body
        if image_kind(data) is None:
            await ctx.fail("That isn't a PNG, JPEG, GIF or WebP image.")
            return
        try:
            emoji = await ctx.guild.create_custom_emoji(name=name, image=data, reason=f"Added by {ctx.author}")
        except discord.Forbidden:
            await ctx.fail("I need Manage Expressions to add an emoji.")
            return
        except discord.HTTPException as error:
            await ctx.fail(f"Discord refused the emoji: {error.text or error.status}")
            return
        await ctx.ok(f"Added {emoji}.")

    @staticmethod
    def _emoji_source(entity: str | None) -> str | None:
        entity = (entity or "").strip()
        match = re.fullmatch(r"<(a?):\w+:(\d+)>", entity)
        if match:
            return f"https://cdn.discordapp.com/emojis/{match.group(2)}.{'gif' if match.group(1) else 'png'}"
        if re.fullmatch(r"\d{15,21}", entity):
            return f"https://cdn.discordapp.com/emojis/{entity}.png"
        return entity if entity.startswith(("http://", "https://")) else None

    # ---- /cute ----

    async def _one_image(self, kind: str) -> str:
        sources = list(IMAGE_SOURCES[kind])
        random.shuffle(sources)
        last: Exception = OutsideError("No source gave a picture.")
        for url, read in sources:
            try:
                image = usable_image(read(await self.get_json(url)))
            except OutsideError as error:
                last = error
                continue
            except (KeyError, IndexError, TypeError) as error:  # the site changed its answer
                last = OutsideError(f"{url}: {error!r}")
                continue
            if image:
                return image
        raise last

    @commands.hybrid_command(name="cute", description="Show a random animal picture")
    @commands.guild_only()
    @commands.cooldown(2, 10, commands.BucketType.user)
    @app_commands.describe(choice="Which animal", bomb="Show five pictures instead of one")
    async def cute(self, ctx: commands.Context, choice: Literal["cat", "dog", "fox", "duck", "aww"], bomb: bool = False) -> None:
        await ctx.defer()
        count = BOMB_SIZE if bomb else 1
        kinds = random.choices(list(IMAGE_SOURCES), k=count) if choice == "aww" else [choice] * count
        answers = await asyncio.gather(*(self._one_image(kind) for kind in kinds), return_exceptions=True)
        images = list(dict.fromkeys(answer for answer in answers if isinstance(answer, str)))
        if not images:
            log.info("No animal pictures for %s: %s", choice, answers[0])
            await ctx.fail("The picture services didn't answer. Try again in a bit.")
            return
        await ctx.send(embeds=[discord.Embed(color=BLUE).set_image(url=image) for image in images])

    # ---- /echo ----

    def _modlog_channel(self, guild_id: int) -> int | None:
        """Where moderation actions get posted. Empty when the moderation module isn't loaded, no channel is set, or the bot can't post in the one that is."""
        if "moderation" not in self.bot.settings.schemas:
            return None
        cfg = self.bot.settings.get(guild_id, "moderation")
        channel_id = cfg.get("modlog_channel") or cfg.get("modlog")
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel_or_thread(channel_id) if guild and channel_id else None
        return channel_id if channel is not None and channel.permissions_for(guild.me).send_messages else None

    @commands.hybrid_command(name="echo", description="Have the bot post your message, with a button that shows who asked")
    @commands.guild_only()
    @requires("manage_guild")
    @app_commands.describe(message="What to post", channel="Where to post it. Empty means this channel.", no_button="Leave off the button. Needs a modlog channel, which records who asked.")
    async def echo(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None, *, message: str, no_button: bool = False) -> None:
        if ctx.interaction is None:
            first, _, rest = message.partition(" ")
            if channel is None and re.fullmatch(r"#\S+|<#\d+>", first):  # a channel that matched nothing would otherwise go out as the message
                await ctx.fail(f"I can't find a channel called `{first.replace('`', '')[:60]}`, so nothing was posted.")
                return
            if first.lower() in ("true", "false") and rest.strip():  # typed, this word comes first: echo [channel] [no_button] <message>
                no_button, message = first.lower() == "true", rest.strip()
        target = channel or ctx.channel
        if len(message) > DISCORD_TEXT_MAX:
            await ctx.fail(f"A message can hold up to {DISCORD_TEXT_MAX} characters.")
            return
        refusal = access.post_refusal(ctx.author, target)
        if refusal:
            await ctx.fail(refusal)
            return
        modlog = self._modlog_channel(ctx.guild.id)
        show_button = not (no_button and modlog)
        kwargs: dict[str, Any] = {"allowed_mentions": NO_PINGS}
        if show_button:
            kwargs["view"] = EchoView(self)
        try:
            posted = await target.send(message, **kwargs)
        except discord.Forbidden:
            await ctx.fail(f"I can't post in {target.mention}.")
            return
        if show_button:
            await self.db.run(
                "INSERT INTO echo_messages (message_id, guild_id, channel_id, author_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (posted.id, ctx.guild.id, target.id, ctx.author.id, time.time()),
            )
        else:
            entry = discord.Embed(title=f"Echo in #{target.name}", description=message[:4000], url=posted.jump_url, color=BLUE)
            entry.set_footer(text=f"Sent by {ctx.author} ({ctx.author.id})")
            self.bot.logs.post(modlog, entry)
        note = " with no button. The modlog records who asked." if not show_button else ("" if not no_button else ". The button stays until this server sets a modlog channel.")
        await ctx.ok(f"Posted in {target.mention}{note}")

    async def echo_pressed(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not getattr(member, "guild_permissions", discord.Permissions.none()).manage_guild:
            await interaction.response.send_message("Only people with Manage Server can see who sent this.", ephemeral=True)
            return
        row = await self.db.one("SELECT * FROM echo_messages WHERE message_id = ? AND guild_id = ?", (interaction.message.id, interaction.guild_id))
        if row is None:
            await interaction.response.send_message("There's no record of who sent this.", ephemeral=True)
            return
        await interaction.response.send_message(f"Sent by <@{row['author_id']}> (`{row['author_id']}`) <t:{int(row['created_at'])}:R>.", ephemeral=True, allowed_mentions=NO_PINGS)

    # ---- /games ----

    @commands.hybrid_group(name="games", description="Play TopRoll, look up players and check World of Warcraft timers")
    @commands.guild_only()
    async def games(self, ctx: commands.Context) -> None:
        await self._list_subcommands(ctx, self.games)

    async def _open_round(self, guild_id: int) -> dict | None:
        row = await self.db.one("SELECT * FROM toproll_rounds WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (guild_id,))
        if row is None or row["ended_at"] is not None or row["ends_at"] <= time.time():
            return None
        return row

    @staticmethod
    def _toproll_embed(name: str, rolls: list[int], *, started_at: float, done: bool) -> discord.Embed:
        lines = [f"Tries: {len(rolls)} of {TOPROLL_TRIES}", f"Total: **{sum(rolls):,}**"]
        if rolls:
            lines.append("Rolls: " + ", ".join(str(r) for r in rolls))
        if done:
            lines.append("Finished. See `/games leaderboard`.")
        else:
            lines.append(f"Time runs out <t:{int(started_at + TOPROLL_SECONDS)}:R>.")
        return discord.Embed(title=f"{name} is playing TopRoll", description="\n".join(lines), color=GREEN if done else BLUE)

    @games.command(name="toproll", description="Roll the highest sum you can in six tries and one minute")
    async def games_toproll(self, ctx: commands.Context) -> None:
        now = time.time()
        current = await self._open_round(ctx.guild.id)
        if current is None:
            round_id = await self.db.insert(
                "INSERT INTO toproll_rounds (guild_id, channel_id, started_at, ends_at) VALUES (?, ?, ?, ?)",
                (ctx.guild.id, ctx.channel.id, now, now + TOPROLL_ROUND),
            )
            await self.bot.timers.schedule("toproll", now + TOPROLL_ROUND, {"id": round_id}, guild_id=ctx.guild.id, key=f"toproll:{round_id}")
            ends_at = now + TOPROLL_ROUND
        else:
            round_id, ends_at = current["id"], current["ends_at"]
        played = await self.db.one("SELECT * FROM toproll_scores WHERE round_id = ? AND user_id = ?", (round_id, ctx.author.id))
        if played is not None:
            await ctx.fail(f"You've played this round and scored {played['total']:,}. The next round can start <t:{int(ends_at)}:R>.")
            return
        await self.db.run(
            "INSERT INTO toproll_scores (round_id, guild_id, user_id, started_at) VALUES (?, ?, ?, ?)",
            (round_id, ctx.guild.id, ctx.author.id, now),
        )
        embed = self._toproll_embed(ctx.author.display_name, [], started_at=now, done=False)
        try:
            sent = await ctx.send(embed=embed, view=TopRollView(self), allowed_mentions=NO_PINGS)
        except discord.HTTPException:  # nothing went out to press, so this try doesn't count as their turn
            await self.db.run("DELETE FROM toproll_scores WHERE round_id = ? AND user_id = ?", (round_id, ctx.author.id))
            raise
        if sent is None:  # this server muted the command's output, so there's nothing to press
            await self.db.run("DELETE FROM toproll_scores WHERE round_id = ? AND user_id = ?", (round_id, ctx.author.id))
            return
        await self.db.run("UPDATE toproll_scores SET message_id = ? WHERE round_id = ? AND user_id = ?", (sent.id, round_id, ctx.author.id))

    async def toproll_pressed(self, interaction: discord.Interaction) -> None:
        score = await self.db.one("SELECT * FROM toproll_scores WHERE message_id = ? AND guild_id = ?", (interaction.message.id, interaction.guild_id))
        if score is None:
            await interaction.response.send_message("This game is over.", ephemeral=True)
            return
        if score["user_id"] != interaction.user.id:
            await interaction.response.send_message("This is someone else's game. Start yours with `/games toproll`.", ephemeral=True)
            return
        rolls = json.loads(score["rolls"])
        in_time = time.time() - score["started_at"] <= TOPROLL_SECONDS
        if in_time and len(rolls) < TOPROLL_TRIES:
            rolls.append(random.randint(1, TOPROLL_MAX))
            await self.db.run(
                "UPDATE toproll_scores SET rolls = ?, total = ? WHERE round_id = ? AND user_id = ?",
                (json.dumps(rolls), sum(rolls), score["round_id"], score["user_id"]),
            )
        done = not in_time or len(rolls) >= TOPROLL_TRIES
        embed = self._toproll_embed(interaction.user.display_name, rolls, started_at=score["started_at"], done=done)
        if done:
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.edit_message(embed=embed)

    async def _leaderboard_embed(self, row: dict) -> discord.Embed:
        scores = await self.db.all("SELECT user_id, total FROM toproll_scores WHERE round_id = ? ORDER BY total DESC, started_at LIMIT 10", (row["id"],))
        lines = [f"{index}. <@{score['user_id']}>: {score['total']:,}" for index, score in enumerate(scores, 1)]
        embed = discord.Embed(title="TopRoll leaderboard", description="\n".join(lines) or "Nobody has rolled yet.", color=BLUE)
        if row["ended_at"] is None and row["ends_at"] > time.time():
            left = humanize(row["ends_at"] - time.time(), parts=2)
            embed.set_footer(text=f"This round ends in {left}.")
        else:
            embed.set_footer(text="This round has ended. Start a new one with /games toproll.")
        return embed

    @games.command(name="leaderboard", description="Show the current TopRoll leaderboard for this server")
    async def games_leaderboard(self, ctx: commands.Context) -> None:
        row = await self.db.one("SELECT * FROM toproll_rounds WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (ctx.guild.id,))
        if row is None:
            await ctx.send("Nobody has played yet. Start with `/games toproll`.", ephemeral=True)
            return
        await ctx.send(embed=await self._leaderboard_embed(row), allowed_mentions=NO_PINGS)

    async def _toproll_due(self, payload: dict, row: dict) -> None:
        changed = await self.db.run("UPDATE toproll_rounds SET ended_at = ? WHERE id = ? AND ended_at IS NULL", (time.time(), payload["id"]))
        if not changed:
            return
        round_row = await self.db.one("SELECT * FROM toproll_rounds WHERE id = ?", (payload["id"],))
        guild = self.bot.get_guild(round_row["guild_id"])
        channel = guild.get_channel_or_thread(round_row["channel_id"]) if guild else None
        if channel is None:
            return
        try:
            await channel.send("This round of TopRoll is over.", embed=await self._leaderboard_embed(round_row), allowed_mentions=NO_PINGS)
        except (discord.Forbidden, discord.NotFound):
            pass
