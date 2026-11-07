"""What templates may spend: each server's pools, each person's share, how many run at once, and what happens to a template that keeps running out of time."""
from __future__ import annotations

import asyncio
import itertools
import re
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from captainguild.core.tagscript import LimitReached, Scope, budget, render_message, run_tag
from captainguild.core.tagscript import runner as runner_module
from captainguild.core.tagscript.text import OUT_OF_TIME
from captainguild.modules.notifications import Notifications, filled_alert_text, free_game_payload
from captainguild.modules.tags import Tags
from captainguild.modules.triggers import NORMAL, Triggers
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, FakeMessage

MODULES = Path(__file__).resolve().parent.parent / "captainguild" / "modules"
PLAIN = {"content": "Hello", "embeds": [], "buttons": []}


def ticking():
    """A processor clock that always moves. On some systems the real one reads the same across a short run."""
    ticks = itertools.count(0, 0.01)
    return mock.patch.object(budget.time, "thread_time", lambda: next(ticks))


def out_of_time(payload, scope):
    raise LimitReached(OUT_OF_TIME)


class BudgetCase(BotTest):
    modules = [Tags, Triggers]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.addAsyncCleanup(self.cog(Triggers).guard.close)
        self.other = FakeMember(self.guild, "other", self.role_member)
        self.third = FakeMember(self.guild, "third", self.role_member)

    def scope(self, member=None, args=""):
        return Scope(guild=self.guild, member=member or self.member, channel=self.channel, args=args)

    def spend(self, pool=None, member=None, seconds=100.0):
        budget.charge(self.guild.id, pool, member.id if member else None, seconds)


class ShareTest(BudgetCase):
    async def test_one_member_spends_their_share_and_everyone_else_still_runs(self):
        self.spend(member=self.member, seconds=budget.MEMBER_SECONDS)
        mine = await run_tag(self.bot, PLAIN, self.scope(), owner_id=self.member.id)
        self.assertEqual((mine.ran, mine.too_big), (False, True))
        theirs = await run_tag(self.bot, PLAIN, self.scope(self.other), owner_id=self.other.id)
        self.assertTrue(theirs.ran)

    async def test_a_run_charges_the_person_as_well_as_the_server(self):
        with ticking():
            await run_tag(self.bot, {"content": "{=(a):b}{a}", "embeds": [], "buttons": []}, self.scope(), owner_id=self.member.id)
        self.assertGreater(budget.spent(self.guild.id, member_id=self.member.id), 0)
        self.assertGreater(budget.spent(self.guild.id, pool=budget.MEMBERS), 0)
        self.assertEqual(budget.spent(self.guild.id, member_id=self.other.id), 0)

    async def test_members_can_not_starve_staff_triggers(self):
        self.spend(budget.MEMBERS, seconds=budget.SERVER_SECONDS)
        tag = await run_tag(self.bot, PLAIN, self.scope(self.other), owner_id=self.other.id)
        self.assertEqual((tag.ran, tag.too_big), (False, True))
        trigger = await run_tag(self.bot, PLAIN, self.scope(self.other), staff_wrote=True)
        self.assertTrue(trigger.ran)
        trusted = await run_tag(self.bot, PLAIN, self.scope(self.other), owner_id=self.admin.id, trusted=True)
        self.assertTrue(trusted.ran)

    async def test_a_member_s_share_holds_on_a_staff_trigger_too(self):
        self.spend(member=self.member, seconds=budget.MEMBER_SECONDS)
        self.assertFalse((await run_tag(self.bot, PLAIN, self.scope(), staff_wrote=True)).ran)
        self.assertTrue((await run_tag(self.bot, PLAIN, self.scope(self.other), staff_wrote=True)).ran)

    async def test_a_share_comes_back_with_the_next_minute(self):
        self.spend(member=self.member, seconds=budget.MEMBER_SECONDS)
        key = next(key for key in budget._spent if key[0] == self.guild.id)
        budget._spent[key] = (time.monotonic() - budget.WINDOW - 1, budget.MEMBER_SECONDS)
        self.assertTrue((await run_tag(self.bot, PLAIN, self.scope(), owner_id=self.member.id)).ran)

    async def test_old_windows_are_forgotten(self):
        with mock.patch.object(budget, "MAX_WINDOWS", 2):
            old = time.monotonic() - budget.WINDOW - 1
            budget._spent[(self.guild.id, "old", 1)] = (old, 1.0)
            budget._spent[(self.guild.id, "old", 2)] = (old, 1.0)
            budget._spent[(self.guild.id, "old", 3)] = (old, 1.0)
            budget.charge(self.guild.id, budget.MEMBERS, None, 0.1)
            self.assertFalse([key for key in budget._spent if key[1] == "old"])

    async def test_over_budget_a_typed_tag_gets_the_warning_reaction(self):
        self.spend(member=self.member, seconds=budget.MEMBER_SECONDS)
        ctx = self.context(author=self.member)
        await run_tag(self.bot, PLAIN, self.scope(), ctx=ctx, owner_id=self.member.id)
        self.assertEqual(ctx.message.added_reactions, [runner_module.WARNING_EMOJI])
        self.assertEqual(ctx.sent, [])


class AtOnceTest(BudgetCase):
    async def test_two_renders_run_at_a_time(self):
        release = threading.Event()
        started = threading.Semaphore(0)
        real = runner_module.render_strict

        def held(payload, scope):
            started.release()
            release.wait(10)
            return real(payload, scope)

        with mock.patch.object(runner_module, "render_strict", held):
            runs = [asyncio.ensure_future(run_tag(self.bot, PLAIN, self.scope(FakeMember(self.guild, f"m{i}")), trusted=True)) for i in range(8)]
            await asyncio.sleep(0)
            for _ in range(budget.RUN_SLOTS):
                await asyncio.to_thread(started.acquire)
            self.assertEqual(budget._gate().waiting, 8 - budget.RUN_SLOTS)
            release.set()
            results = await asyncio.gather(*runs)
        self.assertTrue(all(result.ran for result in results))

    async def test_past_the_queue_a_run_is_refused_rather_than_held(self):
        release = threading.Event()
        started = threading.Semaphore(0)
        real = runner_module.render_strict

        def held(payload, scope):
            started.release()
            release.wait(10)
            return real(payload, scope)

        with mock.patch.object(runner_module, "render_strict", held):
            total = budget.RUN_SLOTS + budget.MAX_WAITING + 5
            runs = [asyncio.ensure_future(run_tag(self.bot, PLAIN, self.scope(FakeMember(self.guild, f"m{i}")), trusted=True)) for i in range(total)]
            await asyncio.sleep(0)
            for _ in range(budget.RUN_SLOTS):
                await asyncio.to_thread(started.acquire)
            for _ in range(5):
                await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(*runs)
        self.assertEqual(sum(1 for result in results if result.too_big), 5)
        self.assertEqual(sum(1 for result in results if result.ran), total - 5)

    async def test_a_budget_that_closed_during_the_wait_stops_the_run(self):
        release = threading.Event()
        started = threading.Semaphore(0)
        real = runner_module.render_strict

        def held(payload, scope):
            started.release()
            release.wait(10)
            return real(payload, scope)

        with mock.patch.object(runner_module, "render_strict", held):
            front = [asyncio.ensure_future(run_tag(self.bot, PLAIN, self.scope(FakeMember(self.guild, f"m{i}")), staff_wrote=True)) for i in range(budget.RUN_SLOTS)]
            await asyncio.sleep(0)
            for _ in range(budget.RUN_SLOTS):
                await asyncio.to_thread(started.acquire)
            waiting = asyncio.ensure_future(run_tag(self.bot, PLAIN, self.scope(self.other), owner_id=self.other.id))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.spend(budget.MEMBERS, seconds=budget.SERVER_SECONDS)
            release.set()
            await asyncio.gather(*front)
            result = await waiting
        self.assertEqual((result.ran, result.too_big), (False, True))


class BenchTest(BudgetCase):
    def stopped(self):
        return mock.patch.object(runner_module, "render_strict", out_of_time)

    async def test_an_owner_who_runs_a_slow_tag_switches_it_off_for_everyone(self):
        payload = {"content": "slow one", "embeds": [], "buttons": []}
        with self.stopped():
            first = await run_tag(self.bot, payload, self.scope(), owner_id=self.member.id)
        self.assertEqual((first.too_big, first.benched), (True, True))
        later = await run_tag(self.bot, payload, self.scope(self.other, args="anything"), owner_id=self.member.id)
        self.assertEqual((later.ran, later.too_big, later.benched), (False, True, False))
        elsewhere = await run_tag(self.bot, PLAIN, self.scope(self.other), owner_id=self.member.id)
        self.assertTrue(elsewhere.ran, "another template carries on")

    async def test_one_member_s_crafted_text_can_not_switch_off_someone_else_s_tag(self):
        payload = {"content": "{replace(a,b):{args}}", "embeds": [], "buttons": []}
        with self.stopped():
            first = await run_tag(self.bot, payload, self.scope(args="crafted"), owner_id=self.admin.id)
        self.assertFalse(first.benched)
        self.assertTrue((await run_tag(self.bot, payload, self.scope(self.other, args="fine"), owner_id=self.admin.id)).ran)
        repeat = await run_tag(self.bot, payload, self.scope(args="crafted"), owner_id=self.admin.id)
        self.assertEqual((repeat.ran, repeat.too_big), (False, True), "the same text stays stopped, so repeating it costs nothing")

    async def test_three_people_running_out_of_time_switch_it_off(self):
        payload = {"content": "shared slow", "embeds": [], "buttons": []}
        with self.stopped():
            results = [await run_tag(self.bot, payload, self.scope(person, args=str(index)), staff_wrote=True) for index, person in enumerate((self.member, self.other, self.third))]
        self.assertEqual([result.benched for result in results], [False, False, True])
        self.assertFalse((await run_tag(self.bot, payload, self.scope(self.admin, args="new"), staff_wrote=True)).ran)

    async def test_the_same_person_three_times_counts_once(self):
        payload = {"content": "one person", "embeds": [], "buttons": []}
        with self.stopped():
            results = [await run_tag(self.bot, payload, self.scope(args=str(index)), staff_wrote=True) for index in range(3)]
        self.assertEqual([result.benched for result in results], [False, False, False])

    async def test_a_bench_ends(self):
        payload = {"content": "comes back", "embeds": [], "buttons": []}
        with self.stopped():
            await run_tag(self.bot, payload, self.scope(), owner_id=self.member.id)
        self.assertFalse((await run_tag(self.bot, payload, self.scope(), owner_id=self.member.id)).ran)
        for key in list(budget._benched):
            budget._benched[key] = time.monotonic() - 1
        self.assertTrue((await run_tag(self.bot, payload, self.scope(), owner_id=self.member.id)).ran)

    async def test_too_much_text_is_not_a_bench(self):
        def too_long(payload, scope):
            raise LimitReached("too much text")

        payload = {"content": "wordy", "embeds": [], "buttons": []}
        with mock.patch.object(runner_module, "render_strict", too_long):
            result = await run_tag(self.bot, payload, self.scope(), owner_id=self.member.id)
        self.assertEqual((result.too_big, result.benched), (True, False))

    async def test_the_owner_hears_when_their_tag_is_switched_off(self):
        tags = self.cog(Tags)
        await tags._insert(self.guild.id, "slowone", {"content": "x", "embeds": [], "buttons": []}, owner_id=self.other.id, trusted=False)
        tag = await tags.find(self.guild.id, "slowone")
        tags._recent.clear()
        ctx = self.context(author=self.other)
        with self.stopped():
            await tags.use_tag(ctx, tag, "")
        self.assertEqual(len(self.other.dms), 1)
        self.assertIn("slowone", self.other.dms[0]["content"])
        ctx = self.context(author=self.other)
        await tags.use_tag(ctx, tag, "")
        self.assertEqual(len(self.other.dms), 1, "told once")

    async def test_staff_hear_when_a_trigger_is_switched_off(self):
        triggers = self.cog(Triggers)
        logs = mock.Mock()
        await triggers._insert(self.guild.id, {"match_type": NORMAL, "trigger": "hello", "message": {"content": "slow", "embeds": [], "buttons": []}, "channel_id": None, "role_wl": [], "role_bl": [], "channel_wl": [], "channel_bl": [], "cooldown": 0}, self.admin.id)
        with self.stopped(), mock.patch.object(self.bot, "module", lambda key: logs if key == "logging" else None):
            for index, person in enumerate((self.member, self.other, self.third)):
                await triggers._handle_message(FakeMessage(self.channel, person, f"hello {index}"))
        self.assertEqual(logs.staff_notice.call_count, 1)
        row = (await triggers.rows(self.guild.id))[0]
        self.assertTrue(triggers._api_row(row)["problem"])


class PreviewChargeTest(BudgetCase):
    async def test_previews_spend_from_a_budget(self):
        client = await self.client()
        for path in ("/tags/preview", "/triggers/preview"):
            before = budget.spent(self.guild.id, member_id=self.admin.id)
            with ticking():
                response = await client.post(self.path(path), json={"message": {"content": "{=(a):b}{a}"}})
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await response.json())["message"]["content"], "b")
            self.assertGreater(budget.spent(self.guild.id, member_id=self.admin.id), before, path)
        self.spend(member=self.admin, seconds=budget.MEMBER_SECONDS)
        for path in ("/tags/preview", "/triggers/preview"):
            answer = await (await client.post(self.path(path), json={"message": {"content": "{=(a):b}{a}"}})).json()
            self.assertTrue(answer["error"], path)

    async def test_previews_can_not_starve_real_runs(self):
        self.spend(budget.PREVIEWS, seconds=budget.SERVER_SECONDS)
        self.assertTrue((await run_tag(self.bot, PLAIN, self.scope(), staff_wrote=True)).ran)
        self.assertTrue((await run_tag(self.bot, PLAIN, self.scope(), owner_id=self.member.id)).ran)


class RenderMessageTest(BudgetCase):
    async def test_it_works_off_the_event_loop_and_charges_the_server(self):
        threads = []
        real = budget.render_payload

        def spy(payload, scope):
            threads.append(threading.current_thread())
            return real(payload, scope)

        with mock.patch.object(budget, "render_payload", spy), ticking():
            out = await render_message({"content": "{=(a):b}{a}", "embeds": [], "buttons": []}, self.scope())
        self.assertEqual(out["content"], "b")
        self.assertNotEqual(threads, [])
        self.assertIsNot(threads[0], threading.current_thread())
        self.assertGreater(budget.spent(self.guild.id, pool=budget.MESSAGES), 0)

    async def test_over_budget_the_template_goes_out_as_written(self):
        self.spend(budget.MESSAGES, seconds=budget.SERVER_SECONDS)
        out = await render_message({"content": "Hi {user}", "embeds": [], "buttons": []}, self.scope())
        self.assertEqual(out["content"], "Hi {user}")

    async def test_text_with_no_blocks_takes_no_thread(self):
        with mock.patch.object(budget.asyncio, "to_thread", side_effect=AssertionError("no thread needed")):
            out = await render_message({"content": "Hello", "embeds": [{"title": "T"}], "buttons": []}, self.scope())
        self.assertEqual((out["content"], out["embeds"][0]["title"]), ("Hello", "T"))

    async def test_messages_wait_their_turn_past_the_queue_limit(self):
        release = threading.Event()
        started = threading.Semaphore(0)
        real = budget.render_payload

        def held(payload, scope):
            started.release()
            release.wait(10)
            return real(payload, scope)

        with mock.patch.object(budget, "render_payload", held):
            runs = [asyncio.ensure_future(render_message({"content": "{=(a):b}{a}", "embeds": [], "buttons": []}, self.scope())) for _ in range(budget.MAX_WAITING + budget.RUN_SLOTS + 5)]
            await asyncio.sleep(0)
            for _ in range(budget.RUN_SLOTS):
                await asyncio.to_thread(started.acquire)
            release.set()
            results = await asyncio.gather(*runs)
        self.assertEqual({result["content"] for result in results}, {"b"}, "a raid's greetings wait rather than go out unfilled")

    async def test_one_server_s_messages_leave_another_s_alone(self):
        self.spend(budget.MESSAGES, seconds=budget.SERVER_SECONDS)
        other = FakeGuild("Another Server")
        out = await render_message({"content": "{=(a):b}{a}", "embeds": [], "buttons": []}, Scope(guild=other, channel=FakeChannel(other, "general")))
        self.assertEqual(out["content"], "b")


class AlertFormatTest(BudgetCase):
    modules = [Notifications]

    async def asyncSetUp(self):
        await BotTest.asyncSetUp(self)
        self.notifications = self.cog(Notifications)
        self.addAsyncCleanup(self.notifications.outside.close)
        await self.bot.settings.save(self.guild.id, "notifications", {"alert_channel": self.channel.id, "free_format": "{=(a):Free}{a} {title}"})

    async def deliver(self):
        cfg = self.notifications.cfg(self.guild.id)
        await self.db.run("INSERT INTO game_alert_state (guild_id, source, filter) VALUES (?, 'free', 'x')", (self.guild.id,))
        await self.notifications._deliver(self.guild.id, "free", "x", cfg, [("1", {"title": "Game", "open_giveaway_url": "https://example.com/game"})], free_game_payload, "free_format")

    async def test_a_game_alert_is_filled_in_off_the_loop(self):
        threads = []

        def spy(*args, **kwargs):
            threads.append(threading.current_thread())
            return free_game_payload(*args, **kwargs)

        cfg = self.notifications.cfg(self.guild.id)
        await self.db.run("INSERT INTO game_alert_state (guild_id, source, filter) VALUES (?, 'free', 'x')", (self.guild.id,))
        await self.notifications._deliver(self.guild.id, "free", "x", cfg, [("1", {"title": "Game", "open_giveaway_url": "https://example.com/game"})], spy, "free_format")
        self.assertIsNot(threads[0], threading.current_thread())
        self.assertEqual(self.channel.sent[0].content, "Free Game")

    async def test_past_the_budget_an_alert_waits_for_the_next_round(self):
        budget.charge(self.guild.id, budget.MESSAGES, None, budget.SERVER_SECONDS)
        await self.deliver()
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM game_alert_seen WHERE guild_id = ?", (self.guild.id,)), 0, "nothing is lost")

    async def test_a_stream_alert_s_format_goes_out_as_written_past_the_budget(self):
        budget.charge(self.guild.id, budget.MESSAGES, None, budget.SERVER_SECONDS)
        text = await filled_alert_text("Live: {title}", 5, guild_id=self.guild.id, values={"title": "x"})
        self.assertEqual(text, "<@&5> Live: {title}")

    async def test_a_stream_alert_s_format_is_filled_in_under_the_budget(self):
        text = await filled_alert_text("Live: {title}", None, guild_id=self.guild.id, values={"title": "x"})
        self.assertEqual(text, "Live: x")


class NoModuleRendersOnTheLoopTest(unittest.TestCase):
    def test_modules_only_render_through_the_budget(self):
        found = []
        for path in sorted(MODULES.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"(?<!lambda: )\b(render_payload|render_strict)\(", source):
                found.append(f"{path.name}: {match.group(0)}")
        self.assertEqual(found, [])

    def test_the_alert_builders_are_only_called_through_the_budget(self):
        source = (MODULES / "notifications.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"budgeted_render\(lambda: build\(")
        self.assertNotRegex(source, r"clean_payload\(build\(item")
        self.assertNotRegex(source, r"= alert_text\((row|sub)", "a stream or video alert's format is a template too")
