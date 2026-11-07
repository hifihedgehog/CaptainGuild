import json
import time
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.core import messages
from captainguild.core.module import required_permissions
from captainguild.core.ops import DiscordOps, OpsError
from captainguild.core.validate import Invalid
from captainguild.modules import embeds as embeds_module
from captainguild.modules.embeds import Embeds
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, FakeMessage, new_id


class FakeAttachment:
    def __init__(self, filename, *, size=100, attachment_id=None):
        self.id = attachment_id or new_id()
        self.filename = filename
        self.size = size
        self.url = f"https://cdn.discordapp.com/attachments/1/{self.id}/{filename}?ex=1&is=2&hm=3"
        self.content_type = "image/png"

    def is_spoiler(self):
        return self.filename.startswith("SPOILER_")

    async def to_file(self, *, spoiler=False):
        return discord.File(__file__, filename=self.filename, spoiler=spoiler)


class ReactionRolesStub:
    key = "reactionroles"

    def __init__(self, *, fail=False):
        self.moves = []
        self.fail = fail

    async def move_message(self, guild_id, old_message_id, new_channel_id, new_message_id):
        if self.fail:
            raise RuntimeError("no")
        self.moves.append((guild_id, old_message_id, new_channel_id, new_message_id))

    async def holds_message(self, guild_id, message_id):
        return True


class EmbedsCase(BotTest):
    modules = [Embeds]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.cog_ = self.cog(Embeds)
        self.other_bot = FakeMember(self.guild, "Old Bot", bot=True)

    def by_other_bot(self, channel=None, content="old post", **kwargs):
        channel = channel or self.channel
        message = FakeMessage(channel, self.other_bot, content, **kwargs)
        channel.sent.append(message)
        return message

    def another_server(self):
        guild = FakeGuild("Elsewhere")
        self.bot.fake_guilds.append(guild)
        guild.role("Bot", 5, "administrator")
        guild.me = FakeMember(guild, "Bot", guild.roles[-1], bot=True)
        return guild, FakeChannel(guild, "elsewhere")

    async def post(self, client, payload=None, **form):
        body = {"channel_id": str(self.channel.id), "payload": payload or {"content": "hello"}, **form}
        return await client.post(self.path("/embeds/post"), json=body)

    async def posted_row(self, client):
        rows = (await (await client.get(self.path("/embeds/messages"))).json())["rows"]
        return rows[0]


class PostingTest(EmbedsCase):
    async def test_a_post_goes_out_and_enters_the_library_with_its_first_version(self):
        client = await self.client()
        response = await self.post(client, {"content": "", "embeds": [{"title": "Rules", "description": "Be kind", "color": 16711680}]})
        self.assertEqual(response.status, 200)
        result = await response.json()
        sent = self.channel.sent[-1]
        self.assertEqual(str(sent.id), result["message_id"])
        self.assertEqual(sent.embeds[0].title, "Rules")
        row = await self.posted_row(client)
        self.assertEqual(row["summary"], "Rules")
        self.assertEqual(row["posted_by_name"], "admin")
        self.assertEqual(row["version"], 1)
        self.assertEqual(row["url"], f"https://discord.com/channels/{self.guild.id}/{self.channel.id}/{sent.id}")
        activity = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(activity[0]["action"], "embed.post")

    async def test_discords_real_description_limit_is_the_only_limit(self):
        client = await self.client()
        response = await self.post(client, {"embeds": [{"description": "x" * 4096}]})
        self.assertEqual(response.status, 200)
        response = await self.post(client, {"embeds": [{"description": "x" * 4097}]})
        self.assertEqual(response.status, 400)
        self.assertIn("payload", (await response.json())["fields"])
        response = await self.post(client, {"embeds": [{"description": "x" * 4000}, {"description": "y" * 2001}]})
        self.assertEqual(response.status, 400)

    async def test_an_empty_post_is_refused(self):
        client = await self.client()
        response = await self.post(client, {"content": "", "embeds": [{}], "buttons": []})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.channel.sent, [])

    async def test_another_servers_channel_is_refused(self):
        _, elsewhere = self.another_server()
        client = await self.client()
        response = await client.post(self.path("/embeds/post"), json={"channel_id": str(elsewhere.id), "payload": {"content": "hi"}})
        self.assertEqual(response.status, 400)
        self.assertIn("channel_id", (await response.json())["fields"])
        self.assertEqual(elsewhere.sent, [])

    async def test_pings_stay_off_unless_asked_for(self):
        client = await self.client()
        await self.post(client, {"content": "@everyone hi"})
        self.assertFalse(self.channel.sent[-1].kwargs["allowed_mentions"].everyone)
        await self.post(client, {"content": "@everyone hi"}, ping=True)
        self.assertTrue(self.channel.sent[-1].kwargs["allowed_mentions"].everyone)

    async def test_staff_access_is_refused(self):
        client = await self.client(access="staff")
        self.assertEqual((await self.post(client)).status, 403)
        self.assertEqual((await client.get(self.path("/embeds/messages"))).status, 403)

    async def test_search_reaches_inside_embeds_and_stays_in_this_server(self):
        client = await self.client()
        await self.post(client, {"embeds": [{"title": "A", "fields": [{"name": "Findme_50%", "value": "v"}]}]})
        await self.post(client, {"content": "Other"})
        rows = (await (await client.get(self.path("/embeds/messages?q=findme_50%25"))).json())["rows"]
        self.assertEqual([row["summary"] for row in rows], ["A"])
        rows = (await (await client.get(self.path("/embeds/messages?q=fi_dme"))).json())["rows"]
        self.assertEqual(rows, [])
        guild, _ = self.another_server()
        await self.cog_.record_post(guild.id, 1, 2, {"content": "Other server", "embeds": [], "buttons": []})
        rows = (await (await client.get(self.path("/embeds/messages"))).json())["rows"]
        self.assertEqual(len(rows), 2)


class EditingTest(EmbedsCase):
    async def test_lookup_reads_a_link_and_knows_who_wrote_it(self):
        client = await self.client()
        await self.post(client)
        mine = self.channel.sent[-1]
        theirs = self.by_other_bot()
        body = await (await client.get(self.path(f"/embeds/lookup?link=https://discord.com/channels/{self.guild.id}/{self.channel.id}/{mine.id}"))).json()
        self.assertTrue(body["message"]["mine"])
        self.assertEqual(body["library"]["version"], 1)
        body = await (await client.get(self.path(f"/embeds/lookup?link=https://ptb.discord.com/channels/{self.guild.id}/{self.channel.id}/{theirs.id}"))).json()
        self.assertFalse(body["message"]["mine"])
        self.assertEqual(body["message"]["author_name"], "Old Bot")
        self.assertIsNone(body["library"])

    async def test_lookup_refuses_bad_links_and_other_servers(self):
        guild, elsewhere = self.another_server()
        client = await self.client()
        self.assertEqual((await client.get(self.path("/embeds/lookup?link=nonsense"))).status, 400)
        response = await client.get(self.path(f"/embeds/lookup?link=https://discord.com/channels/{guild.id}/{elsewhere.id}/123456789012345678"))
        self.assertEqual(response.status, 400)
        response = await client.get(self.path(f"/embeds/lookup?link=https://discord.com/channels/{self.guild.id}/{elsewhere.id}/123456789012345678"))
        self.assertEqual(response.status, 400)

    async def edit(self, client, message, payload, **form):
        return await client.patch(self.path("/embeds/message"), json={"channel_id": str(self.channel.id), "message_id": str(message.id), "payload": payload, **form})

    async def test_an_edit_changes_the_message_and_adds_a_version(self):
        client = await self.client()
        await self.post(client, {"content": "one"})
        message = self.channel.sent[-1]
        response = await self.edit(client, message, {"content": "two"})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual((body["changed"], body["version"]), (True, 2))
        self.assertEqual(message.content, "two")
        row = await self.posted_row(client)
        self.assertEqual(row["summary"], "two")
        self.assertIsNotNone(row["edited_at"])

    async def test_saving_the_same_text_adds_nothing(self):
        client = await self.client()
        await self.post(client, {"content": "one"})
        response = await self.edit(client, self.channel.sent[-1], {"content": "one"})
        self.assertEqual((await response.json())["changed"], False)
        self.assertEqual((await self.posted_row(client))["version"], 1)

    async def test_the_first_edit_of_a_message_posted_elsewhere_saves_what_it_said(self):
        client = await self.client()
        message = FakeMessage(self.channel, self.me, "written by another feature")
        self.channel.sent.append(message)
        response = await self.edit(client, message, {"content": "changed"})
        self.assertEqual(response.status, 200)
        history = await (await client.get(self.path(f"/embeds/messages/{(await response.json())['id']}"))).json()
        self.assertEqual([v["payload"]["content"] for v in history["versions"]], ["changed", "written by another feature"])

    async def test_another_bots_message_cant_be_edited_and_the_reason_is_clear(self):
        client = await self.client()
        theirs = self.by_other_bot()
        response = await self.edit(client, theirs, {"content": "changed"})
        self.assertEqual(response.status, 502)
        self.assertIn("Old Bot", (await response.json())["error"])
        self.assertEqual(theirs.content, "old post")

    async def test_a_stale_editor_is_told_and_can_overwrite(self):
        client = await self.client()
        await self.post(client, {"content": "one"})
        message = self.channel.sent[-1]
        await self.edit(client, message, {"content": "two"}, version=1)
        response = await self.edit(client, message, {"content": "three"}, version=1)
        self.assertEqual(response.status, 409)
        self.assertEqual(message.content, "two")
        response = await self.edit(client, message, {"content": "three"}, version=1, force=True)
        self.assertEqual(response.status, 200)
        self.assertEqual(message.content, "three")

    async def test_editing_a_deleted_message_marks_it_deleted(self):
        client = await self.client()
        await self.post(client, {"content": "one"})
        message = self.channel.sent[-1]
        self.channel.sent.remove(message)
        response = await self.edit(client, message, {"content": "two"})
        self.assertEqual(response.status, 502)
        self.assertIsNotNone((await self.posted_row(client))["deleted_at"])

    async def test_revert_puts_old_text_back_and_keeps_the_history(self):
        client = await self.client()
        await self.post(client, {"content": "one"})
        message = self.channel.sent[-1]
        await self.edit(client, message, {"content": "two"})
        row = await self.posted_row(client)
        response = await client.post(self.path(f"/embeds/messages/{row['id']}/revert"), json={"number": 1})
        self.assertEqual(response.status, 200)
        self.assertEqual(message.content, "one")
        history = await (await client.get(self.path(f"/embeds/messages/{row['id']}"))).json()
        self.assertEqual([(v["number"], v["note"]) for v in history["versions"]], [(3, "Reverted to version 1"), (2, "Edited"), (1, "Posted")])

    async def test_revert_refuses_an_unknown_version_and_another_servers_row(self):
        client = await self.client()
        await self.post(client)
        row = await self.posted_row(client)
        self.assertEqual((await client.post(self.path(f"/embeds/messages/{row['id']}/revert"), json={"number": 9})).status, 404)
        self.assertEqual((await client.post(self.path(f"/embeds/messages/{row['id']}/revert"), json={"number": "1"})).status, 404)
        self.assertEqual((await client.post(self.path(f"/embeds/messages/{row['id']}/revert"), json={"number": 10 ** 30})).status, 404)
        guild, _ = self.another_server()
        theirs = await self.cog_.record_post(guild.id, 1, 2, {"content": "x", "embeds": [], "buttons": []})
        self.assertEqual((await client.get(self.path(f"/embeds/messages/{theirs}"))).status, 404)
        self.assertEqual((await client.post(self.path(f"/embeds/messages/{theirs}/revert"), json={"number": 1})).status, 404)
        self.assertEqual((await client.delete(self.path(f"/embeds/messages/{theirs}"))).status, 404)

    async def test_old_versions_are_trimmed(self):
        client = await self.client()
        await self.post(client, {"content": "0"})
        message = self.channel.sent[-1]
        with mock.patch.object(embeds_module, "MAX_VERSIONS", 3):
            for number in range(1, 6):
                await self.edit(client, message, {"content": str(number)})
        row = await self.posted_row(client)
        history = await (await client.get(self.path(f"/embeds/messages/{row['id']}"))).json()
        self.assertEqual([v["number"] for v in history["versions"]], [6, 5, 4])


class DeletionTest(EmbedsCase):
    async def test_a_deleted_message_is_marked_and_can_come_back_with_its_history(self):
        client = await self.client()
        await self.post(client, {"content": "one"})
        message = self.channel.sent[-1]
        await self.cog_.on_raw_message_delete(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_id=message.id))
        row = await self.posted_row(client)
        self.assertIsNotNone(row["deleted_at"])
        response = await client.post(self.path(f"/embeds/messages/{row['id']}/repost"))
        self.assertEqual(response.status, 200)
        fresh = await self.posted_row(client)
        self.assertIsNone(fresh["deleted_at"])
        self.assertNotEqual(fresh["message_id"], str(message.id))
        self.assertEqual(fresh["version"], 2)

    async def test_a_message_still_there_cant_be_reposted(self):
        client = await self.client()
        await self.post(client)
        row = await self.posted_row(client)
        self.assertEqual((await client.post(self.path(f"/embeds/messages/{row['id']}/repost"))).status, 400)

    async def test_bulk_and_channel_deletes_mark_messages(self):
        client = await self.client()
        await self.post(client, {"content": "one"})
        await self.post(client, {"content": "two"})
        ids = {m.id for m in self.channel.sent}
        await self.cog_.on_raw_bulk_message_delete(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_ids=ids))
        rows = (await (await client.get(self.path("/embeds/messages"))).json())["rows"]
        self.assertTrue(all(row["deleted_at"] for row in rows))
        await self.db.run("UPDATE posted_messages SET deleted_at = NULL")
        await self.cog_.on_guild_channel_delete(self.channel)
        rows = (await (await client.get(self.path("/embeds/messages"))).json())["rows"]
        self.assertTrue(all(row["deleted_at"] for row in rows))

    async def test_delete_removes_the_post_and_its_history(self):
        client = await self.client()
        await self.post(client)
        message = self.channel.sent[-1]
        row = await self.posted_row(client)
        self.assertEqual((await client.delete(self.path(f"/embeds/messages/{row['id']}"))).status, 200)
        self.assertTrue(message.deleted)
        self.assertEqual((await (await client.get(self.path("/embeds/messages"))).json())["rows"], [])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM posted_versions"), 0)


class SchedulingTest(EmbedsCase):
    async def schedule(self, client, at=None, **form):
        return await self.post(client, at=at or time.time() + 3600, **form)

    async def test_a_scheduled_post_waits_then_goes_out_once(self):
        client = await self.client()
        response = await self.schedule(client)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 1)
        self.assertEqual(self.channel.sent[-1].content, "hello")
        self.assertEqual(await (await client.get(self.path("/embeds/scheduled"))).json(), [])
        row = await self.posted_row(client)
        self.assertEqual(row["source"], "schedule")
        self.assertEqual(row["posted_by_name"], "admin")
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 0)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_the_time_must_be_ahead_and_within_a_year(self):
        client = await self.client()
        self.assertEqual((await self.schedule(client, at=time.time() - 10)).status, 400)
        self.assertEqual((await self.schedule(client, at=time.time() + 400 * 86400)).status, 400)
        self.assertEqual((await self.post(client, at="soon")).status, 400)

    async def test_a_refused_post_is_tried_again_and_then_reported(self):
        client = await self.client()
        await self.schedule(client)
        self.channel.guild.channels.remove(self.channel)
        now = time.time()
        with self.assertLogs("captainguild.modules.embeds", level="WARNING"):
            for attempt in range(4):
                now += 7200
                await self.bot.timers.run_due(now)
        rows = await (await client.get(self.path("/embeds/scheduled"))).json()
        self.assertEqual(rows[0]["state"], "failed")
        self.assertIn("no longer exists", rows[0]["error"])
        self.assertEqual(await self.bot.timers.run_due(now + 99999), 0)
        self.guild.channels.append(self.channel)
        response = await client.post(self.path(f"/embeds/scheduled/{rows[0]['id']}/post"))
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(await (await client.get(self.path("/embeds/scheduled"))).json(), [])

    async def test_a_restart_in_the_middle_of_posting_doesnt_post_twice(self):
        client = await self.client()
        await self.schedule(client)
        await self.db.run("UPDATE scheduled_posts SET state = 'posting'")
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertEqual(self.channel.sent, [])
        rows = await (await client.get(self.path("/embeds/scheduled"))).json()
        self.assertEqual(rows[0]["state"], "failed")
        self.assertIn("restarted", rows[0]["error"])

    async def test_cancel_stops_it(self):
        client = await self.client()
        await self.schedule(client)
        rows = await (await client.get(self.path("/embeds/scheduled"))).json()
        self.assertEqual((await client.delete(self.path(f"/embeds/scheduled/{rows[0]['id']}"))).status, 200)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 0)
        self.assertEqual(self.channel.sent, [])

    async def test_editing_moves_the_timer_and_changes_the_text(self):
        client = await self.client()
        await self.schedule(client)
        rows = await (await client.get(self.path("/embeds/scheduled"))).json()
        later = time.time() + 7200
        response = await client.patch(self.path(f"/embeds/scheduled/{rows[0]['id']}"), json={"at": later, "payload": {"content": "changed"}})
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 0)
        self.assertEqual(await self.bot.timers.run_due(later + 1), 1)
        self.assertEqual(self.channel.sent[-1].content, "changed")

    async def test_another_servers_schedule_cant_be_touched(self):
        client = await self.client()
        await self.schedule(client)
        rows = await (await client.get(self.path("/embeds/scheduled"))).json()
        guild, _ = self.another_server()
        await self.db.run("UPDATE scheduled_posts SET guild_id = ?", (guild.id,))
        self.assertEqual(await (await client.get(self.path("/embeds/scheduled"))).json(), [])
        self.assertEqual((await client.delete(self.path(f"/embeds/scheduled/{rows[0]['id']}"))).status, 404)
        self.assertEqual((await client.post(self.path(f"/embeds/scheduled/{rows[0]['id']}/post"))).status, 404)


class AdoptingTest(EmbedsCase):
    async def adopt(self, client, picked, **form):
        body = {"messages": [{"channel_id": str(m.channel.id), "message_id": str(m.id)} for m in picked], **form}
        response = await client.post(self.path("/embeds/adoptions"), json=body)
        await self.cog_.finish_jobs()
        return response

    async def job(self, client, response):
        return await (await client.get(self.path(f"/embeds/adoptions/{(await response.json())['id']}"))).json()

    def bot_posts(self):
        return [m for m in self.channel.sent if m.author is self.me]

    async def test_messages_go_out_in_order_with_their_text_and_embeds(self):
        client = await self.client()
        first = self.by_other_bot(content="first", embeds=[discord.Embed(title="One", color=5)])
        second = self.by_other_bot(content="second")
        response = await self.adopt(client, [second, first])
        self.assertEqual(response.status, 200)
        job = await self.job(client, response)
        self.assertEqual(job["state"], "done")
        self.assertEqual([item["state"] for item in job["items"]], ["done", "done"])
        posts = self.bot_posts()
        self.assertEqual([p.content for p in posts], ["first", "second"])
        self.assertEqual(posts[0].embeds[0].title, "One")
        self.assertEqual(posts[0].embeds[0].color.value, 5)
        rows = (await (await client.get(self.path("/embeds/messages"))).json())["rows"]
        self.assertEqual({row["adopted_from"] for row in rows}, {"Old Bot"})
        self.assertEqual({row["source"] for row in rows}, {"adopted"})
        self.assertIn(first, self.channel.sent)

    async def test_a_repost_pings_nobody(self):
        client = await self.client()
        await self.adopt(client, [self.by_other_bot(content="@everyone <@123>")])
        mentions = self.bot_posts()[0].kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.roles)

    async def test_originals_go_only_when_asked(self):
        client = await self.client()
        keep = self.by_other_bot(content="keep")
        await self.adopt(client, [keep])
        self.assertFalse(keep.deleted)
        gone = self.by_other_bot(content="gone")
        await self.adopt(client, [gone], delete_originals=True)
        self.assertTrue(gone.deleted)
        self.assertEqual(self.bot_posts()[-1].content, "gone")

    async def test_another_channel_can_take_the_copies(self):
        target = FakeChannel(self.guild, "new-home")
        client = await self.client()
        original = self.by_other_bot()
        response = await self.adopt(client, [original], channel_id=str(target.id))
        self.assertEqual(target.sent[0].content, "old post")
        self.assertEqual((await self.job(client, response))["items"][0]["new_channel_id"], str(target.id))

    async def test_reaction_roles_move_along_with_the_right_ids(self):
        stub = ReactionRolesStub()
        self.bot.module = lambda key: stub if key == "reactionroles" else None
        client = await self.client()
        original = self.by_other_bot()
        await self.adopt(client, [original], delete_originals=True)
        copy = self.bot_posts()[0]
        self.assertEqual(stub.moves, [(self.guild.id, original.id, self.channel.id, copy.id)])
        self.assertTrue(original.deleted)

    async def test_when_reaction_roles_cant_move_the_original_stays(self):
        self.bot.module = lambda key: ReactionRolesStub(fail=True) if key == "reactionroles" else None
        client = await self.client()
        original = self.by_other_bot()
        with self.assertLogs("captainguild.modules.embeds", level="ERROR"):
            response = await self.adopt(client, [original], delete_originals=True)
        job = await self.job(client, response)
        self.assertFalse(original.deleted)
        self.assertEqual(job["state"], "done")
        self.assertIn("reaction roles", job["items"][0]["note"])

    async def test_reactions_are_copied_when_asked(self):
        client = await self.client()
        original = self.by_other_bot()
        original.reactions = [SimpleNamespace(emoji="\N{THUMBS UP SIGN}", count=4, me=False)]
        await self.adopt(client, [original], copy_reactions=True)
        self.assertEqual(self.bot_posts()[0].added_reactions, ["\N{THUMBS UP SIGN}"])

    async def test_a_failure_stops_the_line_and_retry_carries_on_in_order(self):
        client = await self.client()
        one, two, three = self.by_other_bot(content="1"), self.by_other_bot(content="2"), self.by_other_bot(content="3")
        real = self.bot.ops.repost_message
        calls = []

        async def flaky(source_channel_id, source_message_id, channel_id, payload, *, suppress_embeds=False):
            calls.append(source_message_id)
            if source_message_id == two.id and calls.count(two.id) == 1:
                raise OpsError("Discord said no.")
            return await real(source_channel_id, source_message_id, channel_id, payload, suppress_embeds=suppress_embeds)

        self.bot.ops.repost_message = flaky
        response = await self.adopt(client, [one, two, three])
        job = await self.job(client, response)
        self.assertEqual(job["state"], "stopped")
        self.assertEqual([item["state"] for item in job["items"]], ["done", "failed", "waiting"])
        self.assertEqual(job["items"][1]["note"], "Discord said no.")
        self.assertEqual([p.content for p in self.bot_posts()], ["1"])
        response = await client.post(self.path(f"/embeds/adoptions/{job['id']}/resume"))
        self.assertEqual(response.status, 200)
        await self.cog_.finish_jobs()
        self.assertEqual([p.content for p in self.bot_posts()], ["1", "2", "3"])

    async def test_skip_leaves_the_failed_one_out(self):
        client = await self.client()
        one, two = self.by_other_bot(content="1"), self.by_other_bot(content="2")
        self.channel.sent.remove(one)  # it can't be read, as if someone deleted it first
        response = await self.adopt(client, [one, two])
        job = await self.job(client, response)
        self.assertEqual(job["items"][0]["state"], "failed")
        await client.post(self.path(f"/embeds/adoptions/{job['id']}/resume"), json={"skip": True})
        await self.cog_.finish_jobs()
        job = await (await client.get(self.path(f"/embeds/adoptions/{job['id']}"))).json()
        self.assertEqual([item["state"] for item in job["items"]], ["skipped", "done"])
        self.assertEqual([p.content for p in self.bot_posts()], ["2"])

    async def test_a_restart_mid_send_never_posts_twice(self):
        client = await self.client()
        original = self.by_other_bot()
        await self.db.run("INSERT INTO adopt_jobs (id, guild_id, delete_originals, copy_reactions, state, created_at) VALUES (1, ?, 0, 0, 'running', 0)", (self.guild.id,))
        await self.db.run(
            "INSERT INTO adopt_items (guild_id, job_id, position, source_channel_id, source_message_id, author_name, summary, state) VALUES (?, 1, 0, ?, ?, '', '', 'posting')",
            (self.guild.id, self.channel.id, original.id),
        )
        await self.cog_.on_ready()
        await self.cog_.finish_jobs()
        job = await (await client.get(self.path("/embeds/adoptions/1"))).json()
        self.assertEqual(job["state"], "stopped")
        self.assertEqual(job["items"][0]["state"], "failed")
        self.assertIn("restarted", job["items"][0]["note"])
        self.assertEqual(self.bot_posts(), [])

    async def test_a_restart_after_the_send_finishes_without_posting_again(self):
        await self.client()
        original = self.by_other_bot()
        await self.db.run("INSERT INTO adopt_jobs (id, guild_id, delete_originals, copy_reactions, state, created_at) VALUES (1, ?, 1, 0, 'running', 0)", (self.guild.id,))
        await self.db.run(
            "INSERT INTO adopt_items (guild_id, job_id, position, source_channel_id, source_message_id, author_name, summary, state, new_channel_id, new_message_id) VALUES (?, 1, 0, ?, ?, '', '', 'posted', ?, 5)",
            (self.guild.id, self.channel.id, original.id, self.channel.id),
        )
        await self.cog_.on_ready()
        await self.cog_.finish_jobs()
        self.assertEqual(self.bot_posts(), [])
        self.assertTrue(original.deleted)
        self.assertEqual(await self.db.scalar("SELECT state FROM adopt_jobs WHERE id = 1"), "done")

    async def test_the_bots_own_message_is_skipped(self):
        client = await self.client()
        mine = FakeMessage(self.channel, self.me, "mine")
        self.channel.sent.append(mine)
        response = await self.adopt(client, [mine])
        job = await self.job(client, response)
        self.assertEqual(job["items"][0]["state"], "skipped")
        self.assertEqual(len(self.channel.sent), 1)

    async def test_files_are_carried_and_embed_images_point_at_them(self):
        client = await self.client()
        stored = FakeAttachment("banner.png")
        too_big = FakeAttachment("movie.mp4", size=10 ** 12)
        embed = discord.Embed(title="With image")
        embed.set_image(url=stored.url)
        embed.set_thumbnail(url="https://example.com/t.png?keep=1")
        original = self.by_other_bot(embeds=[embed], attachments=[stored, too_big])
        self.guild.filesize_limit = 25 * 1024 * 1024
        response = await self.adopt(client, [original])
        job = await self.job(client, response)
        copy = self.bot_posts()[0]
        self.assertEqual([f.filename for f in copy.kwargs["files"]], ["banner.png"])
        self.assertEqual(copy.embeds[0].image.url, "attachment://banner.png")
        self.assertEqual(copy.embeds[0].thumbnail.url, "https://example.com/t.png?keep=1")
        self.assertIn("movie.mp4", job["items"][0]["note"])

    async def test_a_message_that_is_only_files_is_described_by_its_file(self):
        client = await self.client()
        original = self.by_other_bot(content="", attachments=[FakeAttachment("photo.png")])
        self.guild.filesize_limit = 25 * 1024 * 1024
        body = await (await client.get(self.path(f"/embeds/channel-messages?channel_id={self.channel.id}"))).json()
        row = next(r for r in body["messages"] if r["id"] == str(original.id))
        self.assertEqual((row["summary"], row["empty"]), ("photo.png", False))
        response = await self.adopt(client, [original])
        job = await self.job(client, response)
        self.assertEqual((job["items"][0]["summary"], job["items"][0]["state"]), ("photo.png", "done"))
        self.assertEqual([f.filename for f in self.bot_posts()[0].kwargs["files"]], ["photo.png"])

    async def test_input_is_checked(self):
        _, elsewhere = self.another_server()
        client = await self.client()
        original = self.by_other_bot()
        self.assertEqual((await client.post(self.path("/embeds/adoptions"), json={"messages": []})).status, 400)
        response = await client.post(self.path("/embeds/adoptions"), json={"messages": [{"channel_id": str(elsewhere.id), "message_id": str(original.id)}]})
        self.assertEqual(response.status, 400)
        response = await client.post(self.path("/embeds/adoptions"), json={"messages": [{"channel_id": str(self.channel.id), "message_id": "nope"}]})
        self.assertEqual(response.status, 400)
        response = await client.post(self.path("/embeds/adoptions"), json={"messages": [{"channel_id": str(self.channel.id), "message_id": str(original.id)}], "channel_id": str(elsewhere.id)})
        self.assertEqual(response.status, 400)
        too_many = [{"channel_id": str(self.channel.id), "message_id": str(new_id())} for _ in range(101)]
        self.assertEqual((await client.post(self.path("/embeds/adoptions"), json={"messages": too_many})).status, 400)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM adopt_jobs"), 0)

    async def test_another_servers_job_is_out_of_reach(self):
        client = await self.client()
        response = await self.adopt(client, [self.by_other_bot()])
        job_id = (await response.json())["id"]
        guild, _ = self.another_server()
        await self.db.run("UPDATE adopt_jobs SET guild_id = ?", (guild.id,))
        self.assertEqual((await client.get(self.path(f"/embeds/adoptions/{job_id}"))).status, 404)
        self.assertEqual((await client.post(self.path(f"/embeds/adoptions/{job_id}/stop"))).status, 404)
        self.assertEqual((await client.post(self.path(f"/embeds/adoptions/{job_id}/resume"))).status, 404)
        self.assertEqual(await (await client.get(self.path("/embeds/adoptions"))).json(), [])

    async def test_a_channel_listing_marks_what_was_already_reposted(self):
        client = await self.client()
        original = self.by_other_bot(content="old")
        fresh = self.by_other_bot(content="newer")
        await self.adopt(client, [original])
        body = await (await client.get(self.path(f"/embeds/channel-messages?channel_id={self.channel.id}"))).json()
        rows = {row["id"]: row for row in body["messages"]}
        self.assertIsNotNone(rows[str(original.id)]["adopted_url"])
        self.assertIsNone(rows[str(fresh.id)]["adopted_url"])
        self.assertEqual(rows[str(fresh.id)]["summary"], "newer")
        self.assertTrue(body["can_delete"])

    async def test_a_channel_listing_refuses_another_server(self):
        _, elsewhere = self.another_server()
        client = await self.client()
        self.assertEqual((await client.get(self.path(f"/embeds/channel-messages?channel_id={elsewhere.id}"))).status, 400)

    async def test_paging_back_through_a_channel(self):
        client = await self.client()
        older = self.by_other_bot(content="older")
        newer = self.by_other_bot(content="newer")
        body = await (await client.get(self.path(f"/embeds/channel-messages?channel_id={self.channel.id}&before={newer.id}"))).json()
        self.assertEqual([row["id"] for row in body["messages"]], [str(older.id)])


class CommandTest(EmbedsCase):
    async def test_create_posts_with_color_title_and_description(self):
        ctx = self.context()
        target = FakeChannel(self.guild, "news")
        await self.cog_.embed_create.callback(self.cog_, ctx, "FF0000", text="Title | Description", channel=target)
        embed = target.sent[-1].embeds[0]
        self.assertEqual((embed.title, embed.description, embed.color.value), ("Title", "Description", 0xFF0000))
        self.assertTrue(ctx.oks)
        row = await self.db.one("SELECT source, posted_by_name FROM posted_messages")
        self.assertEqual(row, {"source": "command", "posted_by_name": "admin"})

    async def test_create_takes_a_channel_from_the_end_of_the_text(self):
        target = FakeChannel(self.guild, "news")
        ctx = self.context()
        await self.cog_.embed_create.callback(self.cog_, ctx, "FF0000", text=f"Title | Description {target.mention}")
        self.assertEqual(target.sent[-1].embeds[0].description, "Description")

    async def test_create_refuses_a_bad_color_and_nothing_at_all(self):
        ctx = self.context()
        await self.cog_.embed_create.callback(self.cog_, ctx, "red", text="Title")
        await self.cog_.embed_create.callback(self.cog_, ctx, "FF0000", text=" ")
        self.assertEqual(len(ctx.fails), 2)
        self.assertEqual(self.channel.sent, [])

    async def test_edit_keeps_color_and_fields(self):
        ctx = self.context()
        await self.cog_.embed_custom.callback(self.cog_, ctx, json=json.dumps({"title": "Old", "description": "d", "color": 5, "fields": [{"name": "n", "value": "v"}], "url": "https://example.com"}))
        message = self.channel.sent[-1]
        await self.cog_.embed_edit.callback(self.cog_, ctx, str(message.id), text="New title | New description")
        embed = message.embeds[0]
        self.assertEqual((embed.title, embed.description, embed.color.value, embed.fields[0].name), ("New title", "New description", 5, "n"))
        self.assertEqual(embed.url, "https://example.com")
        await self.cog_.embed_edit.callback(self.cog_, ctx, str(message.id), text="Just a description")
        self.assertEqual(message.embeds[0].title, None)
        self.assertEqual(message.embeds[0].url, None)

    async def test_edit_finds_a_library_message_from_another_channel_by_id(self):
        target = FakeChannel(self.guild, "news")
        ctx = self.context()
        await self.cog_.embed_custom.callback(self.cog_, ctx, json='{"title": "Old"}', channel=target)
        message = target.sent[-1]
        await self.cog_.embed_edit.callback(self.cog_, ctx, str(message.id), text="New")
        self.assertEqual(message.embeds[0].description, "New")

    async def test_edit_refuses_bad_references_and_other_servers(self):
        guild, elsewhere = self.another_server()
        ctx = self.context()
        await self.cog_.embed_edit.callback(self.cog_, ctx, "nonsense", text="New")
        await self.cog_.embed_edit.callback(self.cog_, ctx, f"https://discord.com/channels/{guild.id}/{elsewhere.id}/123456789012345678", text="New")
        await self.cog_.embed_edit.callback(self.cog_, ctx, "123456789012345678", text="New")
        await self.cog_.embed_edit.callback(self.cog_, ctx, "99999999999999999999", text="Too big for an ID")
        self.assertEqual(len(ctx.fails), 4)

    async def test_custom_takes_a_whole_message_or_a_paste_link(self):
        ctx = self.context()
        await self.cog_.embed_custom.callback(self.cog_, ctx, json='{"content": "hi", "embeds": [{"title": "T"}]}')
        self.assertEqual((self.channel.sent[-1].content, self.channel.sent[-1].embeds[0].title), ("hi", "T"))
        with mock.patch.object(self.cog_, "_fetch_paste", mock.AsyncMock(return_value='{"title": "Pasted"}')) as fetch:
            await self.cog_.embed_custom.callback(self.cog_, ctx, json="https://pastebin.com/abc123")
        fetch.assert_awaited_once_with("https://pastebin.com/raw/abc123")
        self.assertEqual(self.channel.sent[-1].embeds[0].title, "Pasted")

    async def test_only_known_paste_sites_are_fetched(self):
        ctx = self.context()
        with mock.patch.object(self.cog_, "_fetch_paste", mock.AsyncMock()) as fetch:
            await self.cog_.embed_custom.callback(self.cog_, ctx, json="https://evil.example/pastebin.com/abc")
        fetch.assert_not_awaited()
        self.assertTrue(ctx.fails)

    async def test_bad_json_is_refused(self):
        ctx = self.context()
        await self.cog_.embed_custom.callback(self.cog_, ctx, json="{not json")
        await self.cog_.embed_custom.callback(self.cog_, ctx, json='"just text"')
        await self.cog_.embed_custom.callback(self.cog_, ctx, json="{}")
        self.assertEqual(len(ctx.fails), 3)
        self.assertEqual(self.channel.sent, [])

    async def test_editcustom_replaces_the_message(self):
        ctx = self.context()
        await self.cog_.embed_custom.callback(self.cog_, ctx, json='{"title": "Old"}')
        message = self.channel.sent[-1]
        await self.cog_.embed_editcustom.callback(self.cog_, ctx, str(message.id), json='{"title": "New"}')
        self.assertEqual(message.embeds[0].title, "New")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM posted_versions"), 2)

    async def test_editcustom_refuses_another_bots_message(self):
        ctx = self.context()
        theirs = self.by_other_bot()
        await self.cog_.embed_editcustom.callback(self.cog_, ctx, str(theirs.id), json='{"title": "New"}')
        self.assertIn("Old Bot", ctx.fails[0])
        self.assertEqual(theirs.content, "old post")

    async def test_source_gives_json_that_custom_takes_back(self):
        ctx = self.context()
        theirs = self.by_other_bot(content="", embeds=[discord.Embed(title="Their embed", color=7)])
        await self.cog_.embed_source.callback(self.cog_, ctx, str(theirs.id))
        text = ctx.sent[-1]["content"]
        self.assertIn('"title": "Their embed"', text)
        self.assertEqual(messages.from_json(json.loads(text.removeprefix("```json\n").removesuffix("\n```")))["embeds"][0]["color"], 7)

    async def test_source_sends_a_file_when_its_long(self):
        ctx = self.context()
        theirs = self.by_other_bot(content="x" * 1990)
        await self.cog_.embed_source.callback(self.cog_, ctx, str(theirs.id))
        self.assertEqual(ctx.sent[-1]["file"].filename, "message.json")

    async def test_source_wont_read_out_a_channel_the_person_cant_read(self):
        hidden = FakeChannel(self.guild, "staff")
        secret = self.by_other_bot(channel=hidden, content="secret")
        hidden.permissions_for = lambda member: SimpleNamespace(read_message_history=False, view_channel=False)
        ctx = self.context(author=self.member)
        await self.cog_.embed_source.callback(self.cog_, ctx, str(secret.id), channel=hidden)
        self.assertTrue(ctx.fails)
        self.assertEqual(ctx.sent, [])

    async def test_unhide_names_a_permission_only_when_that_is_the_refusal(self):
        for status, expected in ((403, "Manage Messages"), (503, "Discord refused")):
            message = self.by_other_bot()

            async def refuse(**kwargs):
                response = mock.Mock(status=status, reason="no")
                raise (discord.Forbidden if status == 403 else discord.HTTPException)(response, "no")
            message.edit = refuse
            ctx = self.context()
            await self.cog_.embed_unhide.callback(self.cog_, ctx, str(message.id))
            self.assertIn(expected, ctx.fails[0])

    async def test_unhide_brings_embeds_back(self):
        message = self.by_other_bot()
        ctx = self.context()
        await self.cog_.embed_unhide.callback(self.cog_, ctx, str(message.id))
        self.assertIs(message.kwargs["suppress"], False)
        await self.cog_.embed_unhide.callback(self.cog_, ctx, "123456789012345678")
        self.assertTrue(ctx.fails)

    async def test_who_may_use_what(self):
        for command in (self.cog_.embed_create, self.cog_.embed_edit, self.cog_.embed_custom, self.cog_.embed_editcustom, self.cog_.embed_unhide):
            self.assertEqual(required_permissions(command), ("manage_guild",))
        self.assertEqual(required_permissions(self.cog_.embed_source), ())


class MenuTest(EmbedsCase):
    async def run_menu(self, member, message):
        answers = []

        async def send_message(content, **kwargs):
            answers.append((content, kwargs))

        interaction = SimpleNamespace(user=member, guild_id=self.guild.id, response=SimpleNamespace(send_message=send_message))
        await self.cog_.edit_message_menu(interaction, message)
        return answers[0]

    async def test_it_gives_a_private_link_to_the_editor(self):
        message = self.by_other_bot()
        content, kwargs = await self.run_menu(self.admin, message)
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn(f"https://bot.example/dashboard/{self.guild.id}/embeds?edit=https%3A%2F%2Fdiscord.com%2Fchannels%2F", content)

    async def test_it_refuses_without_manage_server(self):
        content, kwargs = await self.run_menu(self.member, self.by_other_bot())
        self.assertTrue(kwargs["ephemeral"])
        self.assertNotIn("dashboard", content)

    async def test_it_is_registered_for_servers_with_manage_server(self):
        menu = next(c for c in self.bot.tree.get_commands() if c.name == "Edit Message")
        self.assertEqual(menu.type, discord.AppCommandType.message)
        self.assertTrue(menu.default_permissions.manage_guild)
        self.cog_.cog_unload()
        self.assertFalse([c for c in self.bot.tree.get_commands() if c.name == "Edit Message"])


class MessageFormTest(BotTest):
    async def test_signed_discord_links_lose_their_signature(self):
        link = "https://cdn.discordapp.com/attachments/1/2/a.png?ex=1&is=2&hm=3&size=64"
        self.assertEqual(messages.unsigned(link), "https://cdn.discordapp.com/attachments/1/2/a.png?size=64")
        other = "https://example.com/a.png?ex=1"
        self.assertEqual(messages.unsigned(other), other)
        avatar = "https://cdn.discordapp.com/avatars/1/a.png?size=64&hm=9"
        self.assertEqual(messages.unsigned(avatar), avatar)
        self.assertIsNone(messages.unsigned(None))

    async def test_a_message_read_back_has_lasting_links(self):
        embed = discord.Embed(title="T")
        embed.set_image(url="https://cdn.discordapp.com/attachments/1/2/a.png?ex=1&is=2&hm=3")
        embed.set_footer(text="f", icon_url="https://media.discordapp.net/attachments/1/2/f.png?ex=1")
        payload = messages.from_message(SimpleNamespace(embeds=[embed], content="", components=[]))
        self.assertEqual(payload["embeds"][0]["image"]["url"], "https://cdn.discordapp.com/attachments/1/2/a.png")
        self.assertEqual(payload["embeds"][0]["footer"]["icon_url"], "https://media.discordapp.net/attachments/1/2/f.png")

    async def test_json_comes_in_many_shapes(self):
        one = messages.from_json({"title": "T"})
        self.assertEqual(one["embeds"][0]["title"], "T")
        many = messages.from_json([{"title": "A"}, {"title": "B"}])
        self.assertEqual([e["title"] for e in many["embeds"]], ["A", "B"])
        wrapped = messages.from_json({"embed": {"title": "W"}, "content": "c"})
        self.assertEqual((wrapped["content"], wrapped["embeds"][0]["title"]), ("c", "W"))
        backup = messages.from_json({"messages": [{"data": {"content": "from a backup"}}]})
        self.assertEqual(backup["content"], "from a backup")
        buttons = messages.from_json({"content": "c", "components": [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": "Go", "url": "https://example.com", "emoji": {"id": "123", "name": "party"}},
            {"type": 2, "style": 1, "label": "Not a link", "custom_id": "x"},
        ]}]})
        self.assertEqual(buttons["buttons"], [{"url": "https://example.com", "label": "Go", "emoji": "<:party:123>"}])
        with self.assertRaises(Invalid):
            messages.from_json("text")
        with self.assertRaises(Invalid):
            messages.from_json({"title": "x" * 300})

    async def test_summaries(self):
        self.assertEqual(messages.summary({"content": "  two\n words ", "embeds": []}), "two words")
        self.assertEqual(messages.summary({"content": "", "embeds": [{"title": "T", "description": "D"}]}), "T")
        self.assertEqual(messages.summary({"content": "", "embeds": [{"image": {"url": "x"}}]}), "Image only")
        self.assertEqual(messages.summary({"content": "", "embeds": []}), "Empty message")
        self.assertEqual(messages.summary({"content": "x" * 300}, limit=10), "xxxxxxx...")
