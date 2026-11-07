"""Forum messages: the old bot's slash commands, through the real library with only the network replaced. Each keeps its name and options,
apart from /set_log_channel, which is /forum_log_channel here, and each takes Manage Threads where the old bot took the role named Moderator."""
from __future__ import annotations

import asyncio
import datetime
from typing import Any
from unittest import mock

import aiohttp
import discord

from captainguild.core import access
from captainguild.modules import forummessages as fm
from tests.gateway import FakeHTTPResponse, iso, new_id
from tests.test_forummessages import Recorded

OLD_NAMES = {"set_message", "set_inactivity_message", "set_solved_tag", "set_helper_roles", "list_messages", "delete_message", "forum_log_channel",
             "clean_threads", "close_solved_threads", "check_recent_threads", fm.MENU_NAME}


class Commanding(Recorded):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.forum_mod_role = self.add_role("Forum Mod", 3, discord.Permissions(manage_threads=True).value)
        self.forum_mod = self.add_member("forum mod", self.forum_mod_role["id"], self.role_member["id"])
        self.helper_member = self.add_member("helper", self.helper["id"], self.role_member["id"])
        self.thread_members: dict[int, list[int]] = {}
        self.removed: list[tuple[int, int]] = []
        inner = self.bot.http.request

        async def threads(route: Any, **kwargs: Any) -> Any:
            if route.path == "/channels/{channel_id}/thread-members" and route.method == "GET":
                return [{"id": str(route.channel_id), "user_id": str(user_id), "join_timestamp": iso(), "flags": 0} for user_id in self.thread_members.get(int(route.channel_id), [])]
            if route.path == "/channels/{channel_id}/thread-members/{user_id}" and route.method == "DELETE":
                user_id = int(route.url.rsplit("/", 1)[1])
                self.removed.append((int(route.channel_id), user_id))
                return None
            return await inner(route, **kwargs)
        self.bot.http.request = threads

    def channel_option(self, channel: dict) -> dict:
        return {"id": channel["id"], "name": channel["name"], "type": channel["type"], "permissions": str(discord.Permissions.all().value), "parent_id": channel.get("parent_id"),
                "guild_id": str(self.guild_id), "nsfw": False, "position": channel.get("position", 0), "rate_limit_per_user": 0, "topic": None, "flags": 0, "last_message_id": None}

    async def command(self, name: str, user_id: int, **options: Any) -> None:
        """A slash command as Discord sends it, with channels as well as the roles, numbers and text the harness sends."""
        resolved: dict = {}
        data = []
        for key, value in options.items():
            if isinstance(value, dict) and "type" in value and "guild_id" in value:
                resolved.setdefault("channels", {})[value["id"]] = self.channel_option(value)
                data.append({"name": key, "type": 7, "value": value["id"]})
            else:
                data.extend(self._option_data({key: value}, resolved))
        payload = {"id": str(new_id()), "name": name, "type": 1, "options": data, "resolved": resolved, "guild_id": str(self.guild_id)}
        self.state.parse_interaction_create(self.interaction_payload(2, payload, user_id=user_id))
        await self.settle()

    def role(self, data: dict) -> discord.Role:
        return self.guild.get_role(int(data["id"]))

    def typed(self, user_id: int, content: str) -> None:
        message = self.net.message_json(int(self.text["id"]), {"content": content})
        member = self.member_json[user_id]
        message.update(author=member["user"], member={k: v for k, v in member.items() if k != "user"})
        self.state.parse_message_create(message)


class PackTest(Commanding):
    async def test_the_pack_holds_the_old_commands_and_the_menu(self):
        self.assertEqual({command["name"] for command in self.net.guild_commands[self.guild_id]}, OLD_NAMES)
        await self.switch(False)
        self.assertEqual(self.net.guild_commands[self.guild_id], [])

    async def test_every_command_takes_manage_threads_and_a_bypass_role_opens_one(self):
        """AF-17: the old bot let the role named Moderator run each of these. By hand they take Manage Threads, which Discord calls Manage Posts in
        a forum. The cut-over gives the server's Moderator role a bypass on each, as it does for /mass_mention."""
        for command in self.bot.pack_commands["forummessages"]:
            if not isinstance(command, discord.app_commands.ContextMenu):
                self.assertEqual(self.bot.command_permissions(command.name), {"manage_threads"}, command.name)
        await self.configure()
        await self.command("list_messages", self.mod_id)
        self.assertIn("You need Manage Threads to use this command.", self.last_answer())
        await self.bot.settings.save(self.guild_id, "commands", {"overwrites": {"list_messages": {"bypass_roles": [int(self.role_mod["id"])]}}})
        await self.command("list_messages", self.mod_id)
        self.assertTrue(self.last_answer().startswith("Configured forum messages:"), self.last_answer())


class MessageFormTest(Commanding):
    """AF-1 and AF-2: /set_message and /set_inactivity_message open a form holding the forum's current text, and saving it sets that text."""

    async def test_set_message_opens_on_the_current_text_and_saves(self):
        await self.configure()
        await self.command("set_message", self.forum_mod, forum_channel=self.forum)
        modal = self.net.modals[-1]
        self.assertEqual(modal["title"], "Set Custom Message")
        field = modal["components"][0].get("component") or modal["components"][0]["components"][0]
        self.assertEqual((field["label"], field["value"], field["max_length"], field.get("required", True)), ("Message", self.settings()["forums"][0]["message"], 2000, True))
        await self.submit_form(self.forum_mod, "Welcome, {{OP.Mention}}.")
        self.assert_no_errors()
        self.assertEqual(self.settings()["forums"][0]["message"], "Welcome, {{OP.Mention}}.")
        self.assertEqual(self.last_answer(), f"Message set for forum channel <#{self.forum_id}>")
        self.assertIn(f"Message set for forum channel <#{self.forum_id}> by <@{self.forum_mod}>", await self.log_lines())

    async def test_a_new_forum_gets_its_entry(self):
        await self.command("set_message", self.forum_mod, forum_channel=self.forum)
        await self.submit_form(self.forum_mod, "Hello")
        self.assertEqual(self.settings()["forums"], [{"forum_id": self.forum_id, "message": "Hello", "inactivity_message": "", "solved_tag": None, "helper_roles": []}])

    async def test_set_inactivity_message_and_an_empty_one_goes_back_to_the_default(self):
        self.helper["mentionable"] = True  # the default reminder pings the helper roles, so going back to it is a ping only Mention Everyone opens otherwise
        self.state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": self.helper})
        await self.configure(inactivity_message="Old reminder")
        await self.command("set_inactivity_message", self.forum_mod, forum_channel=self.forum)
        modal = self.net.modals[-1]
        field = modal["components"][0].get("component") or modal["components"][0]["components"][0]
        self.assertEqual((modal["title"], field["label"], field["value"], field["required"]), ("Set Inactivity Message", "Message", "Old reminder", False))
        await self.submit_form(self.forum_mod, "Any news, {{OP.Mention}}?")
        self.assertEqual(self.settings()["forums"][0]["inactivity_message"], "Any news, {{OP.Mention}}?")
        self.assertEqual(self.last_answer(), f"Inactivity message set for forum channel <#{self.forum_id}>")
        await self.command("set_inactivity_message", self.forum_mod, forum_channel=self.forum)
        await self.submit_form(self.forum_mod, "")
        self.assertEqual(self.settings()["forums"][0]["inactivity_message"], "")
        self.assertEqual(self.last_answer(), f"Inactivity message for forum channel <#{self.forum_id}> set back to the default")

    async def test_a_full_list_of_forums_is_told_to_the_person(self):
        rows = [{"forum_id": 1400000000000000000 + number, "message": "x", "inactivity_message": "", "solved_tag": None, "helper_roles": []} for number in range(fm.MAX_FORUMS)]
        await self.bot.settings.save(self.guild_id, "forummessages", {"forums": rows})
        await self.command("set_message", self.forum_mod, forum_channel=self.forum)
        await self.submit_form(self.forum_mod, "Hello")
        self.assertEqual(self.last_answer(), f"Can hold at most {fm.MAX_FORUMS} entries.")
        self.assert_no_errors()

    async def test_the_typed_form_points_to_the_slash_command(self):
        self.typed(self.forum_mod, f"!set_message <#{self.forum_id}>")
        await self.settle()
        self.assertTrue(any("The form only opens from the slash command `/set_message`." in (body.get("content") or "") for _, body in self.sent))

    async def test_a_ping_the_person_couldnt_send_is_refused(self):
        """DECISIONS, posting: saved text that pings @everyone, @here or a role members can't ping takes Mention Everyone there."""
        await self.configure()
        for text in ("@everyone look here", "<@&%s> look here" % self.role_mod["id"]):
            await self.command("set_message", self.forum_mod, forum_channel=self.forum)
            await self.submit_form(self.forum_mod, text)
            self.assertEqual(self.last_answer(), "You need Mention Everyone in that channel to ping through the bot.", text)
        self.helper["mentionable"] = True
        self.state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": self.helper})
        await self.command("set_message", self.forum_mod, forum_channel=self.forum)
        await self.submit_form(self.forum_mod, "<@&%s> anyone can ping this one" % self.helper_id)
        self.assertEqual(self.settings()["forums"][0]["message"], "<@&%s> anyone can ping this one" % self.helper_id)
        await self.command("set_message", self.owner_id, forum_channel=self.forum)
        await self.submit_form(self.owner_id, "@everyone from the owner")
        self.assertEqual(self.settings()["forums"][0]["message"], "@everyone from the owner")

    async def test_a_ping_in_the_welcome_doesnt_open_the_reminder_to_it(self):
        """Review finding 4: a ping is new to a text that didn't hold it, even when the forum's other text does."""
        staff = f"<@&{self.role_mod['id']}>"  # a role members can't ping
        await self.configure(message=f"Hey {{{{OP.Mention}}}}, welcome. {staff} please look.", inactivity_message="Any news?")
        await self.command("set_inactivity_message", self.forum_mod, forum_channel=self.forum)
        await self.submit_form(self.forum_mod, f"Still stuck, {{{{OP.Mention}}}}? {staff} will look.")
        self.assertEqual(self.last_answer(), "You need Mention Everyone in that channel to ping through the bot.")
        self.assertEqual(self.settings()["forums"][0]["inactivity_message"], "Any news?")
        await self.command("set_message", self.forum_mod, forum_channel=self.forum)
        await self.submit_form(self.forum_mod, f"Hey {{{{OP.Mention}}}}, welcome! {staff} please have a look.")
        self.assertIn("please have a look", self.settings()["forums"][0]["message"], "a typo fixed around the welcome's own ping still passes")
        await self.configure(message="Hey {{OP.Mention}}, welcome.", inactivity_message="Any news?", helper_roles=[int(self.role_mod["id"])])
        await self.command("set_inactivity_message", self.forum_mod, forum_channel=self.forum)
        await self.submit_form(self.forum_mod, "")
        self.assertEqual(self.last_answer(), "You need Mention Everyone in that channel to ping through the bot.", "the default reminder pings the helper roles")

    async def test_a_forum_the_person_cant_post_in_is_refused(self):
        quiet = {**self.forum, "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(send_messages=True).value)}]}
        self.state.parse_channel_update(quiet)
        await self.command("set_message", self.forum_mod, forum_channel=quiet)
        await self.submit_form(self.forum_mod, "Hello")
        self.assertEqual(self.last_answer(), "You can't post in that channel.")
        self.assertEqual(self.settings()["forums"], [])


class SolvedTagTest(Commanding):
    """AF-3: /set_solved_tag picks the tag by name, any case, from a list of the forum's tags."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "forummessages", {"log_channel": self.log_id})

    async def test_it_sets_the_tag_by_name(self):
        await self.command("set_solved_tag", self.forum_mod, forum_channel=self.forum, tag="solved")
        self.assert_no_errors()
        self.assertEqual(self.settings()["forums"][0]["solved_tag"], self.tags["Solved"])
        self.assertEqual(self.last_answer(), f"Solved tag set to 'Solved' for forum channel <#{self.forum_id}>")
        self.assertIn(f"Solved tag set to 'Solved' for forum channel <#{self.forum_id}> by <@{self.forum_mod}>", await self.log_lines(),
                      "the old bot's line named a variable that didn't exist, so it never reached the log")

    async def test_an_unknown_tag_says_so(self):
        await self.command("set_solved_tag", self.forum_mod, forum_channel=self.forum, tag="Nope")
        self.assertEqual(self.last_answer(), f"No tag named 'Nope' found in <#{self.forum_id}>.")
        self.assertEqual(self.settings()["forums"], [])

    async def test_the_tag_option_lists_the_forums_tags(self):
        data = {"id": str(new_id()), "name": "set_solved_tag", "type": 1, "guild_id": str(self.guild_id),
                "options": [{"name": "forum_channel", "type": 7, "value": self.forum["id"]}, {"name": "tag", "type": 3, "value": "s", "focused": True}],
                "resolved": {"channels": {self.forum["id"]: self.channel_option(self.forum)}}}
        self.state.parse_interaction_create(self.interaction_payload(4, data, user_id=self.forum_mod))
        await self.settle()
        self.assert_no_errors()
        answer = next(body for method, path, body in self.net.calls if path.endswith("/callback") and body.get("type") == 8)
        self.assertEqual([choice["name"] for choice in answer["data"]["choices"]], ["Solved", "Question"])


class HelperRolesTest(Commanding):
    """AF-4: /set_helper_roles replaces the forum's helper roles with the ones given, the first required."""

    async def test_it_sets_the_roles(self):
        await self.configure(helper_roles=[], inactivity_message="Any news?")
        await self.command("set_helper_roles", self.forum_mod, forum_channel=self.forum, role1=self.role(self.helper), role2=self.role(self.role_member))
        self.assert_no_errors()
        self.assertEqual(self.settings()["forums"][0]["helper_roles"], [self.helper_id, int(self.role_member["id"])])
        said = f"Helper roles set to <@&{self.helper_id}>, <@&{self.role_member['id']}> for forum channel <#{self.forum_id}>"
        self.assertEqual(self.last_answer(), said)
        self.assertIn(f"{said} by <@{self.forum_mod}>", await self.log_lines(), "the old bot's line never reached the log")

    async def test_everyone_cant_be_a_helper_role(self):
        await self.command("set_helper_roles", self.owner_id, forum_channel=self.forum, role1=self.guild.default_role)
        self.assertIn(access.NOT_EVERYONE, self.last_answer())
        self.assertEqual(self.settings()["forums"], [])

    async def test_the_default_reminder_pinging_a_role_members_cant_ping_needs_mention_everyone(self):
        await self.configure(helper_roles=[])
        await self.command("set_helper_roles", self.forum_mod, forum_channel=self.forum, role1=self.role(self.role_mod))
        self.assertIn("You need Mention Everyone in that channel to ping through the bot.", self.last_answer())
        await self.configure(helper_roles=[], inactivity_message="Any news?")
        await self.command("set_helper_roles", self.forum_mod, forum_channel=self.forum, role1=self.role(self.role_mod))
        self.assertEqual(self.settings()["forums"][0]["helper_roles"], [int(self.role_mod["id"])], "a reminder of its own pings no helper role")


class ListAndDeleteTest(Commanding):
    """AF-5, AF-6 and AF-7: /list_messages, /delete_message and the log channel command."""

    async def test_list_messages_reads_as_before(self):
        await self.configure(message="M" * 60, inactivity_message="")
        await self.command("list_messages", self.forum_mod)
        self.assertEqual(self.last_answer(), f"Configured forum messages:\n<#{self.forum_id}>:\n  Message: {'M' * 50}...\n  Inactivity Message: Not set...\n"
                                             f"  Solved Tag ID: {self.tags['Solved']}\n  Helper Roles: <@&{self.helper_id}>\n")

    async def test_a_forum_with_no_message_lists_without_failing(self):
        """The old bot raised a KeyError here for a forum given a solved tag or helper roles before a message."""
        await self.command("set_solved_tag", self.forum_mod, forum_channel=self.forum, tag="Solved")
        await self.command("list_messages", self.forum_mod)
        self.assert_no_errors()
        self.assertIn("  Message: Not set...\n", self.last_answer())
        self.assertIn("  Helper Roles: None\n", self.last_answer())

    async def test_nothing_set_says_so_and_a_long_list_comes_in_parts(self):
        await self.command("list_messages", self.forum_mod)
        self.assertEqual(self.last_answer(), "No messages configured.")
        rows = [{"forum_id": 1400000000000000000 + number, "message": "x", "inactivity_message": "", "solved_tag": None, "helper_roles": list(range(1500000000000000000, 1500000000000000025))} for number in range(5)]
        await self.bot.settings.save(self.guild_id, "forummessages", {"forums": rows})
        await self.command("list_messages", self.owner_id)
        parts = [answer.get("content") or "" for answer in self.answers() if (answer.get("content") or "").startswith(("Configured", "<#"))]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 2000 for part in parts))
        self.assertEqual(sum(part.count("Solved Tag ID") for part in parts), 5)

    async def test_delete_message_takes_the_forums_settings(self):
        await self.configure()
        await self.command("delete_message", self.forum_mod, forum_channel=self.forum)
        self.assertEqual(self.settings()["forums"], [])
        said = f"Message, inactivity message, and settings deleted for forum channel <#{self.forum_id}>"
        self.assertEqual(self.last_answer(), said)
        self.assertIn(f"{said} by <@{self.forum_mod}>", await self.log_lines())
        await self.command("delete_message", self.forum_mod, forum_channel=self.forum)
        self.assertEqual(self.last_answer(), "No message configured for this channel.")

    async def test_forum_log_channel_sets_it_and_says_so_there(self):
        """AF-7: the old /set_log_channel. Three old bots used that name, so each now carries its feature's word."""
        await self.command("forum_log_channel", self.forum_mod, log_channel=self.log_channel)
        self.assertEqual(self.settings()["log_channel"], self.log_id)
        self.assertEqual(self.last_answer(), f"Log channel set to <#{self.log_id}>")
        self.assertIn(f"Log channel set to <#{self.log_id}> by <@{self.forum_mod}>", await self.log_lines())

    async def test_a_log_channel_the_person_cant_post_in_is_refused(self):
        quiet = {**self.log_channel, "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(send_messages=True).value)}]}
        self.state.parse_channel_update(quiet)
        await self.command("forum_log_channel", self.forum_mod, log_channel=quiet)
        self.assertIn("You can't post in that channel.", self.last_answer())
        self.assertIsNone(self.settings()["log_channel"])


class CleanupTest(Commanding):
    """AF-9 and AF-10: /clean_threads takes members with the named roles out of every open post, and /close_solved_threads closes every open post
    with the solved tag, one post a second, with a log line for each and a summary."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.configure(message="")
        self.cog.thread_gap = 0

    async def test_clean_threads_takes_role_holders_out_of_open_posts(self):
        first, second = self.post(), self.post()
        self.thread_members[int(first["id"])] = [self.member_id, self.helper_member, self.bot_id]
        self.thread_members[int(second["id"])] = [self.helper_member]
        await self.command("clean_threads", self.forum_mod, forum_channel=self.forum, role1=self.role(self.helper))
        self.assert_no_errors()
        self.assertEqual(sorted(self.removed), sorted([(int(first["id"]), self.helper_member), (int(second["id"]), self.helper_member)]))
        said = [answer.get("content") for answer in self.answers() if answer.get("content")]
        self.assertEqual(said[-2:], ["Thread cleaning process started...", "Thread cleaning process completed. Check the log channel for details."])
        lines = await self.log_lines()
        self.assertIn(f"Starting thread cleanup in forum channel <#{self.forum_id}> for roles: <@&{self.helper_id}> by <@{self.forum_mod}>", lines)
        self.assertIn(f"Removed <@{self.helper_member}> from thread <#{first['id']}>", lines)
        self.assertEqual(lines[-5:], [f"Thread cleanup completed in <#{self.forum_id}>:", "- Threads processed: 2", "- Users removed: 2", "- Errors encountered: 0", f"Notified <@{self.forum_mod}>"])

    async def test_close_solved_threads_closes_only_solved_posts(self):
        solved = self.post(tags=(self.tags["Solved"],))
        open_one = self.post(tags=(self.tags["Question"],))
        await self.command("close_solved_threads", self.forum_mod, forum_channel=self.forum)
        self.assert_no_errors()
        closed = [body for method, path, body in self.net.calls if method == "PATCH" and path == "/channels/{channel_id}" and body == {"archived": True}]
        self.assertEqual(len(closed), 1)
        self.assertTrue(self.net.channels[int(solved["id"])]["thread_metadata"]["archived"])
        self.assertFalse(self.net.channels[int(open_one["id"])]["thread_metadata"]["archived"])
        said = [answer.get("content") for answer in self.answers() if answer.get("content")]
        self.assertEqual(said[-2:], ["Closing solved threads started...", "Closing solved threads completed. Check the log channel for details."])
        lines = await self.log_lines()
        self.assertIn(f"Closed thread <#{solved['id']}> with solved tag", lines)
        self.assertEqual(lines[-5:], [f"Closing solved threads completed in <#{self.forum_id}>:", "- Threads processed: 2", "- Threads closed: 1", "- Errors encountered: 0", f"Notified <@{self.forum_mod}>"])

    async def test_close_solved_threads_needs_a_solved_tag(self):
        await self.configure(message="", solved_tag=None)
        self.post(tags=(self.tags["Solved"],))
        await self.command("close_solved_threads", self.forum_mod, forum_channel=self.forum)
        self.assertEqual(self.last_answer(), "No solved tag configured for this forum channel.")

    async def test_an_answer_past_discords_15_minutes_isnt_posted_for_everyone(self):
        """Review finding 5: for an interaction past its 15 minutes, the library's Context.send posts a plain message in the channel. The sweep's
        last answer is dropped then, since the log channel holds the summary."""
        for post in (self.post(), self.post()):
            self.thread_members[int(post["id"])] = [self.helper_member]
        real_now = discord.utils.utcnow
        late = {"on": False}
        inner = self.bot.http.request

        async def reading(route: Any, **kwargs: Any) -> Any:
            if route.method == "GET" and route.path == "/channels/{channel_id}/thread-members":
                late["on"] = True  # by the time the sweep reaches the posts, 16 minutes have gone by
            return await inner(route, **kwargs)
        self.bot.http.request = reading
        with mock.patch.object(discord.utils, "utcnow", lambda: real_now() + datetime.timedelta(minutes=16) if late["on"] else real_now()):
            await self.command("clean_threads", self.forum_mod, forum_channel=self.forum, role1=self.role(self.helper))
        self.assert_no_errors()
        public = [body.get("content") for channel_id, body in self.sent if channel_id != self.log_id]
        self.assertNotIn("Thread cleaning process completed. Check the log channel for details.", public)
        self.assertIn(f"Thread cleanup completed in <#{self.forum_id}>:", await self.log_lines())

    async def test_one_run_at_a_time_in_a_forum(self):
        self.cog._busy.add((self.guild_id, self.forum_id))
        await self.command("clean_threads", self.forum_mod, forum_channel=self.forum, role1=self.role(self.helper))
        self.assertIn("already running in that forum", self.last_answer())


class RecentThreadsTest(Commanding):
    """AF-15: /check_recent_threads posts the welcome in the newest open posts that don't have it."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.helper["mentionable"] = True  # as the role the old bot's text pinged is
        self.state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": self.helper})

    async def test_it_posts_the_welcome_only_where_it_is_missing(self):
        await self.configure(message="")
        welcomed = self.post(self.helper_member)
        await self.db.run("INSERT INTO forum_welcomes (guild_id, thread_id, channel_id, message_id, posted_at) VALUES (?, ?, ?, 1, 0)", (self.guild_id, int(welcomed["id"]), self.forum_id))
        earlier = self.post(self.forum_mod)
        old = self.net.message_json(int(earlier["id"]), {"content": "", "components": [{"type": 1, "components": [{"type": 2, "style": 3, "label": "Mark as Solved", "custom_id": "solved"}]}]},
                                    message_id=int(earlier["id"]) + 1)  # posted after the post's first message, as a welcome is
        old["author"] = {"id": "1300000000000000999", "username": "Another bot", "discriminator": "0", "global_name": None, "avatar": None, "bot": True}
        self.net.messages[int(old["id"])] = old
        first, second = self.post(self.member_id), self.post(self.helper_member)
        await self.configure()
        self.sent.clear()
        await self.command("check_recent_threads", self.forum_mod, forum_channel=self.forum, num_threads=4)
        self.assert_no_errors()
        self.assertEqual(self.sent_to(welcomed["id"]) + self.sent_to(earlier["id"]), [])
        self.assertEqual([body["content"] for body in self.sent_to(first["id"])], [f"Hey <@{self.member_id}>, welcome. <@&{self.helper_id}>, have a look."])
        self.assertEqual([body["content"] for body in self.sent_to(second["id"])], [f"Hey <@{self.helper_member}>, welcome. <@&{self.helper_id}>, have a look."],
                         "each post's own author, where the old bot reused the first post's filled-in text")
        self.assertEqual(self.last_answer(), "Checked the 4 most recent threads in reviews.")
        self.assertIn(f"Automated message with Solved button posted in thread <#{first['id']}> for forum channel <#{self.forum_id}>", await self.log_lines())
        self.sent.clear()
        await self.command("check_recent_threads", self.forum_mod, forum_channel=self.forum, num_threads=4)
        self.assertEqual(self.sent, [], "a second run finds every welcome")

    async def test_without_message_content_another_bots_post_counts_as_a_welcome(self):
        """DECISIONS, Message Content: without it Discord sends another bot's message with no buttons, so a welcome from before the move can't be
        told from any other bot post. A post holding one is left alone rather than welcomed twice, and a post with none still gets its welcome."""
        self.bot.reads_message_text = False
        await self.configure()
        earlier = self.post(self.forum_mod)
        old = self.net.message_json(int(earlier["id"]), {"content": ""}, message_id=int(earlier["id"]) + 1)  # its button stripped, as Discord sends it here
        old["author"] = {"id": "1300000000000000999", "username": "Another bot", "discriminator": "0", "global_name": None, "avatar": None, "bot": True}
        self.net.messages[int(old["id"])] = old
        fresh = self.post(self.member_id)
        self.sent.clear()
        await self.command("check_recent_threads", self.forum_mod, forum_channel=self.forum, num_threads=5)
        self.assert_no_errors()
        self.assertEqual(self.sent_to(earlier["id"]), [])
        self.assertEqual(len(self.sent_to(fresh["id"])), 1)
        self.bot.reads_message_text = True
        other = self.post(self.member_id)
        plain = self.net.message_json(int(other["id"]), {"content": "Hello from another bot"}, message_id=int(other["id"]) + 1)
        plain["author"] = old["author"]
        self.net.messages[int(plain["id"])] = plain
        await self.command("check_recent_threads", self.forum_mod, forum_channel=self.forum, num_threads=5)
        self.assertEqual(len(self.sent_to(other["id"])), 1, "with Message Content a bot post with no button is no welcome")

    def welcomed_long_ago(self, replies: int) -> dict:
        """A post from before the move: its first message, the old bot's welcome with its button right after it, then `replies` replies."""
        thread = self.post(self.member_id)
        base = int(thread["id"])
        self.net.messages[base] = {**self.net.message_json(base, {"content": "It won't heat"}, message_id=base), "author": self.member_json[self.member_id]["user"]}
        old = self.net.message_json(base, {"content": f"Hey <@{self.member_id}>, welcome.", "components": [{"type": 1, "components": [{"type": 2, "style": 3, "label": "Mark as Solved", "custom_id": "solved"}]}]}, message_id=base + 1)
        old["author"] = {"id": "1300000000000000999", "username": "Another bot", "discriminator": "0", "global_name": None, "avatar": None, "bot": True}
        self.net.messages[base + 1] = old
        for number in range(replies):
            self.net.messages[base + 10 + number] = {**self.net.message_json(base, {"content": f"Try step {number}."}, message_id=base + 10 + number), "author": self.member_json[self.helper_member]["user"]}
        return thread

    async def test_an_old_welcome_far_back_is_still_found(self):
        """Review finding 2: the old bot put its welcome right after a post's first message, so a post with 100 replies since holds it past the
        newest 100. The post is read from its start too. A post whose old welcome is 98 replies back is the control."""
        busy, control = self.welcomed_long_ago(100), self.welcomed_long_ago(98)
        await self.configure()
        self.sent.clear()
        await self.command("check_recent_threads", self.owner_id, forum_channel=self.forum, num_threads=20)
        self.assert_no_errors()
        self.assertEqual(self.sent_to(control["id"]), [])
        self.assertEqual(self.sent_to(busy["id"]), [], "the old bot welcomed this post")

    def drop_the_next_welcome(self, *, after_discord_has_it: bool = False) -> None:
        """The next welcome's send ends with the connection dropped and no answer, before or after Discord took the message."""
        inner = self.bot.http.request
        dropped: list[int] = []

        async def dropping(route: Any, **kwargs: Any) -> Any:
            if route.method == "POST" and route.path == "/channels/{channel_id}/messages" and int(route.channel_id) != self.log_id and not dropped:
                dropped.append(int(route.channel_id))
                if after_discord_has_it:
                    await inner(route, **kwargs)
                raise aiohttp.ServerDisconnectedError()  # discord.py lets this through: it retries only OSError 54 and 10054
            return await inner(route, **kwargs)
        self.bot.http.request = dropping

    async def claim_of(self, thread: dict) -> list[dict]:
        return await self.db.all("SELECT message_id FROM forum_welcomes WHERE guild_id = ? AND thread_id = ?", (self.guild_id, int(thread["id"])))

    async def test_a_welcome_cut_off_by_a_dropped_connection_is_posted_later(self):
        """Review finding 1: the claim is crossed off when the send fails without an answer, so /check_recent_threads posts the welcome."""
        await self.configure()
        self.drop_the_next_welcome()
        thread = self.post()
        self.say(thread)
        await self.settle()
        self.assertEqual(await self.claim_of(thread), [])
        self.assertTrue(any(line.startswith(f"Failed to send automated message in thread <#{thread['id']}>") for line in await self.log_lines()))
        await self.command("check_recent_threads", self.owner_id, forum_channel=self.forum, num_threads=5)
        self.assertEqual(len(self.sent_to(thread["id"])), 1)
        self.assert_no_errors()

    async def test_a_welcome_cut_off_by_a_restart_is_posted_later(self):
        await self.configure()
        inner = self.bot.http.request
        started, never = asyncio.Event(), asyncio.Event()

        async def hanging(route: Any, **kwargs: Any) -> Any:
            if route.method == "POST" and route.path == "/channels/{channel_id}/messages" and int(route.channel_id) != self.log_id and not started.is_set():
                started.set()
                await never.wait()  # the stop lands before Discord has the message
            return await inner(route, **kwargs)
        self.bot.http.request = hanging
        thread = self.post()
        self.say(thread)
        await asyncio.wait_for(started.wait(), 10)
        self.cog.cog_unload()  # a stop removes the pack, and its unload cancels the pack's work
        await asyncio.sleep(0.2)
        self.bot.http.request = inner
        self.assertEqual(await self.claim_of(thread), [])
        await self.command("check_recent_threads", self.owner_id, forum_channel=self.forum, num_threads=5)
        self.assertEqual(len(self.sent_to(thread["id"])), 1)

    async def test_a_claim_left_by_a_run_that_stopped_is_no_welcome(self):
        """A process killed during the send leaves its claim with no message. The post is read, and with no welcome in it, it gets one."""
        await self.configure()
        thread = self.post()
        await self.db.run("INSERT INTO forum_welcomes (guild_id, thread_id, channel_id, posted_at) VALUES (?, ?, ?, 0)", (self.guild_id, int(thread["id"]), self.forum_id))
        await self.command("check_recent_threads", self.owner_id, forum_channel=self.forum, num_threads=5)
        self.assertEqual(len(self.sent_to(thread["id"])), 1)
        self.assertEqual(len(await self.claim_of(thread)), 1)
        self.assertIsNotNone((await self.claim_of(thread))[0]["message_id"])

    async def test_a_welcome_discord_took_before_the_drop_isnt_posted_twice(self):
        await self.configure()
        self.drop_the_next_welcome(after_discord_has_it=True)
        thread = self.post()
        self.say(thread)
        await self.settle()
        self.assertEqual(len(self.sent_to(thread["id"])), 1, "it reached Discord, and its answer was lost")
        await self.command("check_recent_threads", self.owner_id, forum_channel=self.forum, num_threads=5)
        self.assertEqual(len(self.sent_to(thread["id"])), 1, "the post's own welcome is found")
        self.assertIsNotNone((await self.claim_of(thread))[0]["message_id"])

    async def test_a_welcome_in_two_parts_that_fails_halfway_leaves_nothing_behind(self):
        """Review, unproven 4: a welcome filled past 2,000 characters goes in two parts. When Discord refuses the second, the first comes down
        too, so /check_recent_threads posts the whole welcome once, not a second copy of the first part."""
        await self.configure(message=("a" * 1500) + chr(10) + ("b" * 482) + " {{OP.Mention}}")
        inner = self.bot.http.request
        thread = self.post()
        refused: list[int] = []

        async def second_part_refused(route: Any, **kwargs: Any) -> Any:
            if route.method == "POST" and route.path == "/channels/{channel_id}/messages" and route.channel_id == int(thread["id"]):
                if len(self.sent_to(thread["id"])) >= 1 and not refused:
                    raise discord.Forbidden(FakeHTTPResponse(403), {"code": 50013, "message": "Missing Permissions"})
            return await inner(route, **kwargs)
        self.bot.http.request = second_part_refused
        self.say(thread)
        await self.settle()
        refused.append(1)
        self.bot.http.request = inner
        self.assertEqual([message["content"][:1] for message in self.posted_in(thread["id"])], [], "the first part came down")
        await self.command("check_recent_threads", self.owner_id, forum_channel=self.forum, num_threads=5)
        self.assertEqual([message["content"][:1] for message in self.posted_in(thread["id"])], ["a", "b"])

    async def test_no_message_set_says_so(self):
        await self.command("check_recent_threads", self.forum_mod, forum_channel=self.forum, num_threads=3)
        self.assertEqual(self.last_answer(), "No message configured for this forum channel.")

    async def test_a_welcome_that_pings_a_role_members_cant_ping_needs_mention_everyone(self):
        await self.configure(message="<@&%s> a new post" % self.role_mod["id"])
        thread = self.post()
        await self.command("check_recent_threads", self.forum_mod, forum_channel=self.forum, num_threads=3)
        self.assertIn("You need Mention Everyone in that channel to ping through the bot.", self.last_answer())
        self.assertEqual(self.sent_to(thread["id"]), [])
        await self.command("check_recent_threads", self.owner_id, forum_channel=self.forum, num_threads=3)
        self.assertEqual(len(self.sent_to(thread["id"])), 1)


class TypedTest(Commanding):
    """Each command works typed after the prefix too, as every command here does."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cog.thread_gap = 0
        self.helper["mentionable"] = True
        self.state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": self.helper})
        await self.configure()

    def said(self) -> list[str]:
        return [body.get("content") or "" for _, body in self.sent]

    async def test_typed_forms_run(self):
        self.typed(self.forum_mod, f"!set_solved_tag <#{self.forum_id}> question")
        await self.settle()
        self.assertEqual(self.settings()["forums"][0]["solved_tag"], self.tags["Question"])
        self.typed(self.forum_mod, f"!set_helper_roles <#{self.forum_id}> <@&{self.helper_id}>")
        await self.settle()
        self.typed(self.forum_mod, "!list_messages")
        await self.settle()
        self.assertTrue(any(line.startswith("Configured forum messages:") for line in self.said()), self.said())
        self.typed(self.forum_mod, f"!close_solved_threads <#{self.forum_id}>")
        self.typed(self.forum_mod, f"!check_recent_threads <#{self.forum_id}> 5")
        self.typed(self.forum_mod, f"!forum_log_channel <#{self.log_id}>")
        await self.settle()
        self.assert_no_errors()
        self.assertIn("Closing solved threads completed. Check the log channel for details.", self.said())
        self.assertIn(f"Checked the 5 most recent threads in {self.forum['name']}.", self.said())
        self.assertIn(f"Log channel set to <#{self.log_id}>", self.said())


class HiddenForumTest(Commanding):
    """Review finding 3: a forum the person can't see is refused by every command that changes it, as /clean_threads already was."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.staff_forum = {**self.forum, "id": str(1400000000000000099), "name": "staff-guides",
                            "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(view_channel=True).value)}]}
        self.net.channels[int(self.staff_forum["id"])] = self.staff_forum
        self.state.parse_channel_create(self.staff_forum)
        self.staff_id = int(self.staff_forum["id"])
        row = {"forum_id": self.staff_id, "message": "Guide by {{OP.Mention}}.", "inactivity_message": "Any news?", "solved_tag": self.tags["Solved"], "helper_roles": []}
        await self.bot.settings.save(self.guild_id, "forummessages", {"forums": [row], "log_channel": self.log_id})

    def said(self) -> list[str]:
        return [body.get("content") or "" for channel_id, body in self.sent if channel_id != self.log_id]

    async def test_a_forum_the_person_cant_see_keeps_its_settings(self):
        before = self.settings()["forums"]
        for line in (f"!clean_threads <#{self.staff_id}> <@&{self.helper_id}>", f"!set_helper_roles <#{self.staff_id}> <@&{self.helper_id}>",
                     f"!set_solved_tag <#{self.staff_id}> question", f"!delete_message <#{self.staff_id}>"):
            self.typed(self.forum_mod, line)
            await self.settle()
            self.assertIn("You can't see that channel.", self.said()[-1], line)
        self.assertEqual(self.settings()["forums"], before)
        self.assert_no_errors()

    async def test_its_tags_arent_listed_to_them(self):
        data = {"id": str(new_id()), "name": "set_solved_tag", "type": 1, "guild_id": str(self.guild_id),
                "options": [{"name": "forum_channel", "type": 7, "value": self.staff_forum["id"]}, {"name": "tag", "type": 3, "value": "", "focused": True}],
                "resolved": {"channels": {self.staff_forum["id"]: self.channel_option(self.staff_forum)}}}
        self.state.parse_interaction_create(self.interaction_payload(4, data, user_id=self.forum_mod))
        await self.settle()
        answer = next(body for method, path, body in self.net.calls if path.endswith("/callback") and body.get("type") == 8)
        self.assertEqual(answer["data"]["choices"], [])
