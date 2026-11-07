"""Levels, feeds and alerts through the real library with only the network faked: real members, roles, channels and interactions."""
from __future__ import annotations

import io
import time

import discord
from PIL import Image

from captainguild.core.ops import DiscordOps
from captainguild.modules.feeds import Feeds
from captainguild.modules.levels import Levels, xp_for_level
from captainguild.modules.notifications import GAMERPOWER_URL, TWITCH_API, TWITCH_TOKEN_URL, YOUTUBE_FEED, Notifications
from captainguild.modules.personalize import Personalize
from tests.fake_outside import FakeOutside, reply
from tests.gateway import GatewayTest, new_id, user_json
from tests.test_notifications import CHANNEL, feed, giveaway


def picture(width: int = 128, height: int = 128) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (20, 90, 200)).save(out, format="PNG")
    return out.getvalue()


class RealLibraryCase(GatewayTest):
    modules = [Levels, Personalize, Notifications, Feeds]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.levels = self.bot.get_cog("Levels")
        self.notify = self.bot.get_cog("Notifications")
        self.feeds = self.bot.get_cog("Feeds")
        self.web = FakeOutside()
        self.levels.outside = self.web
        self.notify.outside = self.web
        self.channel = self.guild.get_channel(int(self.text["id"]))

    def calls_to(self, key: str) -> list[dict]:
        method, path = key.split(" ", 1)
        return [body for called_method, called_path, body in self.net.calls if called_method == method and called_path == path]

    async def slash(self, name: str, *path: str, user_id: int | None = None, **options) -> None:
        """Run a slash command the way Discord delivers one."""
        data = []
        for key, value in options.items():
            kind = 3
            if isinstance(value, bool):
                kind = 5
            elif isinstance(value, int):
                kind = 4
            data.append({"name": key, "type": kind, "value": value})
        for index, part in enumerate(reversed(path)):
            data = [{"name": part, "type": 1 if index == 0 else 2, "options": data}]
        payload = {"id": str(new_id()), "name": name, "type": 1, "options": data, "resolved": {}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, payload, user_id=user_id or self.owner_id))
        await self.settle()
        self.assert_no_errors(f"/{name} {' '.join(path)}")

    def text_message(self, content: str, user_id: int | None = None) -> None:
        self.dispatch_message(user_id=user_id or self.owner_id, content=content)


class LevelsThroughTheLibrary(RealLibraryCase):
    async def test_a_message_levels_someone_up_and_gives_the_reward(self):
        reward = self.guild.get_role(int(self.role_muted["id"]))
        await self.bot.settings.save(self.guild_id, "levels", {
            "enabled": True, "custom_xp": 100, "destination": "same", "message": {"content": "{user} reached {lvl} {mention}"},
            "rewards": [{"level": 1, "role_id": reward.id}],
        })
        self.dispatch_message(user_id=self.member_id, content="hello")
        await self.settle()
        self.assert_no_errors("level-up")
        self.assertEqual(self.net.posted[-1]["content"], f"member reached 1 <@{self.member_id}>")
        self.assertEqual(len(self.calls_to("PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id}")), 1)
        await self.levels.flush()
        self.assertEqual(await self.db.scalar("SELECT xp FROM level_members WHERE user_id = ?", (self.member_id,)), 100)

    async def test_voice_xp_reads_real_voice_states(self):
        await self.bot.settings.save(self.guild_id, "levels", {"enabled": True, "voice_xp": True, "custom_xp": 10})
        state = self.bot._connection
        for user_id in (self.member_id, self.mod_id):
            state.parse_voice_state_update({"guild_id": str(self.guild_id), "channel_id": self.voice["id"], "user_id": str(user_id), "member": self.member_json[user_id], "session_id": "s", "deaf": False, "mute": False, "self_deaf": False, "self_mute": False, "self_video": False, "suppress": False, "request_to_speak_timestamp": None})
        await self.settle()
        await self.levels.voice_tick()
        await self.levels.flush()
        self.assertEqual(await self.db.scalar("SELECT xp FROM level_members WHERE user_id = ?", (self.member_id,)), 10)
        self.assertEqual(await self.db.scalar("SELECT voice_minutes FROM level_members WHERE user_id = ?", (self.mod_id,)), 1)

    async def test_the_rank_card_goes_out_as_a_file_on_a_slash_command(self):
        await self.bot.settings.save(self.guild_id, "levels", {"enabled": True})
        await self.levels.set_xp(self.guild_id, self.member_id, 500, name="member")
        parts = []
        original = self.net.webhook

        async def watching(adapter, route, session=None, *, multipart=None, **kwargs):
            parts.extend(multipart or [])
            return await original(adapter, route, session, multipart=multipart, **kwargs)
        self.net.webhook = watching
        await self.slash("level", "member")
        self.assertIn("rank.png", [part.get("filename") for part in parts])

    async def test_the_leaderboard_buttons_turn_pages(self):
        await self.bot.settings.save(self.guild_id, "levels", {"enabled": True})
        for index in range(25):
            await self.levels.set_xp(self.guild_id, new_id(), 5000 - index, name=f"person{index}")
        await self.slash("leaderboard", user_id=self.member_id)
        message = self.net.posted[-1]
        self.assertIn("person0", message["embeds"][0]["description"])
        buttons = message["components"][0]["components"]
        self.assertEqual([b["label"] for b in buttons], ["Previous", "Next"])
        self.assertTrue(buttons[0]["disabled"])
        press = {"custom_id": buttons[1]["custom_id"], "component_type": 2}
        self.bot._connection.parse_interaction_create(self.interaction_payload(3, press, user_id=self.member_id, message=message))
        await self.settle()
        self.assert_no_errors("next page")
        self.assertIn("person10", self.net.posted[-1]["embeds"][0]["description"])

    async def test_text_commands_parse_their_arguments(self):
        for line in ("!level rate 2/60", "!level message set Up to level {lvl} now", "!level mod 5", "!level reward 5 Member", "!level blacklist add general Muted", "!level toggle dm", "!level config color ff8800"):
            self.text_message(line)
            await self.settle()
            self.assert_no_errors(line)
        cfg = self.bot.settings.get(self.guild_id, "levels")
        self.assertEqual((cfg["rate"], cfg["window"]), (2, 60))
        self.assertEqual(cfg["message"]["content"], "Up to level {lvl} now")
        self.assertEqual(cfg["modulo"], 5)
        self.assertEqual(cfg["rewards"], [{"level": 5, "role_id": int(self.role_member["id"])}])
        self.assertEqual((cfg["blacklist_channels"], cfg["blacklist_roles"]), ([int(self.text["id"])], [int(self.role_muted["id"])]))
        self.assertEqual(cfg["destination"], "dm")
        self.assertEqual(cfg["card_color"], 0xFF8800)

    async def test_a_member_cannot_use_the_manager_commands(self):
        await self.levels.set_xp(self.guild_id, self.mod_id, 500)
        await self.slash("level", "reset", choice="server", user_id=self.member_id)
        await self.levels.flush()
        self.assertEqual(await self.db.scalar("SELECT xp FROM level_members WHERE user_id = ?", (self.mod_id,)), 500)
        self.text_message("!level mod 5", self.member_id)
        await self.settle()
        self.assertEqual(self.bot.settings.get(self.guild_id, "levels")["modulo"], 1)
        self.assertIn("You need", self.net.posted[-1]["content"])

    async def test_a_returning_member_gets_rewards_from_the_real_join_event(self):
        reward = self.guild.get_role(int(self.role_muted["id"]))
        await self.bot.settings.save(self.guild_id, "levels", {"enabled": True, "rewards": [{"level": 1, "role_id": reward.id}]})
        await self.levels.set_xp(self.guild_id, self.member_id, 500)
        state = self.bot._connection
        state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.member_id]["user"]})
        await self.settle()
        self.assertIsNotNone(await self.db.scalar("SELECT left_at FROM level_members WHERE user_id = ?", (self.member_id,)))
        state.parse_guild_member_add({**self.member_json[self.member_id], "guild_id": str(self.guild_id), "roles": []})
        await self.settle()
        self.assert_no_errors("rejoin")
        self.assertIsNone(await self.db.scalar("SELECT left_at FROM level_members WHERE user_id = ?", (self.member_id,)))
        self.assertEqual(len(self.calls_to("PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id}")), 1)


class FeedsThroughTheLibrary(RealLibraryCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.role = self.guild.get_role(int(self.role_member["id"]))
        bot_role = self.guild.get_role(int(self.role_bot["id"]))
        bot_role._permissions = discord.Permissions(view_channel=True, send_messages=True, embed_links=True, manage_roles=True).value

    async def test_the_role_opens_for_the_send_and_closes_after(self):
        self.text_message(f"!feeds create news <@&{self.role.id}>")
        await self.settle()
        self.text_message("!feeds announce news The patch is out")
        await self.settle()
        self.assert_no_errors("announce")
        kinds = [(method, path, body) for method, path, body in self.net.calls if path == "/guilds/{guild_id}/roles/{role_id}" or (path == "/channels/{channel_id}/messages" and "patch is out" in (body.get("content") or ""))]
        self.assertEqual([k[0] for k in kinds], ["PATCH", "POST", "PATCH"])
        self.assertEqual(kinds[0][2]["mentionable"], True)
        self.assertEqual(kinds[2][2]["mentionable"], False)
        sent = kinds[1][2]
        self.assertEqual(sent["content"], f"<@&{self.role.id}> The patch is out")
        self.assertEqual(sent["allowed_mentions"]["roles"], [self.role.id])
        self.assertNotIn("everyone", sent["allowed_mentions"]["parse"])

    async def test_with_mention_everyone_no_role_edit_is_needed(self):
        bot_role = self.guild.get_role(int(self.role_bot["id"]))
        bot_role._permissions = discord.Permissions(view_channel=True, send_messages=True, mention_everyone=True).value
        self.text_message(f"!feeds create news <@&{self.role.id}>")
        await self.settle()
        self.text_message("!feeds announce news Hello")
        await self.settle()
        self.assertEqual(self.calls_to("PATCH /guilds/{guild_id}/roles/{role_id}"), [])

    async def test_an_autofeed_posts_from_its_timer_with_its_embed(self):
        await self.feeds._insert(self.guild_id, int(self.text["id"]), "everyone", None, {"content": "On {server} at {unix}", "embeds": [{"title": "Reset in {channel}"}], "buttons": [{"label": "Read", "url": "https://example.com/"}]}, time.time() + 1, (0, 1, 0, 0), None, None)
        await self.bot.timers.run_due(time.time() + 5)
        await self.settle()
        self.assert_no_errors("autofeed")
        posted = self.net.posted[-1]
        self.assertTrue(posted["content"].startswith("@everyone On Test Guild at "))
        self.assertEqual(posted["embeds"][0]["title"], "Reset in general")
        self.assertEqual(posted["components"][0]["components"][0]["url"], "https://example.com/")
        self.assertIsNotNone(await self.bot.timers.get_key("autofeed", "1"))

    async def test_text_commands_for_autofeeds(self):
        self.text_message("!feeds autofeeds silent 2h The servers restart soon")
        await self.settle()
        self.assert_no_errors("create")
        row = await self.db.one("SELECT * FROM autofeeds")
        self.assertEqual(row["mention"], "silent")
        self.assertIn("The servers restart soon", row["payload"])
        self.text_message(f"!feeds autofeeds repeat {row['id']} 1d")
        await self.settle()
        self.assertEqual((await self.db.one("SELECT repeat_days FROM autofeeds"))["repeat_days"], 1)
        self.text_message("!feeds autofeeds")
        await self.settle()
        self.assertIn("The servers restart soon", self.net.posted[-1]["embeds"][0]["description"])
        self.assert_no_errors("list")

    async def test_a_name_autocompletes(self):
        await self.db.run("INSERT INTO feeds (guild_id, name, role_id, channel_id, created_at) VALUES (?, 'news', ?, ?, 1)", (self.guild_id, self.role.id, int(self.text["id"])))
        data = {"id": str(new_id()), "name": "feeds", "type": 1, "options": [{"name": "delete", "type": 1, "options": [{"name": "name", "type": 3, "value": "ne", "focused": True}]}]}
        self.bot._connection.parse_interaction_create(self.interaction_payload(4, data, user_id=self.owner_id))
        await self.settle()
        self.assert_no_errors("autocomplete")
        answers = [body for method, path, body in self.net.calls if body.get("type") == 8]
        self.assertEqual(answers[-1]["data"]["choices"], [{"name": "news", "value": "news"}])


class AlertsThroughTheLibrary(RealLibraryCase):
    async def test_a_twitch_alert_goes_out_with_only_the_pings_it_should(self):
        await self.bot.settings.save(self.guild_id, "notifications", {"twitch_client_id": "client-id", "twitch_client_secret": "secret"})
        self.web.answer(TWITCH_TOKEN_URL, {"access_token": "t", "expires_in": 5000})
        self.web.answer(f"{TWITCH_API}/users", {"data": [{"id": "111", "login": "sampleplays", "display_name": "SamplePlays"}]})
        self.web.answer(f"{TWITCH_API}/streams", {"data": [{"id": "s1", "user_id": "111", "user_login": "sampleplays", "user_name": "SamplePlays", "game_name": "Chess", "title": f"<@&{self.role_mod['id']}> @everyone"}]})
        self.text_message("!twitch add sampleplays")
        await self.settle()
        self.assert_no_errors("twitch add")
        await self.db.run("UPDATE twitch_subs SET fmt = ?, role_id = ?", ("{name} {title}", int(self.role_member["id"])))
        await self.notify.poll_twitch()
        posted = [body for method, path, body in self.net.calls if path == "/channels/{channel_id}/messages" and "SamplePlays" in (body.get("content") or "") and "@everyone" in (body.get("content") or "")][-1]
        self.assertEqual(posted["allowed_mentions"]["roles"], [int(self.role_member["id"])])
        self.assertEqual(posted["allowed_mentions"]["parse"], [])
        self.assert_no_errors("twitch alert")

    async def test_a_youtube_alert_goes_out(self):
        self.web.answer(f"https://www.youtube.com/channel/{CHANNEL}", f'<meta property="og:title" content="Sample"><script>"externalId":"{CHANNEL}"</script>')
        self.web.answer(YOUTUBE_FEED, reply(feed("aaaaaaaaaaa")))
        self.text_message(f"!youtube add {CHANNEL}")
        await self.settle()
        self.assert_no_errors("youtube add")
        self.web.answer(YOUTUBE_FEED, reply(feed("bbbbbbbbbbb", "aaaaaaaaaaa")))
        await self.notify.poll_youtube()
        self.assert_no_errors("youtube alert")
        self.assertIn("https://www.youtube.com/watch?v=bbbbbbbbbbb", self.net.posted[-1]["content"])

    async def test_a_free_game_goes_out_with_its_link_buttons(self):
        await self.bot.settings.save(self.guild_id, "notifications", {"alert_channel": int(self.text["id"])})
        self.web.answer(GAMERPOWER_URL, [])
        await self.notify.poll_free_games()
        self.web.answer(GAMERPOWER_URL, [giveaway(1)])
        await self.notify.poll_free_games()
        self.assert_no_errors("free game")
        posted = self.net.posted[-1]
        self.assertEqual(posted["embeds"][0]["title"], "Cool Game")
        urls = [button["url"] for button in posted["components"][0]["components"]]
        self.assertEqual(urls, ["https://www.gamerpower.com/open/1", "https://www.gamerpower.com"])

    async def test_games_alerts_runs_as_a_slash_command(self):
        await self.slash("games", "alerts", config="deals-on")
        self.assertTrue(self.bot.settings.get(self.guild_id, "notifications")["deals_enabled"])
        self.text_message("!games alerts free-off")
        await self.settle()
        self.assert_no_errors("games alerts")
        self.assertFalse(self.bot.settings.get(self.guild_id, "notifications")["alerts_enabled"])


class ProfileThroughTheLibrary(RealLibraryCase):
    async def test_the_real_operation_sends_data_uris(self):
        ops = DiscordOps(self.bot)
        await ops.set_bot_profile(self.guild_id, avatar=picture(), banner=picture(680, 240), bio="Hello")
        body = self.calls_to("PATCH /guilds/{guild_id}/members/@me")[-1]
        self.assertTrue(body["avatar"].startswith("data:image/png;base64,"))
        self.assertTrue(body["banner"].startswith("data:image/png;base64,"))
        self.assertEqual(body["bio"], "Hello")
        await ops.set_bot_profile(self.guild_id, avatar=None, banner=None, bio=None)
        self.assertEqual(self.calls_to("PATCH /guilds/{guild_id}/members/@me")[-1], {"avatar": None, "banner": None, "bio": None})
