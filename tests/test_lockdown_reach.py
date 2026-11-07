"""How far a lockdown reaches: who it silences, who keeps posting, what it puts back, and how a typed command can go wrong."""
from __future__ import annotations

import datetime
import time
from typing import Any
from unittest import mock

import discord

from captainguild.core.invite import ADD_PERMISSIONS
from captainguild.modules.moderation import LOCK_FIELDS, MUTE_DENIED, ModerationError
from tests.fakes import FakeChannel, FakeMember, FakePermissions, _FakeResponse
from tests.test_moderation import ModBase
from tests.test_moderation_live import LiveBase


class Base(ModBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.everyone = self.guild.default_role
        self.everyone.permissions = FakePermissions("view_channel", "send_messages", "add_reactions", "use_application_commands", "connect")

    async def lock(self, channel: Any = None) -> Any:
        ctx = self.context(self.admin)
        await self.cog_.lockdown_channel.callback(self.cog_, ctx, channel or self.channel, None, reason="raid")
        return ctx

    async def unlock(self, channel: Any = None) -> Any:
        ctx = self.context(self.admin)
        await self.cog_.unlockdown_channel.callback(self.cog_, ctx, channel or self.channel)
        return ctx


class WhatItCoversTest(Base):
    async def test_it_also_stops_reactions_and_slash_commands(self):
        await self.lock()
        overwrite = self.channel.overwrites_for(self.everyone)
        self.assertIs(overwrite.add_reactions, False)
        self.assertIs(overwrite.use_application_commands, False)
        self.assertIsNone(overwrite.connect)
        await self.unlock()
        self.assertEqual(self.channel.overwrites_for(self.everyone).pair()[1].value, 0)

    async def test_it_stops_voice_in_a_voice_channel(self):
        voice = FakeChannel(self.guild, "voice", discord.ChannelType.voice)
        await self.lock(voice)
        self.assertIs(voice.overwrites_for(self.everyone).connect, False)
        await self.unlock(voice)
        self.assertIsNone(voice.overwrites_for(self.everyone).connect)

    async def test_a_row_from_before_keeps_to_what_it_changed(self):
        await self.channel.set_permissions(self.everyone, overwrite=discord.PermissionOverwrite(send_messages=False, add_reactions=True))
        await self.db.run(
            "INSERT INTO lockdowns (guild_id, channel_id, previous, created_at) VALUES (?, ?, ?, ?)",
            (self.guild.id, self.channel.id, '{"send_messages": null, "send_messages_in_threads": null, "create_public_threads": null, "create_private_threads": null}', time.time()),
        )
        await self.unlock()
        overwrite = self.channel.overwrites_for(self.everyone)
        self.assertIsNone(overwrite.send_messages)
        self.assertIs(overwrite.add_reactions, True)

    async def test_locking_over_a_row_from_before_can_still_undo_the_new_fields(self):
        await self.channel.set_permissions(self.everyone, overwrite=discord.PermissionOverwrite(send_messages=False, add_reactions=True))
        await self.db.run(
            "INSERT INTO lockdowns (guild_id, channel_id, previous, created_at) VALUES (?, ?, ?, ?)",
            (self.guild.id, self.channel.id, '{"send_messages": null}', time.time()),
        )
        await self.lock()
        self.assertIs(self.channel.overwrites_for(self.everyone).add_reactions, False)
        await self.unlock()
        self.assertIs(self.channel.overwrites_for(self.everyone).add_reactions, True)

    async def test_it_leaves_out_a_bit_the_bot_doesnt_hold(self):
        self.role_bot.permissions = FakePermissions("manage_roles", "send_messages", "send_messages_in_threads", "create_public_threads", "create_private_threads")
        self.everyone.permissions = FakePermissions("view_channel", "send_messages")
        await self.lock()
        overwrite = self.channel.overwrites_for(self.everyone)
        self.assertIs(overwrite.send_messages, False)
        self.assertIsNone(overwrite.add_reactions)

    async def test_it_refuses_when_it_cant_deny_send(self):
        self.role_bot.permissions = FakePermissions("manage_roles")
        self.everyone.permissions = FakePermissions("view_channel")
        with self.assertRaises(ModerationError):
            await self.cog_._lock_channel(self.guild, self.channel, self.admin, None, None)
        self.assertIsNone(await self.db.one("SELECT * FROM lockdowns"))

    def test_the_add_link_grants_what_a_lock_or_a_mute_sets(self):
        # Discord lets a bot set a permission in a channel overwrite only when it holds that permission, and Administrator holds them all.
        for name in {*LOCK_FIELDS, *MUTE_DENIED}:
            self.assertTrue(ADD_PERMISSIONS.administrator or getattr(ADD_PERMISSIONS, name), name)


class StaffKeepPostingTest(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.staff_role = self.guild.role("Helpers", 6)
        self.helper = FakeMember(self.guild, "helper", self.staff_role)
        await self.bot.settings.save(self.guild.id, "core", {"dashboard_staff_roles": [self.staff_role.id]})

    async def test_a_staff_role_keeps_posting_and_it_comes_off_after(self):
        ctx = await self.lock()
        self.assertIs(self.channel.overwrites_for(self.staff_role).send_messages, True)
        self.assertIn("Helpers", ctx.oks[0])
        await self.unlock()
        self.assertNotIn(self.staff_role, self.channel.overwrites)

    async def test_the_bot_manager_role_counts(self):
        manager = self.guild.role("Manager", 7)
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": manager.id})
        await self.lock()
        self.assertIs(self.channel.overwrites_for(manager).send_messages, True)

    async def test_a_staff_role_denied_there_stays_denied(self):
        await self.channel.set_permissions(self.staff_role, overwrite=discord.PermissionOverwrite(send_messages=False))
        await self.lock()
        self.assertIs(self.channel.overwrites_for(self.staff_role).send_messages, False)

    async def test_a_channel_closed_to_everyone_before_doesnt_open_to_staff(self):
        await self.channel.set_permissions(self.everyone, overwrite=discord.PermissionOverwrite(send_messages=False))
        await self.lock()
        self.assertIsNone(self.channel.overwrites_for(self.staff_role).send_messages)

    async def test_with_no_staff_roles_the_reply_says_so(self):
        await self.bot.settings.save(self.guild.id, "core", {"dashboard_staff_roles": []})
        ctx = await self.lock()
        self.assertIn("No role can post here now except Administrators", ctx.oks[0])

    async def test_a_server_lock_names_who_keeps_posting(self):
        ctx = self.context(self.admin)
        await self.cog_.lockdown_server.callback(self.cog_, ctx, None, reason=None)
        self.assertIn("Helpers", ctx.oks[0])
        await self.cog_.unlockdown_server.callback(self.cog_, self.context(self.admin))
        self.assertNotIn(self.staff_role, self.channel.overwrites)


class RoleAllowsTest(Base):
    async def test_a_members_role_allow_comes_off_for_the_lock(self):
        await self.channel.set_permissions(self.role_member, overwrite=discord.PermissionOverwrite(send_messages=True, read_message_history=True))
        await self.lock()
        overwrite = self.channel.overwrites_for(self.role_member)
        self.assertIsNone(overwrite.send_messages)
        self.assertIs(overwrite.read_message_history, True)
        await self.unlock()
        self.assertIs(self.channel.overwrites_for(self.role_member).send_messages, True)

    async def test_a_moderator_role_keeps_its_allow(self):
        strong = self.guild.role("Strong", 6, "ban_members")
        FakeMember(self.guild, "strong", strong)
        await self.channel.set_permissions(strong, overwrite=discord.PermissionOverwrite(send_messages=True))
        ctx = await self.lock()
        self.assertIs(self.channel.overwrites_for(strong).send_messages, True)
        self.assertIn("can still post: Strong", ctx.oks[0])

    async def test_a_staff_role_keeps_its_allow(self):
        staff = self.guild.role("Helpers", 6)
        FakeMember(self.guild, "helper", staff)
        await self.bot.settings.save(self.guild.id, "core", {"dashboard_staff_roles": [staff.id]})
        await self.channel.set_permissions(staff, overwrite=discord.PermissionOverwrite(send_messages=True))
        await self.lock()
        await self.unlock()
        self.assertIs(self.channel.overwrites_for(staff).send_messages, True)

    async def test_a_role_only_bots_hold_keeps_its_allow(self):
        bots = self.guild.role("Bots", 6)
        FakeMember(self.guild, "other bot", bots, bot=True)
        await self.channel.set_permissions(bots, overwrite=discord.PermissionOverwrite(send_messages=True))
        await self.lock()
        self.assertIs(self.channel.overwrites_for(bots).send_messages, True)

    async def test_a_role_deleted_during_the_lock_doesnt_stop_the_unlock(self):
        await self.channel.set_permissions(self.role_member, overwrite=discord.PermissionOverwrite(send_messages=True))
        await self.lock()
        self.guild.roles.remove(self.role_member)
        ctx = await self.unlock()
        self.assertEqual(ctx.fails, [])
        self.assertIsNone(await self.db.one("SELECT * FROM lockdowns"))

    async def test_a_failed_lock_leaves_nothing_behind(self):
        self.role_bot.permissions = FakePermissions("manage_roles", "send_messages")
        real = self.channel.set_permissions

        async def refuse_everyone(target: Any, **kwargs: Any) -> None:
            if target is self.everyone:
                raise discord.Forbidden(_FakeResponse(403), "no")
            await real(target, **kwargs)
        self.channel.set_permissions = refuse_everyone
        ctx = await self.lock()
        self.assertTrue(ctx.fails)
        self.assertNotIn(self.me, self.channel.overwrites)
        self.assertIsNone(await self.db.one("SELECT * FROM lockdowns"))


class HiddenChannelTest(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.manager = FakeMember(self.guild, "manager", self.guild.role("Channels", 4, "manage_channels"))

        class Hidden(FakeChannel):
            def permissions_for(inner, member: Any) -> Any:
                return FakePermissions() if member is self.manager else member.guild_permissions
        self.hidden = Hidden(self.guild, "admin-only")

    async def test_a_lock_refuses_a_channel_they_cant_see(self):
        ctx = self.context(self.manager)
        await self.cog_.lockdown_channel.callback(self.cog_, ctx, self.hidden, None, reason=None)
        self.assertEqual(ctx.fails, ["You can't see that channel."])
        self.assertIsNone(await self.db.one("SELECT * FROM lockdowns"))

    async def test_an_unlock_refuses_too(self):
        await self.lock(self.hidden)
        ctx = self.context(self.manager)
        await self.cog_.unlockdown_channel.callback(self.cog_, ctx, self.hidden)
        self.assertEqual(ctx.fails, ["You can't see that channel."])
        self.assertIsNotNone(await self.db.one("SELECT * FROM lockdowns"))


class SetupTest(Base):
    async def test_it_refuses_a_server_whose_everyone_lacks_send(self):
        talker = self.guild.role("Talker", 2, "send_messages")
        self.everyone.permissions = FakePermissions("view_channel")
        ctx = self.context(self.admin)
        await self.cog_.lockdown_setup.callback(self.cog_, ctx, True)
        self.assertIn("@everyone", ctx.fails[0])
        self.assertTrue(talker.permissions.send_messages)

    async def test_moderator_roles_keep_send(self):
        talker = self.guild.role("Talker", 2, "send_messages")
        moderators = self.guild.role("Mods", 3, "send_messages", "kick_members")
        await self.cog_.lockdown_setup.callback(self.cog_, self.context(self.admin), True)
        self.assertFalse(talker.permissions.send_messages)
        self.assertTrue(moderators.permissions.send_messages)

    async def test_the_preview_tells_the_truth_and_lists_the_allows(self):
        self.guild.role("Talker", 2, "send_messages")
        await self.channel.set_permissions(self.role_member, overwrite=discord.PermissionOverwrite(send_messages=True))
        ctx = self.context(self.admin)
        await self.cog_.lockdown_setup.callback(self.cog_, ctx, False)
        self.assertNotIn("so a lockdown reaches them", ctx.oks[0])
        self.assertIn("keep it through @everyone", ctx.oks[0])
        self.assertIn(self.channel.mention, ctx.oks[0])

    async def test_the_activity_log_keeps_which_roles_changed(self):
        talker = self.guild.role("Talker", 2, "send_messages")
        await self.cog_.lockdown_setup.callback(self.cog_, self.context(self.admin), True)
        row = await self.db.one("SELECT detail FROM activity WHERE action = 'lockdown.setup'")
        self.assertIn(str(talker.id), row["detail"])


class StillPostsAfterAMuteTest(Base):
    async def test_a_role_allow_shows_in_the_reply(self):
        await self.channel.set_permissions(self.role_member, overwrite=discord.PermissionOverwrite(send_messages=True))
        ctx = self.context(self.admin)
        await self.cog_.mute.callback(self.cog_, ctx, self.member, None, reason=None)
        self.assertIn(f"can still post in {self.channel.mention}", ctx.oks[0])
        self.assertIn("hardmute", ctx.oks[0])

    async def test_no_allow_says_nothing(self):
        ctx = self.context(self.admin)
        await self.cog_.mute.callback(self.cog_, ctx, self.member, None, reason=None)
        self.assertNotIn("still post", ctx.oks[0])

    async def test_a_hardmute_takes_that_role_so_it_says_nothing(self):
        await self.channel.set_permissions(self.role_member, overwrite=discord.PermissionOverwrite(send_messages=True))
        ctx = self.context(self.admin)
        await self.cog_.hardmute.callback(self.cog_, ctx, self.member, None, reason=None)
        self.assertNotIn("still post", ctx.oks[0])

    async def test_their_own_allow_counts_even_after_a_hardmute(self):
        await self.channel.set_permissions(self.member, overwrite=discord.PermissionOverwrite(send_messages=True))
        ctx = self.context(self.admin)
        await self.cog_.hardmute.callback(self.cog_, ctx, self.member, None, reason=None)
        self.assertIn("can still post", ctx.oks[0])

    async def test_muterole_update_lists_those_channels(self):
        await self.channel.set_permissions(self.role_member, overwrite=discord.PermissionOverwrite(send_messages=True))
        ctx = self.context(self.admin)
        await self.cog_.muterole_update.callback(self.cog_, ctx)
        self.assertIn(self.channel.mention, ctx.oks[0])


class MuteOverwriteBitsTest(Base):
    async def test_it_sets_only_what_the_bot_holds_and_says_what_it_left_out(self):
        self.role_bot.permissions = FakePermissions("manage_roles", "send_messages", "send_messages_in_threads", "create_public_threads", "create_private_threads", "add_reactions")
        self.everyone.permissions = FakePermissions("view_channel")
        ctx = self.context(self.admin)
        await self.cog_.muterole_update.callback(self.cog_, ctx)
        overwrite = self.channel.overwrites_for(self.role_muted)
        self.assertIs(overwrite.send_messages, False)
        self.assertIsNone(overwrite.use_application_commands)
        self.assertIn("use_application_commands", ctx.oks[0])

    async def test_a_new_channel_skips_a_role_that_cant_be_the_mute_role(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": self.everyone.id})
        fresh = FakeChannel(self.guild, "new")
        await self.cog_.on_guild_channel_create(fresh)
        self.assertEqual(fresh.overwrites, {})


class TypedLockTest(LiveBase):
    async def rows(self) -> list[dict]:
        return await self.db.all("SELECT channel_id, reason, expires_at FROM lockdowns WHERE guild_id = ? ORDER BY channel_id", (self.guild_id,))

    async def test_a_near_miss_of_a_channel_name_asks_and_locks_nothing(self):
        name = self.text["name"]
        await self.typed(f"!lockdown {name[:-1]}x")
        self.assertIn("Did you mean", self.replies()[-1])
        self.assertEqual(await self.rows(), [])

    async def test_a_word_near_no_channel_stays_a_reason(self):
        await self.typed("!lockdown qqqqqqqq please stop")
        rows = await self.rows()
        self.assertEqual(rows[0]["reason"], "qqqqqqqq please stop")


class TypedLengthTest(ModBase):
    async def mute(self, first: Any, rest: Any) -> Any:
        ctx = self.context(self.admin)
        await self.cog_.mute.callback(self.cog_, ctx, self.member, first, reason=rest)
        return ctx

    async def last(self) -> dict:
        return (await self.cases())[-1]

    async def test_for_a_length_up_front(self):
        await self.mute("for", "3h spamming")
        row = await self.last()
        self.assertEqual((row["seconds"], row["reason"]), (10800, "spamming"))

    async def test_for_a_word_stays_a_reason(self):
        await self.mute("for", "spamming")
        row = await self.last()
        self.assertEqual((row["seconds"], row["reason"]), (None, "for spamming"))

    async def test_a_reason_ending_in_for_a_length(self):
        await self.mute("being", "rude for 2 hours")
        row = await self.last()
        self.assertEqual((row["seconds"], row["reason"]), (7200, "being rude"))

    async def test_the_last_for_decides(self):
        await self.mute("warned", "for spam for 1d")
        row = await self.last()
        self.assertEqual((row["seconds"], row["reason"]), (86400, "warned for spam"))

    async def test_a_for_that_isnt_a_length_stays_in_the_reason(self):
        await self.mute("banned", "for spam")
        row = await self.last()
        self.assertEqual((row["seconds"], row["reason"]), (None, "banned for spam"))

    async def test_a_bare_length_at_the_end_asks(self):
        for first, rest in (("spamming", "3h"), ("printing", "in 3d"), ("spamming", "for ages 3 hours")):
            ctx = await self.mute(first, rest)
            self.assertIn("at the end mean a length", ctx.fails[0], rest)
        self.assertEqual(await self.cases(), [])

    async def test_a_period_makes_it_a_reason(self):
        await self.mute("printing", "in 3d.")
        self.assertEqual((await self.last())["reason"], "printing in 3d.")

    async def test_the_reply_says_when_a_mute_has_no_end(self):
        ctx = await self.mute("rude", None)
        self.assertIn("with no end", ctx.oks[0])

    async def test_a_length_doesnt_say_no_end(self):
        ctx = await self.mute("3h", "rude")
        self.assertNotIn("with no end", ctx.oks[0])


class FoundReaderTest(LiveBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        patcher = mock.patch.object(discord.Guild, "query_members", new=mock.AsyncMock(return_value=[]))  # the gateway answers that nobody has that name
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_typo_in_a_record_command_says_not_found(self):
        for typed in ("!warns nobodyhere", "!notes nobodyhere", "!modlog nosuchperson", "!modlog from nosuchperson", "!notes view nosuchperson", "!modlog export nosuchperson"):
            self.net.posted.clear()
            await self.typed(typed)
            self.assertIn("i can't find a member called", " ".join(self.replies()).lower(), typed)
            self.assertNotIn("Newest warnings", str(self.net.posted), typed)

    async def test_a_mention_still_works(self):
        await self.typed(f"!warns <@{self.member_id}>")
        self.assertIn("no warnings", str(self.net.posted))


class RestoredPastEndTest(Base):
    def joined_earlier(self, member: Any) -> None:
        member.joined_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)

    async def row(self, member: Any, *, removed: str = "[]", expired: bool = True) -> None:
        await self.db.run(
            "INSERT INTO active_mutes (guild_id, user_id, kind, role_id, removed_roles, started_at, expires_at) VALUES (?, ?, 'hardmute', ?, ?, ?, ?)",
            (self.guild.id, member.id, self.role_muted.id, removed, time.time() - 7200, time.time() - 3600 if expired else None),
        )

    async def test_someone_without_the_role_gets_nothing_back(self):
        trusted = self.guild.role("Trusted", 3)
        self.joined_earlier(self.member)
        await self.row(self.member, removed=f"[{trusted.id}]")
        await self.cog_.after_restore(self.guild.id)
        self.assertNotIn(trusted, self.member.roles)
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))
        self.assertEqual(await self.cases(), [])

    async def test_someone_who_came_back_after_it_ended_gets_their_roles(self):
        trusted = self.guild.role("Trusted", 3)
        self.member.joined_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        await self.row(self.member, removed=f"[{trusted.id}]")
        await self.cog_.reconcile(self.guild)
        self.assertIn(trusted, self.member.roles)
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_someone_still_holding_the_role_comes_out(self):
        trusted = self.guild.role("Trusted", 3)
        self.member.roles.append(self.role_muted)
        await self.row(self.member, removed=f"[{trusted.id}]")
        await self.cog_.after_restore(self.guild.id)
        self.assertNotIn(self.role_muted, self.member.roles)
        self.assertIn(trusted, self.member.roles)
        self.assertEqual((await self.cases())[-1]["action"], "unmute")

    async def test_a_timer_still_on_the_way_is_left_to_it(self):
        self.member.roles.append(self.role_muted)
        await self.row(self.member)
        await self.bot.timers.schedule("moderation.unmute", time.time() + 5, {"guild_id": self.guild.id, "user_id": self.member.id}, guild_id=self.guild.id, key=f"{self.guild.id}:{self.member.id}")
        await self.cog_.reconcile(self.guild)
        self.assertIn(self.role_muted, self.member.roles)

    async def test_a_tempban_past_its_end_is_named_to_staff(self):
        row = await self.cog_.record_case(self.guild, "tempban", self.member, self.admin, seconds=60)
        await self.db.run("UPDATE cases SET expires_at = ? WHERE number = ?", (time.time() - 10, row["number"]))
        self.modlog.sent.clear()
        await self.cog_.after_restore(self.guild.id)
        self.assertIn(str(self.member.id), self.modlog.sent[-1].embeds[0].description)


class RejoinConfirmedTest(Base):
    async def rejoined_at(self, member: Any, moment: datetime.datetime) -> None:
        member.joined_at = moment

    async def test_a_rejoin_while_up_then_a_hand_unmute_stays_off(self):
        started = time.time() - 7200
        await self.db.run("INSERT INTO active_mutes (guild_id, user_id, kind, role_id, removed_roles, started_at) VALUES (?, ?, 'mute', ?, '[]', ?)", (self.guild.id, self.member.id, self.role_muted.id, started))
        self.member.joined_at = datetime.datetime.fromtimestamp(time.time() - 3600, datetime.timezone.utc)
        await self.cog_.on_member_join(self.member)
        self.assertIn(self.role_muted, self.member.roles)
        self.member.roles.remove(self.role_muted)  # by hand, while the bot is away
        await self.cog_.reconcile(self.guild)
        self.assertNotIn(self.role_muted, self.member.roles)
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_a_rejoin_during_downtime_is_muted_again(self):
        started = time.time() - 7200
        await self.db.run("INSERT INTO active_mutes (guild_id, user_id, kind, role_id, removed_roles, started_at, confirmed_at) VALUES (?, ?, 'mute', ?, '[]', ?, ?)", (self.guild.id, self.member.id, self.role_muted.id, started, started))
        self.member.joined_at = datetime.datetime.fromtimestamp(time.time() - 60, datetime.timezone.utc)
        await self.cog_.reconcile(self.guild)
        self.assertIn(self.role_muted, self.member.roles)
