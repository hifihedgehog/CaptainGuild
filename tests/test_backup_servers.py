"""A backup file can't reach into another server through a row that hangs off a parent row, and every table of that shape in the program is covered."""
import asyncio
import json
import re
import time

from captainguild.core.module import Module
from captainguild.modules import load_all
from captainguild.modules.backup import Backup, child_links, read_catalog, table_owners
from captainguild.modules.embeds import Embeds
from captainguild.modules.reactionroles import ReactionRoles
from captainguild.modules.voice import Voice
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember

# A column ending in _id with one of these stems holds a Discord ID, or some other outside ID, and not the number of a row in another table.
OUTSIDE_IDS = re.compile(r"(?:^|_)(?:guild|channel|user|role|message|author|moderator|owner|target|voter|reporter|submitter|host|initiator|thread|parent|actor)$")
# Columns that look like a pointer to a row and aren't one, each with the reason.
NOT_POINTERS = {
    ("rr_messages", "group_id"): "A group number with no table of its own.",
    ("reports", "thread_id"): "The review thread in Discord. Readers go through the thread's own row.",
    ("review_threads", "thread_id"): "A Discord thread. A thread in another server is refused by the channel check.",
    ("toproll_scores", "message_id"): "The Discord message a score came from.",
    ("giveaways", "channel_id"): "A Discord channel, checked against the server on the way in.",
    ("reminders", "channel_id"): "A Discord channel, checked against the server on the way in.",
    ("twitch_subs", "stream_id"): "A Twitch login.",
    ("twitch_subs", "live_id"): "A Twitch stream number.",
    ("youtube_subs", "youtube_id"): "A YouTube channel.",
    ("moved_messages", "webhook_id"): "The Discord webhook that posted the copy.",
}


def unchecked_pointers(catalog) -> list[str]:
    """Columns of tables that carry a guild_id which look like they name another table's row, with nothing checking them on a restore."""
    keys_of = {}
    for table in catalog.tables.values():
        # A key that holds guild_id is scoped already, and "id" says nothing about what a column points at.
        if len(table.primary_key) == 1 and table.primary_key[0] not in ("id", "guild_id"):
            keys_of.setdefault((table.database, table.owner), {})[table.primary_key[0]] = table.name
    found = []
    for table in catalog.tables.values():
        if "guild_id" not in table.columns or table.key == "main.timers":
            continue
        linked = {link.column for link in table.links}
        for column in table.columns:
            if column in linked or (table.name, column) in NOT_POINTERS:
                continue
            named_key = keys_of.get((table.database, table.owner), {}).get(column)
            shares_a_key = named_key not in (None, table.name)
            odd_stem = column.endswith("_id") and not OUTSIDE_IDS.search(column[:-3])
            if shares_a_key or odd_stem:
                found.append(f"{table.name}.{column}")
    return sorted(found)


class Case(BotTest):
    modules = load_all()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        asyncio.get_running_loop().set_debug(False)
        self.backup = self.cog(Backup)
        self.neighbor = FakeGuild("Neighbor")
        self.bot.fake_guilds.append(self.neighbor)
        self.neighbor.me = FakeMember(self.neighbor, "Bot", bot=True)
        self.their_channel = FakeChannel(self.neighbor, "their-channel")

    async def doc(self):
        out = bytearray()
        async for block in self.backup.export_stream(self.guild.id, include_messages=False):
            out += block
        return json.loads(bytes(out))

    def add_row(self, doc, table, **values):
        part = doc["tables"][table]
        row = [None] * len(part["columns"])
        for name, value in values.items():
            row[part["columns"].index(name)] = value
        part["rows"].append(row)
        return row

    async def job_done(self, client):
        return await self.wait_for_job(client)

    async def restore(self, doc):
        client = await self.client()
        response = await client.post(self.path("/backup/upload"), data=json.dumps(doc).encode())
        self.assertEqual(response.status, 200, await response.text())
        preview = await self.job_done(client)
        self.assertIsNone(preview["error"])
        result = preview["result"]
        items = [row["key"] for row in result["settings"]] + [row["key"] for row in result["tables"]]
        response = await client.post(self.path("/backup/restore"), json={"token": result["token"], "items": items})
        self.assertEqual(response.status, 200, await response.text())
        done = await self.job_done(client)
        self.assertIsNone(done["error"])
        return result, done["result"]


class PlantedChildTest(Case):
    async def test_a_reaction_role_row_cant_join_another_servers_message(self):
        message = 900000000000000001
        await self.db.run("INSERT INTO rr_messages (message_id, guild_id, channel_id, created_at) VALUES (?, ?, ?, ?)", (message, self.neighbor.id, self.their_channel.id, time.time()))
        await self.db.run("INSERT INTO rr_pairs (guild_id, message_id, slot, emoji, emoji_key, role_id) VALUES (?, ?, 0, 'A', 'A', 111)", (self.neighbor.id, message))
        doc = await self.doc()
        self.add_row(doc, "main.rr_pairs", guild_id=str(self.guild.id), message_id=str(message), slot=0, emoji="X", emoji_key="X", role_id=222, max_holders=0, label="", description="", style="secondary")
        preview, _ = await self.restore(doc)
        pairs = next(row for row in preview["tables"] if row["table"] == "rr_pairs")
        self.assertEqual(pairs["foreign"], 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_pairs WHERE message_id = ?", (message,)), 1)
        rr = await self.cog(ReactionRoles).get(message)
        self.assertEqual([option.role_ids for option in rr.options], [[111]])

    async def test_a_reader_ignores_a_child_from_another_server(self):
        message = 900000000000000001
        await self.db.run("INSERT INTO rr_messages (message_id, guild_id, channel_id, created_at) VALUES (?, ?, ?, ?)", (message, self.neighbor.id, self.their_channel.id, time.time()))
        await self.db.run("INSERT INTO rr_pairs (guild_id, message_id, slot, emoji, emoji_key, role_id) VALUES (?, ?, 0, 'A', 'A', 111)", (self.neighbor.id, message))
        await self.db.run("INSERT INTO rr_pairs (guild_id, message_id, slot, emoji, emoji_key, role_id) VALUES (?, ?, 0, 'X', 'X', 222)", (self.guild.id, message))
        rr = await self.cog(ReactionRoles).get(message)
        self.assertEqual([option.role_ids for option in rr.options], [[111]])

    async def test_a_version_cant_join_another_servers_post(self):
        posted = await self.db.insert("INSERT INTO posted_messages (guild_id, channel_id, message_id, summary, search, source, posted_at) VALUES (?, ?, 77, 's', 's', 'dashboard', ?)", (self.neighbor.id, self.their_channel.id, time.time()))
        await self.db.run("INSERT INTO posted_versions (guild_id, posted_id, number, payload, note, saved_at) VALUES (?, ?, 1, ?, 'x', 1.0)", (self.neighbor.id, posted, json.dumps({"content": "theirs", "embeds": [], "buttons": []})))
        doc = await self.doc()
        self.add_row(doc, "main.posted_versions", guild_id=str(self.guild.id), posted_id=posted, number=5000, payload=json.dumps({"content": "planted", "embeds": [], "buttons": []}), note="x", saved_at=1.0)
        await self.restore(doc)
        embeds = self.cog(Embeds)
        self.assertEqual(await embeds._latest_version(self.neighbor.id, posted), 1)
        self.assertEqual((await embeds._payload_of_version(self.neighbor.id, posted, 1))["content"], "theirs")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM posted_versions WHERE posted_id = ?", (posted,)), 1)

    async def test_an_embeds_reader_ignores_a_version_from_another_server(self):
        posted = await self.db.insert("INSERT INTO posted_messages (guild_id, channel_id, message_id, summary, search, source, posted_at) VALUES (?, ?, 77, 's', 's', 'dashboard', ?)", (self.neighbor.id, self.their_channel.id, time.time()))
        versions = [(self.neighbor.id, 1, "theirs"), (self.neighbor.id, 2, "theirs too"), (self.guild.id, 5000, "planted")]
        for guild_id, number, content in versions:
            await self.db.run("INSERT INTO posted_versions (guild_id, posted_id, number, payload, note, saved_at) VALUES (?, ?, ?, ?, 'x', 1.0)", (guild_id, posted, number, json.dumps({"content": content, "embeds": [], "buttons": []})))
        embeds = self.cog(Embeds)
        self.assertEqual(await embeds._latest_version(self.neighbor.id, posted), 2)
        self.assertIsNone(await embeds._payload_of_version(self.neighbor.id, posted, 5000))
        await embeds._add_version(self.neighbor.id, posted, {"content": "next", "embeds": [], "buttons": []}, actor=None, note="Edited")
        kept = [row["number"] for row in await self.db.all("SELECT number FROM posted_versions WHERE posted_id = ? ORDER BY number", (posted,))]
        self.assertEqual(kept, [1, 2, 3, 5000])
        await self.db.run("UPDATE posted_messages SET guild_id = ? WHERE id = ?", (self.guild.id, posted))
        client = await self.client()
        listed = await (await client.get(self.path("/embeds/messages"))).json()
        self.assertEqual(listed["rows"][0]["version"], 5000)

    async def test_a_ballot_cant_join_another_servers_vote(self):
        vote = 900000000000000009
        await self.db.run("INSERT INTO mini_mute_votes (vote_message_id, guild_id, channel_id, target_id, initiator_id, minutes, required, expires_at) VALUES (?, ?, ?, 5, 6, 60, 3, ?)", (vote, self.neighbor.id, self.their_channel.id, time.time() + 900))
        doc = await self.doc()
        for voter in range(10, 15):
            self.add_row(doc, "main.mini_mute_ballots", guild_id=str(self.guild.id), vote_message_id=str(vote), voter_id=voter, approve=1)
        await self.restore(doc)
        self.assertEqual(await self.cog(Voice)._ballots(vote), (0, 0))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM mini_mute_ballots WHERE vote_message_id = ?", (vote,)), 0)

    async def test_the_vote_count_reads_its_own_servers_ballots(self):
        vote = 900000000000000009
        await self.db.run("INSERT INTO mini_mute_votes (vote_message_id, guild_id, channel_id, target_id, initiator_id, minutes, required, expires_at) VALUES (?, ?, ?, 5, 6, 60, 3, ?)", (vote, self.neighbor.id, self.their_channel.id, time.time() + 900))
        await self.db.run("INSERT INTO mini_mute_ballots (vote_message_id, guild_id, voter_id, approve) VALUES (?, ?, 10, 1)", (vote, self.guild.id))
        await self.db.run("INSERT INTO mini_mute_ballots (vote_message_id, guild_id, voter_id, approve) VALUES (?, ?, 11, 1)", (vote, self.neighbor.id))
        self.assertEqual(await self.cog(Voice)._ballots(vote), (1, 0))

    async def test_an_item_cant_join_another_servers_repost_job(self):
        job = await self.db.insert("INSERT INTO adopt_jobs (guild_id, target_channel_id, delete_originals, copy_reactions, state, created_at) VALUES (?, ?, 0, 0, 'stopped', ?)", (self.neighbor.id, self.their_channel.id, time.time()))
        doc = await self.doc()
        self.add_row(doc, "main.adopt_items", guild_id=str(self.guild.id), job_id=job, position=0, source_channel_id=str(self.channel.id), source_message_id=1234, author_name="", summary="", state="waiting")
        await self.restore(doc)
        view = await self.cog(Embeds)._job_view(self.neighbor.id, job)
        self.assertEqual(view["items"], [])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM adopt_items WHERE job_id = ?", (job,)), 0)

    async def test_a_job_runs_only_its_own_servers_items(self):
        job = await self.db.insert("INSERT INTO adopt_jobs (guild_id, target_channel_id, delete_originals, copy_reactions, state, created_at) VALUES (?, ?, 0, 0, 'running', ?)", (self.neighbor.id, self.their_channel.id, time.time()))
        await self.db.run("INSERT INTO adopt_items (guild_id, job_id, position, source_channel_id, source_message_id, author_name, summary, state) VALUES (?, ?, 0, ?, 1234, '', '', 'waiting')", (self.guild.id, job, self.channel.id))
        embeds = self.cog(Embeds)
        self.assertEqual((await embeds._job_view(self.neighbor.id, job))["items"], [])
        await embeds._run_job(job)
        self.assertEqual(await self.db.scalar("SELECT state FROM adopt_items WHERE job_id = ?", (job,)), "waiting")
        self.assertEqual(await self.db.scalar("SELECT state FROM adopt_jobs WHERE id = ?", (job,)), "done")

    async def test_two_file_rows_sharing_a_number_held_elsewhere_dont_leave_a_child_behind(self):
        """Only one of them gets a new number, so a child of the other would still point at the other server's row."""
        taken = await self.db.insert("INSERT INTO posted_messages (guild_id, channel_id, message_id, summary, search, source, posted_at) VALUES (?, ?, 77, 's', 's', 'dashboard', ?)", (self.neighbor.id, self.their_channel.id, time.time()))
        also_taken = await self.db.insert("INSERT INTO posted_messages (guild_id, channel_id, message_id, summary, search, source, posted_at) VALUES (?, ?, 78, 's', 's', 'dashboard', ?)", (self.neighbor.id, self.their_channel.id, time.time()))
        doc = await self.doc()
        for number in (taken, also_taken):
            self.add_row(doc, "main.posted_messages", id=number, guild_id=str(self.guild.id), channel_id=str(self.channel.id), message_id=500, summary="s", search="s", source="dashboard", posted_at=1.0)
            self.add_row(doc, "main.posted_versions", guild_id=str(self.guild.id), posted_id=number, number=1, payload="{}", note="x", saved_at=1.0)
        await self.restore(doc)
        strays = await self.db.scalar("SELECT COUNT(*) FROM posted_versions WHERE guild_id = ? AND posted_id NOT IN (SELECT id FROM posted_messages WHERE guild_id = ?)", (self.guild.id, self.guild.id))
        self.assertEqual(strays, 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM posted_versions WHERE posted_id IN (?, ?)", (taken, also_taken)), 0)

    async def test_a_child_follows_a_parent_that_takes_a_new_number(self):
        taken = await self.db.insert("INSERT INTO posted_messages (guild_id, channel_id, message_id, summary, search, source, posted_at) VALUES (?, ?, 77, 's', 's', 'dashboard', ?)", (self.neighbor.id, self.their_channel.id, time.time()))
        doc = await self.doc()
        self.add_row(doc, "main.posted_messages", id=taken, guild_id=str(self.guild.id), channel_id=str(self.channel.id), message_id=500, summary="mine", search="mine", source="dashboard", posted_at=1.0)
        self.add_row(doc, "main.posted_versions", guild_id=str(self.guild.id), posted_id=taken, number=1, payload="{}", note="x", saved_at=1.0)
        await self.restore(doc)
        mine = await self.db.scalar("SELECT id FROM posted_messages WHERE guild_id = ? AND message_id = 500", (self.guild.id,))
        self.assertNotEqual(mine, taken)
        self.assertEqual(await self.db.scalar("SELECT posted_id FROM posted_versions WHERE guild_id = ?", (self.guild.id,)), mine)

    async def test_a_tag_and_its_aliases_round_trip(self):
        tag = await self.db.insert("INSERT INTO tags (guild_id, name, content, owner_id, created_at, updated_at) VALUES (?, 'hello', 'hi', 1, 1.0, 1.0)", (self.guild.id,))
        await self.db.run("INSERT INTO tag_aliases (guild_id, name, tag_id, owner_id, created_at) VALUES (?, 'hey', ?, 1, 1.0)", (self.guild.id, tag))
        await self.db.run("INSERT INTO tag_shares (guild_id, tag_id, code, created_by, created_at) VALUES (?, ?, 'abc', 1, 1.0)", (self.guild.id, tag))
        await self.restore(await self.doc())
        self.assertEqual(await self.db.scalar("SELECT tag_id FROM tag_aliases WHERE guild_id = ?", (self.guild.id,)), tag)
        self.assertEqual(await self.db.scalar("SELECT tag_id FROM tag_shares WHERE guild_id = ?", (self.guild.id,)), tag)


class SquattedMessageTest(Case):
    async def test_a_row_a_file_left_on_a_message_gives_way_to_the_server_that_has_it(self):
        message = 900000000000000001
        await self.db.run("INSERT INTO rr_messages (message_id, guild_id, channel_id, created_at) VALUES (?, ?, ?, 1.0)", (message, self.neighbor.id, self.their_channel.id))
        await self.db.run("INSERT INTO rr_pairs (guild_id, message_id, slot, emoji, emoji_key, role_id) VALUES (?, ?, 0, 'X', 'X', 222)", (self.neighbor.id, message))
        cog = self.cog(ReactionRoles)
        rr = await cog.get(message)
        rr.guild_id = self.guild.id
        rr.channel_id = self.channel.id
        rr.options = []
        await cog._write(rr)
        row = await self.db.one("SELECT guild_id, channel_id FROM rr_messages WHERE message_id = ?", (message,))
        self.assertEqual((row["guild_id"], row["channel_id"]), (self.guild.id, self.channel.id))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_pairs WHERE message_id = ?", (message,)), 0)


class CoverageTest(Case):
    async def catalog(self):
        return await self.backup.catalogs(("main", self.db), ("messages", self.message_db))

    async def test_every_child_table_in_the_program_is_checked(self):
        catalog = await self.catalog()
        self.assertEqual(unchecked_pointers(catalog), [], "These columns look like they name another table's row. Add them to the module's child_links, or to NOT_POINTERS here with the reason.")

    async def test_every_named_link_is_real(self):
        catalog = await self.catalog()
        for table, columns in child_links(self.bot).items():
            found = next(item for item in catalog.tables.values() if item.name == table)
            for column, target in columns.items():
                parent, _, parent_column = target.partition(".")
                self.assertIn(column, found.columns)
                self.assertIn(parent_column, next(item for item in catalog.tables.values() if item.name == parent).columns)
                self.assertTrue(any(link.column == column and link.parent == parent for link in found.links), f"{table}.{column}")

    async def test_the_reasons_list_stays_true(self):
        catalog = await self.catalog()
        names = {(table.name, column) for table in catalog.tables.values() for column in table.columns}
        self.assertEqual({entry for entry in NOT_POINTERS if entry not in names}, set())

    async def test_a_new_child_table_with_nothing_checking_it_is_caught(self):
        steps = ["""
            CREATE TABLE later_parents (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL);
            CREATE TABLE later_children (id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, widget_id INTEGER NOT NULL);
            CREATE TABLE later_things (thing_key INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL);
            CREATE TABLE later_marks (id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, thing_key INTEGER NOT NULL);
            """]
        await self.db.migrate("module:later", steps)
        owners = table_owners(self.bot) | {name: "later" for name in ("later_parents", "later_children", "later_things", "later_marks")}
        catalog = await read_catalog(self.db, "main", owners, child_links(self.bot))
        self.assertEqual(unchecked_pointers(catalog), ["later_children.widget_id", "later_marks.thing_key"])
        declared = {**child_links(self.bot), "later_children": {"widget_id": "later_parents.id"}, "later_marks": {"thing_key": "later_things.thing_key"}}
        catalog = await read_catalog(self.db, "main", owners, declared)
        self.assertEqual(unchecked_pointers(catalog), [])

    async def test_a_child_and_its_parent_are_one_unit(self):
        catalog = await self.catalog()
        for child, parent in (("rr_pairs", "rr_messages"), ("posted_versions", "posted_messages"), ("adopt_items", "adopt_jobs"), ("mini_mute_ballots", "mini_mute_votes"), ("tag_aliases", "tags"), ("tag_shares", "tags")):
            self.assertEqual(catalog.tables[f"main.{child}"].group, catalog.tables[f"main.{parent}"].group, child)
