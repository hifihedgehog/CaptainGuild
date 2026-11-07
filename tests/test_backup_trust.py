"""Trust in a backup file. Someone restoring a file can be shown a row that says a tag or trigger is trusted, and only this install can prove that it wrote that."""
from __future__ import annotations

import time

from tests.test_backup_timers import Case


class TrustInAFileTest(Case):
    async def tag(self, content="{cmd:ping}", trusted=1):
        now = time.time()
        return await self.db.insert(
            "INSERT INTO tags (guild_id, name, content, owner_id, trusted, created_at, updated_at) VALUES (?, 'mine', ?, ?, ?, ?, ?)", (self.guild.id, content, self.member.id, trusted, now, now),
        )

    async def trigger(self, trusted=1):
        now = time.time()
        return await self.db.insert(
            "INSERT INTO triggers (guild_id, match_type, phrase, content, created_by, trusted, created_at, updated_at) VALUES (?, 1, 'hello', '{cmd:ping}', ?, ?, ?, ?)", (self.guild.id, self.member.id, trusted, now, now),
        )

    async def test_a_real_file_keeps_its_trust(self):
        tag, trigger = await self.tag(), await self.trigger()
        doc = await self.doc()
        await self.db.run("UPDATE tags SET trusted = 0 WHERE id = ?", (tag,))
        await self.db.run("UPDATE triggers SET trusted = 0 WHERE id = ?", (trigger,))
        await self.restore(doc)
        self.assertEqual(await self.db.scalar("SELECT trusted FROM tags WHERE name = 'mine'"), 1)
        self.assertEqual(await self.db.scalar("SELECT trusted FROM triggers WHERE phrase = 'hello'"), 1)

    async def test_words_changed_in_the_file_lose_it_and_the_row_stays(self):
        await self.tag(), await self.trigger()
        doc = await self.doc()
        for key, column, value in (("main.tags", "content", "{cmdAdmin:ban 1 x}"), ("main.triggers", "content", "{cmdAdmin:ban 1 x}")):
            part = doc["tables"][key]
            part["rows"][0][part["columns"].index(column)] = value
        await self.restore(doc)
        self.assertEqual(await self.db.scalar("SELECT trusted FROM tags WHERE name = 'mine'"), 0)
        self.assertEqual(await self.db.scalar("SELECT content FROM tags WHERE name = 'mine'"), "{cmdAdmin:ban 1 x}")
        self.assertEqual(await self.db.scalar("SELECT trusted FROM triggers WHERE phrase = 'hello'"), 0)

    async def test_a_flag_turned_on_in_the_file_does_not_count(self):
        await self.tag(trusted=0)
        doc = await self.doc()
        part = doc["tables"]["main.tags"]
        part["rows"][0][part["columns"].index("trusted")] = 1
        await self.restore(doc)
        self.assertEqual(await self.db.scalar("SELECT trusted FROM tags WHERE name = 'mine'"), 0)

    async def test_a_file_with_no_marks_at_all_loses_trust_and_keeps_rows(self):
        await self.tag()
        doc = await self.doc()
        part = doc["tables"]["main.tags"]
        at = part["columns"].index("$mark")
        part["columns"].pop(at)
        for row in part["rows"]:
            row.pop(at)
        await self.restore(doc)
        self.assertEqual(await self.db.scalar("SELECT trusted FROM tags WHERE name = 'mine'"), 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM tags WHERE name = 'mine'"), 1)
