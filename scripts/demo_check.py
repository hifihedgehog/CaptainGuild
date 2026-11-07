"""Starts the demo dashboard on a free port, screenshots pages, reports browser errors, and always stops the server.

    python scripts/demo_check.py OUT_FOLDER page [page ...]

A page can click things first and fill boxes: reactionroles@text=Create new reaction role or tags@#some-selector. Separate steps with @.
Exit code 1 means the browser reported an error or a page failed to load.
It needs Playwright and Microsoft Edge. Set CAPTAINGUILD_SERVER_PYTHON when the site's packages live in a different Python from the one running this.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SERVER_PYTHON = os.environ.get("CAPTAINGUILD_SERVER_PYTHON") or sys.executable
GUILD = "900000000000000001"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(url: str, process: subprocess.Popen, log: Path) -> None:
    deadline = time.time() + 40
    while time.time() < deadline:
        if process.poll() is not None:
            raise SystemExit("The demo server stopped:\n" + log.read_text(encoding="utf-8", errors="replace")[-3000:])
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except Exception:
            time.sleep(0.4)
    raise SystemExit("The demo server didn't answer in time:\n" + log.read_text(encoding="utf-8", errors="replace")[-3000:])


def main() -> int:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data:
        port = free_port()
        log = Path(data) / "server.log"
        with log.open("wb") as sink:
            server = subprocess.Popen([SERVER_PYTHON, "-W", "ignore", "-m", "captainguild", "--demo", "--port", str(port), "--data", data], cwd=ROOT, stdout=sink, stderr=subprocess.STDOUT)
        try:
            base = f"http://127.0.0.1:{port}"
            wait_for(base + "/api/me", server, log)
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1476, "height": 812})
                page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
                page.on("console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" else None)
                page.on("response", lambda r: problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)
                for spec in sys.argv[2:]:
                    name, *steps = spec.split("@")
                    page.goto(f"{base}/dashboard/{GUILD}/{name}", wait_until="networkidle")
                    page.wait_for_timeout(300)
                    for index, step in enumerate(steps):
                        page.click(step, timeout=5000)
                        page.wait_for_timeout(300)
                    target = out / f"{name}{'-' + str(len(steps)) if steps else ''}.png"
                    page.screenshot(path=str(target), full_page=False)
                    print(target)
                    if page.locator("text=This page could not load").count():
                        problems.append(f"{name}: the page could not load")
                browser.close()
        finally:
            server.kill()
            server.wait(timeout=10)
        errors = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if "ERROR" in line or "Traceback" in line]
        problems.extend(f"server: {line}" for line in errors)
    for line in problems:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
