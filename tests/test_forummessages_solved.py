"""Forum messages: the two ways to mark a post solved, through the real library with only the network replaced. The Mark as Solved button
under a welcome (AF-12) and the Mark as Solution menu on a message (AF-11), and who may use them (AF-17)."""
from __future__ import annotations

from typing import Any

import discord

from captainguild.modules import forummessages as fm
from tests.gateway import FakeHTTPResponse, new_id
from tests.test_forummessages import Recorded


class Solving(Recorded):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.configure()
        self.manage_posts = self.add_role("Forum Mod", 3, discord.Permissions(manage_threads=True).value)
        self.helper_member = self.add_member("helper", self.helper["id"], self.role_member["id"])
        self.stranger = self.add_member("stranger", self.role_member["id"])
        self.forum_mod = self.add_member("forum mod", self.manage_posts["id"])

    async def welcomed(self, **post: Any) -> tuple[dict, dict]:
        """A post whose welcome is in, and that welcome as Discord holds it."""
        thread = self.post(**post)
        self.say(thread)
        await self.settle()
        welcome = self.posted_in(thread["id"])[-1]
        self.assertEqual(welcome["components"][0]["components"][0]["custom_id"], fm.SOLVED_ID)
        return thread, welcome

    def thread_edits(self, thread: dict) -> list[dict]:
        return [body for method, path, body in self.net.calls if method == "PATCH" and path == "/channels/{channel_id}" and body and any(key in body for key in ("applied_tags", "archived"))]

    def pins(self) -> list[tuple]:
        return [call for call in self.net.calls if call[0] == "PUT" and "pins" in call[1]]

    def refuse(self, method: str, fragment: str, status: int) -> Any:
        """Discord refuses matching calls until the returned function is called."""
        inner = self.bot.http.request

        async def refusing(route: Any, **kwargs: Any) -> Any:
            if route.method == method and fragment in route.path:
                kind = discord.Forbidden if status == 403 else discord.HTTPException
                raise kind(FakeHTTPResponse(status), {"code": 50013 if status == 403 else 0, "message": "Missing Permissions" if status == 403 else "refused for a test"})
            return await inner(route, **kwargs)
        self.bot.http.request = refusing

        def restore() -> None:
            self.bot.http.request = inner
        return restore


class SolvedButtonTest(Solving):
    """AF-12: the Mark as Solved button under the welcome. The old bot let the post's author and the helper roles press it, turned the button off,
    added the solved tag, closed the post and checked it was closed, with a log line for each step."""

    async def test_the_author_marks_the_post_solved(self):
        thread, welcome = await self.welcomed(tags=(self.tags["Question"],))
        await self.press(fm.SOLVED_ID, welcome, self.member_id, channel=thread)
        self.assert_no_errors()
        update = next(body for method, path, body in self.net.calls if path.endswith("/callback") and body.get("type") == 7)
        self.assertTrue(update["data"]["components"][0]["components"][0]["disabled"], "the click turns the button off")
        self.assertEqual(self.thread_edits(thread), [{"applied_tags": [str(self.tags["Question"]), str(self.tags["Solved"])]}, {"archived": True}])
        self.assertEqual(self.last_answer(), "Thread marked as solved and closed!")
        who, where = f"<@{self.member_id}>", f"<#{thread['id']}>"
        lines = await self.log_lines()
        self.assertEqual(lines[-6:], [
            f"Mark as Solved button clicked by {who} in channel ID {thread['id']}", f"Disabling Mark as Solved button in thread {where} by {who}",
            f"Applying solved tag to thread {where} by {who}", f"Archiving thread {where} by {who}", f"Thread {where} confirmed archived by {who}",
            f"Thread {where} marked as solved and archived by {who}",
        ])

    async def test_helpers_and_those_who_manage_posts_may_and_others_may_not(self):
        thread, welcome = await self.welcomed()
        await self.press(fm.SOLVED_ID, welcome, self.stranger, channel=thread)
        self.assertEqual(self.last_answer(), "You don't have permission to mark this thread as solved.")
        self.assertEqual(self.thread_edits(thread), [])
        self.assertIn(f"User <@{self.stranger}> lacks permission to mark thread ID {thread['id']} as solved.", await self.log_lines())
        for user_id in (self.helper_member, self.forum_mod):
            thread, welcome = await self.welcomed()
            await self.press(fm.SOLVED_ID, welcome, user_id, channel=thread)
            self.assertEqual(self.last_answer(), "Thread marked as solved and closed!", user_id)

    async def test_a_forum_with_no_solved_tag_says_so(self):
        thread, welcome = await self.welcomed()
        await self.configure(solved_tag=None)
        await self.press(fm.SOLVED_ID, welcome, self.member_id, channel=thread)
        self.assertEqual(self.last_answer(), "No solved tag configured for this forum channel.")
        self.assertIn(f"No solved tag configured for forum channel ID {self.forum_id}.", await self.log_lines())

    async def test_outside_a_forum_post_it_says_where_it_works(self):
        _, welcome = await self.welcomed()
        await self.press(fm.SOLVED_ID, welcome, self.member_id, channel=self.text)
        self.assertEqual(self.last_answer(), "This button can only be used in a thread.")
        chat = self.post(parent=self.text)
        await self.press(fm.SOLVED_ID, welcome, self.member_id, channel=chat)
        self.assertEqual(self.last_answer(), "This thread is not in a forum channel.")
        lines = await self.log_lines()
        self.assertIn(f"Invalid channel for button interaction: {self.text['id']} is not a thread.", lines)
        self.assertIn(f"Thread ID {chat['id']} is not in a forum channel.", lines)

    async def test_discord_refusing_is_told_to_the_person(self):
        for status, said in ((403, "I don't have permission to edit this thread."), (500, "Failed to mark the thread as solved or close it: 500 fake (error code: 0): refused for a test")):
            thread, welcome = await self.welcomed()
            restore = self.refuse("PATCH", "/channels/{channel_id}", status)
            await self.press(fm.SOLVED_ID, welcome, self.member_id, channel=thread)
            restore()
            self.assertEqual(self.last_answer(), said)
            self.assert_no_errors()
        lines = await self.log_lines()
        self.assertTrue(any(line.endswith("as solved or archive: Missing permissions.") for line in lines), lines)

    async def test_the_button_comes_back_when_closing_fails(self):
        """Its click turns the button off. When the close then fails, the button is on again, so the author can try once more."""
        thread, welcome = await self.welcomed()
        restore = self.refuse("PATCH", "/channels/{channel_id}", 500)
        await self.press(fm.SOLVED_ID, welcome, self.member_id, channel=thread)
        restore()
        edits = [body for method, path, body in self.net.calls if method == "PATCH" and path.endswith("/messages/@original")]
        self.assertTrue(edits, "the welcome is edited back")
        self.assertFalse(edits[-1]["components"][0]["components"][0].get("disabled", False))
        self.assert_no_errors()

    def closed_posts_take_no_edits(self) -> None:
        """Discord refuses an edit to a closed post unless the edit opens it (discord-api-docs, channel.mdx, Modify Channel for a thread), and
        keeps the tags an edit sets."""
        inner = self.bot.http.request

        async def rules(route: Any, **kwargs: Any) -> Any:
            if route.method == "PATCH" and route.path == "/channels/{channel_id}":
                stored = self.net.channels.get(int(route.channel_id)) or {}
                body = kwargs.get("json") or {}
                if stored.get("thread_metadata", {}).get("archived") and body.get("archived") is not False:
                    raise discord.HTTPException(FakeHTTPResponse(400), {"code": 50083, "message": "Thread is archived"})
                if "applied_tags" in body:
                    stored["applied_tags"] = body["applied_tags"]
            return await inner(route, **kwargs)
        self.bot.http.request = rules

    async def test_a_second_click_on_a_post_just_solved_is_told_so(self):
        """Review, unproven 2: two people press Mark as Solved at once, and the second click's edits land on the post the first just closed,
        before Discord's update for the close reaches the cache. The second is told it's solved, and the button stays off."""
        self.closed_posts_take_no_edits()
        thread, welcome = await self.welcomed()
        await self.press(fm.SOLVED_ID, welcome, self.member_id, channel=thread)
        self.assertEqual(self.last_answer(), "Thread marked as solved and closed!")
        await self.press(fm.SOLVED_ID, welcome, self.helper_member, channel=thread)
        self.assertEqual(self.last_answer(), fm.ALREADY_SOLVED)
        self.assertEqual([body for method, path, body in self.net.calls if method == "PATCH" and path.endswith("/messages/@original")], [], "the button stays off")
        self.assert_no_errors()

    async def test_a_closed_post_or_a_full_set_of_tags_is_explained_and_left_alone(self):
        thread, welcome = await self.welcomed(archived=True)
        await self.press(fm.SOLVED_ID, welcome, self.member_id, channel=thread)
        self.assertEqual(self.last_answer(), fm.CLOSED_POST)
        full = tuple(self.tags[name] for name in ("Question", "Problem", "Other")) + (1200000000000000008, 1200000000000000009)
        self.forum["available_tags"] += [{"id": str(tag_id), "name": f"Model {tag_id % 10}", "moderated": False, "emoji_id": None, "emoji_name": None} for tag_id in full[3:]]
        self.state.parse_channel_update(self.forum)
        thread, welcome = await self.welcomed(tags=full)
        await self.press(fm.SOLVED_ID, welcome, self.member_id, channel=thread)
        self.assertEqual(self.last_answer(), fm.TAGS_FULL)
        self.assertEqual(self.thread_edits(thread), [])

    async def test_switched_off_it_says_so(self):
        thread, welcome = await self.welcomed()
        await self.switch(False)
        await self.press(fm.SOLVED_ID, welcome, self.member_id, channel=thread)
        self.assertIn("switched off in this server", self.last_answer())
        self.assertEqual(self.thread_edits(thread), [])

    async def test_it_answers_on_welcomes_from_before_a_restart(self):
        store = self.bot._connection._view_store
        self.assertIn((2, fm.SOLVED_ID), store._views[None])


class SolutionMenuTest(Solving):
    """AF-11: Mark as Solution on a message in a forum post. The old bot let the post's author, the helper roles and the role named Moderator use
    it. It pinned the message, added the solved tag and closed the post, with a log line for each step."""

    async def menu(self, user_id: int, message: dict, thread: dict | None = None) -> None:
        data = {"id": str(new_id()), "name": fm.MENU_NAME, "type": 3, "target_id": message["id"], "resolved": {"messages": {message["id"]: message}}, "guild_id": str(self.guild_id)}
        self.state.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id, channel=thread))
        await self.settle()

    async def answered(self, **post: Any) -> tuple[dict, dict]:
        thread, _ = await self.welcomed(**post)
        reply = self.say(thread, self.helper_member, message_id=int(thread["id"]) + 100, content="Wash the plate with soap.")
        await self.settle()
        return thread, reply

    async def test_the_author_marks_the_answer(self):
        thread, reply = await self.answered()
        await self.menu(self.member_id, reply, thread)
        self.assert_no_errors()
        self.assertEqual(len(self.pins()), 1)
        self.assertEqual(self.thread_edits(thread), [{"applied_tags": [str(self.tags["Solved"])]}, {"archived": True}])
        self.assertEqual(self.last_answer(), "Message pinned, thread marked as solved, and closed!")
        who, where = f"<@{self.member_id}>", f"<#{thread['id']}>"
        self.assertEqual((await self.log_lines())[-4:], [f"Pinning message in thread {where} by {who}", f"Applying solved tag to thread {where} by {who}",
                                                          f"Archiving thread {where} by {who}", f"Message pinned and thread {where} marked as solved and archived by {who}"])

    async def test_who_may(self):
        """The old bot's Moderator role check becomes what closing someone's post takes by hand: Manage Threads in the forum, or the bot manager role."""
        thread, reply = await self.answered()
        for user_id in (self.stranger, self.mod_id):
            await self.menu(user_id, reply, thread)
            self.assertEqual(self.last_answer(), "You don't have permission to mark this message as the solution.", user_id)
        self.assertEqual(self.pins(), [])
        await self.bot.settings.save(self.guild_id, "commands", {"mod_role": int(self.role_mod["id"])})
        for user_id in (self.helper_member, self.forum_mod, self.mod_id):
            thread, reply = await self.answered()
            await self.menu(user_id, reply, thread)
            self.assertEqual(self.last_answer(), "Message pinned, thread marked as solved, and closed!", user_id)

    async def test_a_bypass_on_close_solved_threads_marks_any_post(self):
        """Review finding 6: the cut-over gives the Moderator role a bypass on the pack's commands. One on /close_solved_threads, the command that
        closes solved posts, lets its holders mark any post solved, as the old bot let the role named Moderator. A bypass on another doesn't."""
        thread, reply = await self.answered()
        await self.bot.settings.save(self.guild_id, "commands", {"overwrites": {"list_messages": {"bypass_roles": [int(self.role_mod["id"])]}}})
        await self.menu(self.mod_id, reply, thread)
        self.assertEqual(self.last_answer(), "You don't have permission to mark this message as the solution.")
        await self.bot.settings.save(self.guild_id, "commands", {"overwrites": {"close_solved_threads": {"bypass_roles": [int(self.role_mod["id"])]}}})
        await self.menu(self.mod_id, reply, thread)
        self.assertEqual(self.last_answer(), "Message pinned, thread marked as solved, and closed!")

    async def test_outside_a_forum_post_or_with_no_solved_tag_it_says_so(self):
        message = self.dispatch_message(user_id=self.member_id, content="hello")
        await self.menu(self.member_id, message)
        self.assertEqual(self.last_answer(), "This command can only be used on messages in forum threads.")
        thread, reply = await self.answered()
        await self.configure(solved_tag=None)
        await self.menu(self.member_id, reply, thread)
        self.assertEqual(self.last_answer(), "No solved tag configured for this forum channel.")
        self.assertEqual(self.pins(), [])

    async def test_discord_refusing_is_told_to_the_person_and_logged(self):
        thread, reply = await self.answered()
        restore = self.refuse("PUT", "pins", 403)
        await self.menu(self.member_id, reply, thread)
        restore()
        self.assertEqual(self.last_answer(), "I don't have permission to perform this action.")
        lines = await self.log_lines()
        self.assertTrue(any(line.startswith(f"Failed to pin message, mark thread as solved, or archive thread <#{thread['id']}>: 403") for line in lines), lines)
        thread, reply = await self.answered()
        self.refuse("PATCH", "/channels/{channel_id}", 500)
        await self.menu(self.member_id, reply, thread)
        self.assertEqual(self.last_answer(), "Failed to process the action: 500 fake (error code: 0): refused for a test")
        self.assert_no_errors()

    async def test_a_closed_post_is_explained(self):
        thread, reply = await self.answered(archived=True)
        await self.menu(self.member_id, reply, thread)
        self.assertEqual(self.last_answer(), fm.CLOSED_POST)
        self.assertEqual(self.pins(), [])

    async def test_switched_off_it_says_so(self):
        thread, reply = await self.answered()
        await self.bot.settings.save(self.guild_id, "forummessages", {"enabled": False})  # Discord still shows the menu until the new registration lands
        await self.menu(self.member_id, reply, thread)
        self.assertIn("switched off in this server", self.last_answer())
        self.assertEqual(self.pins(), [])

    async def test_the_menu_reaches_only_servers_with_the_pack_on(self):
        self.assertIn((fm.MENU_NAME, 3), [(command["name"], command["type"]) for command in self.net.guild_commands[self.guild_id]])
        await self.switch(False)
        self.assertEqual(self.net.guild_commands[self.guild_id], [])
