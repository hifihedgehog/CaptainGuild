"""The sources answers come from: wikis and documentation sites, copied to this machine and searched in memory.

A source finds its pages one of two ways. With a sitemap, each page under the chosen path is downloaded once, and again only when the
sitemap's date for it changes. With a start page, the bot follows the links it finds there that stay on the site and under the path,
the way a reader would, honoring the site's robots.txt. Optional patterns take addresses in or leave them out. Each page is reduced to
text with its headings kept, cut into sections of about 1,500 characters, and ranked with BM25. Servers that name the same source with
the same paths share one copy.

Reading a page is a small adapter: the markup of Wiki.js, MediaWiki, DokuWiki, Confluence, MkDocs, Docusaurus and GitBook each tells
where the article is, a source can name it with a CSS selector instead, and a page none of those fit gives up its largest block of text.

Addresses come from a server's settings, so every request goes out the way a link a member gave does: public addresses only, and each
redirect checked again.
"""
from __future__ import annotations

import asyncio
import functools
import html as html_text
import json
import logging
import math
import re
import time
import urllib.robotparser
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Iterator
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup, CData, NavigableString

from ..outside import OutsideError

log = logging.getLogger(__name__)

SECTION_TARGET_CHARS = 1500
SEARCH_RESULTS = 6
RESULTS_PER_PAGE = 2
PAGE_CHAR_LIMIT = 40000  # the most of one page read_wiki_page hands the model
FETCH_WORKERS = 2
FETCH_PAUSE = 0.5  # seconds each worker waits between pages
FETCH_RETRIES = 3
RETRY_WAIT = 2.0  # seconds before the second try, doubled before the third
FETCH_SECONDS = 60.0
COMMIT_EVERY = 50
SITEMAPS_PER_INDEX = 50  # the child sitemaps one sitemap index can send the bot to
SITEMAP_MAX_BYTES = 52_428_800  # the sitemap protocol's limit for one file, uncompressed (sitemaps.org). A wiki that lists every language passes the cap on a page.
ROBOTS_AGENT = "CaptainGuild"
# Bounds on what one source can make this machine keep. A product wiki runs to a few thousand pages.
MAX_PAGES = 5000
MAX_COPY_CHARS = 25_000_000  # characters of text one copy keeps. Its index holds about ten bytes in memory for each.
UPDATES_AT_ONCE = 2  # updates that run at the same time across every server, so a burst of new sources downloads in turn
NOT_WANTED = "No server reads this source now, so its update stopped."
MAX_PAGE_CHARS = 200_000
MAX_PAGE_ELEMENTS = 100_000  # a page with more elements is left out, which bounds the work of reading one
SITEMAP = "sitemap"
CRAWL = "crawl"
# Addresses a crawl never opens, since they aren't pages.
_NOT_PAGES = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|ico|bmp|pdf|zip|gz|tgz|7z|rar|exe|dmg|msi|mp4|webm|mov|mp3|wav|ogg|css|js|json|xml|txt|stl|3mf|step|gcode)$", re.I)

MIGRATIONS = [
    """
    CREATE TABLE wiki_copies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL UNIQUE,
        refreshed_at REAL,
        attempted_at REAL,
        last_error TEXT,
        summary TEXT
    );
    CREATE TABLE wiki_pages (
        copy_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        lastmod TEXT,
        fetched_at REAL NOT NULL,
        PRIMARY KEY (copy_id, url)
    );
    """,
]


@functools.lru_cache(maxsize=512)
def _pattern(pattern: str) -> re.Pattern:
    """* stands for any run of characters, and every other character for itself, so an address pasted with ? or [ in it still reads as written."""
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")), re.S)


def _matches(url: str, pattern: str) -> bool:
    """A pattern starting with a slash reads the address's path and query, and any other the whole address."""
    if pattern.startswith("/"):
        parts = urlsplit(url)
        url = parts.path + (f"?{parts.query}" if parts.query else "")
    return _pattern(pattern).fullmatch(url) is not None


@dataclass(frozen=True)
class Source:
    """Where a copy's pages come from, and which of them it keeps."""
    address: str  # the sitemap, or the page a crawl starts from
    kind: str = SITEMAP
    prefix: str = "/"  # the path every page sits under
    include: tuple[str, ...] = ()  # with any, an address has to match one
    exclude: tuple[str, ...] = ()
    selector: str = ""  # the CSS selector of the article, when the page's own markup doesn't say

    def __post_init__(self) -> None:
        # The order patterns are listed in changes nothing they match, so it changes nothing about which copy this is.
        object.__setattr__(self, "include", tuple(sorted(self.include)))
        object.__setattr__(self, "exclude", tuple(sorted(self.exclude)))

    @classmethod
    def of(cls, row: dict) -> "Source":
        return cls(row["address"], row.get("kind") or SITEMAP, row.get("prefix") or "/", tuple(row.get("include") or ()), tuple(row.get("exclude") or ()),
                   row.get("selector") or "")

    @property
    def key(self) -> str:
        return json.dumps([self.address, self.kind, self.prefix, list(self.include), list(self.exclude), self.selector])

    @property
    def base(self) -> str:
        """The site's address with no path, such as https://example.com."""
        parts = urlsplit(self.address)
        return f"{parts.scheme}://{parts.netloc}"

    def wants(self, url: str) -> bool:
        """Whether a page at this address belongs in the copy: on the same site, under the path, and through the patterns."""
        if not url.startswith(self.base) or not url[len(self.base):].startswith(self.prefix):
            return False
        if self.include and not any(_matches(url, pattern) for pattern in self.include):
            return False
        return not any(_matches(url, pattern) for pattern in self.exclude)


# ---- reading a page ----

# Marks a line break while whitespace inside the page text is collapsed. A private-use character, so the whitespace pass leaves it alone.
_LINE = "\ue000"
_BLOCK_TAGS = ["p", "div", "blockquote", "dt", "dd", "figcaption", "ul", "ol"]
_HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
_HEADING_LINE = re.compile(r"^## (.*?)(?: \{#([^}]*)\})?$")
_JUNK = ["script", "style", "nav", "footer", "aside", "header", "img", "svg"]
# What stands between a page's name and its site's in a <title>: a bar, a dash of any length, a middle dot or a guillemet, with spaces.
_TITLE_SEPARATOR = re.compile(r"\s+(?:\||-|\u2013|\u2014|\u00b7|\u00bb)\s+")


def _without_site(title: str) -> str:
    """A <title> without the site's name after the page's."""
    found = list(_TITLE_SEPARATOR.finditer(title))
    return title[:found[-1].start()] if found else title


@dataclass(frozen=True)
class Engine:
    """How one kind of site marks its article: where it is, where the page's own title is, and what inside it isn't the article."""
    name: str
    content: tuple[str, ...]  # CSS selectors, tried in order
    title: tuple[str, ...] = ()
    junk: tuple[str, ...] = ()
    generator: str = ""  # what the site's <meta name="generator"> starts with, when that's how it's known


ENGINES = (
    Engine("MediaWiki", ("#mw-content-text .mw-parser-output", "#mw-content-text"), ("#firstHeading",),
           (".mw-editsection", "#toc", ".toc", ".navbox", ".printfooter", ".catlinks", ".mw-jump-link")),
    Engine("DokuWiki", ("#dokuwiki__content .page", ".dokuwiki .page"), (), (".secedit", "#dw__toc", ".docInfo")),
    Engine("Confluence", ("#main-content.wiki-content", ".wiki-content"), ("#title-text",), (".confluence-information-macro-icon",)),
    Engine("MkDocs", ("article.md-content__inner", "[itemprop=articleBody]", ".rst-content .document", "div[role=main]"), (), (".headerlink", ".md-source-file", ".md-content__button"), "mkdocs"),
    Engine("Docusaurus", (".theme-doc-markdown", "article .markdown"), (), (".hash-link", ".theme-doc-footer", ".pagination-nav"), "docusaurus"),
    Engine("GitBook", (".markdown-section", "main"), (), (), "gitbook"),
)


def _generator(soup: Any) -> str:
    tag = soup.find("meta", attrs={"name": "generator"})
    return str(tag.get("content") or "").lower() if tag else ""


# The strings a page's text is made of. Comments, scripts, styles and template strings are other kinds, as in BeautifulSoup's get_text().
_TEXT_TYPES = (NavigableString, CData)
_END = object()


def _strings(node: Any, skip: set[int], notes: dict[int, str]) -> Iterator[str]:
    """The text under a node in document order, leaving out the elements skip holds, with each link's note right after the link. A walk
    with its own stack, so a deeply nested page can't run out of recursion."""
    stack = [iter(node.contents)]
    after: list[str] = [""]
    while stack:
        child = next(stack[-1], _END)
        if child is _END:
            stack.pop()
            note = after.pop()
            if note:
                yield note
            continue
        if isinstance(child, NavigableString):
            if type(child) in _TEXT_TYPES:
                yield str(child)
        elif id(child) not in skip:
            stack.append(iter(child.contents))
            after.append(notes.get(id(child), ""))


def _words(node: Any, skip: set[int], notes: dict[int, str] | None = None) -> str:
    """The node's text the way get_text(" ", strip=True) gives it."""
    return " ".join(piece for piece in (string.strip() for string in _strings(node, skip, notes or {})) if piece)


def _first_with_id(node: Any, skip: set[int]) -> Any:
    """The node itself or the first element under it with an id, outside what skip holds."""
    if node.get("id"):
        return node
    stack = [iter(node.contents)]
    while stack:
        child = next(stack[-1], _END)
        if child is _END:
            stack.pop()
        elif not isinstance(child, NavigableString) and id(child) not in skip:
            if child.get("id"):
                return child
            stack.append(iter(child.contents))
    return None


def _measure(soup: Any) -> tuple[dict[int, int], set[int]]:
    """For every element, the characters of stripped text under it, and which elements sit inside junk. One walk over the tree."""
    sizes: dict[int, int] = {}
    in_junk: set[int] = set()
    stack: list[tuple[Any, Any, bool]] = [(soup, iter(soup.contents), False)]
    totals = [0]
    while stack:
        node, children, junk = stack[-1]
        child = next(children, _END)
        if child is _END:
            stack.pop()
            size = totals.pop()
            sizes[id(node)] = size
            if totals:
                totals[-1] += size
            continue
        if isinstance(child, NavigableString):
            if type(child) in _TEXT_TYPES:
                totals[-1] += len(child.strip())
            continue
        inside = junk or child.name in _JUNK
        if junk:
            in_junk.add(id(child))
        stack.append((child, iter(child.contents), inside))
        totals.append(0)
    return sizes, in_junk


def _largest_block(soup: Any, sizes: dict[int, int], in_junk: set[int]) -> Any:
    """The element holding the most running text, for a page no adapter knows: each paragraph, list item or cell counts in full for
    the block around it and by half for the one around that."""
    scores: dict[int, float] = defaultdict(float)
    blocks: dict[int, Any] = {}
    for node in soup.find_all(["p", "li", "pre", "td", "dd", "blockquote"]):
        if id(node) in in_junk:
            continue
        length = sizes.get(id(node), 0)
        parent = node.parent
        for weight in (1.0, 0.5):
            if parent is None or parent.name in (None, "[document]", "html"):
                break
            scores[id(parent)] += length * weight
            blocks[id(parent)] = parent
            parent = parent.parent
    if not scores:
        return soup.body or soup
    return blocks[max(scores, key=scores.get)]


def _article(soup: Any, selector: str) -> tuple[Any, Engine | None, str]:
    """(the article's element, the engine that found it, the page's own title)."""
    if selector:
        found = soup.select_one(selector)
        if found is not None:
            return found, None, ""
    generator = _generator(soup)
    for engine in ENGINES:
        if engine.generator and not generator.startswith(engine.generator) and not any(soup.select_one(css) for css in engine.content[:1]):
            continue
        for css in engine.content:
            found = soup.select_one(css)
            if found is not None and found.get_text(strip=True):
                title = next((tag.get_text(" ", strip=True) for css_title in engine.title for tag in [soup.select_one(css_title)] if tag), "")
                return found, engine, title
    sizes, in_junk = _measure(soup)
    articles = sorted(soup.find_all("article"), key=lambda tag: sizes.get(id(tag), 0), reverse=True)
    for found in (*articles[:1], soup.find("main"), soup.select_one("[role=main]")):
        if found is not None and sizes.get(id(found), 0):
            return found, None, ""
    return _largest_block(soup, sizes, in_junk), None, ""


def _text_lines(fragment: str, page_url: str, prefix: str, junk: Iterable[str] = ()) -> list[str]:
    """An article's HTML as lines: "## Heading {#anchor}", " | "-joined table rows, bulleted list items, and a link to another page under
    the source's path followed by that path in brackets, so the model can open a page a hub page lists.

    One walk over the tree, with nothing inserted beside an element: BeautifulSoup finds an element's place by scanning its parent's
    children, so marking thousands of siblings one at a time took time that grew with the square of their number."""
    main = BeautifulSoup(fragment, "html.parser")
    skip = {id(tag) for tag in main.find_all(_JUNK)}
    skip.update(id(tag) for css in junk for tag in main.select(css))
    skip.update(id(tag) for tag in main.find_all("a", class_="toc-anchor"))
    here = urlsplit(page_url)
    notes: dict[int, str] = {}
    for link in main.find_all("a", href=True):
        try:
            target = urlsplit(urljoin(page_url, link["href"]))
        except ValueError:
            continue  # an address urljoin can't read, such as a host in brackets that isn't an IP address
        if target.netloc == here.netloc and target.path.startswith(prefix) and _words(link, skip):
            notes[id(link)] = f" ({target.path}{'?' + target.query if target.query else ''})"
    out: list[str] = []
    stack = [iter(main.contents)]
    closers: list[str] = [""]
    while stack:
        child = next(stack[-1], _END)
        if child is _END:
            stack.pop()
            out.append(closers.pop())
            continue
        if isinstance(child, NavigableString):
            if type(child) in _TEXT_TYPES:
                out.append(str(child))
            continue
        if id(child) in skip:
            continue
        name = child.name
        if name == "pre":
            lines = [line.strip() for line in "".join(_strings(child, skip, notes)).splitlines() if line.strip()]
            out.append(_LINE + _LINE.join(lines) + _LINE)
        elif name == "table":
            rows = []
            for row in child.find_all("tr"):
                cells = [_words(cell, skip, notes) for cell in row.find_all(["th", "td"])]
                if any(cells):
                    rows.append(" | ".join(cells))
            out.append(_LINE + _LINE.join(rows) + _LINE)
        elif name in _HEADING_TAGS:
            words = _words(child, skip, notes)
            named = _first_with_id(child, skip)
            anchor = named.get("id") if named is not None else ""
            out.append(_LINE + (f"## {words} {{#{anchor}}}" if anchor else f"## {words}") + _LINE)
        elif name == "br":
            out.append(_LINE)
        else:
            opener, closer = (_LINE + "• ", _LINE) if name == "li" else (_LINE, _LINE) if name in _BLOCK_TAGS else ("", "")
            out.append(opener)
            stack.append(iter(child.contents))
            closers.append(closer + notes.get(id(child), ""))
    raw = re.sub(r"\s+", " ", "".join(out))
    lines = [line.strip() for line in raw.split(_LINE)]
    return [line for line in lines if line and line != "•"]


def extract_page(page_html: str, url: str = "", prefix: str = "/", selector: str = "") -> tuple[str, str] | None:
    """(title, text) for a page, or None when it holds no article or more than MAX_PAGE_ELEMENTS elements."""
    soup = BeautifulSoup(page_html, "html.parser")
    if sum(1 for _ in soup.find_all(True)) > MAX_PAGE_ELEMENTS:
        return None
    page = soup.find("page")
    template = page.find("template", attrs={"slot": "contents"}) if page is not None and not selector else None
    if template is not None:
        # Wiki.js. BeautifulSoup marks text inside <template> as a TemplateString, and get_text() on the template skips the plain
        # strings put in later, so the contents are parsed on their own.
        title = (page.get("title") or "").strip()
        lines = _text_lines(template.decode_contents(), url, prefix)
    elif page is not None and not selector:
        return None  # a Wiki.js page whose article is missing
    else:
        found, engine, title = _article(soup, selector)
        if found is None:
            return None
        junk = engine.junk if engine else ()
        if not title:
            heading = found.find("h1")
            # The engine's own marks inside the heading, such as a link mark, stay out of the title.
            skip = {id(tag) for css in junk for tag in found.select(css)}
            title = _words(heading, skip) if heading is not None else ""
        lines = _text_lines(found.decode_contents(), url, prefix, junk)
    if not title:
        tag = soup.find("title")
        title = _without_site(tag.get_text(strip=True)) if tag else ""
    if not lines:
        return None
    return title or "Untitled", _bounded("\n".join(lines))


def _bounded(content: str) -> str:
    """The page's text up to MAX_PAGE_CHARS, cut at a line break."""
    if len(content) <= MAX_PAGE_CHARS:
        return content
    cut = content[:MAX_PAGE_CHARS]
    return cut[:cut.rfind("\n")] if "\n" in cut else cut


def split_sections(content: str) -> list[tuple[str, str, str]]:
    """Stored page text as (heading, anchor, text) sections of about SECTION_TARGET_CHARS, a long section broken at line breaks."""
    sections: list[tuple[str, str, str]] = []
    heading, anchor, buffer = "", "", []

    def flush() -> None:
        piece: list[str] = []
        size = 0
        for line in buffer:
            if piece and size + len(line) > SECTION_TARGET_CHARS:
                sections.append((heading, anchor, "\n".join(piece)))
                piece, size = [], 0
            piece.append(line)
            size += len(line) + 1
        if piece:
            sections.append((heading, anchor, "\n".join(piece)))

    for line in content.split("\n"):
        found = _HEADING_LINE.match(line)
        if found:
            flush()
            heading, anchor, buffer = found.group(1), found.group(2) or "", []
        else:
            buffer.append(line)
    flush()
    return sections


# ---- searching ----

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be but by can do does for from how i if in into is it its me my of on "
    "or our so that the their them then there these this to was what when where which who why "
    "will with you your".split()
)
# A model token: a family like a1 or x1, or a code like p1s or x1c.
_MODEL_TOKEN = re.compile(r"^[a-z]\d[a-z]?$")
BM25_K1 = 1.2
BM25_B = 0.75
# When a query names a model, sections that mention neither the model nor its family score this much of what they would.
MODEL_MISS_PENALTY = 0.4
# A wiki names a printer's size several ways: "print size" in a comparison table, "printable area" on one page, "build volume" on
# another. Each phrase also indexes as one shared token, so a search for any of them finds the others.
_SIZE_PHRASE = re.compile(r"\b(?:build|print|printing|printable|max(?:imum)? print(?:able)?)\s+(?:volume|size|area|range|dimensions?)\b")
_SIZE_TOKEN = "specbuildvolume"


def tokenize(text: str) -> list[str]:
    text = _SIZE_PHRASE.sub(lambda match: f"{match.group(0)} {_SIZE_TOKEN}", text.lower())
    tokens = []
    for token in _TOKEN.findall(text):
        if token in _STOPWORDS:
            continue
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        tokens.append(token)
    return tokens


@dataclass
class Section:
    url: str
    page_title: str
    heading: str
    anchor: str
    text: str

    @property
    def source(self) -> str:
        # Some anchors carry emoji, which break Discord links.
        return f"{self.url}#{quote(self.anchor, safe='-_.~')}" if self.anchor else self.url

    @property
    def title(self) -> str:
        return f"{self.page_title} › {self.heading}" if self.heading else self.page_title


class WikiIndex:
    """BM25 over one copy's sections. Built in a worker thread, since a few thousand pages take a few seconds."""

    def __init__(self, pages: Any, source: Source) -> None:
        """pages: (url, title, content) rows."""
        self.source = source
        self.pages: dict[str, tuple[str, list[int]]] = {}  # url -> (title, its sections' numbers in order)
        self.sections: list[Section] = []
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        lengths: list[int] = []
        # Link paths would make hub pages match nearly everything they link to.
        link_path = re.compile(r" \(" + re.escape(source.prefix) + r"[^)\s]*\)")
        start = source.base + source.prefix
        for url, title, content in pages:
            path = url[len(start):] if url.startswith(start) else urlsplit(url).path
            page_tokens = tokenize(title) + tokenize(re.sub(r"[-_/]", " ", path))
            numbers = []
            for heading, anchor, text in split_sections(content):
                counts = Counter(tokenize(link_path.sub("", text)))
                for token in tokenize(heading):
                    counts[token] += 2
                for token in page_tokens:
                    counts[token] += 3
                number = len(self.sections)
                self.sections.append(Section(url, title, heading, anchor, text))
                numbers.append(number)
                for token, count in counts.items():
                    postings[token].append((number, count))
                lengths.append(sum(counts.values()))
            self.pages[url] = (title, numbers)
        self.postings = dict(postings)
        self.lengths = lengths
        self.average = (sum(lengths) / len(lengths)) if lengths else 1.0
        total = len(self.sections)
        self.idf = {token: math.log(1 + (total - len(found) + 0.5) / (len(found) + 0.5)) for token, found in self.postings.items()}

    def ranked(self, query: str) -> list[tuple[float, Section]]:
        """Every section that matches, best first."""
        terms = set(tokenize(query))
        models = {token for token in terms if _MODEL_TOKEN.match(token)}
        terms |= {token[:2] for token in models}  # p1s also matches the P1 family's pages
        scores: dict[int, float] = defaultdict(float)
        for term in terms:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for number, count in self.postings[term]:
                norm = BM25_K1 * (1 - BM25_B + BM25_B * self.lengths[number] / self.average)
                scores[number] += idf * count * (BM25_K1 + 1) / (count + norm)
        if models:
            # A query that names a model is about that model. Without this, a general page whose title says "build volume" ranks above
            # the only sections that give that model's size.
            relevant: set[int] = set()
            for token in models | {token[:2] for token in models}:
                relevant.update(number for number, _ in self.postings.get(token, ()))
            for number in scores:
                if number not in relevant:
                    scores[number] *= MODEL_MISS_PENALTY
        return [(scores[number], self.sections[number]) for number in sorted(scores, key=scores.get, reverse=True)]

    def search(self, query: str, limit: int = SEARCH_RESULTS, per_page: int = RESULTS_PER_PAGE) -> list[Section]:
        return pick(self.ranked(query), limit, per_page)

    def page_sections(self, url: str) -> list[Section]:
        return [self.sections[number] for number in self.pages[url][1]]

    def page_url(self, url: str) -> str | None:
        """The stored page a URL or path the model passes back names, or None. A query can be a page's name, as DokuWiki's doku.php?id=
        is, so the address is looked up with its query first and without it only when that misses."""
        url = url.strip().split("#", 1)[0]
        host = urlsplit(self.source.base).netloc
        if url.startswith("/"):
            url = self.source.base + url
        elif host and url.startswith(host + "/"):
            url = urlsplit(self.source.base).scheme + "://" + url
        for written in (url.rstrip("/"), url.split("?", 1)[0].rstrip("/")):
            for found in (written, _other_scheme(written)):
                if found in self.pages:
                    return found
        return None


def pick(ranked: Iterable[tuple[float, Section]], limit: int, per_page: int) -> list[Section]:
    """The best sections, no more than per_page from one page."""
    picked: list[Section] = []
    per_url: Counter = Counter()
    for _, section in ranked:
        if per_url[section.url] >= per_page:
            continue
        per_url[section.url] += 1
        picked.append(section)
        if len(picked) >= limit:
            break
    return picked


def _other_scheme(url: str) -> str:
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    return url


@dataclass
class Shelf:
    name: str  # as it reads in a sentence, such as "the Example Wiki"
    index: WikiIndex


@dataclass
class Library:
    """One server's sources, searched together. With one source it answers exactly as that source's index does."""
    shelves: list[Shelf] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [shelf.name for shelf in self.shelves]

    def search(self, query: str, limit: int = SEARCH_RESULTS, per_page: int = RESULTS_PER_PAGE) -> list[Section]:
        if len(self.shelves) == 1:
            return self.shelves[0].index.search(query, limit, per_page)
        merged = []
        for order, shelf in enumerate(self.shelves):
            merged += [(-score, order, position, section) for position, (score, section) in enumerate(shelf.index.ranked(query))]
        merged.sort(key=lambda row: row[:3])
        return pick(((-row[0], row[3]) for row in merged), limit, per_page)

    def page_url(self, url: str) -> str | None:
        for shelf in self.shelves:
            found = shelf.index.page_url(url)
            if found is not None:
                return found
        return None

    def page_sections(self, url: str) -> list[Section]:
        for shelf in self.shelves:
            if url in shelf.index.pages:
                return shelf.index.page_sections(url)
        return []

    def name_of(self, url: str) -> str | None:
        """The name of the source a page or a section of one comes from, or None for an address outside them, such as a web page."""
        page = url.split("#", 1)[0]
        return next((shelf.name for shelf in self.shelves if page in shelf.index.pages), None)


# ---- keeping a copy up to date ----

def _blocks(text: str, tag: str) -> list[str]:
    """The text inside each <tag>...</tag>, in order. One pass over the text, so a sitemap whose tags never close costs no more than its
    length, where a lazy regex would scan to the end again from each opening tag."""
    opening, closing = f"<{tag}>", f"</{tag}>"
    found = []
    start = text.find(opening)
    while start != -1:
        start += len(opening)
        end = text.find(closing, start)
        if end == -1:
            break
        found.append(text[start:end])
        start = text.find(opening, end + len(closing))
    return found


def parse_sitemap(xml_text: str, source: Source) -> tuple[dict[str, str | None], list[str]]:
    """(each page the sitemap lists that the source wants, with its lastmod; the child sitemaps a sitemap index names). Slow on a big
    sitemap, so the updates run it in a worker thread."""
    pages: dict[str, str | None] = {}
    for entry in _blocks(xml_text, "url"):
        location = re.search(r"<loc>\s*([^<]+?)\s*</loc>", entry)
        if not location:
            continue
        url = html_text.unescape(location.group(1)).rstrip("/")
        if not source.wants(url):
            continue
        changed = re.search(r"<lastmod>\s*([^<]+?)\s*</lastmod>", entry)
        pages[url] = changed.group(1) if changed else None
    children = []
    for entry in _blocks(xml_text, "sitemap"):
        location = re.search(r"<loc>\s*([^<]+?)\s*</loc>", entry)
        if location:
            children.append(html_text.unescape(location.group(1).strip()))
    return pages, children


@dataclass
class Fetched:
    status: int | None  # None when nothing answered
    body: str | None = None
    problem: str = ""  # why nothing answered, in words safe to show
    html: bool = True  # whether the answer says it's a web page

    def failure(self, what: str) -> str:
        """What went wrong, as sentences."""
        if self.status:
            return f"{what} answered with HTTP {self.status}."
        return f"{what} couldn't be reached." + (f" {self.problem}" if self.problem else "")


async def fetch(outside: Any, url: str, *, max_bytes: int | None = None) -> Fetched:
    """A GET, tried again on 429, a server error or no answer. max_bytes raises the cap on the answer's size, which is the usual one without it."""
    got = Fetched(None)
    larger = {"max_bytes": max_bytes} if max_bytes else {}
    for attempt in range(FETCH_RETRIES):
        try:
            reply = await outside.get(url, timeout=FETCH_SECONDS, stranger=True, **larger)
            got = Fetched(reply.status)
            if reply.status == 200:
                kind = (reply.header("content-type") or "").lower()
                return Fetched(200, reply.text(), html=not kind or "html" in kind)
            if reply.status != 429 and reply.status < 500:
                return got
        except OutsideError as error:
            got = Fetched(None, problem=str(error))
        if attempt + 1 < FETCH_RETRIES:
            await asyncio.sleep(RETRY_WAIT * 2 ** attempt)
    return got


def robot_rules(text: str) -> urllib.robotparser.RobotFileParser:
    """A site's robots.txt, read. The updates run it in a worker thread, since the file comes from the site."""
    rules = urllib.robotparser.RobotFileParser()
    rules.parse(text.splitlines())
    return rules


def same_page(url: str) -> str:
    """One spelling for each page, without its fragment or a trailing slash, as sitemaps list them and the copy keeps them."""
    return url.split("#", 1)[0].rstrip("/")


def page_links(page_html: str, page_url: str, source: Source) -> list[str]:
    """The pages a page links to that the source wants, in the order they appear, as the links spell them."""
    found: list[str] = []
    named: set[str] = set()
    for link in BeautifulSoup(page_html, "html.parser").find_all("a", href=True):
        try:
            target = urljoin(page_url, link["href"]).split("#", 1)[0]
            path = urlsplit(target).path
        except ValueError:
            continue  # an address urljoin can't read, such as a host in brackets that isn't an IP address
        if (target.startswith(("http://", "https://")) and not _NOT_PAGES.search(path) and source.wants(target)
                and same_page(target) not in named):
            named.add(same_page(target))
            found.append(target)
    return found


class Copies:
    """Every copy on this machine, in the database, and the search index of each one in use."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self.indexes: dict[str, WikiIndex] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._turns = asyncio.Semaphore(UPDATES_AT_ONCE)

    def index(self, source: Source | None) -> WikiIndex | None:
        found = self.indexes.get(source.key) if source is not None else None
        return found if found is not None and found.sections else None

    def running(self, source: Source) -> bool:
        return self._running(source.key)

    def _running(self, key: str) -> bool:
        lock = self._locks.get(key)
        return lock is not None and lock.locked()

    async def _copy_id(self, source: Source, *, create: bool = False) -> int | None:
        found = await self.db.scalar("SELECT id FROM wiki_copies WHERE source = ?", (source.key,))
        if found is None and create:
            await self.db.run("INSERT INTO wiki_copies (source) VALUES (?) ON CONFLICT (source) DO NOTHING", (source.key,))
            found = await self.db.scalar("SELECT id FROM wiki_copies WHERE source = ?", (source.key,))
        return found

    async def load(self, source: Source) -> None:
        """Build the search index from the stored copy."""
        copy_id = await self._copy_id(source)
        rows = await self.db.all("SELECT url, title, content FROM wiki_pages WHERE copy_id = ? ORDER BY url", (copy_id,)) if copy_id is not None else []
        built = await asyncio.to_thread(WikiIndex, [(row["url"], row["title"], row["content"]) for row in rows], source)
        self.indexes[source.key] = built

    def forget(self, keep: set[str]) -> None:
        """Let go of the indexes of copies no server uses now."""
        for key in [key for key in self.indexes if key not in keep]:
            del self.indexes[key]

    async def drop_unused(self, keep: set[str]) -> int:
        """Delete the stored copies no server uses now. Returns how many went."""
        gone = [row for row in await self.db.all("SELECT id, source FROM wiki_copies") if row["source"] not in keep and not self._running(row["source"])]
        for row in gone:
            await self.db.run("DELETE FROM wiki_pages WHERE copy_id = ?", (row["id"],))
            await self.db.run("DELETE FROM wiki_copies WHERE id = ?", (row["id"],))
        return len(gone)

    async def last_try(self, source: Source) -> float | None:
        """When the copy was last updated or last tried, whichever is later."""
        row = await self.db.one("SELECT refreshed_at, attempted_at FROM wiki_copies WHERE source = ?", (source.key,))
        moments = [row[name] for name in ("refreshed_at", "attempted_at") if row and row[name]]
        return max(moments) if moments else None

    async def status(self, source: Source) -> dict:
        row = await self.db.one("SELECT * FROM wiki_copies WHERE source = ?", (source.key,))
        stored = await self.db.scalar("SELECT COUNT(*) FROM wiki_pages WHERE copy_id = ?", (row["id"],)) if row else 0
        index = self.indexes.get(source.key)
        return {
            "pages": stored,
            "sections": len(index.sections) if index else 0,
            "loaded": self.index(source) is not None,
            "refreshed_at": row["refreshed_at"] if row else None,
            "last_error": row["last_error"] if row else None,
            "summary": json.loads(row["summary"]) if row and row["summary"] else None,
            "running": self.running(source),
        }

    async def refresh(self, source: Source, outside: Any, *, force: bool = False, wanted: Callable[[], bool] | None = None) -> dict:
        """Bring a copy up to date with its site, then rebuild its index. One update runs at a time for each copy, and a second call
        while one runs returns {"ok": False, "already_running": True}. Across every copy, UPDATES_AT_ONCE run at a time and the rest
        wait their turn. From a sitemap, only pages whose lastmod changed are downloaded unless force is set. A crawl reads every page
        again, since that's how it finds them. wanted is asked before each page, and an update whose source no server reads now stops
        there. A copy keeps at most MAX_COPY_CHARS characters, and pages past that are left out."""
        lock = self._locks.setdefault(source.key, asyncio.Lock())
        if lock.locked():
            return {"ok": False, "already_running": True}
        async with lock, self._turns:
            return await self._refresh(source, outside, force, wanted or (lambda: True))

    async def _sitemap_pages(self, source: Source, outside: Any) -> tuple[dict[str, str | None] | None, str]:
        """The pages the source's sitemap lists, following one level of sitemap index, or (None, why not)."""
        got = await fetch(outside, source.address, max_bytes=SITEMAP_MAX_BYTES)
        if got.body is None:
            return None, got.failure("The sitemap")
        pages, children = await asyncio.to_thread(parse_sitemap, got.body, source)
        for child in children[:SITEMAPS_PER_INDEX]:
            inner = await fetch(outside, child, max_bytes=SITEMAP_MAX_BYTES)
            if inner.body is None:
                return None, inner.failure(f"The sitemap {child}")
            pages.update((await asyncio.to_thread(parse_sitemap, inner.body, source))[0])
        return pages, ""

    async def _robots(self, source: Source, outside: Any) -> urllib.robotparser.RobotFileParser | None:
        got = await fetch(outside, source.base + "/robots.txt")
        if got.body is None:
            return None
        return await asyncio.to_thread(robot_rules, got.body)

    async def _crawl(self, source: Source, outside: Any, summary: dict, keep: Callable[..., Awaitable[bool]], wanted: Callable[[], bool]) -> set[str] | None:
        """Read every page reachable from the start page that the source wants, handing each to keep as it comes, which says False once
        the copy is full. Returns the pages kept, or None when the start page can't be read or the update stopped."""
        robots = await self._robots(source, outside)
        start = source.address.split("#", 1)[0]
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(start)
        seen = {same_page(start)}
        kept: set[str] = set()
        failed_start: list[str] = []
        busy = [0]
        stopped: list[str] = []  # why the crawl ended early

        async def visit(url: str) -> None:
            """Read one page and queue the pages it links to. A page is fetched as its link spells it, so the links on it resolve the
            way a browser resolves them, and kept under its one spelling. A page that can't be read is counted, and the crawl goes on."""
            try:
                await read(url)
            except Exception:
                log.exception("Reading %s failed", url)
                summary["failed"]["error"] += 1

        async def read(url: str) -> None:
            if robots is not None and not robots.can_fetch(ROBOTS_AGENT, url):
                summary["failed"]["robots"] += 1
                return
            try:
                got = await fetch(outside, url)
            finally:
                await asyncio.sleep(FETCH_PAUSE)
            if got.body is None or not got.html:
                summary["failed"][f"http_{got.status}" if got.body is None and got.status else ("network" if got.body is None else "not_a_page")] += 1
                if url == start:
                    failed_start.append(got.failure("The start page") if got.body is None else "The start page isn't a web page.")
                return
            for link in await asyncio.to_thread(page_links, got.body, url, source):
                if same_page(link) in seen:
                    continue
                if len(seen) >= MAX_PAGES:
                    summary["left_out"] += 1  # linked, and past what a copy keeps
                    continue
                seen.add(same_page(link))
                queue.put_nowait(link)
            extracted = await asyncio.to_thread(extract_page, got.body, url, source.prefix, source.selector)
            if extracted is None:
                summary["failed"]["no_content"] += 1
            elif await keep(same_page(url), *extracted):
                kept.add(same_page(url))
            else:
                summary["left_out"] += 1
                stopped.append("full")

        async def worker() -> None:
            while not stopped:
                if not wanted():
                    stopped.append("not wanted")
                    return
                try:
                    url = queue.get_nowait()
                except asyncio.QueueEmpty:
                    if not busy[0]:
                        return
                    await asyncio.sleep(0.05)  # another worker may still find links
                    continue
                busy[0] += 1
                try:
                    await visit(url)
                finally:
                    busy[0] -= 1

        await asyncio.gather(*(worker() for _ in range(FETCH_WORKERS)))
        summary["left_out"] += queue.qsize() if stopped else 0
        if "not wanted" in stopped:
            summary["error"] = NOT_WANTED
            return None
        if failed_start:
            summary["error"] = failed_start[0]
            return None
        return kept

    async def _refresh(self, source: Source, outside: Any, force: bool, wanted: Callable[[], bool]) -> dict:
        started = time.monotonic()
        now = time.time()
        summary: dict[str, Any] = {"ok": False, "sitemap_pages": 0, "updated": 0, "unchanged": 0, "removed": 0, "failed": Counter(), "left_out": 0}
        copy_id = await self._copy_id(source, create=True)
        await self.db.run("UPDATE wiki_copies SET attempted_at = ? WHERE id = ?", (now, copy_id))
        stored_rows = await self.db.all("SELECT url, lastmod, length(content) AS size FROM wiki_pages WHERE copy_id = ?", (copy_id,))
        stored = {row["url"]: row["lastmod"] for row in stored_rows}
        sizes = {row["url"]: row["size"] or 0 for row in stored_rows}
        written: list[tuple] = []
        room = [MAX_COPY_CHARS]  # characters the copy can still take

        async def save() -> None:
            rows, written[:] = list(written), []
            if rows:
                await self.db.many("INSERT OR REPLACE INTO wiki_pages (copy_id, url, title, content, lastmod, fetched_at) VALUES (?, ?, ?, ?, ?, ?)", rows)

        async def store(url: str, title: str, content: str, lastmod: str | None) -> bool:
            """Keep one page when the copy has room for it. Returns False when it doesn't."""
            if len(content) > room[0]:
                return False
            room[0] -= len(content)
            written.append((copy_id, url, title, content, lastmod, now))
            summary["updated"] += 1
            if len(written) >= COMMIT_EVERY:
                await save()
            return True

        async def keep_crawled(url: str, title: str, content: str) -> bool:
            """A crawled page, written only when it changed. A page that didn't change still takes its room."""
            if not force and url in stored:
                row = await self.db.one("SELECT title, content FROM wiki_pages WHERE copy_id = ? AND url = ?", (copy_id, url))
                if row is not None and (row["title"], row["content"]) == (title, content):
                    if len(content) > room[0]:
                        return False
                    room[0] -= len(content)
                    summary["unchanged"] += 1
                    return True
            return await store(url, title, content, None)

        if source.kind == CRAWL:
            try:
                crawled = await self._crawl(source, outside, summary, keep_crawled, wanted)
            finally:
                await save()  # what came in before a stop is kept
            if crawled is None:
                await self.db.run("UPDATE wiki_copies SET last_error = ? WHERE id = ?", (summary["error"], copy_id))
                summary["failed"] = dict(summary["failed"])
                return summary
            summary["sitemap_pages"] = len(crawled)
            listed = crawled
        else:
            sitemap, problem = await self._sitemap_pages(source, outside)
            if sitemap is None:
                summary["error"] = problem
                await self.db.run("UPDATE wiki_copies SET last_error = ? WHERE id = ?", (problem, copy_id))
                summary["failed"] = dict(summary["failed"])
                return summary
            summary["sitemap_pages"] = len(sitemap)
            if len(sitemap) > MAX_PAGES:
                summary["left_out"] = len(sitemap) - MAX_PAGES
                sitemap = dict(sorted(sitemap.items())[:MAX_PAGES])
            todo = [url for url, changed in sitemap.items() if force or url not in stored or stored[url] != changed]
            summary["unchanged"] = len(sitemap) - len(todo)
            waiting = set(todo)
            room[0] -= sum(sizes[url] for url in sitemap if url in stored and url not in waiting)
            queue: asyncio.Queue = asyncio.Queue()
            for url in todo:
                queue.put_nowait(url)
            stopped: list[str] = []

            async def worker() -> None:
                while not stopped:
                    if not wanted():
                        stopped.append("not wanted")
                        return
                    try:
                        url = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    got = await fetch(outside, url)
                    if got.body is None:
                        summary["failed"][f"http_{got.status}" if got.status else "network"] += 1
                    else:
                        try:
                            extracted = await asyncio.to_thread(extract_page, got.body, url, source.prefix, source.selector)
                        except Exception:
                            log.exception("Reading %s failed", url)  # one page can't end the update
                            summary["failed"]["error"] += 1
                        else:
                            if extracted is None:
                                summary["failed"]["no_content"] += 1
                            elif not await store(url, extracted[0], extracted[1], sitemap[url]):
                                summary["left_out"] += 1
                                stopped.append("full")
                    await asyncio.sleep(FETCH_PAUSE)

            try:
                await asyncio.gather(*(worker() for _ in range(FETCH_WORKERS)))
            finally:
                await save()  # what came in before a stop is kept, and the next update skips it
            if "not wanted" in stopped:
                summary["error"] = NOT_WANTED
                summary["failed"] = dict(summary["failed"])
                return summary
            summary["left_out"] += queue.qsize()
            listed = set(sitemap)
        # A list with fewer than half the pages the copy holds is broken or cut short, so nothing is removed on its word.
        if len(listed) * 2 >= len(stored):
            gone = [url for url in stored if url not in listed]
            await self.db.many("DELETE FROM wiki_pages WHERE copy_id = ? AND url = ?", [(copy_id, url) for url in gone])
            summary["removed"] = len(gone)
        else:
            summary["kept_on_short_sitemap"] = True
        if summary["updated"] or summary["removed"] or self.index(source) is None:
            await self.load(source)
        summary["ok"] = True
        index = self.indexes.get(source.key)
        summary["pages"] = len(index.pages) if index else 0
        summary["sections"] = len(index.sections) if index else 0
        summary["seconds"] = round(time.monotonic() - started, 1)
        summary["failed"] = dict(summary["failed"])
        await self.db.run("UPDATE wiki_copies SET refreshed_at = ?, last_error = NULL, summary = ? WHERE id = ?", (now, json.dumps(summary), copy_id))
        log.info("Update of %s: %s", source.address, summary)
        return summary
