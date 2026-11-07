"""Entry giveaways: one entry per member in a channel, a thread or each post of a forum, through the real library with only the network replaced."""
from __future__ import annotations

import asyncio
import datetime
import re
import time

import discord

from captainguild.core.module import required_permissions
from captainguild.core.ops import DiscordOps
from captainguild.modules.entrygiveaways import BOT_WARNING, MULTI, OVERALL, OVERALL_CHOICE, SINGLE, EntryGiveaways, _Place
from captainguild.modules.mydata import MyData
from tests.gateway import FakeHTTPResponse, GatewayTest, iso, new_id, user_json


class Shown:
    """A reply a command sent, which it may edit later."""

    def __init__(self, embed: discord.Embed | None) -> None:
        self.edits: list[str] = []
        self.embed = embed

    async def edit(self, *, embed: discord.Embed) -> None:
        self.edits.append(embed.description)


class Ctx:
    """What the commands read from their context, over the real server."""

    def __init__(self, test: "Base", author_id: int, channel: discord.abc.GuildChannel | None = None) -> None:
        self.bot = test.bot
        self.guild = test.guild
        self.author = test.guild.get_member(author_id)
        self.channel = channel or test.guild.get_channel(int(test.text["id"]))
        self.interaction = None
        self.sent: list[dict] = []
        self.fails: list[str] = []
        self.shown: list[Shown] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        shown = Shown(kwargs.get("embed"))
        self.shown.append(shown)
        return shown

    async def fail(self, text, **kwargs):
        self.fails.append(text)

    async def defer(self, **kwargs):
        pass

    def said(self) -> list[str]:
        return [sent["content"] for sent in self.sent if sent["content"]] + self.fails


class Base(GatewayTest):
    modules = [EntryGiveaways]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cog = self.bot.module("entrygiveaways")
        await self.bot.settings.save(self.guild_id, "entrygiveaways", {"enabled": True})
        self.channel = self.guild.get_channel(int(self.text["id"]))
        self.forum_channel = self.guild.get_channel(int(self.forum["id"]))

    async def asyncTearDown(self) -> None:
        await self.cog.finish_deletions()
        await super().asyncTearDown()

    def everyone_reads(self) -> None:
        """Gives @everyone View Channel, Send Messages and Read Message History, so a plain member sees the channels and can't manage them."""
        allowed = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True)
        self.bot._connection.parse_guild_role_update({"guild_id": str(self.guild_id), "role": {**self.role_everyone, "permissions": str(allowed.value)}})

    def unknown_channels_are_gone(self) -> None:
        """Discord answers 404 for a channel it doesn't have. The fake network makes one up otherwise."""
        original = self.net.http

        async def strict(route, **kwargs):
            if route.method == "GET" and route.path == "/channels/{channel_id}" and int(route.channel_id) not in self.net.channels:
                raise discord.NotFound(FakeHTTPResponse(404), {"code": 10003, "message": "Unknown Channel"})
            return await original(route, **kwargs)
        self.bot.http.request = strict

    def person(self, name: str, *, bot: bool = False, roles: list[str] | None = None) -> int:
        user_id = new_id()
        data = {"user": user_json(user_id, name, bot=bot), "roles": roles or [self.role_member["id"]], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "premium_since": None, "avatar": None, "communication_disabled_until": None}
        self.member_json[user_id] = data
        self.bot._connection.parse_guild_member_add({**data, "guild_id": str(self.guild_id)})
        return user_id

    def thread(self, parent: dict, name: str = "Win a printer") -> discord.Thread:
        data = {"id": str(new_id()), "guild_id": str(self.guild_id), "parent_id": parent["id"], "owner_id": str(self.admin_id), "name": name, "type": 11, "message_count": 0, "member_count": 1,
                "rate_limit_per_user": 0, "flags": 0, "thread_metadata": {"archived": False, "auto_archive_duration": 1440, "archive_timestamp": iso(), "locked": False}}
        self.net.channels[int(data["id"])] = data
        self.bot._connection.parse_thread_create(data)
        return self.guild.get_thread(int(data["id"]))

    def say(self, user_id: int, place: discord.abc.Snowflake | None = None, *, message_id: int | None = None, kind: int = 0, webhook: int | None = None, application: int | None = None) -> dict:
        place_id = str(place.id) if place is not None else self.text["id"]
        author = self.member_json[user_id]
        payload = {
            "id": str(message_id or new_id()), "channel_id": place_id, "guild_id": str(self.guild_id), "author": author["user"],
            "member": {k: v for k, v in author.items() if k != "user"}, "content": "I'd like to enter", "timestamp": iso(), "edited_timestamp": None, "tts": False,
            "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [], "embeds": [], "pinned": False, "type": kind,
        }
        if webhook:
            payload["webhook_id"] = str(webhook)
            payload.pop("member")
        if application:
            payload["application_id"] = str(application)
        self.net.messages[int(payload["id"])] = {**payload, "guild_id": str(self.guild_id)}
        self.bot._connection.parse_message_create(payload)
        return payload

    async def entries(self) -> list[tuple[int, int, int]]:
        rows = await self.db.all("SELECT channel_id, user_id, message_id FROM entry_giveaway_entries WHERE guild_id = ? ORDER BY message_id", (self.guild_id,))
        return [(row["channel_id"], row["user_id"], row["message_id"]) for row in rows]

    def deletes(self) -> int:
        return len([call for call in self.net.calls if call[0] == "DELETE" and call[1] == "/channels/{channel_id}/messages/{message_id}"])

    def posted(self) -> list[str]:
        return [body.get("content") or "" for method, path, body in self.net.calls if method == "POST" and path == "/channels/{channel_id}/messages"]

    async def set_place(self, place) -> None:
        await self.cog.set_place(self.guild_id, place, self.bot.actor(self.guild.get_member(self.admin_id)))

    async def invoke(self, command, ctx, *args) -> Ctx:
        await getattr(self.cog, command).callback(self.cog, ctx, *args)
        await self.settle()
        return ctx


class EnforcementTest(Base):
    """GA-7 to GA-11: the first message is the entry, later ones go, bots go, staff and closed giveaways are left alone."""

    async def test_the_first_message_is_the_entry_and_a_second_is_deleted_with_a_warning(self):
        await self.set_place(self.channel)
        first = self.say(self.member_id)
        second = self.say(self.member_id)
        await self.settle()
        await self.cog.finish_deletions()
        self.assertEqual(await self.entries(), [(self.channel.id, self.member_id, int(first["id"]))])
        self.assertNotIn(int(second["id"]), self.net.messages)
        self.assertIn(int(first["id"]), self.net.messages)
        self.assertEqual(self.posted(), [f"<@{self.member_id}> Only one entry per giveaway is allowed! Duplicate message deleted."])
        warning = next(body for method, path, body in self.net.calls if method == "POST" and path == "/channels/{channel_id}/messages")
        self.assertEqual(warning["allowed_mentions"]["users"], [self.member_id])
        self.assert_no_errors()

    async def test_the_warning_goes_once_in_twenty_seconds_a_person(self):
        await self.set_place(self.channel)
        for _ in range(4):
            self.say(self.member_id)
        await self.settle()
        await self.cog.finish_deletions()
        self.assertEqual(len(self.posted()), 1)
        self.assertEqual(self.deletes(), 3)
        clock = [1000.0]
        self.cog._clock = lambda: clock[0]
        self.cog._warned_people.clear()
        self.say(self.member_id)
        await self.settle()
        clock[0] += 21
        self.say(self.member_id)
        await self.settle()
        self.assertEqual(len(self.posted()), 3)

    async def test_staff_post_freely_and_are_never_an_entry(self):
        await self.bot.settings.save(self.guild_id, "entrygiveaways", {"staff_role": int(self.role_mod["id"])})
        await self.set_place(self.channel)
        for _ in range(3):
            self.say(self.mod_id)
        await self.settle()
        await self.cog.finish_deletions()
        self.assertEqual(await self.entries(), [])
        self.assertEqual(self.deletes(), 0)

    async def test_other_bots_and_webhooks_are_deleted_with_one_warning_and_the_bots_own_posts_stay(self):
        robot = self.person("other bot", bot=True)
        await self.set_place(self.channel)
        self.say(robot)
        self.say(robot)
        self.say(self.member_id, webhook=new_id())
        own = self.say(self.member_id, webhook=new_id(), application=self.bot_id)
        mine = self.say(self.bot_id)
        await self.settle()
        await self.cog.finish_deletions()
        self.assertEqual(self.deletes(), 3)
        self.assertIn(int(own["id"]), self.net.messages)
        self.assertIn(int(mine["id"]), self.net.messages)
        self.assertEqual(self.posted(), [BOT_WARNING], "once in 30 seconds a channel")
        self.assertEqual(await self.entries(), [], "a bot's message is never an entry")

    async def test_messages_elsewhere_system_lines_and_a_closed_giveaway_are_left_alone(self):
        await self.set_place(self.channel)
        other = self.guild.get_channel(int(self.log_channel["id"]))
        self.say(self.member_id, other)
        self.say(self.member_id, other)
        self.say(self.member_id, kind=6)  # a pin notice
        await self.settle()
        self.assertEqual(await self.entries(), [])
        await self.cog.close(self.guild_id, self.channel.id, None)
        self.say(self.member_id)
        self.say(self.member_id)
        await self.settle()
        await self.cog.finish_deletions()
        self.assertEqual(await self.entries(), [])
        self.assertEqual(self.deletes(), 0)

    async def test_nothing_happens_with_the_pack_switched_off(self):
        await self.set_place(self.channel)
        await self.bot.settings.save(self.guild_id, "entrygiveaways", {"enabled": False})
        self.say(self.member_id)
        self.say(self.member_id)
        await self.settle()
        await self.cog.finish_deletions()
        self.assertEqual((await self.entries(), self.deletes()), ([], 0))

    async def test_a_forum_takes_one_entry_a_post_and_the_post_itself_is_no_entry(self):
        await self.set_place(self.forum_channel)
        first, second = self.thread(self.forum, "Printer"), self.thread(self.forum, "Filament")
        starter = self.say(self.member_id, first, message_id=first.id)  # someone who isn't staff opened this post
        entry = self.say(self.member_id, first)
        self.say(self.member_id, first)
        other = self.say(self.member_id, second)
        await self.settle()
        await self.cog.finish_deletions()
        self.assertEqual(await self.entries(), [(first.id, self.member_id, int(entry["id"])), (second.id, self.member_id, int(other["id"]))])
        self.assertIn(int(starter["id"]), self.net.messages)
        self.assertEqual(self.deletes(), 1)

    async def test_a_deleted_entry_lets_the_person_enter_again(self):
        await self.set_place(self.channel)
        first = self.say(self.member_id)
        await self.settle()
        self.bot._connection.parse_message_delete({"id": first["id"], "channel_id": self.text["id"], "guild_id": str(self.guild_id)})
        await self.settle()
        self.assertEqual(await self.entries(), [])
        again = self.say(self.member_id)
        await self.settle()
        self.assertEqual(await self.entries(), [(self.channel.id, self.member_id, int(again["id"]))])
        self.bot._connection.parse_message_delete_bulk({"ids": [again["id"]], "channel_id": self.text["id"], "guild_id": str(self.guild_id)})
        await self.settle()
        self.assertEqual(await self.entries(), [], "a purge frees them too")

    async def test_an_entry_deleted_while_the_bot_was_away_lets_the_next_message_in(self):
        """Discord doesn't replay a delete to a new session, so an entry made before it is checked once before a second one is deleted."""
        await self.set_place(self.channel)
        kept, gone = self.person("kept"), self.person("gone")
        kept_entry, gone_entry = self.say(kept), self.say(gone)
        await self.settle()
        self.net.messages.pop(int(gone_entry["id"]))  # deleted while the bot was away, with no event
        await self.cog.on_connect()  # a new session
        replaced = self.say(gone)
        second = self.say(kept)
        await self.settle()
        await self.cog.finish_deletions()
        self.assertEqual(await self.entries(), sorted([(self.channel.id, kept, int(kept_entry["id"])), (self.channel.id, gone, int(replaced["id"]))], key=lambda row: row[2]))
        self.assertIn(int(replaced["id"]), self.net.messages)
        self.assertNotIn(int(second["id"]), self.net.messages, "an entry still there makes the next message a second one")
        self.assertEqual(self.posted(), [f"<@{kept}> Only one entry per giveaway is allowed! Duplicate message deleted."])
        fetched = [path for method, path, _ in self.net.calls if method == "GET" and path == "/channels/{channel_id}/messages/{message_id}"]
        again = self.say(kept)
        await self.settle()
        await self.cog.finish_deletions()
        self.assertEqual(len([path for method, path, _ in self.net.calls if method == "GET" and path == "/channels/{channel_id}/messages/{message_id}"]), len(fetched), "each member's entry is checked once a session")
        self.assertNotIn(int(again["id"]), self.net.messages)

    async def test_the_deletion_worker_skips_what_is_gone_and_keeps_going(self):
        await self.set_place(self.channel)
        original = self.net.http
        first_delete = []

        async def flaky(route, **kwargs):
            if route.method == "DELETE" and not first_delete:
                first_delete.append(1)
                raise discord.NotFound(FakeHTTPResponse(404), {"code": 10008, "message": "Unknown Message"})
            if route.method == "DELETE" and len(first_delete) == 1:
                first_delete.append(2)
                raise discord.HTTPException(FakeHTTPResponse(500), {"code": 0, "message": "broken"})
            return await original(route, **kwargs)
        self.bot.http.request = flaky
        self.say(self.member_id)
        for _ in range(3):
            self.say(self.member_id)
        await self.settle()
        await self.cog.finish_deletions()
        self.assertEqual(len(first_delete), 2)
        self.assertEqual(self.deletes(), 1, "the third duplicate still went")
        self.assertFalse(self.cog._worker.done())


class LifecycleTest(Base):
    """GA-12, GA-13 and GA-16."""

    async def test_deleting_the_giveaway_channel_switches_the_limit_off(self):
        await self.set_place(self.channel)
        self.say(self.member_id)
        await self.settle()
        self.bot._connection.parse_channel_delete({**self.text})
        await self.settle()
        self.assertIsNone(self.cog.setup_of(self.guild_id))
        self.assertEqual(await self.entries(), [])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM entry_giveaway_setup"), 0)

    async def test_deleting_a_thread_that_was_the_whole_giveaway_switches_it_off(self):
        thread = self.thread(self.text)
        await self.set_place(thread)
        self.bot._connection.parse_thread_delete({"id": str(thread.id), "guild_id": str(self.guild_id), "parent_id": self.text["id"], "type": 11})
        await self.settle()
        self.assertIsNone(self.cog.setup_of(self.guild_id))

    async def test_deleting_a_forum_post_takes_its_entries_and_closed_mark(self):
        await self.set_place(self.forum_channel)
        gone, kept = self.thread(self.forum, "Gone"), self.thread(self.forum, "Kept")
        self.say(self.member_id, gone)
        self.say(self.member_id, kept)
        await self.settle()
        await self.cog.close(self.guild_id, gone.id, None)
        self.bot._connection.parse_thread_delete({"id": str(gone.id), "guild_id": str(self.guild_id), "parent_id": self.forum["id"], "type": 11})
        await self.settle()
        self.assertEqual([entry[0] for entry in await self.entries()], [kept.id])
        self.assertFalse(self.cog.is_closed(self.guild_id, gone.id))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM entry_giveaway_closed"), 0)

    async def test_the_setup_and_closed_marks_come_back_after_a_restart(self):
        await self.set_place(self.forum_channel)
        post = self.thread(self.forum)
        await self.cog.close(self.guild_id, post.id, None)
        fresh = EntryGiveaways(self.bot)
        await fresh._load()
        self.assertEqual(fresh.setup_of(self.guild_id), (self.forum_channel.id, MULTI))
        self.assertTrue(fresh.is_closed(self.guild_id, post.id))

    async def test_the_deletion_worker_starts_once_at_load(self):
        self.assertIsNotNone(self.cog._worker)
        self.assertFalse(self.cog._worker.done())


class CommandTest(Base):
    """GA-1 to GA-5 and GA-14, with the old commands' answers."""

    async def test_every_command_is_for_administrators_as_before(self):
        for name in ("set_giveaway_channel", "set_giveaway_staff", "mark_closed", "statistics", "draw"):
            self.assertEqual(required_permissions(getattr(self.cog, name)), ("administrator",), name)

    async def test_setting_the_channel_names_its_kind_and_starts_the_count_over(self):
        await self.set_place(self.channel)
        self.say(self.member_id)
        await self.settle()
        await self.cog.close(self.guild_id, self.channel.id, None)
        ctx = await self.invoke("set_giveaway_channel", Ctx(self, self.admin_id), self.channel)
        self.assertEqual(ctx.said(), [f"Enforcement enabled on text channel {self.channel.mention}. Participant tracking reset."])
        self.assertEqual(await self.entries(), [])
        self.assertFalse(self.cog.is_closed(self.guild_id, self.channel.id))
        forum = await self.invoke("set_giveaway_channel", Ctx(self, self.admin_id), self.forum_channel)
        self.assertEqual(forum.said(), [f"Enforcement enabled on forum channel {self.forum_channel.mention}. Participant tracking reset."])
        self.assertEqual(self.cog.setup_of(self.guild_id), (self.forum_channel.id, MULTI))
        thread = self.thread(self.text)
        in_thread = await self.invoke("set_giveaway_channel", Ctx(self, self.admin_id), thread)
        self.assertEqual(in_thread.said(), [f"Enforcement enabled on thread {thread.mention}. Participant tracking reset."])
        self.assertEqual(self.cog.setup_of(self.guild_id), (thread.id, SINGLE))

    async def test_no_target_switches_the_limit_off_or_says_none_was_on(self):
        ctx = await self.invoke("set_giveaway_channel", Ctx(self, self.admin_id), None)
        self.assertEqual(ctx.said(), ["No enforcement is currently active."])
        await self.set_place(self.channel)
        await self.bot.settings.save(self.guild_id, "entrygiveaways", {"staff_role": int(self.role_mod["id"])})
        off = await self.invoke("set_giveaway_channel", Ctx(self, self.admin_id), None)
        self.assertEqual(off.said(), [f"Enforcement disabled for {self.channel.mention}. Tracking cleared."])
        self.assertIsNone(self.cog.setup_of(self.guild_id))
        self.assertEqual(self.cog.cfg(self.guild_id)["staff_role"], int(self.role_mod["id"]), "the staff role stays")

    async def test_someone_who_cant_delete_messages_there_cant_aim_it_there(self):
        self.everyone_reads()
        ctx = await self.invoke("set_giveaway_channel", Ctx(self, self.member_id), self.channel)
        self.assertEqual(ctx.fails, ["You need Manage Messages in that channel to limit entries there."])
        self.assertIsNone(self.cog.setup_of(self.guild_id))

    async def test_the_staff_role_is_set_unset_and_only_by_an_administrator(self):
        role = self.guild.get_role(int(self.role_mod["id"]))
        ctx = await self.invoke("set_giveaway_staff", Ctx(self, self.admin_id), role)
        self.assertEqual(ctx.said(), [f"Giveaway staff role set to {role.mention}."])
        off = await self.invoke("set_giveaway_staff", Ctx(self, self.admin_id), None)
        self.assertEqual(off.said(), ["Giveaway staff role unset."])
        again = await self.invoke("set_giveaway_staff", Ctx(self, self.admin_id), None)
        self.assertEqual(again.said(), ["No staff role was previously set."])
        moderator = await self.invoke("set_giveaway_staff", Ctx(self, self.mod_id), role)
        self.assertEqual(moderator.fails, ["Only the server owner or an Administrator can change this."])
        everyone = await self.invoke("set_giveaway_staff", Ctx(self, self.admin_id), self.guild.default_role)
        self.assertEqual(everyone.fails, ["@everyone can't hold that. Pick a role."])

    async def test_closing_answers_as_before(self):
        self.unknown_channels_are_gone()
        none = await self.invoke("mark_closed", Ctx(self, self.admin_id), str(self.channel.id))
        self.assertEqual(none.said(), ["No active giveaway enforcement."])
        await self.set_place(self.channel)
        cases = [("abc", "Invalid channel selection."), (str(new_id()), "Channel/thread not found."), (self.log_channel["id"], "Selected channel/thread is not the active giveaway."),
                 (str(self.channel.id), f"Giveaway in {self.channel.mention} marked as closed. New entries stopped, free posting allowed."), (str(self.channel.id), "This giveaway is already closed.")]
        for value, said in cases:
            ctx = await self.invoke("mark_closed", Ctx(self, self.admin_id), value)
            self.assertEqual(ctx.said(), [said], value)
        await self.set_place(self.forum_channel)
        post = self.thread(self.forum)
        for value, said in ((self.text["id"], "Selected thread is not part of the active forum giveaway."),
                            (f"<#{post.id}>", f"Giveaway thread {post.mention} marked as closed. New entries stopped, free posting allowed."),
                            (str(post.id), "This giveaway thread is already closed.")):
            ctx = await self.invoke("mark_closed", Ctx(self, self.admin_id), value)
            self.assertEqual(ctx.said(), [said], value)

    async def test_statistics_overall_and_for_one_giveaway(self):
        ctx = await self.invoke("statistics", Ctx(self, self.admin_id), None)
        self.assertEqual(ctx.said(), ["No giveaway enforcement is currently active."])
        await self.set_place(self.forum_channel)
        empty = await self.invoke("statistics", Ctx(self, self.admin_id), OVERALL)
        self.assertEqual(empty.sent[0]["embed"].fields[0].name, "No entries yet")
        first, second = self.thread(self.forum, "Printer"), self.thread(self.forum, "Filament")
        someone = self.person("someone")
        self.say(self.member_id, first)
        self.say(someone, first)
        self.say(self.member_id, second)
        await self.settle()
        await self.cog.close(self.guild_id, second.id, None)
        overall = (await self.invoke("statistics", Ctx(self, self.admin_id), None)).sent[0]["embed"]
        self.assertEqual(overall.title, "\N{PARTY POPPER} Giveaway Statistics")
        self.assertEqual(overall.description, "Totals across all active giveaways")
        self.assertEqual([(field.name, field.value) for field in overall.fields], [
            ("#reviews > Printer", "2 unique entries"), ("#reviews > Filament (closed)", "1 unique entry"), ("**Grand Totals**", "**3** total entries\n**2** unique participants")])
        one = (await self.invoke("statistics", Ctx(self, self.admin_id), str(first.id))).sent[0]["embed"]
        self.assertEqual(one.description, f"Current entries in {first.mention}")
        self.assertEqual((one.fields[0].name, one.fields[0].value), ("Unique Entries", "2 participants"))
        wrong = await self.invoke("statistics", Ctx(self, self.admin_id), self.text["id"])
        self.assertEqual(wrong.said(), ["Selected thread is not part of the active forum giveaway."])

    async def test_autocomplete_lists_the_giveaways_and_leaves_closed_ones_out_of_closing(self):
        await self.set_place(self.forum_channel)
        open_post, shut_post = self.thread(self.forum, "Open one"), self.thread(self.forum, "Shut one")
        await self.cog.close(self.guild_id, shut_post.id, None)
        archived = {"id": str(new_id()), "guild_id": str(self.guild_id), "parent_id": self.forum["id"], "owner_id": str(self.admin_id), "name": "Old one", "type": 11,
                    "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "flags": 0, "thread_metadata": {"archived": True, "auto_archive_duration": 1440, "archive_timestamp": iso(), "locked": False}}
        self.net.archived[int(self.forum["id"])] = [archived]
        interaction = type("I", (), {"guild": self.guild})()
        names = [choice.name for choice in await self.cog._statistics_choices(interaction, "")]
        self.assertEqual(names, [OVERALL_CHOICE, "#reviews > Old one (archived)", "#reviews > Open one", "#reviews > Shut one (closed)"])
        closing = [choice.value for choice in await self.cog._close_choices(interaction, "")]
        self.assertEqual(closing, [archived["id"], str(open_post.id)])
        self.assertEqual([choice.name for choice in await self.cog._draw_choices(interaction, "open")], ["#reviews > Open one"])


class DrawTest(Base):
    """GA-5: every message is read, bots, webhooks, staff and the post itself are left out, and the winners are pinged in public."""

    def history(self, place, *authors: int, starter: bool = False, kinds: dict | None = None) -> None:
        self.net.keep_history = True
        if starter:
            self.net.messages[place.id] = self.net.message_json(place.id, {"content": "Win a printer"}, message_id=place.id) | {"author": self.member_json[self.admin_id]["user"]}
        for index, author in enumerate(authors):
            message = self.net.message_json(place.id, {"content": "entry"})
            message["author"] = self.member_json[author]["user"]
            message["type"] = (kinds or {}).get(index, 0)
            self.net.messages[int(message["id"])] = message

    async def test_winners_come_from_everyone_who_posted_once_each(self):
        await self.bot.settings.save(self.guild_id, "entrygiveaways", {"staff_role": int(self.role_mod["id"])})
        post = self.thread(self.forum)
        robot = self.person("robot", bot=True)
        people = [self.person(f"p{index}") for index in range(3)]
        self.history(post, people[0], people[1], people[0], self.mod_id, robot, people[2], starter=True)
        ctx = await self.invoke("draw", Ctx(self, self.admin_id), str(post.id), 2)
        announcement = ctx.sent[-1]["content"]
        winners = [int(found) for found in re.findall(r"<@(\d+)>", announcement)]
        self.assertEqual(len(winners), 2)
        self.assertTrue(set(winners) <= set(people))
        self.assertTrue(announcement.startswith("\N{PARTY POPPER} **WINNERS DRAWN!** \N{PARTY POPPER}\n\nCongratulations to our winners:\n\n1. <@"))
        self.assertTrue(announcement.endswith("Drawn fairly from **3** unique participants."))
        self.assertEqual([user.id for user in ctx.sent[-1]["allowed_mentions"].users], winners)
        latest = max(message_id for message_id, message in self.net.messages.items() if message["author"]["id"] == str(people[0]))
        if people[0] in winners:
            self.assertIn(f"/{post.id}/{latest})", announcement)
        progress = ctx.shown[0]
        self.assertEqual(ctx.sent[0]["embed"].title, "Scanning Giveaway Entries")
        self.assertEqual(progress.edits[-1], "Draw complete! Selected 2 unique winners from 3 participants.")

    async def test_many_winners_split_over_messages_and_each_is_pinged_once(self):
        post = self.thread(self.forum)
        people = [self.person(f"winner_with_a_long_name_{index}") for index in range(60)]
        self.history(post, *people)
        ctx = await self.invoke("draw", Ctx(self, self.admin_id), str(post.id), 60)
        posts = [sent for sent in ctx.sent if sent["content"] and "embed" not in sent]
        self.assertGreater(len(posts), 1)
        self.assertTrue(all(len(sent["content"]) <= 2000 for sent in posts))
        pinged = [int(found) for sent in posts for found in re.findall(r"<@(\d+)>", sent["content"])]
        self.assertEqual(sorted(pinged), sorted(people))
        for sent in posts:
            self.assertEqual(sorted(user.id for user in sent["allowed_mentions"].users), sorted(int(found) for found in re.findall(r"<@(\d+)>", sent["content"])))
        self.assertTrue(posts[0]["content"].startswith("🎉 **WINNERS DRAWN!**"))
        self.assertTrue(posts[-1]["content"].endswith("Drawn fairly from **60** unique participants."))

    async def test_one_winner_and_fewer_people_than_asked(self):
        post = self.thread(self.forum)
        self.history(post, self.member_id)
        ctx = await self.invoke("draw", Ctx(self, self.admin_id), str(post.id), 1)
        self.assertIn(f"Congratulations <@{self.member_id}>!\nClick [here](https://discord.com/channels/{self.guild_id}/{post.id}/", ctx.sent[-1]["content"])
        self.assertTrue(ctx.sent[-1]["content"].endswith("Drawn fairly from **1** unique participant."))
        two = self.person("two")
        self.history(post, two)
        many = await self.invoke("draw", Ctx(self, self.admin_id), str(post.id), 5)
        self.assertIn("*Note: Only 2 unique participants found, so 2 winners selected.*", many.sent[-1]["content"])

    async def test_no_entries_and_bad_choices_say_so(self):
        post = self.thread(self.forum)
        self.history(post)
        empty = await self.invoke("draw", Ctx(self, self.admin_id), str(post.id), 1)
        self.assertEqual(empty.sent[-1]["content"], "No valid entry messages found in the channel/thread.")
        self.assertTrue(empty.shown[0].edits[-1].endswith("No winners drawn."))
        for value, winners, said in (("abc", 1, "Invalid channel selection."), (str(post.id), 0, "Number of winners must be at least 1."), (self.forum["id"], 1, "Selected channel/thread not found or invalid.")):
            ctx = await self.invoke("draw", Ctx(self, self.admin_id), value, winners)
            self.assertEqual(ctx.said(), [said], value)

    async def test_progress_shows_every_hundred_messages(self):
        post = self.thread(self.forum)
        self.history(post, *([self.member_id] * 250))
        ctx = await self.invoke("draw", Ctx(self, self.admin_id), str(post.id), 1)
        edits = ctx.shown[0].edits
        self.assertIn("Scanned 100 messages...\nFound 1 unique participants so far.", edits)
        self.assertIn("Scanned 200 messages...\nFound 1 unique participants so far.", edits)
        self.assertIn("Scan complete!\nFound 1 unique participants with valid entries.", edits)

    async def test_someone_who_cant_read_the_channel_cant_draw_from_it(self):
        ctx = Ctx(self, self.member_id)
        hidden = self.guild.get_channel(int(self.log_channel["id"]))
        hidden._overwrites = [discord.abc._Overwrites({"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(view_channel=True).value)})]
        await self.invoke("draw", ctx, str(hidden.id), 1)
        self.assertEqual(ctx.fails, ["You can't see that channel."])


def at(moment: float) -> int:
    """A message ID from that moment, as Discord makes them."""
    return discord.utils.time_snowflake(datetime.datetime.fromtimestamp(moment, datetime.timezone.utc))


def winners_of(ctx: Ctx) -> list[int]:
    return sorted(int(found) for sent in ctx.sent if sent.get("content") for found in re.findall(r"<@(\d+)>", sent["content"]))


class DrawWindowTest(Base):
    """A draw from the giveaway counts only what was posted between its start and its close, by members still in the server."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.net.keep_history = True
        self.base = time.time()

    async def started(self, place, seconds_ago: float) -> None:
        await self.set_place(place)
        await self.db.run("UPDATE entry_giveaway_setup SET set_at = ? WHERE guild_id = ?", (self.base - seconds_ago, self.guild_id))

    async def test_what_is_posted_after_the_close_is_no_entry(self):
        await self.started(self.forum_channel, 300)
        post = self.thread(self.forum)
        entrant, chatter = self.person("entrant"), self.person("chatter")
        self.say(entrant, post, message_id=at(self.base - 200))
        await self.settle()
        await self.cog.close(self.guild_id, post.id, self.bot.actor(self.guild.get_member(self.admin_id)))
        await self.db.run("UPDATE entry_giveaway_closed SET closed_at = ? WHERE guild_id = ?", (self.base - 100, self.guild_id))
        self.say(chatter, post, message_id=at(self.base - 50))
        await self.settle()
        drawn = await self.invoke("draw", Ctx(self, self.admin_id), str(post.id), 2)
        self.assertEqual(winners_of(drawn), [entrant])
        self.assertTrue(drawn.sent[-1]["content"].endswith("Drawn fairly from **1** unique participant."))

    async def test_what_was_posted_before_the_giveaway_was_set_is_no_entry_and_a_missed_entry_counts(self):
        earlier, entrant = self.person("earlier"), self.person("entrant")
        self.say(earlier, self.channel, message_id=at(self.base - 600))
        await self.settle()
        await self.started(self.channel, 300)
        missed = self.net.message_json(self.channel.id, {"content": "entry"}, message_id=at(self.base - 200))  # posted while the bot was down
        self.net.messages[int(missed["id"])] = missed | {"author": self.member_json[entrant]["user"]}
        drawn = await self.invoke("draw", Ctx(self, self.admin_id), str(self.channel.id), 2)
        self.assertEqual(await self.entries(), [])
        self.assertEqual(winners_of(drawn), [entrant])

    async def test_an_entry_older_than_the_setup_still_counts(self):
        """An imported giveaway is set at the import, after its entries were made."""
        await self.started(self.channel, 0)
        old = self.person("old")
        message = self.say(old, self.channel, message_id=at(self.base - 86400))
        await self.settle()
        await self.db.run("DELETE FROM entry_giveaway_entries WHERE guild_id = ?", (self.guild_id,))
        await self.db.run("INSERT INTO entry_giveaway_entries (guild_id, channel_id, user_id, message_id, entered_at) VALUES (?, ?, ?, ?, ?)",
                          (self.guild_id, self.channel.id, old, int(message["id"]), self.base - 86400))
        drawn = await self.invoke("draw", Ctx(self, self.admin_id), str(self.channel.id), 1)
        self.assertEqual(winners_of(drawn), [old])

    async def test_someone_who_left_the_server_is_not_drawn(self):
        post = self.thread(self.forum)
        stays, goes = self.person("stays"), self.person("goes")
        self.say(stays, post)
        self.say(goes, post)
        await self.settle()
        self.bot._connection.parse_guild_member_remove({"guild_id": str(self.guild_id), "user": self.member_json[goes]["user"]})
        self.member_json.pop(goes)
        drawn = await self.invoke("draw", Ctx(self, self.admin_id), str(post.id), 2)
        self.assertEqual(winners_of(drawn), [stays])
        self.guild._member_count += 1  # the member list is no longer whole, so the draw asks Discord, which doesn't know them either
        asked = await self.invoke("draw", Ctx(self, self.admin_id), str(post.id), 2)
        self.assertEqual(winners_of(asked), [stays])
        self.assertIn(("GET", "/guilds/{guild_id}/members/{member_id}"), [(method, path) for method, path, _ in self.net.calls])


class PageTest(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)

    async def call(self, method: str, tail: str, body=None, *, user_id: int | None = None):
        from aiohttp.test_utils import TestClient, TestServer
        from captainguild.web.api import User
        from captainguild.web.app import build_app
        if not hasattr(self, "_client"):
            self.bot.ops.access_for = lambda guild_id, who: "full"
            app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(user_id or self.admin_id, "admin", None))
            self._client = TestClient(TestServer(app))
            await self._client.start_server()
        answer = await self._client.request(method, f"/api/g/{self.guild_id}{tail}", json=body)
        return answer.status, await answer.json()

    async def asyncTearDown(self) -> None:
        if hasattr(self, "_client"):
            await self._client.close()
        await super().asyncTearDown()

    async def test_a_media_channel_is_a_giveaway_of_posts_as_a_forum_is(self):
        media = {"id": str(new_id()), "guild_id": str(self.guild_id), "name": "photos", "type": 16, "position": 5, "permission_overwrites": [], "parent_id": None, "nsfw": False, "topic": None,
                 "rate_limit_per_user": 0, "last_message_id": None, "flags": 0, "available_tags": [], "default_reaction_emoji": None, "default_thread_rate_limit_per_user": 0, "default_sort_order": None, "default_forum_layout": 0}
        self.net.channels[int(media["id"])] = media
        self.bot._connection.parse_channel_create(media)
        status, view = await self.call("PUT", "/entrygiveaways/channel", {"channel_id": media["id"]})
        self.assertEqual(status, 200, view)
        self.assertEqual((view["setup"]["channel_id"], view["setup"]["kind"]), (media["id"], MULTI))
        uncached = _Place(new_id(), "photos", discord.ChannelType.media.value)  # as the page names one the bot's cache doesn't hold
        self.assertEqual(await self.cog.set_place(self.guild_id, uncached, None), MULTI)

    async def test_set_close_reopen_and_stop_from_the_page(self):
        status, view = await self.call("PUT", "/entrygiveaways/channel", {"channel_id": self.forum["id"]})
        self.assertEqual(status, 200, view)
        self.assertEqual((view["setup"]["channel_id"], view["setup"]["kind"]), (self.forum["id"], MULTI))
        post = self.thread(self.forum)
        self.say(self.member_id, post)
        await self.settle()
        status, view = await self.call("POST", f"/entrygiveaways/areas/{post.id}/close")
        self.assertEqual(status, 200, view)
        self.assertEqual([(area["area_id"], area["entries"], area["closed"]) for area in view["areas"]], [(str(post.id), 1, True)])
        status, view = await self.call("POST", f"/entrygiveaways/areas/{post.id}/reopen")
        self.assertFalse(view["areas"][0]["closed"])
        self.assertEqual((await self.call("POST", f"/entrygiveaways/areas/{self.text['id']}/close"))[0], 404)
        status, view = await self.call("PUT", "/entrygiveaways/channel", {"channel_id": None})
        self.assertIsNone(view["setup"])
        self.assertEqual(await self.entries(), [])

    async def test_the_page_refuses_a_channel_the_person_cant_delete_in(self):
        self.everyone_reads()
        status, answer = await self.call("PUT", "/entrygiveaways/channel", {"channel_id": self.text["id"]}, user_id=self.member_id)
        self.assertEqual(status, 400)
        self.assertEqual(answer["fields"]["channel_id"], "You need Manage Messages in that channel to limit entries there.")

    async def test_the_staff_role_is_a_staff_setting(self):
        self.assertEqual(self.cog.staff_roles({"staff_role": 5, "enabled": True}), {"staff_role": {5}})


class MyDataTest(Base):
    """An entry is the member's own: /my_data deletes it, and only theirs."""
    modules = [EntryGiveaways, MyData]

    async def test_a_member_deletes_their_own_entries_and_can_enter_again(self):
        await self.set_place(self.channel)
        someone = self.person("someone")
        self.say(self.member_id)
        self.say(someone)
        await self.settle()
        await self.bot.module("mydata").delete(self.member_id, "member", self.guild_id, "entrygiveaways")
        self.assertEqual([entry[1] for entry in await self.entries()], [someone])
        again = self.say(self.member_id)
        await self.settle()
        self.assertIn((self.channel.id, self.member_id, int(again["id"])), await self.entries())
