"""Reddit posts: reading an account's feed, laying a post out as Discord messages, sending it with its pictures, and the courier's deliveries."""
from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import time
import unittest

import aiohttp
import discord

from captainguild.core import reddit as reader
from captainguild.core.module import required_permissions
from captainguild.core.ops import DiscordOps
from captainguild.core.outside import OutsideError
from captainguild.core.settings import Actor
from captainguild.core.validate import Invalid
from captainguild.modules.reddit import (CHANNEL_GONE, LOOKUP_TRIES, MAX_AGE, MAX_FEEDS, NO_ACCOUNT, NOT_PINGED, POLL, POLL_SECONDS, SAME_TITLE, SLOWED, TOO_OLD, FetchError, RedditPosts,
                                         closing_line, stored_roles)
from tests.fake_outside import FakeOutside, reply
from tests.fakes import BotTest, FakeChannel, FakeMember
from tests.gateway import FakeHTTPResponse, GatewayTest, iso, new_id

ACCOUNT = "ExampleMaker"
SUB = "example"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64
PICTURE = {"Content-Type": "image/png"}
PACED = {"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "37", "x-ratelimit-used": "1"}


def link_of(key: str, sub: str = SUB) -> str:
    return f"https://www.reddit.com/r/{sub}/comments/{key}/a_post/"


def entry(key: str, title: str, body: str = "", *, target: str | None = None, at: float = 0.0, sub: str = SUB, author: str = ACCOUNT, kind: str = "t3", link: str | None = None) -> str:
    """One entry as Reddit writes it: the body sits in a table with the post's thumbnail, and the address the post points at is the link that reads [link]."""
    link = link or link_of(key, sub)
    md = f'<!-- SC_OFF --><div class="md">{body}</div><!-- SC_ON -->' if body else ""
    frame = (f'<table> <tr><td> <a href="{link}"> <img src="https://preview.redd.it/thumb.png?width=640&amp;s=abc" alt="x" /> </a> </td><td> {md} &#32; submitted by &#32; '
             f'<a href="https://www.reddit.com/user/{author}"> /u/{author} </a> &#32; to &#32; <a href="https://www.reddit.com/r/{sub}/"> r/{sub} </a> <br/> '
             f'<span><a href="{html.escape(target or link)}">[link]</a></span> &#32; <span><a href="{link}">[comments]</a></span> </td></tr></table>')
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(at))
    return (f'<entry><author><name>/u/{author}</name><uri>https://www.reddit.com/user/{author}</uri></author><category term="{sub}" label="r/{sub}"/>'
            f'<content type="html">{html.escape(frame)}</content><id>{kind}_{key}</id><link href="{link}" /><updated>{stamp}</updated><published>{stamp}</published>'
            f'<title>{html.escape(title)}</title></entry>')


def feed(*entries: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"><category term="u_x" label="u/x"/><updated>2026-11-13T15:33:23+00:00</updated>'
            '<id>/user/x/submitted/.rss</id><title>submitted by x</title>' + "".join(entries) + "</feed>").encode()


def picture_line(name: str) -> str:
    return f'<p><a href="https://i.redd.it/{name}">https://i.redd.it/{name}</a></p>'


def a_post(body: str = "", *, title: str = "A title", target: str = "", key: str = "t3_1abcde", **fields) -> reader.Post:
    return reader.Post(key=key, title=title, link=link_of(key[3:]), html=f'<div class="md">{body}</div>' if body else "", target=target or link_of(key[3:]), **fields)


def texts(body: str) -> list[str]:
    return [block.text for block in reader.blocks(f'<div class="md">{body}</div>') if isinstance(block, reader.Text)]


def delivery(payload: dict, size: int = 3900) -> list[dict]:
    """A post as a courier sends it: the payload as base64url, cut into parts, each part an embed with the delivery's title."""
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()).decode().rstrip("=")
    chunks = [encoded[start:start + size] for start in range(0, len(encoded), size)]
    return [{"title": f"cg-reddit 1 {payload['id']} {number}/{len(chunks)} {reader.checksum(encoded)}", "description": chunk} for number, chunk in enumerate(chunks, 1)]


def payload_for(key: str = "1abcde", *, title: str = "From the courier", body: str = "<p>Delivered.</p>", at: float = 0.0, **more) -> dict:
    return {"v": 1, "id": f"t3_{key}", "author": ACCOUNT, "community": SUB, "title": title, "link": link_of(key), "url": link_of(key), "at": int(at),
            "html": f'<div class="md">{body}</div>', "text": "", "nsfw": False, "spoiler": False, **more}


class NamesTest(unittest.TestCase):
    def test_an_account_reads_from_its_name_or_its_address(self):
        for typed in (ACCOUNT, f"u/{ACCOUNT}", f"/u/{ACCOUNT}", f"user/{ACCOUNT}/", f"https://www.reddit.com/user/{ACCOUNT}/", f"https://old.reddit.com/u/{ACCOUNT}/submitted/?sort=new", f"reddit.com/user/{ACCOUNT}"):
            self.assertEqual(reader.account_name(typed), ACCOUNT, typed)
        for typed in ("", "ab", "has space", f"https://example.com/user/{ACCOUNT}", "x" * 21, None, 5):
            with self.assertRaises(Invalid, msg=typed):
                reader.account_name(typed)
        self.assertEqual(reader.feed_address(ACCOUNT), f"https://www.reddit.com/user/{ACCOUNT}/submitted/.rss?sort=new")

    def test_a_posts_link_reads_from_the_forms_people_copy(self):
        full = "https://www.reddit.com/r/example/comments/1abcde/a_post/"
        self.assertEqual(reader.post_link(full), full)
        self.assertEqual(reader.post_link("https://old.reddit.com/r/example/comments/1abcde/a_post/?utm_source=share#x"), full)
        self.assertEqual(reader.post_link("www.reddit.com/r/example/comments/1abcde/a_post/k9xyz/"), full, "a comment's link gives its post")
        self.assertEqual(reader.post_link("https://www.reddit.com/comments/1abcde"), "https://www.reddit.com/comments/1abcde/")
        self.assertEqual(reader.post_link("https://redd.it/1abcde"), "https://www.reddit.com/comments/1abcde/")
        for typed in ("", "https://www.reddit.com/r/example/s/AbCdEf", "https://example.com/comments/1abcde/", "ftp://www.reddit.com/comments/1abcde/", "not a link", None):
            with self.assertRaises(Invalid, msg=typed):
                reader.post_link(typed)
        self.assertEqual(reader.post_key(full), "t3_1abcde")
        self.assertIsNone(reader.post_key("https://www.reddit.com/user/x/"))
        self.assertEqual(reader.post_feed_address(full), "https://www.reddit.com/r/example/comments/1abcde/a_post/.rss")
        self.assertEqual(reader.embed_address(full), "https://embed.reddit.com/r/example/comments/1abcde/a_post/?embed=true")

    def test_an_address_loses_only_its_utm_tags(self):
        self.assertEqual(reader.clean_address("https://example.com/p?utm_campaign=x&utm_medium=post&utm_source=reddit"), "https://example.com/p")
        signed = "https://preview.redd.it/abc.png?width=2160&format=png&auto=webp&s=0123abcd"
        self.assertEqual(reader.clean_address(signed), signed)
        self.assertEqual(reader.clean_address("https://example.com/p?a=1%2F2&UTM_source=x&b=two+words#part"), "https://example.com/p?a=1%2F2&b=two+words#part")
        self.assertEqual(reader.clean_address("/r/example/wiki"), "https://www.reddit.com/r/example/wiki")
        self.assertEqual(reader.clean_address("//example.com/x"), "https://example.com/x")
        for other in ("mailto:a@example.com", "javascript:alert(1)", "", None, "https://"):
            self.assertEqual(reader.clean_address(other), "", other)
        self.assertEqual(reader.clean_address("https://example.com/a b>c"), "https://example.com/a%20b%3Ec")

    def test_a_picture_reddit_hosts_is_fetched_whole_first_and_its_preview_second(self):
        whole = reader.picture("https://i.redd.it/abc123.gif")
        self.assertEqual((whole.kind, whole.urls, whole.link, whole.name), ("image", ["https://i.redd.it/abc123.gif"], "https://i.redd.it/abc123.gif", "abc123.gif"))
        preview = "https://preview.redd.it/def456.png?width=2160&format=png&auto=webp&s=0123abcd"
        self.assertEqual(reader.picture(preview).urls, ["https://i.redd.it/def456.png", preview])
        self.assertEqual(reader.picture("https://preview.redd.it/a-title-in-words-v0-ghi789.jpeg?width=640&s=1").urls[0], "https://i.redd.it/ghi789.jpeg")
        for other in ("https://example.com/a.png", "https://i.redd.it/", "https://i.redd.it/page.html", "https://v.redd.it/abc123", None):
            self.assertIsNone(reader.picture(other), other)


class ReadTest(unittest.TestCase):
    def test_a_feed_reads_as_posts_newest_first_with_what_each_points_at(self):
        body = feed(
            entry("1aaaaa", "Older text post", "<p>Words</p>", at=1000),
            entry("1bbbbb", "A picture & more", "<p>See <a href=\"https://example.com/?a=1&amp;b=2\">this</a></p>", target="https://i.redd.it/pic123.png", at=3000),
            entry("1ccccc", "A video", target="https://v.redd.it/vid123", at=2000, sub="other"),
        )
        posts = reader.read_posts(body)
        self.assertEqual([post.key for post in posts], ["t3_1bbbbb", "t3_1ccccc", "t3_1aaaaa"])
        newest = posts[0]
        self.assertEqual((newest.title, newest.link, newest.target, newest.at, newest.author, newest.community),
                         ("A picture & more", link_of("1bbbbb"), "https://i.redd.it/pic123.png", 3000.0, ACCOUNT, SUB))
        self.assertEqual(newest.html, '<div class="md"><p>See <a href="https://example.com/?a=1&amp;b=2">this</a></p></div>')
        self.assertEqual([reader.kind(post) for post in posts], ["picture", "video", "text"])
        self.assertEqual((posts[1].html, posts[1].community), ("", "other"))

    def test_a_posts_own_feed_gives_the_post_and_not_its_comments(self):
        comment = entry("1aaaaa", "someone on A post", "<p>Nice</p>", kind="t1", link=link_of("1aaaaa") + "k9xyz/")
        posts = reader.read_posts(feed(entry("1aaaaa", "A post", "<p>Words</p>", at=1000), comment.replace("<id>t1_1aaaaa</id>", "<id>t1_k9xyz</id>")))
        self.assertEqual([(post.key, post.title) for post in posts], [("t3_1aaaaa", "A post")])

    def test_a_page_that_is_not_a_feed_reads_as_none_and_an_empty_feed_as_no_posts(self):
        self.assertIsNone(reader.read_posts(b"<html><body>Blocked</body></html>"))
        self.assertEqual(reader.read_posts(feed()), [])

    def test_a_link_in_the_body_that_reads_link_is_not_taken_for_what_the_post_points_at(self):
        [post] = reader.read_posts(feed(entry("1aaaaa", "T", '<p><a href="https://evil.example/">[link]</a></p>', target="https://i.redd.it/real123.png", at=5)))
        self.assertEqual(post.target, "https://i.redd.it/real123.png")

    def test_what_a_post_is_follows_what_it_points_at(self):
        self.assertEqual(reader.kind(a_post(target="https://www.reddit.com/gallery/1abcde")), "gallery")
        self.assertEqual(reader.kind(a_post(target="https://blog.example.com/a-post/")), "link")
        self.assertEqual(reader.kind(a_post(target=link_of("1zzzzz"))), "link", "a crosspost points at another post")
        self.assertEqual(reader.kind(a_post()), "text")
        self.assertEqual(reader.kind(a_post(gallery=["https://i.redd.it/a1.jpeg"])), "gallery")

    def test_fields_from_outside_are_checked_before_they_make_a_post(self):
        good = {"key": "t3_1abcde", "title": "  A   title ", "link": "https://old.reddit.com/r/example/comments/1abcde/a_post", "html": "<p>x</p>", "target": "javascript:alert(1)", "at": 5,
                "author": ACCOUNT, "community": "example", "gallery": ["https://i.redd.it/a1.jpeg", "https://evil.example/b.jpeg", 7], "video": {"url": "https://v.redd.it/vid123/DASH_720.mp4"}}
        post = reader.checked_post(good)
        self.assertEqual((post.title, post.link, post.target, post.at, post.gallery, post.video), ("A title", link_of("1abcde"), "", 5.0, ["https://i.redd.it/a1.jpeg"], {"url": "https://v.redd.it/vid123/DASH_720.mp4"}))
        for change in ({"key": "t1_1abcde"}, {"title": ""}, {"link": "https://example.com/comments/1abcde/"}, {"link": link_of("1zzzzz")}, {"title": 5}):
            self.assertIsNone(reader.checked_post({**good, **change}), change)
        self.assertIsNone(reader.checked_post("a string"))
        self.assertEqual(reader.checked_post({**good, "at": True, "author": "bad name!", "video": {"url": "https://evil.example/v.mp4"}}).video, None)
        self.assertEqual(reader.Post.from_json(post.to_json()), post)
        escaped = reader.checked_post({**good, "html": "&lt;!-- SC_OFF --&gt;&lt;div class=\"md\"&gt;&lt;p&gt;Fish &amp;amp; chips &amp;lt;3&lt;/p&gt;&lt;/div&gt;"})
        self.assertEqual([block.text for block in reader.blocks(escaped.html)], ["Fish & chips \\<3"], "a body handed over with its tags escaped reads the same")
        self.assertEqual(reader.checked_post({**good, "html": "<p>1 &lt; 2</p>"}).html, "<p>1 &lt; 2</p>", "a body that already has tags is left as it is")
        self.assertIsNone(reader.Post.from_json("{not json"))
        self.assertIsNone(reader.Post.from_json(None))


class MarkdownTest(unittest.TestCase):
    def test_a_body_keeps_its_paragraphs_emphasis_headings_lists_quotes_code_and_tables(self):
        body = ("<p>Hello <strong>bold</strong> and <em>it</em> and <del>gone</del>.</p> <h1>Big</h1> <h2>Small</h2> <h3>Tiny</h3> "
                "<ul> <li>One</li> <li>Two <ul><li>Inner</li></ul></li> </ul> <ol><li>First</li><li><p>Second</p><p>More</p></li></ol> "
                "<blockquote><p>Quoted</p><p>Again</p></blockquote> <pre><code>x = 1\n  y</code></pre> <p>Code <code>a_b</code> here</p> <hr/>"
                "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>")
        self.assertEqual(texts(body), [
            "Hello **bold** and *it* and ~~gone~~.", "## Big", "### Small", "**Tiny**", "- One\n- Two\n  - Inner", "1. First\n2. Second\n  More",
            "> Quoted\n> Again", "```\nx = 1\n  y\n```", "Code `a_b` here", "**A** | **B**\n1 | 2",
        ])

    def test_a_link_keeps_its_words_and_goes_out_wrapped_without_its_utm_tags(self):
        self.assertEqual(texts('<p>Read <a href="https://example.com/page?utm_source=reddit&amp;id=5&amp;utm_medium=post">the <strong>post</strong></a>.</p>'),
                         ["Read [the **post**](<https://example.com/page?id=5>)."])
        self.assertEqual(texts('<p><a href="https://example.com/x">https://example.com/x</a></p>'), ["<https://example.com/x>"])
        self.assertEqual(texts('<p><a href="/r/example/wiki">the wiki</a> and <a href="mailto:a@example.com">mail us</a></p>'), ["[the wiki](<https://www.reddit.com/r/example/wiki>) and mail us"])

    def test_words_shaped_like_a_site_never_mask_another_site(self):
        self.assertEqual(texts('<p><a href="https://shop.example.com/h3c?x=1">shop.example.com/h3c</a></p>'), ["<https://shop.example.com/h3c?x=1>"])
        self.assertEqual(texts('<p><a href="https://evil.example.net/">shop.example.com</a></p>'), ["shop.example.com (<https://evil.example.net/>)"])

    def test_words_never_read_as_markdown_or_as_a_mention(self):
        self.assertEqual(texts("<p>2 * 3, snake_case, &lt;@&amp;123&gt; @everyone [x](y) ~~ `tick` a|b \\</p>"), ["2 \\* 3, snake\\_case, \\<@&123> @everyone \\[x\\](y) \\~\\~ \\`tick\\` a\\|b \\\\"])
        self.assertEqual(texts("<p># not a heading</p><p>- not a list</p><p>&gt; not a quote</p><p>-# not small</p>"), ["\\# not a heading", "\\- not a list", "\\> not a quote", "\\-# not small"])
        self.assertEqual(texts("<p>See https://example.com/a_b. Then (https://example.com/c)</p>"), ["See <https://example.com/a_b>. Then (<https://example.com/c>)"])

    def test_spaces_inside_emphasis_move_outside_it(self):
        self.assertEqual(texts("<p>one<strong> two </strong>three <em></em>four<strong><strong>five</strong></strong></p>"), ["one **two** three four**five**"])
        self.assertEqual(texts("<p><strong><em>both</em></strong> and <em><strong>both again</strong></em> and <em><em>once</em></em></p>"), ["***both*** and ***both again*** and *once*"])
        self.assertEqual(texts('<p>A <span class="md-spoiler-text">secret</span> and a line<br/>break</p>'), ["A ||secret|| and a line\nbreak"])
        self.assertEqual(texts('<blockquote class="md-spoiler-text"><p>Hidden</p></blockquote>'), ["||Hidden||"])

    def test_a_paragraph_that_is_only_a_picture_is_that_picture_in_its_place(self):
        signed = "https://preview.redd.it/def456.png?width=2160&amp;format=png&amp;auto=webp&amp;s=0123abcd"
        found = reader.blocks(f'<div class="md"><p>Before</p>{picture_line("abc123.gif")}<p>Between</p><p><a href="{signed}">{signed}</a></p><p>After <a href="https://i.redd.it/inline1.png">a link</a></p></div>')
        self.assertEqual([type(block).__name__ for block in found], ["Text", "Media", "Text", "Media", "Text"])
        self.assertEqual((found[1].urls, found[1].name), (["https://i.redd.it/abc123.gif"], "abc123.gif"))
        self.assertEqual(found[3].urls, ["https://i.redd.it/def456.png", html.unescape(signed)])
        self.assertEqual(found[4].text, "After [a link](<https://i.redd.it/inline1.png>)", "a picture linked among words stays a link")

    def test_a_figure_from_an_embed_page_is_its_picture_under_its_caption(self):
        figure = ('<figure class="rte-media"><a href="https://preview.redd.it/a-title-v0-abc123.gif?width=800&amp;s=1"><img src="https://preview.redd.it/a-title-v0-abc123.gif?width=800&amp;s=1"/></a>'
                  "<figcaption>The caption</figcaption></figure>")
        found = reader.blocks(f'<div class="md"><p>Before</p>{figure}</div>')
        self.assertEqual([getattr(block, "text", None) or block.urls[0] for block in found], ["Before", "The caption", "https://i.redd.it/abc123.gif"])

    def test_a_body_with_no_html_reads_from_its_markdown(self):
        found = reader.markdown_blocks("# Heading\n\nSome **bold** and a [link](https://example.com/p?utm_source=x) and https://example.com/bare.\n\n![img](abc123 \"x\")\n\n&gt; quoted &amp; <@&5>")
        self.assertEqual([block.text for block in found], ["## Heading", "Some **bold** and a [link](<https://example.com/p>) and <https://example.com/bare>.", "> quoted & \\<@&5>"])


class LayoutTest(unittest.TestCase):
    CLOSING = "cc: <@&5>"

    def test_a_post_that_fits_is_one_message_with_its_picture_and_the_closing_line(self):
        post = a_post('<p>One.</p> <ul> <li><strong>A</strong> first</li> <li>B</li> </ul> <p>See <a href="https://example.com/p?utm_source=reddit">here</a>.</p>', title="Hello there!", target="https://i.redd.it/pic123.png")
        self.assertEqual(reader.layout(post, reader.lead_media(post), self.CLOSING), [{
            "text": "# Hello there!\n\nOne.\n\n- **A** first\n- B\n\nSee [here](<https://example.com/p>).\n\ncc: <@&5>",
            "media": [{"kind": "image", "urls": ["https://i.redd.it/pic123.png"], "link": "https://i.redd.it/pic123.png", "name": "pic123.png", "page": ""}], "last": True,
            "closing": "cc: <@&5>",
        }])
        self.assertEqual(reader.layout(post, [], "")[0]["text"].splitlines()[-1], "See [here](<https://example.com/p>).", "no roles, no closing line")

    def test_a_message_ends_at_each_picture_in_the_body_and_holds_it(self):
        post = a_post(f'{picture_line("first1.gif")}<p>P1</p>{picture_line("second2.png")}<h1>H</h1><p>P2</p>{picture_line("third3.png")}{picture_line("fourth4.png")}<p>End</p>', title="T")
        parts = reader.layout(post, reader.lead_media(post), self.CLOSING)
        self.assertEqual([(part["text"], [item["name"] for item in part["media"]], part["last"]) for part in parts], [
            ("# T", ["first1.gif"], False), ("P1", ["second2.png"], False), ("## H\n\nP2", ["third3.png"], False), ("", ["fourth4.png"], False), ("End\n\ncc: <@&5>", [], True),
        ])

    def test_a_long_post_with_a_picture_shows_the_picture_under_its_title_and_the_words_after(self):
        paragraphs = [f"Paragraph {number} " + "word " * 70 for number in range(12)]
        body = "".join(f"<h1>Section {number}</h1><p>{text}</p>" if number == 5 else f"<p>{text}</p>" for number, text in enumerate(paragraphs))
        post = a_post(body, title="Long", target="https://i.redd.it/pic123.png")
        parts = reader.layout(post, reader.lead_media(post), self.CLOSING)
        self.assertEqual((parts[0]["text"], [item["name"] for item in parts[0]["media"]]), ("# Long", ["pic123.png"]))
        self.assertGreater(len(parts), 3)
        self.assertTrue(all(len(part["text"]) <= reader.MESSAGE_LIMIT for part in parts))
        self.assertTrue(all(not part["media"] for part in parts[1:]))
        self.assertFalse(any(part["text"].rstrip().endswith("## Section 5") for part in parts), "a heading goes with the words under it")
        whole = "\n\n".join(part["text"] for part in parts[1:])
        self.assertEqual(whole, "\n\n".join([*(text.strip() for text in paragraphs[:5]), "## Section 5", *(text.strip() for text in paragraphs[5:]), self.CLOSING]))
        self.assertEqual([part["last"] for part in parts], [False] * (len(parts) - 1) + [True])

    def test_one_paragraph_longer_than_a_message_is_cut_at_a_sentence_with_its_bold_kept(self):
        sentence = "This sentence has exactly some words in it and ends here. "
        body = f"<p>{sentence * 10}<strong>{sentence * 40}</strong>{sentence * 10}</p>"
        post = a_post(body, title="Wall")
        parts = reader.layout(post, [], self.CLOSING)
        pieces = [part["text"] for part in parts]
        self.assertTrue(all(len(piece) <= reader.MESSAGE_LIMIT for piece in pieces))
        self.assertTrue(all(piece.count("**") % 2 == 0 for piece in pieces), "bold open at a cut is closed there and opened again")
        self.assertTrue(all(piece.rstrip("*").endswith((".", "5>")) for piece in pieces), [piece[-30:] for piece in pieces])
        words = " ".join(" ".join(pieces).replace("**", "").split())
        self.assertEqual(words, " ".join(f"# Wall {sentence * 60}{self.CLOSING}".split()))

    def test_a_link_is_never_cut_through(self):
        link = "[" + "words " * 30 + "end](<https://example.com/" + "a" * 300 + ">)"
        pieces = reader.split_long(" ".join(["filler"] * 280) + " " + link + " " + " ".join(["after"] * 200), 2000)
        self.assertEqual(sum(piece.count(link) for piece in pieces), 1)
        self.assertTrue(all(len(piece) <= 2000 for piece in pieces))

    def test_a_line_of_nothing_but_spaces_makes_no_empty_message(self):
        pieces = reader.split_blocks([reader.Text("```\nfirst\n" + " " * 4500 + "\nlast\n```", code=True)])
        self.assertTrue(all(piece.strip("`\n ") for piece in pieces), pieces)
        self.assertTrue(all(len(piece) <= reader.MESSAGE_LIMIT for piece in pieces))

    def test_a_long_code_block_is_fenced_again_in_each_piece(self):
        code = "\n".join(f"line {number} " + "x" * 60 for number in range(80))
        pieces = reader.split_blocks([reader.Text(f"```\n{code}\n```", code=True)])
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(piece.startswith("```\n") and piece.endswith("\n```") and len(piece) <= reader.MESSAGE_LIMIT for piece in pieces))
        self.assertEqual("\n".join(piece[4:-4] for piece in pieces), code)

    def test_a_link_post_ends_with_its_link_bare_so_discord_draws_its_card(self):
        post = a_post("<p>Read on.</p>", title="A deeper dive", target="https://blog.example.com/a-post/?utm_source=reddit&ref=1")
        [part] = reader.layout(post, reader.lead_media(post), self.CLOSING)
        self.assertEqual(part["text"], "# A deeper dive\n\nRead on.\n\nhttps://blog.example.com/a-post/?ref=1\n\ncc: <@&5>")

    def test_a_gallery_goes_ten_pictures_to_a_message(self):
        post = a_post("<p>Photos.</p>", title="Meetup", target="https://www.reddit.com/gallery/1abcde")
        self.assertTrue(reader.needs_gallery(post))
        few = reader.layout(post, reader.lead_media(post, [f"https://i.redd.it/g{number}.jpeg" for number in range(3)]), self.CLOSING)
        self.assertEqual([(part["text"], len(part["media"])) for part in few], [("# Meetup\n\nPhotos.\n\ncc: <@&5>", 3)])
        many = reader.layout(post, reader.lead_media(post, [f"https://i.redd.it/g{number}.jpeg" for number in range(12)]), self.CLOSING)
        self.assertEqual([(part["text"], len(part["media"]), part["last"]) for part in many], [("# Meetup", 10, False), ("", 2, False), ("Photos.\n\ncc: <@&5>", 0, True)])
        none = reader.layout(post, reader.lead_media(post, []), self.CLOSING)
        self.assertEqual(none[0]["text"], f"# Meetup\n\nPhotos.\n\n{post.link}\n\ncc: <@&5>", "with no pictures to be had, the post's own link stands in")
        carried = a_post(title="Meetup", target="https://www.reddit.com/gallery/1abcde", gallery=["https://i.redd.it/own1.jpeg"])
        self.assertFalse(reader.needs_gallery(carried))
        self.assertEqual([item.urls for item in reader.lead_media(carried)], [["https://i.redd.it/own1.jpeg"]])

    def test_a_video_is_attached_from_the_files_its_embed_page_lists(self):
        post = a_post("<p>Watch.</p>", title="Clip", target="https://v.redd.it/vid123", video={"url": "https://v.redd.it/vid123/DASH_720.mp4"})
        [part] = reader.layout(post, reader.lead_media(post), "")
        self.assertEqual(part["media"], [{"kind": "video", "urls": ["https://v.redd.it/vid123/DASH_720.mp4"], "link": post.link, "name": "vid123.mp4",
                                          "page": "https://embed.reddit.com/r/example/comments/1abcde/a_post/?embed=true"}])

    def test_a_title_is_plain_words_and_a_body_in_markdown_is_used_when_there_is_no_html(self):
        post = a_post(title="[Launch] *big* news <@&5>", text="Plain **words**.")
        [part] = reader.layout(post, [], "")
        self.assertEqual(part["text"], "# \\[Launch\\] \\*big\\* news \\<@&5>\n\nPlain **words**.")

    def test_a_stored_plan_that_is_not_a_plan_reads_as_none(self):
        post = a_post("<p>x</p>", target="https://v.redd.it/vid123")
        plan = reader.layout(post, reader.lead_media(post), self.CLOSING)
        self.assertEqual(reader.checked_plan(json.dumps(plan)), plan)
        self.assertEqual((plan[-1]["last"], plan[-1]["closing"]), (True, self.CLOSING), "the last message carries the closing line on its own too")
        for bad in (None, "{", "[]", json.dumps([{"text": 5, "media": []}]), json.dumps([{"text": "x", "media": [], "closing": 5}]),
                    json.dumps([{"text": "x", "media": [{"kind": "exe", "urls": [], "link": "", "name": "", "page": ""}]}]),
                    json.dumps([{"text": "x", "media": [{"kind": "video", "urls": [], "link": "", "name": "", "page": "https://evil.example/"}]}])):
            self.assertIsNone(reader.checked_plan(bad), bad)


def embed_page(*, title: str = "A gallery", target: str = "https://www.reddit.com/gallery/1abcde", inside: str = "") -> str:
    context = html.escape(json.dumps({"post": {"id": "t3_1abcde", "url": target, "nsfw": False, "created_timestamp": 1794395964861, "type": "gallery"}, "subreddit": {"name": "example"}}))
    return (f'<html><body><faceplate-tracker noun="embed" data-faceplate-tracking-context="{context}"><h1 class="line-clamp-3">{title}</h1></faceplate-tracker>'
            f'<a href="https://www.reddit.com/user/{ACCOUNT}/?utm_source=embedv2">u/{ACCOUNT}</a>{inside}'
            '<shreddit-embed-copy-link-button permalink="https://www.reddit.com/r/example/comments/1abcde/a_post/"></shreddit-embed-copy-link-button></body></html>')


GALLERY = ('<gallery-carousel post-id="t3_1abcde"><ul>'
           '<li slot="page-1"><faceplate-img src="https://preview.redd.it/a-gallery-v0-aaa111.jpeg?width=640&amp;s=1"></faceplate-img><img src="https://preview.redd.it/a-gallery-v0-aaa111.jpeg?width=640&amp;crop=smart&amp;s=1"/></li>'
           '<li slot="page-2"><img src="https://preview.redd.it/a-gallery-v0-bbb222.jpeg?width=640&amp;s=2"/></li></ul></gallery-carousel>')


def player(seconds: int = 14) -> str:
    choices = [{"source": {"bitrateBps": rate, "dimensions": {"height": height, "width": height * 16 // 9}, "url": f"https://packaged-media.redd.it/vid123/pb/m2-res_{height}p.mp4?e=1&s=sig{height}"}}
               for rate, height in ((53590, 220), (562128, 720), (1023948, 1080))]
    choices.append({"source": {"bitrateBps": 9, "dimensions": {"height": 2160}, "url": "https://evil.example/huge.mp4"}})
    return f'<shreddit-player packaged-media-json="{html.escape(json.dumps({"playbackMp4s": {"duration": seconds, "permutations": choices}}))}"></shreddit-player>'


class EmbedTest(unittest.TestCase):
    def test_an_embed_page_lists_a_gallerys_pictures_in_order(self):
        found = reader.read_embed(embed_page(inside=GALLERY))
        self.assertEqual(found.gallery, ["https://i.redd.it/aaa111.jpeg", "https://i.redd.it/bbb222.jpeg"])
        self.assertEqual((found.title, found.author, found.community, found.link, found.target, found.at),
                         ("A gallery", ACCOUNT, "example", "https://www.reddit.com/r/example/comments/1abcde/a_post/", "https://www.reddit.com/gallery/1abcde", 1794395964.861))
        self.assertEqual(found.post("t3_1abcde", link_of("1abcde")).gallery, found.gallery)

    def test_an_embed_page_lists_a_videos_files_sharpest_first_and_only_reddits_own(self):
        found = reader.read_embed(embed_page(title="Clip", target="https://v.redd.it/vid123", inside=player()))
        self.assertEqual([(video["height"], video["bytes"]) for video in found.videos], [(1080, 1023948 * 14 // 8), (720, 562128 * 14 // 8), (220, 53590 * 14 // 8)])
        self.assertEqual(reader.video_choices(found.videos, 1_500_000), [found.videos[1]["url"], found.videos[2]["url"]], "the sharpest that should fit goes first")
        self.assertEqual(reader.video_choices(found.videos, 10), [])

    def test_a_text_posts_page_gives_its_title_and_body(self):
        body = '<div class="md mx-0" id="t3_1abcde-post-rtjson-content"><h1>Inside the body</h1><p>Words</p></div>'
        page = embed_page(inside=body).replace('<h1 class="line-clamp-3">A gallery</h1>', "<shreddit-embed-title>The real title</shreddit-embed-title>")
        found = reader.read_embed(page)
        self.assertEqual(found.title, "The real title")
        self.assertEqual([block.text for block in reader.blocks(found.html)], ["## Inside the body", "Words"])
        self.assertEqual(reader.read_embed("<html></html>").title, "")
        self.assertIsNone(reader.read_embed("<html></html>").post("t3_1abcde", link_of("1abcde")), "a page with no post on it gives none")


class CourierTest(unittest.TestCase):
    def test_the_check_is_fnv_1a_as_published(self):
        self.assertEqual([reader.checksum(text) for text in ("", "a", "foobar")], ["811c9dc5", "e40c292c", "bf9cf968"])

    def test_the_couriers_own_fixed_delivery_reads_the_same_here(self):
        """courier/tests/protocol.test.ts asserts that the courier writes this text and this check for this post."""
        encoded = "eyJ2IjoxLCJpZCI6InQzX2FiYzEyMyIsImF1dGhvciI6IkV4YW1wbGUiLCJjb21tdW5pdHkiOiJleGFtcGxlIiwidGl0bGUiOiJIZWxsbyIsImxpbmsiOiJodHRwczovL3d3dy5yZWRkaXQuY29tL3IvZXhhbXBsZS9jb21tZW50cy9hYmMxMjMvaGVsbG8vIiwidXJsIjoiaHR0cHM6Ly93d3cucmVkZGl0LmNvbS9yL2V4YW1wbGUvY29tbWVudHMvYWJjMTIzL2hlbGxvLyIsImF0IjoxNzk0MDAwMDAwLCJodG1sIjoiPGRpdiBjbGFzcz1cIm1kXCI-PHA-SGk8L3A-PC9kaXY-IiwidGV4dCI6IkhpIiwibnNmdyI6ZmFsc2UsInNwb2lsZXIiOmZhbHNlfQ"
        self.assertEqual(reader.checksum(encoded), "7157e974")
        post = reader.assemble([reader.read_piece("cg-reddit 1 t3_abc123 1/1 7157e974", encoded)])
        self.assertEqual((post.key, post.title, post.link, post.at, post.author, post.community, post.html, post.text),
                         ("t3_abc123", "Hello", "https://www.reddit.com/r/example/comments/abc123/hello/", 1794000000.0, "Example", "example", '<div class="md"><p>Hi</p></div>', "Hi"))

    def test_a_delivery_in_one_part_reads_as_its_post(self):
        payload = payload_for(at=1794000000, gallery=["https://i.redd.it/a1.jpeg"], video={"url": "https://v.redd.it/vid123/DASH_720.mp4", "seconds": 14})
        [part] = delivery(payload)
        piece = reader.read_piece(part["title"], part["description"])
        self.assertEqual((piece.key, piece.part, piece.parts), ("t3_1abcde", 1, 1))
        post = reader.assemble([piece])
        self.assertEqual((post.key, post.title, post.link, post.target, post.at, post.author, post.community, post.gallery, post.video),
                         ("t3_1abcde", "From the courier", link_of("1abcde"), link_of("1abcde"), 1794000000.0, ACCOUNT, SUB, ["https://i.redd.it/a1.jpeg"], {"url": "https://v.redd.it/vid123/DASH_720.mp4"}))

    def test_a_long_delivery_is_put_together_from_its_parts_in_any_order(self):
        payload = payload_for(body="<p>" + "Caf\u00e9 \u2603 words " * 1500 + "</p>")
        parts = delivery(payload)
        self.assertGreater(len(parts), 3)
        self.assertTrue(all(len(part["description"]) <= 3900 for part in parts))
        pieces = [reader.read_piece(part["title"], part["description"]) for part in parts]
        self.assertIsNone(reader.assemble(pieces[:-1]), "a part is missing")
        self.assertEqual(reader.assemble(reversed(pieces + pieces[:1])).html, payload["html"])

    def test_parts_that_do_not_add_up_give_nothing(self):
        parts = delivery(payload_for(body="<p>" + "x" * 9000 + "</p>"))
        pieces = [reader.read_piece(part["title"], part["description"]) for part in parts]
        changed = [pieces[0], reader.Piece(pieces[1].key, 2, pieces[1].parts, pieces[1].check, pieces[1].chunk[:-4] + "AAAA"), *pieces[2:]]
        self.assertIsNone(reader.assemble(changed), "the check over the whole delivery fails")
        other = [reader.read_piece(part["title"], part["description"]) for part in delivery(payload_for(body="<p>" + "y" * 9000 + "</p>"))]
        self.assertIsNone(reader.assemble([pieces[0], *other[1:]]), "parts of two deliveries don't mix")
        for payload in ({**payload_for(), "v": 2}, {**payload_for(), "id": "t3_1zzzzz", "link": link_of("1abcde")}, {**payload_for(), "title": ""}):
            [part] = delivery(payload)
            self.assertIsNone(reader.assemble([reader.read_piece(part["title"], part["description"])]), payload)
        self.assertIsNone(reader.assemble([]))

    def test_only_a_deliverys_own_title_reads_as_a_part(self):
        [part] = delivery(payload_for())
        for title in ("", "cg-reddit 2 t3_1abcde 1/1 00000000", "cg-reddit 1 t3_1abcde 2/1 00000000", "cg-reddit 1 t3_1abcde 1/999 00000000", "cg-reddit 1 t1_1abcde 1/1 00000000", "hello", None):
            self.assertIsNone(reader.read_piece(title, part["description"]), title)
        self.assertIsNone(reader.read_piece(part["title"], ""))
        self.assertIsNone(reader.read_piece(part["title"], None))

    def test_the_closing_line_and_stored_roles(self):
        self.assertEqual(closing_line([5, 6]), "cc: <@&5> <@&6>")
        self.assertEqual(closing_line([]), "")
        self.assertEqual(stored_roles("[5, 6]"), [5, 6])
        for bad in (None, "", "{", '{"a": 1}', '["5", true, -1, 2.5]'):
            self.assertEqual(stored_roles(bad), [], bad)


class RelayTest(GatewayTest):
    """Through the real library: what a round reads and sends, what a restart partway through costs, and what a courier delivers."""
    modules = [RedditPosts]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.reddit = self.bot.module("reddit")
        self.web = FakeOutside()
        self.reddit.outside = self.web
        self.now = time.time()
        self.reddit._clock = lambda: self.now
        self.slept: list[float] = []

        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)
            self.now += seconds
        self.reddit._sleep = sleep
        await self.bot.settings.save(self.guild_id, "reddit", {"enabled": True})
        self.web.answer("https://i.redd.it/", reply(PNG, headers=PICTURE))
        self.role = int(self.role_member["id"])

    async def follow(self, account: str = ACCOUNT, *, roles: list[int] | None = None, since: float = 3600.0, channel: dict | None = None) -> int:
        return await self.db.insert(
            "INSERT INTO reddit_feeds (guild_id, account, channel_id, role_ids, created_at) VALUES (?, ?, ?, ?, ?)",
            (self.guild_id, account, int((channel or self.text)["id"]), json.dumps(roles or []), self.now - since),
        )

    def serve(self, *entries: str, account: str = ACCOUNT, headers: dict | None = PACED, status: int = 200) -> None:
        self.web.answer(reader.feed_address(account), reply(feed(*entries), status=status, headers=headers))

    def sent(self) -> list[dict]:
        return [body for method, path, body in self.net.calls if method == "POST" and path == "/channels/{channel_id}/messages"]

    async def round(self, later: float = 61.0) -> None:
        self.now += later
        await self.reddit.run_round()

    async def rows(self) -> list[dict]:
        return await self.db.all("SELECT * FROM reddit_posts ORDER BY id")

    def fail_sends(self, when, error) -> list[int]:
        """Makes a send fail while `when(body)` holds. Returns the list that counts the failures."""
        original = self.bot.http.request
        failed: list[int] = []

        async def request(route, **kwargs):
            if route.method == "POST" and route.path == "/channels/{channel_id}/messages" and when(self.net._body(kwargs)):
                failed.append(1)
                raise error() if callable(error) else error
            return await original(route, **kwargs)
        self.bot.http.request = request
        return failed

    async def test_a_new_post_goes_out_once_in_full_with_its_picture_and_the_roles_line(self):
        await self.follow(roles=[self.role])
        self.serve(entry("1abcde", "Hello there!", "<p>One <strong>two</strong>.</p>", target="https://i.redd.it/pic123.png", at=self.now - 60))
        await self.round()
        [message] = self.sent()
        self.assertEqual(message["content"], f"# Hello there!\n\nOne **two**.\n\ncc: <@&{self.role}>")
        self.assertEqual(message["allowed_mentions"], {"parse": [], "roles": [self.role]}, "the chosen role and nothing else")
        self.assertEqual(self.net.uploads, [(int(self.text["id"]), ["pic123.png"])])
        [row] = await self.rows()
        self.assertEqual((row["state"], row["source"], row["data"], row["plan"], row["note"]), ("sent", "feed", None, None, None))
        self.assertEqual(row["message_id"], int(self.net.posted[0]["id"]))
        await self.round()
        await self.round()
        self.assertEqual(len(self.sent()), 1, "seen again in every read, sent once")
        self.assertEqual(self.web.called("https://i.redd.it/")[0]["hosts"], reader.MEDIA_HOSTS)
        self.assert_no_errors()

    async def test_only_posts_made_after_the_account_was_followed_go_out_and_none_older_than_a_day(self):
        await self.follow(since=30.0)
        self.serve(entry("1aaaaa", "Before the follow", "<p>x</p>", at=self.now - 600), entry("1bbbbb", "After it", "<p>y</p>", at=self.now - 5))
        await self.round()
        self.assertEqual([message["content"].splitlines()[0] for message in self.sent()], ["# After it"])
        await self.db.run("UPDATE reddit_feeds SET created_at = ?", (self.now - 10 * MAX_AGE,))
        self.serve(entry("1ccccc", "Two days old", "<p>z</p>", at=self.now - 2 * MAX_AGE), entry("1bbbbb", "After it", "<p>y</p>", at=self.now - 5))
        await self.round()
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual([row["post_key"] for row in await self.rows()], ["t3_1bbbbb"], "a post that's never relayed leaves no row")

    async def test_someone_elses_post_in_the_feed_is_not_the_accounts(self):
        await self.follow()
        self.serve(entry("1aaaaa", "Not theirs", "<p>x</p>", at=self.now - 5, author="SomeoneElse"))
        await self.round()
        self.assertEqual(self.sent(), [])

    async def test_a_long_post_goes_as_several_messages_and_only_the_last_can_ping(self):
        await self.follow(roles=[self.role])
        body = f'{picture_line("first1.gif")}<p>P1 @everyone</p>{picture_line("second2.png")}<h1>H</h1><p>P2</p>'
        self.serve(entry("1abcde", "Long one", body, at=self.now - 60))
        await self.round()
        messages = self.sent()
        self.assertEqual([message.get("content") or "" for message in messages], ["# Long one", "P1 @everyone", f"## H\n\nP2\n\ncc: <@&{self.role}>"])
        self.assertEqual([message["allowed_mentions"] for message in messages], [{"parse": []}, {"parse": []}, {"parse": [], "roles": [self.role]}])
        self.assertEqual([names for _, names in self.net.uploads], [["first1.gif"], ["second2.png"]])
        self.assertEqual((await self.rows())[0]["state"], "sent")

    async def test_a_dropped_connection_partway_through_carries_on_with_the_next_message(self):
        feed_id = await self.follow()
        self.serve(entry("1abcde", "Long one", f'<p>First</p>{picture_line("one1.png")}<p>Second</p>{picture_line("two2.png")}<p>Third</p>', at=self.now - 60))
        failed = self.fail_sends(lambda body: "Second" in (body.get("content") or "") and not failed, aiohttp.ServerDisconnectedError)
        await self.round()
        self.assertEqual([message["content"] for message in self.sent()], ["# Long one\n\nFirst"])
        [row] = await self.rows()
        self.assertEqual((row["state"], row["done"]), ("waiting", 1))
        self.assertIn("is waiting, and it's tried again in a minute. The connection to Discord dropped", await self.db.scalar("SELECT last_error FROM reddit_feeds WHERE id = ?", (feed_id,)))
        await self.round()
        self.assertEqual([message["content"] for message in self.sent()], ["# Long one\n\nFirst", "Second", "Third"])
        self.assertEqual([names for _, names in self.net.uploads], [["one1.png"], ["two2.png"]])
        self.assertEqual((await self.rows())[0]["state"], "sent")
        self.assertIsNone(await self.db.scalar("SELECT last_error FROM reddit_feeds WHERE id = ?", (feed_id,)))
        self.assert_no_errors()

    async def test_a_message_that_landed_before_the_connection_dropped_is_not_sent_again(self):
        await self.follow()
        self.serve(entry("1abcde", "Long one", f'<p>First</p>{picture_line("one1.png")}<p>Second</p>', at=self.now - 60))
        self.net.keep_history = True
        original = self.bot.http.request
        dropped: list[int] = []

        async def lands_then_drops(route, **kwargs):
            answer = await original(route, **kwargs)
            if route.method == "POST" and route.path == "/channels/{channel_id}/messages" and not dropped:
                dropped.append(1)
                raise aiohttp.ServerDisconnectedError()
            return answer
        self.bot.http.request = lands_then_drops
        await self.round()
        self.assertEqual((await self.rows())[0]["done"], 0)
        await self.round()
        self.assertEqual([message["content"] for message in self.sent()], ["# Long one\n\nFirst", "Second"], "the first message was found in the channel, so it wasn't sent twice")
        self.assertEqual((await self.rows())[0]["state"], "sent")

    async def test_a_message_discord_refuses_is_left_out_and_the_rest_goes(self):
        feed_id = await self.follow(roles=[self.role])
        self.serve(entry("1abcde", "Long one", f'<p>First</p>{picture_line("one1.png")}<p>Refused words</p>{picture_line("two2.png")}<p>Third</p>', at=self.now - 60))
        failed = self.fail_sends(lambda body: "Refused" in (body.get("content") or ""), lambda: discord.HTTPException(FakeHTTPResponse(400), {"code": 200000, "message": "Message was blocked by AutoMod"}))
        await self.round()
        self.assertEqual([message["content"] for message in self.sent()], ["# Long one\n\nFirst", f"Third\n\ncc: <@&{self.role}>"])
        self.assertEqual(len(failed), 1)
        [row] = await self.rows()
        self.assertEqual(row["state"], "sent")
        self.assertIn("Discord refused one message of this post, and the rest went out", row["note"])
        self.assertEqual(await self.db.scalar("SELECT last_error FROM reddit_feeds WHERE id = ?", (feed_id,)), row["note"])

    async def test_a_missing_permission_holds_the_post_and_the_ones_after_it_until_it_is_fixed(self):
        await self.follow()
        self.serve(entry("1bbbbb", "Second post", "<p>y</p>", at=self.now - 30), entry("1aaaaa", "First post", "<p>x</p>", at=self.now - 60))
        original = self.bot.http.request
        self.fail_sends(lambda body: True, lambda: discord.Forbidden(FakeHTTPResponse(403), {"code": 50013, "message": "Missing Permissions"}))
        for _ in range(4):
            await self.round()
        self.assertEqual(self.sent(), [])
        self.assertEqual([row["state"] for row in await self.rows()], ["waiting", "waiting"])
        self.assertIn("Discord answered: Missing Permissions", await self.db.scalar("SELECT last_error FROM reddit_feeds"))
        self.bot.http.request = original
        await self.round()
        self.assertEqual([message["content"].splitlines()[0] for message in self.sent()], ["# First post", "# Second post"], "oldest first, however long they waited")

    async def test_a_post_that_waited_more_than_a_day_is_passed_over(self):
        await self.follow()
        self.serve(entry("1aaaaa", "Stale", "<p>x</p>", at=self.now - 60))
        original = self.bot.http.request
        self.fail_sends(lambda body: True, lambda: discord.Forbidden(FakeHTTPResponse(403), {"code": 50013, "message": "Missing Permissions"}))
        await self.round()
        self.bot.http.request = original
        await self.round(later=MAX_AGE + 120)
        self.assertEqual(self.sent(), [])
        [row] = await self.rows()
        self.assertEqual((row["state"], row["note"], row["data"]), ("skipped", TOO_OLD, None))

    async def test_the_same_title_twice_in_a_day_goes_out_once(self):
        await self.follow()
        self.serve(entry("1bbbbb", "Testers wanted!", "<p>In community two</p>", at=self.now - 49, sub="two"), entry("1aaaaa", "Testers  wanted!", "<p>In community one</p>", at=self.now - 60, sub="one"))
        await self.round()
        self.assertEqual([message["content"] for message in self.sent()], ["# Testers wanted!\n\nIn community one"])
        self.assertEqual([(row["post_key"], row["state"], row["note"]) for row in await self.rows()], [("t3_1aaaaa", "sent", None), ("t3_1bbbbb", "skipped", SAME_TITLE)])

    async def test_two_accounts_posting_one_announcement_to_one_channel_send_it_once(self):
        await self.follow()
        await self.follow("OtherMaker")
        elsewhere = await self.follow("ThirdMaker", channel=self.log_channel)
        self.serve(entry("1aaaaa", "Holiday notice", "<p>From the first account</p>", at=self.now - 60))
        self.serve(entry("1bbbbb", "Holiday  NOTICE", "<p>From the second account</p>", at=self.now - 50, author="OtherMaker"), account="OtherMaker")
        self.serve(entry("1ccccc", "Holiday notice", "<p>For another channel</p>", at=self.now - 40, author="ThirdMaker"), account="ThirdMaker")
        for _ in range(3):
            await self.round()
        self.assertEqual(sorted(message["content"] for message in self.sent()), ["# Holiday notice\n\nFor another channel", "# Holiday notice\n\nFrom the first account"])
        rows = {row["post_key"]: (row["state"], row["note"], row["feed_id"]) for row in await self.rows()}
        self.assertEqual(rows["t3_1bbbbb"][:2], ("skipped", SAME_TITLE))
        self.assertEqual(rows["t3_1ccccc"], ("sent", None, elsewhere), "the same title in another channel is that channel's own")

    async def test_a_picture_that_cannot_be_had_goes_out_as_its_link(self):
        await self.follow()
        self.web.answer("https://i.redd.it/gone1.png", reply(b"", status=404))
        self.web.answer("https://i.redd.it/page1.png", reply(b"<html>blocked</html>", headers={"Content-Type": "text/html"}))
        self.serve(entry("1abcde", "Two missing", f'<p>Words</p>{picture_line("gone1.png")}<p>More</p>{picture_line("page1.png")}', at=self.now - 60))
        await self.round()
        self.assertEqual([message["content"] for message in self.sent()], ["# Two missing\n\nWords\nhttps://i.redd.it/gone1.png", "More\nhttps://i.redd.it/page1.png"])
        self.assertEqual(self.net.uploads, [])

    async def test_a_preview_is_fetched_when_the_whole_picture_is_refused(self):
        await self.follow()
        signed = "https://preview.redd.it/def456.png?width=2160&amp;format=png&amp;auto=webp&amp;s=0123abcd"
        self.web.answer("https://i.redd.it/def456.png", reply(b"", status=403))
        self.web.answer("https://preview.redd.it/def456.png", reply(PNG, headers=PICTURE))
        self.serve(entry("1abcde", "Signed", f'<p>Words</p><p><a href="{signed}">{signed}</a></p>', at=self.now - 60))
        await self.round()
        self.assertEqual(self.net.uploads, [(int(self.text["id"]), ["def456.png"])])
        self.assertEqual(self.web.called("https://preview.redd.it/")[0]["url"], html.unescape(signed))

    async def test_files_discord_calls_too_large_go_out_as_links(self):
        await self.follow()
        self.serve(entry("1abcde", "Big", "<p>Words</p>", target="https://i.redd.it/big123.png", at=self.now - 60))
        original = self.bot.http.request

        async def too_large(route, *, files=None, **kwargs):
            if route.method == "POST" and route.path == "/channels/{channel_id}/messages" and files:
                raise discord.HTTPException(FakeHTTPResponse(413), {"code": 40005, "message": "Request entity too large"})
            return await original(route, files=files, **kwargs)
        self.bot.http.request = too_large
        await self.round()
        self.assertEqual([message["content"] for message in self.sent()], ["# Big\n\nWords\nhttps://i.redd.it/big123.png"])

    async def test_files_past_what_one_message_takes_move_to_the_next(self):
        await self.follow()
        big = b"\x89PNG" + b"0" * (9 * 1024 * 1024)
        self.web.answer("https://i.redd.it/", reply(big, headers=PICTURE))
        self.web.answer("https://embed.reddit.com/", embed_page(inside=GALLERY.replace("</ul>", '<li slot="page-3"><img src="https://preview.redd.it/x-v0-ccc333.jpeg?s=3"/></li></ul>')))
        self.serve(entry("1abcde", "Meetup", "<p>Photos.</p>", target="https://www.reddit.com/gallery/1abcde", at=self.now - 60))
        await self.round()
        self.assertEqual([names for _, names in self.net.uploads], [["aaa111.jpeg", "bbb222.jpeg"], ["ccc333.jpeg"]])
        self.assertEqual([message.get("content") or "" for message in self.sent()], ["# Meetup\n\nPhotos.", ""])

    async def test_when_the_files_spill_into_a_second_message_the_closing_line_and_its_ping_go_last(self):
        await self.follow(roles=[self.role])
        big = b"\x89PNG" + b"0" * (9 * 1024 * 1024)
        self.web.answer("https://i.redd.it/", reply(big, headers=PICTURE))
        more = "".join(f'<li slot="page-{number}"><img src="https://preview.redd.it/x-v0-more{number}.jpeg?s={number}"/></li>' for number in (3, 4, 5))
        self.web.answer("https://embed.reddit.com/", embed_page(inside=GALLERY.replace("</ul>", more + "</ul>")))
        self.serve(entry("1abcde", "Meetup", "<p>Photos.</p>", target="https://www.reddit.com/gallery/1abcde", at=self.now - 60))
        await self.round()
        messages = self.sent()
        self.assertEqual([names for _, names in self.net.uploads], [["aaa111.jpeg", "bbb222.jpeg"], ["more3.jpeg", "more4.jpeg"], ["more5.jpeg"]])
        self.assertEqual([message.get("content") or "" for message in messages], ["# Meetup\n\nPhotos.", "", f"cc: <@&{self.role}>"])
        self.assertEqual([message["allowed_mentions"] for message in messages], [{"parse": []}, {"parse": []}, {"parse": [], "roles": [self.role]}], "only the message that now ends the post pings")
        [row] = await self.rows()
        self.assertEqual((row["state"], row["note"]), ("sent", None))

    def serve_three_big_pictures(self) -> None:
        """A gallery post of three pictures of 9 MiB each, so the first message holds two and the third follows in a message of its own."""
        self.web.answer("https://i.redd.it/", reply(b"\x89PNG" + b"0" * (9 * 1024 * 1024), headers=PICTURE))
        self.web.answer("https://embed.reddit.com/", embed_page(inside=GALLERY.replace("</ul>", '<li slot="page-3"><img src="https://preview.redd.it/x-v0-ccc333.jpeg?s=3"/></li></ul>')))
        self.serve(entry("1abcde", "Meetup", "<p>Photos.</p>", target="https://www.reddit.com/gallery/1abcde", at=self.now - 60))

    async def test_a_message_that_landed_as_the_connection_dropped_is_still_followed_by_its_other_files_and_the_closing_line(self):
        await self.follow(roles=[self.role])
        self.serve_three_big_pictures()
        self.net.keep_history = True
        original = self.bot.http.request
        dropped: list[int] = []

        async def lands_then_drops(route, **kwargs):
            answer = await original(route, **kwargs)
            if route.method == "POST" and route.path == "/channels/{channel_id}/messages" and not dropped:
                dropped.append(1)
                raise aiohttp.ServerDisconnectedError()
            return answer
        self.bot.http.request = lands_then_drops
        await self.round()
        self.assertEqual((await self.rows())[0]["done"], 0)
        await self.round()
        messages = self.sent()
        self.assertEqual([message.get("content") or "" for message in messages], ["# Meetup\n\nPhotos.", f"cc: <@&{self.role}>"], "the first message was found in the channel, and what was to follow it still went")
        self.assertEqual([names for _, names in self.net.uploads], [["aaa111.jpeg", "bbb222.jpeg"], ["ccc333.jpeg"]])
        self.assertEqual([message["allowed_mentions"] for message in messages], [{"parse": []}, {"parse": [], "roles": [self.role]}])
        self.assertEqual((await self.rows())[0]["state"], "sent")

    async def test_a_refused_message_is_still_followed_by_its_other_files_and_the_closing_line(self):
        await self.follow(roles=[self.role])
        self.serve_three_big_pictures()
        failed = self.refuse_sends(lambda body: "Photos" in (body.get("content") or ""))
        await self.round()
        [message] = self.sent()
        self.assertEqual(len(failed), 1)
        self.assertEqual((message["content"], message["allowed_mentions"]), (f"cc: <@&{self.role}>", {"parse": [], "roles": [self.role]}))
        self.assertEqual([names for _, names in self.net.uploads], [["ccc333.jpeg"]])
        [row] = await self.rows()
        self.assertEqual(row["state"], "sent")
        self.assertIn("Discord refused one message of this post, and the rest went out", row["note"])

    def refuse_sends(self, when) -> list[int]:
        return self.fail_sends(when, lambda: discord.HTTPException(FakeHTTPResponse(400), {"code": 200000, "message": "Message was blocked by AutoMod"}))

    async def test_when_the_message_that_ends_a_post_is_refused_the_closing_line_goes_out_on_its_own(self):
        await self.follow(roles=[self.role])
        self.serve(entry("1abcde", "Long one", f'<p>First</p>{picture_line("one1.png")}<p>Refused words</p>', at=self.now - 60))
        failed = self.refuse_sends(lambda body: "Refused" in (body.get("content") or ""))
        await self.round()
        messages = self.sent()
        self.assertEqual([message["content"] for message in messages], ["# Long one\n\nFirst", f"cc: <@&{self.role}>"])
        self.assertEqual([message["allowed_mentions"] for message in messages], [{"parse": []}, {"parse": [], "roles": [self.role]}], "the roles still hear of the post")
        self.assertEqual(len(failed), 1)
        [row] = await self.rows()
        self.assertEqual(row["state"], "sent")
        self.assertIn("Discord refused one message of this post, and the rest went out", row["note"])

    async def test_a_post_refused_whole_pings_nobody(self):
        await self.follow(roles=[self.role])
        self.serve(entry("1abcde", "Refused words", "<p>All of it.</p>", at=self.now - 60))
        failed = self.refuse_sends(lambda body: True)
        await self.round()
        self.assertEqual(self.sent(), [])
        self.assertEqual(len(failed), 1, "a closing line isn't sent alone under a post nobody saw")
        self.assertEqual((await self.rows())[0]["state"], "sent")

    async def test_a_closing_line_refused_on_its_own_is_not_tried_again(self):
        await self.follow(roles=[self.role])
        self.serve(entry("1abcde", "Long one", f'<p>First</p>{picture_line("one1.png")}<p>Second</p>', at=self.now - 60))
        failed = self.refuse_sends(lambda body: "cc:" in (body.get("content") or ""))
        await self.round()
        self.assertEqual([message["content"] for message in self.sent()], ["# Long one\n\nFirst"])
        self.assertEqual(len(failed), 2, "once under the words above it, and once alone")
        self.assertEqual((await self.rows())[0]["state"], "sent")

    async def test_a_link_standing_in_for_a_file_goes_above_the_closing_line(self):
        await self.follow(roles=[self.role])
        self.web.answer("https://embed.reddit.com/", reply(b"", status=404))
        self.serve(entry("1abcde", "Clip", "<p>Watch.</p>", target="https://v.redd.it/vid123", at=self.now - 60))
        await self.round()
        [message] = self.sent()
        self.assertEqual(message["content"], f"# Clip\n\nWatch.\n{link_of('1abcde')}\n\ncc: <@&{self.role}>")
        self.assertEqual(message["allowed_mentions"], {"parse": [], "roles": [self.role]})

    async def test_a_link_that_leaves_no_room_for_the_closing_line_sends_the_line_in_the_next_message(self):
        await self.follow(roles=[self.role])
        self.web.answer("https://i.redd.it/gone1.png", reply(b"", status=404))
        closing = closing_line([self.role])
        words = "x" * (reader.MESSAGE_LIMIT - len(f"# Tight\n\n\n\n{closing}") - 5)
        self.serve(entry("1abcde", "Tight", f"<p>{words}</p>", target="https://i.redd.it/gone1.png", at=self.now - 60))
        await self.round()
        messages = self.sent()
        self.assertEqual([message["content"] for message in messages], [f"# Tight\n\n{words}\nhttps://i.redd.it/gone1.png", closing])
        self.assertEqual([message["allowed_mentions"] for message in messages], [{"parse": []}, {"parse": [], "roles": [self.role]}], "the line and its ping still end the post")
        self.assertEqual(self.net.uploads, [])

    async def test_a_gallerys_pictures_come_from_its_embed_page_and_wait_for_it_a_while(self):
        feed_id = await self.follow()
        self.web.answer("https://embed.reddit.com/", OutsideError("The service took too long to answer."))
        self.serve(entry("1abcde", "Meetup", "<p>Photos.</p>", target="https://www.reddit.com/gallery/1abcde", at=self.now - 60))
        await self.round()
        self.assertEqual(self.sent(), [])
        self.assertIn("couldn't be listed yet", await self.db.scalar("SELECT last_error FROM reddit_feeds WHERE id = ?", (feed_id,)))
        self.web.answer("https://embed.reddit.com/", embed_page(inside=GALLERY))
        await self.round()
        self.assertEqual([names for _, names in self.net.uploads], [["aaa111.jpeg", "bbb222.jpeg"]])
        self.assertEqual(self.web.called("https://embed.reddit.com/")[-1]["url"], "https://embed.reddit.com/r/example/comments/1abcde/a_post/?embed=true")

    async def test_a_gallery_whose_page_never_answers_goes_out_with_its_link(self):
        await self.follow()
        self.web.answer("https://embed.reddit.com/", reply(b"", status=503))
        self.serve(entry("1abcde", "Meetup", "<p>Photos.</p>", target="https://www.reddit.com/gallery/1abcde", at=self.now - 60))
        for _ in range(LOOKUP_TRIES - 1):
            await self.round()
            self.assertEqual(self.sent(), [])
        await self.round()
        self.assertEqual([message["content"] for message in self.sent()], [f"# Meetup\n\nPhotos.\n\n{link_of('1abcde')}"])

    async def test_a_video_is_uploaded_from_the_sharpest_file_that_fits(self):
        await self.follow()
        self.web.answer("https://embed.reddit.com/", embed_page(title="Clip", target="https://v.redd.it/vid123", inside=player()))
        self.web.answer("https://packaged-media.redd.it/vid123/pb/m2-res_1080p.mp4", OutsideError("The answer was too large."))
        self.web.answer("https://packaged-media.redd.it/vid123/pb/m2-res_720p.mp4", reply(MP4, headers={"Content-Type": "video/mp4"}))
        self.serve(entry("1abcde", "Clip", "<p>Watch.</p>", target="https://v.redd.it/vid123", at=self.now - 60))
        await self.round()
        self.assertEqual(self.net.uploads, [(int(self.text["id"]), ["vid123.mp4"])])
        self.assertEqual([call["url"].split("?")[0].rsplit("/", 1)[-1] for call in self.web.called("https://packaged-media.redd.it/")], ["m2-res_1080p.mp4", "m2-res_720p.mp4"])
        self.assertEqual(self.sent()[0]["content"], "# Clip\n\nWatch.")

    async def test_a_video_with_no_file_to_be_had_goes_out_with_the_posts_link(self):
        await self.follow()
        self.web.answer("https://embed.reddit.com/", reply(b"", status=404))
        self.serve(entry("1abcde", "Clip", "<p>Watch.</p>", target="https://v.redd.it/vid123", at=self.now - 60))
        await self.round()
        self.assertEqual(self.sent()[0]["content"], f"# Clip\n\nWatch.\n{link_of('1abcde')}")

    async def test_reads_keep_to_what_reddit_says_is_left_and_accounts_take_turns(self):
        await self.follow()
        await self.follow("OtherMaker")
        self.serve()
        self.serve(account="OtherMaker")
        self.now += 61
        start = self.now
        await self.reddit.run_round()
        self.assertEqual(len(self.web.calls), 1)
        self.assertEqual(self.reddit._next_read, start + 38, "nothing is left of this minute, so the next read goes a second after the allowance starts over")
        self.assertEqual((await self.bot.timers.get_key(POLL, POLL))["due_at"], start + 38)
        self.now += 10
        await self.reddit.run_round()
        self.assertEqual(len(self.web.calls), 1, "a round before then reads nothing")
        self.now += 30
        await self.reddit.run_round()
        self.assertEqual([call["url"] for call in self.web.calls], [reader.feed_address(ACCOUNT), reader.feed_address("OtherMaker")])
        self.assertEqual(self.web.calls[0]["hosts"], frozenset({"www.reddit.com"}))
        self.now += 40
        await self.reddit.run_round()
        self.assertEqual(self.web.calls[-1]["url"], reader.feed_address(ACCOUNT), "the account read longest ago goes next")

    async def test_an_answer_that_says_nothing_about_the_allowance_counts_as_a_minute(self):
        await self.follow()
        self.serve(headers=None)
        self.now += 61
        start = self.now
        await self.reddit.run_round()
        self.assertEqual(self.reddit._next_read, start + POLL_SECONDS + 1)

    async def test_a_slow_down_answer_waits_as_long_as_reddit_says(self):
        await self.follow()
        self.serve(status=429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "20", "Retry-After": "45"})
        self.now += 61
        start = self.now
        await self.reddit.run_round()
        self.assertEqual(self.reddit._next_read, start + 46)
        self.assertEqual(self.reddit._reads[ACCOUNT.lower()]["error"], SLOWED)
        for _ in range(3):
            self.now = self.reddit._next_read + 1
            await self.reddit.run_round()
        self.assertGreaterEqual(self.reddit._next_read - self.now, 2 * POLL_SECONDS, "still refused after waiting as told, so the wait grows")
        self.serve()
        self.now = self.reddit._next_read + 1
        await self.reddit.run_round()
        self.assertIsNone(self.reddit._reads[ACCOUNT.lower()]["error"])
        self.assertEqual(self.reddit._limited, 0)

    async def test_a_feed_that_fails_is_read_less_and_less_often_and_never_given_up_on(self):
        await self.follow()
        self.serve(status=404, headers=PACED)
        await self.round()
        state = self.reddit._reads[ACCOUNT.lower()]
        self.assertEqual((state["error"], state["failures"]), (NO_ACCOUNT, 1))
        self.assertEqual(state["next_at"], self.now + 2 * POLL_SECONDS)
        await self.round()
        self.assertEqual(len(self.web.calls), 1, "its turn hasn't come back yet")
        self.serve(status=403, headers=PACED)
        for _ in range(12):
            await self.round(later=1801)
        self.assertEqual(state["error"], "Reddit answered 403 for this account's feed.")
        self.assertEqual(state["next_at"] - self.now, 1800, "half an hour at the most")
        self.web.answer(reader.feed_address(ACCOUNT), OutsideError("The service took too long to answer."))
        await self.round(later=1801)
        self.assertEqual(state["error"], "The service took too long to answer.")
        self.serve(entry("1abcde", "Back", "<p>x</p>", at=self.now + 1800))
        await self.round(later=1801)
        self.assertEqual((state["error"], state["failures"]), (None, 0))
        self.assertEqual(len(self.sent()), 1)
        self.assert_no_errors()

    async def test_a_page_that_is_not_a_feed_counts_as_a_failed_read(self):
        await self.follow()
        self.web.answer(reader.feed_address(ACCOUNT), reply(b"<html>Sign in</html>", headers=PACED))
        await self.round()
        self.assertEqual(self.reddit._reads[ACCOUNT.lower()]["error"], "Reddit didn't answer with a feed.")

    async def test_a_server_with_this_off_is_not_read_and_gets_nothing(self):
        await self.follow()
        self.serve(entry("1abcde", "Hello", "<p>x</p>", at=self.now - 60))
        await self.bot.settings.save(self.guild_id, "reddit", {"enabled": False})
        await self.round()
        self.assertEqual((self.web.calls, self.sent()), ([], []))

    async def test_a_channel_that_is_gone_is_noted_and_the_post_waits(self):
        feed_id = await self.follow(channel={"id": "123456789012345678"})
        self.serve(entry("1abcde", "Hello", "<p>x</p>", at=self.now - 60))
        original = self.bot.http.request

        async def unknown_channel(route, **kwargs):
            if route.method == "GET" and route.path == "/channels/{channel_id}":
                raise discord.NotFound(FakeHTTPResponse(404), {"code": 10003, "message": "Unknown Channel"})
            return await original(route, **kwargs)
        self.bot.http.request = unknown_channel
        await self.round()
        self.assertEqual(await self.db.scalar("SELECT last_error FROM reddit_feeds WHERE id = ?", (feed_id,)), CHANNEL_GONE)
        self.assertEqual((await self.rows())[0]["state"], "waiting")
        self.assertEqual(self.sent(), [])

    async def test_a_role_the_bot_cannot_ping_is_noted_and_the_post_still_goes(self):
        feed_id = await self.follow(roles=[123456789012345678])
        self.serve(entry("1abcde", "Hello", "<p>x</p>", at=self.now - 60))
        await self.round()
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(await self.db.scalar("SELECT last_error FROM reddit_feeds WHERE id = ?", (feed_id,)), NOT_PINGED)

    async def test_the_round_is_scheduled_once_and_old_records_are_forgotten(self):
        timer = await self.bot.timers.get_key(POLL, POLL)
        self.assertIsNotNone(timer)
        await self.reddit.on_load()
        self.assertEqual(len(await self.bot.timers.list_kind(POLL)), 1)
        feed_id = await self.follow()
        for index, age in enumerate((40 * 86400, 86400)):
            await self.db.run("INSERT INTO reddit_posts (guild_id, feed_id, post_key, title, link, state, source, found_at) VALUES (?, ?, ?, 'T', 'L', 'sent', 'feed', ?)", (self.guild_id, feed_id, f"t3_old{index}", self.now - age))
        self.serve()
        await self.round()
        self.assertEqual([row["post_key"] for row in await self.rows()], ["t3_old1"])

    # ---- one post by its link ----

    async def test_one_post_is_relayed_by_its_link_whatever_its_age(self):
        feed_id = await self.follow()
        link = link_of("1abcde")
        self.web.answer(link + ".rss", reply(feed(entry("1abcde", "An old post", "<p>Old words</p>", target="https://i.redd.it/pic123.png", at=self.now - 30 * 86400)), headers=PACED))
        channel = self.guild.get_channel(int(self.log_channel["id"]))
        post, outcome = await self.reddit.relay(self.guild, channel, link, [self.role])
        self.assertEqual((post.title, outcome.finished, outcome.problem, outcome.sent), ("An old post", True, None, 1))
        [message] = self.sent()
        self.assertEqual(message["content"], f"# An old post\n\nOld words\n\ncc: <@&{self.role}>")
        self.assertEqual(self.net.uploads, [(int(self.log_channel["id"]), ["pic123.png"])])
        [row] = await self.rows()
        self.assertEqual((row["feed_id"], row["state"], row["source"]), (feed_id, "sent", "command"), "the account is followed here, so the feed won't send it again")

    async def test_a_post_relayed_by_hand_is_not_sent_again_by_the_feed_that_had_it_waiting(self):
        await self.follow()
        link = link_of("1abcde")
        fresh = entry("1abcde", "Fresh post", "<p>Words</p>", at=self.now - 30)
        self.serve(fresh)
        original = self.bot.http.request
        self.fail_sends(lambda body: True, lambda: discord.Forbidden(FakeHTTPResponse(403), {"code": 50013, "message": "Missing Permissions"}))
        await self.round()
        self.assertEqual([row["state"] for row in await self.rows()], ["waiting"])
        self.bot.http.request = original
        self.web.answer(link + ".rss", reply(feed(fresh), headers=PACED))
        await self.reddit.relay(self.guild, self.guild.get_channel(int(self.text["id"])), link, [])
        await self.round()
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual([(row["state"], row["source"]) for row in await self.rows()], [("sent", "feed")])

    async def test_a_post_whose_feed_is_gone_is_read_from_its_embed_page(self):
        link = link_of("1abcde")
        self.web.answer(link + ".rss", reply(b"", status=404, headers=PACED))
        body = '<div class="md" id="t3_1abcde-post-rtjson-content"><p>From the page</p></div>'
        self.web.answer("https://embed.reddit.com/", embed_page(target=link, inside=body).replace('<h1 class="line-clamp-3">A gallery</h1>', "<shreddit-embed-title>Text post</shreddit-embed-title>"))
        channel = self.guild.get_channel(int(self.text["id"]))
        post, outcome = await self.reddit.relay(self.guild, channel, link, [])
        self.assertTrue(outcome.finished)
        self.assertEqual(self.sent()[0]["content"], "# Text post\n\nFrom the page")
        self.assertEqual(await self.rows(), [], "nobody here follows the account, so there's nothing to record")

    async def test_a_picture_post_is_not_relayed_from_a_page_that_leaves_out_its_words(self):
        link = link_of("1abcde")
        self.web.answer(link + ".rss", reply(b"", status=404, headers=PACED))
        self.web.answer("https://embed.reddit.com/", embed_page(title="A picture post", target="https://i.redd.it/pic123.png"))
        with self.assertRaises(FetchError) as raised:
            await self.reddit.relay(self.guild, self.guild.get_channel(int(self.text["id"])), link, [])
        self.assertEqual(str(raised.exception), "Reddit handed over that post's title and pictures but not its words, so nothing was sent. Reddit answered 404 for the post's feed.")
        self.assertEqual(self.sent(), [])

    async def test_a_read_by_hand_that_is_told_to_slow_down_waits_and_asks_once_more(self):
        link = link_of("1abcde")
        answers = [reply(b"", status=429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "12"}), reply(feed(entry("1abcde", "A post", "<p>x</p>", at=self.now)), headers=PACED)]
        self.web.answer(link + ".rss", lambda call: answers.pop(0))
        post = await self.reddit.fetch_post(link)
        self.assertEqual((post.title, self.slept), ("A post", [13]))

    async def test_with_some_of_the_allowance_left_reads_are_spread_over_the_minute(self):
        await self.follow()
        self.serve(headers={"x-ratelimit-remaining": "9", "x-ratelimit-used": "1", "x-ratelimit-reset": "60"})
        self.now += 61
        start = self.now
        await self.reddit.run_round()
        self.assertEqual(self.reddit._next_read, start + 6)

    async def test_a_post_reddit_will_not_hand_over_says_so(self):
        link = link_of("1abcde")
        self.web.answer(link + ".rss", reply(b"", status=403, headers=PACED))
        self.web.answer("https://embed.reddit.com/", reply(b"", status=403))
        with self.assertRaises(FetchError) as raised:
            await self.reddit.relay(self.guild, self.guild.get_channel(int(self.text["id"])), link, [])
        self.assertEqual(str(raised.exception), "Reddit didn't hand over that post. Reddit answered 403 for the post's feed. Reddit answered 403 for the post's embed page.")

    async def test_a_read_by_hand_waits_for_reddits_minute_like_any_other(self):
        link = link_of("1abcde")
        self.web.answer(link + ".rss", reply(feed(entry("1abcde", "A post", "<p>x</p>", at=self.now)), headers=PACED))
        self.reddit._next_read = self.now + 25
        await self.reddit.fetch_post(link)
        self.assertEqual(self.slept, [25])

    # ---- the courier ----

    async def courier(self) -> int:
        self.net.keep_webhooks = True
        await self.reddit.set_intake(self.guild, self.guild.get_channel(int(self.log_channel["id"])), Actor(self.owner_id, "owner"))
        return self.reddit.cfg(self.guild_id)["intake_webhook"]

    def webhook_message(self, hook_id: int, embeds: list[dict], *, channel: dict | None = None, message_id: int | None = None) -> dict:
        return {
            "id": str(message_id or new_id()), "channel_id": (channel or self.log_channel)["id"], "guild_id": str(self.guild_id), "webhook_id": str(hook_id),
            "author": {"id": str(hook_id), "username": "Reddit courier", "discriminator": "0000", "avatar": None, "bot": True}, "content": "", "timestamp": iso(), "edited_timestamp": None,
            "tts": False, "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [], "embeds": [{"type": "rich", **embed} for embed in embeds], "pinned": False, "type": 0,
        }

    async def deliver(self, hook_id: int, embeds: list[dict], **kwargs) -> dict:
        message = self.webhook_message(hook_id, embeds, **kwargs)
        self.bot._connection.parse_message_create(message)
        await self.settle()
        await asyncio.gather(*list(self.reddit._tasks))
        return message

    def cleared(self) -> list[int]:
        return [params for method, path, params in self.net.calls if method == "DELETE" and path == "/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}"]

    async def test_setting_the_courier_up_makes_a_webhook_of_its_own_and_gives_its_address(self):
        self.net.keep_webhooks = True
        address = await self.reddit.set_intake(self.guild, self.guild.get_channel(int(self.log_channel["id"])), Actor(self.owner_id, "owner"))
        cfg = self.reddit.cfg(self.guild_id)
        [made] = self.net.webhooks[int(self.log_channel["id"])]
        self.assertEqual((made["name"], cfg["intake_channel"], cfg["intake_webhook"]), ("Reddit courier", int(self.log_channel["id"]), int(made["id"])))
        self.assertEqual(address, f"https://discord.com/api/webhooks/{made['id']}/webhook-token")
        self.assertEqual(self.reddit.admin_fields, {"intake_channel", "intake_webhook"})

    async def test_a_courier_delivery_is_relayed_and_then_cleared_from_its_channel(self):
        hook_id = await self.courier()
        await self.follow(roles=[self.role])
        message = await self.deliver(hook_id, delivery(payload_for(at=self.now - 20)))
        [sent] = self.sent()
        self.assertEqual(sent["content"], f"# From the courier\n\nDelivered.\n\ncc: <@&{self.role}>")
        [row] = await self.rows()
        self.assertEqual((row["source"], row["state"]), ("courier", "sent"))
        self.assertEqual(len(self.cleared()), 1)
        self.assertEqual(self.net.webhook_params[-1][1], "/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}")
        self.assertIsNotNone(message)
        self.assert_no_errors()

    async def test_a_post_that_arrives_by_both_ways_goes_out_once(self):
        hook_id = await self.courier()
        await self.follow()
        await self.deliver(hook_id, delivery(payload_for(at=self.now - 20)))
        self.serve(entry("1abcde", "From the courier", "<p>Delivered.</p>", at=self.now - 20))
        await self.round()
        await self.deliver(hook_id, delivery(payload_for(at=self.now - 81)))
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(len(await self.rows()), 1)

    async def test_a_long_delivery_in_several_messages_is_put_together(self):
        hook_id = await self.courier()
        await self.follow()
        parts = delivery(payload_for(body="<p>" + "Many words here. " * 600 + "</p>", at=self.now - 20))
        self.assertGreater(len(parts), 2)
        for part in reversed(parts):
            self.assertEqual(self.sent(), [])
            await self.deliver(hook_id, [part])
        messages = self.sent()
        self.assertGreater(len(messages), 4)
        self.assertEqual(" ".join(" ".join(message["content"] for message in messages).split()), " ".join(("# From the courier " + "Many words here. " * 600).split()))
        self.assertEqual(len(self.cleared()), len(parts))

    async def test_only_the_couriers_own_webhook_in_its_own_channel_is_listened_to(self):
        hook_id = await self.courier()
        await self.follow()
        embeds = delivery(payload_for(at=self.now - 20))
        await self.deliver(hook_id + 1, embeds)
        await self.deliver(hook_id, embeds, channel=self.text)
        self.dispatch_message(user_id=self.admin_id, channel=self.log_channel, embeds=[{"type": "rich", **embeds[0]}])
        await self.settle()
        self.assertEqual(self.sent(), [])
        await self.bot.settings.save(self.guild_id, "reddit", {"enabled": False})
        await self.deliver(hook_id, embeds)
        self.assertEqual(self.sent(), [])

    async def test_a_delivery_for_an_account_nobody_follows_or_too_old_goes_nowhere(self):
        hook_id = await self.courier()
        await self.follow("OtherMaker")
        await self.deliver(hook_id, delivery(payload_for(at=self.now - 20)))
        await self.follow()
        await self.deliver(hook_id, delivery(payload_for("1bbbbb", at=self.now - 2 * MAX_AGE)))
        await self.deliver(hook_id, delivery({**payload_for("1ccccc", at=self.now - 20), "author": "OtherMaker", "title": "Wrong name on it", "link": link_of("1ccccc")}))
        self.assertEqual([message["content"].splitlines()[0] for message in self.sent()], ["# Wrong name on it"], "it goes to the feed of the account it names")
        self.assertEqual(len(self.cleared()), 3)

    async def test_deliveries_made_while_the_bot_was_away_are_read_when_it_is_back(self):
        hook_id = await self.courier()
        await self.follow()
        self.net.keep_history = True
        base = discord.utils.time_snowflake(discord.utils.utcnow())
        for offset, part in enumerate(delivery(payload_for(body="<p>" + "Waiting words. " * 500 + "</p>", at=self.now - 20))):
            message = self.webhook_message(hook_id, [part], message_id=base + offset)
            self.net.messages[int(message["id"])] = message
        other = self.webhook_message(hook_id + 5, delivery(payload_for("1bbbbb", title="Not the courier", at=self.now - 20)), message_id=base + 50)
        self.net.messages[int(other["id"])] = other
        await self.reddit.catch_up(self.guild)
        await asyncio.gather(*list(self.reddit._tasks))
        self.assertEqual(self.sent()[0]["content"].splitlines()[0], "# From the courier")
        self.assertFalse(any("Not the courier" in (message.get("content") or "") for message in self.sent()))

    async def test_a_refused_new_webhook_leaves_the_old_courier_working(self):
        first = await self.courier()
        original = self.bot.http.request

        async def refuse(route, **kwargs):
            if route.method == "POST" and route.path == "/channels/{channel_id}/webhooks":
                raise discord.Forbidden(FakeHTTPResponse(403), {"code": 50013, "message": "Missing Permissions"})
            return await original(route, **kwargs)
        self.bot.http.request = refuse
        with self.assertRaises(Invalid) as raised:
            await self.reddit.set_intake(self.guild, self.guild.get_channel(int(self.text["id"])), Actor(self.owner_id, "owner"))
        self.assertEqual(str(raised.exception), "The bot needs Manage Webhooks in that channel.")
        self.assertEqual(self.reddit.cfg(self.guild_id)["intake_webhook"], first)
        self.assertEqual([path for method, path, _ in self.net.calls if method == "DELETE" and path.startswith("/webhooks/")], [])

    async def test_changing_the_couriers_channel_deletes_the_old_webhook(self):
        first = await self.courier()
        await self.reddit.set_intake(self.guild, self.guild.get_channel(int(self.text["id"])), Actor(self.owner_id, "owner"))
        deleted = [path for method, path, _ in self.net.calls if method == "DELETE" and path.startswith("/webhooks/{webhook_id}")]
        self.assertEqual(len(deleted), 1)
        self.assertNotEqual(self.reddit.cfg(self.guild_id)["intake_webhook"], first)
        await self.reddit.drop_intake(self.guild, Actor(self.owner_id, "owner"))
        cfg = self.reddit.cfg(self.guild_id)
        self.assertEqual((cfg["intake_channel"], cfg["intake_webhook"]), (None, None))


class CommandTest(BotTest):
    modules = [RedditPosts]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.reddit = self.cog(RedditPosts)
        self.web = FakeOutside()
        self.reddit.outside = self.web
        await self.bot.settings.save(self.guild.id, "reddit", {"enabled": True})

    async def test_every_command_takes_manage_server(self):
        for command in (self.reddit.reddit, self.reddit.reddit_add, self.reddit.reddit_remove, self.reddit.reddit_post):
            self.assertEqual(required_permissions(command), ("manage_guild",), command.name)
        self.assertTrue(self.reddit.command_pack)

    async def test_adding_listing_and_removing_an_account(self):
        ctx = self.context()
        await self.reddit.reddit_add.callback(self.reddit, ctx, f"https://www.reddit.com/user/{ACCOUNT}/", self.channel, self.role_member)
        self.assertEqual(ctx.oks, [f"Posts u/{ACCOUNT} makes from now on are relayed to {self.channel.mention}."])
        row = await self.db.one("SELECT * FROM reddit_feeds")
        self.assertEqual((row["account"], row["channel_id"], json.loads(row["role_ids"]), row["created_by"]), (ACCOUNT, self.channel.id, [self.role_member.id], self.admin.id))
        again = self.context()
        await self.reddit.reddit_add.callback(self.reddit, again, ACCOUNT.lower(), None, None)
        self.assertEqual(again.fails, [f"This server already relays u/{ACCOUNT.lower()}."])
        bad = self.context()
        await self.reddit.reddit_add.callback(self.reddit, bad, "no", None, None)
        self.assertIn("That isn't a Reddit account's name.", bad.fails[0])
        listed = self.context()
        await self.reddit.reddit.callback(self.reddit, listed)
        self.assertEqual(listed.sent[0]["embed"].description, f"[u/{ACCOUNT}](<https://www.reddit.com/user/{ACCOUNT}>) in <#{self.channel.id}>")
        gone = self.context()
        await self.reddit.reddit_remove.callback(self.reddit, gone, f"u/{ACCOUNT}")
        self.assertEqual(gone.oks, [f"Posts from u/{ACCOUNT} are no longer relayed. The ones already here stay."])
        missing = self.context()
        await self.reddit.reddit_remove.callback(self.reddit, missing, ACCOUNT)
        self.assertEqual(missing.fails, [f"This server doesn't relay u/{ACCOUNT}."])
        actions = [row["action"] for row in await self.bot.activity_log.recent(self.guild.id)]
        self.assertIn("reddit.add", actions)
        self.assertIn("reddit.remove", actions)

    async def test_a_server_follows_ten_accounts_at_the_most(self):
        for index in range(MAX_FEEDS):
            await self.reddit.follow(self.guild.id, f"Maker{index:03}", self.channel.id, [], Actor(1, "x"))
        with self.assertRaises(Invalid) as raised:
            await self.reddit.follow(self.guild.id, "OneMore", self.channel.id, [], Actor(1, "x"))
        self.assertIn(f"This server relays {MAX_FEEDS} accounts", str(raised.exception))
        self.assertEqual(self.reddit.row_limits["reddit_feeds"], MAX_FEEDS)

    async def test_someone_who_could_not_send_the_ping_themselves_cannot_save_it(self):
        poster = FakeMember(self.guild, "poster", self.guild.role("Poster", 3, "view_channel", "send_messages", "manage_guild"))
        ctx = self.context(author=poster)
        await self.reddit.reddit_add.callback(self.reddit, ctx, ACCOUNT, self.channel, self.role_member)
        self.assertEqual(ctx.fails, ["You need Mention Everyone in that channel to ping through the bot."])
        everyone = self.context()
        await self.reddit.reddit_add.callback(self.reddit, everyone, ACCOUNT, self.channel, self.guild.default_role)
        self.assertEqual(everyone.fails, ["Pick a role of its own. @everyone can't be the role a post mentions."])
        viewer = FakeMember(self.guild, "viewer", self.guild.role("Viewer", 3, "view_channel", "manage_guild"))
        blocked = self.context(author=viewer)
        await self.reddit.reddit_add.callback(self.reddit, blocked, ACCOUNT, self.channel, None)
        self.assertEqual(blocked.fails, ["You can't post in that channel."])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM reddit_feeds"), 0)
        fine = self.context(author=poster)
        await self.reddit.reddit_add.callback(self.reddit, fine, ACCOUNT, self.channel, None)
        self.assertEqual(len(fine.oks), 1)

    async def test_the_post_command_relays_one_post_and_says_where_it_went(self):
        link = link_of("1abcde")
        self.web.answer(link + ".rss", feed(entry("1abcde", "A post", "<p>Words</p>", at=5)))
        other = FakeChannel(self.guild, "news")
        ctx = self.context()
        await self.reddit.reddit_post.callback(self.reddit, ctx, link, other, None)
        self.assertEqual(ctx.oks, [f"\"A post\" is in {other.mention}."])
        self.assertEqual([message.content for message in other.sent], ["# A post\n\nWords"])
        bad = self.context()
        await self.reddit.reddit_post.callback(self.reddit, bad, "https://example.com/x", None, None)
        self.assertIn("That isn't a link to a Reddit post.", bad.fails[0])
        self.web.answer(link + ".rss", reply(b"", status=404))
        self.web.answer("https://embed.reddit.com/", reply(b"", status=404))
        down = self.context()
        await self.reddit.reddit_post.callback(self.reddit, down, link, None, None)
        self.assertIn("Reddit didn't hand over that post.", down.fails[0])


class PageTest(BotTest):
    modules = [RedditPosts]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.reddit = self.cog(RedditPosts)
        self.web = FakeOutside()
        self.reddit.outside = self.web
        self.bot.ops = DiscordOps(self.bot)  # the dashboard checks channels against the fake server, not the demo's

    async def test_add_list_edit_and_remove(self):
        client = await self.client()
        answer = await client.post(self.path("/reddit"), json={"account": f"u/{ACCOUNT}", "channel_id": str(self.channel.id), "role_ids": [str(self.role_member.id)]})
        self.assertEqual(answer.status, 200, await answer.text())
        listed = await (await client.get(self.path("/reddit"))).json()
        [row] = listed["feeds"]
        self.assertEqual((row["account"], row["channel_id"], row["role_ids"], row["hidden"], row["waiting"]), (ACCOUNT, str(self.channel.id), [str(self.role_member.id)], False, 0))
        self.assertEqual((listed["most"], listed["posts"], listed["admin"], listed["intake"]["intake_webhook"]), (MAX_FEEDS, [], True, False))
        self.assertEqual((await client.post(self.path("/reddit"), json={"account": ACCOUNT, "channel_id": str(self.channel.id)})).status, 400, "already followed")
        other = FakeChannel(self.guild, "news")
        edited = await client.put(self.path(f"/reddit/{row['id']}"), json={"channel_id": str(other.id), "role_ids": []})
        self.assertEqual(edited.status, 200, await edited.text())
        stored = await self.db.one("SELECT * FROM reddit_feeds")
        self.assertEqual((stored["channel_id"], stored["role_ids"]), (other.id, "[]"))
        self.assertEqual((await client.delete(self.path(f"/reddit/{row['id']}"))).status, 200)
        self.assertEqual((await (await client.get(self.path("/reddit"))).json())["feeds"], [])
        self.assertEqual((await client.delete(self.path(f"/reddit/{row['id']}"))).status, 404)

    async def test_a_bad_name_role_or_channel_is_refused_on_its_field(self):
        client = await self.client()
        for form, field in (({"account": "no", "channel_id": str(self.channel.id)}, "account"), ({"account": ACCOUNT, "channel_id": "5"}, "channel_id"),
                            ({"account": ACCOUNT, "channel_id": str(self.channel.id), "role_ids": ["12345678901234567"]}, "role_ids"),
                            ({"account": ACCOUNT, "channel_id": str(self.channel.id), "role_ids": [str(self.guild.id)]}, "role_ids"),
                            ({"account": ACCOUNT, "channel_id": str(self.channel.id), "role_ids": "all"}, "role_ids")):
            answer = await client.post(self.path("/reddit"), json=form)
            self.assertEqual(answer.status, 400, form)
            self.assertIn(field, (await answer.json())["fields"], form)

    async def test_the_latest_posts_are_listed_with_where_they_went(self):
        feed_id = (await self.reddit.follow(self.guild.id, ACCOUNT, self.channel.id, [], Actor(1, "x")))["id"]
        await self.db.run("INSERT INTO reddit_posts (guild_id, feed_id, post_key, title, link, state, source, found_at, posted_at, message_id) VALUES (?, ?, 't3_1abcde', 'A post', ?, 'sent', 'courier', 5, 6, 777)",
                          (self.guild.id, feed_id, link_of("1abcde")))
        client = await self.client()
        [post] = (await (await client.get(self.path("/reddit"))).json())["posts"]
        self.assertEqual((post["title"], post["account"], post["state"], post["source"], post["url"]), ("A post", ACCOUNT, "sent", "courier", f"https://discord.com/channels/{self.guild.id}/{self.channel.id}/777"))

    async def test_one_post_is_relayed_from_the_page_and_the_page_can_follow_it(self):
        link = link_of("1abcde")
        self.web.answer(link + ".rss", feed(entry("1abcde", "A post", "<p>Words</p>", at=5)))
        client = await self.client()
        answer = await client.post(self.path("/reddit/post"), json={"link": link, "channel_id": str(self.channel.id)})
        self.assertEqual(answer.status, 200, await answer.text())
        await asyncio.gather(*list(self.reddit._tasks))
        self.assertEqual([message.content for message in self.channel.sent], ["# A post\n\nWords"])
        relay = (await (await client.get(self.path("/reddit"))).json())["relay"]
        self.assertEqual((relay["state"], relay["title"], relay["done"]), ("done", "A post", 1))
        bad = await client.post(self.path("/reddit/post"), json={"link": "https://example.com/", "channel_id": str(self.channel.id)})
        self.assertIn("link", (await bad.json())["fields"])

    async def test_only_an_administrator_sets_the_courier_up(self):
        client = await self.client()
        self.assertEqual((await client.get(self.path("/reddit/intake"))).status, 404, "none set up yet")
        self.admin.roles = [self.guild.default_role, self.role_mod]
        for call in (client.post(self.path("/reddit/intake"), json={"intake_channel": str(self.channel.id)}), client.get(self.path("/reddit/intake")), client.delete(self.path("/reddit/intake"))):
            self.assertEqual((await call).status, 403)
