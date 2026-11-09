"""A test process that leaves a database connection open ends with an error that says where it was opened, instead of hanging."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = """
import asyncio
import tests  # the watch on connections
import aiosqlite


async def main(close):
    connection = await aiosqlite.connect(":memory:")
    await connection.execute("SELECT 1")
    if close:
        await connection.close()
    globals()["kept"] = connection  # held to the end, the way a stuck task holds one


asyncio.run(main({close}))
print("finished")
"""


def run(close: bool) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", SCRIPT.format(close=close)], cwd=ROOT, capture_output=True, text=True, timeout=60)


class OpenConnectionsTest(unittest.TestCase):
    def test_a_connection_left_open_ends_the_process_with_where_it_was_opened(self):
        done = run(close=False)
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("finished", done.stdout)
        self.assertIn("1 database connection(s) were never closed", done.stderr)
        self.assertIn('File "<string>", line 8, in main', done.stderr, "the report names the line that opened it")

    def test_a_closed_connection_lets_the_process_end_normally(self):
        done = run(close=True)
        self.assertEqual((done.returncode, done.stderr.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
