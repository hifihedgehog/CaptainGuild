"""What automod's commands and imports say and store: replies that tell the truth, switches for every rule, warn steps and the name rule."""
from __future__ import annotations

import json
from typing import Any

from captainguild.core.ops import DiscordOps
from captainguild.core.validate import BAN, CHANNEL_MESSAGE, DELETE, KICK, TEMPMUTE, WARN
from captainguild.modules.automod import settings_from_export
from captainguild.web.api import User
from captainguild.web.app import build_app
from aiohttp.test_utils import TestClient, TestServer
from tests.fakes import FakeChannel, FakeMember, FakePermissions, new_id
from tests.test_automod import AutomodBase


class Site(AutomodBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me

    async def site(self, member: Any) -> TestClient:
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(member.id, member.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client


class ImportReachTest(Site):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.manager = FakeMember(self.guild, "manager", self.guild.role("Manager", 4, "manage_guild", "view_channel"))
        outer = self

        class Hidden(FakeChannel):
            def permissions_for(self, member: Any) -> Any:
                return FakePermissions() if member is outer.manager else member.guild_permissions
        self.hidden = Hidden(self.guild, "hidden")

    async def post(self, path: str, code: dict) -> tuple[int, dict]:
        client = await self.site(self.manager)
        response = await client.post(self.path(path), json={"code": json.dumps(code)})
        return response.status, await response.json()

    async def test_automod_import_cant_name_a_hidden_channel(self):
        status, body = await self.post("/automod/import", {"log_channel": self.hidden.id})
        self.assertEqual(status, 400)
        self.assertIn("can't see", body["error"])
        self.assertNotEqual(self.settings("automod")["log_channel"], self.hidden.id)

    async def test_moderation_import_cant_name_a_hidden_channel(self):
        status, body = await self.post("/moderation/import", {"modlogs": {"channel_id": self.hidden.id}})
        self.assertEqual(status, 400)
        self.assertIn("can't see", body["error"])

    async def test_an_import_cant_hand_out_a_role_above_the_person(self):
        high = self.guild.role("High", 7)
        status, body = await self.post("/moderation/import", {"muterole": high.id})
        self.assertEqual(status, 400)
        self.assertIn("highest role", body["error"])

    async def test_an_import_with_what_they_can_reach_works(self):
        self.guild.default_role.permissions = FakePermissions("view_channel", "send_messages")
        status, _ = await self.post("/moderation/import", {"modlogs": {"logging_bitmask": 1}})
        self.assertEqual(status, 200)


class ImportReplacesTest(Site):
    async def test_what_the_export_leaves_out_goes_back_to_its_default(self):
        await self.rule("words", dry_run=True, entries=["old"], exempt_roles=[self.role_member.id], punishment={"actions": BAN, "seconds": 0, "purge_days": 0})
        await self.bot.settings.save(self.guild.id, "automod", {"general_dry_run": True, "log_channel": self.channel.id})
        client = await self.site(self.admin)
        response = await client.post(self.path("/automod/import"), json={"code": json.dumps({"bad_words": ["boat"]})})
        self.assertEqual(response.status, 200)
        cfg = self.settings("automod")
        self.assertFalse(cfg["general_dry_run"])
        self.assertIsNone(cfg["log_channel"])
        self.assertFalse(cfg["words"]["dry_run"])
        self.assertEqual(cfg["words"]["exempt_roles"], [])
        self.assertTrue(cfg["words"]["enabled"])
        self.assertEqual(cfg["words"]["entries"], [{"text": "boat", "kind": "substring"}])
        self.assertEqual(cfg["words"]["punishment"]["actions"], DELETE)

    async def test_the_mute_role_goes_through_the_same_checks(self):
        high = self.guild.role("High", 40)
        client = await self.site(self.admin)
        response = await client.post(self.path("/automod/import"), json={"code": json.dumps({"muterole": high.id})})
        self.assertEqual(response.status, 400)

    def test_the_warn_threshold_import_drops_bits_it_cant_use(self):
        form = settings_from_export({"warn_threshold": 3, "warn_punishment": [DELETE | WARN | CHANNEL_MESSAGE | KICK, 0, 0]}, set(), set())
        self.assertEqual(form["warns"]["punishment"]["actions"], KICK)


class RepliesTest(AutomodBase):
    async def reply(self, command: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = self.context(self.admin)
        await command.callback(self.auto, ctx, *args, **kwargs)
        return ctx

    async def test_a_threshold_with_no_punishment_says_so(self):
        ctx = await self.reply(self.auto.automod_threshold, 3)
        self.assertIn("No punishment is set", ctx.oks[0])

    async def test_a_threshold_with_one_doesnt(self):
        await self.rule("warns", punishment={"actions": KICK, "seconds": 0, "purge_days": 0})
        ctx = await self.reply(self.auto.automod_threshold, 3)
        self.assertNotIn("No punishment", ctx.oks[0])

    async def test_caps_zero_says_its_off(self):
        ctx = await self.reply(self.auto.automod_caps_limit, 0)
        self.assertIn("off", ctx.oks[0])
        self.assertNotIn("0%", ctx.oks[0])

    async def test_linkspam_off_says_what_stays_on(self):
        await self.rule("links", mode="whitelist")
        ctx = await self.reply(self.auto.automod_linkspam_off)
        self.assertIn("blacklist is empty", ctx.oks[0])

    async def test_the_threshold_refuses_what_needs_a_message(self):
        for words in ("delete", "message", "warn", "kick, delete"):
            ctx = await self.reply(self.auto.automod_warnpunish, punishments=words)
            self.assertTrue(ctx.fails, words)
        ctx = await self.reply(self.auto.automod_warnpunish, punishments="kick")
        self.assertEqual(ctx.fails, [])

    async def test_a_long_whitelist_fits_a_message(self):
        channels = [new_id() for _ in range(200)]
        await self.bot.settings.save(self.guild.id, "automod", {"allowed_channels": channels})
        ctx = await self.reply(self.auto.automod_whitelist)
        self.assertLessEqual(len(ctx.sent[0]["content"]), 2000)
        self.assertIn("more", ctx.sent[0]["content"])

    async def test_a_long_honeypot_list_fits_an_embed(self):
        await self.bot.settings.save(self.guild.id, "automod", {"honeypot": {"channels": [new_id() for _ in range(400)]}})
        ctx = await self.reply(self.auto.automod_honeypot)
        self.assertLessEqual(len(ctx.sent[0]["embed"].description), 4096)

    async def test_the_word_list_says_when_it_was_cut(self):
        await self.rule("words", entries=[f"word{index}" for index in range(800)])
        ctx = self.context(self.admin)
        ctx.interaction = object()
        await self.auto.automod_censor_display.callback(self.auto, ctx)
        self.assertLessEqual(len(ctx.sent[0]["content"]), 2000)
        self.assertIn("more", ctx.sent[0]["content"])

    async def test_the_word_list_doesnt_go_public(self):
        await self.rule("words", entries=["secret"])
        ctx = self.context(self.admin)
        self.guild.default_role.permissions = FakePermissions("view_channel")
        await self.auto.automod_censor_display.callback(self.auto, ctx)
        self.assertEqual(ctx.sent, [])
        self.assertIn("secret", self.admin.dms[0]["content"])


class SwitchesTest(AutomodBase):
    async def run_command(self, name: str) -> Any:
        ctx = self.context(self.admin)
        command = getattr(self.auto, name)
        await command.callback(self.auto, ctx)
        return ctx

    async def test_each_rule_turns_on_and_off(self):
        for rule, prefix in (("invites", "invitespam"), ("words", "censor"), ("links", "linkspam"), ("honeypot", "honeypot"), ("caps", "caps")):
            await self.run_command(f"automod_{prefix}_enable")
            self.assertTrue(self.settings("automod")[rule]["enabled"], rule)
            await self.run_command(f"automod_{prefix}_disable")
            self.assertFalse(self.settings("automod")[rule]["enabled"], rule)

    async def test_dry_run_flips(self):
        for rule, prefix in (("invites", "invitespam"), ("words", "censor"), ("links", "linkspam"), ("honeypot", "honeypot"), ("caps", "caps"), ("spam", "slowmode"), ("attachments", "attachmentspam"), ("mentions", "mentionspam")):
            ctx = await self.run_command(f"automod_{prefix}_dryrun")
            self.assertTrue(self.settings("automod")[rule]["dry_run"], rule)
            self.assertIn("Dry run", ctx.oks[0])
            await self.run_command(f"automod_{prefix}_dryrun")
            self.assertFalse(self.settings("automod")[rule]["dry_run"], rule)


class WarnStepsTest(AutomodBase):
    async def steps(self, *pairs: tuple[int, int, int]) -> None:
        await self.rule("warns", threshold=pairs[0][0], punishment={"actions": pairs[0][1], "seconds": pairs[0][2], "purge_days": 0}, steps=[
            {"threshold": limit, "punishment": {"actions": actions, "seconds": seconds, "purge_days": 0}} for limit, actions, seconds in pairs[1:]
        ])

    async def warn(self, times: int) -> list[str]:
        for _ in range(times):
            await self.mods.record_case(self.guild, "warn", self.member, self.admin)
        return [row["action"] for row in await self.cases()]

    async def test_three_mutes_and_five_bans(self):
        await self.steps((3, TEMPMUTE, 3600), (5, BAN, 0))
        self.assertNotIn("mute", await self.warn(3))
        self.assertIn("mute", await self.warn(1))
        self.assertNotIn("ban", await self.cases() and [row["action"] for row in await self.cases()])
        self.assertIn("ban", await self.warn(2))

    async def test_a_step_can_stand_alone(self):
        await self.rule("warns", threshold=0, steps=[{"threshold": 1, "punishment": {"actions": KICK, "seconds": 0, "purge_days": 0}}])
        self.assertIn("kick", await self.warn(2))

    async def test_a_step_refuses_what_needs_a_message(self):
        ctx = self.context(self.admin)
        await self.auto.automod_warnstep_add.callback(self.auto, ctx, 5, punishments="delete")
        self.assertTrue(ctx.fails)
        self.assertEqual(self.settings("automod")["warns"]["steps"], [])

    async def test_commands_add_and_remove(self):
        await self.auto.automod_warnstep_add.callback(self.auto, self.context(self.admin), 5, punishments="ban")
        self.assertEqual(self.settings("automod")["warns"]["steps"][0]["threshold"], 5)
        await self.auto.automod_warnstep_add.callback(self.auto, self.context(self.admin), 5, punishments="kick")
        self.assertEqual(len(self.settings("automod")["warns"]["steps"]), 1)
        self.assertEqual(self.settings("automod")["warns"]["steps"][0]["punishment"]["actions"], KICK)
        await self.auto.automod_warnstep_remove.callback(self.auto, self.context(self.admin), 5)
        self.assertEqual(self.settings("automod")["warns"]["steps"], [])


class NameRuleTest(AutomodBase):
    async def on(self, **fields: Any) -> None:
        await self.rule("words", entries=["boat"])
        await self.rule("names", punishment={"actions": KICK, "seconds": 0, "purge_days": 0}, **fields)

    async def test_a_bad_name_on_join_draws_the_punishment(self):
        await self.on()
        joiner = FakeMember(self.guild, "boatman", self.role_member)
        await self.auto.on_member_join(joiner)
        self.assertEqual((await self.cases())[0]["action"], "kick")

    async def test_a_nickname_change_counts(self):
        await self.on()
        before = type("B", (), {"nick": None, "name": "fine", "global_name": "fine"})()
        self.member.nick = "boat"
        await self.auto.on_member_update(before, self.member)
        self.assertEqual((await self.cases())[0]["action"], "kick")

    async def test_nothing_about_the_name_changing_costs_nothing(self):
        await self.on()
        self.member.nick = "boat"
        before = type("B", (), {"nick": "boat", "name": self.member.name, "global_name": self.member.global_name})()
        await self.auto.on_member_update(before, self.member)
        self.assertEqual(await self.cases(), [])

    async def test_a_clean_name_passes(self):
        await self.on()
        await self.auto.on_member_join(FakeMember(self.guild, "fine", self.role_member))
        self.assertEqual(await self.cases(), [])

    async def test_staff_and_allowed_roles_pass(self):
        await self.on()
        await self.bot.settings.save(self.guild.id, "automod", {"allowed_roles": [self.role_member.id]})
        await self.auto.on_member_join(FakeMember(self.guild, "boat", self.role_member))
        await self.auto.on_member_join(FakeMember(self.guild, "boat2", self.role_admin))
        self.assertEqual(await self.cases(), [])

    async def test_dry_run_only_logs(self):
        await self.on(dry_run=True)
        await self.auto.on_member_join(FakeMember(self.guild, "boat", self.role_member))
        self.assertEqual(await self.cases(), [])
        self.assertIn("dry run", self.logged()[0]["title"])

    async def test_a_global_name_change_counts(self):
        await self.on()
        before = type("B", (), {"name": self.member.name, "global_name": "fine"})()
        self.member.global_name = "boat"
        after = type("A", (), {"id": self.member.id, "name": self.member.name, "global_name": "boat"})()
        await self.auto.on_user_update(before, after)
        self.assertEqual((await self.cases())[0]["action"], "kick")
