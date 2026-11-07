"""Putting an older copy of the main database in place with dbtools restore, and what the next start does with it."""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from captainguild import dbtools
from captainguild.core.module import Module
from captainguild.db import Database, _open_encrypted
from captainguild.dbtools import FILE_RESTORED_KEY, ToolError, copy_database
from tests.fakes import KEY, BotTest, TestBot


class Listening(Module):
    key = "listening"
    role_timers = ("listening.role",)
    calls: list = []

    async def before_restore(self, guild_id):
        Listening.calls.append(("before", guild_id))

    async def on_guild_restored(self, guild_id):
        Listening.calls.append(("restored", guild_id))

    async def after_restore(self, guild_id):
        Listening.calls.append(("after", guild_id))


class Failing(Module):
    key = "failing"

    async def on_guild_restored(self, guild_id):
        raise ValueError("this one breaks")


class ToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.folder.name)
        self.source = self.path / "copy.db"
        db = Database(self.source, KEY)
        await db.open()
        await db.run("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await db.close()

    async def asyncTearDown(self):
        self.folder.cleanup()

    def stored_stamp(self, path):
        connection = _open_encrypted(str(path), KEY)
        try:
            row = connection.execute("SELECT value FROM kv WHERE key = ?", (FILE_RESTORED_KEY,)).fetchone()
            return row[0] if row else None
        finally:
            connection.close()

    async def test_a_restore_marks_the_file_and_a_plain_copy_doesnt(self):
        copy_database(self.source, self.path / "plain.db", KEY)
        self.assertIsNone(self.stored_stamp(self.path / "plain.db"))
        copy_database(self.source, self.path / "main.db", KEY, restoring=True)
        self.assertGreater(float(self.stored_stamp(self.path / "main.db")), time.time() - 60)

    async def test_what_the_old_file_left_behind_goes_with_it(self):
        target = self.path / "main.db"
        copy_database(self.source, target, KEY)
        for suffix in ("-wal", "-shm"):
            (self.path / f"main.db{suffix}").write_bytes(b"stale")
        copy_database(self.source, target, KEY, restoring=True)
        self.assertFalse((self.path / "main.db-wal").exists())
        self.assertFalse((self.path / "main.db-shm").exists())
        self.assertIsNotNone(self.stored_stamp(target))

    async def test_a_file_that_isnt_the_bots_is_refused_and_replaces_nothing(self):
        other = self.path / "other.db"
        db = Database(other, KEY)
        await db.open()
        await db.close()
        target = self.path / "main.db"
        copy_database(self.source, target, KEY)
        with self.assertRaises(ToolError):
            copy_database(other, target, KEY, restoring=True)
        self.assertIsNone(self.stored_stamp(target))

    def test_the_command_line_takes_restore_with_two_paths(self):
        self.assertEqual(dbtools.main(["restore", "a"]), 2)
        self.assertEqual(dbtools.main(["copy", "a", "b"]), 2)


class StartTest(BotTest):
    modules = [Listening, Failing]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        Listening.calls = []
        await self.bot.settings.save(self.guild.id, "core", {"prefixes": ["?"]})
        self.past = await self.bot.timers.schedule("listening.role", time.time() - 100, guild_id=self.guild.id)
        self.future = await self.bot.timers.schedule("listening.role", time.time() + 1000, guild_id=self.guild.id)
        self.other = await self.bot.timers.schedule("listening.other", time.time() - 100, guild_id=self.guild.id)

    async def stamp(self):
        await self.db.run("INSERT INTO kv (key, value) VALUES (?, ?)", (FILE_RESTORED_KEY, str(time.time())))

    async def timer_ids(self):
        return {row["id"] for row in await self.db.all("SELECT id FROM timers")}

    async def test_a_normal_start_changes_nothing(self):
        await self.bot._finish_file_restore()
        self.assertEqual(Listening.calls, [])
        self.assertIn(self.past, await self.timer_ids())

    async def test_a_restored_file_drops_past_role_changes_and_tells_every_module(self):
        await self.stamp()
        with self.assertLogs("captainguild.bot", level="ERROR"):  # the module that breaks doesn't stop the others
            await self.bot._finish_file_restore()
        self.assertEqual(await self.timer_ids(), {self.future, self.other})
        self.assertEqual(Listening.calls, [("before", self.guild.id), ("restored", self.guild.id), ("after", self.guild.id)])
        self.assertIsNotNone(await self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"role_bulk_restored:{self.guild.id}",)))
        self.assertIsNone(await self.db.scalar("SELECT value FROM kv WHERE key = ?", (FILE_RESTORED_KEY,)))

    async def test_it_runs_once(self):
        await self.stamp()
        with self.assertLogs("captainguild.bot", level="ERROR"):
            await self.bot._finish_file_restore()
        Listening.calls = []
        await self.bot._finish_file_restore()
        self.assertEqual(Listening.calls, [])

    async def test_starting_the_bot_on_a_marked_file_does_it(self):
        await self.stamp()
        second = TestBot(self.db, self.message_db)
        with self.assertLogs("captainguild.bot", level="ERROR"):
            await second.prepare([Listening, Failing])
        self.assertIn(("restored", self.guild.id), Listening.calls)
        self.assertNotIn(self.past, await self.timer_ids())
        await second.timers.stop()
