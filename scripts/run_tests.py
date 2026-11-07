"""Runs the test suite with each test module in its own process, several at once.

    python scripts/run_tests.py                every module, as many at once as the machine has cores
    python scripts/run_tests.py --jobs 4       four at a time
    python scripts/run_tests.py --shard 2/4    the second of four even shares, for splitting a run across machines
    python scripts/run_tests.py tags levels    only modules whose names contain one of these words

Each module runs as `python -m unittest tests.<module>` from the project folder. A module that fails has its whole output printed at the
end, and the exit code is 1.
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


def run(name: str, folder: Path) -> tuple[str, int, float, str, Path]:
    started = time.monotonic()
    log = folder / f"{name}.log"
    with open(log, "w", encoding="utf-8", errors="replace") as out:
        try:
            code = subprocess.run([sys.executable, "-m", "unittest", f"tests.{name}"], cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                                  timeout=MODULE_SECONDS).returncode
        except subprocess.TimeoutExpired:
            code = 124
            out.write(f"\nStopped after {MODULE_SECONDS} seconds.\n")
    found = re.findall(r"^Ran (\d+) tests? in", log.read_text(encoding="utf-8", errors="replace"), re.M)
    return name, code, time.monotonic() - started, found[-1] if found else "?", log


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the test suite in parallel.")
    parser.add_argument("words", nargs="*", help="Run only modules whose names contain one of these words")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2, help="Modules to run at once")
    parser.add_argument("--shard", default="1/1", help="Which share to run, such as 2/4")
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
        running = [pool.submit(run, name, Path(folder)) for name in chosen]
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
