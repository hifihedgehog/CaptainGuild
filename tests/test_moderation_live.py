"""Moderation and automod through the real discord.py library with only the network replaced.

tests/test_moderation.py calls commands directly on stand-ins, which is quick. This file checks what stand-ins can't: that a typed
`!mute <@member> 20h45m being rude` parses the way staff expect, that slash options arrive under the names the code uses, and that the
HTTP calls the library makes are the ones Discord wants.
"""
from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any
from unittest import mock

import discord

from captainguild.modules import moderation as moderation_module
from captainguild.modules.automod import Automod
from captainguild.modules.autopurge import AutoPurge
from captainguild.modules.moderation import Moderation
from tests.gateway import GatewayTest, iso, new_id, user_json


class LiveBase(GatewayTest):
    modules = [Moderation, Automod, AutoPurge]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        patcher = mock.patch.object(moderation_module, "AUDIT_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        await self.bot.settings.save(self.guild_id, "moderation", {"modlog_channel": int(self.log_channel["id"]), "mute_role": int(self.role_muted["id"])})
        self.mods = next(m for m in self.bot.modules if isinstance(m, Moderation))
        self.auto = next(m for m in self.bot.modules if isinstance(m, Automod))
        # Query parameters don't reach the harness's call list, and a ban's purge length is one.
        self.params: dict[str, Any] = {}
        real = self.net.http

        async def spy(route: Any, **kwargs: Any) -> Any:
            self.params[route.path] = kwargs.get("params")
            return await real(route, **kwargs)
        self.bot.http.request = spy

    async def typed(self, content: str, *, user_id: int | None = None) -> None:
        self.dispatch_message(user_id=user_id or self.owner_id, content=content)
        await self.settle()
        self.assert_no_errors(content)

    def slash(self, name: str, options: list[dict], *, path: tuple = (), resolved: dict | None = None, user_id: int | None = None) -> None:
        for part in reversed(path):
            options = [{"name": part, "type": 1 if part == path[-1] else 2, "options": options}]
        data = {"id": str(new_id()), "name": name, "type": 1, "options": options, "resolved": resolved or {}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id or self.owner_id))

    def member_option(self, name: str = "member", user_id: int | None = None) -> tuple[dict, dict]:
        target = user_id or self.member_id
        member = self.member_json[target]
        resolved = {"users": {str(target): member["user"]}, "members": {str(target): {**{k: v for k, v in member.items() if k != "user"}, "permissions": "0"}}}
        return {"name": name, "type": 6, "value": str(target)}, resolved

    def role_calls(self, method: str) -> list[tuple]:
        return [call for call in self.net.calls if call[0] == method and "/roles/{role_id}" in call[1]]

    async def cases(self) -> list[dict]:
        return await self.db.all("SELECT * FROM cases WHERE guild_id = ? ORDER BY number", (self.guild_id,))

    def replies(self) -> list[str]:
        """Everything the bot answered through an interaction or a plain message."""
        out = []
        for message in self.net.posted:
            if message.get("content"):
                out.append(message["content"])
        return out


class TextCommandTest(LiveBase):
    async def test_mute_with_a_duration_at_the_front(self):
        await self.typed(f"!mute <@{self.member_id}> 20h45m being rude")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["seconds"], row["reason"], row["user_id"]), ("mute", 20 * 3600 + 45 * 60, "being rude", self.member_id))
        self.assertTrue(self.role_calls("PUT"))

    async def test_mute_with_only_a_reason(self):
        await self.typed(f"!mute <@{self.member_id}> being rude")
        row = (await self.cases())[0]
        self.assertEqual((row["seconds"], row["reason"]), (None, "being rude"))

    async def test_mute_with_a_two_word_duration(self):
        await self.typed(f"!mute <@{self.member_id}> 2 days being rude")
        row = (await self.cases())[0]
        self.assertEqual((row["seconds"], row["reason"]), (172800, "being rude"))

    async def test_warn_by_mention_and_by_id(self):
        await self.typed(f"!warn <@{self.member_id}> first warning")
        await self.typed(f"!warn {self.member_id} second warning")
        self.assertEqual([row["reason"] for row in await self.cases()], ["first warning", "second warning"])

    async def test_ban_with_a_reason(self):
        await self.typed(f"!ban <@{self.member_id}> alt account")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["reason"]), ("ban", "alt account"))
        self.assertEqual(self.params["/guilds/{guild_id}/bans/{user_id}"]["delete_message_seconds"], 2 * 86400)

    async def test_ban_with_days_then_a_reason(self):
        await self.typed(f"!ban <@{self.member_id}> 7 alt account")
        row = (await self.cases())[0]
        self.assertEqual(row["reason"], "alt account")
        self.assertEqual(self.params["/guilds/{guild_id}/bans/{user_id}"]["delete_message_seconds"], 7 * 86400)

    async def test_a_reason_that_reads_like_a_length_stays_a_reason(self):
        await self.typed(f"!ban <@{self.member_id}> 3d printer scam")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["seconds"], row["reason"]), ("ban", None, "3d printer scam"))
        self.assertIsNone(await self.bot.timers.get_key("moderation.unban", f"{self.guild_id}:{self.member_id}"))
        self.assertEqual(self.params["/guilds/{guild_id}/bans/{user_id}"]["delete_message_seconds"], 2 * 86400)

    async def test_a_bare_number_on_a_ban_is_a_day_count(self):
        await self.typed(f"!ban <@{self.member_id}> 3 days old account")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["seconds"], row["reason"]), ("ban", None, "days old account"))
        self.assertEqual(self.params["/guilds/{guild_id}/bans/{user_id}"]["delete_message_seconds"], 3 * 86400)

    async def test_ban_someone_who_is_not_in_the_server(self):
        outsider = new_id()
        self.net.users = {outsider: user_json(outsider, "outsider")}
        await self.typed(f"!massban {outsider} raid")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["user_id"], row["reason"]), ("ban", outsider, "raid"))
        self.assertTrue(any(c[1] == "/guilds/{guild_id}/bulk-ban" for c in self.net.calls))

    async def test_tempban_with_days_and_duration(self):
        await self.typed(f"!tempban <@{self.member_id}> 1 2d cooling off")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["seconds"], row["reason"]), ("tempban", 2 * 86400, "cooling off"))

    async def test_timeout_sends_the_end_time(self):
        await self.typed(f"!timeout <@{self.member_id}> 2h calm down")
        call = next(c for c in self.net.calls if c[0] == "PATCH" and c[1] == "/guilds/{guild_id}/members/{user_id}")
        self.assertIn("communication_disabled_until", call[2])
        self.assertEqual((await self.cases())[0]["seconds"], 7200)

    async def test_the_reason_command_staff_already_type(self):
        await self.typed(f"!warn <@{self.member_id}>")
        await self.typed("!reason 1 changed my mind")
        self.assertEqual((await self.cases())[0]["reason"], "changed my mind")

    async def test_purge_text_form_reads_a_count(self):
        await self.typed("!purge 5")
        self.assertTrue(any(c[1] == "/channels/{channel_id}/messages" and c[0] == "GET" for c in self.net.calls))

    async def test_purge_cannot_delete_the_invocation_twice(self):
        await self.typed("!purge all 5")
        await self.typed(f"!purge user <@{self.member_id}> 5")
        await self.typed("!purge contains hello 5")

    async def test_a_moderator_with_a_bypass_role_passes_the_command_check(self):
        await self.bot.settings.save(self.guild_id, "commands", {"overwrites": {"warn": {"bypass_roles": [int(self.role_mod["id"])]}}})
        await self.typed(f"!warn <@{self.member_id}> from a mod", user_id=self.mod_id)
        self.assertEqual((await self.cases())[0]["moderator_id"], self.mod_id)

    async def test_a_plain_member_is_refused_by_the_command_check(self):
        await self.typed(f"!warn <@{self.mod_id}> from nobody", user_id=self.member_id)
        self.assertEqual(await self.cases(), [])

    async def test_automod_group_alias_and_subgroups_work_in_text(self):
        await self.typed("!am")
        await self.typed("!automod linkspam")
        await self.typed("!automod censor")
        await self.typed("!automod honeypot")
        await self.typed("!automod slowmode set 5 25")
        self.assertEqual(self.bot.settings.get(self.guild_id, "automod")["spam"]["rate"], [5, 25])
        await self.typed("!automod censor words add boat regex:x+y")
        self.assertEqual([entry["kind"] for entry in self.bot.settings.get(self.guild_id, "automod")["words"]["entries"]], ["substring", "regex"])

    async def test_notes_top_level_and_group(self):
        await self.typed(f"!setnote <@{self.member_id}> typed the old way")
        await self.typed(f"!notes setnote <@{self.member_id}> typed the new way")
        await self.typed(f"!notes <@{self.member_id}>")
        self.assertEqual(len(await self.db.all("SELECT * FROM notes")), 2)


class EveryActionTest(LiveBase):
    """Each action through the real library, so a wrong keyword or attribute shows up as a logged error."""

    async def test_hardmute_then_unmute(self):
        await self.typed(f"!hardmute <@{self.member_id}> 2h heated")
        call = next(c for c in self.net.calls if c[0] == "PATCH" and c[1] == "/guilds/{guild_id}/members/{user_id}")
        self.assertIn(self.role_muted["id"], [str(r) for r in call[2]["roles"]])
        self.assertNotIn(self.role_member["id"], [str(r) for r in call[2]["roles"]])
        row = await self.db.one("SELECT * FROM active_mutes")
        self.assertEqual(json.loads(row["removed_roles"]), [int(self.role_member["id"])])
        self.net.calls.clear()
        await self.typed(f"!unmute <@{self.member_id}> done")
        self.assertEqual((await self.cases())[-1]["action"], "unmute")
        self.assertIsNone(await self.db.one("SELECT * FROM active_mutes"))

    async def test_mute_ends_through_the_timer(self):
        await self.typed(f"!mute <@{self.member_id}> 10m heated")
        # Discord echoes the new role back over the gateway, and that must not make a second case.
        member = self.member_json[self.member_id]
        self.bot._connection.parse_guild_member_update({"guild_id": str(self.guild_id), "user": member["user"], "roles": [*member["roles"], self.role_muted["id"]], "joined_at": member["joined_at"], "nick": None, "deaf": False, "mute": False, "flags": 0, "avatar": None, "premium_since": None, "pending": False, "communication_disabled_until": None})
        await self.settle()
        self.assertEqual(len(await self.cases()), 1)
        self.net.calls.clear()
        await self.bot.timers.run_due(10 ** 11)
        await self.settle()
        self.assert_no_errors("mute timer")
        self.assertTrue(self.role_calls("DELETE"))
        self.assertEqual((await self.cases())[-1]["source"], "timer")

    async def test_kick_softban_and_unban(self):
        await self.typed(f"!kick <@{self.member_id}> out")
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/guilds/{guild_id}/members/{user_id}" for c in self.net.calls))
        self.net.calls.clear()
        await self.typed(f"!softban <@{self.mod_id}> 1 clean up")
        self.assertTrue(any(c[0] == "PUT" and c[1] == "/guilds/{guild_id}/bans/{user_id}" for c in self.net.calls))
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/guilds/{guild_id}/bans/{user_id}" for c in self.net.calls))
        self.assertEqual((await self.cases())[-1]["action"], "softban")

    async def test_tempban_ends_through_the_timer(self):
        await self.typed(f"!tempban <@{self.member_id}> 2h cool off")
        self.net.calls.clear()
        await self.bot.timers.run_due(10 ** 11)
        await self.settle()
        self.assert_no_errors("tempban timer")
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/guilds/{guild_id}/bans/{user_id}" for c in self.net.calls))
        self.assertEqual((await self.cases())[-1]["action"], "unban")

    async def test_timeout_and_removing_it(self):
        await self.typed(f"!timeout <@{self.member_id}> 2h calm")
        # The harness's member comes back from Discord through a gateway update.
        member = self.member_json[self.member_id]
        until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).isoformat()
        self.bot._connection.parse_guild_member_update({"guild_id": str(self.guild_id), "user": member["user"], "roles": member["roles"], "joined_at": member["joined_at"], "nick": None, "deaf": False, "mute": False, "flags": 0, "avatar": None, "premium_since": None, "pending": False, "communication_disabled_until": until})
        await self.settle()
        self.assertEqual(len(await self.cases()), 1)
        self.net.calls.clear()
        await self.typed(f"!untimeout <@{self.member_id}> sorted")
        self.assertEqual((await self.cases())[-1]["action"], "untimeout")
        call = next(c for c in self.net.calls if c[0] == "PATCH")
        self.assertIsNone(call[2]["communication_disabled_until"])

    async def test_unban_by_id(self):
        await self.typed(f"!unban {self.member_id}")
        # The harness says nobody is banned, and the reply says so instead of an error.
        self.assertEqual(await self.cases(), [])

    async def test_warns_and_clearing_them(self):
        await self.typed(f"!warn <@{self.member_id}> one")
        await self.typed(f"!warn <@{self.member_id}> two")
        await self.typed(f"!warns <@{self.member_id}>")
        await self.typed("!warns")
        await self.typed("!removewarning 1")
        await self.typed(f"!clearwarn <@{self.member_id}>")
        self.assertEqual((await self.mods.history(self.guild_id, self.member_id))["counts"]["warn"], 0)

    async def test_modlog_commands(self):
        await self.typed(f"!warn <@{self.member_id}> one")
        await self.typed(f"!modlog <@{self.member_id}>")
        await self.typed(f"!modlog from <@{self.member_id}>")
        await self.typed("!modlog highscores")
        await self.typed(f"!modlog export <@{self.member_id}>")
        await self.typed("!modlog reason 1 a better one")
        self.assertEqual((await self.cases())[0]["reason"], "a better one")
        await self.typed("!modlog create cases")
        self.assertTrue(any(c[0] == "POST" and c[1] == "/guilds/{guild_id}/channels" for c in self.net.calls))
        await self.typed(f"!modlog set <#{self.text['id']}>")
        self.assertEqual(self.bot.settings.get(self.guild_id, "moderation")["modlog_channel"], int(self.text["id"]))
        await self.typed("!modlog clear")
        self.assertIsNone(self.bot.settings.get(self.guild_id, "moderation")["modlog_channel"])

    async def test_mute_role_commands(self):
        await self.typed("!muterole create Silenced")
        self.assertTrue(any(c[0] == "POST" and c[1] == "/guilds/{guild_id}/roles" for c in self.net.calls))
        self.assertTrue(any(c[0] == "PUT" and c[1] == "/channels/{channel_id}/permissions/{target}" for c in self.net.calls))
        await self.typed(f"!muterole set <@&{self.role_muted['id']}>")
        self.assertEqual(self.bot.settings.get(self.guild_id, "moderation")["mute_role"], int(self.role_muted["id"]))
        await self.typed("!muterole update")
        await self.typed("!muterole")

    async def test_lockdown_server_and_setup(self):
        self.guild.default_role._permissions = discord.Permissions(view_channel=True).value
        await self.typed("!lockdown server 1h raid")
        self.assertGreaterEqual(await self.db.scalar("SELECT COUNT(*) FROM lockdowns"), 1)
        await self.typed("!unlockdown server")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM lockdowns"), 0)
        await self.typed("!lockdown setup")
        await self.typed("!lockdown setup yes")

    async def test_lockdown_ends_through_the_timer(self):
        await self.typed("!lockdown channel 1h")
        await self.bot.timers.run_due(10 ** 11)
        await self.settle()
        self.assert_no_errors("lockdown timer")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM lockdowns"), 0)

    async def test_setnick_cleanup_and_reactions(self):
        await self.typed(f"!setnick <@{self.member_id}> Fresh Name")
        call = next(c for c in self.net.calls if c[0] == "PATCH" and c[1] == "/guilds/{guild_id}/members/{user_id}")
        self.assertEqual(call[2]["nick"], "Fresh Name")
        await self.typed("!cleanup 5")
        await self.typed("!purge reactions 5")
        await self.typed("!purge bot 5 !")

    async def test_automod_commands_in_text(self):
        for command in (
            f"!automod log <#{self.log_channel['id']}>", f"!automod drama <#{self.log_channel['id']}>", f"!automod media <#{self.text['id']}>", f"!automod unmedia <#{self.text['id']}>",
            f"!automod whitelist channels add <#{self.text['id']}>", f"!automod whitelist roles add <@&{self.role_mod['id']}>", "!automod deletefiles", "!automod threshold 3", "!automod warnpunish kick",
            "!automod slowmode punishment delete, tempmute 20m", "!automod slowmode disable", "!automod attachmentspam set 3 5", "!automod attachmentspam punishment mute", "!automod attachmentspam disable",
            "!automod mentionspam set 25 5", "!automod mentionspam punishment tempban 24h", "!automod mentionspam disable", "!automod linkspam rate 1 1", "!automod linkspam punishment delete", "!automod linkspam blacklist add reddit.com",
            "!automod linkspam whitelist add example.com", "!automod linkspam clear blacklist", "!automod linkspam block", "!automod linkspam off", "!automod linkspam norole", "!automod invitespam", "!automod invitespam punishment delete",
            "!automod invitespam norole", "!automod invitespam whitelist add discord.gg/abc", "!automod censor words add boat", "!automod censor display", "!automod censor punishment delete", "!automod censor clear", "!automod caps limit 80",
            "!automod caps punishment delete", f"!automod honeypot add <#{self.text['id']}>", "!automod honeypot server", f"!automod honeypot remove <#{self.text['id']}>", "!automod honeypot clear", "!automod honeypot punishment delete",
            "!automod whitelist",
        ):
            await self.typed(command)
        cfg = self.bot.settings.get(self.guild_id, "automod")
        self.assertEqual(cfg["log_channel"], int(self.log_channel["id"]))
        self.assertEqual(cfg["allowed_roles"], [int(self.role_mod["id"])])
        self.assertEqual(cfg["links"]["mode"], "no_role")
        self.assertEqual(cfg["mentions"]["punishment"]["seconds"], 86400)

    async def test_autopurge_commands_in_text(self):
        await self.typed(f"!autopurge set <#{self.text['id']}> 6h bots")
        await self.typed("!autopurge")
        await self.typed(f"!autopurge remove <#{self.text['id']}>")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM autopurge_channels"), 0)

    async def test_autopurge_run_through_the_timer(self):
        await self.typed(f"!autopurge set <#{self.text['id']}> 6h")
        purge = next(m for m in self.bot.modules if isinstance(m, AutoPurge))
        await self.bot.timers.run_due(10 ** 11)
        if purge._running:
            await asyncio.gather(*purge._running.values())
        await self.settle()
        self.assert_no_errors("autopurge timer")
        self.assertTrue(any(c[0] == "GET" and c[1] == "/channels/{channel_id}/messages" for c in self.net.calls))
        row = await self.db.one("SELECT * FROM autopurge_channels")
        self.assertEqual(row["last_deleted"], 0)

    async def test_unsafe_file_and_media_only_on_the_real_gateway(self):
        await self.bot.settings.save(self.guild_id, "automod", {"delete_files": True, "media_channels": [int(self.log_channel["id"])], "log_channel": int(self.log_channel["id"])})
        attachment = {"id": str(new_id()), "filename": "virus.exe", "size": 10, "url": "https://cdn.example/virus.exe", "proxy_url": "https://cdn.example/virus.exe"}
        self.dispatch_message(user_id=self.member_id, content="free stuff", attachments=[attachment])
        await self.settle()
        self.assert_no_errors("unsafe file")
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/channels/{channel_id}/messages/{message_id}" for c in self.net.calls))
        self.net.calls.clear()
        self.dispatch_message(user_id=self.member_id, content="just words", channel=self.log_channel)
        await self.settle()
        self.assert_no_errors("media only")
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/channels/{channel_id}/messages/{message_id}" for c in self.net.calls))

    async def test_a_custom_dm_goes_through_the_real_template_engine(self):
        message = {"content": "{offender}: {reason} by {moderator} for {duration}", "embeds": [{"title": "Note"}], "buttons": []}
        await self.bot.settings.save(self.guild_id, "moderation", {"mute": {"mode": 4, "message": message}})
        await self.typed(f"!mute <@{self.member_id}> 2h being rude")
        dm = next(m for m in self.net.posted if "being rude by" in (m.get("content") or ""))
        self.assertEqual(dm["content"], "member: being rude by owner for 2 hours")


class SlashHappyPathTest(LiveBase):
    """Each slash command with sensible values, so its success branch runs through the real library. Failing replies count as failures here."""

    async def call(self, command: str, /, *path: str, **values: Any) -> None:
        root = next(c for c in self.bot.tree.get_commands() if c.name == command).to_dict(self.bot.tree)
        node = root
        for part in path:
            node = next(o for o in node["options"] if o["name"] == part)
        options, resolved = [], {}
        for option in node.get("options", []):
            if option["name"] not in values:
                continue
            value, kind = values[option["name"]], option["type"]
            if kind == 6:
                person = self.member_json.get(value)
                if person:
                    resolved.setdefault("members", {})[str(value)] = {**{k: v for k, v in person.items() if k != "user"}, "permissions": "0"}
                resolved.setdefault("users", {})[str(value)] = person["user"] if person else user_json(value, "outsider")
                value = str(value)
            elif kind == 7:
                channel = value
                resolved.setdefault("channels", {})[channel["id"]] = {"id": channel["id"], "name": channel["name"], "type": channel["type"], "permissions": str(discord.Permissions.all().value), "parent_id": channel.get("parent_id"), "guild_id": str(self.guild_id), "nsfw": False, "position": channel.get("position", 0), "rate_limit_per_user": 0, "topic": None, "flags": 0, "last_message_id": None}
                value = channel["id"]
            elif kind == 8:
                resolved.setdefault("roles", {})[value["id"]] = value
                value = value["id"]
            options.append({"name": option["name"], "type": kind, "value": value})
        for index, part in enumerate(reversed(path)):
            options = [{"name": part, "type": 1 if index == 0 else 2, "options": options}]
        mark = len(self.net.posted)
        data = {"id": str(new_id()), "name": command, "type": 1, "options": options, "resolved": resolved}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.owner_id))
        await self.settle()
        label = " ".join((command, *path))
        self.assert_no_errors(f"/{label}")
        refused = [m["content"] for m in self.net.posted[mark:] if "\N{CROSS MARK}" in (m.get("content") or "")]
        self.assertEqual(refused, [], f"/{label}")

    def role_update(self, *role_ids: str, timed_out_until: str | None = None) -> None:
        member = self.member_json[self.member_id]
        self.bot._connection.parse_guild_member_update({"guild_id": str(self.guild_id), "user": member["user"], "roles": [*member["roles"], *role_ids], "joined_at": member["joined_at"], "nick": None, "deaf": False, "mute": False, "flags": 0, "avatar": None, "premium_since": None, "pending": False, "communication_disabled_until": timed_out_until})

    async def test_punishments(self):
        target = self.member_id
        await self.call("warn", member=target, reason="one")
        await self.call("warns", member=target)
        await self.call("warns")
        await self.call("removewarning", case=1)
        await self.call("warn", member=target, reason="two")
        await self.call("clearwarnings", member=target)
        await self.call("mute", member=target, duration="1h", reason="r")
        self.role_update(self.role_muted["id"])
        await self.settle()
        await self.call("unmute", member=target, reason="r")
        await self.call("hardmute", member=target, duration="1h", reason="r")
        self.role_update(self.role_muted["id"])
        await self.settle()
        await self.call("unmute", member=target)
        await self.call("timeout", member=target, duration="1h", reason="r")
        until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()
        self.role_update(timed_out_until=until)
        await self.settle()
        await self.call("untimeout", member=target, reason="r")
        await self.call("setnick", member=target, name="Fresh")
        await self.call("setnick", member=target)
        await self.call("kick", member=target, reason="r")
        rows = await self.cases()
        self.assertEqual([row["action"] for row in rows], ["warn", "warn", "mute", "unmute", "hardmute", "unmute", "timeout", "untimeout", "kick"])

    async def test_bans(self):
        await self.call("ban", user=self.member_id, days=2, reason="r")
        await self.call("softban", user=self.mod_id, days=1, reason="r")
        outsider = new_id()
        await self.call("tempban", user=outsider, days=1, duration="2h", reason="r")
        self.net.bans[outsider] = user_json(outsider, "outsider")
        await self.call("unban", user=str(outsider), reason="appeal")
        await self.call("unban", user="outsider", reason="by name")
        await self.call("massban", users=f"{new_id()} <@{new_id()}>", reason="raid")
        actions = [row["action"] for row in await self.cases()]
        self.assertEqual(actions, ["ban", "softban", "tempban", "unban", "unban", "ban", "ban"])
        self.assertEqual(await self.bot.timers.run_due(10 ** 11), 0)

    async def test_notes(self):
        await self.call("notes", "setnote", member=self.member_id, note="first")
        await self.call("notes", "view", member=self.member_id)
        await self.call("notes", "removenote", note_id=1)
        await self.call("notes", "setnote", member=self.member_id, note="second")
        await self.call("notes", "clearnotes", member=self.member_id)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM notes"), 0)

    async def test_modlog(self):
        await self.call("warn", member=self.member_id, reason="one")
        await self.call("modlog", "from", member=self.member_id)
        await self.call("modlog", "reason", case=1, reason="better")
        await self.call("modlog", "highscores")
        await self.call("modlog", "export", member=self.member_id)
        await self.call("modlog", "create", name="cases")
        await self.call("modlog", "set", channel=self.text)
        await self.call("modlog", "clear")

    async def test_mute_role(self):
        await self.call("muterole", "create", name="Silenced")
        await self.call("muterole", "set", role=self.role_muted)
        await self.call("muterole", "update")

    async def test_lockdown(self):
        self.guild.default_role._permissions = discord.Permissions(view_channel=True, send_messages=True).value
        await self.call("lockdown", "channel", channel=self.text, duration="1h", reason="raid")
        await self.call("unlockdown", "channel", channel=self.text)
        await self.call("lockdown", "channel", channel=self.voice)
        await self.call("unlockdown", "channel", channel=self.voice)
        await self.call("lockdown", "channel")
        await self.call("unlockdown", "channel")
        await self.call("lockdown", "server", duration="1h", reason="raid")
        await self.call("unlockdown", "server")
        talker = self.guild.get_role(int(self.role_member["id"]))
        talker._permissions = discord.Permissions(send_messages=True, attach_files=True).value
        await self.call("lockdown", "setup")
        self.assertFalse(any(c[0] == "PATCH" and c[1] == "/guilds/{guild_id}/roles/{role_id}" for c in self.net.calls))
        await self.call("lockdown", "setup", confirm=True)
        call = next(c for c in self.net.calls if c[0] == "PATCH" and c[1] == "/guilds/{guild_id}/roles/{role_id}")
        kept = discord.Permissions(int(call[2]["permissions"]))
        self.assertFalse(kept.send_messages)
        self.assertTrue(kept.attach_files)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM lockdowns"), 0)

    async def test_purge(self):
        for name, values in (
            ("all", {"count": 5}), ("bot", {"search": 5, "prefix": "!"}), ("contains", {"substring": "hello", "search": 5}), ("user", {"member": self.member_id, "search": 5}), ("embeds", {"count": 5}),
            ("emoji", {"count": 5}), ("files", {"count": 5}), ("images", {"count": 5}), ("links", {"count": 5}), ("mentions", {"count": 5}), ("human", {"count": 5}), ("reactions", {"count": 5}),
        ):
            await self.call("purge", name, **values)
        await self.call("cleanup", count=5)

    async def test_purge_deletes_what_is_there_and_spares_pins(self):
        self.net.keep_history = True
        spare = self.net.message_json(int(self.text["id"]), {"content": "pinned"})
        spare["pinned"] = True
        gone = self.net.message_json(int(self.text["id"]), {"content": "not pinned"})
        self.net.messages[int(spare["id"])] = spare
        self.net.messages[int(gone["id"])] = gone
        await self.call("purge", "all", count=5)
        self.assertIn(int(spare["id"]), self.net.messages)
        self.assertNotIn(int(gone["id"]), self.net.messages)

    async def test_purge_reactions_clears_what_is_there(self):
        self.net.keep_history = True
        marked = self.net.message_json(int(self.text["id"]), {"content": "x"})
        marked["reactions"] = [{"count": 3, "me": False, "me_burst": False, "burst_colors": [], "count_details": {"burst": 0, "normal": 3}, "emoji": {"id": None, "name": "\N{WHITE MEDIUM STAR}"}}]
        self.net.messages[int(marked["id"])] = marked
        await self.call("purge", "reactions", count=5)
        self.assertTrue(any(c[0] == "DELETE" and "/reactions" in c[1] for c in self.net.calls))

    async def test_automod_and_autopurge(self):
        await self.call("automod", "server")
        await self.call("automod", "whitelist", "channels", choice="add", channels=f"<#{self.text['id']}>")
        await self.call("automod", "whitelist", "roles", choice="add", roles=f"<@&{self.role_mod['id']}>")
        await self.call("automod", "censor", "words", choice="add", words="boat")
        await self.call("automod", "linkspam", "clear", which="whitelist")
        await self.call("automod", "invitespam", "whitelist", choice="add", invites="discord.gg/friend")
        await self.call("automod", "honeypot", "add", channels=f"<#{self.text['id']}>")
        await self.call("automod", "threshold", limit=3)
        await self.call("autopurge", "set", channel=self.text, interval="6h")
        await self.call("autopurge", "config")
        await self.call("autopurge", "remove", channel=self.text)
        await self.call("autopurge", "remove")


class SlashCommandTest(LiveBase):
    async def test_mute_with_options(self):
        option, resolved = self.member_option()
        self.slash("mute", [option, {"name": "duration", "type": 3, "value": "20h45m"}, {"name": "reason", "type": 3, "value": "3d of spam"}], resolved=resolved)
        await self.settle()
        self.assert_no_errors("/mute")
        row = (await self.cases())[0]
        self.assertEqual((row["seconds"], row["reason"]), (20 * 3600 + 45 * 60, "3d of spam"))

    async def test_ban_with_options(self):
        option, resolved = self.member_option("user")
        self.slash("ban", [option, {"name": "days", "type": 4, "value": 1}, {"name": "reason", "type": 3, "value": "spam"}], resolved=resolved)
        await self.settle()
        self.assert_no_errors("/ban")
        self.assertEqual(self.params["/guilds/{guild_id}/bans/{user_id}"]["delete_message_seconds"], 86400)

    async def test_automod_subgroup(self):
        self.slash("automod", [{"name": "rate", "type": 3, "value": "1 1"}], path=("linkspam", "rate"))
        await self.settle()
        self.assert_no_errors("/automod linkspam rate")
        self.assertEqual(self.bot.settings.get(self.guild_id, "automod")["links"]["rate"], [1, 1])

    async def test_automod_fallback_shows_settings(self):
        self.slash("automod", [], path=("server",))
        await self.settle()
        self.assert_no_errors("/automod server")
        self.slash("automod", [], path=("linkspam", "server"))
        await self.settle()
        self.assert_no_errors("/automod linkspam server")

    async def test_settings_only_groups_are_hidden_from_members_by_default(self):
        for name in ("automod", "autopurge", "muterole"):
            command = next(c for c in self.bot.tree.get_commands() if c.name == name)
            self.assertIsNotNone(command.to_dict(self.bot.tree).get("default_member_permissions"), name)
        for name in ("ban", "warn", "mute", "purge", "modlog"):
            command = next(c for c in self.bot.tree.get_commands() if c.name == name)
            self.assertIsNone(command.to_dict(self.bot.tree).get("default_member_permissions"), name)


class EventTest(LiveBase):
    def audit(self, action: discord.AuditLogAction, target_id: int, user_id: int, *, reason: str | None = None, changes: list | None = None) -> None:
        self.bot._connection.parse_guild_audit_log_entry_create({
            "guild_id": str(self.guild_id), "id": str(new_id()), "target_id": str(target_id), "user_id": str(user_id), "action_type": action.value, "changes": changes or [], "reason": reason,
        })

    async def test_a_ban_from_the_real_gateway_names_the_audit_user(self):
        outsider = new_id()
        self.audit(discord.AuditLogAction.ban, outsider, self.mod_id, reason="spam account")
        await self.settle()
        self.bot._connection.parse_guild_ban_add({"guild_id": str(self.guild_id), "user": user_json(outsider, "spammer")})
        await self.settle()
        self.assert_no_errors("ban event")
        row = (await self.cases())[0]
        self.assertEqual((row["user_name"], row["moderator_id"], row["moderator_name"], row["reason"]), ("spammer", self.mod_id, "mod", "spam account"))

    async def test_the_bots_own_ban_event_is_not_recorded_twice(self):
        await self.typed(f"!ban <@{self.member_id}> spam")
        self.bot._connection.parse_guild_ban_add({"guild_id": str(self.guild_id), "user": self.member_json[self.member_id]["user"]})
        await self.settle()
        self.assertEqual(len(await self.cases()), 1)

    async def test_mute_role_given_by_a_person_on_the_real_gateway(self):
        role_id = self.role_muted["id"]
        self.audit(discord.AuditLogAction.member_role_update, self.member_id, self.admin_id, reason="by hand", changes=[{"key": "$add", "new_value": [{"id": role_id, "name": "Muted"}]}])
        await self.settle()
        member = self.member_json[self.member_id]
        self.bot._connection.parse_guild_member_update({"guild_id": str(self.guild_id), "user": member["user"], "roles": [*member["roles"], role_id], "joined_at": member["joined_at"], "nick": None, "deaf": False, "mute": False, "flags": 0, "avatar": None, "premium_since": None, "pending": False, "communication_disabled_until": None})
        await self.settle()
        self.assert_no_errors("role event")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["moderator_id"], row["reason"]), ("mute", self.admin_id, "by hand"))

    async def test_timeout_by_a_person_on_the_real_gateway(self):
        until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()
        self.audit(discord.AuditLogAction.member_update, self.member_id, self.admin_id, reason="calm down", changes=[{"key": "communication_disabled_until", "new_value": until}])
        await self.settle()
        member = self.member_json[self.member_id]
        self.bot._connection.parse_guild_member_update({"guild_id": str(self.guild_id), "user": member["user"], "roles": member["roles"], "joined_at": member["joined_at"], "nick": None, "deaf": False, "mute": False, "flags": 0, "avatar": None, "premium_since": None, "pending": False, "communication_disabled_until": until})
        await self.settle()
        self.assert_no_errors("timeout event")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["moderator_id"], row["reason"]), ("timeout", self.admin_id, "calm down"))
        self.assertAlmostEqual(row["seconds"], 3600, delta=10)

    async def test_a_kick_on_the_real_gateway(self):
        self.audit(discord.AuditLogAction.kick, self.member_id, self.admin_id, reason="out")
        await self.settle()
        self.bot._connection.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.member_id]["user"]})
        await self.settle()
        self.assert_no_errors("kick event")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["moderator_id"]), ("kick", self.admin_id))

    async def test_a_message_edit_on_the_real_gateway(self):
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["boat"]}, "log_channel": int(self.log_channel["id"])})
        sent = self.dispatch_message(user_id=self.member_id, content="clean")
        await self.settle()
        self.bot._connection.parse_message_update({**sent, "content": "a boat", "edited_timestamp": iso()})
        await self.settle()
        self.assert_no_errors("edit event")
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/channels/{channel_id}/messages/{message_id}" for c in self.net.calls))

    async def test_a_new_channel_on_the_real_gateway_gets_the_mute_limits(self):
        channel_id = new_id()
        self.bot._connection.parse_channel_create({"id": str(channel_id), "guild_id": str(self.guild_id), "name": "brand-new", "type": 0, "position": 9, "permission_overwrites": [], "nsfw": False, "parent_id": None, "topic": None, "rate_limit_per_user": 0, "flags": 0, "last_message_id": None})
        await self.settle()
        self.assert_no_errors("channel create")
        call = next(c for c in self.net.calls if c[0] == "PUT" and c[1] == "/channels/{channel_id}/permissions/{target}")
        denied = discord.Permissions(int(call[2]["deny"]))
        self.assertTrue(denied.send_messages)
        self.assertTrue(denied.add_reactions)


class AutomodLiveTest(LiveBase):
    async def test_a_foreign_invite_on_the_real_gateway(self):
        await self.bot.settings.save(self.guild_id, "automod", {"invites": {"enabled": True, "punishment": {"actions": 1 | 2}}, "log_channel": int(self.log_channel["id"])})
        self.net.invites = {"abc": {"code": "abc", "guild": {"id": str(new_id()), "name": "Elsewhere", "features": [], "icon": None, "splash": None, "banner": None, "description": None, "verification_level": 0, "vanity_url_code": None, "nsfw_level": 0, "premium_subscription_count": 0}, "channel": {"id": str(new_id()), "name": "x", "type": 0}, "type": 0}}
        self.dispatch_message(user_id=self.member_id, content="join https://discord.gg/abc")
        await self.settle()
        self.assert_no_errors("invite")
        row = (await self.cases())[0]
        self.assertEqual((row["action"], row["source"]), ("warn", "automod"))

    async def test_an_invite_to_this_server_on_the_real_gateway(self):
        await self.bot.settings.save(self.guild_id, "automod", {"invites": {"enabled": True, "punishment": {"actions": 1 | 2}}})
        self.net.invites = {"home": {"code": "home", "guild": {"id": str(self.guild_id), "name": "Home", "features": [], "icon": None, "splash": None, "banner": None, "description": None, "verification_level": 0, "vanity_url_code": None, "nsfw_level": 0, "premium_subscription_count": 0}, "channel": {"id": self.text["id"], "name": "general", "type": 0}, "type": 0}}
        self.dispatch_message(user_id=self.member_id, content="https://discord.gg/home")
        await self.settle()
        self.assert_no_errors("invite")
        self.assertEqual(await self.cases(), [])

    async def test_drama_buttons_on_the_real_gateway(self):
        drama_id = self.log_channel["id"]
        await self.bot.settings.save(self.guild_id, "automod", {"drama_channel": int(drama_id), "words": {"enabled": True, "entries": ["boat"], "punishment": {"actions": 128}}})
        self.dispatch_message(user_id=self.member_id, content="a boat")
        await self.settle()
        self.assert_no_errors("incident")
        post = next(m for m in self.net.posted if m.get("components"))
        ids = [component["custom_id"] for row in post["components"] for component in row["components"]]
        self.assertEqual(ids, ["automod:drama:ban", "automod:drama:kick", "automod:drama:mute", "automod:drama:dismiss"])
        data = {"custom_id": "automod:drama:ban", "component_type": 2}
        self.bot._connection.parse_interaction_create(self.interaction_payload(3, data, user_id=self.owner_id, message=post))
        await self.settle()
        self.assert_no_errors("press ban")
        self.assertTrue(any(c[0] == "PUT" and c[1] == "/guilds/{guild_id}/bans/{user_id}" for c in self.net.calls))
        record = await self.db.one("SELECT * FROM drama_incidents")
        self.assertEqual(record["outcome"], "ban")
        edited = self.net.messages[int(post["id"])]
        self.assertTrue(all(component["disabled"] for row in edited["components"] for component in row["components"]))
        self.assertIn("banned them", edited["embeds"][0]["description"])

    async def test_autopurge_set_through_a_slash_command(self):
        channel = self.text
        resolved = {"channels": {channel["id"]: {"id": channel["id"], "name": channel["name"], "type": 0, "permissions": str(discord.Permissions.all().value), "parent_id": channel.get("parent_id"), "guild_id": str(self.guild_id), "nsfw": False, "position": 1, "rate_limit_per_user": 0, "topic": None, "flags": 0, "last_message_id": None}}}
        self.slash("autopurge", [{"name": "channel", "type": 7, "value": channel["id"]}, {"name": "interval", "type": 3, "value": "6h"}, {"name": "message_type", "type": 3, "value": "links"}], path=("set",), resolved=resolved)
        await self.settle()
        self.assert_no_errors("/autopurge set")
        row = await self.db.one("SELECT * FROM autopurge_channels")
        self.assertEqual((row["interval_seconds"], row["message_type"]), (21600, "links"))

    async def test_lockdown_sends_a_real_overwrite(self):
        await self.typed("!lockdown channel")
        call = next(c for c in self.net.calls if c[0] == "PUT" and c[1] == "/channels/{channel_id}/permissions/{target}")
        denied = discord.Permissions(int(call[2]["deny"]))
        self.assertTrue(denied.send_messages)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM lockdowns"), 1)
        self.net.calls.clear()
        await self.typed("!unlockdown channel")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM lockdowns"), 0)
