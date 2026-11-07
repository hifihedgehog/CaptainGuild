"""Suggestions when the world isn't tidy: anonymous typing, two decisions at once, posts and channels that went away."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from captainguild.core.settings import Actor
from tests.test_proposals import ProposalCase


class SuggestionEdge(ProposalCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.suggestions = self.bot.module("suggestions")
        await self.setup(channel_id=self.log_id)

    async def setup(self, **form: Any) -> None:
        await self.bot.settings.save(self.guild_id, "suggestions", form)

    async def idea(self, text: str = "idea") -> dict:
        return await self.suggestions.create(self.guild_id, self.guild.get_member(self.people[0]), text)

    def typed_deletes(self, mark: int) -> list[tuple]:
        return [call for call in self.net.calls[mark:] if call[0] == "DELETE" and call[1] == "/channels/{channel_id}/messages/{message_id}"]

    def said(self, mark: int) -> list[dict]:
        return [body for method, path, body in self.net.calls[mark:] if method == "POST" and path == "/channels/{channel_id}/messages"]


class AnonymousTest(SuggestionEdge):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.setup(anon=True)

    async def test_a_typed_suggestion_comes_down_and_the_answer_is_private(self):
        mark = len(self.net.calls)
        self.dispatch_message(user_id=self.people[0], content="!suggest add a bird channel")
        await self.settle(0.3)
        self.assert_no_errors()
        self.assertEqual(len(self.typed_deletes(mark)), 1)
        row = await self.suggestions.get(self.guild_id, 1)
        self.assertEqual(row["content"], "add a bird channel")
        public = [m["content"] for m in self.net.messages.values() if int(m["channel_id"]) == int(self.text["id"])]
        self.assertFalse([text for text in public if "Your suggestion" in text], public)
        self.assertTrue(self.calls_of_dm(), "The number and link go by DM")

    def calls_of_dm(self) -> list[tuple]:
        return [call for call in self.net.calls if call[1] == "/users/@me/channels"]

    async def test_closed_dms_leave_a_note_with_no_number(self):
        restore = self.refuse_dms()
        mark = len(self.net.calls)
        self.dispatch_message(user_id=self.people[0], content="!suggest add a bird channel")
        await self.settle(0.3)
        restore()
        texts = [body.get("content") or "" for body in self.said(mark)]
        self.assertEqual(texts[-1], "Received.")
        self.assertFalse([text for text in texts if "#1" in text])

    def refuse_dms(self) -> Any:
        import discord

        from tests.gateway import FakeHTTPResponse
        real = self.bot.http.request

        async def request(route: Any, **kwargs: Any) -> Any:
            if route.method == "POST" and route.path == "/users/@me/channels":
                raise discord.Forbidden(FakeHTTPResponse(403), {"code": 50007, "message": "closed"})
            return await real(route, **kwargs)
        self.bot.http.request = request

        def restore() -> None:
            self.bot.http.request = real
        return restore

    async def test_a_typed_message_the_bot_cant_remove_makes_no_suggestion(self):
        import discord

        from tests.gateway import FakeHTTPResponse
        real = self.bot.http.request

        async def request(route: Any, **kwargs: Any) -> Any:
            if route.method == "DELETE" and route.path == "/channels/{channel_id}/messages/{message_id}":
                raise discord.Forbidden(FakeHTTPResponse(403), {"code": 50013, "message": "no"})
            return await real(route, **kwargs)
        self.bot.http.request = request
        mark = len(self.net.calls)
        self.dispatch_message(user_id=self.people[0], content="!suggest add a bird channel")
        await self.settle(0.3)
        self.bot.http.request = real
        self.assertIsNone(await self.suggestions.get(self.guild_id, 1))
        self.assertIn("/suggestion suggest", json.dumps(self.said(mark)))

    async def test_a_typed_cooldown_refusal_still_takes_the_message_down(self):
        self.dispatch_message(user_id=self.people[0], content="!suggest first")
        await self.settle(0.3)
        mark = len(self.net.calls)
        self.dispatch_message(user_id=self.people[0], content="!suggest second")
        await self.settle(0.3)
        self.assertEqual(len(self.typed_deletes(mark)), 1)
        self.assert_no_errors()

    async def test_the_slash_path_is_unchanged(self):
        mark = len(self.net.calls)
        self.slash("suggestion", "suggest", user_id=self.people[0], content="add a bird channel")
        await self.settle(0.3)
        self.assertEqual(self.typed_deletes(mark), [])
        self.assertIn("Your suggestion is #1", json.dumps(self.answers()))

    async def test_with_anonymous_off_a_typed_suggestion_stays_and_answers_in_the_open(self):
        await self.setup(anon=False)
        mark = len(self.net.calls)
        self.dispatch_message(user_id=self.people[0], content="!suggest add a bird channel")
        await self.settle(0.3)
        self.assertEqual(self.typed_deletes(mark), [])
        self.assertIn("Your suggestion is #1", json.dumps(self.said(mark)))


class DecidingTogetherTest(SuggestionEdge):
    async def test_two_decisions_at_once_leave_one_post(self):
        await self.setup(decision_channel=self.channel_id, edit=True)
        await self.idea()
        slow = self.suggestions._count_votes

        async def delayed(row: dict) -> Any:
            await asyncio.sleep(0.02)
            return await slow(row)
        self.suggestions._count_votes = delayed
        await asyncio.gather(
            self.suggestions.decide(self.guild_id, 1, "approved", moderator=Actor(self.mods[0], "a"), reason="yes"),
            self.suggestions.decide(self.guild_id, 1, "denied", moderator=Actor(self.mods[1], "b"), reason="no"),
        )
        row = await self.suggestions.get(self.guild_id, 1)
        posts = [m for m in self.net.messages.values() if int(m["channel_id"]) == self.channel_id and m.get("embeds")]
        self.assertEqual(len(posts), 1, [m["embeds"][0]["title"] for m in posts])
        self.assertEqual(posts[0]["id"], str(row["entry_message_id"]))

    async def test_decisions_on_different_suggestions_dont_wait_for_each_other(self):
        await self.idea()
        await self.idea("another")
        self.assertIsNot(self.suggestions._decide_lock(self.guild_id, 1), self.suggestions._decide_lock(self.guild_id, 2))
        self.assertIs(self.suggestions._decide_lock(self.guild_id, 1), self.suggestions._decide_lock(self.guild_id, 1))


class GonePostsTest(SuggestionEdge):
    async def test_a_suggestion_can_be_deleted_with_its_channel_gone(self):
        await self.setup(decision_channel=self.channel_id, edit=True)
        await self.idea()
        await self.suggestions.decide(self.guild_id, 1, "approved", moderator=Actor(self.mods[0], "a"), reason="yes")
        self.guild._channels.pop(self.channel_id, None)
        client = await self.client()
        response = await client.delete(self.path("/suggestions/1"))
        self.assertEqual(response.status, 200, await response.text())
        self.assertIsNone(await self.suggestions.get(self.guild_id, 1))
        self.assertEqual(len((await response.json())["left"]), 1)

    async def test_a_clean_delete_leaves_nothing_behind(self):
        await self.idea()
        client = await self.client()
        response = await client.delete(self.path("/suggestions/1"))
        self.assertEqual((await response.json())["left"], [])

    async def test_a_purge_clears_the_link(self):
        row = await self.idea()
        self.bot._connection.parse_message_delete_bulk({"ids": [str(row["message_id"])], "channel_id": str(self.log_id), "guild_id": str(self.guild_id)})
        await self.settle(0.1)
        self.assertIsNone((await self.suggestions.get(self.guild_id, 1))["message_id"])

    async def test_a_decision_post_deleted_by_hand_clears_its_link(self):
        await self.setup(decision_channel=self.channel_id)
        await self.idea()
        row = await self.suggestions.decide(self.guild_id, 1, "approved", moderator=Actor(self.mods[0], "a"), reason=None)
        self.bot._connection.parse_message_delete({"id": str(row["entry_message_id"]), "channel_id": str(self.channel_id), "guild_id": str(self.guild_id)})
        await self.settle()  # returns once the delete's handlers finish, however busy the machine is
        fresh = await self.suggestions.get(self.guild_id, 1)
        self.assertEqual((fresh["entry_message_id"], fresh["entry_channel_id"]), (None, None))

    async def test_a_delete_in_a_channel_no_longer_set_clears_the_link(self):
        row = await self.idea()
        await self.setup(channel_id=self.channel_id)
        self.bot._connection.parse_message_delete({"id": str(row["message_id"]), "channel_id": str(self.log_id), "guild_id": str(self.guild_id)})
        await self.settle()  # returns once the delete's handlers finish, however busy the machine is
        self.assertIsNone((await self.suggestions.get(self.guild_id, 1))["message_id"])


class BadInputTest(SuggestionEdge):
    async def test_bad_numbers_in_a_query_read_as_the_default(self):
        client = await self.client()
        for tail in ("/suggestions/list?page=%C2%B2", "/suggestions/list?size=%C2%B2", "/suggestions/list?page=" + "9" * 30):
            self.assertEqual((await client.get(self.path(tail))).status, 200, tail)

    async def test_a_decision_of_the_wrong_shape_is_refused(self):
        await self.idea()
        client = await self.client()
        for body in ({"decision": []}, {"decision": {}}, {"decision": 5}):
            response = await client.post(self.path("/suggestions/1/decide"), json=body)
            self.assertEqual(response.status, 400, body)
