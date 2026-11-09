"""Runs the test suite with each test module in its own process, several at once.

    python scripts/run_tests.py                every module, as many at once as the machine has cores
    python scripts/run_tests.py --jobs 4       four at a time
    python scripts/run_tests.py --shard 2/4    the second of four even shares, for splitting a run across machines
    python scripts/run_tests.py tags levels    only modules whose names contain one of these words
    python scripts/run_tests.py --module-seconds 900    stop a module after 15 minutes

Each module runs under unittest from the project folder. A module that fails has its whole output printed at the end, and the exit code
is 1. A module still running at --module-seconds is stopped and counts as failed, and shortly before that it prints the stack of every
thread it has, so a hang shows where it waits.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_SECONDS = 1800
# Runs one module the way `python -m unittest tests.<module>` does, with every thread's stack printed at the given second.
UNDER_WATCH = ("import faulthandler, sys, unittest\n"
               "faulthandler.dump_traceback_later(float(sys.argv[2]), exit=False)\n"
               "unittest.main(module=None, argv=['unittest', 'tests.' + sys.argv[1]])")


def stacks_at(seconds: float) -> float:
    """When a module prints its threads' stacks: a minute before it's stopped, or halfway for a short limit."""
    return seconds - 60 if seconds > 120 else seconds / 2


def modules(words: list[str] | None = None) -> list[str]:
    names = sorted(path.stem for path in (ROOT / "tests").glob("test_*.py"))
    return [name for name in names if any(word in name for word in words)] if words else names


def shares(names: list[str], count: int, size: dict[str, int]) -> list[list[str]]:
    """Splits the modules into `count` shares of about equal work. A test file's size stands in for its running time, and the biggest
    files are placed first, each in the share with the least so far. The same files always split the same way."""
    split: list[list[str]] = [[] for _ in range(count)]
    load = [0] * count
    for name in sorted(names, key=lambda n: (-size[n], n)):
        lightest = load.index(min(load))
        split[lightest].append(name)
        load[lightest] += size[name]
    return [sorted(share) for share in split]


def run(name: str, folder: Path, seconds: float = MODULE_SECONDS, root: Path = ROOT) -> tuple[str, int, float, str, Path]:
    started = time.monotonic()
    log = folder / f"{name}.log"
    with open(log, "w", encoding="utf-8", errors="replace") as out:
        try:
            code = subprocess.run([sys.executable, "-X", "faulthandler", "-c", UNDER_WATCH, name, str(stacks_at(seconds))], cwd=root, stdout=out,
                                  stderr=subprocess.STDOUT, timeout=seconds).returncode
        except subprocess.TimeoutExpired:
            code = 124
            out.write(f"\nStopped after {seconds:g} seconds.\n")
    found = re.findall(r"^Ran (\d+) tests? in", log.read_text(encoding="utf-8", errors="replace"), re.M)
    return name, code, time.monotonic() - started, found[-1] if found else "?", log


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the test suite in parallel.")
    parser.add_argument("words", nargs="*", help="Run only modules whose names contain one of these words")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2, help="Modules to run at once")
    parser.add_argument("--shard", default="1/1", help="Which share to run, such as 2/4")
    parser.add_argument("--module-seconds", type=float, default=MODULE_SECONDS, help="Stop a module that runs longer than this")
    args = parser.parse_args()
    match = re.fullmatch(r"(\d+)/(\d+)", args.shard)
    if not match or not 1 <= int(match.group(1)) <= int(match.group(2)):
        parser.error("--shard takes a share and a count, such as 2/4")
    names = modules(args.words)
    size = {name: (ROOT / "tests" / f"{name}.py").stat().st_size for name in names}
    chosen = shares(names, int(match.group(2)), size)[int(match.group(1)) - 1]
    print(f"Running {len(chosen)} of {len(names)} test modules, {max(1, args.jobs)} at once.", flush=True)
    failed = []
    total = 0
    with tempfile.TemporaryDirectory() as folder, concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        running = [pool.submit(run, name, Path(folder), args.module_seconds) for name in chosen]
        for done in concurrent.futures.as_completed(running):
            name, code, seconds, count, log = done.result()
            print(f"{'ok  ' if code == 0 else 'FAIL'} {name}: {count} tests, {seconds:.0f}s", flush=True)
            total += int(count) if count.isdigit() else 0
            if code != 0:
                failed.append((name, log.read_text(encoding="utf-8", errors="replace")))
        for name, text in failed:
            print(f"\n===== {name} =====\n{text}", flush=True)
    print(f"\n{total} tests in {len(chosen)} modules, {len(failed)} modules failed{': ' + ', '.join(n for n, _ in failed) if failed else ''}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
