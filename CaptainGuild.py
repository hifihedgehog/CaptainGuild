"""Starts Captain Guild.

    python CaptainGuild.py --public-url https://your.site --port 8893 --data data

Packages installed into a lib folder beside this file come first, so this bot's versions can't change what other programs on the machine use.
"""
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent / "lib"
if LIB.is_dir():
    sys.path.insert(0, str(LIB))

from captainguild.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
