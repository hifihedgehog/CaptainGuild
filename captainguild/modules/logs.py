"""Server event logging and the message store.

Every event a server logs becomes one embed. It goes to the event's log channel through the batching log sink and into the
log_entries table, which the Log search page reads. Messages members post go into the messages table, which gives an edit its before
text and a delete its text. Other features read that store through stored_message, user_messages and user_log_entries.

Both tables live in the message database and are written in batches, because a busy server produces far more rows than are worth
committing one at a time. A read writes out whatever is still waiting first, so it never misses a row.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import collections
import contextlib
import csv
import datetime
import io
import json
import logging
import math
import re
import time
import zlib
from typing import Any, AsyncIterator, Callable, Iterable
from urllib.parse import urlsplit

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.durations import humanize, ordinal
from ..core.messages import DESCRIPTION_MAX, FIELD_NAME_MAX, FIELD_VALUE_MAX, FIELDS_MAX, FOOTER_MAX, TITLE_MAX, TOTAL_MAX, embed_size
from ..core.module import Module, requires
from ..core.ops import OpsError
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, choice, integer, list_of, optional, snowflake, snowflakes, text
from ..web.api import ApiError, safe_json

log = logging.getLogger(__name__)

BLUE, GREEN, RED, YELLOW, ORANGE = 0x4286F4, 0x53DDAC, 0xDD5F53, 0xFFF5AF, 0xF48942

# ---- how long things wait. Tests shorten these. ----
FLUSH_SECONDS = 2.0
AUDIT_WAIT = 2.0  # an audit log entry can arrive just after the event it explains
AUDIT_POLL = 0.2
RENAME_AUDIT_WAIT = 4.0  # a rename that arrives before its entry would bury the channel if the ignore list couldn't catch it
DELETE_SETTLE = 1.0
DELETE_LATE_WAIT = 1.5
AUDIT_READ_GAP = 0.5
ROLE_DELETE_GRACE = 0.3  # Discord can send a member's update just before it says the role was deleted
ROLE_DELETE_HOLD = 2.0
AUTOMOD_GATHER = 1.5  # one rule firing sends one event per action, and staff want one entry
DOWNLOAD_SECONDS = 8.0

MAX_PENDING = 50_000
BOT_MESSAGES_KEPT = 5_000
BOT_TEXT_KEPT = 1000
REFUSED_SECONDS = 3600.0  # after Discord refuses a log channel, how long entries go elsewhere and staff stay told
DELETE_CREDITS_KEPT = 500
AUDIT_READ_SIZE = 25
FRESH_ENTRY_SECONDS = 10.0
MAX_COPIED_IMAGES = 4
MAX_QUEUED_COPIES = 20
MAX_COPY_BYTES = 256 * 1024 * 1024
EXPLAINED_KEPT = 2000
MAX_READ_LIMIT = 5000
MAX_DETAIL_BYTES = 8 * 1024 * 1024
PRUNE_SECONDS = 6 * 3600
PRUNE_CHUNK = 2000
PRUNE_TIMER = "logging.prune"
COUNT_CAP = 100_000
EXPORT_MAX = 100_000
EXPORT_CHUNK = 1000
PAGE_SIZES = (10, 25, 50, 100)
CODE_BYTES = 64 * 1024

CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
IMAGE_SUFFIX = re.compile(r"\.(?:png|jpe?g|gif|webp)$", re.IGNORECASE)
INVITE_LINK = re.compile(r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg)/([A-Za-z0-9-]+)", re.IGNORECASE)
MENTION = re.compile(r"<(#|@!?|@&)(\d{15,21})>")

Actions = discord.AuditLogAction
SOUND_ACTIONS = (Actions.soundboard_sound_create, Actions.soundboard_sound_update, Actions.soundboard_sound_delete)

# Each group is one card on the Logging page and shares one log channel. The number is the event's place in the old setup's bitmask,
# which is how its settings import. An event with none is new here.
EVENT_GROUPS: tuple[tuple[str, str, str, tuple[tuple[str, str, int | None], ...]], ...] = (
    ("message", "Message events", "What happens to messages members post.", (
        ("delete", "Deleted messages", 1),
        ("moddelete", "Deleted by a moderator or bot", 1),
        ("edit", "Edited messages", 2),
        ("purge", "Purged messages", 4),
        ("discord", "Discord invites posted", 2097152),
        ("pins", "Pinned and unpinned messages", None),
    )),
    ("join", "Members joining and leaving", "People arriving and leaving.", (
        ("join", "Members joining", 256),
        ("leave", "Members leaving", 512),
    )),
    ("server", "Server events", "Changes made to the server itself.", (
        ("channelcreate", "Channel creation", 1024),
        ("channelupdate", "Updated channel", 2048),
        ("channeldelete", "Channel deletion", 4096),
        ("threadcreate", "Thread creation", 16777216),
        ("threadupdate", "Updated thread", 33554432),
        ("threaddelete", "Thread deletion", 67108864),
        ("rolecreate", "Role creation", 65536),
        ("roleupdate", "Role updates", 131072),
        ("roledelete", "Role deletion", 262144),
        ("server", "Server updates", 524288),
        ("emoji", "Emoji changes", 1048576),
        ("stickers", "Sticker changes", None),
        ("soundboard", "Soundboard changes", None),
        ("invitecreate", "Invites created", None),
        ("invitedelete", "Invites deleted", None),
        ("webhooks", "Webhook changes", None),
        ("integrations", "Bots and integrations", None),
        ("scheduledevents", "Scheduled events", None),
        ("stage", "Stage channels", None),
        ("automodrule", "Discord automod rule changes", None),
    )),
    ("member", "Member events", "Changes to the people in the server.", (
        ("role", "Role updates", 8),
        ("name", "Name changes", 16),
        ("avatar", "Avatar changes", 32),
        ("ban", "Member bans", 64),
        ("unban", "Member unbans", 128),
        ("timeout", "Member timeout", 4194304),
        ("removetimeout", "Member remove timeout", 8388608),
        ("kick", "Member kicks and prunes", None),
        ("automodaction", "Discord automod actions", None),
    )),
    ("voice", "Voice events", "Who is moving through voice channels.", (
        ("voicejoin", "Join voice channel", 8192),
        ("voicemove", "Move between voice channels", 16384),
        ("voiceleave", "Leave voice channel", 32768),
        ("voicemod", "Moderator voice actions", None),
    )),
)
EVENT_KINDS = {key: group[0] for group in EVENT_GROUPS for key, _, _ in group[3]}
EVENT_LABELS = {key: label for group in EVENT_GROUPS for key, label, _ in group[3]}
EVENT_BITS = {key: bit for group in EVENT_GROUPS for key, _, bit in group[3] if bit is not None}
EVENT_KEYS = tuple(EVENT_KINDS)
CHANNEL_FIELDS = {"message": "message_channel", "member": "member_channel", "join": "join_channel", "server": "server_channel", "voice": "voice_channel"}
# Events that can have a channel of their own. With it unset they share their card's channel.
EVENT_CHANNEL_FIELDS = {
    "moddelete": "moderator_delete_channel", "purge": "moderator_delete_channel", "role": "role_channel", "name": "name_channel", "timeout": "timeout_channel", "removetimeout": "timeout_channel",
}
# What copies someone's words out of a channel, and the channels it can land in.
MESSAGE_EVENTS = {"delete", "moddelete", "edit", "purge"}
MESSAGE_LOGS = ("message_channel", "moderator_delete_channel", "default_channel")
FIELD_LABELS = {
    "default_channel": "default log channel", "message_channel": "message log channel", "member_channel": "member log channel", "join_channel": "join and leave log channel",
    "server_channel": "server log channel", "voice_channel": "voice log channel", "moderator_delete_channel": "moderator delete log channel", "role_channel": "role log channel",
    "name_channel": "name log channel", "timeout_channel": "timeout log channel",
}
DEFAULT_EVENTS = ("delete", "moddelete", "edit", "purge", "role", "name", "avatar", "ban", "unban", "timeout", "removetimeout", "join", "leave", "roledelete")
EVENT_SETS = {
    "bans": ("ban", "unban"),
    "channels": ("channelcreate", "channelupdate", "channeldelete"),
    "threads": ("threadcreate", "threadupdate", "threaddelete"),
    "allroles": ("rolecreate", "roleupdate", "roledelete"),
    "invites": ("invitecreate", "invitedelete"),
    "voice": ("voicejoin", "voicemove", "voiceleave", "voicemod"),
}
AIO_NAMES = {"message_channel": "message-log", "member_channel": "member-log", "join_channel": "join-log", "server_channel": "server-log", "voice_channel": "voice-log"}
OLD_CHANNEL_FIELDS = {"log_channel": "default_channel", "message_channel": "message_channel", "moderation_channel": "member_channel", "movement_channel": "join_channel", "server_channel": "server_channel", "voice_channel": "voice_channel"}

CHANNEL_KINDS = {
    discord.ChannelType.text: "Text channel",
    discord.ChannelType.voice: "Voice channel",
    discord.ChannelType.category: "Category",
    discord.ChannelType.news: "Announcement channel",
    discord.ChannelType.stage_voice: "Stage channel",
    discord.ChannelType.forum: "Forum channel",
    discord.ChannelType.media: "Media channel",
}

# Every read of a message row. `search_text` stays out of it because nothing shows it.
MESSAGE_COLUMNS = "id, guild_id, channel_id, author_id, author_name, content, attachments, stickers, reply_to, created_at, edited_at, deleted_at, deleted_by"
ENTRY_COLUMNS = "e.id, e.at, e.event, e.channel_id, e.parent_id, e.user_id, e.actor_id, e.message_id, e.embed, e.detail IS NOT NULL AS has_detail"
UNCERTAIN = object()  # what `_who_deleted` answers when an audit entry fits this delete and another one, and Discord doesn't say which


# ---- embeds ----

def clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: max(limit - 3, 0)] + "..."


def join_lines(*parts: str | None) -> str:
    return "\n".join(part for part in parts if part)


def iso_time(at: float | None = None) -> str:
    moment = datetime.datetime.fromtimestamp(at, datetime.timezone.utc) if at else discord.utils.utcnow()
    return moment.isoformat()


def make_field(name: str, value: str, inline: bool = False) -> dict:
    # Discord refuses a field with an empty name or value.
    return {"name": clip(name or "-", FIELD_NAME_MAX), "value": clip(value or "-", FIELD_VALUE_MAX), "inline": inline}


def make_embed(title: str, *, description: str | None = None, color: int = BLUE, author: dict | None = None, footer: str | None = None, url: str | None = None,
               fields: Iterable[dict] = (), thumbnail: str | None = None, at: float | None = None) -> dict:
    embed: dict[str, Any] = {"title": clip(title, TITLE_MAX), "color": color, "timestamp": iso_time(at)}
    if description:
        embed["description"] = clip(description, DESCRIPTION_MAX)
    if url:
        embed["url"] = url
    if author:
        embed["author"] = author
    if footer:
        embed["footer"] = {"text": clip(footer, FOOTER_MAX)}
    if thumbnail:
        embed["thumbnail"] = {"url": thumbnail}
    rows = list(fields)
    if rows:
        embed["fields"] = rows
        left_out = 0
        room = TOTAL_MAX - 80
        while rows and (len(rows) > FIELDS_MAX - (1 if left_out else 0) or embed_size(embed) > room):
            rows.pop()
            left_out += 1
        if left_out:
            rows.append(make_field("More", f"{left_out} more didn't fit in one entry."))
    over = embed_size(embed) - TOTAL_MAX
    if over > 0 and "description" in embed:
        embed["description"] = clip(embed["description"], max(len(embed["description"]) - over, 1))
    return embed


def author_block(user: Any) -> dict | None:
    name = getattr(user, "name", None)
    if not name:
        return None
    block = {"name": clip(name, 256)}
    avatar = getattr(user, "display_avatar", None)
    if avatar is not None:
        block["icon_url"] = avatar.url
    return block


def who_text(user: Any, user_id: int | None = None) -> str:
    """A mention plus the plain name, because a mention shows as raw digits once Discord forgets the person."""
    user_id = getattr(user, "id", None) or user_id
    name = getattr(user, "name", None)
    return f"<@{user_id}> ({discord.utils.escape_markdown(name)})" if name else f"<@{user_id}>"


def by_lines(entry: Any, label: str = "By") -> str:
    """What the audit log says about who did it and why, as lines for a description."""
    if entry is None:
        return ""
    return join_lines(
        f"**{label}:** {who_text(entry.user, entry.user_id)}" if entry.user_id else None,
        f"**Reason:** {clip(entry.reason, 512)}" if entry.reason else None,
    )


def by_fields(entry: Any, label: str = "By") -> list[dict]:
    """The same facts as `by_lines`, for an entry whose description holds a member's text."""
    if entry is None:
        return []
    fields = [make_field(label, who_text(entry.user, entry.user_id))] if entry.user_id else []
    if entry.reason:
        fields.append(make_field("Reason", entry.reason))
    return fields


def quoted(text: str) -> str:
    """A member's text as it goes in an entry. A masked link could dress up as a Discord link in front of staff, so its link part reads as plain text. The store and the detail file keep the original."""
    return text.replace("](", "]\\(")


def sent_field(at: float) -> dict:
    return make_field("Sent", f"<t:{int(at)}:f>", inline=True)


def before_after(before: str, after: str) -> tuple[list[dict], bool]:
    """Before and After, each in its own field so one's text can't pass for the other, and whether either was too long for one."""
    before, after = quoted(before or "(no text)"), quoted(after or "(no text)")
    return [make_field("Before", before), make_field("After", after)], len(before) > FIELD_VALUE_MAX or len(after) > FIELD_VALUE_MAX


def channel_kind(channel: Any) -> str:
    return CHANNEL_KINDS.get(getattr(channel, "type", None), "Channel")


def overwrite_label(target: Any) -> str:
    name = getattr(target, "name", None)
    if isinstance(target, discord.Member):
        return f"Member override for {name}"
    if name:
        return f"Role override for {name}"
    return f"Override for {getattr(target, 'id', target)}"


# The library's names for these differ from what Discord's settings show.
PERMISSION_NAMES = {"read_messages": "View channel", "external_emojis": "Use external emojis", "external_stickers": "Use external stickers", "manage_emojis": "Manage expressions"}


def permission_name(name: str) -> str:
    return PERMISSION_NAMES.get(name) or name.replace("_", " ").capitalize()


OVERWRITE_MARKS = {True: "\N{WHITE HEAVY CHECK MARK}", False: "\N{CROSS MARK}", None: "\N{HEAVY MINUS SIGN}"}


def overwrite_fields(channel: Any) -> list[dict]:
    fields = []
    for target, overwrite in channel.overwrites.items():
        lines = [f"**{permission_name(name)}:** {OVERWRITE_MARKS[value]}" for name, value in sorted(overwrite) if value is not None]
        if lines:
            fields.append(make_field(overwrite_label(target), "\n".join(lines)))
    return fields


def overwrite_changes(before: Any, after: Any) -> list[dict]:
    old = {getattr(t, "id", t): (t, o) for t, o in before.overwrites.items()}
    new = {getattr(t, "id", t): (t, o) for t, o in after.overwrites.items()}
    fields = []
    for target_id in sorted(set(old) | set(new), key=str):
        target = (new.get(target_id) or old[target_id])[0]
        was = dict(old[target_id][1]) if target_id in old else {}
        now = dict(new[target_id][1]) if target_id in new else {}
        lines = [
            f"**{permission_name(name)}:** {OVERWRITE_MARKS[was.get(name)]} \N{RIGHTWARDS ARROW} {OVERWRITE_MARKS[now.get(name)]}"
            for name in sorted(set(was) | set(now)) if was.get(name) != now.get(name)
        ]
        if lines:
            fields.append(make_field(overwrite_label(target), "\n".join(lines)))
    return fields


def channel_facts(channel: Any) -> dict[str, str]:
    """What a person changes about a channel. Position stays out: moving one channel renumbers its neighbors."""
    facts = {"Name": channel.name}
    if hasattr(channel, "topic"):
        facts["Topic"] = channel.topic or "None"
    category = getattr(channel, "category", None)
    facts["Category"] = category.name if category else "None"
    facts["Age restricted"] = "Yes" if getattr(channel, "nsfw", False) else "No"
    slowmode = getattr(channel, "slowmode_delay", None)
    if slowmode is not None:
        facts["Slowmode"] = humanize(slowmode) if slowmode else "Off"
    bitrate = getattr(channel, "bitrate", None)
    if bitrate is not None:
        facts["Bitrate"] = f"{bitrate // 1000} kbps"
    limit = getattr(channel, "user_limit", None)
    if limit is not None:
        facts["User limit"] = str(limit) if limit else "None"
    if hasattr(channel, "rtc_region"):
        facts["Voice region"] = str(channel.rtc_region) if channel.rtc_region else "Automatic"
    auto_archive = getattr(channel, "default_auto_archive_duration", None)
    if auto_archive is not None:
        facts["Thread auto archive"] = humanize(auto_archive * 60)
    tags = getattr(channel, "available_tags", None)
    if tags is not None:
        facts["Tags"] = ", ".join(tag.name for tag in tags) or "None"
    return facts


def role_facts(role: Any) -> dict[str, str]:
    return {
        "Name": role.name,
        "Color": f"#{role.color.value:06x}",
        "Mentionable": str(role.mentionable),
        "Displayed separately": str(role.hoist),
    }


def guild_facts(guild: Any) -> dict[str, str]:
    def channel_name(channel: Any) -> str:
        return channel.name if channel else "None"

    def asset(value: Any) -> str:
        return value.url if value else "None"

    return {
        "Name": guild.name,
        "Description": guild.description or "None",
        "Icon": asset(guild.icon),
        "Banner": asset(guild.banner),
        "Invite background": asset(guild.splash),
        "Owner": f"<@{guild.owner_id}>",
        "Verification level": str(guild.verification_level),
        "Explicit media filter": str(guild.explicit_content_filter),
        "Default notifications": str(guild.default_notifications).rsplit(".", 1)[-1],
        "Moderator 2FA": str(guild.mfa_level).rsplit(".", 1)[-1],
        "AFK channel": channel_name(guild.afk_channel),
        "AFK timeout": humanize(guild.afk_timeout),
        "System channel": channel_name(guild.system_channel),
        "Rules channel": channel_name(guild.rules_channel),
        "Updates channel": channel_name(guild.public_updates_channel),
        "Vanity link": guild.vanity_url_code or "None",
        "Language": str(guild.preferred_locale),
        "Boost progress bar": str(guild.premium_progress_bar_enabled),
    }


def changed_fields(old: dict[str, str], new: dict[str, str]) -> list[dict]:
    """The Before and After columns the live logs show, holding only what changed."""
    changed = [key for key in new if old.get(key) != new[key]]
    if not changed:
        return []
    return [
        make_field("Before", "\n".join(f"**{key}:** {old.get(key, 'None')}" for key in changed), inline=True),
        make_field("After", "\n".join(f"**{key}:** {new[key]}" for key in changed), inline=True),
    ]


def embed_text(embed: dict, *, with_title: bool = True) -> str:
    """Everything a reader sees in an entry, as plain text for search and for exports."""
    parts: list[str] = [embed.get("title", "")] if with_title else []
    parts.append(embed.get("author", {}).get("name", ""))
    parts.append(embed.get("description", ""))
    for field in embed.get("fields", []):
        parts.extend((field.get("name", ""), field.get("value", "")))
    parts.append(embed.get("footer", {}).get("text", ""))
    return "\n".join(part for part in parts if part)


# ---- the store ----

def attachment_rows(attachments: Iterable[Any]) -> list[dict]:
    return [{"filename": a.filename, "url": a.url, "proxy_url": a.proxy_url, "size": a.size, "content_type": a.content_type} for a in attachments]


def message_text(message: Any) -> str:
    """A forward has no text of its own. What it carries is in its snapshot, and that's what staff need if it gets deleted."""
    if message.content:
        return message.content
    for snapshot in getattr(message, "message_snapshots", None) or []:
        if snapshot.content:
            return f"[Forwarded] {snapshot.content}"
    return ""


def message_row(message: Any) -> tuple:
    attachments = attachment_rows(message.attachments)
    stickers = [sticker.name for sticker in message.stickers]
    reference = message.reference if message.type == discord.MessageType.reply else None
    return (
        message.id, message.guild.id, message.channel.id, message.author.id, message.author.name, message_text(message),
        json.dumps(attachments, ensure_ascii=False) if attachments else None,
        json.dumps(stickers, ensure_ascii=False) if stickers else None,
        reference.message_id if reference is not None else None,
        message.created_at.timestamp(), message.edited_at.timestamp() if message.edited_at else None,
    )


def bot_message_text(message: Any) -> str:
    """What someone reading the channel saw in a bot's message, its embeds included, cut to what's worth holding in memory."""
    parts = [message.content]
    for embed in message.embeds:
        parts += [embed.author.name, embed.title, embed.description]
        parts += [f"{field.name}: {field.value}" for field in embed.fields]
        parts.append(embed.footer.text)
    return clip("\n".join(part for part in parts if part), BOT_TEXT_KEPT)


def message_dict(row: dict) -> dict:
    row["attachments"] = json.loads(row["attachments"]) if row["attachments"] else []
    row["stickers"] = json.loads(row["stickers"]) if row["stickers"] else []
    return row


def is_image(attachment: dict) -> bool:
    kind = attachment.get("content_type") or ""
    return kind.startswith("image/") or (not kind and bool(IMAGE_SUFFIX.search(attachment.get("filename") or "")))


def upload_name(filename: str, taken: set[str]) -> str:
    """A name Discord keeps as it is, so attachment:// finds the file. Discord only keeps ASCII letters, digits, dashes, dots and underscores. Names on one message can repeat."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename or "image") or "image"
    base, dot, suffix = name.rpartition(".")
    if not dot:
        base, suffix = name, ""
    number = 1
    while name in taken:
        number += 1
        name = f"{base}_{number}{dot}{suffix}"
    taken.add(name)
    return name


# ---- settings as text ----

def export_code(cfg: dict) -> str:
    raw = json.dumps(cfg, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode()


def events_from_bits(bits: int) -> list[str]:
    return [key for key, bit in EVENT_BITS.items() if bits & bit]


def read_import(code: str) -> dict:
    """Turn what someone pasted into settings. It can be a code from /log export, the same as raw JSON, or the old setup's JSON or event number."""
    code = (code or "").strip().strip("`").strip()
    if not code:
        raise Invalid("Paste a code from the export first.")
    if code.isascii() and code.isdigit():
        if len(code) > 20:
            raise Invalid("That isn't a settings code.")
        return {"logging": int(code)}
    if code.startswith("{"):
        try:
            data = json.loads(code)
        except (ValueError, RecursionError) as error:  # very deep nesting raises RecursionError
            raise Invalid("That isn't valid JSON.") from error
    else:
        try:
            packed = base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))
            # A short code can unpack to something huge, so the size is capped before any of it is kept.
            raw = zlib.decompressobj().decompress(packed, CODE_BYTES)
            data = json.loads(raw)
        except (binascii.Error, ValueError, RecursionError, zlib.error) as error:
            raise Invalid("That isn't a settings code.") from error
    if not isinstance(data, dict):
        raise Invalid("That isn't a settings code.")
    return data


def _id_or_none(value: Any) -> int | None:
    try:
        return snowflake(value)
    except Invalid:
        return None


def settings_from_import(data: dict, schema: Schema, channel_ids: set[int], role_ids: set[int]) -> dict:
    """A settings form for this server. A channel or role that isn't in this server is dropped, so logs can't be pointed at another server."""
    form: dict[str, Any] = {}
    if "logging" in data or "ignored_entities" in data:
        if not isinstance(data.get("logging", 0), int) or isinstance(data.get("logging", 0), bool):
            raise Invalid("The event number in those settings isn't a number.")
        if "logging" in data:
            form["events"] = events_from_bits(data["logging"])
        for old, new in OLD_CHANNEL_FIELDS.items():
            if old in data:
                form[new] = data[old]
        if "ignored_prefixes" in data:
            form["ignored_prefixes"] = data["ignored_prefixes"] or []
        if "ignored_entities" in data:
            if not isinstance(data["ignored_entities"] or [], list):
                raise Invalid("The ignored list in those settings isn't a list.")
            channels, roles, members = [], [], []
            for value in data["ignored_entities"] or []:
                entity = _id_or_none(value)
                if entity is None:
                    continue
                (channels if entity in channel_ids else roles if entity in role_ids else members).append(entity)
            form.update(ignored_channels=channels, ignored_roles=roles, ignored_members=members)
    else:
        form = {key: value for key, value in data.items() if key in schema.fields}
    for field in FIELD_LABELS:
        if field in form and form[field] is not None:
            channel = _id_or_none(form[field])
            form[field] = channel if channel in channel_ids else None
    if isinstance(form.get("ignored_channels"), list):
        form["ignored_channels"] = [c for c in map(_id_or_none, form["ignored_channels"]) if c in channel_ids]
    if isinstance(form.get("ignored_roles"), list):
        form["ignored_roles"] = [r for r in map(_id_or_none, form["ignored_roles"]) if r in role_ids]
    return form


def parse_targets(guild: Any, value: str) -> tuple[list[int], list[int], list[int], list[str]]:
    """Channels, members, roles and anything not understood, from mentions, IDs and single-word names."""
    channels, members, roles, unknown = [], [], [], []
    for token in re.split(r"[\s,]+", value.strip()):
        if not token:
            continue
        match = MENTION.fullmatch(token)
        if match:
            kind, number = match.group(1), int(match.group(2))
            (channels if kind == "#" else roles if kind == "@&" else members).append(number)
        elif token.isdigit() and _id_or_none(token) is not None:
            number = int(token)
            if guild.get_channel_or_thread(number) is not None:
                channels.append(number)
            elif guild.get_role(number) is not None:
                roles.append(number)
            else:
                members.append(number)  # someone who left still counts, so an unknown ID is taken as a person
        else:
            name = token.lstrip("#@")
            channel = discord.utils.find(lambda c: c.name == name, guild.channels)
            role = discord.utils.find(lambda r: r.name == name, guild.roles)
            member = discord.utils.find(lambda m: m.name == name, guild.members)
            if channel is not None:
                channels.append(channel.id)
            elif role is not None:
                roles.append(role.id)
            elif member is not None:
                members.append(member.id)
            else:
                unknown.append(token)
    return channels, members, roles, unknown


# ---- filters for the search page ----

def _time_filter(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except ValueError as error:
        raise Invalid("That isn't a date.") from error
    if not math.isfinite(number) or number < 0:
        raise Invalid("That isn't a date.")
    return number


def search_filters(query: Any) -> dict:
    events = [key for key in (query.get("event") or "").split(",") if key]
    for key in events:
        if key not in EVENT_KINDS:
            raise Invalid("Pick an event type from the list.")
    return {
        "user": optional(snowflake)(query.get("user")),
        "channel": optional(snowflake)(query.get("channel")),
        "events": events,
        "since": _time_filter(query.get("from")),
        "until": _time_filter(query.get("to")),
        "text": re.sub(r"[\x00-\x1f]", " ", text(200)(query.get("q") or "")),
    }


def search_parent(kind: Any, parent_id: int | None) -> int | None:
    """The parent a stored entry names. Log search opens a thread to whoever sees its parent, and a private thread is closer than that, so its entries don't hang off one."""
    return None if kind == discord.ChannelType.private_thread else parent_id


def search_where(guild_id: int, filters: dict, visible: set[int] | None = None) -> tuple[str, list]:
    # A person or channel is a small share of a server's rows, so their own indexes should lead. Written as an OR, SQLite walks the
    # time index instead, reading everything when the person is rare. The plus sign keeps it off the time index.
    narrow = filters["user"] is not None or filters["channel"] is not None
    where = ["+e.guild_id = ?" if narrow else "e.guild_id = ?"]
    args: list[Any] = [guild_id]
    if visible is not None:
        ids = sorted(visible)
        marks = ",".join("?" * len(ids)) or "NULL"
        where.append(f"(e.channel_id IS NULL OR e.channel_id IN ({marks}) OR e.parent_id IN ({marks}))")
        args += [*ids, *ids]
    if filters["user"] is not None:
        where.append("e.id IN (SELECT id FROM log_entries WHERE guild_id = ? AND user_id = ? UNION ALL SELECT id FROM log_entries WHERE guild_id = ? AND actor_id = ?)")
        args += [guild_id, filters["user"], guild_id, filters["user"]]
    if filters["channel"] is not None:
        where.append("e.id IN (SELECT id FROM log_entries WHERE guild_id = ? AND channel_id = ? UNION ALL SELECT id FROM log_entries WHERE guild_id = ? AND parent_id = ?)")
        args += [guild_id, filters["channel"], guild_id, filters["channel"]]
    if filters["events"]:
        where.append(f"e.event IN ({','.join('?' * len(filters['events']))})")
        args += filters["events"]
    if filters["since"] is not None:
        where.append("e.at >= ?")
        args.append(filters["since"])
    if filters["until"] is not None:
        where.append("e.at <= ?")
        args.append(filters["until"])
    words = filters["text"].split()[:12]
    # The index reads three characters at a time, so shorter words have to scan.
    indexed = [word for word in words if len(word) >= 3]
    if indexed:
        where.append("e.id IN (SELECT rowid FROM log_search WHERE log_search MATCH ?)")
        args.append(" ".join('"' + word.replace('"', '""') + '"' for word in indexed))
    for word in words:
        if len(word) < 3:
            where.append("e.search_text LIKE ? ESCAPE '\\'")
            args.append("%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    return " AND ".join(where), args


def csv_safe(value: Any) -> str:
    """A spreadsheet runs a cell starting with these as a formula, and these cells hold what members typed."""
    cell = "" if value is None else str(value)
    return "'" + cell if cell[:1] in ("=", "+", "-", "@", "\t", "\r") else cell


class Logs(Module):
    key = "logging"
    category = "Logging"
    schema = Schema({
        "default_channel": Field(None, optional(snowflake)),
        "message_channel": Field(None, optional(snowflake)),
        "member_channel": Field(None, optional(snowflake)),
        "join_channel": Field(None, optional(snowflake)),
        "server_channel": Field(None, optional(snowflake)),
        "voice_channel": Field(None, optional(snowflake)),
        "moderator_delete_channel": Field(None, optional(snowflake)),
        "role_channel": Field(None, optional(snowflake)),
        "name_channel": Field(None, optional(snowflake)),
        "timeout_channel": Field(None, optional(snowflake)),
        "events": Field(list(DEFAULT_EVENTS), list_of(choice(*EVENT_KEYS), len(EVENT_KEYS))),
        "ignored_channels": Field([], snowflakes(1000)),
        "ignored_members": Field([], snowflakes(1000)),
        "ignored_roles": Field([], snowflakes(250)),
        "ignored_prefixes": Field([], list_of(text(32, minimum=1, strip=False), 50)),
        "rename_ignored_bots": Field([], snowflakes(100)),
        "log_bot_actions": Field(True, boolean),
        "new_account_days": Field(1, integer(0, 365)),
        "attach_deleted_images": Field(True, boolean),
        "message_retention_days": Field(30, integer(1, 365)),
        "log_retention_days": Field(90, integer(1, 730)),
    })
    message_migrations = [
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            author_name TEXT NOT NULL,
            content TEXT NOT NULL,
            attachments TEXT,
            stickers TEXT,
            reply_to INTEGER,
            created_at REAL NOT NULL,
            edited_at REAL,
            deleted_at REAL,
            deleted_by INTEGER
        );
        CREATE INDEX messages_author ON messages (guild_id, author_id, id);
        CREATE INDEX messages_age ON messages (guild_id, created_at);
        CREATE TABLE log_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            at REAL NOT NULL,
            event TEXT NOT NULL,
            channel_id INTEGER,
            parent_id INTEGER,
            user_id INTEGER,
            actor_id INTEGER,
            message_id INTEGER,
            embed TEXT NOT NULL,
            search_text TEXT NOT NULL,
            detail TEXT
        );
        CREATE INDEX log_entries_time ON log_entries (guild_id, at, event);
        CREATE INDEX log_entries_user ON log_entries (guild_id, user_id, at);
        CREATE INDEX log_entries_actor ON log_entries (guild_id, actor_id, at);
        CREATE INDEX log_entries_channel ON log_entries (guild_id, channel_id, at);
        CREATE INDEX log_entries_parent ON log_entries (guild_id, parent_id, at);
        CREATE INDEX log_entries_event ON log_entries (guild_id, event, at);
        CREATE VIRTUAL TABLE log_search USING fts5(search_text, content='log_entries', content_rowid='id', tokenize='trigram');
        CREATE TRIGGER log_entries_added AFTER INSERT ON log_entries BEGIN
            INSERT INTO log_search (rowid, search_text) VALUES (new.id, new.search_text);
        END;
        CREATE TRIGGER log_entries_removed AFTER DELETE ON log_entries BEGIN
            INSERT INTO log_search (log_search, rowid, search_text) VALUES ('delete', old.id, old.search_text);
        END;
        """,
        """
        CREATE TABLE deleted_channel_readers (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            deleted_at REAL NOT NULL,
            PRIMARY KEY (guild_id, channel_id, role_id)
        );
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._new: dict[int, tuple] = {}
        self._edits: dict[int, tuple] = {}
        self._deletes: dict[int, tuple] = {}
        self._entries: list[tuple] = []
        self._flush_lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._flush_running: asyncio.Task | None = None
        self._closing = False
        self._bot_messages: collections.OrderedDict[int, tuple[int, str, str]] = collections.OrderedDict()
        self._delete_credits: dict[int, collections.OrderedDict[int, int]] = {}
        self._delete_waiting: dict[tuple, int] = {}
        self._delete_doubts: dict[tuple, int] = {}
        self._refused_at: dict[int, float] = {}
        self._refusal_told: dict[int, float] = {}
        self._delete_reads: dict[int, tuple[float, list]] = {}
        self._delete_read_locks: dict[int, asyncio.Lock] = {}
        self._explained: collections.OrderedDict[tuple[int, str], None] = collections.OrderedDict()
        self._copies_coming = 0
        self._copy_bytes_coming = 0
        self._deleted_roles: dict[int, set[int]] = {}
        self._automod_waiting: dict[tuple, dict] = {}
        self._automod_rules: dict[int, str] = {}
        self._audit_routes: dict[Any, Callable[[Any], Any]] = {
            Actions.kick: self._audit_kick,
            Actions.member_prune: self._audit_prune,
            Actions.bot_add: self._audit_bot_add,
            Actions.integration_create: self._audit_integration,
            Actions.integration_update: self._audit_integration,
            Actions.integration_delete: self._audit_integration,
            Actions.app_command_permission_update: self._audit_command_permissions,
            Actions.webhook_create: self._audit_webhook,
            Actions.webhook_update: self._audit_webhook,
            Actions.webhook_delete: self._audit_webhook,
            Actions.message_pin: self._audit_pin,
            Actions.message_unpin: self._audit_pin,
            Actions.member_move: self._audit_voice,
            Actions.member_disconnect: self._audit_voice,
            Actions.onboarding_create: self._audit_onboarding,
            Actions.onboarding_update: self._audit_onboarding,
            Actions.onboarding_prompt_create: self._audit_onboarding,
            Actions.onboarding_prompt_update: self._audit_onboarding,
            Actions.onboarding_prompt_delete: self._audit_onboarding,
            Actions.home_settings_create: self._audit_onboarding,
            Actions.home_settings_update: self._audit_onboarding,
        }

    async def on_load(self) -> None:
        self.bot.logs.refused_listener = self._channel_refused
        self.bot.timers.handler(PRUNE_TIMER, self._prune_due)
        if await self.bot.timers.get_key(PRUNE_TIMER, "all") is None:
            await self.bot.timers.schedule(PRUNE_TIMER, time.time() + PRUNE_SECONDS, key="all")

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {field: {settings[field]} for field in FIELD_LABELS if settings.get(field)}

    def _reads_what_it_covers(self, member: Any, settings: dict) -> bool:
        return access.reads_all_but(member, lambda channel: self._place_ignored(settings, member.guild, channel.id))

    def reading_refusal(self, member: Any, before: dict, after: dict) -> tuple[str, str] | None:
        """A message log shows deleted and edited messages from every channel it covers, so someone who can't read one of them can't aim the log somewhere, turn it on, or take that channel off the ignore list. A log already running stays as it is through other changes."""
        copying = set(after["events"]) & MESSAGE_EVENTS
        places = [field for field in MESSAGE_LOGS if after.get(field)]
        if not copying or not places:
            return None
        field = next((name for name in places if after[name] != before.get(name)), None)
        field = field or ("events" if copying - set(before["events"]) else "ignored_channels" if set(before["ignored_channels"]) - set(after["ignored_channels"]) else None)
        if field is None or self._reads_what_it_covers(member, after):
            return None
        return field, access.READS_ALL

    async def cog_unload(self) -> None:
        self._closing = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self.flush()

    # ---- what other features call ----

    async def stored_message(self, message_id: int) -> dict | None:
        """One message from the store, or None. The row carries guild_id, so a caller can check it belongs to the server it's working on."""
        if self._flush_lock.locked() or message_id in self._new or message_id in self._edits or message_id in self._deletes:
            await self.flush()
        row = await self.message_db.one(f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE id = ?", (message_id,))
        return message_dict(row) if row else None

    async def user_messages(self, guild_id: int, user_id: int, limit: int = 50) -> list[dict]:
        """A member's stored messages, newest first."""
        await self.flush()
        rows = await self.message_db.all(
            f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE guild_id = ? AND author_id = ? ORDER BY id DESC LIMIT ?",
            (guild_id, user_id, max(0, min(limit, MAX_READ_LIMIT))),
        )
        return [message_dict(row) for row in rows]

    async def user_log_entries(self, guild_id: int, user_id: int, limit: int = 50, kind: str | None = None) -> list[dict]:
        """The log entries about a member, newest first. `kind` narrows them to one card on the Logging page, such as "message"."""
        await self.flush()
        events = [event for event, group in EVENT_KINDS.items() if group == kind] if kind else []
        narrowed = f"AND event IN ({', '.join('?' * len(events))}) " if events else ""
        rows = await self.message_db.all(
            f"SELECT at, event, channel_id, user_id, message_id, embed FROM log_entries WHERE guild_id = ? AND user_id = ? {narrowed}ORDER BY at DESC, id DESC LIMIT ?",
            (guild_id, user_id, *events, max(0, min(limit, MAX_READ_LIMIT))),
        )
        for row in rows:
            row["embed"] = json.loads(row["embed"])
        return rows

    # ---- batched writes ----

    def _has_pending(self) -> bool:
        return bool(self._new or self._edits or self._deletes or self._entries)

    def _schedule_flush(self) -> None:
        if self._closing:
            return
        if len(self._new) > MAX_PENDING:  # the database has been failing for a long time, and memory still has to last
            del self._new[next(iter(self._new))]
        if len(self._entries) > MAX_PENDING:
            del self._entries[0]
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.ensure_future(self._flush_soon())

    async def _flush_soon(self) -> None:
        while self._has_pending():
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                # Shutting down cancels this task, and that must only ever stop the sleep above.
                self._flush_running = asyncio.ensure_future(self.flush())
                await asyncio.shield(self._flush_running)
            except Exception:
                log.exception("Could not write the message store. Trying again shortly.")

    async def flush(self) -> None:
        """Write everything waiting, in one transaction. If it fails nothing is lost: it all goes back in the queue."""
        async with self._flush_lock:
            if not self._has_pending() or self.message_db.conn is None:
                return
            new, edits, deletes, entries = self._new, self._edits, self._deletes, self._entries
            self._new, self._edits, self._deletes, self._entries = {}, {}, {}, []
            try:
                async with self.message_db.transaction():
                    if new:
                        await self.message_db.many(
                            "INSERT OR IGNORE INTO messages (id, guild_id, channel_id, author_id, author_name, content, attachments, stickers, reply_to, created_at, edited_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            new.values(),
                        )
                    if edits:
                        await self.message_db.many(
                            "UPDATE messages SET content = ?, attachments = ?, edited_at = ? WHERE id = ?",
                            [(*row, message_id) for message_id, row in edits.items()],
                        )
                    if deletes:
                        await self.message_db.many(
                            "UPDATE messages SET deleted_at = ?, deleted_by = COALESCE(?, deleted_by) WHERE id = ?",
                            [(*row, message_id) for message_id, row in deletes.items()],
                        )
                    if entries:
                        await self.message_db.many(
                            "INSERT INTO log_entries (guild_id, at, event, channel_id, parent_id, user_id, actor_id, message_id, embed, search_text, detail) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            entries,
                        )
            except BaseException:
                self._new = {**new, **self._new}
                self._edits = {**edits, **self._edits}
                self._deletes = {**deletes, **self._deletes}
                self._entries = entries + self._entries
                raise

    async def _prune_due(self, payload: dict, row: dict) -> None:
        try:
            await self.prune()
        finally:
            await self.bot.timers.schedule(PRUNE_TIMER, time.time() + PRUNE_SECONDS, key="all")

    async def prune(self, now: float | None = None) -> tuple[int, int]:
        """Remove what's past each server's retention. It deletes in small bites, so a big backlog can't hold the write lock for long."""
        now = time.time() if now is None else now
        await self.flush()
        messages = await self._prune_table("messages", "created_at", "message_retention_days", now)
        entries = await self._prune_table("log_entries", "at", "log_retention_days", now)
        # What says who could read a deleted channel ends when the last entry that could hang off it does.
        for row in await self.message_db.all("SELECT DISTINCT guild_id FROM deleted_channel_readers"):
            await self.message_db.run("DELETE FROM deleted_channel_readers WHERE guild_id = ? AND deleted_at < ?", (row["guild_id"], now - self.cfg(row["guild_id"])["log_retention_days"] * 86400))
        await self.bot.activity_log.prune(now)
        return messages, entries

    async def _prune_table(self, table: str, column: str, setting: str, now: float) -> int:
        removed = 0
        guild_id = await self.message_db.scalar(f"SELECT MIN(guild_id) FROM {table}")
        while guild_id is not None:
            cutoff = now - self.cfg(guild_id)[setting] * 86400
            while True:
                count = await self.message_db.run(
                    f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE guild_id = ? AND {column} < ? LIMIT ?)",
                    (guild_id, cutoff, PRUNE_CHUNK),
                )
                removed += count
                if count < PRUNE_CHUNK:
                    break
                await asyncio.sleep(0)
            guild_id = await self.message_db.scalar(f"SELECT MIN(guild_id) FROM {table} WHERE guild_id > ?", (guild_id,))
        return removed

    # ---- posting an entry ----

    def _channel_for(self, guild: Any, event: str) -> int | None:
        """The event's own channel, or its card's, or the default one. A channel has to be in this server, whatever the settings say, so logs can't reach another server. One Discord just refused is passed over while another is there."""
        cfg = self.cfg(guild.id)
        wanted = [cfg[EVENT_CHANNEL_FIELDS[event]]] if event in EVENT_CHANNEL_FIELDS else []
        wanted += [cfg[CHANNEL_FIELDS[EVENT_KINDS[event]]], cfg["default_channel"]]
        found = [channel_id for channel_id in wanted if channel_id and guild.get_channel_or_thread(channel_id) is not None]
        now = time.monotonic()
        return next((channel_id for channel_id in found if now >= self._refused_at.get(channel_id, 0.0)), found[0] if found else None)

    def _channel_refused(self, channel_id: int) -> None:
        """The log sink says Discord refused this channel with a 403. Entries go elsewhere for a while, and staff hear once an hour, through a channel that works."""
        now = time.monotonic()
        self._refused_at[channel_id] = now + REFUSED_SECONDS
        channel = self.bot.get_channel(channel_id)
        if channel is None or now < self._refusal_told.get(channel_id, 0.0):
            return
        self._refusal_told[channel_id] = now + REFUSED_SECONDS
        self.staff_notice(channel.guild, "A log channel needs a look", f"The bot can't post in <#{channel_id}>. It needs View Channel, Send Messages and Embed Links there. Until then its entries go to the next log channel that's set, and the Log search page has them all.")

    async def _stored_parent(self, guild: Any, channel_id: int | None) -> int | None:
        """A thread that's archived isn't in the cache, so its parent comes from an earlier entry about it. A private thread's entries name no parent, so it stays closed the same way."""
        if channel_id is None or guild.get_channel_or_thread(channel_id) is not None:
            return None
        return await self.message_db.scalar("SELECT parent_id FROM log_entries WHERE guild_id = ? AND channel_id = ? AND parent_id IS NOT NULL ORDER BY at DESC LIMIT 1", (guild.id, channel_id))

    def _store_entry(self, guild_id: int, event: str, embed: dict, *, at: float | None = None, channel_id: int | None = None, parent_id: int | None = None, user_id: int | None = None,
                     actor_id: int | None = None, message_id: int | None = None, search: str | None = None, detail: str | None = None) -> None:
        # A file the entry points at only exists in the channel it was posted to, so the stored copy can't refer to it.
        stored = {key: value for key, value in embed.items() if not (key == "image" and str(value.get("url", "")).startswith("attachment://"))}
        words = join_lines(embed_text(stored), search, detail)
        self._entries.append((guild_id, at or time.time(), event, channel_id, parent_id, user_id, actor_id, message_id, json.dumps(stored, ensure_ascii=False), words, detail))
        self._schedule_flush()

    def _emit(self, guild: Any, event: str, embed: dict, *, channel_id: int | None = None, user_id: int | None = None, actor: Any = None, message_id: int | None = None,
              search: str | None = None, detail: str | None = None, detail_name: str = "details.txt", images: list[discord.File] | None = None, parent_id: int | None = None) -> None:
        """Keep one entry for the search page and post it to its channel. An entry without a log channel is still kept. `parent_id` is for a thread the cache doesn't hold."""
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        self._store_entry(
            guild.id, event, embed, channel_id=channel_id, parent_id=parent_id or search_parent(getattr(channel, "type", None), getattr(channel, "parent_id", None)), user_id=user_id,
            actor_id=getattr(actor, "user_id", None), message_id=message_id, search=search, detail=detail,
        )
        files = list(images or [])
        if detail:
            files.append(discord.File(io.BytesIO(detail.encode("utf-8")[:MAX_DETAIL_BYTES]), filename=detail_name))
        self.bot.logs.post(self._channel_for(guild, event), embed, file=files or None)

    def keep_entry(self, guild: Any, event: str, embed: dict, *, channel_id: int | None = None, user_id: int | None = None, message_id: int | None = None, search: str | None = None, detail: str | None = None) -> None:
        """Keep an entry for the search page and the review's gather step when another feature posted it to a channel of its own."""
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        self._store_entry(guild.id, event, embed, channel_id=channel_id, parent_id=search_parent(getattr(channel, "type", None), getattr(channel, "parent_id", None)), user_id=user_id, message_id=message_id, search=search, detail=detail)

    def staff_notice(self, guild: Any, title: str, text: str) -> None:
        """Something staff have to act on, from any feature. It goes to the server log channel whichever events are on, and to the search page."""
        self._emit(guild, "server", make_embed(title, description=text, color=ORANGE))

    # ---- who did it ----

    def _reads_audit(self, guild: Any) -> bool:
        me = guild.me
        return bool(me is not None and me.guild_permissions.view_audit_log)

    async def _who(self, guild: Any, actions: Iterable[Any], target_id: Any, *, where: Callable[[Any], bool] | None = None, wait: float | None = None, aspect: str = "") -> Any:
        """The audit log entry behind a change, if it arrives soon.

        Discord can send the change before its entry, so an entry for an earlier change to the same target may be all there is so far. An entry explains one change, and one already used is passed over. `aspect` is for two changes one entry can explain together, such as a nickname and a timeout.
        """
        if not self._reads_audit(guild):
            return None
        actions = tuple(actions)
        deadline = time.monotonic() + (AUDIT_WAIT if wait is None else wait)

        def fits(entry: Any) -> bool:
            return (entry.id, aspect) not in self._explained and (where is None or where(entry))

        while True:
            for action in actions:
                entry = await self.bot.audit.find(guild.id, action, target_id, wait=0, where=fits)
                if entry is not None:
                    self._explained[(entry.id, aspect)] = None
                    while len(self._explained) > EXPLAINED_KEPT:
                        self._explained.popitem(last=False)
                    return entry
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(AUDIT_POLL)

    def _actor_ignored(self, cfg: dict, entry: Any) -> bool:
        return entry is not None and entry.user_id in cfg["ignored_members"]

    def _actor_is_bot(self, entry: Any) -> bool:
        return entry is not None and bool(getattr(entry.user, "bot", False))

    def _place_ignored(self, cfg: dict, guild: Any, channel_id: int | None) -> bool:
        """A thread takes its parent's place on the ignore list, and any channel takes its category's."""
        if channel_id is None or not cfg["ignored_channels"]:
            return False
        ignored = cfg["ignored_channels"]
        if channel_id in ignored:
            return True
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            return False
        parent_id = getattr(channel, "parent_id", None)
        if parent_id in ignored:
            return True
        return getattr(channel, "category_id", None) in ignored

    def _text_ignored(self, cfg: dict, *texts: str) -> bool:
        return any(value.startswith(prefix) for value in texts for prefix in cfg["ignored_prefixes"])

    async def _who_deleted(self, guild: Any, author_id: int | None, channel_id: int, arrived: float) -> Any:
        """Who removed someone else's message.

        Discord writes no audit entry when people delete their own message. When a moderator or bot does it, the first delete makes
        an entry, and further deletes of that author's messages in that channel only raise the entry's count, with no gateway event.
        So the log is read over REST, and each entry's count is compared with how many deletes it already explained.

        An entry says whose message and which channel, never which message. When more deletes of that author's messages in that channel are waiting than the count went up by, some were the author's own and nothing says which. Those get UNCERTAIN rather than the wrong name.
        """
        if not self._reads_audit(guild):
            return None
        place = (guild.id, author_id, channel_id)
        self._delete_waiting[place] = self._delete_waiting.get(place, 0) + 1
        try:
            await asyncio.sleep(DELETE_SETTLE)
            if author_id is None:
                # Any entry in the channel could seem to fit a message nothing is known about, so deletes with a known author choose first.
                await asyncio.sleep(DELETE_LATE_WAIT)
            entries = await self._recent_deletes(guild, arrived)  # a failed read only means nobody gets named
            credits = self._delete_credits.setdefault(guild.id, collections.OrderedDict())
            claimed = self._claim_delete(credits, entries, author_id, channel_id, place)
            if claimed is not None:
                return claimed

            def unexplained(entry: Any) -> bool:
                return getattr(getattr(entry.extra, "channel", None), "id", None) == channel_id and entry.id not in credits

            late = await self.bot.audit.find(guild.id, Actions.message_delete, author_id, within=DELETE_SETTLE + DELETE_LATE_WAIT + 2, wait=DELETE_LATE_WAIT, where=unexplained)
            if late is None:
                return None
            credits[late.id] = 1
            return self._named_or_uncertain(place, late, 1)
        finally:
            left = self._delete_waiting[place] - 1
            if left:
                self._delete_waiting[place] = left
            else:
                del self._delete_waiting[place]

    def _named_or_uncertain(self, place: tuple, entry: Any, explained: int) -> Any:
        waiting = self._delete_waiting.get(place, 1)
        if waiting <= explained:
            return entry
        self._delete_doubts[place] = waiting - 1  # the deletes waiting behind this one can't be told from it either
        return UNCERTAIN

    def _claim_delete(self, credits: collections.OrderedDict, entries: list, author_id: int | None, channel_id: int, place: tuple) -> Any:
        now = discord.utils.utcnow()
        claimed = None
        doubted = self._delete_doubts.get(place, 0)
        for entry in entries:  # newest first
            count = getattr(entry.extra, "count", 1)
            seen = credits.get(entry.id)
            fresh = (now - entry.created_at).total_seconds() < FRESH_ENTRY_SECONDS
            if seen is None and not fresh:
                # Already old the first time it's seen, so the deletes it counts happened before anyone was watching.
                credits[entry.id] = count
                continue
            if doubted or claimed is not None or getattr(getattr(entry.extra, "channel", None), "id", None) != channel_id:
                continue
            if author_id is not None and getattr(entry.target, "id", None) != author_id:
                continue
            risen = count if seen is None else count - seen
            if risen <= 0:
                continue
            claimed = self._named_or_uncertain(place, entry, risen)
            credits[entry.id] = count if claimed is UNCERTAIN else (seen or 0) + 1
        while len(credits) > DELETE_CREDITS_KEPT:
            credits.popitem(last=False)
        if doubted:
            if doubted > 1:
                self._delete_doubts[place] = doubted - 1
            else:
                del self._delete_doubts[place]
            return UNCERTAIN
        return claimed

    async def _recent_deletes(self, guild: Any, arrived: float) -> list:
        """One read of the log serves every delete waiting on it, so a burst of deletes doesn't become a burst of requests."""
        lock = self._delete_read_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            read_at, entries = self._delete_reads.get(guild.id, (0.0, []))
            # A read from before this delete's entry was written can't explain it, and neither can one in the same clock tick.
            if read_at <= arrived + min(AUDIT_READ_GAP, DELETE_SETTLE):
                entries = await self._read_audit(guild, Actions.message_delete, AUDIT_READ_SIZE)
                self._delete_reads[guild.id] = (time.monotonic(), entries)
            return entries

    async def _read_audit(self, guild: Any, action: Any, limit: int) -> list:
        """Naming who did something is a bonus, so a read that fails can't be allowed to cost the entry itself."""
        try:
            return await self.bot.audit.recent(guild, action, limit=limit)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            log.warning("Could not read the audit log for %s: %s", guild.id, error)
            return []

    async def _who_purged(self, guild: Any, channel_id: int) -> Any:
        entry = await self._who(guild, [Actions.message_bulk_delete], channel_id)
        credits = self._delete_credits.setdefault(guild.id, collections.OrderedDict())
        if entry is not None:
            credits[entry.id] = getattr(entry.extra, "count", 0)
            return entry
        if not self._reads_audit(guild):
            return None
        # A second purge soon after the first raises the first entry's count and fires no gateway event.
        now = discord.utils.utcnow()
        for row in await self._read_audit(guild, Actions.message_bulk_delete, 10):
            count = getattr(row.extra, "count", 0)
            seen = credits.get(row.id)
            credits[row.id] = count
            if getattr(row.target, "id", None) != channel_id:
                continue
            if (seen is None and (now - row.created_at).total_seconds() < FRESH_ENTRY_SECONDS) or (seen is not None and count > seen):
                return row
        return None

    # ---- messages ----

    def _remember_bot_message(self, message: Any) -> None:
        """Enough of a bot's or webhook's message to log it if a moderator removes it. These stay out of the store, so this lives in memory and a restart loses it."""
        self._bot_messages[message.id] = (message.author.id, message.author.name, bot_message_text(message))
        if len(self._bot_messages) > BOT_MESSAGES_KEPT:
            self._bot_messages.popitem(last=False)

    def _channel_name(self, guild: Any, channel_id: int | None) -> str:
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        return channel.name if channel is not None else "unknown-channel"

    def _author_of(self, guild: Any, stored: dict) -> dict:
        member = guild.get_member(stored["author_id"])
        return author_block(member) if member is not None else {"name": clip(stored["author_name"], 256)}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if message.author.bot or message.webhook_id is not None:
            self._remember_bot_message(message)
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply) or not self.bot.reads_message_text:
            return
        self._new[message.id] = message_row(message)
        self._schedule_flush()
        cfg = self.cfg(message.guild.id)
        if "discord" not in cfg["events"] or not message.content:
            return
        codes = list(dict.fromkeys(INVITE_LINK.findall(message.content)))
        if not codes or message.author.id in cfg["ignored_members"] or self._place_ignored(cfg, message.guild, message.channel.id):
            return
        embed = make_embed(
            f"Invite posted in #{self._channel_name(message.guild, message.channel.id)}", description=quoted(message.content), url=message.jump_url, author=author_block(message.author),
            fields=[make_field("Invite codes", ", ".join(codes), inline=True)], footer=f"ID: {message.author.id}",
        )
        self._emit(message.guild, "discord", embed, channel_id=message.channel.id, user_id=message.author.id, message_id=message.id)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        message = payload.message
        if payload.guild_id is None or message.author.bot or message.webhook_id is not None or not self.bot.reads_message_text:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        stored = await self.stored_message(message.id)
        after = message_text(message)
        names_now = {a.filename for a in message.attachments}
        edited_at = message.edited_at.timestamp() if message.edited_at else time.time()
        # An event right behind another one, as when a resume replays a burst, can read the row before the first one's change lands in it.
        if stored is None and message.id in self._new:
            stored = await self.stored_message(message.id)
        if stored is not None and message.id in self._edits:
            waiting = self._edits[message.id]
            stored = {**stored, "content": waiting[0], "attachments": json.loads(waiting[1]) if waiting[1] else []}
        if stored is None:
            # An old message. Links loading and pins send this event too, and only a fresh edit time means someone edited it.
            if message.edited_at is None or time.time() - edited_at > 60:
                return
            self._new[message.id] = message_row(message)
            before, removed = None, []
        else:
            before = stored["content"]
            removed = [a["filename"] for a in stored["attachments"] if a["filename"] not in names_now]
            if before == after and not removed:
                return
            rows = attachment_rows(message.attachments)
            self._edits[message.id] = (after, json.dumps(rows, ensure_ascii=False) if rows else None, edited_at)
        self._schedule_flush()
        cfg = self.cfg(guild.id)
        if "edit" not in cfg["events"] or message.author.id in cfg["ignored_members"] or self._place_ignored(cfg, guild, payload.channel_id):
            return
        if self._text_ignored(cfg, before or "", after):
            return
        fields, cut = before_after("(not in the store)" if before is None else before, after)
        fields += [make_field("Message ID", str(message.id), inline=True), sent_field(message.created_at.timestamp())]
        if removed:
            fields.append(make_field("Attachments removed", "\n".join(removed), inline=True))
        embed = make_embed(
            f"Message edited in #{self._channel_name(guild, payload.channel_id)}", url=message.jump_url, author=author_block(message.author), fields=fields, footer=f"ID: {message.author.id}",
        )
        shown = next((a for a in attachment_rows(message.attachments) if is_image(a) and not a["filename"].startswith("SPOILER_")), None)
        if shown is not None:
            embed["image"] = {"url": shown["url"]}
        detail = f"Before:\n{before or ''}\n\nAfter:\n{after}" if cut else None
        parent_id = await self._stored_parent(guild, payload.channel_id)
        self._emit(guild, "edit", embed, channel_id=payload.channel_id, user_id=message.author.id, message_id=message.id, detail=detail, detail_name="edited-message.txt", parent_id=parent_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        arrived, now = time.monotonic(), time.time()
        stored = await self.stored_message(payload.message_id)
        if stored is not None:
            self._deletes[payload.message_id] = (now, None)
            self._schedule_flush()
        kept = self._bot_messages.pop(payload.message_id, None)
        known = stored or self._row_from_kept(kept, payload.message_id)
        cfg = self.cfg(guild.id)
        if not {"delete", "moddelete"} & set(cfg["events"]) or self._place_ignored(cfg, guild, payload.channel_id):
            return
        if known is not None and (known["author_id"] in cfg["ignored_members"] or self._text_ignored(cfg, known["content"])):
            return
        copying = None
        room = 0
        chosen: list[dict] = []
        if stored is not None and stored["attachments"] and cfg["attach_deleted_images"]:
            chosen = self._images_with_room(guild, stored["attachments"])
            if chosen:
                room = sum(attachment.get("size") or 0 for attachment in chosen)
                self._copies_coming += 1
                self._copy_bytes_coming += room
                # Discord stops serving a deleted file soon after, so this starts before anything else waits.
                copying = asyncio.ensure_future(self._copy_images(chosen))
        try:
            who = await self._who_deleted(guild, known["author_id"] if known else None, payload.channel_id, arrived)
            images = await copying if copying is not None else []
            uncertain = who is UNCERTAIN
            entry = None if uncertain else who
            if entry is None and not uncertain:
                # Nobody is named, so a bot deleted its own message or this is someone's own delete. A bot doing that is routine, and this bot's own housekeeping is never news.
                if kept is not None and (not cfg["log_bot_actions"] or kept[0] == getattr(self.bot.user, "id", None)):
                    return
                if known is None and not self.bot.reads_message_text:
                    return  # without message text the store is empty on purpose, and every delete would read as an old message
            if stored is not None and entry is not None:
                self._deletes[payload.message_id] = (now, entry.user_id)
                self._schedule_flush()
            if self._actor_ignored(cfg, entry) or (self._actor_is_bot(entry) and not cfg["log_bot_actions"]):
                return
            someone_else = entry is not None and (known is None or entry.user_id != known["author_id"])
            event = "moddelete" if uncertain or someone_else else "delete"
            if event not in cfg["events"]:
                return
            embed = self._deleted_embed(guild, payload, known, entry, images, uncertain=uncertain, missed=len(chosen) - len(images))
            user_id = known["author_id"] if known else getattr(entry, "target_id", None) or getattr(getattr(entry, "target", None), "id", None)
            # A very long message has to survive somewhere whole.
            cut = stored["content"] if stored and quoted(stored["content"]) not in embed.get("description", "") else None
            parent_id = await self._stored_parent(guild, payload.channel_id)
            self._emit(guild, event, embed, channel_id=payload.channel_id, user_id=user_id, actor=entry, message_id=payload.message_id, images=images, detail=cut, detail_name="deleted-message.txt", parent_id=parent_id)
        finally:
            if copying is not None:
                self._copies_coming -= 1
                self._copy_bytes_coming -= room

    @staticmethod
    def _row_from_kept(kept: tuple[int, str, str] | None, message_id: int) -> dict | None:
        """A bot's message held in memory, in the shape of a row from the store."""
        if kept is None:
            return None
        return {
            "author_id": kept[0], "author_name": kept[1], "content": kept[2], "attachments": [], "stickers": [], "reply_to": None,
            "created_at": discord.utils.snowflake_time(message_id).timestamp(),
        }

    def _deleted_by(self, guild: Any, entry: Any, uncertain: bool) -> list[dict]:
        """Who deleted it, always said, because no line at all reads the same as a read that failed."""
        if uncertain:
            return [make_field("Deleted by", "Uncertain. Two deletes landed together, and Discord doesn't say which was whose.")]
        if entry is not None:
            return by_fields(entry, "Deleted by")
        return [make_field("Deleted by", "The author or a bot" if self._reads_audit(guild) else "Unknown, no audit log access")]

    def _deleted_embed(self, guild: Any, payload: Any, stored: dict | None, entry: Any, images: list[discord.File], *, uncertain: bool = False, missed: int = 0) -> dict:
        title = f"Message deleted in #{self._channel_name(guild, payload.channel_id)}"
        facts = [make_field("Message ID", str(payload.message_id), inline=True), sent_field(stored["created_at"] if stored else discord.utils.snowflake_time(payload.message_id).timestamp())]
        facts += self._deleted_by(guild, entry, uncertain)
        if stored is None:
            target = getattr(entry, "target", None)
            target_id = getattr(entry, "target_id", None) or getattr(target, "id", None)
            member = guild.get_member(target_id) if target_id else None
            if target_id:
                facts.insert(0, make_field("Author", who_text(member, target_id)))
            return make_embed(title, description="This message isn't in the store, so its text is gone.", color=RED, author=author_block(member), fields=facts, footer=f"ID: {target_id}" if target_id else None)
        fields = []
        if stored["attachments"]:
            names = "\n".join(a["filename"] for a in stored["attachments"])
            if missed > 0:
                names += f"\n{missed} image{'' if missed == 1 else 's'} could no longer be fetched."
            fields.append(make_field("Attachments", names, inline=True))
        if stored["stickers"]:
            fields.append(make_field("Stickers", "\n".join(stored["stickers"]), inline=True))
        if stored["reply_to"]:
            fields.append(make_field("Reply to", f"https://discord.com/channels/{guild.id}/{payload.channel_id}/{stored['reply_to']}", inline=True))
        embed = make_embed(title, description=quoted(stored["content"]) or None, color=RED, author=self._author_of(guild, stored), fields=[*fields, *facts], footer=f"ID: {stored['author_id']}")
        # A spoilered image stays behind its warning as a plain attachment instead of opening inside the entry.
        shown = next((image for image in images if not image.filename.startswith("SPOILER_")), None)
        if shown is not None:
            embed["image"] = {"url": f"attachment://{shown.filename}"}
        return embed

    def _images_with_room(self, guild: Any, attachments: list[dict]) -> list[dict]:
        """The images worth copying from a deleted message. A copy stays in memory until it's posted, and posts go out one every couple of seconds, so copies being fetched count along with copies waiting. In a flood the entries still go out, without the copies."""
        if self.bot.logs.queued_files() + self._copies_coming >= MAX_QUEUED_COPIES:
            return []
        limit = guild.filesize_limit
        chosen, total = [], 0
        for attachment in attachments:
            size = attachment.get("size") or 0
            if is_image(attachment) and total + size <= limit and len(chosen) < MAX_COPIED_IMAGES:
                chosen.append(attachment)
                total += size
        if self.bot.logs.queued_bytes() + self._copy_bytes_coming + total > MAX_COPY_BYTES:
            return []
        return chosen

    async def _copy_images(self, chosen: list[dict]) -> list[discord.File]:
        blobs = await asyncio.gather(*(self._download(attachment) for attachment in chosen))
        taken: set[str] = set()
        return [discord.File(io.BytesIO(blob), filename=upload_name(attachment["filename"], taken)) for attachment, blob in zip(chosen, blobs) if blob]

    async def _download(self, attachment: dict) -> bytes | None:
        for url in (attachment.get("url"), attachment.get("proxy_url")):
            try:
                # These links come from Discord's own payloads, and a restored backup is the one way a row could say otherwise.
                if url and urlsplit(url).hostname in CDN_HOSTS:
                    return await asyncio.wait_for(self.bot.http.get_from_cdn(url), DOWNLOAD_SECONDS)
            except (discord.HTTPException, asyncio.TimeoutError, aiohttp.ClientError, OSError, ValueError) as error:
                log.debug("Could not copy a deleted image: %s", error)
        return None

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        now = time.time()
        await self.flush()
        ids = sorted(payload.message_ids)
        marks = ",".join("?" * len(ids))
        rows = await self.message_db.all(f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE id IN ({marks})", ids) if ids else []
        stored = {row["id"]: message_dict(row) for row in rows}
        for message_id in stored:
            self._deletes[message_id] = (now, None)
        if stored:
            self._schedule_flush()
        cfg = self.cfg(guild.id)
        if "purge" not in cfg["events"] or self._place_ignored(cfg, guild, payload.channel_id):
            return
        lines, shown = [], 0
        for message_id in ids:
            row = stored.get(message_id)
            if row is None:
                kept = self._bot_messages.get(message_id)
                if kept is None:
                    lines.append(f"[not in the store] Message ID {message_id}")
                    shown += 1
                elif kept[0] not in cfg["ignored_members"] and not self._text_ignored(cfg, kept[2]):
                    lines.append(f"[a bot] {kept[1]} ({kept[0]}): {kept[2]}")
                    shown += 1
                continue
            if row["author_id"] in cfg["ignored_members"] or self._text_ignored(cfg, row["content"]):
                continue
            shown += 1
            stamp = datetime.datetime.fromtimestamp(row["created_at"], datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            lines.append(f"[{stamp}] {row['author_name']} ({row['author_id']}): {row['content']}")
            lines.extend(f"    Attachment: {a['filename']} {a['url']}" for a in row["attachments"])
            lines.extend(f"    Sticker: {name}" for name in row["stickers"])
        if not shown:
            return
        entry = await self._who_purged(guild, payload.channel_id)
        if self._actor_ignored(cfg, entry) or (self._actor_is_bot(entry) and not cfg["log_bot_actions"]):
            return
        name = self._channel_name(guild, payload.channel_id)
        embed = make_embed(
            f"Messages purged in #{name}", description=join_lines(f"**Messages:** {len(ids)}", by_lines(entry)), color=RED, footer=f"Channel ID: {payload.channel_id}",
        )
        parent_id = await self._stored_parent(guild, payload.channel_id)
        self._emit(guild, "purge", embed, channel_id=payload.channel_id, actor=entry, detail="\n".join([f"{len(ids)} messages deleted in #{name}", "", *lines]), detail_name="purged-messages.txt", parent_id=parent_id)

    # ---- joining and leaving ----

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = self.cfg(member.guild.id)
        if "join" not in cfg["events"] or member.id in cfg["ignored_members"]:
            return
        age = (discord.utils.utcnow() - member.created_at).total_seconds()
        created = humanize(age)
        limit = cfg["new_account_days"] * 86400
        warning = f"\N{WARNING SIGN} **NEW ACCOUNT** created {created} ago \N{WARNING SIGN}" if limit and age < limit else f"created {created} ago"
        place = member.guild.member_count or len(member.guild.members)  # Discord can leave the count out for a moment after a resume
        embed = make_embed("Member joined", description=f"{member.mention} {ordinal(place)} to join\n{warning}", color=GREEN, author=author_block(member), footer=f"ID: {member.id}")
        self._emit(member.guild, "join", embed, user_id=member.id)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        cfg = self.cfg(guild.id)
        user = payload.user
        if "leave" not in cfg["events"] or user.id in cfg["ignored_members"]:
            return
        joined = getattr(user, "joined_at", None)
        roles = [role for role in reversed(getattr(user, "roles", [])) if not role.is_default()]
        embed = make_embed(
            "Member left",
            description=join_lines(
                f"{user.mention} joined {humanize((discord.utils.utcnow() - joined).total_seconds())} ago" if joined else user.mention,
                f"**Roles:** {', '.join(role.mention for role in roles)}" if roles else None,
            ),
            color=YELLOW, author=author_block(user), footer=f"ID: {user.id}",
        )
        self._emit(guild, "leave", embed, user_id=user.id, search=" ".join(role.name for role in roles))

    # ---- member changes ----

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        guild = after.guild
        cfg = self.cfg(guild.id)
        if after.id in cfg["ignored_members"]:
            return
        jobs = []
        if "role" in cfg["events"] and {r.id for r in before.roles} != {r.id for r in after.roles}:
            jobs.append(self._log_roles(before, after, cfg))
        elif any(role.id not in {r.id for r in after.roles} for role in before.roles):
            jobs.append(self._note_lost_role(before, after))
        if "name" in cfg["events"] and before.nick != after.nick:
            jobs.append(self._log_nick(before, after, cfg))
        if before.timed_out_until != after.timed_out_until:
            jobs.append(self._log_timeout(before, after, cfg))
        if "avatar" in cfg["events"] and getattr(before, "guild_avatar", None) != getattr(after, "guild_avatar", None):
            self._log_avatar(guild, after)
        if jobs:
            await asyncio.gather(*jobs)

    async def _note_lost_role(self, before: Any, after: Any) -> list[Any]:
        """Roles this member lost that are still roles. One that was deleted gets a single entry listing everyone who had it, instead of one per member."""
        now = {r.id for r in after.roles}
        lost = [role for role in before.roles if role.id not in now]
        if not lost:
            return []
        await asyncio.sleep(ROLE_DELETE_GRACE)
        kept = []
        for role in lost:
            holders = self._deleted_roles.get(role.id)
            if holders is not None:
                holders.add(after.id)
            elif after.guild.get_role(role.id) is not None:
                kept.append(role)
        return kept

    async def _log_roles(self, before: Any, after: Any, cfg: dict) -> None:
        guild = after.guild
        old, new = {r.id for r in before.roles}, {r.id for r in after.roles}
        gained = new - old
        lost = old - new
        added = [role for role in after.roles if role.id not in old]
        removed = await self._note_lost_role(before, after)
        ignored = set(cfg["ignored_roles"])
        added = [role for role in added if role.id not in ignored]
        removed = [role for role in removed if role.id not in ignored]
        if not added and not removed:
            return

        def same_change(entry: Any) -> bool:
            changes = entry.changes
            return {r.id for r in getattr(changes.after, "roles", [])} == gained and {r.id for r in getattr(changes.before, "roles", [])} == lost

        entry = await self._who(guild, [Actions.member_role_update], after.id, where=same_change)
        if self._actor_ignored(cfg, entry):
            return
        if not cfg["log_bot_actions"] and (self._actor_is_bot(entry) or (entry is None and all(role.managed for role in [*added, *removed]))):
            return
        def named(role: Any) -> str:
            return f"{role.mention} ({discord.utils.escape_markdown(role.name)})"  # the mention reads @deleted-role once the role is gone

        if len(added) == 1 and not removed:
            title, body = "Role added", named(added[0])
        elif len(removed) == 1 and not added:
            title, body = "Role removed", named(removed[0])
        else:
            title = "Roles updated"
            body = join_lines(
                f"**Added:** {', '.join(named(role) for role in added)}" if added else None,
                f"**Removed:** {', '.join(named(role) for role in removed)}" if removed else None,
            )
        by = by_lines(entry) or self._who_hands_out([*added, *removed])
        embed = make_embed(title, description=join_lines(body, by), author=author_block(after), footer=f"ID: {after.id}")
        self._emit(guild, "role", embed, user_id=after.id, actor=entry, search=" ".join(role.name for role in [*added, *removed]))

    @staticmethod
    def _who_hands_out(roles: list[Any]) -> str:
        """Roles nobody hands out by hand have no audit entry, and they say where they come from."""
        if all(role.is_premium_subscriber() or role.is_bot_managed() for role in roles):
            return "**By:** Discord"
        return "**By:** an integration" if all(role.is_integration() for role in roles) else ""

    async def _log_nick(self, before: Any, after: Any, cfg: dict) -> None:
        guild = after.guild
        entry = await self._who(guild, [Actions.member_update], after.id, where=lambda e: hasattr(e.changes.after, "nick") and e.changes.after.nick == after.nick, aspect="nick")
        if self._actor_ignored(cfg, entry) or (self._actor_is_bot(entry) and not cfg["log_bot_actions"]):
            return
        title = "Nickname added" if before.nick is None else "Nickname removed" if after.nick is None else "Nickname changed"
        # People changing their own nickname is the ordinary case, so only someone else's hand gets named.
        by = by_lines(entry) if entry is not None and entry.user_id != after.id else ""
        description = join_lines(f"**Before:** {before.nick or before.name}\n**+After:** {after.nick or after.name}", by)
        embed = make_embed(title, description=description, author=author_block(after), footer=f"ID: {after.id}")
        self._emit(guild, "name", embed, user_id=after.id, actor=entry)

    async def _log_timeout(self, before: Any, after: Any, cfg: dict) -> None:
        guild = after.guild
        if after.timed_out_until is not None and after.is_timed_out():
            event = "timeout"
        elif before.is_timed_out():
            event = "removetimeout"
        else:
            return  # an expired timeout being cleared, which nobody did
        if event not in cfg["events"]:
            return
        # An entry for setting a timeout says nothing about who lifted it.
        entry = await self._who(guild, [Actions.member_update], after.id, where=lambda e: hasattr(e.changes.after, "timed_out_until") and bool(e.changes.after.timed_out_until) == (event == "timeout"), aspect="timeout")
        if self._actor_ignored(cfg, entry) or (self._actor_is_bot(entry) and not cfg["log_bot_actions"]):
            return
        if event == "timeout":
            stamp = int(after.timed_out_until.timestamp())
            title, color, until = "Member timed out", ORANGE, f"**Until:** <t:{stamp}:f> (<t:{stamp}:R>)"
        else:
            title, color, until = "Timeout removed", GREEN, None
        embed = make_embed(title, description=join_lines(after.mention, until, by_lines(entry)), color=color, author=author_block(after), footer=f"ID: {after.id}")
        self._emit(guild, event, embed, user_id=after.id, actor=entry)

    def _log_avatar(self, guild: Any, user: Any) -> None:
        embed = make_embed("Avatar update", description=user.mention, author=author_block(user), thumbnail=user.display_avatar.url, footer=f"ID: {user.id}")
        self._emit(guild, "avatar", embed, user_id=user.id)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        avatar = before.avatar != after.avatar
        names = []
        if before.name != after.name:
            names.append(("Name change", before.name, after.name))
        if before.global_name != after.global_name:
            names.append(("Display name change", before.global_name or before.name, after.global_name or after.name))
        if not avatar and not names:
            return
        for guild in self.bot.guilds:
            if guild.get_member(after.id) is None:
                continue
            cfg = self.cfg(guild.id)
            if after.id in cfg["ignored_members"]:
                continue
            if avatar and "avatar" in cfg["events"]:
                self._log_avatar(guild, after)
            if "name" in cfg["events"]:
                for title, old, new in names:
                    embed = make_embed(title, description=f"**Before:** {old}\n**+After:** {new}", author=author_block(after), footer=f"ID: {after.id}")
                    self._emit(guild, "name", embed, user_id=after.id)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: Any) -> None:
        await self._log_ban(guild, user, "ban", "Member banned", RED, Actions.ban)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: Any) -> None:
        await self._log_ban(guild, user, "unban", "Member unbanned", GREEN, Actions.unban)

    async def _log_ban(self, guild: Any, user: Any, event: str, title: str, color: int, action: Any) -> None:
        cfg = self.cfg(guild.id)
        if event not in cfg["events"] or user.id in cfg["ignored_members"]:
            return
        entry = await self._who(guild, [action], user.id)
        if self._actor_ignored(cfg, entry):
            return
        embed = make_embed(title, description=join_lines(user.mention, by_lines(entry)), color=color, author=author_block(user), footer=f"ID: {user.id}")
        self._emit(guild, event, embed, user_id=user.id, actor=entry)

    # ---- channels ----

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: Any) -> None:
        guild = channel.guild
        cfg = self.cfg(guild.id)
        if "channelcreate" not in cfg["events"] or self._place_ignored(cfg, guild, channel.id):
            return
        entry = await self._who(guild, [Actions.channel_create], channel.id)
        if self._actor_ignored(cfg, entry):
            return
        lines = join_lines(f"**Name:** {channel.name}", f"**Category:** {channel.category.name}" if channel.category else None, by_lines(entry))
        embed = make_embed(f"{channel_kind(channel)} created", description=lines, color=GREEN, fields=overwrite_fields(channel), footer=f"Channel ID: {channel.id}")
        self._emit(guild, "channelcreate", embed, channel_id=channel.id, actor=entry)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: Any, after: Any) -> None:
        guild = after.guild
        cfg = self.cfg(guild.id)
        if "channelupdate" not in cfg["events"] or self._place_ignored(cfg, guild, after.id):
            return
        old, new = channel_facts(before), channel_facts(after)
        changes = changed_fields(old, new)
        overrides = overwrite_changes(before, after)
        if not changes and not overrides:
            return
        only_rename = not overrides and old["Name"] != new["Name"] and all(old.get(key) == value for key, value in new.items() if key != "Name")
        actions = []
        if changes:
            actions.append(Actions.channel_update)
        if overrides:
            actions += [Actions.overwrite_create, Actions.overwrite_update, Actions.overwrite_delete]
        entry = await self._who(guild, actions, after.id, wait=RENAME_AUDIT_WAIT if only_rename and cfg["rename_ignored_bots"] else None)
        if self._actor_ignored(cfg, entry):
            return
        if only_rename and entry is not None and entry.user_id in cfg["rename_ignored_bots"]:
            return
        embed = make_embed(f"{channel_kind(after)} updated", description=by_lines(entry) or None, fields=[*changes, *overrides], footer=f"Channel ID: {after.id}")
        self._emit(guild, "channelupdate", embed, channel_id=after.id, actor=entry, search=old["Name"])

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        guild = channel.guild
        cfg = self.cfg(guild.id)
        await self._remember_readers(channel)
        held = [label for field, label in FIELD_LABELS.items() if cfg[field] == channel.id]
        if held:
            self.staff_notice(guild, "A log channel was deleted", f"#{channel.name} was the {' and the '.join(held)}. Its entries now go to the next log channel that's set, and the Log search page has them all.")
        if "channeldelete" not in cfg["events"] or channel.id in cfg["ignored_channels"] or getattr(channel, "category_id", None) in cfg["ignored_channels"]:
            return
        entry = await self._who(guild, [Actions.channel_delete], channel.id)
        if self._actor_ignored(cfg, entry):
            return
        lines = join_lines(
            f"**Name:** {channel.name}", f"**Category:** {channel.category.name}" if channel.category else None,
            f"Created {humanize((discord.utils.utcnow() - channel.created_at).total_seconds())} ago", by_lines(entry),
        )
        embed = make_embed(f"{channel_kind(channel)} deleted", description=lines, color=RED, footer=f"Channel ID: {channel.id}")
        self._emit(guild, "channeldelete", embed, channel_id=channel.id, actor=entry)

    async def _remember_readers(self, channel: Any) -> None:
        """A channel that's gone can't be asked who could read it, and Log search still holds what was said there. So the roles that could read it are kept with the entries."""
        now = time.time()
        rows = []
        for role in channel.guild.roles:
            allowed = access.permissions_in(role, channel)
            if allowed.view_channel and allowed.read_message_history:
                rows.append((channel.guild.id, channel.id, role.id, now))
        if rows:
            await self.message_db.many("INSERT OR REPLACE INTO deleted_channel_readers (guild_id, channel_id, role_id, deleted_at) VALUES (?, ?, ?, ?)", rows)

    # ---- threads ----

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        guild = thread.guild
        cfg = self.cfg(guild.id)
        if "threadcreate" not in cfg["events"] or self._place_ignored(cfg, guild, thread.parent_id) or thread.owner_id in cfg["ignored_members"]:
            return
        lines = join_lines(f"**Name:** {thread.name}", f"**Channel:** <#{thread.parent_id}>", f"**Created by:** <@{thread.owner_id}>")
        owner = guild.get_member(thread.owner_id)
        embed = make_embed("Thread created", description=lines, color=GREEN, url=thread.jump_url, author=author_block(owner), footer=f"Thread ID: {thread.id}")
        self._store_and_post_thread(guild, "threadcreate", embed, thread.id, search_parent(getattr(thread, "type", None), thread.parent_id), user_id=thread.owner_id)

    def _store_and_post_thread(self, guild: Any, event: str, embed: dict, thread_id: int, parent_id: int | None, *, user_id: int | None = None, actor: Any = None) -> None:
        """A thread that's gone or archived isn't in the cache, so its parent comes from the event."""
        self._store_entry(guild.id, event, embed, channel_id=thread_id, parent_id=parent_id, user_id=user_id, actor_id=getattr(actor, "user_id", None))
        self.bot.logs.post(self._channel_for(guild, event), embed)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        guild = after.guild
        cfg = self.cfg(guild.id)
        if "threadupdate" not in cfg["events"] or self._place_ignored(cfg, guild, after.parent_id):
            return
        old, new = self._thread_facts(before), self._thread_facts(after)
        changes = changed_fields(old, new)
        if not changes:
            return
        entry = await self._who(guild, [Actions.thread_update], after.id)
        if self._actor_ignored(cfg, entry):
            return
        # Discord archives quiet threads by itself. That writes no audit entry, and on a busy forum it would bury everything else.
        only_activity = all(old[key] == new[key] for key in new if key != "Archived")
        if only_activity and entry is None:
            return
        embed = make_embed("Thread updated", description=join_lines(f"**Thread:** {after.mention}", by_lines(entry)), fields=changes, footer=f"Thread ID: {after.id}")
        self._store_and_post_thread(guild, "threadupdate", embed, after.id, search_parent(getattr(after, "type", None), after.parent_id), actor=entry)

    def _thread_facts(self, thread: Any) -> dict[str, str]:
        return {
            "Name": thread.name,
            "Archived": str(thread.archived),
            "Locked": str(thread.locked),
            "Slowmode": humanize(thread.slowmode_delay) if thread.slowmode_delay else "Off",
            "Auto archive": humanize(thread.auto_archive_duration * 60),
            "Tags": ", ".join(tag.name for tag in thread.applied_tags) or "None",
        }

    @commands.Cog.listener()
    async def on_raw_thread_update(self, payload: discord.RawThreadUpdateEvent) -> None:
        """An archived thread isn't in the cache, so reopening one only shows up here. Posting in it reopens it too, so only a named person counts."""
        if payload.thread is not None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        cfg = self.cfg(guild.id)
        if "threadupdate" not in cfg["events"] or self._place_ignored(cfg, guild, payload.parent_id):
            return
        entry = await self._who(guild, [Actions.thread_update], payload.thread_id)
        if entry is None or self._actor_ignored(cfg, entry):
            return
        lines = join_lines(f"**Thread:** <#{payload.thread_id}>", f"**Name:** {payload.data.get('name', 'unknown')}", by_lines(entry))
        embed = make_embed("Thread updated", description=lines, footer=f"Thread ID: {payload.thread_id}")
        self._store_and_post_thread(guild, "threadupdate", embed, payload.thread_id, search_parent(getattr(payload, "thread_type", None), payload.parent_id), actor=entry)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        cfg = self.cfg(guild.id)
        if "threaddelete" not in cfg["events"] or self._place_ignored(cfg, guild, payload.parent_id):
            return
        entry = await self._who(guild, [Actions.thread_delete], payload.thread_id)
        if self._actor_ignored(cfg, entry):
            return
        name = payload.thread.name if payload.thread is not None else None
        lines = join_lines(f"**Name:** {name}" if name else None, f"**Channel:** <#{payload.parent_id}>", by_lines(entry))
        embed = make_embed("Thread deleted", description=lines, color=RED, footer=f"Thread ID: {payload.thread_id}")
        self._store_and_post_thread(guild, "threaddelete", embed, payload.thread_id, search_parent(getattr(payload, "thread_type", None), payload.parent_id), actor=entry)

    # ---- roles ----

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        guild = role.guild
        cfg = self.cfg(guild.id)
        if "rolecreate" not in cfg["events"]:
            return
        entry = await self._who(guild, [Actions.role_create], role.id)
        if self._actor_ignored(cfg, entry):
            return
        lines = join_lines(*(f"**{key}:** {value}" for key, value in role_facts(role).items()), by_lines(entry))
        embed = make_embed("New role created", description=lines, color=GREEN, footer=f"Role ID: {role.id}")
        self._emit(guild, "rolecreate", embed, actor=entry)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        guild = after.guild
        cfg = self.cfg(guild.id)
        if "roleupdate" not in cfg["events"] or after.id in cfg["ignored_roles"]:
            return
        old, new = role_facts(before), role_facts(after)

        def icon(role: Any) -> str:
            shown = role.display_icon
            return getattr(shown, "url", shown) or "None"

        fields = changed_fields({**old, "Icon": icon(before)}, {**new, "Icon": icon(after)})
        gained = sorted(name for name, on in after.permissions if on and not getattr(before.permissions, name))
        lost = sorted(name for name, on in before.permissions if on and not getattr(after.permissions, name))
        if gained:
            fields.append(make_field("Permissions added", ", ".join(permission_name(name) for name in gained)))
        if lost:
            fields.append(make_field("Permissions removed", ", ".join(permission_name(name) for name in lost)))
        if not fields:
            return  # moving a role renumbers its neighbors, which says nothing about them
        entry = await self._who(guild, [Actions.role_update], after.id)
        if self._actor_ignored(cfg, entry):
            return
        embed = make_embed(f'Role "{after.name}" updated', description=by_lines(entry) or None, fields=fields, footer=f"Role ID: {after.id}")
        self._emit(guild, "roleupdate", embed, actor=entry, search=old["Name"])

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        guild = role.guild
        cfg = self.cfg(guild.id)
        # This set stays open for a moment because members' updates can land around the delete, and it's what stops them logging one by one.
        holders = {member.id for member in role.members}
        self._deleted_roles[role.id] = holders
        logged = "roledelete" in cfg["events"] and role.id not in cfg["ignored_roles"]
        entry = None
        try:
            if logged:
                entry = await self._who(guild, [Actions.role_delete], role.id)
            await asyncio.sleep(ROLE_DELETE_HOLD)
        finally:
            self._deleted_roles.pop(role.id, None)
        if not logged or self._actor_ignored(cfg, entry):
            return
        created = humanize((discord.utils.utcnow() - role.created_at).total_seconds())
        lines = join_lines(*(f"**{key}:** {value}" for key, value in role_facts(role).items()), f"**Position:** {role.position}", f"Created {created} ago", by_lines(entry))
        people = []
        for holder in sorted(holders):
            member = guild.get_member(holder)
            people.append((who_text(member, holder), f"{holder} {member.name}" if member else str(holder)))
        lines = join_lines(lines, f"**Members who had it:** {len(people)}")
        detail = None
        if people:
            listed = ", ".join(mention for mention, _ in people)
            if len(lines) + len(listed) + 1 > DESCRIPTION_MAX:
                detail = "\n".join([f'Members who had the role "{role.name}" ({role.id}):', *(plain for _, plain in people)])
            else:
                lines = f"{lines}\n{listed}"
        embed = make_embed(f'Role "{role.name}" removed', description=lines, color=RED, footer=f"Role ID: {role.id}")
        self._emit(guild, "roledelete", embed, actor=entry, detail=detail, detail_name="role-members.txt", search=" ".join(plain for _, plain in people[:2000]))

    # ---- the server ----

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        cfg = self.cfg(after.id)
        if "server" not in cfg["events"]:
            return
        fields = changed_fields(guild_facts(before), guild_facts(after))
        if not fields:
            return
        entry = await self._who(after, [Actions.guild_update], after.id)
        if self._actor_ignored(cfg, entry):
            return
        embed = make_embed("Server updated", description=by_lines(entry) or None, fields=fields, footer=f"Server ID: {after.id}")
        self._emit(after, "server", embed, actor=entry)

    async def _expression_changes(self, guild: Any, event: str, noun: str, before: Iterable[Any], after: Iterable[Any], actions: tuple[Any, Any, Any], *, picture: bool = True) -> None:
        cfg = self.cfg(guild.id)
        if event not in cfg["events"]:
            return
        old, new = {item.id: item for item in before}, {item.id: item for item in after}
        changes = [("added", GREEN, new[key], actions[0], f"**Name:** {new[key].name}") for key in new.keys() - old.keys()]
        changes += [("removed", RED, old[key], actions[2], f"**Name:** {old[key].name}") for key in old.keys() - new.keys()]
        for key in new.keys() & old.keys():
            if old[key].name != new[key].name:
                changes.append(("renamed", BLUE, new[key], actions[1], f"**Before:** {old[key].name}\n**+After:** {new[key].name}"))
        for verb, color, item, action, body in changes:
            entry = await self._who(guild, [action], item.id)
            if self._actor_ignored(cfg, entry):
                continue
            thumbnail = item.url if picture and verb != "removed" else None
            embed = make_embed(f"{noun} {verb}", description=join_lines(body, by_lines(entry)), color=color, thumbnail=thumbnail, footer=f"{noun} ID: {item.id}")
            self._emit(guild, event, embed, actor=entry)

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before: Any, after: Any) -> None:
        await self._expression_changes(guild, "emoji", "Emoji", before, after, (Actions.emoji_create, Actions.emoji_update, Actions.emoji_delete))

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild, before: Any, after: Any) -> None:
        await self._expression_changes(guild, "stickers", "Sticker", before, after, (Actions.sticker_create, Actions.sticker_update, Actions.sticker_delete))

    @commands.Cog.listener()
    async def on_soundboard_sound_create(self, sound: Any) -> None:
        await self._expression_changes(sound.guild, "soundboard", "Sound", [], [sound], SOUND_ACTIONS, picture=False)

    @commands.Cog.listener()
    async def on_soundboard_sound_update(self, before: Any, after: Any) -> None:
        await self._expression_changes(after.guild, "soundboard", "Sound", [before], [after], SOUND_ACTIONS, picture=False)

    @commands.Cog.listener()
    async def on_soundboard_sound_delete(self, sound: Any) -> None:
        await self._expression_changes(sound.guild, "soundboard", "Sound", [sound], [], SOUND_ACTIONS, picture=False)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        guild = self._invite_guild(invite)
        if guild is None:
            return
        cfg = self.cfg(guild.id)
        channel_id = getattr(invite.channel, "id", None)
        inviter = invite.inviter
        if "invitecreate" not in cfg["events"] or self._place_ignored(cfg, guild, channel_id) or (inviter is not None and inviter.id in cfg["ignored_members"]):
            return
        lines = join_lines(
            f"**Link:** {invite.url}", f"**Channel:** <#{channel_id}>" if channel_id else None,
            f"**Created by:** {who_text(inviter)}" if inviter is not None else None,
            f"**Expires:** {'in ' + humanize(invite.max_age) if invite.max_age else 'never'}",
            f"**Uses:** {invite.max_uses or 'no limit'}", "**Temporary membership:** yes" if invite.temporary else None,
        )
        embed = make_embed("Invite created", description=lines, color=GREEN, author=author_block(inviter), footer=f"Invite code: {invite.code}")
        self._emit(guild, "invitecreate", embed, channel_id=channel_id, user_id=inviter.id if inviter is not None else None)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        guild = self._invite_guild(invite)
        if guild is None:
            return
        cfg = self.cfg(guild.id)
        channel_id = getattr(invite.channel, "id", None)
        if "invitedelete" not in cfg["events"] or self._place_ignored(cfg, guild, channel_id):
            return
        entry = await self._who(guild, [Actions.invite_delete], invite.code)
        if self._actor_ignored(cfg, entry):
            return
        lines = join_lines(f"**Code:** {invite.code}", f"**Channel:** <#{channel_id}>" if channel_id else None, by_lines(entry))
        embed = make_embed("Invite deleted", description=lines, color=RED, footer=f"Invite code: {invite.code}")
        self._emit(guild, "invitedelete", embed, channel_id=channel_id, actor=entry)

    def _invite_guild(self, invite: Any) -> Any:
        guild_id = getattr(invite.guild, "id", None)
        return self.bot.get_guild(guild_id) if guild_id else None

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: Any) -> None:
        """This event doesn't say what changed, so the audit log entry makes the entry. Without access to the log, this is all there is to say."""
        guild = channel.guild
        cfg = self.cfg(guild.id)
        if self._reads_audit(guild) or "webhooks" not in cfg["events"] or self._place_ignored(cfg, guild, channel.id):
            return
        self._emit(guild, "webhooks", make_embed(f"Webhooks changed in #{channel.name}", footer=f"Channel ID: {channel.id}"), channel_id=channel.id)

    def _event_facts(self, event: Any) -> dict[str, str]:
        place = f"<#{event.channel_id}>" if event.channel_id else (event.location or "None")
        return {
            "Name": event.name,
            "Description": event.description or "None",
            "Starts": f"<t:{int(event.start_time.timestamp())}:f>",
            "Ends": f"<t:{int(event.end_time.timestamp())}:f>" if event.end_time else "None",
            "Where": place,
            "Status": str(event.status).rsplit(".", 1)[-1],
        }

    async def _log_scheduled(self, event: Any, verb: str, color: int, action: Any, fields: list[dict] | None = None) -> None:
        guild = event.guild
        cfg = self.cfg(guild.id)
        if "scheduledevents" not in cfg["events"] or self._place_ignored(cfg, guild, event.channel_id):
            return
        entry = await self._who(guild, [action], event.id)
        if self._actor_ignored(cfg, entry):
            return
        lines = join_lines(*(f"**{key}:** {value}" for key, value in self._event_facts(event).items()), by_lines(entry)) if fields is None else (by_lines(entry) or None)
        embed = make_embed(f"Scheduled event {verb}", description=lines, color=color, url=event.url, fields=fields or (), footer=f"Event ID: {event.id}")
        self._emit(guild, "scheduledevents", embed, channel_id=event.channel_id, actor=entry, search=event.name)

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        await self._log_scheduled(event, "created", GREEN, Actions.scheduled_event_create)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent) -> None:
        fields = changed_fields(self._event_facts(before), self._event_facts(after))
        if fields:
            await self._log_scheduled(after, "updated", BLUE, Actions.scheduled_event_update, fields)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        await self._log_scheduled(event, "deleted", RED, Actions.scheduled_event_delete)

    async def _log_stage(self, instance: Any, title: str, color: int, action: Any, lines: str) -> None:
        guild = instance.guild
        cfg = self.cfg(guild.id)
        if "stage" not in cfg["events"] or self._place_ignored(cfg, guild, instance.channel_id):
            return
        entry = await self._who(guild, [action], instance.id)
        if self._actor_ignored(cfg, entry):
            return
        embed = make_embed(title, description=join_lines(f"**Channel:** <#{instance.channel_id}>", lines, by_lines(entry)), color=color, footer=f"Channel ID: {instance.channel_id}")
        self._emit(guild, "stage", embed, channel_id=instance.channel_id, actor=entry)

    @commands.Cog.listener()
    async def on_stage_instance_create(self, instance: discord.StageInstance) -> None:
        await self._log_stage(instance, "Stage started", GREEN, Actions.stage_instance_create, f"**Topic:** {instance.topic}")

    @commands.Cog.listener()
    async def on_stage_instance_update(self, before: discord.StageInstance, after: discord.StageInstance) -> None:
        if before.topic != after.topic:
            await self._log_stage(after, "Stage topic changed", BLUE, Actions.stage_instance_update, f"**Before:** {before.topic}\n**+After:** {after.topic}")

    @commands.Cog.listener()
    async def on_stage_instance_delete(self, instance: discord.StageInstance) -> None:
        await self._log_stage(instance, "Stage ended", RED, Actions.stage_instance_delete, f"**Topic:** {instance.topic}")

    async def _log_automod_rule(self, rule: Any, verb: str, color: int, action: Any) -> None:
        guild = rule.guild
        self._automod_rules[rule.id] = rule.name
        cfg = self.cfg(guild.id)
        if "automodrule" not in cfg["events"]:
            return
        entry = await self._who(guild, [action], rule.id)
        if self._actor_ignored(cfg, entry):
            return
        trigger = str(rule.trigger.type).rsplit(".", 1)[-1].replace("_", " ")
        lines = join_lines(f"**Name:** {rule.name}", f"**Turned on:** {rule.enabled}", f"**Watches for:** {trigger}", by_lines(entry))
        embed = make_embed(f"Automod rule {verb}", description=lines, color=color, footer=f"Rule ID: {rule.id}")
        self._emit(guild, "automodrule", embed, actor=entry)

    @commands.Cog.listener()
    async def on_automod_rule_create(self, rule: discord.AutoModRule) -> None:
        await self._log_automod_rule(rule, "created", GREEN, Actions.automod_rule_create)

    @commands.Cog.listener()
    async def on_automod_rule_update(self, rule: discord.AutoModRule) -> None:
        await self._log_automod_rule(rule, "updated", BLUE, Actions.automod_rule_update)

    @commands.Cog.listener()
    async def on_automod_rule_delete(self, rule: discord.AutoModRule) -> None:
        await self._log_automod_rule(rule, "deleted", RED, Actions.automod_rule_delete)

    @commands.Cog.listener()
    async def on_automod_action(self, execution: discord.AutoModAction) -> None:
        guild = self.bot.get_guild(execution.guild_id)
        if guild is None:
            return
        cfg = self.cfg(guild.id)
        if "automodaction" not in cfg["events"] or execution.user_id in cfg["ignored_members"] or self._place_ignored(cfg, guild, execution.channel_id):
            return
        key = (execution.guild_id, execution.rule_id, execution.user_id, execution.content, execution.matched_content)
        waiting = self._automod_waiting.get(key)
        if waiting is not None:
            waiting["actions"].append(execution.action)
            return
        self._automod_waiting[key] = {"actions": [execution.action]}
        try:
            await asyncio.sleep(AUTOMOD_GATHER)
        finally:
            gathered = self._automod_waiting.pop(key)
        await self._log_automod_action(guild, execution, gathered["actions"])

    async def _automod_rule_name(self, guild: Any, rule_id: int) -> str:
        name = self._automod_rules.get(rule_id)
        if name is None:
            try:
                name = (await guild.fetch_automod_rule(rule_id)).name
            except discord.HTTPException:
                return f"Rule {rule_id}"
            self._automod_rules[rule_id] = name
        return name

    async def _log_automod_action(self, guild: Any, execution: Any, actions: list[Any]) -> None:
        kinds = discord.AutoModRuleActionType
        words = []
        for action in actions:
            if action.type is kinds.block_message:
                words.append("blocked the message")
            elif action.type is kinds.send_alert_message:
                words.append("sent an alert")
            elif action.type is kinds.timeout:
                words.append(f"timed out for {humanize(action.duration.total_seconds())}" if action.duration else "timed out")
            else:
                words.append("blocked a member action")
        blocked = any(action.type is kinds.block_message for action in actions)
        name = self._channel_name(guild, execution.channel_id)
        member = guild.get_member(execution.user_id)
        facts = [make_field("Rule", await self._automod_rule_name(guild, execution.rule_id), inline=True)]
        if execution.matched_keyword:
            facts.append(make_field("Matched", execution.matched_keyword, inline=True))
        facts.append(make_field("Actions", ", ".join(words), inline=True))
        embed = make_embed(
            f"Automod {'blocked a message' if blocked else 'flagged a message'} in #{name}", description=quoted(execution.content or "") or None, color=ORANGE, author=author_block(member), fields=facts, footer=f"ID: {execution.user_id}",
        )
        self._emit(guild, "automodaction", embed, channel_id=execution.channel_id, user_id=execution.user_id, message_id=execution.message_id)

    # ---- voice ----

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        guild = member.guild
        cfg = self.cfg(guild.id)
        if member.id in cfg["ignored_members"]:
            return
        name = discord.utils.escape_markdown(member.name)
        old, new = before.channel, after.channel
        place = new if new is not None else old
        footer = f"ID: {member.id}"
        if old != new and not self._place_ignored(cfg, guild, place.id if place else None):
            if old is None and "voicejoin" in cfg["events"]:
                embed = make_embed("Member joined voice channel", description=f"**{name}** joined #{new.name}", color=GREEN, author=author_block(member), footer=footer)
                self._emit(guild, "voicejoin", embed, channel_id=new.id, user_id=member.id)
            elif new is None and "voiceleave" in cfg["events"]:
                embed = make_embed("Member left voice channel", description=f"**{name}** left #{old.name}", color=RED, author=author_block(member), footer=footer)
                self._emit(guild, "voiceleave", embed, channel_id=old.id, user_id=member.id)
            elif old is not None and new is not None and "voicemove" in cfg["events"]:
                embed = make_embed("Member changed voice channel", description=f"**Before:** #{old.name}\n**+After:** #{new.name}", author=author_block(member), footer=footer)
                self._emit(guild, "voicemove", embed, channel_id=new.id, user_id=member.id)
        if "voicemod" not in cfg["events"] or (before.mute == after.mute and before.deaf == after.deaf) or self._place_ignored(cfg, guild, place.id if place else None):
            return
        changes = []
        if before.mute != after.mute:
            changes.append("server muted" if after.mute else "server unmuted")
        if before.deaf != after.deaf:
            changes.append("server deafened" if after.deaf else "server undeafened")

        def same_change(e: Any) -> bool:
            done = e.changes.after
            return (before.mute != after.mute and getattr(done, "mute", None) == after.mute) or (before.deaf != after.deaf and getattr(done, "deaf", None) == after.deaf)

        entry = await self._who(guild, [Actions.member_update], member.id, where=same_change, aspect="voice")
        if self._actor_ignored(cfg, entry):
            return
        lines = join_lines(f"{member.mention} {' and '.join(changes)}", by_lines(entry))
        embed = make_embed("Moderator voice action", description=lines, color=ORANGE, author=author_block(member), footer=footer)
        self._emit(guild, "voicemod", embed, channel_id=place.id if place else None, user_id=member.id, actor=entry)

    # ---- things only the audit log announces ----

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        route = self._audit_routes.get(entry.action)
        if route is not None:
            await route(entry)

    def _audit_blocked(self, entry: Any, event: str) -> bool:
        cfg = self.cfg(entry.guild.id)
        return event not in cfg["events"] or entry.user_id in cfg["ignored_members"]

    async def _known_user(self, target: Any) -> Any:
        """A target that left is only an ID in the event, so look the person up. A failed lookup still leaves the ID."""
        if getattr(target, "name", None) is not None or getattr(target, "id", None) is None:
            return target
        try:
            return await self.bot.fetch_user(target.id)
        except discord.HTTPException:
            return target

    async def _audit_kick(self, entry: Any) -> None:
        if self._audit_blocked(entry, "kick") or getattr(entry.target, "id", None) in self.cfg(entry.guild.id)["ignored_members"]:
            return
        user = await self._known_user(entry.target)
        target_id = getattr(user, "id", None)
        embed = make_embed("Member kicked", description=join_lines(who_text(user, target_id), by_lines(entry)), color=RED, author=author_block(user), footer=f"ID: {target_id}")
        self._emit(entry.guild, "kick", embed, user_id=target_id, actor=entry)

    async def _audit_prune(self, entry: Any) -> None:
        if self._audit_blocked(entry, "kick"):
            return
        removed = getattr(entry.extra, "members_removed", 0)
        days = getattr(entry.extra, "delete_member_days", 0)
        lines = join_lines(f"**Removed:** {removed}", f"**Inactive for:** {days} days", by_lines(entry))
        self._emit(entry.guild, "kick", make_embed("Members pruned", description=lines, color=RED), actor=entry)

    async def _audit_bot_add(self, entry: Any) -> None:
        if self._audit_blocked(entry, "integrations"):
            return
        user = await self._known_user(entry.target)
        target_id = getattr(user, "id", None)
        embed = make_embed("Bot added", description=join_lines(who_text(user, target_id), by_lines(entry)), color=GREEN, author=author_block(user), footer=f"ID: {target_id}")
        self._emit(entry.guild, "integrations", embed, user_id=target_id, actor=entry)

    async def _audit_integration(self, entry: Any) -> None:
        if self._audit_blocked(entry, "integrations"):
            return
        verb, color = {
            Actions.integration_create: ("added", GREEN), Actions.integration_update: ("updated", BLUE), Actions.integration_delete: ("removed", RED),
        }[entry.action]
        name = getattr(entry.changes.after, "name", None) or getattr(entry.changes.before, "name", None) or getattr(entry.target, "name", None)
        kind = getattr(entry.changes.after, "type", None) or getattr(entry.changes.before, "type", None)
        lines = join_lines(f"**Name:** {name}" if name else None, f"**Type:** {kind}" if kind else None, by_lines(entry))
        self._emit(entry.guild, "integrations", make_embed(f"Integration {verb}", description=lines, color=color), actor=entry)

    async def _audit_command_permissions(self, entry: Any) -> None:
        if self._audit_blocked(entry, "integrations"):
            return
        name = getattr(entry.target, "name", None)
        lines = join_lines(f"**Command:** {name}" if name else None, by_lines(entry))
        self._emit(entry.guild, "integrations", make_embed("Command permissions changed", description=lines), actor=entry)

    async def _audit_webhook(self, entry: Any) -> None:
        # The bot's own webhooks are how these entries get posted, and logging them would only be noise.
        if self._audit_blocked(entry, "webhooks") or entry.user_id == getattr(self.bot.user, "id", None):
            return
        verb, color = {Actions.webhook_create: ("created", GREEN), Actions.webhook_update: ("updated", BLUE), Actions.webhook_delete: ("deleted", RED)}[entry.action]
        changes = entry.changes
        name = getattr(changes.after, "name", None) or getattr(changes.before, "name", None)
        channel = getattr(changes.after, "channel", None) or getattr(changes.before, "channel", None)
        channel_id = getattr(channel, "id", None)
        cfg = self.cfg(entry.guild.id)
        if self._place_ignored(cfg, entry.guild, channel_id):
            return
        lines = join_lines(f"**Name:** {name}" if name else None, f"**Channel:** <#{channel_id}>" if channel_id else None, by_lines(entry))
        embed = make_embed(f"Webhook {verb}", description=lines, color=color, footer=f"Webhook ID: {getattr(entry.target, 'id', 'unknown')}")
        self._emit(entry.guild, "webhooks", embed, channel_id=channel_id, actor=entry)

    async def _audit_pin(self, entry: Any) -> None:
        if self._audit_blocked(entry, "pins"):
            return
        guild = entry.guild
        channel_id = getattr(getattr(entry.extra, "channel", None), "id", None)
        message_id = getattr(entry.extra, "message_id", None)
        if self._place_ignored(self.cfg(guild.id), guild, channel_id):
            return
        stored = await self.stored_message(message_id) if message_id else None
        pinned = entry.action is Actions.message_pin
        url = f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}" if channel_id and message_id else None
        author_id = stored["author_id"] if stored else getattr(entry.target, "id", None)
        embed = make_embed(
            f"Message {'pinned' if pinned else 'unpinned'} in #{self._channel_name(guild, channel_id)}", description=quoted(stored["content"]) if stored and stored["content"] else None, url=url,
            author=self._author_of(guild, stored) if stored else None, fields=[make_field("Message ID", str(message_id), inline=True), *by_fields(entry)], footer=f"ID: {author_id}" if author_id else None,
        )
        parent_id = await self._stored_parent(guild, channel_id)
        self._emit(guild, "pins", embed, channel_id=channel_id, user_id=author_id, actor=entry, message_id=message_id, parent_id=parent_id)

    async def _audit_voice(self, entry: Any) -> None:
        if self._audit_blocked(entry, "voicemod"):
            return
        guild = entry.guild
        count = getattr(entry.extra, "count", 1)
        channel = getattr(entry.extra, "channel", None)
        channel_id = getattr(channel, "id", None)
        if self._place_ignored(self.cfg(guild.id), guild, channel_id):
            return
        noun = "a member" if count == 1 else f"{count} members"
        if entry.action is Actions.member_move:
            text_line = f"{noun.capitalize()} moved to <#{channel_id}>" if channel_id else f"{noun.capitalize()} moved"
        else:
            text_line = f"{noun.capitalize()} disconnected from voice"
        embed = make_embed("Moderator voice action", description=join_lines(text_line, by_lines(entry)), color=ORANGE)
        self._emit(guild, "voicemod", embed, channel_id=channel_id, actor=entry)

    async def _audit_onboarding(self, entry: Any) -> None:
        if self._audit_blocked(entry, "server"):
            return
        title = "Server onboarding changed" if entry.action.name.startswith("onboarding") else "Server guide changed"
        self._emit(entry.guild, "server", make_embed(title, description=by_lines(entry) or None), actor=entry)

    # ---- commands ----

    @commands.hybrid_group(name="log", fallback="show", description="Show how logging is set up in this server")
    @commands.guild_only()
    @requires("manage_guild")
    @app_commands.default_permissions(manage_guild=True)
    async def log_root(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)

        def place(field: str) -> str:
            return f"<#{cfg[field]}>" if cfg[field] else "not set"

        lines = [
            f"Default log channel: {place('default_channel')}",
            f"Message log channel: {place('message_channel')}",
            f"Member log channel: {place('member_channel')}",
            f"Join and leave log channel: {place('join_channel')}",
            f"Server log channel: {place('server_channel')}",
            f"Voice log channel: {place('voice_channel')}",
            f"Moderator delete log channel: {place('moderator_delete_channel')}",
            f"Role log channel: {place('role_channel')}",
            f"Name log channel: {place('name_channel')}",
            f"Timeout log channel: {place('timeout_channel')}",
            f"Events on: {len(cfg['events'])} of {len(EVENT_KEYS)}. See them with `/log config`.",
        ]
        if self.bot.public_url:
            lines.append(f"Full settings: {self.bot.public_url}/dashboard/{ctx.guild.id}/logging")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    async def _save(self, ctx: commands.Context, action: str, form: dict) -> dict:
        cfg = self.cfg(ctx.guild.id)
        copying = self.reading_refusal(ctx.author, cfg, {**cfg, **form})
        if copying is not None:
            raise ValidationError({copying[0]: copying[1]})
        return await self.bot.settings.save(ctx.guild.id, self.key, form, actor=self.bot.actor(ctx.author), action=action)

    async def _set_channel(self, ctx: commands.Context, field: str, channel: Any, label: str) -> None:
        refusal = access.post_refusal(ctx.author, channel) if channel is not None else None
        if refusal is not None:
            await ctx.fail(refusal)
            return
        try:
            await self._save(ctx, f"log.{field}", {field: channel.id if channel is not None else None})
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values())))
            return
        if channel is None:
            await ctx.ok(f"The {label} is cleared.")
            return
        allowed = channel.permissions_for(ctx.guild.me)
        problem = "" if allowed.view_channel and allowed.send_messages and allowed.embed_links else " The bot can't post there yet. It needs View Channel, Send Messages and Embed Links."
        await ctx.ok(f"The {label} is now {channel.mention}.{problem}")

    @log_root.command(name="channel", description="Set the default log channel. Leave it empty to clear it.")
    @requires("manage_guild")
    async def log_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_channel(ctx, "default_channel", channel, "default log channel")

    @log_root.command(name="message_channel", aliases=["messagechannel"], description="Set where message events are logged")
    @requires("manage_guild")
    async def log_message_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_channel(ctx, "message_channel", channel, "message log channel")

    @log_root.command(name="member_channel", aliases=["memberchannel"], description="Set where member events are logged")
    @requires("manage_guild")
    async def log_member_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_channel(ctx, "member_channel", channel, "member log channel")

    @log_root.command(name="join_channel", aliases=["joinchannel"], description="Set where joining and leaving are logged")
    @requires("manage_guild")
    async def log_join_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_channel(ctx, "join_channel", channel, "join and leave log channel")

    @log_root.command(name="server_channel", aliases=["serverchannel"], description="Set where changes to the server are logged")
    @requires("manage_guild")
    async def log_server_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_channel(ctx, "server_channel", channel, "server log channel")

    @log_root.command(name="voice_channel", aliases=["voicechannel"], description="Set where voice channel activity is logged")
    @requires("manage_guild")
    async def log_voice_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_channel(ctx, "voice_channel", channel, "voice log channel")

    @log_root.command(name="moderator_delete_channel", aliases=["moderatordeletechannel"], description="Set where moderator and bot deletes and purges are logged, apart from other deletes")
    @requires("manage_guild")
    async def log_moderator_delete_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_channel(ctx, "moderator_delete_channel", channel, FIELD_LABELS["moderator_delete_channel"])

    @log_root.command(name="role_channel", aliases=["rolechannel"], description="Set where members' role changes are logged, apart from other member events")
    @requires("manage_guild")
    async def log_role_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_channel(ctx, "role_channel", channel, FIELD_LABELS["role_channel"])

    @log_root.command(name="name_channel", aliases=["namechannel"], description="Set where name changes are logged, apart from other member events")
    @requires("manage_guild")
    async def log_name_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_channel(ctx, "name_channel", channel, FIELD_LABELS["name_channel"])

    @log_root.command(name="timeout_channel", aliases=["timeoutchannel"], description="Set where timeouts are logged, apart from other member events")
    @requires("manage_guild")
    async def log_timeout_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_channel(ctx, "timeout_channel", channel, FIELD_LABELS["timeout_channel"])

    @log_root.command(name="config", description="Turn an event on or off. Leave it empty to see every event.")
    @requires("manage_guild")
    @app_commands.describe(event="The event to turn on or off")
    async def log_config(self, ctx: commands.Context, event: str | None = None) -> None:
        cfg = self.cfg(ctx.guild.id)
        if event is None:
            embed = discord.Embed(title="Logged events", color=BLUE)
            for _, title, _, rows in EVENT_GROUPS:
                lines = "\n".join(f"{OVERWRITE_MARKS[key in cfg['events']]} `{key}` {label}" for key, label, _ in rows)
                embed.add_field(name=title, value=lines[:FIELD_VALUE_MAX], inline=False)
            await ctx.send(embed=embed, ephemeral=True)
            return
        name = event.strip().lower()
        current = set(cfg["events"])
        if name == "everything":
            wanted, said = set(EVENT_KEYS), "Every event is now on."
        elif name == "nothing":
            wanted, said = set(), "Every event is now off."
        elif name == "default":
            wanted, said = set(DEFAULT_EVENTS), "The events are back to the default set."
        else:
            keys = EVENT_SETS.get(name) or ((name,) if name in EVENT_KINDS else None)
            if keys is None:
                await ctx.fail(f"`{clip(name, 50)}` isn't an event. Use `/log config` to see them.")
                return
            turn_on = not all(key in current for key in keys)
            wanted = current | set(keys) if turn_on else current - set(keys)
            said = f"`{name}` is now {'on' if turn_on else 'off'}."
        await self._save(ctx, "log.config", {"events": [key for key in EVENT_KEYS if key in wanted]})
        await ctx.ok(said)

    @log_config.autocomplete("event")
    async def log_config_event(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        needle = current.strip().lower()
        names = [*EVENT_KEYS, *EVENT_SETS, "everything", "nothing", "default"]
        return [app_commands.Choice(name=name, value=name) for name in names if needle in name][:25]

    async def _change_ignored(self, ctx: commands.Context, targets: str, *, add: bool) -> None:
        channels, members, roles, unknown = parse_targets(ctx.guild, targets)
        if not channels and not members and not roles:
            await ctx.fail("Name at least one channel, member or role, by mention, ID or name.")
            return
        cfg = self.cfg(ctx.guild.id)
        for channel_id in channels:
            channel = ctx.guild.get_channel_or_thread(channel_id)
            refusal = access.view_refusal(ctx.author, channel) if channel is not None else None
            if refusal is not None:
                await ctx.fail(refusal)
                return

        def changed(field: str, values: list[int]) -> list[int]:
            current = cfg[field]
            return [*current, *(v for v in values if v not in current)] if add else [v for v in current if v not in values]

        try:
            await self._save(ctx, "log.ignore" if add else "log.unignore", {
                "ignored_channels": changed("ignored_channels", channels),
                "ignored_members": changed("ignored_members", members),
                "ignored_roles": changed("ignored_roles", roles),
            })
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values())))
            return
        names = [f"<#{c}>" for c in channels] + [f"<@{m}>" for m in members] + [f"<@&{r}>" for r in roles]
        said = f"{'Ignoring' if add else 'No longer ignoring'} {', '.join(names)}."
        if unknown:
            said += f" Couldn't find: {', '.join(unknown)}."
        await ctx.ok(clip(said, 1900))

    @log_root.command(name="ignore", description="Stop logging events in these channels or about these members and roles")
    @requires("manage_guild")
    @app_commands.describe(targets="Channels, categories, members or roles, by mention, ID or name")
    async def log_ignore(self, ctx: commands.Context, *, targets: str) -> None:
        await self._change_ignored(ctx, targets, add=True)

    @log_root.command(name="unignore", description="Go back to logging these channels, members or roles")
    @requires("manage_guild")
    @app_commands.describe(targets="Channels, categories, members or roles, by mention, ID or name")
    async def log_unignore(self, ctx: commands.Context, *, targets: str) -> None:
        await self._change_ignored(ctx, targets, add=False)

    @log_root.command(name="prefix", aliases=["ip"], description="Ignore deletes, edits and purged messages that start with this text")
    @requires("manage_guild")
    async def log_prefix(self, ctx: commands.Context, prefix: str) -> None:
        current = self.cfg(ctx.guild.id)["ignored_prefixes"]
        if prefix in current:
            await ctx.fail("That prefix is already ignored.")
            return
        try:
            await self._save(ctx, "log.prefix", {"ignored_prefixes": [*current, prefix]})
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values())))
            return
        await ctx.ok(f"Messages starting with `{prefix}` are ignored in deletes, edits and purges.")

    @log_root.command(name="remove_prefix", aliases=["removeprefix", "up"], description="Stop ignoring messages that start with this text")
    @requires("manage_guild")
    async def log_remove_prefix(self, ctx: commands.Context, prefix: str) -> None:
        current = self.cfg(ctx.guild.id)["ignored_prefixes"]
        if prefix not in current:
            await ctx.fail("That prefix isn't ignored.")
            return
        await self._save(ctx, "log.remove_prefix", {"ignored_prefixes": [p for p in current if p != prefix]})
        await ctx.ok(f"Messages starting with `{prefix}` are logged again.")

    @log_root.command(name="export", description="Get a code holding this server's logging settings")
    @requires("manage_guild")
    async def log_export(self, ctx: commands.Context) -> None:
        code = export_code(self.cfg(ctx.guild.id))
        if len(code) > 1900:
            await ctx.send("Here are the settings. Paste this file's text into `/log import`.", file=discord.File(io.BytesIO(code.encode()), filename="logging-settings.txt"), ephemeral=True)
            return
        await ctx.send(f"```\n{code}\n```", ephemeral=True)

    @log_root.command(name="import", aliases=["custom"], description="Load logging settings from a code. Channels from other servers are dropped.")
    @requires("manage_guild")
    async def log_import(self, ctx: commands.Context, *, code: str) -> None:
        try:
            data = read_import(code)
            form = settings_from_import(data, self.schema, {c.id for c in ctx.guild.channels}, {r.id for r in ctx.guild.roles})
            refusal = self._import_refusal(ctx, form)
            if refusal is not None:
                await ctx.fail(refusal)
                return
            await self._save(ctx, "log.import", form)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        except ValidationError as error:
            await ctx.fail(f"Those settings have a problem. {next(iter(error.fields.values()))}")
            return
        await ctx.ok("Logging settings are loaded.")

    def _import_refusal(self, ctx: commands.Context, form: dict) -> str | None:
        """What the dashboard refuses for a pasted code, for the command. A channel already set stays open, so a manager can load the rest of a code."""
        cfg = self.cfg(ctx.guild.id)
        for field in FIELD_LABELS:
            channel = ctx.guild.get_channel_or_thread(form[field]) if form.get(field) and form[field] != cfg[field] else None
            refusal = access.post_refusal(ctx.author, channel) if channel is not None else None
            if refusal is not None:
                return refusal
        for channel_id in form.get("ignored_channels") or []:
            channel = ctx.guild.get_channel_or_thread(channel_id) if channel_id not in cfg["ignored_channels"] else None
            refusal = access.view_refusal(ctx.author, channel) if channel is not None else None
            if refusal is not None:
                return refusal
        return None

    async def _build_log_channels(self, guild_id: int, actor: Any) -> dict:
        commands_settings = self.bot.settings.get(guild_id, COMMANDS_MODULE)
        core = self.bot.settings.get(guild_id, "core")
        viewers = [*core["dashboard_staff_roles"], *([commands_settings["mod_role"]] if commands_settings["mod_role"] else [])]
        made = await self.bot.ops.create_private_category(guild_id, "Logs", list(AIO_NAMES.values()), viewer_roles=viewers)
        form = {field: made["channels"][name] for field, name in AIO_NAMES.items()}
        await self.bot.settings.save(guild_id, self.key, form, actor=actor, action="log.aio")
        return form

    @log_root.command(name="aio", description="Create a category with five log channels and split logging into them")
    @requires("manage_guild")
    async def log_aio(self, ctx: commands.Context) -> None:
        await ctx.defer()
        if not access.is_admin(ctx.author) and not ctx.author.guild_permissions.manage_channels:
            await ctx.fail("You need Manage Channels to make channels.")
            return
        cfg = self.cfg(ctx.guild.id)
        if not self._reads_what_it_covers(ctx.author, cfg):
            await ctx.fail(access.READS_ALL)
            return
        if all(cfg[field] and ctx.guild.get_channel(cfg[field]) is not None for field in AIO_NAMES):
            await ctx.fail("All five log channels are already set. Clear one first if you want new ones.")
            return
        try:
            form = await self._build_log_channels(ctx.guild.id, self.bot.actor(ctx.author))
        except OpsError as error:
            await ctx.fail(str(error))
            return
        await ctx.ok("Log channels are ready: " + ", ".join(f"<#{channel}>" for channel in form.values()) + ". They're hidden from everyone but the bot and the staff roles.")

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/logging/catalog", self.api_catalog, access="staff")
        router.get("/logging/bots", self.api_bots)
        router.post("/logging/aio", self.api_aio)
        router.get("/logging/export", self.api_export_code)
        router.post("/logging/import", self.api_import)
        router.get("/logging/status", self.api_status)
        router.post("/logging/test", self.api_test)
        router.get("/logs/search", self.api_search, access="staff")
        router.get("/logs/export", self.api_export, access="staff")
        router.get(r"/logs/entries/{entry_id:\d+}", self.api_entry, access="staff")

    async def api_catalog(self, req: Any) -> dict:
        return {"groups": [
            {"key": kind, "title": title, "help": help_text, "events": [{"key": key, "label": label, "new": bit is None} for key, label, bit in rows]}
            for kind, title, help_text, rows in EVENT_GROUPS
        ]}

    async def api_bots(self, req: Any) -> list[dict]:
        return req.ops.list_bots(req.guild_id)

    async def api_status(self, req: Any) -> dict:
        """What the Logging page warns about: each set channel that's gone, or where the bot can't post."""
        guild = self.bot.get_guild(req.guild_id)
        problems = []
        if guild is not None and not getattr(guild, "unavailable", False):
            cfg = self.cfg(req.guild_id)
            for field, label in FIELD_LABELS.items():
                if not cfg[field] or not req.can_see(cfg[field]):
                    continue
                channel = guild.get_channel_or_thread(cfg[field])
                if channel is None:
                    problems.append({"field": field, "text": f"The {label} no longer exists. Its entries go to the next log channel that's set."})
                    continue
                allowed = channel.permissions_for(guild.me)
                if not (allowed.view_channel and allowed.send_messages and allowed.embed_links):
                    problems.append({"field": field, "text": f"The bot can't post in #{channel.name}, the {label}. It needs View Channel, Send Messages and Embed Links there."})
        return {"problems": problems}

    async def api_test(self, req: Any) -> dict:
        """Posts one entry to each set channel, as the bot itself, and reports each one, so a wrong channel shows before the next real event."""
        cfg = self.cfg(req.guild_id)
        results = []
        for channel_id in dict.fromkeys(cfg[field] for field in FIELD_LABELS if cfg[field] and req.can_see(cfg[field])):
            payload = {"embeds": [make_embed("Test entry", description=f"{req.user.name} sent this from the Logging page. Log entries posted here look like this.", color=GREEN)]}
            try:
                await req.ops.send_message(channel_id, payload, silent=True)
                results.append({"channel_id": channel_id, "ok": True, "error": None})
            except OpsError as error:
                results.append({"channel_id": channel_id, "ok": False, "error": str(error)})
        await req.record("log.test", detail=f"{len(results)} channels")
        return {"results": results}

    async def api_aio(self, req: Any) -> dict:
        req.require_permission("manage_channels")
        info = req.ops.describe_guild(req.guild_id)
        cfg = req.settings.get(req.guild_id, self.key)
        guild = self.bot.get_guild(req.guild_id)
        member = guild.get_member(req.user.id) if guild else None
        if member is not None and not self._reads_what_it_covers(member, cfg):
            raise ApiError(403, access.READS_ALL)
        known = {row["id"] for row in info["channels"]} if info else set()
        if all(cfg[field] in known for field in AIO_NAMES):
            raise ApiError(409, "All five log channels are already set. Clear one first if you want new ones.")
        return await self._build_log_channels(req.guild_id, req.actor)

    async def api_export_code(self, req: Any) -> dict:
        return {"code": export_code(req.settings.get(req.guild_id, self.key))}

    async def api_import(self, req: Any) -> dict:
        body = await req.json()
        info = req.ops.describe_guild(req.guild_id)
        if info is None:
            raise ApiError(404, "The bot isn't in that server.")
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str):
            raise ApiError(400, "Paste a code from the export first.")
        form = settings_from_import(read_import(code), self.schema, {row["id"] for row in info["channels"]}, {row["id"] for row in info["roles"]})
        from ..web.app import _check_reach  # the page's own check on a saved form, and app.py loads the modules
        before = req.settings.get(req.guild_id, self.key)
        _check_reach(req, self.key, before, self.schema.clean(form, base=before))
        return await req.settings.save(req.guild_id, self.key, form, actor=req.actor, action="log.import")

    def _entry_row(self, req: Any, row: dict) -> dict:
        row["embed"] = json.loads(row["embed"])
        row["label"] = EVENT_LABELS.get(row["event"], row["event"])
        row["has_detail"] = bool(row["has_detail"])
        for field, name in (("user_id", "user_name"), ("actor_id", "actor_name")):
            person = req.ops.member(req.guild_id, row[field]) if row[field] else None
            row[name] = person["display_name"] if person else None
        return row

    async def _visible_channels(self, req: Any) -> set[int] | None:
        """The channels this person can read in Discord, or None for the owner and Administrators. Log search must not open a channel Discord keeps from them. A deleted channel counts when one of their roles could read it."""
        if req.is_admin:
            return None
        guild = self.bot.get_guild(req.guild_id)
        member = guild.get_member(req.user.id) if guild else None
        if member is None:
            return set()
        visible = {channel.id for channel in guild.channels if access.can_read(member, channel)}
        roles = [role.id for role in member.roles]
        marks = ",".join("?" * len(roles)) or "NULL"
        rows = await self.message_db.all(f"SELECT DISTINCT channel_id FROM deleted_channel_readers WHERE guild_id = ? AND role_id IN ({marks})", (req.guild_id, *roles))
        return visible | {row["channel_id"] for row in rows}

    async def api_search(self, req: Any) -> dict:
        filters = search_filters(req.query)
        page = integer(1, 1_000_000)(req.query.get("page", "1"))
        size = choice(*PAGE_SIZES)(integer(1, 1000)(req.query.get("size", "25")))
        await self.flush()
        where, args = search_where(req.guild_id, filters, await self._visible_channels(req))
        # A word too short for the index scans the table, and this connection is the one messages are written through, so it reads a copy of its own.
        async with self.message_db.reader() as view:
            total = await view.scalar(f"SELECT COUNT(*) FROM (SELECT 1 FROM log_entries e WHERE {where} LIMIT ?)", [*args, COUNT_CAP + 1])
            capped = total > COUNT_CAP
            total = min(total, COUNT_CAP)
            pages = max(1, math.ceil(total / size))
            rows = await view.all(
                f"SELECT {ENTRY_COLUMNS} FROM log_entries e WHERE {where} ORDER BY e.at DESC, e.id DESC LIMIT ? OFFSET ?",
                [*args, size, (min(page, pages) - 1) * size],
            )
        return {
            "rows": [self._entry_row(req, row) for row in rows], "total": total, "capped": capped, "page": min(page, pages), "pages": pages, "size": size,
            "retention_days": req.settings.get(req.guild_id, self.key)["log_retention_days"],
        }

    async def api_entry(self, req: Any) -> dict:
        await self.flush()
        row = await self.message_db.one(f"SELECT {ENTRY_COLUMNS}, e.detail FROM log_entries e WHERE e.id = ? AND e.guild_id = ?", (req.int_param("entry_id"), req.guild_id))
        if row is None:
            raise ApiError(404, "That log entry is gone.")
        visible = await self._visible_channels(req)
        if visible is not None and row["channel_id"] is not None and row["channel_id"] not in visible and row["parent_id"] not in visible:
            raise ApiError(404, "That log entry is gone.")
        return self._entry_row(req, row)

    async def _export_rows(self, guild_id: int, filters: dict, visible: set[int] | None = None) -> AsyncIterator[dict]:
        """Keyed on time rather than page number, and read from a copy of its own, so a long export can't repeat a row or hold up the store."""
        where, args = search_where(guild_id, filters, visible)
        last: tuple[float, int] | None = None
        sent = 0
        async with self.message_db.reader() as view:
            while sent < EXPORT_MAX:
                clause, extra = where, []
                if last is not None:
                    clause += " AND (e.at < ? OR (e.at = ? AND e.id < ?))"
                    extra = [last[0], last[0], last[1]]
                rows = await view.all(f"SELECT {ENTRY_COLUMNS}, e.detail FROM log_entries e WHERE {clause} ORDER BY e.at DESC, e.id DESC LIMIT ?", [*args, *extra, EXPORT_CHUNK])
                if not rows:
                    return
                for row in rows:
                    yield row
                sent += len(rows)
                last = (rows[-1]["at"], rows[-1]["id"])

    async def api_export(self, req: Any) -> web.StreamResponse:
        filters = search_filters(req.query)
        kind = req.query.get("format", "csv")
        if kind not in ("csv", "json"):
            raise ApiError(400, "Pick CSV or JSON.")
        await self.flush()
        visible = await self._visible_channels(req)
        await req.record("logs.export", detail=kind)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        # Prepared by hand because this streams, so the middleware's headers would arrive too late. A download doesn't need the rest of them.
        response = web.StreamResponse(headers={
            "Content-Type": "text/csv; charset=utf-8" if kind == "csv" else "application/json; charset=utf-8",
            "Content-Disposition": f'attachment; filename="logs-{req.guild_id}-{stamp}.{kind}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })
        await response.prepare(req.request)
        try:
            if kind == "csv":
                await self._write_csv(response, req.guild_id, filters, visible)
            else:
                await self._write_json(response, req.guild_id, filters, visible)
            await response.write_eof()
        except ConnectionResetError:
            log.debug("A log export stopped because the browser went away")  # closing the tab mid-download isn't an error
        return response

    async def _write_csv(self, response: web.StreamResponse, guild_id: int, filters: dict, visible: set[int] | None = None) -> None:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(["time_utc", "event", "channel_id", "user_id", "actor_id", "message_id", "title", "text", "full_text"])
        # Excel reads a CSV as the local code page unless the file starts with this mark, which breaks every emoji and accent.
        pending = codecs.BOM_UTF8
        # Closed on the way out, so a browser going away doesn't leave the export's connection open.
        async with contextlib.aclosing(self._export_rows(guild_id, filters, visible)) as found:
            async for row in found:
                embed = json.loads(row["embed"])
                writer.writerow([
                    iso_time(row["at"]), row["event"], row["channel_id"] or "", row["user_id"] or "", row["actor_id"] or "", row["message_id"] or "",
                    csv_safe(embed.get("title")), csv_safe(embed_text(embed, with_title=False)), csv_safe(row["detail"]),
                ])
                if buffer.tell() > 64 * 1024:
                    await response.write(pending + buffer.getvalue().encode("utf-8"))
                    pending = b""
                    buffer.seek(0)
                    buffer.truncate()
        await response.write(pending + buffer.getvalue().encode("utf-8"))

    async def _write_json(self, response: web.StreamResponse, guild_id: int, filters: dict, visible: set[int] | None = None) -> None:
        await response.write(b"[")
        first = True
        async with contextlib.aclosing(self._export_rows(guild_id, filters, visible)) as found:
            async for row in found:
                item = {
                    "id": row["id"], "time_utc": iso_time(row["at"]), "at": row["at"], "event": row["event"], "channel_id": row["channel_id"], "parent_id": row["parent_id"],
                    "user_id": row["user_id"], "actor_id": row["actor_id"], "message_id": row["message_id"], "embed": json.loads(row["embed"]), "full_text": row["detail"],
                }
                await response.write((b"" if first else b",\n") + json.dumps(safe_json(item), ensure_ascii=False).encode("utf-8"))
                first = False
        await response.write(b"]\n")

    # ---- the demo's sample rows ----

    async def seed_demo(self, guild_id: int) -> None:
        """Made-up entries, so the search page has something to show without Discord."""
        info = self.bot.ops.describe_guild(guild_id)
        if info is None:
            return
        channels = {row["name"]: row["id"] for row in info["channels"]}
        roles = {row["name"]: row["id"] for row in info["roles"]}
        general, lounge = channels.get("general"), channels.get("Lounge")
        moderator = next(member for member in self.bot.ops.members if not member["bot"])
        by = f"**By:** <@{moderator['id']}> ({moderator['name']})"
        icon = "/static/img/logo.svg"
        names = ("sample_member", "printer_fan", "filament_friend", "layer_lover", "nozzle_nerd")
        people = (guild_id + 3, 900000000000000301, 900000000000000302, 900000000000000303, 900000000000000304)  # the first is the demo's own sample member
        lines = ("I think the first layer is too fast.", "Has anyone tried the new silk filament?", "The purge tower keeps falling over.", "Nice print! What settings did you use?", "Does this need supports?")
        now = time.time()
        for round_number in range(14):
            index = round_number % len(names)
            user_id = people[index]
            who = {"name": names[index], "icon_url": icon}
            line = lines[index]
            message_id = 900000000000200000 + round_number
            at = now - round_number * 1700

            def add(event: str, title: str, *, ago: int = 0, channel_id: int | None = None, person: int | None = None, actor_id: int | None = None, detail: str | None = None, **kwargs: Any) -> None:
                self._store_entry(guild_id, event, make_embed(title, at=at - ago, **kwargs), at=at - ago, channel_id=channel_id, user_id=person, actor_id=actor_id, detail=detail)

            facts = [make_field("Message ID", str(message_id), inline=True), sent_field(at - 3600)]
            add("delete", "Message deleted in #general", channel_id=general, person=user_id, description=line, color=RED, author=who, footer=f"ID: {user_id}",
                fields=[*facts, make_field("Deleted by", "The author or a bot")])
            add("edit", "Message edited in #general", ago=300, channel_id=general, person=user_id, fields=[make_field("Before", line), make_field("After", f"{line} Thanks!"), *facts],
                url=f"https://discord.com/channels/{guild_id}/{general}/{message_id}", author=who, footer=f"ID: {user_id}")
            add("role", "Role added", ago=600, person=user_id, actor_id=moderator["id"], description=f"<@&{roles.get('Member')}>\n{by}", author=who, footer=f"ID: {user_id}")
            add("join", "Member joined", ago=900, person=user_id, description=f"<@{user_id}> {ordinal(4200 + round_number)} to join\ncreated {humanize(90000000 + round_number * 86400)} ago", color=GREEN, author=who, footer=f"ID: {user_id}")
            add("voicejoin", "Member joined voice channel", ago=1200, channel_id=lounge, person=user_id, description=f"**{names[index]}** joined #Lounge", color=GREEN, author=who, footer=f"ID: {user_id}")
            if round_number % 3 == 0:
                listing = "\n".join(f"[sample] {names[i % 5]} ({people[i % 5]}): {lines[i % 5]}" for i in range(12 + round_number))
                add("purge", "Messages purged in #general", ago=1400, channel_id=general, actor_id=moderator["id"], detail=listing, description=f"**Messages:** {12 + round_number}\n{by}", color=RED, footer=f"Channel ID: {general}")
            if round_number % 4 == 0:
                add("channelupdate", "Voice channel updated", ago=1500, channel_id=lounge, footer=f"Channel ID: {lounge}",
                    fields=[make_field("Before", f"**Name:** Members: {4200 + round_number}", inline=True), make_field("After", f"**Name:** Members: {4201 + round_number}", inline=True)])
        await self.flush()
