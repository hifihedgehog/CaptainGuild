"""One bad row never stops the bot from starting, for any server, in any module."""
import asyncio
import json
import logging
import time

from captainguild.modules import backup as backup_module
from captainguild.modules import load_all
from captainguild.modules.backup import Backup, decode_cell
from captainguild.modules.proposals import TIMER, Proposals
from captainguild.modules.utilities import Utilities
from captainguild.modules.voice import Voice
from tests.fakes import BotTest, TestBot

OTHER = 555000000000000002
STATES = {"state": "open", "status": "active"}


class CellTest(BotTest):
    def test_a_real_column_takes_only_a_finite_number(self):
        for value in ("not a number", "", "12.5", {"$b64": "AA=="}, float("inf"), float("-inf"), float("nan"), {"$float": "inf"}, {"$float": "nan"}):
            with self.assertRaises(backup_module.BadCell, msg=repr(value)):
                decode_cell(value, "real")
        self.assertEqual(decode_cell(None, "real"), None)
        self.assertEqual(decode_cell(1.5, "real"), 1.5)
        self.assertEqual(decode_cell(3, "real"), 3)
        self.assertEqual(decode_cell({"$float": "1.5"}, "real"), 1.5)

    def test_only_a_blob_column_takes_bytes(self):
        for kind in ("text", "integer", "numeric", "real"):
            with self.assertRaises(backup_module.BadCell, msg=kind):
                decode_cell({"$b64": "AA=="}, kind)
        self.assertEqual(decode_cell({"$b64": "AA=="}, "blob"), b"\x00")

    def test_no_kind_of_column_takes_infinity(self):
        for kind in ("text", "blob", "real"):
            with self.assertRaises(backup_module.BadCell, msg=kind):
                decode_cell(float("inf"), kind)


class StartTest(BotTest):
    modules = load_all()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        asyncio.get_running_loop().set_debug(False)
        self.bots = []

    async def asyncTearDown(self):
        for bot in self.bots:
            voice = bot.module("voice")
            if voice is not None and voice._flush_loop.is_running():
                voice._flush_loop.cancel()
        await super().asyncTearDown()

    async def restart(self):
        bot = TestBot(self.db, self.message_db)
        self.bots.append(bot)
        await bot.prepare(load_all())
        return bot

    async def proposal(self, guild_id, number, ends_at):
        await self.db.run(
            "INSERT INTO proposals (guild_id, number, pings, title, content, submitter_id, submitter_name, started_at, vote_hours, ends_at, community_up, community_down, community_done, community_pass, mod_up, mod_down, mod_done, mod_pass, state, rendered, imported) "
            "VALUES (?, ?, '', 't', 'c', 1, 'n', 1.0, 1.0, ?, 0, 0, 0, 0, 0, 0, 0, 0, 'open', 1, 0)",
            (guild_id, number, ends_at),
        )

    async def plant_everywhere(self, real, text):
        """A row in every table, holding what a column should never hold, as a database copied from before these checks could."""
        catalog = await self.cog(Backup).catalogs(("main", self.db))
        for table in catalog.tables.values():
            if table.name in ("timers", "activity"):
                continue
            row = []
            for name in table.columns:
                kind = table.affinity[name]
                if name == "guild_id":
                    row.append(self.guild.id)
                elif name in STATES and kind == "text":
                    row.append(STATES[name])
                elif kind == "integer":
                    row.append(7 if name in table.primary_key else 0)
                elif kind == "real":
                    row.append(real)
                else:
                    row.append(text)
            names = ", ".join(f'"{name}"' for name in table.columns)
            marks = ", ".join("?" for _ in table.columns)
            await self.db.run(f'INSERT OR IGNORE INTO "{table.name}" ({names}) VALUES ({marks})', row)

    async def wipe_planted(self):
        catalog = await self.cog(Backup).catalogs(("main", self.db))
        for table in catalog.tables.values():
            if table.name in ("timers", "activity"):
                continue
            await self.db.run(f'DELETE FROM "{table.name}" WHERE {table.where}', (self.guild.id,))

    async def test_a_bad_row_in_every_table_at_once_leaves_a_working_bot(self):
        await self.db.conn.execute("PRAGMA foreign_keys=OFF")
        for real, text in (("x", "t"), (1.0, b"\x00\x01"), (float("inf"), "t")):
            await self.plant_everywhere(real, text)
            bot = await self.restart()
            self.assertEqual(len(bot.modules), len(self.bot.modules))
            for module in bot.modules:
                hook = getattr(module, "on_ready", None)
                if hook is not None:
                    await asyncio.wait_for(hook(), 10)
            self.assertTrue(bot.module("voice")._flush_loop.is_running())
            await self.wipe_planted()

    async def test_a_module_that_cant_finish_loading_doesnt_stop_the_others(self):
        original = Proposals.on_load

        async def broken(module):
            raise RuntimeError("a bug")

        Proposals.on_load = broken
        try:
            with self.assertLogs("captainguild.bot", logging.ERROR) as logged:
                bot = await self.restart()
        finally:
            Proposals.on_load = original
        self.assertIn("proposals", logged.output[0])
        self.assertIsNotNone(bot.module("voice"))
        self.assertIn("embeds.scheduled", bot.timers.handlers)

    async def test_a_bad_proposal_costs_only_itself(self):
        await self.proposal(self.guild.id, 1, "not a number")
        await self.proposal(OTHER, 2, time.time() + 3600)
        bot = await self.restart()
        self.assertIsNotNone(await bot.timers.get_key(TIMER, f"{OTHER}:2"))

    async def test_a_bad_highlight_word_costs_only_itself(self):
        await self.db.run("INSERT INTO highlight_words (guild_id, user_id, word, created_at) VALUES (?, 1, ?, 1.0)", (self.guild.id, b"\x00bytes"))
        await self.db.run("INSERT INTO highlight_words (guild_id, user_id, word, created_at) VALUES (?, 2, 'printer', 1.0)", (OTHER,))
        bot = await self.restart()
        self.assertIn(2, bot.module("utilities")._words[OTHER])

    async def test_a_failed_highlight_reload_leaves_what_it_had(self):
        utilities = self.cog(Utilities)
        await self.db.run("INSERT INTO highlight_words (guild_id, user_id, word, created_at) VALUES (?, 2, 'printer', 1.0)", (OTHER,))
        await utilities._load_highlights()

        def broken(guild_id):
            raise RuntimeError("a bug")

        utilities._reindex, original = broken, utilities._reindex
        with self.assertRaises(RuntimeError):
            await utilities._load_highlights()
        utilities._reindex = original
        self.assertIn(2, utilities._words[OTHER])

    async def test_a_bad_voice_row_doesnt_stop_counts_or_other_servers_timers(self):
        await self.db.run("INSERT INTO mini_mutes (guild_id, user_id, expires_at) VALUES (?, 1, 'not a number')", (self.guild.id,))
        await self.db.run("INSERT INTO mini_mutes (guild_id, user_id, expires_at) VALUES (?, 2, ?)", (OTHER, time.time() + 600))
        await self.db.run("INSERT INTO voice_actions (guild_id, user_id, action, expires_at) VALUES (?, 1, 'mute', 'not a number')", (self.guild.id,))
        voice = self.cog(Voice)
        await voice.on_ready()
        self.addCleanup(voice._flush_loop.cancel)
        self.assertTrue(voice._flush_loop.is_running())
        self.assertIsNotNone(await self.bot.timers.get_key("mini_mute", f"{OTHER}:2"))

    async def test_a_bad_bulk_job_doesnt_stop_other_servers_jobs(self):
        roles = self.bot.module("roles")
        columns = [row["name"] for row in await self.db.all("PRAGMA table_info(role_bulk_jobs)")]
        for guild_id, saved_at in ((OTHER, "not a number"), (self.guild.id, "not a number")):
            values = dict.fromkeys(columns)
            values.update(guild_id=guild_id, action="add", scope="all", role_id=1, user_id=1, channel_id=1, message_id=1, done=0, failed=0, total=0, started_at=time.time(), saved_at=saved_at)
            await self.db.run(f"INSERT INTO role_bulk_jobs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", [values[name] for name in columns])
        # Rows come back in guild order, so the first bad one is the other server's.
        with self.assertLogs("captainguild.modules.roles", logging.ERROR):
            await roles.resume_jobs()

    async def test_a_restore_with_a_bad_row_drops_the_row_and_the_bot_still_starts(self):
        backup = self.cog(Backup)
        out = bytearray()
        async for block in backup.export_stream(self.guild.id, include_messages=False):
            out += block
        doc = json.loads(bytes(out))
        part = doc["tables"]["main.proposals"]
        row = [None] * len(part["columns"])
        values = dict(guild_id=str(self.guild.id), number=1, pings="", title="t", content="c", submitter_id=1, submitter_name="n", started_at=1.0, vote_hours=1.0, ends_at="not a number", community_up=0, community_down=0, community_done=0, community_pass=0, mod_up=0, mod_down=0, mod_done=0, mod_pass=0, state="open", rendered=1, imported=0)
        for name, value in values.items():
            row[part["columns"].index(name)] = value
        part["rows"].append(row)
        client = await self.client()
        self.assertEqual((await client.post(self.path("/backup/upload"), data=json.dumps(doc).encode())).status, 200)
        job = await backup.wait_for_job(self.guild.id)
        self.assertIsNone(job.error)
        result = next(item for item in job.result["tables"] if item["table"] == "proposals")
        self.assertEqual(result["dropped"], 1)
        await self.restart()
