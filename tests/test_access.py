"""The one answer commands and dashboard routes share for what a person may do in a channel."""
from __future__ import annotations

import unittest

import discord

from captainguild.core import access
from captainguild.core.ops import DiscordOps
from captainguild.core.validate import ValidationError, whole_number
from captainguild.web.api import Req, User
from tests.fakes import BotTest, FakeChannel, FakeMember, FakePermissions


class Limited(FakeChannel):
    """A channel with permissions of its own for one person."""

    def __init__(self, guild, name, member, *names, kind=discord.ChannelType.text):
        super().__init__(guild, name, kind)
        self.member = member
        self.names = names

    def permissions_for(self, member):
        return FakePermissions(*self.names) if member is self.member else member.guild_permissions


class ChannelChecks(BotTest):
    def test_a_person_who_can_only_read_cant_post_or_ping(self):
        channel = Limited(self.guild, "news", self.member, "view_channel", "read_message_history")
        self.assertTrue(access.can_view(self.member, channel))
        self.assertTrue(access.can_read(self.member, channel))
        self.assertFalse(access.can_post(self.member, channel))
        self.assertFalse(access.can_ping(self.member, channel))
        self.assertEqual(access.post_refusal(self.member, channel), "You can't post in that channel.")
        self.assertIn("Mention Everyone", access.ping_refusal(self.member, channel))

    def test_a_hidden_channel_refuses_everything_with_the_same_sentence(self):
        channel = Limited(self.guild, "secret", self.member)
        for refusal in (access.view_refusal, access.read_refusal, access.post_refusal, access.ping_refusal):
            self.assertEqual(refusal(self.member, channel), "You can't see that channel.")
        self.assertFalse(access.can_manage_messages(self.member, channel))

    def test_seeing_a_channel_isnt_reading_its_history(self):
        channel = Limited(self.guild, "lobby", self.member, "view_channel")
        self.assertTrue(access.can_view(self.member, channel))
        self.assertFalse(access.can_read(self.member, channel))
        self.assertEqual(access.read_refusal(self.member, channel), "You can't read the messages in that channel.")

    def test_full_rights_pass(self):
        channel = Limited(self.guild, "news", self.member, "view_channel", "send_messages", "mention_everyone", "manage_messages", "read_message_history")
        self.assertTrue(access.can_post(self.member, channel))
        self.assertTrue(access.can_ping(self.member, channel))
        self.assertTrue(access.can_manage_messages(self.member, channel))
        self.assertIsNone(access.post_refusal(self.member, channel))

    def test_owner_and_administrators_pass_every_check(self):
        channel = Limited(self.guild, "secret", self.member)
        for person in (self.owner, self.admin):
            self.assertTrue(access.can_post(person, channel))
            self.assertTrue(access.can_ping(person, channel))
            self.assertTrue(access.can_read(person, channel))

    def test_a_private_thread_takes_manage_threads(self):
        thread = Limited(self.guild, "t", self.member, "view_channel", kind=discord.ChannelType.private_thread)
        self.assertFalse(access.can_view(self.member, thread))
        thread.names = ("view_channel", "manage_threads")
        self.assertTrue(access.can_view(self.member, thread))

    def test_a_thread_whose_parent_isnt_cached_reads_as_no(self):
        class Lost(FakeChannel):
            def permissions_for(self, member):
                raise discord.ClientException("Parent channel not found")
        self.assertFalse(access.can_view(self.member, Lost(self.guild, "t")))
        self.assertFalse(access.can_post(self.member, Lost(self.guild, "t")))


class RouteChecks(BotTest):
    def req(self, member):
        self.bot.ops = DiscordOps(self.bot)
        return Req(None, self.bot, User(member.id, member.name, None), self.guild.id, "full")

    def test_posting_needs_send_messages_where_viewing_doesnt(self):
        channel = Limited(self.guild, "news", self.member, "view_channel")
        req = self.req(self.member)
        self.assertEqual(req.channel(channel.id), channel.id)
        with self.assertRaises(ValidationError) as caught:
            req.channel(channel.id, posting=True)
        self.assertEqual(caught.exception.fields, {"channel_id": "You can't post in that channel."})

    def test_a_ping_needs_mention_everyone(self):
        channel = Limited(self.guild, "news", self.member, "view_channel", "send_messages")
        req = self.req(self.member)
        self.assertEqual(req.channel(channel.id, posting=True), channel.id)
        with self.assertRaises(ValidationError):
            req.require_ping(channel.id)
        channel.names = ("view_channel", "send_messages", "mention_everyone")
        req.require_ping(channel.id)
        self.assertTrue(self.req(self.admin).can_ping(channel.id))

    def test_a_channel_thats_gone_hides_nothing_and_a_person_who_left_may_do_nothing(self):
        channel = Limited(self.guild, "news", self.member, "view_channel", "send_messages")
        gone = FakeMember(self.guild, "gone")
        req = self.req(gone)
        self.guild.members.remove(gone)
        self.assertFalse(req.can_post(channel.id))
        self.assertTrue(self.req(self.member).can_see(123))


class Numbers(unittest.TestCase):
    def test_plain_digits_and_ints_read_as_numbers(self):
        self.assertEqual(whole_number("42"), 42)
        self.assertEqual(whole_number(" 7 "), 7)
        self.assertEqual(whole_number(42), 42)
        self.assertEqual(whole_number("1234567890123456789"), 1234567890123456789)

    def test_everything_else_reads_as_none(self):
        for value in ("\u00b2", "\u0661\u0662", "-1", "1.5", "", "abc", "1" * 20, 2 ** 63, -1, True, None, 1.5, [], "9" * 19):
            self.assertIsNone(whole_number(value), repr(value))

    def test_a_maximum_applies(self):
        self.assertEqual(whole_number("10", maximum=10), 10)
        self.assertIsNone(whole_number("11", maximum=10))


class Transient(BotTest):
    async def test_every_feature_can_name_roles_that_dont_come_back(self):
        self.assertEqual(self.bot.transient_roles(self.guild.id), set())

        class Holder:
            def transient_roles(self, guild_id):
                return {guild_id + 1}
        self.bot.modules.append(Holder())
        self.assertEqual(self.bot.transient_roles(self.guild.id), {self.guild.id + 1})
        self.bot.modules.pop()


if __name__ == "__main__":
    unittest.main()
