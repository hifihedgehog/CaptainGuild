"""Builds the public docs page from the code, so its command list can't drift.

    python scripts/build_docs.py

Reads every command from the modules and docs/template-language.md, and writes captainguild/web/static/docs.html. Run it with the
project's Python after changing a command.
"""
from __future__ import annotations

import asyncio
import html
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from captainguild.bot import Bot  # noqa: E402
from captainguild.core.perms import PERMISSION_LABELS  # noqa: E402
from captainguild.core.shortcuts import BARE_FORMS, SHORTCUTS, TAG_FIRST_SHORTCUTS  # noqa: E402
from captainguild.db import Database  # noqa: E402
from captainguild.modules import load_all  # noqa: E402

KEY = "3" * 64
PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Docs - Captain Guild</title>
  <link rel="icon" href="/static/img/logo.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/static/css/site.css">
</head>
<body class="site">
  <header class="site-top">
    <a class="brand" href="/"><img src="/static/img/logo.svg" alt="">Captain Guild</a>
    <nav><a href="/docs">Docs</a><a href="/status">Status</a><a href="/privacy">Privacy</a><a class="btn primary" href="/servers">Dashboard</a></nav>
  </header>
  <main class="site-main">
    <h1>Docs</h1>
    <div class="toc"><ul>{toc}</ul></div>
{body}
    <div class="site-foot"><a href="/docs">Docs</a><a href="/status">Status</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></div>
  </main>
</body>
</html>
"""

GUIDE = """
<h2 id="start">Getting started</h2>
<p>Click Dashboard at the top of this page. Discord asks you to confirm who you are, and then you pick a server. The bot's <code>/dashboard</code> command also answers with a private link that signs you in. You need Manage Server to change settings. Each page in the sidebar is one feature. Most features do nothing until you set them up. The bot keeps recent message text for the deleted and edited message logs, 30 days unless you change it.</p>
<p>Move the bot's role above every role you want it to hand out. Discord doesn't let a bot give or take a role at or above its own.</p>

<h2 id="proposals">Proposals</h2>
<p>A proposal is a change to the server put to two votes on one post. A moderator submits it with <code>/submit_proposal</code> or from the Proposals page. The bot posts it, opens a discussion thread and adds the two vote reactions.</p>
<p>Members have the voting time set on the page, 72 hours unless you change it. Their vote passes if more than half of the votes are in favor. Moderators vote with the same reactions, counted apart, with no deadline. Their vote is decided once more than half of all moderators agree. A proposal is approved only if both votes pass, and it's rejected as soon as either one fails.</p>
<p>Once the member vote is over, moderators who haven't voted get a DM every 24 hours until their vote is decided. <code>/proposal promote</code> or Put to a vote on the Suggestions page starts a proposal from a suggestion, and the result becomes that suggestion's decision.</p>

<h2 id="rolerules">Role rules</h2>
<p>A role rule adds or removes roles for members who match all of its conditions, up to ten. A condition looks at a set of roles: whether a member has any of them, all of them, none of them, or is missing one, or how many of them they hold. Rules run from the top of the list on the Role rules page, and a change one rule makes can set off the rules below it.</p>
<p>A rule that keeps in sync takes its added roles back from members who stop matching, unless another rule that matches them adds the same role. Without it, a role stays until something else takes it. <code>/rolerule create</code> makes a rule that keeps one target role on everyone who holds any of the tracked roles, with an optional ignored role that keeps people out and a required role they must also hold.</p>
<p>The bot looks at a member when their roles change and when they pass the rules screen, at everyone when a rule is saved or switched on, and at everyone again every 10 minutes. Each member gets one role edit for all of their changes, and edits go out at about one a second. Preview on the page shows who would gain and lose each role before you save, and a save that changes more than 200 members asks you to confirm the count. Rules that keep undoing each other's changes are switched off, and so is a rule that checks for a role someone deleted. The server log channel says which ones.</p>
<p><code>/mass_mention</code> pings every member who holds all of the roles you name, up to 1,000 of them, and asks first when it's more than 50. It needs Mention Everyone, and the roles named in its first line don't ping.</p>

<h2 id="commands-work">How commands work</h2>
<p>Most commands work three ways: as a slash command, after the server's prefix (<code>!warn @someone spamming</code>), and after mentioning the bot. A command listed below with <code>!</code> in front has no slash form.</p>
<p>A command needs its Discord permission, shown beside it below. A group whose subcommands differ says Varies, and each subcommand shows its own. On the Commands page you can grant bypass roles per command, so a moderator role can use a command without holding that permission in Discord. The same page limits a command to chosen channels or roles, turns it off, or sends its output somewhere else.</p>
<p>A command marked Staff, Moderators or Veterans checks for that group itself, and the group is set on that feature's page. A bypass role doesn't open it.</p>
<p>The bot manager role counts as holding every permission, Ban Members included. Only the server owner or an Administrator can set it, or any other role that makes people staff. To add a bypass role, you need the permission it skips, and the role has to sit below your highest. @everyone can't hold any of these. Manage Server and Administrator skip channel and role limits. A turned-off command is off for everyone, except that <code>/dashboard</code> and <code>/my_data</code> always work, and the owner and Administrators always keep <code>/config</code>.</p>

<h2 id="settings">Settings by page</h2>
<p>Each page in the sidebar holds its own settings, and most explain themselves there. These change how a feature behaves. You need Manage Server to change them.</p>
<table><thead><tr><th>Page</th><th>Setting</th><th>What it does</th></tr></thead><tbody>
<tr><td>Automod</td><td>Pause the sender</td><td>A timeout of up to 59 seconds on every hit of a rule, to break up a burst. It makes no case. 0 turns it off.</td></tr>
<tr><td>Automod</td><td>Copied text</td><td>Counts one text in one channel, whoever posts it, and deleting takes the earlier copies too. Text under eight letters doesn't count.</td></tr>
<tr><td>Automod</td><td>Bad names</td><td>Reads usernames and nicknames against the bad word list, when someone joins and when they change one.</td></tr>
<tr><td>Automod</td><td>Punish above</td><td>How many warnings someone can hold before a punishment. More steps can follow, such as a mute above three and a ban above five.</td></tr>
<tr><td>Autoroles</td><td>Timed roles</td><td>Gives a role some time after someone joins.</td></tr>
<tr><td>Autoroles</td><td>Skip bots</td><td>Bots that join get no autoroles or timed roles.</td></tr>
<tr><td>Greetings</td><td>Announcement Time</td><td>The hour birthdays go out. The whole server shares one time.</td></tr>
<tr><td>Greetings</td><td>Joins in a minute before welcomes pause</td><td>Past this many, the bot skips each welcome and join DM, and posts one line with the count once joins go quiet. 0 welcomes everyone.</td></tr>
<tr><td>Levels</td><td>Panel opacity</td><td>How solid the dark panel on a rank card is, from clear to solid.</td></tr>
<tr><td>Logging</td><td>Moderator delete log channel</td><td>Deletes where the audit log names a moderator or bot, and purges. Empty keeps them in the message log channel.</td></tr>
<tr><td>Logging</td><td>Role log channel</td><td>Members' role changes. Empty keeps them in the member log channel.</td></tr>
<tr><td>Logging</td><td>Name log channel</td><td>Name and nickname changes. Empty keeps them in the member log channel.</td></tr>
<tr><td>Logging</td><td>Timeout log channel</td><td>Timeouts and lifted timeouts. Empty keeps them in the member log channel.</td></tr>
<tr><td>Starboard</td><td>Private channels</td><td>A message stays off the board when someone who can open the board couldn't read its channel. A channel or category listed here goes up anyway.</td></tr>
<tr><td>Voice controls</td><td>Record mini mutes as cases</td><td>Off, a mini mute writes only to its log channel and that page. On, it also goes in the mod log, the member's case list and moderator rankings.</td></tr>
</tbody></table>
<p>New Twitch alerts go to the channel the command is used in. <code>!twitch default_channel</code> sets another one.</p>

<h2 id="data">Your data</h2>
<p>Any member can type <code>/my_data</code> for a private link to a page that shows what the bot holds about them, gives them a copy, and deletes what's theirs to delete.</p>
<p>The Backup page exports everything stored for a server as one file and can put it back. Scheduled snapshots stay on the server. The Activity page shows who changed which setting, with undo.</p>
"""


def inline_markdown(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', out)
    return out


def markdown(source: str) -> str:
    """Enough Markdown for the reference file: headings, paragraphs, lists, tables and fenced code."""
    out: list[str] = []
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            block = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(block), quote=False)}</code></pre>")
            continue
        match = re.match(r"(#{1,4}) (.+)", line)
        if match:
            level = min(len(match.group(1)) + 1, 5)
            out.append(f"<h{level}>{inline_markdown(match.group(2))}</h{level}>")
            index += 1
            continue
        if line.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                rows.append(lines[index])
                index += 1
            cells = [[c.strip() for c in re.split(r"(?<!\\)\|", row.strip().strip("|"))] for row in rows]
            head, body = cells[0], [c for c in cells[1:] if not all(re.fullmatch(r":?-+:?", x) for x in c)]
            table = ["<table><thead><tr>"] + [f"<th>{inline_markdown(c)}</th>" for c in head] + ["</tr></thead><tbody>"]
            for row in body:
                table.append("<tr>" + "".join(f"<td>{inline_markdown(c.replace(chr(92) + '|', '|'))}</td>" for c in row) + "</tr>")
            table.append("</tbody></table>")
            out.append("".join(table))
            continue
        if re.match(r"[-*] ", line):
            items = []
            while index < len(lines) and re.match(r"[-*] ", lines[index]):
                items.append(f"<li>{inline_markdown(lines[index][2:])}</li>")
                index += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        if line.strip():
            paragraph = [line]
            index += 1
            while index < len(lines) and lines[index].strip() and not re.match(r"(#{1,4} |[-*] |\||```)", lines[index]):
                paragraph.append(lines[index])
                index += 1
            out.append(f"<p>{inline_markdown(' '.join(paragraph))}</p>")
            continue
        index += 1
    return "\n".join(out)


def needs_text(permissions: list[str], staff: str | None) -> str:
    """What one command needs, in words. A check the command makes itself is the whole story, since it's the stricter one."""
    if staff:
        return staff
    return ", ".join(PERMISSION_LABELS.get(p) or p.replace("_", " ").title() for p in permissions) or "None"


def child_label(group: str, child: dict) -> str:
    """A subcommand's short name, with the prefix if it has no slash form, and what it needs if anything."""
    short = html.escape(child["name"].split(" ", 1)[-1])
    needs = needs_text(child["permissions"], child["staff"])
    tail = f" ({html.escape(needs)})" if needs != "None" else ""
    return f"<code>{'!' + html.escape(group) + ' ' if child['text_only'] else ''}{short}</code>{tail}"


def command_tables(categories: list[dict]) -> tuple[str, list[tuple[str, str]]]:
    parts = []
    toc = []
    for group in categories:
        anchor = "c-" + re.sub(r"[^a-z0-9]+", "-", group["category"].lower())
        toc.append((anchor, group["category"]))
        parts.append(f'<h3 id="{anchor}">{html.escape(group["category"])}</h3>')
        rows = []
        for row in group["commands"]:
            if row["varies"]:
                needs = "Varies"
            elif row["children"]:
                first = row["children"][0]
                needs = needs_text(first["permissions"], first["staff"])
            else:
                needs = needs_text(row["permissions"], row["staff"])
            subcommands = ""
            if row["children"]:
                # With every subcommand alike, the row's own column says it once.
                names = ", ".join(child_label(row["name"], child) if row["varies"] else child_label(row["name"], {**child, "permissions": [], "staff": None}) for child in row["children"])
                subcommands = f'<div class="small muted">{names}</div>'
            if row["aliases"]:
                subcommands += '<div class="small muted">Also answers to ' + ", ".join(f"<code>{html.escape(alias)}</code>" for alias in row["aliases"]) + "</div>"
            start = "!" if row["text_only"] else "/"
            rows.append(f'<tr><td><code>{start}{html.escape(row["name"])}</code></td><td>{html.escape(row["description"])}{subcommands}</td><td class="small">{html.escape(needs)}</td></tr>')
        parts.append("<table><thead><tr><th>Command</th><th>What it does</th><th>Needs</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")
    return "\n".join(parts), toc


def page_text(categories: list[dict]) -> str:
    """The whole page, from the command catalog and the reference file."""
    tables, command_toc = command_tables(categories)
    template = ROOT / "docs" / "template-language.md"
    reference = markdown(template.read_text(encoding="utf-8")) if template.exists() else ""
    toc = [("start", "Getting started"), ("proposals", "Proposals"), ("commands-work", "How commands work"), ("shortcuts", "Text shortcuts"), ("settings", "Settings by page"), ("data", "Your data"), ("commands", "Commands")]
    if reference:
        toc.append(("templates", "Template language"))
    shortcuts = "".join(f"<tr><td><code>{html.escape(name)}</code></td><td><code>{html.escape(target)}</code></td></tr>" for name, target in sorted({**SHORTCUTS, **TAG_FIRST_SHORTCUTS}.items()))
    bare = "".join(f"<tr><td><code>{html.escape(name)} ...</code></td><td><code>{html.escape(target)} ...</code></td></tr>" for name, target in sorted(BARE_FORMS.items()))
    body = GUIDE + '<h2 id="shortcuts">Text shortcuts</h2>\n<p>These short names work when you type a command with the server\'s prefix. Each one runs the longer command under the same rules.</p>\n<table><thead><tr><th>Type</th><th>It runs</th></tr></thead><tbody>' + shortcuts + '</tbody></table>\n<p>A word a typed command can\'t use is refused with the command\'s usage, and nothing changes. These commands also take text straight after their name:</p>\n<table><thead><tr><th>Type</th><th>It runs</th></tr></thead><tbody>' + bare + '</tbody></table>\n<h2 id="commands">Commands</h2>\n' + tables
    if reference:
        body += '\n<h2 id="templates">Template language</h2>\n' + reference
    toc_html = "".join(f'<li><a href="#{anchor}">{html.escape(name)}</a></li>' for anchor, name in toc)
    return PAGE.format(toc=toc_html, body=body)


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        db = Database(Path(folder) / "a.db", KEY)
        message_db = Database(Path(folder) / "b.db", KEY)
        await db.open()
        await message_db.open()
        try:
            bot = Bot(db, message_db)
            await bot.prepare(load_all())
            categories = bot.command_catalog()
        finally:
            await db.close()
            await message_db.close()
    target = ROOT / "captainguild" / "web" / "static" / "docs.html"
    target.write_text(page_text(categories), encoding="utf-8", newline="\n")
    print("wrote", target, "commands:", sum(len(g["commands"]) for g in categories))


if __name__ == "__main__":
    asyncio.run(main())
