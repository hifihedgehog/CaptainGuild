"""The template language used by tags, triggers, greetings and any message a server writes.

render_message is what other features call to fill in a stored message: it works away from the event loop, within the server's budget, and a person's template can't make it raise. It runs every block that needs nothing
from Discord, and anything unknown stays as written. run_tag is the version for tags and triggers, and it also carries out what the blocks ask of Discord. render_text and render_payload are the synchronous parts underneath, for code already off the loop.
docs/template-language.md describes the language.
"""
from __future__ import annotations

from . import budget
from .budget import OverBudget, budgeted_render, render_message
from .engine import Actions, Interpreter, Scope, split_block
from .payload import TEMPLATE_MAX, clean_template_message, fit_payload, ids_from, read_json_text, render_payload, render_strict, render_text
from .regexguard import RegexGuard
from .runner import BlockContext, TagRun, find_channel, run_tag
from .text import MAX_BLOCKS, MAX_COMMANDS, MAX_REACTIONS, MAX_SECONDS, MAX_SOURCE, MAX_VALUE, MAX_WORK, LimitReached

__all__ = [
    "Actions", "BlockContext", "Interpreter", "LimitReached", "MAX_BLOCKS", "MAX_COMMANDS", "MAX_REACTIONS", "MAX_SECONDS", "MAX_SOURCE", "MAX_VALUE", "MAX_WORK", "OverBudget",
    "RegexGuard", "Scope", "TEMPLATE_MAX", "TagRun", "budget", "budgeted_render", "clean_template_message", "find_channel", "fit_payload", "ids_from", "read_json_text", "render_message", "render_payload",
    "render_strict", "render_text", "run_tag", "split_block",
]
