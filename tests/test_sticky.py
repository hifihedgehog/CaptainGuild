import asyncio
import time
from types import SimpleNamespace

import discord

from captainguild.core.module import required_permissions
from captainguild.core.ops import DiscordOps
from captainguild.modules import sticky as sticky_module
from captainguild.modules.sticky import Sticky
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, FakeMessage, _FakeResponse
from tests.test_access_limits import HiddenChannel, SiteCase


def text(content):
    return {"content": content, "embeds": [], "buttons": []}


class StickyCase(BotTest):
    modules = [Sticky]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.cog_ = self.cog(Sticky)
        self.cog_.QUIET = 0.01
        self.cog_.MIN_GAP = 0.0
        self.cog_.MAX_WAIT = 0.05
        self.cog_.BACKOFF = 0.2

    async def stick(self, content="Read the rules", seconds=3600, channel=None):
        return await self.cog_.create(self.guild.id, (channel or self.channel).id, text(content), seconds=seconds, actor=self.bot.actor(self.admin))

    def chat(self, channel=None, author=None):
        channel = channel or self.channel
        message = FakeMessage(channel, author or self.member, "chatter")
        channel.sent.append(message)
        return message

    async def said(self, channel=None, author=None):
        await self.cog_.on_message(self.chat(channel, author))

    async def settle(self, channel=None):
        live = self.cog_._live.get((channel or self.channel).id)
        while live and live.task and not live.task.done():
            await asyncio.wait([live.task])

    def copies(self, channel=None):
        return [m for m in (channel or self.channel).sent if m.author is self.me]


class RepostTest(StickyCase):
    async def test_making_one_posts_it_and_pings_nobody(self):
        await self.stick("Hi {server} <@123>")
        sent = self.copies()[0]
        self.assertEqual(sent.content, f"Hi {self.guild.name} <@123>")
        mentions = sent.kwargs["allowed_mentions"]
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)

    async def test_a_new_message_brings_a_new_copy_and_removes_the_old_one(self):
        await self.stick()
        first = self.copies()[0]
        await self.said()
        await self.settle()
        copies = self.copies()
        self.assertEqual(len(copies), 1)
        self.assertTrue(first.deleted)
        self.assertIsNot(copies[0], first)
        self.assertEqual(self.channel.sent[-1], copies[0])
        self.assertEqual(await self.db.scalar("SELECT message_id FROM sticky_messages"), copies[0].id)

    async def test_the_new_copy_goes_up_before_the_old_one_comes_down(self):
        await self.stick()
        order = []
        real_send, real_delete = self.channel.send, self.bot.ops.delete_message

        async def send(*args, **kwargs):
            order.append("post")
            return await real_send(*args, **kwargs)

        async def delete(*args, **kwargs):
            order.append("delete")
            await real_delete(*args, **kwargs)

        self.channel.send = send
        self.bot.ops.delete_message = delete
        await self.said()
        await self.settle()
        self.assertEqual(order, ["post", "delete"])

    async def test_a_burst_of_messages_means_one_repost(self):
        await self.stick()
        self.cog_.QUIET = 0.05
        self.cog_.MAX_WAIT = 5
        for _ in range(20):
            await self.said()
        await self.settle()
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(self.channel.sent[-1], self.copies()[0])
        rows = await self.db.all("SELECT stale_message_id FROM sticky_messages")
        self.assertIsNone(rows[0]["stale_message_id"])

    async def test_the_sticky_doesnt_set_itself_off(self):
        await self.stick()
        live = self.cog_._live[self.channel.id]
        await self.cog_.on_message(self.copies()[0])
        self.assertIsNone(live.task)
        live.posting = True
        echo = FakeMessage(self.channel, self.me, "an echo that beat the reply")
        await self.cog_.on_message(echo)
        self.assertIsNone(live.task)

    async def test_the_bots_other_messages_and_other_bots_count_as_new_messages(self):
        await self.stick()
        await self.said(author=self.me)
        await self.settle()
        other = FakeMember(self.guild, "Other", bot=True)
        await self.said(author=other)
        await self.settle()
        self.assertEqual(self.channel.sent[-1].content, "Read the rules")

    async def test_two_sticky_bots_cant_loop_faster_than_the_gap(self):
        await self.stick()
        self.cog_.MIN_GAP = 0.2
        other = FakeMember(self.guild, "Other Sticky Bot", bot=True)
        start = time.monotonic()
        posted = []
        real_send = self.channel.send

        async def send(*args, **kwargs):
            posted.append(time.monotonic() - start)
            return await real_send(*args, **kwargs)

        self.channel.send = send
        while time.monotonic() - start < 0.5:
            await self.said(author=other)
            await asyncio.sleep(0.01)
        await self.settle()
        self.assertLessEqual(len(posted), 4)
        for earlier, later in zip(posted, posted[1:]):
            self.assertGreaterEqual(later - earlier, 0.19)

    async def test_a_channel_that_never_goes_quiet_still_gets_it_back(self):
        await self.stick()
        first = self.copies()[0]
        self.cog_.QUIET = 10
        self.cog_.MAX_WAIT = 0.05
        for _ in range(40):
            await self.said()
            await asyncio.sleep(0.01)
            if first.deleted:
                break
        self.assertTrue(first.deleted)
        self.cog_.cog_unload()

    async def test_a_message_during_the_repost_gets_its_own(self):
        await self.stick()
        real_send = self.channel.send
        done = []

        async def send(*args, **kwargs):
            if not done:
                done.append(1)
                await self.said()
            return await real_send(*args, **kwargs)

        self.channel.send = send
        await self.said()
        await self.settle()
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(self.channel.sent[-1], self.copies()[0])
        self.assertEqual(len([m for m in self.channel.sent if m.author is self.member]), 2)

    async def test_removing_it_mid_repost_leaves_no_copy_behind(self):
        await self.stick()
        real_send = self.channel.send
        started, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            started.set()
            await release.wait()
            return await real_send(*args, **kwargs)

        self.channel.send = send
        await self.said()
        await started.wait()
        removing = asyncio.ensure_future(self.cog_._remove(self.channel.id))
        await asyncio.sleep(0.01)
        release.set()
        await removing
        self.assertEqual(self.copies(), [])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_messages"), 0)

    async def test_other_channels_arent_watched(self):
        await self.stick()
        other = FakeChannel(self.guild, "other")
        await self.said(channel=other)
        self.assertEqual(self.copies(other), [])

    async def test_a_refusal_keeps_the_sticky_and_backs_off(self):
        await self.stick()
        first = self.copies()[0]
        real_send = self.channel.send
        calls = []

        async def refuse(*args, **kwargs):
            calls.append(1)
            raise discord.Forbidden(_FakeResponse(403), "Missing Access")

        self.channel.send = refuse
        await self.said()
        await self.settle()
        self.assertEqual(len(calls), 1)
        self.assertFalse(first.deleted)
        live = self.cog_._live[self.channel.id]
        self.assertGreater(live.not_before, time.monotonic())
        self.assertEqual(live.message_id, first.id)
        self.channel.send = real_send
        await self.said()
        await self.settle()
        self.assertTrue(first.deleted)

    async def test_when_the_old_copy_wont_delete_it_gets_cleaned_up_next_time(self):
        await self.stick()
        first = self.copies()[0]
        real_delete = first.delete

        async def stuck(**kwargs):
            raise discord.Forbidden(_FakeResponse(403), "Missing Access")

        first.delete = stuck
        await self.said()
        await self.settle()
        self.assertFalse(first.deleted)
        self.assertEqual(await self.db.scalar("SELECT stale_message_id FROM sticky_messages"), first.id)
        first.delete = real_delete
        await self.said()
        await self.settle()
        self.assertTrue(first.deleted)
        self.assertIsNone(await self.db.scalar("SELECT stale_message_id FROM sticky_messages"))

    async def test_a_moderator_deleting_the_copy_doesnt_bring_it_straight_back(self):
        await self.stick()
        copy = self.copies()[0]
        self.channel.sent.remove(copy)
        await self.cog_.on_raw_message_delete(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_id=copy.id))
        live = self.cog_._live[self.channel.id]
        self.assertIsNone(live.message_id)
        self.assertIsNone(live.task)
        self.assertIsNone(await self.db.scalar("SELECT message_id FROM sticky_messages"))
        await self.said()
        await self.settle()
        self.assertEqual(len(self.copies()), 1)

    async def test_bulk_delete_is_noticed(self):
        await self.stick()
        copy = self.copies()[0]
        await self.cog_.on_raw_bulk_message_delete(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_ids={copy.id, 5}))
        self.assertIsNone(self.cog_._live[self.channel.id].message_id)


class LifetimeTest(StickyCase):
    async def test_it_ends_when_its_time_is_up_and_the_copy_goes(self):
        await self.stick(seconds=3600)
        copy = self.copies()[0]
        self.assertEqual(await self.bot.timers.run_due(time.time() + 100), 0)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 1)
        self.assertTrue(copy.deleted)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_messages"), 0)
        self.assertNotIn(self.channel.id, self.cog_._live)

    async def test_one_with_no_end_has_no_timer(self):
        await self.stick(seconds=None)
        self.assertIsNone(await self.bot.timers.get_key(sticky_module.EXPIRE, str(self.channel.id)))
        row = await self.db.one("SELECT expires_at FROM sticky_messages")
        self.assertIsNone(row["expires_at"])

    async def test_a_deleted_channel_is_forgotten(self):
        await self.stick()
        await self.cog_.on_guild_channel_delete(self.channel)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_messages"), 0)
        self.assertIsNone(await self.bot.timers.get_key(sticky_module.EXPIRE, str(self.channel.id)))

    async def test_a_restart_remembers_and_catches_up_on_what_it_missed(self):
        await self.stick()
        copy = self.copies()[0]
        fresh = Sticky(self.bot)
        fresh.QUIET, fresh.MIN_GAP, fresh.MAX_WAIT = 0.01, 0.0, 0.05
        await fresh.on_load()
        self.assertEqual(fresh._live[self.channel.id].message_id, copy.id)
        self.channel.last_message_id = copy.id
        await fresh.on_ready()
        self.assertIsNone(fresh._live[self.channel.id].task)
        self.channel.last_message_id = copy.id + 1
        await fresh.on_ready()
        await asyncio.wait([fresh._live[self.channel.id].task])
        self.assertTrue(copy.deleted)
        self.assertEqual(len(self.copies()), 1)
        fresh.cog_unload()


class EndpointTest(StickyCase):
    async def create(self, client, **form):
        body = {"channel_id": str(self.channel.id), "payload": text("Hello"), "seconds": 3600, **form}
        return await client.post(self.path("/sticky"), json=body)

    async def test_create_list_edit_remove(self):
        client = await self.client()
        response = await self.create(client)
        self.assertEqual(response.status, 200)
        copy = self.copies()[0]
        self.assertEqual(copy.content, "Hello")
        listed = await (await client.get(self.path("/sticky"))).json()
        self.assertEqual(listed["stickies"][0]["channel_id"], str(self.channel.id))
        self.assertEqual(listed["stickies"][0]["payload"]["content"], "Hello")
        response = await client.patch(self.path(f"/sticky/{self.channel.id}"), json={"payload": text("Changed")})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.copies()[-1].content, "Changed")
        self.assertTrue(copy.deleted)
        self.assertEqual(len(self.copies()), 1)
        response = await client.delete(self.path(f"/sticky/{self.channel.id}"))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.copies(), [])
        actions = [row["action"] for row in await self.bot.activity_log.recent(self.guild.id)]
        self.assertEqual(actions, ["sticky.remove", "sticky.update", "sticky.create"])

    async def test_one_per_channel(self):
        client = await self.client()
        await self.create(client)
        self.assertEqual((await self.create(client)).status, 409)
        self.assertEqual(len(self.copies()), 1)

    async def test_the_duration_limits_apply_and_no_end_is_allowed(self):
        client = await self.client()
        self.assertEqual((await self.create(client, seconds=59)).status, 400)
        self.assertEqual((await self.create(client, seconds=366 * 86400)).status, 400)
        self.assertEqual((await self.create(client, seconds="soon")).status, 400)
        response = await self.create(client, seconds=None, forever=True)
        self.assertEqual(response.status, 200)
        self.assertIsNone((await response.json())["expires_at"])

    async def test_changing_the_end_moves_the_timer(self):
        client = await self.client()
        await self.create(client, seconds=3600)
        response = await client.patch(self.path(f"/sticky/{self.channel.id}"), json={"seconds": 7200})
        self.assertGreater((await response.json())["expires_at"], time.time() + 7000)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3700), 0)
        await client.patch(self.path(f"/sticky/{self.channel.id}"), json={"forever": True})
        self.assertIsNone(await self.bot.timers.get_key(sticky_module.EXPIRE, str(self.channel.id)))

    async def test_a_saved_template_can_use_variables_in_links(self):
        client = await self.client()
        payload = {"content": "", "embeds": [{"title": "T", "thumbnail": {"url": "{user(avatar)}"}}], "buttons": []}
        self.assertEqual((await self.create(client, payload=payload)).status, 200)
        response = await client.post(self.path("/sticky/templates"), json={"name": "With link", "payload": payload})
        self.assertEqual(response.status, 200)

    async def test_an_empty_message_or_a_bad_one_is_refused(self):
        client = await self.client()
        self.assertEqual((await self.create(client, payload=text(""))).status, 400)
        self.assertEqual((await self.create(client, payload={"content": "x" * 2001})).status, 400)
        self.assertEqual(self.copies(), [])

    async def test_a_refused_post_leaves_nothing_behind(self):
        client = await self.client()

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "Missing Access")

        self.channel.send = refuse
        self.assertEqual((await self.create(client)).status, 502)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sticky_messages"), 0)
        self.assertNotIn(self.channel.id, self.cog_._live)
        self.assertIsNone(await self.bot.timers.get_key(sticky_module.EXPIRE, str(self.channel.id)))

    async def test_another_server_is_out_of_reach(self):
        other = FakeGuild("Elsewhere")
        self.bot.fake_guilds.append(other)
        other.me = FakeMember(other, "Bot", bot=True)
        elsewhere = FakeChannel(other, "elsewhere")
        client = await self.client()
        response = await client.post(self.path("/sticky"), json={"channel_id": str(elsewhere.id), "payload": text("Hi"), "seconds": 3600})
        self.assertEqual(response.status, 400)
        self.assertEqual(elsewhere.sent, [])
        await self.cog_.create(other.id, elsewhere.id, text("Theirs"), seconds=3600, actor=self.bot.actor(self.admin))
        self.assertEqual((await client.patch(self.path(f"/sticky/{elsewhere.id}"), json={"payload": text("Mine now")})).status, 404)
        self.assertEqual((await client.delete(self.path(f"/sticky/{elsewhere.id}"))).status, 404)
        self.assertEqual((await (await client.get(self.path("/sticky"))).json())["stickies"], [])

    async def test_templates_are_named_uniquely_and_scoped(self):
        client = await self.client()
        response = await client.post(self.path("/sticky/templates"), json={"name": "Rules", "payload": text("One")})
        self.assertEqual(response.status, 200)
        template_id = (await response.json())["id"]
        self.assertEqual((await client.post(self.path("/sticky/templates"), json={"name": "rules", "payload": text("Two")})).status, 400)
        self.assertEqual((await client.post(self.path("/sticky/templates"), json={"name": "ab", "payload": text("Two")})).status, 400)
        self.assertEqual((await client.post(self.path("/sticky/templates"), json={"name": "Good name", "payload": text("")})).status, 400)
        response = await client.patch(self.path(f"/sticky/templates/{template_id}"), json={"name": "Rules", "payload": text("Changed")})
        self.assertEqual(response.status, 200)
        await self.db.run("INSERT INTO sticky_templates (guild_id, name, payload, summary, updated_at) VALUES (999, 'Theirs', '{}', '', 0)")
        theirs = await self.db.scalar("SELECT id FROM sticky_templates WHERE guild_id = 999")
        self.assertEqual((await client.patch(self.path(f"/sticky/templates/{theirs}"), json={"name": "Taken"})).status, 404)
        self.assertEqual((await client.delete(self.path(f"/sticky/templates/{theirs}"))).status, 404)
        listed = await (await client.get(self.path("/sticky"))).json()
        self.assertEqual([t["name"] for t in listed["templates"]], ["Rules"])
        self.assertEqual((await client.delete(self.path(f"/sticky/templates/{template_id}"))).status, 200)

    async def test_staff_are_refused(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/sticky"))).status, 403)
        self.assertEqual((await self.create(client)).status, 403)


class CommandTest(StickyCase):
    async def test_add_defaults_to_a_day_and_here(self):
        ctx = self.context()
        await self.cog_.sticky_add.callback(self.cog_, ctx, message="Hello there")
        self.assertEqual(self.copies()[0].content, "Hello there")
        row = await self.db.one("SELECT seconds, created_by_name FROM sticky_messages")
        self.assertEqual(row, {"seconds": 86400, "created_by_name": "admin"})
        self.assertIn("1 day", ctx.oks[0])

    async def test_a_text_command_reads_the_channel_and_duration_off_the_end(self):
        target = FakeChannel(self.guild, "news")
        ctx = self.context()
        await self.cog_.sticky_add.callback(self.cog_, ctx, message=f"Hello there everyone 2h {target.mention}")
        self.assertEqual(target.sent[0].content, "Hello there everyone")
        self.assertEqual(await self.db.scalar("SELECT seconds FROM sticky_messages"), 7200)

    async def test_a_slash_command_leaves_the_text_alone(self):
        ctx = self.context()
        ctx.interaction = object()
        await self.cog_.sticky_add.callback(self.cog_, ctx, message="Back in 2h", duration=3600, channel=None)
        self.assertEqual(self.copies()[0].content, "Back in 2h")
        self.assertEqual(await self.db.scalar("SELECT seconds FROM sticky_messages"), 3600)

    async def test_one_per_channel_and_duration_limits(self):
        ctx = self.context()
        await self.cog_.sticky_add.callback(self.cog_, ctx, message="One")
        await self.cog_.sticky_add.callback(self.cog_, ctx, message="Two")
        self.assertIn("already", ctx.fails[0])
        other = FakeChannel(self.guild, "other")
        await self.cog_.sticky_add.callback(self.cog_, ctx, message="Three", duration=10, channel=other)
        self.assertIn("between", ctx.fails[1])
        self.assertEqual(self.copies(other), [])

    async def test_list_and_remove(self):
        ctx = self.context()
        await self.cog_.sticky.callback(self.cog_, ctx)
        self.assertIn("No sticky", ctx.sent[-1]["content"])
        await self.stick()
        await self.cog_.sticky.callback(self.cog_, ctx)
        self.assertIn(self.channel.mention, ctx.sent[-1]["content"])
        self.assertIn("Read the rules", ctx.sent[-1]["content"])
        await self.cog_.sticky_remove.callback(self.cog_, ctx)
        self.assertEqual(self.copies(), [])
        await self.cog_.sticky_remove.callback(self.cog_, ctx)
        self.assertTrue(ctx.fails)

    async def test_template_command(self):
        client = await self.client()
        await client.post(self.path("/sticky/templates"), json={"name": "Rules", "payload": text("Be kind")})
        ctx = self.context()
        await self.cog_.sticky_template.callback(self.cog_, ctx, "rules")
        self.assertEqual(self.copies()[0].content, "Be kind")
        await self.cog_.sticky_template.callback(self.cog_, ctx, "unknown", channel=FakeChannel(self.guild, "other"))
        self.assertIn("no template", ctx.fails[0])

    async def test_template_names_autocomplete_within_the_server(self):
        client = await self.client()
        await client.post(self.path("/sticky/templates"), json={"name": "Rules", "payload": text("Be kind")})
        await client.post(self.path("/sticky/templates"), json={"name": "Links", "payload": text("Go here")})
        await self.db.run("INSERT INTO sticky_templates (guild_id, name, payload, summary, updated_at) VALUES (999, 'Rules elsewhere', '{}', '', 0)")
        found = await self.cog_._template_names(SimpleNamespace(guild_id=self.guild.id), "ru")
        self.assertEqual([c.name for c in found], ["Rules"])

    async def test_every_command_needs_manage_server(self):
        for command in (self.cog_.sticky, self.cog_.sticky_add, self.cog_.sticky_remove, self.cog_.sticky_template):
            self.assertEqual(required_permissions(command), ("manage_guild",), command.name)


class TwoBotsTest(StickyCase):
    """Another sticky bot answers every post with its own, so reposts set off only by bots have to slow down and then stop."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.other = FakeMember(self.guild, "Other Sticky Bot", bot=True)
        self.cog_.QUIET = 0.0
        self.cog_.MIN_GAP = 0.001
        self.cog_.MAX_WAIT = 0.0

    async def answered_by_the_other_bot(self):
        """Posts until the other bot's answers stop setting off a repost. Returns how many went out."""
        posts = []
        real_send = self.channel.send

        async def send(*args, **kwargs):
            sent = await real_send(*args, **kwargs)
            posts.append(sent)
            self.assertLess(len(posts), 30, "the bots never stopped answering each other")
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.cog_.on_message(self.chat(author=self.other))))
            return sent
        self.channel.send = send
        await self.said(author=self.other)
        while True:
            await self.settle()
            for _ in range(5):  # the answer to the last post runs as its own task
                await asyncio.sleep(0)
            if self.cog_._live[self.channel.id].task.done():
                return len(posts)

    async def test_they_slow_down_and_then_stop(self):
        await self.stick()
        self.cog_.BOT_WAIT_MAX = 0.01
        count = await self.answered_by_the_other_bot()
        # Three go out at the normal pace, and the wait then doubles from 0.002 until it reaches the most it can be.
        self.assertEqual(count, 3 + 3)
        live = self.cog_._live[self.channel.id]
        self.assertEqual(self.cog_._gap(live), self.cog_.BOT_WAIT_MAX)

    async def test_the_wait_doubles_from_the_fourth(self):
        await self.stick()
        live = self.cog_._live[self.channel.id]
        self.cog_.MIN_GAP = 15.0
        self.assertEqual([self.cog_._gap(type(live)(self.guild.id, bot_reposts=n)) for n in (0, 2, 3, 4, 5)], [15.0, 15.0, 30.0, 60.0, 120.0])
        self.assertEqual(self.cog_._gap(type(live)(self.guild.id, bot_reposts=500)), self.cog_.BOT_WAIT_MAX)

    async def test_a_person_talking_starts_it_again(self):
        await self.stick()
        self.cog_.BOT_WAIT_MAX = 0.01
        count = await self.answered_by_the_other_bot()
        self.channel.send = FakeChannel.send.__get__(self.channel)
        await self.said()
        await self.settle()
        self.assertEqual(self.cog_._live[self.channel.id].bot_reposts, 0)
        self.assertEqual(self.channel.sent[-1].content, "Read the rules")
        self.assertGreater(count, 0)

    async def test_a_person_talking_doesnt_wait_out_a_long_gap(self):
        await self.stick()
        live = self.cog_._live[self.channel.id]
        self.cog_.MIN_GAP = 0.01
        self.cog_.BOT_WAIT_MAX = 100.0
        live.bot_reposts = 12
        live.last_post = time.monotonic()
        await self.said(author=self.other)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertTrue(live.sleeping)
        before = live.message_id
        await self.said()
        await asyncio.wait_for(self.settle(), 10)
        self.assertNotEqual(live.message_id, before)

    async def test_people_alone_never_slow_it_down(self):
        await self.stick()
        for _ in range(6):
            await self.said()
            await self.settle()
        self.assertEqual(self.cog_._live[self.channel.id].bot_reposts, 0)


class ChannelsGoingAwayTest(StickyCase):
    async def test_a_deleted_thread_takes_its_sticky_along(self):
        thread = FakeChannel(self.guild, "thread")
        await self.stick(channel=thread)
        self.guild.channels.remove(thread)
        await self.cog_.on_raw_thread_delete(SimpleNamespace(thread_id=thread.id, guild_id=self.guild.id))
        self.assertIsNone(await self.db.one("SELECT * FROM sticky_messages"))
        self.assertNotIn(thread.id, self.cog_._live)

    async def test_a_channel_deleted_during_downtime_goes_on_ready(self):
        gone = FakeChannel(self.guild, "gone")
        await self.stick(channel=gone)
        self.guild.channels.remove(gone)

        async def fetch_channel(channel_id):
            raise discord.NotFound(_FakeResponse(404), "Unknown Channel")
        self.guild.fetch_channel = fetch_channel
        await self.cog_.on_ready()
        self.assertIsNone(await self.db.one("SELECT * FROM sticky_messages"))

    async def test_an_archived_thread_isnt_mistaken_for_a_deleted_one(self):
        archived = FakeChannel(self.guild, "archived")
        await self.stick(channel=archived)
        self.guild.channels.remove(archived)  # an archived thread isn't in the cache

        async def fetch_channel(channel_id):
            return archived
        self.guild.fetch_channel = fetch_channel
        await self.cog_.on_ready()
        self.assertIsNotNone(await self.db.one("SELECT * FROM sticky_messages"))

    async def test_a_server_that_isnt_back_yet_keeps_its_stickies(self):
        await self.stick()
        self.guild.channels.remove(self.channel)
        self.guild.unavailable = True

        async def fetch_channel(channel_id):
            raise discord.NotFound(_FakeResponse(404), "Unknown Channel")
        self.guild.fetch_channel = fetch_channel
        await self.cog_.on_ready()
        self.assertIsNotNone(await self.db.one("SELECT * FROM sticky_messages"))

    async def test_a_failed_lookup_keeps_the_sticky(self):
        await self.stick()
        self.guild.channels.remove(self.channel)

        async def fetch_channel(channel_id):
            raise discord.DiscordServerError(_FakeResponse(500), "down")
        self.guild.fetch_channel = fetch_channel
        await self.cog_.on_ready()
        self.assertIsNotNone(await self.db.one("SELECT * FROM sticky_messages"))


class TwoCreatesTest(StickyCase):
    async def test_the_second_gets_the_plain_answer(self):
        client = await self.client()
        form = {"channel_id": str(self.channel.id), "payload": {"content": "x"}, "forever": True}
        real = self.cog_._room_for_one
        waiting = []
        both_here = asyncio.Event()

        async def together(guild_id):
            waiting.append(guild_id)
            if len(waiting) == 2:
                both_here.set()
            await both_here.wait()
            return await real(guild_id)
        self.cog_._room_for_one = together
        answers = await asyncio.gather(client.post(self.path("/sticky"), json=form), client.post(self.path("/sticky"), json=form))
        self.assertEqual(sorted(answer.status for answer in answers), [200, 409])
        self.assertEqual(len(self.copies()), 1)

    async def test_the_command_says_so_too(self):
        await self.db.run(
            "INSERT INTO sticky_messages (guild_id, channel_id, payload, summary, created_at) VALUES (?, ?, ?, ?, ?)", (self.guild.id, self.channel.id, "{}", "x", time.time()),
        )
        ctx = self.context(self.admin)
        await self.cog_.sticky_add.callback(self.cog_, ctx, message="hello", duration=None, channel=None)
        self.assertIn("already has a sticky", ctx.fails[0])


class ReachTest(SiteCase):
    """The commands answer the way the dashboard does."""
    modules = [Sticky]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Sticky)
        self.secret = HiddenChannel(self.guild, "owners-only", {self.admin.id, self.me.id})
        self.poster = FakeMember(self.guild, "poster", self.guild.role("Poster", 4, "manage_guild", "view_channel", "send_messages"))

    async def asyncTearDown(self):
        self.cog_.cog_unload()
        await super().asyncTearDown()

    async def stick_in_secret(self):
        await self.cog_.create(self.guild.id, self.secret.id, text("private owner text"), seconds=None, actor=self.bot.actor(self.admin))

    async def test_the_list_leaves_out_what_they_cant_see(self):
        await self.stick_in_secret()
        ctx = self.context(self.poster)
        await self.cog_.sticky.callback(self.cog_, ctx)
        self.assertEqual(ctx.sent[0]["content"], "No sticky messages are set.")
        ctx = self.context(self.admin)
        await self.cog_.sticky.callback(self.cog_, ctx)
        self.assertIn("private owner text", ctx.sent[0]["content"])

    async def test_a_channel_thats_gone_stays_listed(self):
        await self.stick_in_secret()
        self.guild.channels.remove(self.secret)
        ctx = self.context(self.poster)
        await self.cog_.sticky.callback(self.cog_, ctx)
        self.assertIn("private owner text", ctx.sent[0]["content"])

    async def test_add_and_template_refuse_a_channel_they_cant_see(self):
        ctx = self.context(self.poster)
        await self.cog_.sticky_add.callback(self.cog_, ctx, message="from a manager", duration=None, channel=self.secret)
        self.assertEqual(ctx.fails, ["You can't see that channel."])
        self.assertEqual(self.secret.sent, [])
        await self.db.run("INSERT INTO sticky_templates (guild_id, name, payload, summary, updated_at) VALUES (?, ?, ?, ?, ?)", (self.guild.id, "rules", '{"content": "x"}', "x", time.time()))
        ctx = self.context(self.poster)
        await self.cog_.sticky_template.callback(self.cog_, ctx, "rules", None, self.secret)
        self.assertEqual(ctx.fails, ["You can't see that channel."])
        self.assertEqual(self.secret.sent, [])

    async def test_someone_who_cant_post_there_cant_stick_there(self):
        ctx = self.context(self.manager)
        await self.cog_.sticky_add.callback(self.cog_, ctx, message="from a manager", duration=None, channel=self.channel)
        self.assertEqual(ctx.fails, ["You can't post in that channel."])
        client = await self.site(self.manager)
        response = await client.post(self.path("/sticky"), json={"channel_id": str(self.channel.id), "payload": {"content": "x"}, "forever": True})
        self.assertEqual(response.status, 400)
        self.assertIn("can't post", (await response.json())["error"])
        self.assertEqual(self.channel.sent, [])

    async def test_someone_who_can_post_there_can(self):
        ctx = self.context(self.poster)
        await self.cog_.sticky_add.callback(self.cog_, ctx, message="hello", duration=None, channel=self.channel)
        self.assertEqual(ctx.fails, [])
        self.assertEqual(len(self.channel.sent), 1)

    async def test_remove_refuses_a_channel_they_cant_see(self):
        await self.stick_in_secret()
        ctx = self.context(self.poster)
        await self.cog_.sticky_remove.callback(self.cog_, ctx, channel=self.secret)
        self.assertEqual(ctx.fails, ["You can't see that channel."])
        self.assertIsNotNone(await self.db.one("SELECT * FROM sticky_messages"))
        ctx = self.context(self.admin)
        await self.cog_.sticky_remove.callback(self.cog_, ctx, channel=self.secret)
        self.assertEqual(ctx.fails, [])
