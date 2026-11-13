"""Reddit posts, read and laid out as Discord messages.

Nothing here touches the network or Discord. modules/reddit.py fetches the pages and sends the messages, and this file reads what came
back: an account's feed, a post's embed page and a courier's delivery. It turns a post's body into Discord markdown and cuts the result
into messages of at most 2,000 characters, each holding the pictures the post shows at that point.

A relayed post reads as the post does. The title is a heading. The body keeps its words, headings, lists, bold and links. A link keeps
its words and its address without the utm_ tags, wrapped so Discord draws no preview card. A picture in the body ends a message and is
attached to it, so it sits under the words it followed. The closing line, which mentions the roles a server chose, ends the last message.
"""
from __future__ import annotations

import base64
import binascii
import calendar
import html as html_text
import io
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import feedparser
from bs4 import BeautifulSoup, Comment, NavigableString

from .validate import Invalid

SITE = "https://www.reddit.com"
EMBED_SITE = "https://embed.reddit.com"
MESSAGE_LIMIT = 2000
FILES_PER_MESSAGE = 10
TITLE_LIMIT = 300
BODY_LIMIT = 300_000
GALLERY_LIMIT = 20  # the most pictures Reddit lets a gallery hold
MARK_ROOM = 12  # kept free in each piece of a cut paragraph, for the bold and italic marks closed at its end and opened again after it
REDDIT_HOSTS = ("reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "np.reddit.com", "sh.reddit.com")
# Where a post's pictures and videos are fetched from. Nothing is downloaded from any other host, whatever a post or a delivery names.
MEDIA_HOSTS = frozenset({"i.redd.it", "preview.redd.it", "packaged-media.redd.it", "v.redd.it"})
PICTURE_NAME = re.compile(r"[0-9A-Za-z]{1,40}\.(?:png|jpe?g|gif|webp)", re.IGNORECASE)
ACCOUNT = re.compile(r"[A-Za-z0-9_-]{3,20}")
ACCOUNT_LINK = re.compile(r"(?:https?://)?(?:[a-z]+\.)?reddit\.com/(?:u|user)/([A-Za-z0-9_-]{3,20})(?:[/?#].*)?", re.IGNORECASE)
COMMUNITY = re.compile(r"[A-Za-z0-9_.-]{1,40}")
POST_KEY = re.compile(r"t3_[0-9a-z]{1,13}")
POST_IN_PATH = re.compile(r"/comments/([0-9a-z]{1,13})(?:/|$)")
SHORT_LINK = re.compile(r"/([0-9a-z]{1,13})/?")


@dataclass
class Post:
    """One post. `key` is Reddit's own ID for it, such as t3_1abcde. `target` is what the post points at: a picture, a video, a gallery,
    another site, or the post itself when it's all text. `html` is the body as Reddit renders it, and `text` the same body as markdown,
    used only when there's no HTML. A courier fills `gallery` and `video` when it knows them."""
    key: str
    title: str
    link: str
    html: str = ""
    target: str = ""
    at: float | None = None
    author: str = ""
    community: str = ""
    text: str = ""
    gallery: list[str] = field(default_factory=list)
    video: dict | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: Any) -> "Post | None":
        """A stored post. A row can come from a backup file someone edited, so anything that doesn't read as a post counts as none."""
        try:
            return checked_post(json.loads(raw)) if isinstance(raw, str) else None
        except ValueError:
            return None


@dataclass
class Text:
    """A block of the body as Discord markdown. A heading stays with the block after it when a message has to break."""
    text: str
    heading: bool = False
    code: bool = False


@dataclass
class Media:
    """A picture or a video to attach. `urls` are the addresses to try, best first. `link` goes out in the file's place when none of them
    can be had. A video's addresses stop working within hours, so `page` names the embed page that lists fresh ones."""
    kind: str
    urls: list[str]
    link: str
    name: str
    page: str = ""


# ---- names and addresses ----

def account_name(value: Any) -> str:
    """A Reddit account's name from what a person typed: the name, u/name or the account's address. Raises Invalid with a sentence."""
    if not isinstance(value, str) or not value.strip():
        raise Invalid("Give the Reddit account's name.")
    cleaned = value.strip()
    found = ACCOUNT_LINK.fullmatch(cleaned)
    name = found.group(1) if found else re.sub(r"^/?(?:u|user)/", "", cleaned, flags=re.IGNORECASE).strip("/")
    if not ACCOUNT.fullmatch(name):
        raise Invalid("That isn't a Reddit account's name. A name is 3 to 20 letters, numbers, hyphens and underscores, as in u/example.")
    return name


def feed_address(account: str) -> str:
    """The feed of an account's own posts, newest first. Its comments are in a different feed, so they never appear."""
    return f"{SITE}/user/{account}/submitted/.rss?sort=new"


def post_key(link: Any) -> str | None:
    """Reddit's ID for the post a link leads to, or None when the link isn't a post's."""
    if not isinstance(link, str):
        return None
    found = POST_IN_PATH.search(urlsplit(link).path)
    return f"t3_{found.group(1)}" if found else None


def post_link(value: Any) -> str:
    """A post's address on Reddit's main site, from a link a person gave. Raises Invalid with a sentence."""
    if not isinstance(value, str) or not value.strip():
        raise Invalid("Give the post's link.")
    cleaned = value.strip()
    parts = urlsplit(cleaned if "://" in cleaned else f"https://{cleaned}")
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or len(cleaned) > 2048:
        raise Invalid("That isn't a link to a Reddit post.")
    if host == "redd.it" and SHORT_LINK.fullmatch(parts.path):
        return f"{SITE}/comments/{parts.path.strip('/')}/"
    if host not in REDDIT_HOSTS or not POST_IN_PATH.search(parts.path):
        raise Invalid("That isn't a link to a Reddit post. Open the post and copy the address from the browser's address bar.")
    path = parts.path[:POST_IN_PATH.search(parts.path).end()]
    slug = parts.path[len(path):].split("/")[0]
    path = path if path.endswith("/") else path + "/"
    return f"{SITE}{path}{slug + '/' if slug else ''}"


def post_feed_address(link: str) -> str:
    """The feed of one post, which holds the post itself first and its comments after it."""
    return link.rstrip("/") + "/.rss"


def embed_address(link: str) -> str:
    """The page Reddit serves for embedding a post on another site. It lists a gallery's pictures and a video's files."""
    return f"{EMBED_SITE}{urlsplit(link).path}?embed=true"


def clean_address(address: Any) -> str:
    """A link's address as it goes out: a full http or https address, without the utm_ tags that name where a click came from. An address
    inside Reddit that starts with a slash is made whole. Anything else, such as a mailto link, gives nothing."""
    if not isinstance(address, str):
        return ""
    address = address.strip()
    if address.startswith("//"):
        address = "https:" + address
    elif address.startswith("/"):
        address = SITE + address
    try:
        parts = urlsplit(address)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    if "utm_" in parts.query.lower():
        # Only the tags are dropped. Everything else stays byte for byte, because some addresses are signed over their query.
        kept = "&".join(pair for pair in parts.query.split("&") if not pair.lower().startswith("utm_"))
        address = urlunsplit((parts.scheme, parts.netloc, parts.path, kept, parts.fragment))
    return address.replace(" ", "%20").replace("<", "%3C").replace(">", "%3E")


def picture(address: Any) -> Media | None:
    """A picture Reddit hosts, from its address or from the address of a preview of it. The full picture is tried first, then the preview."""
    address = clean_address(address)
    parts = urlsplit(address)
    host = (parts.hostname or "").lower()
    if host != "i.redd.it" and not host.endswith("preview.redd.it"):
        return None
    name = re.sub(r"^.*-v0-", "", parts.path.rsplit("/", 1)[-1])  # a preview's name can start with words from the post's title
    if not PICTURE_NAME.fullmatch(name):
        return None
    original = f"https://i.redd.it/{name}"
    signed = [address] if host == "preview.redd.it" and parts.query else []
    return Media("image", [original, *signed], original, name)


# ---- reading a feed ----

def read_posts(body: bytes) -> list[Post] | None:
    """The posts in one of Reddit's feeds, newest first, or None when the body isn't a feed. Comments, which a post's own feed lists after
    the post, are left out."""
    parsed = feedparser.parse(io.BytesIO(body))  # as a stream, because feedparser tries bytes as a file name first
    items = parsed.get("entries") or []
    if not items and (parsed.get("bozo") or not (parsed.get("feed") or {}).get("title")):
        return None
    posts = []
    for item in items:
        link = item.get("link") or ""
        key = post_key(link)
        if key is None or re.search(r"t1_[0-9a-z]+$", item.get("id") or ""):
            continue
        content = item.get("content") or []
        body_html, target = _frame((content[0].get("value") if content else "") or item.get("summary") or "")
        stamp = item.get("published_parsed") or item.get("updated_parsed")
        tags = item.get("tags") or []
        post = checked_post({
            "key": key, "title": item.get("title") or "", "link": link, "html": body_html, "target": target,
            "at": float(calendar.timegm(stamp)) if stamp else None, "author": re.sub(r"^/?u/", "", item.get("author") or ""),
            "community": (tags[0].get("term") or "") if tags else "",
        })
        if post is not None:
            posts.append(post)
    posts.sort(key=lambda post: post.at or 0.0, reverse=True)
    return posts


def _frame(content: str) -> tuple[str, str]:
    """The body, and the address the post points at, from the table Reddit wraps a feed entry in."""
    soup = BeautifulSoup(content or "", "html.parser")
    body = soup.find("div", class_="md")
    body_html = str(body) if body is not None else ""
    if body is not None:
        body.extract()  # so a link in the body that reads "[link]" isn't taken for the post's own
    target = next((link.get("href") or "" for link in soup.find_all("a") if link.get_text(strip=True) == "[link]"), "")
    return body_html, target.strip()


def checked_post(data: Any) -> Post | None:
    """A post from fields that came from outside: a feed, a courier or a stored row. Anything that isn't a post's gives None. Text is cut
    to what a post can hold, and addresses that aren't plain web addresses are dropped."""
    if not isinstance(data, dict):
        return None
    key, title, link = data.get("key"), data.get("title"), data.get("link")
    if not isinstance(key, str) or not POST_KEY.fullmatch(key) or not isinstance(title, str) or not isinstance(link, str):
        return None
    title = " ".join(title.split())[:TITLE_LIMIT]
    parts = urlsplit(link.strip())
    if not title or (parts.hostname or "").lower() not in REDDIT_HOSTS or post_key(link) != key:
        return None
    at = data.get("at")
    gallery = data.get("gallery")
    video = data.get("video")
    body_html = _string(data.get("html"), BODY_LIMIT)
    if "&lt;" in body_html and not re.search(r"<[A-Za-z!/]", body_html):  # some of Reddit's own interfaces hand the body over with its tags escaped
        body_html = html_text.unescape(body_html)
    post = Post(
        key=key, title=title, link=f"{SITE}{parts.path if parts.path.endswith('/') else parts.path + '/'}",
        html=body_html, target=clean_address(_string(data.get("target"), 2048)),
        at=float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and 0 < at < 1e11 else None,
        author=_named(data.get("author"), ACCOUNT), community=_named(data.get("community"), COMMUNITY), text=_string(data.get("text"), BODY_LIMIT),
    )
    if isinstance(gallery, list):
        found = [item for item in (picture(address) for address in gallery[:GALLERY_LIMIT] if isinstance(address, str)) if item is not None]
        post.gallery = [item.urls[0] for item in found]
    if isinstance(video, dict) and isinstance(video.get("url"), str) and (urlsplit(video["url"]).hostname or "").lower() == "v.redd.it":
        post.video = {"url": clean_address(video["url"][:2048])}
    return post


def _string(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _named(value: Any, pattern: re.Pattern) -> str:
    return value if isinstance(value, str) and pattern.fullmatch(value) else ""


def kind(post: Post) -> str:
    """What a post is, by what it points at: picture, gallery, video, text or link."""
    parts = urlsplit(post.target)
    host = (parts.hostname or "").lower()
    if post.gallery or (host in REDDIT_HOSTS and parts.path.startswith("/gallery/")):
        return "gallery"
    if host == "v.redd.it":
        return "video"
    if picture(post.target) is not None:
        return "picture"
    if not post.target or (host in REDDIT_HOSTS and post_key(post.target) == post.key):
        return "text"
    return "link"


# ---- the body as Discord markdown ----

_ADDRESS = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_MARKS = re.compile(r"([\\*_~`|<\[\]])")
_LINE_START = re.compile(r"^(\s*)(#{1,3}\s|-#\s|>{1,3}\s|[-+]\s)", re.MULTILINE)
_WRAP = {"strong": "**", "b": "**", "em": "*", "i": "*", "del": "~~", "s": "~~", "strike": "~~"}
_HEADINGS = {"h1": "## ", "h2": "### ", "h3": None, "h4": None, "h5": None, "h6": None}
_BLOCKS = {"p", "div", "section", "article", "ul", "ol", "blockquote", "pre", "table", "hr", "figure", *_HEADINGS}
_SKIPPED = {"script", "style", "template", "noscript"}


def escape(text: str) -> str:
    """Words as Discord shows them typed. Nothing in them reads as markdown, as a mention or as a link's mask. An address written out in
    the words is wrapped, so Discord draws no card for it."""
    out, last = [], 0
    for found in _ADDRESS.finditer(text):
        address = found.group(0)
        while address[-1] in ".,;:!?'\"" or (address[-1] == ")" and "(" not in address):  # punctuation after an address isn't part of it
            address = address[:-1]
        out.append(_MARKS.sub(r"\\\1", text[last:found.start()]))
        out.append(f"<{address}>")
        last = found.start() + len(address)
    out.append(_MARKS.sub(r"\\\1", text[last:]))
    return "".join(out)


def _guard(text: str) -> str:
    """Keeps a line of plain words from reading as a heading, a quote or a list because of how it starts."""
    return _LINE_START.sub(lambda found: f"{found.group(1)}\\{found.group(2)}", text)


def _wrap(inner: str, mark: str) -> str:
    """Bold, italics and the like around some text. Spaces at its ends move outside the marks, where Discord wants them."""
    core = inner.strip()
    opened, closed = len(core) - len(core.lstrip(mark[0])), len(core) - len(core.rstrip(mark[0]))
    if not core or (opened == closed == len(mark) and len(core) > 2 * len(mark)):  # the same mark is already around it, from a tag inside
        return inner
    return f"{inner[:len(inner) - len(inner.lstrip())]}{mark}{core}{mark}{inner[len(inner.rstrip()):]}"


def _inline(nodes: Iterable[Any]) -> str:
    """Words and the tags inside a line of them, as markdown."""
    out = []
    for node in nodes:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            out.append(escape(re.sub(r"\s+", " ", str(node))))
            continue
        name = node.name
        if name in _SKIPPED:
            continue
        if name == "br":
            out.append("\n")
        elif name in _WRAP:
            out.append(_wrap(_inline(node.children), _WRAP[name]))
        elif name == "code":
            code = node.get_text()
            out.append(f"`` {code} ``" if "`" in code else f"`{code}`" if code else "")
        elif name == "a":
            out.append(_link(node))
        elif name == "img":
            source = clean_address(node.get("src"))
            out.append(f"<{source}>" if source else "")
        elif name == "span" and "md-spoiler-text" in (node.get("class") or []):
            out.append(_wrap(_inline(node.children), "||"))
        else:
            out.append(_inline(node.children))
    return "".join(out)


def _line(nodes: Iterable[Any]) -> str:
    return re.sub(r" *\n *", "\n", _inline(nodes)).strip()


def _link(node: Any) -> str:
    """A link as its words over its address. Words that are themselves an address give the address alone, since Discord shows a mask over
    such words as plain text."""
    address = clean_address(node.get("href"))
    label = " ".join(_inline(node.children).split())
    shown = " ".join(node.get_text().split())
    if not address:
        return label
    if not shown or re.fullmatch(r"(?:https?://|www\.)\S+", shown, re.IGNORECASE):
        return f"<{address}>"
    if re.fullmatch(r"[^\s/]+\.[A-Za-z]{2,}(?:/\S*)?", shown):  # words shaped like a site's name
        same_site = shown.split("/")[0].lower().removeprefix("www.") == (urlsplit(address).hostname or "").lower().removeprefix("www.")
        return f"<{address}>" if same_site else f"{label} (<{address}>)"
    return f"[{label}](<{address}>)"


def blocks(body_html: str) -> list[Text | Media]:
    """A post's body as blocks of text and the pictures between them, in the order the post shows them."""
    soup = BeautifulSoup(body_html or "", "html.parser")
    return [block for block in _blocks(soup.find("div", class_="md") or soup) if isinstance(block, Media) or block.text]


def _blocks(parent: Any) -> list[Text | Media]:
    out: list[Text | Media] = []
    loose: list[Any] = []

    def close_loose() -> None:
        text = _line(loose)
        loose.clear()
        if text:
            out.append(Text(_guard(text)))

    for node in parent.children:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString) or node.name not in _BLOCKS:
            if not isinstance(node, NavigableString) and node.name == "img" and picture(node.get("src")) is not None:
                close_loose()
                out.append(picture(node.get("src")))
            elif isinstance(node, NavigableString) or node.name not in _SKIPPED:
                loose.append(node)
            continue
        close_loose()
        name = node.name
        if name == "p":
            out.extend(_paragraph(node))
        elif name in _HEADINGS:
            text = " ".join(_line(node.children).split())
            if text:
                out.append(Text(f"{_HEADINGS[name]}{text}" if _HEADINGS[name] else f"**{text.replace('**', '')}**", heading=True))
        elif name in ("ul", "ol"):
            out.append(Text("\n".join(_list(node, 0))))
        elif name == "blockquote":
            out.extend(_quote(node))
        elif name == "pre":
            code = node.get_text().strip("\n").replace("```", "'''")
            if code.strip():
                out.append(Text(f"```\n{code}\n```", code=True))
        elif name == "table":
            out.append(Text(_guard(_table(node))))
        elif name == "figure":
            out.extend(_figure(node))
        elif name != "hr":
            out.extend(_blocks(node))
    close_loose()
    return out


def _paragraph(node: Any) -> list[Text | Media]:
    """A paragraph. One that holds only a link to a picture Reddit hosts is that picture, which is how Reddit writes a picture placed in
    the body."""
    inside = [child for child in node.children if not isinstance(child, Comment) and not (isinstance(child, NavigableString) and not child.strip())]
    if len(inside) == 1 and not isinstance(inside[0], NavigableString) and inside[0].name in ("a", "img"):
        found = picture(inside[0].get("href") if inside[0].name == "a" else inside[0].get("src"))
        if found is not None:
            words = " ".join(inside[0].get_text().split())
            caption = [] if not words or re.match(r"https?://", words, re.IGNORECASE) else [Text(_guard(_line(inside[0].children)))]
            return [*caption, found]
    text = _line(node.children)
    return [Text(_guard(text))] if text else []


def _list(node: Any, depth: int) -> list[str]:
    """A list as lines, one per item, with a list inside an item indented under it."""
    lines: list[str] = []
    try:
        number = int(node.get("start") or 1)
    except ValueError:
        number = 1
    for item in node.find_all("li", recursive=False):
        marker = f"{number}. " if node.name == "ol" else "- "
        number += 1
        runs: list[list[Any]] = [[]]  # the item's own words, a new run for each paragraph inside it
        nested: list[Any] = []
        for child in item.children:
            if not isinstance(child, NavigableString) and child.name in ("ul", "ol"):
                nested.append(child)
            elif not isinstance(child, NavigableString) and child.name == "p":
                runs.extend([list(child.children), []])
            else:
                runs[-1].append(child)
        text = "\n".join(line for run in runs for line in _line(run).split("\n") if line.strip())
        first, *rest = text.split("\n") if text else [""]
        lines.append(f"{'  ' * depth}{marker}{first}")
        lines.extend(f"{'  ' * (depth + 1)}{line}" for line in rest)
        for inner in nested:
            lines.extend(_list(inner, depth + 1))
    return lines


def _quote(node: Any) -> list[Text | Media]:
    """A quote with a mark at the start of each line, or hidden text when Reddit marks the quote as a spoiler. Pictures inside come after it."""
    inside = [block for block in _blocks(node) if isinstance(block, Media) or block.text]
    words = "\n".join(block.text for block in inside if isinstance(block, Text))
    media = [block for block in inside if isinstance(block, Media)]
    if not words:
        return media
    if "md-spoiler-text" in (node.get("class") or []):
        return [Text(f"||{words}||"), *media]
    return [Text("\n".join(f"> {line}" for line in words.split("\n") if line.strip())), *media]


def _table(node: Any) -> str:
    """A table as one line per row, its cells set apart by bars and its heading row in bold."""
    lines = []
    for row in node.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        words = [" ".join(_line(cell.children).split()) for cell in cells]
        if any(words):
            heading = all(cell.name == "th" for cell in cells)
            lines.append(" | ".join(f"**{word.replace('**', '')}**" if heading and word else word for word in words))
    return "\n".join(lines)


def _figure(node: Any) -> list[Text | Media]:
    """A picture Reddit set in the body with a figure tag, and its caption, which goes above it so the two share a message."""
    link, image, caption = node.find("a"), node.find("img"), node.find("figcaption")
    found = next((item for item in (picture(link.get("href")) if link else None, picture(image.get("src")) if image else None) if item is not None), None)
    words = _line(caption.children) if caption else ""
    return [*([Text(_guard(words))] if words else []), *([found] if found is not None else [])]


def markdown_blocks(text: str) -> list[Text | Media]:
    """A body that came as Reddit's markdown, for a post with no HTML. Discord reads most of it as written. Headings step down a size so
    the title stays the largest, links are wrapped so no card is drawn, and nothing in it can mention anyone."""
    out: list[Text | Media] = []
    for chunk in re.split(r"\n\s*\n", html_text.unescape(text or "").replace("\r\n", "\n")):
        chunk = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", chunk).strip()  # a picture set in the body, which only Reddit can show
        if not chunk:
            continue
        chunk = chunk.replace("<", "\\<")
        chunk = re.sub(r"\]\((https?://[^)\s]+)\)", lambda found: f"](<{clean_address(found.group(1))}>)", chunk)
        chunk = re.sub(r"(?<![(<])(https?://[^\s<>]*[^\s<>.,;:!?'\")])", lambda found: f"<{found.group(1)}>", chunk)
        heading = re.fullmatch(r"(#{1,6})\s*(.+)", chunk)
        if heading:
            level, words = len(heading.group(1)), heading.group(2).strip().rstrip("#").strip()
            out.append(Text(f"## {words}" if level == 1 else f"### {words}" if level == 2 else f"**{words.replace('**', '')}**", heading=True))
        else:
            out.append(Text(chunk, code=chunk.startswith("```")))
    return out


# ---- laying a post out as messages ----

def title_line(post: Post) -> str:
    return f"# {escape(' '.join(post.title.split())[:TITLE_LIMIT])}"


def lead_media(post: Post, gallery: list[str] | None = None) -> list[Media]:
    """What a post shows above its words: its picture, its gallery's pictures or its video. `gallery` is what an embed page listed, for a
    gallery post that doesn't carry its own list."""
    flavor = kind(post)
    if flavor == "picture":
        return [picture(post.target)]
    if flavor == "gallery":
        found = [picture(address) for address in (post.gallery or gallery or [])[:GALLERY_LIMIT]]
        return [item for item in found if item is not None]
    if flavor == "video":
        name = re.sub(r"[^0-9A-Za-z]", "", urlsplit(post.target).path.strip("/").split("/")[0]) or post.key
        return [Media("video", [post.video["url"]] if post.video else [], post.link, f"{name}.mp4", embed_address(post.link))]
    return []


def needs_gallery(post: Post) -> bool:
    """Whether a post's pictures are listed only on its embed page."""
    return kind(post) == "gallery" and not post.gallery


def layout(post: Post, lead: list[Media], closing: str = "") -> list[dict]:
    """The messages a post goes out as, in order. Each is {"text", "media", "last"}, and only the last may ping. The last also carries
    the closing line on its own, as "closing".

    A post whose words fit one message, with no pictures set in its body, is one message: title, body and closing line, with what it
    shows attached. Any other post starts with its title and what it shows, and its body follows. A message ends at each picture in the
    body, which is attached to it. A link post ends with its link on a line of its own, bare, so Discord draws the card for it.
    """
    body = blocks(post.html) if post.html.strip() else markdown_blocks(post.text)
    flavor = kind(post)
    if flavor == "link":
        body.append(Text(clean_address(post.target)))
    elif flavor in ("gallery", "video") and not lead:
        body.append(Text(post.link))
    head = title_line(post)
    if all(isinstance(block, Text) for block in body) and len(lead) <= FILES_PER_MESSAGE:
        whole = "\n\n".join(part for part in (head, *(block.text for block in body), closing) if part)
        if len(whole) <= MESSAGE_LIMIT:
            return [{"text": whole, "media": [asdict(item) for item in lead], "last": True, "closing": closing}]
    sequence: list[Any] = [Text(head, heading=True), *([lead] if lead else []), *body, *([Text(closing)] if closing else [])]
    parts = _pack(sequence)
    for part in parts:
        part["last"] = part is parts[-1]
    parts[-1]["closing"] = closing  # so the sender can move the line when this message's files spill into another
    return parts


def _pack(sequence: list[Any]) -> list[dict]:
    """Text blocks, pictures and groups of pictures as messages. A picture or a group closes the message that holds the words before it."""
    parts: list[dict] = []
    waiting: list[Text] = []

    def close(media: list[Media]) -> None:
        pieces = split_blocks(waiting)
        waiting.clear()
        parts.extend({"text": piece, "media": []} for piece in pieces[:-1])
        for start in range(0, max(len(media), 1), FILES_PER_MESSAGE):
            words = pieces[-1] if pieces and start == 0 else ""
            group = media[start:start + FILES_PER_MESSAGE]
            if words or group:
                parts.append({"text": words, "media": [asdict(item) for item in group]})

    for item in sequence:
        if isinstance(item, Text):
            waiting.append(item)
        else:
            close([item] if isinstance(item, Media) else list(item))
    close([])
    return parts


def split_blocks(found: list[Text], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Blocks of text as messages of at most `limit` characters. A message breaks between blocks where it can. A heading goes with the
    block after it. A block longer than a message is cut at a line, then at the end of a sentence, then at a word."""
    messages: list[str] = []
    current: list[Text] = []

    def size(items: list[Text]) -> int:
        return sum(len(item.text) for item in items) + 2 * max(len(items) - 1, 0)

    def close() -> None:
        if current:
            messages.append("\n\n".join(item.text for item in current))
            current.clear()

    for block in found:
        if not block.text:
            continue
        if len(block.text) > limit:
            held = current.pop() if current and current[-1].heading else None
            close()
            room = limit - len(held.text) - 2 if held is not None and len(held.text) + 2 < limit // 2 else None
            if held is not None and room is None:
                messages.append(held.text)
            pieces = split_long(block.text, limit, first=room, code=block.code)
            if held is not None and room is not None:
                pieces[0] = f"{held.text}\n\n{pieces[0]}"
            messages.extend(pieces[:-1])
            current.append(Text(pieces[-1], code=block.code))
            continue
        if size([*current, block]) > limit:
            held = current.pop() if len(current) > 1 and current[-1].heading else None
            close()
            if held is not None:
                current.append(held)
            if size([*current, block]) > limit:
                close()
        current.append(block)
    close()
    return messages


def split_long(text: str, limit: int = MESSAGE_LIMIT, *, first: int | None = None, code: bool = False) -> list[str]:
    """One block that's longer than a message, as pieces that each fit. `first` is the room in the first piece when something shares its
    message. A code block is cut between lines and fenced again in each piece. Bold and the like left open where a piece ends is closed
    there and opened again in the next."""
    if code and text.startswith("```"):
        inner = text.removeprefix("```").removeprefix("\n").removesuffix("```").removesuffix("\n")
        pieces = _pack_lines(inner.split("\n"), limit - 8, None if first is None else first - 8)
        return [f"```\n{piece}\n```" for piece in pieces] or [text[:limit]]
    pieces = _pack_lines(text.split("\n"), limit - MARK_ROOM, None if first is None else first - MARK_ROOM)
    return _balance(pieces) or [text[:limit]]


def _pack_lines(lines: list[str], limit: int, first: int | None) -> list[str]:
    out: list[str] = []
    current = ""

    def room() -> int:
        return max(first if first is not None and not out else limit, 40)

    def close() -> None:
        nonlocal current
        if current.strip("\n"):
            out.append(current.strip("\n"))
        current = ""

    for line in lines:
        joined = f"{current}\n{line}" if current else line
        if len(joined) <= room():
            current = joined
            continue
        close()
        quoted = "> " if line.startswith("> ") else ""
        while len(line) > room():
            head, rest = _cut(line, room())
            if head:
                out.append(head)
            line = f"{quoted}{rest}" if rest else ""
        current = line
    close()
    return out


_TOKEN = re.compile(r"\[(?:\\.|[^\]\\])*\]\(<[^>\s]*>\)|<https?://[^>\s]*>|\S+|\s+")


def _cut(line: str, room: int) -> tuple[str, str]:
    """One long line as what fits and the rest. The cut falls after a sentence when one ends in the second half of the room, and else
    between words. A link is never cut through unless it alone is longer than a message."""
    used = stop = 0
    for found in _TOKEN.finditer(line):
        if found.end() > room:
            break
        used = found.end()
        token = found.group(0).rstrip("\"')*_~")
        if token and token[-1] in ".!?":
            stop = found.end()
    cut = stop if stop >= room // 2 else used
    if not line[:cut].strip():
        cut = room
    return line[:cut].rstrip(), line[cut:].lstrip()


def _balance(pieces: list[str]) -> list[str]:
    """Closes the bold, italics, strike and hidden-text marks a piece leaves open, and opens them again at the start of the next piece."""
    out: list[str] = []
    still_open: list[str] = []
    for piece in pieces:
        piece = "".join(still_open) + piece
        counts = {
            "**": len(re.findall(r"(?<!\\)\*\*", piece)), "~~": len(re.findall(r"(?<!\\)~~", piece)), "||": len(re.findall(r"(?<!\\)\|\|", piece)),
            "*": len(re.findall(r"(?<![\\*])\*(?!\*)", piece)),
        }
        still_open = [mark for mark, count in counts.items() if count % 2]
        out.append(piece + "".join(reversed(still_open)))
    return out


def checked_plan(raw: Any) -> list[dict] | None:
    """A stored list of messages, as `layout` makes it. A row can come from a backup file someone edited, so one that doesn't read as a
    plan gives None, and the post is laid out again."""
    try:
        parts = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if not isinstance(parts, list) or not parts or not all(checked_part(part) for part in parts):
        return None
    return parts


def checked_part(part: Any) -> bool:
    """Whether one stored message reads as `layout` makes it."""
    if not isinstance(part, dict) or not isinstance(part.get("text"), str) or not isinstance(part.get("media"), list) or len(part["media"]) > FILES_PER_MESSAGE:
        return False
    if not isinstance(part.get("closing", ""), str):
        return False
    for item in part["media"]:
        if not isinstance(item, dict) or item.get("kind") not in ("image", "video") or not all(isinstance(item.get(name), str) for name in ("link", "name", "page")):
            return False
        if not isinstance(item.get("urls"), list) or not all(isinstance(address, str) for address in item["urls"]):
            return False
        if item["page"] and not item["page"].startswith(f"{EMBED_SITE}/"):
            return False
    return True


# ---- a post's embed page ----

@dataclass
class Embed:
    """What a post's embed page tells: its title, its body when the post is all text, a gallery's pictures and a video's files."""
    title: str = ""
    link: str = ""
    html: str = ""
    target: str = ""
    at: float | None = None
    author: str = ""
    community: str = ""
    gallery: list[str] = field(default_factory=list)
    videos: list[dict] = field(default_factory=list)

    def post(self, key: str, link: str) -> Post | None:
        """The page as a post. It holds the body only for a text post, so a picture or link post comes without its words."""
        return checked_post({"key": key, "title": self.title, "link": self.link or link, "html": self.html, "target": self.target, "at": self.at,
                             "author": self.author, "community": self.community, "gallery": self.gallery})


def read_embed(page: str) -> Embed:
    soup = BeautifulSoup(page or "", "html.parser")
    found = Embed()
    title = soup.find("shreddit-embed-title") or next((heading for heading in soup.find_all("h1") if heading.find_parent(class_="md") is None), None)
    found.title = " ".join(title.get_text().split()) if title is not None else ""
    body = soup.find("div", class_="md")
    found.html = str(body) if body is not None else ""
    for tracker in soup.find_all("faceplate-tracker"):
        try:
            context = json.loads(tracker.get("data-faceplate-tracking-context") or "null")
        except ValueError:
            continue
        about = context.get("post") if isinstance(context, dict) else None
        if isinstance(about, dict) and isinstance(about.get("id"), str):
            found.target = about["url"] if isinstance(about.get("url"), str) else ""
            stamp = about.get("created_timestamp")
            found.at = stamp / 1000 if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else None
            break
    button = soup.find("shreddit-embed-copy-link-button")
    found.link = (button.get("permalink") or "") if button is not None else ""
    community = re.match(r"/r/([A-Za-z0-9_.-]{1,40})/", urlsplit(found.link).path)
    found.community = community.group(1) if community else ""
    author = next((re.search(r"/user/([A-Za-z0-9_-]{3,20})", link.get("href") or "") for link in soup.find_all("a") if "/user/" in (link.get("href") or "")), None)
    found.author = author.group(1) if author else ""
    carousel = soup.find("gallery-carousel")
    for slot in carousel.find_all("li") if carousel is not None else []:
        image = slot.find("img") or slot.find("faceplate-img")
        item = picture(image.get("src")) if image is not None else None
        if item is not None and item.urls[0] not in found.gallery:
            found.gallery.append(item.urls[0])
    player = soup.find("shreddit-player")
    try:
        packaged = json.loads(player.get("packaged-media-json") or "null") if player is not None else None
    except ValueError:
        packaged = None
    playback = packaged.get("playbackMp4s") if isinstance(packaged, dict) else None
    if isinstance(playback, dict):
        seconds = playback.get("duration") if isinstance(playback.get("duration"), (int, float)) else 0
        for choice in playback.get("permutations") or []:
            source = choice.get("source") if isinstance(choice, dict) else None
            if not isinstance(source, dict) or not isinstance(source.get("url"), str) or (urlsplit(source["url"]).hostname or "").lower() != "packaged-media.redd.it":
                continue
            rate = source.get("bitrateBps") if isinstance(source.get("bitrateBps"), (int, float)) else 0
            size = source.get("dimensions") if isinstance(source.get("dimensions"), dict) else {}
            height = size.get("height") if isinstance(size.get("height"), (int, float)) else 0
            found.videos.append({"url": source["url"], "bytes": int(rate * seconds / 8), "height": int(height)})
        found.videos.sort(key=lambda video: video["height"], reverse=True)
    return found


def video_choices(videos: list[dict], most: int) -> list[str]:
    """A video's files that should fit in `most` bytes, sharpest first. The sizes are worked out from each file's bit rate and the length."""
    return [video["url"] for video in videos if video["bytes"] <= most]


# ---- a courier's delivery ----

DELIVERY = re.compile(r"cg-reddit 1 (t3_[0-9a-z]{1,13}) ([1-9][0-9]{0,2})/([1-9][0-9]{0,2}) ([0-9a-f]{8})")
DELIVERY_PARTS = 120  # at 3,900 characters a part, far more than the longest post Reddit takes


@dataclass
class Piece:
    """One message of a delivery: which post, which part of how many, the check over the whole delivery, and this part's text."""
    key: str
    part: int
    parts: int
    check: str
    chunk: str


def checksum(encoded: str) -> str:
    """The check a courier writes on every part of a delivery: FNV-1a over the delivery's whole encoded text, as eight hex digits."""
    value = 0x811C9DC5
    for byte in encoded.encode("utf-8"):
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return f"{value:08x}"


def read_piece(title: Any, description: Any) -> Piece | None:
    """A delivery's part from a message's embed, or None when the embed isn't one."""
    found = DELIVERY.fullmatch(title.strip()) if isinstance(title, str) else None
    if found is None or not isinstance(description, str) or not description.strip():
        return None
    part, parts = int(found.group(2)), int(found.group(3))
    if part > parts or parts > DELIVERY_PARTS:
        return None
    return Piece(found.group(1), part, parts, found.group(4), description.strip())


def assemble(pieces: Iterable[Piece]) -> Post | None:
    """The post a full set of parts holds. None while a part is missing, or when the parts don't add up to what their check says."""
    by_part: dict[int, Piece] = {}
    for piece in pieces:
        by_part[piece.part] = piece
    if not by_part:
        return None
    first = next(iter(by_part.values()))
    if any((piece.key, piece.parts, piece.check) != (first.key, first.parts, first.check) for piece in by_part.values()) or set(by_part) != set(range(1, first.parts + 1)):
        return None
    encoded = "".join(by_part[number].chunk for number in range(1, first.parts + 1))
    if checksum(encoded) != first.check:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8"))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("id") != first.key:
        return None
    return checked_post({
        "key": first.key, "title": payload.get("title"), "link": payload.get("link"), "html": payload.get("html"), "target": payload.get("url"), "at": payload.get("at"),
        "author": payload.get("author"), "community": payload.get("community"), "text": payload.get("text"), "gallery": payload.get("gallery"), "video": payload.get("video"),
    })
