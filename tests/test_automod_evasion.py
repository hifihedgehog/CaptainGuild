"""Text that reads one way to people and another to a filter: disguised hosts, spaced letters, polls, thread titles and patterns."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import mock

import discord

from captainguild.core.validate import BAN, DELETE, DM_OFFENDER, KICK, TEMPMUTE, WARN
from captainguild.modules.automod import Automod, fold, hosts_in, split_words
from captainguild.modules.logs import Logs
from captainguild.modules.moderation import Moderation
from tests.fakes import FakeChannel, FakeMember, _FakeResponse, new_id
from tests.test_automod import AutomodBase

DELETE_ONLY = {"actions": DELETE, "seconds": 0, "purge_days": 0}


class SplitWordsTest(AutomodBase):
    def test_backslashes_stay(self):
        self.assertEqual(split_words(r"regex:\bboat\b"), [r"regex:\bboat\b"])

    def test_quotes_still_group(self):
        self.assertEqual(split_words('"two words" one, #channel'), ["two words", "one", "#channel"])

    def test_an_open_quote_falls_back_to_spaces(self):
        self.assertEqual(split_words('"two words'), ['"two', "words"])

    async def test_a_pattern_is_stored_as_typed_and_the_reply_shows_it(self):
        ctx = self.context(self.admin)
        await self.auto.automod_censor_words.callback(self.auto, ctx, "add", words=r"regex:\bboat\b")
        self.assertEqual(self.settings("automod")["words"]["entries"], [{"text": r"\bboat\b", "kind": "regex"}])
        self.assertIn(r"regex:\bboat\b", ctx.oks[0])


class RegexReadsFoldedTextTest(AutomodBase):
    async def test_a_pattern_sees_past_accents_lookalikes_and_invisible_characters(self):
        await self.rule("words", entries=[{"text": "b[o0]at", "kind": "regex"}], punishment=DELETE_ONLY)
        for text in ("boat", "B0AT", "b\u00f3at", "b\u043eat", "bo\u200bat"):
            self.assertTrue((await self.say(text)).deleted, ascii(text))

    async def test_it_still_reads_the_raw_text(self):
        await self.rule("words", entries=[{"text": "a\\.b", "kind": "regex"}], punishment=DELETE_ONLY)
        self.assertTrue((await self.say("a.b")).deleted)

    async def test_plain_ascii_is_asked_once(self):
        await self.rule("words", entries=[{"text": "zzz", "kind": "regex"}], punishment=DELETE_ONLY)
        with mock.patch.object(self.auto.regex, "search", wraps=self.auto.regex.search) as search:
            await self.say("hello there")
        self.assertEqual(search.await_count, 1)


class FoldEvasionTest(AutomodBase):
    def test_blank_letters_go(self):
        for filler in ("\u3164", "\u115f", "\u1160", "\uffa0", "\u2800"):
            self.assertEqual(fold(f"bo{filler}at"), "boat", ascii(filler))

    def test_regional_indicators_read_as_letters(self):
        self.assertEqual(fold("\U0001F1E7\U0001F1F4\U0001F1E6\U0001F1F9"), "boat")

    async def test_markup_doesnt_hide_a_whole_word(self):
        await self.rule("words", entries=[{"text": "ship", "kind": "word"}], punishment=DELETE_ONLY)
        for text in ("_ship_", "__ship__", "sh__ip", "*ship*", "~~ship~~", "||ship||", "`ship`"):
            self.assertTrue((await self.say(text)).deleted, text)
        self.assertFalse((await self.say("shipment")).deleted)

    async def test_an_entry_holding_markup_still_matches(self):
        await self.rule("words", entries=[{"text": "a_b", "kind": "word"}], punishment=DELETE_ONLY)
        self.assertTrue((await self.say("a_b")).deleted)

    async def test_spaced_letters_match_both_lists(self):
        await self.rule("words", entries=["boat", {"text": "ship", "kind": "word"}], punishment=DELETE_ONLY)
        for text in ("b o a t", "s h i p", "s\nh\ni\np", "look b o a t now"):
            self.assertTrue((await self.say(text)).deleted, text)

    async def test_ordinary_sentences_with_short_words_pass(self):
        await self.rule("words", entries=["boat", {"text": "ship", "kind": "word"}], punishment=DELETE_ONLY)
        for text in ("a boa t", "is a b or an a", "I s h"):
            self.assertFalse((await self.say(text)).deleted, text)

    def test_weak_lookalikes_count_inside_a_latin_word(self):
        self.assertEqual(fold("\u0433ed"), "red")
        self.assertEqual(fold("b\u03c3at"), "boat")
        self.assertIn("boat", fold("b\u044coat"))

    def test_weak_lookalikes_leave_other_alphabets_alone(self):
        self.assertNotEqual(fold("\u0433\u043e\u0434"), "rod")

    async def test_a_short_english_entry_doesnt_catch_a_russian_word(self):
        await self.rule("words", entries=["pot", {"text": "hot", "kind": "word"}], punishment=DELETE_ONLY)
        for text in ("\u0440\u043e\u0442", "\u043d\u043e\u0442"):
            self.assertFalse((await self.say(text)).deleted, ascii(text))

    async def test_a_mixed_word_still_reads_as_latin(self):
        await self.rule("words", entries=["pot"], punishment=DELETE_ONLY)
        self.assertTrue((await self.say("\u0440ot")).deleted)

    async def test_a_long_whole_script_spelling_still_counts(self):
        await self.rule("words", entries=["boats"], punishment=DELETE_ONLY)
        self.assertTrue((await self.say("\u0412\u041e\u0410\u0422\u0405")).deleted)

    async def test_an_entry_in_another_alphabet_still_matches_when_short(self):
        await self.rule("words", entries=["\u0445\u0443\u0439"], punishment=DELETE_ONLY)
        self.assertTrue((await self.say("\u0445\u0443\u0439")).deleted)


class DisguisedHostTest(AutomodBase):
    FORMS = ("https://discord\uff0egg/abc", "https://\uff44iscord.gg/abc", "https://discord\u3002gg/abc", "https://dis\xadcord.gg/abc", "https://discord\uff61gg/abc")

    async def test_an_invite_in_a_disguise_counts(self):
        await self.rule("invites", punishment=DELETE_ONLY)
        self.foreign_invite()
        for text in self.FORMS:
            self.assertTrue((await self.say(text)).deleted, ascii(text))

    def test_a_host_reads_the_way_a_browser_reads_it(self):
        for text in ("https://evil\u3002com/x", "https://ev\xadil.com/x", "https://evil\uff0ecom/x", "https://ev\u200bil.com/x"):
            self.assertEqual(hosts_in(text)[0][0], "evil.com", ascii(text))

    def test_a_host_too_long_to_convert_still_loses_its_disguise(self):
        self.assertEqual(hosts_in("https://" + "a" * 70 + "\u3002com/x")[0][0], "a" * 70 + ".com")

    def test_a_host_it_cant_convert_keeps_its_letters(self):
        self.assertEqual(hosts_in("https://m\u00fcnchen.de/x")[0][0], "m\u00fcnchen.de")

    async def test_a_blacklisted_host_in_a_disguise_counts(self):
        await self.rule("links", mode="blacklist", blacklist=["evil.com"], punishment=DELETE_ONLY)
        self.assertTrue((await self.say("https://evil\u3002com/x")).deleted)

    async def test_the_old_invite_site_with_a_php_path_counts(self):
        await self.rule("invites", punishment=DELETE_ONLY)
        self.assertTrue((await self.say("https://discordy.com/server.php?id=1")).deleted)


class WhitelistBareHostTest(AutomodBase):
    async def test_a_www_address_with_no_scheme_counts(self):
        await self.rule("links", mode="whitelist", whitelist=["good.net"], punishment=DELETE_ONLY)
        self.assertTrue((await self.say("go to www.evil.net/x")).deleted)
        self.assertFalse((await self.say("go to www.good.net/x")).deleted)

    async def test_no_role_mode_counts_it_too(self):
        bare = FakeMember(self.guild, "bare")
        await self.rule("links", mode="no_role", whitelist=[], punishment=DELETE_ONLY)
        self.assertTrue((await self.say("www.evil.net", author=bare)).deleted)
        self.assertFalse((await self.say("www.evil.net")).deleted)

    async def test_ordinary_text_with_www_passes(self):
        await self.rule("links", mode="whitelist", whitelist=[], punishment=DELETE_ONLY)
        self.assertFalse((await self.say("the www is down")).deleted)


class InviteFloodTest(AutomodBase):
    async def test_one_message_spends_three_lookups(self):
        from discord import NotFound
        await self.rule("invites", punishment=DELETE_ONLY)
        self.fetch_invite.side_effect = NotFound(_FakeResponse(404), "Unknown Invite")
        codes = " ".join(f"discord.gg/code{index}" for index in range(100))
        message = await self.say(codes)
        self.assertEqual(self.fetch_invite.await_count, 3)
        self.assertTrue(message.deleted)

    async def test_the_same_code_repeated_is_one_lookup(self):
        from discord import NotFound
        await self.rule("invites", punishment=DELETE_ONLY)
        self.fetch_invite.side_effect = NotFound(_FakeResponse(404), "Unknown Invite")
        message = await self.say(" ".join(["discord.gg/same"] * 30))
        self.assertEqual(self.fetch_invite.await_count, 1)
        self.assertFalse(message.deleted)

    async def test_known_codes_dont_use_the_allowance(self):
        from discord import NotFound
        await self.rule("invites", punishment=DELETE_ONLY)
        self.fetch_invite.side_effect = NotFound(_FakeResponse(404), "Unknown Invite")
        await self.say("discord.gg/one discord.gg/two discord.gg/three discord.gg/four")
        self.fetch_invite.reset_mock()
        message = self.message("discord.gg/two discord.gg/three discord.gg/four discord.gg/five")
        await self.auto._foreign_invite(self.guild, message.content, self.auto._rules(self.guild.id))
        self.assertLessEqual(self.fetch_invite.await_count, 2)


class PollsAndForwardsTest(AutomodBase):
    def with_poll(self, question: str, *answers: str) -> Any:
        message = self.message("")
        message.poll = SimpleNamespace(question=question, answers=[SimpleNamespace(text=answer) for answer in answers])
        return message

    async def test_a_poll_question_counts(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        message = self.with_poll("boat?", "yes", "no")
        await self.auto.on_message(message)
        self.assertTrue(message.deleted)

    async def test_a_poll_answer_counts(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        message = self.with_poll("which?", "boat", "no")
        await self.auto.on_message(message)
        self.assertTrue(message.deleted)

    async def test_a_forwarded_embed_counts(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        message = self.message("")
        message.message_snapshots = [SimpleNamespace(content="", embeds=[discord.Embed(title="a boat", description="x")], attachments=[])]
        await self.auto.on_message(message)
        self.assertTrue(message.deleted)


class FakeThread(FakeChannel):
    def __init__(self, guild: Any, name: str, owner: Any, parent: Any) -> None:
        super().__init__(guild, name, discord.ChannelType.public_thread)
        self.owner_id = owner.id
        self.parent = parent
        self.deleted = False

    async def delete(self, *, reason: str | None = None) -> None:
        self.deleted = True


class ThreadTitleTest(AutomodBase):
    async def test_a_bad_title_deletes_the_thread(self):
        await self.rule("words", entries=["boat"], punishment={"actions": DELETE | WARN, "seconds": 0, "purge_days": 0})
        thread = FakeThread(self.guild, "boat sale", self.member, self.channel)
        await self.auto.on_thread_create(thread)
        self.assertTrue(thread.deleted)
        self.assertEqual((await self.cases())[0]["action"], "warn")
        self.assertIn("Thread title", self.logged()[0]["description"])

    async def test_an_invite_in_a_title_counts(self):
        await self.rule("invites", punishment=DELETE_ONLY)
        self.foreign_invite()
        thread = FakeThread(self.guild, "join discord.gg/abc", self.member, self.channel)
        await self.auto.on_thread_create(thread)
        self.assertTrue(thread.deleted)

    async def test_a_clean_title_stays(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        thread = FakeThread(self.guild, "hello", self.member, self.channel)
        await self.auto.on_thread_create(thread)
        self.assertFalse(thread.deleted)

    async def test_a_rule_without_delete_keeps_the_thread(self):
        await self.rule("words", entries=["boat"], punishment={"actions": WARN, "seconds": 0, "purge_days": 0})
        thread = FakeThread(self.guild, "boat", self.member, self.channel)
        await self.auto.on_thread_create(thread)
        self.assertFalse(thread.deleted)

    async def test_dry_run_keeps_the_thread(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY, dry_run=True)
        thread = FakeThread(self.guild, "boat", self.member, self.channel)
        await self.auto.on_thread_create(thread)
        self.assertFalse(thread.deleted)

    async def test_staff_are_left_alone(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        thread = FakeThread(self.guild, "boat", self.admin, self.channel)
        await self.auto.on_thread_create(thread)
        self.assertFalse(thread.deleted)


class NoMessageContentTest(AutomodBase):
    async def test_a_media_channel_stands_down(self):
        await self.bot.settings.save(self.guild.id, "automod", {"media_channels": [self.channel.id]})
        self.bot.reads_message_text = False
        self.assertFalse((await self.say("")).deleted)

    async def test_with_the_intent_it_still_deletes(self):
        await self.bot.settings.save(self.guild.id, "automod", {"media_channels": [self.channel.id]})
        self.bot.reads_message_text = True
        self.assertTrue((await self.say("just talk")).deleted)


class RepeatLogTest(AutomodBase):
    async def test_a_repeat_hit_is_logged_with_no_new_punishment(self):
        await self.rule("words", entries=["boat"], punishment={"actions": DELETE | WARN, "seconds": 0, "purge_days": 0})
        await self.say("boat")
        await self.say("boat again")
        self.assertEqual(len(await self.cases()), 1)
        self.assertEqual(len(self.logged()), 2)
        self.assertIn("No new punishment", self.logged()[1]["description"])
        self.assertIn("boat again", self.logged()[1]["fields"][0]["value"])

    async def test_a_rate_rule_stays_quiet(self):
        await self.rule("spam", rate=[1, 60], punishment={"actions": DELETE, "seconds": 0, "purge_days": 0})
        await self.say("one")
        await self.say("two")
        await self.say("three")
        await self.say("four")
        self.assertEqual(len(self.logged()), 1)


class LogEntryTest(AutomodBase):
    async def test_an_edit_says_so(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        message = self.message("boat")
        await self.auto._scan(message, self.member, edited=True)
        self.assertIn("edited", self.logged()[0]["title"].lower())

    async def test_it_carries_the_message_id(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        message = await self.say("boat")
        self.assertIn(str(message.id), self.logged()[0]["footer"]["text"])

    async def test_an_image_shows(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        picture = SimpleNamespace(filename="a.png", url="https://cdn.example/a.png", content_type="image/png")
        await self.say("boat", attachments=[picture])
        self.assertEqual(self.logged()[0]["image"]["url"], "https://cdn.example/a.png")

    async def test_long_text_comes_as_a_file(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        await self.say("boat " + "x" * 3000)
        posted = [entry for channel_id, entry, _ in self.bot.logs.posted]
        files = [item for channel_id, entry, item in self.bot.logs.posted if item]
        self.assertTrue(posted)
        self.assertEqual(len(files), 1)

    async def test_short_text_has_no_file(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        await self.say("boat")
        self.assertEqual([item for channel_id, entry, item in self.bot.logs.posted if item], [])

    async def test_an_oversized_invite_doesnt_lose_the_entry(self):
        await self.rule("invites", punishment=DELETE_ONLY)
        self.foreign_invite()
        await self.say("https://discord.gg/" + "a" * 3970)
        self.assertLessEqual(len(self.logged()[0]["description"]), 4096)
        self.assertIn("Invite", self.logged()[0]["description"])
        self.assertLessEqual(len(self.logged()[0]["description"]), 800)


class ManageMessagesImmuneTest(AutomodBase):
    async def test_a_cleanup_helper_isnt_filtered(self):
        helper = FakeMember(self.guild, "helper", self.role_mod)
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        self.assertFalse((await self.say("boat", author=helper)).deleted)


class WarnThresholdRolesTest(AutomodBase):
    async def test_an_allowed_role_is_left_alone(self):
        await self.bot.settings.save(self.guild.id, "automod", {"allowed_roles": [self.role_member.id], "warns": {"enabled": True, "threshold": 1, "punishment": {"actions": KICK, "seconds": 0, "purge_days": 0}}})
        await self.mods.record_case(self.guild, "warn", self.member, self.admin)
        await self.mods.record_case(self.guild, "warn", self.member, self.admin)
        self.assertEqual([row["action"] for row in await self.cases()], ["warn", "warn"])


class DmOrderTest(AutomodBase):
    async def test_a_refused_ban_takes_the_dm_back(self):
        await self.rule("words", entries=["boat"], punishment={"actions": BAN | DM_OFFENDER, "seconds": 0, "purge_days": 0})

        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.guild.ban = refuse
        deleted: list[int] = []

        class Sent:
            async def delete(self) -> None:
                deleted.append(1)

        async def send(*args: Any, **kwargs: Any) -> Any:
            return Sent()
        self.member.send = send
        await self.say("boat")
        self.assertEqual(deleted, [1])

    async def test_a_mute_dm_comes_after_and_says_nothing_false_when_refused(self):
        await self.rule("words", entries=["boat"], punishment={"actions": TEMPMUTE | DM_OFFENDER, "seconds": 300, "purge_days": 0})

        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.member.add_roles = refuse
        await self.say("boat")
        self.assertEqual(len(self.member.dms), 1)
        self.assertNotIn("You were muted", self.member.dms[0]["content"])

    async def test_a_mute_dm_says_so_when_it_went_through(self):
        await self.rule("words", entries=["boat"], punishment={"actions": TEMPMUTE | DM_OFFENDER, "seconds": 300, "purge_days": 0})
        await self.say("boat")
        self.assertIn("You were muted", self.member.dms[0]["content"])


class OutsideTheWindowTest(AutomodBase):
    def test_the_spam_window_keeps_only_what_it_needs(self):
        for _ in range(20):
            self.auto._tally(self.guild.id, 1, "spam", 1, 60, keep=6)
        window = self.auto._windows[(self.guild.id, 1, "spam", 60)]
        self.assertEqual(len(window), 6)

    def test_a_week_long_window_is_refused(self):
        from captainguild.core.validate import Invalid
        from captainguild.modules.automod import rate
        with self.assertRaises(Invalid):
            rate([5, 7 * 86400])
        self.assertEqual(rate([5, 86400]), [5, 86400])


class CopiesTest(AutomodBase):
    async def post_from(self, count: int, text: str) -> list[Any]:
        out = []
        for index in range(count):
            account = FakeMember(self.guild, f"bot{new_id()}", self.role_member)
            out.append(await self.say(text, author=account))
        return out

    async def test_the_same_text_across_accounts_trips_it_and_clears_the_copies(self):
        await self.rule("copies", rate=[3, 15], punishment={"actions": DELETE | WARN, "seconds": 0, "purge_days": 0})
        posts = await self.post_from(3, "free nitro at this site")
        self.assertTrue(posts[-1].deleted)
        self.assertTrue(all(post.deleted for post in posts))
        self.assertEqual(len(await self.cases()), 1)
        self.assertEqual((await self.cases())[0]["user_id"], posts[-1].author.id)

    async def test_below_the_count_nothing_happens(self):
        await self.rule("copies", rate=[3, 15], punishment=DELETE_ONLY)
        posts = await self.post_from(2, "free nitro at this site")
        self.assertFalse(any(post.deleted for post in posts))

    async def test_short_text_never_counts(self):
        await self.rule("copies", rate=[3, 15], punishment=DELETE_ONLY)
        posts = await self.post_from(6, "lol ok")
        self.assertFalse(any(post.deleted for post in posts))

    async def test_a_different_channel_counts_apart(self):
        await self.rule("copies", rate=[3, 15], punishment=DELETE_ONLY)
        other = FakeChannel(self.guild, "other")
        first = await self.say("free nitro at this site")
        second = await self.say("free nitro at this site", channel=other)
        self.assertFalse(first.deleted or second.deleted)

    async def test_dressed_up_copies_count_as_one_text(self):
        await self.rule("copies", rate=[2, 15], punishment=DELETE_ONLY)
        await self.post_from(1, "FREE nitro at this site")
        posts = await self.post_from(1, "free  n\u0456tro at this site!")
        self.assertTrue(posts[0].deleted)


class SearchPageTest(AutomodBase):
    modules = [Moderation, Automod, Logs]

    async def test_an_entry_reaches_the_search_page(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        message = await self.say("boat")
        logs = self.cog(Logs)
        await logs.flush()
        row = await self.message_db.one("SELECT event, user_id, message_id FROM log_entries WHERE event = 'automod'")
        self.assertEqual((row["user_id"], row["message_id"]), (self.member.id, message.id))

    async def test_an_edit_shows_what_it_said_before(self):
        await self.rule("words", entries=["boat"], punishment=DELETE_ONLY)
        logs = self.cog(Logs)
        message = self.message("boat")
        with mock.patch.object(logs, "stored_message", new=mock.AsyncMock(return_value={"content": "harmless"})):
            await self.auto._scan(message, self.member, edited=True)
        names = [field["name"] for field in self.logged()[0]["fields"]]
        self.assertIn("Message before", names)
        self.assertIn("Message after", names)
