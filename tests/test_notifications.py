import asyncio
import json
import time
from unittest import mock

import discord

from captainguild.core.ops import DiscordOps
from captainguild.core.outside import USER_AGENT, OutsideError
from captainguild.core.validate import SECRET_MASK
from captainguild.modules.notifications import (
    CHEAPSHARK_URL, GAMERPOWER_URL, TWITCH_API, TWITCH_TOKEN_URL, YOUTUBE_FEED, YOUTUBE_SITE, Notifications, Video, alert_mentions, alert_text, new_videos,
    read_channel_page, read_feed, read_search_page, read_videos_page, youtube_target, Listing, ServiceError,
)
from tests.fake_outside import FakeOutside, reply
from tests.fakes import BotTest, FakeChannel, FakeGuild, _FakeResponse, new_id

CHANNEL = "UC" + "a" * 22
OTHER_CHANNEL = "UC" + "b" * 22


def feed(*videos, name="Sample Channel", shorts=(), published=None) -> bytes:
    published = time.time() + 30 if published is None else published  # after the test's first look at the channel
    entries = "".join(
        f"""<entry><id>yt:video:{v}</id><yt:videoId>{v}</yt:videoId><yt:channelId>{CHANNEL}</yt:channelId><title>Title of {v}</title>
        <link rel="alternate" href="{'https://www.youtube.com/shorts/' + v if v in shorts else 'https://www.youtube.com/watch?v=' + v}"/>
        <author><name>{name}</name><uri>https://www.youtube.com/channel/{CHANNEL}</uri></author>
        <published>{time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(published))}</published></entry>"""
        for v in videos
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
    <link rel="self" href="http://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"/><id>yt:channel:{CHANNEL}</id><yt:channelId>{CHANNEL}</yt:channelId>
    <title>{name}</title><author><name>{name}</name></author>{entries}</feed>""".encode()


def video_id(number: int) -> str:
    return f"vid{number:08d}"


def videos_page(*ids, name="Sample Channel") -> str:
    data = {"metadata": {"channelMetadataRenderer": {"title": name, "externalId": CHANNEL}}, "contents": [
        {"lockupViewModel": {"contentId": i, "contentType": "LOCKUP_CONTENT_TYPE_VIDEO", "metadata": {"lockupMetadataViewModel": {"title": {"content": f"Page title of {i}"}}}}} for i in ids
    ]}
    return f'<html><script>var ytInitialData = {json.dumps(data)};</script></html>'


def channel_page(channel_id=CHANNEL, name="Sample &amp; Channel") -> str:
    return f'<html><meta property="og:title" content="{name}"><script>{{"externalId":"{channel_id}"}}</script></html>'


class CaseBase(BotTest):
    modules = [Notifications]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.notify = self.cog(Notifications)
        self.web = FakeOutside()
        self.notify.outside = self.web
        self.notify._sleep = mock.AsyncMock()
        self.now = time.time()
        self.notify._clock = lambda: self.now
        self.bot.ops = DiscordOps(self.bot)


class TwitchCase(CaseBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.web.answer(TWITCH_TOKEN_URL, {"access_token": "token-1", "expires_in": 5000})
        self.web.answer(f"{TWITCH_API}/streams", {"data": []})
        self.web.answer(f"{TWITCH_API}/users", {"data": [{"id": "111", "login": "sampleplays", "display_name": "SamplePlays"}]})
        await self.bot.settings.save(self.guild.id, "notifications", {"twitch_client_id": "client-id-1234", "twitch_client_secret": "secret-value"})

    async def follow(self, fmt: str = "{name} is live playing {game}: {link}", role=None) -> None:
        await self.notify._twitch_store(self.guild.id, {"id": "111", "login": "sampleplays", "display_name": "SamplePlays"}, self.channel.id, fmt, role, None)

    def stream(self, stream_id: str = "s1", title: str = "Big game", game: str = "Chess") -> dict:
        return {"id": stream_id, "user_id": "111", "user_login": "sampleplays", "user_name": "SamplePlays", "game_name": game, "title": title, "type": "live"}

    def live(self, *streams) -> None:
        self.web.answer(f"{TWITCH_API}/streams", {"data": list(streams)})


class TwitchAlertTest(TwitchCase):
    async def test_going_live_posts_one_filled_in_alert(self):
        await self.follow()
        self.live(self.stream())
        await self.notify.poll_twitch()
        self.assertEqual(self.channel.sent[0].content, "SamplePlays is live playing Chess: https://twitch.tv/sampleplays")
        await self.notify.poll_twitch()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_the_call_asks_for_live_streams_by_id_with_the_app_token(self):
        await self.follow()
        await self.notify.poll_twitch()
        call = self.web.called(f"{TWITCH_API}/streams")[0]
        self.assertIn(("user_id", "111"), call["params"])
        self.assertIn(("type", "live"), call["params"])
        self.assertEqual(call["headers"], {"Client-Id": "client-id-1234", "Authorization": "Bearer token-1"})
        token_call = self.web.called(TWITCH_TOKEN_URL)[0]
        self.assertEqual(token_call["data"], {"client_id": "client-id-1234", "client_secret": "secret-value", "grant_type": "client_credentials"})

    async def test_the_token_is_kept_until_it_expires(self):
        await self.follow()
        await self.notify.poll_twitch()
        await self.notify.poll_twitch()
        self.assertEqual(len(self.web.called(TWITCH_TOKEN_URL)), 1)
        self.now += 6000
        await self.notify.poll_twitch()
        self.assertEqual(len(self.web.called(TWITCH_TOKEN_URL)), 2)

    async def test_a_token_refused_early_is_replaced_once(self):
        await self.follow()
        answers = [reply({}, 401), reply({"data": [self.stream()]})]
        self.web.answer(f"{TWITCH_API}/streams", lambda call: answers.pop(0))
        await self.notify.poll_twitch()
        self.assertEqual(len(self.web.called(TWITCH_TOKEN_URL)), 2)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_many_streamers_go_in_batches_of_one_hundred(self):
        for index in range(150):
            await self.notify._twitch_store(self.guild.id, {"id": str(index), "login": f"s{index}", "display_name": f"S{index}"}, self.channel.id, "x", None, None)
        await self.notify.poll_twitch()
        calls = self.web.called(f"{TWITCH_API}/streams")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(sum(1 for key, _ in call["params"] if key == "user_id") <= 100 for call in calls))

    async def test_a_dropped_stream_that_returns_soon_stays_quiet_and_a_later_one_alerts(self):
        await self.follow()
        self.live(self.stream("s1"))
        await self.notify.poll_twitch()
        self.live()
        await self.notify.poll_twitch()
        self.now += 300
        self.live(self.stream("s2"))
        await self.notify.poll_twitch()
        self.assertEqual(len(self.channel.sent), 1, "back within ten minutes is the same stream to members")
        self.live()
        await self.notify.poll_twitch()
        self.now += 1000
        self.live(self.stream("s3"))
        await self.notify.poll_twitch()
        self.assertEqual(len(self.channel.sent), 2)

    async def test_a_restart_does_not_alert_again(self):
        await self.follow()
        self.live(self.stream())
        await self.notify.poll_twitch()
        fresh = Notifications(self.bot)
        fresh.outside = self.web
        await fresh.poll_twitch()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_text_from_twitch_cannot_ping_or_run_a_template(self):
        await self.follow("{title}")
        self.live(self.stream(title="@everyone <@&123456789012345678> {server}"))
        await self.notify.poll_twitch()
        sent = self.channel.sent[0]
        mentions = sent.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertEqual(mentions.roles, [])
        self.assertFalse(mentions.users)
        self.assertIn("{server}", sent.content, "a title is only ever text")
        self.assertNotIn(self.guild.name, sent.content)

    async def test_everyone_only_pings_when_the_format_asks(self):
        await self.follow("{everyone} {name} is live")
        self.live(self.stream())
        await self.notify.poll_twitch()
        sent = self.channel.sent[0]
        self.assertTrue(sent.content.startswith("@everyone SamplePlays"))
        self.assertTrue(sent.kwargs["allowed_mentions"].everyone)

    async def test_a_chosen_role_pings_first_or_where_the_format_puts_it(self):
        await self.follow("{name} is live", role=self.role_member.id)
        self.live(self.stream())
        await self.notify.poll_twitch()
        self.assertEqual(self.channel.sent[0].content, f"{self.role_member.mention} SamplePlays is live")
        self.assertEqual([r.id for r in self.channel.sent[0].kwargs["allowed_mentions"].roles], [self.role_member.id])
        text_ = alert_text("Hey {role}, {name}", self.role_member.id, values={"name": "Z"})
        self.assertEqual(text_, f"Hey {self.role_member.mention}, Z")
        self.assertEqual([r.id for r in alert_mentions(f"<@&{self.role_mod.id}> go", None).roles], [self.role_mod.id])


class TwitchFailureTest(TwitchCase):
    async def status_error(self) -> str | None:
        return self.notify._status.get(f"twitch:{self.guild.id}", {}).get("error")

    async def test_bad_credentials_are_recorded_and_nothing_breaks(self):
        await self.follow()
        self.web.answer(TWITCH_TOKEN_URL, reply({"status": 403, "message": "invalid client secret"}, 403))
        await self.notify.poll_twitch()
        self.assertIn("refused the client ID or secret", await self.status_error())

    async def test_an_outage_changes_no_state(self):
        await self.follow()
        self.live(self.stream())
        await self.notify.poll_twitch()
        self.web.answer(f"{TWITCH_API}/streams", OutsideError("The service took too long to answer."))
        await self.notify.poll_twitch()
        self.assertIn("too long", await self.status_error())
        row = await self.db.one("SELECT live_id FROM twitch_subs")
        self.assertEqual(row["live_id"], "s1", "a failed check must not read as the stream having ended")
        self.live(self.stream())
        await self.notify.poll_twitch()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIsNone(await self.status_error())

    async def test_rate_limit_waits_for_twitchs_reset_time(self):
        await self.follow()
        self.web.answer(f"{TWITCH_API}/streams", reply({}, 429, {"Ratelimit-Reset": str(int(self.now + 30))}))
        await self.notify.poll_twitch()
        calls = len(self.web.called(f"{TWITCH_API}/streams"))
        await self.notify.poll_twitch()
        self.assertEqual(len(self.web.called(f"{TWITCH_API}/streams")), calls, "no call until the reset")
        self.now += 31
        self.live(self.stream())
        await self.notify.poll_twitch()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_server_error_or_garbage_is_survived(self):
        await self.follow()
        self.web.answer(f"{TWITCH_API}/streams", reply({}, 500))
        await self.notify.poll_twitch()
        self.web.answer(f"{TWITCH_API}/streams", reply(b"<html>not json</html>"))
        await self.notify.poll_twitch()
        self.web.answer(f"{TWITCH_API}/streams", reply([1, 2, 3]))
        await self.notify.poll_twitch()
        self.web.answer(f"{TWITCH_API}/streams", {"data": [1, "x", {"no": "id"}]})
        await self.notify.poll_twitch()
        self.web.answer(TWITCH_TOKEN_URL, {"access_token": "token-2", "expires_in": "soon"})
        self.now += 6000
        await self.notify.poll_twitch()
        self.assertEqual(self.channel.sent, [])

    async def test_a_deleted_channel_is_noted_and_the_alert_waits(self):
        await self.follow()
        self.guild.channels.remove(self.channel)
        self.live(self.stream())
        await self.notify.poll_twitch()
        row = await self.db.one("SELECT live_id, last_error FROM twitch_subs")
        self.assertIsNone(row["live_id"])
        self.assertIn("no longer exists", row["last_error"])

    async def test_a_refused_post_is_noted(self):
        await self.follow()

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.channel.send = refuse
        self.live(self.stream())
        await self.notify.poll_twitch()
        self.assertIn("Discord refused", (await self.db.one("SELECT last_error FROM twitch_subs"))["last_error"])

    async def test_one_servers_trouble_leaves_another_alone(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        other.me = self.me
        theirs = FakeChannel(other, "alerts")
        await self.bot.settings.save(other.id, "notifications", {"twitch_client_id": "bad-client", "twitch_client_secret": "bad"})
        await self.notify._twitch_store(other.id, {"id": "111", "login": "sampleplays", "display_name": "SamplePlays"}, theirs.id, "{name}", None, None)
        await self.follow()
        self.live(self.stream())

        def token(call):
            if call["data"]["client_id"] == "bad-client":
                return reply({}, 403)
            return reply({"access_token": "good", "expires_in": 5000})
        self.web.answer(TWITCH_TOKEN_URL, token)
        await self.notify.poll_twitch()
        self.assertEqual(theirs.sent, [])
        self.assertEqual(len(self.channel.sent), 1)

    async def test_the_timer_always_comes_back_and_does_not_hold_up_others(self):
        await self.follow()
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow():
            entered.set()
            await release.wait()
            raise RuntimeError("the round blew up")
        with mock.patch.object(self.notify, "poll_twitch", slow):
            await self.bot.timers.run_due(time.time() + 121)
            await asyncio.wait_for(entered.wait(), 1)
            self.assertIsNotNone(await self.bot.timers.get_key("notify.twitch", "notify.twitch"))
            self.assertGreater((await self.bot.timers.get_key("notify.twitch", "notify.twitch"))["due_at"], self.now)
            await self.notify._twitch_due({}, {})  # a second due time while the round runs doesn't start another
            release.set()
            await self.notify._rounds["twitch"]


class TwitchCommandTest(TwitchCase):
    async def test_add_list_online_move_format_remove(self):
        ctx = self.context()
        await self.notify.twitch_add.callback(self.notify, ctx, "https://www.twitch.tv/SamplePlays", None)
        self.assertEqual(ctx.fails, [])
        self.assertEqual(self.web.called(f"{TWITCH_API}/users")[0]["params"], [("login", "sampleplays")])
        row = await self.db.one("SELECT * FROM twitch_subs")
        self.assertEqual((row["stream_id"], row["channel_id"], row["fmt"]), ("111", self.channel.id, "{name} is live: {link}"))
        await self.notify.twitch.callback(self.notify, ctx)
        self.assertIn("SamplePlays", ctx.sent[0]["embed"].description)
        self.live(self.stream())
        await self.notify.twitch_online.callback(self.notify, ctx)
        self.assertIn("Chess", ctx.sent[1]["embed"].description)
        other = FakeChannel(self.guild, "other")
        await self.notify.twitch_move.callback(self.notify, ctx, "sampleplays", other)
        await self.notify.twitch_format.callback(self.notify, ctx, "SAMPLEPLAYS", format="{name}!")
        row = await self.db.one("SELECT * FROM twitch_subs")
        self.assertEqual((row["channel_id"], row["fmt"]), (other.id, "{name}!"))
        await self.notify.twitch_remove.callback(self.notify, ctx, "SamplePlays")
        self.assertIsNone(await self.db.one("SELECT * FROM twitch_subs"))
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "twitch.remove")

    async def test_adding_again_updates_the_same_row(self):
        ctx = self.context()
        await self.notify.twitch_add.callback(self.notify, ctx, "sampleplays", None, format="one {name}")
        await self.notify.twitch_add.callback(self.notify, ctx, "sampleplays", None, format="two {name}")
        rows = await self.db.all("SELECT fmt FROM twitch_subs")
        self.assertEqual(rows, [{"fmt": "two {name}"}])

    async def test_refusals(self):
        ctx = self.context()
        await self.notify.twitch_add.callback(self.notify, ctx, "not a name!", None)
        self.web.answer(f"{TWITCH_API}/users", {"data": []})
        await self.notify.twitch_add.callback(self.notify, ctx, "nobodyhere", None)
        await self.notify.twitch_remove.callback(self.notify, ctx, "nobody")
        await self.notify.twitch_move.callback(self.notify, ctx, "nobody", self.channel)
        await self.notify.twitch_format.callback(self.notify, ctx, "nobody", format="x")
        self.assertEqual(len(ctx.fails), 5)

    async def test_without_credentials_it_says_what_to_do(self):
        await self.bot.settings.save(self.guild.id, "notifications", {"twitch_client_id": "", "twitch_client_secret": ""})
        ctx = self.context()
        await self.notify.twitch_add.callback(self.notify, ctx, "sampleplays", None)
        self.assertIn("client ID", ctx.fails[0])
        await self.notify.poll_twitch()
        self.assertEqual(self.web.called(f"{TWITCH_API}/streams"), [])

    async def test_ids_in_another_server_are_not_reachable_by_name(self):
        await self.notify._twitch_store(self.guild.id + 1, {"id": "111", "login": "sampleplays", "display_name": "SamplePlays"}, self.channel.id, "x", None, None)
        ctx = self.context()
        await self.notify.twitch_remove.callback(self.notify, ctx, "sampleplays")
        self.assertEqual(len(ctx.fails), 1)
        self.assertIsNotNone(await self.db.one("SELECT 1 FROM twitch_subs"))


class TwitchDashboardTest(TwitchCase):
    async def test_the_secret_never_reaches_a_browser(self):
        for client in (await self.client(), await self.client(access="staff")):
            body = await (await client.get(self.path("/settings/notifications"))).json()
            self.assertEqual(body["twitch_client_secret"], SECRET_MASK)
            self.assertEqual(body["twitch_client_id"], SECRET_MASK)
        client = await self.client()
        text_ = await (await client.get(self.path("/twitch"))).text()
        self.assertNotIn("secret-value", text_)
        self.assertNotIn("client-id-1234", text_)
        self.assertIn('"client_id_hint": "1234"', text_)
        saved = await client.put(self.path("/settings/notifications"), json=body | {"alert_channel": str(self.channel.id)})
        answer = await saved.text()
        self.assertNotIn("secret-value", answer)
        self.assertEqual(self.settings("notifications")["twitch_client_secret"], "secret-value", "sending the mask back keeps the stored value")
        self.assertEqual(self.settings("notifications")["twitch_client_id"], "client-id-1234")
        await client.put(self.path("/settings/notifications"), json={"twitch_client_secret": "replacement"})
        self.assertEqual(self.settings("notifications")["twitch_client_secret"], "replacement")

    async def test_add_edit_test_delete(self):
        client = await self.client()
        response = await client.post(self.path("/twitch"), json={"name": "sampleplays", "channel_id": str(self.channel.id), "fmt": "{name} {link}", "role_id": str(self.role_member.id)})
        self.assertEqual(response.status, 200)
        data = await (await client.get(self.path("/twitch"))).json()
        row = data["rows"][0]
        self.assertEqual((row["stream_id"], row["channel_id"], row["role_id"], row["live"]), ("111", str(self.channel.id), str(self.role_member.id), False))
        response = await client.put(self.path("/twitch/111"), json={"channel_id": str(self.channel.id), "fmt": "{everyone} {name}", "role_id": None})
        self.assertEqual(response.status, 200)
        response = await client.post(self.path("/twitch/111/test"))
        self.assertEqual(response.status, 200)
        sent = self.channel.sent[0]
        self.assertEqual(sent.content, "@everyone SamplePlays")
        self.assertFalse(sent.kwargs["allowed_mentions"].everyone, "a test pings nobody")
        self.assertEqual((await client.delete(self.path("/twitch/111"))).status, 200)
        self.assertEqual((await client.delete(self.path("/twitch/111"))).status, 404)
        actions = [row["action"] for row in await self.bot.activity_log.recent(self.guild.id)]
        self.assertEqual(actions[:4], ["twitch.remove", "twitch.test", "twitch.edit", "twitch.add"])

    async def test_bad_input_names_the_field(self):
        client = await self.client()
        good = {"name": "sampleplays", "channel_id": str(self.channel.id)}
        for changes, field in (
            ({"channel_id": None}, "channel_id"),
            ({"channel_id": str(new_id())}, "channel_id"),
            ({"role_id": "x"}, "role_id"),
            ({"role_id": str(new_id())}, "role_id"),
            ({"fmt": "x" * 1001}, "fmt"),
            ({"fmt": 5}, "fmt"),
            ({"name": ""}, "name"),
            ({"name": 5}, "name"),
        ):
            response = await client.post(self.path("/twitch"), json={**good, **changes})
            self.assertEqual(response.status, 400, changes)
            self.assertIn(field, (await response.json())["fields"], changes)
        self.assertEqual((await client.post(self.path("/twitch"), json=[1])).status, 400)

    async def test_a_channel_in_another_server_is_refused(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        theirs = FakeChannel(other, "their-channel")
        client = await self.client()
        response = await client.post(self.path("/twitch"), json={"name": "sampleplays", "channel_id": str(theirs.id)})
        self.assertEqual(response.status, 400)

    async def test_search_and_connection_check(self):
        self.web.answer(f"{TWITCH_API}/search/channels", {"data": [{"id": "9", "broadcaster_login": "abc", "display_name": "ABC", "is_live": True, "thumbnail_url": "https://x/y.png"}]})
        client = await self.client()
        self.assertEqual(await (await client.get(self.path("/twitch/channels?query="))).json(), [])
        rows = await (await client.get(self.path("/twitch/channels?query=ab"))).json()
        self.assertEqual(rows[0]["login"], "abc")
        self.assertEqual((await client.post(self.path("/twitch/check"))).status, 200)
        self.web.answer(TWITCH_TOKEN_URL, reply({}, 403))
        response = await client.post(self.path("/twitch/check"))
        self.assertEqual(response.status, 502)
        self.assertIn("refused", (await response.json())["error"])

    async def test_another_servers_rows_are_out_of_reach(self):
        await self.notify._twitch_store(self.guild.id + 1, {"id": "222", "login": "other", "display_name": "Other"}, self.channel.id, "x", None, None)
        client = await self.client()
        self.assertEqual((await (await client.get(self.path("/twitch"))).json())["rows"], [])
        self.assertEqual((await client.delete(self.path("/twitch/222"))).status, 404)
        self.assertEqual((await client.put(self.path("/twitch/222"), json={"channel_id": str(self.channel.id)})).status, 404)
        self.assertEqual((await client.post(self.path("/twitch/222/test"))).status, 404)

    async def test_staff_cannot_reach_the_alert_pages(self):
        client = await self.client(access="staff")
        for method, path in (("get", "/twitch"), ("post", "/twitch"), ("get", "/youtube"), ("post", "/youtube"), ("get", "/gamealerts"), ("post", "/gamealerts/test")):
            self.assertEqual((await getattr(client, method)(self.path(path))).status, 403, path)


class YoutubeParsingTest(BotTest):
    async def test_targets(self):
        cases = {
            CHANNEL: ("channel", CHANNEL),
            f"https://www.youtube.com/channel/{CHANNEL}/videos": ("channel", CHANNEL),
            "@Handle": ("page", "/@Handle"),
            "https://youtube.com/@handle/about": ("page", "/@handle"),
            "youtube.com/@handle": ("page", "/@handle"),
            "https://www.youtube.com/c/Custom": ("page", "/c/Custom"),
            "https://www.youtube.com/user/Older": ("page", "/user/Older"),
            "https://www.youtube.com/plainname": ("page", "/plainname"),
            "https://www.youtube.com/watch?v=abcdefghijk&t=5": ("video", "abcdefghijk"),
            "https://youtu.be/abcdefghijk?si=x": ("video", "abcdefghijk"),
            "https://m.youtube.com/shorts/abcdefghijk": ("video", "abcdefghijk"),
            "https://www.youtube.com/live/abcdefghijk": ("video", "abcdefghijk"),
            "Some Channel Name": ("search", "Some Channel Name"),
        }
        for value, expected in cases.items():
            self.assertEqual(youtube_target(value), expected, value)
        for bad in ("https://notyoutube.com.evil.example/@x", "https://evil.example/youtube.com", "https://www.youtube.com/watch?v=short", "https://www.youtube.com/a/b/c"):
            with self.assertRaises(ServiceError, msg=bad):
                youtube_target(bad)

    async def test_feed(self):
        listing = read_feed(feed("aaaaaaaaaaa", "bbbbbbbbbbb"))
        self.assertEqual(listing.name, "Sample Channel")
        self.assertEqual([v.id for v in listing.videos], ["aaaaaaaaaaa", "bbbbbbbbbbb"])
        self.assertEqual(listing.videos[0].title, "Title of aaaaaaaaaaa")
        self.assertEqual(listing.videos[0].link, "https://www.youtube.com/watch?v=aaaaaaaaaaa")
        self.assertIsNotNone(listing.videos[0].published)
        self.assertIsNone(read_feed(b"<html>Error 404 (Not Found)!!1</html><broken"))
        self.assertEqual(read_feed(feed()).videos, [])

    async def test_a_body_is_never_opened_as_a_file_path(self):
        with mock.patch("builtins.open", side_effect=AssertionError("opened a file")):
            read_feed(feed("aaaaaaaaaaa"))

    async def test_videos_page(self):
        listing = read_videos_page(videos_page("aaaaaaaaaaa", "bbbbbbbbbbb"))
        self.assertEqual((listing.name, [v.id for v in listing.videos]), ("Sample Channel", ["aaaaaaaaaaa", "bbbbbbbbbbb"]))
        self.assertEqual(listing.videos[0].title, "Page title of aaaaaaaaaaa")
        older = '<script>var ytInitialData = {"videoRenderer": {"videoId": "ccccccccccc", "title": {"runs": [{"text": "Old "}, {"text": "shape"}]}}};</script>'
        self.assertEqual(read_videos_page(older).videos[0].title, "Old shape")
        self.assertIsNone(read_videos_page("<html>no data</html>"))
        self.assertIsNone(read_videos_page("<script>var ytInitialData = {broken};</script>"))

    async def test_channel_and_search_pages(self):
        self.assertEqual(read_channel_page(channel_page()), (CHANNEL, "Sample & Channel"))
        self.assertEqual(read_channel_page(f'<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL}">'), (CHANNEL, CHANNEL))
        self.assertIsNone(read_channel_page("<html></html>"))
        page = f'"channelRenderer":{{"channelId":"{CHANNEL}","title":{{"simpleText":"Quoted \\"Name\\""}}'
        self.assertEqual(read_search_page(page), (CHANNEL, 'Quoted "Name"'))
        self.assertIsNone(read_search_page("nothing"))

    async def test_what_counts_as_new(self):
        now = time.time()
        video = lambda name, published=None: Video(name, name, "link", published)
        listing = Listing("x", [video("new2"), video("new1"), video("old1"), video("old2")])
        self.assertEqual(new_videos(listing, None, now), [], "the first look only records")
        self.assertEqual([v.id for v in new_videos(listing, ["old1", "old2"], now)], ["new1", "new2"], "oldest first")
        self.assertEqual(new_videos(listing, ["new2", "new1", "old1", "old2"], now), [])
        # A longer list than we've seen, such as the channel page after the feed, doesn't make older videos look new.
        self.assertEqual(new_videos(Listing("x", [video("a"), video("b"), video("c")]), ["a"], now), [])
        # An old video that reappears above what we know stays quiet when its date gives it away.
        self.assertEqual(new_videos(Listing("x", [video("back", now - 9 * 86400), video("a")]), ["a"], now), [])
        # A feed that shares nothing with ours announces only the newest. A channel page proves nothing, since it lists different videos.
        self.assertEqual([v.id for v in new_videos(Listing("x", [video("y", now), video("z", now)]), ["a"], now)], ["y"])
        self.assertEqual(new_videos(Listing("x", [video("y"), video("z")]), ["a"], now), [])
        # A video from before the channel was first followed stays quiet, such as a Short the channel page never listed.
        self.assertEqual(new_videos(Listing("x", [video("short", now - 100), video("a")]), ["a"], now, since=now - 50), [])
        self.assertEqual([v.id for v in new_videos(Listing("x", [video("short", now - 10), video("a")]), ["a"], now, since=now - 50)], ["short"])
        # A burst is cut to the newest few.
        burst = Listing("x", [video(f"v{i}") for i in range(6)] + [video("a")])
        self.assertEqual([v.id for v in new_videos(burst, ["a"], now)], ["v2", "v1", "v0"])


class YoutubeCase(CaseBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.web.answer(YOUTUBE_FEED, reply(feed("aaaaaaaaaaa")))

    async def follow(self, fmt: str = "{author} posted {title}: {link}", channel=None, guild_id=None, shorts: bool = True, youtube_id: str = CHANNEL) -> None:
        await self.notify._youtube_store(guild_id or self.guild.id, youtube_id, "Sample Channel", (channel or self.channel).id, fmt, None, shorts, None)


class YoutubeAlertTest(YoutubeCase):
    async def test_following_records_what_exists_and_only_new_videos_alert(self):
        await self.follow()
        await self.notify.poll_youtube()
        self.assertEqual(self.channel.sent, [])
        self.web.answer(YOUTUBE_FEED, reply(feed("bbbbbbbbbbb", "aaaaaaaaaaa")))
        await self.notify.poll_youtube()
        self.assertEqual(self.channel.sent[0].content, "Sample Channel posted Title of bbbbbbbbbbb: https://www.youtube.com/watch?v=bbbbbbbbbbb")
        await self.notify.poll_youtube()
        self.assertEqual(len(self.channel.sent), 1)
        mentions = self.channel.sent[0].kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone or mentions.users)

    async def test_the_feed_is_read_by_channel_id(self):
        await self.follow()
        self.assertEqual(self.web.called(YOUTUBE_FEED)[0]["params"], {"channel_id": CHANNEL})

    async def test_every_following_server_gets_it_from_one_fetch(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        theirs = FakeChannel(other, "videos")
        await self.follow()
        await self.follow(channel=theirs, guild_id=other.id)
        before = len(self.web.called(YOUTUBE_FEED))
        self.web.answer(YOUTUBE_FEED, reply(feed("bbbbbbbbbbb", "aaaaaaaaaaa")))
        await self.notify.poll_youtube()
        self.assertEqual(len(self.web.called(YOUTUBE_FEED)) - before, 1)
        self.assertEqual((len(self.channel.sent), len(theirs.sent)), (1, 1))

    async def test_shorts_can_be_left_out(self):
        await self.follow(shorts=False)
        self.web.answer(YOUTUBE_FEED, reply(feed("ccccccccccc", "aaaaaaaaaaa", shorts=("ccccccccccc",))))
        await self.notify.poll_youtube()
        self.assertEqual(self.channel.sent, [])
        self.web.answer(YOUTUBE_FEED, reply(feed("ddddddddddd", "ccccccccccc", "aaaaaaaaaaa", shorts=("ccccccccccc",))))
        await self.notify.poll_youtube()
        self.assertEqual(len(self.channel.sent), 1, "a regular video still goes out")

    async def test_a_channel_that_stops_being_followed_starts_fresh_later(self):
        await self.follow()
        ctx = self.context()
        await self.notify.youtube_remove.callback(self.notify, ctx, "Sample Channel")
        self.assertIsNone(await self.db.one("SELECT 1 FROM youtube_state"))
        self.web.answer(YOUTUBE_FEED, reply(feed("bbbbbbbbbbb", "aaaaaaaaaaa")))
        await self.follow()
        await self.notify.poll_youtube()
        self.assertEqual(self.channel.sent, [])

    async def test_the_template_engine_cannot_be_reached_from_a_title(self):
        await self.follow("{title}")
        body = feed("bbbbbbbbbbb", "aaaaaaaaaaa").replace(b"Title of bbbbbbbbbbb", b"{server} @everyone")
        self.web.answer(YOUTUBE_FEED, reply(body))
        await self.notify.poll_youtube()
        self.assertEqual(self.channel.sent[0].content, "{server} @everyone")
        self.assertFalse(self.channel.sent[0].kwargs["allowed_mentions"].everyone)


class YoutubeFailureTest(YoutubeCase):
    def feed_down(self, status: int = 404) -> None:
        self.web.answer(YOUTUBE_FEED, reply(b"<html>Error 404</html>", status))

    async def test_when_the_feed_is_down_the_channel_page_stands_in(self):
        await self.follow()
        self.feed_down()
        self.web.answer(f"{YOUTUBE_SITE}/channel/{CHANNEL}/videos", reply(videos_page("bbbbbbbbbbb", "aaaaaaaaaaa")))
        await self.notify.poll_youtube()
        self.assertEqual(self.channel.sent[0].content, "Sample Channel posted Page title of bbbbbbbbbbb: https://www.youtube.com/watch?v=bbbbbbbbbbb")
        self.assertEqual(self.web.called(f"{YOUTUBE_SITE}/channel/{CHANNEL}/videos")[0]["headers"]["Cookie"], "SOCS=CAI; CONSENT=YES+")

    async def test_shorts_the_channel_page_never_listed_stay_quiet_when_the_feed_returns(self):
        self.feed_down()
        self.web.answer(f"{YOUTUBE_SITE}/channel/{CHANNEL}/videos", reply(videos_page("aaaaaaaaaaa")))
        await self.follow()
        older = feed("ccccccccccc", "aaaaaaaaaaa", shorts=("ccccccccccc",), published=time.time() - 3600)
        self.web.answer(YOUTUBE_FEED, reply(older))
        await self.notify.poll_youtube()
        self.assertEqual(self.channel.sent, [])

    async def test_a_feed_that_was_down_when_following_only_records_on_the_first_look(self):
        self.feed_down()
        await self.follow()
        self.assertIsNone(await self.db.one("SELECT seen FROM youtube_state"))
        self.web.answer(YOUTUBE_FEED, reply(feed("bbbbbbbbbbb", "aaaaaaaaaaa")))
        await self.notify.poll_youtube()
        self.assertEqual(self.channel.sent, [])
        self.web.answer(YOUTUBE_FEED, reply(feed("ccccccccccc", "bbbbbbbbbbb", "aaaaaaaaaaa")))
        await self.notify.poll_youtube()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_everything_down_backs_off_records_why_and_recovers(self):
        await self.follow()
        self.feed_down(500)
        self.web.answer(f"{YOUTUBE_SITE}/channel/", OutsideError("The service took too long to answer."))
        await self.notify.poll_youtube()
        state = await self.db.one("SELECT * FROM youtube_state")
        self.assertEqual(state["failures"], 1)
        self.assertGreater(state["next_check"], self.now)
        self.assertIn("too long", state["last_error"])
        self.assertEqual(state["seen"], json.dumps(["aaaaaaaaaaa"]), "a failure keeps what we know")
        calls = len(self.web.calls)
        await self.notify.poll_youtube()
        self.assertEqual(len(self.web.calls), calls, "it waits out the back-off")
        self.now = state["next_check"] + 1
        self.web.answer(YOUTUBE_FEED, reply(feed("bbbbbbbbbbb", "aaaaaaaaaaa")))
        await self.notify.poll_youtube()
        self.assertEqual(len(self.channel.sent), 1)
        state = await self.db.one("SELECT * FROM youtube_state")
        self.assertEqual((state["failures"], state["last_error"]), (0, None))

    async def test_a_broken_page_is_a_failure_and_a_404_does_not_unfollow(self):
        await self.follow()
        self.feed_down()
        self.web.answer(f"{YOUTUBE_SITE}/channel/", reply(b"<html></html>"))
        await self.notify.poll_youtube()
        self.assertIsNotNone(await self.db.one("SELECT 1 FROM youtube_subs"))
        self.assertEqual((await self.db.one("SELECT failures FROM youtube_state"))["failures"], 1)

    async def test_trouble_with_one_channel_leaves_the_next_alone(self):
        await self.follow()
        await self.follow(youtube_id=OTHER_CHANNEL)
        await self.db.run("UPDATE youtube_state SET seen = ?", (json.dumps(["aaaaaaaaaaa"]),))

        def by_channel(call):
            if call["params"]["channel_id"] == CHANNEL:
                raise OutsideError("down")
            return reply(feed("bbbbbbbbbbb", "aaaaaaaaaaa"))
        self.web.answer(YOUTUBE_FEED, by_channel)
        self.web.answer(f"{YOUTUBE_SITE}/channel/{CHANNEL}", reply(b"", 500))
        await self.notify.poll_youtube()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_gone_channel_or_refused_post_only_notes_it(self):
        other = FakeChannel(self.guild, "other")
        await self.follow(channel=other)
        await self.follow(guild_id=self.guild.id + 1)
        self.guild.channels.remove(other)
        self.web.answer(YOUTUBE_FEED, reply(feed("bbbbbbbbbbb", "aaaaaaaaaaa")))
        await self.notify.poll_youtube()
        row = await self.db.one("SELECT last_error FROM youtube_subs WHERE guild_id = ?", (self.guild.id,))
        self.assertIn("no longer exists", row["last_error"])
        self.assertIsNotNone(await self.db.one("SELECT seen FROM youtube_state"))


class YoutubeCommandTest(YoutubeCase):
    def site(self, page: str) -> None:
        self.web.answer(f"{YOUTUBE_SITE}/@", page)

    async def test_add_by_handle_then_list_move_format_remove(self):
        self.site(channel_page())
        ctx = self.context()
        await self.notify.youtube_add.callback(self.notify, ctx, "@sample", None)
        self.assertEqual(ctx.fails, [])
        row = await self.db.one("SELECT * FROM youtube_subs")
        self.assertEqual((row["youtube_id"], row["youtube_name"], row["channel_id"], row["fmt"]), (CHANNEL, "Sample & Channel", self.channel.id, "New video from {author}: {link}"))
        await self.notify.youtube.callback(self.notify, ctx)
        self.assertIn("Sample & Channel", ctx.sent[0]["embed"].description)
        other = FakeChannel(self.guild, "other")
        await self.notify.youtube_move.callback(self.notify, ctx, CHANNEL, other)
        await self.notify.youtube_format.callback(self.notify, ctx, "sample & channel", format="{title}")
        row = await self.db.one("SELECT * FROM youtube_subs")
        self.assertEqual((row["channel_id"], row["fmt"]), (other.id, "{title}"))
        await self.notify.youtube_remove.callback(self.notify, ctx, f"https://www.youtube.com/channel/{CHANNEL}")
        self.assertIsNone(await self.db.one("SELECT * FROM youtube_subs"))

    async def test_add_by_video_link_uses_oembed_then_the_channel_page(self):
        self.web.answer(f"{YOUTUBE_SITE}/oembed", {"author_name": "Sample", "author_url": "https://www.youtube.com/@sample"})
        self.site(channel_page())
        ctx = self.context()
        await self.notify.youtube_add.callback(self.notify, ctx, "https://youtu.be/abcdefghijk", None)
        self.assertEqual(ctx.fails, [])
        self.assertEqual(self.web.called(f"{YOUTUBE_SITE}/oembed")[0]["params"]["url"], f"{YOUTUBE_SITE}/watch?v=abcdefghijk")
        self.assertEqual((await self.db.one("SELECT youtube_id FROM youtube_subs"))["youtube_id"], CHANNEL)

    async def test_add_by_channel_id_checks_the_channel_page_not_the_feed(self):
        self.web.answer(YOUTUBE_FEED, reply(b"", 404))
        self.web.answer(f"{YOUTUBE_SITE}/channel/{CHANNEL}", channel_page())
        self.web.answer(f"{YOUTUBE_SITE}/channel/{CHANNEL}/videos", videos_page("aaaaaaaaaaa"))
        ctx = self.context()
        await self.notify.youtube_add.callback(self.notify, ctx, CHANNEL, None)
        self.assertEqual(ctx.fails, [])
        self.assertEqual((await self.db.one("SELECT seen FROM youtube_state"))["seen"], json.dumps(["aaaaaaaaaaa"]))

    async def test_add_by_name_takes_the_top_search_result(self):
        self.web.answer(f"{YOUTUBE_SITE}/results", f'"channelRenderer":{{"channelId":"{CHANNEL}","title":{{"simpleText":"Top Result"}}')
        ctx = self.context()
        await self.notify.youtube_add.callback(self.notify, ctx, "some channel", None)
        self.assertEqual(self.web.calls[0]["url"], f"{YOUTUBE_SITE}/results?search_query=some%20channel&sp=EgIQAg%253D%253D")
        self.assertEqual((await self.db.one("SELECT youtube_name FROM youtube_subs"))["youtube_name"], "Top Result")

    async def test_only_youtube_is_ever_called_for_a_typed_link(self):
        ctx = self.context()
        await self.notify.youtube_add.callback(self.notify, ctx, "https://evil.example/youtube.com/@x", None)
        self.assertEqual(self.web.calls, [])
        self.assertIn("isn't a YouTube link", ctx.fails[0])

    async def test_refusals(self):
        ctx = self.context()
        self.web.answer(f"{YOUTUBE_SITE}/@nobody", reply(b"", 404))
        await self.notify.youtube_add.callback(self.notify, ctx, "@nobody", None)
        self.web.answer(f"{YOUTUBE_SITE}/oembed", reply(b"", 404))
        await self.notify.youtube_add.callback(self.notify, ctx, "https://youtu.be/abcdefghijk", None)
        self.web.answer(f"{YOUTUBE_SITE}/results", "no channels here")
        await self.notify.youtube_add.callback(self.notify, ctx, "no such channel", None)
        await self.notify.youtube_add.callback(self.notify, ctx, "@down", None)
        await self.notify.youtube_remove.callback(self.notify, ctx, "nobody")
        await self.notify.youtube_move.callback(self.notify, ctx, "nobody", self.channel)
        await self.notify.youtube_format.callback(self.notify, ctx, "nobody", format="x")
        self.assertEqual(len(ctx.fails), 7)
        self.assertIsNone(await self.db.one("SELECT 1 FROM youtube_subs"))


class YoutubeDashboardTest(YoutubeCase):
    async def test_add_edit_test_delete(self):
        self.web.answer(f"{YOUTUBE_SITE}/@", channel_page())
        client = await self.client()
        response = await client.post(self.path("/youtube"), json={"link": "@sample", "channel_id": str(self.channel.id), "fmt": "{author} {link}", "shorts": False})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["youtube_name"], "Sample & Channel")
        row = (await (await client.get(self.path("/youtube"))).json())["rows"][0]
        self.assertEqual((row["youtube_id"], row["shorts"], row["ok_at"] is not None), (CHANNEL, False, True))
        self.assertEqual((await client.put(self.path(f"/youtube/{CHANNEL}"), json={"channel_id": str(self.channel.id), "fmt": "{everyone} {title}", "shorts": True})).status, 200)
        self.assertEqual((await client.post(self.path(f"/youtube/{CHANNEL}/test"))).status, 200)
        sent = self.channel.sent[0]
        self.assertEqual(sent.content, "@everyone A sample video title")
        self.assertFalse(sent.kwargs["allowed_mentions"].everyone)
        self.assertEqual((await client.delete(self.path(f"/youtube/{CHANNEL}"))).status, 200)
        self.assertEqual((await (await client.get(self.path("/youtube"))).json())["rows"], [])

    async def test_bad_input_and_foreign_rows(self):
        await self.follow(guild_id=self.guild.id + 1)
        client = await self.client()
        for body, field in (({"link": "", "channel_id": str(self.channel.id)}, "link"), ({"link": "@x", "channel_id": str(new_id())}, "channel_id"), ({"link": "@x", "channel_id": str(self.channel.id), "fmt": "x" * 1001}, "fmt")):
            response = await client.post(self.path("/youtube"), json=body)
            self.assertEqual(response.status, 400, body)
            self.assertIn(field, (await response.json())["fields"])
        self.assertEqual((await client.post(self.path("/youtube"), json={"link": "@down", "channel_id": str(self.channel.id)})).status, 502)
        self.assertEqual((await (await client.get(self.path("/youtube"))).json())["rows"], [])
        self.assertEqual((await client.delete(self.path(f"/youtube/{CHANNEL}"))).status, 404)
        self.assertEqual((await client.post(self.path(f"/youtube/{CHANNEL}/test"))).status, 404)


def giveaway(item_id: int, title: str = "Cool Game", platforms: str = "PC, Steam", end: str = "2030-01-02 03:04:05", worth: str = "$9.99") -> dict:
    return {"id": item_id, "title": title, "worth": worth, "image": f"https://www.gamerpower.com/offers/{item_id}.jpg", "open_giveaway_url": f"https://www.gamerpower.com/open/{item_id}", "gamerpower_url": "https://www.gamerpower.com/x", "platforms": platforms, "end_date": end, "status": "Active", "type": "Game"}


def deal(game: str, price: str = "4.99", savings: str = "75.0", rating: str = "9.0", store: str = "1") -> dict:
    return {"title": f"Game {game}", "dealID": f"id%3D{game}", "gameID": game, "storeID": store, "salePrice": price, "normalPrice": "19.99", "savings": savings, "dealRating": rating, "thumb": "https://cdn.example/t.jpg"}


class GameCase(CaseBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.set_free()
        self.set_deals()

    def set_free(self, *items) -> None:
        self.web.answer(GAMERPOWER_URL, list(items))

    def set_deals(self, *items) -> None:
        self.web.answer(CHEAPSHARK_URL, list(items))

    async def enable(self, **settings) -> None:
        await self.bot.settings.save(self.guild.id, "notifications", {"alert_channel": self.channel.id, **settings})


class FreeGameTest(GameCase):
    async def test_the_first_look_only_records_then_new_games_alert_once(self):
        await self.enable()
        self.set_free(giveaway(1))
        await self.notify.poll_free_games()
        self.assertEqual(self.channel.sent, [])
        self.set_free(giveaway(2, "New Game"), giveaway(1))
        await self.notify.poll_free_games()
        await self.notify.poll_free_games()
        self.assertEqual(len(self.channel.sent), 1)
        sent = self.channel.sent[0]
        embed = sent.embeds[0]
        self.assertEqual((embed.title, embed.url, embed.image.url), ("New Game", "https://www.gamerpower.com/open/2", "https://www.gamerpower.com/offers/2.jpg"))
        self.assertIn("~~$9.99~~", embed.fields[0].value)
        self.assertIn("<t:", embed.fields[1].value)
        urls = [child.url for child in sent.kwargs["view"].children]
        self.assertIn("https://www.gamerpower.com/open/2", urls)
        self.assertIn("https://www.gamerpower.com", urls, "the service's terms ask for a link back")

    async def test_the_request_asks_for_games_and_names_itself(self):
        await self.enable()
        await self.notify.poll_free_games()
        self.assertEqual(self.web.calls[0]["params"], {"type": "game", "sort-by": "date"})
        self.assertIn("/", USER_AGENT)
        self.assertNotIn("aiohttp", USER_AGENT)

    async def test_nothing_is_called_until_a_server_wants_it(self):
        await self.notify.poll_free_games()
        await self.bot.settings.save(self.guild.id, "notifications", {"alerts_enabled": True})
        await self.notify.poll_free_games()
        await self.enable(alerts_enabled=False)
        await self.notify.poll_free_games()
        self.assertEqual(self.web.calls, [])

    async def test_platforms_filter_and_changing_them_does_not_flood(self):
        await self.enable(free_platforms=["steam"])
        await self.notify.poll_free_games()
        self.set_free(giveaway(1, "Epic One", "PC, Epic Games Store"), giveaway(2, "Steam One"))
        await self.notify.poll_free_games()
        self.assertEqual([m.embeds[0].title for m in self.channel.sent], ["Steam One"])
        await self.enable(free_platforms=["epic-games-store"])
        await self.notify.poll_free_games()
        self.assertEqual(len(self.channel.sent), 1, "a new filter looks afresh")
        self.set_free(giveaway(3, "Epic Two", "Epic Games Store"), giveaway(1, "Epic One", "PC, Epic Games Store"), giveaway(2, "Steam One"))
        await self.notify.poll_free_games()
        self.assertEqual(self.channel.sent[-1].embeds[0].title, "Epic Two")

    async def test_turning_off_and_on_looks_afresh(self):
        await self.enable()
        await self.notify.poll_free_games()
        await self.enable(alerts_enabled=False)
        await self.notify.poll_free_games()
        self.set_free(giveaway(1))
        await self.enable(alerts_enabled=True)
        await self.notify.poll_free_games()
        self.assertEqual(self.channel.sent, [])

    async def test_a_burst_is_capped_and_goes_out_oldest_first(self):
        await self.enable()
        await self.notify.poll_free_games()
        self.set_free(*(giveaway(i, f"Game {i}") for i in range(9, 0, -1)))
        await self.notify.poll_free_games()
        self.assertEqual([m.embeds[0].title for m in self.channel.sent], ["Game 5", "Game 6", "Game 7", "Game 8", "Game 9"])
        await self.notify.poll_free_games()
        self.assertEqual(len(self.channel.sent), 5, "what the limit passed over isn't saved for later")

    async def test_no_active_giveaways_is_not_a_failure(self):
        await self.enable()
        self.web.answer(GAMERPOWER_URL, reply({"status": 0, "status_message": "No active giveaways"}, 201))
        await self.notify.poll_free_games()
        self.assertIsNone(self.notify._status["gamerpower"]["error"])

    async def test_missing_end_date_and_worth_are_left_out(self):
        await self.enable()
        await self.notify.poll_free_games()
        self.set_free(giveaway(1, end="N/A", worth="N/A"))
        await self.notify.poll_free_games()
        self.assertEqual(self.channel.sent[0].embeds[0].fields, [])

    async def test_format_and_role(self):
        await self.enable(free_format="{everyone} {title} on {platforms} until {until}", alert_role=self.role_member.id)
        await self.notify.poll_free_games()
        self.set_free(giveaway(1, end="N/A"))
        await self.notify.poll_free_games()
        sent = self.channel.sent[0]
        self.assertEqual(sent.content, f"{self.role_member.mention} @everyone Cool Game on PC, Steam until while it lasts")
        mentions = sent.kwargs["allowed_mentions"]
        self.assertTrue(mentions.everyone)
        self.assertEqual([r.id for r in mentions.roles], [self.role_member.id])

    async def test_failures_back_off_and_recover(self):
        await self.enable()
        await self.notify.poll_free_games()
        self.web.answer(GAMERPOWER_URL, reply({}, 500))
        await self.notify.poll_free_games()
        self.assertIn("500", self.notify._status["gamerpower"]["error"])
        calls = len(self.web.calls)
        await self.notify.poll_free_games()
        self.assertEqual(len(self.web.calls), calls, "it waits out the back-off")
        self.now += 8 * 3600
        self.set_free(giveaway(1))
        await self.notify.poll_free_games()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIsNone(self.notify._status["gamerpower"]["error"])

    async def test_a_service_answer_of_nonsense_is_survived(self):
        await self.enable()
        await self.notify.poll_free_games()
        for answer in (reply(b"<html>"), reply({"not": "a list"}), reply([1, "x", {"no": "id"}])):
            self.web.answer(GAMERPOWER_URL, answer)
            self.now += 8 * 3600
            await self.notify.poll_free_games()
        self.assertEqual(self.channel.sent, [])

    async def test_a_refused_post_keeps_the_game_for_the_next_round(self):
        await self.enable()
        await self.notify.poll_free_games()
        self.set_free(giveaway(1))
        original = self.channel.send

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.channel.send = refuse
        await self.notify.poll_free_games()
        self.assertIn("Discord refused", self.notify._status[f"free:{self.guild.id}"]["error"])
        self.channel.send = original
        await self.notify.poll_free_games()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_deleted_channel_is_noted(self):
        await self.enable()
        await self.notify.poll_free_games()
        self.guild.channels.remove(self.channel)
        self.set_free(giveaway(1))
        await self.notify.poll_free_games()
        self.assertIn("no longer exists", self.notify._status[f"free:{self.guild.id}"]["error"])

    async def test_an_unsendable_item_is_passed_over(self):
        await self.enable()
        await self.notify.poll_free_games()
        bad = giveaway(1)
        bad["open_giveaway_url"] = "javascript:alert(1)"
        self.set_free(giveaway(2), bad)
        await self.notify.poll_free_games()
        self.assertEqual(len(self.channel.sent), 1)
        await self.notify.poll_free_games()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_each_server_keeps_its_own_record(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        theirs = FakeChannel(other, "games")
        await self.enable()
        await self.notify.poll_free_games()
        self.set_free(giveaway(1))
        await self.bot.settings.save(other.id, "notifications", {"alert_channel": theirs.id})
        await self.notify.poll_free_games()
        self.assertEqual((len(self.channel.sent), len(theirs.sent)), (1, 0), "the newcomer's first look only records")
        self.set_free(giveaway(2), giveaway(1))
        await self.notify.poll_free_games()
        self.assertEqual((len(self.channel.sent), len(theirs.sent)), (2, 1))


class DealsTest(GameCase):
    async def test_deals_follow_the_filters_and_use_the_redirect_link(self):
        await self.enable(deals_enabled=True, discount_threshold=50, deal_min_rating=8)
        await self.notify.poll_deals()
        self.assertEqual(self.web.calls[0]["params"], {"sortBy": "Recent", "onSale": "1", "pageSize": "60"})
        self.set_deals(deal("a"), deal("b", savings="20.0"), deal("c", rating="3.0"))
        await self.notify.poll_deals()
        self.assertEqual(len(self.channel.sent), 1)
        sent = self.channel.sent[0]
        self.assertEqual(sent.embeds[0].title, "Game a")
        self.assertEqual(sent.embeds[0].url, "https://www.cheapshark.com/redirect?dealID=id%3Da")
        self.assertEqual(sent.kwargs["view"].children[0].url, "https://www.cheapshark.com/redirect?dealID=id%3Da")
        self.assertIn("~~$19.99~~ $4.99", sent.embeds[0].fields[0].value)
        self.assertEqual(sent.embeds[0].fields[2].value, "Steam")

    async def test_the_same_deal_only_goes_out_again_at_a_new_price(self):
        await self.enable(deals_enabled=True)
        await self.notify.poll_deals()
        self.set_deals(deal("a"))
        await self.notify.poll_deals()
        reissued = deal("a")
        reissued["dealID"] = "fresh-id"
        self.set_deals(reissued)
        await self.notify.poll_deals()
        self.assertEqual(len(self.channel.sent), 1)
        self.set_deals(deal("a", price="2.99"))
        await self.notify.poll_deals()
        self.assertEqual(len(self.channel.sent), 2)

    async def test_stores_filter(self):
        await self.enable(deals_enabled=True, deal_stores=["7"])
        await self.notify.poll_deals()
        self.set_deals(deal("a", store="1"), deal("b", store="7"))
        await self.notify.poll_deals()
        self.assertEqual([m.embeds[0].title for m in self.channel.sent], ["Game b"])

    async def test_changing_a_limit_does_not_flood(self):
        await self.enable(deals_enabled=True, discount_threshold=90)
        self.set_deals(deal("a"))
        await self.notify.poll_deals()
        await self.notify.poll_deals()
        await self.enable(discount_threshold=50)
        await self.notify.poll_deals()
        self.assertEqual(self.channel.sent, [])

    async def test_off_by_default(self):
        await self.enable()
        await self.notify.poll_deals()
        self.assertEqual(self.web.calls, [])

    async def test_too_many_requests_waits_as_long_as_asked(self):
        await self.enable(deals_enabled=True)
        self.web.answer(CHEAPSHARK_URL, reply({}, 429, {"Retry-After": "7200"}))
        await self.notify.poll_deals()
        calls = len(self.web.calls)
        self.now += 7100
        await self.notify.poll_deals()
        self.assertEqual(len(self.web.calls), calls)
        self.now += 200
        self.set_deals()
        await self.notify.poll_deals()
        self.assertEqual(len(self.web.calls), calls + 1)

    async def test_an_outage_is_survived(self):
        await self.enable(deals_enabled=True)
        self.web.answer(CHEAPSHARK_URL, OutsideError("down"))
        await self.notify.poll_deals()
        self.assertEqual(self.notify._status["cheapshark"]["error"], "down")


class GameCommandTest(GameCase):
    async def test_alerts_joins_games_and_sets_things(self):
        group = self.bot.get_command("games")
        self.assertIsNotNone(group.get_command("alerts"))
        command = group.get_command("alerts")
        self.assertEqual(command.qualified_name, "games alerts")
        ctx = self.context()
        await command.callback(self.notify, ctx, None, None)
        self.assertIn("none, so nothing is posted", ctx.sent[0]["content"])
        await command.callback(self.notify, ctx, "deals-on", self.channel)
        cfg = self.settings("notifications")
        self.assertEqual((cfg["alert_channel"], cfg["deals_enabled"]), (self.channel.id, True))
        await command.callback(self.notify, ctx, "free-off", None)
        self.assertFalse(self.settings("notifications")["alerts_enabled"])
        self.assertEqual(ctx.fails, [])

    async def test_it_shows_up_for_discord(self):
        tree = self.bot.tree.get_command("games")
        self.assertIsNotNone(tree)
        self.assertIn("alerts", [option["name"] for option in tree.to_dict(self.bot.tree)["options"]])


class GameDashboardTest(GameCase):
    async def test_settings_and_page_data(self):
        client = await self.client()
        response = await client.put(self.path("/settings/notifications"), json={"alert_channel": str(self.channel.id), "free_platforms": ["steam", "gog"], "discount_threshold": 70, "deal_min_rating": 7.5, "deal_stores": ["1"]})
        self.assertEqual(response.status, 200)
        data = await (await client.get(self.path("/gamealerts"))).json()
        self.assertIn({"value": "steam", "label": "Steam"}, data["platforms"])
        self.assertIn({"value": "7", "label": "GOG"}, data["stores"])
        for form, field in (({"free_platforms": ["nintendo64"]}, "free_platforms"), ({"discount_threshold": 5}, "discount_threshold"), ({"deal_min_rating": 11}, "deal_min_rating"), ({"deal_stores": ["999"]}, "deal_stores"), ({"alert_channel": "x"}, "alert_channel"), ({"free_format": "x" * 1001}, "free_format")):
            response = await client.put(self.path("/settings/notifications"), json=form)
            self.assertEqual(response.status, 400, form)
            self.assertIn(field, (await response.json())["fields"])

    async def test_a_sample_goes_out_without_pings(self):
        client = await self.client()
        response = await client.post(self.path("/gamealerts/test"), json={"kind": "free"})
        self.assertEqual(response.status, 400)
        await self.enable(free_format="{everyone} {title}", alert_role=self.role_member.id)
        self.assertEqual((await client.post(self.path("/gamealerts/test"), json={"kind": "free"})).status, 200)
        self.assertEqual((await client.post(self.path("/gamealerts/test"), json={"kind": "deals"})).status, 200)
        first, second = self.channel.sent
        self.assertIn("@everyone", first.content)
        self.assertFalse(first.kwargs["allowed_mentions"].everyone or first.kwargs["allowed_mentions"].roles)
        self.assertEqual(second.embeds[0].title, "Sample Game")

    async def test_a_saved_channel_from_another_server_cannot_be_posted_to(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        theirs = FakeChannel(other, "their-channel")
        await self.bot.settings.save(self.guild.id, "notifications", {"alert_channel": theirs.id})
        client = await self.client()
        self.assertEqual((await client.post(self.path("/gamealerts/test"), json={"kind": "free"})).status, 400)
        self.assertEqual(theirs.sent, [])


class TimerTest(CaseBase):
    async def test_every_poll_has_a_timer_that_comes_back(self):
        for kind, poll in (("notify.twitch", "poll_twitch"), ("notify.youtube", "poll_youtube"), ("notify.free", "poll_free_games"), ("notify.deals", "poll_deals")):
            self.assertIsNotNone(await self.bot.timers.get_key(kind, kind), kind)
        with mock.patch.object(self.notify, "poll_twitch", side_effect=RuntimeError("boom")), mock.patch.object(self.notify, "poll_youtube", side_effect=OutsideError("boom")):
            await self.bot.timers.run_due(time.time() + 86400)
            for task in self.notify._rounds.values():
                await task
        for kind in ("notify.twitch", "notify.youtube", "notify.free", "notify.deals"):
            row = await self.bot.timers.get_key(kind, kind)
            self.assertEqual(row["tries"], 0, kind)

    async def test_loading_twice_keeps_one_timer_of_each(self):
        await self.notify.on_load()
        self.assertIsNotNone(self.bot.get_command("games alerts"))
        rows = await self.db.all("SELECT kind, COUNT(*) AS n FROM timers GROUP BY kind")
        self.assertTrue(all(row["n"] == 1 for row in rows), rows)
