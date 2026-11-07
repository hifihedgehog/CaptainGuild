"""The dashboard on sample data, with no Discord connection. Used for trying pages out and by the browser checks.

DemoOps stands in for core.ops.DiscordOps. It keeps posted messages in memory, so editing a message works end to end here.
"""
from __future__ import annotations

import itertools
import json
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from aiohttp import web

from .bot import Bot
from .core import messages
from .core.ops import MessageGone, OpsError
from .core.outside import OutsideError, Reply
from .core.settings import Actor
from .db import Database
from .modules import load_all
from .web.api import User
from .web.app import build_app

DEMO_KEY = "0" * 64
MAX_DEMO_UPLOAD = 64 * 1024 * 1024
GUILD = 900000000000000001
ME = 900000000000000002
USER = 900000000000000003
OWNER_ROLE, MOD_ROLE, VET_ROLE, MEMBER_ROLE, MUTED_ROLE, EVERYONE = (900000000000000100 + i for i in range(6))


def _channel(index: int, name: str, kind: int, parent: int | None = None, nsfw: bool = False) -> dict:
    return {"id": 900000000000001000 + index, "name": name, "type": kind, "parent_id": parent, "position": index, "nsfw": nsfw}


def _sample_channels() -> list[dict]:
    rows = []
    groups = [
        ("Community", ["welcome", "rules", "announcements", "general", "introductions", "roles"]),
        ("Support", ["help-desk", "bug-reports", "suggestions"]),
        ("Staff", ["mod-chat", "reports", "mod-log"]),
        ("Logs", ["message-log", "member-log", "join-leave-log", "server-log", "voice-log"]),
    ]
    index = 0
    for group, names in groups:
        category = _channel(index, group, 4)
        rows.append(category)
        index += 1
        for name in names:
            rows.append(_channel(index, name, 0, category["id"]))
            index += 1
    voice = _channel(index, "Voice", 4)
    rows.append(voice)
    index += 1
    for name in ("Lounge", "Gaming"):
        rows.append(_channel(index, name, 2, voice["id"]))
        index += 1
    rows.append(_channel(index, "help-forum", 15))
    rows.append(_channel(index + 1, "starboard", 0))
    return rows


def _sample_roles() -> list[dict]:
    def role(role_id: int, name: str, color: int, position: int, permissions: list[str], *, default: bool = False, members: int = 0) -> dict:
        return {"id": role_id, "name": name, "color": color, "position": position, "managed": False, "default": default, "permissions": permissions, "members": members}

    return [
        role(OWNER_ROLE, "Admin", 0xE74C3C, 6, ["administrator"], members=2),
        role(ME, "Captain Guild", 0x00A2F8, 5, ["administrator"], members=1),
        role(MOD_ROLE, "Moderator", 0x3498DB, 4, ["manage_messages", "moderate_members"], members=8),
        role(VET_ROLE, "Server Veteran", 0x2ECC71, 3, [], members=20),
        role(MEMBER_ROLE, "Member", 0x95A5A6, 2, [], members=4000),
        role(MUTED_ROLE, "Muted", 0x4F545C, 1, [], members=3),
        role(EVERYONE, "@everyone", 0, 0, [], default=True, members=4200),
    ]


class DemoOps:
    DEMO_GUILD_ID = GUILD

    def __init__(self) -> None:
        self.channels = _sample_channels()
        self.roles = _sample_roles()
        self.messages: dict[int, dict] = {}
        self.ids = itertools.count(900000000000100000)
        self.member_changes: list[tuple] = []
        self.in_voice: set[int] = set()
        self.members = [
            {"id": USER, "name": "demo_admin", "display_name": "Demo Admin", "avatar": "/static/img/logo.svg", "bot": False,
             "joined_at": time.time() - 90000000, "created_at": time.time() - 190000000, "role_ids": [OWNER_ROLE, MEMBER_ROLE]},
            {"id": USER + 1, "name": "sample_member", "display_name": "Sample Member", "avatar": "/static/img/logo.svg", "bot": False,
             "joined_at": time.time() - 900000, "created_at": time.time() - 90000000, "role_ids": [MEMBER_ROLE]},
            {"id": USER + 2, "name": "StatCounter", "display_name": "StatCounter", "avatar": "/static/img/logo.svg", "bot": True,
             "joined_at": time.time() - 9000000, "created_at": time.time() - 90000000, "role_ids": []},
            {"id": USER + 3, "name": "MusicBot", "display_name": "MusicBot", "avatar": "/static/img/logo.svg", "bot": True,
             "joined_at": time.time() - 8000000, "created_at": time.time() - 80000000, "role_ids": []},
        ]
        # The other people in the sample log entries (logs.seed_demo uses these IDs), so a mention of one reads as a name.
        for index, name in enumerate(("printer_fan", "filament_friend", "layer_lover", "nozzle_nerd"), start=1):
            self.members.append({"id": 900000000000000300 + index, "name": name, "display_name": name.replace("_", " ").title(), "avatar": "/static/img/logo.svg", "bot": False,
                                 "joined_at": time.time() - 5000000, "created_at": time.time() - 90000000, "role_ids": [MEMBER_ROLE]})

    def guild_ids(self) -> list[int]:
        return [GUILD]

    def guild_summary(self, guild_id: int) -> dict | None:
        return {"id": GUILD, "name": "Demo Server", "icon": None} if guild_id == GUILD else None

    def describe_guild(self, guild_id: int) -> dict | None:
        if guild_id != GUILD:
            return None
        return {
            "id": GUILD, "name": "Demo Server", "icon": None, "member_count": 4200, "owner_id": USER,
            "channels": self.channels, "roles": self.roles,
            "emojis": [{"id": 900000000000002000, "name": "party", "animated": False, "url": "/static/img/logo.svg", "available": True}],
            "me": {"id": ME, "name": "Captain Guild", "nick": None, "avatar": "/static/img/logo.svg", "top_role_position": 5, "permissions": ["administrator"]},
        }

    def access_for(self, guild_id: int, user_id: int) -> str | None:
        return "full" if guild_id == GUILD else None

    def member(self, guild_id: int, user_id: int) -> dict | None:
        return next((m for m in self.members if m["id"] == user_id), None)

    def search_members(self, guild_id: int, query: str, limit: int = 25) -> list[dict]:
        needle = query.strip().lower()
        return [m for m in self.members if needle in m["name"] or needle in m["display_name"].lower() or needle == str(m["id"])][:limit]

    def list_bots(self, guild_id: int, limit: int = 200) -> list[dict]:
        return [m for m in self.members if m["bot"]][:limit]

    async def create_private_category(self, guild_id: int, category: str, names: list[str], *, viewer_roles: list[int] | None = None) -> dict:
        parent = _channel(100 + len(self.channels), category, 4)
        self.channels.append(parent)
        channels: dict[str, int] = {}
        for name in names:
            row = _channel(100 + len(self.channels), name, 0, parent["id"])
            self.channels.append(row)
            channels[name] = row["id"]
        return {"category_id": parent["id"], "channels": channels}

    def add_sample_posts(self) -> None:
        """Posts from another bot, so reposting can be tried here. Discord only lets a bot edit its own messages, and so does this."""
        channels = {c["name"]: c["id"] for c in self.channels}
        now = time.time()
        samples = [
            ("welcome", {"content": "", "embeds": [{"title": "Welcome to the Demo Server", "description": "Read the rules, say hello, and pick your roles.", "color": 0x3498DB}], "buttons": []}),
            ("rules", {"content": "", "embeds": [{"title": "Server rules", "description": "1. Be kind.\n2. Stay on topic.\n3. No spam.", "color": 0xE74C3C, "footer": {"text": "Last updated in March"}}], "buttons": []}),
            ("roles", {"content": "**Pick your colors**", "embeds": [{"description": "React below to pick a color role.", "color": 0x2ECC71}], "buttons": []}),
            ("roles", {"content": "", "embeds": [{"title": "Pings", "description": "React to get pinged for announcements.", "color": 0xF1C40F}], "buttons": [{"label": "Read more", "url": "https://example.com/roles"}]}),
        ]
        for index, (name, payload) in enumerate(samples):
            self.messages[next(self.ids)] = {
                "channel_id": channels[name], "payload": payload, "created_at": now - 86400 * (30 - index), "mine": False,
                "author_id": 900000000000000900, "author_name": "Another Bot", "author_bot": True,
                "reactions": [{"emoji": "\N{WHITE HEAVY CHECK MARK}", "count": 12, "me": False}] if name == "roles" else [],
            }

    def channel(self, guild_id: int, channel_id: int) -> dict | None:
        return next((c for c in self.channels if c["id"] == channel_id), None)

    def _message_row(self, message_id: int, stored: dict) -> dict:
        mine = stored.get("mine", True)
        return {
            "id": message_id, "channel_id": stored["channel_id"], "author_id": stored.get("author_id", ME),
            "author_name": stored.get("author_name", "Captain Guild"), "author_bot": stored.get("author_bot", True), "mine": mine, "system": False,
            "created_at": stored["created_at"], "url": f"https://discord.com/channels/{GUILD}/{stored['channel_id']}/{message_id}",
            "payload": stored["payload"], "reactions": stored.get("reactions", []), "attachments": stored.get("attachments", []),
            "controls": stored.get("controls", 0), "stickers": stored.get("stickers", 0),
        }

    async def channel_messages(self, guild_id: int, channel_id: int, *, limit: int = 50, before: int | None = None) -> dict:
        if self.channel(guild_id, channel_id) is None:
            raise OpsError("That channel doesn't exist or the bot can't see it.")
        found = sorted((i for i, m in self.messages.items() if m["channel_id"] == channel_id and (before is None or i < before)), reverse=True)
        return {"messages": [self._message_row(i, self.messages[i]) for i in found[:limit]], "can_delete": True, "can_post": True}

    async def find_thread(self, guild_id: int, channel_id: int) -> bool:
        return any(c["id"] == channel_id for c in self.channels)

    def known_thread(self, guild_id: int, channel_id: int) -> Any:
        return None

    async def send_message(self, channel_id: int, payload: dict, *, ping: bool = False, silent: bool = False, components: list[dict] | None = None, files: list[tuple[str, bytes]] | None = None) -> dict:
        if not any(c["id"] == channel_id for c in self.channels):
            raise OpsError("That channel doesn't exist or the bot can't see it.")
        message_id = next(self.ids)
        self.messages[message_id] = {"channel_id": channel_id, "payload": payload, "created_at": time.time(), "components": components or []}
        return {"id": message_id, "channel_id": channel_id, "url": f"https://discord.com/channels/{GUILD}/{channel_id}/{message_id}"}

    async def start_thread(self, channel_id: int, message_id: int, name: str) -> dict:
        if message_id not in self.messages:
            raise MessageGone("That message doesn't exist.")
        return {"id": next(self.ids), "name": name[:100]}

    async def repost_message(self, source_channel_id: int, source_message_id: int, channel_id: int, payload: dict) -> dict:
        if source_message_id not in self.messages:
            raise MessageGone("The original message no longer exists.")
        sent = await self.send_message(channel_id, payload, silent=True)
        return {**sent, "payload": payload, "skipped": []}

    async def send_dm(self, user_id: int, payload: dict) -> dict:
        return {"id": next(self.ids)}

    async def create_channel(self, guild_id: int, name: str, *, read_only: bool = False, private: bool = False, viewer_roles: Iterable[int] = ()) -> dict:
        row = _channel(100 + len(self.channels), name.strip().lower().replace(" ", "-"), 0)
        self.channels.append(row)
        return row

    async def create_role(self, guild_id: int, name: str, *, color: int = 0) -> dict:
        row = {"id": next(self.ids), "name": name, "color": color, "position": 1, "managed": False, "default": False, "permissions": [], "members": 0}
        self.roles.insert(len(self.roles) - 1, row)
        return row

    async def create_mute_role(self, guild_id: int, name: str = "Muted") -> dict:
        return await self.create_role(guild_id, name)

    async def edit_message(self, channel_id: int, message_id: int, payload: dict, *, components: list[dict] | None = None, current: dict | None = None) -> None:
        if message_id not in self.messages:
            raise MessageGone("That message no longer exists.")
        if not self.messages[message_id].get("mine", True):
            raise OpsError("Discord only lets a bot edit its own messages, and this one has another author. Repost it with the bot instead.")
        self.messages[message_id]["payload"] = payload
        if components is not None:
            self.messages[message_id]["components"] = components

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        self.messages.pop(message_id, None)

    async def fetch_message(self, channel_id: int, message_id: int) -> dict:
        stored = self.messages.get(message_id)
        if stored is None:
            raise MessageGone("That message doesn't exist.")
        return self._message_row(message_id, stored)

    def _reaction(self, message_id: int, emoji: str, *, create: bool = False) -> dict | None:
        stored = self.messages.get(message_id)
        if stored is None:
            return None
        rows = stored.setdefault("reactions", [])
        wanted = emoji.replace("\ufe0f", "")
        row = next((r for r in rows if r["emoji"].replace("\ufe0f", "") == wanted), None)
        if row is None and create:
            row = {"emoji": emoji, "count": 0, "me": False}
            rows.append(row)
        return row

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        row = self._reaction(message_id, emoji, create=True)
        if row is not None and not row["me"]:
            row["me"] = True
            row["count"] += 1

    def react_as(self, message_id: int, emoji: str, user_id: int, *, bot: bool = False) -> None:
        """A person's reaction, as Discord would hold it."""
        row = self._reaction(message_id, emoji, create=True)
        if row is None:
            return
        row["count"] += 1
        self.messages[message_id].setdefault("reactors", {}).setdefault(row["emoji"], []).append({"id": user_id, "bot": bot})

    async def remove_reaction(self, channel_id: int, message_id: int, emoji: str, user_id: int | None = None) -> None:
        row = self._reaction(message_id, emoji)
        if row is None:
            return
        stored = self.messages[message_id]
        if user_id is None:
            if row["me"]:
                row["me"] = False
                row["count"] -= 1
        else:
            people = stored.get("reactors", {}).get(row["emoji"], [])
            if any(p["id"] == user_id for p in people):
                people[:] = [p for p in people if p["id"] != user_id]
                row["count"] -= 1
        if row["count"] <= 0:
            stored["reactions"].remove(row)

    async def clear_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        row = self._reaction(message_id, emoji)
        if row is not None:
            self.messages[message_id]["reactions"].remove(row)
            self.messages[message_id].get("reactors", {}).pop(row["emoji"], None)

    async def clear_reactions(self, channel_id: int, message_id: int) -> None:
        stored = self.messages.get(message_id)
        if stored is not None:
            stored["reactions"] = []
            stored["reactors"] = {}

    async def reaction_users(self, channel_id: int, message_id: int, emoji: str, *, after: int | None = None, limit: int = 100, burst: bool = False) -> list[dict]:
        if message_id not in self.messages:
            raise MessageGone("That message or reaction no longer exists.")
        row = self._reaction(message_id, emoji)
        if row is None or burst:
            return []
        people = sorted(self.messages[message_id].get("reactors", {}).get(row["emoji"], []), key=lambda p: p["id"])
        return [p for p in people if after is None or p["id"] > after][:limit]

    async def set_nickname(self, guild_id: int, nick: str | None) -> None:
        pass

    async def set_bot_profile(self, guild_id: int, *, avatar: Any = ..., banner: Any = ..., bio: Any = ...) -> None:
        pass

    async def ban_list(self, guild_id: int, limit: int = 1000) -> list[dict]:
        return []

    async def add_member_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str | None = None) -> None:
        self.member_changes.append(("add_role", user_id, role_id))

    async def remove_member_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str | None = None) -> None:
        self.member_changes.append(("remove_role", user_id, role_id))

    async def edit_member_voice(self, guild_id: int, user_id: int, *, mute: bool | None = None, deafen: bool | None = None, disconnect: bool = False, reason: str | None = None) -> bool:
        if user_id not in self.in_voice:
            return False
        self.member_changes.append(("voice", user_id, mute, deafen, disconnect))
        return True

    async def open_review(self, guild_id: int, user_id: int, report_id: int | None = None) -> dict:
        self.member_changes.append(("review", user_id, report_id))
        return {"thread_id": 900000000000400001, "url": f"https://discord.com/channels/{guild_id}/900000000000400001"}


class DemoOutside:
    """Stands in for core.outside.Outside, so the demo never leaves this machine and the pages that look things up still work."""

    CHANNEL = "UCdemo000000000000000000"

    def _answer(self, url: str, params: Any) -> Reply:
        def as_json(data: Any) -> Reply:
            return Reply(200, json.dumps(data).encode())

        if url.startswith("https://id.twitch.tv/"):
            return as_json({"access_token": "demo", "expires_in": 86400})
        if url.endswith("/helix/search/channels"):
            return as_json({"data": [
                {"id": "2001", "broadcaster_login": "samplegamer", "display_name": "SampleGamer", "is_live": True, "thumbnail_url": "/static/img/logo.svg"},
                {"id": "2002", "broadcaster_login": "samplespeedruns", "display_name": "SampleSpeedruns", "is_live": False, "thumbnail_url": "/static/img/logo.svg"},
            ]})
        if url.endswith("/helix/users"):
            login = dict(params)["login"]
            return as_json({"data": [{"id": "2001" if login == "samplegamer" else "2002", "login": login, "display_name": login.title()}]})
        if url.endswith("/helix/streams"):
            return as_json({"data": []})
        if url.startswith("https://www.youtube.com/feeds"):
            return Reply(404)
        if url.startswith("https://www.youtube.com/oembed"):
            return as_json({"author_name": "Sample Maker", "author_url": "https://www.youtube.com/@samplemaker"})
        if url.startswith("https://www.youtube.com/"):
            data = {"metadata": {"channelMetadataRenderer": {"title": "Sample Maker", "externalId": self.CHANNEL}}}
            page = f'<meta property="og:title" content="Sample Maker"><script>"externalId":"{self.CHANNEL}"</script><script>var ytInitialData = {json.dumps(data)};</script>'
            return Reply(200, page.encode())
        raise OutsideError("The demo doesn't call outside services.")

    async def get(self, url: str, *, params: Any = None, **kwargs: Any) -> Reply:
        return self._answer(url, params)

    async def post(self, url: str, **kwargs: Any) -> Reply:
        return self._answer(url, None)

    async def close(self) -> None:
        pass


@web.middleware
async def _local_only(request: web.Request, handler: Any) -> web.StreamResponse:
    """The demo runs on a local port with nobody signed in, so any page open in the same browser could post to it. It takes writes only from its own pages, and small ones."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        fetched_from = request.headers.get("Sec-Fetch-Site")
        origin = request.headers.get("Origin")
        same_site = fetched_from in ("same-origin", "none") if fetched_from else (not origin or urlsplit(origin).netloc == request.host)
        if not same_site:
            return web.json_response({"error": "That request didn't come from this site.", "fields": {}}, status=403)
        if (request.content_length or 0) > MAX_DEMO_UPLOAD:
            return web.json_response({"error": "The demo takes files up to 64 MB.", "fields": {}}, status=413)
    return await handler(request)


async def _seed_samples(bot: Bot) -> None:
    """Sample rows for the pages whose features add none of their own, so no page opens empty. Nothing here runs: there's no Discord to act on."""
    channels = {row["name"]: row["id"] for row in bot.ops.channels}
    now = time.time()
    db = bot.db

    async def post(place: str, payload: dict, source: str = "dashboard") -> tuple[int, str]:
        sent = await bot.ops.send_message(channels[place], payload)
        await db.run(
            "INSERT INTO posted_messages (guild_id, channel_id, message_id, summary, search, source, posted_by, posted_by_name, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (GUILD, channels[place], sent["id"], messages.summary(payload), messages.searchable(payload), source, USER, "Demo Admin", now - 3600),
        )
        return sent["id"], messages.summary(payload)

    await db.run("INSERT INTO autopurge_channels (guild_id, channel_id, interval_seconds, message_type, next_run) VALUES (?, ?, ?, ?, ?)", (GUILD, channels["bug-reports"], 86400, "bots", now + 86400))
    notes = messages.clean_payload({"content": "Patch notes are up in the help desk."})
    await post("announcements", notes)
    planned = messages.clean_payload({"content": "Maintenance starts on Saturday."})
    await db.run(
        "INSERT INTO scheduled_posts (guild_id, channel_id, payload, summary, due_at, created_by, created_by_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (GUILD, channels["announcements"], json.dumps(planned), messages.summary(planned), now + 2 * 86400, USER, "Demo Admin", now),
    )
    await db.run("INSERT INTO birthdays (guild_id, user_id, month, day, updated_at) VALUES (?, ?, 6, 15, ?)", (GUILD, USER, now))
    colors, _ = await post("roles", messages.clean_payload({"content": "Pick a role below."}))
    await db.run("INSERT INTO rr_messages (message_id, guild_id, channel_id, created_at) VALUES (?, ?, ?, ?)", (colors, GUILD, channels["roles"], now))
    await db.run(
        "INSERT INTO rr_pairs (guild_id, message_id, slot, emoji, emoji_key, role_id, label) VALUES (?, ?, 0, ?, ?, ?, ?)",
        (GUILD, colors, "\N{BELL}", "\N{BELL}", MEMBER_ROLE, "Member"),
    )
    await db.run(
        "INSERT INTO starboard_entries (guild_id, message_id, channel_id, author_id, stars, star_channel_id, star_message_id, preview, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (GUILD, 900000000000002001, channels["general"], USER, 7, channels["general"], 900000000000002002, "The new map looks great.", now - 86400),
    )
    pinned = messages.clean_payload({"content": "Read the pinned guide before asking."})
    await db.run(
        "INSERT INTO sticky_messages (guild_id, channel_id, payload, summary, created_by, created_by_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (GUILD, channels["help-desk"], json.dumps(pinned), messages.summary(pinned), USER, "Demo Admin", now),
    )
    await db.run(
        "INSERT INTO suggestions (guild_id, number, author_id, author_name, anonymous, content, channel_id, up, down, created_at) VALUES (?, 1, ?, ?, 0, ?, ?, 5, 1, ?)",
        (GUILD, USER, "Demo Admin", "Add a channel for screenshots.", channels["suggestions"], now - 7200),
    )
    await db.run("INSERT INTO tags (guild_id, name, content, owner_id, created_at, updated_at) VALUES (?, 'rules', 'Read the rules channel first.', ?, ?, ?)", (GUILD, USER, now, now))
    # The settings those rows belong to, so each page opens on its form and not on a "set this up" card.
    admin = Actor(USER, "Demo Admin")
    for module, form in (
        ("suggestions", {"channel_id": channels["suggestions"]}),
        ("starboard", {"channel_id": channels["starboard"]}),
        ("moderation", {"modlog_channel": channels["mod-log"], "mute_role": MUTED_ROLE}),
        ("voicelink", {"links": [{"channel_id": channels["Lounge"], "roles": [VET_ROLE]}]}),
        ("levels", {"rewards": [{"level": 5, "role_id": MEMBER_ROLE}, {"level": 20, "role_id": VET_ROLE}]}),
        ("proposals", {"channel_id": channels["announcements"], "moderator_role": MOD_ROLE}),
    ):
        await bot.settings.save(GUILD, module, form, actor=admin)
    await db.run("UPDATE activity SET user_id = ?, user_name = ? WHERE guild_id = ? AND user_id IS NULL", (USER, "Demo Admin", GUILD))
    await db.run("INSERT INTO triggers (guild_id, match_type, phrase, content, created_by, created_at, updated_at) VALUES (?, 0, 'server ip', 'The address is in the welcome channel.', ?, ?, ?)", (GUILD, USER, now, now))


async def build_demo(data_dir: Path) -> tuple[Bot, Any, list]:
    db = Database(data_dir / "demo-main.db", DEMO_KEY)
    message_db = Database(data_dir / "demo-messages.db", DEMO_KEY)
    await db.open()
    await message_db.open()
    bot = Bot(db, message_db)
    bot.ops = DemoOps()
    bot.ops.add_sample_posts()
    await bot.prepare(load_all())
    for module in bot.modules:
        seed = getattr(module, "seed_demo", None)  # a feature with history to show adds its sample rows here
        if seed is not None:
            await seed(GUILD)
        if hasattr(module, "outside"):
            module.outside = DemoOutside()
    await _seed_samples(bot)
    app = build_app(bot, session_key=b"demo" * 8, public_url="", secure_cookies=False, demo_user=User(USER, "Demo Admin", "/static/img/logo.svg"))
    app.middlewares.append(_local_only)
    return bot, app, [db.close, message_db.close]
