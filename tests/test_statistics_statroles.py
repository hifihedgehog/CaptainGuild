"""Stat roles: rules and their checks, schedules, who passes each kind of condition, what a run changes, and the run itself."""
import asyncio
import datetime as dt
import json
import re
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import discord

from captainguild.core.ops import DiscordOps
from captainguild.core.stats import statroles as sr
from captainguild.core.stats import store
from captainguild.core.stats import timeframe as tf
from captainguild.core.validate import Invalid
from captainguild.modules.backup import child_links, read_catalog, table_owners
from captainguild.modules.statistics import Statistics
from tests.fakes import BotTest, FakeChannel, FakeMember, FakePermissions

LA = tf.zone("America/Los_Angeles")
UTC = tf.zone("UTC")
DAY = 86400


def quiet(stats, guild_id):
    """Whether every run in the server is over, its worker and the bot's own record of it included."""
    state = stats._server_runs.get(guild_id)
    busy = state is not None and (state.runs or state.working_out or state.again or (state.worker is not None and not state.worker.done()))
    return not busy and not stats._tools_tasks


def leaf(kind="message", minimum=10, maximum=None, period=None, **extra):
    return {"type": kind, "limit": {"kind": "threshold", "min": minimum, "max": maximum, "period": period or {"amount": 30, "unit": "days"}}, **extra}


class Cron(unittest.TestCase):
    def test_fields_lists_ranges_steps_and_names(self):
        cron = sr.parse_cron("0,30 9-17/4 * jan-mar mon-fri")
        self.assertEqual((cron.minutes, cron.hours), ((0, 30), (9, 13, 17)))
        self.assertEqual(cron.months, frozenset({1, 2, 3}))
        self.assertEqual(cron.weekdays, frozenset({1, 2, 3, 4, 5}))
        self.assertEqual(sr.parse_cron("0 0 * * 7").weekdays, frozenset({0}))
        self.assertEqual(sr.parse_cron("0 0 * * 5-0").weekdays, frozenset({5, 6, 0}))
        self.assertEqual(sr.parse_cron("15/20 * * * *").minutes, (15, 35, 55))
        self.assertEqual(sr.parse_cron("@daily"), sr.parse_cron("0 0 * * *"))

    def test_both_day_fields_named_means_either(self):
        cron = sr.parse_cron("0 0 13 * fri")  # the 13th of any month, and every Friday
        self.assertTrue(cron.runs_on(dt.date(2026, 11, 13)))
        self.assertTrue(cron.runs_on(dt.date(2026, 11, 20)))
        self.assertTrue(cron.runs_on(dt.date(2026, 12, 13)))
        self.assertFalse(cron.runs_on(dt.date(2026, 11, 14)))
        starred = sr.parse_cron("0 0 */2 * fri")  # a day field starting with * makes both necessary
        self.assertTrue(starred.runs_on(dt.date(2026, 11, 13)))
        self.assertFalse(starred.runs_on(dt.date(2026, 11, 20)))

    def test_mistakes(self):
        for bad in ("* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "5-1 * * * *", "* * 0 * *", "* * * 13 *", "* * * * xyz", "1,,2 * * * *"):
            with self.assertRaises(Invalid, msg=bad):
                sr.parse_cron(bad)

    def test_a_clock_change_works_as_cronie_handles_it(self):
        # Clocks in Los Angeles jumped from 2:00 to 3:00 on March 8, 2026, and fell back from 2:00 to 1:00 on November 1.
        start = dt.datetime(2026, 3, 7, 12, tzinfo=LA).timestamp()
        fixed = sr.cron_next(sr.parse_cron("30 2 * * *"), start, LA)
        self.assertEqual(fixed, dt.datetime(2026, 3, 8, 10, tzinfo=dt.timezone.utc).timestamp(), "a fixed time the jump skips runs as the clock lands")
        self.assertEqual(sr.cron_next(sr.parse_cron("30 2 * * *"), fixed, LA), dt.datetime(2026, 3, 9, 2, 30, tzinfo=LA).timestamp())
        wildcard = sr.cron_next(sr.parse_cron("30 * * * *"), dt.datetime(2026, 3, 8, 1, 45, tzinfo=LA).timestamp(), LA)
        self.assertEqual(dt.datetime.fromtimestamp(wildcard, LA).hour, 3, "a schedule with * in the hour skips the missing hour")
        twice = sr.parse_cron("30 1 * * *")
        first = sr.cron_next(twice, dt.datetime(2026, 11, 1, 0, 0, tzinfo=LA).timestamp(), LA)
        self.assertEqual(sr.cron_next(twice, first, LA), dt.datetime(2026, 11, 2, 1, 30, tzinfo=LA).timestamp(), "1:30 happens twice and runs once")
        daily = sr.parse_cron("0 0 * * *")
        self.assertEqual(sr.cron_next(daily, dt.datetime(2026, 11, 8, 12, tzinfo=LA).timestamp(), LA), dt.datetime(2026, 11, 9, tzinfo=LA).timestamp())

    def test_a_schedule_with_a_star_runs_through_a_repeated_hour(self):
        # On November 1, 2026 in Los Angeles, 1:00 to 1:59 happens twice. cronie runs jobs with * in the minute or hour through the second
        # pass, and fixed-time jobs once.
        start = dt.datetime(2026, 11, 1, 1, 50, tzinfo=LA).timestamp()  # 1:50 PDT, the first time through
        self.assertEqual(sr.cron_next(sr.parse_cron("*/10 * * * *"), start, LA) - start, 600, "1:00 PST comes ten minutes later")
        hourly = sr.cron_next(sr.parse_cron("0 * * * *"), dt.datetime(2026, 11, 1, 1, 0, tzinfo=LA).timestamp(), LA)
        self.assertEqual(dt.datetime.fromtimestamp(hourly, LA).strftime("%H:%M %Z"), "01:00 PST")
        fixed = sr.parse_cron("30 1 * * *")
        first = sr.cron_next(fixed, dt.datetime(2026, 11, 1, 0, 0, tzinfo=LA).timestamp(), LA)
        self.assertEqual(sr.cron_next(fixed, first, LA), dt.datetime(2026, 11, 2, 1, 30, tzinfo=LA).timestamp(), "a fixed time still runs once")

    def test_no_more_often_than_every_ten_minutes(self):
        self.assertEqual(sr.cron_gap(sr.parse_cron("*/10 * * * *")), 10)
        self.assertEqual(sr.cron_gap(sr.parse_cron("0,55 0,23 * * *")), 5)  # 23:55 and then midnight
        self.assertEqual(sr.cron_gap(sr.parse_cron("55 23 * * 1")), 1440)
        self.assertIsNone(sr.cron_gap(sr.parse_cron("0 0 30 2 *")))
        self.assertEqual(sr.clean_schedule({"preset": "fridays"}), {"preset": "fridays", "cron": "0 0 * * 5"})
        self.assertEqual(sr.clean_schedule({"preset": "custom", "cron": " */15  * * * * "})["cron"], "*/15 * * * *")
        for bad in ({"preset": "custom", "cron": "*/5 * * * *"}, {"preset": "custom", "cron": "0 0 31 2 *"}, {"preset": "weekly"}):
            with self.assertRaises(Invalid, msg=str(bad)):
                sr.clean_schedule(bad)


class Rules(unittest.TestCase):
    def test_a_rule_with_its_defaults(self):
        rule = sr.clean_rule({"conditions": [leaf()]})
        self.assertEqual(rule["schedule"], {"preset": "3h", "cron": "0 */3 * * *"})
        self.assertEqual((rule["apply_to"], rule["permanent"], rule["give"], rule["remove"]), ("humans", False, None, None))
        self.assertEqual(sr.clean_rule(json.loads(json.dumps(rule))), rule)

    def test_limits(self):
        for bad in (leaf(minimum=0), {**leaf(), "limit": {"kind": "threshold", "min": 10, "max": 5}},
                    {**leaf(), "limit": {"kind": "top", "by": "rank", "from": 5, "to": 1}}, {**leaf(), "limit": {"kind": "top", "by": "percent", "from": 20, "to": 10}},
                    {**leaf(), "limit": {"kind": "days", "per_day": 5, "days": 8, "of": 7}}, {**leaf(), "limit": {"kind": "days", "per_day": 0, "days": 1, "of": 7}}):
            with self.assertRaises(Invalid, msg=str(bad)):
                sr.clean_rule({"conditions": [bad]})
        self.assertEqual(sr.clean_rule({"conditions": [leaf(minimum=0, maximum=0)]})["conditions"]["conditions"][0]["limit"]["max"], 0)

    def test_groups_need_a_condition_and_have_limits(self):
        with self.assertRaises(Invalid):
            sr.clean_rule({"conditions": [{"type": "match", "mode": "all", "conditions": []}]})
        with self.assertRaises(Invalid):
            sr.clean_rule({"conditions": {"type": "match", "mode": "all", "conditions": [{"type": "match", "conditions": [{"type": "match", "conditions": []}]}]}})
        deep = leaf()
        for _ in range(7):
            deep = {"type": "match", "mode": "any", "conditions": [deep]}
        with self.assertRaises(Invalid):
            sr.clean_rule({"conditions": [deep]})
        with self.assertRaises(Invalid):
            sr.clean_rule({"conditions": [leaf() for _ in range(sr.MAX_NODES)]})

    def test_loops_through_other_rules_are_found(self):
        rules = {1: sr.clean_rule({"conditions": [{"type": "statrole", "rule": 2}]}), 2: sr.clean_rule({"conditions": [{"type": "statrole", "rule": 3}]}),
                 3: sr.clean_rule({"conditions": [{"type": "statrole", "rule": 1}]}), 4: sr.clean_rule({"conditions": [leaf()]})}
        self.assertEqual(sr.loop_through(1, rules), [1, 2, 3, 1])
        self.assertIsNone(sr.loop_through(4, rules))
        self.assertEqual(sr.loop_through(5, {5: sr.clean_rule({"conditions": [{"type": "statrole", "rule": 5}]})}), [5, 5])

    def test_notifications(self):
        note = sr.clean_notification({"send": "dm_fallback", "channel_id": "222222222222222222", "group": True}, sr.DEFAULT_GIVE)
        self.assertEqual((note["send"], note["channel_id"], note["group"], note["message"]["content"]), ("dm_fallback", 222222222222222222, False, sr.DEFAULT_GIVE))
        with self.assertRaises(Invalid):
            sr.clean_notification({"send": "channel"}, sr.DEFAULT_GIVE)
        self.assertIsNone(sr.clean_notification({"on": False}, sr.DEFAULT_GIVE))

    def test_an_age_condition_needs_an_all_group_beside_a_statistic(self):
        age = {"type": "account", "compare": "more", "amount": 30, "unit": "days"}
        for bad in ([age], {"type": "match", "mode": "any", "conditions": [age, leaf()]}, [{"type": "match", "mode": "all", "conditions": [age]}, leaf()]):
            with self.assertRaises(Invalid, msg=str(bad)) as caught:
                sr.clean_rule({"conditions": bad})
            self.assertIn("age", str(caught.exception))
        sr.clean_rule({"conditions": [age, leaf()]})
        sr.clean_rule({"conditions": [age, {"type": "match", "mode": "any", "conditions": [leaf(), leaf("voice")]}]})
        sr.clean_rule({"conditions": [{"type": "joined", "compare": "less", "amount": 3, "unit": "days"}, {"type": "statrole", "rule": 4}]})

    def test_the_page_offers_no_group_level_the_server_refuses(self):
        page = (Path(__file__).resolve().parent.parent / "captainguild" / "web" / "static" / "js" / "pages" / "statroles.js").read_text(encoding="utf-8")
        self.assertEqual(int(re.search(r"const MAX_DEPTH = (\d+);", page).group(1)), sr.MAX_DEPTH)
        self.assertIn("depth >= MAX_DEPTH - 2", page, "the Group button turns off where a new group's conditions would sit too deep")

        def nested(groups):  # the page's root group, and `groups` groups below it, each holding the next
            node = leaf()
            for _ in range(groups):
                node = {"type": "match", "mode": "all", "conditions": [node]}
            return {"type": "match", "mode": "all", "conditions": [node]}
        sr.clean_rule({"conditions": nested(sr.MAX_DEPTH - 2)})  # a group added in a group at page depth MAX_DEPTH - 3
        with self.assertRaises(Invalid):
            sr.clean_rule({"conditions": nested(sr.MAX_DEPTH - 1)})  # one added at page depth MAX_DEPTH - 2, where the button is off

    def test_descriptions(self):
        self.assertEqual(sr.describe(sr.clean_condition(leaf(minimum=100))), "at least 100 messages in the last 30 days")
        top = sr.clean_condition({"type": "voice", "limit": {"kind": "top", "by": "percent", "from": 0, "to": 10, "period": {"amount": 7, "unit": "days"}}, "invert": True})
        self.assertEqual(sr.describe(top), "Not: the top 0% to 10% by minutes in the last 7 days")
        days = sr.clean_condition({"type": "message", "limit": {"kind": "days", "per_day": 5, "days": 7, "of": 7}})
        self.assertEqual(sr.describe(days), "5 messages a day on 7 of the last 7 days")
        self.assertEqual(sr.describe(sr.clean_condition({"type": "account", "compare": "more", "amount": 1, "unit": "days"})), "Account made more than 1 day ago")


class Engine(BotTest):
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        self.now = dt.datetime(2026, 11, 8, 12, tzinfo=dt.timezone.utc).timestamp()
        self.slot = tf.slot_of(self.now)
        self.people = [FakeMember(self.guild, f"p{index}", self.role_member) for index in range(6)]
        self.mdb = self.bot.message_db

    async def messages(self, counts, days_back=1):
        rows = [(self.guild.id, self.slot - days_back * 96 + 1, 10, person.id, 0, count) for person, count in counts.items()]
        await self.mdb.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?) "
                           "ON CONFLICT (guild_id, slot, channel_id, user_id, flags) DO UPDATE SET count = count + excluded.count", rows)

    def snapshot(self, private=frozenset()):
        snap = self.stats.snapshot(self.guild, self.now)
        snap.private = frozenset(private)
        return snap

    async def passing(self, *conditions, rules=None, mode="all", private=frozenset()):
        rule = sr.clean_rule({"conditions": {"type": "match", "mode": mode, "conditions": list(conditions)}})
        evaluator = sr.Evaluator(self.mdb, self.snapshot(private), rules or {})
        return (await evaluator.evaluate(rule)).passing, rule

    async def test_thresholds(self):
        p = self.people
        await self.messages({p[0]: 50, p[1]: 10, p[2]: 9})
        await self.messages({p[2]: 100}, days_back=40)
        found, _ = await self.passing(leaf(minimum=10))
        self.assertEqual(found, {p[0].id, p[1].id})
        found, _ = await self.passing(leaf(minimum=10, maximum=20))
        self.assertEqual(found, {p[1].id})
        found, _ = await self.passing(leaf(minimum=10, period={"all": True}))
        self.assertEqual(found, {p[0].id, p[1].id, p[2].id})
        found, _ = await self.passing(leaf(minimum=0, maximum=0))
        self.assertEqual(found, set(self.snapshot().people) - {p[0].id, p[1].id, p[2].id}, "nothing at all in the period")
        found, _ = await self.passing(leaf(minimum=10, invert=True))
        self.assertNotIn(p[0].id, found)
        self.assertIn(p[3].id, found)

    async def test_filters_fail_who_they_leave_out(self):
        p = self.people
        await self.messages({p[0]: 50, p[1]: 50, self.admin: 50})
        found, _ = await self.passing(leaf(roles_block=[self.role_admin.id]))
        self.assertEqual(found, {p[0].id, p[1].id})
        found, _ = await self.passing(leaf(roles_allow=[self.role_admin.id]))
        self.assertEqual(found, {self.admin.id})
        found, _ = await self.passing(leaf(member_mode="allow", members=[p[1].id]))
        self.assertEqual(found, {p[1].id})
        found, _ = await self.passing(leaf(stat={"channel_mode": "allow", "channels": [100000000000099999]}))
        self.assertEqual(found, set())

    async def test_top_by_place_and_by_percentile(self):
        p = self.people
        await self.messages({p[0]: 50, p[1]: 40, p[2]: 40, p[3]: 5})
        top = {"type": "message", "limit": {"kind": "top", "by": "rank", "from": 1, "to": 2, "period": {"amount": 30, "unit": "days"}}}
        found, _ = await self.passing(top)
        self.assertEqual(found, {p[0].id, p[1].id, p[2].id}, "a tie shares the place")
        second = {"type": "message", "limit": {"kind": "top", "by": "rank", "from": 4, "to": 10, "period": {"amount": 30, "unit": "days"}}}
        found, _ = await self.passing(second)
        self.assertEqual(found, {p[3].id}, "nobody with nothing is placed")
        population = len(self.snapshot().people)
        percent = {"type": "message", "limit": {"kind": "top", "by": "percent", "from": 0, "to": 100 / population, "period": {"amount": 30, "unit": "days"}}}
        found, _ = await self.passing(percent)
        self.assertEqual(found, {p[0].id})
        matching = {"type": "message", "roles_allow": [self.role_member.id],
                    "limit": {"kind": "top", "by": "percent", "from": 0, "to": 25, "matching_only": True, "period": {"amount": 30, "unit": "days"}}}
        members_with_role = sum(1 for person in self.guild.members if self.role_member in person.roles)
        found, _ = await self.passing(matching)
        expected = {p[0].id} if 1 / members_with_role * 100 <= 25 < 2 / members_with_role * 100 else found
        self.assertEqual(found, expected)

    async def test_day_ranges(self):
        p = self.people
        today = dt.datetime.fromtimestamp(self.now, UTC).date()
        rows = []
        for back in range(1, 8):  # p0 hits all of the last seven days before today, p1 misses one, p2 has a single big day
            day = today - dt.timedelta(days=back)
            slot = tf.slot_of(dt.datetime(day.year, day.month, day.day, 10, tzinfo=dt.timezone.utc).timestamp())
            rows.append((self.guild.id, slot, 10, p[0].id, 0, 5))
            if back != 3:
                rows.append((self.guild.id, slot, 10, p[1].id, 0, 5))
        rows.append((self.guild.id, self.slot - 4 * 96, 10, p[2].id, 0, 500))
        await self.mdb.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)", rows)
        every_day = {"type": "message", "limit": {"kind": "days", "per_day": 5, "days": 7, "of": 7}}
        found, _ = await self.passing(every_day)
        self.assertEqual(found, {p[0].id}, "today isn't in yet, so the seven days before it count")
        most_days = {"type": "message", "limit": {"kind": "days", "per_day": 5, "days": 6, "of": 7}}
        found, _ = await self.passing(most_days)
        self.assertEqual(found, {p[0].id, p[1].id})

    async def test_ages(self):
        old = FakeMember(self.guild, "veteran", joined=dt.datetime.fromtimestamp(self.now - 400 * DAY, dt.timezone.utc))
        new = FakeMember(self.guild, "newcomer", joined=dt.datetime.fromtimestamp(self.now - 2 * DAY, dt.timezone.utc))
        anyone = leaf(minimum=0, maximum=10 ** 9)  # an age condition sits beside a statistic, and this one everybody passes
        found, _ = await self.passing({"type": "joined", "compare": "more", "amount": 365, "unit": "days"}, anyone)
        self.assertIn(old.id, found)
        self.assertNotIn(new.id, found)
        found, _ = await self.passing({"type": "joined", "compare": "less", "amount": 72, "unit": "hours"}, anyone)
        self.assertIn(new.id, found)
        self.assertNotIn(old.id, found)
        found, _ = await self.passing({"type": "joined", "compare": "after", "date": "2026-11-01"}, anyone)
        self.assertIn(new.id, found)
        found, _ = await self.passing({"type": "account", "compare": "before", "date": "2025-01-01"}, anyone)
        self.assertIn(old.id, found)

    async def test_a_top_rule_ranks_only_the_members_it_applies_to(self):
        p = self.people
        bots = [FakeMember(self.guild, f"b{index}", bot=True) for index in range(2)]
        await self.messages({bots[0]: 500, bots[1]: 400, p[0]: 50, p[1]: 40, p[2]: 30})
        top = {"type": "message", "limit": {"kind": "top", "by": "rank", "from": 1, "to": 2, "period": {"amount": 30, "unit": "days"}}}
        for apply_to, expected in (("humans", {p[0].id, p[1].id}), ("bots", {bots[0].id, bots[1].id}), ("both", {bots[0].id, bots[1].id})):
            rule = sr.clean_rule({"conditions": [top], "apply_to": apply_to})
            evaluation = await sr.Evaluator(self.mdb, self.snapshot(), {}).evaluate(rule)
            self.assertEqual(evaluation.passing, expected, apply_to)
        people = sum(1 for member in self.guild.members if not member.bot)
        tenth = {"type": "message", "limit": {"kind": "top", "by": "percent", "from": 0, "to": 100 / people, "period": {"amount": 30, "unit": "days"}}}
        evaluation = await sr.Evaluator(self.mdb, self.snapshot(), {}).evaluate(sr.clean_rule({"conditions": [tenth], "apply_to": "humans"}))
        self.assertEqual(evaluation.passing, {p[0].id}, "percentiles are sized on the people the rule applies to")
        rules = {3: sr.clean_rule({"conditions": [top], "apply_to": "humans"})}
        follower = sr.clean_rule({"conditions": [{"type": "statrole", "rule": 3}], "apply_to": "both"})
        evaluation = await sr.Evaluator(self.mdb, self.snapshot(), rules).evaluate(follower)
        self.assertEqual(evaluation.passing, {p[0].id, p[1].id}, "a followed rule ranks the members it applies to")

    async def test_compare(self):
        p = self.people
        await self.messages({p[0]: 10, p[1]: 10})
        await self.mdb.many("INSERT INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (?, ?, 20, ?, 1, 0, ?)",
                           [(self.guild.id, self.slot - 5, p[1].id, 3600)])
        compare = {"type": "compare", "operator": "greater", "left": [{"kind": "message", "weight": 2, "period": {"amount": 7, "unit": "days"}}],
                   "right": [{"kind": "voice", "weight": 1, "period": {"amount": 7, "unit": "days"}}]}
        found, _ = await self.passing(compare)
        self.assertEqual(found, {p[0].id}, "20 is more than 0 minutes, and not more than 60")

    async def test_another_rules_result_and_groups(self):
        p = self.people
        await self.messages({p[0]: 50, p[1]: 15})
        rules = {7: sr.clean_rule({"conditions": [leaf(minimum=40)]})}
        found, _ = await self.passing({"type": "statrole", "rule": 7}, rules=rules)
        self.assertEqual(found, {p[0].id})
        found, _ = await self.passing(leaf(minimum=10), {"type": "statrole", "rule": 7, "invert": True}, rules=rules)
        self.assertEqual(found, {p[1].id})
        found, _ = await self.passing(leaf(minimum=40), leaf(minimum=12, maximum=20), mode="any")
        self.assertEqual(found, {p[0].id, p[1].id})

    async def test_what_a_run_changes(self):
        p = self.people
        await self.messages({p[0]: 50, p[1]: 50, p[2]: 50, self.me: 50})
        role = self.guild.role("Active", 3)
        p[3].roles.append(role)
        p[4].roles.append(role)
        p[2].pending = True
        rule = sr.clean_rule({"conditions": [leaf(minimum=10)], "ignore": [p[4].id]})
        evaluation = await sr.Evaluator(self.mdb, self.snapshot(private={p[1].id}), {}).evaluate(rule)
        change = sr.plan(rule, evaluation, self.snapshot(private={p[1].id}), role.id)
        self.assertEqual((change.give, change.take, change.left_alone), ([p[0].id], [p[3].id], 1))
        permanent = sr.plan({**rule, "permanent": True}, evaluation, self.snapshot(private={p[1].id}), role.id)
        self.assertEqual(permanent.take, [])
        bots = sr.plan({**rule, "apply_to": "bots"}, evaluation, self.snapshot(), role.id)
        self.assertEqual(bots.give, [], "the bot itself is never in a run")

    async def test_each_members_results(self):
        p = self.people
        await self.messages({p[0]: 50})
        rule = sr.clean_rule({"conditions": [leaf(minimum=10), {"type": "joined", "compare": "more", "amount": 1, "unit": "days"}]})
        evaluation = await sr.Evaluator(self.mdb, self.snapshot(), {}).evaluate(rule)
        [tree] = sr.explain(rule, evaluation, p[0].id)
        self.assertEqual([(entry["passed"], entry["value"]) for entry in tree["children"]][0], (True, 50.0))
        self.assertEqual(tree["text"], "All of 2 conditions")


def rule_form(role, **fields):
    return {"name": fields.pop("name", "Regulars"), "role_id": str(role.id), "conditions": fields.pop("conditions", [leaf(minimum=10)]), **fields}


class Runs(BotTest):
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        self.role = self.guild.role("Regular", 3)
        self.people = [FakeMember(self.guild, f"p{index}", self.role_member) for index in range(4)]
        self.slot = tf.slot_of(time.time())
        rows = [(self.guild.id, self.slot - 1, 10, person.id, 0, count) for person, count in zip(self.people, (50, 50, 1, 0))]
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)", rows)
        self.people[2].roles.append(self.role)
        self.notes = FakeChannel(self.guild, "role-news")

    async def save(self, **fields):
        cleaned = await self.stats.rule_form(self.guild, None, rule_form(self.role, **fields), None)
        return await self.stats.save_rule(self.guild.id, cleaned, None)

    async def finish(self, number, reason="manual"):
        with mock.patch("captainguild.modules.statistics_tools.SPACING", 0):
            self.assertEqual(await self.stats.start_run(self.guild.id, number, reason), "started")
            for _ in range(200):
                await asyncio.sleep(0)
                state = self.stats._server_runs.get(self.guild.id)
                if not state.runs and not state.working_out and not self.stats._tools_tasks and (state.worker is None or state.worker.done()):
                    break
                await asyncio.sleep(0.01)
        return await self.bot.db.one("SELECT * FROM stat_role_runs WHERE guild_id = ? AND rule = ? ORDER BY started_at DESC", (self.guild.id, number))

    async def test_a_run_gives_and_takes_the_role_and_notifies(self):
        give = {"send": "channel", "channel_id": str(self.notes.id), "message": {"content": "{mention} is a regular now ({role(name)})."}}
        number = await self.save(give=give, remove={"send": "dm", "message": {"content": "You lost {role(name)}."}})
        run = await self.finish(number)
        self.assertEqual((run["state"], run["gave"], run["took"], run["matching"]), ("done", 2, 1, 2))
        self.assertIn(self.role, self.people[0].roles)
        self.assertNotIn(self.role, self.people[2].roles)
        self.assertEqual(sorted(message.content for message in self.notes.sent), sorted(f"<@{person.id}> is a regular now (Regular)." for person in self.people[:2]))
        self.assertEqual(self.people[2].dms[0]["content"], "You lost Regular.")
        self.assertFalse(self.notes.sent[0].kwargs["allowed_mentions"].roles)

    async def test_grouped_notifications_and_a_dm_that_falls_back(self):
        give = {"send": "channel", "channel_id": str(self.notes.id), "group": True, "message": {"content": "Welcome to the regulars: {mention}"}}
        remove = {"send": "dm_fallback", "channel_id": str(self.notes.id), "message": {"content": "{mention} left the regulars."}}
        number = await self.save(give=give, remove=remove)

        async def closed(*args, **kwargs):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="no"), "Cannot send messages to this user")
        self.people[2].send = closed
        await self.finish(number)
        contents = [message.content for message in self.notes.sent]
        self.assertEqual(len(contents), 2)
        self.assertIn(f"<@{self.people[2].id}> left the regulars.", contents, "a closed DM falls back to the channel, as the change happens")
        [grouped] = [text for text in contents if text.startswith("Welcome to the regulars: ")]
        self.assertEqual(grouped.count("<@"), 2, "one message for the run, naming both")

    async def test_losing_manage_roles_stops_the_run(self):
        number = await self.save()
        self.role_bot.permissions = type(self.role_bot.permissions)()
        run = await self.finish(number)
        self.assertEqual(run["state"], "stopped")
        self.assertIn("Manage Roles", run["note"])
        self.assertNotIn(self.role, self.people[0].roles)

    async def test_a_role_above_the_bots_stops_the_run(self):
        number = await self.save()
        self.role.position = 50
        run = await self.finish(number)
        self.assertEqual((run["state"], run["note"]), ("stopped", "The role sits at or above the bot's highest role, so the run stopped. Move the bot's role above it."))

    async def test_a_change_that_goes_wrong_costs_only_itself(self):
        number = await self.save()

        async def broken(*roles, reason=None, atomic=True):
            raise RuntimeError("a bug")
        self.people[0].add_roles = broken
        with self.assertLogs("captainguild.modules.statistics_tools", "ERROR"):
            run = await self.finish(number)
        self.assertEqual((run["state"], run["failed"], run["gave"], run["took"]), ("done", 1, 1, 1))

        async def failing_notice(guild, run, member, role, adding):
            raise RuntimeError("another bug")
        self.stats._notify = failing_notice
        del self.people[0].add_roles
        with self.assertLogs("captainguild.modules.statistics_tools", "ERROR"):
            run = await self.finish(number)
        self.assertEqual((run["gave"], run["failed"], run["note"]), (1, 0, "A notification couldn't be sent. It's been logged."))
        self.assertEqual(await self.stats.start_run(self.guild.id, number, "manual"), "started", "the rule isn't left waiting on a worker that's gone")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if quiet(self.stats, self.guild.id):
                break

    async def test_a_worker_that_breaks_closes_its_runs(self):
        number = await self.save()
        with mock.patch("captainguild.modules.statistics_tools.SPACING", "not a number"), \
                self.assertLogs("captainguild.modules.statistics_tools", "ERROR"):
            self.assertEqual(await self.stats.start_run(self.guild.id, number, "manual"), "started")
            for _ in range(200):
                await asyncio.sleep(0.01)
                state = self.stats._server_runs.get(self.guild.id)
                if state and not state.runs and not state.working_out and not self.stats._tools_tasks and state.worker and state.worker.done():
                    break
        run = await self.bot.db.one("SELECT * FROM stat_role_runs WHERE guild_id = ? AND rule = ? ORDER BY started_at DESC", (self.guild.id, number))
        self.assertEqual((run["state"], run["gave"], run["note"]), ("stopped", 1, "Something went wrong during this run. It's been logged."))
        run = await self.finish(number)
        self.assertEqual((run["state"], run["gave"], run["took"]), ("done", 1, 1), "the rule runs again and finishes what was left")

    async def test_a_second_run_waits_for_the_first(self):
        number = await self.save()
        with mock.patch("captainguild.modules.statistics_tools.SPACING", 0):
            self.assertEqual(await self.stats.start_run(self.guild.id, number, "manual"), "started")
            self.assertEqual(await self.stats.start_run(self.guild.id, number, "manual"), "queued")
            for _ in range(300):
                await asyncio.sleep(0.01)
                state = self.stats._server_runs[self.guild.id]
                if quiet(self.stats, self.guild.id):
                    break
        runs = await self.bot.db.all("SELECT * FROM stat_role_runs WHERE rule = ?", (number,))
        self.assertEqual(len(runs), 2)

    async def test_the_schedule(self):
        number = await self.save(schedule={"preset": "hourly"})
        row = await self.bot.db.one("SELECT next_run FROM stat_roles WHERE number = ?", (number,))
        self.assertEqual(row["next_run"] % 3600, 0)
        await self.stats.statroles_due(row["next_run"] - 1)
        self.assertNotIn(number, self.stats.running(self.guild.id))
        with mock.patch("captainguild.modules.statistics_tools.SPACING", 0):
            await self.stats.statroles_due(row["next_run"] + 5)
            self.assertIn(number, self.stats.running(self.guild.id))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if quiet(self.stats, self.guild.id):
                    break
        after = await self.bot.db.one("SELECT next_run FROM stat_roles WHERE number = ?", (number,))
        self.assertEqual(after["next_run"], row["next_run"] + 3600)

    async def test_a_new_time_zone_moves_the_schedule_and_other_saves_dont(self):
        number = await self.save(schedule={"preset": "daily"})
        before = (await self.bot.db.one("SELECT next_run FROM stat_roles WHERE number = ?", (number,)))["next_run"]
        await self.bot.db.run("UPDATE stat_roles SET next_run = 5 WHERE number = ?", (number,))  # overdue, so the next minute runs it
        await self.bot.settings.save(self.guild.id, "statistics", {"interval": "hour"})
        self.assertEqual((await self.bot.db.one("SELECT next_run FROM stat_roles WHERE number = ?", (number,)))["next_run"], 5)
        await self.bot.settings.save(self.guild.id, "statistics", {"timezone": "Asia/Tokyo"})
        moved = (await self.bot.db.one("SELECT next_run FROM stat_roles WHERE number = ?", (number,)))["next_run"]
        self.assertEqual(dt.datetime.fromtimestamp(moved, tf.zone("Asia/Tokyo")).hour, 0)
        self.assertNotEqual(moved, before)

    async def test_a_deleted_role_switches_the_rule_off(self):
        number = await self.save()
        await self.stats._statrole_role_deleted(self.role)
        row = await self.bot.db.one("SELECT enabled, paused FROM stat_roles WHERE number = ?", (number,))
        self.assertEqual((row["enabled"], row["paused"]), (0, "Its role was deleted, so the bot switched it off. Pick a role, then switch it back on."))

    async def test_checks_on_saving(self):
        await self.save()
        with self.assertRaises(Exception) as caught:
            await self.stats.rule_form(self.guild, None, rule_form(self.role, name="Other"), None)
        self.assertIn("already gives that role", str(caught.exception))
        managed = self.guild.role("Integration", 2, managed=True)
        with self.assertRaises(Exception) as caught:
            await self.stats.rule_form(self.guild, None, rule_form(managed, name="X"), None)
        self.assertIn("integration", str(caught.exception))
        elevated = self.guild.role("Helpers", 2, "kick_members")
        with self.assertRaises(Exception) as caught:
            await self.stats.rule_form(self.guild, None, rule_form(elevated, name="Y"), None)
        self.assertIn("elevated roles", str(caught.exception))
        high = self.guild.role("Senior", 6)
        with self.assertRaises(Exception) as caught:
            await self.stats.rule_form(self.guild, self.mod, rule_form(high, name="Z"), None)
        self.assertIn("your highest role", str(caught.exception))
        await self.bot.settings.save(self.guild.id, "statistics", {"statrole_above_own": True})
        await self.stats.rule_form(self.guild, self.mod, rule_form(high, name="Z"), None)
        with self.assertRaises(Exception) as caught:
            await self.stats.rule_form(self.guild, None, rule_form(high, name="Loop", conditions=[{"type": "statrole", "rule": 1}, {"type": "statrole", "rule": 9}]), None)
        self.assertIn("isn't there", str(caught.exception))

    # ---- what stops a run, and what a restore does to one ----

    async def settle(self):
        """Lets runs, the worker and spawned work finish."""
        for _ in range(500):
            await asyncio.sleep(0.01)
            state = self.stats._server_runs.get(self.guild.id)
            busy = state is not None and (state.runs or state.working_out or state.again or (state.worker is not None and not state.worker.done()))
            if not busy and not self.stats._tools_tasks:
                return
        self.fail("runs never finished")

    def hold_first_change(self):
        """p0's role edit waits until the event is set, so a run sits in the worker partway through."""
        release = asyncio.Event()
        first = self.people[0]
        original = first.add_roles

        async def held(*roles, reason=None, atomic=True):
            await release.wait()
            await original(*roles, reason=reason, atomic=atomic)
        first.add_roles = held
        return release

    async def waiting_on_the_first_change(self, number):
        for _ in range(200):
            await asyncio.sleep(0.01)
            state = self.stats._server_runs.get(self.guild.id)
            if state and number in state.runs and len(state.runs[number].give) < 2:
                return
        self.fail("the run never reached its first change")

    async def restore(self, number, rule=None, next_run=None):
        """What a backup's restore does around its swap of the tables, with the backup's version of the rule."""
        await self.stats.before_restore(self.guild.id)
        if rule is not None:
            await self.bot.db.run("UPDATE stat_roles SET rule = ?, next_run = ? WHERE guild_id = ? AND number = ?", (json.dumps(rule), next_run, self.guild.id, number))
        await self.stats.on_guild_restored(self.guild.id)
        await self.stats.after_restore(self.guild.id)

    async def runs_since(self, moment):
        return await self.bot.db.all("SELECT * FROM stat_role_runs WHERE guild_id = ? AND started_at >= ? ORDER BY started_at", (self.guild.id, moment))

    async def test_a_restore_stops_runs_and_the_one_queued_behind_them(self):
        number = await self.save()
        release = self.hold_first_change()
        with mock.patch("captainguild.modules.statistics_tools.SPACING", 0):
            self.assertEqual(await self.stats.start_run(self.guild.id, number, "manual"), "started")
            await self.waiting_on_the_first_change(number)
            self.assertEqual(await self.stats.start_run(self.guild.id, number, "schedule"), "queued")
            restored_at = time.time()
            await self.restore(number, sr.clean_rule({"conditions": [leaf(minimum=1_000_000)]}))
            release.set()
            await self.settle()
        self.assertEqual(await self.runs_since(restored_at), [], "nothing starts on its own after a restore")
        self.assertIn(self.role, self.people[2].roles)
        self.assertNotIn(self.role, self.people[1].roles, "the run stopped at the restore")
        row = await self.bot.db.one("SELECT * FROM stat_role_runs WHERE guild_id = ? AND rule = ?", (self.guild.id, number))
        self.assertEqual((row["state"], row["note"]), ("stopped", "A backup was restored during this run."))

    async def test_a_run_working_out_during_a_restore_changes_nothing(self):
        number = await self.save()
        release = asyncio.Event()
        original = sr.Evaluator.evaluate

        async def slow(evaluator, rule):
            await release.wait()
            return await original(evaluator, rule)
        with mock.patch.object(sr.Evaluator, "evaluate", slow), mock.patch("captainguild.modules.statistics_tools.SPACING", 0):
            self.assertEqual(await self.stats.start_run(self.guild.id, number, "manual"), "started")
            await asyncio.sleep(0.05)
            self.assertIn(number, self.stats._server_runs[self.guild.id].working_out)
            await self.restore(number, sr.clean_rule({"conditions": [leaf(minimum=1_000_000)]}))
            release.set()
            await self.settle()
        self.assertNotIn(self.role, self.people[0].roles)
        self.assertIn(self.role, self.people[2].roles)
        row = await self.bot.db.one("SELECT * FROM stat_role_runs WHERE guild_id = ? AND rule = ?", (self.guild.id, number))
        self.assertEqual((row["state"], row["gave"], row["took"]), ("stopped", 0, 0))

    async def test_nothing_starts_while_a_restore_is_under_way(self):
        number = await self.save()
        await self.stats.before_restore(self.guild.id)
        self.assertEqual(await self.stats.start_run(self.guild.id, number, "manual"), "held")
        await self.bot.db.run("UPDATE stat_roles SET next_run = ? WHERE number = ?", (time.time() - 60, number))  # the swap brought back a past time
        await self.stats.statroles_due(time.time())
        await self.stats.on_guild_restored(self.guild.id)
        await self.stats.statroles_due(time.time())
        self.assertEqual(self.stats.running(self.guild.id), {})
        await self.stats.after_restore(self.guild.id)
        await self.settle()
        self.assertEqual(await self.bot.db.all("SELECT * FROM stat_role_runs"), [])
        self.assertEqual((await self.finish(number))["state"], "done", "the hold ends with the restore")

    async def test_a_restore_reads_the_backups_rules_afresh(self):
        number = await self.save()
        self.assertEqual(self.bot.transient_roles(self.guild.id), {self.role.id})
        backup = sr.clean_rule({"conditions": [leaf(minimum=10)], "permanent": True})
        restored_at = time.time()
        await self.restore(number, backup, next_run=restored_at - 3600)
        with mock.patch("captainguild.modules.statistics_tools.SPACING", 0):
            await self.stats.statroles_due(time.time())
            await self.settle()
        self.assertEqual(await self.runs_since(restored_at), [], "the backup's past next run doesn't start the rule at the next minute")
        self.assertGreater((await self.bot.db.one("SELECT next_run FROM stat_roles WHERE number = ?", (number,)))["next_run"], time.time())
        self.assertEqual(self.bot.transient_roles(self.guild.id), set(), "the restored rule is permanent, so sticky roles may give its role back")

    async def test_a_role_that_became_elevated_stops_the_run(self):
        number = await self.save()
        self.role.permissions = FakePermissions("kick_members", "ban_members")
        run = await self.finish(number)
        self.assertEqual(run["state"], "stopped")
        self.assertIn("Commands page", run["note"])
        self.assertNotIn(self.role, self.people[0].roles)
        self.assertIn((self.guild.id, f"statrole-elevated:{number}"), self.stats._tools_told)
        self.role.permissions = FakePermissions()
        self.stats._tools_told.clear()
        first = self.people[0]
        original = first.add_roles

        async def then_elevated(*roles, reason=None, atomic=True):
            await original(*roles, reason=reason, atomic=atomic)
            self.role.permissions = FakePermissions("ban_members")  # someone gives the role Ban Members partway through
        first.add_roles = then_elevated
        run = await self.finish(number)
        self.assertEqual((run["state"], run["gave"]), ("stopped", 1))
        self.assertNotIn(self.role, self.people[1].roles)

    async def test_losing_manage_roles_after_the_first_change_stops_the_run(self):
        number = await self.save()
        first = self.people[0]
        original = first.add_roles

        async def then_lost(*roles, reason=None, atomic=True):
            await original(*roles, reason=reason, atomic=atomic)
            self.role_bot.permissions = FakePermissions()  # someone takes Manage Roles from the bot partway through
        first.add_roles = then_lost
        run = await self.finish(number)
        self.assertEqual((run["state"], run["gave"], run["note"]), ("stopped", 1, "The bot lost Manage Roles, so the run stopped."))
        self.assertNotIn(self.role, self.people[1].roles)
        self.assertIn(self.role, self.people[2].roles)

    async def test_a_rule_following_one_that_is_gone_stops_and_takes_nothing(self):
        base = await self.save(name="Base")
        fans = self.guild.role("Fans", 2)
        cleaned = await self.stats.rule_form(self.guild, None, rule_form(fans, name="Fans", conditions=[{"type": "statrole", "rule": base}]), None)
        number = await self.stats.save_rule(self.guild.id, cleaned, None)
        for person in self.people:
            person.roles.append(fans)
        for gone in ("DELETE FROM stat_roles WHERE guild_id = ? AND number = ?", "UPDATE stat_roles SET rule = 'not a rule' WHERE guild_id = ? AND number = ?"):
            if gone.startswith("UPDATE"):
                await self.bot.db.run("INSERT INTO stat_roles (guild_id, number, name, role_id, enabled, rule, created_at, updated_at) VALUES (?, ?, 'Base', ?, 1, '{}', 0, 0)",
                                      (self.guild.id, base, self.role.id))
            await self.bot.db.run(gone, (self.guild.id, base))
            self.stats._tools_told.clear()
            run = await self.finish(number)
            self.assertEqual((run["state"], run["took"]), ("stopped", 0), gone)
            self.assertIn("follows a stat role", run["note"])
            self.assertTrue(all(fans in person.roles for person in self.people), "nobody lost the role")
            self.assertIn((self.guild.id, f"statrole-follows:{number}"), self.stats._tools_told)

    async def test_with_statistics_off_nothing_runs_and_a_run_going_stops(self):
        number = await self.save()
        release = self.hold_first_change()
        with mock.patch("captainguild.modules.statistics_tools.SPACING", 0):
            self.assertEqual(await self.stats.start_run(self.guild.id, number, "manual"), "started")
            await self.waiting_on_the_first_change(number)
            await self.bot.settings.save(self.guild.id, "statistics", {"enabled": False})
            release.set()
            await self.settle()
        row = await self.bot.db.one("SELECT * FROM stat_role_runs WHERE guild_id = ? AND rule = ?", (self.guild.id, number))
        self.assertEqual((row["state"], row["note"]), ("stopped", "Statistics was switched off, so the run stopped."))
        self.assertNotIn(self.role, self.people[1].roles)
        self.assertEqual(await self.stats.start_run(self.guild.id, number, "manual"), "off")

    async def test_grouped_notifications_never_cut_a_mention(self):
        long_name = self.guild.role("R" * 100, 3)
        people = [FakeMember(self.guild, f"x{index}", self.role_member) for index in range(200)]
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 10, ?, 0, 50)",
                                       [(self.guild.id, self.slot - 1, person.id) for person in people])
        give = {"send": "channel", "channel_id": str(self.notes.id), "group": True, "message": {"content": "{role(name)} for {mention}"}}
        cleaned = await self.stats.rule_form(self.guild, None, rule_form(long_name, name="Long", give=give), None)
        run = await self.finish(await self.stats.save_rule(self.guild.id, cleaned, None))
        self.assertEqual(run["gave"], 202)
        contents = [message.content for message in self.notes.sent]
        self.assertGreater(len(contents), 1)
        for content in contents:
            self.assertLessEqual(len(content), 2000)
            self.assertRegex(content, r"<@\d+>$", "each message ends on a whole mention")
        named = [int(found) for content in contents for found in re.findall(r"<@(\d+)>", content)]
        self.assertEqual(sorted(named), sorted([self.people[0].id, self.people[1].id, *(person.id for person in people)]))

    async def test_grouped_notifications_stop_at_ten_messages_and_say_how_many_more(self):
        people = [FakeMember(self.guild, f"y{index}", self.role_member) for index in range(1000)]
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 10, ?, 0, 50)",
                                       [(self.guild.id, self.slot - 1, person.id) for person in people])
        give = {"send": "channel", "channel_id": str(self.notes.id), "group": True, "message": {"content": "Welcome {mention}"}}
        run = await self.finish(await self.save(give=give))
        contents = [message.content for message in self.notes.sent]
        self.assertEqual(len(contents), 10)
        self.assertRegex(contents[-1], r"<@\d+> and [\d,]+ more$")
        named = sum(len(re.findall(r"<@\d+>", content)) for content in contents)
        more = int(re.search(r"and ([\d,]+) more$", contents[-1]).group(1).replace(",", ""))
        self.assertEqual(named + more, run["gave"])

    async def test_dms_stop_past_two_hundred_in_a_run_and_the_rest_go_to_the_channel(self):
        people = [FakeMember(self.guild, f"d{index}", self.role_member) for index in range(203)]
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 10, ?, 0, 50)",
                                       [(self.guild.id, self.slot - 1, person.id) for person in people])
        give = {"send": "dm_fallback", "channel_id": str(self.notes.id), "message": {"content": "{mention} got {role(name)}."}}
        number = await self.save(give=give)
        with mock.patch("captainguild.modules.statistics_tools.DM_SPACING", 0):
            run = await self.finish(number)
        everyone = [self.people[0], self.people[1], *people]
        self.assertEqual(sum(len(person.dms) for person in everyone), 200)
        self.assertEqual(len(self.notes.sent), 5)
        self.assertIn("200", run["note"])
        dm_only = {"send": "dm", "message": {"content": "You got {role(name)}."}}
        for person in everyone:
            person.roles.remove(self.role)
            person.dms.clear()
        self.notes.sent.clear()
        await self.stats.save_rule(self.guild.id, await self.stats.rule_form(self.guild, None, rule_form(self.role, give=dm_only), number), number)
        with mock.patch("captainguild.modules.statistics_tools.DM_SPACING", 0):
            run = await self.finish(number)
        self.assertEqual((sum(len(person.dms) for person in everyone), len(self.notes.sent)), (200, 0), "with no channel to fall back on, the rest go without")
        self.assertIn("200", run["note"])

    async def test_dms_in_a_server_go_out_spaced_apart(self):
        give = {"send": "dm", "message": {"content": "You got {role(name)}."}}
        number = await self.save(give=give)
        times = []
        for person in self.people[:2]:
            original = person.send

            async def timed(*args, _original=original, **kwargs):
                times.append(time.monotonic())
                return await _original(*args, **kwargs)
            person.send = timed
        with mock.patch("captainguild.modules.statistics_tools.DM_SPACING", 0.2):
            await self.finish(number)
        self.assertEqual(len(times), 2)
        self.assertGreaterEqual(times[1] - times[0], 0.2)

    async def test_a_slow_message_doesnt_shorten_the_wait_for_the_next_dm(self):
        give = {"send": "dm", "message": {"content": "You got {role(name)}."}}
        number = await self.save(give=give)
        times = []
        for person in self.people[:2]:
            original = person.send

            async def timed(*args, _original=original, **kwargs):
                times.append(time.monotonic())
                return await _original(*args, **kwargs)
            person.send = timed
        renders = []
        from captainguild.modules import statistics_tools
        real_render = statistics_tools.render_message

        async def slow_first(payload, scope):
            renders.append(1)
            if len(renders) == 1:
                await asyncio.sleep(0.15)  # the first DM takes long to render, which used to come out of the second one's wait
            return await real_render(payload, scope)
        with mock.patch("captainguild.modules.statistics_tools.DM_SPACING", 0.2), mock.patch("captainguild.modules.statistics_tools.render_message", slow_first):
            await self.finish(number)
        self.assertEqual(len(times), 2)
        self.assertGreaterEqual(times[1] - times[0], 0.2)

    async def test_a_long_stretch_of_members_to_skip_lets_other_work_run(self):
        number = await self.save()
        events = []
        original = self.stats._change_role

        async def counted(guild, run, member_id, adding):
            events.append("s")
            return await original(guild, run, member_id, adding)
        self.stats._change_role = counted
        nobody = [10 ** 17 + index for index in range(2000)]  # not in the server, so each is skipped
        done = False

        async def ticker():
            while not done:
                events.append("t")
                await asyncio.sleep(0)
        ticking = asyncio.ensure_future(ticker())
        with mock.patch.object(sr, "plan", lambda rule, evaluation, snapshot, role_id: sr.Plan(nobody, [], 0, 0)):
            await self.finish(number)
        done = True
        await ticking
        longest = max(len(stretch) for stretch in "".join(events).split("t"))
        self.assertLessEqual(longest, 500)

    async def test_a_recount_or_merge_applying_holds_stat_roles_until_its_next_time(self):
        number = await self.save()
        self.stats.stats_applying = lambda guild_id: guild_id == self.guild.id
        self.assertEqual(await self.stats.start_run(self.guild.id, number, "manual"), "applying")
        now = time.time()
        await self.bot.db.run("UPDATE stat_roles SET next_run = ? WHERE number = ?", (now - 60, number))
        await self.stats.statroles_due(now)
        await self.settle()
        self.assertEqual(await self.bot.db.all("SELECT * FROM stat_role_runs"), [], "the scheduled time passed by")
        self.assertGreater((await self.bot.db.one("SELECT next_run FROM stat_roles WHERE number = ?", (number,)))["next_run"], now, "it runs at its next time")
        del self.stats.stats_applying
        self.assertEqual((await self.finish(number))["state"], "done")

    async def test_a_run_working_out_when_a_recount_starts_applying_stops(self):
        number = await self.save()
        original = sr.Evaluator.evaluate

        async def then_applying(evaluator, rule):
            found = await original(evaluator, rule)
            self.stats.stats_applying = lambda guild_id: True
            return found
        with mock.patch.object(sr.Evaluator, "evaluate", then_applying), mock.patch("captainguild.modules.statistics_tools.SPACING", 0):
            self.assertEqual(await self.stats.start_run(self.guild.id, number, "manual"), "started")
            await self.settle()
        row = await self.bot.db.one("SELECT * FROM stat_role_runs WHERE rule = ?", (number,))
        self.assertEqual((row["state"], row["gave"], row["took"]), ("stopped", 0, 0))
        self.assertIn("recount or merge", row["note"])

    async def test_a_schedule_never_starts_a_run_within_ten_minutes_of_the_last(self):
        number = await self.save()
        now = time.time()
        await self.bot.db.run("INSERT INTO stat_role_runs (guild_id, rule, started_at, reason, state) VALUES (?, ?, ?, 'schedule', 'done')", (self.guild.id, number, now - 60))
        await self.bot.db.run("UPDATE stat_roles SET next_run = ? WHERE number = ?", (now - 1, number))
        await self.stats.statroles_due(now)
        await self.settle()
        self.assertEqual(len(await self.bot.db.all("SELECT * FROM stat_role_runs WHERE rule = ?", (number,))), 1, "a clock change can't start it a minute after the last")
        self.assertGreater((await self.bot.db.one("SELECT next_run FROM stat_roles WHERE number = ?", (number,)))["next_run"], now)


class Routes(BotTest):
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)  # the page's channel checks read the fake server
        self.stats = self.cog(Statistics)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        self.role = self.guild.role("Regular", 3)
        self.people = [FakeMember(self.guild, f"p{index}", self.role_member) for index in range(3)]
        slot = tf.slot_of(time.time())
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 10, ?, 0, ?)",
                                       [(self.guild.id, slot - 1, person.id, count) for person, count in zip(self.people, (50, 5, 0))])

    async def test_preview_check_save_run_and_delete(self):
        client = await self.client()
        form = rule_form(self.role)
        preview = await (await client.post(self.path("/statroles/preview"), json={**form, "everyone": True})).json()
        self.assertEqual((preview["matching"], preview["gain"], preview["lose"]), (1, 1, 0))
        self.assertEqual(preview["gain_names"], [{"id": str(self.people[0].id), "name": "p0"}])  # an ID past what JavaScript holds goes as text
        self.assertEqual([row["id"] for row in preview["everyone"]], [str(self.people[0].id)])
        check = await (await client.post(self.path("/statroles/check"), json={**form, "member_id": str(self.people[1].id)})).json()
        self.assertFalse(check["passes"])
        self.assertEqual(check["conditions"][0]["children"][0]["value"], 5.0)
        saved = await client.post(self.path("/statroles"), json=form)
        self.assertEqual(saved.status, 200, await saved.text())
        number = (await saved.json())["number"]
        listed = await (await client.get(self.path("/statroles"))).json()
        self.assertEqual([(rule["name"], rule["described"]) for rule in listed["rules"]], [("Regulars", ["at least 10 messages in the last 30 days"])])
        follower = await client.post(self.path("/statroles"), json=rule_form(self.guild.role("Fans", 2), name="Fans", conditions=[{"type": "statrole", "rule": number}]))
        self.assertEqual(follower.status, 200, await follower.text())
        refused = await client.delete(self.path(f"/statroles/{number}"))
        self.assertEqual(refused.status, 409)
        self.assertIn("Fans", (await refused.json())["error"])
        switched = await client.patch(self.path(f"/statroles/{number}"), json={"enabled": False})
        self.assertEqual(switched.status, 200)
        with mock.patch("captainguild.modules.statistics_tools.SPACING", 0):
            ran = await client.post(self.path(f"/statroles/{number}/run"))
            self.assertEqual((await ran.json())["started"], "started")
            for _ in range(300):
                await asyncio.sleep(0.01)
                if quiet(self.stats, self.guild.id):
                    break
        self.assertIn(self.role, self.people[0].roles, "a switched-off rule still runs when someone asks")

    async def test_a_returning_member_doesnt_get_a_stat_role_back_unless_its_permanent(self):
        client = await self.client()
        kept = self.guild.role("Veteran", 2)
        number = (await (await client.post(self.path("/statroles"), json=rule_form(self.role))).json())["number"]
        await client.post(self.path("/statroles"), json=rule_form(kept, name="Veterans", permanent=True))
        self.assertEqual(self.bot.transient_roles(self.guild.id), {self.role.id})
        await client.patch(self.path(f"/statroles/{number}"), json={"enabled": False})
        self.assertEqual(self.bot.transient_roles(self.guild.id), set(), "a rule that's off no longer keeps its role in step")
        await client.patch(self.path(f"/statroles/{number}"), json={"enabled": True})
        self.assertEqual(self.bot.transient_roles(self.guild.id), {self.role.id})
        await self.stats._statrole_role_deleted(self.role)
        self.assertEqual(self.bot.transient_roles(self.guild.id), set())
        await client.patch(self.path(f"/statroles/{number}"), json={"enabled": True})
        self.stats._held_roles = {}
        await self.stats._load_held_roles()
        self.assertEqual(self.bot.transient_roles(self.guild.id), {self.role.id}, "read back the same at the next start")

    async def test_the_setting_for_roles_above_ones_own_is_for_administrators(self):
        client = await self.client()
        self.admin.roles = [self.guild.default_role, self.guild.role("Manager", 3, "manage_guild", "manage_roles")]
        answer = await client.put(self.path("/settings/statistics"), json={"statrole_above_own": True})
        self.assertEqual(answer.status, 400)
        self.assertIn("Administrator", (await answer.json())["error"])

    async def test_someone_below_a_rules_role_cant_rewrite_it_or_switch_it_on(self):
        client = await self.client()
        senior = self.guild.role("Senior", 6)  # above the editor's highest role, below the bot's
        cleaned = await self.stats.rule_form(self.guild, None, rule_form(senior, name="Seniors", conditions=[leaf(minimum=100_000)]), None)
        number = await self.stats.save_rule(self.guild.id, cleaned, None)  # an Administrator set it up
        manager = self.guild.role("Manager", 3, "manage_guild", "manage_roles")
        self.admin.roles = [self.guild.default_role, manager]
        only_me = leaf(minimum=0, maximum=10 ** 9, member_mode="allow", members=[str(self.admin.id)])
        edited = await client.put(self.path(f"/statroles/{number}"), json=rule_form(senior, name="Seniors", conditions=[only_me]))
        self.assertEqual(edited.status, 400)
        self.assertIn("your highest role", (await edited.json())["error"])
        await self.bot.db.run("UPDATE stat_roles SET enabled = 0 WHERE number = ?", (number,))
        switched = await client.patch(self.path(f"/statroles/{number}"), json={"enabled": True})
        self.assertEqual(switched.status, 400)
        self.assertIn("your highest role", (await switched.json())["error"])
        self.assertEqual((await self.stats._rule(self.guild.id, number))["rule"]["conditions"], cleaned["rule"]["conditions"])
        own = await client.post(self.path("/statroles"), json=rule_form(self.guild.role("Helper", 2), name="Helpers"))
        self.assertEqual(own.status, 200, "a rule for a role below their own still saves")

    async def test_previews_and_checks_need_manage_roles_as_saving_does(self):
        client = await self.client()
        self.admin.roles = [self.guild.default_role, self.guild.role("Manager", 3, "manage_guild")]
        for tail, extra in (("/statroles/preview", {"everyone": True}), ("/statroles/check", {"member_id": str(self.people[1].id)})):
            answer = await client.post(self.path(tail), json={**rule_form(self.role), **extra})
            self.assertEqual(answer.status, 403, tail)

    async def test_run_now_waits_for_statistics_to_be_on(self):
        client = await self.client()
        number = (await (await client.post(self.path("/statroles"), json=rule_form(self.role))).json())["number"]
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": False})
        answer = await client.post(self.path(f"/statroles/{number}/run"))
        self.assertEqual(answer.status, 409)
        self.assertIn("Statistics", (await answer.json())["error"])
        self.assertEqual(self.stats.running(self.guild.id), {})

    async def test_run_now_waits_for_a_recount_or_merge_to_finish_applying(self):
        client = await self.client()
        number = (await (await client.post(self.path("/statroles"), json=rule_form(self.role))).json())["number"]
        self.stats.stats_applying = lambda guild_id: True
        answer = await client.post(self.path(f"/statroles/{number}/run"))
        self.assertEqual(answer.status, 409)
        self.assertIn("recount or merge", (await answer.json())["error"])
        preview = await client.post(self.path("/statroles/preview"), json={**rule_form(self.role), "number": number})
        self.assertEqual(preview.status, 409, "a preview would read half-swapped counts too")

    def picked(self, area, role):
        """The server's Administrators picked `role` for `area` in Statistics settings."""
        original = self.stats.cfg
        self.stats.cfg = lambda guild_id: {**original(guild_id), "dashboard_access": "members", "area_roles": {area: [role.id]}}

    async def test_a_role_picked_for_stat_roles_manages_them_and_nothing_else(self):
        helpers = self.guild.role("Stat helpers", 3)
        self.admin.roles = [self.guild.default_role, helpers]  # no Manage Server, no Manage Roles
        self.picked("statroles", helpers)
        client = await self.client(access=None)
        below = await client.post(self.path("/statroles"), json=rule_form(self.guild.role("Regular helpers", 2), name="Low"))
        self.assertEqual(below.status, 200, await below.text())
        self.assertEqual((await client.get(self.path("/statroles"))).status, 200)
        above = await client.post(self.path("/statroles"), json=rule_form(self.guild.role("Senior", 5), name="High"))
        self.assertEqual(above.status, 400)
        self.assertIn("your highest role", (await above.json())["error"])
        over_the_bot = await client.post(self.path("/statroles"), json=rule_form(self.guild.role("Top", 12), name="Top"))
        self.assertEqual(over_the_bot.status, 400)
        self.assertIn("bot's highest role", (await over_the_bot.json())["error"])
        counters_page = await client.get(self.path("/counters"))
        self.assertEqual(counters_page.status, 403, "the stat roles area doesn't open counters")
        self.admin.roles = [self.guild.default_role, self.role_member]
        refused = await client.get(self.path("/statroles"))
        self.assertEqual(refused.status, 403)
        self.assertIn("role picked for stat roles", (await refused.json())["error"])

    async def test_a_role_picked_for_counters_manages_them_and_nothing_else(self):
        keepers = self.guild.role("Counter keepers", 3)
        self.admin.roles = [self.guild.default_role, keepers]
        self.picked("counters", keepers)
        client = await self.client(access=None)
        self.assertEqual((await client.get(self.path("/counters"))).status, 200)
        made = await client.post(self.path("/counters"), json={"text": "Members: {1}", "counters": [{"type": "members"}], "frequency": 10, "enabled": False})
        self.assertEqual(made.status, 200, await made.text())
        self.assertEqual((await client.post(self.path("/counters/preview"), json={"text": "{1}", "frequency": 10, "counters": [{"type": "members"}]})).status, 200)
        self.assertEqual((await client.get(self.path("/statroles"))).status, 403)
        self.assertEqual((await client.post(self.path("/statroles"), json=rule_form(self.guild.role("Low", 2)))).status, 403)
        self.admin.roles = [self.guild.default_role, self.role_member]
        self.assertEqual((await client.get(self.path("/counters"))).status, 403, "a plain member gets nothing")

    async def test_discord_failing_a_step_answers_in_a_sentence(self):
        client = await self.client()
        thread = FakeChannel(self.guild, "a question", discord.ChannelType.public_thread)
        form = rule_form(self.role, give={"send": "channel", "channel_id": str(thread.id), "message": {"content": "{mention}!"}})
        for error, status in ((discord.Forbidden(types.SimpleNamespace(status=403, reason="no"), "Missing Access"), 409),
                              (discord.HTTPException(types.SimpleNamespace(status=503, reason="down"), "unavailable"), 502)):
            with mock.patch.object(DiscordOps, "find_thread", mock.AsyncMock(side_effect=error)):
                answer = await client.post(self.path("/statroles"), json=form)
            self.assertEqual(answer.status, status)
            self.assertIn("Discord", (await answer.json())["error"])

    async def test_a_restore_never_renumbers_counters_or_stat_roles(self):
        catalog = await read_catalog(self.db, "main", table_owners(self.bot), child_links(self.bot))
        for name in ("stat_counters", "stat_roles", "stat_role_runs"):
            self.assertIsNone(catalog.tables[f"main.{name}"].rowid_key, f"{name} is keyed by server and number, so its numbers come back as they were")

    async def test_a_notification_channel_must_be_one_they_can_post_in(self):
        client = await self.client()
        channel = FakeChannel(self.guild, "announcements")
        form = rule_form(self.role, give={"send": "channel", "channel_id": str(channel.id), "message": {"content": "{mention}!"}})
        with mock.patch("captainguild.web.api.Req.can_post", lambda req, channel_id: False):
            answer = await client.post(self.path("/statroles"), json=form)
        self.assertEqual(answer.status, 400)
        self.assertIn("post", (await answer.json())["error"])

    async def test_a_notification_can_go_to_a_thread(self):
        client = await self.client()
        parent = FakeChannel(self.guild, "help")
        thread = FakeChannel(self.guild, "a question", discord.ChannelType.public_thread)
        thread.parent = parent
        thread.locked = False
        form = rule_form(self.role, give={"send": "channel", "channel_id": str(thread.id), "message": {"content": "{mention}!"}})
        with mock.patch.object(DiscordOps, "find_thread", mock.AsyncMock(return_value=True)) as looked:
            answer = await client.post(self.path("/statroles"), json=form)
        self.assertEqual(answer.status, 200, await answer.text())
        looked.assert_awaited()
        saved = await self.stats._rule(self.guild.id, (await answer.json())["number"])
        self.assertEqual(saved["rule"]["give"]["channel_id"], thread.id)

    async def test_a_test_notification(self):
        client = await self.client()
        channel = FakeChannel(self.guild, "news")
        answer = await client.post(self.path("/statroles/test"), json={"which": "give", "role_id": str(self.role.id),
                                                                        "notification": {"send": "channel", "channel_id": str(channel.id), "message": {"content": "{mention} got {role(name)}"}}})
        self.assertEqual(answer.status, 200, await answer.text())
        self.assertEqual(channel.sent[0].content, f"<@{self.admin.id}> got Regular")


if __name__ == "__main__":
    unittest.main()
