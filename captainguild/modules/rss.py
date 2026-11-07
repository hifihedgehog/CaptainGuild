"""RSS feeds: any RSS or Atom address, read every minute, with each new entry posted in its channel under the feed's own name.

A post is the entry's title as a heading, its text turned from HTML into Discord markdown, and a Read more link, sent through the bot's own
webhook in that channel so it can carry the feed's name. A feed added by hand posts its newest entry on its first read, as a check that it
works. After that, each read posts what's newer than the last entry it saw, oldest first, up to five at a time.

Each address is read once a round for every feed that follows it, through core/outside.py, so the usual address checks and limits apply. A
feed whose address fails, whose channel is gone or whose post Discord refuses notes why on the page and stays out of the way of the others.
A post Discord refuses is tried again on the next round, and after three tries the entry is skipped so the feed can move on. New entries
wait, however long it takes, while the bot can't have its webhook in the channel, for a missing permission or a channel that already has
its 15. Each entry is remembered the moment it goes out, so a dropped connection or a restart partway through a round never posts one twice.

Captain Guild's /feeds is something else: scheduled posts. These commands keep the names /add_feed, /remove_feed, /list_feeds, /test_feed
and /rss_log_channel.
"""
from __future__ import annotations

import asyncio
import calendar
import html
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.module import Module, requires
from ..core.ops import OpsError
from ..core.outside import Outside, OutsideError, check_public_link
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, optional, snowflake
from ..web.api import ApiError

log = logging.getLogger(__name__)

POLL = "rss.poll"
POLL_SECONDS = 60
MAX_FEEDS = 100
MAX_URL = 2048
NAME_LENGTH = 80
FEED_BYTES = 8 * 1024 * 1024
FEED_SECONDS = 20.0
FETCHES_AT_ONCE = 8
POSTS_PER_ROUND = 5
SEEN_KEPT = 500
TRIES = 3
WEBHOOKS_FULL = 30007  # Discord's code for a channel that already has its 15 webhooks
STOP_SECONDS = 5.0  # how long a stop waits for a post already on its way
MESSAGE_LIMIT = 2000
TITLE_LIMIT = 256
CUT = "..."
LIST_HEADER = "**Currently Configured RSS Feeds**:"
CHANNEL_GONE = "The channel for this feed no longer exists."
# Text and announcement channels, the kinds the commands take.
POSTABLE = (discord.ChannelType.text, discord.ChannelType.news)


class FeedError(Exception):
    """The address couldn't be read as a feed. The text is for the person reading the page or running the command."""


@dataclass
class Entry:
    key: str
    title: str
    link: str
    html: str
    at: float | None = None


# ---- reading a feed ----

def read_entries(body: bytes, address: str = "") -> list[Entry] | None:
    """A feed's entries, newest first, or None when the body isn't a feed. Passed as a stream, because feedparser tries bytes as a file name first.
    The address the body came from goes along, so an ID or link that isn't a full address reads as it does when feedparser fetches the feed
    itself, as the old bot had it. Entries are put in date order when every one has a date, so a feed listed oldest first still reads newest first."""
    # The content type reads the bytes as they read with no headers at all: the encoding the feed declares, or UTF-8.
    parsed = feedparser.parse(io.BytesIO(body), response_headers={"content-type": "application/xml", "content-location": address} if address else None)
    items = parsed.get("entries") or []
    if not items and (parsed.get("bozo") or not (parsed.get("feed") or {}).get("title")):
        return None
    entries = []
    for item in items:
        key = item.get("id") or item.get("link") or item.get("title")
        if not key:
            continue
        stamp = item.get("published_parsed") or item.get("updated_parsed")
        content = item.get("content") or []
        body_html = item.get("description") or item.get("summary") or (content[0].get("value") if content else "") or ""
        entries.append(Entry(str(key), item.get("title") or "", item.get("link") or "", body_html, float(calendar.timegm(stamp)) if stamp else None))
    if entries and all(entry.at is not None for entry in entries):
        entries.sort(key=lambda entry: entry.at, reverse=True)
    return entries


def fingerprint(entry: Entry) -> str:
    """An entry by its link and title, which a feed that renames its entries on every read keeps. Kept in the seen list beside the IDs."""
    return f"{entry.link}\x1f{entry.title}"


def fresh_entries(entries: list[Entry], seen: list[str] | None) -> list[Entry]:
    """What's new since the last read, oldest first. A feed never read before gives its newest entry. Every entry not seen before counts,
    down to the lowest one already seen, since below that the feed holds what came before, as it does after an import that names only the
    last entry posted. An edited entry dated again moves up the list and hides nothing under it. When nothing in the feed was seen before,
    its entries have all moved on, and only the newest goes out, so a feed that changes how it names its entries can't post its whole history.
    That newest one stays put when its link and title went out before, so a feed that renames its entries on every read posts each one once."""
    if not entries:
        return []
    if seen is None:
        return entries[:1]
    known = set(seen)
    lowest = max((index for index, entry in enumerate(entries) if entry.key in known), default=None)
    if lowest is None:
        return [] if fingerprint(entries[0]) in known else entries[:1]
    return [entry for entry in reversed(entries[:lowest]) if entry.key not in known]


def next_seen(entries: list[Entry], seen: list[str] | None, waiting: set[str]) -> list[str]:
    """Everything the feed holds now except the entries still waiting to go out, newest first, with each one's link and title, ahead of what
    was seen before. However long the feed, all of it stays seen, so an old entry dated again can't come back as new."""
    recorded = [entry for entry in entries if entry.key not in waiting]
    current = [entry.key for entry in recorded] + [fingerprint(entry) for entry in recorded]
    known = set(current)
    return (current + [key for key in seen or [] if key not in known and key not in waiting])[:max(SEEN_KEPT, len(current))]


class _Markdown(HTMLParser):
    """HTML as Discord markdown: paragraphs and line breaks, bold, italics, quotes, links, pictures as links, lists and code. Anything else keeps its text and loses its tags."""

    BLOCKS = {"p", "div", "section", "article", "header", "footer", "figure", "figcaption", "table", "ul", "ol", "dl", "dd", "dt", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}
    WRAP = {"strong": "**", "b": "**", "em": "*", "i": "*", "small": "*", "u": "__", "s": "~~", "strike": "~~", "del": "~~"}
    SKIP = {"script", "style", "head", "title", "noscript", "template"}
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skipping = 0
        self.pre = 0
        self.links: list[tuple[str | None, int]] = []
        self.quotes: list[int] = []
        self.lists: list[list[Any]] = []

    def _break(self, lines: int) -> None:
        self.out.append("\n" * lines)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag in self.SKIP:
            self.skipping += 1
        elif self.skipping:
            return
        elif tag == "br":
            self.out.append("\n")
        elif tag == "img":
            source = (found.get("src") or "").strip()
            if source:
                self.out.append(f"![{found.get('alt') or 'image'}]({source})")
        elif tag == "a":
            self.links.append((found.get("href"), len(self.out)))
        elif tag in ("blockquote", "aside"):
            self._break(2)
            self.quotes.append(len(self.out))
        elif tag == "pre":
            self._break(2)
            self.out.append("```\n")
            self.pre += 1
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("ul", "ol"):
            self._break(1)
            self.lists.append([tag, 0])
        elif tag == "li":
            self._break(1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self.out.append(f"{self.lists[-1][1]}. ")
            else:
                self.out.append("- ")
        elif tag in ("tr",):
            self._break(1)
        elif tag in ("td", "th"):
            self.out.append(" ")
        elif tag in self.BLOCKS:
            self._break(2)
            if tag in self.HEADINGS:
                self.out.append("**")
        elif tag in self.WRAP:
            self.out.append(self.WRAP[tag])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "img", "hr"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif self.skipping:
            return
        elif tag == "a" and self.links:
            href, start = self.links.pop()
            label = " ".join("".join(self.out[start:]).split())
            del self.out[start:]
            href = (href or "").strip()
            self.out.append(f"[{label or href}]({href})" if href.lower().startswith(("http://", "https://")) else label)
        elif tag in ("blockquote", "aside") and self.quotes:
            start = self.quotes.pop()
            text = _tidy("".join(self.out[start:]))
            del self.out[start:]
            if text:
                self.out.append("\n".join(f"> {line}" if line else ">" for line in text.split("\n")))
            self._break(2)
        elif tag == "pre" and self.pre:
            self.pre -= 1
            self.out.append("\n```")
            self._break(2)
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self._break(1 if self.lists else 2)
        elif tag in self.BLOCKS:
            if tag in self.HEADINGS:
                self.out.append("**")
            self._break(2)
        elif tag in self.WRAP:
            self.out.append(self.WRAP[tag])

    def handle_data(self, data: str) -> None:
        if self.skipping:
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def _tidy(text: str) -> str:
    """Trims each line and keeps at most one blank line in a row, leaving the inside of a code block as it was."""
    lines, inside = [], False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            inside = not inside
            lines.append(line.strip())
        else:
            lines.append(line.rstrip() if inside else line.strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def to_markdown(source: str) -> str:
    """An entry's HTML as Discord markdown. Entities are decoded first, as before, because many feeds escape their HTML twice."""
    parser = _Markdown()
    try:
        parser.feed(html.unescape(source or ""))
        parser.close()
    except (AssertionError, ValueError):  # html.parser gives up on some broken markup, and what it read so far still counts
        pass
    return _tidy("".join(parser.out))


def entry_text(entry: Entry) -> str:
    """The post for one entry, as before: the title as a heading, the text, and a Read more link, cut to fit in one message."""
    title = " ".join(entry.title.split())[:TITLE_LIMIT]
    head = f"# {title}" if title else ""
    tail = f"[Read more]({entry.link})" if entry.link else ""
    body = to_markdown(entry.html)
    room = MESSAGE_LIMIT - len(head) - len(tail) - 4
    if len(body) > room:
        body = body[:max(0, room - len(CUT))].rstrip() + CUT
    return "\n\n".join(part for part in (head, body, tail) if part)[:MESSAGE_LIMIT]


def refused_content(error: OpsError) -> bool:
    """Whether Discord turned the post down for what it held, which trying again can't change, as opposed to a missing permission, an outage
    or a channel with no room for the bot's webhook."""
    cause = error.__cause__
    return isinstance(cause, discord.HTTPException) and cause.status == 400 and cause.code != WEBHOOKS_FULL


def check_url(value: Any) -> str:
    """An address a person gave, once it's plain http or https to a public place."""
    if not isinstance(value, str) or not value.strip():
        raise Invalid("Give the feed's address.")
    url = value.strip()
    if len(url) > MAX_URL:
        raise Invalid(f"A feed's address can be at most {MAX_URL} characters.")
    try:
        check_public_link(url)
    except OutsideError as error:
        raise Invalid(str(error)) from error
    return url


def check_name(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise Invalid("The name must be text.")
    if len(value.strip()) > NAME_LENGTH:
        raise Invalid(f"A name can be at most {NAME_LENGTH} characters, the most Discord shows on a post.")
    return value.strip()


class RSSFeeds(Module):
    key = "rss"
    category = "Notifications"
    command_pack = True
    schema = Schema({
        "enabled": Field(False, boolean),
        "log_channel": Field(None, optional(snowflake)),
    })
    row_limits = {"rss_feeds": MAX_FEEDS}
    migrations = [
        """
        CREATE TABLE rss_feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            name TEXT,
            seen TEXT,
            posted_at REAL,
            created_by INTEGER,
            created_by_name TEXT,
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX rss_feeds_place ON rss_feeds (guild_id, channel_id, url);
        CREATE INDEX rss_feeds_url ON rss_feeds (url);
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.outside = Outside()
        self._clock = time.time
        self._round: asyncio.Future | None = None
        self._status: dict[int, dict] = {}
        self._tries: dict[tuple[int, str], int] = {}
        self._validators: dict[str, dict[str, str]] = {}
        self._feed_locks: dict[int, asyncio.Lock] = {}
        self._in_flight: set[asyncio.Future] = set()

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        """The log names the address and the name someone typed."""
        return {"log_channel": {settings["log_channel"]} if settings.get("log_channel") else set()}

    async def on_load(self) -> None:
        timers = self.bot.timers
        timers.handler(POLL, self._poll_due)
        if await timers.get_key(POLL, POLL) is None:
            await timers.schedule(POLL, self._clock() + POLL_SECONDS, key=POLL)

    async def cog_unload(self) -> None:
        if self._round is not None:
            self._round.cancel()
        if self._in_flight:  # a post on its way goes out and is remembered, so the next start doesn't post it again
            await asyncio.wait(set(self._in_flight), timeout=STOP_SECONDS)
            for task in self._in_flight:
                task.cancel()
        await self.outside.close()

    async def on_guild_restored(self, guild_id: int) -> None:
        for row in await self.db.all("SELECT id FROM rss_feeds WHERE guild_id = ?", (guild_id,)):
            self._status.pop(row["id"], None)

    # ---- reading every feed ----

    async def _poll_due(self, payload: dict, row: dict) -> None:
        await self.bot.timers.schedule(POLL, self._clock() + POLL_SECONDS, key=POLL)
        if self._round is not None and not self._round.done():
            return  # the last round is still reading slow addresses
        self._round = asyncio.ensure_future(self._guarded_round())

    async def _guarded_round(self) -> None:
        try:
            await self.poll()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("The RSS round failed. The next one runs on schedule.")

    async def poll(self) -> None:
        """One round: every address once, then each feed that follows it."""
        groups: dict[str, list[dict]] = {}
        for row in await self.db.all("SELECT * FROM rss_feeds ORDER BY id"):
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None or getattr(guild, "unavailable", False) or not self.on_in(row["guild_id"]):
                continue
            groups.setdefault(row["url"], []).append(row)
        gate = asyncio.Semaphore(FETCHES_AT_ONCE)

        async def one(url: str, feeds: list[dict]) -> None:
            async with gate:
                try:
                    entries = await self.fetch(url, fresh=any(feed["seen"] is None for feed in feeds))
                except FeedError as error:
                    for feed in feeds:
                        self._note(feed["id"], str(error))
                    return
            if entries is None:  # unchanged since the last read
                for feed in feeds:
                    self._note(feed["id"], None)
                return
            for feed in feeds:
                try:
                    await self.deliver(feed, entries)
                except Exception:  # one feed's bug must not cost the others their posts
                    log.exception("RSS feed %s failed", feed["id"])
                    self._note(feed["id"], "Something went wrong posting this feed. It's been logged.")

        await asyncio.gather(*(one(url, feeds) for url, feeds in groups.items()))

    async def fetch(self, url: str, *, fresh: bool = True, remember: bool = True) -> list[Entry] | None:
        """The feed's entries, newest first. None when the address says nothing changed since the last read. Raises FeedError with a sentence.
        Only a read for the feeds that follow the address keeps what it says about changes, so a test read in between can't hide an entry from them."""
        headers = {} if fresh else dict(self._validators.get(url, {}))
        try:
            reply = await self.outside.get(url, headers=headers, max_bytes=FEED_BYTES, timeout=FEED_SECONDS, stranger=True)
        except OutsideError as error:
            raise FeedError(str(error)) from error
        if reply.status == 304 and headers:
            return None
        if reply.status >= 400:
            raise FeedError(f"The address answered {reply.status}.")
        entries = await asyncio.to_thread(read_entries, reply.body, reply.url or url)  # a large feed takes a while to read, and the bot keeps answering meanwhile
        if entries is None:
            raise FeedError("That address didn't answer with an RSS or Atom feed.")
        if not remember:
            return entries
        validators = {name: value for name, value in (("If-None-Match", reply.header("ETag")), ("If-Modified-Since", reply.header("Last-Modified"))) if value}
        if validators:
            self._validators[url] = validators
        else:
            self._validators.pop(url, None)
        return entries

    def _note(self, feed_id: int, error: str | None, *, posted: float | None = None) -> None:
        status = self._status.setdefault(feed_id, {})
        status["checked_at"] = self._clock()
        status["error"] = error
        if posted:
            status["posted_at"] = posted

    async def deliver(self, feed: dict, entries: list[Entry]) -> int:
        """Posts what's new for one feed and remembers what it saw. Returns how many went out."""
        async with self._feed_locks.setdefault(feed["id"], asyncio.Lock()):
            row = await self.db.one("SELECT * FROM rss_feeds WHERE id = ?", (feed["id"],))
            if row is None:
                return 0  # removed while its address was being read
            seen = json.loads(row["seen"]) if row["seen"] else None
            fresh = fresh_entries(entries, seen)
            skipped = fresh[:-POSTS_PER_ROUND]
            waiting = [entry for entry in fresh if entry not in skipped]
            guild = self.bot.get_guild(row["guild_id"])
            channel = guild.get_channel(row["channel_id"]) if guild is not None else None
            if waiting and channel is None:
                self._note(row["id"], CHANNEL_GONE)
                return 0
            if waiting:
                try:
                    await self.bot.webhooks.hook(channel)
                except (OpsError, aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:  # like a missing permission, however long it lasts
                    self._note(row["id"], f"New entries wait until the bot has a webhook in that channel: {str(error) or type(error).__name__}")
                    return 0
            posted, problem = 0, None
            while waiting:
                entry = waiting[0]
                remembered = next_seen(entries, seen, {later.key for later in waiting[1:]})
                try:
                    await self._unstoppable(self._post_and_remember(channel, row, entry, remembered))
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:  # the connection dropped, with no answer from Discord
                    problem = f"The post for {entry.title or entry.key} didn't go out, and it's tried again next round: {str(error) or type(error).__name__}"
                    break
                except OpsError as error:
                    if not refused_content(error):  # a missing permission or Discord being down: the entry waits its turn, however long that takes
                        problem = f"The post for {entry.title or entry.key} didn't go out, and it's tried again next round: {error}"
                        break
                    tries = self._tries.get((row["id"], entry.key), 0) + 1
                    if tries < TRIES:
                        self._tries[(row["id"], entry.key)] = tries
                        problem = f"Discord refused the post for {entry.title or entry.key}, and it's tried again next round: {error}"
                        break
                    self._tries.pop((row["id"], entry.key), None)
                    problem = f"Skipped {entry.title or entry.key} after Discord refused it {TRIES} times: {error}"
                else:
                    self._tries.pop((row["id"], entry.key), None)
                    posted += 1
                waiting.pop(0)
            now = self._clock()
            seen = next_seen(entries, seen, {entry.key for entry in waiting})
            await self.db.run("UPDATE rss_feeds SET seen = ?, posted_at = COALESCE(?, posted_at) WHERE id = ?", (json.dumps(seen), now if posted else None, row["id"]))
            if skipped and not problem:
                problem = f"{len(skipped)} older new entries were left out, so the channel got the {POSTS_PER_ROUND} newest."
            self._note(row["id"], problem, posted=now if posted else None)
            return posted

    async def _post_and_remember(self, channel: Any, row: dict, entry: Entry, remembered: list[str]) -> None:
        """One post and the record that it went out, saved at once, so a dropped connection or a stop later in the round can't post it again."""
        await self.post(channel, row["name"], entry)
        await self.db.run("UPDATE rss_feeds SET seen = ?, posted_at = ? WHERE id = ?", (json.dumps(remembered), self._clock(), row["id"]))

    async def _unstoppable(self, work: Any) -> None:
        """Runs a post and its record where a stop can't come between them. The stop waits for it."""
        task = asyncio.ensure_future(work)
        self._in_flight.add(task)
        task.add_done_callback(self._settled)
        await asyncio.shield(task)

    def _settled(self, task: asyncio.Future) -> None:
        self._in_flight.discard(task)
        if not task.cancelled():
            task.exception()  # a stop may leave nobody waiting for its answer

    async def post(self, channel: Any, name: str | None, entry: Entry) -> None:
        """One entry through the bot's webhook in that channel, under the feed's name or the bot's own, with the bot's picture. Nobody is pinged.
        An entry with no title, text or link has nothing to show, so it's passed over."""
        text = entry_text(entry)
        if not text:
            return
        me = self.bot.user
        await self.bot.webhooks.send(channel, name=name or (me.name if me else None), avatar_url=me.display_avatar.url if me else None, content=text)

    # ---- adding and removing ----

    async def _count(self, guild_id: int) -> int:
        return await self.db.scalar("SELECT COUNT(*) FROM rss_feeds WHERE guild_id = ?", (guild_id,))

    async def add(self, guild_id: int, channel_id: int, url: str, name: str | None, actor: Any) -> dict:
        """Checks and stores a feed. Raises Invalid with a sentence for the person."""
        if await self.db.one("SELECT id FROM rss_feeds WHERE guild_id = ? AND channel_id = ? AND url = ?", (guild_id, channel_id, url)):
            raise Invalid(f"This RSS feed is already being monitored in <#{channel_id}>.")
        if await self._count(guild_id) >= MAX_FEEDS:
            raise Invalid(f"This server follows {MAX_FEEDS} feeds, the most it can. Remove one first.")
        try:
            await self.fetch(url, remember=False)  # the new feed's first round reads the address afresh
        except FeedError as error:
            raise Invalid(f"That address couldn't be read as a feed: {error}") from error
        now = self._clock()
        feed_id = await self.db.insert(
            "INSERT OR IGNORE INTO rss_feeds (guild_id, channel_id, url, name, created_by, created_by_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, url, name, actor.id, actor.name, now),
        )
        await self.bot.activity_log.record(guild_id, actor, "rss.add", detail=url[:400])
        await self.tell_log(guild_id, f"RSS feed added: {url} with username: {name or 'Bot default'} in <#{channel_id}> by <@{actor.id}>")
        return {"id": feed_id}

    async def remove(self, guild_id: int, feed: dict, actor: Any) -> None:
        await self.db.run("DELETE FROM rss_feeds WHERE guild_id = ? AND id = ?", (guild_id, feed["id"]))
        self._status.pop(feed["id"], None)
        self._feed_locks.pop(feed["id"], None)
        for key in [key for key in self._tries if key[0] == feed["id"]]:
            self._tries.pop(key)
        if not await self.db.scalar("SELECT COUNT(*) FROM rss_feeds WHERE url = ?", (feed["url"],)):
            self._validators.pop(feed["url"], None)
        await self.bot.activity_log.record(guild_id, actor, "rss.remove", detail=feed["url"][:400])
        await self.tell_log(guild_id, f"RSS feed removed: {feed['url']} in <#{feed['channel_id']}> by <@{actor.id}>")

    async def tell_log(self, guild_id: int, text: str) -> None:
        """A line in the log channel, when one is set and still there. Nobody is pinged."""
        channel_id = self.cfg(guild_id).get("log_channel")
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if channel_id and guild is not None else None
        if channel is None:
            return
        try:
            await channel.send(text[:MESSAGE_LIMIT], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            log.info("Could not post in the RSS log channel %s: %s", channel_id, error)

    async def test_post(self, channel: Any, url: str) -> bool:
        """Posts the feed's newest entry as a feed with no name of its own would. False when the feed has none. Raises FeedError or OpsError with a sentence."""
        entries = await self.fetch(url, remember=False)
        if not entries:
            return False
        await self.post(channel, None, entries[0])
        return True

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        """A feed whose channel is gone has nowhere to post, so it goes with it."""
        await self.db.run("DELETE FROM rss_feeds WHERE guild_id = ? AND channel_id = ?", (channel.guild.id, channel.id))

    # ---- commands ----

    async def _refuse_channel(self, ctx: commands.Context, channel: Any) -> bool:
        refusal = access.post_refusal(ctx.author, channel)
        if refusal:
            await ctx.fail(refusal)
            return True
        return False

    @commands.hybrid_command(name="list_feeds", description="List all currently configured RSS feeds")
    @commands.guild_only()
    @requires("administrator")
    async def list_feeds(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT url, channel_id, name FROM rss_feeds WHERE guild_id = ? ORDER BY id", (ctx.guild.id,))
        if not rows:
            await ctx.send("No RSS feeds are currently configured.", ephemeral=True)
            return
        blocks = []
        for row in rows:
            channel = ctx.guild.get_channel(row["channel_id"])
            where = channel.mention if channel else "Unknown Channel"
            who = f" with username '{discord.utils.escape_markdown(row['name'])}'" if row["name"] else " using the bot's default username"
            blocks.append(f"**Feed URL**: <{row['url']}>\n**Channel**: {where}{who}")
        for text in split_messages(LIST_HEADER, blocks):
            await ctx.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="test_feed", description="Test an RSS feed and post the latest entry to the specified channel")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(rss_url="The URL of the RSS feed to test", target_channel="The channel to post the test message")
    async def test_feed(self, ctx: commands.Context, rss_url: str, target_channel: discord.TextChannel) -> None:
        if await self._refuse_channel(ctx, target_channel):
            return
        try:
            url = check_url(rss_url)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        try:
            found = await self.test_post(target_channel, url)
        except (FeedError, OpsError) as error:
            await ctx.fail(str(error))
            return
        if not found:
            await ctx.send("No entries found in the RSS feed.", ephemeral=True)
            return
        await ctx.send(f"The latest entry from the feed has been posted to {target_channel.mention}.", ephemeral=True)

    @commands.hybrid_command(name="rss_log_channel", description="Set the logging channel for command activity")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(log_channel="The destination channel to log command activity")
    async def rss_log_channel(self, ctx: commands.Context, log_channel: discord.TextChannel) -> None:
        if await self._refuse_channel(ctx, log_channel):
            return
        await self.bot.settings.save(ctx.guild.id, self.key, {"log_channel": log_channel.id}, actor=self.bot.actor(ctx.author), action="rss.log_channel")
        await ctx.send(f"Logging channel has been set to {log_channel.mention}.", ephemeral=True)

    @commands.hybrid_command(name="add_feed", description="Add an RSS feed to monitor in the specified channel with a custom username")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(rss_url="The URL of the RSS feed to monitor", custom_username="(Optional) The custom username to post with", target_channel="The destination channel for RSS feed posts")
    async def add_feed(self, ctx: commands.Context, rss_url: str, target_channel: discord.TextChannel, *, custom_username: str | None = None) -> None:
        if await self._refuse_channel(ctx, target_channel):
            return
        try:
            url, name = check_url(rss_url), check_name(custom_username)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        try:
            await self.add(ctx.guild.id, target_channel.id, url, name, self.bot.actor(ctx.author))
        except Invalid as error:
            await ctx.fail(str(error))
            return
        if name:
            await ctx.send(f"RSS feed added and will be monitored in {target_channel.mention} with username: {discord.utils.escape_markdown(name)}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await ctx.send(f"RSS feed added and will be monitored in {target_channel.mention} using the bot's default username.", ephemeral=True)

    @commands.hybrid_command(name="remove_feed", description="Remove an RSS feed from the specified channel")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.describe(rss_url="The URL of the RSS feed to stop monitoring", target_channel="The destination channel from where to remove the RSS feed")
    async def remove_feed(self, ctx: commands.Context, rss_url: str, target_channel: discord.TextChannel) -> None:
        feed = await self.db.one("SELECT * FROM rss_feeds WHERE guild_id = ? AND channel_id = ? AND url = ?", (ctx.guild.id, target_channel.id, rss_url.strip()))
        if feed is None:
            await ctx.send(f"This RSS feed is not being monitored in {target_channel.mention}.", ephemeral=True)
            return
        await self.remove(ctx.guild.id, feed, self.bot.actor(ctx.author))
        await ctx.send(f"RSS feed removed from {target_channel.mention}.", ephemeral=True)

    @remove_feed.autocomplete("rss_url")
    async def _feed_urls(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """The server's feed addresses that fit in a choice. Discord takes at most 100 characters there, and a longer one can still be typed."""
        rows = await self.db.all("SELECT DISTINCT url FROM rss_feeds WHERE guild_id = ? ORDER BY url", (interaction.guild_id,))
        needle = current.strip().lower()
        return [app_commands.Choice(name=row["url"], value=row["url"]) for row in rows if len(row["url"]) <= 100 and needle in row["url"].lower()][:25]

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/rss", self._list_endpoint)
        router.post("/rss", self._add_endpoint)
        router.delete(r"/rss/{feed_id:\d+}", self._remove_endpoint)
        router.post("/rss/test", self._test_endpoint)

    async def _list_endpoint(self, req: Any) -> dict:
        rows = await self.db.all("SELECT id, channel_id, url, name, posted_at, created_by_name, created_at FROM rss_feeds WHERE guild_id = ? ORDER BY id", (req.guild_id,))
        feeds = []
        for row in rows:
            if not req.can_see(row["channel_id"]):
                continue
            status = self._status.get(row["id"], {})
            feeds.append({**row, "checked_at": status.get("checked_at"), "error": status.get("error"), "posted_at": status.get("posted_at") or row["posted_at"]})
        return {"feeds": feeds, "most": MAX_FEEDS, "total": len(rows)}

    def _form(self, req: Any, form: dict) -> tuple[int, str]:
        """The channel and the address a form names, once this person could post there and the address can be used."""
        channel_id = req.channel(form.get("channel_id"), posting=True)
        if (req.ops.channel(req.guild_id, channel_id) or {}).get("type") not in [kind.value for kind in POSTABLE]:
            raise ValidationError({"channel_id": "Pick a text or announcement channel."})
        try:
            url = check_url(form.get("url"))
        except Invalid as error:
            raise ValidationError({"url": str(error)}) from error
        return channel_id, url

    async def _add_endpoint(self, req: Any) -> dict:
        form = await req.json()
        channel_id, url = self._form(req, form)
        try:
            name = check_name(form.get("name"))
        except Invalid as error:
            raise ValidationError({"name": str(error)}) from error
        try:
            return await self.add(req.guild_id, channel_id, url, name, req.actor)
        except Invalid as error:
            raise ValidationError({"url": str(error)}) from error

    async def _remove_endpoint(self, req: Any) -> dict:
        feed = await self.db.one("SELECT * FROM rss_feeds WHERE guild_id = ? AND id = ?", (req.guild_id, req.int_param("feed_id")))
        if feed is None or not req.can_see(feed["channel_id"]):
            raise ApiError(404, "That feed isn't there anymore.")
        await self.remove(req.guild_id, feed, req.actor)
        return {"ok": True}

    async def _test_endpoint(self, req: Any) -> dict:
        form = await req.json()
        channel_id, url = self._form(req, form)
        guild = self.bot.get_guild(req.guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        if channel is None:
            raise ApiError(409, "The bot isn't connected to Discord here, so it can't post.")
        try:
            found = await self.test_post(channel, url)
        except FeedError as error:
            raise ValidationError({"url": str(error)}) from error
        if not found:
            raise ValidationError({"url": "No entries found in the RSS feed."})
        return {"ok": True}

    async def seed_demo(self, guild_id: int) -> None:
        """Sample feeds for the demo dashboard."""
        info = self.bot.ops.describe_guild(guild_id) or {}
        channels = {row["name"]: row["id"] for row in info.get("channels", [])}
        if "announcements" not in channels:
            return
        now = self._clock()
        for url, name in (("https://example.com/blog/feed.xml", "Blog"), ("https://example.com/status/history.atom", None)):
            feed_id = await self.db.insert(
                "INSERT INTO rss_feeds (guild_id, channel_id, url, name, seen, posted_at, created_by, created_by_name, created_at) VALUES (?, ?, ?, ?, '[]', ?, ?, ?, ?)",
                (guild_id, channels["announcements"], url, name, now - 3600, guild_id + 2, "Demo Admin", now - 86400),
            )
            self._status[feed_id] = {"checked_at": now - 30, "error": None, "posted_at": now - 3600}
        await self.bot.settings.save(guild_id, self.key, {"enabled": True, "log_channel": channels.get("mod-log")})


def split_messages(header: str, blocks: list[str]) -> list[str]:
    """The list as messages of at most 2,000 characters each, never splitting a block, with the header on the first. A block longer than a message is cut."""
    out: list[str] = []
    current = header
    for block in blocks:
        block = block[:MESSAGE_LIMIT - 2]
        if len(current) + 2 + len(block) > MESSAGE_LIMIT:
            out.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}"
    out.append(current)
    return out
