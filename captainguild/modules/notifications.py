"""Twitch, YouTube and game alerts.

Each service is polled on a repeating timer. A round runs as its own task, because the timer loop runs handlers one at a time and an
unban shouldn't wait on a slow website. A failed service logs, backs off and tries again.

Each server's delivery stands alone. A post Discord turned down goes out on a later round to that server only, and what landed never goes
out again. A YouTube video is written down as owed to each following server before anything is sent. A Twitch stream and a game alert are
marked done per server, right after each post. Only a message Discord refuses for what it holds is dropped, since sending it again can't help.

YouTube's RSS server has gone down for hours at a stretch, answering 404 for every channel, so a 404 from it never means a channel is
gone, and the channel's public videos page stands in while the feed is down. CheapShark refuses a generic User-Agent, which core/outside.py
takes care of, and its terms ask that deal links go through its redirect page. GamerPower's terms ask for a link back on what we post.
"""
from __future__ import annotations

import asyncio
import calendar
import datetime
import html
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import parse_qs, quote, urlsplit

import discord
import feedparser
from discord import app_commands
from discord.ext import commands

from ..core import access, delivery, messages
from ..core.module import Module, requires
from ..core.outside import Outside, OutsideError
from ..core.tagscript import OverBudget, Scope, budget, budgeted_render, render_text
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, choice, integer, list_of, number, optional, snowflake, text
from ..web.api import ApiError

log = logging.getLogger(__name__)

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API = "https://api.twitch.tv/helix"
TWITCH_BATCH = 100  # Get Streams takes at most this many user IDs per call
TWITCH_SECONDS = 120
TWITCH_REARM_SECONDS = 600  # a stream that drops and comes back within this time isn't a new stream to members
YOUTUBE_SITE = "https://www.youtube.com"
YOUTUBE_FEED = YOUTUBE_SITE + "/feeds/videos.xml"
YOUTUBE_COOKIE = {"Cookie": "SOCS=CAI; CONSENT=YES+"}  # without this, YouTube sends European visitors to a consent page
YOUTUBE_SECONDS = 600
YOUTUBE_GAP = 1.0
YOUTUBE_SEEN_KEPT = 200
YOUTUBE_MAX_AGE = 7 * 86400
YOUTUBE_PER_ROUND = 3
YOUTUBE_PENDING_KEPT = 10
YOUTUBE_PAGE_BYTES = 6 * 1024 * 1024
GAMERPOWER_URL = "https://www.gamerpower.com/api/giveaways"
GAMERPOWER_HOME = "https://www.gamerpower.com"
CHEAPSHARK_URL = "https://www.cheapshark.com/api/1.0/deals"
CHEAPSHARK_DEAL = "https://www.cheapshark.com/redirect?dealID={}"
FREE_SECONDS = 1800
DEALS_SECONDS = 3600  # CheapShark updates prices about hourly and asks that automated calls stay few
ALERTS_PER_ROUND = 5
LONGEST_BACKOFF = 6 * 3600
MAX_SUBSCRIPTIONS = 200
DEFAULT_TWITCH_FORMAT = "{name} is live: {link}"
DEFAULT_YOUTUBE_FORMAT = "New video from {author}: {link}"
FREE_COLOR = 0x4DBD74
DEAL_COLOR = 0x00A2F8

CHANNEL_ID = re.compile(r"UC[0-9A-Za-z_-]{22}")
VIDEO_ID = re.compile(r"[0-9A-Za-z_-]{11}")
TWITCH_LOGIN = re.compile(r"[A-Za-z0-9_]{1,25}")
ROLE_MENTION = re.compile(r"<@&(\d{15,21})>")
ROLE_AT_END = re.compile(r"\s*<@&(\d{15,21})>\s*$")
USER_MENTION = re.compile(r"<@!?(\d{15,21})>")
CHANNEL_GONE = "The channel for this alert no longer exists."
SERVER_AWAY = "This server is away right now, so the alert waits for it."
GAME_CHANNEL_GONE = "The alert channel no longer exists."
NOT_PINGED = "Posted, but the ping didn't go out. The bot needs Mention Everyone in that channel, or Manage Roles and a place above the role, and the role has to exist."

# What GamerPower's API takes, and how the same platform reads in its giveaways.
FREE_PLATFORMS = {
    "steam": "Steam", "epic-games-store": "Epic Games Store", "gog": "GOG", "pc": "PC", "drm-free": "DRM-Free", "itchio": "Itch.io",
    "android": "Android", "ios": "iOS", "ps4": "Playstation 4", "ps5": "Playstation 5", "xbox-one": "Xbox One", "xbox-series-xs": "Xbox Series X|S", "switch": "Nintendo Switch",
}
# CheapShark's stores that are active. Its list barely changes, so this saves a call.
DEAL_STORES = {
    "1": "Steam", "2": "GamersGate", "3": "GreenManGaming", "7": "GOG", "11": "Humble Store", "13": "Uplay", "15": "Fanatical", "21": "WinGameStore",
    "23": "GameBillet", "25": "Epic Games Store", "27": "Gamesplanet", "28": "Gamesload", "30": "IndieGala", "35": "DreamGame",
}
SAMPLE_GIVEAWAY = {
    "id": 0, "title": "Sample Game (Steam) Giveaway", "worth": "$19.99", "image": "", "open_giveaway_url": GAMERPOWER_HOME, "platforms": "PC, Steam",
    "end_date": "N/A", "status": "Active", "type": "Game",
}
SAMPLE_DEAL = {"title": "Sample Game", "dealID": "sample", "storeID": "1", "gameID": "0", "salePrice": "4.99", "normalPrice": "19.99", "savings": "75.03", "dealRating": "9.0", "thumb": ""}


class ServiceError(Exception):
    """An outside service refused or failed. The text is for the person reading the page or running the command."""


def _plain(value: Any, limit: int = 300) -> str:
    """Text from outside, such as a stream title, on one line with its markdown escaped, so it can't show as a link or a heading in the bot's post. It can't run as a template, because values filled in are never read as tags."""
    return discord.utils.escape_markdown(" ".join(str(value or "").split())[:limit])


def _roles_named(fmt: str, role_id: int | None) -> set[int]:
    roles = {int(found) for found in ROLE_MENTION.findall(fmt)}
    if role_id:
        roles.add(role_id)
    return roles


def alert_mentions(fmt: str, role_id: int | None) -> discord.AllowedMentions:
    """Only what the server itself wrote can ping. A stream title holding a mention or @everyone stays plain text."""
    users = sorted({int(found) for found in USER_MENTION.findall(fmt)})
    return discord.AllowedMentions(
        everyone="{everyone}" in fmt, roles=[discord.Object(role) for role in sorted(_roles_named(fmt, role_id))], users=[discord.Object(user) for user in users], replied_user=False,
    )


def adds_ping(guild: Any, channel_id: int, fmt: str, role_id: int | None, row: dict | None = None) -> str | None:
    """The field that saves a new ping needing Mention Everyone, or None. A ping a row already sends to that channel stays through an edit, so someone can fix a typo in it."""
    same_place = row is not None and row["channel_id"] == channel_id
    if "{everyone}" in fmt and not (same_place and "{everyone}" in row["fmt"]):
        return "fmt"
    had = _roles_named(row["fmt"], row["role_id"]) if same_place else set()
    for role in sorted(_roles_named(fmt, None) - had):
        if delivery.needs_ping_right(guild, role):
            return "fmt"
    if role_id and role_id not in had and delivery.needs_ping_right(guild, role_id):
        return "role_id"
    return None


def alert_text(fmt: str, role_id: int | None, *, guild: Any = None, channel: Any = None, values: dict[str, str]) -> str:
    extra = {**values, "everyone": "@everyone", "role": f"<@&{role_id}>" if role_id else ""}
    rendered = render_text(fmt, Scope(guild=guild, channel=channel, extra=extra))
    if role_id and "{role}" not in fmt:
        rendered = f"<@&{role_id}> {rendered}"
    return rendered[:messages.CONTENT_MAX]


async def filled_alert_text(fmt: str, role_id: int | None, *, guild_id: int, guild: Any = None, channel: Any = None, values: dict[str, str]) -> str:
    """alert_text away from the event loop, since a format is a template and can take a quarter second. Past the server's budget it goes out as written."""
    try:
        return await budgeted_render(lambda: alert_text(fmt, role_id, guild=guild, channel=channel, values=values), guild_id, pool=budget.MESSAGES)
    except OverBudget:
        return (f"<@&{role_id}> {fmt}" if role_id and "{role}" not in fmt else fmt)[:messages.CONTENT_MAX]


@dataclass
class Video:
    id: str
    title: str
    link: str
    published: float | None = None


@dataclass
class Listing:
    name: str | None
    videos: list[Video]


def read_feed(body: bytes) -> Listing | None:
    """A channel's Atom feed, newest first. None when the body isn't a feed. Passed as a stream, because feedparser tries bytes as a file name first."""
    feed = feedparser.parse(io.BytesIO(body))
    if feed.get("bozo") and not feed.get("entries"):
        return None
    if not feed.get("feed", {}).get("title") and not feed.get("entries"):
        return None
    videos = []
    for entry in feed.get("entries", []):
        video_id = entry.get("yt_videoid") or str(entry.get("id", "")).rpartition(":")[2]
        if not VIDEO_ID.fullmatch(video_id or ""):
            continue
        parsed = entry.get("published_parsed")
        videos.append(Video(video_id, entry.get("title") or "", entry.get("link") or f"{YOUTUBE_SITE}/watch?v={video_id}", calendar.timegm(parsed) if parsed else None))
    return Listing(feed.get("feed", {}).get("title") or None, videos)


def _walk(node: Any, wanted: set[str]) -> Any:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in wanted:
                yield key, value
            else:
                yield from _walk(value, wanted)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, wanted)


def read_videos_page(page: str) -> Listing | None:
    """A channel's /videos page, newest first, from the data it embeds for its own scripts. None if that data isn't there. Shorts live on another tab."""
    match = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", page, re.S)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    name, videos, listed = None, [], set()
    for key, value in _walk(data, {"channelMetadataRenderer", "lockupViewModel", "videoRenderer"}):
        if key == "channelMetadataRenderer":
            name = name or value.get("title")
            continue
        if key == "lockupViewModel":
            if value.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
                continue
            video_id = value.get("contentId")
            title = ((value.get("metadata") or {}).get("lockupMetadataViewModel") or {}).get("title", {}).get("content")
        else:
            video_id = value.get("videoId")
            title = "".join(run.get("text", "") for run in (value.get("title") or {}).get("runs", []))
        if video_id and VIDEO_ID.fullmatch(video_id) and video_id not in listed:
            listed.add(video_id)
            videos.append(Video(video_id, title or "", f"{YOUTUBE_SITE}/watch?v={video_id}"))
    if not videos and name is None:
        return None
    return Listing(name, videos)


def read_channel_page(page: str) -> tuple[str, str] | None:
    """The channel ID and name a channel page is about."""
    found = re.search(r'"externalId":"(UC[0-9A-Za-z_-]{22})"', page) or re.search(r'<meta itemprop="identifier" content="(UC[0-9A-Za-z_-]{22})"', page) \
        or re.search(r'rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"', page)
    if found is None:
        return None
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    return found.group(1), html.unescape(title.group(1)) if title else found.group(1)


def read_search_page(page: str) -> tuple[str, str] | None:
    """The first channel on a channel search page."""
    found = re.search(r'"channelRenderer":\{"channelId":"(UC[0-9A-Za-z_-]{22})","title":\{"simpleText":"((?:[^"\\]|\\.)*)"', page)
    if found is None:
        return None
    try:
        return found.group(1), json.loads(f'"{found.group(2)}"')
    except ValueError:
        return found.group(1), found.group(1)


def youtube_target(value: str) -> tuple[str, str]:
    """What someone typed, as ("channel", id), ("page", path), ("video", id) or ("search", words). Only the path of a link is kept, so the bot only ever calls YouTube."""
    value = value.strip()
    if CHANNEL_ID.fullmatch(value):
        return "channel", value
    if value.startswith("@"):
        return "page", "/" + quote(value, safe="@")
    if "youtube.com" not in value.lower() and "youtu.be" not in value.lower():
        return "search", value
    parts = urlsplit(value if "://" in value else "https://" + value)
    host = (parts.hostname or "").lower()
    if host != "youtu.be" and not (host == "youtube.com" or host.endswith(".youtube.com")):
        raise ServiceError("That isn't a YouTube link.")
    pieces = [piece for piece in parts.path.split("/") if piece]
    if host == "youtu.be" and pieces and VIDEO_ID.fullmatch(pieces[0]):
        return "video", pieces[0]
    if pieces[:1] == ["watch"]:
        video = (parse_qs(parts.query).get("v") or [""])[0]
        if VIDEO_ID.fullmatch(video):
            return "video", video
        raise ServiceError("That video link has no video in it.")
    if len(pieces) >= 2 and pieces[0] in ("shorts", "live", "embed") and VIDEO_ID.fullmatch(pieces[1]):
        return "video", pieces[1]
    if len(pieces) >= 2 and pieces[0] == "channel" and CHANNEL_ID.fullmatch(pieces[1]):
        return "channel", pieces[1]
    if pieces and pieces[0].startswith("@"):
        return "page", "/" + quote(pieces[0], safe="@")
    if len(pieces) >= 2 and pieces[0] in ("c", "user"):
        return "page", f"/{pieces[0]}/{quote(pieces[1])}"
    if len(pieces) == 1:
        return "page", "/" + quote(pieces[0])
    raise ServiceError("That link doesn't lead to a channel or a video.")


def new_videos(listing: Listing, seen: list[str] | None, now: float, since: float = 0.0) -> list[Video]:
    """What to announce, oldest first. A video is new only when it sits above something already seen, so an older one that was never in our list stays quiet.

    The feed and the channel page list different things: the page has more videos and no Shorts. So a video with a date also has to be recent and
    published since the channel was first followed, and a page that shares nothing with what we know proves nothing.
    """
    if seen is None:
        return []  # the first look at a channel only records what's there
    known = set(seen)
    last_known = max((index for index, video in enumerate(listing.videos) if video.id in known), default=None)
    if last_known is None:
        fresh = [video for video in listing.videos[:1] if video.published is not None]
    else:
        fresh = [video for video in listing.videos[:last_known] if video.id not in known]
    fresh = [video for video in fresh if video.published is None or (video.published >= since and now - video.published <= YOUTUBE_MAX_AGE)]
    return list(reversed(fresh[:YOUTUBE_PER_ROUND]))


def free_game_payload(item: dict, cfg: dict, *, guild: Any = None, channel: Any = None) -> dict:
    title = str(item.get("title") or "Free game")[:messages.TITLE_MAX]
    link = item.get("open_giveaway_url") or item.get("gamerpower_url") or GAMERPOWER_HOME
    embed: dict[str, Any] = {"title": title, "url": link, "color": FREE_COLOR, "fields": [], "footer": {"text": str(item.get("platforms") or "")[:200] or "Free game"}}
    worth = item.get("worth")
    if worth and worth != "N/A":
        embed["fields"].append({"name": "Price", "value": f"~~{worth}~~ Free", "inline": True})
    ends = _giveaway_end(item.get("end_date"))
    if ends:
        embed["fields"].append({"name": "Free until", "value": f"<t:{ends}:F>", "inline": True})
    if item.get("image"):
        embed["image"] = {"url": item["image"]}
    values = {"title": _plain(title), "platforms": _plain(item.get("platforms")), "worth": _plain(worth), "link": link, "until": f"<t:{ends}:F>" if ends else "while it lasts"}
    content = alert_text(cfg["free_format"], cfg["alert_role"], guild=guild, channel=channel, values=values) if cfg["free_format"] or cfg["alert_role"] else ""
    return {"content": content, "embeds": [embed], "buttons": [{"label": "Claim", "url": link}, {"label": "Data from GamerPower", "url": GAMERPOWER_HOME}]}


def _giveaway_end(value: Any) -> int | None:
    try:
        moment = datetime.datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return int(moment.timestamp())


def deal_payload(item: dict, cfg: dict, *, guild: Any = None, channel: Any = None) -> dict:
    link = CHEAPSHARK_DEAL.format(item.get("dealID", ""))
    title = str(item.get("title") or "Game deal")[:messages.TITLE_MAX]
    store = DEAL_STORES.get(str(item.get("storeID")), f"Store {item.get('storeID')}")
    savings = _as_float(item.get("savings"))
    embed: dict[str, Any] = {
        "title": title, "url": link, "color": DEAL_COLOR,
        "fields": [
            {"name": "Price", "value": f"~~${item.get('normalPrice')}~~ ${item.get('salePrice')}", "inline": True},
            {"name": "Discount", "value": f"{savings:.0f}% off", "inline": True},
            {"name": "Store", "value": store, "inline": True},
        ],
        "footer": {"text": "Deals from CheapShark. Prices in US dollars."},
    }
    if item.get("thumb"):
        embed["thumbnail"] = {"url": item["thumb"]}
    values = {"title": _plain(title), "store": store, "price": f"${item.get('salePrice')}", "discount": f"{savings:.0f}%", "link": link}
    content = alert_text(cfg["deal_format"], cfg["alert_role"], guild=guild, channel=channel, values=values) if cfg["deal_format"] or cfg["alert_role"] else ""
    return {"content": content, "embeds": [embed], "buttons": [{"label": "View deal", "url": link}]}


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _wanted_free(item: dict, cfg: dict) -> bool:
    if str(item.get("status", "Active")).lower() != "active":
        return False
    chosen = cfg["free_platforms"]
    if not chosen:
        return True
    listed = {part.strip().lower() for part in str(item.get("platforms", "")).split(",")}
    return any(FREE_PLATFORMS[name].lower() in listed for name in chosen if name in FREE_PLATFORMS)


def _wanted_deal(item: dict, cfg: dict) -> bool:
    if _as_float(item.get("savings")) < cfg["discount_threshold"] or _as_float(item.get("dealRating")) < cfg["deal_min_rating"]:
        return False
    return not cfg["deal_stores"] or str(item.get("storeID")) in cfg["deal_stores"]


class Notifications(Module):
    key = "notifications"
    row_limits = {"youtube_subs": MAX_SUBSCRIPTIONS, "twitch_subs": MAX_SUBSCRIPTIONS}
    category = "Notifications"
    schema = Schema({
        "twitch_client_id": Field("", text(100), secret=True),
        "twitch_client_secret": Field("", text(200), secret=True),
        "alert_channel": Field(None, optional(snowflake)),
        "twitch_channel": Field(None, optional(snowflake)),
        "alert_role": Field(None, optional(snowflake)),
        "alerts_enabled": Field(True, boolean),
        "deals_enabled": Field(False, boolean),
        "free_platforms": Field([], list_of(choice(*FREE_PLATFORMS), len(FREE_PLATFORMS))),
        "free_format": Field("", text(1000)),
        "discount_threshold": Field(50, integer(10, 95)),
        "deal_min_rating": Field(8.0, number(0, 10)),
        "deal_stores": Field([], list_of(choice(*DEAL_STORES), len(DEAL_STORES))),
        "deal_format": Field("", text(1000)),
    })
    migrations = [
        """
        CREATE TABLE twitch_subs (
            guild_id INTEGER NOT NULL,
            stream_id TEXT NOT NULL,
            stream_login TEXT NOT NULL,
            stream_name TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            fmt TEXT NOT NULL,
            role_id INTEGER,
            added_by INTEGER,
            added_at REAL NOT NULL,
            live_id TEXT,
            live_since REAL,
            offline_since REAL,
            last_alert REAL,
            last_error TEXT,
            PRIMARY KEY (guild_id, stream_id)
        );
        CREATE TABLE youtube_subs (
            guild_id INTEGER NOT NULL,
            youtube_id TEXT NOT NULL,
            youtube_name TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            fmt TEXT NOT NULL,
            role_id INTEGER,
            shorts INTEGER NOT NULL DEFAULT 1,
            added_by INTEGER,
            added_at REAL NOT NULL,
            last_video TEXT,
            last_alert REAL,
            last_error TEXT,
            PRIMARY KEY (guild_id, youtube_id)
        );
        CREATE INDEX youtube_subs_channel ON youtube_subs (youtube_id);
        CREATE TABLE youtube_state (
            youtube_id TEXT PRIMARY KEY,
            seen TEXT,
            first_at REAL NOT NULL,
            checked_at REAL,
            ok_at REAL,
            failures INTEGER NOT NULL DEFAULT 0,
            next_check REAL NOT NULL DEFAULT 0,
            last_error TEXT
        );
        CREATE TABLE game_alert_seen (
            guild_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            item TEXT NOT NULL,
            at REAL NOT NULL,
            PRIMARY KEY (guild_id, source, item)
        );
        CREATE TABLE game_alert_state (
            guild_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            filter TEXT NOT NULL,
            PRIMARY KEY (guild_id, source)
        );
        """,
        # When a followed Twitch stream was last seen live, and the YouTube videos still owed to one server.
        """
        ALTER TABLE twitch_subs ADD COLUMN live_seen_at REAL;
        ALTER TABLE youtube_subs ADD COLUMN pending TEXT;
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.outside = Outside()
        self._sleep = asyncio.sleep
        self._clock = time.time
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}
        self._twitch_wait: dict[str, float] = {}
        self._status: dict[str, dict] = {}
        self._rounds: dict[str, asyncio.Future] = {}
        self._backoff: dict[str, tuple[float, int]] = {}

    def power_roles(self, settings: dict) -> dict[str, set[int]]:
        return {"alert_role": {settings["alert_role"]} if settings["alert_role"] else set()}

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {name: {settings[name]} if settings[name] else set() for name in ("alert_channel", "twitch_channel")}

    def pinged_channels(self, settings: dict) -> dict[str, set[int]]:
        channel = settings["alert_channel"]
        found = {name: {channel} if channel and "{everyone}" in settings[name] else set() for name in ("free_format", "deal_format")}
        if channel:
            # The field carries the role's ID, because swapping one role members can't ping for another is a new ping with the channel unchanged.
            guild = getattr(self.bot.get_channel(channel), "guild", None)
            for field, role_id in [("alert_role", settings["alert_role"]), *((name, int(named)) for name in ("free_format", "deal_format") for named in ROLE_MENTION.findall(settings[name]))]:
                if role_id and delivery.needs_ping_right(guild, role_id):
                    found[f"{field}:{role_id}"] = {channel}
        return found

    async def on_load(self) -> None:
        timers = self.bot.timers
        for kind, every, handler in (
            ("notify.twitch", TWITCH_SECONDS, self._twitch_due),
            ("notify.youtube", YOUTUBE_SECONDS, self._youtube_due),
            ("notify.free", FREE_SECONDS, self._free_due),
            ("notify.deals", DEALS_SECONDS, self._deals_due),
        ):
            timers.handler(kind, handler)
            if await timers.get_key(kind, kind) is None:
                await timers.schedule(kind, self._clock() + every, key=kind)
        self._attach_games_alerts()

    async def cog_unload(self) -> None:
        for task in self._rounds.values():
            task.cancel()
        await self.outside.close()

    # ---- polling rounds ----

    def _start_round(self, name: str, work: Callable[[], Awaitable[None]]) -> None:
        running = self._rounds.get(name)
        if running is not None and not running.done():
            return

        async def guarded() -> None:
            try:
                await work()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("The %s round failed. The next one runs on schedule.", name)

        self._rounds[name] = asyncio.ensure_future(guarded())

    async def _twitch_due(self, payload: dict, row: dict) -> None:
        await self.bot.timers.schedule("notify.twitch", self._clock() + TWITCH_SECONDS, key="notify.twitch")
        self._start_round("twitch", self.poll_twitch)

    async def _youtube_due(self, payload: dict, row: dict) -> None:
        await self.bot.timers.schedule("notify.youtube", self._clock() + YOUTUBE_SECONDS, key="notify.youtube")
        self._start_round("youtube", self.poll_youtube)

    async def _free_due(self, payload: dict, row: dict) -> None:
        await self.bot.timers.schedule("notify.free", self._clock() + FREE_SECONDS, key="notify.free")
        self._start_round("free", self.poll_free_games)

    async def _deals_due(self, payload: dict, row: dict) -> None:
        await self.bot.timers.schedule("notify.deals", self._clock() + DEALS_SECONDS, key="notify.deals")
        self._start_round("deals", self.poll_deals)

    def _note(self, key: str, error: str | None) -> None:
        """What the page shows about the last try at a service."""
        now = self._clock()
        status = self._status.setdefault(key, {})
        status["checked_at"] = now
        status["error"] = error
        if error is None:
            status["ok_at"] = now

    # ---- Twitch ----

    def _has_twitch(self, cfg: dict) -> bool:
        return bool(cfg["twitch_client_id"] and cfg["twitch_client_secret"])

    async def _twitch_token(self, cfg: dict) -> str:
        if not self._has_twitch(cfg):
            raise ServiceError("Enter your Twitch client ID and secret on the Twitch page first.")
        key = (cfg["twitch_client_id"], cfg["twitch_client_secret"])
        held = self._tokens.get(key)
        if held and held[1] > self._clock() + 60:
            return held[0]
        reply = await self.outside.post(TWITCH_TOKEN_URL, data={"client_id": key[0], "client_secret": key[1], "grant_type": "client_credentials"})
        if reply.status in (400, 401, 403):
            raise ServiceError("Twitch refused the client ID or secret. Check them on the Twitch page.")
        if reply.status != 200:
            raise ServiceError(f"Twitch's sign-in answered with error {reply.status}.")
        data = reply.json()
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise ServiceError("Twitch's sign-in sent no token.")
        self._tokens[key] = (token, self._clock() + (_as_float(data.get("expires_in")) or 3600.0))
        return token

    async def _twitch_get(self, cfg: dict, path: str, params: list[tuple[str, str]]) -> dict:
        client_id = cfg["twitch_client_id"]
        if self._twitch_wait.get(client_id, 0) > self._clock():
            raise ServiceError("Twitch asked the bot to slow down. It tries again shortly.")
        reply = None
        for _ in range(2):
            token = await self._twitch_token(cfg)
            reply = await self.outside.get(f"{TWITCH_API}/{path}", params=params, headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"})
            if reply.status != 401:
                break
            self._tokens.pop((client_id, cfg["twitch_client_secret"]), None)  # the token expired early, so one fresh try
        if reply.status == 429:
            try:
                self._twitch_wait[client_id] = float(reply.header("Ratelimit-Reset") or 0) or self._clock() + 60
            except ValueError:
                self._twitch_wait[client_id] = self._clock() + 60
            raise ServiceError("Twitch asked the bot to slow down. It tries again shortly.")
        if reply.status != 200:
            raise ServiceError(f"Twitch answered with error {reply.status}.")
        data = reply.json()
        return data if isinstance(data, dict) else {}

    async def _twitch_live(self, cfg: dict, stream_ids: list[str]) -> dict[str, dict]:
        live: dict[str, dict] = {}
        for start in range(0, len(stream_ids), TWITCH_BATCH):
            params = [("type", "live"), ("first", str(TWITCH_BATCH)), *(("user_id", stream_id) for stream_id in stream_ids[start:start + TWITCH_BATCH])]
            for stream in (await self._twitch_get(cfg, "streams", params)).get("data") or []:
                if isinstance(stream, dict) and stream.get("user_id"):
                    live[str(stream["user_id"])] = stream
        return live

    async def poll_twitch(self) -> None:
        rows = await self.db.all("SELECT * FROM twitch_subs ORDER BY guild_id")
        by_guild: dict[int, list[dict]] = {}
        for row in rows:
            by_guild.setdefault(row["guild_id"], []).append(row)
        for guild_id, subs in by_guild.items():
            guild = self.bot.get_guild(guild_id)
            cfg = self.cfg(guild_id)
            if guild is None or not self._has_twitch(cfg):
                continue
            key = f"twitch:{guild_id}"
            try:
                live = await self._twitch_live(cfg, [row["stream_id"] for row in subs])
            except (ServiceError, OutsideError) as error:
                log.info("Twitch check failed for %s: %s", guild_id, error)
                self._note(key, str(error))
                continue
            except Exception:
                log.exception("Twitch check failed for %s", guild_id)
                self._note(key, "Twitch sent something the bot couldn't read.")
                continue
            self._note(key, None)
            for row in subs:
                try:
                    await self._twitch_update(guild, row, live.get(row["stream_id"]))
                except Exception:
                    log.exception("Twitch alert failed for %s in %s", row["stream_name"], guild_id)

    async def _twitch_update(self, guild: Any, row: dict, stream: dict | None) -> None:
        now = self._clock()
        where = (row["guild_id"], row["stream_id"])
        if stream is None:
            if row["live_id"]:
                await self.db.run("UPDATE twitch_subs SET live_id = NULL, live_since = NULL, offline_since = ? WHERE guild_id = ? AND stream_id = ?", (now, *where))
            return
        stream_key = str(stream.get("id") or stream.get("started_at") or "live")
        if row["live_id"] == stream_key:
            await self.db.run("UPDATE twitch_subs SET live_seen_at = ? WHERE guild_id = ? AND stream_id = ?", (now, *where))
            return
        # A stream that drops and comes back between two polls has a new ID and was never seen offline, so the last sighting decides.
        seen_recently = row["live_id"] and row["live_seen_at"] and now - row["live_seen_at"] < TWITCH_REARM_SECONDS
        if seen_recently or (row["offline_since"] and now - row["offline_since"] < TWITCH_REARM_SECONDS):
            await self.db.run("UPDATE twitch_subs SET live_id = ?, live_since = ?, live_seen_at = ?, offline_since = NULL WHERE guild_id = ? AND stream_id = ?", (stream_key, now, now, *where))
            return
        channel = await delivery.find_channel(guild, row["channel_id"])
        if channel is None or not delivery.holds_messages(channel):
            await self.db.run("UPDATE twitch_subs SET last_error = ? WHERE guild_id = ? AND stream_id = ?", (CHANNEL_GONE if channel is None else delivery.NOT_POSTABLE, *where))
            return
        login = stream.get("user_login") or row["stream_login"]
        name = stream.get("user_name") or row["stream_name"]
        values = {"name": _plain(name), "link": f"https://twitch.tv/{login}", "game": _plain(stream.get("game_name")) or "something", "title": _plain(stream.get("title"))}
        content = await filled_alert_text(row["fmt"], row["role_id"], guild_id=row["guild_id"], guild=guild, channel=channel, values=values)
        try:
            pinged = await self._post_alert(guild, channel, {"content": content, "allowed_mentions": alert_mentions(row["fmt"], row["role_id"])})
        except Exception as error:
            log.info("Could not post a Twitch alert in %s: %r", guild.id, error)
            await self.db.run("UPDATE twitch_subs SET last_error = ? WHERE guild_id = ? AND stream_id = ?", (self._send_problem(error), *where))
            if delivery.refused_for_good(error):  # this stream's alert can't go out, and the next stream gets a fresh try
                await self.db.run("UPDATE twitch_subs SET live_id = ?, live_since = ?, live_seen_at = ?, offline_since = NULL WHERE guild_id = ? AND stream_id = ?", (stream_key, now, now, *where))
            return
        await self.db.run(
            "UPDATE twitch_subs SET live_id = ?, live_since = ?, live_seen_at = ?, last_alert = ?, offline_since = NULL, last_error = ?, stream_name = ?, stream_login = ? WHERE guild_id = ? AND stream_id = ?",
            (stream_key, now, now, now, None if pinged else NOT_PINGED, name, login, *where),
        )

    # ---- posting an alert ----

    async def _post_alert(self, guild: Any, channel: Any, kwargs: dict) -> bool:
        """Send with its roles ready to ping. Returns whether every ping could go out."""
        allowed = kwargs["allowed_mentions"]
        async with delivery.role_pings(self.db, guild, channel, [role.id for role in allowed.roles]) as ready:
            await channel.send(kwargs.pop("content", None), **kwargs)
        return ready and (not allowed.everyone or channel.permissions_for(guild.me).mention_everyone)

    @staticmethod
    def _send_problem(error: Exception) -> str:
        if isinstance(error, discord.HTTPException):
            return f"Discord refused the alert: {error.text or error.status}"
        return "The alert couldn't be sent. The bot tries again."

    async def _twitch_lookup(self, cfg: dict, name: str) -> dict:
        cleaned = name.strip()
        match = re.search(r"twitch\.tv/([A-Za-z0-9_]{1,25})", cleaned, re.IGNORECASE)
        login = (match.group(1) if match else cleaned).lower()
        if not TWITCH_LOGIN.fullmatch(login):
            raise ServiceError("That isn't a Twitch name. Names hold letters, numbers and underscores.")
        users = (await self._twitch_get(cfg, "users", [("login", login)])).get("data") or []
        if not users or not isinstance(users[0], dict) or not users[0].get("id"):
            raise ServiceError(f"Twitch has no channel called {login}.")
        return users[0]

    async def _twitch_store(self, guild_id: int, user: dict, channel_id: int, fmt: str, role_id: int | None, actor_id: int | None) -> None:
        count = await self.db.scalar("SELECT COUNT(*) FROM twitch_subs WHERE guild_id = ?", (guild_id,))
        exists = await self.db.scalar("SELECT 1 FROM twitch_subs WHERE guild_id = ? AND stream_id = ?", (guild_id, str(user["id"])))
        if count >= MAX_SUBSCRIPTIONS and not exists:
            raise ServiceError(f"A server can follow at most {MAX_SUBSCRIPTIONS} Twitch channels.")
        await self.db.run(
            "INSERT INTO twitch_subs (guild_id, stream_id, stream_login, stream_name, channel_id, fmt, role_id, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, stream_id) DO UPDATE SET stream_login = excluded.stream_login, stream_name = excluded.stream_name, channel_id = excluded.channel_id, "
            "fmt = excluded.fmt, role_id = excluded.role_id, last_error = NULL",
            (guild_id, str(user["id"]), user.get("login") or user.get("broadcaster_login") or "", user.get("display_name") or user.get("login") or "", channel_id, fmt, role_id, actor_id, self._clock()),
        )

    async def _find_twitch(self, guild_id: int, name: str) -> dict | None:
        cleaned = name.strip().lower()
        return await self.db.one("SELECT * FROM twitch_subs WHERE guild_id = ? AND (lower(stream_login) = ? OR lower(stream_name) = ? OR stream_id = ?)", (guild_id, cleaned, cleaned, cleaned))

    async def _twitch_name_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        rows = await self.db.all("SELECT stream_name, stream_login FROM twitch_subs WHERE guild_id = ? AND lower(stream_name) LIKE ? ORDER BY stream_name LIMIT 25", (interaction.guild_id, f"{current.lower()}%"))
        return [app_commands.Choice(name=row["stream_name"][:100], value=row["stream_login"]) for row in rows]

    # ---- YouTube ----

    async def _youtube_page(self, path: str) -> str | None:
        reply = await self.outside.get(YOUTUBE_SITE + path, headers=YOUTUBE_COOKIE, max_bytes=YOUTUBE_PAGE_BYTES)
        if reply.status == 404:
            return None
        if reply.status != 200:
            raise ServiceError(f"YouTube answered with error {reply.status}. Try again in a while.")
        return reply.text()

    async def _youtube_resolve(self, value: str) -> tuple[str, str]:
        """Find the channel someone means, as its ID and name. The feed isn't used for this, because it can be down while channels are fine."""
        kind, target = youtube_target(value)
        if kind == "video":
            reply = await self.outside.get(f"{YOUTUBE_SITE}/oembed", params={"url": f"{YOUTUBE_SITE}/watch?v={target}", "format": "json"})
            if reply.status != 200:
                raise ServiceError("YouTube can't find that video, or it's private.")
            data = reply.json()
            owner = urlsplit(str(data.get("author_url") or "") if isinstance(data, dict) else "")
            if not owner.path:
                raise ServiceError("YouTube didn't say whose video that is.")
            kind, target = "page", owner.path
        if kind == "channel":
            kind, target = "page", f"/channel/{target}"
        if kind == "search":
            page = await self._youtube_page(f"/results?search_query={quote(target)}&sp=EgIQAg%253D%253D")
            found = read_search_page(page) if page else None
            if found is None:
                raise ServiceError(f"YouTube found no channel called {target}. A channel or video link is the surest way.")
            return found
        page = await self._youtube_page(target)
        found = read_channel_page(page) if page else None
        if found is None:
            raise ServiceError("YouTube has no channel there.")
        return found

    async def _youtube_listing(self, youtube_id: str) -> Listing:
        """The newest videos, from the feed or, while the feed is down, the channel's page."""
        problem = "YouTube's feed is unavailable."
        try:
            reply = await self.outside.get(YOUTUBE_FEED, params={"channel_id": youtube_id})
            if reply.status == 200:
                listing = await asyncio.to_thread(read_feed, reply.body)
                if listing is not None:
                    return listing
                problem = "YouTube's feed sent something unreadable."
            else:
                problem = f"YouTube's feed answered with error {reply.status}."
        except OutsideError as error:
            problem = str(error)
        page = await self._youtube_page(f"/channel/{youtube_id}/videos")
        listing = await asyncio.to_thread(read_videos_page, page) if page else None
        if listing is None:
            raise ServiceError(problem)
        return listing

    async def poll_youtube(self) -> None:
        ids = [row["youtube_id"] for row in await self.db.all("SELECT DISTINCT youtube_id FROM youtube_subs")]
        for index, youtube_id in enumerate(ids):
            if index:
                await self._sleep(YOUTUBE_GAP)
            try:
                await self._poll_youtube_channel(youtube_id)
            except Exception:
                log.exception("YouTube check failed for %s", youtube_id)
            await self._deliver_youtube(youtube_id)  # what a server is owed goes out whether the feed answered this time or not

    async def _poll_youtube_channel(self, youtube_id: str) -> None:
        now = self._clock()
        state = await self.db.one("SELECT * FROM youtube_state WHERE youtube_id = ?", (youtube_id,)) or {"failures": 0, "next_check": 0, "seen": None}
        if state["next_check"] > now:
            return
        try:
            listing = await self._youtube_listing(youtube_id)
        except (ServiceError, OutsideError) as error:
            failures = state["failures"] + 1
            wait = min(3600, YOUTUBE_SECONDS * 2 ** min(failures, 8))
            log.info("YouTube check failed for %s, try %d: %s", youtube_id, failures, error)
            await self.db.run(
                "INSERT INTO youtube_state (youtube_id, first_at, checked_at, failures, next_check, last_error) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (youtube_id) DO UPDATE SET checked_at = excluded.checked_at, failures = excluded.failures, next_check = excluded.next_check, last_error = excluded.last_error",
                (youtube_id, now, now, failures, now + wait, str(error)),
            )
            return
        seen = json.loads(state["seen"]) if state.get("seen") is not None else None
        fresh = new_videos(listing, seen, now, state.get("first_at") or now)
        updated = list(dict.fromkeys([video.id for video in listing.videos] + (seen or [])))[:YOUTUBE_SEEN_KEPT]
        # Together, so a video counts as seen only once each server is owed it. Nothing is sent inside, so a post can't hold this up or undo it.
        async with self.db.transaction():
            await self._owe_videos(youtube_id, fresh, listing.name, now)
            await self.db.run(
                "INSERT INTO youtube_state (youtube_id, seen, first_at, checked_at, ok_at, failures, next_check, last_error) VALUES (?, ?, ?, ?, ?, 0, 0, NULL) "
                "ON CONFLICT (youtube_id) DO UPDATE SET seen = excluded.seen, checked_at = excluded.checked_at, ok_at = excluded.ok_at, failures = 0, next_check = 0, last_error = NULL",
                (youtube_id, json.dumps(updated), now, now, now),
            )
            if listing.name:
                await self.db.run("UPDATE youtube_subs SET youtube_name = ? WHERE youtube_id = ?", (listing.name, youtube_id))

    async def _owe_videos(self, youtube_id: str, videos: list[Video], name: str | None, now: float) -> None:
        """Writes these videos down against each server following the channel. A long outage keeps the newest ones."""
        if not videos:
            return
        for sub in await self.db.all("SELECT guild_id, shorts, pending FROM youtube_subs WHERE youtube_id = ?", (youtube_id,)):
            wanted = [{"id": video.id, "title": video.title, "link": video.link, "author": name, "at": now} for video in videos if sub["shorts"] or "/shorts/" not in video.link]
            if wanted:
                owed = (self._owed(sub["pending"]) + wanted)[-YOUTUBE_PENDING_KEPT:]
                await self.db.run("UPDATE youtube_subs SET pending = ? WHERE guild_id = ? AND youtube_id = ?", (json.dumps(owed), sub["guild_id"], youtube_id))

    @staticmethod
    def _owed(pending: str | None) -> list[dict]:
        """What a row says is still owed. It can come from a backup file someone edited, so anything unreadable counts as nothing."""
        try:
            rows = json.loads(pending or "[]")
        except ValueError:
            return []
        return [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("link"), str) and isinstance(row.get("at"), (int, float))] if isinstance(rows, list) else []

    async def _deliver_youtube(self, youtube_id: str) -> None:
        for sub in await self.db.all("SELECT * FROM youtube_subs WHERE youtube_id = ? AND pending IS NOT NULL", (youtube_id,)):
            try:
                await self._deliver_youtube_to(sub)
            except Exception:
                log.exception("YouTube alerts failed for %s in %s", youtube_id, sub["guild_id"])

    async def _deliver_youtube_to(self, sub: dict) -> None:
        """Send one server what it's owed, oldest first. A failure leaves that video and the later ones for the next round, and touches no other server."""
        where = (sub["guild_id"], sub["youtube_id"])
        now = self._clock()
        owed = [row for row in self._owed(sub["pending"]) if now - row["at"] <= YOUTUBE_MAX_AGE]

        async def note(problem: str | None) -> None:
            await self.db.run("UPDATE youtube_subs SET pending = ?, last_error = ? WHERE guild_id = ? AND youtube_id = ?", (json.dumps(owed) if owed else None, problem, *where))

        if not owed:
            await note(sub["last_error"])
            return
        guild = self.bot.get_guild(sub["guild_id"])
        if guild is None:
            await note(SERVER_AWAY)
            return
        channel = await delivery.find_channel(guild, sub["channel_id"])
        if channel is None or not delivery.holds_messages(channel):
            await note(CHANNEL_GONE if channel is None else delivery.NOT_POSTABLE)
            return
        for row in list(owed[:YOUTUBE_PER_ROUND]):
            values = {"author": _plain(row.get("author") or sub["youtube_name"]), "link": row["link"], "title": _plain(row.get("title"))}
            content = await filled_alert_text(sub["fmt"], sub["role_id"], guild_id=sub["guild_id"], guild=guild, channel=channel, values=values)
            try:
                pinged = await self._post_alert(guild, channel, {"content": content, "allowed_mentions": alert_mentions(sub["fmt"], sub["role_id"])})
            except Exception as error:
                log.info("Could not post a YouTube alert in %s: %r", sub["guild_id"], error)
                if not delivery.refused_for_good(error):
                    await note(self._send_problem(error))
                    return
                owed.remove(row)
                await note(self._send_problem(error))
                continue
            owed.remove(row)
            await self.db.run(
                "UPDATE youtube_subs SET pending = ?, last_video = ?, last_alert = ?, last_error = ? WHERE guild_id = ? AND youtube_id = ?",
                (json.dumps(owed) if owed else None, row["id"], self._clock(), None if pinged else NOT_PINGED, *where),
            )

    async def _youtube_store(self, guild_id: int, youtube_id: str, name: str, channel_id: int, fmt: str, role_id: int | None, shorts: bool, actor_id: int | None) -> None:
        count = await self.db.scalar("SELECT COUNT(*) FROM youtube_subs WHERE guild_id = ?", (guild_id,))
        exists = await self.db.scalar("SELECT 1 FROM youtube_subs WHERE guild_id = ? AND youtube_id = ?", (guild_id, youtube_id))
        if count >= MAX_SUBSCRIPTIONS and not exists:
            raise ServiceError(f"A server can follow at most {MAX_SUBSCRIPTIONS} YouTube channels.")
        await self.db.run(
            "INSERT INTO youtube_subs (guild_id, youtube_id, youtube_name, channel_id, fmt, role_id, shorts, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, youtube_id) DO UPDATE SET youtube_name = excluded.youtube_name, channel_id = excluded.channel_id, fmt = excluded.fmt, role_id = excluded.role_id, "
            "shorts = excluded.shorts, last_error = NULL",
            (guild_id, youtube_id, name, channel_id, fmt, role_id, int(shorts), actor_id, self._clock()),
        )
        # Look now, so what's already on the channel is on record before the first round and nothing old gets announced.
        known = await self.db.scalar("SELECT seen FROM youtube_state WHERE youtube_id = ?", (youtube_id,))
        if known is None:
            try:
                listing = await self._youtube_listing(youtube_id)
            except (ServiceError, OutsideError) as error:
                log.info("First look at %s failed, the next round tries again: %s", youtube_id, error)  # no record means that round only records too
                return
            await self.db.run(
                "INSERT INTO youtube_state (youtube_id, seen, first_at, checked_at, ok_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (youtube_id) DO UPDATE SET seen = excluded.seen, ok_at = excluded.ok_at",
                (youtube_id, json.dumps([video.id for video in listing.videos][:YOUTUBE_SEEN_KEPT]), self._clock(), self._clock(), self._clock()),
            )

    async def _youtube_delete(self, guild_id: int, youtube_id: str) -> None:
        """When the last server stops following a channel, its record goes too, so a later follow starts from what's there then."""
        await self.db.run("DELETE FROM youtube_subs WHERE guild_id = ? AND youtube_id = ?", (guild_id, youtube_id))
        await self.db.run("DELETE FROM youtube_state WHERE youtube_id = ? AND NOT EXISTS (SELECT 1 FROM youtube_subs WHERE youtube_subs.youtube_id = youtube_state.youtube_id)", (youtube_id,))

    async def _find_youtube(self, guild_id: int, name: str) -> dict | None:
        cleaned = name.strip()
        row = await self.db.one("SELECT * FROM youtube_subs WHERE guild_id = ? AND (youtube_id = ? OR lower(youtube_name) = ?)", (guild_id, cleaned, cleaned.lower()))
        if row is None and ("youtu" in cleaned or cleaned.startswith("@")):
            try:
                kind, target = youtube_target(cleaned)
            except ServiceError:
                return None
            if kind == "channel":
                return await self.db.one("SELECT * FROM youtube_subs WHERE guild_id = ? AND youtube_id = ?", (guild_id, target))
        return row

    async def _youtube_name_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        rows = await self.db.all("SELECT youtube_name, youtube_id FROM youtube_subs WHERE guild_id = ? AND lower(youtube_name) LIKE ? ORDER BY youtube_name LIMIT 25", (interaction.guild_id, f"{current.lower()}%"))
        return [app_commands.Choice(name=row["youtube_name"][:100], value=row["youtube_id"]) for row in rows]

    # ---- game alerts ----

    def _source_ready(self, source: str) -> bool:
        return self._backoff.get(source, (0.0, 0))[0] <= self._clock()

    def _source_failed(self, source: str, base: float, error: Any, wait: float | None = None) -> None:
        fails = self._backoff.get(source, (0.0, 0))[1] + 1
        delay = wait if wait is not None else min(LONGEST_BACKOFF, base * 2 ** min(fails, 6))
        self._backoff[source] = (self._clock() + delay, fails)
        log.info("%s failed, waiting %.0f seconds: %s", source, delay, error)
        self._note(source, str(error))

    def _source_worked(self, source: str) -> None:
        self._backoff.pop(source, None)
        self._note(source, None)

    def _game_guilds(self, flag: str) -> list[int]:
        return [guild_id for guild_id in self.bot.settings.guilds_with(self.key) if self.cfg(guild_id)["alert_channel"] and self.cfg(guild_id)[flag]]

    async def _forget_disabled(self, source: str, enabled: list[int]) -> None:
        """A server that turned a source off looks at it afresh when it comes back, instead of announcing everything it missed."""
        marks = ",".join("?" * len(enabled))
        await self.db.run(f"DELETE FROM game_alert_state WHERE source = ? AND guild_id NOT IN ({marks})", (source, *enabled))

    async def poll_free_games(self) -> None:
        guilds = self._game_guilds("alerts_enabled")
        await self._forget_disabled("free", guilds)
        if not guilds or not self._source_ready("gamerpower"):
            return
        try:
            reply = await self.outside.get(GAMERPOWER_URL, params={"type": "game", "sort-by": "date"}, timeout=30.0)
            if reply.status == 429:
                self._source_failed("gamerpower", FREE_SECONDS, "GamerPower asked the bot to slow down.", reply.retry_after(FREE_SECONDS))
                return
            if reply.status not in (200, 201):  # 201 means no active giveaways
                raise ServiceError(f"GamerPower answered with error {reply.status}.")
            items = reply.json() if reply.status == 200 else []
        except (ServiceError, OutsideError) as error:
            self._source_failed("gamerpower", FREE_SECONDS, error)
            return
        self._source_worked("gamerpower")
        items = [item for item in items if isinstance(item, dict) and item.get("id") is not None] if isinstance(items, list) else []
        for guild_id in guilds:
            cfg = self.cfg(guild_id)
            await self._deliver(
                guild_id, "free", json.dumps(cfg["free_platforms"]), cfg,
                [(str(item["id"]), item) for item in items if _wanted_free(item, cfg)], free_game_payload, "free_format",
            )

    async def poll_deals(self) -> None:
        guilds = self._game_guilds("deals_enabled")
        await self._forget_disabled("deals", guilds)
        if not guilds or not self._source_ready("cheapshark"):
            return
        try:
            reply = await self.outside.get(CHEAPSHARK_URL, params={"sortBy": "Recent", "onSale": "1", "pageSize": "60"}, timeout=30.0)
            if reply.status == 429:
                self._source_failed("cheapshark", DEALS_SECONDS, "CheapShark asked the bot to slow down.", reply.retry_after(DEALS_SECONDS))
                return
            if reply.status != 200:
                raise ServiceError(f"CheapShark answered with error {reply.status}.")
            items = reply.json()
        except (ServiceError, OutsideError) as error:
            self._source_failed("cheapshark", DEALS_SECONDS, error)
            return
        self._source_worked("cheapshark")
        items = [item for item in items if isinstance(item, dict) and item.get("dealID")] if isinstance(items, list) else []
        for guild_id in guilds:
            cfg = self.cfg(guild_id)
            await self._deliver(
                guild_id, "deals", json.dumps([cfg["discount_threshold"], cfg["deal_min_rating"], cfg["deal_stores"]]), cfg,
                # The same game at the same store at the same price is one deal, whatever ID CheapShark gives it today.
                [(f"{item.get('gameID')}:{item.get('storeID')}:{item.get('salePrice')}", item) for item in items if _wanted_deal(item, cfg)], deal_payload, "deal_format",
            )

    async def _deliver(self, guild_id: int, source: str, filters: str, cfg: dict, items: list[tuple[str, dict]], build: Callable, format_field: str) -> None:
        """Post what this server hasn't seen. The first look, and the first look after its filters change, only records what's there."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        now = self._clock()
        try:
            known = await self.db.scalar("SELECT filter FROM game_alert_state WHERE guild_id = ? AND source = ?", (guild_id, source))
            seen = {row["item"] for row in await self.db.all("SELECT item FROM game_alert_seen WHERE guild_id = ? AND source = ?", (guild_id, source))}
            fresh = [row for row in items if row[0] not in seen]
            if known != filters:
                await self._mark_seen(guild_id, source, [row[0] for row in fresh], now)
                await self.db.run(
                    "INSERT INTO game_alert_state (guild_id, source, filter) VALUES (?, ?, ?) ON CONFLICT (guild_id, source) DO UPDATE SET filter = excluded.filter",
                    (guild_id, source, filters),
                )
                return
            status_key = f"{source}:{guild_id}"
            channel = await delivery.find_channel(guild, cfg["alert_channel"])
            if channel is None or not delivery.holds_messages(channel):
                self._note(status_key, GAME_CHANNEL_GONE if channel is None else delivery.NOT_POSTABLE)
                return
            if self._status.get(status_key, {}).get("error") in (GAME_CHANNEL_GONE, delivery.NOT_POSTABLE):
                self._note(status_key, None)  # the channel is fine now, even with nothing new to prove it
            if not fresh:
                return
            problem = None
            for key, item in reversed(fresh[:ALERTS_PER_ROUND]):
                try:
                    # The alert's format is a template, and its text can take a quarter second to fill in.
                    payload = messages.clean_payload(await budgeted_render(lambda: build(item, cfg, guild=guild, channel=channel), guild_id, pool=budget.MESSAGES))
                except OverBudget:
                    break  # what's left stays unseen, so it goes out next round
                except Exception as error:  # a service's answer can hold anything, and one bad item can't stop the rest
                    log.info("Skipping a %s alert that Discord wouldn't take: %r", source, error)
                    await self._mark_seen(guild_id, source, [key], now)
                    continue
                try:
                    pinged = await self._post_alert(guild, channel, messages.send_kwargs(payload, allowed_mentions=alert_mentions(cfg[format_field], cfg["alert_role"])))
                except Exception as error:
                    log.info("Could not post a %s alert in %s: %r", source, guild_id, error)
                    problem = self._send_problem(error)
                    if delivery.refused_for_good(error):  # Discord won't take this one, so it's passed over and the rest still go
                        await self._mark_seen(guild_id, source, [key], now)
                        continue
                    self._note(status_key, problem)
                    break  # what's left stays unseen, so it goes out once the channel works again
                await self._mark_seen(guild_id, source, [key], now)  # one at a time, so a failure further on can't post this again
                if not pinged:
                    problem = problem or NOT_PINGED
            else:
                await self._mark_seen(guild_id, source, [row[0] for row in fresh[ALERTS_PER_ROUND:]], now)  # a burst beyond the limit is passed over, not saved for later
                self._note(status_key, problem)
            # A giveaway ID is never reused. A deal can honestly come back later at the same price.
            await self.db.run("DELETE FROM game_alert_seen WHERE guild_id = ? AND source = ? AND at < ?", (guild_id, source, now - (31536000 if source == "free" else 2592000)))
        except Exception:
            log.exception("Game alerts failed for %s", guild_id)

    async def _mark_seen(self, guild_id: int, source: str, keys: list[str], now: float) -> None:
        if keys:
            await self.db.many("INSERT OR IGNORE INTO game_alert_seen (guild_id, source, item, at) VALUES (?, ?, ?, ?)", [(guild_id, source, key, now) for key in keys])

    # ---- commands: /twitch ----

    async def _refuse_alert(self, ctx: commands.Context, channel: Any, fmt: str, role: Any) -> bool:
        """Whether this person can't aim alerts at this channel. An alert posts what they wrote, and pings who they named, later and in the bot's name."""
        if role is not None and role.is_default():
            await ctx.fail("Use {everyone} in the alert text to ping everyone. The role is for a role of its own.")
            return True
        refusal = access.post_refusal(ctx.author, channel)
        if refusal is None and adds_ping(ctx.guild, channel.id, fmt, role.id if role else None):
            refusal = access.ping_refusal(ctx.author, channel)
        if refusal:
            await ctx.fail(refusal)
            return True
        return False

    async def _refuse_new_format(self, ctx: commands.Context, row: dict, fmt: str) -> bool:
        """New text in the channel a row already has. A manager can fix a typo there, and a new ping still takes what it would take them."""
        channel = ctx.guild.get_channel_or_thread(row["channel_id"])
        if channel is None or not adds_ping(ctx.guild, row["channel_id"], fmt, row["role_id"], row):
            return False
        refusal = access.ping_refusal(ctx.author, channel)
        if refusal:
            await ctx.fail(refusal)
        return refusal is not None

    async def _refuse_hidden(self, ctx: commands.Context, row: dict) -> bool:
        """A row in a channel this person can't see stays out of their reach, as it does on the dashboard. A channel that's gone has nothing left to hide."""
        channel = ctx.guild.get_channel_or_thread(row["channel_id"])
        refusal = access.view_refusal(ctx.author, channel) if channel is not None else None
        if refusal:
            await ctx.fail(refusal)
            return True
        return False

    async def _save(self, ctx: commands.Context, action: str, form: dict) -> bool:
        try:
            await self.bot.settings.save(ctx.guild.id, self.key, form, actor=self.bot.actor(ctx.author), action=action)
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values())))
            return False
        return True

    @commands.hybrid_group(name="twitch", fallback="list", description="Show the Twitch channels this server follows")
    @commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @requires("manage_guild")
    async def twitch(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT * FROM twitch_subs WHERE guild_id = ? ORDER BY stream_name", (ctx.guild.id,))
        lines = [f"{'**live** ' if row['live_id'] else ''}[{discord.utils.escape_markdown(row['stream_name'])}](https://twitch.tv/{row['stream_login']}) in <#{row['channel_id']}>" for row in rows]
        await ctx.send(embed=discord.Embed(title="Twitch alerts", description=delivery.clipped(lines) or "This server follows no Twitch channels yet.", color=0x9146FF), allowed_mentions=discord.AllowedMentions.none())

    @twitch.command(name="add", description="Post an alert in a channel when this Twitch channel goes live")
    @requires("manage_guild")
    @app_commands.describe(name="The Twitch name or link", channel="Where alerts go. This channel if you leave it out.", format="The alert's text. {link} {name} {game} {title} {everyone} {role} work.", role="A role to ping")
    async def twitch_add(self, ctx: commands.Context, name: str, channel: discord.TextChannel | None = None, *, format: str | None = None, role: discord.Role | None = None) -> None:
        cfg = self.cfg(ctx.guild.id)
        format, role, problem = self._typed_alert_text(ctx, channel, format, role)
        if problem:
            await ctx.fail(problem)
            return
        target = channel or ctx.guild.get_channel(cfg["twitch_channel"] or 0) or ctx.channel
        if await self._refuse_alert(ctx, target, format or "", role):
            return
        await ctx.defer()
        try:
            user = await self._twitch_lookup(cfg, name)
            await self._twitch_store(ctx.guild.id, user, target.id, (format or DEFAULT_TWITCH_FORMAT)[:1000], role.id if role else None, ctx.author.id)
        except (ServiceError, OutsideError) as error:
            await ctx.fail(str(error))
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "twitch.add", detail=user.get("login"))
        await ctx.ok(f"Alerts for {user.get('display_name') or name} now go to {target.mention}.")

    @staticmethod
    def _typed_alert_text(ctx: commands.Context, channel: Any, format: str | None, role: Any) -> tuple[str | None, Any, str | None]:
        """Typed, the rest of the line is the alert's text, and a role mention can end it. One word with no {name} in it, where a channel could go, is a channel that didn't match, and must not become the alert's text."""
        if ctx.interaction is not None or not format:
            return format, role, None
        at_end = ROLE_AT_END.search(format)
        found = ctx.guild.get_role(int(at_end.group(1))) if at_end else None
        if found is not None:
            role, format = found, format[:at_end.start()].strip() or None
        if channel is None and format and " " not in format and "{" not in format:
            return format, role, f"I can't find a channel called `{format.replace('`', '')[:60]}`, so nothing changed."
        return format, role, None

    @twitch.command(name="default_channel", with_app_command=False, description="Set the channel new Twitch alerts go to when none is named, or clear it")
    @requires("manage_guild")
    async def twitch_default_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        if channel is not None:
            refusal = access.post_refusal(ctx.author, channel)
            if refusal:
                await ctx.fail(refusal)
                return
        await self.bot.settings.save(ctx.guild.id, self.key, {"twitch_channel": channel.id if channel else None}, actor=self.bot.actor(ctx.author), action="twitch.channel")
        await ctx.ok(f"New Twitch alerts go to {channel.mention} unless you name another channel." if channel else "New Twitch alerts go to the channel the command is used in.")

    @twitch.command(name="online", description="Show which followed Twitch channels are live right now")
    @requires("manage_guild")
    async def twitch_online(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT * FROM twitch_subs WHERE guild_id = ? ORDER BY stream_name", (ctx.guild.id,))
        await ctx.defer()
        try:
            live = await self._twitch_live(self.cfg(ctx.guild.id), [row["stream_id"] for row in rows]) if rows else {}
        except (ServiceError, OutsideError) as error:
            await ctx.fail(str(error))
            return
        lines = []
        for row in rows:
            stream = live.get(row["stream_id"])
            if stream:
                lines.append(f"[{discord.utils.escape_markdown(row['stream_name'])}](https://twitch.tv/{row['stream_login']}) playing {discord.utils.escape_markdown(stream.get('game_name') or 'something')}")
        await ctx.send(embed=discord.Embed(title="Live now", description=delivery.clipped(lines) or "Nobody this server follows is live.", color=0x9146FF), allowed_mentions=discord.AllowedMentions.none())

    @twitch.command(name="remove", description="Stop alerts for a Twitch channel")
    @requires("manage_guild")
    async def twitch_remove(self, ctx: commands.Context, name: str) -> None:
        row = await self._find_twitch(ctx.guild.id, name)
        if row is None:
            await ctx.fail(f"This server doesn't follow `{name}`.")
            return
        if await self._refuse_hidden(ctx, row):
            return
        await self.db.run("DELETE FROM twitch_subs WHERE guild_id = ? AND stream_id = ?", (ctx.guild.id, row["stream_id"]))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "twitch.remove", detail=row["stream_login"])
        await ctx.ok(f"Alerts for {row['stream_name']} are off.")

    @twitch.command(name="move", description="Send a Twitch channel's alerts to a different channel")
    @requires("manage_guild")
    async def twitch_move(self, ctx: commands.Context, name: str, channel: discord.TextChannel) -> None:
        row = await self._find_twitch(ctx.guild.id, name)
        if row is None:
            await ctx.fail(f"This server doesn't follow `{name}`.")
            return
        if await self._refuse_hidden(ctx, row) or await self._refuse_alert(ctx, channel, row["fmt"], ctx.guild.get_role(row["role_id"]) if row["role_id"] else None):
            return
        await self.db.run("UPDATE twitch_subs SET channel_id = ?, last_error = NULL WHERE guild_id = ? AND stream_id = ?", (channel.id, ctx.guild.id, row["stream_id"]))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "twitch.move", detail=row["stream_login"])
        await ctx.ok(f"Alerts for {row['stream_name']} now go to {channel.mention}.")

    @twitch.command(name="format", aliases=["message"], description="Change a Twitch channel's alert text")
    @requires("manage_guild")
    async def twitch_format(self, ctx: commands.Context, name: str, *, format: str) -> None:
        row = await self._find_twitch(ctx.guild.id, name)
        if row is None:
            await ctx.fail(f"This server doesn't follow `{name}`.")
            return
        if await self._refuse_hidden(ctx, row) or await self._refuse_new_format(ctx, row, format):
            return
        await self.db.run("UPDATE twitch_subs SET fmt = ? WHERE guild_id = ? AND stream_id = ?", (format[:1000], ctx.guild.id, row["stream_id"]))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "twitch.format", detail=row["stream_login"])
        await ctx.ok(f"The alert text for {row['stream_name']} is set.")

    @twitch_remove.autocomplete("name")
    @twitch_move.autocomplete("name")
    @twitch_format.autocomplete("name")
    async def _twitch_names(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._twitch_name_choices(interaction, current)

    # ---- commands: /youtube ----

    @commands.hybrid_group(name="youtube", fallback="list", description="Show the YouTube channels this server follows")
    @commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @requires("manage_guild")
    async def youtube(self, ctx: commands.Context) -> None:
        rows = await self.db.all("SELECT * FROM youtube_subs WHERE guild_id = ? ORDER BY youtube_name", (ctx.guild.id,))
        lines = [f"[{discord.utils.escape_markdown(row['youtube_name'])}]({YOUTUBE_SITE}/channel/{row['youtube_id']}) in <#{row['channel_id']}>" for row in rows]
        await ctx.send(embed=discord.Embed(title="YouTube alerts", description=delivery.clipped(lines) or "This server follows no YouTube channels yet.", color=0xFF0000), allowed_mentions=discord.AllowedMentions.none())

    @youtube.command(name="add", aliases=["sub"], description="Post an alert in a channel when this YouTube channel uploads")
    @requires("manage_guild")
    @app_commands.describe(name="A channel link, video link, @handle or name. A link is the surest.", channel="Where alerts go. This channel if you leave it out.", format="The alert's text. {link} {author} {title} {everyone} {role} work.", role="A role to ping")
    async def youtube_add(self, ctx: commands.Context, name: str, channel: discord.TextChannel | None = None, *, format: str | None = None, role: discord.Role | None = None) -> None:
        format, role, problem = self._typed_alert_text(ctx, channel, format, role)
        if problem:
            await ctx.fail(problem)
            return
        target = channel or ctx.channel
        if await self._refuse_alert(ctx, target, format or "", role):
            return
        await ctx.defer()
        try:
            youtube_id, title = await self._youtube_resolve(name)
            await self._youtube_store(ctx.guild.id, youtube_id, title, target.id, (format or DEFAULT_YOUTUBE_FORMAT)[:1000], role.id if role else None, True, ctx.author.id)
        except (ServiceError, OutsideError) as error:
            await ctx.fail(str(error))
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "youtube.add", detail=title)
        await ctx.ok(f"Alerts for {title} now go to {target.mention}. {YOUTUBE_SITE}/channel/{youtube_id}", suppress_embeds=True)

    @youtube.command(name="remove", aliases=["unsub"], description="Stop alerts for a YouTube channel")
    @requires("manage_guild")
    async def youtube_remove(self, ctx: commands.Context, name: str) -> None:
        row = await self._find_youtube(ctx.guild.id, name)
        if row is None:
            await ctx.fail(f"This server doesn't follow `{name}`.")
            return
        if await self._refuse_hidden(ctx, row):
            return
        await self._youtube_delete(ctx.guild.id, row["youtube_id"])
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "youtube.remove", detail=row["youtube_name"])
        await ctx.ok(f"Alerts for {row['youtube_name']} are off.")

    @youtube.command(name="move", description="Send a YouTube channel's alerts to a different channel")
    @requires("manage_guild")
    async def youtube_move(self, ctx: commands.Context, name: str, channel: discord.TextChannel) -> None:
        row = await self._find_youtube(ctx.guild.id, name)
        if row is None:
            await ctx.fail(f"This server doesn't follow `{name}`.")
            return
        if await self._refuse_hidden(ctx, row) or await self._refuse_alert(ctx, channel, row["fmt"], ctx.guild.get_role(row["role_id"]) if row["role_id"] else None):
            return
        await self.db.run("UPDATE youtube_subs SET channel_id = ?, last_error = NULL WHERE guild_id = ? AND youtube_id = ?", (channel.id, ctx.guild.id, row["youtube_id"]))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "youtube.move", detail=row["youtube_name"])
        await ctx.ok(f"Alerts for {row['youtube_name']} now go to {channel.mention}.")

    @youtube.command(name="format", aliases=["message"], description="Change a YouTube channel's alert text")
    @requires("manage_guild")
    async def youtube_format(self, ctx: commands.Context, name: str, *, format: str) -> None:
        row = await self._find_youtube(ctx.guild.id, name)
        if row is None:
            await ctx.fail(f"This server doesn't follow `{name}`.")
            return
        if await self._refuse_hidden(ctx, row) or await self._refuse_new_format(ctx, row, format):
            return
        await self.db.run("UPDATE youtube_subs SET fmt = ? WHERE guild_id = ? AND youtube_id = ?", (format[:1000], ctx.guild.id, row["youtube_id"]))
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "youtube.format", detail=row["youtube_name"])
        await ctx.ok(f"The alert text for {row['youtube_name']} is set.")

    @youtube_remove.autocomplete("name")
    @youtube_move.autocomplete("name")
    @youtube_format.autocomplete("name")
    async def _youtube_names(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._youtube_name_choices(interaction, current)

    # ---- commands: /games alerts ----

    def _attach_games_alerts(self) -> None:
        """/games belongs to the fun module, and alerts belong here, so the subcommand joins that group once every module has loaded."""
        parent = self.bot.get_command("games")
        if parent is None:
            parent = commands.HybridGroup(Notifications._games_root, name="games", description="Game alerts")
            parent.cog = self
            self.bot.add_command(parent)
        if parent.get_command("alerts") is None:
            command = commands.HybridCommand(Notifications._games_alerts, name="alerts", description="Show or set free game and deal alerts")
            command.cog = self
            parent.add_command(command)

    async def _games_root(self, ctx: commands.Context) -> None:
        await self._show_game_alerts(ctx)

    async def _show_game_alerts(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)
        channel = f"<#{cfg['alert_channel']}>" if cfg["alert_channel"] else "none, so nothing is posted"
        await ctx.send(
            f"Alert channel: {channel}\nFree games: {'on' if cfg['alerts_enabled'] else 'off'}\n"
            f"Deals: {'on' if cfg['deals_enabled'] else 'off'}, from {cfg['discount_threshold']}% off\n"
            f"All settings: {self.bot.public_url}/dashboard/{ctx.guild.id}/gamealerts",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @requires("manage_guild")
    async def _games_alerts(self, ctx: commands.Context, config: Literal["free-on", "free-off", "deals-on", "deals-off"] | None = None, channel: discord.TextChannel | None = None) -> None:
        if config is None and channel is None:
            await self._show_game_alerts(ctx)
            return
        form: dict[str, Any] = {}
        if channel is not None:
            refusal = access.post_refusal(ctx.author, channel)
            if refusal is None and any(self.pinged_channels({**self.cfg(ctx.guild.id), "alert_channel": channel.id}).values()):
                refusal = access.ping_refusal(ctx.author, channel)
            if refusal:
                await ctx.fail(refusal)
                return
            form["alert_channel"] = channel.id
        if config is not None:
            kind, _, state = config.partition("-")
            form["alerts_enabled" if kind == "free" else "deals_enabled"] = state == "on"
        if await self._save(ctx, "gamealerts.update", form):
            cfg = self.cfg(ctx.guild.id)
            where = f"<#{cfg['alert_channel']}>" if cfg["alert_channel"] else "no channel yet"
            await ctx.ok(f"Free games {'on' if cfg['alerts_enabled'] else 'off'}, deals {'on' if cfg['deals_enabled'] else 'off'}, posting to {where}.")

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/twitch", self._api_twitch)
        router.get("/twitch/channels", self._api_twitch_search)
        router.post("/twitch", self._api_twitch_add)
        router.post("/twitch/check", self._api_twitch_check)
        router.put("/twitch/{stream_id:\\d+}", self._api_twitch_edit)
        router.delete("/twitch/{stream_id:\\d+}", self._api_twitch_delete)
        router.post("/twitch/{stream_id:\\d+}/test", self._api_twitch_test)
        router.get("/youtube", self._api_youtube)
        router.post("/youtube", self._api_youtube_add)
        router.put("/youtube/{youtube_id:[0-9A-Za-z_-]+}", self._api_youtube_edit)
        router.delete("/youtube/{youtube_id:[0-9A-Za-z_-]+}", self._api_youtube_delete)
        router.post("/youtube/{youtube_id:[0-9A-Za-z_-]+}/test", self._api_youtube_test)
        router.get("/gamealerts", self._api_games)
        router.post("/gamealerts/test", self._api_games_test)

    def _form_role(self, req: Any, value: Any, field: str = "role_id") -> int | None:
        if value in (None, ""):
            return None
        try:
            role_id = snowflake(value)
        except Invalid as error:
            raise ValidationError({field: "That isn't a role."}) from error
        info = req.ops.describe_guild(req.guild_id) or {}
        if not any(role["id"] == role_id for role in info.get("roles", [])):
            raise ValidationError({field: "That role isn't in this server."})
        if role_id == req.guild_id:
            raise ValidationError({field: "Use {everyone} in the alert text to ping everyone. The role is for a role of its own."})
        return role_id

    @staticmethod
    def _check_new_ping(req: Any, channel_id: int, fmt: str, role_id: int | None, row: dict | None = None) -> None:
        """A ping saved now goes out later in the bot's name, so whoever saves it must be able to send it themselves. One a row already sends there stays through an edit."""
        field = adds_ping(req.bot.get_guild(req.guild_id), channel_id, fmt, role_id, row)
        if field:
            req.require_ping(channel_id, field)

    @staticmethod
    def _form_format(form: dict, default: str) -> str:
        value = form.get("fmt")
        if value in (None, ""):
            return default
        try:
            return text(1000, minimum=1)(value)
        except Invalid as error:
            raise ValidationError({"fmt": str(error)}) from error

    @staticmethod
    def _form(raw: Any) -> dict:
        if not isinstance(raw, dict):
            raise ApiError(400, "Expected a set of fields.")
        return raw

    @staticmethod
    def _hide_row(req: Any, row: dict) -> None:
        """A row in a channel this person can't see stays listed, since it counts toward the limit, with nothing from that channel."""
        row["hidden"] = not req.can_see(row["channel_id"])
        if row["hidden"]:
            row.update(fmt="", role_id=None, last_error=None)

    @staticmethod
    def _require_visible(req: Any, row: dict) -> dict:
        if not req.can_see(row["channel_id"]):
            raise ApiError(403, "You can't see that channel.")
        return row

    @staticmethod
    def _require_posting(req: Any, channel_id: int) -> None:
        if not req.can_post(channel_id):
            raise ApiError(403, "You can't post in that channel.")

    # Twitch endpoints

    async def _api_twitch(self, req: Any) -> dict:
        cfg = self.cfg(req.guild_id)
        rows = await req.db.all(
            "SELECT stream_id, stream_login, stream_name, channel_id, fmt, role_id, live_id, live_since, last_alert, last_error FROM twitch_subs WHERE guild_id = ? ORDER BY stream_name",
            (req.guild_id,),
        )
        for row in rows:
            row["live"] = bool(row.pop("live_id"))
            self._hide_row(req, row)
        client_id = cfg["twitch_client_id"]
        return {
            "rows": rows, "limit": MAX_SUBSCRIPTIONS, "configured": self._has_twitch(cfg), "client_id_hint": client_id[-4:] if len(client_id) > 8 else "",
            "status": self._status.get(f"twitch:{req.guild_id}", {}), "default_format": DEFAULT_TWITCH_FORMAT,
        }

    async def _api_twitch_search(self, req: Any) -> list[dict]:
        query = req.query.get("query", "").strip()[:60]
        if not query:
            return []
        cfg = self.cfg(req.guild_id)
        if not self._has_twitch(cfg):
            raise ApiError(400, "Enter your Twitch client ID and secret first.")
        try:
            data = await self._twitch_get(cfg, "search/channels", [("query", query), ("first", "10")])
        except (ServiceError, OutsideError) as error:
            raise ApiError(502, str(error)) from error
        return [{"id": str(row.get("id")), "login": row.get("broadcaster_login"), "name": row.get("display_name"), "live": bool(row.get("is_live")), "avatar": row.get("thumbnail_url")} for row in data.get("data") or [] if isinstance(row, dict)]

    async def _api_twitch_add(self, req: Any) -> dict:
        form = self._form(await req.json())
        channel_id = delivery.form_channel(req, form.get("channel_id"))
        role_id = self._form_role(req, form.get("role_id"))
        fmt = self._form_format(form, DEFAULT_TWITCH_FORMAT)
        self._check_new_ping(req, channel_id, fmt, role_id)
        if not isinstance(form.get("name"), str) or not form["name"].strip():
            raise ValidationError({"name": "Pick a Twitch channel."})
        try:
            user = await self._twitch_lookup(self.cfg(req.guild_id), form["name"])
            await self._twitch_store(req.guild_id, user, channel_id, fmt, role_id, req.actor.id)
        except (ServiceError, OutsideError) as error:
            raise ApiError(502, str(error)) from error
        await req.record("twitch.add", detail=user.get("login"))
        return {"stream_id": str(user["id"]), "stream_name": user.get("display_name")}

    async def _api_twitch_check(self, req: Any) -> dict:
        cfg = self.cfg(req.guild_id)
        self._tokens.pop((cfg["twitch_client_id"], cfg["twitch_client_secret"]), None)
        try:
            await self._twitch_token(cfg)
        except (ServiceError, OutsideError) as error:
            raise ApiError(502, str(error)) from error
        return {"ok": True}

    async def _twitch_row(self, req: Any) -> dict:
        row = await req.db.one("SELECT * FROM twitch_subs WHERE guild_id = ? AND stream_id = ?", (req.guild_id, req.param("stream_id")))
        if row is None:
            raise ApiError(404, "This server doesn't follow that channel.")
        return self._require_visible(req, row)

    async def _api_twitch_edit(self, req: Any) -> dict:
        row = await self._twitch_row(req)
        form = self._form(await req.json())
        channel_id = delivery.form_channel(req, form.get("channel_id"), keeping=row["channel_id"])
        role_id = self._form_role(req, form.get("role_id"))
        fmt = self._form_format(form, DEFAULT_TWITCH_FORMAT)
        self._check_new_ping(req, channel_id, fmt, role_id, row)
        await req.db.run("UPDATE twitch_subs SET channel_id = ?, fmt = ?, role_id = ?, last_error = NULL WHERE guild_id = ? AND stream_id = ?", (channel_id, fmt, role_id, req.guild_id, row["stream_id"]))
        await req.record("twitch.edit", detail=row["stream_login"])
        return {"ok": True}

    async def _api_twitch_delete(self, req: Any) -> dict:
        row = await self._twitch_row(req)
        await req.db.run("DELETE FROM twitch_subs WHERE guild_id = ? AND stream_id = ?", (req.guild_id, row["stream_id"]))
        await req.record("twitch.remove", detail=row["stream_login"])
        return {"ok": True}

    async def _api_twitch_test(self, req: Any) -> dict:
        row = await self._twitch_row(req)
        self._require_posting(req, row["channel_id"])
        values = {"name": _plain(row["stream_name"]), "link": f"https://twitch.tv/{row['stream_login']}", "game": "a sample game", "title": "A sample stream title"}
        content = await filled_alert_text(row["fmt"], row["role_id"], guild_id=req.guild_id, values=values)
        sent = await req.ops.send_message(row["channel_id"], {"content": content, "embeds": [], "buttons": []})  # no role or everyone ping in a test
        await req.record("twitch.test", detail=row["stream_login"])
        return sent

    # YouTube endpoints

    async def _api_youtube(self, req: Any) -> dict:
        rows = await req.db.all(
            "SELECT s.youtube_id, s.youtube_name, s.channel_id, s.fmt, s.role_id, s.shorts, s.last_video, s.last_alert, s.last_error, y.ok_at, y.checked_at, y.last_error AS feed_error "
            "FROM youtube_subs s LEFT JOIN youtube_state y ON y.youtube_id = s.youtube_id WHERE s.guild_id = ? ORDER BY s.youtube_name",
            (req.guild_id,),
        )
        for row in rows:
            row["shorts"] = bool(row["shorts"])
            self._hide_row(req, row)
        return {"rows": rows, "limit": MAX_SUBSCRIPTIONS, "default_format": DEFAULT_YOUTUBE_FORMAT}

    async def _api_youtube_add(self, req: Any) -> dict:
        form = self._form(await req.json())
        channel_id = delivery.form_channel(req, form.get("channel_id"))
        role_id = self._form_role(req, form.get("role_id"))
        fmt = self._form_format(form, DEFAULT_YOUTUBE_FORMAT)
        self._check_new_ping(req, channel_id, fmt, role_id)
        if not isinstance(form.get("link"), str) or not form["link"].strip():
            raise ValidationError({"link": "Enter a channel link, a video link, an @handle or a name."})
        try:
            youtube_id, title = await self._youtube_resolve(form["link"][:300])
            await self._youtube_store(req.guild_id, youtube_id, title, channel_id, fmt, role_id, form.get("shorts") is not False, req.actor.id)
        except (ServiceError, OutsideError) as error:
            raise ApiError(502, str(error)) from error
        await req.record("youtube.add", detail=title)
        return {"youtube_id": youtube_id, "youtube_name": title}

    async def _youtube_row(self, req: Any) -> dict:
        row = await req.db.one("SELECT * FROM youtube_subs WHERE guild_id = ? AND youtube_id = ?", (req.guild_id, req.param("youtube_id")))
        if row is None:
            raise ApiError(404, "This server doesn't follow that channel.")
        return self._require_visible(req, row)

    async def _api_youtube_edit(self, req: Any) -> dict:
        row = await self._youtube_row(req)
        form = self._form(await req.json())
        channel_id = delivery.form_channel(req, form.get("channel_id"), keeping=row["channel_id"])
        role_id = self._form_role(req, form.get("role_id"))
        fmt = self._form_format(form, DEFAULT_YOUTUBE_FORMAT)
        self._check_new_ping(req, channel_id, fmt, role_id, row)
        await req.db.run(
            "UPDATE youtube_subs SET channel_id = ?, fmt = ?, role_id = ?, shorts = ?, last_error = NULL WHERE guild_id = ? AND youtube_id = ?",
            (channel_id, fmt, role_id, int(form.get("shorts") is not False), req.guild_id, row["youtube_id"]),
        )
        await req.record("youtube.edit", detail=row["youtube_name"])
        return {"ok": True}

    async def _api_youtube_delete(self, req: Any) -> dict:
        row = await self._youtube_row(req)
        await self._youtube_delete(req.guild_id, row["youtube_id"])
        await req.record("youtube.remove", detail=row["youtube_name"])
        return {"ok": True}

    async def _api_youtube_test(self, req: Any) -> dict:
        row = await self._youtube_row(req)
        self._require_posting(req, row["channel_id"])
        values = {"author": _plain(row["youtube_name"]), "link": f"{YOUTUBE_SITE}/channel/{row['youtube_id']}", "title": "A sample video title"}
        content = await filled_alert_text(row["fmt"], row["role_id"], guild_id=req.guild_id, values=values)
        sent = await req.ops.send_message(row["channel_id"], {"content": content, "embeds": [], "buttons": []})  # no role or everyone ping in a test
        await req.record("youtube.test", detail=row["youtube_name"])
        return sent

    # Game alert endpoints

    async def _api_games(self, req: Any) -> dict:
        return {
            "platforms": [{"value": value, "label": label} for value, label in FREE_PLATFORMS.items()],
            "stores": [{"value": value, "label": label} for value, label in DEAL_STORES.items()],
            "free": {"service": self._status.get("gamerpower", {}), "delivery": self._status.get(f"free:{req.guild_id}", {})},
            "deals": {"service": self._status.get("cheapshark", {}), "delivery": self._status.get(f"deals:{req.guild_id}", {})},
        }

    async def _api_games_test(self, req: Any) -> dict:
        form = self._form(await req.json())
        cfg = self.cfg(req.guild_id)
        if not cfg["alert_channel"]:
            raise ApiError(400, "Pick and save a notifications channel first.")
        channel_id = delivery.form_channel(req, cfg["alert_channel"], "alert_channel")
        build = deal_payload if form.get("kind") == "deals" else free_game_payload
        sample = SAMPLE_DEAL if form.get("kind") == "deals" else SAMPLE_GIVEAWAY
        sent = await req.ops.send_message(channel_id, messages.clean_payload(build(sample, cfg)))  # no role or everyone ping in a test
        await req.record("gamealerts.test")
        return sent

    async def seed_demo(self, guild_id: int) -> None:
        """Sample subscriptions for the demo dashboard."""
        info = self.bot.ops.describe_guild(guild_id) or {}
        channel = next((row["id"] for row in info.get("channels", []) if row["name"] == "announcements"), None)
        if channel is None:
            return
        now = self._clock()
        await self.bot.settings.save(guild_id, self.key, {"twitch_client_id": "demo-client-id-1234", "twitch_client_secret": "demo-secret", "alert_channel": channel})
        await self.db.run(
            "INSERT INTO twitch_subs (guild_id, stream_id, stream_login, stream_name, channel_id, fmt, added_at, live_id, live_since, last_alert) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, "1001", "sampleplays", "SamplePlays", channel, DEFAULT_TWITCH_FORMAT, now, "44", now - 1200, now - 1200),
        )
        await self.db.run(
            "INSERT INTO twitch_subs (guild_id, stream_id, stream_login, stream_name, channel_id, fmt, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, "1002", "quietstreamer", "QuietStreamer", channel, "{name} started {game}: {link}", now),
        )
        await self.db.run(
            "INSERT INTO youtube_subs (guild_id, youtube_id, youtube_name, channel_id, fmt, added_at, last_alert) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, "UCsample0000000000000000", "Sample Channel", channel, DEFAULT_YOUTUBE_FORMAT, now, now - 86400),
        )
