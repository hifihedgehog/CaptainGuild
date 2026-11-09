"""The Statistics module is one class built from four mixins. A name two of them define hands one mixin's calls to the other's code without
a word, so each name lives in one place, apart from hooks that every mixin passes along with super()."""
from __future__ import annotations

import inspect
import unittest

from captainguild.modules.statistics import Statistics
from captainguild.modules.statistics_commands import StatsCommands
from captainguild.modules.statistics_data import StatsData
from captainguild.modules.statistics_tools import StatsTools
from captainguild.modules.statistics_web import StatsWeb

MIXINS = (StatsCommands, StatsWeb, StatsTools, StatsData)
CHAINED = {"on_guild_restored"}  # each mixin's version awaits the next one's through super()


def own_names(cls: type) -> set[str]:
    return {name for name in vars(cls) if not (name.startswith("__") and name.endswith("__"))}


class MixinsTest(unittest.TestCase):
    def test_no_two_parts_define_the_same_name(self):
        seen: dict[str, str] = {}
        clashes = []
        for part in (Statistics, *MIXINS):
            for name in sorted(own_names(part) - CHAINED):
                if name in seen:
                    clashes.append(f"{name}: {seen[name]} and {part.__name__}")
                seen[name] = part.__name__
        self.assertEqual(clashes, [])

    def test_every_chained_hook_passes_the_call_along(self):
        for name in CHAINED:
            for part in MIXINS:
                if name in vars(part):
                    self.assertIn("super()", inspect.getsource(vars(part)[name]), f"{part.__name__}.{name}")


if __name__ == "__main__":
    unittest.main()
