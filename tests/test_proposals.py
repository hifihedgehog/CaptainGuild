"""Proposals: the two votes, through the real discord.py library and real Discord operations, with only the network faked."""
from __future__ import annotations

import json
import time
import unittest
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from captainguild.core.messages import clean_payload, embed_size
from captainguild.core.ops import DiscordOps
from captainguild.modules.proposals import APPROVED, CONTENT_MAX, CREDIT_MAX, DOWN, OPEN, REJECTED, TIMER, UP, WITHDRAWN, Proposals, moderators_decided, outcome, split_votes
from captainguild.modules.suggestions import Suggestions
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.gateway import iso, new_id, user_json
from tests.test_gateway_smoke import Walker


class Rules(unittest.TestCase):
    def test_someone_on_both_reactions_counts_for_neither(self):
        self.assertEqual(split_votes({1, 2}, {2, 3}, set()), {"community_up": 1, "community_down": 1, "mod_up": 0, "mod_down": 0})

    def test_the_two_groups_are_counted_apart(self):
        self.assertEqual(split_votes({1, 10}, {2, 11, 12}, {10, 11, 12}), {"community_up": 1, "community_down": 1, "mod_up": 1, "mod_down": 2})

    def test_moderators_decide_once_more_than_half_agree(self):
        for up, down, total, expected in [(5, 0, 9, True), (4, 0, 9, None), (0, 5, 9, False), (0, 4, 8, None), (5, 3, 8, True), (3, 5, 8, False), (4, 4, 8, None), (1, 0, 1, True), (0, 1, 1, False), (0, 0, 0, None), (0, 0, 5, None)]:
            with self.subTest(up=up, down=down, total=total):
                self.assertIs(moderators_decided(up, down, total), expected)

    def test_the_outcome_needs_both_votes(self):
        base = {"community_done": 0, "community_pass": 0, "mod_done": 0, "mod_pass": 0}
        cases = [
            ({}, (OPEN, None)),
            ({"community_done": 1, "community_pass": 1}, (OPEN, None)),
            ({"mod_done": 1, "mod_pass": 1}, (OPEN, None)),
            ({"community_done": 1, "community_pass": 1, "mod_done": 1, "mod_pass": 1}, (APPROVED, None)),
            ({"community_done": 1}, (REJECTED, "Community vote failed")),
            ({"mod_done": 1}, (REJECTED, "Moderator vote failed")),
            ({"community_done": 1, "mod_done": 1}, (REJECTED, "Community and moderator votes failed")),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(outcome({**base, **changes}), expected)


class ProposalCase(Walker):
    modules = [Suggestions, Proposals]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot.public_url = "https://bot.example"
        self.cog_ = next(m for m in self.bot.modules if isinstance(m, Proposals))
        self.cog_.REFRESH_DELAY = 0.01
        self.channel_id = int(self.text["id"])
        self.log_id = int(self.log_channel["id"])
        self.mod_role = int(self.role_mod["id"])
        self.net.keep_reactions = True
        self.clients: list[TestClient] = []
        self.mods = [self.mod_id, *[self.add_member(f"mod{n}", self.mod_role) for n in range(2)]]
        self.people = [self.add_member(f"person{n}") for n in range(4)]
        await self.configure(channel_id=self.channel_id, moderator_role=self.mod_role)

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        await super().asyncTearDown()

    def add_member(self, name: str, *roles: int) -> int:
        user_id = new_id()
        data = {"user": user_json(user_id, name), "roles": [str(role) for role in roles], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False}
        self.member_json[user_id] = data
        self.bot._connection.parse_guild_member_add({**data, "guild_id": str(self.guild_id)})
        return user_id

    async def configure(self, **form: Any) -> None:
        await self.bot.settings.save(self.guild_id, "proposals", form)

    async def propose(self, content: str = "Add a channel for finished prints", **kwargs: Any) -> dict:
        return await self.cog_.submit(self.guild_id, self.bot.actor(self.guild.get_member(self.mod_id)), content, **kwargs)

    async def row(self, number: int = 1) -> dict:
        return await self.db.one("SELECT * FROM proposals WHERE guild_id = ? AND number = ?", (self.guild_id, number))

    async def time_is_up(self, number: int = 1) -> None:
        await self.db.run("UPDATE proposals SET ends_at = ? WHERE guild_id = ? AND number = ?", (time.time() - 1, self.guild_id, number))

    def vote(self, row: dict, user_id: int, emoji: str, *, event: bool = True) -> None:
        """A person reacts. Discord holds the reaction, and with `event` it tells the bot."""
        self.net.reactors.setdefault((row["message_id"], emoji), {})[user_id] = self.member_json[user_id]["user"]
        if event:
            self.bot._connection.parse_message_reaction_add({
                "user_id": str(user_id), "channel_id": str(row["channel_id"]), "message_id": str(row["message_id"]), "guild_id": str(self.guild_id),
                "member": self.member_json[user_id], "emoji": {"id": None, "name": emoji}, "burst": False, "type": 0,
            })

    def votes(self, row: dict, ups: list[int] = (), downs: list[int] = ()) -> None:
        for user_id in ups:
            self.vote(row, user_id, UP, event=False)
        for user_id in downs:
            self.vote(row, user_id, DOWN, event=False)

    def edits(self, row: dict) -> list[dict]:
        return [body for method, path, body in self.net.calls if method == "PATCH" and path == "/channels/{channel_id}/messages/{message_id}" and body]

    def field(self, payload: dict, name: str) -> str | None:
        for embed in payload["embeds"]:
            for field in embed.get("fields", []):
                if field["name"] == name:
                    return field["value"]
        return None

    def last_post(self, row: dict) -> dict:
        return self.net.messages[row["message_id"]]

    def removals(self) -> list[tuple[str, str]]:
        return [(call[0], call[1]) for call in self.net.calls if call[0] == "DELETE" and "/reactions/" in call[1]]

    def slash(self, *path: str, user_id: int, **values: Any) -> None:
        root = next(c for c in self.bot.tree.get_commands() if c.name == path[0]).to_dict(self.bot.tree)
        leaf = root
        for name in path[1:]:
            leaf = next(o for o in leaf["options"] if o["name"] == name)
        options = [{"name": o["name"], "type": o["type"], "value": values[o["name"]]} for o in leaf.get("options", []) if o["name"] in values]
        for name in reversed(path[1:]):
            options = [{"name": name, "type": 1, "options": options}]
        data = {"id": str(new_id()), "name": path[0], "type": 1, "options": options, "resolved": {}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id))

    def answers(self) -> list[dict]:
        found = []
        for _method, path, body in self.net.calls:
            if path.endswith("/callback"):
                found.append(body.get("data") or {})
            elif "/webhooks/" in path and isinstance(body, dict):
                found.append(body)
        return found

    async def client(self, *, user_id: int | None = None) -> TestClient:
        person = User(user_id or self.owner_id, "owner", None)
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=person)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)
        return client

    def path(self, tail: str) -> str:
        return f"/api/g/{self.guild_id}{tail}"

    async def until(self, ready: Any, seconds: float = 5.0) -> None:
        """Wait for something a background task does. The database's speed decides how long that takes, so a fixed wait would be a coin flip."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = ready()
            if hasattr(result, "__await__"):
                result = await result
            if result:
                return
            await self.settle(0.02)
        self.fail("It never happened")


class SubmitTest(ProposalCase):
    async def test_the_form_posts_it_with_pings_thread_reactions_and_a_timer(self):
        notify = int(self.role_member["id"])
        await self.configure(participation_role=notify)
        mark = len(self.net.modals)
        self.slash("submit_proposal", user_id=self.mod_id)
        await self.settle(0.3)
        self.assert_no_errors()
        self.assertEqual(len(self.net.modals), mark + 1)
        await self.fill_forms(mark, user_id=self.mod_id, label="submit")
        row = await self.row()
        self.assertIsNotNone(row, self.errors.lines)
        post = self.last_post(row)
        self.assertEqual(post["content"], f"<@&{notify}> <@&{self.mod_role}>")
        body = next(b for m, p, b in self.net.calls if m == "POST" and p == "/channels/{channel_id}/messages")
        self.assertEqual(body["allowed_mentions"]["parse"].count("roles"), 1)
        self.assertEqual(post["embeds"][0]["title"], "Server Change Proposal")
        self.assertEqual(self.field(post, "Current Votes"), "Community: Favor: 0 Against: 0\nModerators: Favor: 0 Against: 0")
        self.assertIn(f"<@{self.mod_id}>", self.field(post, "Submitted by"))
        self.assertIn(f"<t:{int(row['ends_at'])}:f>", self.field(post, "Proposal Approval Process"))
        threads = [b for m, p, b in self.net.calls if m == "POST" and p.endswith("/threads")]
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0]["name"].startswith("Discussion: "))
        self.assertIsNotNone(row["thread_id"])
        self.assertEqual({key[1] for key in self.net.reactors if key[0] == row["message_id"]}, {UP, DOWN})
        self.assertIsNotNone(await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1"))
        self.assertAlmostEqual(row["ends_at"], row["started_at"] + 72 * 3600, delta=1)

    async def test_only_moderators_may_submit(self):
        mark = len(self.net.modals)
        self.slash("submit_proposal", user_id=self.member_id)
        await self.settle(0.3)
        self.assertEqual(len(self.net.modals), mark)
        self.assertIsNone(await self.row())
        self.assertIn("moderator role", json.dumps(self.answers()))

    async def test_manage_server_may_submit_too(self):
        mark = len(self.net.modals)
        self.slash("submit_proposal", user_id=self.admin_id)
        await self.settle(0.3)
        self.assertEqual(len(self.net.modals), mark + 1)

    async def test_it_says_what_setting_is_missing(self):
        await self.configure(channel_id=None)
        self.slash("submit_proposal", user_id=self.mod_id)
        await self.settle(0.3)
        self.assertIn("no proposal channel", json.dumps(self.answers()))
        await self.configure(channel_id=self.channel_id, moderator_role=None)
        self.slash("submit_proposal", user_id=self.admin_id)
        await self.settle(0.3)
        self.assertIn("no moderator role", json.dumps(self.answers()))

    async def test_a_text_command_takes_the_words_directly(self):
        self.dispatch_message(user_id=self.mod_id, content="!proposal submit Add a bird channel")
        await self.settle(0.3)
        self.assert_no_errors()
        row = await self.row()
        self.assertEqual(row["content"], "Add a bird channel")

    async def test_the_biggest_proposal_fits_what_discord_allows(self):
        await self.configure(title="T" * 200)
        row = await self.propose("x" * CONTENT_MAX, credit="c" * CREDIT_MAX)
        self.votes(row, ups=self.mods)
        await self.time_is_up()
        await self.cog_.refresh(self.guild_id, 1)
        row = await self.row()
        self.assertEqual(row["state"], REJECTED)  # no community votes
        payload = self.cog_._post(row)
        clean_payload(payload)
        self.assertLessEqual(sum(embed_size(embed) for embed in payload["embeds"]), 6000)
        for embed in payload["embeds"]:
            for field in embed.get("fields", []):
                self.assertLessEqual(len(field["value"]), 1024, field["name"])

    async def test_a_refused_post_gives_the_number_back(self):
        original = self.bot.ops.send_message

        async def refuse(*args: Any, **kwargs: Any) -> dict:
            from captainguild.core.ops import OpsError
            raise OpsError("no")

        self.bot.ops.send_message = refuse
        with self.assertRaises(Exception):
            await self.propose()
        self.bot.ops.send_message = original
        self.assertIsNone(await self.row())
        row = await self.propose()
        self.assertEqual(row["number"], 1)

    async def test_a_thread_failure_doesnt_stop_the_vote(self):
        from captainguild.core.ops import OpsError

        async def refuse(*args: Any, **kwargs: Any) -> dict:
            raise OpsError("no threads")

        self.bot.ops.start_thread = refuse
        row = await self.propose()
        self.assertEqual(row["state"], OPEN)
        self.assertEqual({key[1] for key in self.net.reactors if key[0] == row["message_id"]}, {UP, DOWN})
        self.errors.lines.clear()

    async def test_numbers_are_per_server(self):
        await self.propose()
        await self.db.run("UPDATE proposals SET guild_id = 999")
        self.assertEqual((await self.propose())["number"], 1)


class VoteTest(ProposalCase):
    async def test_both_votes_passing_approves_it(self):
        row = await self.propose()
        self.votes(row, ups=[*self.people[:3], *self.mods[:2]], downs=self.people[3:])
        await self.cog_.refresh(self.guild_id, 1)
        current = await self.row()
        self.assertEqual(current["state"], OPEN, "The community still has time")
        self.assertTrue(current["mod_done"])
        self.assertTrue(current["mod_pass"])
        await self.time_is_up()
        await self.cog_.refresh(self.guild_id, 1)
        current = await self.row()
        self.assertEqual(current["state"], APPROVED)
        self.assertEqual((current["community_up"], current["community_down"]), (3, 1))
        self.assertEqual((current["mod_up"], current["mod_down"]), (2, 0))
        post = self.last_post(row)
        self.assertEqual(self.field(post, "Final Status"), "Approved")
        self.assertIn("Outcome: Passed", self.field(post, "Community Vote Results"))
        self.assertIn("Outcome: Approved", self.field(post, "Moderator Vote Results"))
        self.assertEqual(post["embeds"][0]["color"], 0x2ECC71)
        self.assertIsNone(await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1"))

    async def test_a_failed_community_vote_rejects_it_at_once(self):
        row = await self.propose()
        self.votes(row, ups=self.people[:1], downs=self.people[1:3])
        await self.time_is_up()
        await self.cog_.refresh(self.guild_id, 1)
        current = await self.row()
        self.assertEqual(current["state"], REJECTED)
        self.assertEqual(current["status_reason"], "Community vote failed")
        self.assertEqual(self.field(self.last_post(row), "Final Status"), "Rejected (Community vote failed)")

    async def test_a_tie_and_no_votes_both_fail_the_community_vote(self):
        for ups, downs in (([], []), (self.people[:1], self.people[1:2])):
            row = await self.propose()
            self.votes(row, ups=ups, downs=downs)
            await self.time_is_up(row["number"])
            await self.cog_.refresh(self.guild_id, row["number"])
            self.assertEqual((await self.row(row["number"]))["state"], REJECTED)

    async def test_moderators_rejecting_ends_it_before_the_community_is_done(self):
        row = await self.propose()
        self.votes(row, ups=self.people, downs=self.mods[:2])
        await self.cog_.refresh(self.guild_id, 1)
        current = await self.row()
        self.assertEqual(current["state"], REJECTED)
        self.assertEqual(current["status_reason"], "Moderator vote failed")
        self.assertFalse(current["community_done"])

    async def test_one_moderator_of_three_decides_nothing(self):
        row = await self.propose()
        self.votes(row, ups=self.mods[:1])
        await self.cog_.refresh(self.guild_id, 1)
        current = await self.row()
        self.assertFalse(current["mod_done"])
        self.assertEqual(current["mod_up"], 1)

    async def test_moderators_have_no_deadline(self):
        row = await self.propose()
        self.votes(row, ups=self.people)
        await self.time_is_up()
        await self.cog_.refresh(self.guild_id, 1)
        current = await self.row()
        self.assertEqual(current["state"], OPEN)
        self.assertTrue(current["community_done"])
        self.assertIsNotNone(await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1"), "A vote still going needs its check")
        self.votes(row, ups=self.mods[:2])
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["state"], APPROVED)

    async def test_a_closed_community_count_stays_as_it_was(self):
        row = await self.propose()
        self.votes(row, ups=self.people[:2])
        await self.time_is_up()
        await self.cog_.refresh(self.guild_id, 1)
        self.votes(row, ups=self.people[2:], downs=[])
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["community_up"], 2)

    async def test_the_count_shows_on_the_post_as_votes_come_in(self):
        row = await self.propose()
        self.vote(row, self.people[0], UP)
        self.vote(row, self.people[1], DOWN)
        self.vote(row, self.mods[0], UP)
        await self.until(lambda: self.field(self.last_post(row), "Current Votes") == "Community: Favor: 1 Against: 1\nModerators: Favor: 1 Against: 0")
        self.assert_no_errors()

    async def test_votes_landing_together_cost_one_edit(self):
        row = await self.propose()
        before = len(self.edits(row))
        self.cog_.REFRESH_DELAY = 0.6
        for person in self.people:
            self.vote(row, person, UP)
        await self.until(lambda: len(self.edits(row)) > before)
        await self.settle(0.8)
        self.assertEqual(len(self.edits(row)) - before, 1)
        self.assertEqual((await self.row())["community_up"], len(self.people))

    async def test_no_change_means_no_edit(self):
        row = await self.propose()
        before = len(self.edits(row))
        await self.cog_.refresh(self.guild_id, 1)
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual(len(self.edits(row)), before)

    async def test_bots_dont_count(self):
        row = await self.propose()
        other = self.add_member("other bot")
        self.member_json[other]["user"]["bot"] = True
        self.votes(row, ups=[other])
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["community_up"], 0)

    async def test_votes_cast_while_the_bot_was_down_count(self):
        row = await self.propose()
        self.votes(row, ups=self.people[:2])  # no events, as during downtime
        await self.cog_._check_due({"guild_id": self.guild_id, "number": 1}, {})
        self.assertEqual((await self.row())["community_up"], 2)

    async def test_a_moderator_who_loses_the_role_stops_counting_as_one(self):
        row = await self.propose()
        self.votes(row, ups=[self.mods[0]])
        self.member_json[self.mods[1]]["roles"] = []
        self.bot._connection.parse_guild_member_update({**self.member_json[self.mods[1]], "guild_id": str(self.guild_id)})
        self.member_json[self.mods[2]]["roles"] = []
        self.bot._connection.parse_guild_member_update({**self.member_json[self.mods[2]], "guild_id": str(self.guild_id)})
        await self.cog_.refresh(self.guild_id, 1)
        current = await self.row()
        self.assertTrue(current["mod_done"], "One of one moderators is more than half")
        self.assertTrue(current["mod_pass"])

    async def test_a_missing_moderator_role_waits_instead_of_guessing(self):
        row = await self.propose()
        self.votes(row, ups=self.people)
        await self.time_is_up()
        await self.configure(moderator_role=None)
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["community_done"], 0)
        self.errors.lines.clear()


class ReactionTest(ProposalCase):
    async def test_changing_your_mind_takes_the_other_reaction_off(self):
        row = await self.propose()
        self.vote(row, self.people[0], UP)
        await self.settle(0.2)
        self.vote(row, self.people[0], DOWN)

        async def counted() -> bool:
            current = await self.row()
            return (current["community_up"], current["community_down"]) == (0, 1)

        await self.until(counted)
        self.assert_no_errors()
        self.assertNotIn(self.people[0], self.net.reactors[(row["message_id"], UP)])
        self.assertIn(self.people[0], self.net.reactors[(row["message_id"], DOWN)])

    async def test_other_reactions_come_off(self):
        row = await self.propose()
        self.vote(row, self.people[0], "\N{SMILING FACE WITH OPEN MOUTH}")
        await self.settle(0.2)
        self.assertNotIn(self.people[0], self.net.reactors[(row["message_id"], "\N{SMILING FACE WITH OPEN MOUTH}")])

    async def test_a_late_member_vote_comes_off_and_their_earlier_vote_stays(self):
        row = await self.propose()
        self.vote(row, self.people[0], UP)
        await self.settle(0.2)
        await self.time_is_up()
        self.vote(row, self.people[0], DOWN)
        await self.settle(0.2)
        self.assertNotIn(self.people[0], self.net.reactors[(row["message_id"], DOWN)], "The late reaction comes off")
        self.assertIn(self.people[0], self.net.reactors[(row["message_id"], UP)], "The vote cast in time stays")

    async def test_a_moderator_can_still_vote_after_the_community_is_done(self):
        row = await self.propose()
        await self.time_is_up()
        self.vote(row, self.mods[0], UP)
        await self.settle(0.2)
        self.assertIn(self.mods[0], self.net.reactors[(row["message_id"], UP)])

    async def test_every_vote_comes_off_once_it_is_over(self):
        row = await self.propose()
        await self.cog_.withdraw(self.guild_id, 1, self.bot.actor(self.guild.get_member(self.mod_id)), None)
        self.vote(row, self.mods[0], UP)
        await self.settle(0.2)
        self.assertNotIn(self.mods[0], self.net.reactors[(row["message_id"], UP)])

    async def test_reactions_elsewhere_cost_nothing(self):
        await self.propose()
        before = len(self.net.calls)
        message = self.message_payload(user_id=self.member_id, channel=self.log_channel)
        self.bot._connection.parse_message_reaction_add({
            "user_id": str(self.member_id), "channel_id": self.log_channel["id"], "message_id": message["id"], "guild_id": str(self.guild_id),
            "member": self.member_json[self.member_id], "emoji": {"id": None, "name": UP}, "burst": False, "type": 0,
        })
        await self.settle(0.1)
        self.assertEqual(len(self.net.calls), before)

    async def test_clearing_the_reactions_puts_the_two_back(self):
        row = await self.propose()
        self.votes(row, ups=self.people[:1])
        self.net.reactors.clear()
        self.bot._connection.parse_message_reaction_remove_all({"channel_id": str(row["channel_id"]), "message_id": str(row["message_id"]), "guild_id": str(self.guild_id)})
        await self.settle(0.2)
        self.assert_no_errors()
        self.assertEqual({key[1] for key in self.net.reactors if key[0] == row["message_id"]}, {UP, DOWN})


class ReminderTest(ProposalCase):
    def reminders(self) -> list[str]:
        return [m["content"] for m in self.net.posted if "Reminder:" in (m.get("content") or "")]

    def dm_recipients(self) -> set[int]:
        return {int(body["recipient_id"]) for _method, path, body in self.net.calls if path == "/users/@me/channels"}

    async def closed_with_one_mod_voted(self) -> dict:
        row = await self.propose()
        self.votes(row, ups=[*self.people, self.mods[0]])
        await self.time_is_up()
        await self.cog_.refresh(self.guild_id, 1)
        return row

    async def test_no_reminder_right_when_the_community_vote_closes(self):
        await self.closed_with_one_mod_voted()
        self.assertEqual(self.reminders(), [])

    async def test_moderators_who_havent_voted_get_a_dm_after_a_day(self):
        row = await self.closed_with_one_mod_voted()
        await self.db.run("UPDATE proposals SET last_reminder_at = ?", (time.time() - 86401,))
        await self.cog_.refresh(self.guild_id, 1)
        self.assert_no_errors()
        self.assertEqual(len(self.reminders()), 2, "Two of the three moderators haven't voted")
        self.assertEqual(self.dm_recipients(), set(self.mods[1:]))
        self.assertIn(f"/{row['channel_id']}/{row['message_id']}", self.reminders()[0])
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual(len(self.reminders()), 2, "Not again for another day")
        await self.db.run("UPDATE proposals SET last_reminder_at = ?", (time.time() - 86401,))
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual(len(self.reminders()), 4)

    async def test_reminders_can_be_turned_off(self):
        await self.closed_with_one_mod_voted()
        await self.configure(reminder_hours=0)
        await self.db.run("UPDATE proposals SET last_reminder_at = ?", (time.time() - 10 * 86400,))
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual(self.reminders(), [])

    async def test_no_reminders_while_the_community_is_still_voting(self):
        await self.propose()
        await self.db.run("UPDATE proposals SET last_reminder_at = ?", (time.time() - 10 * 86400,))
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual(self.reminders(), [])


class LifecycleTest(ProposalCase):
    async def test_a_setting_change_leaves_an_open_vote_as_it_was(self):
        row = await self.propose()
        await self.configure(vote_hours=1)
        self.assertEqual((await self.row())["ends_at"], row["ends_at"])
        self.assertIn("72 hours", self.field(self.last_post(row), "Proposal Approval Process"))

    async def test_withdrawing_stops_the_votes_and_says_so(self):
        row = await self.propose()
        self.slash("proposal", "withdraw", user_id=self.mod_id, number=1, reason="Changed plans")
        await self.settle(0.3)
        self.assert_no_errors()
        current = await self.row()
        self.assertEqual(current["state"], WITHDRAWN)
        self.assertIn("Changed plans", self.field(self.last_post(row), "Final Status"))
        self.assertIsNone(await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1"))

    async def test_only_moderators_may_withdraw(self):
        await self.propose()
        self.slash("proposal", "withdraw", user_id=self.member_id, number=1)
        await self.settle(0.3)
        self.assertEqual((await self.row())["state"], OPEN)

    async def test_a_deleted_post_ends_the_vote_and_keeps_the_record(self):
        row = await self.propose()
        self.bot._connection.parse_message_delete({"id": str(row["message_id"]), "channel_id": str(row["channel_id"]), "guild_id": str(self.guild_id)})
        await self.settle(0.2)
        self.assert_no_errors()
        current = await self.row()
        self.assertEqual(current["state"], WITHDRAWN)
        self.assertEqual(current["status_reason"], "Post deleted")
        self.assertIsNone(await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1"))
        self.assertEqual(current["content"], row["content"])

    async def test_a_deleted_channel_clears_the_setting_and_ends_its_votes(self):
        await self.propose()
        self.bot._connection.parse_channel_delete({**self.text})
        await self.settle()  # returns once the delete's handlers finish, however busy the machine is
        self.assert_no_errors()
        self.assertIsNone(self.bot.settings.get(self.guild_id, "proposals")["channel_id"])
        self.assertEqual((await self.row())["state"], WITHDRAWN)

    async def test_a_deleted_role_clears_the_setting(self):
        self.bot._connection.parse_guild_role_delete({"guild_id": str(self.guild_id), "role_id": str(self.mod_role)})
        await self.settle(0.2)
        self.assert_no_errors()
        self.assertIsNone(self.bot.settings.get(self.guild_id, "proposals")["moderator_role"])

    async def test_a_restart_picks_up_where_it_left_off(self):
        row = await self.propose()
        await self.bot.timers.cancel_key(TIMER, f"{self.guild_id}:1")
        self.cog_._voting.clear()
        await self.cog_.on_load()
        self.assertIn(row["message_id"], self.cog_._voting)
        self.assertIsNotNone(await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1"))

    async def test_the_check_comes_due_when_the_vote_closes_at_the_latest(self):
        await self.propose()
        await self.db.run("UPDATE proposals SET ends_at = ?", (time.time() + 60,))
        await self.cog_._arm(await self.row())
        timer = await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1")
        self.assertLessEqual(timer["due_at"], time.time() + 61)

    async def test_the_timer_closes_the_vote(self):
        row = await self.propose()
        self.votes(row, ups=self.people)
        await self.db.run("UPDATE proposals SET ends_at = ?", (time.time() - 1,))
        await self.db.run("UPDATE timers SET due_at = ?", (time.time() - 1,))
        await self.bot.timers.run_due()
        current = await self.row()
        self.assertTrue(current["community_done"])
        self.assertIsNotNone(await self.bot.timers.get_key(TIMER, f"{self.guild_id}:1"))

    async def test_an_edit_that_fails_tries_again_at_the_next_check(self):
        from captainguild.core.ops import OpsError
        row = await self.propose()
        self.votes(row, ups=self.people, downs=[])
        await self.time_is_up()
        original = self.bot.ops.edit_message

        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise OpsError("not now")

        self.bot.ops.edit_message = refuse
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["rendered"], 0)
        self.bot.ops.edit_message = original
        self.votes(row, ups=self.mods[:2])
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual((await self.row())["rendered"], 1)
        self.errors.lines.clear()

    async def test_a_past_proposal_brought_over_is_left_alone(self):
        await self.db.run(
            "INSERT INTO proposals (guild_id, number, channel_id, message_id, title, content, submitter_id, submitter_name, started_at, vote_hours, ends_at, state, imported) "
            "VALUES (?, 1, ?, 555, 'T', 'Old', 1, 'Old', 1, 72, 2, 'approved', 1)", (self.guild_id, self.channel_id))
        row = await self.row()
        before = len(self.net.calls)
        self.bot._connection.parse_message_reaction_add({
            "user_id": str(self.member_id), "channel_id": str(self.channel_id), "message_id": "555", "guild_id": str(self.guild_id),
            "member": self.member_json[self.member_id], "emoji": {"id": None, "name": UP}, "burst": False, "type": 0,
        })
        await self.settle(0.2)
        await self.cog_.refresh(self.guild_id, 1)
        self.assertEqual(len(self.net.calls), before)
        self.assertEqual((await self.row())["state"], row["state"])
        self.assertEqual((await self.propose())["number"], 2, "Numbering carries on after them")

    async def test_the_list_command_shows_open_ones_to_moderators_only(self):
        await self.propose()
        self.slash("proposal", "list", user_id=self.member_id)
        await self.settle(0.3)
        self.assertIn("Only moderators", json.dumps(self.answers()))
        self.net.calls.clear()
        self.slash("proposal", "list", user_id=self.mod_id)
        await self.settle(0.3)
        self.assertIn("#1", json.dumps(self.answers()))

    async def test_the_recheck_command_counts_now(self):
        row = await self.propose()
        self.votes(row, ups=self.people[:2])
        self.slash("proposal", "recheck", user_id=self.owner_id)
        await self.settle(0.3)
        self.assert_no_errors()
        self.assertEqual((await self.row())["community_up"], 2)


class ResultTest(ProposalCase):
    async def test_results_go_to_the_results_channel(self):
        await self.configure(results_channel=self.log_id)
        row = await self.propose()
        self.votes(row, ups=[*self.people, *self.mods[:2]])
        await self.time_is_up()
        await self.cog_.refresh(self.guild_id, 1)
        posts = [m for m in self.net.posted if int(m["channel_id"]) == self.log_id]
        self.assertEqual(len(posts), 1)
        self.assertIn("Approved", posts[0]["embeds"][0]["title"])
        self.assertIn(str(row["message_id"]), json.dumps(posts[0]["embeds"][0]["fields"]))

    async def test_a_withdrawn_proposal_posts_no_result(self):
        await self.configure(results_channel=self.log_id)
        await self.propose()
        await self.cog_.withdraw(self.guild_id, 1, self.bot.actor(self.guild.get_member(self.mod_id)), None)
        self.assertEqual([m for m in self.net.posted if int(m["channel_id"]) == self.log_id], [])


class PromoteTest(ProposalCase):
    async def suggestion(self, *, anon: bool = False) -> dict:
        await self.bot.settings.save(self.guild_id, "suggestions", {"channel_id": self.log_id, "anon": anon})
        suggestions = next(m for m in self.bot.modules if isinstance(m, Suggestions))
        return await suggestions.create(self.guild_id, self.guild.get_member(self.member_id), "A bird channel")

    async def test_the_command_opens_the_form_filled_in(self):
        await self.suggestion()
        mark = len(self.net.modals)
        self.slash("proposal", "promote", user_id=self.mod_id, suggestion_id=1)
        await self.settle(0.3)
        self.assert_no_errors()
        fields = [row["components"][0] for row in self.net.modals[mark]["components"]]
        self.assertEqual(fields[0]["value"], "A bird channel")
        self.assertEqual(fields[1]["value"], "member")

    async def test_an_anonymous_suggestion_gets_no_credit(self):
        await self.suggestion(anon=True)
        mark = len(self.net.modals)
        self.slash("proposal", "promote", user_id=self.mod_id, suggestion_id=1)
        await self.settle(0.3)
        fields = [row["components"][0] for row in self.net.modals[mark]["components"]]
        self.assertFalse(fields[1].get("value"))

    async def test_the_suggestion_follows_the_vote(self):
        await self.suggestion()
        row = await self.propose("A bird channel", credit="member", suggestion=1)
        suggestions = next(m for m in self.bot.modules if isinstance(m, Suggestions))
        self.assertEqual((await suggestions.get(self.guild_id, 1))["state"], "considered")
        self.assertIn("proposal #1", (await suggestions.get(self.guild_id, 1))["reason"])
        self.votes(row, ups=[*self.people, *self.mods[:2]])
        await self.time_is_up()
        await self.cog_.refresh(self.guild_id, 1)
        self.assert_no_errors()
        final = await suggestions.get(self.guild_id, 1)
        self.assertEqual(final["state"], "approved")
        self.assertEqual(final["decided_by_name"], "the proposal vote")

    async def test_a_rejected_proposal_denies_the_suggestion(self):
        await self.suggestion()
        row = await self.propose("A bird channel", suggestion=1)
        self.votes(row, downs=self.mods[:2])
        await self.cog_.refresh(self.guild_id, 1)
        suggestions = next(m for m in self.bot.modules if isinstance(m, Suggestions))
        final = await suggestions.get(self.guild_id, 1)
        self.assertEqual(final["state"], "denied")
        self.assertIn("Moderator vote failed", final["reason"])

    async def test_the_credit_shows_on_the_post(self):
        row = await self.propose(credit="https://discord.com/channels/1/2/3")
        self.assertEqual(self.field(self.last_post(row), "Credit"), "https://discord.com/channels/1/2/3")

    async def test_a_missing_suggestion_says_so(self):
        self.slash("proposal", "promote", user_id=self.mod_id, suggestion_id=99)
        await self.settle(0.3)
        self.assertIn("no suggestion #99", json.dumps(self.answers()))


class DashboardTest(ProposalCase):
    async def test_the_list_filters_and_shows_the_votes(self):
        row = await self.propose()
        self.votes(row, ups=self.people[:2])
        await self.cog_.refresh(self.guild_id, 1)
        await self.propose("Another")
        client = await self.client()
        data = await (await client.get(self.path("/proposals/list"))).json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["rows"][1]["community_up"], 2)
        self.assertIn(str(row["message_id"]), data["rows"][1]["url"])
        self.assertIsNotNone(data["rows"][1]["thread_url"])
        self.assertIsNone(data["problem"])
        data = await (await client.get(self.path("/proposals/list?q=another"))).json()
        self.assertEqual(data["total"], 1)
        data = await (await client.get(self.path("/proposals/list?state=approved"))).json()
        self.assertEqual(data["total"], 0)

    async def test_the_list_says_what_is_missing(self):
        await self.configure(channel_id=None)
        client = await self.client()
        data = await (await client.get(self.path("/proposals/list"))).json()
        self.assertIn("no proposal channel", data["problem"])

    async def test_submitting_from_the_site(self):
        client = await self.client()
        response = await client.post(self.path("/proposals"), json={"content": "From the site", "credit": "Someone"})
        self.assertEqual(response.status, 200, await response.text())
        row = await self.row()
        self.assertEqual((row["content"], row["credit"]), ("From the site", "Someone"))
        self.assertEqual(row["submitter_id"], self.owner_id)

    async def test_empty_text_is_refused(self):
        client = await self.client()
        response = await client.post(self.path("/proposals"), json={"content": "   "})
        self.assertEqual(response.status, 400)
        self.assertIn("content", (await response.json())["fields"])

    async def test_promoting_from_the_site(self):
        await self.bot.settings.save(self.guild_id, "suggestions", {"channel_id": self.log_id})
        suggestions = next(m for m in self.bot.modules if isinstance(m, Suggestions))
        await suggestions.create(self.guild_id, self.guild.get_member(self.member_id), "A bird channel")
        client = await self.client()
        response = await client.post(self.path("/proposals/from-suggestion/1"), json={})
        self.assertEqual(response.status, 200, await response.text())
        row = await self.row()
        self.assertEqual((row["content"], row["credit"], row["suggestion_number"]), ("A bird channel", "member", 1))
        self.assertEqual((await suggestions.get(self.guild_id, 1))["state"], "considered")
        self.assertEqual((await client.post(self.path("/proposals/from-suggestion/99"), json={})).status, 404)

    async def test_withdrawing_and_rechecking_from_the_site(self):
        row = await self.propose()
        self.votes(row, ups=self.people[:1])
        client = await self.client()
        self.assertEqual((await (await client.post(self.path("/proposals/recheck"), json={})).json())["checked"], 1)
        self.assertEqual((await self.row())["community_up"], 1)
        response = await client.post(self.path("/proposals/1/withdraw"), json={"reason": "Not now"})
        self.assertEqual(response.status, 200, await response.text())
        self.assertIn("Not now", (await self.row())["status_reason"])
        self.assertEqual((await client.post(self.path("/proposals/1/withdraw"), json={})).status, 404)

    async def test_staff_without_the_moderator_role_can_read_but_not_act(self):
        await self.propose()
        staff_role = int(self.role_member["id"])
        await self.bot.settings.save(self.guild_id, "core", {"dashboard_staff_roles": [staff_role]})
        client = await self.client(user_id=self.member_id)
        self.assertEqual((await client.get(self.path("/proposals/list"))).status, 200)
        self.assertEqual((await client.post(self.path("/proposals"), json={"content": "No"})).status, 403)
        self.assertEqual((await client.post(self.path("/proposals/1/withdraw"), json={})).status, 403)
        self.assertEqual((await client.post(self.path("/proposals/recheck"), json={})).status, 403)

    async def test_members_with_the_moderator_role_can_act_from_the_site(self):
        await self.bot.settings.save(self.guild_id, "core", {"dashboard_staff_roles": [self.mod_role]})
        client = await self.client(user_id=self.mod_id)
        response = await client.post(self.path("/proposals"), json={"content": "From a moderator"})
        self.assertEqual(response.status, 200, await response.text())

    async def test_settings_refuse_nonsense(self):
        client = await self.client()
        response = await client.put(self.path("/settings/proposals"), json={"vote_hours": 0})
        self.assertEqual(response.status, 400)
        response = await client.put(self.path("/settings/proposals"), json={"title": ""})
        self.assertEqual(response.status, 400)
