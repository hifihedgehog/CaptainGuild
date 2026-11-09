"""Statistics cards: the words in six languages, the reads behind the cards, what each card shows and to whom, and the pictures."""
import datetime
import io
import types
import unittest

import discord
from PIL import Image

from captainguild.core.stats import cards, draw, query, reads, store, views, words
from captainguild.core.stats import timeframe as tf
from captainguild.modules.statistics import Statistics
from tests.fakes import NOW, BotTest, FakeChannel, FakeMember, FakePermissions

UTC = tf.zone("UTC")


class Words(unittest.TestCase):
    def test_every_string_has_six_languages_and_the_same_placeholders(self):
        for key, row in words.TEXT.items():
            self.assertEqual(len(row), 6, key)
            english = set(_placeholders(row[0]))
            for index, text in enumerate(row):
                if text is None:
                    self.assertEqual(index, 1, f"{key}: only the UK column may borrow the US text")
                    continue
                self.assertEqual(set(_placeholders(text)), english, f"{key} in {words.LANGUAGES[index]}")
                self.assertNotIn(chr(0x2014), text, key)  # the house style keeps the long dash out of UI text

    def test_numbers_the_way_each_language_writes_them(self):
        self.assertEqual(words.number("en-US", 1234567), "1,234,567")
        self.assertEqual(words.number("fr", 1234567), "1 234 567")
        self.assertEqual(words.number("de", 1234567.5, 1), "1.234.567,5")
        self.assertEqual(words.number("es-ES", 1234), "1234")
        self.assertEqual(words.number("es-ES", 12345), "12.345")
        self.assertEqual(words.compact("en-US", 2370), "2.37K")
        self.assertEqual(words.compact("en-US", 125_000), "125K")
        self.assertEqual(words.compact("en-US", 999_999), "1M")
        self.assertEqual(words.compact("fr", 2370), "2,37 k")
        self.assertEqual(words.compact("de", 125_000), "125.000")
        self.assertEqual(words.compact("de", 2_500_000), "2,5 Mio.")
        self.assertEqual(words.compact("nl", 2_500_000), "2,5 mln.")
        self.assertEqual(words.percent("de", 0.125), "13 %")
        self.assertEqual(words.percent("en-US", 0.05), "5%")

    def test_plurals(self):
        self.assertEqual(words.amount("en-US", 1, "message"), "1 message")
        self.assertEqual(words.amount("en-US", 2, "message"), "2 messages")
        self.assertEqual(words.amount("fr", 0, "message"), "0 message")
        self.assertEqual(words.amount("fr", 1.5, "member").split(" ")[-1], "membre")
        self.assertEqual(words.amount("de", 1, "message"), "1 Nachricht")
        self.assertEqual(words.voice_amount("en-US", 3600, "hours"), "1 hour")
        self.assertEqual(words.voice_amount("en-US", 5400, "hours"), "1.5 hours")
        self.assertEqual(words.voice_amount("en-US", 90, "minutes"), "2 minutes")

    def test_dates_and_buckets(self):
        moment = datetime.datetime(2026, 11, 8, 15, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(words.date("en-US", moment, UTC), "Nov 8, 2026")
        self.assertEqual(words.date("en-GB", moment, UTC), "8 Nov 2026")
        self.assertEqual(words.date("fr", moment, UTC), "8 nov. 2026")
        self.assertEqual(words.date("de", moment, UTC), "08.11.2026")
        self.assertEqual(words.bucket("en-US", moment, "hour", UTC), "Nov 8, 3 PM")
        self.assertEqual(words.bucket("en-GB", moment, "hour", UTC), "8 Nov, 15:00")
        self.assertEqual(words.bucket("de", moment, "month", UTC), "Nov. 2026")
        self.assertEqual(words.period_label("en-US", {"mode": "days", "days": 30}), "Last 30 days")
        self.assertEqual(words.period_label("es-ES", {"mode": "all"}), "Desde el principio")
        self.assertEqual(words.period_label("en-GB", {"mode": "since", "since": "2025-03-01"}), "Since 1 Mar 2025")

    def test_picking_a_language(self):
        self.assertEqual(words.pick(None, "fr", "en-US"), "fr")
        self.assertEqual(words.pick("de", "fr"), "de")
        self.assertEqual(words.pick(None, "es-419"), "es-ES")
        self.assertEqual(words.pick(None, "ja", None), "en-US")
        self.assertEqual(words.pick(None, discord.Locale.british_english), "en-GB")


def _placeholders(text: str) -> list[str]:
    import string
    return [name for _, name, _, _ in string.Formatter().parse(text) if name]


class Drawing(unittest.TestCase):
    def test_round_ticks(self):
        self.assertEqual(draw.nice_ticks(9_400), [0, 2500, 5000, 7500, 10000])
        self.assertEqual(draw.nice_ticks(3), [0, 1, 2, 3])
        self.assertEqual(draw.nice_step(80, 4), 20)

    def test_labels_keep_the_last_bucket(self):
        chosen = draw.label_indexes(30, 1000, 80)
        self.assertEqual(chosen[-1], 29)
        self.assertLessEqual(len(chosen), 10)

    def test_names_lose_only_the_characters_the_font_lacks(self):
        self.assertEqual(draw.drawable("\U0001F3EC│printer-chat"), "│printer-chat")
        self.assertEqual(draw.drawable("\U0001F9EA", "#1234"), "#1234")


class Reads(BotTest):
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.counts = self.bot.message_db
        rows = [(100, 10, 1, 0, 5), (100, 10, 2, 0, 3), (101, 11, 1, 0, 2), (104, 11, 3, store.BOT, 9), (108, 12, 2, 0, 4), (108, 10, 0, 0, 7)]
        await self.counts.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (1, ?, ?, ?, ?, ?)", rows)
        await self.counts.many("INSERT INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (1, ?, ?, ?, ?, 0, ?)",
                           [(100, 20, 1, store.TALKING, 600), (100, 20, 2, store.AFK, 900), (105, 21, 1, store.SELF_MUTED, 300)])
        await self.counts.many("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id) VALUES (1, ?, ?, 0, ?)", [(10, "general", 90), (11, "help", 90), (12, "off", None)])

    async def test_the_covering_indexes_exist_and_rankings_agree_with_the_core(self):
        names = {row["name"] for row in await self.counts.all("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertTrue({"stat_messages_user", "stat_messages_channel", "stat_voice_user", "stat_emoji_by_use"} <= names)
        f = query.Filters()
        for start, end in ((None, 200), (100, 109), (100, 100 + reads.LONG_SLOTS + 5)):
            people = await reads.by_user(self.counts, "messages", 1, f, start, end)
            core = await query.top_members(self.counts, "messages", 1, f, start, end)
            self.assertEqual([(user, value) for user, value, _ in core.rows], reads.ranked(people))
            places = await reads.by_channel(self.counts, "messages", 1, f, start, end)
            self.assertEqual(sorted(places.items()), sorted((channel, value) for channel, value, _ in (await query.top_channels(self.counts, "messages", 1, f, start, end)).rows))
        self.assertEqual(sum((await reads.by_user(self.counts, "messages", 1, f, None, 200)).values()), await query.total(self.counts, "messages", 1, f, None, 200))

    async def test_one_person_and_some_channels(self):
        f = query.Filters()
        self.assertEqual(await reads.by_channel(self.counts, "messages", 1, f.only(1), None, 200, user_id=1), {10: 5, 11: 2})
        self.assertEqual(await reads.by_user(self.counts, "messages", 1, f, None, 200, channels={10}), {1: 5, 2: 3, 0: 7})
        self.assertEqual(await reads.windows(self.counts, "messages", 1, f, [104, 100, None], 200), [(11, 1), (21, 2), (21, 2)])
        self.assertEqual(await reads.windows(self.counts, "messages", 1, f.only(2), [100, 108], 200, user_id=2, people=False), [(7, 0), (4, 0)])

    async def test_buckets_match_the_core_series(self):
        f = query.Filters()
        edges = [100, 104, 108]
        found = await reads.buckets(self.counts, "messages", 1, f, edges, 112, people=True)
        self.assertEqual([total for total, _ in found], await query.series(self.counts, "messages", 1, f, edges, 112))
        self.assertEqual([people for _, people in found], await query.unique_series(self.counts, ["messages"], "user_id", 1, f, edges, 112))
        mine = await reads.buckets(self.counts, "messages", 1, f.only(1), edges, 112, user_id=1)
        self.assertEqual([total for total, _ in mine], [7, 0, 0])

    async def test_a_long_people_filter_goes_as_json(self):
        many = frozenset(range(5_000, 5_000 + reads.INLINE_PEOPLE + 10)) | {2}
        f = query.Filters(members_in=many)
        self.assertEqual(await reads.by_user(self.counts, "messages", 1, f, None, 200), {2: 7})
        f = query.Filters(members_out=frozenset(range(5_000, 5_300)) | {1})
        self.assertNotIn(1, await reads.by_user(self.counts, "messages", 1, f, None, 200))

    async def test_member_counts_and_statuses_match_the_core(self):
        await self.counts.many("INSERT INTO stat_members (guild_id, slot, members, humans, bots, joins, leaves) VALUES (1, ?, ?, ?, ?, ?, ?)",
                           [(96, 50, 48, 2, 0, 0), (101, 52, 50, 2, 2, 0), (110, None, None, None, 0, 1)])
        core = await query.member_counts(self.counts, 1, [100, 104, 108], 112)
        mine = await reads.member_buckets(self.counts, 1, [100, 104, 108], 112)
        self.assertEqual([row["members"] for row in mine], [row["members"] for row in core])
        self.assertEqual([(row["joins"], row["leaves"]) for row in mine], [(row["joins"], row["leaves"]) for row in core])
        await self.counts.many("INSERT INTO stat_status (guild_id, slot, samples, online, idle, dnd, bot_online, bot_idle, bot_dnd, present_samples, present) "
                           "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [(100, 2, 20, 4, 2, 2, 0, 0, 0, 0), (101, 1, 12, 2, 0, 1, 0, 0, 0, 0), (104, 0, 0, 0, 0, 0, 0, 0, 3, 90)])
        for include in ("humans", "both"):
            self.assertEqual(await reads.status_buckets(self.counts, 1, include, [100, 104], 108), await query.status_series(self.counts, 1, include, [100, 104], 108))


class CardData(BotTest):
    """The numbers each card gets, from a small server with a known history."""
    modules = [Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True, "lookback": {"mode": "days", "days": 14}})
        self.counts = self.bot.message_db
        everyone = self.guild.default_role
        everyone.permissions = FakePermissions("view_channel", "read_message_history", "send_messages")
        self.secret = FakeChannel(self.guild, "staff-room")
        self.secret.permissions_for = lambda member: FakePermissions("administrator") if member.guild_permissions.administrator else FakePermissions()
        self.secret.overwrites = {everyone: discord.PermissionOverwrite(view_channel=False)}
        self.now = NOW.timestamp()
        slot = tf.slot_of(self.now)
        rows = []
        for back in range(0, 20 * 96, 7):
            rows.append((self.guild.id, slot - back, self.channel.id, self.member.id, 0, 3))
            rows.append((self.guild.id, slot - back, self.secret.id, self.admin.id, 0, 5))
        await self.counts.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)", rows)
        await self.counts.many("INSERT INTO stat_channels (guild_id, channel_id, name, type, parent_id) VALUES (?, ?, ?, 0, NULL)",
                           [(self.guild.id, self.channel.id, "general"), (self.guild.id, self.secret.id, "staff-room")])

    def scope(self, *, viewer=None, channel=None, public=True, code="s", lang="en-US"):
        cfg = self.stats.cfg(self.guild.id)
        visibility = views.Visibility(self.guild, viewer or self.member, channel or self.channel, public)
        return views.Scope(db=self.counts, guild=self.guild, cfg=cfg, lang=lang, now=self.now, lookback=views.lookback_of(code, cfg), filters=views.filters_for(cfg, self.guild),
                           visibility=visibility)

    async def test_a_public_reply_hides_a_channel_its_readers_cant_all_see(self):
        data = await views.gather(self.scope(), "messages")
        names = {row["id"]: row for row in data["top_channels"]}
        self.assertEqual(names[self.secret.id]["state"], "hidden")
        self.assertIsNone(names[self.secret.id]["name"])
        self.assertEqual(names[self.channel.id]["name"], "general")
        private = await views.gather(self.scope(viewer=self.admin, public=False), "messages")
        self.assertEqual({row["id"]: row["name"] for row in private["top_channels"]}[self.secret.id], "staff-room")
        inside = await views.gather(self.scope(channel=self.secret), "messages")
        self.assertEqual({row["id"]: row["name"] for row in inside["top_channels"]}[self.secret.id], "staff-room", "everyone who reads the staff room can see it")

    async def test_the_period_row_is_left_out_when_it_matches_a_fixed_one(self):
        data = await views.gather(self.scope(code="d7"), "server")
        self.assertEqual([row["days"] for row in data["rows"]], [1, 7])
        data = await views.gather(self.scope(code="d14"), "server")
        self.assertEqual([row["days"] for row in data["rows"]], [1, 7, None])
        self.assertEqual(data["rows"][-1]["messages"], sum(3 + 5 for back in range(0, 14 * 96 + 1, 7) if tf.slot_of(self.now) - back >= tf.slot_of(self.now - 14 * 86400)))

    async def test_member_card_ranks_and_periods(self):
        data = await views.gather(self.scope(), "member", self.member)
        self.assertEqual(data["ranks"]["messages"], (2, 2))
        self.assertEqual(data["rows"][0]["days"], 1)
        self.assertEqual(data["text_channels"][0]["id"], self.channel.id)
        everything = await views.gather(self.scope(code="a"), "member", self.member)
        self.assertEqual(everything["rows"][-1]["messages"], 3 * len(range(0, 20 * 96, 7)))

    async def test_filters_from_settings_turn_roles_into_members(self):
        await self.bot.settings.save(self.guild.id, "statistics", {"roles_allow": [self.role_member.id]})
        f = views.filters_for(self.stats.cfg(self.guild.id), self.guild)
        self.assertEqual(f.members_in, frozenset({self.member.id}))
        await self.bot.settings.save(self.guild.id, "statistics", {"roles_allow": [], "roles_block": [self.role_admin.id], "members": [self.mod.id], "member_mode": "block"})
        f = views.filters_for(self.stats.cfg(self.guild.id), self.guild)
        self.assertIsNone(f.members_in)
        self.assertEqual(f.members_out, frozenset({self.admin.id, self.owner.id, self.mod.id}))

    async def test_period_codes_round_trip(self):
        cfg = self.stats.cfg(self.guild.id)
        for lookback in ({"mode": "days", "days": 90, "since": None}, {"mode": "all", "days": 14, "since": None}, {"mode": "since", "days": 14, "since": "2025-03-01"}):
            self.assertEqual(views.lookback_of(views.code_of(lookback), cfg)["mode"], lookback["mode"])
        self.assertEqual(views.lookback_of("s", cfg), cfg["lookback"])
        self.assertEqual(views.lookback_of("y20250230", cfg), cfg["lookback"], "a day that doesn't exist reads as the server's setting")

    async def test_every_view_draws_a_card_in_every_language(self):
        target = {"member": self.member, "chart_member": self.member, "channel": self.channel, "chart_channel": self.channel}
        for view in views.VIEWS:
            for lang in words.LANGUAGES:
                with self.subTest(view=view, lang=lang):
                    data = await views.gather(self.scope(lang=lang), view, target.get(view), page_kind="members_messages" if view == "top" else "overview")
                    png = cards.render(data, lang, {})
                    with Image.open(io.BytesIO(png)) as image:
                        self.assertEqual(image.width, draw.WIDTH)
                        self.assertGreater(image.height, 400)
                        self.assertLess(image.height, 1400)

    async def test_cards_draw_with_nothing_recorded(self):
        await self.counts.run("DELETE FROM stat_messages")
        for view in views.VIEWS:
            with self.subTest(view=view):
                target = self.member if view in ("member", "chart_member") else self.channel if view in ("channel", "chart_channel") else None
                data = await views.gather(self.scope(code="a"), view, target)
                self.assertTrue(cards.render(data, "en-US", {}).startswith(b"\x89PNG"))

    async def test_pictures_go_on_the_card_and_a_broken_one_is_left_out(self):
        data = await views.gather(self.scope(), "server")
        avatar = io.BytesIO()
        Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(avatar, format="PNG")
        with_icon = cards.render(data, "en-US", {"icon": avatar.getvalue()})
        broken = cards.render(data, "en-US", {"icon": b"not a picture"})
        without = cards.render(data, "en-US", {})
        self.assertNotEqual(with_icon, without)
        self.assertEqual(broken, without)
        with Image.open(io.BytesIO(with_icon)) as image:
            self.assertEqual(image.convert("RGB").getpixel((80, 76))[:3], (255, 0, 0))

    async def test_names_people_left_with(self):
        gone = types.SimpleNamespace(id=424242, display_name=None, name=None, joined_at=None, created_at=None, bot=False)
        await self.counts.run("INSERT INTO stat_people (guild_id, user_id, name, display_name, nick, bot, seen_at) VALUES (?, ?, 'oldname', 'Old Name', NULL, 0, 0)",
                          (self.guild.id, gone.id))
        named = await views.people(self.scope(), [gone.id, self.member.id, 777])
        self.assertEqual(named[gone.id], {"id": gone.id, "name": "Old Name", "username": "oldname", "global_name": "Old Name", "state": "former"})
        self.assertEqual(named[self.member.id]["state"], "member")
        self.assertEqual(named[777]["state"], "unknown")
        data = await views.gather(self.scope(), "member", gone)
        self.assertEqual(data["title"], "Old Name")
        self.assertFalse(data["in_server"])

    async def test_the_messages_card_shows_voice_beside_it_and_the_voice_card_messages(self):
        slot = tf.slot_of(self.now)
        await self.counts.many("INSERT INTO stat_voice (guild_id, slot, channel_id, user_id, state, flags, seconds) VALUES (?, ?, ?, ?, 0, 0, ?)",
                               [(self.guild.id, slot - 2, self.channel.id, self.member.id, 1800), (self.guild.id, slot - 3 * 96, self.channel.id, self.admin.id, 3600),
                                (self.guild.id, slot - 20 * 96, self.channel.id, self.admin.id, 7200)])
        messages = await views.gather(self.scope(), "messages")
        self.assertEqual(messages["other_kind"], "voice")
        self.assertEqual([row["days"] for row in messages["other_rows"]], [1, 7, 30])
        self.assertEqual([row["total"] for row in messages["other_rows"]], [1800, 5400, 12600])
        self.assertNotIn("people", messages["rows"][0], "contributors stay on the server and channel cards")
        voice = await views.gather(self.scope(), "voice")
        self.assertEqual(voice["other_kind"], "messages")
        day = await self.counts.scalar("SELECT SUM(count) FROM stat_messages WHERE guild_id = ? AND slot >= ?", (self.guild.id, self.scope().window(1)))
        self.assertEqual(voice["other_rows"][0]["total"], day)
        for data in (messages, voice):
            self.assertTrue(cards.render(data, "de", {}).startswith(b"\x89PNG"))

    async def test_names_the_font_cant_draw_fall_back_to_ones_it_can(self):
        self.member.nick = "\u5f35\u5049"  # Chinese, which the card's font doesn't have
        self.member.global_name = "\u7530\u4e2d"
        named = await views.people(self.scope(), [self.member.id])
        entry = named[self.member.id]
        self.assertEqual((entry["name"], entry["username"]), ("\u5f35\u5049", "member"))
        self.assertEqual(cards._person_label("en-US", entry), ("member", True), "the username, since every one of its letters draws")
        self.assertEqual(cards._person_label("en-US", {**entry, "name": "\U0001F3AE\U0001F3AE", "username": "\u30ed\u30dc", "global_name": "Gamer"}), ("Gamer", True))
        self.assertEqual(cards._person_label("en-US", {**entry, "name": "\u30ed\u30dc 3D", "username": "printer_fan"}), ("printer_fan", True),
                         "a name that loses its letters isn't shortened to what's left")
        self.assertEqual(cards._person_label("en-US", {"id": 123456789012345678, "name": "\u5f35\u5049", "username": "\u30ed\u30dc", "global_name": None, "state": "member"}),
                         (words.t("en-US", "user_number", number="5678"), False))
        self.assertEqual(cards._person_label("en-US", {"id": 4321, "name": None, "username": None, "global_name": None, "state": "unknown"}),
                         (words.t("en-US", "user_number", number="4321"), False), "someone with an ID is never an unknown member")
        card = await views.gather(self.scope(), "member", self.member)
        self.assertEqual(cards.person_name("en-US", card), "member")

    def test_the_emoji_font_comes_from_fixed_places(self):
        import importlib
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"STATS_EMOJI_FONT": str(draw.REGULAR)}):
            fresh = importlib.reload(draw)
            try:
                self.assertNotIn(str(draw.REGULAR), fresh.EMOJI_FONTS)
                self.assertIn("/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf", fresh.EMOJI_FONTS)
                self.assertIn("C:/Windows/Fonts/seguiemj.ttf", fresh.EMOJI_FONTS)
            finally:
                fresh._emoji_font_path.cache_clear()
        importlib.reload(draw)


class Audience(BotTest):
    modules = [Statistics]

    async def test_roles_and_overwrites_decide_who_reads_what(self):
        everyone = self.guild.default_role
        everyone.permissions = FakePermissions("view_channel")
        hidden = FakeChannel(self.guild, "mods")
        allowed = {self.role_mod.id, self.role_admin.id}
        hidden.permissions_for = lambda member: FakePermissions("view_channel") if {role.id for role in member.roles} & allowed or member.guild_permissions.administrator else FakePermissions()
        hidden.overwrites = {everyone: discord.PermissionOverwrite(view_channel=False)}
        mods = FakeChannel(self.guild, "mod-chat")
        mods.permissions_for = hidden.permissions_for
        mods.overwrites = dict(hidden.overwrites)
        for number in range(50):
            FakeMember(self.guild, f"extra{number}", self.role_member)
        self.assertFalse(views.all_see(self.guild, self.channel, hidden))
        self.assertTrue(views.all_see(self.guild, mods, hidden), "everyone in the mod chat can see the mods channel")
        self.assertTrue(views.all_see(self.guild, hidden, self.channel))
        self.assertTrue(views.open_to_all(self.guild, self.channel))
        self.assertFalse(views.open_to_all(self.guild, hidden))

    async def test_a_member_with_an_overwrite_of_their_own_answers_for_themselves(self):
        everyone = self.guild.default_role
        everyone.permissions = FakePermissions("view_channel")
        kept_out = self.member
        quiet = FakeChannel(self.guild, "quiet")
        quiet.permissions_for = lambda member: FakePermissions() if member is kept_out else FakePermissions("view_channel")
        quiet.overwrites = {kept_out: discord.PermissionOverwrite(view_channel=False)}
        self.assertFalse(views.open_to_all(self.guild, quiet))
        self.assertFalse(views.all_see(self.guild, self.channel, quiet), "the one member kept out reads the general channel")
        vip = FakeChannel(self.guild, "vip")
        vip.permissions_for = lambda member: FakePermissions("view_channel") if member is self.mod or member.guild_permissions.administrator else FakePermissions()
        vip.overwrites = {everyone: discord.PermissionOverwrite(view_channel=False), self.mod: discord.PermissionOverwrite(view_channel=True)}
        self.assertFalse(views.all_see(self.guild, self.channel, vip), "one member's own way in doesn't open it to everyone")
        self.assertTrue(views.all_see(self.guild, vip, self.channel), "everyone who reads the vip room can see general")

    async def test_a_mix_of_roles_that_loses_sight_of_the_channel_hides_its_name(self):
        everyone = self.guild.default_role
        everyone.permissions = FakePermissions("view_channel")
        agreed = self.guild.role("Agreed", 3)
        general = FakeChannel(self.guild, "chat")
        general.overwrites = {everyone: discord.PermissionOverwrite(view_channel=False), agreed: discord.PermissionOverwrite(view_channel=True)}
        news = FakeChannel(self.guild, "news")
        help_room = FakeChannel(self.guild, "help")
        help_room.overwrites = dict(general.overwrites)
        self.assertTrue(views.all_see(self.guild, general, news))
        self.assertTrue(views.all_see(self.guild, general, help_room))
        no_news = self.guild.role("No news", 1)
        news.overwrites = {no_news: discord.PermissionOverwrite(view_channel=False)}
        self.assertFalse(views.all_see(self.guild, general, news), "someone holding Agreed and No news reads the chat and can't see news")
        help_room.overwrites[no_news] = discord.PermissionOverwrite(view_channel=False)
        self.assertTrue(views.all_see(self.guild, general, help_room), "Agreed lets them into help, whatever No news says")
        staff = self.guild.role("Staff", 4)
        general.overwrites[staff] = discord.PermissionOverwrite(view_channel=True)
        self.assertFalse(views.all_see(self.guild, general, help_room), "Staff alone opens the chat and not help")
        help_room.overwrites[staff] = discord.PermissionOverwrite(view_channel=True)
        other_bot = self.guild.role("Another bot", 7, managed=True)
        general.overwrites[other_bot] = discord.PermissionOverwrite(view_channel=True)
        self.assertTrue(views.all_see(self.guild, general, help_room), "a bot's own role says nothing about who reads the chat")
        bosses = self.guild.role("Bosses", 9, "administrator")
        general.overwrites[bosses] = discord.PermissionOverwrite(view_channel=True)
        self.assertTrue(views.all_see(self.guild, general, help_room), "an Administrator sees every channel")

    async def test_a_server_where_only_a_role_sees_channels(self):
        everyone = self.guild.default_role
        everyone.permissions = FakePermissions()
        verified = self.guild.role("Verified", 3, "view_channel")
        rules = FakeChannel(self.guild, "rules")
        rules.overwrites = {everyone: discord.PermissionOverwrite(view_channel=True)}
        self.assertTrue(views.all_see(self.guild, self.channel, rules), "everyone who reads general can read the rules")
        self.assertFalse(views.all_see(self.guild, rules, self.channel), "someone not yet verified reads the rules and can't see general")
        self.assertTrue(views.all_see(self.guild, self.channel, self.channel))
        thread = FakeChannel(self.guild, "a thread", discord.ChannelType.public_thread)
        thread.parent = rules
        self.assertFalse(views.all_see(self.guild, thread, self.channel), "a thread's readers are its channel's")
        self.assertTrue(verified.permissions.view_channel)


if __name__ == "__main__":
    unittest.main()
