"""scripts/run_tests.py, which runs the suite in parallel and splits it across CI jobs."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_tests.py"
spec = importlib.util.spec_from_file_location("run_tests", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class SharesTest(unittest.TestCase):
    def test_every_module_runs_in_exactly_one_share(self):
        names = runner.modules()
        self.assertIn("test_run_tests", names)
        size = {name: (SCRIPT.parent.parent / "tests" / f"{name}.py").stat().st_size for name in names}
        split = runner.shares(names, 4, size)
        self.assertEqual(sorted(name for share in split for name in share), sorted(names))
        self.assertEqual(len(split), 4)

    def test_the_shares_carry_about_equal_work(self):
        size = {f"test_{n}": weight for n, weight in enumerate([90, 80, 70, 60, 50, 40, 30, 20, 10, 10, 10, 10])}
        loads = [sum(size[name] for name in share) for share in runner.shares(list(size), 4, size)]
        self.assertEqual(sum(loads), sum(size.values()))
        self.assertLessEqual(max(loads) - min(loads), 20)

    def test_the_same_files_always_split_the_same_way(self):
        size = {"test_a": 5, "test_b": 5, "test_c": 3}
        self.assertEqual(runner.shares(list(size), 2, size), runner.shares(list(reversed(size)), 2, size))

    def test_words_pick_modules_by_name(self):
        self.assertEqual(runner.modules(["run_tests"]), ["test_run_tests"])


if __name__ == "__main__":
    unittest.main()
