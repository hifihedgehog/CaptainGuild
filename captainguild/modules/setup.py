"""The quick setup wizard's server side.

The wizard fills the same settings the other pages use, through the same settings store. What lives here is what no other page has:
the automod presets, the sets of ready-made roles, and "make one for me" for a mute role and a channel.
"""
from __future__ import annotations

import re
from typing import Any

from ..core.module import Module
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import role_is_safe
from ..core.validate import DELETE, TIMEOUT, WARN, ValidationError
from ..web.api import ApiError

HOUR = 3600
DAY = 86400


def _rule(actions: int, seconds: int = 0, **fields: Any) -> dict:
    return {"enabled": True, "punishment": {"actions": actions, "seconds": seconds, "purge_days": 0}, **fields}


# Timeouts instead of mutes, because they work with no mute role set up, which a new server may not have yet.
AUTOMOD_PRESETS: dict[str, dict] = {
    "minimal": {
        "mentions": _rule(DELETE | TIMEOUT, HOUR, rate=[10, 5]),
    },
    "medium": {
        "mentions": _rule(DELETE | TIMEOUT, HOUR, rate=[8, 5]),
        "invites": _rule(DELETE | WARN),
        "spam": _rule(DELETE, rate=[6, 5]),
        "attachments": _rule(DELETE, rate=[5, 10]),
    },
    "high": {
        "mentions": _rule(DELETE | TIMEOUT, DAY, rate=[5, 5]),
        "invites": _rule(DELETE | WARN),
        "spam": _rule(DELETE | TIMEOUT, 10 * 60, rate=[5, 5]),
        "attachments": _rule(DELETE | WARN, rate=[3, 10]),
        "caps": _rule(DELETE, threshold=70),
        "links": _rule(DELETE, mode="no_role"),
        "warns": _rule(TIMEOUT, DAY, threshold=3),
        "delete_files": True,
    },
}
PRESET_CHOICES = [
    {"key": "none", "label": "Off or custom", "hint": "Leaves automod as it is. You can always change it later."},
    {"key": "minimal", "label": "Minimal", "hint": "Times out people who spam mentions. Nothing else."},
    {"key": "medium", "label": "Medium", "hint": "Also removes invites and floods of messages or files. A good fit for most servers."},
    {"key": "high", "label": "High", "hint": "Also removes shouting, links from people with no roles and unsafe files, and times out after three warnings."},
]

# Each emoji shows as color by default, so none of them needs an invisible selector character to work as a reaction.
ROLE_TEMPLATES: dict[str, dict] = {
    "colors": {"label": "Colors", "title": "Pick a color", "color": 0x49D6DF, "roles": [
        ("Red", 0xFF7070, "\N{LARGE RED CIRCLE}"), ("Purple", 0xAA88FF, "\N{LARGE PURPLE CIRCLE}"), ("Green", 0x70E070, "\N{LARGE GREEN CIRCLE}"),
        ("Pink", 0xFF88D8, "\N{CHERRY BLOSSOM}"), ("Orange", 0xFFA858, "\N{LARGE ORANGE CIRCLE}"), ("Yellow", 0xF6E058, "\N{LARGE YELLOW CIRCLE}"), ("Blue", 0x60A8FF, "\N{LARGE BLUE CIRCLE}"),
    ]},
    "continents": {"label": "Continents", "title": "Where are you from?", "color": 0x2FBEFF, "roles": [
        ("Europe", 0x2FBEFF, "\N{EUROPEAN CASTLE}"), ("North America", 0x2FBEFF, "\N{STATUE OF LIBERTY}"), ("South America", 0x2FBEFF, "\N{PALM TREE}"),
        ("Asia", 0x2FBEFF, "\N{JAPANESE CASTLE}"), ("Oceania", 0x2FBEFF, "\N{KANGAROO}"), ("Africa", 0x2FBEFF, "\N{LION FACE}"),
    ]},
    "zodiac": {"label": "Zodiac signs", "title": "What's your sign?", "color": 0x9B59B6, "roles": [
        ("Aries", 0x9B59B6, "\N{ARIES}"), ("Taurus", 0x9B59B6, "\N{TAURUS}"), ("Gemini", 0x9B59B6, "\N{GEMINI}"), ("Cancer", 0x9B59B6, "\N{CANCER}"),
        ("Leo", 0x9B59B6, "\N{LEO}"), ("Virgo", 0x9B59B6, "\N{VIRGO}"), ("Libra", 0x9B59B6, "\N{LIBRA}"), ("Scorpio", 0x9B59B6, "\N{SCORPIUS}"),
        ("Sagittarius", 0x9B59B6, "\N{SAGITTARIUS}"), ("Capricorn", 0x9B59B6, "\N{CAPRICORN}"), ("Aquarius", 0x9B59B6, "\N{AQUARIUS}"), ("Pisces", 0x9B59B6, "\N{PISCES}"),
    ]},
    "platforms": {"label": "Platforms", "title": "What do you play on?", "color": 0x3F7FDF, "roles": [
        ("PC", 0xEB6B36, "\N{PERSONAL COMPUTER}"), ("Xbox", 0x107C10, "\N{LARGE GREEN CIRCLE}"), ("PlayStation", 0x3F7FDF, "\N{LARGE BLUE CIRCLE}"),
        ("Switch", 0xFF1F2B, "\N{LARGE RED CIRCLE}"), ("Mobile", 0x9FBC26, "\N{MOBILE PHONE}"),
    ]},
    "rpg": {"label": "RPG roles", "title": "What do you play as?", "color": 0x00CCFF, "roles": [
        ("Tank", 0xB0B0DB, "\N{BRICK}"), ("DPS", 0xFEF468, "\N{BOW AND ARROW}"), ("Healer", 0x70E070, "\N{GREEN HEART}"),
    ]},
    "pronouns": {"label": "Pronouns", "title": "What are your pronouns?", "color": 0xFF8000, "roles": [
        ("He/Him", 0x60A8FF, "\N{LARGE BLUE CIRCLE}"), ("She/Her", 0xFF7070, "\N{LARGE RED CIRCLE}"), ("They/Them", 0xAA88FF, "\N{LARGE PURPLE CIRCLE}"), ("Ask", 0x78B059, "\N{BLACK QUESTION MARK ORNAMENT}"),
    ]},
    "league": {"label": "League lanes", "title": "Which lane do you play?", "color": 0xFFDC00, "roles": [
        ("Top", 0xB0B0DB, "\N{TOP WITH UPWARDS ARROW ABOVE}"), ("Jungle", 0x609C46, "\N{DECIDUOUS TREE}"), ("Mid", 0x9FCCDA, "\N{DIRECT HIT}"),
        ("Bot", 0xB25656, "\N{ROBOT FACE}"), ("Support", 0x63BC9E, "\N{SPARKLING HEART}"), ("Fill", 0x49D6DF, "\N{GAME DIE}"),
    ]},
}
CHANNEL_NAME_MAX = 100


def clean_channel_name(value: Any) -> str:
    name = re.sub(r"[^\w-]+", "-", value.strip().lower()).strip("-") if isinstance(value, str) else ""
    if not name or len(name) > CHANNEL_NAME_MAX:
        raise ValidationError({"name": f"A channel name takes 1 to {CHANNEL_NAME_MAX} letters, numbers or dashes."})
    return name


class Setup(Module):
    key = "setup"
    category = "Config"

    def api(self, router: Any) -> None:
        router.get("/setup", self.api_state)
        router.post("/setup/automod", self.api_automod)
        router.post("/setup/role-template", self.api_role_template)
        router.post("/setup/mute-role", self.api_mute_role)
        router.post("/setup/modlog-channel", self.api_modlog_channel)
        router.post("/setup/channel", self.api_channel)

    def _has(self, module: str) -> bool:
        return module in self.bot.settings.schemas

    def _settings_of(self, guild_id: int, module: str) -> dict:
        return self.bot.settings.get(guild_id, module) if self._has(module) else {}

    async def api_state(self, req: Any) -> dict:
        greetings = self._settings_of(req.guild_id, "greetings")
        moderation = self._settings_of(req.guild_id, "moderation")
        return {
            "prefixes": self.bot.settings.get(req.guild_id, "core")["prefixes"],
            "welcome_channel": greetings.get("welcome_channel"),
            "welcome": greetings.get("welcome"),
            "mute_role": moderation.get("mute_role"),
            "modlog_channel": moderation.get("modlog_channel"),
            "presets": PRESET_CHOICES,
            "templates": [
                {"key": key, "label": row["label"], "title": row["title"], "color": row["color"], "roles": [{"name": name, "emoji": emoji} for name, _, emoji in row["roles"]]}
                for key, row in ROLE_TEMPLATES.items()
            ],
            "has": {name: self.bot.module(name) is not None for name in ("greetings", "moderation", "automod", "reactionroles")},
        }

    async def api_automod(self, req: Any) -> dict:
        form = await req.json()
        preset = form.get("preset") if isinstance(form, dict) else None
        if preset == "none":
            return {"ok": True}
        if not isinstance(preset, str) or preset not in AUTOMOD_PRESETS:
            raise ValidationError({"preset": "Pick one of the listed presets."})
        if not self._has("automod"):
            raise ApiError(404, "Automod isn't available.")
        await self.bot.settings.save(req.guild_id, "automod", AUTOMOD_PRESETS[preset], actor=req.actor, action=f"setup.automod.{preset}")
        return {"ok": True}

    async def api_role_template(self, req: Any) -> dict:
        """Roles for a template, reusing any with the same name that are safe to hand out. Running this twice makes no duplicates."""
        req.require_permission("manage_roles")
        form = await req.json()
        template = ROLE_TEMPLATES.get(form.get("template")) if isinstance(form, dict) and isinstance(form.get("template"), str) else None
        if template is None:
            raise ValidationError({"template": "Pick one of the listed templates."})
        info = req.ops.describe_guild(req.guild_id)
        if info is None:
            raise ApiError(404, "The bot isn't in that server.")
        commands_settings = self.bot.settings.get(req.guild_id, COMMANDS_MODULE)
        top = min(info["me"]["top_role_position"], req.top_role_position())
        usable = {
            role["name"].casefold(): role for role in info["roles"]
            if not role["managed"] and not role["default"] and role["position"] < top and role_is_safe(role["permissions"], commands_settings)
        }
        rows = []
        made = 0
        for name, color, emoji in template["roles"]:
            role = usable.get(name.casefold())
            if role is None:
                role = await req.ops.create_role(req.guild_id, name, color=color)
                made += 1
            rows.append({"id": role["id"], "name": role["name"], "emoji": emoji})
        await req.record("setup.roles", detail=f"{form['template']}, {made} new")
        return {"roles": rows, "made": made}

    async def api_mute_role(self, req: Any) -> dict:
        req.require_permission("manage_roles")
        if not self._has("moderation"):
            raise ApiError(404, "Moderation isn't available.")
        # A second click keeps the role in use. Members muted under it would keep it after their time ended, since the timer removes the role the setting names.
        current = self.bot.settings.get(req.guild_id, "moderation")["mute_role"]
        info = req.ops.describe_guild(req.guild_id) or {}
        existing = next((row for row in info.get("roles", []) if row["id"] == current), None)
        if existing is not None:
            return {"id": existing["id"], "name": existing["name"]}
        role = await req.ops.create_mute_role(req.guild_id, "Muted")
        await self.bot.settings.save(req.guild_id, "moderation", {"mute_role": role["id"]}, actor=req.actor, action="setup.muterole")
        return {"id": role["id"], "name": role["name"]}

    def _moderator_roles(self, guild_id: int) -> set[int]:
        """The bot manager role and the roles Moderation counts as staff."""
        roles: set[int] = set()
        mod_role = self.bot.settings.get(guild_id, COMMANDS_MODULE).get("mod_role")
        if mod_role:
            roles.add(mod_role)
        moderation = self.bot.module("moderation")
        if moderation is not None:
            for found in moderation.staff_roles(self.bot.settings.get(guild_id, "moderation")).values():
                roles |= set(found)
        return roles

    async def api_modlog_channel(self, req: Any) -> dict:
        req.require_permission("manage_channels")
        if not self._has("moderation"):
            raise ApiError(404, "Moderation isn't available.")
        current = self.bot.settings.get(req.guild_id, "moderation")["modlog_channel"]
        existing = req.ops.channel(req.guild_id, current) if current else None
        if existing is not None:
            return {"id": existing["id"], "name": existing["name"]}
        # Members can't see it, so the people who run moderation need to. Administrators already can.
        channel = await req.ops.create_channel(req.guild_id, "mod-log", private=True, viewer_roles=self._moderator_roles(req.guild_id))
        await self.bot.settings.save(req.guild_id, "moderation", {"modlog_channel": channel["id"]}, actor=req.actor, action="setup.modlog")
        return {"id": channel["id"], "name": channel["name"]}

    async def api_channel(self, req: Any) -> dict:
        req.require_permission("manage_channels")
        form = await req.json()
        name = clean_channel_name(form.get("name") if isinstance(form, dict) else None)
        channel = await req.ops.create_channel(req.guild_id, name, read_only=True)
        await req.record("setup.channel", detail=name)
        return {"id": channel["id"], "name": channel["name"]}
