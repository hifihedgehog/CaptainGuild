"""RSS feeds: reading any RSS or Atom address, posting what's new under the feed's name, and the commands and page that manage it."""
from __future__ import annotations

import asyncio
import html
import json
import time
from email.utils import formatdate

import aiohttp
import discord

from captainguild.core.ops import DiscordOps, OpsError
from captainguild.core.outside import OutsideError
from captainguild.core.module import required_permissions
from captainguild.modules.rss import CHANNEL_GONE, MAX_FEEDS, MESSAGE_LIMIT, POLL, POLL_SECONDS, POSTS_PER_ROUND, SEEN_KEPT, TRIES, Entry, RSSFeeds, entry_text, fingerprint, fresh_entries, next_seen, read_entries, refused_content, split_messages, to_markdown
from tests.fake_outside import FakeOutside, reply
from tests.fakes import BotTest, FakeChannel
from tests.gateway import FakeHTTPResponse, GatewayTest

URL = "https://blog.example.com/feed.xml"
OTHER = "https://news.example.com/rss"


def item(key: str, title: str = "", text: str = "<p>Body</p>", *, at: float | None = None, link: bool = True) -> str:
    when = f"<pubDate>{formatdate(at)}</pubDate>" if at is not None else ""
    address = f"<link>https://blog.example.com/{key}</link>" if link else ""
    return f"<item><guid isPermaLink=\"false\">{key}</guid><title>{html.escape(title or key)}</title>{address}<description>{html.escape(text)}</description>{when}</item>"


RELATIVE_GUIDS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>News</title><link>https://news.example.com/</link><description>d</description>
<item><title>Version 1.08</title><link>https://news.example.com/posts/108</link><guid>post-108</guid><description>New</description><pubDate>Sat, 07 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Version 1.07</title><link>https://news.example.com/posts/107</link><guid>post-107</guid><description>Older</description><pubDate>Sat, 31 Oct 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""


def rss(*items: str) -> bytes:
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title><link>https://blog.example.com</link><description>News</description>'
            + "".join(items) + "</channel></rss>").encode()


class ConvertTest(BotTest):
    """RS-8: the old conversion's tags read as before, and the rest lose their tags instead of showing as raw HTML."""

    def test_the_old_tags_read_as_before(self):
        self.assertEqual(to_markdown("<p>Hello <strong>world</strong></p><p>Second <small>note</small> <var>x</var></p>"), "Hello **world**\n\nSecond *note* x")
        self.assertEqual(to_markdown("one<br>two<br/>three"), "one\ntwo\nthree")
        self.assertEqual(to_markdown('<a href="https://e.com/a">a link</a>'), "[a link](https://e.com/a)")
        self.assertEqual(to_markdown('<img src="https://e.com/a.png" alt="a cat"><img src="https://e.com/b.png">'), "![a cat](https://e.com/a.png)![image](https://e.com/b.png)")
        self.assertEqual(to_markdown("<blockquote>Quoted<br>twice</blockquote><aside>Side</aside>"), "> Quoted\n> twice\n\n> Side")

    def test_entities_are_decoded_first_as_before(self):
        self.assertEqual(to_markdown("&lt;p&gt;Escaped &lt;strong&gt;twice&lt;/strong&gt;&lt;/p&gt;"), "Escaped **twice**")

    def test_a_picture_or_link_with_no_address_no_longer_breaks_the_post(self):
        self.assertEqual(to_markdown('<img alt="x"> and <a>plain</a> and <a href="javascript:alert(1)">odd</a>'), "and plain and odd")

    def test_tags_the_old_conversion_left_raw_now_read_as_markdown(self):
        text = to_markdown("<h2>Head</h2><ul><li>One</li><li>Two</li></ul><ol><li>A</li><li>B</li></ol><em>it</em> <code>x</code><pre>keep\n  this</pre><script>bad()</script><table><tr><td>a</td><td>b</td></tr></table>")
        self.assertEqual(text, "**Head**\n\n- One\n- Two\n\n1. A\n2. B\n\n*it* `x`\n\n```\nkeep\n  this\n```\n\na b")
        self.assertNotIn("<", text)


class ReadTest(BotTest):
    def test_entries_come_newest_first_by_date_even_from_a_feed_listed_oldest_first(self):
        now = time.time()
        entries = read_entries(rss(item("a", at=now - 300), item("b", at=now - 200), item("c", at=now - 100)))
        self.assertEqual([entry.key for entry in entries], ["c", "b", "a"])

    def test_an_entry_with_no_id_goes_by_its_link(self):
        entries = read_entries(b'<rss version="2.0"><channel><title>t</title><item><title>x</title><link>https://e.com/x</link></item></channel></rss>')
        self.assertEqual(entries[0].key, "https://e.com/x")

    def test_an_atom_feed_reads_and_a_page_that_is_not_a_feed_does_not(self):
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>A</title><entry><id>tag:1</id><title>One</title><link href="https://e.com/1"/><content type="html">&lt;p&gt;Hi&lt;/p&gt;</content></entry></feed>'
        [entry] = read_entries(atom)
        self.assertEqual((entry.key, entry.title, entry.link, to_markdown(entry.html)), ("tag:1", "One", "https://e.com/1", "Hi"))
        self.assertIsNone(read_entries(b"<html><body>Not a feed</body></html>"))

    def test_what_is_new_since_the_last_read(self):
        feed = [Entry(key, key, "", "") for key in ("e5", "e4", "e3", "e2", "e1")]
        self.assertEqual([e.key for e in fresh_entries(feed, None)], ["e5"], "a feed never read gives its newest")
        self.assertEqual([e.key for e in fresh_entries(feed, ["e3"])], ["e4", "e5"], "an imported last entry: only what's newer, oldest first")
        self.assertEqual(fresh_entries(feed, ["e5", "e4"]), [])
        self.assertEqual([e.key for e in fresh_entries(feed, ["gone"])], ["e5"], "nothing known left in the feed: the newest only")
        self.assertEqual(next_seen(feed, ["e3", "older"], {"e5"}), ["e4", "e3", "e2", "e1", *(fingerprint(entry) for entry in feed[1:]), "older"])

    def test_an_entry_dated_again_hides_nothing_new(self):
        """A feed with only updated dates moves an edited entry to the top, above what came in since the last read."""
        redated = [Entry(key, key, "", "") for key in ("v1.1", "v1.2", "v1.0")]
        self.assertEqual([e.key for e in fresh_entries(redated, ["v1.1", "v1.0"])], ["v1.2"])
        self.assertEqual([e.key for e in fresh_entries(redated, ["v1.1", "v1.2", "v1.0"])], [])
        long_feed = [Entry(f"e{index}", "", "", "") for index in range(SEEN_KEPT + 100)]
        self.assertLessEqual({entry.key for entry in long_feed}, set(next_seen(long_feed, None, set())), "every entry the feed still holds stays seen")

    def test_an_id_that_is_not_a_full_address_reads_as_the_old_bot_saved_it(self):
        """A guid that isn't marked isPermaLink="false" is an address, and a reader that fetched the feed took it from the feed's own address."""
        entries = read_entries(RELATIVE_GUIDS, "https://news.example.com/feed/rss")
        self.assertEqual([entry.key for entry in entries], ["https://news.example.com/feed/post-108", "https://news.example.com/feed/post-107"])
        self.assertEqual(fresh_entries(entries, ["https://news.example.com/feed/post-108"]), [])

    def test_a_feed_that_renames_its_entries_on_every_read_posts_each_once(self):
        """Some feeds write the read time into each ID. With nothing seen in a read, an entry whose link and title went out before is old."""
        def read(stamp: int) -> list[Entry]:
            return [Entry(f"42-{stamp}", "Scheduled maintenance", "https://status.example.com/incidents/42", "")]
        seen = next_seen(read(1), None, set())
        self.assertEqual(fresh_entries(read(2), seen), [])
        seen = next_seen(read(2), seen, set())
        moved = [Entry("43-3", "Printers back online", "https://status.example.com/incidents/43", ""), *read(3)]
        self.assertEqual([entry.key for entry in fresh_entries(moved, seen)], ["43-3"], "a new entry still goes out")
        weekly = [Entry("b", "Weekly update", "https://e.com/", ""), Entry("a", "Weekly update", "https://e.com/", "")]
        self.assertEqual([entry.key for entry in fresh_entries(weekly, next_seen(weekly[1:], None, set()))], ["b"], "while an ID is still seen, a link and title used again count for nothing")

    def test_a_post_fits_one_message_and_keeps_its_link(self):
        text = entry_text(Entry("k", "A title", "https://e.com/k", "<p>" + "word " * 1000 + "</p>"))
        self.assertLessEqual(len(text), MESSAGE_LIMIT)
        self.assertTrue(text.startswith("# A title\n\n"))
        self.assertTrue(text.endswith("...\n\n[Read more](https://e.com/k)"))
        self.assertEqual(entry_text(Entry("k", "Short", "https://e.com/k", "<p>Body</p>")), "# Short\n\nBody\n\n[Read more](https://e.com/k)")

    def test_only_a_refused_post_counts_against_an_entry(self):
        """A webhook made again in a channel that has no room for it isn't the entry's fault."""
        def ops_error(status: int, code: int) -> OpsError:
            error = OpsError("Discord refused it")
            error.__cause__ = discord.HTTPException(FakeHTTPResponse(status), {"code": code, "message": "x"})
            return error
        self.assertTrue(refused_content(ops_error(400, 50035)))
        self.assertFalse(refused_content(ops_error(400, 30007)), "the channel's webhook limit")
        self.assertFalse(refused_content(ops_error(403, 50013)))

    def test_a_long_list_splits_into_messages_without_cutting_a_block(self):
        blocks = [f"**Feed URL**: <https://e.com/{index}>" + "x" * 300 for index in range(20)]
        messages = split_messages("**Currently Configured RSS Feeds**:", blocks)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= MESSAGE_LIMIT for message in messages))
        self.assertEqual(sum(message.count("**Feed URL**") for message in messages), 20)


class PollTest(GatewayTest):
    """RS-7 and RS-10 through the real library: what a round posts, under which name, and what one broken feed costs the rest."""
    modules = [RSSFeeds]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.rss = self.bot.module("rss")
        self.web = FakeOutside()
        self.rss.outside = self.web
        await self.bot.settings.save(self.guild_id, "rss", {"enabled": True})
        self.now = time.time()

    async def feed(self, url: str = URL, *, channel: int | None = None, name: str | None = None, seen: list[str] | None = None) -> int:
        return await self.db.insert(
            "INSERT INTO rss_feeds (guild_id, channel_id, url, name, seen, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (self.guild_id, channel or int(self.text["id"]), url, name, json.dumps(seen) if seen is not None else None, self.now),
        )

    def posts(self) -> list[dict]:
        return [body for method, path, body in self.net.calls if method == "POST" and path == "/webhooks/{webhook_id}/{webhook_token}"]

    def serve(self, url: str, *items: str) -> None:
        self.web.answer(url, rss(*items))

    async def test_a_new_feed_posts_its_newest_entry_and_then_only_what_is_new(self):
        await self.feed()
        self.serve(URL, item("b", "Second", at=self.now - 10), item("a", "First", at=self.now - 20))
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Second"])
        await self.rss.poll()
        self.assertEqual(len(self.posts()), 1, "nothing new, nothing posted")
        self.serve(URL, item("d", "Fourth", at=self.now), item("c", "Third", at=self.now - 5), item("b", "Second", at=self.now - 10), item("a", "First", at=self.now - 20))
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Second", "# Third", "# Fourth"])
        self.assert_no_errors()

    async def test_an_imported_last_entry_posts_nothing_old(self):
        await self.feed(seen=["b"])
        self.serve(URL, item("c", "Third", at=self.now), item("b", "Second", at=self.now - 10), item("a", "First", at=self.now - 20))
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Third"])

    async def test_a_post_goes_under_the_feeds_name_with_the_bots_picture_and_pings_nobody(self):
        await self.feed(name="Printer News")
        await self.feed(OTHER)
        self.serve(URL, item("a", "Hello @everyone", "<p>Visit <a href=\"https://e.com\">us</a></p>"))
        self.serve(OTHER, item("z", "Other"))
        await self.rss.poll()
        named = next(post for post in self.posts() if post["username"] == "Printer News")
        plain = next(post for post in self.posts() if post is not named)
        self.assertEqual(plain["username"], "Captain Guild")
        self.assertEqual(named["avatar_url"], str(self.bot.user.display_avatar.url))
        self.assertEqual(named["allowed_mentions"], {"parse": []})
        self.assertEqual(named["content"], "# Hello @everyone\n\nVisit [us](https://e.com)\n\n[Read more](https://blog.example.com/a)")
        made = [call for call in self.net.calls if call[0] == "POST" and call[1] == "/channels/{channel_id}/webhooks"]
        self.assertEqual(len(made), 1, "one webhook of the bot's own per channel, shared by every feed there")

    async def test_one_address_is_read_once_for_every_feed_that_follows_it(self):
        await self.feed()
        await self.feed(channel=int(self.log_channel["id"]))
        self.serve(URL, item("a"))
        await self.rss.poll()
        self.assertEqual(len(self.web.called(URL)), 1)
        self.assertEqual(len(self.posts()), 2)

    async def test_a_broken_address_or_a_deleted_channel_never_stops_the_other_feeds(self):
        broken = await self.feed("https://down.example.com/feed")
        orphan = await self.feed(OTHER, channel=123456789012345678)
        working = await self.feed()
        self.web.answer("https://down.example.com", OutsideError("The service took too long to answer."))
        self.serve(OTHER, item("x"))
        self.serve(URL, item("a", "Works"))
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Works"])
        self.assertEqual(self.rss._status[broken]["error"], "The service took too long to answer.")
        self.assertEqual(self.rss._status[orphan]["error"], CHANNEL_GONE)
        self.assertIsNone(self.rss._status[working]["error"])
        self.assert_no_errors()

    async def test_a_feed_that_raises_costs_only_itself(self):
        first = await self.feed()
        await self.feed(OTHER)
        self.serve(URL, item("a"))
        self.serve(OTHER, item("b", "Other works"))
        original = self.rss.deliver

        async def deliver(feed, entries):
            if feed["id"] == first:
                raise RuntimeError("a bug")
            return await original(feed, entries)
        self.rss.deliver = deliver
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Other works"])
        self.errors.lines.clear()  # the bug is logged, which is the point

    async def test_a_refused_post_is_tried_again_and_skipped_after_three_tries(self):
        feed_id = await self.feed(seen=["a"])
        self.serve(URL, item("c", "Third", at=self.now), item("b", "Second", at=self.now - 5), item("a", "First", at=self.now - 10))
        original = self.net.webhook
        refused = []

        async def refuse(adapter, route, session=None, **kwargs):
            body = kwargs.get("payload") or {}
            if route.path == "/webhooks/{webhook_id}/{webhook_token}" and "Second" in (body.get("content") or ""):
                refused.append(1)
                raise discord.HTTPException(FakeHTTPResponse(400), {"code": 50035, "message": "Invalid Form Body"})
            return await original(adapter, route, session, **kwargs)
        self.net.webhook = refuse
        for _ in range(TRIES - 1):
            await self.rss.poll()
            self.assertEqual(self.posts(), [], "the entry after it waits, so the order holds")
            self.assertIn("tried again next round", self.rss._status[feed_id]["error"])
        await self.rss.poll()
        self.assertEqual(len(refused), TRIES)
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Third"])
        self.assertIn("Skipped Second", self.rss._status[feed_id]["error"])
        await self.rss.poll()
        self.assertEqual(len(refused), TRIES, "a skipped entry stays skipped")

    async def test_a_missing_permission_holds_the_entry_until_it_is_fixed(self):
        feed_id = await self.feed(seen=["a"])
        self.serve(URL, item("b", "Second", at=self.now), item("a", "First", at=self.now - 10))
        original = self.net.http

        async def refuse(route, **kwargs):
            if route.path == "/channels/{channel_id}/webhooks":
                raise discord.Forbidden(FakeHTTPResponse(403), {"code": 50013, "message": "Missing Permissions"})
            return await original(route, **kwargs)
        self.bot.http.request = refuse
        for _ in range(TRIES + 1):
            await self.rss.poll()
        self.assertIn("Manage Webhooks", self.rss._status[feed_id]["error"])
        self.bot.http.request = original
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Second"], "no number of rounds without the permission skips an entry")

    async def test_a_new_entry_posts_when_an_old_one_is_dated_again_in_the_same_read(self):
        def atom(*entries: tuple[str, str, float]) -> bytes:
            body = "".join(f"<entry><id>tag:example.com,2026:{key}</id><title>{title}</title><link href='https://example.com/{key}'/>"
                           f"<updated>{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(at))}</updated><summary>{title}</summary></entry>" for key, title, at in entries)
            return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Releases</title><id>tag:example.com,2026:feed</id>{body}</feed>'.encode()
        await self.feed(seen=["tag:example.com,2026:v1.1", "tag:example.com,2026:v1.0"])
        self.web.answer(URL, atom(("v1.1", "Version 1.1", self.now), ("v1.2", "Version 1.2", self.now - 30), ("v1.0", "Version 1.0", self.now - 86400 * 30)))
        await self.rss.poll()
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Version 1.2"])

    async def test_a_test_post_of_a_followed_address_holds_nothing_back(self):
        """The address's validators belong to the feeds that follow it. A test read in between must not move them on."""
        await self.feed(seen=["a"])
        state = {"etag": '"v1"', "items": [item("a", "First", at=self.now - 100)]}

        def address(call):
            if (call.get("headers") or {}).get("If-None-Match") == state["etag"]:
                return reply(b"", status=304)
            return reply(rss(*state["items"]), headers={"ETag": state["etag"]})
        self.web.answer(URL, address)
        await self.rss.poll()
        state.update(etag='"v2"', items=[item("b", "Second", at=self.now - 50), item("a", "First", at=self.now - 100)])
        await self.rss.test_post(self.guild.get_channel(int(self.log_channel["id"])), URL)
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Second", "# Second"], "the test, then the feed's own post")
        self.assertEqual([(call.get("headers") or {}).get("If-None-Match") for call in self.web.calls], [None, None, '"v1"'])

    async def test_an_imported_last_entry_with_a_short_id_posts_nothing_old(self):
        address = "https://news.example.com/feed/rss"
        await self.feed(address, seen=["https://news.example.com/feed/post-108"])  # as the old bot saved it
        self.web.answer(address, RELATIVE_GUIDS)
        await self.rss.poll()
        self.assertEqual(self.posts(), [])

    async def test_a_feed_that_renames_its_entry_on_every_read_posts_it_once(self):
        await self.feed(seen=["old"])
        reads = iter(range(1, 100))

        def address(call):
            return reply(('<?xml version="1.0"?><rss version="2.0"><channel><title>Status</title><link>https://status.example.com</link><description>d</description>'
                          f'<item><title>Scheduled maintenance</title><link>https://status.example.com/incidents/42</link><guid isPermaLink="false">42-{next(reads)}</guid>'
                          '<description>Printers may disconnect</description></item></channel></rss>').encode())
        self.web.answer(URL, address)
        for _ in range(5):
            await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Scheduled maintenance"])

    async def test_a_channel_full_of_webhooks_holds_the_entries_until_there_is_room(self):
        feed_id = await self.feed(seen=["a"])
        self.serve(URL, item("b", "Second", at=self.now), item("a", "First", at=self.now - 10))
        original = self.net.http

        async def full(route, **kwargs):
            if route.method == "POST" and route.path == "/channels/{channel_id}/webhooks":
                raise discord.HTTPException(FakeHTTPResponse(400), {"code": 30007, "message": "Maximum number of webhooks reached (15)"})
            return await original(route, **kwargs)
        self.bot.http.request = full
        for _ in range(TRIES + 1):
            await self.rss.poll()
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.rss._status[feed_id]["error"], "New entries wait until the bot has a webhook in that channel: Discord refused the webhook: Maximum number of webhooks reached (15)")
        self.bot.http.request = original  # someone deletes an old webhook
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Second"])

    def three(self) -> None:
        """"a" went out before. "b" and "c" are new."""
        self.serve(URL, item("c", "Third", at=self.now), item("b", "Second", at=self.now - 5), item("a", "First", at=self.now - 10))

    async def test_a_dropped_connection_partway_through_posts_nothing_twice(self):
        feed_id = await self.feed(seen=["a"])
        self.three()
        original = self.net.webhook
        dropped = []

        async def drop(adapter, route, session=None, **kwargs):
            body = kwargs.get("payload") or {}
            if route.path == "/webhooks/{webhook_id}/{webhook_token}" and "Third" in (body.get("content") or "") and not dropped:
                dropped.append(1)
                raise aiohttp.ServerDisconnectedError()  # no HTTP status, so nothing wraps it
            return await original(adapter, route, session, **kwargs)
        self.net.webhook = drop
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Second"])
        self.assertIn("The post for Third didn't go out, and it's tried again next round", self.rss._status[feed_id]["error"])
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Second", "# Third"])
        self.assert_no_errors()

    async def test_a_stop_partway_through_lets_the_post_on_its_way_finish_and_posts_nothing_twice(self):
        feed_id = await self.feed(seen=["a"])
        self.three()
        original = self.net.webhook
        on_its_way, release = asyncio.Event(), asyncio.Event()

        async def slow(adapter, route, session=None, **kwargs):
            body = kwargs.get("payload") or {}
            if route.path == "/webhooks/{webhook_id}/{webhook_token}" and "Third" in (body.get("content") or "") and not on_its_way.is_set():
                on_its_way.set()
                await release.wait()
            return await original(adapter, route, session, **kwargs)
        self.net.webhook = slow
        self.rss._round = asyncio.ensure_future(self.rss._guarded_round())
        await asyncio.wait_for(on_its_way.wait(), 5)
        stopping = asyncio.ensure_future(self.rss.cog_unload())  # what the service's stop runs for every module
        await asyncio.sleep(0)  # the stop cancels the round and waits for the post
        release.set()
        await asyncio.wait_for(stopping, 5)
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Second", "# Third"], "the post on its way went out")
        self.assertEqual(json.loads(await self.db.scalar("SELECT seen FROM rss_feeds WHERE id = ?", (feed_id,)))[:3], ["c", "b", "a"])
        self.net.webhook = original
        await self.rss.poll()  # the first round after the restart
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], ["# Second", "# Third"])

    async def test_an_entry_with_nothing_to_show_is_passed_over(self):
        await self.feed(seen=["a"])
        self.serve(URL, '<item><guid isPermaLink="false">b</guid><title></title><description></description></item>', item("a"))
        await self.rss.poll()
        self.assertEqual(self.posts(), [])
        self.assertEqual(json.loads(await self.db.scalar("SELECT seen FROM rss_feeds"))[0], "b")

    async def test_a_burst_of_new_entries_posts_the_newest_five_and_says_how_many_it_left_out(self):
        feed_id = await self.feed(seen=["old"])
        self.serve(URL, *[item(f"n{index}", f"New {index}", at=self.now + index) for index in range(8)], item("old", at=self.now - 100))
        await self.rss.poll()
        self.assertEqual([post["content"].splitlines()[0] for post in self.posts()], [f"# New {index}" for index in range(3, 8)])
        self.assertIn("3 older new entries were left out", self.rss._status[feed_id]["error"])
        self.assertEqual(POSTS_PER_ROUND, 5)

    async def test_a_server_with_the_pack_off_is_not_read(self):
        await self.feed()
        self.serve(URL, item("a"))
        await self.bot.settings.save(self.guild_id, "rss", {"enabled": False})
        await self.rss.poll()
        self.assertEqual(self.web.calls, [])

    async def test_an_unchanged_feed_is_asked_with_its_validators_and_posts_nothing(self):
        await self.feed()
        self.web.answer(URL, reply(rss(item("a")), headers={"ETag": '"v1"'}))
        await self.rss.poll()
        self.web.answer(URL, reply(b"", status=304))
        await self.rss.poll()
        self.assertEqual(self.web.calls[-1]["headers"], {"If-None-Match": '"v1"'})
        self.assertEqual(len(self.posts()), 1)

    async def test_the_round_runs_every_minute_and_is_scheduled_once(self):
        timer = await self.bot.timers.get_key(POLL, POLL)
        self.assertIsNotNone(timer)
        self.assertLessEqual(timer["due_at"] - time.time(), POLL_SECONDS)
        await self.rss.on_load()
        self.assertEqual(len(await self.bot.timers.list_kind(POLL)), 1)

    async def test_a_deleted_channel_takes_its_feeds_with_it(self):
        await self.feed()
        self.bot._connection.parse_channel_delete({**self.text})
        await self.settle()
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rss_feeds"), 0)


class Hooks:
    """Stands in for bot.webhooks where the channels are the quick fakes."""

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send(self, channel, **kwargs):
        self.sent.append((channel, kwargs))


class CommandTest(BotTest):
    """RS-1 to RS-5 and RS-9: the old commands with their old answers, and their gate."""
    modules = [RSSFeeds]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.rss = self.cog(RSSFeeds)
        self.web = FakeOutside()
        self.rss.outside = self.web
        self.web.answer(URL, rss(item("a", "Newest"), item("b", "Older")))
        self.hooks = Hooks()
        self.bot.webhooks = self.hooks
        self.logs = FakeChannel(self.guild, "rss-log")
        await self.bot.settings.save(self.guild.id, "rss", {"enabled": True, "log_channel": self.logs.id})

    async def add(self, ctx, url=URL, channel=None, name=None):
        await self.rss.add_feed.callback(self.rss, ctx, url, channel or self.channel, custom_username=name)

    def said(self, ctx) -> list[str]:
        return [sent["content"] for sent in ctx.sent] + ctx.fails

    async def test_every_command_is_for_administrators_as_before(self):
        for command in (self.rss.list_feeds, self.rss.test_feed, self.rss.rss_log_channel, self.rss.add_feed, self.rss.remove_feed):
            self.assertEqual(required_permissions(command), ("administrator",), command.name)

    async def test_adding_a_feed_answers_logs_and_refuses_a_second_copy(self):
        ctx = self.context()
        await self.add(ctx, name="Printer News")
        self.assertEqual(self.said(ctx), [f"RSS feed added and will be monitored in {self.channel.mention} with username: Printer News."])
        self.assertEqual(self.logs.sent[-1].content, f"RSS feed added: {URL} with username: Printer News in {self.channel.mention} by <@{self.admin.id}>")
        self.assertEqual(self.logs.sent[-1].kwargs["allowed_mentions"].users, False)
        row = await self.db.one("SELECT * FROM rss_feeds")
        self.assertEqual((row["url"], row["channel_id"], row["name"], row["seen"]), (URL, self.channel.id, "Printer News", None))
        again = self.context()
        await self.add(again)
        self.assertEqual(again.fails, [f"This RSS feed is already being monitored in {self.channel.mention}."])
        other = FakeChannel(self.guild, "elsewhere")
        third = self.context()
        await self.add(third, channel=other)
        self.assertEqual(self.said(third), [f"RSS feed added and will be monitored in {other.mention} using the bot's default username."])
        self.assertIn("with username: Bot default", self.logs.sent[-1].content)

    async def test_an_address_that_is_not_a_public_feed_is_refused(self):
        for url, said in (("ftp://e.com/feed", "Links must start with http"), ("http://127.0.0.1/feed", "private address"), ("https://nothing.example.com/feed", "couldn't be read as a feed")):
            ctx = self.context()
            await self.add(ctx, url=url)
            self.assertIn(said, ctx.fails[0], url)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rss_feeds"), 0)

    async def test_a_name_longer_than_discord_shows_is_refused(self):
        ctx = self.context()
        await self.add(ctx, name="x" * 81)
        self.assertIn("at most 80 characters", ctx.fails[0])

    async def test_a_server_stops_at_its_most_feeds(self):
        for index in range(MAX_FEEDS):
            await self.db.run("INSERT INTO rss_feeds (guild_id, channel_id, url, created_at) VALUES (?, ?, ?, 0)", (self.guild.id, self.channel.id, f"https://e.com/{index}"))
        ctx = self.context()
        await self.add(ctx)
        self.assertIn(f"follows {MAX_FEEDS} feeds", ctx.fails[0])

    async def test_someone_who_cant_post_in_the_channel_cant_point_a_feed_there(self):
        locked = FakeChannel(self.guild, "locked")
        mod = self.mod
        mod_permissions = locked.permissions_for

        def deny(member):
            return mod_permissions(member) if member is not mod else type("P", (), {"view_channel": True, "send_messages": False, "read_message_history": True, "__getattr__": lambda s, n: False})()
        locked.permissions_for = deny
        ctx = self.context(author=mod)
        await self.add(ctx, channel=locked)
        self.assertEqual(ctx.fails, ["You can't post in that channel."])

    async def test_removing_answers_and_logs(self):
        await self.add(self.context())
        ctx = self.context()
        await self.rss.remove_feed.callback(self.rss, ctx, URL, self.channel)
        self.assertEqual(self.said(ctx), [f"RSS feed removed from {self.channel.mention}."])
        self.assertEqual(self.logs.sent[-1].content, f"RSS feed removed: {URL} in {self.channel.mention} by <@{self.admin.id}>")
        missing = self.context()
        await self.rss.remove_feed.callback(self.rss, missing, URL, self.channel)
        self.assertEqual(self.said(missing), [f"This RSS feed is not being monitored in {self.channel.mention}."])

    async def test_the_list_names_each_feed_and_says_when_there_are_none(self):
        empty = self.context()
        await self.rss.list_feeds.callback(self.rss, empty)
        self.assertEqual(self.said(empty), ["No RSS feeds are currently configured."])
        await self.add(self.context(), name="Printer News")
        await self.db.run("INSERT INTO rss_feeds (guild_id, channel_id, url, created_at) VALUES (?, 1, ?, 0)", (self.guild.id, OTHER))
        ctx = self.context()
        await self.rss.list_feeds.callback(self.rss, ctx)
        self.assertEqual(self.said(ctx), [
            f"**Currently Configured RSS Feeds**:\n\n**Feed URL**: <{URL}>\n**Channel**: {self.channel.mention} with username 'Printer News'\n\n"
            f"**Feed URL**: <{OTHER}>\n**Channel**: Unknown Channel using the bot's default username"])

    async def test_a_test_post_sends_the_newest_entry_and_logs_nothing(self):
        ctx = self.context()
        await self.rss.test_feed.callback(self.rss, ctx, URL, self.channel)
        self.assertEqual(self.said(ctx), [f"The latest entry from the feed has been posted to {self.channel.mention}."])
        [(channel, sent)] = self.hooks.sent
        self.assertIs(channel, self.channel)
        self.assertTrue(sent["content"].startswith("# Newest"))
        self.assertEqual(self.logs.sent, [])
        self.web.answer("https://empty.example.com", rss())
        empty = self.context()
        await self.rss.test_feed.callback(self.rss, empty, "https://empty.example.com/feed", self.channel)
        self.assertEqual(self.said(empty), ["No entries found in the RSS feed."])

    async def test_the_log_channel_is_set_and_a_missing_one_is_skipped(self):
        ctx = self.context()
        other = FakeChannel(self.guild, "new-log")
        await self.rss.rss_log_channel.callback(self.rss, ctx, other)
        self.assertEqual(self.said(ctx), [f"Logging channel has been set to {other.mention}."])
        self.assertEqual(self.settings("rss")["log_channel"], other.id)
        await other.delete()
        await self.add(self.context())
        self.assertEqual(other.sent, [])


class PageTest(BotTest):
    """The dashboard page: the same checks as the commands, and the settings form."""
    modules = [RSSFeeds]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.rss = self.cog(RSSFeeds)
        self.web = FakeOutside()
        self.rss.outside = self.web
        self.web.answer(URL, rss(item("a", "Newest")))
        self.hooks = Hooks()
        self.bot.webhooks = self.hooks
        self.bot.ops = DiscordOps(self.bot)  # the dashboard checks channels against the fake server, not the demo's

    async def test_add_list_test_and_remove(self):
        client = await self.client()
        answer = await client.post(self.path("/rss"), json={"url": URL, "channel_id": str(self.channel.id), "name": "Blog"})
        self.assertEqual(answer.status, 200, await answer.text())
        listed = await (await client.get(self.path("/rss"))).json()
        [row] = listed["feeds"]
        self.assertEqual((row["url"], row["name"], row["channel_id"]), (URL, "Blog", str(self.channel.id)))
        self.assertEqual((await client.post(self.path("/rss"), json={"url": URL, "channel_id": str(self.channel.id)})).status, 400)
        tested = await client.post(self.path("/rss/test"), json={"url": URL, "channel_id": str(self.channel.id)})
        self.assertEqual(tested.status, 200)
        self.assertEqual(len(self.hooks.sent), 1)
        gone = await client.delete(self.path(f"/rss/{row['id']}"))
        self.assertEqual(gone.status, 200)
        self.assertEqual((await (await client.get(self.path("/rss"))).json())["feeds"], [])
        actions = [entry["action"] for entry in await self.bot.activity_log.recent(self.guild.id)]
        self.assertIn("rss.add", actions)
        self.assertIn("rss.remove", actions)

    async def test_a_bad_address_is_refused_on_its_field(self):
        client = await self.client()
        answer = await client.post(self.path("/rss"), json={"url": "http://10.0.0.1/feed", "channel_id": str(self.channel.id)})
        self.assertEqual(answer.status, 400)
        self.assertIn("url", (await answer.json())["fields"])

    async def test_another_servers_feed_cant_be_removed(self):
        feed_id = await self.db.insert("INSERT INTO rss_feeds (guild_id, channel_id, url, created_at) VALUES (999, 1, ?, 0)", (URL,))
        client = await self.client()
        self.assertEqual((await client.delete(self.path(f"/rss/{feed_id}"))).status, 404)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rss_feeds"), 1)

    async def test_the_log_channel_setting_names_a_channel_the_person_can_post_in(self):
        self.assertEqual(self.rss.posted_channels({"log_channel": 5, "enabled": True}), {"log_channel": {5}})
        self.assertEqual(self.rss.posted_channels({"log_channel": None, "enabled": True}), {"log_channel": set()})


class ForgetTest(BotTest):
    """A removed feed leaves nothing behind in memory: its status, its lock, its tries, and its address's validators once no feed reads it."""
    modules = [RSSFeeds]

    async def test_removing_the_last_feed_of_an_address_forgets_it(self):
        cog = self.cog(RSSFeeds)
        cog.outside = FakeOutside().answer(URL, rss(item("a")))
        cog.bot.webhooks = Hooks()
        first = await self.db.insert("INSERT INTO rss_feeds (guild_id, channel_id, url, created_at) VALUES (?, ?, ?, 0)", (self.guild.id, self.channel.id, URL))
        second = await self.db.insert("INSERT INTO rss_feeds (guild_id, channel_id, url, created_at) VALUES (999, 1, ?, 0)", (URL,))
        cog._validators[URL] = {"If-None-Match": '"v1"'}
        cog._feed_locks[first] = object()
        cog._tries[(first, "k")] = 1
        await cog.remove(self.guild.id, {"id": first, "url": URL, "channel_id": self.channel.id}, self.bot.actor(self.admin))
        self.assertNotIn(first, cog._feed_locks)
        self.assertNotIn((first, "k"), cog._tries)
        self.assertIn(URL, cog._validators, "another server still reads it")
        await cog.remove(999, {"id": second, "url": URL, "channel_id": 1}, self.bot.actor(self.admin))
        self.assertNotIn(URL, cog._validators)
