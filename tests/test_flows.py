"""The bot under load and across restarts: reconnects during a refill, shutdowns that have to finish, and work that mustn't stall the event loop."""
import asyncio
import itertools
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import aiohttp
import discord

from aiohttp import web

from captainguild import __main__ as launcher
from captainguild.core import delivery, timers
from captainguild.core import members as member_lists
from captainguild.core.tagscript import Interpreter, LimitReached, RegexGuard, Scope
from captainguild.core.tagscript import engine
from captainguild.core.tagscript.text import OUT_OF_TIME
from captainguild.modules.moderation import Moderation
from tests.gateway import FakeHTTPResponse, GatewayTest, iso, new_id, user_json


class RefillTest(GatewayTest):
    modules = []

    def waiting_on_chunks(self) -> list:
        self.guild._member_count = len(self.guild.members) + 5000
        calls: list = []

        async def chunker(guild_id, query="", limit=0, presences=False, *, nonce=None):
            calls.append(nonce)

        self.bot._connection.chunker = chunker
        return calls

    async def test_a_resume_during_the_refill_keeps_the_timers_paused(self):
        calls = self.waiting_on_chunks()
        await self.bot.on_connect()
        ready = asyncio.ensure_future(self.bot.on_ready())
        await asyncio.sleep(0.01)
        await self.bot.on_resumed()
        await asyncio.sleep(0.05)
        self.assertEqual(len(calls), 2, "the list was asked for again")
        self.assertFalse(member_lists._looks_whole(self.guild))
        self.assertFalse(self.bot.members_settled.is_set(), "half a list still counts as being refilled")
        self.assertFalse(self.bot.timers._unpaused.is_set())
        self.guild._member_count = len(self.guild.members)
        member_lists.forget_chunk_request(self.guild)
        await asyncio.wait_for(ready, 1)
        self.assertTrue(self.bot.members_settled.is_set())
        self.assertTrue(self.bot.timers._unpaused.is_set())
        self.bot._member_list_task.cancel()


class RolePingTest(GatewayTest):
    """A role members can't ping, in a channel where the bot holds no Mention Everyone."""

    modules = []

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.role = self.guild.get_role(int(self.role_member["id"]))
        self.channel = self.guild.get_channel(int(self.text["id"]))
        self.edits: list[bool] = []
        self.inner = self.bot.http.request
        self.bot.http.request = self.request
        limits = mock.patch.object(discord.abc.GuildChannel, "permissions_for", return_value=discord.Permissions.none())
        limits.start()
        self.addCleanup(limits.stop)

    async def request(self, route, **kwargs):
        if route.method == "PATCH" and "/roles/" in route.url:
            self.edits.append(kwargs["json"]["mentionable"])
        return await self.inner(route, **kwargs)

    def turning_back_does(self, effect) -> None:
        """What happens when the bot turns a role's mentions back off. Anything else goes through."""
        async def request(route, **kwargs):
            if route.method == "PATCH" and "/roles/" in route.url and kwargs["json"]["mentionable"] is False:
                await effect()
            return await self.request(route, **kwargs)

        self.bot.http.request = request

    async def ping(self) -> bool:
        async with delivery.role_pings(self.db, self.guild, self.channel, [self.role.id]) as ready:
            return ready

    async def held(self) -> int:
        return await self.db.scalar("SELECT COUNT(*) FROM kv WHERE key LIKE 'role_mentionable:%'")

    async def test_a_lost_connection_turning_it_back_off_doesnt_hold_the_lock_for_good(self):
        async def lost():
            raise aiohttp.ClientOSError("connection lost")

        self.turning_back_does(lost)
        with mock.patch.object(delivery, "TURN_BACK_WAITS", ()):
            await self.ping()
        self.assertEqual(await self.held(), 1, "the next start or send still knows to turn it off")
        self.bot.http.request = self.request
        self.assertTrue(await asyncio.wait_for(self.ping(), 2), "the next announcement to this role gets its turn")

    async def test_a_cancel_while_turning_it_back_off_lets_go_of_the_lock(self):
        forever, reached = asyncio.Event(), asyncio.Event()

        async def stuck() -> None:
            reached.set()
            await forever.wait()

        self.turning_back_does(stuck)
        first = asyncio.ensure_future(self.ping())
        await asyncio.wait_for(reached.wait(), 5)  # cancelled while turning it back off, however busy the machine is
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        self.assertEqual(await self.held(), 1)
        self.bot.http.request = self.request
        await asyncio.wait_for(self.ping(), 2)

    async def test_a_failure_turning_it_back_off_tries_again(self):
        tries: list[bool] = []

        async def outage():
            tries.append(True)
            if len(tries) < 3:
                raise discord.DiscordServerError(FakeHTTPResponse(500), {"code": 0, "message": "oops"})

        self.turning_back_does(outage)
        with mock.patch.object(delivery, "TURN_BACK_WAITS", (0, 0)):
            await self.ping()
        self.assertEqual(len(tries), 3)
        self.assertEqual(await self.held(), 0)

    async def test_a_role_left_open_is_closed_by_the_next_send(self):
        await self.db.run("INSERT INTO kv (key, value) VALUES (?, '1')", (delivery.HELD_KEY.format(self.guild.id, self.role.id),))
        self.role.mentionable = True
        self.assertTrue(await self.ping())
        self.assertEqual(self.edits, [False])
        self.assertEqual(await self.held(), 0)

    async def test_a_second_alert_right_after_the_first_opens_the_role_again(self):
        """Discord's update for the first edit reaches the cache right away, and its update for the turn back hasn't yet."""
        async def request(route, **kwargs):
            result = await self.request(route, **kwargs)
            if route.method == "PATCH" and "/roles/" in route.url and kwargs["json"]["mentionable"] is True:
                self.bot._connection.parse_guild_role_update({"guild_id": str(self.guild.id), "role": {**self.role_member, "mentionable": True}})
            return result

        self.bot.http.request = request
        await self.ping()
        self.assertTrue(self.guild.get_role(self.role.id).mentionable, "the cache is behind Discord")
        await self.ping()
        self.assertEqual(self.edits, [True, False, True, False])


class StopTest(GatewayTest):
    modules = []

    async def test_a_handler_stuck_in_its_cleanup_doesnt_hold_up_a_stop(self):
        started = asyncio.Event()
        forever = asyncio.Event()

        async def handler(payload, row):
            try:
                started.set()
                await forever.wait()
            finally:
                await forever.wait()  # a long wait on Discord's rate limit, in the code that puts things back

        self.bot.timers.handler("flow.stuck", handler)
        await self.bot.timers.schedule("flow.stuck", 0, {})
        self.bot.timers.start()
        await asyncio.wait_for(started.wait(), 5)
        with mock.patch.object(timers, "STOP_WAIT", 0.05):
            await asyncio.wait_for(self.bot.timers.stop(), 2)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'flow.stuck'"), 1, "the row stays for the next start")


class TemplateClockTest(unittest.TestCase):
    def test_waiting_for_a_turn_on_a_busy_bot_doesnt_use_up_a_templates_time(self):
        class Slow(SimpleNamespace):
            @property
            def display_name(self):
                time.sleep(0.3)  # this thread isn't working, as when the event loop and another template have the interpreter
                return "someone"

        self.assertEqual(Interpreter(Scope(member=Slow())).render("{user}{=(x):1}{x}"), "someone1")

    def test_a_template_that_really_uses_its_time_still_stops(self):
        ticks = itertools.count(0, 0.1)
        with mock.patch.object(engine.time, "thread_time", lambda: next(ticks)):
            with self.assertRaisesRegex(LimitReached, OUT_OF_TIME):
                Interpreter(Scope()).render("{=(a):1}" * 10)


class CloseTest(GatewayTest):
    modules = []

    async def test_a_ready_during_a_stop_doesnt_start_the_timers(self):
        self.bot._ready = asyncio.Event()
        self.bot._timer_start = asyncio.ensure_future(self.bot._start_timers())
        await asyncio.sleep(0)
        await self.bot.timers.stop()
        await self.bot.close()
        self.bot._ready.set()
        await asyncio.sleep(0.01)
        self.assertIsNone(self.bot.timers._task)


class WebStopTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_page_stuck_on_discord_doesnt_hold_up_a_stop(self):
        entered = asyncio.Event()

        async def stuck(request):
            entered.set()
            await asyncio.Event().wait()

        app = web.Application()
        app.router.add_get("/", stuck)
        with mock.patch.object(launcher, "WEB_STOP_WAIT", 0.05):
            runner = launcher.site_runner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as session:
            request = asyncio.ensure_future(session.get(f"http://127.0.0.1:{port}/"))
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(runner.cleanup(), 2)
            await asyncio.gather(request, return_exceptions=True)


class MassbanFilterTest(GatewayTest):
    modules = [Moderation]

    async def test_a_filter_over_a_big_server_lets_everything_else_run(self):
        state = self.bot._connection
        for number in range(5000):
            data = {"user": user_json(new_id(), f"person{number}"), "roles": [], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "premium_since": None, "avatar": None, "communication_disabled_until": None}
            self.guild._add_member(discord.Member(data=data, guild=self.guild, state=state))
        self.guild._member_count = len(self.guild.members)
        turns = 0

        async def other_work():
            nonlocal turns
            while True:
                turns += 1
                await asyncio.sleep(0)

        other = asyncio.ensure_future(other_work())
        await asyncio.sleep(0)
        before = turns
        moderation = self.bot.module("moderation")
        matched, _ = await moderation._ban_matches(SimpleNamespace(guild=self.guild, author=self.guild.get_member(self.owner_id)), {"noroles": ""})
        other.cancel()
        self.assertGreater(len(matched), 4000)
        self.assertGreaterEqual(turns - before, 2)


class HoldTest(GatewayTest):
    modules = []

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.ran: list[int | None] = []

        async def handler(payload, row):
            self.ran.append(row["guild_id"])

        self.bot.timers.handler("flow.held", handler)
        self.bot.timers.server_here = lambda guild_id: True

    async def test_a_server_on_hold_doesnt_hold_up_the_others(self):
        for _ in range(250):
            await self.bot.timers.schedule("flow.held", 1, {}, guild_id=1)
        await self.bot.timers.schedule("flow.held", 2, {}, guild_id=2)
        self.bot.timers.hold(1)
        await self.bot.timers.run_due()
        self.assertEqual(self.ran, [2])
        self.bot.timers.release(1)
        await self.bot.timers.run_due()
        self.assertEqual(self.ran.count(1), 200)

    async def test_the_loop_doesnt_keep_waking_for_a_server_on_hold(self):
        await self.bot.timers.schedule("flow.held", 1, {}, guild_id=1)
        self.bot.timers.hold(1)
        with mock.patch.object(self.bot.timers, "run_due", wraps=self.bot.timers.run_due) as looks:
            self.bot.timers.start()
            await asyncio.sleep(0.3)
            await self.bot.timers.stop()
        self.assertLessEqual(looks.call_count, 2)
        self.assertEqual(self.ran, [])


class RegexCloseTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_close_doesnt_wait_behind_every_search_in_line(self):
        guard = RegexGuard(time_limit=0.3)
        await guard.search("x", "x")
        waiting = [asyncio.ensure_future(guard.search(r"(a+)+$", "a" * 40 + str(number) + "!", 1)) for number in range(30)]
        await asyncio.sleep(0.05)
        began = time.monotonic()
        await guard.close()
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertEqual(await asyncio.gather(*waiting), [False] * 30)


class ReleaseTest(GatewayTest):
    modules = [Moderation]

    async def test_someone_missing_from_a_short_member_list_is_still_let_go(self):
        role_id = int(self.role_muted["id"])
        await self.db.run("INSERT INTO raid_holds (guild_id, user_id, user_name, role_id, held_at) VALUES (?, ?, ?, ?, ?)", (self.guild.id, self.member_id, "member", role_id, 1.0))
        self.member_json[self.member_id]["roles"].append(self.role_muted["id"])
        self.guild._remove_member(self.guild.get_member(self.member_id))
        self.guild._member_count = 5000
        released, failed = await self.bot.module("moderation").release_held(self.guild, "test")
        self.assertEqual((released, failed), (1, 0))
        self.assertTrue(any(method == "DELETE" and path.endswith("/roles/{role_id}") for method, path, _ in self.net.calls), "the role came off")
