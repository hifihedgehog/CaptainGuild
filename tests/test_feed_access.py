"""Feeds and alerts post in the bot's name later on, so they ask what the person setting them up could do themselves, on commands and the dashboard alike."""
import json
import time

import discord

from captainguild.core import delivery
from captainguild.core.ops import DiscordOps
from captainguild.modules.feeds import Feeds, next_run
from captainguild.modules.notifications import TWITCH_API, YOUTUBE_FEED, YOUTUBE_SITE, Notifications
from captainguild.web.api import User
from captainguild.web.app import SITE
from tests.fake_outside import FakeOutside, reply
from tests.fakes import BotTest, FakeChannel, FakeMember, FakePermissions, _FakeResponse
from tests.test_notifications import CHANNEL, channel_page, feed

EVERYDAY = ("view_channel", "send_messages", "read_message_history", "send_messages_in_threads")
NO_SEE = "You can't see that channel."
NO_POST = "You can't post in that channel."
NO_PING = "You need Mention Everyone in that channel to ping through the bot."


class Case(BotTest):
    modules = [Feeds, Notifications]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.feeds = self.cog(Feeds)
        self.notify = self.cog(Notifications)
        self.web = FakeOutside()
        self.notify.outside = self.web
        self.bot.ops = DiscordOps(self.bot)
        self.now = time.time()
        self.feeds._clock = lambda: self.now
        self.notify._clock = lambda: self.now
        self.manager = FakeMember(self.guild, "manager", self.guild.role("Manager", 3, "manage_guild", *EVERYDAY))
        self.hidden = self.closed("staff")
        self.readonly = self.closed("news", "view_channel", "read_message_history")
        self.pings = self.guild.role("Pings", 2)
        self.pings.mentionable = True
        self.quiet = self.guild.role("Quiet", 2)
        self.web.answer(f"{TWITCH_API}/users", {"data": [{"id": "111", "login": "sample", "display_name": "Sample"}]})
        self.web.answer("https://id.twitch.tv", {"access_token": "t", "expires_in": 5000})
        self.web.answer(YOUTUBE_FEED, reply(feed("aaaaaaaaaaa")))
        self.web.answer(f"{YOUTUBE_SITE}/@", channel_page())
        await self.bot.settings.save(self.guild.id, "notifications", {"twitch_client_id": "a", "twitch_client_secret": "b"})

    def closed(self, name, *allowed):
        """A channel where the manager holds only these permissions."""
        channel = FakeChannel(self.guild, name)
        real = channel.permissions_for
        channel.permissions_for = lambda member: FakePermissions(*allowed) if member is self.manager else real(member)
        return channel

    def ctx(self, channel=None):
        return self.context(self.manager, channel or self.channel)

    async def as_manager(self):
        client = await self.client()
        client.server.app[SITE].demo_user = User(self.manager.id, self.manager.name, None)
        return client

    def assertRefused(self, ctx, sentence):
        self.assertEqual(ctx.fails, [sentence])
        self.assertEqual(ctx.oks, [])

    async def autofeed(self, channel, mention="silent", role_id=None, next_at=None):
        return await self.feeds._insert(self.guild.id, channel.id, mention, role_id, {"content": "secret plans", "embeds": [], "buttons": []}, next_at or self.now + 1000, (0, 0, 0, 0), None, None)

    def form(self, channel, mention="silent", when=None):
        return {"channel_id": str(channel.id), "message": {"content": "hello"}, "when": when or self.now + 1000, "mention": mention, "repeat": [0, 0, 0, 0]}


class FeedCommandTest(Case):
    async def feed(self, channel, role=None):
        await self.db.run("INSERT INTO feeds (guild_id, name, role_id, channel_id, created_at) VALUES (?, 'news', ?, ?, 1)", (self.guild.id, (role or self.pings).id, channel.id))

    async def test_move_refuses_a_channel_they_cant_see_or_post_in(self):
        await self.feed(self.channel)
        for target, sentence in ((self.hidden, NO_SEE), (self.readonly, NO_POST)):
            ctx = self.ctx()
            await self.feeds.feeds_move.callback(self.feeds, ctx, "news", target)
            self.assertRefused(ctx, sentence)
        self.assertEqual(await self.db.scalar("SELECT channel_id FROM feeds"), self.channel.id)

    async def test_announce_checks_a_channel_chosen_earlier(self):
        await self.feed(self.hidden)
        ctx = self.ctx()
        await self.feeds.feeds_announce.callback(self.feeds, ctx, "news", content="text")
        self.assertRefused(ctx, NO_SEE)
        self.assertEqual(self.hidden.sent, [])

    async def test_announce_to_a_role_members_cant_ping_takes_mention_everyone(self):
        await self.feed(self.channel, self.quiet)
        ctx = self.ctx()
        await self.feeds.feeds_announce.callback(self.feeds, ctx, "news", content="text")
        self.assertRefused(ctx, NO_PING)
        admin = self.context(self.admin)
        await self.feeds.feeds_announce.callback(self.feeds, admin, "news", content="text")
        self.assertEqual(len(self.channel.sent), 1)

    async def test_create_checks_the_channel_and_the_role(self):
        ctx = self.ctx(self.readonly)
        await self.feeds.feeds_create.callback(self.feeds, ctx, "news", self.pings)
        self.assertRefused(ctx, NO_POST)
        ctx = self.ctx()
        await self.feeds.feeds_create.callback(self.feeds, ctx, "news", self.quiet)
        self.assertRefused(ctx, NO_PING)
        ctx = self.ctx()
        await self.feeds.feeds_create.callback(self.feeds, ctx, "news", self.pings)
        self.assertEqual(ctx.fails, [])

    async def test_a_role_that_was_deleted_gets_its_own_remedy(self):
        gone = self.guild.role("Gone", 2)
        await self.feed(self.channel, gone)
        self.guild.roles.remove(gone)
        ctx = self.context(self.admin)
        await self.feeds.feeds_announce.callback(self.feeds, ctx, "news", content="text")
        self.assertIn("no longer exists", ctx.oks[0])
        self.assertNotIn("Manage Roles", ctx.oks[0])

    async def test_clear_asks_first_and_leaves_what_they_cant_see(self):
        await self.feed(self.hidden)
        await self.db.run("INSERT INTO feeds (guild_id, name, role_id, channel_id, created_at) VALUES (?, 'mine', ?, ?, 1)", (self.guild.id, self.pings.id, self.channel.id))
        ctx = self.ctx()
        await self.feeds.feeds_clear.callback(self.feeds, ctx)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM feeds"), 2, "nothing goes before the button")
        view = ctx.sent[0]["view"]
        self.assertEqual(await view.clear(), "Deleted 1 feeds. 1 in channels you can't see stay.")
        self.assertEqual(await self.db.scalar("SELECT name FROM feeds"), "news")


class AutofeedCommandTest(Case):
    async def test_a_scheduled_ping_takes_what_it_would_take_them(self):
        for command in (self.feeds.autofeeds_everyone, self.feeds.autofeeds_here):
            ctx = self.ctx()
            await command.callback(self.feeds, ctx, "2h", message="free nitro")
            self.assertRefused(ctx, NO_PING)
        ctx = self.ctx()
        await self.feeds.autofeeds_create.callback(self.feeds, ctx, self.quiet, "2h", message="x")
        self.assertRefused(ctx, NO_PING)
        ctx = self.ctx()
        await self.feeds.autofeeds_create.callback(self.feeds, ctx, self.guild.default_role, "2h", message="x")
        self.assertRefused(ctx, NO_PING)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autofeeds"), 0)
        ctx = self.ctx()
        await self.feeds.autofeeds_create.callback(self.feeds, ctx, self.pings, "2h", message="x")
        await self.feeds.autofeeds_silent.callback(self.feeds, ctx, "2h", message="x")
        self.assertEqual(ctx.fails, [])

    async def test_scheduling_in_a_read_only_channel_is_refused(self):
        ctx = self.ctx(self.readonly)
        await self.feeds.autofeeds_silent.callback(self.feeds, ctx, "2h", message="x")
        self.assertRefused(ctx, NO_POST)

    async def test_move_checks_the_new_channel_and_its_ping(self):
        row = await self.autofeed(self.channel, "everyone")
        ctx = self.ctx()
        await self.feeds.autofeeds_move.callback(self.feeds, ctx, row, self.readonly)
        self.assertRefused(ctx, NO_POST)
        ctx = self.ctx()
        await self.feeds.autofeeds_move.callback(self.feeds, ctx, row, FakeChannel(self.guild, "other"))
        self.assertRefused(ctx, NO_PING)

    async def test_a_row_they_cant_see_is_out_of_reach(self):
        row = await self.autofeed(self.hidden)
        for command, args in ((self.feeds.autofeeds_delete, ()), (self.feeds.autofeeds_silence, ()), (self.feeds.autofeeds_repeat, ("1d",)), (self.feeds.autofeeds_move, (self.channel,))):
            ctx = self.ctx()
            await command.callback(self.feeds, ctx, row, *args)
            self.assertRefused(ctx, NO_SEE)
        ctx = self.ctx()
        await self.feeds.autofeeds.callback(self.feeds, ctx)
        self.assertNotIn("secret plans", ctx.sent[0]["embed"].description)

    async def test_move_arms_a_one_time_post_that_failed(self):
        row = await self.autofeed(self.channel, next_at=self.now - 500)
        await self.db.run("UPDATE autofeeds SET last_error = 'The channel no longer exists.' WHERE id = ?", (row,))
        await self.bot.timers.cancel_key("autofeed", str(row))
        ctx = self.context(self.admin)
        await self.feeds.autofeeds_move.callback(self.feeds, ctx, row, self.readonly)
        self.assertIn("posts now", ctx.oks[0])
        self.assertIsNotNone(await self.bot.timers.get_key("autofeed", str(row)))
        self.assertIsNone(await self.db.scalar("SELECT last_error FROM autofeeds WHERE id = ?", (row,)))

    async def test_repeat_arms_a_one_time_post_that_failed_and_off_doesnt(self):
        row = await self.autofeed(self.channel, next_at=self.now - 500)
        await self.db.run("UPDATE autofeeds SET last_error = 'gone' WHERE id = ?", (row,))
        await self.bot.timers.cancel_key("autofeed", str(row))
        ctx = self.context(self.admin)
        await self.feeds.autofeeds_repeat.callback(self.feeds, ctx, row, "off")
        self.assertIsNone(await self.bot.timers.get_key("autofeed", str(row)))
        await self.feeds.autofeeds_repeat.callback(self.feeds, ctx, row, "1 day")
        self.assertIsNotNone(await self.bot.timers.get_key("autofeed", str(row)))
        self.assertIsNone(await self.db.scalar("SELECT last_error FROM autofeeds WHERE id = ?", (row,)))


class AutofeedRunTest(Case):
    async def fire(self):
        await self.bot.timers.run_due(time.time() + 10 ** 7)

    async def error(self, row):
        return await self.db.scalar("SELECT last_error FROM autofeeds WHERE id = ?", (row,))

    async def test_a_repeat_with_no_next_time_posts_once(self):
        for bad in ({"repeat_months": 10 ** 9}, {"repeat_days": 10 ** 10}, {"repeat_minutes": 1}):
            self.channel.sent.clear()
            row = await self.autofeed(self.channel)
            column, value = next(iter(bad.items()))
            await self.db.run(f"UPDATE autofeeds SET {column} = ? WHERE id = ?", (value, row))
            for _ in range(3):
                await self.fire()
            self.assertEqual(len(self.channel.sent), 1, bad)
            self.assertIn("out of range", await self.error(row))

    def test_next_run_answers_none_past_what_a_date_can_hold(self):
        self.assertIsNone(next_run(self.now, (10 ** 9, 0, 0, 0), self.now))
        self.assertIsNone(next_run(self.now, (0, 10 ** 10, 0, 0), self.now))
        self.assertIsNone(next_run(self.now, (0, 0, 0, 1), self.now))
        self.assertIsNone(next_run(253_000_000_000.0, (120, 0, 0, 0), 253_000_000_000.0), "the year runs past 9999")
        self.assertIsNotNone(next_run(self.now, (0, 0, 1, 0), self.now))

    async def test_a_ping_that_couldnt_go_out_shows_on_the_row(self):
        self.role_bot.permissions = FakePermissions("send_messages")
        row = await self.autofeed(self.channel, "role", self.quiet.id)
        await self.db.run("UPDATE autofeeds SET repeat_days = 1 WHERE id = ?", (row,))
        await self.fire()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("ping didn't go out", await self.error(row))

    async def test_everyone_without_the_bots_permission_shows_on_the_row(self):
        self.role_bot.permissions = FakePermissions("send_messages")
        row = await self.autofeed(self.channel, "everyone")
        await self.fire()
        self.assertIn("ping didn't go out", await self.error(row))

    async def test_a_deleted_role_shows_on_the_row(self):
        gone = self.guild.role("Gone", 2)
        row = await self.autofeed(self.channel, "role", gone.id)
        self.guild.roles.remove(gone)
        await self.fire()
        self.assertIn("role no longer exists", await self.error(row))

    async def test_a_ping_that_went_out_leaves_no_note(self):
        row = await self.autofeed(self.channel, "role", self.pings.id)
        await self.db.run("UPDATE autofeeds SET repeat_days = 1 WHERE id = ?", (row,))
        await self.fire()
        self.assertIsNone(await self.error(row))

    async def test_a_kind_of_channel_with_no_messages_says_so(self):
        category = FakeChannel(self.guild, "cat", discord.ChannelType.category)
        row = await self.autofeed(category)
        await self.fire()
        self.assertEqual(await self.error(row), delivery.NOT_POSTABLE)

    async def test_an_archived_thread_is_found_on_discord_rather_than_called_gone(self):
        thread = FakeChannel(self.guild, "thread", discord.ChannelType.public_thread)
        self.guild.channels.remove(thread)  # discord.py drops an archived thread from its lists

        async def fetch(channel_id):
            return thread
        self.guild.fetch_channel = fetch
        row = await self.autofeed(thread)
        await self.fire()
        self.assertEqual(len(thread.sent), 1)
        self.assertIsNone(await self.db.one("SELECT id FROM autofeeds WHERE id = ?", (row,)))


class HeldRoleTest(Case):
    def bot_can_only_manage_roles(self):
        self.role_bot.permissions = FakePermissions("manage_roles", "send_messages")

    def watch(self, role):
        self.edits = []

        async def edit(**kwargs):
            self.edits.append(kwargs["mentionable"])
            role.mentionable = kwargs["mentionable"]
        role.edit = edit

    async def test_the_role_is_on_record_while_its_mentionable_and_cleared_after(self):
        self.bot_can_only_manage_roles()
        self.watch(self.quiet)
        held = []
        real = self.channel.send

        async def send(*args, **kwargs):
            held.append(await self.db.scalar("SELECT COUNT(*) FROM kv WHERE key LIKE 'role_mentionable:%'"))
            return await real(*args, **kwargs)
        self.channel.send = send
        await self.feeds.send_with_mention(self.guild, self.channel, {"content": "x", "embeds": []}, "role", self.quiet.id)
        self.assertEqual(held, [1])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM kv WHERE key LIKE 'role_mentionable:%'"), 0)

    async def test_a_role_left_mentionable_by_a_killed_process_is_turned_back_off(self):
        self.watch(self.quiet)
        self.quiet.mentionable = True
        await self.db.run("INSERT INTO kv (key, value) VALUES (?, '1')", (f"role_mentionable:{self.guild.id}:{self.quiet.id}",))
        await self.feeds.on_guild_available(self.guild)
        self.assertEqual(self.edits, [False])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM kv WHERE key LIKE 'role_mentionable:%'"), 0)


class AlertCommandTest(Case):
    async def follow_youtube(self, channel, fmt="{title}"):
        await self.notify._youtube_store(self.guild.id, CHANNEL, "Sample", channel.id, fmt, None, True, None)

    async def follow_twitch(self, channel, fmt="{name}"):
        await self.notify._twitch_store(self.guild.id, {"id": "111", "login": "sample", "display_name": "Sample"}, channel.id, fmt, None, None)

    async def test_add_checks_channel_format_and_role(self):
        for command, name in ((self.notify.twitch_add, "sample"), (self.notify.youtube_add, "@sample")):
            for args, sentence in (
                ((self.hidden, None, None), NO_SEE), ((self.readonly, None, None), NO_POST),
                ((None, "{everyone} live", None), NO_PING), ((None, f"{self.quiet.mention} live", None), NO_PING), ((None, None, self.quiet), NO_PING),
            ):
                ctx = self.ctx()
                await command.callback(self.notify, ctx, name, args[0], format=args[1], role=args[2])
                self.assertRefused(ctx, sentence)
            ctx = self.ctx()
            await command.callback(self.notify, ctx, name, None, role=self.guild.default_role)
            self.assertIn("{everyone}", ctx.fails[0])
            ctx = self.ctx()
            await command.callback(self.notify, ctx, name, None, role=self.pings)
            self.assertEqual(ctx.fails, [])

    async def test_move_checks_the_new_channel(self):
        await self.follow_twitch(self.channel)
        await self.follow_youtube(self.channel)
        for command, name in ((self.notify.twitch_move, "sample"), (self.notify.youtube_move, CHANNEL)):
            for target, sentence in ((self.hidden, NO_SEE), (self.readonly, NO_POST)):
                ctx = self.ctx()
                await command.callback(self.notify, ctx, name, target)
                self.assertRefused(ctx, sentence)

    async def test_a_new_ping_in_the_text_is_refused_and_a_typo_fix_isnt(self):
        await self.follow_twitch(self.channel, "{everyone} {name}")
        await self.follow_youtube(self.channel, "{title}")
        ctx = self.ctx()
        await self.notify.twitch_format.callback(self.notify, ctx, "sample", format="{everyone} {name} is live")
        self.assertEqual(ctx.fails, [])
        ctx = self.ctx()
        await self.notify.youtube_format.callback(self.notify, ctx, CHANNEL, format="{everyone} {title}")
        self.assertRefused(ctx, NO_PING)

    async def test_a_row_they_cant_see_is_out_of_reach(self):
        await self.follow_twitch(self.hidden)
        await self.follow_youtube(self.hidden)
        for command, args in (
            (self.notify.twitch_remove, ("sample",)), (self.notify.youtube_remove, (CHANNEL,)), (self.notify.twitch_move, ("sample", self.channel)), (self.notify.youtube_move, (CHANNEL, self.channel)),
        ):
            ctx = self.ctx()
            await command.callback(self.notify, ctx, *args)
            self.assertRefused(ctx, NO_SEE)
        ctx = self.ctx()
        await self.notify.twitch_format.callback(self.notify, ctx, "sample", format="x")
        self.assertRefused(ctx, NO_SEE)
        ctx = self.ctx()
        await self.notify.youtube_format.callback(self.notify, ctx, CHANNEL, format="x")
        self.assertRefused(ctx, NO_SEE)

    async def test_game_alerts_check_the_channel(self):
        for target, sentence in ((self.hidden, NO_SEE), (self.readonly, NO_POST)):
            ctx = self.ctx()
            await self.notify._games_alerts(ctx, None, target)
            self.assertRefused(ctx, sentence)
        self.assertIsNone(self.notify.cfg(self.guild.id)["alert_channel"])
        await self.bot.settings.save(self.guild.id, "notifications", {"free_format": "{everyone} free"})
        ctx = self.ctx()
        await self.notify._games_alerts(ctx, None, self.channel)
        self.assertRefused(ctx, NO_PING)


class DashboardTest(Case):
    async def test_a_kind_of_channel_with_no_messages_is_refused(self):
        category = FakeChannel(self.guild, "cat", discord.ChannelType.category)
        forum = FakeChannel(self.guild, "forum", discord.ChannelType.forum)
        client = await self.client()
        for channel in (category, forum):
            response = await client.post(self.path("/autofeeds"), json=self.form(channel))
            self.assertEqual(response.status, 400, await response.text())
            self.assertEqual((await response.json())["fields"]["channel_id"], delivery.NOT_POSTABLE)
            response = await client.post(self.path("/feeds"), json={"name": "n", "role_id": str(self.pings.id), "channel_id": str(channel.id)})
            self.assertEqual(response.status, 400)
            response = await client.post(self.path("/twitch"), json={"name": "sample", "channel_id": str(channel.id)})
            self.assertEqual(response.status, 400)
            response = await client.post(self.path("/youtube"), json={"link": "@sample", "channel_id": str(channel.id)})
            self.assertEqual(response.status, 400)
        voice = FakeChannel(self.guild, "voice", discord.ChannelType.voice)
        response = await client.post(self.path("/autofeeds"), json=self.form(voice))
        self.assertEqual(response.status, 200)

    async def test_a_new_row_needs_a_channel_they_can_post_in(self):
        client = await self.as_manager()
        for path, body in (("/autofeeds", self.form(self.readonly)), ("/feeds", {"name": "n", "role_id": str(self.pings.id), "channel_id": str(self.readonly.id)}), ("/twitch", {"name": "sample", "channel_id": str(self.readonly.id)}), ("/youtube", {"link": "@sample", "channel_id": str(self.readonly.id)})):
            response = await client.post(self.path(path), json=body)
            self.assertEqual(response.status, 400, path)
            self.assertEqual((await response.json())["fields"]["channel_id"], NO_POST)

    async def test_a_typo_fix_keeps_a_channel_they_cant_post_in(self):
        row = await self.autofeed(self.readonly, next_at=self.now + 1000)
        client = await self.as_manager()
        response = await client.put(self.path(f"/autofeeds/{row}"), json=self.form(self.readonly, when=self.now + 1000))
        self.assertEqual(response.status, 200, await response.text())

    async def test_a_saved_role_members_cant_ping_takes_mention_everyone(self):
        client = await self.as_manager()
        response = await client.post(self.path("/autofeeds"), json=self.form(self.channel, str(self.quiet.id)))
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["fields"]["mention"], NO_PING)
        response = await client.post(self.path("/autofeeds"), json=self.form(self.channel, str(self.pings.id)))
        self.assertEqual(response.status, 200)
        for path, body in (("/feeds", {"name": "n", "role_id": str(self.quiet.id), "channel_id": str(self.channel.id)}), ("/twitch", {"name": "sample", "channel_id": str(self.channel.id), "role_id": str(self.quiet.id)}), ("/youtube", {"link": "@sample", "channel_id": str(self.channel.id), "role_id": str(self.quiet.id)})):
            response = await client.post(self.path(path), json=body)
            self.assertEqual(response.status, 400, path)
            self.assertIn(NO_PING, (await response.json())["fields"].values())

    async def test_a_role_an_administrator_saved_stays_through_a_managers_edit(self):
        row = await self.autofeed(self.channel, "role", self.quiet.id)
        await self.db.run("INSERT INTO feeds (guild_id, name, role_id, channel_id, created_at) VALUES (?, 'news', ?, ?, 1)", (self.guild.id, self.quiet.id, self.channel.id))
        client = await self.as_manager()
        response = await client.put(self.path(f"/autofeeds/{row}"), json=self.form(self.channel, str(self.quiet.id), when=self.now + 1000))
        self.assertEqual(response.status, 200, await response.text())
        response = await client.put(self.path("/feeds/news"), json={"role_id": str(self.quiet.id), "channel_id": str(self.channel.id)})
        self.assertEqual(response.status, 200, await response.text())

    async def test_a_hidden_row_shows_nothing_and_cant_be_touched(self):
        autofeed = await self.autofeed(self.hidden)
        await self.db.run("INSERT INTO feeds (guild_id, name, role_id, channel_id, created_at) VALUES (?, 'news', ?, ?, 1)", (self.guild.id, self.pings.id, self.hidden.id))
        await self.notify._twitch_store(self.guild.id, {"id": "111", "login": "s", "display_name": "S"}, self.hidden.id, "secret {name}", None, None)
        await self.notify._youtube_store(self.guild.id, CHANNEL, "Sample", self.hidden.id, "secret {title}", None, True, None)
        client = await self.as_manager()
        listed = await (await client.get(self.path("/autofeeds"))).json()
        self.assertTrue(listed["autofeeds"][0]["hidden"])
        self.assertTrue(listed["feeds"][0]["hidden"])
        self.assertNotIn("secret plans", json.dumps(listed))
        for path in ("/twitch", "/youtube"):
            listed = await (await client.get(self.path(path))).json()
            self.assertTrue(listed["rows"][0]["hidden"])
            self.assertNotIn("secret", json.dumps(listed))
        for path in (f"/autofeeds/{autofeed}", "/feeds/news", "/twitch/111", f"/youtube/{CHANNEL}"):
            response = await client.delete(self.path(path))
            self.assertEqual(response.status, 403, path)
        for path in (f"/autofeeds/{autofeed}/test", "/twitch/111/test", f"/youtube/{CHANNEL}/test"):
            response = await client.post(self.path(path))
            self.assertEqual(response.status, 403, path)
        self.assertEqual(self.hidden.sent, [])

    async def test_a_test_post_needs_the_right_to_post_there(self):
        autofeed = await self.autofeed(self.readonly)
        await self.notify._twitch_store(self.guild.id, {"id": "111", "login": "s", "display_name": "S"}, self.readonly.id, "{name}", None, None)
        await self.notify._youtube_store(self.guild.id, CHANNEL, "Sample", self.readonly.id, "{title}", None, True, None)
        client = await self.as_manager()
        for path in (f"/autofeeds/{autofeed}/test", "/twitch/111/test", f"/youtube/{CHANNEL}/test"):
            response = await client.post(self.path(path))
            self.assertEqual(response.status, 403, path)
            self.assertEqual((await response.json())["error"], NO_POST)
        self.assertEqual(self.readonly.sent, [])

    async def test_the_test_button_fills_in_tags_as_the_real_post_does(self):
        row = await self.feeds._insert(self.guild.id, self.channel.id, "silent", None, {"content": "{channel(id)} {server(id)}", "embeds": [], "buttons": []}, self.now + 1000, (0, 0, 0, 0), None, None)
        client = await self.client()
        response = await client.post(self.path(f"/autofeeds/{row}/test"))
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.channel.sent[0].content, f"{self.channel.id} {self.guild.id}")

    async def test_the_role_picker_refuses_everyone_for_an_alert(self):
        self.guild.default_role.id = self.guild.id
        client = await self.client()
        response = await client.post(self.path("/twitch"), json={"name": "sample", "channel_id": str(self.channel.id), "role_id": str(self.guild.id)})
        self.assertEqual(response.status, 400)
        self.assertIn("{everyone}", (await response.json())["fields"]["role_id"])

    async def test_the_settings_form_checks_the_alert_role(self):
        other_quiet = self.guild.role("Quiet 2", 2)
        await self.bot.settings.save(self.guild.id, "notifications", {"alert_channel": self.channel.id, "alert_role": self.quiet.id})
        client = await self.as_manager()
        response = await client.put(self.path("/settings/notifications"), json={"alert_role": str(self.quiet.id), "free_format": "hello"})
        self.assertEqual(response.status, 200, await response.text())
        response = await client.put(self.path("/settings/notifications"), json={"alert_role": str(other_quiet.id)})
        self.assertEqual(response.status, 400, await response.text())
        self.assertEqual((await response.json())["error"], NO_PING)
        response = await client.put(self.path("/settings/notifications"), json={"alert_role": str(self.pings.id)})
        self.assertEqual(response.status, 200, await response.text())

    async def test_the_pages_get_their_limits(self):
        client = await self.client()
        self.assertEqual((await (await client.get(self.path("/twitch"))).json())["limit"], 200)
        self.assertEqual((await (await client.get(self.path("/youtube"))).json())["limit"], 200)


class ListLengthTest(Case):
    async def test_a_long_list_says_how_many_were_left_out(self):
        for index in range(200):
            await self.autofeed(self.channel)
        ctx = self.context(self.admin)
        await self.feeds.autofeeds.callback(self.feeds, ctx)
        description = ctx.sent[0]["embed"].description
        self.assertIn("more. The dashboard lists them all.", description)
        self.assertLessEqual(len(description), 4096)

    def test_lines_are_never_cut_partway(self):
        made = delivery.clipped(["x" * 300] * 20)
        self.assertTrue(all(line in ("x" * 300, "And 8 more. The dashboard lists them all.") for line in made.split("\n")), made[-60:])
        self.assertEqual(delivery.clipped([]), "")


class AlertPingTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.role_bot.permissions = FakePermissions("manage_roles", "send_messages")
        self.edits = []

        async def edit(**kwargs):
            self.edits.append(kwargs["mentionable"])
            self.quiet.mentionable = kwargs["mentionable"]
        self.quiet.edit = edit
        await self.notify._twitch_store(self.guild.id, {"id": "111", "login": "s", "display_name": "S"}, self.channel.id, "{name}", self.quiet.id, None)
        self.web.answer(f"{TWITCH_API}/streams", {"data": [{"id": "9", "user_id": "111", "user_login": "s", "user_name": "S"}]})

    async def error(self):
        return await self.db.scalar("SELECT last_error FROM twitch_subs")

    async def test_a_twitch_alert_makes_the_role_mentionable_for_the_send(self):
        seen = []
        real = self.channel.send

        async def send(*args, **kwargs):
            seen.append(self.quiet.mentionable)
            return await real(*args, **kwargs)
        self.channel.send = send
        await self.notify.poll_twitch()
        self.assertEqual(seen, [True])
        self.assertEqual(self.edits, [True, False])
        self.assertIsNone(await self.error())

    async def test_a_role_above_the_bot_posts_and_says_the_ping_failed(self):
        self.quiet.position = 50
        await self.notify.poll_twitch()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("ping didn't go out", await self.error())

    async def test_mentions_go_back_off_when_the_send_fails(self):
        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.channel.send = refuse
        await self.notify.poll_twitch()
        self.assertEqual(self.edits, [True, False])
        self.assertIn("Discord refused", await self.error())

    async def test_everyone_without_the_bots_permission_says_so(self):
        await self.db.run("UPDATE twitch_subs SET fmt = '{everyone} {name}', role_id = NULL")
        await self.notify.poll_twitch()
        self.assertIn("ping didn't go out", await self.error())
