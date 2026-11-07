"""What one server's backup can cost everyone else: memory and time reading a file, disk, database reads, and rows past what a server can hold."""
import asyncio
import json
from unittest import mock

from captainguild.modules import backup as backup_module
from captainguild.modules.backup import Backup, BackupError, JsonStream, read_events
from captainguild.modules.notifications import MAX_SUBSCRIPTIONS
from tests.test_backup_timers import Case


async def stream_of(text, size=65536):
    async def blocks():
        data = text.encode()
        for start in range(0, len(data), size):
            yield data[start:start + size]

    return JsonStream(blocks())


class RowReaderTest(Case):
    async def row(self, text, size=65536):
        return await (await stream_of(text, size)).row()

    async def test_plain_rows_read_as_they_always_did(self):
        self.assertEqual(await self.row('[1, "a", null, {"$b64":"AA=="}, 1.5] '), [1, "a", None, {"$b64": "AA=="}, 1.5])
        self.assertEqual(await self.row('"not a list" '), "not a list")

    async def test_a_long_row_reads_cell_by_cell_and_comes_out_the_same(self):
        long_text = "x" * 100_000
        marked = "y" * 100_000
        row = [1, long_text, {"$b64": marked}, {"$sealed": marked}, None, 2.5, "é \\ \"quoted\"", True]
        for size in (7, 4096, 1_000_000):
            self.assertEqual(await self.row(json.dumps(row) + " ", size), row, size)

    async def test_small_oddities_still_come_back_for_the_row_cleaner_to_drop(self):
        filler = "x" * 20_000
        self.assertEqual(await self.row(json.dumps([filler, {"bad": 1}, ["list"]]) + " "), [filler, {"bad": 1}, ["list"]])

    async def test_a_flood_of_nested_lists_is_refused_at_the_first_large_cell(self):
        built = []
        original = json.JSONDecoder.raw_decode

        def counting(decoder, text, index=0):
            built.append(len(text) - index)
            return original(decoder, text, index)

        bomb = '["a", [' + ",".join(["[]"] * 1_000_000) + "]] "
        with mock.patch.object(json.JSONDecoder, "raw_decode", counting):
            with self.assertRaises(BackupError):
                await self.row(bomb)
        self.assertLess(max(built), 1_000_000, "the reader handed a whole nested cell to the parser")

    async def test_too_many_cells_is_refused(self):
        with self.assertRaises(BackupError):
            await self.row("[" + ",".join(['"' + "x" * 20 + '"'] * (backup_module.MAX_ROW_CELLS + 1)) + "] ")

    async def test_a_marked_cell_can_only_hold_its_string(self):
        filler = "x" * 20_000
        with self.assertRaises(BackupError):
            await self.row('["' + filler + '", {"$b64": "AA==", "extra": [[],[],[]]}] ')

    async def test_a_cut_off_row_says_so(self):
        with self.assertRaises(BackupError) as caught:
            await self.row('["' + "x" * 20_000)
        self.assertIn("ends early", str(caught.exception))

    async def test_a_string_past_the_old_limit_reads(self):
        long_text = "x" * (backup_module.MAX_VALUE_CHARS + 1024)
        self.assertEqual(len((await self.row(json.dumps([1, long_text]) + " "))[1]), len(long_text))

    async def test_a_string_past_the_new_limit_is_refused(self):
        with mock.patch.object(backup_module, "MAX_STRING_CHARS", 30_000):
            with self.assertRaises(BackupError):
                await self.row(json.dumps([1, "x" * 100_000]) + " ")

    async def test_a_second_settings_key_is_refused(self):
        async def blocks():
            yield b'{"settings":{},"settings":{}}'

        with self.assertRaises(BackupError):
            async for _ in read_events(blocks()):
                pass

    async def test_a_batch_ends_at_a_number_of_cells(self):
        row = [0] * 100
        document = {"tables": {"main.x": {"columns": ["a"], "rows": [row] * 1000}}}

        async def blocks():
            yield json.dumps(document).encode()

        batches = [event[2] async for event in read_events(blocks()) if event[0] == "rows"]
        self.assertLessEqual(max(sum(len(r) for r in batch) for batch in batches), backup_module.BATCH_CELLS)


class OversizeRowTest(Case):
    async def test_a_file_never_holds_a_row_it_cant_read_back_and_says_so(self):
        await self.db.run("INSERT INTO birthdays (guild_id, user_id, month, day, updated_at) VALUES (?, 1, 2, 3, 1.0)", (self.guild.id,))
        await self.db.run("INSERT INTO level_backups (guild_id, reason, made_at, members, data) VALUES (?, 'x', 1.0, 1, ?)", (self.guild.id, "x" * 200_000))
        with mock.patch.object(backup_module, "MAX_STRING_CHARS", 100_000):
            doc = await self.doc()
            self.assertEqual(doc["rows_left_out"], {"main.level_backups": 1})
            self.assertEqual(doc["tables"]["main.level_backups"]["rows"], [])
            preview, _ = await self.restore(doc)
        self.assertTrue(any("too large to go in this file" in text for text in preview["warnings"]), preview["warnings"])
        self.assertFalse(any("Someone may have edited it" in text for text in preview["warnings"]), preview["warnings"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM birthdays WHERE guild_id = ?", (self.guild.id,)), 1)


class RowLimitTest(Case):
    async def test_rows_past_a_limit_count_as_dropped_and_show_in_the_preview(self):
        doc = await self.doc()
        part = doc["tables"]["main.youtube_subs"]
        for number in range(MAX_SUBSCRIPTIONS + 5):
            row = [None] * len(part["columns"])
            values = dict(guild_id=str(self.guild.id), youtube_id=f"UC{number}", channel_id=str(self.channel.id), created_at=1.0)
            for name, column in zip(part["columns"], range(len(part["columns"]))):
                if name in values:
                    row[column] = values[name]
            part["rows"].append(row)
        columns = await self.db.all("PRAGMA table_info(youtube_subs)")
        for spec in columns:
            if spec["notnull"] and spec["name"] not in ("guild_id", "youtube_id", "channel_id"):
                at = part["columns"].index(spec["name"])
                for row in part["rows"]:
                    row[at] = 0 if "INT" in spec["type"].upper() or "REAL" in spec["type"].upper() else "x"
        preview, _ = await self.restore(doc)
        item = next(row for row in preview["tables"] if row["table"] == "youtube_subs")
        self.assertEqual(item["dropped"], 5)
        self.assertTrue(any("past what a server can hold" in text for text in preview["warnings"]), preview["warnings"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM youtube_subs WHERE guild_id = ?", (self.guild.id,)), MAX_SUBSCRIPTIONS)

    async def test_a_restore_has_a_limit_on_rows_in_all(self):
        doc = await self.doc()
        part = doc["tables"]["main.birthdays"]
        at = {name: index for index, name in enumerate(part["columns"])}
        for number in range(10):
            row = [None] * len(part["columns"])
            row[at["guild_id"]], row[at["user_id"]], row[at["month"]], row[at["day"]], row[at["updated_at"]] = str(self.guild.id), 1000 + number, 1, 1, 1.0
            part["rows"].append(row)
        with mock.patch.object(backup_module, "MAX_RESTORE_ROWS", 4):
            await self.restore(doc)
        self.assertLessEqual(await self.db.scalar("SELECT COUNT(*) FROM birthdays WHERE guild_id = ?", (self.guild.id,)), 4)


class DiskTest(Case):
    def nearly_full(self):
        return mock.patch.object(backup_module.shutil, "disk_usage", return_value=mock.Mock(free=backup_module.MIN_FREE_BYTES - 1))

    async def test_an_upload_is_refused_when_the_disk_is_nearly_full(self):
        client = await self.client()
        with self.nearly_full():
            response = await client.post(self.path("/backup/upload"), data=b"{}")
        self.assertEqual(response.status, 507)
        self.assertIn("disk is nearly full", (await response.json())["error"])

    async def test_a_snapshot_stops_when_the_disk_is_nearly_full(self):
        client = await self.client()
        with self.nearly_full():
            await client.post(self.path("/backup/snapshots"))
            job = await self.job_done(client)
        self.assertIn("disk is nearly full", job["error"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM backup_snapshots"), 0)

    async def test_one_upload_at_a_time_for_a_server(self):
        client = await self.client()
        self.backup._uploading.add(self.guild.id)
        response = await client.post(self.path("/backup/upload"), data=b"{}")
        self.assertEqual(response.status, 409)
        self.assertIn("already on its way", (await response.json())["error"])

    async def test_uploads_have_a_limit_across_servers(self):
        client = await self.client()
        self.backup._uploading.update({1, 2})
        response = await client.post(self.path("/backup/upload"), data=b"{}")
        self.assertEqual(response.status, 409)
        self.assertIn("other servers", (await response.json())["error"])

    async def test_jobs_take_turns_across_servers(self):
        release = asyncio.Event()
        running = []

        async def work(job):
            running.append(job)
            await release.wait()

        jobs = [self.backup._start_job(guild_id, "snapshot", work) for guild_id in (1, 2, 3)]
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(len(running), backup_module.MAX_JOBS_AT_ONCE)
        self.assertIn("another server", jobs[-1].stage)
        release.set()
        await asyncio.wait({job.task for job in jobs})
        self.assertEqual(len(running), 3)

    async def test_the_oldest_scheduled_snapshots_go_when_a_server_passes_its_budget(self):
        for number in range(3):
            await self.backup._write_snapshot(self.guild.id, kind="scheduled", actor_name=None, include_messages=False)
        await self.db.run("UPDATE backup_snapshots SET size = 100")
        with mock.patch.object(backup_module, "MAX_SNAPSHOT_BYTES", 150):
            await self.backup._prune(self.guild.id)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM backup_snapshots"), 1)

    async def test_snapshots_by_hand_past_the_budget_are_refused(self):
        await self.backup._write_snapshot(self.guild.id, kind="manual", actor_name=None, include_messages=False)
        await self.db.run("UPDATE backup_snapshots SET size = 100")
        client = await self.client()
        with mock.patch.object(backup_module, "MAX_SNAPSHOT_BYTES", 50):
            response = await client.post(self.path("/backup/snapshots"))
        self.assertEqual(response.status, 400)


class ExportTest(Case):
    async def test_no_database_read_stays_open_during_a_download(self):
        client = await self.client()
        reads_open = []
        original = self.backup._read_stored

        async def watching(path):
            reads_open.append(self.backup._is_busy(self.guild.id))
            async for block in original(path):
                yield block

        self.backup._read_stored = watching
        response = await client.get(self.path("/backup/export"))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(await response.read())["format"], backup_module.FORMAT)
        self.assertEqual(reads_open, [False])

    async def test_the_page_starts_a_job_and_then_downloads(self):
        client = await self.client()
        response = await client.post(self.path("/backup/exports"), json={"messages": False})
        self.assertEqual(response.status, 200)
        job = await self.job_done(client)
        self.assertEqual(job["kind"], "export")
        token = job["result"]["token"]
        plain = await client.get(self.path(f"/backup/exports/{token}"))
        self.assertEqual(json.loads(await plain.read())["includes_messages"], False)
        packed = await client.get(self.path(f"/backup/exports/{token}?gzip=1"))
        self.assertEqual(packed.headers["Content-Type"], "application/gzip")
        await packed.read()
        for _ in range(100):
            if self.backup._downloads == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.backup._downloads, 0)

    async def test_a_wrong_token_finds_nothing(self):
        client = await self.client()
        await client.post(self.path("/backup/exports"), json={})
        await self.job_done(client)
        self.assertEqual((await client.get(self.path("/backup/exports/nope"))).status, 404)

    async def test_an_export_is_a_job_so_only_one_runs_for_a_server(self):
        release = asyncio.Event()

        async def work(job):
            await release.wait()

        self.backup._start_job(self.guild.id, "snapshot", work)
        client = await self.client()
        self.assertEqual((await client.post(self.path("/backup/exports"), json={})).status, 409)
        self.assertEqual((await client.get(self.path("/backup/export"))).status, 409)
        release.set()

    async def test_downloads_have_a_limit(self):
        self.backup._downloads = backup_module.MAX_DOWNLOADS_AT_ONCE
        client = await self.client()
        self.assertEqual((await client.get(self.path("/backup/export"))).status, 409)
        self.backup._downloads = 0

    async def test_a_new_export_replaces_the_old_file_and_a_restart_clears_them(self):
        client = await self.client()
        await client.post(self.path("/backup/exports"), json={})
        await self.job_done(client)
        first = self.backup._exports[self.guild.id][1]
        await client.post(self.path("/backup/exports"), json={})
        await self.job_done(client)
        self.assertFalse(first.exists())
        second = self.backup._exports[self.guild.id][1]
        await self.backup._tidy_files()
        self.assertFalse(second.exists())


class StopPreviewTest(Case):
    async def test_a_running_preview_can_be_stopped_and_its_file_goes(self):
        client = await self.client()
        release = asyncio.Event()
        original = self.backup._build_preview

        async def stuck(guild_id, staged, job):
            await release.wait()
            return await original(guild_id, staged, job)

        self.backup._build_preview = stuck
        response = await client.post(self.path("/backup/upload"), data=b"{}")
        self.assertEqual(response.status, 200)
        path = self.backup._staged[self.guild.id].path
        self.assertEqual((await client.delete(self.path("/backup/staged"))).status, 200)
        job = (await (await client.get(self.path("/backup/job"))).json())["job"]
        self.assertFalse(job["running"])
        self.assertEqual(job["error"], "Stopped.")
        self.assertNotIn(self.guild.id, self.backup._staged)
        self.assertFalse(path.exists())

    async def test_a_restore_cant_be_stopped_that_way(self):
        release = asyncio.Event()

        async def work(job):
            await release.wait()

        self.backup._start_job(self.guild.id, "restore", work)
        client = await self.client()
        self.assertEqual((await client.delete(self.path("/backup/staged"))).status, 409)
        release.set()


class JsonCheckTest(Case):
    def reaches(self, name, value):
        return self.backup._row_reaches_elsewhere(self.guild.id, [name], [value], read_json=True)

    def neighbor_channel(self):
        from tests.fakes import FakeChannel, FakeGuild, FakeMember

        neighbor = FakeGuild("Neighbor")
        self.bot.fake_guilds.append(neighbor)
        neighbor.me = FakeMember(neighbor, "Bot", bot=True)
        return neighbor, FakeChannel(neighbor, "theirs")

    def test_a_leading_space_doesnt_step_around_it(self):
        _, theirs = self.neighbor_channel()
        self.assertTrue(self.reaches("payload", f' {{"channel_id": {theirs.id}}}'))

    def test_an_escape_in_a_key_doesnt_step_around_it(self):
        _, theirs = self.neighbor_channel()
        self.assertTrue(self.reaches("payload", '{"\\u0063hannel_id": %d}' % theirs.id))

    def test_a_bare_list_of_ids_is_read(self):
        _, theirs = self.neighbor_channel()
        self.assertTrue(self.reaches("channel_wl", f"[{theirs.id}]"))
        self.assertFalse(self.reaches("channel_wl", f"[{self.channel.id}]"))

    def test_nesting_too_deep_for_the_parser_counts_as_foreign(self):
        self.assertTrue(self.reaches("payload", "[" * 200_000))

    def test_ordinary_text_passes(self):
        self.assertFalse(self.reaches("content", "{user} said [this]"))


class SettingsCheckTest(Case):
    async def test_a_setting_cant_name_another_servers_channel_or_role_under_any_field_name(self):
        from tests.fakes import FakeChannel, FakeGuild, FakeMember

        neighbor = FakeGuild("Neighbor")
        self.bot.fake_guilds.append(neighbor)
        neighbor.me = FakeMember(neighbor, "Bot", bot=True)
        role = neighbor.role("Theirs", 2)
        FakeChannel(neighbor, "theirs")
        warnings = []
        schema = self.bot.settings.schemas["greetings"]
        current = self.bot.settings.get(self.guild.id, "greetings")
        out = self.backup._clean_fields(self.guild.id, schema, {"birthday_role": str(role.id)}, current, "greetings", warnings)
        self.assertEqual(out["birthday_role"], current["birthday_role"])
        self.assertIn("another server", warnings[0])
