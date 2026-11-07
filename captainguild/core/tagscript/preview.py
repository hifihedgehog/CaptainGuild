"""Trying a template on the dashboard: stand-ins for Discord objects, built from what the dashboard already knows, and a plain list of what the blocks asked for.

Endpoints reach Discord only through req.ops, so a preview runs the same in the demo, the tests and production. Nothing here acts: it only reads.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any

import discord

from .engine import Actions, Scope

_SNOWFLAKE_EPOCH_MS = 1420070400000


def _moment(seconds: float | None) -> datetime.datetime | None:
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc) if seconds else None


def _created_from_id(snowflake: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(((snowflake >> 22) + _SNOWFLAKE_EPOCH_MS) / 1000, datetime.timezone.utc)


def _role_view(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["id"], name=row["name"], position=row["position"], mention=f"<@&{row['id']}>", color=discord.Color(row.get("color") or 0),
        permissions=[(name, True) for name in row.get("permissions") or []],
    )


def _person_view(row: dict, roles: list[SimpleNamespace]) -> SimpleNamespace:
    wanted = {str(role_id) for role_id in row.get("role_ids") or []}
    held = sorted((r for r in roles if str(r.id) in wanted or r.position == 0), key=lambda r: r.position)
    color = next((r.color for r in reversed(held) if r.color.value), discord.Color(0))

    class Person(SimpleNamespace):
        def __str__(self) -> str:
            return self.name

    return Person(
        id=row["id"], name=row["name"], display_name=row.get("display_name") or row["name"], mention=f"<@{row['id']}>", bot=bool(row.get("bot")),
        display_avatar=SimpleNamespace(url=row.get("avatar") or ""), created_at=_moment(row.get("created_at")), joined_at=_moment(row.get("joined_at")),
        roles=held, top_role=held[-1] if held else None, color=color,
    )


def preview_scope(ops: Any, guild_id: int, user_id: int, *, args: str = "", channel_id: int | None = None, role_id: int | None = None) -> Scope:
    """The signed-in person, in this server, in the chosen channel or the first text channel it has."""
    info = ops.describe_guild(guild_id) or {}
    roles = [_role_view(row) for row in info.get("roles") or []]
    row = ops.member(guild_id, user_id)
    member = _person_view(row, roles) if row else None
    channels = info.get("channels") or []
    place = next((c for c in channels if c["id"] == channel_id), None) or next((c for c in channels if c.get("type") == 0), None)
    channel = None
    if place is not None:
        channel = SimpleNamespace(
            id=place["id"], name=place["name"], position=place.get("position", 0), mention=f"<#{place['id']}>", topic=place.get("topic"),
            slowmode_delay=place.get("slowmode", 0), category_id=place.get("parent_id"),
        )
    icon = info.get("icon")
    owner_row = ops.member(guild_id, info["owner_id"]) if info.get("owner_id") else None
    guild = SimpleNamespace(
        id=guild_id, name=info.get("name", ""), icon=SimpleNamespace(url=icon) if icon else None, member_count=info.get("member_count"),
        owner=_person_view(owner_row, roles) if owner_row else None, owner_id=info.get("owner_id"), members=[member] if member else [], roles=roles, channels=channels, emojis=[], created_at=_created_from_id(guild_id),
    )
    role = next((r for r in roles if r.id == role_id), None) if role_id else None
    return Scope(guild=guild, member=member, channel=channel, target=member, args=args, role=role, role_added=True if role else None)


def describe_actions(actions: Actions, *, trusted: bool = True, kind: str = "tag") -> list[str]:
    """What the blocks would do, for the person trying the template. `trusted` and `kind` say whether this one would really have the powers its blocks ask for."""
    out = []
    if actions.requires:
        out.append("Only runs for the roles or channels its require blocks name: " + ", ".join(entry for _, entries in actions.requires for entry in entries))
    if actions.blacklists:
        out.append("Refuses the roles or channels its blacklist blocks name: " + ", ".join(entry for _, entries in actions.blacklists for entry in entries))
    if actions.destination == "dm":
        out.append("Sends its output as a DM to the person who ran it.")
    elif actions.destination == "channel":
        out.append(f"Sends its output to {actions.channel_text}." if trusted else f"Sends its output to {actions.channel_text}, if whoever wrote it can post there.")
    if actions.react:
        out.append("Reacts to its output with " + " ".join(actions.react))
    if actions.react_up:
        out.append("Reacts to the message that set it off with " + " ".join(actions.react_up))
    for text, admin in actions.commands:
        if trusted:
            out.append(f"Runs the command `{text}` " + ("with the bot's permissions." if admin else "with the permissions of the person who ran it."))
        elif kind == "trigger":
            out.append(f"The command `{text}` doesn't run until this trigger is trusted.")
        else:
            out.append(f"Runs the command `{text}` only for its owner, at the owner's permissions, until this tag is trusted.")
    if actions.override:
        out.append("Its commands run as if the person held the bot manager role." if trusted else f"Its override block does nothing until this {kind} is trusted.")
    if actions.silent:
        out.append("Its commands post nothing unless they fail.")
    if actions.delete:
        out.append("Deletes the message that set it off.")
    return out
