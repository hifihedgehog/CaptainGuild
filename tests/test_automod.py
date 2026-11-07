import datetime
import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.core.validate import BAN, CHANNEL_MESSAGE, DELETE, DM_OFFENDER, DRAMA, Invalid, KICK, MUTE, TEMPBAN, TEMPMUTE, TIMEOUT, WARN
from captainguild.modules import moderation as moderation_module
from captainguild.modules.automod import Automod, DramaView, caps_percent, domain, fold, hosts_in, invite_entry, link_matches, parse_punishment, parse_rate, settings_from_export, word_entry
from captainguild.modules.moderation import Moderation
from tests.fakes import BotTest, FakeChannel, FakeMember, FakeMessage, _FakeResponse, new_id


class Helpers(unittest.TestCase):
    def test_fold_ignores_case_accents_widths_and_punctuation(self):
        self.assertEqual(fold("B.o.A-T!"), "boat")
        self.assertEqual(fold("Crème"), "creme")
        self.assertEqual(fold("ｂｏａｔ"), "boat")
        self.assertEqual(fold("two   words"), "two words")

    def test_domain_cleans_a_pasted_link(self):
        self.assertEqual(domain("https://www.Example.com/"), "example.com")
        self.assertEqual(domain("example.com/docs"), "example.com/docs")
        for bad in ("notadomain", "", "http://", 5):
            with self.assertRaises(Invalid):
                domain(bad)

    def test_link_match_is_about_the_host(self):
        entries = [("example.com", ""), ("store.test.net", "/en")]
        self.assertTrue(link_matches("example.com", "/x", entries))
        self.assertTrue(link_matches("old.example.com", "/x", entries))
        self.assertFalse(link_matches("example.com.evil.net", "/x", entries))
        self.assertFalse(link_matches("notexample.com", "/x", entries))
        self.assertTrue(link_matches("store.test.net", "/en/page", entries))
        self.assertFalse(link_matches("store.test.net", "/de/page", entries))

    def test_hosts_in_sees_through_tricks(self):
        self.assertEqual(hosts_in("go to https://www.Example.com/a, ok")[0], ("example.com", "/a"))
        self.assertEqual(hosts_in("https://youtube.com@evil.net/x")[0][0], "evil.net")
        self.assertEqual(hosts_in("<https://example.com>")[0][0], "example.com")
        self.assertEqual(hosts_in("no links here. file.txt"), [])

    def test_caps_counts_letters_only(self):
        self.assertEqual(caps_percent("HELLO there")[0], 50)
        self.assertEqual(caps_percent("ABC 123 !!!")[0], 100)
        self.assertEqual(caps_percent("<@123456789012345678> hi")[0], 0)
        self.assertEqual(caps_percent("<:SHOUT:123456789012345678> hi")[0], 0)
        self.assertEqual(caps_percent("HTTPS://EXAMPLE.COM hi")[0], 0)
        self.assertEqual(caps_percent("1234"), (0, 0))

    def test_punishment_words(self):
        self.assertEqual(parse_punishment("delete, tempmute 3h, warn"), {"actions": DELETE | TEMPMUTE | WARN, "seconds": 10800, "purge_days": 0})
        self.assertEqual(parse_punishment("ban")["actions"], BAN)
        self.assertEqual(parse_punishment("defer dm message")["actions"], DRAMA | DM_OFFENDER | CHANNEL_MESSAGE)
        self.assertEqual(parse_punishment("timeout")["seconds"], 3600)
        self.assertEqual(parse_punishment("tempban 2d", keep_purge_days=3), {"actions": TEMPBAN, "seconds": 172800, "purge_days": 3})
        for bad in ("nonsense", "tempmute", "kick, ban", "mute tempmute 3h", "timeout 40d"):
            with self.assertRaises(Invalid):
                parse_punishment(bad)

    def test_rate_words(self):
        self.assertEqual(parse_rate("5 25"), [5, 25])
        self.assertEqual(parse_rate("30"), [1, 30])
        self.assertEqual(parse_rate("5/25"), [5, 25])
        for bad in ("", "a b", "0 5", "1 2 3"):
            with self.assertRaises(Invalid):
                parse_rate(bad)

    def test_invite_entry(self):
        self.assertEqual(invite_entry("https://discord.gg/AbC"), "AbC")
        self.assertEqual(invite_entry("discord.com/invite/AbC/"), "AbC")
        self.assertEqual(invite_entry("AbC"), "AbC")
        self.assertEqual(invite_entry(111222333444555666), "111222333444555666")
        with self.assertRaises(Invalid):
            invite_entry("not an invite")

    def test_word_entries(self):
        self.assertEqual(word_entry("boat"), {"text": "boat", "kind": "substring"})
        self.assertEqual(word_entry({"text": "b[o0]at", "kind": "regex"})["kind"], "regex")
        with self.assertRaises(Invalid):
            word_entry({"text": "(unclosed", "kind": "regex"})
        with self.assertRaises(Invalid):
            word_entry({"text": "x", "kind": "nonsense"})
        with self.assertRaises(Invalid):
            word_entry({"text": "a" * 501, "kind": "regex"})


class AutomodBase(BotTest):
    modules = [Moderation, Automod]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.auto = self.cog(Automod)
        self.mods = self.cog(Moderation)
        self.addAsyncCleanup(self.auto.regex.close)
        self.log_channel = FakeChannel(self.guild, "automod-log")
        self.drama = FakeChannel(self.guild, "drama")
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": self.role_muted.id})
        await self.bot.settings.save(self.guild.id, "automod", {"log_channel": self.log_channel.id, "drama_channel": self.drama.id})
        patcher = mock.patch.object(moderation_module, "AUDIT_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fetch_invite = mock.AsyncMock()
        self.bot.fetch_invite = self.fetch_invite

    async def rule(self, name, **fields):
        await self.bot.settings.save(self.guild.id, "automod", {name: {"enabled": True, **fields}})

    def message(self, content="hello", *, author=None, channel=None, **kwargs):
        channel = channel or self.channel
        message = FakeMessage(channel, author or self.member, content, **kwargs)
        channel.sent.append(message)
        return message

    async def say(self, content="hello", **kwargs):
        message = self.message(content, **kwargs)
        await self.auto.on_message(message)
        return message

    async def cases(self):
        return await self.db.all("SELECT * FROM cases WHERE guild_id = ? ORDER BY number", (self.guild.id,))

    def logged(self):
        return [embed for channel_id, embed, _ in self.bot.logs.posted if channel_id == self.log_channel.id]

    def foreign_invite(self, guild_id=None):
        self.fetch_invite.return_value = SimpleNamespace(guild=SimpleNamespace(id=guild_id or new_id()))


class InviteTest(AutomodBase):
    async def test_an_invite_to_another_server_is_punished_and_logged(self):
        await self.rule("invites", punishment={"actions": DELETE | WARN})
        self.foreign_invite()
        message = await self.say("join https://discord.gg/abc123 now")
        self.assertTrue(message.deleted)
        self.assertEqual((await self.cases())[0]["action"], "warn")
        self.assertEqual((await self.cases())[0]["source"], "automod")
        self.assertIsNone((await self.cases())[0]["moderator_id"])
        entry = self.logged()[0]
        self.assertEqual(entry["title"], "Automod: Invite spam")
        self.assertIn("Deleted the message", entry["description"])
        self.assertIn("join https://discord.gg/abc123 now", entry["fields"][0]["value"])

    async def test_an_invite_to_this_server_never_counts(self):
        await self.rule("invites")
        self.foreign_invite(self.guild.id)
        message = await self.say("https://discord.gg/ours")
        self.assertFalse(message.deleted)
        self.guild.vanity_url_code = "OurVanity"
        self.fetch_invite.reset_mock()
        self.fetch_invite.return_value = SimpleNamespace(guild=SimpleNamespace(id=new_id()))
        message = await self.say("https://discord.gg/ourvanity")
        self.assertFalse(message.deleted)
        self.fetch_invite.assert_not_called()

    async def test_each_invite_form_is_seen(self):
        await self.rule("invites")
        self.foreign_invite()
        for text in ("discord.gg/abc", "https://discordapp.com/invite/abc", "discord.com/invite/abc", "https://dsc.gg/abc", "discord . gg/abc"):
            message = await self.say(text)
            self.assertTrue(message.deleted, text)

    async def test_a_dead_invite_does_not_count(self):
        await self.rule("invites")
        self.fetch_invite.side_effect = discord.NotFound(_FakeResponse(404), "Unknown Invite")
        message = await self.say("discord.gg/dead")
        self.assertFalse(message.deleted)

    async def test_a_failed_lookup_counts_so_an_outage_lets_nothing_through(self):
        await self.rule("invites")
        self.fetch_invite.side_effect = discord.HTTPException(_FakeResponse(500), "boom")
        message = await self.say("discord.gg/maybe")
        self.assertTrue(message.deleted)

    async def test_lookups_are_remembered(self):
        await self.rule("invites", punishment={"actions": 0})
        self.foreign_invite(self.guild.id)
        await self.say("discord.gg/same")
        await self.say("discord.gg/same")
        self.assertEqual(self.fetch_invite.call_count, 1)

    async def test_the_whitelist_takes_a_code_or_a_server(self):
        partner = new_id()
        await self.rule("invites", whitelist=["https://discord.gg/friend", str(partner)])
        self.foreign_invite(partner)
        self.assertFalse((await self.say("discord.gg/other")).deleted)
        self.foreign_invite()
        self.assertFalse((await self.say("discord.gg/FRIEND")).deleted)
        self.assertTrue((await self.say("discord.gg/stranger")).deleted)

    async def test_no_role_mode(self):
        await self.rule("invites", no_role=True)
        self.foreign_invite()
        self.assertFalse((await self.say("discord.gg/abc")).deleted)
        bare = FakeMember(self.guild, "bare")
        self.assertTrue((await self.say("discord.gg/abc", author=bare)).deleted)

    async def test_a_forwarded_invite_is_caught_too(self):
        await self.rule("invites")
        self.foreign_invite()
        message = self.message("")
        message.message_snapshots = [SimpleNamespace(content="discord.gg/abc", attachments=[])]
        await self.auto.on_message(message)
        self.assertTrue(message.deleted)


class LinkTest(AutomodBase):
    async def test_blacklist_mode(self):
        await self.rule("links", mode="blacklist", blacklist=["reddit.com"], rate=[1, 1])
        self.assertTrue((await self.say("https://old.reddit.com/r/x")).deleted)
        self.assertFalse((await self.say("https://example.com")).deleted)
        self.assertFalse((await self.say("https://reddit.com.evil.net/")).deleted)

    async def test_blacklist_catches_a_site_typed_without_https(self):
        await self.rule("links", mode="blacklist", blacklist=["reddit.com"], rate=[1, 1])
        self.assertTrue((await self.say("go to reddit.com/r/x")).deleted)
        self.assertFalse((await self.say("notreddit.community")).deleted)

    async def test_whitelist_mode_like_the_old_link_filter(self):
        await self.rule("links", mode="whitelist", whitelist=["example.com", "wiki.test.net/en"], rate=[1, 1])
        self.assertFalse((await self.say("https://sub.example.com/page")).deleted)
        self.assertFalse((await self.say("https://wiki.test.net/en/a")).deleted)
        self.assertTrue((await self.say("https://wiki.test.net/de/a")).deleted)
        self.assertTrue((await self.say("https://example.com.evil.net")).deleted)
        self.assertTrue((await self.say("https://example.com@evil.net")).deleted)
        self.assertFalse((await self.say("no links, and file.txt")).deleted)

    async def test_no_role_mode(self):
        await self.rule("links", mode="no_role", whitelist=["example.com"], rate=[1, 1])
        self.assertFalse((await self.say("https://elsewhere.net")).deleted)
        bare = FakeMember(self.guild, "bare")
        self.assertTrue((await self.say("https://elsewhere.net", author=bare)).deleted)
        self.assertFalse((await self.say("https://example.com", author=bare)).deleted)

    async def test_the_rate_must_be_reached(self):
        await self.rule("links", mode="whitelist", rate=[3, 60])
        self.assertFalse((await self.say("https://a.net https://b.net")).deleted)
        self.assertTrue((await self.say("https://c.net")).deleted)

    async def test_a_zero_rate_never_trips(self):
        await self.rule("links", mode="whitelist", rate=[0, 0])
        self.assertFalse((await self.say("https://a.net")).deleted)


class WordTest(AutomodBase):
    async def test_substring_mode_ignores_punctuation_and_case(self):
        await self.rule("words", entries=["boat"])
        self.assertTrue((await self.say("my FREDBOAT rocks")).deleted)
        self.assertTrue((await self.say("b.o.a.t")).deleted)
        self.assertTrue((await self.say("b o a t")).deleted)
        self.assertFalse((await self.say("coat")).deleted)

    async def test_whole_word_mode(self):
        await self.rule("words", entries=[{"text": "boat", "kind": "word"}])
        self.assertTrue((await self.say("a BOAT!")).deleted)
        self.assertFalse((await self.say("my fredboat rocks")).deleted)

    async def test_regex_mode(self):
        await self.rule("words", entries=[{"text": r"b[o0]+at\b", "kind": "regex"}])
        self.assertTrue((await self.say("a B00AT here")).deleted)
        self.assertFalse((await self.say("boats")).deleted)

    async def test_a_runaway_regex_cannot_stall_the_bot_and_shows_in_the_log(self):
        self.auto.regex.time_limit = 0.5
        await self.rule("words", entries=[{"text": "(a+)+$", "kind": "regex"}])
        started = time.monotonic()
        message = await self.say("a" * 40 + "!")
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(message.deleted)
        self.assertIn("slow", self.logged()[0]["title"])
        again = await self.say("a" * 40 + "!")
        self.assertFalse(again.deleted)
        self.assertEqual(len(self.logged()), 1)

    async def test_saving_refuses_a_broken_regex(self):
        client = await self.client()
        response = await client.put(self.path("/settings/automod"), json={"words": {"entries": [{"text": "(oops", "kind": "regex"}]}})
        self.assertEqual(response.status, 400)
        self.assertIn("words.entries", (await response.json())["fields"])

    async def test_the_page_can_ask_about_a_pattern_before_adding_it(self):
        client = await self.client()
        for pattern, bad in (("hel+o", False), ("(oops", True), ("(?<=a+)b", True)):
            body = await (await client.post(self.path("/automod/check-pattern"), json={"pattern": pattern})).json()
            self.assertEqual(bool(body["problem"]), bad, pattern)
        self.assertEqual((await client.post(self.path("/automod/check-pattern"), json={"pattern": ""})).status, 400)
        self.assertEqual((await client.post(self.path("/automod/check-pattern"), json=["no"])).status, 400)
        staff = await self.client(access="staff")
        self.assertEqual((await staff.post(self.path("/automod/check-pattern"), json={"pattern": "a"})).status, 403)

    async def test_a_command_refuses_a_pattern_only_compiling_can_catch(self):
        ctx = self.context(self.admin)
        await self.auto.automod_censor_words.callback(self.auto, ctx, "add", words="regex:(?<=a+)b")
        self.assertTrue(ctx.fails)
        self.assertEqual(self.settings("automod")["words"]["entries"], [])

    async def test_big_lists_are_fine(self):
        await self.rule("words", entries=[f"word{index}" for index in range(5000)])
        self.assertTrue((await self.say("a word4999 here")).deleted)


class RateTest(AutomodBase):
    async def test_mentions_add_up_and_reach_the_limit(self):
        await self.rule("mentions", rate=[3, 60], punishment={"actions": DELETE})
        people = [FakeMember(self.guild, f"p{i}") for i in range(3)]
        self.assertFalse((await self.say("hi", mentions=people[:2])).deleted)
        self.assertTrue((await self.say("hi", mentions=people[2:])).deleted)

    async def test_mention_everyone_and_roles_count(self):
        await self.rule("mentions", rate=[2, 60])
        message = self.message("hi", role_mentions=[self.role_member])
        message.mention_everyone = True
        await self.auto.on_message(message)
        self.assertTrue(message.deleted)

    async def test_a_message_with_no_mentions_never_trips_it(self):
        await self.rule("mentions", rate=[1, 60])
        self.assertFalse((await self.say("hi")).deleted)

    async def test_attachments_allow_the_limit_and_punish_the_next(self):
        await self.rule("attachments", rate=[2, 60])
        file = SimpleNamespace(filename="a.png")
        self.assertFalse((await self.say("x", attachments=[file, file])).deleted)
        self.assertTrue((await self.say("x", attachments=[file])).deleted)

    async def test_message_rate_deletes_even_without_delete_in_the_punishment(self):
        await self.rule("spam", rate=[2, 60], punishment={"actions": WARN})
        self.assertFalse((await self.say("a")).deleted)
        self.assertFalse((await self.say("b")).deleted)
        self.assertTrue((await self.say("c")).deleted)
        self.assertEqual(len(await self.cases()), 1)

    async def test_more_messages_in_the_burst_go_without_a_second_punishment(self):
        await self.rule("spam", rate=[1, 60], punishment={"actions": WARN})
        await self.say("a")
        await self.say("b")
        third = await self.say("c")
        self.assertTrue(third.deleted)
        self.assertEqual(len(await self.cases()), 1)
        self.assertEqual(len(self.logged()), 1)

    async def test_people_are_counted_apart(self):
        await self.rule("spam", rate=[1, 60])
        await self.say("a")
        self.assertFalse((await self.say("b", author=self.mod)).deleted)

    async def test_the_window_slides(self):
        await self.rule("spam", rate=[1, 60])
        await self.say("a")
        window = self.auto._windows[(self.guild.id, self.member.id, "spam", 60)]
        window[0] = (window[0][0] - 61, 1)
        self.assertFalse((await self.say("b")).deleted)


class SimpleRuleTest(AutomodBase):
    async def test_caps(self):
        await self.rule("caps", threshold=70)
        self.assertTrue((await self.say("STOP SHOUTING NOW")).deleted)
        self.assertFalse((await self.say("Stop shouting now")).deleted)
        self.assertFalse((await self.say("OK!")).deleted)

    async def test_honeypot_bans_with_purge_days_by_default(self):
        trap = FakeChannel(self.guild, "trap")
        await self.rule("honeypot", channels=[trap.id], punishment={"actions": DELETE | BAN, "purge_days": 1})
        message = await self.say("buy now", channel=trap)
        self.assertTrue(message.deleted)
        self.assertIn(self.member.id, self.guild.banned)
        self.assertEqual((await self.cases())[0]["action"], "ban")
        self.assertFalse((await self.say("hello", author=self.mod)).deleted)

    async def test_honeypot_takes_a_category(self):
        category = FakeChannel(self.guild, "traps", discord.ChannelType.category)
        trap = FakeChannel(self.guild, "trap", category=category)
        await self.rule("honeypot", channels=[category.id])
        self.assertTrue((await self.say("x", channel=trap)).deleted)

    async def test_media_only(self):
        gallery = FakeChannel(self.guild, "gallery")
        await self.bot.settings.save(self.guild.id, "automod", {"media_channels": [gallery.id]})
        self.assertTrue((await self.say("just talking", channel=gallery)).deleted)
        self.assertFalse((await self.say("look", channel=gallery, attachments=[SimpleNamespace(filename="a.png")])).deleted)
        self.assertFalse((await self.say("https://example.com/a.png", channel=gallery)).deleted)
        self.assertFalse((await self.say("elsewhere")).deleted)
        self.assertEqual(self.logged()[0]["title"], "Automod: Media only")

    async def test_unsafe_files(self):
        await self.bot.settings.save(self.guild.id, "automod", {"delete_files": True})
        self.assertTrue((await self.say("x", attachments=[SimpleNamespace(filename="virus.exe")])).deleted)
        self.assertTrue((await self.say("x", attachments=[SimpleNamespace(filename="README")])).deleted)
        self.assertFalse((await self.say("x", attachments=[SimpleNamespace(filename="photo.JPG")])).deleted)
        self.assertIn("virus.exe", self.logged()[0]["description"])

    async def test_an_edit_is_checked(self):
        await self.rule("words", entries=["boat"])
        message = self.message("a nice boat")
        message.edited_at = discord.utils.utcnow()
        await self.auto.on_raw_message_edit(SimpleNamespace(message=message, guild_id=self.guild.id))
        self.assertTrue(message.deleted)

    async def test_a_preview_arriving_is_not_an_edit(self):
        await self.rule("words", entries=["boat"])
        message = self.message("a nice boat")
        await self.auto.on_raw_message_edit(SimpleNamespace(message=message, guild_id=self.guild.id))
        self.assertFalse(message.deleted)

    async def test_the_same_edit_is_only_punished_once(self):
        await self.rule("words", entries=["boat"], punishment={"actions": WARN})
        message = self.message("a nice boat")
        message.edited_at = discord.utils.utcnow()
        await self.auto.on_raw_message_edit(SimpleNamespace(message=message, guild_id=self.guild.id))
        await self.auto.on_raw_message_edit(SimpleNamespace(message=message, guild_id=self.guild.id))
        self.assertEqual(len(await self.cases()), 1)

    async def test_an_edit_does_not_count_toward_rate_rules(self):
        await self.rule("spam", rate=[1, 60])
        message = self.message("a")
        message.edited_at = discord.utils.utcnow()
        for _ in range(3):
            message.edited_at += datetime.timedelta(seconds=1)
            await self.auto.on_raw_message_edit(SimpleNamespace(message=message, guild_id=self.guild.id))
        self.assertFalse(message.deleted)


class WhoIsExemptTest(AutomodBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.rule("words", entries=["boat"])

    async def test_manage_server_and_administrator_are_immune(self):
        manager = FakeMember(self.guild, "manager", self.guild.role("Mgr", 4, "manage_guild"))
        self.assertFalse((await self.say("boat", author=manager)).deleted)
        self.assertFalse((await self.say("boat", author=self.admin)).deleted)

    async def test_bots_and_webhooks_are_left_alone(self):
        other_bot = FakeMember(self.guild, "SomeBot", bot=True)
        self.assertFalse((await self.say("boat", author=other_bot)).deleted)
        message = self.message("boat")
        message.webhook_id = 123
        await self.auto.on_message(message)
        self.assertFalse(message.deleted)

    async def test_system_messages_are_left_alone(self):
        message = self.message("boat")
        message.type = discord.MessageType.pins_add
        await self.auto.on_message(message)
        self.assertFalse(message.deleted)

    async def test_allowed_channels_categories_and_roles(self):
        category = FakeChannel(self.guild, "safe", discord.ChannelType.category)
        inside = FakeChannel(self.guild, "inside", category=category)
        free = FakeChannel(self.guild, "free")
        await self.bot.settings.save(self.guild.id, "automod", {"allowed_channels": [category.id, free.id], "allowed_roles": [self.role_mod.id]})
        self.assertFalse((await self.say("boat", channel=inside)).deleted)
        self.assertFalse((await self.say("boat", channel=free)).deleted)
        self.assertFalse((await self.say("boat", author=self.mod)).deleted)
        self.assertTrue((await self.say("boat")).deleted)

    async def test_a_thread_takes_its_parents_exemption(self):
        free = FakeChannel(self.guild, "free")
        thread = FakeChannel(self.guild, "thread", discord.ChannelType.public_thread)
        thread.parent = free
        await self.bot.settings.save(self.guild.id, "automod", {"allowed_channels": [free.id]})
        self.assertFalse((await self.say("boat", channel=thread)).deleted)

    async def test_per_rule_exemptions(self):
        free = FakeChannel(self.guild, "free")
        await self.rule("words", exempt_channels=[free.id], exempt_roles=[self.role_mod.id])
        self.assertFalse((await self.say("boat", channel=free)).deleted)
        self.assertFalse((await self.say("boat", author=self.mod)).deleted)
        self.assertTrue((await self.say("boat")).deleted)

    async def test_a_rule_that_is_off_does_nothing(self):
        await self.bot.settings.save(self.guild.id, "automod", {"words": {"enabled": False}})
        self.assertFalse((await self.say("boat")).deleted)


class PunishmentTest(AutomodBase):
    async def test_each_heavy_action(self):
        cases = (
            (MUTE, 0, "mute", None), (TEMPMUTE, 600, "mute", 600), (TIMEOUT, 600, "timeout", 600), (KICK, 0, "kick", None), (TEMPBAN, 600, "tempban", 600), (BAN, 0, "ban", None),
        )
        for bit, seconds, action, expected_seconds in cases:
            target = FakeMember(self.guild, f"t{bit}", self.role_member)
            await self.rule("words", entries=["boat"], punishment={"actions": bit, "seconds": seconds})
            await self.say("boat", author=target)
            row = (await self.cases())[-1]
            self.assertEqual((row["action"], row["user_id"], row["seconds"]), (action, target.id, expected_seconds), action)

    async def test_channel_message_goes_away_after_a_while_and_pings_only_them(self):
        await self.rule("words", entries=["boat"], punishment={"actions": CHANNEL_MESSAGE})
        self.channel.sent.clear()
        await self.say("boat")
        notice = self.channel.sent[-1]
        self.assertIn(self.member.mention, notice.content)
        self.assertEqual(notice.kwargs["delete_after"], 15)
        self.assertFalse(notice.kwargs["allowed_mentions"].everyone)

    async def test_a_spam_notice_goes_after_five_seconds(self):
        await self.rule("spam", rate=[1, 60], punishment={"actions": CHANNEL_MESSAGE})
        await self.say("a")
        await self.say("b")
        self.assertEqual(self.channel.sent[-1].kwargs["delete_after"], 5)

    async def test_dm_replaces_the_moderation_dm_and_goes_before_a_ban(self):
        order = []
        real_send, real_ban = self.member.send, self.guild.ban

        async def send(*args, **kwargs):
            order.append("dm")
            return await real_send(*args, **kwargs)

        async def ban(*args, **kwargs):
            order.append("ban")
            return await real_ban(*args, **kwargs)

        self.member.send = send
        self.guild.ban = ban
        await self.bot.settings.save(self.guild.id, "moderation", {"ban": {"mode": 3}})
        await self.rule("words", entries=["boat"], punishment={"actions": DM_OFFENDER | BAN})
        await self.say("boat")
        self.assertEqual(order, ["dm", "ban"])
        self.assertEqual(len(self.member.dms), 1)
        self.assertIn("Automod in Test Server", self.member.dms[0]["content"])
        self.assertIn("You were banned", self.member.dms[0]["content"])

    async def test_without_the_dm_bit_the_moderation_dm_setting_applies(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"warn": {"mode": 1}})
        await self.rule("words", entries=["boat"], punishment={"actions": WARN})
        await self.say("boat")
        self.assertEqual(self.member.dms[0]["content"], "You were warned in Test Server.")

    async def test_one_action_failing_is_logged_and_the_others_still_happen(self):
        high = FakeMember(self.guild, "high", self.guild.role("High", 30))
        await self.rule("words", entries=["boat"], punishment={"actions": DELETE | BAN})
        message = await self.say("boat", author=high)
        self.assertTrue(message.deleted)
        self.assertIn("Couldn't ban", self.logged()[0]["description"])

    async def test_a_message_already_gone_does_not_raise(self):
        await self.rule("words", entries=["boat"])
        message = self.message("boat")

        async def gone(*args, **kwargs):
            raise discord.NotFound(_FakeResponse(404), "Unknown Message")
        message.delete = gone
        await self.auto.on_message(message)
        self.assertIn("already gone", self.logged()[0]["description"])

    async def test_a_message_that_cannot_be_deleted_does_not_raise(self):
        await self.rule("words", entries=["boat"])
        message = self.message("boat")

        async def refused(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "Missing Permissions")
        message.delete = refused
        await self.auto.on_message(message)
        self.assertIn("Couldn't delete", self.logged()[0]["description"])

    async def test_only_the_first_rule_acts_on_a_message(self):
        await self.rule("words", entries=["boat"], punishment={"actions": WARN})
        await self.rule("caps", threshold=50, punishment={"actions": WARN})
        await self.say("BOAT BOAT BOAT")
        self.assertEqual(len(await self.cases()), 1)

    async def test_moderation_not_loaded_only_means_no_punishment(self):
        await self.rule("words", entries=["boat"], punishment={"actions": DELETE | BAN})
        self.bot.modules = [m for m in self.bot.modules if m.key != "moderation"]
        message = await self.say("boat")
        self.assertTrue(message.deleted)
        self.assertIn("Moderation isn't loaded", self.logged()[0]["description"])

    async def test_no_log_channel_means_no_log_and_no_crash(self):
        await self.bot.settings.save(self.guild.id, "automod", {"log_channel": None})
        await self.rule("words", entries=["boat"])
        self.assertTrue((await self.say("boat")).deleted)
        self.assertEqual(self.bot.logs.posted, [])

    async def test_saving_refuses_two_heavy_actions(self):
        client = await self.client()
        response = await client.put(self.path("/settings/automod"), json={"words": {"punishment": {"actions": MUTE | BAN}}})
        self.assertEqual(response.status, 400)
        response = await client.put(self.path("/settings/automod"), json={"words": {"punishment": {"actions": TEMPMUTE, "seconds": 0}}})
        self.assertEqual(response.status, 400)
        response = await client.put(self.path("/settings/automod"), json={"links": {"rate": ["a", 1]}})
        self.assertEqual(response.status, 400)


class LogScopeTest(AutomodBase):
    async def test_a_log_channel_in_another_server_gets_nothing(self):
        await self.bot.settings.save(self.guild.id, "automod", {"log_channel": 111222333444555666})
        await self.rule("words", entries=["boat"])
        self.assertTrue((await self.say("boat")).deleted)
        self.assertEqual(self.bot.logs.posted, [])

    async def test_a_drama_channel_in_another_server_gets_nothing(self):
        await self.bot.settings.save(self.guild.id, "automod", {"drama_channel": 111222333444555666})
        await self.rule("words", entries=["boat"], punishment={"actions": DRAMA})
        await self.say("boat")
        self.assertEqual(self.drama.sent, [])
        self.assertIn("No drama channel", self.logged()[0]["description"])


class DryRunTest(AutomodBase):
    async def test_it_logs_what_would_happen_and_does_nothing(self):
        await self.rule("words", entries=["boat"], dry_run=True, punishment={"actions": DELETE | BAN | DRAMA})
        message = await self.say("boat")
        self.assertFalse(message.deleted)
        self.assertEqual(self.guild.banned, [])
        self.assertEqual(await self.cases(), [])
        self.assertEqual(self.drama.sent, [])
        entry = self.logged()[0]
        self.assertIn("(dry run)", entry["title"])
        self.assertIn("Would do", entry["description"])
        self.assertIn("delete, ban, post to drama", entry["description"])

    async def test_the_next_rule_still_gets_its_turn(self):
        await self.rule("words", entries=["boat"], dry_run=True)
        await self.rule("caps", threshold=50)
        message = await self.say("BOAT BOAT")
        self.assertTrue(message.deleted)
        self.assertEqual(len(self.logged()), 2)

    async def test_general_dry_run_covers_media_and_files(self):
        gallery = FakeChannel(self.guild, "gallery")
        await self.bot.settings.save(self.guild.id, "automod", {"media_channels": [gallery.id], "delete_files": True, "general_dry_run": True})
        self.assertFalse((await self.say("talking", channel=gallery)).deleted)
        self.assertFalse((await self.say("x", attachments=[SimpleNamespace(filename="a.exe")])).deleted)
        self.assertEqual(len(self.logged()), 2)
        self.assertIn("(dry run)", self.logged()[0]["title"])


class ThresholdTest(AutomodBase):
    async def test_past_the_limit_each_new_warning_draws_the_punishment(self):
        await self.rule("warns", threshold=2, punishment={"actions": TEMPMUTE, "seconds": 600})
        for _ in range(2):
            await self.mods.punish(self.guild, self.member, "warn", moderator=self.mod)
        self.assertNotIn(self.role_muted, self.member.roles)
        await self.mods.punish(self.guild, self.member, "warn", moderator=self.mod)
        self.assertIn(self.role_muted, self.member.roles)
        rows = await self.cases()
        self.assertEqual(rows[-1]["action"], "mute")
        self.assertEqual(rows[-1]["source"], "threshold")

    async def test_removing_warnings_brings_them_back_under(self):
        await self.rule("warns", threshold=1, punishment={"actions": KICK})
        first = await self.mods.punish(self.guild, self.member, "warn", moderator=self.mod)
        await self.mods.void_case(self.guild.id, first["number"], self.mod)
        await self.mods.punish(self.guild, self.member, "warn", moderator=self.mod)
        self.assertIn(self.member, self.guild.members)

    async def test_zero_turns_it_off(self):
        await self.rule("warns", threshold=0, punishment={"actions": KICK})
        for _ in range(3):
            await self.mods.punish(self.guild, self.member, "warn", moderator=self.mod)
        self.assertIn(self.member, self.guild.members)

    async def test_a_warning_given_by_automod_counts(self):
        await self.rule("warns", threshold=1, punishment={"actions": KICK})
        await self.rule("words", entries=["boat"], punishment={"actions": WARN})
        await self.say("boat")
        self.auto._last_fired.clear()
        await self.say("boat")
        self.assertNotIn(self.member, self.guild.members)

    async def test_immunity_and_exempt_roles(self):
        await self.rule("warns", threshold=1, punishment={"actions": KICK}, exempt_roles=[self.role_member.id])
        for _ in range(3):
            await self.mods.record_case(self.guild, "warn", self.member, self.mod, post=False)
        self.assertIn(self.member, self.guild.members)

    async def test_dry_run(self):
        await self.rule("warns", threshold=1, punishment={"actions": KICK}, dry_run=True)
        for _ in range(2):
            await self.mods.punish(self.guild, self.member, "warn", moderator=self.mod)
        self.assertIn(self.member, self.guild.members)
        self.assertIn("(dry run)", self.logged()[0]["title"])

    async def test_someone_who_left_is_not_chased(self):
        await self.rule("warns", threshold=0)
        await self.rule("warns", threshold=1, punishment={"actions": KICK})
        gone = moderation_module.Someone(new_id(), "gone")
        for _ in range(3):
            await self.mods.record_case(self.guild, "warn", gone, self.mod, post=False)
        self.assertEqual(len(await self.cases()), 3)


class FakeResponse:
    def __init__(self):
        self.sent = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.sent.append(content)

    async def defer(self, **kwargs):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


def clicked(guild, user, message, channel):
    return SimpleNamespace(guild=guild, user=user, message=message, channel=channel, response=FakeResponse(), followup=FakeFollowup())


class DramaTest(AutomodBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.rule("words", entries=["boat"], punishment={"actions": DRAMA})

    async def post(self, content="a boat"):
        await self.say(content)
        return self.drama.sent[-1]

    async def test_an_incident_posts_context_and_buttons(self):
        post = await self.post()
        embed = post.embeds[0]
        self.assertEqual(embed.title, "Incident: Bad words")
        self.assertIn(self.member.mention, embed.description)
        self.assertIn(self.channel.mention, embed.description)
        self.assertEqual(embed.fields[0].value, "a boat")
        view = post.kwargs["view"]
        self.assertEqual({item.label for item in view.children}, {"Ban", "Kick", "Mute", "Dismiss"})
        self.assertTrue(all(item.custom_id.startswith("automod:drama:") for item in view.children))
        self.assertIsNone(view.timeout)
        row = await self.db.one("SELECT * FROM drama_incidents WHERE guild_id = ? AND message_id = ?", (self.guild.id, post.id))
        self.assertEqual((row["user_id"], row["rule"], row["handled_at"]), (self.member.id, "words", None))

    async def test_the_buttons_survive_a_restart(self):
        self.assertTrue(any(isinstance(view, DramaView) for view in self.bot.added_views))
        self.assertTrue(next(view for view in self.bot.added_views if isinstance(view, DramaView)).is_persistent())

    async def test_ban_button_bans_and_closes_the_incident(self):
        post = await self.post()
        press = clicked(self.guild, self.admin, post, self.drama)
        await self.auto.drama_action(press, "ban")
        self.assertIn(self.member.id, self.guild.banned)
        row = (await self.cases())[-1]
        self.assertEqual((row["action"], row["moderator_id"], row["source"]), ("ban", self.admin.id, "drama"))
        self.assertIn("banned them", post.embeds[0].description)
        self.assertTrue(all(item.disabled for item in post.kwargs["view"].children))
        record = await self.db.one("SELECT * FROM drama_incidents WHERE message_id = ?", (post.id,))
        self.assertEqual((record["outcome"], record["handled_by"]), ("ban", self.admin.id))

    async def test_kick_mute_and_dismiss(self):
        post = await self.post()
        await self.auto.drama_action(clicked(self.guild, self.admin, post, self.drama), "mute")
        self.assertIn(self.role_muted, self.member.roles)
        self.auto._last_fired.clear()
        post = await self.post()
        await self.auto.drama_action(clicked(self.guild, self.admin, post, self.drama), "dismiss")
        self.assertIn("dismissed this", post.embeds[0].description)
        self.auto._last_fired.clear()
        post = await self.post()
        await self.auto.drama_action(clicked(self.guild, self.admin, post, self.drama), "kick")
        self.assertNotIn(self.member, self.guild.members)

    async def test_someone_without_the_permission_is_refused(self):
        post = await self.post()
        press = clicked(self.guild, self.mod, post, self.drama)
        await self.auto.drama_action(press, "ban")
        self.assertEqual(press.response.sent, ["You can't do that here."])
        self.assertEqual(self.guild.banned, [])

    async def test_a_bypass_role_counts(self):
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"ban": {"bypass_roles": [self.role_mod.id]}}})
        post = await self.post()
        await self.auto.drama_action(clicked(self.guild, self.mod, post, self.drama), "ban")
        self.assertIn(self.member.id, self.guild.banned)

    async def test_hierarchy_still_applies_and_leaves_it_open(self):
        strong = FakeMember(self.guild, "strong", self.guild.role("Strong", 6))
        await self.rule("words", entries=["boat"], punishment={"actions": DRAMA})
        await self.say("a boat", author=strong)
        post = self.drama.sent[-1]
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"ban": {"bypass_roles": [self.role_mod.id]}}})
        press = clicked(self.guild, self.mod, post, self.drama)
        await self.auto.drama_action(press, "ban")
        self.assertIn("at or above yours", press.followup.sent[0])
        record = await self.db.one("SELECT * FROM drama_incidents WHERE message_id = ?", (post.id,))
        self.assertIsNone(record["handled_at"])

    async def test_two_clicks_act_once(self):
        post = await self.post()
        await self.auto.drama_action(clicked(self.guild, self.admin, post, self.drama), "ban")
        late = clicked(self.guild, self.owner, post, self.drama)
        await self.auto.drama_action(late, "kick")
        self.assertIn("already handled", late.followup.sent[0])
        self.assertEqual(len(await self.cases()), 1)

    async def test_an_incident_not_on_record_is_refused(self):
        stray = FakeMessage(self.drama, self.me, "old")
        press = clicked(self.guild, self.admin, stray, self.drama)
        await self.auto.drama_action(press, "ban")
        self.assertIn("isn't on record", press.response.sent[0])

    async def test_no_drama_channel_is_said_in_the_log(self):
        await self.bot.settings.save(self.guild.id, "automod", {"drama_channel": None})
        await self.say("a boat")
        self.assertIn("No drama channel", self.logged()[0]["description"])

    async def test_a_deleted_drama_channel_does_not_raise(self):
        self.guild.channels.remove(self.drama)
        await self.say("a boat")
        self.assertIn("No drama channel", self.logged()[0]["description"])

    async def test_someone_who_left_can_still_be_banned(self):
        post = await self.post()
        self.guild.members.remove(self.member)
        await self.auto.drama_action(clicked(self.guild, self.admin, post, self.drama), "ban")
        self.assertIn(self.member.id, self.guild.banned)


class CommandTest(AutomodBase):
    async def test_general_settings(self):
        ctx = self.context(self.admin)
        await self.auto.automod_log.callback(self.auto, ctx, channel=self.channel)
        await self.auto.automod_drama.callback(self.auto, ctx, channel=self.drama)
        await self.auto.automod_deletefiles.callback(self.auto, ctx)
        cfg = self.settings("automod")
        self.assertEqual((cfg["log_channel"], cfg["drama_channel"], cfg["delete_files"]), (self.channel.id, self.drama.id, True))
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "automod.deletefiles")

    async def test_overview_lists_every_rule(self):
        ctx = self.context(self.admin)
        await self.auto.automod.callback(self.auto, ctx)
        description = ctx.sent[0]["embed"].description
        for label in ("Invite spam", "Bad words", "Honeypot", "Message spam", "Warn threshold"):
            self.assertIn(label, description)
        self.assertEqual(ctx.sent[0]["allowed_mentions"].roles, False)

    async def test_media_and_unmedia(self):
        ctx = self.context(self.admin)
        await self.auto.automod_media.callback(self.auto, ctx, channels=f"{self.channel.mention} {self.drama.id} nosuchplace")
        self.assertEqual(self.settings("automod")["media_channels"], [self.channel.id, self.drama.id])
        self.assertIn("nosuchplace", ctx.oks[0])
        await self.auto.automod_unmedia.callback(self.auto, ctx, channels=self.channel.mention)
        self.assertEqual(self.settings("automod")["media_channels"], [self.drama.id])
        ctx = self.context(self.admin)
        await self.auto.automod_media.callback(self.auto, ctx, channels="nosuchplace")
        self.assertTrue(ctx.fails)

    async def test_whitelist(self):
        ctx = self.context(self.admin)
        await self.auto.automod_whitelist_channels.callback(self.auto, ctx, "add", channels=self.channel.mention)
        await self.auto.automod_whitelist_roles.callback(self.auto, ctx, "add", roles=f"{self.role_mod.mention} Member")
        cfg = self.settings("automod")
        self.assertEqual(cfg["allowed_channels"], [self.channel.id])
        self.assertEqual(cfg["allowed_roles"], [self.role_mod.id, self.role_member.id])
        await self.auto.automod_whitelist_roles.callback(self.auto, ctx, "remove", roles="Member")
        self.assertEqual(self.settings("automod")["allowed_roles"], [self.role_mod.id])

    async def test_rate_commands_turn_the_rule_on(self):
        ctx = self.context(self.admin)
        for command, name in ((self.auto.automod_slowmode_set, "spam"), (self.auto.automod_attachmentspam_set, "attachments"), (self.auto.automod_mentionspam_set, "mentions"), (self.auto.automod_linkspam_rate, "links")):
            await command.callback(self.auto, ctx, rate="5 25")
            rule = self.settings("automod")[name]
            self.assertEqual((rule["enabled"], rule["rate"]), (True, [5, 25]), name)
        await self.auto.automod_slowmode_set.callback(self.auto, ctx, rate="30")
        self.assertEqual(self.settings("automod")["spam"]["rate"], [1, 30])
        await self.auto.automod_slowmode_disable.callback(self.auto, ctx)
        self.assertFalse(self.settings("automod")["spam"]["enabled"])
        ctx = self.context(self.admin)
        await self.auto.automod_slowmode_set.callback(self.auto, ctx, rate="soon")
        self.assertTrue(ctx.fails)

    async def test_punishment_commands(self):
        ctx = self.context(self.admin)
        for command, name in (
            (self.auto.automod_slowmode_punishment, "spam"), (self.auto.automod_attachmentspam_punishment, "attachments"), (self.auto.automod_mentionspam_punishment, "mentions"),
            (self.auto.automod_linkspam_punishment, "links"), (self.auto.automod_invitespam_punishment, "invites"), (self.auto.automod_censor_punishment, "words"),
            (self.auto.automod_caps_punishment, "caps"), (self.auto.automod_honeypot_punishment, "honeypot"),
        ):
            await command.callback(self.auto, ctx, punishments="delete, tempmute 20m")
            self.assertEqual(self.settings("automod")[name]["punishment"], {"actions": DELETE | TEMPMUTE, "seconds": 1200, "purge_days": 1}, name)
        ctx = self.context(self.admin)
        await self.auto.automod_censor_punishment.callback(self.auto, ctx, punishments="kick ban")
        self.assertTrue(ctx.fails)

    async def test_threshold(self):
        ctx = self.context(self.admin)
        await self.auto.automod_threshold.callback(self.auto, ctx, 5)
        self.assertEqual(self.settings("automod")["warns"]["threshold"], 5)
        self.assertTrue(self.settings("automod")["warns"]["enabled"])
        await self.auto.automod_threshold.callback(self.auto, ctx, 0)
        self.assertFalse(self.settings("automod")["warns"]["enabled"])

    async def test_link_lists_and_modes(self):
        ctx = self.context(self.admin)
        await self.auto.automod_linkspam_blacklist.callback(self.auto, ctx, "add", links="reddit.com https://www.twitter.com/")
        self.assertEqual(self.settings("automod")["links"]["blacklist"], ["reddit.com", "twitter.com"])
        await self.auto.automod_linkspam_blacklist.callback(self.auto, ctx, "remove", links="reddit.com")
        self.assertEqual(self.settings("automod")["links"]["blacklist"], ["twitter.com"])
        await self.auto.automod_linkspam_whitelist.callback(self.auto, ctx, "add", links="example.com")
        await self.auto.automod_linkspam_clear.callback(self.auto, ctx, "blacklist")
        self.assertEqual(self.settings("automod")["links"]["blacklist"], [])
        self.assertEqual(self.settings("automod")["links"]["whitelist"], ["example.com"])
        await self.auto.automod_linkspam_block.callback(self.auto, ctx)
        self.assertEqual(self.settings("automod")["links"]["mode"], "whitelist")
        await self.auto.automod_linkspam_norole.callback(self.auto, ctx)
        self.assertEqual(self.settings("automod")["links"]["mode"], "no_role")
        await self.auto.automod_linkspam_off.callback(self.auto, ctx)
        self.assertEqual(self.settings("automod")["links"]["mode"], "blacklist")
        ctx = self.context(self.admin)
        await self.auto.automod_linkspam_blacklist.callback(self.auto, ctx, "add", links="not a site")
        self.assertTrue(ctx.fails)
        ctx = self.context(self.admin)
        await self.auto.automod_linkspam.callback(self.auto, ctx)
        self.assertIn("Blacklist", ctx.sent[0]["embed"].description)

    async def test_invite_commands(self):
        ctx = self.context(self.admin)
        await self.auto.automod_invitespam_norole.callback(self.auto, ctx)
        self.assertTrue(self.settings("automod")["invites"]["no_role"])
        await self.auto.automod_invitespam_whitelist.callback(self.auto, ctx, "add", invites="discord.gg/friend")
        self.assertEqual(self.settings("automod")["invites"]["whitelist"], ["friend"])
        ctx = self.context(self.admin)
        await self.auto.automod_invitespam.callback(self.auto, ctx)
        self.assertIn("never count", ctx.sent[0]["embed"].description)

    async def test_censor_words_with_kinds_and_quotes(self):
        ctx = self.context(self.admin)
        await self.auto.automod_censor_words.callback(self.auto, ctx, "add", words='boat "bad word" word:car regex:^x+$')
        entries = self.settings("automod")["words"]["entries"]
        self.assertEqual(entries, [{"text": "boat", "kind": "substring"}, {"text": "bad word", "kind": "substring"}, {"text": "car", "kind": "word"}, {"text": "^x+$", "kind": "regex"}])
        self.assertTrue(self.settings("automod")["words"]["enabled"])
        await self.auto.automod_censor_words.callback(self.auto, ctx, "remove", words="word:car BOAT")
        self.assertEqual(len(self.settings("automod")["words"]["entries"]), 2)
        ctx = self.context(self.admin)
        await self.auto.automod_censor_display.callback(self.auto, ctx)
        self.assertIn("bad word", ctx.sent[0]["content"])
        self.assertEqual(ctx.sent[0]["allowed_mentions"].everyone, False)
        ctx = self.context(self.admin)
        await self.auto.automod_censor_words.callback(self.auto, ctx, "add", words="regex:(broken")
        self.assertTrue(ctx.fails)
        await self.auto.automod_censor_clear.callback(self.auto, self.context(self.admin))
        self.assertEqual(self.settings("automod")["words"]["entries"], [])

    async def test_caps_and_honeypot(self):
        ctx = self.context(self.admin)
        await self.auto.automod_caps_limit.callback(self.auto, ctx, 80)
        self.assertEqual(self.settings("automod")["caps"]["threshold"], 80)
        ctx = self.context(self.admin)
        await self.auto.automod_caps_limit.callback(self.auto, ctx, 101)
        self.assertTrue(ctx.fails)
        ctx = self.context(self.admin)
        await self.auto.automod_honeypot_add.callback(self.auto, ctx, channels=self.channel.mention)
        self.assertEqual(self.settings("automod")["honeypot"]["channels"], [self.channel.id])
        self.assertTrue(self.settings("automod")["honeypot"]["enabled"])
        ctx = self.context(self.admin)
        await self.auto.automod_honeypot_server.callback(self.auto, ctx)
        self.assertIn(self.channel.mention, ctx.sent[0]["embed"].description)
        await self.auto.automod_honeypot_remove.callback(self.auto, ctx, channels=self.channel.mention)
        self.assertEqual(self.settings("automod")["honeypot"]["channels"], [])
        await self.auto.automod_honeypot_add.callback(self.auto, ctx, channels=self.channel.mention)
        await self.auto.automod_honeypot_clear.callback(self.auto, ctx)
        self.assertEqual(self.settings("automod")["honeypot"]["channels"], [])


class ImportTest(AutomodBase):
    def export(self, **settings):
        base = {
            "attachment_punishment": None, "attachmentspam": None, "bad_words": [], "caps_punishment": [1, 0], "caps_threshold": None, "channel_wl": [], "council_channel": None,
            "delete_files": False, "disabled_features": 0, "guild_id": 111222333444555666, "honeypot_channels": [], "honeypot_punishment": [], "invite_bl": [], "invite_punishment": [1, 0],
            "invite_setting": 0, "invite_slowmode": None, "invite_wl": [], "invitespam_enabled": False, "isInviteSpamEnabled": False, "isNoroleModeEnabled": False, "link_bl": [],
            "link_punishment": [8, 0], "link_setting": 0, "link_slowmode": [1, 1], "link_wl": [], "log_channel": None, "media_channels": [], "mentionspam": [10, 1],
            "mentionspam_punishment": [65, 0], "role_wl": [], "slowmode": None, "slowmode_punishment": [1, 0], "warn_punishment": None, "warn_threshold": None, "word_punishment": [129, 0],
        }
        return {"is_vip": 0, "muterole": str(self.role_muted.id), "settings": {**base, **settings}}

    def form(self, **settings):
        channels = {c.id for c in self.guild.channels}
        roles = {r.id for r in self.guild.roles}
        form = settings_from_export(self.export(**settings), channels, roles)
        return Automod.schema.clean(form)

    def test_a_mostly_quiet_server(self):
        form = self.form()
        self.assertEqual((form["mentions"]["enabled"], form["mentions"]["rate"], form["mentions"]["punishment"]["actions"]), (True, [10, 1], DELETE | BAN))
        self.assertEqual((form["links"]["enabled"], form["links"]["mode"], form["links"]["rate"]), (True, "blacklist", [1, 1]))
        for name in ("invites", "words", "caps", "attachments", "spam", "honeypot", "warns"):
            self.assertFalse(form[name]["enabled"], name)
        self.assertEqual(form["words"]["punishment"]["actions"], DELETE | DRAMA)
        self.assertEqual(form["words"]["punishment"]["purge_days"], 1)

    def test_everything_set_up(self):
        form = self.form(
            bad_words=["boat", "car"], caps_threshold=70, channel_wl=[str(self.channel.id), "900000000000006999"], role_wl=[str(self.role_mod.id)], council_channel=str(self.drama.id),
            log_channel=str(self.log_channel.id), delete_files=True, honeypot_channels=[str(self.channel.id)], honeypot_punishment=[64, 0, 3], invite_wl=["https://discord.gg/abc"],
            isInviteSpamEnabled=True, isNoroleModeEnabled=True, link_bl=["reddit.com", "not a site"], link_setting=1, link_wl=["example.com"], media_channels=[str(self.drama.id)], slowmode=[5, 25],
            slowmode_punishment=[4, 600], attachmentspam=[3, 5], attachment_punishment=[1024, 3600], warn_threshold=3, warn_punishment=[16, 0],
        )
        self.assertEqual(form["log_channel"], self.log_channel.id)
        self.assertEqual(form["drama_channel"], self.drama.id)
        self.assertEqual(form["allowed_channels"], [self.channel.id])
        self.assertEqual(form["allowed_roles"], [self.role_mod.id])
        self.assertEqual(form["media_channels"], [self.drama.id])
        self.assertTrue(form["delete_files"])
        self.assertEqual([e["text"] for e in form["words"]["entries"]], ["boat", "car"])
        self.assertTrue(form["words"]["enabled"])
        self.assertEqual((form["caps"]["enabled"], form["caps"]["threshold"]), (True, 70))
        self.assertEqual((form["honeypot"]["enabled"], form["honeypot"]["punishment"]["purge_days"]), (True, 3))
        self.assertEqual((form["invites"]["enabled"], form["invites"]["no_role"], form["invites"]["whitelist"]), (True, True, ["abc"]))
        self.assertEqual((form["links"]["mode"], form["links"]["blacklist"], form["links"]["whitelist"]), ("whitelist", ["reddit.com"], ["example.com"]))
        self.assertEqual((form["spam"]["enabled"], form["spam"]["rate"], form["spam"]["punishment"]), (True, [5, 25], {"actions": TEMPMUTE, "seconds": 600, "purge_days": 1}))
        self.assertEqual(form["attachments"]["punishment"]["seconds"], 3600)
        self.assertEqual((form["warns"]["enabled"], form["warns"]["threshold"], form["warns"]["punishment"]["actions"]), (True, 3, KICK))

    def test_a_rule_switched_off_there_stays_off(self):
        form = self.form(disabled_features=4 | 64, bad_words=["boat"])
        self.assertFalse(form["mentions"]["enabled"])
        self.assertFalse(form["links"]["enabled"])
        self.assertTrue(form["words"]["enabled"])

    def test_a_punishment_that_cannot_work_turns_the_rule_off(self):
        form = self.form(slowmode=[1, 1], slowmode_punishment=[4, 0])
        self.assertFalse(form["spam"]["enabled"])

    async def test_the_endpoint_saves_both_and_is_scoped(self):
        self.bot.ops.describe_guild = lambda guild_id: {"channels": [{"id": c.id} for c in self.guild.channels], "roles": [{"id": r.id} for r in self.guild.roles]}
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": None})
        client = await self.client()
        response = await client.post(self.path("/automod/import"), json={"code": json.dumps(self.export(bad_words=["boat"]))})
        self.assertEqual(response.status, 200)
        self.assertTrue(self.settings("automod")["words"]["enabled"])
        self.assertEqual(self.settings("moderation")["mute_role"], self.role_muted.id)
        self.assertEqual((await client.post(self.path("/automod/import"), json={"code": "nope"})).status, 400)
        staff = await self.client(access="staff")
        self.assertEqual((await staff.post(self.path("/automod/import"), json={"code": "{}"})).status, 403)
        # A role from elsewhere doesn't become the mute role.
        export = self.export()
        export["muterole"] = "900000000000006777"
        await client.post(self.path("/automod/import"), json={"code": json.dumps(export)})
        self.assertEqual(self.settings("moderation")["mute_role"], self.role_muted.id)


class DashboardTest(AutomodBase):
    async def test_defaults_are_off(self):
        client = await self.client(access="staff")
        body = await (await client.get(self.path("/settings/automod"))).json()
        for name in ("invites", "mentions", "attachments", "caps", "honeypot", "links", "words", "spam", "warns"):
            self.assertFalse(body[name]["enabled"], name)
            self.assertFalse(body[name]["dry_run"], name)
        await self.bot.settings.save(self.guild.id, "automod", {"log_channel": None})
        self.assertFalse((await self.say("https://discord.gg/abc")).deleted)

    async def test_staff_cannot_save(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.put(self.path("/settings/automod"), json={"delete_files": True})).status, 403)

    async def test_full_save_round_trip_and_ids_as_text(self):
        client = await self.client()
        form = {
            "allowed_roles": [str(self.role_mod.id)],
            "links": {"enabled": True, "mode": "whitelist", "whitelist": ["https://www.Example.com/"], "rate": [1, 1], "exempt_channels": [str(self.channel.id)]},
            "words": {"enabled": True, "entries": ["boat", {"text": "car", "kind": "word"}], "dry_run": True},
            "invites": {"whitelist": ["https://discord.gg/abc", "111222333444555666"]},
        }
        response = await client.put(self.path("/settings/automod"), json=form)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["allowed_roles"], [str(self.role_mod.id)])
        self.assertEqual(body["links"]["whitelist"], ["example.com"])
        self.assertEqual(body["words"]["entries"][0], {"text": "boat", "kind": "substring"})
        self.assertEqual(body["invites"]["whitelist"], ["abc", "111222333444555666"])
        self.assertEqual(self.settings("automod")["allowed_roles"], [self.role_mod.id])

    async def test_bad_values_are_refused_with_their_field(self):
        client = await self.client()
        for form, field in (
            ({"links": {"blacklist": ["not a site"]}}, "links.blacklist"), ({"links": {"mode": "x"}}, "links.mode"), ({"caps": {"threshold": 101}}, "caps.threshold"),
            ({"invites": {"whitelist": ["not an invite!"]}}, "invites.whitelist"), ({"log_channel": "x"}, "log_channel"), ({"nonsense": 1}, "nonsense"),
        ):
            response = await client.put(self.path("/settings/automod"), json=form)
            self.assertEqual(response.status, 400, form)
            self.assertIn(field, (await response.json())["fields"], form)

    async def test_saving_new_rules_takes_effect_at_once(self):
        client = await self.client()
        await client.put(self.path("/settings/automod"), json={"words": {"enabled": True, "entries": ["first"]}})
        self.assertTrue((await self.say("first")).deleted)
        await client.put(self.path("/settings/automod"), json={"words": {"entries": ["second"]}})
        self.assertFalse((await self.say("first")).deleted)
        self.assertTrue((await self.say("second")).deleted)
