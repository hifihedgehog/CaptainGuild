import asyncio
import gzip
import json
import time

from captainguild.core.module import Module
from captainguild.core.validate import SECRET_MASK, Field, Schema, boolean, integer, optional, snowflake, text
from captainguild.modules import backup as backup_module
from captainguild.modules import load_all
from captainguild.modules.backup import Backup, BackupError, JsonStream, decode_cell, encode_cell, inflate, read_catalog, read_events, table_owners
from captainguild.modules.autoroles import Autoroles
from captainguild.modules.utilities import Utilities
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember

OTHER_GUILD = 555000000000000001
HUGE = 900000000000000123  # over what a JavaScript number can hold exactly


class Sample(Module):
    """A module standing in for one written later. Backup knows nothing about it."""
    key = "sample"
    category = "Misc"
    schema = Schema({
        "channel": Field(None, optional(snowflake)),
        "limit": Field(5, integer(0, 10)),
        "token": Field("", text(50), secret=True),
        "nested": Schema({"on": Field(False, boolean), "name": Field("x", text(10))}),
    })
    migrations = [
        """
        CREATE TABLE sample_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER,
            name TEXT NOT NULL,
            big INTEGER,
            ratio REAL,
            note TEXT,
            image BLOB,
            anything
        );
        CREATE INDEX sample_items_guild ON sample_items (guild_id);
        CREATE TABLE sample_children (
            item_id INTEGER NOT NULL REFERENCES sample_items (id) ON DELETE CASCADE,
            value TEXT NOT NULL,
            PRIMARY KEY (item_id, value)
        );
        CREATE TABLE sample_pairs (
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            value TEXT DEFAULT 'fallback',
            PRIMARY KEY (guild_id, name)
        ) WITHOUT ROWID;
        CREATE TABLE sample_messages (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            label TEXT
        );
        CREATE TABLE sample_seen (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (message_id, user_id)
        ) WITHOUT ROWID;
        CREATE TABLE sample_tokens (
            id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            api_token TEXT
        );
        CREATE TABLE sample_orphans (id INTEGER PRIMARY KEY, nothing_to_tie_it TEXT);
        """,
    ]
    message_migrations = [
        """
        CREATE TABLE sample_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            search_text TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX sample_log_guild ON sample_log (guild_id);
        CREATE VIRTUAL TABLE sample_search USING fts5(search_text, content='sample_log', content_rowid='id', tokenize='trigram');
        CREATE TRIGGER sample_log_added AFTER INSERT ON sample_log BEGIN
            INSERT INTO sample_search (rowid, search_text) VALUES (new.id, new.search_text);
        END;
        CREATE TRIGGER sample_log_removed AFTER DELETE ON sample_log BEGIN
            INSERT INTO sample_search (sample_search, rowid, search_text) VALUES ('delete', old.id, old.search_text);
        END;
        """,
    ]
    restored: list = []

    async def on_guild_restored(self, guild_id: int) -> None:
        Sample.restored.append(guild_id)


SEEDED_TABLES = ("sample_items", "sample_children", "sample_pairs", "sample_messages", "sample_seen", "sample_tokens", "highlight_words", "command_uses", "activity")


class BackupCase(BotTest):
    modules = [Autoroles, Utilities, Sample, Backup]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The test runner's debug mode records a stack for every callback, and these tests make thousands of database calls.
        asyncio.get_running_loop().set_debug(False)
        self.backup = self.cog(Backup)
        Sample.restored = []

    async def seed(self, guild_id=None):
        guild_id = guild_id or self.guild.id
        item = await self.db.insert(
            "INSERT INTO sample_items (guild_id, channel_id, name, big, ratio, note, image, anything) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, self.channel.id if guild_id == self.guild.id else None, "Caf\u00e9 \U0001f389 \"quoted\" \\ back\nline", HUGE, 0.1 + 0.2, None, b"\x00\x01\xffbytes", HUGE + 7),
        )
        second = await self.db.insert(
            "INSERT INTO sample_items (guild_id, name, big, ratio, note, image, anything) VALUES (?, 'second', -5, 1.0, '', NULL, 'plain text')", (guild_id,),
        )
        await self.db.many("INSERT INTO sample_children (item_id, value) VALUES (?, ?)", [(item, "a"), (item, "b"), (second, "c")])
        await self.db.run("INSERT INTO sample_pairs (guild_id, name, value) VALUES (?, 'k', NULL)", (guild_id,))
        message_id = HUGE + guild_id % 1000
        await self.db.run("INSERT INTO sample_messages (message_id, guild_id, label) VALUES (?, ?, 'x')", (message_id, guild_id))
        await self.db.many("INSERT INTO sample_seen (message_id, user_id) VALUES (?, ?)", [(message_id, 1), (message_id, HUGE)])
        await self.db.run("INSERT INTO sample_tokens (id, guild_id, api_token) VALUES (?, ?, 'very secret value')", (guild_id % 100000, guild_id))
        await self.message_db.run("INSERT INTO sample_log (guild_id, body, search_text) VALUES (?, 'logged', 'findable words')", (guild_id,))
        await self.db.run("INSERT INTO highlight_words VALUES (?, 77, 'printer', 1.5)", (guild_id,))
        await self.db.run("INSERT INTO command_uses VALUES (?, 20000, 'ban', 77, 3)", (guild_id,))
        await self.db.run("INSERT INTO sticky_roles VALUES (?, 77, '[1, 2]', 9.5)", (guild_id,))
        return item

    async def snapshot_rows(self, guild_id=None):
        guild_id = guild_id or self.guild.id
        out = {}
        for name in ("sample_items", "sample_pairs", "sample_messages", "sample_tokens", "highlight_words", "command_uses", "sticky_roles"):
            out[name] = sorted(map(repr, await self.db.all(f"SELECT * FROM {name} WHERE guild_id = ?", (guild_id,))))
        out["sample_children"] = sorted(map(repr, await self.db.all("SELECT * FROM sample_children WHERE item_id IN (SELECT id FROM sample_items WHERE guild_id = ?)", (guild_id,))))
        out["sample_seen"] = sorted(map(repr, await self.db.all("SELECT * FROM sample_seen WHERE message_id IN (SELECT message_id FROM sample_messages WHERE guild_id = ?)", (guild_id,))))
        out["sample_log"] = sorted(map(repr, await self.message_db.all("SELECT * FROM sample_log WHERE guild_id = ?", (guild_id,))))
        return out

    async def wipe(self, guild_id=None):
        guild_id = guild_id or self.guild.id
        await self.db.run("DELETE FROM sample_seen WHERE message_id IN (SELECT message_id FROM sample_messages WHERE guild_id = ?)", (guild_id,))
        for name in ("sample_items", "sample_pairs", "sample_messages", "sample_tokens", "highlight_words", "command_uses", "sticky_roles"):
            await self.db.run(f"DELETE FROM {name} WHERE guild_id = ?", (guild_id,))
        await self.message_db.run("DELETE FROM sample_log WHERE guild_id = ?", (guild_id,))

    async def export_bytes(self, guild_id=None, **options):
        options.setdefault("include_messages", True)
        out = bytearray()
        async for block in self.backup.export_stream(guild_id or self.guild.id, **options):
            out += block
        return bytes(out)

    async def export_doc(self, **options):
        return json.loads(await self.export_bytes(**options))

    async def upload(self, client, body):
        response = await client.post(self.path("/backup/upload"), data=body)
        return response, await response.json()

    async def finished(self, client):
        return await self.wait_for_job(client)

    async def preview_of(self, client, body):
        response, data = await self.upload(client, body)
        self.assertEqual(response.status, 200, data)
        return await self.finished(client)

    def all_items(self, preview):
        return [row["key"] for row in preview["settings"]] + [row["key"] for row in preview["tables"]]

    async def restore(self, client, preview, items=None):
        response = await client.post(self.path("/backup/restore"), json={"token": preview["token"], "items": items if items is not None else self.all_items(preview)})
        self.assertEqual(response.status, 200, await response.text())
        return await self.finished(client)


class RoundTripTest(BackupCase):
    async def test_export_wipe_import_gives_back_every_row_exactly(self):
        await self.seed()
        await self.seed(OTHER_GUILD)
        await self.bot.settings.save(self.guild.id, "autoroles", {"roles": [self.role_member.id], "reassign": True, "reassign_days": 9})
        await self.bot.settings.save(self.guild.id, "sample", {"channel": self.channel.id, "limit": 7, "token": "abc", "nested": {"on": True, "name": "yes"}})
        await self.bot.settings.save(self.guild.id, "core", {"prefixes": [".", "?"]})
        before = await self.snapshot_rows()
        other_before = await self.snapshot_rows(OTHER_GUILD)
        settings_before = {name: self.settings(name) for name in ("autoroles", "sample", "core")}
        body = await self.export_bytes()

        await self.wipe()
        for name in ("autoroles", "sample", "core"):
            await self.bot.settings.restore(self.guild.id, name, {})
        self.assertEqual(self.settings("sample")["limit"], 5)
        self.assertNotEqual(await self.snapshot_rows(), before)

        client = await self.client()
        job = await self.preview_of(client, body)
        self.assertIsNone(job["error"])
        done = await self.restore(client, job["result"])
        self.assertIsNone(done["error"])

        self.assertEqual(await self.snapshot_rows(), before)
        self.assertEqual(await self.snapshot_rows(OTHER_GUILD), other_before)
        for name, value in settings_before.items():
            self.assertEqual(self.settings(name), value, name)
        self.assertEqual(type(self.settings("sample")["channel"]), int)
        self.assertIn(self.guild.id, Sample.restored)

    async def test_the_search_index_of_a_restored_store_works(self):
        await self.seed()
        body = await self.export_bytes()
        await self.wipe()
        client = await self.client()
        job = await self.preview_of(client, body)
        await self.restore(client, job["result"])
        hits = await self.message_db.all("SELECT rowid FROM sample_search WHERE sample_search MATCH 'findable'")
        self.assertEqual(len(hits), 1)

    async def test_a_second_round_trip_gives_the_same_rows(self):
        await self.seed()
        client = await self.client()
        rows = await self.snapshot_rows()
        for _ in range(2):
            body = await self.export_bytes()
            await self.wipe()
            job = await self.preview_of(client, body)
            await self.restore(client, job["result"])
            self.assertEqual(await self.snapshot_rows(), rows)

    async def test_restoring_over_changed_data_replaces_it(self):
        await self.seed()
        rows = await self.snapshot_rows()
        body = await self.export_bytes()
        await self.db.run("UPDATE sample_items SET name = 'changed' WHERE guild_id = ?", (self.guild.id,))
        added = await self.db.insert("INSERT INTO sample_items (guild_id, name) VALUES (?, 'added later')", (self.guild.id,))
        await self.db.run("INSERT INTO sample_children (item_id, value) VALUES (?, 'added child')", (added,))
        client = await self.client()
        job = await self.preview_of(client, body)
        row = next(t for t in job["result"]["tables"] if t["table"] == "sample_items")
        self.assertEqual(row["removed"], 1)
        self.assertEqual(row["changed"], 2)
        self.assertEqual(row["same"], 0)
        await self.restore(client, job["result"])
        self.assertEqual(await self.snapshot_rows(), rows)

    async def test_new_tables_and_modules_are_found_without_being_named(self):
        doc = await self.export_doc()
        for table in ("main.sample_items", "main.sample_children", "main.sample_pairs", "main.sample_seen", "messages.sample_log", "main.sticky_roles", "main.activity", "main.timers", "main.command_uses"):
            self.assertIn(table, doc["tables"])
        self.assertIn("sample", doc["settings"])
        self.assertIn("autoroles", doc["settings"])
        self.assertIn("core", doc["settings"])
        self.assertIn("commands", doc["settings"])
        self.assertIn("main.sample_orphans", doc["left_out"])

    async def test_the_export_holds_nothing_from_another_server_and_no_global_tables(self):
        await self.seed()
        await self.seed(OTHER_GUILD)
        doc = await self.export_doc()
        for key, table in doc["tables"].items():
            if "guild_id" in table["columns"]:
                at = table["columns"].index("guild_id")
                for row in table["rows"]:
                    self.assertEqual(str(row[at]), str(self.guild.id), key)
        children = doc["tables"]["main.sample_children"]["rows"]
        self.assertEqual(len(children), 3)
        self.assertEqual(len(doc["tables"]["main.sample_seen"]["rows"]), 2)
        for name in ("main.web_sessions", "main.kv", "main.schema_steps", "main.guild_settings", "main.backup_snapshots", "messages.sample_search"):
            self.assertNotIn(name, doc["tables"])

    async def test_the_file_is_plain_readable_json_with_big_numbers_as_text(self):
        await self.seed()
        doc = await self.export_doc()
        self.assertEqual(doc["format"], "captainguild-backup")
        self.assertEqual(doc["guild_id"], str(self.guild.id))
        items = doc["tables"]["main.sample_items"]
        first = dict(zip(items["columns"], items["rows"][0]))
        self.assertEqual(first["guild_id"], str(self.guild.id))
        self.assertEqual(first["big"], str(HUGE))
        self.assertEqual(first["anything"], {"$int": str(HUGE + 7)})
        self.assertEqual(first["image"], {"$b64": "AAH/Ynl0ZXM="})
        self.assertAlmostEqual(first["ratio"], 0.30000000000000004, places=16)
        self.assertEqual(doc["row_counts"]["main.sample_items"], 2)

    async def test_the_export_can_leave_out_messages(self):
        await self.seed()
        doc = await self.export_doc(include_messages=False)
        self.assertNotIn("messages.sample_log", doc["tables"])
        self.assertFalse(doc["includes_messages"])

    async def test_a_large_table_is_written_and_read_in_pieces(self):
        rows = [(self.guild.id, f"row {n}") for n in range(backup_module.BATCH_ROWS * 3 + 7)]
        await self.db.many("INSERT INTO sample_items (guild_id, name) VALUES (?, ?)", rows)
        body = await self.export_bytes()
        await self.db.run("DELETE FROM sample_items")
        client = await self.client()
        job = await self.preview_of(client, body)
        item = next(t for t in job["result"]["tables"] if t["table"] == "sample_items")
        self.assertEqual(item["in_file"], len(rows))
        self.assertEqual(item["added"], len(rows))
        await self.restore(client, job["result"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_items"), len(rows))

    async def test_a_file_past_the_sites_ordinary_request_limit_still_goes_through(self):
        filler = "x" * 60000
        rows = [(self.guild.id, f"row {n}", filler) for n in range(230)]
        await self.db.many("INSERT INTO sample_items (guild_id, name, note) VALUES (?, ?, ?)", rows)
        body = await self.export_bytes()
        self.assertGreater(len(body), 13 * 1024 * 1024)
        await self.db.run("DELETE FROM sample_items")
        client = await self.client()
        job = await self.preview_of(client, body)
        self.assertIsNone(job["error"])
        await self.restore(client, job["result"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_items WHERE note = ?", (filler,)), 230)

    async def test_rows_written_while_an_export_runs_do_not_tear_the_file(self):
        await self.seed()
        stream = self.backup.export_stream(self.guild.id, include_messages=False)
        first = await stream.__anext__()
        await self.db.run("INSERT INTO sample_items (guild_id, name) VALUES (?, 'written during')", (self.guild.id,))
        rest = bytearray(first)
        async for block in stream:
            rest += block
        doc = json.loads(bytes(rest))
        self.assertEqual(doc["row_counts"]["main.sample_items"], 2)
        self.assertEqual(len(doc["tables"]["main.sample_items"]["rows"]), 2)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_items WHERE guild_id = ?", (self.guild.id,)), 3)


class PreviewAndSelectionTest(BackupCase):
    async def test_the_preview_changes_nothing_and_says_what_would_change(self):
        await self.seed()
        await self.bot.settings.save(self.guild.id, "sample", {"limit": 7})
        body = await self.export_bytes()
        await self.wipe()
        await self.bot.settings.save(self.guild.id, "sample", {"limit": 2})
        before = await self.snapshot_rows()
        client = await self.client()
        job = await self.preview_of(client, body)
        result = job["result"]
        self.assertEqual(await self.snapshot_rows(), before)
        self.assertEqual(self.settings("sample")["limit"], 2)
        sample = next(row for row in result["settings"] if row["module"] == "sample")
        self.assertEqual(sample["changes"], [{"field": "limit", "before": "2", "after": "7"}])
        items = next(t for t in result["tables"] if t["table"] == "sample_items")
        self.assertEqual((items["now"], items["in_file"], items["added"], items["removed"]), (0, 2, 2, 0))
        self.assertEqual(items["owner"], "sample")
        self.assertEqual(items["mode"], "replace")
        log_row = next(t for t in result["tables"] if t["table"] == "sample_log")
        self.assertEqual(log_row["mode"], "merge")
        self.assertEqual(result["file"]["guild_id"], str(self.guild.id))
        self.assertTrue(result["file"]["includes_messages"])

    async def test_a_table_too_big_to_compare_still_previews_and_restores(self):
        await self.seed()
        rows = await self.snapshot_rows()
        body = await self.export_bytes()
        await self.db.run("UPDATE sample_pairs SET value = 'changed'")
        client = await self.client()
        original = backup_module.DIFF_ROW_LIMIT
        backup_module.DIFF_ROW_LIMIT = 0
        try:
            job = await self.preview_of(client, body)
            items = next(t for t in job["result"]["tables"] if t["table"] == "sample_items")
            self.assertFalse(items["compared"])
            self.assertEqual((items["now"], items["in_file"], items["added"], items["changed"], items["removed"]), (2, 2, 0, 0, 0))
            done = await self.restore(client, job["result"])
        finally:
            backup_module.DIFF_ROW_LIMIT = original
        self.assertIsNone(done["error"])
        self.assertEqual(await self.snapshot_rows(), rows)

    async def test_linked_tables_are_one_unit_so_a_parent_never_takes_children_along_by_accident(self):
        await self.seed()
        client = await self.client()
        job = await self.preview_of(client, await self.export_bytes())
        by_name = {t["table"]: t for t in job["result"]["tables"]}
        self.assertEqual(by_name["sample_items"]["group"], by_name["sample_children"]["group"])
        self.assertEqual(by_name["sample_messages"]["group"], by_name["sample_seen"]["group"])
        self.assertNotEqual(by_name["sample_items"]["group"], by_name["sample_pairs"]["group"])

    async def test_only_what_was_chosen_comes_back(self):
        await self.seed()
        await self.bot.settings.save(self.guild.id, "sample", {"limit": 7})
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign_days": 44})
        body = await self.export_bytes()
        await self.wipe()
        await self.bot.settings.save(self.guild.id, "sample", {"limit": 1})
        await self.bot.settings.save(self.guild.id, "autoroles", {"reassign_days": 2})
        client = await self.client()
        job = await self.preview_of(client, body)
        done = await self.restore(client, job["result"], ["table:main.sample_items", "settings:sample"])
        self.assertIsNone(done["error"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_items WHERE guild_id = ?", (self.guild.id,)), 2)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_children"), 3, "the child table came with its parent")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_pairs"), 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM highlight_words"), 0)
        self.assertEqual(self.settings("sample")["limit"], 7)
        self.assertEqual(self.settings("autoroles")["reassign_days"], 2)

    async def test_a_table_the_file_does_not_hold_stays_as_it_is(self):
        await self.seed()
        doc = await self.export_doc()
        del doc["tables"]["main.sample_pairs"]
        await self.db.run("INSERT INTO sample_pairs (guild_id, name) VALUES (?, 'made after')", (self.guild.id,))
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        await self.restore(client, job["result"], [*self.all_items(job["result"]), "table:main.sample_pairs"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_pairs WHERE guild_id = ?", (self.guild.id,)), 2)

    async def test_histories_gain_what_is_missing_and_lose_nothing(self):
        await self.seed()
        await self.bot.activity_log.record(self.guild.id, None, "old.action")
        await self.message_db.run("INSERT INTO sample_log (guild_id, body) VALUES (?, 'old')", (self.guild.id,))
        body = await self.export_bytes()
        await self.db.run("DELETE FROM activity WHERE action = 'old.action'")
        await self.bot.activity_log.record(self.guild.id, None, "new.action")
        await self.db.run("INSERT INTO command_uses VALUES (?, 20001, 'kick', 77, 1)", (self.guild.id,))
        await self.message_db.run("DELETE FROM sample_log WHERE body = 'logged'")
        await self.message_db.run("INSERT INTO sample_log (guild_id, body) VALUES (?, 'new')", (self.guild.id,))
        client = await self.client()
        job = await self.preview_of(client, body)
        await self.restore(client, job["result"])
        actions = {row["action"] for row in await self.db.all("SELECT action FROM activity WHERE guild_id = ?", (self.guild.id,))}
        self.assertIn("old.action", actions)
        self.assertIn("new.action", actions)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM command_uses WHERE guild_id = ?", (self.guild.id,)), 2)
        bodies = {row["body"] for row in await self.message_db.all("SELECT body FROM sample_log WHERE guild_id = ?", (self.guild.id,))}
        self.assertEqual(bodies, {"logged", "old", "new"})

    async def test_a_scheduled_job_stays_scheduled_and_past_due_ones_are_warned_about(self):
        await self.bot.timers.schedule("reminder", time.time() - 100, {"id": 999}, guild_id=self.guild.id, key="reminder:999")
        await self.bot.timers.schedule("reminder", time.time() + 100, {"id": 1000}, guild_id=self.guild.id, key="reminder:1000")
        body = await self.export_bytes()
        await self.db.run("DELETE FROM timers WHERE guild_id = ? AND key = 'reminder:999'", (self.guild.id,))
        await self.bot.timers.schedule("reminder", time.time() + 5000, {"id": 1000}, guild_id=self.guild.id, key="reminder:1000")
        await self.bot.timers.schedule("reminder", time.time() + 5000, {"id": 1001}, guild_id=self.guild.id, key="reminder:1001")
        client = await self.client()
        job = await self.preview_of(client, body)
        self.assertTrue(any("past due" in text for text in job["result"]["warnings"]))
        await self.restore(client, job["result"])
        rows = {row["key"]: row["due_at"] for row in await self.db.all("SELECT key, due_at FROM timers WHERE guild_id = ?", (self.guild.id,))}
        self.assertEqual(set(rows), {"reminder:999", "reminder:1000", "reminder:1001"})
        self.assertGreater(rows["reminder:1000"], time.time() + 1000, "the job scheduled now is the one that stays")

    async def test_a_past_due_role_change_stays_out_and_the_preview_says_so(self):
        role = self.guild.role("Regular", 3)
        await self.bot.timers.schedule("timedrole", time.time() - 100, {"guild_id": self.guild.id, "user_id": self.member.id, "role_id": role.id}, guild_id=self.guild.id, key="late")
        await self.bot.timers.schedule("timedrole", time.time() + 1000, {"guild_id": self.guild.id, "user_id": self.member.id, "role_id": role.id}, guild_id=self.guild.id, key="still-to-come")
        await self.bot.timers.schedule("reminder", time.time() - 100, {"id": 7}, guild_id=self.guild.id, key="reminder:7")
        body = await self.export_bytes()
        await self.db.run("DELETE FROM timers WHERE guild_id = ?", (self.guild.id,))
        client = await self.client()
        job = await self.preview_of(client, body)
        warnings = job["result"]["warnings"]
        self.assertTrue(any("1 scheduled changes" in text and "stay out" in text for text in warnings), warnings)
        self.assertTrue(any("1 scheduled jobs" in text and "past due" in text for text in warnings), warnings)
        await self.restore(client, job["result"])
        keys = {row["key"] for row in await self.db.all("SELECT key FROM timers WHERE guild_id = ?", (self.guild.id,))}
        self.assertEqual(keys, {"still-to-come", "reminder:7"})
        await self.bot.timers.run_due(time.time())
        self.assertNotIn(role, self.member.roles)

    async def test_a_role_a_moderator_took_after_its_timer_ran_stays_taken(self):
        role = self.guild.role("Regular", 3)
        await self.bot.settings.save(self.guild.id, "autoroles", {"timed": [{"role_id": role.id, "seconds": 60}]})
        await self.bot.timers.schedule("timedrole", time.time() + 0.05, {"guild_id": self.guild.id, "user_id": self.member.id, "role_id": role.id}, guild_id=self.guild.id, key="soon")
        body = await self.export_bytes()
        await asyncio.sleep(0.1)
        await self.bot.timers.run_due()
        self.assertIn(role, self.member.roles)
        await self.member.remove_roles(role)
        client = await self.client()
        job = await self.preview_of(client, body)
        await self.restore(client, job["result"])
        await self.bot.timers.run_due()
        self.assertNotIn(role, self.member.roles)

    async def test_activity_is_recorded(self):
        await self.seed()
        client = await self.client()
        response = await client.get(self.path("/backup/export"))
        await response.read()
        job = await self.preview_of(client, await self.export_bytes())
        await self.restore(client, job["result"])
        actions = [row["action"] for row in await self.bot.activity_log.recent(self.guild.id)]
        self.assertIn("backup.export", actions)
        self.assertIn("backup.import", actions)


class ExportEndpointTest(BackupCase):
    async def test_download_headers_and_body(self):
        await self.seed()
        client = await self.client()
        response = await client.get(self.path("/backup/export"))
        self.assertEqual(response.status, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertTrue(response.headers["Content-Disposition"].rstrip('"').endswith(".json"))
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        doc = json.loads(await response.read())
        self.assertIn("messages.sample_log", doc["tables"])

    async def test_gzip_and_no_messages_options(self):
        await self.seed()
        client = await self.client()
        response = await client.get(self.path("/backup/export?gzip=1&messages=0"))
        raw = await response.read()
        self.assertTrue(response.headers["Content-Disposition"].rstrip('"').endswith(".json.gz"))
        doc = json.loads(gzip.decompress(raw))
        self.assertNotIn("messages.sample_log", doc["tables"])

    async def test_a_gzip_file_can_be_restored(self):
        await self.seed()
        rows = await self.snapshot_rows()
        body = gzip.compress(await self.export_bytes())
        await self.wipe()
        client = await self.client()
        job = await self.preview_of(client, body)
        await self.restore(client, job["result"])
        self.assertEqual(await self.snapshot_rows(), rows)

    async def test_staff_can_do_none_of_this(self):
        client = await self.client(access="staff")
        calls = [
            client.get(self.path("/backup")), client.get(self.path("/backup/export")), client.get(self.path("/backup/job")),
            client.post(self.path("/backup/upload"), data=b"{}"), client.post(self.path("/backup/restore"), json={}),
            client.post(self.path("/backup/snapshots")), client.get(self.path("/backup/snapshots/1/download")),
            client.post(self.path("/backup/snapshots/1/preview")), client.delete(self.path("/backup/snapshots/1")), client.delete(self.path("/backup/staged")),
        ]
        for call in calls:
            self.assertEqual((await call).status, 403)


class SecretsTest(BackupCase):
    async def test_a_secret_never_leaves_in_the_clear_and_comes_back_whole(self):
        await self.bot.settings.save(self.guild.id, "sample", {"token": "hunter2"})
        await self.bot.settings.save(self.guild.id, "sample", {"token": "hunter3"})
        await self.seed()
        body = await self.export_bytes()
        self.assertNotIn(b"hunter", body)
        self.assertNotIn(b"very secret value", body)
        doc = json.loads(body)
        self.assertIn("$sealed", doc["settings"]["sample"]["token"])
        await self.bot.settings.restore(self.guild.id, "sample", {})
        self.assertEqual(self.settings("sample")["token"], "")
        await self.db.run("DELETE FROM sample_tokens")
        client = await self.client()
        job = await self.preview_of(client, body)
        text = json.dumps(job["result"])
        self.assertNotIn("hunter", text)
        await self.restore(client, job["result"])
        self.assertEqual(self.settings("sample")["token"], "hunter3")
        self.assertEqual(await self.db.scalar("SELECT api_token FROM sample_tokens WHERE guild_id = ?", (self.guild.id,)), "very secret value")

    async def test_undo_still_works_after_a_restore_and_old_forms_carry_no_clear_secret(self):
        await self.bot.settings.save(self.guild.id, "sample", {"token": "first"})
        await self.bot.settings.save(self.guild.id, "sample", {"token": "second", "limit": 3})
        body = await self.export_bytes()
        self.assertNotIn(b'"first"', body)
        self.assertNotIn(b'\\"first\\"', body)
        await self.db.run("DELETE FROM activity")
        client = await self.client()
        job = await self.preview_of(client, body)
        await self.restore(client, job["result"])
        row = await self.db.one("SELECT before_data FROM activity WHERE guild_id = ? AND module = 'sample' AND after_data LIKE '%second%'", (self.guild.id,))
        self.assertEqual(json.loads(row["before_data"])["token"], "first")

    async def test_a_secret_edited_in_by_hand_is_ignored(self):
        await self.bot.settings.save(self.guild.id, "sample", {"token": "real"})
        doc = await self.export_doc()
        doc["settings"]["sample"]["token"] = "typed by a stranger"
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        self.assertTrue(any("isn't sealed" in text for text in job["result"]["warnings"]))
        await self.restore(client, job["result"])
        self.assertEqual(self.settings("sample")["token"], "real")

    async def test_a_seal_cannot_be_moved_to_another_field_or_server(self):
        await self.bot.settings.save(self.guild.id, "sample", {"token": "real"})
        doc = await self.export_doc()
        sealed = doc["settings"]["sample"]["token"]
        doc["settings"]["sample"]["nested"]["name"] = sealed
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        self.assertTrue(job["result"]["warnings"])
        await self.restore(client, job["result"])
        self.assertEqual(self.settings("sample")["nested"]["name"], "x")
        with self.assertRaises(backup_module.BadCell):
            self.backup._unseal(OTHER_GUILD, "sample.token", sealed)

    async def test_a_seal_from_another_install_keeps_what_is_stored(self):
        await self.bot.settings.save(self.guild.id, "sample", {"token": "real"})
        doc = await self.export_doc()
        doc["settings"]["sample"]["token"] = {"$sealed": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        self.assertTrue(any("can't be read here" in text for text in job["result"]["warnings"]))
        await self.restore(client, job["result"])
        self.assertEqual(self.settings("sample")["token"], "real")


class HostileFileTest(BackupCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.neighbor = FakeGuild("Neighbor")
        self.bot.fake_guilds.append(self.neighbor)
        self.neighbor.me = FakeMember(self.neighbor, "Bot", bot=True)
        self.their_channel = FakeChannel(self.neighbor, "their-channel")

    async def test_a_file_for_another_server_is_refused(self):
        await self.seed(OTHER_GUILD)
        body = await self.export_bytes(OTHER_GUILD)
        client = await self.client()
        job = await self.preview_of(client, body)
        self.assertIn("server it came from", job["error"])

    async def test_a_guild_id_in_a_row_cannot_write_into_another_server(self):
        doc = await self.export_doc()
        table = doc["tables"]["main.sample_pairs"]
        table["rows"] = [[str(self.neighbor.id), "planted", "x"]]
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        await self.restore(client, job["result"])
        row = await self.db.one("SELECT guild_id FROM sample_pairs WHERE name = 'planted'")
        self.assertEqual(row["guild_id"], self.guild.id)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_pairs WHERE guild_id = ?", (self.neighbor.id,)), 0)

    async def test_another_servers_rows_are_never_overwritten_or_deleted(self):
        await self.seed(OTHER_GUILD)
        before = await self.snapshot_rows(OTHER_GUILD)
        theirs = await self.db.one("SELECT id FROM sample_items WHERE guild_id = ?", (OTHER_GUILD,))
        await self.seed()
        doc = await self.export_doc()
        items = doc["tables"]["main.sample_items"]
        at = items["columns"].index("id")
        name_at = items["columns"].index("name")
        items["rows"][0][at] = theirs["id"]
        items["rows"][0][name_at] = "planted"
        children = doc["tables"]["main.sample_children"]
        children["rows"] = [[items["rows"][0][at], "child of planted"]]
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        done = await self.restore(client, job["result"])
        self.assertIsNone(done["error"])
        self.assertEqual(await self.snapshot_rows(OTHER_GUILD), before)
        planted = await self.db.one("SELECT id, guild_id FROM sample_items WHERE name = 'planted'")
        self.assertEqual(planted["guild_id"], self.guild.id)
        self.assertNotEqual(planted["id"], theirs["id"])
        self.assertEqual(done["result"]["tables"]["main.sample_items"]["renumbered"], 1)
        child = await self.db.one("SELECT item_id FROM sample_children WHERE value = 'child of planted'")
        self.assertEqual(child["item_id"], planted["id"], "a child follows its parent's new number")

    async def test_a_child_row_cannot_hang_off_another_servers_parent(self):
        theirs = await self.db.insert("INSERT INTO sample_items (guild_id, name) VALUES (?, 'theirs')", (OTHER_GUILD,))
        await self.seed()
        doc = await self.export_doc()
        doc["tables"]["main.sample_children"]["rows"].append([theirs, "planted"])
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        children = next(t for t in job["result"]["tables"] if t["table"] == "sample_children")
        self.assertEqual(children["foreign"], 1)
        await self.restore(client, job["result"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_children WHERE item_id = ?", (theirs,)), 0)

    async def test_an_inferred_child_cannot_hang_off_another_servers_parent(self):
        await self.db.run("INSERT INTO sample_messages (message_id, guild_id) VALUES (?, ?)", (HUGE, OTHER_GUILD))
        doc = await self.export_doc()
        doc["tables"]["main.sample_seen"]["rows"] = [[str(HUGE), 5]]
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        await self.restore(client, job["result"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_seen"), 0)

    async def test_a_row_cannot_name_another_servers_channel(self):
        doc = await self.export_doc()
        items = doc["tables"]["main.sample_items"]
        columns = items["columns"]
        row = [None] * len(columns)
        row[columns.index("guild_id")] = str(self.guild.id)
        row[columns.index("name")] = "planted"
        row[columns.index("channel_id")] = str(self.their_channel.id)
        items["rows"] = [row]
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        await self.restore(client, job["result"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_items WHERE name = 'planted'"), 0)

    async def test_a_timer_written_by_hand_cannot_come_in(self):
        await self.bot.timers.schedule("timedrole", time.time() + 50, {"guild_id": self.guild.id, "user_id": 1, "role_id": 2}, guild_id=self.guild.id, key="real")
        doc = await self.export_doc()
        timers = doc["tables"]["main.timers"]
        columns = timers["columns"]
        real = next(row for row in timers["rows"] if row[columns.index("key")] == "real")

        def timer(payload, key, mark="0" * 64):
            row = [None] * len(columns)
            row[columns.index("guild_id")] = str(self.guild.id)
            row[columns.index("kind")] = "giveaway"
            row[columns.index("key")] = key
            row[columns.index("due_at")] = 1.0
            row[columns.index("payload")] = json.dumps(payload)
            row[columns.index("tries")] = 0
            row[columns.index("created_at")] = 1.0
            row[columns.index("$mark")] = mark
            return row

        changed = list(real)
        changed[columns.index("key")] = "changed after export"
        changed[columns.index("payload")] = json.dumps({"id": 12345})
        timers["rows"] = [
            real,
            changed,
            timer({"id": 999}, "evil:row-in-another-server"),
            timer({"id": 999}, "evil:mark-copied", real[columns.index("$mark")]),
        ]
        await self.db.run("DELETE FROM timers WHERE key = 'real'")
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        row = next(t for t in job["result"]["tables"] if t["table"] == "timers")
        self.assertEqual(row["foreign"], 3)
        self.assertTrue(any("scheduled jobs" in text for text in job["result"]["warnings"]))
        await self.restore(client, job["result"])
        keys = {row["key"] for row in await self.db.all("SELECT key FROM timers WHERE guild_id = ?", (self.guild.id,))}
        self.assertEqual(keys, {"real"})

    async def test_history_written_by_hand_cannot_come_in(self):
        await self.bot.activity_log.record(self.guild.id, None, "real.action", detail="true")
        doc = await self.export_doc()
        activity = doc["tables"]["main.activity"]
        columns = activity["columns"]
        real = next(row for row in activity["rows"] if row[columns.index("action")] == "real.action")
        forged = list(real)
        forged[columns.index("id")] = 987654
        forged[columns.index("user_name")] = "someone innocent"
        forged[columns.index("action")] = "ban.everyone"
        activity["rows"].append(forged)
        await self.db.run("DELETE FROM activity")
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        self.assertTrue(any("activity rows" in text for text in job["result"]["warnings"]))
        await self.restore(client, job["result"])
        actions = {row["action"] for row in await self.db.all("SELECT action FROM activity")}
        self.assertIn("real.action", actions)
        self.assertNotIn("ban.everyone", actions)

    async def test_a_message_that_is_json_naming_another_server_still_comes_back(self):
        pasted = json.dumps({"guild_id": self.neighbor.id, "channel_id": self.their_channel.id})
        await self.message_db.run("INSERT INTO sample_log (guild_id, body) VALUES (?, ?)", (self.guild.id, pasted))
        doc = await self.export_doc()
        await self.message_db.run("DELETE FROM sample_log")
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        await self.restore(client, job["result"])
        self.assertEqual(await self.message_db.scalar("SELECT body FROM sample_log"), pasted)

    async def test_a_marked_timer_still_cannot_name_another_server(self):
        await self.bot.timers.schedule("timedrole", time.time() + 50, {"guild_id": self.neighbor.id}, guild_id=self.guild.id, key="odd")
        await self.bot.timers.schedule("timedrole", time.time() + 50, {"channel_id": self.their_channel.id}, guild_id=self.guild.id, key="odder")
        doc = await self.export_doc()
        await self.db.run("DELETE FROM timers WHERE key IN ('odd', 'odder')")
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        await self.restore(client, job["result"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE key IN ('odd', 'odder')"), 0)

    async def test_a_file_from_before_marks_or_with_no_mark_column_brings_no_timers(self):
        await self.bot.timers.schedule("timedrole", time.time() + 50, {}, guild_id=self.guild.id, key="real")
        doc = await self.export_doc()
        timers = doc["tables"]["main.timers"]
        at = timers["columns"].index("$mark")
        timers["columns"].pop(at)
        timers["rows"] = [row[:at] + row[at + 1:] for row in timers["rows"]]
        await self.db.run("DELETE FROM timers WHERE key = 'real'")
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        await self.restore(client, job["result"], [*self.all_items(job["result"]), "table:main.timers"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE key = 'real'"), 0)

    async def test_settings_go_through_their_checks_and_cannot_name_another_servers_channel(self):
        doc = await self.export_doc()
        doc["settings"]["sample"].update({"limit": 9999, "channel": str(self.their_channel.id), "made_up": 1, "nested": {"on": "maybe", "name": "fine"}})
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        warnings = " ".join(job["result"]["warnings"])
        for needle in ("sample.limit", "sample.channel", "sample.made_up", "sample.nested.on"):
            self.assertIn(needle, warnings)
        await self.restore(client, job["result"])
        settings = self.settings("sample")
        self.assertEqual(settings["limit"], 5)
        self.assertIsNone(settings["channel"])
        self.assertFalse(settings["nested"]["on"])
        self.assertEqual(settings["nested"]["name"], "fine")
        self.assertNotIn("made_up", settings)

    async def test_unknown_tables_and_columns_are_ignored_and_reported(self):
        doc = await self.export_doc()
        doc["tables"]["main.web_sessions"] = {"columns": ["token_hash", "user_id", "user_name", "created_at", "expires_at"], "rows": [["h", 1, "x", 1.0, 9e12]]}
        doc["tables"]["main.not_real"] = {"columns": ["a"], "rows": [[1]]}
        doc["tables"]["main.kv"] = {"columns": ["key", "value"], "rows": [["commands_digest", "x"]]}
        doc["tables"]["main.backup_snapshots"] = {"columns": ["id", "guild_id", "file"], "rows": [[1, 1, "../../evil"]]}
        pairs = doc["tables"]["main.sample_pairs"]
        pairs["columns"].append("gone_now")
        pairs["rows"] = [[str(self.guild.id), "kept", "x", "extra"]]
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        warnings = " ".join(job["result"]["warnings"])
        self.assertIn("main.not_real", warnings)
        self.assertIn("gone_now", warnings)
        await self.restore(client, job["result"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM web_sessions"), 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM backup_snapshots WHERE file = '../../evil'"), 0)
        self.assertIsNone(await self.db.scalar("SELECT value FROM kv WHERE key = 'commands_digest' AND value = 'x'"))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_pairs WHERE name = 'kept'"), 1)

    async def test_bad_rows_are_dropped_and_counted_and_the_rest_comes_back(self):
        doc = await self.export_doc()
        pairs = doc["tables"]["main.sample_pairs"]
        pairs["rows"] = [
            "not a list", [1], [str(self.guild.id), {"bad": 1}, "x"], [str(self.guild.id), ["list"], "x"],
            [str(self.guild.id), None, "name is required"], [str(self.guild.id), "good", "x"],
        ]
        client = await self.client()
        job = await self.preview_of(client, json.dumps(doc).encode())
        row = next(t for t in job["result"]["tables"] if t["table"] == "sample_pairs")
        self.assertEqual(row["dropped"], 4)
        done = await self.restore(client, job["result"])
        self.assertIsNone(done["error"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM sample_pairs WHERE guild_id = ?", (self.guild.id,)), 1)
        self.assertEqual(done["result"]["tables"]["main.sample_pairs"]["dropped"], 5)

    async def test_files_that_are_not_backups_are_refused_with_a_reason(self):
        client = await self.client()
        cases = {
            b"not json at all": "backup format",
            b"[1, 2, 3]": "backup format",
            b'{"format": "something else", "settings": {}, "tables": {}}': "isn't a backup file",
            b'{"format": "captainguild-backup", "version": 999, "guild_id": "1", "tables": {}}': "newer version",
            b"\xff\xfe\x00\x01": "readable text",
            b'{"format": "captainguild-backup", "version": 1': "ends early",
        }
        for body, expected in cases.items():
            job = await self.preview_of(client, body)
            self.assertIsNotNone(job["error"], body)
            self.assertIn(expected, job["error"], body)

    async def test_a_cut_off_download_changes_nothing(self):
        await self.seed()
        rows = await self.snapshot_rows()
        body = await self.export_bytes()
        client = await self.client()
        job = await self.preview_of(client, body[: len(body) // 2])
        self.assertIn("ends early", job["error"])
        self.assertEqual(await self.snapshot_rows(), rows)
        gz = gzip.compress(body)
        job = await self.preview_of(client, gz[: len(gz) // 2])
        self.assertIsNotNone(job["error"])
        self.assertEqual(await self.snapshot_rows(), rows)

    async def test_a_download_a_web_server_cut_off_with_its_own_error_text_is_refused(self):
        await self.seed()
        rows = await self.snapshot_rows()
        body = await self.export_bytes()
        complaint = b"<p>The dynamic response body size is over the limit, the response will be truncated by the web server."
        client = await self.client()
        job = await self.preview_of(client, body[: len(body) // 2] + complaint)
        self.assertIsNotNone(job["error"])
        gz = gzip.compress(body)
        job = await self.preview_of(client, gz[: len(gz) // 2] + complaint)
        self.assertIsNotNone(job["error"])
        self.assertEqual(await self.snapshot_rows(), rows)

    async def test_an_empty_upload_is_refused(self):
        client = await self.client()
        response = await client.post(self.path("/backup/upload"), data=b"")
        self.assertEqual(response.status, 400)

    async def test_restore_needs_the_token_from_the_preview(self):
        await self.seed()
        client = await self.client()
        job = await self.preview_of(client, await self.export_bytes())
        response = await client.post(self.path("/backup/restore"), json={"token": "wrong", "items": ["settings:sample"]})
        self.assertEqual(response.status, 409)
        response = await client.post(self.path("/backup/restore"), json={"token": job["result"]["token"], "items": []})
        self.assertEqual(response.status, 400)
        response = await client.post(self.path("/backup/restore"), json={"token": job["result"]["token"], "items": "all"})
        self.assertEqual(response.status, 400)

    async def test_a_cancelled_upload_goes_away(self):
        client = await self.client()
        job = await self.preview_of(client, await self.export_bytes())
        response = await client.delete(self.path("/backup/staged"))
        self.assertEqual(response.status, 200)
        response = await client.post(self.path("/backup/restore"), json={"token": job["result"]["token"], "items": ["settings:sample"]})
        self.assertEqual(response.status, 409)
        folder = self.backup._root / str(self.guild.id)
        self.assertEqual([p.name for p in folder.iterdir() if p.name.startswith("upload-")], [])

    async def test_a_restore_that_cannot_finish_changes_nothing(self):
        await self.seed()
        rows = await self.snapshot_rows()
        body = await self.export_bytes()
        await self.db.run("UPDATE sample_pairs SET value = 'changed'")
        changed = await self.snapshot_rows()
        self.assertNotEqual(changed, rows)
        client = await self.client()
        job = await self.preview_of(client, body)

        async def boom(*args, **kwargs):
            raise RuntimeError("the disk went away")

        real = self.backup._insert_rows
        calls = 0

        async def fail_late(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls >= 3:
                await boom()
            return await real(*args, **kwargs)

        self.backup._insert_rows = fail_late
        with self.assertLogs("captainguild.modules.backup", level="ERROR"):
            done = await self.restore(client, job["result"])
        self.assertIsNotNone(done["error"])
        self.assertEqual(await self.snapshot_rows(), changed, "the main database rolled back as one")
        await self.db.run("INSERT INTO sample_pairs (guild_id, name) VALUES (?, 'still writable')", (self.guild.id,))


class SnapshotTest(BackupCase):
    async def overview(self, client):
        return await (await client.get(self.path("/backup"))).json()

    async def test_take_list_restore_and_delete(self):
        await self.seed()
        rows = await self.snapshot_rows()
        client = await self.client()
        response = await client.post(self.path("/backup/snapshots"))
        self.assertEqual(response.status, 200)
        job = await self.finished(client)
        self.assertIsNone(job["error"])
        data = await self.overview(client)
        self.assertEqual(len(data["snapshots"]), 1)
        snapshot = data["snapshots"][0]
        self.assertEqual(snapshot["kind"], "manual")
        self.assertEqual(snapshot["created_by"], self.admin.name)
        self.assertGreater(snapshot["row_count"], 5)
        self.assertGreater(snapshot["size"], 0)
        self.assertFalse(snapshot["includes_messages"])
        await self.wipe()
        await client.post(self.path(f"/backup/snapshots/{snapshot['id']}/preview"))
        job = await self.finished(client)
        self.assertIsNone(job["error"])
        await self.restore(client, job["result"])
        restored = await self.snapshot_rows()
        rows["sample_log"] = restored["sample_log"]  # snapshots leave the message store out by default
        self.assertEqual(restored, rows)
        self.assertEqual({row["kind"] for row in (await self.overview(client))["snapshots"]}, {"manual", "before import"})
        response = await client.delete(self.path(f"/backup/snapshots/{snapshot['id']}"))
        self.assertEqual(response.status, 200)
        self.assertEqual({row["kind"] for row in (await self.overview(client))["snapshots"]}, {"before import"})

    async def test_a_restore_keeps_a_way_back(self):
        await self.seed()
        client = await self.client()
        body = await self.export_bytes()
        await self.db.run("UPDATE sample_pairs SET value = 'newer'")
        job = await self.preview_of(client, body)
        done = await self.restore(client, job["result"])
        self.assertEqual(await self.db.scalar("SELECT value FROM sample_pairs WHERE guild_id = ?", (self.guild.id,)), None)
        back = done["result"]["before_snapshot"]
        await client.post(self.path(f"/backup/snapshots/{back}/preview"))
        job = await self.finished(client)
        await self.restore(client, job["result"])
        self.assertEqual(await self.db.scalar("SELECT value FROM sample_pairs WHERE guild_id = ?", (self.guild.id,)), "newer")

    async def test_files_on_disk_are_encrypted(self):
        await self.bot.settings.save(self.guild.id, "sample", {"limit": 8})
        await self.seed()
        await self.db.run("INSERT INTO sample_pairs (guild_id, name, value) VALUES (?, 'marker', 'SEARCHABLEMARKER')", (self.guild.id,))
        client = await self.client()
        await client.post(self.path("/backup/snapshots"))
        await self.finished(client)
        files = list((self.backup._root / str(self.guild.id)).iterdir())
        self.assertEqual(len(files), 1)
        raw = files[0].read_bytes()
        self.assertFalse(raw.startswith(b"SQLite format 3"))
        self.assertNotIn(b"SEARCHABLEMARKER", raw)

    async def test_download_is_a_restorable_gzip(self):
        await self.seed()
        client = await self.client()
        await client.post(self.path("/backup/snapshots"))
        await self.finished(client)
        snapshot = (await self.overview(client))["snapshots"][0]
        response = await client.get(self.path(f"/backup/snapshots/{snapshot['id']}/download"))
        self.assertEqual(response.status, 200)
        raw = await response.read()
        doc = json.loads(gzip.decompress(raw))
        self.assertEqual(doc["guild_id"], str(self.guild.id))
        job = await self.preview_of(client, raw)
        self.assertIsNone(job["error"])
        self.assertIn("backup.download", [row["action"] for row in await self.bot.activity_log.recent(self.guild.id)])

    async def test_another_servers_snapshot_cannot_be_reached(self):
        row = await self.backup._write_snapshot(OTHER_GUILD, kind="manual", actor_name="x", include_messages=False)
        client = await self.client()
        for call in (
            client.get(self.path(f"/backup/snapshots/{row['id']}/download")),
            client.post(self.path(f"/backup/snapshots/{row['id']}/preview")),
            client.delete(self.path(f"/backup/snapshots/{row['id']}")),
        ):
            self.assertEqual((await call).status, 404)
        self.assertEqual((await self.overview(client))["snapshots"], [])
        self.assertIsNotNone(await self.db.one("SELECT id FROM backup_snapshots WHERE id = ?", (row["id"],)))

    async def test_only_one_job_at_a_time_per_server(self):
        client = await self.client()
        hold = asyncio.Event()

        async def slow(job):
            await hold.wait()

        self.backup._start_job(self.guild.id, "test", slow)
        response = await client.post(self.path("/backup/snapshots"))
        self.assertEqual(response.status, 409)
        response = await client.post(self.path("/backup/upload"), data=b"{}")
        self.assertEqual(response.status, 409)
        hold.set()
        await self.backup.wait_for_job(self.guild.id)

    async def test_failure_in_a_job_is_reported_and_logged(self):
        async def broken(job):
            raise RuntimeError("boom")

        self.backup._start_job(self.guild.id, "test", broken)
        with self.assertLogs("captainguild.modules.backup", level="ERROR"):
            job = await self.backup.wait_for_job(self.guild.id)
        self.assertIn("Something went wrong", job.error)
        self.assertFalse(job.running)


class ScheduleTest(BackupCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops.guild_ids = lambda: [self.guild.id]

    async def count(self, kind="scheduled"):
        return await self.db.scalar("SELECT COUNT(*) FROM backup_snapshots WHERE guild_id = ? AND kind = ?", (self.guild.id, kind))

    async def test_on_by_default_and_only_once_per_interval(self):
        self.assertEqual(await self.backup.take_due_snapshots(), 1)
        self.assertEqual(await self.count(), 1)
        self.assertEqual(await self.backup.take_due_snapshots(), 0)
        await self.db.run("UPDATE backup_snapshots SET created_at = created_at - ?", (25 * 3600,))
        self.assertEqual(await self.backup.take_due_snapshots(), 1)
        self.assertEqual(await self.count(), 2)
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "backup.snapshot")
        self.assertIsNone(rows[0]["user_name"])

    async def test_turned_off_means_none(self):
        await self.bot.settings.save(self.guild.id, "backup", {"enabled": False})
        self.assertEqual(await self.backup.take_due_snapshots(), 0)
        self.assertEqual(await self.count(), 0)

    async def test_the_interval_comes_from_the_settings(self):
        await self.bot.settings.save(self.guild.id, "backup", {"interval_hours": 168})
        await self.backup.take_due_snapshots()
        await self.db.run("UPDATE backup_snapshots SET created_at = created_at - ?", (25 * 3600,))
        self.assertEqual(await self.backup.take_due_snapshots(), 0)

    async def test_old_ones_are_pruned_and_their_files_removed(self):
        await self.bot.settings.save(self.guild.id, "backup", {"keep": 2})
        for _ in range(4):
            await self.backup.take_due_snapshots()
            await self.db.run("UPDATE backup_snapshots SET created_at = created_at - ?", (25 * 3600,))
        self.assertEqual(await self.count(), 2)
        files = list((self.backup._root / str(self.guild.id)).iterdir())
        self.assertEqual(len(files), 2)

    async def test_pruning_leaves_the_snapshot_someone_is_looking_at(self):
        await self.bot.settings.save(self.guild.id, "backup", {"keep": 1})
        client = await self.client()
        await self.backup.take_due_snapshots()
        oldest = await self.db.one("SELECT * FROM backup_snapshots WHERE kind = 'scheduled'")
        await client.post(self.path(f"/backup/snapshots/{oldest['id']}/preview"))
        job = await self.finished(client)
        for _ in range(2):
            await self.db.run("UPDATE backup_snapshots SET created_at = created_at - ?", (25 * 3600,))
            await self.backup.take_due_snapshots()
        self.assertIsNotNone(await self.db.one("SELECT id FROM backup_snapshots WHERE id = ?", (oldest["id"],)))
        done = await self.restore(client, job["result"], ["settings:core"])
        self.assertIsNone(done["error"])

    async def test_pruning_leaves_snapshots_taken_by_hand(self):
        await self.bot.settings.save(self.guild.id, "backup", {"keep": 1})
        await self.backup._write_snapshot(self.guild.id, kind="manual", actor_name="x", include_messages=False)
        for _ in range(3):
            await self.backup.take_due_snapshots()
            await self.db.run("UPDATE backup_snapshots SET created_at = created_at - ?", (25 * 3600,))
        self.assertEqual(await self.count("manual"), 1)

    async def test_messages_can_be_included_when_asked(self):
        await self.seed()
        await self.bot.settings.save(self.guild.id, "backup", {"include_messages": True})
        await self.backup.take_due_snapshots()
        row = await self.db.one("SELECT * FROM backup_snapshots WHERE kind = 'scheduled'")
        self.assertEqual(row["includes_messages"], 1)

    async def test_the_timer_starts_it_in_the_background_and_comes_back(self):
        due = await self.bot.timers.get_key(backup_module.SWEEP_KIND, "all")
        self.assertIsNotNone(due)
        self.assertIsNone(due["guild_id"], "the schedule itself isn't part of any server's backup")
        await self.bot.timers.run_due(due["due_at"] + 1)
        await self.backup._sweep_task
        self.assertEqual(await self.count(), 1)
        again = await self.bot.timers.get_key(backup_module.SWEEP_KIND, "all")
        self.assertGreater(again["due_at"], due["due_at"])

    async def test_the_schedule_survives_a_restart_without_a_second_timer(self):
        fresh = Backup(self.bot)
        await fresh.on_load()
        rows = await self.db.all("SELECT id FROM timers WHERE kind = ?", (backup_module.SWEEP_KIND,))
        self.assertEqual(len(rows), 1)

    async def test_a_restart_cleans_half_written_files_and_keeps_good_ones(self):
        row = await self.backup._write_snapshot(self.guild.id, kind="manual", actor_name="x", include_messages=False)
        folder = self.backup._root / str(self.guild.id)
        (folder / "abc.db.part").write_bytes(b"half")
        (folder / "upload-x.db").write_bytes(b"left over")
        fresh = Backup(self.bot)
        await fresh._tidy_files()
        self.assertEqual([p.name for p in folder.iterdir()], [row["file"]])

    async def test_settings_are_checked(self):
        client = await self.client()
        self.assertEqual((await client.put(self.path("/settings/backup"), json={"interval_hours": 7})).status, 400)
        self.assertEqual((await client.put(self.path("/settings/backup"), json={"keep": 0})).status, 400)
        self.assertEqual((await client.put(self.path("/settings/backup"), json={"interval_hours": 48, "keep": 3})).status, 200)


class FormatTest(BotTest):
    async def read(self, *pieces):
        async def blocks():
            for piece in pieces:
                yield piece

        return [event async for event in read_events(inflate(blocks()))]

    def test_cells_survive_the_trip(self):
        for value, kind in ((None, "integer"), (5, "integer"), (HUGE, "integer"), (-HUGE, "integer"), (HUGE, "blob"), (1.5, "real"), ("text", "text"), ("123", "text"), (b"\x00\xff", "blob")):
            encoded = encode_cell(value, kind)
            self.assertEqual(decode_cell(json.loads(json.dumps(encoded)), kind), value, (value, kind))

    def test_text_that_only_looks_like_a_number_stays_text(self):
        self.assertEqual(decode_cell("123", "text"), "123")
        self.assertEqual(decode_cell("123", "blob"), "123")
        self.assertEqual(decode_cell("123", "integer"), 123)

    def test_a_number_column_takes_only_whole_numbers(self):
        for value in ("abc", " 123", "123 ", "+123", "1e3", "", 12.0, 1.5, {"$float": "1.5"}, {"$b64": "AA=="}, 2 ** 63, "9" * 20):
            with self.assertRaises(backup_module.BadCell, msg=repr(value)):
                decode_cell(value, "integer")
            with self.assertRaises(backup_module.BadCell, msg=repr(value)):
                decode_cell(value, "numeric")
        self.assertEqual(decode_cell(None, "integer"), None)
        self.assertEqual(decode_cell(True, "integer"), 1)
        self.assertEqual(decode_cell({"$int": "12"}, "integer"), 12)
        self.assertEqual(decode_cell(-(2 ** 63), "integer"), -(2 ** 63))

    def test_impossible_cells_are_refused(self):
        for value in ({"a": 1}, [1], {"$b64": "!!!"}, {"$int": "x"}, {"$b64": "AA==", "extra": 1}):
            with self.assertRaises(backup_module.BadCell):
                decode_cell(value, "integer")

    async def test_a_value_can_split_across_any_block_boundary(self):
        document = json.dumps({"format": "f", "settings": {"a": {"b": "\U0001f389 long"}}, "tables": {"t": {"columns": ["x", "y"], "rows": [[12345, "caf\u00e9"], [6789, None], [1.25, "z"]]}}}, ensure_ascii=False).encode()
        whole = await self.read(document)
        for size in (1, 2, 3, 7, 64):
            pieces = [document[i:i + size] for i in range(0, len(document), size)]
            self.assertEqual(await self.read(*pieces), whole, size)
        rows = next(event for event in whole if event[0] == "rows")
        self.assertEqual(rows[2], [[12345, "caf\u00e9"], [6789, None], [1.25, "z"]])

    async def test_any_whitespace_and_key_order_work(self):
        document = json.dumps({"tables": {"t": {"columns": ["x"], "rows": [[1], [2]]}}, "settings": {"m": {}}, "format": "f"}, indent=4, sort_keys=True).encode()
        events = await self.read(document)
        self.assertEqual([e[0] for e in events], ["meta", "settings", "table", "rows", "end"])

    async def test_a_byte_order_mark_is_fine(self):
        events = await self.read(chr(0xFEFF).encode("utf-8") + b'{"format": "f"}')
        self.assertEqual(events, [("meta", "format", "f")])

    async def test_broken_documents_raise_a_sentence(self):
        for broken in (b"", b"{", b'{"a"', b'{"a":', b'{"a":1', b'{"a":1,', b"nope", b'{"tables":{"t":{"rows":[[1]]}}}', b'{"a":1} trailing', b'{"tables":{"t":{"columns":["x"],"rows":[[1],'):
            with self.assertRaises(BackupError, msg=broken):
                await self.read(broken)

    async def test_gzip_bomb_is_stopped(self):
        original = backup_module.MAX_INFLATED_BYTES
        backup_module.MAX_INFLATED_BYTES = 1000
        try:
            with self.assertRaises(BackupError):
                await self.read(gzip.compress(b" " * 100000))
        finally:
            backup_module.MAX_INFLATED_BYTES = original

    async def test_very_deep_nesting_is_refused(self):
        with self.assertRaises(BackupError):
            await self.read(b'{"a":' + b"[" * 200000 + b"]" * 200000 + b"}")


class EveryRealModuleTest(BotTest):
    """Runs against whatever modules exist today, so a table added later is covered by this test the day it appears."""
    modules = load_all()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        asyncio.get_running_loop().set_debug(False)

    async def catalog(self):
        backup = self.cog(Backup)
        return backup, await backup.catalogs(("main", self.db), ("messages", self.message_db))

    def value_for(self, table, column, number, parents):
        for link in table.links:
            if link.column == column:
                return parents[f"{table.database}.{link.parent}"][link.parent_column]
        kind = table.affinity[column]
        if column == "guild_id":
            return self.guild.id
        if kind == "integer":
            return 1000 + number
        if kind == "real":
            return 1500.25 + number
        if kind == "blob":
            return bytes([number % 256, 0, 255])
        return f"value {number} {column}"

    async def test_every_table_can_be_tied_to_a_server(self):
        _, catalog = await self.catalog()
        self.assertEqual(catalog.left_out, [], "A table with neither guild_id nor a link to one can't be backed up. Add a guild_id column.")

    async def test_every_module_can_reload_after_a_restore(self):
        for module in self.bot.modules:
            await module.on_guild_restored(self.guild.id)

    async def test_every_table_in_every_module_round_trips(self):
        backup, catalog = await self.catalog()

        async def nothing(guild_id):
            pass

        # Some modules clear rows on purpose once a restore finishes, such as a bulk change that was running back then. This test is about what Backup itself writes.
        for module in self.bot.modules:
            module.on_guild_restored = nothing
        parents: dict = {}
        seeded = {}
        skipped = []
        for number, table in enumerate(catalog.ordered()):
            database = self.db if table.database == "main" else self.message_db
            row = {column: self.value_for(table, column, number, parents) for column in table.columns}
            names = ", ".join(f'"{c}"' for c in row)
            marks = ", ".join("?" for _ in row)
            try:
                await database.run(f'INSERT INTO "{table.name}" ({names}) VALUES ({marks})', list(row.values()))
            except Exception as error:  # a CHECK rule this generic row can't meet
                skipped.append((table.key, str(error)))
                continue
            parents[table.key] = row
            seeded[table.key] = row

        async def current():
            out = {}
            for key in seeded:
                table = catalog.tables[key]
                database = self.db if table.database == "main" else self.message_db
                out[key] = await database.all(f'SELECT * FROM "{table.name}" WHERE {table.where}', (self.guild.id,))
            return out

        before = await current()
        self.assertGreater(len(seeded), 40, f"Too few tables got a row: {skipped}")
        self.assertFalse([k for k, _ in skipped], f"Tables that couldn't take a generic row: {skipped}")
        body = bytearray()
        async for block in backup.export_stream(self.guild.id, include_messages=True):
            body += block
        doc = json.loads(bytes(body))
        for key, rows in before.items():
            self.assertEqual(len(doc["tables"][key]["rows"]), len(rows), key)
        for table in reversed(catalog.ordered()):
            database = self.db if table.database == "main" else self.message_db
            await database.run(f'DELETE FROM "{table.name}" WHERE {table.where}', (self.guild.id,))
        client = await self.client()
        response = await client.post(self.path("/backup/upload"), data=bytes(body))
        self.assertEqual(response.status, 200)
        await backup.wait_for_job(self.guild.id)
        job = backup._jobs[self.guild.id]
        self.assertIsNone(job.error)
        items = [row["key"] for row in job.result["settings"]] + [row["key"] for row in job.result["tables"]]
        response = await client.post(self.path("/backup/restore"), json={"token": job.result["token"], "items": items})
        self.assertEqual(response.status, 200)
        await backup.wait_for_job(self.guild.id)
        job = backup._jobs[self.guild.id]
        self.assertIsNone(job.error)
        after = await current()
        for key, rows in before.items():
            if key in ("main.activity", "main.timers"):
                for row in rows:
                    self.assertIn(row, after[key], key)
                continue
            self.assertEqual(after[key], rows, key)

    async def test_every_modules_settings_export_and_come_back_unchanged(self):
        backup, _ = await self.catalog()
        before = {module: self.settings(module) for module in self.bot.settings.schemas}
        body = bytearray()
        async for block in backup.export_stream(self.guild.id, include_messages=False):
            body += block
        client = await self.client()
        await client.post(self.path("/backup/upload"), data=bytes(body))
        await backup.wait_for_job(self.guild.id)
        job = backup._jobs[self.guild.id]
        self.assertIsNone(job.error)
        self.assertEqual(job.result["warnings"], [])
        self.assertEqual([row for row in job.result["settings"] if row["changes"]], [])
        self.assertEqual({module: self.settings(module) for module in self.bot.settings.schemas}, before)

    async def test_owners_name_the_module_that_made_each_table(self):
        owners = table_owners(self.bot)
        self.assertEqual(owners["giveaways"], "utilities")
        self.assertEqual(owners["backup_snapshots"], "backup")
        self.assertEqual(owners["activity"], "activity")
