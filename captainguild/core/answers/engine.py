"""One member's request through the model: the call loop, the wiki tools, sketches, and the sources under the answer.

Each passage a wiki tool returns starts with a tag such as [S4], the model puts tags after the sentences they support, and the bot
turns them into numbered links. The API's own citations (search_result blocks) made the model quote the wiki word for word and repeat
each step, so the tags are the bot's. Web pages from /ask come back as [W: url] tags, kept only for pages the search really returned.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from xml.etree import ElementTree

import anthropic

from .prompts import DECLINE_WORD, REDIRECT_WORD, SKIP_WORD
from .wiki import PAGE_CHAR_LIMIT, Library, Section

log = logging.getLogger(__name__)

MAX_TOKENS = 64000  # thinking counts toward this, and at Max effort it can pass 20,000 tokens before the reply starts
# Every round streams, so no answer waits on one long HTTP read. The thinking isn't shown, so the stream can go quiet for as long as the
# model thinks, which at 64,000 tokens and about 90 a second passes ten minutes. A read waits that out.
READ_SECONDS = 1200.0
CONNECT_SECONDS = 10.0
CLIENT_RETRIES = 2
ANSWER_SECONDS = 25 * 60  # the most one request takes, every round together
TASK_BUDGET_BETA = "task-budgets-2026-03-13"
LAST_ROUND_AFTER = 10 * 60  # a round that starts later than this has to answer from what it has
# Bounds on one answer. A reply can ask for any number of tools at once, and every page read goes back in as input on each later
# round, so without these one request's cost and time have no limit short of the context window.
MAX_TOOL_CALLS = 40  # comparison questions have used 35 searches
READ_BUDGET = 300_000  # characters of wiki passages one answer reads, about 75,000 tokens
TOO_MANY_CALLS = "This answer has used every tool call it gets. Answer from what you have now."
CALLS_USED = "This answer can't call {tool} again. Answer from what you have now."
READ_ALL = "This answer has read as much of the wiki as it can. Answer from what you've read."

# List prices in dollars per million tokens (input, output, cache write, cache read), for the cost each request logs and the monthly
# budget. These are the models a server can choose. A model missing here is priced as the dearest one listed, so the estimate errs high.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 6.25, 0.50),
}
# A Haiku 5.5 round whose prompt passes this many tokens bills at its long-prompt rates.
LONG_PROMPT_TOKENS = 100_000
LONG_PRICES = {"claude-haiku-5-5": (0.50, 2.50, 0.625, 0.05)}
PRICE_WEB_SEARCH = 0.01  # $10 per 1,000 searches
# The models /ask can use. Its web search tool, web_search_20260209, runs on these and not on Haiku 5.5.
WEB_SEARCH_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5")


def token_cost(model: str, used: Any) -> float:
    """Dollars for one round's tokens at list prices. The prompt is every input token, cached or not."""
    prompt = used["input_tokens"] + used["cache_creation_input_tokens"] + used["cache_read_input_tokens"]
    rates = LONG_PRICES.get(model) if prompt > LONG_PROMPT_TOKENS else None
    price_in, price_out, price_write, price_read = rates or PRICES.get(model, max(PRICES.values()))
    return (used["input_tokens"] * price_in + used["output_tokens"] * price_out + used["cache_creation_input_tokens"] * price_write
            + used["cache_read_input_tokens"] * price_read) / 1_000_000

SKETCH_WIDTH = 1200  # pixels
SKETCH_MAX_CHARS = 120_000
CAPTION_MAX = 200
# Each sketch renders in a child process the bot can stop. A thread can't be stopped once it starts, and a few kilobytes of SVG can
# keep cairosvg drawing for half an hour, or ask for a picture a million pixels tall.
SKETCH_SECONDS = 20
SKETCH_MEMORY = 1 << 30  # bytes of address space for the child, where prlimit is on the machine
_SKETCH_CHILD = (
    "import sys\n"
    "sys.path = [entry for entry in sys.path if entry]\n"  # the folder it starts in can't hand it a module
    "import cairosvg\n"
    "png = cairosvg.svg2png(bytestring=sys.stdin.buffer.read(), output_width=int(sys.argv[1]), background_color='white', unsafe=False)\n"
    "sys.stdout.buffer.write(png)\n"
)
# The SVG comes from the model, which reads what members write, so anything that could make the renderer load a file or an address is
# refused before cairosvg sees it. cairosvg's own unsafe=False also blocks outside files and XML entities. Every markup declaration is
# refused too: a CDATA section draws its text, and a comment can split a word, yet both hide those words from the word screen. So is
# <use>, since nested reuse of one group draws it tens of thousands of times from a few kilobytes.
_SVG_REFUSED = re.compile(
    r"<\s*(?:image|foreignObject|script|iframe|object|embed|audio|video|use)\b"
    r"|<!"
    r"|\bhref\s*=\s*[\"'](?!#)"
    r"|url\(\s*[\"']?(?!#)"
    r"|@import",
    re.I,
)

Handler = Callable[[dict, "Answer"], Awaitable[tuple[str, bool]]]


@dataclass
class Answer:
    # ok, skipped, redirect, declined, refused, too_long, busy, unavailable, key_refused, error
    status: str
    model: str = ""
    text: str = ""
    sources: list[tuple[str, str]] = field(default_factory=list)  # (title, url), numbered in this order
    rounds: int = 0
    tool_calls: int = 0
    usage: Counter = field(default_factory=Counter)
    image: bytes | None = None  # the PNG on the answer card: a sketch or a model's preview
    image_caption: str = ""
    image_name: str = "sketch.png"
    image_label: str = "Sketch"
    files: list[tuple[str, bytes]] = field(default_factory=list)  # (file name, data), attached as they are
    screen_text: list[str] = field(default_factory=list)  # the words in the image and the files, for the word screen
    billed: float = 0.0  # dollars for the rounds' tokens, each round priced on its own

    @property
    def cost(self) -> float:
        """Dollars, from the list prices."""
        return self.billed + self.usage["web_search_requests"] * PRICE_WEB_SEARCH


class Sources:
    """The passages shown to the model while it answers one request, tagged S1, S2, and on. Web pages the model cites join the same
    series. With more than one source on a server, each passage also names the source it comes from."""

    def __init__(self, library: Library | None = None) -> None:
        self.items: list[tuple[str, str]] = []  # (address, title), at the tag's number less one
        self._tags: dict[str, int] = {}
        self.library = library
        self.read = 0  # characters of passages handed to the model so far

    def tag(self, section: Section) -> str:
        return self.tag_url(section.source, section.title)

    def tag_url(self, url: str, title: str) -> str:
        if url not in self._tags:
            self.items.append((url, title))
            self._tags[url] = len(self.items)
        return f"S{self._tags[url]}"

    def passage(self, section: Section) -> str:
        title = section.title
        if self.library is not None and len(self.library.shelves) > 1:
            title = f"{title} ({self.library.name_of(section.url)})"
        return f"[{self.tag(section)}] {title}\n{section.source}\n{section.text}"


def run_wiki_tool(name: str, args: dict, sources: Sources, library: Library) -> tuple[str, bool]:
    """(tool result, is_error) for search_wiki and read_wiki_page, across the server's sources. Once the answer has read READ_BUDGET
    characters, both refuse."""
    if name in ("search_wiki", "read_wiki_page") and sources.read >= READ_BUDGET:
        return READ_ALL, True
    found, is_error = _wiki_tool(name, args, sources, library)
    sources.read += len(found)
    return found, is_error


def _wiki_tool(name: str, args: dict, sources: Sources, library: Library) -> tuple[str, bool]:
    if name == "search_wiki":
        query = str(args.get("query", "")).strip()
        hits = library.search(query) if query else []
        if not hits:
            return "No wiki sections matched that search. Try other words or a model code.", False
        return "\n\n".join(sources.passage(section) for section in hits), False
    if name == "read_wiki_page":
        url = library.page_url(str(args.get("url", "")))
        if url is None:
            return "That URL isn't a page in the wiki copy. Use a URL from a search_wiki result.", True
        passages, total = [], 0
        for section in library.page_sections(url):
            if total + len(section.text) > PAGE_CHAR_LIMIT:
                passages.append("(The rest of this page is cut off here.)")
                break
            passages.append(sources.passage(section))
            total += len(section.text)
        if not passages:
            return "That page has no text in the wiki copy.", False
        return "\n\n".join(passages), False
    return f"Unknown tool: {name}", True


_TAG_GROUP = re.compile(r"\s*\[(S\d+(?:\s*,\s*S\d+)*)\]")


def render(text: str, sources: Sources, answer: Answer) -> None:
    """Turn [S4]-style tags into numbered sources.

    Sources are numbered by page, so a long procedure doesn't list a dozen sections of one page. A page cited from one section links
    to that section, and otherwise to the page. Tags the model wasn't given are dropped."""
    numbers: dict[str, int] = {}  # page address -> source number
    cited: dict[str, dict[str, str]] = {}  # page address -> {section address: section title}, in first-cited order

    def replace(match: re.Match) -> str:
        marks: list[str] = []
        for tag in re.findall(r"S(\d+)", match.group(1)):
            index = int(tag) - 1
            if not 0 <= index < len(sources.items):
                continue
            source, title = sources.items[index]
            page = source.split("#", 1)[0]
            if page not in numbers:
                numbers[page] = len(numbers) + 1
                cited[page] = {}
            cited[page].setdefault(source, title)
            mark = f"[{numbers[page]}]"
            if mark not in marks:
                marks.append(mark)
        return " " + "".join(marks) if marks else ""

    rendered = _TAG_GROUP.sub(replace, text)
    # Neighboring sentences from one page would otherwise read "... [1] ... [1] [1]".
    rendered = re.sub(r"(\[\d+\])(?:\s*\1)+", r"\1", rendered)
    answer.text = rendered.strip()
    for page, sections in cited.items():
        if len(sections) == 1:
            ((source, title),) = sections.items()
            answer.sources.append((title, source))
        else:
            first_title = next(iter(sections.values()))
            answer.sources.append((first_title.split(" › ", 1)[0], page))


def cited_text(blocks: list, sources: Sources) -> str:
    """The reply's text blocks joined, with an [S#] tag after each passage the model cited from a web search result, so web pages are
    numbered with the wiki's."""
    parts = []
    for block in blocks:
        text = block.text
        tags: list[str] = []
        for citation in getattr(block, "citations", None) or []:
            url = getattr(citation, "url", None)
            if getattr(citation, "type", "") == "web_search_result_location" and url:
                tag = sources.tag_url(url, getattr(citation, "title", None) or url)
                if tag not in tags:
                    tags.append(tag)
        if tags:
            body = text.rstrip()
            text = body + " " + " ".join(f"[{tag}]" for tag in tags) + text[len(body):]
        parts.append(text)
    return "".join(parts)


_WEB_TAG = re.compile(r"\[W:\s*(https?://[^\]\s]+)\s*\]")


def _url_key(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/").lower()


def collect_web_pages(content: list, pages: dict) -> None:
    """Remember the address and title of every web search result in a response. Search runs in a sandbox on the service's side, so
    the reply's text carries no citations of its own."""
    for block in content:
        if getattr(block, "type", "") != "web_search_tool_result" or not isinstance(getattr(block, "content", None), list):
            continue
        for item in block.content:
            url = getattr(item, "url", None)
            if url:
                pages.setdefault(_url_key(url), (url, getattr(item, "title", None) or url))


def web_tags(text: str, pages: dict, sources: Sources) -> str:
    """Turn [W: url] tags into [S#] tags. An address that wasn't in the search results is dropped, so the model can't cite a page it never saw."""
    def replace(match: re.Match) -> str:
        found = pages.get(_url_key(match.group(1)))
        return f"[{sources.tag_url(*found)}]" if found else ""
    return _WEB_TAG.sub(replace, text)


async def final_message(client: Any, **request: Any) -> Any:
    """One round, streamed, and handed back whole, as the call that doesn't stream would give it. A request that names a beta goes
    to the beta endpoint, the one that takes it."""
    messages = client.beta.messages if "betas" in request else client.messages
    async with messages.stream(**request) as stream:
        return await stream.get_final_message()


def client_timeout() -> anthropic.Timeout:
    return anthropic.Timeout(READ_SECONDS, connect=CONNECT_SECONDS)


def is_word(text: str, word: str, max_len: int) -> bool:
    """Whether the reply is just this control word, give or take a few words."""
    stripped = text.strip().strip(".").upper()
    return stripped.startswith(word) and len(stripped) < max_len


async def converse(client: Any, library: Library, question: str, *, system: str, tools: list, model: str, effort: str, max_rounds: int,
                   auto: bool = False, redirect: bool = False, handlers: dict[str, Handler] | None = None, max_tokens: int = MAX_TOKENS,
                   tool_limits: dict[str, int] | None = None, task_budget: int | None = None) -> Answer:
    """Run one member's request through the model with these tools.

    handlers maps a tool the bot runs, other than the two wiki tools, to an async function(args, answer) returning (result, is_error).
    tool_limits caps how often one answer runs a tool, and MAX_TOOL_CALLS caps them all: a call past either gets an error result telling
    the model to answer from what it has. Server tools such as web search run on the service's side. With redirect set, a reply of
    REDIRECT_WORD has status "redirect", or "skipped" when auto is set.

    task_budget is the tokens the whole answer is told it has, its thinking, tool calls, tool results and reply together. The model
    sees it count down and wraps up inside it, where max_tokens cuts a round off unseen. The service counts what this answer has
    used from the history each round sends, so the budget goes out the same every round."""
    handlers = handlers or {}
    tool_limits = tool_limits or {}
    called: Counter = Counter()
    answer = Answer(status="ok", model=model)
    sources = Sources(library)
    messages: list[dict] = [{"role": "user", "content": question}]
    reply_blocks: list = []  # the text blocks of the final reply, which a paused turn can split
    web_pages: dict[str, tuple[str, str]] = {}  # normalized address -> (address, title) from web search results
    output_config: dict[str, Any] = {"effort": effort}
    extra: dict[str, Any] = {}
    if task_budget:
        output_config["task_budget"] = {"type": "tokens", "total": task_budget}
        extra["betas"] = [TASK_BUDGET_BETA]
    started = time.monotonic()
    try:
        async with asyncio.timeout(ANSWER_SECONDS):
            for round_number in range(1, max_rounds + 1):
                last_round = round_number == max_rounds or time.monotonic() - started >= LAST_ROUND_AFTER
                response = await final_message(
                    client,
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=tools,
                    # On the last round the model has to answer from what it has already read.
                    tool_choice={"type": "none"} if last_round else {"type": "auto"},
                    messages=messages,
                    output_config=output_config,
                    cache_control={"type": "ephemeral"},
                    **extra,
                )
                answer.rounds = round_number
                used = {key: getattr(response.usage, key, 0) or 0 for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
                answer.usage.update(used)
                answer.billed += token_cost(model, used)
                server_tools = getattr(response.usage, "server_tool_use", None)
                if server_tools is not None:
                    answer.usage["web_search_requests"] += getattr(server_tools, "web_search_requests", 0) or 0
                collect_web_pages(response.content, web_pages)

                if response.stop_reason == "refusal":
                    answer.status = "refused"
                    return answer
                # Thinking counts toward max_tokens. A reply cut off there has no text, or ends in an unfinished tool call, so there's nothing to send.
                if response.stop_reason == "max_tokens" and (
                        any(block.type == "tool_use" for block in response.content)
                        or not any(block.type == "text" and block.text.strip() for block in response.content)):
                    answer.status = "too_long"
                    return answer
                if response.stop_reason == "pause_turn" and not last_round:
                    # Web search reached its step limit on the service's side. Sending the turn back resumes it.
                    reply_blocks += [block for block in response.content if block.type == "text"]
                    messages.append({"role": "assistant", "content": response.content})
                    continue
                tool_uses = [block for block in response.content if block.type == "tool_use"]
                if response.stop_reason == "tool_use" and tool_uses and not last_round:
                    reply_blocks = []  # text before a tool call is working notes, not the reply
                    messages.append({"role": "assistant", "content": response.content})
                    results = []
                    for tool in tool_uses:
                        answer.tool_calls += 1
                        called[tool.name] += 1
                        if answer.tool_calls > MAX_TOOL_CALLS:
                            content, is_error = TOO_MANY_CALLS, True
                        elif called[tool.name] > tool_limits.get(tool.name, MAX_TOOL_CALLS):
                            content, is_error = CALLS_USED.format(tool=tool.name), True
                        elif tool.name in handlers:
                            content, is_error = await handlers[tool.name](tool.input, answer)
                        else:
                            content, is_error = run_wiki_tool(tool.name, tool.input, sources, library)
                        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool.id, "content": content}
                        if is_error:
                            result["is_error"] = True
                        results.append(result)
                    messages.append({"role": "user", "content": results})
                    continue

                reply_blocks += [block for block in response.content if block.type == "text"]
                text = web_tags(cited_text(reply_blocks, sources), web_pages, sources)
                if auto and is_word(text, SKIP_WORD, 200):
                    answer.status = "skipped"
                    return answer
                if is_word(text, DECLINE_WORD, 20):
                    answer.status = "skipped" if auto else "declined"
                    return answer
                if redirect and is_word(text, REDIRECT_WORD, 20):
                    answer.status = "skipped" if auto else "redirect"
                    return answer
                render(text, sources, answer)
                if not answer.text:
                    answer.status = "error"
                return answer
            answer.status = "error"
            return answer
    except TimeoutError:
        log.warning("An answer ran past %d minutes after %d rounds", ANSWER_SECONDS // 60, answer.rounds)
        answer.status = "too_slow"
    except anthropic.RateLimitError:
        answer.status = "busy"
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as error:
        log.warning("The AI service refused the API key: %s", error)
        answer.status = "key_refused"
    except anthropic.APIStatusError as error:
        log.warning("The AI service answered %s: %s", error.status_code, error.message)
        # An account out of prepaid credit answers 400 "Your credit balance is too low". An error sent partway through a stream comes
        # on a response that began as 200, so its kind is read from the body.
        out_of_credit = "credit balance" in str(error.message).lower()
        kind = (error.body.get("error") or {}).get("type") if isinstance(error.body, dict) and isinstance(error.body.get("error"), dict) else None
        answer.status = "unavailable" if error.status_code >= 500 or out_of_credit or kind in ("overloaded_error", "api_error") else "error"
    except anthropic.APIConnectionError as error:
        log.warning("The AI service couldn't be reached: %s", error)
        answer.status = "unavailable"
    except Exception:
        # Anything else is a fault here, not the service's. It's logged, and the member gets an answer instead of a stuck "thinking".
        log.error("An AI request failed:\n%s", traceback.format_exc())
        answer.status = "error"
    return answer


# ---- sketches ----

_cairo_loaded: bool | None = None


def sketches_available() -> bool:
    """Whether cairosvg and the cairo library it drives load on this machine."""
    global _cairo_loaded
    if _cairo_loaded is None:
        try:
            import cairosvg  # noqa: F401  (needs the cairo library, which some machines lack)
            _cairo_loaded = True
        except (ImportError, OSError):
            _cairo_loaded = False
    return _cairo_loaded


def render_sketch(svg: str) -> bytes:
    """A model-written SVG as a PNG, drawn in a child process that stops after SKETCH_SECONDS. Raises ValueError for SVG that isn't
    allowed, that doesn't render, or that takes too long."""
    if len(svg) > SKETCH_MAX_CHARS:
        raise ValueError(f"The SVG is over {SKETCH_MAX_CHARS} characters.")
    if "<svg" not in svg:
        raise ValueError("That isn't an SVG document.")
    if _SVG_REFUSED.search(svg):
        raise ValueError("The SVG uses images, reused groups, outside links, scripts, comments, or declarations. Use plain shapes and text only.")
    command = [sys.executable, "-c", _SKETCH_CHILD, str(SKETCH_WIDTH)]
    if shutil.which("prlimit"):
        command = ["prlimit", f"--as={SKETCH_MEMORY}", "--", *command]
    # The child finds cairosvg where this process did, and gets none of this process's secrets.
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "LD_LIBRARY_PATH", "LANG") if name in os.environ}
    env["PYTHONPATH"] = os.pathsep.join(entry for entry in sys.path if entry)
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=os.name != "nt")
    try:
        png, errors = process.communicate(svg.encode("utf-8"), timeout=SKETCH_SECONDS)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.communicate()
        raise ValueError(f"The sketch took over {SKETCH_SECONDS} seconds to draw. Draw fewer shapes.") from None
    if process.returncode != 0:
        lines = errors.decode("utf-8", "replace").strip().splitlines()
        # The last line of the child's traceback, without the exception's name, as the error read when the render ran in this process.
        raise ValueError(re.sub(r"^[\w.]+: ", "", lines[-1]) if lines else "The renderer stopped.")
    return png


# cairosvg (2.9.1, cairosvg/parser.py:224-226 and 246-311) draws a <text>, <textPath> or <a> and everything inside it as one run of text.
# Inside a run, tspan, textPath, a and text draw their own text (cairosvg/surface.py:50-72), a tref draws the text of the element it
# names and then that element's tail in place of its own (parser.py:262-306), and any other element draws only the text after its
# closing tag. The SVG holds no declarations (_SVG_REFUSED), so parsing it here can't expand an entity.
_SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_RUN_ROOTS = ("text", "textPath", "a")
_DRAWS_ITS_TEXT = ("a", "text", "textPath", "tspan")


def _svg_tag(element: Any) -> str:
    """An element's name as cairosvg reads it: bare in the SVG namespace or in none, and with its namespace otherwise
    (parser.py:169-172)."""
    tag = element.tag if isinstance(element.tag, str) else ""
    return tag[len(_SVG_NAMESPACE):] if tag.startswith(_SVG_NAMESPACE) else tag


def _preserves(element: Any, inherited: bool) -> bool:
    """Whether xml:space is preserve for an element, which keeps its parent's setting unless it has its own (parser.py:180-184)."""
    value = element.get(_XML_SPACE)
    return inherited if value is None else value == "preserve"


def _white_spaces(text: str | None, preserve: bool) -> str:
    """Text as cairosvg's handle_white_spaces leaves it (parser.py:57-70): with xml:space preserve, each line break and tab becomes a
    space, and otherwise line breaks are dropped, tabs become spaces and runs of spaces become one."""
    if not text:
        return ""
    if preserve:
        return re.sub("[\n\r\t]", " ", text)
    return re.sub(" +", " ", re.sub("\t", " ", re.sub("[\n\r]", "", text)))


def _run_text(root: Any, preserve: bool, ids: dict[str, Any]) -> str:
    """The text cairosvg draws for one run, in order, with nothing where its tags are."""
    pieces = [_white_spaces(root.text, preserve)]
    steps: list[tuple[str, Any, bool]] = [("element", child, preserve) for child in list(root)[::-1]]
    while steps:
        step, item, inherited = steps.pop()
        if step == "tail":
            pieces.append(_white_spaces(item, inherited))
            continue
        own = _preserves(item, inherited)
        tag = _svg_tag(item)
        if tag == "tref":
            href = (item.get(_XLINK_HREF) or item.get("href") or "").strip()
            target = ids.get(href[1:]) if href.startswith("#") else None
            if target is not None:
                pieces.append(_white_spaces("".join(target.itertext()), own))
                pieces.append(_white_spaces(target.tail, inherited))
            continue
        if tag in _DRAWS_ITS_TEXT:
            pieces.append(_white_spaces(item.text, own))
        steps.append(("tail", item.tail, inherited))
        steps.extend(("element", child, own) for child in list(item)[::-1])
    return "".join(pieces)


def sketch_texts(svg: str) -> list[str]:
    """The words a sketch draws, for the word screen. One reading puts a space at every tag. Beside it, when it differs, another joins
    each run of text the way cairosvg draws it, so a word split across tspans or lines reads whole, with a space between runs."""
    spaced = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", svg)).split())
    try:
        tree = ElementTree.fromstring(svg.encode("utf-8"))
    except ElementTree.ParseError:  # cairosvg drew it, so it parses, and if it didn't every tag would come out without a space
        runs = [html.unescape(re.sub(r"<[^>]+>", "", svg))]
    else:
        ids: dict[str, Any] = {}
        for element in tree.iter():
            if element.get("id") is not None:
                ids.setdefault(element.get("id"), element)
        runs = []
        elements = [(tree, False)]
        while elements:
            element, inherited = elements.pop()
            own = _preserves(element, inherited)
            if _svg_tag(element) in _RUN_ROOTS:
                runs.append(_run_text(element, own, ids))
            else:
                elements.extend((child, own) for child in list(element)[::-1])
    joined = " ".join(words for words in (" ".join(run.split()) for run in runs) if words)
    return [spaced, joined] if joined and joined != spaced else [spaced]


async def draw_sketch(args: dict, answer: Answer) -> tuple[str, bool]:
    if answer.image is not None:
        return "A sketch is already attached. Don't draw another.", True
    if not sketches_available():
        return "Sketches aren't available on this bot. Write the plan without one.", True
    svg = str(args.get("svg", ""))
    try:
        answer.image = await asyncio.to_thread(render_sketch, svg)
    except Exception as error:  # cairosvg raises a range of parser errors on malformed SVG
        return f"The sketch didn't render: {error}. Fix the SVG and call draw_sketch again, or skip it.", True
    answer.image_caption = str(args.get("caption", "")).strip()[:CAPTION_MAX]
    # The words drawn in the sketch, with character references decoded the way the renderer reads them.
    answer.screen_text = await asyncio.to_thread(sketch_texts, svg)
    return "The sketch rendered and will be attached under your reply.", False
