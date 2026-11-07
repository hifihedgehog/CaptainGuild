import re
from pathlib import Path

import discord

from captainguild.core.ops import DiscordOps, OpsError
from captainguild.core.tagscript import Scope, render_text
from captainguild.modules.automod import Automod
from captainguild.modules.backup import Backup
from captainguild.modules.greetings import Greetings
from captainguild.modules.moderation import Moderation
from captainguild.modules.reactionroles import ReactionRoles
from captainguild.modules.setup import AUTOMOD_PRESETS, PRESET_CHOICES, ROLE_TEMPLATES, Setup
from captainguild.demo import GUILD
from tests.fakes import BotTest
from tests.gateway import GatewayTest


class SetupCase(BotTest):
    modules = [Moderation, Automod, Greetings, ReactionRoles, Setup]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ops = self.bot.ops
        self.real_guild = self.guild
        self.guild.id = GUILD  # the demo's sample server answers for this ID
        self.web = await self.client()

    def url(self, tail):
        return f"/api/g/{GUILD}{tail}"

    async def post(self, tail, body=None):
        return await self.web.post(self.url(tail), json=body or {})


class StateTest(SetupCase):
    async def test_state_describes_every_step(self):
        await self.bot.settings.save(GUILD, "core", {"prefixes": [".", "?"]})
        await self.bot.settings.save(GUILD, "moderation", {"modlog_channel": 900000000000001011})
        data = await (await self.web.get(self.url("/setup"))).json()
        self.assertEqual(data["prefixes"], [".", "?"])
        self.assertEqual(data["modlog_channel"], "900000000000001011")
        self.assertIsNone(data["mute_role"])
        self.assertEqual({row["key"] for row in data["templates"]}, set(ROLE_TEMPLATES))
        self.assertEqual([row["key"] for row in data["presets"]], ["none", "minimal", "medium", "high"])
        self.assertEqual(data["has"], {"greetings": True, "moderation": True, "automod": True, "reactionroles": True})

    async def test_setup_needs_manage_server(self):
        staff = await self.client(access="staff")
        for call in (staff.get, ):
            self.assertEqual((await call(self.url("/setup"))).status, 403)
        for tail in ("/setup/automod", "/setup/role-template", "/setup/mute-role", "/setup/modlog-channel", "/setup/channel"):
            self.assertEqual((await staff.post(self.url(tail), json={})).status, 403, tail)


class GeneralAndWelcomeTest(SetupCase):
    async def test_the_steps_fill_the_same_settings_the_other_pages_use(self):
        response = await self.web.put(self.url("/settings/core"), json={"prefixes": ["!", "?"]})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.bot.settings.get(GUILD, "core")["prefixes"], ["!", "?"])
        welcome = {"content": "Welcome to {server}, {user}! Make yourself at home.", "embeds": [], "buttons": []}
        response = await self.web.put(self.url("/settings/greetings"), json={"welcome_channel": "900000000000001001", "welcome": welcome})
        self.assertEqual(response.status, 200, await response.text())
        settings = self.bot.settings.get(GUILD, "greetings")
        self.assertEqual(settings["welcome_channel"], 900000000000001001)
        self.assertEqual(settings["welcome"]["content"], welcome["content"])
        response = await self.web.put(self.url("/settings/greetings"), json={"welcome_channel": None})
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.bot.settings.get(GUILD, "greetings")["welcome_channel"])


    async def test_the_preset_greeting_fills_in_with_the_real_template_language(self):
        source = Path(__file__).resolve().parent.parent / "captainguild/web/static/js/pages/setup.js"
        preset = re.search(r"WELCOME_PRESET = '([^']+)'", source.read_text(encoding="utf-8")).group(1)
        self.guild.name = "Printers"
        self.member.name = "Ada"
        rendered = render_text(preset, Scope(guild=self.guild, member=self.member, channel=self.channel))
        self.assertNotIn("{", rendered)
        self.assertIn("Printers", rendered)
        self.assertIn("Ada", rendered)


class AutomodPresetTest(SetupCase):
    def test_every_preset_passes_the_real_automod_checks(self):
        for name, form in AUTOMOD_PRESETS.items():
            self.bot.settings.schemas["automod"].clean(form)
        self.assertEqual({row["key"] for row in PRESET_CHOICES} - {"none"}, set(AUTOMOD_PRESETS))

    async def test_a_preset_turns_on_its_rules_and_is_recorded(self):
        response = await self.post("/setup/automod", {"preset": "medium"})
        self.assertEqual(response.status, 200)
        settings = self.bot.settings.get(GUILD, "automod")
        for rule in ("mentions", "invites", "spam", "attachments"):
            self.assertTrue(settings[rule]["enabled"], rule)
        self.assertFalse(settings["caps"]["enabled"])
        rows = await self.bot.activity_log.recent(GUILD)
        self.assertEqual(rows[0]["action"], "setup.automod.medium")

    async def test_each_preset_adds_to_the_one_before(self):
        def turned_on(form):
            return {name for name, value in form.items() if isinstance(value, dict) and value.get("enabled")}
        minimal, medium, high = (turned_on(AUTOMOD_PRESETS[k]) for k in ("minimal", "medium", "high"))
        self.assertLess(minimal, medium)
        self.assertLess(medium, high)

    async def test_a_preset_leaves_lists_someone_already_set(self):
        await self.bot.settings.save(GUILD, "automod", {"mentions": {"exempt_roles": [900000000000000101]}, "log_channel": 900000000000001011})
        await self.post("/setup/automod", {"preset": "minimal"})
        settings = self.bot.settings.get(GUILD, "automod")
        self.assertTrue(settings["mentions"]["enabled"])
        self.assertEqual(settings["mentions"]["exempt_roles"], [900000000000000101])
        self.assertEqual(settings["log_channel"], 900000000000001011)

    async def test_off_or_custom_changes_nothing(self):
        before = self.bot.settings.get(GUILD, "automod")
        response = await self.post("/setup/automod", {"preset": "none"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.bot.settings.get(GUILD, "automod"), before)

    async def test_an_unknown_preset_is_refused(self):
        self.assertEqual((await self.post("/setup/automod", {"preset": "nuclear"})).status, 400)
        self.assertEqual((await self.post("/setup/automod", {"preset": ["high"]})).status, 400)


class RoleTemplateTest(SetupCase):
    async def test_roles_are_made_with_their_colors(self):
        response = await self.post("/setup/role-template", {"template": "pronouns"})
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertEqual([row["name"] for row in data["roles"]], ["He/Him", "She/Her", "They/Them", "Ask"])
        self.assertEqual(data["made"], 4)
        names = {role["name"]: role for role in self.ops.roles}
        self.assertEqual(names["Ask"]["color"], 0x78B059)
        self.assertEqual(names["Ask"]["permissions"], [])

    async def test_running_it_twice_makes_no_duplicates(self):
        first = await (await self.post("/setup/role-template", {"template": "rpg"})).json()
        second = await (await self.post("/setup/role-template", {"template": "rpg"})).json()
        self.assertEqual(second["made"], 0)
        self.assertEqual([row["id"] for row in first["roles"]], [row["id"] for row in second["roles"]])

    async def test_a_same_named_role_with_power_or_above_the_bot_is_not_reused(self):
        self.ops.roles.insert(0, {"id": 900000000000000900, "name": "Tank", "color": 0, "position": 1, "managed": False, "default": False, "permissions": ["ban_members"], "members": 0})
        self.ops.roles.insert(0, {"id": 900000000000000901, "name": "DPS", "color": 0, "position": 99, "managed": False, "default": False, "permissions": [], "members": 0})
        data = await (await self.post("/setup/role-template", {"template": "rpg"})).json()
        ids = {row["name"]: row["id"] for row in data["roles"]}
        self.assertNotEqual(ids["Tank"], "900000000000000900")
        self.assertNotEqual(ids["DPS"], "900000000000000901")
        self.assertEqual(data["made"], 3)

    async def test_an_unknown_template_is_refused(self):
        for bad in ("nothing", None, ["colors"], {"x": 1}):
            self.assertEqual((await self.post("/setup/role-template", {"template": bad})).status, 400, bad)

    async def test_every_template_fits_a_reaction_role_message(self):
        for key, template in ROLE_TEMPLATES.items():
            emoji = [row[2] for row in template["roles"]]
            self.assertEqual(len(emoji), len(set(emoji)), key)
            self.assertLessEqual(len(emoji), 20, key)
            for mark in emoji:
                self.assertEqual(len(mark), 1, f"{key}: one code point, so a reaction needs no invisible selector character")

    async def test_the_page_can_post_the_message_through_the_reaction_roles_api(self):
        data = await (await self.post("/setup/role-template", {"template": "platforms"})).json()
        form = {
            "create_mode": "post", "channel_id": "900000000000001001", "type": "normal", "mode": "reactions",
            "payload": {"content": "", "buttons": [], "embeds": [{"title": "What do you play on?", "description": "x", "color": "#49d6df"}]},
            "rows": [{"emoji": row["emoji"], "roles": [row["id"]]} for row in data["roles"]],
        }
        response = await self.post("/reactionroles", form)
        self.assertEqual(response.status, 200, await response.text())
        made = await response.json()
        self.assertEqual(made["warnings"], [])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_pairs WHERE guild_id = ?", (GUILD,)), 5)


class ModerationStepTest(SetupCase):
    async def test_making_a_mute_role_saves_it(self):
        response = await self.post("/setup/mute-role")
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertEqual(data["name"], "Muted")
        self.assertEqual(str(self.bot.settings.get(GUILD, "moderation")["mute_role"]), str(data["id"]))
        self.assertEqual((await self.bot.activity_log.recent(GUILD))[0]["action"], "setup.muterole")

    async def test_making_a_modlog_channel_saves_it(self):
        response = await self.post("/setup/modlog-channel")
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertEqual(data["name"], "mod-log")
        self.assertEqual(str(self.bot.settings.get(GUILD, "moderation")["modlog_channel"]), str(data["id"]))

    async def test_discord_refusing_is_a_message(self):
        async def refuse(*args, **kwargs):
            raise OpsError("The bot can't create roles here. It needs Manage Roles.")

        self.ops.create_mute_role = refuse
        response = await self.post("/setup/mute-role")
        self.assertEqual(response.status, 502)
        self.assertIn("Manage Roles", (await response.json())["error"])
        self.assertIsNone(self.bot.settings.get(GUILD, "moderation")["mute_role"])

    async def test_a_named_channel_is_cleaned_and_refused_when_empty(self):
        response = await self.post("/setup/channel", {"name": "Pick Your Roles!"})
        self.assertEqual((await response.json())["name"], "pick-your-roles")
        for bad in ("", "!!!", None, 5, "x" * 101):
            self.assertEqual((await self.post("/setup/channel", {"name": bad})).status, 400, bad)


class WizardWithoutModulesTest(BotTest):
    modules = [Setup, Backup]

    async def test_steps_whose_modules_are_missing_say_so(self):
        self.guild.id = GUILD
        client = await self.client()
        data = await (await client.get(f"/api/g/{GUILD}/setup")).json()
        self.assertEqual(data["has"], {"greetings": False, "moderation": False, "automod": False, "reactionroles": False})
        self.assertIsNone(data["welcome_channel"])
        self.assertEqual((await client.post(f"/api/g/{GUILD}/setup/mute-role", json={})).status, 404)
        self.assertEqual((await client.post(f"/api/g/{GUILD}/setup/automod", json={"preset": "high"})).status, 404)


class RealLibraryTest(GatewayTest):
    """These run the real DiscordOps against the real library with only the network faked, so a wrong keyword or attribute shows up here."""
    modules = [Moderation]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.real = DiscordOps(self.bot)

    async def test_create_role(self):
        row = await self.real.create_role(self.guild_id, "Red", color=0xFF7070)
        self.assertEqual(row["name"], "Red")
        call = next(c for c in self.net.calls if c[0] == "POST" and c[1] == "/guilds/{guild_id}/roles")
        self.assertEqual(call[2]["name"], "Red")
        self.assertEqual(call[2]["colors"]["primary_color"], 0xFF7070)
        self.assertEqual(int(call[2]["permissions"]), 0)

    async def test_create_mute_role_limits_every_channel(self):
        row = await self.real.create_mute_role(self.guild_id)
        self.assertEqual(row["name"], "Muted")
        touched = [c for c in self.net.calls if c[0] == "PUT" and c[1] == "/channels/{channel_id}/permissions/{target}"]
        self.assertGreater(len(touched), 0)

    async def test_create_private_channel(self):
        row = await self.real.create_channel(self.guild_id, "mod-log", private=True)
        self.assertEqual(row["name"], "mod-log")
        call = next(c for c in self.net.calls if c[0] == "POST" and c[1] == "/guilds/{guild_id}/channels")
        overwrites = {int(o["id"]): o for o in call[2]["permission_overwrites"]}
        everyone = overwrites[self.guild_id]
        self.assertTrue(int(everyone["deny"]) & discord.Permissions(view_channel=True).value)
        mine = overwrites[self.bot_id]
        self.assertTrue(int(mine["allow"]) & discord.Permissions(view_channel=True, send_messages=True).value)

    async def test_refusal_becomes_a_sentence(self):
        async def refuse(route, **kwargs):
            raise discord.Forbidden(type("R", (), {"status": 403, "reason": "no"})(), {"code": 50013, "message": "Missing Permissions"})

        self.bot.http.request = refuse
        with self.assertRaises(OpsError) as caught:
            await self.real.create_role(self.guild_id, "Red")
        self.assertIn("Manage Roles", str(caught.exception))
        self.bot.http.request = self.net.http
