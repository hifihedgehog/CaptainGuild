"""CaptainGuild.py, the file a process manager starts."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent.parent / "CaptainGuild.py"


class LauncherTest(unittest.TestCase):
    def test_it_starts_the_program(self):
        result = subprocess.run([sys.executable, str(LAUNCHER), "--help"], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--public-url", result.stdout)

    def test_packages_in_its_lib_folder_come_before_the_machines(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            base = Path(folder)
            shutil.copy(LAUNCHER, base / "CaptainGuild.py")
            package = base / "lib" / "captainguild"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "__main__.py").write_text("def main():\n    print('from the lib folder')\n", encoding="utf-8")
            result = subprocess.run([sys.executable, str(base / "CaptainGuild.py")], capture_output=True, text=True, timeout=120, cwd=base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "from the lib folder")


if __name__ == "__main__":
    unittest.main()
