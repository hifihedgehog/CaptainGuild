"""One server's trouble with an alert stays with that server, a post Discord turned down comes back later, and what already landed never goes out twice."""
import json

import discord

from captainguild.modules.notifications import (
    GAMERPOWER_URL, TWITCH_API, TWITCH_REARM_SECONDS, TWITCH_TOKEN_URL, YOUTUBE_FEED, YOUTUBE_MAX_AGE, YOUTUBE_PENDING_KEPT, _plain, alert_mentions, alert_text, free_game_payload,
)
from tests.fake_outside import reply
from tests.fakes import FakeChannel, FakeGuild, FakeMember, _FakeResponse, new_id
from tests.test_notifications import CHANNEL, CaseBase, feed

OLD = "aaaaaaaaaaa"
NEW = "bbbbbbbbbbb"
NEWER = "ccccccccccc"
BAD_FIELD = {"code": 50035, "message": "Invalid Form Body"}


def refused(status=403, message="no"):
    kind = {403: discord.Forbidden, 404: discord.NotFound}.get(status, discord.DiscordServerError)

    async def send(*args, **kwargs):
        raise kind(_FakeResponse(status), message)
    return send


def giveaway(number):
    return {"id": number, "title": f"Game {number}", "open_giveaway_url": f"https://g.example/{number}", "platforms": "PC", "status": "Active"}


class YoutubeBase(CaseBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.other = FakeGuild("Other")
        self.bot.fake_guilds.append(self.other)
        self.other.me = FakeMember(self.other, "Bot", self.other.role("Bot", 9, "administrator"), bot=True)
        self.theirs = FakeChannel(self.other, "alerts")
        self.web.answer(YOUTUBE_FEED, reply(feed(OLD)))
        await self.notify._youtube_store(self.guild.id, CHANNEL, "Sample", self.channel.id, "{title}", None, True, None)
        await self.notify._youtube_store(self.other.id, CHANNEL, "Sample", self.theirs.id, "{title}", None, True, None)
        self.web.answer(YOUTUBE_FEED, reply(feed(NEW, OLD)))

    async def theirs_row(self):
        return await self.db.one("SELECT * FROM youtube_subs WHERE guild_id = ?", (self.other.id,))


class YoutubeOneServerTest(YoutubeBase):
    async def test_a_channel_with_no_send_only_touches_its_own_server(self):
        real = self.theirs.send

        async def broken(*args, **kwargs):
            raise AttributeError("no send on this kind of channel")
        self.theirs.send = broken
        for _ in range(3):
            await self.notify.poll_youtube()
            self.now += 700
        self.assertEqual(len(self.channel.sent), 1, "the healthy server got it once")
        self.assertIsNotNone((await self.theirs_row())["last_error"])
        self.theirs.send = real
        await self.notify.poll_youtube()
        self.assertEqual(len(self.theirs.sent), 1)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_lost_connection_in_one_server_repeats_nothing_in_the_other(self):
        real = self.theirs.send

        async def blip(*args, **kwargs):
            raise OSError("connection reset")
        self.theirs.send = blip
        await self.notify.poll_youtube()
        self.now += 700
        self.theirs.send = real
        await self.notify.poll_youtube()
        self.assertEqual((len(self.channel.sent), len(self.theirs.sent)), (1, 1))
        self.assertIsNone((await self.theirs_row())["last_error"])

    async def test_an_outage_retries_and_a_second_video_goes_out_after_the_first(self):
        real = self.theirs.send
        self.theirs.send = refused(503, "busy")
        await self.notify.poll_youtube()
        self.assertIn("Discord refused", (await self.theirs_row())["last_error"])
        self.now += 700
        self.web.answer(YOUTUBE_FEED, reply(feed(NEWER, NEW, OLD)))
        self.theirs.send = real
        await self.notify.poll_youtube()
        self.assertEqual([m.content for m in self.theirs.sent], [f"Title of {NEW}", f"Title of {NEWER}"])
        self.assertEqual(len(self.channel.sent), 2)
        self.assertEqual((await self.theirs_row())["last_video"], NEWER)

    async def test_a_missing_permission_fixed_later_still_gets_the_alert(self):
        real = self.theirs.send
        self.theirs.send = refused(403)
        await self.notify.poll_youtube()
        self.theirs.send = real
        self.now += 700
        await self.notify.poll_youtube()
        self.assertEqual(len(self.theirs.sent), 1)

    async def test_a_message_refused_for_what_it_holds_is_not_tried_again(self):
        tried = []

        async def refuse(*args, **kwargs):
            tried.append(args)
            raise discord.HTTPException(_FakeResponse(400), BAD_FIELD)
        self.theirs.send = refuse
        await self.notify.poll_youtube()
        self.now += 700
        await self.notify.poll_youtube()
        self.assertEqual(len(tried), 1)
        self.assertIn("Discord refused", (await self.theirs_row())["last_error"])

    async def test_a_retry_runs_while_the_feed_is_down(self):
        real = self.theirs.send
        self.theirs.send = refused(503, "busy")
        await self.notify.poll_youtube()
        self.theirs.send = real
        self.web.answer(YOUTUBE_FEED, reply(b"", 500))
        self.web.answer("https://www.youtube.com/channel", reply(b"", 500))
        self.now += 700
        await self.notify.poll_youtube()
        self.assertEqual(len(self.theirs.sent), 1)

    async def test_a_server_thats_away_waits_and_isnt_told_its_channel_is_gone(self):
        self.bot.fake_guilds.remove(self.other)
        await self.notify.poll_youtube()
        self.assertNotIn("no longer exists", (await self.theirs_row())["last_error"])
        self.bot.fake_guilds.append(self.other)
        self.now += 700
        await self.notify.poll_youtube()
        self.assertEqual(len(self.theirs.sent), 1)

    async def test_a_deleted_channel_is_noted_and_a_new_one_gets_the_alert(self):
        self.other.channels.remove(self.theirs)
        await self.notify.poll_youtube()
        self.assertIn("no longer exists", (await self.theirs_row())["last_error"])
        fixed = FakeChannel(self.other, "fixed")
        await self.db.run("UPDATE youtube_subs SET channel_id = ? WHERE guild_id = ?", (fixed.id, self.other.id))
        self.now += 700
        await self.notify.poll_youtube()
        self.assertEqual(len(fixed.sent), 1)

    async def test_what_waits_too_long_goes_stale(self):
        self.theirs.send = refused(403)
        await self.notify.poll_youtube()
        self.now += YOUTUBE_MAX_AGE + 1
        await self.notify.poll_youtube()
        self.assertIsNone((await self.theirs_row())["pending"])

    async def test_a_long_outage_keeps_only_the_newest(self):
        self.theirs.send = refused(503, "busy")
        await self.notify.poll_youtube()
        for index in range(YOUTUBE_PENDING_KEPT + 3):
            newest = f"new{index:08d}"
            self.web.answer(YOUTUBE_FEED, reply(feed(newest, NEW, OLD, published=self.now)))
            await self.notify.poll_youtube()
            await self.db.run("UPDATE youtube_state SET seen = ?", (json.dumps([NEW, OLD]),))
        self.assertLessEqual(len(json.loads((await self.theirs_row())["pending"])), YOUTUBE_PENDING_KEPT)

    async def test_a_server_that_leaves_out_shorts_gets_nothing_held_for_one(self):
        await self.db.run("UPDATE youtube_subs SET shorts = 0 WHERE guild_id = ?", (self.other.id,))
        self.web.answer(YOUTUBE_FEED, reply(feed(NEW, OLD, shorts=(NEW,))))
        await self.notify.poll_youtube()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.theirs.sent, [])
        self.assertIsNone((await self.theirs_row())["pending"])

    async def test_held_alerts_with_damaged_data_cant_stop_the_others(self):
        await self.db.run("UPDATE youtube_subs SET pending = 'not json' WHERE guild_id = ?", (self.other.id,))
        await self.notify.poll_youtube()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(len(self.theirs.sent), 1)


class TwitchRetryTest(CaseBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.web.answer(TWITCH_TOKEN_URL, {"access_token": "t", "expires_in": 5000})
        await self.bot.settings.save(self.guild.id, "notifications", {"twitch_client_id": "a", "twitch_client_secret": "b"})
        await self.notify._twitch_store(self.guild.id, {"id": "1", "login": "x", "display_name": "X"}, self.channel.id, "{name}", None, None)
        self.live("100")

    def live(self, *keys):
        self.web.answer(f"{TWITCH_API}/streams", {"data": [{"id": key, "user_id": "1", "user_login": "x", "user_name": "X"} for key in keys]})

    async def row(self):
        return await self.db.one("SELECT * FROM twitch_subs")

    async def test_a_lost_connection_retries_with_no_second_post(self):
        real = self.channel.send

        async def blip(*args, **kwargs):
            raise OSError("reset")
        self.channel.send = blip
        with self.assertNoLogs("captainguild.modules.notifications", "ERROR"):
            await self.notify.poll_twitch()
        self.assertIsNone((await self.row())["live_id"])
        self.assertIsNotNone((await self.row())["last_error"])
        self.channel.send = real
        await self.notify.poll_twitch()
        await self.notify.poll_twitch()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIsNone((await self.row())["last_error"])

    async def test_a_message_refused_for_what_it_holds_waits_for_the_next_stream(self):
        tried = []
        real = self.channel.send

        async def refuse(*args, **kwargs):
            tried.append(args)
            raise discord.HTTPException(_FakeResponse(400), BAD_FIELD)
        self.channel.send = refuse
        await self.notify.poll_twitch()
        await self.notify.poll_twitch()
        self.assertEqual(len(tried), 1)
        self.assertIn("Discord refused", (await self.row())["last_error"])
        self.channel.send = real
        self.live()
        await self.notify.poll_twitch()
        self.now += TWITCH_REARM_SECONDS + 1
        self.live("101")
        await self.notify.poll_twitch()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_stream_that_drops_and_returns_between_two_polls_is_one_stream(self):
        await self.notify.poll_twitch()
        self.now += 120
        self.live("101")
        await self.notify.poll_twitch()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual((await self.row())["live_id"], "101")

    async def test_a_new_stream_long_after_the_last_sighting_alerts(self):
        await self.notify.poll_twitch()
        self.now += 60
        await self.notify.poll_twitch()
        self.web.answer(f"{TWITCH_API}/streams", reply({}, 500))
        await self.notify.poll_twitch()
        self.now += TWITCH_REARM_SECONDS + 1
        self.live("101")
        await self.notify.poll_twitch()
        self.assertEqual(len(self.channel.sent), 2)

    async def test_the_last_sighting_moves_with_each_poll(self):
        await self.notify.poll_twitch()
        self.now += TWITCH_REARM_SECONDS - 1
        await self.notify.poll_twitch()
        self.now += TWITCH_REARM_SECONDS - 1
        self.live("101")
        await self.notify.poll_twitch()
        self.assertEqual(len(self.channel.sent), 1)


class GameRetryTest(CaseBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.web.answer(GAMERPOWER_URL, [giveaway(1)])
        await self.bot.settings.save(self.guild.id, "notifications", {"alert_channel": self.channel.id})
        await self.notify.poll_free_games()

    def status(self):
        return self.notify._status[f"free:{self.guild.id}"]

    def titles(self):
        return [message.embeds[0].title for message in self.channel.sent]

    async def test_one_item_discord_refuses_doesnt_block_the_ones_after_it(self):
        real = self.channel.send
        tried = []

        async def refuse_two(*args, **kwargs):
            title = kwargs["embeds"][0].title
            tried.append(title)
            if title == "Game 2":
                raise discord.HTTPException(_FakeResponse(400), BAD_FIELD)
            return await real(*args, **kwargs)
        self.channel.send = refuse_two
        self.web.answer(GAMERPOWER_URL, [giveaway(3), giveaway(2)])
        await self.notify.poll_free_games()
        await self.notify.poll_free_games()
        self.assertEqual(tried, ["Game 2", "Game 3"])
        self.assertIn("Discord refused", self.status()["error"])

    async def test_a_channel_problem_keeps_every_item_in_order(self):
        real = self.channel.send
        self.channel.send = refused(403)
        self.web.answer(GAMERPOWER_URL, [giveaway(3), giveaway(2)])
        await self.notify.poll_free_games()
        self.channel.send = real
        await self.notify.poll_free_games()
        self.assertEqual(self.titles(), ["Game 2", "Game 3"])

    async def test_a_failure_partway_doesnt_post_the_earlier_item_again(self):
        real = self.channel.send

        async def second_fails(*args, **kwargs):
            if self.channel.sent:
                raise OSError("reset")
            return await real(*args, **kwargs)
        self.channel.send = second_fails
        self.web.answer(GAMERPOWER_URL, [giveaway(3), giveaway(2)])
        await self.notify.poll_free_games()
        self.assertEqual(len(self.channel.sent), 1)
        self.channel.send = real
        await self.notify.poll_free_games()
        self.assertEqual(self.titles(), ["Game 2", "Game 3"])

    async def test_an_item_that_cant_be_built_is_passed_over(self):
        def build(item, cfg, **kwargs):
            if item["id"] == 2:
                raise TypeError("a field of the wrong kind")
            return free_game_payload(item, cfg, **kwargs)
        cfg = self.notify.cfg(self.guild.id)
        filters = json.dumps(cfg["free_platforms"])
        items = [("3", giveaway(3)), ("2", giveaway(2))]
        await self.notify._deliver(self.guild.id, "free", filters, cfg, items, build, "free_format")
        self.assertEqual(self.titles(), ["Game 3"])
        await self.notify._deliver(self.guild.id, "free", filters, cfg, items, build, "free_format")
        self.assertEqual(self.titles(), ["Game 3"])

    async def test_the_gone_channel_note_clears_when_the_channel_is_back_and_nothing_is_new(self):
        self.guild.channels.remove(self.channel)
        self.web.answer(GAMERPOWER_URL, [giveaway(2), giveaway(1)])
        await self.notify.poll_free_games()
        self.assertIn("no longer exists", self.status()["error"])
        fixed = FakeChannel(self.guild, "fixed")
        await self.bot.settings.save(self.guild.id, "notifications", {"alert_channel": fixed.id})
        await self.notify._deliver(self.guild.id, "free", json.dumps([]), self.notify.cfg(self.guild.id), [], free_game_payload, "free_format")
        self.assertIsNone(self.status()["error"])

    async def test_a_note_about_a_refused_post_stays_when_nothing_is_new(self):
        self.channel.send = refused(403)
        self.web.answer(GAMERPOWER_URL, [giveaway(2), giveaway(1)])
        await self.notify.poll_free_games()
        await self.notify._deliver(self.guild.id, "free", json.dumps([]), self.notify.cfg(self.guild.id), [], free_game_payload, "free_format")
        self.assertIn("Discord refused", self.status()["error"])


class AlertTextTest(CaseBase):
    def test_outside_text_stays_on_one_line_with_its_markdown_escaped(self):
        made = _plain("[Official](https://evil.example)\n# BIG")
        self.assertNotIn("\n", made)
        self.assertIn("\\[Official]", made)

    def test_braces_stay_as_written_and_never_run(self):
        made = alert_text("{title}", None, values={"title": _plain("My {cool} stream {everyone}")})
        self.assertEqual(made, "My {cool} stream {everyone}")

    def test_a_person_named_in_the_format_can_be_pinged(self):
        user = new_id()
        self.assertEqual([u.id for u in alert_mentions(f"Hey <@{user}> and <@!{user}>", None).users], [user])
        self.assertEqual(alert_mentions("{title}", None).users, [])
