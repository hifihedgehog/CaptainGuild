"""Who gets through every dashboard route, read from the real routers rather than from a list kept by hand.

Each route runs as seven people against a real session and the real access check: no session, a stranger with a session, a plain member, dashboard staff, a manager with Manage Server alone, an Administrator and the owner. A new route is covered the moment a module registers it.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time

from aiohttp.test_utils import TestClient, TestServer

from captainguild.core.ops import DiscordOps
from captainguild.modules import load_all
from captainguild.web.api import ACCESS_ORDER, User
from captainguild.web.app import DEV_SESSION_COOKIE, _core_routes, build_app, csrf_for
from captainguild.web.api import Router
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, FakePermissions, new_id

SESSION_KEY = b"t" * 32
PARAM = re.compile(r"\{(\w+)(?::[^}]*)?\}")
EVERYONE = ("view_channel", "send_messages", "read_message_history")
NO_ACCESS = "You don't have access to that here."


def routes_of(bot) -> list[tuple[str, str, str]]:
    """Every server route as (method, path, access), read from the routers build_app reads."""
    router = Router()
    _core_routes(router)
    for module in bot.modules:
        module.api(router)
    return [(method, path, level) for method, path, _, level in router.routes]


def filled(path: str, value: int = 999) -> str:
    """A route's path with its parameters filled in."""
    words = {"module": "core", "part": "avatar", "name": "nothing", "token": "nothing", "youtube_id": "nothing"}
    return PARAM.sub(lambda match: words.get(match.group(1), str(value)), path)


async def fingerprint(db) -> str:
    """Every row of every table but the ones a request writes by itself."""
    digest = hashlib.sha256()
    for row in await db.all("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if row["name"] in ("web_sessions", "schema_migrations"):
            continue
        for found in await db.all(f'SELECT * FROM "{row["name"]}"'):
            digest.update(repr((row["name"], sorted(dict(found).items()))).encode())
    return digest.hexdigest()


class MatrixCase(BotTest):
    modules = load_all()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.guild.default_role.permissions = FakePermissions(*EVERYONE)
        self.staff_role = self.guild.role("Helpers", 3)
        self.manager_role = self.guild.role("Manager", 4, "manage_guild")
        self.staff = FakeMember(self.guild, "staff", self.staff_role)
        self.manager = FakeMember(self.guild, "manager", self.manager_role)
        await self.bot.settings.save(self.guild.id, "core", {"dashboard_staff_roles": [self.staff_role.id]})
        self.app = build_app(self.bot, session_key=SESSION_KEY, public_url="", secure_cookies=False)
        self.site = TestClient(TestServer(self.app))
        await self.site.start_server()
        self._clients.append(self.site)
        self.requests: list[tuple] = []

        async def request(route, **kwargs):
            self.requests.append((route.method, route.url, kwargs.get("json")))
            return {"id": new_id()}
        self.bot.http.request = request

    async def session(self, person) -> dict[str, str]:
        """The headers of a signed-in browser. None is a browser with no session."""
        if person is None:
            return {}
        token = await self._new_session(person)
        return {"Cookie": f"{DEV_SESSION_COOKIE}={token}", "X-CSRF-Token": csrf_for(token, SESSION_KEY)}

    async def _new_session(self, person) -> str:
        from captainguild.web.app import SITE
        return await self.app[SITE].sessions.create(User(person.id, person.name, None))

    async def call(self, headers: dict, method: str, path: str, **kwargs):
        response = await self.site.request(method, f"/api/g/{self.guild.id}{filled(path)}", headers=headers, **kwargs)
        await response.read()
        return response


class EveryRouteTest(MatrixCase):
    @staticmethod
    async def answer(response) -> str | None:
        """The sentence a refusal gave, which tells the access check from a route's own."""
        if response.status != 403:
            return None
        return (await response.json())["error"]

    async def test_the_routers_hold_routes_for_every_module_with_a_page(self):
        routes = routes_of(self.bot)
        self.assertGreater(len(routes), 190)
        self.assertTrue(all(level in ACCESS_ORDER for _, _, level in routes))

    async def test_no_session_a_stranger_and_a_plain_member_get_nothing_and_change_nothing(self):
        stranger = User(987654321987654321, "stranger", None)
        member = FakeMember(self.guild, "plain")
        before = await fingerprint(self.db)
        for label, person, status in (("no session", None, 401), ("stranger", stranger, 403), ("member", member, 403)):
            headers = await self.session(person)
            for method, path, _ in routes_of(self.bot):
                response = await self.call(headers, method, path, json={})
                self.assertEqual(response.status, status, (label, method, path))
        self.assertEqual(await fingerprint(self.db), before)

    async def test_a_write_with_no_csrf_token_is_refused(self):
        token = await self._new_session(self.admin)
        before = await fingerprint(self.db)
        for method, path, _ in routes_of(self.bot):
            if method == "GET":
                continue
            response = await self.call({"Cookie": f"{DEV_SESSION_COOKIE}={token}"}, method, path, json={})
            self.assertEqual(response.status, 403, (method, path))
        self.assertEqual(await fingerprint(self.db), before)

    async def test_staff_reach_staff_routes_and_nothing_above_them(self):
        headers = await self.session(self.staff)
        for method, path, level in routes_of(self.bot):
            before = await fingerprint(self.db)
            response = await self.call(headers, method, path, json={})
            if level == "full":
                self.assertEqual(response.status, 403, (method, path))
                self.assertEqual(await fingerprint(self.db), before, (method, path))
            else:
                self.assertNotIn(response.status, (401, 500), (method, path))
                self.assertNotEqual(await self.answer(response), NO_ACCESS, (method, path))

    async def test_a_manager_an_administrator_and_the_owner_reach_every_route(self):
        for person in (self.manager, self.admin, self.owner):
            headers = await self.session(person)
            for method, path, _ in routes_of(self.bot):
                if method == "POST" and path == "/levels/reset":
                    continue
                response = await self.call(headers, method, path, json={})
                self.assertNotIn(response.status, (401, 500), (person.name, method, path))
                self.assertNotEqual(await self.answer(response), NO_ACCESS, (person.name, method, path))


SEED_ID = 777000000000000777
SEED_NUMBER = 424242
CANARY = "canary-from-the-other-server"
TEXT_KEY = "canaryb"


async def seed_rows(db, guild_id: int, ids: dict[str, int]) -> list[str]:
    """One row in every table that holds a guild_id, naming the other server's things. Returns the tables that took a row."""
    seeded = []
    await db.run("PRAGMA foreign_keys = OFF")
    for table in await db.all("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        columns = await db.all(f'PRAGMA table_info("{table["name"]}")')
        if not any(column["name"] == "guild_id" for column in columns):
            continue
        values = []
        for column in columns:
            name, kind = column["name"], (column["type"] or "").upper()
            if name == "guild_id":
                values.append(guild_id)
            elif "INT" in kind or "REAL" in kind or kind == "":
                if name == "id" or name.endswith("_id") and name not in ("channel_id", "message_id", "role_id", "user_id"):
                    values.append(SEED_ID)
                elif "channel" in name:
                    values.append(ids["channel"])
                elif "message" in name:
                    values.append(ids["message"])
                elif "role" in name:
                    values.append(ids["role"])
                elif "user" in name:
                    values.append(ids["user"])
                elif name == "number":
                    values.append(SEED_NUMBER)
                else:
                    values.append(1)
            elif name in ("name", "stream_id", "youtube_id", "module", "code", "token"):
                values.append(TEXT_KEY)
            else:
                values.append(CANARY)
        marks = ", ".join("?" for _ in columns)
        names = ", ".join(f'"{column["name"]}"' for column in columns)
        await db.run(f'INSERT OR IGNORE INTO "{table["name"]}" ({names}) VALUES ({marks})', values)
        seeded.append(table["name"])
    await db.run("PRAGMA foreign_keys = ON")
    return seeded


async def rows_of(db, guild_id: int) -> str:
    digest = hashlib.sha256()
    for table in await db.all("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        columns = {column["name"] for column in await db.all(f'PRAGMA table_info("{table["name"]}")')}
        if "guild_id" in columns:
            for row in await db.all(f'SELECT * FROM "{table["name"]}" WHERE guild_id = ?', (guild_id,)):
                digest.update(repr((table["name"], sorted(dict(row).items()))).encode())
    return digest.hexdigest()


class TwoServersCase(MatrixCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.other = FakeGuild("Other Server")
        self.bot.fake_guilds.append(self.other)
        self.other_bot_role = self.other.role("Bot", 9, "administrator", managed=True)
        self.other.me = FakeMember(self.other, "Bot", self.other_bot_role, bot=True)
        self.other_role = self.other.role("Theirs", 2)
        self.other_owner = FakeMember(self.other, "their owner", self.other_role)
        self.other.owner_id = self.other_owner.id
        self.other_channel = FakeChannel(self.other, "their-channel")
        self.other_message = await self.other_channel.send("their message")
        ids = {"channel": self.other_channel.id, "message": self.other_message.id, "role": self.other_role.id, "user": self.other_owner.id}
        self.seeded = await seed_rows(self.db, self.other.id, ids) + await seed_rows(self.message_db, self.other.id, ids)

    def fill_path(self, path: str) -> str:
        words = {
            "module": "core", "part": "avatar", "name": TEXT_KEY, "token": TEXT_KEY, "youtube_id": TEXT_KEY, "stream_id": TEXT_KEY,
            "channel_id": self.other_channel.id, "message_id": self.other_message.id, "user_id": self.other_owner.id, "number": SEED_NUMBER,
        }
        return PARAM.sub(lambda match: str(words.get(match.group(1), SEED_ID)), path)

    def assert_other_server_untouched(self, label) -> None:
        self.assertEqual(self.other_channel.sent, [self.other_message], label)
        self.assertEqual(self.other_channel.calls, [], label)
        for sent in self.requests:
            self.assertNotIn(str(self.other.id), sent[1], label)
            self.assertNotIn(str(self.other_channel.id), sent[1], label)


class RowsOfAnotherServerTest(TwoServersCase):
    async def test_seeding_reached_the_tables_that_matter(self):
        for table in ("reports", "cases", "tags", "triggers", "autofeeds", "rr_messages", "posted_messages", "scheduled_posts", "log_entries", "suggestions", "proposals"):
            self.assertIn(table, self.seeded)

    async def test_an_id_in_the_path_never_reaches_a_row_of_another_server(self):
        """The Administrator here holds nothing there. Whatever the route answers, their rows don't change and their words don't come back."""
        headers = await self.session(self.admin)
        before, before_messages = await rows_of(self.db, self.other.id), await rows_of(self.message_db, self.other.id)
        for method, path, _ in routes_of(self.bot):
            if "{" not in path:
                continue
            response = await self.site.request(method, f"/api/g/{self.guild.id}{self.fill_path(path)}", headers=headers, json={})
            body = await response.read()
            self.assertNotIn(CANARY.encode(), body, (method, path))
            self.assertNotEqual(response.status, 500, (method, path, body[:200]))
            self.assert_other_server_untouched((method, path))
        self.assertEqual(await rows_of(self.db, self.other.id), before)
        self.assertEqual(await rows_of(self.message_db, self.other.id), before_messages)

    async def test_a_list_never_shows_a_row_of_another_server(self):
        headers = await self.session(self.admin)
        for method, path, _ in routes_of(self.bot):
            if method != "GET" or "{" in path:
                continue
            response = await self.call(headers, method, path)
            body = await response.read()
            self.assertNotIn(CANARY.encode(), body, path)
            self.assertNotIn(str(self.other_message.id).encode(), body, path)

    async def test_an_outsider_reaches_nothing_with_the_other_servers_address(self):
        headers = await self.session(self.admin)
        for method, path, _ in routes_of(self.bot):
            response = await self.site.request(method, f"/api/g/{self.other.id}{filled(path)}", headers=headers, json={})
            self.assertEqual(response.status, 403, (method, path))

    async def test_the_same_requests_find_those_rows_from_their_own_server(self):
        """The check above means nothing unless these paths reach rows when they're asked in the right place."""
        logging.disable(logging.CRITICAL)  # the seeded text isn't the JSON some columns hold, and reading it there fails loudly
        self.addCleanup(logging.disable, logging.NOTSET)
        headers = await self.session(self.other_owner)
        found = []
        for method, path, _ in routes_of(self.bot):
            if method != "GET" or "{" not in path:
                continue
            response = await self.site.request(method, f"/api/g/{self.other.id}{self.fill_path(path)}", headers=headers)
            if response.status == 500 or CANARY.encode() in await response.read():
                found.append(path.split("{")[0])
        for wanted in ("/reports/", "/tags/", "/embeds/messages/"):
            self.assertIn(wanted, found)


class IdsInFormsTest(TwoServersCase):
    """Every ID a form takes, first from this server to show the route works, then from the other one."""

    async def forms(self):
        here_message = await self.channel.send("mine")
        low = self.guild.role("Regular", 2)
        soon = time.time() + 3600
        post = {"content": "x"}
        return [
            # route, the form with this server's IDs, the same form with the other server's
            ("POST", "/embeds/post", {"channel_id": self.channel.id, "payload": post}, {"channel_id": self.other_channel.id, "payload": post}),
            ("POST", "/embeds/post", {"channel_id": self.channel.id, "payload": post, "at": soon}, {"channel_id": self.other_channel.id, "payload": post, "at": soon}),
            ("PATCH", "/embeds/message", {"channel_id": self.channel.id, "message_id": here_message.id, "payload": {"content": "y"}}, {"channel_id": self.other_channel.id, "message_id": self.other_message.id, "payload": {"content": "y"}}),
            ("PATCH", "/embeds/message", {"channel_id": self.channel.id, "message_id": here_message.id, "payload": {"content": "z"}}, {"channel_id": self.channel.id, "message_id": self.other_message.id, "payload": {"content": "z"}}),
            ("POST", "/embeds/adoptions", {"messages": [{"channel_id": self.channel.id, "message_id": here_message.id}], "channel_id": self.channel.id}, {"messages": [{"channel_id": self.other_channel.id, "message_id": self.other_message.id}], "channel_id": self.channel.id}),
            ("POST", "/embeds/adoptions", None, {"messages": [{"channel_id": self.channel.id, "message_id": here_message.id}], "channel_id": self.other_channel.id}),
            ("POST", "/autofeeds", {"channel_id": self.channel.id, "message": post, "when": soon}, {"channel_id": self.other_channel.id, "message": post, "when": soon}),
            ("POST", "/autofeeds", {"channel_id": self.channel.id, "mention": low.id, "message": post, "when": soon}, {"channel_id": self.channel.id, "mention": self.other_role.id, "message": post, "when": soon}),
            ("POST", "/feeds", {"name": "a", "role_id": low.id, "channel_id": self.channel.id}, {"name": "b", "role_id": self.other_role.id, "channel_id": self.channel.id}),
            ("POST", "/feeds", {"name": "c", "role_id": low.id, "channel_id": self.channel.id}, {"name": "d", "role_id": low.id, "channel_id": self.other_channel.id}),
            ("POST", "/sticky", {"channel_id": self.channel.id, "payload": post, "forever": True}, {"channel_id": self.other_channel.id, "payload": post, "forever": True}),
            ("PUT", f"/autopurge/{self.channel.id}", {"interval_seconds": 7200}, None),
            ("PUT", f"/autopurge/{self.other_channel.id}", None, {"interval_seconds": 7200}),
            ("POST", "/triggers", {"trigger": "first words", "message": post, "channel_id": self.channel.id}, {"trigger": "second words", "message": post, "channel_id": self.other_channel.id}),
            ("POST", "/triggers", {"trigger": "third words", "message": post, "role_wl": [low.id]}, {"trigger": "fourth words", "message": post, "role_wl": [self.other_role.id]}),
            ("POST", "/tags", {"name": "one", "message": post, "channel_bl": [self.channel.id]}, {"name": "two", "message": post, "channel_bl": [self.other_channel.id]}),
            ("POST", "/voice/button", {"channel_id": self.channel.id}, {"channel_id": self.other_channel.id}),
            ("PUT", "/settings/greetings", {"welcome_channel": self.channel.id}, {"welcome_channel": self.other_channel.id}),
            ("PUT", "/settings/autoroles", {"roles": [low.id]}, {"roles": [self.other_role.id]}),
            ("PUT", "/settings/commands", {"mod_role": low.id}, {"mod_role": self.other_role.id}),
            ("PATCH", "/commands/overwrites", {"changes": {"tag": {"role_wl": [low.id]}}}, {"changes": {"tag": {"role_wl": [self.other_role.id]}}}),
            ("POST", "/reactionroles", {"channel_id": self.channel.id, "payload": post, "rows": [{"emoji": "\N{THUMBS UP SIGN}", "roles": [low.id]}]}, {"channel_id": self.other_channel.id, "payload": post, "rows": [{"emoji": "\N{THUMBS UP SIGN}", "roles": [low.id]}]}),
            ("POST", "/reactionroles", {"channel_id": self.channel.id, "payload": post, "rows": [{"emoji": "\N{THUMBS DOWN SIGN}", "roles": [low.id]}]}, {"channel_id": self.channel.id, "payload": post, "rows": [{"emoji": "\N{THUMBS UP SIGN}", "roles": [self.other_role.id]}]}),
            ("POST", "/reports/review", None, {"user_id": self.other_owner.id, "report_id": SEED_ID}),
            ("PUT", f"/levels/members/{self.member.id}", {"xp": 5}, None),
            ("PUT", f"/levels/members/{self.other_owner.id}", None, {"xp": 5}),
            ("POST", f"/voice/access/{self.other_owner.id}/grant", None, {}),
        ]

    @staticmethod
    def as_text(form):
        """Discord IDs arrive as text, as the page sends them."""
        if isinstance(form, dict):
            return {key: IdsInFormsTest.as_text(value) for key, value in form.items()}
        if isinstance(form, list):
            return [IdsInFormsTest.as_text(value) for value in form]
        return str(form) if isinstance(form, int) and abs(form) > 2 ** 53 else form

    async def test_a_form_refuses_the_other_servers_ids(self):
        headers = await self.session(self.admin)
        before, before_messages = await rows_of(self.db, self.other.id), await rows_of(self.message_db, self.other.id)
        url = f"/api/g/{self.guild.id}"
        for method, path, mine, theirs in await self.forms():
            if mine is not None:
                response = await self.site.request(method, url + path, headers=headers, json=self.as_text(mine))
                self.assertEqual(response.status, 200, (method, path, await response.text()))
            if theirs is not None:
                response = await self.site.request(method, url + path, headers=headers, json=self.as_text(theirs))
                self.assertIn(response.status, (400, 403, 404, 502), (method, path, theirs, await response.text()))
            self.assert_other_server_untouched((method, path))
        self.assertEqual(await rows_of(self.db, self.other.id), before)
        self.assertEqual(await rows_of(self.message_db, self.other.id), before_messages)
        await self.assertNothingHereNamesThem()

    async def assertNothingHereNamesThem(self):
        marks = (str(self.other_channel.id), str(self.other_role.id), str(self.other_message.id))
        for db in (self.db, self.message_db):
            for table in await db.all("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'log_search%'"):
                columns = {column["name"] for column in await db.all(f'PRAGMA table_info("{table["name"]}")')}
                if "guild_id" not in columns:
                    continue
                for row in await db.all(f'SELECT * FROM "{table["name"]}" WHERE guild_id = ?', (self.guild.id,)):
                    text = repr(dict(row))
                    for mark in marks:
                        self.assertNotIn(mark, text, table["name"])
