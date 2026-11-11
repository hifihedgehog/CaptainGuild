"""The welcome picture: a server's settings for it, the pictures it uploads, and the picture greetings asks for when someone joins.

Greetings owns the welcome post and the join DM. This module only makes the picture that goes with them:

    cards = self.bot.module("welcomecard")
    picture = await cards.picture_for(member, "welcome", scope)   # None, or {"filename": ..., "data": ..., "in_embed": ...}

A picture is a design: a canvas and layers. In ready-made mode the design is built from a few simple settings by core/welcomecard/layouts.py,
and in designer mode the server's own stored design is used. Both go through one renderer, and so does the page's preview, so what the
page shows is what Discord gets.

Drawing runs on worker threads, two at a time, behind a short queue. A burst of joins past what the queue holds gets its welcomes with no
picture, which is what a picture that fails for any other reason gets too. Uploaded pictures are kept in the database as text, so a backup
carries them.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import io
import logging
import re
import secrets
import time
from collections import OrderedDict
from typing import Any, AsyncIterator, Callable, Literal
from urllib.parse import urlsplit

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from ..core import images
from ..core.module import Module, requires
from ..core.ops import upload_limit
from ..core.outside import Outside, OutsideError
from ..core.tagscript import OverBudget, Scope, budget, budgeted_render
from ..core.tagscript.preview import preview_scope
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, choice, integer
from ..core.welcomecard import backgrounds, fonts, layouts, model, previews, render, template, uploads
from ..web.api import ApiError, json_response

log = logging.getLogger(__name__)

KINDS = ("welcome", "dm")
MODES = ("layout", "design")
MAX_UPLOADS = 24                          # pictures one server can keep
MAX_UPLOAD_TOTAL = 32 * 1024 * 1024       # and what they may weigh together, as stored
MAX_NAME = 80
PICTURE_SECONDS = 14.0                    # greetings is promised an answer inside 15 seconds
PREVIEW_SECONDS = 20.0
FETCH_SECONDS = 3.0                       # for the avatar and any other picture from Discord
FETCH_KEPT = 300.0                        # how long a fetched picture is reused, and a failed fetch isn't tried again
FETCH_MISS_KEPT = 30.0
FETCH_BYTES = 24 * 1024 * 1024
MISS_BYTES = 256                          # what a failed fetch kept in memory costs besides its address, measured at about 270
BLOB_BYTES = 64 * 1024 * 1024             # uploads held in memory as bytes, so a join doesn't read them from the database again
WORKERS = 2                               # threads drawing joins' pictures, across every server
MAX_WAITING = 16                          # joins' draws running or waiting for a worker. One more is turned away.
PREVIEW_WORKERS = 1                       # previews and the preview command draw on a worker and in a line of their own,
PREVIEW_WAITING = 32                      # so no number of them takes a place or a worker a join's picture needs
PREVIEW_PER_SERVER = 16                   # one server's previews take at most half those places, and a server with none gets one when they are full
MAX_EMOJI = 20                            # server emoji fetched for one picture
RECENT_SECONDS = 60.0                     # a member's picture is kept this long, since the welcome and the join DM ask for the same one
RECENT_BYTES = 24 * 1024 * 1024           # and what those kept pictures may weigh together
CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
# Paths on Discord's image servers that take a size and serve any format, so a still PNG no larger than the layer is asked for.
_SIZED = frozenset({"avatars", "icons", "banners", "splashes", "discovery-splashes", "emojis", "embed", "guilds", "role-icons", "app-icons", "stickers"})
_PATH = re.compile(r"/[A-Za-z0-9_\-./%]{1,400}")
_QUERY = re.compile(r"[A-Za-z0-9_\-=&%.]{0,400}")
_KEY = re.compile(r"[0-9a-f]{16}")
SKIPPED_SECONDS = 600.0                   # how long the page keeps saying that a burst of joins outran the drawing
NO_PINGS = discord.AllowedMentions.none()


class _Busy(Exception):
    """The queue of draws is full."""


class _Line:
    """A few workers and the draws running or waiting for them, counted for each server. A draw holds its place until its thread ends.

    In a line whose servers take turns, a worker that comes free goes to the waiting draw whose server holds the fewest places, the earliest
    of those. A server that draws now and then waits behind the draws already running, and the servers that fill the line wait behind it.
    Otherwise the draws take the workers in the order they came."""

    def __init__(self, turns: bool = False) -> None:
        self.held = 0
        self.by_server: dict[int, int] = {}
        self.slots: asyncio.Semaphore | None = None
        self.running = 0
        self.waiting: list[tuple[int, int, asyncio.Future[None]]] | None = [] if turns else None  # server, when it came, and its turn
        self.came = 0

    @contextlib.asynccontextmanager
    async def worker(self, guild_id: int, workers: int) -> AsyncIterator[None]:
        """Holds one of the line's workers while the block runs."""
        if self.waiting is None:
            if self.slots is None:
                self.slots = asyncio.Semaphore(workers)
            async with self.slots:
                yield
            return
        if self.running < workers and not self.waiting:
            self.running += 1
        else:
            self.came += 1
            entry = (guild_id, self.came, asyncio.get_running_loop().create_future())
            self.waiting.append(entry)
            try:
                await entry[2]
            except BaseException:
                if entry[2].done() and not entry[2].cancelled():
                    self._pass_on()  # the worker came to it as it gave up, so it goes on to the next
                elif entry in self.waiting:
                    self.waiting.remove(entry)
                raise
        try:
            yield
        finally:
            self._pass_on()

    def _pass_on(self) -> None:
        """A worker that came free goes straight to the waiting draw whose server holds the fewest places, or back to the line."""
        if not self.waiting:
            self.running -= 1
            return
        entry = min(self.waiting, key=lambda waiting: (self.by_server.get(waiting[0], 0), waiting[1]))
        self.waiting.remove(entry)
        entry[2].set_result(None)

    def take(self, guild_id: int) -> None:
        self.held += 1
        self.by_server[guild_id] = self.by_server.get(guild_id, 0) + 1

    def leave(self, guild_id: int) -> None:
        self.held -= 1
        left = self.by_server.pop(guild_id, 1) - 1
        if left:
            self.by_server[guild_id] = left


def cdn_address(address: Any, side: int = 256) -> str | None:
    """A Discord image address made safe to fetch, or None for anything else. It must be https on one of Discord's two image servers, with no
    login, port or odd characters. An avatar, icon, banner or emoji is asked for as a still PNG no larger than `side` needs."""
    if not isinstance(address, str) or not address or len(address) > 600:
        return None
    try:
        parts = urlsplit(address.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or host not in CDN_HOSTS or parts.username or parts.password or port not in (None, 443):
        return None
    path = parts.path
    if not _PATH.fullmatch(path) or ".." in path or "//" in path:
        return None
    if path.split("/")[1] in _SIZED:
        stem, dot, ending = path.rpartition(".")
        if dot and ending.lower() in ("webp", "gif"):
            path = f"{stem}.png"
        size = 64
        while size < side and size < 1024:
            size *= 2
        return f"https://{host}{path}?size={size}"
    query = parts.query if _QUERY.fullmatch(parts.query) else ""
    return f"https://{host}{path}" + (f"?{query}" if query else "")


class _Kept:
    """A few things by key, the least recently used dropped first once they pass a byte limit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.items: OrderedDict[Any, tuple[Any, int]] = OrderedDict()
        self.held = 0

    def get(self, key: Any) -> Any:
        found = self.items.get(key)
        if found is None:
            return None
        self.items.move_to_end(key)
        return found[0]

    def put(self, key: Any, value: Any, size: int) -> None:
        old = self.items.pop(key, None)
        if old is not None:
            self.held -= old[1]
        if size > self.limit:
            return
        self.items[key] = (value, size)
        self.held += size
        while self.held > self.limit and self.items:
            _, (_, dropped) = self.items.popitem(last=False)
            self.held -= dropped

    def drop(self, matches: Callable[[Any], bool]) -> None:
        for key in [key for key in self.items if matches(key)]:
            self.held -= self.items.pop(key)[1]


def catalog() -> dict:
    """What the page's pickers offer. None of it differs between servers."""
    return {
        "layouts": layouts.catalog(),
        "fonts": fonts.catalog(),
        "stock": backgrounds.names(),
        "categories": list(backgrounds.CATEGORIES),
        "stripes": [{"name": name, "colors": list(colors)} for name, colors in backgrounds.STRIPE_SETS],
        "positions": list(layouts.TEXT_POSITIONS),
        "shapes": list(layouts.SHAPES),
        "defaults": layouts.DEFAULTS,
        "limits": {
            "side": [model.MIN_SIDE, model.MAX_SIDE], "layers": model.MAX_LAYERS, "text": model.MAX_TEXT, "address": model.MAX_ADDRESS, "colors": model.MAX_COLORS, "stroke": model.MAX_STROKE,
            "radius": model.MAX_RADIUS, "font_size": [model.MIN_FONT_SIZE, model.MAX_FONT_SIZE], "offset": model.MAX_OFFSET, "uploads": MAX_UPLOADS, "upload_bytes": images.MAX_BYTES,
            "upload_total": MAX_UPLOAD_TOTAL, "upload_side": [images.MIN_SIDE, images.MAX_SIDE], "frames": render.MAX_FRAMES,
        },
    }


def _picture_text(value: Any) -> str:
    if not isinstance(value, str):
        raise Invalid("Must be text.")
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(cleaned) > model.MAX_TEXT:
        raise Invalid(f"Must be at most {model.MAX_TEXT:,} characters. This is {len(cleaned):,}.")
    return cleaned


def _background(value: Any) -> dict:
    return model.clean_fill(value, "The background")


def _design(value: Any) -> dict:
    return model.clean_design(value, fonts.KEYS)


def _uploads_named(settings: dict, field: str) -> set[str]:
    value = settings.get(field)
    if not isinstance(value, dict):
        return set()
    if field == "background":
        return {value["id"]} if value.get("kind") == "upload" else set()
    try:
        return model.uploads_in(value)
    except (KeyError, TypeError, AttributeError):
        return set()


class WelcomeCard(Module):
    key = "welcomecard"
    category = "Greetings"
    row_limits = {"welcome_images": MAX_UPLOADS}
    schema = Schema({
        "enabled": Field(False, boolean),
        "dm_image": Field(False, boolean),
        "in_embed": Field(True, boolean),
        "animate": Field(True, boolean),
        "mode": Field("layout", choice(*MODES)),
        "layout": Field(layouts.DEFAULTS["layout"], choice(*layouts.KEYS)),
        "background": Field(layouts.DEFAULTS["background"], _background),
        "text": Field(layouts.DEFAULTS["text"], _picture_text),
        "text_position": Field(layouts.DEFAULTS["text_position"], choice(*layouts.TEXT_POSITIONS)),
        "text_color": Field(layouts.DEFAULTS["text_color"], model.color),
        "outline_color": Field(layouts.DEFAULTS["outline_color"], model.color),
        "outline_width": Field(layouts.DEFAULTS["outline_width"], integer(0, 32)),
        "font": Field(layouts.DEFAULTS["font"], choice(*fonts.KEYS)),
        "avatar": Field(layouts.DEFAULTS["avatar"], boolean),
        "avatar_shape": Field(layouts.DEFAULTS["avatar_shape"], choice(*layouts.SHAPES)),
        "ring_color": Field(layouts.DEFAULTS["ring_color"], model.color),
        "ring_width": Field(layouts.DEFAULTS["ring_width"], integer(0, model.MAX_STROKE)),
        "frame": Field(layouts.DEFAULTS["frame"], boolean),
        "frame_color": Field(layouts.DEFAULTS["frame_color"], model.color),
        "frame_width": Field(layouts.DEFAULTS["frame_width"], integer(1, model.MAX_STROKE)),
        "design": Field(layouts.build(layouts.DEFAULTS), _design),
    })
    migrations = [
        """
        CREATE TABLE welcome_images (
            guild_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            frames INTEGER NOT NULL,
            size INTEGER NOT NULL,
            image TEXT NOT NULL,
            preview TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (guild_id, key)
        );
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.outside = Outside()
        self._known: dict[int, set[str]] = {}  # each server's upload keys, so a save can be checked without the database
        self._storing: dict[int, asyncio.Lock] = {}  # one per server that has uploaded, held from counting its pictures to storing a new one
        self._blobs = _Kept(BLOB_BYTES)
        self._fetched = _Kept(FETCH_BYTES)
        self._joins = _Line()
        self._previews = _Line(turns=True)
        self._recent = _Kept(RECENT_BYTES)  # (server, member) -> (until when, the picture)
        self._drawing: dict[tuple[int, int], asyncio.Future] = {}  # the pictures being drawn right now, so a second ask for one waits for the first
        self._changes: dict[int, int] = {}  # how often each server's settings changed, so a picture drawn from older settings isn't kept
        self._problems: dict[int, str] = {}
        self._skipped: dict[int, tuple[int, float]] = {}  # welcomes that went out with no picture because the queue was full, and until when that is news

    async def on_load(self) -> None:
        for row in await self.db.all("SELECT guild_id, key FROM welcome_images"):
            self._known.setdefault(row["guild_id"], set()).add(row["key"])
        self.bot.settings.listen(self.key, self._settings_changed)
        self._attach()

    async def cog_unload(self) -> None:
        await self.outside.close()

    async def _settings_changed(self, guild_id: int, settings: dict) -> None:
        self._forget(guild_id)

    def _forget(self, guild_id: int) -> None:
        """Drops the pictures kept for a server, and stops one that is being drawn right now from being kept when it is done."""
        self._changes[guild_id] = self._changes.get(guild_id, 0) + 1
        self._recent.drop(lambda key: key[0] == guild_id)

    async def on_guild_restored(self, guild_id: int) -> None:
        rows = await self.db.all("SELECT key FROM welcome_images WHERE guild_id = ?", (guild_id,))
        self._known[guild_id] = {row["key"] for row in rows}
        self._blobs.drop(lambda key: key[0] == guild_id)
        self._forget(guild_id)
        render.CACHE.clear()

    def settings_refusal(self, guild_id: int, before: dict, after: dict) -> dict[str, str] | None:
        """A picture a save newly names has to be one this server uploaded. One named before and removed since doesn't block other changes."""
        known = self._known.get(guild_id, set())
        for field in ("background", "design"):
            if _uploads_named(after, field) - _uploads_named(before, field) - known:
                return {field: "That uploaded picture is gone. Pick another one."}
        return None

    def _note(self, guild_id: int, problem: str | None) -> None:
        """What last went wrong with a picture, for the page. A picture that draws cleanly clears it."""
        if problem is None:
            self._problems.pop(guild_id, None)
        else:
            self._problems[guild_id] = problem

    def _note_skipped(self, guild_id: int) -> None:
        """A welcome went out with no picture because the queue was full. The pictures drawn around it don't clear this, so the page can say a burst outran the drawing."""
        now = time.monotonic()
        count, until = self._skipped.get(guild_id, (0, 0.0))
        self._skipped[guild_id] = ((count if until > now else 0) + 1, now + SKIPPED_SECONDS)

    def problems(self, guild_id: int) -> list[str]:
        """What the page should say went wrong lately, as sentences."""
        out = [self._problems[guild_id]] if guild_id in self._problems else []
        count, until = self._skipped.get(guild_id, (0, 0.0))
        if count and until > time.monotonic():
            out.append(f"{count:,} {'welcome' if count == 1 else 'welcomes'} in the last ten minutes went out with no picture, because more people joined at once than the bot draws pictures for.")
        return out

    # ---- what greetings calls ----

    async def picture_for(self, member: Any, kind: str, scope: Any) -> dict | None:
        """The welcome picture for this member, as {"filename", "data", "in_embed"}, or None when pictures are off for this kind of
        message or one couldn't be made. Never raises, and answers within 15 seconds."""
        try:
            guild_id = member.guild.id
            cfg = self.cfg(guild_id)
            if kind not in KINDS or not cfg["enabled"] or (kind == "dm" and not cfg["dm_image"]):
                return None
            key = (guild_id, member.id)
            recent = self._recent.get(key)
            if recent is not None and recent[0] > time.monotonic():
                return dict(recent[1])
            # The welcome and the join DM ask for the same picture, sometimes at the same moment. One drawing serves both.
            drawing = self._drawing.get(key)
            if drawing is None:
                drawing = self._drawing[key] = asyncio.ensure_future(self._fresh(member, cfg, scope))
                drawing.add_done_callback(lambda done: self._drawing.pop(key, None) if self._drawing.get(key) is done else None)
            result = await asyncio.shield(drawing)
            return dict(result) if result is not None else None
        except Exception:  # a picture is an extra, and no failure here may cost someone their welcome
            log.warning("A welcome picture failed", exc_info=True)
            return None

    async def _fresh(self, member: Any, cfg: dict, scope: Any) -> dict | None:
        """Draws the picture and notes for the page how that went. Never raises, and is done within PICTURE_SECONDS."""
        guild_id = member.guild.id
        try:
            if not isinstance(scope, Scope):
                scope = Scope(guild=member.guild, member=member)
            settings = self._changes.get(guild_id, 0)
            give_up = time.monotonic() + PICTURE_SECONDS
            avatar = str(getattr(getattr(member, "display_avatar", None), "url", "") or "")
            picture = await asyncio.wait_for(
                self._draw(guild_id, cfg, scope, person=member.id, avatar=avatar, pool=budget.MESSAGES, give_up=give_up, max_bytes=self._max_bytes(member.guild)),
                PICTURE_SECONDS,
            )
            result = {"filename": picture.filename, "data": picture.data, "in_embed": bool(cfg["in_embed"])}
            if self._changes.get(guild_id, 0) == settings:
                self._recent.put((guild_id, member.id), (time.monotonic() + RECENT_SECONDS, result), len(picture.data))
            self._note(guild_id, picture.notes[0] if picture.notes else None)
            return result
        except asyncio.TimeoutError:
            self._note(guild_id, "The last welcome picture took too long to draw, so the welcome went out without it.")
        except _Busy:
            self._note_skipped(guild_id)
        except render.RenderError as error:
            self._note(guild_id, f"The last welcome picture wasn't drawn. {error}")
        except Invalid as error:
            self._note(guild_id, f"The stored design can't be drawn. {error}")
        except Exception:
            log.warning("A welcome picture in %s failed", guild_id, exc_info=True)
            self._note(guild_id, "The last welcome picture couldn't be drawn. The bot's log says why.")
        return None

    @staticmethod
    def _max_bytes(guild: Any) -> int:
        return min(render.DEFAULT_MAX_BYTES, upload_limit(guild))

    def design_for(self, cfg: dict) -> dict:
        """The design these settings draw: the stored one in designer mode, or the one their layout builds. Raises Invalid for a stored design this version can't draw."""
        if cfg["mode"] == "design":
            return model.clean_design(cfg["design"], fonts.KEYS)
        return layouts.build(cfg)

    # ---- gathering and drawing ----

    async def _upload(self, guild_id: int, key: str) -> render.Raw | None:
        """One of this server's uploads as bytes. Another server's key finds nothing."""
        held = self._blobs.get((guild_id, key))
        if held is not None:
            return held
        row = await self.db.one("SELECT image FROM welcome_images WHERE guild_id = ? AND key = ?", (guild_id, key))
        if row is None:
            return None
        try:
            data = await asyncio.to_thread(base64.b64decode, row["image"])
        except (binascii.Error, ValueError, TypeError):
            return None
        raw = render.Raw(data, f"{guild_id}:{key}")
        self._blobs.put((guild_id, key), raw, len(data))
        return raw

    async def _fetch(self, address: str | None, *, keep: bool) -> render.Raw | None:
        """A picture from one of Discord's image servers, reused for a few minutes. A slow or failed fetch gives None, and the picture is drawn without it."""
        if not address:
            return None
        now = time.monotonic()
        held = self._fetched.get(address)
        if held is not None and held[0] > now:
            return held[1]
        raw: render.Raw | None = None
        try:
            reply = await self.outside.get(address, stranger=True, max_bytes=images.MAX_BYTES, timeout=FETCH_SECONDS, hosts=CDN_HOSTS)
            if reply.status == 200 and reply.body and (urlsplit(reply.url).hostname or "").lower() in CDN_HOSTS:
                raw = render.Raw(reply.body, address if keep else "")
        except OutsideError as error:
            log.debug("No picture from %s: %s", address, error)
        self._fetched.put(address, (now + (FETCH_KEPT if raw is not None else FETCH_MISS_KEPT), raw), len(raw.data) if raw is not None else MISS_BYTES + len(address))
        return raw

    async def _inputs(self, guild_id: int, cfg: dict, design: dict, filled: template.Filled, *, person: int, avatar: str, max_bytes: int) -> render.Inputs:
        """Everything the renderer needs beside the design: the avatar, this server's uploads, pictures at Discord addresses and server emoji."""
        jobs: dict[str, Any] = {}
        if model.uses_avatar(design):
            # As large as the largest avatar on the picture needs. One that only lends its color is asked for small.
            largest = max((max(layer["w"], layer["h"]) for layer in design["layers"] if layer["type"] == "avatar" and not layer["hidden"]), default=64)
            jobs["avatar"] = self._fetch(cdn_address(avatar, largest), keep=False)
        for upload in sorted(model.uploads_in(design)):
            jobs[f"upload:{upload}"] = self._upload(guild_id, upload)
        sides = {layer["id"]: max(layer["w"], layer["h"]) for layer in design["layers"]}
        for layer_id, address in filled.addresses.items():
            jobs[f"address:{layer_id}"] = self._fetch(cdn_address(address, sides.get(layer_id, 256)), keep=True)
        for found in filled.emoji[:MAX_EMOJI]:
            jobs[f"emoji:{found}"] = self._fetch(f"https://cdn.discordapp.com/emojis/{found}.png?size=128", keep=True)
        names = list(jobs)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        pictures = {name: result for name, result in zip(names, results) if isinstance(result, render.Raw)}
        return render.Inputs(texts=filled.texts, avatar=pictures.pop("avatar", None), pictures=pictures, person=person, animate=bool(cfg["animate"]), max_bytes=max_bytes)

    @property
    def _waiting(self) -> int:
        """Draws running or waiting, joins and previews together."""
        return self._joins.held + self._previews.held

    async def _draw(self, guild_id: int, cfg: dict, scope: Scope, *, person: int, avatar: str, pool: str, give_up: float, max_bytes: int, budget_member: int | None = None) -> render.Picture:
        """Takes a place in the line, then fills in the texts, gathers the pictures and draws. Raises Invalid, _Busy or render.RenderError.
        The place comes first, so a burst fills in and fetches nothing for the pictures it turns away."""
        design = self.design_for(cfg)
        line, workers = self._place(guild_id, preview=pool == budget.PREVIEWS)
        try:
            try:
                filled = await budgeted_render(lambda: template.fill(design, scope), guild_id, pool=pool, member_id=budget_member)
            except OverBudget:
                filled = template.Filled()  # past the server's template time, the texts are drawn as written, as a message goes out as written
            inputs = await self._inputs(guild_id, cfg, design, filled, person=person, avatar=avatar, max_bytes=max_bytes)
        except BaseException:
            line.leave(guild_id)  # nothing was drawn, so the place goes back at once, the way it does when whoever asked gives up while the pictures are fetched
            raise

        def work() -> render.Picture:
            left = give_up - time.monotonic()
            if left <= 0.5:
                raise render.RenderError("The bot was too busy to draw it in time.")
            inputs.seconds = left
            return render.render(design, inputs)
        return await self._in_queue(work, line, workers, guild_id)

    def _place(self, guild_id: int, *, preview: bool) -> tuple[_Line, int]:
        """A place in the joins' line or the previews' line, and how many workers that line has. Raises _Busy when the line is full, or when
        the server already has its share of the previews' line. A server with no preview in that line gets a place even when it is full, so
        no number of busy servers turns another away."""
        line, places, workers = (self._previews, PREVIEW_WAITING, PREVIEW_WORKERS) if preview else (self._joins, MAX_WAITING, WORKERS)
        mine = line.by_server.get(guild_id, 0)
        if (line.held >= places and not (preview and not mine)) or (preview and mine >= PREVIEW_PER_SERVER):
            raise _Busy
        line.take(guild_id)
        return line, workers

    async def _in_queue(self, work: Callable[[], render.Picture], line: _Line, workers: int, guild_id: int) -> render.Picture:
        """Draws on one of the line's workers, with the place already taken. The place goes back when the draw's thread ends."""
        # The draw holds its worker until its thread ends, even when whoever asked has stopped waiting, so no more than the line's workers ever run.
        task = asyncio.ensure_future(self._in_turn(work, line, workers, guild_id))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done: done.cancelled() or done.exception())
            raise

    async def _in_turn(self, work: Callable[[], render.Picture], line: _Line, workers: int, guild_id: int) -> render.Picture:
        try:
            async with line.worker(guild_id, workers):
                return await asyncio.to_thread(work)
        finally:
            line.leave(guild_id)

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/welcomecard/catalog", self._catalog_endpoint, access="staff")
        router.get("/welcomecard/problems", self._problems_endpoint, access="staff")
        router.get("/welcomecard/stock/{name}", self._stock_endpoint, access="staff")
        router.get("/welcomecard/fonts/{name}", self._font_endpoint, access="staff")
        router.get("/welcomecard/layouts/{name}", self._layout_endpoint, access="staff")
        router.post("/welcomecard/preview", self._preview_endpoint)
        router.post("/welcomecard/design", self._design_endpoint)
        router.get("/welcomecard/uploads", self._uploads_endpoint, access="staff")
        router.post("/welcomecard/uploads", self._upload_endpoint)
        router.get("/welcomecard/uploads/{image_id}", self._upload_file_endpoint, access="staff")
        router.delete("/welcomecard/uploads/{image_id}", self._upload_remove_endpoint)

    async def _catalog_endpoint(self, req: Any) -> dict:
        return catalog()

    async def _problems_endpoint(self, req: Any) -> list[str]:
        return self.problems(req.guild_id)

    @staticmethod
    def _png(data: bytes | None) -> web.Response:
        if data is None:
            raise ApiError(404, "There's no picture by that name.")
        return web.Response(body=data, content_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

    async def _stock_endpoint(self, req: Any) -> web.Response:
        return self._png(await asyncio.to_thread(backgrounds.thumbnail, req.param("name")))

    async def _font_endpoint(self, req: Any) -> web.Response:
        return self._png(await asyncio.to_thread(previews.font_sample, req.param("name")))

    async def _layout_endpoint(self, req: Any) -> web.Response:
        return self._png(await asyncio.to_thread(previews.layout_thumbnail, req.param("name")))

    async def _drafted(self, req: Any) -> dict:
        """The saved settings with the page's unsaved changes over them, checked the way a save checks them."""
        form = await req.json()
        draft = form.get("draft", {})
        if not isinstance(draft, dict):
            raise ValidationError({"draft": "Expected a set of fields."})
        return self.schema.clean(draft, base=self.cfg(req.guild_id))

    async def _viewer(self, req: Any) -> tuple[Scope, str]:
        """The values and the avatar of whoever is looking at the page, standing in for someone who just joined."""
        guild = self.bot.get_guild(req.guild_id)
        member = guild.get_member(req.user.id) if guild is not None else None
        if member is not None:
            return await self.scope_of(member), str(getattr(member.display_avatar, "url", "") or "")
        row = req.ops.member(req.guild_id, req.user.id) or {}
        return preview_scope(req.ops, req.guild_id, req.user.id), str(row.get("avatar") or req.user.avatar or "")

    async def scope_of(self, member: Any) -> Scope:
        """What a welcome for this member could show, with sample values where only a real join has them. Greetings builds it when it can."""
        greetings = self.bot.module("greetings")
        build = getattr(greetings, "scope_for", None)
        if build is not None:
            try:
                scope = await build(member, channel=None, preview=True)
                if isinstance(scope, Scope):
                    return scope
            except Exception:
                log.warning("Greetings couldn't build a preview's values", exc_info=True)
        return Scope(guild=member.guild, member=member)

    async def _preview_endpoint(self, req: Any) -> dict:
        """The picture these settings draw, for the person looking at the page. Nothing is saved or posted."""
        cfg = await self._drafted(req)
        scope, avatar = await self._viewer(req)
        guild = self.bot.get_guild(req.guild_id)
        try:
            picture = await asyncio.wait_for(
                self._draw(req.guild_id, cfg, scope, person=req.user.id, avatar=avatar, pool=budget.PREVIEWS, budget_member=req.user.id, give_up=time.monotonic() + PREVIEW_SECONDS,
                           max_bytes=self._max_bytes(guild)),
                PREVIEW_SECONDS,
            )
        except asyncio.TimeoutError:
            raise ApiError(503, "The preview took too long to draw. Try again in a moment.") from None
        except _Busy:
            raise ApiError(503, "The bot is busy drawing welcome pictures. Try again in a moment.") from None
        except render.RenderError as error:
            raise ApiError(422, str(error)) from None
        except Invalid as error:
            raise ValidationError({"design": str(error)}) from None
        design = self.design_for(cfg)

        def answer() -> web.Response:
            """The picture as text in a JSON answer. A GIF can weigh 8 MiB, so this runs on a worker thread."""
            return json_response({
                "image": f"data:image/{picture.kind};base64,{base64.b64encode(picture.data).decode('ascii')}",
                "kind": picture.kind, "width": picture.width, "height": picture.height, "frames": picture.frames, "bytes": len(picture.data), "notes": picture.notes,
                "sizes": picture.sizes, "canvas": {"width": design["width"], "height": design["height"], "frame": design["frame"]["width"]},
            })
        return await asyncio.to_thread(answer)

    async def _design_endpoint(self, req: Any) -> dict:
        """The design the current layout and its settings make, for the designer to start from. Nothing is saved."""
        cfg = await self._drafted(req)
        return {"design": layouts.build(cfg)}

    def _row_view(self, guild_id: int, row: dict) -> dict:
        address = f"/api/g/{guild_id}/welcomecard/uploads/{row['key']}"
        return {"id": row["key"], "name": row["name"], "width": row["width"], "height": row["height"], "frames": row["frames"], "animated": row["frames"] > 1, "size": row["size"],
                "created_at": row["created_at"], "url": address, "preview": f"{address}?preview=1"}

    async def _uploads_endpoint(self, req: Any) -> dict:
        rows = await self.db.all("SELECT key, name, width, height, frames, size, created_at FROM welcome_images WHERE guild_id = ? ORDER BY created_at DESC, key", (req.guild_id,))
        return {"uploads": [self._row_view(req.guild_id, row) for row in rows], "used": sum(row["size"] for row in rows), "most": MAX_UPLOADS, "room": MAX_UPLOAD_TOTAL}

    async def store_upload(self, guild_id: int, data: bytes, name: str) -> dict:
        """Check a file and keep it as one of this server's pictures. Raises Invalid when the file is refused and ApiError when the server is out of room."""
        prepared = await asyncio.to_thread(uploads.prepare, data)
        key = secrets.token_hex(8)
        row = {"key": key, "name": name, "width": prepared.width, "height": prepared.height, "frames": prepared.frames, "size": len(prepared.data), "created_at": time.time()}
        encoded, small = await asyncio.to_thread(lambda: (base64.b64encode(prepared.data).decode("ascii"), base64.b64encode(prepared.preview).decode("ascii")))
        # Uploads sent at the same moment take turns from the count to the insert, so each one counts the pictures stored before it.
        async with self._storing.setdefault(guild_id, asyncio.Lock()):
            held = await self.db.one("SELECT COUNT(*) AS pictures, COALESCE(SUM(size), 0) AS bytes FROM welcome_images WHERE guild_id = ?", (guild_id,))
            if held["pictures"] >= MAX_UPLOADS:
                raise ApiError(409, f"A server can keep {MAX_UPLOADS} uploaded pictures. Remove one first.")
            if held["bytes"] + len(prepared.data) > MAX_UPLOAD_TOTAL:
                raise ApiError(409, f"This server's uploaded pictures can take {MAX_UPLOAD_TOTAL // 1048576} MB in all, and this one doesn't fit. Remove one first.")
            await self.db.run(
                "INSERT INTO welcome_images (guild_id, key, name, kind, width, height, frames, size, image, preview, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, key, name, prepared.kind, prepared.width, prepared.height, prepared.frames, len(prepared.data), encoded, small, row["created_at"]),
            )
            self._known.setdefault(guild_id, set()).add(key)
        return self._row_view(guild_id, row)

    async def _upload_endpoint(self, req: Any) -> dict:
        files, fields = await images.read_form(req.request)
        data = files.get("image")
        if data is None:
            raise ApiError(400, "Pick an image first.")
        name = " ".join((fields.get("name") or "").split())[:MAX_NAME] or "Picture"
        row = await self.store_upload(req.guild_id, data, name)
        await req.record("welcomecard.upload", detail=name)
        return row

    @staticmethod
    def _key(req: Any) -> str:
        key = req.param("image_id")
        if not _KEY.fullmatch(key):
            raise ApiError(404, "That picture is gone.")
        return key

    async def _upload_file_endpoint(self, req: Any) -> web.Response:
        small = req.query.get("preview") == "1"
        row = await self.db.one(f"SELECT {'preview' if small else 'image'} AS body, kind FROM welcome_images WHERE guild_id = ? AND key = ?", (req.guild_id, self._key(req)))
        # A restored backup can hold any type here, and a page or script served from this site would run as this site.
        if row is None or row["kind"] not in images.MIMES.values():
            raise ApiError(404, "That picture is gone.")
        try:
            body = await asyncio.to_thread(base64.b64decode, row["body"])
        except (binascii.Error, ValueError, TypeError):
            raise ApiError(404, "That picture is gone.") from None
        return web.Response(body=body, content_type="image/jpeg" if small else row["kind"], headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})

    async def _upload_remove_endpoint(self, req: Any) -> dict:
        key = self._key(req)
        row = await self.db.one("SELECT name FROM welcome_images WHERE guild_id = ? AND key = ?", (req.guild_id, key))
        if row is None:
            raise ApiError(404, "That picture is gone.")
        cfg = self.cfg(req.guild_id)
        if key in _uploads_named(cfg, "background") | _uploads_named(cfg, "design"):
            raise ApiError(409, "The welcome picture uses that one. Pick another background or remove it from the design, save, and then remove it here.")
        await self.db.run("DELETE FROM welcome_images WHERE guild_id = ? AND key = ?", (req.guild_id, key))
        self._known.get(req.guild_id, set()).discard(key)
        self._blobs.drop(lambda held: held == (req.guild_id, key))
        await req.record("welcomecard.upload_remove", detail=row["name"])
        return {"ok": True}

    # ---- /greetings image ----

    def _attach(self) -> None:
        """/greetings belongs to the greetings module, so these join that group once every module has loaded. Without greetings there is no group to join."""
        parent = self.bot.get_command("greetings")
        if parent is None or parent.get_command("image") is not None:
            return
        for name, callback, description in (
            ("image", WelcomeCard._image, "Switch the welcome picture on or off"),
            ("image_dm", WelcomeCard._image_dm, "Switch the welcome picture in the join DM on or off"),
            ("image_preview", WelcomeCard._image_preview, "See the welcome picture as it would be drawn for a member. Only you see it."),
        ):
            command = commands.HybridCommand(callback, name=name, description=description)
            command.cog = self
            parent.add_command(command)

    async def _switch(self, ctx: commands.Context, field: str, on: bool) -> bool:
        try:
            await self.bot.settings.save(ctx.guild.id, self.key, {field: on}, actor=self.bot.actor(ctx.author), action=f"welcomecard.{field}")
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values()), "That can't be saved."))
            return False
        return True

    @requires("manage_guild")
    @app_commands.describe(state="On sends the picture with each welcome message, and off stops it")
    async def _image(self, ctx: commands.Context, state: Literal["on", "off"]) -> None:
        if not await self._switch(ctx, "enabled", state == "on"):
            return
        if state == "off":
            await ctx.ok("Welcome pictures are off.")
            return
        await ctx.ok(f"Welcome pictures are on. Each welcome message carries one. Change how it looks at {self.bot.public_url}/dashboard/{ctx.guild.id}/welcomecard")

    @requires("manage_guild")
    @app_commands.describe(state="On puts the picture in the join DM too, and off leaves the DM without it")
    async def _image_dm(self, ctx: commands.Context, state: Literal["on", "off"]) -> None:
        if not await self._switch(ctx, "dm_image", state == "on"):
            return
        if state == "off":
            await ctx.ok("The join DM goes out without the welcome picture.")
        elif self.cfg(ctx.guild.id)["enabled"]:
            await ctx.ok("The join DM now carries the welcome picture.")
        else:
            await ctx.ok("The join DM carries the welcome picture once welcome pictures are on. Switch them on with `/greetings image on`.")

    @requires("manage_guild")
    @commands.cooldown(3, 20, commands.BucketType.user)
    @app_commands.describe(member="Whose picture to draw. Defaults to you.")
    async def _image_preview(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if ctx.interaction is None and getattr(getattr(ctx, "decision", None), "muted", False):
            return  # a typed command the server's rules mute answers nowhere, so nothing is drawn for it
        await ctx.defer(ephemeral=True)
        target = member or ctx.author
        cfg = self.cfg(ctx.guild.id)
        try:
            picture = await asyncio.wait_for(
                self._draw(ctx.guild.id, cfg, await self.scope_of(target), person=target.id, avatar=str(getattr(getattr(target, "display_avatar", None), "url", "") or ""), pool=budget.PREVIEWS,
                           budget_member=ctx.author.id, give_up=time.monotonic() + PICTURE_SECONDS, max_bytes=self._max_bytes(ctx.guild)),
                PICTURE_SECONDS,
            )
        except asyncio.TimeoutError:
            await ctx.fail("The picture took too long to draw. Try again in a moment.")
            return
        except _Busy:
            await ctx.fail("The bot is busy drawing welcome pictures. Try again in a moment.")
            return
        except (render.RenderError, Invalid) as error:
            await ctx.fail(f"The picture wasn't drawn. {error}")
            return
        words = "This is the welcome picture for " + ("you." if target.id == ctx.author.id else f"{target.mention}.")
        if not cfg["enabled"]:
            words += " Welcome pictures are off, so nobody gets one yet. Switch them on with `/greetings image on`."
        file = discord.File(io.BytesIO(picture.data), filename=picture.filename)
        if ctx.interaction is not None:
            # A slash command answers where only the person asking sees it, past any redirect the server's rules set, which would post the picture for everyone.
            send = commands.Context.send if isinstance(ctx, commands.Context) else type(ctx).send
            await send(ctx, words, file=file, ephemeral=True, allowed_mentions=NO_PINGS)
            return
        try:
            await ctx.author.send(words, file=file, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            await ctx.fail("Your DMs are closed, so the preview couldn't reach you. Use `/greetings image_preview` as a slash command, or open the Welcome image page on the dashboard.")
            return
        await ctx.ok("Sent the preview to your DMs.")

    # ---- demo ----

    async def seed_demo(self, guild_id: int) -> None:
        """A picture set up and one uploaded background, so the demo's page opens on something."""
        sample = backgrounds.draw("waves", 800, 240)
        out = io.BytesIO()
        sample.save(out, format="PNG")
        await self.store_upload(guild_id, out.getvalue(), "Sample waves")
        await self.bot.settings.save(guild_id, self.key, {"enabled": True, "layout": "banner", "background": {"kind": "stock", "name": "aurora"}})
