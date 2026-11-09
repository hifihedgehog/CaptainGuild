"""Statistics through the real library: slash commands, the buttons and menus under a card, the custom period form, right-click menus and
typed commands, with only the network replaced."""
from __future__ import annotations

import time
import types

import discord

from captainguild.core.perms import MODULE as COMMANDS_MODULE
from captainguild.core.shortcuts import SHORTCUTS, expand_line, named_command
from captainguild.core.stats import timeframe as tf
from captainguild.core.stats import words
from captainguild.modules.config import Config
from captainguild.modules.statistics import Statistics
from captainguild.modules.tags import Tags
from captainguild.modules.triggers import Triggers
from captainguild.modules.utilities import Utilities
from tests.gateway import GatewayTest, new_id


class StatisticsThroughTheLibrary(GatewayTest):
    modules = [Utilities, Statistics]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "statistics", {"enabled": True})
        await self.bot.sync_server_commands(self.guild_id)
        slot = tf.slot_of(time.time())
        rows = [(self.guild_id, slot - back, int(self.text["id"]), user, 0, 3) for back in range(0, 4 * 96, 3) for user in (self.member_id, self.admin_id)]
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)", rows)

    def slash(self, name: str, *, user_id: int, options: list | None = None, guild_command: bool = True, resolved: dict | None = None) -> None:
        data = {"id": str(new_id()), "name": name, "type": 1, "options": options or [], "resolved": resolved or {}}
        if guild_command:
            data["guild_id"] = str(self.guild_id)
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id))

    def press(self, message: dict, custom_id: str, *, user_id: int, values: list | None = None) -> None:
        data: dict = {"custom_id": custom_id, "component_type": 3 if values is not None else 2}
        if values is not None:
            data["values"] = values
        self.bot._connection.parse_interaction_create(self.interaction_payload(3, data, user_id=user_id, message=message))

    def components(self, message: dict) -> list[dict]:
        return [component for row in message.get("components") or [] for component in row.get("components") or []]

    def last_card(self) -> dict:
        found = [message for message in self.net.posted if any(str(component.get("custom_id", "")).startswith("cgst:") for component in self.components(message))]
        self.assertTrue(found, "no card was posted")
        return found[-1]

    def calls_since(self, mark: int) -> list[tuple]:
        return self.net.calls[mark:]

    async def test_a_slash_card_and_its_buttons(self):
        mark = len(self.net.calls)
        self.slash("server", user_id=self.member_id)
        await self.settle()
        self.assert_no_errors("/server")
        card = self.last_card()
        bodies = [body for _, path, body in self.calls_since(mark) if path == "/webhooks/{webhook_id}/{webhook_token}"]
        self.assertTrue(bodies and bodies[-1]["attachments"][0]["filename"] == "statistics.png")
        refresh = next(component["custom_id"] for component in self.components(card) if component["custom_id"].startswith("cgst:r:"))

        mark = len(self.net.calls)
        self.press(card, refresh, user_id=self.member_id)
        await self.settle()
        self.assert_no_errors("refresh by the asker")
        edits = [(method, path) for method, path, _ in self.calls_since(mark) if path.endswith("/messages/@original")]
        self.assertIn(("PATCH", "/webhooks/{webhook_id}/{webhook_token}/messages/@original"), edits)

        mark = len(self.net.calls)
        self.press(card, refresh, user_id=self.admin_id)
        await self.settle()
        self.assert_no_errors("refresh by someone else")
        followups = [body for method, path, body in self.calls_since(mark) if method == "POST" and path == "/webhooks/{webhook_id}/{webhook_token}"]
        self.assertTrue(followups and followups[-1].get("flags", 0) & 64, "their copy is only for them")

        period = next(component["custom_id"] for component in self.components(card) if component["custom_id"].startswith("cgst:p:"))
        mark = len(self.net.calls)
        self.press(card, period, user_id=self.member_id, values=["a"])
        await self.settle()
        self.assert_no_errors("picking all time")
        patched = [body for method, path, body in self.calls_since(mark) if path.endswith("/messages/@original") and method == "PATCH"]
        self.assertTrue(any(":a:" in component["custom_id"] for body in patched for row in body.get("components", []) for component in row["components"]))

    async def test_the_custom_period_form(self):
        self.slash("messages", user_id=self.member_id)
        await self.settle()
        card = self.last_card()
        period = next(component["custom_id"] for component in self.components(card) if component["custom_id"].startswith("cgst:p:"))
        modals = len(self.net.modals)
        self.press(card, period, user_id=self.member_id, values=["custom"])
        await self.settle()
        self.assert_no_errors("opening the form")
        self.assertEqual(len(self.net.modals), modals + 1)
        modal = self.net.modals[-1]
        rows = []
        for row in modal["components"]:
            field = row.get("component") or (row.get("components") or [None])[0]
            if row.get("type") == 18:
                rows.append({"type": 18, "id": row.get("id", 1), "component": {"type": 4, "custom_id": field["custom_id"], "value": "90"}})
            else:
                rows.append({"type": 1, "components": [{"type": 4, "custom_id": field["custom_id"], "value": "90"}]})
        mark = len(self.net.calls)
        self.bot._connection.parse_interaction_create(self.interaction_payload(5, {"custom_id": modal["custom_id"], "components": rows, "resolved": {}},
                                                                              user_id=self.member_id, message=card))
        await self.settle()
        self.assert_no_errors("submitting 90 days")
        patched = [body for method, path, body in self.calls_since(mark) if method == "PATCH" and path.endswith("/messages/@original")]
        self.assertTrue(any(":d90:" in component["custom_id"] for body in patched for row in body.get("components", []) for component in row["components"]))

    async def test_top_pages_through_the_menu_and_buttons(self):
        self.slash("top", user_id=self.member_id)
        await self.settle()
        card = self.last_card()
        menu = next(component["custom_id"] for component in self.components(card) if component["custom_id"].startswith("cgst:s:"))
        self.press(card, menu, user_id=self.member_id, values=["members_messages"])
        await self.settle()
        self.assert_no_errors("picking a page")

    async def test_the_member_menu(self):
        member = self.member_json[self.member_id]
        data = {"id": str(new_id()), "name": "Member Statistics", "type": 2, "target_id": str(self.member_id), "guild_id": str(self.guild_id),
                "resolved": {"users": {str(self.member_id): member["user"]}, "members": {str(self.member_id): {**{k: v for k, v in member.items() if k != "user"}, "permissions": "0"}}}}
        mark = len(self.net.calls)
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.admin_id))
        await self.settle()
        self.assert_no_errors("the member menu")
        self.assertTrue(any(path == "/webhooks/{webhook_id}/{webhook_token}" for _, path, _ in self.calls_since(mark)))

    async def test_typed_aliases_and_the_member_lookup_under_info(self):
        mark = len(self.net.calls)
        self.dispatch_message(user_id=self.member_id, content="!m")
        await self.settle()
        self.assert_no_errors("!m")
        self.assertTrue(any(method == "POST" and path == "/channels/{channel_id}/messages" and body.get("attachments") for method, path, body in self.calls_since(mark)))
        mark = len(self.net.calls)
        self.dispatch_message(user_id=self.member_id, content=f"!info <@{self.admin_id}>")
        await self.settle()
        self.assert_no_errors("!info @admin")
        posted = [body for method, path, body in self.calls_since(mark) if method == "POST" and path == "/channels/{channel_id}/messages"]
        self.assertTrue(posted and posted[-1].get("embeds"), "typed info with a member is still the member lookup")
        self.assertFalse(posted[-1].get("attachments"))
        mark = len(self.net.calls)
        self.dispatch_message(user_id=self.member_id, content="!info id")
        await self.settle()
        posted = [body for method, path, body in self.calls_since(mark) if method == "POST" and path == "/channels/{channel_id}/messages"]
        self.assertIn(str(self.guild_id), posted[-1]["content"])

    async def test_switched_off_the_shared_subcommands_say_so_and_personal_ones_still_work(self):
        await self.bot.settings.save(self.guild_id, "statistics", {"enabled": False})
        options = [{"name": "message", "type": 1, "options": []}]
        mark = len(self.net.calls)
        self.slash("stats", user_id=self.member_id, options=options, guild_command=False)
        await self.settle()
        self.assert_no_errors("/stats message while off")
        said = [body for _, path, body in self.calls_since(mark) if "/callback" in path or path.startswith("/webhooks")]
        self.assertTrue(any("switched off in this server" in str(body) for body in said), "the bot's own check answers before the card does")
        self.assertFalse(any("attachments" in str(body) for body in said))
        mark = len(self.net.calls)
        self.slash("stats", user_id=self.member_id, options=[{"name": "language", "type": 1, "options": [{"name": "language", "type": 3, "value": "de"}]}],
                   guild_command=False)
        await self.settle()
        self.assert_no_errors("/stats language while off")
        self.assertEqual(self.bot.module("statistics").language_of(self.member_id), "de")


class TypedNamesAndPrivateAnswers(GatewayTest):
    """Typed statistics commands next to a server's tags, and typed answers that name settings or channels."""
    modules = [Utilities, Tags, Triggers, Statistics]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        now = time.time()
        for name, text in (("top", "Our top printing tips: dry your filament."), ("me", "Introduce yourself in #introductions.")):
            await self.bot.db.run("INSERT INTO tags (guild_id, name, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (self.guild_id, name, text, now, now))

    async def typed(self, content: str, *, user_id: int | None = None) -> list[dict]:
        """What the bot posted after a typed message, anywhere."""
        mark = len(self.net.posted)
        self.dispatch_message(user_id=user_id or self.member_id, content=content)
        await self.settle()
        self.assert_no_errors(content)
        return self.net.posted[mark:]

    def here(self, posted: list[dict]) -> list[dict]:
        return [message for message in posted if int(message["channel_id"]) == int(self.text["id"])]

    async def test_with_statistics_off_tags_and_triggers_keep_their_names(self):
        self.assertFalse(self.bot.settings.get(self.guild_id, "statistics")["enabled"])
        said = " | ".join(message["content"] for message in self.here(await self.typed("!top")))
        self.assertIn("dry your filament", said)
        self.assertNotIn("switched off", said)
        self.assertIn("Introduce yourself", " | ".join(message["content"] for message in self.here(await self.typed("!me"))))
        self.assertEqual(await self.typed("!voice"), [], "a statistics command with no tag of its name is an unknown word here")
        message = types.SimpleNamespace(content="!voice", guild=self.guild)
        self.assertFalse(await self.bot.module("triggers")._is_command(message), "so a trigger can answer it")
        tags = self.bot.module("tags")
        self.assertIsNone(await tags.create(self.guild, self.guild.get_member(self.admin_id), "voice", "Voice chat opens at six."))
        self.assertEqual(await tags.shadowed(self.guild_id), [])

    async def test_with_statistics_on_the_command_answers_and_staff_see_the_clash(self):
        await self.bot.settings.save(self.guild_id, "statistics", {"enabled": True})
        posted = self.here(await self.typed("!top"))
        self.assertTrue(any(message["attachments"] for message in posted), "the card")
        self.assertNotIn("dry your filament", str(posted))
        tags = self.bot.module("tags")
        self.assertEqual(await tags.shadowed(self.guild_id), ["me", "top"])
        message = types.SimpleNamespace(content="!voice", guild=self.guild)
        self.assertTrue(await self.bot.module("triggers")._is_command(message))
        self.assertIsNotNone(await tags.create(self.guild, self.guild.get_member(self.admin_id), "voice", "Voice chat opens at six."))
        posted = await self.typed("!stats_check", user_id=self.admin_id)
        elsewhere = [message for message in posted if message not in self.here(posted)]
        text = str(elsewhere)
        self.assertIn(words.t("en-US", "check_tag_clash", names="`me`, `top`"), text, "the check names the shadowed tags for staff")

    async def test_a_typed_check_goes_by_dm_and_names_no_hidden_channel_here(self):
        await self.bot.settings.save(self.guild_id, "statistics", {"enabled": True})
        state = self.bot._connection
        readable = discord.Permissions(view_channel=True, read_message_history=True, send_messages=True)
        state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": {**self.role_everyone, "permissions": str(readable.value)}})
        bot_perms = discord.Permissions(view_channel=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True)
        state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": {**self.role_bot, "permissions": str(bot_perms.value)}})
        hidden = {"id": str(new_id()), "guild_id": str(self.guild_id), "name": "ban-appeals-private", "type": 0, "position": 9, "parent_id": None,
                  "nsfw": False, "topic": None, "rate_limit_per_user": 0, "last_message_id": None, "flags": 0,
                  "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(view_channel=True).value)}]}
        state.parse_channel_create(hidden)
        self.assertFalse(self.guild.get_channel(int(hidden["id"])).permissions_for(self.guild.me).view_channel)
        posted = await self.typed("!stats_check", user_id=self.admin_id)
        here = self.here(posted)
        self.assertEqual([message["attachments"] for message in here], [[]], "one short pointer here, with no file")
        self.assertIn(words.t("en-US", "sent_by_dm"), here[0]["content"])
        files = [(message["channel_id"], attachment["filename"]) for message in posted for attachment in message["attachments"]]
        self.assertEqual([name for _, name in files], ["channels-not-seen.txt"])
        self.assertNotEqual(int(files[0][0]), int(self.text["id"]), "the file went to the admin's DMs")


class InfoNames(GatewayTest):
    """Typed, `info` is the member lookup, as it was before statistics came. The statistics lookups keep /info on the slash side and go by
    statsinfo typed, which `info activity` and the rest reach as short names."""
    modules = [Utilities, Config, Statistics]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "statistics", {"enabled": True})
        await self.bot.sync_server_commands(self.guild_id)

    async def typed(self, content: str, *, user_id: int | None = None) -> list[dict]:
        mark = len(self.net.posted)
        self.dispatch_message(user_id=user_id or self.owner_id, content=content)
        await self.settle()
        self.assert_no_errors(content)
        return self.net.posted[mark:]

    async def test_typed_info_is_the_member_lookup(self):
        self.assertIsNone(self.bot.get_command("info"), "no command takes the word, so its short name runs")
        for line, name in (("!info", "member"), ("!i", "member"), (f"!info <@{self.admin_id}>", "admin")):
            with self.subTest(line=line):
                posted = await self.typed(line, user_id=self.member_id)
                self.assertTrue(posted and posted[-1]["embeds"], line)
                self.assertIn(name, posted[-1]["embeds"][0]["title"])
                self.assertFalse(posted[-1]["attachments"])

    async def test_server_rules_on_info_reach_the_member_lookup(self):
        await self.typed("!disable info")
        overwrites = self.bot.settings.get(self.guild_id, COMMANDS_MODULE)["overwrites"]
        self.assertEqual(overwrites["members"]["disabled_subcommands"], ["members info"])
        self.assertNotIn("statsinfo", overwrites)
        await self.typed("!enable info")
        for line in ("!restrict info", "!modonly info"):
            with self.subTest(line=line):
                said = " ".join(message["content"] for message in await self.typed(line))
                self.assertIn("`members", said)
                self.assertIn("members", self.bot.settings.get(self.guild_id, COMMANDS_MODULE)["overwrites"])

    async def test_typed_lookups_reach_statistics_through_short_names(self):
        self.assertNotIn("statsinfo", SHORTCUTS, "the typed name collides with nothing")
        for short in ("info activity", "info emoji", "info emojis", "info id", "info roles"):
            with self.subTest(short=short):
                self.assertTrue(expand_line(self.bot, short).startswith("statsinfo "))
                self.assertEqual(named_command(self.bot, short)[0].root_parent.qualified_name, "statsinfo")
        posted = await self.typed("!info id")
        self.assertIn(str(self.guild_id), posted[-1]["content"])
        for line in ("!info emoji", "!info emojis"):
            with self.subTest(line=line):
                self.assertIn(words.t("en-US", "info_emoji_none"), (await self.typed(line))[-1]["content"])
        self.assertFalse(self.bot.intents.presences)
        self.assertIn(words.t("en-US", "info_no_presence"), (await self.typed("!info activity"))[-1]["content"])
        posted = await self.typed(f"!info roles <@&{self.role_member['id']}>")
        self.assertEqual([attachment["filename"] for attachment in posted[-1]["attachments"]], ["roles.csv"])

    async def test_slash_info_keeps_its_names(self):
        registered = {command["name"]: command for command in self.net.guild_commands.get(self.guild_id, [])}
        self.assertIn("info", registered)
        self.assertEqual(sorted(option["name"] for option in registered["info"]["options"]), ["activity", "emoji", "id", "roles"])
        self.assertNotIn("statsinfo", registered)
        data = {"id": str(new_id()), "name": "info", "type": 1, "guild_id": str(self.guild_id), "options": [{"name": "id", "type": 1, "options": []}], "resolved": {}}
        mark = len(self.net.calls)
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.member_id))
        await self.settle()
        self.assert_no_errors("/info id")
        said = [body for method, path, body in self.net.calls[mark:] if "/callback" in path or path.startswith("/webhooks")]
        self.assertTrue(any(str(self.guild_id) in str(body) for body in said), said)
        data["options"] = [{"name": "roles", "type": 1, "options": [{"name": "role", "type": 8, "value": self.role_member["id"]}]}]
        data["resolved"] = {"roles": {self.role_member["id"]: self.role_member}}
        mark = len(self.net.calls)
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.member_id))
        await self.settle()
        said = " ".join(str(body) for method, path, body in self.net.calls[mark:] if "/callback" in path or path.startswith("/webhooks"))
        self.assertNotIn("roles.csv", said, "the roles file needs Manage Roles on the slash side too")
