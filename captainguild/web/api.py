"""The dashboard's JSON API: what a module uses to publish its endpoints.

    def api(self, router):
        router.get("/tags", self.list_tags, access="staff")
        router.post("/tags", self.create_tag)

    async def create_tag(self, req):
        form = await req.json()
        ...
        await req.record("tag.create", detail=name)
        return {"id": new_id}

Paths are relative to /api/g/{guild_id}. A handler returns anything JSON can hold. Discord IDs can be plain ints: the
encoder sends every int too big for JavaScript as text, and validate.snowflake turns text back into int on the way in.
Raise ApiError for anything the person should see. A ValidationError becomes per-field messages.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiohttp import web

from ..core import access
from ..core.ops import OpsError, require_channel
from ..core.perms import permission_names
from ..core.settings import Actor
from ..core.validate import Invalid, ValidationError

JS_SAFE_INT = 2 ** 53 - 1
MAX_JSON_BYTES = 1024 * 1024
# "viewer" routes are the statistics pages, which a server can open to everyone in it. Whoever else gets through is the statistics
# module's call (its viewer_level), and only those routes ask it.
ACCESS_ORDER = {"viewer": 0, "staff": 1, "full": 2}


class ApiError(Exception):
    def __init__(self, status: int, message: str, fields: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.fields = fields or {}


def safe_json(value: Any) -> Any:
    """Make a value safe for JavaScript: a Discord ID can't survive as a number there."""
    if value is None or isinstance(value, (bool, str, float)):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > JS_SAFE_INT else value
    if isinstance(value, dict):
        return {str(k): safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [safe_json(v) for v in value]
    raise TypeError(f"Can't send {type(value).__name__} as JSON")


def json_response(data: Any, status: int = 200) -> web.Response:
    # Plain ASCII on the wire, so a lone surrogate that reached storage in an old row can still be sent and fixed.
    return web.Response(text=json.dumps(safe_json(data)), status=status, content_type="application/json", headers={"Cache-Control": "no-store"})


def _not_a_value(name: str) -> None:
    raise ValueError(f"{name} isn't a JSON value")


def read_json(body: bytes) -> Any:
    """A request's JSON. Refuses what could be stored and then not sent back: a lone surrogate, which has no UTF-8 form, and NaN or Infinity, which a browser can't parse."""
    if len(body) > MAX_JSON_BYTES:
        raise ApiError(413, "That's too much data for one request.")
    try:
        parsed = json.loads(body or b"{}", parse_constant=_not_a_value)
        json.dumps(parsed, ensure_ascii=False).encode()
    except (ValueError, RecursionError) as error:  # very deep nesting raises RecursionError
        raise ApiError(400, "The request wasn't valid JSON.") from error
    return parsed


@dataclass
class User:
    id: int
    name: str
    avatar: str | None
    mfa: bool = False  # two-factor authentication on the account, as Discord said at sign-in


class Req:
    """One API call: who asked, for which server, and what they sent."""

    def __init__(self, request: web.Request, bot: Any, user: User, guild_id: int, access: str):
        self.request = request
        self.bot = bot
        self.user = user
        self.guild_id = guild_id
        self.access = access
        self.actor = Actor(user.id, user.name)
        self.ops = bot.ops
        self.db = bot.db
        self.settings = bot.settings

    @property
    def query(self) -> Any:
        return self.request.query

    @property
    def is_admin(self) -> bool:
        """The server's owner or an Administrator. The demo has no server behind it, so there it's anyone."""
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return True
        member = guild.get_member(self.user.id)
        return member is not None and access.is_admin(member)

    def can_see(self, channel_id: int) -> bool:
        """Whether Discord shows this person the channel. The owner and Administrators see all of them, and a channel that's gone has nothing left to hide."""
        return self._may(channel_id, access.can_view)

    def can_read(self, channel_id: int) -> bool:
        """Whether they could read the channel's earlier messages themselves."""
        return self._may(channel_id, access.can_read)

    def can_post(self, channel_id: int) -> bool:
        """Whether they could post in the channel themselves."""
        return self._may(channel_id, access.can_post)

    def can_ping(self, channel_id: int) -> bool:
        """Whether they could ping @everyone, @here and unmentionable roles there themselves."""
        return self._may(channel_id, access.can_ping)

    def can_manage_messages(self, channel_id: int) -> bool:
        return self._may(channel_id, access.can_manage_messages)

    def holds(self, permission: str) -> bool:
        """Whether they hold this Discord permission across the server themselves."""
        if self.is_admin:
            return True
        member = self.bot.get_guild(self.guild_id).get_member(self.user.id)
        return member is not None and bool(getattr(member.guild_permissions, permission))

    def require_permission(self, permission: str) -> None:
        """Refuses a route that uses the bot's permission unless they hold the same one. Manage Server alone doesn't include Manage Roles or Manage Channels."""
        if not self.holds(permission):
            raise ApiError(403, f"You need {permission_names([permission])} to do that.")

    def top_role_position(self) -> float:
        """Where their highest role sits. The owner and Administrators outrank every role."""
        if self.is_admin:
            return float("inf")
        member = self.bot.get_guild(self.guild_id).get_member(self.user.id)
        return member.top_role.position if member is not None else 0

    def _may(self, channel_id: int, check: Callable[[Any, Any], bool]) -> bool:
        if self.is_admin:
            return True
        guild = self.bot.get_guild(self.guild_id)
        channel = guild.get_channel_or_thread(channel_id) or self.ops.known_thread(self.guild_id, channel_id)
        member = guild.get_member(self.user.id)
        if channel is None:
            return True
        if member is None:
            return False
        return check(member, channel)

    def channel(self, value: Any, field: str = "channel_id", *, posting: bool = False) -> int:
        """The ID of a channel a browser named, once it's known to be in this server and where this person can see.
        With `posting`, the bot is about to post there for them, so they must be able to post there themselves."""
        channel_id = require_channel(self.ops, self.guild_id, value, field)
        if not self.can_see(channel_id):
            raise ValidationError({field: "You can't see that channel."})
        if posting and not self.can_post(channel_id):
            raise ValidationError({field: "You can't post in that channel."})
        return channel_id

    def require_ping(self, channel_id: int, field: str = "ping") -> None:
        """Refuses a saved or sent ping of @everyone, @here or an unmentionable role unless they could send it themselves."""
        if not self.can_ping(channel_id):
            raise ValidationError({field: "You need Mention Everyone in that channel to ping through the bot."})

    def refuse_other_servers(self, form: dict) -> None:
        """Refuses a form that names a channel or role of another server the bot is in."""
        from .app import field_naming_another_server  # app.py imports this module
        field = field_naming_another_server(self.bot, self.guild_id, form)
        if field is not None:
            raise ValidationError({field: "That belongs to another server."})

    def param(self, name: str) -> str:
        return self.request.match_info[name]

    def int_param(self, name: str) -> int:
        try:
            value = int(self.request.match_info[name])
        except ValueError as error:
            raise ApiError(404, "Not found.") from error
        if abs(value) >= 2 ** 63:  # SQLite refuses a number this big, and no row can have it
            raise ApiError(404, "Not found.")
        return value

    async def json(self, expected: type | None = dict) -> Any:
        """The body as JSON, which must be an object unless the route says otherwise, so a handler can call `form.get`."""
        parsed = read_json(await self.request.read())
        if expected is not None and not isinstance(parsed, expected):
            raise ValidationError({"form": "Expected a set of fields."})
        return parsed

    async def save_settings(self, module: str, form: Any, *, action: str | None = None) -> dict:
        """Save a settings form with every check the page's Save button runs. An import route uses this, so pasting a setting reaches no further than picking it."""
        from .app import store_settings  # app.py imports this module
        return await store_settings(self, module, form, action=action)

    async def record(self, action: str, *, detail: str | None = None, module: str | None = None, before: Any = None, after: Any = None) -> None:
        await self.bot.activity_log.record(self.guild_id, self.actor, action, detail=detail, module=module, before=before, after=after)


class PublicReq:
    """A call to a page a server made public: no session, no person, reads only."""

    access = "public"
    user = None

    def __init__(self, request: web.Request, bot: Any, guild_id: int):
        self.request = request
        self.bot = bot
        self.guild_id = guild_id
        self.ops = bot.ops

    @property
    def query(self) -> Any:
        return self.request.query

    def param(self, name: str) -> str:
        return self.request.match_info[name]


Handler = Callable[[Req], Awaitable[Any]]


class Router:
    """Collects a module's endpoints. web/app.py wraps each with sign-in, access and error handling."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Handler, str]] = []

    def add(self, method: str, path: str, handler: Handler, access: str = "full") -> None:
        if access not in ACCESS_ORDER:
            raise ValueError(access)
        self.routes.append((method, path, handler, access))

    def get(self, path: str, handler: Handler, access: str = "full") -> None:
        self.add("GET", path, handler, access)

    def post(self, path: str, handler: Handler, access: str = "full") -> None:
        self.add("POST", path, handler, access)

    def put(self, path: str, handler: Handler, access: str = "full") -> None:
        self.add("PUT", path, handler, access)

    def patch(self, path: str, handler: Handler, access: str = "full") -> None:
        self.add("PATCH", path, handler, access)

    def delete(self, path: str, handler: Handler, access: str = "full") -> None:
        self.add("DELETE", path, handler, access)


def error_response(error: Exception) -> web.Response | None:
    """The answer for an error a person can act on, or None when it's a bug that belongs in the log."""
    if isinstance(error, ApiError):
        return json_response({"error": error.message, "fields": error.fields}, error.status)
    if isinstance(error, ValidationError):
        first = next(iter(error.fields.values()), "Check the highlighted fields.")
        return json_response({"error": first, "fields": error.fields}, 400)
    if isinstance(error, Invalid):
        return json_response({"error": str(error), "fields": {}}, 400)
    if isinstance(error, OpsError):
        return json_response({"error": str(error), "fields": {}}, 502)
    return None
