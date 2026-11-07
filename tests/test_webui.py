"""What the dashboard's shell and Commands page lean on: the command catalog, a change to one command's rules, bot channels that are a category, and the sign-in notes."""
from __future__ import annotations

import unittest

import discord
from aiohttp.test_utils import TestClient, TestServer

from captainguild.core.ops import DiscordOps
from captainguild.core.perms import Who, answer_place, evaluate
from captainguild.core.validate import ValidationError
from captainguild.demo import GUILD
from captainguild.modules import load_all
from captainguild.modules.moderation import Moderation
from captainguild.modules.reactionroles import clean_settings
from captainguild.modules.setup import Setup
from captainguild.modules.starboard import Starboard
from captainguild.web.app import build_app
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMessage
from tests.gateway import GatewayTest


class Catalog(BotTest):
    modules = load_all()

    def tabs(self) -> dict[str, list[dict]]:
        return {group["category"]: group["commands"] for group in self.bot.command_catalog()}

    def test_tabs_follow_the_old_dashboard_order(self) -> None:
        names = list(self.tabs())
        order = ["Config", "Automod", "Feeds", "Fun", "Greetings", "Levels", "Logging", "Misc", "Moderation", "Roles", "Stats", "Notifications", "Suggestions", "Tags & Triggers", "Utilities"]
        self.assertEqual([n for n in names if n in order], order)
        self.assertEqual(names[0], "Config")

    def test_misc_stats_and_games_have_their_own_tabs(self) -> None:
        tabs = self.tabs()
        for tab, command in (("Misc", "misc"), ("Stats", "stats"), ("Games", "games")):
            self.assertIn(command, [c["name"] for c in tabs[tab]], tab)
        self.assertNotIn("misc", [c["name"] for c in tabs["Utilities"]])
        self.assertNotIn("games", [c["name"] for c in tabs["Fun"]])
        self.assertIn("fun", [c["name"] for c in tabs["Fun"]])

    def test_every_command_keeps_exactly_one_tab(self) -> None:
        seen = [c["name"] for rows in self.tabs().values() for c in rows]
        self.assertEqual(len(seen), len(set(seen)))

    def test_a_command_lists_the_short_names_that_reach_it(self) -> None:
        fun = next(c for c in self.tabs()["Fun"] if c["name"] == "fun")
        for name in ("8ball", "coinflip", "urbandictionary"):
            self.assertIn(name, fun["aliases"])
        level = next(c for c in self.tabs()["Levels"] if c["name"] == "level")
        self.assertIn("levels", level["aliases"])

    def test_a_subcommand_alias_is_listed_on_its_group(self) -> None:
        fun = next(c for c in self.tabs()["Fun"] if c["name"] == "fun")
        self.assertIn("8ball", fun["aliases"], "eightball answers to 8ball")


class OverwritePatch(BotTest):
    modules = load_all()

    async def test_a_change_lands_on_the_map_as_it_is_now(self) -> None:
        client = await self.client()
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"ban": {"enabled": False}}})
        # The page was opened before that save, so it only knows about "kick".
        response = await client.patch(self.path("/commands/overwrites"), json={"changes": {"kick": {"enabled": False}}})
        self.assertEqual(response.status, 200, await response.text())
        saved = self.settings("commands")["overwrites"]
        self.assertFalse(saved["ban"]["enabled"])
        self.assertFalse(saved["kick"]["enabled"])

    async def test_two_changes_in_a_row_both_stay(self) -> None:
        client = await self.client()
        first = await client.patch(self.path("/commands/overwrites"), json={"changes": {"ban": {"enabled": False}}})
        second = await client.patch(self.path("/commands/overwrites"), json={"changes": {"kick": {"enabled": False}}})
        self.assertEqual((first.status, second.status), (200, 200))
        self.assertEqual(sorted(self.settings("commands")["overwrites"]), ["ban", "kick"])

    async def test_null_takes_a_rule_away(self) -> None:
        client = await self.client()
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"ban": {"enabled": False}, "kick": {"enabled": False}}})
        response = await client.patch(self.path("/commands/overwrites"), json={"changes": {"ban": None}})
        self.assertEqual(response.status, 200)
        self.assertEqual(list(self.settings("commands")["overwrites"]), ["kick"])

    async def test_it_needs_the_same_access_as_saving_settings(self) -> None:
        client = await self.client(access="staff")
        response = await client.patch(self.path("/commands/overwrites"), json={"changes": {"ban": {"enabled": False}}})
        self.assertGreaterEqual(response.status, 400)
        self.assertEqual(self.settings("commands")["overwrites"], {})

    async def test_it_applies_the_checks_a_full_save_applies(self) -> None:
        client = await self.client()
        response = await client.patch(self.path("/commands/overwrites"), json={"changes": {"ban": {"redirect": "not a channel"}}})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.settings("commands")["overwrites"], {})

    async def test_an_empty_request_is_refused(self) -> None:
        client = await self.client()
        self.assertEqual((await client.patch(self.path("/commands/overwrites"), json={"changes": {}})).status, 400)
        self.assertEqual((await client.patch(self.path("/commands/overwrites"), json={})).status, 400)


class ShellFacts(BotTest):
    async def site(self, *, client_secret: str = "") -> TestClient:
        app = build_app(self.bot, session_key=b"t" * 32, client_id="1", client_secret=client_secret, public_url="https://bot.example", secure_cookies=False)
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def test_activity_with_a_huge_before_answers_with_the_newest(self) -> None:
        client = await self.client()
        response = await client.get(self.path("/activity"), params={"before": "9" * 30})
        self.assertEqual(response.status, 200)
        self.assertIsInstance(await response.json(), list)

    async def test_activity_pages_back_from_an_id(self) -> None:
        client = await self.client()
        for number in range(3):
            await self.bot.activity_log.record(self.guild.id, self.bot.actor(self.admin), f"test.{number}")
        rows = await (await client.get(self.path("/activity"))).json()
        older = await (await client.get(self.path("/activity"), params={"before": str(rows[0]["id"])})).json()
        self.assertEqual([r["id"] for r in older], [r["id"] for r in rows[1:]])

    async def test_me_carries_a_link_that_adds_the_bot(self) -> None:
        client = await self.site()
        invite = (await (await client.get("/api/me")).json())["invite"]
        self.assertTrue(invite.startswith("https://discord.com/oauth2/authorize?"), invite)
        self.assertIn("client_id=1", invite)
        self.assertIn("permissions=", invite)
        self.assertIn("applications.commands", invite)

    async def test_a_failed_sign_in_says_so(self) -> None:
        client = await self.site(client_secret="secret")
        response = await client.get("/login/callback?code=x&state=y", allow_redirects=False)
        self.assertEqual(response.status, 400)
        body = await response.text()
        self.assertIn("didn't work", body)
        self.assertIn('href="/login"', body)

    async def test_the_my_data_link_ignores_a_page_remembered_from_earlier(self) -> None:
        client = await self.site()
        await client.get("/login?next=/dashboard/5/logging")
        token = self.bot.sign_in_links.create(self.admin.id, "Ann", None, "/my-data")
        used = await client.post(f"/login/link/{token}", allow_redirects=False)
        self.assertEqual(used.headers["Location"], "/my-data")

    async def test_the_catalog_names_permissions_the_way_discord_does(self) -> None:
        client = await self.client()
        data = await (await client.get(self.path("/commands"))).json()
        self.assertEqual(data["permission_labels"]["manage_guild"], "Manage Server")
        self.assertEqual(data["permission_labels"]["moderate_members"], "Timeout Members")


class ModlogChannel(BotTest):
    modules = [Moderation, Setup]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.guild.id = GUILD  # the demo's sample server answers for this ID
        self.web = await self.client()
        self.asked: list[dict] = []

        async def create_channel(guild_id, name, **kwargs):
            self.asked.append(kwargs)
            return {"id": 900000000000000777, "name": name}

        self.bot.ops.create_channel = create_channel

    async def test_setup_names_the_roles_that_may_read_the_channel(self) -> None:
        await self.bot.settings.save(GUILD, "commands", {"mod_role": self.role_mod.id})
        response = await self.web.post(f"/api/g/{GUILD}/setup/modlog-channel", json={})
        self.assertEqual(response.status, 200, await response.text())
        self.assertTrue(self.asked[0]["private"])
        self.assertEqual(set(self.asked[0]["viewer_roles"]), {self.role_mod.id})

    async def test_without_a_named_role_only_the_default_applies(self) -> None:
        await self.web.post(f"/api/g/{GUILD}/setup/modlog-channel", json={})
        self.assertEqual(list(self.asked[0]["viewer_roles"]), [])


class ModlogChannelOverDiscord(GatewayTest):
    """The real DiscordOps against the real library with only the network faked."""
    modules = [Moderation]

    async def test_viewer_roles_are_allowed_to_see_a_private_channel(self) -> None:
        role_id = int(self.role_mod["id"])
        await DiscordOps(self.bot).create_channel(self.guild_id, "mod-log", private=True, viewer_roles=[role_id])
        call = next(c for c in self.net.calls if c[0] == "POST" and c[1] == "/guilds/{guild_id}/channels")
        overwrites = {int(o["id"]): o for o in call[2]["permission_overwrites"]}
        self.assertTrue(int(overwrites[role_id]["allow"]) & discord.Permissions(view_channel=True, read_message_history=True).value)
        self.assertTrue(int(overwrites[self.guild_id]["deny"]) & discord.Permissions(view_channel=True).value)


class StarboardIgnoredCategory(unittest.TestCase):
    def test_an_ignored_category_covers_the_channels_and_threads_in_it(self) -> None:
        cfg = {"channel_id": 1, "blacklist": [900]}
        inside = type("C", (), {"id": 5, "parent_id": None, "category_id": 900})()
        thread = type("T", (), {"id": 6, "parent_id": 5, "category_id": 900})()
        outside = type("C", (), {"id": 7, "parent_id": None, "category_id": 901})()
        loose = type("C", (), {"id": 8, "parent_id": None, "category_id": None})()
        self.assertFalse(Starboard._channel_counts(cfg, inside))
        self.assertFalse(Starboard._channel_counts(cfg, thread))
        self.assertTrue(Starboard._channel_counts(cfg, outside))
        self.assertTrue(Starboard._channel_counts(cfg, loose))

    def test_a_listed_channel_and_its_threads_stay_ignored(self) -> None:
        cfg = {"channel_id": 1, "blacklist": [5]}
        self.assertFalse(Starboard._channel_counts(cfg, type("C", (), {"id": 5, "parent_id": None, "category_id": None})()))
        self.assertFalse(Starboard._channel_counts(cfg, type("T", (), {"id": 6, "parent_id": 5, "category_id": None})()))


class MessageRowNamesItsChannel(BotTest):
    async def test_a_message_row_carries_the_name_of_the_channel_it_is_in(self) -> None:
        self.bot._connection.user = self.me
        thread = FakeChannel(self.guild, "a forum post")
        row = DiscordOps(self.bot)._message_row(FakeMessage(thread, self.me, "hi"), thread.id)
        self.assertEqual(row["channel_name"], "a forum post")


class ReactionRoleLabelLimit(unittest.TestCase):
    INFO = {"roles": [{"id": 900000000000000500, "name": "Red", "default": False, "managed": False, "permissions": []}]}

    def form(self, mode: str, size: int) -> dict:
        return {"mode": mode, "rows": [{"emoji": "", "roles": [900000000000000500], "label": "x" * size}]}

    def test_a_menu_option_label_can_be_a_hundred_characters(self) -> None:
        clean = clean_settings(self.form("select", 100), self.INFO, foreign=False, link_buttons=0)
        self.assertEqual(len(clean["options"][0].label), 100)

    def test_a_menu_option_label_over_a_hundred_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            clean_settings(self.form("select", 101), self.INFO, foreign=False, link_buttons=0)
        self.assertIn("100", caught.exception.fields["rows.0.label"])

    def test_a_button_label_stays_at_eighty(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            clean_settings(self.form("buttons", 90), self.INFO, foreign=False, link_buttons=0)
        self.assertIn("80", caught.exception.fields["rows.0.label"])


class BotChannelCategory(unittest.TestCase):
    def rule(self, bot_channels: list[int]) -> dict:
        return {"bot_channels": bot_channels, "overwrites": {"ping": {"restricted": True}}}

    def test_a_channel_in_a_bot_category_is_a_bot_channel(self) -> None:
        decision = evaluate(self.rule([900]), root="ping", who=Who(), channel_id=5, category_id=900)
        self.assertIsNone(decision.restricted_channel)

    def test_a_channel_outside_it_is_sent_to_it(self) -> None:
        decision = evaluate(self.rule([900]), root="ping", who=Who(), channel_id=5, category_id=901)
        self.assertEqual(decision.restricted_channel, 900)

    def test_a_listed_channel_still_counts(self) -> None:
        decision = evaluate(self.rule([5]), root="ping", who=Who(), channel_id=5)
        self.assertIsNone(decision.restricted_channel)

    def test_a_category_answers_in_the_first_text_channel_that_takes_a_post(self) -> None:
        guild = FakeGuild()
        guild.me = guild_member = type("Me", (), {"guild_permissions": None})()
        category = FakeChannel(guild, "bots", kind=discord.ChannelType.category)
        voice = FakeChannel(guild, "voice", kind=discord.ChannelType.voice, category=category)
        text = FakeChannel(guild, "commands", category=category)
        FakeChannel(guild, "elsewhere")
        self.assertIs(answer_place(guild, category.id), text)
        self.assertIs(answer_place(guild, text.id), text)
        self.assertIsNone(answer_place(guild, 1))
        self.assertIsNone(answer_place(None, text.id))
        del voice, guild_member


if __name__ == "__main__":
    unittest.main()
