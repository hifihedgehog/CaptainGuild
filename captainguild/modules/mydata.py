"""What the bot holds about a person, for that person: see it, download it, and delete what's theirs to delete.

Records staff keep for moderation stay out of reach here, since someone could otherwise wipe the evidence about them. Stored messages and log entries end when their
retention runs out, and notes and reports when staff delete them. Cases stay on record, and staff can void one.

Statistics are counts that also make up a server's totals, so they don't get deleted here: removing your name from them moves them to no one, in every server, for good.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

from ..core.settings import Actor
from ..core.module import Module
from ..web.api import ApiError, json_response, read_json

log = logging.getLogger(__name__)

EXPORT_MAX = 100_000
LOOKS_PER_MINUTE = 10  # each look reads several tables, and only a stuck script needs more than this


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    tables: tuple[tuple[str, str], ...]  # each table and the column holding the person's ID
    module: str = ""  # the feature that also keeps these rows in memory
    database: str = "main"  # "messages" for the tables in the message database
    made_by: str = ""  # the feature whose setup makes these tables, when nothing else does. Without it loaded, they don't exist.
    anonymize: bool = False  # deleting moves the rows to no one in every server, which is the statistics privacy choice


SECTIONS = (
    Section("birthday", "Birthday", (("birthdays", "user_id"),), "greetings"),
    Section("highlights", "Highlight words, blocks and pauses", (("highlight_words", "user_id"), ("highlight_blocks", "user_id"), ("highlight_pauses", "user_id")), "utilities"),
    Section("reminders", "Reminders", (("reminders", "user_id"),), "utilities"),
    Section("levels", "XP, level and rank card background", (("level_members", "user_id"), ("level_backgrounds", "user_id"), ("level_events", "user_id")), "levels"),
    Section("giveaways", "Giveaway entries", (("giveaway_entries", "user_id"),), "utilities"),
    Section("games", "Game scores", (("toproll_scores", "user_id"),), "fun"),
    Section("usage", "Command use counts", (("command_uses", "user_id"),)),
    Section("voice_counts", "Message counts for voice access", (("voice_message_counts", "user_id"),), "voice"),
    Section("translation", "Translation language", (("translate_languages", "user_id"),)),
    Section("entrygiveaways", "Entries in giveaway channels", (("entry_giveaway_entries", "user_id"),), "entrygiveaways"),
    Section("statistics", "Statistics counted under your name", (
        ("stat_messages", "user_id"), ("stat_voice", "user_id"), ("stat_activities", "user_id"), ("stat_emoji", "user_id"), ("stat_emoji", "target_id"),
        ("stat_joins", "user_id"), ("stat_joins", "inviter_id"), ("stat_invites", "inviter_id"), ("stat_people", "user_id"),
        ("stat_forum_posts", "owner_id"), ("stat_forum_posts", "replier_id"), ("stat_recount_messages", "user_id"), ("stat_recount_emoji", "user_id"),
        ("stat_recount_people", "user_id"), ("stat_recount_places", "owner_id"), ("stat_recount_places", "answered_by"),
    ), "statistics", database="messages", anonymize=True),
    Section("statpanels", "Statistics panels you made on the dashboard", (("stat_panels", "user_id"),), made_by="statistics"),
    Section("statapikeys", "Statistics API key you made, which stops working once deleted", (("stat_api_keys", "created_by"),), made_by="statistics"),
)
# Columns a download leaves out of a row found through one of the person's own IDs: another person's ID and what it says about them, or a
# secret's hash.
LEFT_OUT = {
    ("stat_api_keys", "created_by"): ("key_hash",),
    ("stat_joins", "user_id"): ("inviter_id",),
    ("stat_joins", "inviter_id"): ("user_id", "account_created", "facts", "role_met_at", "left_at"),
    ("stat_emoji", "user_id"): ("target_id",),
    ("stat_emoji", "target_id"): ("user_id",),
    ("stat_forum_posts", "owner_id"): ("replier_id",),
    ("stat_forum_posts", "replier_id"): ("owner_id",),
    ("stat_recount_places", "owner_id"): ("answered_by",),
    ("stat_recount_places", "answered_by"): ("owner_id",),
}
STATISTICS = "statistics"
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
    "collab_editors": "who may edit a post, which its collaborators and staff set", "moved_messages": "a public post, which whoever moved it can take down with Delete Moved Message",
    "rss_feeds": "the server's setup", "entry_giveaway_setup": "the server's setup", "entry_giveaway_closed": "the server's setup",
    "archive_jobs": "the server's record of channel copies", "archive_cursors": "where each channel copy got to",
    "answer_uses": "counts toward the server's daily limit on AI answers, and goes after a day", "answer_costs": "the host's record of what each AI answer cost, kept 70 days",
    "stat_user_settings": "the person's own statistics choices, which they change on this page", "stat_members": "the server's member count over time, which names no one",
    "stat_recounts": "who started and approved a recount of the server's statistics, for staff", "stat_merges": "who started a merge of statistics channels, for staff",
    "stat_reports": "who set up a scheduled statistics report, for staff",
    "stat_presets": "the server's saved views, which staff manage", "stat_merged_channels": "which channels a merge moved, whose target is a channel, not a person",
    "stat_auto_merges": "when categories fed a merged channel, whose target is a channel, not a person", "stat_counters": "the server's setup", "stat_roles": "the server's setup",
    "greeting_posts": "which welcome, leave and ban posts are about someone, so the bot can delete those posts on time, and gone after 30 days",
}


# Who a moderator was isn't the member's own record.
STAFF_COLUMNS = frozenset({"deleted_by"})


def _plain(row: dict) -> dict:
    """A row a JSON file can hold. A stored picture comes out as base64."""
    return {name: base64.b64encode(value).decode() if isinstance(value, (bytes, bytearray, memoryview)) else value for name, value in row.items() if name not in STAFF_COLUMNS}


def _own(row: dict, table: str, column: str) -> dict:
    """A row found through the person's ID, without the columns LEFT_OUT names for that table and column."""
    left_out = LEFT_OUT.get((table, column), ())
    return _plain({name: value for name, value in row.items() if name not in left_out})


async def _set_privacy(stats: Any, user_id: int, on: bool) -> None:
    """Turns someone's statistics privacy on or off through the Statistics module, the same way the privacy command does."""
    key, values = await stats.set_privacy(user_id, on)
    if key == "privacy_wait":
        raise ApiError(409, f"Your statistics privacy changed less than a day ago. It can change again after {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(values['next']))}.")
    if key == "privacy_failed":
        raise ApiError(500, "New statistics carry no name now, but your name couldn't come off everything counted before. Finish it from this page in a few minutes.")


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
            if section.database == "messages":
                counts = await self._message_counts(section, user_id)
            elif self._missing(section):
                continue
            else:
                for table, column in section.tables:
                    for row in await self.db.all(f"SELECT guild_id, COUNT(*) AS n FROM {table} WHERE {column} = ? GROUP BY guild_id", (user_id,)):
                        counts[row["guild_id"]] = counts.get(row["guild_id"], 0) + row["n"]
            for guild_id, count in counts.items():
                server(guild_id)["sections"].append({"key": section.key, "label": section.label, "count": count, "everywhere": section.anonymize})
                if section.key == STATISTICS:
                    server(guild_id)["statistics"] = await self._statistics_summary(guild_id, user_id)
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

    def _missing(self, section: Section) -> bool:
        """True when the feature that makes the section's tables isn't loaded, so the tables aren't there to read."""
        return bool(section.made_by) and self.bot.module(section.made_by) is None

    def _statistics_here(self) -> Any:
        """The Statistics module, when it's loaded and its tables exist."""
        return self.bot.module(STATISTICS)

    async def _statistics_servers(self) -> list[int]:
        """Every server that could hold someone's statistics, read one at a time because each per-person index leads with the server."""
        stats = self._statistics_here()
        return sorted(await stats.privacy_guilds()) if stats is not None else []

    async def _message_counts(self, section: Section, user_id: int) -> dict[int | None, int]:
        counts: dict[int | None, int] = {}
        if section.module and self.bot.module(section.module) is None:
            return counts
        for guild_id in await self._statistics_servers():
            for table, column in section.tables:
                found = await self.message_db.scalar(f"SELECT COUNT(*) FROM {table} WHERE guild_id = ? AND {column} = ?", (guild_id, user_id))
                if found:
                    counts[guild_id] = counts.get(guild_id, 0) + found
        return counts

    async def _statistics_summary(self, guild_id: int, user_id: int) -> dict:
        """What the rows add up to, in words a person reads: messages, voice and app time, emoji, and joins recorded under their name."""
        db = self.message_db

        async def total(sql: str) -> float:
            return await db.scalar(sql, (guild_id, user_id)) or 0
        return {
            "messages": int(await total("SELECT SUM(count) FROM stat_messages WHERE guild_id = ? AND user_id = ?")),
            "voice_hours": round(await total("SELECT SUM(seconds) FROM stat_voice WHERE guild_id = ? AND user_id = ?") / 3600, 1),
            "activity_hours": round(await total("SELECT SUM(seconds) FROM stat_activities WHERE guild_id = ? AND user_id = ?") / 3600, 1),
            "emoji": int(await total("SELECT SUM(count) FROM stat_emoji WHERE guild_id = ? AND user_id = ?")),
            "joins": int(await total("SELECT COUNT(*) FROM stat_joins WHERE guild_id = ? AND user_id = ?")),
        }

    async def delete(self, user_id: int, user_name: str, guild_id: int | None, key: str) -> None:
        section = BY_KEY[key]
        if section.anonymize:
            stats = self._statistics_here()
            if stats is None:
                return
            await _set_privacy(stats, user_id, True)
            return  # in every server at once, so no one server's Activity page says who chose it
        if self._missing(section):
            return
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
            if section.database == "messages":
                if section.module and self.bot.module(section.module) is None:
                    continue
                for guild_id in await self._statistics_servers():
                    for table, column in section.tables:
                        rows = await self.message_db.all(f"SELECT * FROM {table} WHERE guild_id = ? AND {column} = ? LIMIT ?", (guild_id, user_id, max(room, 0)))
                        out["tables"].setdefault(f"{table}.{column}", []).extend(_own(row, table, column) for row in rows)
                        room -= len(rows)
                continue
            if self._missing(section):
                continue
            for table, column in section.tables:
                rows = await self.db.all(f"SELECT * FROM {table} WHERE {column} = ? LIMIT ?", (user_id, room))
                out["tables"][table] = [_own(row, table, column) for row in rows]
                room -= len(rows)
        if self._statistics_here() is not None:
            out["tables"]["stat_user_settings"] = [_plain(row) for row in await self.db.all("SELECT * FROM stat_user_settings WHERE user_id = ?", (user_id,))]
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

    # ---- statistics choices ----

    def site_routes(self, app: Any) -> None:
        """The person's statistics choices, which apply in every server: their name on their counts, and replies only they see."""
        from ..web.app import _for_the_person  # app.py imports this module
        app.router.add_get("/api/my-data/statistics", _for_the_person(self._choices))
        app.router.add_post("/api/my-data/statistics", _for_the_person(self._choose))

    async def _choices(self, request: Any, module: Any, user: Any, token: Any) -> Any:
        stats = self._statistics_here()
        if stats is None:
            raise ApiError(404, "This bot doesn't keep statistics.")
        return json_response(await stats.privacy_state(user.id))

    async def _choose(self, request: Any, module: Any, user: Any, token: Any) -> Any:
        stats = self._statistics_here()
        if stats is None:
            raise ApiError(404, "This bot doesn't keep statistics.")
        form = read_json(await request.read())
        if not isinstance(form, dict) or not set(form) <= {"private", "private_replies"} or not form or not all(isinstance(value, bool) for value in form.values()):
            raise ApiError(400, "Send private or private_replies as on or off.")
        if "private" in form:
            await _set_privacy(stats, user.id, form["private"])
        if "private_replies" in form:
            await stats.set_private_replies(user.id, form["private_replies"])
        return json_response(await stats.privacy_state(user.id))
