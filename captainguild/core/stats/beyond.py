"""Statistics that go further than counting: moderation over time, help forums, whether newcomers stay, and the numbers a scheduled report
posts. Moderation reads Captain Guild's own records, cases, reports and automod's log entries, so it covers what those features kept.

Nothing here touches Discord. Each function takes the period as Unix seconds [start, end), the server's time zone and an interval, and
answers with plain values a page or a report can show.
"""
from __future__ import annotations

import bisect
import json
import statistics as numbers
from collections import Counter, defaultdict
from typing import Any, Callable, Iterable

from ...db import Database
from . import query, store
from .timeframe import bucket_edges, bucket_label, floor_bucket, slot_of

AUTOMOD_PREFIX = "Automod: "
AUTOMOD_SOURCES = ("automod", "threshold")
FIRST_ANSWER_MARKS = (3600, 86400)  # answered within an hour, within a day
RETENTION_DAYS = (1, 7, 30)
DEFAULT_WEEKS = 12
MAX_WEEKS = 104
SQL_LIST = 500


async def has_table(db: Database, name: str) -> bool:
    return bool(await db.scalar("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)))


def buckets(start: float, end: float, interval: str, tz: Any) -> list[float]:
    return bucket_edges(start, end, interval, tz)


def _index(edges: list[float], at: float) -> int | None:
    if not edges or at < edges[0]:
        return None
    return bisect.bisect_right(edges, at) - 1


def _labels(edges: list[float], interval: str, tz: Any) -> list[dict]:
    return [{"at": edge, "label": bucket_label(edge, interval, tz)} for edge in edges]


def _median(values: list[float]) -> float | None:
    return float(numbers.median(values)) if values else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def automod_rule(title: str | None) -> tuple[str, bool] | None:
    """(the rule's label, whether it was a dry run) from an automod log entry's title, or None for another kind of entry."""
    if not title or not title.startswith(AUTOMOD_PREFIX):
        return None
    rule = title[len(AUTOMOD_PREFIX):]
    dry = rule.endswith(" (dry run)")
    rule = rule.removesuffix(" (dry run)").removesuffix(" (edited in)").strip()
    if rule == "slow bad-word pattern":
        return None
    return rule, dry


# ---- moderation ----

async def moderation(main: Database, messages: Database, guild_id: int, start: float, end: float, interval: str, tz: Any, *,
                     readable: set[int] | None = None) -> dict:
    """Cases by action and by moderator, automod by rule, and reports opened and handled, with how long handling took. Automod's log entries
    count only in the channels in `readable`, or all of them when it's None, the same rule the log search follows."""
    edges = buckets(start, end, interval, tz)
    out: dict[str, Any] = {"buckets": _labels(edges, interval, tz), "cases": None, "automod": None, "reports": None}
    if await has_table(main, "cases"):
        rows = await main.all("SELECT action, moderator_id, moderator_name, moderator_bot, source, reason, created_at, voided_at FROM cases "
                              "WHERE guild_id = ? AND created_at >= ? AND created_at < ?", (guild_id, start, end))
        by_action: dict[str, list[int]] = defaultdict(lambda: [0] * len(edges))
        moderators: dict[Any, dict] = {}
        punished: Counter = Counter()
        voided = 0
        for row in rows:
            if row["voided_at"] is not None:
                voided += 1
                continue
            at = _index(edges, row["created_at"])
            if at is not None:
                by_action[row["action"]][at] += 1
            if row["source"] in AUTOMOD_SOURCES:
                rule = (row["reason"] or "").removeprefix(AUTOMOD_PREFIX).strip() or "Automod"
                punished[rule] += 1
                who, name = "automod", "Automod"
            elif row["moderator_id"] is None:
                who, name = "unknown", row["moderator_name"] or ("Timer" if row["source"] == "timer" else "Unknown")
            else:
                who, name = row["moderator_id"], row["moderator_name"] or str(row["moderator_id"])
            entry = moderators.setdefault(who, {"id": who if isinstance(who, int) else None, "name": name, "total": 0, "actions": Counter(), "series": [0] * len(edges)})
            entry["total"] += 1
            entry["actions"][row["action"]] += 1
            if at is not None:
                entry["series"][at] += 1
        out["cases"] = {
            "total": sum(sum(series) for series in by_action.values()),
            "voided": voided,
            "by_action": [{"action": action, "series": series, "total": sum(series)} for action, series in sorted(by_action.items(), key=lambda item: -sum(item[1]))],
            "moderators": sorted(({**entry, "actions": dict(entry["actions"])} for entry in moderators.values()), key=lambda entry: (-entry["total"], entry["name"].casefold())),
        }
        out["automod"] = {"punishments": [{"rule": rule, "count": count} for rule, count in punished.most_common()], "hits": [], "hits_kept": False}
    if await has_table(messages, "log_entries"):
        hits: Counter = Counter()
        dry_runs: Counter = Counter()
        series: dict[str, list[int]] = defaultdict(lambda: [0] * len(edges))
        where, args = "", ()
        if readable is not None:
            ids = sorted(readable)
            marks = ", ".join("?" for _ in ids) or "NULL"
            where, args = f" AND (channel_id IS NULL OR channel_id IN ({marks}) OR parent_id IN ({marks}))", (*ids, *ids)
        for row in await messages.all(f"SELECT at, embed FROM log_entries WHERE guild_id = ? AND event = 'automod' AND at >= ? AND at < ?{where}",
                                      (guild_id, start, end, *args)):
            try:
                title = json.loads(row["embed"]).get("title")
            except (ValueError, AttributeError):
                continue
            found = automod_rule(title)
            if found is None:
                continue
            rule, dry = found
            if dry:
                dry_runs[rule] += 1
                continue
            hits[rule] += 1
            at = _index(edges, row["at"])
            if at is not None:
                series[rule][at] += 1
        automod = out["automod"] or {"punishments": []}
        automod["hits"] = [{"rule": rule, "count": count, "dry_runs": dry_runs.get(rule, 0), "series": series[rule]} for rule, count in hits.most_common()]
        automod["dry_runs_only"] = [{"rule": rule, "count": count} for rule, count in dry_runs.items() if rule not in hits]
        automod["hits_kept"] = True
        out["automod"] = automod
    if await has_table(main, "reports"):
        rows = await main.all("SELECT state, created_at, handled_at, handled_by, handled_name FROM reports WHERE guild_id = ? AND "
                              "((created_at >= ? AND created_at < ?) OR (handled_at >= ? AND handled_at < ?))", (guild_id, start, end, start, end))
        opened, handled = [0] * len(edges), [0] * len(edges)
        waits: list[float] = []
        handlers: dict[Any, dict] = {}
        outcomes: Counter = Counter()
        for row in rows:
            if start <= row["created_at"] < end:
                at = _index(edges, row["created_at"])
                if at is not None:
                    opened[at] += 1
            if row["handled_at"] is not None and start <= row["handled_at"] < end and row["state"] != "open":
                at = _index(edges, row["handled_at"])
                if at is not None:
                    handled[at] += 1
                wait = max(0.0, row["handled_at"] - row["created_at"])
                waits.append(wait)
                outcomes[row["state"]] += 1
                who = row["handled_by"] or "unknown"
                entry = handlers.setdefault(who, {"id": row["handled_by"], "name": row["handled_name"] or "Unknown", "count": 0, "waits": []})
                entry["count"] += 1
                entry["waits"].append(wait)
        open_now = await main.scalar("SELECT COUNT(*) FROM reports WHERE guild_id = ? AND state = 'open'", (guild_id,))
        out["reports"] = {
            "opened": opened, "handled": handled, "opened_total": sum(opened), "handled_total": len(waits), "open_now": int(open_now or 0),
            "median_seconds": _median(waits), "average_seconds": _mean(waits), "outcomes": dict(outcomes),
            "handlers": sorted(({"id": entry["id"], "name": entry["name"], "count": entry["count"], "median_seconds": _median(entry["waits"])} for entry in handlers.values()),
                               key=lambda entry: (-entry["count"], entry["name"].casefold())),
        }
    return out


# ---- help forums ----

FORUM_MIGRATION = """
    CREATE TABLE stat_forum_posts (
        guild_id INTEGER NOT NULL,
        thread_id INTEGER NOT NULL,
        forum_id INTEGER NOT NULL,
        owner_id INTEGER,
        created_at REAL NOT NULL,
        replied_at REAL,
        replier_id INTEGER,
        solved_at REAL,
        PRIMARY KEY (guild_id, thread_id)
    );
    CREATE INDEX stat_forum_posts_forum ON stat_forum_posts (guild_id, forum_id, created_at);
    CREATE INDEX stat_forum_posts_owner ON stat_forum_posts (guild_id, owner_id);
    CREATE INDEX stat_forum_posts_replier ON stat_forum_posts (guild_id, replier_id);
"""


async def forums(messages: Database, guild_id: int, start: float, end: float, interval: str, tz: Any, solved_tags: dict[int, int | None], *,
                 shown: Callable[[int], bool] = lambda forum_id: True) -> dict:
    """For each forum `shown` lets through, the posts made in the period: how many got an answer from someone other than the poster, how soon,
    and how many are marked solved. Solved shows only for a forum with a solved tag on the Forum messages page. The trend counts the same
    forums as the table."""
    edges = buckets(start, end, interval, tz)
    rows = await messages.all("SELECT forum_id, created_at, replied_at, solved_at FROM stat_forum_posts WHERE guild_id = ? AND created_at >= ? AND created_at < ?",
                              (guild_id, start, end))
    per: dict[int, dict] = {}
    posted, answered = [0] * len(edges), [0] * len(edges)
    waits_by_bucket: list[list[float]] = [[] for _ in edges]
    for row in rows:
        if not shown(row["forum_id"]):
            continue
        entry = per.setdefault(row["forum_id"], {"posts": 0, "answered": 0, "waits": [], "solved": 0})
        entry["posts"] += 1
        at = _index(edges, row["created_at"])
        if at is not None:
            posted[at] += 1
        if row["replied_at"] is not None:
            wait = max(0.0, row["replied_at"] - row["created_at"])
            entry["answered"] += 1
            entry["waits"].append(wait)
            if at is not None:
                answered[at] += 1
                waits_by_bucket[at].append(wait)
        if row["solved_at"] is not None:
            entry["solved"] += 1
    out = []
    for forum_id, entry in per.items():
        waits = entry["waits"]
        tagged = forum_id in solved_tags and solved_tags[forum_id] is not None
        out.append({
            "forum_id": forum_id, "posts": entry["posts"], "answered": entry["answered"],
            "answered_share": entry["answered"] / entry["posts"] if entry["posts"] else None,
            "median_seconds": _median(waits), "average_seconds": _mean(waits),
            "within": {str(mark): (sum(1 for wait in waits if wait <= mark) / entry["posts"]) if entry["posts"] else None for mark in FIRST_ANSWER_MARKS},
            "solved": entry["solved"] if tagged else None,
            "solved_share": (entry["solved"] / entry["posts"]) if tagged and entry["posts"] else None,
        })
    out.sort(key=lambda entry: -entry["posts"])
    return {"buckets": _labels(edges, interval, tz), "posted": posted, "answered": answered, "median_seconds": [_median(waits) for waits in waits_by_bucket],
            "forums": out}


# ---- newcomers ----

async def newcomers(messages: Database, guild_id: int, now: float, tz: Any, *, weeks: int = DEFAULT_WEEKS, members_now: set[int] | None = None) -> list[dict]:
    """Of the people who joined in each week, how many are still here, and how many spoke at all after joining and 1, 7 and 30 or more days
    after. A week too recent to tell gives None for that mark. Someone who chose privacy counts as a join with nothing more known."""
    weeks = max(1, min(weeks, MAX_WEEKS))
    this_week = floor_bucket(now, "week", tz)
    edges = bucket_edges(this_week - (weeks - 1) * 7 * 86400 - 3 * 86400, now, "week", tz)[-weeks:]
    if not edges:
        return []
    rows = await messages.all("SELECT user_id, at, left_at FROM stat_joins WHERE guild_id = ? AND at >= ? AND at < ? ORDER BY at",
                              (guild_id, edges[0], now))
    later = await messages.all("SELECT user_id, MAX(at) AS last_join, MAX(left_at IS NULL) AS stayed FROM stat_joins WHERE guild_id = ? AND at >= ? GROUP BY user_id",
                               (guild_id, edges[0]))
    stayed_by_rows = {row["user_id"]: bool(row["stayed"]) for row in later}
    cohorts: list[dict] = [{"start": edge, "label": bucket_label(edge, "week", tz), "people": {}, "anonymous": 0} for edge in edges]
    for row in rows:
        at = _index(edges, row["at"])
        if at is None:
            continue
        cohort = cohorts[at]
        if not row["user_id"]:
            cohort["anonymous"] += 1
            continue
        cohort["people"].setdefault(row["user_id"], row["at"])
    everyone = sorted({user_id for cohort in cohorts for user_id in cohort["people"]})
    last_spoke: dict[int, int] = {}
    for chunk_start in range(0, len(everyone), SQL_LIST):
        chunk = everyone[chunk_start:chunk_start + SQL_LIST]
        marks = ", ".join("?" for _ in chunk)
        for row in await messages.all(f"SELECT user_id, MAX(slot) AS last FROM stat_messages WHERE guild_id = ? AND user_id IN ({marks}) AND flags & 1 = 0 GROUP BY user_id",
                                      (guild_id, *chunk)):
            last_spoke[row["user_id"]] = row["last"]
    out = []
    for index, cohort in enumerate(cohorts):
        week_end = edges[index + 1] if index + 1 < len(edges) else floor_bucket(now, "week", tz) + 7 * 86400
        people = cohort["people"]
        joined = len(people) + cohort["anonymous"]
        here = 0
        spoke = 0
        after = {days: 0 for days in RETENTION_DAYS}
        for user_id, at in people.items():
            if members_now is not None:
                here += user_id in members_now
            else:
                here += stayed_by_rows.get(user_id, False)
            last = last_spoke.get(user_id)
            if last is None or last < slot_of(at):
                continue
            spoke += 1
            for days in RETENTION_DAYS:
                if last >= slot_of(at + days * 86400):
                    after[days] += 1
        out.append({"start": cohort["start"], "label": cohort["label"], "joined": joined, "still_here": here, "spoke": spoke,
                    "spoke_after": {str(days): (after[days] if week_end + days * 86400 <= now else None) for days in RETENTION_DAYS},
                    "complete": week_end <= now})
    return out


# ---- the summary a scheduled report posts ----

async def summary(messages: Database, guild_id: int, start: float, end: float, previous: float, f: query.Filters | None = None) -> dict:
    """Totals for [start, end) beside the same length before it, from `previous`, with the top three members and channels by messages, read
    with the server's own view filters `f`, or people without bots when there are none."""
    f = f if f is not None else query.Filters(include="humans")
    first, last = slot_of(start), slot_of(end)
    before = slot_of(previous)
    out: dict[str, Any] = {}
    for kind in ("messages", "voice"):
        now_total = await query.total(messages, kind, guild_id, f, first, last)
        then_total = await query.total(messages, kind, guild_id, f, before, first)
        out[kind] = {"now": now_total, "before": then_total}
    out["active"] = {"now": await query.unique_count(messages, ["messages", "voice"], "user_id", guild_id, f, first, last),
                     "before": await query.unique_count(messages, ["messages", "voice"], "user_id", guild_id, f, before, first)}
    joins, leaves = await query.joins_and_leaves(messages, guild_id, first, last)
    out["joins"], out["leaves"] = joins, leaves
    out["members"] = {"now": await query.member_count_at(messages, guild_id, last - 1), "before": await query.member_count_at(messages, guild_id, first - 1)}
    out["top_members"] = (await query.top_members(messages, "messages", guild_id, f, first, last, limit=3)).rows
    out["top_channels"] = (await query.top_channels(messages, "messages", guild_id, f, first, last, limit=3)).rows
    return out


def change_text(now: float, before: float) -> str:
    """How a number moved, as a report line puts it: "up 12%", "down 3%", "no change", or "new" when there was nothing before."""
    if not before:
        return "new" if now else "no change"
    change = (now - before) / before * 100
    if abs(change) < 0.5:
        return "no change"
    return f"{'up' if change > 0 else 'down'} {abs(change):.0f}%"


def people_of(rows: Iterable[tuple]) -> list[int]:
    return [row[0] for row in rows]


def kept_names(store_rows: dict[int, dict], user_id: int) -> str:
    row = store_rows.get(user_id)
    if row is None:
        return "Someone who left"
    return row.get("nick") or row.get("display_name") or row.get("name") or "Someone who left"


__all__ = ["moderation", "forums", "newcomers", "summary", "change_text", "automod_rule", "store"]
