"""Posting under another name through the bot's own webhooks, through the real library with only the network replaced."""
from __future__ import annotations

import io

import discord

from captainguild import brand
from captainguild.core.ops import OpsError
from captainguild.core.webhooks import Webhooks, safe_name
from tests.gateway import FakeHTTPResponse, GatewayTest, new_id


class NamesTest(GatewayTest):
    modules: list = []

    async def test_reserved_words_are_respelled_and_long_names_cut(self):
        self.assertEqual(safe_name("DiscordFan"), "disc0rdFan")
        self.assertEqual(safe_name("clyde the bot"), "clyd3 the bot")
        self.assertEqual(safe_name("x" * 100), "x" * 80)
        self.assertEqual(safe_name("   "), "Member")
        self.assertEqual(safe_name(None, "Feed"), "Feed")


class WebhooksTest(GatewayTest):
    modules: list = []

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.hooks = Webhooks(self.bot)
        self.channel = self.guild.get_channel(int(self.text["id"]))

    def made(self) -> list[tuple]:
        return [call for call in self.net.calls if call[0] == "POST" and call[1] == "/channels/{channel_id}/webhooks"]

    def posts(self) -> list[tuple]:
        return [call for call in self.net.webhook_params if call[0] == "POST" and call[1] == "/webhooks/{webhook_id}/{webhook_token}"]

    async def test_one_webhook_per_channel_is_made_and_kept(self):
        first = await self.hooks.send(self.channel, name="Someone", avatar_url="https://cdn.example/a.png", content="hello")
        await self.hooks.send(self.channel, name="Someone else", content="again")
        self.assertEqual(len(self.made()), 1)
        self.assertEqual(self.made()[0][2]["name"], brand.NAME)
        bodies = [body for method, path, body in self.net.calls if path == "/webhooks/{webhook_id}/{webhook_token}"]
        self.assertEqual([body["username"] for body in bodies], ["Someone", "Someone else"])
        self.assertEqual(bodies[0]["avatar_url"], "https://cdn.example/a.png")
        self.assertEqual(bodies[0]["allowed_mentions"], {"parse": []})
        self.assertTrue(first.webhook_id)
        self.assertTrue(self.posts()[0][2].get("wait"))

    async def test_an_existing_webhook_of_the_bots_own_is_found_again(self):
        self.net.keep_webhooks = True
        await self.hooks.send(self.channel, name="A", content="one")
        fresh = Webhooks(self.bot)  # a restart
        await fresh.send(self.channel, name="B", content="two")
        self.assertEqual(len(self.made()), 1)

    async def test_a_thread_post_goes_through_its_parents_webhook(self):
        thread_id = new_id()
        thread = discord.Thread(guild=self.guild, state=self.bot._connection, data={
            "id": str(thread_id), "guild_id": str(self.guild_id), "parent_id": self.text["id"], "owner_id": str(self.owner_id), "name": "help", "type": 11,
            "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "thread_metadata": {"archived": False, "auto_archive_duration": 1440, "archive_timestamp": "2026-01-01T00:00:00+00:00", "locked": False},
        })
        await self.hooks.send(thread, name="A", content="in a thread")
        self.assertEqual(self.made()[0][1], "/channels/{channel_id}/webhooks")
        self.assertEqual(self.posts()[-1][2].get("thread_id"), thread_id)

    async def test_a_deleted_webhook_is_made_again_once_and_files_go_again(self):
        await self.hooks.send(self.channel, name="A", content="one")
        gone = int(self.net.webhooks[int(self.text["id"])][0]["id"])
        self.net.gone_webhooks.add(gone)
        await self.hooks.send(self.channel, name="A", content="two", files=[discord.File(io.BytesIO(b"data"), "a.txt")])
        self.assertEqual(len(self.made()), 2)
        self.net.gone_webhooks.add(int(self.net.webhooks[int(self.text["id"])][1]["id"]))
        self.net.calls.clear()
        original = self.net.http

        async def every_new_one_is_gone(route, **kwargs):
            answer = await original(route, **kwargs)
            if route.path == "/channels/{channel_id}/webhooks" and route.method == "POST":
                self.net.gone_webhooks.add(int(answer["id"]))
            return answer
        self.bot.http.request = every_new_one_is_gone
        with self.assertRaises(OpsError):
            await self.hooks.send(self.channel, name="A", content="three")
        self.assertEqual(len(self.made()), 1, "one more try, not a loop")

    async def test_no_manage_webhooks_says_so(self):
        original = self.net.http

        async def refuse(route, **kwargs):
            if route.path == "/channels/{channel_id}/webhooks":
                raise discord.Forbidden(FakeHTTPResponse(403), {"code": 50013, "message": "Missing Permissions"})
            return await original(route, **kwargs)
        self.bot.http.request = refuse
        with self.assertRaises(OpsError) as caught:
            await self.hooks.send(self.channel, name="A", content="x")
        self.assertIn("Manage Webhooks", str(caught.exception))

    async def test_a_stored_webhook_edits_and_deletes_what_it_posted(self):
        self.bot.http._HTTPClient__session = object()  # set at login, and the network here is replaced below the session
        hook = self.hooks.partial(new_id(), "stored-token")
        await self.hooks.edit(hook, 123456789012345678, thread_id=987654321098765432, content="changed")
        await self.hooks.delete(hook, 123456789012345678)
        edits = [call for call in self.net.webhook_params if call[0] == "PATCH"]
        deletes = [call for call in self.net.webhook_params if call[0] == "DELETE"]
        self.assertEqual(edits[0][1], "/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}")
        self.assertEqual(edits[0][2].get("thread_id"), 987654321098765432)
        self.assertEqual(len(deletes), 1)
        self.assertNotIn("thread_id", deletes[0][2])
        self.net.gone_webhooks.add(hook.id)
        with self.assertRaises(OpsError):
            await self.hooks.edit(hook, 123456789012345678, content="again")
        await self.hooks.delete(hook, 123456789012345678)  # already gone counts as done
