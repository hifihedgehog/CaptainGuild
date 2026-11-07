"""My data: a member sees, downloads and deletes what's theirs, and nothing of anyone else's or of what staff keep."""
from __future__ import annotations

import re
import time

from aiohttp.test_utils import TestClient, TestServer

from captainguild.modules import load_all
from captainguild.modules.fun import Fun
from captainguild.modules.greetings import Greetings
from captainguild.modules.levels import Levels
from captainguild.modules.logs import Logs
from captainguild.modules.moderation import Moderation
from captainguild.modules.mydata import BY_KEY, KEPT, SECTIONS, MyData
from captainguild.modules.utilities import Utilities
from captainguild.modules.entrygiveaways import EntryGiveaways
from captainguild.modules.voice import Voice
from captainguild.modules.translation import Translation
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.fakes import BotTest

PERSON = re.compile(r"(user|author|member|moderator|actor|target|submitter|reporter|voter|owner|creator|host|winner|applied_by|decided_by|handled|initiator|_by$|_by_)", re.I)


class EveryTableTest(BotTest):
    modules = load_all()

    async def test_every_table_that_can_name_a_person_is_accounted_for(self):
        listed = {table for section in SECTIONS for table, _ in section.tables} | set(KEPT)
        missing = []
        for database in (self.db, self.message_db):
            for row in await database.all("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
                name = row["name"]
                columns = [c["name"] for c in await database.all(f"PRAGMA table_info('{name}')")]
                if any(PERSON.search(column) for column in columns) and name not in listed and not name.startswith("log_search"):
                    missing.append(name)
        self.assertEqual(missing, [], "Add each to SECTIONS or KEPT in modules/mydata.py")

    def test_a_table_that_names_people_inside_its_text_is_accounted_for(self):
        self.assertIn("timers", KEPT)  # its payload names who a tempban or temporary role is for, which no column name shows

    async def test_every_section_points_at_a_real_table_and_column(self):
        for section in SECTIONS:
            for table, column in section.tables:
                columns = [c["name"] for c in await self.db.all(f"PRAGMA table_info('{table}')")]
                self.assertIn(column, columns, table)
                self.assertIn("guild_id", columns, table)
            if section.module:
                self.assertIsNotNone(self.bot.module(section.module), section.key)


class Case(BotTest):
    modules = [Greetings, Levels, Logs, Moderation, Utilities, Fun, Voice, Translation, EntryGiveaways, MyData]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.mine = self.cog(MyData)
        self.you, self.other = self.member.id, self.mod.id
        now = time.time()
        for user in (self.you, self.other):
            await self.db.run("INSERT INTO birthdays (guild_id, user_id, month, day, updated_at) VALUES (?, ?, 5, 11, ?)", (self.guild.id, user, now))
            await self.db.run("INSERT INTO highlight_words (guild_id, user_id, word, created_at) VALUES (?, ?, 'printer', ?)", (self.guild.id, user, now))
            await self.db.run("INSERT INTO command_uses (guild_id, day, command, user_id, uses) VALUES (?, 1, 'ping', ?, 3)", (self.guild.id, user))
            await self.message_db.run(
                "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, created_at) VALUES (?, ?, ?, ?, 'x', 'hello there', ?)",
                (1000 + user % 1000, self.guild.id, self.channel.id, user, now))
        await self.db.run("INSERT INTO birthdays (guild_id, user_id, month, day, updated_at) VALUES (999, ?, 1, 2, ?)", (self.you, now))
        await self.db.run(
            "INSERT INTO cases (guild_id, number, user_id, user_name, action, moderator_id, moderator_name, source, created_at) VALUES (?, 1, ?, 'x', 'warn', 1, 'm', 'command', ?)", (self.guild.id, self.you, now))

    async def site(self, user_id=None, *, demo=True, csrf_token=None):
        person = User(user_id or self.you, "member", None)
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=person if demo else None)
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    def server(self, data, guild_id=None):
        wanted = str(guild_id or self.guild.id)
        return next(s for s in data["servers"] if str(s["id"]) == wanted)


class PageTest(Case):
    async def test_signed_out_gets_told_to_sign_in(self):
        client = await self.site(demo=False)
        self.assertEqual((await client.get("/api/my-data")).status, 401)
        self.assertEqual((await client.post("/api/my-data/delete", json={"section": "birthday"})).status, 401)
        self.assertEqual((await client.get("/api/my-data/download")).status, 401)

    async def test_it_shows_their_rows_by_server(self):
        client = await self.site()
        data = await (await client.get("/api/my-data")).json()
        here = self.server(data)
        self.assertEqual({s["key"]: s["count"] for s in here["sections"]}, {"birthday": 1, "highlights": 1, "usage": 1})
        self.assertEqual(here["messages"], 1)
        self.assertEqual(here["message_days"], 30)
        self.assertEqual({s["key"] for s in self.server(data, 999)["sections"]}, {"birthday"})
        self.assertEqual(self.server(data, 999)["name"], "A server the bot has left")

    async def test_nothing_held_shows_nothing(self):
        client = await self.site(self.admin.id)
        data = await (await client.get("/api/my-data")).json()
        self.assertEqual(data["servers"], [])

    async def test_deleting_takes_only_that_section_in_that_server_for_that_person(self):
        client = await self.site()
        response = await client.post("/api/my-data/delete", json={"guild_id": str(self.guild.id), "section": "birthday"})
        self.assertEqual(response.status, 200, await response.text())
        here = self.server(await response.json())
        self.assertNotIn("birthday", {s["key"] for s in here["sections"]})
        rows = await self.db.all("SELECT guild_id, user_id FROM birthdays")
        self.assertIn({"guild_id": self.guild.id, "user_id": self.other}, rows, "Someone else's stays")
        self.assertIn({"guild_id": 999, "user_id": self.you}, rows, "Another server's stays")
        self.assertEqual(len(rows), 2)
        recent = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(recent[0]["action"], "mydata.delete")

    async def test_staff_records_cant_be_deleted(self):
        client = await self.site()
        for section in ("cases", "messages", "notes", "reports", ""):
            response = await client.post("/api/my-data/delete", json={"guild_id": str(self.guild.id), "section": section})
            self.assertEqual(response.status, 400, section)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM cases"), 1)

    async def test_nonsense_is_refused(self):
        client = await self.site()
        for form in ({"guild_id": "abc", "section": "birthday"}, {"guild_id": str(2 ** 70), "section": "birthday"}, ["birthday"]):
            self.assertEqual((await client.post("/api/my-data/delete", json=form)).status, 400, form)
        self.assertEqual((await client.post("/api/my-data/delete", data=b"not json")).status, 400)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM birthdays"), 3)

    async def test_a_highlight_deleted_stops_matching(self):
        utilities = self.cog(Utilities)
        await utilities.on_guild_restored(self.guild.id)
        self.assertIn(self.you, utilities._words[self.guild.id])
        client = await self.site()
        response = await client.post("/api/my-data/delete", json={"guild_id": str(self.guild.id), "section": "highlights"})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM highlight_words WHERE user_id = ?", (self.you,)), 0)
        self.assertNotIn(self.you, utilities._words[self.guild.id])
        self.assertIn(self.other, utilities._words[self.guild.id])

    async def test_a_deleted_reminder_never_fires(self):
        await self.db.run(
            "INSERT INTO reminders (id, guild_id, channel_id, user_id, about, created_at, due_at) VALUES (77, ?, ?, ?, 'Do it', ?, ?)",
            (self.guild.id, self.channel.id, self.you, time.time(), time.time() + 3600))
        await self.bot.timers.schedule("reminder", time.time() + 3600, {"id": 77}, guild_id=self.guild.id, key="reminder:77")
        client = await self.site()
        response = await client.post("/api/my-data/delete", json={"guild_id": str(self.guild.id), "section": "reminders"})
        self.assertEqual(response.status, 200, await response.text())
        self.assertIsNone(await self.bot.timers.get_key("reminder", "reminder:77"))

    async def test_xp_waiting_in_memory_cant_come_back(self):
        levels = self.cog(Levels)
        await self.db.run("INSERT INTO level_members (guild_id, user_id, xp, messages, name, updated_at) VALUES (?, ?, 500, 10, 'x', ?)", (self.guild.id, self.you, time.time()))
        client = await self.site()
        response = await client.post("/api/my-data/delete", json={"guild_id": str(self.guild.id), "section": "levels"})
        self.assertEqual(response.status, 200, await response.text())
        await levels.flush()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM level_members WHERE user_id = ?", (self.you,)), 0)

    async def test_the_download_holds_their_rows_and_nothing_else(self):
        client = await self.site()
        response = await client.get("/api/my-data/download")
        self.assertEqual(response.status, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        data = await response.json()
        self.assertEqual(len(data["tables"]["birthdays"]), 2)
        self.assertEqual({str(row["user_id"]) for row in data["tables"]["birthdays"]}, {str(self.you)})
        self.assertEqual(data["tables"]["messages"][0]["content"], "hello there")
        self.assertEqual(len(data["tables"]["messages"]), 1)
        self.assertNotIn("cases", data["tables"])

    async def test_someone_with_nothing_there_writes_nothing_to_activity(self):
        client = await self.site(self.admin.id)
        response = await client.post("/api/my-data/delete", json={"guild_id": "424242", "section": "birthday"})
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.bot.activity_log.recent(424242), [])
        self.assertEqual(await self.bot.activity_log.recent(self.guild.id), [])

    async def test_a_stuck_script_gets_slowed_down(self):
        client = await self.site()
        answers = [(await client.get("/api/my-data")).status for _ in range(12)]
        self.assertEqual(answers[:10], [200] * 10)
        self.assertEqual(answers[10:], [429, 429])

    async def test_a_stored_picture_downloads_too(self):
        await self.db.run("INSERT INTO level_backgrounds (guild_id, user_id, image, updated_at) VALUES (?, ?, ?, ?)", (self.guild.id, self.you, b"\x89PNG", time.time()))
        client = await self.site()
        response = await client.get("/api/my-data/download")
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["tables"]["level_backgrounds"][0]["image"], "iVBORw==")

    async def test_every_key_has_a_section(self):
        self.assertEqual(set(BY_KEY), {section.key for section in SECTIONS})

    async def test_the_page_is_public_and_comes_from_the_site(self):
        client = await self.site()
        response = await client.get("/my-data")
        self.assertEqual(response.status, 200)
        self.assertIn("/static/js/mydata.js", await response.text())
