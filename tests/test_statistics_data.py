"""Statistics data: recounts from Discord's history, merged channels, the privacy choice and My data, help forums, newcomers, moderation
over time and scheduled reports."""
from __future__ import annotations

import asyncio
import datetime
import itertools
import json
import time
import types
import unittest
from unittest import mock

import aiohttp
import discord

from captainguild.core.ops import DiscordOps
from captainguild.core.stats import beyond, history, merge, query, store
from captainguild.core.stats import timeframe as tf
from captainguild.modules import statistics_commands, statistics_data
from captainguild.modules.entrygiveaways import EntryGiveaways
from captainguild.modules.forummessages import ForumMessages
from captainguild.modules.fun import Fun
from captainguild.modules.greetings import Greetings
from captainguild.modules.levels import Levels
from captainguild.modules.logs import Logs
from captainguild.modules.moderation import Moderation
from captainguild.modules.mydata import MyData
from captainguild.modules.reports import Reports
from captainguild.modules.statistics import Statistics
from captainguild.modules.statistics_data import next_report_at, report_period
from captainguild.modules.translation import Translation
from captainguild.modules.utilities import Utilities
from captainguild.modules.voice import Voice
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.fakes import BotTest, FakeChannel, FakeMember, FakePermissions

from aiohttp.test_utils import TestClient, TestServer

UTC = datetime.timezone.utc
DAY = 86400
_serial = itertools.count(1)


def ago(seconds: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(time.time() - seconds, UTC)


def snowflake(at: datetime.datetime) -> int:
    return discord.utils.time_snowflake(at) + next(_serial)


def http_error(kind: type, status: int, text: str = "refused") -> Exception:
    return kind(types.SimpleNamespace(status=status, reason=text), text)


class Readable:
    """A channel or thread that hands out its history the way discord.py does: oldest first, after one ID and before another."""

    def _start_history(self) -> None:
        self.said: list = []
        self.refuse: Exception | None = None
        self.break_after: int | None = None  # how many messages go out before the connection drops, once

    def say(self, author, at, content="", **extra):
        message = types.SimpleNamespace(id=snowflake(at), created_at=at, author=author, type=discord.MessageType.default, webhook_id=None, content=content,
                                        stickers=[], reactions=[], channel=self)
        for name, value in extra.items():
            setattr(message, name, value)
        self.said.append(message)
        return message

    async def history(self, limit=None, before=None, after=None, oldest_first=None, **kwargs):
        if self.refuse is not None:
            raise self.refuse
        sent = 0
        for message in sorted(self.said, key=lambda item: item.id):
            if after is not None and message.id <= after.id:
                continue
            if before is not None and message.id >= before.id:
                continue
            if self.break_after is not None and sent == self.break_after:
                self.break_after = None
                raise aiohttp.ClientConnectionError("dropped")
            sent += 1
            yield message


class HistoryChannel(Readable, FakeChannel):
    def __init__(self, guild, name, kind=discord.ChannelType.text, category=None):
        FakeChannel.__init__(self, guild, name, kind, category)
        self._start_history()
        self.archived: list = []
        self.archived_private: list = []
        self.private_refused = False

    async def archived_threads(self, *, private=False, joined=False, limit=100, before=None):
        if private and not joined and self.private_refused:
            raise http_error(discord.Forbidden, 403)
        for thread in (self.archived_private if private else self.archived):
            yield thread


class FakeThread(Readable):
    def __init__(self, parent, name, owner, created, *, archived_at=None, tags=(), private=False):
        self._start_history()
        self.id = snowflake(created)
        self.guild = parent.guild
        self.parent = parent
        self.parent_id = parent.id
        self.name = name
        self.owner_id = owner.id
        self.created_at = created
        self.archive_timestamp = archived_at
        self.applied_tags = [types.SimpleNamespace(id=tag) for tag in tags]
        self.type = discord.ChannelType.private_thread if private else discord.ChannelType.public_thread
        self.category_id = None
        self.position = 0
        self.nsfw = False


class DataTest(BotTest):
    modules = [Statistics, MyData]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        self.bot.ops = DiscordOps(self.bot)  # the dashboard checks channels against the fake server, not the demo's
        self.known: dict[int, object] = {}
        self.guild.get_channel_or_thread = lambda channel_id: self.guild.get_channel(channel_id) or self.known.get(channel_id)
        self.guild.active_threads = mock.AsyncMock(return_value=[])
        self.guild.stickers = []
        self.guild.channels.remove(self.channel)  # one that reads history and lists threads as discord.py's channels do
        self.channel = self.history_channel("chat")
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})

    def history_channel(self, name, kind=discord.ChannelType.text, category=None):
        channel = HistoryChannel(self.guild, name, kind, category)
        self.known[channel.id] = channel
        return channel

    def thread(self, parent, name, owner, created, **options):
        found = FakeThread(parent, name, owner, created, **options)
        self.known[found.id] = found
        return found

    async def rows(self, sql, args=()):
        await self.stats.up_to_date()
        return [tuple(row.values()) for row in await self.bot.message_db.all(sql, args)]

    async def counts(self, channel_id):
        return dict((tuple(row[:-1]), row[-1]) for row in await self.rows(
            "SELECT user_id, flags, SUM(count) FROM stat_messages WHERE guild_id = ? AND channel_id = ? GROUP BY user_id, flags", (self.guild.id, channel_id)))

    async def settle(self):
        await self.stats.finish_data_work()

    async def start(self, client, start, end, **form):
        response = await client.post(self.path("/statistics/data/recount"), json={"start": start.timestamp(), "end": end.timestamp(), **form})
        self.assertEqual(response.status, 200, await response.text())
        await self.settle()
        return await (await client.get(self.path("/statistics/data/recount"))).json()

    async def approve(self, client):
        response = await client.post(self.path("/statistics/data/recount/approve"))
        self.assertEqual(response.status, 200, await response.text())
        await self.settle()
        return await (await client.get(self.path("/statistics/data/recount"))).json()


class Recounts(DataTest):
    async def test_a_recount_replaces_the_period_with_what_discord_holds(self):
        general = self.history_channel("general")
        lounge = self.history_channel("Lounge", discord.ChannelType.voice)
        hidden = self.history_channel("staff-only")
        bot = FakeMember(self.guild, "helper", bot=True)
        quiet = FakeMember(self.guild, "quiet")
        start, end = ago(10 * DAY), ago(5 * DAY)
        inside = ago(8 * DAY)
        slot = tf.slot_of(inside.timestamp())
        # Raid spam recorded live, since deleted, and a count outside the period that must stay.
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)", [
            (self.guild.id, slot, general.id, quiet.id, 0, 500),
            (self.guild.id, tf.slot_of(ago(2 * DAY).timestamp()), general.id, quiet.id, 0, 7),
            (self.guild.id, slot, hidden.id, quiet.id, 0, 9),
        ])
        for _ in range(3):
            general.say(self.member, inside, "hello")
        general.say(self.admin, inside, "hi")
        general.say(bot, inside, "beep")
        general.say(self.member, inside, "from a webhook", webhook_id=5)
        general.say(self.member, inside, "", type=discord.MessageType.pins_add)
        general.say(self.member, ago(12 * DAY), "too early")
        general.say(quiet, inside, "secret")
        thread = self.thread(general, "a thread", self.member, ago(9 * DAY), archived_at=ago(6 * DAY))
        thread.say(self.member, inside, "in a thread")
        thread.say(self.admin, inside, "answer")
        general.archived = [thread]
        lounge.say(self.admin, inside, "voice chat")
        hidden.say(self.member, inside, "kept out")
        self.stats.private.add(quiet.id)
        await self.bot.settings.save(self.guild.id, "statistics", {"untracked_channels": [hidden.id]})
        client = await self.client()
        page = await self.start(client, start, end)
        self.assertEqual(page["recount"]["state"], history.APPROVAL, page)
        self.assertEqual(page["recount"]["found"], 9)
        row = next(entry for entry in page["channels"] if entry["channel_id"] == str(general.id) or entry["channel_id"] == general.id)
        self.assertEqual(row["staged"], 8)
        self.assertEqual(row["recorded"], 500)
        self.assertEqual(await self.counts(general.id), {(quiet.id, 0): 507}, "nothing changes before approval")
        await self.approve(client)
        self.assertEqual(await self.counts(general.id), {(self.member.id, 0): 3, (self.admin.id, 0): 1, (bot.id, store.BOT): 1, (0, 0): 1,
                                                          (self.member.id, store.THREAD): 1, (self.admin.id, store.THREAD): 1, (quiet.id, 0): 7})
        self.assertEqual(await self.counts(lounge.id), {(self.admin.id, store.VOICE_CHAT): 1})
        self.assertEqual(await self.counts(hidden.id), {(quiet.id, 0): 9}, "a channel statistics don't track is left alone")
        names = {row[0] for row in await self.rows("SELECT user_id FROM stat_people WHERE guild_id = ?", (self.guild.id,))}
        self.assertTrue({self.member.id, self.admin.id} <= names)
        self.assertNotIn(quiet.id, names, "someone who chose privacy gets no name row")
        self.assertNotIn(bot.id, names)
        recount = await self.stats._recount(self.guild.id)
        self.assertEqual(recount["state"], history.DONE)
        self.assertEqual(await self.rows("SELECT COUNT(*) FROM stat_recount_messages"), [(0,)])
        actions = [entry["action"] for entry in await self.bot.activity_log.recent(self.guild.id)]
        self.assertIn("statistics.recount", actions)
        self.assertIn("statistics.recount_approve", actions)

    async def test_emoji_are_only_ever_added_and_a_second_recount_adds_nothing(self):
        general = self.history_channel("general")
        own = types.SimpleNamespace(id=111111111111111111)
        self.guild.emojis = [own]
        self.guild.stickers = [types.SimpleNamespace(id=222222222222222222)]
        inside = ago(8 * DAY)
        slot = tf.slot_of(inside.timestamp())
        thumbs = "\U0001F44D"
        message = general.say(self.member, inside, f"<:ours:111111111111111111> \U0001F600 \U0001F600 <:theirs:333333333333333333>",
                              stickers=[types.SimpleNamespace(id=222222222222222222), types.SimpleNamespace(id=444444444444444444)])
        message.reactions = [
            types.SimpleNamespace(emoji=thumbs, count=3, normal_count=3, burst_count=0, me=True, me_burst=False),
            types.SimpleNamespace(emoji=types.SimpleNamespace(id=111111111111111111, name="ours"), count=2, normal_count=1, burst_count=1, me=False, me_burst=False),
            types.SimpleNamespace(emoji=types.SimpleNamespace(id=999999999999999999, name="theirs"), count=4, normal_count=4, burst_count=0, me=False, me_burst=False),
        ]
        bot = FakeMember(self.guild, "helper", bot=True)
        general.say(bot, inside, "\U0001F600")
        # One of the thumbs is already recorded under no one, as an import or an earlier recount puts it. Reactions counted live would keep a
        # recount from adding any for the period, which the guard tests cover.
        await self.bot.message_db.run("INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                      (self.guild.id, slot, general.id, 0, 0, store.unicode_key(thumbs), store.REACTION, 0, 1))
        client = await self.client()
        for _ in range(2):
            await self.start(client, ago(10 * DAY), ago(5 * DAY))
            await self.approve(client)
            rows = await self.rows("SELECT user_id, target_id, emoji, use, flags, count FROM stat_emoji WHERE guild_id = ? ORDER BY emoji, use, user_id, flags", (self.guild.id,))
            self.assertEqual(rows, [
                (0, 0, "c:111111111111111111", store.REACTION, 0, 1),
                (0, 0, "c:111111111111111111", store.REACTION, store.SUPER, 1),
                (self.member.id, 0, "c:111111111111111111", store.IN_TEXT, 0, 1),
                (self.member.id, 0, "s:222222222222222222", store.STICKER, 0, 1),
                (0, 0, "u:" + thumbs, store.REACTION, 0, 2),
                (self.member.id, 0, "u:\U0001F600", store.IN_TEXT, 0, 1),
            ])

    async def test_an_emoji_deleted_since_still_counts_as_the_servers(self):
        general = self.history_channel("general")
        await self.bot.message_db.run("INSERT INTO stat_emoji_info (guild_id, emoji, name, deleted_at) VALUES (?, 'c:555555555555555555', 'gone', 1)", (self.guild.id,))
        general.say(self.member, ago(8 * DAY), "<:gone:555555555555555555> <:stranger:666666666666666666>")
        client = await self.client()
        await self.start(client, ago(10 * DAY), ago(5 * DAY))
        await self.approve(client)
        self.assertEqual(await self.rows("SELECT emoji FROM stat_emoji WHERE guild_id = ?", (self.guild.id,)), [("c:555555555555555555",)])

    async def test_reading_saves_as_it_goes_and_picks_up_after_a_dropped_connection(self):
        general = self.history_channel("general")
        inside = ago(8 * DAY)
        for _ in range(5):
            general.say(self.member, inside, "hello")
        general.break_after = 3
        client = await self.client()
        with mock.patch.object(statistics_data, "READ_BATCH", 2), mock.patch.object(statistics_data, "RETRY_SECONDS", 0):
            page = await self.start(client, ago(10 * DAY), ago(5 * DAY))
        self.assertEqual(page["recount"]["found"], 5, "the two saved before the drop aren't read twice")
        await self.approve(client)
        self.assertEqual(await self.counts(general.id), {(self.member.id, 0): 5})

    async def test_a_place_the_bot_cant_read_waits_for_review(self):
        general = self.history_channel("general")
        locked = self.history_channel("locked")
        inside = ago(8 * DAY)
        slot = tf.slot_of(inside.timestamp())
        general.say(self.member, inside, "hello")
        locked.say(self.member, inside, "hidden")
        locked.refuse = http_error(discord.Forbidden, 403)
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 40)",
                                      (self.guild.id, slot, locked.id, self.admin.id))
        client = await self.client()
        page = await self.start(client, ago(10 * DAY), ago(5 * DAY))
        self.assertEqual(page["recount"]["state"], history.REVIEW)
        self.assertEqual([entry["name"] for entry in page["errors"]], ["locked"])
        self.assertIn("Read Message History", page["errors"][0]["error"])
        self.assertEqual((await client.post(self.path("/statistics/data/recount/approve"))).status, 409)
        locked.refuse = None
        locked.said.append(types.SimpleNamespace(**{**vars(locked.said[0]), "id": locked.said[0].id + 1}))
        response = await client.post(self.path("/statistics/data/recount/resume"))
        self.assertEqual(response.status, 200, await response.text())
        await self.settle()
        page = await (await client.get(self.path("/statistics/data/recount"))).json()
        self.assertEqual(page["recount"]["state"], history.APPROVAL)
        await self.approve(client)
        self.assertEqual(await self.counts(locked.id), {(self.member.id, 0): 2})

    async def test_a_place_left_out_keeps_its_channels_recorded_counts(self):
        general = self.history_channel("general")
        inside = ago(8 * DAY)
        slot = tf.slot_of(inside.timestamp())
        general.say(self.member, inside, "hello")
        thread = self.thread(general, "private one", self.admin, ago(9 * DAY), archived_at=ago(6 * DAY), private=True)
        thread.refuse = http_error(discord.Forbidden, 403)
        general.archived_private = [thread]
        other = self.history_channel("other")
        other.say(self.admin, inside, "fine")
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)",
                                       [(self.guild.id, slot, general.id, self.member.id, store.THREAD, 12), (self.guild.id, slot, other.id, self.admin.id, 0, 30)])
        client = await self.client()
        page = await self.start(client, ago(10 * DAY), ago(5 * DAY))
        self.assertEqual(page["recount"]["state"], history.REVIEW)
        response = await client.post(self.path("/statistics/data/recount/skip"))
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["recount"]["state"], history.APPROVAL)
        page = await self.approve(client)
        self.assertEqual(await self.counts(general.id), {(self.member.id, store.THREAD): 12}, "its unreadable thread would have lost its counts")
        self.assertEqual(await self.counts(other.id), {(self.admin.id, 0): 1})
        self.assertIn("kept their recorded counts", page["recount"]["note"])

    async def test_threads_can_be_left_out_and_then_stay_as_recorded(self):
        general = self.history_channel("general")
        inside = ago(8 * DAY)
        slot = tf.slot_of(inside.timestamp())
        general.say(self.member, inside, "hello")
        thread = self.thread(general, "a thread", self.member, ago(9 * DAY), archived_at=ago(6 * DAY))
        thread.say(self.member, inside, "thread")
        general.archived = [thread]
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, 6)",
                                      (self.guild.id, slot, general.id, self.member.id, store.THREAD))
        client = await self.client()
        await self.start(client, ago(10 * DAY), ago(5 * DAY), threads=False)
        await self.approve(client)
        self.assertEqual(await self.counts(general.id), {(self.member.id, 0): 1, (self.member.id, store.THREAD): 6})

    async def test_cancel_changes_nothing(self):
        general = self.history_channel("general")
        inside = ago(8 * DAY)
        general.say(self.member, inside, "hello")
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 99)",
                                      (self.guild.id, tf.slot_of(inside.timestamp()), general.id, self.admin.id))
        client = await self.client()
        await self.start(client, ago(10 * DAY), ago(5 * DAY))
        response = await client.post(self.path("/statistics/data/recount/cancel"))
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["recount"]["state"], history.CANCELED)
        self.assertEqual(await self.counts(general.id), {(self.admin.id, 0): 99})
        for table in ("stat_recount_messages", "stat_recount_places", "stat_recount_people"):
            self.assertEqual(await self.rows(f"SELECT COUNT(*) FROM {table}"), [(0,)], table)
        self.assertEqual((await client.post(self.path("/statistics/data/recount/cancel"))).status, 409)

    async def test_what_a_recount_refuses(self):
        client = await self.client()
        future = await client.post(self.path("/statistics/data/recount"), json={"start": time.time() + DAY, "end": time.time() + 2 * DAY})
        self.assertEqual(future.status, 400)
        nonsense = await client.post(self.path("/statistics/data/recount"), json={"start": "soon", "end": time.time()})
        self.assertEqual(nonsense.status, 400)
        other_server = await client.post(self.path("/statistics/data/recount"), json={"start": time.time() - 9 * DAY, "end": time.time() - DAY, "channels": ["123456789012345678"]})
        self.assertEqual(other_server.status, 400)
        self.history_channel("general")
        first = await client.post(self.path("/statistics/data/recount"), json={"start": time.time() - 9 * DAY, "end": time.time() - DAY})
        self.assertEqual(first.status, 200, await first.text())
        await self.settle()
        again = await client.post(self.path("/statistics/data/recount"), json={"start": time.time() - 9 * DAY, "end": time.time() - DAY})
        self.assertEqual(again.status, 409)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": False})
        await client.post(self.path("/statistics/data/recount/cancel"))
        off = await client.post(self.path("/statistics/data/recount"), json={"start": time.time() - 9 * DAY, "end": time.time() - DAY})
        self.assertEqual(off.status, 409)
        staff = await self.client(access="staff")
        self.assertEqual((await staff.get(self.path("/statistics/data/recount"))).status, 403)

    async def test_the_offered_period_runs_from_the_servers_creation_to_the_first_recorded_message(self):
        slot = tf.slot_of(time.time() - 30 * DAY)
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 5, 6, 0, 1)", (self.guild.id, slot))
        client = await self.client()
        page = await (await client.get(self.path("/statistics/data/recount"))).json()
        self.assertEqual(page["defaults"]["start"], self.guild.created_at.timestamp())
        self.assertEqual(page["defaults"]["end"], tf.slot_time(slot))

    async def test_chosen_channels_and_categories_only(self):
        category = FakeChannel(self.guild, "Archive", discord.ChannelType.category)
        filed = self.history_channel("filed", category=category)
        picked = self.history_channel("picked")
        skipped = self.history_channel("skipped")
        inside = ago(8 * DAY)
        for channel in (filed, picked, skipped):
            channel.say(self.member, inside, "hello")
        client = await self.client()
        page = await self.start(client, ago(10 * DAY), ago(5 * DAY), channels=[str(category.id), str(picked.id)])
        self.assertEqual({str(entry["channel_id"]) for entry in page["channels"]}, {str(filed.id), str(picked.id)})

    async def test_a_forum_post_made_in_the_period_gets_its_first_answer(self):
        forum = self.history_channel("help", discord.ChannelType.forum)
        early = self.thread(forum, "old question", self.member, ago(30 * DAY), archived_at=ago(7 * DAY))
        post = self.thread(forum, "question", self.member, ago(9 * DAY), archived_at=ago(6 * DAY))
        bot = FakeMember(self.guild, "helper", bot=True)
        asked = ago(9 * DAY - 10)
        post.said.append(types.SimpleNamespace(id=post.id, created_at=post.created_at, author=self.member, type=discord.MessageType.default, webhook_id=None,
                                               content="help", stickers=[], reactions=[], channel=post))
        post.say(bot, ago(9 * DAY - 20), "welcome")
        post.say(self.member, ago(9 * DAY - 30), "anyone?")
        answer = post.say(self.admin, ago(9 * DAY - 3600), "try this")
        post.say(self.mod, ago(9 * DAY - 7200), "or this")
        early.say(self.admin, ago(8 * DAY), "late reply")
        forum.archived = [post, early]
        client = await self.client()
        await self.start(client, ago(10 * DAY), ago(5 * DAY))
        await self.approve(client)
        rows = await self.rows("SELECT thread_id, forum_id, owner_id, replier_id FROM stat_forum_posts")
        self.assertEqual(rows, [(post.id, forum.id, self.member.id, self.admin.id)], "the post made before the period isn't added")
        replied = await self.rows("SELECT replied_at FROM stat_forum_posts")
        self.assertAlmostEqual(replied[0][0], answer.created_at.timestamp(), places=3)
        self.assertEqual(await self.counts(forum.id), {(self.member.id, store.THREAD): 2, (bot.id, store.THREAD | store.BOT): 1, (self.admin.id, store.THREAD): 2,
                                                        (self.mod.id, store.THREAD): 1})

    async def test_a_channel_deleted_partway_keeps_what_it_had(self):
        general = self.history_channel("general")
        doomed = self.history_channel("doomed")
        inside = ago(8 * DAY)
        general.say(self.member, inside, "hello")
        doomed.say(self.member, inside, "bye")
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 4)",
                                      (self.guild.id, tf.slot_of(inside.timestamp()), doomed.id, self.admin.id))
        client = await self.client()
        await self.start(client, ago(10 * DAY), ago(5 * DAY))
        self.guild.channels.remove(doomed)
        self.known.pop(doomed.id)
        await self.approve(client)
        self.assertEqual(await self.counts(doomed.id), {(self.admin.id, 0): 4})
        self.assertEqual(await self.counts(general.id), {(self.member.id, 0): 1})

    async def test_a_thread_gone_before_its_reading_waits_for_review(self):
        general = self.history_channel("general")
        inside = ago(8 * DAY)
        general.say(self.member, inside, "hello")
        thread = self.thread(general, "a thread", self.member, ago(9 * DAY), archived_at=ago(6 * DAY))
        thread.say(self.member, inside, "thread")
        thread.refuse = http_error(discord.NotFound, 404)
        general.archived = [thread]
        client = await self.client()
        page = await self.start(client, ago(10 * DAY), ago(5 * DAY))
        self.assertEqual(page["recount"]["state"], history.REVIEW)
        self.assertEqual(page["recount"]["places"].get(history.ERROR), 1)
        self.assertIn("thread is gone", page["errors"][0]["error"])
        await client.post(self.path("/statistics/data/recount/skip"))
        await self.approve(client)
        self.assertEqual(await self.counts(general.id), {}, "left out, the channel keeps what it had recorded, which was nothing")

    async def test_a_restart_partway_through_applying_never_swaps_a_channel_twice(self):
        general = self.history_channel("general")
        other = self.history_channel("other")
        inside = ago(8 * DAY)
        general.say(self.member, inside, "hello")
        other.say(self.admin, inside, "hi")
        gone = self.thread(general, "a thread", self.member, ago(9 * DAY), archived_at=ago(6 * DAY))
        gone.refuse = http_error(discord.NotFound, 404)
        general.archived = [gone]
        client = await self.client()
        await self.start(client, ago(10 * DAY), ago(5 * DAY))
        await self.bot.message_db.run("UPDATE stat_recounts SET state = ? WHERE guild_id = ?", (history.APPLYING, self.guild.id))
        job = await self.stats._recount(self.guild.id)
        await history.apply_channel(self.bot.message_db, self.guild.id, general.id, job["first_slot"], job["end_slot"], threads=True)  # what ran before the restart
        await self.stats._resume_work()
        await self.settle()
        self.assertEqual(await self.counts(general.id), {(self.member.id, 0): 1})
        self.assertEqual(await self.counts(other.id), {(self.admin.id, 0): 1})
        self.assertEqual((await self.stats._recount(self.guild.id))["state"], history.DONE)

    async def test_a_thread_kept_out_of_statistics_isnt_read(self):
        general = self.history_channel("general")
        inside = ago(8 * DAY)
        kept_out = self.thread(general, "staff thread", self.admin, ago(9 * DAY), archived_at=ago(6 * DAY))
        kept_out.say(self.admin, inside, "private")
        general.archived = [kept_out]
        await self.bot.settings.save(self.guild.id, "statistics", {"untracked_channels": [kept_out.id]})
        client = await self.client()
        page = await self.start(client, ago(10 * DAY), ago(5 * DAY))
        self.assertEqual(page["recount"]["found"], 0)
        self.assertEqual(await self.rows("SELECT COUNT(*) FROM stat_recount_places WHERE place_id = ?", (kept_out.id,)), [(0,)])

    async def test_a_restart_resumes_reading(self):
        general = self.history_channel("general")
        inside = ago(8 * DAY)
        for _ in range(3):
            general.say(self.member, inside, "hello")
        client = await self.client()
        with mock.patch.object(self.stats, "_recount_read", mock.AsyncMock()):
            await client.post(self.path("/statistics/data/recount"), json={"start": ago(10 * DAY).timestamp(), "end": ago(5 * DAY).timestamp()})
            await self.settle()
        self.assertEqual((await self.stats._recount(self.guild.id))["state"], history.READING)
        await self.stats._resume_work()
        await self.settle()
        self.assertEqual((await self.stats._recount(self.guild.id))["state"], history.APPROVAL)
        self.assertEqual((await self.stats._recount(self.guild.id))["found"], 3)


class MergedChannels(DataTest):
    async def make(self, client, name, **extra):
        response = await client.post(self.path("/statistics/data/merged"), json={"name": name, **extra})
        self.assertEqual(response.status, 200, await response.text())
        return next(row for row in (await response.json())["aggregates"] if row["name"] == name)

    async def add(self, table, rows):
        columns = {"stat_messages": "guild_id, slot, channel_id, user_id, flags, count", "stat_voice": "guild_id, slot, channel_id, user_id, state, flags, seconds",
                   "stat_emoji": "guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count"}[table]
        marks = ", ".join("?" for _ in columns.split(", "))
        await self.bot.message_db.many(f"INSERT INTO {table} ({columns}) VALUES ({marks})", [(self.guild.id, *row) for row in rows])

    async def test_create_edit_and_delete(self):
        client = await self.client()
        category = FakeChannel(self.guild, "Archive", discord.ChannelType.category)
        row = await self.make(client, "Old channels", category_id=str(category.id))
        number = int(row["id"])
        self.assertLess(number, 2 ** 39)
        self.assertEqual(self.settings("statistics")["aggregates"][0]["category_id"], category.id)
        self.assertEqual(await self.rows("SELECT name, type, parent_id, created_at IS NOT NULL FROM stat_channels WHERE channel_id = ?", (number,)),
                         [("Old channels", merge.AGGREGATE_TYPE, category.id, 1)])
        self.assertEqual((await client.post(self.path("/statistics/data/merged"), json={"name": "old CHANNELS"})).status, 400, "names don't repeat")
        self.assertEqual((await client.post(self.path("/statistics/data/merged"), json={"name": " "})).status, 400)
        edited = await client.patch(self.path(f"/statistics/data/merged/{number}"), json={"name": "Archive", "auto_categories": [str(category.id)], "kinds": "messages"})
        self.assertEqual(edited.status, 200, await edited.text())
        saved = self.settings("statistics")["aggregates"][0]
        self.assertEqual((saved["name"], saved["auto_categories"], saved["kinds"]), ("Archive", [category.id], "messages"))
        self.assertEqual(await self.rows("SELECT name FROM stat_channels WHERE channel_id = ?", (number,)), [("Archive",)])
        second = await self.make(client, "Second")
        twice = await client.patch(self.path(f"/statistics/data/merged/{second['id']}"), json={"auto_categories": [str(category.id)]})
        self.assertEqual(twice.status, 400, "a category feeds one merged channel")
        not_category = await client.patch(self.path(f"/statistics/data/merged/{second['id']}"), json={"category_id": str(self.channel.id)})
        self.assertEqual(not_category.status, 400)
        await self.add("stat_messages", [(tf.slot_of(time.time()), number, self.member.id, 0, 3)])
        refused = await client.delete(self.path(f"/statistics/data/merged/{number}"))
        self.assertEqual(refused.status, 409, "it still holds counts")
        await self.bot.message_db.run("DELETE FROM stat_messages WHERE channel_id = ?", (number,))
        gone = await client.delete(self.path(f"/statistics/data/merged/{number}"))
        self.assertEqual(gone.status, 200, await gone.text())
        self.assertEqual([row["name"] for row in self.settings("statistics")["aggregates"]], ["Second"])
        self.assertEqual(await self.rows("SELECT COUNT(*) FROM stat_channels WHERE channel_id = ?", (number,)), [(0,)])
        self.assertEqual((await client.delete(self.path(f"/statistics/data/merged/{number}"))).status, 404)

    async def test_the_settings_form_cant_remove_or_duplicate_one(self):
        client = await self.client()
        row = await self.make(client, "Old channels")
        removed = await client.put(self.path("/settings/statistics"), json={"aggregates": []})
        self.assertEqual(removed.status, 400)
        self.assertIn("aggregates", (await removed.json())["fields"])
        kept = self.settings("statistics")["aggregates"]
        doubled = await client.put(self.path("/settings/statistics"), json={"aggregates": [*kept, {"id": 12345678, "name": "OLD channels"}]})
        self.assertEqual(doubled.status, 400)
        renamed = await client.put(self.path("/settings/statistics"), json={"aggregates": [{**kept[0], "name": "Renamed"}]})
        self.assertEqual(renamed.status, 200, await renamed.text())
        self.assertEqual(int(row["id"]), self.settings("statistics")["aggregates"][0]["id"])

    async def test_a_merge_moves_counts_and_forgets_only_deleted_empty_channels(self):
        client = await self.client()
        target = int((await self.make(client, "Old channels"))["id"])
        live = self.history_channel("ticket-7")
        old_id = 1_200_000_000_000_000_001
        now = time.time()
        await self.bot.message_db.many("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, ?, 0, NULL, ?, ?)",
                                       [(self.guild.id, old_id, "old-chat", now - 900 * DAY, now - 400 * DAY), (self.guild.id, live.id, "ticket-7", now - 10 * DAY, None)])
        slot, later = tf.slot_of(now - 500 * DAY), tf.slot_of(now - 450 * DAY)
        future = tf.slot_of(now) + 8
        await self.add("stat_messages", [(slot, old_id, self.member.id, 0, 3), (slot, old_id, self.admin.id, 0, 1), (slot, target, self.member.id, 0, 10),
                                         (later, old_id, self.member.id, store.THREAD, 2), (later, live.id, self.member.id, 0, 4), (future, live.id, self.member.id, 0, 5)])
        await self.add("stat_emoji", [(slot, old_id, self.member.id, 0, "u:x", store.IN_TEXT, 0, 4)])
        await self.add("stat_voice", [(slot, old_id, self.member.id, store.TALKING, 0, 600)])
        before = await self.rows("SELECT SUM(count) FROM stat_messages")
        form = {"target_id": str(target), "kinds": "messages", "patterns": ["old-*", "TICKET-*"], "forget": True}
        preview = await client.post(self.path("/statistics/data/merged/preview"), json=form)
        self.assertEqual(preview.status, 200, await preview.text())
        found = await preview.json()
        self.assertEqual(sorted(row["name"] for row in found["channels"]), ["old-chat", "ticket-7"])
        self.assertEqual(found["rows"], 5, "old-chat's three message rows and one emoji row, and ticket-7's one row from before now")
        self.assertEqual(found["would_forget"], 1)
        self.assertEqual((await client.post(self.path("/statistics/data/merged/merge"), json=form)).status, 400, "it needs a confirm")
        with mock.patch.object(merge, "BATCH_ROWS", 1):
            response = await client.post(self.path("/statistics/data/merged/merge"), json={**form, "confirm": True})
            self.assertEqual(response.status, 200, await response.text())
            await self.settle()
        self.assertEqual(await self.rows("SELECT SUM(count) FROM stat_messages"), before, "totals don't change")
        self.assertEqual(await self.counts(target), {(self.member.id, 0): 17, (self.admin.id, 0): 1, (self.member.id, store.THREAD): 2})
        self.assertEqual(await self.counts(live.id), {(self.member.id, 0): 5}, "activity from after the merge started stays")
        self.assertEqual(await self.rows("SELECT channel_id, count FROM stat_emoji"), [(target, 4)])
        self.assertEqual(await self.rows("SELECT channel_id FROM stat_voice"), [(old_id,)], "only messages were merged")
        self.assertEqual(await self.rows("SELECT COUNT(*) FROM stat_channels WHERE channel_id = ?", (old_id,)), [(1,)], "it still holds voice time")
        job = await self.bot.message_db.one("SELECT * FROM stat_merges WHERE guild_id = ?", (self.guild.id,))
        self.assertEqual((job["state"], job["total"], job["done"], job["rows_moved"], job["forgotten"]), ("done", 2, 2, 5, 0))
        voice = {"target_id": str(target), "kinds": "voice", "channels": [str(old_id)], "forget": True, "confirm": True}
        response = await client.post(self.path("/statistics/data/merged/merge"), json=voice)
        self.assertEqual(response.status, 200, await response.text())
        await self.settle()
        self.assertEqual(await self.rows("SELECT channel_id, seconds FROM stat_voice"), [(target, 600)])
        self.assertEqual(await self.rows("SELECT COUNT(*) FROM stat_channels WHERE channel_id = ?", (old_id,)), [(0,)], "deleted in Discord and empty now")
        self.assertEqual(await self.rows("SELECT COUNT(*) FROM stat_channels WHERE channel_id = ?", (live.id,)), [(1,)], "a channel Discord still has stays")
        actions = [entry["action"] for entry in await self.bot.activity_log.recent(self.guild.id)]
        self.assertIn("statistics.merge", actions)

    async def test_one_merge_at_a_time_and_nothing_to_merge(self):
        client = await self.client()
        target = int((await self.make(client, "Old channels"))["id"])
        nothing = await client.post(self.path("/statistics/data/merged/merge"), json={"target_id": str(target), "patterns": ["nope-*"], "confirm": True})
        self.assertEqual(nothing.status, 400)
        wrong = await client.post(self.path("/statistics/data/merged/preview"), json={"target_id": "42", "patterns": ["x"]})
        self.assertEqual(wrong.status, 400)
        await self.bot.message_db.run("INSERT INTO stat_merges (guild_id, target_id, kinds, forget, state, until_slot, created_at) VALUES (?, ?, 'both', 0, 'running', 1, 1)",
                                      (self.guild.id, target))
        await self.bot.message_db.run("INSERT INTO stat_channels (guild_id, channel_id, name, type) VALUES (?, 5555555555555555, 'old-1', 0)", (self.guild.id,))
        busy = await client.post(self.path("/statistics/data/merged/merge"), json={"target_id": str(target), "patterns": ["old-*"], "confirm": True})
        self.assertEqual(busy.status, 409)

    async def test_the_data_page_warns_about_a_long_channel_list(self):
        now = time.time()
        await self.bot.message_db.many("INSERT INTO stat_channels (guild_id, channel_id, name, type, deleted_at) VALUES (?, ?, ?, 0, ?)",
                                       [(self.guild.id, 1_300_000_000_000_000_000 + n, f"old-{n}", now) for n in range(1200)])
        client = await self.client()
        page = await (await client.get(self.path("/statistics/data"))).json()
        self.assertGreaterEqual(page["channels"]["channels"], 1200)
        self.assertEqual(page["channels"]["deleted"], 1200)
        self.assertEqual(page["channels_warning"], statistics_data.CHANNELS_WARNING)


class Privacy(DataTest):
    modules = [Statistics, MyData, Greetings, Levels, Logs, Moderation, Utilities, Fun, Voice, Translation, EntryGiveaways]  # every table My data reads
    async def seed_person(self, guild_id, user_id, other):
        slot = tf.slot_of(time.time() - DAY)
        db = self.bot.message_db
        await db.run("INSERT INTO stat_messages VALUES (?, ?, 1, ?, 0, 5)", (guild_id, slot, user_id))
        await db.run("INSERT INTO stat_messages VALUES (?, ?, 1, 0, 0, 2)", (guild_id, slot))
        await db.run("INSERT INTO stat_voice VALUES (?, ?, 2, ?, 1, 0, 120)", (guild_id, slot, user_id))
        await db.run("INSERT INTO stat_activities VALUES (?, ?, ?, 9, 0, 60)", (guild_id, slot, user_id))
        await db.run("INSERT INTO stat_emoji VALUES (?, ?, 1, ?, ?, 'u:x', 1, 0, 1)", (guild_id, slot, user_id, other))
        await db.run("INSERT INTO stat_emoji VALUES (?, ?, 1, ?, ?, 'u:y', 1, 0, 1)", (guild_id, slot, other, user_id))
        await db.run("INSERT INTO stat_joins (guild_id, user_id, at, inviter_id) VALUES (?, ?, 1000, ?)", (guild_id, user_id, other))
        await db.run("INSERT INTO stat_invites (guild_id, code, inviter_id) VALUES (?, 'abc', ?)", (guild_id, user_id))
        await db.run("INSERT INTO stat_people (guild_id, user_id, name, seen_at) VALUES (?, ?, 'someone', 1)", (guild_id, user_id))
        await db.run("INSERT INTO stat_forum_posts (guild_id, thread_id, forum_id, owner_id, created_at, replier_id) VALUES (?, 7, 8, ?, 1, ?)", (guild_id, user_id, other))
        await db.run("INSERT INTO stat_recount_messages VALUES (?, 1, ?, ?, 0, 3)", (guild_id, slot, user_id))
        await db.run("INSERT INTO stat_members (guild_id, slot, members) VALUES (?, ?, 10)", (guild_id, slot))

    async def named(self, user_id):
        tables = [("stat_messages", "user_id"), ("stat_voice", "user_id"), ("stat_activities", "user_id"), ("stat_emoji", "user_id"), ("stat_emoji", "target_id"),
                  ("stat_joins", "user_id"), ("stat_invites", "inviter_id"), ("stat_people", "user_id"), ("stat_forum_posts", "owner_id"), ("stat_forum_posts", "replier_id"),
                  ("stat_recount_messages", "user_id")]
        return {f"{table}.{column}": (await self.rows(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (user_id,)))[0][0] for table, column in tables}

    async def test_privacy_moves_everything_in_every_server_and_waits_a_day_between_changes(self):
        other_server = 424242424242424242
        for guild_id in (self.guild.id, other_server):
            await self.seed_person(guild_id, self.member.id, self.admin.id)
        now = time.time()
        self.assertEqual(await self.stats.set_privacy(self.member.id, True, now), ("privacy_done_on", {}))
        self.assertEqual(set((await self.named(self.member.id)).values()), {0})
        self.assertEqual(await self.rows("SELECT SUM(count) FROM stat_messages WHERE guild_id = ? AND user_id = 0", (other_server,)), [(7,)], "totals stay")
        self.assertEqual(self.stats.recorded_as(self.member.id), 0)
        state = await self.stats.privacy_state(self.member.id, now + 60)
        self.assertTrue(state["private"])
        self.assertFalse(state["can_change"])
        self.assertEqual(await self.stats.set_privacy(self.member.id, False, now + 60), ("privacy_wait", {"changed": int(now), "next": int(now + DAY)}))
        self.assertEqual(await self.stats.set_privacy(self.member.id, False, now + DAY + 1), ("privacy_done_off", {}))
        self.assertEqual(self.stats.recorded_as(self.member.id), self.member.id, "new counts carry the name again")
        self.assertEqual((await self.stats.set_privacy(self.member.id, True, now + DAY + 60))[0], "privacy_wait")

    async def test_the_move_alone_takes_the_name_off_every_server_and_this_parts_tables(self):
        # The privacy command calls store.anonymize itself, so the tables a recount and the help forums keep have to come along with it.
        other_server = 424242424242424242
        for guild_id in (self.guild.id, other_server):
            await self.seed_person(guild_id, self.member.id, self.admin.id)
        await self.bot.message_db.run("INSERT INTO stat_forum_posts (guild_id, thread_id, forum_id, owner_id, created_at, replier_id) VALUES (?, 9, 8, ?, 1, ?)",
                                      (other_server, self.admin.id, self.member.id))
        await store.anonymize(self.bot.message_db, self.member.id)
        self.assertEqual(set((await self.named(self.member.id)).values()), {0})
        self.assertEqual(await self.rows("SELECT owner_id, replier_id FROM stat_forum_posts WHERE guild_id = ? ORDER BY thread_id", (other_server,)),
                         [(0, self.admin.id), (self.admin.id, 0)])
        self.assertEqual(await self.rows("SELECT guild_id, SUM(count) FROM stat_recount_messages WHERE user_id = 0 GROUP BY guild_id ORDER BY guild_id"),
                         sorted([(self.guild.id, 3), (other_server, 3)]))
        self.assertEqual(await store.servers_in(self.bot.message_db, "stat_messages"), sorted([self.guild.id, other_server]))
        self.assertEqual(await store.servers_in(self.bot.message_db, "stat_recount_places"), [], "an empty table holds no server")

    async def test_a_move_that_fails_keeps_new_counts_nameless_and_finishes_when_chosen_again(self):
        await self.seed_person(self.guild.id, self.member.id, self.admin.id)
        with mock.patch.object(store, "anonymize", side_effect=RuntimeError("the disk is full")), self.assertLogs(statistics_commands.log, "ERROR"):
            self.assertEqual(await self.stats.set_privacy(self.member.id, True), ("privacy_failed", {}))
        self.assertEqual(self.stats.recorded_as(self.member.id), 0)
        self.assertNotEqual(set((await self.named(self.member.id)).values()), {0})
        self.assertEqual(await self.stats.set_privacy(self.member.id, True), ("privacy_done_on", {}))
        self.assertEqual(set((await self.named(self.member.id)).values()), {0})

    async def test_my_data_shows_downloads_and_takes_the_name_off(self):
        await self.seed_person(self.guild.id, self.member.id, self.admin.id)
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(self.member.id, "member", None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        data = await (await client.get("/api/my-data")).json()
        here = next(server for server in data["servers"] if str(server["id"]) == str(self.guild.id))
        section = next(item for item in here["sections"] if item["key"] == "statistics")
        self.assertTrue(section["everywhere"])
        self.assertEqual(section["count"], 10)
        self.assertEqual(here["statistics"], {"messages": 5, "voice_hours": 0.0, "activity_hours": 0.0, "emoji": 1, "joins": 1})
        download = await (await client.get("/api/my-data/download")).json()
        self.assertEqual(len(download["tables"]["stat_messages.user_id"]), 1)
        self.assertEqual(len(download["tables"]["stat_emoji.target_id"]), 1)
        choices = await (await client.get("/api/my-data/statistics")).json()
        self.assertEqual((choices["private"], choices["private_replies"]), (False, False))
        replies = await client.post("/api/my-data/statistics", json={"private_replies": True})
        self.assertEqual(replies.status, 200, await replies.text())
        self.assertTrue((await replies.json())["private_replies"])
        for bad in ({"private": "yes"}, {}, ["private"]):
            self.assertEqual((await client.post("/api/my-data/statistics", json=bad)).status, 400, bad)
        removed = await client.post("/api/my-data/delete", json={"guild_id": str(self.guild.id), "section": "statistics"})
        self.assertEqual(removed.status, 200, await removed.text())
        self.assertNotIn("statistics", {item["key"] for server in (await removed.json())["servers"] for item in server["sections"]})
        self.assertEqual(set((await self.named(self.member.id)).values()), {0})
        self.assertEqual((await client.post("/api/my-data/statistics", json={"private": False})).status, 409, "a day has to pass")
        self.assertNotIn("mydata.delete", [entry["action"] for entry in await self.bot.activity_log.recent(self.guild.id)], "no server's Activity page says who chose privacy")


class HelpForums(DataTest):
    modules = [Statistics, MyData, ForumMessages]
    TAG = 555555555555555555

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.forum = self.history_channel("help", discord.ChannelType.forum)
        await self.bot.settings.save(self.guild.id, "forummessages", {"forums": [{"forum_id": self.forum.id, "solved_tag": self.TAG}]})

    def message(self, thread, author, seconds_after):
        return types.SimpleNamespace(guild=self.guild, channel=thread, author=author, id=snowflake(thread.created_at + datetime.timedelta(seconds=seconds_after)),
                                     created_at=thread.created_at + datetime.timedelta(seconds=seconds_after))

    async def test_posts_first_answers_and_the_solved_tag(self):
        post = self.thread(self.forum, "help me", self.member, ago(3600))
        await self.stats._forum_post_made(post)
        bot = FakeMember(self.guild, "helper", bot=True)
        await self.stats._forum_answer(self.message(post, self.member, 30))
        await self.stats._forum_answer(self.message(post, bot, 40))
        self.assertEqual(await self.rows("SELECT replied_at FROM stat_forum_posts"), [(None,)], "the poster and bots don't answer")
        await self.stats._forum_answer(self.message(post, self.admin, 600))
        await self.stats._forum_answer(self.message(post, self.mod, 900))
        self.assertEqual(await self.rows("SELECT owner_id, replier_id, CAST(replied_at - created_at AS INTEGER) FROM stat_forum_posts"), [(self.member.id, self.admin.id, 600)])
        solved = types.SimpleNamespace(**{**vars(post), "applied_tags": [types.SimpleNamespace(id=self.TAG)]})
        await self.stats._forum_tags(post, solved)
        self.assertEqual(await self.rows("SELECT solved_at IS NOT NULL FROM stat_forum_posts"), [(1,)])
        await self.stats._forum_tags(solved, post)
        self.assertEqual(await self.rows("SELECT solved_at FROM stat_forum_posts"), [(None,)])
        await self.stats._forum_tags(post, solved)
        client = await self.client(access="staff")
        page = await (await client.get(self.path("/statistics/insights/forums?days=7"))).json()
        self.assertEqual(len(page["forums"]), 1)
        forum = page["forums"][0]
        self.assertEqual((forum["name"], forum["posts"], forum["answered"], forum["median_seconds"], forum["solved"], forum["solved_share"]), ("help", 1, 1, 600, 1, 1))
        self.assertEqual(forum["within"], {"3600": 1, "86400": 1})
        await self.stats._forum_post_gone(types.SimpleNamespace(guild_id=self.guild.id, thread_id=post.id))
        self.assertEqual(await self.rows("SELECT COUNT(*) FROM stat_forum_posts"), [(0,)])

    async def test_a_post_from_before_tracking_gets_no_answer_time(self):
        post = self.thread(self.forum, "old", self.member, ago(30 * DAY))
        await self.stats._forum_answer(self.message(post, self.admin, 60))
        self.assertEqual(await self.rows("SELECT COUNT(*) FROM stat_forum_posts"), [(0,)])

    async def test_privacy_covers_forum_posts(self):
        post = self.thread(self.forum, "help me", self.member, ago(3600))
        self.stats.private.add(self.member.id)
        await self.stats._forum_post_made(post)
        self.assertEqual(await self.rows("SELECT owner_id FROM stat_forum_posts"), [(0,)])
        await self.stats._forum_answer(self.message(post, self.member, 30))
        self.assertEqual(await self.rows("SELECT replied_at FROM stat_forum_posts"), [(None,)], "the poster's own message isn't an answer, private or not")


class Insights(DataTest):
    modules = [Statistics, MyData, Moderation, Reports, Logs]

    async def test_moderation_over_time(self):
        now = time.time()
        await self.db.many("INSERT INTO cases (guild_id, number, action, user_id, user_name, moderator_id, moderator_name, source, reason, created_at, voided_at) "
                           "VALUES (?, ?, ?, ?, 'x', ?, ?, ?, ?, ?, ?)", [
                               (self.guild.id, 1, "warn", self.member.id, self.mod.id, "mod", "command", "rude", now - 3 * DAY, None),
                               (self.guild.id, 2, "ban", self.member.id, self.mod.id, "mod", "command", "spam", now - 2 * DAY, None),
                               (self.guild.id, 3, "warn", self.member.id, None, None, "automod", "Automod: Link spam", now - DAY, None),
                               (self.guild.id, 4, "mute", self.member.id, self.mod.id, "mod", "command", "oops", now - DAY, now - DAY + 60),
                               (self.guild.id, 5, "kick", self.member.id, self.mod.id, "mod", "command", "old", now - 200 * DAY, None),
                           ])
        await self.message_db.many("INSERT INTO log_entries (guild_id, at, event, embed, search_text) VALUES (?, ?, 'automod', ?, '')", [
            (self.guild.id, now - DAY, json.dumps({"title": "Automod: Link spam"})), (self.guild.id, now - DAY, json.dumps({"title": "Automod: Link spam (edited in)"})),
            (self.guild.id, now - DAY, json.dumps({"title": "Automod: Caps (dry run)"})), (self.guild.id, now - DAY, "not json"),
        ])
        await self.db.many("INSERT INTO reports (guild_id, number, kind, state, reporter_id, reporter_name, target_id, target_name, reason, created_at, handled_by, handled_name, handled_at) "
                           "VALUES (?, ?, 'user', ?, 1, 'r', 2, 't', 'why', ?, ?, ?, ?)", [
                               (self.guild.id, 1, "open", now - DAY, None, None, None),
                               (self.guild.id, 2, "actioned", now - 2 * DAY, self.admin.id, "admin", now - 2 * DAY + 600),
                               (self.guild.id, 3, "false_positive", now - 2 * DAY, self.admin.id, "admin", now - 2 * DAY + 1200),
                           ])
        client = await self.client(access="staff")
        response = await client.get(self.path("/statistics/insights/moderation?days=30"))
        self.assertEqual(response.status, 200, await response.text())
        page = await response.json()
        self.assertEqual((page["cases"]["total"], page["cases"]["voided"]), (3, 1))
        self.assertEqual({row["action"]: row["total"] for row in page["cases"]["by_action"]}, {"warn": 2, "ban": 1})
        self.assertEqual({row["name"]: row["total"] for row in page["cases"]["moderators"]}, {"mod": 2, "Automod": 1})
        self.assertEqual({row["name"]: sum(row["series"]) for row in page["cases"]["moderators"]}, {"mod": 2, "Automod": 1}, "each moderator over time")
        self.assertEqual(page["automod"]["punishments"], [{"rule": "Link spam", "count": 1}])
        self.assertEqual([(row["rule"], row["count"]) for row in page["automod"]["hits"]], [("Link spam", 2)])
        self.assertEqual(page["automod"]["dry_runs_only"], [{"rule": "Caps", "count": 1}])
        reports = page["reports"]
        self.assertEqual((reports["opened_total"], reports["handled_total"], reports["open_now"], reports["median_seconds"]), (3, 2, 1, 900))
        self.assertEqual(reports["outcomes"], {"actioned": 1, "false_positive": 1})
        self.assertEqual(reports["handlers"][0]["name"], "admin")
        self.assertEqual(page["log_days"], 90)
        self.assertEqual(sum(sum(row["series"]) for row in page["cases"]["by_action"]), 3)

    async def test_newcomers_who_stay_and_speak(self):
        now = time.time()
        joined = now - 20 * DAY
        stays = FakeMember(self.guild, "stays")
        quiet = FakeMember(self.guild, "quiet")
        left_id = 900000000000000999
        await self.message_db.many("INSERT INTO stat_joins (guild_id, user_id, at, left_at) VALUES (?, ?, ?, ?)", [
            (self.guild.id, stays.id, joined, None), (self.guild.id, quiet.id, joined + 60, None), (self.guild.id, left_id, joined + 120, joined + DAY), (self.guild.id, 0, joined, None),
            (self.guild.id, self.member.id, now - DAY, None),
        ])
        await self.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 1, ?, ?, 1)", [
            (self.guild.id, tf.slot_of(joined + 3600), stays.id, 0), (self.guild.id, tf.slot_of(joined + 10 * DAY), stays.id, 0),
            (self.guild.id, tf.slot_of(joined + 3600), left_id, 0), (self.guild.id, tf.slot_of(joined + 15 * DAY), quiet.id, store.BOT),
        ])
        client = await self.client(access="staff")
        page = await (await client.get(self.path("/statistics/insights/newcomers?weeks=6"))).json()
        cohort = next(row for row in page["weeks"] if row["joined"] == 4)
        self.assertEqual((cohort["still_here"], cohort["spoke"]), (2, 2))
        self.assertEqual(cohort["spoke_after"], {"1": 1, "7": 1, "30": None})
        latest = next(row for row in page["weeks"] if row["start"] <= now - DAY < row["start"] + 8 * DAY and row["joined"])
        self.assertEqual((latest["joined"], latest["spoke_after"]["7"]), (1, None))


class OpenChannel(FakeChannel):
    """A channel everyone in the server can see and post in."""

    def permissions_for(self, member):
        return FakePermissions("view_channel", "send_messages", "embed_links", "read_message_history")


class StaffChannel(FakeChannel):
    """A channel only Administrators can see."""

    def permissions_for(self, member):
        return FakePermissions("administrator") if member.guild_permissions.administrator else FakePermissions()


class ScheduledReports(DataTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.public = OpenChannel(self.guild, "announcements")
        self.secret = StaffChannel(self.guild, "staff-chat")
        now = time.time()
        rows = []
        for days in range(1, 13):
            slot = tf.slot_of(now - days * DAY)
            rows += [(slot, self.secret.id, self.member.id, 5), (slot, self.public.id, self.admin.id, 1)]
        await self.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, ?)",
                                   [(self.guild.id, *row) for row in rows])

    async def create(self, client, channel, **form):
        response = await client.post(self.path("/statistics/data/reports"), json={"channel_id": str(channel.id), "cadence": "week", "weekday": 0, "hour": 9, **form})
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())["reports"][-1]

    async def test_a_report_is_scheduled_posted_and_rescheduled(self):
        client = await self.client()
        report = await self.create(client, self.public)
        key = f"{self.guild.id}:{report['id']}"
        timer = await self.bot.timers.get_key(statistics_data.REPORT_TIMER, key)
        self.assertIsNotNone(timer)
        self.assertEqual(timer["due_at"], report["next_at"])
        await self.stats._report_due({"guild_id": self.guild.id, "id": report["id"]}, timer)
        embed = self.public.sent[-1].embeds[0]
        self.assertEqual(embed.title, "Weekly statistics")
        fields = {field.name: field.value for field in embed.fields}
        self.assertTrue(fields["Messages"].startswith("42 ("), fields["Messages"])
        self.assertIn("1. member: 35", fields["Most messages"])
        self.assertIn("1. Hidden channel: 35", fields["Busiest channels"], "everyone can read this channel, and not everyone can see that one")
        self.assertIn("2. #announcements: 7", fields["Busiest channels"])
        row = await self.db.one("SELECT * FROM stat_reports WHERE id = ?", (report["id"],))
        self.assertIsNotNone(row["last_at"])
        self.assertGreater(row["next_at"], time.time())
        self.assertEqual((await self.bot.timers.get_key(statistics_data.REPORT_TIMER, key))["due_at"], row["next_at"])

    async def test_a_staff_channel_shows_the_names_its_readers_can_see(self):
        client = await self.client()
        report = await self.create(client, self.secret, cadence="month", hour=8)
        response = await client.post(self.path(f"/statistics/data/reports/{report['id']}/send"))
        self.assertEqual(response.status, 200, await response.text())
        fields = {field.name: field.value for field in self.secret.sent[-1].embeds[0].fields}
        self.assertEqual(self.secret.sent[-1].embeds[0].title, "Monthly statistics")
        self.assertNotIn("Hidden channel", fields.get("Busiest channels", ""))

    async def test_what_a_report_refuses_and_how_it_goes(self):
        client = await self.client()
        voice = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)
        for form in ({"channel_id": str(voice.id), "cadence": "week"}, {"channel_id": str(self.public.id), "cadence": "daily"},
                     {"channel_id": str(self.public.id), "cadence": "week", "hour": 24}, {"channel_id": str(self.public.id), "cadence": "week", "weekday": 7}):
            self.assertEqual((await client.post(self.path("/statistics/data/reports"), json=form)).status, 400, form)
        report = await self.create(client, self.public)
        await self.bot.timers.cancel_key(statistics_data.REPORT_TIMER, f"{self.guild.id}:{report['id']}")
        await self.stats.on_guild_restored(self.guild.id)
        self.assertIsNotNone(await self.bot.timers.get_key(statistics_data.REPORT_TIMER, f"{self.guild.id}:{report['id']}"), "a restore brings the timer back")
        deleted = await client.delete(self.path(f"/statistics/data/reports/{report['id']}"))
        self.assertEqual(deleted.status, 200)
        self.assertEqual((await deleted.json())["reports"], [])
        self.assertIsNone(await self.bot.timers.get_key(statistics_data.REPORT_TIMER, f"{self.guild.id}:{report['id']}"))
        self.guild.channels.remove(self.public)
        await self.db.run("INSERT INTO stat_reports (guild_id, id, channel_id, cadence, created_at) VALUES (?, 'abcdef0123456789', ?, 'week', 1)", (self.guild.id, self.public.id))
        await self.stats._report_due({"guild_id": self.guild.id, "id": "abcdef0123456789"}, {})
        self.assertEqual(await self.db.scalar("SELECT last_error FROM stat_reports WHERE id = 'abcdef0123456789'"), "The report's channel no longer exists.")


class Pieces(unittest.TestCase):
    def test_name_patterns(self):
        pattern = merge.name_pattern("ticket-*")
        self.assertTrue(pattern.fullmatch("ticket-12"))
        self.assertTrue(pattern.fullmatch("Ticket-Archive"))
        self.assertFalse(pattern.fullmatch("old-ticket-12"))
        self.assertTrue(merge.name_pattern("\\*important").fullmatch("*important"))
        self.assertFalse(merge.name_pattern("\\*important").fullmatch("very-important"))
        self.assertTrue(merge.name_pattern("a\\\\b").fullmatch("a\\b"))
        self.assertTrue(merge.name_pattern("ends\\").fullmatch("ends\\"))
        self.assertTrue(merge.name_pattern("*").fullmatch("anything at all"))
        self.assertTrue(merge.name_pattern("a.b").fullmatch("a.b"))
        self.assertFalse(merge.name_pattern("a.b").fullmatch("axb"))

    def test_periods_cover_whole_quarter_hours(self):
        self.assertEqual(history.period_slots(900 * 10 + 5, 900 * 20 + 1), (10, 21))
        self.assertEqual(history.period_slots(900 * 10, 900 * 20), (10, 20))
        self.assertEqual(history.snowflake_at(1420070400), 0)
        moment = datetime.datetime(2025, 1, 1, tzinfo=UTC)
        self.assertEqual(history.snowflake_at(moment.timestamp()), discord.utils.time_snowflake(moment))

    def test_reports_go_out_at_the_hour_in_the_servers_zone(self):
        la = tf.zone("America/Los_Angeles")
        friday = datetime.datetime(2026, 11, 6, 15, tzinfo=la).timestamp()  # a Friday afternoon
        monday = next_report_at("week", 0, 9, la, friday)
        self.assertEqual(datetime.datetime.fromtimestamp(monday, la), datetime.datetime(2026, 11, 9, 9, tzinfo=la))
        self.assertEqual(datetime.datetime.fromtimestamp(next_report_at("week", 4, 9, la, friday), la), datetime.datetime(2026, 11, 13, 9, tzinfo=la))
        self.assertEqual(datetime.datetime.fromtimestamp(next_report_at("week", 4, 16, la, friday), la), datetime.datetime(2026, 11, 6, 16, tzinfo=la))
        self.assertEqual(datetime.datetime.fromtimestamp(next_report_at("month", 0, 9, la, friday), la), datetime.datetime(2026, 12, 1, 9, tzinfo=la))
        march = datetime.datetime(2026, 3, 2, 10, tzinfo=la).timestamp()
        self.assertEqual(datetime.datetime.fromtimestamp(next_report_at("week", 0, 9, la, march), la), datetime.datetime(2026, 3, 9, 9, tzinfo=la), "across the spring change")

    def test_a_report_covers_the_last_full_week_or_month(self):
        la = tf.zone("America/Los_Angeles")
        monday = datetime.datetime(2026, 11, 9, 9, tzinfo=la).timestamp()
        end, start, previous = report_period("week", la, monday)
        self.assertEqual([datetime.datetime.fromtimestamp(value, la).date() for value in (previous, start, end)],
                         [datetime.date(2026, 10, 26), datetime.date(2026, 11, 2), datetime.date(2026, 11, 9)])
        self.assertEqual(end - start, 7 * DAY)
        self.assertEqual(start - previous, 7 * DAY + 3600, "the week the clocks went back, on November 1, is an hour longer")
        end, start, previous = report_period("month", la, datetime.datetime(2026, 12, 1, 9, tzinfo=la).timestamp())
        self.assertEqual([datetime.datetime.fromtimestamp(value, la).date() for value in (previous, start, end)],
                         [datetime.date(2026, 10, 1), datetime.date(2026, 11, 1), datetime.date(2026, 12, 1)])

    def test_change_text(self):
        self.assertEqual(beyond.change_text(110, 100), "up 10%")
        self.assertEqual(beyond.change_text(90, 100), "down 10%")
        self.assertEqual(beyond.change_text(100, 100), "no change")
        self.assertEqual(beyond.change_text(5, 0), "new")
        self.assertEqual(beyond.change_text(0, 0), "no change")

    def test_automod_titles(self):
        self.assertEqual(beyond.automod_rule("Automod: Link spam"), ("Link spam", False))
        self.assertEqual(beyond.automod_rule("Automod: Bad words (edited in)"), ("Bad words", False))
        self.assertEqual(beyond.automod_rule("Automod: Caps (dry run)"), ("Caps", True))
        self.assertIsNone(beyond.automod_rule("Member joined"))
        self.assertIsNone(beyond.automod_rule("Automod: slow bad-word pattern"))


if __name__ == "__main__":
    unittest.main()
