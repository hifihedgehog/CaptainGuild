"""Reminders, highlights, polls, temporary channels and the fun commands, for someone with less reach than the bot, and after a restore."""
import datetime
import json
import time
import types
from unittest import mock

import discord
from discord.ext import commands

from captainguild.core.module import required_permissions
from captainguild.modules.fun import Fun
from captainguild.modules.utilities import MAX_REMINDERS, DumpError, Utilities, _find_roles, parse_dump_query, parse_moment
from tests.fakes import BotTest, FakeChannel, FakeMember, FakeMessage, FakePermissions, _FakeResponse

EVERYDAY = ("view_channel", "send_messages", "read_message_history", "send_messages_in_threads", "add_reactions")
NO_POST = "You can't post in that channel."
NOW = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)


class Press:
    def __init__(self, user, guild, message):
        self.user = user
        self.guild_id = guild.id
        self.guild = guild
        self.message = message
        self.sent = []
        self.response = types.SimpleNamespace(send_message=self._send, edit_message=self._send, defer=self._send)

    async def _send(self, content=None, **kwargs):
        self.sent.append(content)


class Case(BotTest):
    modules = [Utilities, Fun]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.utils = self.cog(Utilities)
        self.fun = self.cog(Fun)
        self.person = FakeMember(self.guild, "person", self.guild.role("Person", 2, *EVERYDAY))
        self.friend = FakeMember(self.guild, "friend", self.guild.role("Friend", 2, *EVERYDAY))
        self.manager = FakeMember(self.guild, "manager", self.guild.role("Manager", 3, "manage_guild", *EVERYDAY))
        self.hidden = self.closed("staff")
        self.readonly = self.closed("news", "view_channel", "read_message_history")

    def closed(self, name, *allowed):
        """A channel where everyone below an Administrator holds only these permissions."""
        channel = FakeChannel(self.guild, name)
        real = channel.permissions_for

        def permissions(member):
            found = real(member)
            return found if found.administrator else FakePermissions(*allowed)
        channel.permissions_for = permissions
        return channel

    def ctx(self, author=None, channel=None):
        return self.context(author or self.person, channel or self.channel)

    async def reminder(self, author=None, channel=None, about="in 1 hour call bob", typed=True):
        ctx = self.ctx(author, channel)
        if not typed:
            ctx.interaction = object()
        await self.utils.reminder.callback(self.utils, ctx, about=about)
        return await self.db.one("SELECT * FROM reminders ORDER BY id DESC LIMIT 1")


class ClosedDmsTest(Case):
    def close_dms(self, member):
        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "Cannot send messages to this user")
        member.send = refuse

    async def test_the_fallback_never_posts_their_text(self):
        row = await self.reminder(about="in 1 hour [free nitro](https://evil.example)")
        self.close_dms(self.person)
        await self.utils._deliver_reminder(row)
        sent = self.channel.sent[-1]
        self.assertIn("DMs", sent.content)
        self.assertNotIn("nitro", sent.content)
        self.assertEqual(sent.embeds, [])
        self.assertEqual(sent.kwargs["allowed_mentions"].users, [self.person])

    async def test_no_post_in_a_channel_they_cant_post_in(self):
        for channel in (self.readonly, self.hidden):
            row = await self.reminder(channel=channel)
            self.close_dms(self.person)
            await self.utils._deliver_reminder(row)
            self.assertEqual(channel.sent, [])

    async def test_no_post_for_someone_no_longer_in_the_server(self):
        row = await self.reminder()
        self.close_dms(self.person)
        self.guild.members.remove(self.person)
        with mock.patch.object(self.bot, "get_user", return_value=self.person):
            await self.utils._deliver_reminder(row)
        self.assertEqual(self.channel.sent, [])

    async def test_a_copy_goes_by_the_right_of_the_person_who_copied_it(self):
        row = await self.reminder(self.admin, self.hidden)
        await self.db.run("UPDATE reminders SET user_id = ? WHERE id = ?", (self.person.id, row["id"]))
        row = await self.db.one("SELECT * FROM reminders WHERE id = ?", (row["id"],))
        self.close_dms(self.person)
        await self.utils._deliver_reminder(row)
        self.assertEqual(self.hidden.sent, [])

    async def test_open_dms_get_the_whole_text(self):
        row = await self.reminder()
        await self.utils._deliver_reminder(row)
        self.assertEqual(self.person.dms[0]["embed"].description, "call bob")


class ReadingRemindersTest(Case):
    async def when(self, author, reminder_id, channel=None, *, interaction=None):
        ctx = self.ctx(author, channel)
        ctx.interaction = interaction
        await self.utils.reminder_when.callback(self.utils, ctx, reminder_id)
        return ctx

    async def test_a_channel_they_cant_read_hides_it(self):
        row = await self.reminder(self.admin, self.hidden)
        ctx = await self.when(self.person, row["id"])
        self.assertEqual(ctx.sent, [])
        self.assertEqual(ctx.fails, ["There's no reminder with that ID here."])
        ctx = self.ctx()
        await self.utils.reminder_subscribe.callback(self.utils, ctx, row["id"])
        self.assertEqual(ctx.fails, ["There's no reminder with that ID here."])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM reminders"), 1)

    async def test_a_reminder_made_with_the_slash_command_stays_private(self):
        row = await self.reminder(self.friend, typed=False)
        ctx = await self.when(self.person, row["id"])
        self.assertEqual(ctx.fails, ["There's no reminder with that ID here."])
        ctx = self.ctx()
        await self.utils.reminder_subscribe.callback(self.utils, ctx, row["id"])
        self.assertEqual(ctx.fails, ["There's no reminder with that ID here."])

    async def test_a_reminder_typed_in_the_open_can_be_read_where_it_was_typed(self):
        row = await self.reminder(self.friend)
        ctx = await self.when(self.person, row["id"])
        self.assertEqual(ctx.sent[0]["embed"].description, "call bob")

    async def test_the_owner_always_reads_their_own(self):
        row = await self.reminder(self.admin, self.hidden, typed=False)
        ctx = await self.when(self.admin, row["id"])
        self.assertEqual(ctx.sent[0]["embed"].description, "call bob")

    async def test_a_channel_that_is_gone_hides_it(self):
        row = await self.reminder(self.friend)
        self.guild.channels.remove(self.channel)
        ctx = await self.when(self.person, row["id"], FakeChannel(self.guild, "other"))
        self.assertEqual(ctx.fails, ["There's no reminder with that ID here."])

    async def test_a_typed_question_elsewhere_cant_carry_it_into_another_channel(self):
        row = await self.reminder(self.friend)
        elsewhere = FakeChannel(self.guild, "elsewhere")
        ctx = await self.when(self.person, row["id"], elsewhere)
        self.assertEqual(ctx.sent, [])
        self.assertEqual(len(ctx.fails), 1)
        ctx = await self.when(self.person, row["id"], elsewhere, interaction=object())
        self.assertEqual(ctx.sent[0]["embed"].description, "call bob", "only the person asking sees a slash answer")


class ReminderCapTest(Case):
    async def test_subscribing_counts_toward_the_cap(self):
        row = await self.reminder(self.friend)
        for _ in range(MAX_REMINDERS):
            await self.db.run("INSERT INTO reminders (user_id, about, created_at, due_at) VALUES (?, 'x', 1, 2)", (self.person.id,))
        ctx = self.ctx()
        await self.utils.reminder_subscribe.callback(self.utils, ctx, row["id"])
        self.assertIn("reminders waiting", ctx.fails[0])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM reminders WHERE copied_from = ?", (row["id"],)), 0)

    async def test_repeating_reminders_have_their_own_cap(self):
        for _ in range(10):
            await self.db.run("INSERT INTO reminders (user_id, about, created_at, due_at, repeat_seconds) VALUES (?, 'x', 1, 2, 3600)", (self.person.id,))
        mine = await self.reminder()
        ctx = self.ctx()
        await self.utils.reminder_repeat.callback(self.utils, ctx, mine["id"], duration="1 day")
        self.assertIn("repeating", ctx.fails[0])
        source = await self.reminder(self.friend)
        await self.db.run("UPDATE reminders SET repeat_seconds = 3600 WHERE id = ?", (source["id"],))
        ctx = self.ctx()
        await self.utils.reminder_subscribe.callback(self.utils, ctx, source["id"])
        self.assertIn("repeating", ctx.fails[0])

    async def test_changing_a_repeat_that_exists_is_still_open_at_the_cap(self):
        mine = await self.reminder()
        await self.db.run("UPDATE reminders SET repeat_seconds = 3600 WHERE id = ?", (mine["id"],))
        for _ in range(9):
            await self.db.run("INSERT INTO reminders (user_id, about, created_at, due_at, repeat_seconds) VALUES (?, 'x', 1, 2, 3600)", (self.person.id,))
        ctx = self.ctx()
        await self.utils.reminder_repeat.callback(self.utils, ctx, mine["id"], duration="1 day")
        self.assertEqual(ctx.fails, [])

    async def test_nothing_repeats_faster_than_once_an_hour(self):
        mine = await self.reminder()
        ctx = self.ctx()
        await self.utils.reminder_repeat.callback(self.utils, ctx, mine["id"], duration="30 minutes")
        self.assertEqual(len(ctx.fails), 1)

    def test_a_length_past_any_date_reads_as_no_date(self):
        self.assertIsNone(parse_moment("in 100000 years", NOW))

    async def test_a_length_past_any_date_gets_a_sentence(self):
        ctx = self.ctx()
        await self.utils.reminder.callback(self.utils, ctx, about="call bob", when="in 100000 years")
        self.assertEqual(len(ctx.fails), 1)


class HighlightReachTest(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.thread_kind = discord.ChannelType.private_thread

    def thread(self, *members, parent_missing=False):
        thread = FakeChannel(self.guild, "thread", self.thread_kind)
        thread.get_member = lambda user_id: next((m for m in members if m.id == user_id), None)
        if parent_missing:
            def no_parent(member):
                raise discord.ClientException("Parent channel not found")
            thread.permissions_for = no_parent
        return thread

    def viewer(self):
        return FakeMember(self.guild, "viewer", self.guild.role("Viewer", 2, "view_channel", "manage_messages", "read_message_history"))

    async def test_a_private_thread_needs_being_in_it_or_manage_threads(self):
        viewer = self.viewer()
        self.assertFalse(self.utils._highlight_allowed(viewer, self.thread()))
        self.assertTrue(self.utils._highlight_allowed(viewer, self.thread(viewer)))
        keeper = FakeMember(self.guild, "keeper", self.guild.role("Keeper", 2, "view_channel", "manage_messages", "manage_threads"))
        self.assertTrue(self.utils._highlight_allowed(keeper, self.thread()))

    async def test_a_public_channel_still_works(self):
        self.assertTrue(self.utils._highlight_allowed(self.viewer(), self.channel))

    async def test_a_thread_with_no_cached_parent_is_no(self):
        self.assertFalse(self.utils._highlight_allowed(self.viewer(), self.thread(parent_missing=True)))

    async def test_earlier_lines_need_read_message_history(self):
        reader = self.viewer()
        looker = FakeMember(self.guild, "looker", self.guild.role("Looker", 2, "view_channel", "manage_messages"))
        await self.channel.send("the earlier secret")
        message = FakeMessage(self.channel, self.person, "the word")
        self.assertIn("earlier secret", await self.utils._highlight_context(message, reader))
        made = await self.utils._highlight_context(message, looker)
        self.assertNotIn("earlier secret", made)
        self.assertIn("the word", made)


class MiscPermissionsTest(Case):
    async def run_it(self, command, author, channel, words=""):
        ctx = self.ctx(author)
        ctx.view = commands.view.StringView(words)
        if command is self.utils.misc_permissions:
            await command.callback(self.utils, ctx, None, channel)
        else:
            await command.callback(self.utils, ctx, channel=channel)
        return ctx

    async def test_a_hidden_channel_reads_like_one_that_isnt_there(self):
        hidden = await self.run_it(self.utils.misc_permissions, self.person, self.hidden)
        unknown = await self.run_it(self.utils.misc_permissions, self.person, None, "no-such-channel")
        self.assertEqual(hidden.sent, [])
        self.assertEqual(unknown.sent, [])
        self.assertEqual(hidden.fails, unknown.fails)
        self.assertNotIn("staff", hidden.fails[0])

    async def test_a_channel_they_can_see_gets_its_answer(self):
        ctx = await self.run_it(self.utils.misc_permissions, self.person, self.readonly)
        self.assertEqual(ctx.fails, [])
        self.assertEqual(len(ctx.sent), 1)

    async def test_the_bots_permissions_ask_the_same(self):
        manager = FakeMember(self.guild, "roles", self.guild.role("Roles", 3, "manage_roles"))
        ctx = await self.run_it(self.utils.misc_botpermissions, manager, self.hidden)
        self.assertEqual(ctx.sent, [])
        self.assertEqual(len(ctx.fails), 1)


class PostingTest(Case):
    async def test_poll_defers_and_says_what_failed(self):
        ctx = self.ctx(self.manager)
        ctx.defer = mock.AsyncMock()
        real = FakeMessage.add_reaction

        async def refuse(message, emoji):
            raise discord.HTTPException(_FakeResponse(400), "Too many reactions")
        with mock.patch.object(FakeMessage, "add_reaction", refuse):
            await self.utils.poll.callback(self.utils, ctx, question="q", choices="a|b")
        self.assertIs(FakeMessage.add_reaction, real)
        ctx.defer.assert_awaited_once()
        self.assertEqual(ctx.oks, [])
        self.assertIn("reaction", ctx.fails[0])

    async def test_a_poll_choice_has_a_limit_so_none_are_cut_off(self):
        ctx = self.ctx(self.manager)
        await self.utils.poll.callback(self.utils, ctx, question="q" * 1000, choices="|".join("c" * 151 for _ in range(2)))
        self.assertIn("150", ctx.fails[0])
        ctx = self.ctx(self.manager)
        choices = [f"{index:02d}" + "c" * 148 for index in range(20)]
        await self.utils.poll.callback(self.utils, ctx, question="q" * 1000, choices="|".join(choices))
        description = self.channel.sent[-1].embeds[0].description
        self.assertTrue(all(choice in description for choice in choices))

    async def test_a_poll_in_a_read_only_channel_is_refused(self):
        ctx = self.ctx(self.manager, self.readonly)
        await self.utils.poll.callback(self.utils, ctx, question="q", choices="a|b")
        self.assertEqual(ctx.fails, [NO_POST])
        self.assertEqual(self.readonly.sent, [])

    async def test_a_giveaway_in_a_read_only_channel_gets_the_same_sentence(self):
        ctx = self.ctx(self.manager)
        await self.utils.giveaway_create.callback(self.utils, ctx, 3600, 1, prize="p", channel=self.readonly)
        self.assertEqual(ctx.fails, [NO_POST])

    async def test_eightball_keeps_their_words_out_of_a_channel_they_cant_post_in(self):
        ctx = self.ctx(self.person, self.readonly)
        await self.fun.fun_eightball.callback(self.fun, ctx, question="[free](https://evil.example)")
        self.assertNotIn("free", ctx.sent[0]["content"])
        self.assertTrue(ctx.sent[0]["ephemeral"])
        ctx = self.ctx(self.person)
        await self.fun.fun_eightball.callback(self.fun, ctx, question="will it rain")
        self.assertIn("will it rain", ctx.sent[0]["content"])

    async def test_the_text_toys_ask_the_same(self):
        ctx = self.ctx(self.manager, self.readonly)
        await self.fun.fun_clap.callback(self.fun, ctx, sentence="free nitro")
        self.assertEqual(ctx.fails, [NO_POST])
        self.assertEqual(ctx.sent, [])
        ctx = self.ctx(self.manager, self.readonly)
        await self.fun.fun_pick.callback(self.fun, ctx, sentence="free, nitro")
        self.assertEqual(ctx.fails, [NO_POST])
        self.assertEqual(ctx.sent, [])

    async def test_echo_gets_the_same_sentence(self):
        ctx = self.ctx(self.manager)
        await self.fun.echo.callback(self.fun, ctx, self.readonly, message="x")
        self.assertEqual(ctx.fails, [NO_POST])
        self.assertEqual(self.readonly.sent, [])

    def test_the_commands_ask_for_the_permission_they_use(self):
        self.assertEqual(required_permissions(self.utils.tempchannel), ("manage_channels",))
        self.assertEqual(required_permissions(self.fun.fun_addemoji), ("manage_expressions",))

    async def test_temporary_channels_have_a_limit(self):
        for index in range(25):
            await self.db.run("INSERT INTO temp_channels (channel_id, guild_id, created_by, created_at, expires_at) VALUES (?, ?, 1, 1, 2)", (index + 1, self.guild.id))
        ctx = self.ctx(self.admin)
        before = len(self.guild.channels)
        await self.utils.tempchannel.callback(self.utils, ctx, 3600, name="more")
        self.assertIn("25", ctx.fails[0])
        self.assertEqual(len(self.guild.channels), before)

    async def test_echo_with_no_modlog_channel_left_keeps_the_button(self):
        self.bot.settings.schemas["moderation"] = None
        with mock.patch.object(self.bot.settings, "get", return_value={"modlog_channel": 12345}):
            self.assertIsNone(self.fun._modlog_channel(self.guild.id))
        with mock.patch.object(self.bot.settings, "get", return_value={"modlog_channel": self.channel.id}):
            self.assertEqual(self.fun._modlog_channel(self.guild.id), self.channel.id)
        del self.bot.settings.schemas["moderation"]

    async def test_a_modlog_the_bot_cant_post_in_counts_as_none(self):
        self.bot.settings.schemas["moderation"] = None
        self.channel.permissions_for = lambda member: FakePermissions("view_channel")
        with mock.patch.object(self.bot.settings, "get", return_value={"modlog_channel": self.channel.id}):
            self.assertIsNone(self.fun._modlog_channel(self.guild.id))
        del self.bot.settings.schemas["moderation"]


class StatsTest(Case):
    async def test_global_totals_are_for_the_bots_owner(self):
        ctx = self.ctx()
        ctx.bot = self.bot
        with mock.patch.object(self.bot, "is_owner", mock.AsyncMock(return_value=False)):
            with self.assertRaises(commands.NotOwner):
                await self.utils.stats_global.can_run(ctx)

    def test_stats_are_limited(self):
        for command in (self.utils.stats, self.utils.stats_today, self.utils.stats_global, self.bot.get_command("stats usage")):
            self.assertIsNotNone(command._buckets._cooldown, command.qualified_name)
            self.assertIsNotNone(command._max_concurrency, command.qualified_name)

    async def test_stats_answer_after_deferring(self):
        ctx = self.ctx()
        ctx.defer = mock.AsyncMock()
        await self.utils.stats.callback(self.utils, ctx)
        ctx.defer.assert_awaited_once()


class SlowCommandsTest(Case):
    def test_role_words_have_a_limit(self):
        with self.assertRaises(DumpError):
            _find_roles(self.guild, ["Moderator"] * 26)

    def test_a_many_word_role_name_is_found_longest_first(self):
        role = self.guild.role("Very Long Role Name", 2)
        self.assertEqual(_find_roles(self.guild, ["very", "long", "role", "name", "Moderator"]), [role, self.role_mod])

    def test_the_separator_has_a_limit(self):
        with self.assertRaises(DumpError):
            parse_dump_query("--separator " + "x" * 11)
        self.assertEqual(parse_dump_query("--separator ,").separator, ",")

    async def test_highlight_targets_have_a_limit(self):
        ctx = self.ctx(self.mod)
        await self.utils.highlight_block.callback(self.utils, ctx, blocks=" ".join(["member"] * 26))
        self.assertIn("25", ctx.fails[0])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM highlight_blocks"), 0)

    async def test_a_list_too_large_to_upload_gets_a_sentence(self):
        ctx = self.ctx(self.admin)
        with mock.patch("captainguild.modules.utilities.upload_limit", lambda guild: 100):
            await self.utils._send_lines(ctx, "heading", ["x" * 60] * 100)
        self.assertEqual(ctx.sent, [])
        self.assertIn("too large", ctx.fails[0])


class RestoreTest(Case):
    async def timer(self, kind, key):
        return await self.bot.timers.get_key(kind, key)

    async def test_a_restore_without_timers_arms_what_it_brought_back(self):
        now = time.time()
        reminder = await self.db.insert("INSERT INTO reminders (guild_id, channel_id, user_id, about, created_at, due_at) VALUES (?, ?, ?, 'x', ?, ?)", (self.guild.id, self.channel.id, self.person.id, now, now + 100))
        running = await self.db.insert("INSERT INTO giveaways (guild_id, channel_id, host_id, prize, winners, created_at, ends_at) VALUES (?, ?, 1, 'p', 1, ?, ?)", (self.guild.id, self.channel.id, now, now + 100))
        unannounced = await self.db.insert("INSERT INTO giveaways (guild_id, channel_id, host_id, prize, winners, created_at, ends_at, ended_at) VALUES (?, ?, 1, 'p', 1, ?, ?, ?)", (self.guild.id, self.channel.id, now, now, now))
        finished = await self.db.insert("INSERT INTO giveaways (guild_id, channel_id, host_id, prize, winners, created_at, ends_at, ended_at, announced) VALUES (?, ?, 1, 'p', 1, ?, ?, ?, 1)", (self.guild.id, self.channel.id, now, now, now))
        await self.db.run("INSERT INTO temp_channels (channel_id, guild_id, created_by, created_at, expires_at) VALUES (5, ?, 1, ?, ?)", (self.guild.id, now, now + 100))
        await self.utils.on_guild_restored(self.guild.id)
        self.assertEqual((await self.timer("reminder", f"reminder:{reminder}"))["due_at"], now + 100)
        self.assertEqual((await self.timer("reminder", f"reminder:{reminder}"))["payload"], {"id": reminder})
        self.assertIsNotNone(await self.timer("giveaway", f"giveaway:{running}"))
        self.assertIsNotNone(await self.timer("giveaway", f"giveaway:{unannounced}"))
        self.assertIsNone(await self.timer("giveaway", f"giveaway:{finished}"))
        self.assertEqual((await self.timer("tempchannel", "tempchannel:5"))["payload"], {"guild_id": self.guild.id, "channel_id": 5})

    async def test_a_timer_that_exists_is_left_alone(self):
        row = await self.reminder()
        before = await self.timer("reminder", f"reminder:{row['id']}")
        await self.utils.on_guild_restored(self.guild.id)
        self.assertEqual((await self.timer("reminder", f"reminder:{row['id']}"))["id"], before["id"])

    async def test_a_toproll_round_comes_back_with_its_timer(self):
        now = time.time()
        open_round = await self.db.insert("INSERT INTO toproll_rounds (guild_id, channel_id, started_at, ends_at) VALUES (?, ?, ?, ?)", (self.guild.id, self.channel.id, now, now + 100))
        over = await self.db.insert("INSERT INTO toproll_rounds (guild_id, channel_id, started_at, ends_at, ended_at) VALUES (?, ?, ?, ?, ?)", (self.guild.id, self.channel.id, now, now, now))
        await self.fun.on_guild_restored(self.guild.id)
        self.assertIsNotNone(await self.timer("toproll", f"toproll:{open_round}"))
        self.assertIsNone(await self.timer("toproll", f"toproll:{over}"))


class TempChannelDeleteTest(Case):
    async def test_a_refused_delete_keeps_the_row_and_tries_again(self):
        channel = FakeChannel(self.guild, "temp")

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        channel.delete = refuse
        now = time.time()
        await self.db.run("INSERT INTO temp_channels (channel_id, guild_id, created_by, created_at, expires_at) VALUES (?, ?, 1, ?, ?)", (channel.id, self.guild.id, now, now))
        await self.bot.timers.schedule("tempchannel", now, {"guild_id": self.guild.id, "channel_id": channel.id}, guild_id=self.guild.id, key=f"tempchannel:{channel.id}")
        with self.assertLogs("captainguild.core.timers", "ERROR"):
            await self.bot.timers.run_due(now + 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM temp_channels"), 1)
        self.assertEqual((await self.bot.timers.get_key("tempchannel", f"tempchannel:{channel.id}"))["tries"], 1)


class GiveawayRulesTest(Case):
    async def create(self, **kwargs):
        ctx = self.ctx(self.admin)
        await self.utils.giveaway_create.callback(self.utils, ctx, 3600, 1, prize="p", **kwargs)
        return await self.db.one("SELECT * FROM giveaways ORDER BY id DESC LIMIT 1")

    async def press(self, member, row):
        pressed = Press(member, self.guild, types.SimpleNamespace(id=row["message_id"]))
        await self.utils.giveaway_pressed(pressed)
        return pressed

    async def entered(self, row):
        return await self.db.scalar("SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id = ?", (row["id"],))

    async def test_a_role_requirement_keeps_others_out(self):
        role = self.guild.role("Regulars", 2)
        row = await self.create(role=role)
        self.assertIn(role.mention, self.channel.sent[0].embeds[0].description)
        pressed = await self.press(self.person, row)
        self.assertIn(role.mention, pressed.sent[0])
        self.assertEqual(await self.entered(row), 0)
        self.person.roles.append(role)
        await self.press(self.person, row)
        self.assertEqual(await self.entered(row), 1)

    async def test_an_account_age_requirement_keeps_new_accounts_out(self):
        row = await self.create(account_days=30)
        self.person.created_at = discord.utils.utcnow() - datetime.timedelta(days=2)
        pressed = await self.press(self.person, row)
        self.assertIn("30 days", pressed.sent[0])
        self.assertEqual(await self.entered(row), 0)
        self.person.created_at = discord.utils.utcnow() - datetime.timedelta(days=40)
        await self.press(self.person, row)
        self.assertEqual(await self.entered(row), 1)

    async def test_someone_who_lost_the_role_can_still_leave(self):
        role = self.guild.role("Regulars", 2)
        row = await self.create(role=role)
        self.person.roles.append(role)
        await self.press(self.person, row)
        self.person.roles.remove(role)
        await self.press(self.person, row)
        self.assertEqual(await self.entered(row), 0)

    async def test_a_deleted_message_ends_it_and_tells_the_host(self):
        row = await self.create()
        await self.utils.on_raw_message_delete(types.SimpleNamespace(message_id=row["message_id"], guild_id=self.guild.id))
        stored = await self.db.one("SELECT * FROM giveaways WHERE id = ?", (row["id"],))
        self.assertIsNotNone(stored["ended_at"])
        self.assertEqual(stored["announced"], 1)
        self.assertIsNone(await self.bot.timers.get_key("giveaway", f"giveaway:{row['id']}"))
        self.assertIn("deleted", self.admin.dms[0]["content"])

    async def test_a_bulk_delete_counts_too(self):
        row = await self.create()
        await self.utils.on_raw_bulk_message_delete(types.SimpleNamespace(message_ids={row["message_id"], 5}, guild_id=self.guild.id))
        self.assertIsNotNone((await self.db.one("SELECT * FROM giveaways WHERE id = ?", (row["id"],)))["ended_at"])

    async def test_a_giveaway_in_an_archived_thread_still_announces(self):
        row = await self.create()
        self.guild.channels.remove(self.channel)  # discord.py drops an archived thread from its lists

        async def fetch(channel_id):
            return self.channel
        self.guild.fetch_channel = fetch
        before = len(self.channel.sent)
        await self.utils.finish_giveaway(await self.db.one("SELECT * FROM giveaways WHERE id = ?", (row["id"],)))
        self.assertEqual(len(self.channel.sent), before + 1)

    async def test_a_replaced_winner_cant_come_back_in_a_later_reroll(self):
        row = await self.create()
        for person in (self.person, self.friend):
            await self.press(person, row)
        ctx = self.ctx(self.admin)
        winners = await self.utils.finish_giveaway(await self.db.one("SELECT * FROM giveaways WHERE id = ?", (row["id"],)))
        first = winners[0]
        await self.utils.giveaway_reroll.callback(self.utils, ctx, row["id"])
        second = json.loads(await self.db.scalar("SELECT winner_ids FROM giveaways WHERE id = ?", (row["id"],)))
        self.assertNotEqual(second, [first])
        ctx = self.ctx(self.admin)
        await self.utils.giveaway_reroll.callback(self.utils, ctx, row["id"])
        self.assertEqual(ctx.fails, ["Nobody else is left to pick from."])


class TopRollPostTest(Case):
    async def test_a_failed_post_doesnt_use_up_their_turn(self):
        ctx = self.ctx()

        async def refuse(*args, **kwargs):
            raise discord.HTTPException(_FakeResponse(500), "busy")
        ctx.send = refuse
        with self.assertRaises(discord.HTTPException):
            await self.fun.games_toproll.callback(self.fun, ctx)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM toproll_scores"), 0)

