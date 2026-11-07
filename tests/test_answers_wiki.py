"""The sources behind Wiki answers: reading a page from each kind of site, cutting it into sections, ranking them across a server's
sources, and keeping each copy in step with its sitemap or its links. Every page here is written in this file. Nothing is fetched."""
from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from captainguild.core.answers import wiki
from captainguild.core.answers.wiki import CRAWL, Copies, Library, Shelf, Source, WikiIndex, extract_page, page_links, parse_sitemap, split_sections, tokenize
from captainguild.core import outside as outside_module
from captainguild.core.outside import OutsideError, Reply
from captainguild.db import Database
from tests.answer_fakes import page_html, sitemap
from tests.fake_outside import FakeOutside

BASE = "https://wiki.example.org"
SOURCE = Source(BASE + "/sitemap.xml", prefix="/en/", exclude=("/en/admin/*", "/en/drafts*"))

NOZZLE = page_html("Replacing the Nozzle", (
    '<h2 id="before">Before you start</h2><p>Let the hotend cool. Turn the printer off.</p>'
    '<h2 id="steps">Steps</h2><ol><li>Take the <a href="/en/tools/wrench#size">wrench</a> from the box.</li><li>Unscrew the nozzle.</li><li></li></ol>'
    '<table><tr><th>Model</th><th>Build volume</th></tr><tr><td>X1</td><td>256 mm</td></tr><tr><td></td><td></td></tr></table>'
    '<pre>G28\n   M104 S0\n\n</pre><script>alert(1)</script><img src="x.png"><a class="toc-anchor" href="#steps">link</a>'
    '<div>First line<br>Second line</div><nav>Menu</nav><a href="https://elsewhere.example/page">elsewhere</a>'))


def site_page(head: str, body: str) -> str:
    return f"<!doctype html><html><head>{head}</head><body><nav class=\"menu\"><a href=\"/\">Home</a> Menu links</nav>{body}<footer>Footer text</footer></body></html>"


# One small page in each engine's markup, with the article's words in it and something around it that isn't the article.
ENGINE_PAGES = {
    "MediaWiki": (site_page('<title>Nozzle - Example Wiki</title><meta name="generator" content="MediaWiki 1.42.1">',
                            '<h1 id="firstHeading">Nozzle care</h1><div id="mw-content-text"><div class="mw-parser-output">'
                            '<div id="toc" class="toc">Contents 1 Cleaning</div>'
                            '<h2><span class="mw-headline" id="Cleaning">Cleaning</span><span class="mw-editsection">[edit]</span></h2>'
                            '<p>Heat the nozzle and push filament through.</p><div class="navbox">All guides</div></div></div>'),
                  "Nozzle care", ["## Cleaning {#Cleaning}", "Heat the nozzle and push filament through."], ["[edit]", "Contents", "All guides", "Menu links", "Footer text"]),
    "DokuWiki": (site_page('<title>nozzle [Example Docs]</title><meta name="generator" content="DokuWiki">',
                           '<div class="dokuwiki"><div id="dokuwiki__content"><div class="page"><div id="dw__toc">Table of Contents</div>'
                           '<h1 class="sectionedit1" id="nozzle_care">Nozzle care</h1><div class="level1"><p>Heat the nozzle and push filament through.</p></div>'
                           '<div class="secedit"><button>Edit</button></div></div><div class="docInfo">Last modified</div></div></div>'),
                 "Nozzle care", ["## Nozzle care {#nozzle_care}", "Heat the nozzle and push filament through."], ["Table of Contents", "Edit", "Menu links", "Footer text"]),
    "Wiki.js": (page_html("Nozzle care", '<h2 id="cleaning">Cleaning</h2><p>Heat the nozzle and push filament through.</p>'),
                "Nozzle care", ["## Cleaning {#cleaning}", "Heat the nozzle and push filament through."], ["Sample Wiki"]),
    "Confluence": (site_page('<title>Nozzle care - Printers - Confluence</title><meta name="confluence-request-time" content="1">',
                             '<h1 id="title-heading"><span id="title-text">Nozzle care</span></h1><div id="main-content" class="wiki-content">'
                             '<h2 id="Nozzlecare-Cleaning">Cleaning</h2><p>Heat the nozzle and push filament through.</p></div><div id="likes-section">Like</div>'),
                   "Nozzle care", ["## Cleaning {#Nozzlecare-Cleaning}", "Heat the nozzle and push filament through."], ["Like", "Menu links", "Footer text"]),
    "MkDocs": (site_page('<title>Nozzle care - Example Docs</title><meta name="generator" content="mkdocs-1.6.1, mkdocs-material-9.5.0">',
                         '<div class="md-container"><main class="md-main"><div class="md-content"><article class="md-content__inner md-typeset">'
                         '<h1 id="nozzle-care">Nozzle care<a class="headerlink" href="#nozzle-care">#</a></h1><p>Heat the nozzle and push filament through.</p>'
                         '<aside class="md-source-file">Last update: today</aside></article></div></main></div>'),
               "Nozzle care", ["## Nozzle care {#nozzle-care}", "Heat the nozzle and push filament through."], ["care#", "Last update", "Menu links", "Footer text"]),
    "Docusaurus": (site_page('<title>Nozzle care | Example Docs</title><meta name="generator" content="Docusaurus v3.5.2">',
                             '<div class="main-wrapper"><article><div class="theme-doc-markdown markdown"><header><h1>Nozzle care</h1></header>'
                             '<h2 class="anchor" id="cleaning">Cleaning<a class="hash-link" href="#cleaning">#</a></h2><p>Heat the nozzle and push filament through.</p></div>'
                             '<footer class="theme-doc-footer">Edit this page</footer></article><nav class="pagination-nav">Next page</nav></div>'),
                   "Nozzle care", ["## Cleaning {#cleaning}", "Heat the nozzle and push filament through."], ["Edit this page", "Next page", "Cleaning#", "Menu links"]),
    "GitBook": (site_page('<title>Nozzle care | Example Docs</title><meta name="generator" content="GitBook (1.0)">',
                          '<div class="book"><div class="book-summary"><ul><li>Chapters: getting started with your printer and its first setup</li>'
                          '<li>Every guide to filament, nozzles, plates and upkeep in one long list</li><li>Troubleshooting pages for each model</li></ul></div>'
                          '<div class="page-inner"><section class="normal markdown-section">'
                          '<h1 id="nozzle-care">Nozzle care</h1><p>Heat the nozzle and push filament through.</p></section></div></div>'),
                "Nozzle care", ["## Nozzle care {#nozzle-care}", "Heat the nozzle and push filament through."], ["Chapters", "Menu links", "Footer text"]),
    "article": (site_page("<title>Nozzle care | Example Blog</title>",
                          '<div class="sidebar"><p>Popular posts and a long list of links that is not the article at all</p></div>'
                          '<article><h1>Nozzle care</h1><p>Heat the nozzle and push filament through.</p></article>'),
                "Nozzle care", ["## Nozzle care", "Heat the nozzle and push filament through."], ["Popular posts", "Menu links", "Footer text"]),
    "main": (site_page("<title>Nozzle care | Example Help</title>", '<main><h2 id="clean">Cleaning</h2><p>Heat the nozzle and push filament through.</p></main>'),
             "Nozzle care", ["## Cleaning {#clean}", "Heat the nozzle and push filament through."], ["Menu links", "Footer text"]),
}


class ReadingAPageTest(unittest.TestCase):
    def test_wiki_js_headings_lists_tables_and_code_become_lines(self):
        title, text = extract_page(NOZZLE, BASE + "/en/printer/nozzle", "/en/")
        self.assertEqual(title, "Replacing the Nozzle")
        lines = text.split("\n")
        for expected in ("## Before you start {#before}", "Let the hotend cool. Turn the printer off.", "## Steps {#steps}",
                         "• Take the wrench (/en/tools/wrench) from the box.", "• Unscrew the nozzle.", "Model | Build volume", "X1 | 256 mm", "G28", "M104 S0",
                         "First line", "Second line"):
            self.assertIn(expected, lines)
        for gone in ("alert", "x.png", "Menu", "elsewhere.example", "link"):
            self.assertNotIn(gone, text)
        self.assertIn("elsewhere", text)  # the words of an outside link stay, only its address goes

    def test_each_engine_gives_its_article_and_title(self):
        for engine, (page, title, wanted, unwanted) in ENGINE_PAGES.items():
            with self.subTest(engine=engine):
                found = extract_page(page, "https://docs.example.org/en/nozzle", "/")
                self.assertIsNotNone(found)
                self.assertEqual(found[0], title)
                lines = found[1].split("\n")
                for line in wanted:
                    self.assertIn(line, lines)
                for words in unwanted:
                    self.assertNotIn(words, found[1])

    def test_a_named_selector_takes_the_article(self):
        page = site_page("<title>Odd site</title>", '<div class="ads"><p>Buy things now, a long advertisement block of words</p></div><div class="post-body"><p>The real article.</p></div>')
        self.assertEqual(extract_page(page, "", "/", selector=".post-body"), ("Odd site", "The real article."))
        self.assertEqual(extract_page(page, "", "/", selector=".not-there")[1], "Buy things now, a long advertisement block of words",
                         "a selector that matches nothing falls back to the largest block")

    def test_a_page_no_adapter_knows_gives_its_largest_block_of_text(self):
        page = site_page("<title>Notes - Some Site</title>", '<div class="left"><p>Short.</p></div><div class="center"><h2>Cleaning</h2>'
                         '<p>Heat the nozzle and push filament through until it runs clear.</p><p>Wipe it with a brass brush while warm.</p></div>')
        title, text = extract_page(page, "", "/")
        self.assertEqual(title, "Notes")
        self.assertEqual(text.split("\n"), ["## Cleaning", "Heat the nozzle and push filament through until it runs clear.", "Wipe it with a brass brush while warm."])

    def test_links_to_other_pages_under_the_path_keep_their_path(self):
        page = page_html("Hub", '<p><a href="../printer/plate">Plate</a> <a href="https://wiki.example.org/en/x?y=1">X</a> <a href="/de/z">Z</a> <a href="https://other.example/en/q">Q</a></p>')
        text = extract_page(page, BASE + "/en/guides/hub", "/en/")[1]
        self.assertEqual(text, "Plate (/en/printer/plate) X (/en/x?y=1) Z Q")

    def test_a_page_with_no_article_is_none(self):
        self.assertIsNone(extract_page("<html><body></body></html>"))
        self.assertIsNone(extract_page('<html><body><page title="x"><div>no template</div></page></body></html>'))
        self.assertIsNone(extract_page('<html><body><page title="x"><template slot="contents"><script>x</script></template></page></body></html>'))

    def test_the_sites_name_leaves_the_title(self):
        page = '<html><head><title>Cleaning | Plates | Sample-Wiki</title></head><body><page title=""><template slot="contents"><p>Wash it.</p></template></page></body></html>'
        self.assertEqual(extract_page(page, "", "/en/"), ("Cleaning | Plates", "Wash it."))

    def test_a_huge_page_is_cut_at_a_line_break(self):
        body = "".join(f"<p>{'word ' * 40}{number}</p>" for number in range(2000))
        text = extract_page(page_html("Huge", body), "", "/")[1]
        self.assertLessEqual(len(text), wiki.MAX_PAGE_CHARS)
        self.assertTrue(text.split("\n")[-1].rstrip().split(" ")[-1].isdigit(), "the cut falls between lines")

    def test_a_long_flat_page_reads_in_one_pass(self):
        # Marking each block beside itself scanned its parent's children every time, so a page of many siblings took time that grew
        # with the square of their number.
        # At 32,000 siblings one walk takes a second or two, and a scan of the parent for each one takes half a minute.
        for body in ("<p>a</p>" * 32_000, "<ul>" + "<li>b</li>" * 32_000 + "</ul>", "<div>" + "c<br>" * 32_000 + "</div>",
                     "<table>" + "<tr><td>d</td></tr>" * 32_000 + "</table>"):
            started = time.perf_counter()
            title, text = extract_page(page_html("Long", body), BASE + "/en/long", "/en/")
            self.assertLess(time.perf_counter() - started, 10.0, body[:20])
            self.assertEqual(len(text.split("\n")), 32_000, body[:20])

    def test_a_page_with_too_many_elements_is_left_out(self):
        with mock.patch.object(wiki, "MAX_PAGE_ELEMENTS", 50):
            self.assertIsNone(extract_page(page_html("Big", "<p>x</p>" * 60), BASE + "/en/big", "/en/"))
            self.assertIsNotNone(extract_page(page_html("Small", "<p>x</p>" * 10), BASE + "/en/small", "/en/"))
        self.assertEqual(wiki.MAX_PAGE_ELEMENTS, 100_000)


class SectionsTest(unittest.TestCase):
    def test_sections_follow_headings_and_long_ones_break_at_lines(self):
        content = "Intro line\n## First {#one}\n" + "\n".join("x" * 400 for _ in range(6)) + "\n## Second\nShort"
        sections = split_sections(content)
        self.assertEqual(sections[0], ("", "", "Intro line"))
        first = [s for s in sections if s[0] == "First"]
        self.assertEqual(len(first), 2, "2,400 characters split near 1,500")
        self.assertTrue(all(s[1] == "one" for s in first))
        self.assertEqual(sections[-1], ("Second", "", "Short"))


class SearchTest(unittest.TestCase):
    def index(self, rows, source=SOURCE):
        return WikiIndex(rows, source)

    def test_the_size_phrases_find_each_other(self):
        self.assertIn("specbuildvolume", tokenize("What is the print size?"))
        self.assertIn("specbuildvolume", tokenize("maximum printable area"))
        self.assertEqual(tokenize("The printers are glass and bus"), ["printer", "glass", "bus"])  # stopwords go, and words over four letters lose an s, but not an ss

    def test_a_query_naming_a_model_ranks_that_models_pages_first(self):
        rows = [
            (f"{BASE}/en/general/sizes", "Build volume of printers", "Every printer's build volume is listed on its own page. Build volume matters."),
            (f"{BASE}/en/h2/h2c-faq", "H2C FAQ", "## Specs {#specs}\nThe H2C print size is 325 mm."),
        ]
        hits = self.index(rows).search("H2C build volume")
        self.assertEqual(hits[0].url, f"{BASE}/en/h2/h2c-faq")
        self.assertEqual(hits[0].source, f"{BASE}/en/h2/h2c-faq#specs")
        self.assertEqual(hits[0].title, "H2C FAQ › Specs")

    def test_a_page_gives_at_most_two_sections_and_six_in_all(self):
        rows = [(f"{BASE}/en/p/{n}", f"Nozzle page {n}", "\n".join(f"## Part {i}\nnozzle " * 3 for i in range(5))) for n in range(5)]
        hits = self.index(rows).search("nozzle")
        self.assertEqual(len(hits), 6)
        per_page = {}
        for hit in hits:
            per_page[hit.url] = per_page.get(hit.url, 0) + 1
        self.assertEqual(max(per_page.values()), 2)

    def test_link_paths_dont_make_a_hub_match_everything(self):
        rows = [(f"{BASE}/en/hub", "Hub", "Guides (/en/nozzle/swap) and more"), (f"{BASE}/en/real", "Real", "How to swap a nozzle")]
        self.assertEqual([hit.url for hit in self.index(rows).search("nozzle swap")], [f"{BASE}/en/real"])

    def test_a_page_named_any_way_the_model_writes_it_is_found(self):
        url = f"{BASE}/en/p1/clean"
        index = self.index([(url, "Clean", "text")])
        for written in (url, url + "/", url + "#part", url + "?x=1", "/en/p1/clean", "wiki.example.org/en/p1/clean", url.replace("https", "http")):
            self.assertEqual(index.page_url(written), url, written)
        self.assertIsNone(index.page_url("/en/missing"))

    def test_a_page_addressed_by_its_query_is_found(self):
        # DokuWiki without URL rewriting names every page doku.php?id=..., so the query is the page's name.
        start, page = f"{BASE}/doku.php", f"{BASE}/doku.php?id=maintenance:nozzle"
        index = self.index([(start, "Start", "Welcome. Pick a topic."), (page, "Nozzle", "Swap the nozzle.")], Source(start, kind=CRAWL, prefix="/"))
        for written in (page, page + "#swap", "/doku.php?id=maintenance:nozzle", page.replace("https", "http")):
            self.assertEqual(index.page_url(written), page, written)
        self.assertEqual(index.page_url(start), start)
        self.assertEqual(index.page_url(start + "?do=export_raw"), start, "a query that names no page of its own falls back to the page")

    def test_a_library_ranks_across_its_sources_and_knows_where_each_page_came_from(self):
        docs = Source("https://docs.example.org/", kind=CRAWL)
        wiki_index = self.index([(f"{BASE}/en/plate", "Plate", "Wash the plate with soap.")])
        docs_index = self.index([("https://docs.example.org/nozzle", "Nozzle", "Swap the nozzle when it wears. The nozzle wears with abrasive filament.")], docs)
        library = Library([Shelf("the Example Wiki", wiki_index), Shelf("the Example Docs", docs_index)])
        self.assertEqual([hit.url for hit in library.search("nozzle plate soap")], [f"{BASE}/en/plate", "https://docs.example.org/nozzle"])
        self.assertEqual(library.search("nozzle")[0].url, "https://docs.example.org/nozzle")
        self.assertEqual([hit.url for hit in library.search("nozzle wears soap")], ["https://docs.example.org/nozzle", f"{BASE}/en/plate"],
                         "the better match leads, whichever source it's in")
        self.assertEqual(library.page_url("/nozzle"), "https://docs.example.org/nozzle", "a bare path is tried on each source's site in turn")
        self.assertEqual(library.page_url("https://docs.example.org/nozzle/"), "https://docs.example.org/nozzle")
        self.assertEqual(library.page_sections("https://docs.example.org/nozzle")[0].page_title, "Nozzle")
        self.assertEqual((library.name_of(f"{BASE}/en/plate#x"), library.name_of("https://docs.example.org/nozzle"), library.name_of("https://web.example/x")),
                         ("the Example Wiki", "the Example Docs", None))
        single = Library([Shelf("the Example Wiki", wiki_index)])
        self.assertEqual(single.search("plate"), wiki_index.search("plate"), "one source answers exactly as its own index")


class HostileSitemapTest(unittest.TestCase):
    """A sitemap comes from any site a server names. One whose tags never close mustn't cost time that grows with the square of its size."""

    def test_tags_that_never_close_cost_one_pass(self):
        for tag in ("url", "sitemap"):
            hostile = (f"<{tag}><loc>{BASE}/en/x</loc>" * 40_000)
            started = time.perf_counter()
            pages, children = parse_sitemap(hostile, SOURCE)
            self.assertLess(time.perf_counter() - started, 3.0, tag)
            self.assertEqual((pages, children), ({}, []), tag)

    def test_well_formed_sitemaps_read_as_before(self):
        xml = sitemap(BASE, {"/en/a": "2026-01-01", "/en/b": None}) + "<sitemapindex><sitemap><loc>https://wiki.example.org/s2.xml</loc></sitemap><sitemap></sitemap></sitemapindex>"
        self.assertEqual(parse_sitemap(xml, SOURCE), ({f"{BASE}/en/a": "2026-01-01", f"{BASE}/en/b": None}, ["https://wiki.example.org/s2.xml"]))


class SourceTest(unittest.TestCase):
    def test_only_pages_under_the_path_and_through_the_patterns_are_read(self):
        xml = sitemap(BASE, {"/en/a": "2026-01-01", "/en/admin/x": None, "/en/drafts-2": None, "/de/a": None, "/en/b/": None, "/en/c?x=1&amp;y=2": "d"})
        self.assertEqual(parse_sitemap(xml, SOURCE), ({f"{BASE}/en/a": "2026-01-01", f"{BASE}/en/b": None, f"{BASE}/en/c?x=1&y=2": "d"}, []))
        only = Source(SOURCE.address, prefix="/en/", include=(f"{BASE}/en/a", "/en/c?x=1*"))  # a ? is the character itself
        self.assertEqual(sorted(parse_sitemap(xml, only)[0]), [f"{BASE}/en/a", f"{BASE}/en/c?x=1&y=2"])
        self.assertFalse(SOURCE.wants("https://wiki.example.org.attacker.example/en/a"))

    def test_a_sitemap_index_names_its_children(self):
        xml = '<sitemapindex><sitemap><loc>https://wiki.example.org/sitemap-1.xml</loc></sitemap><sitemap><loc>https://wiki.example.org/sitemap-2.xml</loc></sitemap></sitemapindex>'
        self.assertEqual(parse_sitemap(xml, SOURCE), ({}, ["https://wiki.example.org/sitemap-1.xml", "https://wiki.example.org/sitemap-2.xml"]))

    def test_the_same_site_with_other_paths_is_another_copy(self):
        self.assertNotEqual(SOURCE.key, Source(SOURCE.address, prefix="/de/").key)
        self.assertEqual(SOURCE.key, Source(SOURCE.address, prefix="/en/", exclude=("/en/drafts*", "/en/admin/*")).key)
        self.assertNotEqual(SOURCE.key, Source(SOURCE.address, prefix="/en/", exclude=SOURCE.exclude, selector="main").key)
        self.assertEqual(SOURCE.base, BASE)

    def test_a_crawl_follows_only_its_own_pages(self):
        source = Source("https://docs.example.org/guide/", kind=CRAWL, prefix="/guide/", exclude=("/guide/old/*",))
        page = ('<a href="setup">Setup</a> <a href="/guide/setup#top">Setup again</a> <a href="/guide/old/x">Old</a> <a href="/blog/news">Blog</a> '
                '<a href="https://other.example/guide/a">Other</a> <a href="/guide/manual.pdf">PDF</a> <a href="mailto:x@example.org">Mail</a>')
        self.assertEqual(page_links(page, "https://docs.example.org/guide/", source), ["https://docs.example.org/guide/setup"])

    def test_a_link_urljoin_cant_read_is_skipped(self):
        # A placeholder host in brackets isn't an IP address, so urllib raises ValueError for it.
        page = '<p>Open <a href="http://[printer-ip]/">the printer page</a> or read <a href="setup">setup</a>.</p>'
        self.assertEqual(page_links(page, "https://docs.example.org/guide/", GUIDE), ["https://docs.example.org/guide/setup"])
        title, text = extract_page(page_html("Network", page), "https://docs.example.org/guide/net", "/guide/")
        self.assertEqual(text, "Open the printer page or read setup (/guide/setup).")


class CopyCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(Path(self.folder.name) / "messages.db", "4" * 64)
        await self.db.open()
        await self.db.migrate("wiki", wiki.MIGRATIONS)
        self.copies = Copies(self.db)
        self.web = FakeOutside()
        for name, value in (("FETCH_PAUSE", 0), ("RETRY_WAIT", 0)):
            patcher = mock.patch.object(wiki, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.db.close()
        self.folder.cleanup()


class SitemapCopyTest(CopyCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pages = {"/en/nozzle": "2026-01-01", "/en/plate": "2026-01-02"}
        self.serve()

    def serve(self):
        self.web.answer(SOURCE.address, sitemap(BASE, self.pages))
        self.web.answer(BASE + "/en/nozzle", NOZZLE)
        self.web.answer(BASE + "/en/plate", page_html("Cleaning the Plate", "<p>Wash the plate with dish soap.</p>"))

    def fetched(self, path):
        return len(self.web.called(BASE + path))

    async def test_the_first_update_copies_every_page_and_builds_the_index(self):
        summary = await self.copies.refresh(SOURCE, self.web)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual((summary["updated"], summary["unchanged"], summary["removed"], summary["pages"]), (2, 0, 0, 2))
        self.assertEqual(self.copies.index(SOURCE).search("dish soap")[0].url, BASE + "/en/plate")
        self.assertTrue(all(call["stranger"] for call in self.web.calls), "an address a server typed is fetched as a stranger's link")
        status = await self.copies.status(SOURCE)
        self.assertEqual((status["pages"], status["last_error"], status["loaded"]), (2, None, True))
        self.assertIsNotNone(await self.copies.last_try(SOURCE))

    async def test_a_page_whose_date_is_unchanged_isnt_downloaded_again(self):
        await self.copies.refresh(SOURCE, self.web)
        self.pages["/en/plate"] = "2026-02-01"
        self.serve()
        summary = await self.copies.refresh(SOURCE, self.web)
        self.assertEqual((summary["updated"], summary["unchanged"]), (1, 1))
        self.assertEqual((self.fetched("/en/nozzle"), self.fetched("/en/plate")), (1, 2))
        await self.copies.refresh(SOURCE, self.web, force=True)
        self.assertEqual(self.fetched("/en/nozzle"), 2)

    async def test_a_page_gone_from_the_sitemap_leaves_the_copy(self):
        await self.copies.refresh(SOURCE, self.web)
        self.pages = {"/en/nozzle": "2026-01-01", "/en/new": None}
        self.serve()
        self.web.answer(BASE + "/en/new", page_html("New", "<p>New page</p>"))
        summary = await self.copies.refresh(SOURCE, self.web)
        self.assertEqual(summary["removed"], 1)
        self.assertEqual(sorted(self.copies.index(SOURCE).pages), [BASE + "/en/new", BASE + "/en/nozzle"])

    async def test_a_sitemap_with_under_half_the_pages_removes_nothing(self):
        self.pages = {f"/en/p{n}": None for n in range(6)}
        self.serve()
        for n in range(6):
            self.web.answer(BASE + f"/en/p{n}", page_html(f"P{n}", f"<p>page {n}</p>"))
        await self.copies.refresh(SOURCE, self.web)
        self.pages = {"/en/p0": None, "/en/p1": None}
        self.serve()
        summary = await self.copies.refresh(SOURCE, self.web)
        self.assertEqual(summary["removed"], 0)
        self.assertTrue(summary["kept_on_short_sitemap"])
        self.assertEqual(len(self.copies.index(SOURCE).pages), 6)

    async def test_a_sitemap_index_is_followed(self):
        self.web.answer(SOURCE.address, f'<sitemapindex><sitemap><loc>{BASE}/sitemap-1.xml</loc></sitemap></sitemapindex>')
        self.web.answer(BASE + "/sitemap-1.xml", sitemap(BASE, self.pages))
        summary = await self.copies.refresh(SOURCE, self.web)
        self.assertEqual(summary["pages"], 2)

    async def test_a_sitemap_past_the_cap_on_a_page_is_read_whole(self):
        # A wiki that lists every language in one sitemap runs past the cap on a page. One product wiki's was 9,345,697 bytes unpacked.
        others = "".join(f"<url><loc>{BASE}/de/page-{n}</loc></url>" for n in range(170_000))
        body = sitemap(BASE, self.pages).replace("</urlset>", others + "</urlset>").encode()
        self.assertGreater(len(body), outside_module.DEFAULT_MAX_BYTES)

        def capped(call):
            if len(body) > call.get("max_bytes", outside_module.DEFAULT_MAX_BYTES):
                raise OutsideError("The answer was too large.")  # as Outside._read refuses it
            return Reply(200, body, {"content-type": "text/xml"})
        self.web.answer(SOURCE.address, capped)
        summary = await self.copies.refresh(SOURCE, self.web)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["pages"], 2)
        self.assertEqual(self.web.called(BASE + "/en/nozzle")[0].get("max_bytes"), None, "a page keeps the usual cap")

    async def test_a_child_sitemap_past_the_cap_on_a_page_is_read_whole(self):
        others = "".join(f"<url><loc>{BASE}/de/page-{n}</loc></url>" for n in range(170_000))
        body = sitemap(BASE, self.pages).replace("</urlset>", others + "</urlset>").encode()

        def capped(call):
            if len(body) > call.get("max_bytes", outside_module.DEFAULT_MAX_BYTES):
                raise OutsideError("The answer was too large.")
            return Reply(200, body, {"content-type": "text/xml"})
        self.web.answer(SOURCE.address, f'<sitemapindex><sitemap><loc>{BASE}/sitemap-1.xml</loc></sitemap></sitemapindex>')
        self.web.answer(BASE + "/sitemap-1.xml", capped)
        summary = await self.copies.refresh(SOURCE, self.web)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["pages"], 2)

    async def test_a_page_that_fails_is_counted_and_the_rest_are_kept(self):
        self.web.answer(BASE + "/en/plate", "", status=404)
        summary = await self.copies.refresh(SOURCE, self.web)
        self.assertEqual(summary["failed"], {"http_404": 1})
        self.assertEqual(summary["pages"], 1)

    async def test_a_page_that_cant_be_read_is_counted_and_the_update_goes_on(self):
        real = wiki.extract_page

        def broken(page, url, *args):
            if url.endswith("/en/plate"):
                raise RuntimeError("a reader fault")
            return real(page, url, *args)
        with mock.patch.object(wiki, "extract_page", broken), self.assertLogs("captainguild.core.answers.wiki", level="ERROR"):
            summary = await self.copies.refresh(SOURCE, self.web)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual((summary["failed"], summary["pages"]), ({"error": 1}, 1))
        self.assertIsNotNone(self.copies.index(SOURCE))

    async def test_a_busy_server_is_tried_three_times(self):
        answers = iter([Reply(503), Reply(429), Reply(200, NOZZLE.encode())])
        self.web.answer(BASE + "/en/nozzle", lambda call: next(answers))
        summary = await self.copies.refresh(SOURCE, self.web)
        self.assertEqual(summary["updated"], 2)
        self.assertEqual(self.fetched("/en/nozzle"), 3)

    async def test_a_sitemap_that_cant_be_read_says_why_and_keeps_the_copy(self):
        await self.copies.refresh(SOURCE, self.web)
        self.web.answer(SOURCE.address, OutsideError("That link points to a private address."))
        summary = await self.copies.refresh(SOURCE, self.web)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["error"], "The sitemap couldn't be reached. That link points to a private address.")
        self.assertEqual((await self.copies.status(SOURCE))["last_error"], summary["error"])
        self.assertEqual(len(self.copies.index(SOURCE).pages), 2)
        self.web.answer(SOURCE.address, "", status=404)
        self.assertEqual((await self.copies.refresh(SOURCE, self.web))["error"], "The sitemap answered with HTTP 404.")

    async def test_the_sitemap_is_parsed_off_the_event_loop(self):
        where = []
        real = wiki.parse_sitemap

        def watched(*args):
            where.append(threading.current_thread() is threading.main_thread())
            return real(*args)
        with mock.patch.object(wiki, "parse_sitemap", watched):
            await self.copies.refresh(SOURCE, self.web)
        self.assertEqual(where, [False])

    async def test_updates_take_turns_across_the_host(self):
        gate = asyncio.Event()
        started: list[str] = []
        web = self.web

        class Slow(FakeOutside):
            async def get(inner, url, **kwargs):
                if url.endswith("/sitemap.xml"):
                    started.append(url)
                    await gate.wait()
                return await web.get(url, **kwargs)
        sources = [Source(f"https://wiki{number}.example.org/sitemap.xml", prefix="/en/") for number in range(3)]
        for number in range(3):
            self.web.answer(f"https://wiki{number}.example.org/sitemap.xml", sitemap(f"https://wiki{number}.example.org", {}))
        tasks = [asyncio.ensure_future(self.copies.refresh(source, Slow())) for source in sources]
        for _ in range(500):
            if len(started) >= wiki.UPDATES_AT_ONCE:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        self.assertEqual(len(started), 2, "the third waits its turn")
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(len(started), 3)
        self.assertEqual(wiki.UPDATES_AT_ONCE, 2)

    async def test_an_update_stops_once_no_server_reads_its_source(self):
        asked: list[int] = []

        def wanted() -> bool:
            asked.append(1)
            return len(asked) < 2
        summary = await self.copies.refresh(SOURCE, self.web, wanted=wanted)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["error"], wiki.NOT_WANTED)
        self.assertEqual(self.fetched("/en/nozzle") + self.fetched("/en/plate"), 1, "one page came down before it stopped")

    async def test_a_copy_stops_at_its_character_cap(self):
        with mock.patch.object(wiki, "MAX_COPY_CHARS", 250):  # room for one of the two pages, whichever comes first
            summary = await self.copies.refresh(SOURCE, self.web)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual((summary["pages"], summary["left_out"]), (1, 1))
        self.assertEqual(wiki.MAX_COPY_CHARS, 25_000_000)

    async def test_one_update_at_a_time(self):
        gate = asyncio.Event()
        web = self.web

        class Slow(FakeOutside):
            async def get(inner, url, **kwargs):
                if url == SOURCE.address:
                    await gate.wait()
                return await web.get(url, **kwargs)

        first = asyncio.ensure_future(self.copies.refresh(SOURCE, Slow()))
        await asyncio.sleep(0.05)
        self.assertTrue(self.copies.running(SOURCE))
        self.assertEqual(await asyncio.wait_for(self.copies.refresh(SOURCE, self.web), 5), {"ok": False, "already_running": True})
        gate.set()
        self.assertTrue((await first)["ok"])
        self.assertFalse(self.copies.running(SOURCE))

    async def test_more_pages_than_a_copy_keeps_are_left_out_and_counted(self):
        with mock.patch.object(wiki, "MAX_PAGES", 1):
            summary = await self.copies.refresh(SOURCE, self.web)
        self.assertEqual((summary["left_out"], summary["pages"]), (1, 1))

    async def test_copies_nobody_reads_are_deleted(self):
        await self.copies.refresh(SOURCE, self.web)
        self.assertEqual(await self.copies.drop_unused({SOURCE.key}), 0)
        self.assertEqual(await self.copies.drop_unused(set()), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM wiki_pages"), 0)


DOCS = "https://docs.example.org"
GUIDE = Source(DOCS + "/guide/", kind=CRAWL, prefix="/guide/")


def docs_page(title: str, body: str) -> str:
    return site_page(f"<title>{title} | Example Docs</title>", f'<main><h1>{title}</h1>{body}</main><nav><a href="/guide/">Guide</a><a href="/guide/setup">Setup</a></nav>')


class CrawlCopyTest(CopyCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The newest answer whose address starts the request wins, so the start page, whose address starts every other, goes first.
        self.web.answer(DOCS + "/robots.txt", "User-agent: *\nDisallow: /guide/private\n")
        self.web.answer(DOCS + "/guide", docs_page("Guide", '<p>Start with <a href="setup">setup</a>.</p><a href="/guide/logo.png">Logo</a> <a href="/guide/data">Data</a>'))
        self.web.answer(DOCS + "/guide/setup", docs_page("Setup", '<p>Install the slicer.</p><a href="private/keys">Keys</a> <a href="calibration">Calibration</a>'), headers={"Content-Type": "text/html; charset=utf-8"})
        self.web.answer(DOCS + "/guide/calibration", docs_page("Calibration", "<p>Run the flow test.</p>"))
        self.web.answer(DOCS + "/guide/private", docs_page("Keys", "<p>Secret.</p>"))
        self.web.answer(DOCS + "/guide/data", "{}", headers={"Content-Type": "application/json"})

    async def test_fetched_text_is_parsed_off_the_event_loop(self):
        where = []

        def watch(name, real):
            def wrapped(*args):
                where.append((name, threading.current_thread() is threading.main_thread()))
                return real(*args)
            return wrapped
        with mock.patch.object(wiki, "page_links", watch("page_links", wiki.page_links)), mock.patch.object(wiki, "robot_rules", watch("robot_rules", wiki.robot_rules)):
            await self.copies.refresh(GUIDE, self.web)
        self.assertEqual({name for name, _ in where}, {"page_links", "robot_rules"})
        self.assertFalse(any(on_loop for _, on_loop in where), where)

    async def test_a_crawl_copies_the_pages_its_links_reach(self):
        summary = await self.copies.refresh(GUIDE, self.web)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(sorted(self.copies.index(GUIDE).pages), [DOCS + "/guide", DOCS + "/guide/calibration", DOCS + "/guide/setup"])
        self.assertEqual(summary["failed"], {"robots": 1, "not_a_page": 1})
        self.assertEqual(self.web.called(DOCS + "/guide/private"), [], "robots.txt keeps the bot out")
        self.assertEqual(self.web.called(DOCS + "/guide/logo.png"), [])
        self.assertEqual(self.copies.index(GUIDE).search("flow test")[0].page_title, "Calibration")

    async def test_a_crawled_page_that_cant_be_read_is_counted_and_the_crawl_goes_on(self):
        real = wiki.page_links

        def broken(page, url, source):
            if url.endswith("/guide/setup"):
                raise RuntimeError("a reader fault")
            return real(page, url, source)
        with mock.patch.object(wiki, "page_links", broken), self.assertLogs("captainguild.core.answers.wiki", level="ERROR"):
            summary = await self.copies.refresh(GUIDE, self.web)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["failed"]["error"], 1)
        self.assertIn(DOCS + "/guide", self.copies.index(GUIDE).pages)

    async def test_a_crawl_stops_at_the_copys_character_cap(self):
        with mock.patch.object(wiki, "MAX_COPY_CHARS", 100):  # room for the start page only
            summary = await self.copies.refresh(GUIDE, self.web)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(sorted(self.copies.index(GUIDE).pages), [DOCS + "/guide"])
        self.assertGreaterEqual(summary["left_out"], 1)

    async def test_a_second_crawl_counts_what_didnt_change_and_removes_what_went(self):
        await self.copies.refresh(GUIDE, self.web)
        self.web.answer(DOCS + "/guide/calibration", docs_page("Calibration", "<p>Run the flow test twice.</p>"))
        summary = await self.copies.refresh(GUIDE, self.web)
        self.assertEqual((summary["updated"], summary["unchanged"], summary["removed"]), (1, 2, 0))
        self.web.answer(DOCS + "/guide/setup", docs_page("Setup", "<p>Install the slicer.</p>"))
        summary = await self.copies.refresh(GUIDE, self.web)
        self.assertEqual(summary["removed"], 1)

    async def test_a_start_page_that_cant_be_read_says_so(self):
        self.web.answer(DOCS + "/guide", "", status=404)
        summary = await self.copies.refresh(GUIDE, self.web)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["error"], "The start page answered with HTTP 404.")

    async def test_a_crawl_stops_at_what_a_copy_keeps(self):
        with mock.patch.object(wiki, "MAX_PAGES", 2):
            summary = await self.copies.refresh(GUIDE, self.web)
        self.assertEqual(summary["pages"], 2)
        self.assertGreater(summary["left_out"], 0)


if __name__ == "__main__":
    unittest.main()
