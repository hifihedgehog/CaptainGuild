"""Logging: what gets stored, what gets posted, who gets named, and the search behind the dashboard."""
from __future__ import annotations

import asyncio
import base64
import csv
import datetime
import io
import json
import time
import zlib
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.core.module import required_permissions
from captainguild.core.ops import upload_limit
from captainguild.core.ops import DiscordOps, OpsError
from captainguild.modules import logs as logs_module
from captainguild.modules.logs import EVENT_KEYS, Logs, csv_safe, export_code, read_import, settings_from_import
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, FakeMessage, FakePermissions, new_id
from tests.gateway import GatewayTest, iso, new_id as gateway_id, user_json

Actions = discord.AuditLogAction
QUICK = {
    "AUDIT_WAIT": 0, "AUDIT_POLL": 0, "RENAME_AUDIT_WAIT": 0, "DELETE_SETTLE": 0, "DELETE_LATE_WAIT": 0, "ROLE_DELETE_GRACE": 0,
    "ROLE_DELETE_HOLD": 0, "AUTOMOD_GATHER": 0, "FLUSH_SECONDS": 0.01,
}


def attachment(filename="photo.png", *, content_type="image/png", size=1000, url=None):
    return SimpleNamespace(filename=filename, url=url or f"https://cdn.discordapp.com/attachments/1/2/{filename}", proxy_url=f"https://media.discordapp.net/attachments/1/2/{filename}", size=size, content_type=content_type)


def field_value(embed, name):
    return next((row["value"] for row in embed.get("fields", []) if row["name"] == name), None)


NOBODY = "The author or a bot"


class Base(BotTest):
    modules = [Logs]

    async def asyncSetUp(self):
        self.quick = mock.patch.multiple(logs_module, **QUICK)
        self.quick.start()
        await super().asyncSetUp()
        self.logs = self.cog(Logs)
        self.log_channel = FakeChannel(self.guild, "logs")
        self.other_channel = FakeChannel(self.guild, "random")
        await self.configure(default_channel=self.log_channel.id)

    async def asyncTearDown(self):
        try:
            await self.logs.cog_unload()
        finally:
            await super().asyncTearDown()
            self.quick.stop()

    async def configure(self, **form):
        await self.bot.settings.save(self.guild.id, "logging", form)

    async def turn_on(self, *events):
        await self.configure(events=[*self.settings("logging")["events"], *events])

    @property
    def posted(self):
        return self.bot.logs.posted

    def embeds(self, title=None):
        return [embed for _, embed, _ in self.posted if title is None or embed["title"].startswith(title)]

    def message(self, content="hello there", *, author=None, channel=None, **kwargs):
        return FakeMessage(channel or self.channel, author or self.member, content, **kwargs)

    async def send(self, content="hello there", **kwargs):
        message = self.message(content, **kwargs)
        await self.logs.on_message(message)
        return message

    async def edit(self, message, content):
        message.content = content
        message.edited_at = discord.utils.utcnow()
        await self.logs.on_raw_message_edit(SimpleNamespace(message=message, guild_id=self.guild.id, channel_id=message.channel.id, message_id=message.id))

    async def delete(self, message_id, channel=None):
        await self.logs.on_raw_message_delete(SimpleNamespace(message_id=message_id, channel_id=(channel or self.channel).id, guild_id=self.guild.id, cached_message=None))

    def audit(self, action, *, user=None, target=None, reason=None, extra=None, before=None, after=None, age=0.0):
        return SimpleNamespace(
            id=new_id(), guild=self.guild, action=action, target=target, user=user or self.mod, user_id=(user or self.mod).id, reason=reason, extra=extra,
            changes=SimpleNamespace(before=SimpleNamespace(**(before or {})), after=SimpleNamespace(**(after or {}))),
            created_at=discord.utils.utcnow() - datetime.timedelta(seconds=age),
        )

    def delete_entry(self, *, author=None, channel=None, count=1, user=None, age=0.0):
        entry = self.audit(Actions.message_delete, user=user, target=author or self.member, extra=SimpleNamespace(channel=channel or self.channel, count=count), age=age)
        self.guild.audit_entries.append(entry)
        return entry


class StoreTest(Base):
    async def test_a_busy_channel_is_written_in_one_batch(self):
        # The timer stays off, so only the flush below writes. On a loaded machine the count read outlasts the quick timer, which then
        # writes the batch before `many` is watched.
        with mock.patch.object(logs_module, "FLUSH_SECONDS", 3600):
            for number in range(500):
                await self.send(f"message {number}")
            self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM messages"), 0)
            with mock.patch.object(self.message_db, "many", wraps=self.message_db.many) as many:
                await self.logs.flush()
        self.assertEqual(many.call_count, 1)
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM messages"), 500)

    async def test_the_timer_writes_the_batch_without_being_asked(self):
        await self.send()
        for _ in range(100):
            if await self.message_db.scalar("SELECT COUNT(*) FROM messages"):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM messages"), 1)

    async def test_bots_webhooks_system_messages_and_dms_stay_out(self):
        robot = FakeMember(self.guild, "robot", bot=True)
        await self.logs.on_message(self.message(author=robot))
        hooked = self.message()
        hooked.webhook_id = new_id()
        await self.logs.on_message(hooked)
        system = self.message()
        system.type = discord.MessageType.new_member
        await self.logs.on_message(system)
        dm = FakeMessage(None, self.member, "secret")
        await self.logs.on_message(dm)
        await self.logs.flush()
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM messages"), 0)

    async def test_stored_message_has_what_other_features_need(self):
        reply = self.message("see above")
        reply.type = discord.MessageType.reply
        reply.reference = SimpleNamespace(message_id=12345)
        reply.attachments = [attachment("a.png")]
        reply.stickers = [SimpleNamespace(name="party")]
        await self.logs.on_message(reply)
        row = await self.logs.stored_message(reply.id)  # still waiting for its batch
        self.assertEqual(row["id"], reply.id)
        self.assertEqual(row["guild_id"], self.guild.id)
        self.assertEqual(row["channel_id"], self.channel.id)
        self.assertEqual(row["author_id"], self.member.id)
        self.assertEqual(row["content"], "see above")
        self.assertEqual(row["reply_to"], 12345)
        self.assertEqual(row["stickers"], ["party"])
        self.assertEqual(row["attachments"][0]["filename"], "a.png")
        for key in ("url", "size", "content_type"):
            self.assertIn(key, row["attachments"][0])
        self.assertEqual(row["created_at"], reply.created_at.timestamp())
        self.assertIsNone(row["deleted_at"])
        self.assertIsNone(row["edited_at"])
        self.assertIsNone(await self.logs.stored_message(999))

    async def test_a_forward_keeps_the_text_it_carries(self):
        forward = self.message("")
        forward.message_snapshots = [SimpleNamespace(content="the original words")]
        await self.logs.on_message(forward)
        self.assertEqual((await self.logs.stored_message(forward.id))["content"], "[Forwarded] the original words")

    async def test_user_messages_are_newest_first_scoped_and_limited(self):
        first = await self.send("first")
        second = await self.send("second")
        await self.send("someone else", author=self.mod)
        elsewhere = FakeGuild("Elsewhere")
        elsewhere.me = FakeMember(elsewhere, "Bot", bot=True)
        visitor = FakeMember(elsewhere, "visitor")
        visitor.id = self.member.id
        other_channel = FakeChannel(elsewhere, "general")
        await self.logs.on_message(FakeMessage(other_channel, visitor, "other server"))
        rows = await self.logs.user_messages(self.guild.id, self.member.id)
        self.assertEqual([row["id"] for row in rows], [second.id, first.id])
        self.assertEqual(len(await self.logs.user_messages(self.guild.id, self.member.id, limit=1)), 1)
        await self.delete(first.id)
        await self.edit(second, "second, edited")
        rows = await self.logs.user_messages(self.guild.id, self.member.id)
        self.assertEqual(rows[0]["content"], "second, edited")
        self.assertIsNotNone(rows[0]["edited_at"])
        self.assertIsNotNone(rows[1]["deleted_at"])

    async def test_user_log_entries_are_newest_first_and_scoped(self):
        await self.turn_on("join")
        await self.logs.on_member_join(self.member)
        message = await self.send()
        await self.delete(message.id)
        await self.logs.on_member_join(self.mod)
        rows = await self.logs.user_log_entries(self.guild.id, self.member.id)
        self.assertEqual([row["event"] for row in rows], ["delete", "join"])
        self.assertEqual(rows[0]["channel_id"], self.channel.id)
        self.assertEqual(rows[0]["user_id"], self.member.id)
        self.assertEqual(rows[0]["embed"]["title"], "Message deleted in #general")
        self.assertIsInstance(rows[0]["at"], float)
        self.assertEqual(await self.logs.user_log_entries(self.guild.id + 1, self.member.id), [])
        self.assertEqual(len(await self.logs.user_log_entries(self.guild.id, self.member.id, limit=1)), 1)
        self.assertEqual([row["event"] for row in await self.logs.user_log_entries(self.guild.id, self.member.id, kind="message")], ["delete"])
        self.assertEqual([row["event"] for row in await self.logs.user_log_entries(self.guild.id, self.member.id, kind="join")], ["join"])

    async def test_a_failed_write_loses_nothing(self):
        # The timer stays off, so the flushes below are the only writes, as in the batching test above.
        with mock.patch.object(logs_module, "FLUSH_SECONDS", 3600):
            await self.send("keep me")
            await self.logs.on_member_join(self.member)
            with mock.patch.object(self.message_db, "many", side_effect=RuntimeError("disk full")):
                with self.assertRaises(RuntimeError):
                    await self.logs.flush()
            self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM messages"), 0)
            await self.logs.flush()
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM messages"), 1)
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM log_entries"), 1)

    async def test_shutting_down_writes_what_was_waiting(self):
        await self.send("last words")
        await self.logs.cog_unload()
        self.assertEqual(await self.message_db.scalar("SELECT content FROM messages"), "last words")

    async def test_rows_written_while_a_flush_runs_are_not_lost(self):
        release = asyncio.Event()
        real_many = self.message_db.many

        async def slow_many(*args):
            await release.wait()
            await real_many(*args)

        first = await self.send("first")
        with mock.patch.object(self.message_db, "many", side_effect=slow_many):
            flushing = asyncio.ensure_future(self.logs.flush())
            await asyncio.sleep(0)
            second = await self.send("second")
            reading = asyncio.ensure_future(self.logs.stored_message(first.id))
            await asyncio.sleep(0)
            release.set()
            await flushing
            self.assertEqual((await reading)["content"], "first")
        self.assertEqual((await self.logs.stored_message(second.id))["content"], "second")


class EditTest(Base):
    async def test_edit_entry_looks_like_the_live_one(self):
        message = await self.send("in a god way")
        await self.edit(message, "in a good way")
        channel_id, embed, _ = self.posted[0]
        self.assertEqual(channel_id, self.log_channel.id)
        self.assertEqual(embed["title"], "Message edited in #general")
        self.assertEqual(field_value(embed, "Before"), "in a god way")
        self.assertEqual(field_value(embed, "After"), "in a good way")
        self.assertEqual(embed["url"], message.jump_url)
        self.assertEqual(embed["color"], 0x4286F4)
        self.assertEqual(embed["author"]["name"], "member")
        self.assertEqual(embed["footer"]["text"], f"ID: {self.member.id}")
        self.assertIn("timestamp", embed)
        self.assertEqual((await self.logs.stored_message(message.id))["content"], "in a good way")

    async def test_a_link_loading_or_a_pin_is_not_an_edit(self):
        message = await self.send("look at https://example.com")
        await self.logs.on_raw_message_edit(SimpleNamespace(message=message, guild_id=self.guild.id, channel_id=self.channel.id, message_id=message.id))
        self.assertEqual(self.posted, [])

    async def test_a_message_older_than_the_store(self):
        old = self.message("edited long after")
        old.edited_at = discord.utils.utcnow()
        await self.logs.on_raw_message_edit(SimpleNamespace(message=old, guild_id=self.guild.id, channel_id=self.channel.id, message_id=old.id))
        self.assertEqual(field_value(self.posted[0][1], "Before"), "(not in the store)")
        self.assertEqual((await self.logs.stored_message(old.id))["content"], "edited long after")
        stale = self.message("a link loaded in an old message")
        stale.edited_at = discord.utils.utcnow() - datetime.timedelta(days=3)
        await self.logs.on_raw_message_edit(SimpleNamespace(message=stale, guild_id=self.guild.id, channel_id=self.channel.id, message_id=stale.id))
        unedited = self.message("never edited")
        await self.logs.on_raw_message_edit(SimpleNamespace(message=unedited, guild_id=self.guild.id, channel_id=self.channel.id, message_id=unedited.id))
        self.assertEqual(len(self.posted), 1)

    async def test_long_text_keeps_the_limit_and_attaches_the_rest(self):
        before, after = "a" * 4000, "b" * 4000
        message = await self.send(before)
        await self.edit(message, after)
        _, embed, file = self.posted[0]
        self.assertLessEqual(len(field_value(embed, "After")), 1024)
        self.assertTrue(field_value(embed, "After").startswith("b"))
        text = file[0].fp.read().decode()
        self.assertIn(before, text)
        self.assertIn(after, text)
        self.assertEqual(file[0].filename, "edited-message.txt")

    async def test_a_removed_attachment_counts_as_an_edit(self):
        message = await self.send("pics", attachments=[attachment("a.png"), attachment("b.png")])
        message.attachments = [attachment("b.png")]
        await self.edit(message, "pics")
        self.assertEqual(field_value(self.posted[0][1], "Attachments removed"), "a.png")

    async def test_the_ignore_lists(self):
        category = FakeChannel(self.guild, "Staff", discord.ChannelType.category)
        in_category = FakeChannel(self.guild, "mods", category=category)
        await self.configure(ignored_channels=[self.other_channel.id, category.id], ignored_members=[self.mod.id], ignored_prefixes=["!"])
        for message, text in (
            (await self.send("x", channel=self.other_channel), "y"),
            (await self.send("x", channel=in_category), "y"),
            (await self.send("x", author=self.mod), "y"),
            (await self.send("!play"), "!play song"),
            (await self.send("before"), "!after"),
        ):
            await self.edit(message, text)
        self.assertEqual(self.posted, [])
        self.assertEqual((await self.logs.user_messages(self.guild.id, self.mod.id))[0]["content"], "y")  # the store keeps them

    async def test_a_bot_editing_its_own_message_is_ignored(self):
        robot = FakeMember(self.guild, "robot", bot=True)
        message = self.message("beep", author=robot)
        message.edited_at = discord.utils.utcnow()
        await self.logs.on_raw_message_edit(SimpleNamespace(message=message, guild_id=self.guild.id, channel_id=self.channel.id, message_id=message.id))
        self.assertEqual(self.posted, [])


class DeleteTest(Base):
    async def test_delete_entry_looks_like_the_live_one(self):
        message = await self.send("oops")
        await self.delete(message.id)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Message deleted in #general")
        self.assertEqual(embed["description"], "oops")
        self.assertEqual(field_value(embed, "Message ID"), str(message.id))
        self.assertEqual(embed["color"], 0xDD5F53)
        self.assertEqual(embed["author"]["name"], "member")
        self.assertEqual(embed["footer"]["text"], f"ID: {self.member.id}")
        self.assertEqual(field_value(embed, "Deleted by"), NOBODY)
        self.assertIsNotNone((await self.logs.stored_message(message.id))["deleted_at"])

    async def test_attachments_stickers_and_replies_get_fields(self):
        message = self.message("", attachments=[attachment("clip.mp4", content_type="video/mp4")])
        message.type = discord.MessageType.reply
        message.reference = SimpleNamespace(message_id=777)
        message.stickers = [SimpleNamespace(name="party")]
        await self.logs.on_message(message)
        await self.delete(message.id)
        embed = self.posted[0][1]
        self.assertNotIn("description", embed)
        self.assertEqual(field_value(embed, "Message ID"), str(message.id))
        fields = {field["name"]: field for field in embed["fields"]}
        self.assertEqual(fields["Attachments"]["value"], "clip.mp4")
        self.assertTrue(fields["Attachments"]["inline"])
        self.assertEqual(fields["Stickers"]["value"], "party")
        self.assertIn("/777", fields["Reply to"]["value"])

    async def test_a_moderators_delete_names_them(self):
        message = await self.send("rude")
        self.delete_entry(count=1)
        await self.delete(message.id)
        self.assertEqual(field_value(self.posted[0][1], "Deleted by"), f"<@{self.mod.id}> (mod)")
        self.assertEqual((await self.logs.stored_message(message.id))["deleted_by"], self.mod.id)
        row = await self.message_db.one("SELECT actor_id, user_id FROM log_entries")
        self.assertEqual(row, {"actor_id": self.mod.id, "user_id": self.member.id})

    async def test_repeated_deletes_raise_one_entrys_count(self):
        first, second, third = await self.send("a"), await self.send("b"), await self.send("c")
        entry = self.delete_entry(count=1)
        await self.delete(first.id)
        entry.extra.count = 2  # Discord raises the count on the same entry and sends no gateway event
        await self.delete(second.id)
        await self.delete(third.id)  # the count didn't move, so the author deleted this one
        named = [field_value(embed, "Deleted by") for _, embed, _ in self.posted]
        self.assertIn(f"<@{self.mod.id}>", named[0])
        self.assertIn(f"<@{self.mod.id}>", named[1])
        self.assertEqual(named[2], NOBODY)

    async def test_a_burst_of_deletes_is_credited_one_by_one_from_one_read(self):
        messages = [await self.send(str(n)) for n in range(5)]
        self.delete_entry(count=5)
        with mock.patch.object(logs_module, "DELETE_SETTLE", 0.05), mock.patch.object(self.bot.audit, "recent", wraps=self.bot.audit.recent) as recent:
            await asyncio.gather(*(self.delete(m.id) for m in messages))
        self.assertEqual(recent.call_count, 1)
        self.assertEqual(sum(f"<@{self.mod.id}>" in field_value(embed, "Deleted by") for _, embed, _ in self.posted), 5)

    async def test_an_old_entry_seen_for_the_first_time_explains_nothing(self):
        message = await self.send("mine")
        self.delete_entry(count=4, age=600)
        await self.delete(message.id)
        self.assertEqual(field_value(self.posted[0][1], "Deleted by"), NOBODY)

    async def test_someone_elses_entry_explains_nothing(self):
        mine = await self.send("mine")
        theirs = await self.send("theirs", author=self.admin)
        elsewhere = await self.send("elsewhere", channel=self.other_channel)
        self.delete_entry(author=self.admin, count=1)
        await self.delete(mine.id)
        self.assertEqual(field_value(self.posted[0][1], "Deleted by"), NOBODY)
        await self.delete(elsewhere.id, channel=self.other_channel)
        self.assertEqual(field_value(self.posted[1][1], "Deleted by"), NOBODY)
        await self.delete(theirs.id)
        self.assertIn(f"<@{self.mod.id}>", field_value(self.posted[2][1], "Deleted by"))

    async def test_a_bot_deleting_is_named_too(self):
        robot = FakeMember(self.guild, "filterbot", bot=True)
        message = await self.send("bad word")
        self.delete_entry(user=robot)
        await self.delete(message.id)
        self.assertIn("filterbot", field_value(self.posted[0][1], "Deleted by"))

    async def test_an_entry_arriving_late_through_the_gateway_still_counts(self):
        message = await self.send("late")
        entry = self.audit(Actions.message_delete, target=self.member, extra=SimpleNamespace(channel=self.channel, count=1))

        async def arrives_late():
            await asyncio.sleep(0.03)
            self.bot.audit.feed(entry)

        with mock.patch.object(logs_module, "DELETE_LATE_WAIT", 1.0):
            await asyncio.gather(self.delete(message.id), arrives_late())
        self.assertIn(f"<@{self.mod.id}>", field_value(self.posted[0][1], "Deleted by"))

    async def test_without_audit_access_nothing_reads_the_log(self):
        self.role_bot.permissions = FakePermissions("send_messages")
        message = await self.send("x")
        with mock.patch.object(self.bot.audit, "recent", wraps=self.bot.audit.recent) as recent:
            await self.delete(message.id)
        recent.assert_not_called()
        self.assertEqual(len(self.posted), 1)

    async def test_a_message_the_store_never_saw(self):
        self.delete_entry(count=1)
        await self.delete(434343)
        embed = self.posted[0][1]
        self.assertIn("isn't in the store", embed["description"])
        self.assertIn(f"<@{self.mod.id}>", field_value(embed, "Deleted by"))
        self.assertIn(f"<@{self.member.id}>", field_value(embed, "Author"))

    async def test_the_ignore_lists(self):
        await self.configure(ignored_channels=[self.other_channel.id], ignored_members=[self.mod.id], ignored_prefixes=["."])
        for message in (await self.send("x", channel=self.other_channel), await self.send("x", author=self.mod), await self.send(".tag")):
            await self.delete(message.id, channel=message.channel)
        self.assertEqual(self.posted, [])

    async def test_a_delete_by_an_ignored_member_stays_out(self):
        await self.configure(ignored_members=[self.mod.id])
        message = await self.send("x")
        self.delete_entry(count=1)
        await self.delete(message.id)
        self.assertEqual(self.posted, [])
        self.assertIsNotNone((await self.logs.stored_message(message.id))["deleted_at"])

    async def test_switched_off_still_marks_the_store(self):
        await self.configure(events=[])
        message = await self.send("x")
        await self.delete(message.id)
        self.assertEqual(self.posted, [])
        self.assertIsNotNone((await self.logs.stored_message(message.id))["deleted_at"])
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM log_entries"), 0)


class DeletedImageTest(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.downloads = []

        async def download(item):
            self.downloads.append(item["filename"])
            return b"image bytes" if "gone" not in item["filename"] else None

        self.logs._download = download

    async def test_the_image_comes_along_and_shows_in_the_entry(self):
        message = await self.send("look", attachments=[attachment("cat photo.png")])
        await self.delete(message.id)
        _, embed, files = self.posted[0]
        self.assertEqual(files[0].filename, "cat_photo.png")
        self.assertEqual(files[0].fp.read(), b"image bytes")
        self.assertEqual(embed["image"]["url"], "attachment://cat_photo.png")
        await self.logs.flush()
        stored = json.loads(await self.message_db.scalar("SELECT embed FROM log_entries"))
        self.assertNotIn("image", stored)
        self.assertEqual(stored["fields"][0]["value"], "cat photo.png")

    async def test_a_download_starts_before_the_wait_for_the_audit_log(self):
        message = await self.send("look", attachments=[attachment()])
        fetched = asyncio.Event()
        real = self.logs._download

        async def download(item):
            fetched.set()
            return await real(item)

        self.logs._download = download
        with mock.patch.object(logs_module, "DELETE_SETTLE", 0.5):
            deleting = asyncio.ensure_future(self.delete(message.id))
            await asyncio.wait_for(fetched.wait(), 0.4)
            self.assertEqual(self.posted, [])  # the audit wait hasn't finished, and the download already began
            await deleting
        self.assertEqual(len(self.posted), 1)

    async def test_an_image_discord_no_longer_serves_still_leaves_the_entry(self):
        message = await self.send("look", attachments=[attachment("gone.png")])
        await self.delete(message.id)
        _, embed, files = self.posted[0]
        self.assertIsNone(files)
        self.assertNotIn("image", embed)
        self.assertEqual(embed["fields"][0]["value"], "gone.png\n1 image could no longer be fetched.")

    async def test_only_images_and_only_a_few_and_only_what_fits(self):
        files = [attachment(f"{n}.png") for n in range(6)]
        message = await self.send("many", attachments=[attachment("clip.mp4", content_type="video/mp4"), attachment("huge.png", size=upload_limit(self.guild) + 1), *files])
        await self.delete(message.id)
        self.assertEqual(self.downloads, ["0.png", "1.png", "2.png", "3.png"])

    async def test_an_unboosted_server_copies_images_up_to_discords_default(self):
        """Discord raised the default upload limit to 20 MiB on September 3, 2026. discord.py 2.7.1 still says 10 below boost level 2."""
        self.assertEqual(self.guild.filesize_limit, 10 * 1024 * 1024)
        message = await self.send("big", attachments=[attachment("fifteen.png", size=15 * 1024 * 1024)])
        await self.delete(message.id)
        self.assertEqual(self.downloads, ["fifteen.png"])

    async def test_a_spoiler_stays_a_spoiler(self):
        message = await self.send("careful", attachments=[attachment("SPOILER_end.png")])
        await self.delete(message.id)
        _, embed, files = self.posted[0]
        self.assertEqual(files[0].filename, "SPOILER_end.png")
        self.assertNotIn("image", embed)

    async def test_a_name_discord_would_change_is_made_safe_first(self):
        message = await self.send("look", attachments=[attachment("\u732b\u306e\u5199\u771f v2.PNG")])
        await self.delete(message.id)
        _, embed, files = self.posted[0]
        self.assertRegex(files[0].filename, r"^[A-Za-z0-9_.-]+$")
        self.assertTrue(files[0].filename.endswith(".PNG"))
        self.assertEqual(embed["image"]["url"], f"attachment://{files[0].filename}")

    async def test_a_flood_of_deleted_images_cant_fill_memory(self):
        self.bot.logs.queued_files = lambda: logs_module.MAX_QUEUED_COPIES
        message = await self.send("look", attachments=[attachment()])
        await self.delete(message.id)
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.posted[0][1]["fields"][0]["value"], "photo.png")  # the entry itself still goes out

    async def test_two_files_with_one_name(self):
        message = await self.send("two", attachments=[attachment("a.png"), attachment("a.png")])
        await self.delete(message.id)
        self.assertEqual([f.filename for f in self.posted[0][2]], ["a.png", "a_2.png"])

    async def test_the_switch_turns_it_off(self):
        await self.configure(attach_deleted_images=False)
        message = await self.send("look", attachments=[attachment()])
        await self.delete(message.id)
        self.assertEqual(self.downloads, [])
        self.assertIsNone(self.posted[0][2])


class AuditFailureTest(Base):
    async def test_a_failed_read_of_the_log_still_leaves_the_entry(self):
        message = await self.send("x")
        with mock.patch.object(self.bot.audit, "recent", side_effect=OSError("network down")):
            await self.delete(message.id)
        self.assertEqual(len(self.posted), 1)
        self.assertNotIn("Deleted by", self.posted[0][1]["description"])
        await self.turn_on("purge")
        other = await self.send("y")
        with mock.patch.object(self.bot.audit, "recent", side_effect=OSError("network down")):
            await self.logs.on_raw_bulk_message_delete(SimpleNamespace(message_ids={other.id}, channel_id=self.channel.id, guild_id=self.guild.id, cached_messages=[]))
        self.assertEqual(self.posted[1][1]["title"], "Messages purged in #general")


class DownloadTest(Base):
    async def test_only_discords_own_hosts_are_ever_fetched(self):
        fetched = []

        async def fetch(url):
            fetched.append(url)
            return b"x"

        self.bot.http.get_from_cdn = fetch
        result = await self.logs._download({"filename": "a.png", "url": "http://169.254.169.254/latest/meta-data", "proxy_url": "https://evil.example/a.png"})
        self.assertIsNone(result)
        self.assertIsNone(await self.logs._download({"filename": "a.png", "url": "http://[broken", "proxy_url": None}))
        self.assertEqual(fetched, [])
        result = await self.logs._download({"filename": "a.png", "url": "https://cdn.discordapp.com/attachments/1/2/a.png", "proxy_url": None})
        self.assertEqual(result, b"x")

    async def test_falls_back_to_the_proxy_and_survives_refusal(self):
        calls = []

        async def fetch(url):
            calls.append(url)
            if "cdn.discordapp.com" in url:
                raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
            raise asyncio.TimeoutError

        self.bot.http.get_from_cdn = fetch
        self.assertIsNone(await self.logs._download({"filename": "a.png", "url": "https://cdn.discordapp.com/a.png", "proxy_url": "https://media.discordapp.net/a.png"}))
        self.assertEqual(len(calls), 2)


class PurgeTest(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.turn_on("purge")

    async def purge(self, *ids, channel=None):
        await self.logs.on_raw_bulk_message_delete(SimpleNamespace(message_ids=set(ids), channel_id=(channel or self.channel).id, guild_id=self.guild.id, cached_messages=[]))

    async def test_a_file_lists_every_message(self):
        first = await self.send("first words", attachments=[attachment("a.png")])
        second = await self.send("second words", author=self.mod)
        await self.purge(first.id, second.id, 555)
        _, embed, file = self.posted[0]
        self.assertEqual(embed["title"], "Messages purged in #general")
        self.assertIn("**Messages:** 3", embed["description"])
        self.assertEqual(file[0].filename, "purged-messages.txt")
        text = file[0].fp.read().decode()
        self.assertIn("first words", text)
        self.assertIn(f"member ({self.member.id})", text)
        self.assertIn("second words", text)
        self.assertIn("a.png", text)
        self.assertIn("[not in the store] Message ID 555", text)
        self.assertIsNotNone((await self.logs.stored_message(first.id))["deleted_at"])
        stored = await self.message_db.one("SELECT detail, search_text FROM log_entries")
        self.assertIn("first words", stored["detail"])
        self.assertIn("first words", stored["search_text"])

    async def test_ignored_prefixes_members_and_bot_messages_stay_out_of_the_file(self):
        await self.configure(ignored_prefixes=["!"], ignored_members=[self.admin.id])
        robot = FakeMember(self.guild, "robot", bot=True)
        spoken = self.message("beep", author=robot)
        await self.logs.on_message(spoken)
        command = await self.send("!play")
        by_admin = await self.send("secret", author=self.admin)
        kept = await self.send("kept")
        await self.purge(spoken.id, command.id, by_admin.id, kept.id)
        text = self.posted[0][2][0].fp.read().decode()
        self.assertIn("kept", text)
        self.assertIn(f"[a bot] robot ({robot.id}): beep", text)
        for left_out in ("!play", "secret"):
            self.assertNotIn(left_out, text)

    async def test_nothing_left_to_show_means_no_entry(self):
        await self.configure(ignored_prefixes=["!"])
        command = await self.send("!play")
        await self.purge(command.id)
        self.assertEqual(self.posted, [])

    async def test_an_ignored_channel(self):
        await self.configure(ignored_channels=[self.channel.id])
        message = await self.send("x")
        await self.purge(message.id)
        self.assertEqual(self.posted, [])

    async def test_switched_off(self):
        await self.configure(events=[])
        message = await self.send("x")
        await self.purge(message.id)
        self.assertEqual(self.posted, [])
        self.assertIsNotNone((await self.logs.stored_message(message.id))["deleted_at"])

    async def test_names_who_did_it_with_the_reason(self):
        message = await self.send("x")
        self.bot.audit.feed(self.audit(Actions.message_bulk_delete, target=self.channel, reason="clean up", extra=SimpleNamespace(count=1)))
        await self.purge(message.id)
        description = self.posted[0][1]["description"]
        self.assertIn(f"**By:** <@{self.mod.id}> (mod)", description)
        self.assertIn("**Reason:** clean up", description)

    async def test_a_purge_by_an_ignored_member_stays_out(self):
        await self.configure(ignored_members=[self.mod.id])
        message = await self.send("x")
        self.bot.audit.feed(self.audit(Actions.message_bulk_delete, target=self.channel, extra=SimpleNamespace(count=1)))
        await self.purge(message.id)
        self.assertEqual(self.posted, [])

    async def test_a_second_purge_raises_the_first_entrys_count(self):
        first, second = await self.send("a"), await self.send("b")
        entry = self.audit(Actions.message_bulk_delete, target=self.channel, extra=SimpleNamespace(count=2))
        self.guild.audit_entries.append(entry)
        await self.purge(first.id)
        self.assertIn("**By:**", self.posted[0][1]["description"])
        entry.extra.count = 4
        await self.purge(second.id)
        self.assertIn("**By:**", self.posted[1][1]["description"])
        third = await self.send("c")
        await self.purge(third.id)  # the count didn't move and this entry is known, so it says nothing about this one
        self.assertNotIn("**By:**", self.posted[2][1]["description"])


class RoutingTest(Base):
    async def test_an_events_own_channel_beats_the_default(self):
        messages = FakeChannel(self.guild, "message-log")
        await self.configure(message_channel=messages.id)
        message = await self.send("x")
        await self.delete(message.id)
        await self.logs.on_member_join(self.member)
        self.assertEqual(self.posted[0][0], messages.id)
        self.assertEqual(self.posted[1][0], self.log_channel.id)

    async def test_every_kind_reaches_its_channel(self):
        await self.configure(events=list(EVENT_KEYS))
        made = {field: FakeChannel(self.guild, field) for field in ("message_channel", "member_channel", "join_channel", "server_channel", "voice_channel")}
        await self.configure(**{field: channel.id for field, channel in made.items()})
        for event, field in (("delete", "message_channel"), ("role", "member_channel"), ("join", "join_channel"), ("channelupdate", "server_channel"), ("threadcreate", "server_channel"), ("voicejoin", "voice_channel"), ("automodaction", "member_channel"), ("webhooks", "server_channel"), ("pins", "message_channel")):
            self.assertEqual(self.logs._channel_for(self.guild, event), made[field].id, event)

    async def test_a_deleted_log_channel_falls_back(self):
        gone = FakeChannel(self.guild, "gone")
        await self.configure(message_channel=gone.id)
        self.guild.channels.remove(gone)
        self.assertEqual(self.logs._channel_for(self.guild, "delete"), self.log_channel.id)

    async def test_a_channel_in_another_server_never_gets_posted_to(self):
        elsewhere = FakeGuild("Elsewhere")
        elsewhere.me = FakeMember(elsewhere, "Bot", bot=True)
        foreign = FakeChannel(elsewhere, "secret")
        self.bot.fake_guilds.append(elsewhere)
        await self.configure(default_channel=foreign.id)
        message = await self.send("x")
        await self.delete(message.id)
        self.assertEqual(self.posted, [])

    async def test_with_no_channel_an_entry_is_still_kept(self):
        await self.configure(default_channel=None)
        message = await self.send("x")
        await self.delete(message.id)
        self.assertEqual(self.posted, [])
        self.assertEqual(len(await self.logs.user_log_entries(self.guild.id, self.member.id)), 1)

    async def test_an_event_switched_off_is_not_kept_or_posted(self):
        await self.configure(events=["edit"])
        await self.logs.on_member_join(self.member)
        await self.logs.flush()
        self.assertEqual(self.posted, [])
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM log_entries"), 0)


class JoinLeaveTest(Base):
    async def test_a_new_account_gets_the_warning(self):
        fresh = FakeMember(self.guild, "fresh", created=discord.utils.utcnow() - datetime.timedelta(hours=11))
        await self.logs.on_member_join(fresh)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Member joined")
        self.assertEqual(embed["color"], 0x53DDAC)
        self.assertTrue(embed["description"].startswith(f"<@{fresh.id}> {self.guild.member_count}th to join\n"))
        self.assertIn("**NEW ACCOUNT** created 11 hours", embed["description"])
        self.assertEqual(embed["footer"]["text"], f"ID: {fresh.id}")

    async def test_an_old_account_doesnt(self):
        old = FakeMember(self.guild, "old", created=discord.utils.utcnow() - datetime.timedelta(days=400))
        await self.logs.on_member_join(old)
        self.assertNotIn("NEW ACCOUNT", self.posted[0][1]["description"])
        self.assertIn("created 1 year", self.posted[0][1]["description"])

    async def test_the_threshold_is_a_setting_and_zero_turns_it_off(self):
        await self.configure(new_account_days=30)
        recent = FakeMember(self.guild, "recent", created=discord.utils.utcnow() - datetime.timedelta(days=10))
        await self.logs.on_member_join(recent)
        self.assertIn("NEW ACCOUNT", self.posted[0][1]["description"])
        await self.configure(new_account_days=0)
        fresh = FakeMember(self.guild, "fresh", created=discord.utils.utcnow() - datetime.timedelta(minutes=1))
        await self.logs.on_member_join(fresh)
        self.assertNotIn("NEW ACCOUNT", self.posted[1][1]["description"])

    async def test_leaving_lists_the_roles(self):
        leaver = FakeMember(self.guild, "leaver", self.role_member, self.role_mod, joined=discord.utils.utcnow() - datetime.timedelta(minutes=2))
        await self.logs.on_raw_member_remove(SimpleNamespace(guild_id=self.guild.id, user=leaver))
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Member left")
        self.assertEqual(embed["color"], 0xFFF5AF)
        self.assertIn("joined 2 minutes", embed["description"])
        self.assertIn(f"**Roles:** {self.role_mod.mention}, {self.role_member.mention}", embed["description"])

    async def test_someone_the_cache_never_held_still_gets_an_entry(self):
        user = SimpleNamespace(id=new_id(), name="stranger", mention="<@1>", display_avatar=SimpleNamespace(url="https://cdn.example/a.png"))
        await self.logs.on_raw_member_remove(SimpleNamespace(guild_id=self.guild.id, user=user))
        self.assertEqual(self.posted[0][1]["description"], "<@1>")

    async def test_ignored_members_stay_out(self):
        await self.configure(ignored_members=[self.member.id])
        await self.logs.on_member_join(self.member)
        await self.logs.on_raw_member_remove(SimpleNamespace(guild_id=self.guild.id, user=self.member))
        self.assertEqual(self.posted, [])


class MemberChangeTest(Base):
    def copy_of(self, member):
        before = SimpleNamespace(id=member.id, name=member.name, nick=member.nick, roles=list(member.roles), timed_out_until=member.timed_out_until, guild=member.guild, guild_avatar=None)
        before.is_timed_out = lambda: before.timed_out_until is not None
        return before

    async def change_roles(self, member, *, add=(), remove=()):
        before = self.copy_of(member)
        member.roles = [r for r in member.roles if r not in remove] + list(add)
        await self.logs.on_member_update(before, member)

    def role_entry(self, member, *, added=(), removed=(), user=None):
        return self.audit(Actions.member_role_update, user=user, target=member, after={"roles": list(added)}, before={"roles": list(removed)})

    async def test_one_role_added_or_removed_looks_like_the_live_entry(self):
        await self.change_roles(self.member, add=[self.role_muted])
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Role added")
        self.assertEqual(embed["description"], f"{self.role_muted.mention} (Muted)")
        self.assertEqual(embed["author"]["name"], "member")
        self.assertEqual(embed["color"], 0x4286F4)
        await self.change_roles(self.member, remove=[self.role_muted])
        self.assertEqual(self.posted[1][1]["title"], "Role removed")

    async def test_several_at_once_become_one_entry(self):
        await self.change_roles(self.member, add=[self.role_muted, self.role_mod], remove=[self.role_member])
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Roles updated")
        self.assertIn(f"**Added:** {self.role_muted.mention} (Muted), {self.role_mod.mention} (Moderator)", embed["description"])
        self.assertIn(f"**Removed:** {self.role_member.mention} (Member)", embed["description"])

    async def test_names_who_changed_the_role(self):
        self.bot.audit.feed(self.role_entry(self.member, added=[self.role_muted]))
        await self.change_roles(self.member, add=[self.role_muted])
        self.assertIn(f"**By:** <@{self.mod.id}> (mod)", self.posted[0][1]["description"])
        await self.logs.flush()
        self.assertEqual(await self.message_db.scalar("SELECT actor_id FROM log_entries"), self.mod.id)

    async def test_two_changes_close_together_each_name_their_own_actor(self):
        robot = FakeMember(self.guild, "rolebot", bot=True)
        self.bot.audit.feed(self.role_entry(self.member, added=[self.role_muted], user=self.mod))
        self.bot.audit.feed(self.role_entry(self.member, added=[self.role_mod], user=robot))
        await self.change_roles(self.member, add=[self.role_muted])
        self.assertIn("(mod)", self.posted[0][1]["description"])
        await self.change_roles(self.member, add=[self.role_mod])
        self.assertIn("(rolebot)", self.posted[1][1]["description"])

    async def test_the_bot_switch(self):
        await self.configure(log_bot_actions=False)
        robot = FakeMember(self.guild, "rolebot", bot=True)
        self.bot.audit.feed(self.role_entry(self.member, added=[self.role_muted], user=robot))
        await self.change_roles(self.member, add=[self.role_muted])
        self.assertEqual(self.posted, [])
        self.bot.audit.feed(self.role_entry(self.member, added=[self.role_mod], user=self.mod))
        await self.change_roles(self.member, add=[self.role_mod])
        self.assertEqual(len(self.posted), 1)

    async def test_the_bot_switch_covers_roles_only_integrations_hand_out(self):
        await self.configure(log_bot_actions=False)
        booster = self.guild.role("Booster", 3, managed=True)
        await self.change_roles(self.member, add=[booster])
        self.assertEqual(self.posted, [])

    async def test_ignored_roles_and_people(self):
        await self.configure(ignored_roles=[self.role_muted.id])
        await self.change_roles(self.member, add=[self.role_muted])
        self.assertEqual(self.posted, [])
        await self.configure(ignored_roles=[], ignored_members=[self.mod.id])
        self.bot.audit.feed(self.role_entry(self.member, added=[self.role_mod]))
        await self.change_roles(self.member, add=[self.role_mod])
        self.assertEqual(self.posted, [])  # the person who made the change is ignored

    async def test_nicknames(self):
        before = self.copy_of(self.member)
        self.member.nick = "Sam"
        await self.logs.on_member_update(before, self.member)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Nickname added")
        self.assertEqual(embed["description"], "**Before:** member\n**+After:** Sam")
        before = self.copy_of(self.member)
        self.member.nick = "Samantha"
        self.bot.audit.feed(self.audit(Actions.member_update, target=self.member, after={"nick": "Samantha"}))
        await self.logs.on_member_update(before, self.member)
        self.assertEqual(self.posted[1][1]["title"], "Nickname changed")
        self.assertIn("**By:** <@", self.posted[1][1]["description"])
        before = self.copy_of(self.member)
        self.member.nick = None
        self.bot.audit._entries.clear()
        self.bot.audit.feed(self.audit(Actions.member_update, user=self.member, target=self.member, after={"nick": None}))
        await self.logs.on_member_update(before, self.member)
        self.assertEqual(self.posted[2][1]["title"], "Nickname removed")
        self.assertNotIn("**By:**", self.posted[2][1]["description"])  # changing your own needs no name

    async def test_a_bot_changing_a_nickname_obeys_the_switch(self):
        await self.configure(log_bot_actions=False)
        robot = FakeMember(self.guild, "namebot", bot=True)
        self.bot.audit.feed(self.audit(Actions.member_update, user=robot, target=self.member, after={"nick": "clean"}))
        before = self.copy_of(self.member)
        self.member.nick = "clean"
        await self.logs.on_member_update(before, self.member)
        self.assertEqual(self.posted, [])

    async def test_timeouts(self):
        await self.turn_on("timeout", "removetimeout")
        until = discord.utils.utcnow() + datetime.timedelta(hours=1)
        before = self.copy_of(self.member)
        self.member.timed_out_until = until
        self.bot.audit.feed(self.audit(Actions.member_update, target=self.member, reason="spam", after={"timed_out_until": until}))
        await self.logs.on_member_update(before, self.member)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Member timed out")
        self.assertIn(f"<t:{int(until.timestamp())}:f>", embed["description"])
        self.assertIn("**Reason:** spam", embed["description"])
        self.assertIn("(mod)", embed["description"])
        before = self.copy_of(self.member)
        self.member.timed_out_until = None
        await self.logs.on_member_update(before, self.member)
        self.assertEqual(self.posted[1][1]["title"], "Timeout removed")

    async def test_a_timeout_that_just_ran_out_is_nobodys_doing(self):
        await self.turn_on("timeout", "removetimeout")
        before = self.copy_of(self.member)
        before.timed_out_until = discord.utils.utcnow() - datetime.timedelta(minutes=1)
        before.is_timed_out = lambda: False
        await self.logs.on_member_update(before, self.member)
        self.assertEqual(self.posted, [])

    async def test_timeouts_are_on_by_default_and_obey_the_bot_switch(self):
        before = self.copy_of(self.member)
        self.member.timed_out_until = discord.utils.utcnow() + datetime.timedelta(hours=1)
        await self.logs.on_member_update(before, self.member)
        self.assertEqual(len(self.posted), 1)
        self.posted.clear()
        await self.configure(log_bot_actions=False)
        robot = FakeMember(self.guild, "modbot", bot=True)
        self.bot.audit.feed(self.audit(Actions.member_update, user=robot, target=self.member, after={"timed_out_until": self.member.timed_out_until}))
        before = self.copy_of(self.member)
        before.timed_out_until = None
        await self.logs.on_member_update(before, self.member)
        self.assertEqual(self.posted, [])

    async def test_a_server_avatar_change(self):
        before = self.copy_of(self.member)
        before.guild_avatar = "old"
        self.member.guild_avatar = "new"
        await self.logs.on_member_update(before, self.member)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Avatar update")
        self.assertEqual(embed["description"], self.member.mention)
        self.assertEqual(embed["thumbnail"]["url"], self.member.display_avatar.url)

    async def test_user_updates_reach_every_server_they_share(self):
        before = SimpleNamespace(id=self.member.id, name="old_name", global_name="Old", avatar="a")
        after = SimpleNamespace(id=self.member.id, name="new_name", global_name="New", avatar="b", mention=self.member.mention, display_avatar=self.member.display_avatar)
        await self.logs.on_user_update(before, after)
        titles = [embed["title"] for _, embed, _ in self.posted]
        self.assertEqual(titles, ["Avatar update", "Name change", "Display name change"])
        self.assertEqual(self.posted[1][1]["description"], "**Before:** old_name\n**+After:** new_name")
        self.posted.clear()
        stranger = SimpleNamespace(id=123, name="x", global_name="y", avatar="z", mention="", display_avatar=self.member.display_avatar)
        await self.logs.on_user_update(SimpleNamespace(id=123, name="w", global_name="y", avatar="z"), stranger)
        self.assertEqual(self.posted, [])

    async def test_bans_name_who_and_why(self):
        self.bot.audit.feed(self.audit(Actions.ban, target=self.member, reason="spam"))
        await self.logs.on_member_ban(self.guild, self.member)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Member banned")
        self.assertEqual(embed["color"], 0xDD5F53)
        self.assertIn(self.member.mention, embed["description"])
        self.assertIn("**Reason:** spam", embed["description"])
        await self.logs.on_member_unban(self.guild, self.member)
        self.assertEqual(self.posted[1][1]["title"], "Member unbanned")

    async def test_a_ban_by_an_ignored_member_stays_out(self):
        await self.configure(ignored_members=[self.mod.id])
        self.bot.audit.feed(self.audit(Actions.ban, target=self.member))
        await self.logs.on_member_ban(self.guild, self.member)
        self.assertEqual(self.posted, [])

    async def test_ignored_member_gets_no_member_events(self):
        await self.configure(ignored_members=[self.member.id])
        await self.change_roles(self.member, add=[self.role_muted])
        await self.logs.on_member_ban(self.guild, self.member)
        self.assertEqual(self.posted, [])


class RoleDeleteTest(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.turn_on("roledelete", "role")

    async def test_lists_who_had_it(self):
        role = self.guild.role("Doomed", 3, color=0x112233)
        holders = [FakeMember(self.guild, f"holder{n}", role) for n in range(3)]
        self.guild.roles.remove(role)
        self.bot.audit.feed(self.audit(Actions.role_delete, target=role))
        await self.logs.on_guild_role_delete(role)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], 'Role "Doomed" removed')
        self.assertEqual(embed["color"], 0xDD5F53)
        self.assertEqual(embed["footer"]["text"], f"Role ID: {role.id}")
        self.assertIn("**Color:** #112233", embed["description"])
        self.assertIn("**Position:** 3", embed["description"])
        self.assertIn("**By:** <@", embed["description"])
        self.assertIn("**Members who had it:** 3", embed["description"])
        for holder in holders:
            self.assertIn(f"<@{holder.id}>", embed["description"])

    async def test_a_big_role_goes_into_a_file(self):
        role = self.guild.role("Popular", 3)
        for n in range(400):
            FakeMember(self.guild, f"person{n}", role)
        await self.logs.on_guild_role_delete(role)
        _, embed, files = self.posted[0]
        self.assertLessEqual(len(embed["description"]), 4096)
        self.assertIn("**Members who had it:** 400", embed["description"])
        self.assertEqual(files[0].filename, "role-members.txt")
        self.assertIn("person399", files[0].fp.read().decode())

    async def test_updates_around_the_delete_join_the_list_instead_of_logging_one_by_one(self):
        role = self.guild.role("Doomed", 3)
        early = FakeMember(self.guild, "early", role)
        before = SimpleNamespace(id=early.id, name=early.name, nick=None, roles=list(early.roles), timed_out_until=None, guild=self.guild, guild_avatar=None)
        before.is_timed_out = lambda: False
        early.roles = [self.guild.default_role]  # Discord told us about this member first
        with mock.patch.multiple(logs_module, ROLE_DELETE_GRACE=0.02, ROLE_DELETE_HOLD=0.05):
            await asyncio.gather(self.logs.on_member_update(before, early), self.logs.on_guild_role_delete(role))
        self.assertEqual([embed["title"] for _, embed, _ in self.posted], ['Role "Doomed" removed'])
        self.assertIn(f"<@{early.id}>", self.posted[0][1]["description"])

    async def test_a_role_gone_before_the_update_is_processed_stays_quiet(self):
        role = self.guild.role("Doomed", 3)
        member = FakeMember(self.guild, "someone", role)
        before = SimpleNamespace(id=member.id, name=member.name, nick=None, roles=list(member.roles), timed_out_until=None, guild=self.guild, guild_avatar=None)
        before.is_timed_out = lambda: False
        member.roles = [self.guild.default_role]
        self.guild.roles.remove(role)
        await self.logs.on_member_update(before, member)
        self.assertEqual(self.posted, [])

    async def test_switched_off_or_ignored(self):
        await self.configure(events=["role"])
        role = self.guild.role("Doomed", 3)
        await self.logs.on_guild_role_delete(role)
        self.assertEqual(self.posted, [])
        self.assertEqual(self.logs._deleted_roles, {})
        await self.configure(events=["roledelete"], ignored_roles=[role.id])
        await self.logs.on_guild_role_delete(role)
        self.assertEqual(self.posted, [])


class ChannelEventTest(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.turn_on("channelcreate", "channelupdate", "channeldelete")

    def rename(self, channel, new_name, **others):
        before = SimpleNamespace(id=channel.id, name=channel.name, topic=channel.topic, category=channel.category, nsfw=channel.nsfw, overwrites=channel.overwrites, type=channel.type, guild=self.guild)
        channel.name = new_name
        for key, value in others.items():
            setattr(channel, key, value)
        return before

    async def test_a_rename_looks_like_the_live_entry(self):
        before = self.rename(self.channel, "renamed")
        await self.logs.on_guild_channel_update(before, self.channel)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Text channel updated")
        self.assertEqual(embed["footer"]["text"], f"Channel ID: {self.channel.id}")
        self.assertEqual(embed["fields"][0], {"name": "Before", "value": "**Name:** general", "inline": True})
        self.assertEqual(embed["fields"][1], {"name": "After", "value": "**Name:** renamed", "inline": True})

    async def test_a_stat_counter_bots_renames_are_dropped(self):
        counter = FakeMember(self.guild, "statbot", bot=True)
        await self.configure(rename_ignored_bots=[counter.id])
        self.bot.audit.feed(self.audit(Actions.channel_update, user=counter, target=self.channel, after={"name": "Members: 2"}))
        await self.logs.on_guild_channel_update(self.rename(self.channel, "Members: 2"), self.channel)
        self.assertEqual(self.posted, [])

    async def test_the_same_bot_changing_anything_else_is_logged(self):
        counter = FakeMember(self.guild, "statbot", bot=True)
        await self.configure(rename_ignored_bots=[counter.id])
        self.bot.audit.feed(self.audit(Actions.channel_update, user=counter, target=self.channel))
        await self.logs.on_guild_channel_update(self.rename(self.channel, "Members: 2", topic="also this"), self.channel)
        self.assertEqual(len(self.posted), 1)
        self.assertIn("(statbot)", self.posted[0][1]["description"])

    async def test_a_rename_by_anyone_else_is_logged_and_named(self):
        counter = FakeMember(self.guild, "statbot", bot=True)
        await self.configure(rename_ignored_bots=[counter.id])
        self.bot.audit.feed(self.audit(Actions.channel_update, target=self.channel))
        await self.logs.on_guild_channel_update(self.rename(self.channel, "renamed"), self.channel)
        self.assertIn(f"**By:** <@{self.mod.id}> (mod)", self.posted[0][1]["description"])

    async def test_without_an_audit_entry_a_rename_is_logged(self):
        counter = FakeMember(self.guild, "statbot", bot=True)
        await self.configure(rename_ignored_bots=[counter.id])
        await self.logs.on_guild_channel_update(self.rename(self.channel, "unknown hand"), self.channel)
        self.assertEqual(len(self.posted), 1)

    async def test_ignore_lists_cover_server_events(self):
        category = FakeChannel(self.guild, "Stats", discord.ChannelType.category)
        inside = FakeChannel(self.guild, "counter", category=category)
        await self.configure(ignored_channels=[self.channel.id, category.id])
        await self.logs.on_guild_channel_update(self.rename(self.channel, "renamed"), self.channel)
        await self.logs.on_guild_channel_update(self.rename(inside, "renamed"), inside)
        await self.logs.on_guild_channel_delete(inside)
        await self.logs.on_guild_channel_create(inside)
        self.assertEqual(self.posted, [])

    async def test_changes_by_an_ignored_member_stay_out(self):
        await self.configure(ignored_members=[self.mod.id])
        self.bot.audit.feed(self.audit(Actions.channel_update, target=self.channel))
        await self.logs.on_guild_channel_update(self.rename(self.channel, "renamed"), self.channel)
        self.assertEqual(self.posted, [])

    async def test_moving_a_channel_says_nothing(self):
        before = self.rename(self.channel, "general")
        self.channel.position = 40
        await self.logs.on_guild_channel_update(before, self.channel)
        self.assertEqual(self.posted, [])

    async def test_create_and_delete(self):
        category = FakeChannel(self.guild, "Community", discord.ChannelType.category)
        new = FakeChannel(self.guild, "new-channel", category=category)
        self.bot.audit.feed(self.audit(Actions.channel_create, target=new))
        await self.logs.on_guild_channel_create(new)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Text channel created")
        self.assertEqual(embed["color"], 0x53DDAC)
        self.assertIn("**Name:** new-channel\n**Category:** Community", embed["description"])
        self.assertIn("(mod)", embed["description"])
        await self.logs.on_guild_channel_delete(new)
        self.assertEqual(self.posted[1][1]["title"], "Text channel deleted")
        self.assertEqual(self.posted[1][1]["color"], 0xDD5F53)


class VoiceTest(Base):
    def state(self, channel=None, *, mute=False, deaf=False):
        return SimpleNamespace(channel=channel, mute=mute, deaf=deaf)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.lounge = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)
        self.gaming = FakeChannel(self.guild, "Gaming", discord.ChannelType.voice)
        await self.turn_on("voicejoin", "voicemove", "voiceleave", "voicemod")

    async def test_joining_moving_and_leaving(self):
        await self.logs.on_voice_state_update(self.member, self.state(), self.state(self.lounge))
        await self.logs.on_voice_state_update(self.member, self.state(self.lounge), self.state(self.gaming))
        await self.logs.on_voice_state_update(self.member, self.state(self.gaming), self.state())
        titles = [embed["title"] for _, embed, _ in self.posted]
        self.assertEqual(titles, ["Member joined voice channel", "Member changed voice channel", "Member left voice channel"])
        self.assertEqual(self.posted[0][1]["description"], "**member** joined #Lounge")
        self.assertEqual(self.posted[1][1]["description"], "**Before:** #Lounge\n**+After:** #Gaming")
        self.assertEqual(self.posted[2][1]["description"], "**member** left #Gaming")
        self.assertEqual([embed["color"] for _, embed, _ in self.posted], [0x53DDAC, 0x4286F4, 0xDD5F53])

    async def test_a_self_mute_is_no_event(self):
        await self.logs.on_voice_state_update(self.member, self.state(self.lounge), self.state(self.lounge))
        self.assertEqual(self.posted, [])

    async def test_a_server_mute_names_who(self):
        self.bot.audit.feed(self.audit(Actions.member_update, target=self.member, after={"mute": True}))
        await self.logs.on_voice_state_update(self.member, self.state(self.lounge), self.state(self.lounge, mute=True))
        self.assertIn("server muted", self.posted[0][1]["description"])
        self.assertIn("(mod)", self.posted[0][1]["description"])

    async def test_ignored_channels_and_members(self):
        await self.configure(ignored_channels=[self.lounge.id])
        await self.logs.on_voice_state_update(self.member, self.state(), self.state(self.lounge))
        await self.logs.on_voice_state_update(self.member, self.state(self.lounge), self.state(self.lounge, mute=True))
        await self.configure(ignored_channels=[], ignored_members=[self.member.id])
        await self.logs.on_voice_state_update(self.member, self.state(), self.state(self.gaming))
        self.assertEqual(self.posted, [])


class AuditEventTest(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.configure(events=list(EVENT_KEYS))

    async def test_kicks_name_who_and_why(self):
        target = SimpleNamespace(id=new_id(), name="kicked", display_avatar=self.member.display_avatar)
        await self.logs.on_audit_log_entry_create(self.audit(Actions.kick, target=target, reason="rude"))
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Member kicked")
        self.assertIn(f"<@{target.id}> (kicked)", embed["description"])
        self.assertIn("**Reason:** rude", embed["description"])

    async def test_an_ignored_actor_leaves_no_entry(self):
        await self.configure(ignored_members=[self.mod.id])
        await self.logs.on_audit_log_entry_create(self.audit(Actions.kick, target=self.member))
        self.assertEqual(self.posted, [])

    async def test_an_event_switched_off_leaves_no_entry(self):
        await self.configure(events=[])
        await self.logs.on_audit_log_entry_create(self.audit(Actions.kick, target=self.member))
        await self.logs.on_audit_log_entry_create(self.audit(Actions.webhook_create, target=SimpleNamespace(id=1)))
        self.assertEqual(self.posted, [])

    async def test_webhooks(self):
        entry = self.audit(Actions.webhook_create, target=SimpleNamespace(id=55), after={"name": "alerts", "channel": self.channel})
        await self.logs.on_audit_log_entry_create(entry)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Webhook created")
        self.assertIn("**Name:** alerts", embed["description"])
        self.assertIn(f"<#{self.channel.id}>", embed["description"])

    async def test_the_bots_own_webhooks_stay_out(self):
        self.bot._connection.user = SimpleNamespace(id=self.me.id)
        entry = self.audit(Actions.webhook_create, user=self.me, target=SimpleNamespace(id=55), after={"name": "Logging", "channel": self.channel})
        await self.logs.on_audit_log_entry_create(entry)
        self.assertEqual(self.posted, [])

    async def test_a_pin_shows_the_message(self):
        message = await self.send("pin me")
        entry = self.audit(Actions.message_pin, target=self.member, extra=SimpleNamespace(channel=self.channel, message_id=message.id))
        await self.logs.on_audit_log_entry_create(entry)
        embed = self.posted[0][1]
        self.assertEqual(embed["title"], "Message pinned in #general")
        self.assertEqual(embed["description"], "pin me")
        self.assertIn("(mod)", field_value(embed, "By"))
        self.assertIn(str(message.id), embed["url"])

    async def test_an_action_nothing_handles_is_left_alone(self):
        await self.logs.on_audit_log_entry_create(self.audit(Actions.overwrite_update, target=self.channel))
        self.assertEqual(self.posted, [])


class PruneTest(Base):
    async def add_message(self, days_old, guild_id=None):
        message_id = new_id()
        await self.message_db.run(
            "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, created_at) VALUES (?, ?, 1, 1, 'x', 'x', ?)",
            (message_id, guild_id or self.guild.id, time.time() - days_old * 86400),
        )
        return message_id

    async def add_entry(self, days_old, text="findme", guild_id=None):
        embed = logs_module.make_embed("t", description=text)
        self.logs._store_entry(guild_id or self.guild.id, "delete", embed, at=time.time() - days_old * 86400)
        await self.logs.flush()

    async def test_old_rows_go_and_new_rows_stay(self):
        old, fresh = await self.add_message(31), await self.add_message(29)
        await self.add_entry(91, "oldword")
        await self.add_entry(89, "newword")
        self.assertEqual(await self.logs.prune(), (1, 1))
        self.assertIsNone(await self.logs.stored_message(old))
        self.assertIsNotNone(await self.logs.stored_message(fresh))
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM log_search WHERE log_search MATCH '\"oldword\"'"), 0)
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM log_search WHERE log_search MATCH '\"newword\"'"), 1)
        await self.message_db.run("INSERT INTO log_search (log_search) VALUES ('integrity-check')")  # raises if the search index and the table disagree

    async def test_retention_is_each_servers_own(self):
        other = new_id()
        await self.bot.settings.save(other, "logging", {"message_retention_days": 5, "log_retention_days": 5})
        mine, theirs = await self.add_message(10), await self.add_message(10, other)
        await self.add_entry(10, guild_id=other)
        await self.add_entry(10)
        await self.logs.prune()
        self.assertIsNotNone(await self.logs.stored_message(mine))
        self.assertIsNone(await self.logs.stored_message(theirs))
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM log_entries WHERE guild_id = ?", (other,)), 0)
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM log_entries WHERE guild_id = ?", (self.guild.id,)), 1)

    async def test_a_server_that_never_saved_settings_gets_the_defaults(self):
        never = new_id()
        gone, kept = await self.add_message(31, never), await self.add_message(1, never)
        await self.logs.prune()
        self.assertIsNone(await self.logs.stored_message(gone))
        self.assertIsNotNone(await self.logs.stored_message(kept))

    async def test_a_backlog_bigger_than_one_bite_all_goes(self):
        with mock.patch.object(logs_module, "PRUNE_CHUNK", 3):
            for _ in range(10):
                await self.add_message(40)
            removed, _ = await self.logs.prune()
        self.assertEqual(removed, 10)

    async def test_the_timer_is_there_after_a_restart_and_runs_again(self):
        timer = await self.bot.timers.get_key("logging.prune", "all")
        self.assertIsNotNone(timer)
        await self.logs.on_load()  # a restart finds it and doesn't add a second
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'logging.prune'"), 1)
        old = await self.add_message(31)
        self.assertEqual(await self.bot.timers.run_due(time.time() + logs_module.PRUNE_SECONDS + 1), 1)
        self.assertIsNone(await self.logs.stored_message(old))
        again = await self.bot.timers.get_key("logging.prune", "all")
        self.assertGreater(again["due_at"], time.time())
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'logging.prune'"), 1)

    async def test_a_failing_prune_still_schedules_the_next(self):
        with mock.patch.object(self.logs, "prune", side_effect=RuntimeError("busy")):
            await self.bot.timers.run_due(time.time() + logs_module.PRUNE_SECONDS + 1)
        self.assertIsNotNone(await self.bot.timers.get_key("logging.prune", "all"))


class CommandTest(Base):
    async def test_channel_commands_set_and_clear(self):
        for name, field in (("log_channel", "default_channel"), ("log_message_channel", "message_channel"), ("log_member_channel", "member_channel"), ("log_join_channel", "join_channel"), ("log_server_channel", "server_channel"), ("log_voice_channel", "voice_channel")):
            ctx = self.context()
            command = getattr(self.logs, name)
            await command.callback(self.logs, ctx, self.other_channel)
            self.assertEqual(self.settings("logging")[field], self.other_channel.id)
            self.assertIn(self.other_channel.mention, ctx.oks[0])
            await command.callback(self.logs, ctx, None)
            self.assertIsNone(self.settings("logging")[field])
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "log.voice_channel")
        self.assertEqual(rows[0]["user_name"], "admin")

    async def test_every_command_needs_manage_server(self):
        for command in self.logs.log_root.walk_commands():
            self.assertEqual(required_permissions(command), ("manage_guild",), command.qualified_name)
        self.assertEqual(required_permissions(self.logs.log_root), ("manage_guild",))
        self.assertTrue(self.logs.log_root.app_command.default_permissions.manage_guild)

    async def test_warns_when_the_bot_cant_post(self):
        with mock.patch.object(self.other_channel, "permissions_for", return_value=SimpleNamespace(view_channel=True, send_messages=False, embed_links=True)):
            ctx = self.context()
            await self.logs.log_channel.callback(self.logs, ctx, self.other_channel)
        self.assertIn("can't post there", ctx.oks[0])

    async def test_show(self):
        ctx = self.context()
        await self.logs.log_root.callback(self.logs, ctx)
        self.assertIn(self.log_channel.mention, ctx.sent[0]["content"])
        self.assertEqual(ctx.sent[0]["allowed_mentions"].users, False)

    async def test_config_flips_one_event(self):
        ctx = self.context()
        await self.logs.log_config.callback(self.logs, ctx, "pins")
        self.assertIn("pins", self.settings("logging")["events"])
        await self.logs.log_config.callback(self.logs, ctx, "PINS")
        self.assertNotIn("pins", self.settings("logging")["events"])
        self.assertEqual(len(ctx.oks), 2)

    async def test_config_groups(self):
        ctx = self.context()
        await self.logs.log_config.callback(self.logs, ctx, "channels")
        for key in ("channelcreate", "channelupdate", "channeldelete"):
            self.assertIn(key, self.settings("logging")["events"])
        await self.logs.log_config.callback(self.logs, ctx, "channels")
        self.assertNotIn("channelcreate", self.settings("logging")["events"])
        await self.logs.log_config.callback(self.logs, ctx, "bans")  # both on by default, so this turns both off
        self.assertNotIn("ban", self.settings("logging")["events"])
        self.assertNotIn("unban", self.settings("logging")["events"])
        await self.logs.log_config.callback(self.logs, ctx, "everything")
        self.assertEqual(set(self.settings("logging")["events"]), set(EVENT_KEYS))
        await self.logs.log_config.callback(self.logs, ctx, "nothing")
        self.assertEqual(self.settings("logging")["events"], [])
        await self.logs.log_config.callback(self.logs, ctx, "default")
        self.assertEqual(set(self.settings("logging")["events"]), set(logs_module.DEFAULT_EVENTS))

    async def test_config_blank_and_wrong(self):
        ctx = self.context()
        await self.logs.log_config.callback(self.logs, ctx, None)
        embed = ctx.sent[0]["embed"]
        self.assertEqual(len(embed.fields), len(logs_module.EVENT_GROUPS))
        for field in embed.fields:
            self.assertLessEqual(len(field.value), 1024)
        await self.logs.log_config.callback(self.logs, ctx, "nonsense")
        self.assertIn("isn't an event", ctx.fails[0])

    async def test_config_autocomplete_stays_within_discords_limit(self):
        found = await self.logs.log_config_event(None, "")
        self.assertLessEqual(len(found), 25)
        found = await self.logs.log_config_event(None, "thread")
        self.assertEqual({c.value for c in found}, {"threadcreate", "threadupdate", "threaddelete", "threads"})

    async def test_ignore_takes_mentions_ids_and_names(self):
        ctx = self.context()
        gone = new_id()
        await self.logs.log_ignore.callback(self.logs, ctx, targets=f"{self.other_channel.mention} {self.member.mention}, {self.role_muted.mention} {gone} {self.channel.name} nobody-here")
        cfg = self.settings("logging")
        self.assertEqual(set(cfg["ignored_channels"]), {self.other_channel.id, self.channel.id})
        self.assertEqual(set(cfg["ignored_members"]), {self.member.id, gone})
        self.assertEqual(cfg["ignored_roles"], [self.role_muted.id])
        self.assertIn("nobody-here", ctx.oks[0])
        await self.logs.log_unignore.callback(self.logs, ctx, targets=f"{self.other_channel.mention} {gone}")
        cfg = self.settings("logging")
        self.assertEqual(cfg["ignored_channels"], [self.channel.id])
        self.assertEqual(cfg["ignored_members"], [self.member.id])

    async def test_a_long_answer_stays_inside_discords_limit(self):
        ctx = self.context()
        await self.logs.log_ignore.callback(self.logs, ctx, targets=f"{self.channel.mention} " + " ".join(f"unknown{n}" for n in range(1000)))
        self.assertLessEqual(len(ctx.oks[0]), 1900)
        await self.logs.log_config.callback(self.logs, ctx, "x" * 3000)
        self.assertLessEqual(len(ctx.fails[0]), 200)

    async def test_values_the_settings_refuse_get_a_plain_answer(self):
        ctx = self.context()
        await self.logs.log_prefix.callback(self.logs, ctx, "x" * 40)
        self.assertIn("at most 32", ctx.fails[0])
        self.assertEqual(self.settings("logging")["ignored_prefixes"], [])
        await self.configure(ignored_members=[900000000000000000 + n for n in range(1000)])
        ctx = self.context()
        await self.logs.log_ignore.callback(self.logs, ctx, targets=self.member.mention)
        self.assertIn("at most 1000", ctx.fails[0])

    async def test_ignore_with_nothing_found(self):
        ctx = self.context()
        await self.logs.log_ignore.callback(self.logs, ctx, targets="@everyone-nobody")
        self.assertEqual(self.settings("logging")["ignored_members"], [])
        self.assertTrue(ctx.fails)

    async def test_prefixes(self):
        ctx = self.context()
        await self.logs.log_prefix.callback(self.logs, ctx, "!")
        await self.logs.log_prefix.callback(self.logs, ctx, "!")
        self.assertEqual(self.settings("logging")["ignored_prefixes"], ["!"])
        self.assertEqual(len(ctx.fails), 1)
        await self.logs.log_remove_prefix.callback(self.logs, ctx, "?")
        self.assertEqual(len(ctx.fails), 2)
        await self.logs.log_remove_prefix.callback(self.logs, ctx, "!")
        self.assertEqual(self.settings("logging")["ignored_prefixes"], [])

    async def test_export_then_import(self):
        await self.configure(message_channel=self.other_channel.id, ignored_prefixes=["!"], events=["delete", "purge"])
        ctx = self.context()
        await self.logs.log_export.callback(self.logs, ctx)
        code = ctx.sent[0]["content"].strip("`\n")
        await self.configure(message_channel=None, ignored_prefixes=[], events=[])
        await self.logs.log_import.callback(self.logs, ctx, code=code)
        cfg = self.settings("logging")
        self.assertEqual(cfg["message_channel"], self.other_channel.id)
        self.assertEqual(cfg["ignored_prefixes"], ["!"])
        self.assertEqual(cfg["events"], ["delete", "purge"])

    async def test_import_from_the_old_setup(self):
        form = {
            "guild_id": self.guild.id, "ignored_entities": [str(self.channel.id), str(self.member.id), str(self.role_muted.id)], "ignored_prefixes": None, "log_channel": None,
            "logging": 1019, "message_channel": str(self.other_channel.id), "moderation_channel": str(self.other_channel.id), "movement_channel": str(self.log_channel.id),
            "server_channel": None, "voice_channel": None,
        }
        ctx = self.context()
        await self.logs.log_import.callback(self.logs, ctx, code=json.dumps(form))
        cfg = self.settings("logging")
        self.assertEqual(set(cfg["events"]), {"delete", "moddelete", "edit", "role", "name", "avatar", "ban", "unban", "join", "leave"})
        self.assertEqual(cfg["message_channel"], self.other_channel.id)
        self.assertEqual(cfg["member_channel"], self.other_channel.id)
        self.assertEqual(cfg["join_channel"], self.log_channel.id)
        self.assertIsNone(cfg["default_channel"])
        self.assertEqual(cfg["ignored_channels"], [self.channel.id])
        self.assertEqual(cfg["ignored_members"], [self.member.id])
        self.assertEqual(cfg["ignored_roles"], [self.role_muted.id])

    async def test_a_bare_event_number_changes_only_the_events(self):
        await self.configure(ignored_prefixes=["!"], ignored_channels=[self.channel.id])
        ctx = self.context()
        await self.logs.log_import.callback(self.logs, ctx, code="3")
        cfg = self.settings("logging")
        self.assertEqual(set(cfg["events"]), {"delete", "moddelete", "edit"})
        self.assertEqual(cfg["ignored_prefixes"], ["!"])
        self.assertEqual(cfg["ignored_channels"], [self.channel.id])

    async def test_import_drops_another_servers_channels_and_roles(self):
        foreign_channel, foreign_role = new_id(), new_id()
        code = export_code({"default_channel": foreign_channel, "message_channel": self.channel.id, "ignored_channels": [foreign_channel, self.channel.id], "ignored_roles": [foreign_role]})
        ctx = self.context()
        await self.logs.log_import.callback(self.logs, ctx, code=code)
        cfg = self.settings("logging")
        self.assertIsNone(cfg["default_channel"])
        self.assertEqual(cfg["message_channel"], self.channel.id)
        self.assertEqual(cfg["ignored_channels"], [self.channel.id])
        self.assertEqual(cfg["ignored_roles"], [])

    async def test_import_refuses_nonsense(self):
        for bad in ("not a code", "{not json", "[1, 2]", "9" * 30, json.dumps({"events": ["nonsense"]}), json.dumps({"logging": "lots"})):
            ctx = self.context()
            before = dict(self.settings("logging"))
            await self.logs.log_import.callback(self.logs, ctx, code=bad)
            self.assertTrue(ctx.fails, bad)
            self.assertEqual(self.settings("logging"), before)

    async def test_a_code_that_unpacks_to_something_huge_is_refused_without_keeping_it(self):
        bomb = base64.urlsafe_b64encode(zlib.compress(b'{"a":"' + b"x" * (5 * 1024 * 1024) + b'"}', 9)).decode()
        self.assertLess(len(bomb), 100_000)
        with self.assertRaises(ValueError):
            read_import(bomb)

    async def test_aio_creates_the_channels_and_splits_logging(self):
        real = self.bot.ops
        self.bot.ops = DiscordOps(self.bot)
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.role_mod.id})
        ctx = self.context()
        await self.logs.log_aio.callback(self.logs, ctx)
        self.bot.ops = real
        cfg = self.settings("logging")
        names = {field: self.guild.get_channel(cfg[field]).name for field in ("message_channel", "member_channel", "join_channel", "server_channel", "voice_channel")}
        self.assertEqual(names, {"message_channel": "message-log", "member_channel": "member-log", "join_channel": "join-log", "server_channel": "server-log", "voice_channel": "voice-log"})
        self.assertEqual(self.guild.get_channel(cfg["message_channel"]).category.name, "Logs")
        category = self.guild.get_channel(cfg["message_channel"]).category
        self.assertEqual(category.type, discord.ChannelType.category)
        self.assertFalse(category.overwrites[self.guild.default_role].view_channel)
        self.assertTrue(category.overwrites[self.guild.me].manage_webhooks)
        self.assertTrue(category.overwrites[self.role_mod].view_channel)
        channel = self.guild.get_channel(cfg["voice_channel"])
        self.assertFalse(channel.overwrites[self.guild.default_role].view_channel)  # hidden by its own settings too
        self.assertEqual(len(ctx.oks), 1)
        again = self.context()
        await self.logs.log_aio.callback(self.logs, again)
        self.assertIn("already set", again.fails[0])

    async def test_aio_failure_is_told_and_changes_nothing(self):
        with mock.patch.object(self.bot.ops, "create_private_category", side_effect=OpsError("The bot can't create channels here. It needs Manage Channels.")):
            ctx = self.context()
            await self.logs.log_aio.callback(self.logs, ctx)
        self.assertIn("Manage Channels", ctx.fails[0])
        self.assertIsNone(self.settings("logging")["message_channel"])


class AioRollbackTest(Base):
    async def test_the_bot_only_grants_itself_what_it_holds(self):
        self.role_bot.permissions = FakePermissions("view_channel", "send_messages", "embed_links", "manage_channels")
        made = await DiscordOps(self.bot).create_private_category(self.guild.id, "Logs", ["a"], viewer_roles=[self.role_mod.id])
        channel = self.guild.get_channel(made["channels"]["a"])
        mine = channel.overwrites[self.guild.me]
        self.assertTrue(mine.view_channel)
        self.assertTrue(mine.send_messages)
        self.assertIsNone(mine.manage_webhooks)
        self.assertIsNone(mine.attach_files)
        self.assertIsNone(channel.overwrites[self.role_mod].read_message_history)

    async def test_a_failure_partway_removes_what_was_made(self):
        ops = DiscordOps(self.bot)
        real = self.guild.create_text_channel
        made = []

        async def create(name, **kwargs):
            if len(made) == 2:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "Missing Permissions")
            channel = await real(name, **kwargs)
            channel.delete = mock.AsyncMock()
            made.append(channel)
            return channel

        self.guild.create_text_channel = create
        with self.assertRaises(OpsError) as caught:
            await ops.create_private_category(self.guild.id, "Logs", ["a", "b", "c"])
        self.assertIn("Manage Channels", str(caught.exception))
        for channel in made:
            channel.delete.assert_awaited()


class FloodTest(Base):
    async def test_a_long_outage_cant_use_up_memory(self):
        with mock.patch.object(logs_module, "MAX_PENDING", 3), mock.patch.object(logs_module, "FLUSH_SECONDS", 60):
            for number in range(10):
                await self.send(f"message {number}")
                self.logs._store_entry(self.guild.id, "delete", logs_module.make_embed(f"entry {number}"))
            self.assertLessEqual(len(self.logs._new), 3)
            self.assertLessEqual(len(self.logs._entries), 3)

    async def test_a_very_large_result_says_it_was_capped(self):
        for number in range(5):
            self.logs._store_entry(self.guild.id, "delete", logs_module.make_embed(f"entry {number}"))
        await self.logs.flush()
        client = await self.client(access="staff")
        with mock.patch.object(logs_module, "COUNT_CAP", 3):
            body = await (await client.get(self.path("/logs/search"), params={"size": "10"})).json()
        self.assertEqual(body["total"], 3)
        self.assertTrue(body["capped"])


class NoAuditAccessTest(Base):
    async def test_webhook_changes_still_get_a_line(self):
        await self.turn_on("webhooks")
        await self.logs.on_webhooks_update(self.channel)
        self.assertEqual(self.posted, [])  # with access, the audit entry says what happened instead
        self.role_bot.permissions = FakePermissions("send_messages")
        await self.logs.on_webhooks_update(self.channel)
        self.assertEqual(self.posted[0][1]["title"], "Webhooks changed in #general")

    async def test_nothing_waits_for_an_entry_that_cant_come(self):
        self.role_bot.permissions = FakePermissions("send_messages")
        with mock.patch.object(logs_module, "AUDIT_WAIT", 30):
            await asyncio.wait_for(self.logs.on_member_ban(self.guild, self.member), 2)
        self.assertEqual(self.posted[0][1]["title"], "Member banned")


class SearchEndpointTest(Base):
    async def seed(self):
        self.a_id, self.b_id = 900000000000000301, 900000000000000302
        now = time.time()
        rows = [
            ("delete", "Message deleted in #general", "the quick brown fox", self.channel.id, None, self.a_id, None, now - 100),
            ("edit", "Message edited in #general", "**Before:** lazy dog\n**+After:** lazy cat", self.channel.id, None, self.b_id, None, now - 200),
            ("role", "Role added", "<@&1>\n**By:** someone", None, None, self.a_id, self.b_id, now - 86400 * 2),
            ("voicejoin", "Member joined voice channel", "**zed** joined #Lounge", self.other_channel.id, None, self.b_id, None, now - 86400 * 5),
            ("threadcreate", "Thread created", "**Name:** printing tips", 12345, self.channel.id, self.a_id, None, now - 86400 * 6),
        ]
        for event, title, text, channel_id, parent_id, user_id, actor_id, at in rows:
            self.logs._store_entry(self.guild.id, event, logs_module.make_embed(title, description=text, at=at), at=at, channel_id=channel_id, parent_id=parent_id, user_id=user_id, actor_id=actor_id)
        self.logs._store_entry(self.guild.id + 1, "delete", logs_module.make_embed("Other server", description="the quick brown fox"), channel_id=self.channel.id, user_id=self.a_id)
        await self.logs.flush()

    async def search(self, client, **params):
        response = await client.get(self.path("/logs/search"), params={k: str(v) for k, v in params.items()})
        return response.status, await response.json()

    async def titles(self, client, **params):
        status, body = await self.search(client, **params)
        self.assertEqual(status, 200, body)
        return [row["embed"]["title"] for row in body["rows"]]

    async def test_everything_comes_newest_first_as_discord_would_show_it(self):
        await self.seed()
        client = await self.client(access="staff")
        status, body = await self.search(client)
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["rows"][0]["event"], "delete")
        self.assertEqual(body["rows"][0]["label"], "Deleted messages")
        self.assertEqual(body["rows"][0]["embed"]["description"], "the quick brown fox")
        self.assertEqual(body["rows"][0]["user_id"], str(self.a_id))
        self.assertEqual(body["retention_days"], 90)

    async def test_filter_by_member_finds_them_as_subject_or_actor(self):
        await self.seed()
        client = await self.client(access="staff")
        titles = await self.titles(client, user=self.b_id)
        self.assertEqual(titles, ["Message edited in #general", "Role added", "Member joined voice channel"])

    async def test_filter_by_channel_takes_in_its_threads(self):
        await self.seed()
        client = await self.client(access="staff")
        titles = await self.titles(client, channel=self.channel.id)
        self.assertEqual(titles, ["Message deleted in #general", "Message edited in #general", "Thread created"])

    async def test_filter_by_event(self):
        await self.seed()
        client = await self.client(access="staff")
        self.assertEqual(await self.titles(client, event="role"), ["Role added"])
        self.assertEqual(len(await self.titles(client, event="delete,edit")), 2)

    async def test_filter_by_date(self):
        await self.seed()
        client = await self.client(access="staff")
        now = time.time()
        self.assertEqual(await self.titles(client, **{"from": now - 86400 * 3, "to": now - 86400}), ["Role added"])
        self.assertEqual(len(await self.titles(client, **{"from": now - 86400})), 2)

    async def test_filter_by_text(self):
        await self.seed()
        client = await self.client(access="staff")
        self.assertEqual(await self.titles(client, q="quick"), ["Message deleted in #general"])
        self.assertEqual(await self.titles(client, q="QUICK fox"), ["Message deleted in #general"])
        self.assertEqual(await self.titles(client, q="uick"), ["Message deleted in #general"])  # the middle of a word counts
        self.assertEqual(await self.titles(client, q="quick cat"), [])
        self.assertEqual(await self.titles(client, q="#Lounge"), ["Member joined voice channel"])
        self.assertEqual(await self.titles(client, q="by"), ["Role added"])  # too short for the index
        self.assertEqual(await self.titles(client, q="#g fox"), ["Message deleted in #general"])

    async def test_text_cant_be_read_as_search_syntax(self):
        await self.seed()
        client = await self.client(access="staff")
        for text in ('"', '""', 'quick"', "NEAR(a b)", "a AND", "NOT", "*", "col:thing", "%", "_", "\\", "a\x00b", "-x", "()", "^abc", "'; DROP TABLE log_entries; --"):
            status, body = await self.search(client, q=text)
            self.assertEqual(status, 200, text)
        self.assertEqual((await self.search(client))[1]["total"], 5)
        self.assertEqual(await self.titles(client, q="%"), [])

    async def test_filters_combine(self):
        await self.seed()
        client = await self.client(access="staff")
        self.assertEqual(await self.titles(client, user=self.a_id, event="delete", q="fox", channel=self.channel.id), ["Message deleted in #general"])
        self.assertEqual(await self.titles(client, user=self.b_id, q="fox"), [])

    async def test_pages(self):
        for number in range(25):
            self.logs._store_entry(self.guild.id, "delete", logs_module.make_embed(f"entry {number}", at=1000.0 + number), at=time.time() - 1000 + number)
        await self.logs.flush()
        client = await self.client(access="staff")
        status, body = await self.search(client, size=10)
        self.assertEqual((body["total"], body["pages"], body["page"]), (25, 3, 1))
        self.assertEqual(body["rows"][0]["embed"]["title"], "entry 24")
        status, body = await self.search(client, size=10, page=3)
        self.assertEqual(len(body["rows"]), 5)
        self.assertEqual(body["rows"][-1]["embed"]["title"], "entry 0")
        status, body = await self.search(client, size=10, page=99)
        self.assertEqual(body["page"], 3)

    async def test_entries_waiting_for_their_batch_are_found(self):
        message = await self.send("just now")
        await self.delete(message.id)
        client = await self.client(access="staff")
        self.assertEqual(await self.titles(client, q="just now"), ["Message deleted in #general"])

    async def test_bad_filters_get_a_clear_answer(self):
        client = await self.client(access="staff")
        for params in ({"user": "abc"}, {"channel": "12"}, {"event": "nonsense"}, {"from": "yesterday"}, {"to": "nan"}, {"from": "inf"}, {"page": "0"}, {"page": "x"}, {"size": "7"}, {"q": "x" * 201}):
            status, body = await self.search(client, **params)
            self.assertEqual(status, 400, params)
            self.assertTrue(body["error"])

    async def test_another_server_is_invisible(self):
        await self.seed()
        client = await self.client(access="staff")
        status, body = await self.search(client, q="fox")
        self.assertEqual(body["total"], 1)
        other = await self.message_db.one("SELECT id FROM log_entries WHERE guild_id = ?", (self.guild.id + 1,))
        response = await client.get(self.path(f"/logs/entries/{other['id']}"))
        self.assertEqual(response.status, 404)
        foreign = await client.get(f"/api/g/{self.guild.id + 1}/logs/search")
        self.assertEqual(foreign.status, 403)

    async def test_an_entry_with_a_file_keeps_its_text_for_the_page(self):
        await self.turn_on("purge")
        message = await self.send("purged words")
        await self.logs.on_raw_bulk_message_delete(SimpleNamespace(message_ids={message.id}, channel_id=self.channel.id, guild_id=self.guild.id, cached_messages=[]))
        client = await self.client(access="staff")
        status, body = await self.search(client, event="purge")
        row = body["rows"][0]
        self.assertTrue(row["has_detail"])
        self.assertNotIn("detail", row)
        response = await client.get(self.path(f"/logs/entries/{row['id']}"))
        self.assertIn("purged words", (await response.json())["detail"])
        self.assertEqual(await self.titles(client, q="purged words"), ["Messages purged in #general"])

    async def test_names_come_from_the_current_member_list(self):
        self.logs._store_entry(self.guild.id, "role", logs_module.make_embed("Role added"), user_id=self.bot.ops.members[1]["id"], actor_id=self.bot.ops.members[0]["id"])
        await self.logs.flush()
        client = await self.client(access="staff")
        _, body = await self.search(client)
        self.assertEqual(body["rows"][0]["user_name"], "Sample Member")
        self.assertEqual(body["rows"][0]["actor_name"], "Demo Admin")

    async def test_queries_use_indexes(self):
        await self.seed()

        async def plan(sql, args=()):
            return " | ".join(row["detail"] for row in await self.message_db.all("EXPLAIN QUERY PLAN " + sql, args))

        blank = {"user": None, "channel": None, "events": [], "since": None, "until": None, "text": ""}
        cases = [
            ({}, ["log_entries_time"]),
            ({"since": 1.0, "until": 2.0}, ["log_entries_time"]),
            ({"user": 5}, ["log_entries_user", "log_entries_actor"]),
            ({"channel": 5}, ["log_entries_channel", "log_entries_parent"]),
            ({"events": ["delete"]}, ["log_entries_event"]),
            ({"text": "quick"}, ["log_search"]),
        ]
        for filters, indexes in cases:
            where, args = logs_module.search_where(self.guild.id, {**blank, **filters})
            found = await plan(f"SELECT e.id FROM log_entries e WHERE {where} ORDER BY e.at DESC, e.id DESC LIMIT 25", args)
            for index in indexes:
                self.assertIn(index, found, filters)
            self.assertNotRegex(found, r"SCAN (e|log_entries)( |$)(?!USING)", filters)
        found = await plan("SELECT id FROM messages WHERE guild_id = 1 AND author_id = 2 ORDER BY id DESC LIMIT 50")
        self.assertIn("messages_author", found)
        self.assertNotIn("TEMP B-TREE", found)
        found = await plan("SELECT at, event FROM log_entries WHERE guild_id = 1 AND user_id = 2 ORDER BY at DESC, id DESC LIMIT 50")
        self.assertIn("log_entries_user", found)
        self.assertNotIn("TEMP B-TREE", found)
        found = await plan("SELECT id FROM messages WHERE guild_id = 1 AND created_at < 5 LIMIT 2000")
        self.assertIn("messages_age", found)
        found = await plan("SELECT id FROM log_entries WHERE guild_id = 1 AND at < 5 LIMIT 2000")
        self.assertIn("log_entries_time", found)


class ExportEndpointTest(Base):
    async def seed(self):
        self.logs._store_entry(self.guild.id, "delete", logs_module.make_embed("Message deleted", description="=HYPERLINK(\"http://evil.example\")", author={"name": "@sneaky"}, footer="ID: 900000000000000301"), at=time.time(), channel_id=self.channel.id, user_id=900000000000000301, message_id=900000000000000999, detail="+full text")
        self.logs._store_entry(self.guild.id, "role", logs_module.make_embed("Role added", description="\N{WHITE HEAVY CHECK MARK} emoji stays"), at=time.time() - 50, user_id=900000000000000302)
        self.logs._store_entry(self.guild.id + 1, "delete", logs_module.make_embed("Other server's secret"), at=time.time())
        await self.logs.flush()

    async def test_csv(self):
        await self.seed()
        client = await self.client(access="staff")
        response = await client.get(self.path("/logs/export"))
        self.assertEqual(response.status, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn(".csv", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        raw = await response.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event"], "delete")
        self.assertEqual(rows[0]["user_id"], "900000000000000301")
        self.assertEqual(rows[0]["message_id"], "900000000000000999")
        self.assertTrue(rows[0]["text"].startswith("'@sneaky"))  # a spreadsheet won't run it
        self.assertTrue(rows[0]["full_text"].startswith("'+full"))
        self.assertIn("emoji stays", rows[1]["text"])
        self.assertNotIn("secret", raw.decode("utf-8-sig"))

    async def test_a_cell_never_starts_like_a_formula(self):
        for bad in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"):
            self.assertTrue(csv_safe(bad).startswith("'"), bad)
        self.assertEqual(csv_safe("plain"), "plain")
        self.assertEqual(csv_safe(None), "")

    async def test_json(self):
        await self.seed()
        client = await self.client(access="staff")
        response = await client.get(self.path("/logs/export"), params={"format": "json"})
        self.assertEqual(response.status, 200)
        rows = json.loads(await response.text())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["user_id"], "900000000000000301")  # text, so JavaScript can't round it
        self.assertEqual(rows[0]["full_text"], "+full text")
        self.assertEqual(rows[1]["embed"]["title"], "Role added")

    async def test_filters_apply_and_an_empty_result_is_still_a_file(self):
        await self.seed()
        client = await self.client(access="staff")
        rows = json.loads(await (await client.get(self.path("/logs/export"), params={"format": "json", "event": "role"})).text())
        self.assertEqual([row["event"] for row in rows], ["role"])
        self.assertEqual(json.loads(await (await client.get(self.path("/logs/export"), params={"format": "json", "q": "nothing like this"})).text()), [])

    async def test_a_big_export_arrives_whole(self):
        with mock.patch.object(logs_module, "EXPORT_CHUNK", 7):
            for number in range(30):
                self.logs._store_entry(self.guild.id, "delete", logs_module.make_embed(f"entry {number}"), at=1000.0)  # every row in the same second
            await self.logs.flush()
            client = await self.client(access="staff")
            rows = json.loads(await (await client.get(self.path("/logs/export"), params={"format": "json"})).text())
        self.assertEqual(len({row["id"] for row in rows}), 30)

    async def test_bad_format_and_bad_filter(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/logs/export"), params={"format": "xml"})).status, 400)
        self.assertEqual((await client.get(self.path("/logs/export"), params={"user": "x"})).status, 400)

    async def test_who_exported_goes_in_the_activity_log(self):
        client = await self.client(access="staff")
        await client.get(self.path("/logs/export"))
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "logs.export")


class SettingsEndpointTest(Base):
    async def test_saving_from_the_page(self):
        client = await self.client()
        form = {"default_channel": str(self.channel.id), "events": ["delete", "threadcreate"], "ignored_members": [str(self.member.id)], "rename_ignored_bots": [str(self.me.id)], "log_bot_actions": False, "message_retention_days": 14}
        response = await client.put(self.path("/settings/logging"), json=form)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["default_channel"], str(self.channel.id))
        cfg = self.settings("logging")
        self.assertEqual(cfg["default_channel"], self.channel.id)
        self.assertEqual(cfg["rename_ignored_bots"], [self.me.id])
        self.assertEqual(cfg["message_retention_days"], 14)

    async def test_bad_values_are_refused_per_field(self):
        client = await self.client()
        for field, value in (("default_channel", "nope"), ("events", ["nonsense"]), ("message_retention_days", 0), ("log_retention_days", 9999), ("ignored_prefixes", [""]), ("new_account_days", -1), ("log_bot_actions", "yes")):
            response = await client.put(self.path("/settings/logging"), json={field: value})
            self.assertEqual(response.status, 400, field)
            self.assertIn(field, (await response.json())["fields"])

    async def test_staff_read_but_cant_write(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/logging/catalog"))).status, 200)
        self.assertEqual((await client.get(self.path("/settings/logging"))).status, 200)
        self.assertEqual((await client.put(self.path("/settings/logging"), json={"log_bot_actions": False})).status, 403)
        self.assertEqual((await client.get(self.path("/logging/bots"))).status, 403)
        self.assertEqual((await client.post(self.path("/logging/aio"))).status, 403)
        self.assertEqual((await client.post(self.path("/logging/import"), json={"code": "1"})).status, 403)
        self.assertEqual((await client.get(self.path("/logging/export"))).status, 403)

    async def test_the_catalog_lists_every_event_once(self):
        client = await self.client(access="staff")
        body = await (await client.get(self.path("/logging/catalog"))).json()
        keys = [event["key"] for group in body["groups"] for event in group["events"]]
        self.assertEqual(sorted(keys), sorted(EVENT_KEYS))
        self.assertEqual(len(keys), len(set(keys)))
        for wanted in ("threadcreate", "invitecreate", "stickers", "webhooks", "scheduledevents", "automodaction"):
            self.assertIn(wanted, keys)

    async def test_bots_list(self):
        client = await self.client()
        rows = await (await client.get(self.path("/logging/bots"))).json()
        self.assertTrue(rows)
        self.assertTrue(all(row["bot"] for row in rows))

    async def test_create_channels_from_the_page(self):
        client = await self.client()
        response = await client.post(self.path("/logging/aio"))
        self.assertEqual(response.status, 200)
        cfg = self.settings("logging")
        self.assertIsNotNone(cfg["voice_channel"])
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "log.aio")

    async def test_create_channels_refuses_when_all_are_set(self):
        made = {field: FakeChannel(self.guild, field) for field in ("message_channel", "member_channel", "join_channel", "server_channel", "voice_channel")}
        await self.configure(**{field: channel.id for field, channel in made.items()})
        self.bot.ops.describe_guild = lambda guild_id: {"channels": [{"id": channel.id} for channel in made.values()], "roles": []}
        client = await self.client()
        self.assertEqual((await client.post(self.path("/logging/aio"))).status, 409)

    async def test_import_from_the_page(self):
        self.bot.ops.describe_guild = lambda guild_id: {"channels": [{"id": self.channel.id}], "roles": []}
        client = await self.client()
        foreign = new_id()
        code = export_code({"message_channel": self.channel.id, "default_channel": foreign, "events": ["edit"]})
        response = await client.post(self.path("/logging/import"), json={"code": code})
        self.assertEqual(response.status, 200)
        cfg = self.settings("logging")
        self.assertEqual(cfg["message_channel"], self.channel.id)
        self.assertIsNone(cfg["default_channel"])
        self.assertEqual(cfg["events"], ["edit"])
        for bad in ({"code": "garbage!"}, {"code": 5}, {}, ["x"]):
            self.assertEqual((await client.post(self.path("/logging/import"), json=bad)).status, 400, bad)
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "log.import")

    async def test_export_code_from_the_page(self):
        await self.configure(ignored_prefixes=["!"])
        client = await self.client()
        code = (await (await client.get(self.path("/logging/export"))).json())["code"]
        self.assertEqual(settings_from_import(read_import(code), Logs.schema, set(), set())["ignored_prefixes"], ["!"])


class TwoDatabasesTest(Base):
    async def test_a_transaction_on_the_main_database_leaves_the_store_alone(self):
        await self.db.migrate("two", ["CREATE TABLE two (x INTEGER);"])
        message = await self.send("written from inside the other database's transaction")
        async with self.db.transaction():
            await self.db.run("INSERT INTO two VALUES (1)")
            rows = await self.logs.user_messages(self.guild.id, self.member.id)
            self.assertEqual(rows[0]["id"], message.id)
            self.assertFalse(self.message_db.conn.in_transaction)  # the store's own write committed
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM two"), 1)
        await self.send("the next batch still writes")
        await self.logs.flush()
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM messages"), 2)

    async def test_writes_inside_a_transaction_still_join_it_and_roll_back(self):
        await self.message_db.migrate("three", ["CREATE TABLE three (x INTEGER);"])
        with self.assertRaises(RuntimeError):
            async with self.message_db.transaction():
                await self.message_db.run("INSERT INTO three VALUES (1)")
                raise RuntimeError("no")
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM three"), 0)

    async def test_a_failed_write_doesnt_block_the_next_transaction(self):
        await self.message_db.migrate("four", ["CREATE TABLE four (x INTEGER UNIQUE);"])
        await self.message_db.run("INSERT INTO four VALUES (1)")
        with self.assertRaises(Exception):
            await self.message_db.run("INSERT INTO four VALUES (1)")
        async with self.message_db.transaction():
            await self.message_db.run("INSERT INTO four VALUES (2)")
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM four"), 2)

    async def test_a_canceled_transaction_leaves_nothing_open(self):
        await self.message_db.migrate("five", ["CREATE TABLE five (x INTEGER);"])

        async def writes():
            async with self.message_db.transaction():
                await self.message_db.run("INSERT INTO five VALUES (1)")

        for _ in range(5):
            task = asyncio.ensure_future(writes())
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.message_db.transaction():
            await self.message_db.run("INSERT INTO five VALUES (2)")
        self.assertGreaterEqual(await self.message_db.scalar("SELECT COUNT(*) FROM five WHERE x = 2"), 1)


class OldSettingsTest(BotTest):
    modules = [Logs]

    def test_every_bit_on_the_old_page_has_an_event(self):
        for bit in range(27):
            # The old setup's one delete switch covered what are two events here.
            self.assertEqual(len(logs_module.events_from_bits(1 << bit)), 2 if bit == 0 else 1, bit)

    def test_the_named_sets_add_up_to_the_old_numbers(self):
        bits = logs_module.EVENT_BITS
        self.assertEqual(sum(bits[key] for key in ("delete", "edit", "role", "name", "avatar", "ban", "unban", "join", "leave")), 1019)
        self.assertEqual(sum(bits[key] for key in logs_module.EVENT_SETS["bans"]), 192)
        self.assertEqual(sum(bits[key] for key in logs_module.EVENT_SETS["channels"]), 7168)
        self.assertEqual(sum(bits[key] for key in logs_module.EVENT_SETS["allroles"]), 458752)
        self.assertEqual(sum(bits[key] for key in ("voicejoin", "voicemove", "voiceleave")), 57344)


class EmbedLimitTest(BotTest):
    modules = [Logs]

    def test_nothing_fails_discords_limits(self):
        huge = "x" * 5000
        embed = logs_module.make_embed(huge, description=huge, footer=huge, fields=[logs_module.make_field(huge, huge) for _ in range(40)])
        self.assertLessEqual(len(embed["title"]), 256)
        self.assertLessEqual(len(embed["description"]), 4096)
        self.assertLessEqual(len(embed["footer"]["text"]), 2048)
        self.assertLessEqual(len(embed["fields"]), 25)
        self.assertLessEqual(logs_module.embed_size(embed), 6000)
        self.assertEqual(embed["fields"][-1]["name"], "More")

    def test_empty_values_become_something_discord_accepts(self):
        field = logs_module.make_field("", "")
        self.assertTrue(field["name"])
        self.assertTrue(field["value"])

    def test_before_and_after_share_the_room(self):
        short, long = "a" * 10, "b" * 8000
        fields, cut = logs_module.before_after(short, long)
        self.assertTrue(cut)
        self.assertEqual(fields[0]["value"], short)
        self.assertLessEqual(len(fields[1]["value"]), 1024)
        fields, cut = logs_module.before_after("", "x")
        self.assertFalse(cut)
        self.assertEqual(fields[0]["value"], "(no text)")


class RealLibraryTest(GatewayTest):
    """The same listeners fed gateway JSON through the real library, so wrong attribute names and keyword arguments fail here."""

    modules = [Logs]

    async def asyncSetUp(self):
        self.quick = mock.patch.multiple(logs_module, **QUICK)
        self.quick.start()
        await super().asyncSetUp()
        self.logs = next(m for m in self.bot.modules if isinstance(m, Logs))
        self.state = self.bot._connection
        self.logs_id = int(self.log_channel["id"])
        await self.bot.settings.save(self.guild_id, "logging", {"default_channel": self.logs_id, "events": list(EVENT_KEYS)})
        self.uploads = []
        self.audit_serial = 0
        original = self.net.webhook

        async def webhook(adapter, route, session=None, **kwargs):
            if kwargs.get("files"):
                self.uploads.extend(file.filename for file in kwargs["files"])
            return await original(adapter, route, session, **kwargs)

        self.net.webhook = webhook

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.quick.stop()

    async def drained(self):
        await self.settle()
        for _ in range(200):
            workers = [worker for worker in self.bot.logs._workers.values() if not worker.done()]
            if not workers:
                break
            await asyncio.sleep(0.005)
        self.assert_no_errors()
        return [embed for message in self.net.posted for embed in message.get("embeds", [])]

    def titles(self, embeds):
        return [embed["title"] for embed in embeds]

    def audit_json(self, action, *, target=None, user=None, changes=None, options=None, reason=None):
        # An entry's ID holds when it was made, and the code reads that to tell a new entry from an old one.
        self.audit_serial += 1
        data = {"id": str(discord.utils.time_snowflake(discord.utils.utcnow()) + self.audit_serial), "guild_id": str(self.guild_id), "action_type": action.value, "user_id": str(user or self.mod_id), "target_id": str(target) if target else None, "changes": changes or []}
        if options:
            data["options"] = options
        if reason:
            data["reason"] = reason
        return data

    def serve_audit_log(self, *entries):
        """What GET audit-logs answers, as Discord shapes it."""
        original = self.net.http

        async def http(route, **kwargs):
            if route.path == "/guilds/{guild_id}/audit-logs":
                wanted = kwargs.get("params", {}).get("action_type")
                rows = [e for e in entries if wanted is None or int(wanted) == e["action_type"]]
                users = [user_json(self.mod_id, "mod"), user_json(self.member_id, "member")]
                return {"audit_log_entries": rows, "users": users, "webhooks": [], "threads": [], "integrations": [], "auto_moderation_rules": [], "application_commands": [], "guild_scheduled_events": []}
            return await original(route, **kwargs)

        self.bot.http.request = http

    async def test_store_edit_delete_and_purge(self):
        message = self.dispatch_message(user_id=self.member_id, content="first words", attachments=[{"id": str(gateway_id()), "filename": "a.png", "size": 10, "url": "https://cdn.discordapp.com/attachments/1/2/a.png", "proxy_url": "https://media.discordapp.net/attachments/1/2/a.png", "content_type": "image/png"}], type=19, message_reference={"message_id": "12345", "channel_id": self.text["id"], "guild_id": str(self.guild_id)})
        await self.settle()
        row = await self.logs.stored_message(int(message["id"]))
        self.assertEqual(row["content"], "first words")
        self.assertEqual(row["reply_to"], 12345)
        self.assertEqual(row["attachments"][0]["filename"], "a.png")

        self.state.parse_message_update({**message, "content": "second words", "edited_timestamp": iso()})
        self.state.parse_message_update({**message, "content": "second words", "edited_timestamp": iso(), "embeds": [{"type": "link", "url": "https://example.com"}]})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Message edited in #general"])
        self.assertEqual(field_value(embeds[0], "Before"), "first words")
        self.assertEqual(field_value(embeds[0], "After"), "second words")
        self.assertEqual(embeds[0]["color"], 0x4286F4)

        self.bot.http.get_from_cdn = mock.AsyncMock(return_value=b"\x89PNG bytes")
        self.state.parse_message_delete({"id": message["id"], "channel_id": self.text["id"], "guild_id": str(self.guild_id)})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Message deleted in #general")
        self.assertEqual(embeds[-1]["image"]["url"], "attachment://a.png")
        self.assertIn("a.png", self.uploads)
        self.assertEqual(await self.logs.message_db.scalar("SELECT COUNT(*) FROM messages WHERE deleted_at IS NOT NULL"), 1)

        other = self.dispatch_message(user_id=self.member_id, content="purge me")
        await self.settle()
        self.state.parse_message_delete_bulk({"ids": [other["id"], str(gateway_id())], "channel_id": self.text["id"], "guild_id": str(self.guild_id)})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Messages purged in #general")
        self.assertIn("purged-messages.txt", self.uploads)

    async def test_who_deleted_through_the_real_audit_log_reader(self):
        first = self.dispatch_message(user_id=self.member_id, content="one")
        second = self.dispatch_message(user_id=self.member_id, content="two")
        third = self.dispatch_message(user_id=self.member_id, content="three")
        await self.settle()
        entry = self.audit_json(Actions.message_delete, target=self.member_id, options={"channel_id": self.text["id"], "count": "1"})
        self.serve_audit_log(entry)
        self.state.parse_message_delete({"id": first["id"], "channel_id": self.text["id"], "guild_id": str(self.guild_id)})
        embeds = await self.drained()
        self.assertEqual(field_value(embeds[-1], "Deleted by"), f"<@{self.mod_id}> (mod)")
        entry["options"]["count"] = "2"
        self.state.parse_message_delete({"id": second["id"], "channel_id": self.text["id"], "guild_id": str(self.guild_id)})
        embeds = await self.drained()
        self.assertEqual(field_value(embeds[-1], "Deleted by"), f"<@{self.mod_id}> (mod)")
        self.state.parse_message_delete({"id": third["id"], "channel_id": self.text["id"], "guild_id": str(self.guild_id)})
        embeds = await self.drained()
        self.assertEqual(field_value(embeds[-1], "Deleted by"), NOBODY)

    async def test_a_gateway_audit_entry_names_who_changed_a_role(self):
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.member_role_update, target=self.member_id, changes=[{"key": "$add", "new_value": [{"id": self.role_mod["id"], "name": "Moderator"}]}], reason="trusted"))
        await self.settle()
        member = self.member_json[self.member_id]
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), "user": member["user"], "roles": [self.role_member["id"], self.role_mod["id"]], "nick": "New Nick", "joined_at": member["joined_at"], "deaf": False, "mute": False, "flags": 0, "avatar": None, "premium_since": None, "pending": False, "communication_disabled_until": None})
        embeds = await self.drained()
        self.assertEqual(sorted(self.titles(embeds)), ["Nickname added", "Role added"])
        role_entry = next(embed for embed in embeds if embed["title"] == "Role added")
        self.assertIn(f"**By:** <@{self.mod_id}> (mod)", role_entry["description"])
        self.assertIn("**Reason:** trusted", role_entry["description"])

    async def test_timeouts_and_user_updates(self):
        member = self.member_json[self.member_id]
        later = (discord.utils.utcnow() + datetime.timedelta(hours=1)).isoformat()
        base = {"guild_id": str(self.guild_id), "user": member["user"], "roles": member["roles"], "nick": None, "joined_at": member["joined_at"], "deaf": False, "mute": False, "flags": 0, "avatar": None, "premium_since": None, "pending": False}
        self.state.parse_guild_member_update({**base, "communication_disabled_until": later})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Member timed out"])
        self.state.parse_guild_member_update({**base, "communication_disabled_until": None})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Timeout removed")
        self.state.parse_guild_member_update({**base, "user": {**member["user"], "username": "renamed", "global_name": "Renamed", "avatar": "a" * 32}, "communication_disabled_until": None})
        embeds = await self.drained()
        self.assertEqual(set(self.titles(embeds)[-3:]), {"Avatar update", "Name change", "Display name change"})
        self.assertTrue(next(e for e in embeds if e["title"] == "Avatar update")["thumbnail"]["url"].startswith("https://"))

    async def test_join_leave_ban_and_kick(self):
        newcomer = gateway_id()
        self.state.parse_guild_member_add({"guild_id": str(self.guild_id), "user": user_json(newcomer, "newcomer"), "roles": [self.role_member["id"]], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "avatar": None, "premium_since": None, "communication_disabled_until": None})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Member joined"])
        self.assertIn("6th to join", embeds[0]["description"])
        self.state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": user_json(newcomer, "newcomer")})
        self.state.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": user_json(gateway_id(), "never_cached")})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[1:], ["Member left", "Member left"])
        self.assertIn(self.role_member["id"], embeds[1]["description"])
        banned = gateway_id()
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.ban, target=banned, reason="spam"))
        await self.settle()
        self.state.parse_guild_ban_add({"guild_id": str(self.guild_id), "user": user_json(banned, "banned")})
        self.state.parse_guild_ban_remove({"guild_id": str(self.guild_id), "user": user_json(banned, "banned")})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[3:], ["Member banned", "Member unbanned"])
        self.assertIn("**Reason:** spam", embeds[3]["description"])
        kicked = gateway_id()
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.kick, target=kicked))
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Member kicked")
        self.assertIn(f"<@{kicked}> (someone)", embeds[-1]["description"])

    async def test_channels_with_their_overrides(self):
        new_id_ = gateway_id()
        override = {"id": self.role_mod["id"], "type": 0, "allow": str(discord.Permissions(send_messages=True).value), "deny": str(discord.Permissions(view_channel=True).value)}
        payload = {"id": str(new_id_), "guild_id": str(self.guild_id), "name": "brand-new", "type": 0, "position": 9, "permission_overwrites": [override], "nsfw": False, "parent_id": self.category["id"], "topic": None, "rate_limit_per_user": 0, "flags": 0, "last_message_id": None}
        self.state.parse_channel_create(payload)
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Text channel created"])
        self.assertIn("**Category:** Community", embeds[0]["description"])
        field = embeds[0]["fields"][0]
        self.assertEqual(field["name"], "Role override for Moderator")
        self.assertIn("**View channel:** \N{CROSS MARK}", field["value"])
        self.assertIn("**Send messages:** \N{WHITE HEAVY CHECK MARK}", field["value"])

        self.state.parse_channel_update({**payload, "name": "renamed", "topic": "new topic", "rate_limit_per_user": 30})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Text channel updated")
        before, after = embeds[-1]["fields"][0], embeds[-1]["fields"][1]
        self.assertEqual((before["name"], after["name"]), ("Before", "After"))
        self.assertIn("**Name:** brand-new", before["value"])
        self.assertIn("**Topic:** new topic", after["value"])
        self.assertIn("**Slowmode:** 30 seconds", after["value"])

        changed = {**override, "deny": "0"}
        self.state.parse_channel_update({**payload, "name": "renamed", "topic": "new topic", "rate_limit_per_user": 30, "permission_overwrites": [changed]})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Text channel updated")
        self.assertIn("**View channel:** \N{CROSS MARK} \N{RIGHTWARDS ARROW} \N{HEAVY MINUS SIGN}", embeds[-1]["fields"][0]["value"])

        self.state.parse_channel_update({**payload, "name": "renamed", "topic": "new topic", "rate_limit_per_user": 30, "permission_overwrites": [changed], "position": 44})
        self.assertEqual(len(await self.drained()), len(embeds))

        self.state.parse_channel_update({**self.voice, "name": "Members: 2"})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Voice channel updated")

        self.state.parse_channel_create({"id": str(gateway_id()), "guild_id": str(self.guild_id), "name": "New Category", "type": 4, "position": 1, "permission_overwrites": [], "nsfw": False, "parent_id": None, "flags": 0})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Category created")

        self.state.parse_channel_delete(payload)
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Text channel deleted")

    async def test_a_stat_counter_rename_through_the_real_library(self):
        counter = gateway_id()
        await self.bot.settings.save(self.guild_id, "logging", {"rename_ignored_bots": [counter]})
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.channel_update, target=self.voice["id"], user=counter, changes=[{"key": "name", "old_value": "Lounge", "new_value": "Members: 5"}]))
        await self.settle()
        self.state.parse_channel_update({**self.voice, "name": "Members: 5"})
        self.assertEqual(await self.drained(), [])

    async def test_threads(self):
        thread = {"id": str(gateway_id()), "guild_id": str(self.guild_id), "parent_id": self.text["id"], "owner_id": str(self.member_id), "name": "a thread", "type": 11, "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "thread_metadata": {"archived": False, "auto_archive_duration": 1440, "archive_timestamp": iso(), "locked": False}, "newly_created": True, "flags": 0}
        self.state.parse_thread_create(thread)
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Thread created"])
        self.assertEqual(embeds[0]["author"]["name"], "member")
        self.assertIn(f"<#{self.text['id']}>", embeds[0]["description"])
        await self.logs.flush()
        row = await self.logs.message_db.one("SELECT channel_id, parent_id FROM log_entries WHERE event = 'threadcreate'")
        self.assertEqual(row, {"channel_id": int(thread["id"]), "parent_id": int(self.text["id"])})

        self.state.parse_thread_update({**thread, "name": "renamed thread", "thread_metadata": {**thread["thread_metadata"], "locked": True}})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Thread updated")
        self.assertIn("**Name:** renamed thread", embeds[-1]["fields"][1]["value"])
        count = len(embeds)

        # Discord archiving a quiet thread by itself: no audit entry, so no log entry.
        self.state.parse_thread_update({**thread, "name": "renamed thread", "thread_metadata": {**thread["thread_metadata"], "locked": True, "archived": True}})
        self.assertEqual(len(await self.drained()), count)

        # Reopened by a person: it isn't in the cache anymore, and the audit log says who.
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.thread_update, target=thread["id"]))
        await self.settle()
        self.state.parse_thread_update({**thread, "name": "renamed thread", "thread_metadata": {**thread["thread_metadata"], "locked": True}})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Thread updated")

        self.state.parse_thread_delete({"id": thread["id"], "guild_id": str(self.guild_id), "parent_id": self.text["id"], "type": 11})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Thread deleted")

    async def test_roles(self):
        role = {"id": str(gateway_id()), "name": "new", "colors": {"primary_color": 0x112233, "secondary_color": None, "tertiary_color": None}, "hoist": True, "position": 1, "permissions": "0", "managed": False, "mentionable": False}
        self.state.parse_guild_role_create({"guild_id": str(self.guild_id), "role": role})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["New role created"])
        self.assertEqual(embeds[0]["description"], "**Name:** new\n**Color:** #112233\n**Mentionable:** False\n**Displayed separately:** True")
        self.state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": {**role, "name": "renamed", "permissions": str(discord.Permissions(ban_members=True).value)}})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], 'Role "renamed" updated')
        self.assertEqual(embeds[-1]["fields"][-1], {"name": "Permissions added", "value": "Ban members", "inline": False})
        self.state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": {**role, "name": "renamed", "permissions": str(discord.Permissions(ban_members=True).value), "position": 3}})
        self.assertEqual(len(await self.drained()), len(embeds))
        self.state.parse_guild_role_delete({"guild_id": str(self.guild_id), "role_id": self.role_member["id"]})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], 'Role "Member" removed')
        self.assertIn(f"<@{self.member_id}>", embeds[-1]["description"])
        self.assertIn("**Members who had it:** 1", embeds[-1]["description"])

    async def test_server_emoji_stickers_and_sounds(self):
        self.state.parse_guild_update({"id": str(self.guild_id), "name": "Renamed Guild", "owner_id": str(self.owner_id), "features": [], "roles": list(self.net.roles.values()), "emojis": [], "stickers": [], "verification_level": 1, "default_message_notifications": 0, "explicit_content_filter": 0, "mfa_level": 0, "premium_tier": 0, "system_channel_flags": 0, "preferred_locale": "en-US", "nsfw_level": 0, "premium_progress_bar_enabled": False})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Server updated"])
        self.assertIn("**Name:** Renamed Guild", embeds[0]["fields"][1]["value"])
        self.assertIn("**Verification level:**", embeds[0]["fields"][1]["value"])

        emoji = {"id": str(gateway_id()), "name": "party", "animated": False, "available": True, "managed": False, "require_colons": True, "roles": []}
        self.state.parse_guild_emojis_update({"guild_id": str(self.guild_id), "emojis": [emoji]})
        self.state.parse_guild_emojis_update({"guild_id": str(self.guild_id), "emojis": [{**emoji, "name": "fiesta"}]})
        self.state.parse_guild_emojis_update({"guild_id": str(self.guild_id), "emojis": []})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-3:], ["Emoji added", "Emoji renamed", "Emoji removed"])
        self.assertIn("/emojis/", embeds[-3]["thumbnail"]["url"])

        sticker = {"id": str(gateway_id()), "name": "wave", "description": "hi", "tags": "wave", "type": 2, "format_type": 1, "available": True, "guild_id": str(self.guild_id)}
        self.state.parse_guild_stickers_update({"guild_id": str(self.guild_id), "stickers": [sticker]})
        self.state.parse_guild_stickers_update({"guild_id": str(self.guild_id), "stickers": []})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-2:], ["Sticker added", "Sticker removed"])

        sound_id = str(gateway_id())
        sound = {"sound_id": sound_id, "name": "boing", "volume": 1.0, "emoji_id": None, "emoji_name": None, "guild_id": str(self.guild_id), "available": True, "user_id": str(self.mod_id)}
        self.state.parse_guild_soundboard_sound_create(sound)
        self.state.parse_guild_soundboard_sound_update({**sound, "name": "bonk"})
        self.state.parse_guild_soundboard_sound_delete({"sound_id": sound_id, "guild_id": str(self.guild_id)})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-3:], ["Sound added", "Sound renamed", "Sound removed"])
        self.assertNotIn("thumbnail", embeds[-3])

    async def test_invites_and_webhooks(self):
        self.state.parse_invite_create({"channel_id": self.text["id"], "code": "abc", "created_at": iso(), "guild_id": str(self.guild_id), "max_age": 3600, "max_uses": 5, "temporary": True, "uses": 0, "inviter": user_json(self.member_id, "member")})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Invite created"])
        self.assertIn("discord.gg/abc", embeds[0]["description"])
        self.assertIn("in 1 hour", embeds[0]["description"])
        self.assertEqual(embeds[0]["author"]["name"], "member")
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.invite_delete, changes=[{"key": "code", "old_value": "abc"}, {"key": "channel_id", "old_value": self.text["id"]}, {"key": "inviter_id", "old_value": str(self.member_id)}, {"key": "uses", "old_value": 0}, {"key": "max_uses", "old_value": 5}, {"key": "max_age", "old_value": 3600}, {"key": "temporary", "old_value": True}]))
        await self.settle()
        self.state.parse_invite_delete({"channel_id": self.text["id"], "code": "abc", "guild_id": str(self.guild_id)})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Invite deleted")
        self.assertIn("(mod)", embeds[-1]["description"])

        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.webhook_create, target=gateway_id(), changes=[{"key": "name", "new_value": "alerts"}, {"key": "channel_id", "new_value": self.text["id"]}, {"key": "type", "new_value": 1}]))
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Webhook created")
        self.assertIn("**Name:** alerts", embeds[-1]["description"])
        self.assertIn(self.text["id"], embeds[-1]["description"])
        before = len(embeds)
        self.state.parse_webhooks_update({"guild_id": str(self.guild_id), "channel_id": self.text["id"]})
        self.assertEqual(len(await self.drained()), before)  # the audit entry already said it

    async def test_pins_prunes_bots_integrations_and_voice_actions(self):
        message = self.dispatch_message(user_id=self.member_id, content="pin this")
        await self.settle()
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.message_pin, target=self.member_id, options={"channel_id": self.text["id"], "message_id": message["id"]}))
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.member_prune, options={"delete_member_days": "7", "members_removed": "12"}))
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.bot_add, target=gateway_id()))
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.integration_create, target=gateway_id(), changes=[{"key": "name", "new_value": "Some App"}, {"key": "type", "new_value": "discord"}]))
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.member_move, options={"channel_id": self.voice["id"], "count": "2"}))
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.member_disconnect, options={"count": "1"}))
        self.state.parse_guild_audit_log_entry_create(self.audit_json(Actions.onboarding_update))
        embeds = await self.drained()
        self.assertEqual(sorted(self.titles(embeds)), sorted(["Message pinned in #general", "Members pruned", "Bot added", "Integration added", "Moderator voice action", "Moderator voice action", "Server onboarding changed"]))
        pinned = next(e for e in embeds if e["title"].startswith("Message pinned"))
        self.assertIn("pin this", pinned["description"])
        pruned = next(e for e in embeds if e["title"] == "Members pruned")
        self.assertIn("**Removed:** 12", pruned["description"])

    async def test_scheduled_events_stages_and_automod(self):
        event = {"id": str(gateway_id()), "guild_id": str(self.guild_id), "channel_id": None, "creator_id": str(self.mod_id), "name": "Print night", "description": "Bring filament", "scheduled_start_time": iso(), "scheduled_end_time": iso(), "privacy_level": 2, "status": 1, "entity_type": 3, "entity_id": None, "entity_metadata": {"location": "Online"}, "user_count": 0}
        self.state.parse_guild_scheduled_event_create(event)
        self.state.parse_guild_scheduled_event_update({**event, "name": "Print evening"})
        self.state.parse_guild_scheduled_event_delete({**event, "name": "Print evening"})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Scheduled event created", "Scheduled event updated", "Scheduled event deleted"])
        self.assertIn("**Where:** Online", embeds[0]["description"])
        self.assertIn("**Name:** Print evening", embeds[1]["fields"][1]["value"])

        stage = {"id": str(gateway_id()), "guild_id": str(self.guild_id), "channel_id": self.voice["id"], "topic": "Q and A", "privacy_level": 2, "discoverable_disabled": True, "guild_scheduled_event_id": None}
        self.state.parse_stage_instance_create(stage)
        self.state.parse_stage_instance_update({**stage, "topic": "Show and tell"})
        self.state.parse_stage_instance_delete({**stage, "topic": "Show and tell"})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-3:], ["Stage started", "Stage topic changed", "Stage ended"])

        rule = {"id": str(gateway_id()), "guild_id": str(self.guild_id), "name": "Bad words", "creator_id": str(self.mod_id), "event_type": 1, "trigger_type": 1, "trigger_metadata": {"keyword_filter": ["x"], "regex_patterns": [], "allow_list": []}, "actions": [{"type": 1, "metadata": {}}], "enabled": True, "exempt_roles": [], "exempt_channels": []}
        self.state.parse_auto_moderation_rule_create(rule)
        self.state.parse_auto_moderation_rule_update({**rule, "name": "Worse words"})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-2:], ["Automod rule created", "Automod rule updated"])
        self.assertIn("**Name:** Bad words", embeds[-2]["description"])
        self.assertIn("**Watches for:** keyword", embeds[-2]["description"])

        execution = {"guild_id": str(self.guild_id), "rule_id": rule["id"], "rule_trigger_type": 1, "user_id": str(self.member_id), "channel_id": self.text["id"], "message_id": str(gateway_id()), "alert_system_message_id": None, "content": "a bad word here", "matched_keyword": "bad", "matched_content": "bad"}
        with mock.patch.object(logs_module, "AUTOMOD_GATHER", 0.05):
            self.state.parse_auto_moderation_action_execution({**execution, "action": {"type": 1, "metadata": {}}})
            self.state.parse_auto_moderation_action_execution({**execution, "action": {"type": 2, "metadata": {"channel_id": self.log_channel["id"]}}})
            self.state.parse_auto_moderation_action_execution({**execution, "action": {"type": 3, "metadata": {"duration_seconds": 60}}})
            embeds = await self.drained()
        automod = [e for e in embeds if e["title"].startswith("Automod blocked")]
        self.assertEqual(len(automod), 1)
        self.assertEqual(automod[0]["description"], "a bad word here")
        self.assertEqual(field_value(automod[0], "Rule"), "Worse words")
        self.assertEqual(field_value(automod[0], "Matched"), "bad")
        self.assertIn("blocked the message", field_value(automod[0], "Actions"))
        self.assertIn("sent an alert", field_value(automod[0], "Actions"))
        self.assertIn("timed out for 1 minute", field_value(automod[0], "Actions"))

        self.logs._automod_rules.clear()
        self.state.parse_auto_moderation_action_execution({**execution, "content": "alert only", "action": {"type": 2, "metadata": {"channel_id": self.log_channel["id"]}}})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Automod flagged a message in #general")
        self.assertEqual(field_value(embeds[-1], "Rule"), "Blocked words")

        self.state.parse_auto_moderation_rule_delete(rule)
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds)[-1], "Automod rule deleted")

    async def test_voice(self):
        member = self.member_json[self.member_id]
        state = {"guild_id": str(self.guild_id), "user_id": str(self.member_id), "member": member, "session_id": "s", "deaf": False, "mute": False, "self_deaf": False, "self_mute": False, "self_video": False, "suppress": False, "request_to_speak_timestamp": None}
        other = {"id": str(gateway_id()), "guild_id": str(self.guild_id), "name": "Gaming", "type": 2, "position": 8, "permission_overwrites": [], "parent_id": None, "nsfw": False, "bitrate": 64000, "user_limit": 0, "rtc_region": None, "flags": 0}
        self.state.parse_channel_create(other)
        await self.drained()
        self.net.posted.clear()
        self.state.parse_voice_state_update({**state, "channel_id": self.voice["id"]})
        await self.settle()
        self.state.parse_voice_state_update({**state, "channel_id": other["id"]})
        await self.settle()
        self.state.parse_voice_state_update({**state, "channel_id": other["id"], "mute": True})
        await self.settle()
        self.state.parse_voice_state_update({**state, "channel_id": None, "mute": True})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Member joined voice channel", "Member changed voice channel", "Moderator voice action", "Member left voice channel"])
        self.assertEqual(embeds[0]["description"], "**member** joined #Lounge")
        self.assertEqual(embeds[1]["description"], "**Before:** #Lounge\n**+After:** #Gaming")
        self.assertIn("server muted", embeds[2]["description"])

    async def test_every_audit_action_with_a_route_runs_on_real_payloads(self):
        target = gateway_id()
        payloads = {
            Actions.kick: {"target": target},
            Actions.member_prune: {"options": {"delete_member_days": "7", "members_removed": "1"}},
            Actions.bot_add: {"target": target},
            Actions.integration_create: {"target": target, "changes": [{"key": "name", "new_value": "App"}, {"key": "type", "new_value": "discord"}]},
            Actions.integration_update: {"target": target, "changes": [{"key": "enable_emoticons", "old_value": False, "new_value": True}]},
            Actions.integration_delete: {"target": target, "changes": [{"key": "name", "old_value": "App"}, {"key": "type", "old_value": "discord"}]},
            Actions.app_command_permission_update: {"target": target, "options": {"application_id": str(self.bot_id)}},
            Actions.webhook_create: {"target": target, "changes": [{"key": "name", "new_value": "hook"}, {"key": "channel_id", "new_value": self.text["id"]}]},
            Actions.webhook_update: {"target": target, "changes": [{"key": "name", "old_value": "hook", "new_value": "hook2"}]},
            Actions.webhook_delete: {"target": target, "changes": [{"key": "name", "old_value": "hook2"}, {"key": "channel_id", "old_value": self.text["id"]}]},
            Actions.message_pin: {"target": self.member_id, "options": {"channel_id": self.text["id"], "message_id": str(gateway_id())}},
            Actions.message_unpin: {"target": self.member_id, "options": {"channel_id": self.text["id"], "message_id": str(gateway_id())}},
            Actions.member_move: {"options": {"channel_id": self.voice["id"], "count": "1"}},
            Actions.member_disconnect: {"options": {"count": "3"}},
            Actions.onboarding_create: {},
            Actions.onboarding_update: {},
            Actions.onboarding_prompt_create: {"target": target},
            Actions.onboarding_prompt_update: {"target": target},
            Actions.onboarding_prompt_delete: {"target": target},
            Actions.home_settings_create: {},
            Actions.home_settings_update: {},
        }
        self.assertEqual(set(payloads), set(self.logs._audit_routes))  # a new route has to come with a real payload here
        for action, payload in payloads.items():
            with self.subTest(action=action.name):
                self.net.posted.clear()
                self.state.parse_guild_audit_log_entry_create(self.audit_json(action, **payload))
                embeds = await self.drained()
                self.assertEqual(len(embeds), 1, action.name)
                self.assertIn("(mod)", json.dumps(embeds[0]))

    async def test_autocomplete_answers_discord(self):
        data = {"id": str(gateway_id()), "name": "log", "type": 1, "options": [{"name": "config", "type": 1, "options": [{"name": "event", "type": 3, "value": "thread", "focused": True}]}]}
        self.bot._connection.parse_interaction_create(self.interaction_payload(4, data, user_id=self.owner_id))
        await self.settle()
        self.assert_no_errors()
        answers = [body for method, path, body in self.net.calls if path.endswith("/callback") and body.get("type") == 8]
        self.assertEqual({choice["value"] for choice in answers[0]["data"]["choices"]}, {"threadcreate", "threadupdate", "threaddelete", "threads"})

    async def test_a_forward_keeps_its_text_through_the_real_library(self):
        snapshot = {"message": {"type": 0, "content": "the original words", "embeds": [], "attachments": [], "timestamp": iso(), "edited_timestamp": None, "flags": 0, "mentions": [], "mention_roles": []}}
        message = self.dispatch_message(user_id=self.member_id, content="", message_reference={"type": 1, "message_id": "12345", "channel_id": "67890"}, message_snapshots=[snapshot])
        await self.settle()
        row = await self.logs.stored_message(int(message["id"]))
        self.assertEqual(row["content"], "[Forwarded] the original words")
        self.assertIsNone(row["reply_to"])  # a forward's reference points somewhere else entirely

    async def test_threads_follow_their_channel(self):
        thread = {"id": str(gateway_id()), "guild_id": str(self.guild_id), "parent_id": self.text["id"], "owner_id": str(self.member_id), "name": "a thread", "type": 11, "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "thread_metadata": {"archived": False, "auto_archive_duration": 1440, "archive_timestamp": iso(), "locked": False}, "newly_created": True, "flags": 0}
        self.state.parse_thread_create(thread)
        await self.drained()
        self.net.posted.clear()
        said = self.message_payload(user_id=self.member_id, content="in the thread", channel=thread)
        self.state.parse_message_create(said)
        await self.settle()
        self.state.parse_message_delete({"id": said["id"], "channel_id": thread["id"], "guild_id": str(self.guild_id)})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Message deleted in #a thread"])
        await self.logs.flush()
        row = await self.logs.message_db.one("SELECT channel_id, parent_id FROM log_entries WHERE event = 'delete'")
        self.assertEqual(row, {"channel_id": int(thread["id"]), "parent_id": int(self.text["id"])})
        await self.bot.settings.save(self.guild_id, "logging", {"ignored_channels": [int(self.text["id"])]})
        self.net.posted.clear()
        again = self.message_payload(user_id=self.member_id, content="ignored now", channel=thread)
        self.state.parse_message_create(again)
        await self.settle()
        self.state.parse_message_delete({"id": again["id"], "channel_id": thread["id"], "guild_id": str(self.guild_id)})
        self.assertEqual(await self.drained(), [])

    async def test_a_real_category_and_forum_update(self):
        await self.logs.flush()
        self.net.posted.clear()
        self.state.parse_channel_update({**self.forum, "available_tags": [{"id": str(gateway_id()), "name": "Solved", "moderated": False, "emoji_id": None, "emoji_name": None}]})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Forum channel updated"])
        self.assertIn("**Tags:** Solved", embeds[0]["fields"][1]["value"])
        self.net.posted.clear()
        self.state.parse_channel_update({**self.category, "name": "Renamed Community"})
        embeds = await self.drained()
        self.assertEqual(self.titles(embeds), ["Category updated"])

    async def test_a_role_icon_change_is_an_update(self):
        await self.logs.flush()
        self.net.posted.clear()
        self.state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": {**self.role_member, "unicode_emoji": "\N{ROCKET}"}})
        embeds = await self.drained()
        self.assertIn("**Icon:** \N{ROCKET}", embeds[0]["fields"][1]["value"])

    async def test_text_subcommands_dont_run_the_summary_first(self):
        posted = len(self.net.posted)
        self.dispatch_message(user_id=self.owner_id, content=f"!log channel <#{self.text['id']}>")
        await self.settle()
        self.assert_no_errors()
        replies = [m.get("content") or "" for m in self.net.posted[posted:]]
        self.assertEqual(len(replies), 1, replies)
        self.assertIn("default log channel", replies[0])
        self.assertEqual(self.bot.settings.get(self.guild_id, "logging")["default_channel"], int(self.text["id"]))
        posted = len(self.net.posted)
        self.dispatch_message(user_id=self.owner_id, content="!log")
        await self.settle()
        replies = [m.get("content") or "" for m in self.net.posted[posted:]]
        self.assertTrue(any("Default log channel" in reply for reply in replies), replies)

    async def test_a_webhook_failure_still_uploads_the_whole_file(self):
        original = self.net.webhook
        sizes = []

        async def webhook(adapter, route, session=None, **kwargs):
            if route.path == "/webhooks/{webhook_id}/{webhook_token}":
                for file in kwargs.get("files") or []:
                    file.fp.read()  # a real upload reads the file to its end before the refusal comes back
                raise discord.HTTPException(type("R", (), {"status": 500, "reason": "boom"})(), {"code": 0, "message": "boom"})
            return await original(adapter, route, session, **kwargs)

        async def http(route, *, files=None, form=None, **kwargs):
            if route.path == "/channels/{channel_id}/messages" and files:
                sizes.extend(len(file.fp.read()) for file in files)
            return await real_http(route, files=files, form=form, **kwargs)

        real_http = self.net.http
        self.net.webhook = webhook
        self.bot.http.request = http
        await self.bot.settings.save(self.guild_id, "logging", {"events": ["purge"]})
        message = self.dispatch_message(user_id=self.member_id, content="kept in the file")
        await self.settle()
        self.state.parse_message_delete_bulk({"ids": [message["id"]], "channel_id": self.text["id"], "guild_id": str(self.guild_id)})
        await self.drained()
        self.assertTrue(sizes and sizes[0] > 0, sizes)

    async def test_log_aio_through_the_real_library(self):
        self.bot.ops = DiscordOps(self.bot)
        channel_ids = {}
        original = self.net.http

        async def http(route, **kwargs):
            answer = await original(route, **kwargs)
            if route.path == "/guilds/{guild_id}/channels" and route.method == "POST":
                # The real gateway adds the channel to the cache before the bot uses it, and the library reads it from there.
                self.state.parse_channel_create(answer)
                channel_ids[answer["name"]] = answer["id"]
            return answer

        self.bot.http.request = http
        form = await self.logs._build_log_channels(self.guild_id, self.bot.actor(self.guild.get_member(self.owner_id)))
        self.assertEqual(set(form), {"message_channel", "member_channel", "join_channel", "server_channel", "voice_channel"})
        self.assertEqual(self.bot.settings.get(self.guild_id, "logging")["voice_channel"], int(channel_ids["voice-log"]))
        await self.drained()


class OneEntryOneChangeTest(Base):
    """Discord can send a change before its audit entry, so an entry for the change before it can be all there is. It mustn't get the credit."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.second = FakeMember(self.guild, "second mod", self.role_mod)

    def copy_of(self, member):
        return SimpleNamespace(**{name: getattr(member, name, None) for name in ("id", "name", "roles", "nick", "timed_out_until", "guild", "mention", "display_avatar", "guild_avatar", "joined_at")}, is_timed_out=lambda: bool(member.timed_out_until))

    async def test_a_timeout_lifted_by_someone_else_doesnt_name_who_set_it(self):
        await self.turn_on("timeout", "removetimeout")
        until = discord.utils.utcnow() + datetime.timedelta(hours=1)
        before = self.copy_of(self.member)
        self.member.timed_out_until = until
        self.bot.audit.feed(self.audit(Actions.member_update, target=self.member, reason="spam", after={"timed_out_until": until}))
        await self.logs.on_member_update(before, self.member)
        self.assertIn("(mod)", self.embeds("Member timed out")[0]["description"])
        before = self.copy_of(self.member)
        before.is_timed_out = lambda: True
        self.member.timed_out_until = None
        await self.logs.on_member_update(before, self.member)
        lifted = self.embeds("Timeout removed")[0]["description"]
        self.assertNotIn("(mod)", lifted)
        self.assertNotIn("spam", lifted)

    async def test_a_lift_ignores_a_setting_entry_nobody_used(self):
        await self.turn_on("removetimeout")
        await self.configure(events=[event for event in self.settings("logging")["events"] if event != "timeout"])
        until = discord.utils.utcnow() + datetime.timedelta(hours=1)
        self.bot.audit.feed(self.audit(Actions.member_update, target=self.member, reason="spam", after={"timed_out_until": until}))
        before = self.copy_of(self.member)
        before.timed_out_until = until
        before.is_timed_out = lambda: True
        await self.logs.on_member_update(before, self.member)
        self.assertNotIn("(mod)", self.embeds("Timeout removed")[0]["description"])

    async def test_the_right_entry_still_names_who_lifted_it(self):
        await self.turn_on("removetimeout")
        until = discord.utils.utcnow() + datetime.timedelta(hours=1)
        self.bot.audit.feed(self.audit(Actions.member_update, user=self.second, target=self.member, after={"timed_out_until": None}))
        before = self.copy_of(self.member)
        before.timed_out_until = until
        before.is_timed_out = lambda: True
        await self.logs.on_member_update(before, self.member)
        self.assertIn("(second mod)", self.embeds("Timeout removed")[0]["description"])

    async def test_changing_your_own_name_doesnt_name_the_moderator_who_changed_it_before(self):
        self.bot.audit.feed(self.audit(Actions.member_update, target=self.member, after={"nick": "clean name"}))
        before = self.copy_of(self.member)
        self.member.nick = "clean name"
        await self.logs.on_member_update(before, self.member)
        self.assertIn("(mod)", self.posted[0][1]["description"])
        before = self.copy_of(self.member)
        self.member.nick = "my name again"
        await self.logs.on_member_update(before, self.member)
        self.assertNotIn("(mod)", self.posted[1][1]["description"])

    async def test_one_entry_can_explain_a_name_and_a_timeout_together(self):
        await self.turn_on("timeout")
        until = discord.utils.utcnow() + datetime.timedelta(hours=1)
        self.bot.audit.feed(self.audit(Actions.member_update, target=self.member, after={"nick": "quiet", "timed_out_until": until}))
        before = self.copy_of(self.member)
        self.member.nick = "quiet"
        self.member.timed_out_until = until
        await self.logs.on_member_update(before, self.member)
        self.assertIn("(mod)", self.embeds("Nickname")[0]["description"])
        self.assertIn("(mod)", self.embeds("Member timed out")[0]["description"])

    async def test_a_second_ban_doesnt_name_who_did_the_first(self):
        self.bot.audit.feed(self.audit(Actions.ban, target=self.member, reason="first ban"))
        await self.logs.on_member_ban(self.guild, self.member)
        self.assertIn("first ban", self.posted[0][1]["description"])
        await self.logs.on_member_ban(self.guild, self.member)
        self.assertNotIn("first ban", self.posted[1][1]["description"])

    async def test_a_second_role_edit_doesnt_name_who_did_the_first(self):
        await self.turn_on("roleupdate")
        role = self.role_member
        self.bot.audit.feed(self.audit(Actions.role_update, target=role, after={"color": discord.Color(1)}))

        def state(color: int, permissions: discord.Permissions) -> SimpleNamespace:
            return SimpleNamespace(id=role.id, name=role.name, color=discord.Color(color), mentionable=False, hoist=False, permissions=permissions, display_icon=None, guild=self.guild)

        none = discord.Permissions.none()
        await self.logs.on_guild_role_update(state(0, none), state(1, none))
        self.assertIn("(mod)", self.posted[0][1]["description"])
        await self.logs.on_guild_role_update(state(1, none), state(1, discord.Permissions(administrator=True)))
        self.assertNotIn("description", self.posted[1][1])

    async def test_an_unmute_doesnt_name_who_muted(self):
        await self.turn_on("voicemod")
        self.bot.audit.feed(self.audit(Actions.member_update, target=self.member, after={"mute": True}))
        channel = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)

        def state(mute: bool) -> SimpleNamespace:
            return SimpleNamespace(channel=channel, mute=mute, deaf=False)

        await self.logs.on_voice_state_update(self.member, state(False), state(True))
        self.assertIn("(mod)", self.posted[-1][1]["description"])
        await self.logs.on_voice_state_update(self.member, state(True), state(False))
        self.assertNotIn("(mod)", self.posted[-1][1]["description"])

    async def test_a_later_change_gets_its_own_entry_when_it_arrives_in_time(self):
        await self.turn_on("timeout", "removetimeout")
        self.bot.audit.feed(self.audit(Actions.ban, target=self.member, reason="first ban"))
        await self.logs.on_member_ban(self.guild, self.member)
        self.bot.audit.feed(self.audit(Actions.ban, user=self.second, target=self.member, reason="second ban"))
        await self.logs.on_member_ban(self.guild, self.member)
        self.assertIn("second ban", self.posted[1][1]["description"])


class TimeoutLiftedThroughTheLibraryTest(GatewayTest):
    modules = [Logs]

    async def asyncSetUp(self):
        self.quick = mock.patch.multiple(logs_module, **QUICK)
        self.quick.start()
        await super().asyncSetUp()
        self.state = self.bot._connection
        await self.bot.settings.save(self.guild_id, "logging", {"default_channel": int(self.log_channel["id"]), "events": list(EVENT_KEYS)})

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.quick.stop()

    def member_update(self, **fields):
        self.member_json[self.member_id] = {**self.member_json[self.member_id], **fields}
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **self.member_json[self.member_id]})

    async def embeds(self):
        await self.settle()
        for _ in range(200):
            if not [worker for worker in self.bot.logs._workers.values() if not worker.done()]:
                break
            await asyncio.sleep(0.005)
        return [embed for message in self.net.posted for embed in message.get("embeds", [])]

    async def test_the_lift_names_nobody_and_stores_nobody(self):
        until = (discord.utils.utcnow() + datetime.timedelta(hours=1)).isoformat()
        entry = {"id": str(discord.utils.time_snowflake(discord.utils.utcnow())), "guild_id": str(self.guild_id), "action_type": Actions.member_update.value, "user_id": str(self.mod_id), "target_id": str(self.member_id), "reason": "spam", "changes": [{"key": "communication_disabled_until", "new_value": until}]}
        self.state.parse_guild_audit_log_entry_create(entry)
        await self.settle()
        self.member_update(communication_disabled_until=until)
        embeds = await self.embeds()
        self.assertEqual(embeds[-1]["title"], "Member timed out")
        self.assertIn("(mod)", embeds[-1]["description"])
        self.member_update(communication_disabled_until=None)  # the admin's entry is still on the way
        embeds = await self.embeds()
        self.assertEqual(embeds[-1]["title"], "Timeout removed")
        self.assertNotIn("(mod)", embeds[-1]["description"])
        self.assertNotIn("spam", embeds[-1]["description"])
        await self.bot.module("logging").flush()
        rows = await self.message_db.all("SELECT event, actor_id FROM log_entries ORDER BY id")
        self.assertEqual([(row["event"], row["actor_id"]) for row in rows], [("timeout", self.mod_id), ("removetimeout", None)])
        self.assert_no_errors()


class UnknownMessageCreditTest(Base):
    async def test_a_message_the_store_doesnt_have_lets_a_known_one_claim_first(self):
        real = await self.send("what the moderator deleted")
        self.delete_entry(count=1)
        # The known delete's head start has to outlast its store lookup. With eight test processes at once, 50 ms didn't.
        with mock.patch.object(logs_module, "DELETE_SETTLE", 0.02), mock.patch.object(logs_module, "DELETE_LATE_WAIT", 0.5):
            await asyncio.gather(self.delete(new_id()), self.delete(real.id))
        by_text = {embed.get("description"): field_value(embed, "Deleted by") for _, embed, _ in self.posted}
        self.assertIn(f"<@{self.mod.id}>", by_text["what the moderator deleted"])
        self.assertEqual(by_text["This message isn't in the store, so its text is gone."], NOBODY)

    async def test_a_message_the_store_doesnt_have_can_still_be_credited_alone(self):
        self.delete_entry(count=1)
        with mock.patch.object(logs_module, "DELETE_SETTLE", 0.01), mock.patch.object(logs_module, "DELETE_LATE_WAIT", 0.01):
            await self.delete(new_id())
        self.assertIn("isn't in the store", self.posted[0][1]["description"])
        self.assertIn(f"<@{self.mod.id}>", field_value(self.posted[0][1], "Deleted by"))


class ImagesInFlightTest(Base):
    async def setup_images(self, count: int, size: int) -> list:
        self.downloads = []

        async def download(item):
            self.downloads.append(item["filename"])
            await asyncio.sleep(0.01)
            return b"image bytes"

        self.logs._download = download
        # Nothing here posts, so what's been handed to the sink stays in its queue, as it does for a good while in a flood.
        self.bot.logs.queued_files = lambda: sum(1 for _, _, files in self.posted if files)
        self.bot.logs.queued_bytes = lambda: sum(len(files) * size for _, _, files in self.posted if files)
        return [await self.send("spam", attachments=[attachment(f"{number}-{part}.png", size=size) for part in range(4)]) for number in range(count)]

    async def test_copies_being_fetched_count_toward_the_limit(self):
        messages = await self.setup_images(100, 1000)
        with mock.patch.object(logs_module, "DELETE_SETTLE", 0.05):
            await asyncio.gather(*(self.delete(message.id) for message in messages))
        self.assertLessEqual(len(self.downloads), logs_module.MAX_QUEUED_COPIES * logs_module.MAX_COPIED_IMAGES)
        self.assertEqual(len(self.posted), 100)  # the entries all go out, and the ones past the limit go without copies
        self.assertEqual(self.logs._copies_coming, 0)
        self.assertEqual(self.logs._copy_bytes_coming, 0)

    async def test_big_copies_count_by_size(self):
        self.guild.filesize_limit = 100 * 1024 * 1024
        messages = await self.setup_images(10, 25 * 1024 * 1024)
        with mock.patch.object(logs_module, "DELETE_SETTLE", 0.05):
            await asyncio.gather(*(self.delete(message.id) for message in messages))
        self.assertLessEqual(len(self.downloads) * 25 * 1024 * 1024, logs_module.MAX_COPY_BYTES)
        self.assertGreater(len(self.downloads), 0)
        self.assertEqual(len(self.posted), 10)

    async def test_what_waits_to_post_counts_by_size_too(self):
        messages = await self.setup_images(1, 1000)
        self.bot.logs.queued_bytes = lambda: logs_module.MAX_COPY_BYTES
        await self.delete(messages[0].id)
        self.assertEqual(self.downloads, [])
        self.assertEqual(len(self.posted), 1)

    async def test_room_comes_back_once_a_delete_is_done(self):
        messages = await self.setup_images(3, 1000)
        for message in messages:
            await self.delete(message.id)
            self.posted.clear()  # it reached the channel
        self.assertEqual(len(self.downloads), 12)
        self.assertEqual(self.logs._copies_coming, 0)


class StaffNoticeTest(Base):
    async def test_it_reaches_the_server_channel_whichever_events_are_on(self):
        server = FakeChannel(self.guild, "server-log")
        await self.configure(events=[], server_channel=server.id)
        self.logs.staff_notice(self.guild, "Something needs a look", "What and where.")
        channel_id, embed, _ = self.posted[0]
        self.assertEqual(channel_id, server.id)
        self.assertEqual(embed["title"], "Something needs a look")
        self.assertEqual(embed["description"], "What and where.")
        await self.logs.flush()
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM log_entries"), 1)
