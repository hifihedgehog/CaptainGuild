"""Signing in with a one-time link from /dashboard, which needs only the bot token, and running without Message Content."""
from __future__ import annotations

import time
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from captainguild import __main__ as program
from captainguild.core.perms import MODULE as COMMANDS_MODULE
from captainguild.core.signin import LINK_SECONDS, MAX_LINKS, SignInLinks
from captainguild.modules.config import Config
from captainguild.web.app import SESSION_COOKIE, build_app
from tests.fakes import BotTest
from tests.gateway import GatewayTest

PUBLIC = "https://bot.example"


class Links(unittest.TestCase):
    def test_works_once(self) -> None:
        links = SignInLinks()
        token = links.create(1, "Ann", None, "/servers")
        self.assertEqual(links.peek(token).user_id, 1)
        self.assertEqual(links.peek(token).user_id, 1)
        self.assertEqual(links.take(token).name, "Ann")
        self.assertIsNone(links.take(token))
        self.assertIsNone(links.peek(token))

    def test_expires(self) -> None:
        links = SignInLinks()
        token = links.create(1, "Ann", None, "/servers", now=1000.0)
        self.assertIsNotNone(links.peek(token, now=1000.0 + LINK_SECONDS - 1))
        self.assertIsNone(links.peek(token, now=1000.0 + LINK_SECONDS))
        self.assertIsNone(links.take(token, now=1000.0 + LINK_SECONDS))

    def test_a_new_link_ends_the_last_one(self) -> None:
        links = SignInLinks()
        first = links.create(1, "Ann", None, "/servers")
        second = links.create(1, "Ann", None, "/servers")
        other = links.create(2, "Bob", None, "/servers")
        self.assertIsNone(links.peek(first))
        self.assertIsNotNone(links.peek(second))
        self.assertIsNotNone(links.peek(other))

    def test_keeps_no_token(self) -> None:
        links = SignInLinks()
        token = links.create(1, "Ann", None, "/servers")
        self.assertNotIn(token, links._links)

    def test_cancel(self) -> None:
        links = SignInLinks()
        token = links.create(1, "Ann", None, "/servers")
        links.cancel(token)
        self.assertIsNone(links.peek(token))

    def test_memory_stays_bounded(self) -> None:
        links = SignInLinks()
        now = time.time()
        for user_id in range(MAX_LINKS + 50):
            links.create(user_id, "x", None, "/servers", now=now)
        self.assertLessEqual(len(links._links), MAX_LINKS)
        self.assertLessEqual(len(links._latest), MAX_LINKS)


class Website(BotTest):
    async def site(self, *, client_secret: str = "", secure_cookies: bool = False) -> TestClient:
        app = build_app(self.bot, session_key=b"t" * 32, client_id="1", client_secret=client_secret, public_url=PUBLIC, secure_cookies=secure_cookies)
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    def link(self, name: str = "Ann", target: str = "/servers", avatar: str | None = None) -> str:
        return self.bot.sign_in_links.create(self.admin.id, name, avatar, target)

    async def test_login_page_explains_the_command_without_a_client_secret(self) -> None:
        client = await self.site()
        response = await client.get("/login?next=/dashboard/5/logging", allow_redirects=False)
        self.assertEqual(response.status, 200)
        self.assertIn("/dashboard", await response.text())
        self.assertIn("/dashboard/5/logging", response.cookies["cg_flow"].value)

    async def test_login_goes_to_discord_with_a_client_secret(self) -> None:
        client = await self.site(client_secret="secret")
        response = await client.get("/login", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertTrue(response.headers["Location"].startswith("https://discord.com/oauth2/authorize"))

    async def test_callback_goes_back_to_sign_in_without_a_client_secret(self) -> None:
        client = await self.site()
        response = await client.get("/login/callback?code=x&state=y", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/login")

    async def test_following_a_link_signs_in_once(self) -> None:
        client = await self.site()
        token = self.link(target="/servers")
        page = await client.get(f"/login/link/{token}")
        self.assertEqual(page.status, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn("Sign in as Ann", await page.text())
        self.assertIsNotNone(self.bot.sign_in_links.peek(token), "Loading the page must not use the link up")

        used = await client.post(f"/login/link/{token}", allow_redirects=False, headers={"Sec-Fetch-Site": "same-origin", "Origin": "null"})
        self.assertEqual(used.status, 303)
        self.assertEqual(used.headers["Location"], "/servers")
        me = await (await client.get("/api/me")).json()
        self.assertEqual(int(me["user"]["id"]), self.admin.id)

        again = await client.post(f"/login/link/{token}", allow_redirects=False, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(again.status, 410)
        self.assertEqual((await client.get(f"/login/link/{token}")).status, 410)

    async def test_goes_back_to_the_page_that_asked_for_sign_in(self) -> None:
        client = await self.site()
        await client.get("/login?next=/dashboard/5/logging")
        used = await client.post(f"/login/link/{self.link(target='/dashboard/9')}", allow_redirects=False)
        self.assertEqual(used.status, 303)
        self.assertEqual(used.headers["Location"], "/dashboard/5/logging")

    async def test_goes_to_the_server_the_command_came_from(self) -> None:
        client = await self.site()
        used = await client.post(f"/login/link/{self.link(target='/dashboard/9')}", allow_redirects=False)
        self.assertEqual(used.headers["Location"], "/dashboard/9")

    async def test_a_remembered_page_cant_leave_the_site(self) -> None:
        client = await self.site()
        await client.get("/login?next=//evil.example")
        used = await client.post(f"/login/link/{self.link(target='/dashboard/9')}", allow_redirects=False)
        self.assertEqual(used.headers["Location"], "/servers")

    async def test_refuses_a_post_from_another_site(self) -> None:
        client = await self.site()
        token = self.link()
        cross = await client.post(f"/login/link/{token}", allow_redirects=False, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(cross.status, 403)
        older_browser = await client.post(f"/login/link/{token}", allow_redirects=False, headers={"Origin": "https://evil.example"})
        self.assertEqual(older_browser.status, 403)
        self.assertIsNotNone(self.bot.sign_in_links.peek(token), "A refused post must not use the link up")

    async def test_a_name_cant_add_to_the_page(self) -> None:
        client = await self.site()
        token = self.link(name="<script>alert(1)</script>", avatar='https://cdn.example/a.png"><script>')
        body = await (await client.get(f"/login/link/{token}")).text()
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)

    async def test_an_unknown_link_says_so(self) -> None:
        client = await self.site()
        self.assertEqual((await client.get("/login/link/nothing")).status, 410)
        self.assertEqual((await client.post("/login/link/nothing")).status, 410)

    async def test_someone_signed_in_skips_the_page(self) -> None:
        client = await self.site()
        await client.post(f"/login/link/{self.link()}", allow_redirects=False)
        response = await client.get("/login?next=/dashboard/5", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/dashboard/5")

    async def test_signing_out_clears_a_host_cookie_with_secure(self) -> None:
        client = await self.site(secure_cookies=True)
        response = await client.post("/logout", allow_redirects=False)
        self.assertEqual(response.status, 302)
        cookie = response.headers["Set-Cookie"]
        self.assertIn(SESSION_COOKIE, cookie)
        self.assertIn("Secure", cookie)

    async def test_me_says_whether_the_bot_reads_message_text(self) -> None:
        client = await self.site()
        self.assertTrue((await (await client.get("/api/me")).json())["message_text"])
        self.bot.reads_message_text = False
        self.assertFalse((await (await client.get("/api/me")).json())["message_text"])


class StartUp(unittest.IsolatedAsyncioTestCase):
    async def check(self, flags: int) -> bool:
        info = mock.Mock()
        info.flags = program.discord.ApplicationFlags._from_value(flags)
        with mock.patch.object(program.discord.Client, "login", mock.AsyncMock()), \
                mock.patch.object(program.discord.Client, "application_info", mock.AsyncMock(return_value=info)), \
                mock.patch.object(program.discord.Client, "close", mock.AsyncMock()) as close:
            answer = await program.can_read_message_text("token")
        close.assert_awaited_once()
        return answer

    async def test_reads_the_app_flags(self) -> None:
        members = 1 << 14
        self.assertFalse(await self.check(members))
        self.assertFalse(await self.check(1 << 14 | 1 << 15))
        self.assertTrue(await self.check(members | 1 << 18))
        self.assertTrue(await self.check(members | 1 << 19))

    def test_the_add_link_asks_for_administrator(self) -> None:
        self.assertTrue(program.ADD_PERMISSIONS.administrator)
        self.assertRegex(program.invite_url(111222333444555666), r"[?&]permissions=8(&|$)")


class Command(GatewayTest):
    modules = [Config]

    def slash(self, user_id: int) -> None:
        data = {"id": str(self.bot_id), "name": "dashboard", "type": 1, "options": []}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id))

    def link_in(self, body: dict) -> str:
        return body["components"][0]["components"][0]["url"]

    async def test_slash_command_answers_privately(self) -> None:
        self.slash(self.mod_id)
        await self.settle(1.0)
        self.assert_no_errors()
        answers = [body for method, path, body in self.net.calls if path.endswith("/callback")]
        self.assertEqual(len(answers), 1)
        data = answers[0]["data"]
        self.assertEqual(data["flags"] & 64, 64)
        url = self.link_in(data)
        self.assertTrue(url.startswith(f"{PUBLIC}/login/link/"), url)
        link = self.bot.sign_in_links.peek(url.rsplit("/", 1)[1])
        self.assertEqual(link.user_id, self.mod_id)

    async def test_a_redirect_rule_cant_post_the_link_in_a_channel(self) -> None:
        await self.bot.settings.save(self.guild_id, COMMANDS_MODULE, {"overwrites": {"dashboard": {"redirect": int(self.text["id"])}}})
        self.slash(self.mod_id)
        await self.settle(1.0)
        self.assert_no_errors()
        self.assertTrue(any("/login/link/" in str(body) for method, path, body in self.net.calls if path.endswith("/callback")), "The private answer is missing")
        self.assertFalse(any("/login/link/" in str(body) for method, path, body in self.net.calls if path == "/channels/{channel_id}/messages"),
                         "The link must never be posted in a channel")

    async def test_text_command_never_follows_a_redirect_rule_to_a_channel(self) -> None:
        await self.bot.settings.save(self.guild_id, COMMANDS_MODULE, {"overwrites": {"dashboard": {"redirect": int(self.text["id"])}}})
        self.dispatch_message(user_id=self.mod_id, content="!dashboard")
        await self.settle(1.0)
        self.assert_no_errors()
        sent = [str(body) for method, path, body in self.net.calls if path == "/channels/{channel_id}/messages"]
        self.assertEqual(sum("/login/link/" in body for body in sent), 1, "The link goes out once, by DM")
        for method, path, body in self.net.calls:
            if path == "/channels/{channel_id}/messages" and "/login/link/" in str(body):
                self.assertNotIn(self.text["id"], self.net_route_channel(body))

    def net_route_channel(self, body: dict) -> str:
        """The channel a message landed in, from the copy the fake network kept."""
        for message in self.net.posted:
            if message.get("components") == body.get("components"):
                return message["channel_id"]
        return ""

    async def test_text_command_sends_the_link_by_dm(self) -> None:
        channel_id = self.text["id"]
        self.dispatch_message(user_id=self.mod_id, content="!dashboard")
        await self.settle(1.0)
        self.assert_no_errors()
        with_link = [message for message in self.net.posted if "/login/link/" in str(message)]
        self.assertEqual(len(with_link), 1)
        self.assertNotEqual(with_link[0]["channel_id"], channel_id, "The link went to the channel the command was typed in")
        replies = [message for message in self.net.posted if message["channel_id"] == channel_id]
        self.assertTrue(any("DM" in message["content"] for message in replies), replies)
