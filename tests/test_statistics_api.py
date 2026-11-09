"""The statistics API: keys, limits, and every endpoint with its parameters and answers."""
from __future__ import annotations

import datetime
import json
import time
import types
import unittest

import discord

from captainguild.core.ops import DiscordOps
from captainguild.core.stats import devapi, store
from captainguild.core.stats import timeframe as tf
from captainguild.modules.backup import Backup
from captainguild.modules.statistics import Statistics
from tests.fakes import BotTest, FakeChannel, FakeMember

UTC = datetime.timezone.utc
T0 = int(datetime.datetime(2026, 10, 5, tzinfo=UTC).timestamp())  # a Monday
HOUR, DAY = 3600, 86400
MINECRAFT = store.activity_id(store.PLAYING, 123456789012345678, "Minecraft")
SPOTIFY = store.activity_id(store.LISTENING, None, "Spotify")
AGGREGATE = 4242


def ms(seconds: float) -> int:
    return int(seconds * 1000)


def slot(seconds: float) -> int:
    return tf.slot_of(seconds)


class ApiTest(BotTest):
    modules = [Statistics, Backup]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        self.bot.ops = DiscordOps(self.bot)
        g = self.guild.id
        self.category = FakeChannel(self.guild, "Text", discord.ChannelType.category)
        self.general = FakeChannel(self.guild, "general", category=self.category)
        self.help = FakeChannel(self.guild, "help")
        self.lounge = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)
        self.robot = FakeMember(self.guild, "robot", bot=True)
        self.old = 1_200_000_000_000_000_007
        db = self.message_db
        await db.many("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id, created_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (g, self.general.id, "general", 0, self.category.id, T0 - 100 * DAY, None), (g, self.help.id, "help", 0, None, T0 - 100 * DAY, None),
            (g, self.lounge.id, "Lounge", 2, None, T0 - 100 * DAY, None), (g, self.category.id, "Text", 4, None, T0 - 100 * DAY, None),
            (g, self.old, "old-chat", 0, None, T0 - 300 * DAY, T0 - 200 * DAY), (g, AGGREGATE, "Old stuff", 999, None, T0, None),
        ])
        await db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)", [
            (g, slot(T0 + HOUR), self.general.id, self.member.id, 0, 3), (g, slot(T0 + HOUR), self.general.id, self.admin.id, 0, 2),
            (g, slot(T0 + HOUR + 900), self.general.id, self.member.id, store.THREAD, 1), (g, slot(T0 + 25 * HOUR), self.help.id, self.member.id, 0, 4),
            (g, slot(T0 + 25 * HOUR), self.help.id, self.robot.id, store.BOT, 5), (g, slot(T0 + 26 * HOUR), self.help.id, 0, 0, 7),
            (g, slot(T0 + 8 * DAY), self.general.id, self.admin.id, 0, 1),
        ])
        await db.many("INSERT INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (g, slot(T0 + HOUR), self.lounge.id, self.member.id, store.TALKING, 0, 1800), (g, slot(T0 + HOUR), self.lounge.id, self.admin.id, store.SELF_MUTED, 0, 600),
            (g, slot(T0 + 30 * HOUR), self.lounge.id, self.robot.id, store.TALKING, store.BOT, 300),
        ])
        await db.many("INSERT INTO stat_activities (guild_id, slot, user_id, activity_id, flags, seconds) VALUES (?, ?, ?, ?, 0, ?)", [
            (g, slot(T0 + HOUR), self.member.id, MINECRAFT, 3600), (g, slot(T0 + HOUR), self.admin.id, SPOTIFY, 1200)])
        await db.many("INSERT INTO stat_activity_names (guild_id, activity_id, kind, application_id, name, verified, first_seen) VALUES (?, ?, ?, ?, ?, 1, ?)", [
            (g, MINECRAFT, store.PLAYING, 123456789012345678, "Minecraft", T0), (g, SPOTIFY, store.LISTENING, None, "Spotify", T0)])
        await db.many("INSERT INTO stat_status (guild_id, slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)", [
            (g, slot(T0 + HOUR), 2, 20, 4, 2, 2, 0, 0), (g, slot(T0 + HOUR + 900), 1, 12, 0, 1, 0, 0, 0), (g, slot(T0 + 25 * HOUR), 0, 0, 0, 0, 0, 2, 90)])
        await db.many("INSERT INTO stat_members (guild_id, slot, members, humans, bots, joins, leaves) VALUES (?, ?, ?, NULL, NULL, ?, ?)", [
            (g, slot(T0), 100, 2, 1), (g, slot(T0 + HOUR), 101, 1, 0), (g, slot(T0 + 25 * HOUR), None, 0, 1), (g, slot(T0 + 2 * DAY), 103, 0, 0)])
        await db.many("INSERT INTO stat_invites (guild_id, code, inviter_id, channel_id, uses, max_uses, max_age, temporary, vanity, created_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (g, "abc", self.admin.id, self.general.id, 3, 10, 86400, 0, 0, T0 - DAY, None), (g, "vanity:printers", None, None, 50, None, None, 0, 1, None, None),
            (g, "def", self.mod.id, self.help.id, 1, None, None, 0, 0, T0 - DAY, T0)])
        self.outsider = 900_000_000_000_000_555
        await db.many("INSERT INTO stat_joins (guild_id, user_id, at, code, inviter_id, account_created, facts, role_met_at, left_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)", [
            (g, self.member.id, T0 + HOUR, "abc", self.admin.id, T0 - 400 * DAY, 0, None), (g, self.mod.id, T0 + 2 * HOUR, "abc", self.admin.id, T0 - 400 * DAY, store.JOIN_NO_AVATAR, None),
            (g, self.robot.id, T0 + 3 * HOUR, "def", self.mod.id, T0 - 400 * DAY, store.JOIN_BOT, None), (g, self.outsider, T0 + 4 * HOUR, None, None, T0 - 400 * DAY, 0, T0 + 5 * HOUR)])
        await db.run("INSERT INTO stat_people (guild_id, user_id, name, display_name, nick, avatar, bot, seen_at) VALUES (?, ?, 'member', 'Member', NULL, 'abc123', 0, ?)", (g, self.member.id, T0))
        await self.bot.settings.save(g, "statistics", {"enabled": True, "invite_labels": {"abc": "Website"}, "aggregates": [
            {"id": AGGREGATE, "name": "Old stuff", "category_id": None, "auto_categories": [self.category.id], "kinds": "messages"}]})
        self.dashboard = await self.client()
        response = await self.dashboard.post(self.path("/statistics/data/api/key"))
        self.assertEqual(response.status, 200, await response.text())
        self.key = (await response.json())["new_key"]

    async def call(self, path, params=None, *, key=None, guild=None):
        """GET a path under /api/stats/v1/guilds/{guild}, or under /api/stats when it starts with /v1."""
        target = path if path.startswith("/v1") else f"/v1/guilds/{guild or self.guild.id}{path}"
        headers = {} if key is False else {"Authorization": f"Bearer {key or self.key}"}
        response = await self.dashboard.get(f"/api/stats{target}", params=params or [], headers=headers)
        body = await response.json()
        return response.status, body, response.headers

    async def ok(self, path, params=None):
        status, body, headers = await self.call(path, params)
        self.assertEqual(status, 200, body)
        return body

    def points(self, rows, *fields):
        return [tuple(row.get(field) for field in ("unixTimestamp", *fields, "count")) for row in rows]


def window(start=T0, end=T0 + 10 * DAY, **extra):
    params = [("start", str(ms(start))), ("end", str(ms(end)))]
    for name, value in extra.items():
        for item in (value if isinstance(value, list) else [value]):
            params.append((name, str(item)))
    return params


class Keys(ApiTest):
    async def test_a_key_shows_once_and_only_its_hash_is_kept(self):
        self.assertRegex(self.key, r"^cgstats_[A-Za-z0-9_-]{43}$")
        page = await (await self.dashboard.get(self.path("/statistics/data/api"))).json()
        self.assertNotIn("new_key", page)
        self.assertEqual(page["key"]["hint"], self.key[-4:])
        self.assertIn("/v1/guilds/{guild_id}/messages/series", page["paths"])
        stored = await self.db.one("SELECT * FROM stat_api_keys WHERE guild_id = ?", (self.guild.id,))
        self.assertNotIn(self.key, json.dumps(stored))
        self.assertEqual(len(stored["key_hash"]), 64)

    async def test_replacing_and_deleting_end_the_old_key(self):
        old = self.key
        fresh = (await (await self.dashboard.post(self.path("/statistics/data/api/key"))).json())["new_key"]
        self.assertNotEqual(fresh, old)
        self.assertEqual((await self.call("/messages/sums", key=old))[0], 401)
        self.assertEqual((await self.call("/messages/sums", key=fresh))[0], 200)
        await self.dashboard.delete(self.path("/statistics/data/api/key"))
        self.assertEqual((await self.call("/messages/sums", key=fresh))[0], 401)
        actions = [entry["action"] for entry in await self.bot.activity_log.recent(self.guild.id)]
        self.assertIn("statistics.api_key_delete", actions)
        self.assertIn("statistics.api_key_replace", actions)

    async def test_only_the_owner_and_administrators_manage_the_key(self):
        staff = await self.client(access="staff")
        self.assertEqual((await staff.get(self.path("/statistics/data/api"))).status, 403)
        self.assertEqual((await staff.post(self.path("/statistics/data/api/key"))).status, 403)
        self.assertEqual((await staff.delete(self.path("/statistics/data/api/key"))).status, 403)
        self.admin.roles = [self.guild.default_role, self.guild.role("Manager", 4, "manage_guild", "view_channel")]  # full access without Administrator
        manager = await self.client(access="full")
        page = await (await manager.get(self.path("/statistics/data/api"))).json()
        self.assertEqual((page["key"], page["is_admin"]), (None, False))
        self.assertIn("Administrator", page["note"])
        self.assertEqual((await manager.post(self.path("/statistics/data/api/key"))).status, 403)
        self.assertEqual((await manager.delete(self.path("/statistics/data/api/key"))).status, 403)

    async def test_what_isnt_let_in(self):
        for key in (False, "nope", "cgstats_" + "a" * 43):
            status, body, _ = await self.call("/messages/sums", key=key)
            self.assertEqual((status, body), (401, {"message": "Unauthorized"}), key)
        status, body, headers = await self.call("/messages/sums", guild=123456789012345678)
        self.assertEqual(status, 401, "a key works for its own server only")
        lower = await self.dashboard.get(f"/api/stats/v1/guilds/{self.guild.id}/messages/sums", headers={"Authorization": f"bearer {self.key}"})
        self.assertEqual(lower.status, 200, "the scheme isn't case sensitive")

    async def test_rate_limit_headers_and_the_limit(self):
        self.stats._data.limiter.per_window = 3
        for remaining in (2, 1, 0):
            status, _, headers = await self.call("/messages/sums")
            self.assertEqual(status, 200)
            self.assertEqual((headers["x-ratelimit-limit"], headers["x-ratelimit-remaining"]), ("3", str(remaining)))
            self.assertTrue(1 <= int(headers["x-ratelimit-reset"]) <= 60)
        status, body, headers = await self.call("/messages/sums")
        self.assertEqual(status, 429)
        self.assertIn("message", body)
        self.assertTrue(1 <= int(headers["retry-after"]) <= 60)
        self.assertEqual(headers["x-ratelimit-remaining"], "0")

    def test_the_reset_stays_within_the_window_at_any_clock_reading(self):
        from captainguild.core.stats import devapi
        for now in (452.007, 452.008, 1023.5, 1_794_199_866.2417974):  # the first two round (now + 60) - now up past 60
            limiter = devapi.Limiter(per_window=3, seconds=60)
            for _ in range(4):
                _, headers = limiter.take(1, now)
                self.assertEqual(headers["x-ratelimit-reset"], "60", now)
            self.assertEqual(headers["retry-after"], "60", now)

    async def test_the_key_never_goes_into_a_backup(self):
        body = bytearray()
        async for block in self.cog(Backup).export_stream(self.guild.id, include_messages=True):
            body += block
        tables = json.loads(bytes(body))["tables"]
        self.assertFalse([name for name in tables if "stat_api_keys" in name])
        self.assertTrue([name for name in tables if name.endswith("stat_messages")])


class Messages(ApiTest):
    async def test_series_by_day_with_only_the_buckets_that_hold_something(self):
        rows = await self.ok("/messages/series", window())
        self.assertEqual(self.points(rows), [(ms(T0), 6), (ms(T0 + DAY), 16), (ms(T0 + 8 * DAY), 1)])
        self.assertEqual(self.points(await self.ok("/messages/series", window(bot="false"))), [(ms(T0), 6), (ms(T0 + DAY), 11), (ms(T0 + 8 * DAY), 1)])
        self.assertEqual(self.points(await self.ok("/messages/series", window(bot="true"))), [(ms(T0 + DAY), 5)])

    async def test_groupings(self):
        rows = await self.ok("/messages/series", window(by_member="true"))
        self.assertEqual(self.points(rows, "memberId"), [
            (ms(T0), str(self.admin.id), 2), (ms(T0), str(self.member.id), 4), (ms(T0 + DAY), None, 7), (ms(T0 + DAY), str(self.member.id), 4),
            (ms(T0 + DAY), str(self.robot.id), 5), (ms(T0 + 8 * DAY), str(self.admin.id), 1)])
        rows = await self.ok("/messages/series", window(end=T0 + DAY - 1, by_channel="true", by_flag="true", interval="hour"))
        self.assertEqual(self.points(rows, "channelId", "flags"), [(ms(T0 + HOUR), str(self.general.id), 0, 5), (ms(T0 + HOUR), str(self.general.id), store.THREAD, 1)])
        self.assertNotIn("memberId", rows[0])

    async def test_intervals_and_time_zones(self):
        self.assertEqual(self.points(await self.ok("/messages/series", window(interval="week"))), [(ms(T0), 22), (ms(T0 + 7 * DAY), 1)])
        october = int(datetime.datetime(2026, 10, 1, tzinfo=UTC).timestamp())
        self.assertEqual(self.points(await self.ok("/messages/series", window(interval="month"))), [(ms(october), 23)])
        pacific = await self.ok("/messages/series", window(timezone_offset=-8))
        self.assertEqual(self.points(pacific), [(ms(T0 - DAY + 8 * HOUR), 6), (ms(T0 + 8 * HOUR), 16), (ms(T0 + 7 * DAY + 8 * HOUR), 1)])
        nepal = await self.ok("/messages/series", window(timezone_offset=5.75))
        self.assertEqual(nepal[0]["unixTimestamp"], ms(T0 - 5 * HOUR - 45 * 60))

    async def test_the_end_counts_the_millisecond_it_names(self):
        self.assertEqual(self.points(await self.ok("/messages/series", window(end=T0 + 8 * DAY)))[-1], (ms(T0 + 8 * DAY), 1))
        self.assertEqual(self.points(await self.ok("/messages/series", window(end=T0 + 8 * DAY - 0.001)))[-1], (ms(T0 + DAY), 16))

    async def test_order_and_limit(self):
        self.assertEqual(self.points(await self.ok("/messages/series", window(order="desc", limit=1))), [(ms(T0 + 8 * DAY), 1)])
        self.assertEqual(self.points(await self.ok("/messages/series", window(limit=2))), [(ms(T0), 6), (ms(T0 + DAY), 16)])

    async def test_channel_member_and_role_lists(self):
        def series(**extra):
            return self.ok("/messages/series", window(**extra))
        self.assertEqual(self.points(await series(**{"whitelist_channels[]": self.help.id})), [(ms(T0 + DAY), 16)])
        self.assertEqual(self.points(await series(**{"blacklist_channels[]": self.help.id})), [(ms(T0), 6), (ms(T0 + 8 * DAY), 1)])
        self.assertEqual(self.points(await series(**{"whitelist_channels[]": self.category.id})), [(ms(T0), 6), (ms(T0 + 8 * DAY), 1)], "a category covers its channels")
        self.assertEqual(self.points(await series(**{"whitelist_members[]": self.member.id})), [(ms(T0), 4), (ms(T0 + DAY), 4)])
        self.assertEqual(self.points(await series(**{"blacklist_members[]": [self.member.id, self.robot.id]})), [(ms(T0), 2), (ms(T0 + DAY), 7), (ms(T0 + 8 * DAY), 1)])
        self.assertEqual(self.points(await series(**{"whitelist_roles[]": self.role_member.id})), [(ms(T0), 4), (ms(T0 + DAY), 4)])
        self.assertEqual(self.points(await series(**{"blacklist_roles[]": self.role_admin.id})), [(ms(T0), 4), (ms(T0 + DAY), 16)])
        self.assertEqual(self.points(await self.ok("/messages/series", window() + [("whitelist_members", str(self.member.id))])), [(ms(T0), 4), (ms(T0 + DAY), 4)],
                         "a list without brackets works too")

    async def test_sums(self):
        self.assertEqual(await self.ok("/messages/sums", window()), {"count": 23})
        self.assertEqual(await self.ok("/messages/sums", window(bot="false")), {"count": 18})
        self.assertEqual(await self.ok("/messages/sums", window(**{"whitelist_channels[]": self.general.id})), {"count": 7})

    async def test_top_members(self):
        rows = await self.ok("/messages/tops/members", window())
        self.assertEqual([(row["id"], row["rank"], row["count"], row["channelCount"]) for row in rows],
                         [(str(self.member.id), 1, 8, 2), (str(self.robot.id), 2, 5, 1), (str(self.admin.id), 3, 3, 1)])
        self.assertEqual(set(rows[0]), {"id", "rank", "count", "channelCount"})
        full = await self.ok("/messages/tops/members", window(full="true"))
        self.assertEqual({key: full[0][key] for key in ("username", "type", "globalName", "nick", "avatar", "guildAvatar")},
                         {"username": "member", "type": "user", "globalName": "member", "nick": "", "avatar": "abc123", "guildAvatar": ""})
        self.assertEqual(full[1]["type"], "bot")
        self.assertEqual([row["rank"] for row in await self.ok("/messages/tops/members", window(page_size=2, page=2))], [3])
        self.assertEqual([row["id"] for row in await self.ok("/messages/tops/members", window(**{"select[]": self.admin.id}))], [str(self.admin.id)])
        self.assertEqual([row["rank"] for row in await self.ok("/messages/tops/members", window(order="desc"))], [3, 2, 1])
        self.assertEqual([row["rank"] for row in await self.ok("/messages/tops/members", window(limit=1))], [1])

    async def test_top_channels(self):
        rows = await self.ok("/messages/tops/channels", window())
        self.assertEqual([(row["id"], row["rank"], row["count"], row["memberCount"]) for row in rows], [(str(self.help.id), 1, 16, 2), (str(self.general.id), 2, 7, 2)])
        full = await self.ok("/messages/tops/channels", window(full="true"))
        self.assertEqual({key: full[1][key] for key in ("id", "name", "type", "parentId", "isActive", "rank")},
                         {"id": str(self.general.id), "name": "general", "type": 0, "parentId": str(self.category.id), "isActive": 1, "rank": 2})

    async def test_requests_that_cant_be_answered(self):
        for params in (window(**{"whitelist_members[]": 1, "blacklist_members[]": 2}), window(limit=1, page_size=2, page=1), window(page=1),
                       window(timezone_offset=0.3), window(timezone_offset=15), window(interval="year"), window(order="up"), window(bot="maybe"),
                       [("start", "soon")], [("start", "100"), ("end", "50")], window(**{"whitelist_channels[]": "general"}), [("end", "9" * 19)]):
            status, body, _ = await self.call("/messages/series" if "page" not in str(params) else "/messages/tops/members", params)
            self.assertEqual(status, 400, params)
            self.assertIn("message", body)


class Voice(ApiTest):
    async def test_voice_in_minutes(self):
        self.assertEqual(self.points(await self.ok("/voice/series", window())), [(ms(T0), 40), (ms(T0 + DAY), 5)])
        self.assertEqual(self.points(await self.ok("/voice/series", window(by_state="true")), "type"), [(ms(T0), 1, 30), (ms(T0), 3, 10), (ms(T0 + DAY), 1, 5)])
        self.assertEqual(self.points(await self.ok("/voice/series", window(by_flag="true")), "flags"), [(ms(T0), 0, 40), (ms(T0 + DAY), store.BOT, 5)])
        self.assertEqual(self.points(await self.ok("/voice/series", window(**{"voice_states[]": "self_mute"}))), [(ms(T0), 10)])
        self.assertEqual(self.points(await self.ok("/voice/series", window(bot="false"))), [(ms(T0), 40)])
        self.assertEqual(await self.ok("/voice/sums", window()), {"count": 45})
        self.assertEqual(await self.ok("/voice/sums", window(**{"voice_states[]": ["normal", "afk"]})), {"count": 35})
        self.assertEqual((await self.call("/voice/sums", window(**{"voice_states[]": "loud"})))[0], 400)

    async def test_voice_tops(self):
        rows = await self.ok("/voice/tops/members", window())
        self.assertEqual([(row["id"], row["rank"], row["count"], row["channelCount"]) for row in rows],
                         [(str(self.member.id), 1, 30, 1), (str(self.admin.id), 2, 10, 1), (str(self.robot.id), 3, 5, 1)])
        rows = await self.ok("/voice/tops/channels", window(full="true"))
        self.assertEqual([(row["id"], row["count"], row["memberCount"], row["name"], row["type"]) for row in rows], [(str(self.lounge.id), 45, 3, "Lounge", 2)])
        rows = await self.ok("/voice/series", window(**{"whitelist_voice_channels[]": self.lounge.id, "whitelist_channels[]": self.help.id}))
        self.assertEqual(self.points(rows), [(ms(T0), 40), (ms(T0 + DAY), 5)], "voice reads its own channel list when one is given")


class Activities(ApiTest):
    async def test_the_list_series_and_tops(self):
        self.assertEqual(await self.ok("/v1/activities"), [
            {"id": devapi.activity_number(MINECRAFT), "name": "Minecraft", "applicationId": "123456789012345678"},
            {"id": devapi.activity_number(SPOTIFY), "name": "Spotify", "applicationId": None}])
        self.assertLessEqual(devapi.activity_number(MINECRAFT), 2 ** 53 - 1)
        self.assertEqual(self.points(await self.ok("/activities/series", window())), [(ms(T0), 80)])
        rows = await self.ok("/activities/series", window(by_activity="true"))
        self.assertEqual(sorted((row["activityId"], row["activityType"], row["count"]) for row in rows),
                         sorted([(devapi.activity_number(MINECRAFT), store.PLAYING, 60), (devapi.activity_number(SPOTIFY), store.LISTENING, 20)]))
        rows = await self.ok("/activities/series", window(by_member="true"))
        self.assertEqual(sorted((row["memberId"], row["count"]) for row in rows), sorted([(str(self.member.id), 60), (str(self.admin.id), 20)]))
        self.assertEqual(self.points(await self.ok("/activities/series", window(**{"whitelist_activities[]": devapi.activity_number(MINECRAFT)}))), [(ms(T0), 60)])
        self.assertEqual(self.points(await self.ok("/activities/series", window(**{"blacklist_activities[]": devapi.activity_number(MINECRAFT)}))), [(ms(T0), 20)])
        self.assertEqual(await self.ok("/activities/series", window(**{"whitelist_activities[]": 77})), [], "an activity no one has played")
        tops = await self.ok("/activities/tops/activities", window())
        self.assertEqual(tops, [{"applicationId": "123456789012345678", "count": 60, "id": devapi.activity_number(MINECRAFT), "name": "Minecraft", "rank": 1},
                                {"applicationId": None, "count": 20, "id": devapi.activity_number(SPOTIFY), "name": "Spotify", "rank": 2}])
        self.assertEqual([row["rank"] for row in await self.ok("/activities/tops/activities", window(**{"select[]": devapi.activity_number(SPOTIFY)}))], [2])


class MembersAndStatuses(ApiTest):
    async def test_statuses(self):
        self.assertEqual(await self.ok("/statuses/series", window()), [
            {"unixTimestamp": ms(T0), "online": 12, "dnd": 1, "idle": 1}, {"unixTimestamp": ms(T0 + DAY), "online": 45, "dnd": 0, "idle": 0}])

    async def test_member_counts(self):
        self.assertEqual(await self.ok("/membercounts/series", window()), [
            {"unixTimestamp": ms(T0), "count": 101, "joins": 3, "leaves": 1}, {"unixTimestamp": ms(T0 + DAY), "count": 101, "joins": 0, "leaves": 1},
            {"unixTimestamp": ms(T0 + 2 * DAY), "count": 103, "joins": 0, "leaves": 0}])
        later = await self.ok("/membercounts/series", window(start=T0 + DAY))
        self.assertEqual(later[0]["count"], 101, "the count carries in from before the period")

    async def test_unique_members_and_channels(self):
        def counts(path, *stats):
            return self.ok(path, window(**({"stats[]": list(stats)} if stats else {})))
        self.assertEqual(self.points(await counts("/counts/members/series", "text")), [(ms(T0), 2), (ms(T0 + DAY), 2), (ms(T0 + 8 * DAY), 1)])
        self.assertEqual(self.points(await counts("/counts/members/series", "voice")), [(ms(T0), 2), (ms(T0 + DAY), 1)])
        self.assertEqual(self.points(await counts("/counts/members/series", "text", "voice")), [(ms(T0), 2), (ms(T0 + DAY), 2), (ms(T0 + 8 * DAY), 1)])
        self.assertEqual(self.points(await counts("/counts/members/series")), [(ms(T0), 2), (ms(T0 + DAY), 2), (ms(T0 + 8 * DAY), 1)])
        self.assertEqual(self.points(await counts("/counts/channels/series", "text")), [(ms(T0), 1), (ms(T0 + DAY), 1), (ms(T0 + 8 * DAY), 1)])
        self.assertEqual(self.points(await counts("/counts/channels/series", "text", "voice")), [(ms(T0), 2), (ms(T0 + DAY), 2), (ms(T0 + 8 * DAY), 1)])
        self.assertEqual((await self.call("/counts/channels/series", window(**{"stats[]": "video"})))[0], 400)

    async def test_member_counts_with_names(self):
        rows = await self.ok("/counts/members", window())
        self.assertEqual([(row["id"], row["messageCount"], row["voiceCount"], row["messageChannelCount"], row["voiceChannelCount"], row["type"]) for row in rows], [
            (str(self.admin.id), 3, 10, 1, 1, "user"), (str(self.member.id), 8, 30, 2, 1, "user"), (str(self.robot.id), 5, 5, 1, 1, "bot")])
        self.assertEqual(set(rows[0]), {"id", "messageCount", "voiceCount", "messageChannelCount", "voiceChannelCount", "username", "type", "globalName", "nick", "avatar", "guildAvatar"})


class Invites(ApiTest):
    async def test_the_invite_list_and_one_invite(self):
        rows = await self.ok("/invites")
        by_code = {row["code"]: row for row in rows}
        self.assertEqual(by_code["abc"], {"id": devapi.invite_number("abc"), "memberId": str(self.admin.id), "channelId": str(self.general.id), "code": "abc", "uses": 3,
                                          "maxUses": 10, "maxAge": 86400, "isTemporary": 0, "isVanity": 0, "isActive": 1, "label": "Website"})
        self.assertEqual((by_code["printers"]["isVanity"], by_code["printers"]["memberId"]), (1, None))
        self.assertEqual(by_code["def"]["isActive"], 0)
        self.assertEqual(await self.ok(f"/invites/{devapi.invite_number('abc')}"), by_code["abc"])
        self.assertEqual((await self.call("/invites/12345"))[0], 404)
        self.assertEqual((await self.call("/invites/abc"))[0], 404)

    async def test_joins_over_time(self):
        self.assertEqual(self.points(await self.ok("/invites/series", window())), [(ms(T0), 4)])
        self.assertEqual(self.points(await self.ok("/invites/series", window(bot="false"))), [(ms(T0), 3)])
        rows = await self.ok("/invites/series", window(by_invite="true"))
        self.assertEqual(sorted((row["inviteId"] or 0, row["count"]) for row in rows), sorted([(0, 1), (devapi.invite_number("abc"), 2), (devapi.invite_number("def"), 1)]))
        rows = await self.ok("/invites/series", window(by_inviter="true"))
        self.assertEqual(sorted((row["inviterId"] or "", row["count"]) for row in rows), sorted([("", 1), (str(self.admin.id), 2), (str(self.mod.id), 1)]))
        rows = await self.ok("/invites/series", window(by_invitee="true", by_flag="true"))
        self.assertEqual(sorted((row["inviteeId"], row["flags"]) for row in rows), sorted([
            (str(self.member.id), 0), (str(self.mod.id), 0x10), (str(self.robot.id), 0x400), (str(self.outsider), 0x200)]))
        self.assertEqual(self.points(await self.ok("/invites/series", window(whitelist_invite_flags_mask=0x10, whitelist_invite_flags_type="any"))), [(ms(T0), 1)])
        self.assertEqual(self.points(await self.ok("/invites/series", window(blacklist_invite_flags_mask=0x600, blacklist_invite_flags_type="any"))), [(ms(T0), 2)])
        self.assertEqual(self.points(await self.ok("/invites/series", window(**{"whitelist_invites[]": devapi.invite_number("abc")}))), [(ms(T0), 2)])
        self.assertEqual((await self.call("/invites/series", window(whitelist_invite_flags_mask=1)))[0], 400)

    async def test_top_invites_and_inviters(self):
        rows = await self.ok("/invites/tops/invites", window())
        self.assertEqual(rows, [{"id": devapi.invite_number("abc"), "rank": 1, "count": 2, "memberCount": 2}, {"id": devapi.invite_number("def"), "rank": 2, "count": 1, "memberCount": 1}])
        full = await self.ok("/invites/tops/invites", window(full="true"))
        self.assertEqual((full[0]["code"], full[0]["label"], full[0]["memberId"]), ("abc", "Website", str(self.admin.id)))
        rows = await self.ok("/invites/tops/members", window(full="true"))
        self.assertEqual([(row["id"], row["rank"], row["count"], row["username"]) for row in rows], [(str(self.admin.id), 1, 2, "admin"), (str(self.mod.id), 2, 1, "mod")])
        rows = await self.ok("/invites/tops/members", window(**{"whitelist_members[]": self.mod.id}))
        self.assertEqual([row["id"] for row in rows], [str(self.mod.id)], "the member lists pick inviters here")


class Channels(ApiTest):
    async def test_channels_merged_ones_included(self):
        rows = await self.ok("/channels")
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[str(self.general.id)], {"id": str(self.general.id), "name": "general", "type": 0, "parentId": str(self.category.id), "isActive": 1})
        self.assertEqual(by_id[str(self.old)]["isActive"], 0)
        self.assertEqual(by_id[str(AGGREGATE)], {"id": str(AGGREGATE), "name": "Old stuff", "type": 999, "parentId": None, "aggregate": True, "autoMergeEnabled": True,
                                                 "autoMergeChannelIds": [str(self.category.id)], "autoMergeTypes": ["text"], "createdUnixTimestamp": ms(T0)})
        self.assertEqual(sorted(row["id"] for row in await self.ok("/channels", [("types[]", "0")])), sorted([str(self.general.id), str(self.help.id), str(self.old)]))
        self.assertEqual([row["id"] for row in await self.ok("/channels", [("ids[]", str(self.help.id))])], [str(self.help.id)])
        self.assertEqual((await self.call("/channels", [("types[]", "3")]))[0], 400)
        self.assertEqual((await self.ok(f"/channels/{self.lounge.id}"))["type"], 2)
        self.assertTrue((await self.ok(f"/channels/{AGGREGATE}"))["aggregate"])
        self.assertEqual((await self.call("/channels/123"))[0], 404)


class EveryEndpoint(ApiTest):
    async def test_every_path_answers(self):
        samples = {"{channel_id}": str(self.general.id), "{invite_id}": str(devapi.invite_number("abc"))}
        for path in ["/v1/activities", *devapi.GUILD_ROUTES]:
            for name, value in samples.items():
                path = path.replace(name, value)
            params = window(**({"stats[]": "text"} if "/counts/" in path and "series" in path else {}))
            status, body, headers = await self.call(path, params)
            self.assertEqual(status, 200, (path, body))
            self.assertIn("x-ratelimit-remaining", headers)
            self.assertEqual(headers["Content-Type"].split(";")[0], "application/json")

    async def test_a_script_written_for_the_old_api_reads_everything(self):
        """The calls an import script made to the old API, against this one: monthly windows whose end is the last millisecond asked for,
        booleans as text, and top lists read a page at a time until a short page."""
        months = await self.ok("/messages/series", [("start", "0"), ("interval", "month"), ("timezone_offset", "0")])
        october = int(datetime.datetime(2026, 10, 1, tzinfo=UTC).timestamp())
        self.assertEqual(months[0]["unixTimestamp"], ms(october))
        november = int(datetime.datetime(2026, 11, 1, tzinfo=UTC).timestamp())
        hourly = [("start", str(october * 1000)), ("end", str(november * 1000 - 1)), ("interval", "hour"), ("timezone_offset", "0")]
        detail = await self.ok("/messages/series", hourly + [("by_member", "true"), ("by_channel", "true"), ("by_flag", "true")])
        total = await self.ok("/messages/series", hourly)
        self.assertEqual(sum(row["count"] for row in detail), sum(row["count"] for row in total))
        self.assertEqual(sum(row["count"] for row in total), 23)
        voice = await self.ok("/voice/series", hourly + [("by_member", "true"), ("by_channel", "true"), ("by_state", "true"), ("by_flag", "true")])
        self.assertEqual(sum(row["count"] for row in voice), 45)
        self.assertTrue(all(row.get("channelId") and "type" in row for row in voice))
        people, page = [], 1
        while True:
            rows = await self.ok("/messages/tops/members", [("start", "0"), ("page_size", "2"), ("page", str(page)), ("full", "true")])
            people += rows
            if len(rows) < 2:
                break
            page += 1
        self.assertEqual([row["rank"] for row in people], [1, 2, 3])
        self.assertTrue(all("username" in row for row in people))


if __name__ == "__main__":
    unittest.main()
