"""The worker that runs patterns servers wrote: a slow pattern costs little, and no member can switch one off for everyone."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from captainguild.core.tagscript import RegexGuard
from captainguild.core.tagscript import regexguard as regexguard_module
from captainguild.modules.automod import Automod
from tests.fakes import BotTest, FakeMessage

SLOW = r"(a+)+$|hello"
CRAFTED = "a" * 40 + "b"


class GuardCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guard = RegexGuard(time_limit=1.0)
        self.addAsyncCleanup(self.guard.close)
        self.assertTrue(await self.guard.search("warm", "warm up"), "the worker has started before anything is timed")


class OneMessageTest(GuardCase):
    async def test_a_crafted_message_leaves_the_pattern_on_for_other_text(self):
        self.assertFalse(await self.guard.search(SLOW, CRAFTED))
        self.assertTrue(await self.guard.search(SLOW, "well hello there"))

    async def test_the_same_text_again_costs_nothing(self):
        self.assertFalse(await self.guard.search(SLOW, CRAFTED))
        with mock.patch.object(self.guard, "_ask", side_effect=AssertionError("the worker was asked again")):
            self.assertFalse(await self.guard.search(SLOW, CRAFTED))

    async def test_that_text_comes_back_after_a_while(self):
        self.assertFalse(await self.guard.search(SLOW, CRAFTED))
        for key in list(self.guard._skip_until):
            self.guard._skip_until[key] = time.monotonic() - 1
        with mock.patch.object(self.guard, "_ask", return_value={"m": True}) as asked:
            self.assertTrue(await self.guard.search(SLOW, CRAFTED))
        asked.assert_called_once()

    async def test_the_page_can_tell_which_pattern_ran_out_of_time(self):
        self.assertFalse(self.guard.took_too_long(SLOW, 1))
        await self.guard.search(SLOW, CRAFTED, server=1)
        self.assertTrue(self.guard.took_too_long(SLOW, 1))
        self.assertFalse(self.guard.took_too_long(SLOW, 2), "another server's copy of the pattern is its own")

    async def test_the_notice_comes_once(self):
        await self.guard.search(SLOW, CRAFTED, server=1)
        self.assertTrue(self.guard.first_slow_notice(SLOW, 1))
        self.assertFalse(self.guard.first_slow_notice(SLOW, 1))


class VariedTextTest(GuardCase):
    async def test_time_lost_puts_the_server_on_a_short_clock(self):
        seen = []
        real = self.guard._ask

        async def spy(pattern, text, limit=None):
            seen.append(limit)
            return await real(pattern, text, limit)

        with mock.patch.object(self.guard, "_ask", spy):
            self.guard._lose_time(1, regexguard_module.SHORT_AFTER)
            await self.guard.search("x", "x", server=1)
            await self.guard.search("x", "x", server=2)
        self.assertEqual(seen, [min(self.guard.time_limit, regexguard_module.SHORT_LIMIT), self.guard.time_limit])

    async def test_innocent_text_still_matches_on_the_short_clock(self):
        self.guard._lose_time(1, regexguard_module.SHORT_AFTER)
        with mock.patch.object(regexguard_module, "SHORT_LIMIT", 0.9):  # a busy machine shouldn't decide this test
            self.assertTrue(await self.guard.search(SLOW, "hello", server=1))

    async def test_past_the_limit_one_server_s_patterns_wait_and_others_do_not(self):
        self.guard._lose_time(1, regexguard_module.STOP_AFTER)
        with mock.patch.object(self.guard, "_ask", side_effect=AssertionError("the worker was asked")):
            self.assertFalse(await self.guard.search("x", "x", server=1))
        self.assertTrue(self.guard.is_stopped(1))
        self.assertTrue(await self.guard.search("x", "x", server=2))

    async def test_the_stop_ends_with_the_minute(self):
        self.guard._lose_time(1, regexguard_module.STOP_AFTER)
        self.guard._lost[1] = (time.monotonic() - regexguard_module.SLOW_WINDOW - 1, regexguard_module.STOP_AFTER)
        self.assertFalse(self.guard.is_stopped(1))
        self.assertTrue(await self.guard.search("x", "x", server=1))

    async def test_a_timeout_counts_toward_the_limit(self):
        await self.guard.search(SLOW, CRAFTED, server=7)
        self.assertGreaterEqual(self.guard._lost[7][1], 0.1)

    async def test_what_it_remembers_stays_bounded(self):
        with mock.patch.object(regexguard_module, "MAX_REMEMBERED", 3):
            old = time.monotonic() - 1
            self.guard._skip_until.update({("p", bytes([n])): old for n in range(5)})
            self.guard._slow_until.update({(None, f"p{n}"): old for n in range(5)})
            await self.guard.search(SLOW, CRAFTED)
        self.assertLessEqual(len(self.guard._skip_until), 3)
        self.assertLessEqual(len(self.guard._slow_until), 3)

    async def test_when_nothing_has_run_out_the_oldest_goes(self):
        with mock.patch.object(regexguard_module, "MAX_REMEMBERED", 3):
            soon = time.monotonic() + 50
            self.guard._skip_until.update({("p", bytes([n])): soon + n for n in range(5)})
            await self.guard.search(SLOW, CRAFTED)
        self.assertLessEqual(len(self.guard._skip_until), 3)


class CancelTest(GuardCase):
    async def test_a_canceled_search_leaves_the_worker_in_step(self):
        slow = asyncio.ensure_future(self.guard.search(r"(a+)+$", CRAFTED))
        while self.guard._lock.locked() is False:
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        slow.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await slow
        self.assertTrue(await self.guard.search("a", "a"))
        self.assertFalse(await self.guard.search("b", "a"))
        self.assertTrue(await self.guard.search("a", "a"))


class AutomodTest(BotTest):
    modules = [Automod]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.automod = self.cog(Automod)
        self.automod.regex.time_limit = 1.0
        self.addAsyncCleanup(self.automod.regex.close)
        await self.bot.settings.save(self.guild.id, "automod", {"words": {"enabled": True, "entries": [{"text": SLOW, "kind": "regex"}]}})

    async def bad(self, text):
        compiled = self.automod._rules(self.guild.id)
        return await self.automod._bad_word(compiled, self.guild, text)

    async def test_one_crafted_message_does_not_let_the_next_one_through(self):
        self.assertIsNone(await self.bad(CRAFTED))
        self.assertIsNotNone(await self.bad("hello"), "the pattern still judges other text")

    async def test_staff_hear_once(self):
        with mock.patch.object(self.automod, "_log_slow_pattern") as told:
            await self.bad(CRAFTED)
            await self.bad(CRAFTED)
            await self.bad(CRAFTED + "c")
        told.assert_called_once()

    async def test_time_lost_is_charged_to_the_server(self):
        await self.bad(CRAFTED)
        self.assertIn(self.guild.id, self.automod.regex._lost)

    async def test_the_message_path_carries_on(self):
        FakeMessage(self.channel, self.member, CRAFTED)
        self.assertIsNotNone(await self.bad("say hello"))
