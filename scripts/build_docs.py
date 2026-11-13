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

<h2 id="collaborative">Collaborative messages</h2>
<p>A collaborative message is a post the bot makes that the people and roles you name can edit. Switch it on from the Collaborative messages page. <code>/create_collaborative_message</code> posts one in the channel you use it in, as plain text, or as an embed when you give a color, and you become its first collaborator. The owner and Administrators can always make one, and the page names roles that can too.</p>
<p>Collaborators right-click the message for five menus. Edit Collaborative Message opens its text in a form. View Revision History shows its earlier texts and puts one back, and Revert to Previous Revision goes back one text at a time. A revert keeps the text it replaces, so nothing in the history is lost. Delete Collaborative Message removes it, and See Current Collaborators lists who can edit it. The owner and Administrators can change any of them.</p>
<p><code>/move_collaborative_message</code> and <code>/copy_collaborative_message</code> repost one in another channel or thread with its history and its collaborators, and a move deletes the original. Whoever moves or copies one needs to be able to post where it goes.</p>

<h2 id="mover">Moving messages</h2>
<p>With the Message mover page switched on, members right-click their own message, pick Move Message or Copy Message, and pick a channel or thread. The bot reposts it there under their name and picture, with its text, files and embeds, and pings nobody. Moving deletes the original. Anyone with Manage Messages in that channel, or the bot manager role, can move or copy anyone's message.</p>
<p>Whoever moved or copied a message can change its text with Edit Moved Message, or take it down with Delete Moved Message. A message that would lose a sticker, a poll, its buttons or a file too big to upload stays where it is, and a copy says what it left behind. The bot needs Manage Webhooks where it posts, and Manage Messages to delete an original.</p>

<h2 id="translation">Translation and conversions</h2>
<p>A server switches this pack on from its Translate &amp; convert page. <code>/translate</code> translates text between 36 languages with DeepL. The Translate menu on a message shows its translation to the person who asked, in the language they picked with <code>/translate_language</code>, else their Discord language, else English. A message that starts with translate, such as <code>translate hola to english</code>, gets a translation as a reply, and <code>translate</code> alone as a reply translates the message it answers. Reacting to a message with a country's flag posts a translation into that country's language, deleted after 120 seconds unless <code>/translate_autodelete</code> or the page sets another time. Each person gets 5 translations in 30 seconds.</p>
<p><code>/currency</code> and <code>/units</code> convert, and so does a message that asks, such as <code>20 bucks in euros</code>, <code>100 usd eur</code> or <code>5 km to miles</code>. Exchange rates come from a free daily rate list, which the bot checks once an hour. The page switches each kind of answer off and leaves channels out. <code>/export_emojis</code> sends the server's custom emojis as ZIP files to the Administrator who asked.</p>

<h2 id="packs">Bookmarks, RSS feeds, the channel archive and entry giveaways</h2>
<p>These are off until a manager switches each one on from its page. Their commands and right-click menus then reach that server within a minute, and other servers don't see them.</p>
<p>A giveaway channel takes one entry from each member. The first message counts, and a second is deleted with a warning. In a forum each post is its own giveaway. Closing a giveaway lets people talk there. <code>/draw</code> reads what was posted while the giveaway was open and gives one chance to each member who posted, leaving out bots, the staff role and anyone who has left the server.</p>
<p><code>/copy_channel</code> copies a channel or a forum, threads included, into another channel or thread, one message every two seconds, each under its author's name and picture. It needs Manage Webhooks, copies only the threads the person starting it can read, and a later copy into the same place brings only what's new.</p>

<h2 id="reddit">Reddit posts</h2>
<p>Reddit posts is a command pack, off until a manager switches it on from its page. <code>/reddit add</code> follows an account, and each post the account makes from then on goes to the channel in full: the title as a heading, the post's own words with their headings, lists, bold and links, and its pictures and video uploaded with it. A post too long for one message goes out as several, each ending where the post shows a picture. The last message ends with a line that mentions the roles the server chose, and nothing in a post can ping anyone. <code>/reddit post</code> relays any one post by its link.</p>
<p>The bot reads each followed account's own feed, one account a minute. A server can also set up a courier on the page: a small app on Reddit's developer platform, in the <code>courier</code> folder of the source, which its owner uploads under their own Reddit account. The courier hands each new post to a webhook in a channel the server picks, and the bot reads it from there. A post that arrives both ways goes out once. A post with the same title as one that went to the same channel earlier that day is passed over, since one announcement is often posted in several communities or by two accounts.</p>

<h2 id="answers">Wiki answers</h2>
<p>Wiki answers is a command pack, off until an Administrator switches it on. <code>/how_do_i</code> answers from the server's sources alone. <code>/brainstorm</code> plans a print or laser project and draws a sketch when asked. <code>/ask</code> also searches the web, for prices, other brands and what the sources don't cover. <code>/make_model</code> writes the part in OpenSCAD and posts a 3MF file, its source and a preview. Each answer links the pages it drew from, and its footer names their sources.</p>
<p>A server names up to five sources. A source is a wiki or documentation site, found from its sitemap or from a start page whose links the bot follows under a path. The bot keeps its own copy of each, up to 25 million characters, and updates it on the schedule the source sets. It reads MediaWiki, DokuWiki, Wiki.js, Confluence, MkDocs, Docusaurus and GitBook pages by their markup, a source can name the article with a CSS selector, and on any other page it takes the largest block of text. <code>/refresh_wiki</code> updates every source now. <code>/auto_answer</code> picks forums where each new post gets one answer while it waits for a helper.</p>
<p>Each request is billed to the bot host's own Anthropic account, so the host names the servers that may use it, and only the owner or an Administrator changes these settings. Each person gets the server's daily number of requests, 25 unless it's changed, and three a minute. A request nothing was billed for doesn't count, and neither do the first three a day that <code>/how_do_i</code> or <code>/brainstorm</code> sends to <code>/ask</code>. A server can set a monthly budget, which starts at $200 when it first switches the pack on, and 0 means none. Once the month's answers have cost that much, the commands and automatic answers stop until the 1st. The page shows what this month's answers cost.</p>
<p>The bot's host puts the API key in <code>.env</code> as <code>ANTHROPIC_API_KEY</code> and starts the bot with <code>--answers-servers</code> followed by the IDs of the servers that may use it, separated by commas. In any other server the pack can't be switched on. <code>/make_model</code> needs the <code>openscad</code>, <code>xvfb-run</code> and <code>prlimit</code> programs, and sketches need the cairo library. Without them those parts say they're unavailable, and the rest works. Requests and answers are checked against the server's automod bad-word rule, and an answer it would flag isn't posted. A host that runs its own word filter can start the bot with <code>--term-matching</code> and <code>--term-configs</code>, naming that filter's matching module and its folder of per-server lists. The answer card is then censored the way the filter censors a message.</p>

<h2 id="forummessages">Forum messages</h2>
<p>Forum messages is a command pack, off until a manager switches it on from its page. Each forum it lists can have a welcome, posted in each new post once its author's first message is in, and a reminder, posted once in a post that has had no message for six days. Placeholders such as <code>{{OP.Mention}}</code> fill in the post's author. A welcome or a reminder pings the author and the people and roles its own text names.</p>
<p>With a solved tag set, the welcome carries a Mark as Solved button, and right-clicking a message gives Mark as Solution, which also pins it. The post's author, the forum's helper roles, anyone with Manage Threads in the forum and roles that skip Manage Threads on <code>/close_solved_threads</code> can use them, and either one adds the solved tag and closes the post. The reminder needs the solved tag too, since it asks for the solution to be marked. <code>/close_solved_threads</code> closes every open post with the tag, <code>/clean_threads</code> takes members with the roles you name out of every open post, and <code>/check_recent_threads</code> posts the welcome in recent posts that missed it.</p>

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
<tr><td>Greetings</td><td>Post the welcome message</td><td>Off keeps the message and posts nothing. The leave message, the ban message and the join DM each have the same switch.</td></tr>
<tr><td>Greetings</td><td>Wait until they accept the rules</td><td>With Discord's rules screen on, the welcome and the join DM go out when the member accepts the rules.</td></tr>
<tr><td>Greetings</td><td>Who the welcome pings</td><td>The member alone, or also the roles the message names, or also @everyone and @here when the message has them. Picking roles or everyone takes your own right to ping in that channel.</td></tr>
<tr><td>Greetings</td><td>Who the leave message pings</td><td>The same three choices as the welcome, for the leave message. The ban message has its own.</td></tr>
<tr><td>Greetings</td><td>Welcome bots too</td><td>Off, a bot that joins gets no welcome.</td></tr>
<tr><td>Greetings</td><td>Delete each welcome after</td><td>Removes each welcome that long after it went out, up to 30 days. Leave and ban messages have their own. 0 keeps them.</td></tr>
<tr><td>Greetings</td><td>Delete a member's welcome when they leave</td><td>Their welcome goes when they leave, are kicked or are banned.</td></tr>
<tr><td>Greetings</td><td>Minutes before a rejoin is welcomed again</td><td>Someone who leaves and comes back inside this time gets no second welcome or join DM. 0 welcomes every join.</td></tr>
<tr><td>Greetings</td><td>Send the welcome message as the DM</td><td>The join DM sends the welcome message in place of its own.</td></tr>
<tr><td>Greetings</td><td>People who have joined so far</td><td>A count of every join, which goes up when members leave too. <code>{joined}</code> shows it in a message or on the picture, and you can set it.</td></tr>
<tr><td>Welcome image</td><td>Send the picture with each welcome message</td><td>Draws a picture for each newcomer with their avatar and your text, and posts it with the welcome.</td></tr>
<tr><td>Welcome image</td><td>Show it inside the message's embed</td><td>On, the picture sits in the welcome's first embed. Off, it is a plain attachment beside the text.</td></tr>
<tr><td>Welcome image</td><td>Send it with the join DM too</td><td>The same picture goes in the join DM.</td></tr>
<tr><td>Welcome image</td><td>Keep an animated background moving</td><td>With an animated GIF as the background, the picture goes out as a GIF. Off draws its first frame.</td></tr>
<tr><td>Welcome image</td><td>Text position</td><td>Where the text sits in its space. Automatic follows the layout.</td></tr>
<tr><td>Welcome image</td><td>Put a frame around the picture</td><td>A frame in the color and width you pick, which grows the picture on every side.</td></tr>
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
    toc = [("start", "Getting started"), ("proposals", "Proposals"), ("answers", "Wiki answers"), ("commands-work", "How commands work"), ("shortcuts", "Text shortcuts"), ("settings", "Settings by page"), ("data", "Your data"), ("commands", "Commands")]
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
