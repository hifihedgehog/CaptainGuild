"""The Welcome image page's own JavaScript, run in a headless browser: the designer's mouse, keyboard and touch handling, undo, the layer
list, and how often the preview asks the server for a drawing. The static files are served as the site serves them and a page imports the
real modules. Skipped where no Chromium-based browser is installed."""
from __future__ import annotations

import asyncio
import html
import json
import re
import shutil
import sys
import tempfile
import unittest

from aiohttp import web

from captainguild.core.welcomecard import fonts, model
from captainguild.modules import welcomecard as module
from tests.test_statistics_browser import STATIC, _browser

DESIGN = model.clean_design({"width": 1000, "height": 300, "background": {"kind": "color", "color": "#223344"}, "layers": [
    {"id": "back", "type": "rect", "name": "Back", "x": 0, "y": 0, "w": 1000, "h": 300},
    {"id": "avatar", "type": "avatar", "x": 32, "y": 32, "w": 236, "h": 236},
    {"id": "text", "type": "text", "x": 300, "y": 32, "w": 668, "h": 236, "text": "Hello {user}"},
]}, fonts.KEYS)

DESIGNER = """<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/css/app.css">
<div id="root" style="width: 1100px"></div><pre id="out">running</pre>
<script type="module">
const out = {};
try {
  const { html, render, useState } = await import('/static/js/lib.js');
  const { GuildContext } = await import('/static/js/ui.js');
  const { Designer } = await import('/static/js/welcomecard/designer.js');
  const parts = await import('/static/js/welcomecard/parts.js');
  const tick = (wait = 40) => new Promise((done) => setTimeout(done, wait));
  const design = __DESIGN__;
  const catalog = __CATALOG__;
  const pixel = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';
  let latest = null;
  const sets = [];
  function Harness() {
    const [data, setData] = useState({ design, layout: 'wide' });
    latest = data;
    const form = { data, errors: {}, set: (field, value) => { sets.push(field); setData((old) => ({ ...old, [field]: value })); } };
    const uploads = { uploads: [], loaded: true, most: 24, used: 0, room: 1, busy: false, add: async () => null, remove: async () => false };
    const preview = { shown: { image: pixel, width: data.design.width, height: data.design.height, kind: 'png', frames: 1, bytes: 2048, notes: [] }, error: null, busy: false };
    return html`<${GuildContext.Provider} value=${{ id: '1' }}><${Designer} form=${form} catalog=${catalog} uploads=${uploads} preview=${preview} /><//>`;
  }
  render(html`<${Harness} />`, document.getElementById('root'));
  await tick(150);

  const one = (selector) => document.querySelector(selector);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const layer = (id) => ({ ...latest.design.layers.find((row) => row.id === id) });
  const rows = () => all('.wc-layer:not(.wc-canvas-row)').length;
  const boxOf = (id) => one('.wc-box[data-layer="' + id + '"]');
  const middle = (element) => { const r = element.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; };
  const point = (target, type, x, y, more = {}) => target.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0, pointerType: 'mouse', ...more }));
  const press = async (target, name, more = {}) => { target.dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true, ...more })); await tick(); };
  const selected = () => { const box = one('.wc-box.on'); return box ? box.dataset.layer : null; };
  const undoOff = () => one('button[title^=Undo]').disabled;
  const pick = async (name) => { all('.wc-layer-pick').find((button) => button.textContent.trim().startsWith(name)).click(); await tick(); };
  const drag = async (target, from, to, more = {}) => {
    point(target, 'pointerdown', from[0], from[1], more);
    await tick();
    point(window, 'pointermove', (from[0] + to[0]) / 2, (from[1] + to[1]) / 2, more);
    point(window, 'pointermove', to[0], to[1], more);
    point(window, 'pointerup', to[0], to[1], more);
    await tick();
  };
  const onCanvas = (x, y) => { const box = one('.wc-canvas').getBoundingClientRect(); return [box.left + x * scale, box.top + y * scale]; };
  const scale = one('.wc-canvas').getBoundingClientRect().width / design.width;
  out.scale = scale;
  out.colors = [parts.hexOf('#11223380'), parts.solidOf('#11223380'), parts.withSolid('#112233', 50), parts.withSolid('#112233', 100), parts.withSolid('#112233', 0)];
  out.start = { boxes: all('.wc-box').length, rows: rows(), canvasRow: one('.wc-canvas-row').classList.contains('on'), undoOff: undoOff(), selected: selected(),
    labels: all('.wc-box').map((box) => box.getAttribute('aria-label')), title: one('.wc-designer-side .card:nth-child(2) .card-head span').textContent };

  // A click selects the layer and changes nothing.
  const at = middle(boxOf('avatar'));
  await drag(boxOf('avatar'), at, at);
  out.click = { selected: selected(), handles: all('.wc-box.on .wc-handle').length, sets: sets.length, undoOff: undoOff(), focused: document.activeElement === boxOf('avatar'),
    title: one('.wc-designer-side .card:nth-child(2) .card-head span').textContent };

  // A drag moves it, and Undo comes on.
  await drag(boxOf('avatar'), at, [at[0] + 66, at[1] + 33]);
  out.move = { ...layer('avatar'), undoOff: undoOff() };

  // The keyboard.
  const before = layer('avatar');
  await press(boxOf('avatar'), 'ArrowRight');
  await press(boxOf('avatar'), 'ArrowDown', { shiftKey: true });
  const moved = layer('avatar');
  await press(boxOf('avatar'), 'ArrowRight', { altKey: true });
  await press(boxOf('avatar'), 'ArrowUp', { altKey: true, shiftKey: true });
  const sized = layer('avatar');
  await press(boxOf('avatar'), 'z', { ctrlKey: true });
  const undone = layer('avatar');
  const stageHasKeys = document.activeElement === one('.wc-stage');
  await press(one('.wc-stage'), 'y', { ctrlKey: true });
  out.keys = { before, moved, sized, undone, redone: layer('avatar'), stageHasKeys };

  // The round handle turns the layer, and Shift takes steps of 15 degrees.
  const turnTo = async (dx, dy, more) => {
    const now = layer('avatar');
    const mid = onCanvas(now.x + now.w / 2, now.y + now.h / 2);
    await drag(one('.wc-box.on .wc-turn'), middle(one('.wc-box.on .wc-turn')), [mid[0] + dx, mid[1] + dy], more);
    return layer('avatar').rotation;
  };
  out.turn = { right: await turnTo(120, 0), stepped: await turnTo(100, -80, { shiftKey: true }) };

  // Sizing a layer turned a quarter turn: its right edge now faces down, and the edge across from it stays where it is.
  await turnTo(120, 0);
  const turned = layer('avatar');
  const handle = all('.wc-box.on .wc-handle').find((found) => found.style.left === '100%' && found.style.top === '50%');
  const from = middle(handle);
  await drag(handle, from, [from[0], from[1] + 40]);
  const grown = layer('avatar');
  const farEdge = (found) => [found.x + found.w / 2, found.y + found.h / 2 - found.w / 2];
  out.turned = { before: turned, after: grown, edgeBefore: farEdge(turned), edgeAfter: farEdge(grown) };

  // A layer under another: a drag inside the selected one moves it, and a click goes to the layer on top there.
  await pick('Back');
  const spot = onCanvas(800, 150);
  const hit = document.elementFromPoint(spot[0], spot[1]);
  const backBefore = layer('back');
  await drag(hit, spot, [spot[0], spot[1] + 30]);
  const backAfter = layer('back');
  const stillBack = selected();
  await press(one('.wc-stage'), 'z', { ctrlKey: true });
  await drag(document.elementFromPoint(spot[0], spot[1]), spot, spot);
  out.under = { hit: (hit.closest('.wc-box') || hit).dataset.layer, movedBy: backAfter.y - backBefore.y, stillBack, backAgain: layer('back').y, afterClick: selected() };

  // Delete removes the selected layer, the picture keeps the keyboard, and Undo brings the layer back.
  await press(boxOf('text'), 'Delete');
  const gone = { rows: rows(), stageHasKeys: document.activeElement === one('.wc-stage'), selected: selected() };
  await press(one('.wc-stage'), 'z', { ctrlKey: true });
  out.remove = { ...gone, back: rows() };

  // Adding a layer selects it.
  all('.wc-add .btn').find((button) => button.textContent.trim() === 'Rectangle').click();
  await tick();
  out.added = { rows: rows(), selected: selected(), title: one('.wc-designer-side .card:nth-child(2) .card-head span').textContent, id: latest.design.layers[latest.design.layers.length - 1].id };

  // A finger drags only the selected layer.
  const far = onCanvas(900, 240);
  const textBefore = layer('text');
  await drag(document.elementFromPoint(far[0], far[1]), far, [far[0] - 40, far[1]], { pointerType: 'touch' });
  const leftAlone = layer('text').x === textBefore.x && selected() !== 'text';
  await pick('Hello');
  await drag(document.elementFromPoint(far[0], far[1]), far, [far[0] - 40, far[1]], { pointerType: 'touch' });
  out.touch = { leftAlone, movedBy: layer('text').x - textBefore.x };

  // A hidden layer has no box on the picture.
  const shown = all('.wc-box').length;
  one('.wc-layer.on button[title=Hide]').click();
  await tick();
  out.hide = { before: shown, after: all('.wc-box').length, dimmed: !!one('.wc-layer.on.off') };

  // Escape lets go of the layer, and the Canvas row takes its place in the side panel.
  await pick('Back');
  await press(boxOf('back'), 'Escape');
  out.escape = { selected: selected(), canvasRow: one('.wc-canvas-row').classList.contains('on'), title: one('.wc-designer-side .card:nth-child(2) .card-head span').textContent };
} catch (error) { out.error = String((error && error.stack) || error); }
document.getElementById('out').textContent = JSON.stringify(out);
</script>"""

PREVIEW = """<!doctype html><meta charset="utf-8"><div id="root"></div><pre id="out">running</pre>
<script type="module">
const out = {};
try {
  const { html, render, useState } = await import('/static/js/lib.js');
  const { GuildContext } = await import('/static/js/ui.js');
  const parts = await import('/static/js/welcomecard/parts.js');
  const { setGuild } = await import('/static/js/api.js');
  setGuild('7');
  const tick = (wait) => new Promise((done) => setTimeout(done, wait));
  const began = Date.now();
  const calls = [];
  let answerAfter = 20;
  let flying = 0;
  let most = 0;
  window.fetch = async (path, options) => {
    flying += 1;
    most = Math.max(most, flying);
    calls.push({ path, at: Date.now() - began, text: JSON.parse(options.body).draft.text });
    await tick(answerAfter);
    flying -= 1;
    return new Response(JSON.stringify({ image: 'data:,', width: 10, height: 10, kind: 'png', frames: 1, bytes: 1, notes: [], sizes: {}, canvas: {} }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  let change = null;
  let shownNow = null;
  function Watcher() {
    const [count, setCount] = useState(0);
    change = () => setCount((old) => old + 1);
    const preview = parts.usePreview({ text: 'v' + count });
    shownNow = preview.shown;
    return null;
  }
  render(html`<${GuildContext.Provider} value=${{ id: '7' }}><${Watcher} /><//>`, document.getElementById('root'));
  await tick(600);
  out.first = calls.map((call) => call.text);
  out.shownAfterFirst = !!shownNow;

  // Two seconds of steady changes, with a server that answers at once.
  const startedChanging = Date.now() - began;
  for (let step = 0; step < 40; step += 1) { change(); await tick(50); }
  out.during = calls.filter((call) => call.at >= startedChanging).map((call) => call.at - startedChanging);
  await tick(900);
  out.afterFast = calls[calls.length - 1].text;
  out.mostWhileFast = most;

  // The same with a server slower than the changes are apart.
  answerAfter = 800;
  for (let step = 0; step < 30; step += 1) { change(); await tick(50); }
  await tick(2600);
  out.afterSlow = calls[calls.length - 1].text;
  out.most = most;
  out.paths = [...new Set(calls.map((call) => call.path))];
  const counted = calls.length;
  await tick(1500);
  out.settled = calls.length === counted;
} catch (error) { out.error = String((error && error.stack) || error); }
document.getElementById('out').textContent = JSON.stringify(out);
</script>"""

_found: dict[str, dict] = {}


async def run_page(page: str) -> dict:
    """Serves a harness page beside the site's static files, opens it in a headless browser and reads back what it wrote."""
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
        sandbox = ["--no-sandbox"] if sys.platform.startswith("linux") else []
        process = await asyncio.create_subprocess_exec(_browser(), "--headless=new", "--disable-gpu", "--no-first-run", *sandbox, f"--user-data-dir={profile}", "--window-size=1400,1000",
                                                       "--virtual-time-budget=20000", "--dump-dom", f"http://127.0.0.1:{port}/harness",
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        dom, errors = await asyncio.wait_for(process.communicate(), 120)
    finally:
        await runner.cleanup()
        shutil.rmtree(profile, ignore_errors=True)
    found = re.search(r'<pre id="out">(.*?)</pre>', dom.decode("utf-8", "replace"), re.S)
    if found is None:
        raise AssertionError((dom[-500:], errors.decode("utf-8", "replace")[-1500:]))
    result = json.loads(html.unescape(found.group(1)))
    if "error" in result:
        raise AssertionError(result["error"])
    return result


@unittest.skipUnless(_browser(), "needs a Chromium-based browser")
class DesignerScripts(unittest.IsolatedAsyncioTestCase):
    """One run of the designer in the browser, read by every test here."""

    async def asyncSetUp(self) -> None:
        if "designer" not in _found:
            _found["designer"] = await run_page(DESIGNER.replace("__DESIGN__", json.dumps(DESIGN)).replace("__CATALOG__", json.dumps(module.catalog())))
        self.found = _found["designer"]
        self.scale = self.found["scale"]

    def on_design(self, pixels: float) -> int:
        """A distance on screen as the whole pixels of the design it comes to."""
        return round(pixels / self.scale)

    async def test_it_opens_with_a_box_for_every_layer_and_the_canvas_settings(self):
        start = self.found["start"]
        self.assertEqual((start["boxes"], start["rows"]), (3, 3))
        self.assertEqual((start["selected"], start["canvasRow"], start["title"]), (None, True, "Canvas"))
        self.assertTrue(start["undoOff"])
        self.assertEqual(start["labels"], ["Back, rectangle layer", "Avatar layer", "Hello {user}, text layer"], "what a screen reader says for each layer")
        self.assertLess(self.scale, 1, "the picture is scaled to fit its place")

    async def test_a_click_selects_and_changes_nothing(self):
        click = self.found["click"]
        self.assertEqual((click["selected"], click["title"]), ("avatar", "Avatar layer"))
        self.assertEqual(click["handles"], 9, "eight to size it and one to turn it")
        self.assertEqual(click["sets"], 0, "the design is as it was")
        self.assertTrue(click["undoOff"], "and there is nothing to undo")
        self.assertTrue(click["focused"], "the keyboard goes to the layer with the mouse")

    async def test_a_drag_moves_the_layer_by_what_the_pointer_traveled_on_the_design(self):
        move = self.found["move"]
        self.assertEqual((move["x"], move["y"]), (32 + self.on_design(66), 32 + self.on_design(33)))
        self.assertEqual((move["w"], move["h"]), (236, 236))
        self.assertFalse(move["undoOff"])

    async def test_arrow_keys_move_and_size_and_undo_takes_a_run_of_them_back_at_once(self):
        keys = self.found["keys"]
        before, moved, sized = keys["before"], keys["moved"], keys["sized"]
        self.assertEqual((moved["x"], moved["y"]), (before["x"] + 1, before["y"] + 10), "an arrow moves by 1 and with Shift by 10")
        self.assertEqual((sized["w"], sized["h"]), (before["w"] + 1, before["h"] - 10), "Alt sizes instead")
        self.assertEqual((sized["x"], sized["y"]), (moved["x"], moved["y"]))
        self.assertEqual(keys["undone"], moved, "one Ctrl+Z takes back both sizing keys and leaves the moves")
        self.assertEqual(keys["redone"], sized)
        self.assertTrue(keys["stageHasKeys"], "after an undo the picture has the keyboard, so the next key lands")

    async def test_the_round_handle_turns_and_shift_takes_steps_of_fifteen(self):
        self.assertEqual(self.found["turn"], {"right": 90, "stepped": 45})

    async def test_a_turned_layer_sizes_the_way_its_handle_points_and_keeps_its_far_edge(self):
        turned = self.found["turned"]
        self.assertEqual(turned["before"]["rotation"], 90)
        self.assertEqual(turned["after"]["w"], turned["before"]["w"] + self.on_design(40), "the right edge faces down after a quarter turn, so a pull down widens it")
        self.assertEqual(turned["after"]["h"], turned["before"]["h"])
        for was, now in zip(turned["edgeBefore"], turned["edgeAfter"]):
            self.assertAlmostEqual(was, now, delta=1.01, msg="the edge across from the handle stays where it is")

    async def test_a_covered_layer_is_dragged_from_inside_and_a_click_goes_to_the_layer_on_top(self):
        under = self.found["under"]
        self.assertEqual(under["hit"], "back", "the selected layer is above the rest")
        self.assertEqual((under["movedBy"], under["stillBack"]), (self.on_design(30), "back"))
        self.assertEqual(under["backAgain"], 0, "Ctrl+Z put it back")
        self.assertEqual(under["afterClick"], "text")

    async def test_delete_removes_the_layer_and_undo_brings_it_back(self):
        remove = self.found["remove"]
        self.assertEqual((remove["rows"], remove["selected"], remove["back"]), (2, None, 3))
        self.assertTrue(remove["stageHasKeys"])

    async def test_adding_a_layer_selects_it(self):
        self.assertEqual(self.found["added"], {"rows": 4, "selected": "l1", "title": "Rectangle layer", "id": "l1"})

    async def test_a_finger_drags_only_the_selected_layer(self):
        touch = self.found["touch"]
        self.assertTrue(touch["leftAlone"], "a swipe over a layer that is not selected is left to scroll the page")
        self.assertEqual(touch["movedBy"], self.on_design(-40))

    async def test_a_hidden_layer_has_no_box(self):
        self.assertEqual(self.found["hide"], {"before": 4, "after": 3, "dimmed": True})

    async def test_escape_lets_go_of_the_layer(self):
        self.assertEqual(self.found["escape"], {"selected": None, "canvasRow": True, "title": "Canvas"})

    async def test_a_colors_solid_share_reads_and_writes_as_two_hex_digits(self):
        self.assertEqual(self.found["colors"], ["#112233", 50, "#11223380", "#112233", "#11223300"])


@unittest.skipUnless(_browser(), "needs a Chromium-based browser")
class PreviewScripts(unittest.IsolatedAsyncioTestCase):
    """One run of the preview's timing in the browser, against a server the page itself stands in for."""

    async def asyncSetUp(self) -> None:
        if "preview" not in _found:
            _found["preview"] = await run_page(PREVIEW)
        self.found = _found["preview"]

    async def test_the_first_drawing_is_asked_for_once(self):
        self.assertEqual(self.found["first"], ["v0"])
        self.assertTrue(self.found["shownAfterFirst"])
        self.assertEqual(self.found["paths"], ["/api/g/7/welcomecard/preview"], "every ask goes to the preview route of the server in view")

    async def test_a_long_run_of_changes_is_drawn_now_and_then_and_once_more_at_its_end(self):
        during = self.found["during"]
        self.assertGreaterEqual(len(during), 2, "two seconds of steady changes are drawn along the way, not only at the end")
        self.assertLessEqual(len(during), 4, "and not for every change")
        for earlier, later in zip(during, during[1:]):
            self.assertGreaterEqual(later - earlier, 600)
        self.assertLess(during[0], 1000, "the first of them within a second of the first change")
        self.assertEqual(self.found["afterFast"], "v40", "what is drawn last is the last change")
        self.assertTrue(self.found["settled"], "nothing more is asked once that is drawn")

    async def test_one_drawing_is_asked_for_at_a_time(self):
        self.assertEqual(self.found["mostWhileFast"], 1)
        self.assertEqual(self.found["most"], 1, "a server slower than the changes are apart still gets one ask at a time")
        self.assertEqual(self.found["afterSlow"], "v70", "and the last change is still the last one drawn")


if __name__ == "__main__":
    unittest.main()
