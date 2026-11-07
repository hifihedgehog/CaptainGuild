"""The voice commands people know by name: real top-level commands with the parameters people already know, with every typed spelling still working."""
from __future__ import annotations

import time

from discord import app_commands

from captainguild.core.perms import MODULE as COMMANDS_MODULE
from captainguild.core.shortcuts import SHORTCUTS
from captainguild.modules.voice import BLOCK, DEAFEN, MUTE, read_length, split_length
from tests.test_voice import VoiceTest

OLD_NAMES = {
    "vc_report": ["user", "reason"],
    "vc_mute": ["user", "minutes", "reason"],
    "vc_deafen": ["user", "minutes", "reason"],
    "vc_disconnect": ["user", "block_minutes", "reason"],
    "vc_release": ["user"],
    "vc_access_grant": ["user"],
    "vc_access_revoke": ["user", "reason"],
    "vc_access_status": ["user"],
    "mini_mute": ["user", "reason"],
    "mini_mute_lift": ["user", "reason"],
}


class OldNamesTest(VoiceTest):
    async def test_each_old_name_is_top_level_with_its_old_parameters(self):
        tree = {command.name: command for command in self.bot.tree.get_commands()}
        for name, parameters in OLD_NAMES.items():
            with self.subTest(name=name):
                self.assertIsInstance(tree[name], app_commands.Command)
                self.assertEqual([parameter.name for parameter in tree[name].parameters], parameters)

    async def test_the_vc_group_keeps_only_what_has_no_old_name(self):
        group = next(command for command in self.bot.tree.get_commands() if command.name == "vc")
        self.assertEqual(sorted(child.name for child in group.commands), ["button", "progress"])

    async def test_slash_mute_takes_minutes_as_a_number_or_a_length(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id), minutes="15", reason="noisy")
        row = await self.db.one("SELECT * FROM voice_actions WHERE action = ?", (MUTE,))
        self.assertAlmostEqual(row["expires_at"], time.time() + 900, delta=5)
        await self.slash("vc_deafen", self.staff_id, user=self.member(self.member_id), minutes="2h")
        row = await self.db.one("SELECT * FROM voice_actions WHERE action = ?", (DEAFEN,))
        self.assertAlmostEqual(row["expires_at"], time.time() + 7200, delta=5)
        self.assert_no_errors()

    async def test_slash_disconnect_blocks_for_block_minutes(self):
        await self.slash("vc_disconnect", self.staff_id, user=self.member(self.member_id), block_minutes="30")
        row = await self.db.one("SELECT * FROM voice_actions WHERE action = ?", (BLOCK,))
        self.assertAlmostEqual(row["expires_at"], time.time() + 1800, delta=5)
        self.assert_no_errors()


class TypedSpellingsTest(VoiceTest):
    async def typed(self, content: str, user_id: int | None = None) -> None:
        self.dispatch_message(user_id=user_id or self.staff_id, content=content)
        await self.settle()
        self.assert_no_errors(content)

    async def test_both_spellings_of_a_mute(self):
        await self.join(self.member_id)
        await self.typed(f"!vc_mute <@{self.member_id}> 15 noisy here")
        row = await self.db.one("SELECT * FROM voice_actions WHERE action = ?", (MUTE,))
        self.assertAlmostEqual(row["expires_at"], time.time() + 900, delta=5)
        await self.db.run("DELETE FROM voice_actions")
        await self.typed(f"!vc mute <@{self.member_id}> 20 still noisy")
        row = await self.db.one("SELECT * FROM voice_actions WHERE action = ?", (MUTE,))
        self.assertAlmostEqual(row["expires_at"], time.time() + 1200, delta=5)
        self.assertEqual(row["reason"], "still noisy")

    async def test_the_words_after_a_spelling_stay_as_typed(self):
        await self.join(self.member_id)
        await self.typed(f"!VC  Mute <@{self.member_id}> 5 two  spaces")
        self.assertEqual(await self.db.scalar("SELECT reason FROM voice_actions"), "two  spaces")

    async def test_a_three_word_spelling(self):
        await self.typed(f"!vc access grant <@{self.member_id}>")
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access WHERE user_id = ?", (self.member_id,)), "approved")

    async def test_both_spellings_of_a_lift(self):
        for typed in ("!mini_mute_lift", "!mini_mute lift"):
            await self.typed(f"!mini_mute <@{self.member_id}> first")
            self.assertIsNotNone(await self.db.one("SELECT * FROM mini_mutes WHERE user_id = ?", (self.member_id,)), typed)
            await self.typed(f"{typed} <@{self.member_id}>")
            self.assertIsNone(await self.db.one("SELECT * FROM mini_mutes WHERE user_id = ?", (self.member_id,)), typed)

    async def test_a_typed_report_by_the_old_group_name(self):
        await self.typed(f"!vc report <@{self.member_id}> shouting", user_id=self.vet_id)
        self.assertEqual(await self.db.scalar("SELECT reason FROM voice_reports"), "shouting")

    async def test_the_server_rule_for_the_real_command_covers_the_other_spelling(self):
        await self.join(self.member_id)
        await self.bot.settings.save(self.guild_id, COMMANDS_MODULE, {"overwrites": {"vc_mute": {"enabled": False}}})
        await self.typed(f"!vc mute <@{self.member_id}> 5 noisy")
        self.assertIsNone(await self.db.one("SELECT * FROM voice_actions"))

    async def test_every_long_spelling_names_a_command_here(self):
        for name, target in SHORTCUTS.items():
            if name.split()[0] in ("vc", "mini_mute"):
                with self.subTest(name=name):
                    self.assertIsNotNone(self.bot.get_command(target.split()[0]))


class LengthInputTest(VoiceTest):
    async def test_a_superscript_digit_isnt_a_length(self):
        self.assertIsNone(read_length("²"))
        self.assertEqual(split_length("²", "reason"), (None, "² reason"))

    async def test_the_command_answers_with_a_sentence(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id), minutes="²")
        self.assertIn("isn't a length", self.last_answer())
        self.assert_no_errors()


class DashboardFilterTest(VoiceTest):
    async def test_odd_filters_answer_and_dont_fail(self):
        client = await self.client()
        await self.db.run("INSERT INTO voice_access (guild_id, user_id, user_name, status) VALUES (?, 5, '² named', 'pending')", (self.guild_id,))
        for path, total in (("/voice/access?member=99999999999999999999", 0), ("/voice/access?member=%C2%B2", 1), ("/voice/access?member=5", 1), ("/voice/access?page=99999999999999999999", 1), ("/voice/reports?page=99999999999999999999", 0)):
            with self.subTest(path=path):
                reply = await client.get(self.path(path))
                self.assertEqual(reply.status, 200)
                self.assertEqual((await reply.json())["total"], total)


class PeerTest(VoiceTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.peer_id = self.add_member("peer", self.staff_role["id"])
        self.higher = self.add_role("Higher", 8)
        self.boss_id = self.add_member("boss", self.higher["id"])

    async def test_a_moderator_can_act_on_a_fellow_moderator(self):
        await self.join(self.peer_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.peer_id))
        self.assertEqual(await self.actions(self.peer_id), {MUTE})
        self.assertIn("Server-muted", self.last_answer())

    async def test_someone_ranked_above_stays_out_of_reach(self):
        await self.join(self.boss_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.boss_id))
        self.assertEqual(await self.actions(self.boss_id), set())
        self.assertIn("above yours", self.last_answer())


class AnswerFirstTest(VoiceTest):
    """A role route waiting on a rate limit can pass Discord's three seconds, so the first answer goes out before anything waits on Discord."""

    def answered_before(self, fragment: str) -> bool:
        callback = next(index for index, call in enumerate(self.net.calls) if call[1].endswith("/callback"))
        change = next(index for index, call in enumerate(self.net.calls) if fragment in call[1] and call[0] != "GET")
        return callback < change

    async def test_a_voice_command_answers_before_it_edits_the_member(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        self.assertTrue(self.answered_before("/members/{user_id}"))

    async def test_a_grant_answers_before_it_adds_the_role(self):
        await self.slash("vc_access_grant", self.staff_id, user=self.member(self.member_id))
        self.assertTrue(self.answered_before("/roles/{role_id}"))

    async def test_a_release_answers_before_it_edits_the_member(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        self.net.calls.clear()
        await self.slash("vc_release", self.staff_id, user=self.member(self.member_id))
        self.assertTrue(self.answered_before("/members/{user_id}"))

    async def test_a_lift_answers_before_it_takes_the_role(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.member_json[self.member_id]["roles"] = [self.mute_role["id"]]  # the faked network sends no member update
        self.net.calls.clear()
        await self.slash("mini_mute_lift", self.staff_id, user=self.member(self.member_id))
        self.assertTrue(self.answered_before("/roles/{role_id}"))

    async def test_why_it_was_refused_stays_private(self):
        await self.slash("vc_mute", self.vet_id, user=self.member(self.staff_id))
        callback = next(call for call in self.net.calls if call[1].endswith("/callback"))
        self.assertTrue(self.is_private(callback[2]["data"]))
