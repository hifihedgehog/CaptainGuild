"""What a restore does with work whose time has passed, and with files that hold only part of what belongs together."""
import asyncio
import json
import time

from captainguild.core.ops import DiscordOps
from captainguild.modules import load_all
from captainguild.modules.backup import RESTORING_KEY, Backup
from captainguild.modules.embeds import TIME_PASSED, Embeds
from captainguild.modules.greetings import ROLE_ENDS
from captainguild.modules.utilities import Utilities
from tests.fakes import BotTest, TestBot


class Case(BotTest):
    modules = load_all()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        asyncio.get_running_loop().set_debug(False)
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.backup = self.cog(Backup)

    async def doc(self):
        out = bytearray()
        async for block in self.backup.export_stream(self.guild.id, include_messages=False):
            out += block
        return json.loads(bytes(out))

    async def job_done(self, client):
        return await self.wait_for_job(client)

    async def preview(self, doc):
        client = await self.client()
        response = await client.post(self.path("/backup/upload"), data=json.dumps(doc).encode())
        self.assertEqual(response.status, 200, await response.text())
        job = await self.job_done(client)
        self.assertIsNone(job["error"])
        return client, job["result"]

    async def restore(self, doc):
        client, preview = await self.preview(doc)
        items = [row["key"] for row in preview["settings"]] + [row["key"] for row in preview["tables"]]
        response = await client.post(self.path("/backup/restore"), json={"token": preview["token"], "items": items})
        self.assertEqual(response.status, 200, await response.text())
        done = await self.job_done(client)
        self.assertIsNone(done["error"])
        return preview, done["result"]


class PastDueTest(Case):
    async def test_a_lifted_ban_stays_on_when_an_older_file_comes_back(self):
        victim = 777777777777777777
        self.guild.banned.append(victim)
        key = f"{self.guild.id}:{victim}"
        now = time.time()
        await self.bot.timers.schedule("moderation.unban", now + 0.05, {"guild_id": self.guild.id, "user_id": victim, "user_name": "spammer", "moderator_id": None, "moderator_name": None, "started_at": now}, guild_id=self.guild.id, key=key)
        doc = await self.doc()
        await self.bot.timers.cancel_key("moderation.unban", key)  # a moderator made it a permanent ban
        await asyncio.sleep(0.1)
        preview, _ = await self.restore(doc)
        self.assertTrue(any("stay out" in text for text in preview["warnings"]), preview["warnings"])
        self.assertIsNone(await self.bot.timers.get_key("moderation.unban", key))
        await self.bot.timers.run_due()
        self.assertIn(victim, self.guild.banned)

    async def test_the_preview_names_the_jobs_that_run_late(self):
        await self.bot.timers.schedule("reminder", time.time() - 100, {"id": 7}, guild_id=self.guild.id, key="reminder:7")
        doc = await self.doc()
        await self.db.run("DELETE FROM timers WHERE guild_id = ?", (self.guild.id,))
        _, preview = await self.preview(doc)
        self.assertTrue(any("past due" in text and text.endswith(": reminder.") for text in preview["warnings"]), preview["warnings"])

    async def test_the_birthday_role_timer_stays_out(self):
        role = self.guild.role("Birthday", 3)
        await self.member.add_roles(role)
        key = f"{self.guild.id}:{self.member.id}"
        await self.bot.timers.schedule(ROLE_ENDS, time.time() + 0.05, {"guild_id": self.guild.id, "user_id": self.member.id, "role_id": role.id}, guild_id=self.guild.id, key=key)
        doc = await self.doc()
        await self.bot.timers.cancel_key(ROLE_ENDS, key)
        await asyncio.sleep(0.1)
        await self.restore(doc)
        await self.bot.timers.run_due()
        self.assertIn(role, self.member.roles)

    async def scheduled_post(self, due, content="@everyone sale starts"):
        payload = {"content": content, "embeds": [], "buttons": []}
        post_id = await self.db.insert(
            "INSERT INTO scheduled_posts (guild_id, channel_id, payload, summary, ping, due_at, created_at) VALUES (?, ?, ?, 's', 1, ?, 1.0)",
            (self.guild.id, self.channel.id, json.dumps(payload), due),
        )
        await self.bot.timers.schedule("embeds.scheduled", due, {"id": post_id}, guild_id=self.guild.id, key=str(post_id))
        return post_id

    async def test_a_scheduled_post_doesnt_go_out_twice(self):
        post_id = await self.scheduled_post(time.time() + 0.05)
        doc = await self.doc()
        await asyncio.sleep(0.1)
        await self.bot.timers.run_due()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM scheduled_posts"), 0)
        sent = len(self.channel.sent)
        self.assertEqual(sent, 1)
        await self.restore(doc)
        await self.bot.timers.run_due()
        self.assertEqual(len(self.channel.sent), sent)
        row = await self.db.one("SELECT state, error FROM scheduled_posts WHERE id = ?", (post_id,))
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error"], TIME_PASSED)

    async def test_a_scheduled_post_still_to_come_stays_and_goes_out(self):
        post_id = await self.scheduled_post(time.time() + 3600)
        await self.restore(await self.doc())
        self.assertEqual(await self.db.scalar("SELECT state FROM scheduled_posts WHERE id = ?", (post_id,)), "waiting")
        self.assertIsNotNone(await self.bot.timers.get_key("embeds.scheduled", str(post_id)))

    async def test_a_post_that_comes_back_without_its_timer_gets_one(self):
        post_id = await self.scheduled_post(time.time() + 3600)
        await self.bot.timers.cancel_key("embeds.scheduled", str(post_id))
        await self.cog(Embeds).on_guild_restored(self.guild.id)
        self.assertIsNotNone(await self.bot.timers.get_key("embeds.scheduled", str(post_id)))

    async def test_a_post_being_sent_when_the_file_was_made_says_so(self):
        post_id = await self.scheduled_post(time.time() + 3600)
        await self.db.run("UPDATE scheduled_posts SET state = 'posting' WHERE id = ?", (post_id,))
        await self.cog(Embeds).on_guild_restored(self.guild.id)
        self.assertEqual(await self.db.scalar("SELECT state FROM scheduled_posts WHERE id = ?", (post_id,)), "failed")

    async def test_another_servers_timer_cant_send_this_servers_post(self):
        post_id = await self.scheduled_post(time.time() + 3600)
        await self.cog(Embeds)._scheduled_due({"id": post_id}, {"guild_id": 555, "tries": 0})
        self.assertEqual(self.channel.sent, [])

    async def autofeed(self, next_at, **repeat):
        return await self.db.insert(
            "INSERT INTO autofeeds (guild_id, channel_id, mention, payload, next_at, repeat_months, repeat_days, repeat_hours, repeat_minutes, timezone, created_at) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, 'UTC', 1.0)",
            (self.guild.id, self.channel.id, json.dumps({"content": "feed", "embeds": [], "buttons": []}), next_at, repeat.get("months", 0), repeat.get("days", 0), repeat.get("hours", 0), repeat.get("minutes", 0)),
        )

    async def test_a_repeating_autofeed_waits_for_its_next_time(self):
        now = time.time()
        feed = await self.autofeed(now - 100, days=1)
        await self.bot.timers.schedule("autofeed", now - 100, {"id": feed}, guild_id=self.guild.id, key=str(feed))
        await self.restore(await self.doc())
        await self.bot.timers.run_due()
        self.assertEqual(self.channel.sent, [])
        self.assertGreater(await self.db.scalar("SELECT next_at FROM autofeeds WHERE id = ?", (feed,)), now)
        self.assertIsNotNone(await self.bot.timers.get_key("autofeed", str(feed)))

    async def test_a_one_time_autofeed_says_its_time_passed(self):
        now = time.time()
        feed = await self.autofeed(now - 100)
        await self.bot.timers.schedule("autofeed", now - 100, {"id": feed}, guild_id=self.guild.id, key=str(feed))
        await self.restore(await self.doc())
        await self.bot.timers.run_due()
        self.assertEqual(self.channel.sent, [])
        self.assertIn("time passed", await self.db.scalar("SELECT last_error FROM autofeeds WHERE id = ?", (feed,)))

    async def test_a_giveaway_that_ended_since_the_file_was_made_stays_ended(self):
        columns = {row["name"]: row for row in await self.db.all("PRAGMA table_info(giveaways)")}
        values = {name: (0 if "INT" in row["type"].upper() else 1.0 if "REAL" in row["type"].upper() else "x") for name, row in columns.items() if row["notnull"] and name != "id"}
        values.update(guild_id=self.guild.id, channel_id=self.channel.id, ends_at=time.time() + 0.05)
        values.pop("ended_at", None)
        names = ", ".join(values)
        giveaway = await self.db.insert(f"INSERT INTO giveaways ({names}) VALUES ({', '.join('?' for _ in values)})", list(values.values()))
        doc = await self.doc()
        await self.db.run("UPDATE giveaways SET ended_at = 5.0, winner_ids = '[1]', announced = 1 WHERE id = ?", (giveaway,))
        await self.restore(doc)
        row = await self.db.one("SELECT ended_at, winner_ids, announced FROM giveaways WHERE id = ?", (giveaway,))
        self.assertEqual((row["ended_at"], row["winner_ids"], row["announced"]), (5.0, "[1]", 1))

    async def test_nothing_of_this_server_runs_until_the_restore_is_done(self):
        await self.bot.timers.schedule("reminder", time.time() - 100, {"id": 7}, guild_id=self.guild.id, key="reminder:7")
        doc = await self.doc()
        ran = []

        async def reminder(payload, row):
            ran.append(payload)

        self.bot.timers.handlers["reminder"] = reminder
        seen = []
        utilities = self.cog(Utilities)
        original = utilities.on_guild_restored

        async def run_during_restore(guild_id):
            await self.bot.timers.run_due()
            seen.append(list(ran))
            await original(guild_id)

        utilities.on_guild_restored = run_during_restore
        await self.restore(doc)
        self.assertEqual(seen, [[]])
        await self.bot.timers.run_due()
        self.assertEqual(ran, [{"id": 7}])


class MarkedReminderTest(Case):
    async def test_a_reminder_rewritten_in_the_file_stays_out(self):
        now = time.time()
        reminder = await self.db.insert(
            "INSERT INTO reminders (guild_id, channel_id, user_id, about, due_at, created_at) VALUES (?, ?, ?, 'water the plants', ?, ?)",
            (self.guild.id, self.channel.id, self.member.id, now + 3600, now),
        )
        doc = await self.doc()
        part = doc["tables"]["main.reminders"]
        part["rows"][0][part["columns"].index("user_id")] = 424242424242
        part["rows"][0][part["columns"].index("about")] = "Free nitro at example.test"
        preview, _ = await self.restore(doc)
        self.assertTrue(any("reminders rows" in text for text in preview["warnings"]), preview["warnings"])
        self.assertIsNone(await self.db.one("SELECT * FROM reminders WHERE id = ? AND user_id = 424242424242", (reminder,)))

    async def test_a_real_reminder_comes_back(self):
        now = time.time()
        reminder = await self.db.insert(
            "INSERT INTO reminders (guild_id, channel_id, user_id, about, due_at, created_at) VALUES (?, ?, ?, 'water the plants', ?, ?)",
            (self.guild.id, self.channel.id, self.member.id, now + 3600, now),
        )
        doc = await self.doc()
        await self.db.run("DELETE FROM reminders WHERE id = ?", (reminder,))
        await self.restore(doc)
        self.assertEqual(await self.db.scalar("SELECT about FROM reminders WHERE id = ?", (reminder,)), "water the plants")


class PartialFileTest(Case):
    async def post(self):
        posted = await self.db.insert("INSERT INTO posted_messages (guild_id, channel_id, message_id, summary, search, source, posted_at) VALUES (?, ?, 77, 's', 's', 'dashboard', 1.0)", (self.guild.id, self.channel.id))
        await self.db.run("INSERT INTO posted_versions (guild_id, posted_id, number, payload, note, saved_at) VALUES (?, ?, 1, '{}', 'x', 1.0)", (self.guild.id, posted))
        return posted

    async def test_a_file_with_a_parent_but_not_its_child_changes_neither(self):
        posted = await self.post()
        doc = await self.doc()
        del doc["tables"]["main.posted_versions"]
        await self.db.run("UPDATE posted_messages SET summary = 'newer' WHERE id = ?", (posted,))
        preview, _ = await self.restore(doc)
        self.assertTrue(any("posted_messages without posted_versions" in text for text in preview["warnings"]), preview["warnings"])
        self.assertNotIn("posted_messages", [row["table"] for row in preview["tables"]])
        self.assertEqual(await self.db.scalar("SELECT summary FROM posted_messages WHERE id = ?", (posted,)), "newer")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM posted_versions WHERE posted_id = ?", (posted,)), 1)

    async def test_a_file_with_a_child_but_not_its_parent_changes_neither(self):
        posted = await self.post()
        doc = await self.doc()
        del doc["tables"]["main.posted_messages"]
        await self.restore(doc)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM posted_versions WHERE posted_id = ?", (posted,)), 1)

    async def test_a_parent_with_a_declared_key_keeps_its_children_too(self):
        columns = {row["name"]: row for row in await self.db.all("PRAGMA table_info(giveaways)")}
        values = {name: (0 if "INT" in row["type"].upper() else 1.0 if "REAL" in row["type"].upper() else "x") for name, row in columns.items() if row["notnull"] and name != "id"}
        values.update(guild_id=self.guild.id, channel_id=self.channel.id, ends_at=time.time() + 3600)
        names = ", ".join(values)
        giveaway = await self.db.insert(f"INSERT INTO giveaways ({names}) VALUES ({', '.join('?' for _ in values)})", list(values.values()))
        await self.db.run("INSERT INTO giveaway_entries (giveaway_id, guild_id, user_id, entered_at) VALUES (?, ?, 5, 1.0)", (giveaway, self.guild.id))
        doc = await self.doc()
        del doc["tables"]["main.giveaway_entries"]
        await self.restore(doc)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id = ?", (giveaway,)), 1)

    async def test_a_missing_required_column_leaves_the_table_as_it_is(self):
        await self.db.run("INSERT INTO birthdays (guild_id, user_id, month, day, updated_at) VALUES (?, 1, 2, 3, 1.0)", (self.guild.id,))
        doc = await self.doc()
        part = doc["tables"]["main.birthdays"]
        at = part["columns"].index("month")
        part["columns"].pop(at)
        for row in part["rows"]:
            row.pop(at)
        preview, _ = await self.restore(doc)
        self.assertTrue(any("birthdays" in text and "stays as it is" in text for text in preview["warnings"]), preview["warnings"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM birthdays WHERE guild_id = ?", (self.guild.id,)), 1)

    async def test_repost_with_no_saved_text_answers_with_the_reason(self):
        posted = await self.db.insert("INSERT INTO posted_messages (guild_id, channel_id, message_id, summary, search, source, posted_at, deleted_at) VALUES (?, ?, 78, 's', 's', 'dashboard', 1.0, 2.0)", (self.guild.id, self.channel.id))
        client = await self.client()
        response = await client.post(self.path(f"/embeds/messages/{posted}/repost"), json={})
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"], "This post has no saved text to send.")


class CutOffTest(Case):
    async def test_a_restore_a_restart_cut_off_leaves_a_note_on_the_activity_page(self):
        await self.db.run("INSERT INTO kv (key, value) VALUES (?, 'An uploaded file')", (f"{RESTORING_KEY}{self.guild.id}",))
        restarted = TestBot(self.db, self.message_db)
        await restarted.prepare([Backup])
        row = await self.db.one("SELECT action, detail FROM activity WHERE guild_id = ? AND action = 'backup.import.interrupted'", (self.guild.id,))
        self.assertIn("An uploaded file", row["detail"])
        self.assertIsNone(await self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"{RESTORING_KEY}{self.guild.id}",)))

    async def test_a_finished_restore_leaves_no_mark_and_says_it_started(self):
        await self.restore(await self.doc())
        self.assertIsNone(await self.db.scalar("SELECT value FROM kv WHERE key LIKE ?", (f"{RESTORING_KEY}%",)))
        self.assertIsNotNone(await self.db.one("SELECT 1 FROM activity WHERE guild_id = ? AND action = 'backup.import.started'", (self.guild.id,)))

    async def test_a_restore_canceled_during_the_swap_keeps_the_mark(self):
        doc = await self.doc()
        client, preview = await self.preview(doc)
        items = [row["key"] for row in preview["tables"]]
        utilities = self.cog(Utilities)

        async def stop_here(guild_id):
            self.backup._jobs[guild_id].task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        utilities.before_restore = stop_here
        response = await client.post(self.path("/backup/restore"), json={"token": preview["token"], "items": items})
        self.assertEqual(response.status, 200)
        await self.job_done(client)
        self.assertIsNotNone(await self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"{RESTORING_KEY}{self.guild.id}",)))
        self.assertFalse(self.bot.timers._held)
