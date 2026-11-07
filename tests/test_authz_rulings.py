"""Attacks on the delegation, posting, reading and ping rulings by someone who holds Manage Server and little else, each with the ordinary use that must keep working."""
from __future__ import annotations

import json
import time

from aiohttp.test_utils import TestClient, TestServer

from captainguild.core.ops import DiscordOps
from captainguild.modules.automod import Automod
from captainguild.modules.config import Config
from captainguild.modules.embeds import Embeds
from captainguild.modules.feeds import Feeds
from captainguild.modules.levels import Levels
from captainguild.modules.logs import Logs
from captainguild.modules.moderation import Moderation
from captainguild.modules.proposals import Proposals
from captainguild.modules.reports import Reports
from captainguild.modules.roles import Roles
from captainguild.modules.starboard import Starboard
from captainguild.modules.suggestions import Suggestions
from captainguild.modules.tags import Tags
from captainguild.modules.triggers import Triggers
from captainguild.modules.voice import Voice
from captainguild.modules.voicelink import VoiceLink
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, FakePermissions

EVERYONE = ("view_channel", "send_messages", "read_message_history")


class Closed(FakeChannel):
    """A channel that treats people by name: `allowed` maps a member to what they hold here, and anyone else gets their server-wide permissions."""

    def __init__(self, guild, name, allowed):
        super().__init__(guild, name)
        self.allowed = allowed

    def permissions_for(self, target):
        if target in self.allowed:
            return FakePermissions(*self.allowed[target])
        return target.permissions if hasattr(target, "permissions") else target.guild_permissions


class ReachCase(BotTest):
    modules = [Reports, Suggestions, Proposals, Voice, VoiceLink, Levels, Logs, Embeds, Feeds, Triggers, Tags]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.guild.default_role.permissions = FakePermissions(*EVERYONE)
        self.manager_role = self.guild.role("Manager", 4, "manage_guild")
        self.manager = FakeMember(self.guild, "manager", self.manager_role)
        self.high = self.guild.role("High", 6)
        self.low = self.guild.role("Low", 3)
        self.read_only = Closed(self.guild, "announcements", {self.manager: ("view_channel", "read_message_history")})
        self.hidden = Closed(self.guild, "admin-chat", {self.manager: ()})

    async def site(self, member):
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(member.id, member.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def put(self, member, module, form):
        client = await self.site(member)
        response = await client.put(self.path(f"/settings/{module}"), json=form)
        return response.status, await response.json()

    async def send(self, member, method, tail, form=None):
        client = await self.site(member)
        response = await client.request(method, self.path(tail), json=form if form is not None else {})
        return response.status, await response.json()


class RanksTest(ReachCase):
    async def test_the_mini_mute_role_counts_as_handing_out_a_role(self):
        status, body = await self.put(self.manager, "voice", {"mini_mute_role": str(self.high.id)})
        self.assertEqual(status, 400)
        self.assertIn("highest role", body["fields"]["mini_mute_role"])
        self.assertEqual((await self.put(self.manager, "voice", {"mini_mute_role": str(self.low.id)}))[0], 200)
        self.assertEqual((await self.put(self.admin, "voice", {"mini_mute_role": str(self.high.id)}))[0], 200)

    async def test_lowering_the_level_of_a_reward_above_you_takes_your_rank(self):
        await self.bot.settings.save(self.guild.id, "levels", {"rewards": [{"level": 50, "role_id": self.high.id}]})
        status, _ = await self.put(self.manager, "levels", {"rewards": [{"level": 1, "role_id": self.high.id}]})
        self.assertEqual(status, 400)
        self.assertEqual(self.settings("levels")["rewards"][0]["level"], 50)
        self.assertEqual((await self.put(self.admin, "levels", {"rewards": [{"level": 1, "role_id": self.high.id}]}))[0], 200)

    async def test_a_manager_still_edits_rewards_below_them_and_keeps_rows_above_them(self):
        await self.bot.settings.save(self.guild.id, "levels", {"rewards": [{"level": 50, "role_id": self.high.id}, {"level": 5, "role_id": self.low.id}]})
        status, _ = await self.put(self.manager, "levels", {"rewards": [{"level": 50, "role_id": self.high.id}, {"level": 2, "role_id": self.low.id}]})
        self.assertEqual(status, 200)

    async def test_moving_a_voice_link_above_you_to_another_channel_takes_your_rank(self):
        room, other = FakeChannel(self.guild, "room"), FakeChannel(self.guild, "other")
        await self.bot.settings.save(self.guild.id, "voicelink", {"links": [{"channel_id": room.id, "roles": [self.high.id]}]})
        status, _ = await self.put(self.manager, "voicelink", {"links": [{"channel_id": str(other.id), "roles": [str(self.high.id)]}]})
        self.assertEqual(status, 400)
        self.assertEqual(self.settings("voicelink")["links"][0]["channel_id"], room.id)
        self.assertEqual((await self.put(self.manager, "voicelink", {"links": [{"channel_id": str(room.id), "roles": [str(self.high.id)]}]}))[0], 200, "the same link saved again changes nothing")


class PingsTest(ReachCase):
    async def test_a_saved_alert_role_nobody_can_ping_takes_mention_everyone(self):
        channel = FakeChannel(self.guild, "reports")
        status, body = await self.put(self.manager, "reports", {"report_channel": str(channel.id), "alert_role": str(self.low.id)})
        self.assertEqual(status, 400)
        self.assertIn("Mention Everyone", body["error"])
        self.low.mentionable = True
        self.assertEqual((await self.put(self.manager, "reports", {"report_channel": str(channel.id), "alert_role": str(self.low.id)}))[0], 200)

    async def test_a_participation_role_nobody_can_ping_takes_mention_everyone(self):
        channel = FakeChannel(self.guild, "votes")
        status, body = await self.put(self.manager, "proposals", {"channel_id": str(channel.id), "participation_role": str(self.low.id)})
        self.assertEqual(status, 400)
        self.assertIn("Mention Everyone", body["error"])
        self.assertEqual((await self.put(self.admin, "proposals", {"channel_id": str(channel.id), "participation_role": str(self.low.id)}))[0], 200)

    async def schedule(self, text):
        client = await self.site(self.admin)
        response = await client.post(self.path("/embeds/post"), json={"channel_id": str(self.channel.id), "payload": {"content": text}, "ping": True, "at": time.time() + 3600})
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())["scheduled"]

    async def test_new_text_on_a_scheduled_ping_cant_add_everyone(self):
        post = await self.schedule("Game night <@&1>")
        status, body = await self.send(self.manager, "PATCH", f"/embeds/scheduled/{post}", {"payload": {"content": "Game night @everyone"}})
        self.assertEqual(status, 400)
        self.assertIn("Mention Everyone", body["error"])
        stored = await self.db.scalar("SELECT payload FROM scheduled_posts WHERE id = ?", (post,))
        self.assertNotIn("@everyone", stored)

    async def test_a_typo_fixed_on_a_scheduled_ping_still_saves(self):
        post = await self.schedule("Gmae night @everyone")
        status, _ = await self.send(self.manager, "PATCH", f"/embeds/scheduled/{post}", {"payload": {"content": "Game night @everyone"}})
        self.assertEqual(status, 200)

    async def autofeed(self, mention, text):
        client = await self.site(self.admin)
        response = await client.post(self.path("/autofeeds"), json={"channel_id": str(self.channel.id), "mention": mention, "message": {"content": text}, "when": time.time() + 3600})
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())["id"]

    async def next_at(self, row):
        return await self.db.scalar("SELECT next_at FROM autofeeds WHERE id = ?", (row,))

    async def test_new_text_on_an_at_here_autofeed_cant_add_everyone(self):
        row = await self.autofeed("here", "Starting soon")
        status, body = await self.send(self.manager, "PUT", f"/autofeeds/{row}", {"channel_id": str(self.channel.id), "mention": "here", "message": {"content": "Starting soon @everyone"}, "when": await self.next_at(row)})
        self.assertEqual(status, 400)
        self.assertIn("Mention Everyone", body["error"])

    async def test_a_typo_fixed_on_a_pinging_autofeed_still_saves(self):
        row = await self.autofeed("everyone", "Strating soon")
        status, _ = await self.send(self.manager, "PUT", f"/autofeeds/{row}", {"channel_id": str(self.channel.id), "mention": "everyone", "message": {"content": "Starting soon"}, "when": await self.next_at(row)})
        self.assertEqual(status, 200)


class PostingTest(ReachCase):
    async def test_a_channel_that_takes_someones_words_needs_posting_there(self):
        for module, field in (("reports", "report_channel"), ("reports", "log_channel_fallback"), ("suggestions", "channel_id"), ("suggestions", "decision_channel"), ("proposals", "channel_id"), ("proposals", "results_channel")):
            status, body = await self.put(self.manager, module, {field: str(self.read_only.id)})
            self.assertEqual(status, 400, (module, field))
            self.assertIn("post in that channel", body["error"], (module, field))
            self.assertEqual((await self.put(self.admin, module, {field: str(self.read_only.id)}))[0], 200, (module, field))

    async def test_a_channel_a_form_already_names_stays_through_a_save(self):
        await self.bot.settings.save(self.guild.id, "reports", {"report_channel": self.read_only.id})
        self.assertEqual((await self.put(self.manager, "reports", {"cooldown_seconds": 20}))[0], 200)

    async def test_a_command_rule_cant_send_answers_where_they_couldnt_post(self):
        for form in ({"overwrites": {"tag": {"redirect": str(self.read_only.id)}}}, {"bot_channels": [str(self.read_only.id)]}):
            status, body = await self.put(self.manager, "commands", form)
            self.assertEqual(status, 400, form)
            self.assertIn("post in that channel", body["error"], form)
            self.assertEqual((await self.put(self.admin, "commands", form))[0], 200, form)

    async def test_the_report_channel_command_refuses_what_the_page_refuses(self):
        for channel, sentence in ((self.hidden, "can't see"), (self.read_only, "can't post")):
            ctx = self.context(author=self.manager)
            await self.cog(Reports).reportchannel.callback(self.cog(Reports), ctx, channel)
            self.assertIn(sentence, ctx.fails[0])
            self.assertIsNone(self.settings("reports")["report_channel"])
        ctx = self.context(author=self.manager)
        await self.cog(Reports).reportchannel.callback(self.cog(Reports), ctx, self.channel)
        self.assertEqual(self.settings("reports")["report_channel"], self.channel.id)

    async def test_the_suggestion_channel_commands_refuse_what_the_page_refuses(self):
        suggestions = self.cog(Suggestions)
        for channel, sentence in ((self.hidden, "can't see"), (self.read_only, "can't post")):
            ctx = self.context(author=self.manager)
            await suggestions.suggestion_channel.callback(suggestions, ctx, channel)
            self.assertIn(sentence, ctx.fails[0])
            self.assertIsNone(self.settings("suggestions")["channel_id"])


class ReadingTest(ReachCase):
    async def trigger(self, author):
        client = await self.site(author)
        response = await client.post(self.path("/triggers"), json={"trigger": "hello there", "message": {"content": "hi"}})
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())["id"]

    async def test_new_words_on_a_trigger_are_their_writers(self):
        row = await self.trigger(self.admin)
        status, _ = await self.send(self.manager, "PUT", f"/triggers/{row}", {"message": {"content": "{args}"}})
        self.assertEqual(status, 200)
        self.assertEqual(await self.db.scalar("SELECT created_by FROM triggers WHERE id = ?", (row,)), self.manager.id)

    async def test_a_cooldown_changed_on_a_trigger_keeps_its_writer(self):
        row = await self.trigger(self.admin)
        status, _ = await self.send(self.manager, "PUT", f"/triggers/{row}", {"cooldown": 30})
        self.assertEqual(status, 200)
        self.assertEqual(await self.db.scalar("SELECT created_by FROM triggers WHERE id = ?", (row,)), self.admin.id)

    async def report_in(self, channel):
        return await self.db.insert(
            "INSERT INTO reports (guild_id, number, kind, state, reporter_id, reporter_name, target_id, target_name, reason, channel_id, message_id, content, created_at) VALUES (?, 1, 'message', 'open', ?, 'r', ?, 't', 'why', ?, 55, 'secret words', ?)",
            (self.guild.id, self.member.id, self.member.id, channel.id, time.time()),
        )

    async def test_opening_a_review_cant_copy_a_report_from_a_channel_you_cant_read(self):
        report = await self.report_in(self.hidden)
        status, body = await self.send(self.manager, "POST", "/reports/review", {"user_id": str(self.member.id), "report_id": report})
        self.assertEqual(status, 403)
        self.assertIn("can't read", body["error"])

    async def test_a_message_log_takes_being_able_to_read_what_it_copies(self):
        log = FakeChannel(self.guild, "my-log")
        for field in ("message_channel", "default_channel", "moderator_delete_channel"):
            status, body = await self.put(self.manager, "logging", {field: str(log.id)})
            self.assertEqual(status, 400, field)
            self.assertIn("can't read", body["error"], field)
            self.assertEqual((await self.put(self.admin, "logging", {field: str(log.id)}))[0], 200, field)
            await self.bot.settings.save(self.guild.id, "logging", {field: None})
        self.assertEqual((await self.put(self.manager, "logging", {"join_channel": str(log.id)}))[0], 200, "a log of joins copies nobody's words")

    async def test_a_message_log_is_fine_for_someone_who_reads_every_channel(self):
        self.guild.channels.remove(self.hidden)
        log = FakeChannel(self.guild, "my-log")
        self.assertEqual((await self.put(self.manager, "logging", {"message_channel": str(log.id)}))[0], 200)

    async def test_the_log_command_refuses_what_the_page_refuses(self):
        logs = self.cog(Logs)
        ctx = self.context(author=self.manager)
        await logs._set_channel(ctx, "message_channel", FakeChannel(self.guild, "my-log"), "message log channel")
        self.assertIn("can't read", ctx.fails[0])
        self.assertIsNone(self.settings("logging")["message_channel"])

    async def test_taking_a_hidden_channel_off_the_ignore_list_needs_reading_it(self):
        log = FakeChannel(self.guild, "my-log")
        await self.bot.settings.save(self.guild.id, "logging", {"message_channel": log.id, "ignored_channels": [self.hidden.id]})
        status, body = await self.put(self.manager, "logging", {"ignored_channels": []})
        self.assertEqual(status, 400)
        self.assertIn("can't read", body["error"])


class AutomodLogTest(ReachCase):
    modules = [*ReachCase.modules, Automod]

    async def test_an_automod_log_takes_being_able_to_read_what_it_copies(self):
        log = FakeChannel(self.guild, "my-log")
        for field in ("log_channel", "drama_channel"):
            status, body = await self.put(self.manager, "automod", {field: str(log.id)})
            self.assertEqual(status, 400, field)
            self.assertIn("can't read", body["error"])
            self.assertEqual((await self.put(self.admin, "automod", {field: str(log.id)}))[0], 200, field)

    async def test_the_automod_commands_refuse_what_the_page_refuses(self):
        automod = self.cog(Automod)
        ctx = self.context(author=self.manager)
        await automod.automod_log.callback(automod, ctx, channel=FakeChannel(self.guild, "my-log"))
        self.assertIn("can't read", ctx.fails[0])
        self.assertIsNone(self.settings("automod")["log_channel"])

    async def test_someone_who_reads_everything_sets_it(self):
        self.guild.channels.remove(self.hidden)
        self.assertEqual((await self.put(self.manager, "automod", {"log_channel": str(self.channel.id)}))[0], 200)


class LogChannelsMadeForYouTest(ReachCase):
    async def test_making_log_channels_takes_being_able_to_read_what_they_copy(self):
        self.manager_role.permissions = FakePermissions("manage_guild", "manage_channels")
        status, body = await self.send(self.manager, "POST", "/logging/aio")
        self.assertEqual(status, 403)
        self.assertIn("can't read", body["error"])
        logs = self.cog(Logs)
        ctx = self.context(author=self.manager)
        await logs.log_aio.callback(logs, ctx)
        self.assertIn("can't read", ctx.fails[0])


class ServerIdsInListsTest(ReachCase):
    async def test_a_tag_and_a_trigger_refuse_another_servers_ids(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        theirs = other.role("Theirs", 1)
        there = FakeChannel(other, "there")
        for route, form in (("/tags", {"name": "probe", "message": {"content": "x"}}), ("/triggers", {"trigger": "probe words", "message": {"content": "x"}})):
            for field, value in (("role_wl", theirs.id), ("role_bl", theirs.id), ("channel_wl", there.id), ("channel_bl", there.id)):
                status, _ = await self.send(self.admin, "POST", route, {**form, field: [str(value)]})
                self.assertEqual(status, 400, (route, field))
        status, body = await self.send(self.admin, "POST", "/triggers/import", {"data": json.dumps({"triggers": [{"trigger": "words here", "content": "x"}], "ignored_channels": [str(there.id)]})})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.settings("triggers")["ignored_channels"], [])


class FormTest(ReachCase):
    async def test_a_field_the_schema_doesnt_know_isnt_a_server_error(self):
        status, _ = await self.put(self.admin, "core", {"nothing_like_this": 1})
        self.assertNotEqual(status, 500)


class HiddenChannelCommandsTest(ReachCase):
    """What the dashboard refuses for a channel someone can't see, the command refuses too."""
    modules = [*ReachCase.modules, Automod, Starboard]

    async def test_automod_commands_cant_aim_at_a_channel_you_cant_see(self):
        automod = self.cog(Automod)
        word = f"<#{self.hidden.id}>"
        await automod.automod_media.callback(automod, self.context(author=self.manager), channels=word)
        self.assertEqual(self.settings("automod")["media_channels"], [])
        await automod.automod_whitelist_channels.callback(automod, self.context(author=self.manager), "add", channels=word)
        self.assertEqual(self.settings("automod")["allowed_channels"], [])
        await automod.automod_honeypot_add.callback(automod, self.context(author=self.manager), channels=word)
        self.assertEqual(self.settings("automod")["honeypot"]["channels"], [])
        await automod.automod_media.callback(automod, self.context(author=self.manager), channels=f"<#{self.channel.id}>")
        self.assertEqual(self.settings("automod")["media_channels"], [self.channel.id])

    async def test_the_starboard_blacklist_cant_name_a_channel_you_cant_see(self):
        starboard = self.cog(Starboard)
        await starboard.starboard_blacklist.callback(starboard, self.context(author=self.manager), channels=f"<#{self.hidden.id}>")
        self.assertEqual(self.settings("starboard")["blacklist"], [])

    async def test_trigger_ignores_cant_name_a_channel_you_cant_see(self):
        triggers = self.cog(Triggers)
        ctx = self.context(author=self.manager)
        ctx.message.content = ""
        await triggers.triggers_ignore_add.callback(triggers, ctx, None, self.hidden)
        self.assertIn("can't see", ctx.fails[0])
        self.assertEqual(self.settings("triggers")["ignored_channels"], [])

    async def test_the_level_blacklist_cant_name_a_channel_you_cant_see(self):
        levels = self.cog(Levels)
        ctx = self.context(author=self.manager)
        await levels.level_blacklist_add.callback(levels, ctx, targets=f"<#{self.hidden.id}>")
        self.assertIn("can't see", ctx.fails[0])
        self.assertEqual(self.settings("levels")["blacklist_channels"], [])

    async def test_the_suggestion_limit_cant_name_a_channel_you_cant_see(self):
        suggestions = self.cog(Suggestions)
        ctx = self.context(author=self.manager)
        await suggestions.suggestion_submit.callback(suggestions, ctx, self.hidden)
        self.assertIn("can't see", ctx.fails[0])
        self.assertIsNone(self.settings("suggestions")["submit_channel"])


class ImportTest(ReachCase):
    modules = [*ReachCase.modules, Moderation]

    async def test_an_import_keeps_going_past_a_report_channel_they_couldnt_post_in(self):
        code = json.dumps({"modlogs": {"report_channel": str(self.read_only.id)}})
        status, body = await self.send(self.manager, "POST", "/moderation/import", {"code": code})
        self.assertEqual(status, 200, body)
        self.assertIsNone(self.settings("reports")["report_channel"])
        self.assertEqual((await self.send(self.admin, "POST", "/moderation/import", {"code": code}))[0], 200)
        self.assertEqual(self.settings("reports")["report_channel"], self.read_only.id)


class TypedTwinsTest(ReachCase):
    modules = [*ReachCase.modules, Moderation, Roles, Config]

    def decide(self, name, member):
        return self.bot.command_decision(self.bot.get_command(name), member, self.channel)

    async def test_turning_off_a_command_turns_off_its_typed_spelling(self):
        for name, root in (("setnote", "notes"), ("removenote", "notes"), ("clearnotes", "notes"), ("reason", "modlog"), ("addrank", "rank"), ("removerank", "rank"), ("ranks", "rank"), ("allroles", "role"), ("rolediagnose", "role")):
            await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {root: {"enabled": False}}})
            self.assertFalse(self.decide(name, self.admin).allowed, name)
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {}})
        self.assertTrue(self.decide("setnote", self.admin).allowed)

    async def test_a_bypass_role_on_the_command_carries_its_typed_spelling(self):
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"notes": {"bypass_roles": [self.role_mod.id]}}})
        self.assertTrue(self.decide("setnote", self.mod).allowed)
        self.assertFalse(self.decide("setnote", self.member).allowed)

    async def test_a_rule_named_by_the_typed_spelling_lands_on_the_command(self):
        config = self.cog(Config)
        await config.config_disable.callback(config, self.context(author=self.admin), command="setnote")
        self.assertFalse(self.decide("notes setnote", self.admin).allowed)
        self.assertFalse(self.decide("setnote", self.admin).allowed)

    async def test_the_on_and_off_list_leaves_a_twin_out(self):
        config = self.cog(Config)
        ctx = self.context(author=self.admin)
        await config._list_on_and_off(ctx)
        self.assertNotIn("`setnote`", ctx.sent[0]["embed"].fields[0].value)

    async def test_the_commands_page_lists_a_twin_as_a_name_of_its_command(self):
        rows = {row["name"]: row for group in self.bot.command_catalog() for row in group["commands"]}
        self.assertNotIn("setnote", rows)
        self.assertIn("setnote", rows["notes"]["aliases"])


class VoiceAccessRankTest(ReachCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild.id, "voice", {"access_role": self.high.id})

    async def test_a_manager_cant_give_a_role_above_them_on_the_dashboard(self):
        status, body = await self.send(self.manager, "POST", f"/voice/access/{self.manager.id}/grant")
        self.assertEqual(status, 403)
        self.assertIn("highest role", body["error"])
        self.assertNotIn(self.high, self.manager.roles)

    async def test_a_manager_cant_give_a_role_above_them_by_command(self):
        voice = self.cog(Voice)
        ctx = self.context(author=self.manager)
        await voice.vc_access_grant.callback(voice, ctx, self.manager)
        self.assertIn("highest role", ctx.fails[0])
        self.assertNotIn(self.high, self.manager.roles)

    async def test_a_role_below_them_and_an_administrator_still_work(self):
        await self.bot.settings.save(self.guild.id, "voice", {"access_role": self.low.id})
        self.assertEqual((await self.send(self.manager, "POST", f"/voice/access/{self.member.id}/grant"))[0], 200)
        await self.bot.settings.save(self.guild.id, "voice", {"access_role": self.high.id})
        self.assertEqual((await self.send(self.admin, "POST", f"/voice/access/{self.member.id}/grant"))[0], 200)
        self.assertIn(self.high, self.member.roles)

    async def test_the_request_button_goes_where_they_could_post(self):
        status, body = await self.send(self.manager, "POST", "/voice/button", {"channel_id": str(self.read_only.id)})
        self.assertEqual(status, 400)
        self.assertIn("post in that channel", body["error"])
