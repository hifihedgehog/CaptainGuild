"""Forum messages through the real library with only the network replaced: the settings, the welcome on a new post, the Mark as Solved
button, the Mark as Solution menu and the reminder on a quiet post. The behavior IDs (AF-1 and so on) are the old bot's."""
from __future__ import annotations

import asyncio
import datetime
import itertools
from typing import Any

import discord

from captainguild.core.validate import ValidationError
from captainguild.modules import forummessages as fm
from captainguild.modules.forummessages import ForumMessages
from tests.gateway import FakeHTTPResponse, iso
from tests.voice_report_harness import Harness

_spread = itertools.count(1)


class Case(Harness):
    modules = [ForumMessages]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cog: ForumMessages = self.bot.module("forummessages")
        usual = discord.Permissions(view_channel=True, send_messages=True, send_messages_in_threads=True, read_message_history=True, use_application_commands=True)
        self.role_everyone["permissions"] = str(usual.value)
        self.state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": self.role_everyone})
        self.helper = self.add_role("Helper", 2)
        self.helper_id = int(self.helper["id"])
        self.tags = {name: 1200000000000000000 + number for number, name in enumerate(("Solved", "Question", "Problem", "Other"), start=1)}
        self.forum["available_tags"] = [{"id": str(tag_id), "name": name, "moderated": name == "Other", "emoji_id": None, "emoji_name": None} for name, tag_id in self.tags.items()]
        self.state.parse_channel_update(self.forum)
        self.forum_id = int(self.forum["id"])
        self.log_id = int(self.log_channel["id"])
        await self.switch(True)

    async def settle(self, seconds: float = 8.0) -> None:
        """The library's work, then the pack's own: a welcome and a reminder look run in tasks of the pack's, which the library's settle doesn't see."""
        for _ in range(50):
            await super().settle(seconds)
            cog = getattr(self, "cog", None)
            waiting = [task for task in (cog._tasks if cog is not None else ()) if not task.done()]
            if not waiting:
                return
            await asyncio.wait(waiting, timeout=seconds)

    async def switch(self, on: bool) -> None:
        await self.bot.settings.save(self.guild_id, "forummessages", {"enabled": on})
        await self.bot.sync_server_commands(self.guild_id)

    async def configure(self, **fields: Any) -> dict:
        """The forum set up the way the old bot's file held it, with the log channel."""
        row = {"forum_id": self.forum_id, "message": "Hey {{OP.Mention}}, welcome. <@&%s>, have a look." % self.helper_id, "inactivity_message": "",
               "solved_tag": self.tags["Solved"], "helper_roles": [self.helper_id], **fields}
        await self.bot.settings.save(self.guild_id, "forummessages", {"forums": [row], "log_channel": self.log_id})
        return row

    def settings(self) -> dict:
        return self.bot.settings.get(self.guild_id, "forummessages")

    # ---- posts ----

    def post(self, user_id: int | None = None, *, name: str = "Printer won't heat", tags: tuple = (), created: datetime.datetime | None = None,
             last: int | None = None, archived: bool = False, locked: bool = False, parent: dict | None = None) -> dict:
        """A new forum post, as Discord sends its thread. Its ID carries the moment it was made."""
        user_id = user_id or self.member_id
        parent = parent or self.forum
        moment = created or discord.utils.utcnow()
        thread_id = discord.utils.time_snowflake(moment) + next(_spread) * 1000  # room for a test to number a post's messages after its ID
        data = {"id": str(thread_id), "guild_id": str(self.guild_id), "parent_id": parent["id"], "owner_id": str(user_id), "name": name, "type": 11,
                "message_count": 1, "member_count": 1, "rate_limit_per_user": 0, "flags": 0, "applied_tags": [str(tag) for tag in tags],
                "last_message_id": str(last) if last else None,
                "thread_metadata": {"archived": archived, "auto_archive_duration": 10080, "archive_timestamp": iso(moment), "locked": locked, "create_timestamp": iso(moment)}}
        self.net.channels[thread_id] = data
        self.state.parse_thread_create({**data, "newly_created": True})
        return data

    def say(self, thread: dict, user_id: int | None = None, *, message_id: int | None = None, content: str = "It won't heat past 180.", member: bool = True) -> dict:
        """A message in a post. With no ID given, the first one, whose ID is the post's, as a forum post's first message has. A message with no
        member data comes from outside the server's member list, as a webhook's does."""
        author = self.member_json[user_id or int(thread["owner_id"])]
        payload = {"id": str(message_id or thread["id"]), "channel_id": thread["id"], "guild_id": str(self.guild_id), "author": author["user"],
                   "content": content, "timestamp": iso(), "edited_timestamp": None, "tts": False,
                   "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [], "embeds": [], "pinned": False, "type": 0}
        if member:
            payload["member"] = {k: v for k, v in author.items() if k != "user"}
        self.net.messages[int(payload["id"])] = dict(payload)
        self.state.parse_message_create(payload)
        return payload

    def posted_in(self, channel_id: int | str) -> list[dict]:
        """Messages the bot posted in a channel or post, in order. The private answers to interactions carry flag 64 and stay out."""
        return [message for message in self.posts_in(channel_id) if message["author"]["id"] == self.net.bot_user["id"] and not (message.get("flags") or 0) & 64]

    async def log_lines(self) -> list[str]:
        await self.cog.finish_log()
        return [line for message in self.posted_in(self.log_id) for line in (message.get("content") or "").split("\n")]


class SettingsTest(Case):
    """AF-18: the old bot kept each forum's message, inactivity message, solved tag and helper roles, and the log channel, in its file. Here
    they're the pack's settings, and what it remembers about posts is in its tables."""

    async def test_the_pack_starts_switched_off(self):
        self.assertEqual(ForumMessages.schema.defaults(), {"enabled": False, "log_channel": None, "forums": []})
        self.assertTrue(ForumMessages.command_pack)

    async def test_a_forum_row_keeps_its_text_exactly(self):
        text = "  Hello {{OP.Mention}}\n\n**Bold** line  \n"
        await self.configure(message=text, inactivity_message="Still there, {{OP.Mention}}?")
        row = self.settings()["forums"][0]
        self.assertEqual(row["message"], text)
        self.assertEqual(row["inactivity_message"], "Still there, {{OP.Mention}}?")
        self.assertEqual((row["solved_tag"], row["helper_roles"]), (self.tags["Solved"], [self.helper_id]))

    async def test_space_alone_is_no_text_and_text_past_discords_limit_is_refused(self):
        await self.configure(message=" \n ", inactivity_message="")
        self.assertEqual(self.settings()["forums"][0]["message"], "")
        with self.assertRaises(ValidationError) as caught:
            await self.configure(message="x" * 2001)
        self.assertIn("2000", caught.exception.fields["forums"])
        await self.configure(message="x" * 2000)

    async def test_a_forum_listed_twice_is_refused(self):
        row = await self.configure()
        with self.assertRaises(ValidationError) as caught:
            await self.bot.settings.save(self.guild_id, "forummessages", {"forums": [row, dict(row)]})
        self.assertIn("twice", caught.exception.fields["forums"])

    async def test_its_tables_keep_each_post_once_per_server(self):
        for table in ("forum_welcomes", "forum_reminders"):
            columns = [row["name"] for row in await self.db.all(f"PRAGMA table_info('{table}')")]
            self.assertEqual(columns[:3], ["guild_id", "thread_id", "channel_id"], table)
            keys = [row["name"] for row in await self.db.all(f"PRAGMA table_info('{table}')") if row["pk"]]
            self.assertEqual(keys, ["guild_id", "thread_id"], table)


class Recorded(Case):
    """Remembers where each message the bot sent went and with what, and can make Discord refuse some of them."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.sent: list[tuple[int, dict]] = []
        inner = self.bot.http.request

        async def recording(route: Any, *, files: Any = None, form: Any = None, **kwargs: Any) -> Any:
            if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
                self.sent.append((int(route.channel_id), self.net._body({**kwargs, "form": form})))
            return await inner(route, files=files, form=form, **kwargs)
        self.bot.http.request = recording

    def sent_to(self, channel_id: int | str) -> list[dict]:
        return [body for place, body in self.sent if place == int(channel_id)]

    def refuse_posts(self, status: int, *, with_buttons_only: bool = False) -> None:
        """Discord refuses messages the bot posts outside the log channel, or only those that carry a button."""
        inner = self.bot.http.request

        async def refusing(route: Any, *, files: Any = None, form: Any = None, **kwargs: Any) -> Any:
            body = self.net._body({**kwargs, "form": form})
            if route.method == "POST" and route.path == "/channels/{channel_id}/messages" and int(route.channel_id) != self.log_id and (body.get("components") or not with_buttons_only):
                kind = discord.Forbidden if status == 403 else discord.HTTPException
                raise kind(FakeHTTPResponse(status), {"code": 50013 if status == 403 else 0, "message": "Missing Permissions" if status == 403 else "refused for a test"})
            return await inner(route, files=files, form=form, **kwargs)
        self.bot.http.request = refusing

    @staticmethod
    def button(body: dict) -> dict | None:
        for row in body.get("components") or []:
            for part in row.get("components") or []:
                return part
        return None


class WelcomeTest(Recorded):
    """AF-13: a new post in a forum on the list gets the forum's message, posted once its author's first message is in. The old bot waited up to
    ten minutes for that message from the post's author, then posted in the post with the library's default mentions, so the author and any role
    the text named were pinged. It skipped posts outside forums and forums with no settings."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.configure()

    async def test_the_first_message_brings_the_welcome_with_the_button(self):
        thread = self.post()
        await self.settle()
        self.assertEqual(self.sent_to(thread["id"]), [], "nothing before the post's first message is in")
        self.say(thread)
        await self.settle()
        self.assert_no_errors()
        [body] = self.sent_to(thread["id"])
        self.assertEqual(body["content"], f"Hey <@{self.member_id}>, welcome. <@&{self.helper_id}>, have a look.")
        button = self.button(body)
        self.assertEqual((button["label"], button["custom_id"], button["style"]), ("Mark as Solved", fm.SOLVED_ID, 3))
        self.assertEqual(body["allowed_mentions"], {"parse": [], "users": [self.member_id], "roles": [self.helper_id]})
        self.assertIn(f"Automated message with Solved button sent in thread <#{thread['id']}> for forum channel <#{self.forum_id}>", await self.log_lines())

    async def test_a_post_gets_it_once(self):
        thread = self.post()
        self.say(thread)
        await self.settle()
        self.say(thread)  # the same first message again, as a replay would bring it
        self.say(thread, message_id=int(thread["id"]) + 5, content="Any ideas?")
        await self.settle()
        self.assertEqual(len(self.sent_to(thread["id"])), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM forum_welcomes WHERE guild_id = ? AND thread_id = ?", (self.guild_id, int(thread["id"]))), 1)

    async def test_no_solved_tag_means_no_button(self):
        await self.configure(solved_tag=None)
        thread = self.post()
        self.say(thread)
        await self.settle()
        [body] = self.sent_to(thread["id"])
        self.assertIsNone(self.button(body))
        self.assertIn(f"Automated message sent without Solved button in thread <#{thread['id']}> for forum channel <#{self.forum_id}> (no solved tag configured)", await self.log_lines())

    async def test_every_placeholder_fills_in(self):
        names = ["ID", "Name", "Username", "Discriminator", "GlobalName", "Mention", "CreatedAt", "JoinedAt", "Avatar", "Bot"]
        await self.configure(message="\n".join(f"{name}={{{{OP.{name}}}}}" for name in names))
        thread = self.post()
        self.say(thread)
        await self.settle()
        member = self.guild.get_member(self.member_id)
        filled = dict(line.split("=", 1) for line in self.sent_to(thread["id"])[0]["content"].split("\n"))
        self.assertEqual(filled, {
            "ID": str(self.member_id), "Name": "member", "Username": "member", "Discriminator": "0", "GlobalName": "member", "Mention": f"<@{self.member_id}>",
            "CreatedAt": discord.utils.format_dt(member.created_at, "R"), "JoinedAt": discord.utils.format_dt(member.joined_at, "R"),
            "Avatar": member.display_avatar.url, "Bot": "False",
        })

    async def test_what_a_name_holds_pings_nobody_and_shows_as_written(self):
        """A name can't add a ping or markdown, and a placeholder's text isn't read as another placeholder."""
        self.member_json[self.member_id]["user"]["global_name"] = "@everyone <@&%s> _x_ {{OP.ID}}" % self.helper_id
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **self.member_json[self.member_id]})
        await self.configure(message="Hi {{OP.GlobalName}}")
        thread = self.post()
        self.say(thread)
        await self.settle()
        [body] = self.sent_to(thread["id"])
        self.assertEqual(body["content"], "Hi @everyone <@&%s> \\_x\\_ {{OP.ID}}" % self.helper_id)
        self.assertEqual(body["allowed_mentions"], {"parse": [], "users": [self.member_id], "roles": []})

    async def test_everyone_in_the_servers_own_text_pings(self):
        await self.configure(message="@everyone, a new post from {{OP.Mention}}")
        thread = self.post()
        self.say(thread)
        await self.settle()
        self.assertEqual(self.sent_to(thread["id"])[0]["allowed_mentions"]["parse"], ["everyone"])

    async def test_a_welcome_past_discords_limit_splits_between_lines_with_the_button_last(self):
        first = "a" * 1500
        template = f"{first}\n{'b' * 482} {{{{OP.Mention}}}}"
        self.assertEqual(len(template), 1998)
        await self.configure(message=template)
        thread = self.post()
        self.say(thread)
        await self.settle()
        bodies = self.sent_to(thread["id"])
        self.assertEqual([body["content"] for body in bodies], [first, f"{'b' * 482} <@{self.member_id}>"])
        self.assertIsNone(self.button(bodies[0]))
        self.assertIsNotNone(self.button(bodies[1]))

    async def test_a_media_channel_is_a_forum_too(self):
        self.forum["type"] = 16
        self.state.parse_channel_update(self.forum)
        thread = self.post()
        self.say(thread)
        await self.settle()
        self.assertEqual(len(self.sent_to(thread["id"])), 1)

    async def test_posts_it_isnt_set_up_for_are_left_alone(self):
        other = {**self.forum, "id": str(self.forum_id + 7), "name": "showcase"}
        self.net.channels[int(other["id"])] = other
        self.state.parse_channel_create(other)
        self.say(self.post(parent=other))
        self.say(self.post(parent=self.text))
        self.say({"id": self.text["id"], "owner_id": str(self.member_id)})  # a channel's own ID on a message is no forum post
        await self.settle()
        self.assertEqual(self.sent, [])
        self.assert_no_errors()

    async def test_nothing_with_the_pack_off_or_no_message_set(self):
        await self.switch(False)
        thread = self.post()
        self.say(thread)
        await self.settle()
        await self.switch(True)
        await self.configure(message="")
        other = self.post()
        self.say(other)
        await self.settle()
        self.assertEqual(self.sent, [])
        self.assert_no_errors()

    async def test_a_refused_button_still_posts_the_text(self):
        for status, line in ((403, " due to missing permissions."), (500, ": 500 fake (error code: 0): refused for a test")):
            self.refuse_posts(status, with_buttons_only=True)
            thread = self.post()
            self.say(thread)
            await self.settle()
            [body] = self.sent_to(thread["id"])
            self.assertIsNone(self.button(body), status)
            self.assertIn(f"Failed to send Solved button in thread <#{thread['id']}>{line}", await self.log_lines(), status)

    async def test_a_refused_welcome_is_logged_and_left_to_try_again(self):
        self.refuse_posts(403)
        thread = self.post()
        self.say(thread)
        await self.settle()
        self.assertIn(f"Failed to send automated message in thread <#{thread['id']}> due to missing permissions.", await self.log_lines())
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM forum_welcomes"), 0, "/check_recent_threads can post it later")

    async def test_an_author_who_has_left_is_logged(self):
        gone = 1300000000000000777
        self.member_json[gone] = {"user": {"id": str(gone), "username": "gone", "discriminator": "0", "global_name": None, "avatar": None, "bot": False}}
        thread = self.post(gone)
        self.say(thread, member=False)
        del self.member_json[gone]  # the fake network answers from the same list, so Discord now says they're gone
        await self.settle()
        self.assertEqual(self.sent_to(thread["id"]), [])
        self.assertIn(f"Failed to fetch thread owner in thread <#{thread['id']}>: Owner is no longer a member.", await self.log_lines())


class LogTest(Recorded):
    """AF-8: every action goes to the log channel as plain text that pings nobody. Lines that arrive together share a message."""

    async def test_lines_go_out_in_order_pinging_nobody(self):
        await self.configure()
        for number in range(3):
            self.cog.log_line(self.guild_id, f"line {number} <@{self.member_id}> <@&{self.helper_id}>")
        lines = await self.log_lines()
        self.assertEqual(lines, [f"line {number} <@{self.member_id}> <@&{self.helper_id}>" for number in range(3)])
        for body in self.sent_to(self.log_id):
            self.assertEqual(body["allowed_mentions"], {"parse": []})

    async def test_no_log_channel_posts_nothing(self):
        await self.configure()
        await self.bot.settings.save(self.guild_id, "forummessages", {"log_channel": None})
        self.cog.log_line(self.guild_id, "nowhere")
        await self.cog.finish_log()
        self.assertEqual(self.sent, [])
