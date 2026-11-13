"""Reddit posts: an account's new posts, relayed in full to a channel.

A relayed post is the post itself, not a notice about it. Its title is a heading, its body follows word for word, and its pictures and
video are uploaded with it. A post too long for one message goes out as several, each ending where the post shows a picture. The last
message closes with a line that mentions the roles the server chose, and only that message can ping. core/reddit.py does the reading and
the layout. This file fetches, sends and keeps the records.

Posts come from two sources. One is the account's own feed on Reddit, read once a minute for all servers together, because Reddit takes
one read a minute from one address. Accounts take turns. The other is a courier: a small app on Reddit's developer platform, in the
courier folder beside this package, that hands each new post to a Discord webhook in a channel the server names. Either source alone is
enough, and a post that arrives by both goes out once.

Only posts made after an account was followed are relayed, and none older than a day. A post with the same title as one that went
to the same channel that day is passed over, since one announcement is often posted in several communities, or by two accounts of one company.

Progress is saved as each message lands, so a restart partway through a long post carries on with the next message. A post Discord can't
take right now, for a missing permission or an outage, waits and is tried again each minute. A message Discord refuses for what it holds
is left out, and the rest of the post still goes.
"""
from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ..core import access, delivery
from ..core import reddit as reader
from ..core.module import Module, requires
from ..core.ops import upload_limit
from ..core.outside import Outside, OutsideError, Reply
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, optional, snowflake
from ..web.api import ApiError

log = logging.getLogger(__name__)

POLL = "reddit.poll"
POLL_SECONDS = 60
MAX_FEEDS = 10
MAX_ROLES = 5
POSTS_KEPT = 200  # how many relayed posts a feed remembers, which is also how it knows a post went out before
KEEP_SECONDS = 30 * 86400
MAX_AGE = 86400
POSTS_PER_ROUND = 3
LOOKUP_TRIES = 5
LONGEST_BACKOFF = 1800
PIECES_KEPT = 3600.0  # how long the parts of a delivery wait for the rest
CATCH_UP = 500
FEED_BYTES = 4 * 1024 * 1024
FEED_SECONDS = 20.0
MEDIA_SECONDS = 90.0
MESSAGE_BYTES = 24 * 1024 * 1024  # the files of one message together, kept under what Discord takes in one request
TOO_LARGE = 40005  # Discord's code for a request with more in it than it takes
STOP_SECONDS = 5.0
FEED_HOSTS = frozenset({"www.reddit.com"})
EMBED_HOSTS = frozenset({"embed.reddit.com"})
POSTABLE = (discord.ChannelType.text, discord.ChannelType.news)
COURIER_NAME = "Reddit courier"
CHANNEL_GONE = "The channel for this account's posts no longer exists."
NOT_PINGED = "Posted, but the roles weren't pinged. The bot needs Mention Everyone in that channel, or Manage Roles and a place above each role, and each role has to exist."
SAME_TITLE = "Passed over: a post with the same title went to this channel earlier in the day."
TOO_OLD = "Passed over: it waited more than a day to go out."
NO_ACCOUNT = "Reddit has no feed for that account. Check the name."
SLOWED = "Reddit asked the bot to slow down, so the next read waits."


class FetchError(Exception):
    """Reddit didn't hand over what was asked for. The text is for the person reading the page or running the command."""


@dataclass
class Ready:
    """One message ready to send: its text, its files as (name, bytes), a message to send right after it for what didn't fit, and
    whether it is still the one that ends the post."""
    text: str
    files: list[tuple[str, bytes]] = field(default_factory=list)
    extra: dict | None = None
    last: bool = False


@dataclass
class Outcome:
    """How sending a post went. `problem` is a sentence for the page, also on a post that finished with something left out."""
    finished: bool
    problem: str | None = None
    first: Any = None
    sent: int = 0


def closing_line(roles: list[int]) -> str:
    """The line a post ends with, mentioning the roles a server chose. No roles, no line."""
    return "cc: " + " ".join(f"<@&{role}>" for role in roles) if roles else ""


def stored_roles(raw: Any) -> list[int]:
    """The roles a row names. A row can come from a backup file someone edited, so anything that isn't a list of role IDs counts as none."""
    try:
        found = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(found, list):
        return []
    return [role for role in found if isinstance(role, int) and not isinstance(role, bool) and 0 < role < 2 ** 63][:MAX_ROLES]


def title_key(title: str) -> str:
    return " ".join(title.split()).casefold()


def file_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "", name)[-80:] or "file"


def carry_on(plan: list[dict], done: int, last: bool, extra: dict | None) -> None:
    """Writes into a post's messages what message number `done` left behind once it's dealt with, sent or not. What didn't fit in it
    goes next as a message of its own. When the closing line moved there, this one no longer ends the post."""
    plan[done]["last"] = last
    if extra is not None:
        plan.insert(done + 1, extra)


def closing_alone(plan: list[dict], done: int, ready: Ready) -> None:
    """After Discord refused the message that ends a post, puts the closing line next as a message of its own, so the roles still hear
    of a post whose earlier messages went out. A post nobody saw gets no line, and a line refused on its own isn't tried again."""
    part = plan[done]
    closing = part.get("closing")
    if not ready.last or not isinstance(closing, str) or not closing or not ready.text.endswith(closing):
        return
    if not ready.files and ready.text.strip() == closing:
        return
    if not any(earlier.get("sent") for earlier in plan[:done]):
        return
    part["last"] = False
    plan.insert(done + 1, {"text": closing, "media": [], "last": True, "closing": closing})


class RedditPosts(Module):
    key = "reddit"
    category = "Notifications"
    command_pack = True
    schema = Schema({
        "enabled": Field(False, boolean),
        "intake_channel": Field(None, optional(snowflake)),
        "intake_webhook": Field(None, optional(snowflake)),
    })
    # Whoever sets where a courier delivers decides what the bot relays as an account's posts.
    admin_fields = frozenset({"intake_channel", "intake_webhook"})
    # A feed is brought back to POSTS_KEPT rows once an hour, so between two of those it can hold a few more.
    row_limits = {"reddit_feeds": MAX_FEEDS, "reddit_posts": 2 * MAX_FEEDS * POSTS_KEPT}
    child_links = {"reddit_posts": {"feed_id": "reddit_feeds.id"}}
    migrations = [
        """
        CREATE TABLE reddit_feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            account TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            role_ids TEXT NOT NULL DEFAULT '[]',
            last_error TEXT,
            posted_at REAL,
            created_by INTEGER,
            created_by_name TEXT,
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX reddit_feeds_account ON reddit_feeds (guild_id, account COLLATE NOCASE);
        CREATE TABLE reddit_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            feed_id INTEGER NOT NULL,
            post_key TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            data TEXT,
            plan TEXT,
            done INTEGER NOT NULL DEFAULT 0,
            tries INTEGER NOT NULL DEFAULT 0,
            roles TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL,
            note TEXT,
            source TEXT NOT NULL,
            found_at REAL NOT NULL,
            posted_at REAL,
            message_id INTEGER
        );
        CREATE UNIQUE INDEX reddit_posts_key ON reddit_posts (feed_id, post_key);
        CREATE INDEX reddit_posts_guild ON reddit_posts (guild_id, found_at);
        CREATE INDEX reddit_posts_state ON reddit_posts (state);
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.outside = Outside()
        self._clock = time.time
        self._sleep = asyncio.sleep
        self._round: asyncio.Future | None = None
        self._reads: dict[str, dict] = {}  # by account in lowercase: how the last read of its feed went
        self._next_read = 0.0  # when Reddit takes the next request
        self._limited = 0
        self._turn = asyncio.Lock()
        self._feed_locks: dict[int, asyncio.Lock] = {}
        self._pieces: dict[tuple[int, str, str], dict] = {}
        self._hooks: dict[int, Any] = {}
        self._delivered_at: dict[int, float] = {}
        self._relays: dict[int, dict] = {}
        self._in_flight: set[asyncio.Future] = set()
        self._tasks: set[asyncio.Future] = set()
        self._pruned_at = 0.0

    async def on_load(self) -> None:
        timers = self.bot.timers
        timers.handler(POLL, self._poll_due)
        if await timers.get_key(POLL, POLL) is None:
            await timers.schedule(POLL, self._clock() + POLL_SECONDS, key=POLL)

    async def cog_unload(self) -> None:
        for task in (self._round, *self._tasks):
            if task is not None:
                task.cancel()
        if self._in_flight:  # a message on its way goes out and is recorded, so the next start doesn't send it again
            await asyncio.wait(set(self._in_flight), timeout=STOP_SECONDS)
            for task in self._in_flight:
                task.cancel()
        await self.outside.close()

    async def on_guild_restored(self, guild_id: int) -> None:
        for key in [key for key in self._pieces if key[0] == guild_id]:
            self._pieces.pop(key)
        self._relays.pop(guild_id, None)

    # ---- rounds ----

    async def _poll_due(self, payload: dict, row: dict) -> None:
        await self.bot.timers.schedule(POLL, self._clock() + POLL_SECONDS, key=POLL)
        if self._round is not None and not self._round.done():
            return  # the last round is still sending a long post
        self._round = asyncio.ensure_future(self._guarded_round())

    async def _guarded_round(self) -> None:
        try:
            await self.run_round()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("The Reddit round failed. The next one runs on schedule.")

    async def run_round(self) -> None:
        """One round: read the feed of the account whose turn it is, then send every server what it's owed."""
        await self.read_next()
        await self.deliver_all()
        await self._prune()
        now = self._clock()
        if now < self._next_read < now + POLL_SECONDS:  # the next round comes when Reddit takes the next read, so a new post is seen as soon as it can be
            await self.bot.timers.schedule(POLL, max(self._next_read, now + 1), key=POLL)

    async def _followed(self) -> dict[str, list[dict]]:
        """The accounts being followed, in lowercase, each with the feeds of the servers that are here and have this switched on."""
        groups: dict[str, list[dict]] = {}
        for row in await self.db.all("SELECT * FROM reddit_feeds ORDER BY id"):
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None or getattr(guild, "unavailable", False) or not self.on_in(row["guild_id"]):
                continue
            groups.setdefault(row["account"].lower(), []).append(row)
        return groups

    async def read_next(self) -> None:
        groups = await self._followed()
        for name in [name for name in self._reads if name not in groups]:
            self._reads.pop(name)
        now = self._clock()
        if not groups or now < self._next_read:
            return
        due = [name for name in groups if self._reads.get(name, {}).get("next_at", 0.0) <= now]
        if not due:
            return
        account = min(due, key=lambda name: self._reads.get(name, {}).get("checked_at", 0.0))
        await self.read_account(account, groups[account])

    async def read_account(self, account: str, feeds: list[dict]) -> None:
        """Reads one account's feed and writes down, for each server following it, the posts that server is owed."""
        state = self._reads.setdefault(account, {"failures": 0})
        try:
            reply = await self._reddit_get(reader.feed_address(feeds[0]["account"]))
        except OutsideError as error:
            self._read_failed(state, str(error))
            return
        now = self._clock()
        if reply.status == 429:
            state.update(checked_at=now, error=SLOWED)
            return
        if reply.status != 200:
            self._read_failed(state, NO_ACCOUNT if reply.status == 404 else f"Reddit answered {reply.status} for this account's feed.")
            return
        posts = await asyncio.to_thread(reader.read_posts, reply.body)
        if posts is None:
            self._read_failed(state, "Reddit didn't answer with a feed.")
            return
        state.update(checked_at=now, ok_at=now, error=None, failures=0, next_at=0.0)
        for feed in feeds:
            try:
                await self.found(feed, posts, "feed")
            except Exception:  # one server's trouble must not cost the others their posts
                log.exception("Could not record the Reddit posts for feed %s", feed["id"])

    def _read_failed(self, state: dict, error: str) -> None:
        """An account whose feed fails is read less and less often, down to every half hour, and never given up on."""
        now = self._clock()
        state["failures"] = state.get("failures", 0) + 1
        state.update(checked_at=now, error=error, next_at=now + min(LONGEST_BACKOFF, POLL_SECONDS * 2 ** min(state["failures"], 5)))

    async def _reddit_get(self, url: str) -> Reply:
        """One request to Reddit's main site. It waits its turn, and for the moment Reddit said the next request may come."""
        async with self._turn:
            wait = self._next_read - self._clock()
            if wait > 0:
                await self._sleep(wait)
            try:
                reply = await self.outside.get(url, max_bytes=FEED_BYTES, timeout=FEED_SECONDS, hosts=FEED_HOSTS)
            except OutsideError:
                self._next_read = self._clock() + POLL_SECONDS  # nothing said when the next may go, so it waits a minute
                raise
            self._pace(reply)
            return reply

    def _pace(self, reply: Reply) -> None:
        """Works out when Reddit takes the next request, from what its answer says is left of this minute's allowance. With nothing left,
        the next goes when the allowance starts over. With some left, the requests are spread over the rest of the minute. An answer that
        says nothing counts as a minute."""
        now = self._clock()
        try:
            left = float(reply.header("x-ratelimit-remaining") or 0)
            used = float(reply.header("x-ratelimit-used") or 1)
            reset = float(reply.header("x-ratelimit-reset") or POLL_SECONDS)
        except ValueError:
            left, used, reset = 0.0, 1.0, float(POLL_SECONDS)
        reset = min(max(reset, 1.0), float(LONGEST_BACKOFF))
        if reply.status == 429:
            self._limited += 1
            wait = min(max(reset, reply.retry_after(reset)), float(LONGEST_BACKOFF))
            if self._limited > 2:  # still refused after waiting as told, so each further wait doubles
                wait = max(wait, min(LONGEST_BACKOFF, POLL_SECONDS * 2 ** (self._limited - 2)))
            self._next_read = now + wait + 1
            return
        self._limited = 0
        if left < 1:
            self._next_read = now + reset + 1
        else:
            even = reset - (POLL_SECONDS - POLL_SECONDS / (left + max(used, 1.0)) * max(used, 1.0))
            self._next_read = now + max(1.0, min(reset, even, 10.0))

    # ---- what a server is owed ----

    async def found(self, feed: dict, posts: list[reader.Post], source: str) -> int:
        """Writes down the posts a server is owed out of what a source just showed, oldest first. Returns how many are new to it."""
        floor = max(feed["created_at"], self._clock() - MAX_AGE)
        owed = 0
        for post in sorted(posts, key=lambda post: post.at or 0.0):
            if post.at is None or post.at < floor:
                continue
            if post.author and post.author.lower() != feed["account"].lower():
                continue
            if await self._owe(feed, post, source):
                owed += 1
        return owed

    async def _owe(self, feed: dict, post: reader.Post, source: str) -> bool:
        if await self.db.scalar("SELECT 1 FROM reddit_posts WHERE feed_id = ? AND post_key = ?", (feed["id"], post.key)):
            return False
        now = self._clock()
        # The same title in the same channel within a day is the same announcement, whichever of the followed accounts posted it.
        recent = await self.db.all(
            "SELECT p.title FROM reddit_posts p JOIN reddit_feeds f ON f.id = p.feed_id WHERE f.guild_id = ? AND f.channel_id = ? AND p.found_at > ? AND p.state != 'skipped'",
            (feed["guild_id"], feed["channel_id"], now - MAX_AGE),
        )
        twin = any(title_key(row["title"]) == title_key(post.title) for row in recent)
        added = await self.db.run(
            "INSERT OR IGNORE INTO reddit_posts (guild_id, feed_id, post_key, title, link, data, roles, state, note, source, found_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (feed["guild_id"], feed["id"], post.key, post.title, post.link, None if twin else post.to_json(), feed["role_ids"], "skipped" if twin else "waiting", SAME_TITLE if twin else None, source, now),
        )
        return bool(added) and not twin

    async def _prune(self) -> None:
        """Once an hour, forgets relayed posts older than a month, and keeps each feed to its newest few hundred."""
        now = self._clock()
        if now - self._pruned_at < 3600:
            return
        self._pruned_at = now
        await self.db.run("DELETE FROM reddit_posts WHERE state != 'waiting' AND found_at < ?", (now - KEEP_SECONDS,))
        for row in await self.db.all("SELECT feed_id FROM reddit_posts GROUP BY feed_id HAVING COUNT(*) > ?", (POSTS_KEPT,)):
            await self.db.run(
                "DELETE FROM reddit_posts WHERE feed_id = ? AND state != 'waiting' AND id NOT IN (SELECT id FROM reddit_posts WHERE feed_id = ? ORDER BY id DESC LIMIT ?)",
                (row["feed_id"], row["feed_id"], POSTS_KEPT),
            )

    # ---- sending ----

    async def deliver_all(self) -> None:
        rows = await self.db.all("SELECT DISTINCT feed_id FROM reddit_posts WHERE state = 'waiting'")
        await asyncio.gather(*(self._deliver_guarded(row["feed_id"]) for row in rows))

    async def _deliver_guarded(self, feed_id: int) -> None:
        try:
            await self.deliver(feed_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # one feed's bug must not cost the others their posts
            log.exception("Reddit feed %s failed", feed_id)
            await self._note(feed_id, "Something went wrong relaying this account's posts. It's been logged.")

    def _start(self, work: Awaitable[Any]) -> asyncio.Future:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _note(self, feed_id: int, problem: str | None) -> None:
        await self.db.run("UPDATE reddit_feeds SET last_error = ? WHERE id = ?", (problem, feed_id))

    async def deliver(self, feed_id: int) -> None:
        """Sends one server the posts it's owed from one account, oldest first. A post that can't go yet keeps the later ones waiting, so
        the order holds."""
        async with self._feed_locks.setdefault(feed_id, asyncio.Lock()):
            feed = await self.db.one("SELECT * FROM reddit_feeds WHERE id = ?", (feed_id,))
            if feed is None:
                await self.db.run("DELETE FROM reddit_posts WHERE feed_id = ?", (feed_id,))
                return
            guild = self.bot.get_guild(feed["guild_id"])
            if guild is None or getattr(guild, "unavailable", False) or not self.on_in(feed["guild_id"]):
                return
            channel = await delivery.find_channel(guild, feed["channel_id"])
            if channel is None or not delivery.holds_messages(channel):
                await self._note(feed_id, CHANNEL_GONE if channel is None else delivery.NOT_POSTABLE)
                return
            for row in await self.db.all("SELECT * FROM reddit_posts WHERE feed_id = ? AND state = 'waiting' ORDER BY id LIMIT ?", (feed_id, POSTS_PER_ROUND)):
                if not await self._deliver_post(feed, guild, channel, row):
                    break

    async def _settle(self, row: dict, state: str, note: str | None) -> None:
        """A post that's done with, one way or the other. Its body is dropped, and the row stays as the record that it was seen."""
        await self.db.run("UPDATE reddit_posts SET state = ?, note = ?, data = NULL, plan = NULL, posted_at = ? WHERE id = ?", (state, note, self._clock() if state == "sent" else None, row["id"]))

    async def _deliver_post(self, feed: dict, guild: Any, channel: Any, row: dict) -> bool:
        """Sends one owed post, from the message it got to. True when the post is done with, and False when it has to wait."""
        post = reader.Post.from_json(row["data"])
        if post is None:
            await self._settle(row, "skipped", "Passed over: the stored post couldn't be read.")
            return True
        if post.at is not None and self._clock() - post.at > MAX_AGE:
            await self._settle(row, "skipped", TOO_OLD)
            return True
        roles = stored_roles(row["roles"])
        plan = reader.checked_plan(row["plan"])
        done = max(0, min(row["done"], len(plan))) if plan is not None else 0
        if plan is None:
            try:
                plan = await self.plan(post, roles, patient=row["tries"] + 1 < LOOKUP_TRIES)
            except FetchError as error:
                await self.db.run("UPDATE reddit_posts SET tries = tries + 1 WHERE id = ?", (row["id"],))
                await self._note(feed["id"], f"The pictures of \"{post.title}\" couldn't be listed yet, and it's tried again in a minute: {error}")
                return False
            await self.db.run("UPDATE reddit_posts SET plan = ?, done = 0 WHERE id = ?", (json.dumps(plan), row["id"]))

        async def save(plan: list[dict], done: int, message_id: int | None) -> None:
            await self.db.run("UPDATE reddit_posts SET plan = ?, done = ?, message_id = COALESCE(message_id, ?) WHERE id = ?", (json.dumps(plan), done, message_id, row["id"]))

        outcome = await self.send_plan(guild, channel, plan, done, roles, save)
        if not outcome.finished:
            await self._note(feed["id"], f"\"{post.title}\" is waiting, and it's tried again in a minute. {outcome.problem}")
            return False
        await self._settle(row, "sent", outcome.problem)
        await self.db.run("UPDATE reddit_feeds SET posted_at = ?, last_error = ? WHERE id = ?", (self._clock(), outcome.problem, feed["id"]))
        return True

    async def plan(self, post: reader.Post, roles: list[int], *, patient: bool = False) -> list[dict]:
        """The messages a post goes out as. A gallery's pictures are listed on the post's embed page. When that page can't be had, a
        patient caller gets FetchError so it can try again later, and any other gets the post with a link to its gallery."""
        gallery = None
        if reader.needs_gallery(post):
            try:
                gallery = (await self._embed(reader.embed_address(post.link))).gallery
            except FetchError:
                if patient:
                    raise
        return reader.layout(post, reader.lead_media(post, gallery), closing_line(roles))

    async def send_plan(self, guild: Any, channel: Any, plan: list[dict], done: int, roles: list[int], save: Callable[[list[dict], int, int | None], Awaitable[None]]) -> Outcome:
        """Sends a post's messages from number `done` on, saving after each one. Stops at the first that can't go right now."""
        outcome = Outcome(True, sent=done)
        as_links = False
        while done < len(plan):
            part = plan[done]
            mark = part.get("unsure")
            if isinstance(mark, dict) and await self._went_out(channel, mark):
                part.pop("unsure")
                part["sent"] = True
                extra = mark.get("extra")  # a row can come from a backup file someone edited, so the mark is read as carefully as the plan
                carry_on(plan, done, mark["last"] if isinstance(mark.get("last"), bool) else bool(part.get("last")), extra if reader.checked_part(extra) else None)
                done += 1
                await save(plan, done, None)
                continue
            ready = await self._prepare(guild, part, as_links)
            try:
                message, pinged = await self._unstoppable(self._send_and_save(guild, channel, plan, done, ready, roles, save))
            except discord.HTTPException as error:
                if (error.status == 413 or error.code == TOO_LARGE) and ready.files and not as_links:
                    as_links = True  # the files were more than Discord takes here, so this message goes with their links
                    continue
                if not delivery.refused_for_good(error):
                    return Outcome(False, f"Discord answered: {error.text or error.status}", outcome.first, done)
                outcome.problem = f"Discord refused one message of this post, and the rest went out: {error.text or error.status}"
                carry_on(plan, done, ready.last, ready.extra)
                closing_alone(plan, done, ready)
                done += 1
                await save(plan, done, None)
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:
                # What was to follow this message is kept with the mark, so it still goes when the message turns out to have landed.
                part["unsure"] = {"text": ready.text, "files": [name for name, _ in ready.files], "last": ready.last, "extra": ready.extra}
                await save(plan, done, None)
                return Outcome(False, f"The connection to Discord dropped: {str(error) or type(error).__name__}", outcome.first, done)
            else:
                done += 1
                outcome.first = outcome.first or message
                if part.get("last") and roles and not pinged:
                    outcome.problem = outcome.problem or NOT_PINGED
            as_links = False
        outcome.sent = done
        return outcome

    async def _prepare(self, guild: Any, part: dict, as_links: bool) -> Ready:
        """Fetches a message's files. One that can't be had goes out as its link, under the message's words. Files past what one message
        takes move to a message of their own, sent right after, and so do links the message has no room for. The closing line stays
        last through all of it: links go above it, and when anything moves to the next message, the line and its ping move there too."""
        most = min(upload_limit(guild), MESSAGE_BYTES)
        ready = Ready(part["text"], last=bool(part.get("last")))
        links: list[str] = []
        left: list[dict] = []
        total = 0
        for index, item in enumerate(part["media"]):
            body = None if as_links else await self._download(item, most)
            if body is None:
                links.append(item["link"])
                continue
            if ready.files and total + len(body) > MESSAGE_BYTES:
                left = part["media"][index:]
                break
            ready.files.append((file_name(item["name"]), body))
            total += len(body)
        joined = "\n".join(dict.fromkeys(link for link in links if link))
        if not joined and not left:
            return ready
        closing = part.get("closing") if ready.last else None
        if not isinstance(closing, str) or not closing or not ready.text.endswith(closing):
            closing = ""
        words = ready.text[:len(ready.text) - len(closing)].rstrip() if closing else ready.text
        spill = ""
        if joined and not words:
            words = joined[:reader.MESSAGE_LIMIT]
        elif joined and len(words) + 1 + len(joined) <= reader.MESSAGE_LIMIT:
            words = f"{words}\n{joined}"
        elif joined:
            spill = joined[:reader.MESSAGE_LIMIT - (len(closing) + 2 if closing else 0)]
        if closing and not left and not spill and len(words) + 2 + len(closing) <= reader.MESSAGE_LIMIT:
            ready.text = f"{words}\n\n{closing}"
        elif closing:
            ready.text, ready.last = words, False
            ready.extra = {"text": f"{spill}\n\n{closing}" if spill else closing, "media": left, "last": True, "closing": closing}
        else:
            ready.text = words
            if spill or left:
                ready.extra = {"text": spill, "media": left, "last": False}
        return ready

    async def _download(self, item: dict, most: int) -> bytes | None:
        """One picture or video, from the first of its addresses that gives a file small enough to upload."""
        urls = list(item["urls"])
        if item["kind"] == "video" and item["page"]:
            try:
                urls = reader.video_choices((await self._embed(item["page"])).videos, most) + urls
            except FetchError as error:
                log.info("Could not list a video's files: %s", error)
        for url in urls[:8]:
            try:
                reply = await self.outside.get(url, max_bytes=most, timeout=MEDIA_SECONDS, hosts=reader.MEDIA_HOSTS)
            except OutsideError:
                continue
            kind = (reply.header("content-type") or "").split(";")[0].strip().lower()
            if reply.status == 200 and reply.body and kind.startswith(("image/", "video/")):
                return reply.body
        return None

    async def _embed(self, url: str) -> reader.Embed:
        try:
            reply = await self.outside.get(url, max_bytes=FEED_BYTES, timeout=FEED_SECONDS, hosts=EMBED_HOSTS)
        except OutsideError as error:
            raise FetchError(str(error)) from error
        if reply.status != 200:
            raise FetchError(f"Reddit answered {reply.status} for the post's embed page.")
        return await asyncio.to_thread(reader.read_embed, reply.text())

    async def _send_and_save(self, guild: Any, channel: Any, plan: list[dict], done: int, ready: Ready, roles: list[int], save: Callable[[list[dict], int, int | None], Awaitable[None]]) -> tuple[Any, bool]:
        """One message and the record that it went out, saved at once, so a dropped connection or a stop later on can't send it again.
        Only the message that ends a post can ping, and only the roles the server chose. Nothing a post says can ping anyone."""
        part = plan[done]
        pings = bool(ready.last and roles)
        mentions = discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(role) for role in roles], replied_user=False) if pings else discord.AllowedMentions.none()
        kwargs: dict[str, Any] = {"allowed_mentions": mentions}
        if ready.files:
            kwargs["files"] = [discord.File(io.BytesIO(body), filename=name) for name, body in ready.files]
        pinged = True
        if pings:
            async with delivery.role_pings(self.db, guild, channel, roles) as pinged:
                message = await channel.send(ready.text or None, **kwargs)
        else:
            message = await channel.send(ready.text or None, **kwargs)
        part.pop("unsure", None)
        part["sent"] = True
        carry_on(plan, done, ready.last, ready.extra)
        await save(plan, done + 1, message.id)
        return message, bool(pinged)

    async def _went_out(self, channel: Any, mark: dict) -> bool:
        """Whether a message did go out when the connection dropped during its send, by looking for it among the channel's last messages."""
        me = self.bot.user
        try:
            async for message in channel.history(limit=20):
                if me is not None and message.author.id == me.id and message.content == mark.get("text") and [file.filename for file in message.attachments] == mark.get("files"):
                    return True
        except (discord.HTTPException, aiohttp.ClientError, OSError, asyncio.TimeoutError):
            pass
        return False

    async def _unstoppable(self, work: Any) -> Any:
        """Runs a send and its record where a stop can't come between them. The stop waits for it."""
        task = asyncio.ensure_future(work)
        self._in_flight.add(task)
        task.add_done_callback(self._settled)
        return await asyncio.shield(task)

    def _settled(self, task: asyncio.Future) -> None:
        self._in_flight.discard(task)
        if not task.cancelled():
            task.exception()  # a stop may leave nobody waiting for its answer

    # ---- one post, by its link ----

    async def fetch_post(self, link: str) -> reader.Post:
        """One post by its link: from its own feed, and from its embed page when the feed doesn't give it. Raises FetchError with a sentence."""
        key = reader.post_key(link)
        problem = ""
        try:
            reply = await self._reddit_get(reader.post_feed_address(link))
            if reply.status == 429:  # told to slow down, so it waits as told and asks once more
                reply = await self._reddit_get(reader.post_feed_address(link))
            if reply.status == 200:
                posts = await asyncio.to_thread(reader.read_posts, reply.body) or []
                found = next((post for post in posts if post.key == key), None)
                if found is not None:
                    return found
            problem = f"Reddit answered {reply.status} for the post's feed."
        except OutsideError as error:
            problem = str(error)
        try:
            embed = await self._embed(reader.embed_address(link))
        except FetchError as error:
            raise FetchError(f"Reddit didn't hand over that post. {problem} {error}") from error
        post = embed.post(key or "", link)
        if post is None:
            raise FetchError("Reddit's page for that link holds no post.")
        if reader.kind(post) != "text":  # the embed page of any other kind of post leaves out the post's words
            raise FetchError(f"Reddit handed over that post's title and pictures but not its words, so nothing was sent. {problem}")
        return post

    async def relay(self, guild: Any, channel: Any, link: str, roles: list[int], note: Callable[[dict], None] | None = None) -> tuple[reader.Post, Outcome]:
        """Sends one post to a channel now, whatever its age. Raises FetchError when Reddit doesn't hand it over. A post this server would
        also get from the account's feed is marked as sent, so it doesn't go out twice."""
        post = await self.fetch_post(link)
        plan = await self.plan(post, roles)
        if note:
            note({"title": post.title, "state": "sending", "done": 0, "of": len(plan)})

        async def save(plan: list[dict], done: int, message_id: int | None) -> None:
            if note:
                note({"title": post.title, "state": "sending", "done": done, "of": len(plan)})

        outcome = await self.send_plan(guild, channel, plan, 0, roles, save)
        feed = await self.db.one("SELECT * FROM reddit_feeds WHERE guild_id = ? AND account = ? COLLATE NOCASE", (guild.id, post.author)) if post.author else None
        if feed is not None and outcome.finished:
            now = self._clock()
            first = outcome.first.id if outcome.first is not None else None
            # A copy the feed already has waiting, with none of it sent yet, counts as this one.
            await self.db.run(
                "UPDATE reddit_posts SET state = 'sent', note = NULL, data = NULL, plan = NULL, posted_at = ?, message_id = ? WHERE feed_id = ? AND post_key = ? AND state = 'waiting' AND done = 0",
                (now, first, feed["id"], post.key),
            )
            await self.db.run(
                "INSERT OR IGNORE INTO reddit_posts (guild_id, feed_id, post_key, title, link, roles, state, source, found_at, posted_at, message_id) VALUES (?, ?, ?, ?, ?, ?, 'sent', 'command', ?, ?, ?)",
                (guild.id, feed["id"], post.key, post.title, post.link, json.dumps(roles), now, now, first),
            )
        return post, outcome

    # ---- a courier's deliveries ----

    @commands.Cog.listener()
    async def on_message(self, message: Any) -> None:
        if message.guild is None or message.webhook_id is None or not message.embeds or not self.on_in(message.guild.id):
            return
        cfg = self.cfg(message.guild.id)
        if message.channel.id != cfg["intake_channel"] or message.webhook_id != cfg["intake_webhook"]:
            return
        try:
            await self.intake(message)
        except Exception:
            log.exception("A courier's delivery failed in %s", message.guild.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """What a courier delivered while the bot was away is still in the channel, so it's read now."""
        for guild in list(self.bot.guilds):
            try:
                await self.catch_up(guild)
            except Exception:
                log.exception("Could not read the courier's channel in %s", guild.id)

    async def catch_up(self, guild: Any) -> None:
        cfg = self.cfg(guild.id)
        channel = guild.get_channel(cfg["intake_channel"] or 0)
        if channel is None or not cfg["intake_webhook"] or not self.on_in(guild.id):
            return
        since = datetime.datetime.fromtimestamp(self._clock() - MAX_AGE, datetime.timezone.utc)
        try:
            waiting = [message async for message in channel.history(limit=CATCH_UP, after=since, oldest_first=True)]
        except discord.HTTPException as error:
            log.info("Could not read the courier's channel in %s: %s", guild.id, error)
            return
        for message in waiting:
            if message.webhook_id == cfg["intake_webhook"] and message.embeds:
                await self.intake(message)

    async def intake(self, message: Any) -> None:
        """Takes in one message of a delivery. A long post comes as several, and it's put together when the last part is in."""
        now = self._clock()
        for key in [key for key, held in self._pieces.items() if now - held["at"] > PIECES_KEPT]:
            self._pieces.pop(key)
        for embed in message.embeds:
            piece = reader.read_piece(embed.title, embed.description)
            if piece is None:
                continue
            key = (message.guild.id, piece.key, piece.check)
            held = self._pieces.setdefault(key, {"at": now, "pieces": {}, "messages": set()})
            held["pieces"][piece.part] = piece
            held["messages"].add(message.id)
            post = reader.assemble(held["pieces"].values())
            if post is None:
                continue
            self._pieces.pop(key, None)
            self._delivered_at[message.guild.id] = now
            parts = reader.layout(post, reader.lead_media(post), "")
            log.info("A courier delivered %s by u/%s in %s: a %s post, %d messages, text check %s", post.key, post.author, message.guild.id, reader.kind(post), len(parts),
                     reader.checksum("\n".join(part["text"] for part in parts)))
            feed = await self.db.one("SELECT * FROM reddit_feeds WHERE guild_id = ? AND account = ? COLLATE NOCASE", (message.guild.id, post.author)) if post.author else None
            if feed is not None and await self.found(feed, [post], "courier"):
                self._start(self._deliver_guarded(feed["id"]))
            await self._clear(message.channel, held["messages"])

    async def _intake_hook(self, channel: Any) -> Any:
        """The webhook a courier delivers through, with its token, which the bot holds because it made the webhook."""
        hook_id = self.cfg(channel.guild.id)["intake_webhook"]
        if hook_id and hook_id not in self._hooks:
            for hook in await channel.webhooks():
                if hook.id == hook_id and hook.token:
                    self._hooks[hook_id] = hook
        return self._hooks.get(hook_id)

    async def _clear(self, channel: Any, message_ids: set[int]) -> None:
        """Deletes a delivery's messages once the post is on record, so the courier's channel doesn't fill up. Where the bot can't, they stay."""
        try:
            hook = await self._intake_hook(channel)
            for message_id in sorted(message_ids) if hook is not None else ():
                await hook.delete_message(message_id)
        except (discord.HTTPException, aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:
            log.info("Could not clear a courier's delivery in %s: %s", channel.guild.id, error)

    async def set_intake(self, guild: Any, channel: Any, actor: Any) -> str:
        """Makes the webhook a courier delivers through and remembers it. Returns the webhook's address, which is the courier's to keep.
        The webhook it replaces is deleted once the new one exists, so a refusal leaves the old one working."""
        try:
            hook = await channel.create_webhook(name=COURIER_NAME, reason="Receives posts from a Reddit courier")
        except discord.Forbidden as error:
            raise Invalid("The bot needs Manage Webhooks in that channel.") from error
        except discord.HTTPException as error:
            raise Invalid(f"Discord refused the webhook: {error.text or error.status}") from error
        await self.drop_intake(guild, actor, keep_settings=True)
        self._hooks[hook.id] = hook
        await self.bot.settings.save(guild.id, self.key, {"intake_channel": channel.id, "intake_webhook": hook.id}, actor=actor, action="reddit.intake")
        return hook.url

    async def drop_intake(self, guild: Any, actor: Any, *, keep_settings: bool = False) -> None:
        cfg = self.cfg(guild.id)
        channel = guild.get_channel(cfg["intake_channel"] or 0)
        if channel is not None and cfg["intake_webhook"]:
            try:
                hook = await self._intake_hook(channel)
                if hook is not None:
                    await hook.delete(reason="The Reddit courier's channel was changed")
            except discord.HTTPException as error:
                log.info("Could not delete the courier's old webhook in %s: %s", guild.id, error)
        self._hooks.pop(cfg["intake_webhook"], None)
        if not keep_settings:
            await self.bot.settings.save(guild.id, self.key, {"intake_channel": None, "intake_webhook": None}, actor=actor, action="reddit.intake")

    # ---- following and unfollowing ----

    async def follow(self, guild_id: int, account: str, channel_id: int, roles: list[int], actor: Any) -> dict:
        """Starts relaying an account's posts. Raises Invalid with a sentence for the person."""
        if await self.db.one("SELECT id FROM reddit_feeds WHERE guild_id = ? AND account = ? COLLATE NOCASE", (guild_id, account)):
            raise Invalid(f"This server already relays u/{account}.")
        if await self.db.scalar("SELECT COUNT(*) FROM reddit_feeds WHERE guild_id = ?", (guild_id,)) >= MAX_FEEDS:
            raise Invalid(f"This server relays {MAX_FEEDS} accounts, the most it can. Remove one first.")
        feed_id = await self.db.insert(
            "INSERT INTO reddit_feeds (guild_id, account, channel_id, role_ids, created_by, created_by_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, account, channel_id, json.dumps(roles), actor.id, actor.name, self._clock()),
        )
        await self.bot.activity_log.record(guild_id, actor, "reddit.add", detail=account)
        return {"id": feed_id, "account": account}

    async def unfollow(self, feed: dict, actor: Any) -> None:
        await self.db.run("DELETE FROM reddit_posts WHERE feed_id = ?", (feed["id"],))
        await self.db.run("DELETE FROM reddit_feeds WHERE id = ?", (feed["id"],))
        self._feed_locks.pop(feed["id"], None)
        await self.bot.activity_log.record(feed["guild_id"], actor, "reddit.remove", detail=feed["account"])

    # ---- commands ----

    async def _refuse_target(self, ctx: commands.Context, channel: Any, role: Any) -> bool:
        """Whether this person can't aim posts at this channel. The posts go out later in the bot's name, and so does the ping."""
        if role is not None and role.is_default():
            await ctx.fail("Pick a role of its own. @everyone can't be the role a post mentions.")
            return True
        refusal = access.post_refusal(ctx.author, channel)
        if refusal is None and role is not None and delivery.needs_ping_right(ctx.guild, role.id):
            refusal = access.ping_refusal(ctx.author, channel)
        if refusal:
            await ctx.fail(refusal)
            return True
        return False

    @commands.hybrid_group(name="reddit", fallback="list", description="Show the Reddit accounts whose posts this server relays")
    @commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @requires("manage_guild")
    async def reddit(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT * FROM reddit_feeds WHERE guild_id = ? ORDER BY account COLLATE NOCASE", (ctx.guild.id,))
        lines = [f"[u/{row['account']}](<{reader.SITE}/user/{row['account']}>) in <#{row['channel_id']}>" for row in rows]
        await ctx.send(embed=discord.Embed(title="Reddit posts", description=delivery.clipped(lines) or "This server relays no Reddit accounts yet.", color=0xFF4500), allowed_mentions=discord.AllowedMentions.none())

    @reddit.command(name="add", description="Relay an account's new Reddit posts, in full, to a channel")
    @requires("manage_guild")
    @app_commands.describe(account="The account's name or address", channel="Where its posts go. This channel if you leave it out.", role="A role to mention under each post")
    async def reddit_add(self, ctx: commands.Context, account: str, channel: discord.TextChannel | None = None, role: discord.Role | None = None) -> None:
        target = channel or ctx.channel
        if not delivery.holds_messages(target):
            await ctx.fail(delivery.NOT_POSTABLE)
            return
        if await self._refuse_target(ctx, target, role):
            return
        try:
            name = reader.account_name(account)
            await self.follow(ctx.guild.id, name, target.id, [role.id] if role else [], self.bot.actor(ctx.author))
        except Invalid as error:
            await ctx.fail(str(error))
            return
        await ctx.ok(f"Posts u/{name} makes from now on are relayed to {target.mention}.")

    @reddit.command(name="remove", description="Stop relaying an account's Reddit posts")
    @requires("manage_guild")
    @app_commands.describe(account="The account's name")
    async def reddit_remove(self, ctx: commands.Context, account: str) -> None:
        try:
            name = reader.account_name(account)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        feed = await self.db.one("SELECT * FROM reddit_feeds WHERE guild_id = ? AND account = ? COLLATE NOCASE", (ctx.guild.id, name))
        if feed is None:
            await ctx.fail(f"This server doesn't relay u/{name}.")
            return
        channel = ctx.guild.get_channel_or_thread(feed["channel_id"])
        refusal = access.view_refusal(ctx.author, channel) if channel is not None else None
        if refusal:
            await ctx.fail(refusal)
            return
        await self.unfollow(feed, self.bot.actor(ctx.author))
        await ctx.ok(f"Posts from u/{feed['account']} are no longer relayed. The ones already here stay.")

    @reddit.command(name="post", description="Relay one Reddit post now, in full, by its link")
    @requires("manage_guild")
    @app_commands.describe(link="The post's link", channel="Where it goes. This channel if you leave it out.", role="A role to mention under it")
    async def reddit_post(self, ctx: commands.Context, link: str, channel: discord.TextChannel | None = None, role: discord.Role | None = None) -> None:
        target = channel or ctx.channel
        if not delivery.holds_messages(target):
            await ctx.fail(delivery.NOT_POSTABLE)
            return
        if await self._refuse_target(ctx, target, role):
            return
        try:
            address = reader.post_link(link)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        await ctx.defer(ephemeral=True)
        try:
            post, outcome = await self.relay(ctx.guild, target, address, [role.id] if role else [])
        except FetchError as error:
            await ctx.fail(str(error))
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "reddit.post", detail=post.link[:400])
        if not outcome.finished:
            await ctx.fail(f"{outcome.sent} of the post's messages went to {target.mention}, and then it stopped. {outcome.problem}")
        elif outcome.problem:
            await ctx.ok(f"\"{post.title}\" is in {target.mention}. {outcome.problem}")
        else:
            await ctx.ok(f"\"{post.title}\" is in {target.mention}.")

    @reddit_remove.autocomplete("account")
    async def _account_names(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        rows = await self.db.all("SELECT account FROM reddit_feeds WHERE guild_id = ? ORDER BY account COLLATE NOCASE", (interaction.guild_id,))
        needle = current.strip().lower().removeprefix("u/")
        return [app_commands.Choice(name=f"u/{row['account']}", value=row["account"]) for row in rows if needle in row["account"].lower()][:25]

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/reddit", self._api_list)
        router.post("/reddit", self._api_add)
        router.put(r"/reddit/{feed_id:\d+}", self._api_edit)
        router.delete(r"/reddit/{feed_id:\d+}", self._api_remove)
        router.post("/reddit/post", self._api_post)
        router.post("/reddit/intake", self._api_intake_set)
        router.get("/reddit/intake", self._api_intake_address)
        router.delete("/reddit/intake", self._api_intake_drop)

    async def _api_list(self, req: Any) -> dict:
        cfg = self.cfg(req.guild_id)
        feeds = []
        for row in await self.db.all("SELECT * FROM reddit_feeds WHERE guild_id = ? ORDER BY account COLLATE NOCASE", (req.guild_id,)):
            hidden = not req.can_see(row["channel_id"])  # a row in a channel this person can't see stays listed, since it counts toward the limit
            read = self._reads.get(row["account"].lower(), {})
            feeds.append({
                "id": row["id"], "account": row["account"], "channel_id": row["channel_id"], "hidden": hidden, "role_ids": [] if hidden else stored_roles(row["role_ids"]),
                "last_error": None if hidden else row["last_error"], "posted_at": row["posted_at"], "created_by_name": row["created_by_name"],
                "waiting": await self.db.scalar("SELECT COUNT(*) FROM reddit_posts WHERE feed_id = ? AND state = 'waiting'", (row["id"],)),
                "read_at": read.get("ok_at"), "read_error": read.get("error"),
            })
        visible = {feed["id"]: feed for feed in feeds if not feed["hidden"]}
        posts = []
        for row in await self.db.all("SELECT * FROM reddit_posts WHERE guild_id = ? ORDER BY id DESC LIMIT 40", (req.guild_id,)):
            feed = visible.get(row["feed_id"])
            if feed is not None and len(posts) < 15:
                posts.append({
                    "id": row["id"], "account": feed["account"], "title": row["title"], "link": row["link"], "state": row["state"], "note": row["note"], "source": row["source"],
                    "found_at": row["found_at"], "posted_at": row["posted_at"],
                    "url": f"https://discord.com/channels/{req.guild_id}/{feed['channel_id']}/{row['message_id']}" if row["message_id"] else None,
                })
        intake_channel = cfg["intake_channel"] if cfg["intake_channel"] and req.can_see(cfg["intake_channel"]) else None
        return {
            "feeds": feeds, "most": MAX_FEEDS, "posts": posts, "relay": self._relays.get(req.guild_id), "admin": req.is_admin,
            "intake": {"intake_channel": intake_channel, "intake_webhook": bool(cfg["intake_webhook"]), "delivered_at": self._delivered_at.get(req.guild_id), "reads_text": bool(getattr(self.bot, "reads_message_text", True))},
        }

    def _form_roles(self, req: Any, value: Any) -> list[int]:
        if value in (None, ""):
            return []
        if not isinstance(value, list) or len(value) > MAX_ROLES:
            raise ValidationError({"role_ids": f"Pick up to {MAX_ROLES} roles."})
        known = {role["id"] for role in (req.ops.describe_guild(req.guild_id) or {}).get("roles", [])}
        roles: list[int] = []
        for item in value:
            try:
                role_id = snowflake(item)
            except Invalid as error:
                raise ValidationError({"role_ids": "That isn't a role."}) from error
            if role_id not in known:
                raise ValidationError({"role_ids": "That role isn't in this server."})
            if role_id == req.guild_id:
                raise ValidationError({"role_ids": access.NOT_EVERYONE})
            if role_id not in roles:
                roles.append(role_id)
        return roles

    def _check_new_pings(self, req: Any, channel_id: int, roles: list[int], row: dict | None = None) -> None:
        """A ping saved now goes out later in the bot's name, so whoever saves it must be able to send it themselves. One a row already
        sends to that channel stays through an edit."""
        had = set(stored_roles(row["role_ids"])) if row is not None and row["channel_id"] == channel_id else set()
        guild = self.bot.get_guild(req.guild_id)
        if any(role not in had and delivery.needs_ping_right(guild, role) for role in roles):
            req.require_ping(channel_id, "role_ids")

    async def _api_add(self, req: Any) -> dict:
        form = await req.json()
        channel_id = delivery.form_channel(req, form.get("channel_id"))
        roles = self._form_roles(req, form.get("role_ids"))
        self._check_new_pings(req, channel_id, roles)
        try:
            account = reader.account_name(form.get("account"))
            return await self.follow(req.guild_id, account, channel_id, roles, req.actor)
        except Invalid as error:
            raise ValidationError({"account": str(error)}) from error

    async def _feed_row(self, req: Any) -> dict:
        row = await self.db.one("SELECT * FROM reddit_feeds WHERE guild_id = ? AND id = ?", (req.guild_id, req.int_param("feed_id")))
        if row is None:
            raise ApiError(404, "This server doesn't relay that account anymore.")
        if not req.can_see(row["channel_id"]):
            raise ApiError(403, "You can't see that channel.")
        return row

    async def _api_edit(self, req: Any) -> dict:
        row = await self._feed_row(req)
        form = await req.json()
        channel_id = delivery.form_channel(req, form.get("channel_id"), keeping=row["channel_id"])
        roles = self._form_roles(req, form.get("role_ids"))
        self._check_new_pings(req, channel_id, roles, row)
        await self.db.run("UPDATE reddit_feeds SET channel_id = ?, role_ids = ?, last_error = NULL WHERE id = ?", (channel_id, json.dumps(roles), row["id"]))
        await req.record("reddit.edit", detail=row["account"])
        return {"ok": True}

    async def _api_remove(self, req: Any) -> dict:
        await self.unfollow(await self._feed_row(req), req.actor)
        return {"ok": True}

    async def _api_post(self, req: Any) -> dict:
        """Relays one post in the background, since a long one takes a while, and the page shows how far it got."""
        form = await req.json()
        channel_id = delivery.form_channel(req, form.get("channel_id"))
        roles = self._form_roles(req, form.get("role_ids"))
        self._check_new_pings(req, channel_id, roles)
        try:
            link = reader.post_link(form.get("link"))
        except Invalid as error:
            raise ValidationError({"link": str(error)}) from error
        guild = self.bot.get_guild(req.guild_id)
        channel = await delivery.find_channel(guild, channel_id) if guild is not None else None
        if channel is None:
            raise ApiError(409, "The bot isn't connected to Discord here, so it can't post.")
        if (self._relays.get(req.guild_id) or {}).get("state") in ("reading", "sending"):
            raise ApiError(409, "A post is still on its way. Wait for it to finish.")
        self._relays[req.guild_id] = {"link": link, "state": "reading", "at": self._clock()}
        self._start(self._relay_for_page(guild, channel, link, roles))
        await req.record("reddit.post", detail=link[:400])
        return {"ok": True}

    async def _relay_for_page(self, guild: Any, channel: Any, link: str, roles: list[int]) -> None:
        status = self._relays[guild.id]
        try:
            post, outcome = await self.relay(guild, channel, link, roles, status.update)
        except FetchError as error:
            status.update(state="failed", error=str(error))
            return
        except Exception:
            log.exception("Relaying %s failed", link)
            status.update(state="failed", error="Something went wrong. It's been logged.")
            return
        status.update(title=post.title, state="done" if outcome.finished else "failed", done=outcome.sent, error=outcome.problem, at=self._clock())

    def _require_admin(self, req: Any) -> None:
        if not req.is_admin:
            raise ApiError(403, access.ONLY_ADMINS)

    async def _api_intake_set(self, req: Any) -> dict:
        self._require_admin(req)
        form = await req.json()
        channel_id = delivery.form_channel(req, form.get("intake_channel"), "intake_channel")
        guild = self.bot.get_guild(req.guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        if channel is None or channel.type not in POSTABLE:
            raise ValidationError({"intake_channel": "Pick a text channel the bot is connected to."})
        try:
            address = await self.set_intake(guild, channel, req.actor)
        except Invalid as error:
            raise ValidationError({"intake_channel": str(error)}) from error
        return {"address": address}

    async def _api_intake_address(self, req: Any) -> dict:
        self._require_admin(req)
        guild = self.bot.get_guild(req.guild_id)
        channel = guild.get_channel(self.cfg(req.guild_id)["intake_channel"] or 0) if guild is not None else None
        try:
            hook = await self._intake_hook(channel) if channel is not None else None
        except discord.HTTPException as error:
            raise ApiError(409, f"Discord didn't hand over the webhook: {error.text or error.status}") from error
        if hook is None:
            raise ApiError(404, "The courier's webhook is gone. Pick its channel again to make a new one.")
        return {"address": hook.url}

    async def _api_intake_drop(self, req: Any) -> dict:
        self._require_admin(req)
        guild = self.bot.get_guild(req.guild_id)
        if guild is None:
            raise ApiError(409, "The bot isn't connected to Discord here.")
        await self.drop_intake(guild, req.actor)
        return {"ok": True}

    async def seed_demo(self, guild_id: int) -> None:
        """Sample rows for the demo dashboard."""
        info = self.bot.ops.describe_guild(guild_id) or {}
        channels = {row["name"]: row["id"] for row in info.get("channels", [])}
        if "announcements" not in channels:
            return
        now = self._clock()
        feed_id = await self.db.insert(
            "INSERT INTO reddit_feeds (guild_id, account, channel_id, role_ids, posted_at, created_by, created_by_name, created_at) VALUES (?, ?, ?, '[]', ?, ?, ?, ?)",
            (guild_id, "ExampleMaker", channels["announcements"], now - 7200, guild_id + 2, "Demo Admin", now - 6 * 86400),
        )
        for index, (title, state, note) in enumerate((("Firmware 2.4 is out", "sent", None), ("Meet us at the maker fair", "sent", None), ("Meet us at the maker fair", "skipped", SAME_TITLE))):
            await self.db.run(
                "INSERT INTO reddit_posts (guild_id, feed_id, post_key, title, link, state, note, source, found_at, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'feed', ?, ?)",
                (guild_id, feed_id, f"t3_demo{index}", title, f"{reader.SITE}/r/example/comments/demo{index}/", state, note, now - 7200 * (3 - index), now - 7200 * (3 - index) + 40 if state == "sent" else None),
            )
        self._reads["examplemaker"] = {"failures": 0, "checked_at": now - 20, "ok_at": now - 20, "error": None}
        await self.bot.settings.save(guild_id, self.key, {"enabled": True})
