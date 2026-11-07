"""Forum messages and Wiki answers on the same forum, through the real library: each acts on a new post on its own, and neither takes the
other's post for its own."""
from __future__ import annotations

import asyncio

from captainguild.modules.answers import Answers
from captainguild.modules.automod import Automod
from captainguild.modules.forummessages import SOLVED_ID, ForumMessages
from tests.gateway import iso, new_id
from tests.test_answers import AnswersCase

SOLVED = 1200000000000000001
WELCOME = "Hey {{OP.Mention}}, a helper will be along."


class BothTest(AnswersCase):
    modules = [Automod, Answers, ForumMessages]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.net.keep_history = True
        self.forums: ForumMessages = self.bot.module("forummessages")
        forum = {**self.forum, "available_tags": [{"id": str(SOLVED), "name": "Solved", "moderated": False, "emoji_id": None, "emoji_name": None}]}
        self.bot._connection.parse_channel_update(forum)
        await self.configure(auto_answer_forums=[int(self.forum["id"])])
        await self.bot.settings.save(self.guild_id, "forummessages", {"enabled": True, "forums": [
            {"forum_id": int(self.forum["id"]), "message": WELCOME, "inactivity_message": "", "solved_tag": SOLVED, "helper_roles": []}]})
        await self.bot.sync_server_commands(self.guild_id)

    def post(self, title: str, content: str) -> int:
        thread_id = new_id()
        self.bot._connection.parse_thread_create({
            "id": str(thread_id), "guild_id": str(self.guild_id), "parent_id": self.forum["id"], "owner_id": str(self.member_id), "name": title, "type": 11,
            "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "applied_tags": [], "flags": 0,
            "thread_metadata": {"archived": False, "auto_archive_duration": 4320, "archive_timestamp": iso(), "locked": False}})
        starter = self.dispatch_message(user_id=self.member_id, content=content, channel={"id": str(thread_id)}, id=str(thread_id))
        self.net.messages[thread_id] = {**starter, "guild_id": str(self.guild_id)}
        return thread_id

    async def drain_both(self) -> None:
        for _ in range(20):
            await self.drain()
            waiting = [task for task in self.forums._tasks if not task.done()]
            if not waiting:
                return
            await asyncio.wait(waiting, timeout=10)

    def bot_messages(self, thread: int) -> list[dict]:
        return [message for message in self.net.posted if int(message["channel_id"]) == thread and message["author"]["id"] == self.net.bot_user["id"]]

    async def test_a_new_post_gets_the_welcome_and_the_wiki_answer(self):
        """What a member sees when both act on one post: the welcome, which pings them and carries the button, and the wiki's answer as a
        reply to their post, which pings nobody."""
        self.answered("Let it cool first. [S1]")
        thread = self.post("Nozzle clogged", "My nozzle is clogged after printing PETG, how do I clear it?")
        await self.drain_both()
        self.assert_no_errors()
        welcomes = [message for message in self.bot_messages(thread) if message.get("components")]
        answers = [message for message in self.bot_messages(thread) if message.get("embeds")]
        self.assertEqual([message["content"] for message in welcomes], [f"Hey <@{self.member_id}>, a helper will be along."])
        self.assertEqual(welcomes[0]["components"][0]["components"][0]["custom_id"], SOLVED_ID)
        self.assertEqual(len(answers), 1)
        self.assertTrue(answers[0]["content"].startswith("While you wait for a helper"))
        bodies = [body for method, path, body in self.net.calls if method == "POST" and path == "/channels/{channel_id}/messages"]
        welcome_body = next(body for body in bodies if body.get("components"))
        answer_body = next(body for body in bodies if body.get("embeds"))
        self.assertNotIn("message_reference", welcome_body)
        self.assertEqual(int(answer_body["message_reference"]["message_id"]), thread)
        self.assertEqual(welcome_body["allowed_mentions"]["users"], [self.member_id])
        self.assertEqual(answer_body["allowed_mentions"], {"parse": [], "replied_user": False})

    async def test_the_wiki_answer_is_no_welcome_to_check_recent_threads(self):
        await self.bot.settings.save(self.guild_id, "forummessages", {"enabled": False})
        self.answered("Let it cool first. [S1]")
        thread = self.post("Nozzle clogged", "My nozzle is clogged after printing PETG, how do I clear it?")
        await self.drain_both()
        self.assertEqual([message for message in self.bot_messages(thread) if message.get("components")], [], "the pack was off")
        self.assertEqual(len([message for message in self.bot_messages(thread) if message.get("embeds")]), 1)
        await self.bot.settings.save(self.guild_id, "forummessages", {"enabled": True})
        await self.bot.sync_server_commands(self.guild_id)
        resolved = {"channels": {self.forum["id"]: {"id": self.forum["id"], "name": self.forum["name"], "type": 15, "permissions": "0", "guild_id": str(self.guild_id)}}}
        data = {"id": str(new_id()), "name": "check_recent_threads", "type": 1, "guild_id": str(self.guild_id), "resolved": resolved,
                "options": [{"name": "forum_channel", "type": 7, "value": self.forum["id"]}, {"name": "num_threads", "type": 4, "value": 5}]}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.owner_id))
        await self.drain_both()
        self.assert_no_errors()
        self.assertEqual(len([message for message in self.bot_messages(thread) if message.get("components")]), 1, "the welcome the post missed")
