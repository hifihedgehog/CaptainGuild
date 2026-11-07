"""The message mover through the real library on a faked network: moving and copying under the author's name, the destination picker, editing and
deleting a copy through its stored webhook, the log channel, pruning, the dashboard, and the webhook token staying out of every page, log and file.
The behavior IDs (MV-1 and so on) are the old bot's, as the build spec numbers them."""
from __future__ import annotations

import json
import time
from typing import Any
from unittest import mock

import discord

from captainguild.core.ops import OpsError
from captainguild.modules.automod import Automod
from captainguild.modules.backup import Backup
from captainguild.modules.mover import DESTINATIONS, PRUNE_REFUSED, Mover
from captainguild.modules.mydata import KEPT, SECTIONS, MyData
from tests.gateway import FakeHTTPResponse, iso, new_id
from tests.voice_report_harness import Harness

HOOK_POST = "/webhooks/{webhook_id}/{webhook_token}"
HOOK_MESSAGE = "/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}"


class Case(Harness):
    modules = [Mover]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cog: Mover = self.bot.module("mover")
        self.bot.http._HTTPClient__session = object()  # set at login, and the network here is replaced below the session
        self.text_id = int(self.text["id"])
        self.log_id = int(self.log_channel["id"])
        usual = discord.Permissions(view_channel=True, send_messages=True, send_messages_in_threads=True, read_message_history=True, use_application_commands=True)
        self.role_everyone["permissions"] = str(usual.value)
        self.state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": self.role_everyone})
        await self.switch(True)

    async def switch(self, on: bool) -> None:
        await self.bot.settings.save(self.guild_id, "mover", {"enabled": on})
        await self.bot.sync_server_commands(self.guild_id)

    async def message_menu(self, name: str, user_id: int, message: dict) -> None:
        data = {"id": str(new_id()), "name": name, "type": 3, "target_id": message["id"], "resolved": {"messages": {message["id"]: message}}, "guild_id": str(self.guild_id)}
        self.state.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id, channel=self.net.channels.get(int(message["channel_id"]))))
        await self.settle()

    async def command(self, name: str, user_id: int, **options: Any) -> None:
        resolved: dict = {}
        data = []
        for key, value in options.items():
            resolved.setdefault("channels", {})[value["id"]] = {**value, "permissions": str(discord.Permissions.all().value)}
            data.append({"name": key, "type": 7, "value": value["id"]})
        payload = {"id": str(new_id()), "name": name, "type": 1, "options": data, "resolved": resolved, "guild_id": str(self.guild_id)}
        self.state.parse_interaction_create(self.interaction_payload(2, payload, user_id=user_id))
        await self.settle()

    def member_post(self, user_id: int | None = None, content: str = "My words", channel: dict | None = None, **extra: Any) -> dict:
        payload = self.message_payload(user_id=user_id or self.member_id, content=content, channel=channel)
        payload.update(extra)
        self.net.messages[int(payload["id"])] = payload
        return payload

    def picker(self) -> tuple[dict, dict]:
        reply = next(message for message in reversed(self.net.posted) if message.get("components"))
        return reply, reply["components"][0]["components"][0]

    async def pick(self, user_id: int, destination: dict) -> None:
        reply, select = self.picker()
        data = {"custom_id": select["custom_id"], "component_type": 8, "values": [destination["id"]],
                "resolved": {"channels": {destination["id"]: {**destination, "permissions": str(discord.Permissions.all().value)}}}}
        self.state.parse_interaction_create(self.interaction_payload(3, data, user_id=user_id, message=reply))
        await self.settle()

    async def move(self, user_id: int, message: dict, destination: dict, *, copy: bool = False) -> None:
        await self.message_menu("Copy Message" if copy else "Move Message", user_id, message)
        await self.pick(user_id, destination)
        self.assert_no_errors()

    def hook_bodies(self) -> list[dict]:
        """What went out through the bot's channel webhooks. An answer to an interaction goes to the same route without a name."""
        return [body for method, path, body in self.net.calls if method == "POST" and path == HOOK_POST and "username" in body]

    def copies(self) -> list[dict]:
        hooks = {int(hook["id"]) for made in self.net.webhooks.values() for hook in made}
        return [message for message in self.net.posted if message.get("webhook_id") and int(message["webhook_id"]) in hooks]

    async def record(self, message_id: int | str) -> dict | None:
        return await self.cog.record_of(self.guild_id, int(message_id))

    def thread(self) -> dict:
        thread_id = new_id()
        data = {"id": str(thread_id), "guild_id": str(self.guild_id), "parent_id": self.text["id"], "owner_id": str(self.owner_id), "name": "help", "type": 11,
                "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "flags": 0,
                "thread_metadata": {"archived": False, "auto_archive_duration": 1440, "archive_timestamp": iso(), "locked": False}}
        self.net.channels[thread_id] = data
        self.state.parse_thread_create({**data, "newly_created": True})
        return data


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

class MoveTest(Case):
    async def test_a_member_moves_their_own_message_under_their_name(self):
        """MV-1, MV-6: posted through the bot's webhook in the destination with the author's name and picture, pinging nobody, and the original goes."""
        mine = self.member_post(content="Wrong channel, sorry <@&1> @everyone")
        await self.move(self.member_id, mine, self.log_channel)
        self.assertIn("Message moved successfully.", self.last_answer())
        made = [call for call in self.net.calls if call[0] == "POST" and call[1] == "/channels/{channel_id}/webhooks"]
        self.assertEqual(len(made), 1)
        self.assertIn(self.log_channel["id"], str(self.net.webhooks.keys()))
        body = self.hook_bodies()[-1]
        self.assertEqual((body["username"], body["content"]), ("member", "Wrong channel, sorry <@&1> @everyone"))
        self.assertTrue(body["avatar_url"].startswith("https://cdn.discordapp.com/"))
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertNotIn(int(mine["id"]), self.net.messages)
        copy = self.copies()[-1]
        row = await self.record(copy["id"])
        self.assertEqual((row["user_id"], row["author_id"], row["channel_id"], row["thread_id"], row["copied"]), (self.member_id, self.member_id, self.log_id, None, 0))
        self.assertEqual(row["source_message_id"], int(mine["id"]))
        self.assertTrue(row["webhook_token"])

    async def test_only_your_own_message_unless_you_could_delete_it_by_hand(self):
        """MV-1: the old bot let a role named Moderator through. Here it's Manage Messages in that channel, or the bot manager role."""
        theirs = self.member_post(user_id=self.mod_id, content="Theirs")
        await self.message_menu("Move Message", self.member_id, theirs)
        self.assertIn("You can only move your own messages.", self.last_answer())
        await self.message_menu("Copy Message", self.member_id, theirs)
        self.assertIn("You can only copy your own messages.", self.last_answer())
        mine = self.member_post(content="Mine")
        await self.move(self.mod_id, mine, self.log_channel)
        self.assertEqual(self.hook_bodies()[-1]["username"], "member")
        await self.bot.settings.save(self.guild_id, "commands", {"mod_role": int(self.role_member["id"])})
        another = self.member_post(user_id=self.mod_id, content="Another")
        await self.move(self.member_id, another, self.log_channel, copy=True)
        self.assertEqual(self.hook_bodies()[-1]["content"], "Another")

    async def test_your_own_message_moves_from_where_you_cant_read_back(self):
        """DECISIONS, reading: a copy mustn't carry what someone couldn't read. Their own words they wrote, so a channel that hides its history still lets them go."""
        blind = {**self.text, "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(read_message_history=True).value)}]}
        self.state.parse_channel_update(blind)
        mine = self.member_post(content="Mine to move")
        await self.move(self.member_id, mine, self.log_channel)
        self.assertIn("Message moved successfully.", self.last_answer())
        stranger = self.add_member("stranger", self.role_member["id"])
        await self.bot.settings.save(self.guild_id, "commands", {"mod_role": int(self.role_member["id"])})
        theirs = self.member_post(content="Not theirs to read")
        await self.message_menu("Copy Message", stranger, theirs)
        self.assertIn("You can't read the messages in that channel.", self.last_answer())

    async def test_the_bots_own_post_stays_with_the_bot(self):
        """A collaborative message or any other post of the bot's own lives in the Embeds library under its message. A webhook copy would leave that post
        deleted and the copy editable by no one, so the mover leaves the bot's posts alone, even for a moderator."""
        post = self.net.message_json(self.text_id, {"content": "A post the bot made"})
        self.net.messages[int(post["id"])] = post
        for menu in ("Move Message", "Copy Message"):
            await self.message_menu(menu, self.mod_id, post)
            self.assertIn("one of the bot's own posts", self.last_answer(), menu)
        self.assertIn(int(post["id"]), self.net.messages)
        self.assertEqual(self.hook_bodies(), [])

    async def test_the_message_is_read_again_when_the_channel_is_picked(self):
        """MV-6: the picker stays open up to a minute, and the message can be deleted or edited meanwhile. The copy carries what Discord holds when the channel is picked."""
        gone = self.member_post(content="Text a moderator removes")
        await self.message_menu("Move Message", self.member_id, gone)
        self.net.messages.pop(int(gone["id"]))
        await self.pick(self.member_id, self.log_channel)
        self.assertIn("That message is gone.", self.last_answer())
        self.assertEqual(self.hook_bodies(), [])
        fixed = self.member_post(content="First wording")
        await self.message_menu("Move Message", self.member_id, fixed)
        self.net.messages[int(fixed["id"])]["content"] = "Corrected wording"
        await self.pick(self.member_id, self.log_channel)
        self.assert_no_errors()
        self.assertEqual(self.hook_bodies()[-1]["content"], "Corrected wording")

    async def test_copy_keeps_the_original(self):
        """MV-2."""
        mine = self.member_post(content="Copy this")
        await self.move(self.member_id, mine, self.log_channel, copy=True)
        self.assertIn("Message copied successfully.", self.last_answer())
        self.assertIn(int(mine["id"]), self.net.messages)
        self.assertEqual((await self.record(self.copies()[-1]["id"]))["copied"], 1)

    async def test_the_picker_offers_text_channels_and_threads_and_a_thread_goes_through_its_parent(self):
        """MV-3, MV-7."""
        mine = self.member_post(content="To a thread")
        await self.message_menu("Move Message", self.member_id, mine)
        reply, select = self.picker()
        self.assertEqual(reply["content"], "Select the channel or thread to move this message to:")
        self.assertEqual(reply["flags"] & 64, 64)
        self.assertEqual((select["type"], select["placeholder"], select["min_values"], select["max_values"]), (8, "Select a destination channel or thread", 1, 1))
        self.assertEqual(sorted(select["channel_types"]), sorted(kind.value for kind in DESTINATIONS))
        thread = self.thread()
        await self.pick(self.member_id, thread)
        self.assert_no_errors()
        made = [path for method, path, _ in self.net.calls if method == "POST" and path == "/channels/{channel_id}/webhooks"]
        self.assertEqual(len(made), 1)
        self.assertIn(self.text_id, self.net.webhooks)
        posted = [params for method, path, params in self.net.webhook_params if method == "POST" and path == HOOK_POST and params.get("thread_id")]
        self.assertEqual(posted[-1]["thread_id"], int(thread["id"]))
        row = await self.record(self.copies()[-1]["id"])
        self.assertEqual((row["channel_id"], row["thread_id"]), (self.text_id, int(thread["id"])))

    async def test_one_webhook_per_channel_serves_every_move(self):
        """MV-7."""
        for words in ("one", "two"):
            await self.move(self.member_id, self.member_post(content=words), self.log_channel)
        made = [path for method, path, _ in self.net.calls if method == "POST" and path == "/channels/{channel_id}/webhooks"]
        self.assertEqual(len(made), 1)

    async def test_files_and_rich_embeds_come_along_and_a_link_preview_does_not(self):
        """MV-6: the old bot also sent a link's preview, which Discord makes again from the link, so the copy showed it twice."""
        attachment = {"id": str(new_id()), "filename": "photo.png", "size": 10, "url": "https://cdn.discordapp.com/attachments/1/2/photo.png", "proxy_url": "https://x/photo.png", "content_type": "image/png"}
        embeds = [{"type": "rich", "title": "Rich one"}, {"type": "link", "url": "https://example.com", "title": "Preview"}]
        mine = self.member_post(content="See https://example.com", attachments=[attachment], embeds=embeds)
        uploaded: list[list[str]] = []
        real = self.net.webhook

        async def spy(adapter: Any, route: Any, session: Any = None, **kwargs: Any) -> Any:
            if kwargs.get("files"):
                uploaded.append([file.filename for file in kwargs["files"]])
            return await real(adapter, route, session, **kwargs)
        self.net.webhook = spy
        await self.move(self.member_id, mine, self.log_channel)
        body = self.hook_bodies()[-1]
        self.assertEqual([embed.get("title") for embed in body["embeds"]], ["Rich one"])
        self.assertEqual(uploaded, [["photo.png"]])

    async def test_a_spoilered_file_stays_spoilered(self):
        """discord.py's Attachment.to_file takes spoiler=False unless told, and an explicit False uploads the file under its bare name, unblurred."""
        spoiler = {"id": str(new_id()), "filename": "SPOILER_ending.png", "size": 10, "url": "https://cdn.discordapp.com/attachments/1/2/SPOILER_ending.png",
                   "proxy_url": "https://media.discordapp.net/attachments/1/2/SPOILER_ending.png", "content_type": "image/png"}
        uploaded: list[list[str]] = []
        real = self.net.webhook

        async def spy(adapter: Any, route: Any, session: Any = None, **kwargs: Any) -> Any:
            if kwargs.get("files"):
                uploaded.append([file.filename for file in kwargs["files"]])
            return await real(adapter, route, session, **kwargs)
        self.net.webhook = spy
        await self.move(self.member_id, self.member_post(content="Moving my spoiler", attachments=[spoiler]), self.log_channel)
        await self.move(self.member_id, self.member_post(content="Copying my spoiler", attachments=[dict(spoiler, id=str(new_id()))]), self.log_channel, copy=True)
        self.assertEqual(uploaded, [["SPOILER_ending.png"], ["SPOILER_ending.png"]])

    async def test_a_file_under_discords_default_limit_moves_below_boost_level_two(self):
        """MV-6: Discord raised the default upload limit from 10 MiB to 20 MiB for bots and webhooks on September 3, 2026 (discord-api-docs change log),
        and discord.py 2.7.1's table still gives 10 MiB below boost level 2."""
        self.assertEqual(self.guild.filesize_limit, 10 * 1024 * 1024)
        clip = {"id": str(new_id()), "filename": "print-timelapse.mp4", "size": 15 * 1024 * 1024, "url": "https://cdn.discordapp.com/attachments/1/2/print-timelapse.mp4",
                "proxy_url": "https://media.discordapp.net/attachments/1/2/print-timelapse.mp4", "content_type": "video/mp4"}
        await self.move(self.member_id, self.member_post(content="Timelapse", attachments=[clip]), self.log_channel)
        self.assertIn("Message moved successfully.", self.last_answer())
        huge = dict(clip, id=str(new_id()), filename="huge.zip", size=21 * 1024 * 1024)
        await self.move(self.member_id, self.member_post(content="Too big", attachments=[huge]), self.log_channel)
        self.assertIn("huge.zip, too big for the bot to upload here", self.last_answer())

    async def test_a_move_that_would_lose_part_of_the_message_stays_put_and_a_copy_says_what_is_missing(self):
        sticker = [{"id": str(new_id()), "name": "wave", "format_type": 1}]
        mine = self.member_post(content="With a sticker", sticker_items=sticker)
        await self.move(self.member_id, mine, self.log_channel)
        self.assertIn("Moving it would lose its sticker, so it stays where it is. Copy it instead.", self.last_answer())
        self.assertIn(int(mine["id"]), self.net.messages)
        self.assertEqual(self.hook_bodies(), [])
        await self.move(self.member_id, mine, self.log_channel, copy=True)
        self.assertIn("The copy is missing its sticker.", self.last_answer())
        big = {"id": str(new_id()), "filename": "huge.zip", "size": 999 * 1024 * 1024, "url": "https://cdn.discordapp.com/attachments/1/2/huge.zip", "proxy_url": "https://x/huge.zip"}
        heavy = self.member_post(content="With a big file", attachments=[big])
        await self.move(self.member_id, heavy, self.log_channel)
        self.assertIn("huge.zip, too big for the bot to upload here", self.last_answer())
        self.assertIn(int(heavy["id"]), self.net.messages)

    async def test_an_original_that_cant_be_deleted_leaves_a_working_copy(self):
        """MV-6: the old bot reported the move as failed after the copy was already posted."""
        mine = self.member_post(content="Stuck")
        restore = self.failing("DELETE", "/channels/{channel_id}/messages/{message_id}", 403)
        await self.move(self.member_id, mine, self.log_channel)
        restore()
        answer = self.last_answer()
        self.assertIn("Message moved successfully.", answer)
        self.assertIn("The original couldn't be deleted: The bot needs Manage Messages in that channel.", answer)
        self.assertIsNotNone(await self.record(self.copies()[-1]["id"]))

    async def test_a_destination_the_member_cant_post_in_is_refused(self):
        quiet = {**self.log_channel, "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(send_messages=True).value)}]}
        self.state.parse_channel_update(quiet)
        mine = self.member_post(content="Nope")
        await self.move(self.member_id, mine, quiet)
        self.assertIn("You can't post in that channel.", self.last_answer())
        self.assertEqual(self.hook_bodies(), [])

    async def test_a_copy_can_be_moved_again_by_whoever_made_it(self):
        mine = self.member_post(content="Twice")
        await self.move(self.member_id, mine, self.log_channel)
        first = self.copies()[-1]
        record = await self.record(first["id"])
        await self.move(self.member_id, first, self.text)
        self.assertIn("Message moved successfully.", self.last_answer())
        deleted = [path for method, path, _ in self.net.calls if method == "DELETE" and path == HOOK_MESSAGE]
        self.assertEqual(len(deleted), 1)
        self.assertIsNone(await self.record(first["id"]))
        newest = await self.record(self.copies()[-1]["id"])
        self.assertEqual((newest["author_id"], newest["user_id"]), (record["author_id"], self.member_id))


class EditDeleteTest(Case):
    async def moved(self) -> dict:
        await self.move(self.member_id, self.member_post(content="First draft"), self.log_channel)
        return self.copies()[-1]

    async def test_whoever_moved_it_edits_the_copy_through_its_webhook(self):
        """MV-4."""
        copy = await self.moved()
        await self.message_menu("Edit Moved Message", self.member_id, copy)
        modal = self.net.modals[-1]
        self.assertEqual(modal["title"], "Edit Moved Message")
        field = modal["components"][0].get("component") or modal["components"][0]["components"][0]
        self.assertEqual((field["label"], field["value"], field["required"]), ("New Message Content", "First draft", False))
        await self.submit_form(self.member_id, "Second draft")
        self.assert_no_errors()
        self.assertIn("Message edited successfully.", self.last_answer())
        edit = [body for method, path, body in self.net.calls if method == "PATCH" and path == HOOK_MESSAGE][-1]
        self.assertEqual((edit["content"], edit["allowed_mentions"]), ("Second draft", {"parse": []}))
        self.assertNotIn("embeds", edit)  # a preview isn't sent back as an embed

    async def test_only_the_mover_or_someone_who_could_delete_it_edits_or_deletes(self):
        copy = await self.moved()
        stranger = self.add_member("stranger", self.role_member["id"])
        await self.message_menu("Edit Moved Message", stranger, copy)
        self.assertIn("You can only edit your own moved/copied messages.", self.last_answer())
        await self.message_menu("Delete Moved Message", stranger, copy)
        self.assertIn("You can only delete your own moved/copied messages.", self.last_answer())
        await self.message_menu("Edit Moved Message", self.mod_id, copy)
        self.assertEqual(self.net.modals[-1]["title"], "Edit Moved Message")

    async def test_a_message_it_didnt_move_says_so(self):
        plain = self.member_post(content="Plain")
        await self.message_menu("Edit Moved Message", self.member_id, plain)
        self.assertIn("This message cannot be edited using this command.", self.last_answer())
        await self.message_menu("Delete Moved Message", self.member_id, plain)
        self.assertIn("This message cannot be deleted using this command.", self.last_answer())

    async def test_an_empty_edit_with_nothing_else_to_show_is_refused(self):
        copy = await self.moved()
        await self.message_menu("Edit Moved Message", self.member_id, copy)
        await self.submit_form(self.member_id, "")
        self.assertIn("A message can't be empty.", self.last_answer())
        self.assertEqual([call for call in self.net.calls if call[0] == "PATCH" and call[1] == HOOK_MESSAGE], [])

    async def test_delete_goes_through_the_webhook_and_forgets_the_copy(self):
        """MV-5."""
        copy = await self.moved()
        await self.message_menu("Delete Moved Message", self.member_id, copy)
        self.assert_no_errors()
        self.assertIn("Message deleted successfully.", self.last_answer())
        self.assertTrue([call for call in self.net.calls if call[0] == "DELETE" and call[1] == HOOK_MESSAGE])
        self.assertIsNone(await self.record(copy["id"]))

    async def test_a_refused_delete_keeps_the_record(self):
        copy = await self.moved()

        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise OpsError("Discord refused to delete it: 500")
        with mock.patch.object(self.bot.webhooks, "delete", refuse):
            await self.message_menu("Delete Moved Message", self.member_id, copy)
        self.assertIn("Failed to delete message: Discord refused to delete it: 500", self.last_answer())
        self.assertIsNotNone(await self.record(copy["id"]))

    async def test_a_copy_deleted_in_discord_is_forgotten(self):
        copy = await self.moved()
        self.state.parse_message_delete({"id": copy["id"], "channel_id": self.log_channel["id"], "guild_id": str(self.guild_id)})
        await self.settle()
        self.assertIsNone(await self.record(copy["id"]))


class CommandsTest(Case):
    async def test_the_log_channel_is_for_administrators_and_logs_without_pinging(self):
        """MV-8, MV-10: the old bot's admin check did nothing on a slash command, and its saves never ran."""
        await self.command("mover_log_channel", self.mod_id, channel=self.log_channel)
        self.assertIn("You need Administrator", self.last_answer())
        await self.command("mover_log_channel", self.admin_id, channel=self.log_channel)
        self.assertIn(f"Log channel set to <#{self.log_id}>", self.last_answer())
        self.assertEqual(self.bot.settings.get(self.guild_id, "mover")["log_channel"], self.log_id)
        await self.move(self.member_id, self.member_post(content="Logged"), self.log_channel)
        logs = [call[2] for call in self.net.calls if call[0] == "POST" and call[1] == "/channels/{channel_id}/messages" and "used by" in (call[2].get("content") or "")]
        self.assertTrue(logs[-1]["content"].startswith(f"Command `Move Message` used by <@{self.member_id}> in <#{self.text_id}>\nDetails: "))
        self.assertEqual(logs[-1]["allowed_mentions"]["parse"], [])

    async def test_prune_forgets_copies_that_are_gone_and_keeps_ones_it_cant_read(self):
        """MV-9."""
        await self.move(self.member_id, self.member_post(content="Gone"), self.log_channel)
        gone = self.copies()[-1]
        await self.move(self.member_id, self.member_post(content="Hidden"), self.log_channel)
        hidden = self.copies()[-1]
        self.net.messages.pop(int(gone["id"]))
        real = self.bot.http.request

        async def request(route: Any, **kwargs: Any) -> Any:
            if route.method == "GET" and str(hidden["id"]) in route.url:
                raise discord.Forbidden(FakeHTTPResponse(403), {"code": 50001, "message": "Missing Access"})
            return await real(route, **kwargs)
        self.bot.http.request = request
        await self.command("prune_messages", self.mod_id)
        self.assertIn("You need Administrator", self.last_answer())
        await self.command("prune_messages", self.admin_id)
        self.assert_no_errors()
        self.assertIn("Pruned 1 out of 2 messages.", self.last_answer())
        self.assertIsNone(await self.record(gone["id"]))
        self.assertIsNotNone(await self.record(hidden["id"]))

    async def test_prune_keeps_a_copy_whose_thread_discord_wont_look_up_and_forgets_one_that_is_gone(self):
        """MV-9: a refusal or an outage says nothing about whether the copy is there, and its record holds the only stored webhook token."""
        for status, kept in ((403, True), (503, True), (404, False)):
            thread = self.thread()
            await self.move(self.member_id, self.member_post(content=f"Into the thread {status}"), thread)
            copy = self.copies()[-1]
            self.archive(thread)
            restore = self.refuse_lookup(int(thread["id"]), status)
            await self.command("prune_messages", self.admin_id)
            restore()
            self.assert_no_errors()
            self.assertEqual(await self.record(copy["id"]) is not None, kept, status)
            self.assertIn(f"Pruned {0 if kept else 1} out of", self.last_answer(), status)

    async def test_the_bot_manager_role_prunes_on_neither_the_command_nor_the_page(self):
        """MV-9, DECISIONS, commands match the dashboard: the page's Prune button is the owner's and Administrators', as the old bot's check was, and the
        bot manager role passes an Administrator command, so the command checks for itself and refuses with the page's sentence."""
        await self.move(self.member_id, self.member_post(content="Gone"), self.log_channel)
        gone = self.copies()[-1]
        self.net.messages.pop(int(gone["id"]))
        await self.bot.settings.save(self.guild_id, "commands", {"mod_role": int(self.role_mod["id"])})
        await self.command("prune_messages", self.mod_id)
        self.assertIn(PRUNE_REFUSED, self.last_answer())
        self.assertIsNotNone(await self.record(gone["id"]))
        await self.command("prune_messages", self.admin_id)
        self.assertIn("Pruned 1 out of 1 messages.", self.last_answer())

    async def test_the_pack_holds_the_old_menus_under_new_names(self):
        """MV-11: they reach a server when it switches the pack on."""
        names = {command["name"] for command in self.net.guild_commands[self.guild_id]}
        self.assertEqual(names, {"Move Message", "Copy Message", "Edit Moved Message", "Delete Moved Message", "mover_log_channel", "prune_messages"})

    async def test_with_the_pack_off_the_menus_say_so(self):
        mine = self.member_post()
        await self.bot.settings.save(self.guild_id, "mover", {"enabled": False})
        await self.message_menu("Move Message", self.member_id, mine)
        self.assertIn("switched off in this server", self.last_answer())


class TokenTest(Case):
    """The stored webhook token can edit or delete what it posted, so it shows nowhere a person can read."""
    modules = [Mover, Backup, MyData]

    async def test_the_page_lists_copies_without_the_token(self):
        await self.move(self.member_id, self.member_post(content="Listed"), self.log_channel)
        client = await self.client()
        response = await client.get(self.path("/mover/messages"))
        text = await response.text()
        self.assertEqual(response.status, 200, text)
        self.assertNotIn("webhook-token", text)
        self.assertNotIn("webhook_token", text)
        rows = json.loads(text)["rows"]
        self.assertEqual((len(rows), str(rows[0]["user_id"])), (1, str(self.member_id)))

    async def test_a_backup_seals_the_token_and_brings_it_back(self):
        await self.move(self.member_id, self.member_post(content="Kept"), self.log_channel)
        backup = self.bot.module("backup")
        body = bytearray()
        async for block in backup.export_stream(self.guild_id, include_messages=False):
            body += block
        self.assertNotIn(b"webhook-token", bytes(body))
        self.assertIn(b"$sealed", bytes(body))
        await self.db.run("DELETE FROM moved_messages")
        client = await self.client()
        response = await client.post(self.path("/backup/upload"), data=bytes(body))
        self.assertEqual(response.status, 200, await response.text())
        await backup.wait_for_job(self.guild_id)
        job = backup._jobs[self.guild_id]
        response = await client.post(self.path("/backup/restore"), json={"token": job.result["token"], "items": [row["key"] for row in job.result["tables"]]})
        self.assertEqual(response.status, 200, await response.text())
        await backup.wait_for_job(self.guild_id)
        self.assertEqual(await self.db.scalar("SELECT webhook_token FROM moved_messages"), "webhook-token")

    def test_a_persons_data_download_holds_no_token(self):
        """A person's download reads only the tables in SECTIONS, and the copies are kept apart with the reason."""
        self.assertNotIn("moved_messages", {table for section in SECTIONS for table, _ in section.tables})
        self.assertIn("moved_messages", KEPT)

    async def test_the_prune_button_is_for_administrators(self):
        client = await self.client()
        data = {**self.member_json[self.admin_id], "roles": [self.role_mod["id"]]}
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **data})
        response = await client.post(self.path("/mover/prune"), json={})
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())["error"], PRUNE_REFUSED)

    def test_nothing_logs_the_token(self):
        source = open(Mover.__module__.replace(".", "/") + ".py", encoding="utf-8").read()
        for line in source.splitlines():
            if "log." in line:
                self.assertNotIn("token", line, line)


class AutomodTest(Case):
    """Automod never scans a webhook's posts, so what a member has the bot post for them meets the server's word rule first, as a tag's text does.
    A channel's exemption doesn't count, since a copy carries the text somewhere the rule covers."""
    modules = [Automod, Mover]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["scamword"], "punishment": {"actions": 1}}})
        self.addAsyncCleanup(self.bot.module("automod").regex.close)

    async def test_an_edit_to_a_copy_meets_the_word_rule(self):
        await self.move(self.member_id, self.member_post(content="hello there"), self.log_channel)
        copy = self.copies()[-1]
        await self.message_menu("Edit Moved Message", self.member_id, copy)
        await self.submit_form(self.member_id, "buy scamword now")
        self.assertIn("This server's automod doesn't allow some of that text.", self.last_answer())
        self.assertEqual([call for call in self.net.calls if call[0] == "PATCH" and call[1] == HOOK_MESSAGE], [])

    async def test_a_copy_out_of_an_exempt_channel_meets_the_word_rule_and_staff_stay_immune(self):
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"exempt_channels": [self.log_id]}})
        said = self.member_post(content="buy scamword now", channel=self.log_channel)
        await self.move(self.member_id, said, self.text, copy=True)
        self.assertIn("This server's automod doesn't allow some of that text.", self.last_answer())
        self.assertEqual(self.hook_bodies(), [])
        await self.move(self.mod_id, said, self.text, copy=True)
        self.assertIn("Message copied successfully.", self.last_answer())

