"""Statistics: the dashboard's statistics pages and their routes. A mixin the Statistics pack module inherits, so its routes belong to the pack.

Who sees these pages is the server's choice, in dashboard_access: people with Manage Server, the dashboard's staff too, everyone in the
server, or anyone with the link. web/app.py lets a statistics route through for whoever `viewer_level` allows, and nothing else in the
dashboard opens wider. viewer_roles see the pages whatever the choice. Analytics and drilldowns take Administrator, Manage Server or View
Server Insights on top, as Discord asks of bots that show statistics.

The public pages, at /stats/{server}, never need a session and never carry a member's name or ID.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import web

from ..core.stats import carryover, query, web as stats
from ..core.stats import invites as invite_rules
from ..core.stats.carryover import PRESET_KINDS, PRESETS_PER_KIND
from ..core.stats.timeframe import zone, zone_label, zone_names
from ..core.validate import Field, ValidationError, boolean, choice, mapping_of, snowflakes
from ..web.api import ApiError, safe_json

log = logging.getLogger(__name__)

# The parts of the statistics settings a role can be given. "presets" covers saving and deleting presets, and "counters" and "statroles"
# setting up counter channels and stat roles, whose routes check their own part.
AREAS = ("time", "filters", "tracking", "invites", "presets", "commands", "dashboard", "channels", "counters", "statroles")
# The settings each part's roles may change from the dashboard. Every save still runs the dashboard's own checks, so a role list among them
# changes only for an Administrator.
AREA_FIELDS = {
    "time": ("timezone", "lookback", "interval", "time_unit", "smooth_charts"),
    "filters": ("include", "voice_states", "channel_mode", "channels", "voice_mode", "voice_channels", "member_mode", "members", "roles_allow", "roles_block",
                "activity_mode", "activities", "invite_mode", "invites", "fake_mode", "fake_match", "fake_reasons", "emoji_mode", "emoji", "emoji_states",
                "emoji_type_mode", "emoji_type_match", "emoji_types", "emoji_uses"),
    "tracking": ("track_activities", "verify_activities"),
    "invites": ("invite_min_account_minutes", "invite_min_join_minutes", "invite_required_roles", "invite_required_match", "invite_labels"),
    "commands": ("command_access", "command_channel_mode", "command_channels", "override_roles"),
    "dashboard": ("dashboard_access", "viewer_roles", "viewer_filters", "drill_roles", "filter_roles", "preset_read_roles"),
    "channels": ("aggregates", "untracked_channels"),
}
# Role lists that give their holders a power over statistics. Only the owner or an Administrator changes one, and @everyone can't be in one.
POWER_FIELDS = ("area_roles", "override_roles", "viewer_roles", "drill_roles", "filter_roles", "preset_read_roles")
# Reads that miss the cache running at once, each on a connection and a thread of its own. More wait their turn, so a burst of different
# questions, signed in or not, can't open connections without end.
READS_AT_ONCE = 4
# How long one request reads personal panels before it answers with those it has. The page asks again for the rest, so no request waits
# on a dozen panels over all time.
PANEL_READ_SECONDS = 0.5
# The most often a read writes the counts waiting in memory before it reads. Each write is a transaction on disk, so a burst of different
# questions writes once, and the minute loop writes in between.
FLUSH_SECONDS = 10.0
NOT_PUBLIC = "These statistics aren't public."
NO_DRILL = "Analytics, drilldowns and CSV downloads take a role the statistics settings name, with Administrator, Manage Server or View Server Insights."
NO_PRESETS = "Loading presets takes a role the statistics settings name."
NO_PEOPLE = "Searching for people takes the role that opens drilldowns."
# Rows of people a ranking or the invites page lists for someone without the drill right. The whole list, and its CSV file, take that
# right.
LIST_LIMIT = 250
STATS_OFF = "Statistics are off in this server."

WEB_FIELDS: dict[str, Any] = {
    "smooth_charts": Field(False, boolean),  # charts draw smooth lines unless a viewer switches it off
    "viewer_filters": Field(False, boolean),  # everyone who sees the pages changes the query bar, not filter_roles alone
    "area_roles": Field({}, mapping_of(choice(*AREAS), snowflakes(50), len(AREAS))),
    "drill_roles": Field([], snowflakes(50)),  # Analytics, drilldowns and CSV downloads, with Administrator, Manage Server or View Server Insights
    "filter_roles": Field([], snowflakes(50)),  # changing the query bar, and loading presets
    "preset_read_roles": Field([], snowflakes(50)),  # loading presets
}
WEB_MIGRATIONS: list[Any] = [
    """
    CREATE TABLE stat_presets (
        id INTEGER PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        query TEXT NOT NULL,
        picks TEXT NOT NULL DEFAULT '[]',
        created_by INTEGER,
        created_at REAL NOT NULL,
        UNIQUE (guild_id, kind, name)
    );
    CREATE TABLE stat_panels (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        panels TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    );
    """,
]
WEB_MESSAGE_MIGRATIONS: list[Any] = []
# What a backup restore takes for one server: every preset, and one set of panels for each of up to 50,000 people.
WEB_ROW_LIMITS = {"stat_presets": PRESETS_PER_KIND * len(PRESET_KINDS), "stat_panels": 50_000}


@dataclass
class Viewer:
    """Who is looking at a statistics page, and so what it may show."""
    level: str  # full, staff, viewer or public
    guild: Any
    member: Any
    sees: Callable[[int], bool]  # whether a channel's name may show

    @property
    def public(self) -> bool:
        return self.level == "public"


def _avatar_url(user_id: int, avatar: str | None) -> str:
    if avatar:
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.{'gif' if avatar.startswith('a_') else 'png'}?size=64"
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"


def _role_ids(member: Any) -> set[int]:
    found = getattr(member, "_roles", None)
    return set(found) if found is not None else {role.id for role in getattr(member, "roles", [])}


class StatsWeb:
    async def _web_load(self) -> None:
        self._web_cache = stats.Cache()
        self._web_reads = asyncio.Semaphore(READS_AT_ONCE)
        self._web_flushed = float("-inf")

    async def _web_unload(self) -> None:
        pass

    # ---- who sees what ----

    def viewer_level(self, guild_id: int, user_id: int, level: str | None) -> str | None:
        """How far into the statistics pages someone gets, given their dashboard level: "full" and "staff" as the dashboard has them,
        "viewer" to read only, or None. web/app.py asks this for statistics routes and for the server list."""
        if level == "full":
            return "full"
        if not self._web_in_server(guild_id):
            return None
        cfg = self.cfg(guild_id)
        access = cfg["dashboard_access"]
        if level == "staff" and access != "managers":
            return "staff"
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild is not None else None
        if member is None or not cfg["enabled"]:
            return None
        held = _role_ids(member)
        if set(cfg["viewer_roles"]) & held:
            return level or "viewer"
        # A role for any part of the settings reaches the statistics routes, which check the part themselves.
        if access in ("members", "public") or any(set(int(role) for role in roles) & held for roles in cfg["area_roles"].values()):
            return "viewer"
        return None

    async def _web_fresh(self) -> None:
        """Writes what's been counted before a read, at most once every FLUSH_SECONDS."""
        now = time.monotonic()
        if now - self._web_flushed >= FLUSH_SECONDS:
            self._web_flushed = now
            await self.up_to_date()

    def _web_in_server(self, guild_id: int) -> bool:
        """Whether the bot is in this server. Reading a server's settings keeps a copy of them until a restart, so nothing reads them for a
        server the bot isn't in: any number in an address would otherwise cost memory."""
        return self.bot.get_guild(guild_id) is not None or self.bot.ops.guild_summary(guild_id) is not None

    def _web_viewer(self, req: Any) -> Viewer:
        guild = self.bot.get_guild(req.guild_id)
        if req.access == "public":
            return Viewer("public", guild, None, lambda channel_id: _public_sees(guild, channel_id))
        member = guild.get_member(req.user.id) if guild is not None else None
        return Viewer(req.access, guild, member, req.can_see)

    def _web_held(self, req: Any) -> set[int]:
        """The roles of the person asking, none for the public."""
        if req.access == "public":
            return set()
        guild = self.bot.get_guild(req.guild_id)
        member = guild.get_member(req.user.id) if guild is not None else None
        return _role_ids(member) if member is not None else set()

    def _web_may_drill(self, req: Any) -> bool:
        """Analytics, drilldowns and every CSV download. Full access passes. Anyone else needs a role from drill_roles and Administrator,
        Manage Server or View Server Insights, as Discord asks of bots that show statistics. In a server that requires two-factor
        authentication for moderation, someone signed in without it gets none of it."""
        if req.access == "public":
            return False
        if self._web_needs_2fa(req):
            return False
        if req.access == "full":
            return True
        if not (req.holds("view_guild_insights") or req.holds("manage_guild")):
            return False
        return bool(self._web_held(req) & {int(role) for role in self.cfg(req.guild_id)["drill_roles"]})

    def _web_may_filter(self, req: Any) -> bool:
        """Changing the query bar: staff and full access, everyone who sees the pages while viewer_filters is on, a role from filter_roles,
        and whoever may drill. Anyone else reads the view the settings set, whatever the address says."""
        if req.access in ("full", "staff"):
            return True
        cfg = self.cfg(req.guild_id)
        if cfg["viewer_filters"]:
            return True
        if req.access == "public":
            return False
        return bool(self._web_held(req) & {int(role) for role in cfg["filter_roles"]}) or self._web_may_drill(req)

    def _web_may_read_presets(self, req: Any) -> bool:
        """Loading presets: whoever changes the query bar, a role from preset_read_roles, and the presets area, which saves them too."""
        if req.access == "public":
            return False
        if self._web_may_filter(req):
            return True
        return bool(self._web_held(req) & {int(role) for role in self.cfg(req.guild_id)["preset_read_roles"]}) or "presets" in self._web_areas(req)

    def _web_may_search_people(self, req: Any) -> bool:
        """The search for people by name: staff and full access, whoever may drill, and the filters area, whose settings name people."""
        if req.access in ("full", "staff"):
            return True
        return req.access != "public" and (self._web_may_drill(req) or "filters" in self._web_areas(req))

    def _web_needs_2fa(self, req: Any) -> bool:
        guild = self.bot.get_guild(req.guild_id)
        level = getattr(guild, "mfa_level", 0) if guild is not None else 0
        return int(getattr(level, "value", level) or 0) >= 1 and not getattr(req.user, "mfa", False)

    def _web_areas(self, req: Any) -> list[str]:
        if req.access == "full":
            return list(AREAS)
        guild = self.bot.get_guild(req.guild_id)
        member = guild.get_member(req.user.id) if guild is not None else None
        if member is None:
            return []
        held = _role_ids(member)
        return [area for area in AREAS if set(int(role) for role in (self.cfg(req.guild_id)["area_roles"].get(area) or [])) & held]

    def staff_roles(self, settings: dict) -> dict[str, set[int]]:
        """Roles that give a power over statistics count as staff, so only the owner or an Administrator names them, and nobody hands out
        through the bot what they don't hold."""
        out = {"area_roles": {int(role) for roles in (settings.get("area_roles") or {}).values() for role in roles}}
        for name in POWER_FIELDS[1:]:
            out[name] = {int(role) for role in settings.get(name) or []}
        return out

    def _web_require_on(self, req: Any) -> dict:
        """The server's settings, after checking a public read again: public_api's wrapper has already refused a server that isn't public."""
        if req.access == "public" and not self._web_is_public(req.guild_id):
            raise ApiError(404, NOT_PUBLIC)
        return self.cfg(req.guild_id)

    # ---- reading ----

    def _web_role_holders(self, guild_id: int) -> stats.RoleHolders:
        guild = self.bot.get_guild(guild_id)

        def holders(role_ids: Any) -> set[int] | None:
            if guild is None:
                return None
            wanted = {int(role) for role in role_ids}
            return {member.id for member in guild.members if wanted & _role_ids(member)}
        return holders

    def _web_visible(self, req: Any) -> frozenset[int] | None:
        """The channels whose numbers someone who only views may read: those Discord shows them now, or shows everyone for the public, and
        merged channels. A hidden or deleted channel's counts, rows and ID stay out of everything they read. None for staff and full
        access, who read every channel's numbers, and in the demo, which has no server behind it."""
        if req.access not in ("viewer", "public"):
            return None
        guild = self.bot.get_guild(req.guild_id)
        if guild is None:
            return None
        if req.access == "public":
            seen = {channel.id for channel in guild.channels if _public_sees(guild, channel.id)}
        else:
            seen = {channel.id for channel in guild.channels if req.can_see(channel.id)}
        return frozenset(seen | {int(row["id"]) for row in self.cfg(req.guild_id)["aggregates"]})

    async def _web_view_of(self, req: Any, cfg: dict) -> stats.View:
        """The view an address asks for. Someone who may not change the query bar gets the server's own view, or, if they may load
        presets, the view of the preset the address names."""
        params = {name: req.query[name] for name in stats.FIELDS if name in req.query}
        if not self._web_may_filter(req):
            params = await self._web_preset_view(req) if self._web_may_read_presets(req) else {}
        if req.access == "public":
            for name in ("members", "member_mode", "roles_allow", "roles_block", "invites", "invite_mode", "fake_reasons", "fake_mode", "fake_match"):
                params.pop(name, None)
        return stats.read_view(params, cfg)

    async def _web_computed(self, req: Any, name: str, kinds: tuple[str, ...], work: Callable, *, extra: Any = None,
                            then: Callable | None = None, keyed: bool = False) -> tuple:
        """A frame for this request's view and the work's answer for it, from the cache when it was read in the last minute. `then` turns
        the answer into what's kept, once per read, such as naming the 40,000 people of a ranking. It must give the same for anyone who
        reaches the route. `keyed` adds the answer's key, for what's kept beside it."""
        cfg = self._web_require_on(req)
        view = await self._web_view_of(req, cfg)
        visible = self._web_visible(req)
        key = (req.guild_id, name, json.dumps(extra, sort_keys=True, default=str), view.key(), stats.visible_key(visible))

        async def read() -> tuple[stats.Frame, Any]:
            await self._web_fresh()
            async with self._web_reads, self.message_db.reader() as db:
                fr = await stats.frame_for(db, req.guild_id, cfg, view, time.time(), self._web_role_holders(req.guild_id), kinds, visible)
                found = await work(db, fr)
            return fr, (await then(found) if then is not None else found)
        fr, found = await self._web_cache.get(key, read)
        return (fr, found, key) if keyed else (fr, found)

    async def _web_answer(self, key: tuple, payload: dict) -> web.Response:
        """A large answer that's the same for everyone who reaches it, from the bytes kept for it. JSON's encoder holds the interpreter
        for the whole of a ranking of 40,000 people, worker thread or not, so it runs once a minute rather than on every request."""
        async def encode() -> bytes:
            return await asyncio.to_thread(lambda: json.dumps(safe_json(payload)).encode())
        body = await self._web_cache.get((*key, "body"), encode)
        response = web.Response(body=body, content_type="application/json", headers={"Cache-Control": "no-store"})
        response.enable_compression()
        return response

    async def _web_names(self, guild_id: int) -> dict[int, tuple]:
        """Everyone the counts have a name for: (name, display name, avatar, bot, left at, joined at, created at)."""
        async def read() -> dict[int, tuple]:
            found = await self.message_db.all("SELECT user_id, name, display_name, nick, avatar, bot, left_at, joined_at, created_at FROM stat_people WHERE guild_id = ?",
                                              (guild_id,))
            return {row["user_id"]: (row["name"], row["nick"] or row["display_name"] or row["name"], row["avatar"], bool(row["bot"]), row["left_at"],
                                     row["joined_at"], row["created_at"]) for row in found}
        return await self._web_cache.get((guild_id, "people"), read)

    async def _web_channel_rows(self, guild_id: int) -> dict[int, dict]:
        async def read() -> dict[int, dict]:
            return await query.channels(self.message_db, guild_id)
        return await self._web_cache.get((guild_id, "channels"), read)

    def _web_person(self, viewer: Viewer, user_id: int, known: dict[int, tuple]) -> dict:
        if not user_id:
            return {"id": None, "name": "Someone who chose privacy", "display_name": "Someone who chose privacy", "in_server": None, "bot": False, "avatar": None}
        member = viewer.guild.get_member(user_id) if viewer.guild is not None else None
        row = known.get(user_id)
        if member is not None:
            avatar = getattr(getattr(member, "display_avatar", None), "url", None)
            return {"id": user_id, "name": member.name, "display_name": member.display_name, "in_server": True, "bot": bool(member.bot), "avatar": avatar}
        if row is not None:
            # With the server connected, someone it doesn't list has left. Without it, as in the demo, the stored departure says.
            in_server = row[4] is None if viewer.guild is None else False
            return {"id": user_id, "name": row[0], "display_name": row[1], "in_server": in_server, "bot": row[3], "avatar": _avatar_url(user_id, row[2])}
        return {"id": user_id, "name": str(user_id), "display_name": str(user_id), "in_server": False if viewer.guild is not None else None, "bot": False,
                "avatar": _avatar_url(user_id, None)}

    def _web_place(self, viewer: Viewer, channel_id: int, known: dict[int, dict]) -> dict:
        """A channel as this viewer may see it. One they can't see is "Hidden channel", and for the public it carries no ID either."""
        live = viewer.guild.get_channel(channel_id) if viewer.guild is not None else None
        row = known.get(channel_id) or {}
        if live is not None:
            name, kind, parent = live.name, live.type.value, getattr(live, "category_id", None)
        else:
            name, kind, parent = row.get("name"), row.get("type"), row.get("parent_id")
        deleted = live is None and (row.get("deleted_at") is not None or viewer.guild is not None and kind != 999)
        if not self._web_shows(viewer, channel_id, kind, live):
            shown_id = channel_id if viewer.level in ("full", "staff") else None
            return {"id": shown_id, "name": "Hidden channel", "type": kind, "category": None, "hidden": True, "deleted": deleted}
        parent_name = None
        if parent:
            parent_live = viewer.guild.get_channel(parent) if viewer.guild is not None else None
            parent_name = parent_live.name if parent_live is not None else (known.get(parent) or {}).get("name")
            if parent_name is not None and not self._web_shows(viewer, parent, 4, parent_live):
                parent_name = None
        return {"id": channel_id, "name": name or "Deleted channel", "type": kind, "category": parent_name, "hidden": False, "deleted": deleted}

    @staticmethod
    def _web_shows(viewer: Viewer, channel_id: int, kind: Any, live: Any) -> bool:
        """Whether a channel's name shows. A merged channel's always does. Staff see what Discord shows them, and a deleted channel's name
        too, the way the dashboard's logs show it. Someone who only views, signed in or not, sees a name only where Discord shows them the
        channel now, which it does for nobody once a channel is deleted."""
        if kind == 999:
            return True
        if viewer.guild is None:  # the demo, which has no server behind it
            return True
        if viewer.public or viewer.level == "viewer":
            return live is not None and viewer.sees(channel_id)
        return viewer.sees(channel_id)

    # ---- routes ----

    def _web_routes(self, router: Any) -> None:
        for path, handler in (
            ("/statistics/info", self._web_info), ("/statistics/context", self._web_context), ("/statistics/overview", self._web_overview),
            ("/statistics/activity", self._web_activity), ("/statistics/top", self._web_top), ("/statistics/heatmap", self._web_heatmap),
            ("/statistics/status", self._web_status), ("/statistics/apps", self._web_apps), ("/statistics/invites", self._web_invites),
            ("/statistics/emoji", self._web_emoji), ("/statistics/analytics", self._web_analytics), ("/statistics/analytics/unique", self._web_unique),
            ("/statistics/analytics/members", self._web_analytics_members), ("/statistics/analytics/channels", self._web_analytics_channels),
            ("/statistics/drilldown/members", self._web_drill_members), ("/statistics/drilldown/channels", self._web_drill_channels),
            ("/statistics/people", self._web_people), ("/statistics/presets", self._web_presets), ("/statistics/panels", self._web_panels),
            ("/statistics/panels/values", self._web_panel_values), ("/statistics/settings", self._web_settings),
        ):
            router.get(path, handler, access="viewer")
        router.post("/statistics/presets", self._web_preset_save, access="viewer")
        router.get("/statistics/presets/export", self._web_presets_export, access="viewer")
        router.post("/statistics/presets/import", self._web_presets_import, access="viewer")
        router.delete(r"/statistics/presets/{preset_id:\d+}", self._web_preset_delete, access="viewer")
        router.put("/statistics/panels", self._web_panels_save, access="viewer")
        router.put("/statistics/settings", self._web_settings_save, access="viewer")

    def public_api(self, router: Any) -> None:
        """The routes /stats/{server} reads, which web/app.py serves with no session. Every one is wrapped in the same check, so none can
        answer for a server that didn't make its statistics public."""
        for path, handler in (
            ("/statistics/context", self._web_context), ("/statistics/overview", self._web_overview), ("/statistics/activity", self._web_activity),
            ("/statistics/top", self._web_top), ("/statistics/heatmap", self._web_heatmap), ("/statistics/status", self._web_status),
            ("/statistics/apps", self._web_apps), ("/statistics/emoji", self._web_emoji),
        ):
            router.get(path, self._web_public_only(handler))

    def _web_public_only(self, handler: Callable) -> Callable:
        async def guarded(req: Any) -> Any:
            if req.access != "public" or not self._web_is_public(req.guild_id):
                raise ApiError(404, NOT_PUBLIC)
            return await handler(req)
        return guarded

    def _web_is_public(self, guild_id: int) -> bool:
        if not self._web_in_server(guild_id):
            return False
        cfg = self.cfg(guild_id)
        return bool(cfg["enabled"]) and cfg["dashboard_access"] == "public" and self.bot.ops.guild_summary(guild_id) is not None

    async def _web_info(self, req: Any) -> dict:
        """The server's channels, roles and emoji for someone who views statistics only, with what Discord hides from them left out."""
        info = req.ops.describe_guild(req.guild_id)
        if info is None:
            raise ApiError(404, "The bot isn't in that server.")
        channels = [{key: row.get(key) for key in ("id", "name", "type", "parent_id", "position", "nsfw")} for row in info["channels"] if req.can_see(row["id"])]
        roles = [{key: role.get(key) for key in ("id", "name", "color", "position", "managed", "default")} | {"permissions": []} for role in info["roles"]]
        me = info.get("me") or {}
        return {"id": info["id"], "name": info["name"], "icon": info.get("icon"), "member_count": info.get("member_count"), "channels": channels, "roles": roles,
                "emojis": info.get("emojis", []), "me": {"id": me.get("id"), "name": me.get("name"), "avatar": me.get("avatar"), "top_role_position": me.get("top_role_position", 0),
                                                         "permissions": []},
                "access": req.access, "is_admin": False, "elevated_roles": False, "dangerous": []}

    async def _web_context(self, req: Any) -> dict:
        """What every statistics page needs before its numbers: who's looking and what they may do, the server's defaults, and the lists
        the query bar picks from. The public's is the same for every visitor, so it's read once a minute."""
        cfg = self._web_require_on(req)
        if req.access == "public":
            return await self._web_cache.get((req.guild_id, "public-context"), lambda: self._web_context_of(req, cfg))
        return await self._web_context_of(req, cfg)

    async def _web_context_of(self, req: Any, cfg: dict) -> dict:
        viewer = self._web_viewer(req)
        tz = zone(cfg["timezone"])
        now = time.time()
        defaults = stats.address_of({name: stats.default_of(name, cfg) for name in stats.FIELDS if name != "period"})
        guild = viewer.guild
        me = getattr(guild, "me", None) if guild is not None else None
        out = {
            "enabled": cfg["enabled"], "level": viewer.level, "can_drill": self._web_may_drill(req), "needs_2fa": req.access != "public" and self._web_needs_2fa(req), "zone": getattr(tz, "key", "UTC"), "zone_label": zone_label(tz, now),
            "lookback": cfg["lookback"], "defaults": defaults, "smooth": cfg["smooth_charts"], "presences": bool(self.bot.intents.presences),
            "track_activities": cfg["track_activities"], "invites_readable": bool(me.guild_permissions.manage_guild) if me is not None else None,
            "fake_reasons": list(invite_rules.FAKE_REASONS), "public": cfg["dashboard_access"] == "public",
            "public_url": f"{getattr(self.bot, 'public_url', '') or ''}/stats/{req.guild_id}" if cfg["dashboard_access"] == "public" else None,
            "can_filter": self._web_may_filter(req), "can_csv": self._web_may_drill(req),
        }
        if not viewer.public:
            areas = self._web_areas(req)
            out.update({"can_presets": "presets" in areas, "can_read_presets": self._web_may_read_presets(req), "can_settings": bool(areas),
                        "can_counters": "counters" in areas, "can_statroles": "statroles" in areas,
                        "can_people": self._web_may_search_people(req), "list_limit": LIST_LIMIT})
        out["guild"] = {"id": req.guild_id, "name": (req.ops.guild_summary(req.guild_id) or {}).get("name"), "icon": (req.ops.guild_summary(req.guild_id) or {}).get("icon")}
        if viewer.public:
            info = req.ops.describe_guild(req.guild_id) or {}
            known = await self._web_channel_rows(req.guild_id)
            out["channels"] = [self._web_place(viewer, row["id"], known) | {"parent_id": row.get("parent_id"), "position": row.get("position", 0)}
                               for row in info.get("channels", []) if viewer.sees(row["id"])]
            out["emojis"] = info.get("emojis", [])
        names = await query.activity_names(self.message_db, req.guild_id)
        out["activities"] = sorted(({"id": number, "name": row["name"], "kind": stats.activity_kind(row["kind"]), "verified": bool(row["verified"])}
                                    for number, row in names.items()), key=lambda row: row["name"].casefold())
        if not viewer.public:
            labels = cfg["invite_labels"]
            found = await self.message_db.all("SELECT code, vanity, deleted_at FROM stat_invites WHERE guild_id = ? ORDER BY code", (req.guild_id,))
            out["invites"] = [{"code": row["code"], "label": labels.get(row["code"]), "vanity": bool(row["vanity"]), "deleted": row["deleted_at"] is not None} for row in found]
        info_rows = await query.emoji_info(self.message_db, req.guild_id)
        out["emoji"] = [{"key": key, **stats.emoji_face(key, row), "deleted": row.get("deleted_at") is not None} for key, row in info_rows.items()]
        return out

    async def _web_overview(self, req: Any) -> dict:
        fr, found = await self._web_computed(req, "overview", ("members", "messages", "voice"), stats.overview)
        return {"period": fr.describe(), **found}

    def _web_kind(self, req: Any, allowed: tuple[str, ...] = ("messages", "voice")) -> str:
        kind = req.query.get("kind", allowed[0])
        if kind not in allowed:
            raise ApiError(400, "Pick messages or voice.")
        return kind

    async def _web_activity(self, req: Any) -> dict:
        kind = self._web_kind(req)
        fr, found = await self._web_computed(req, "activity", (kind,), lambda db, fr: stats.activity(db, fr, kind), extra=kind)
        return {"period": fr.describe(), "kind": kind, **found}

    async def _web_top(self, req: Any) -> web.Response:
        """A whole ranking, everyone or every channel with something in the period, in columns. Large, so it goes compressed."""
        kind = self._web_kind(req)
        by = req.query.get("by", "channels")
        if by not in ("members", "channels"):
            raise ApiError(400, "Rank members or channels.")
        viewer = self._web_viewer(req)
        if by == "members" and viewer.public:
            raise ApiError(403, "Public statistics don't list members.")
        if by == "members":
            async def named(ranked: list) -> list:
                known = await self._web_names(req.guild_id)
                out = []
                for rank, (user_id, value, channels) in enumerate(ranked, start=1):
                    person = self._web_person(viewer, user_id, known)
                    out.append([rank, user_id, person["name"], person["display_name"], value, channels, person["in_server"], person["bot"]])
                return out
            fr, rows, key = await self._web_computed(req, "top-members", (kind,), lambda db, fr: stats.top_members(db, fr, kind), extra=kind, then=named,
                                                     keyed=True)
            columns = ["rank", "id", "name", "display_name", "value", "channels", "in_server", "bot"]
            limited = len(rows) > LIST_LIMIT and not self._web_may_drill(req)
            return await self._web_answer((*key, limited), {"period": fr.describe(), "kind": kind, "by": by, "columns": columns,
                                                            "rows": rows[:LIST_LIMIT] if limited else rows, "limited": limited})
        else:
            fr, ranked = await self._web_computed(req, "top-channels", (kind,), lambda db, fr: stats.top_channels(db, fr, kind), extra=kind)
            limited = False
            known = await self._web_channel_rows(req.guild_id)
            columns = ["rank", "id", "name", "type", "category", "value", "members", "deleted", "hidden"]
            rows = []
            for rank, (channel_id, value, people) in enumerate(ranked, start=1):
                place = self._web_place(viewer, channel_id, known)
                rows.append([rank, place["id"], place["name"], place["type"], place["category"], value, people, place["deleted"], place["hidden"]])
        return await _compressed({"period": fr.describe(), "kind": kind, "by": by, "columns": columns, "rows": rows, "limited": limited})

    async def _web_heatmap(self, req: Any) -> dict:
        kind = self._web_kind(req, ("messages", "voice", "status"))
        fr, found = await self._web_computed(req, "heatmap", (kind if kind != "status" else "status",), lambda db, fr: stats.heatmap(db, fr, kind), extra=kind)
        return {"period": fr.describe(), "kind": kind, **found}

    async def _web_status(self, req: Any) -> dict:
        fr, found = await self._web_computed(req, "status", ("status",), stats.statuses)
        return {"period": fr.describe(), **found, "presences": bool(self.bot.intents.presences)}

    async def _web_apps(self, req: Any) -> dict:
        cfg = self._web_require_on(req)
        fr, found = await self._web_computed(req, "apps", ("activities",), stats.activities)
        return {"period": fr.describe(), **found, "tracking": cfg["track_activities"], "presences": bool(self.bot.intents.presences)}

    def _web_join_facts(self, guild_id: int, cfg: dict) -> stats.JoinFacts:
        guild = self.bot.get_guild(guild_id)

        def member(user_id: int) -> Any:
            if guild is None:
                return _Present()
            return guild.get_member(user_id)
        return stats.JoinFacts(cfg, member)

    async def _web_invites(self, req: Any) -> dict:
        cfg = self._web_require_on(req)
        viewer = self._web_viewer(req)
        fr, found = await self._web_computed(req, "invites", ("members",), lambda db, fr: stats.invites(db, fr, self._web_join_facts(req.guild_id, cfg)))
        known = await self._web_names(req.guild_id)
        places = await self._web_channel_rows(req.guild_id)
        people = _People(self, viewer, known)
        whole = self._web_may_drill(req)
        inviters = found["inviters"] if whole else found["inviters"][:LIST_LIMIT]
        listed_joins = found["joins"] if whole else found["joins"][:LIST_LIMIT]
        invites = [{**row, "channel": self._web_place(viewer, row["channel_id"], places) if row["channel_id"] else None} for row in found["invites"]]
        for row in invites:
            people.add(row["inviter_id"])
        for row in inviters:
            people.add(row["user_id"])
        joins = [[row["at"], people.add(row["user_id"]), row["code"], people.add(row["inviter_id"]), row["reasons"], row["left_at"]] for row in listed_joins]
        guild = viewer.guild
        me = getattr(guild, "me", None) if guild is not None else None
        return await _compressed({"period": fr.describe(), "series": [{**row, "label": cfg["invite_labels"].get(row["code"])} for row in found["series"]],
                            "invites": invites, "inviters": inviters, "joins": {"columns": ["at", "user_id", "code", "inviter_id", "reasons", "left_at"], "rows": joins},
                            "people": people.named, "labels": cfg["invite_labels"], "total": found["total"], "fake": found["fake"],
                            "truncated": found["truncated"] or len(listed_joins) < len(found["joins"]), "limited": not whole and (len(inviters) < len(found["inviters"]) or len(listed_joins) < len(found["joins"])),
                            "readable": bool(me.guild_permissions.manage_guild) if me is not None else None})

    async def _web_emoji(self, req: Any) -> dict:
        fr, found = await self._web_computed(req, "emoji", ("emoji",), stats.emoji)
        return {"period": fr.describe(), **found}

    def _web_require_drill(self, req: Any) -> None:
        if not self._web_may_drill(req):
            raise ApiError(403, NO_DRILL)

    async def _web_analytics(self, req: Any) -> dict:
        self._web_require_drill(req)
        guild = self.bot.get_guild(req.guild_id)
        present = frozenset(member.id for member in guild.members) if guild is not None else None
        fr, found = await self._web_computed(req, "analytics", ("messages", "voice"), lambda db, fr: stats.analytics(db, fr, present), extra=present is not None)
        return {"period": fr.describe(), **found, "roles_on": fr.roles_on}

    async def _web_unique(self, req: Any) -> dict:
        self._web_require_drill(req)
        fr, found = await self._web_computed(req, "unique", ("messages", "voice"), stats.analytics_unique)
        return {"period": fr.describe(), **found}

    async def _web_analytics_members(self, req: Any) -> web.Response:
        self._web_require_drill(req)
        viewer = self._web_viewer(req)

        async def named(found: list) -> list:
            known = await self._web_names(req.guild_id)
            out = []
            for row in found:
                person = self._web_person(viewer, row["user_id"], known)
                stored = known.get(row["user_id"]) or (None,) * 7
                member = viewer.guild.get_member(row["user_id"]) if viewer.guild is not None else None
                joined = member.joined_at.timestamp() if member is not None and getattr(member, "joined_at", None) else stored[5]
                created = member.created_at.timestamp() if member is not None else stored[6]
                out.append([row["user_id"], person["name"], person["display_name"], row["messages"], row["voice"], row["message_channels"], row["voice_channels"],
                            person["in_server"], person["bot"], joined, created])
            return out
        fr, rows, key = await self._web_computed(req, "analytics-members", ("messages", "voice"), stats.analytics_people, then=named, keyed=True)
        columns = ["id", "name", "display_name", "messages", "voice", "message_channels", "voice_channels", "in_server", "bot", "joined_at", "created_at"]
        return await self._web_answer(key, {"period": fr.describe(), "columns": columns, "rows": rows})

    async def _web_analytics_channels(self, req: Any) -> web.Response:
        self._web_require_drill(req)
        viewer = self._web_viewer(req)
        fr, found = await self._web_computed(req, "analytics-channels", ("messages", "voice"), stats.analytics_channels)
        known = await self._web_channel_rows(req.guild_id)
        columns = ["id", "name", "type", "category", "messages", "voice", "message_members", "voice_members", "deleted", "created_at", "hidden"]
        rows = []
        for row in found:
            place = self._web_place(viewer, row["channel_id"], known)
            created = (known.get(row["channel_id"]) or {}).get("created_at")
            rows.append([place["id"], place["name"], place["type"], place["category"], row["messages"], row["voice"], row["message_members"], row["voice_members"],
                         place["deleted"], created if not place["hidden"] else None, place["hidden"]])
        return await _compressed({"period": fr.describe(), "columns": columns, "rows": rows})

    def _web_ids(self, req: Any) -> list[int]:
        found = stats.read_field("members", req.query.get("ids", ""))
        if found is None:
            raise ApiError(400, "Those aren't IDs.")
        return found[:stats.DRILL_LIMIT]

    async def _web_drill_members(self, req: Any) -> dict:
        self._web_require_drill(req)
        viewer = self._web_viewer(req)
        ids = self._web_ids(req)
        fr, found = await self._web_computed(req, "drill-members", ("messages", "voice"), lambda db, fr: stats.member_drilldown(db, fr, ids), extra=ids)
        known = await self._web_names(req.guild_id)
        places = await self._web_channel_rows(req.guild_id)
        out = []
        for row in found["members"]:
            person = self._web_person(viewer, row["user_id"], known)
            stored = known.get(row["user_id"]) or (None,) * 7
            member = viewer.guild.get_member(row["user_id"]) if viewer.guild is not None else None
            out.append({
                **row, "person": person,
                "created_at": member.created_at.timestamp() if member is not None else stored[6],
                "joined_at": member.joined_at.timestamp() if member is not None and getattr(member, "joined_at", None) else stored[5],
                "left_at": None if member is not None else stored[4],
                "last_message": _with_place(row["last_message"], viewer, places, self._web_place),
                "last_voice": _with_place(row["last_voice"], viewer, places, self._web_place),
                "channels": [{**part, "channel": self._web_place(viewer, part["channel_id"], places)} for part in row["channels"]],
            })
        return {"period": fr.describe(), "members": out}

    async def _web_drill_channels(self, req: Any) -> dict:
        self._web_require_drill(req)
        viewer = self._web_viewer(req)
        ids = self._web_ids(req)
        fr, found = await self._web_computed(req, "drill-channels", ("messages", "voice"), lambda db, fr: stats.channel_drilldown(db, fr, ids), extra=ids)
        known = await self._web_names(req.guild_id)
        places = await self._web_channel_rows(req.guild_id)
        people = _People(self, viewer, known)
        out = []
        for row in found["channels"]:
            place = self._web_place(viewer, row["channel_id"], places)
            if place["hidden"]:
                out.append({"channel_id": row["channel_id"], "channel": place, "hidden": True})
                continue
            stored = places.get(row["channel_id"]) or {}
            out.append({
                **row, "channel": place, "created_at": stored.get("created_at"),
                "last_message": {**row["last_message"], "person": self._web_person(viewer, row["last_message"]["user_id"] or 0, known)} if row["last_message"] else None,
                "last_voice": {**row["last_voice"], "person": self._web_person(viewer, row["last_voice"]["user_id"] or 0, known)} if row["last_voice"] else None,
                "members": [[people.add(part["user_id"]), part["messages"], part["voice"]] for part in row["members"]],
            })
        return await _compressed({"period": fr.describe(), "channels": out, "people": people.named})

    async def _web_people(self, req: Any) -> dict:
        """People to pick for a filter or a drilldown, by name or ID: everyone here now and everyone the counts have a name for."""
        self._web_require_on(req)
        if not self._web_may_search_people(req):
            raise ApiError(403, NO_PEOPLE)
        viewer = self._web_viewer(req)
        needle = (req.query.get("q") or "").strip().casefold()
        if len(needle) < 2:
            return {"people": []}
        known = await self._web_names(req.guild_id)
        found: dict[int, dict] = {}
        number = stats.id_of(needle) if needle.isdigit() else None
        guild = viewer.guild
        if guild is not None:
            for member in guild.members:
                if member.id == number or needle in member.name.casefold() or needle in member.display_name.casefold():
                    found[member.id] = self._web_person(viewer, member.id, known)
                    if len(found) >= 25:
                        break
        for user_id, row in known.items():
            if len(found) >= 25:
                break
            if user_id in found:
                continue
            if user_id == number or needle in (row[0] or "").casefold() or needle in (row[1] or "").casefold():
                found[user_id] = self._web_person(viewer, user_id, known)
        return {"people": list(found.values())}

    # ---- presets ----

    def _web_may_edit_presets(self, req: Any) -> bool:
        return "presets" in self._web_areas(req)

    async def _web_preset_view(self, req: Any) -> dict:
        """The saved address of the filter preset an address names with `preset`, or nothing."""
        number = req.query.get("preset", "")
        if not number.isdigit() or len(number) > 18:
            return {}
        row = await self.db.one("SELECT query FROM stat_presets WHERE guild_id = ? AND id = ? AND kind = 'filters'", (req.guild_id, int(number)))
        return _loaded(row["query"], {}) if row else {}

    async def _web_presets(self, req: Any) -> dict:
        self._web_require_on(req)
        if not self._web_may_read_presets(req):
            raise ApiError(403, NO_PRESETS)
        found = await self.db.all("SELECT id, name, kind, query, picks, created_by, created_at FROM stat_presets WHERE guild_id = ? ORDER BY kind, name COLLATE NOCASE",
                                  (req.guild_id,))
        out: dict[str, list] = {kind: [] for kind in PRESET_KINDS}
        drill = self._web_may_drill(req)
        for row in found:
            if row["kind"] not in out or row["kind"] != "filters" and not drill:
                continue
            out[row["kind"]].append({"id": row["id"], "name": row["name"], "query": _loaded(row["query"], {}), "picks": [str(pick) for pick in _loaded(row["picks"], [])],
                                     "created_at": row["created_at"]})
        return {**out, "can_edit": self._web_may_edit_presets(req)}

    async def _web_preset_save(self, req: Any) -> dict:
        self._web_require_on(req)
        if not self._web_may_edit_presets(req):
            raise ApiError(403, "Saving presets takes Manage Server or a role the statistics settings allow.")
        form = await req.json()
        name = " ".join(str(form.get("name") or "").split())
        if not name:
            raise ValidationError({"name": "Name the preset."})
        if len(name) >= 20:
            raise ValidationError({"name": "Keep the name under 20 characters."})
        kind = form.get("kind", "filters")
        if kind not in PRESET_KINDS:
            raise ValidationError({"kind": "Pick filters, members or channels."})
        if kind != "filters" and not self._web_may_drill(req):
            raise ApiError(403, NO_DRILL)
        params = form.get("query") if isinstance(form.get("query"), dict) else {}
        address = stats.clean_address(params)
        picks = stats.read_field("members", form.get("picks") or []) if kind != "filters" else []
        if picks is None:
            raise ValidationError({"picks": "Those aren't IDs."})
        count = await self.db.scalar("SELECT COUNT(*) FROM stat_presets WHERE guild_id = ? AND kind = ?", (req.guild_id, kind))
        if count >= PRESETS_PER_KIND:
            raise ValidationError({"name": f"A server keeps up to {PRESETS_PER_KIND} presets of each kind. Delete one first."})
        taken = await self.db.scalar("SELECT id FROM stat_presets WHERE guild_id = ? AND kind = ? AND name = ? COLLATE NOCASE", (req.guild_id, kind, name))
        if taken is not None:
            raise ValidationError({"name": "A preset already has that name. Pick another or delete that one first."})
        number = await self.db.insert("INSERT INTO stat_presets (guild_id, name, kind, query, picks, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                      (req.guild_id, name, kind, json.dumps(address, sort_keys=True), json.dumps(picks[:stats.DRILL_LIMIT]), req.user.id, time.time()))
        await req.record("statistics.preset.create", detail=name)
        return {"id": number, "name": name, "kind": kind, "query": address, "picks": [str(pick) for pick in picks[:stats.DRILL_LIMIT]]}

    async def _web_presets_export(self, req: Any) -> dict:
        """Every preset here in Captain Guild's own format, for a file another server can import."""
        self._web_require_on(req)
        if not self._web_may_read_presets(req):
            raise ApiError(403, NO_PRESETS)
        found = await carryover.export_presets(self.db, req.guild_id)
        return {"presets": [preset for preset in found if preset["kind"] == "filters" or self._web_may_drill(req)]}

    async def _web_presets_import(self, req: Any) -> dict:
        """Presets from a file in the own format, each replacing one of the same kind and name."""
        self._web_require_on(req)
        if not self._web_may_edit_presets(req):
            raise ApiError(403, "Saving presets takes Manage Server or a role the statistics settings allow.")
        presets = (await req.json()).get("presets")
        if not isinstance(presets, list) or len(presets) > PRESETS_PER_KIND * len(PRESET_KINDS):
            raise ValidationError({"presets": f"Send a list of up to {PRESETS_PER_KIND * len(PRESET_KINDS)} presets."})
        if not self._web_may_drill(req):
            presets = [preset for preset in presets if not isinstance(preset, dict) or (preset.get("kind") or "filters") == "filters"]
        done = await carryover.import_presets(self.db, req.guild_id, presets, actor_id=req.user.id)
        await req.record("statistics.preset.import", detail=f"{sum(1 for row in done if 'error' not in row)} presets")
        return {"presets": done}

    async def _web_preset_delete(self, req: Any) -> dict:
        self._web_require_on(req)
        if not self._web_may_edit_presets(req):
            raise ApiError(403, "Deleting presets takes Manage Server or a role the statistics settings allow.")
        row = await self.db.one("SELECT id, name FROM stat_presets WHERE guild_id = ? AND id = ?", (req.guild_id, req.int_param("preset_id")))
        if row is None:
            raise ApiError(404, "That preset isn't there anymore.")
        await self.db.run("DELETE FROM stat_presets WHERE guild_id = ? AND id = ?", (req.guild_id, row["id"]))
        await req.record("statistics.preset.delete", detail=row["name"])
        return {"ok": True}

    # ---- personal panels ----

    async def _web_panels(self, req: Any) -> dict:
        self._web_require_on(req)
        row = await self.db.one("SELECT panels FROM stat_panels WHERE guild_id = ? AND user_id = ?", (req.guild_id, req.user.id))
        return {"panels": _loaded(row["panels"], []) if row else [], "most": stats.PANEL_LIMIT}

    async def _web_panels_save(self, req: Any) -> dict:
        """Replaces this person's own panels here. Nobody else's change."""
        self._web_require_on(req)
        form = await req.json()
        try:
            panels = stats.clean_panels(form.get("panels"))
        except ValueError as error:
            raise ValidationError({"panels": str(error)}) from error
        if panels:
            await self.db.run("INSERT INTO stat_panels (guild_id, user_id, panels, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (guild_id, user_id) "
                              "DO UPDATE SET panels = excluded.panels, updated_at = excluded.updated_at", (req.guild_id, req.user.id, json.dumps(panels), time.time()))
        else:
            await self.db.run("DELETE FROM stat_panels WHERE guild_id = ? AND user_id = ?", (req.guild_id, req.user.id))
        return {"panels": panels, "most": stats.PANEL_LIMIT}

    async def _web_panel_values(self, req: Any) -> dict:
        """This person's panel values from `from` on: each one read in the last minute, and as many more as PANEL_READ_SECONDS of
        reading gets through, at least one. `next` is where the page asks again, or None once it has them all."""
        cfg = self._web_require_on(req)
        row = await self.db.one("SELECT panels FROM stat_panels WHERE guild_id = ? AND user_id = ?", (req.guild_id, req.user.id))
        panels = _loaded(row["panels"], []) if row else []
        start = req.query.get("from", "0")
        if not start.isdigit():
            raise ApiError(400, "Ask from a panel's place in the list.")
        start = int(start)
        known = await self._web_channel_rows(req.guild_id)
        visible = self._web_visible(req)
        channel_count = sum(1 for channel_id, info in known.items() if info.get("deleted_at") is None and info.get("type") != 4 and (visible is None or channel_id in visible))
        out: list[dict] = []
        began = time.monotonic()
        async with contextlib.AsyncExitStack() as stack:
            reader = None
            for panel in panels[start:]:
                key = (req.guild_id, "panel", json.dumps(panel, sort_keys=True), cfg["timezone"], stats.visible_key(visible))
                found = self._web_cache.peek(key)
                if found is stats.MISSING:
                    if out and time.monotonic() - began >= PANEL_READ_SECONDS:
                        break
                    if reader is None:
                        await self._web_fresh()
                        await stack.enter_async_context(self._web_reads)
                        reader = await stack.enter_async_context(self.message_db.reader())
                    found = await self._web_cache.get(key, functools.partial(self._web_panel, reader, req.guild_id, cfg, panel, channel_count, visible))
                out.append(found)
        done = start + len(out)
        return {"values": out, "from": start, "next": done if done < len(panels) else None}

    async def _web_panel(self, db: Any, guild_id: int, cfg: dict, panel: Any, channel_count: int, visible: frozenset[int] | None) -> dict:
        try:
            return await stats.panel_value(db, guild_id, cfg, stats.clean_panel(panel), time.time(), self._web_role_holders(guild_id), channel_count, visible)
        except ValueError:
            return {"number": None, "series": [], "label": ""}

    # ---- settings ----

    async def _web_settings(self, req: Any) -> dict:
        """The statistics settings, for anyone who may change some of them, with which parts they may change."""
        areas = self._web_areas(req)
        if not areas:
            raise ApiError(403, "Changing statistics settings takes Manage Server or a role the statistics settings allow.")
        cfg = self.cfg(req.guild_id)
        guild = self.bot.get_guild(req.guild_id)
        return {"settings": self.schema.masked(cfg), "areas": areas, "all": req.access == "full", "is_admin": req.is_admin,
                "presences": bool(self.bot.intents.presences), "public_url": f"{getattr(self.bot, 'public_url', '') or ''}/stats/{req.guild_id}",
                "invites_readable": bool(guild.me.guild_permissions.manage_guild) if guild is not None and guild.me is not None else None,
                "zones": _zone_choices()}

    async def _web_settings_save(self, req: Any) -> dict:
        """Saves statistics settings. Manage Server changes any of them, and anyone else only the fields in the areas their roles hold. Either
        way the save runs every check the dashboard's own settings save runs (web/app.py store_settings), Administrator-only fields included."""
        areas = self._web_areas(req)
        if not areas:
            raise ApiError(403, "Changing statistics settings takes Manage Server or a role the statistics settings allow.")
        form = await req.json()
        if req.access != "full":
            allowed = {name for area in areas for name in AREA_FIELDS.get(area, ())}
            refused = sorted(name for name in form if name not in allowed)
            if refused:
                raise ApiError(403, "Your roles don't let you change that setting.", {name: "Your roles don't let you change this." for name in refused})
        saved = await req.save_settings(self.key, form)
        self._web_cache.forget(req.guild_id)
        return {"settings": saved, "areas": areas}


class _People:
    """The people a long answer names, each once, so thousands of rows carry only an ID."""

    def __init__(self, owner: Any, viewer: Viewer, known: dict) -> None:
        self.owner = owner
        self.viewer = viewer
        self.known = known
        self.named: dict[str, dict] = {}

    def add(self, user_id: int | None) -> int | None:
        if user_id is not None and str(user_id) not in self.named:
            person = self.owner._web_person(self.viewer, user_id, self.known)
            person.pop("avatar", None)
            self.named[str(user_id)] = person
        return user_id


class _Present:
    """A member the demo, which has no Discord behind it, treats as still in the server with nothing to count against them."""
    roles: list = []
    pending = False
    flags = None


def _public_sees(guild: Any, channel_id: int) -> bool:
    """Whether everyone in the server can see a channel, which is what the public pages show by name."""
    if guild is None:
        return True
    channel = guild.get_channel(channel_id)
    if channel is None:
        return False
    try:
        return bool(channel.permissions_for(guild.default_role).view_channel)
    except Exception:  # a channel whose permissions can't be read stays hidden
        return False


def _with_place(found: dict | None, viewer: Viewer, places: dict, place: Callable) -> dict | None:
    if found is None:
        return None
    return {**found, "channel": place(viewer, found["channel_id"], places)}


def _zone_choices() -> list[list[str]]:
    """Every time zone the settings accept, with its offset from UTC now, for the settings page's search."""
    now = time.time()
    names = sorted(name for name in zone_names() if not name.startswith(("posix/", "right/")) and name not in ("localtime", "posixrules", "Factory"))
    return [[name, zone_label(zone(name), now).split(" (")[0]] for name in names]


def _loaded(text: Any, fallback: Any) -> Any:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


async def _compressed(data: Any) -> web.Response:
    """A large answer, gzipped when the browser takes it. A ranking of 40,000 people is a few megabytes as text and a few hundred
    kilobytes this way. It's written out on a worker thread, since that takes a quarter second the bot shouldn't wait through."""
    text = await asyncio.to_thread(lambda: json.dumps(safe_json(data)))
    response = web.Response(text=text, content_type="application/json", headers={"Cache-Control": "no-store"})
    response.enable_compression()
    return response
