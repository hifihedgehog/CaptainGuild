"""Forum messages and backups: a restore of an older backup keeps the posts welcomed and reminded since, so none gets either twice."""
from __future__ import annotations

from captainguild.modules.backup import Backup
from captainguild.modules.forummessages import ForumMessages
from tests.test_backup import BackupCase

FORUM = 1400000000000000001
EARLIER, LATER = 1400000000000000010, 1400000000000000020


class RestoreTest(BackupCase):
    modules = [Backup, ForumMessages]

    async def rows(self, table: str) -> list[int]:
        return [row["thread_id"] for row in await self.db.all(f"SELECT thread_id FROM {table} WHERE guild_id = ? ORDER BY thread_id", (self.guild.id,))]

    async def test_an_older_backup_keeps_the_posts_welcomed_and_reminded_since(self):
        """Review, unproven 1: a restore that replaced these tables would forget every post welcomed or reminded after the backup was taken,
        and the next hourly look would remind a quiet one again."""
        for thread in (EARLIER,):
            await self.db.run("INSERT INTO forum_welcomes (guild_id, thread_id, channel_id, message_id, posted_at) VALUES (?, ?, ?, ?, 0)", (self.guild.id, thread, FORUM, thread + 1))
            await self.db.run("INSERT INTO forum_reminders (guild_id, thread_id, channel_id, reminded_at) VALUES (?, ?, ?, 0)", (self.guild.id, thread, FORUM))
        older = await self.export_bytes()
        await self.db.run("INSERT INTO forum_welcomes (guild_id, thread_id, channel_id, message_id, posted_at) VALUES (?, ?, ?, ?, 1)", (self.guild.id, LATER, FORUM, LATER + 1))
        await self.db.run("INSERT INTO forum_reminders (guild_id, thread_id, channel_id, reminded_at) VALUES (?, ?, ?, 1)", (self.guild.id, LATER, FORUM))
        client = await self.client()
        job = await self.preview_of(client, older)
        await self.restore(client, job["result"])
        self.assertEqual(await self.rows("forum_welcomes"), [EARLIER, LATER])
        self.assertEqual(await self.rows("forum_reminders"), [EARLIER, LATER])
