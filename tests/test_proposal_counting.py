"""Proposals when the world isn't tidy: a member list still refilling, a channel gone while the bot was off, a busy vote, a restart in the wrong place."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import discord

from captainguild.core.messages import embed_size
from captainguild.core.ops import OpsError
from captainguild.core.settings import Actor
from captainguild.modules.proposals import APPROVED, CONTENT_MAX, CREDIT_MAX, DOWN, OPEN, REASON_MAX, TIMER, UP, WITHDRAWN
from tests.test_proposals import ProposalCase


class RefillTest(ProposalCase):
    def thin_out(self, *keep: int) -> None:
        for user_id in [*self.mods, *self.people]:
            if user_id not in keep:
                self.guild._members.pop(user_id, None)

    async def test_a_vote_during_the_refill_decides_nothing(self):
        row = await self.propose()
        self.bot.members_settled.clear()
        self.thin_out(self.mods[0])
        self.vote(row, self.mods[0], UP)
        await self.settle(0.2)
        fresh = await self.row()
        self.assertEqual((fresh["mod_done"], fresh["mod_up"]), (0, 0))
        self.assertEqual(await self.cog_.refresh(self.guild_id, 1), False)

    async def test_the_community_vote_doesnt_close_during_the_refill(self):
        row = await self.propose()
        self.votes(row, ups=self.mods[1:], downs=[self.people[0]])
        self.bot.members_settled.clear()
        self.thin_out()
        await self.time_is_up()
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["community_done"], 0)

    async def test_a_list_still_short_after_the_wait_counts_nothing_either(self):
        row = await self.propose()
        self.votes(row, ups=[self.mods[0]])
        self.thin_out(self.mods[0])
        self.guild._member_count = 1000
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["mod_done"], 0)

    async def test_the_count_goes_on_once_the_list_is_back(self):
        row = await self.propose()
        self.votes(row, ups=self.mods)
        self.bot.members_settled.clear()
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["mod_done"], 0)
        self.bot.members_settled.set()
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["mod_done"], 1)

    async def test_a_recheck_during_the_refill_says_nothing_was_counted(self):
        await self.propose()
        self.bot.members_settled.clear()
        result = await self.cog_._recheck(self.guild_id)
        self.assertEqual((result["checked"], result["counted"]), (1, 0))


class GoneWhileOffTest(ProposalCase):
    async def test_a_channel_deleted_while_off_ends_the_vote(self):
        await self.propose()
        self.guild._channels.pop(self.channel_id, None)
        await self.time_is_up()
        await self.cog_._check_due({"guild_id": self.guild_id, "number": 1}, {})
        fresh = await self.row()
        self.assertEqual(fresh["state"], WITHDRAWN)
        self.assertIsNone(await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1"))

    async def test_withdrawing_with_the_channel_gone_stops_retrying(self):
        await self.propose()
        self.guild._channels.pop(self.channel_id, None)
        await self.cog_.withdraw(self.guild_id, 1, Actor(self.mod_id, "m"), None)
        fresh = await self.row()
        self.assertEqual((fresh["state"], fresh["rendered"]), (WITHDRAWN, 1))
        await self.cog_._check_due({"guild_id": self.guild_id, "number": 1}, {})
        self.assertIsNone(await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1"))
        self.assertEqual(self.errors.lines, [])

    async def test_a_missing_channel_during_the_refill_isnt_gone(self):
        await self.propose()
        self.bot.members_settled.clear()
        self.guild._channels.pop(self.channel_id, None)
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["state"], OPEN)

    async def test_a_server_gone_unavailable_isnt_a_channel_gone(self):
        await self.propose()
        self.guild._channels.pop(self.channel_id, None)
        self.guild.unavailable = True
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["state"], OPEN)


class BusyVoteTest(ProposalCase):
    async def test_a_busy_vote_never_stacks_counts(self):
        row = await self.propose()
        running = peak = 0
        real = self.cog_.refresh

        async def counted(*args: Any) -> Any:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                await asyncio.sleep(0.05)  # a big vote's reaction pages
                return await real(*args)
            finally:
                running -= 1
        self.cog_.refresh = counted
        for index in range(12):
            self.vote(row, self.people[index % 4], UP if index % 2 else DOWN)
            await asyncio.sleep(0.015)
        await self.until(lambda: not self.cog_._soon)
        self.assertEqual(peak, 1)
        self.assert_no_errors()

    async def test_a_vote_during_a_count_gets_counted(self):
        row = await self.propose()
        real = self.cog_._voters
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held(*args: Any) -> Any:
            entered.set()
            await release.wait()
            return await real(*args)
        self.cog_._voters = held
        self.vote(row, self.people[0], UP)
        await asyncio.wait_for(entered.wait(), 5)
        self.vote(row, self.people[1], UP)
        await self.settle(0.05)
        release.set()
        await self.until(lambda: not self.cog_._soon)
        self.assertEqual((await self.row())["community_up"], 2)

    async def test_the_wait_grows_with_the_vote(self):
        self.cog_.REFRESH_DELAY = 3.0
        self.assertEqual(self.cog_._wait_before_count({"community_up": 10, "community_down": 0, "mod_up": 0, "mod_down": 0}), 3.0)
        self.assertGreater(self.cog_._wait_before_count({"community_up": 3000, "community_down": 2000, "mod_up": 0, "mod_down": 0}), 30.0)
        self.assertLessEqual(self.cog_._wait_before_count({"community_up": 900000, "community_down": 0, "mod_up": 0, "mod_down": 0}), 60.0)

    async def test_a_super_reaction_is_a_vote(self):
        await self.propose()
        real = self.bot.ops.reaction_users
        person = {"id": self.people[0], "bot": False}

        async def both(channel_id: int, message_id: int, emoji: str, *, after: int | None = None, limit: int = 100, burst: bool = False) -> list[dict]:
            if burst:
                return [person] if emoji == UP and not after else []
            return await real(channel_id, message_id, emoji, after=after, limit=limit)
        self.bot.ops.reaction_users = both
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["community_up"], 1)

    async def test_an_animated_reaction_comes_off_by_name_and_id(self):
        row = await self.propose()
        seen: list[str] = []
        real = self.bot.http.remove_reaction

        async def spy(channel_id: int, message_id: int, emoji: str, member_id: int) -> Any:
            seen.append(emoji)
            return await real(channel_id, message_id, emoji, member_id)
        self.bot.http.remove_reaction = spy
        await self.cog_._take_off(row, discord.PartialEmoji(name="party", id=123, animated=True), self.people[0])
        await self.cog_._take_off(row, discord.PartialEmoji(name="party", id=123), self.people[0])
        await self.cog_._take_off(row, UP, self.people[0])
        self.assertEqual(seen[:2], ["party:123", "party:123"])
        self.assertEqual(seen[2], UP)


class SizeTest(ProposalCase):
    async def test_the_largest_withdrawn_proposal_fits(self):
        await self.configure(title="T" * 200)
        row = await self.propose("x" * CONTENT_MAX, credit="c" * CREDIT_MAX)
        self.votes(row, ups=[self.people[0]])
        await self.time_is_up()
        await self.cog_.refresh(self.guild_id, 1)
        await self.cog_.withdraw(self.guild_id, 1, Actor(self.mod_id, "m" * 32), "r" * REASON_MAX)
        fresh = await self.row()
        payload = self.cog_._post(fresh)
        self.assertLessEqual(sum(embed_size(embed) for embed in payload["embeds"]), 6000)
        self.assertEqual(payload["embeds"][0]["description"][:10], "x" * 10)
        self.assertIn("dashboard", payload["embeds"][0]["description"])

    async def test_a_proposal_that_fits_keeps_all_its_words(self):
        row = await self.propose("x" * 3000)
        self.assertEqual(self.cog_._post(row)["embeds"][0]["description"], "x" * 3000)


class RestartTest(ProposalCase):
    async def finished_then_cut_off(self) -> dict:
        await self.configure(results_channel=self.log_id)
        row = await self.propose()
        self.votes(row, ups=[self.people[0], *self.mods])
        await self.time_is_up()
        real = self.cog_._finished

        async def process_stops(_row: dict) -> None:
            return None
        self.cog_._finished = process_stops
        await self.cog_.refresh(self.guild_id, 1)
        self.cog_._finished = real
        return await self.row()

    def results(self) -> list[dict]:
        return [m for m in self.net.messages.values() if int(m["channel_id"]) == self.log_id]

    async def test_a_restart_before_the_result_goes_out_still_sends_it(self):
        fresh = await self.finished_then_cut_off()
        self.assertEqual((fresh["state"], fresh["finish_pending"]), (APPROVED, 1))
        self.assertEqual(self.results(), [])
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual(len(self.results()), 1)
        self.assertEqual((await self.row())["finish_pending"], 0)
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual(len(self.results()), 1, "The result goes out once")

    async def test_a_restart_arms_a_check_for_unfinished_work(self):
        await self.finished_then_cut_off()
        await self.bot.timers.cancel_key(TIMER, f"{self.guild_id}:1")
        await self.cog_.on_load()
        self.assertIsNotNone(await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1"))


class SuggestionLinkTest(ProposalCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.suggestions = self.bot.module("suggestions")
        await self.bot.settings.save(self.guild_id, "suggestions", {"channel_id": self.log_id, "edit": True})
        self.moderator = Actor(self.mod_id, "m")

    async def idea(self, who: int | None = None) -> dict:
        return await self.suggestions.create(self.guild_id, self.guild.get_member(who or self.people[0]), "idea")

    async def state(self, number: int = 1) -> str:
        return (await self.suggestions.get(self.guild_id, number))["state"]

    async def test_a_decided_suggestion_cant_be_put_to_a_vote(self):
        await self.idea()
        await self.suggestions.decide(self.guild_id, 1, "implemented", moderator=self.moderator, reason=None)
        with self.assertRaisesRegex(Exception, "already decided"):
            await self.propose("idea", suggestion=1)
        self.assertEqual(await self.state(), "implemented")
        client = await self.client()
        response = await client.post(self.path("/proposals/from-suggestion/1"), json={})
        self.assertEqual(response.status, 400)
        self.slash("proposal", "promote", user_id=self.mod_id, suggestion_id=1)
        await self.settle(0.3)
        self.assertIn("already decided", json.dumps(self.answers()))

    async def test_one_suggestion_has_one_open_vote(self):
        await self.idea()
        await self.propose("idea", suggestion=1)
        with self.assertRaisesRegex(Exception, "proposal #1"):
            await self.propose("idea again", suggestion=1)

    async def test_two_promotes_at_once_make_one_vote(self):
        await self.idea()
        outcomes = await asyncio.gather(self.propose("a", suggestion=1), self.propose("b", suggestion=1), return_exceptions=True)
        self.assertEqual(sum(1 for outcome in outcomes if isinstance(outcome, dict)), 1)

    async def test_a_decision_made_during_the_vote_stands(self):
        await self.idea()
        row = await self.propose("idea", suggestion=1)
        await self.suggestions.decide(self.guild_id, 1, "implemented", moderator=self.moderator, reason="Done")
        self.votes(row, downs=[self.people[2], *self.mods])
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual(await self.state(), "implemented")
        self.assertEqual((await self.row())["finish_pending"], 0)

    async def test_the_vote_still_decides_a_suggestion_it_is_considering(self):
        await self.idea()
        row = await self.propose("idea", suggestion=1)
        self.votes(row, downs=[self.people[2], *self.mods])
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual(await self.state(), "denied")

    async def test_deleting_a_suggestion_with_an_open_vote_is_refused(self):
        await self.idea()
        await self.propose("idea", suggestion=1)
        client = await self.client()
        response = await client.delete(self.path("/suggestions/1"))
        self.assertEqual(response.status, 409)
        self.assertIn("proposal #1", (await response.json())["error"])
        self.assertIsNotNone(await self.suggestions.get(self.guild_id, 1))

    async def test_a_result_never_lands_on_a_later_suggestion_with_the_same_number(self):
        await self.idea()
        await self.propose("idea", suggestion=1)
        await self.cog_.withdraw(self.guild_id, 1, self.moderator, None)
        client = await self.client()
        self.assertEqual((await client.delete(self.path("/suggestions/1"))).status, 200)
        self.assertIsNone((await self.row())["suggestion_number"])
        self.assertEqual((await self.idea(self.people[1]))["number"], 1)
        self.assertEqual(await self.state(), "open")


class MissingRoleTest(ProposalCase):
    async def test_a_late_vote_stays_on_while_no_role_says_who_is_a_moderator(self):
        row = await self.propose()
        await self.time_is_up()
        await self.configure(moderator_role=None)
        self.vote(row, self.mods[0], UP)
        await self.settle(0.2)
        self.assertIn(self.mods[0], self.net.reactors[(row["message_id"], UP)], "The vote stays on the post")
        self.errors.lines.clear()

    async def test_a_role_deleted_while_off_counts_as_no_role(self):
        row = await self.propose()
        await self.time_is_up()
        self.guild._roles.pop(self.mod_role, None)
        self.vote(row, self.mods[0], UP)
        await self.settle(0.2)
        self.assertIn(self.mods[0], self.net.reactors[(row["message_id"], UP)], "The vote stays on the post")
        self.errors.lines.clear()

    async def test_with_a_role_a_late_members_vote_still_comes_off(self):
        row = await self.propose()
        await self.time_is_up()
        self.vote(row, self.people[0], UP)
        await self.settle(0.2)
        self.assertNotIn(self.people[0], self.net.reactors[(row["message_id"], UP)])

    async def test_deleting_the_role_says_so_once_in_the_vote(self):
        row = await self.propose()
        self.bot._connection.parse_guild_role_delete({"guild_id": str(self.guild_id), "role_id": str(self.mod_role)})
        await self.settle(0.2)
        notices = lambda: [m for m in self.net.messages.values() if "moderator role" in (m.get("content") or "")]
        await self.until(lambda: len(notices()) == 1)
        await self.cog_.refresh(self.guild_id, 1)
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual(len(notices()), 1)
        self.assertIn(int(notices()[0]["channel_id"]), (row["thread_id"], row["channel_id"]))
        self.errors.lines.clear()

    async def test_setting_a_new_role_lets_the_count_go_on(self):
        row = await self.propose()
        self.votes(row, ups=self.people)
        await self.time_is_up()
        await self.configure(moderator_role=None)
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["community_done"], 0, "A moderator's vote can't be told from a member's until there's a role")
        await self.configure(moderator_role=self.mod_role)
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["community_done"], 1)
        self.errors.lines.clear()


class AnswersTest(ProposalCase):
    async def test_recheck_says_the_role_is_missing(self):
        await self.propose()
        await self.configure(moderator_role=None)
        self.slash("proposal", "recheck", user_id=self.owner_id)
        await self.settle()  # returns once the command finishes, however busy the machine is
        text = json.dumps(self.answers())
        self.assertIn("no moderator role", text)
        self.assertNotIn("Checked", text)
        self.errors.lines.clear()

    async def test_recheck_gives_counted_and_failed_numbers(self):
        await self.propose()
        await self.propose("another")
        real = self.bot.ops.reaction_users

        async def refuse(channel_id: int, message_id: int, emoji: str, **kwargs: Any) -> list[dict]:
            if message_id == (await self.row(2))["message_id"]:
                raise OpsError("no")
            return await real(channel_id, message_id, emoji, **kwargs)
        self.bot.ops.reaction_users = refuse
        self.slash("proposal", "recheck", user_id=self.owner_id)
        await self.settle()  # returns once the command finishes, however busy the machine is
        text = json.dumps(self.answers())
        self.assertIn("Counted 1 of 2 open proposals", text)
        self.errors.lines.clear()

    async def test_the_site_gets_the_same_numbers(self):
        await self.propose()
        client = await self.client()
        body = await (await client.post(self.path("/proposals/recheck"), json={})).json()
        self.assertEqual((body["checked"], body["counted"], body["problem"]), (1, 1, None))

    async def test_a_long_list_says_how_many_are_left_and_fits_a_message(self):
        for index in range(25):
            await self.propose(f"Change {index}")
        self.slash("proposal", "list", user_id=self.mod_id)
        await self.settle(0.3)
        answer = [a for a in self.answers() if "#1" in (a.get("content") or "")][-1]
        self.assertLessEqual(len(answer["content"]), 2000)
        self.assertRegex(answer["content"], r"And \d+ more, on the Proposals page\.")

    async def test_a_short_list_has_no_note(self):
        await self.propose()
        self.slash("proposal", "list", user_id=self.mod_id)
        await self.settle(0.3)
        self.assertNotIn("more, on the", json.dumps(self.answers()))


class SiteTest(ProposalCase):
    async def staff_client(self, user_id: int) -> Any:
        await self.bot.settings.save(self.guild_id, "core", {"dashboard_staff_roles": [self.mod_role, int(self.role_member["id"])]})
        return await self.client(user_id=user_id)

    async def test_the_list_says_who_may_propose(self):
        await self.propose()
        client = await self.client()
        self.assertTrue((await (await client.get(self.path("/proposals/list"))).json())["can_propose"])
        client = await self.staff_client(self.member_id)
        self.assertFalse((await (await client.get(self.path("/proposals/list"))).json())["can_propose"])

    async def test_only_the_submitter_or_manage_server_withdraws(self):
        await self.propose()
        other = self.mods[1]
        self.assertNotEqual(other, self.mod_id)
        client = await self.staff_client(other)
        body = await (await client.get(self.path("/proposals/list"))).json()
        self.assertFalse(body["rows"][0]["can_withdraw"])
        response = await client.post(self.path("/proposals/1/withdraw"), json={})
        self.assertEqual(response.status, 403)
        self.assertEqual((await self.row())["state"], OPEN)
        self.slash("proposal", "withdraw", user_id=other, number=1)
        await self.settle(0.3)
        self.assertEqual((await self.row())["state"], OPEN)
        self.assertIn("submitted it", json.dumps(self.answers()))
        client = await self.staff_client(self.mod_id)
        body = await (await client.get(self.path("/proposals/list"))).json()
        self.assertTrue(body["rows"][0]["can_withdraw"])
        self.assertEqual((await client.post(self.path("/proposals/1/withdraw"), json={})).status, 200)

    async def test_the_list_gives_the_moderator_count_a_tie_is_read_from(self):
        self.mods.append(self.add_member("mod9", self.mod_role))
        row = await self.propose()
        self.votes(row, ups=self.mods[:2], downs=self.mods[2:])
        await self.cog_.refresh(self.guild_id, 1)
        client = await self.client()
        body = await (await client.get(self.path("/proposals/list"))).json()
        self.assertEqual(body["rows"][0]["mod_total"], 4)

    async def test_bad_numbers_in_a_query_read_as_the_default(self):
        client = await self.client()
        for tail in ("/proposals/list?size=%C2%B2", "/proposals/list?page=%C2%B2", "/proposals/list?page=" + "9" * 30):
            response = await client.get(self.path(tail))
            self.assertEqual(response.status, 200, tail)

    async def test_a_body_that_isnt_an_object_is_refused(self):
        client = await self.client()
        await self.propose()
        for path in ("/proposals", "/proposals/1/withdraw", "/proposals/recheck"):
            response = await client.post(self.path(path), data="[]", headers={"Content-Type": "application/json"})
            self.assertEqual(response.status, 200 if path.endswith("recheck") else 400, path)


class CommandNameTest(ProposalCase):
    async def test_submit_proposal_opens_the_form(self):
        mark = len(self.net.modals)
        self.slash("submit_proposal", user_id=self.mod_id)
        await self.settle(0.3)
        self.assert_no_errors()
        self.assertEqual(len(self.net.modals), mark + 1)

    async def test_the_group_holds_no_second_slash_spelling(self):
        group = next(c for c in self.bot.tree.get_commands() if c.name == "proposal")
        self.assertNotIn("submit", [c.name for c in group.commands])

    async def test_both_typed_spellings_work(self):
        self.dispatch_message(user_id=self.mod_id, content="!submit_proposal Add a bird channel")
        await self.settle(0.3)
        self.assertEqual((await self.row(1))["content"], "Add a bird channel")
        self.dispatch_message(user_id=self.mod_id, content="!proposal submit Add a fish channel")
        await self.settle(0.3)
        self.assert_no_errors()
        self.assertEqual((await self.row(2))["content"], "Add a fish channel")

    async def test_only_moderators_may_use_it(self):
        self.slash("submit_proposal", user_id=self.member_id)
        await self.settle(0.3)
        self.assertIn("moderator role", json.dumps(self.answers()))
        self.assertIsNone(await self.row())


class ImportSafetyTest(ProposalCase):
    async def test_rows_from_before_the_column_never_post_again(self):
        await self.configure(results_channel=self.log_id)
        await self.db.run(
            "INSERT INTO proposals (guild_id, number, channel_id, message_id, title, content, submitter_id, submitter_name, started_at, vote_hours, ends_at, state, imported) "
            "VALUES (?, 1, ?, 555, 'T', 'Old', 1, 'Old', 1, 72, 2, 'approved', 0)", (self.guild_id, self.channel_id))
        await self.cog_._reload()
        self.assertIsNone(await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1"))
        self.assertEqual((await self.row())["finish_pending"], 0)
