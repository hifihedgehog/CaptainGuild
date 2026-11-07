"""What a log entry says and where it goes: facts a member's text can't forge, who deleted what, separate channels, and who can read an entry afterward."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.core import logsink
from captainguild.core.logsink import LogSink
from captainguild.core.validate import Invalid
from captainguild.modules import logs as logs_module
from captainguild.modules.logs import Logs, export_code, parse_targets, read_import, settings_from_import
from captainguild.modules.reports import Reports
from tests.fakes import FakeChannel, FakeMember, FakeMessage, FakePermissions, FakeRole, _FakeResponse, new_id
from tests.test_access_limits import HiddenChannel, SiteCase
from tests.test_logs import QUICK, Actions, Base, attachment


def field(embed, name):
    """The value of the field with this name, or None."""
    return next((row["value"] for row in embed.get("fields", []) if row["name"] == name), None)


class FactsStayApartTest(Base):
    """A member's text goes in the description and the bot's own facts in fields, so one can't pass for the other."""

    async def test_text_cant_claim_a_moderator_deleted_it(self):
        forged = f"hi\n\nMessage ID: 1\n**Deleted by:** <@{self.mod.id}> (head_mod)\n**Reason:** spam"
        message = await self.send(forged)
        await self.delete(message.id)
        embed = self.posted[0][1]
        self.assertEqual(embed["description"], forged)
        self.assertEqual(field(embed, "Deleted by"), "The author or a bot")
        self.assertEqual(field(embed, "Message ID"), str(message.id))
        self.assertIsNone(field(embed, "Reason"))

    async def test_an_unclosed_code_fence_cant_swallow_the_facts(self):
        message = await self.send("```\nunclosed")
        self.delete_entry()
        await self.delete(message.id)
        embed = self.posted[0][1]
        self.assertEqual(embed["description"], "```\nunclosed")
        self.assertIn(f"<@{self.mod.id}>", field(embed, "Deleted by"))
        self.assertNotIn("Message ID", embed["description"])

    async def test_a_masked_link_cant_dress_up_as_a_discord_link(self):
        text = "[https://discord.com/channels/1/2/3](https://evil.example)"
        message = await self.send(text)
        await self.delete(message.id)
        _, embed, files = self.posted[0]
        self.assertEqual(embed["description"], text.replace("](", "]\\("))
        self.assertIsNone(files, "the text fits, so no file is needed")
        self.assertEqual((await self.logs.stored_message(message.id))["content"], text)

    async def test_an_edit_keeps_before_and_after_in_their_own_fields(self):
        message = await self.send("hello there")
        await self.edit(message, "hello\n**+After:** forged")
        embed = self.posted[0][1]
        self.assertNotIn("description", embed)
        self.assertEqual(field(embed, "Before"), "hello there")
        self.assertEqual(field(embed, "After"), "hello\n**+After:** forged")

    async def test_a_long_edit_clips_the_fields_and_attaches_the_whole_text(self):
        message = await self.send("x" * 1500)
        await self.edit(message, "y" * 1500)
        _, embed, files = self.posted[0]
        self.assertLessEqual(len(field(embed, "Before")), 1024)
        self.assertLessEqual(len(field(embed, "After")), 1024)
        text = files[0].fp.read().decode()
        self.assertIn("x" * 1500, text)
        self.assertIn("y" * 1500, text)

    async def test_a_short_edit_has_no_file(self):
        message = await self.send("short")
        await self.edit(message, "x" * 1024)
        self.assertIsNone(self.posted[0][2])

    async def test_a_pin_keeps_its_facts_apart_too(self):
        message = await self.send(f"Message ID: 999\n**By:** <@{self.admin.id}>")
        await self.logs.on_audit_log_entry_create(self.audit(Actions.message_pin, extra=SimpleNamespace(channel=self.channel, message_id=message.id), target=self.member))
        await self.turn_on("pins")
        await self.logs.on_audit_log_entry_create(self.audit(Actions.message_pin, extra=SimpleNamespace(channel=self.channel, message_id=message.id), target=self.member))
        embed = self.posted[0][1]
        self.assertEqual(embed["description"], f"Message ID: 999\n**By:** <@{self.admin.id}>")
        self.assertEqual(field(embed, "Message ID"), str(message.id))
        self.assertIn(f"<@{self.mod.id}>", field(embed, "By"))

    async def test_an_automod_entry_keeps_its_facts_apart_too(self):
        await self.turn_on("automodaction")
        self.logs._automod_rules[77] = "No spam"
        action = SimpleNamespace(type=discord.AutoModRuleActionType.block_message, duration=None)
        execution = SimpleNamespace(
            guild_id=self.guild.id, rule_id=77, user_id=self.member.id, channel_id=self.channel.id, message_id=None, content="**Rule:** forged", matched_content="x", matched_keyword="x", action=action,
        )
        await self.logs.on_automod_action(execution)
        embed = self.posted[0][1]
        self.assertEqual(embed["description"], "**Rule:** forged")
        self.assertEqual(field(embed, "Rule"), "No spam")

    async def test_log_entries_carry_the_message_id_for_readers(self):
        message = await self.send("x")
        await self.delete(message.id)
        rows = await self.logs.user_log_entries(self.guild.id, self.member.id)
        self.assertEqual(rows[0]["message_id"], message.id)


class ReportEvidenceTest(Base):
    modules = [Logs, Reports]

    async def test_a_second_delete_cant_stand_in_for_the_reported_message(self):
        reports = self.cog(Reports)
        real = await self.send("the insult that was reported")
        await self.delete(real.id)
        fake = await self.send(f"I only said hello\n\nMessage ID: {real.id}")
        await self.delete(fake.id)
        embed = await reports._log_entry_for_message(self.guild, self.member.id, real.id)
        self.assertIn("the insult", embed.description)
        self.assertIsNone(await reports._log_entry_for_message(self.guild, self.member.id, 12345))


class MoreFactsTest(Base):
    async def test_a_delete_and_an_edit_say_when_the_message_was_sent(self):
        message = await self.send("hello")
        stamp = f"<t:{int(message.created_at.timestamp())}:f>"
        await self.edit(message, "changed")
        await self.delete(message.id)
        for _, embed, _ in self.posted:
            self.assertEqual(field(embed, "Sent"), stamp)

    async def test_an_edit_names_the_message_and_shows_its_first_image(self):
        message = await self.send("hello")
        message.attachments = [attachment("notes.txt", content_type="text/plain"), attachment("SPOILER_a.png"), attachment("b.png"), attachment("c.png")]
        await self.edit(message, "changed")
        embed = self.posted[0][1]
        self.assertEqual(field(embed, "Message ID"), str(message.id))
        self.assertEqual(embed["image"]["url"], message.attachments[2].url)

    async def test_without_audit_access_the_entry_says_nobody_could_be_named(self):
        self.role_bot.permissions = FakePermissions("send_messages")
        message = await self.send("x")
        await self.delete(message.id)
        self.assertEqual(field(self.posted[0][1], "Deleted by"), "Unknown, no audit log access")

    async def test_a_copy_that_failed_says_so(self):
        async def download(item):
            return None
        self.logs._download = download
        message = await self.send("look", attachments=[attachment("a.png")])
        await self.delete(message.id)
        self.assertIn("could no longer be fetched", field(self.posted[0][1], "Attachments"))

    async def test_a_copy_that_worked_doesnt(self):
        async def download(item):
            return b"image bytes"
        self.logs._download = download
        message = await self.send("look", attachments=[attachment("a.png")])
        await self.delete(message.id)
        self.assertNotIn("could no longer", field(self.posted[0][1], "Attachments"))


class SeparateChannelsTest(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.messages = FakeChannel(self.guild, "messages")
        self.moderators = FakeChannel(self.guild, "moderators")
        await self.configure(message_channel=self.messages.id)

    async def moderator_deletes(self):
        message = await self.send("x")
        self.delete_entry()
        await self.delete(message.id)

    async def test_with_nothing_set_everything_goes_where_it_went(self):
        await self.moderator_deletes()
        message = await self.send("mine")
        await self.delete(message.id)
        self.assertEqual([channel for channel, _, _ in self.posted], [self.messages.id, self.messages.id])

    async def test_a_moderators_delete_can_go_apart_from_a_members(self):
        await self.configure(moderator_delete_channel=self.moderators.id)
        await self.moderator_deletes()
        message = await self.send("mine")
        await self.delete(message.id)
        self.assertEqual([channel for channel, _, _ in self.posted], [self.moderators.id, self.messages.id])

    async def test_a_purge_goes_with_the_moderators(self):
        await self.configure(moderator_delete_channel=self.moderators.id)
        await self.turn_on("purge")
        message = await self.send("x")
        await self.logs.on_raw_bulk_message_delete(SimpleNamespace(message_ids={message.id}, channel_id=self.channel.id, guild_id=self.guild.id, cached_messages=[]))
        self.assertEqual(self.posted[0][0], self.moderators.id)

    async def test_member_deletes_can_be_off_with_moderator_deletes_on(self):
        await self.configure(events=["moddelete"])
        message = await self.send("mine")
        await self.delete(message.id)
        self.assertEqual(self.posted, [])
        await self.moderator_deletes()
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(await self.message_db.scalar("SELECT event FROM log_entries") if await self.logs.flush() is None else None, "moddelete")

    async def test_a_read_in_the_same_clock_tick_as_a_delete_is_read_again(self):
        # Some clocks move in steps of about 16 ms, so a delete can arrive at the same reading as the last look at the audit log.
        entry = self.delete_entry()
        reads = []

        async def read(guild, action, limit):
            reads.append(limit)
            return [entry]
        self.logs._delete_reads[self.guild.id] = (500.0, [])
        with mock.patch.object(self.logs, "_read_audit", read), mock.patch.object(logs_module, "DELETE_SETTLE", 0):
            entries = await self.logs._recent_deletes(self.guild, 500.0)
        self.assertEqual(entries, [entry])
        self.assertEqual(len(reads), 1)

    async def test_moderator_deletes_can_be_off_with_member_deletes_on(self):
        await self.configure(events=["delete"])
        await self.moderator_deletes()
        self.assertEqual(self.posted, [])
        message = await self.send("mine")
        await self.delete(message.id)
        self.assertEqual(len(self.posted), 1)

    async def test_a_channel_for_roles_names_and_timeouts(self):
        roles, names, timeouts, members = (FakeChannel(self.guild, name) for name in ("roles", "names", "timeouts", "members"))
        await self.configure(role_channel=roles.id, name_channel=names.id, timeout_channel=timeouts.id, member_channel=members.id)
        for event, wanted in (("role", roles), ("name", names), ("timeout", timeouts), ("removetimeout", timeouts), ("ban", members)):
            self.assertEqual(self.logs._channel_for(self.guild, event), wanted.id, event)

    async def test_one_thats_gone_falls_back(self):
        roles = FakeChannel(self.guild, "roles")
        members = FakeChannel(self.guild, "members")
        await self.configure(role_channel=roles.id, member_channel=members.id)
        self.guild.channels.remove(roles)
        self.assertEqual(self.logs._channel_for(self.guild, "role"), members.id)

    async def test_the_old_setups_delete_bit_turns_on_both(self):
        self.assertEqual(set(logs_module.events_from_bits(1)), {"delete", "moddelete"})

    async def test_the_new_channels_survive_export_and_import(self):
        await self.configure(moderator_delete_channel=self.moderators.id)
        code = export_code(self.settings("logging"))
        ids = {channel.id for channel in self.guild.channels}
        form = settings_from_import(read_import(code), self.logs.schema, ids, set())
        self.assertEqual(form["moderator_delete_channel"], self.moderators.id)
        form = settings_from_import(read_import(code), self.logs.schema, ids - {self.moderators.id}, set())
        self.assertIsNone(form["moderator_delete_channel"])

    async def test_the_commands_set_and_clear_them(self):
        for command, field_name in (
            (self.logs.log_moderator_delete_channel, "moderator_delete_channel"), (self.logs.log_role_channel, "role_channel"),
            (self.logs.log_name_channel, "name_channel"), (self.logs.log_timeout_channel, "timeout_channel"),
        ):
            ctx = self.context(self.admin)
            await command.callback(self.logs, ctx, self.moderators)
            self.assertEqual(self.settings("logging")[field_name], self.moderators.id)
            await command.callback(self.logs, ctx, None)
            self.assertIsNone(self.settings("logging")[field_name])


class BotMessagesTest(Base):
    async def robot_says(self, content="beep", *, author=None, embed=None):
        robot = author or FakeMember(self.guild, "robot", bot=True)
        message = self.message(content, author=robot, embeds=[embed] if embed else [])
        await self.logs.on_message(message)
        return message, robot

    async def test_a_moderator_removing_a_bots_message_is_logged_with_its_text(self):
        message, robot = await self.robot_says("beep boop")
        self.delete_entry(author=robot)
        await self.delete(message.id)
        embed = self.posted[0][1]
        self.assertEqual(embed["description"], "beep boop")
        self.assertIn(f"<@{self.mod.id}>", field(embed, "Deleted by"))
        self.assertEqual(embed["author"]["name"], "robot")

    async def test_a_webhooks_message_counts_too(self):
        hook = SimpleNamespace(id=new_id(), name="Proxy", bot=True, display_avatar=None)
        message = self.message("through a webhook", author=hook)
        message.webhook_id = hook.id
        await self.logs.on_message(message)
        self.delete_entry(author=hook)
        await self.delete(message.id)
        self.assertEqual(self.posted[0][1]["description"], "through a webhook")

    async def test_an_embed_with_no_text_is_quoted(self):
        inner = discord.Embed(title="Rules", description="Be kind")
        inner.add_field(name="One", value="No spam")
        inner.set_author(name="Staff")
        inner.set_footer(text="Thanks")
        message, robot = await self.robot_says("", embed=inner)
        self.delete_entry(author=robot)
        await self.delete(message.id)
        description = self.posted[0][1]["description"]
        for part in ("Rules", "Be kind", "One", "No spam", "Staff", "Thanks"):
            self.assertIn(part, description)

    async def test_what_is_held_for_a_bot_stays_small(self):
        await self.robot_says("x" * 5000)
        kept = next(iter(self.logs._bot_messages.values()))
        self.assertLessEqual(len(kept[2]), logs_module.BOT_TEXT_KEPT)
        self.assertLessEqual(logs_module.BOT_MESSAGES_KEPT * logs_module.BOT_TEXT_KEPT, 8 * 1024 * 1024)

    async def test_a_bot_deleting_its_own_message_follows_the_switch(self):
        message, _ = await self.robot_says("temporary")
        await self.delete(message.id)
        self.assertEqual(self.posted[0][1]["description"], "temporary")
        await self.configure(log_bot_actions=False)
        message, _ = await self.robot_says("temporary again")
        await self.delete(message.id)
        self.assertEqual(len(self.posted), 1)

    async def test_this_bots_own_deletes_never_show(self):
        self.bot._connection.user = self.me
        message, _ = await self.robot_says("from this bot", author=self.me)
        await self.delete(message.id)
        self.assertEqual(self.posted, [])

    async def test_a_moderator_removing_this_bots_message_shows(self):
        self.bot._connection.user = self.me
        message, _ = await self.robot_says("from this bot", author=self.me)
        self.delete_entry(author=self.me)
        await self.delete(message.id)
        self.assertEqual(self.posted[0][1]["description"], "from this bot")

    async def test_a_purge_file_has_a_bots_text(self):
        await self.turn_on("purge")
        message, robot = await self.robot_says("beep")
        await self.logs.on_raw_bulk_message_delete(SimpleNamespace(message_ids={message.id}, channel_id=self.channel.id, guild_id=self.guild.id, cached_messages=[]))
        text = self.posted[0][2][0].fp.read().decode()
        self.assertIn(f"robot ({robot.id}): beep", text)

    async def test_ignored_members_and_prefixes_cover_bots(self):
        message, robot = await self.robot_says("!skip")
        await self.configure(ignored_prefixes=["!"])
        self.delete_entry(author=robot)
        await self.delete(message.id)
        self.assertEqual(self.posted, [])


class OldMessagesTest(Base):
    async def test_a_message_older_than_the_store_leaves_a_line(self):
        message_id = discord.utils.time_snowflake(discord.utils.utcnow())
        await self.delete(message_id)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Message deleted in #general")
        self.assertIn("isn't in the store", embed["description"])
        self.assertEqual(field(embed, "Message ID"), str(message_id))
        self.assertEqual(field(embed, "Sent"), f"<t:{int(discord.utils.snowflake_time(message_id).timestamp())}:f>")
        self.assertEqual(field(embed, "Deleted by"), "The author or a bot")

    async def test_someone_named_still_shows_with_who_wrote_it(self):
        self.delete_entry()
        await self.delete(new_id())
        embed = self.posted[0][1]
        self.assertIn(f"<@{self.mod.id}>", field(embed, "Deleted by"))
        self.assertIn(f"<@{self.member.id}>", field(embed, "Author"))


class WithoutMessageContentTest(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.reads_message_text = False

    async def test_nothing_empty_goes_in_the_store(self):
        await self.send("")
        await self.logs.flush()
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM messages"), 0)

    async def test_an_edit_logs_nothing(self):
        message = self.message("")
        message.edited_at = discord.utils.utcnow()
        await self.logs.on_raw_message_edit(SimpleNamespace(message=message, guild_id=self.guild.id, channel_id=self.channel.id, message_id=message.id))
        self.assertEqual(self.posted, [])

    async def test_a_delete_nobody_is_named_for_logs_nothing(self):
        await self.delete(new_id())
        self.assertEqual(self.posted, [])

    async def test_a_moderators_delete_still_logs(self):
        self.delete_entry()
        await self.delete(new_id())
        self.assertEqual(len(self.posted), 1)


class TwoDeletesAtOnceTest(Base):
    """Discord's audit entry says who deleted a message by this author in this channel. It doesn't say which message, so two deletes close together can't be told apart."""

    async def both_waiting(self, count):
        """Holds the first read of the audit log until every delete has come in, as a settle time does on a real server. A later read
        goes straight through, since by then some of the deletes may be done."""
        real = self.bot.audit.recent
        held = False

        async def recent(guild, action, limit=5):
            nonlocal held
            while not held and sum(self.logs._delete_waiting.values()) < count:
                await asyncio.sleep(0)
            held = True
            return await real(guild, action, limit=limit)
        return mock.patch.object(self.bot.audit, "recent", recent)

    async def test_when_they_cant_be_told_apart_neither_names_anyone(self):
        first = await self.send("the author removes this")
        second = await self.send("the moderator removes this")
        await self.logs.flush()
        self.delete_entry(count=1)
        with await self.both_waiting(2):
            await asyncio.gather(self.delete(first.id), self.delete(second.id))
        self.assertEqual(len(self.posted), 2)
        for _, embed, _ in self.posted:
            self.assertTrue(field(embed, "Deleted by").startswith("Uncertain"), embed)
            self.assertNotIn(str(self.mod.id), json.dumps(embed))
        await self.logs.flush()
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM messages WHERE deleted_by IS NOT NULL"), 0)
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM log_entries WHERE actor_id IS NOT NULL"), 0)

    async def test_uncertain_entries_go_with_the_moderators(self):
        moderators = FakeChannel(self.guild, "moderators")
        await self.configure(moderator_delete_channel=moderators.id)
        first = await self.send("one")
        second = await self.send("two")
        await self.logs.flush()
        self.delete_entry(count=1)
        with await self.both_waiting(2):
            await asyncio.gather(self.delete(first.id), self.delete(second.id))
        self.assertEqual([channel for channel, _, _ in self.posted], [moderators.id, moderators.id])

    async def test_a_burst_the_count_covers_still_names_them(self):
        messages = [await self.send(f"message {number}") for number in range(3)]
        await self.logs.flush()
        self.delete_entry(count=3)
        with await self.both_waiting(3):
            await asyncio.gather(*(self.delete(message.id) for message in messages))
        for _, embed, _ in self.posted:
            self.assertIn(f"<@{self.mod.id}>", field(embed, "Deleted by"))

    async def test_a_later_delete_isnt_caught_up_in_it(self):
        first = await self.send("one")
        second = await self.send("two")
        await self.logs.flush()
        entry = self.delete_entry(count=1)
        with await self.both_waiting(2):
            await asyncio.gather(self.delete(first.id), self.delete(second.id))
        third = await self.send("three")
        entry.extra.count = 2
        await self.delete(third.id)
        self.assertIn(f"<@{self.mod.id}>", field(self.posted[-1][1], "Deleted by"))
        self.assertEqual(self.logs._delete_doubts, {})
        self.assertEqual(self.logs._delete_waiting, {})


class DefaultEventsTest(Base):
    def test_a_fresh_server_logs_purges_deleted_roles_and_timeouts(self):
        for event in ("purge", "roledelete", "timeout", "removetimeout", "moddelete"):
            self.assertIn(event, logs_module.DEFAULT_EVENTS)
            self.assertIn(event, logs_module.Logs.schema.fields["events"].default)


class RoleEntriesTest(Base):
    def changed(self, before, after):
        old = SimpleNamespace(id=self.member.id, roles=before, guild=self.guild, name="member", nick=None, timed_out_until=None, display_avatar=self.member.display_avatar, is_timed_out=lambda: False)
        new = SimpleNamespace(id=self.member.id, roles=after, guild=self.guild, name="member", nick=None, timed_out_until=None, display_avatar=self.member.display_avatar, is_timed_out=lambda: False)
        return old, new

    async def test_a_role_is_named_as_well_as_mentioned(self):
        await self.logs.on_member_update(*self.changed([], [self.role_member]))
        self.assertIn(f"{self.role_member.mention} (Member)", self.posted[0][1]["description"])

    async def test_a_boost_role_says_discord_gave_it(self):
        booster = self.guild.role("Booster", 2, managed=True)
        booster.premium = True
        await self.logs.on_member_update(*self.changed([], [booster]))
        self.assertIn("**By:** Discord", self.posted[0][1]["description"])

    async def test_a_bots_own_role_says_discord_gave_it(self):
        await self.logs.on_member_update(*self.changed([], [self.guild.role("A Bot", 2, managed=True)]))
        self.assertIn("**By:** Discord", self.posted[0][1]["description"])

    async def test_an_integration_role_says_so(self):
        linked = self.guild.role("Subscriber", 2, managed=True)
        linked.integration = True
        await self.logs.on_member_update(*self.changed([], [linked]))
        self.assertIn("**By:** an integration", self.posted[0][1]["description"])

    async def test_a_plain_role_with_no_entry_names_nobody(self):
        await self.logs.on_member_update(*self.changed([], [self.role_member]))
        self.assertNotIn("**By:**", self.posted[0][1]["description"])

    async def test_a_mix_names_nobody(self):
        booster = self.guild.role("Booster", 2, managed=True)
        booster.premium = True
        await self.logs.on_member_update(*self.changed([], [booster, self.role_member]))
        self.assertNotIn("**By:**", self.posted[0][1]["description"])

    async def test_the_list_on_a_deleted_role_has_names(self):
        await self.turn_on("roledelete")
        self.member.roles.append(self.role_muted)
        await self.logs.on_guild_role_delete(self.role_muted)
        self.assertIn(f"<@{self.member.id}> (member)", self.posted[0][1]["description"])


class BotSwitchCoversDeletesTest(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.robot = FakeMember(self.guild, "robot", bot=True)
        await self.turn_on("purge")

    async def bot_deletes(self):
        message = await self.send("x")
        self.delete_entry(user=self.robot)
        await self.delete(message.id)

    async def bot_purges(self):
        message = await self.send("y")
        self.guild.audit_entries.append(self.audit(Actions.message_bulk_delete, user=self.robot, target=self.channel, extra=SimpleNamespace(count=1)))
        await self.logs.on_raw_bulk_message_delete(SimpleNamespace(message_ids={message.id}, channel_id=self.channel.id, guild_id=self.guild.id, cached_messages=[]))

    async def test_on_they_show(self):
        await self.bot_deletes()
        self.assertEqual(len(self.posted), 1)

    async def test_off_a_bots_delete_stays_out(self):
        await self.configure(log_bot_actions=False)
        await self.bot_deletes()
        self.assertEqual(self.posted, [])

    async def test_off_a_bots_purge_stays_out(self):
        await self.configure(log_bot_actions=False)
        await self.bot_purges()
        self.assertEqual(self.posted, [])

    async def test_off_a_person_still_shows(self):
        await self.configure(log_bot_actions=False)
        message = await self.send("x")
        self.delete_entry()
        await self.delete(message.id)
        self.assertEqual(len(self.posted), 1)


class ImportCodeShapesTest(Base):
    def test_odd_codes_are_refused_with_the_sentence(self):
        for code in ('{"a":' + "[" * 100000, "\N{SUPERSCRIPT TWO}", "\N{ARABIC-INDIC DIGIT THREE}" * 3):
            with self.assertRaises(Invalid, msg=code[:10]):
                read_import(code)

    def test_a_list_of_the_wrong_shape_is_refused(self):
        with self.assertRaises(Invalid):
            settings_from_import({"logging": 1, "ignored_entities": 5}, Logs.schema, set(), set())

    def test_plain_numbers_still_work(self):
        self.assertEqual(read_import("3"), {"logging": 3})


class ThreadTargetTest(Base):
    async def test_a_threads_id_is_a_channel_and_not_a_person(self):
        thread = SimpleNamespace(id=123456789012345678, name="a thread")
        real = self.guild.get_channel_or_thread
        self.guild.get_channel_or_thread = lambda channel_id: thread if channel_id == thread.id else real(channel_id)
        channels, members, roles, unknown = parse_targets(self.guild, str(thread.id))
        self.assertEqual((channels, members, roles, unknown), ([thread.id], [], [], []))


class RefusedChannelTest(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.server_log = FakeChannel(self.guild, "server-log")
        self.messages = FakeChannel(self.guild, "messages")
        await self.configure(message_channel=self.messages.id, server_channel=self.server_log.id)

    def notices(self):
        return [(channel, embed) for channel, embed, _ in self.posted if embed["title"] == "A log channel needs a look"]

    async def test_after_a_refusal_entries_go_to_the_default_channel(self):
        self.assertEqual(self.logs._channel_for(self.guild, "delete"), self.messages.id)
        self.logs._channel_refused(self.messages.id)
        self.assertEqual(self.logs._channel_for(self.guild, "delete"), self.log_channel.id)

    async def test_staff_hear_once_an_hour_and_through_another_channel(self):
        self.logs._channel_refused(self.messages.id)
        self.logs._channel_refused(self.messages.id)
        self.assertEqual(len(self.notices()), 1)
        channel, embed = self.notices()[0]
        self.assertEqual(channel, self.server_log.id)
        self.assertIn(f"<#{self.messages.id}>", embed["description"])
        with mock.patch.object(logs_module.time, "monotonic", return_value=time.monotonic() + logs_module.REFUSED_SECONDS + 1):
            self.logs._channel_refused(self.messages.id)
        self.assertEqual(len(self.notices()), 2)

    async def test_the_channel_gets_another_try_after_the_hour(self):
        self.logs._channel_refused(self.messages.id)
        with mock.patch.object(logs_module.time, "monotonic", return_value=time.monotonic() + logs_module.REFUSED_SECONDS + 1):
            self.assertEqual(self.logs._channel_for(self.guild, "delete"), self.messages.id)

    async def test_with_nowhere_else_the_only_channel_stays(self):
        await self.configure(message_channel=None, server_channel=None)
        self.logs._channel_refused(self.log_channel.id)
        self.assertEqual(self.logs._channel_for(self.guild, "delete"), self.log_channel.id)

    async def test_a_deleted_log_channel_tells_staff(self):
        self.guild.channels.remove(self.messages)
        await self.logs.on_guild_channel_delete(self.messages)
        self.assertTrue(any(embed["title"] == "A log channel was deleted" and "message log channel" in embed["description"] for _, embed, _ in self.posted))
        self.assertEqual(self.logs._channel_for(self.guild, "delete"), self.log_channel.id)

    async def test_the_sink_says_when_discord_refuses(self):
        heard = []
        channel = FakeChannel(self.guild, "refusing")

        async def refuse(**kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        channel.send = refuse
        channel.webhooks = None
        sink = LogSink(SimpleNamespace(get_channel=lambda channel_id: channel, user=None, members_settled=None))
        sink.refused_listener = heard.append
        sink._webhook = mock.AsyncMock(return_value=None)
        with mock.patch.object(logsink, "FLUSH_SECONDS", 0):
            sink.post(channel.id, {"title": "x"})
            await asyncio.wait_for(sink._workers[channel.id], 5)
        self.assertEqual(heard, [channel.id])


class StatusAndTestEntryTest(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops.send_message = self.send_message
        self.sent_to = []

    async def send_message(self, channel_id, payload, **kwargs):
        if channel_id == getattr(self, "refusing", None):
            raise logs_module.OpsError("The bot can't post in that channel.")
        self.sent_to.append((channel_id, payload))
        return {"id": new_id(), "channel_id": channel_id, "url": ""}

    async def test_a_channel_thats_gone_or_closed_shows_on_the_page(self):
        class Closed(FakeChannel):
            def permissions_for(self, member):
                return FakePermissions("view_channel")
        closed = Closed(self.guild, "closed")
        gone = FakeChannel(self.guild, "gone")
        await self.configure(message_channel=closed.id, voice_channel=gone.id)
        self.guild.channels.remove(gone)
        client = await self.client()
        problems = (await (await client.get(self.path("/logging/status"))).json())["problems"]
        by_field = {row["field"]: row["text"] for row in problems}
        self.assertEqual(set(by_field), {"message_channel", "voice_channel"})
        self.assertIn("Send Messages", by_field["message_channel"])
        self.assertIn("no longer exists", by_field["voice_channel"])

    async def test_a_test_entry_goes_to_each_channel_once_and_reports_each(self):
        second = FakeChannel(self.guild, "second")
        third = FakeChannel(self.guild, "third")
        await self.configure(message_channel=second.id, member_channel=second.id, voice_channel=third.id)
        self.refusing = third.id
        client = await self.client()
        response = await client.post(self.path("/logging/test"))
        self.assertEqual(response.status, 200, await response.text())
        results = {row["channel_id"]: row for row in (await response.json())["results"]}
        self.assertEqual(set(results), {str(self.log_channel.id), str(second.id), str(third.id)})
        self.assertTrue(results[str(second.id)]["ok"])
        self.assertFalse(results[str(third.id)]["ok"])
        self.assertIn("can't post", results[str(third.id)]["error"])
        self.assertEqual(sorted(channel for channel, _ in self.sent_to), sorted([self.log_channel.id, second.id]))
        self.assertEqual(self.sent_to[0][1]["embeds"][0]["title"], "Test entry")

    async def test_staff_cant_send_one(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.post(self.path("/logging/test"))).status, 403)


class LogsSite(SiteCase):
    modules = [Logs]

    async def asyncSetUp(self):
        self.quick = mock.patch.multiple(logs_module, **QUICK)
        self.quick.start()
        await super().asyncSetUp()
        self.logs = self.cog(Logs)
        self.bot.logs.posted.clear()
        self.reader_role = self.guild.role("Reader", 3, "manage_guild", "view_channel", "read_message_history", "send_messages")
        self.reader = FakeMember(self.guild, "reader", self.reader_role)

    async def asyncTearDown(self):
        try:
            await self.logs.cog_unload()
        finally:
            await super().asyncTearDown()
            self.quick.stop()

    async def search(self, member):
        client = await self.site(member)
        response = await client.get(self.path("/logs/search"), params={"size": "25"})
        self.assertEqual(response.status, 200, await response.text())
        return [row["embed"]["title"] for row in (await response.json())["rows"]]


class ReadableRoom(FakeChannel):
    """Open to the roles listed, with Administrators always in."""

    def __init__(self, guild, name, *roles):
        super().__init__(guild, name)
        self.readers = {role.id for role in roles}

    def permissions_for(self, target):
        holds = [target] if isinstance(target, FakeRole) else target.roles
        if any("administrator" in role.permissions.names for role in holds):
            return FakePermissions("administrator")
        return FakePermissions("view_channel", "read_message_history") if any(role.id in self.readers for role in holds) else FakePermissions()


class DeletedChannelSearchTest(LogsSite):
    async def entries_from(self, channel):
        self.logs._store_entry(self.guild.id, "delete", {"title": f"Message in #{channel.name}", "description": "x"}, at=time.time(), channel_id=channel.id)
        await self.logs.flush()

    async def test_staff_who_could_read_it_keep_its_entries(self):
        await self.bot.settings.save(self.guild.id, "logging", {"events": [*logs_module.DEFAULT_EVENTS, "channeldelete"]})
        room = ReadableRoom(self.guild, "old-general", self.reader_role)
        await self.entries_from(room)
        self.assertEqual(await self.search(self.reader), ["Message in #old-general"])
        self.guild.channels.remove(room)
        await self.logs.on_guild_channel_delete(room)
        titles = await self.search(self.reader)
        self.assertIn("Message in #old-general", titles)
        self.assertIn("Text channel deleted", titles)

    async def test_someone_who_couldnt_doesnt_gain_them(self):
        room = ReadableRoom(self.guild, "mod-chat", self.senior)
        await self.entries_from(room)
        self.guild.channels.remove(room)
        await self.logs.on_guild_channel_delete(room)
        self.assertEqual(await self.search(self.reader), [])
        self.assertIn("Message in #mod-chat", await self.search(self.admin))

    async def test_threads_of_a_deleted_channel_stay_with_it(self):
        room = ReadableRoom(self.guild, "old-forum", self.reader_role)
        self.logs._store_entry(self.guild.id, "threadcreate", {"title": "A thread", "description": "x"}, at=time.time(), channel_id=555, parent_id=room.id)
        await self.logs.flush()
        self.guild.channels.remove(room)
        await self.logs.on_guild_channel_delete(room)
        self.assertIn("A thread", await self.search(self.reader))

    async def test_a_single_entry_and_an_export_follow_the_same_rule(self):
        room = ReadableRoom(self.guild, "old-general", self.reader_role)
        hidden = ReadableRoom(self.guild, "mod-chat", self.senior)
        await self.entries_from(room)
        await self.entries_from(hidden)
        for channel in (room, hidden):
            self.guild.channels.remove(channel)
            await self.logs.on_guild_channel_delete(channel)
        client = await self.site(self.reader)
        text = await (await client.get(self.path("/logs/export"), params={"format": "json"})).text()
        self.assertIn("old-general", text)
        self.assertNotIn("mod-chat", text)

    async def test_the_record_ends_with_the_entries(self):
        room = ReadableRoom(self.guild, "old-general", self.reader_role)
        await self.logs.on_guild_channel_delete(room)
        self.assertGreater(await self.message_db.scalar("SELECT COUNT(*) FROM deleted_channel_readers"), 0)
        await self.logs.prune(time.time() + 91 * 86400)
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM deleted_channel_readers"), 0)

    async def test_seeing_without_history_isnt_reading(self):
        class ViewOnly(FakeChannel):
            def permissions_for(self, target):
                return FakePermissions("view_channel")
        room = ViewOnly(self.guild, "lobby")
        await self.entries_from(room)
        self.assertEqual(await self.search(self.reader), [])

    async def test_an_archived_threads_parent_comes_from_its_earlier_entry(self):
        self.logs._store_entry(self.guild.id, "threadcreate", {"title": "Thread made", "description": "x"}, at=time.time(), channel_id=777, parent_id=self.channel.id)
        await self.logs.flush()
        message = FakeMessage(self.channel, self.member, "in the thread")
        message.channel = SimpleNamespace(id=777, name="thread", guild=self.guild)
        await self.logs.on_message(message)
        await self.logs.on_raw_message_delete(SimpleNamespace(message_id=message.id, channel_id=777, guild_id=self.guild.id, cached_message=None))
        await self.logs.flush()
        row = await self.message_db.one("SELECT parent_id FROM log_entries WHERE event = 'delete'")
        self.assertEqual(row["parent_id"], self.channel.id)

    async def test_a_private_threads_entries_stay_closed(self):
        message_id = new_id()
        await self.logs.on_raw_message_delete(SimpleNamespace(message_id=message_id, channel_id=888, guild_id=self.guild.id, cached_message=None))
        await self.logs.flush()
        row = await self.message_db.one("SELECT parent_id FROM log_entries WHERE event = 'delete'")
        self.assertIsNone(row["parent_id"])


class SearchLeavesTheStoreAloneTest(Base):
    async def test_a_search_and_an_export_read_their_own_copy(self):
        await self.send("hi there")
        message = await self.send("hi")
        await self.delete(message.id)
        client = await self.client()
        await self.logs.flush()
        with mock.patch.object(self.message_db, "all", side_effect=AssertionError("read on the store's own connection")), mock.patch.object(self.message_db, "scalar", side_effect=AssertionError("read on the store's own connection")):
            response = await client.get(self.path("/logs/search"), params={"q": "hi"})
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await response.json())["total"], 1)
            response = await client.get(self.path("/logs/export"), params={"format": "csv"})
            self.assertEqual(response.status, 200)
            self.assertIn("hi", await response.text())


class SettingsReachTest(LogsSite):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.secret = HiddenChannel(self.guild, "owners-only", {self.admin.id, self.me.id})
        self.bot.ops = mock.Mock(wraps=self.bot.ops)
        self.bot.ops.describe_guild = lambda guild_id: {"channels": [{"id": c.id} for c in self.guild.channels], "roles": []}

    async def test_an_import_cant_name_a_channel_they_cant_see(self):
        client = await self.site(self.reader)
        response = await client.post(self.path("/logging/import"), json={"code": export_code({"message_channel": self.secret.id})})
        self.assertEqual(response.status, 400, await response.text())
        self.assertIn("can't see", (await response.json())["error"])
        self.assertIsNone(self.settings("logging")["message_channel"])

    async def test_an_import_cant_name_a_channel_they_cant_post_in(self):
        client = await self.site(self.manager)
        response = await client.post(self.path("/logging/import"), json={"code": export_code({"message_channel": self.channel.id})})
        self.assertEqual(response.status, 400, await response.text())
        self.assertIn("can't post", (await response.json())["error"])

    async def test_an_import_of_what_they_can_reach_goes_through(self):
        client = await self.site(self.reader)
        response = await client.post(self.path("/logging/import"), json={"code": export_code({"join_channel": self.channel.id})})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.settings("logging")["join_channel"], self.channel.id)
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "log.import")

    async def test_an_import_that_leaves_a_hidden_channel_where_it_was_goes_through(self):
        await self.bot.settings.save(self.guild.id, "logging", {"message_channel": self.secret.id})
        client = await self.site(self.reader)
        response = await client.post(self.path("/logging/import"), json={"code": export_code({"message_channel": self.secret.id, "events": ["edit"]})})
        self.assertEqual(response.status, 200, await response.text())

    async def test_the_settings_form_refuses_too(self):
        client = await self.site(self.manager)
        response = await client.put(self.path("/settings/logging"), json={"moderator_delete_channel": str(self.channel.id)})
        self.assertEqual(response.status, 400)
        self.assertIn("can't post", (await response.json())["error"])

    async def test_the_commands_answer_the_same(self):
        ctx = self.context(self.reader)
        await self.logs.log_channel.callback(self.logs, ctx, self.secret)
        self.assertEqual(ctx.fails, ["You can't see that channel."])
        ctx = self.context(self.manager)
        await self.logs.log_message_channel.callback(self.logs, ctx, self.channel)
        self.assertEqual(ctx.fails, ["You can't post in that channel."])
        ctx = self.context(self.reader)
        await self.logs.log_import.callback(self.logs, ctx, code=export_code({"message_channel": self.secret.id}))
        self.assertEqual(ctx.fails, ["You can't see that channel."])
        self.assertIsNone(self.settings("logging")["default_channel"])
        self.assertIsNone(self.settings("logging")["message_channel"])
        ctx = self.context(self.reader)
        await self.logs.log_channel.callback(self.logs, ctx, None)
        self.assertEqual(ctx.fails, [])

    async def test_ignoring_a_channel_they_cant_see_is_refused(self):
        ctx = self.context(self.reader)
        await self.logs.log_ignore.callback(self.logs, ctx, targets=f"<#{self.secret.id}>")
        self.assertEqual(ctx.fails, ["You can't see that channel."])
        self.assertEqual(self.settings("logging")["ignored_channels"], [])

    async def test_making_log_channels_takes_manage_channels(self):
        client = await self.site(self.reader)
        self.assertEqual((await client.post(self.path("/logging/aio"))).status, 403)
        ctx = self.context(self.reader)
        await self.logs.log_aio.callback(self.logs, ctx)
        self.assertIn("Manage Channels", ctx.fails[0])


class MentionNothingTest(Base):
    async def test_a_test_entry_pings_nobody(self):
        seen = []

        async def send_message(channel_id, payload, **kwargs):
            seen.append(kwargs)
            return {"id": new_id(), "channel_id": channel_id, "url": ""}
        self.bot.ops.send_message = send_message
        client = await self.client()
        await client.post(self.path("/logging/test"))
        self.assertEqual(seen, [{"silent": True}])


class WhatThePagesSayTest(Base):
    """The text that tells members and staff how long message text stays."""

    def test_the_privacy_page_says_log_entries_hold_message_text(self):
        page = (Path(logs_module.__file__).parent.parent / "web" / "static" / "privacy.html").read_text(encoding="utf-8")
        row = next(line for line in page.splitlines() if "<td>Log entries:" in line)
        self.assertIn("full text of deleted, edited, purged and pinned messages", row)
        self.assertIn("stored messages and log entries stay until their retention ends", page)

    def test_the_storage_card_says_so_too(self):
        page = (Path(logs_module.__file__).parent.parent / "web" / "static" / "js" / "pages" / "logging.js").read_text(encoding="utf-8")
        self.assertIn("holds that message's text, and stays for as long as entries are kept", page)
