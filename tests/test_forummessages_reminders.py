"""Forum messages: the reminder in a post that goes quiet (AF-14), and the record that keeps a post from getting it twice, through the real
library with only the network replaced."""
from __future__ import annotations

import asyncio
import datetime
import time

import discord

from captainguild.modules import forummessages as fm
from tests.test_forummessages import Recorded

OLD_DEFAULT = ("<@&{helper}>\n\n<@{owner}>, this forum post has been inactive for 6 days. Please consider marking the message that resolved your issue as the "
               "solution if your issue is resolved. If your issue is not resolved, please provide us with an update. Thank you. To mark a message as a solution, "
               "tap and hold or right click on the solution message, go to *Apps*, and select *Mark as Solution*. This will also close your forum post. "
               "***Please do not reply after closing your forum post or it will reopen it!***")


def days_ago(days: float) -> datetime.datetime:
    return discord.utils.utcnow() - datetime.timedelta(days=days)


class ReminderTest(Recorded):
    """AF-14: once an hour, from the moment it connected, the old bot looked at the open posts of each forum with a solved tag. A post without the
    solved tag, not reminded before, whose newest message (or its own start, with none) was more than six days old, got the forum's inactivity
    message, or the default text that pings the helper roles and the author, with every mention allowed. It wrote the post into notified_threads
    after sending."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.configure(message="", inactivity_message="Still stuck, {{OP.Mention}}? <@&%s> can help." % self.helper_id)

    def quiet_post(self, quiet_days: float = 7, *, age_days: float = 8, **post) -> dict:
        """A post started `age_days` ago whose newest message came `quiet_days` ago."""
        thread = self.post(created=days_ago(age_days), **post)
        self.say(thread)
        if quiet_days < age_days:
            self.say(thread, message_id=discord.utils.time_snowflake(days_ago(quiet_days)) + 1, content="Tried that.")
        return thread

    async def reminded(self) -> list[int]:
        return [row["thread_id"] for row in await self.db.all("SELECT thread_id FROM forum_reminders WHERE guild_id = ? ORDER BY thread_id", (self.guild_id,))]

    async def round(self) -> None:
        self.sent.clear()
        await self.cog.remind_round()
        await self.settle()

    async def test_a_post_quiet_for_six_days_gets_the_reminder_once(self):
        thread = self.quiet_post()
        await self.round()
        self.assert_no_errors()
        [body] = self.sent_to(thread["id"])
        self.assertEqual(body["content"], f"Still stuck, <@{self.member_id}>? <@&{self.helper_id}> can help.")
        self.assertEqual(body["allowed_mentions"], {"parse": [], "users": [self.member_id], "roles": [self.helper_id]})
        self.assertEqual(await self.reminded(), [int(thread["id"])])
        self.assertIn(f"Inactivity ping sent in thread <#{thread['id']}> for forum channel <#{self.forum_id}>", await self.log_lines())
        await self.round()
        self.assertEqual(self.sent_to(thread["id"]), [], "never twice")

    async def test_the_default_text_pings_the_helper_roles_and_the_author(self):
        await self.configure(message="", inactivity_message="")
        thread = self.quiet_post()
        await self.round()
        [body] = self.sent_to(thread["id"])
        self.assertEqual(body["content"], OLD_DEFAULT.format(helper=self.helper_id, owner=self.member_id))
        self.assertEqual(body["allowed_mentions"], {"parse": [], "users": [self.member_id], "roles": [self.helper_id]})

    async def test_recent_activity_or_a_young_post_gets_nothing(self):
        busy = self.quiet_post(2)
        young = self.quiet_post(5.9, age_days=5.9)
        edge = self.quiet_post(5.99)
        await self.round()
        for thread in (busy, young, edge):
            self.assertEqual(self.sent_to(thread["id"]), [], thread["name"])
        self.assertEqual(await self.reminded(), [])

    async def test_a_post_with_no_messages_counts_from_its_start(self):
        thread = self.post(created=days_ago(7))
        await self.round()
        self.assertEqual(len(self.sent_to(thread["id"])), 1)

    async def test_solved_reminded_and_closed_posts_are_left_alone(self):
        solved = self.quiet_post(tags=(self.tags["Solved"],))
        before = self.quiet_post()
        await self.db.run("INSERT INTO forum_reminders (guild_id, thread_id, channel_id, reminded_at) VALUES (?, ?, ?, ?)", (self.guild_id, int(before["id"]), self.forum_id, time.time()))
        closed = self.quiet_post(archived=True)
        await self.round()
        for thread in (solved, before, closed):
            self.assertEqual(self.sent_to(thread["id"]), [], thread["id"])

    async def test_a_forum_with_no_solved_tag_or_the_pack_off_gets_none(self):
        await self.configure(message="", solved_tag=None)
        thread = self.quiet_post()
        await self.round()
        self.assertEqual(self.sent_to(thread["id"]), [])
        await self.configure(message="")
        await self.switch(False)
        await self.round()
        self.assertEqual(self.sent_to(thread["id"]), [])

    async def test_an_author_who_left_is_logged_once_and_not_reminded(self):
        gone = 1300000000000000555
        self.member_json[gone] = {"user": {"id": str(gone), "username": "gone", "discriminator": "0", "global_name": None, "avatar": None, "bot": False}}
        thread = self.post(gone, created=days_ago(8))
        del self.member_json[gone]
        await self.round()
        await self.round()
        line = f"Failed to send inactivity ping in thread <#{thread['id']}>: Thread owner is no longer a member."
        self.assertEqual((await self.log_lines()).count(line), 1, "the old bot wrote this every hour until the post closed")
        self.assertEqual(await self.reminded(), [])

    async def test_a_refused_reminder_is_tried_again_the_next_hour(self):
        thread = self.quiet_post()
        real = self.bot.http.request
        self.refuse_posts(403)
        await self.round()
        self.assertIn(f"Failed to send inactivity ping in thread <#{thread['id']}> due to missing permissions.", await self.log_lines())
        self.assertEqual(await self.reminded(), [])
        self.bot.http.request = real
        await self.round()
        self.assertEqual(len(self.sent_to(thread["id"])), 1)
        self.assertEqual(await self.reminded(), [int(thread["id"])])

    async def test_two_rounds_at_once_send_one_reminder(self):
        thread = self.quiet_post()
        self.sent.clear()
        await asyncio.gather(self.cog.remind_round(), self.cog.remind_round())
        await self.settle()
        self.assertEqual(len(self.sent_to(thread["id"])), 1)

    async def test_the_check_runs_every_hour(self):
        row = await self.db.one("SELECT due_at FROM timers WHERE kind = ? AND key = ?", (fm.REMINDER_TIMER, fm.REMINDER_TIMER))
        self.assertIsNotNone(row, "the first check is scheduled when the pack loads")
        thread = self.quiet_post()
        await self.cog._reminders_due({}, {})
        await self.cog.finish_round()
        await self.settle()
        self.assertEqual(len(self.sent_to(thread["id"])), 1)
        due = await self.db.scalar("SELECT due_at FROM timers WHERE kind = ? AND key = ?", (fm.REMINDER_TIMER, fm.REMINDER_TIMER))
        self.assertAlmostEqual(due, time.time() + 3600, delta=60)

    async def test_a_deleted_post_or_forum_takes_its_rows_along(self):
        await self.configure()
        thread = self.quiet_post()
        await self.round()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM forum_welcomes WHERE thread_id = ?", (int(thread["id"]),)), 1)
        self.state.parse_thread_delete({"id": thread["id"], "guild_id": str(self.guild_id), "parent_id": self.forum["id"], "type": 11})
        await self.settle()
        for table in ("forum_welcomes", "forum_reminders"):
            self.assertEqual(await self.db.scalar(f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", (int(thread["id"]),)), 0, table)
        other = self.quiet_post()
        await self.round()
        self.state.parse_channel_delete(self.forum)
        await self.settle()
        for table in ("forum_welcomes", "forum_reminders"):
            self.assertEqual(await self.db.scalar(f"SELECT COUNT(*) FROM {table} WHERE channel_id = ?", (self.forum_id,)), 0, table)
        self.assertNotIn(int(other["id"]), await self.reminded())


class OneCheckTest(Recorded):
    """AF-16: the old bot's on_ready registered the button, synced its commands, posted "Registered SolvedView for persistent button
    interactions." in every server's log channel and started another hourly loop, at every ready, reconnects included. Two loops could then
    ping one post before either saved it."""

    async def test_one_timer_and_one_look_at_a_time(self):
        await self.cog._reminders_due({}, {})
        await self.cog.finish_round()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = ?", (fm.REMINDER_TIMER,)), 1)
        started: list[int] = []
        gate = asyncio.Event()

        async def slow_round() -> None:
            started.append(1)
            await gate.wait()
        self.cog.remind_round = slow_round
        await self.cog._reminders_due({}, {})
        await asyncio.sleep(0)
        await self.cog._reminders_due({}, {})
        gate.set()
        await self.cog.finish_round()
        self.assertEqual(len(started), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = ?", (fm.REMINDER_TIMER,)), 1)

    async def test_nothing_is_posted_at_a_start(self):
        await self.configure()
        await self.cog.finish_log()
        self.assertEqual(self.posted_in(self.log_id), [])
