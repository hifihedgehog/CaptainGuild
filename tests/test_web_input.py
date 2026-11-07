"""What the site does with odd input, and the routes that act as someone: posting, setup, signing out, limits."""
from __future__ import annotations

import asyncio
import json
import time
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from captainguild.core import ops as ops_module
from captainguild.core.ops import DiscordOps
from captainguild.core.validate import Invalid, number
from captainguild.modules.config import Config
from captainguild.modules.embeds import Embeds
from captainguild.modules.feeds import Feeds
from captainguild.modules.moderation import Moderation
from captainguild.modules.mydata import MyData
from captainguild.modules.notifications import Notifications
from captainguild.modules.reactionroles import ReactionRoles
from captainguild.modules.setup import Setup
from captainguild.web import app as app_module
from captainguild.web.api import User, json_response
from captainguild.web.app import SITE, build_app, csrf_for
from tests.fakes import BotTest, FakeChannel, FakeMember, FakePermissions
from tests.test_access_limits import HiddenChannel

KEY = b"t" * 32


class ReadOnly(FakeChannel):
    def __init__(self, guild, name, person, *names):
        super().__init__(guild, name)
        self.person = person
        self.names = names or ("view_channel", "read_message_history")

    def permissions_for(self, member):
        return FakePermissions(*self.names) if member is self.person else member.guild_permissions


class SiteCase(BotTest):
    modules = [Config, MyData, Moderation, Setup, Embeds, Feeds, Notifications, ReactionRoles]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.manager_role = self.guild.role("Manager", 4, "manage_guild", "view_channel", "send_messages")
        self.manager = FakeMember(self.guild, "manager", self.manager_role)

    async def site(self, member):
        app = build_app(self.bot, session_key=KEY, public_url="", secure_cookies=False, demo_user=User(member.id, member.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def real(self, secret="", public="https://bot.example"):
        app = build_app(self.bot, session_key=KEY, client_id="1", client_secret=secret, public_url=public, secure_cookies=False)
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def sign_in(self, client, member):
        token = self.bot.sign_in_links.create(member.id, member.name, None, "/servers")
        return await client.post(f"/login/link/{token}", allow_redirects=False, headers={"Sec-Fetch-Site": "same-origin"})

    def session_cookie(self, client):
        return next((cookie.value for cookie in client.session.cookie_jar if cookie.key == "cg_session"), None)


class PostingThroughTheBotTest(SiteCase):
    async def post(self, member, channel, **extra):
        client = await self.site(member)
        return await client.post(self.path("/embeds/post"), json={"channel_id": str(channel.id), "payload": {"content": "@everyone hi"}, **extra})

    async def test_a_read_only_channel_refuses_a_manager(self):
        news = ReadOnly(self.guild, "news", self.manager)
        response = await self.post(self.manager, news)
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["fields"]["channel_id"], "You can't post in that channel.")
        self.assertEqual(news.sent, [])

    async def test_a_ping_needs_mention_everyone(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        response = await self.post(self.manager, news, ping=True)
        self.assertEqual(response.status, 400)
        self.assertIn("Mention Everyone", (await response.json())["fields"]["ping"])
        self.assertEqual(news.sent, [])

    async def test_a_plain_post_passes_and_a_ping_with_the_permission_passes(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        self.assertEqual((await self.post(self.manager, news)).status, 200)
        news.names = ("view_channel", "send_messages", "mention_everyone")
        self.assertEqual((await self.post(self.manager, news, ping=True)).status, 200)

    async def test_the_owner_and_administrators_pass(self):
        news = ReadOnly(self.guild, "news", self.manager)
        self.assertEqual((await self.post(self.admin, news, ping=True)).status, 200)

    async def test_a_scheduled_post_takes_the_same_checks(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        response = await self.post(self.manager, news, ping=True, at=time.time() + 3600)
        self.assertEqual(response.status, 400)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM scheduled_posts"), 0)

    async def scheduled(self, channel, ping=False):
        client = await self.site(self.admin)
        response = await client.post(self.path("/embeds/post"), json={"channel_id": str(channel.id), "payload": {"content": "hi"}, "ping": ping, "at": time.time() + 3600})
        return (await response.json())["scheduled"]

    async def test_a_reschedule_checks_only_what_it_changes(self):
        news = ReadOnly(self.guild, "news", self.manager)
        post = await self.scheduled(news, ping=True)
        client = await self.site(self.manager)
        moved = await client.patch(self.path(f"/embeds/scheduled/{post}"), json={"at": time.time() + 7200})
        self.assertEqual(moved.status, 200, "a new time on a row an Administrator set up")
        edited = await client.patch(self.path(f"/embeds/scheduled/{post}"), json={"payload": {"content": "changed"}})
        self.assertEqual(edited.status, 400, "changed text goes out as theirs")

    async def test_a_reschedule_cant_turn_on_a_ping_they_cant_send(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        post = await self.scheduled(news)
        client = await self.site(self.manager)
        response = await client.patch(self.path(f"/embeds/scheduled/{post}"), json={"ping": True})
        self.assertEqual(response.status, 400)
        self.assertEqual(await self.db.scalar("SELECT ping FROM scheduled_posts WHERE id = ?", (post,)), 0)


class SavedPingsTest(SiteCase):
    def form(self, channel, mention):
        return {"channel_id": str(channel.id), "mention": mention, "message": {"content": "hi"}, "when": time.time() + 3600, "repeat": [0, 0, 0, 0]}

    async def test_an_autofeed_cant_ping_everyone_for_someone_who_cant(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        client = await self.site(self.manager)
        for mention in ("everyone", "here"):
            response = await client.post(self.path("/autofeeds"), json=self.form(news, mention))
            self.assertEqual(response.status, 400, mention)
        self.assertEqual((await client.post(self.path("/autofeeds"), json=self.form(news, "silent"))).status, 200)
        news.names = ("view_channel", "send_messages", "mention_everyone")
        self.assertEqual((await client.post(self.path("/autofeeds"), json=self.form(news, "everyone"))).status, 200)

    async def test_an_edit_keeps_an_existing_ping(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        admin = await self.site(self.admin)
        created = await (await admin.post(self.path("/autofeeds"), json=self.form(news, "everyone"))).json()
        client = await self.site(self.manager)
        response = await client.put(self.path(f"/autofeeds/{created['id']}"), json=self.form(news, "everyone"))
        self.assertEqual(response.status, 200, await response.text())
        silent = await (await admin.post(self.path("/autofeeds"), json=self.form(news, "silent"))).json()
        response = await client.put(self.path(f"/autofeeds/{silent['id']}"), json=self.form(news, "everyone"))
        self.assertEqual(response.status, 400)

    async def test_an_alert_format_with_everyone_needs_the_permission(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        client = await self.site(self.manager)
        response = await client.put(self.path("/settings/notifications"), json={"alert_channel": str(news.id), "free_format": "{everyone} free"})
        self.assertEqual(response.status, 400)
        self.assertIn("Mention Everyone", (await response.json())["fields"]["free_format"])
        response = await client.put(self.path("/settings/notifications"), json={"alert_channel": str(news.id), "free_format": "free"})
        self.assertEqual(response.status, 200)

    async def test_an_alert_channel_needs_send_there(self):
        news = ReadOnly(self.guild, "news", self.manager)
        client = await self.site(self.manager)
        response = await client.put(self.path("/settings/notifications"), json={"alert_channel": str(news.id)})
        self.assertEqual(response.status, 400)

    async def test_a_followed_stream_format_with_everyone_needs_the_permission(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        await self.db.run(
            "INSERT INTO twitch_subs (guild_id, stream_id, stream_login, stream_name, channel_id, fmt, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.guild.id, "1", "x", "x", news.id, "live", self.admin.id, time.time()),
        )
        client = await self.site(self.manager)
        response = await client.put(self.path("/twitch/1"), json={"channel_id": str(news.id), "fmt": "{everyone} live"})
        self.assertEqual(response.status, 400)
        self.assertEqual((await client.put(self.path("/twitch/1"), json={"channel_id": str(news.id), "fmt": "now live"})).status, 200)


class InfoTest(SiteCase):
    async def test_a_manager_gets_a_row_for_a_hidden_channel_with_nothing_in_it(self):
        secret = HiddenChannel(self.guild, "owners-only", {self.admin.id, self.owner.id, self.me.id})
        client = await self.site(self.manager)
        info = await (await client.get(self.path("/info"))).json()
        row = next(row for row in info["channels"] if int(row["id"]) == secret.id)
        self.assertEqual(row["name"], "Hidden channel")
        self.assertTrue(row["hidden"])
        self.assertFalse(row["nsfw"])
        self.assertIsNone(row["parent_id"])
        self.assertNotIn("owners-only", json.dumps(info))
        self.assertFalse(info["is_admin"])

    async def test_an_administrator_gets_every_name(self):
        HiddenChannel(self.guild, "owners-only", {self.admin.id, self.owner.id, self.me.id})
        info = await (await (await self.site(self.admin)).get(self.path("/info"))).json()
        self.assertIn("owners-only", [row["name"] for row in info["channels"]])
        self.assertTrue(info["is_admin"])

    async def test_each_row_says_whether_they_can_post_and_ping(self):
        news = ReadOnly(self.guild, "news", self.manager, "view_channel", "send_messages")
        client = await self.site(self.manager)
        info = await (await client.get(self.path("/info"))).json()
        row = next(row for row in info["channels"] if int(row["id"]) == news.id)
        self.assertTrue(row["can_post"])
        self.assertFalse(row["can_ping"])


class SetupReachTest(SiteCase):
    async def test_making_channels_takes_manage_channels(self):
        client = await self.site(self.manager)
        for tail, body in (("/setup/channel", {"name": "x"}), ("/setup/modlog-channel", {})):
            response = await client.post(self.path(tail), json=body)
            self.assertEqual(response.status, 403, tail)
            self.assertIn("Manage Channels", (await response.json())["error"])

    async def test_making_roles_takes_manage_roles(self):
        client = await self.site(self.manager)
        for tail, body in (("/setup/mute-role", {}), ("/setup/role-template", {"template": "colors"})):
            response = await client.post(self.path(tail), json=body)
            self.assertEqual(response.status, 403, tail)
            self.assertIn("Manage Roles", (await response.json())["error"])
        self.assertIsNone(self.settings("moderation")["mute_role"])

    async def test_someone_who_holds_them_passes(self):
        builder = FakeMember(self.guild, "builder", self.guild.role("Builder", 6, "manage_guild", "manage_channels", "view_channel"))
        client = await self.site(builder)
        self.assertEqual((await client.post(self.path("/setup/channel"), json={"name": "x"})).status, 200)

    async def test_the_mute_role_is_made_once(self):
        client = await self.site(self.admin)
        first = await (await client.post(self.path("/setup/mute-role"))).json()
        second = await (await client.post(self.path("/setup/mute-role"))).json()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.settings("moderation")["mute_role"], int(first["id"]))

    async def test_a_new_mute_role_replaces_one_that_was_deleted(self):
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": 123456789012345678})
        client = await self.site(self.admin)
        answer = await (await client.post(self.path("/setup/mute-role"))).json()
        self.assertNotEqual(int(answer["id"]), 123456789012345678)

    async def test_the_mod_log_channel_is_made_once(self):
        client = await self.site(self.admin)
        first = await (await client.post(self.path("/setup/modlog-channel"))).json()
        second = await (await client.post(self.path("/setup/modlog-channel"))).json()
        self.assertEqual(first["id"], second["id"])

    async def test_a_template_doesnt_reuse_a_role_above_the_person(self):
        strong = self.guild.role("Red", 7)
        builder = FakeMember(self.guild, "builder", self.guild.role("Builder", 6, "manage_guild", "manage_roles", "view_channel"))
        client = await self.site(builder)
        answer = await (await client.post(self.path("/setup/role-template"), json={"template": "colors"})).json()
        self.assertNotIn(str(strong.id), [str(row["id"]) for row in answer["roles"]])


class OddInputTest(SiteCase):
    async def test_a_lone_surrogate_is_refused_and_stores_nothing(self):
        client = await self.site(self.admin)
        response = await client.put(self.path("/settings/core"), data=b'{"prefixes":["\\ud800"]}', headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.settings("core")["prefixes"], ["!", "?"])
        self.assertEqual((await client.get(self.path("/settings/core"))).status, 200)

    def test_an_old_row_with_a_surrogate_can_still_be_sent(self):
        self.assertEqual(json.loads(json_response({"a": "\ud800"}).text)["a"], "\ud800")

    async def test_nesting_and_not_a_number_are_refused(self):
        client = await self.site(self.admin)
        for body in (b'{"prefixes":' + b"[" * 100000 + b"]" * 100000 + b"}", b'{"prefixes": NaN}', b'{"prefixes": Infinity}'):
            response = await client.put(self.path("/settings/core"), data=body)
            self.assertEqual(response.status, 400, body[:30])

    async def test_a_body_that_isnt_an_object_is_refused(self):
        client = await self.site(self.admin)
        for tail in ("/embeds/post", "/setup/channel"):
            response = await client.post(self.path(tail), json=[1, 2])
            self.assertEqual(response.status, 400, tail)

    async def test_digits_that_arent_plain(self):
        client = await self.site(self.admin)
        for query in ("99999999999999999999999", "²", "٣"):
            self.assertEqual((await client.get(self.path("/activity"), params={"before": query})).status, 200, ascii(query))
        self.assertEqual((await client.get(self.path("/members/search"), params={"q": "²"})).status, 200)
        self.assertEqual((await client.get("/api/g/" + "9" * 5000 + "/info")).status, 403)

    async def test_my_data_delete_with_odd_values(self):
        client = await self.site(self.admin)
        for body in ({"section": []}, {"section": {}}, {"section": "birthday", "guild_id": "²"}, {"section": "birthday", "guild_id": "9" * 5000}):
            self.assertEqual((await client.post("/api/my-data/delete", json=body)).status, 400, ascii(str(body))[:40])
        self.assertEqual((await client.post("/api/my-data/delete", data=b"[" * 200000)).status, 400)
        self.assertEqual((await client.post("/api/my-data/delete", data=b'{"section":"\\ud800"}')).status, 400)

    async def test_a_long_digit_string_in_a_form_isnt_read_as_an_id(self):
        client = await self.site(self.admin)
        response = await client.put(self.path("/settings/moderation"), json={"modlog_channel": "²" * 20})
        self.assertEqual(response.status, 400)

    async def test_a_string_of_superscripts_in_any_field_doesnt_break_the_id_scan(self):
        self.assertEqual(list(app_module._ids_in("²" * 20)), [])
        self.assertEqual(list(app_module._ids_in("9" * 5000)), [])
        self.assertEqual(list(app_module._ids_in("123456789012345678")), [123456789012345678])

    async def test_bad_bytes_in_a_cookie_read_as_signed_out(self):
        client = await self.real()
        for path in (b"/api/me", b"/login"):
            reader, writer = await asyncio.open_connection(client.host, client.port)
            writer.write(b"GET " + path + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\nCookie: cg_session=\xff\xfe\r\n\r\n")
            await writer.drain()
            status = await reader.readline()
            writer.close()
            self.assertIn(b" 200 ", status, path)

    async def test_a_csrf_token_that_isnt_ascii_is_refused_as_expired(self):
        client = await self.real()
        await self.sign_in(client, self.admin)
        response = await client.put(self.path("/settings/core"), json={"prefixes": ["?"]}, headers={"X-CSRF-Token": "é".encode("utf-8").decode("latin-1")})
        self.assertEqual(response.status, 403)

    async def test_a_state_that_isnt_ascii_fails_the_sign_in(self):
        client = await self.real(secret="s")
        await client.get("/login", allow_redirects=False)
        response = await client.get("/login/callback?state=%C3%A9&code=x", allow_redirects=False)
        self.assertEqual(response.status, 400)
        self.assertIn("Try again", await response.text())

    def test_a_huge_number_reads_as_not_a_number(self):
        with self.assertRaises(Invalid):
            number()(10 ** 400)
        with self.assertRaises(Invalid):
            number()(float("inf"))
        self.assertEqual(number()(5), 5.0)

    async def test_pasted_reaction_roles_with_the_wrong_shapes_are_skipped(self):
        client = await self.site(self.admin)
        cases = (
            {"message_id": "123456789012345678", "messages": [5]},
            {"message_id": "123456789012345678", "messages": [None]},
        )
        for case in cases:
            response = await client.post(self.path("/reactionroles/import"), json={"text": json.dumps({"rr": [case]})})
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual(len((await response.json())["skipped"]), 1, case)


class LogoutTest(SiteCase):
    async def test_another_site_cant_sign_someone_out(self):
        client = await self.real()
        await self.sign_in(client, self.admin)
        response = await client.post("/logout", allow_redirects=False, headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM web_sessions"), 1)

    async def test_this_site_can(self):
        client = await self.real()
        await self.sign_in(client, self.admin)
        response = await client.post("/logout", allow_redirects=False, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status, 302)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM web_sessions"), 0)

    async def test_signing_out_ends_only_this_session_unless_asked(self):
        client = await self.real()
        await app_module.Sessions(self.db).create(User(self.admin.id, "a", None))
        await app_module.Sessions(self.db).create(User(self.mod.id, "m", None))
        await self.sign_in(client, self.admin)
        await client.post("/logout", allow_redirects=False, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM web_sessions WHERE user_id = ?", (self.admin.id,)), 1)

    async def test_everywhere_ends_every_session_of_that_person_alone(self):
        client = await self.real()
        await app_module.Sessions(self.db).create(User(self.admin.id, "a", None))
        await app_module.Sessions(self.db).create(User(self.mod.id, "m", None))
        await self.sign_in(client, self.admin)
        response = await client.post("/logout", data={"everywhere": "1"}, allow_redirects=False, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status, 302)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM web_sessions WHERE user_id = ?", (self.admin.id,)), 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM web_sessions WHERE user_id = ?", (self.mod.id,)), 1)

    async def test_everywhere_with_no_session_ends_nothing(self):
        client = await self.real()
        await app_module.Sessions(self.db).create(User(self.mod.id, "m", None))
        await client.post("/logout", data={"everywhere": "1"}, allow_redirects=False, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM web_sessions"), 1)


class FlowCookieTest(SiteCase):
    async def test_a_plain_visit_to_login_doesnt_change_where_the_link_goes(self):
        client = await self.real()
        await client.get("/login", allow_redirects=False)
        token = self.bot.sign_in_links.create(self.admin.id, "a", None, "/dashboard/7")
        response = await client.post(f"/login/link/{token}", allow_redirects=False, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.headers["Location"], "/dashboard/7")

    async def test_a_named_destination_still_wins(self):
        client = await self.real()
        await client.get("/login?next=/dashboard/5", allow_redirects=False)
        token = self.bot.sign_in_links.create(self.admin.id, "a", None, "/servers")
        response = await client.post(f"/login/link/{token}", allow_redirects=False, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.headers["Location"], "/dashboard/5")


class ReadLimitsTest(SiteCase):
    async def test_me_doesnt_describe_the_whole_server(self):
        client = await self.real()
        await self.sign_in(client, self.admin)
        with mock.patch.object(ops_module, "_members_per_role", side_effect=AssertionError("walked every member")):
            response = await client.get("/api/me")
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["guilds"][0]["name"], self.guild.name)

    async def test_reads_have_a_limit_per_person(self):
        client = await self.site(self.admin)
        client.app[SITE].read_limiter.windows[self.admin.id] = (time.monotonic(), app_module.READS_PER_MINUTE)
        response = await client.get(self.path("/info"))
        self.assertEqual(response.status, 429)
        other = await self.site(self.owner)
        self.assertEqual((await other.get(self.path("/info"))).status, 200)
