"""Collaborative messages, through the real library on a faked network: creating, the per-post collaborators, the five right-click menus, moving and
copying with the history, the log channel, the dashboard and bringing a reposted message's history over. The behavior IDs (CM-1 and so on) are
the old bot's, as the build spec numbers them."""
from __future__ import annotations

import json
import time
from typing import Any

import discord
from discord.ext import commands

from captainguild.core import access
from captainguild.modules import collaborative as collab
from captainguild.modules.automod import Automod
from captainguild.modules.collaborative import Collaborative, color_value, previous_version
from captainguild.modules.embeds import Embeds
from tests.gateway import FakeHTTPResponse, iso, new_id, user_json
from tests.voice_report_harness import Harness


class Case(Harness):
    modules = [Embeds, Collaborative]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cog: Collaborative = self.bot.module("collaborative")
        self.embeds: Embeds = self.bot.module("embeds")
        self.text_id = int(self.text["id"])
        self.log_id = int(self.log_channel["id"])
        # What @everyone holds on most servers. The fixture gives it nothing, so a member could see no channel at all.
        usual = discord.Permissions(view_channel=True, send_messages=True, send_messages_in_threads=True, read_message_history=True, use_application_commands=True)
        self.role_everyone["permissions"] = str(usual.value)
        self.state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": self.role_everyone})
        await self.switch(True)

    async def switch(self, on: bool) -> None:
        await self.bot.settings.save(self.guild_id, "collaborative", {"enabled": on})
        await self.bot.sync_server_commands(self.guild_id)

    async def command(self, name: str, user_id: int, *, where: dict | None = None, **options: Any) -> None:
        """A slash command, with channels as well as the people, roles and text the harness sends."""
        resolved: dict = {}
        data = []
        for key, value in options.items():
            if isinstance(value, dict) and "type" in value and "guild_id" in value:
                resolved.setdefault("channels", {})[value["id"]] = {**value, "permissions": str(discord.Permissions.all().value)}
                data.append({"name": key, "type": 7, "value": value["id"]})
            else:
                data.extend(self._option_data({key: value}, resolved))
        # Discord names the server in the command's own data when the command is registered there, and the library looks for it there.
        payload = {"id": str(new_id()), "name": name, "type": 1, "options": data, "resolved": resolved, "guild_id": str(self.guild_id)}
        self.state.parse_interaction_create(self.interaction_payload(2, payload, user_id=user_id, channel=where))
        await self.settle()

    async def message_menu(self, name: str, user_id: int, message: dict, *, channel: dict | None = None) -> None:
        data = {"id": str(new_id()), "name": name, "type": 3, "target_id": message["id"], "resolved": {"messages": {message["id"]: message}}, "guild_id": str(self.guild_id)}
        self.state.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id, channel=channel))
        await self.settle()

    def thread(self, name: str = "notes", parent: dict | None = None) -> dict:
        parent = parent or self.text
        thread_id = new_id()
        data = {"id": str(thread_id), "guild_id": str(self.guild_id), "parent_id": parent["id"], "owner_id": str(self.owner_id), "name": name, "type": 11,
                "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "flags": 0,
                "thread_metadata": {"archived": False, "auto_archive_duration": 1440, "archive_timestamp": iso(), "locked": False}}
        self.net.channels[thread_id] = data
        self.state.parse_thread_create({**data, "newly_created": True})
        return data

    def staff_channel(self) -> dict:
        """A channel @everyone can't see, as a staff channel is set up."""
        staff = {"id": str(new_id()), "guild_id": str(self.guild_id), "name": "staff-only", "type": 0, "position": 9, "parent_id": self.category["id"], "nsfw": False,
                 "topic": None, "rate_limit_per_user": 0, "last_message_id": None, "flags": 0,
                 "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(view_channel=True).value)}]}
        self.net.channels[int(staff["id"])] = staff
        self.state.parse_channel_create(staff)
        return staff

    def archive(self, thread: dict) -> None:
        """Discord archives a quiet thread on its own, and discord.py then drops it from its cache."""
        thread["thread_metadata"]["archived"] = True
        self.state.parse_thread_update(thread)
        self.assertIsNone(self.guild.get_channel_or_thread(int(thread["id"])))

    def refuse_lookup(self, channel_id: int, status: int) -> Any:
        """Discord answers a lookup of this channel with this status, until the returned function is called."""
        real = self.bot.http.request

        async def request(route: Any, **kwargs: Any) -> Any:
            if route.method == "GET" and route.path == "/channels/{channel_id}" and int(route.channel_id) == channel_id:
                kind = {403: discord.Forbidden, 404: discord.NotFound}.get(status, discord.DiscordServerError)
                raise kind(FakeHTTPResponse(status), {"code": 0, "message": "refused for a test"})
            return await real(route, **kwargs)
        self.bot.http.request = request

        def restore() -> None:
            self.bot.http.request = real
        return restore

    def bot_posts(self, channel_id: int | str) -> list[dict]:
        """What the bot posted in a channel itself, leaving out the private answers to interactions, which the fake files there too."""
        return [message for message in self.posts_in(channel_id) if not (message.get("flags") or 0) & 64 and not message.get("webhook_id")]

    def member(self, user_id: int) -> discord.Member:
        return self.guild.get_member(user_id)

    def role(self, data: dict) -> discord.Role:
        return self.guild.get_role(int(data["id"]))

    async def create(self, content: str = "First text", user_id: int | None = None, **options: Any) -> dict:
        """Creates one as an Administrator and returns the message the bot posted."""
        before = len(self.bot_posts(self.text_id))
        await self.command("create_collaborative_message", user_id or self.admin_id, content=content, **options)
        self.assert_no_errors()
        posts = self.bot_posts(self.text_id)
        self.assertEqual(len(posts), before + 1, self.last_answer())
        return posts[-1]

    async def post_row(self, message: dict) -> dict:
        return await self.cog.post_of(self.guild_id, int(message["id"]))

    async def editors(self, message: dict) -> tuple[list[int], list[int]]:
        row = await self.post_row(message)
        return await self.cog.collaborators(self.guild_id, row["posted_id"])

    def link(self, message: dict) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{message['channel_id']}/{message['id']}"

    async def edit(self, message: dict, text: str, user_id: int | None = None) -> None:
        await self.message_menu("Edit Collaborative Message", user_id or self.admin_id, message)
        await self.submit_form(user_id or self.admin_id, text)
        self.assert_no_errors()

    def stored(self, message: dict) -> dict:
        return self.net.messages[int(message["id"])]


class CreateTest(Case):
    async def test_an_administrator_posts_plain_text_and_is_the_first_collaborator(self):
        """CM-1: no color posts plain text in the channel the command ran in, and the invoker comes first among the users."""
        message = await self.create("Hello there", user1=self.member(self.member_id), role1=self.role(self.role_mod))
        self.assertEqual(message["content"], "Hello there")
        self.assertEqual(message["embeds"], [])
        body = self.calls_to("POST", "/channels/{channel_id}/messages")[-1][2]
        self.assertEqual(body["allowed_mentions"]["parse"], ["users"])  # no @everyone or role pings through it
        self.assertEqual(await self.editors(message), ([self.admin_id, self.member_id], [int(self.role_mod["id"])]))
        self.assertIn("Collaborative message created successfully!", self.last_answer())
        row = await self.db.one("SELECT source, posted_by FROM posted_messages WHERE message_id = ?", (int(message["id"]),))
        self.assertEqual((row["source"], row["posted_by"]), ("collaborative", self.admin_id))

    async def test_a_color_makes_an_embed_from_a_name_or_hex(self):
        """CM-1: a color name such as red, or six hex digits with or without #, makes an embed whose description is the content."""
        for given, expected in (("red", discord.Color.red().value), ("#00ff00", 0x00FF00), ("ff8800", 0xFF8800), ("dark_teal", discord.Color.dark_teal().value)):
            message = await self.create("Embed text", color=given)
            self.assertEqual(message["content"], "")
            self.assertEqual((message["embeds"][0]["description"], message["embeds"][0]["color"]), ("Embed text", expected), given)

    async def test_a_bad_color_is_refused_with_the_old_words(self):
        for given in ("nonsense", "#fff", "to_rgb", "from_rgb", "value"):
            posts = len(self.bot_posts(self.text_id))
            await self.command("create_collaborative_message", self.admin_id, content="x", color=given)
            self.assert_no_errors()
            self.assertIn(collab.INVALID_COLOR, self.last_answer(), given)
            self.assertEqual(len(self.bot_posts(self.text_id)), posts, given)

    def test_color_names_are_the_librarys(self):
        self.assertEqual(color_value("Blurple"), discord.Color.blurple().value)
        self.assertEqual(color_value("dark gray"), discord.Color.dark_gray().value)
        self.assertNotIn("from_rgb", collab.COLOR_MAKERS)

    async def test_only_administrators_and_creator_roles_create(self):
        """CM-1, CM-13: a member needs a creator role."""
        await self.command("create_collaborative_message", self.member_id, content="Mine")
        self.assertIn("You do not have permission to create collaborative messages.", self.last_answer())
        await self.bot.settings.save(self.guild_id, "collaborative", {"creator_roles": [int(self.role_member["id"])]})
        message = await self.create("Mine", user_id=self.member_id)
        self.assertEqual(await self.editors(message), ([self.member_id], []))

    async def test_someone_who_cant_post_there_is_refused(self):
        """DECISIONS, posting: the bot posts only where the person could post themselves."""
        await self.bot.settings.save(self.guild_id, "collaborative", {"creator_roles": [int(self.role_member["id"])]})
        quiet = {**self.text, "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(send_messages=True).value)}]}
        self.state.parse_channel_update(quiet)
        posts = len(self.bot_posts(self.text_id))
        await self.command("create_collaborative_message", self.member_id, content="Mine", where=quiet)
        self.assertIn("You can't post in that channel.", self.last_answer())
        self.assertEqual(len(self.bot_posts(self.text_id)), posts)

    async def test_everyone_cant_be_a_collaborator(self):
        await self.command("create_collaborative_message", self.admin_id, content="x", role1=self.guild.default_role)
        self.assertIn(access.NOT_EVERYONE, self.last_answer())

    async def test_text_over_discords_limits_is_refused(self):
        await self.command("create_collaborative_message", self.admin_id, content="a" * 2001)
        self.assertIn("at most 2000 characters", self.last_answer())
        message = await self.create("b" * 4096, color="red")
        self.assertEqual(len(message["embeds"][0]["description"]), 4096)

    async def test_the_typed_form_reads_people_and_roles_after_an_optional_color(self):
        """CM-1 typed: a word that isn't a color goes on to the users and roles."""
        before = len(self.bot_posts(self.text_id))
        self.dispatch_message(user_id=self.admin_id, content=f'!create_collaborative_message "Two words" <@{self.member_id}> <@&{self.role_mod["id"]}>')
        await self.settle()
        self.assert_no_errors()
        posts = self.bot_posts(self.text_id)
        message = next(post for post in posts[before:] if post["content"] == "Two words")
        self.assertEqual(await self.editors(message), ([self.admin_id, self.member_id], [int(self.role_mod["id"])]))


class PackTest(Case):
    async def test_the_pack_holds_the_old_commands_and_menus(self):
        """CM-20: they reach a server only when it switches the pack on."""
        names = {command["name"] for command in self.net.guild_commands[self.guild_id]}
        self.assertEqual(names, {"create_collaborative_message", "add_collaborators", "remove_collaborators", "move_collaborative_message", "copy_collaborative_message",
                                 "list_collaborative_messages", "prune", "allow_collaboration_creation", "deny_collaboration_creation", "collaborative_log_channel",
                                 "Edit Collaborative Message", "View Revision History", "Revert to Previous Revision", "Delete Collaborative Message", "See Current Collaborators"})
        await self.switch(False)
        self.assertEqual(self.net.guild_commands[self.guild_id], [])

    async def test_a_menu_with_the_pack_off_says_so(self):
        message = await self.create()
        await self.bot.settings.save(self.guild_id, "collaborative", {"enabled": False})
        await self.message_menu("Edit Collaborative Message", self.admin_id, message)
        self.assertIn("switched off in this server", self.last_answer())
        self.assertEqual(self.net.modals, [])

    def test_it_reads_no_messages_of_its_own(self):
        """CM-21: the old bot's on_message did nothing, and this pack doesn't listen to messages at all."""
        self.assertNotIn("on_message", {name for name, _ in Collaborative.get_listeners(self.cog)})

    def test_help_is_the_shared_one(self):
        """CM-16: the shared /help lists these, so the pack has none of its own."""
        self.assertNotIn("help", {command.name for command in self.bot.pack_commands["collaborative"]})


class PermissionTest(Case):
    async def test_a_collaborator_by_name_or_role_may_change_it_and_others_may_not(self):
        """CM-17."""
        message = await self.create(user1=self.member(self.mod_id))
        await self.message_menu("Edit Collaborative Message", self.member_id, message)
        self.assertIn(collab.NO_PERMISSION, self.last_answer())
        self.assertEqual(self.net.modals, [])
        await self.message_menu("Edit Collaborative Message", self.mod_id, message)
        self.assertEqual(len(self.net.modals), 1)
        row = await self.post_row(message)
        await self.cog.set_collaborators(self.guild_id, row["posted_id"], [], [int(self.role_member["id"])])
        await self.message_menu("Edit Collaborative Message", self.member_id, message)
        self.assertEqual(len(self.net.modals), 2)

    async def test_an_administrator_always_may(self):
        message = await self.create()
        row = await self.post_row(message)
        await self.cog.set_collaborators(self.guild_id, row["posted_id"], [], [])
        await self.message_menu("Edit Collaborative Message", self.owner_id, message)
        self.assertEqual(len(self.net.modals), 1)

    async def test_a_message_that_isnt_collaborative_says_so(self):
        other = self.net.message_json(self.text_id, {"content": "plain"})
        self.net.messages[int(other["id"])] = other
        for menu in ("Edit Collaborative Message", "View Revision History", "Revert to Previous Revision", "Delete Collaborative Message"):
            await self.message_menu(menu, self.admin_id, other)
            self.assertIn(collab.NOT_COLLABORATIVE, self.last_answer(), menu)
        await self.message_menu("See Current Collaborators", self.admin_id, other)
        self.assertIn(collab.NOT_COLLABORATIVE_HERE, self.last_answer())


class EditTest(Case):
    async def test_the_form_edits_the_text_and_keeps_the_old_one(self):
        """CM-4, CM-19: the form opens on the current text, and the edit adds a version with who made it."""
        message = await self.create("Old words", user1=self.member(self.member_id))
        await self.message_menu("Edit Collaborative Message", self.member_id, message)
        modal = self.net.modals[-1]
        self.assertEqual(modal["title"], "Edit Collaborative Message")
        field = modal["components"][0].get("component") or modal["components"][0]["components"][0]
        self.assertEqual((field["label"], field["value"], field["max_length"]), ("New Content", "Old words", 2000))
        await self.submit_form(self.member_id, "New words")
        self.assert_no_errors()
        self.assertEqual(self.stored(message)["content"], "New words")
        self.assertIn(f"Message {message['id']} successfully updated!", self.last_answer())
        row = await self.post_row(message)
        versions = await self.embeds.versions(self.guild_id, row["posted_id"])
        self.assertEqual([v["payload"]["content"] for v in versions], ["Old words", "New words"])
        self.assertEqual(versions[-1]["saved_by"], self.member_id)
        self.assertAlmostEqual(versions[-1]["saved_at"], time.time(), delta=60)

    async def test_an_embed_post_edits_its_description_and_keeps_its_color(self):
        message = await self.create("Old", color="red")
        await self.edit(message, "New")
        embed = self.stored(message)["embeds"][0]
        self.assertEqual((embed["description"], embed["color"]), ("New", discord.Color.red().value))

    async def test_a_link_preview_doesnt_make_plain_text_an_embed(self):
        """The old bot read a link's preview as the post's embed. Only a rich embed counts here."""
        message = await self.create("See https://example.com")
        self.stored(message)["embeds"] = [{"type": "link", "url": "https://example.com", "title": "Example"}]
        message = self.stored(message)
        await self.edit(message, "See https://example.org")
        self.assertEqual(self.stored(message)["content"], "See https://example.org")

    async def test_the_form_is_checked_again_when_it_comes_back(self):
        message = await self.create(user1=self.member(self.member_id))
        await self.message_menu("Edit Collaborative Message", self.member_id, message)
        row = await self.post_row(message)
        await self.cog.set_collaborators(self.guild_id, row["posted_id"], [], [])
        await self.submit_form(self.member_id, "Too late")
        self.assertIn(collab.NO_PERMISSION, self.last_answer())
        self.assertEqual(self.stored(message)["content"], "First text")


    async def test_an_administrator_edits_after_the_thread_archives_with_the_form_open(self):
        """Discord takes no application commands in an archived thread, so the form opens while the thread is active, and the thread can archive on its
        own before the form comes back. An Administrator skips the collaborator check, and the edit still finds the thread."""
        thread = self.thread(name="faq")
        await self.command("create_collaborative_message", self.admin_id, where=thread, content="Thread text", user1=self.member(self.member_id))
        self.assert_no_errors()
        message = self.bot_posts(thread["id"])[-1]
        await self.message_menu("Edit Collaborative Message", self.admin_id, message, channel=thread)
        self.archive(thread)
        await self.submit_form(self.admin_id, "Admin's fix", channel=thread)
        self.assert_no_errors()
        self.assertIn(f"Message {message['id']} successfully updated!", self.last_answer())
        self.assertEqual(self.stored(message)["content"], "Admin's fix")


class HistoryTest(Case):
    async def test_the_panel_walks_the_earlier_texts_and_reverts_one(self):
        """CM-5: oldest first, Previous and Next, and Revert puts that text back as a new version."""
        message = await self.create("One")
        await self.edit(message, "Two")
        await self.edit(message, "Three")
        await self.message_menu("View Revision History", self.admin_id, message)
        panel = self.net.posted[-1]
        embed = panel["embeds"][0]
        self.assertEqual(embed["title"], "Revision 1/2")
        self.assertIn("**Content:**\nOne", embed["description"])
        self.assertIn("**Created At:** <t:", embed["description"])
        buttons = self.buttons(panel)
        await self.press(buttons["Previous"]["custom_id"], panel, self.admin_id)
        self.assertIn("This is the first revision.", self.last_answer())
        await self.press(buttons["Next"]["custom_id"], panel, self.admin_id)
        self.assertEqual(self.net.posted[-1]["embeds"][0]["title"], "Revision 2/2")
        await self.press(buttons["Next"]["custom_id"], panel, self.admin_id)
        self.assertIn("This is the latest revision.", self.last_answer())
        await self.press(buttons["Revert"]["custom_id"], panel, self.admin_id)
        self.assert_no_errors()
        self.assertEqual(self.stored(message)["content"], "Two")
        self.assertIn(f"Reverted message {message['id']} to revision 2.", self.last_answer())
        row = await self.post_row(message)
        versions = await self.embeds.versions(self.guild_id, row["posted_id"])
        self.assertEqual([v["payload"]["content"] for v in versions], ["One", "Two", "Three", "Two"])
        self.assertEqual(versions[-1]["note"], "Reverted to version 2")

    async def test_someone_who_may_not_change_it_sees_revert_switched_off(self):
        message = await self.create("One")
        await self.edit(message, "Two")
        await self.message_menu("View Revision History", self.member_id, message)
        self.assertTrue(self.buttons(self.net.posted[-1])["Revert"]["disabled"])

    async def test_no_earlier_text_says_so(self):
        message = await self.create("Only")
        await self.message_menu("View Revision History", self.admin_id, message)
        self.assertIn("No revisions found for this message.", self.last_answer())


class RevertTest(Case):
    async def test_each_revert_goes_one_further_back_and_nothing_is_lost(self):
        """CM-6: the old bot used up each revision it restored. Here each revert is a new version, and the next one carries on from the text it restored."""
        message = await self.create("A")
        for text in ("B", "C"):
            await self.edit(message, text)
        said = []
        for _ in range(3):
            await self.message_menu("Revert to Previous Revision", self.admin_id, message)
            said.append((self.stored(message)["content"], self.last_answer()))
        self.assertEqual([text for text, _ in said], ["B", "A", "A"])
        self.assertIn("Message successfully reverted to the previous revision.", said[0][1])
        self.assertIn("No previous revisions exist for this message.", said[2][1])
        row = await self.post_row(message)
        self.assertEqual(len(await self.embeds.versions(self.guild_id, row["posted_id"])), 5)

    def test_the_walk_follows_reverts_and_an_edit_after_one(self):
        def row(number: int, note: str = "Edited") -> dict:
            return {"number": number, "note": note}
        self.assertEqual(previous_version([row(1), row(2), row(3)]), 2)
        self.assertEqual(previous_version([row(1), row(2), row(3), row(4, "Reverted to version 2")]), 1)
        self.assertEqual(previous_version([row(1), row(2), row(3), row(4, "Reverted to version 2"), row(5)]), 4)
        self.assertEqual(previous_version([row(1), row(2), row(3, "Reverted to version 1")]), None)
        self.assertIsNone(previous_version([row(1)]))
        self.assertIsNone(previous_version([]))


class DeleteTest(Case):
    async def test_delete_takes_the_message_and_its_history(self):
        """CM-7."""
        message = await self.create(user1=self.member(self.member_id))
        await self.edit(message, "Two")
        await self.message_menu("Delete Collaborative Message", self.member_id, message)
        self.assert_no_errors()
        self.assertNotIn(int(message["id"]), self.net.messages)
        self.assertIn("Message and all associated revisions successfully deleted.", self.last_answer())
        for table in ("collab_posts", "collab_editors", "posted_messages", "posted_versions"):
            self.assertEqual(await self.db.scalar(f"SELECT COUNT(*) FROM {table}"), 0, table)


class CollaboratorsTest(Case):
    async def test_anyone_can_see_who_collaborates(self):
        """CM-8."""
        message = await self.create(user1=self.member(self.member_id), role1=self.role(self.role_mod))
        await self.message_menu("See Current Collaborators", self.member_id, message)
        answer = self.last_answer()
        self.assertIn(f"**Current Collaborators for Message {message['id']}:**", answer)
        self.assertIn("**Users:** admin, member", answer)
        self.assertIn("**Roles:** Moderator", answer)

    async def test_a_deleted_role_is_left_out_and_none_says_none(self):
        message = await self.create()
        row = await self.post_row(message)
        await self.cog.set_collaborators(self.guild_id, row["posted_id"], [], [12345678901234567])
        await self.message_menu("See Current Collaborators", self.member_id, message)
        self.assertIn("**Users:** None\n**Roles:** None", self.last_answer())

    async def test_add_by_link_or_id_says_who_was_new(self):
        """CM-2."""
        message = await self.create()
        await self.command("add_collaborators", self.admin_id, message_identifier=self.link(message), user1=self.member(self.member_id), role1=self.role(self.role_mod))
        self.assertIn("User member and Role Moderator successfully added as collaborators.", self.last_answer())
        await self.command("add_collaborators", self.admin_id, message_identifier=message["id"], user1=self.member(self.member_id), user2=self.member(self.mod_id))
        answer = self.last_answer()
        self.assertIn("User mod successfully added as collaborators.", answer)
        self.assertIn("User member are already collaborators. No changes made.", answer)
        self.assertEqual(await self.editors(message), ([self.admin_id, self.member_id, self.mod_id], [int(self.role_mod["id"])]))

    async def test_add_refuses_what_the_old_bot_choked_on(self):
        message = await self.create()
        await self.command("add_collaborators", self.admin_id, message_identifier=message["id"])
        self.assertIn("Name at least one user or role to add.", self.last_answer())
        await self.command("add_collaborators", self.admin_id, message_identifier="not a link")
        self.assertIn("That isn't a message ID or link.", self.last_answer())
        await self.command("add_collaborators", self.admin_id, message_identifier=f"https://discord.com/channels/1/2/{message['id']}", user1=self.member(self.member_id))
        self.assertIn("outside this server", self.last_answer())
        await self.command("add_collaborators", self.admin_id, message_identifier="123456789012345678", user1=self.member(self.member_id))
        self.assertIn("Message ID 123456789012345678 not found.", self.last_answer())
        self.assert_no_errors()

    async def test_only_a_collaborator_adds(self):
        message = await self.create()
        await self.command("add_collaborators", self.member_id, message_identifier=message["id"], user1=self.member(self.member_id))
        self.assertIn(collab.NO_PERMISSION, self.last_answer())
        self.assertEqual((await self.editors(message))[0], [self.admin_id])

    async def test_adding_takes_being_able_to_post_there_as_the_page_asks(self):
        """CM-2 against the dashboard: adding a collaborator hands on the right to have the bot post there, so the command refuses what the page refuses."""
        quiet = {**self.log_channel, "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(send_messages=True).value)}]}
        self.state.parse_channel_update(quiet)
        await self.command("create_collaborative_message", self.admin_id, where=quiet, content="Announcement", user1=self.member(self.member_id))
        message = self.bot_posts(self.log_id)[-1]
        await self.command("add_collaborators", self.member_id, message_identifier=message["id"], user1=self.member(self.mod_id), role1=self.role(self.role_member))
        self.assertIn("You can't post in that channel.", self.last_answer())
        self.assertEqual(await self.editors(message), ([self.admin_id, self.member_id], []))
        await self.command("remove_collaborators", self.member_id, message_identifier=message["id"], user1=self.member(self.admin_id))
        self.assertIn("User admin successfully removed as collaborators.", self.last_answer())

    async def test_remove_keeps_you_and_says_who_wasnt_one(self):
        """CM-3."""
        message = await self.create(user1=self.member(self.member_id), user2=self.member(self.mod_id))
        await self.command("remove_collaborators", self.member_id, message_identifier=message["id"], user1=self.member(self.member_id))
        self.assertIn("You cannot remove yourself as a collaborator.", self.last_answer())
        await self.command("remove_collaborators", self.member_id, message_identifier=message["id"], user1=self.member(self.mod_id), role1=self.role(self.role_mod))
        answer = self.last_answer()
        self.assertIn("User mod successfully removed as collaborators.", answer)
        self.assertIn("Role Moderator are not yet collaborators and could not be removed.", answer)
        self.assertEqual(await self.editors(message), ([self.admin_id, self.member_id], []))

    async def test_the_limit_on_collaborators_holds(self):
        message = await self.create()
        row = await self.post_row(message)
        await self.cog.set_collaborators(self.guild_id, row["posted_id"], list(range(10 ** 17, 10 ** 17 + collab.MAX_USERS)), [])
        await self.command("add_collaborators", self.admin_id, message_identifier=message["id"], user1=self.member(self.member_id))
        self.assertIn(collab.TOO_MANY, self.last_answer())


class MoveCopyTest(Case):
    async def test_move_reposts_deletes_the_original_and_keeps_history_and_collaborators(self):
        """CM-9."""
        message = await self.create("Moving", user1=self.member(self.member_id))
        await self.edit(message, "Moving along")
        row = await self.post_row(message)
        await self.command("move_collaborative_message", self.member_id, message_identifier=self.link(message), destination=self.log_channel)
        self.assert_no_errors()
        self.assertIn(f"Message successfully moved to <#{self.log_id}>.", self.last_answer())
        self.assertNotIn(int(message["id"]), self.net.messages)
        moved = self.bot_posts(self.log_id)[-1]
        self.assertEqual(moved["content"], "Moving along")
        body = self.calls_to("POST", "/channels/{channel_id}/messages")[-1][2]
        self.assertEqual(body["allowed_mentions"]["parse"], [])
        after = await self.post_row(moved)
        self.assertEqual((after["posted_id"], after["channel_id"]), (row["posted_id"], self.log_id))
        self.assertEqual(len(await self.embeds.versions(self.guild_id, row["posted_id"])), 2)
        self.assertEqual(await self.editors(moved), ([self.admin_id, self.member_id], []))

    async def test_copy_keeps_the_original_and_copies_history_and_collaborators(self):
        """CM-10."""
        message = await self.create("Copy me", color="blue", role1=self.role(self.role_mod))
        await self.edit(message, "Copy me now")
        await self.command("copy_collaborative_message", self.admin_id, message_identifier=message["id"], destination=self.log_channel)
        self.assert_no_errors()
        self.assertIn(f"Message successfully copied to <#{self.log_id}>.", self.last_answer())
        self.assertIn(int(message["id"]), self.net.messages)
        copy = self.bot_posts(self.log_id)[-1]
        self.assertEqual(copy["embeds"][0]["description"], "Copy me now")
        old, new = await self.post_row(message), await self.post_row(copy)
        self.assertNotEqual(old["posted_id"], new["posted_id"])
        texts = lambda versions: [v["payload"]["embeds"][0]["description"] for v in versions]  # noqa: E731
        self.assertEqual(texts(await self.embeds.versions(self.guild_id, new["posted_id"])), ["Copy me", "Copy me now"])
        self.assertEqual(await self.editors(copy), ([self.admin_id], [int(self.role_mod["id"])]))

    async def test_a_spoilered_file_stays_spoilered_in_a_repost(self):
        """The repost behind a move, a copy and the Embeds page's repost tool. discord.py's Attachment.to_file takes spoiler=False unless told."""
        message = self.net.message_json(self.text_id, {"content": "A post with a spoiler"})
        message["attachments"] = [{"id": str(new_id()), "filename": "SPOILER_plot.png", "size": 10, "url": "https://cdn.discordapp.com/attachments/1/2/SPOILER_plot.png",
                                   "proxy_url": "https://media.discordapp.net/attachments/1/2/SPOILER_plot.png", "content_type": "image/png"}]
        self.net.messages[int(message["id"])] = message
        await self.bot.ops.repost_message(self.text_id, int(message["id"]), self.log_id, {"content": "A post with a spoiler", "embeds": [], "buttons": []})
        self.assertEqual(self.net.uploads[-1][1], ["SPOILER_plot.png"])

    async def test_a_repost_carries_a_file_under_discords_default_limit(self):
        """The repost behind a move, a copy and the Embeds page's repost tool. Discord's default upload limit is 20 MiB since September 3, 2026, and
        discord.py 2.7.1's table still gives 10 MiB below boost level 2."""
        message = self.net.message_json(self.text_id, {"content": "A post with a clip"})
        file = lambda name, size: {"id": str(new_id()), "filename": name, "size": size, "url": f"https://cdn.discordapp.com/attachments/1/2/{name}",  # noqa: E731
                                   "proxy_url": f"https://media.discordapp.net/attachments/1/2/{name}"}
        message["attachments"] = [file("clip.mp4", 15 * 1024 * 1024), file("huge.zip", 21 * 1024 * 1024)]
        self.net.messages[int(message["id"])] = message
        sent = await self.bot.ops.repost_message(self.text_id, int(message["id"]), self.log_id, {"content": "A post with a clip", "embeds": [], "buttons": []})
        self.assertEqual((self.net.uploads, sent["skipped"]), ([(self.log_id, ["clip.mp4"])], ["huge.zip"]))

    async def test_a_destination_the_mover_cant_post_in_is_refused(self):
        message = await self.create(user1=self.member(self.member_id))
        quiet = {**self.log_channel, "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(send_messages=True).value)}]}
        self.state.parse_channel_update(quiet)
        await self.command("move_collaborative_message", self.member_id, message_identifier=message["id"], destination=quiet)
        self.assertIn("You can't post in that channel.", self.last_answer())
        self.assertIn(int(message["id"]), self.net.messages)

    async def test_a_message_that_is_gone_says_so(self):
        message = await self.create()
        self.net.messages.pop(int(message["id"]))
        await self.command("copy_collaborative_message", self.admin_id, message_identifier=message["id"], destination=self.log_channel)
        self.assertIn(f"Message {message['id']} not found.", self.last_answer())


class ListAndPruneTest(Case):
    async def test_list_pages_by_ten_with_links(self):
        """CM-11."""
        await self.command("list_collaborative_messages", self.admin_id)
        self.assertIn("There are no collaborative messages on the server.", self.last_answer())
        made = [await self.create(f"Post number {n}") for n in range(11)]
        await self.command("list_collaborative_messages", self.admin_id)
        page = self.net.posted[-1]
        embed = page["embeds"][0]
        self.assertEqual(embed["title"], "Collaborative Messages (Page 1/2)")
        self.assertIn(f"[Message Link]({self.link(made[0])}) in **#general**: Post number 0", embed["description"])
        await self.press(self.buttons(page)["Next"]["custom_id"], page, self.admin_id)
        self.assertEqual(self.net.posted[-1]["embeds"][0]["title"], "Collaborative Messages (Page 2/2)")
        await self.press(self.buttons(page)["Next"]["custom_id"], page, self.admin_id)
        self.assertIn("This is the last page.", self.last_answer())

    async def test_list_hides_an_archived_thread_the_asker_cant_read(self):
        """CM-11, DECISIONS, reading: a bot manager passes the command's Administrator check, and an archived thread isn't in the cache, so its channel
        has to be looked up before its text can be shown or hidden."""
        thread = self.thread(name="appeals", parent=self.staff_channel())
        await self.command("create_collaborative_message", self.admin_id, where=thread, content="Draft: the ban appeal policy")
        self.archive(thread)
        await self.bot.settings.save(self.guild_id, "commands", {"mod_role": int(self.role_mod["id"])})
        await self.command("list_collaborative_messages", self.mod_id)
        self.assertIn("No accessible collaborative messages found on the server.", self.last_answer())
        await self.command("list_collaborative_messages", self.admin_id)
        self.assertIn("in thread **appeals** (in **#staff-only**): Draft: the ban appeal policy", self.net.posted[-1]["embeds"][0]["description"])

    async def test_list_and_prune_need_an_administrator(self):
        for name in ("list_collaborative_messages", "prune"):
            await self.command(name, self.mod_id)
            self.assertIn("You need Administrator", self.last_answer(), name)

    async def test_prune_is_for_the_owner_and_administrators_as_the_old_check_was(self):
        """CM-12: the old bot checked Administrator itself, and the bot manager role passes an Administrator command, so prune checks for itself, with the
        sentence the mover's prune and its page use."""
        gone = await self.create("gone")
        self.net.messages.pop(int(gone["id"]))
        await self.bot.settings.save(self.guild_id, "commands", {"mod_role": int(self.role_mod["id"])})
        await self.command("prune", self.mod_id)
        self.assertIn(collab.PRUNE_REFUSED, self.last_answer())
        self.assertIsNotNone(await self.post_row(gone))
        await self.command("prune", self.admin_id)
        self.assertIn("Pruned 1 message(s)", self.last_answer())

    async def test_prune_forgets_what_is_gone_and_keeps_what_it_cant_read(self):
        """CM-12: a message the bot can't read may still be there, which the old bot pruned anyway."""
        gone, hidden, kept = await self.create("gone"), await self.create("hidden"), await self.create("kept")
        self.net.messages.pop(int(gone["id"]))
        real = self.bot.http.request

        async def request(route: Any, **kwargs: Any) -> Any:
            if route.method == "GET" and str(hidden["id"]) in route.url:
                raise discord.Forbidden(type("R", (), {"status": 403, "reason": "no"})(), {"code": 50001, "message": "Missing Access"})
            return await real(route, **kwargs)
        self.bot.http.request = request
        orphan = await self.create("orphan")
        await self.embeds.forget_post(self.guild_id, (await self.post_row(orphan))["posted_id"])
        await self.command("prune", self.admin_id)
        self.assert_no_errors()
        self.assertIn("Pruned 1 message(s) and 2 orphaned record(s) from the database.", self.last_answer())
        self.assertIsNone(await self.cog.post_of(self.guild_id, int(gone["id"])))
        self.assertIsNotNone(await self.cog.post_of(self.guild_id, int(hidden["id"])))
        self.assertIsNotNone(await self.cog.post_of(self.guild_id, int(kept["id"])))


class PruneLookupTest(Case):
    async def test_prune_keeps_a_post_whose_thread_discord_wont_look_up_and_forgets_one_that_is_gone(self):
        """CM-12: a refusal or an outage says nothing about whether the post is there, and only Discord's not-found does."""
        for status, kept in ((403, True), (503, True), (404, False)):
            thread = self.thread(name=f"notes-{status}")
            await self.command("create_collaborative_message", self.admin_id, where=thread, content="Version one")
            post = self.bot_posts(thread["id"])[-1]
            self.archive(thread)
            restore = self.refuse_lookup(int(thread["id"]), status)
            await self.command("prune", self.admin_id)
            restore()
            self.assert_no_errors()
            self.assertEqual(await self.cog.post_of(self.guild_id, int(post["id"])) is not None, kept, status)
            self.assertIn(f"Pruned {0 if kept else 1} message(s)", self.last_answer(), status)


class SettingsCommandTest(Case):
    async def test_allow_and_deny_change_the_creator_roles(self):
        """CM-13, CM-14."""
        await self.command("allow_collaboration_creation", self.admin_id, role=self.role(self.role_member))
        self.assertIn("Role Member has been granted access to create collaborative messages.\nCurrent roles with access: Member", self.last_answer())
        self.assertEqual(self.bot.settings.get(self.guild_id, "collaborative")["creator_roles"], [int(self.role_member["id"])])
        await self.command("deny_collaboration_creation", self.admin_id, role=self.role(self.role_member))
        self.assertIn("Role Member has been denied access to create collaborative messages.\nCurrent roles with access: ", self.last_answer())
        self.assertEqual(self.bot.settings.get(self.guild_id, "collaborative")["creator_roles"], [])
        await self.command("allow_collaboration_creation", self.admin_id, role=self.guild.default_role)
        self.assertIn(access.NOT_EVERYONE, self.last_answer())
        await self.command("allow_collaboration_creation", self.mod_id, role=self.role(self.role_member))
        self.assertIn("You need Administrator", self.last_answer())

    async def test_the_log_channel_logs_what_happens_and_mentions_nobody(self):
        """CM-15, CM-18: enabled is 0 or 1, and the reply and the logging agree on it."""
        await self.command("collaborative_log_channel", self.admin_id, channel=self.log_channel, enabled=0)
        self.assertIn(f"Log channel set to <#{self.log_id}> and logging disabled.", self.last_answer())
        await self.create("Not logged")
        self.assertEqual(self.bot_posts(self.log_id), [])
        await self.command("collaborative_log_channel", self.admin_id, channel=self.log_channel, enabled=1)
        self.assertIn("and logging enabled.", self.last_answer())
        message = await self.create("Logged")
        await self.edit(message, "Logged again")
        lines = [post["content"] for post in self.bot_posts(self.log_id)]
        self.assertTrue(any("set log channel to" in line for line in lines), lines)
        self.assertTrue(any(f"admin ({self.admin_id}) created a new collaborative message" in line for line in lines), lines)
        self.assertTrue(any("edited collaborative message" in line for line in lines), lines)
        logged = [call[2] for call in self.calls_to("POST", "/channels/{channel_id}/messages") if call[2].get("content", "").startswith("admin (")]
        self.assertTrue(all(body["allowed_mentions"]["parse"] == [] for body in logged))
        settings = self.bot.settings.get(self.guild_id, "collaborative")
        self.assertEqual((settings["log_channel"], settings["log_enabled"]), (self.log_id, True))

    async def test_enabled_takes_only_0_or_1(self):
        command = next(c for c in self.bot.pack_commands["collaborative"] if c.name == "collaborative_log_channel").to_dict(self.bot.tree)
        enabled = next(o for o in command["options"] if o["name"] == "enabled")
        self.assertEqual((enabled["min_value"], enabled["max_value"]), (0, 1))


class DashboardTest(Case):
    async def test_staff_see_and_change_the_collaborators(self):
        message = await self.create(user1=self.member(self.member_id))
        client = await self.client()
        rows = (await (await client.get(self.path("/collaborative/posts"))).json())["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual([str(user) for user in rows[0]["users"]], [str(self.admin_id), str(self.member_id)])
        posted = rows[0]["posted_id"]
        response = await client.put(self.path(f"/collaborative/posts/{posted}"), json={"users": [str(self.mod_id)], "roles": [self.role_member["id"]]})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await self.editors(message), ([self.mod_id], [int(self.role_member["id"])]))
        for body, field in (({"roles": [str(self.guild_id)]}, access.NOT_EVERYONE), ({"users": ["123456789012345678"]}, "isn't in this server"), ({"roles": ["123456789012345678"]}, "isn't in this server")):
            response = await client.put(self.path(f"/collaborative/posts/{posted}"), json=body)
            self.assertEqual(response.status, 400, body)
            self.assertIn(field, (await response.json())["error"])
        recent = await self.bot.activity_log.recent(self.guild_id)
        self.assertEqual(recent[0]["action"], "collaborative.collaborators")

    async def test_a_post_in_an_archived_thread_the_person_cant_read_isnt_theirs_to_change(self):
        """DECISIONS, reading: the page lists only the posts this person can read. An archived thread isn't in the cache, so a change looks it up first,
        as the list does, and a post this person can't read is as missing to them as it is in the list."""
        thread = self.thread(name="appeals", parent=self.staff_channel())
        await self.command("create_collaborative_message", self.admin_id, where=thread, content="Draft: the ban appeal policy", user1=self.member(self.mod_id))
        self.assert_no_errors()
        message = self.bot_posts(thread["id"])[-1]
        row = await self.post_row(message)
        self.archive(thread)
        client = await self.client()
        data = {**self.member_json[self.admin_id], "roles": [self.role_mod["id"]]}  # the person signed in keeps the page and loses Administrator
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **data})
        # Sent straight, with a number guessed, since they count up from 1. Opening the list first would look the thread up for the change.
        response = await client.put(self.path(f"/collaborative/posts/{row['posted_id']}"), json={"users": [], "roles": []})
        self.assertEqual(response.status, 404, await response.text())
        self.assertEqual(await self.editors(message), ([self.admin_id, self.mod_id], []))
        self.assertEqual((await (await client.get(self.path("/collaborative/posts"))).json())["rows"], [])

    async def test_only_an_administrator_names_creator_roles_on_the_settings_page(self):
        """CM-13 on the dashboard: a creator role lets people post through the bot, so it's set like a staff role."""
        client = await self.client()
        data = {**self.member_json[self.admin_id], "roles": [self.role_mod["id"]]}  # the person signed in keeps Manage Server's page access and loses Administrator
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **data})
        response = await client.put(self.path("/settings/collaborative"), json={"creator_roles": [self.role_member["id"]]})
        self.assertEqual(response.status, 400)
        self.assertIn(access.ONLY_ADMINS, (await response.json())["error"])
        self.assertEqual(self.cog.posted_channels({"log_channel": 7}), {"log_channel": {7}})


class ImportTest(Case):
    """The importer adopts another bot's message and then hands its history and collaborators over through this endpoint."""

    async def adopt(self, *, content: str, embeds: list | None = None) -> tuple[dict, dict]:
        old = self.net.message_json(self.text_id, {"content": content, "embeds": embeds or []})
        old["author"] = user_json(new_id(), "Old Bot", bot=True)
        self.net.messages[int(old["id"])] = old
        client = await self.client()
        response = await client.post(self.path("/embeds/adoptions"), json={"messages": [{"channel_id": self.text["id"], "message_id": old["id"]}], "delete_originals": True})
        self.assertEqual(response.status, 200, await response.text())
        await self.embeds.finish_jobs()
        return old, self.bot_posts(self.text_id)[-1]

    async def test_history_goes_in_front_in_order_with_the_old_times(self):
        old, new = await self.adopt(content="Third")
        client = await self.client()
        status = await (await client.get(self.path(f"/collaborative/import?ids={old['id']}"))).json()
        self.assertEqual(status["rows"][0]["collaborative"], False)
        self.assertEqual(str(status["rows"][0]["message_id"]), new["id"])
        body = {"source_message_id": old["id"], "created_at": 1000.0, "revisions": [{"text": "First", "replaced_at": 2000}, {"text": "", "replaced_at": 2500}, {"text": "Second", "replaced_at": 3000}],
                "users": [str(self.member_id)], "roles": [self.role_mod["id"], "123456789012345678", str(self.guild_id)]}
        response = await client.post(self.path("/collaborative/import"), json=body)
        self.assertEqual(response.status, 200, await response.text())
        answer = await response.json()
        self.assertEqual(answer["versions"], 2)
        self.assertEqual([row["revision"] for row in answer["skipped"]], [2])
        self.assertEqual(sorted(map(str, answer["roles_left_out"])), sorted(["123456789012345678", str(self.guild_id)]))
        row = await self.post_row(new)
        versions = await self.embeds.versions(self.guild_id, row["posted_id"])
        self.assertEqual([v["payload"]["content"] for v in versions], ["First", "Second", "Third"])
        self.assertEqual([v["saved_at"] for v in versions[:2]], [1000.0, 2500.0])
        self.assertEqual(versions[0]["note"], collab.EARLIER_NOTE)
        self.assertEqual(await self.editors(new), ([self.member_id], [int(self.role_mod["id"])]))
        response = await client.post(self.path("/collaborative/import"), json=body)
        self.assertEqual(response.status, 409)

    async def test_an_embed_post_brings_its_revisions_as_descriptions_with_its_color(self):
        old, new = await self.adopt(content="", embeds=[{"type": "rich", "description": "Now", "color": 0x123456}])
        client = await self.client()
        response = await client.post(self.path("/collaborative/import"), json={"source_message_id": old["id"], "created_at": 5.0, "revisions": [{"text": "Before", "replaced_at": 9}], "users": [], "roles": []})
        self.assertEqual(response.status, 200, await response.text())
        row = await self.post_row(new)
        first = (await self.embeds.versions(self.guild_id, row["posted_id"]))[0]["payload"]
        self.assertEqual((first["content"], first["embeds"][0]["description"], first["embeds"][0]["color"]), ("", "Before", 0x123456))

    async def test_only_an_administrator_brings_one_over(self):
        """Making a collaborative message takes an Administrator or a creator role, and this route also writes the post's whole history, so it's for the
        owner and Administrators, as the importer signs in."""
        old, new = await self.adopt(content="Server rules")
        client = await self.client()
        data = {**self.member_json[self.admin_id], "roles": [self.role_mod["id"]]}  # the person signed in keeps the page and loses Administrator
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **data})
        response = await client.post(self.path("/collaborative/import"), json={"source_message_id": old["id"], "revisions": [{"text": "Rule 1: anything goes", "replaced_at": 0}]})
        self.assertEqual(response.status, 403)
        self.assertIsNone(await self.post_row(new))
        self.assertEqual((await client.get(self.path(f"/collaborative/import?ids={old['id']}"))).status, 403)

    async def test_a_failure_halfway_leaves_nothing_for_the_next_run_to_repeat(self):
        """The earlier texts and the collaborators go in together, so a run stopped between them leaves no history for the next run to add again."""
        old, new = await self.adopt(content="First post")
        client = await self.client()
        real = self.cog.make_collaborative
        failed: list[bool] = []

        async def stops_once(*args: Any, **kwargs: Any) -> None:
            if not failed:
                failed.append(True)
                raise RuntimeError("stopped halfway for a test")
            await real(*args, **kwargs)
        self.cog.make_collaborative = stops_once
        body = {"source_message_id": old["id"], "created_at": 5.0, "revisions": [{"text": "Earlier A", "replaced_at": 6}, {"text": "Earlier B", "replaced_at": 7}], "users": [], "roles": []}
        self.assertEqual((await client.post(self.path("/collaborative/import"), json=body)).status, 500)
        self.errors.lines.clear()  # that failure was logged on purpose
        self.assertEqual((await client.post(self.path("/collaborative/import"), json=body)).status, 200)
        row = await self.post_row(new)
        versions = await self.embeds.versions(self.guild_id, row["posted_id"])
        self.assertEqual([v["payload"]["content"] for v in versions], ["Earlier A", "Earlier B", "First post"])

    async def test_a_message_not_reposted_yet_is_refused(self):
        client = await self.client()
        response = await client.post(self.path("/collaborative/import"), json={"source_message_id": "123456789012345678", "revisions": []})
        self.assertEqual(response.status, 404)


class AutomodTest(Case):
    """Automod never scans the bot's own posts, so a member's text meets the server's word rule before the bot posts it for them."""
    modules = [Automod, Embeds, Collaborative]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["scamword"], "punishment": {"actions": 1}}})
        self.addAsyncCleanup(self.bot.module("automod").regex.close)

    async def test_a_new_post_and_an_edit_meet_the_word_rule(self):
        await self.bot.settings.save(self.guild_id, "collaborative", {"creator_roles": [int(self.role_member["id"])]})
        posts = len(self.bot_posts(self.text_id))
        await self.command("create_collaborative_message", self.member_id, content="buy scamword now")
        self.assertIn("This server's automod doesn't allow some of that text.", self.last_answer())
        self.assertEqual(len(self.bot_posts(self.text_id)), posts)
        message = await self.create("Event schedule", user1=self.member(self.member_id))
        await self.message_menu("Edit Collaborative Message", self.member_id, message)
        await self.submit_form(self.member_id, "buy scamword now")
        self.assertIn("This server's automod doesn't allow some of that text.", self.last_answer())
        self.assertEqual(self.stored(message)["content"], "Event schedule")

