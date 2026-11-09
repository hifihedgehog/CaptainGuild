"""What statistics data must never do: let a key or a page show a channel or a thread the person can't see, count a message or a reaction
twice, put a name back on counts someone took it off, or hand one person another's rows."""
from __future__ import annotations

import asyncio
import datetime
import json
import time
import types
from unittest import mock

import discord
from aiohttp.test_utils import TestClient, TestServer

from captainguild.core import access
from captainguild.core.ops import DiscordOps
from captainguild.core.stats import history, merge, store
from captainguild.core.stats import timeframe as tf
from captainguild.modules.backup import Backup
from captainguild.modules.logs import Logs
from captainguild.modules.moderation import Moderation
from captainguild.modules.mydata import MyData
from captainguild.modules.reports import Reports
from captainguild.modules import statistics_commands, statistics_data
from captainguild.modules.statistics import Statistics
from captainguild.modules.statistics_data import _visible_to_viewers
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests import fakes
from tests import test_statistics_api as tsa
from tests import test_statistics_data as tsd

DAY = 86400


async def client_as(test, member, level=None):
    """A dashboard client signed in as `member`. Without `level`, the bot's own rule decides their access from their permissions."""
    if level is not None:
        test.bot.ops.access_for = lambda guild_id, user_id: level if guild_id == test.guild.id else None
    app = build_app(test.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(member.id, member.name, None))
    client = TestClient(TestServer(app))
    await client.start_server()
    test._clients.append(client)
    return client


def admin_only(member):
    return fakes.FakePermissions("administrator") if member.guild_permissions.administrator else fakes.FakePermissions()


class AdminOnly(fakes.FakeChannel):
    """A channel only Administrators can see."""

    def permissions_for(self, member):
        return admin_only(member)


class AdminOnlyHistory(tsd.HistoryChannel):
    def permissions_for(self, member):
        return admin_only(member)


class OpenHistory(tsd.HistoryChannel):
    """A channel everyone can see."""

    def permissions_for(self, member):
        return fakes.FakePermissions("view_channel", "send_messages", "read_message_history")


class ListingFails(tsd.HistoryChannel):
    """A channel whose archived threads Discord won't list right now (a 503 after discord.py's own retries)."""

    async def archived_threads(self, *, private=False, joined=False, limit=100, before=None):
        raise tsd.http_error(discord.DiscordServerError, 503, "Service Unavailable")
        yield  # an async generator, as discord.py's is


def manager_in(guild):
    """Manage Server and ordinary channel access, without Administrator or Manage Threads."""
    role = guild.role("Manager", 4, "manage_guild", "view_channel", "read_message_history", "send_messages")
    return fakes.FakeMember(guild, "manager", role)


# ---- the statistics API ----

class ApiGuards(tsa.ApiTest):
    modules = [*tsd.Privacy.modules, Backup]  # every table My data reads

    async def asyncSetUp(self):
        await super().asyncSetUp()
        g = self.guild.id
        self.manager = manager_in(self.guild)
        self.secret = AdminOnly(self.guild, "staff-secret")
        await self.message_db.run("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, 'staff-secret', 0, NULL, ?, NULL)",
                                  (g, self.secret.id, tsa.T0 - 100 * DAY))
        await self.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 9)",
                                  (g, tsa.slot(tsa.T0 + 2 * tsa.HOUR), self.secret.id, self.member.id))
        self.bot.ops = DiscordOps(self.bot)  # access levels from each person's own permissions

    async def test_a_manager_who_cant_see_a_channel_cant_read_it_through_the_api(self):
        manager = await client_as(self, self.manager)
        info = await (await manager.get(self.path("/info"))).json()
        shown = next(row for row in info["channels"] if str(row["id"]) == str(self.secret.id))
        page = await (await manager.get(self.path("/statistics/data/api"))).json()
        made = await manager.post(self.path("/statistics/data/api/key"))
        removed = await manager.delete(self.path("/statistics/data/api/key"))
        self.assertEqual(shown["name"], "Hidden channel")
        self.assertEqual(made.status, 403, await made.text())
        self.assertEqual(removed.status, 403)
        self.assertFalse(page["is_admin"])
        self.assertEqual(page["note"], "Only the server owner or an Administrator can see this.")
        self.assertEqual((await self.call("/messages/sums", tsa.window()))[0], 200, "the Administrator's key still works")

    async def test_a_key_reads_only_what_its_maker_can_see_now(self):
        tops = [(row["username"], row["count"]) for row in (await self.ok("/messages/tops/members", tsa.window(**{"whitelist_channels[]": self.secret.id, "full": "true"})))]
        self.assertEqual(tops, [("member", 9)], "an Administrator's key reads every channel")
        self.assertEqual((await self.ok("/messages/sums", tsa.window()))["count"], 32)
        self.admin.roles = [self.guild.default_role, self.guild.role("Helper", 3, "view_channel", "read_message_history")]  # no longer an Administrator
        names = [row["name"] for row in await self.ok("/channels")]
        tops = await self.ok("/messages/tops/members", tsa.window(**{"whitelist_channels[]": self.secret.id}))
        status, _, _ = await self.call(f"/channels/{self.secret.id}")
        total = (await self.ok("/messages/sums", tsa.window()))["count"]
        self.assertNotIn("staff-secret", names)
        self.assertEqual(tops, [])
        self.assertEqual(status, 404)
        self.assertEqual(total, 3 + 2 + 1 + 4 + 5 + 7 + 1, "the hidden channel's 9 aren't in the total either")
        self.guild.members.remove(self.admin)
        status, body, _ = await self.call("/messages/sums", tsa.window())
        self.assertEqual(status, 403, body)

    async def test_a_wrong_key_never_reads_the_database_and_a_deleted_key_stops_at_once(self):
        self.assertEqual((await self.call("/messages/sums", tsa.window()))[0], 200)  # the right key, which reads the known keys once
        with mock.patch.object(self.db, "one", side_effect=AssertionError("a wrong key read the database")) as one:
            self.assertEqual((await self.call("/messages/sums", tsa.window(), key="cgstats_" + "x" * 43))[0], 401)
        one.assert_not_called()
        await self.db.run("DELETE FROM stat_api_keys WHERE guild_id = ?", (self.guild.id,))  # as My data deletes it
        self.assertEqual((await self.call("/messages/sums", tsa.window()))[0], 401)

    async def test_no_parameter_reaches_another_servers_rows(self):
        other, channel, person = 424242424242424242, 1_300_000_000_000_000_001, 1_300_000_000_000_000_002
        db = self.message_db
        await db.run("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, 'their-secret', 0, NULL, 1, NULL)", (other, channel))
        await db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 77)", (other, tsa.slot(tsa.T0 + tsa.HOUR), channel, person))
        await db.run("INSERT INTO stat_activity_names (guild_id, activity_id, kind, application_id, name, verified, first_seen) VALUES (?, 5, 0, NULL, 'Their game', 1, 1)", (other,))
        await db.run("INSERT INTO stat_invites (guild_id, code, inviter_id, uses) VALUES (?, 'theirs', ?, 9)", (other, person))
        reads = [
            await self.call("/messages/series", tsa.window(**{"whitelist_channels[]": channel})),
            await self.call("/messages/series", tsa.window(**{"whitelist_members[]": person})),
            await self.call("/messages/tops/members", tsa.window(**{"select[]": person, "full": "true"})),
            await self.call(f"/channels/{channel}"),
            await self.call("/channels", [("ids[]", str(channel))]),
            await self.call(f"/invites/{tsa.devapi.invite_number('theirs')}"),
            await self.call("/v1/activities"),
            await self.call("/messages/sums", tsa.window(), guild=other),
        ]
        text = json.dumps([body for _, body, _ in reads])
        self.assertNotIn("their-secret", text)
        self.assertNotIn("Their game", text)
        self.assertNotIn("77", text)
        self.assertEqual([status for status, _, _ in reads], [200, 200, 200, 404, 200, 404, 200, 401])

    async def test_a_key_stops_working_when_the_bot_leaves_the_server_and_its_maker_can_still_delete_it(self):
        self.bot.fake_guilds.remove(self.guild)
        status, body, _ = await self.call("/messages/sums", tsa.window())
        self.assertIn(status, (401, 403), "the key still reads the server's statistics")
        mine = await client_as(self, self.admin)
        overview = await (await mine.get("/api/my-data")).json()
        here = next(server for server in overview["servers"] if str(server["id"]) == str(self.guild.id))
        self.assertIn("statapikeys", [section["key"] for section in here["sections"]])
        removed = await mine.post("/api/my-data/delete", json={"guild_id": str(self.guild.id), "section": "statapikeys"})
        self.assertEqual(removed.status, 200, await removed.text())
        self.assertIsNone(await self.db.one("SELECT 1 FROM stat_api_keys WHERE guild_id = ?", (self.guild.id,)))
        download = await (await mine.get("/api/my-data/download")).json()
        self.assertNotIn("key_hash", json.dumps(download))


# ---- help forums ----

class ForumGuards(tsd.DataTest):
    def forum(self, kind, name):
        channel = kind(self.guild, name, discord.ChannelType.forum)
        self.known[channel.id] = channel
        return channel

    def answer(self, thread, author, seconds_after):
        at = thread.created_at + datetime.timedelta(seconds=seconds_after)
        return types.SimpleNamespace(guild=self.guild, channel=thread, author=author, id=tsd.snowflake(at), created_at=at)

    async def test_the_forums_trend_leaves_out_forums_the_viewer_cant_see(self):
        help_forum = self.forum(OpenHistory, "help")
        staff_forum = self.forum(AdminOnlyHistory, "mod-appeals")
        for forum, count in ((help_forum, 1), (staff_forum, 3)):
            for number in range(count):
                await self.stats._forum_post_made(self.thread(forum, f"post {number}", self.member, tsd.ago(3600)))
        viewer = await client_as(self, self.mod, "staff")
        page = await (await viewer.get(self.path("/statistics/insights/forums?days=7"))).json()
        listed = sum(row["posts"] for row in page["forums"])
        self.assertEqual(listed, 1)
        self.assertEqual(sum(page["posted"]), listed, "the trend counts posts in a forum the viewer can't see")

    async def test_a_forum_kept_out_of_statistics_records_no_posts(self):
        forum = self.forum(OpenHistory, "staff-help")
        await self.bot.settings.save(self.guild.id, "statistics", {"untracked_channels": [forum.id]})
        post = self.thread(forum, "question", self.member, tsd.ago(3600))
        await self.stats._forum_post_made(post)
        await self.stats._forum_answer(self.answer(post, self.admin, 600))
        self.assertIsNone(self.stats._place(self.guild, post, "messages"))
        self.assertEqual(await self.rows("SELECT thread_id = ?, owner_id, replier_id FROM stat_forum_posts", (post.id,)), [],
                         "an untracked forum still records who posted and who answered")


# ---- recounts ----

class RecountGuards(tsd.DataTest):
    def failing(self, name, kind=discord.ChannelType.text):
        channel = ListingFails(self.guild, name, kind)
        self.known[channel.id] = channel
        return channel

    async def test_a_thread_list_discord_wont_give_keeps_the_threads_counts(self):
        general = self.failing("general")
        inside = tsd.ago(8 * DAY)
        general.say(self.member, inside, "hello")
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, 12)",
                                      (self.guild.id, tf.slot_of(inside.timestamp()), general.id, self.member.id, store.THREAD))
        client = await self.client()
        page = await self.start(client, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        self.assertEqual(page["recount"]["state"], history.REVIEW)
        problem = next(item for item in page["errors"] if item["name"] == "general")
        self.assertIn("didn't list", problem["error"])
        skipped = await client.post(self.path("/statistics/data/recount/skip"))
        self.assertEqual(skipped.status, 200, await skipped.text())
        await self.approve(client)
        self.assertEqual((await self.counts(general.id)).get((self.member.id, store.THREAD)), 12,
                         "the thread messages recorded live are gone, and no thread was read")

    async def test_a_thread_list_that_works_on_resume_reads_its_threads(self):
        general = self.failing("general")
        thread = self.thread(general, "found later", self.member, tsd.ago(9 * DAY), archived_at=tsd.ago(6 * DAY))
        thread.say(self.member, tsd.ago(8 * DAY), "in the thread")
        client = await self.client()
        page = await self.start(client, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        self.assertEqual(page["recount"]["state"], history.REVIEW)
        general.__class__ = tsd.HistoryChannel  # Discord lists the threads again
        general.archived = [thread]
        resumed = await client.post(self.path("/statistics/data/recount/resume"))
        self.assertEqual(resumed.status, 200, await resumed.text())
        await self.settle()
        page = await (await client.get(self.path("/statistics/data/recount"))).json()
        self.assertEqual(page["recount"]["state"], history.APPROVAL, page)
        await self.approve(client)
        self.assertEqual(await self.counts(general.id), {(self.member.id, store.THREAD): 1})

    async def test_a_forum_whose_archived_posts_discord_wont_list_keeps_their_counts(self):
        forum = self.failing("help", discord.ChannelType.forum)
        inside = tsd.ago(8 * DAY)
        active = self.thread(forum, "open question", self.member, tsd.ago(9 * DAY))
        active.say(self.member, inside, "still open")
        self.guild.active_threads = mock.AsyncMock(return_value=[active])
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, 40)",
                                      (self.guild.id, tf.slot_of(inside.timestamp()), forum.id, self.admin.id, store.THREAD))
        client = await self.client()
        page = await self.start(client, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        self.assertEqual(page["recount"]["state"], history.REVIEW)
        await client.post(self.path("/statistics/data/recount/skip"))
        await self.approve(client)
        self.assertEqual(sum((await self.counts(forum.id)).values()), 40, "the archived posts' recorded messages are deleted and none of them were read")

    async def test_active_threads_discord_wont_list_stop_the_recount_before_it_reads(self):
        general = self.history_channel("general")
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, 5)",
                                      (self.guild.id, tf.slot_of(tsd.ago(8 * DAY).timestamp()), general.id, self.member.id, store.THREAD))
        self.guild.active_threads = mock.AsyncMock(side_effect=tsd.http_error(discord.DiscordServerError, 503, "Service Unavailable"))
        client = await self.client()
        page = await self.start(client, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        self.assertEqual(page["recount"]["state"], history.REVIEW)
        self.assertIn("active threads", page["recount"]["note"])
        self.assertEqual(await self.counts(general.id), {(self.member.id, store.THREAD): 5})

    async def test_a_thread_discord_says_is_gone_waits_for_review(self):
        general = self.history_channel("general")
        thread = self.thread(general, "deleted since", self.member, tsd.ago(9 * DAY), archived_at=tsd.ago(6 * DAY))
        thread.refuse = tsd.http_error(discord.NotFound, 404, "Unknown Channel")
        general.archived = [thread]
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, 6)",
                                      (self.guild.id, tf.slot_of(tsd.ago(8 * DAY).timestamp()), general.id, self.member.id, store.THREAD))
        client = await self.client()
        page = await self.start(client, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        self.assertEqual(page["recount"]["state"], history.REVIEW)
        self.assertIn("gone", next(item for item in page["errors"] if item["name"] == "deleted since")["error"])
        await client.post(self.path("/statistics/data/recount/skip"))
        await self.approve(client)
        self.assertEqual(await self.counts(general.id), {(self.member.id, store.THREAD): 6}, "leaving it out keeps the channel's recorded counts")

    async def test_a_recount_after_a_merge_doesnt_count_the_merged_messages_twice(self):
        ticket = self.history_channel("ticket-1")
        inside = tsd.ago(8 * DAY)
        for _ in range(3):
            ticket.say(self.member, inside, "help")
        db = self.bot.message_db
        await db.run("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, 'ticket-1', 0, NULL, ?, NULL)",
                     (self.guild.id, ticket.id, time.time() - 30 * DAY))
        await db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 3)",
                     (self.guild.id, tf.slot_of(inside.timestamp()), ticket.id, self.member.id))
        client = await self.client()
        made = await client.post(self.path("/statistics/data/merged"), json={"name": "Tickets"})
        target = next(row for row in (await made.json())["aggregates"] if row["name"] == "Tickets")["id"]
        merged = await client.post(self.path("/statistics/data/merged/merge"), json={"target_id": str(target), "kinds": "messages", "channels": [str(ticket.id)], "confirm": True})
        self.assertEqual(merged.status, 200, await merged.text())
        await self.settle()
        first, end = tf.slot_of(tsd.ago(10 * DAY).timestamp()), tf.slot_of(tsd.ago(5 * DAY).timestamp())
        period = "SELECT COALESCE(SUM(count), 0) FROM stat_messages WHERE guild_id = ? AND slot >= ? AND slot < ?"
        page = await self.start(client, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        self.assertIn("merged", page["recount"]["left_out"])
        await self.approve(client)
        self.assertEqual((await self.rows(period, (self.guild.id, first, end)))[0][0], 3, "the merged channel keeps the 3 and the recount adds them again under ticket-1")

    async def test_a_category_that_used_to_merge_its_channels_keeps_them_out(self):
        category = fakes.FakeChannel(self.guild, "Tickets", discord.ChannelType.category)
        ticket = self.history_channel("ticket-2", category=category)
        ticket.say(self.member, tsd.ago(8 * DAY), "help")
        client = await self.client()
        made = await client.post(self.path("/statistics/data/merged"), json={"name": "Tickets"})
        target = next(row for row in (await made.json())["aggregates"] if row["name"] == "Tickets")["id"]
        await client.patch(self.path(f"/statistics/data/merged/{target}"), json={"auto_categories": [str(category.id)], "kinds": "messages"})
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 1)",
                                      (self.guild.id, tf.slot_of(tsd.ago(8 * DAY).timestamp()), target, self.member.id))
        await self.bot.message_db.run("UPDATE stat_auto_merges SET from_slot = ? WHERE guild_id = ?", (tf.slot_of(tsd.ago(20 * DAY).timestamp()), self.guild.id))
        edited = await client.patch(self.path(f"/statistics/data/merged/{target}"), json={"auto_categories": []})
        self.assertEqual(edited.status, 200, await edited.text())
        page = await self.start(client, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        self.assertIn("merged", page["recount"]["left_out"])
        await self.approve(client)
        self.assertEqual(await self.counts(ticket.id), {}, "the message the merged channel holds would count twice")
        self.assertEqual(await self.counts(target), {(self.member.id, 0): 1})

    async def test_a_reaction_counted_live_isnt_counted_again(self):
        general = self.history_channel("general")
        message = general.say(self.member, tsd.ago(8 * DAY), "nice")
        thumbs = "\U0001F44D"
        message.reactions = [types.SimpleNamespace(emoji=thumbs, count=1, normal_count=1, burst_count=0, me=False, me_burst=False)]
        later = tf.slot_of(tsd.ago(6 * DAY).timestamp())  # the reaction came two days after the message, inside the same period
        await self.bot.message_db.run("INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1)",
                                      (self.guild.id, later, general.id, self.admin.id, self.member.id, store.unicode_key(thumbs), store.REACTION))
        client = await self.client()
        await self.start(client, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        await self.approve(client)
        rows = await self.rows("SELECT slot, user_id, count FROM stat_emoji WHERE guild_id = ? AND emoji = ? ORDER BY slot", (self.guild.id, store.unicode_key(thumbs)))
        self.assertEqual(sum(row[2] for row in rows), 1, "the live reaction stays at its own quarter hour and the recount adds Discord's total again at the message's")

    async def test_reactions_from_before_any_were_counted_live_are_added(self):
        general = self.history_channel("general")
        message = general.say(self.member, tsd.ago(40 * DAY), "old")
        thumbs = "\U0001F44D"
        message.reactions = [types.SimpleNamespace(emoji=thumbs, count=2, normal_count=2, burst_count=0, me=False, me_burst=False)]
        await self.bot.message_db.run("INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1)",
                                      (self.guild.id, tf.slot_of(tsd.ago(6 * DAY).timestamp()), general.id, self.admin.id, self.member.id, store.unicode_key(thumbs), store.REACTION))
        client = await self.client()
        await self.start(client, tsd.ago(50 * DAY), tsd.ago(30 * DAY))
        await self.approve(client)
        rows = await self.rows("SELECT user_id, count FROM stat_emoji WHERE guild_id = ? AND emoji = ? AND slot < ?",
                               (self.guild.id, store.unicode_key(thumbs), tf.slot_of(tsd.ago(30 * DAY).timestamp())))
        self.assertEqual(rows, [(0, 2)], "a period before live counting began gets Discord's reaction totals")

    async def test_a_private_threads_name_stays_hidden_from_someone_who_cant_see_it(self):
        manager = manager_in(self.guild)
        general = self.history_channel("general")
        thread = self.thread(general, "ban-appeal-alice", self.admin, tsd.ago(9 * DAY), archived_at=tsd.ago(6 * DAY), private=True)
        thread.say(self.admin, tsd.ago(8 * DAY), "appeal")
        thread.permissions_for = lambda member: member.guild_permissions
        general.archived_private = [thread]
        admin = await self.client()
        await self.start(admin, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        self.assertIn("ban-appeal-alice", [item["name"] for row in (await (await admin.get(self.path("/statistics/data/recount"))).json())["channels"] for item in row["threads"]])
        viewer = await client_as(self, manager)
        page = await (await viewer.get(self.path("/statistics/data/recount"))).json()
        names = [item["name"] for row in page["channels"] for item in row["threads"]]
        self.assertFalse(access.can_view(manager, thread))
        self.assertNotIn("ban-appeal-alice", names, "a private thread's name reaches someone Discord doesn't show it to")
        self.assertIn("Hidden channel", names)

    async def test_the_quarter_hour_still_running_stays_as_counted_live(self):
        general = self.history_channel("general")
        now = time.time()
        general.say(self.member, tsd.ago(2 * 3600), "earlier")
        general.say(self.member, tsd.ago(1), "this quarter hour")
        running = tf.slot_of(now)
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 4)",
                                      (self.guild.id, running, general.id, self.member.id))
        client = await self.client()
        await self.start(client, tsd.ago(3 * 3600), datetime.datetime.fromtimestamp(now, datetime.timezone.utc))
        await self.approve(client)
        rows = dict(await self.rows("SELECT slot, SUM(count) FROM stat_messages WHERE guild_id = ? AND channel_id = ? GROUP BY slot", (self.guild.id, general.id)))
        self.assertEqual(rows.get(running), 4, "the running quarter hour keeps what live counting has, untouched by the recount")
        self.assertEqual(sum(rows.values()), 5)

    async def test_channels_in_a_category_merged_now_stay_out(self):
        category = fakes.FakeChannel(self.guild, "Tickets", discord.ChannelType.category)
        ticket = self.history_channel("ticket-3", category=category)
        ticket.say(self.member, tsd.ago(8 * DAY), "help")
        client = await self.client()
        made = await client.post(self.path("/statistics/data/merged"), json={"name": "Tickets"})
        target = next(row for row in (await made.json())["aggregates"] if row["name"] == "Tickets")["id"]
        await client.patch(self.path(f"/statistics/data/merged/{target}"), json={"auto_categories": [str(category.id)], "kinds": "messages"})
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 1)",
                                      (self.guild.id, tf.slot_of(tsd.ago(8 * DAY).timestamp()), target, self.member.id))
        page = await self.start(client, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        self.assertIn("merged channel", page["recount"]["left_out"])
        await self.approve(client)
        self.assertEqual(await self.counts(ticket.id), {})
        self.assertEqual(await self.counts(target), {(self.member.id, 0): 1})

    async def test_a_recount_of_every_channel_covers_only_what_its_starter_can_see(self):
        manager = manager_in(self.guild)
        open_channel = self.history_channel("open")
        secret = AdminOnlyHistory(self.guild, "mod-chat")
        self.known[secret.id] = secret
        open_channel.say(self.member, tsd.ago(8 * DAY), "hi")
        secret.say(self.admin, tsd.ago(8 * DAY), "staff only")
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 9)",
                                      (self.guild.id, tf.slot_of(tsd.ago(8 * DAY).timestamp()), secret.id, self.admin.id))
        viewer = await client_as(self, manager, "full")
        page = await self.start(viewer, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        self.assertNotIn(secret.id, [int(row["channel_id"]) for row in page["channels"]])
        await self.approve(viewer)
        self.assertEqual(await self.counts(secret.id), {(self.admin.id, 0): 9})
        self.assertEqual(await self.counts(open_channel.id), {(self.member.id, 0): 1})


# ---- merged channels ----

class MergeGuards(tsd.DataTest):
    async def test_a_merge_refuses_a_channel_the_person_cant_see(self):
        manager = manager_in(self.guild)
        secret = AdminOnlyHistory(self.guild, "mod-chat")
        self.known[secret.id] = secret
        db = self.bot.message_db
        await db.run("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, 'mod-chat', 0, NULL, ?, NULL)",
                     (self.guild.id, secret.id, time.time() - 30 * DAY))
        await db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 9)",
                     (self.guild.id, tf.slot_of(time.time() - 3 * DAY), secret.id, self.admin.id))
        admin = await self.client()
        made = await admin.post(self.path("/statistics/data/merged"), json={"name": "Old stuff"})
        target = next(row for row in (await made.json())["aggregates"] if row["name"] == "Old stuff")["id"]
        viewer = await client_as(self, manager)
        picked = await viewer.post(self.path("/statistics/data/recount"), json={"start": time.time() - 9 * DAY, "end": time.time() - DAY, "channels": [str(secret.id)]})
        self.assertEqual(picked.status, 400)
        for form in ({"channels": [str(secret.id)]}, {"patterns": ["mod-*"]}):
            merged = await viewer.post(self.path("/statistics/data/merged/merge"), json={"target_id": str(target), "kinds": "messages", "confirm": True, **form})
            preview = await viewer.post(self.path("/statistics/data/merged/preview"), json={"target_id": str(target), "kinds": "messages", **form})
            self.assertEqual(merged.status, 400, form)
            self.assertEqual(preview.status, 400, form)
        await self.settle()
        self.assertEqual(await self.counts(target), {}, "the merge takes a channel the person can't see, which the recount form refuses")

    async def test_a_new_channel_in_a_category_merged_both_ways_isnt_listed_on_its_own(self):
        category = fakes.FakeChannel(self.guild, "Tickets", discord.ChannelType.category)
        admin = await self.client()
        made = await admin.post(self.path("/statistics/data/merged"), json={"name": "Tickets"})
        target = next(row for row in (await made.json())["aggregates"] if row["name"] == "Tickets")["id"]
        edited = await admin.patch(self.path(f"/statistics/data/merged/{target}"), json={"auto_categories": [str(category.id)], "kinds": "both"})
        self.assertEqual(edited.status, 200, await edited.text())
        ticket = fakes.FakeChannel(self.guild, "ticket-0042", category=category)
        await self.stats.on_guild_channel_create(ticket)
        self.assertEqual(await self.rows("SELECT name FROM stat_channels WHERE guild_id = ? AND channel_id = ?", (self.guild.id, ticket.id)), [],
                         "every ticket channel joins the channel list, which the warning past 1,000 is about")
        self.stats._note_channels(self.guild, time.time())
        self.assertEqual(await self.rows("SELECT name FROM stat_channels WHERE guild_id = ? AND channel_id = ?", (self.guild.id, ticket.id)), [], "nor at catch-up")
        lone = fakes.FakeChannel(self.guild, "lobby")
        await self.stats.on_guild_channel_create(lone)
        self.assertEqual(await self.rows("SELECT name FROM stat_channels WHERE guild_id = ? AND channel_id = ?", (self.guild.id, lone.id)), [("lobby",)])


# ---- scheduled reports ----

class ReportGuards(tsd.DataTest):
    async def test_a_channel_hidden_from_readers_stays_hidden_when_the_owner_has_no_roles(self):
        class VerifiedOnly(fakes.FakeChannel):
            """Hidden from @everyone, open to anyone holding a role: a server with a verification gate."""

            def permissions_for(self, member):
                held = member.guild_permissions.administrator or any(not role.is_default() for role in member.roles)
                return fakes.FakePermissions("view_channel", "send_messages") if held else fakes.FakePermissions()
        public, chat = tsd.OpenChannel(self.guild, "announcements"), VerifiedOnly(self.guild, "verified-chat")
        self.owner.roles = [self.guild.default_role]  # an owner who holds no roles, listed before the members
        fakes.FakeMember(self.guild, "newcomer")  # not verified yet: reads announcements, can't see verified-chat
        self.assertFalse(_visible_to_viewers(self.guild, public, chat), "the owner's answer is cached for everyone holding the same roles")


# ---- My data ----

class MyDataGuards(tsd.DataTest):
    modules = tsd.Privacy.modules

    async def test_the_download_holds_no_one_elses_join_or_reaction(self):
        other = fakes.FakeMember(self.guild, "invited")
        db = self.bot.message_db
        await db.run("INSERT INTO stat_joins (guild_id, user_id, at, code, inviter_id, account_created, facts, role_met_at, left_at) VALUES (?, ?, 1000, 'abc', ?, 500, ?, NULL, 2000)",
                     (self.guild.id, other.id, self.member.id, store.JOIN_NO_AVATAR))
        await db.run("INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) VALUES (?, ?, 1, ?, ?, 'u:x', 1, 0, 1)",
                     (self.guild.id, tf.slot_of(time.time() - DAY), other.id, self.member.id))
        await db.run("INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) VALUES (?, ?, 1, ?, ?, 'u:y', 1, 0, 1)",
                     (self.guild.id, tf.slot_of(time.time() - DAY), self.member.id, other.id))
        await db.run("INSERT INTO stat_forum_posts (guild_id, thread_id, forum_id, owner_id, created_at, replier_id) VALUES (?, 7, 8, ?, 1, ?)", (self.guild.id, self.member.id, other.id))
        viewer = await client_as(self, self.member)
        download = await (await viewer.get("/api/my-data/download")).json()
        joins = download["tables"].get("stat_joins.inviter_id", [])
        reactions = download["tables"].get("stat_emoji.target_id", []) + download["tables"].get("stat_emoji.user_id", [])
        self.assertEqual(len(joins), 1)
        self.assertEqual(len(reactions), 2)
        self.assertNotIn(str(other.id), json.dumps(download), "another person's join, leave and reaction come out in the download")
        self.assertNotIn("account_created", joins[0])

    async def test_after_a_failed_move_the_page_still_offers_a_retry(self):
        db = self.bot.message_db
        await db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 1, ?, 0, 5)", (self.guild.id, tf.slot_of(time.time() - DAY), self.member.id))
        viewer = await client_as(self, self.member)
        with mock.patch.object(store, "anonymize", side_effect=RuntimeError("the disk is full")):
            failed = await viewer.post("/api/my-data/statistics", json={"private": True})
        self.assertEqual(failed.status, 500)
        state = await (await viewer.get("/api/my-data/statistics")).json()
        self.assertTrue(state["private"])
        self.assertTrue(state["pending"])
        self.assertTrue(state["can_change"], "after a reload nothing on the page tries the move again for a day")
        finished = await viewer.post("/api/my-data/statistics", json={"private": True})
        self.assertEqual(finished.status, 200, await finished.text())
        self.assertFalse((await finished.json())["pending"])
        self.assertEqual(await self.rows("SELECT COUNT(*) FROM stat_messages WHERE user_id = ?", (self.member.id,)), [(0,)])


class RouteGuards(tsd.DataTest):
    async def test_a_report_path_with_a_malformed_id_is_not_found_after_the_access_check(self):
        client = await self.client()
        self.assertEqual((await client.delete(self.path("/statistics/data/reports/999"))).status, 404)
        self.assertEqual((await client.post(self.path("/statistics/data/reports/not-a-report/send"))).status, 404)
        staff = await self.client(access="staff")
        self.assertEqual((await staff.delete(self.path("/statistics/data/reports/999"))).status, 403)


class PrivacyMoveGuards(tsd.DataTest):
    async def test_a_step_that_fails_leaves_the_rest_for_the_retry(self):
        db = self.bot.message_db
        for day in range(1, 7):
            await db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 1, ?, 0, 1)",
                         (self.guild.id, tf.slot_of(time.time() - day * DAY), self.member.id))
        original = db.run
        moved = 0

        async def run(sql, args=()):
            nonlocal moved
            if sql.startswith("INSERT INTO stat_messages") and "DO UPDATE SET count" in sql:
                moved += 1
                if moved == 2:
                    raise RuntimeError("the disk is full")
            return await original(sql, args)
        with mock.patch.object(store, "STEP_ROWS", 2), mock.patch.object(db, "run", run), self.assertLogs(statistics_commands.log, "ERROR"):
            self.assertEqual(await self.stats.set_privacy(self.member.id, True), ("privacy_failed", {}))
        named = "SELECT COUNT(*) FROM stat_messages WHERE user_id = ?"
        self.assertEqual(await self.rows(named, (self.member.id,)), [(4,)], "the first step's two rows moved, and the failed step's stayed")
        self.assertEqual(await self.rows("SELECT SUM(count) FROM stat_messages WHERE user_id = 0"), [(2,)])
        row = await self.bot.db.one("SELECT anonymized_at, private_changed_at FROM stat_user_settings WHERE user_id = ?", (self.member.id,))
        self.assertLess(row["anonymized_at"] or 0, row["private_changed_at"], "the move isn't marked done")
        with mock.patch.object(store, "STEP_ROWS", 2):
            self.assertEqual(await self.stats.set_privacy(self.member.id, True), ("privacy_done_on", {}))
        self.assertEqual(await self.rows(named, (self.member.id,)), [(0,)])
        self.assertEqual(await self.rows("SELECT SUM(count) FROM stat_messages WHERE user_id = 0"), [(6,)], "nothing moved twice")
        row = await self.bot.db.one("SELECT anonymized_at, private_changed_at FROM stat_user_settings WHERE user_id = ?", (self.member.id,))
        self.assertGreaterEqual(row["anonymized_at"], row["private_changed_at"])


class RestoreGuards(tsd.DataTest):
    async def asyncSetUp(self):
        self.modules = [Statistics, MyData, Backup]
        await super().asyncSetUp()
        self.backup = self.cog(Backup)

    async def test_a_restored_backup_doesnt_put_a_name_back_on_counts_taken_off_it(self):
        db = self.bot.message_db
        slot = tf.slot_of(time.time() - DAY)
        await db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 1, ?, 0, 5)", (self.guild.id, slot, self.member.id))
        await db.run("INSERT INTO stat_people (guild_id, user_id, name, seen_at) VALUES (?, ?, 'member', 1)", (self.guild.id, self.member.id))
        await db.run("INSERT INTO stat_forum_posts (guild_id, thread_id, forum_id, owner_id, created_at) VALUES (?, 7, 8, ?, 1)", (self.guild.id, self.member.id))
        body = bytearray()
        async for block in self.backup.export_stream(self.guild.id, include_messages=True):
            body += block
        self.assertEqual((await self.stats.set_privacy(self.member.id, True))[0], "privacy_done_on")
        named = ("SELECT (SELECT COUNT(*) FROM stat_messages WHERE user_id = ?) + (SELECT COUNT(*) FROM stat_people WHERE user_id = ?) "
                 "+ (SELECT COUNT(*) FROM stat_forum_posts WHERE owner_id = ?)")
        self.assertEqual((await self.rows(named, (self.member.id,) * 3))[0][0], 0)
        client = await self.client()
        response = await client.post(self.path("/backup/upload"), data=bytes(body))
        self.assertEqual(response.status, 200, await response.text())
        preview = (await self.wait_for_job(client))["result"]
        items = [row["key"] for row in preview["settings"]] + [row["key"] for row in preview["tables"]]
        restored = await client.post(self.path("/backup/restore"), json={"token": preview["token"], "items": items})
        self.assertEqual(restored.status, 200, await restored.text())
        await self.wait_for_job(client)
        self.assertEqual((await self.rows(named, (self.member.id,) * 3))[0][0], 0, "the privacy page promises the move is for good")
        self.assertEqual(await self.rows("SELECT SUM(count) FROM stat_messages WHERE user_id = 0"), [(5,)], "the counts stay in the totals")

    async def test_restored_counts_no_one_holds_yet_keep_their_totals(self):
        db = self.bot.message_db
        slot = tf.slot_of(time.time() - DAY)
        other = fakes.FakeMember(self.guild, "other")
        for person, count in ((self.member, 3), (other, 2)):
            await db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 1, ?, 0, ?)", (self.guild.id, slot, person.id, count))
        await db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 2, 0, 0, 7)", (self.guild.id, slot))
        await db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 2, ?, 0, 7)", (self.guild.id, slot, self.member.id))
        await store.anonymize_restored(db, self.guild.id, [self.member.id, other.id])
        self.assertEqual(await self.rows("SELECT channel_id, user_id, count FROM stat_messages WHERE guild_id = ? ORDER BY channel_id", (self.guild.id,)),
                         [(1, 0, 5), (2, 0, 7)], "a row no one held yet goes in summed, and one no one already holds was counted there before")


# ---- insights ----

class InsightsGuards(tsd.DataTest):
    modules = [Statistics, MyData, Moderation, Reports, Logs]

    async def test_automod_counts_leave_out_channels_the_viewer_cant_read(self):
        secret = AdminOnly(self.guild, "staff-chat")
        for channel_id in (secret.id, None):
            await self.message_db.run("INSERT INTO log_entries (guild_id, at, event, channel_id, embed, search_text) VALUES (?, ?, 'automod', ?, ?, '')",
                                      (self.guild.id, time.time() - DAY, channel_id, json.dumps({"title": "Automod: Link spam"})))
        viewer = await client_as(self, self.mod, "staff")
        page = await (await viewer.get(self.path("/statistics/insights/moderation?days=30"))).json()
        logs = await (await viewer.get(self.path("/logs/search"))).json()
        self.assertEqual(logs["total"], 1)
        self.assertEqual([(row["rule"], row["count"]) for row in page["automod"]["hits"]], [("Link spam", logs["total"])],
                         "the log hides that channel's entries from them, and the insights count them")
        admin = await client_as(self, self.admin, "full")
        page = await (await admin.get(self.path("/statistics/insights/moderation?days=30"))).json()
        self.assertEqual([(row["rule"], row["count"]) for row in page["automod"]["hits"]], [("Link spam", 2)])

    async def test_managers_only_statistics_keep_staff_out_of_the_statistics_insights(self):
        await self.bot.settings.save(self.guild.id, "statistics", {"dashboard_access": "managers"})
        staff = await self.client(access="staff")
        statuses = {}
        for tail in ("/statistics/insights/forums?days=7", "/statistics/insights/newcomers?weeks=4", "/statistics/insights/moderation?days=7"):
            statuses[tail] = (await staff.get(self.path(tail))).status
        self.assertEqual(statuses["/statistics/insights/forums?days=7"], 403)
        self.assertEqual(statuses["/statistics/insights/newcomers?weeks=4"], 403, "staff read statistics the server kept to Manage Server")
        self.assertEqual(statuses["/statistics/insights/moderation?days=7"], 200, "moderation records stay open to staff")
        full = await self.client(access="full")
        self.assertEqual((await full.get(self.path("/statistics/insights/newcomers?weeks=4"))).status, 200)


# ---- the review's leads ----

class SlowHistory(tsd.HistoryChannel):
    """A channel that stops partway through its history until the test lets it go on."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate = asyncio.Event()

    async def history(self, **kwargs):
        sent = 0
        async for message in super().history(**kwargs):
            if sent == 1:
                await self.gate.wait()
            sent += 1
            yield message


class Broken(tsd.HistoryChannel):
    """A channel whose reading fails once with something no handler expects."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fails = 1

    async def history(self, **kwargs):
        if self.fails:
            self.fails -= 1
            raise RuntimeError("something nobody planned for")
        async for message in super().history(**kwargs):
            yield message


class LeadGuards(tsd.DataTest):
    modules = [*tsd.Privacy.modules]

    async def test_a_reader_that_fails_stops_the_others_so_a_resume_reads_nothing_twice(self):
        slow = SlowHistory(self.guild, "busy")
        self.known[slow.id] = slow
        for number in range(600):
            slow.say(self.member, tsd.ago(8 * DAY - number), "hi")
        broken = Broken(self.guild, "broken")
        self.known[broken.id] = broken
        client = await self.client()
        page = await self.start(client, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        self.assertEqual(page["recount"]["state"], history.REVIEW)
        resumed = await client.post(self.path("/statistics/data/recount/resume"))
        self.assertEqual(resumed.status, 200, await resumed.text())
        await asyncio.sleep(0.05)
        slow.gate.set()
        await self.settle()
        page = await (await client.get(self.path("/statistics/data/recount"))).json()
        self.assertEqual(page["recount"]["state"], history.APPROVAL, page["recount"])
        self.assertEqual(next(row for row in page["channels"] if row["name"] == "busy")["staged"], 600, "two readers staged the same messages")

    async def note(self, channel):
        await self.bot.message_db.run("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, ?, 0, NULL, 1, NULL)",
                                      (self.guild.id, channel.id, channel.name))

    async def test_two_quick_merges_start_one(self):
        ticket = self.history_channel("ticket-9")
        await self.note(ticket)
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 3)",
                                      (self.guild.id, tf.slot_of(time.time() - 3 * DAY), ticket.id, self.member.id))
        client = await self.client()
        made = await client.post(self.path("/statistics/data/merged"), json={"name": "Tickets"})
        target = next(row for row in (await made.json())["aggregates"] if row["name"] == "Tickets")["id"]
        form = {"target_id": str(target), "kinds": "messages", "channels": [str(ticket.id)], "confirm": True}
        first, second = await asyncio.gather(client.post(self.path("/statistics/data/merged/merge"), json=form),
                                             client.post(self.path("/statistics/data/merged/merge"), json=form))
        self.assertEqual(sorted([first.status, second.status]), [200, 409])
        await self.settle()
        self.assertEqual(await self.counts(target), {(self.member.id, 0): 3})

    async def test_two_quick_recounts_start_one(self):
        self.history_channel("general").say(self.member, tsd.ago(8 * DAY), "hi")
        client = await self.client()
        form = {"start": tsd.ago(10 * DAY).timestamp(), "end": tsd.ago(5 * DAY).timestamp()}
        first, second = await asyncio.gather(client.post(self.path("/statistics/data/recount"), json=form),
                                             client.post(self.path("/statistics/data/recount"), json=form))
        self.assertEqual(sorted([first.status, second.status]), [200, 409])
        await self.settle()

    async def test_a_merge_resumed_before_the_server_is_back_forgets_nothing_that_exists(self):
        ticket = self.history_channel("ticket-7")
        await self.bot.message_db.run("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, 'ticket-7', 0, NULL, 1, NULL)",
                                      (self.guild.id, ticket.id))
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 3)",
                                      (self.guild.id, tf.slot_of(time.time() - 3 * DAY), ticket.id, self.member.id))
        client = await self.client()
        made = await client.post(self.path("/statistics/data/merged"), json={"name": "Tickets"})
        target = next(row for row in (await made.json())["aggregates"] if row["name"] == "Tickets")["id"]
        with mock.patch.object(self.stats, "_start_merge"):  # as if the bot stopped before the merge ran
            merged = await client.post(self.path("/statistics/data/merged/merge"), json={"target_id": str(target), "kinds": "messages", "channels": [str(ticket.id)],
                                                                                          "forget": True, "confirm": True})
        self.assertEqual(merged.status, 200, await merged.text())
        self.guild.unavailable = True
        kept = list(self.guild.channels)
        self.guild.channels.clear()
        try:
            self.stats._start_merge(self.guild.id)
            await self.settle()
        finally:
            self.guild.channels.extend(kept)
            self.guild.unavailable = False
        self.assertEqual(await self.rows("SELECT name FROM stat_channels WHERE guild_id = ? AND channel_id = ?", (self.guild.id, ticket.id)), [("ticket-7",)])
        self.assertEqual(await self.counts(target), {(self.member.id, 0): 3})

    async def test_a_report_reads_with_the_servers_own_filters(self):
        kept_out = self.history_channel("bot-spam")
        slot = tf.slot_of(time.time() - 2 * DAY)
        for channel, count in ((self.channel, 4), (kept_out, 50)):
            await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, ?)",
                                          (self.guild.id, slot, channel.id, self.member.id, count))
        await self.bot.settings.save(self.guild.id, "statistics", {"channel_mode": "block", "channels": [kept_out.id]})
        embed = await self.stats.report_embed(self.guild, self.channel, "week", time.time())
        messages = next(field for field in embed.fields if field.name == "Messages").value
        self.assertTrue(messages.startswith("4 "), messages)

    async def test_my_data_finds_counts_in_a_server_nothing_else_names(self):
        other = 777777777777777777
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 1, ?, 0, 6)",
                                      (other, tf.slot_of(time.time() - DAY), self.member.id))
        viewer = await client_as(self, self.member)
        overview = await (await viewer.get("/api/my-data")).json()
        there = next((server for server in overview["servers"] if str(server["id"]) == str(other)), None)
        self.assertIsNotNone(there, "an imported server the bot left is missing")
        self.assertEqual(there["statistics"]["messages"], 6)

    async def test_insights_reach_back_to_discords_start_and_go_by_day_for_a_thousand_days(self):
        client = await self.client()
        self.assertEqual((await client.get(self.path("/statistics/insights/moderation?days=36500&interval=day"))).status, 400)
        page = await (await client.get(self.path("/statistics/insights/moderation?days=36500&interval=month"))).json()
        self.assertGreaterEqual(page["start"], history.DISCORD_EPOCH_MS / 1000 - 31 * DAY)
        self.assertEqual((await client.get(self.path("/statistics/insights/moderation?days=1000&interval=day"))).status, 200)

    async def test_a_deleted_forum_takes_its_posts_with_it(self):
        forum = self.history_channel("help", discord.ChannelType.forum)
        await self.bot.message_db.run("INSERT INTO stat_forum_posts (guild_id, thread_id, forum_id, owner_id, created_at) VALUES (?, 1, ?, ?, 1), (?, 2, ?, ?, 1)",
                                      (self.guild.id, forum.id, self.member.id, self.guild.id, forum.id, self.admin.id))
        await self.stats._forum_gone(forum)
        self.assertEqual(await self.rows("SELECT COUNT(*) FROM stat_forum_posts WHERE guild_id = ?", (self.guild.id,)), [(0,)])

    async def test_a_cancel_after_a_failed_swap_says_which_channels_kept_the_recount(self):
        first, second = self.history_channel("alpha"), self.history_channel("beta")
        for channel in (first, second):
            channel.say(self.member, tsd.ago(8 * DAY), "hi")
        client = await self.client()
        await self.start(client, tsd.ago(10 * DAY), tsd.ago(5 * DAY))
        original = history.apply_channel
        swaps = 0

        async def apply(*args, **kwargs):
            nonlocal swaps
            swaps += 1
            if swaps == 2:
                raise RuntimeError("the disk is full")
            return await original(*args, **kwargs)
        with mock.patch.object(history, "apply_channel", apply), self.assertLogs(statistics_data.log, "ERROR"):
            page = await self.approve(client)
        self.assertEqual(page["recount"]["state"], history.REVIEW)
        canceled = await (await client.post(self.path("/statistics/data/recount/cancel"))).json()
        self.assertIn("1 channels were already swapped", canceled["recount"]["note"])

    async def test_stat_roles_and_counters_can_tell_a_merge_is_moving_counts(self):
        ticket = self.history_channel("ticket-8")
        await self.note(ticket)
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 3)",
                                      (self.guild.id, tf.slot_of(time.time() - 3 * DAY), ticket.id, self.member.id))
        client = await self.client()
        made = await client.post(self.path("/statistics/data/merged"), json={"name": "Tickets"})
        target = next(row for row in (await made.json())["aggregates"] if row["name"] == "Tickets")["id"]
        gate, seen = asyncio.Event(), []
        original = merge.move_batch

        async def move(*args, **kwargs):
            seen.append(self.stats.stats_applying(self.guild.id))
            await gate.wait()
            return await original(*args, **kwargs)
        self.assertFalse(self.stats.stats_applying(self.guild.id))
        with mock.patch.object(merge, "move_batch", move):
            await client.post(self.path("/statistics/data/merged/merge"), json={"target_id": str(target), "kinds": "messages", "channels": [str(ticket.id)], "confirm": True})
            await asyncio.sleep(0.05)
            self.assertTrue(self.stats.stats_applying(self.guild.id))
            gate.set()
            await self.settle()
        self.assertEqual(seen[:1], [True])
        self.assertFalse(self.stats.stats_applying(self.guild.id))
