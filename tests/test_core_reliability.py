"""The core's behavior when something else goes wrong around it: tasks canceled from elsewhere, connections that die, disks that fail, saves that overlap."""
from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import logging
import struct
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord
from discord.ext import commands

from captainguild import __main__ as program
from captainguild import bot as bot_module
from captainguild.core import images, members as member_lists, messages, rankcard
from captainguild.core.durations import parse_duration
from captainguild.core.logsink import LogSink
from captainguild.core.outside import OutsideError, _is_public, check_public_link
from captainguild.core.perms import COMMAND_SCHEMA, SETTINGS_SCHEMA
from captainguild.core.settings import Settings
from captainguild.core.timers import Timers
from captainguild.core.validate import Field, Invalid, Schema, boolean, snowflakes
from captainguild.db import Database, DatabaseKeyError
from captainguild.modules.config import Config
from captainguild.modules.mydata import MyData
from tests.fakes import KEY, BotTest, FakeChannel, FakeMember, FakePermissions
from tests.gateway import GatewayTest


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(Path(self.folder.name) / "main.db", KEY)
        await self.db.open()
        await self.db.run("CREATE TABLE t (x INTEGER)")
        await self.db.run("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    async def asyncTearDown(self):
        await self.db.close()
        self.folder.cleanup()


class TransactionTest(DbCase):
    async def test_a_task_started_inside_a_transaction_doesnt_join_it(self):
        release = asyncio.Event()
        child_done = asyncio.Event()

        async def child():
            await release.wait()
            await self.db.run("INSERT INTO t VALUES (2)")
            child_done.set()

        async with self.db.transaction():
            await self.db.run("INSERT INTO t VALUES (1)")
            task = asyncio.create_task(child())
        release.set()
        await child_done.wait()
        await task
        self.assertFalse(self.db.conn.in_transaction, "the child's write committed on its own")
        async with self.db.transaction():  # the next one starts cleanly
            await self.db.run("INSERT INTO t VALUES (3)")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM t"), 3)

    async def test_nesting_in_one_task_still_joins(self):
        async with self.db.transaction():
            async with self.db.transaction():
                await self.db.run("INSERT INTO t VALUES (1)")
            self.assertTrue(self.db.conn.in_transaction)
        self.assertFalse(self.db.conn.in_transaction)

    async def test_a_commit_that_fails_rolls_back(self):
        real = self.db.conn.commit
        with mock.patch.object(self.db.conn, "commit", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                async with self.db.transaction():
                    await self.db.run("INSERT INTO t VALUES (1)")
        self.assertFalse(self.db.conn.in_transaction)
        self.assertIs(self.db.conn.commit.__func__, real.__func__)
        await self.db.run("INSERT INTO t VALUES (2)")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM t"), 1, "the row the first caller was told failed stays gone")

    async def test_exclusive_inside_a_transaction_raises_rather_than_hangs(self):
        async with self.db.transaction():
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(self.db.exclusive(lambda connection: None), 5)

    async def test_a_read_through_a_transaction_waits_for_the_other_task(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def writer():
            try:
                async with self.db.transaction():
                    await self.db.run("INSERT INTO t VALUES (1)")
                    started.set()
                    await finish.wait()
                    raise ValueError("rolled back")
            except ValueError:
                pass

        task = asyncio.create_task(writer())
        await started.wait()

        async def careful():
            async with self.db.transaction():
                return await self.db.scalar("SELECT COUNT(*) FROM t")

        read = asyncio.create_task(careful())
        await asyncio.sleep(0)
        self.assertFalse(read.done())
        finish.set()
        await task
        self.assertEqual(await read, 0, "it never saw the row that rolled back")


class OpenTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_locked_file_isnt_reported_as_a_wrong_key(self):
        from sqlcipher3 import dbapi2 as cipher

        from captainguild import db as db_module

        class Connection:
            def execute(self, sql):
                if "sqlite_master" in sql:
                    raise cipher.OperationalError("database is locked")
                return self

            def fetchall(self):
                return []

            def close(self):
                pass
        with mock.patch.object(cipher, "connect", return_value=Connection()):
            with self.assertRaises(cipher.OperationalError):
                db_module._open_encrypted("x", KEY)

    async def test_an_open_cut_short_leaves_no_connection_running(self):
        # A task canceled while the file is set up, as at shutdown, or a setup statement that fails: either way the connection's worker
        # thread, which isn't a daemon and would keep the process from exiting, ends.
        import aiosqlite
        for trouble in (asyncio.CancelledError(), OSError("disk I/O error")):
            with self.subTest(trouble=type(trouble).__name__), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
                started = []
                real_execute = aiosqlite.Connection.execute

                def execute(connection, sql, *args, **kwargs):
                    started.append(connection._thread)
                    if sql.startswith("PRAGMA synchronous"):
                        raise trouble
                    return real_execute(connection, sql, *args, **kwargs)
                database = Database(Path(folder) / "cut.db", KEY)
                kept = None
                with mock.patch.object(aiosqlite.Connection, "execute", execute):
                    try:
                        await database.open()
                    except type(trouble) as error:
                        kept = error  # held with its frames, the way a finished task holds what ended it
                self.assertIsNotNone(kept)
                self.assertIsNone(database.conn)
                thread = started[0]
                for _ in range(200):
                    if not thread.is_alive():
                        break
                    await asyncio.sleep(0.01)
                self.assertFalse(thread.is_alive(), "the connection's thread ended")

    async def test_a_wrong_key_still_is(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            path = Path(folder) / "main.db"
            first = Database(path, KEY)
            await first.open()
            await first.close()
            second = Database(path, "2" * 64)
            with self.assertRaises(DatabaseKeyError):
                await second.open()
            await second.close()


class TimerCase(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.timers = Timers(self.db)
        await self.timers.load()

    async def asyncTearDown(self):
        await self.timers.stop()
        await super().asyncTearDown()


class TimerCancelTest(TimerCase):
    async def test_a_handler_awaiting_a_task_canceled_elsewhere_counts_as_a_failure(self):
        inner = asyncio.get_running_loop().create_future()
        inner.cancel()
        ran = []

        async def bad(payload, row):
            await inner

        async def good(payload, row):
            ran.append(1)

        self.timers.handler("bad", bad)
        self.timers.handler("good", good)
        await self.timers.schedule("bad", time.time() - 1)
        await self.timers.schedule("good", time.time() - 1)
        with self.assertLogs("captainguild.core.timers", level="ERROR"):
            await self.timers.run_due()
        self.assertEqual(ran, [1])
        row = await self.db.one("SELECT tries, due_at FROM timers WHERE kind = 'bad'")
        self.assertEqual(row["tries"], 1)
        self.assertGreater(row["due_at"], time.time())

    async def test_stop_still_stops_a_running_handler(self):
        started = asyncio.Event()

        async def slow(payload, row):
            started.set()
            await asyncio.Event().wait()

        self.timers.handler("slow", slow)
        await self.timers.schedule("slow", time.time() - 1)
        self.timers.start()
        await started.wait()
        await asyncio.wait_for(self.timers.stop(), 5)
        self.assertEqual(await self.db.scalar("SELECT tries FROM timers"), 0)


class TimerWakeTest(TimerCase):
    async def test_a_schedule_during_the_batch_isnt_lost(self):
        ran = asyncio.Event()

        async def later(payload, row):
            ran.set()

        self.timers.handler("later", later)
        real_run_due = self.timers.run_due
        once = []

        async def run_due_then_schedule(now=None):
            count = await real_run_due(now)
            if not once:
                once.append(1)
                await self.timers.schedule("later", time.time() + 0.05)
            return count

        real_scalar = self.db.scalar
        # A schedule that lands right after the read of the next due time has to survive the wait that follows it.
        asked = []

        async def scalar_then_schedule(sql, args=()):
            value = await real_scalar(sql, args)
            if "MIN(due_at)" in sql and not asked:
                asked.append(1)
                await self.timers.schedule("later", time.time() + 0.05)
            return value

        with mock.patch.object(self.db, "scalar", scalar_then_schedule), mock.patch("captainguild.core.timers.MAX_SLEEP", 3600.0):
            self.timers.start()
            await asyncio.wait_for(ran.wait(), 10)


class LeftServerTimerTest(TimerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.here = False
        self.left = True
        self.timers.server_here = lambda guild_id: self.here
        self.timers.server_left = lambda guild_id: self.left
        self.ran = []

        async def work(payload, row):
            self.ran.append(row["guild_id"])

        self.timers.handler("work", work)

    async def stamp(self, guild_id=5):
        return await self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"server_left:{guild_id}",))

    async def test_the_first_look_stamps_the_time_and_keeps_the_work(self):
        await self.timers.schedule("work", time.time() - 1, guild_id=5)
        await self.timers.run_due()
        self.assertIsNotNone(await self.stamp())
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers"), 1)
        self.assertEqual(self.ran, [])

    async def test_after_thirty_days_the_work_goes_and_says_so_once(self):
        await self.timers.schedule("work", time.time() - 1, guild_id=5)
        await self.timers.schedule("work", time.time() - 1, guild_id=5)
        await self.timers.schedule("work", time.time() + 99999, guild_id=5)
        await self.timers.schedule("work", time.time() + 99999, guild_id=6)
        await self.db.run("INSERT INTO kv (key, value) VALUES ('server_left:5', ?)", (str(time.time() - 31 * 86400),))
        with self.assertLogs("captainguild.core.timers", level="WARNING") as logged:
            await self.timers.run_due()
        self.assertEqual(len(logged.records), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE guild_id = 5"), 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE guild_id = 6"), 1)
        self.assertIsNone(await self.stamp())

    async def test_a_server_thats_only_unavailable_stamps_nothing(self):
        self.left = False
        await self.timers.schedule("work", time.time() - 1, guild_id=5)
        await self.timers.run_due()
        self.assertIsNone(await self.stamp())
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers"), 1)

    async def test_a_return_keeps_the_work(self):
        await self.timers.schedule("work", time.time() - 1, guild_id=5)
        await self.timers.run_due()
        await self.timers.server_back(5)
        self.assertIsNone(await self.stamp())
        await self.db.run("UPDATE timers SET due_at = ?", (time.time() - 1,))
        self.here = True
        await self.timers.run_due()
        self.assertEqual(self.ran, [5])


class BotLeftServerTest(BotTest):
    async def test_the_bot_tells_gone_from_away(self):
        self.assertFalse(self.bot._server_left(self.guild.id))
        self.assertTrue(self.bot._server_left(123))
        self.bot.members_settled.clear()
        self.assertFalse(self.bot._server_left(123), "a half-filled list says nothing")

    async def test_a_server_coming_back_clears_the_stamp(self):
        await self.db.run("INSERT INTO kv (key, value) VALUES (?, '1')", (f"server_left:{self.guild.id}",))
        await self.bot.on_guild_join(self.guild)
        self.assertIsNone(await self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"server_left:{self.guild.id}",)))
        await self.db.run("INSERT INTO kv (key, value) VALUES (?, '1')", (f"server_left:{self.guild.id}",))
        await self.bot.on_guild_available(self.guild)
        self.assertIsNone(await self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"server_left:{self.guild.id}",)))


class ChunkTest(GatewayTest):
    modules = []

    def counting(self):
        calls = []

        async def chunker(guild_id, query="", limit=0, presences=False, *, nonce=None):
            calls.append(nonce)

        self.bot._connection.chunker = chunker
        return calls

    async def test_a_resume_sends_the_request_again(self):
        guild = self.guild
        guild._member_count = len(guild.members) + 5000
        calls = self.counting()
        first = asyncio.ensure_future(guild.chunk())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(len(calls), 1)
        await self.bot.on_resumed()
        for _ in range(50):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(calls), 2, "a new request went out")
        self.assertTrue(first.done(), "the old waiter was let go")
        self.bot._member_list_task.cancel()

    async def test_a_resume_with_a_whole_list_asks_for_nothing(self):
        calls = self.counting()
        self.guild._member_count = len(self.guild.members)
        await self.bot.on_resumed()
        await asyncio.sleep(0.01)
        self.assertEqual(calls, [])

    async def test_a_request_nothing_answers_is_dropped_so_the_next_try_sends_again(self):
        guild = self.guild
        guild._member_count = len(guild.members) + 5000
        calls = self.counting()
        with mock.patch.object(member_lists, "CHUNK_WAIT", 0.02):
            self.assertFalse(await member_lists.whole_member_list(guild))
            self.assertFalse(await member_lists.whole_member_list(guild))
        self.assertEqual(len(calls), 2)

    async def test_ready_doesnt_ask_again_for_a_list_within_a_few_of_the_count(self):
        guild = self.guild
        guild._member_count = 1000
        calls = self.counting()
        guild._members = {index: SimpleNamespace(id=index) for index in range(995)}
        self.assertFalse(guild.chunked)
        self.bot._connects = 1
        await self.bot.on_ready()
        self.assertEqual(calls, [])


class CommandScopeTest(BotTest):
    modules = [Config]

    class Scoped(FakeChannel):
        def __init__(self, guild, name, person, *names):
            super().__init__(guild, name)
            self.person = person
            self.names = names

        def permissions_for(self, member):
            return FakePermissions(*self.names) if member is self.person else member.guild_permissions

    def decide(self, member, channel, permission):
        @commands.command(name="scoped")
        async def scoped(ctx):
            pass
        scoped.callback.__required_permissions__ = (permission,)
        return self.bot.command_decision(scoped, member, channel)

    async def test_an_overwrite_denying_it_here_counts(self):
        person = FakeMember(self.guild, "p", self.guild.role("R", 3, "manage_channels", "manage_messages"))
        denied = self.Scoped(self.guild, "denied", person, "view_channel")
        self.assertFalse(self.decide(person, denied, "manage_channels").allowed)
        self.assertFalse(self.decide(person, denied, "manage_messages").allowed)

    async def test_a_grant_on_one_channel_counts(self):
        person = FakeMember(self.guild, "p", self.guild.role("R", 3))
        granted = self.Scoped(self.guild, "granted", person, "manage_channels", "manage_messages")
        self.assertTrue(self.decide(person, granted, "manage_channels").allowed)
        self.assertFalse(self.decide(person, self.channel, "manage_channels").allowed)

    async def test_a_server_wide_permission_stays_server_wide(self):
        person = FakeMember(self.guild, "p", self.guild.role("R", 3, "ban_members"))
        denied = self.Scoped(self.guild, "denied", person, "view_channel")
        self.assertTrue(self.decide(person, denied, "ban_members").allowed)


class LockoutTest(BotTest):
    modules = [Config]

    async def rules(self, **form):
        await self.bot.settings.save(self.guild.id, "commands", form)

    def may(self, name, member, channel=None):
        return self.bot.command_decision(self.bot.get_command(name), member, channel or self.channel).allowed

    async def test_the_owner_and_administrators_keep_config_even_when_its_off(self):
        await self.rules(overwrites={"config": {"enabled": False}, "dashboard": {"enabled": False}, "my_data": {"enabled": False}})
        for person in (self.owner, self.admin):
            for name in ("config", "dashboard", "my_data"):
                self.assertTrue(self.may(name, person), (name, person.name))
        self.assertTrue(self.may("config enable", self.admin))

    async def test_a_manager_below_administrator_doesnt_get_config_back(self):
        await self.rules(overwrites={"config": {"enabled": False}})
        manager = FakeMember(self.guild, "m", self.guild.role("M", 3, "manage_guild"))
        self.assertFalse(self.may("config", manager))

    async def test_rules_never_take_dashboard_or_my_data_from_anyone(self):
        await self.rules(mod_only=True, channel_bl=[self.channel.id], overwrites={"my_data": {"enabled": False, "mod_only": True}, "dashboard": {"role_bl": [self.role_member.id]}})
        self.assertTrue(self.may("my_data", self.member))
        self.assertTrue(self.may("dashboard", self.member))

    async def test_those_three_cant_be_turned_off_by_command(self):
        cog = self.cog(Config)
        for name in ("config", "dashboard", "my_data"):
            ctx = self.context(author=self.admin)
            await cog.config_disable.callback(cog, ctx, command=name)
            self.assertTrue(ctx.fails, name)
            self.assertNotIn(name, self.settings("commands")["overwrites"])

    async def test_another_command_still_can(self):
        cog = self.cog(Config)
        ctx = self.context(author=self.admin)
        await cog.config_disable.callback(cog, ctx, command="ping")
        self.assertFalse(self.settings("commands")["overwrites"]["ping"]["enabled"])

    async def test_the_commands_page_doesnt_list_them(self):
        client = await self.client()
        data = await (await client.get(self.path("/commands"))).json()
        names = {row["name"] for group in data["categories"] for row in group["commands"]}
        self.assertTrue({"ping", "help"} <= names)
        self.assertFalse({"config", "dashboard", "my_data"} & names)

    async def test_help_still_lists_them(self):
        names = {row["name"] for group in self.bot.command_catalog() for row in group["commands"]}
        self.assertTrue({"config", "dashboard", "my_data"} <= names)


async def _noop(ctx):
    pass


class HelpLengthTest(BotTest):
    modules = [Config]

    async def test_a_big_group_fits_a_message(self):
        cog = self.cog(Config)

        @commands.group(name="biggroup", description="Lots here")
        async def group(ctx):
            pass
        for index in range(90):
            group.add_command(commands.Command(_noop, name=f"sub{index}", description="y" * 40))
        self.bot.add_command(group)
        ctx = self.context(author=self.member)
        await cog.help.callback(cog, ctx, command="biggroup")
        sent = ctx.sent[-1]
        self.assertLessEqual(len(sent["content"] or ""), 2000)
        self.assertLessEqual(len(sent["embed"].description), 4096)
        self.assertIn("sub0", sent["embed"].description)

    @staticmethod
    async def _noop(ctx):
        pass


class SettingsLockTest(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.activity = SimpleNamespace(record=self.record)
        self.settings = Settings(self.db, self.activity)
        await self.settings.load()
        self.settings.register("x", Schema({"a": Field(False, boolean), "b": Field(False, boolean)}))

    async def record(self, *args, **kwargs):
        await asyncio.sleep(0)

    async def test_two_saves_at_once_keep_both_changes(self):
        await asyncio.gather(self.settings.save(1, "x", {"a": True}), self.settings.save(1, "x", {"b": True}))
        self.assertEqual(self.settings.get(1, "x"), {"a": True, "b": True})
        stored = await self.db.scalar("SELECT data FROM guild_settings")
        self.assertIn('"a": true', stored)
        self.assertIn('"b": true', stored)

    async def test_a_restore_beside_a_save_doesnt_lose_the_save(self):
        await asyncio.gather(self.settings.restore(1, "x", {"a": True, "b": False}), self.settings.save(1, "x", {"b": True}))
        self.assertEqual(self.settings.get(1, "x"), {"a": True, "b": True})

    async def test_a_listener_can_save_again_without_waiting_on_itself(self):
        async def listener(guild_id, new):
            if new["a"] and not new["b"]:
                await self.settings.save(guild_id, "x", {"b": True})

        self.settings.listen("x", listener)
        await asyncio.wait_for(self.settings.save(1, "x", {"a": True}), 5)
        self.assertEqual(self.settings.get(1, "x"), {"a": True, "b": True})

    async def test_a_listener_can_restore_too(self):
        async def listener(guild_id, new):
            if new["a"]:
                await self.settings.restore(guild_id, "x", {"a": False, "b": True})

        self.settings.listen("x", listener)
        await asyncio.wait_for(self.settings.restore(1, "x", {"a": True, "b": False}), 5)
        self.assertEqual(self.settings.get(1, "x"), {"a": False, "b": True})



class FormerCommandNameTest(DbCase):
    """A rule saved under a command's former name answers to its current name, read from the database or put back by a restore."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.activity = SimpleNamespace(record=self.record)
        await Settings(self.db, self.activity).load()
        saved = {"overwrites": {"clearwarns": {"bypass_roles": [994247288555831296]}}}
        await self.db.run("INSERT INTO guild_settings (guild_id, module, data, updated_at) VALUES (1, 'commands', ?, 0)", (json.dumps(saved),))
        self.settings = Settings(self.db, self.activity)
        await self.settings.load()
        self.settings.register("commands", SETTINGS_SCHEMA)

    async def record(self, *args, **kwargs):
        await asyncio.sleep(0)

    async def test_a_saved_rule_answers_to_the_current_name(self):
        overwrites = self.settings.get(1, "commands")["overwrites"]
        self.assertEqual((list(overwrites), overwrites["clearwarnings"]["bypass_roles"]), (["clearwarnings"], [994247288555831296]))

    async def test_a_restore_from_before_the_rename_answers_to_the_current_name(self):
        await self.settings.restore(2, "commands", {"overwrites": {"clearwarns": {"bypass_roles": [994247288555831297]}}})
        overwrites = self.settings.get(2, "commands")["overwrites"]
        self.assertEqual((list(overwrites), overwrites["clearwarnings"]["bypass_roles"]), (["clearwarnings"], [994247288555831297]))

class LogSinkEdgesTest(unittest.IsolatedAsyncioTestCase):
    def sink(self, channel=None, settled=True):
        event = asyncio.Event()
        if settled:
            event.set()
        bot = SimpleNamespace(get_channel=lambda channel_id: channel, user=None, members_settled=event)
        return LogSink(bot)

    async def test_a_batch_in_flight_when_close_cancels_goes_back_and_is_counted(self):
        sink = self.sink(channel=SimpleNamespace())
        sent = asyncio.Event()

        async def hang(channel_id, embeds, file):
            sent.set()
            await asyncio.Event().wait()

        sink._send = hang
        with mock.patch("captainguild.core.logsink.FLUSH_SECONDS", 0), mock.patch("captainguild.core.logsink.CLOSE_WAIT", 0.01):
            sink.post(1, {"title": "a"})
            sink.post(1, {"title": "b"})
            await sent.wait()
            with self.assertLogs("captainguild.core.logsink", level="WARNING") as logged:
                await sink.close()
        self.assertEqual(len(sink._queues[1]), 2)
        self.assertIn("2 log entries", logged.output[0])

    async def test_a_post_during_closing_is_counted(self):
        sink = self.sink()
        sink._closing = True
        sink.post(1, {"title": "late"})
        with self.assertLogs("captainguild.core.logsink", level="WARNING") as logged:
            await sink.close()
        self.assertIn("1 log entries", logged.output[0])

    async def test_a_channel_missing_during_the_refill_waits(self):
        sink = self.sink(channel=None, settled=False)
        with self.assertRaises(Exception) as caught:
            await sink._send(1, [{"title": "a"}], None)
        self.assertTrue(type(caught.exception).__name__ == "ChannelNotBack")
        from captainguild.core.logsink import _worth_retrying
        self.assertTrue(_worth_retrying(caught.exception))

    async def test_a_channel_thats_truly_gone_says_so_once(self):
        sink = self.sink(channel=None, settled=True)
        with self.assertLogs("captainguild.core.logsink", level="WARNING") as logged:
            await sink._send(1, [{"title": "a"}], None)
            await sink._send(1, [{"title": "b"}], None)
        self.assertEqual(len(logged.records), 1)

    async def test_one_entry_that_cant_be_measured_doesnt_stall_the_queue(self):
        sink = self.sink(channel=SimpleNamespace())
        got = []
        done = asyncio.Event()

        async def send(channel_id, embeds, file):
            got.extend(embeds)
            done.set()

        sink._send = send
        with mock.patch("captainguild.core.logsink.FLUSH_SECONDS", 0):
            sink.post(1, {"fields": [5]})
            sink.post(1, {"title": "good"})
            with self.assertLogs("captainguild.core.logsink", level="WARNING"):
                await asyncio.wait_for(done.wait(), 5)
        self.assertEqual(got, [{"title": "good"}])

    def test_a_null_title_measures_as_empty(self):
        self.assertEqual(messages.embed_size({"title": None, "description": None, "footer": None, "author": None}), 0)


class AddressTest(unittest.TestCase):
    def test_addresses_inside_other_forms_are_judged_as_what_they_hold(self):
        for address in ("64:ff9b::a9fe:a9fe", "64:ff9b::7f00:1", "::127.0.0.1", "::169.254.169.254", "fec0::1", "ff02::1", "224.0.0.1", "239.255.255.250", "::ffff:127.0.0.1", "2002:7f00:1::"):
            self.assertFalse(_is_public(address), address)

    def test_public_addresses_stay_public(self):
        for address in ("8.8.8.8", "2606:4700:4700::1111", "64:ff9b::808:808", "::ffff:8.8.8.8"):
            self.assertTrue(_is_public(address), address)

    def test_a_link_to_one_is_refused(self):
        for link in ("http://[64:ff9b::7f00:1]/", "http://224.0.0.1/", "http://[::127.0.0.1]/"):
            with self.assertRaises(OutsideError):
                check_public_link(link)


class MessageInputTest(unittest.TestCase):
    def test_a_timestamp_that_cant_be_sent_is_refused(self):
        for value in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00", "1960-01-01T00:00:00+00:00", "3001-01-01T00:00:00+00:00"):
            with self.assertRaises(Invalid, msg=value):
                messages.clean_embed({"title": "x", "timestamp": value})

    def test_a_normal_timestamp_comes_out_in_utc(self):
        clean = messages.clean_embed({"title": "x", "timestamp": "2026-11-06T10:00:00+02:00"})
        self.assertEqual(clean["timestamp"], "2026-11-06T08:00:00+00:00")
        discord.Embed.from_dict(clean).to_dict()

    def test_components_of_the_wrong_shape_are_ignored(self):
        self.assertEqual(messages.from_json({"content": "hi", "components": [{"components": 5}]})["content"], "hi")


class DurationTest(unittest.TestCase):
    def test_a_long_run_of_digits_isnt_searched(self):
        with mock.patch("captainguild.core.durations._PART") as searched:
            self.assertIsNone(parse_duration("1" * 4000 + "!"))
        searched.finditer.assert_not_called()

    def test_ordinary_lengths_still_read(self):
        self.assertEqual(parse_duration("1w 3d"), 864000)


def png(width: int, height: int) -> bytes:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\0" * 10)) + chunk(b"IEND", b"")


class ImageLimitsTest(unittest.TestCase):
    def test_a_stored_picture_past_the_limit_isnt_decoded(self):
        with self.assertRaises(ValueError):
            rankcard._open(png(images.MAX_SIDE + 1, 64))

    def test_a_format_the_bot_doesnt_use_isnt_opened(self):
        from PIL import Image
        out = io.BytesIO()
        Image.new("RGB", (64, 64)).save(out, format="BMP")
        with self.assertRaises(images.READ_ERRORS):
            rankcard._open(out.getvalue())
        with self.assertRaises(Invalid):
            images.check(out.getvalue())

    def test_a_card_with_an_oversized_background_still_draws(self):
        card = rankcard.draw(name="a", level=1, rank=1, into_level=1, level_size=2, background=png(images.MAX_SIDE + 1, 64))
        self.assertTrue(card.startswith(b"\x89PNG"))


class FakeReader:
    def __init__(self, parts):
        self.parts = list(parts)

    async def next(self):
        return self.parts.pop(0) if self.parts else None


class FormLimitsTest(unittest.IsolatedAsyncioTestCase):
    def request(self, parts):
        async def multipart():
            return FakeReader(parts)
        return SimpleNamespace(content_type="multipart/form-data", multipart=multipart)

    def part(self, name, size, filename="x.png"):
        from aiohttp import BodyPartReader
        data = [b"x" * size, b""]

        class Part(BodyPartReader):
            def __init__(self):
                self.name = name
                self.filename = filename

            name = None
            filename = None

            async def read_chunk(self, length=0):
                return data.pop(0)
        Part.name = name
        Part.filename = filename
        return object.__new__(Part)

    async def test_the_whole_form_has_a_limit(self):
        parts = [self.part(f"f{index}", 7 * 1024 * 1024) for index in range(3)]
        with self.assertRaises(Invalid):
            await images.read_form(self.request(parts))

    async def test_the_number_of_parts_has_a_limit(self):
        parts = [self.part(f"f{index}", 1) for index in range(images.MAX_PARTS + 1)]
        with self.assertRaises(Invalid):
            await images.read_form(self.request(parts))

    async def test_what_a_page_sends_passes(self):
        files, fields = await images.read_form(self.request([self.part("avatar", 7 * 1024 * 1024), self.part("banner", 7 * 1024 * 1024), self.part("bio", 100, filename=None)]))
        self.assertEqual(set(files), {"avatar", "banner"})
        self.assertEqual(set(fields), {"bio"})


class MyDataExportTest(BotTest):
    modules = [MyData]

    async def test_the_download_doesnt_name_the_moderator(self):
        await self.message_db.run("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, guild_id INTEGER, author_id INTEGER, content TEXT, created_at REAL, deleted_by INTEGER)")
        await self.message_db.run("INSERT INTO messages (id, guild_id, author_id, content, created_at, deleted_by) VALUES (1, ?, ?, 'hi', 1.0, ?)", (self.guild.id, self.member.id, self.mod.id))
        cog = self.cog(MyData)
        for section in list(cog.export.__globals__["SECTIONS"]):
            for table, column in section.tables:
                await self.db.run(f"CREATE TABLE IF NOT EXISTS {table} ({column} INTEGER, guild_id INTEGER)")
        out = await cog.export(self.member.id)
        row = out["tables"]["messages"][0]
        self.assertEqual(row["content"], "hi")
        self.assertNotIn("deleted_by", row)


class PublicUrlTest(unittest.TestCase):
    def test_a_trailing_slash_comes_off(self):
        self.assertEqual(program.clean_public_url("https://x.example/"), "https://x.example")

    def test_a_value_with_no_scheme_is_refused(self):
        for value in ("x.example", "ftp://x.example", ""):
            with self.assertRaises(ValueError):
                program.clean_public_url(value)


class IntentsTest(unittest.IsolatedAsyncioTestCase):
    def client(self, **flags):
        class Client:
            def __init__(self, intents):
                pass

            async def login(self, token):
                pass

            async def application_info(self):
                return SimpleNamespace(flags=SimpleNamespace(**flags))

            async def close(self):
                pass
        return Client

    async def test_no_server_members_stops_before_the_site_starts(self):
        flags = dict(gateway_message_content=True, gateway_message_content_limited=False, gateway_guild_members=False, gateway_guild_members_limited=False)
        with mock.patch.object(program.discord, "Client", self.client(**flags)):
            with self.assertRaises(discord.PrivilegedIntentsRequired):
                await program.can_read_message_text("token")

    async def test_the_limited_flag_counts(self):
        flags = dict(gateway_message_content=False, gateway_message_content_limited=False, gateway_guild_members=False, gateway_guild_members_limited=True)
        with mock.patch.object(program.discord, "Client", self.client(**flags)):
            self.assertFalse(await program.can_read_message_text("token"))
