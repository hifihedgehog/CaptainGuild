"""A file an older version wrote still comes back whole. It holds its tables in the order that version knew, and none of the proof added since."""
import json
import time

from captainguild.modules.backup import GLOBAL_TABLES
from captainguild.modules.moderation import Moderation
from tests.test_backup_servers import Case

# The order the first version wrote these in: by name, since it knew of no link between them.
CHILDREN_FIRST = ("main.adopt_items", "main.adopt_jobs", "main.mini_mute_ballots", "main.mini_mute_votes", "main.tag_aliases", "main.tag_shares", "main.tags")


def children_first(doc: dict) -> dict:
    """The same file, in the order an older version wrote it."""
    tables = doc["tables"]
    ordered = {key: tables[key] for key in CHILDREN_FIRST}
    ordered.update({key: value for key, value in tables.items() if key not in ordered})
    return {**doc, "tables": ordered}


def without_marks(doc: dict) -> dict:
    """The same file with no proof column on reminders, as an older version wrote them."""
    part = doc["tables"]["main.reminders"]
    at = part["columns"].index("$mark")
    return {**doc, "tables": {**doc["tables"], "main.reminders": {"columns": part["columns"][:at] + part["columns"][at + 1:], "rows": [row[:at] + row[at + 1:] for row in part["rows"]]}}}


class OlderOrderTest(Case):
    async def seed(self):
        now = time.time()
        tag = await self.db.insert("INSERT INTO tags (guild_id, name, content, created_at, updated_at) VALUES (?, 'seen', 'text', ?, ?)", (self.guild.id, now, now))
        await self.db.run("INSERT INTO tag_aliases (guild_id, name, tag_id, created_at) VALUES (?, 'also', ?, ?)", (self.guild.id, tag, now))
        await self.db.run("INSERT INTO tag_shares (guild_id, tag_id, code, created_at) VALUES (?, ?, 'older1', ?)", (self.guild.id, tag, now))
        job = await self.db.insert("INSERT INTO adopt_jobs (guild_id, delete_originals, copy_reactions, state, created_at) VALUES (?, 0, 0, 'done', ?)", (self.guild.id, now))
        await self.db.run(
            "INSERT INTO adopt_items (guild_id, job_id, position, source_channel_id, source_message_id, author_name, summary, state) VALUES (?, ?, 0, 1, 1, 'a', 's', 'done')", (self.guild.id, job),
        )
        vote = 900000000000000077
        await self.db.run(
            "INSERT INTO mini_mute_votes (vote_message_id, guild_id, channel_id, target_id, initiator_id, minutes, required, expires_at) VALUES (?, ?, 1, 2, 3, 5, 3, ?)", (vote, self.guild.id, now + 600),
        )
        await self.db.run("INSERT INTO mini_mute_ballots (vote_message_id, guild_id, voter_id, approve) VALUES (?, ?, 4, 1)", (vote, self.guild.id))
        return tag

    async def test_children_written_before_their_parents_come_back(self):
        await self.seed()
        preview, done = await self.restore(children_first(await self.doc()))
        for table in ("tag_aliases", "tag_shares", "adopt_items", "mini_mute_ballots"):
            self.assertEqual(await self.db.scalar(f"SELECT COUNT(*) FROM {table} WHERE guild_id = ?", (self.guild.id,)), 1, table)
            self.assertEqual(done["tables"][f"main.{table}"]["dropped"], 0, table)
            item = next(row for row in preview["tables"] if row["table"] == table)
            self.assertEqual(item["foreign"], 0, table)

    async def test_a_child_ahead_of_its_parent_still_cant_join_another_servers_row(self):
        await self.seed()
        now = time.time()
        theirs = await self.db.insert("INSERT INTO tags (guild_id, name, content, created_at, updated_at) VALUES (?, 'theirs', 'text', ?, ?)", (self.neighbor.id, now, now))
        doc = await self.doc()
        self.add_row(doc, "main.tag_aliases", guild_id=str(self.guild.id), name="stolen", tag_id=theirs, created_at=now)
        _, done = await self.restore(children_first(doc))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM tag_aliases WHERE name = 'stolen'"), 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM tag_aliases WHERE name = 'also'"), 1)
        self.assertEqual(done["tables"]["main.tag_aliases"]["dropped"], 1)

    async def test_a_child_with_no_server_of_its_own_stays_checked_in_any_order(self):
        message = 900000000000000088
        await self.db.run("INSERT INTO rr_messages (message_id, guild_id, channel_id, created_at) VALUES (?, ?, ?, ?)", (message, self.neighbor.id, self.their_channel.id, time.time()))
        doc = await self.doc()
        doc["tables"]["main.rr_seen"]["rows"].append([str(message), "A", 5])
        tables = doc["tables"]
        doc["tables"] = {"main.rr_seen": tables["main.rr_seen"], **{key: value for key, value in tables.items() if key != "main.rr_seen"}}
        await self.restore(doc)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ?", (message,)), 0)


class OlderRemindersTest(Case):
    async def reminder(self, about: str) -> int:
        now = time.time()
        return await self.db.insert("INSERT INTO reminders (guild_id, channel_id, user_id, about, created_at, due_at) VALUES (?, 1, 2, ?, ?, ?)", (self.guild.id, about, now, now + 600))

    async def test_a_file_with_no_proof_on_reminders_leaves_todays_alone(self):
        await self.reminder("in the file")
        doc = without_marks(await self.doc())
        await self.db.run("DELETE FROM reminders")
        await self.reminder("made since")
        preview, _ = await self.restore(doc)
        rows = await self.db.all("SELECT about FROM reminders WHERE guild_id = ?", (self.guild.id,))
        self.assertEqual([row["about"] for row in rows], ["made since"])
        self.assertTrue(any("main.reminders" in line and "stays as it is" in line for line in preview["warnings"]), preview["warnings"])
        self.assertFalse(any("weren't made by this install" in line for line in preview["warnings"]), preview["warnings"])

    async def test_a_file_with_proof_still_replaces_them(self):
        await self.reminder("in the file")
        doc = await self.doc()
        await self.db.run("DELETE FROM reminders")
        await self.reminder("made since")
        await self.restore(doc)
        rows = await self.db.all("SELECT about FROM reminders WHERE guild_id = ?", (self.guild.id,))
        self.assertEqual([row["about"] for row in rows], ["in the file"])


class OlderTrustTest(Case):
    async def test_the_preview_says_when_a_tag_comes_back_without_its_trust(self):
        now = time.time()
        await self.db.run("INSERT INTO tags (guild_id, name, content, trusted, created_at, updated_at) VALUES (?, 'runs', 'text', 1, ?, ?)", (self.guild.id, now, now))
        await self.db.run("INSERT INTO tags (guild_id, name, content, trusted, created_at, updated_at) VALUES (?, 'plain', 'text', 0, ?, ?)", (self.guild.id, now, now))
        doc = await self.doc()
        part = doc["tables"]["main.tags"]
        at = part["columns"].index("$mark")
        doc["tables"]["main.tags"] = {"columns": part["columns"][:at] + part["columns"][at + 1:], "rows": [row[:at] + row[at + 1:] for row in part["rows"]]}
        preview, _ = await self.restore(doc)
        self.assertEqual(await self.db.scalar("SELECT trusted FROM tags WHERE guild_id = ? AND name = 'runs'", (self.guild.id,)), 0)
        self.assertIn("1 tags rows in this file come back without trust, because nothing proves this install wrote them. Trust them again on their page after reading them.", preview["warnings"])

    async def test_a_file_with_its_proof_says_nothing_about_trust(self):
        now = time.time()
        await self.db.run("INSERT INTO tags (guild_id, name, content, trusted, created_at, updated_at) VALUES (?, 'runs', 'text', 1, ?, ?)", (self.guild.id, now, now))
        preview, _ = await self.restore(await self.doc())
        self.assertFalse(any("without trust" in line for line in preview["warnings"]), preview["warnings"])


class RaidStateTest(Case):
    async def test_a_file_made_during_a_raid_doesnt_turn_raid_mode_back_on(self):
        moderation = self.cog(Moderation)
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": self.role_muted.id})
        await moderation.raid_on(self.guild, self.admin, None, "from the file")
        doc = await self.doc()
        await moderation.raid_off(self.guild, self.admin)
        await self.restore(doc)
        self.assertIsNone(await self.db.one("SELECT 1 FROM raid_modes WHERE guild_id = ?", (self.guild.id,)))

    async def test_what_is_held_now_stays_through_a_restore(self):
        doc = await self.doc()
        await self.db.run("INSERT INTO raid_holds (guild_id, user_id, user_name, role_id, held_at) VALUES (?, 5, 'n', 6, ?)", (self.guild.id, time.time()))
        await self.restore(doc)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM raid_holds WHERE guild_id = ?", (self.guild.id,)), 1)

    def test_raid_state_is_how_things_stand_now(self):
        self.assertLessEqual({"raid_modes", "raid_holds"}, GLOBAL_TABLES)

    async def test_a_timer_from_a_file_cant_end_a_raid_it_didnt_start(self):
        moderation = self.cog(Moderation)
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": self.role_muted.id})
        await moderation.raid_on(self.guild, self.admin, None, "running now, with no end")
        await moderation._raid_due({"guild_id": self.guild.id}, {"guild_id": self.guild.id, "kind": "moderation.raid_end", "key": str(self.guild.id)})
        self.assertIsNotNone(await self.db.one("SELECT 1 FROM raid_modes WHERE guild_id = ?", (self.guild.id,)))

    async def test_a_timer_from_a_file_says_nothing_when_no_raid_is_on(self):
        moderation = self.cog(Moderation)
        sent = []

        async def tell(guild, title, text):
            sent.append(title)

        moderation._tell_staff = tell
        await moderation._raid_due({"guild_id": self.guild.id}, {"guild_id": self.guild.id, "kind": "moderation.raid_end", "key": str(self.guild.id)})
        self.assertEqual(sent, [])

    async def test_the_raid_still_ends_when_its_own_time_runs_out(self):
        moderation = self.cog(Moderation)
        await self.bot.settings.save(self.guild.id, "moderation", {"mute_role": self.role_muted.id})
        await moderation.raid_on(self.guild, self.admin, 60, "short")
        await self.db.run("UPDATE raid_modes SET expires_at = ? WHERE guild_id = ?", (time.time() - 1, self.guild.id))
        await moderation._raid_due({"guild_id": self.guild.id}, {"guild_id": self.guild.id, "kind": "moderation.raid_end", "key": str(self.guild.id)})
        self.assertIsNone(await self.db.one("SELECT 1 FROM raid_modes WHERE guild_id = ?", (self.guild.id,)))
