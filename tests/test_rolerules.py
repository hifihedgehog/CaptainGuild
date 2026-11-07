"""Role rules: how rules decide, how changes reach Discord, and who may set them."""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from unittest import mock

import discord

from captainguild.core.shortcuts import expand_line
from captainguild.modules import rolerules as module
from captainguild.modules.rolerules import (
    CONFIRM_OVER, GONE_NOTE, MAX_PASSES, CreateFlags, Rule, RoleRules, compile_rules, condition_holds, from_old, old_shape, rule_list, settle,
)
from tests.fakes import BotTest, FakeChannel, FakeMember
from tests.gateway import GatewayTest, iso, new_id as gateway_id, user_json

LOG = "captainguild.modules.rolerules"


def rule(rule_id: int, conditions: list, *, add=(), remove=(), keep=False, name: str | None = None) -> Rule:
    return Rule(rule_id, name or f"r{rule_id}", tuple((kind, frozenset(roles), count) for kind, roles, count in conditions), frozenset(add), frozenset(remove), keep)


def row(name: str, conditions: list, *, add=(), remove=(), keep=False, enabled=True, rule_id=None) -> dict:
    out = {"name": name, "enabled": enabled, "keep_in_sync": keep, "add": [str(r) for r in add], "remove": [str(r) for r in remove],
           "conditions": [{"type": kind, "roles": [str(r) for r in roles], "count": count} for kind, roles, count in conditions]}
    if rule_id is not None:
        out["id"] = rule_id
    return out


class ConditionTest(BotTest):
    async def test_each_type_at_its_edges(self):
        roles = frozenset({1, 2, 3})
        cases = {
            "has_some": {0: False, 1: True, 3: True},
            "has_all": {0: False, 2: False, 3: True},
            "lacks_some": {0: True, 2: True, 3: False},
            "lacks_all": {0: True, 1: False, 3: False},
        }
        for kind, answers in cases.items():
            for held, expected in answers.items():
                self.assertIs(condition_holds(kind, roles, 0, set(list(roles)[:held]) | {99}), expected, f"{kind} holding {held}")
        counted = {"exactly": (False, True, False), "at_least": (False, True, True), "at_most": (True, True, False), "more_than": (False, False, True), "fewer_than": (True, False, False)}
        for kind, (below, at, above) in counted.items():
            self.assertIs(condition_holds(kind, roles, 2, {1}), below, f"{kind} 1 of 2")
            self.assertIs(condition_holds(kind, roles, 2, {1, 2}), at, f"{kind} 2 of 2")
            self.assertIs(condition_holds(kind, roles, 2, {1, 2, 3}), above, f"{kind} 3 of 2")
        self.assertTrue(condition_holds("fewer_than", roles, 1, set()))
        self.assertTrue(condition_holds("exactly", roles, 0, {99}))
        self.assertFalse(condition_holds("more_than", roles, 0, set()))

    async def test_every_condition_must_hold(self):
        both = rule(1, [("has_some", [1], 0), ("lacks_all", [2], 0)], add=[9])
        self.assertEqual(settle([both], {1}, {9}).final, {1, 9})
        self.assertEqual(settle([both], {1, 2}, {9}).final, {1, 2})


class SettleTest(BotTest):
    async def test_keep_in_sync_adds_and_takes_back(self):
        sync = [rule(1, [("has_some", [1], 0)], add=[9], keep=True)]
        self.assertEqual(settle(sync, {1}, {9}).final, {1, 9})
        self.assertEqual(settle(sync, {9}, {9}).final, set())

    async def test_another_matching_rule_keeps_the_role(self):
        rules = [rule(1, [("has_some", [1], 0)], add=[9], keep=True), rule(2, [("has_some", [2], 0)], add=[9])]
        self.assertEqual(settle(rules, {2, 9}, {9}).final, {2, 9})
        self.assertEqual(settle(rules, {9}, {9}).final, set())

    async def test_a_one_way_rule_leaves_the_role(self):
        one_way = [rule(1, [("has_some", [1], 0)], add=[9])]
        self.assertEqual(settle(one_way, {9}, {9}).final, {9})
        removing = [rule(1, [("has_some", [1], 0)], remove=[8])]
        self.assertEqual(settle(removing, {1, 8}, {8}).final, {1})

    async def test_rules_cascade(self):
        rules = [rule(2, [("has_some", [8], 0)], add=[9]), rule(1, [("has_some", [1], 0)], add=[8])]
        outcome = settle(rules, {1}, {8, 9})
        self.assertEqual(outcome.final, {1, 8, 9})
        self.assertEqual(outcome.why, {8: (True, "r1"), 9: (True, "r2")})

    def chain(self, length: int) -> list[Rule]:
        """Listed backwards, so each pass moves the chain one step."""
        return [rule(step, [("has_some", [100 + step - 1], 0)], add=[100 + step]) for step in range(length, 0, -1)]

    async def test_the_pass_limit(self):
        self.assertEqual(MAX_PASSES, 10)
        steps = 9  # nine changes and a tenth pass that changes nothing settle, and a tenth change doesn't
        roles = {100 + step for step in range(1, steps + 2)}
        self.assertTrue(settle(self.chain(steps), {100}, roles).settled)
        self.assertFalse(settle(self.chain(steps + 1), {100}, roles | {100 + steps + 1}).settled)

    async def test_rules_that_undo_each_other_change_nothing(self):
        rules = [rule(1, [("lacks_all", [9], 0)], add=[9]), rule(2, [("has_some", [9], 0)], remove=[9])]
        outcome = settle(rules, {1}, {9})
        self.assertFalse(outcome.settled)
        self.assertEqual(outcome.final, {1})
        self.assertEqual(outcome.looping, {1, 2})

    async def test_only_roles_the_bot_can_change_move(self):
        rules = [rule(1, [("has_some", [1], 0)], add=[8, 9])]
        self.assertEqual(settle(rules, {1}, {9}).final, {1, 9})


class ValidationTest(BotTest):
    async def test_a_role_cant_be_added_and_removed(self):
        with self.assertRaisesRegex(Exception, "both add and remove"):
            rule_list([row("x", [("has_some", [111111111111111111], 0)], add=[222222222222222222], remove=[222222222222222222])])

    async def test_names_are_unique_and_ids_are_given(self):
        rows = rule_list([row("A", [("has_some", [111111111111111111], 0)], add=[222222222222222222]), row("B", [("has_some", [111111111111111111], 0)], add=[222222222222222222], rule_id=7)])
        self.assertEqual([r["id"] for r in rows], [8, 7])
        with self.assertRaisesRegex(Exception, "Two rules are called"):
            rule_list([row("A", [("has_some", [111111111111111111], 0)], add=[222222222222222222]), row("a", [("has_some", [111111111111111111], 0)], add=[222222222222222222])])

    async def test_limits(self):
        cond = ("has_some", [111111111111111111], 0)
        with self.assertRaisesRegex(Exception, "at most 10 conditions"):
            rule_list([row("A", [cond] * 11, add=[222222222222222222])])
        with self.assertRaisesRegex(Exception, "a role to add or remove"):
            rule_list([row("A", [cond])])
        with self.assertRaisesRegex(Exception, "at least one condition"):
            rule_list([row("A", [], add=[222222222222222222])])
        with self.assertRaisesRegex(Exception, "from 0 to 250"):
            rule_list([row("A", [("at_least", [111111111111111111], 251)], add=[222222222222222222])])

    async def test_switching_on_clears_the_pause_note(self):
        paused = {**row("A", [("has_some", [111111111111111111], 0)], add=[222222222222222222], enabled=False), "paused": "looped"}
        self.assertEqual(rule_list([paused])[0]["paused"], "looped")
        self.assertEqual(rule_list([{**paused, "enabled": True}])[0]["paused"], "")

    async def test_old_role_sets_read_back(self):
        made = rule_list([from_old("Set", 5_000_000_000_000_000, [6_000_000_000_000_000], 7_000_000_000_000_000, 8_000_000_000_000_000)])[0]
        self.assertEqual(old_shape(made), {"target": 5_000_000_000_000_000, "tracked": [6_000_000_000_000_000], "ignored": 7_000_000_000_000_000, "required": 8_000_000_000_000_000})
        self.assertIsNone(old_shape({**made, "keep_in_sync": False}))
        self.assertIsNone(old_shape({**made, "conditions": [*made["conditions"], {"type": "at_least", "roles": [1], "count": 2}]}))


class Base(BotTest):
    modules = [RoleRules]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.patches = [mock.patch.object(module, "SPACING", 0)]
        for patch in self.patches:
            patch.start()
        self.rules = self.cog(RoleRules)
        self.tracked = self.guild.role("Tracked", 3)
        self.other = self.guild.role("Other", 3)
        self.target = self.guild.role("Target", 4)
        self.extra = self.guild.role("Extra", 4)
        self.ignored = self.guild.role("Ignored", 3)
        self.high = self.guild.role("High", 6)

    async def asyncTearDown(self) -> None:
        await self.rules.cog_unload()
        for patch in self.patches:
            patch.stop()
        await super().asyncTearDown()

    async def idle(self) -> None:
        for _ in range(3000):
            passing = self.rules._passes.get(self.guild.id)
            queue = self.rules._queues.get(self.guild.id)
            working = queue is not None and (queue.live or queue.bulk or (queue.task is not None and not queue.task.done()))
            if (passing is None or passing.done()) and not working:
                return
            await asyncio.sleep(0.005)
        self.fail("role rules never went idle")

    async def save(self, *rows: dict, **more) -> None:
        await self.bot.settings.save(self.guild.id, "rolerules", {"rules": list(rows), **more})
        await self.idle()

    def sync_rule(self, name: str = "Set", **kwargs) -> dict:
        return row(name, [("has_some", [self.tracked.id], 0)], add=[self.target.id], keep=True, **kwargs)

    def edits(self, member) -> list:
        """Each change the rules sent for this member: one role added or removed, or an edit of the whole list."""
        return [call for call in member.calls if call[0] in ("edit", "add_roles", "remove_roles")]

    async def put_rules(self, client, rules: list, **more):
        """A save from the page, which names the version of the rules it was made from."""
        version = (await (await client.get(self.path("/rolerules"))).json())["version"]
        return await client.put(self.path("/rolerules"), json={"rules": rules, "version": version, **more})


class ApplyTest(Base):
    async def test_one_edit_for_the_whole_difference(self):
        holder = FakeMember(self.guild, "holder", self.tracked, self.ignored)
        await self.save(row("Both", [("has_some", [self.tracked.id], 0)], add=[self.target.id, self.extra.id], remove=[self.ignored.id]))
        edits = self.edits(holder)
        self.assertEqual(len(edits), 1)
        self.assertEqual({r.id for r in edits[0][1]["roles"]}, {self.tracked.id, self.target.id, self.extra.id})
        self.assertEqual(edits[0][1]["reason"], "Role rules: Both")
        self.assertEqual([call for call in holder.calls if call[0] in ("add_roles", "remove_roles")], [])

    async def test_keep_in_sync_takes_the_role_back(self):
        holder = FakeMember(self.guild, "holder", self.tracked)
        await self.save(self.sync_rule())
        self.assertIn(self.target, holder.roles)
        holder.roles.remove(self.tracked)
        self.rules.enqueue(self.guild.id, [holder.id], live=True)
        await self.idle()
        self.assertNotIn(self.target, holder.roles)

    async def test_a_stale_copy_of_the_member_brings_no_second_edit(self):
        holder = FakeMember(self.guild, "holder", self.tracked)
        await self.save(self.sync_rule())
        self.assertEqual(len(self.edits(holder)), 1)
        holder.roles.remove(self.target)  # the cache as it stood before the edit, which the gateway hasn't replaced yet
        self.rules.enqueue(self.guild.id, [holder.id], live=True)
        await self.idle()
        self.assertEqual(len(self.edits(holder)), 1)

    async def test_a_one_way_rule_leaves_its_role(self):
        holder = FakeMember(self.guild, "holder", self.tracked)
        await self.save(row("Once", [("has_some", [self.tracked.id], 0)], add=[self.target.id]))
        holder.roles.remove(self.tracked)
        self.rules.enqueue(self.guild.id, [holder.id], live=True)
        await self.idle()
        self.assertIn(self.target, holder.roles)

    async def test_members_on_the_rules_screen_and_the_bot_are_left_alone(self):
        waiting = FakeMember(self.guild, "waiting", self.tracked, pending=True)
        self.me.roles.append(self.tracked)
        await self.save(self.sync_rule())
        self.assertEqual(self.edits(waiting), [])
        self.assertEqual(self.edits(self.me), [])

    async def test_a_member_on_the_rules_screen_is_never_edited(self):
        waiting = FakeMember(self.guild, "waiting", self.tracked, pending=True)
        with mock.patch.object(RoleRules, "schedule_pass"):
            await self.bot.settings.save(self.guild.id, "rolerules", {"rules": [self.sync_rule()]})
        self.assertFalse(await self.rules.apply(self.guild, waiting.id))
        self.assertEqual(self.edits(waiting), [])
        waiting.pending = False
        self.assertTrue(await self.rules.apply(self.guild, waiting.id))

    async def test_rules_that_undo_each_other_are_paused_and_staff_hear_once(self):
        holder = FakeMember(self.guild, "holder", self.tracked)
        with self.assertLogs(LOG, logging.WARNING) as logged:
            await self.save(row("Give", [("lacks_all", [self.target.id], 0)], add=[self.target.id]), row("Take", [("has_some", [self.target.id], 0)], remove=[self.target.id]))
        self.assertEqual(self.edits(holder), [])
        saved = self.settings("rolerules")["rules"]
        self.assertEqual([r["enabled"] for r in saved], [False, False])
        self.assertIn("Give, Take", saved[0]["paused"])
        self.assertEqual(len([line for line in logged.output if "undo each other" in line]), 1)

    async def test_changes_are_paced(self):
        for index in range(4):
            FakeMember(self.guild, f"m{index}", self.tracked)
        with mock.patch.object(module, "SPACING", 0.15):
            started = time.monotonic()
            await self.save(self.sync_rule())
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.45)

    async def test_a_role_moved_above_the_bot_is_skipped_and_staff_hear_once(self):
        await self.save(row("Two", [("has_some", [self.tracked.id], 0)], add=[self.target.id, self.extra.id]))
        self.target.position = 20
        holder = FakeMember(self.guild, "holder", self.tracked)
        with self.assertLogs(LOG, logging.WARNING) as logged:
            self.rules.enqueue(self.guild.id, [holder.id], live=True)
            await self.idle()
            other = FakeMember(self.guild, "other", self.tracked)
            self.rules.enqueue(self.guild.id, [other.id], live=True)
            await self.idle()
        self.assertEqual(len([line for line in logged.output if "above the bot" in line]), 1)
        self.assertIn(self.extra, holder.roles)
        self.assertNotIn(self.target, holder.roles)
        self.assertTrue(self.settings("rolerules")["rules"][0]["enabled"])

    async def test_a_deleted_role_leaves_the_rule_and_one_with_nothing_left_pauses(self):
        holder = FakeMember(self.guild, "holder", self.tracked)
        await self.save(row("Two", [("has_some", [self.tracked.id], 0)], add=[self.target.id, self.extra.id]), row("Lone", [("has_some", [self.tracked.id], 0)], add=[self.other.id], keep=True))
        self.guild.roles.remove(self.extra)
        await self.rules.on_guild_role_delete(self.extra)
        rules = self.settings("rolerules")["rules"]
        self.assertTrue(rules[0]["enabled"])
        self.assertIn(self.extra.id, rules[0]["add"])
        self.guild.roles.remove(self.other)
        holder.roles = [r for r in holder.roles if r is not self.other]
        with self.assertLogs(LOG, logging.WARNING):
            await self.rules.on_guild_role_delete(self.other)
        rules = self.settings("rolerules")["rules"]
        self.assertTrue(rules[0]["enabled"])
        self.assertFalse(rules[1]["enabled"])
        self.assertEqual(rules[1]["paused"], GONE_NOTE)

    async def test_a_deleted_role_in_a_condition_pauses_the_rule_and_staff_hear(self):
        required = self.guild.role("Required", 3)
        holders = [FakeMember(self.guild, f"m{index}", self.tracked, required) for index in range(3)]
        await self.save(row("Set", [("has_some", [self.tracked.id], 0), ("has_all", [required.id], 0)], add=[self.target.id], keep=True))
        self.assertTrue(all(self.target in member.roles for member in holders))
        self.guild.roles.remove(required)
        for member in holders:
            member.roles.remove(required)  # what the cache holds after a restart rebuilds it
        with self.assertLogs(LOG, logging.WARNING) as logged:
            await self.rules.on_guild_role_delete(required)
        rule = self.settings("rolerules")["rules"][0]
        self.assertEqual((rule["enabled"], rule["paused"]), (False, module.CONDITION_GONE_NOTE))
        self.assertTrue(any("checks for was deleted" in line for line in logged.output))
        await self.rules.resume()
        await self.idle()
        self.assertTrue(all(self.target in member.roles for member in holders), "a paused rule takes nothing")

    async def test_a_live_change_pauses_a_rule_whose_condition_role_is_gone(self):
        required = self.guild.role("Required", 3)
        holder = FakeMember(self.guild, "holder", self.tracked, required, self.target)
        with mock.patch.object(RoleRules, "schedule_pass"):
            await self.bot.settings.save(self.guild.id, "rolerules", {"rules": [row("Set", [("has_some", [self.tracked.id], 0), ("has_all", [required.id], 0)], add=[self.target.id], keep=True)]})
        self.guild.roles.remove(required)  # deleted while the bot was away, so no delete event came
        holder.roles.remove(required)
        with self.assertLogs(LOG, logging.WARNING):
            self.assertFalse(await self.rules.apply(self.guild, holder.id))
        self.assertIn(self.target, holder.roles)
        self.assertFalse(self.settings("rolerules")["rules"][0]["enabled"])

    async def test_rules_with_long_names_that_undo_each_other_are_still_paused(self):
        bystander = FakeMember(self.guild, "bystander", self.other)
        FakeMember(self.guild, "holder", self.tracked)
        give, take = "G" * 98, "T" * 98
        with self.assertLogs(LOG, logging.WARNING):
            await self.save(row(give, [("has_some", [self.tracked.id], 0), ("lacks_all", [self.target.id], 0)], add=[self.target.id]), row(take, [("has_some", [self.target.id], 0)], remove=[self.target.id]),
                            row("Unrelated", [("has_some", [self.other.id], 0)], add=[self.extra.id]))
        rules = self.settings("rolerules")["rules"]
        self.assertEqual([r["enabled"] for r in rules], [False, False, True])
        self.assertLessEqual(len(rules[0]["paused"]), 300)
        self.assertIn(self.extra, bystander.roles)

    async def test_a_pause_note_names_what_fits(self):
        note = module.loop_note(["x" * 100, "y" * 100, "z" * 100])
        self.assertLessEqual(len(note), 300)
        self.assertIn("and 2 more", note)
        self.assertIn("A, B", module.loop_note(["A", "B"]))

    async def test_a_failed_pause_still_queues_the_other_rules_members(self):
        bystander = FakeMember(self.guild, "bystander", self.other)
        FakeMember(self.guild, "holder", self.tracked)
        with mock.patch.object(RoleRules, "_pause_looping", mock.AsyncMock(side_effect=RuntimeError("saving failed"))), self.assertLogs(LOG, logging.ERROR):
            await self.save(row("Give", [("has_some", [self.tracked.id], 0), ("lacks_all", [self.target.id], 0)], add=[self.target.id]), row("Take", [("has_some", [self.target.id], 0)], remove=[self.target.id]),
                            row("Unrelated", [("has_some", [self.other.id], 0)], add=[self.extra.id]))
        self.assertIn(self.extra, bystander.roles)

    async def test_a_quick_revert_after_the_gateway_shows_the_edit_is_put_right(self):
        holder = FakeMember(self.guild, "holder", self.tracked)
        await self.save(self.sync_rule())
        self.assertIn(self.target, holder.roles)
        before = copy.copy(holder)
        before.roles = list(holder.roles)  # the gateway's copy shows the bot's edit
        holder.roles.remove(self.target)  # and then a moderator takes it off a moment later
        await self.rules.on_member_update(before, holder)
        await self.idle()
        self.assertIn(self.target, holder.roles)
        self.assertEqual(len(self.edits(holder)), 2)

    async def test_the_log_channel_gets_one_line_per_change(self):
        logs = FakeChannel(self.guild, "role-log")
        holder = FakeMember(self.guild, "holder", self.tracked, self.ignored)
        await self.save(row("Both", [("has_some", [self.tracked.id], 0)], add=[self.target.id], remove=[self.ignored.id]), log_channel=str(logs.id))
        channel_id, embed, _ = self.bot.logs.posted[-1]
        self.assertEqual(channel_id, logs.id)
        self.assertEqual(embed["description"].splitlines(), [f"Added <@&{self.target.id}> to {holder.mention} (Both).", f"Removed <@&{self.ignored.id}> from {holder.mention} (Both)."])

    async def test_kept_in_sync_roles_are_transient(self):
        await self.save(self.sync_rule(), row("Once", [("has_some", [self.tracked.id], 0)], add=[self.extra.id]))
        self.assertEqual(self.bot.transient_roles(self.guild.id), {self.target.id})


class QueueTest(Base):
    async def test_a_restart_picks_the_sync_up(self):
        holders = [FakeMember(self.guild, f"m{index}", self.tracked) for index in range(3)]
        with mock.patch.object(RoleRules, "schedule_pass"):
            await self.bot.settings.save(self.guild.id, "rolerules", {"rules": [self.sync_rule()]})
        self.assertTrue(all(self.target not in member.roles for member in holders))
        post = await self.channel.send("progress")
        await self.db.run("INSERT INTO role_rule_jobs (guild_id, user_id, channel_id, message_id, total, done, changed, started_at, saved_at) VALUES (?, ?, ?, ?, 10, 7, 7, ?, ?)",
                          (self.guild.id, self.admin.id, self.channel.id, post.id, time.time(), time.time()))
        await self.rules.resume()
        await self.idle()
        self.assertTrue(all(self.target in member.roles for member in holders))
        self.assertIsNone(await self.db.one("SELECT * FROM role_rule_jobs"))
        self.assertIn("Finished.", post.embeds[0].description)
        self.assertIn("10 of 10 members", post.embeds[0].description)

    async def test_a_sync_counts_who_it_changed(self):
        for index in range(3):
            FakeMember(self.guild, f"m{index}", self.tracked)
        with mock.patch.object(RoleRules, "schedule_pass"):
            await self.bot.settings.save(self.guild.id, "rolerules", {"rules": [self.sync_rule()]})
        job = await self.rules.start_sync(self.guild, self.admin.id, self.channel)
        self.assertEqual(job.total, 3)
        self.assertEqual(await self.rules.start_sync(self.guild, self.admin.id), "A sync is already running in this server.")
        await self.idle()
        self.assertEqual((job.done, job.changed), (3, 3))
        self.assertIsNone(self.rules.job_json(self.guild.id))

    async def test_a_sync_started_while_the_worker_runs_keeps_its_job(self):
        live = FakeMember(self.guild, "live", self.tracked)
        others = [FakeMember(self.guild, f"m{index}", self.tracked) for index in range(3)]
        with mock.patch.object(RoleRules, "schedule_pass"):
            await self.bot.settings.save(self.guild.id, "rolerules", {"rules": [self.sync_rule()]})
        original = RoleRules.needing

        async def slow_needing(cog, guild, *args):
            await asyncio.sleep(0.2)  # a large server takes a moment to look over, and the worker empties its queue meanwhile
            return await original(cog, guild, *args)
        with mock.patch.object(RoleRules, "needing", slow_needing):
            self.rules.enqueue(self.guild.id, [live.id], live=True)
            job = await self.rules.start_sync(self.guild, self.admin.id, self.channel)
            await self.idle()
        self.assertTrue(all(self.target in member.roles for member in others))
        self.assertEqual(job.done, job.total, "the sync counts every member it was given")
        self.assertGreaterEqual(job.total, len(others))
        self.assertIsNone(await self.db.one("SELECT * FROM role_rule_jobs WHERE guild_id = ?", (self.guild.id,)))
        self.assertIn("Finished.", self.channel.sent[-1].embeds[0].description)

    async def test_a_failed_live_change_stays_ahead_of_the_pass(self):
        order = []
        failed = set()

        async def apply(cog, guild, user_id):
            order.append(user_id)
            if user_id == 999 and user_id not in failed:
                failed.add(user_id)
                raise module.Again("503 Service Unavailable")
            return False
        with mock.patch.object(RoleRules, "apply", apply), mock.patch.object(RoleRules, "_start_worker"), mock.patch.object(module, "OUTAGE_PAUSE", 0):
            self.rules.enqueue(self.guild.id, list(range(1, 201)))
            self.rules.enqueue(self.guild.id, [999], live=True)
            with mock.patch.object(module.asyncio, "sleep", mock.AsyncMock()):
                await self.rules._work(self.guild.id, self.rules._queue(self.guild.id))
        self.assertEqual(order[:2], [999, 999])

    async def test_a_live_change_goes_ahead_of_a_full_pass(self):
        order = []
        original = RoleRules.apply

        async def recording(cog, guild, user_id):
            order.append(user_id)
            return await original(cog, guild, user_id)
        with mock.patch.object(RoleRules, "apply", recording), mock.patch.object(RoleRules, "_start_worker"):
            self.rules.enqueue(self.guild.id, [1, 2, 3])
            self.rules.enqueue(self.guild.id, [9], live=True)
        queue = self.rules._queue(self.guild.id)
        with mock.patch.object(RoleRules, "apply", recording):
            await self.rules._work(self.guild.id, queue)
        self.assertEqual(order, [9, 1, 2, 3])

    async def test_a_loading_member_list_changes_nobody(self):
        holder = FakeMember(self.guild, "holder", self.tracked)
        with mock.patch.object(module, "whole_member_list", mock.AsyncMock(return_value=False)):
            await self.save(self.sync_rule())
            self.assertEqual(await self.rules.start_sync(self.guild, self.admin.id), module.NOT_READY)
        self.assertEqual(self.edits(holder), [])


class CommandTest(Base):
    def flags(self, tracked: str, ignored=None, required=None) -> CreateFlags:
        flags = CreateFlags.__new__(CreateFlags)
        flags.tracked, flags.ignored, flags.required = tracked, ignored, required
        return flags

    async def create(self, ctx, name="Set", target=None, tracked=None, **flags) -> None:
        await self.rules.rolerule_create.callback(self.rules, ctx, name, target or self.target, flags=self.flags(tracked or self.tracked.mention, **flags))
        await self.idle()

    async def test_create_makes_a_kept_in_sync_rule(self):
        holder = FakeMember(self.guild, "holder", self.tracked)
        ctx = self.context()
        await self.create(ctx, tracked=f"{self.tracked.mention} Other", ignored=self.ignored)
        saved = self.settings("rolerules")["rules"][0]
        self.assertEqual(old_shape(saved), {"target": self.target.id, "tracked": [self.tracked.id, self.other.id], "ignored": self.ignored.id, "required": None})
        self.assertIn(self.target, holder.roles)
        self.assertEqual(ctx.fails, [])

    async def test_the_old_edits(self):
        ctx = self.context()
        await self.create(ctx)
        cog = self.rules
        await cog.rolerule_add_tracked.callback(cog, ctx, "set", roles="Other")
        await cog.rolerule_set_ignored.callback(cog, ctx, "Set", self.ignored)
        await cog.rolerule_set_required.callback(cog, ctx, "Set", self.extra)
        await cog.rolerule_change_target.callback(cog, ctx, "Set", self.extra)
        shape = old_shape(self.settings("rolerules")["rules"][0])
        self.assertEqual(shape, {"target": self.extra.id, "tracked": [self.tracked.id, self.other.id], "ignored": self.ignored.id, "required": self.extra.id})
        await cog.rolerule_remove_tracked.callback(cog, ctx, "Set", roles="Tracked")
        await cog.rolerule_clear_ignored.callback(cog, ctx, name="Set")
        await cog.rolerule_clear_required.callback(cog, ctx, name="Set")
        shape = old_shape(self.settings("rolerules")["rules"][0])
        self.assertEqual(shape, {"target": self.extra.id, "tracked": [self.other.id], "ignored": None, "required": None})
        await cog.rolerule_remove_tracked.callback(cog, ctx, "Set", roles="Other")
        self.assertIn("at least one tracked role", ctx.fails[-1])
        await cog.rolerule_disable.callback(cog, ctx, name="Set")
        self.assertFalse(self.settings("rolerules")["rules"][0]["enabled"])
        await cog.rolerule_enable.callback(cog, ctx, name="Set")
        self.assertTrue(self.settings("rolerules")["rules"][0]["enabled"])
        await cog.rolerule_delete.callback(cog, ctx, name="SET")
        self.assertEqual(self.settings("rolerules")["rules"], [])

    async def test_a_rule_the_old_shape_cant_describe_is_refused(self):
        await self.save(row("Counted", [("at_least", [self.tracked.id, self.other.id], 2)], add=[self.target.id], keep=True))
        ctx = self.context()
        await self.rules.rolerule_add_tracked.callback(self.rules, ctx, "Counted", roles="Other")
        self.assertIn("dashboard's Role rules page", ctx.fails[-1])

    async def test_rank_and_safety_on_commands(self):
        mod = self.mod
        mod.roles.append(self.guild.role("Manager", 5, "manage_roles"))
        ctx = self.context(author=mod)
        await self.create(ctx, target=self.high)
        self.assertIn("at or above your highest role", ctx.fails[-1])
        await self.create(ctx, target=self.role_admin)
        self.assertIn("bot's highest role", ctx.fails[-1])
        await self.create(ctx, target=self.guild.default_role)
        self.assertIn("@everyone", ctx.fails[-1])
        elevated = self.guild.role("Helpers", 3, "manage_messages")
        await self.create(ctx, target=elevated)
        self.assertIn("elevated roles", ctx.fails[-1])
        self.assertEqual(self.settings("rolerules")["rules"], [])
        admin = self.context()
        await self.create(admin, target=self.high)
        self.assertEqual(admin.fails, [])

    async def test_changing_who_an_admins_rule_reaches_counts_as_handing_it_out(self):
        await self.save(row("High", [("has_some", [self.tracked.id], 0)], add=[self.high.id], keep=True))
        mod = self.mod
        mod.roles.append(self.guild.role("Manager", 5, "manage_roles"))
        ctx = self.context(author=mod)
        await self.rules.rolerule_disable.callback(self.rules, ctx, name="High")
        self.assertEqual(ctx.fails, [])
        await self.rules.rolerule_enable.callback(self.rules, ctx, name="High")
        self.assertIn("at or above your highest role", ctx.fails[-1])

    async def test_the_log_channel_needs_posting_rights(self):
        mod = self.mod
        mod.roles.append(self.guild.role("Manager", 5, "manage_roles"))
        hidden = FakeChannel(self.guild, "staff")
        ctx = self.context(author=mod)
        with mock.patch.object(FakeChannel, "permissions_for", lambda channel, member: discord.Permissions.none() if channel is hidden else discord.Permissions.all()):
            await self.rules.rolerule_logchannel.callback(self.rules, ctx, hidden)
        self.assertEqual(ctx.fails[-1], "You can't see that channel.")
        self.assertIsNone(self.settings("rolerules")["log_channel"])

    async def test_a_save_past_200_members_asks_first(self):
        for index in range(CONFIRM_OVER + 1):
            FakeMember(self.guild, f"m{index}", self.tracked)
        ctx = self.context()
        task = asyncio.create_task(self.create(ctx))
        while not any(sent.get("view") for sent in ctx.sent) and not task.done():
            await asyncio.sleep(0.01)
        prompt = next(sent for sent in ctx.sent if sent.get("view"))
        self.assertIn(f"{CONFIRM_OVER + 1:,} members", prompt["content"])
        prompt["view"].stop()
        await task
        self.assertEqual(self.settings("rolerules")["rules"], [])
        self.assertIn("Nothing was saved.", ctx.fails)

    async def test_a_save_refuses_when_the_rules_changed_during_the_wait(self):
        for index in range(CONFIRM_OVER + 1):
            FakeMember(self.guild, f"m{index}", self.tracked)
        ctx = self.context()
        task = asyncio.create_task(self.rules.rolerule_create.callback(self.rules, ctx, "Big", self.target, flags=self.flags(self.tracked.mention)))
        while not any(sent.get("view") for sent in ctx.sent) and not task.done():
            await asyncio.sleep(0.01)
        await self.bot.settings.save(self.guild.id, "rolerules", {"rules": [row("Dashboard rule", [("has_some", [self.other.id], 0)], add=[self.extra.id])]})
        view = next(sent for sent in ctx.sent if sent.get("view"))["view"]
        view.answer = True
        view.stop()
        await task
        await self.idle()
        self.assertEqual([r["name"] for r in self.settings("rolerules")["rules"]], ["Dashboard rule"])
        self.assertEqual(ctx.fails[-1], module.CHANGED_MEANWHILE)

    async def test_delete_asks_first_and_says_who_it_changes(self):
        for index in range(CONFIRM_OVER + 1):
            FakeMember(self.guild, f"m{index}", self.tracked)
        await self.save(self.sync_rule("From tracked"), self.sync_rule("From other") | {"conditions": [{"type": "has_some", "roles": [str(self.other.id)], "count": 0}]})
        ctx = self.context()
        task = asyncio.create_task(self.rules.rolerule_delete.callback(self.rules, ctx, name="From tracked"))
        while not any(sent.get("view") for sent in ctx.sent) and not task.done():
            await asyncio.sleep(0.01)
        prompt = next(sent for sent in ctx.sent if sent.get("view"))
        self.assertIn(f"{CONFIRM_OVER + 1:,} members", prompt["content"])
        prompt["view"].answer = True
        prompt["view"].stop()
        await task
        self.assertEqual([r["name"] for r in self.settings("rolerules")["rules"]], ["From other"])
        self.assertIn(f"changes the roles of {CONFIRM_OVER + 1:,} members", ctx.oks[-1])
        quiet = self.context()
        await self.rules.rolerule_delete.callback(self.rules, quiet, name="From other")
        self.assertIn("Roles it gave stay where they are.", quiet.oks[-1])

    async def test_a_tracked_name_several_roles_share_is_refused(self):
        wanted = self.guild.role("Helper", 3)
        self.guild.role("Helper", 3)
        ctx = self.context()
        await self.create(ctx, tracked="Helper")
        self.assertIn("Several roles are called Helper", ctx.fails[-1])
        self.assertEqual(self.settings("rolerules")["rules"], [])
        await self.create(ctx, tracked=wanted.mention)
        self.assertEqual(self.settings("rolerules")["rules"][0]["conditions"][0]["roles"], [wanted.id])

    async def test_a_typed_target_several_roles_share_is_refused(self):
        twin = self.guild.role("Twin", 3)
        self.guild.role("Twin", 3)
        ctx = self.context()
        ctx.message.content = f"!rolerule create Set Twin {self.tracked.mention}"
        await self.create(ctx, target=twin)
        self.assertIn("Several roles are called Twin", ctx.fails[-1])
        ctx.message.content = f"!rolerule create Set {twin.id} {self.tracked.mention}"
        await self.create(ctx, target=twin)
        self.assertEqual(old_shape(self.settings("rolerules")["rules"][0])["target"], twin.id)
        ctx.message.content = "!rolerule change_target Set Twin"
        await self.rules.rolerule_change_target.callback(self.rules, ctx, "Set", twin)
        self.assertIn("Several roles are called Twin", ctx.fails[-1])

    async def test_sync_says_which_rules_it_switched_off(self):
        FakeMember(self.guild, "holder", self.tracked)
        with mock.patch.object(RoleRules, "schedule_pass"):
            await self.bot.settings.save(self.guild.id, "rolerules", {"rules": [row("Give", [("lacks_all", [self.target.id], 0)], add=[self.target.id]), row("Take", [("has_some", [self.target.id], 0)], remove=[self.target.id])]})
        ctx = self.context()
        with self.assertLogs(LOG, logging.WARNING):
            await self.rules.rolerule_sync.callback(self.rules, ctx)
        self.assertEqual(ctx.oks, [])
        self.assertIn("undo each other's changes: Give, Take", ctx.fails[-1])
        self.assertIsNone(self.rules.job_json(self.guild.id))

    async def test_test_says_who_matches(self):
        holder = FakeMember(self.guild, "holder", self.tracked)
        FakeMember(self.guild, "other", self.tracked)
        await self.save(self.sync_rule())
        ctx = self.context()
        await self.rules.rolerule_test.callback(self.rules, ctx, "Set", holder)
        text = ctx.sent[-1]["embed"].description
        self.assertIn(f"{holder.mention} matches Set.", text)
        self.assertIn("2 members match it now.", text)

    async def test_the_old_names_reach_the_new_commands(self):
        for old, new in (("create_roleset", "rolerule create"), ("list_rolesets", "rolerule"), ("delete_roleset", "rolerule delete"), ("add_tracked_roles", "rolerule add_tracked"),
                         ("remove_tracked_roles", "rolerule remove_tracked"), ("change_target_role", "rolerule change_target"), ("add_ignored_role", "rolerule set_ignored"),
                         ("remove_ignored_role", "rolerule clear_ignored"), ("add_required_role", "rolerule set_required"), ("remove_required_role", "rolerule clear_required"), ("sync_roles", "rolerule sync")):
            self.assertEqual(expand_line(self.bot, f"{old} x"), f"{new} x")
            self.assertIsNotNone(self.bot.get_command(new))


class PageTest(Base):
    async def test_preview_counts_and_names(self):
        holders = [FakeMember(self.guild, f"m{index}", self.tracked, self.ignored) for index in range(25)]
        client = await self.client()
        response = await client.post(self.path("/rolerules/preview"), json={"rules": [row("Both", [("has_some", [self.tracked.id], 0)], add=[self.target.id], remove=[self.ignored.id])]})
        self.assertEqual(response.status, 200)
        report = await response.json()
        self.assertEqual(report["members"], 25)
        by_role = {entry["role_id"]: entry for entry in report["roles"]}
        self.assertEqual((by_role[str(self.target.id)]["gain"], by_role[str(self.target.id)]["lose"]), (25, 0))
        self.assertEqual(by_role[str(self.ignored.id)]["lose"], 25)
        self.assertEqual(len(by_role[str(self.target.id)]["gain_names"]), 20)
        self.assertEqual(all(self.target not in member.roles for member in holders), True, "a preview changes nobody")

    async def test_saving_past_200_members_needs_the_count(self):
        for index in range(CONFIRM_OVER + 1):
            FakeMember(self.guild, f"m{index}", self.tracked)
        client = await self.client()
        rules = [self.sync_rule()]
        response = await self.put_rules(client, rules)
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["fields"]["confirm"], str(CONFIRM_OVER + 1))
        self.assertEqual(self.settings("rolerules")["rules"], [])
        response = await self.put_rules(client, rules, confirm=CONFIRM_OVER + 1)
        self.assertEqual(response.status, 200)
        await self.idle()
        self.assertEqual(len(self.settings("rolerules")["rules"]), 1)

    async def test_rank_and_safety_on_the_page(self):
        self.admin.roles = [self.guild.default_role, self.guild.role("Manager", 5, "manage_guild", "manage_roles")]
        client = await self.client()
        refusals = (
            (self.high, "at or above your highest role"), (self.role_admin, "bot's highest role"),
            (self.guild.role("Helpers", 3, "manage_messages"), "elevated roles"), (self.guild.default_role, "@everyone"),
        )
        for role, words in refusals:
            response = await self.put_rules(client, [row("R", [("has_some", [self.tracked.id], 0)], add=[role.id])])
            self.assertEqual(response.status, 400, words)
            self.assertIn(words, (await response.json())["fields"]["rules"])
        response = await self.put_rules(client, [row("R", [("has_some", [self.tracked.id], 0)], add=[self.target.id])])
        self.assertEqual(response.status, 200)

    async def test_the_page_cant_point_an_admins_rule_at_different_people(self):
        await self.save(row("High", [("has_some", [self.tracked.id], 0)], add=[self.high.id], keep=True, rule_id=1))
        self.admin.roles = [self.guild.default_role, self.guild.role("Manager", 5, "manage_guild", "manage_roles")]
        client = await self.client()
        moved = row("High", [("has_some", [self.other.id], 0)], add=[self.high.id], keep=True, rule_id=1)
        response = await self.put_rules(client, [moved])
        self.assertEqual(response.status, 400)
        renamed = row("Higher", [("has_some", [self.tracked.id], 0)], add=[self.high.id], keep=True, rule_id=1)
        response = await self.put_rules(client, [renamed])
        self.assertEqual(response.status, 200)

    async def test_moving_a_role_between_the_lists_counts_as_handing_it_out(self):
        for first, then in (("remove", "add"), ("add", "remove")):
            await self.bot.settings.save(self.guild.id, "rolerules", {"rules": [row("High", [("has_some", [self.tracked.id], 0)], rule_id=1, **{first: [self.high.id]})]})
            await self.idle()
            self.admin.roles = [self.guild.default_role, self.guild.role("Manager", 5, "manage_guild", "manage_roles")]
            client = await self.client()
            response = await self.put_rules(client, [row("High", [("has_some", [self.tracked.id], 0)], rule_id=1, **{then: [self.high.id]})])
            self.assertEqual(response.status, 400, f"{first} to {then}")
            self.assertIn("at or above your highest role", (await response.json())["fields"]["rules"])
            self.assertEqual(self.settings("rolerules")["rules"][0][first], [self.high.id])
            self.admin.roles = [self.guild.default_role, self.role_admin]

    async def test_the_page_refuses_a_save_made_from_an_older_copy(self):
        await self.save(row("A", [("has_some", [self.tracked.id], 0)], add=[self.target.id], rule_id=1), row("B", [("has_some", [self.other.id], 0)], add=[self.extra.id], rule_id=2))
        client = await self.client()
        loaded = await (await client.get(self.path("/rolerules"))).json()
        await self.save(*loaded["rules"], row("Added elsewhere", [("has_some", [self.ignored.id], 0)], add=[self.extra.id]))
        response = await client.put(self.path("/rolerules"), json={"rules": [loaded["rules"][1], loaded["rules"][0]], "version": loaded["version"]})
        self.assertEqual(response.status, 409)
        self.assertIn("Reload the page", (await response.json())["error"])
        unnamed = await client.put(self.path("/rolerules"), json={"rules": [loaded["rules"][1], loaded["rules"][0]]})
        self.assertEqual(unnamed.status, 409)
        self.assertEqual([r["name"] for r in self.settings("rolerules")["rules"]], ["A", "B", "Added elsewhere"])
        fresh = await (await client.get(self.path("/rolerules"))).json()
        response = await client.put(self.path("/rolerules"), json={"rules": [fresh["rules"][1], fresh["rules"][0], fresh["rules"][2]], "version": fresh["version"]})
        self.assertEqual(response.status, 200)
        self.assertNotEqual((await response.json())["version"], fresh["version"])

    async def test_the_log_channel_on_the_page_needs_posting_rights(self):
        self.admin.roles = [self.guild.default_role, self.guild.role("Manager", 5, "manage_guild", "manage_roles")]
        hidden = FakeChannel(self.guild, "staff")
        client = await self.client()
        with mock.patch.object(FakeChannel, "permissions_for", lambda channel, member: discord.Permissions.none() if channel is hidden else discord.Permissions.all()):
            response = await client.put(self.path("/rolerules"), json={"log_channel": str(hidden.id)})
        self.assertEqual(response.status, 400)

    async def test_sync_now_reports_progress(self):
        for index in range(2):
            FakeMember(self.guild, f"m{index}", self.tracked)
        with mock.patch.object(RoleRules, "schedule_pass"):
            await self.bot.settings.save(self.guild.id, "rolerules", {"rules": [self.sync_rule()]})
        client = await self.client()
        with mock.patch.object(RoleRules, "_start_worker"):
            started = await (await client.post(self.path("/rolerules/sync"))).json()
            self.assertEqual(started["job"], {"running": True, "done": 0, "total": 2, "changed": 0})
            again = await client.post(self.path("/rolerules/sync"))
            self.assertEqual(again.status, 409)
        self.rules._start_worker(self.guild.id)
        await self.idle()
        self.assertEqual(await (await client.get(self.path("/rolerules/sync"))).json(), {"job": None})

    async def test_staff_access_cant_reach_the_routes(self):
        staff = await self.client(access="staff")
        self.assertEqual((await staff.post(self.path("/rolerules/preview"), json={"rules": []})).status, 403)
        self.assertEqual((await staff.put(self.path("/rolerules"), json={"rules": []})).status, 403)
        self.assertEqual((await staff.post(self.path("/rolerules/sync"))).status, 403)


class RealLibraryTest(GatewayTest):
    modules = [RoleRules]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.spacing = mock.patch.object(module, "SPACING", 0)
        self.spacing.start()
        self.cog = next(m for m in self.bot.modules if isinstance(m, RoleRules))

    async def asyncTearDown(self) -> None:
        self.spacing.stop()
        await self.cog.cog_unload()
        await super().asyncTearDown()

    async def idle(self) -> None:
        for _ in range(2000):
            passing = self.cog._passes.get(self.guild_id)
            queue = self.cog._queues.get(self.guild_id)
            working = queue is not None and (queue.live or queue.bulk or (queue.task is not None and not queue.task.done()))
            if (passing is None or passing.done()) and not working:
                await self.settle()
                return
            await asyncio.sleep(0.005)
        self.fail("role rules never went idle")

    def edits(self) -> list:
        """Every role change the rules sent: a PUT or DELETE for one role, or a PATCH of the whole list."""
        return [(method, path, body) for method, path, body in self.net.calls
                if (method == "PATCH" and path == "/guilds/{guild_id}/members/{user_id}") or path == "/guilds/{guild_id}/members/{user_id}/roles/{role_id}"]

    async def rule_on_member_role(self) -> None:
        rules = [row("Mods", [("has_some", [self.role_mod["id"]], 0)], add=[self.role_muted["id"]], keep=True)]
        with mock.patch.object(RoleRules, "schedule_pass"):
            await self.bot.settings.save(self.guild_id, "rolerules", {"rules": rules})

    def member_update(self, user_id: int, roles: list[str], *, pending: bool = False) -> None:
        data = {**self.member_json[user_id], "guild_id": str(self.guild_id), "roles": roles, "pending": pending}
        self.member_json[user_id] = {k: v for k, v in data.items() if k != "guild_id"}
        self.bot._connection.parse_guild_member_update(data)

    async def test_a_role_change_brings_one_edit(self):
        await self.rule_on_member_role()
        self.member_update(self.member_id, [self.role_member["id"], self.role_mod["id"]])
        await self.idle()
        self.assert_no_errors()
        edits = self.edits()
        self.assertEqual([(method, path) for method, path, _ in edits], [("PUT", "/guilds/{guild_id}/members/{user_id}/roles/{role_id}")])

    async def test_several_changes_are_one_edit_from_discords_own_copy(self):
        """The edit replaces the whole list, so it starts from what Discord holds. A role another feature gave a moment ago, which the gateway hasn't reported, stays."""
        rules = [row("Mods", [("has_some", [self.role_mod["id"]], 0)], add=[self.role_muted["id"]], remove=[self.role_member["id"]], keep=True)]
        with mock.patch.object(RoleRules, "schedule_pass"):
            await self.bot.settings.save(self.guild_id, "rolerules", {"rules": rules})
        at_discord = [self.role_member["id"], self.role_mod["id"], self.role_admin["id"]]  # Admin came by its own PUT, still on its way to the gateway
        self.net.members[self.member_id] = {**self.member_json[self.member_id], "roles": at_discord}
        self.member_update(self.member_id, [self.role_member["id"], self.role_mod["id"]])
        self.net.members[self.member_id] = {**self.member_json[self.member_id], "roles": at_discord}
        await self.idle()
        self.assert_no_errors()
        patches = [body for method, path, body in self.edits() if method == "PATCH"]
        self.assertEqual(len(self.edits()), 1)
        self.assertEqual(sorted(map(int, patches[0]["roles"])), sorted(int(r["id"]) for r in (self.role_mod, self.role_admin, self.role_muted)))
        self.assertTrue([1 for method, path, _ in self.net.calls if method == "GET" and path.startswith("/guilds/{guild_id}/members/{")])

    async def test_a_join_past_the_rules_screen(self):
        await self.rule_on_member_role()
        newcomer = gateway_id()
        member = {"user": user_json(newcomer, "newcomer"), "roles": [self.role_mod["id"]], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": True, "nick": None, "avatar": None, "premium_since": None, "communication_disabled_until": None}
        self.member_json[newcomer] = member
        self.net.members[newcomer] = member
        self.bot._connection.parse_guild_member_add({**member, "guild_id": str(self.guild_id)})
        await self.idle()
        self.assertEqual(self.edits(), [])
        self.member_update(newcomer, [self.role_mod["id"]], pending=False)
        await self.idle()
        self.assert_no_errors()
        self.assertEqual(len(self.edits()), 1)

    async def test_create_by_slash_and_by_the_old_typed_name(self):
        data = {"id": str(gateway_id()), "name": "rolerule", "type": 1, "options": [{"name": "create", "type": 1, "options": [
            {"name": "name", "type": 3, "value": "Mods"}, {"name": "target", "type": 8, "value": self.role_muted["id"]},
            {"name": "tracked", "type": 3, "value": f"<@&{self.role_mod['id']}>"}]}], "resolved": {"roles": {self.role_muted["id"]: self.role_muted}}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.owner_id))
        await self.settle()
        await self.idle()
        self.assert_no_errors()
        saved = self.bot.settings.get(self.guild_id, "rolerules")["rules"]
        self.assertEqual(old_shape(saved[0])["target"], int(self.role_muted["id"]))
        self.dispatch_message(user_id=self.owner_id, content=f'!create_roleset Members <@&{self.role_muted["id"]}> <@&{self.role_member["id"]}> ignored: <@&{self.role_mod["id"]}>')
        await self.settle()
        await self.idle()
        self.assert_no_errors()
        saved = self.bot.settings.get(self.guild_id, "rolerules")["rules"]
        self.assertEqual(old_shape(saved[1]), {"target": int(self.role_muted["id"]), "tracked": [int(self.role_member["id"])], "ignored": int(self.role_mod["id"]), "required": None})

    async def test_test_answers_at_once_while_the_member_list_loads(self):
        await self.rule_on_member_role()
        data = {"id": str(gateway_id()), "name": "rolerule", "type": 1, "options": [{"name": "test", "type": 1, "options": [{"name": "name", "type": 3, "value": "Mods"}]}], "resolved": {}}
        with mock.patch.object(module, "whole_member_list", mock.AsyncMock(side_effect=lambda guild: asyncio.sleep(0.3, result=True))):
            self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.owner_id))
            await asyncio.sleep(0.1)
            first = [body for method, path, body in self.net.calls if path.endswith("/callback")]
            self.assertEqual([body["type"] for body in first], [5], "a deferred answer comes before the member list")
            await self.settle()
        self.assert_no_errors()

    async def test_every_subcommand_answers_through_the_library(self):
        await self.rule_on_member_role()
        steps = [("list", []), ("show", [{"name": "name", "type": 3, "value": "Mods"}]), ("test", [{"name": "name", "type": 3, "value": "Mods"}]),
                 ("disable", [{"name": "name", "type": 3, "value": "Mods"}]), ("enable", [{"name": "name", "type": 3, "value": "Mods"}]), ("sync", []), ("logchannel", [])]
        for command, options in steps:
            self.net.calls.clear()
            data = {"id": str(gateway_id()), "name": "rolerule", "type": 1, "options": [{"name": command, "type": 1, "options": options}], "resolved": {}}
            self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.owner_id))
            await self.settle()
            await self.idle()
            self.assert_no_errors(command)
            replies = [body for method, path, body in self.net.calls if "/callback" in path or path.startswith("/webhooks/")]
            self.assertTrue(replies, command)
            failed = [body for body in replies if "\N{CROSS MARK}" in str((body.get("data") or body).get("content"))]
            self.assertEqual(failed, [], command)
