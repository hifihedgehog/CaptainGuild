"""The real discord.py library with only the network replaced.

tests/fakes.py is quick but only as right as its stand-ins. Here a real Bot holds a real Guild built by discord.py from gateway JSON, a
fake network answers its HTTP calls, and events and interactions arrive as gateway JSON, so attribute names, keyword arguments and
payload shapes all get exercised the way Discord would exercise them.
"""
from __future__ import annotations

import asyncio
import datetime
import itertools
import json
import logging
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from urllib.parse import unquote

import discord
from discord.webhook.async_ import AsyncWebhookAdapter

from captainguild.bot import Bot
from captainguild.core import logsink
from captainguild.core.outside import Outside
from captainguild.db import Database
from captainguild.demo import DemoOps

Outside.offline = True  # nothing a test runs may reach the internet
KEY = "2" * 64
_ids = itertools.count(1100000000000000000)


def new_id() -> int:
    return next(_ids)


def iso(moment: datetime.datetime | None = None) -> str:
    return (moment or datetime.datetime.now(datetime.timezone.utc)).isoformat()


def user_json(user_id: int, name: str, *, bot: bool = False) -> dict:
    return {"id": str(user_id), "username": name, "discriminator": "0", "global_name": name, "avatar": None, "bot": bot}


class FakeHTTPResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "fake"


class Network:
    """Answers every HTTP call the library makes, and remembers them."""

    def __init__(self, bot_user: dict) -> None:
        self.bot_user = bot_user
        self.calls: list[tuple[str, str, Any]] = []
        self.unknown: list[str] = []
        self.messages: dict[int, dict] = {}
        self.posted: list[dict] = []
        self.modals: list[dict] = []
        self.members: dict[int, dict] = {}
        self.guild_id = 0
        self.channels: dict[int, dict] = {}
        self.roles: dict[int, dict] = {}
        self.uploads: list[tuple[int, list[str]]] = []
        self.keep_history = False  # a test that reads channel history turns this on. Messages the bot posted then come back from it.
        self.archived: dict[int, list[dict]] = {}
        self.keep_reactions = False  # a test that reads who reacted turns this on
        self.reactors: dict[tuple[int, str], dict[int, dict]] = {}
        self.invites: dict[str, dict] = {}
        self.bans: dict[int, dict] = {}  # who a test says is banned, by ID, as user JSON
        self.keep_webhooks = False  # a test that checks a webhook is found again turns this on
        self.webhooks: dict[int, list[dict]] = {}
        self.gone_webhooks: set[int] = set()  # webhooks a test says someone deleted
        self.webhook_params: list[tuple[str, str, dict]] = []
        self.guild_commands: dict[int, list[dict]] = {}  # what each server's command registration last sent

    def message_json(self, channel_id: int, body: dict | None, *, message_id: int | None = None, webhook_id: int | None = None) -> dict:
        body = body or {}
        message = {
            "id": str(message_id or new_id()), "channel_id": str(channel_id), "author": self.bot_user, "content": body.get("content") or "",
            "timestamp": iso(), "edited_timestamp": None, "tts": False, "mention_everyone": False, "mentions": [], "mention_roles": [],
            "attachments": [], "embeds": body.get("embeds") or [], "pinned": False, "type": 0, "components": body.get("components") or [],
            "flags": body.get("flags") or 0,
        }
        if self.guild_id:
            message["guild_id"] = str(self.guild_id)
        if webhook_id:
            message["webhook_id"] = str(webhook_id)
        return message

    def _reaction_call(self, method: str, path: str, route: Any, kwargs: dict) -> Any:
        """Holds reactions the way Discord does, for a test that turned keep_reactions on."""
        message_id = int(route.message_id)
        emoji = unquote(str(getattr(route, "emoji", "")))
        people = self.reactors.setdefault((message_id, emoji), {}) if emoji else {}
        if method == "GET":
            query = kwargs.get("params") or {}
            after = int(query.get("after") or 0)
            found = sorted((user for user_id, user in people.items() if user_id > after), key=lambda user: int(user["id"]))
            return found[:int(query.get("limit") or 25)]
        if method == "PUT":
            people[int(self.bot_user["id"])] = self.bot_user
            return None
        if path.endswith("/reactions"):
            for key in [key for key in self.reactors if key[0] == message_id]:
                self.reactors[key].clear()
        elif path.endswith("/@me"):
            people.pop(int(self.bot_user["id"]), None)
        elif path.endswith("{member_id}"):
            people.pop(int(route.member_id), None)
        else:
            people.clear()
        return None

    def _body(self, kwargs: dict) -> dict:
        if kwargs.get("json") is not None:
            return kwargs["json"]
        for part in kwargs.get("form") or []:
            if part.get("name") == "payload_json":
                return json.loads(part["value"])
        return {}

    def _store(self, message: dict) -> dict:
        self.messages[int(message["id"])] = message
        self.posted.append(message)
        return message

    @staticmethod
    def _seen(message: dict) -> dict:
        """A message as Discord returns it: its embeds don't appear while SUPPRESS_EMBEDS (4) is on, and come back once it's off."""
        if message.get("flags", 0) & 4 and message.get("embeds"):
            return {**message, "embeds": []}
        return message

    def _missing(self, what: str) -> discord.NotFound:
        return discord.NotFound(FakeHTTPResponse(404), {"code": 10008, "message": what})

    def _history(self, channel_id: int, query: dict) -> list[dict]:
        if not self.keep_history:
            return []
        found = sorted((m for m in self.messages.values() if int(m["channel_id"]) == channel_id), key=lambda m: int(m["id"]), reverse=True)
        limit = int(query.get("limit") or 50)
        if query.get("before"):
            found = [m for m in found if int(m["id"]) < int(query["before"])]
        if query.get("after"):
            # Discord answers with the messages right after it, the newest of those first, which is how discord.py pages forward (_after_strategy in discord/abc.py).
            return [self._seen(m) for m in [m for m in found if int(m["id"]) > int(query["after"])][-limit:]]
        return [self._seen(m) for m in found[:limit]]

    async def cdn(self, url: str, **kwargs: Any) -> bytes:
        return b"file from " + url.encode()

    async def http(self, route: Any, *, files: Any = None, form: Any = None, **kwargs: Any) -> Any:
        method, path = route.method, route.path
        body = self._body({**kwargs, "form": form})
        self.calls.append((method, path, body))
        # A Route keeps only its major parameters as attributes, so the others come back out of its URL.
        names = re.findall(r"{(\w+)}", path)
        pattern = re.sub(r"\\{\w+\\}", "([^/]+)", re.escape(path)) + "$"
        found = re.search(pattern, route.url)
        for name, value in zip(names, found.groups() if found else ()):
            setattr(route, name, int(value) if value.isdigit() else value)
        if files:
            self.uploads.append((getattr(route, "channel_id", 0), [f.filename for f in files]))
        channel_id = getattr(route, "channel_id", None)
        guild_id = getattr(route, "guild_id", None)
        message_id = getattr(route, "message_id", None)
        key = f"{method} {path}"
        if key == "POST /channels/{channel_id}/messages":
            message = self.message_json(channel_id, body)
            for file in files or []:
                message["attachments"].append({"id": str(new_id()), "filename": file.filename, "size": 1, "url": f"https://cdn.example/{file.filename}", "proxy_url": f"https://cdn.example/{file.filename}"})
            return self._seen(self._store(message))
        if key == "PATCH /channels/{channel_id}/messages/{message_id}":
            existing = self.messages.get(message_id) or self.message_json(channel_id, {}, message_id=message_id)
            existing.update({k: v for k, v in body.items() if k in ("content", "embeds", "components", "flags")})
            existing["edited_timestamp"] = iso()
            return self._seen(self._store(existing))
        if key == "GET /channels/{channel_id}/messages/{message_id}":
            if message_id in self.messages:
                return self._seen(self.messages[message_id])
            raise self._missing("Unknown Message")
        if key == "GET /channels/{channel_id}/messages":
            return self._history(channel_id, kwargs.get("params") or {})
        if key.startswith("GET /channels/{channel_id}/pins") or key == "GET /channels/{channel_id}/messages/pins":
            return {"items": [], "has_more": False}
        if "/reactions" in path:
            return self._reaction_call(method, path, route, kwargs) if self.keep_reactions else [] if method == "GET" else None
        if key == "DELETE /channels/{channel_id}/messages/{message_id}":
            self.messages.pop(message_id, None)
            return None
        if key == "POST /channels/{channel_id}/messages/bulk-delete":
            for gone in body.get("messages", []):
                self.messages.pop(int(gone), None)
            return None
        if key == "GET /channels/{channel_id}/webhooks":
            return list(self.webhooks.get(channel_id, [])) if self.keep_webhooks else []
        if key == "GET /guilds/{guild_id}/webhooks":
            return []
        if key == "POST /channels/{channel_id}/webhooks":
            made = {"id": str(new_id()), "type": 1, "channel_id": str(channel_id), "guild_id": str(self.guild_id), "name": body.get("name"), "token": "webhook-token", "user": self.bot_user, "application_id": self.bot_user["id"]}
            self.webhooks.setdefault(channel_id, []).append(made)
            return made
        if key == "PUT /applications/{application_id}/guilds/{guild_id}/commands":
            self.guild_commands[guild_id] = list(body or [])
            return [{**command, "description": command.get("description", ""), "id": str(new_id()), "application_id": self.bot_user["id"], "guild_id": str(guild_id), "version": "1",
                     "default_member_permissions": command.get("default_member_permissions")} for command in body or []]
        if key == "GET /guilds/{guild_id}/audit-logs":
            return {"audit_log_entries": [], "users": [], "webhooks": [], "threads": [], "integrations": [], "auto_moderation_rules": [], "application_commands": [], "guild_scheduled_events": []}
        if key in ("PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id}", "DELETE /guilds/{guild_id}/members/{user_id}/roles/{role_id}", "DELETE /guilds/{guild_id}/members/{user_id}", "PUT /guilds/{guild_id}/bans/{user_id}", "DELETE /guilds/{guild_id}/bans/{user_id}", "PUT /channels/{channel_id}/permissions/{target}", "DELETE /channels/{channel_id}/permissions/{target}"):
            return None
        if key in ("PATCH /guilds/{guild_id}/members/{user_id}", "GET /guilds/{guild_id}/members/{user_id}"):
            user_id = int(getattr(route, "user_id", 0))
            if user_id in self.members:
                return {**self.members[user_id], **{k: v for k, v in body.items() if k in ("nick", "communication_disabled_until")}}
            raise discord.NotFound(FakeHTTPResponse(404), {"code": 10007, "message": "Unknown Member"})
        if key == "GET /guilds/{guild_id}/members/{member_id}":
            if int(route.member_id) in self.members:
                return self.members[int(route.member_id)]
            raise discord.NotFound(FakeHTTPResponse(404), {"code": 10007, "message": "Unknown Member"})
        if key in ("PATCH /guilds/{guild_id}/members/@me", "PATCH /guilds/{guild_id}/members/@me/nick"):
            return self.members[int(self.bot_user["id"])]
        if key == "GET /guilds/{guild_id}/members":
            return list(self.members.values())
        if key == "GET /guilds/{guild_id}/members/search":
            return list(self.members.values())[:1]
        if key == "GET /guilds/{guild_id}/bans":
            return [{"user": user, "reason": None} for user in self.bans.values()]
        if key == "GET /guilds/{guild_id}/bans/{user_id}":
            if int(route.user_id) in self.bans:
                return {"user": self.bans[int(route.user_id)], "reason": None}
            raise discord.NotFound(FakeHTTPResponse(404), {"code": 10026, "message": "Unknown Ban"})
        if key == "POST /guilds/{guild_id}/bulk-ban":
            return {"banned_users": body.get("user_ids", []), "failed_users": []}
        if key == "POST /guilds/{guild_id}/channels":
            return {"id": str(new_id()), "guild_id": str(self.guild_id), "name": body.get("name", "new"), "type": body.get("type", 0), "position": body.get("position", 0), "parent_id": body.get("parent_id"), "permission_overwrites": body.get("permission_overwrites", []), "nsfw": False}
        if key in ("PATCH /channels/{channel_id}", "GET /channels/{channel_id}", "DELETE /channels/{channel_id}"):
            base = self.channels.get(channel_id) or {"id": str(channel_id), "guild_id": str(self.guild_id), "name": "channel", "type": 0, "position": 0, "permission_overwrites": [], "nsfw": False}
            if "thread_metadata" in base and method == "PATCH":
                base["thread_metadata"].update({k: body[k] for k in ("archived", "locked") if k in body})
            return {**base, **body}
        if key == "GET /guilds/{guild_id}/roles":
            return list(self.roles.values())
        if key == "POST /guilds/{guild_id}/roles":
            return {"id": str(new_id()), "name": body.get("name", "new role"), "color": body.get("color", 0), "hoist": False, "position": 1, "permissions": str(body.get("permissions", 0)), "managed": False, "mentionable": False}
        if key == "PATCH /guilds/{guild_id}/roles/{role_id}":
            base = self.roles.get(int(route.role_id)) or {"id": str(route.role_id), "name": "role", "color": 0, "hoist": False, "position": 1, "permissions": "0", "managed": False, "mentionable": False}
            return {**base, **body}
        if key == "DELETE /guilds/{guild_id}/roles/{role_id}":
            return None
        if key == "PATCH /guilds/{guild_id}/roles":
            return list(self.roles.values())
        if key == "POST /users/@me/channels":
            return {"id": str(new_id()), "type": 1, "recipients": [user_json(int(body.get("recipient_id", 1)), "someone")]}
        if key == "POST /guilds/{guild_id}/emojis":
            return {"id": str(new_id()), "name": body.get("name", "emoji"), "animated": False, "available": True, "managed": False, "require_colons": True, "roles": []}
        if key in ("POST /channels/{channel_id}/threads", "POST /channels/{channel_id}/messages/{message_id}/threads"):
            thread_id = new_id()
            thread = {"id": str(thread_id), "guild_id": str(self.guild_id), "parent_id": str(channel_id), "owner_id": self.bot_user["id"], "name": body.get("name", "thread"), "type": 11, "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "thread_metadata": {"archived": False, "auto_archive_duration": 10080, "archive_timestamp": iso(), "locked": False}, "flags": 0}
            if "message" in body:
                thread["message"] = self._store(self.message_json(thread_id, body["message"]))
            self.channels[thread_id] = {k: v for k, v in thread.items() if k != "message"}
            return thread
        if key == "GET /guilds/{guild_id}/threads/active":
            return {"threads": [], "members": []}
        if "threads/archived" in path:
            return {"threads": self.archived.get(channel_id, []), "members": [], "has_more": False}
        if key == "GET /oauth2/applications/@me":
            return {"id": self.bot_user["id"], "name": "bot", "icon": None, "description": "", "rpc_origins": [], "bot_public": False, "bot_require_code_grant": False, "owner": self.bot_user, "team": None, "verify_key": "", "summary": "", "flags": 0}
        if key == "GET /applications/@me":
            return {"id": self.bot_user["id"], "name": "bot", "bot_public": False, "owner": self.bot_user}
        if key == "GET /users/{user_id}":
            return user_json(int(route.user_id), "someone")
        if key == "GET /invites/{invite_id}":
            if str(route.invite_id) in self.invites:
                return self.invites[str(route.invite_id)]
            raise discord.NotFound(FakeHTTPResponse(404), {"code": 10006, "message": "Unknown Invite"})
        if key == "GET /guilds/{guild_id}/auto-moderation/rules/{rule_id}":
            return {"id": str(route.rule_id), "guild_id": str(self.guild_id), "name": "Blocked words", "creator_id": self.bot_user["id"], "event_type": 1, "trigger_type": 1, "trigger_metadata": {}, "actions": [], "enabled": True, "exempt_roles": [], "exempt_channels": []}
        self.unknown.append(key)
        return {}

    async def webhook(self, adapter: Any, route: Any, session: Any = None, *, payload: Any = None, multipart: Any = None, files: Any = None, **kwargs: Any) -> Any:
        method, path = route.method, route.path
        body = payload
        if body is None:
            for part in multipart or []:
                if part.get("name") == "payload_json":
                    body = json.loads(part["value"])
                    break
        body = body or {}
        self.calls.append((method, path, body))
        self.webhook_params.append((method, path, dict(kwargs.get("params") or {})))
        if getattr(route, "webhook_id", None) in self.gone_webhooks:
            raise discord.NotFound(FakeHTTPResponse(404), {"code": 10015, "message": "Unknown Webhook"})
        if path == "/interactions/{webhook_id}/{webhook_token}/callback":
            kind = body.get("type")
            data = body.get("data") or {}
            if kind == 9:
                self.modals.append(data)
                return {"interaction": {"id": str(route.webhook_id), "type": 2}}
            response = {"interaction": {"id": str(route.webhook_id), "type": 2}}
            if kind in (4, 5, 7):
                message = self._store(self.message_json(self._current_channel, data))
                response["resource"] = {"type": kind, "message": message}
            return response
        if path.endswith("/messages/@original") or "/messages/{message_id}" in path:
            if method == "DELETE":
                return None
            return self._store(self.message_json(self._current_channel, body))
        if path == "/webhooks/{webhook_id}/{webhook_token}" and method == "POST":
            return self._store(self.message_json(self._current_channel, body, webhook_id=route.webhook_id))
        self.unknown.append(f"webhook {method} {path}")
        return {}

    _current_channel = 0


class ErrorCollector(logging.Handler):
    """Every error the bot or the library logged. A healthy run leaves this empty."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        self.lines.append(text)


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    """A real Bot with a real guild, and the network replaced."""

    modules: list = []

    async def asyncSetUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        path = Path(self.folder.name)
        self.db = Database(path / "main.db", KEY)
        self.message_db = Database(path / "messages.db", KEY)
        await self.db.open()
        await self.message_db.open()
        self.errors = ErrorCollector()
        logging.getLogger().addHandler(self.errors)
        self._flush = mock.patch.object(logsink, "FLUSH_SECONDS", 0)
        self._flush.start()

        self.bot_id = new_id()
        self.bot_user = user_json(self.bot_id, "Captain Guild", bot=True)
        self.bot = Bot(self.db, self.message_db, public_url="https://bot.example")
        self.net = Network(self.bot_user)
        self.bot.http.request = self.net.http
        self.bot.http.get_from_cdn = self.net.cdn
        self._hook = mock.patch.object(AsyncWebhookAdapter, "request", self._adapter_request())
        self._hook.start()
        await self.bot._async_setup_hook()
        state = self.bot._connection
        state.user = discord.ClientUser(state=state, data=self.bot_user)
        state.application_id = self.bot_id
        await self.bot.prepare(self.modules)
        self.bot.ops = DemoOps()
        self._build_guild()

    def _adapter_request(self) -> Any:
        network = lambda: self.net

        async def request(adapter: Any, route: Any, session: Any = None, **kwargs: Any) -> Any:
            return await network().webhook(adapter, route, session, **kwargs)
        return request

    def _build_guild(self) -> None:
        self.guild_id = new_id()
        self.net.guild_id = self.guild_id
        everything = str(discord.Permissions.all().value)

        def role(name: str, position: int, permissions: str = "0", managed: bool = False, role_id: int | None = None) -> dict:
            data = {"id": str(role_id or new_id()), "name": name, "color": 0, "hoist": False, "position": position, "permissions": permissions, "managed": managed, "mentionable": False, "icon": None, "unicode_emoji": None, "flags": 0}
            if managed:
                data["tags"] = {"bot_id": str(self.bot_id)}
            return data

        self.role_everyone = role("@everyone", 0, "0", role_id=self.guild_id)
        self.role_muted = role("Muted", 1)
        self.role_member = role("Member", 2)
        self.role_mod = role("Moderator", 3, str(discord.Permissions(manage_messages=True).value))
        self.role_bot = role("Captain Guild", 5, str(discord.Permissions.all().value), managed=True)
        self.role_admin = role("Admin", 4, str(discord.Permissions(administrator=True).value))
        roles = [self.role_everyone, self.role_muted, self.role_member, self.role_mod, self.role_admin, self.role_bot]
        self.net.roles = {int(r["id"]): r for r in roles}

        def member(user_id: int, name: str, role_ids: list[str], *, bot: bool = False) -> dict:
            return {"user": user_json(user_id, name, bot=bot), "roles": role_ids, "joined_at": iso(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "premium_since": None, "avatar": None, "communication_disabled_until": None}

        self.owner_id, self.admin_id, self.mod_id, self.member_id = new_id(), new_id(), new_id(), new_id()
        members = [
            member(self.bot_id, "Captain Guild", [self.role_bot["id"]], bot=True),
            member(self.owner_id, "owner", [self.role_admin["id"]]),
            member(self.admin_id, "admin", [self.role_admin["id"]]),
            member(self.mod_id, "mod", [self.role_mod["id"]]),
            member(self.member_id, "member", [self.role_member["id"]]),
        ]
        self.member_json = {int(m["user"]["id"]): m for m in members}
        self.net.members = self.member_json

        def channel(name: str, kind: int, parent: int | None = None, position: int = 0, **extra: Any) -> dict:
            data = {"id": str(new_id()), "guild_id": str(self.guild_id), "name": name, "type": kind, "position": position, "permission_overwrites": [], "parent_id": str(parent) if parent else None, "nsfw": False, "topic": None, "rate_limit_per_user": 0, "last_message_id": None, "flags": 0, **extra}
            return data

        self.category = channel("Community", 4)
        category_id = int(self.category["id"])
        self.text = channel("general", 0, category_id, 1)
        self.log_channel = channel("logs", 0, category_id, 2)
        self.voice = channel("Lounge", 2, category_id, 3, bitrate=64000, user_limit=0, rtc_region=None)
        self.forum = channel("reviews", 15, category_id, 4, available_tags=[], default_reaction_emoji=None, default_thread_rate_limit_per_user=0, default_sort_order=None, default_forum_layout=0)
        channels = [self.category, self.text, self.log_channel, self.voice, self.forum]
        self.net.channels = {int(c["id"]): c for c in channels}
        self.net._current_channel = int(self.text["id"])
        payload = {
            "id": str(self.guild_id), "name": "Test Guild", "icon": None, "splash": None, "discovery_splash": None, "banner": None, "description": None,
            "owner_id": str(self.owner_id), "afk_channel_id": None, "afk_timeout": 300, "verification_level": 0, "default_message_notifications": 0,
            "explicit_content_filter": 0, "features": [], "mfa_level": 0, "premium_tier": 0, "premium_subscription_count": 0, "preferred_locale": "en-US",
            "nsfw_level": 0, "premium_progress_bar_enabled": False, "system_channel_id": None, "system_channel_flags": 0, "rules_channel_id": None,
            "public_updates_channel_id": None, "max_members": 500000, "vanity_url_code": None, "application_id": None,
            "joined_at": iso(), "large": False, "member_count": len(members), "unavailable": False,
            "roles": roles, "members": members, "channels": channels, "threads": [], "emojis": [], "stickers": [], "presences": [], "voice_states": [],
            "stage_instances": [], "guild_scheduled_events": [], "soundboard_sounds": [],
        }
        self.bot._connection.parse_guild_create(payload)
        self.guild = self.bot.get_guild(self.guild_id)
        self.assertTrue(self.guild.chunked)

    async def asyncTearDown(self) -> None:
        logging.getLogger().removeHandler(self.errors)
        await self.settle()
        await self.bot.timers.stop()
        self._hook.stop()
        self._flush.stop()
        for module in self.bot.modules:
            unload = getattr(module, "cog_unload", None)
            if unload:
                result = unload()
                if asyncio.iscoroutine(result):
                    await result
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.db.close()
        await self.message_db.close()
        self.folder.cleanup()

    async def settle(self, seconds: float = 8.0) -> None:
        """Wait for event handlers and command tasks to finish."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            await asyncio.sleep(0.01)
            busy = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and (t.get_name().startswith("discord.py") or t.get_name().startswith("CommandTree") or t.get_name().startswith("discord-ui"))]
            if not busy:
                await asyncio.sleep(0.02)
                return

    def member_payload(self, user_id: int) -> dict:
        data = dict(self.member_json[user_id])
        member = self.guild.get_member(user_id)
        data["permissions"] = str(member.guild_permissions.value)
        return data

    def interaction_payload(self, kind: int, data: dict, *, user_id: int, channel: dict | None = None, message: dict | None = None) -> dict:
        channel = channel or self.text
        self.net._current_channel = int(channel["id"])
        # An interaction expires 15 minutes after the moment in its ID, and the library falls back to a plain channel post for an expired one.
        fresh_id = discord.utils.time_snowflake(discord.utils.utcnow()) + next(_ids) % 4096
        payload = {
            "id": str(fresh_id), "application_id": str(self.bot_id), "type": kind, "token": "token", "version": 1, "guild_id": str(self.guild_id),
            "channel_id": channel["id"], "channel": channel, "member": self.member_payload(user_id), "locale": "en-US", "guild_locale": "en-US",
            "app_permissions": str(discord.Permissions.all().value), "entitlements": [], "authorizing_integration_owners": {"0": str(self.guild_id)}, "context": 0,
            "attachment_size_limit": 26214400, "data": data,
        }
        if message is not None:
            payload["message"] = message
        return payload

    def message_payload(self, *, user_id: int, content: str = "hello", channel: dict | None = None, **extra: Any) -> dict:
        channel = channel or self.text
        author = self.member_json[user_id]
        return {
            "id": str(new_id()), "channel_id": channel["id"], "guild_id": str(self.guild_id), "author": author["user"],
            "member": {k: v for k, v in author.items() if k != "user"}, "content": content, "timestamp": iso(), "edited_timestamp": None, "tts": False,
            "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [], "embeds": [], "pinned": False, "type": 0, **extra,
        }

    def dispatch_message(self, **kwargs: Any) -> dict:
        payload = self.message_payload(**kwargs)
        self.bot._connection.parse_message_create(payload)
        return payload

    def assert_no_errors(self, context: str = "") -> None:
        if self.errors.lines:
            self.fail(f"{context}\n" + "\n\n".join(self.errors.lines[:6]))
