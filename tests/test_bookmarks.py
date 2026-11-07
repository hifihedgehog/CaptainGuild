"""Bookmarks: the right-click menu sends a DM copy, and the button or a reaction deletes it, through the real library with only the network replaced."""
from __future__ import annotations

import discord

from captainguild.modules.bookmarks import DELETE_ID, MENU_NAME, Bookmarks
from tests.gateway import FakeHTTPResponse, GatewayTest, new_id, user_json

TRASH = "\N{WASTEBASKET}\N{VARIATION SELECTOR-16}"


class Base(GatewayTest):
    modules = [Bookmarks]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.pack = self.bot.module("bookmarks")

    async def switch(self, on: bool, *, register: bool = True) -> None:
        """The bot isn't connected here, so the registration a save sets off runs by hand. Without it, the server keeps the menus it had."""
        await self.bot.settings.save(self.guild_id, "bookmarks", {"enabled": on})
        if register:
            await self.bot.sync_server_commands(self.guild_id)

    def target(self, **body) -> dict:
        message = self.net.message_json(int(self.text["id"]), {"content": body.pop("content", "Print at 0.2 mm first.")})
        message["author"] = self.member_json[self.member_id]["user"]
        message.update(body)
        return message

    def run_menu(self, message: dict) -> None:
        data = {"id": str(new_id()), "name": MENU_NAME, "type": 3, "target_id": message["id"], "resolved": {"messages": {message["id"]: message}}, "guild_id": str(self.guild_id)}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.member_id))

    def dm_posts(self) -> list[dict]:
        dms = {int(call[2]["recipient_id"]) for call in self.net.calls if call[1] == "/users/@me/channels"}
        return [body for method, path, body in self.net.calls if method == "POST" and path == "/channels/{channel_id}/messages" and dms]

    def answers(self) -> list[dict]:
        return [body for method, path, body in self.net.calls if path in ("/webhooks/{webhook_id}/{webhook_token}", "/interactions/{webhook_id}/{webhook_token}/callback")]

    def refuse_dms(self, status: int) -> None:
        original = self.net.http

        async def refuse(route, **kwargs):
            if route.path == "/channels/{channel_id}/messages" and route.method == "POST" and int(route.channel_id) != int(self.text["id"]):
                error = discord.Forbidden if status == 403 else discord.HTTPException
                raise error(FakeHTTPResponse(status), {"code": 50007, "message": "Cannot send messages to this user"})
            return await original(route, **kwargs)
        self.bot.http.request = refuse


class MenuTest(Base):
    async def test_the_menu_goes_only_into_servers_with_bookmarks_on(self):
        self.assertIsNone(self.bot.tree.get_command(MENU_NAME, type=discord.AppCommandType.message))
        await self.bot.sync_server_commands(self.guild_id)
        self.assertEqual(self.net.guild_commands[self.guild_id], [])
        await self.switch(True)
        await self.bot.sync_server_commands(self.guild_id)
        self.assertEqual([(c["name"], c["type"]) for c in self.net.guild_commands[self.guild_id]], [(MENU_NAME, 3)])

    async def test_a_bookmark_dm_carries_the_copy_the_information_and_the_button(self):
        await self.switch(True)
        picture = {"id": str(new_id()), "filename": "benchy.png", "size": 10, "url": "https://cdn.example/benchy.png", "proxy_url": "https://cdn.example/benchy.png", "content_type": "image/png"}
        later = {**picture, "id": str(new_id()), "filename": "second.png", "url": "https://cdn.example/second.png"}
        message = self.target(attachments=[picture, later], embeds=[{"title": "a preview"}, {"title": "another"}])
        self.run_menu(message)
        await self.settle()
        self.assert_no_errors()
        [dm] = self.dm_posts()
        copy, info = dm["embeds"]
        self.assertEqual(copy["description"], "Print at 0.2 mm first.")
        self.assertEqual(copy["author"]["name"], "member")
        self.assertEqual(copy["image"]["url"], "https://cdn.example/benchy.png")
        self.assertEqual(copy["color"], discord.Color.blurple().value)
        self.assertEqual(info["title"], "Information")
        self.assertEqual(info["color"], 0x23272A)
        self.assertIn("timestamp", info)
        fields = {field["name"]: field["value"] for field in info["fields"]}
        jump = f"https://discord.com/channels/{self.guild_id}/{self.text['id']}/{message['id']}"
        self.assertEqual(fields["Original Message"], f"[Jump to message]({jump})")
        self.assertEqual(fields["Embeds"], "[2]")
        self.assertEqual(fields["From"], f"[Test Guild](https://discord.com/channels/{self.guild_id}) in <#{self.text['id']}>")
        self.assertEqual(dm["components"][0]["components"][0]["custom_id"], DELETE_ID)
        self.assertEqual(dm["components"][0]["components"][0]["label"], "Delete Bookmark")
        reply = self.answers()[-1]
        self.assertEqual(reply["embeds"][0]["title"], "Bookmarked!")
        self.assertEqual(reply["embeds"][0]["description"], "Check Your DMs")
        self.assertEqual(reply["flags"] & 64, 64)

    async def test_no_text_says_so_and_a_file_that_is_not_a_picture_is_linked(self):
        await self.switch(True)
        document = {"id": str(new_id()), "filename": "notes.3mf", "size": 10, "url": "https://cdn.example/notes.3mf", "proxy_url": "https://cdn.example/notes.3mf", "content_type": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"}
        self.run_menu(self.target(content="", attachments=[document]))
        await self.settle()
        [dm] = self.dm_posts()
        copy = dm["embeds"][0]
        self.assertEqual(copy["description"], "`No message found`")
        self.assertNotIn("image", copy)
        self.assertEqual(copy["fields"], [{"name": "Attachment included that couldn't be bookmarked", "value": "[Attachment](https://cdn.example/notes.3mf)", "inline": True}])

    async def test_text_past_an_embeds_limit_is_cut_to_fit(self):
        await self.switch(True)
        self.run_menu(self.target(content="x" * 5000))
        await self.settle()
        self.assertEqual(len(self.dm_posts()[0]["embeds"][0]["description"]), 4096)

    async def test_a_bracket_in_the_servers_name_cannot_end_the_link(self):
        await self.switch(True)
        self.guild.name = "Prints [and] more"
        self.run_menu(self.target())
        await self.settle()
        fields = {field["name"]: field["value"] for field in self.dm_posts()[0]["embeds"][1]["fields"]}
        self.assertTrue(fields["From"].startswith("[Prints \\[and\\] more](https://discord.com/channels/"))

    async def test_closed_dms_say_so(self):
        await self.switch(True)
        self.refuse_dms(403)
        self.run_menu(self.target())
        await self.settle()
        reply = self.answers()[-1]["embeds"][0]
        self.assertEqual((reply["title"], reply["description"]), ("Unable to bookmark this message", "Are your DMs enabled?"))
        self.assertEqual(reply["color"], discord.Color.red().value)

    async def test_another_refusal_says_something_went_wrong(self):
        await self.switch(True)
        self.refuse_dms(500)
        self.run_menu(self.target())
        await self.settle()
        reply = self.answers()[-1]["embeds"][0]
        self.assertEqual((reply["title"], reply["description"]), ("Unable to bookmark this message", "An unexpected error occurred."))

    async def test_a_menu_used_after_bookmarks_went_off_is_told_so_and_no_dm_goes(self):
        await self.switch(True)
        await self.switch(False, register=False)  # Discord still shows the menu until the new registration lands
        self.run_menu(self.target())
        await self.settle()
        self.assertEqual(self.dm_posts(), [])
        self.assertIn("switched off in this server", self.answers()[-1]["data"]["content"])


class DeleteTest(Base):
    async def bookmark(self) -> dict:
        await self.switch(True)
        self.run_menu(self.target())
        await self.settle()
        dm = next(message for message in self.net.posted if message.get("components") and message["components"][0]["components"][0].get("custom_id") == DELETE_ID)
        dm.pop("guild_id", None)
        return dm

    def press(self, dm: dict) -> None:
        payload = {
            "id": str(discord.utils.time_snowflake(discord.utils.utcnow())), "application_id": str(self.bot_id), "type": 3, "token": "token", "version": 1,
            "channel_id": dm["channel_id"], "channel": {"id": dm["channel_id"], "type": 1}, "user": self.member_json[self.member_id]["user"], "locale": "en-US",
            "app_permissions": str(discord.Permissions.all().value), "entitlements": [], "authorizing_integration_owners": {"1": str(self.member_id)}, "context": 1,
            "attachment_size_limit": 26214400, "data": {"custom_id": DELETE_ID, "component_type": 2}, "message": dm,
        }
        self.bot._connection.parse_interaction_create(payload)

    def react(self, dm: dict, emoji: str, *, user_id: int | None = None, author: int | None = None, guild: bool = False) -> None:
        data = {"user_id": str(user_id or self.member_id), "channel_id": dm["channel_id"], "message_id": dm["id"], "emoji": {"id": None, "name": emoji},
                "message_author_id": str(author or self.bot_id), "burst": False, "type": 0}
        if guild:
            data["guild_id"] = str(self.guild_id)
        self.bot._connection.parse_message_reaction_add(data)

    def deleted(self) -> list[tuple]:
        return [call for call in self.net.calls if call[0] == "DELETE" and call[1] == "/channels/{channel_id}/messages/{message_id}"]

    async def test_the_button_deletes_the_bookmark_and_says_so(self):
        dm = await self.bookmark()
        self.press(dm)
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(len(self.deleted()), 1)
        self.assertNotIn(int(dm["id"]), self.net.messages)
        reply = self.answers()[-1]["data"]
        self.assertEqual(reply["content"], "Bookmark deleted successfully.")
        self.assertEqual(reply["flags"] & 64, 64)

    async def test_the_button_works_after_a_restart_and_with_bookmarks_switched_off(self):
        dm = await self.bookmark()
        await self.switch(False)
        store = self.bot._connection._view_store
        self.assertIn((2, DELETE_ID), store._views[None], "registered for any message, so a restart keeps it working")
        self.press(dm)
        await self.settle()
        self.assertEqual(len(self.deleted()), 1)

    async def test_the_button_says_why_when_discord_refuses(self):
        dm = await self.bookmark()
        original = self.net.http
        for status, said in ((403, "I don't have permission to delete this message."), (500, "Failed to delete the bookmark due to an HTTP error.")):
            async def refuse(route, _status=status, **kwargs):
                if route.method == "DELETE":
                    error = discord.Forbidden if _status == 403 else discord.HTTPException
                    raise error(FakeHTTPResponse(_status), {"code": 0, "message": "no"})
                return await original(route, **kwargs)
            self.bot.http.request = refuse
            self.press(dm)
            await self.settle()
            self.assertEqual(self.answers()[-1]["data"]["content"], said)

    async def test_a_trash_can_or_red_x_on_a_bookmark_deletes_it(self):
        for emoji in (TRASH, "\N{WASTEBASKET}", "\N{CROSS MARK}"):
            dm = await self.bookmark()
            self.react(dm, emoji)
            await self.settle()
            self.assertNotIn(int(dm["id"]), self.net.messages, emoji)
        self.assert_no_errors()

    async def test_other_reactions_and_other_messages_are_left_alone(self):
        dm = await self.bookmark()
        self.react(dm, "\N{THUMBS UP SIGN}")
        self.react(dm, TRASH, user_id=self.bot_id)
        self.react(dm, TRASH, author=self.member_id)
        self.react(dm, TRASH, guild=True)
        await self.settle()
        self.assertEqual(self.deleted(), [])
        other = self.net.message_json(int(dm["channel_id"]), {"content": "You were warned in Test Guild."})
        other.pop("guild_id", None)
        self.net.messages[int(other["id"])] = other
        self.react(other, TRASH)
        await self.settle()
        self.assertEqual(self.deleted(), [], "a DM that isn't a bookmark stays")
        self.assertIn(int(other["id"]), self.net.messages)
