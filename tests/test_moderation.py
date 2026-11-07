import asyncio
import csv
import datetime
import io
import json
import time
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.modules import moderation as moderation_module
from captainguild.modules.moderation import ModerationError, Moderation, Someone
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, FakeMessage, TestBot, _FakeResponse, new_id


def audit_entry(guild, action, target_id, user, *, reason=None, changes=None):
    return SimpleNamespace(guild=guild, action=action, target=SimpleNamespace(id=target_id), user_id=user.id if user else None, user=user, reason=reason, extra=None, changes=changes)


class ModBase(BotTest):
    modules = [Moderation]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Moderation)
        self.modlog = FakeChannel(self.guild, "mod-log")
        self.guild.default_role.permissions = type(self.guild.default_role.permissions)("view_channel")
        await self.bot.settings.save(self.guild.id, "moderation", {"modlog_channel": self.modlog.id, "mute_role": self.role_muted.id})
        patcher = mock.patch.object(moderation_module, "AUDIT_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def cases(self, guild_id=None):
        return await self.db.all("SELECT * FROM cases WHERE guild_id = ? ORDER BY number", (guild_id or self.guild.id,))

    def last_post(self):
        return self.modlog.sent[-1].embeds[0]


class CaseTest(ModBase):
    async def test_numbers_count_per_server(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        first = await self.cog_.record_case(self.guild, "warn", self.member, self.mod, reason="one")
        second = await self.cog_.record_case(self.guild, "warn", self.member, self.mod, reason="two")
        elsewhere = await self.cog_.record_case(other, "warn", Someone(new_id(), "x"), None)
        self.assertEqual((first["number"], second["number"], elsewhere["number"]), (1, 2, 1))

    async def test_numbering_can_start_past_an_old_modlog(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"case_start": 1847})
        first = await self.cog_.record_case(self.guild, "ban", self.member, self.mod)
        second = await self.cog_.record_case(self.guild, "ban", self.member, self.mod)
        self.assertEqual((first["number"], second["number"]), (1847, 1848))

    async def test_post_matches_the_modlog_look(self):
        row = await self.cog_.record_case(self.guild, "mute", self.member, self.mod, reason="Being rude", seconds=2 * 86400 + 23 * 3600)
        embed = self.last_post()
        self.assertEqual(embed.title, f"mute | case {row['number']}")
        self.assertEqual(embed.description, f"**Offender:** member  <@{self.member.id}>\n**Duration:** 2 days and 23 hours\n**Reason:** Being rude\n**Responsible moderator:** mod")
        self.assertEqual(embed.color.value, 0xF48942)
        self.assertEqual(embed.footer.text, f"ID: {self.member.id}")
        self.assertIsNotNone(embed.timestamp)
        stored = (await self.cases())[0]
        self.assertEqual(stored["message_id"], self.modlog.sent[-1].id)
        self.assertEqual(stored["channel_id"], self.modlog.id)
        self.assertGreater(stored["expires_at"], time.time())

    async def test_colors_per_action(self):
        for action, color in (("ban", 0xDD5F53), ("unban", 0x53DDA4), ("unmute", 0x42F4A7), ("kick", 0x4C88FF), ("warn", 0xFFFA77)):
            await self.cog_.record_case(self.guild, action, self.member, self.mod)
            self.assertEqual(self.last_post().color.value, color, action)

    async def test_no_reason_shows_how_to_add_one_with_the_servers_prefix(self):
        await self.bot.settings.save(self.guild.id, "core", {"prefixes": ["."]})
        row = await self.cog_.record_case(self.guild, "ban", self.member, self.mod)
        self.assertIn(f"`.reason {row['number']} <text>`", self.last_post().description)
        await self.bot.settings.save(self.guild.id, "core", {"prefixes": []})
        row = await self.cog_.record_case(self.guild, "ban", self.member, self.mod)
        self.assertIn(f"`/modlog reason {row['number']} <text>`", self.last_post().description)

    async def test_markdown_in_a_name_stays_literal(self):
        await self.cog_.record_case(self.guild, "ban", Someone(new_id(), "_crinos_"), None, source="automod")
        self.assertIn("\\_crinos\\_", self.last_post().description)
        self.assertIn("**Responsible moderator:** Automod", self.last_post().description)

    async def test_logged_event_off_stores_but_does_not_post(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"logged": {"warn": False}})
        await self.cog_.record_case(self.guild, "warn", self.member, self.mod)
        self.assertEqual(self.modlog.sent, [])
        self.assertEqual(len(await self.cases()), 1)

    async def test_softban_and_minimute_follow_the_nearest_box(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"logged": {"ban": False, "mute": False}})
        await self.cog_.record_case(self.guild, "softban", self.member, self.mod)
        await self.cog_.record_case(self.guild, "minimute", self.member, self.mod)
        self.assertEqual(self.modlog.sent, [])

    async def test_a_deleted_modlog_channel_does_not_raise(self):
        self.guild.channels.remove(self.modlog)
        row = await self.cog_.record_case(self.guild, "warn", self.member, self.mod)
        self.assertEqual(row["number"], 1)

    async def test_a_modlog_channel_in_another_server_gets_nothing(self):
        other = FakeGuild("Other")
        other.me = self.me
        elsewhere = FakeChannel(other, "their-channel")
        await self.bot.settings.save(self.guild.id, "moderation", {"modlog_channel": elsewhere.id})
        row = await self.cog_.record_case(self.guild, "warn", self.member, self.mod)
        self.assertIsNone(row["message_id"])
        self.assertEqual(elsewhere.sent, [])

    async def test_a_refused_post_does_not_raise(self):
        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.modlog.send = refuse
        row = await self.cog_.record_case(self.guild, "warn", self.member, self.mod)
        self.assertIsNone(row["message_id"])

    async def test_unknown_action_is_a_bug_not_a_case(self):
        with self.assertRaises(ValueError):
            await self.cog_.record_case(self.guild, "nonsense", self.member, self.mod)

    async def test_history_counts_and_notes(self):
        for action in ("warn", "warn", "mute", "hardmute", "tempban", "ban", "softban", "kick", "timeout", "unmute"):
            await self.cog_.record_case(self.guild, action, self.member, self.mod)
        voided = await self.cog_.record_case(self.guild, "warn", self.member, self.mod)
        await self.cog_.void_case(self.guild.id, voided["number"], self.admin)
        await self.cog_.add_note(self.guild.id, self.member.id, self.mod, "watch this one")
        record = await self.cog_.history(self.guild.id, self.member.id)
        self.assertEqual(record["counts"], {"warn": 2, "mute": 2, "tempban": 1, "ban": 1, "kick": 2, "timeout": 1})
        self.assertEqual([row["number"] for row in record["cases"]], list(range(10, 0, -1)))
        self.assertEqual(record["notes"][0]["text"], "watch this one")

    async def test_history_of_nobody_is_empty(self):
        record = await self.cog_.history(self.guild.id, 123)
        self.assertEqual(record, {"cases": [], "notes": [], "counts": {"warn": 0, "mute": 0, "tempban": 0, "ban": 0, "kick": 0, "timeout": 0}})


class HierarchyTest(ModBase):
    async def assert_refused(self, user, action="ban", moderator=None, contains=""):
        with self.assertRaises(ModerationError) as caught:
            await self.cog_.punish(self.guild, user, action, moderator=moderator or self.mod)
        self.assertIn(contains, str(caught.exception))
        self.assertEqual(self.guild.banned, [])
        self.assertEqual(await self.cases(), [])

    async def test_a_moderator_cannot_act_at_or_above_their_own_role(self):
        await self.assert_refused(self.admin, contains="at or above yours")
        equal = FakeMember(self.guild, "equal", self.role_mod)
        await self.assert_refused(equal, contains="at or above yours")

    async def test_nobody_acts_on_the_owner(self):
        await self.assert_refused(self.owner, moderator=self.admin, contains="server owner")

    async def test_not_on_yourself_or_the_bot(self):
        await self.assert_refused(self.mod, contains="yourself")
        await self.assert_refused(self.me, moderator=self.owner, contains="myself")

    async def test_the_bot_cannot_act_above_its_own_role(self):
        high = self.guild.role("Above", 20)
        target = FakeMember(self.guild, "target", high)
        await self.assert_refused(target, moderator=self.owner, contains="at or above mine")

    async def test_the_owner_can_act_on_anyone_below_the_bot(self):
        await self.cog_.punish(self.guild, self.mod, "kick", moderator=self.owner)
        self.assertNotIn(self.mod, self.guild.members)

    async def test_a_missing_bot_permission_is_said_in_words(self):
        self.role_bot.permissions = type(self.role_bot.permissions)("send_messages")
        await self.assert_refused(self.member, contains="Ban Members")

    async def test_a_member_only_action_needs_them_in_the_server(self):
        for action in ("kick", "mute", "hardmute", "timeout", "warn"):
            with self.assertRaises(ModerationError) as caught:
                await self.cog_.punish(self.guild, Someone(new_id(), "gone"), action, moderator=self.mod)
            self.assertIn("isn't in this server", str(caught.exception))

    async def test_discord_refusing_comes_back_as_words_and_records_nothing(self):
        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.guild.ban = refuse
        await self.assert_refused(self.member, contains="Discord refused")
        self.assertEqual(self.cog_._expected, {})


class BanTest(ModBase):
    async def test_ban_someone_outside_the_server_by_id(self):
        outsider = Someone(new_id(), "outsider")
        row = await self.cog_.punish(self.guild, outsider, "ban", moderator=self.mod, reason="spam")
        self.assertIn(outsider.id, self.guild.banned)
        self.assertEqual((row["action"], row["user_name"], row["reason"], row["source"]), ("ban", "outsider", "spam", "command"))

    async def test_a_ban_on_someone_already_banned_is_refused(self):
        outsider = Someone(new_id(), "outsider")
        await self.cog_.punish(self.guild, outsider, "ban", moderator=self.mod)
        with self.assertRaises(ModerationError):
            await self.cog_.punish(self.guild, outsider, "ban", moderator=self.mod)

    async def test_text_command_takes_a_day_count_then_a_reason(self):
        ctx = self.context(self.admin)
        calls = []
        real = self.guild.ban

        async def spy(user, **kwargs):
            calls.append(kwargs)
            await real(user, **kwargs)
        self.guild.ban = spy
        await self.cog_.ban.callback(self.cog_, ctx, self.member, 3, reason="being rude")
        self.assertEqual(calls[0]["delete_message_seconds"], 3 * 86400)
        self.assertIn("being rude", calls[0]["reason"])
        self.assertIn(str(self.admin.id), calls[0]["reason"])
        self.assertEqual((await self.cases())[0]["reason"], "being rude")
        self.assertEqual(ctx.fails, [])
        self.assertIn("Banned member", ctx.oks[0])

    async def test_text_command_uses_the_servers_purge_days_when_none_given(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"ban": {"days": 5}})
        calls = []
        real = self.guild.ban

        async def spy(user, **kwargs):
            calls.append(kwargs)
            await real(user, **kwargs)
        self.guild.ban = spy
        await self.cog_.ban.callback(self.cog_, self.context(self.admin), self.member, None, reason="x")
        self.assertEqual(calls[0]["delete_message_seconds"], 5 * 86400)

    async def test_a_number_too_big_for_days_starts_the_reason(self):
        await self.cog_.ban.callback(self.cog_, self.context(self.admin), self.member, 100, reason="spam messages")
        self.assertEqual((await self.cases())[0]["reason"], "100 spam messages")

    async def test_a_ban_stays_permanent_whatever_its_reason_starts_with(self):
        for index, words in enumerate(("3d printer scam links", "30 minutes after joining", "2 weeks old account")):
            member = FakeMember(self.guild, f"spammer{index}", self.role_member)
            ctx = self.context(self.admin)
            await self.cog_.ban.callback(self.cog_, ctx, member, None, reason=words)
            row = (await self.cases())[-1]
            self.assertEqual((row["action"], row["seconds"], row["reason"]), ("ban", None, words))
            self.assertNotIn(" for ", ctx.oks[0])
            self.assertIsNone(await self.bot.timers.get_key("moderation.unban", f"{self.guild.id}:{member.id}"))

    async def test_a_bare_number_on_a_ban_is_a_day_count_and_tempban_reads_a_length(self):
        calls = []
        real = self.guild.ban

        async def spy(user, **kwargs):
            calls.append(kwargs)
            await real(user, **kwargs)
        self.guild.ban = spy
        await self.cog_.ban.callback(self.cog_, self.context(self.admin), self.member, 3, reason="days spam")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["seconds"], row["reason"]), ("ban", None, "days spam"))
        self.assertEqual(calls[0]["delete_message_seconds"], 3 * 86400)
        other = FakeMember(self.guild, "other", self.role_member)
        await self.cog_.tempban.callback(self.cog_, self.context(self.admin), other, 3, "days", reason="spam")
        row = (await self.cases())[1]
        self.assertEqual((row["action"], row["seconds"], row["reason"]), ("tempban", 3 * 86400, "spam"))

    async def test_slash_ban_never_reads_the_reason_as_a_duration(self):
        ctx = self.context(self.admin)
        ctx.interaction = object()
        await self.cog_.ban.callback(self.cog_, ctx, self.member, None, reason="2 days of spam")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["reason"]), ("ban", "2 days of spam"))

    async def test_slash_refuses_bad_days(self):
        ctx = self.context(self.admin)
        ctx.interaction = object()
        await self.cog_.ban.callback(self.cog_, ctx, self.member, 9, reason=None)
        self.assertTrue(ctx.fails)
        self.assertEqual(self.guild.banned, [])

    async def test_tempban_takes_days_and_duration_and_unbans_on_time(self):
        ctx = self.context(self.admin)
        await self.cog_.tempban.callback(self.cog_, ctx, self.member, 1, "2d", reason="cool off")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["seconds"], row["reason"]), ("tempban", 2 * 86400, "cool off"))
        self.assertIn(self.member.id, self.guild.banned)
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        await self.bot.timers.run_due(time.time() + 2 * 86400 + 1)
        self.assertNotIn(self.member.id, self.guild.banned)
        last = (await self.cases())[-1]
        self.assertEqual(last["action"], "unban")
        self.assertEqual(last["source"], "timer")
        self.assertEqual(last["moderator_id"], self.admin.id)
        self.assertIn("Automatic unban from tempban", last["reason"])
        self.assertIn(str(self.admin.id), last["reason"])

    async def test_a_day_count_alone_is_still_a_day_count(self):
        calls = []
        real = self.guild.ban

        async def spy(user, **kwargs):
            calls.append(kwargs)
            await real(user, **kwargs)
        self.guild.ban = spy
        await self.cog_.ban.callback(self.cog_, self.context(self.admin), self.member, 3, reason=None)
        self.assertEqual(calls[0]["delete_message_seconds"], 3 * 86400)
        self.assertEqual((await self.cases())[0]["action"], "ban")

    async def test_tempban_falls_back_on_the_servers_default_length(self):
        ctx = self.context(self.admin)
        await self.bot.settings.save(self.guild.id, "moderation", {"tempban": {"seconds": 7200}})
        await self.cog_.tempban.callback(self.cog_, ctx, self.member, None, "being", reason="rude")
        row = (await self.cases())[0]
        self.assertEqual((row["seconds"], row["reason"]), (7200, "being rude"))

    async def test_tempban_can_be_replaced_by_a_permanent_ban(self):
        outsider = Someone(new_id(), "outsider")
        await self.cog_.punish(self.guild, outsider, "tempban", moderator=self.mod, seconds=3600)
        await self.cog_.punish(self.guild, outsider, "ban", moderator=self.mod)
        await self.bot.timers.run_due(time.time() + 7200)
        self.assertIn(outsider.id, self.guild.banned)

    async def test_lifting_a_tempban_by_hand_cancels_the_timer(self):
        outsider = Someone(new_id(), "outsider")
        await self.cog_.punish(self.guild, outsider, "tempban", moderator=self.mod, seconds=3600)
        await self.cog_.punish(self.guild, outsider, "unban", moderator=self.mod)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 7200), 0)
        self.assertEqual([row["action"] for row in await self.cases()], ["tempban", "unban"])

    async def test_a_tempban_survives_a_restart(self):
        outsider = Someone(new_id(), "outsider")
        await self.cog_.punish(self.guild, outsider, "tempban", moderator=self.mod, seconds=3600)
        restarted = TestBot(self.db, self.message_db)
        await restarted.prepare([Moderation])
        restarted.fake_guilds.append(self.guild)
        await restarted.timers.run_due(time.time() + 3601)
        self.assertNotIn(outsider.id, self.guild.banned)

    async def test_a_due_timer_before_the_gateway_connects_waits(self):
        outsider = Someone(new_id(), "outsider")
        await self.cog_.punish(self.guild, outsider, "tempban", moderator=self.mod, seconds=3600)
        self.bot.fake_guilds.clear()
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertIn(outsider.id, self.guild.banned)
        timer = await self.bot.timers.get_key("moderation.unban", f"{self.guild.id}:{outsider.id}")
        self.assertIsNotNone(timer)
        self.bot.fake_guilds.append(self.guild)
        await self.bot.timers.run_due(timer["due_at"] + 1)
        self.assertNotIn(outsider.id, self.guild.banned)

    async def test_softban_clears_and_lets_them_back(self):
        ctx = self.context(self.admin)
        await self.cog_.softban.callback(self.cog_, ctx, self.member, 2, reason="cleanup")
        self.assertEqual(self.guild.banned, [])
        row = (await self.cases())[0]
        self.assertEqual(row["action"], "softban")
        await self.cog_.on_member_ban(self.guild, self.member)
        await self.cog_.on_member_unban(self.guild, self.member)
        self.assertEqual(len(await self.cases()), 1)

    async def test_unban_by_id_mention_and_name(self):
        for form in ("id", "mention", "name"):
            outsider = Someone(new_id(), f"person-{form}")
            self.guild.banned.append(outsider.id)
            self.guild.ban_users[outsider.id] = outsider
            words = {"id": str(outsider.id), "mention": f"<@{outsider.id}>", "name": outsider.name}[form]
            ctx = self.context(self.admin)
            await self.cog_.unban.callback(self.cog_, ctx, words, reason="appeal")
            self.assertEqual(ctx.fails, [], form)
            self.assertNotIn(outsider.id, self.guild.banned, form)

    async def test_unban_says_when_they_are_not_banned(self):
        ctx = self.context(self.admin)
        await self.cog_.unban.callback(self.cog_, ctx, "nobody", reason=None)
        self.assertIn("isn't banned", ctx.fails[0])
        ctx = self.context(self.admin)
        await self.cog_.unban.callback(self.cog_, ctx, str(new_id()), reason=None)
        self.assertIn("isn't banned", ctx.fails[0])

    async def test_massban_bans_each_id_once_and_skips_who_it_must(self):
        outsiders = [Someone(new_id()) for _ in range(12)]
        words = " ".join(f"<@{o.id}>" if i % 2 else str(o.id) for i, o in enumerate(outsiders))
        ctx = self.context(self.mod)
        self.role_mod.permissions = type(self.role_mod.permissions)("ban_members")
        await self.cog_.massban.callback(self.cog_, ctx, f"{words} {self.admin.id} {outsiders[0].id}", reason="raid wave")
        self.assertEqual(set(self.guild.banned), {o.id for o in outsiders})
        rows = await self.cases()
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(row["reason"] == "raid wave" for row in rows))
        self.assertEqual(rows[0]["user_name"], f"ID {outsiders[0].id}")
        self.assertIn("Banned 12 of 13", ctx.oks[0])
        self.assertIn("at or above yours", ctx.oks[0])
        # Twelve cases fit in two posts of up to ten embeds, and each remembers its post.
        self.assertEqual([len(m.embeds) for m in self.modlog.sent], [10, 2])
        self.assertEqual(len({row["message_id"] for row in rows}), 2)

    async def test_massban_text_form_reads_the_reason_after_the_ids(self):
        first, second = new_id(), new_id()
        ctx = self.context(self.admin)
        await self.cog_.massban.callback(self.cog_, ctx, str(first), reason=f"{second} raid wave")
        self.assertEqual(set(self.guild.banned), {first, second})
        self.assertEqual((await self.cases())[0]["reason"], "raid wave")

    async def test_massban_falls_back_when_bulk_is_refused(self):
        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "needs manage server")
        self.guild.bulk_ban = refuse
        target = new_id()
        ctx = self.context(self.admin)
        await self.cog_.massban.callback(self.cog_, ctx, str(target), reason=None)
        self.assertIn(target, self.guild.banned)

    async def test_massban_without_ids_is_refused(self):
        ctx = self.context(self.admin)
        await self.cog_.massban.callback(self.cog_, ctx, "spam everywhere", reason=None)
        self.assertTrue(ctx.fails)


class MuteTest(ModBase):
    async def test_mute_with_a_leading_duration_in_text(self):
        ctx = self.context(self.admin)
        await self.cog_.mute.callback(self.cog_, ctx, self.member, "20h45m", reason="being rude")
        self.assertIn(self.role_muted, self.member.roles)
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["seconds"], row["reason"]), ("mute", 20 * 3600 + 45 * 60, "being rude"))
        self.assertIn("20 hours and 45 minutes", ctx.oks[0])

    async def test_a_multi_word_duration_works_in_text(self):
        await self.cog_.mute.callback(self.cog_, self.context(self.admin), self.member, "1", reason="day late again")
        row = (await self.cases())[0]
        self.assertEqual((row["seconds"], row["reason"]), (86400, "late again"))

    async def test_a_plain_reason_in_text_uses_the_default_length(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"mute": {"seconds": 600}})
        await self.cog_.mute.callback(self.cog_, self.context(self.admin), self.member, "being", reason="rude")
        row = (await self.cases())[0]
        self.assertEqual((row["seconds"], row["reason"]), (600, "being rude"))

    async def test_slash_mute_reads_the_duration_box_and_refuses_nonsense(self):
        ctx = self.context(self.admin)
        ctx.interaction = object()
        await self.cog_.mute.callback(self.cog_, ctx, self.member, "soon", reason="x")
        self.assertIn("isn't a length of time", ctx.fails[0])
        self.assertNotIn(self.role_muted, self.member.roles)
        ctx = self.context(self.admin)
        ctx.interaction = object()
        await self.cog_.mute.callback(self.cog_, ctx, self.member, "2h", reason="3d of spam")
        row = (await self.cases())[0]
        self.assertEqual((row["seconds"], row["reason"]), (7200, "3d of spam"))

    async def test_no_mute_role_says_what_to_do(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": None})
        ctx = self.context(self.admin)
        await self.cog_.mute.callback(self.cog_, ctx, self.member, None, reason=None)
        self.assertIn("/muterole", ctx.fails[0])

    async def test_a_mute_role_above_the_bot_or_with_power_is_refused(self):
        high = self.guild.role("Top", 20)
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": high.id})
        with self.assertRaises(ModerationError):
            await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin)
        sneaky = self.guild.role("Sneaky", 3, "ban_members")
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": sneaky.id})
        with self.assertRaises(ModerationError):
            await self.cog_.punish(self.guild, self.member, "mute", moderator=self.admin)
        self.assertNotIn(sneaky, self.member.roles)

    async def test_mute_ends_on_time_with_an_automatic_case(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=600, reason="x")
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        await self.bot.timers.run_due(time.time() + 601)
        self.assertNotIn(self.role_muted, self.member.roles)
        last = (await self.cases())[-1]
        self.assertEqual((last["action"], last["source"], last["moderator_id"]), ("unmute", "timer", self.mod.id))
        self.assertIn("Automatic unmute from mute made", last["reason"])
        self.assertIn("ago by mod", last["reason"])
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_a_permanent_mute_has_no_timer(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=0)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 10 ** 9), 0)
        self.assertIn(self.role_muted, self.member.roles)

    async def test_muting_again_replaces_the_timer(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=600)
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=6000)
        await self.bot.timers.run_due(time.time() + 601)
        self.assertIn(self.role_muted, self.member.roles)
        await self.bot.timers.run_due(time.time() + 6001)
        self.assertNotIn(self.role_muted, self.member.roles)

    async def test_mute_comes_back_when_they_leave_and_return(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=600)
        self.guild.members.remove(self.member)
        back = FakeMember(self.guild, "member")
        back.id = self.member.id
        await self.cog_.on_member_join(back)
        self.assertIn(self.role_muted, back.roles)
        self.assertEqual(len(await self.cases()), 1)

    async def test_a_returning_member_on_the_rules_screen_gets_it_when_they_accept(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod)
        self.guild.members.remove(self.member)
        back = FakeMember(self.guild, "member", pending=True)
        back.id = self.member.id
        await self.cog_.on_member_join(back)
        self.assertNotIn(self.role_muted, back.roles)
        before = SimpleNamespace(pending=True, timed_out_until=None, get_role=lambda role_id: None)
        back.pending = False
        await self.cog_.on_member_update(before, back)
        self.assertIn(self.role_muted, back.roles)

    async def test_mute_ending_while_they_are_away_records_nothing_and_stays_off(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=600)
        self.guild.members.remove(self.member)
        await self.bot.timers.run_due(time.time() + 601)
        self.assertEqual((await self.cases())[-1]["action"], "mute")
        back = FakeMember(self.guild, "member")
        back.id = self.member.id
        await self.cog_.on_member_join(back)
        self.assertNotIn(self.role_muted, back.roles)

    async def test_a_newer_mute_outruns_a_timer_already_starting(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=600)
        stale = await self.bot.timers.get_key("moderation.unmute", f"{self.guild.id}:{self.member.id}")
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=6000)
        await self.cog_._mute_due(stale["payload"], stale)
        self.assertIn(self.role_muted, self.member.roles)
        self.assertEqual(len(await self.cases()), 2)

    async def test_commands_and_timers_for_one_member_take_turns(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=600)
        order = []
        real = self.member.remove_roles
        removing = asyncio.Event()

        async def slow_remove(*roles, **kwargs):
            order.append("removing")
            removing.set()
            await asyncio.sleep(0.05)
            await real(*roles, **kwargs)
            order.append("removed")
        self.member.remove_roles = slow_remove

        async def mute_again():
            await removing.wait()
            order.append("mute asked")
            await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=6000)
            order.append("mute done")

        timer = await self.bot.timers.get_key("moderation.unmute", f"{self.guild.id}:{self.member.id}")
        await asyncio.gather(self.cog_._mute_due(timer["payload"], timer), mute_again())
        self.assertEqual(order, ["removing", "mute asked", "removed", "mute done"])
        self.assertIn(self.role_muted, self.member.roles)
        self.assertIsNotNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_a_member_list_still_arriving_does_not_look_like_they_left(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=600)
        await self.bot.on_connect()
        members, self.guild.members = self.guild.members, [self.me]
        await self.bot.timers.run_due(time.time() + 601)
        self.assertIsNotNone(await self.db.one("SELECT * FROM active_mutes"))
        self.assertEqual(len(await self.cases()), 1)
        timer = await self.bot.timers.get_key("moderation.unmute", f"{self.guild.id}:{self.member.id}")
        self.assertIsNotNone(timer)
        self.bot.timers.resume()
        self.guild.members = members
        # By the time the timer comes around again in real life, the mute's end has passed.
        await self.db.run("UPDATE active_mutes SET expires_at = ?", (time.time() - 1,))
        await self.bot.timers.run_due(time.time() + 601)
        self.assertNotIn(self.role_muted, self.member.roles)
        self.assertEqual((await self.cases())[-1]["action"], "unmute")

    async def test_mutes_survive_a_restart(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=600)
        restarted = TestBot(self.db, self.message_db)
        await restarted.prepare([Moderation])
        restarted.fake_guilds.append(self.guild)
        await restarted.timers.run_due(time.time() + 601)
        self.assertNotIn(self.role_muted, self.member.roles)

    async def test_startup_mutes_anyone_who_came_back_while_the_bot_was_away(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod)
        await self.member.remove_roles(self.role_muted)
        self.member.joined_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=1)
        await self.cog_.reconcile(self.guild)
        self.assertIn(self.role_muted, self.member.roles)

    async def test_hardmute_takes_roles_and_gives_them_back(self):
        keep = self.guild.role("Linked", 3, managed=True)
        self.member.roles.append(keep)
        other = self.guild.role("Extra", 3)
        self.member.roles.append(other)
        await self.cog_.punish(self.guild, self.member, "hardmute", moderator=self.mod, seconds=600, reason="x")
        self.assertEqual({r.name for r in self.member.roles}, {"@everyone", "Muted", "Linked"})
        await self.bot.timers.run_due(time.time() + 601)
        self.assertEqual({r.name for r in self.member.roles}, {"@everyone", "Member", "Extra", "Linked"})

    async def test_hardmute_gives_roles_back_on_unmute(self):
        await self.cog_.punish(self.guild, self.member, "hardmute", moderator=self.mod)
        self.assertNotIn(self.role_member, self.member.roles)
        ctx = self.context(self.admin)
        await self.cog_.unmute.callback(self.cog_, ctx, self.member, reason="sorted")
        self.assertIn(self.role_member, self.member.roles)
        self.assertNotIn(self.role_muted, self.member.roles)
        self.assertEqual(ctx.fails, [])

    async def test_hardmute_skips_a_deleted_role_on_the_way_back(self):
        await self.cog_.punish(self.guild, self.member, "hardmute", moderator=self.mod)
        self.guild.roles.remove(self.role_member)
        await self.cog_.punish(self.guild, self.member, "unmute", moderator=self.mod)
        self.assertEqual({r.name for r in self.member.roles}, {"@everyone"})

    async def test_hardmute_ending_while_away_restores_roles_on_return(self):
        await self.cog_.punish(self.guild, self.member, "hardmute", moderator=self.mod, seconds=600)
        self.guild.members.remove(self.member)
        await self.bot.timers.run_due(time.time() + 601)
        back = FakeMember(self.guild, "member")
        back.id = self.member.id
        await self.cog_.on_member_join(back)
        self.assertIn(self.role_member, back.roles)
        self.assertNotIn(self.role_muted, back.roles)
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_unmute_someone_not_muted_is_refused(self):
        ctx = self.context(self.admin)
        await self.cog_.unmute.callback(self.cog_, ctx, self.member, reason=None)
        self.assertIn("isn't muted", ctx.fails[0])
        self.assertEqual(await self.cases(), [])

    async def test_changing_the_mute_role_does_not_strand_a_mute(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=600)
        newer = self.guild.role("Quiet", 2)
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": newer.id})
        await self.bot.timers.run_due(time.time() + 601)
        self.assertNotIn(self.role_muted, self.member.roles)

    async def test_a_mute_that_outlives_its_role_can_still_be_lifted(self):
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod, seconds=600)
        self.guild.roles.remove(self.role_muted)
        await self.bot.timers.run_due(time.time() + 601)
        self.assertEqual((await self.cases())[-1]["action"], "unmute")


class TimeoutTest(ModBase):
    async def test_timeout_uses_the_servers_default_and_a_future_time(self):
        ctx = self.context(self.admin)
        await self.cog_.timeout.callback(self.cog_, ctx, self.member, None, reason=None)
        self.assertEqual(ctx.fails, [])
        name, until, reason = self.member.calls[-1]
        self.assertEqual(name, "timeout")
        self.assertGreater(until, discord.utils.utcnow() + datetime.timedelta(minutes=59))
        self.assertEqual((await self.cases())[0]["seconds"], 3600)

    async def test_timeout_over_28_days_is_refused(self):
        ctx = self.context(self.admin)
        await self.cog_.timeout.callback(self.cog_, ctx, self.member, "29d", reason=None)
        self.assertIn("28 days", ctx.fails[0])

    async def test_an_administrator_cannot_be_timed_out(self):
        strong = FakeMember(self.guild, "strong", self.guild.role("Strong", 3, "administrator"))
        with self.assertRaises(ModerationError):
            await self.cog_.punish(self.guild, strong, "timeout", moderator=self.owner)

    async def test_untimeout(self):
        await self.cog_.punish(self.guild, self.member, "timeout", moderator=self.mod)
        ctx = self.context(self.admin)
        await self.cog_.untimeout.callback(self.cog_, ctx, self.member, reason="done")
        self.assertIsNone(self.member.timed_out_until)
        self.assertEqual((await self.cases())[-1]["action"], "untimeout")

    async def test_untimeout_someone_without_one_is_refused(self):
        ctx = self.context(self.admin)
        await self.cog_.untimeout.callback(self.cog_, ctx, self.member, reason=None)
        self.assertTrue(ctx.fails)


class DmTest(ModBase):
    async def dm_for(self, action="warn", mode=1, **kwargs):
        await self.bot.settings.save(self.guild.id, "moderation", {action: {"mode": mode, **kwargs.pop("settings", {})}})
        self.member.dms.clear()
        row = await self.cog_.punish(self.guild, self.member, action, moderator=self.mod, reason="spam", **kwargs)
        return row, self.member.dms

    async def test_each_mode(self):
        row, dms = await self.dm_for(mode=0)
        self.assertEqual(dms, [])
        self.assertIsNone(row["dm"])
        _, dms = await self.dm_for(mode=1)
        self.assertEqual(dms[0]["content"], "You were warned in Test Server.")
        _, dms = await self.dm_for(mode=2)
        self.assertEqual(dms[0]["content"], "You were warned in Test Server. Reason: 'spam'.")
        _, dms = await self.dm_for(mode=3)
        self.assertEqual(dms[0]["content"], f"You were warned in Test Server. Reason: 'spam'. Moderator: mod (id: {self.mod.id}).")

    async def test_a_duration_shows_in_the_dm(self):
        _, dms = await self.dm_for("mute", 1, seconds=1200)
        self.assertEqual(dms[0]["content"], "You were muted in Test Server for 20 minutes.")

    async def test_custom_message_fills_its_variables(self):
        message = {"content": "{offender} hit by {moderator}: {reason} ({duration})", "embeds": [{"title": "Note {reason}"}]}
        _, dms = await self.dm_for("mute", 4, seconds=1200, settings={"message": message})
        self.assertEqual(dms[0]["content"], "member hit by mod: spam (20 minutes)")
        self.assertEqual(dms[0]["embeds"][0].title, "Note spam")
        self.assertEqual(dms[0]["allowed_mentions"].everyone, False)

    async def test_custom_message_with_no_reason_says_na(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"warn": {"mode": 4, "message": {"content": "Why: {reason}"}}})
        await self.cog_.punish(self.guild, self.member, "warn", moderator=self.mod)
        self.assertEqual(self.member.dms[-1]["content"], "Why: N/A")

    async def test_an_empty_custom_message_falls_back_on_plain_words(self):
        _, dms = await self.dm_for("warn", 4)
        self.assertIn("You were warned", dms[0]["content"])

    async def test_a_ban_dm_goes_before_the_ban(self):
        order = []
        real_ban = self.guild.ban
        real_send = self.member.send

        async def ban(user, **kwargs):
            order.append("ban")
            await real_ban(user, **kwargs)

        async def send(*args, **kwargs):
            order.append("dm")
            return await real_send(*args, **kwargs)
        self.guild.ban = ban
        self.member.send = send
        await self.bot.settings.save(self.guild.id, "moderation", {"ban": {"mode": 1}})
        await self.cog_.punish(self.guild, self.member, "ban", moderator=self.mod)
        self.assertEqual(order, ["dm", "ban"])

    async def test_a_kick_dm_goes_before_the_kick(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"kick": {"mode": 1}})
        await self.cog_.punish(self.guild, self.member, "kick", moderator=self.mod)
        self.assertEqual(len(self.member.dms), 1)
        self.assertNotIn(self.member, self.guild.members)

    async def test_refused_dms_do_not_stop_the_action_and_are_mentioned(self):
        async def closed(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "closed")
        self.member.send = closed
        ctx = self.context(self.admin)
        await self.cog_.warn.callback(self.cog_, ctx, self.member, reason="x")
        self.assertIn("couldn't be sent a DM", ctx.oks[0])
        row = (await self.db.all("SELECT * FROM cases"))[0]
        self.assertEqual(json.loads(row["extra"])["dm"], False)

    async def test_dm_off_sends_nothing(self):
        await self.cog_.punish(self.guild, self.member, "warn", moderator=self.mod, dm=False)
        self.assertEqual(self.member.dms, [])


class OutsideTheBotTest(ModBase):
    def feed(self, action, target_id, user, **kwargs):
        self.bot.audit.feed(audit_entry(self.guild, action, target_id, user, **kwargs))

    async def test_a_ban_in_discords_menus_becomes_a_case_naming_who_did_it(self):
        outsider = Someone(new_id(), "spammer")
        self.feed(discord.AuditLogAction.ban, outsider.id, self.mod, reason="spam bot")
        await self.cog_.on_member_ban(self.guild, outsider)
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["moderator_id"], row["reason"], row["source"]), ("ban", self.mod.id, "spam bot", "external"))
        self.assertIn("**Responsible moderator:** mod", self.last_post().description)

    async def test_another_bot_is_named_and_marked(self):
        other_bot = FakeMember(self.guild, "ScamGuard", bot=True)
        outsider = Someone(new_id(), "spammer")
        self.feed(discord.AuditLogAction.ban, outsider.id, other_bot, reason="known scam image")
        await self.cog_.on_member_ban(self.guild, outsider)
        row = (await self.cases())[0]
        self.assertEqual((row["moderator_name"], row["moderator_bot"]), ("ScamGuard", 1))

    async def test_without_audit_access_it_is_still_a_case(self):
        await self.cog_.on_member_ban(self.guild, Someone(new_id(), "spammer"))
        row = (await self.cases())[0]
        self.assertIsNone(row["moderator_id"])
        self.assertIn("**Responsible moderator:** Unknown", self.last_post().description)

    async def test_the_bots_own_actions_are_not_recorded_twice(self):
        await self.cog_.punish(self.guild, self.member, "ban", moderator=self.mod)
        await self.cog_.on_member_ban(self.guild, self.member)
        self.assertEqual(len(await self.cases()), 1)
        outsider = Someone(new_id(), "x")
        await self.cog_.punish(self.guild, outsider, "tempban", moderator=self.mod, seconds=60)
        await self.cog_.on_member_ban(self.guild, outsider)
        self.assertEqual(len(await self.cases()), 2)

    async def test_after_a_restart_the_audit_log_still_tells_the_bot_it_was_itself(self):
        me = SimpleNamespace(id=self.me.id, name="Bot", bot=True)
        outsider = Someone(new_id(), "x")
        self.feed(discord.AuditLogAction.ban, outsider.id, me)
        with mock.patch.object(type(self.bot), "user", new_callable=mock.PropertyMock, return_value=me):
            await self.cog_.on_member_ban(self.guild, outsider)
        self.assertEqual(await self.cases(), [])

    async def test_unban_in_discords_menus_ends_a_tempban(self):
        outsider = Someone(new_id(), "x")
        await self.cog_.punish(self.guild, outsider, "tempban", moderator=self.mod, seconds=3600)
        self.feed(discord.AuditLogAction.unban, outsider.id, self.admin, reason="appeal")
        await self.cog_.on_member_ban(self.guild, outsider)
        await self.cog_.on_member_unban(self.guild, outsider)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 7200), 0)
        last = (await self.cases())[-1]
        self.assertEqual((last["action"], last["moderator_id"], last["reason"]), ("unban", self.admin.id, "appeal"))

    async def test_a_kick_needs_the_audit_log_to_say_so(self):
        await self.cog_.on_member_remove(self.member)
        self.assertEqual(await self.cases(), [])
        self.feed(discord.AuditLogAction.kick, self.member.id, self.mod, reason="rude")
        await self.cog_.on_member_remove(self.member)
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["moderator_id"]), ("kick", self.mod.id))

    async def test_one_kick_entry_counts_once(self):
        self.feed(discord.AuditLogAction.kick, self.member.id, self.mod)
        await self.cog_.on_member_remove(self.member)
        await self.cog_.on_member_remove(self.member)
        self.assertEqual(len(await self.cases()), 1)

    async def test_a_kick_by_the_bot_is_not_recorded_twice(self):
        await self.cog_.punish(self.guild, self.member, "kick", moderator=self.mod)
        await self.cog_.on_member_remove(self.member)
        self.assertEqual(len(await self.cases()), 1)

    def timed(self, member, seconds):
        copy = SimpleNamespace(id=member.id, guild=member.guild, pending=False, get_role=member.get_role, roles=member.roles, name=member.name)
        copy.timed_out_until = discord.utils.utcnow() + datetime.timedelta(seconds=seconds) if seconds is not None else None
        return copy

    async def test_a_timeout_in_discords_menus_becomes_a_case(self):
        changes = SimpleNamespace(before=SimpleNamespace(timed_out_until=None), after=SimpleNamespace(timed_out_until=1))
        self.feed(discord.AuditLogAction.member_update, self.member.id, self.mod, reason="calm down", changes=changes)
        await self.cog_.on_member_update(self.timed(self.member, None), self.timed(self.member, 3600))
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["moderator_id"], row["reason"]), ("timeout", self.mod.id, "calm down"))
        self.assertAlmostEqual(row["seconds"], 3600, delta=5)

    async def test_a_nickname_change_is_not_mistaken_for_who_timed_them_out(self):
        changes = SimpleNamespace(before=SimpleNamespace(nick=None), after=SimpleNamespace(nick="x"))
        self.feed(discord.AuditLogAction.member_update, self.member.id, self.admin, changes=changes)
        await self.cog_.on_member_update(self.timed(self.member, None), self.timed(self.member, 3600))
        self.assertIsNone((await self.cases())[0]["moderator_id"])

    async def test_discords_automod_timeout_names_its_entry(self):
        system = SimpleNamespace(id=new_id(), name="AutoMod", bot=True)
        self.feed(discord.AuditLogAction.automod_timeout_member, self.member.id, system, reason="keyword")
        await self.cog_.on_member_update(self.timed(self.member, None), self.timed(self.member, 60))
        self.assertEqual((await self.cases())[0]["moderator_name"], "AutoMod")

    async def test_removing_a_timeout_becomes_a_case_and_a_natural_end_does_not(self):
        changes = SimpleNamespace(before=SimpleNamespace(timed_out_until=1), after=SimpleNamespace(timed_out_until=None))
        self.feed(discord.AuditLogAction.member_update, self.member.id, self.mod, changes=changes)
        await self.cog_.on_member_update(self.timed(self.member, 3600), self.timed(self.member, None))
        self.assertEqual((await self.cases())[0]["action"], "untimeout")
        await self.cog_.on_member_update(self.timed(self.member, -10), self.timed(self.member, None))
        self.assertEqual(len(await self.cases()), 1)

    async def test_the_bots_own_timeout_is_not_recorded_twice(self):
        before = self.timed(self.member, None)
        await self.cog_.punish(self.guild, self.member, "timeout", moderator=self.mod)
        await self.cog_.on_member_update(before, self.member)
        self.assertEqual(len(await self.cases()), 1)

    def roles_changed(self, role, *, added):
        changes = SimpleNamespace(before=SimpleNamespace(roles=[] if added else [role]), after=SimpleNamespace(roles=[role] if added else []))
        return changes

    def with_roles(self, member, *roles):
        return SimpleNamespace(id=member.id, guild=member.guild, pending=False, timed_out_until=None, roles=list(roles), get_role=lambda role_id: next((r for r in roles if r.id == role_id), None), name=member.name, top_role=member.top_role)

    async def test_the_mute_role_given_by_hand_becomes_a_case_and_sticks(self):
        self.feed(discord.AuditLogAction.member_role_update, self.member.id, self.mod, reason="rude", changes=self.roles_changed(self.role_muted, added=True))
        await self.cog_.on_member_update(self.with_roles(self.member, self.role_member), self.with_roles(self.member, self.role_member, self.role_muted))
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["moderator_id"], row["reason"]), ("mute", self.mod.id, "rude"))
        self.assertIsNotNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_some_other_role_change_is_not_mistaken_for_it(self):
        self.feed(discord.AuditLogAction.member_role_update, self.member.id, self.admin, changes=self.roles_changed(self.role_member, added=True))
        await self.cog_.on_member_update(self.with_roles(self.member), self.with_roles(self.member, self.role_muted))
        self.assertIsNone((await self.cases())[0]["moderator_id"])

    async def test_taking_the_mute_role_off_by_hand_ends_the_mute(self):
        await self.cog_.punish(self.guild, self.member, "hardmute", moderator=self.mod, seconds=600)
        self.feed(discord.AuditLogAction.member_role_update, self.member.id, self.admin, changes=self.roles_changed(self.role_muted, added=False))
        await self.member.remove_roles(self.role_muted)
        await self.cog_.on_member_update(self.with_roles(self.member, self.role_muted), self.member)
        last = (await self.cases())[-1]
        self.assertEqual((last["action"], last["moderator_id"]), ("unmute", self.admin.id))
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))
        self.assertIn(self.role_member, self.member.roles)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 601), 0)

    async def test_the_bots_own_mute_is_not_recorded_twice(self):
        before = self.with_roles(self.member, self.role_member)
        await self.cog_.punish(self.guild, self.member, "mute", moderator=self.mod)
        await self.cog_.on_member_update(before, self.member)
        self.assertEqual(len(await self.cases()), 1)
        unmuted_before = self.with_roles(self.member, self.role_member, self.role_muted)
        await self.cog_.punish(self.guild, self.member, "unmute", moderator=self.mod)
        await self.cog_.on_member_update(unmuted_before, self.member)
        self.assertEqual(len(await self.cases()), 2)

    async def test_no_mute_role_set_means_role_changes_are_ignored(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": None})
        await self.cog_.on_member_update(self.with_roles(self.member), self.with_roles(self.member, self.role_muted))
        self.assertEqual(await self.cases(), [])


def staff_room(channel):
    """A channel @everyone can't read, so a record typed there answers there rather than going to DMs."""
    channel.permissions_for = lambda who: type(who.permissions)() if who is channel.guild.default_role else who.guild_permissions


class WarnAndNoteTest(ModBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        staff_room(self.channel)

    async def test_warn_case_and_warns_list(self):
        ctx = self.context(self.mod)
        await self.cog_.warn.callback(self.cog_, ctx, self.member, reason="careful @everyone")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["reason"]), ("warn", "careful @everyone"))
        ctx = self.context(self.mod)
        await self.cog_.warns.callback(self.cog_, ctx, member=self.member)
        self.assertIn("careful @everyone", ctx.sent[0]["embed"].description)
        self.assertEqual(ctx.sent[0]["allowed_mentions"].everyone, False)
        ctx = self.context(self.mod)
        await self.cog_.warns.callback(self.cog_, ctx, member=None)
        self.assertIn("member", ctx.sent[0]["embed"].description)

    async def test_warns_on_nobody(self):
        ctx = self.context(self.mod)
        await self.cog_.warns.callback(self.cog_, ctx, member=self.member)
        self.assertEqual(ctx.sent[0]["embed"].description, "They have no warnings.")

    async def test_a_long_list_stays_within_discords_limit_and_says_how_many_more(self):
        for index in range(40):
            await self.cog_.record_case(self.guild, "warn", self.member, self.mod, reason="x" * 300, post=False)
        ctx = self.context(self.mod)
        await self.cog_.warns.callback(self.cog_, ctx, member=self.member)
        description = ctx.sent[0]["embed"].description
        self.assertLessEqual(len(description), 4096)
        self.assertIn("more.", description)

    async def test_removing_a_warning_voids_it_and_marks_the_post(self):
        self.bot.ops.edit_message = mock.AsyncMock()
        row = await self.cog_.record_case(self.guild, "warn", self.member, self.mod)
        ctx = self.context(self.mod)
        await self.cog_.removewarning.callback(self.cog_, ctx, row["number"])
        self.assertEqual(ctx.fails, [])
        self.assertEqual((await self.cog_.history(self.guild.id, self.member.id))["counts"]["warn"], 0)
        payload = self.bot.ops.edit_message.call_args.args[2]
        self.assertIn("(voided)", payload["embeds"][0]["title"])
        ctx = self.context(self.mod)
        await self.cog_.removewarning.callback(self.cog_, ctx, row["number"])
        self.assertTrue(ctx.fails)

    async def test_removewarning_refuses_a_case_that_is_not_a_warning(self):
        row = await self.cog_.record_case(self.guild, "ban", self.member, self.mod)
        ctx = self.context(self.mod)
        await self.cog_.removewarning.callback(self.cog_, ctx, row["number"])
        self.assertTrue(ctx.fails)

    async def test_clearwarns(self):
        for _ in range(3):
            await self.cog_.record_case(self.guild, "warn", self.member, self.mod)
        await self.cog_.record_case(self.guild, "ban", self.member, self.mod)
        ctx = self.context(self.mod)
        await self.cog_.clearwarns.callback(self.cog_, ctx, member=self.member)
        self.assertIn("Removed 3 warnings", ctx.oks[0])
        record = await self.cog_.history(self.guild.id, self.member.id)
        self.assertEqual((record["counts"]["warn"], record["counts"]["ban"]), (0, 1))

    async def test_notes_commands_are_scoped_to_the_server(self):
        ctx = self.context(self.admin)
        await self.cog_.notes_setnote.callback(self.cog_, ctx, self.member, note="first note")
        await self.cog_.notes_setnote.callback(self.cog_, ctx, self.member, note="second note")
        ctx = self.context(self.admin)
        await self.cog_.notes_view.callback(self.cog_, ctx, member=self.member)
        self.assertIn("first note", ctx.sent[0]["embed"].description)
        other_guild_note = await self.cog_.add_note(self.guild.id + 1, self.member.id, self.admin, "elsewhere")
        ctx = self.context(self.admin)
        await self.cog_.notes_removenote.callback(self.cog_, ctx, 1)
        self.assertEqual(ctx.fails, [])
        self.assertEqual(len(await self.db.all("SELECT * FROM notes WHERE guild_id = ?", (self.guild.id,))), 1)
        self.assertEqual(other_guild_note["number"], 1)
        self.assertIsNotNone(await self.db.one("SELECT * FROM notes WHERE guild_id = ?", (self.guild.id + 1,)))
        ctx = self.context(self.admin)
        await self.cog_.notes_clearnotes.callback(self.cog_, ctx, member=self.member)
        self.assertIn("Removed 1 note", ctx.oks[0])

    async def test_a_note_works_on_someone_who_left(self):
        ctx = self.context(self.admin)
        gone = Someone(new_id(), "gone")
        await self.cog_.setnote_text.callback(self.cog_, ctx, gone, note="came back as an alt")
        self.assertEqual((await self.cog_.history(self.guild.id, gone.id))["notes"][0]["text"], "came back as an alt")

    async def test_removing_a_note_that_is_not_there_says_so(self):
        ctx = self.context(self.admin)
        await self.cog_.removenote_text.callback(self.cog_, ctx, 99)
        self.assertTrue(ctx.fails)

    async def test_a_note_too_long_is_refused(self):
        ctx = self.context(self.admin)
        await self.cog_.notes_setnote.callback(self.cog_, ctx, self.member, note="x" * 1801)
        self.assertTrue(ctx.fails)


class ModlogCommandTest(ModBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        staff_room(self.channel)

    async def test_create_makes_a_private_channel_and_uses_it(self):
        ctx = self.context(self.admin)
        await self.cog_.modlog_create.callback(self.cog_, ctx, name="cases")
        channel = next(c for c in self.guild.channels if c.name == "cases")
        self.assertEqual(self.settings("moderation")["modlog_channel"], channel.id)
        self.assertIs(channel.overwrites[self.guild.default_role].view_channel, False)
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "modlog.create")

    async def test_set_and_clear(self):
        target = FakeChannel(self.guild, "elsewhere")
        await self.cog_.modlog_set.callback(self.cog_, self.context(self.admin), channel=target)
        self.assertEqual(self.settings("moderation")["modlog_channel"], target.id)
        await self.cog_.modlog_clear.callback(self.cog_, self.context(self.admin))
        self.assertIsNone(self.settings("moderation")["modlog_channel"])

    async def test_from_lists_cases_with_the_moderator(self):
        await self.cog_.record_case(self.guild, "warn", self.member, self.mod, reason="one")
        ctx = self.context(self.admin)
        await self.cog_.modlog_from.callback(self.cog_, ctx, member=self.member)
        self.assertIn("by mod", ctx.sent[0]["embed"].description)
        self.assertEqual(ctx.sent[0]["allowed_mentions"].users, False)

    async def test_reason_changes_the_row_and_the_post(self):
        self.bot.ops.edit_message = mock.AsyncMock()
        row = await self.cog_.record_case(self.guild, "ban", self.member, self.mod)
        ctx = self.context(self.admin)
        await self.cog_.modlog_reason.callback(self.cog_, ctx, row["number"], reason="alt of someone")
        self.assertEqual(ctx.fails, [])
        self.assertEqual((await self.cases())[0]["reason"], "alt of someone")
        channel_id, message_id, payload = self.bot.ops.edit_message.call_args.args
        self.assertEqual((channel_id, message_id), (self.modlog.id, self.modlog.sent[0].id))
        self.assertIn("**Reason:** alt of someone", payload["embeds"][0]["description"])
        self.assertEqual(payload["embeds"][0]["title"], f"ban | case {row['number']}")

    async def test_the_text_command_reason_works_too(self):
        row = await self.cog_.record_case(self.guild, "ban", self.member, self.mod)
        await self.cog_.reason_text.callback(self.cog_, self.context(self.admin), row["number"], reason="typed it")
        self.assertEqual((await self.cases())[0]["reason"], "typed it")

    async def test_reason_on_a_post_that_is_gone_keeps_the_new_reason_and_says_so(self):
        row = await self.cog_.record_case(self.guild, "ban", self.member, self.mod)
        ctx = self.context(self.admin)
        await self.cog_.modlog_reason.callback(self.cog_, ctx, row["number"], reason="later")
        self.assertEqual((await self.cases())[0]["reason"], "later")
        self.assertIn("couldn't be changed", ctx.oks[0])

    async def test_reason_on_no_such_case(self):
        ctx = self.context(self.admin)
        await self.cog_.modlog_reason.callback(self.cog_, ctx, 99, reason="x")
        self.assertIn("no case 99", ctx.fails[0])

    async def test_changing_one_case_in_a_shared_post_keeps_the_others(self):
        self.bot.ops.edit_message = mock.AsyncMock()
        ids = [new_id(), new_id(), new_id()]
        await self.cog_.massban.callback(self.cog_, self.context(self.admin), " ".join(map(str, ids)), reason="raid")
        await self.cog_.set_reason(self.guild.id, 2, "this one was a mistake")
        payload = self.bot.ops.edit_message.call_args.args[2]
        self.assertEqual([e["title"] for e in payload["embeds"]], ["ban | case 1", "ban | case 2", "ban | case 3"])
        self.assertIn("mistake", payload["embeds"][1]["description"])
        self.assertIn("raid", payload["embeds"][0]["description"])

    async def test_highscores_rank_people_and_skip_bots_voids_and_timers(self):
        other_bot = FakeMember(self.guild, "SomeBot", bot=True)
        for _ in range(3):
            await self.cog_.record_case(self.guild, "warn", self.member, self.mod)
        await self.cog_.record_case(self.guild, "warn", self.member, self.admin)
        await self.cog_.record_case(self.guild, "ban", self.member, other_bot, source="external")
        await self.cog_.record_case(self.guild, "unmute", self.member, self.admin, source="timer")
        voided = await self.cog_.record_case(self.guild, "warn", self.member, self.admin)
        await self.cog_.void_case(self.guild.id, voided["number"], self.mod)
        ctx = self.context(self.admin)
        await self.cog_.modlog_highscores.callback(self.cog_, ctx)
        self.assertEqual(ctx.sent[0]["embed"].description, "1. mod: 3\n2. admin: 1")

    async def test_export_writes_a_text_file(self):
        other = FakeMember(self.guild, "other")
        await self.cog_.record_case(self.guild, "warn", self.member, self.mod, reason="one")
        await self.cog_.record_case(self.guild, "kick", other, self.mod, reason="two")
        ctx = self.context(self.admin)
        await self.cog_.modlog_export.callback(self.cog_, ctx, self.member, other)
        data = ctx.sent[0]["file"].fp.read().decode()
        self.assertIn("Reason: one", data)
        self.assertIn("Reason: two", data)
        self.assertIn(str(other.id), data)

    async def test_export_with_nothing_says_so(self):
        ctx = self.context(self.admin)
        await self.cog_.modlog_export.callback(self.cog_, ctx, self.member, None)
        self.assertTrue(ctx.fails)


class MuteRoleTest(ModBase):
    async def test_create_makes_the_role_and_sets_limits_everywhere(self):
        voice = FakeChannel(self.guild, "voice", discord.ChannelType.voice)
        ctx = self.context(self.admin)
        await self.cog_.muterole_create.callback(self.cog_, ctx, name="Silenced")
        role = next(r for r in self.guild.roles if r.name == "Silenced")
        self.assertEqual(self.settings("moderation")["mute_role"], role.id)
        for channel in (self.channel, voice):
            overwrite = channel.overwrites_for(role)
            self.assertIs(overwrite.send_messages, False)
            self.assertIs(overwrite.add_reactions, False)
            self.assertIs(overwrite.speak, False)
        self.assertIn("Silenced", ctx.oks[0] if "Silenced" in ctx.oks[0] else role.name)

    async def test_update_only_touches_what_changed(self):
        await self.cog_.muterole_update.callback(self.cog_, self.context(self.admin))
        before = len(self.channel.calls)
        ctx = self.context(self.admin)
        await self.cog_.muterole_update.callback(self.cog_, ctx)
        self.assertEqual(len(self.channel.calls), before)
        self.assertIn("Updated 0 channels", ctx.oks[0])

    async def test_update_needs_a_role(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": None})
        ctx = self.context(self.admin)
        await self.cog_.muterole_update.callback(self.cog_, ctx)
        self.assertTrue(ctx.fails)

    async def test_a_refusing_channel_is_counted(self):
        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.channel.set_permissions = refuse
        ctx = self.context(self.admin)
        await self.cog_.muterole_update.callback(self.cog_, ctx)
        self.assertIn("1 channels refused", ctx.oks[0])

    async def test_set_refuses_a_role_that_would_hand_out_power(self):
        for role in (self.guild.role("Sneaky", 3, "ban_members"), self.guild.role("Above", 20), self.guild.role("Linked", 2, managed=True), self.guild.default_role):
            ctx = self.context(self.admin)
            await self.cog_.muterole_set.callback(self.cog_, ctx, role=role)
            self.assertTrue(ctx.fails, role.name)
        self.assertEqual(self.settings("moderation")["mute_role"], self.role_muted.id)

    async def test_set_accepts_a_plain_role(self):
        role = self.guild.role("Quiet", 2)
        ctx = self.context(self.admin)
        await self.cog_.muterole_set.callback(self.cog_, ctx, role=role)
        self.assertEqual(self.settings("moderation")["mute_role"], role.id)

    async def test_a_new_channel_takes_the_limits(self):
        channel = FakeChannel(self.guild, "brand-new")
        await self.cog_.on_guild_channel_create(channel)
        self.assertIs(channel.overwrites_for(self.role_muted).send_messages, False)

    async def test_a_new_channel_without_a_mute_role_is_left_alone(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": None})
        channel = FakeChannel(self.guild, "brand-new")
        await self.cog_.on_guild_channel_create(channel)
        self.assertEqual(channel.calls, [])


class LockdownTest(ModBase):
    async def test_lock_denies_everyone_and_unlock_puts_it_back(self):
        ctx = self.context(self.admin)
        await self.cog_.lockdown_channel.callback(self.cog_, ctx, self.channel, None, reason="raid")
        everyone = self.guild.default_role
        overwrite = self.channel.overwrites_for(everyone)
        self.assertIs(overwrite.send_messages, False)
        self.assertIs(overwrite.send_messages_in_threads, False)
        ctx = self.context(self.admin)
        await self.cog_.unlockdown_channel.callback(self.cog_, ctx, self.channel)
        self.assertEqual(self.channel.overwrites_for(everyone).pair()[1].value, 0)
        self.assertEqual(ctx.fails, [])
        self.assertIsNone(await self.db.one("SELECT * FROM lockdowns"))

    async def test_a_channel_already_closed_stays_closed_afterward(self):
        everyone = self.guild.default_role
        await self.channel.set_permissions(everyone, overwrite=discord.PermissionOverwrite(send_messages=False, add_reactions=False))
        await self.cog_.lockdown_channel.callback(self.cog_, self.context(self.admin), self.channel, None, reason=None)
        await self.cog_.unlockdown_channel.callback(self.cog_, self.context(self.admin), self.channel)
        overwrite = self.channel.overwrites_for(everyone)
        self.assertIs(overwrite.send_messages, False)
        self.assertIs(overwrite.add_reactions, False)

    async def test_locking_twice_keeps_the_true_original(self):
        everyone = self.guild.default_role
        await self.channel.set_permissions(everyone, overwrite=discord.PermissionOverwrite(send_messages=True))
        await self.cog_.lockdown_channel.callback(self.cog_, self.context(self.admin), self.channel, None, reason=None)
        await self.cog_.lockdown_channel.callback(self.cog_, self.context(self.admin), self.channel, None, reason=None)
        await self.cog_.unlockdown_channel.callback(self.cog_, self.context(self.admin), self.channel)
        self.assertIs(self.channel.overwrites_for(everyone).send_messages, True)

    async def test_lock_with_a_time_ends_itself(self):
        ctx = self.context(self.admin)
        await self.cog_.lockdown_channel.callback(self.cog_, ctx, self.channel, "2h", reason="raid")
        self.assertIn("2 hours", ctx.oks[0])
        await self.bot.timers.run_due(time.time() + 7201)
        self.assertIsNone(self.channel.overwrites_for(self.guild.default_role).send_messages)

    async def test_lock_with_a_time_survives_a_restart(self):
        await self.cog_.lockdown_channel.callback(self.cog_, self.context(self.admin), self.channel, "2h", reason=None)
        restarted = TestBot(self.db, self.message_db)
        await restarted.prepare([Moderation])
        restarted.fake_guilds.append(self.guild)
        await restarted.timers.run_due(time.time() + 7201)
        self.assertIsNone(self.channel.overwrites_for(self.guild.default_role).send_messages)

    async def test_a_deleted_channel_at_the_end_of_a_lock_does_not_raise(self):
        await self.cog_.lockdown_channel.callback(self.cog_, self.context(self.admin), self.channel, "2h", reason=None)
        self.guild.channels.remove(self.channel)
        await self.bot.timers.run_due(time.time() + 7201)
        self.assertIsNone(await self.db.one("SELECT * FROM lockdowns"))

    async def test_run_in_a_thread_it_locks_the_parent(self):
        thread = FakeChannel(self.guild, "thread", discord.ChannelType.public_thread)
        thread.parent = self.channel
        await self.cog_.lockdown_channel.callback(self.cog_, self.context(self.admin, thread), None, None, reason=None)
        self.assertIs(self.channel.overwrites_for(self.guild.default_role).send_messages, False)
        self.assertIsNone(thread.overwrites_for(self.guild.default_role).send_messages)

    async def test_the_group_alone_locks_this_channel_in_text(self):
        ctx = self.context(self.admin)
        await self.cog_.lockdown.callback(self.cog_, ctx, None, None, reason=None)
        self.assertIs(self.channel.overwrites_for(self.guild.default_role).send_messages, False)

    async def test_without_administrator_the_bot_keeps_its_own_voice(self):
        self.role_bot.permissions = type(self.role_bot.permissions)("manage_roles", "send_messages")
        await self.cog_.lockdown_channel.callback(self.cog_, self.context(self.admin), self.channel, None, reason=None)
        self.assertIs(self.channel.overwrites_for(self.me).send_messages, True)
        await self.cog_.unlockdown_channel.callback(self.cog_, self.context(self.admin), self.channel)
        self.assertNotIn(self.me, self.channel.overwrites)

    async def test_server_lock_skips_channels_the_public_cannot_see(self):
        hidden = FakeChannel(self.guild, "staff")
        await hidden.set_permissions(self.guild.default_role, overwrite=discord.PermissionOverwrite(view_channel=False))
        category = FakeChannel(self.guild, "a category", discord.ChannelType.category)
        ctx = self.context(self.admin)
        await self.cog_.lockdown_server.callback(self.cog_, ctx, None, reason="raid")
        self.assertIs(self.channel.overwrites_for(self.guild.default_role).send_messages, False)
        self.assertIsNone(hidden.overwrites_for(self.guild.default_role).send_messages)
        self.assertIsNone(category.overwrites_for(self.guild.default_role).send_messages)
        ctx = self.context(self.admin)
        await self.cog_.unlockdown_server.callback(self.cog_, ctx)
        self.assertIsNone(self.channel.overwrites_for(self.guild.default_role).send_messages)
        self.assertIsNone(await self.db.one("SELECT * FROM lockdowns"))

    async def test_a_channel_refusing_does_not_stop_the_rest(self):
        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.modlog.set_permissions = refuse
        ctx = self.context(self.admin)
        await self.cog_.lockdown_server.callback(self.cog_, ctx, None, reason=None)
        self.assertIs(self.channel.overwrites_for(self.guild.default_role).send_messages, False)
        self.assertIn("1 refused", ctx.oks[0])

    async def test_unlock_on_a_channel_that_was_not_locked(self):
        ctx = self.context(self.admin)
        await self.cog_.unlockdown_channel.callback(self.cog_, ctx, self.channel)
        self.assertIn("isn't locked", ctx.fails[0])

    async def test_it_names_roles_that_can_still_post(self):
        strong = self.guild.role("Strong", 4, "kick_members")
        await self.channel.set_permissions(strong, overwrite=discord.PermissionOverwrite(send_messages=True))
        ctx = self.context(self.admin)
        await self.cog_.lockdown_channel.callback(self.cog_, ctx, self.channel, None, reason=None)
        self.assertIn("can still post: Strong", ctx.oks[0])

    async def test_setup_previews_then_changes(self):
        self.guild.default_role.permissions = type(self.guild.default_role.permissions)("view_channel", "send_messages")
        talker = self.guild.role("Talker", 2, "send_messages")
        high = self.guild.role("High", 30, "send_messages")
        strong = self.guild.role("Strong", 3, "administrator")
        ctx = self.context(self.admin)
        await self.cog_.lockdown_setup.callback(self.cog_, ctx, False)
        self.assertIn("Talker", ctx.oks[0])
        self.assertTrue(talker.permissions.send_messages)
        ctx = self.context(self.admin)
        await self.cog_.lockdown_setup.callback(self.cog_, ctx, True)
        self.assertFalse(talker.permissions.send_messages)
        self.assertTrue(high.permissions.send_messages)
        self.assertTrue(strong.permissions.administrator)


class PurgeChannel(FakeChannel):
    """Purges for real, the way discord.py applies limit, check and before."""

    async def purge(self, *, limit=100, check=lambda m: True, before=None, bulk=True, reason=None, **kwargs):
        self.calls.append(("purge", {"limit": limit, "before": before, "bulk": bulk, "reason": reason}))
        recent = [m for m in reversed(self.sent) if before is None or m.id < before.id][:limit]
        deleted = [m for m in recent if check(m)]
        for message in deleted:
            self.sent.remove(message)
        return deleted


class PurgeTest(ModBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.room = PurgeChannel(self.guild, "room")
        self.other_bot = FakeMember(self.guild, "SomeBot", bot=True)

    def post(self, author, content="", **kwargs):
        message = FakeMessage(self.room, author, content, **kwargs)
        self.room.sent.append(message)
        return message

    def ctx(self):
        return self.context(self.admin, self.room)

    async def test_purge_all_spares_pins_and_reports_who(self):
        pinned = self.post(self.member, "keep")
        pinned.pinned = True
        self.post(self.member, "one")
        self.post(self.mod, "two")
        ctx = self.ctx()
        await self.cog_.purge_all.callback(self.cog_, ctx, 10)
        self.assertEqual(self.room.sent, [pinned])
        self.assertIn("Removed 2 messages", ctx.oks[0])
        self.assertIn("**member**: 1", ctx.oks[0])
        self.assertTrue(ctx.message.deleted)

    async def test_the_command_message_is_not_counted(self):
        ctx = self.ctx()
        await self.cog_.purge_all.callback(self.cog_, ctx, 5)
        self.assertIs(self.room.calls[-1][1]["before"], ctx.message)

    async def test_each_kind_picks_the_right_messages(self):
        plain = self.post(self.member, "hello")
        link = self.post(self.member, "see https://example.com")
        emoji = self.post(self.member, "hi <:party:123456789012345678>")
        image = self.post(self.member, "", attachments=[SimpleNamespace(filename="a.png")])
        embed = self.post(self.other_bot, "", embed=discord.Embed(title="x"))
        ping = self.post(self.member, "hey", mentions=[self.mod])
        bot_text = self.post(self.other_bot, "beep")
        prefixed = self.post(self.member, "!help")

        async def remaining(command, *args, **kwargs):
            start = list(self.room.sent)
            await command.callback(self.cog_, self.ctx(), *args, **kwargs)
            gone = [m for m in start if m not in self.room.sent]
            self.room.sent[:] = start
            return gone

        self.assertEqual(await remaining(self.cog_.purge_links, 50), [link])
        self.assertEqual(await remaining(self.cog_.purge_emoji, 50), [emoji])
        self.assertEqual(await remaining(self.cog_.purge_files, 50), [image])
        self.assertEqual(set(await remaining(self.cog_.purge_images, 50)), {image, embed})
        self.assertEqual(await remaining(self.cog_.purge_embeds, 50), [embed])
        self.assertEqual(await remaining(self.cog_.purge_mentions, 50), [ping])
        self.assertEqual(set(await remaining(self.cog_.purge_bot, 50)), {embed, bot_text})
        self.assertEqual(set(await remaining(self.cog_.purge_bot, 50, prefix="!")), {embed, bot_text, prefixed})
        self.assertEqual(set(await remaining(self.cog_.purge_human, 50)), {plain, link, emoji, image, ping, prefixed})
        self.assertEqual(await remaining(self.cog_.purge_contains, substring="HELLO", search=50), [plain])
        self.assertEqual(set(await remaining(self.cog_.purge_user, self.other_bot, 50)), {embed, bot_text})

    async def test_typed_forms_of_the_group(self):
        keep = self.post(self.mod, "stay")
        gone = self.post(self.member, "go")
        await self.cog_.purge.callback(self.cog_, self.ctx(), target=f"<@{self.member.id}> 50")
        self.assertIn(keep, self.room.sent)
        self.assertNotIn(gone, self.room.sent)
        await self.cog_.purge.callback(self.cog_, self.ctx(), target="50")
        self.assertEqual(self.room.sent, [])

    async def test_nonsense_or_nothing_to_the_group_deletes_nothing(self):
        keep = self.post(self.member, "stay")
        for words in ("everything", "", "50 everything"):
            ctx = self.ctx()
            await self.cog_.purge.callback(self.cog_, ctx, target=words)
            self.assertTrue(ctx.fails, words)
            self.assertIn(keep, self.room.sent, words)

    async def test_count_limits(self):
        for count in (0, 2001):
            ctx = self.ctx()
            await self.cog_.purge_all.callback(self.cog_, ctx, count)
            self.assertTrue(ctx.fails, count)

    async def test_short_search_text_is_refused(self):
        ctx = self.ctx()
        await self.cog_.purge_contains.callback(self.cog_, ctx, substring="a", search=10)
        self.assertTrue(ctx.fails)

    async def test_without_manage_messages_it_says_so(self):
        self.role_bot.permissions = type(self.role_bot.permissions)("send_messages")
        ctx = self.ctx()
        await self.cog_.purge_all.callback(self.cog_, ctx, 5)
        self.assertIn("Manage Messages", ctx.fails[0])

    async def test_discord_refusing_is_said_in_words(self):
        async def refuse(**kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.room.purge = refuse
        ctx = self.ctx()
        await self.cog_.purge_all.callback(self.cog_, ctx, 5)
        self.assertTrue(ctx.fails)

    async def test_reactions(self):
        with_reactions = self.post(self.member, "x")
        with_reactions.reactions = [SimpleNamespace(count=3)]
        with_reactions.added_reactions = ["a"]
        ctx = self.ctx()
        await self.cog_.purge_reactions.callback(self.cog_, ctx, 10)
        self.assertEqual(with_reactions.added_reactions, [])
        self.assertIn("Removed 3 reactions from 1 messages", ctx.oks[0])

    async def test_cleanup_takes_my_messages_and_commands_even_pinned(self):
        await self.bot.settings.save(self.guild.id, "core", {"prefixes": ["."]})
        mine = self.post(self.me, "reply")
        mine.pinned = True
        command = self.post(self.member, ".warn someone")
        chat = self.post(self.member, "chat")
        await self.cog_.cleanup.callback(self.cog_, self.ctx(), 50)
        self.assertEqual(self.room.sent, [chat])
        self.assertNotIn(mine, self.room.sent)
        self.assertNotIn(command, self.room.sent)


class SetNickTest(ModBase):
    async def test_sets_and_clears(self):
        ctx = self.context(self.admin)
        await self.cog_.setnick.callback(self.cog_, ctx, self.member, name="Newname")
        self.assertEqual(self.member.nick, "Newname")
        await self.cog_.setnick.callback(self.cog_, ctx, self.member, name=None)
        self.assertIsNone(self.member.nick)

    async def test_hierarchy_and_length(self):
        ctx = self.context(self.mod)
        await self.cog_.setnick.callback(self.cog_, ctx, self.admin, name="x")
        self.assertIn("at or above yours", ctx.fails[0])
        ctx = self.context(self.admin)
        await self.cog_.setnick.callback(self.cog_, ctx, self.member, name="x" * 33)
        self.assertTrue(ctx.fails)
        high = FakeMember(self.guild, "high", self.guild.role("High", 20))
        ctx = self.context(self.owner)
        await self.cog_.setnick.callback(self.cog_, ctx, high, name="x")
        self.assertIn("at or above mine", ctx.fails[0])
        ctx = self.context(self.admin)
        await self.cog_.setnick.callback(self.cog_, ctx, self.owner, name="x")
        self.assertTrue(ctx.fails)


class ImportTest(ModBase):
    def export(self, channel_id, other_channel=900000000000006999):
        return {
            "is_vip": 0,
            "modlogs": {"channel_id": str(channel_id), "logging_bitmask": 255, "report_channel": str(other_channel)},
            "overrides": {
                "ban": {"command": "ban", "default_duration": None, "default_time": 0, "embed": None, "message": "", "mode": 3},
                "tempban": {"command": "tempban", "default_duration": 7200, "default_time": 3, "embed": None, "message": "", "mode": 2},
                "timeout": {"command": "timeout", "default_duration": 3600, "default_time": None, "embed": None, "message": "", "mode": 1},
                "mute": {"command": "mute", "default_duration": None, "default_time": None, "embed": json.dumps({"title": "Muted"}), "message": "You were muted", "mode": 4},
                "warn": {"command": "warn", "mode": 9, "message": "", "embed": None},
                "mystery": {"command": "mystery", "mode": 1},
            },
        }

    def ops_guild(self):
        self.bot.ops.describe_guild = lambda guild_id: {
            "channels": [{"id": c.id} for c in self.guild.channels], "roles": [{"id": r.id} for r in self.guild.roles],
        } if guild_id == self.guild.id else None

    async def test_it_fills_the_settings_from_the_export(self):
        self.ops_guild()
        client = await self.client()
        response = await client.post(self.path("/moderation/import"), json={"code": json.dumps(self.export(self.modlog.id))})
        self.assertEqual(response.status, 200)
        cfg = self.settings("moderation")
        self.assertEqual(cfg["modlog_channel"], self.modlog.id)
        self.assertEqual({key: on for key, on in cfg["logged"].items() if not on}, {"timeout": False, "untimeout": False})
        self.assertEqual((cfg["ban"]["mode"], cfg["ban"]["days"]), (3, 0))
        self.assertEqual((cfg["tempban"]["mode"], cfg["tempban"]["seconds"], cfg["tempban"]["days"]), (2, 7200, 3))
        self.assertEqual((cfg["timeout"]["mode"], cfg["timeout"]["seconds"]), (1, 3600))
        self.assertEqual((cfg["mute"]["mode"], cfg["mute"]["message"]["content"], cfg["mute"]["message"]["embeds"][0]["title"]), (4, "You were muted", "Muted"))
        self.assertEqual(cfg["warn"]["mode"], 3)
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "moderation.import")

    async def test_a_channel_from_another_server_is_left_out(self):
        self.ops_guild()
        await self.bot.settings.save(self.guild.id, "moderation", {"modlog_channel": None})
        client = await self.client()
        response = await client.post(self.path("/moderation/import"), json={"code": json.dumps(self.export(111222333444555666))})
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.settings("moderation")["modlog_channel"])

    async def test_an_empty_export_changes_nothing(self):
        self.ops_guild()
        before = dict(self.settings("moderation"))
        client = await self.client()
        response = await client.post(self.path("/moderation/import"), json={"code": json.dumps({"is_vip": 0, "modlogs": {"channel_id": None, "logging_bitmask": None, "report_channel": None}, "overrides": {}})})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.settings("moderation"), before)

    async def test_bad_input_and_access(self):
        self.ops_guild()
        client = await self.client()
        for body in ({"code": "not json"}, {"code": "[1]"}, {}, ["nope"]):
            self.assertEqual((await client.post(self.path("/moderation/import"), json=body)).status, 400, body)
        staff = await self.client(access="staff")
        self.assertEqual((await staff.post(self.path("/moderation/import"), json={"code": "{}"})).status, 403)


class ApiTest(ModBase):
    async def seed(self):
        await self.cog_.record_case(self.guild, "warn", self.member, self.mod, reason="first reason")
        await self.cog_.record_case(self.guild, "ban", Someone(new_id(), "spammer"), self.admin, reason="=HYPERLINK(evil)")
        await self.cog_.record_case(self.guild, "mute", self.member, self.admin, reason="100% rude_person", seconds=600)

    async def test_list_paginates_on_the_server(self):
        for index in range(30):
            await self.cog_.record_case(self.guild, "warn", self.member, self.mod, post=False)
        client = await self.client(access="staff")
        body = await (await client.get(self.path("/cases?page=2&size=10"))).json()
        self.assertEqual((body["total"], body["page"], body["size"], len(body["rows"])), (30, 2, 10, 10))
        self.assertEqual(body["rows"][0]["number"], 20)
        self.assertIsInstance(body["rows"][0]["user_id"], str)

    async def test_bad_paging_input_falls_back(self):
        await self.seed()
        client = await self.client(access="staff")
        response = await client.get(self.path("/cases?page=abc&size=77777"))
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["size"], 25)
        response = await client.get(self.path("/cases?page=-5"))
        self.assertEqual((await response.json())["page"], 1)

    async def test_filters(self):
        await self.seed()
        client = await self.client(access="staff")

        async def numbers(query):
            body = await (await client.get(self.path(f"/cases?{query}"))).json()
            return [row["number"] for row in body["rows"]]

        self.assertEqual(await numbers("action=warn"), [1])
        self.assertEqual(await numbers("action=warn,ban"), [2, 1])
        self.assertEqual(await numbers("action=nonsense"), [3, 2, 1])
        self.assertEqual(await numbers("user=spam"), [2])
        self.assertEqual(await numbers(f"user={self.member.id}"), [3, 1])
        self.assertEqual(await numbers("moderator=admin"), [3, 2])
        self.assertEqual(await numbers(f"moderator={self.mod.id}"), [1])
        self.assertEqual(await numbers("q=first"), [1])
        self.assertEqual(await numbers("q=2"), [2])
        self.assertEqual(await numbers("number=3"), [3])
        self.assertEqual(await numbers("reason=rude"), [3])
        self.assertEqual(await numbers("q=99999999999999999999999"), [])

    async def test_search_wildcards_are_literal(self):
        await self.seed()
        client = await self.client(access="staff")
        for query, expected in (("q=100%25", [3]), ("q=_", [3]), ("q=%25", [3]), ("user=%25", [])):
            body = await (await client.get(self.path(f"/cases?{query}"))).json()
            self.assertEqual([row["number"] for row in body["rows"]], expected, query)

    async def test_a_quote_in_a_search_is_only_text(self):
        await self.seed()
        client = await self.client(access="staff")
        response = await client.get(self.path("/cases?q=x'%20OR%201=1%20--"))
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["total"], 0)

    async def test_state_filter_and_void_restore(self):
        await self.seed()
        client = await self.client(access="staff")
        response = await client.post(self.path("/cases/1/void"), json={"reason": "mistake"})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual((body["voided"], body["void_reason"], body["voided_by_name"]), (True, "mistake", "admin"))
        self.assertEqual((await (await client.get(self.path("/cases?state=voided"))).json())["total"], 1)
        self.assertEqual((await (await client.get(self.path("/cases?state=active"))).json())["total"], 2)
        self.assertEqual((await client.post(self.path("/cases/1/void"), json={})).status, 404)
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "case.void")
        response = await client.post(self.path("/cases/1/restore"))
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.cases())[0]["voided_at"], None)
        self.assertEqual((await client.post(self.path("/cases/1/restore"))).status, 404)

    async def test_edit_reason(self):
        await self.seed()
        self.bot.ops.edit_message = mock.AsyncMock()
        client = await self.client(access="staff")
        response = await client.patch(self.path("/cases/1"), json={"reason": "better reason"})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual((body["reason"], body["post_updated"]), ("better reason", True))
        self.assertEqual((await client.patch(self.path("/cases/1"), json={"reason": ""})).status, 400)
        self.assertEqual((await client.patch(self.path("/cases/1"), json={"reason": "x" * 1025})).status, 400)
        self.assertEqual((await client.patch(self.path("/cases/1"), json={})).status, 400)
        self.assertEqual((await client.patch(self.path("/cases/999"), json={"reason": "x"})).status, 404)
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "case.reason")

    async def test_edit_survives_a_post_that_is_gone(self):
        await self.seed()
        client = await self.client()
        body = await (await client.patch(self.path("/cases/1"), json={"reason": "new"})).json()
        self.assertEqual((body["reason"], body["post_updated"]), ("new", False))

    async def test_no_case_from_another_server_is_reachable(self):
        other = FakeGuild("Other")
        secret = await self.cog_.record_case(other, "ban", Someone(new_id(), "theirs"), None, reason="their secret")
        client = await self.client()
        self.assertEqual((await client.get(self.path(f"/cases/{secret['number']}"))).status, 404)
        self.assertEqual((await client.patch(self.path(f"/cases/{secret['number']}"), json={"reason": "mine now"})).status, 404)
        self.assertEqual((await client.post(self.path(f"/cases/{secret['number']}/void"), json={})).status, 404)
        self.assertEqual((await (await client.get(self.path("/cases?q=secret"))).json())["total"], 0)
        body = await (await client.get(self.path(f"/cases/member/{secret['user_id']}"))).json()
        self.assertEqual(body["cases"], [])
        row = await self.db.one("SELECT reason, voided_at FROM cases WHERE guild_id = ?", (other.id,))
        self.assertEqual((row["reason"], row["voided_at"]), ("their secret", None))
        # A guild the signed-in person has no access to answers 403 before any handler runs.
        self.assertEqual((await client.get(f"/api/g/{other.id}/cases")).status, 403)

    async def test_huge_numbers_are_not_found_rather_than_a_crash(self):
        client = await self.client()
        huge = "9" * 40
        self.assertEqual((await client.get(self.path(f"/cases/{huge}"))).status, 404)
        self.assertEqual((await client.patch(self.path(f"/cases/{huge}"), json={"reason": "x"})).status, 404)
        self.assertEqual((await client.post(self.path(f"/cases/{huge}/void"), json={})).status, 404)
        self.assertEqual((await client.get(self.path(f"/cases/member/{huge}"))).status, 404)
        self.assertEqual((await client.post(self.path(f"/cases/member/{huge}/notes"), json={"text": "x"})).status, 404)
        self.assertEqual((await client.delete(self.path(f"/cases/notes/{huge}"))).status, 404)

    async def test_member_view(self):
        await self.seed()
        await self.cog_.add_note(self.guild.id, self.member.id, self.mod, "note one")
        await self.cog_.void_case(self.guild.id, 1, self.mod)
        client = await self.client(access="staff")
        body = await (await client.get(self.path(f"/cases/member/{self.member.id}"))).json()
        self.assertEqual([row["number"] for row in body["cases"]], [3, 1])
        self.assertEqual(body["counts"]["warn"], 0)
        self.assertEqual(body["counts"]["mute"], 1)
        self.assertEqual(body["notes"][0]["text"], "note one")
        self.assertEqual(body["name"], "member")

    async def test_notes_from_the_dashboard(self):
        client = await self.client(access="staff")
        response = await client.post(self.path(f"/cases/member/{self.member.id}/notes"), json={"text": "seen in the ticket"})
        self.assertEqual(response.status, 200)
        number = (await response.json())["number"]
        self.assertEqual((await client.post(self.path(f"/cases/member/{self.member.id}/notes"), json={"text": ""})).status, 400)
        self.assertEqual((await client.post(self.path(f"/cases/member/{self.member.id}/notes"), json=["nope"])).status, 400)
        self.assertEqual((await client.delete(self.path(f"/cases/notes/{number}"))).status, 200)
        self.assertEqual((await client.delete(self.path(f"/cases/notes/{number}"))).status, 404)

    async def test_a_note_in_another_server_cannot_be_deleted(self):
        other_note = await self.cog_.add_note(self.guild.id + 1, self.member.id, self.mod, "theirs")
        client = await self.client()
        self.assertEqual((await client.delete(self.path(f"/cases/notes/{other_note['number']}"))).status, 404)
        self.assertIsNotNone(await self.db.one("SELECT * FROM notes WHERE guild_id = ?", (self.guild.id + 1,)))

    async def test_export_csv(self):
        await self.seed()
        client = await self.client(access="staff")
        response = await client.get(self.path("/cases/export?format=csv&moderator=admin"))
        self.assertEqual(response.status, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.content_type, "text/csv")
        rows = list(csv.DictReader(io.StringIO(await response.text())))
        self.assertEqual([row["case"] for row in rows], ["3", "2"])
        self.assertEqual(rows[1]["reason"], "'=HYPERLINK(evil)")
        self.assertEqual(rows[0]["user_id"], str(self.member.id))

    async def test_export_json_keeps_ids_exact(self):
        await self.seed()
        client = await self.client(access="staff")
        response = await client.get(self.path("/cases/export?format=json"))
        self.assertEqual(response.content_type, "application/json")
        rows = json.loads(await response.text())
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["user_id"], str(self.member.id))
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "case.export")

    async def test_leaderboard(self):
        await self.seed()
        await self.cog_.record_case(self.guild, "ban", self.member, FakeMember(self.guild, "SomeBot", bot=True), source="external")
        await self.cog_.record_case(self.guild, "unmute", self.member, self.admin, source="timer")
        client = await self.client(access="staff")
        rows = await (await client.get(self.path("/cases/leaderboard"))).json()
        self.assertEqual([(row["moderator_name"], row["total"]) for row in rows], [("admin", 2), ("mod", 1)])
        self.assertEqual(rows[0]["ban"], 1)
        self.assertEqual(rows[0]["mute"], 1)
        rows = await (await client.get(self.path("/cases/leaderboard?bots=1"))).json()
        self.assertEqual(len(rows), 3)
        await self.db.run("UPDATE cases SET created_at = 0 WHERE moderator_name = 'mod'")
        rows = await (await client.get(self.path("/cases/leaderboard?days=30"))).json()
        self.assertEqual([row["moderator_name"] for row in rows], ["admin"])
        self.assertEqual((await client.get(self.path("/cases/leaderboard?days=abc"))).status, 200)

    async def test_filters_endpoint(self):
        await self.seed()
        client = await self.client(access="staff")
        body = await (await client.get(self.path("/cases/filters"))).json()
        self.assertIn("ban", body["actions"])
        self.assertEqual({row["name"] for row in body["moderators"]}, {"mod", "admin"})

    async def test_settings_need_full_access_and_refuse_bad_values(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.put(self.path("/settings/moderation"), json={"modlog_channel": None})).status, 403)
        client = await self.client()
        for form in ({"ban": {"days": 9}}, {"ban": {"mode": 9}}, {"timeout": {"seconds": 29 * 86400}}, {"mute_role": "abc"}, {"warn": {"message": {"content": "x" * 2001}}}, {"case_start": 0}, {"logged": {"ban": "yes"}}):
            self.assertEqual((await client.put(self.path("/settings/moderation"), json=form)).status, 400, form)
        response = await client.put(self.path("/settings/moderation"), json={"logged": {"ban": False}, "ban": {"days": 7, "mode": 3}})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.settings("moderation")["ban"]["days"], 7)
        self.assertFalse(self.settings("moderation")["logged"]["ban"])
        self.assertTrue(self.settings("moderation")["logged"]["kick"])
