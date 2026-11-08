import asyncio
import datetime
import json
import time
import types
import unicodedata
from unittest import mock

import discord

from captainguild.modules import utilities
from captainguild.modules.tags import Tags
from captainguild.modules.utilities import (
    DumpError,
    EndGiveawayView,
    GiveawayView,
    Utilities,
    build_index,
    compile_word,
    dump_line,
    highlight_tokens,
    marked_spans,
    marked_text,
    matching_words,
    parse_dump_query,
    placed_tokens,
    parse_moment,
    parse_when,
    select_members,
    split_poll,
    word_forms,
)
from tests.fakes import NOW, BotTest, FakeChannel, FakeGuild, FakeMember, FakeMessage, FakePermissions
from tests.gateway import GatewayTest, new_id

MOMENT = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)  # a Thursday


def at(month: int, day: int, hour: int = 12, minute: int = 0, year: int = 2026) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)


class ParseWhenTest(BotTest):
    def test_the_examples_people_are_told_to_use(self):
        cases = {
            "next thursday at 3pm do something funny": (at(11, 12, 15), "do something funny"),
            "do the dishes tomorrow": (at(11, 6), "do the dishes"),
            "in 3 days do the thing": (at(11, 8), "do the thing"),
            "2d unmute someone": (at(11, 7), "unmute someone"),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_when(text, MOMENT), expected, text)

    def test_times_of_day_are_utc_and_roll_to_the_next_time_the_clock_shows_them(self):
        self.assertEqual(parse_moment("at noon", at(11, 5, 9)), at(11, 5, 12))
        self.assertEqual(parse_moment("at noon", at(11, 5, 13)), at(11, 6, 12))
        self.assertEqual(parse_moment("midnight", MOMENT), at(11, 6, 0))
        self.assertEqual(parse_moment("3pm", MOMENT), at(11, 5, 15))
        self.assertEqual(parse_moment("15:30", MOMENT), at(11, 5, 15, 30))
        self.assertEqual(parse_moment("at 5", MOMENT), at(11, 6, 5))

    def test_dates(self):
        self.assertEqual(parse_moment("2026-12-25", MOMENT), at(12, 25))
        self.assertEqual(parse_moment("2026-12-25 14:30", MOMENT), at(12, 25, 14, 30))
        self.assertEqual(parse_moment("2026-12-25T14:30", MOMENT), at(12, 25, 14, 30))
        self.assertEqual(parse_moment("12/25", MOMENT), at(12, 25))
        self.assertEqual(parse_moment("12/25/2027", MOMENT), at(12, 25, year=2027))
        self.assertEqual(parse_moment("dec 25 at 9am", MOMENT), at(12, 25, 9))
        self.assertEqual(parse_moment("december 25th 2027", MOMENT), at(12, 25, year=2027))
        self.assertEqual(parse_moment("25th of december", MOMENT), at(12, 25))

    def test_a_date_without_a_year_moves_to_next_year_once_passed(self):
        self.assertEqual(parse_moment("jan 2", MOMENT), at(1, 2, year=2027))
        self.assertEqual(parse_moment("3/1", MOMENT), at(3, 1, year=2027))

    def test_weekdays_and_next(self):
        self.assertEqual(parse_moment("friday", MOMENT), at(11, 6))
        self.assertEqual(parse_moment("on friday at 5pm", MOMENT), at(11, 6, 17))
        self.assertEqual(parse_moment("next friday", MOMENT), at(11, 13))
        self.assertEqual(parse_moment("thursday at 3pm", MOMENT), at(11, 5, 15))
        self.assertEqual(parse_moment("thursday at 9am", MOMENT), at(11, 12, 9))
        self.assertEqual(parse_moment("next week", MOMENT), at(11, 12))
        self.assertEqual(parse_moment("next month", MOMENT), at(12, 5))
        self.assertEqual(parse_moment("next year", MOMENT), at(11, 5, year=2027))

    def test_ordinary_words_are_not_dates(self):
        self.assertIsNone(parse_when("look at the sun", MOMENT))
        self.assertIsNone(parse_when("may the force be with you", MOMENT))
        self.assertIsNone(parse_when("nothing to see here", MOMENT))
        self.assertIsNone(parse_moment("february 30", MOMENT))
        self.assertIsNone(parse_moment("25:00", MOMENT))
        self.assertIsNone(parse_moment("13pm", MOMENT))
        self.assertIsNone(parse_moment("next", MOMENT))

    def test_a_date_in_the_middle_of_a_sentence(self):
        self.assertEqual(parse_when("call bob tomorrow about the taxes", MOMENT), (at(11, 6), "call bob about the taxes"))
        self.assertEqual(parse_when("remind me to call mom at 9am", MOMENT), (at(11, 6, 9), "call mom"))
        self.assertEqual(parse_when("in an hour stretch", MOMENT), (at(11, 5, 13), "stretch"))

    def test_lengths_of_time(self):
        self.assertEqual(parse_moment("half an hour", MOMENT), at(11, 5, 12, 30))
        self.assertEqual(parse_moment("an hour from now", MOMENT), at(11, 5, 13))
        self.assertEqual(parse_moment("1 hour and 30 minutes", MOMENT), at(11, 5, 13, 30))
        self.assertIsNone(parse_moment("0m", MOMENT))
        self.assertIsNone(parse_moment("7", MOMENT))


class HighlightWordsTest(BotTest):
    def index(self, *words):
        return build_index(compile_word(1, word) for word in words)

    def found(self, sentence, *words):
        return {entry.word for entry in matching_words(highlight_tokens(sentence), self.index(*words))}

    def test_matching_works_like_a_search_index(self):
        self.assertEqual(self.found("Is the PRINTER on fire?", "printer"), {"printer"})
        self.assertEqual(self.found("the printers are fine", "printer"), {"printer"})
        self.assertEqual(self.found("it was printing", "print"), {"print"})
        self.assertEqual(self.found("she printed it", "print"), {"print"})
        self.assertEqual(self.found("two cities", "city"), {"city"})
        self.assertEqual(self.found("he is running", "run"), {"run"})
        self.assertEqual(self.found("while making it", "make"), {"make"})
        self.assertEqual(self.found("caf\u00e9 time", "cafe"), {"cafe"})

    def test_a_word_inside_another_word_does_not_count(self):
        self.assertEqual(self.found("concatenate them", "cat"), set())
        self.assertEqual(self.found("caring for it", "car"), set())

    def test_several_words_must_come_in_sequence(self):
        self.assertEqual(self.found("my acme printer is great", "acme printer"), {"acme printer"})
        self.assertEqual(self.found("printer from acme", "acme printer"), set())
        self.assertEqual(self.found("acme makes a printer", "acme printer"), set())

    def test_links_mentions_and_custom_emoji_are_not_words(self):
        self.assertEqual(self.found("see https://printer.example/printer", "printer"), set())
        self.assertEqual(self.found("<:printer:123456789012345678> hello", "printer"), set())
        self.assertEqual(highlight_tokens("<@123> don't"), ["dont"])

    def test_word_forms_stay_tight(self):
        self.assertIn("run", word_forms("running"))
        self.assertNotIn("car", word_forms("caring"))
        self.assertEqual(word_forms("bus"), {"bus"})
        self.assertEqual(word_forms("class"), {"class"})

    def test_placed_words_are_the_same_words_at_their_places(self):
        for text in (
            "Is the PRINTER on fire?", "caf\u00e9 time", "cafe\u0301 time", "Stra\u00dfe and \ufb01re", "don't \u2019quote\u2019",
            "<@123> see https://printer.example/x <:printer:1234567890> printer", "\u0130stanbul \u2460 \u00bd", "<https://x.com/a> b", "",
        ):
            placed = placed_tokens(text)
            self.assertEqual([token for token, _, _ in placed], highlight_tokens(text), text)
            for token, start, end in placed:
                self.assertIn(token, _fold_for_test(text[start:end]), text)

    def test_marked_spans_cover_each_match_in_the_original_text(self):
        text = "My Printer, and the PRINTERS on the acme  printer line"
        spans = marked_spans(text, [compile_word(1, "printer"), compile_word(1, "acme printer")])
        self.assertEqual([text[start:end] for start, end in spans], ["Printer", "PRINTERS", "acme  printer"])
        self.assertEqual(marked_spans("caf\u00e9 time", [compile_word(1, "cafe")]), [(0, 4)])
        self.assertEqual(marked_spans("see https://printer.example", [compile_word(1, "printer")]), [])

    def test_the_patience_is_ten_seconds(self):
        self.assertEqual(utilities.HIGHLIGHT_PATIENCE, 10)

    def test_marked_text_bolds_and_cuts_around_the_first_match(self):
        self.assertEqual(marked_text("my printer broke", [(3, 10)], 100), "my **printer** broke")
        long = "x" * 3000 + " printer " + "y" * 3000
        shown = marked_text(long, [(3001, 3008)], 1000)
        self.assertIn("**printer**", shown)
        self.assertTrue(shown.startswith("\N{HORIZONTAL ELLIPSIS}") and shown.endswith("\N{HORIZONTAL ELLIPSIS}"))
        self.assertLessEqual(len(shown), 1000 + 6)
        self.assertEqual(marked_text("short", [], 1000), "short")


def _fold_for_test(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold().replace("'", "").replace("\u2019", "")


class HighlightFlowTest(BotTest):
    modules = [Utilities]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        patience = mock.patch.object(utilities, "HIGHLIGHT_PATIENCE", 0)
        patience.start()
        self.addCleanup(patience.stop)
        self.bot._connection.user = self.me  # telling a command from talk reads the bot's own mention as a prefix
        self.cog_ = self.cog(Utilities)
        self.guild.default_role.permissions = FakePermissions("view_channel", "read_message_history")
        self.follower = self.member  # no Manage Messages, which a highlight doesn't need
        self.speaker = self.mod

    async def add(self, word, who=None):
        ctx = self.context(author=who or self.follower)
        await self.cog_.highlight_add.callback(self.cog_, ctx, word=word)
        return ctx

    def say(self, text, author=None, channel=None, **kwargs):
        return FakeMessage(channel or self.channel, author or self.speaker, text, **kwargs)

    async def post(self, message):
        """The message comes in, and every notice it starts runs to the end."""
        await self.cog_.on_message(message)
        await self.settle()

    async def settle(self):
        while self.cog_._tasks:
            await asyncio.gather(*list(self.cog_._tasks), return_exceptions=True)

    @staticmethod
    def notices(member):
        return [dm for dm in member.dms if dm.get("embed") is not None]

    async def test_a_word_sends_a_dm_with_the_word_marked_context_and_a_button(self):
        await self.add("printer")
        earlier = FakeMessage(self.channel, self.speaker, "before that")
        self.channel.sent.append(earlier)
        message = self.say("my printer broke")
        await self.post(message)
        self.assertEqual(len(self.notices(self.follower)), 1)
        dm = self.notices(self.follower)[0]
        self.assertEqual(dm["content"], f"**printer** came up in {self.channel.mention} in **{self.guild.name}**.")
        embed = dm["embed"]
        self.assertIn("my **printer** broke", embed.description)
        self.assertLess(embed.description.index("before that"), embed.description.index("my **printer** broke"))
        self.assertEqual(embed.author.name, self.speaker.display_name)
        button = dm["view"].children[0]
        self.assertEqual((button.label, button.url), ("Go to message", message.jump_url))

    async def test_the_first_word_sends_a_welcome_dm_and_warns_when_dms_are_closed(self):
        await self.add("printer")
        self.assertEqual([dm["content"] for dm in self.follower.dms], [f"Highlight notices from **{self.guild.name}** come here."])
        await self.add("filament")
        self.assertEqual(len(self.follower.dms), 1)

        async def closed(*args, **kwargs):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="closed"), "Cannot send messages to this user")

        self.admin.send = closed
        ctx = await self.add("printer", who=self.admin)
        self.assertIn("DMs are closed", ctx.oks[0])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM highlight_words WHERE user_id = ?", (self.admin.id,)), 1)

    async def test_your_own_message_and_bots_do_not_notify(self):
        await self.add("printer")
        await self.post(self.say("printer", author=self.follower))
        robot = FakeMember(self.guild, "robot", bot=True)
        await self.post(self.say("printer", author=robot))
        self.assertEqual(self.notices(self.follower), [])

    async def test_a_mention_already_told_them(self):
        await self.add("printer")
        await self.post(self.say("printer", mentions=[self.follower]))
        self.assertEqual(self.notices(self.follower), [])

    async def test_writing_typing_or_reacting_in_that_channel_drops_the_notice(self):
        await self.add("printer")
        other = FakeChannel(self.guild, "other")
        for show_up in (
            lambda: self.cog_.on_message(self.say("on it", author=self.follower)),
            lambda: self.cog_._highlight_typing(types.SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, user_id=self.follower.id)),
            lambda: self.cog_._highlight_reaction(types.SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, user_id=self.follower.id)),
        ):
            await self.cog_.on_message(self.say("printer"))
            await show_up()
            await self.settle()
        self.assertEqual(self.notices(self.follower), [])
        await self.cog_.on_message(self.say("printer"))
        await self.cog_.on_message(self.say("elsewhere", author=self.follower, channel=other))
        await self.cog_._highlight_typing(types.SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, user_id=self.admin.id))
        await self.settle()
        self.assertEqual(len(self.notices(self.follower)), 1)

    async def test_a_notice_waits_out_the_patience(self):
        with mock.patch.object(utilities, "HIGHLIGHT_PATIENCE", 30):
            await self.add("printer")
            await self.cog_.on_message(self.say("printer"))
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(self.notices(self.follower), [])
            self.assertEqual(len(self.cog_._pending), 1)
            for task, _ in list(self.cog_._pending.values()):
                task.cancel()
            await self.settle()

    async def test_a_message_taken_down_before_the_notice_sends_nothing(self):
        await self.add("printer")
        message = self.say("printer")
        await self.cog_.on_message(message)
        await self.cog_.on_raw_message_delete(types.SimpleNamespace(message_id=message.id, guild_id=self.guild.id, channel_id=self.channel.id))
        bulk = self.say("printer", channel=FakeChannel(self.guild, "other"))
        await self.cog_.on_message(bulk)
        await self.cog_.on_raw_bulk_message_delete(types.SimpleNamespace(message_ids={bulk.id}, guild_id=self.guild.id, channel_id=bulk.channel.id))
        await self.settle()
        self.assertEqual(self.notices(self.follower), [])

    async def test_a_word_removed_during_the_wait_sends_nothing(self):
        await self.add("printer")
        with mock.patch.object(utilities, "HIGHLIGHT_PATIENCE", 1):  # long enough for the removal's database write to land first
            await self.cog_.on_message(self.say("printer"))
            await self.cog_.highlight_remove.callback(self.cog_, self.context(author=self.follower), word="printer")
            await self.settle()
        self.assertEqual(self.notices(self.follower), [])

    async def test_needs_channel_access(self):
        await self.add("printer")
        secret = FakeChannel(self.guild, "secret")
        secret.permissions_for = lambda member: FakePermissions("manage_messages")
        await self.post(self.say("printer", channel=secret))
        self.assertEqual(self.notices(self.follower), [])

    async def test_everyone_can_follow_words(self):
        plain = FakeMember(self.guild, "plain")
        await self.add("printer", who=plain)
        await self.post(self.say("printer"))
        self.assertEqual(len(self.notices(plain)), 1)

    async def test_who_can_use_the_command_decides_who_gets_notices(self):
        await self.add("printer")
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"highlight": {"role_bl": [self.role_member.id]}}})
        await self.post(self.say("printer"))
        self.assertEqual(self.notices(self.follower), [])
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"highlight": {"enabled": False}}})
        await self.post(self.say("printer", channel=FakeChannel(self.guild, "other")))
        self.assertEqual(self.notices(self.follower), [])

    async def test_rules_on_where_commands_are_typed_do_not_stop_notices(self):
        await self.add("printer")
        bots = FakeChannel(self.guild, "bots")
        await self.bot.settings.save(self.guild.id, "commands", {"channel_bl": [self.channel.id], "overwrites": {"highlight": {"channel_wl": [bots.id], "channel_bl": [self.channel.id]}}})
        await self.post(self.say("printer"))
        self.assertEqual(len(self.notices(self.follower)), 1)

    async def test_block_and_unblock_a_channel_and_a_person(self):
        await self.add("printer")
        ctx = self.context(author=self.follower)
        await self.cog_.highlight_block.callback(self.cog_, ctx, blocks=self.speaker.mention)
        await self.post(self.say("printer"))
        self.assertEqual(self.notices(self.follower), [])
        await self.cog_.highlight_unblock.callback(self.cog_, ctx, blocks=self.speaker.mention)
        await self.cog_.highlight_block.callback(self.cog_, ctx, blocks=None)
        await self.post(self.say("printer"))
        self.assertEqual(self.notices(self.follower), [])
        await self.cog_.highlight_unblock.callback(self.cog_, ctx, blocks=None)
        await self.post(self.say("printer"))
        self.assertEqual(len(self.notices(self.follower)), 1)

    async def test_a_blocked_category_covers_its_channels(self):
        await self.add("printer")
        category = FakeChannel(self.guild, "printers", discord.ChannelType.category)
        inside = FakeChannel(self.guild, "x1c", category=category)
        ctx = self.context(author=self.follower)
        await self.cog_.highlight_block.callback(self.cog_, ctx, blocks="printers")
        self.assertEqual(ctx.fails, [])
        await self.post(self.say("printer", channel=inside))
        self.assertEqual(self.notices(self.follower), [])
        await self.post(self.say("printer"))
        self.assertEqual(len(self.notices(self.follower)), 1)

    async def test_clearblocks_unblocks_everything(self):
        await self.add("printer")
        ctx = self.context(author=self.follower)
        await self.cog_.highlight_clearblocks.callback(self.cog_, ctx)
        self.assertEqual(len(ctx.fails), 1)
        await self.cog_.highlight_block.callback(self.cog_, ctx, blocks=f"{self.speaker.mention} {self.channel.mention}")
        await self.cog_.highlight_clearblocks.callback(self.cog_, ctx)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM highlight_blocks"), 0)
        await self.post(self.say("printer"))
        self.assertEqual(len(self.notices(self.follower)), 1)

    async def test_block_by_a_name_that_does_not_exist_is_refused(self):
        ctx = self.context(author=self.follower)
        await self.cog_.highlight_block.callback(self.cog_, ctx, blocks="nosuchthing")
        self.assertTrue(ctx.fails)

    async def test_a_hidden_channel_reads_as_not_found(self):
        secret = FakeChannel(self.guild, "secret-plans")
        secret.permissions_for = lambda member: FakePermissions() if member is self.follower else member.guild_permissions
        for named in ("secret-plans", secret.mention, str(secret.id)):
            ctx = self.context(author=self.follower)
            await self.cog_.highlight_block.callback(self.cog_, ctx, blocks=named)
            self.assertEqual(ctx.fails, ["I can't find one of those channels or people."], named)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM highlight_blocks"), 0)

    async def test_pause_for_a_while_then_resume(self):
        await self.add("printer")
        ctx = self.context(author=self.follower)
        await self.cog_.highlight_pause.callback(self.cog_, ctx, duration="2h")
        self.assertIn("paused until <t:", ctx.oks[-1])
        await self.post(self.say("printer"))
        self.assertEqual(self.notices(self.follower), [])
        await self.cog_.highlight_resume.callback(self.cog_, ctx)
        self.assertIn("back on", ctx.oks[-1])
        await self.post(self.say("printer"))
        self.assertEqual(len(self.notices(self.follower)), 1)
        await self.cog_.highlight_resume.callback(self.cog_, ctx)
        self.assertEqual(ctx.fails, ["Your highlight DMs here aren't paused."])

    async def test_a_pause_that_ran_out_lets_notices_through(self):
        await self.add("printer")
        await self.cog_.highlight_pause.callback(self.cog_, self.context(author=self.follower), duration="2h")
        self.cog_._pauses[(self.guild.id, self.follower.id)] = time.time() - 1
        await self.post(self.say("printer"))
        self.assertEqual(len(self.notices(self.follower)), 1)

    async def test_a_pause_with_no_end_lasts_through_a_restart_and_shows(self):
        await self.add("printer")
        ctx = self.context(author=self.follower)
        await self.cog_.highlight_pause.callback(self.cog_, ctx, duration=None)
        self.assertIn("until you run `/highlight resume`", ctx.oks[-1])
        fresh = Utilities(self.bot)
        await fresh._load_highlights()
        self.assertTrue(fresh._held_back(self.guild.id, self.follower.id, self.say("printer"), time.time()))
        await self.cog_.highlight.callback(self.cog_, ctx)
        self.assertTrue(ctx.sent[-1]["content"].startswith("**Paused** until you run"))

    async def test_a_pause_length_that_can_not_be_read_is_refused(self):
        for text in ("banana", "in 100000 years", "yesterday", "2020-01-01"):
            ctx = self.context(author=self.follower)
            await self.cog_.highlight_pause.callback(self.cog_, ctx, duration=text)
            self.assertEqual(len(ctx.fails), 1, text)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM highlight_pauses"), 0)

    async def test_import_copies_words_from_another_server(self):
        other = FakeGuild("Other Place")
        self.bot.fake_guilds.append(other)
        rows = [(other.id, self.follower.id, word, 1.0) for word in ("printer", "Filament", "spool")]
        await self.db.many("INSERT INTO highlight_words VALUES (?, ?, ?, ?)", rows)
        await self.cog_._load_highlights()
        await self.add("filament")
        interaction = types.SimpleNamespace(user=self.follower, guild_id=self.guild.id)
        choices = await self.cog_._highlight_import_servers(interaction, "oth")
        self.assertEqual([(c.name, c.value) for c in choices], [("Other Place", str(other.id))])
        ctx = self.context(author=self.follower)
        await self.cog_.highlight_import.callback(self.cog_, ctx, server=str(other.id))
        self.assertIn("Copied 2 words", ctx.oks[-1])
        words = {row["word"] for row in await self.db.all("SELECT word FROM highlight_words WHERE guild_id = ? AND user_id = ?", (self.guild.id, self.follower.id))}
        self.assertEqual(words, {"filament", "printer", "spool"})
        await self.post(self.say("the spool ran out"))
        self.assertEqual(len(self.notices(self.follower)), 1)
        await self.cog_.highlight_import.callback(self.cog_, ctx, server="other place")
        self.assertIn("already follow every word", ctx.fails[-1])
        for named in (str(self.guild.id), "nowhere"):
            await self.cog_.highlight_import.callback(self.cog_, ctx, server=named)
        self.assertEqual(len(ctx.fails), 3)

    async def test_import_stops_at_the_word_limit(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        await self.db.many("INSERT INTO highlight_words VALUES (?, ?, ?, ?)", [(other.id, self.follower.id, f"word{n}", 1.0) for n in range(5)])
        await self.db.many("INSERT INTO highlight_words VALUES (?, ?, ?, ?)", [(self.guild.id, self.follower.id, f"here{n}", 1.0) for n in range(utilities.MAX_HIGHLIGHT_WORDS - 2)])
        await self.cog_._load_highlights()
        ctx = self.context(author=self.follower)
        await self.cog_.highlight_import.callback(self.cog_, ctx, server="Other")
        self.assertIn("Copied 2 words", ctx.oks[-1])
        self.assertIn("3 more didn't fit", ctx.oks[-1])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM highlight_words WHERE guild_id = ?", (self.guild.id,)), utilities.MAX_HIGHLIGHT_WORDS)

    async def test_one_notice_a_minute_per_channel(self):
        await self.add("printer")
        await self.cog_.on_message(self.say("printer"))
        await self.cog_.on_message(self.say("printer again"))
        await self.settle()
        await self.post(self.say("printer once more"))
        self.assertEqual(len(self.notices(self.follower)), 1)

    async def test_waiting_notices_have_a_ceiling(self):
        await self.add("printer")
        await self.add("printer", who=self.admin)
        with mock.patch.object(utilities, "MAX_PENDING_HIGHLIGHTS", 1):
            await self.cog_.on_message(self.say("printer"))
            self.assertEqual(len(self.cog_._pending), 1)
        await self.settle()
        self.assertEqual(len(self.notices(self.follower)) + len(self.notices(self.admin)), 1)

    async def test_two_words_in_one_message_send_one_dm(self):
        await self.add("printer")
        await self.add("filament")
        await self.post(self.say("printer filament"))
        self.assertEqual(len(self.notices(self.follower)), 1)
        self.assertTrue(self.notices(self.follower)[0]["content"].startswith("**filament** and **printer** came up"))

    async def test_running_a_command_does_not_notify_but_a_line_that_starts_like_one_does(self):
        await self.add("printer")
        await self.bot.settings.save(self.guild.id, "core", {"prefixes": ["."]})
        await self.post(self.say(".highlight matches printer"))
        await self.post(self.say(".hl m printer"))
        self.assertEqual(self.notices(self.follower), [])
        await self.post(self.say(".3mf files for the printer"))
        self.assertEqual(len(self.notices(self.follower)), 1)

    async def test_typed_commands_come_down_and_their_answers_do_too(self):
        ctx = self.context(author=self.follower)
        ctx.command = self.bot.get_command("highlight add")
        await self.cog_.cog_before_invoke(ctx)
        self.assertTrue(ctx.message.deleted)
        await self.cog_.highlight_add.callback(self.cog_, ctx, word="printer")
        await self.cog_.highlight.callback(self.cog_, ctx)
        self.assertEqual(ctx.reply_options[-1].get("delete_after"), 10)
        self.assertEqual(ctx.sent[-1].get("delete_after"), 10)
        other = self.context(author=self.follower)
        other.command = self.bot.get_command("reminder")
        await self.cog_.cog_before_invoke(other)
        self.assertFalse(other.message.deleted)

    async def test_a_closed_dm_is_not_an_error(self):
        await self.add("printer")

        async def closed(*args, **kwargs):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="closed"), "Cannot send messages to this user")

        self.follower.send = closed
        with self.assertNoLogs(utilities.log, level="ERROR"):
            await self.post(self.say("printer"))

    async def test_mentions_and_tiny_words_are_refused(self):
        for word in (self.speaker.mention, "@everyone", "a", "!!!", "x" * 101):
            ctx = await self.add(word)
            self.assertTrue(ctx.fails, word)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM highlight_words"), 0)

    async def test_duplicates_are_refused_and_the_echo_cannot_ping(self):
        await self.add("Printer")
        ctx = await self.add("printer")
        self.assertTrue(ctx.fails)

    async def test_matches_show_and_remove(self):
        await self.add("printer")
        ctx = self.context(author=self.follower)
        await self.cog_.highlight_matches.callback(self.cog_, ctx, words="the printers")
        self.assertIn("printer", ctx.sent[-1]["content"])
        await self.cog_.highlight_matches.callback(self.cog_, ctx, words="nothing")
        self.assertIn("wouldn't", ctx.sent[-1]["content"])
        await self.cog_.highlight.callback(self.cog_, ctx)
        self.assertIn("printer", ctx.sent[-1]["content"])
        await self.cog_.highlight_remove.callback(self.cog_, ctx, word="PRINTER")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM highlight_words"), 0)
        await self.post(self.say("printer"))
        self.assertEqual(self.notices(self.follower), [])

    async def test_clear_only_touches_this_person_in_this_server(self):
        await self.add("printer")
        await self.add("printer", who=self.admin)
        other = FakeGuild("Other")
        await self.db.run("INSERT INTO highlight_words VALUES (?, ?, 'printer', 1)", (other.id, self.follower.id))
        ctx = self.context(author=self.follower)
        await self.cog_.highlight_clear.callback(self.cog_, ctx)
        rows = await self.db.all("SELECT guild_id, user_id FROM highlight_words")
        self.assertCountEqual(rows, [{"guild_id": self.guild.id, "user_id": self.admin.id}, {"guild_id": other.id, "user_id": self.follower.id}])

    async def test_words_and_blocks_come_back_after_a_restart(self):
        await self.add("printer")
        ctx = self.context(author=self.follower)
        await self.cog_.highlight_block.callback(self.cog_, ctx, blocks=self.speaker.mention)
        fresh = Utilities(self.bot)
        await fresh._load_highlights()
        await fresh.on_message(self.say("printer"))
        while fresh._tasks:
            await asyncio.gather(*list(fresh._tasks), return_exceptions=True)
        self.assertEqual(self.notices(self.follower), [])
        await fresh.on_message(self.say("printer", author=self.admin))
        while fresh._tasks:
            await asyncio.gather(*list(fresh._tasks), return_exceptions=True)
        self.assertEqual(len(self.notices(self.follower)), 1)

    async def test_a_pause_that_ran_out_is_cleared_at_start(self):
        await self.db.run("INSERT INTO highlight_pauses VALUES (?, ?, ?)", (self.guild.id, self.follower.id, time.time() - 5))
        await self.db.run("INSERT INTO highlight_pauses VALUES (?, ?, NULL)", (self.guild.id, self.admin.id))
        await self.db.run("INSERT INTO highlight_pauses VALUES (?, ?, ?)", (self.guild.id, self.mod.id, time.time() + 3600))
        await self.cog_._forget_ended_pauses()
        rows = await self.db.all("SELECT user_id FROM highlight_pauses ORDER BY user_id")
        self.assertEqual({row["user_id"] for row in rows}, {self.admin.id, self.mod.id})


class HighlightTagLineTest(BotTest):
    modules = [Utilities, Tags]

    async def test_a_tag_typed_as_chat_does_not_notify(self):
        self.bot._connection.user = self.me
        self.guild.default_role.permissions = FakePermissions("view_channel", "read_message_history")
        utilities_ = self.cog(Utilities)
        with mock.patch.object(utilities, "HIGHLIGHT_PATIENCE", 0):
            await utilities_.highlight_add.callback(utilities_, self.context(author=self.member), word="printer")
            await self.bot.settings.save(self.guild.id, "core", {"prefixes": ["."]})
            await self.db.run("INSERT INTO tags (guild_id, name, content, created_at, updated_at) VALUES (?, 'printer', 'Read the wiki', 1, 1)", (self.guild.id,))
            await utilities_.on_message(FakeMessage(self.channel, self.mod, ".printer"))
            await utilities_.on_message(FakeMessage(self.channel, self.mod, ".nottag printer"))
            while utilities_._tasks:
                await asyncio.gather(*list(utilities_._tasks), return_exceptions=True)
        notices = [dm for dm in self.member.dms if dm.get("embed") is not None]
        self.assertEqual(len(notices), 1)
        self.assertIn("nottag", notices[0]["embed"].description)

class ReminderTest(BotTest):
    modules = [Utilities]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Utilities)

    async def make(self, about, when=None, author=None):
        ctx = self.context(author=author or self.member)
        await self.cog_.reminder.callback(self.cog_, ctx, about=about, when=when)
        return ctx

    async def rows(self):
        return await self.db.all("SELECT * FROM reminders ORDER BY id")

    async def test_set_from_one_sentence_and_delivered_by_dm(self):
        ctx = await self.make("in 3 days do the thing")
        row = (await self.rows())[0]
        self.assertEqual(row["about"], "do the thing")
        self.assertAlmostEqual(row["due_at"], time.time() + 3 * 86400, delta=5)
        self.assertEqual(ctx.oks and "do the thing" in ctx.oks[0], True)
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        self.assertEqual(await self.bot.timers.run_due(row["due_at"] + 1), 1)
        self.assertEqual(self.member.dms[0]["embed"].description, "do the thing")
        self.assertEqual(await self.rows(), [])

    async def test_set_with_separate_about_and_when(self):
        await self.make("water the plants", "2 hours")
        row = (await self.rows())[0]
        self.assertEqual(row["about"], "water the plants")
        self.assertAlmostEqual(row["due_at"], time.time() + 7200, delta=5)

    async def test_refusals(self):
        ctx = await self.make("no date in this")
        self.assertTrue(ctx.fails)
        ctx = await self.make("x", "not a date")
        self.assertTrue(ctx.fails)
        ctx = await self.make("x", "2020-01-01")
        self.assertIn("passed", ctx.fails[0])
        ctx = await self.make("x", "2099-01-01")
        self.assertIn("ten years", ctx.fails[0])
        ctx = await self.make("y" * 1501, "1h")
        self.assertTrue(ctx.fails)
        self.assertEqual(await self.rows(), [])

    async def test_closed_dms_fall_back_to_the_channel_and_ping_only_them(self):
        await self.make("do it @everyone <@&123>", "1h")

        async def closed(*args, **kwargs):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="closed"), "Cannot send messages to this user")

        self.member.send = closed
        self.guild.default_role.permissions = FakePermissions("view_channel", "send_messages")  # the fallback only goes where they could post
        await self.bot.timers.run_due(time.time() + 3601)
        sent = self.channel.sent[-1]
        self.assertIn(self.member.mention, sent.content)
        self.assertNotIn("do it", sent.content, "their own words never go in the channel")
        mentions = sent.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertEqual(mentions.users, [self.member])
        self.assertEqual(await self.rows(), [])

    async def test_closed_dms_and_a_deleted_channel_do_not_retry_forever(self):
        await self.make("x", "1h")

        async def closed(*args, **kwargs):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="closed"), "Cannot send messages to this user")

        self.member.send = closed
        self.guild.channels.remove(self.channel)
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers"), 0)

    async def test_a_failed_send_for_another_reason_is_retried(self):
        await self.make("x", "1h")

        async def broken(*args, **kwargs):
            raise discord.DiscordServerError(types.SimpleNamespace(status=503, reason="down"), "try later")

        self.member.send = broken
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(await self.db.scalar("SELECT tries FROM timers"), 1)

    async def test_mine_cancel_and_clear_touch_only_your_own(self):
        await self.make("mine", "1h")
        await self.make("theirs", "1h", author=self.admin)
        rows = await self.rows()
        ctx = self.context(author=self.member)
        await self.cog_.reminder_mine.callback(self.cog_, ctx)
        text = ctx.sent[-1]["embed"].description
        self.assertIn("mine", text)
        self.assertNotIn("theirs", text)
        await self.cog_.reminder_cancel.callback(self.cog_, ctx, rows[1]["id"])
        self.assertTrue(ctx.fails)
        await self.cog_.reminder_cancel.callback(self.cog_, ctx, rows[0]["id"])
        self.assertEqual(len(await self.rows()), 1)
        self.assertIsNone(await self.bot.timers.get_key("reminder", f"reminder:{rows[0]['id']}"))
        await self.make("mine again", "1h")
        await self.cog_.reminder_clear.callback(self.cog_, ctx)
        self.assertEqual([row["about"] for row in await self.rows()], ["theirs"])

    async def test_subscribe_copies_and_outlives_the_original(self):
        await self.make("shared", "1h", author=self.admin)
        source = (await self.rows())[0]
        self.guild.default_role.permissions = FakePermissions("view_channel", "read_message_history")  # someone else's reminder takes reading the channel it was typed in
        ctx = self.context(author=self.member)
        await self.cog_.reminder_subscribe.callback(self.cog_, ctx, source["id"])
        await self.cog_.reminder_subscribe.callback(self.cog_, ctx, source["id"])
        self.assertTrue(ctx.fails)
        rows = await self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["user_id"], self.member.id)
        self.assertEqual(rows[1]["copied_from"], source["id"])
        self.assertEqual(rows[1]["due_at"], source["due_at"])
        admin_ctx = self.context(author=self.admin)
        await self.cog_.reminder_cancel.callback(self.cog_, admin_ctx, source["id"])
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertEqual(len(self.member.dms), 1)

    async def test_subscribing_to_your_own_is_refused(self):
        await self.make("x", "1h")
        ctx = self.context(author=self.member)
        await self.cog_.reminder_subscribe.callback(self.cog_, ctx, (await self.rows())[0]["id"])
        self.assertTrue(ctx.fails)

    async def test_another_servers_reminder_cannot_be_read_or_copied(self):
        other = FakeGuild("Other")
        foreign = await self.cog_._add_reminder(guild_id=other.id, channel_id=None, user_id=self.admin.id, about="secret plans", due_at=time.time() + 60)
        ctx = self.context(author=self.member)
        await self.cog_.reminder_when.callback(self.cog_, ctx, foreign)
        await self.cog_.reminder_subscribe.callback(self.cog_, ctx, foreign)
        self.assertEqual(len(ctx.fails), 2)
        self.assertEqual(ctx.sent, [])

    async def test_when_shows_details_without_pinging(self):
        await self.make("details @everyone", "1h", author=self.admin)
        self.guild.default_role.permissions = FakePermissions("view_channel", "read_message_history")  # someone else's reminder takes reading the channel it was typed in
        ctx = self.context(author=self.member)
        await self.cog_.reminder_when.callback(self.cog_, ctx, (await self.rows())[0]["id"])
        sent = ctx.sent[-1]
        self.assertIn("details", sent["embed"].description)
        self.assertEqual(sent["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())

    async def test_used_in_a_dm_it_sees_only_your_reminders(self):
        ctx = self.context(author=self.member)
        ctx.guild = None
        await self.cog_.reminder.callback(self.cog_, ctx, about="dm one", when="1h")
        row = (await self.rows())[0]
        self.assertIsNone(row["guild_id"])
        self.assertIsNone(row["channel_id"])
        stranger = self.context(author=self.admin)
        stranger.guild = None
        await self.cog_.reminder_when.callback(self.cog_, stranger, row["id"])
        self.assertTrue(stranger.fails)
        await self.cog_.reminder_mine.callback(self.cog_, ctx)
        self.assertIn("dm one", ctx.sent[-1]["embed"].description)
        await self.cog_.reminder_clear.callback(self.cog_, ctx)
        self.assertEqual(await self.rows(), [])

    async def test_a_dm_reminder_is_delivered_without_a_server(self):
        ctx = self.context(author=self.member)
        ctx.guild = None
        await self.cog_.reminder.callback(self.cog_, ctx, about="dm one", when="1h")
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertEqual(self.member.dms[0]["embed"].description, "dm one")

    async def test_repeat_comes_back_and_skips_what_it_missed(self):
        await self.make("water", "1h")
        row = (await self.rows())[0]
        ctx = self.context(author=self.member)
        await self.cog_.reminder_repeat.callback(self.cog_, ctx, row["id"], duration="1 day")
        self.assertEqual(ctx.oks and "1 day" in ctx.oks[-1], True)
        far_later = row["due_at"] + 86400 * 5 + 60
        with mock.patch.object(utilities.time, "time", return_value=far_later):
            await self.bot.timers.run_due(far_later)
        self.assertEqual(len(self.member.dms), 1)
        again = (await self.rows())[0]
        self.assertAlmostEqual(again["due_at"], row["due_at"] + 86400 * 6, delta=1)
        timer = await self.bot.timers.get_key("reminder", f"reminder:{row['id']}")
        self.assertEqual(timer["due_at"], again["due_at"])

    async def test_repeat_with_no_length_uses_the_first_wait_and_off_stops_it(self):
        await self.make("water", "2h")
        row = (await self.rows())[0]
        ctx = self.context(author=self.member)
        await self.cog_.reminder_repeat.callback(self.cog_, ctx, row["id"])
        self.assertAlmostEqual((await self.rows())[0]["repeat_seconds"], 7200, delta=5)
        await self.cog_.reminder_repeat.callback(self.cog_, ctx, row["id"], duration="off")
        self.assertIsNone((await self.rows())[0]["repeat_seconds"])

    async def test_repeat_refuses_spam_and_other_peoples_reminders(self):
        await self.make("water", "1h")
        row = (await self.rows())[0]
        ctx = self.context(author=self.member)
        await self.cog_.reminder_repeat.callback(self.cog_, ctx, row["id"], duration="1m")
        self.assertTrue(ctx.fails)
        stranger = self.context(author=self.admin)
        await self.cog_.reminder_repeat.callback(self.cog_, stranger, row["id"], duration="1 day")
        self.assertTrue(stranger.fails)
        self.assertIsNone((await self.rows())[0]["repeat_seconds"])

    async def test_a_canceled_reminder_whose_timer_still_fires_does_nothing(self):
        await self.make("x", "1h")
        row = (await self.rows())[0]
        await self.db.run("DELETE FROM reminders")
        await self.cog_._reminder_due({"id": row["id"]}, {})
        self.assertEqual(self.member.dms, [])

    async def test_reminders_survive_a_restart(self):
        await self.make("x", "1h")
        fresh = Utilities(self.bot)
        await fresh.on_load()
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertEqual(len(self.member.dms), 1)


class Press:
    """Just enough of an interaction to press a button."""

    def __init__(self, user, guild, message):
        self.user = user
        self.guild_id = guild.id
        self.guild = guild
        self.message = message
        self.sent = []
        self.edits = []
        self.response = types.SimpleNamespace(send_message=self._send, edit_message=self._edit, defer=self._defer)

    async def _send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})

    async def _edit(self, **kwargs):
        self.edits.append(kwargs)

    async def _defer(self, **kwargs):
        pass

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


class GiveawayTest(BotTest):
    modules = [Utilities]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Utilities)
        self.ctx = self.context()

    async def create(self, winners=1, channel=None, prize="A printer"):
        await self.cog_.giveaway_create.callback(self.cog_, self.ctx, 3600, winners, prize=prize, channel=channel)
        return await self.db.one("SELECT * FROM giveaways ORDER BY id DESC LIMIT 1")

    def post(self, row, channel=None):
        return next(m for m in (channel or self.channel).sent if m.id == row["message_id"])

    async def press(self, user, row):
        interaction = Press(user, self.guild, self.post(row))
        await self.cog_.giveaway_pressed(interaction)
        return interaction

    async def entries(self, row):
        return {r["user_id"] for r in await self.db.all("SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?", (row["id"],))}

    async def test_create_posts_a_button_and_schedules_the_end(self):
        row = await self.create(winners=2)
        message = self.post(row)
        self.assertEqual(message.embeds[0].title, "A printer")
        self.assertIsInstance(message.kwargs["view"], GiveawayView)
        self.assertEqual(message.kwargs["view"].children[0].custom_id, "captainguild:giveaway:enter")
        self.assertIsNotNone(await self.bot.timers.get_key("giveaway", f"giveaway:{row['id']}"))
        self.assertAlmostEqual(row["ends_at"], time.time() + 3600, delta=5)

    async def test_the_button_is_registered_for_restarts(self):
        self.assertTrue(any(isinstance(view, GiveawayView) for view in self.bot.added_views))

    async def test_create_posts_in_the_chosen_channel(self):
        other = FakeChannel(self.guild, "giveaways")
        row = await self.create(channel=other)
        self.assertEqual(row["channel_id"], other.id)
        self.assertEqual(self.post(row, other).embeds[0].title, "A printer")

    async def test_prefix_text_can_end_in_a_channel(self):
        other = FakeChannel(self.guild, "giveaways")
        await self.cog_.giveaway_create.callback(self.cog_, self.ctx, 3600, 1, prize=f"A printer {other.mention}")
        row = await self.db.one("SELECT * FROM giveaways")
        self.assertEqual(row["channel_id"], other.id)
        self.assertEqual(row["prize"], "A printer")

    async def test_refusals(self):
        await self.cog_.giveaway_create.callback(self.cog_, self.ctx, 5, 1, prize="x")
        await self.cog_.giveaway_create.callback(self.cog_, self.ctx, 3600, 1, prize="x" * 257)
        self.assertEqual(len(self.ctx.fails), 2)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM giveaways"), 0)

    async def test_posting_failure_leaves_nothing_behind(self):
        async def forbidden(*args, **kwargs):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="no"), "Missing Access")

        self.channel.send = forbidden
        await self.create()
        self.assertTrue(self.ctx.fails)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM giveaways"), 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers"), 0)

    async def test_button_enters_and_leaves(self):
        row = await self.create()
        first = await self.press(self.member, row)
        self.assertEqual(await self.entries(row), {self.member.id})
        self.assertTrue(first.sent[0]["ephemeral"])
        await self.press(self.member, row)
        self.assertEqual(await self.entries(row), set())

    async def test_the_button_does_nothing_once_it_ended_or_on_another_servers_message(self):
        row = await self.create()
        await self.db.run("UPDATE giveaways SET ends_at = ?", (time.time() - 1,))
        pressed = await self.press(self.member, row)
        self.assertIn("ended", pressed.sent[0]["content"])
        self.assertEqual(await self.entries(row), set())
        await self.db.run("UPDATE giveaways SET ends_at = ?", (time.time() + 60,))
        elsewhere = Press(self.member, FakeGuild("Other"), self.post(row))
        await self.cog_.giveaway_pressed(elsewhere)
        self.assertIn("no longer exists", elsewhere.sent[0]["content"])
        self.assertEqual(await self.entries(row), set())

    async def test_the_timer_picks_winners_and_pings_only_them(self):
        row = await self.create(prize="Hello @everyone")
        await self.press(self.member, row)
        await self.bot.timers.run_due(time.time() + 3601)
        row = await self.db.one("SELECT * FROM giveaways")
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(json.loads(row["winner_ids"]), [self.member.id])
        announcement = self.channel.sent[-1]
        self.assertIn(self.member.mention, announcement.content)
        mentions = announcement.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertEqual([u.id for u in mentions.users], [self.member.id])
        self.assertIsNone(self.post(row).kwargs["view"])

    async def test_never_picks_more_than_asked_or_people_who_left(self):
        row = await self.create(winners=2)
        leaver = FakeMember(self.guild, "leaver")
        for person in (self.member, self.mod, self.admin, leaver):
            await self.press(person, row)
        self.guild.members.remove(leaver)
        await self.bot.timers.run_due(time.time() + 3601)
        winners = json.loads((await self.db.one("SELECT winner_ids FROM giveaways"))["winner_ids"])
        self.assertEqual(len(winners), 2)
        self.assertEqual(len(set(winners)), 2)
        self.assertNotIn(leaver.id, winners)

    async def test_no_entries_ends_with_no_winner_and_says_so(self):
        await self.create()
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertIn("Nobody entered", self.channel.sent[-1].content)

    async def test_a_retry_after_a_crash_announces_the_same_winners_once(self):
        row = await self.create(winners=1)
        await self.press(self.member, row)
        await self.press(self.mod, row)
        row = await self.db.one("SELECT * FROM giveaways")
        real_announce = self.cog_._announce
        boom = mock.AsyncMock(side_effect=discord.DiscordServerError(types.SimpleNamespace(status=503, reason="down"), "later"))
        self.cog_._announce = boom
        with self.assertRaises(discord.DiscordServerError):
            await self.cog_.finish_giveaway(row)
        stored = await self.db.one("SELECT * FROM giveaways")
        self.assertEqual(stored["announced"], 0)
        first_winners = json.loads(stored["winner_ids"])
        self.cog_._announce = real_announce
        count_before = len(self.channel.sent)
        await self.cog_._giveaway_due({"id": row["id"]}, {})
        again = await self.db.one("SELECT * FROM giveaways")
        self.assertEqual(json.loads(again["winner_ids"]), first_winners)
        self.assertEqual(again["announced"], 1)
        self.assertEqual(len(self.channel.sent), count_before + 1)
        await self.cog_._giveaway_due({"id": row["id"]}, {})
        self.assertEqual(len(self.channel.sent), count_before + 1)

    async def test_a_deleted_channel_still_ends_it(self):
        row = await self.create()
        await self.press(self.member, row)
        self.guild.channels.remove(self.channel)
        await self.bot.timers.run_due(time.time() + 3601)
        stored = await self.db.one("SELECT * FROM giveaways")
        self.assertIsNotNone(stored["ended_at"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers"), 0)

    async def test_end_asks_then_ends_quietly_or_out_loud(self):
        row = await self.create()
        await self.press(self.member, row)
        ctx = self.context()
        await self.cog_.giveaway_end.callback(self.cog_, ctx, row["id"])
        view = ctx.sent[-1]["view"]
        self.assertIsInstance(view, EndGiveawayView)
        before = len(self.channel.sent)
        pressed = Press(self.admin, self.guild, None)
        await view._finish(pressed, False)
        self.assertEqual(len(self.channel.sent), before)
        stored = await self.db.one("SELECT * FROM giveaways")
        self.assertEqual(json.loads(stored["winner_ids"]), [self.member.id])
        self.assertIn(self.member.mention, pressed.edits[-1]["content"])
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertEqual(len(self.channel.sent), before)

    async def test_end_question_is_only_for_the_person_who_asked(self):
        row = await self.create()
        view = EndGiveawayView(self.cog_, row["id"], self.admin.id)
        stranger = Press(self.member, self.guild, None)
        self.assertFalse(await view.interaction_check(stranger))
        self.assertTrue(stranger.sent)

    async def test_reroll_replaces_only_who_was_named(self):
        row = await self.create(winners=2)
        for person in (self.member, self.mod, self.admin):
            await self.press(person, row)
        await self.cog_._giveaway_due({"id": row["id"]}, {})
        winners = json.loads((await self.db.one("SELECT winner_ids FROM giveaways"))["winner_ids"])
        keeper, gone = winners
        loser = ({self.member.id, self.mod.id, self.admin.id} - set(winners)).pop()
        ctx = self.context()
        await self.cog_.giveaway_reroll.callback(self.cog_, ctx, row["id"], f"<@{gone}>")
        self.assertEqual(ctx.fails, [])
        after = json.loads((await self.db.one("SELECT winner_ids FROM giveaways"))["winner_ids"])
        self.assertEqual(set(after), {keeper, loser})

    async def test_reroll_honors_exclude_and_refuses_the_rest(self):
        row = await self.create(winners=1)
        for person in (self.member, self.mod, self.admin):
            await self.press(person, row)
        ctx = self.context()
        await self.cog_.giveaway_reroll.callback(self.cog_, ctx, row["id"])
        self.assertIn("still running", ctx.fails[-1])
        await self.cog_._giveaway_due({"id": row["id"]}, {})
        winner = json.loads((await self.db.one("SELECT winner_ids FROM giveaways"))["winner_ids"])[0]
        others = [p.id for p in (self.member, self.mod, self.admin) if p.id != winner]
        await self.cog_.giveaway_reroll.callback(self.cog_, ctx, row["id"], f"<@{others[0]}>")
        self.assertIn("isn't a winner", ctx.fails[-1])
        await self.cog_.giveaway_reroll.callback(self.cog_, ctx, row["id"], None, exclude=f"<@{others[0]}>")
        after = json.loads((await self.db.one("SELECT winner_ids FROM giveaways"))["winner_ids"])
        self.assertEqual(after, [others[1]])
        await self.cog_.giveaway_reroll.callback(self.cog_, ctx, row["id"], None, exclude=f"<@{others[0]}> <@{winner}>")
        self.assertIn("Nobody else", ctx.fails[-1])

    async def test_another_servers_giveaway_is_out_of_reach(self):
        other = FakeGuild("Other")
        foreign = await self.db.insert(
            "INSERT INTO giveaways (guild_id, channel_id, host_id, prize, winners, created_at, ends_at) VALUES (?, 1, 1, 'x', 1, 1, 1)", (other.id,))
        for command, args in ((self.cog_.giveaway_end, ()), (self.cog_.giveaway_reroll, (None,)), (self.cog_.giveaway_participants, ())):
            ctx = self.context()
            await command.callback(self.cog_, ctx, foreign, *args)
            self.assertIn("no giveaway", ctx.fails[0])

    async def test_list_and_participants(self):
        running = await self.create(prize="running one")
        await self.press(self.member, running)
        ended = await self.create(prize="ended one")
        await self.cog_.finish_giveaway(await self.db.one("SELECT * FROM giveaways WHERE id = ?", (ended["id"],)))
        ctx = self.context()
        await self.cog_.giveaway_list.callback(self.cog_, ctx, "active")
        self.assertIn("running one", ctx.sent[-1]["embed"].description)
        self.assertNotIn("ended one", ctx.sent[-1]["embed"].description)
        await self.cog_.giveaway_list.callback(self.cog_, ctx, "inactive")
        self.assertIn("ended one", ctx.sent[-1]["embed"].description)
        await self.cog_.giveaway_participants.callback(self.cog_, ctx, running["id"])
        self.assertIn(str(self.member.id), ctx.sent[-1]["content"])

    async def test_long_participant_lists_arrive_as_a_file(self):
        row = await self.create()
        rows = [(row["id"], self.guild.id, 100000000000000000 + n, float(n)) for n in range(120)]
        await self.db.many("INSERT INTO giveaway_entries (giveaway_id, guild_id, user_id, entered_at) VALUES (?, ?, ?, ?)", rows)
        ctx = self.context()
        await self.cog_.giveaway_participants.callback(self.cog_, ctx, row["id"])
        self.assertIn("120 people", ctx.sent[-1]["content"])
        self.assertIn("file", ctx.sent[-1])


class PollTest(BotTest):
    modules = [Utilities]

    async def poll(self, question, choices=None):
        cog = self.cog(Utilities)
        ctx = self.context()
        await cog.poll.callback(cog, ctx, question=question, choices=choices)
        return ctx

    async def test_no_choices_is_thumbs_up_and_down(self):
        await self.poll("Is this nice?")
        message = self.channel.sent[-1]
        self.assertEqual(message.added_reactions, ["\N{THUMBS UP SIGN}", "\N{THUMBS DOWN SIGN}"])
        self.assertIn("Is this nice?", message.embeds[0].description)
        self.assertEqual(message.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())

    async def test_choices_get_letters(self):
        await self.poll("Best?", "red | blue, green | white")
        message = self.channel.sent[-1]
        self.assertEqual(len(message.added_reactions), 3)
        self.assertIn("blue, green", message.embeds[0].description)

    def test_splitting_one_line(self):
        self.assertEqual(split_poll("Best color? red, blue"), ("Best color?", ["red", "blue"]))
        self.assertEqual(split_poll("Best color | red | blue"), ("Best color", ["red", "blue"]))
        self.assertEqual(split_poll("Hello, is this nice?"), ("Hello, is this nice?", []))
        self.assertEqual(split_poll("Yes or no"), ("Yes or no", []))

    async def test_refusals(self):
        self.assertTrue((await self.poll("Why?", "only one")).fails)
        self.assertTrue((await self.poll("Why?", "|".join(str(n) for n in range(21)))).fails)
        self.assertTrue((await self.poll("   ")).fails)
        self.assertEqual(self.channel.sent, [])

    async def test_missing_reaction_permission_is_a_message_not_a_crash(self):
        async def forbidden(*args, **kwargs):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="no"), "Missing Permissions")

        real_send = self.channel.send

        async def send(*args, **kwargs):
            message = await real_send(*args, **kwargs)
            message.add_reaction = forbidden
            return message

        self.channel.send = send
        self.assertTrue((await self.poll("Why?")).fails)


class TempChannelTest(BotTest):
    modules = [Utilities]

    async def make(self, name="planning chat", duration=3600):
        cog = self.cog(Utilities)
        ctx = self.context()
        await cog.tempchannel.callback(cog, ctx, duration, name=name)
        return ctx

    async def test_made_stored_and_deleted_when_time_runs_out(self):
        ctx = await self.make()
        channel = next(c for c in self.guild.channels if c.name == "planning-chat")
        self.assertTrue(ctx.oks)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM temp_channels WHERE channel_id = ?", (channel.id,)), 1)
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertNotIn(channel, self.guild.channels)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM temp_channels"), 0)

    async def test_someone_deleting_it_first_cancels_the_timer(self):
        await self.make()
        channel = next(c for c in self.guild.channels if c.name == "planning-chat")
        self.guild.channels.remove(channel)
        await self.cog(Utilities).on_guild_channel_delete(channel)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM temp_channels"), 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers"), 0)

    async def test_a_timer_for_a_channel_already_gone_finishes_cleanly(self):
        await self.make()
        channel = next(c for c in self.guild.channels if c.name == "planning-chat")
        self.guild.channels.remove(channel)
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers"), 0)

    async def test_the_timer_only_reaches_its_own_server(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        other.me = FakeMember(other, "Bot", bot=True)
        outsider = FakeChannel(other, "their-channel")
        await self.cog(Utilities)._tempchannel_due({"guild_id": self.guild.id, "channel_id": outsider.id}, {})
        self.assertIn(outsider, other.channels)

    async def test_refusals(self):
        self.assertTrue((await self.make(duration=1)).fails)
        self.assertTrue((await self.make(duration=366 * 86400)).fails)

        async def forbidden(*args, **kwargs):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="no"), "Missing Permissions")

        self.guild.create_text_channel = forbidden
        ctx = await self.make()
        self.assertIn("Manage Channels", ctx.fails[0])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers"), 0)


class MemberLookupTest(BotTest):
    modules = [Utilities]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Utilities)
        self.newest = FakeMember(self.guild, "newest", joined=NOW + datetime.timedelta(days=5), created=NOW - datetime.timedelta(days=1))
        self.ancient = FakeMember(self.guild, "ancient", joined=NOW - datetime.timedelta(days=500), created=NOW - datetime.timedelta(days=4000))

    async def test_info_defaults_to_you_and_shows_the_facts(self):
        ctx = self.context(author=self.member)
        await self.cog_.members.callback(self.cog_, ctx)
        embed = ctx.sent[-1]["embed"]
        fields = {f.name: f.value for f in embed.fields}
        self.assertEqual(fields["ID"], str(self.member.id))
        self.assertIn(self.role_member.mention, fields["Roles (1)"])
        self.assertIn("Join position", fields)
        self.assertEqual(ctx.sent[-1]["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())

    async def test_info_counts_join_position(self):
        ctx = self.context()
        await self.cog_.members.callback(self.cog_, ctx, member=self.ancient)
        fields = {f.name: f.value for f in ctx.sent[-1]["embed"].fields}
        self.assertEqual(fields["Join position"], "1st")

    async def test_rankings(self):
        ctx = self.context()
        await self.cog_.members_youngest.callback(self.cog_, ctx, 1)
        self.assertIn(self.newest.mention, ctx.sent[-1]["embed"].description)
        await self.cog_.members_oldest.callback(self.cog_, ctx, 1)
        self.assertIn(self.ancient.mention, ctx.sent[-1]["embed"].description)
        await self.cog_.members_newusers.callback(self.cog_, ctx, 1)
        self.assertIn(self.newest.mention, ctx.sent[-1]["embed"].description)
        await self.cog_.members_oldusers.callback(self.cog_, ctx, 1)
        self.assertIn(self.ancient.mention, ctx.sent[-1]["embed"].description)
        await self.cog_.members_oldest.callback(self.cog_, ctx, 3)
        self.assertEqual(ctx.sent[-1]["embed"].description.count("\n"), 2)

    async def test_ranking_needs_manage_server(self):
        for command in (self.cog_.members_youngest, self.cog_.members_oldest, self.cog_.members_newusers, self.cog_.members_oldusers):
            self.assertEqual(command.callback.__required_permissions__, ("manage_guild",))

    async def test_avatars(self):
        ctx = self.context()
        await self.cog_.avatars.callback(self.cog_, ctx, self.member)
        self.assertEqual(ctx.sent[-1]["embed"].image.url, self.member.display_avatar.url)
        await self.cog_.avatars.callback(self.cog_, ctx, None, True)
        self.assertEqual(ctx.sent[-1]["embed"].image.url, self.admin.avatar.url)


class StatsTest(BotTest):
    modules = [Utilities]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Utilities)
        day = int(time.time() // 86400)
        other = FakeGuild("Other")
        rows = [
            (self.guild.id, day, "warn", self.mod.id, 5),
            (self.guild.id, day, "ban", self.mod.id, 2),
            (self.guild.id, day - 10, "warn", self.admin.id, 7),
            (self.guild.id, day - 100, "ping", self.admin.id, 1),
            (other.id, day, "tag", self.admin.id, 50),
        ]
        await self.db.many("INSERT INTO command_uses (guild_id, day, command, user_id, uses) VALUES (?, ?, ?, ?, ?)", rows)

    async def test_usage_counts_this_server_only(self):
        ctx = self.context()
        await self.cog_.stats.callback(self.cog_, ctx)
        embed = ctx.sent[-1]["embed"]
        self.assertIn("15 commands", embed.description)
        self.assertIn("`warn`: 12", embed.fields[0].value)
        self.assertNotIn("tag", embed.fields[0].value)
        self.assertIn(self.admin.mention, embed.fields[1].value)

    async def test_usage_by_member_and_period(self):
        ctx = self.context()
        await self.cog_.stats.callback(self.cog_, ctx, self.admin, "all")
        self.assertIn("8 commands", ctx.sent[-1]["embed"].description)
        await self.cog_.stats.callback(self.cog_, ctx, None, "month")
        self.assertIn("14 commands", ctx.sent[-1]["embed"].description)
        await self.cog_.stats.callback(self.cog_, ctx, None, "today")
        self.assertIn("7 commands", ctx.sent[-1]["embed"].description)
        await self.cog_.stats_today.callback(self.cog_, ctx, member=self.mod)
        self.assertIn("7 commands", ctx.sent[-1]["embed"].description)

    async def test_global_counts_all_servers_and_names_no_people(self):
        ctx = self.context()
        await self.cog_.stats_global.callback(self.cog_, ctx)
        embed = ctx.sent[-1]["embed"]
        self.assertIn("65 commands", embed.description)
        self.assertEqual(len(embed.fields), 1)

    async def test_uptime_and_serverinfo(self):
        ctx = self.context()
        await self.cog_.stats_uptime.callback(self.cog_, ctx)
        self.assertIn("Up for", ctx.sent[-1]["content"])
        await self.cog_.stats_serverinfo.callback(self.cog_, ctx)
        fields = {f.name: f.value for f in ctx.sent[-1]["embed"].fields}
        self.assertEqual(fields["ID"], str(self.guild.id))
        self.assertIn("1 text", fields["Channels"])
        self.assertEqual(fields["Roles"], str(len(self.guild.roles)))


class DumpTest(BotTest):
    modules = [Utilities]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Utilities)
        self.bare = FakeMember(self.guild, "aaa-bare", joined=NOW - datetime.timedelta(days=9), created=NOW - datetime.timedelta(days=900))
        self.bare.nick = "Zed"
        self.multi = self.guild.role("Two Words", 3)
        self.both = FakeMember(self.guild, "zzz-both", self.role_member, self.multi)

    async def run_dump(self, query="", role=None):
        ctx = self.context()
        await self.cog_.stats_dump.callback(self.cog_, ctx, role, query=query)
        return ctx

    async def test_default_output_is_full_name_and_id(self):
        ctx = await self.run_dump(f"--has-roles {self.role_mod.id}")
        self.assertIn(f"mod ({self.mod.id})", ctx.sent[-1]["content"])

    async def test_the_documented_example_with_role_enumerate_order_format_and_limit(self):
        ctx = await self.run_dump("--enumerate --order id -f %n %c --dateformat %c --limit 1", role=self.role_member)
        expected = f"1. member {self.member.created_at.strftime('%c')}"
        self.assertIn(expected, ctx.sent[-1]["content"])
        self.assertNotIn("zzz-both", ctx.sent[-1]["content"])

    async def test_no_roles_flag(self):
        ctx = await self.run_dump("-o id --limit 10 --no-roles")
        text = ctx.sent[-1]["content"]
        self.assertIn("aaa-bare", text)
        self.assertNotIn("mod (", text)

    async def test_descending_join_date_limit_one_by_role_name(self):
        ctx = await self.run_dump("-o joined_at -d --limit 1 -r Moderator")
        self.assertIn(f"mod ({self.mod.id})", ctx.sent[-1]["content"])

    def test_every_flag_parses(self):
        options = parse_dump_query("--format %i --desc --has-roles a b --order nick --limit 3 --has-all-roles c --no-roles --enumerate --separator , --dateformat %Y")
        self.assertEqual(options.format, "%i")
        self.assertTrue(options.desc and options.no_roles and options.enumerate)
        self.assertEqual(options.has_roles, ["a", "b"])
        self.assertEqual(options.has_all_roles, ["c"])
        self.assertEqual((options.order, options.limit, options.separator, options.dateformat), ("nick", 3, ",", "%Y"))
        short = parse_dump_query("-f %n -d -r a -o id -l 2 -e -s ;")
        self.assertEqual((short.format, short.desc, short.has_roles, short.order, short.limit, short.enumerate, short.separator), ("%n", True, ["a"], "id", 2, True, ";"))

    def test_bad_flags_are_refused_with_a_reason(self):
        for query in ("--nope", "-o sideways", "-l zero", "-l 0", "--format"):
            with self.assertRaises(DumpError, msg=query):
                parse_dump_query(query)

    async def test_has_all_roles_and_quoted_or_unquoted_names(self):
        ctx = await self.run_dump(f'--has-all-roles {self.role_member.id} "Two Words"')
        self.assertIn("zzz-both", ctx.sent[-1]["content"])
        self.assertNotIn("member (", ctx.sent[-1]["content"])
        ctx = await self.run_dump("-r Two Words")
        self.assertIn("zzz-both", ctx.sent[-1]["content"])

    async def test_a_role_that_does_not_exist_is_refused(self):
        ctx = await self.run_dump("-r Nonexistent")
        self.assertIn("can't find a role", ctx.fails[0])

    async def test_order_by_nick_and_separator(self):
        options = parse_dump_query("-o nick -d")
        chosen = select_members(self.guild, None, options)
        self.assertEqual(chosen[0], self.bare)
        ctx = await self.run_dump("-o id -l 2 -s \\t -f %i")
        self.assertIn(f"{self.me.id}\t", ctx.sent[-1]["content"])

    def test_a_name_holding_a_variable_is_not_expanded(self):
        tricky = FakeMember(self.guild, "%i")
        self.assertEqual(dump_line(tricky, parse_dump_query("-f %u %i")), f"%i {tricky.id}")
        self.assertEqual(dump_line(tricky, parse_dump_query("-f %%i")), "%i")

    async def test_dates_use_the_date_format_and_unknown_joins_say_so(self):
        ctx = await self.run_dump(f'-r {self.role_mod.id} -f %j --dateformat %Y')
        self.assertIn(str(self.mod.joined_at.year), ctx.sent[-1]["content"])
        self.mod.joined_at = None
        ctx = await self.run_dump(f"-r {self.role_mod.id} -f %j -o joined_at")
        self.assertIn("unknown", ctx.sent[-1]["content"])

    async def test_long_output_and_code_fences_arrive_as_a_file(self):
        self.mod.nick = "```break out"
        ctx = await self.run_dump(f"-r {self.role_mod.id} -f %n")
        self.assertIn("file", ctx.sent[-1])
        for number in range(150):
            FakeMember(self.guild, f"filler-{number:03d}-" + "x" * 20)
        ctx = await self.run_dump("--no-roles")
        self.assertIn("file", ctx.sent[-1])

    async def test_nothing_matching_says_so(self):
        ctx = await self.run_dump("--no-roles", role=self.role_mod)
        self.assertIn("No members", ctx.sent[-1]["content"])

    async def test_needs_manage_roles(self):
        self.assertEqual(self.cog_.stats_dump.callback.__required_permissions__, ("manage_roles",))


class MiscTest(BotTest):
    modules = [Utilities]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Utilities)

    async def test_charinfo(self):
        ctx = self.context()
        await self.cog_.misc_charinfo.callback(self.cog_, ctx, characters="a\N{SNOWMAN}")
        text = ctx.sent[-1]["content"]
        self.assertIn("U+0061", text)
        self.assertIn("LATIN SMALL LETTER A", text)
        self.assertIn("SNOWMAN", text)

    async def test_charinfo_limit_is_15(self):
        ctx = self.context()
        await self.cog_.misc_charinfo.callback(self.cog_, ctx, characters="a" * 16)
        self.assertTrue(ctx.fails)

    async def test_permissions_for_a_member_and_the_bot(self):
        ctx = self.context()
        await self.cog_.misc_permissions.callback(self.cog_, ctx, self.mod, self.channel)
        fields = {f.name: f.value for f in ctx.sent[-1]["embed"].fields}
        self.assertIn("Manage Messages", fields["Allowed"])
        self.assertIn("Ban Members", fields["Denied"])
        await self.cog_.misc_botpermissions.callback(self.cog_, ctx)
        fields = {f.name: f.value for f in ctx.sent[-1]["embed"].fields}
        self.assertIn("Ban Members", fields["Allowed"])

    async def test_botpermissions_needs_manage_roles(self):
        self.assertEqual(self.cog_.misc_botpermissions.callback.__required_permissions__, ("manage_roles",))


class RealLibraryTest(GatewayTest):
    """The same flows through the real library with only the network faked, so a wrong attribute or keyword can't hide behind a fake."""
    modules = [Utilities]

    def calls(self, method, path):
        return [call for call in self.net.calls if call[0] == method and call[1] == path]

    async def say(self, content, user_id=None):
        self.dispatch_message(user_id=user_id or self.owner_id, content=content)
        await self.settle()
        self.assert_no_errors(content)

    def latest_with_button(self):
        return next(m for m in reversed(self.net.posted) if m.get("components"))

    async def press(self, message, user_id, label=None):
        buttons = [c for row in message["components"] for c in row["components"]]
        button = next(c for c in buttons if label is None or c.get("label") == label)
        data = {"custom_id": button["custom_id"], "component_type": 2}
        self.bot._connection.parse_interaction_create(self.interaction_payload(3, data, user_id=user_id, message=message))
        await self.settle()
        self.assert_no_errors(f"pressing {button.get('label')}")

    async def test_a_highlight_reaches_a_real_dm(self):
        utilities_ = next(m for m in self.bot.modules if isinstance(m, Utilities))

        async def delivered():
            while utilities_._tasks:
                await asyncio.gather(*list(utilities_._tasks), return_exceptions=True)
            await self.settle()

        with mock.patch.object(utilities, "HIGHLIGHT_PATIENCE", 0):
            await self.say("!highlight add printer", self.admin_id)
            self.assertEqual(len(self.calls("POST", "/users/@me/channels")), 1)
            self.assertIn("come here", next(m for m in self.net.posted if (m.get("content") or "").startswith("Highlight notices"))["content"])
            await self.say("my printer broke", self.member_id)
            await delivered()
            self.assert_no_errors("the notice")
            dm = self.net.posted[-1]
            self.assertIn("**printer** came up in", dm["content"])
            self.assertIn("my **printer** broke", dm["embeds"][0]["description"])
            button = dm["components"][0]["components"][0]
            self.assertEqual((button["label"], button["style"]), ("Go to message", 5))
            self.assertIn(f"/channels/{self.guild_id}/{self.text['id']}/", button["url"])
        for line in ("!highlight matches the printers", "!highlight", f"!highlight block <@{self.member_id}>", f"!highlight unblock <@{self.member_id}>",
                     "!highlight clearblocks", "!highlight pause 2h", "!highlight", "!highlight resume", "!hl dnd", "!hl enable", "!highlight import nowhere",
                     "!highlight remove printer", "!highlight clear"):
            await self.say(line, self.admin_id)
        deleted = self.calls("DELETE", "/channels/{channel_id}/messages/{message_id}")
        self.assertGreaterEqual(len(deleted), 14, "every typed highlight command comes down")

    async def test_a_reminder_is_delivered(self):
        await self.say("!reminder in 1 hour stretch your legs", self.member_id)
        row = await self.db.one("SELECT * FROM reminders")
        self.assertEqual(row["about"], "stretch your legs")
        self.assertIn(str(row["channel_id"]), self.text["id"])
        await self.say("!reminder mine", self.member_id)
        await self.say(f"!reminder when {row['id']}", self.owner_id)
        await self.say(f"!reminder subscribe {row['id']}", self.owner_id)
        await self.say(f"!reminder repeat {row['id']} 1 day", self.member_id)
        await self.bot.timers.run_due(time.time() + 3601)
        await self.settle()
        self.assert_no_errors("delivering")
        dms = [m for m in self.net.posted if m.get("embeds") and m["embeds"][0].get("title") == "Reminder"]
        self.assertEqual(len(dms), 2)
        await self.say(f"!reminder cancel {row['id']}", self.member_id)
        await self.say("!reminder clear", self.owner_id)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM reminders"), 0)

    async def test_a_giveaway_from_start_to_reroll(self):
        await self.say("!giveaway create 1h 1 A shiny printer")
        message = self.latest_with_button()
        row = await self.db.one("SELECT * FROM giveaways")
        self.assertEqual(row["prize"], "A shiny printer")
        self.assertEqual(str(row["message_id"]), message["id"])
        await self.press(message, self.member_id)
        await self.press(message, self.admin_id)
        await self.press(message, self.mod_id)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM giveaway_entries"), 3)
        await self.say(f"!giveaway participants {row['id']}")
        await self.say("!giveaway list")
        await self.bot.timers.run_due(time.time() + 3601)
        await self.settle()
        self.assert_no_errors("ending")
        ended = await self.db.one("SELECT * FROM giveaways")
        winner = json.loads(ended["winner_ids"])[0]
        announced = self.calls("POST", "/channels/{channel_id}/messages")[-1]
        self.assertIn(f"<@{winner}>", announced[2]["content"])
        self.assertEqual(announced[2]["allowed_mentions"]["users"], [winner])
        self.assertEqual(announced[2]["allowed_mentions"]["parse"], [])
        edit = self.calls("PATCH", "/channels/{channel_id}/messages/{message_id}")[-1]
        self.assertEqual(edit[2]["components"], [])
        await self.say(f"!giveaway reroll {row['id']} <@{winner}>")
        rerolled = json.loads((await self.db.one("SELECT winner_ids FROM giveaways"))["winner_ids"])
        self.assertNotIn(winner, rerolled)
        await self.say("!giveaway list inactive")

    async def test_ending_a_giveaway_early_through_its_question(self):
        await self.say("!giveaway create 1h 1 Quiet prize")
        message = self.latest_with_button()
        row = await self.db.one("SELECT * FROM giveaways")
        await self.press(message, self.member_id)
        await self.say(f"!giveaway end {row['id']}")
        question = self.latest_with_button()
        await self.press(question, self.owner_id, "End without announcing")
        stored = await self.db.one("SELECT * FROM giveaways")
        self.assertIsNotNone(stored["ended_at"])
        self.assertEqual(json.loads(stored["winner_ids"]), [self.member_id])
        self.assertEqual(stored["announced"], 1)

    async def test_a_poll_and_its_reactions(self):
        await self.say("!poll Best color? red, blue, green")
        self.assertEqual(len([c for c in self.net.calls if c[0] == "PUT" and "/reactions/" in c[1]]), 3)
        self.assertIn("Best color?", self.calls("POST", "/channels/{channel_id}/messages")[-2][2]["embeds"][0]["description"])

    async def test_a_temporary_channel_comes_and_goes(self):
        await self.say("!tempchannel 1h Planning Chat")
        made = self.calls("POST", "/guilds/{guild_id}/channels")[-1]
        self.assertEqual(made[2]["name"], "planning-chat")
        row = await self.db.one("SELECT * FROM temp_channels")
        self.assertIsNotNone(self.guild.get_channel(row["channel_id"]))
        await self.bot.timers.run_due(time.time() + 3601)
        await self.settle()
        self.assert_no_errors("deleting")
        self.assertEqual(len(self.calls("DELETE", "/channels/{channel_id}")), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM temp_channels"), 0)

    async def test_lookups_with_real_members_and_channels(self):
        self.bot.owner_id = self.owner_id  # /stats global asks who owns the bot, which the stand-in for Discord doesn't answer
        for text in (
            "!members", f"!members <@{self.member_id}>", f"!members info <@{self.member_id}>", "!members youngest 2", "!members oldest", "!members newusers", "!members oldusers 3",
            "!avatars", f"!avatars <@{self.member_id}> true", "!avatar", "!stats", f"!stats <@{self.member_id}> week", "!stats usage", "!stats today", "!stats global",
            "!stats uptime", "!stats serverinfo", "!misc", "!misc charinfo a!", "!misc permissions", f"!misc permissions <@{self.member_id}> <#{self.text['id']}>",
            "!misc botpermissions", "!misc invite",
        ):
            await self.say(text)
        await self.settle()

    async def test_dump_with_real_members(self):
        await self.say("!stats dump -o id --limit 2 -e -f %u %n %i %c %j --dateformat %Y")
        text = self.net.posted[-1]["content"]
        self.assertIn("1. ", text)
        self.assertIn(str(self.bot_id), text)
        await self.say(f"!stats dump <@&{self.role_member['id']}> -f %n")
        self.assertIn("member", self.net.posted[-1]["content"])
        await self.say("!stats dump --no-roles")
        self.assertIn("No members", self.net.posted[-1]["content"])
        await self.say("!stats dump --nonsense")
        self.assertIn("flag", self.net.posted[-1]["content"])

    async def test_every_name_a_group_answers_to_in_slash_is_really_registered(self):
        def names(root):
            return {o["name"] for o in self.bot.tree.get_command(root).to_dict(self.bot.tree)["options"]}

        self.assertLessEqual({"info", "youngest", "oldest", "newusers", "oldusers"}, names("members"))
        self.assertLessEqual({"usage", "today", "global", "uptime", "serverinfo", "dump"}, names("stats"))
        self.assertLessEqual({"show", "add", "remove", "clear", "matches", "block", "unblock", "clearblocks", "pause", "resume", "import"}, names("highlight"))
        self.assertLessEqual({"remindme", "mine", "cancel", "subscribe", "clear", "repeat", "when"}, names("reminder"))
        self.assertLessEqual({"create", "reroll", "end", "list", "participants"}, names("giveaway"))
        self.assertLessEqual({"charinfo", "permissions", "botpermissions", "invite"}, names("misc"))

    async def test_the_slash_fallback_runs_and_the_same_words_work_with_a_prefix(self):
        root = self.bot.tree.get_command("members").to_dict(self.bot.tree)
        leaf = next(o for o in root["options"] if o["name"] == "info")
        self.assertEqual([o["name"] for o in leaf["options"]], ["member"])
        data = {"id": str(new_id()), "name": "members", "type": 1, "options": [{"name": "info", "type": 1, "options": []}], "resolved": {}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.member_id))
        await self.settle()
        self.assert_no_errors("/members info")
        self.assertEqual(self.net.posted[-1]["embeds"][0]["fields"][0]["value"], str(self.member_id))
        await self.say(f"!members info <@{self.admin_id}>")
        self.assertEqual(self.net.posted[-1]["embeds"][0]["fields"][0]["value"], str(self.admin_id))
        await self.say("!stats usage")
        self.assertIn("Command use", self.net.posted[-1]["embeds"][0]["title"])
        await self.say("!highlight show", self.admin_id)
        self.assertIn("don't follow", self.net.posted[-1]["content"])
        await self.say("!reminder remindme call bob tomorrow")
        self.assertEqual((await self.db.one("SELECT about FROM reminders"))["about"], "call bob")

    async def test_turning_off_a_fallback_name_covers_slash_and_prefix_and_leaves_the_rest(self):
        await self.bot.settings.save(self.guild_id, "commands", {"overwrites": {"members": {"disabled_subcommands": ["members info"]}}})
        posted = len(self.net.posted)
        data = {"id": str(new_id()), "name": "members", "type": 1, "options": [{"name": "info", "type": 1, "options": []}], "resolved": {}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.owner_id))
        await self.settle()
        self.assert_no_errors("/members info")
        self.assertIn("turned off", self.net.posted[-1]["content"])
        await self.say("!members info")
        self.assertIn("turned off", self.net.posted[-1]["content"])
        await self.say("!members")
        self.assertIn("turned off", self.net.posted[-1]["content"])
        self.assertEqual(len([m for m in self.net.posted[posted:] if m.get("embeds")]), 0)
        await self.say("!members youngest 1")
        self.assertTrue(self.net.posted[-1]["embeds"])

    async def test_avatar_links_on_a_real_asset(self):
        asset = discord.Asset._from_avatar(self.bot._connection, self.member_id, "abcdef")
        links = Utilities._avatar_links(asset)
        self.assertEqual(len(links), 3)
        self.assertIn("format=png", links[0].replace(".png", "format=png"))
        animated = discord.Asset._from_avatar(self.bot._connection, self.member_id, "a_abcdef")
        self.assertEqual(len(Utilities._avatar_links(animated)), 4)
