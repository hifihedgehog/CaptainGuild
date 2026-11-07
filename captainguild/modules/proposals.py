"""Proposals: a change to the server put to two votes, one by members and one by moderators.

A moderator writes a proposal. The bot posts it with a discussion thread, and members and moderators vote with the same two reactions, counted apart.
Members have a set time. Moderators have no deadline, and their vote is decided once more than half of all moderators agree. A proposal passes only if
both votes pass, and it fails as soon as either one fails.

The reactions on the post are the votes, so nothing here stores who voted which way, and votes cast while the bot was down still count.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..core import delivery
from ..core import members as member_lists
from ..core.messages import embed_size
from ..core.module import Module, requires, staff_only
from ..core.ops import MessageGone, OpsError
from ..core.settings import Actor
from ..core.validate import Field, Invalid, Schema, ValidationError, number, optional, page_number, snowflake, text
from ..web.api import ApiError

log = logging.getLogger(__name__)

UP = "\N{THUMBS UP SIGN}"
DOWN = "\N{THUMBS DOWN SIGN}"
CONTENT_MAX = 4000
CREDIT_MAX = 200
REASON_MAX = 500
PAGE_MAX = 100
NUMBER_MAX = 2 ** 31
CHECK_SECONDS = 300.0  # the slowest a closing vote or a change in who's a moderator goes unnoticed
LONGEST_COUNT_WAIT = 60.0  # the numbers on the post needn't be current to the second on a vote this big
EMBEDS_MAX = 6000  # what Discord takes across the embeds of one message
CUT_NOTE = "\n\nThe whole text is on the dashboard."
MESSAGE_ROOM = 1900
TIMER = "proposal.check"

OPEN = "open"
APPROVED = "approved"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
STATE_LABELS = {OPEN: "Voting", APPROVED: "Approved", REJECTED: "Rejected", WITHDRAWN: "Withdrawn"}
COLORS = {OPEN: 0x00A2F8, APPROVED: 0x2ECC71, REJECTED: 0xE74C3C, WITHDRAWN: 0x4F545C}


class ProposalError(Exception):
    """Text for the person trying to act on a proposal."""


def split_votes(up: set[int], down: set[int], moderators: set[int]) -> dict[str, int]:
    """Someone on both reactions counts for neither, and a moderator's vote never counts as a member's."""
    only_up, only_down = up - down, down - up
    return {
        "community_up": len(only_up - moderators), "community_down": len(only_down - moderators),
        "mod_up": len(only_up & moderators), "mod_down": len(only_down & moderators),
    }


def moderators_decided(up: int, down: int, total: int) -> bool | None:
    """True or False once more than half of all moderators agree. None until then."""
    if up > total / 2 or down > total / 2:
        return up > down
    return None


def outcome(row: dict) -> tuple[str, str | None]:
    """Where a proposal stands, and why it failed if it did."""
    community_failed = bool(row["community_done"] and not row["community_pass"])
    mods_failed = bool(row["mod_done"] and not row["mod_pass"])
    if community_failed and mods_failed:
        return REJECTED, "Community and moderator votes failed"
    if community_failed:
        return REJECTED, "Community vote failed"
    if mods_failed:
        return REJECTED, "Moderator vote failed"
    if row["community_done"] and row["mod_done"]:
        return APPROVED, None
    return OPEN, None


def _hours(value: float) -> str:
    return f"{value:g} hour" + ("" if value == 1 else "s")


class ProposalForm(discord.ui.Modal, title="Submit Proposal"):
    def __init__(self, module: "Proposals", *, content: str = "", credit: str = "", suggestion: int | None = None) -> None:
        super().__init__()
        self.module = module
        self.suggestion = suggestion
        self.content = discord.ui.TextInput(label="Proposal Description", style=discord.TextStyle.paragraph, max_length=CONTENT_MAX, default=content[:CONTENT_MAX] or None)
        self.credit = discord.ui.TextInput(label="Credit (optional)", required=False, max_length=CREDIT_MAX, placeholder="Who suggested it, or a link to the post", default=credit[:CREDIT_MAX] or None)
        self.add_item(self.content)
        self.add_item(self.credit)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        actor = self.module.bot.actor(interaction.user)
        try:
            row = await self.module.submit(interaction.guild.id, actor, self.content.value, credit=self.credit.value, suggestion=self.suggestion)
        except (ProposalError, OpsError, Invalid) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await interaction.followup.send(f"Proposal #{row['number']} is up for a vote: {row['url']}", ephemeral=True)


class Proposals(Module):
    key = "proposals"
    category = "Suggestions"
    schema = Schema({
        "channel_id": Field(None, optional(snowflake)),
        "moderator_role": Field(None, optional(snowflake)),
        "participation_role": Field(None, optional(snowflake)),
        "vote_hours": Field(72.0, number(0.1, 8760)),
        "reminder_hours": Field(24.0, number(0, 8760)),
        "results_channel": Field(None, optional(snowflake)),
        "title": Field("Server Change Proposal", text(200, minimum=1)),
    })
    migrations = [
        """
        CREATE TABLE proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            number INTEGER NOT NULL,
            channel_id INTEGER,
            message_id INTEGER,
            thread_id INTEGER,
            pings TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            credit TEXT,
            submitter_id INTEGER NOT NULL,
            submitter_name TEXT NOT NULL,
            suggestion_number INTEGER,
            started_at REAL NOT NULL,
            vote_hours REAL NOT NULL,
            ends_at REAL NOT NULL,
            community_up INTEGER NOT NULL DEFAULT 0,
            community_down INTEGER NOT NULL DEFAULT 0,
            community_done INTEGER NOT NULL DEFAULT 0,
            community_pass INTEGER NOT NULL DEFAULT 0,
            mod_up INTEGER NOT NULL DEFAULT 0,
            mod_down INTEGER NOT NULL DEFAULT 0,
            mod_done INTEGER NOT NULL DEFAULT 0,
            mod_pass INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'open',
            status_reason TEXT,
            last_reminder_at REAL,
            finished_at REAL,
            rendered INTEGER NOT NULL DEFAULT 1,
            imported INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX proposals_number ON proposals (guild_id, number);
        CREATE INDEX proposals_message ON proposals (guild_id, message_id);
        CREATE INDEX proposals_state ON proposals (guild_id, state, number DESC);
        """,
        # Set in the same write as a final state, and cleared once the result has gone out, so a restart between the two still sends it.
        "ALTER TABLE proposals ADD COLUMN finish_pending INTEGER NOT NULL DEFAULT 0;",
    ]
    REFRESH_DELAY = 3.0  # votes that land together cost one count and one edit

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._voting: set[int] = set()  # the posts still open, so other servers' reactions cost nothing
        self._soon: dict[tuple[int, int], asyncio.Future] = {}  # one count running or waiting per proposal
        self._again: set[tuple[int, int]] = set()  # votes landed since that count started, so it goes round once more
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._told_no_role: set[tuple[int, int]] = set()

    def staff_roles(self, settings: dict) -> dict[str, set[int]]:
        return {"moderator_role": {settings["moderator_role"]} if settings["moderator_role"] else set()}

    def power_roles(self, settings: dict) -> dict[str, set[int]]:
        return {"participation_role": {settings["participation_role"]} if settings["participation_role"] else set()}

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {name: {settings[name]} if settings[name] else set() for name in ("channel_id", "results_channel")}

    def pinged_channels(self, settings: dict) -> dict[str, set[int]]:
        channel = settings["channel_id"]
        guild = getattr(self.bot.get_channel(channel), "guild", None) if channel else None
        return {f"{field}:{settings[field]}": {channel} for field in ("participation_role", "moderator_role") if settings[field] and delivery.needs_ping_right(guild, settings[field])}

    async def on_load(self) -> None:
        self.bot.timers.handler(TIMER, self._check_due)
        await self._reload()

    async def on_guild_restored(self, guild_id: int) -> None:
        await self._reload()

    async def _reload(self) -> None:
        rows = await self.db.all("SELECT * FROM proposals WHERE (state = 'open' OR rendered = 0 OR finish_pending = 1) AND imported = 0")
        self._voting = {row["message_id"] for row in rows if row["state"] == OPEN and row["message_id"]}
        for row in rows:
            try:
                if await self.bot.timers.get_key(TIMER, self._timer_key(row)) is None:
                    await self._arm(row)
            except Exception:  # one bad row must not leave another server's proposals with no timer
                log.exception("Could not arm proposal %s", row.get("id"))

    def cog_unload(self) -> None:
        for task in self._soon.values():
            task.cancel()

    @staticmethod
    def _timer_key(row: dict) -> str:
        return f"{row['guild_id']}:{row['number']}"

    def _lock(self, guild_id: int, number_: int) -> asyncio.Lock:
        return self._locks.setdefault((guild_id, number_), asyncio.Lock())

    async def _row(self, guild_id: int, number_: int) -> dict | None:
        if not 0 < number_ < NUMBER_MAX:
            return None
        return await self.db.one("SELECT * FROM proposals WHERE guild_id = ? AND number = ?", (guild_id, number_))

    async def _write(self, row: dict, changes: dict) -> dict:
        names = ", ".join(f"{name} = ?" for name in changes)
        await self.db.run(f"UPDATE proposals SET {names} WHERE id = ?", (*changes.values(), row["id"]))
        return {**row, **changes}

    async def _arm(self, row: dict) -> None:
        now = time.time()
        due = now + CHECK_SECONDS
        if now < row["ends_at"]:
            due = min(due, row["ends_at"])
        await self.bot.timers.schedule(TIMER, due, {"guild_id": row["guild_id"], "number": row["number"]}, guild_id=row["guild_id"], key=self._timer_key(row))

    # ---- the post ----

    def _post(self, row: dict) -> dict:
        lead = {
            "title": row["title"][:256], "description": row["content"], "color": COLORS[row["state"]], "footer": {"text": f"Proposal #{row['number']}"},
            "fields": [{"name": "Submitted by", "value": f"<@{row['submitter_id']}>", "inline": False}],
        }
        if row["credit"]:
            lead["fields"].append({"name": "Credit", "value": row["credit"], "inline": False})
        process = (
            "This proposal will be voted on by the community and moderators.\n\n"
            "**Community Voting:**\n"
            f"- Use {UP} to vote in favor, {DOWN} to vote against. If you react with both, the bot will remove the previous one, keeping only the latest.\n"
            f"- Voting is open for {_hours(row['vote_hours'])}, until <t:{int(row['ends_at'])}:f>. Attempts to vote or change votes after this period will be removed for non-moderators.\n"
            "- The community vote passes if more than 50% of the votes are in favor. It fails otherwise.\n\n"
            "**Moderator Voting:**\n"
            "- Moderators vote using the same reactions, but their votes are counted separately.\n"
            "- Moderator voting has no time limit but is independent.\n"
            "- The moderator vote is decided once more than half of all moderators vote in favor (approved) or more than half vote against (rejected).\n\n"
            "The proposal is approved only if both the community and moderator votes pass. If either fails, the proposal is rejected immediately."
        )
        fields = [
            {"name": "Current Votes", "value": f"Community: Favor: {row['community_up']} Against: {row['community_down']}\nModerators: Favor: {row['mod_up']} Against: {row['mod_down']}", "inline": False},
            {"name": "Proposal Approval Process", "value": process, "inline": False},
        ]
        if row["community_done"]:
            fields.append({"name": "Community Vote Results", "value": f"Favor: {row['community_up']}\nAgainst: {row['community_down']}\nOutcome: {'Passed' if row['community_pass'] else 'Failed'}", "inline": False})
        if row["mod_done"]:
            fields.append({"name": "Moderator Vote Results", "value": f"Favor: {row['mod_up']}\nAgainst: {row['mod_down']}\nOutcome: {'Approved' if row['mod_pass'] else 'Rejected'}", "inline": False})
        if row["state"] != OPEN:
            status = STATE_LABELS[row["state"]] + (f" ({row['status_reason']})" if row["status_reason"] else "")
            fields.append({"name": "Final Status", "value": status[:1024], "inline": False})
        tally = {"title": "Vote Tally and Process", "fields": fields}
        over = embed_size(lead) + embed_size(tally) - EMBEDS_MAX
        if over > 0:  # the longest title, text, credit and withdraw reason together are too much for one message
            lead["description"] = row["content"][:len(row["content"]) - over - len(CUT_NOTE)].rstrip() + CUT_NOTE
        return {"content": row["pings"], "embeds": [lead, tally], "buttons": []}

    async def _render(self, row: dict) -> dict:
        """Show the row on Discord. A refusal leaves it marked, and the next check tries again."""
        if not row["message_id"]:
            return await self._write(row, {"rendered": 1})
        if self._channel_gone(row):
            return await self._post_gone(row)
        try:
            await self.bot.ops.edit_message(row["channel_id"], row["message_id"], self._post(row))
        except MessageGone:
            return await self._post_gone(row)
        except OpsError as error:
            log.warning("Could not update proposal #%s in %s: %s", row["number"], row["guild_id"], error)
            return row
        return await self._write(row, {"rendered": 1})

    def _channel_gone(self, row: dict) -> bool:
        """A channel deleted while the bot was off sends no event, so it's noticed here. Only once the server's lists are back can a missing channel mean that."""
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None or guild.unavailable or not self.bot.members_settled.is_set():
            return False
        return guild.get_channel_or_thread(row["channel_id"]) is None

    def _role_problem(self, guild_id: int) -> str | None:
        role_id = self.cfg(guild_id)["moderator_role"]
        guild = self.bot.get_guild(guild_id)
        if not role_id or (guild is not None and guild.get_role(role_id) is None):
            return "This server has no moderator role set for proposals. Choose one on the Proposals page."
        return None

    def _setup_problem(self, guild_id: int) -> str | None:
        cfg = self.cfg(guild_id)
        problem = self._role_problem(guild_id)
        if problem:
            return problem
        if not cfg["channel_id"] or self.bot.ops.channel(guild_id, cfg["channel_id"]) is None:
            return "This server has no proposal channel set. Choose one on the Proposals page."
        return None

    async def create(self, guild_id: int, submitter: Actor, content: str, *, credit: str | None = None, suggestion: int | None = None) -> dict:
        """Post a proposal and open its votes. Raises ProposalError or OpsError with the reason it can't happen."""
        problem = self._setup_problem(guild_id)
        if problem:
            raise ProposalError(problem)
        cfg = self.cfg(guild_id)
        pings = " ".join(f"<@&{role}>" for role in (cfg["participation_role"], cfg["moderator_role"]) if role)
        now = time.time()
        async with self.db.transaction():  # the check is inside, so two promotes at once can't both pass it
            refusal = await self._promote_refusal(guild_id, suggestion) if suggestion is not None else None
            if refusal:
                raise ProposalError(refusal)
            number_ = await self.db.scalar("SELECT COALESCE(MAX(number), 0) + 1 FROM proposals WHERE guild_id = ?", (guild_id,))
            row_id = await self.db.insert(
                "INSERT INTO proposals (guild_id, number, pings, title, content, credit, submitter_id, submitter_name, suggestion_number, started_at, vote_hours, ends_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, number_, pings, cfg["title"], content, credit or None, submitter.id, submitter.name, suggestion, now, cfg["vote_hours"], now + cfg["vote_hours"] * 3600),
            )
        row = await self.db.one("SELECT * FROM proposals WHERE id = ?", (row_id,))
        ops = self.bot.ops
        try:
            sent = await ops.send_message(cfg["channel_id"], self._post(row), ping=True)
        except OpsError:
            await self.db.run("DELETE FROM proposals WHERE id = ?", (row_id,))  # gives the number back, since nobody saw it
            raise
        row = await self._write(row, {"channel_id": cfg["channel_id"], "message_id": sent["id"]})
        self._voting.add(sent["id"])
        first_line = " ".join(content.split())
        name = first_line[:50] + "..." if len(first_line) > 50 else first_line
        try:
            thread = await ops.start_thread(cfg["channel_id"], sent["id"], f"Discussion: {name}")
            row = await self._write(row, {"thread_id": thread["id"]})
        except OpsError as error:
            log.warning("Could not open a discussion thread for proposal #%s in %s: %s", number_, guild_id, error)
        for emoji in (UP, DOWN):
            try:
                await ops.add_reaction(cfg["channel_id"], sent["id"], emoji)
            except OpsError as error:
                log.warning("Could not add a vote reaction to proposal #%s in %s: %s", number_, guild_id, error)
        await self._arm(row)
        return {**row, "url": sent["url"]}

    async def submit(self, guild_id: int, submitter: Actor, content: str, *, credit: str | None = None, suggestion: int | None = None) -> dict:
        """A proposal from a person's words, including the step that links it to the suggestion it came from."""
        cleaned = text(CONTENT_MAX, minimum=1)(content)
        credit_text = text(CREDIT_MAX)(credit or "") or None
        row = await self.create(guild_id, submitter, cleaned, credit=credit_text, suggestion=suggestion)
        if suggestion is not None:
            await self._tell_suggestion(guild_id, suggestion, "considered", f"Put to a vote as proposal #{row['number']}: {row['url']}")
        return row

    async def _promote_refusal(self, guild_id: int, number_: int) -> str | None:
        """Why this suggestion can't go to a vote. Staff decided it already, or a vote on it is running."""
        suggestions = self.bot.module("suggestions")
        suggestion = await suggestions.get(guild_id, number_) if suggestions else None
        if suggestion is not None and suggestion["state"] not in ("open", "considered"):
            return f"Suggestion #{number_} is already decided, so it can't go to a vote."
        running = await self.vote_on_suggestion(guild_id, number_)
        return f"Suggestion #{number_} is already being voted on as proposal #{running}." if running else None

    async def vote_on_suggestion(self, guild_id: int, number_: int) -> int | None:
        """The proposal voting on this suggestion now, if there is one."""
        return await self.db.scalar("SELECT number FROM proposals WHERE guild_id = ? AND suggestion_number = ? AND state = 'open'", (guild_id, number_))

    async def forget_suggestion(self, guild_id: int, number_: int) -> None:
        await self.db.run("UPDATE proposals SET suggestion_number = NULL WHERE guild_id = ? AND suggestion_number = ?", (guild_id, number_))

    async def _tell_suggestion(self, guild_id: int, number_: int, decision: str, reason: str) -> None:
        suggestions = self.bot.module("suggestions")
        if suggestions is None:
            return
        me = getattr(self.bot, "user", None)
        try:
            await suggestions.decide(guild_id, number_, decision, moderator=Actor(me.id if me else 0, "the proposal vote"), reason=reason[:1000])
        except Exception:
            # The vote stands whatever happens to the note on the old suggestion.
            log.warning("Could not update suggestion #%s in %s", number_, guild_id, exc_info=True)

    # ---- counting ----

    async def _voters(self, row: dict, emoji: str) -> set[int]:
        """Discord lists super reactions apart from normal ones. Both are the person's vote."""
        people: set[int] = set()
        for burst in (False, True):
            after: int | None = None
            while True:
                page = await self.bot.ops.reaction_users(row["channel_id"], row["message_id"], emoji, after=after, limit=100, burst=burst)
                if not page:  # only an empty page means the list is done
                    break
                people.update(person["id"] for person in page if not person["bot"])
                after = page[-1]["id"]
        return people

    async def refresh(self, guild_id: int, number_: int) -> bool:
        """Count the votes on Discord and act on the result. False means nothing could be counted this time, and the next check tries again."""
        async with self._lock(guild_id, number_):
            row = await self._row(guild_id, number_)
            if row is None or row["imported"]:
                return False
            if row["state"] != OPEN:
                if not row["rendered"]:
                    row = await self._render(row)
                if row["finish_pending"]:
                    await self._finished(row)
                return True
            guild = self.bot.get_guild(guild_id)
            # Moderators are read from the member list. A list still refilling would let one moderator be "more than half", and that result is final.
            if guild is None or guild.unavailable or not self.bot.members_settled.is_set() or not member_lists._looks_whole(guild):
                log.info("Proposal #%s in %s waits for the member list before it's counted.", number_, guild_id)
                return False
            if self._channel_gone(row):
                await self._post_gone(row)
                return True
            role_id = self.cfg(guild_id)["moderator_role"]
            role = guild.get_role(role_id) if role_id else None
            if role is None:
                # With no role, a moderator's vote can't be told from a member's, and a closed count is final. The reactions keep every vote until there's a role again.
                log.warning("Proposal #%s in %s can't be counted, because the moderator role isn't set or no longer exists.", number_, guild_id)
                await self._say_no_role(row)
                return False
            self._told_no_role.discard((guild_id, number_))
            try:
                up = await self._voters(row, UP)
                down = await self._voters(row, DOWN)
            except MessageGone:
                await self._post_gone(row)
                return True
            except OpsError as error:
                log.warning("Could not count proposal #%s in %s: %s", number_, guild_id, error)
                return False
            moderators = {member.id for member in role.members if not member.bot}
            await self._apply_count(row, up, down, moderators)
            return True

    async def _say_no_role(self, row: dict) -> None:
        """Once, in the proposal's discussion, so the people voting know why the numbers stopped."""
        key = (row["guild_id"], row["number"])
        if key in self._told_no_role:
            return
        self._told_no_role.add(key)
        text_ = "The moderator role for proposals is gone, so these votes aren't being counted. Every vote stays on the post. Choose a role on the Proposals page and the count picks up from there."
        for where in (row["thread_id"], row["channel_id"]):  # an archived thread isn't in the bot's cache, and the post's channel is there to fall back on
            if where:
                try:
                    await self.bot.ops.send_message(where, {"content": text_, "embeds": [], "buttons": []}, silent=True)
                    return
                except OpsError as error:
                    log.info("Could not post the missing role notice for proposal #%s: %s", row["number"], error)

    async def _apply_count(self, row: dict, up: set[int], down: set[int], moderators: set[int]) -> None:
        now = time.time()
        counts = split_votes(up, down, moderators)
        changes: dict[str, Any] = {}
        # A finished vote's numbers stay as they were when it finished.
        if not row["community_done"]:
            changes.update(community_up=counts["community_up"], community_down=counts["community_down"])
            if now >= row["ends_at"]:
                changes.update(community_done=1, community_pass=int(counts["community_up"] > counts["community_down"]), last_reminder_at=now)
        if not row["mod_done"]:
            changes.update(mod_up=counts["mod_up"], mod_down=counts["mod_down"])
            decided = moderators_decided(counts["mod_up"], counts["mod_down"], len(moderators))
            if decided is not None:
                changes.update(mod_done=1, mod_pass=int(decided))
        state, reason = outcome({**row, **changes})
        if state != OPEN:
            changes.update(state=state, status_reason=reason, finished_at=now, finish_pending=1)
        changes = {name: value for name, value in changes.items() if row[name] != value}
        if changes:
            row = await self._write(row, {**changes, "rendered": 0})
        if not row["rendered"]:
            row = await self._render(row)
        if state != OPEN:
            await self._finished(row)
        else:
            await self._remind(row, up | down, moderators, now)

    async def _remind(self, row: dict, voted: set[int], moderators: set[int], now: float) -> None:
        hours = self.cfg(row["guild_id"])["reminder_hours"]
        if not hours or not row["community_done"] or row["mod_done"] or now - (row["last_reminder_at"] or 0) < hours * 3600:
            return
        await self._write(row, {"last_reminder_at": now})  # first, so a restart partway through can't send them twice
        link = f"https://discord.com/channels/{row['guild_id']}/{row['channel_id']}/{row['message_id']}"
        for user_id in moderators - voted:
            try:
                await self.bot.ops.send_dm(user_id, {"content": f"Reminder: Please vote on the proposal: {link}", "embeds": [], "buttons": []})
            except OpsError:
                pass  # plenty of people close their DMs

    async def _finished(self, row: dict) -> None:
        self._voting.discard(row["message_id"])
        if row["rendered"]:
            await self.bot.timers.cancel_key(TIMER, self._timer_key(row))
        cfg = self.cfg(row["guild_id"])
        if row["state"] in (APPROVED, REJECTED):
            if cfg["results_channel"] and self.bot.ops.channel(row["guild_id"], cfg["results_channel"]) is not None:
                await self._post_result(row, cfg["results_channel"])
            if row["suggestion_number"] is not None:
                await self._decide_suggestion(row)
        if row["finish_pending"]:
            await self._write(row, {"finish_pending": 0})

    async def _decide_suggestion(self, row: dict) -> None:
        """The vote decides a suggestion it's still considering. Staff who decided it while the vote ran have the last word."""
        suggestions = self.bot.module("suggestions")
        suggestion = await suggestions.get(row["guild_id"], row["suggestion_number"]) if suggestions else None
        if suggestion is None or suggestion["state"] != "considered":
            return
        decision = "approved" if row["state"] == APPROVED else "denied"
        why = f"Proposal #{row['number']}: {STATE_LABELS[row['state']].lower()}" + (f" ({row['status_reason']})" if row["status_reason"] else "")
        await self._tell_suggestion(row["guild_id"], row["suggestion_number"], decision, why)

    async def _post_result(self, row: dict, channel_id: int) -> None:
        link = f"https://discord.com/channels/{row['guild_id']}/{row['channel_id']}/{row['message_id']}" if row["message_id"] else "The post was deleted."
        embed = {
            "title": f"{row['title']} #{row['number']}: {STATE_LABELS[row['state']]}"[:256], "description": row["content"], "color": COLORS[row["state"]],
            "fields": [
                {"name": "Community", "value": f"Favor: {row['community_up']} Against: {row['community_down']}", "inline": True},
                {"name": "Moderators", "value": f"Favor: {row['mod_up']} Against: {row['mod_down']}", "inline": True},
                {"name": "The vote", "value": link, "inline": False},
            ],
        }
        if row["status_reason"]:
            embed["fields"].insert(0, {"name": "Why", "value": row["status_reason"], "inline": False})
        try:
            await self.bot.ops.send_message(channel_id, {"content": "", "embeds": [embed], "buttons": []}, silent=True)
        except OpsError as error:
            log.warning("Could not post the result of proposal #%s in %s: %s", row["number"], row["guild_id"], error)

    async def _post_gone(self, row: dict) -> dict:
        """With its post deleted, a vote can't go on. The record stays."""
        self._voting.discard(row["message_id"])
        row = await self._write(row, {"state": WITHDRAWN if row["state"] == OPEN else row["state"], "status_reason": row["status_reason"] or "Post deleted", "finished_at": row["finished_at"] or time.time(),
                                      "message_id": None, "thread_id": None, "rendered": 1})
        await self.bot.timers.cancel_key(TIMER, self._timer_key(row))
        return row

    async def withdraw(self, guild_id: int, number_: int, actor: Actor, reason: str | None) -> dict:
        async with self._lock(guild_id, number_):
            row = await self._row(guild_id, number_)
            if row is None:
                raise ProposalError(f"There is no proposal #{number_}.")
            if row["state"] != OPEN:
                raise ProposalError(f"Proposal #{number_} isn't being voted on anymore.")
            why = (reason or "").strip()[:REASON_MAX]
            row = await self._write(row, {"state": WITHDRAWN, "status_reason": f"By {actor.name}" + (f": {why}" if why else ""), "finished_at": time.time(), "rendered": 0})
            row = await self._render(row)
            await self._finished(row)
            return row

    async def _check_due(self, payload: dict, timer: dict) -> None:
        guild_id, number_ = payload["guild_id"], payload["number"]
        try:
            await self.refresh(guild_id, number_)
        except Exception:
            log.exception("Checking proposal #%s in %s failed", number_, guild_id)
        row = await self._row(guild_id, number_)
        if row is not None and not row["imported"] and (row["state"] == OPEN or not row["rendered"] or row["finish_pending"]):
            await self._arm(row)

    # ---- votes ----

    def _refresh_soon(self, guild_id: int, number_: int) -> None:
        key = (guild_id, number_)
        running = self._soon.get(key)
        if running is None or running.done():
            self._soon[key] = asyncio.ensure_future(self._count_while_voting(key))
        else:
            self._again.add(key)

    def _wait_before_count(self, row: dict | None) -> float:
        """Each count reads every page of voters, so a big vote waits longer between counts rather than spending the bot's rate limit on them."""
        if row is None:
            return self.REFRESH_DELAY
        voters = row["community_up"] + row["community_down"] + row["mod_up"] + row["mod_down"]
        return min(self.REFRESH_DELAY * (1 + voters // 100), max(LONGEST_COUNT_WAIT, self.REFRESH_DELAY))

    async def _count_while_voting(self, key: tuple[int, int]) -> None:
        """One count at a time for a proposal. A vote during a count sends it round once more when it ends, so nothing piles up behind the lock."""
        try:
            while True:
                try:
                    await asyncio.sleep(self._wait_before_count(await self._row(*key)))
                    self._again.discard(key)  # a vote from here on calls for another round, so it gets counted too
                    await self.refresh(*key)
                except Exception:
                    log.exception("Counting proposal #%s in %s failed", key[1], key[0])
                if key not in self._again:
                    return
        finally:
            self._soon.pop(key, None)

    async def _vote_row(self, guild_id: int | None, channel_id: int, message_id: int) -> dict | None:
        if guild_id is None:
            return None
        if message_id not in self._voting and channel_id != self.cfg(guild_id)["channel_id"]:
            return None
        row = await self.db.one("SELECT * FROM proposals WHERE guild_id = ? AND message_id = ?", (guild_id, message_id))
        return None if row is None or row["imported"] else row

    async def _take_off(self, row: dict, emoji: Any, user_id: int) -> None:
        try:
            # Discord names a custom emoji as name:id on this route. Text of the form <a:name:id> would keep the a and be refused.
            named = emoji if isinstance(emoji, str) else emoji.name if emoji.id is None else f"{emoji.name}:{emoji.id}"
            await self.bot.ops.remove_reaction(row["channel_id"], row["message_id"], named, user_id)
        except OpsError as error:
            log.info("Could not remove a reaction from proposal #%s: %s", row["number"], error)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        row = await self._vote_row(payload.guild_id, payload.channel_id, payload.message_id)
        if row is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or payload.user_id == guild.me.id:
            return
        member = payload.member or guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        choice = payload.emoji.name if payload.emoji.id is None else None
        if choice not in (UP, DOWN):
            await self._take_off(row, payload.emoji, payload.user_id)
            return
        if row["state"] != OPEN:
            await self._take_off(row, choice, payload.user_id)
            return
        role_id = self.cfg(payload.guild_id)["moderator_role"]
        role = guild.get_role(role_id) if role_id else None
        # With no role to say who's a moderator, a late vote might be one of theirs, and theirs have no deadline.
        late = role is not None and member.get_role(role.id) is None and (row["community_done"] or time.time() >= row["ends_at"])
        if late:
            await self._take_off(row, choice, payload.user_id)  # only this reaction: the vote they cast in time still stands
            return
        await self._take_off(row, DOWN if choice == UP else UP, payload.user_id)
        self._refresh_soon(payload.guild_id, row["number"])

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.emoji.id is not None or payload.emoji.name not in (UP, DOWN):
            return
        row = await self._vote_row(payload.guild_id, payload.channel_id, payload.message_id)
        if row is not None and row["state"] == OPEN:
            self._refresh_soon(payload.guild_id, row["number"])

    async def _votes_cleared(self, guild_id: int | None, channel_id: int, message_id: int) -> None:
        row = await self._vote_row(guild_id, channel_id, message_id)
        if row is None or row["state"] != OPEN:
            return
        for emoji in (UP, DOWN):
            try:
                await self.bot.ops.add_reaction(row["channel_id"], row["message_id"], emoji)
            except OpsError as error:
                log.warning("Could not put a vote reaction back on proposal #%s: %s", row["number"], error)
        self._refresh_soon(guild_id, row["number"])

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        await self._votes_cleared(payload.guild_id, payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        await self._votes_cleared(payload.guild_id, payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        row = await self._vote_row(payload.guild_id, payload.channel_id, payload.message_id)
        if row is not None:
            await self._post_gone(row)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        for message_id in payload.message_ids:
            row = await self._vote_row(payload.guild_id, payload.channel_id, message_id)
            if row is not None:
                await self._post_gone(row)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        guild_id = channel.guild.id
        cfg = self.cfg(guild_id)
        cleared = {name: None for name in ("channel_id", "results_channel") if cfg[name] == channel.id}
        if cleared:
            await self.bot.settings.save(guild_id, self.key, cleared, action="proposals.channel_deleted")
        for row in await self.db.all("SELECT * FROM proposals WHERE guild_id = ? AND channel_id = ? AND message_id IS NOT NULL AND imported = 0", (guild_id, channel.id)):
            await self._post_gone(row)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: Any) -> None:
        cfg = self.cfg(role.guild.id)
        cleared = {name: None for name in ("moderator_role", "participation_role") if cfg[name] == role.id}
        if cleared:
            await self.bot.settings.save(role.guild.id, self.key, cleared, action="proposals.role_deleted")
        if "moderator_role" in cleared:
            for row in await self.db.all("SELECT number FROM proposals WHERE guild_id = ? AND state = 'open' AND imported = 0", (role.guild.id,)):
                self._refresh_soon(role.guild.id, row["number"])

    # ---- who may act ----

    def _may_propose(self, guild_id: int, member: Any) -> bool:
        if member is None:
            return False
        role_id = self.cfg(guild_id)["moderator_role"]
        return member.guild_permissions.manage_guild or (role_id is not None and member.get_role(role_id) is not None)

    def _may_propose_on_site(self, req: Any) -> bool:
        if req.access == "full":
            return True
        guild = self.bot.get_guild(req.guild_id)
        return guild is not None and self._may_propose(req.guild_id, guild.get_member(req.user.id))

    def _may_withdraw(self, row: dict, member: Any) -> bool:
        """Any one moderator stopping a vote they're losing, with no undo, is too much. The person who submitted it can, and so can Manage Server."""
        if member is None:
            return False
        return member.guild_permissions.manage_guild or (row["submitter_id"] == member.id and self._may_propose(row["guild_id"], member))

    def _may_withdraw_on_site(self, req: Any, row: dict) -> bool:
        if req.access == "full":
            return True
        guild = self.bot.get_guild(req.guild_id)
        return guild is not None and self._may_withdraw(row, guild.get_member(req.user.id))

    def _human_moderators(self, guild_id: int) -> int | None:
        """How many people hold the moderator role, or None with no role to read."""
        guild = self.bot.get_guild(guild_id)
        role_id = self.cfg(guild_id)["moderator_role"]
        role = guild.get_role(role_id) if guild is not None and role_id else None
        return None if role is None else sum(1 for member in role.members if not member.bot)

    def _suggestion_defaults(self, row: dict) -> tuple[str, str]:
        return row["content"], "" if row["anonymous"] else row["author_name"]

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/proposals/list", self._list_endpoint, access="staff")
        router.post("/proposals", self._submit_endpoint, access="staff")
        router.post("/proposals/recheck", self._recheck_endpoint)
        router.post(r"/proposals/{number:\d+}/withdraw", self._withdraw_endpoint, access="staff")
        router.post(r"/proposals/from-suggestion/{number:\d+}", self._promote_endpoint, access="staff")

    def _view(self, row: dict) -> dict:
        guild_id = row["guild_id"]
        return {
            "number": row["number"], "title": row["title"], "content": row["content"], "credit": row["credit"], "submitter_name": row["submitter_name"], "state": row["state"], "status_reason": row["status_reason"],
            "community_up": row["community_up"], "community_down": row["community_down"], "community_done": bool(row["community_done"]), "community_pass": bool(row["community_pass"]),
            "mod_up": row["mod_up"], "mod_down": row["mod_down"], "mod_done": bool(row["mod_done"]), "mod_pass": bool(row["mod_pass"]),
            "started_at": row["started_at"], "ends_at": row["ends_at"], "finished_at": row["finished_at"], "suggestion_number": row["suggestion_number"],
            "url": f"https://discord.com/channels/{guild_id}/{row['channel_id']}/{row['message_id']}" if row["channel_id"] and row["message_id"] else None,
            "thread_url": f"https://discord.com/channels/{guild_id}/{row['thread_id']}" if row["thread_id"] else None,
        }

    async def _list_endpoint(self, req: Any) -> dict:
        where = "guild_id = ?"
        args: list[Any] = [req.guild_id]
        state = req.query.get("state", "")
        if state:
            where += " AND state = ?"
            args.append(state)
        needle = req.query.get("q", "").strip().lower()[:200]
        if needle:
            where += " AND LOWER(content) LIKE ? ESCAPE '\\'"
            args.append("%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        page = page_number(req.query.get("page", "1"), 1, NUMBER_MAX)
        size = page_number(req.query.get("size", "10"), 10, PAGE_MAX)
        total = await self.db.scalar(f"SELECT COUNT(*) FROM proposals WHERE {where}", args)
        rows = await self.db.all(f"SELECT * FROM proposals WHERE {where} ORDER BY number DESC LIMIT ? OFFSET ?", (*args, size, (page - 1) * size))
        moderators = self._human_moderators(req.guild_id) if any(row["state"] == OPEN and not row["mod_done"] for row in rows) else None
        return {
            "rows": [{**self._view(row), "can_withdraw": self._may_withdraw_on_site(req, row), "mod_total": moderators} for row in rows],
            "total": total, "page": page, "size": size, "problem": self._setup_problem(req.guild_id) if req.access == "full" else None, "can_propose": self._may_propose_on_site(req),
        }

    def _form_text(self, form: dict) -> tuple[str, str | None]:
        problems: dict[str, str] = {}
        content = credit = None
        try:
            content = text(CONTENT_MAX, minimum=1)(form.get("content"))
        except Invalid as error:
            problems["content"] = str(error)
        try:
            credit = text(CREDIT_MAX)(form.get("credit") or "") or None
        except Invalid as error:
            problems["credit"] = str(error)
        if problems:
            raise ValidationError(problems)
        return content, credit

    async def _submit_endpoint(self, req: Any) -> dict:
        if not self._may_propose_on_site(req):
            raise ApiError(403, "Only moderators can submit proposals.")
        content, credit = self._form_text(await req.json())
        try:
            row = await self.create(req.guild_id, req.actor, content, credit=credit)
        except ProposalError as error:
            raise ApiError(400, str(error)) from error
        await req.record("proposal.submit", detail=f"#{row['number']}")
        return self._view(row)

    async def _promote_endpoint(self, req: Any) -> dict:
        if not self._may_propose_on_site(req):
            raise ApiError(403, "Only moderators can submit proposals.")
        number_ = req.int_param("number")
        suggestions = self.bot.module("suggestions")
        suggestion = await suggestions.get(req.guild_id, number_) if suggestions else None
        if suggestion is None:
            raise ApiError(404, f"There is no suggestion #{number_}.")
        form = await req.json()
        default_content, default_credit = self._suggestion_defaults(suggestion)
        content, credit = self._form_text({"content": form.get("content") or default_content, "credit": form["credit"] if "credit" in form else default_credit})
        try:
            row = await self.create(req.guild_id, req.actor, content, credit=credit, suggestion=number_)
        except ProposalError as error:
            raise ApiError(400, str(error)) from error
        await self._tell_suggestion(req.guild_id, number_, "considered", f"Put to a vote as proposal #{row['number']}: {row['url']}")
        await req.record("proposal.promote", detail=f"suggestion #{number_} to proposal #{row['number']}")
        return self._view(row)

    async def _recheck_endpoint(self, req: Any) -> dict:
        return await self._recheck(req.guild_id)

    async def _recheck(self, guild_id: int) -> dict:
        """Count every open proposal now. `problem` says why none could be, when the server's setup is the reason."""
        rows = await self.db.all("SELECT number FROM proposals WHERE guild_id = ? AND state = 'open' AND imported = 0", (guild_id,))
        problem = self._role_problem(guild_id)
        counted = 0
        for row in rows:
            if await self.refresh(guild_id, row["number"]):
                counted += 1
        return {"checked": len(rows), "counted": counted, "problem": problem}

    @staticmethod
    def _recheck_text(result: dict) -> str:
        checked, counted = result["checked"], result["counted"]
        if not checked:
            return "No proposals are being voted on."
        text_ = f"Counted {counted} of {checked} open proposal{'' if checked == 1 else 's'}."
        return text_ if counted == checked else f"{text_} The rest couldn't be read from Discord just now, and the next check tries them again."

    async def _withdraw_endpoint(self, req: Any) -> dict:
        if not self._may_propose_on_site(req):
            raise ApiError(403, "Only moderators can withdraw proposals.")
        number_ = req.int_param("number")
        row = await self._row(req.guild_id, number_)
        if row is not None and not self._may_withdraw_on_site(req, row):
            raise ApiError(403, "Only the moderator who submitted it or someone with Manage Server can withdraw a proposal.")
        form = await req.json()
        try:
            row = await self.withdraw(req.guild_id, number_, req.actor, text(REASON_MAX)(form.get("reason") or ""))
        except Invalid as error:
            raise ValidationError({"reason": str(error)}) from error
        except ProposalError as error:
            raise ApiError(404, str(error)) from error
        await req.record("proposal.withdraw", detail=f"#{number_}")
        return self._view(row)

    async def seed_demo(self, guild_id: int) -> None:
        now = time.time()
        samples = [
            (1, "Add a channel for sharing finished prints, so the help channels stay on topic.", APPROVED, 22, 3, 1, 1, 8, 1, 1, 1, None, 9 * 86400),
            (2, "Move the rules into a single post at the top of the welcome channel.", REJECTED, 4, 11, 1, 0, 0, 0, 0, 0, "Community vote failed", 5 * 86400),
            (3, "Open voice channels for Saturday print-alongs.", OPEN, 13, 2, 0, 0, 3, 0, 0, 0, None, 3600),
        ]
        for number_, content, state, up, down, done, passed, mod_up, mod_down, mod_done, mod_pass, reason, age in samples:
            await self.db.run(
                "INSERT OR IGNORE INTO proposals (guild_id, number, title, content, submitter_id, submitter_name, started_at, vote_hours, ends_at, community_up, community_down, community_done, community_pass, "
                "mod_up, mod_down, mod_done, mod_pass, state, status_reason, finished_at, imported) VALUES (?, ?, 'Server Change Proposal', ?, 1, 'Demo Admin', ?, 72, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (guild_id, number_, content, now - age, now - age + 72 * 3600, up, down, done, passed, mod_up, mod_down, mod_done, mod_pass, state, reason, None if state == OPEN else now - age + 72 * 3600),
            )

    # ---- /proposal ----

    @commands.hybrid_group(name="proposal", fallback="list", description="List the proposals being voted on")
    @commands.guild_only()
    @staff_only("Moderators")
    async def proposal(self, ctx: commands.Context) -> None:
        if not self._may_propose(ctx.guild.id, ctx.author):
            await ctx.fail("Only moderators can list proposals.")
            return
        rows = await self.db.all("SELECT * FROM proposals WHERE guild_id = ? AND state = 'open' AND imported = 0 ORDER BY number", (ctx.guild.id,))
        if not rows:
            await ctx.send("No proposals are being voted on.", ephemeral=True)
            return
        lines: list[str] = []
        room = MESSAGE_ROOM
        for row in rows:
            link = f"https://discord.com/channels/{ctx.guild.id}/{row['channel_id']}/{row['message_id']}"
            community = "community vote over" if row["community_done"] else f"community vote ends <t:{int(row['ends_at'])}:R>"
            line = f"[#{row['number']}]({link}): {community}, moderators {row['mod_up']} for and {row['mod_down']} against"
            if len(line) + 1 > room:
                break
            lines.append(line)
            room -= len(line) + 1
        if len(lines) < len(rows):
            lines.append(f"And {len(rows) - len(lines)} more, on the Proposals page.")
        await ctx.send("\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="submit_proposal", description="Put a change to the server to a vote")
    @commands.guild_only()
    @staff_only("Moderators")
    @app_commands.describe(proposal="What to put to a vote. Leave it out to write it in a form.")
    async def submit_proposal(self, ctx: commands.Context, *, proposal: str | None = None) -> None:
        if not self._may_propose(ctx.guild.id, ctx.author):
            await ctx.fail("You must have the moderator role to submit proposals.")
            return
        problem = self._setup_problem(ctx.guild.id)
        if problem:
            await ctx.fail(problem)
            return
        if proposal is None and ctx.interaction is not None:
            await ctx.interaction.response.send_modal(ProposalForm(self))
            return
        if proposal is None:
            await ctx.fail("Write the proposal after the command.")
            return
        await ctx.defer(ephemeral=True)
        try:
            row = await self.submit(ctx.guild.id, self.bot.actor(ctx.author), proposal)
        except (ProposalError, OpsError, Invalid) as error:
            await ctx.fail(str(error))
            return
        await ctx.ok(f"Proposal #{row['number']} is up for a vote: {row['url']}")

    @proposal.command(name="promote", description="Put a suggestion to a vote as a proposal")
    @staff_only("Moderators")
    async def proposal_promote(self, ctx: commands.Context, suggestion_id: int) -> None:
        if not self._may_propose(ctx.guild.id, ctx.author):
            await ctx.fail("You must have the moderator role to submit proposals.")
            return
        problem = self._setup_problem(ctx.guild.id)
        if problem:
            await ctx.fail(problem)
            return
        suggestions = self.bot.module("suggestions")
        suggestion = await suggestions.get(ctx.guild.id, suggestion_id) if suggestions else None
        if suggestion is None:
            await ctx.fail(f"There is no suggestion #{suggestion_id}.")
            return
        refusal = await self._promote_refusal(ctx.guild.id, suggestion_id)
        if refusal:
            await ctx.fail(refusal)
            return
        content, credit = self._suggestion_defaults(suggestion)
        if ctx.interaction is not None:
            await ctx.interaction.response.send_modal(ProposalForm(self, content=content, credit=credit, suggestion=suggestion_id))
            return
        try:
            row = await self.submit(ctx.guild.id, self.bot.actor(ctx.author), content, credit=credit, suggestion=suggestion_id)
        except (ProposalError, OpsError, Invalid) as error:
            await ctx.fail(str(error))
            return
        await ctx.ok(f"Proposal #{row['number']} is up for a vote: {row['url']}")

    @proposal.command(name="withdraw", description="Stop the votes on a proposal")
    @staff_only("Moderators")
    async def proposal_withdraw(self, ctx: commands.Context, number: int, *, reason: str | None = None) -> None:
        if not self._may_propose(ctx.guild.id, ctx.author):
            await ctx.fail("You must have the moderator role to withdraw proposals.")
            return
        row = await self._row(ctx.guild.id, number)
        if row is not None and not self._may_withdraw(row, ctx.author):
            await ctx.fail("Only the moderator who submitted it or someone with Manage Server can withdraw a proposal.")
            return
        await ctx.defer(ephemeral=True)
        try:
            await self.withdraw(ctx.guild.id, number, self.bot.actor(ctx.author), reason)
        except ProposalError as error:
            await ctx.fail(str(error))
            return
        await self.bot.activity_log.record(ctx.guild.id, self.bot.actor(ctx.author), "proposal.withdraw", detail=f"#{number}")
        await ctx.ok(f"Proposal #{number} is withdrawn.")

    @proposal.command(name="recheck", description="Count the votes on every open proposal now")
    @requires("manage_guild")
    async def proposal_recheck(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        result = await self._recheck(ctx.guild.id)
        if result["problem"]:
            await ctx.fail(result["problem"])
            return
        await ctx.ok(self._recheck_text(result))

    @proposal.command(name="settings", description="Show how proposals are set up")
    @requires("manage_guild")
    async def proposal_settings(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)

        def named(prefix: str, value: int | None, otherwise: str) -> str:
            return f"<{prefix}{value}>" if value else otherwise

        await ctx.send(
            f"Proposal channel: {named('#', cfg['channel_id'], 'not set')}\nModerator role: {named('@&', cfg['moderator_role'], 'not set')}\n"
            f"Role to notify: {named('@&', cfg['participation_role'], 'none')}\nCommunity voting: {_hours(cfg['vote_hours'])}\n"
            f"Moderator reminders: {'every ' + _hours(cfg['reminder_hours']) if cfg['reminder_hours'] else 'off'}\nResults channel: {named('#', cfg['results_channel'], 'none')}\n"
            f"Full settings: {self.bot.public_url}/dashboard/{ctx.guild.id}/proposals",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
