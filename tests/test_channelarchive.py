"""Channel archive: copying a channel and its threads into another channel under each author's name, as a stored job that picks up where it stopped."""
from __future__ import annotations

import asyncio
import datetime
import json

import discord

from captainguild.core.module import required_permissions
from captainguild.core.ops import DiscordOps
from captainguild.modules.channelarchive import MARKER, NEEDS_TEXT, PROGRESS_EVERY, ChannelArchive, eta_text
from tests.gateway import FakeHTTPResponse, GatewayTest, iso, new_id


class Ctx:
    """What the command reads from its context, over the real server."""

    def __init__(self, test: "Base", author_id: int) -> None:
        self.bot = test.bot
        self.guild = test.guild
        self.author = test.guild.get_member(author_id)
        self.channel = test.guild.get_channel(int(test.text["id"]))
        self.interaction = None
        self.sent: list[str] = []
        self.fails: list[str] = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)

    async def fail(self, text, **kwargs):
        self.fails.append(text)

    async def defer(self, **kwargs):
        pass


class Base(GatewayTest):
    modules = [ChannelArchive]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cog = self.bot.module("channelarchive")
        await self.bot.settings.save(self.guild_id, "channelarchive", {"enabled": True})
        self.slept: list[float] = []

        async def no_wait(seconds):
            self.slept.append(seconds)
        self.cog._sleep = no_wait
        self.net.keep_history = True
        self.net._current_channel = int(self.log_channel["id"])  # webhook posts land in the target, as on Discord, and never in the source being read
        self.net._history = self.history_page
        self.source = self.guild.get_channel(int(self.text["id"]))
        self.target = self.guild.get_channel(int(self.log_channel["id"]))
        self.uploaded: list[list[str]] = []
        original = self.net.webhook

        async def webhook(adapter, route, session=None, **kwargs):
            if kwargs.get("files"):
                self.uploaded.append([file.filename for file in kwargs["files"]])
            return await original(adapter, route, session, **kwargs)
        self.net.webhook = webhook

    def history_page(self, channel_id: int, query: dict) -> list[dict]:
        """A page of history as Discord gives it: newest first, and with `after`, the page that starts right after that message."""
        found = sorted((m for m in self.net.messages.values() if int(m["channel_id"]) == channel_id), key=lambda m: int(m["id"]), reverse=True)
        limit = int(query.get("limit") or 50)
        if query.get("before"):
            found = [m for m in found if int(m["id"]) < int(query["before"])]
        if query.get("after") is not None:  # the first page of an oldest-first read asks after 0
            found = [m for m in found if int(m["id"]) > int(query["after"])][-limit:]
        return found[:limit]

    def message(self, channel_id: int, content: str = "hello", *, author: int | None = None, kind: int = 0, **extra) -> dict:
        data = self.net.message_json(channel_id, {"content": content})
        data["author"] = self.member_json[author or self.member_id]["user"]
        data["type"] = kind
        data.update(extra)
        self.net.messages[int(data["id"])] = data
        return data

    def thread(self, parent: dict, name: str, *, archived: bool = False) -> dict:
        data = {"id": str(new_id()), "guild_id": str(self.guild_id), "parent_id": parent["id"], "owner_id": str(self.admin_id), "name": name, "type": 11, "message_count": 0, "member_count": 1,
                "rate_limit_per_user": 0, "flags": 0, "thread_metadata": {"archived": archived, "auto_archive_duration": 1440, "archive_timestamp": iso(), "locked": False, "create_timestamp": iso()}}
        self.net.channels[int(data["id"])] = data
        if archived:
            self.net.archived.setdefault(int(parent["id"]), []).append(data)
        else:
            self.bot._connection.parse_thread_create(data)
        return data

    def posts(self) -> list[tuple[str, str]]:
        """(name, text) of every webhook post, markers and copies alike."""
        found = []
        for method, path, body in self.net.calls:
            if method == "POST" and path == "/webhooks/{webhook_id}/{webhook_token}":
                text = body.get("content") or ((body.get("embeds") or [{}])[0].get("description") or "")
                found.append((body.get("username"), text))
        return found

    def markers(self) -> list[str]:
        return [text for name, text in self.posts() if name == MARKER]

    async def copy(self, source=None, target=None, author: int | None = None) -> int:
        actor = self.bot.actor(self.guild.get_member(author or self.admin_id))
        job_id = await self.cog.start(self.guild_id, source or self.source, target or self.target, actor)
        await self.cog.finish_jobs()
        await self.settle()
        return job_id

    async def job(self, job_id: int) -> dict:
        return await self.db.one("SELECT * FROM archive_jobs WHERE id = ?", (job_id,))


class CopyTest(Base):
    """CA-3 to CA-7: what goes over, in what order, with which markers, and what a later copy adds."""

    async def test_a_first_copy_marks_each_part_and_a_later_one_brings_only_whats_new(self):
        one = self.message(int(self.text["id"]), "first")
        self.message(int(self.text["id"]), "second", author=self.mod_id)
        self.message(int(self.text["id"]), "joined", kind=7)  # a join line isn't copied
        busy = self.thread(self.text, "Prints")
        self.message(int(busy["id"]), "in the thread")
        empty = self.thread(self.text, "Quiet")
        job_id = await self.copy()
        self.assert_no_errors()
        created = str(self.guild.get_thread(int(busy["id"])).created_at)
        self.assertEqual(self.posts(), [
            (MARKER, "Found 3 messages to sync. Estimated time to completion: 6s."),
            (MARKER, "**Start of parent channel archive: general**"),
            ("member", "first"), ("mod", "second"),
            (MARKER, "**End of parent channel archive**"),
            (MARKER, f"**Start of thread archive: Prints (ID: {busy['id']}, created: {created})**"),
            ("member", "in the thread"),
            (MARKER, "**End of thread archive: Prints**"),
            (MARKER, f"**Start of thread archive: Quiet (ID: {empty['id']}, created: {str(self.guild.get_thread(int(empty['id'])).created_at)})**"),
            (MARKER, "**End of thread archive: Quiet**"),
            (MARKER, "Sync complete! Copied 3 messages."),
        ])
        self.assertEqual(self.slept, [2.0, 2.0, 2.0], "two seconds after every message")
        self.assertEqual((await self.job(job_id))["state"], "done")
        self.net.calls.clear()
        self.message(int(self.text["id"]), "third")
        await self.copy()
        self.assertEqual(self.posts(), [
            (MARKER, "Found 1 messages to sync. Estimated time to completion: 2s."),
            (MARKER, "**Additional messages in parent channel**"),
            ("member", "third"),
            (MARKER, "Sync complete! Copied 1 messages."),
        ])
        self.net.calls.clear()
        await self.copy()
        self.assertEqual(self.posts(), [(MARKER, "No new messages to sync.")])
        self.assertEqual(await self.db.scalar("SELECT last_message_id FROM archive_cursors WHERE source_channel_id = ? AND kind = 'parent'", (self.source.id,)), max(m for m, d in self.net.messages.items() if d["channel_id"] == self.text["id"] and d["type"] == 0))
        self.assertIsNotNone(one)

    async def test_each_copy_carries_its_authors_name_and_picture_and_pings_nobody(self):
        self.message(int(self.text["id"]), "@everyone look <@&123456789012345678>")
        await self.copy()
        body = next(body for method, path, body in self.net.calls if path == "/webhooks/{webhook_id}/{webhook_token}" and body.get("username") == "member")
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertTrue(body["avatar_url"].startswith("https://cdn.discordapp.com/"))
        self.assertEqual(body["content"], "@everyone look <@&123456789012345678>")

    async def test_long_text_goes_into_an_embed_and_a_poll_shows_its_results(self):
        self.message(int(self.text["id"]), "x" * 2500)
        poll = {"question": {"text": "Best filament?"}, "answers": [{"answer_id": 1, "poll_media": {"text": "PLA"}}, {"answer_id": 2, "poll_media": {"text": "PETG"}}],
                "expiry": iso(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)), "allow_multiselect": False, "layout_type": 1,
                "results": {"is_finalized": True, "answer_counts": [{"id": 1, "count": 3, "me_voted": False}, {"id": 2, "count": 1, "me_voted": False}]}}
        self.message(int(self.text["id"]), "", poll=poll)
        await self.copy()
        bodies = [body for method, path, body in self.net.calls if path == "/webhooks/{webhook_id}/{webhook_token}" and body.get("username") == "member"]
        self.assertNotIn("content", bodies[0])
        self.assertEqual(bodies[0]["embeds"][0]["description"], "x" * 2500)
        poll_embed = bodies[1]["embeds"][-1]
        self.assertEqual(poll_embed["title"], "Poll: Best filament?")
        self.assertEqual([(field["name"], field["value"]) for field in poll_embed["fields"]], [("PLA", "Votes: 3 (75.0%)"), ("PETG", "Votes: 1 (25.0%)")])

    async def test_files_spoilers_and_stickers_are_uploaded_again(self):
        files = [{"id": str(new_id()), "filename": "benchy.png", "size": 10, "url": "https://cdn.example/benchy.png", "proxy_url": "https://cdn.example/benchy.png"},
                 {"id": str(new_id()), "filename": "SPOILER_secret.png", "size": 10, "url": "https://cdn.example/secret.png", "proxy_url": "https://cdn.example/secret.png"},
                 {"id": str(new_id()), "filename": "huge.zip", "size": 200 * 1024 * 1024, "url": "https://cdn.example/huge.zip", "proxy_url": "https://cdn.example/huge.zip"}]
        self.message(int(self.text["id"]), "files", attachments=files, sticker_items=[{"id": str(new_id()), "name": "wave", "format_type": 1}])
        await self.copy()
        self.assertEqual(self.uploaded, [["benchy.png", "SPOILER_secret.png", "sticker.png"]], "a file past the server's upload limit is left out")

    async def test_files_past_what_one_message_carries_are_left_out(self):
        limit = self.guild.filesize_limit
        files = [{"id": str(new_id()), "filename": f"part{index}.bin", "size": limit // 2 + 1, "url": f"https://cdn.example/part{index}.bin", "proxy_url": f"https://cdn.example/part{index}.bin"} for index in range(3)]
        self.message(int(self.text["id"]), "parts", attachments=files)
        await self.copy()
        self.assertEqual(self.uploaded, [["part0.bin"]], "the second file would take the message past the limit")

    async def test_a_message_with_nothing_to_post_is_counted_and_linked(self):
        """Such as one whose only file is past the upload limit. Passing it without a trace would hide it for good."""
        empty = self.message(int(self.text["id"]), "")
        self.message(int(self.text["id"]), "after it")
        job_id = await self.copy()
        job = await self.job(job_id)
        self.assertEqual((job["copied"], job["failed"]), (1, 1))
        [failure] = json.loads(job["failures"])
        self.assertEqual((failure["url"].rsplit("/", 1)[1], failure["error"]), (empty["id"], "Nothing in it could be copied."))
        self.assertEqual([text for name, text in self.posts() if name == "member"], ["after it"])

    async def test_a_forward_brings_what_it_forwarded_under_a_forwarded_line(self):
        self.message(int(self.text["id"]), "", message_reference={"type": 1, "channel_id": self.log_channel["id"], "message_id": "1"},
                     message_snapshots=[{"message": {"type": 0, "content": "Version 1.08 is out", "embeds": [], "attachments": [], "timestamp": iso(), "edited_timestamp": None, "flags": 0}}])
        job_id = await self.copy()
        self.assertEqual([text for name, text in self.posts() if name == "member"], ["-# Forwarded\nVersion 1.08 is out"])
        self.assertEqual(((await self.job(job_id))["copied"], (await self.job(job_id))["failed"]), (1, 0))

    async def test_every_page_of_archived_threads_is_read(self):
        pages = []
        for index in range(3):
            thread = self.thread(self.text, f"Old {index}", archived=True)
            self.message(int(thread["id"]), f"old {index}")
            pages.append(thread)
        original = self.net.http
        asked = []

        async def paged(route, **kwargs):
            if route.path.endswith("/threads/archived/public"):
                params = kwargs.get("params") or {}
                asked.append(params.get("before"))
                shown = pages[:2] if not params.get("before") else pages[2:]
                return {"threads": shown, "members": [], "has_more": not params.get("before")}
            if "threads/archived" in route.path:
                return {"threads": [], "members": [], "has_more": False}
            return await original(route, **kwargs)
        self.bot.http.request = paged
        await self.copy()
        self.assertEqual(len(asked), 2, "the second page was asked for")
        copied = [text for name, text in self.posts() if name == "member"]
        self.assertEqual(sorted(copied), ["old 0", "old 1", "old 2"])

    async def test_a_message_discord_refuses_is_counted_linked_and_passed(self):
        bad = self.message(int(self.text["id"]), "refused")
        self.message(int(self.text["id"]), "fine")
        original = self.net.webhook

        async def refuse(adapter, route, session=None, **kwargs):
            if (kwargs.get("payload") or {}).get("content") == "refused":
                raise discord.HTTPException(FakeHTTPResponse(400), {"code": 50035, "message": "Invalid Form Body"})
            return await original(adapter, route, session, **kwargs)
        self.net.webhook = refuse
        job_id = await self.copy()
        job = await self.job(job_id)
        self.assertEqual((job["copied"], job["failed"]), (1, 1))
        self.assertIn(f"/{bad['id']}", json.loads(job["failures"])[0]["url"])
        self.assertEqual(self.markers()[-1], "Sync complete! Copied 1 messages. 1 failures. The Channel archive page links each one.")
        self.assertEqual(await self.db.scalar("SELECT last_message_id FROM archive_cursors WHERE kind = 'parent'"), max(int(m) for m, d in self.net.messages.items() if d["channel_id"] == self.text["id"]))

    async def test_a_discord_outage_stops_the_copy_where_it_is(self):
        """discord.py gives up on a server error after five tries. The message isn't refused, so it waits for a resume instead of being passed."""
        for index in range(3):
            self.message(int(self.text["id"]), f"m{index}")
        original = self.net.webhook
        down = []

        async def outage(adapter, route, session=None, **kwargs):
            if (kwargs.get("payload") or {}).get("content") == "m1" and not down:
                down.append(1)
                raise discord.DiscordServerError(FakeHTTPResponse(503), "Service Unavailable")
            return await original(adapter, route, session, **kwargs)
        self.net.webhook = outage
        job_id = await self.copy()
        job = await self.job(job_id)
        self.assertEqual((job["state"], job["copied"], job["failed"]), ("stopped", 1, 0))
        self.assertEqual(job["note"], "An error occurred: Discord refused the post: Service Unavailable")
        await self.db.run("UPDATE archive_jobs SET state = 'running' WHERE id = ?", (job_id,))
        self.cog._start(job_id)
        await self.cog.finish_jobs()
        self.assertEqual([text for name, text in self.posts() if name == "member"], ["m0", "m1", "m2"])

    async def test_progress_every_hundred_messages(self):
        for index in range(PROGRESS_EVERY + 1):
            self.message(int(self.text["id"]), f"m{index}")
        await self.copy()
        self.assertIn(f"Progress: Copied {PROGRESS_EVERY}/{PROGRESS_EVERY + 1} messages.", self.markers())

    async def test_a_thread_target_gets_the_copies_through_its_parent(self):
        self.message(int(self.text["id"]), "into a thread")
        into = self.thread(self.log_channel, "Archive")
        await self.copy(target=self.guild.get_thread(int(into["id"])))
        sent = [params for method, path, params in self.net.webhook_params if method == "POST" and path == "/webhooks/{webhook_id}/{webhook_token}"]
        self.assertTrue(sent and all(params.get("thread_id") == int(into["id"]) for params in sent))

    async def test_a_forum_copies_its_posts_and_has_no_parent_part(self):
        post = self.thread(self.forum, "Review")
        self.message(int(post["id"]), "nice printer")
        await self.copy(source=self.guild.get_channel(int(self.forum["id"])))
        self.assertNotIn("**Start of parent channel archive: reviews**", self.markers())
        self.assertIn(("member", "nice printer"), self.posts())


class JobTest(Base):
    """A copy is a stored job: it resumes after a restart, stops when asked, waits its turn, and says what went wrong."""

    async def test_a_stopped_copy_resumes_from_its_place_without_repeating_a_marker(self):
        for index in range(4):
            self.message(int(self.text["id"]), f"m{index}")
        original = self.cog.copy_message
        done = []

        async def copy_then_stop(guild, message, target):
            posted = await original(guild, message, target)
            done.append(message.id)
            if len(done) == 2:
                await self.db.run("UPDATE archive_jobs SET state = 'stopped'")
            return posted
        self.cog.copy_message = copy_then_stop
        job_id = await self.copy()
        self.assertEqual([text for name, text in self.posts() if name == "member"], ["m0", "m1"])
        await self.db.run("UPDATE archive_jobs SET state = 'running' WHERE id = ?", (job_id,))
        self.cog._start(job_id)
        await self.cog.finish_jobs()
        self.assertEqual([text for name, text in self.posts() if name == "member"], ["m0", "m1", "m2", "m3"])
        self.assertEqual(self.markers().count("**Start of parent channel archive: general**"), 1)
        self.assertEqual(self.markers()[-2:], ["**End of parent channel archive**", "Sync complete! Copied 4 messages."])

    async def stop_while_posting(self, text: str) -> int:
        """Starts a copy, stops the service while the post holding `text` waits for Discord's answer, then starts the service again."""
        original = self.net.webhook
        on_its_way, release = asyncio.Event(), asyncio.Event()

        async def slow(adapter, route, session=None, **kwargs):
            answer = await original(adapter, route, session, **kwargs)  # Discord has the post
            if (kwargs.get("payload") or {}).get("content") == text and not on_its_way.is_set():
                on_its_way.set()
                await release.wait()  # and its answer is on the way back when the stop comes
            return answer
        self.net.webhook = slow
        job_id = await self.cog.start(self.guild_id, self.source, self.target, self.bot.actor(self.guild.get_member(self.admin_id)))
        await asyncio.wait_for(on_its_way.wait(), 5)
        stopping = asyncio.ensure_future(self.cog.cog_unload())  # what the service's stop runs for every module
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(stopping, 5)
        await asyncio.gather(*list(self.cog._jobs.values()), return_exceptions=True)
        self.assertEqual((await self.job(job_id))["state"], "running", "a stop leaves the copy for the next start")
        self.net.webhook = original
        await self.cog.on_ready()
        await self.cog.finish_jobs()
        return job_id

    async def test_a_stop_while_a_message_is_on_its_way_copies_it_once(self):
        for index in range(3):
            self.message(int(self.text["id"]), f"m{index}")
        await self.stop_while_posting("m1")
        self.assertEqual([text for name, text in self.posts() if name == "member"], ["m0", "m1", "m2"])

    async def test_a_stop_while_a_start_marker_is_on_its_way_posts_it_once(self):
        self.message(int(self.text["id"]), "m0")
        await self.stop_while_posting("**Start of parent channel archive: general**")
        self.assertEqual(self.markers().count("**Start of parent channel archive: general**"), 1)
        self.assertEqual([text for name, text in self.posts() if name == "member"], ["m0"])

    async def test_a_copy_without_message_content_stops_and_keeps_its_place(self):
        """The bot can come back up without the intent, and then Discord sends every message with no text."""
        source = int(self.text["id"])
        sent = [self.message(source, "") for _ in range(2)]
        job_id = await self.db.insert("INSERT INTO archive_jobs (guild_id, source_channel_id, target_channel_id, source_name, target_name, state, started_by, started_by_name, created_at) VALUES (?, ?, ?, 'general', 'logs', 'running', ?, 'admin', 0)",
                                      (self.guild_id, source, self.target.id, self.admin_id))
        self.bot.reads_message_text = False
        await self.cog.on_ready()
        await self.cog.finish_jobs()
        job = await self.job(job_id)
        self.assertEqual((job["state"], job["note"], job["copied"]), ("stopped", NEEDS_TEXT, 0))
        self.assertEqual(self.posts(), [])
        self.assertIsNone(await self.db.scalar("SELECT last_message_id FROM archive_cursors WHERE source_channel_id = ?", (source,)))
        for message, text in zip(sent, ("first", "second")):
            message["content"] = text
        self.bot.reads_message_text = True
        await self.db.run("UPDATE archive_jobs SET state = 'running' WHERE id = ?", (job_id,))
        self.cog._start(job_id)
        await self.cog.finish_jobs()
        self.assertEqual([text for name, text in self.posts() if name == "member"], ["first", "second"])

    async def test_a_restart_picks_up_a_running_copy(self):
        self.message(int(self.text["id"]), "after the restart")
        await self.db.insert("INSERT INTO archive_jobs (guild_id, source_channel_id, target_channel_id, source_name, target_name, state, started_by, started_by_name, created_at) VALUES (?, ?, ?, 'general', 'logs', 'running', ?, 'admin', 0)",
                             (self.guild_id, self.source.id, self.target.id, self.admin_id))
        await self.cog.on_ready()
        await self.cog.finish_jobs()
        self.assertIn(("member", "after the restart"), self.posts())

    async def test_one_copy_at_a_time_in_a_server(self):
        self.message(int(self.text["id"]), "from general")
        other = self.guild.get_channel(int(self.forum["id"]))
        post = self.thread(self.forum, "Review")
        self.message(int(post["id"]), "from the forum")
        actor = self.bot.actor(self.guild.get_member(self.admin_id))
        await self.cog.start(self.guild_id, self.source, self.target, actor)
        await self.cog.start(self.guild_id, other, self.target, actor)
        await self.cog.finish_jobs()
        markers = self.markers()
        self.assertLess(markers.index("Sync complete! Copied 1 messages."), markers.index("Found 1 messages to sync. Estimated time to completion: 2s.", 2))

    async def test_the_same_pair_cant_run_twice(self):
        self.message(int(self.text["id"]), "x")
        actor = self.bot.actor(self.guild.get_member(self.admin_id))
        await self.db.insert("INSERT INTO archive_jobs (guild_id, source_channel_id, target_channel_id, source_name, target_name, state, created_at) VALUES (?, ?, ?, 'general', 'logs', 'running', 0)", (self.guild_id, self.source.id, self.target.id))
        with self.assertRaises(Exception) as caught:
            await self.cog.start(self.guild_id, self.source, self.target, actor)
        self.assertIn("is already running", str(caught.exception))

    async def test_no_manage_webhooks_stops_the_copy_and_says_so(self):
        self.message(int(self.text["id"]), "x")
        original = self.net.http

        async def refuse(route, **kwargs):
            if route.path == "/channels/{channel_id}/webhooks":
                raise discord.Forbidden(FakeHTTPResponse(403), {"code": 50013, "message": "Missing Permissions"})
            return await original(route, **kwargs)
        self.bot.http.request = refuse
        asker = Ctx(self, self.admin_id)
        await self.cog.start(self.guild_id, self.source, self.target, self.bot.actor(asker.author), asker=asker)
        await self.cog.finish_jobs()
        job = await self.db.one("SELECT * FROM archive_jobs")
        self.assertEqual(job["state"], "stopped")
        self.assertEqual(job["note"], "An error occurred: The bot needs Manage Webhooks in that channel.")
        self.assertIn("An error occurred: The bot needs Manage Webhooks in that channel.", asker.sent)

    async def test_a_restore_stops_the_servers_copies_for_a_resume(self):
        self.message(int(self.text["id"]), "x")
        actor = self.bot.actor(self.guild.get_member(self.admin_id))
        job_id = await self.cog.start(self.guild_id, self.source, self.target, actor, run=False)
        self.cog._start(job_id)
        await self.cog.before_restore(self.guild_id)
        self.assertNotIn(job_id, self.cog._jobs)
        await self.cog.on_guild_restored(self.guild_id)
        job = await self.job(job_id)
        self.assertEqual(job["state"], "stopped")
        self.assertIn("Resume it", job["note"])

    async def test_a_bug_stops_the_copy_and_says_so(self):
        self.message(int(self.text["id"]), "x")

        async def broken(guild, message, target):
            raise RuntimeError("a bug")
        self.cog.copy_message = broken
        asker = Ctx(self, self.admin_id)
        job_id = await self.cog.start(self.guild_id, self.source, self.target, self.bot.actor(asker.author), asker=asker)
        await self.cog.finish_jobs()
        self.assertEqual((await self.job(job_id))["state"], "stopped")
        self.assertIn("Something went wrong with the copy.", asker.sent[-1])
        self.errors.lines.clear()  # the bug is logged, which is the point

    async def test_the_count_and_the_end_go_to_whoever_started_it(self):
        self.message(int(self.text["id"]), "x")
        asker = Ctx(self, self.admin_id)
        await self.cog.start(self.guild_id, self.source, self.target, self.bot.actor(asker.author), asker=asker)
        await self.cog.finish_jobs()
        self.assertEqual(asker.sent, ["Found 1 messages to sync. Estimated time to completion: 2s.", "Sync complete! Copied 1 messages."])
        self.assertEqual(self.markers()[-1], "Sync complete! Copied 1 messages.", "the end also goes in the target, as before")

    async def test_answers_after_the_slash_command_expires_go_in_the_target(self):
        """After 15 minutes discord.py would post an answer in the command's channel for everyone to see, so it goes in the target as a marker."""
        self.message(int(self.text["id"]), "x")
        for expired in (False, True):
            asker = Ctx(self, self.admin_id)
            asker.interaction = type("Interaction", (), {"is_expired": lambda self, gone=expired: gone})()
            await self.db.run("DELETE FROM archive_cursors")
            calls = len(self.net.calls)
            await self.cog.start(self.guild_id, self.source, self.target, self.bot.actor(asker.author), asker=asker)
            await self.cog.finish_jobs()
            markers = [body.get("content") for method, path, body in self.net.calls[calls:] if method == "POST" and path == "/webhooks/{webhook_id}/{webhook_token}" and body.get("username") == MARKER]
            if expired:
                self.assertEqual(asker.sent, [])
                self.assertIn("Found 1 messages to sync. Estimated time to completion: 2s.", markers)
            else:
                self.assertEqual(asker.sent, ["Found 1 messages to sync. Estimated time to completion: 2s.", "Sync complete! Copied 1 messages."])
                self.assertNotIn("Found 1 messages to sync. Estimated time to completion: 2s.", markers)

    def test_the_estimate_reads_as_before(self):
        self.assertEqual(eta_text(10), "20s")
        self.assertEqual(eta_text(90), "3m 0s")
        self.assertEqual(eta_text(2000), "1h 6m 40s")


class CommandTest(Base):
    """CA-1: who can copy what, and the answer."""

    async def test_it_needs_manage_webhooks(self):
        self.assertEqual(required_permissions(self.cog.copy_channel), ("manage_webhooks",))

    async def test_the_command_starts_a_copy_and_answers(self):
        self.message(int(self.text["id"]), "x")
        ctx = Ctx(self, self.admin_id)
        await self.cog.copy_channel.callback(self.cog, ctx, self.source, self.target)
        await self.cog.finish_jobs()
        self.assertEqual(ctx.sent[0], f"Copying {self.source.mention} into {self.target.mention}. The count of messages follows here.")
        self.assertEqual(ctx.sent[1:], ["Found 1 messages to sync. Estimated time to completion: 2s.", "Sync complete! Copied 1 messages."])

    async def test_what_cant_be_copied_is_refused(self):
        ctx = Ctx(self, self.admin_id)
        await self.cog.copy_channel.callback(self.cog, ctx, self.source, self.source)
        thread = self.guild.get_thread(int(self.thread(self.text, "inside")["id"]))
        await self.cog.copy_channel.callback(self.cog, ctx, self.source, thread)
        self.assertEqual(ctx.fails, ["The target can't be the source or one of its threads."] * 2)
        member = Ctx(self, self.member_id)
        await self.cog.copy_channel.callback(self.cog, member, self.source, self.target)
        self.assertEqual(member.fails, ["You can't see that channel."])
        self.bot.reads_message_text = False
        blind = Ctx(self, self.admin_id)
        await self.cog.copy_channel.callback(self.cog, blind, self.source, self.target)
        self.assertEqual(blind.fails, [NEEDS_TEXT])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM archive_jobs"), 0)

    async def test_threads_the_starter_cant_read_are_left_out_and_noted(self):
        allowed = discord.Permissions(view_channel=True, read_message_history=True, send_messages=True, send_messages_in_threads=True, manage_webhooks=True)
        self.bot._connection.parse_guild_role_update({"guild_id": str(self.guild_id), "role": {**self.role_everyone, "permissions": str(allowed.value)}})
        hidden = self.thread(self.text, "Private talk")
        self.net.channels[int(hidden["id"])]["type"] = 12
        self.bot._connection.parse_thread_create({**hidden, "type": 12})
        self.message(int(hidden["id"]), "secret")
        self.message(int(self.text["id"]), "public")
        job_id = await self.copy(author=self.member_id)
        self.assertNotIn(("member", "secret"), self.posts())
        self.assertIn(("member", "public"), self.posts())
        self.assertEqual((await self.job(job_id))["note"], "Left out 1 threads that the person who started this can't read.")

    def private_thread(self) -> None:
        hidden = self.thread(self.text, "Staff only")
        self.net.channels[int(hidden["id"])]["type"] = 12
        self.bot._connection.parse_thread_create({**hidden, "type": 12})
        self.message(int(hidden["id"]), "secret", author=self.admin_id)
        self.message(int(self.text["id"]), "public")

    async def test_a_starter_who_left_before_the_copy_began_gets_no_private_thread(self):
        """A copy waits its turn, so its starter can be gone by the time it's planned."""
        self.private_thread()
        job_id = await self.cog.start(self.guild_id, self.source, self.target, self.bot.actor(self.guild.get_member(self.member_id)), run=False)
        self.bot._connection.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[self.member_id]["user"]})
        self.member_json.pop(self.member_id)  # Discord doesn't know them either
        self.cog._start(job_id)
        await self.cog.finish_jobs()
        self.assertEqual([(name, text) for name, text in self.posts() if name != MARKER], [("member", "public")])
        self.assertEqual((await self.job(job_id))["note"], "Left out 1 private threads, since the person who started this copy has left the server.")

    async def test_a_starter_missing_from_the_member_list_is_looked_up(self):
        """After a restart the member list takes a while to fill, and a copy can be planned meanwhile."""
        self.private_thread()
        admin = self.guild.get_member(self.admin_id)
        job_id = await self.cog.start(self.guild_id, self.source, self.target, self.bot.actor(admin), run=False)
        self.guild._remove_member(admin)
        self.cog._start(job_id)
        await self.cog.finish_jobs()
        self.assertIn(("admin", "secret"), self.posts(), "an Administrator reads every thread")
        self.assertIsNone((await self.job(job_id))["note"])
        self.assertIn(("GET", "/guilds/{guild_id}/members/{member_id}"), [(method, path) for method, path, _ in self.net.calls])


class PageTest(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        from aiohttp.test_utils import TestClient, TestServer
        from captainguild.web.api import User
        from captainguild.web.app import build_app
        self.bot.ops.access_for = lambda guild_id, who: "full"
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(self.admin_id, "admin", None))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await super().asyncTearDown()

    async def call(self, method: str, tail: str, body=None):
        answer = await self.client.request(method, f"/api/g/{self.guild_id}{tail}", json=body)
        return answer.status, await answer.json()

    async def test_start_list_stop_and_resume(self):
        for index in range(3):
            self.message(int(self.text["id"]), f"m{index}")
        original = self.cog.copy_message

        async def copy_then_stop(guild, message, target):
            posted = await original(guild, message, target)
            await self.db.run("UPDATE archive_jobs SET state = 'stopped'")
            return posted
        self.cog.copy_message = copy_then_stop
        status, started = await self.call("POST", "/channelarchive", {"source_channel_id": self.text["id"], "target_channel_id": self.log_channel["id"]})
        self.assertEqual(status, 200, started)
        await self.cog.finish_jobs()
        status, listed = await self.call("GET", "/channelarchive")
        [job] = listed["jobs"]
        self.assertEqual((job["state"], job["copied"], job["total"]), ("stopped", 1, 3))
        self.cog.copy_message = original
        self.assertEqual((await self.call("POST", f"/channelarchive/{job['id']}/resume"))[0], 200)
        await self.cog.finish_jobs()
        status, listed = await self.call("GET", "/channelarchive")
        self.assertEqual((listed["jobs"][0]["state"], listed["jobs"][0]["copied"]), ("done", 3))
        self.assertEqual((await self.call("POST", f"/channelarchive/{job['id']}/stop"))[0], 409)

    async def test_resume_refuses_someone_who_cant_post_in_the_target_as_start_does(self):
        """A manager with Manage Webhooks who sees #logs but can't post there, and an Administrator's stopped copy into it."""
        role = {"id": str(new_id()), "name": "Manager", "color": 0, "hoist": False, "position": 3, "managed": False, "mentionable": False, "icon": None, "unicode_emoji": None, "flags": 0,
                "permissions": str(discord.Permissions(manage_guild=True, manage_webhooks=True, view_channel=True, read_message_history=True, send_messages=True).value)}
        self.net.roles[int(role["id"])] = role
        self.bot._connection.parse_guild_role_create({"guild_id": str(self.guild_id), "role": role})
        manager_id = new_id()
        self.member_json[manager_id] = {"user": {"id": str(manager_id), "username": "manager", "discriminator": "0", "global_name": "manager", "avatar": None, "bot": False}, "roles": [role["id"]],
                                        "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "premium_since": None, "avatar": None, "communication_disabled_until": None}
        self.bot._connection.parse_guild_member_add({"guild_id": str(self.guild_id), **self.member_json[manager_id]})
        self.target._overwrites = [discord.abc._Overwrites({"id": role["id"], "type": 0, "allow": "0", "deny": str(discord.Permissions(send_messages=True).value)})]
        await self.client.close()
        from aiohttp.test_utils import TestClient, TestServer
        from captainguild.web.api import User
        from captainguild.web.app import build_app
        self.client = TestClient(TestServer(build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(manager_id, "manager", None))))
        await self.client.start_server()
        self.message(int(self.text["id"]), "m0")
        job_id = await self.db.insert("INSERT INTO archive_jobs (guild_id, source_channel_id, target_channel_id, source_name, target_name, state, note, started_by, started_by_name, created_at) VALUES (?, ?, ?, 'general', 'logs', 'stopped', 'Stopped by admin.', ?, 'admin', 0)",
                                      (self.guild_id, self.source.id, self.target.id, self.admin_id))
        started = await self.call("POST", "/channelarchive", {"source_channel_id": self.text["id"], "target_channel_id": self.log_channel["id"]})
        resumed = await self.call("POST", f"/channelarchive/{job_id}/resume")
        self.assertEqual((started[0], started[1]["error"]), (400, "You can't post in that channel."))
        self.assertEqual((resumed[0], resumed[1]["error"]), (400, "You can't post in that channel."))
        await self.cog.finish_jobs()
        self.assertEqual((await self.job(job_id))["state"], "stopped")
        self.assertEqual(self.posts(), [])

    async def test_resume_refuses_a_copy_a_newer_one_of_the_same_pair_has_passed(self):
        """Its plan still calls every thread it hadn't reached the first, so it would mark each one's start and end again."""
        for index in range(2):
            self.message(int(self.text["id"]), f"m{index}")
        for name in ("Prints", "Filament"):
            thread = self.thread(self.text, name)
            self.message(int(thread["id"]), f"in {name}")
        actor = self.bot.actor(self.guild.get_member(self.admin_id))
        older = await self.cog.start(self.guild_id, self.source, self.target, actor, run=False)
        original = self.cog.copy_message

        async def copy_then_stop(guild, message, target):
            posted = await original(guild, message, target)
            await self.db.run("UPDATE archive_jobs SET state = 'stopped' WHERE id = ?", (older,))
            return posted
        self.cog.copy_message = copy_then_stop
        self.cog._start(older)
        await self.cog.finish_jobs()
        self.cog.copy_message = original
        newer = await self.cog.start(self.guild_id, self.source, self.target, actor, run=False)
        status, answer = await self.call("POST", f"/channelarchive/{older}/resume")
        self.assertEqual((status, answer["error"]), (409, "A newer copy from #general into #logs is running or has run since this one. Start a new copy to bring what's new."))
        self.cog._start(newer)
        await self.cog.finish_jobs()
        markers = self.markers()
        status, answer = await self.call("POST", f"/channelarchive/{older}/resume")
        await self.cog.finish_jobs()
        self.assertEqual(status, 409)
        self.assertEqual(self.markers(), markers)
        self.assertEqual(len([marker for marker in markers if marker.startswith("**Start of thread archive: Prints")]), 1)
        self.assertEqual((await self.job(older))["state"], "stopped")

    async def test_resume_refuses_without_message_content_as_start_does(self):
        self.message(int(self.text["id"]), "")
        job_id = await self.db.insert("INSERT INTO archive_jobs (guild_id, source_channel_id, target_channel_id, source_name, target_name, state, started_by, started_by_name, created_at) VALUES (?, ?, ?, 'general', 'logs', 'stopped', ?, 'admin', 0)",
                                      (self.guild_id, self.source.id, self.target.id, self.admin_id))
        self.bot.reads_message_text = False
        status, answer = await self.call("POST", f"/channelarchive/{job_id}/resume")
        self.assertEqual((status, answer["error"]), (409, NEEDS_TEXT))
        self.assertEqual((await self.job(job_id))["state"], "stopped")

    async def test_the_page_refuses_a_copy_into_its_own_source(self):
        status, answer = await self.call("POST", "/channelarchive", {"source_channel_id": self.text["id"], "target_channel_id": self.text["id"]})
        self.assertEqual(status, 400)
        self.assertEqual(answer["fields"]["target_channel_id"], "The target can't be the source or one of its threads.")


class BoundsTest(Base):
    """A server's copies are bounded: ten running or waiting, and the newest 50 finished ones kept, with their plans gone once done."""

    async def test_a_server_queues_ten_copies_at_most(self):
        actor = self.bot.actor(self.guild.get_member(self.admin_id))
        for index in range(10):
            await self.db.insert("INSERT INTO archive_jobs (guild_id, source_channel_id, target_channel_id, source_name, target_name, state, created_at) VALUES (?, ?, ?, 'a', 'b', 'running', 0)", (self.guild_id, index + 1, index + 2))
        with self.assertRaises(Exception) as caught:
            await self.cog.start(self.guild_id, self.source, self.target, actor, run=False)
        self.assertIn("10 copies running or waiting", str(caught.exception))

    async def test_a_finished_copy_drops_its_plan_and_old_copies_go(self):
        for index in range(55):
            await self.db.insert("INSERT INTO archive_jobs (guild_id, source_channel_id, target_channel_id, source_name, target_name, state, created_at) VALUES (?, 1, 2, 'a', 'b', 'done', 0)", (self.guild_id,))
        self.message(int(self.text["id"]), "x")
        job_id = await self.copy()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM archive_jobs WHERE guild_id = ?", (self.guild_id,)), 50)
        self.assertIsNotNone(await self.job(job_id), "the newest stays")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM archive_units"), 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM archive_cursors"), 1, "the place stays for the next copy")


class GoneTest(Base):
    """A deleted channel or thread takes its places along, and stops a copy running from or into it."""

    async def test_a_deleted_channel_or_thread_takes_its_places_and_stops_its_copies(self):
        thread = self.thread(self.text, "Prints")
        for source, kind in ((self.source.id, "parent"), (int(thread["id"]), "thread")):
            await self.db.run("INSERT INTO archive_cursors (guild_id, source_channel_id, target_channel_id, kind, last_message_id) VALUES (?, ?, ?, ?, 1)", (self.guild_id, source, self.target.id, kind))
        await self.db.run("INSERT INTO archive_cursors (guild_id, source_channel_id, target_channel_id, kind, last_message_id) VALUES (?, 5, 6, 'parent', 1)", (self.guild_id,))
        running = await self.db.insert("INSERT INTO archive_jobs (guild_id, source_channel_id, target_channel_id, source_name, target_name, state, created_at) VALUES (?, ?, ?, 'general', 'logs', 'running', 0)", (self.guild_id, self.source.id, self.target.id))
        self.bot._connection.parse_thread_delete({"id": thread["id"], "guild_id": str(self.guild_id), "parent_id": self.text["id"], "type": 11})
        await self.settle()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM archive_cursors"), 2)
        self.assertEqual((await self.job(running))["state"], "running", "a thread of the source going leaves the copy running")
        self.bot._connection.parse_channel_delete({**self.log_channel})
        await self.settle()
        self.assertEqual([row["source_channel_id"] for row in await self.db.all("SELECT source_channel_id FROM archive_cursors")], [5])
        job = await self.job(running)
        self.assertEqual((job["state"], job["note"]), ("stopped", "The source or target channel was deleted."))


class ResumeRaceTest(Base):
    """A resume that lands while a stopped copy's task is still on its way out runs once that task ends, instead of being lost."""

    async def test_a_resume_during_a_stop_still_runs(self):
        import asyncio
        leaving = asyncio.Event()
        runs = []

        async def slow_run(job_id):
            runs.append(job_id)
            if len(runs) == 1:
                await leaving.wait()  # the first run is past its last check and hasn't ended yet
        self.cog._run = slow_run
        self.cog._start(7)
        await asyncio.sleep(0)
        self.cog._start(7)  # the resume
        leaving.set()
        await self.cog.finish_jobs()
        self.assertEqual(runs, [7, 7])
