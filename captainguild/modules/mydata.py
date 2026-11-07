"""What the bot holds about a person, for that person: see it, download it, and delete what's theirs to delete.

Records staff keep for moderation stay out of reach here, since someone could otherwise wipe the evidence about them. Stored messages and log entries end when their
retention runs out, and notes and reports when staff delete them. Cases stay on record, and staff can void one.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

from ..core.settings import Actor
from ..core.module import Module

log = logging.getLogger(__name__)

EXPORT_MAX = 100_000
LOOKS_PER_MINUTE = 10  # each look reads several tables, and only a stuck script needs more than this


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    tables: tuple[tuple[str, str], ...]  # each table and the column holding the person's ID
    module: str = ""  # the feature that also keeps these rows in memory


SECTIONS = (
    Section("birthday", "Birthday", (("birthdays", "user_id"),), "greetings"),
    Section("highlights", "Highlight words and blocks", (("highlight_words", "user_id"), ("highlight_blocks", "user_id")), "utilities"),
    Section("reminders", "Reminders", (("reminders", "user_id"),), "utilities"),
    Section("levels", "XP, level and rank card background", (("level_members", "user_id"), ("level_backgrounds", "user_id"), ("level_events", "user_id")), "levels"),
    Section("giveaways", "Giveaway entries", (("giveaway_entries", "user_id"),), "utilities"),
    Section("games", "Game scores", (("toproll_scores", "user_id"),), "fun"),
    Section("usage", "Command use counts", (("command_uses", "user_id"),)),
    Section("voice_counts", "Message counts for voice access", (("voice_message_counts", "user_id"),), "voice"),
)
BY_KEY = {section.key: section for section in SECTIONS}

# Every other table that can name a person, and why it isn't theirs to delete. A test fails when a new table is in neither place.
KEPT = {
    "raid_holds": "a moderation record", "raid_modes": "a moderation record", "active_mutes": "a moderation record", "cases": "a moderation record", "notes": "a moderation record", "mini_mutes": "a moderation record", "mini_mute_votes": "a moderation record",
    "mini_mute_ballots": "a moderation record", "drama_incidents": "a moderation record", "lockdowns": "a moderation record", "moderator_pings": "a moderation record",
    "reports": "a moderation record", "review_state": "a moderation record", "review_threads": "a moderation record", "voice_actions": "a moderation record", "voice_access": "a moderation record",
    "voice_reports": "a moderation record", "sticky_roles": "puts roles back on someone who rejoins, a mute among them", "messages": "ends with its retention", "log_entries": "ends with its retention",
    "activity": "records who changed a setting", "guild_settings": "the server's setup", "autofeeds": "the server's setup", "feeds": "the server's setup", "twitch_subs": "the server's setup",
    "youtube_subs": "the server's setup", "triggers": "the server's setup", "sticky_messages": "the server's setup", "temp_channels": "the server's setup", "rr_messages": "the server's setup",
    "role_bulk_jobs": "the server's setup", "role_rule_jobs": "the server's setup", "backup_snapshots": "a copy of the server's data, and someone's rows in it stay until that copy ages out", "adopt_items": "the server's setup", "adopt_jobs": "the server's setup", "posted_messages": "the server's setup",
    "posted_versions": "the server's setup", "saved_messages": "the server's setup", "scheduled_posts": "the server's setup", "giveaways": "a public draw and who won it", "timers": "ends when the job runs", "level_backups": "copies of the whole board, and a person's entry comes out when they delete their level data", "tags": "the server's commands, which staff can reassign", "tag_aliases": "the server's commands, which staff can reassign",
    "tag_shares": "the server's commands, which staff can reassign", "proposals": "a public vote and the server's record of it", "suggestions": "a public post staff decided on",
    "echo_messages": "who sent an echoed message, for staff", "starboard_entries": "a public post", "starboard_stars": "a public reaction", "rr_seen": "a public reaction", "voicelink_held": "ends when they leave the voice channel",
    "web_sessions": "signing out ends it, on this device or on all of them", "autorole_waiting": "ends when they pass the rules screen or leave",
}


# Who a moderator was isn't the member's own record.
STAFF_COLUMNS = frozenset({"deleted_by"})


def _plain(row: dict) -> dict:
    """A row a JSON file can hold. A stored picture comes out as base64."""
    return {name: base64.b64encode(value).decode() if isinstance(value, (bytes, bytearray, memoryview)) else value for name, value in row.items() if name not in STAFF_COLUMNS}


class MyData(Module):
    key = "mydata"
    category = "Config"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._looks: dict[int, tuple[float, int]] = {}

    def allow_look(self, user_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        start, count = self._looks.get(user_id, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        self._looks[user_id] = (start, count + 1)
        if len(self._looks) > 10_000:
            self._looks.pop(next(iter(self._looks)))
        return count < LOOKS_PER_MINUTE

    def _server_name(self, guild_id: int | None) -> str:
        if guild_id is None:
            return "Direct messages"
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            return guild.name
        info = self.bot.ops.describe_guild(guild_id)
        return info["name"] if info else "A server the bot has left"

    async def overview(self, user_id: int) -> list[dict]:
        """The person's rows, by server."""
        found: dict[int | None, dict] = {}

        def server(guild_id: int | None) -> dict:
            if guild_id not in found:
                found[guild_id] = {"id": guild_id, "name": self._server_name(guild_id), "sections": [], "messages": 0, "message_days": None, "log_days": None}
            return found[guild_id]

        for section in SECTIONS:
            counts: dict[int | None, int] = {}
            for table, column in section.tables:
                for row in await self.db.all(f"SELECT guild_id, COUNT(*) AS n FROM {table} WHERE {column} = ? GROUP BY guild_id", (user_id,)):
                    counts[row["guild_id"]] = counts.get(row["guild_id"], 0) + row["n"]
            for guild_id, count in counts.items():
                server(guild_id)["sections"].append({"key": section.key, "label": section.label, "count": count})
        logs = self.bot.module("logging")
        if logs is not None:
            await logs.flush()
        # One server at a time, because the store's index leads with the server. Asked across all of them, this would read every stored message.
        for guild_id in {*(guild.id for guild in self.bot.guilds), *(key for key in found if key is not None)}:
            count = await self.message_db.scalar("SELECT COUNT(*) FROM messages WHERE guild_id = ? AND author_id = ?", (guild_id, user_id))
            if count:
                server(guild_id)["messages"] = count
        for entry in found.values():
            if entry["id"] is not None and logs is not None:
                cfg = self.bot.settings.get(entry["id"], logs.key)
                entry["message_days"] = cfg["message_retention_days"]
                entry["log_days"] = cfg["log_retention_days"]
        return sorted(found.values(), key=lambda entry: entry["name"].lower())

    async def delete(self, user_id: int, user_name: str, guild_id: int | None, key: str) -> None:
        section = BY_KEY[key]
        owner = self.bot.module(section.module) if section.module else None
        flush = getattr(owner, "flush", None) or getattr(owner, "flush_counts", None)
        if flush is not None:
            await flush()  # numbers waiting in memory would otherwise be written back
        if key == "reminders":
            for row in await self.db.all("SELECT id FROM reminders WHERE guild_id IS ? AND user_id = ?", (guild_id, user_id)):
                await self.bot.timers.cancel_key("reminder", f"reminder:{row['id']}")
        removed = 0
        for table, column in section.tables:
            removed += await self.db.run(f"DELETE FROM {table} WHERE guild_id IS ? AND {column} = ?", (guild_id, user_id))
        forget = getattr(owner, "forget_person", None)
        if forget is not None and guild_id is not None:
            removed += await forget(guild_id, user_id)  # copies a feature keeps inside another row
        if not removed:
            return  # nothing happened, and a stranger shouldn't be able to write to a server's Activity page
        if owner is not None and guild_id is not None:
            await owner.on_guild_restored(guild_id)  # what it holds in memory is older than its tables now
        if guild_id is not None:
            await self.bot.activity_log.record(guild_id, Actor(user_id, user_name), "mydata.delete", detail=section.label)

    async def export(self, user_id: int) -> dict:
        """Their own rows, for them to keep. Moderation records aren't in it, for the reason at the top of this file."""
        out: dict[str, Any] = {"user_id": user_id, "exported_at": time.time(), "tables": {}}
        room = EXPORT_MAX
        for section in SECTIONS:
            for table, column in section.tables:
                rows = await self.db.all(f"SELECT * FROM {table} WHERE {column} = ? LIMIT ?", (user_id, room))
                out["tables"][table] = [_plain(row) for row in rows]
                room -= len(rows)
        logs = self.bot.module("logging")
        if logs is not None:
            await logs.flush()
        messages: list[dict] = []
        for guild in self.bot.guilds:
            rows = await self.message_db.all("SELECT * FROM messages WHERE guild_id = ? AND author_id = ? ORDER BY created_at DESC LIMIT ?", (guild.id, user_id, max(room, 0)))
            messages += [_plain(row) for row in rows]
            room -= len(rows)
        out["tables"]["messages"] = messages
        return out
