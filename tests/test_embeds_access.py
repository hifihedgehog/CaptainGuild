"""What a person can do through the embed editor and commands, against what Discord would let them do, and the editor's edge cases."""
from __future__ import annotations

import asyncio
import json
import time
from unittest import mock

import aiohttp
import discord
from aiohttp import web
from aiohttp.test_utils import TestServer

from captainguild.core.ops import OpsError, SendUnknown
from captainguild.core.validate import Invalid
from captainguild.core.messages import clean_payload
from captainguild.modules import embeds as embeds_module
from captainguild.modules.embeds import Embeds
from tests.fakes import FakeChannel, FakeMember, FakeMessage
from tests.test_web_input import ReadOnly, SiteCase

TEXT = {"content": "hello", "embeds": [], "buttons": []}


class Thread(FakeChannel):
    """What discord.py hands back for an archived thread. It isn't in the guild's lists."""

    def __init__(self, guild, name, *, archived=True):
        super().__init__(guild, name, discord.ChannelType.public_thread)
        guild.channels.remove(self)
        self.archived = archived
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.archived = kwargs.get("archived", self.archived)
        return self


class Case(SiteCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.embeds = self.cog(Embeds)
        self.other_bot = FakeMember(self.guild, "Old Bot", bot=True)

    async def library_row(self, channel, *, deleted=False, payload=TEXT):
        message = FakeMessage(channel, self.guild.me, payload["content"])
        channel.sent.append(message)
        posted = await self.embeds.record_post(self.guild.id, channel.id, message.id, payload, source="dashboard")
        if deleted:
            await self.db.run("UPDATE posted_messages SET deleted_at = 1.0 WHERE id = ?", (posted,))
        return posted, message


class PostingTest(Case):
    async def test_repost_needs_send_there(self):
        news = ReadOnly(self.guild, "news", self.manager)
        posted, _ = await self.library_row(news, deleted=True)
        client = await self.site(self.manager)
        response = await client.post(self.path(f"/embeds/messages/{posted}/repost"), json={})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["fields"]["channel_id"], "You can't post in that channel.")
        self.assertEqual(len(news.sent), 1)

    async def test_an_edit_needs_send_there(self):
        news = ReadOnly(self.guild, "news", self.manager)
        _, message = await self.library_row(news)
        client = await self.site(self.manager)
        response = await client.patch(self.path("/embeds/message"), json={"channel_id": str(news.id), "message_id": str(message.id), "payload": {"content": "changed"}})
        self.assertEqual(response.status, 400)
        self.assertEqual(message.content, "hello")

    async def test_a_revert_needs_send_there(self):
        news = ReadOnly(self.guild, "news", self.manager)
        posted, _ = await self.library_row(news)
        await self.embeds._add_version(self.guild.id, posted, {"content": "second", "embeds": [], "buttons": []}, actor=None, note="x")
        client = await self.site(self.manager)
        response = await client.post(self.path(f"/embeds/messages/{posted}/revert"), json={"number": 1})
        self.assertEqual(response.status, 400)

    async def scheduled(self, channel, ping=False):
        client = await self.site(self.admin)
        response = await client.post(self.path("/embeds/post"), json={"channel_id": str(channel.id), "payload": {"content": "hi"}, "ping": ping, "at": time.time() + 3600})
        return (await response.json())["scheduled"]

    async def test_post_now_needs_send_there_and_the_ping(self):
        news = ReadOnly(self.guild, "news", self.manager)
        post = await self.scheduled(news)
        client = await self.site(self.manager)
        self.assertEqual((await client.post(self.path(f"/embeds/scheduled/{post}/post"))).status, 400)
        self.assertEqual(news.sent, [])
        news.names = ("view_channel", "read_message_history", "send_messages")
        pinging = await self.scheduled(news, ping=True)
        self.assertEqual((await client.post(self.path(f"/embeds/scheduled/{pinging}/post"))).status, 400)
        self.assertEqual(news.sent, [])
        self.assertEqual((await client.post(self.path(f"/embeds/scheduled/{post}/post"))).status, 200)

    async def test_create_refuses_a_channel_they_cant_post_in(self):
        news = ReadOnly(self.guild, "news", self.manager)
        context = self.context(self.manager, self.channel)
        await self.embeds.embed_create.callback(self.embeds, context, "FF0000", text="Hello", channel=news)
        self.assertEqual(context.fails, ["You can't post in that channel."])
        self.assertEqual(news.sent, [])

    async def test_custom_refuses_a_channel_they_cant_post_in(self):
        news = ReadOnly(self.guild, "news", self.manager)
        context = self.context(self.manager, self.channel)
        await self.embeds.embed_custom.callback(self.embeds, context, json='{"content": "hi"}', channel=news)
        self.assertEqual(context.fails, ["You can't post in that channel."])

    async def test_edit_refuses_a_channel_they_cant_post_in(self):
        news = ReadOnly(self.guild, "news", self.manager)
        _, message = await self.library_row(news)
        context = self.context(self.manager, self.channel)
        await self.embeds.embed_editcustom.callback(self.embeds, context, str(message.id), json='{"content": "changed"}', channel=news)
        self.assertEqual(context.fails, ["You can't post in that channel."])
        self.assertEqual(message.content, "hello")

    async def test_source_answers_with_the_same_sentence_as_the_dashboard(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel")
        _, message = await self.library_row(news)
        context = self.context(self.manager, self.channel)
        await self.embeds.embed_source.callback(self.embeds, context, str(message.id), channel=news)
        self.assertEqual(context.fails, ["You can't read the messages in that channel."])

    async def test_unhide_on_someone_elses_message_needs_manage_messages(self):
        message = FakeMessage(self.channel, self.other_bot, "theirs")
        self.channel.sent.append(message)
        context = self.context(self.manager, self.channel)
        await self.embeds.embed_unhide.callback(self.embeds, context, str(message.id), channel=self.channel)
        self.assertEqual(context.fails, ["You need Manage Messages in that channel to do that."])


class ReadingTest(Case):
    async def test_lookup_needs_read_history(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        _, message = await self.library_row(news)
        client = await self.site(self.manager)
        response = await client.get(self.path("/embeds/lookup"), params={"link": message.jump_url})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["fields"]["link"], "You can't read the messages in that channel.")

    async def test_paging_a_channel_needs_read_history(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        client = await self.site(self.manager)
        response = await client.get(self.path("/embeds/channel-messages"), params={"channel_id": str(news.id)})
        self.assertEqual(response.status, 400)

    async def test_history_needs_read_history(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        posted, _ = await self.library_row(news)
        client = await self.site(self.manager)
        self.assertEqual((await client.get(self.path(f"/embeds/messages/{posted}"))).status, 404)
        listed = await (await client.get(self.path("/embeds/messages"))).json()
        self.assertEqual(listed["rows"], [])

    async def hidden_job(self):
        hidden = ReadOnly(self.guild, "private", self.manager, "manage_guild")
        job = await self.db.insert("INSERT INTO adopt_jobs (guild_id, target_channel_id, delete_originals, copy_reactions, state, created_at) VALUES (?, NULL, 0, 0, 'stopped', 1.0)", (self.guild.id,))
        await self.db.run("INSERT INTO adopt_items (guild_id, job_id, position, source_channel_id, source_message_id, author_name, summary, state) VALUES (?, ?, 0, ?, 5, 'Old Bot', 'the private plan', 'done')", (self.guild.id, job, hidden.id))
        return job

    async def test_a_job_from_a_hidden_channel_doesnt_show(self):
        job = await self.hidden_job()
        client = await self.site(self.manager)
        self.assertEqual(await (await client.get(self.path("/embeds/adoptions"))).json(), [])
        self.assertEqual((await client.get(self.path(f"/embeds/adoptions/{job}"))).status, 404)

    async def test_a_job_from_a_hidden_channel_cant_be_stopped_or_resumed(self):
        job = await self.hidden_job()
        client = await self.site(self.manager)
        self.assertEqual((await client.post(self.path(f"/embeds/adoptions/{job}/resume"), json={})).status, 404)
        await self.db.run("UPDATE adopt_jobs SET state = 'running' WHERE id = ?", (job,))
        self.assertEqual((await client.post(self.path(f"/embeds/adoptions/{job}/stop"))).status, 404)
        self.assertEqual(await self.db.scalar("SELECT state FROM adopt_jobs WHERE id = ?", (job,)), "running")

    async def test_an_administrator_still_sees_it(self):
        await self.hidden_job()
        client = await self.site(self.admin)
        self.assertEqual(len(await (await client.get(self.path("/embeds/adoptions"))).json()), 1)


class AdoptTest(Case):
    def posted_by(self, author, channel=None, content="words"):
        channel = channel or self.channel
        message = FakeMessage(channel, author, content)
        channel.sent.append(message)
        return message

    async def adopt(self, member, *picked, **form):
        client = await self.site(member)
        return await client.post(self.path("/embeds/adoptions"), json={"messages": [{"channel_id": str(m.channel.id), "message_id": str(m.id)} for m in picked], **form})

    def can_all_but_manage(self):
        return ReadOnly(self.guild, "general", self.manager, "view_channel", "read_message_history", "send_messages")

    async def test_deleting_originals_needs_manage_messages(self):
        channel = self.can_all_but_manage()
        message = self.posted_by(self.other_bot, channel)
        response = await self.adopt(self.manager, message, delete_originals=True)
        self.assertEqual(response.status, 400)
        self.assertIn("Manage Messages", (await response.json())["fields"]["messages"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM adopt_jobs"), 0)

    async def test_a_members_message_needs_manage_messages(self):
        channel = self.can_all_but_manage()
        message = self.posted_by(self.member, channel)
        response = await self.adopt(self.manager, message)
        self.assertEqual(response.status, 400)
        self.assertIn("a member's message", (await response.json())["fields"]["messages"])

    async def test_a_bots_message_can_be_reposted_without_it(self):
        channel = self.can_all_but_manage()
        message = self.posted_by(self.other_bot, channel)
        response = await self.adopt(self.manager, message)
        self.assertEqual(response.status, 200)
        await self.embeds.finish_jobs()

    async def test_manage_messages_covers_both(self):
        channel = ReadOnly(self.guild, "general", self.manager, "view_channel", "read_message_history", "send_messages", "manage_messages")
        message = self.posted_by(self.member, channel)
        self.assertEqual((await self.adopt(self.manager, message, delete_originals=True)).status, 200)
        await self.embeds.finish_jobs()

    async def test_copying_from_a_channel_they_cant_read_is_refused(self):
        channel = ReadOnly(self.guild, "private", self.manager, "view_channel", "send_messages")
        message = self.posted_by(self.other_bot, channel)
        response = await self.adopt(self.manager, message, channel_id=str(self.channel.id))
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["fields"]["messages"], "You can't read the messages in that channel.")

    async def test_the_target_needs_send(self):
        news = ReadOnly(self.guild, "news", self.manager)
        message = self.posted_by(self.other_bot)
        response = await self.adopt(self.manager, message, channel_id=str(news.id))
        self.assertEqual(response.status, 400)

    async def test_no_target_means_the_source_needs_send(self):
        channel = ReadOnly(self.guild, "news", self.manager)
        message = self.posted_by(self.other_bot, channel)
        response = await self.adopt(self.manager, message)
        self.assertEqual(response.status, 400)


class EditingTest(Case):
    async def test_an_edit_leaves_another_features_buttons_alone(self):
        _, message = await self.library_row(self.channel)
        button = discord.ui.Button(style=discord.ButtonStyle.primary, custom_id="enter", label="Enter")
        message.components = [button]
        client = await self.site(self.admin)
        response = await client.patch(self.path("/embeds/message"), json={"channel_id": str(self.channel.id), "message_id": str(message.id), "payload": {"content": "changed"}})
        self.assertEqual(response.status, 200)
        self.assertEqual(message.content, "changed")
        self.assertNotIn("view", message.kwargs)

    async def test_changing_link_buttons_on_such_a_message_is_refused(self):
        _, message = await self.library_row(self.channel)
        message.components = [discord.ui.Button(style=discord.ButtonStyle.primary, custom_id="enter", label="Enter")]
        client = await self.site(self.admin)
        response = await client.patch(self.path("/embeds/message"), json={"channel_id": str(self.channel.id), "message_id": str(message.id), "payload": {"content": "changed", "buttons": [{"url": "https://example.test", "label": "Go"}]}})
        self.assertEqual(response.status, 502)
        self.assertIn("another feature", (await response.json())["error"])
        self.assertEqual(message.content, "hello")

    async def test_a_message_with_only_link_buttons_takes_new_ones(self):
        _, message = await self.library_row(self.channel)
        client = await self.site(self.admin)
        response = await client.patch(self.path("/embeds/message"), json={"channel_id": str(self.channel.id), "message_id": str(message.id), "payload": {"content": "changed", "buttons": [{"url": "https://example.test", "label": "Go"}]}})
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(message.kwargs["view"])

    async def test_too_many_controls_answers_with_the_remedy(self):
        ops = self.bot.ops
        with mock.patch("captainguild.core.messages.link_view", side_effect=ValueError("maximum number of children exceeded")):
            with self.assertRaises(OpsError) as caught:
                await ops.send_message(self.channel.id, {"content": "hi", "buttons": [{"url": "https://a.test"}]})
        self.assertIn("Remove a link button", str(caught.exception))
        _, message = await self.library_row(self.channel)
        with mock.patch("captainguild.core.messages.link_view", side_effect=ValueError("maximum number of children exceeded")):
            with self.assertRaises(OpsError) as caught:
                await ops.edit_message(self.channel.id, message.id, {"content": "hi"})
        self.assertIn("Remove a link button", str(caught.exception))

    async def test_update_puts_the_text_back_when_the_live_message_drifted(self):
        posted, message = await self.library_row(self.channel)
        message.content = "another feature changed this"
        result = await self.embeds.edit_post(self.guild.id, self.channel.id, message.id, TEXT, actor=None)
        self.assertTrue(result["changed"])
        self.assertEqual(message.content, "hello")

    async def test_nothing_changed_when_the_live_message_already_says_it(self):
        posted, message = await self.library_row(self.channel)
        await self.embeds._add_version(self.guild.id, posted, {"content": "newer", "embeds": [], "buttons": []}, actor=None, note="x")
        result = await self.embeds.edit_post(self.guild.id, self.channel.id, message.id, TEXT, actor=None)
        self.assertFalse(result["changed"])

    async def test_a_template_link_is_refused_where_nothing_fills_it_in(self):
        with self.assertRaises(Invalid):
            clean_payload({"embeds": [{"title": "t", "image": {"url": "{user(avatar)}"}}]})
        self.assertEqual(clean_payload({"embeds": [{"title": "t", "image": {"url": "{user(avatar)}"}}]}, templates=True)["embeds"][0]["image"]["url"], "{user(avatar)}")


class ArchivedThreadTest(Case):
    async def thread(self, **kwargs):
        thread = Thread(self.guild, "old thread", **kwargs)

        async def fetch(channel_id):
            if channel_id != thread.id:
                raise discord.NotFound(mock.Mock(status=404, reason="gone"), "gone")
            return thread

        self.guild.fetch_channel = fetch
        message = FakeMessage(thread, self.guild.me, "hello")
        thread.sent.append(message)
        return thread, message

    async def test_lookup_reads_a_post_in_an_archived_thread(self):
        thread, message = await self.thread()
        client = await self.site(self.admin)
        response = await client.get(self.path("/embeds/lookup"), params={"link": message.jump_url})
        self.assertEqual(response.status, 200, await response.text())

    async def test_a_gone_channel_doesnt_blame_another_server(self):
        await self.thread()
        client = await self.site(self.admin)
        link = f"https://discord.com/channels/{self.guild.id}/999/888"
        response = await client.get(self.path("/embeds/lookup"), params={"link": link})
        self.assertEqual(response.status, 400)
        self.assertNotIn("another server", (await response.json())["fields"]["link"])
        link = f"https://discord.com/channels/12345/999/888"
        response = await client.get(self.path("/embeds/lookup"), params={"link": link})
        self.assertIn("another server", (await response.json())["fields"]["link"])

    async def test_an_edit_opens_the_thread_and_archives_it_again(self):
        thread, message = await self.thread()
        client = await self.site(self.admin)
        response = await client.patch(self.path("/embeds/message"), json={"channel_id": str(thread.id), "message_id": str(message.id), "payload": {"content": "changed"}})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(message.content, "changed")
        self.assertEqual([edit["archived"] for edit in thread.edits], [False, True])
        self.assertTrue(thread.archived)

    async def test_refused_to_open_names_manage_threads(self):
        thread, message = await self.thread()

        async def refuse(**kwargs):
            raise discord.Forbidden(mock.Mock(status=403, reason="no"), "no")

        thread.edit = refuse
        client = await self.site(self.admin)
        response = await client.patch(self.path("/embeds/message"), json={"channel_id": str(thread.id), "message_id": str(message.id), "payload": {"content": "changed"}})
        self.assertEqual(response.status, 502)
        self.assertIn("Manage Threads", (await response.json())["error"])

    async def test_an_archived_thread_in_a_hidden_channel_stays_hidden(self):
        thread, message = await self.thread()
        thread.permissions_for = lambda member: discord.Permissions.none()
        client = await self.site(self.manager)
        response = await client.get(self.path("/embeds/lookup"), params={"link": message.jump_url})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["fields"]["link"], "You can't see that channel.")

    async def test_the_command_finds_it_too(self):
        thread, message = await self.thread()
        context = self.context(self.admin, self.channel)
        await self.embeds.embed_editcustom.callback(self.embeds, context, message.jump_url, json='{"content": "changed"}', channel=None)
        self.assertEqual(context.fails, [])
        self.assertEqual(message.content, "changed")

    async def test_a_thread_in_another_server_isnt_found(self):
        thread, _ = await self.thread()
        other = FakeChannel(self.guild, "x")
        thread.guild = mock.Mock(id=12345)
        self.assertFalse(await self.bot.ops.find_thread(self.guild.id, thread.id))
        self.assertIsNone(self.bot.ops.known_thread(self.guild.id, thread.id))
        self.assertTrue(await self.bot.ops.find_thread(self.guild.id, other.id))


class GoneChannelTest(Case):
    async def test_a_row_whose_channel_is_gone_can_be_removed(self):
        posted, _ = await self.library_row(self.channel)
        self.guild.channels.remove(self.channel)
        client = await self.site(self.admin)
        self.assertEqual((await client.delete(self.path(f"/embeds/messages/{posted}"))).status, 200)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM posted_messages WHERE id = ?", (posted,)), 0)

    async def test_editing_it_marks_it_deleted(self):
        posted, message = await self.library_row(self.channel)
        self.guild.channels.remove(self.channel)
        with self.assertRaises(OpsError):
            await self.embeds.edit_post(self.guild.id, self.channel.id, message.id, {"content": "x"}, actor=None)
        self.assertIsNotNone(await self.db.scalar("SELECT deleted_at FROM posted_messages WHERE id = ?", (posted,)))

    async def test_a_deleted_thread_marks_its_rows(self):
        thread = Thread(self.guild, "t", archived=False)
        posted, _ = await self.library_row(thread)
        await self.embeds.on_raw_thread_delete(mock.Mock(guild_id=self.guild.id, thread_id=thread.id))
        self.assertIsNotNone(await self.db.scalar("SELECT deleted_at FROM posted_messages WHERE id = ?", (posted,)))


class RepostTwiceTest(Case):
    async def test_two_clicks_post_once(self):
        posted, _ = await self.library_row(self.channel, deleted=True)
        release = asyncio.Event()
        original = self.channel.send
        sends = []

        async def slow(*args, **kwargs):
            sends.append(args)
            await release.wait()
            return await original(*args, **kwargs)

        self.channel.send = slow
        client = await self.site(self.admin)
        first = asyncio.ensure_future(client.post(self.path(f"/embeds/messages/{posted}/repost"), json={}))
        while not sends:
            await asyncio.sleep(0.001)
        second = await asyncio.wait_for(client.post(self.path(f"/embeds/messages/{posted}/repost"), json={}), 5)
        self.assertEqual(second.status, 400)
        release.set()
        self.assertEqual((await first).status, 200)
        self.assertEqual(len(sends), 1)

    async def test_a_failed_send_leaves_it_deleted(self):
        posted, _ = await self.library_row(self.channel, deleted=True)

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(mock.Mock(status=403, reason="no"), "no")

        self.channel.send = refuse
        client = await self.site(self.admin)
        self.assertEqual((await client.post(self.path(f"/embeds/messages/{posted}/repost"), json={})).status, 502)
        self.assertEqual(await self.db.scalar("SELECT deleted_at FROM posted_messages WHERE id = ?", (posted,)), 1.0)


class ScheduledSendTest(Case):
    async def schedule(self, client, **form):
        return await client.post(self.path("/embeds/post"), json={"channel_id": str(self.channel.id), "payload": {"content": "hi"}, "at": time.time() + 3600, **form})

    async def test_past_the_limit_the_error_names_the_number(self):
        client = await self.site(self.admin)
        await self.db.many(
            "INSERT INTO scheduled_posts (guild_id, channel_id, payload, summary, due_at, created_at) VALUES (?, ?, '{}', 's', ?, 1.0)",
            [(self.guild.id, self.channel.id, time.time() + 9000) for _ in range(embeds_module.MAX_SCHEDULED)],
        )
        response = await self.schedule(client)
        self.assertEqual(response.status, 400)
        self.assertIn(str(embeds_module.MAX_SCHEDULED), (await response.json())["fields"]["at"])

    async def test_a_slow_send_doesnt_hold_up_the_timer_loop(self):
        client = await self.site(self.admin)
        await self.schedule(client)
        release = asyncio.Event()
        original = self.channel.send

        async def slow(*args, **kwargs):
            await release.wait()
            return await original(*args, **kwargs)

        self.channel.send = slow
        with mock.patch.object(embeds_module, "SEND_WAIT_SECONDS", 0.01):
            await asyncio.wait_for(self.bot.timers.run_due(time.time() + 3601), 5)
        self.assertEqual(self.channel.sent, [])
        release.set()
        await self.embeds.finish_sends()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'embeds.scheduled'"), 0)

    async def test_a_restart_during_the_send_leaves_a_note(self):
        client = await self.site(self.admin)
        await self.schedule(client)
        release = asyncio.Event()

        async def stuck(*args, **kwargs):
            await release.wait()

        self.channel.send = stuck
        with mock.patch.object(embeds_module, "SEND_WAIT_SECONDS", 0.01):
            await self.bot.timers.run_due(time.time() + 3601)
        watch = await self.bot.timers.get_key("embeds.scheduled", "1")
        self.assertIsNotNone(watch)
        for task in self.embeds._sending.values():
            task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.embeds._sending.clear()
        await self.bot.timers.run_due(time.time() + 99999)
        rows = await (await client.get(self.path("/embeds/scheduled"))).json()
        self.assertEqual(rows[0]["state"], "failed")
        self.assertIn("may or may not", rows[0]["error"])

    async def test_a_dropped_connection_isnt_tried_again(self):
        client = await self.site(self.admin)
        await self.schedule(client)

        async def dropped(*args, **kwargs):
            raise aiohttp.ClientConnectionError("dropped")

        self.channel.send = dropped
        await self.bot.timers.run_due(time.time() + 3601)
        await self.embeds.finish_sends()
        rows = await (await client.get(self.path("/embeds/scheduled"))).json()
        self.assertEqual(rows[0]["state"], "failed")
        self.assertIn("may or may not", rows[0]["error"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'embeds.scheduled'"), 0)

    async def test_send_message_turns_a_dropped_connection_into_a_sentence(self):
        async def dropped(*args, **kwargs):
            raise OSError("dropped")

        self.channel.send = dropped
        with self.assertRaises(SendUnknown):
            await self.bot.ops.send_message(self.channel.id, {"content": "x"})


class PasteTest(Case):
    async def paste_server(self, body):
        async def handler(request):
            response = web.StreamResponse()
            await response.prepare(request)
            for start in range(0, len(body), 4096):
                await response.write(body[start:start + 4096])
                await asyncio.sleep(0)
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_get("/raw/x", handler)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        return f"http://127.0.0.1:{server.port}/raw/x"

    async def test_a_paste_that_arrives_in_pieces_is_read_whole(self):
        body = json.dumps({"content": "x" * 1500, "embeds": [{"description": "y" * 4000}] * 4}).encode()
        url = await self.paste_server(body)
        self.assertEqual(await self.embeds._fetch_paste(url), body.decode())

    async def test_too_long_a_paste_is_refused(self):
        url = await self.paste_server(b"x" * (embeds_module.MAX_PASTE_BYTES + 5))
        with self.assertRaises(Invalid):
            await self.embeds._fetch_paste(url)

    async def test_a_fenced_paste_still_reads(self):
        self.assertEqual((await self.embeds._read_json('```json\n{"content": "hi"}\n```'))["content"], "hi")
        self.assertEqual((await self.embeds._read_json('```\n{"content": "hi"}\n```'))["content"], "hi")

    async def test_a_long_run_of_whitespace_takes_no_time(self):
        started = time.perf_counter()
        with self.assertRaises(Invalid):
            await self.embeds._read_json("{" + " " * 64000 + "x")
        self.assertLess(time.perf_counter() - started, 1.0)

    async def test_deep_nesting_is_a_sentence(self):
        with self.assertRaises(Invalid):
            await self.embeds._read_json("[" * 200000)


class SavedMessagesTest(Case):
    async def test_save_list_replace_and_delete(self):
        client = await self.site(self.admin)
        response = await client.post(self.path("/embeds/saved"), json={"name": "rules", "payload": {"content": "one"}})
        self.assertEqual(response.status, 200)
        await client.post(self.path("/embeds/saved"), json={"name": "rules", "payload": {"content": "two"}})
        rows = await (await client.get(self.path("/embeds/saved"))).json()
        self.assertEqual([(row["name"], row["payload"]["content"]) for row in rows], [("rules", "two")])
        self.assertEqual((await client.delete(self.path(f"/embeds/saved/{rows[0]['id']}"))).status, 200)
        self.assertEqual(await (await client.get(self.path("/embeds/saved"))).json(), [])

    async def test_another_servers_cant_be_read_or_removed(self):
        saved = await self.db.insert("INSERT INTO saved_messages (guild_id, name, payload, saved_at) VALUES (555, 'theirs', '{}', 1.0)")
        client = await self.site(self.admin)
        self.assertEqual(await (await client.get(self.path("/embeds/saved"))).json(), [])
        self.assertEqual((await client.delete(self.path(f"/embeds/saved/{saved}"))).status, 404)

    async def test_there_is_a_limit(self):
        await self.db.many("INSERT INTO saved_messages (guild_id, name, payload, saved_at) VALUES (?, ?, '{}', 1.0)", [(self.guild.id, f"m{n}") for n in range(embeds_module.MAX_SAVED)])
        client = await self.site(self.admin)
        response = await client.post(self.path("/embeds/saved"), json={"name": "one more", "payload": {"content": "x"}})
        self.assertEqual(response.status, 400)
        replaced = await client.post(self.path("/embeds/saved"), json={"name": "m1", "payload": {"content": "x"}})
        self.assertEqual(replaced.status, 200)

    async def test_a_name_is_needed_and_something_to_keep(self):
        client = await self.site(self.admin)
        self.assertEqual((await client.post(self.path("/embeds/saved"), json={"name": " ", "payload": {"content": "x"}})).status, 400)
        self.assertEqual((await client.post(self.path("/embeds/saved"), json={"name": "x", "payload": {}})).status, 400)


class FilesTest(Case):
    def form(self, channel, *files, **extra):
        data = aiohttp.FormData()
        data.add_field("form", json.dumps({"channel_id": str(channel.id), "payload": {"content": "see this"}, **extra}))
        for name, body in files:
            data.add_field(name, body, filename=name, content_type="application/octet-stream")
        return data

    async def test_a_post_can_carry_a_file(self):
        client = await self.site(self.admin)
        response = await client.post(self.path("/embeds/post-files"), data=self.form(self.channel, ("notes.txt", b"hello")))
        self.assertEqual(response.status, 200, await response.text())
        sent = self.channel.sent[-1]
        self.assertEqual([file.filename for file in sent.kwargs["files"]], ["notes.txt"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM posted_messages WHERE guild_id = ?", (self.guild.id,)), 1)

    async def test_it_takes_the_same_checks_as_a_plain_post(self):
        news = ReadOnly(self.guild, "news", self.manager)
        client = await self.site(self.manager)
        response = await client.post(self.path("/embeds/post-files"), data=self.form(news, ("notes.txt", b"hello")))
        self.assertEqual(response.status, 400)
        self.assertEqual(news.sent, [])
        news.names = ("view_channel", "send_messages")
        response = await client.post(self.path("/embeds/post-files"), data=self.form(news, ("notes.txt", b"hello"), ping=True))
        self.assertEqual(response.status, 400)
        self.assertEqual(news.sent, [])

    async def test_a_path_in_a_name_goes(self):
        self.assertEqual(embeds_module._safe_filename("../../etc/pass wd"), "pass_wd")
        self.assertEqual(embeds_module._safe_filename("..\\x.png"), "x.png")
        self.assertEqual(embeds_module._safe_filename(""), "file")

    async def test_no_files_is_refused(self):
        client = await self.site(self.admin)
        response = await client.post(self.path("/embeds/post-files"), data=self.form(self.channel))
        self.assertEqual(response.status, 400)


class ShortFormTest(Case):
    async def test_the_short_form_posts_here_or_in_a_named_channel(self):
        other = FakeChannel(self.guild, "welcome")
        context = self.context(self.admin, self.channel)
        await self.embeds.embed.callback(self.embeds, context, text=f"<#{other.id}> FF0000 Title | Description")
        self.assertEqual(context.fails, [])
        self.assertEqual(other.sent[-1].embeds[0].title, "Title")
        await self.embeds.embed.callback(self.embeds, context, text="00FF00 Here | Too")
        self.assertEqual(self.channel.sent[-1].embeds[0].title, "Here")

    async def test_it_takes_the_same_permissions(self):
        context = self.context(self.member, self.channel)
        await self.embeds.embed.callback(self.embeds, context, text="FF0000 Title | Description")
        self.assertEqual(context.fails, ["You need Manage Server to post through the bot."])
        news = ReadOnly(self.guild, "news", self.manager)
        context = self.context(self.manager, self.channel)
        await self.embeds.embed.callback(self.embeds, context, text=f"<#{news.id}> FF0000 Title | Description")
        self.assertEqual(context.fails, ["You can't post in that channel."])

    async def test_alone_it_says_how(self):
        context = self.context(self.member, self.channel)
        await self.embeds.embed.callback(self.embeds, context)
        self.assertIn("/embed create", context.sent[-1]["content"])

    def test_the_old_typed_names_work(self):
        from captainguild.core.shortcuts import SHORTCUTS
        self.assertEqual(SHORTCUTS["cembed"], "embed custom")
        self.assertEqual(SHORTCUTS["banmsg"], "greetings banmessage")
        self.assertEqual(SHORTCUTS["welcome"], "greetings welcome")
        self.assertEqual(SHORTCUTS["leave"], "greetings farewell")


class ThreadPickerTest(Case):
    async def test_a_live_thread_is_listed_for_the_picker_with_its_type(self):
        thread = Thread(self.guild, "plans", archived=False)
        self.guild.threads = [thread]
        client = await self.site(self.admin)
        info = await (await client.get(self.path("/info"))).json()
        row = next(item for item in info["channels"] if str(item["id"]) == str(thread.id))
        self.assertEqual(row["type"], discord.ChannelType.public_thread.value)


class CommandShapeTest(Case):
    def test_threads_are_offered_as_a_destination(self):
        group = next(command for command in self.bot.tree.get_commands() if command.name == "embed")
        for command in group.commands:
            data = command.to_dict(self.bot.tree)
            option = next((o for o in data["options"] if o["name"] == "channel"), None)
            if option is not None:
                self.assertIn(discord.ChannelType.public_thread.value, option["channel_types"], command.name)
