"""Level commands: every typed form from the documentation, confirmations, long lists, cooldowns, and where level-ups go."""
from __future__ import annotations

import asyncio
import time
from unittest import mock

import discord
from discord.ext import commands

from captainguild.core.shortcuts import SHORTCUTS
from captainguild.modules.levels import ConfirmView, Levels, fit_list, parse_rate, xp_for_level
from tests.fakes import BotTest, FakeChannel, new_id
from tests.gateway import GatewayTest


class Base(BotTest):
    modules = [Levels]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.levels = self.cog(Levels)
        await self.bot.settings.save(self.guild.id, "levels", {"enabled": True})

    async def asyncTearDown(self) -> None:
        await self.levels.cog_unload()
        await super().asyncTearDown()


class TypedTest(GatewayTest):
    modules = [Levels]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.net.keep_history = True
        await self.bot.settings.save(self.guild_id, "levels", {"enabled": True})
        self.levels = next(m for m in self.bot.modules if isinstance(m, Levels))

    def cfg(self) -> dict:
        return self.bot.settings.get(self.guild_id, "levels")

    async def typed(self, content: str) -> None:
        self.dispatch_message(user_id=self.owner_id, content=content)
        await self.settle()
        self.assert_no_errors(content)

    def replies(self) -> list[dict]:
        return list(self.net.messages.values())

    async def test_a_rate_in_two_words_is_both_numbers(self):
        await self.typed("!level rate 5 60")
        self.assertEqual((self.cfg()["rate"], self.cfg()["window"]), (5, 60))

    async def test_a_rate_in_the_short_form_works_too(self):
        await self.typed("!lvl rate 2/30")
        self.assertEqual((self.cfg()["rate"], self.cfg()["window"]), (2, 30))

    async def test_the_documented_spellings_reach_the_real_commands(self):
        await self.typed("!lvl msgxp 15")
        self.assertEqual(self.cfg()["custom_xp"], 15)
        await self.typed(f"!lvl addxp 10 <@{self.member_id}>")
        self.assertEqual((await self.levels._entry(self.guild_id, self.member_id)).xp, 10)
        await self.typed(f"!level setlevel 5 <@{self.member_id}>")
        self.assertEqual((await self.levels._entry(self.guild_id, self.member_id)).xp, xp_for_level(5))
        await self.typed(f"!lvl bl <#{self.text['id']}>")
        self.assertEqual(self.cfg()["blacklist_channels"], [int(self.text["id"])])
        await self.typed(f"!lvl unbl <#{self.text['id']}>")
        self.assertEqual(self.cfg()["blacklist_channels"], [])
        await self.typed("!lvl voicexp")
        self.assertTrue(self.cfg()["voice_xp"])
        await self.typed("!lvl dm")
        self.assertEqual(self.cfg()["destination"], "dm")
        await self.typed("!lvl shh")
        self.assertEqual(self.cfg()["destination"], "none")
        await self.typed("!lvl replace")
        self.assertTrue(self.cfg()["replace_rewards"])
        await self.typed("!lvl color FF0000")
        self.assertEqual(self.cfg()["card_color"], 0xFF0000)
        await self.typed("!lvl accent 00FF00")
        self.assertEqual(self.cfg()["card_accent"], 0x00FF00)
        await self.typed("!lvl alpha 40")
        self.assertEqual(self.cfg()["card_opacity"], 40.0)

    async def test_a_word_thats_neither_a_command_nor_a_member_says_so(self):
        with mock.patch.object(commands.MemberConverter, "query_member_named", mock.AsyncMock(return_value=None)):
            await self.typed("!level notacommand")
        self.assertTrue(any("isn't a member or a level command" in (m.get("content") or "") for m in self.replies()))
        self.assertFalse(any(m.get("attachments") for m in self.replies()))

    async def test_the_group_alone_shows_the_card_of_a_named_member(self):
        with mock.patch.object(self.levels, "_send_card", mock.AsyncMock()) as card:
            await self.typed(f"!level <@{self.member_id}>")
            self.assertEqual(card.await_args.args[1].id, self.member_id)

    async def test_lvl_alone_is_the_askers_card(self):
        with mock.patch.object(self.levels, "_send_card", mock.AsyncMock()) as card:
            await self.typed("!lvl")
            self.assertEqual(card.await_args.args[1].id, self.owner_id)

    async def test_blacklist_with_a_channel_adds_it(self):
        await self.typed(f"!level blacklist <#{self.text['id']}>")
        self.assertEqual(self.cfg()["blacklist_channels"], [int(self.text["id"])])

    async def test_cards_have_a_cooldown(self):
        with mock.patch.object(self.levels, "_send_card", mock.AsyncMock()) as card:
            await self.typed("!level")
            await self.typed("!level")
            self.assertEqual(card.await_count, 1)

    async def test_the_leaderboard_has_a_cooldown(self):
        with mock.patch.object(self.levels, "board_embed", mock.AsyncMock(return_value=(discord.Embed(title="x"), 1, 1))) as board:
            await self.typed("!leaderboard")
            await self.typed("!leaderboard")
            self.assertEqual(board.await_count, 1)

    async def test_resetall_asks_first(self):
        with mock.patch.object(self.levels, "_confirm", mock.AsyncMock(return_value=False)) as confirm:
            await self.typed("!lvl resetall")
        confirm.assert_awaited_once()

    async def test_every_long_spelling_leads_somewhere(self):
        for name, target in SHORTCUTS.items():
            if name.split()[0] in ("level", "lvl", "lvls"):
                with self.subTest(name=name):
                    self.assertIsNotNone(self.bot.get_command(target) or self.bot.get_command(" ".join(target.split()[:-1])))


class ParseRateTest(Base):
    async def test_the_forms_it_documents(self):
        self.assertEqual(parse_rate("5 60"), (5, 60))
        self.assertEqual(parse_rate("5"), (1, 5))


class ConfirmTest(Base):
    async def asked(self, run, answer: bool | None) -> tuple[dict, list]:
        """Run a command to where it asks, then answer. Returns what it asked."""
        ctx = self.context()
        task = asyncio.create_task(run(ctx))
        while not ctx.sent and not task.done():
            await asyncio.sleep(0)
        view = ctx.sent[-1]["view"]
        self.assertIsInstance(view, ConfirmView)
        view.answer = answer
        view.stop()
        await task
        return ctx.sent[-1], ctx

    async def test_a_server_reset_says_how_many_and_cancel_leaves_them(self):
        await self.levels.set_xp(self.guild.id, self.member.id, 900)
        sent, ctx = await self.asked(lambda c: self.levels.level_reset.callback(self.levels, c, "server", None), False)
        self.assertIn("all 1 members", sent["content"])
        self.assertEqual(await self.db.scalar("SELECT xp FROM level_members"), 900)
        self.assertEqual(ctx.oks, [])

    async def test_confirm_goes_ahead(self):
        await self.levels.set_xp(self.guild.id, self.member.id, 900)
        await self.asked(lambda c: self.levels.level_reset.callback(self.levels, c, "all", None), True)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM level_members"), 0)

    async def test_no_answer_changes_nothing(self):
        await self.levels.set_xp(self.guild.id, self.member.id, 900)
        await self.asked(lambda c: self.levels.level_reset.callback(self.levels, c, "server", None), None)
        self.assertEqual(await self.db.scalar("SELECT xp FROM level_members"), 900)

    async def test_a_restore_names_the_backup_it_will_use(self):
        await self.levels.set_xp(self.guild.id, self.member.id, 900)
        await self.levels.snapshot(self.guild.id, "daily")
        await self.levels.set_xp(self.guild.id, self.member.id, 50)
        sent, _ = await self.asked(lambda c: self.levels.level_restore.callback(self.levels, c), False)
        self.assertIn("daily", sent["content"])
        self.assertIn("<t:", sent["content"])
        self.assertEqual(await self.db.scalar("SELECT xp FROM level_members"), 50)

    async def test_an_import_asks_first(self):
        fetch = mock.AsyncMock()
        with mock.patch.object(self.levels, "import_mee6", fetch):
            await self.asked(lambda c: self.levels.level_mee6import.callback(self.levels, c), False)
        fetch.assert_not_awaited()

    async def test_the_buttons_answer(self):
        for button, answer in (("confirm", True), ("cancel", False)):
            view = ConfirmView(self.admin.id, "Reset")
            interaction = mock.Mock(user=self.admin, response=mock.Mock(edit_message=mock.AsyncMock()))
            await getattr(view, button).callback(interaction)
            self.assertIs(view.answer, answer)
            self.assertTrue(view.is_finished())
            self.assertIsNone(interaction.response.edit_message.await_args.kwargs["view"])

    async def test_only_the_person_who_asked_can_answer(self):
        view = ConfirmView(self.admin.id, "Reset")
        interaction = mock.Mock(user=self.member, response=mock.Mock(send_message=mock.AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))


class LongListTest(Base):
    async def test_config_stays_inside_an_embed(self):
        rows = [{"level": i + 1, "role_id": 1100000000000000000 + i} for i in range(200)]
        await self.bot.settings.save(self.guild.id, "levels", {"enabled": False, "rewards": rows})
        ctx = self.context()
        await self.levels.level_config.callback(self.levels, ctx)
        text = ctx.sent[-1]["embed"].description
        self.assertLessEqual(len(text), 4096)
        self.assertRegex(text, r"and \d+ more, see the dashboard")

    async def test_the_blacklist_stays_inside_a_message(self):
        ids = [1100000000000000000 + i for i in range(150)]
        await self.bot.settings.save(self.guild.id, "levels", {"blacklist_channels": ids, "blacklist_roles": ids})
        ctx = self.context()
        await self.levels.level_blacklist.callback(self.levels, ctx)
        self.assertLessEqual(len(ctx.sent[-1]["content"]), 2000)
        self.assertRegex(ctx.sent[-1]["content"], r"and \d+ more, see the dashboard")

    async def test_a_list_that_fits_is_whole(self):
        self.assertEqual(fit_list(["a", "b"], 100), "a, b")
        self.assertEqual(fit_list([], 100), "")


class AnnounceTest(Base):
    async def announce(self) -> None:
        cfg = self.levels.cfg(self.guild.id)
        await self.levels._announce(self.member, self.channel, 1, 2, cfg)

    async def test_a_nickname_cant_ping_anyone_else(self):
        await self.bot.settings.save(self.guild.id, "levels", {"destination": "same"})
        await self.announce()
        mentions = self.channel.sent[-1].kwargs["allowed_mentions"]
        self.assertEqual(mentions.users, [self.member])
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.everyone)

    async def test_a_channel_thats_gone_tells_staff_once_a_day(self):
        logs = mock.Mock()
        real = self.bot.module
        self.bot.module = lambda key: logs if key == "logging" else real(key)
        await self.bot.settings.save(self.guild.id, "levels", {"destination": new_id()})
        await self.announce()
        await self.announce()
        self.assertEqual(logs.staff_notice.call_count, 1)

    async def test_deleting_the_channel_moves_level_ups_to_the_same_channel(self):
        place = FakeChannel(self.guild, "level-ups")
        await self.bot.settings.save(self.guild.id, "levels", {"destination": place.id})
        await self.levels.on_guild_channel_delete(place)
        self.assertEqual(self.levels.cfg(self.guild.id)["destination"], "same")

    async def test_deleting_another_channel_changes_nothing(self):
        place = FakeChannel(self.guild, "level-ups")
        await self.bot.settings.save(self.guild.id, "levels", {"destination": place.id})
        await self.levels.on_guild_channel_delete(self.channel)
        self.assertEqual(self.levels.cfg(self.guild.id)["destination"], place.id)


class ChannelCommandTest(Base):
    async def test_a_channel_the_person_cant_post_in_is_refused(self):
        ctx = self.context(author=self.mod)
        with mock.patch.object(self.channel, "permissions_for", lambda member: discord.Permissions(view_channel=True)):
            await self.levels.level_channel.callback(self.levels, ctx, self.channel)
        self.assertEqual(ctx.fails, ["You can't post in that channel."])

    async def test_a_channel_the_bot_cant_post_in_is_refused(self):
        ctx = self.context()
        with mock.patch.object(self.channel, "permissions_for", lambda member: discord.Permissions(view_channel=True, send_messages=member.id != self.me.id)):
            await self.levels.level_channel.callback(self.levels, ctx, self.channel)
        self.assertIn("I can't post", ctx.fails[-1])

    async def test_the_form_names_the_channel_it_posts_in(self):
        self.assertEqual(self.levels.posted_channels({"destination": 5}), {"destination": {5}})
        self.assertEqual(self.levels.posted_channels({"destination": "same"}), {"destination": set()})


class BackgroundTest(Base):
    async def test_none_are_taken_with_levels_off(self):
        await self.bot.settings.save(self.guild.id, "levels", {"enabled": False})
        ctx = self.context()
        await self.levels.level_background.callback(self.levels, ctx, None, None)
        self.assertIn("Levels are off", ctx.fails[-1])

    async def test_a_background_goes_a_month_after_they_left(self):
        await self.levels._store_background(self.guild.id, self.member.id, b"x")
        await self.levels._store_background(self.guild.id, self.mod.id, b"x")
        await self.levels._store_background(self.guild.id, 0, b"x")
        await self.levels.set_xp(self.guild.id, self.member.id, 5)
        await self.levels.set_xp(self.guild.id, self.mod.id, 5)
        await self.db.run("UPDATE level_members SET left_at = ? WHERE user_id = ?", (time.time() - 40 * 86400, self.member.id))
        await self.db.run("UPDATE level_members SET left_at = ? WHERE user_id = ?", (time.time() - 5 * 86400, self.mod.id))
        await self.levels._sweep_backgrounds(self.guild.id)
        kept = {row["user_id"] for row in await self.db.all("SELECT user_id FROM level_backgrounds")}
        self.assertEqual(kept, {self.mod.id, 0})


class BoardInputTest(Base):
    async def test_odd_searches_answer_and_dont_fail(self):
        client = await self.client()
        await self.levels.set_xp(self.guild.id, 5, 100, name="² named")
        for search, total in (("9999999999999999999", 0), ("%C2%B2", 1), ("5", 1), ("abc", 0)):
            with self.subTest(search=search):
                reply = await client.get(self.path(f"/levels/leaderboard?q={search}"))
                self.assertEqual(reply.status, 200)
                self.assertEqual((await reply.json())["total"], total)

    async def test_a_finished_import_says_when_it_finished(self):
        client = await self.client()
        self.levels._imports[self.guild.id] = {"state": "done", "members": 5, "rewards": 0, "finished_at": 123.0}
        reply = await client.get(self.path("/levels/import"))
        self.assertEqual((await reply.json())["finished_at"], 123.0)

