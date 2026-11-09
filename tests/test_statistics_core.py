"""Statistics: what's recorded, how it's kept, and how it reads back."""
import datetime
import time
import types
import unittest
from unittest import mock

import discord

from captainguild.core.stats import emoji as emoji_text
from captainguild.core.stats import invites as invite_rules
from captainguild.core.stats import query, store
from captainguild.core.stats import timeframe as tf
from captainguild.core.validate import Invalid, ValidationError
from captainguild.modules.statistics import Statistics, activity_identity, counted, voice_state_code
from tests.fakes import NOW, BotTest, FakeChannel, FakeMember, FakeMessage

LA = tf.zone("America/Los_Angeles")


class TimeFrame(unittest.TestCase):
    def test_a_day_chart_in_los_angeles_has_a_short_day_at_the_spring_change(self):
        start = datetime.datetime(2025, 3, 8, tzinfo=LA).timestamp()
        edges = tf.bucket_edges(start, start + 3 * 86400, "day", LA)
        self.assertEqual([edges[1] - edges[0], edges[2] - edges[1]], [86400, 23 * 3600])
        self.assertTrue(all(edge % tf.SLOT_SECONDS == 0 for edge in edges))

    def test_every_zone_starts_its_days_on_a_slot_edge(self):
        now = datetime.datetime(2026, 11, 8, 12, tzinfo=datetime.timezone.utc).timestamp()
        for name in ("Asia/Kathmandu", "Asia/Kolkata", "Australia/Eucla", "Pacific/Chatham", "America/St_Johns"):
            for edge in tf.bucket_edges(now - 10 * 86400, now, "day", tf.zone(name)):
                self.assertEqual(edge % tf.SLOT_SECONDS, 0, name)

    def test_a_lookback_in_days_starts_at_the_first_whole_bucket(self):
        now = datetime.datetime(2026, 11, 8, 16, tzinfo=LA).timestamp()
        period = tf.period_for({"mode": "days", "days": 14}, now, "day", LA)
        self.assertEqual(period.start, datetime.datetime(2026, 10, 25, tzinfo=LA).timestamp())
        self.assertEqual(period.label, "Last 14 days")
        self.assertEqual(tf.period_for({"mode": "all"}, now, "day", LA).start, None)
        since = tf.period_for({"mode": "since", "since": "2025-03-01"}, now, "day", LA)
        self.assertEqual(since.start, datetime.datetime(2025, 3, 1, tzinfo=LA).timestamp())
        self.assertEqual(since.label, "Since Mar 1, 2025")

    def test_weeks_start_on_monday_and_months_on_the_first(self):
        now = datetime.datetime(2026, 11, 8, 16, tzinfo=LA).timestamp()
        self.assertEqual(datetime.datetime.fromtimestamp(tf.floor_bucket(now, "week", LA), LA).date(), datetime.date(2026, 11, 2))
        self.assertEqual(datetime.datetime.fromtimestamp(tf.floor_bucket(now, "month", LA), LA).date(), datetime.date(2026, 11, 1))
        edges = tf.bucket_edges(datetime.datetime(2026, 1, 15, tzinfo=LA).timestamp(), now, "month", LA)
        self.assertEqual([tf.bucket_label(edge, "month", LA) for edge in edges][:3], ["Jan 2026", "Feb 2026", "Mar 2026"])

    def test_typed_periods(self):
        now = datetime.datetime(2026, 11, 8, 16, tzinfo=LA).timestamp()
        self.assertEqual(tf.parse_lookback("90", LA, now)["days"], 90)
        self.assertEqual(tf.parse_lookback("6 months", LA, now)["days"], 180)
        self.assertEqual(tf.parse_lookback("2 weeks", LA, now)["days"], 14)
        self.assertEqual(tf.parse_lookback("ALL", LA, now)["mode"], "all")
        self.assertEqual(tf.parse_lookback("2025-03-01", LA, now)["since"], "2025-03-01")
        self.assertEqual(tf.parse_lookback("<t:1735689600:D>", LA, now)["since"], "2024-12-31")
        self.assertEqual(tf.parse_lookback("1735689600000", LA, now)["since"], "2024-12-31")
        for bad in ("", "yesterday-ish", "0", "2025-02-30", "2099-01-01", "<t:9999999999:D>"):
            with self.assertRaises(Invalid, msg=bad):
                tf.parse_lookback(bad, LA, now)

    def test_a_long_period_moves_to_a_longer_interval(self):
        self.assertEqual(tf.fitting_interval(0, 30 * 86400, "hour"), "hour")
        self.assertEqual(tf.fitting_interval(0, 400 * 86400, "hour"), "day")
        self.assertEqual(tf.fitting_interval(0, 4 * 365 * 86400, "hour"), "week")

    def test_settings_checks(self):
        self.assertEqual(tf.time_zone(" America/Los_Angeles "), "America/Los_Angeles")
        with self.assertRaises(Invalid):
            tf.time_zone("Mars/Olympus_Mons")
        self.assertEqual(tf.lookback_check({"mode": "since", "since": "2025-3-1"})["since"], "2025-03-01")
        for bad in ({"mode": "days", "days": 0}, {"mode": "since"}, {"mode": "weeks"}, "14"):
            with self.assertRaises(Invalid):
                tf.lookback_check(bad)

    def test_the_zone_label(self):
        winter = datetime.datetime(2026, 1, 8, tzinfo=LA).timestamp()
        self.assertEqual(tf.zone_label(LA, winter), "UTC-08:00 (America/Los_Angeles)")
        self.assertEqual(tf.zone_label(tf.zone("Asia/Kathmandu"), winter), "UTC+05:45 (Asia/Kathmandu)")
        self.assertEqual(tf.zone_label(tf.zone("UTC"), winter), "UTC+00:00")


class Emoji(unittest.TestCase):
    def test_each_emoji_once_with_skin_tones_folded_in(self):
        thumbs = "\U0001F44D"
        self.assertEqual(emoji_text.find(f"nice {thumbs}\U0001F3FD and {thumbs}"), {thumbs})
        self.assertEqual(emoji_text.find("plain text"), set())

    def test_a_text_character_counts_only_with_the_selector(self):
        self.assertEqual(emoji_text.find("\u00a9 2026"), set())
        self.assertEqual(emoji_text.find("smile \u263a\ufe0f"), {"\u263a"})
        self.assertEqual(emoji_text.display("\u263a"), "\u263a\ufe0f")

    def test_sequences_and_flags_stay_whole(self):
        coder = "\U0001F469\u200d\U0001F4BB"
        self.assertEqual(emoji_text.find("\U0001F469\U0001F3FD\u200d\U0001F4BB"), {coder})
        self.assertEqual(emoji_text.find("\U0001F1FA\U0001F1F8\U0001F1EF\U0001F1F5"), {"\U0001F1FA\U0001F1F8", "\U0001F1EF\U0001F1F5"})
        self.assertEqual(emoji_text.bare("\U0001F44D\U0001F3FD"), "\U0001F44D")


class Sessions(unittest.TestCase):
    def setUp(self):
        self.recorder = store.Recorder(None)
        self.private = set()
        self.sessions = store.VoiceSessions(self.recorder, lambda user: 0 if user in self.private else user)

    def test_time_splits_at_quarter_hour_edges(self):
        start = 900 * 1000 + 840
        self.sessions.update(1, 7, 50, store.TALKING, 0, start)
        self.sessions.update(1, 7, None, 0, 0, start + 120)
        self.assertEqual(dict(self.recorder.pending.voice), {(1, 1000, 50, 7, store.TALKING, 0): 60, (1, 1001, 50, 7, store.TALKING, 0): 60})

    def test_a_state_change_starts_a_new_stretch(self):
        start = 900 * 2000
        self.sessions.update(1, 7, 50, store.TALKING, 0, start)
        self.sessions.update(1, 7, 50, store.TALKING, 0, start + 30)  # nothing changed
        self.sessions.update(1, 7, 50, store.SELF_MUTED, 0, start + 100)
        self.sessions.checkpoint(start + 160)
        self.assertEqual(self.recorder.pending.voice[(1, 2000, 50, 7, store.TALKING, 0)], 100)
        self.assertEqual(self.recorder.pending.voice[(1, 2000, 50, 7, store.SELF_MUTED, 0)], 60)

    def test_checkpoints_add_up_exactly(self):
        start = 900 * 3000 + 0.4
        self.sessions.update(1, 7, 50, store.TALKING, 0, start)
        for step in range(1, 11):
            self.sessions.checkpoint(start + step * 61.7)
        self.assertEqual(sum(self.recorder.pending.voice.values()), int(start + 617) - int(start))

    def test_privacy_mid_call_applies_from_then_on(self):
        start = 900 * 4000
        self.sessions.update(1, 7, 50, store.TALKING, 0, start)
        self.sessions.checkpoint(start + 60)
        self.private.add(7)
        self.sessions.checkpoint(start + 120)
        self.assertEqual(self.recorder.pending.voice[(1, 4000, 50, 7, store.TALKING, 0)], 60)
        self.assertEqual(self.recorder.pending.voice[(1, 4000, 50, 0, store.TALKING, 0)], 60)

    def test_dropping_a_server_keeps_its_time_so_far(self):
        start = 900 * 5000
        self.sessions.update(1, 7, 50, store.TALKING, 0, start)
        self.sessions.update(2, 8, 60, store.TALKING, 0, start)
        self.sessions.drop_guild(1, start + 45)
        self.assertEqual(self.recorder.pending.voice[(1, 5000, 50, 7, store.TALKING, 0)], 45)
        self.assertEqual(list(self.sessions.open), [(2, 8)])


class Invites(unittest.TestCase):
    def state(self, uses, max_uses=None, inviter=None, temporary=False):
        return invite_rules.InviteState(uses, max_uses, inviter, temporary)

    def test_the_invite_whose_count_rose(self):
        before = {"a": self.state(3), "b": self.state(1)}
        self.assertEqual(invite_rules.attribute(before, {"a": self.state(3), "b": self.state(2)}), "b")
        self.assertEqual(invite_rules.attribute(before, {"a": self.state(3), "b": self.state(1), "c": self.state(1)}), "c")

    def test_a_used_up_one_use_invite(self):
        before = {"a": self.state(3), "once": self.state(0, max_uses=1)}
        self.assertEqual(invite_rules.attribute(before, {"a": self.state(3)}), "once")

    def test_two_at_once_or_nothing_known_stays_unattributed(self):
        before = {"a": self.state(3), "b": self.state(1)}
        self.assertIsNone(invite_rules.attribute(before, {"a": self.state(4), "b": self.state(2)}))
        self.assertIsNone(invite_rules.attribute(before, dict(before)))
        self.assertIsNone(invite_rules.attribute(None, before))

    def test_reasons_follow_the_member(self):
        settings = {"invite_min_account_minutes": 60, "invite_min_join_minutes": 30, "invite_required_roles": [5], "invite_required_match": "any"}
        row = {"at": 10_000.0, "account_created": 9_000.0, "facts": store.JOIN_NO_AVATAR, "role_met_at": None, "left_at": None}
        member = types.SimpleNamespace(roles=[], pending=False, flags=types.SimpleNamespace(completed_onboarding=True, started_onboarding=True,
                                                                                            started_home_actions=False, completed_home_actions=False))
        bits = invite_rules.reasons(row, settings, 10_000.0 + 60, member)
        for key in ("no_avatar", "young_account", "new_member", "missing_role"):
            self.assertTrue(bits & invite_rules.FAKE_REASONS[key], key)
        member.roles = [types.SimpleNamespace(id=5)]
        bits = invite_rules.reasons(row, settings, 10_000.0 + 3600, member)
        self.assertFalse(bits & (invite_rules.FAKE_REASONS["new_member"] | invite_rules.FAKE_REASONS["missing_role"]))
        gone = invite_rules.reasons({**row, "left_at": 20_000.0}, settings, 30_000.0, None)
        self.assertTrue(gone & invite_rules.FAKE_REASONS["left"])

    def test_the_fake_filter(self):
        self.assertTrue(invite_rules.passes(0x4, {"fake_reasons": []}))
        self.assertFalse(invite_rules.passes(0x4, {"fake_reasons": ["young_account"], "fake_mode": "block", "fake_match": "any"}))
        self.assertTrue(invite_rules.passes(0x4, {"fake_reasons": ["young_account", "left"], "fake_mode": "block", "fake_match": "all"}))
        self.assertTrue(invite_rules.passes(0x4, {"fake_reasons": ["young_account"], "fake_mode": "allow", "fake_match": "any"}))


class Pieces(unittest.TestCase):
    def test_what_counts_as_a_message(self):
        message = types.SimpleNamespace(type=discord.MessageType.default, webhook_id=None)
        self.assertTrue(counted(message))
        self.assertFalse(counted(types.SimpleNamespace(type=discord.MessageType.default, webhook_id=5)))
        self.assertFalse(counted(types.SimpleNamespace(type=discord.MessageType.new_member, webhook_id=None)))
        self.assertFalse(counted(types.SimpleNamespace(type=discord.MessageType.pins_add, webhook_id=None)))
        self.assertTrue(counted(types.SimpleNamespace(type=discord.MessageType.chat_input_command, webhook_id=5)))

    def test_one_voice_state_by_priority(self):
        def state(**kwargs):
            values = dict(channel=types.SimpleNamespace(id=1), deaf=False, self_deaf=False, mute=False, self_mute=False)
            values.update(kwargs)
            return types.SimpleNamespace(**values)
        self.assertEqual(voice_state_code(state(), None), store.TALKING)
        self.assertEqual(voice_state_code(state(self_mute=True, deaf=True), None), store.SERVER_DEAFENED)
        self.assertEqual(voice_state_code(state(self_mute=True, self_deaf=True), None), store.SELF_DEAFENED)
        self.assertEqual(voice_state_code(state(mute=True, self_mute=True), None), store.SERVER_MUTED)
        self.assertEqual(voice_state_code(state(self_mute=True), None), store.SELF_MUTED)
        self.assertEqual(voice_state_code(state(deaf=True), 1), store.AFK)

    def test_activities(self):
        game = discord.Game("Blender")
        self.assertEqual(activity_identity(game)[:3], (store.PLAYING, None, "Blender"))
        self.assertFalse(activity_identity(game)[3])
        verified = discord.Activity(type=discord.ActivityType.playing, name="Minecraft", application_id=356875570916753438)
        self.assertTrue(activity_identity(verified)[3])
        self.assertIsNone(activity_identity(discord.CustomActivity(name="printing")))
        stream = discord.Streaming(name="Live", url="https://www.twitch.tv/someone")
        self.assertEqual(activity_identity(stream)[0], store.STREAMING)
        self.assertTrue(activity_identity(stream)[3])
        self.assertEqual(store.activity_id(0, 356875570916753438, "Minecraft"), store.activity_id(0, 356875570916753438, "Minecraft: Java Edition"))
        self.assertNotEqual(store.activity_id(0, None, "a"), store.activity_id(0, None, "b"))
        self.assertLess(store.activity_id(0, None, "a"), 2 ** 63)


class Tracking(BotTest):
    from captainguild.modules.statistics import Statistics as _module
    modules = [_module]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        self.voice = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)

    async def rows(self, sql, args=()):
        await self.stats.up_to_date()
        return [tuple(row.values()) for row in await self.bot.message_db.all(sql, args)]

    def message(self, author, channel=None, content="hello", **kwargs):
        message = FakeMessage(channel or self.channel, author, content)
        for name, value in kwargs.items():
            setattr(message, name, value)
        return message

    async def test_nothing_is_recorded_while_switched_off(self):
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": False})
        await self.stats.on_message(self.message(self.member))
        self.assertEqual(await self.rows("SELECT * FROM stat_messages"), [])

    async def test_messages_count_per_channel_person_and_slot(self):
        for _ in range(3):
            await self.stats.on_message(self.message(self.member))
        await self.stats.on_message(self.message(self.admin))
        slot = tf.slot_of(NOW.timestamp())
        rows = await self.rows("SELECT slot, channel_id, user_id, flags, count FROM stat_messages ORDER BY count")
        self.assertEqual(rows, [(slot, self.channel.id, self.admin.id, 0, 1), (slot, self.channel.id, self.member.id, 0, 3)])
        people = await self.rows("SELECT user_id, name FROM stat_people ORDER BY name")
        self.assertEqual(people, [(self.admin.id, "admin"), (self.member.id, "member")])

    async def test_threads_count_toward_their_parent_and_voice_chat_is_flagged(self):
        thread = FakeChannel(self.guild, "a thread", discord.ChannelType.public_thread)
        thread.parent_id = self.channel.id
        thread.parent = self.channel
        await self.stats.on_message(self.message(self.member, thread))
        await self.stats.on_message(self.message(self.member, self.voice))
        bot = FakeMember(self.guild, "helper", bot=True)
        await self.stats.on_message(self.message(bot))
        rows = await self.rows("SELECT channel_id, user_id, flags FROM stat_messages ORDER BY flags")
        self.assertEqual(rows, [(self.channel.id, bot.id, store.BOT), (self.channel.id, self.member.id, store.THREAD), (self.voice.id, self.member.id, store.VOICE_CHAT)])

    async def test_webhooks_and_system_messages_dont_count(self):
        await self.stats.on_message(self.message(self.member, webhook_id=123))
        await self.stats.on_message(self.message(self.member, type=discord.MessageType.pins_add))
        self.assertEqual(await self.rows("SELECT * FROM stat_messages"), [])

    async def test_an_untracked_category_and_a_merged_category(self):
        category = FakeChannel(self.guild, "Staff", discord.ChannelType.category)
        staff = FakeChannel(self.guild, "staff-chat", category=category)
        archive = FakeChannel(self.guild, "Archive", discord.ChannelType.category)
        old = FakeChannel(self.guild, "old-chat", category=archive)
        await self.bot.settings.save(self.guild.id, "statistics", {"untracked_channels": [category.id], "aggregates": [
            {"id": 77, "name": "Old channels", "category_id": archive.id, "auto_categories": [archive.id], "kinds": "both"}]})
        await self.stats.on_message(self.message(self.member, staff))
        await self.stats.on_message(self.message(self.member, old))
        self.assertEqual(await self.rows("SELECT channel_id FROM stat_messages"), [(77,)])
        self.assertIn((77, "Old channels", 999, archive.id), await self.rows("SELECT channel_id, name, type, parent_id FROM stat_channels"))

    async def test_emoji_in_text_count_once_each_and_only_this_servers(self):
        own = types.SimpleNamespace(id=111111111111111111)
        self.guild.emojis = [own]
        self.guild.stickers = [types.SimpleNamespace(id=222222222222222222)]
        text = "<:ours:111111111111111111> <:ours:111111111111111111> <:theirs:333333333333333333> \U0001F600 \U0001F600"
        message = self.message(self.member, content=text)
        message.stickers = [types.SimpleNamespace(id=222222222222222222), types.SimpleNamespace(id=444444444444444444)]
        await self.stats.on_message(message)
        rows = await self.rows("SELECT emoji, use, count FROM stat_emoji ORDER BY emoji")
        self.assertEqual(rows, [("c:111111111111111111", store.IN_TEXT, 1), ("s:222222222222222222", store.STICKER, 1), ("u:\U0001F600", store.IN_TEXT, 1)])

    async def test_reactions(self):
        self.guild.get_emoji = lambda emoji_id: types.SimpleNamespace(id=emoji_id) if emoji_id == 111111111111111111 else None
        def payload(member, emoji_id=None, name=None, author=None, burst=False):
            return types.SimpleNamespace(guild_id=self.guild.id, member=member, user_id=member.id, emoji=types.SimpleNamespace(id=emoji_id, name=name),
                                         channel_id=self.channel.id, message_author_id=author, burst=burst)
        await self.stats.on_raw_reaction_add(payload(self.member, name="\U0001F44D\U0001F3FD", author=self.admin.id))
        await self.stats.on_raw_reaction_add(payload(self.member, name="\U0001F44D", author=self.member.id, burst=True))
        await self.stats.on_raw_reaction_add(payload(self.member, emoji_id=111111111111111111, name="ours", author=self.admin.id))
        await self.stats.on_raw_reaction_add(payload(self.member, emoji_id=999999999999999999, name="theirs", author=self.admin.id))
        await self.stats.on_raw_reaction_add(payload(self.me, name="\U0001F44D", author=self.admin.id))
        rows = await self.rows("SELECT user_id, target_id, emoji, use, flags FROM stat_emoji ORDER BY emoji, use")
        self.assertEqual(rows, [
            (self.member.id, self.admin.id, "c:111111111111111111", store.REACTION, 0),
            (self.member.id, self.admin.id, "u:\U0001F44D", store.REACTION, 0),
            (self.member.id, self.member.id, "u:\U0001F44D", store.SELF_REACTION, store.SUPER),
        ])
        await self.bot.message_db.run("DELETE FROM stat_emoji")
        self.assertEqual(await query.emoji_received(self.bot.message_db, self.guild.id, query.Filters(), self.admin.id, None, None), 0)

    async def test_voice_time(self):
        start = 900 * 1_900_000
        state = types.SimpleNamespace(channel=self.voice, deaf=False, self_deaf=False, mute=False, self_mute=False)
        with mock.patch("captainguild.modules.statistics.time.time", return_value=start):
            await self.stats.on_voice_state_update(self.member, None, state)
        muted = types.SimpleNamespace(channel=self.voice, deaf=False, self_deaf=False, mute=False, self_mute=True)
        with mock.patch("captainguild.modules.statistics.time.time", return_value=start + 300):
            await self.stats.on_voice_state_update(self.member, state, muted)
        gone = types.SimpleNamespace(channel=None, deaf=False, self_deaf=False, mute=False, self_mute=False)
        with mock.patch("captainguild.modules.statistics.time.time", return_value=start + 1000):
            await self.stats.on_voice_state_update(self.member, muted, gone)
        rows = await self.rows("SELECT slot, channel_id, user_id, state, seconds FROM stat_voice ORDER BY slot, state")
        self.assertEqual(rows, [(1_900_000, self.voice.id, self.member.id, store.TALKING, 300), (1_900_000, self.voice.id, self.member.id, store.SELF_MUTED, 600),
                                (1_900_001, self.voice.id, self.member.id, store.SELF_MUTED, 100)])

    async def test_joins_leaves_and_the_invite_used(self):
        invites = [types.SimpleNamespace(code="abc", uses=4, max_uses=0, inviter=self.admin, temporary=False, channel=self.channel, max_age=0, created_at=NOW)]
        self.guild.invites = mock.AsyncMock(side_effect=lambda: list(invites))
        self.guild.features = []
        await self.stats._read_invites_into_cache(self.guild)
        invites[0] = types.SimpleNamespace(**{**vars(invites[0]), "uses": 5})
        newcomer = FakeMember(self.guild, "newcomer")
        newcomer.avatar = None
        await self.stats.on_member_join(newcomer)
        await asyncio_settle(self.stats)
        rows = await self.rows("SELECT user_id, code, inviter_id, facts FROM stat_joins")
        self.assertEqual(rows, [(newcomer.id, "abc", self.admin.id, store.JOIN_NO_AVATAR)])
        await self.stats.on_raw_member_remove(types.SimpleNamespace(guild_id=self.guild.id, user=newcomer))
        joins = await self.rows("SELECT SUM(joins), SUM(leaves) FROM stat_members")
        self.assertEqual(joins, [(1, 1)])
        self.assertIsNotNone((await self.rows("SELECT left_at FROM stat_joins"))[0][0])

    async def test_privacy_counts_as_nobody_and_moves_what_came_before(self):
        await self.stats.on_message(self.message(self.member))
        await self.stats.on_message(self.message(self.admin))
        await self.stats.up_to_date()
        self.stats.private.add(self.member.id)
        await store.anonymize(self.bot.message_db, self.member.id)
        await self.stats.on_message(self.message(self.member))
        rows = await self.rows("SELECT user_id, count FROM stat_messages ORDER BY user_id")
        self.assertEqual(rows, [(0, 2), (self.admin.id, 1)])
        self.assertEqual(await self.rows("SELECT user_id FROM stat_people WHERE user_id = ?", (self.member.id,)), [])

    async def test_the_minute_reading(self):
        self.bot.members_settled.set()
        self.bot.fetch_guild = mock.AsyncMock(return_value=types.SimpleNamespace(approximate_presence_count=3))
        now = 900 * 1_950_000 + 30
        await self.stats.tick(now)
        await asyncio_settle(self.stats)
        members = await self.rows("SELECT slot, members, humans, bots FROM stat_members")
        self.assertEqual(members, [(1_950_000, len(self.guild.members), len(self.guild.members) - 1, 1)])
        self.assertEqual(await self.rows("SELECT present_samples, present FROM stat_status"), [(1, 3)])
        await self.stats.tick(now + 60)
        await asyncio_settle(self.stats)
        self.assertEqual(await self.rows("SELECT present_samples FROM stat_status"), [(1,)], "Discord's count is read every five minutes")

    async def test_statuses_with_the_presence_intent(self):
        self.bot.members_settled.set()
        self.bot._connection._intents.presences = True
        try:
            for member, status in ((self.member, "online"), (self.admin, "idle"), (self.mod, "dnd"), (self.owner, "offline"), (self.me, "online")):
                member.status = status
                member.activities = []
            await self.stats.tick(900 * 1_960_000)
        finally:
            self.bot._connection._intents.presences = False
        self.assertEqual(await self.rows("SELECT samples, online, idle, dnd, bot_online FROM stat_status"), [(1, 1, 1, 1, 1)])

    async def test_settings_refuse_what_cant_work(self):
        for bad in ({"timezone": "Nowhere/Here"}, {"lookback": {"mode": "days", "days": 0}}, {"aggregates": [{"id": 2 ** 40, "name": "x"}]},
                    {"invites": ["bad code!"]}, {"emoji": ["not an emoji"]}):
            with self.assertRaises(ValidationError, msg=str(bad)):
                await self.bot.settings.save(self.guild.id, "statistics", bad)
        saved = await self.bot.settings.save(self.guild.id, "statistics", {"emoji": ["u:\U0001F44D\U0001F3FD", "c:111111111111111111"]})
        self.assertEqual(saved["emoji"], ["u:\U0001F44D", "c:111111111111111111"])

    async def test_a_failed_write_keeps_the_counts(self):
        await self.stats.on_message(self.message(self.member))
        with mock.patch.object(self.bot.message_db, "many", mock.AsyncMock(side_effect=RuntimeError("disk"))):
            await self.stats.write()
        await self.stats.on_message(self.message(self.member))
        self.assertEqual(await self.rows("SELECT count FROM stat_messages"), [(2,)])


async def asyncio_settle(stats):
    for task in list(stats._tasks):
        await task


class Queries(BotTest):
    from captainguild.modules.statistics import Statistics as _module
    modules = [_module]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats_db = self.bot.message_db
        rows = [  # slot, channel, user, flags, count
            (100, 10, 1, 0, 5), (100, 10, 2, 0, 3), (101, 11, 1, 0, 2), (104, 11, 3, store.BOT, 9), (108, 12, 2, 0, 4), (108, 10, 0, 0, 7),
        ]
        await self.stats_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (1, ?, ?, ?, ?, ?)", rows)
        await self.stats_db.many("INSERT INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (1, ?, ?, ?, ?, 0, ?)",
                           [(100, 20, 1, store.TALKING, 600), (100, 20, 2, store.AFK, 900), (105, 21, 1, store.SELF_MUTED, 300)])
        await self.stats_db.many("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id) VALUES (1, ?, ?, 0, ?)", [(10, "general", 90), (11, "help", 90), (12, "off", None)])

    async def test_totals_and_filters(self):
        f = query.Filters()
        self.assertEqual(await query.total(self.stats_db, "messages", 1, f, None, None), 21)
        self.assertEqual(await query.total(self.stats_db, "messages", 1, query.Filters(include="both"), None, None), 30)
        self.assertEqual(await query.total(self.stats_db, "messages", 1, query.Filters(include="bots"), None, None), 9)
        self.assertEqual(await query.total(self.stats_db, "messages", 1, f, 101, 109), 13)
        self.assertEqual(await query.total(self.stats_db, "messages", 1, query.Filters(channel_mode="block", channels=frozenset({90})), None, None), 4)
        self.assertEqual(await query.total(self.stats_db, "messages", 1, query.Filters(channel_mode="allow", channels=frozenset({12})), None, None), 4)
        self.assertEqual(await query.total(self.stats_db, "messages", 1, query.Filters(members_out=frozenset({1})), None, None), 14)
        many = frozenset(range(1000, 1600)) | {2}
        self.assertEqual(await query.total(self.stats_db, "messages", 1, query.Filters(members_in=many), None, None), 7)
        self.assertEqual(await query.total(self.stats_db, "voice", 1, query.Filters(voice_states=frozenset({store.TALKING, store.SELF_MUTED})), None, None), 900)

    async def test_series_fold_slots_into_buckets(self):
        self.assertEqual(await query.series(self.stats_db, "messages", 1, query.Filters(), [100, 104, 108], 112), [10, 0, 11])
        self.assertEqual(await query.unique_series(self.stats_db, ["messages"], "user_id", 1, query.Filters(), [100, 104, 108], 112), [2, 0, 1])
        self.assertEqual(await query.unique_count(self.stats_db, ["messages", "voice"], "user_id", 1, query.Filters(), None, None), 2)
        self.assertEqual(await query.unique_count(self.stats_db, ["messages"], "channel_id", 1, query.Filters(), None, None), 3)

    async def test_rankings(self):
        top = await query.top_members(self.stats_db, "messages", 1, query.Filters(), None, None)
        self.assertEqual(top.rows, [(1, 7, 2), (2, 7, 2)])
        self.assertEqual(top.total, 2)
        self.assertEqual(await query.rank_of(self.stats_db, "messages", 1, query.Filters(), None, None, 2), (2, 7, 2))
        self.assertEqual(await query.rank_of(self.stats_db, "messages", 1, query.Filters(), None, None, 9), (None, 0, 2))
        channels = await query.top_channels(self.stats_db, "messages", 1, query.Filters(), None, None)
        self.assertEqual(channels.rows, [(10, 15, 2), (12, 4, 1), (11, 2, 1)])
        self.assertEqual(await query.favorite(self.stats_db, "voice", 1, query.Filters(), None, None, "channel_id"), (20, 1500))

    async def test_member_counts_carry_forward(self):
        await self.stats_db.many("INSERT INTO stat_members (guild_id, slot, members, humans, bots, joins, leaves) VALUES (1, ?, ?, ?, ?, ?, ?)",
                           [(96, 50, 48, 2, 0, 0), (101, 52, 50, 2, 2, 0), (110, None, None, None, 0, 1)])
        buckets = await query.member_counts(self.stats_db, 1, [100, 104, 108], 112)
        self.assertEqual([bucket["members"] for bucket in buckets], [52, 52, 52])
        self.assertEqual([(bucket["joins"], bucket["leaves"]) for bucket in buckets], [(2, 0), (0, 0), (0, 1)])
        self.assertEqual(await query.joins_and_leaves(self.stats_db, 1, None, None), (2, 1))

    async def test_status_averages(self):
        await self.stats_db.many("INSERT INTO stat_status (guild_id, slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present) "
                           "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [(100, 2, 20, 4, 2, 2, 0, 0, 0, 0), (101, 1, 12, 2, 0, 1, 0, 0, 0, 0), (104, 0, 0, 0, 0, 0, 0, 0, 3, 90)])
        series = await query.status_series(self.stats_db, 1, "humans", [100, 104], 108)
        self.assertEqual(series[0]["online"], 11)  # 20 over 2 readings, then 12 over 1
        self.assertEqual(series[0]["dnd"], 0.5)
        self.assertEqual(series[0]["present"], 11 + 2 + 0.5)
        self.assertEqual(series[1], {"online": None, "idle": None, "dnd": None, "present": 30})

    async def test_heatmap_averages_each_hour_of_the_week(self):
        start = tf.slot_of(datetime.datetime(2026, 11, 2, tzinfo=datetime.timezone.utc).timestamp())  # a Monday
        await self.stats_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (2, ?, 1, 1, 0, 14)", (start + 4 * 9,))
        cells = await query.heatmap(self.stats_db, "messages", 2, query.Filters(), start, start + 4 * 24 * 14, tf.zone("UTC"))
        self.assertEqual(cells[0][9], 7.0)
        self.assertEqual(sum(sum(row) for row in cells), 7.0)


if __name__ == "__main__":
    unittest.main()
