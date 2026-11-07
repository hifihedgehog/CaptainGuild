"""The website: sign-in, the JSON API, and the dashboard's static files.

Pages are static files that fetch JSON and render with text nodes, so nothing a member typed can run as script. A person signs in
with a one-time link from the bot's /dashboard command, which needs only the bot token. With a client secret set, the site uses Sign
in with Discord instead, which asks for the `identify` scope and nothing else, reads the user once and revokes the token. Which
servers a person can open comes from the bot's own view of each server, on every request, so a role change applies at once.
"""
from __future__ import annotations

import hmac
import hashlib
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from .. import __version__, brand
from ..core import access
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import DANGEROUS_PERMISSIONS, PERMISSION_LABELS
from ..core.perms import NEVER_OFF, permission_names
from ..core.invite import invite_url
from ..core.signin import LINK_SECONDS
from ..core.validate import ValidationError, whole_number
from ..crypto import new_token, token_hash
from ..modules.mydata import BY_KEY
from .api import ACCESS_ORDER, ApiError, Req, Router, User, error_response, json_response, read_json

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "__Host-cg_session"
FLOW_COOKIE = "__Host-cg_flow"
DEV_SESSION_COOKIE = "cg_session"
DEV_FLOW_COOKIE = "cg_flow"
SESSION_SECONDS = 14 * 24 * 3600
FOREIGN_REQUEST = "That request didn't come from this site. Open the dashboard again and retry."
FLOW_SECONDS = 600
DISCORD_API = "https://discord.com/api/v10"
WRITES_PER_MINUTE = 240
READS_PER_MINUTE = 600
MEMBERS_PER_CALL = 100
MAX_REQUEST_BYTES = 12 * 1024 * 1024

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' https: data: blob:; font-src 'self'; connect-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=()",
}
PUBLIC_PAGES = {"/": "index.html", "/privacy": "privacy.html", "/terms": "terms.html", "/docs": "docs.html", "/status": "status.html", "/my-data": "mydata.html"}
DASHBOARD_PAGE = "dashboard.html"
CORE_SETTINGS = "core"
DISCORD_ID_TEXT = re.compile(r"[0-9]{17,20}")
HIDDEN_CHANNEL = "Hidden channel"


class Sessions:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def create(self, user: User) -> str:
        token = new_token()
        now = time.time()
        await self.db.run(
            "INSERT INTO web_sessions (token_hash, user_id, user_name, avatar, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token_hash(token), user.id, user.name, user.avatar, now, now + SESSION_SECONDS),
        )
        await self.db.run("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
        return token

    async def read(self, token: str | None) -> User | None:
        if not token:
            return None
        row = await self.db.one("SELECT * FROM web_sessions WHERE token_hash = ? AND expires_at > ?", (token_hash(token), time.time()))
        return User(row["user_id"], row["user_name"], row["avatar"]) if row else None

    async def end(self, token: str | None) -> None:
        if token:
            await self.db.run("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash(token),))

    async def end_all(self, user_id: int) -> None:
        await self.db.run("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))


def csrf_for(session_token: str, key: bytes) -> str:
    return hmac.new(key, session_token.encode(), hashlib.sha256).hexdigest()


class RateLimiter:
    """A small per-person limit, far above what a person clicks and below what a stuck script sends."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.windows: dict[int, tuple[float, int]] = {}

    def allow(self, user_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        start, count = self.windows.get(user_id, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        count += 1
        self.windows[user_id] = (start, count)
        if len(self.windows) > 10_000:
            self.windows.pop(next(iter(self.windows)))
        return count <= self.per_minute


def local_path(value: str | None) -> str:
    """A place on this site to go after sign-in. Browsers read a backslash as a slash and drop tabs and line breaks, so "/\\evil.example" and "/\t/evil.example" would both leave the site."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value or any(character < " " or character == "\x7f" for character in value):
        return "/servers"
    return value


@dataclass
class Site:
    bot: Any
    session_key: bytes
    sessions: Sessions
    client_id: str
    client_secret: str
    public_url: str
    session_cookie: str
    flow_cookie: str
    secure_cookies: bool
    demo_user: User | None
    limiter: RateLimiter
    read_limiter: RateLimiter


SITE = web.AppKey("site", Site)


def build_app(bot: Any, *, session_key: bytes, client_id: str = "", client_secret: str = "", public_url: str = "", secure_cookies: bool = True,
              demo_user: User | None = None) -> web.Application:
    app = web.Application(client_max_size=MAX_REQUEST_BYTES, middlewares=[_security_headers])
    app[SITE] = Site(
        bot=bot, session_key=session_key, sessions=Sessions(bot.db), client_id=client_id, client_secret=client_secret,
        public_url=public_url.rstrip("/"), session_cookie=SESSION_COOKIE if secure_cookies else DEV_SESSION_COOKIE,
        flow_cookie=FLOW_COOKIE if secure_cookies else DEV_FLOW_COOKIE, secure_cookies=secure_cookies, demo_user=demo_user, limiter=RateLimiter(WRITES_PER_MINUTE),
        read_limiter=RateLimiter(READS_PER_MINUTE),
    )

    app.router.add_get("/login", _login)
    app.router.add_get("/login/callback", _login_callback)
    app.router.add_get("/login/link/{token}", _link_page)
    app.router.add_post("/login/link/{token}", _use_link)
    app.router.add_post("/logout", _logout)
    app.router.add_get("/api/me", _me)
    app.router.add_get("/api/status", _status)
    app.router.add_get("/api/my-data", _for_the_person(_my_data))
    app.router.add_post("/api/my-data/delete", _for_the_person(_my_data_delete))
    app.router.add_get("/api/my-data/download", _for_the_person(_my_data_download))
    app.router.add_get("/healthz", _healthz)
    for path, page in PUBLIC_PAGES.items():
        app.router.add_get(path, _page(page))
    app.router.add_get("/servers", _page(DASHBOARD_PAGE))
    app.router.add_get(r"/dashboard/{guild_id:\d+}", _page(DASHBOARD_PAGE))
    app.router.add_get(r"/dashboard/{guild_id:\d+}/{tail:.*}", _page(DASHBOARD_PAGE))
    app.router.add_static("/static/", STATIC, append_version=False)

    router = Router()
    _core_routes(router)
    for module in getattr(bot, "modules", []):
        module.api(router)
    for method, path, handler, level in router.routes:
        app.router.add_route(method, f"/api/g/{{guild_id:\\d+}}{path}", _wrap(handler, level))
    return app


@web.middleware
async def _security_headers(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as error:
        response = error
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=300")
    if isinstance(response, web.HTTPException):
        raise response
    return response


def _page(name: str) -> Any:
    async def handler(request: web.Request) -> web.FileResponse:
        path = STATIC / name
        if not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
    return handler


async def _current_user(request: web.Request) -> tuple[User | None, str | None]:
    demo = request.app[SITE].demo_user
    if demo is not None:
        return demo, "demo"
    token = request.cookies.get(request.app[SITE].session_cookie)
    return await request.app[SITE].sessions.read(token), token


def _wrap(handler: Any, access: str) -> Any:
    needed = ACCESS_ORDER[access]

    async def wrapped(request: web.Request) -> web.StreamResponse:
        try:
            user, token = await _current_user(request)
            if user is None:
                raise ApiError(401, "Sign in first.")
            if request.method not in ("GET", "HEAD"):
                _check_write(request, token, user)
            elif not request.app[SITE].read_limiter.allow(user.id):
                raise ApiError(429, "That's a lot of requests in a minute. Wait a moment and try again.")
            guild_id = whole_number(request.match_info["guild_id"])
            bot = request.app[SITE].bot
            level = bot.ops.access_for(guild_id, user.id) if guild_id is not None else None
            if level is None:
                raise ApiError(403, "You don't have access to that here.")
            if ACCESS_ORDER[level] < needed:
                raise ApiError(403, "That takes Manage Server. Ask someone who has it.")
            result = await handler(Req(request, bot, user, guild_id, level))
            return result if isinstance(result, web.StreamResponse) else json_response(result if result is not None else {"ok": True})
        except web.HTTPException:
            raise
        except Exception as error:
            answer = error_response(error)
            if answer is not None:
                return answer
            log.exception("%s %s failed", request.method, request.path)
            return json_response({"error": "Something went wrong on the server. It's been logged.", "fields": {}}, 500)
    return wrapped


def _same_text(sent: str, expected: str) -> bool:
    """A constant-time compare. On text, compare_digest refuses anything but ASCII, and a header or a query can hold anything."""
    return hmac.compare_digest(sent.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass"))


def _check_write(request: web.Request, token: str | None, user: User) -> None:
    origin = request.headers.get("Origin")
    public = request.app[SITE].public_url
    if origin and public and origin != public:
        raise ApiError(403, FOREIGN_REQUEST)
    if token != "demo":
        sent = request.headers.get("X-CSRF-Token", "")
        if not token or not _same_text(sent, csrf_for(token, request.app[SITE].session_key)):
            raise ApiError(403, "Your session expired. Reload the page.")
    if not request.app[SITE].limiter.allow(user.id):
        raise ApiError(429, "That's a lot of changes in a minute. Wait a moment and try again.")


async def _me(request: web.Request) -> web.Response:
    user, token = await _current_user(request)
    bot = request.app[SITE].bot
    meta = {"brand": {"name": brand.NAME, "tagline": brand.TAGLINE, "repo": brand.REPO_URL}, "version": __version__, "message_text": bot.reads_message_text}
    if request.app[SITE].client_id:
        meta["invite"] = invite_url(request.app[SITE].client_id)
    if user is None:
        return json_response({**meta, "user": None, "guilds": []})
    guilds = []
    for guild_id in bot.ops.guild_ids():
        level = bot.ops.access_for(guild_id, user.id)
        if level is None:
            continue
        info = bot.ops.guild_summary(guild_id)
        if info is None:
            continue
        guilds.append({"id": guild_id, "name": info["name"], "icon": info["icon"], "access": level})
    guilds.sort(key=lambda g: g["name"].lower())
    csrf = csrf_for(token, request.app[SITE].session_key) if token and token != "demo" else ""
    return json_response({**meta, "user": {"id": user.id, "name": user.name, "avatar": user.avatar}, "csrf": csrf, "guilds": guilds})


def _for_the_person(handler: Any) -> Any:
    """An endpoint about whoever is signed in, wherever they are, rather than about one server."""

    async def wrapped(request: web.Request) -> web.StreamResponse:
        try:
            user, token = await _current_user(request)
            if user is None:
                raise ApiError(401, "Sign in first.")
            if request.method not in ("GET", "HEAD"):
                _check_write(request, token, user)
            module = request.app[SITE].bot.module("mydata")
            if module is None:
                raise ApiError(404, "Not found.")
            if not module.allow_look(user.id):
                raise ApiError(429, "That's a lot of requests in a minute. Wait a moment and try again.")
            return await handler(request, module, user, token)
        except web.HTTPException:
            raise
        except Exception as error:
            answer = error_response(error)
            if answer is not None:
                return answer
            log.exception("%s %s failed", request.method, request.path)
            return json_response({"error": "Something went wrong on the server. It's been logged.", "fields": {}}, 500)
    return wrapped


async def _my_data_view(request: web.Request, module: Any, user: User, token: str | None) -> web.Response:
    csrf = csrf_for(token, request.app[SITE].session_key) if token and token != "demo" else ""
    return json_response({"user": {"id": user.id, "name": user.name}, "csrf": csrf, "servers": await module.overview(user.id)})


async def _my_data(request: web.Request, module: Any, user: User, token: str | None) -> web.Response:
    return await _my_data_view(request, module, user, token)


async def _my_data_delete(request: web.Request, module: Any, user: User, token: str | None) -> web.Response:
    form = read_json(await request.read())
    key = form.get("section") if isinstance(form, dict) else None
    if not isinstance(key, str) or key not in BY_KEY:
        raise ApiError(400, "That isn't something you can delete here.")
    raw = form.get("guild_id")
    guild_id = whole_number(raw) if raw is not None else None
    if raw is not None and guild_id is None:
        raise ApiError(400, "That isn't a server.")
    await module.delete(user.id, user.name, guild_id, key)
    return await _my_data_view(request, module, user, token)


async def _my_data_download(request: web.Request, module: Any, user: User, token: str | None) -> web.Response:
    response = json_response(await module.export(user.id))
    response.headers["Content-Disposition"] = 'attachment; filename="my-data.json"'
    response.headers["Cache-Control"] = "no-store"
    return response


async def _status(request: web.Request) -> web.Response:
    bot = request.app[SITE].bot
    latency = getattr(bot, "latency", None)
    return json_response({
        "version": __version__,
        "uptime": time.time() - getattr(bot, "started_at", time.time()),
        "latency_ms": round(latency * 1000) if isinstance(latency, float) and latency == latency else None,
        "guilds": len(bot.ops.guild_ids()),
    })


async def _healthz(request: web.Request) -> web.Response:
    """Whether the site answers. The installer waits on this."""
    return web.json_response({"ok": True})


def _small_page(title: str, body: str, *, status: int = 200) -> web.Response:
    """A page with no script, for the steps of signing in. The caller escapes whatever a person could have typed."""
    page = (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="robots" content="noindex"><title>{title} - {html.escape(brand.NAME)}</title>'
            f'<link rel="icon" href="/static/img/logo.svg" type="image/svg+xml"><link rel="stylesheet" href="/static/css/site.css"></head>'
            f'<body class="site"><main class="site-main sign-in"><img class="sign-in-logo" src="/static/img/logo.svg" alt=""><h1>{title}</h1>{body}</main></body></html>\n')
    return web.Response(text=page, content_type="text/html", status=status, headers={"Cache-Control": "no-store"})


def _sign_in_failed() -> web.Response:
    body = '<p>Discord didn\'t confirm who you are, or the sign-in took more than ten minutes. Try again from the start.</p><p><a class="btn primary" href="/login">Sign in</a></p>'
    return _small_page("Sign-in didn't work", body, status=400)


def _dead_link() -> web.Response:
    return _small_page("This link no longer works", f"<p>A sign-in link works once and expires after {LINK_SECONDS // 60} minutes. Type <code>/dashboard</code> in Discord for a new one.</p>", status=410)


def _same_site(request: web.Request) -> bool:
    """Whether a form was posted from this site. Under this site's referrer policy a browser sends "Origin: null" on its own forms, so the browser's fetch header decides first."""
    fetched_from = request.headers.get("Sec-Fetch-Site")
    if fetched_from:
        return fetched_from in ("same-origin", "none")
    origin = request.headers.get("Origin")
    public = request.app[SITE].public_url
    return not origin or origin == "null" or not public or origin == public


async def _link_page(request: web.Request) -> web.Response:
    """Shows who the link signs in and waits for a click. Signing in on a plain page load would let a link scanner use the link up, or sign someone in as a stranger."""
    link = request.app[SITE].bot.sign_in_links.peek(request.match_info["token"])
    if link is None:
        return _dead_link()
    name = html.escape(link.name)
    avatar = f'<img class="sign-in-avatar" src="{html.escape(link.avatar, quote=True)}" alt="">' if link.avatar else ""
    return _small_page(f"Sign in as {name}", f"{avatar}<p>You asked for this link with <code>/dashboard</code> in Discord. If you didn't, close this page.</p>"
                                             f'<form method="post"><button class="btn primary" type="submit">Continue</button></form>')


async def _use_link(request: web.Request) -> web.Response:
    app = request.app[SITE]
    if not _same_site(request):
        raise web.HTTPForbidden(text=FOREIGN_REQUEST)
    link = app.bot.sign_in_links.take(request.match_info["token"])
    if link is None:
        return _dead_link()
    token = await app.sessions.create(User(link.user_id, link.name, link.avatar))
    _, _, remembered = request.cookies.get(app.flow_cookie, "").partition("|")
    # The My data link goes to My data, whatever sign-in page was open in the last few minutes.
    response = web.HTTPSeeOther(local_path(link.target if link.target == "/my-data" else remembered or link.target))
    response.set_cookie(app.session_cookie, token, max_age=SESSION_SECONDS, httponly=True, secure=app.secure_cookies, samesite="Lax", path="/")
    response.del_cookie(app.flow_cookie, path="/", secure=app.secure_cookies)
    raise response


async def _login(request: web.Request) -> web.Response:
    app = request.app[SITE]
    target = local_path(request.query.get("next"))
    user, _ = await _current_user(request)
    if user is not None:
        raise web.HTTPFound(target)
    if not app.client_secret:
        page = _small_page("Sign in", f"<p>Type <code>/dashboard</code> in Discord, in a server that has {html.escape(brand.NAME)}. It answers with a private link that signs you in.</p>")
        if "next" in request.query:  # with no destination asked for, the link's own target decides
            page.set_cookie(app.flow_cookie, f"|{target}", max_age=FLOW_SECONDS, httponly=True, secure=app.secure_cookies, samesite="Lax", path="/")
        return page
    state = secrets.token_urlsafe(24)
    query = urlencode({
        "response_type": "code", "client_id": app.client_id, "scope": "identify", "state": state,
        "redirect_uri": f"{app.public_url}/login/callback", "prompt": "none",
    })
    response = web.HTTPFound(f"https://discord.com/oauth2/authorize?{query}")
    response.set_cookie(app.flow_cookie, f"{state}|{target}", max_age=FLOW_SECONDS, httponly=True, secure=app.secure_cookies, samesite="Lax", path="/")
    raise response


async def _login_callback(request: web.Request) -> web.Response:
    app = request.app[SITE]
    if not app.client_secret:
        raise web.HTTPFound("/login")
    flow = request.cookies.get(app.flow_cookie, "")
    state, _, target = flow.partition("|")
    if not state or not _same_text(state, request.query.get("state", "")) or "code" not in request.query:
        return _sign_in_failed()
    user = await _read_discord_user(app, request.query["code"])
    if user is None:
        return _sign_in_failed()
    token = await app.sessions.create(user)
    response = web.HTTPFound(local_path(target))
    response.set_cookie(app.session_cookie, token, max_age=SESSION_SECONDS, httponly=True, secure=app.secure_cookies, samesite="Lax", path="/")
    response.del_cookie(app.flow_cookie, path="/", secure=app.secure_cookies)
    raise response


async def _read_discord_user(app: Site, code: str) -> User | None:
    """Trade the code for a token, read who it belongs to, and revoke it. No token is ever stored."""
    auth = aiohttp.BasicAuth(app.client_id, app.client_secret)
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            data = {"grant_type": "authorization_code", "code": code, "redirect_uri": f"{app.public_url}/login/callback"}
            async with session.post(f"{DISCORD_API}/oauth2/token", data=data, auth=auth) as response:
                if response.status != 200:
                    log.warning("Token exchange returned %s", response.status)
                    return None
                access = (await response.json()).get("access_token")
            if not access:
                return None
            async with session.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {access}"}) as response:
                body = await response.json() if response.status == 200 else None
            async with session.post(f"{DISCORD_API}/oauth2/token/revoke", data={"token": access, "token_type_hint": "access_token"}, auth=auth):
                pass
        except (aiohttp.ClientError, TimeoutError, ValueError):
            log.warning("Sign-in with Discord failed", exc_info=True)
            return None
    if not body or "id" not in body:
        return None
    avatar = f"https://cdn.discordapp.com/avatars/{body['id']}/{body['avatar']}.png?size=128" if body.get("avatar") else None
    return User(int(body["id"]), body.get("global_name") or body.get("username") or body["id"], avatar)


async def _logout(request: web.Request) -> web.Response:
    """Ends this browser's session, or with the form field `everywhere`, every session this person has. Another site can't ask for either: its form carries no cookie, so the session would live on while this browser lost its cookie."""
    if not _same_site(request):
        raise web.HTTPForbidden(text=FOREIGN_REQUEST)
    token = request.cookies.get(request.app[SITE].session_cookie)
    form = await request.post() if request.content_type == "application/x-www-form-urlencoded" else {}
    user = await request.app[SITE].sessions.read(token) if form.get("everywhere") else None
    if user is not None:
        await request.app[SITE].sessions.end_all(user.id)
    await request.app[SITE].sessions.end(token)
    response = web.HTTPFound("/")
    # A browser ignores a change to a __Host- cookie that doesn't say Secure, and signing out would leave the old cookie behind.
    response.del_cookie(request.app[SITE].session_cookie, path="/", secure=request.app[SITE].secure_cookies)
    raise response


# ---- endpoints every server has ----

async def _guild_info(req: Req) -> dict:
    info = req.ops.describe_guild(req.guild_id)
    if info is None:
        raise ApiError(404, "The bot isn't in that server.")
    admin = req.is_admin
    # A settings page needs a row for an ID it stored, but what Discord hides from this person stays hidden here. Pickers leave these rows out.
    channels = []
    for row in info["channels"]:
        if admin or req.can_see(row["id"]):
            channels.append({**row, "can_post": admin or req.can_post(row["id"]), "can_ping": admin or req.can_ping(row["id"])})
        else:
            channels.append({"id": row["id"], "name": HIDDEN_CHANNEL, "type": row["type"], "parent_id": None, "position": 0, "nsfw": False, "hidden": True, "can_post": False, "can_ping": False})
    return {**info, "channels": channels, "access": req.access, "is_admin": admin, "elevated_roles": bool(req.settings.get(req.guild_id, COMMANDS_MODULE)["elevated_roles"]), "dangerous": list(DANGEROUS_PERMISSIONS)}


async def _get_settings(req: Req) -> dict:
    module = req.param("module")
    if module not in req.settings.schemas:
        raise ApiError(404, "No such settings.")
    return req.settings.schemas[module].masked(req.settings.get(req.guild_id, module))


def _ids_in(value: Any) -> Any:
    if isinstance(value, dict):
        for inner in value.values():
            yield from _ids_in(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _ids_in(inner)
    elif isinstance(value, int) and not isinstance(value, bool):
        yield value
    elif isinstance(value, str) and DISCORD_ID_TEXT.fullmatch(value):
        yield int(value)


def field_naming_another_server(bot: Any, guild_id: int, form: Any) -> str | None:
    """A log channel or role from another server would send this server's data there, since the bot can reach both. The form can hold any ID, so each one is checked against what the other servers have."""
    others = [guild for guild in getattr(bot, "guilds", []) if guild.id != guild_id]
    if not others or not isinstance(form, dict):
        return None
    for field, value in form.items():
        for found in _ids_in(value):
            if any(guild.get_channel_or_thread(found) is not None or guild.get_role(found) is not None for guild in others):
                return field
    return None


def _staff_roles(req: Req, module: str, settings: dict) -> dict[str, set[int]]:
    """The roles a module's settings make count as staff, by field."""
    if module == COMMANDS_MODULE:
        return {"mod_role": {settings["mod_role"]} if settings.get("mod_role") else set()}
    if module == CORE_SETTINGS:
        return {"dashboard_staff_roles": set(settings.get("dashboard_staff_roles") or [])}
    feature = req.bot.module(module)
    return feature.staff_roles(settings) if feature is not None else {}


def _bypass_roles(settings: dict) -> dict[str, set[int]]:
    return {name: set(row.get("bypass_roles") or []) for name, row in (settings.get("overwrites") or {}).items()}


def _answer_channels(settings: dict) -> dict[str, set[int]]:
    """Where these settings send commands' answers, which carry what people wrote."""
    redirects = {row["redirect"] for row in (settings.get("overwrites") or {}).values() if isinstance(row.get("redirect"), int)}
    return {"bot_channels": set(settings.get("bot_channels") or []), "overwrites": redirects}


def _new_in(after: dict[str, set[int]], before: dict[str, set[int]]) -> Any:
    for field, found in after.items():
        for value in sorted(found - before.get(field, set())):
            yield field, value


def _refuse_everyone(req: Req, module: str, before: dict, after: dict) -> None:
    """@everyone holding a power means every member holds it, which no one means to set, so this applies to the owner too."""
    guild = req.bot.get_guild(req.guild_id)
    everyone = {req.guild_id, guild.default_role.id} if guild is not None else {req.guild_id}
    feature = req.bot.module(module)
    pairs = [(_staff_roles(req, module, after), _staff_roles(req, module, before))]
    if feature is not None:
        pairs.append((feature.power_roles(after), feature.power_roles(before)))
    for now, then in pairs:
        for field, role_id in _new_in(now, then):
            if role_id in everyone:
                raise ValidationError({field: access.NOT_EVERYONE})
    if module == COMMANDS_MODULE:
        for _, role_id in _new_in(_bypass_roles(after), _bypass_roles(before)):
            if role_id in everyone:
                raise ValidationError({"overwrites": access.NOT_EVERYONE})


def _check_bypass_roles(req: Req, guild: Any, member: Any, before: dict, after: dict) -> None:
    """A bypass role lets its holders skip a command's Discord permission, so only someone who holds that permission can share it, and only with a role below their own."""
    held = {name for name, on in member.guild_permissions if on}
    for command, role_id in _new_in(_bypass_roles(after), _bypass_roles(before)):
        missing = sorted(req.bot.command_permissions(command) - held)
        if missing:
            raise ValidationError({"overwrites": f"You need {permission_names(missing)} yourself to let a role skip it on {command}."})
        role = guild.get_role(role_id)
        if role is not None and role >= member.top_role:
            raise ValidationError({"overwrites": f"{role.name} sits at or above your highest role, so you can't let it skip the permission on {command}."})


def _check_reach(req: Req, module: str, before: dict, after: dict) -> None:
    """What a form can't do for someone. Manage Server doesn't reach as far in Discord as these settings do, so they'd be a way to give yourself more.

    Below the owner and Administrators, nobody turns on elevated roles, changes who counts as staff, shares a permission they don't hold, names a channel they can't see or post in, saves a ping they couldn't send, adds a role at or above their own highest, or copies what's said in channels they can't read. Nobody at all gives @everyone a power, or sets the bot up to hand out a role above its own, or makes a change the feature's `settings_refusal` names.
    """
    _refuse_everyone(req, module, before, after)
    feature = req.bot.module(module)
    refusal = feature.settings_refusal(req.guild_id, before, after) if feature is not None else None
    if refusal:
        raise ValidationError(refusal)
    guild = req.bot.get_guild(req.guild_id)
    if feature is not None and guild is not None:
        had = feature.roles_given(before)
        for field, role_id in _new_in(feature.roles_given(after), had):
            role = guild.get_role(role_id)
            field = field.partition(":")[0]
            if role is not None and role >= guild.me.top_role:
                raise ValidationError({field: f"{role.name} sits at or above the bot's highest role, so the bot can't hand it out."})
            refusal = feature.role_refusal(guild, role) if role is not None else None
            if refusal:
                raise ValidationError({field: refusal})
    if req.is_admin:
        return
    if module == COMMANDS_MODULE and bool(before.get("elevated_roles")) != bool(after.get("elevated_roles")):
        raise ValidationError({"elevated_roles": access.ONLY_ADMINS})
    for name in sorted(feature.admin_fields if feature is not None else ()):
        if before.get(name) != after.get(name):
            raise ValidationError({name: access.ONLY_ADMINS})
    was_staff = _staff_roles(req, module, before)
    for field, roles in _staff_roles(req, module, after).items():
        if roles != was_staff.get(field, set()):
            raise ValidationError({field: access.ONLY_ADMINS})
    for name, value in after.items():
        for found in set(_ids_in(value)) - set(_ids_in(before.get(name))):
            if not req.can_see(found):
                raise ValidationError({name: "You can't see that channel."})
    member = guild.get_member(req.user.id) if guild else None
    if member is None:
        return
    if module == COMMANDS_MODULE:
        _check_bypass_roles(req, guild, member, before, after)
        for field, channel_id in _new_in(_answer_channels(after), _answer_channels(before)):
            req.channel(channel_id, field, posting=True)
    if feature is None:
        return
    for field, channel_id in _new_in(feature.posted_channels(after), feature.posted_channels(before)):
        req.channel(channel_id, field, posting=True)
    for field, channel_id in _new_in(feature.pinged_channels(after), feature.pinged_channels(before)):
        req.require_ping(channel_id, field)
    had = feature.roles_given(before)
    for field, role_id in _new_in(feature.roles_given(after), had):
        role = guild.get_role(role_id)
        if role is not None and role >= member.top_role:
            raise ValidationError({field.partition(":")[0]: f"{role.name} sits at or above your highest role, so you can't set it up to be handed out."})
    copying = feature.reading_refusal(member, before, after)
    if copying is not None:
        raise ValidationError({copying[0]: copying[1]})


async def _save_settings(req: Req) -> dict:
    module = req.param("module")
    if module not in req.settings.schemas:
        raise ApiError(404, "No such settings.")
    return await store_settings(req, module, await req.json())


async def store_settings(req: Req, module: str, form: Any, *, action: str | None = None) -> dict:
    before = req.settings.get(req.guild_id, module)
    # What the schema will keep, since it reads " 123" as the ID 123 and a check on the raw form wouldn't.
    after = req.settings.schemas[module].clean(form, base=before)
    field = field_naming_another_server(req.bot, req.guild_id, {name: after[name] for name in form})
    if field is not None:
        raise ValidationError({field: "That belongs to another server."})
    _check_reach(req, module, before, after)
    saved = await req.settings.save(req.guild_id, module, form, actor=req.actor, action=action)
    return req.settings.schemas[module].masked(saved)


async def _patch_overwrites(req: Req) -> dict:
    """Changes some commands' rules on the map as it stands now. A page holding an older copy of the whole map would put back what others changed since it loaded."""
    body = await req.json()
    changes = body.get("changes") if isinstance(body, dict) else None
    if not isinstance(changes, dict) or not changes:
        raise ApiError(400, "Say which commands to change.")
    merged = dict(req.settings.get(req.guild_id, COMMANDS_MODULE)["overwrites"])
    for name, rule in changes.items():
        if rule is None:
            merged.pop(name, None)
        else:
            merged[name] = rule
    return await store_settings(req, COMMANDS_MODULE, {"overwrites": merged})


async def _activity(req: Req) -> list[dict]:
    return await req.bot.activity_log.recent(req.guild_id, before_id=whole_number(req.query.get("before"), 2 ** 62))


async def _undo(req: Req) -> dict:
    entry = await req.bot.activity_log.one(req.guild_id, req.int_param("entry_id"))
    if entry is None or not entry["module"] or entry["before"] is None:
        raise ApiError(404, "That change can't be undone.")
    if entry["module"] not in req.settings.schemas:
        raise ApiError(404, "That change can't be undone.")
    _check_reach(req, entry["module"], req.settings.get(req.guild_id, entry["module"]), req.settings.schemas[entry["module"]].merged(entry["before"]))
    restored = await req.settings.restore(req.guild_id, entry["module"], entry["before"], actor=req.actor)
    return req.settings.schemas[entry["module"]].masked(restored)


async def _command_catalog(req: Req) -> dict:
    # Turning these off could lock everyone out, so the page doesn't offer it.
    categories = [{**group, "commands": [row for row in group["commands"] if row["name"] not in NEVER_OFF]} for group in req.bot.command_catalog()]
    return {"categories": [group for group in categories if group["commands"]], "settings": req.settings.get(req.guild_id, "commands"), "permission_labels": PERMISSION_LABELS}


async def _search_members(req: Req) -> list[dict]:
    return req.ops.search_members(req.guild_id, req.query.get("q", ""))


async def _members(req: Req) -> dict:
    """Several people's names in one call, for a page listing up to 500 of them."""
    wanted = [part for part in req.query.get("ids", "").split(",") if part]
    if len(wanted) > MEMBERS_PER_CALL:
        raise ApiError(400, f"One call can ask for at most {MEMBERS_PER_CALL} people.")
    rows = (req.ops.member(req.guild_id, number) for number in (whole_number(part) for part in wanted) if number is not None)
    return {"members": [row for row in rows if row is not None]}


async def _member(req: Req) -> dict:
    row = req.ops.member(req.guild_id, req.int_param("user_id"))
    if row is None:
        raise ApiError(404, "That person isn't in this server.")
    return row


async def _set_nickname(req: Req) -> dict:
    form = await req.json()
    nick = form.get("nick")
    if nick is not None and (not isinstance(nick, str) or len(nick) > 32):
        raise ValidationError({"nick": "A nickname can be at most 32 characters."})
    await req.ops.set_nickname(req.guild_id, nick or None)
    await req.record("nickname.update", detail=nick or "(cleared)")
    return {"ok": True}


def _core_routes(router: Router) -> None:
    router.get("/info", _guild_info, access="staff")
    router.get("/settings/{module}", _get_settings, access="staff")
    router.put("/settings/{module}", _save_settings)
    router.get("/activity", _activity)
    router.post("/activity/{entry_id:\\d+}/undo", _undo)
    router.get("/commands", _command_catalog)
    router.patch("/commands/overwrites", _patch_overwrites)
    router.get("/members", _members, access="staff")
    router.get("/members/search", _search_members, access="staff")
    router.get("/members/{user_id:\\d+}", _member, access="staff")
    router.post("/nickname", _set_nickname)
