"""Presets in Captain Guild's own format, so a server's saved views move between servers, backups and files.

A preset is a view's address under a name (web.py): {"name": "Two months", "kind": "filters", "query": {"period": "2mo", "interval":
"week"}, "picks": []}. A drilldown preset, of kind "members" or "channels", keeps the people or channels it picked in "picks".
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable

from .web import DRILL_LIMIT, clean_address, read_field

PRESET_KINDS = ("filters", "members", "channels")
PRESETS_PER_KIND = 100
NAME_LIMIT = 19  # names stay under 20 characters


def preset_name(value: Any) -> str:
    """A preset's name, its spaces tidied and cut to fit."""
    text = " ".join(str(value or "").split())
    return text[:NAME_LIMIT].strip() or "Preset"


def clean_preset(value: Any) -> dict:
    """One preset in the own format, checked: the fields of its query a view can read, and its picks as IDs. Raises ValueError with the
    sentence to show."""
    if not isinstance(value, dict):
        raise ValueError("Each preset needs a name and a query.")
    kind = value.get("kind") or "filters"
    if kind not in PRESET_KINDS:
        raise ValueError("A preset's kind is filters, members or channels.")
    query = value.get("query") if isinstance(value.get("query"), dict) else {}
    picks = read_field("members", value.get("picks") or []) if kind != "filters" else []
    if picks is None:
        raise ValueError("A preset's picks are Discord IDs.")
    return {"name": preset_name(value.get("name")), "kind": kind, "query": clean_address(query), "picks": picks[:DRILL_LIMIT]}


async def export_presets(db: Any, guild_id: int) -> list[dict]:
    """Every preset of a server in the own format, filters first, then by name."""
    rows = await db.all("SELECT name, kind, query, picks FROM stat_presets WHERE guild_id = ? ORDER BY kind, name COLLATE NOCASE", (guild_id,))
    return [{"name": row["name"], "kind": row["kind"], "query": _loaded(row["query"], {}), "picks": [str(pick) for pick in _loaded(row["picks"], [])]}
            for row in rows]


async def import_presets(db: Any, guild_id: int, presets: Iterable[Any], *, actor_id: int | None = None, now: float | None = None) -> list[dict]:
    """Saves presets in the own format. One whose kind and name a preset already has, in any case, replaces it. A kind already holding
    PRESETS_PER_KIND takes no new name. Returns each preset as saved, or its name with the sentence that kept it out."""
    stamp = time.time() if now is None else now
    done = []
    for raw in presets:
        try:
            preset = clean_preset(raw)
        except ValueError as error:
            done.append({"name": preset_name(raw.get("name") if isinstance(raw, dict) else None), "error": str(error)})
            continue
        query, picks = json.dumps(preset["query"], sort_keys=True), json.dumps(preset["picks"])
        same = await db.scalar("SELECT id FROM stat_presets WHERE guild_id = ? AND kind = ? AND name = ? COLLATE NOCASE", (guild_id, preset["kind"], preset["name"]))
        if same is not None:
            await db.run("UPDATE stat_presets SET name = ?, query = ?, picks = ?, created_by = ?, created_at = ? WHERE id = ?",
                         (preset["name"], query, picks, actor_id, stamp, same))
        elif await db.scalar("SELECT COUNT(*) FROM stat_presets WHERE guild_id = ? AND kind = ?", (guild_id, preset["kind"])) >= PRESETS_PER_KIND:
            done.append({"name": preset["name"], "error": f"A server keeps up to {PRESETS_PER_KIND} presets of each kind."})
            continue
        else:
            await db.run("INSERT INTO stat_presets (guild_id, name, kind, query, picks, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (guild_id, preset["name"], preset["kind"], query, picks, actor_id, stamp))
        done.append({**preset, "picks": [str(pick) for pick in preset["picks"]]})
    return done


def _loaded(text: Any, fallback: Any) -> Any:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback
