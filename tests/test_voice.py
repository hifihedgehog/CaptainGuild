"""Voice moderation, voice access and mini mutes, driven through the real library with the network faked."""
from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any
from unittest import mock

import discord

from captainguild.core.perms import MODULE as COMMANDS_MODULE
from captainguild.modules.moderation import Moderation
from captainguild.modules.reports import Reports
from captainguild.modules.voice import (
    ACCESS_APPROVE_ID, ACCESS_DENY_ID, ACCESS_REQUEST_ID, BLOCK, DEAFEN, MUTE, REPORT_DEAFEN_ID, REPORT_DISMISS_ID, REPORT_MUTE_ID, REPORT_REMOVE_ID, VOTE_AGAINST_ID,
    VOTE_FOR_ID, VOTE_VETO_ID, Voice, format_minutes, read_length, split_length,
)
from tests.fakes import FakeLogSink
from tests.gateway import new_id
from tests.voice_report_harness import Harness

MEMBER_CALLS = "/guilds/{guild_id}/members/{user_id}"
LONG_AGO = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


class VoiceTest(Harness):
    modules = [Voice]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.logs = FakeLogSink()
        self.voice_module = self.bot.module("voice")
        self.staff_role = self.add_role("Staff", 3)
        self.vet_role = self.add_role("VC Veteran", 2)
        self.server_vet_role = self.add_role("Server Veteran", 2)
        self.access_role = self.add_role("Voice Access", 1)
        self.mute_role = self.add_role("Mini Muted", 1)
        self.staff_id = self.add_member("staffer", self.staff_role["id"])
        self.vet_id = self.add_member("vet", self.vet_role["id"])
        self.server_vet_id = self.add_member("oldtimer", self.server_vet_role["id"])
        self.server_vet_two = self.add_member("oldtimer2", self.server_vet_role["id"])
        self.server_vet_three = self.add_member("oldtimer3", self.server_vet_role["id"])
        self.approvals = self.add_channel("approvals")
        self.reports_channel = self.add_channel("vc-reports")
        self.mini_log = self.add_channel("mini-log")
        await self.configure(
            moderator_role=int(self.staff_role["id"]), veteran_role=int(self.vet_role["id"]), server_veteran_role=int(self.server_vet_role["id"]),
            access_role=int(self.access_role["id"]), mini_mute_role=int(self.mute_role["id"]), report_channel=int(self.reports_channel["id"]),
            approval_channel=int(self.approvals["id"]), mini_mute_log_channel=int(self.mini_log["id"]),
        )

    def add_channel(self, name: str) -> dict:
        channel = {"id": str(new_id()), "guild_id": str(self.guild_id), "name": name, "type": 0, "position": 9, "permission_overwrites": [], "parent_id": None, "nsfw": False, "topic": None, "rate_limit_per_user": 0, "last_message_id": None, "flags": 0}
        self.net.channels[int(channel["id"])] = channel
        self.state.parse_channel_create(channel)
        return channel

    async def configure(self, **form: Any) -> None:
        await self.bot.settings.save(self.guild_id, "voice", form)

    def member(self, user_id: int) -> discord.Member:
        return self.guild.get_member(user_id)

    def voice_state(self, user_id: int, channel_id: str | None, *, mute: bool = False, deaf: bool = False) -> None:
        self.state.parse_voice_state_update({
            "guild_id": str(self.guild_id), "channel_id": channel_id, "user_id": str(user_id), "member": self.member_json[user_id], "session_id": "s", "deaf": deaf, "mute": mute,
            "self_deaf": False, "self_mute": False, "self_video": False, "suppress": False, "request_to_speak_timestamp": None,
        })

    async def join(self, user_id: int, *, mute: bool = False, deaf: bool = False) -> None:
        self.voice_state(user_id, self.voice["id"], mute=mute, deaf=deaf)
        await self.settle()

    async def leave(self, user_id: int) -> None:
        self.voice_state(user_id, None)
        await self.settle()

    def edits_of(self, user_id: int) -> list[dict]:
        # The harness keeps the template path, so a member's calls are told apart by marking them as they go through.
        return [body for body, who in self.member_edits if who == user_id]

    member_edits: list

    def watch_member_edits(self) -> None:
        self.member_edits = []
        real = self.bot.http.request

        async def request(route: Any, **kwargs: Any) -> Any:
            if route.method == "PATCH" and route.path == MEMBER_CALLS:
                who = int(route.url.rstrip("/").split("/")[-1])
                self.member_edits.append((kwargs.get("json") or {}, who))
            return await real(route, **kwargs)
        self.bot.http.request = request

    async def age_everything(self) -> None:
        """Make everything stored for later due now, as if the time had passed."""
        past = time.time() - 1
        await self.db.run("UPDATE voice_actions SET expires_at = ?", (past,))
        await self.db.run("UPDATE mini_mutes SET expires_at = ?", (past,))
        await self.db.run("UPDATE mini_mute_votes SET expires_at = ?", (past,))
        await self.db.run("UPDATE timers SET due_at = ?", (past,))

    async def actions(self, user_id: int) -> set[str]:
        rows = await self.db.all("SELECT action FROM voice_actions WHERE guild_id = ? AND user_id = ?", (self.guild_id, user_id))
        return {row["action"] for row in rows}

    def logged(self, channel: dict) -> list[dict]:
        return [embed for channel_id, embed, _ in self.bot.logs.posted if channel_id == int(channel["id"])]

    def role_calls(self, method: str) -> list[tuple]:
        return self.calls_to(method, "/guilds/{guild_id}/members/{user_id}/roles/{role_id}")


class LengthTest(VoiceTest):
    def test_read_length_takes_words_or_bare_minutes(self):
        self.assertEqual(read_length("15"), 15)
        self.assertEqual(read_length("2h"), 120)
        self.assertEqual(read_length("90s"), 2)
        self.assertIsNone(read_length("soon"))

    def test_split_length_pulls_a_length_off_the_front_of_a_text_command(self):
        self.assertEqual(split_length("20m", "spamming"), (20, "spamming"))
        self.assertEqual(split_length("2", "days of noise"), (2880, "of noise"))
        self.assertEqual(split_length("15", "noisy"), (15, "noisy"))
        self.assertEqual(split_length("spamming", "a lot"), (None, "spamming a lot"))
        self.assertEqual(split_length(None, None), (None, None))

    def test_format_minutes(self):
        self.assertEqual(format_minutes(1), "1 minute")
        self.assertEqual(format_minutes(90), "90 minutes")
        self.assertEqual(format_minutes(120), "2 hours")
        self.assertEqual(format_minutes(1440), "1 day")


class TempActionTest(VoiceTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.watch_member_edits()

    async def test_moderator_mutes_for_the_default_length_and_a_timer_is_set(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        self.assertEqual(self.edits_of(self.member_id)[-1]["mute"], True)
        row = await self.db.one("SELECT * FROM voice_actions WHERE user_id = ?", (self.member_id,))
        self.assertEqual((row["action"], row["moderator_id"]), (MUTE, self.staff_id))
        self.assertAlmostEqual(row["expires_at"], time.time() + 600, delta=5)
        self.assertIsNotNone(await self.bot.timers.get_key("voice_action", f"{self.guild_id}:{self.member_id}:mute"))
        self.assertIn("Server-muted", self.last_answer())
        self.assertFalse(self.is_private([a for a in self.answers() if a.get("content")][-1]), "staff in the channel see what was done")
        self.assert_no_errors()

    async def test_why_it_didnt_happen_stays_private(self):
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        answer = [a for a in self.answers() if a.get("content")][-1]
        self.assertIn("isn't connected", answer["content"])
        self.assertTrue(self.is_private(answer))

    async def test_release_answers_in_public(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        await self.slash("vc_release", self.staff_id, user=self.member(self.member_id))
        answer = [a for a in self.answers() if a.get("content")][-1]
        self.assertIn("Released", answer["content"])
        self.assertFalse(self.is_private(answer))
        self.assert_no_errors()

    async def test_a_failed_unmute_keeps_trying_until_it_works(self):
        await self.join(self.member_id, mute=True)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        await self.age_everything()
        restore = self.failing("PATCH", MEMBER_CALLS, 403)
        for _ in range(12):  # more tries than a timer gets when its handler raises
            await self.db.run("UPDATE timers SET due_at = ? WHERE kind = 'voice_action'", (time.time() - 1,))
            await self.bot.timers.run_due()
            timers = await self.bot.timers.list_kind("voice_action")
            self.assertEqual(len(timers), 1)
            self.assertEqual(timers[0]["tries"], 0)
            self.assertAlmostEqual(timers[0]["due_at"], time.time() + 60, delta=5)
            self.assertEqual(await self.actions(self.member_id), {MUTE})
        restore()
        await self.db.run("UPDATE timers SET due_at = ? WHERE kind = 'voice_action'", (time.time() - 1,))
        await self.bot.timers.run_due()
        self.assertEqual(self.edits_of(self.member_id)[-1]["mute"], False)
        self.assertEqual(await self.actions(self.member_id), set())
        self.assertEqual(await self.bot.timers.list_kind("voice_action"), [])
        self.assert_no_errors()

    async def test_lengths_in_every_form_the_commands_take(self):
        await self.join(self.member_id)
        await self.slash("vc_deafen", self.staff_id, user=self.member(self.member_id), minutes="20m", reason="too loud")
        row = await self.db.one("SELECT * FROM voice_actions WHERE action = 'deafen'")
        self.assertAlmostEqual(row["expires_at"], time.time() + 1200, delta=5)
        self.assertIn("too loud", row["reason"])
        self.dispatch_message(user_id=self.staff_id, content=f"!vc mute <@{self.member_id}> 15 noisy here")
        await self.settle()
        row = await self.db.one("SELECT * FROM voice_actions WHERE action = 'mute'")
        self.assertAlmostEqual(row["expires_at"], time.time() + 900, delta=5)
        self.assertIn("noisy here", row["reason"])
        self.dispatch_message(user_id=self.staff_id, content=f"!vc mute <@{self.member_id}> just being rude")
        await self.settle()
        row = await self.db.one("SELECT * FROM voice_actions WHERE action = 'mute'")
        self.assertAlmostEqual(row["expires_at"], time.time() + 600, delta=5)
        self.assertIn("just being rude", row["reason"])
        self.assert_no_errors()

    async def test_a_length_that_isnt_one_or_is_too_long_is_refused(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id), minutes="soon")
        self.assertIn("isn't a length", self.last_answer())
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id), minutes="30d")
        self.assertIn("seven days", self.last_answer())
        self.assertEqual(await self.actions(self.member_id), set())

    async def test_someone_not_in_voice_cant_be_muted(self):
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        self.assertIn("isn't connected", self.last_answer())
        self.assertEqual(await self.actions(self.member_id), set())
        self.assertEqual(self.edits_of(self.member_id), [])

    async def test_the_timer_reverses_a_mute_while_they_are_in_voice(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        await self.join(self.member_id, mute=True)
        await self.age_everything()
        await self.bot.timers.run_due()
        self.assertEqual(self.edits_of(self.member_id)[-1]["mute"], False)
        self.assertEqual(await self.actions(self.member_id), set())

    async def test_someone_who_left_voice_is_cleared_when_they_next_connect(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        await self.leave(self.member_id)
        await self.age_everything()
        edits = len(self.edits_of(self.member_id))
        await self.bot.timers.run_due()
        self.assertEqual(len(self.edits_of(self.member_id)), edits, "Discord refuses this outside voice, so nothing is tried")
        self.assertEqual(await self.actions(self.member_id), {MUTE}, "the row waits")
        await self.join(self.member_id, mute=True)
        self.assertEqual(self.edits_of(self.member_id)[-1]["mute"], False)
        self.assertEqual(await self.actions(self.member_id), set())
        self.assertNotIn((self.guild_id, self.member_id), self.voice_module._active)

    async def test_applying_again_replaces_the_timer_so_the_earlier_end_doesnt_cut_it_short(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id), minutes="5m")
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id), minutes="60m")
        rows = await self.db.all("SELECT * FROM voice_actions WHERE user_id = ?", (self.member_id,))
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["expires_at"], time.time() + 3600, delta=5)
        self.assertEqual(len(await self.bot.timers.list_kind("voice_action")), 1)
        await self.voice_module._action_due({"guild_id": self.guild_id, "user_id": self.member_id, "action": MUTE}, {})
        self.assertEqual(await self.actions(self.member_id), {MUTE}, "a stale timer does nothing while the row runs later")

    async def test_a_block_disconnects_and_follows_them_to_any_channel_until_it_lapses(self):
        await self.join(self.member_id)
        await self.slash("vc_disconnect", self.staff_id, user=self.member(self.member_id), block_minutes="30m")
        self.assertIn("channel_id", self.edits_of(self.member_id)[-1])
        self.assertIsNone(self.edits_of(self.member_id)[-1]["channel_id"])
        await self.leave(self.member_id)
        await self.join(self.member_id)
        self.assertIsNone(self.edits_of(self.member_id)[-1]["channel_id"], "rejoining gets them disconnected again")
        count = len(self.edits_of(self.member_id))
        await self.leave(self.member_id)
        await self.age_everything()
        await self.bot.timers.run_due()
        self.assertEqual(await self.actions(self.member_id), set())
        await self.join(self.member_id)
        self.assertEqual(len(self.edits_of(self.member_id)), count, "once it lapses they can stay")

    async def test_a_block_set_while_not_in_voice_still_holds(self):
        await self.slash("vc_disconnect", self.staff_id, user=self.member(self.member_id), block_minutes="30m")
        self.assertIn("blocked from joining", self.last_answer())
        await self.join(self.member_id)
        self.assertIsNone(self.edits_of(self.member_id)[-1]["channel_id"])

    async def test_plain_disconnect_leaves_no_block(self):
        await self.slash("vc_disconnect", self.staff_id, user=self.member(self.member_id))
        self.assertIn("isn't connected", self.last_answer())
        await self.join(self.member_id)
        await self.slash("vc_disconnect", self.staff_id, user=self.member(self.member_id))
        self.assertIn("Disconnected", self.last_answer())
        self.assertEqual(await self.actions(self.member_id), set())

    async def test_release_clears_now_when_they_are_in_voice(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        await self.slash("vc_deafen", self.staff_id, user=self.member(self.member_id))
        await self.slash("vc_release", self.staff_id, user=self.member(self.member_id))
        last = self.edits_of(self.member_id)[-1]
        self.assertEqual((last["mute"], last["deaf"]), (False, False))
        self.assertEqual(await self.actions(self.member_id), set())
        self.assertEqual(await self.bot.timers.list_kind("voice_action"), [])

    async def test_release_while_they_are_away_waits_for_their_next_connect(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        await self.slash("vc_disconnect", self.staff_id, user=self.member(self.member_id), block_minutes="1h")
        await self.leave(self.member_id)
        await self.slash("vc_release", self.staff_id, user=self.member(self.member_id))
        self.assertIn("clears the moment they reconnect", self.last_answer())
        self.assertEqual(await self.actions(self.member_id), {MUTE})
        await self.join(self.member_id, mute=True)
        self.assertEqual(self.edits_of(self.member_id)[-1]["mute"], False)
        self.assertEqual(await self.actions(self.member_id), set())

    async def test_a_release_waiting_for_their_next_connect_survives_a_restart(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        await self.leave(self.member_id)
        await self.slash("vc_release", self.staff_id, user=self.member(self.member_id))
        await self.voice_module.on_ready()
        self.voice_module._flush_loop.cancel()
        self.assertEqual(await self.actions(self.member_id), {MUTE}, "startup's cleanup keeps it")
        await self.join(self.member_id, mute=True)
        self.assertEqual(self.edits_of(self.member_id)[-1]["mute"], False)

    async def test_release_with_nothing_to_release_says_so(self):
        await self.slash("vc_release", self.staff_id, user=self.member(self.member_id))
        self.assertIn("no temporary voice actions", self.last_answer())

    async def test_release_is_for_moderators(self):
        await self.slash("vc_release", self.vet_id, user=self.member(self.member_id))
        self.assertIn("Only moderators", self.last_answer())

    async def test_a_moderator_cant_act_on_someone_above_their_top_role_or_the_owner(self):
        await self.join(self.admin_id)
        await self.join(self.owner_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.admin_id))
        self.assertIn("above yours", self.last_answer())
        await self.slash("vc_mute", self.admin_id, user=self.member(self.owner_id))
        self.assertIn("server owner", self.last_answer())
        self.assertEqual(self.edits_of(self.admin_id), [])
        self.assertEqual(self.edits_of(self.owner_id), [])

    async def test_ordinary_members_cant_use_them(self):
        await self.join(self.mod_id)
        await self.slash("vc_mute", self.member_id, user=self.member(self.mod_id))
        self.assertIn("Only moderators and VC veterans", self.last_answer())

    async def test_discord_refusing_is_reported_and_stores_nothing(self):
        await self.join(self.member_id)
        restore = self.failing("PATCH", MEMBER_CALLS, 403)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        restore()
        self.assertTrue(self.last_answer())
        self.assertEqual(await self.actions(self.member_id), set())
        self.assert_no_errors()

    async def test_voice_traffic_from_people_with_no_action_costs_no_query(self):
        with mock.patch.object(self.db, "all", wraps=self.db.all) as reads:
            await self.join(self.member_id)
        self.assertEqual(reads.call_count, 0)

    async def test_rows_survive_a_restart(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        fresh = Voice(self.bot)
        await fresh._load_active()
        self.assertIn((self.guild_id, self.member_id), fresh._active)


class VeteranTest(VoiceTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.watch_member_edits()

    async def test_a_veteran_is_capped_at_the_default_and_logged(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.vet_id, user=self.member(self.member_id), minutes="2h")
        row = await self.db.one("SELECT * FROM voice_actions WHERE user_id = ?", (self.member_id,))
        self.assertAlmostEqual(row["expires_at"], time.time() + 600, delta=5)
        self.assertIn("capped at 10 minutes", self.last_answer())
        entry = self.logged(self.reports_channel)[-1]
        self.assertEqual(entry["title"], "VC Veteran Action")
        self.assertIn(str(self.vet_id), entry["fields"][0]["value"])
        self.assertIn(str(self.member_id), entry["fields"][1]["value"])

    async def test_a_moderator_isnt_capped_and_isnt_logged(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id), minutes="2h")
        row = await self.db.one("SELECT * FROM voice_actions WHERE user_id = ?", (self.member_id,))
        self.assertAlmostEqual(row["expires_at"], time.time() + 7200, delta=5)
        self.assertEqual(self.logged(self.reports_channel), [])

    async def test_a_veteran_cant_touch_staff_fellow_veterans_or_bots(self):
        other_vet = self.add_member("vet2", self.vet_role["id"])
        bot_id = self.add_member("musicbot", bot=True)
        for target, expected in ((self.staff_id, "moderators or fellow"), (other_vet, "moderators or fellow"), (bot_id, "bots"), (self.admin_id, "moderators or fellow")):
            await self.join(target)
            await self.slash("vc_mute", self.vet_id, user=self.member(target))
            self.assertIn(expected, self.last_answer())
            self.assertEqual(self.edits_of(target), [])

    async def test_the_top_role_rule_doesnt_get_in_a_veterans_way(self):
        high = self.add_role("Vanity", 3)
        target = self.add_member("colorful", high["id"])
        await self.join(target)
        await self.slash("vc_mute", self.vet_id, user=self.member(target))
        self.assertIn("Server-muted", self.last_answer())


class ActionsPanelTest(VoiceTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.watch_member_edits()

    async def test_the_menu_opens_a_private_panel_and_each_button_acts(self):
        await self.join(self.member_id)
        await self.user_menu("Voice Actions", self.staff_id, self.member_id)
        panel = self.net.posted[-1]
        self.assertTrue(panel["flags"] & 64)
        buttons = self.buttons(panel)
        self.assertEqual(list(buttons), ["Temp Mute", "Temp Deafen", "Disconnect"])
        await self.press(buttons["Temp Mute"]["custom_id"], panel, self.staff_id)
        self.assertEqual(self.edits_of(self.member_id)[-1]["mute"], True)
        await self.press(buttons["Temp Deafen"]["custom_id"], panel, self.staff_id)
        self.assertEqual(self.edits_of(self.member_id)[-1]["deaf"], True)
        self.assertEqual(await self.actions(self.member_id), {MUTE, DEAFEN})
        await self.press(buttons["Disconnect"]["custom_id"], panel, self.staff_id)
        self.assertIsNone(self.edits_of(self.member_id)[-1]["channel_id"])
        self.assertNotIn(BLOCK, await self.actions(self.member_id), "a plain disconnect from the menu")
        self.assert_no_errors()

    async def test_the_panel_leaves_the_same_public_note_as_the_commands(self):
        await self.join(self.member_id)
        await self.user_menu("Voice Actions", self.staff_id, self.member_id)
        panel = self.net.posted[-1]
        await self.press(self.buttons(panel)["Temp Mute"]["custom_id"], panel, self.staff_id)
        note = [a for a in self.answers() if a.get("content")][-1]
        self.assertIn("Server-muted", note["content"])
        self.assertFalse(self.is_private(note))
        self.assert_no_errors()

    async def test_the_panel_keeps_a_refusal_to_itself(self):
        await self.user_menu("Voice Actions", self.staff_id, self.member_id)
        panel = self.net.posted[-1]
        before = len(self.calls_to("POST", "/webhooks/{webhook_id}/{webhook_token}"))
        await self.press(self.buttons(panel)["Temp Mute"]["custom_id"], panel, self.staff_id)
        self.assertEqual(len(self.calls_to("POST", "/webhooks/{webhook_id}/{webhook_token}")), before, "no note goes out")
        self.assertIn("isn't connected", self.last_answer())

    async def test_ordinary_members_get_no_panel(self):
        await self.user_menu("Voice Actions", self.mod_id, self.member_id)
        self.assertIn("Only moderators and VC veterans", self.last_answer())
        self.assertFalse(self.net.posted and self.buttons(self.net.posted[-1]))

    async def test_a_veteran_uses_the_panel_within_their_limits(self):
        await self.join(self.staff_id)
        await self.user_menu("Voice Actions", self.vet_id, self.staff_id)
        panel = self.net.posted[-1]
        await self.press(self.buttons(panel)["Temp Mute"]["custom_id"], panel, self.vet_id)
        self.assertEqual(self.edits_of(self.staff_id), [])
        self.assert_no_errors()


class VCReportTest(VoiceTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.watch_member_edits()

    async def filed(self, *, by: int | None = None) -> dict:
        await self.slash("vc_report", by or self.mod_id, user=self.member(self.member_id), reason="Playing loud music")
        return self.posts_in(self.reports_channel["id"])[-1]

    async def test_a_report_reaches_staff_with_who_is_in_the_channel_and_pings_moderators(self):
        await self.join(self.member_id)
        await self.join(self.admin_id)
        post = await self.filed()
        embed = post["embeds"][0]
        self.assertEqual(embed["title"], "Voice Channel Report")
        names = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertIn(str(self.member_id), names["Reported user"])
        self.assertIn(str(self.mod_id), names["Reported by"])
        self.assertEqual(names["Voice channel"], f"<#{self.voice['id']}>")
        self.assertIn("admin", names["Currently in channel"])
        self.assertEqual(names["Reason"], "Playing loud music")
        self.assertEqual(post["content"], f"<@&{self.staff_role['id']}>")
        self.assertEqual(list(self.buttons(post)), ["Temp Mute", "Temp Deafen", "Temp Remove", "Dismiss Report"])
        row = await self.db.one("SELECT * FROM voice_reports")
        self.assertEqual((row["target_id"], row["status"]), (self.member_id, "open"))
        self.assertIn("went to the moderators", self.last_answer())
        self.assert_no_errors()

    async def test_the_ping_goes_to_the_role_named_here_whatever_else_counts_as_a_moderator(self):
        for _ in range(64):  # the pair a set would hand back in the wrong order
            named, other = self.add_role("Named here", 3), self.add_role("Bot manager", 3)
            if next(iter({int(named["id"]), int(other["id"])})) == int(other["id"]):
                break
        await self.configure(moderator_role=int(named["id"]))
        await self.bot.settings.save(self.guild_id, COMMANDS_MODULE, {"mod_role": int(other["id"])})
        self.assertEqual((await self.filed())["content"], f"<@&{named['id']}>")
        await self.configure(moderator_role=None)
        self.voice_module._cooldowns.clear()
        self.voice_module._report_pings.clear()
        self.assertEqual((await self.filed())["content"], f"<@&{other['id']}>", "with none named here, whoever else counts as one")
        self.assert_no_errors()

    async def test_reported_while_not_in_voice_says_so(self):
        post = await self.filed()
        self.assertEqual({f["name"]: f["value"] for f in post["embeds"][0]["fields"]}["Voice channel"], "Not currently in voice")

    async def test_a_report_typed_as_text_comes_down_and_leaves_nothing_behind(self):
        self.dispatch_message(user_id=self.mod_id, content=f"!vc report <@{self.member_id}> shouting")
        await self.settle()
        self.assertTrue(self.calls_to("DELETE", "/channels/{channel_id}/messages/{message_id}"))
        self.assertEqual(await self.db.scalar("SELECT reason FROM voice_reports"), "shouting")
        self.assert_no_errors()

    async def test_self_bots_and_cooldown_are_refused(self):
        await self.slash("vc_report", self.member_id, user=self.member(self.member_id), reason="x")
        self.assertIn("yourself", self.last_answer())
        bot_id = self.add_member("musicbot", bot=True)
        await self.slash("vc_report", self.member_id, user=self.member(bot_id), reason="x")
        self.assertIn("Bots", self.last_answer())
        await self.filed()
        await self.slash("vc_report", self.mod_id, user=self.member(self.member_id), reason="again")
        self.assertIn("too fast", self.last_answer())
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voice_reports"), 1)

    async def test_no_channel_or_a_deleted_one_says_so(self):
        await self.configure(report_channel=None)
        await self.slash("vc_report", self.mod_id, user=self.member(self.member_id), reason="x")
        self.assertIn("aren't set up", self.last_answer())
        await self.configure(report_channel=new_id())
        await self.user_menu("VC Report", self.mod_id, self.member_id)
        self.assertIn("no longer exists", self.last_answer())
        self.assertEqual(self.net.modals, [])

    async def test_a_post_discord_refuses_tells_the_reporter(self):
        restore = self.failing("POST", "/channels/{channel_id}/messages", 403)
        await self.slash("vc_report", self.mod_id, user=self.member(self.member_id), reason="x")
        restore()
        self.assertIn("can't post", self.last_answer())
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voice_reports"), 0)
        self.assert_no_errors()

    async def test_the_right_click_menu_takes_a_reason(self):
        await self.user_menu("VC Report", self.mod_id, self.member_id)
        self.assertEqual(self.net.modals[-1]["title"], "Report to voice moderators")
        await self.submit_form(self.mod_id, "Shouting")
        self.assertEqual(self.posts_in(self.reports_channel["id"])[-1]["embeds"][0]["fields"][-1]["value"], "Shouting")
        await self.user_menu("VC Report", self.mod_id, self.member_id)
        self.assertIn("too fast", self.last_answer())

    async def test_buttons_act_with_the_default_length_and_note_it_on_the_report(self):
        await self.join(self.member_id)
        post = await self.filed()
        await self.press(REPORT_MUTE_ID, post, self.staff_id)
        self.assertEqual(self.edits_of(self.member_id)[-1]["mute"], True)
        self.assertEqual(await self.actions(self.member_id), {MUTE})
        await self.press(REPORT_DEAFEN_ID, post, self.staff_id)
        self.assertEqual(await self.actions(self.member_id), {MUTE, DEAFEN})
        await self.press(REPORT_REMOVE_ID, post, self.staff_id)
        self.assertEqual(await self.actions(self.member_id), {MUTE, DEAFEN, BLOCK})
        self.assert_no_errors()

    async def test_action_taken_is_added_to_the_report(self):
        await self.join(self.member_id)
        post = await self.filed()
        await self.press(REPORT_MUTE_ID, post, self.staff_id)
        edited = next(b for m, p, b in reversed(self.net.calls) if "embeds" in b and m == "PATCH")
        self.assertEqual(edited["embeds"][0]["fields"][-1]["name"], "Action taken")
        self.assertIn(f"<@{self.staff_id}>", edited["embeds"][0]["fields"][-1]["value"])

    async def test_dismiss_closes_it_and_takes_the_buttons_away(self):
        post = await self.filed()
        await self.press(REPORT_DISMISS_ID, post, self.staff_id)
        answer = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertEqual(answer["components"], [])
        self.assertIn("Dismissed by", answer["embeds"][0]["fields"][-1]["value"])
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_reports"), "handled")
        await self.press(REPORT_MUTE_ID, post, self.staff_id)
        self.assertIn("already been dismissed", self.last_answer())

    async def test_veterans_dont_handle_the_queue(self):
        post = await self.filed()
        await self.press(REPORT_DISMISS_ID, post, self.vet_id)
        self.assertIn("Only moderators", self.last_answer())
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_reports"), "open")

    async def test_buttons_respect_the_hierarchy_and_a_member_who_left(self):
        await self.slash("vc_report", self.mod_id, user=self.member(self.admin_id), reason="abuse")
        post = self.posts_in(self.reports_channel["id"])[-1]
        await self.press(REPORT_MUTE_ID, post, self.staff_id)
        self.assertIn("above yours", self.last_answer())
        self.voice_module._cooldowns.clear()
        await self.slash("vc_report", self.mod_id, user=self.member(self.member_id), reason="abuse")
        post = self.posts_in(self.reports_channel["id"])[-1]
        self.state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.member_id]["user"]})
        self.net.members.pop(self.member_id)
        await self.press(REPORT_MUTE_ID, post, self.staff_id)
        self.assertIn("no longer in the server", self.last_answer())
        self.assert_no_errors()

    async def test_the_reports_banned_role_applies_here_too(self):
        banned = self.add_role("No Reports", 1)
        blocked = self.add_member("blocked", banned["id"])
        fake_reports = mock.Mock(key="reports")
        fake_reports.cfg.return_value = {"banned_role": int(banned["id"]), "moderator_role": None}
        self.bot.modules.append(fake_reports)
        await self.slash("vc_report", blocked, user=self.member(self.member_id), reason="x")
        self.assertIn("stops you", self.last_answer())


class AccessTest(VoiceTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.old_member = self.add_member("regular", joined=LONG_AGO)
        await self.configure(min_messages=2, min_days=7)

    async def chat(self, user_id: int, times: int = 2) -> None:
        for _ in range(times):
            self.dispatch_message(user_id=user_id, content="hello")
        await self.settle()

    def request_button(self) -> dict:
        return self.net.message_json(int(self.text["id"]), {"embeds": [{"title": "Voice Channel Access"}]})

    async def ask(self, user_id: int) -> None:
        await self.press(ACCESS_REQUEST_ID, self.request_button(), user_id)

    def request_post(self) -> dict:
        return self.posts_in(self.approvals["id"])[-1]

    async def test_the_button_is_posted_with_the_requirements(self):
        await self.slash("vc", self.admin_id, "button")
        post = self.net.posted[-2] if not self.net.posted[-1].get("embeds") else self.net.posted[-1]
        posts = [m for m in self.posts_in(self.text["id"]) if m["embeds"]]
        self.assertIn("**2 messages**", posts[-1]["embeds"][0]["description"])
        self.assertIn("**7 days**", posts[-1]["embeds"][0]["description"])
        self.assertEqual(self.buttons(posts[-1])["Request Voice Access"]["custom_id"], ACCESS_REQUEST_ID)
        self.assertNotIn(int(posts[-1]["id"]), self.bot._connection._view_store._views)
        self.assert_no_errors()

    async def test_only_manage_server_can_post_it(self):
        await self.slash("vc", self.staff_id, "button")
        self.assertIn("Manage Server", self.last_answer())

    async def test_requirements_are_checked_and_named(self):
        new_member = self.add_member("fresh", joined=discord.utils.utcnow())
        await self.ask(new_member)
        self.assertIn("messages: 0 of 2", self.last_answer())
        self.assertIn("days on the server: 0 of 7", self.last_answer())
        await self.chat(new_member)
        await self.ask(new_member)
        self.assertNotIn("messages:", self.last_answer())
        self.assertIn("days on the server", self.last_answer())
        self.assertEqual(self.posts_in(self.approvals["id"]), [])

    async def test_a_request_that_qualifies_goes_to_staff_once(self):
        await self.chat(self.old_member)
        await self.ask(self.old_member)
        self.assertIn("went to the moderators", self.last_answer())
        post = self.request_post()
        names = {f["name"]: f["value"] for f in post["embeds"][0]["fields"]}
        self.assertEqual(names["Messages"], "2 (requires 2)")
        self.assertEqual(list(self.buttons(post)), ["Approve VC Access", "Deny"])
        await self.ask(self.old_member)
        self.assertIn("already waiting", self.last_answer())
        self.assertEqual(len(self.posts_in(self.approvals["id"])), 1)
        row = await self.db.one("SELECT * FROM voice_access")
        self.assertEqual((row["status"], row["request_message_id"]), ("pending", int(post["id"])))

    async def test_two_clicks_at_the_same_moment_post_once(self):
        await self.chat(self.old_member)
        member = self.member(self.old_member)
        answers = await asyncio.gather(self.voice_module.submit_request(member), self.voice_module.submit_request(member))
        self.assertEqual(len(self.posts_in(self.approvals["id"])), 1)
        self.assertEqual(sum("already waiting" in a for a in answers), 1)

    async def test_a_channel_that_refuses_gives_the_claim_back(self):
        await self.chat(self.old_member)
        restore = self.failing("POST", "/channels/{channel_id}/messages", 403)
        await self.ask(self.old_member)
        restore()
        self.assertIn("couldn't reach", self.last_answer())
        self.assertIsNone(await self.db.one("SELECT * FROM voice_access"))
        await self.ask(self.old_member)
        self.assertIn("went to the moderators", self.last_answer())

    async def test_not_set_up_says_so(self):
        await self.configure(access_role=None)
        await self.ask(self.old_member)
        self.assertIn("aren't fully set up", self.last_answer())

    async def test_approve_gives_the_role_and_closes_the_request(self):
        await self.chat(self.old_member)
        await self.ask(self.old_member)
        post = self.request_post()
        await self.press(ACCESS_APPROVE_ID, post, self.staff_id)
        self.assertEqual(len(self.role_calls("PUT")), 1)
        row = await self.db.one("SELECT * FROM voice_access")
        self.assertEqual((row["status"], row["decided_by"]), ("approved", self.staff_id))
        answer = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertEqual(answer["components"], [])
        self.assertIn("Approved by staffer", answer["embeds"][0]["footer"]["text"])
        await self.press(ACCESS_DENY_ID, post, self.staff_id)
        self.assertIn("already resolved", self.last_answer())

    async def test_a_veteran_can_approve_but_not_deny(self):
        await self.chat(self.old_member)
        await self.ask(self.old_member)
        post = self.request_post()
        await self.press(ACCESS_DENY_ID, post, self.vet_id)
        self.assertIn("Only moderators can deny", self.last_answer())
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access"), "pending")
        await self.press(ACCESS_APPROVE_ID, post, self.vet_id)
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access"), "approved")
        self.assertIn("(VC veteran)", [a for a in self.answers() if a.get("embeds")][-1]["embeds"][0]["footer"]["text"])

    async def test_ordinary_members_cant_decide(self):
        await self.chat(self.old_member)
        await self.ask(self.old_member)
        await self.press(ACCESS_APPROVE_ID, self.request_post(), self.mod_id)
        self.assertIn("Only moderators and VC veterans", self.last_answer())

    async def test_deny_stops_asking_again(self):
        await self.chat(self.old_member)
        await self.ask(self.old_member)
        await self.press(ACCESS_DENY_ID, self.request_post(), self.staff_id)
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access"), "denied")
        await self.ask(self.old_member)
        self.assertIn("denied or revoked", self.last_answer())
        self.assertEqual(len(self.posts_in(self.approvals["id"])), 1)

    async def test_approving_someone_who_left_keeps_it_pending(self):
        await self.chat(self.old_member)
        await self.ask(self.old_member)
        self.state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.old_member]["user"]})
        self.net.members.pop(self.old_member)
        await self.settle()
        await self.press(ACCESS_APPROVE_ID, self.request_post(), self.staff_id)
        self.assertIn("stays pending", self.last_answer())
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access"), "pending")

    async def test_the_role_comes_back_on_rejoin_and_on_request(self):
        await self.voice_module._set_access(self.guild_id, self.old_member, "regular", "approved", self.staff_id, "staffer")
        self.state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.old_member]["user"]})
        await self.settle()
        self.state.parse_guild_member_add({"guild_id": str(self.guild_id), **self.member_json[self.old_member]})
        await self.settle()
        self.assertEqual(len(self.role_calls("PUT")), 1)
        await self.ask(self.old_member)
        self.assertIn("already approved", self.last_answer())
        self.assertEqual(len(self.role_calls("PUT")), 2)

    async def test_a_member_on_the_rules_screen_waits_until_they_accept(self):
        await self.voice_module._set_access(self.guild_id, self.old_member, "regular", "approved", self.staff_id, "staffer")
        self.state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.old_member]["user"]})
        pending = {**self.member_json[self.old_member], "pending": True}
        self.state.parse_guild_member_add({"guild_id": str(self.guild_id), **pending})
        await self.settle()
        self.assertEqual(self.role_calls("PUT"), [])
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **{**pending, "pending": False}})
        await self.settle()
        self.assertEqual(len(self.role_calls("PUT")), 1)

    async def test_denied_doesnt_come_back_on_rejoin(self):
        await self.voice_module._set_access(self.guild_id, self.old_member, "regular", "denied", self.staff_id, "staffer")
        self.state.parse_guild_member_add({"guild_id": str(self.guild_id), **self.member_json[self.old_member]})
        await self.settle()
        self.assertEqual(self.role_calls("PUT"), [])

    async def test_grant_skips_the_requirements_and_closes_a_pending_request(self):
        await self.chat(self.old_member)
        await self.ask(self.old_member)
        post = self.request_post()
        await self.slash("vc_access_grant", self.staff_id, user=self.member(self.old_member))
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access"), "approved")
        edited = self.net.messages[int(post["id"])]
        self.assertEqual(edited["components"], [])
        self.assertIn("Granted by", edited["embeds"][0]["footer"]["text"])
        self.assertEqual(self.logged(self.approvals), [], "a moderator's grant isn't logged")
        self.assert_no_errors()

    async def test_a_veterans_grant_is_logged_for_moderators(self):
        await self.slash("vc_access_grant", self.vet_id, user=self.member(self.old_member))
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access"), "approved")
        self.assertEqual(self.logged(self.approvals)[-1]["title"], "Voice Access Granted")

    async def test_ordinary_members_cant_grant(self):
        await self.slash("vc_access_grant", self.mod_id, user=self.member(self.old_member))
        self.assertIn("Only moderators and VC veterans", self.last_answer())

    async def test_a_role_with_power_is_never_handed_out(self):
        strong = self.add_role("Power", 1, discord.Permissions(ban_members=True).value)
        await self.configure(access_role=int(strong["id"]))
        await self.slash("vc_access_grant", self.staff_id, user=self.member(self.old_member))
        self.assertIn("moderation permissions", self.last_answer())
        self.assertEqual(self.role_calls("PUT"), [])
        await self.bot.settings.save(self.guild_id, "commands", {"elevated_roles": True})
        await self.slash("vc_access_grant", self.staff_id, user=self.member(self.old_member))
        self.assertEqual(len(self.role_calls("PUT")), 1)

    async def test_a_role_above_the_bot_is_refused_with_a_reason(self):
        high = self.add_role("Too High", 50)
        await self.configure(access_role=int(high["id"]))
        await self.slash("vc_access_grant", self.staff_id, user=self.member(self.old_member))
        self.assertIn("below mine", self.last_answer())

    async def test_revoke_takes_the_role_stops_asking_disconnects_and_logs(self):
        await self.slash("vc_access_grant", self.staff_id, user=self.member(self.old_member))
        member_data = self.member_json[self.old_member]
        member_data["roles"] = [*member_data["roles"], self.access_role["id"]]
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **member_data})
        await self.join(self.old_member)
        self.watch_member_edits()
        await self.slash("vc_access_revoke", self.staff_id, user=self.member(self.old_member), reason="Abuse in voice")
        self.assertEqual(len(self.role_calls("DELETE")), 1)
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access"), "revoked")
        self.assertIsNone(self.edits_of(self.old_member)[-1]["channel_id"])
        self.assertFalse(self.is_private([a for a in self.answers() if a.get("content")][-1]))
        deferred = [body for _, path, body in self.net.calls if path.endswith("/callback")][-1]
        self.assertEqual(deferred["type"], 5)
        self.assertFalse((deferred.get("data") or {}).get("flags", 0) & 64, "the wait itself is public, because the first answer takes after it")
        entry = self.logged(self.approvals)[-1]
        self.assertEqual(entry["title"], "Voice Access Revoked")
        self.assertEqual(entry["fields"][-1]["value"], "Abuse in voice")
        member_data["roles"] = [r for r in member_data["roles"] if r != self.access_role["id"]]
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **member_data})
        await self.ask(self.old_member)
        self.assertIn("denied or revoked", self.last_answer())

    async def test_a_server_that_muted_the_command_gets_a_quiet_revoke(self):
        await self.bot.settings.save(self.guild_id, COMMANDS_MODULE, {"overwrites": {"vc_access_revoke": {"muted": True}}})
        await self.slash("vc_access_revoke", self.staff_id, user=self.member(self.old_member))
        deferred = [body for _, path, body in self.net.calls if path.endswith("/callback")][-1]
        self.assertTrue((deferred.get("data") or {}).get("flags", 0) & 64)
        self.assertTrue(self.is_private([a for a in self.answers() if a.get("content")][-1]))

    async def test_grant_answers_in_public(self):
        await self.slash("vc_access_grant", self.staff_id, user=self.member(self.old_member))
        answer = [a for a in self.answers() if a.get("content")][-1]
        self.assertIn("Granted voice access", answer["content"])
        self.assertFalse(self.is_private(answer))

    async def test_revoke_is_for_moderators_and_follows_the_hierarchy(self):
        await self.slash("vc_access_revoke", self.vet_id, user=self.member(self.old_member))
        self.assertIn("Only moderators", self.last_answer())
        await self.slash("vc_access_revoke", self.staff_id, user=self.member(self.admin_id))
        self.assertIn("above yours", self.last_answer())
        self.assertIsNone(await self.db.one("SELECT * FROM voice_access"))

    async def test_nested_commands_work_as_text_too(self):
        self.dispatch_message(user_id=self.staff_id, content=f"!vc access grant <@{self.old_member}>")
        await self.settle()
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access"), "approved")
        self.dispatch_message(user_id=self.staff_id, content=f"!vc access status <@{self.old_member}>")
        await self.settle()
        self.dispatch_message(user_id=self.staff_id, content="!vc access")
        await self.settle()
        self.assert_no_errors()

    async def test_status_and_progress(self):
        await self.chat(self.old_member, 3)
        await self.slash("vc_access_status", self.staff_id, user=self.member(self.old_member))
        self.assertIn("3 / 2", self.last_answer())
        self.assertIn("no request yet", self.last_answer())
        await self.slash("vc_access_status", self.old_member, user=self.member(self.staff_id))
        self.assertIn("Only moderators", self.last_answer())
        await self.slash("vc", self.old_member, "progress")
        self.assertIn("3 / 2", self.last_answer())
        self.assert_no_errors()

    async def test_messages_are_counted_in_batches_and_survive_a_flush(self):
        await self.chat(self.old_member, 3)
        self.dispatch_message(user_id=self.bot_id, content="a bot doesn't count")
        await self.settle()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voice_message_counts"), 0, "nothing is written per message")
        self.assertEqual(await self.voice_module._message_count(self.guild_id, self.old_member), 3)
        await self.voice_module.flush_counts()
        self.assertEqual(await self.db.scalar("SELECT count FROM voice_message_counts WHERE user_id = ?", (self.old_member,)), 3)
        await self.chat(self.old_member, 2)
        await self.voice_module.flush_counts()
        self.assertEqual(await self.voice_module._message_count(self.guild_id, self.old_member), 5)
        self.assertIsNone(await self.db.scalar("SELECT count FROM voice_message_counts WHERE user_id = ?", (self.bot_id,)))

    async def test_counts_are_written_on_shutdown(self):
        await self.chat(self.old_member, 1)
        await self.voice_module.flush_counts()
        await self.chat(self.old_member, 1)
        menus = self.voice_module._menus
        await self.voice_module.cog_unload()
        self.assertEqual(await self.db.scalar("SELECT count FROM voice_message_counts WHERE user_id = ?", (self.old_member,)), 2)
        for menu in menus:
            self.bot.tree.add_command(menu)


class MuteBase(VoiceTest):
    async def mute_row(self, user_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM mini_mutes WHERE guild_id = ? AND user_id = ?", (self.guild_id, user_id))

    def holds_role(self, user_id: int) -> bool:
        member = self.member(user_id)
        return any(role.id == int(self.mute_role["id"]) for role in member.roles)

    def give_role_in_cache(self, user_id: int) -> None:
        data = self.member_json[user_id]
        data["roles"] = [*data["roles"], self.mute_role["id"]]
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **data})


class MiniMuteTest(MuteBase):
    async def test_one_vote_server_applies_at_once_and_logs(self):
        await self.slash("mini_mute", self.server_vet_id, user=self.member(self.member_id), reason="Spamming")
        self.assertEqual(len(self.role_calls("PUT")), 1)
        row = await self.mute_row(self.member_id)
        self.assertEqual((row["applied_by"], row["reason"]), (self.server_vet_id, "Spamming"))
        self.assertAlmostEqual(row["expires_at"], time.time() + 600, delta=5)
        self.assertIsNotNone(await self.bot.timers.get_key("mini_mute", f"{self.guild_id}:{self.member_id}"))
        entry = self.logged(self.mini_log)[-1]
        self.assertEqual(entry["title"], "Mini Mute Applied")
        self.assertIn("(Server Veteran)", entry["fields"][1]["value"])
        self.assertIn("is muted for 10 minutes", self.last_answer())
        self.assert_no_errors()

    async def test_the_length_comes_from_the_server_and_nobody_chooses_it(self):
        await self.configure(mini_mute_minutes=45)
        await self.slash("mini_mute", self.server_vet_id, user=self.member(self.member_id))
        self.assertAlmostEqual((await self.mute_row(self.member_id))["expires_at"], time.time() + 2700, delta=5)

    async def test_who_can_be_targeted_by_a_veteran(self):
        vc_vet = self.vet_id
        bot_id = self.add_member("somebot", bot=True)
        cases = [(self.server_vet_id, "yourself"), (bot_id, "Bots"), (self.staff_id, "moderators"), (self.server_vet_two, "fellow veterans"), (vc_vet, "fellow veterans"), (self.admin_id, "moderators")]
        for target, expected in cases:
            await self.slash("mini_mute", self.server_vet_id, user=self.member(target))
            self.assertIn(expected, self.last_answer())
        self.assertEqual(self.role_calls("PUT"), [])

    async def test_a_moderator_follows_the_hierarchy_and_can_mute_a_veteran(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.admin_id))
        self.assertIn("above yours", self.last_answer())
        await self.slash("mini_mute", self.staff_id, user=self.member(self.server_vet_id))
        self.assertIn("is muted", self.last_answer())

    async def test_ordinary_members_cant(self):
        await self.slash("mini_mute", self.mod_id, user=self.member(self.member_id))
        self.assertIn("Only Server Veterans", self.last_answer())

    async def test_no_role_or_a_role_above_the_bot_says_so(self):
        await self.configure(mini_mute_role=None)
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.assertIn("No mini mute role", self.last_answer())
        high = self.add_role("Too High", 50)
        await self.configure(mini_mute_role=int(high["id"]))
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.assertIn("at or above my top role", self.last_answer())
        self.assertIsNone(await self.mute_row(self.member_id))

    async def test_a_mute_role_with_power_is_refused(self):
        strong = self.add_role("Power", 1, discord.Permissions(ban_members=True).value)
        await self.configure(mini_mute_role=int(strong["id"]))
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.assertIn("moderation permissions", self.last_answer())
        self.assertEqual(self.role_calls("PUT"), [])

    async def test_a_running_mute_isnt_stacked_on(self):
        self.give_role_in_cache(self.member_id)
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.assertIn("already muted", self.last_answer())
        self.assertEqual(self.role_calls("PUT"), [])
        self.assertIsNone(await self.mute_row(self.member_id))

    async def test_the_timer_takes_the_role_back(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.give_role_in_cache(self.member_id)
        await self.age_everything()
        await self.bot.timers.run_due()
        self.assertEqual(len(self.role_calls("DELETE")), 1)
        self.assertIsNone(await self.mute_row(self.member_id))

    async def test_a_failed_removal_keeps_trying_until_it_works(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.give_role_in_cache(self.member_id)
        await self.age_everything()
        restore = self.failing("DELETE", "/guilds/{guild_id}/members/{user_id}/roles/{role_id}", 403)
        for _ in range(12):  # more tries than a timer gets when its handler raises
            await self.db.run("UPDATE timers SET due_at = ? WHERE kind = 'mini_mute'", (time.time() - 1,))
            await self.bot.timers.run_due()
            timers = await self.bot.timers.list_kind("mini_mute")
            self.assertEqual(len(timers), 1)
            self.assertEqual(timers[0]["tries"], 0)
            self.assertAlmostEqual(timers[0]["due_at"], time.time() + 60, delta=5)
            self.assertIsNotNone(await self.mute_row(self.member_id))
        restore()
        await self.db.run("UPDATE timers SET due_at = ? WHERE kind = 'mini_mute'", (time.time() - 1,))
        await self.bot.timers.run_due()
        self.assertEqual(len(self.role_calls("DELETE")), 1)
        self.assertIsNone(await self.mute_row(self.member_id))
        self.assert_no_errors()

    async def test_a_timer_that_gave_up_is_set_again_at_startup(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        await self.db.run("DELETE FROM timers")
        await self.voice_module._rearm_timers()
        self.assertEqual(len(await self.bot.timers.list_kind("mini_mute")), 1)

    async def test_rejoining_puts_a_running_mute_back(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.member_id]["user"]})
        await self.settle()
        self.state.parse_guild_member_add({"guild_id": str(self.guild_id), **self.member_json[self.member_id]})
        await self.settle()
        self.assertEqual(len(self.role_calls("PUT")), 2)

    async def test_rejoining_after_it_ran_out_clears_it(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.member_id]["user"]})
        await self.settle()
        await self.age_everything()
        self.state.parse_guild_member_add({"guild_id": str(self.guild_id), **self.member_json[self.member_id]})
        await self.settle()
        self.assertEqual(len(self.role_calls("PUT")), 1)
        self.assertIsNone(await self.mute_row(self.member_id))

    async def test_lift_takes_it_off_for_moderators_only(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.give_role_in_cache(self.member_id)
        await self.slash("mini_mute_lift", self.server_vet_id, user=self.member(self.member_id))
        self.assertIn("Only moderators", self.last_answer())
        await self.slash("mini_mute_lift", self.staff_id, user=self.member(self.member_id), reason="Mistake")
        self.assertEqual(len(self.role_calls("DELETE")), 1)
        answer = [a for a in self.answers() if a.get("content")][-1]
        self.assertIn("Cleared the mute", answer["content"])
        self.assertFalse(self.is_private(answer))
        self.assertIsNone(await self.mute_row(self.member_id))
        self.assertEqual(await self.bot.timers.list_kind("mini_mute"), [])
        self.assertEqual(self.logged(self.mini_log)[-1]["title"], "Mini Mute Lifted")
        await self.slash("mini_mute_lift", self.staff_id, user=self.member(self.mod_id))
        self.assertIn("isn't mini muted", self.last_answer())

    async def test_the_right_click_menu_takes_an_optional_reason(self):
        await self.user_menu("Mini Mute", self.server_vet_id, self.member_id)
        self.assertEqual(self.net.modals[-1]["title"], "Mini mute this member")
        await self.submit_form(self.server_vet_id, "")
        self.assertIsNone((await self.mute_row(self.member_id))["reason"])
        await self.user_menu("Mini Mute", self.server_vet_id, self.staff_id)
        self.assertIn("moderators", self.last_answer())
        self.assert_no_errors()

    async def test_text_command_works(self):
        self.dispatch_message(user_id=self.server_vet_id, content=f"!mini_mute <@{self.member_id}> for spam")
        await self.settle()
        self.assertEqual((await self.mute_row(self.member_id))["reason"], "for spam")
        self.assert_no_errors()


class VoteTest(MuteBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.configure(mini_mute_votes_required=3, mini_mute_vote_window=30)

    async def opened(self) -> dict:
        await self.slash("mini_mute", self.server_vet_id, user=self.member(self.member_id), reason="Baiting")
        return self.posts_in(self.mini_log["id"])[-1]

    async def vote_row(self) -> dict:
        return await self.db.one("SELECT * FROM mini_mute_votes")

    def latest_vote_embed(self) -> dict:
        return [a for a in self.answers() if a.get("embeds")][-1]["embeds"][0]

    def vote_field(self, name: str) -> str:
        return {f["name"]: f["value"] for f in self.latest_vote_embed()["fields"]}[name]

    async def test_a_ballot_opens_counting_the_requester_and_pings_veterans(self):
        post = await self.opened()
        self.assertEqual(post["content"], f"<@&{self.server_vet_role['id']}>")
        fields = {f["name"]: f["value"] for f in post["embeds"][0]["fields"]}
        self.assertEqual(fields["Votes to apply"], "1 of 3")
        self.assertEqual(list(self.buttons(post)), ["Vote to Mute", "Vote Against", "Veto (moderator)"])
        self.assertIn("needs 2 more", self.last_answer())
        self.assertEqual(self.role_calls("PUT"), [])
        self.assertIsNotNone(await self.bot.timers.get_key("mini_mute_vote", post["id"]))
        self.assert_no_errors()

    async def test_moderators_skip_the_ballot(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.assertEqual(len(self.role_calls("PUT")), 1)
        self.assertEqual(self.posts_in(self.mini_log["id"]), [])

    async def test_one_open_vote_per_person(self):
        await self.opened()
        await self.slash("mini_mute", self.server_vet_two, user=self.member(self.member_id))
        self.assertIn("already open", self.last_answer())
        self.assertEqual(len(self.posts_in(self.mini_log["id"])), 1)

    async def test_no_log_channel_means_no_vote(self):
        await self.configure(mini_mute_log_channel=None)
        await self.slash("mini_mute", self.server_vet_id, user=self.member(self.member_id))
        self.assertIn("need a log channel", self.last_answer())

    async def test_quorum_applies_the_mute_and_closes(self):
        post = await self.opened()
        await self.press(VOTE_FOR_ID, post, self.server_vet_two)
        self.assertEqual(self.vote_field("Votes to apply"), "2 of 3")
        self.assertEqual((await self.vote_row())["status"], "open")
        self.assertEqual(self.role_calls("PUT"), [])
        await self.press(VOTE_FOR_ID, post, self.server_vet_three)
        self.assertEqual((await self.vote_row())["status"], "applied")
        self.assertEqual(len(self.role_calls("PUT")), 1)
        self.assertAlmostEqual((await self.mute_row(self.member_id))["expires_at"], time.time() + 600, delta=5)
        self.assertEqual(self.vote_field("Outcome"), "Applied")
        self.assertEqual([a for a in self.answers() if a.get("embeds")][-1]["components"], [])
        self.assertEqual(await self.bot.timers.list_kind("mini_mute_vote"), [])
        self.assertIn("Quorum reached", self.last_answer())
        self.assert_no_errors()

    async def test_voted_down_closes_with_no_mute(self):
        post = await self.opened()
        for voter in (self.server_vet_two, self.server_vet_three, self.staff_id):
            await self.press(VOTE_AGAINST_ID, post, voter)
        self.assertEqual((await self.vote_row())["status"], "rejected")
        self.assertEqual(self.role_calls("PUT"), [])
        self.assertIn("voted down", self.last_answer())

    async def test_one_vote_each(self):
        post = await self.opened()
        await self.press(VOTE_FOR_ID, post, self.server_vet_id)
        self.assertIn("already voted", self.last_answer())
        self.assertEqual(await self.voice_module._ballots(int(post["id"])), (1, 0))

    async def test_only_veterans_and_moderators_vote(self):
        post = await self.opened()
        await self.press(VOTE_FOR_ID, post, self.mod_id)
        self.assertIn("Only Server Veterans", self.last_answer())

    async def test_quorum_on_someone_already_muted_leaves_that_mute_alone(self):
        post = await self.opened()
        await self.press(VOTE_FOR_ID, post, self.server_vet_two)
        self.give_role_in_cache(self.member_id)
        await self.press(VOTE_FOR_ID, post, self.server_vet_three)
        self.assertIn("stays as it is", self.last_answer())
        self.assertEqual(self.role_calls("PUT"), [])
        self.assertIsNone(await self.mute_row(self.member_id))

    async def test_a_mute_that_cant_land_reopens_the_vote(self):
        post = await self.opened()
        await self.press(VOTE_FOR_ID, post, self.server_vet_two)
        restore = self.failing("PUT", "/guilds/{guild_id}/members/{user_id}/roles/{role_id}", 403)
        await self.press(VOTE_FOR_ID, post, self.server_vet_three)
        restore()
        self.assertEqual((await self.vote_row())["status"], "open")
        self.assertIn("can't apply the mute role", self.last_answer())
        self.assertIsNotNone(await self.bot.timers.get_key("mini_mute_vote", post["id"]))

    async def test_two_clicks_reaching_quorum_together_apply_once(self):
        post = await self.opened()
        await self.press(VOTE_FOR_ID, post, self.server_vet_two)
        for voter in (self.server_vet_three, self.staff_id):
            self.state.parse_interaction_create(self.interaction_payload(3, {"custom_id": VOTE_FOR_ID, "component_type": 2}, user_id=voter, message=post))
        await self.settle()
        self.assertEqual(len(self.role_calls("PUT")), 1)
        self.assert_no_errors()

    async def test_veto_is_for_moderators_and_closes_it(self):
        post = await self.opened()
        await self.press(VOTE_VETO_ID, post, self.server_vet_two)
        self.assertIn("Only moderators", self.last_answer())
        await self.press(VOTE_VETO_ID, post, self.staff_id)
        self.assertEqual((await self.vote_row())["status"], "vetoed")
        self.assertIn("Vetoed by", self.latest_vote_embed()["footer"]["text"])
        self.assertEqual(await self.bot.timers.list_kind("mini_mute_vote"), [])
        await self.press(VOTE_FOR_ID, post, self.server_vet_two)
        self.assertIn("already closed", self.last_answer())
        await self.press(VOTE_VETO_ID, post, self.staff_id)
        self.assertIn("already closed", self.last_answer())

    async def test_a_target_who_left_closes_the_vote(self):
        post = await self.opened()
        self.state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.member_id]["user"]})
        self.net.members.pop(self.member_id)
        await self.settle()
        await self.press(VOTE_FOR_ID, post, self.server_vet_two)
        self.assertEqual((await self.vote_row())["status"], "expired")
        self.assertIn("has left", self.last_answer())
        self.assert_no_errors()

    async def test_a_vote_that_runs_out_closes_itself_even_if_people_left(self):
        post = await self.opened()
        self.state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.member_id]["user"]})
        await self.settle()
        await self.age_everything()
        await self.bot.timers.run_due()
        self.assertEqual((await self.vote_row())["status"], "expired")
        edited = self.net.messages[int(post["id"])]
        self.assertEqual(edited["components"], [])
        self.assertEqual({f["name"]: f["value"] for f in edited["embeds"][0]["fields"]}["Outcome"], "Expired")
        self.assert_no_errors()

    async def test_a_closed_vote_ignores_its_old_timer(self):
        post = await self.opened()
        await self.press(VOTE_VETO_ID, post, self.staff_id)
        await self.voice_module._vote_due({"vote_id": int(post["id"])}, {})
        self.assertEqual((await self.vote_row())["status"], "vetoed")


class CaseTest(Harness):
    """The real moderation module on the other end of the interface."""
    modules = [Moderation, Voice]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.logs = FakeLogSink()
        self.voice_module = self.bot.module("voice")
        self.moderation = self.bot.module("moderation")
        self.mute_role = self.add_role("Mini Muted", 1)
        await self.bot.settings.save(self.guild_id, "voice", {"mini_mute_role": int(self.mute_role["id"]), "mini_mute_cases": True})

    async def cases(self) -> list[dict]:
        return (await self.moderation.history(self.guild_id, self.member_id))["cases"]

    async def test_a_mini_mute_becomes_a_case_and_its_lift_another(self):
        await self.slash("mini_mute", self.admin_id, user=self.guild.get_member(self.member_id), reason="Spam")
        cases = await self.cases()
        self.assertEqual([c["action"] for c in cases], ["minimute"])
        self.assertEqual((cases[0]["reason"], cases[0]["seconds"], cases[0]["moderator_id"], cases[0]["source"]), ("Spam", 600, self.admin_id, "mini mute"))
        await self.slash("mini_mute_lift", self.admin_id, user=self.guild.get_member(self.member_id))
        self.assertEqual([c["action"] for c in await self.cases()], ["unminimute", "minimute"])
        self.assert_no_errors()

    async def test_running_out_is_a_case_with_no_moderator(self):
        await self.slash("mini_mute", self.admin_id, user=self.guild.get_member(self.member_id))
        await self.db.run("UPDATE mini_mutes SET expires_at = ?", (time.time() - 1,))
        await self.db.run("UPDATE timers SET due_at = ? WHERE kind = 'mini_mute'", (time.time() - 1,))
        await self.bot.timers.run_due()
        latest = (await self.cases())[0]
        self.assertEqual((latest["action"], latest["moderator_id"]), ("unminimute", None))

    async def test_a_case_that_fails_doesnt_undo_the_mute(self):
        self.moderation.record_case = mock.AsyncMock(side_effect=RuntimeError("database locked"))
        self.errors.setLevel(100)
        await self.slash("mini_mute", self.admin_id, user=self.guild.get_member(self.member_id))
        self.errors.setLevel(40)
        self.assertIsNotNone(await self.db.one("SELECT * FROM mini_mutes"))
        self.assertIn("is muted", self.last_answer())


class LoadedAloneTest(VoiceTest):
    async def test_a_mini_mute_works_with_no_moderation_module(self):
        self.assertIsNone(self.bot.module("moderation"))
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id))
        self.assertIsNotNone(await self.db.one("SELECT * FROM mini_mutes"))
        self.assert_no_errors()


class WithReportsTest(Harness):
    modules = [Reports, Voice]

    async def test_the_reports_moderator_role_counts_here_too(self):
        role = self.add_role("Staff", 3)
        staff = self.add_member("staffer", role["id"])
        self.bot.logs = FakeLogSink()
        await self.bot.settings.save(self.guild_id, "reports", {"moderator_role": int(role["id"])})
        voice = self.bot.module("voice")
        self.assertTrue(voice.is_moderator(self.guild.get_member(staff)))
        self.assertFalse(voice.is_moderator(self.guild.get_member(self.member_id)))


class DashboardTest(VoiceTest):
    async def test_settings_save_and_are_checked(self):
        client = await self.client()
        response = await client.put(self.path("/settings/voice"), json={"min_messages": 250, "temp_minutes": 15, "approval_channel": self.approvals["id"]})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual((body["min_messages"], body["temp_minutes"], body["approval_channel"]), (250, 15, self.approvals["id"]))
        for bad in ({"temp_minutes": 0}, {"temp_minutes": 99999}, {"mini_mute_minutes": 1441}, {"mini_mute_votes_required": 11}, {"min_days": -1}, {"access_role": "nope"}, {"nonsense": 1}):
            self.assertEqual((await client.put(self.path("/settings/voice"), json=bad)).status, 400, bad)

    async def test_every_page_endpoint_needs_full_access(self):
        client = await self.client(access="staff")
        for path in ("/voice/actions", "/voice/access", "/voice/mini-mutes", "/voice/reports"):
            self.assertEqual((await client.get(self.path(path))).status, 403, path)
        self.assertEqual((await client.post(self.path("/voice/button"), json={"channel_id": self.text["id"]})).status, 403)
        self.assertEqual((await client.post(self.path(f"/voice/actions/{self.member_id}/release"))).status, 403)
        self.assertEqual((await client.post(self.path(f"/voice/access/{self.member_id}/grant"))).status, 403)

    async def test_actions_list_and_release(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        self.watch_member_edits()
        client = await self.client()
        rows = await (await client.get(self.path("/voice/actions"))).json()
        self.assertEqual((rows[0]["action"], rows[0]["name"], rows[0]["moderator_name"]), (MUTE, "member", "staffer"))
        response = await client.post(self.path(f"/voice/actions/{self.member_id}/release"))
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["cleared_now"])
        self.assertEqual(self.edits_of(self.member_id)[-1]["mute"], False)
        self.assertEqual(await self.actions(self.member_id), set())
        self.assertEqual((await self.bot.activity_log.recent(self.guild_id))[0]["action"], "voice.release")
        self.assertEqual((await client.post(self.path(f"/voice/actions/{self.member_id}/release"))).status, 404)

    async def test_release_when_they_left_voice_waits(self):
        await self.join(self.member_id)
        await self.slash("vc_mute", self.staff_id, user=self.member(self.member_id))
        await self.leave(self.member_id)
        client = await self.client()
        response = await client.post(self.path(f"/voice/actions/{self.member_id}/release"))
        self.assertFalse((await response.json())["cleared_now"])
        self.assertLess((await self.db.one("SELECT expires_at FROM voice_actions"))["expires_at"], time.time())

    async def test_another_servers_row_cant_be_reached(self):
        await self.db.run("INSERT INTO voice_actions (guild_id, user_id, action, expires_at) VALUES (?, ?, ?, ?)", (self.guild_id + 1, self.member_id, MUTE, time.time() + 60))
        await self.db.run("INSERT INTO mini_mutes (guild_id, user_id, expires_at) VALUES (?, ?, ?)", (self.guild_id + 1, self.member_id, time.time() + 60))
        await self.db.run("INSERT INTO voice_access (guild_id, user_id, status) VALUES (?, ?, 'approved')", (self.guild_id + 1, self.member_id))
        client = await self.client()
        self.assertEqual(await (await client.get(self.path("/voice/actions"))).json(), [])
        self.assertEqual((await client.post(self.path(f"/voice/actions/{self.member_id}/release"))).status, 404)
        self.assertEqual((await (await client.get(self.path("/voice/mini-mutes"))).json())["mutes"], [])
        self.assertEqual((await client.post(self.path(f"/voice/mini-mutes/{self.member_id}/lift"))).status, 404)
        self.assertEqual((await (await client.get(self.path("/voice/access"))).json())["total"], 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM voice_actions WHERE guild_id = ?", (self.guild_id + 1,)), 1)

    async def test_access_list_filters_and_pages(self):
        for name, status in (("alpha", "pending"), ("beta", "approved"), ("gamma", "denied")):
            await self.voice_module._set_access(self.guild_id, new_id(), name, status, self.staff_id, "staffer")
        client = await self.client()
        everything = await (await client.get(self.path("/voice/access"))).json()
        self.assertEqual(everything["total"], 3)
        one = await (await client.get(self.path("/voice/access?status=approved"))).json()
        self.assertEqual([r["user_name"] for r in one["rows"]], ["beta"])
        named = await (await client.get(self.path("/voice/access?member=gam"))).json()
        self.assertEqual([r["status"] for r in named["rows"]], ["denied"])
        paged = await (await client.get(self.path("/voice/access?size=2&page=2"))).json()
        self.assertEqual(len(paged["rows"]), 1)
        self.assertEqual((await client.get(self.path("/voice/access?status=weird"))).status, 400)
        self.assertEqual((await client.get(self.path("/voice/access?page=x"))).status, 400)

    async def test_grant_and_revoke_from_the_dashboard(self):
        client = await self.client()
        response = await client.post(self.path(f"/voice/access/{self.member_id}/grant"))
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.role_calls("PUT")), 1)
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access"), "approved")
        await self.join(self.member_id)
        self.watch_member_edits()
        response = await client.post(self.path(f"/voice/access/{self.member_id}/revoke"), json={"reason": "Abuse"})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.role_calls("DELETE")), 1)
        self.assertIsNone(self.edits_of(self.member_id)[-1]["channel_id"], "they come out of voice too")
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access"), "revoked")
        self.assertEqual(self.logged(self.approvals)[-1]["fields"][-1]["value"], "Abuse")
        self.assertEqual((await client.post(self.path(f"/voice/access/{self.member_id}/revoke"), json={"reason": "x" * 600})).status, 400)

    async def test_a_disconnect_that_fails_doesnt_lose_the_revoke(self):
        client = await self.client()
        await client.post(self.path(f"/voice/access/{self.member_id}/grant"))
        await self.join(self.member_id)
        restore = self.failing("PATCH", "/guilds/{guild_id}/members/{user_id}", 403)
        response = await client.post(self.path(f"/voice/access/{self.member_id}/revoke"), json={})
        restore()
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.db.scalar("SELECT status FROM voice_access"), "revoked")
        self.assertEqual(self.logged(self.approvals)[-1]["title"], "Voice Access Revoked")
        self.assertEqual((await self.bot.activity_log.recent(self.guild_id))[0]["action"], "voice.access.revoke")

    async def test_grant_needs_the_role_and_a_member(self):
        client = await self.client()
        self.assertEqual((await client.post(self.path(f"/voice/access/{new_id()}/grant"))).status, 404)
        await self.configure(access_role=None)
        self.assertEqual((await client.post(self.path(f"/voice/access/{self.member_id}/grant"))).status, 400)

    async def test_grant_refuses_a_role_with_power_or_above_the_bot(self):
        client = await self.client()
        strong = self.add_role("Power", 1, discord.Permissions(ban_members=True).value)
        await self.configure(access_role=int(strong["id"]))
        response = await client.post(self.path(f"/voice/access/{self.member_id}/grant"))
        self.assertEqual(response.status, 400)
        self.assertIn("moderation permissions", (await response.json())["error"])
        high = self.add_role("Too High", 50)
        await self.configure(access_role=int(high["id"]))
        response = await client.post(self.path(f"/voice/access/{self.member_id}/grant"))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.role_calls("PUT"), [])

    async def test_mini_mutes_and_votes_list_and_lift(self):
        await self.slash("mini_mute", self.staff_id, user=self.member(self.member_id), reason="Spam")
        await self.configure(mini_mute_votes_required=3)
        await self.slash("mini_mute", self.server_vet_id, user=self.member(self.mod_id), reason="Bait")
        client = await self.client()
        body = await (await client.get(self.path("/voice/mini-mutes"))).json()
        self.assertEqual((body["mutes"][0]["name"], body["mutes"][0]["reason"]), ("member", "Spam"))
        self.assertEqual((body["votes"][0]["name"], body["votes"][0]["approvals"], body["votes"][0]["required"], body["votes"][0]["status"]), ("mod", 1, 3, "open"))
        self.assertIn("/channels/", body["votes"][0]["url"])
        data = dict(self.member_json[self.member_id])
        data["roles"] = [*data["roles"], self.mute_role["id"]]  # Discord tells the bot about the role it just gave
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **data})
        response = await client.post(self.path(f"/voice/mini-mutes/{self.member_id}/lift"))
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.role_calls("DELETE")), 1)
        self.assertIsNone(await self.db.one("SELECT * FROM mini_mutes"))
        self.assertEqual((await self.bot.activity_log.recent(self.guild_id))[0]["action"], "voice.minimute.lift")

    async def test_vc_reports_list(self):
        await self.slash("vc_report", self.mod_id, user=self.member(self.member_id), reason="Loud")
        client = await self.client()
        body = await (await client.get(self.path("/voice/reports"))).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual((body["rows"][0]["target_name"], body["rows"][0]["reason"], body["rows"][0]["status"]), ("member", "Loud", "open"))

    async def test_post_button_from_the_dashboard_answers_after_a_restart(self):
        client = await self.client()
        response = await client.post(self.path("/voice/button"), json={"channel_id": self.text["id"]})
        self.assertEqual(response.status, 200)
        post = self.posts_in(self.text["id"])[-1]
        self.assertEqual(self.buttons(post)["Request Voice Access"]["custom_id"], ACCESS_REQUEST_ID)
        self.assertFalse(self.bot._connection._view_store._views.get(int(post["id"])), "the persistent view answers it, not a throwaway")
        await self.press(ACCESS_REQUEST_ID, post, self.member_id)
        self.assertTrue(self.last_answer())
        self.assert_no_errors()

    async def test_post_button_refuses_a_channel_from_elsewhere(self):
        client = await self.client()
        response = await client.post(self.path("/voice/button"), json={"channel_id": str(new_id())})
        self.assertEqual(response.status, 400)
        response = await client.post(self.path("/voice/button"), json={"channel_id": "nope"})
        self.assertEqual(response.status, 400)
