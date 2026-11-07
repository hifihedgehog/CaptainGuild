"""Who can run which command, and where.

One check runs for every command, whether it came as a slash command, a prefix or a mention. The rule:

0. /dashboard and /my_data run for everyone, and /config for the owner and Administrators, whatever the rules below say. Each is a way back in.
1. A disabled command or subcommand is off for everyone, even administrators.
2. Manage Server and Administrator skip every channel and role rule below.
3. Otherwise the server-wide ignore lists and "entire bot mod only" apply.
4. A channel in "ignore overwrites in" or a member with a role in "roles that ignore overwrites" skips the
   per-command allow and ignore lists. Everyone else must pass them.
5. The command's Discord permission is required unless the member holds a bypass role for that command or the bot
   manager role. A tag's {override} counts as the bot manager role, except for Ban Members.
6. A command marked mod only needs Manage Server, the bot manager role or a bypass role.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .validate import Field, Schema, boolean, list_of, mapping_of, optional, snowflake, snowflakes, text


def _redirect(value):
    """None, the word "dm", or a channel."""
    if value in (None, ""):
        return None
    if value == "dm":
        return "dm"
    return snowflake(value)


def _overwrite(value) -> dict:
    return COMMAND_SCHEMA.clean(value)


COMMAND_SCHEMA = Schema({
    "enabled": Field(True, boolean),
    "channel_wl": Field([], snowflakes()),
    "channel_bl": Field([], snowflakes()),
    "role_wl": Field([], snowflakes()),
    "role_bl": Field([], snowflakes()),
    "bypass_roles": Field([], snowflakes()),
    "redirect": Field(None, _redirect),
    "mod_only": Field(False, boolean),
    "restricted": Field(False, boolean),
    "muted": Field(False, boolean),
    "delete": Field(False, boolean),
    "disabled_subcommands": Field([], list_of(text(100))),
})

SETTINGS_SCHEMA = Schema({
    "channel_wl": Field([], snowflakes()),
    "channel_bl": Field([], snowflakes()),
    "role_wl": Field([], snowflakes()),
    "role_bl": Field([], snowflakes()),
    "bot_channels": Field([], snowflakes()),
    "mod_role": Field(None, optional(snowflake)),
    "mod_only": Field(False, boolean),
    "elevated_roles": Field(False, boolean),
    "overwrites": Field({}, mapping_of(text(100, minimum=1), _overwrite, maximum=400)),
})

# Commands no rule can take away, because they're the way back in. Everyone keeps the first two, and the owner and Administrators keep /config.
ALWAYS_OPEN = frozenset({"dashboard", "my_data"})
ADMINS_KEEP = frozenset({"config"})
NEVER_OFF = ALWAYS_OPEN | ADMINS_KEEP

# What Discord's own menus call these, where the library's name differs.
PERMISSION_LABELS = {"manage_guild": "Manage Server", "moderate_members": "Timeout Members", "manage_emojis": "Manage Expressions", "external_emojis": "Use External Emoji"}
MODULE = "commands"

# Permissions a role can hold that make handing it out a risk. Self-assign features refuse these roles until a server turns on elevated_roles.
DANGEROUS_PERMISSIONS = (
    "administrator", "manage_guild", "manage_channels", "manage_roles", "manage_messages", "manage_expressions",
    "manage_nicknames", "manage_webhooks", "ban_members", "kick_members", "moderate_members",
)


def permission_names(permissions: Iterable[str]) -> str:
    """Permissions the way Discord's own menus name them."""
    return ", ".join(PERMISSION_LABELS.get(p) or p.replace("_", " ").title() for p in permissions)


@dataclass
class Decision:
    allowed: bool = True
    reason: str | None = None
    muted: bool = False
    delete: bool = False
    redirect: int | str | None = None
    restricted_channel: int | None = None
    waived: bool = False  # a bypass role or the bot manager role stood in for a permission the person lacks


@dataclass
class Who:
    """The person asking, in plain values so the rule can be tested without Discord."""
    role_ids: set[int] = field(default_factory=set)
    permissions: set[str] = field(default_factory=set)
    is_owner: bool = False
    moderator: bool = False  # counts as holding the bot manager role, for a tag's {override} block

    @property
    def is_admin(self) -> bool:
        return self.is_owner or "administrator" in self.permissions or "manage_guild" in self.permissions


def evaluate(settings: dict, *, root: str, leaf: str | None = None, required: Iterable[str] = (), who: Who, channel_id: int,
             category_id: int | None = None) -> Decision:
    """Decide one command run. `root` is the top-level command, and `leaf` is the full name when it's a subcommand."""
    overwrite = settings.get("overwrites", {}).get(root) or {}
    decision = Decision(muted=bool(overwrite.get("muted")), delete=bool(overwrite.get("delete")), redirect=overwrite.get("redirect"))

    if overwrite.get("enabled") is False:
        return Decision(False, "This command is turned off in this server.")
    if leaf and leaf in (overwrite.get("disabled_subcommands") or []):
        return Decision(False, "This command is turned off in this server.")

    places = {channel_id} | ({category_id} if category_id else set())
    mod_role = settings.get("mod_role")
    bypass = set(overwrite.get("bypass_roles") or [])
    holds_manager_role = bool(mod_role and mod_role in who.role_ids)
    is_manager = who.moderator or holds_manager_role
    has_bypass = bool(bypass & who.role_ids)

    if not who.is_admin:
        if places & set(settings.get("channel_bl") or []):
            return Decision(False, "Commands are turned off in this channel.")
        if who.role_ids & set(settings.get("role_bl") or []):
            return Decision(False, "You can't use commands in this server.")
        if settings.get("mod_only") and not (is_manager or has_bypass):
            return Decision(False, "Commands are limited to moderators in this server.")
        skips_overwrites = bool(places & set(settings.get("channel_wl") or [])) or bool(who.role_ids & set(settings.get("role_wl") or []))
        if not skips_overwrites:
            channel_wl = set(overwrite.get("channel_wl") or [])
            if channel_wl and not places & channel_wl:
                return Decision(False, "This command can't be used in this channel.")
            if places & set(overwrite.get("channel_bl") or []):
                return Decision(False, "This command can't be used in this channel.")
            role_wl = set(overwrite.get("role_wl") or [])
            if role_wl and not who.role_ids & role_wl:
                return Decision(False, "You don't have a role that can use this command.")
            if who.role_ids & set(overwrite.get("role_bl") or []):
                return Decision(False, "You have a role that can't use this command.")

    missing = [p for p in required if p not in who.permissions]
    if missing and not who.is_owner and "administrator" not in who.permissions and not has_bypass:
        if not is_manager or ("ban_members" in missing and not holds_manager_role):
            return Decision(False, f"You need {permission_names(missing)} to use this command.")
    decision.waived = bool(missing) and not who.is_owner and "administrator" not in who.permissions

    if overwrite.get("mod_only") and not (who.is_admin or is_manager or has_bypass):
        return Decision(False, "This command is limited to moderators.")

    if overwrite.get("restricted") and not (who.is_admin or is_manager or has_bypass):
        bot_channels = settings.get("bot_channels") or []
        # A bot channel can be a category, which then covers every channel in it.
        if bot_channels and not places & set(bot_channels):
            decision.restricted_channel = bot_channels[0]
    return decision


def answer_place(guild: Any, target: int | None) -> Any:
    """The channel an answer is sent to when a rule names `target`. A category stands for the first text channel in it that the bot can post in."""
    place = guild.get_channel(target) if guild is not None and target else None
    if place is None or getattr(getattr(place, "type", None), "name", "") != "category":
        return place
    me = getattr(guild, "me", None)
    for channel in guild.text_channels:
        if channel.category_id == place.id and (me is None or _may_post(channel, me)):
            return channel
    return None


def _may_post(channel: Any, me: Any) -> bool:
    try:
        return bool(channel.permissions_for(me).send_messages)
    except AttributeError:
        return True


def role_is_safe(permissions: Iterable[str], settings: dict) -> bool:
    """Whether a feature may hand out a role holding these permissions."""
    return bool(settings.get("elevated_roles")) or not (set(permissions) & set(DANGEROUS_PERMISSIONS))
