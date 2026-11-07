"""The first things a person meets: a bare mention, the note to whoever added the bot, and the short pause a filter hit can bring."""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any

import discord

from tests.fakes import FakeGuild
from tests.gateway import FakeHTTPResponse, GatewayTest, iso, new_id
from tests.test_automod import AutomodBase
from tests.test_moderation_live import LiveBase


class BareMentionTest(GatewayTest):
    modules: list = []

    def answers(self) -> list[str]:
        return [body.get("content") or "" for method, path, body in self.net.calls if method == "POST" and path == "/channels/{channel_id}/messages"]

    async def test_a_bare_mention_points_at_help(self):
        self.dispatch_message(user_id=self.member_id, content=f"<@{self.bot_id}>")
        await self.settle(0.05)
        self.assertEqual(len(self.answers()), 1)
        self.assertIn("/help", self.answers()[0])
        self.assert_no_errors()

    async def test_the_nickname_form_counts_too(self):
        self.dispatch_message(user_id=self.member_id, content=f"<@!{self.bot_id}>")
        await self.settle(0.05)
        self.assertEqual(len(self.answers()), 1)

    async def test_a_channel_full_of_mentions_gets_one_answer(self):
        for _ in range(3):
            self.dispatch_message(user_id=self.member_id, content=f"<@{self.bot_id}>")
        await self.settle(0.05)
        self.assertEqual(len(self.answers()), 1)

    async def test_a_mention_inside_a_sentence_is_no_call_for_help(self):
        self.dispatch_message(user_id=self.member_id, content=f"I think <@{self.bot_id}> is down")
        await self.settle(0.05)
        self.assertEqual(self.answers(), [])

    async def test_other_people_mentioned_get_nothing(self):
        self.dispatch_message(user_id=self.member_id, content=f"<@{self.admin_id}>")
        await self.settle(0.05)
        self.assertEqual(self.answers(), [])


class AddedByTest(GatewayTest):
    modules: list = []

    def guild_with(self, *entries: Any) -> FakeGuild:
        guild = FakeGuild("Fresh Server")
        guild.audit_entries = list(entries)
        return guild

    def entry(self, target_id: int, person: Any) -> Any:
        return SimpleNamespace(action=discord.AuditLogAction.bot_add, target=SimpleNamespace(id=target_id), user=person)

    def person(self) -> Any:
        sent: list[str] = []

        async def send(text: str, **kwargs: Any) -> None:
            sent.append(text)
        return SimpleNamespace(id=new_id(), send=send, sent=sent)

    async def test_whoever_added_the_bot_hears_where_to_start(self):
        who = self.person()
        await self.bot.on_guild_join(self.guild_with(self.entry(self.bot_id, who)))
        self.assertEqual(len(who.sent), 1)
        self.assertIn("/dashboard", who.sent[0])
        self.assertIn("Fresh Server", who.sent[0])

    async def test_another_bots_entry_isnt_ours(self):
        who = self.person()
        await self.bot.on_guild_join(self.guild_with(self.entry(new_id(), who)))
        self.assertEqual(who.sent, [])

    async def test_no_audit_log_access_changes_nothing(self):
        guild = self.guild_with()

        async def refused(**kwargs: Any) -> Any:
            raise discord.Forbidden(FakeHTTPResponse(403), {"code": 50013, "message": "no"})
            yield
        guild.audit_logs = refused
        await self.bot.on_guild_join(guild)
        self.assert_no_errors()

    async def test_closed_dms_change_nothing(self):
        who = self.person()

        async def closed(text: str, **kwargs: Any) -> None:
            raise discord.Forbidden(FakeHTTPResponse(403), {"code": 50007, "message": "no"})
        who.send = closed
        await self.bot.on_guild_join(self.guild_with(self.entry(self.bot_id, who)))
        self.assert_no_errors()


class FilterPauseTest(AutomodBase):
    def timeouts(self) -> list[tuple]:
        return [call for call in self.member.calls if call[0] == "timeout"]

    async def test_a_hit_pauses_the_sender_with_no_case(self):
        await self.rule("words", entries=["boat"], pause_seconds=5)
        await self.say("boat")
        self.assertEqual(len(self.timeouts()), 1)
        self.assertEqual(await self.cases(), [])

    async def test_every_hit_pauses_even_when_the_punishment_doesnt_repeat(self):
        await self.rule("words", entries=["boat"], pause_seconds=5)
        await self.say("boat")
        self.member.timed_out_until = None
        await self.say("boat again")
        self.assertEqual(len(self.timeouts()), 2)

    async def test_off_by_default(self):
        await self.rule("words", entries=["boat"])
        await self.say("boat")
        self.assertEqual(self.timeouts(), [])

    async def test_a_longer_timeout_isnt_cut_short(self):
        await self.rule("words", entries=["boat"], pause_seconds=5)
        self.member.timed_out_until = discord.utils.utcnow() + datetime.timedelta(hours=1)
        await self.say("boat")
        self.assertEqual(self.timeouts(), [])

    async def test_dry_run_only_logs(self):
        await self.rule("words", entries=["boat"], pause_seconds=5, dry_run=True)
        await self.say("boat")
        self.assertEqual(self.timeouts(), [])

    async def test_a_kick_needs_no_pause(self):
        from captainguild.core.validate import DELETE, KICK
        await self.rule("words", entries=["boat"], pause_seconds=5, punishment={"actions": DELETE | KICK, "seconds": 0, "purge_days": 0})
        await self.say("boat")
        self.assertEqual(self.timeouts(), [])

    async def test_a_length_past_a_real_timeout_is_refused(self):
        client = await self.client()
        response = await client.put(self.path("/settings/automod"), json={"words": {"pause_seconds": 60}})
        self.assertEqual(response.status, 400)


class FilterPauseLiveTest(LiveBase):
    async def test_the_member_update_that_follows_makes_no_case(self):
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["boat"], "pause_seconds": 5}})
        self.dispatch_message(user_id=self.member_id, content="boat")
        await self.settle()
        until = iso(discord.utils.utcnow() + datetime.timedelta(seconds=5))
        self.bot._connection.parse_guild_member_update({**self.member_json[self.member_id], "guild_id": str(self.guild_id), "communication_disabled_until": until})
        await self.settle()
        self.assertEqual(await self.db.all("SELECT * FROM cases WHERE guild_id = ?", (self.guild_id,)), [])
        self.assert_no_errors()
