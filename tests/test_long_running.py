"""What happens after the bot has run for months: timers through outages and reconnects, restores of a live server, and what has to survive a shutdown or a recovery."""
from __future__ import annotations

import argparse
import asyncio
import datetime
import gzip
import json
import os
import threading
import time
import tracemalloc
from pathlib import Path
from unittest import mock

import discord
from discord.state import ChunkRequest

from captainguild import __main__ as program
from captainguild import bot as bot_module
from captainguild.core import activity as activity_module
from captainguild.core.timers import AWAY_SECONDS, MAX_TRIES
from captainguild.crypto import new_master_key
from captainguild.db import DatabaseKeyError
from captainguild.modules import backup as backup_module
from captainguild.modules.autopurge import AutoPurge
from captainguild.modules.autoroles import Autoroles
from captainguild.modules.backup import Backup, BackupError, read_events
from captainguild.modules.feeds import Feeds, next_run
from captainguild.modules.greetings import Greetings
from captainguild.modules.levels import Levels
from captainguild.modules.logs import Logs
from captainguild.modules.moderation import Moderation
from captainguild.modules.roles import Roles
from captainguild.modules.utilities import Utilities
from captainguild.modules.voice import Voice
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember
from tests.gateway import FakeHTTPResponse, GatewayTest, iso, new_id, user_json


class TimerCoreTest(BotTest):
    async def test_a_row_canceled_during_a_batch_does_not_run(self):
        ran = []

        async def first(payload, row):
            await self.bot.timers.cancel_key("second", "k")  # as a permanent ban cancels the temporary one's unban

        async def second(payload, row):
            ran.append("second")

        self.bot.timers.handler("first", first)
        self.bot.timers.handler("second", second)
        now = time.time()
        await self.bot.timers.schedule("first", now - 2, {})
        await self.bot.timers.schedule("second", now - 1, {}, key="k")
        self.assertEqual(await self.bot.timers.run_due(), 2)
        self.assertEqual(ran, [])

    async def test_a_row_moved_during_a_batch_waits_for_its_new_time(self):
        ran = []

        async def first(payload, row):
            await self.db.run("UPDATE timers SET due_at = ? WHERE kind = 'second'", (time.time() + 500,))

        async def second(payload, row):
            ran.append("second")

        self.bot.timers.handler("first", first)
        self.bot.timers.handler("second", second)
        now = time.time()
        await self.bot.timers.schedule("first", now - 2, {})
        await self.bot.timers.schedule("second", now - 1, {})
        await self.bot.timers.run_due()
        self.assertEqual(ran, [])
        self.assertIsNotNone(await self.db.one("SELECT id FROM timers WHERE kind = 'second'"))

    async def test_a_timer_that_keeps_failing_is_never_dropped(self):
        tries = 0

        async def always_fails(payload, row):
            nonlocal tries
            tries += 1
            raise OSError("the network is down")

        self.bot.timers.handler("x", always_fails)
        await self.bot.timers.schedule("x", time.time() - 1, {}, guild_id=self.guild.id)
        with self.assertLogs("captainguild.core.timers", level="ERROR"):
            for _ in range(MAX_TRIES + 3):
                await self.db.run("UPDATE timers SET due_at = 0 WHERE kind = 'x'")
                await self.bot.timers.run_due()
        self.assertEqual(tries, MAX_TRIES + 3)
        row = await self.db.one("SELECT * FROM timers WHERE kind = 'x'")
        self.assertEqual(row["tries"], MAX_TRIES + 3)
        self.assertAlmostEqual(row["due_at"], time.time() + 3600, delta=5)

    async def test_a_server_that_is_away_keeps_its_timers_without_using_a_try(self):
        ran = []

        async def handler(payload, row):
            ran.append(payload)

        self.bot.timers.handler("x", handler)
        gone_id = new_id()
        await self.bot.timers.schedule("x", time.time() - 1, {"where": "gone"}, guild_id=gone_id)
        await self.bot.timers.schedule("x", time.time() - 1, {"where": "here"}, guild_id=self.guild.id)
        self.guild.unavailable = True
        await self.bot.timers.run_due()
        self.assertEqual(ran, [])
        rows = await self.db.all("SELECT * FROM timers WHERE kind = 'x'")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["tries"], 0)
            self.assertAlmostEqual(row["due_at"], time.time() + AWAY_SECONDS, delta=5)
        self.guild.unavailable = False
        await self.db.run("UPDATE timers SET due_at = 0 WHERE kind = 'x'")
        await self.bot.timers.run_due()
        self.assertEqual(ran, [{"where": "here"}])
        self.assertEqual((await self.db.one("SELECT guild_id FROM timers WHERE kind = 'x'"))["guild_id"], gone_id)

    async def test_work_that_needs_no_server_runs_without_it(self):
        ran = []

        async def handler(payload, row):
            ran.append(payload)

        self.bot.timers.handler("x", handler, anywhere=True)
        await self.bot.timers.schedule("x", time.time() - 1, {"a": 1}, guild_id=new_id())
        await self.bot.timers.run_due()
        self.assertEqual(ran, [{"a": 1}])

    async def test_a_paused_loop_runs_nothing_until_it_resumes(self):
        ran = []

        async def handler(payload, row):
            ran.append(payload)

        self.bot.timers.handler("x", handler)
        self.bot.timers.pause()
        await self.bot.timers.schedule("x", time.time() - 1, {"a": 1})
        self.assertEqual(await self.bot.timers.run_due(), 0)
        self.bot.timers.start()
        await asyncio.sleep(0.1)
        self.assertEqual(ran, [])
        self.bot.timers.resume()
        for _ in range(500):
            if ran:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(ran, [{"a": 1}])

    async def test_a_pause_stops_the_rest_of_a_batch(self):
        ran = []

        async def first(payload, row):
            self.bot.timers.pause()

        async def second(payload, row):
            ran.append("second")

        self.bot.timers.handler("first", first)
        self.bot.timers.handler("second", second)
        now = time.time()
        await self.bot.timers.schedule("first", now - 2, {})
        await self.bot.timers.schedule("second", now - 1, {})
        await self.bot.timers.run_due()
        self.assertEqual(ran, [])
        self.assertIsNotNone(await self.db.one("SELECT id FROM timers WHERE kind = 'second'"))

    async def test_a_second_ready_on_the_way_keeps_the_timers_paused(self):
        release = asyncio.Event()

        async def still_arriving():
            await release.wait()

        self.bot._wait_for_member_lists = still_arriving
        await self.bot.on_connect()
        first_ready = asyncio.ensure_future(self.bot.on_ready())
        await asyncio.sleep(0)
        await self.bot.on_connect()  # the connection dropped, and a new session started over
        release.set()
        await first_ready
        self.assertFalse(self.bot.members_settled.is_set())
        self.assertEqual(await self.bot.timers.run_due(), 0)
        await self.bot.on_ready()
        self.assertTrue(self.bot.members_settled.is_set())


class ModerationTimerTest(BotTest):
    modules = [Moderation]

    async def test_a_server_away_does_not_lose_an_unban(self):
        cog = self.bot.get_cog("Moderation")
        payload = {"guild_id": self.guild.id, "user_id": 42, "user_name": "x"}
        await self.bot.timers.schedule("moderation.unban", time.time() - 1, payload, guild_id=self.guild.id, key=f"{self.guild.id}:42")
        row = await self.db.one("SELECT * FROM timers WHERE kind = 'moderation.unban'")
        self.bot.fake_guilds.clear()
        self.assertIsNone(await cog._timer_guild("moderation.unban", payload, row))
        self.assertIsNotNone(await self.bot.timers.get_key("moderation.unban", f"{self.guild.id}:42"))
        self.bot.fake_guilds.append(self.guild)
        self.guild.unavailable = True
        self.assertIsNone(await cog._timer_guild("moderation.unban", payload, row))
        self.assertIsNotNone(await self.bot.timers.get_key("moderation.unban", f"{self.guild.id}:42"))
        self.guild.unavailable = False
        self.assertIs(await cog._timer_guild("moderation.unban", payload, row), self.guild)


class AutoPurgeAwayTest(BotTest):
    modules = [AutoPurge]

    async def test_an_unavailable_server_keeps_its_channel_and_its_timer(self):
        cog = self.bot.get_cog("AutoPurge")
        room = FakeChannel(self.guild, "room")
        await cog.set_channel(self.guild.id, room.id, 3600, "all")
        key = f"{self.guild.id}:{room.id}"
        row = await self.db.one("SELECT * FROM timers WHERE key = ?", (key,))
        self.guild.unavailable = True
        channels, self.guild.channels = self.guild.channels, []  # what an unavailable server looks like
        await cog._due(json.loads(row["payload"]), row)
        self.assertIsNotNone(await self.db.one("SELECT * FROM autopurge_channels"))
        self.assertIsNotNone(await self.db.one("SELECT * FROM timers WHERE key = ?", (key,)))
        self.guild.channels = channels
        self.bot.fake_guilds.clear()  # and when the bot isn't in it at all
        row = await self.db.one("SELECT * FROM timers WHERE key = ?", (key,))
        await cog._due(json.loads(row["payload"]), row)
        self.assertIsNotNone(await self.db.one("SELECT * FROM autopurge_channels"))
        self.assertIsNotNone(await self.db.one("SELECT * FROM timers WHERE key = ?", (key,)))


class MemberListDriftTest(GatewayTest):
    modules = [Moderation]

    async def test_a_leave_during_chunking_does_not_stop_unbans(self):
        state = self.bot._connection
        guild = self.guild
        # Someone leaves while the member list is on its way: the leave comes first, and a later chunk built from the older list still holds them.
        request = ChunkRequest(guild.id, 0, asyncio.get_running_loop(), state._get_guild, cache=True)
        state._chunk_requests[request.nonce] = request
        left = user_json(new_id(), "left-during-chunk")
        state.parse_guild_member_remove({"guild_id": str(guild.id), "user": left})
        state.parse_guild_members_chunk({
            "guild_id": str(guild.id), "nonce": request.nonce, "chunk_index": 0, "chunk_count": 1,
            "members": [{"user": left, "roles": [], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "avatar": None, "premium_since": None, "communication_disabled_until": None}],
        })
        self.assertFalse(guild.chunked, "the library's count and its list now disagree for the life of the process")
        banned = new_id()
        await self.bot.timers.schedule("moderation.unban", time.time() - 1, {"guild_id": guild.id, "user_id": banned, "user_name": "x"}, guild_id=guild.id, key=f"{guild.id}:{banned}")
        await self.bot.timers.run_due()
        self.assertEqual(len([call for call in self.net.calls if call[0] == "DELETE" and "/bans/" in call[1]]), 1)
        self.assertIsNone(await self.bot.timers.get_key("moderation.unban", f"{guild.id}:{banned}"))


class PermissionLostTest(GatewayTest):
    modules = [Moderation]

    async def test_a_ban_outlives_a_two_hour_permission_loss_and_then_ends(self):
        real = self.net.http

        async def refuses(route, **kwargs):
            if route.method == "DELETE" and "/bans/" in route.path:
                raise discord.Forbidden(FakeHTTPResponse(403), {"code": 50013, "message": "Missing Permissions"})
            return await real(route, **kwargs)

        self.bot.http.request = refuses
        guild = self.guild
        banned = new_id()
        key = f"{guild.id}:{banned}"
        await self.bot.timers.schedule("moderation.unban", time.time() - 1, {"guild_id": guild.id, "user_id": banned, "user_name": "x"}, guild_id=guild.id, key=key)
        for _ in range(MAX_TRIES + 1):
            await self.db.run("UPDATE timers SET due_at = 0 WHERE kind = 'moderation.unban'")
            await self.bot.timers.run_due()
        self.errors.lines.clear()
        self.assertIsNotNone(await self.bot.timers.get_key("moderation.unban", key))
        self.bot.http.request = real  # the bot's role goes back where it was
        await self.db.run("UPDATE timers SET due_at = 0 WHERE kind = 'moderation.unban'")
        await self.bot.timers.run_due()
        self.assertEqual(len([call for call in self.net.calls if call[0] == "DELETE" and "/bans/" in call[1]]), 1)
        self.assertIsNone(await self.bot.timers.get_key("moderation.unban", key))


class SessionThatCannotResumeTest(GatewayTest):
    modules = [Moderation, Roles, Utilities, Feeds, Greetings, Autoroles]

    def _build_guild(self) -> None:
        state = self.bot._connection
        real = state.parse_guild_create

        def keep(payload):
            self.guild_payload = payload
            real(payload)

        state.parse_guild_create = keep
        super()._build_guild()
        state.parse_guild_create = real

    def _ready(self) -> None:
        self.bot._connection.parse_ready({
            "v": 10, "user": self.bot_user, "guilds": [{"id": str(self.guild_id), "unavailable": True}], "session_id": "s", "resume_gateway_url": "wss://gateway.example",
            "application": {"id": str(self.bot_id), "flags": 0}, "shard": None,
        })

    async def test_timers_wait_until_the_lists_are_back(self):
        state = self.bot._connection
        state.guild_ready_timeout = 0.01
        gid, chan, role_id = self.guild_id, int(self.text["id"]), int(self.role_member["id"])
        now = time.time()
        await self.db.run("INSERT INTO giveaways (guild_id, channel_id, message_id, host_id, prize, winners, created_at, ends_at) VALUES (?,?,?,?,?,?,?,?)", (gid, chan, new_id(), self.owner_id, "prize", 1, now - 100, now - 1))
        giveaway = await self.db.scalar("SELECT id FROM giveaways")
        for user in (self.member_id, self.mod_id, self.admin_id):
            await self.db.run("INSERT INTO giveaway_entries (giveaway_id, guild_id, user_id, entered_at) VALUES (?,?,?,?)", (giveaway, gid, user, now - 50))
        await self.bot.timers.schedule("giveaway", now - 1, {"id": giveaway}, guild_id=gid, key=f"giveaway:{giveaway}")
        autofeed = await self.bot.get_cog("Feeds")._insert(gid, chan, "silent", None, {"content": "weekly", "embeds": [], "buttons": []}, now - 1, (0, 0, 0, 0), None, None)
        await self.bot.timers.schedule("temprole", now - 1, {"guild_id": gid, "user_id": self.member_id, "role_id": role_id}, guild_id=gid, key="k1")
        await self.bot.timers.schedule("greetings.birthday_role", now - 1, {"guild_id": gid, "user_id": self.member_id, "role_id": role_id}, guild_id=gid, key="k2")
        kinds = ("giveaway", "autofeed", "temprole", "greetings.birthday_role")
        marks = ",".join("?" * len(kinds))

        self._ready()
        await asyncio.sleep(0)  # the event task starts
        self.assertFalse(self.bot.members_settled.is_set())
        self.assertEqual(len(self.bot.get_guild(gid).members), 0)
        before = len(self.net.calls)
        self.assertEqual(await self.bot.timers.run_due(), 0)
        self.assertEqual(len(self.net.calls), before)
        self.assertEqual(await self.db.scalar(f"SELECT COUNT(*) FROM timers WHERE kind IN ({marks})", kinds), 4)
        self.assertIsNone((await self.db.one("SELECT ended_at FROM giveaways"))["ended_at"])

        state.parse_guild_create(self.guild_payload)
        await asyncio.wait_for(self.bot.members_settled.wait(), 10)
        self.assertEqual(await self.bot.timers.run_due(), 4)
        await self.settle()
        self.assertEqual(await self.db.scalar(f"SELECT COUNT(*) FROM timers WHERE kind IN ({marks})", kinds), 0)
        self.assertEqual(len(json.loads((await self.db.one("SELECT winner_ids FROM giveaways"))["winner_ids"])), 1)
        self.assertIsNone(await self.db.one("SELECT id FROM autofeeds WHERE id = ?", (autofeed,)))
        self.assertEqual(len([call for call in self.net.calls if call[0] == "DELETE" and "/roles/" in call[1]]), 2)
        self.errors.lines.clear()

    async def test_the_wait_has_a_limit(self):
        async def never(guild):
            await asyncio.Event().wait()

        self.guild._member_count = len(self.guild.members) + 1  # so it counts as still arriving
        with mock.patch.object(discord.Guild, "chunk", never), mock.patch.object(bot_module, "MEMBER_LIST_SECONDS", 0.05):
            await self.bot.on_connect()
            self.assertFalse(self.bot.members_settled.is_set())
            await self.bot.on_ready()
        self.assertTrue(self.bot.members_settled.is_set())


class RestoreCase(BotTest):
    modules = [Autoroles, Logs, Levels, Utilities, Feeds, Backup]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        asyncio.get_running_loop().set_debug(False)
        self.backup = self.cog(Backup)
        self.levels = self.cog(Levels)

    async def export_bytes(self, **options):
        options.setdefault("include_messages", True)
        out = bytearray()
        async for block in self.backup.export_stream(self.guild.id, **options):
            out += block
        return bytes(out)

    async def finished(self, client):
        return await self.wait_for_job(client)

    async def preview(self, client, body):
        response = await client.post(self.path("/backup/upload"), data=body)
        self.assertEqual(response.status, 200, await response.text())
        job = await self.finished(client)
        self.assertIsNone(job["error"])
        return job["result"]

    @staticmethod
    def items(preview):
        return [row["key"] for row in preview["settings"]] + [row["key"] for row in preview["tables"]]

    async def start_restore(self, client, preview):
        response = await client.post(self.path("/backup/restore"), json={"token": preview["token"], "items": self.items(preview)})
        self.assertEqual(response.status, 200, await response.text())

    def hold_at(self, label):
        """Stop a restore at the start of one read of the file: "main" for the holding file, "messages" for the message store."""
        real = self.backup._restore_pass
        reached, release = asyncio.Event(), asyncio.Event()

        async def held(guild_id, staged, catalog, wanted, database, report, job):
            if (database is self.message_db) == (label == "messages"):
                reached.set()
                await release.wait()
            return await real(guild_id, staged, catalog, wanted, database, report, job)

        self.backup._restore_pass = held
        return reached, release


class RestoreMergesTest(RestoreCase):
    async def test_a_message_changed_since_the_backup_is_not_copied(self):
        gid = self.guild.id
        message = 1100000000000000500
        await self.message_db.run("INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, created_at) VALUES (?,?,?,?,?,?,?)", (message, gid, 5, 6, "a", "original", time.time()))
        body = await self.export_bytes()
        await self.message_db.run("UPDATE messages SET deleted_at = ?, content = 'edited' WHERE id = ?", (time.time(), message))
        client = await self.client()
        for _ in range(2):
            await self.start_restore(client, await self.preview(client, body))
            done = await self.finished(client)
            self.assertIsNone(done["error"])
            self.assertEqual(done["result"]["tables"]["messages.messages"]["renumbered"], 0)
        rows = await self.message_db.all("SELECT id, content, deleted_at FROM messages WHERE guild_id = ?", (gid,))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["content"]), (message, "edited"))
        self.assertIsNotNone(rows[0]["deleted_at"])

    async def test_a_message_another_server_holds_still_takes_a_new_number(self):
        gid, other = self.guild.id, new_id()
        message = 1100000000000000500
        await self.message_db.run("INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, created_at) VALUES (?,?,?,?,?,?,?)", (message, gid, 5, 6, "a", "mine", 1.0))
        body = await self.export_bytes()
        await self.message_db.run("UPDATE messages SET guild_id = ?, content = 'theirs' WHERE id = ?", (other, message))
        client = await self.client()
        await self.start_restore(client, await self.preview(client, body))
        done = await self.finished(client)
        self.assertEqual(done["result"]["tables"]["messages.messages"]["renumbered"], 1)
        self.assertEqual((await self.message_db.one("SELECT guild_id, content FROM messages WHERE id = ?", (message,)))["content"], "theirs")
        self.assertEqual((await self.message_db.one("SELECT content FROM messages WHERE guild_id = ?", (gid,)))["content"], "mine")


class RestoreSwapTest(RestoreCase):
    async def test_a_row_another_server_holds_takes_a_new_number_and_its_children_follow(self):
        gid, other = self.guild.id, new_id()
        giveaway = await self.db.insert("INSERT INTO giveaways (guild_id, channel_id, host_id, prize, winners, created_at, ends_at) VALUES (?,?,?,?,?,?,?)", (gid, self.channel.id, 1, "prize", 1, 1.0, 2.0))
        await self.db.run("INSERT INTO giveaway_entries (giveaway_id, guild_id, user_id, entered_at) VALUES (?,?,?,?)", (giveaway, gid, 77, 1.0))
        body = await self.export_bytes(include_messages=False)
        await self.db.run("UPDATE giveaways SET guild_id = ?, prize = 'theirs' WHERE id = ?", (other, giveaway))
        await self.db.run("UPDATE giveaway_entries SET guild_id = ?, user_id = 88 WHERE giveaway_id = ?", (other, giveaway))
        client = await self.client()
        await self.start_restore(client, await self.preview(client, body))
        done = await self.finished(client)
        self.assertIsNone(done["error"])
        self.assertEqual(done["result"]["tables"]["main.giveaways"]["renumbered"], 1)
        mine = await self.db.one("SELECT id, prize FROM giveaways WHERE guild_id = ?", (gid,))
        self.assertNotEqual(mine["id"], giveaway)
        self.assertEqual(mine["prize"], "prize")
        self.assertEqual((await self.db.one("SELECT giveaway_id FROM giveaway_entries WHERE guild_id = ?", (gid,)))["giveaway_id"], mine["id"])
        self.assertEqual((await self.db.one("SELECT prize FROM giveaways WHERE id = ?", (giveaway,)))["prize"], "theirs")

    async def test_other_servers_can_write_while_the_file_is_read(self):
        await self.db.run("INSERT INTO highlight_words VALUES (?, 77, 'word', 1.5)", (self.guild.id,))
        body = await self.export_bytes(include_messages=False)
        client = await self.client()
        preview = await self.preview(client, body)
        reached, release = self.hold_at("main")
        await self.start_restore(client, preview)
        await asyncio.wait_for(reached.wait(), 20)
        await asyncio.wait_for(self.db.run("INSERT INTO highlight_words VALUES (?, 78, 'other', 1.5)", (new_id(),)), 5)
        self.assertEqual(len(await self.db.all("SELECT * FROM highlight_words WHERE guild_id = ?", (self.guild.id,))), 1, "the live table is whole while the file is read")
        release.set()
        self.assertIsNone((await self.finished(client))["error"])

    async def test_a_read_cannot_land_between_the_delete_and_the_insert(self):
        await self.db.run("INSERT INTO highlight_words VALUES (?, 77, 'word', 1.5)", (self.guild.id,))
        body = await self.export_bytes(include_messages=False)
        client = await self.client()
        preview = await self.preview(client, body)
        seen = []
        running = True

        async def keep_reading():
            while running:
                seen.append(await self.db.scalar("SELECT COUNT(*) FROM highlight_words WHERE guild_id = ?", (self.guild.id,)))
                await asyncio.sleep(0)

        reader = asyncio.ensure_future(keep_reading())
        await self.start_restore(client, preview)
        self.assertIsNone((await self.finished(client))["error"])
        running = False
        await reader
        self.assertGreater(len(seen), 0)
        self.assertEqual(set(seen), {1})

    async def test_settings_and_tables_land_before_the_long_read_of_the_messages(self):
        gid = self.guild.id
        await self.bot.settings.save(gid, "autoroles", {"roles": [self.role_member.id]})
        await self.db.run("INSERT INTO highlight_words VALUES (?, 77, 'old-word', 1.5)", (gid,))
        await self.message_db.run("INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, created_at) VALUES (1,?,5,6,'a','m',1.0)", (gid,))
        body = await self.export_bytes()
        await self.db.run("UPDATE highlight_words SET word = 'new-word'")
        await self.bot.settings.save(gid, "autoroles", {"roles": []})
        await self.message_db.run("DELETE FROM messages")
        client = await self.client()
        preview = await self.preview(client, body)
        reached, release = self.hold_at("messages")
        await self.start_restore(client, preview)
        await asyncio.wait_for(reached.wait(), 20)
        self.assertEqual((await self.db.one("SELECT word FROM highlight_words"))["word"], "old-word")
        self.assertEqual(self.settings("autoroles")["roles"], [self.role_member.id])
        release.set()
        self.assertIsNone((await self.finished(client))["error"])
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM messages"), 1)

    async def test_previewing_a_snapshot_during_a_restore_leaves_its_file_alone(self):
        gid = self.guild.id
        await self.db.run("INSERT INTO highlight_words VALUES (?, 77, 'word', 1.5)", (gid,))
        await self.message_db.run("INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, created_at) VALUES (1,?,5,6,'a','m',1.0)", (gid,))
        snapshot = await self.backup._write_snapshot(gid, kind="manual", actor_name="x", include_messages=False)
        body = await self.export_bytes()
        await self.message_db.run("DELETE FROM messages")
        client = await self.client()
        preview = await self.preview(client, body)
        staged_path = self.backup._staged[gid].path
        reached, release = self.hold_at("messages")
        await self.start_restore(client, preview)
        await asyncio.wait_for(reached.wait(), 20)
        response = await client.post(self.path(f"/backup/snapshots/{snapshot['id']}/preview"))
        self.assertEqual(response.status, 409)
        self.assertTrue(staged_path.exists())
        release.set()
        done = await self.finished(client)
        self.assertIsNone(done["error"])
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM messages"), 1)

    async def test_an_upload_refused_during_a_job_leaves_no_file_behind(self):
        gid = self.guild.id
        body = await self.export_bytes(include_messages=False)
        client = await self.client()
        preview = await self.preview(client, body)
        reached, release = self.hold_at("main")
        await self.start_restore(client, preview)
        await asyncio.wait_for(reached.wait(), 20)
        folder = self.backup._snapshot_path(gid, "x").parent
        before = {path.name for path in folder.iterdir()}
        with self.assertRaises(backup_module.ApiError):
            self.backup._stage(gid, backup_module.Staged("t", self.backup._snapshot_path(gid, "upload-extra.db"), True, "x"))
        self.assertEqual({path.name for path in folder.iterdir()}, before)
        release.set()
        self.assertIsNone((await self.finished(client))["error"])

    async def test_no_holding_file_stays_behind(self):
        gid = self.guild.id
        await self.db.run("INSERT INTO highlight_words VALUES (?, 77, 'word', 1.5)", (gid,))
        body = await self.export_bytes(include_messages=False)
        client = await self.client()
        await self.start_restore(client, await self.preview(client, body))
        self.assertIsNone((await self.finished(client))["error"])
        names = [path.name for path in self.backup._snapshot_path(gid, "x").parent.iterdir()]
        self.assertEqual([name for name in names if "holding" in name], [])


class RestoreLevelsTest(RestoreCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild.id, "levels", {"enabled": True})
        self.talker = FakeMember(self.guild, "talker", self.role_member)

    async def seed_and_export(self):
        gid = self.guild.id
        await self.db.run("INSERT INTO level_members (guild_id, user_id, xp, messages, voice_minutes, name, updated_at) VALUES (?,?,90000,0,0,'talker',1.0)", (gid, self.talker.id))
        body = await self.export_bytes(include_messages=False)
        await self.db.run("UPDATE level_members SET xp = 1000 WHERE user_id = ?", (self.talker.id,))
        return body

    async def test_someone_talking_during_a_restore_keeps_the_restored_xp(self):
        gid = self.guild.id
        body = await self.seed_and_export()
        client = await self.client()
        preview = await self.preview(client, body)
        reached, release = self.hold_at("main")
        await self.start_restore(client, preview)
        await asyncio.wait_for(reached.wait(), 20)
        await self.levels._award(self.talker, self.channel, self.levels.cfg(gid), message_count=1)  # the live table is whole, so this reads 1000
        self.assertGreater(self.levels._entries[(gid, self.talker.id)].xp, 1000)
        release.set()
        self.assertIsNone((await self.finished(client))["error"])
        await self.levels.flush()
        self.assertEqual((await self.db.one("SELECT xp FROM level_members WHERE user_id = ?", (self.talker.id,)))["xp"], 90000)
        await self.levels._award(self.talker, self.channel, self.levels.cfg(gid), message_count=1)
        await self.levels.flush()
        self.assertGreater((await self.db.one("SELECT xp FROM level_members WHERE user_id = ?", (self.talker.id,)))["xp"], 90000)

    async def test_xp_earned_during_the_swap_is_not_saved_over_it(self):
        gid = self.guild.id
        body = await self.seed_and_export()
        client = await self.client()
        preview = await self.preview(client, body)
        real_swap = self.backup._swap_rows
        checked = []

        def swap_after_a_look(connection, guild_id, restoring, report):
            checked.append(guild_id in self.levels._restoring)
            return real_swap(connection, guild_id, restoring, report)

        self.backup._swap_rows = swap_after_a_look
        real_before = self.levels.before_restore

        async def before_then_talk(guild_id):
            await real_before(guild_id)
            await self.levels._award(self.talker, self.channel, self.levels.cfg(gid), message_count=1)

        self.levels.before_restore = before_then_talk
        await self.start_restore(client, preview)
        self.assertIsNone((await self.finished(client))["error"])
        self.assertEqual(checked, [True])
        await self.levels.flush()
        self.assertEqual((await self.db.one("SELECT xp FROM level_members WHERE user_id = ?", (self.talker.id,)))["xp"], 90000)
        self.assertEqual(self.levels._restoring, set())

    async def test_a_failed_swap_changes_nothing_and_xp_goes_on(self):
        gid = self.guild.id
        body = await self.seed_and_export()
        client = await self.client()
        preview = await self.preview(client, body)

        def boom(connection, guild_id, restoring, report):
            connection.execute("DELETE FROM level_members")
            raise RuntimeError("the disk went away")

        self.backup._swap_rows = boom
        await self.start_restore(client, preview)
        with self.assertLogs("captainguild.modules.backup", level="ERROR"):
            done = await self.finished(client)
        self.assertIsNotNone(done["error"])
        self.assertEqual((await self.db.one("SELECT xp FROM level_members WHERE user_id = ?", (self.talker.id,)))["xp"], 1000)
        self.assertEqual(self.levels._restoring, set())
        await self.levels._award(self.talker, self.channel, self.levels.cfg(gid), message_count=1)
        self.assertTrue(self.levels._dirty)


class HostileFileTest(RestoreCase):
    async def doc(self):
        return json.loads(await self.export_bytes(include_messages=False))

    async def seed_autofeed(self, **extra):
        columns = {"guild_id": self.guild.id, "channel_id": self.channel.id, "mention": "silent", "payload": '{"content":"x","embeds":[],"buttons":[]}', "next_at": time.time() - 5000, "created_at": time.time(), **extra}
        names = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        return await self.db.insert(f"INSERT INTO autofeeds ({names}) VALUES ({marks})", list(columns.values()))

    async def restore_doc(self, doc):
        client = await self.client()
        await self.start_restore(client, await self.preview(client, json.dumps(doc).encode()))
        done = await self.finished(client)
        self.assertIsNone(done["error"])
        return done["result"]

    async def test_a_row_numbered_near_the_top_cannot_stop_a_shared_counter(self):
        await self.seed_autofeed()
        doc = await self.doc()
        table = doc["tables"]["main.autofeeds"]
        table["rows"][0][table["columns"].index("id")] = str(2 ** 63 - 1)
        extra = list(table["rows"][0])
        extra[table["columns"].index("id")] = str(2 ** 53)
        table["rows"].append(extra)
        result = await self.restore_doc(doc)
        self.assertEqual(result["tables"]["main.autofeeds"]["dropped"], 2)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autofeeds"), 0)
        # The next server's row still gets a number.
        self.assertLess(await self.seed_autofeed(), 2 ** 53)

    async def test_a_snowflake_above_that_line_is_fine_where_nothing_counts(self):
        message = 1400000000000000000
        await self.message_db.run("INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, created_at) VALUES (?,?,5,6,'a','m',1.0)", (message, self.guild.id))
        body = await self.export_bytes()
        await self.message_db.run("DELETE FROM messages")
        client = await self.client()
        await self.start_restore(client, await self.preview(client, body))
        self.assertIsNone((await self.finished(client))["error"])
        self.assertEqual((await self.message_db.one("SELECT id FROM messages"))["id"], message)

    async def test_a_channel_written_to_slip_past_the_check_is_refused(self):
        other_guild = FakeGuild()
        self.bot.fake_guilds.append(other_guild)
        other = FakeChannel(other_guild, "theirs")
        await self.seed_autofeed()
        doc = await self.doc()
        table = doc["tables"]["main.autofeeds"]
        at = table["columns"].index("channel_id")
        row = table["rows"].pop()
        for written in (f" {other.id}", f"+{other.id}", f"{other.id} ", float(other.id), str(other.id)):
            copy = list(row)
            copy[at] = written
            copy[table["columns"].index("id")] = None
            table["rows"].append(copy)
        result = await self.restore_doc(doc)
        counts = result["tables"]["main.autofeeds"]
        self.assertEqual(counts["added"], 0)
        self.assertEqual(counts["dropped"], 5)  # four that aren't whole numbers, and one that names another server's channel
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autofeeds"), 0)

    async def test_a_channel_in_text_is_read_however_it_is_written(self):
        other_guild = FakeGuild()
        self.bot.fake_guilds.append(other_guild)
        other = FakeChannel(other_guild, "theirs")
        for written in (other.id, str(other.id), f" {other.id}", f"+{other.id}", f"{other.id}\n"):
            self.assertTrue(self.backup._channel_elsewhere(self.guild.id, written), repr(written))
        self.assertFalse(self.backup._channel_elsewhere(self.guild.id, self.channel.id))
        self.assertFalse(self.backup._channel_elsewhere(self.guild.id, "not a number"))

    async def test_a_doctored_schedule_cannot_freeze_the_bot(self):
        await self.seed_autofeed(repeat_minutes=1)
        doc = await self.doc()
        table = doc["tables"]["main.autofeeds"]
        table["rows"][0][table["columns"].index("repeat_minutes")] = 1e-9
        result = await self.restore_doc(doc)
        self.assertEqual(result["tables"]["main.autofeeds"]["dropped"], 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autofeeds"), 0)

    async def test_a_big_value_outside_the_rows_is_refused(self):
        body = json.dumps({"format": backup_module.FORMAT, "version": 1, "guild_id": str(self.guild.id), "junk": "x" * (backup_module.MAX_HEADER_CHARS + 10)})

        async def blocks():
            yield body.encode()

        with self.assertRaises(BackupError):
            async for _ in read_events(blocks()):
                pass

    async def test_many_small_values_outside_the_rows_are_refused(self):
        fields = {f"k{n}": "x" * 900_000 for n in range(10)}
        body = json.dumps({"format": backup_module.FORMAT, **fields})

        async def blocks():
            yield body.encode()

        with self.assertRaises(BackupError):
            async for _ in read_events(blocks()):
                pass

    async def test_long_rows_end_a_batch_early(self):
        row = ["x" * (3 * 1024 * 1024)]
        body = json.dumps({"tables": {"main.anything": {"columns": ["a"], "rows": [row] * 10}}}).encode()

        async def blocks():
            for start in range(0, len(body), 1024 * 1024):
                yield body[start:start + 1024 * 1024]

        sizes = [len(event[2]) async for event in read_events(blocks()) if event[0] == "rows"]
        self.assertEqual(sum(sizes), 10)
        self.assertLessEqual(max(sizes), 3)

    async def test_a_small_upload_cannot_fill_memory(self):
        # Fifteen rows of 15 MiB each pack down to a fraction of a megabyte.
        row = ["x" * (15 * 1024 * 1024)]
        document = json.dumps({"format": backup_module.FORMAT, "version": 1, "guild_id": str(self.guild.id), "settings": {}, "tables": {"main.highlight_words": {"columns": ["word"], "rows": [row] * 15}}}).encode()
        packed = gzip.compress(document, 9)
        self.assertLess(len(packed), 1024 * 1024)

        async def blocks():
            yield packed

        tracemalloc.start()
        try:
            async for _ in read_events(backup_module.inflate(blocks())):
                pass
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 100 * 1024 * 1024)


class RecoveryTest(RestoreCase):
    async def test_a_snapshot_the_database_does_not_know_goes_back_on_the_list(self):
        gid = self.guild.id
        await self.db.run("INSERT INTO highlight_words VALUES (?, 77, 'word', 1.5)", (gid,))
        kept = await self.backup._write_snapshot(gid, kind="manual", actor_name="x", include_messages=False)
        newer = await self.backup._write_snapshot(gid, kind="scheduled", actor_name=None, include_messages=False)
        # Starting on a copy of the database from before the newer one was made.
        await self.db.run("DELETE FROM backup_snapshots WHERE id = ?", (newer["id"],))
        folder = self.backup._snapshot_path(gid, "x").parent
        (folder / "half.db.part").write_bytes(b"half")
        (folder / "half.db.part-wal").write_bytes(b"half")
        (folder / "upload-x.db").write_bytes(b"left over")
        (folder / "not-a-snapshot.db").write_bytes(b"something else")
        fresh = Backup(self.bot)
        with self.assertLogs("captainguild.modules.backup", level="WARNING"):
            await fresh._tidy_files()
        names = {path.name for path in folder.iterdir()}
        self.assertEqual(names, {kept["file"], newer["file"], "not-a-snapshot.db"})
        back = await self.db.one("SELECT * FROM backup_snapshots WHERE file = ?", (newer["file"],))
        self.assertEqual(back["guild_id"], gid)
        self.assertEqual(back["row_count"], newer["row_count"])
        self.assertAlmostEqual(back["created_at"], newer["created_at"], delta=5)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM backup_snapshots WHERE file = ?", (kept["file"],)), 1)
        # And it can be put back.
        client = await self.client()
        await self.db.run("DELETE FROM highlight_words")
        response = await client.post(self.path(f"/backup/snapshots/{back['id']}/preview"))
        self.assertEqual(response.status, 200)
        preview = (await self.finished(client))["result"]
        await self.start_restore(client, preview)
        self.assertIsNone((await self.finished(client))["error"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM highlight_words"), 1)

    async def test_a_snapshot_of_another_server_in_this_folder_is_left_alone(self):
        gid = self.guild.id
        row = await self.backup._write_snapshot(gid, kind="manual", actor_name="x", include_messages=False)
        await self.db.run("DELETE FROM backup_snapshots")
        wrong = self.backup._snapshot_path(new_id(), row["file"])
        wrong.parent.mkdir(parents=True)
        os.replace(self.backup._snapshot_path(gid, row["file"]), wrong)
        with self.assertLogs("captainguild.modules.backup", level="WARNING"):
            await Backup(self.bot)._tidy_files()
        self.assertTrue(wrong.exists())
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM backup_snapshots"), 0)


class ShutdownTest(BotTest):
    modules = [Levels, Voice]

    async def hold_writes(self):
        held, release = asyncio.Event(), asyncio.Event()

        async def holder():
            async with self.db.transaction():
                held.set()
                await release.wait()

        task = asyncio.ensure_future(holder())
        await held.wait()
        return task, release

    async def test_a_canceled_flush_keeps_the_xp_for_the_last_one(self):
        levels = self.cog(Levels)
        gid = self.guild.id
        await self.bot.settings.save(gid, "levels", {"enabled": True})
        await levels._award(self.member, self.channel, levels.cfg(gid), message_count=1)
        xp = levels._entries[(gid, self.member.id)].xp
        task, release = await self.hold_writes()
        flusher = asyncio.ensure_future(levels.flush())
        await asyncio.sleep(0.02)
        flusher.cancel()  # what shutting down does to the flusher
        with self.assertRaises(asyncio.CancelledError):
            await flusher
        release.set()
        await task
        await levels.flush()
        self.assertEqual((await self.db.one("SELECT xp FROM level_members WHERE user_id = ?", (self.member.id,)))["xp"], xp)

    async def test_a_canceled_write_keeps_the_voice_counts(self):
        voice = self.cog(Voice)
        voice._pending_counts[(self.guild.id, self.member.id)] += 3
        task, release = await self.hold_writes()
        flusher = asyncio.ensure_future(voice.flush_counts())
        await asyncio.sleep(0.02)
        flusher.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await flusher
        release.set()
        await task
        await voice.flush_counts()
        self.assertEqual((await self.db.one("SELECT count FROM voice_message_counts WHERE user_id = ?", (self.member.id,)))["count"], 3)


class EntryPointTest(BotTest):
    async def test_a_damaged_message_database_lets_the_process_exit(self):
        # An open database runs a thread that keeps the process alive, so a start that fails must close whatever opened.
        data = Path(self.folder.name) / "start"
        data.mkdir()
        (data / "messages.db").write_bytes(os.urandom(8192))
        args = argparse.Namespace(data=str(data), public_url="https://bot.example", host="127.0.0.1", port=0)
        before = set(threading.enumerate())
        with mock.patch.dict(os.environ, {"MASTER_KEY": new_master_key(), "DISCORD_TOKEN": "not a token"}):
            with self.assertRaises(DatabaseKeyError):
                await program.run(args)
        for _ in range(100):
            left = [thread for thread in threading.enumerate() if thread not in before and not thread.daemon]
            if not left:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(left, [])


class ScheduleTest(BotTest):
    modules = [Utilities]

    def test_next_run_cannot_spin(self):
        now = time.time()
        self.assertIsNone(next_run(now - 100000, (0, 0, 0, 1e-9), now))
        self.assertIsNone(next_run(now - 100000, (0, 0, -1, 0), now))
        self.assertIsNone(next_run(now - 100000, (0, 0, 0, 0), now))
        self.assertIsNone(next_run(now - 100000, (0, 0, 0, 59), now), "shorter than the shortest repeat a form can save")
        started = time.perf_counter()
        far_behind = next_run(0.0, (0, 0, 1, 0), now)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertGreater(far_behind, now)

    def test_next_run_catches_up_on_a_real_schedule(self):
        start = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
        now = start + 86400 * 2.5
        self.assertEqual(next_run(start, (0, 1, 0, 0), now), start + 86400 * 3)
        self.assertEqual(next_run(start, (1, 0, 0, 0), start + 1), datetime.datetime(2026, 2, 15, 12, 0, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(next_run(start, (0, 0, 1, 0), start + 5400), start + 7200)

    async def test_a_reminder_repeating_too_fast_ends_instead_of_spinning(self):
        cog = self.cog(Utilities)
        reminder_id = await self.db.insert(
            "INSERT INTO reminders (guild_id, channel_id, user_id, about, created_at, due_at, repeat_seconds) VALUES (?,?,?,?,?,?,?)",
            (self.guild.id, self.channel.id, self.member.id, "x", 1.0, 2.0, -5),
        )
        await cog._reminder_due({"id": reminder_id}, {})
        self.assertIsNone(await self.db.one("SELECT id FROM reminders WHERE id = ?", (reminder_id,)))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'reminder'"), 0)

    async def test_a_reminder_goes_out_even_if_its_server_is_gone(self):
        cog = self.cog(Utilities)
        reminder = await cog._add_reminder(guild_id=new_id(), channel_id=None, user_id=self.member.id, about="x", due_at=time.time() - 1)
        sent = []
        cog._deliver_reminder = lambda row: self._note(sent, row)
        await self.bot.timers.run_due()
        self.assertEqual([row["id"] for row in sent], [reminder])

    @staticmethod
    async def _note(sent, row):
        sent.append(row)


class RetentionTest(BotTest):
    modules = [Logs]

    async def test_old_command_counts_go_and_recent_ones_stay(self):
        today = int(time.time() // 86400)
        gid = self.guild.id
        await self.db.many("INSERT INTO command_uses (guild_id, day, command, user_id, uses) VALUES (?, ?, 'ban', ?, 1)", [(gid, today - 91, n) for n in range(3)])
        await self.db.run("INSERT INTO command_uses (guild_id, day, command, user_id, uses) VALUES (?, ?, 'ban', 1, 1)", (gid, today - 90))
        await self.db.run("INSERT INTO command_uses (guild_id, day, command, user_id, uses) VALUES (?, ?, 'ban', 2, 1)", (gid, today))
        await self.cog(Logs).prune()
        self.assertEqual(sorted(row["day"] for row in await self.db.all("SELECT day FROM command_uses")), [today - 90, today])

    async def test_activity_keeps_the_newest_rows_of_each_server(self):
        gid, other = self.guild.id, new_id()
        for n in range(6):
            await self.bot.activity_log.record(gid, None, f"a{n}")
        await self.bot.activity_log.record(other, None, "theirs")
        with mock.patch.object(activity_module, "ACTIVITY_PER_SERVER", 3):
            await self.bot.activity_log.prune()
        self.assertEqual([row["action"] for row in await self.db.all("SELECT action FROM activity WHERE guild_id = ? ORDER BY id", (gid,))], ["a3", "a4", "a5"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM activity WHERE guild_id = ?", (other,)), 1)
