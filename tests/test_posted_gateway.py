"""Embeds, greetings, sticky messages, the starboard and suggestions through the real discord.py library, with real Discord operations and only the network faked."""
from __future__ import annotations

import asyncio
import datetime
import json
import time
from typing import Any

import discord
from aiohttp.test_utils import TestClient, TestServer
from discord import app_commands

from captainguild.core.ops import DiscordOps
from captainguild.modules.embeds import Embeds
from captainguild.modules.greetings import Greetings
from captainguild.modules.starboard import STAR, Starboard
from captainguild.modules.sticky import Sticky
from captainguild.modules.suggestions import DOWN, UP, Suggestions
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.gateway import iso, new_id, user_json
from tests.test_gateway_smoke import Walker


def text(content: str) -> dict:
    return {"content": content, "embeds": [], "buttons": []}


class PostedCase(Walker):
    modules = [Embeds, Greetings, Sticky, Starboard, Suggestions]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.net.keep_history = True
        self.text_id = int(self.text["id"])
        self.log_id = int(self.log_channel["id"])
        self.clients: list[TestClient] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        await super().asyncTearDown()

    def cog_of(self, kind: type) -> Any:
        return next(m for m in self.bot.modules if isinstance(m, kind))

    def path(self, tail: str) -> str:
        return f"/api/g/{self.guild_id}{tail}"

    async def client(self, user_id: int | None = None) -> TestClient:
        person = User(user_id or self.owner_id, "owner", None)
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=person)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)
        return client

    def slash(self, path: tuple[str, ...], *, user_id: int | None = None, channel: dict | None = None, **values: Any) -> None:
        root = next(c for c in self.bot.tree.get_commands() if c.name == path[0]).to_dict(self.bot.tree)
        leaf = root
        for name in path[1:]:
            leaf = next(o for o in leaf["options"] if o["name"] == name)
        resolved: dict = {}
        options = []
        for option in leaf.get("options", []):
            if option["name"] not in values:
                continue
            value = values[option["name"]]
            if option["type"] == 7:
                resolved.setdefault("channels", {})[value["id"]] = {"id": value["id"], "name": value["name"], "type": value["type"], "permissions": str(discord.Permissions.all().value), "parent_id": value.get("parent_id"), "guild_id": str(self.guild_id), "nsfw": False, "position": 0, "rate_limit_per_user": 0, "topic": None, "flags": 0, "last_message_id": None}
                value = value["id"]
            elif option["type"] == 6:
                member = self.member_json[value]
                resolved.setdefault("users", {})[str(value)] = member["user"]
                resolved.setdefault("members", {})[str(value)] = {**{k: v for k, v in member.items() if k != "user"}, "permissions": "0"}
                value = str(value)
            options.append({"name": option["name"], "type": option["type"], "value": value})
        for name in reversed(path[1:]):
            options = [{"name": name, "type": 1, "options": options}]
        data = {"id": str(new_id()), "name": path[0], "type": 1, "options": options, "resolved": resolved}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id or self.owner_id, channel=channel))

    def answer(self) -> dict:
        """The last message the bot posted."""
        return self.net.posted[-1]

    def calls(self, method: str, fragment: str) -> list:
        return [call for call in self.net.calls if call[0] == method and fragment in call[1]]

    def other_bot_post(self, *, channel_id: int | None = None, **extra: Any) -> dict:
        channel_id = channel_id or self.text_id
        message = self.net.message_json(channel_id, {"content": extra.pop("content", "old post"), "embeds": extra.pop("embeds", [])})
        message["author"] = user_json(new_id(), "Old Bot", bot=True)
        message.update(extra)
        self.net.messages[int(message["id"])] = message
        return message

    def member_message(self, *, user_id: int | None = None, content: str = "A very good post", channel: dict | None = None) -> dict:
        payload = self.message_payload(user_id=user_id or self.member_id, content=content, channel=channel)
        self.net.messages[int(payload["id"])] = payload
        return payload


class WalkTest(PostedCase):
    async def test_every_command_runs_clean_with_real_discord_calls(self):
        settings = self.bot.settings
        await settings.save(self.guild_id, "greetings", {"welcome": text("Hi {mention}"), "farewell": text("Bye"), "ban": text("Banned"), "dm": text("Hello"), "birthday_channel": self.log_id})
        await settings.save(self.guild_id, "starboard", {"channel_id": self.log_id})
        await settings.save(self.guild_id, "suggestions", {"channel_id": self.log_id, "dm": True})
        for command in self.bot.tree.get_commands():
            if isinstance(command, app_commands.ContextMenu):
                continue
            root = command.to_dict(self.bot.tree)
            for path, leaf in self.leaves(root):
                with self.subTest(command=" ".join((root["name"], *path))):
                    self.errors.lines.clear()
                    await self.run_command(root, path, leaf, every_option=False, user_id=self.owner_id, target_id=self.member_id)
                    await self.run_command(root, path, leaf, every_option=True, user_id=self.owner_id, target_id=self.member_id)
                    await self.run_command(root, path, leaf, every_option=True, user_id=self.member_id, target_id=self.admin_id)
        self.assertEqual(self.net.unknown, [])

    async def test_every_prefix_group_answers_without_a_subcommand(self):
        for command in sorted(self.bot.commands, key=lambda c: c.qualified_name):
            self.errors.lines.clear()
            self.dispatch_message(user_id=self.owner_id, content=f"!{command.qualified_name}")
            await self.settle()
            self.assert_no_errors(f"!{command.qualified_name}")


class EmbedTest(PostedCase):
    async def test_create_by_slash_sends_what_discord_expects(self):
        self.slash(("embed", "create"), color="FF0000", text="Title | Description")
        await self.settle()
        self.assert_no_errors()
        body = self.calls("POST", "/channels/{channel_id}/messages")[-1][2]
        self.assertEqual(body["embeds"][0]["title"], "Title")
        self.assertEqual(body["embeds"][0]["description"], "Description")
        self.assertEqual(body["embeds"][0]["color"], 0xFF0000)
        self.assertEqual(body["allowed_mentions"]["parse"], ["users"])

    async def test_create_by_prefix_takes_the_channel_off_the_end(self):
        self.dispatch_message(user_id=self.owner_id, content=f"!embed create ff0000 Title | Description <#{self.log_id}>")
        await self.settle()
        self.assert_no_errors()
        post = next(p for p in self.net.posted if p["embeds"])
        self.assertEqual(int(post["channel_id"]), self.log_id)
        self.assertEqual(post["embeds"][0]["description"], "Description")

    async def test_a_text_subcommand_doesnt_also_run_the_group(self):
        self.dispatch_message(user_id=self.owner_id, content="!embed create ff0000 Title")
        await self.settle()
        self.assertFalse([p for p in self.net.posted if "Use `/embed create`" in (p["content"] or "")])

    async def test_a_member_is_refused_cleanly(self):
        self.slash(("embed", "create"), user_id=self.member_id, color="FF0000", text="Title")
        await self.settle()
        self.assert_no_errors()
        self.assertFalse(self.calls("POST", "/channels/{channel_id}/messages"))
        self.assertIn("You need", self.answer()["content"])

    async def test_edit_keeps_the_rest_of_the_embed_through_the_real_message_object(self):
        self.slash(("embed", "custom"), json=json.dumps({"title": "Old", "description": "d", "color": 5, "footer": {"text": "foot"}, "fields": [{"name": "n", "value": "v", "inline": True}]}))
        await self.settle()
        self.assert_no_errors()
        message = next(p for p in self.net.posted if p["embeds"])
        self.slash(("embed", "edit"), message_id=message["id"], text="New | Words")
        await self.settle()
        self.assert_no_errors()
        patch = self.calls("PATCH", "/channels/{channel_id}/messages/{message_id}")[-1][2]
        embed = patch["embeds"][0]
        self.assertEqual((embed["title"], embed["description"], embed["color"], embed["footer"]["text"], embed["fields"][0]["name"]), ("New", "Words", 5, "foot", "n"))

    async def test_source_reads_a_real_embed_back(self):
        theirs = self.other_bot_post(content="", embeds=[{"type": "rich", "title": "Theirs", "color": 7, "footer": {"text": "f", "icon_url": "https://cdn.discordapp.com/attachments/1/2/f.png?ex=1&is=2&hm=3", "proxy_icon_url": "https://x"}, "image": {"url": "https://example.com/i.png", "proxy_url": "https://x", "width": 1, "height": 1}}])
        self.slash(("embed", "source"), message_id=theirs["id"])
        await self.settle()
        self.assert_no_errors()
        data = json.loads(self.answer()["content"].removeprefix("```json\n").removesuffix("\n```"))
        self.assertEqual(data["title"], "Theirs")
        self.assertEqual(data["footer"]["icon_url"], "https://cdn.discordapp.com/attachments/1/2/f.png")
        self.assertEqual(data["image"], {"url": "https://example.com/i.png"})

    async def test_unhide_sends_the_flag_off(self):
        theirs = self.other_bot_post(flags=4)
        self.slash(("embed", "unhide"), message_id=theirs["id"])
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.calls("PATCH", "/channels/{channel_id}/messages/{message_id}")[-1][2]["flags"], 0)

    async def test_the_menu_gives_the_owner_a_private_link(self):
        target = self.member_message()
        data = {"id": str(new_id()), "name": "Edit Message", "type": 3, "target_id": target["id"], "resolved": {"messages": {target["id"]: target}}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.owner_id))
        await self.settle()
        self.assert_no_errors()
        reply = self.answer()
        self.assertIn(f"/dashboard/{self.guild_id}/embeds?edit=", reply["content"])
        self.assertEqual(reply["flags"] & 64, 64)

    async def test_post_lookup_edit_and_revert_through_the_dashboard(self):
        client = await self.client()
        response = await client.post(self.path("/embeds/post"), json={"channel_id": self.text["id"], "payload": {"content": "One", "embeds": [{"title": "T"}], "buttons": [{"label": "Go", "url": "https://example.com"}]}})
        self.assertEqual(response.status, 200)
        posted = await response.json()
        self.assertEqual(self.answer()["components"][0]["components"][0]["url"], "https://example.com")
        look = await (await client.get(self.path(f"/embeds/lookup?link={posted['url']}"))).json()
        self.assertTrue(look["message"]["mine"])
        self.assertEqual(look["message"]["payload"]["buttons"], [{"url": "https://example.com", "label": "Go"}])
        response = await client.patch(self.path("/embeds/message"), json={"channel_id": self.text["id"], "message_id": posted["message_id"], "payload": {"content": "Two"}, "version": 1})
        self.assertEqual(response.status, 200)
        patch = self.calls("PATCH", "/channels/{channel_id}/messages/{message_id}")[-1][2]
        self.assertEqual((patch["content"], patch["embeds"], patch["components"]), ("Two", [], []))
        response = await client.post(self.path(f"/embeds/messages/{posted['id']}/revert"), json={"number": 1})
        self.assertEqual(response.status, 200)
        patch = self.calls("PATCH", "/channels/{channel_id}/messages/{message_id}")[-1][2]
        self.assertEqual((patch["content"], patch["embeds"][0]["title"], len(patch["components"])), ("One", "T", 1))

    async def test_a_post_in_an_archived_thread_edits_on_the_real_path(self):
        thread_id = new_id()
        self.net.channels[thread_id] = {"id": str(thread_id), "guild_id": str(self.guild_id), "parent_id": self.text["id"], "owner_id": self.net.bot_user["id"], "name": "old thread", "type": 11, "message_count": 1, "member_count": 1, "rate_limit_per_user": 0, "thread_metadata": {"archived": True, "auto_archive_duration": 1440, "archive_timestamp": iso(), "locked": False}, "flags": 0}
        message = self.net.message_json(thread_id, {"content": "old text", "embeds": []})
        message["author"] = self.net.bot_user
        self.net.messages[int(message["id"])] = message
        client = await self.client()
        link = f"https://discord.com/channels/{self.guild_id}/{thread_id}/{message['id']}"
        found = await client.get(self.path("/embeds/lookup"), params={"link": link})
        self.assertEqual(found.status, 200, await found.text())
        response = await client.patch(self.path("/embeds/message"), json={"channel_id": str(thread_id), "message_id": message["id"], "payload": text("new text")})
        self.assertEqual(response.status, 200, await response.text())
        patches = [call for call in self.calls("PATCH", "/channels/{channel_id}") if "archived" in call[2]]
        self.assertEqual([call[2]["archived"] for call in patches], [False, True])

    async def test_scheduled_post_goes_out_on_the_real_path(self):
        client = await self.client()
        response = await client.post(self.path("/embeds/post"), json={"channel_id": self.text["id"], "payload": text("Later"), "at": time.time() + 60})
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 61), 1)
        self.assertEqual(self.answer()["content"], "Later")
        self.assert_no_errors()

    async def test_adopting_real_messages_end_to_end(self):
        client = await self.client()
        self.bot.http.get_from_cdn = self._fake_cdn
        attachment = {"id": str(new_id()), "filename": "banner.png", "size": 10, "url": "https://cdn.discordapp.com/attachments/1/2/banner.png", "proxy_url": "https://x/banner.png", "content_type": "image/png"}
        first = self.other_bot_post(content="First", embeds=[{"type": "rich", "title": "One", "image": {"url": f"https://cdn.discordapp.com/attachments/1/{attachment['id']}/banner.png?ex=1&is=2&hm=3"}}], attachments=[attachment],
                                    reactions=[{"emoji": {"id": None, "name": UP}, "count": 3, "me": False, "me_burst": False, "burst_count": 0, "count_details": {"normal": 3, "burst": 0}, "burst_colors": []}])
        second = self.other_bot_post(content="Second", components=[{"type": 1, "components": [{"type": 2, "style": 1, "label": "Role", "custom_id": "theirs"}, {"type": 2, "style": 5, "label": "Site", "url": "https://example.com"}]}])
        listing = await (await client.get(self.path(f"/embeds/channel-messages?channel_id={self.text['id']}"))).json()
        rows = {row["id"]: row for row in listing["messages"]}
        self.assertEqual(rows[first["id"]]["attachments"][0]["filename"], "banner.png")
        self.assertEqual(rows[second["id"]]["controls"], 1)
        self.assertEqual(rows[second["id"]]["payload"]["buttons"], [{"url": "https://example.com", "label": "Site"}])
        self.assertTrue(listing["can_delete"])
        older = await (await client.get(self.path(f"/embeds/channel-messages?channel_id={self.text['id']}&before={second['id']}"))).json()
        self.assertEqual([row["id"] for row in older["messages"]], [first["id"]])
        body = {"messages": [{"channel_id": self.text["id"], "message_id": second["id"]}, {"channel_id": self.text["id"], "message_id": first["id"]}], "delete_originals": True, "copy_reactions": True}
        response = await client.post(self.path("/embeds/adoptions"), json=body)
        self.assertEqual(response.status, 200)
        await self.cog_of(Embeds).finish_jobs()
        job = await (await client.get(self.path(f"/embeds/adoptions/{(await response.json())['id']}"))).json()
        self.assertEqual(job["state"], "done")
        self.assertEqual([item["summary"] for item in job["items"]], ["First", "Second"])
        self.assertIn("buttons or menus", job["items"][1]["note"])
        posts = self.calls("POST", "/channels/{channel_id}/messages")
        self.assertEqual([p[2]["content"] for p in posts], ["First", "Second"])
        self.assertEqual(posts[0][2]["embeds"][0]["image"]["url"], "attachment://banner.png")
        self.assertEqual(posts[0][2]["allowed_mentions"]["parse"], [])
        self.assertEqual(len(posts[1][2]["components"][0]["components"]), 1)
        self.assertTrue(self.calls("PUT", "/reactions/"))
        self.assertNotIn(int(first["id"]), self.net.messages)
        self.assertNotIn(int(second["id"]), self.net.messages)
        self.assert_no_errors()

    async def test_a_post_whose_embed_is_hidden_comes_over_hidden(self):
        # A long role list runs on into a message whose embed is hidden, so Discord shows only its reactions and returns no embed.
        client = await self.client()
        hidden = self.other_bot_post(content="", embeds=[{"type": "rich", "title": "Printers, continued", "description": "Three more"}], flags=4)
        listing = await (await client.get(self.path(f"/embeds/channel-messages?channel_id={self.text['id']}"))).json()
        self.assertEqual([row["payload"]["embeds"] for row in listing["messages"] if row["id"] == hidden["id"]], [[]])
        response = await client.post(self.path("/embeds/adoptions"), json={"messages": [{"channel_id": self.text["id"], "message_id": hidden["id"]}], "delete_originals": True})
        self.assertEqual(response.status, 200)
        await self.cog_of(Embeds).finish_jobs()
        job = await (await client.get(self.path(f"/embeds/adoptions/{(await response.json())['id']}"))).json()
        self.assertEqual([(item["state"], item["note"], item["summary"]) for item in job["items"]], [("done", None, "Printers, continued")])
        self.assertEqual([call[2]["flags"] & 4 for call in self.calls("PATCH", "/channels/{channel_id}/messages/{message_id}")], [0, 4])
        copy = self.calls("POST", "/channels/{channel_id}/messages")[-1][2]
        self.assertEqual((copy["embeds"][0]["title"], copy["flags"] & 4), ("Printers, continued", 4))
        self.assertNotIn(int(hidden["id"]), self.net.messages)
        kept = await (await client.get(self.path("/embeds/messages"))).json()
        self.assertIn("Printers, continued", json.dumps(kept))
        self.assert_no_errors()

    async def test_a_hidden_post_left_in_place_is_hidden_again(self):
        client = await self.client()
        hidden = self.other_bot_post(content="", embeds=[{"type": "rich", "title": "Kept", "description": "Still here"}], flags=4)
        response = await client.post(self.path("/embeds/adoptions"), json={"messages": [{"channel_id": self.text["id"], "message_id": hidden["id"]}]})
        self.assertEqual(response.status, 200)
        await self.cog_of(Embeds).finish_jobs()
        self.assertEqual(self.net.messages[int(hidden["id"])]["flags"] & 4, 4)
        self.assertEqual(self.calls("POST", "/channels/{channel_id}/messages")[-1][2]["embeds"][0]["title"], "Kept")
        self.assert_no_errors()

    @staticmethod
    async def _fake_cdn(url: str) -> bytes:
        return b"image bytes"


class GreetingsTest(PostedCase):
    async def test_join_leave_and_ban_events_post_through_the_real_objects(self):
        cog = self.cog_of(Greetings)
        cog.BAN_WAIT = 0
        await self.bot.settings.save(self.guild_id, "greetings", {
            "welcome_channel": self.text_id, "welcome": {"content": "Hi {mention} of {server}, you are the {ord:{server(members)}}", "embeds": [{"title": "{user}", "thumbnail": {"url": "{user(avatar)}"}}], "buttons": []},
            "farewell": text("Bye {user}"), "ban": text("{user} is banned"), "dm": text("Hello {user}"),
        })
        state = self.bot._connection
        newcomer = new_id()
        self.net.members[newcomer] = {"user": user_json(newcomer, "newcomer"), "roles": [], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "avatar": None, "premium_since": None, "communication_disabled_until": None}
        state.parse_guild_member_add({"guild_id": str(self.guild_id), **self.net.members[newcomer]})
        await self.settle()
        self.assert_no_errors()
        welcome = next(p for p in self.net.posted if p["embeds"])
        self.assertEqual(welcome["embeds"][0]["title"], "newcomer")
        self.assertTrue(welcome["embeds"][0]["thumbnail"]["url"].startswith("https://cdn.discordapp.com/embed/avatars/"))
        self.assertIn(f"Hi <@{newcomer}> of Test Guild", welcome["content"])
        self.assertTrue(any(p["content"] == "Hello newcomer" for p in self.net.posted))
        state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.net.members[newcomer]["user"]})
        await self.settle()
        self.assert_no_errors()
        self.assertTrue(any(p["content"] == "Bye newcomer" for p in self.net.posted))
        state.parse_guild_ban_add({"guild_id": str(self.guild_id), "user": user_json(new_id(), "rule breaker")})
        await self.settle()
        self.assert_no_errors()
        self.assertTrue(any(p["content"] == "rule breaker is banned" for p in self.net.posted))

    async def test_a_birthday_gives_the_role_through_the_real_route_and_takes_it_back(self):
        cog = self.cog_of(Greetings)
        role_id = int(self.role_muted["id"])
        await self.bot.settings.save(self.guild_id, "greetings", {"birthday_channel": self.text_id, "birthday_role": role_id})
        today = datetime.datetime.now(datetime.timezone.utc)
        await cog._set_birthday(self.guild_id, self.member_id, today.month, today.day)
        await cog._announce(self.guild, cog.cfg(self.guild_id))
        self.assert_no_errors()
        self.assertIn(f"<@{self.member_id}>", self.answer()["content"])
        self.assertTrue(self.calls("PUT", "/guilds/{guild_id}/members/{user_id}/roles/{role_id}"))
        member = self.guild.get_member(self.member_id)
        member._roles.add(role_id)
        await self.bot.timers.run_due(time.time() + 86401)
        self.assertTrue(self.calls("DELETE", "/guilds/{guild_id}/members/{user_id}/roles/{role_id}"))
        self.assert_no_errors()

    async def test_the_test_button_uses_the_preview_stand_ins(self):
        client = await self.client()
        response = await client.post(self.path("/greetings/test"), json={"which": "welcome", "channel_id": self.text["id"], "payload": text("{user} in {server}, {channel}, {ord:{server(members)}}")})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.answer()["content"], "owner in Test Guild, general, 5th")
        self.assertEqual(self.calls("POST", "/channels/{channel_id}/messages")[-1][2]["allowed_mentions"]["parse"], [])
        response = await client.post(self.path("/greetings/test"), json={"which": "dm", "payload": text("Hi {user}")})
        self.assertEqual(response.status, 200)
        self.assertTrue(self.calls("POST", "/users/@me/channels"))
        self.assert_no_errors()


class StickyTest(PostedCase):
    async def test_a_message_brings_the_sticky_back_and_its_own_echo_doesnt(self):
        cog = self.cog_of(Sticky)
        cog.QUIET, cog.MIN_GAP, cog.MAX_WAIT = 0.01, 0.0, 0.1
        client = await self.client()
        response = await client.post(self.path("/sticky"), json={"channel_id": self.text["id"], "payload": text("Read the rules"), "seconds": 3600})
        self.assertEqual(response.status, 200)
        first = self.answer()
        self.assertEqual(self.calls("POST", "/channels/{channel_id}/messages")[-1][2]["allowed_mentions"]["parse"], [])
        self.bot._connection.parse_message_create({**first, "member": {"roles": [], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0}})
        await self.settle()
        live = cog._live[self.text_id]
        self.assertIsNone(live.task)
        self.dispatch_message(user_id=self.member_id, content="chatter")
        await self.settle()
        await asyncio.wait([live.task])
        self.assert_no_errors()
        self.assertNotEqual(self.answer()["id"], first["id"])
        self.assertEqual(self.answer()["content"], "Read the rules")
        self.assertNotIn(int(first["id"]), self.net.messages)
        cog.cog_unload()

    async def test_commands_and_expiry_through_the_real_objects(self):
        self.slash(("sticky", "add"), message="Hello {server}", duration="2h", channel=self.log_channel)
        await self.settle()
        self.assert_no_errors()
        copy = next(p for p in self.net.posted if p["content"] == "Hello Test Guild")
        self.assertEqual(int(copy["channel_id"]), self.log_id)
        self.assertEqual(await self.db.scalar("SELECT seconds FROM sticky_messages"), 7200)
        self.slash(("sticky", "list"))
        self.dispatch_message(user_id=self.owner_id, content="!sticky list")
        await self.settle()
        self.assert_no_errors()
        self.dispatch_message(user_id=self.owner_id, content="!sticky add Stay here 3d")
        await self.settle()
        self.assertEqual(await self.db.scalar("SELECT seconds FROM sticky_messages WHERE channel_id = ?", (self.text_id,)), 259200)
        await self.bot.timers.run_due(time.time() + 8000)
        self.assert_no_errors()
        self.assertNotIn(int(copy["id"]), self.net.messages)


class AutocompleteTest(PostedCase):
    async def test_template_names_come_back_through_the_real_interaction(self):
        await self.db.run("INSERT INTO sticky_templates (guild_id, name, payload, summary, updated_at) VALUES (?, 'Rules', '{}', '', 0)", (self.guild_id,))
        data = {"id": str(new_id()), "name": "sticky", "type": 1, "options": [{"name": "template", "type": 1, "options": [{"name": "template_name", "type": 3, "value": "ru", "focused": True}]}]}
        self.bot._connection.parse_interaction_create(self.interaction_payload(4, data, user_id=self.owner_id))
        await self.settle()
        self.assert_no_errors()
        answer = next(call[2] for call in self.net.calls if call[1].endswith("/callback"))
        self.assertEqual(answer["data"]["choices"], [{"name": "Rules", "value": "Rules"}])


class StarboardTest(PostedCase):
    def reaction(self, user_id: int, message: dict, emoji: dict | None = None) -> dict:
        return {"user_id": str(user_id), "channel_id": message["channel_id"], "message_id": message["id"], "guild_id": str(self.guild_id), "member": self.member_json[user_id], "emoji": emoji or {"id": None, "name": STAR}, "burst": False, "type": 0, "message_author_id": message["author"]["id"]}

    def add(self, user_id: int, message: dict) -> None:
        self.bot._connection.parse_message_reaction_add(self.reaction(user_id, message))

    def remove(self, user_id: int, message: dict) -> None:
        data = self.reaction(user_id, message)
        data.pop("member")
        data.pop("message_author_id")
        self.bot._connection.parse_message_reaction_remove(data)

    async def test_stars_post_update_and_take_down_through_real_events(self):
        await self.bot.settings.save(self.guild_id, "starboard", {"channel_id": self.log_id, "threshold": 2})
        message = self.member_message()
        self.add(self.mod_id, message)
        await self.settle()
        self.assertEqual(self.net.posted, [])
        self.add(self.admin_id, message)
        await self.settle()
        self.assert_no_errors()
        post = self.answer()
        self.assertEqual(int(post["channel_id"]), self.log_id)
        self.assertEqual(post["content"], f"{STAR} **2** <#{self.text_id}>")
        self.assertEqual(post["embeds"][0]["description"], "A very good post")
        self.assertEqual(post["embeds"][0]["author"]["name"], "member")
        self.add(self.owner_id, message)
        await self.settle()
        self.assert_no_errors()
        patch = self.calls("PATCH", "/channels/{channel_id}/messages/{message_id}")[-1][2]
        self.assertIn("**3**", patch["content"])
        self.assertNotIn("embeds", patch)
        for user_id in (self.owner_id, self.admin_id):
            self.remove(user_id, message)
            await self.settle()
        self.assert_no_errors()
        self.assertNotIn(int(post["id"]), self.net.messages)

    async def test_an_attachment_and_a_reply_come_through_a_real_message(self):
        await self.bot.settings.save(self.guild_id, "starboard", {"channel_id": self.log_id, "threshold": 1, "autostar": True})
        original = self.member_message(content="Replied to")
        message = self.member_message(content="Look at this")
        message["attachments"] = [{"id": str(new_id()), "filename": "a.png", "size": 1, "url": "https://cdn.example/a.png", "proxy_url": "https://cdn.example/a.png", "content_type": "image/png"}]
        message["message_reference"] = {"message_id": original["id"], "channel_id": original["channel_id"], "guild_id": str(self.guild_id), "type": 0}
        message["referenced_message"] = original
        message["type"] = 19
        self.add(self.mod_id, message)
        await self.settle()
        self.assert_no_errors()
        post = self.answer()
        self.assertEqual(post["embeds"][0]["image"]["url"], "https://cdn.example/a.png")
        self.assertEqual(post["embeds"][0]["fields"][0]["name"], "Replying to")
        self.assertTrue(post["embeds"][0]["timestamp"])
        self.assertTrue(self.calls("PUT", "/reactions/"))

    async def test_commands_with_a_real_board(self):
        await self.bot.settings.save(self.guild_id, "starboard", {"channel_id": self.log_id, "threshold": 1})
        message = self.member_message()
        self.add(self.mod_id, message)
        await self.settle()
        post = self.answer()
        self.slash(("starboard", "show"), message_id=message["id"])
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.answer()["content"], post["content"])
        self.assertEqual(self.answer()["embeds"][0]["description"], "A very good post")
        for path in (("starboard", "server"), ("starboard", "stats"), ("starboard", "random"), ("starboard", "config")):
            self.slash(path)
            await self.settle()
            self.assert_no_errors(" ".join(path))

    async def test_setup_makes_a_channel_on_the_real_route(self):
        self.slash(("starboard", "setup"), name="stars")
        await self.settle()
        self.assert_no_errors()
        created = self.calls("POST", "/guilds/{guild_id}/channels")[-1][2]
        self.assertEqual(created["name"], "stars")
        everyone = next(o for o in created["permission_overwrites"] if int(o["id"]) == self.guild_id)
        self.assertTrue(int(everyone["deny"]) & discord.Permissions(send_messages=True).value)
        self.assertIsNotNone(self.bot.settings.get(self.guild_id, "starboard")["channel_id"])


class SuggestionsTest(PostedCase):
    async def test_a_suggestion_and_decision_through_the_real_path(self):
        await self.bot.settings.save(self.guild_id, "suggestions", {"channel_id": self.log_id, "dm": True, "threshold": 2})
        self.slash(("suggestion", "suggest"), user_id=self.member_id, content="Add a bird channel")
        await self.settle()
        self.assert_no_errors()
        post = next(p for p in self.net.posted if p["embeds"])
        self.assertEqual(int(post["channel_id"]), self.log_id)
        self.assertEqual(post["embeds"][0]["title"], "Suggestion #1")
        self.assertEqual(len(self.calls("PUT", "/reactions/")), 2)
        post["reactions"] = [{"emoji": {"id": None, "name": UP}, "count": 3, "me": True, "me_burst": False, "burst_count": 0, "count_details": {"normal": 3, "burst": 0}, "burst_colors": []},
                             {"emoji": {"id": None, "name": DOWN}, "count": 1, "me": True, "me_burst": False, "burst_count": 0, "count_details": {"normal": 1, "burst": 0}, "burst_colors": []}]
        self.slash(("suggestion", "approve"), suggestion_id=1, reason="Yes please")
        await self.settle()
        self.assert_no_errors()
        entry = next(p for p in reversed(self.net.posted) if p["embeds"] and "Approved" in (p["embeds"][0].get("title") or ""))
        self.assertEqual(entry["embeds"][0]["fields"][0]["value"], "Yes please")
        self.assertIn(f"{UP} 2", entry["embeds"][0]["fields"][1]["value"])
        self.assertEqual(entry["embeds"][0]["color"], 0x2ECC71)
        self.assertNotIn(int(post["id"]), self.net.messages)
        self.assertTrue(self.calls("POST", "/users/@me/channels"))

    async def test_a_member_cant_decide(self):
        await self.bot.settings.save(self.guild_id, "suggestions", {"channel_id": self.log_id})
        self.slash(("suggestion", "approve"), user_id=self.member_id, suggestion_id=1)
        await self.settle()
        self.assert_no_errors()
        self.assertIn("You need", self.answer()["content"])

    async def test_prefix_suggest_keeps_the_whole_sentence(self):
        await self.bot.settings.save(self.guild_id, "suggestions", {"channel_id": self.log_id})
        self.dispatch_message(user_id=self.member_id, content="!suggestion suggest Add a whole bird channel")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(await self.db.scalar("SELECT content FROM suggestions"), "Add a whole bird channel")

    async def test_channel_creation_on_the_real_route(self):
        self.slash(("suggestion", "channel"), name="ideas")
        await self.settle()
        self.assert_no_errors()
        self.assertIsNotNone(self.bot.settings.get(self.guild_id, "suggestions")["channel_id"])
