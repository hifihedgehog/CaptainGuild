"""What automod reads through and who it leaves alone: disguised links and invites, punctuation in a bad word, staff, and commands that only change a setting."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

import discord

from captainguild.core.validate import BAN, DELETE
from captainguild.modules.automod import fold, hosts_in, invite_entry
from tests.fakes import FakeChannel, FakeMember
from tests.test_automod import AutomodBase
from tests.test_moderation_live import LiveBase


class FoldTest(unittest.TestCase):
    def test_lookalike_letters_read_as_latin(self):
        self.assertEqual(fold("b\u043eat"), "boat")  # a Cyrillic o
        self.assertEqual(fold("\u0412\u041e\u0410\u0422"), "boat")  # all four, in capitals
        self.assertEqual(fold("\u03b2\u03bf\u03b1\u03c4"), "boat")  # Greek
        self.assertEqual(fold("h\u0131\u0142\u00f8"), "hilo")  # Latin letters that have no accent to strip

    def test_invisible_characters_never_count(self):
        self.assertEqual(fold("bo\u200bat"), "boat")
        self.assertEqual(fold("bo\u200bat", keep_punctuation=True), "boat")
        self.assertEqual(fold("a\u200d$$", keep_punctuation=True), "a$$")

    def test_a_host_named_before_an_at_counts_after_the_real_one(self):
        self.assertEqual(hosts_in("https://youtube.com@evil.net/x"), [("evil.net", "/x"), ("youtube.com", "/x")])
        self.assertEqual(hosts_in("https://user:secret@example.com/x"), [("example.com", "/x")])

    def test_a_host_reads_the_way_a_browser_reads_it(self):
        self.assertEqual(hosts_in("https://www.evil.com./x"), [("evil.com", "/x")])
        self.assertEqual(hosts_in("https://\uff45\uff56\uff49\uff4c.com/x"), [("evil.com", "/x")])

    def test_new_invite_sites_can_be_whitelisted(self):
        self.assertEqual(invite_entry("https://discord.li/AbC"), "AbC")
        self.assertEqual(invite_entry("disboard.org/server/join/AbC"), "AbC")


class PunctuatedWordTest(AutomodBase):
    async def test_an_entry_keeps_its_punctuation(self):
        await self.rule("words", entries=["a$$", "@ss", "s#it", "f*ck", "c++"])
        for clean in ("what a nice print", "hello all", "class passed the message", "visit my website", "fck", "see you at 9"):
            self.assertFalse((await self.say(clean)).deleted, clean)
        for bad in ("you a$$", "@ss", "S#IT", "what the f*ck", "I write C++"):
            self.assertTrue((await self.say(bad)).deleted, bad)

    async def test_a_plain_entry_still_ignores_punctuation_in_the_message(self):
        await self.rule("words", entries=["boat"])
        self.assertTrue((await self.say("b.o.a.t")).deleted)
        self.assertTrue((await self.say("b\u043e.a.t")).deleted)
        self.assertFalse((await self.say("what a nice print")).deleted)

    async def test_an_entry_with_nothing_left_after_folding_matches_nothing(self):
        await self.rule("words", entries=["\u200b", {"text": "\u0301", "kind": "word"}, {"text": "\u200b", "kind": "word"}])
        for text in ("hello", "", ". . .", "  spaced  out  "):
            self.assertFalse((await self.say(text)).deleted, text)

    async def test_an_emoji_entry_still_works(self):
        await self.rule("words", entries=["\N{FIRE}"])
        self.assertTrue((await self.say("this is \N{FIRE}")).deleted)
        self.assertFalse((await self.say("this is fine")).deleted)

    async def test_a_whole_word_entry_sees_through_an_invisible_character(self):
        await self.rule("words", entries=[{"text": "boat", "kind": "word"}])
        self.assertTrue((await self.say("my bo\u200bat")).deleted)
        self.assertFalse((await self.say("fredboat")).deleted)


class DisguisedInviteTest(AutomodBase):
    async def test_each_form_reaches_the_rule(self):
        await self.rule("invites")
        self.foreign_invite()
        for text in (
            "https://discord.gg//spam", "https://discord.gg/#/spam", "https://discord.gg\\spam", "https://discord.com//invite/spam", "https://discord.com\\invite\\spam",
            "https:\\\\discord.gg\\spam", "https://discord.gg%2Fspam", "discord.gg/%73pam", "discord\\.gg/spam", "https:\\/\\/discord\\.gg\\/spam", "dsc\\.gg/spam", "https://discord.li/spam", "https://disboard.org/server/join/spam", "HTTPS://DISCORD.GG//spam",
        ):
            message = await self.say(f"join {text}")
            self.assertTrue(message.deleted, text)

    async def test_the_code_looked_up_is_the_real_one(self):
        await self.rule("invites")
        self.foreign_invite()
        await self.say("https://discord.com//invite//AbC")
        self.fetch_invite.assert_awaited()
        self.assertEqual(self.fetch_invite.await_args.args[0], "AbC")

    async def test_a_code_in_italics_is_still_the_code(self):
        await self.rule("invites")
        self.foreign_invite()
        await self.say("_discord.gg/AbC_")
        self.assertEqual(self.fetch_invite.await_args.args[0], "AbC")

    async def test_ordinary_discord_links_are_not_invites(self):
        await self.rule("invites")
        self.foreign_invite()
        for text in ("https://discord.com/channels/1/2/3", "discord.gg", "the invite is in discord", "https://discord.com/developers/docs"):
            self.assertFalse((await self.say(text)).deleted, text)
        self.fetch_invite.assert_not_awaited()

    async def test_the_whitelist_still_applies_to_a_disguised_form(self):
        await self.rule("invites", whitelist=["friend"])
        self.foreign_invite()
        self.assertFalse((await self.say("https://discord.gg//friend")).deleted)


class DisguisedLinkTest(AutomodBase):
    async def test_the_blacklist_reads_through_them(self):
        await self.rule("links", mode="blacklist", blacklist=["evil.com"], rate=[1, 1])
        for text in (
            "https://evil.com/x", "https://evil.com./x", "https://evil\\.com/x", "https://ev%69l.com/x", "https:\\/\\/evil.com/x", "https://\uff45\uff56\uff49\uff4c.com/x", "HTTPS://EVIL.COM/x",
            "https://user@evil.com/x", "https://evil.com@good.org/x",
        ):
            self.assertTrue((await self.say(text)).deleted, text)
        for text in ("https://good.org/x", "https://notevil.com/x", "https://evil.com.good.org/x"):
            self.assertFalse((await self.say(text)).deleted, text)

    async def test_the_whitelist_reads_through_them(self):
        await self.rule("links", mode="whitelist", whitelist=["example.com"], rate=[1, 1])
        for text in ("https://evil.com/x", "https:\\/\\/evil.com/x", "https://evil.net\\@example.com/x", "https://ev%69l.com/x"):
            self.assertTrue((await self.say(text)).deleted, text)
        for text in ("https://example.com/x", "https://www.example.com./x", "https://ex%61mple.com/x", "no link here, 100% of it"):
            self.assertFalse((await self.say(text)).deleted, text)


class StaffImmunityTest(AutomodBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.rule("words", entries=["boat"])

    def staff(self, *permissions: str) -> Any:
        return FakeMember(self.guild, "staff", self.guild.role("Staff", 6, *permissions))

    async def test_ban_and_kick_permissions_are_immune(self):
        self.assertFalse((await self.say("boat", author=self.staff("ban_members"))).deleted)
        self.assertFalse((await self.say("boat", author=self.staff("kick_members"))).deleted)

    async def test_the_bot_manager_role_is_immune(self):
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.role_mod.id})
        self.assertFalse((await self.say("boat", author=self.mod)).deleted)

    async def test_other_staff_permissions_are_not(self):
        self.assertTrue((await self.say("boat", author=self.staff("moderate_members"))).deleted)

    async def test_someone_who_only_shares_a_rule_with_the_bot_manager_is_not(self):
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.role_admin.id})
        self.assertTrue((await self.say("boat")).deleted)

    async def test_a_moderator_who_pings_many_people_is_not_banned(self):
        moderator = self.staff("ban_members")
        await self.rule("mentions", rate=[10, 1], punishment={"actions": DELETE | BAN, "seconds": 0, "purge_days": 1})
        await self.say("staff meeting", author=moderator, mentions=[SimpleNamespace(id=index) for index in range(10)])
        self.assertEqual(self.guild.banned, [])
        await self.say("join my server", mentions=[SimpleNamespace(id=index) for index in range(10)])
        self.assertIn(self.member.id, self.guild.banned)

    async def test_the_warn_threshold_leaves_staff_alone_too(self):
        moderator = self.staff("kick_members")
        await self.bot.settings.save(self.guild.id, "automod", {"warns": {"enabled": True, "threshold": 1, "punishment": {"actions": BAN, "seconds": 0, "purge_days": 0}}})
        for _ in range(3):
            await self.mods.record_case(self.guild, "warn", moderator, self.owner)
        self.assertEqual(self.guild.banned, [])


class RuleSwitchTest(AutomodBase):
    async def test_a_punishment_alone_leaves_a_rule_off(self):
        for command, name in (
            (self.auto.automod_slowmode_punishment, "spam"), (self.auto.automod_attachmentspam_punishment, "attachments"), (self.auto.automod_mentionspam_punishment, "mentions"),
            (self.auto.automod_linkspam_punishment, "links"), (self.auto.automod_invitespam_punishment, "invites"), (self.auto.automod_caps_punishment, "caps"),
            (self.auto.automod_censor_punishment, "words"), (self.auto.automod_honeypot_punishment, "honeypot"), (self.auto.automod_warnpunish, "warns"),
        ):
            ctx = self.context(self.admin)
            await command.callback(self.auto, ctx, punishments="ban")
            rule = self.settings("automod")[name]
            self.assertFalse(rule["enabled"], name)
            self.assertEqual(rule["punishment"]["actions"], BAN, name)
            self.assertIn("still off", ctx.oks[0], name)

    async def test_it_does_nothing_until_a_rate_is_set(self):
        ctx = self.context(self.admin)
        await self.auto.automod_slowmode_punishment.callback(self.auto, ctx, punishments="ban")
        for index in range(8):
            await self.say(f"line {index}")
        self.assertEqual(self.guild.banned, [])
        await self.auto.automod_slowmode_set.callback(self.auto, self.context(self.admin), rate="5 5")
        for index in range(6):
            await self.say(f"line {index}")
        self.assertIn(self.member.id, self.guild.banned)

    async def test_a_rule_already_on_stays_on(self):
        await self.rule("spam", rate=[5, 5])
        ctx = self.context(self.admin)
        await self.auto.automod_slowmode_punishment.callback(self.auto, ctx, punishments="delete")
        self.assertTrue(self.settings("automod")["spam"]["enabled"])
        self.assertNotIn("still off", ctx.oks[0])

    async def test_removing_a_word_leaves_a_rule_off(self):
        await self.bot.settings.save(self.guild.id, "automod", {"words": {"enabled": False, "entries": ["boat", "car"]}})
        await self.auto.automod_censor_words.callback(self.auto, self.context(self.admin), "remove", words="car")
        self.assertFalse(self.settings("automod")["words"]["enabled"])
        self.assertEqual(len(self.settings("automod")["words"]["entries"]), 1)

    async def test_adding_a_word_turns_it_on(self):
        await self.auto.automod_censor_words.callback(self.auto, self.context(self.admin), "add", words="boat")
        self.assertTrue(self.settings("automod")["words"]["enabled"])

    async def test_the_command_called_off_does_not_turn_links_on(self):
        await self.auto.automod_linkspam_off.callback(self.auto, self.context(self.admin))
        links = self.settings("automod")["links"]
        self.assertEqual((links["enabled"], links["mode"]), (False, "blacklist"))
        await self.auto.automod_linkspam_block.callback(self.auto, self.context(self.admin))
        self.assertTrue(self.settings("automod")["links"]["enabled"])


class MediaThreadTest(AutomodBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.gallery = FakeChannel(self.guild, "gallery")
        self.thread = FakeChannel(self.guild, "thread", discord.ChannelType.public_thread)
        self.thread.parent = self.gallery
        await self.bot.settings.save(self.guild.id, "automod", {"media_channels": [self.gallery.id]})

    async def test_talk_in_a_thread_stays(self):
        self.assertFalse((await self.say("nice print, what filament?", channel=self.thread)).deleted)
        self.assertTrue((await self.say("nice print, what filament?", channel=self.gallery)).deleted)

    async def test_a_listed_category_still_covers_its_channels(self):
        category = FakeChannel(self.guild, "photos", discord.ChannelType.category)
        room = FakeChannel(self.guild, "room", category=category)
        await self.bot.settings.save(self.guild.id, "automod", {"media_channels": [category.id]})
        self.assertTrue((await self.say("just talking", channel=room)).deleted)

    async def test_unsafe_files_in_a_thread_still_go(self):
        await self.bot.settings.save(self.guild.id, "automod", {"delete_files": True})
        message = await self.say("here", channel=self.thread, attachments=[SimpleNamespace(filename="virus.exe")])
        self.assertTrue(message.deleted)


class ShortcutTargetTest(LiveBase):
    async def test_capslimit_sets_the_limit(self):
        await self.typed("!capslimit 40")
        caps = self.bot.settings.get(self.guild_id, "automod")["caps"]
        self.assertEqual((caps["threshold"], caps["enabled"]), (40, True))

    async def test_capspunish_sets_the_punishment(self):
        await self.typed("!capspunish delete, warn")
        caps = self.bot.settings.get(self.guild_id, "automod")["caps"]
        self.assertEqual(caps["punishment"]["actions"], 3)
