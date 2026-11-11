// The layer designer of the Welcome image page. The canvas shows the picture as the bot draws it, and the boxes over it move, size and turn
// the layers. Every change is drawn again by the server a moment later, so what is on screen is what Discord gets.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, Check, Icon, Notice, Select, TextInput, confirmDialog, toastError } from '../ui.js';
import { ColorField, FillPicker, FontPicker, PreviewNotes, TEXT_HELP, TemplateArea, UploadGrid, StockGrid } from './parts.js';

const TYPE_NAMES = { text: 'Text', picture: 'Picture', avatar: 'Avatar', rect: 'Rectangle', ellipse: 'Ellipse' };
const TYPE_ICONS = { text: 'pencil', picture: 'image', avatar: 'user', rect: 'layers-3', ellipse: 'smile' };
// The eight handles around a selected layer: which way each one pulls, and the cursor that says so.
const HANDLES = [[-1, -1, 'nwse-resize'], [0, -1, 'ns-resize'], [1, -1, 'nesw-resize'], [1, 0, 'ew-resize'], [1, 1, 'nwse-resize'], [0, 1, 'ns-resize'], [-1, 1, 'nesw-resize'], [-1, 0, 'ew-resize']];
const SMALLEST = 4;
const HISTORY = 100;
// The height the page's header, tabs and save bar and the picture card's own title, notes and padding take around the picture.
const AROUND = 445;
// A handle's size on screen, for a mouse and for a finger. An edge keeps its middle handle while four handles fit along it.
const GRIP = { fine: 12, coarse: 22 };

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

function freshId(design) {
  const taken = new Set(design.layers.map((layer) => layer.id));
  let number = 1;
  while (taken.has('l' + number)) number += 1;
  return 'l' + number;
}

// A new layer of each type, in the middle of the canvas, with every field the panel reads. A picture starts as a stock background, which
// always draws, and its panel offers an upload or an address on Discord from there.
function newLayer(type, design, stock) {
  const sizes = { text: [Math.min(600, design.width - 40), 110], picture: [200, 200], avatar: [200, 200], rect: [320, 160], ellipse: [200, 200] };
  const w = clamp(sizes[type][0], SMALLEST, design.width);
  const h = clamp(sizes[type][1], SMALLEST, design.height);
  const base = { id: freshId(design), type, name: '', hidden: false, x: Math.round((design.width - w) / 2), y: Math.round((design.height - h) / 2), w, h, rotation: 0, flip_x: false, flip_y: false, opacity: 100 };
  if (type === 'text') {
    return { ...base, fill: { kind: 'color', color: '#ffffff' }, stroke: { color: '#000000', width: 3 }, text: 'New text', font: 'fredoka', weight: 600, size: 48, line_height: 1.2, letter_spacing: 0,
      align: 'center', valign: 'middle', wrap: true, shrink: true };
  }
  if (type === 'picture') {
    return { ...base, fill: { kind: 'none' }, stroke: { color: '#ffffff', width: 0 }, radius: [0, 0, 0, 0], source: { kind: 'stock', name: stock }, fit: 'cover' };
  }
  if (type === 'avatar') return { ...base, fill: { kind: 'color', color: '#48525c' }, stroke: { color: '#ffffff', width: 8 }, radius: [0, 0, 0, 0], shape: 'circle' };
  if (type === 'rect') return { ...base, fill: { kind: 'color', color: '#00000080' }, stroke: { color: '#ffffff', width: 0 }, radius: [16, 16, 16, 16] };
  return { ...base, fill: { kind: 'color', color: '#00a2f8' }, stroke: { color: '#ffffff', width: 0 } };
}

const layerLabel = (layer) => layer.name || (layer.type === 'text' && layer.text ? layer.text.split('\n')[0].slice(0, 28) : TYPE_NAMES[layer.type]);
// What a screen reader says for a layer on the picture: its own name when it has one, and always its type.
const spokenLabel = (layer) => (layerLabel(layer) === TYPE_NAMES[layer.type] ? `${TYPE_NAMES[layer.type]} layer` : `${layerLabel(layer)}, ${TYPE_NAMES[layer.type].toLowerCase()} layer`);

// ---- the canvas ----

function Stage({ design, selected, preview, onSelect, onPatch, onBegin, onKey }) {
  const wrap = useRef(null);
  const stage = useRef(null);
  const canvas = useRef(null);
  const drag = useRef(null);
  const [room, setRoom] = useState({ wide: 0, high: 0 });
  useEffect(() => {
    const measure = () => {
      if (!wrap.current) return;
      const next = { wide: wrap.current.clientWidth, high: window.innerHeight };
      // A page scrollbar that comes and goes changes the width by its own thickness. Growing by that little is not taken, so a picture
      // whose height decides whether the page scrolls can't flip back and forth between two sizes.
      setRoom((old) => {
        const wide = old.wide && next.wide > old.wide && next.wide - old.wide <= 20 ? old.wide : next.wide;
        return old.wide === wide && old.high === next.high ? old : { wide, high: next.high };
      });
    };
    measure();
    const watcher = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null;
    if (watcher && wrap.current) watcher.observe(wrap.current);
    window.addEventListener('resize', measure);
    return () => { if (watcher) watcher.disconnect(); window.removeEventListener('resize', measure); };
  }, []);
  const frame = design.frame.width;
  const fullWidth = design.width + 2 * frame;
  const fullHeight = design.height + 2 * frame;
  // The picture is shown whole and never larger than life: it fits the width it has, and a tall one fits the window too.
  const tallest = Math.max(160, room.high - AROUND);
  const scale = room.wide ? Math.min(1, room.wide / fullWidth, tallest / fullHeight) : 1;
  const shown = preview.shown;
  const grip = typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(pointer: coarse)').matches ? GRIP.coarse : GRIP.fine;
  const handlesOf = (layer) => HANDLES.filter(([pullX, pullY]) => (pullX !== 0 || layer.w * scale >= 4 * grip) && (pullY !== 0 || layer.h * scale >= 4 * grip));

  const track = (event) => {
    const held = drag.current;
    if (!held) return;
    if (!held.begun) {
      // A press that never travels is a click: it selects the layer and leaves the design, and Undo, as they were.
      if (Math.abs(event.clientX - held.x) + Math.abs(event.clientY - held.y) < 2) return;
      held.begun = true;
      onBegin();
    }
    const start = held.layer;
    const dx = (event.clientX - held.x) / held.scale;
    const dy = (event.clientY - held.y) / held.scale;
    if (held.kind === 'move') {
      onPatch(held.id, { x: Math.round(start.x + dx), y: Math.round(start.y + dy) });
      return;
    }
    const turn = (start.rotation * Math.PI) / 180;
    const cos = Math.cos(turn);
    const sin = Math.sin(turn);
    if (held.kind === 'turn') {
      const centerX = held.left + (start.x + start.w / 2) * held.scale;
      const centerY = held.top + (start.y + start.h / 2) * held.scale;
      let angle = (Math.atan2(event.clientY - centerY, event.clientX - centerX) * 180) / Math.PI + 90;
      if (event.shiftKey) angle = Math.round(angle / 15) * 15;
      onPatch(held.id, { rotation: ((Math.round(angle) + 540) % 360) - 180 });
      return;
    }
    // The pointer's move along the layer's own two directions, so a turned layer sizes the way its handle points.
    const along = dx * cos + dy * sin;
    const across = -dx * sin + dy * cos;
    let w = start.w + held.pullX * along;
    let h = start.h + held.pullY * across;
    if (event.shiftKey && held.pullX && held.pullY) {
      const shape = start.w / start.h;
      if (w / h > shape) h = w / shape; else w = h * shape;
    }
    w = clamp(w, SMALLEST, 2000);
    h = clamp(h, SMALLEST, 2000);
    // The edge across from the handle stays put, so the middle moves by half of what was added, turned to the layer's angle.
    const shiftX = (held.pullX * (w - start.w)) / 2;
    const shiftY = (held.pullY * (h - start.h)) / 2;
    const centerX = start.x + start.w / 2 + shiftX * cos - shiftY * sin;
    const centerY = start.y + start.h / 2 + shiftX * sin + shiftY * cos;
    onPatch(held.id, { x: Math.round(centerX - w / 2), y: Math.round(centerY - h / 2), w: Math.round(w), h: Math.round(h) });
  };

  const grab = (event, layer, kind, pullX = 0, pullY = 0) => {
    if (event.button !== undefined && event.button !== 0) return;
    event.stopPropagation();
    // A finger drags only the selected layer. On any other it is a tap, which selects, or a swipe, which scrolls the page.
    if (event.pointerType === 'touch' && layer.id !== selected) return;
    event.preventDefault();
    // The press takes the keyboard to the layer as well, so the arrow keys follow the mouse.
    const own = event.currentTarget.closest('.wc-box');
    if (own) own.focus();
    const box = canvas.current.getBoundingClientRect();
    onSelect(layer.id);
    drag.current = { kind, pullX, pullY, id: layer.id, x: event.clientX, y: event.clientY, layer: { ...layer }, left: box.left, top: box.top, scale, begun: false };
    const end = (last) => {
      window.removeEventListener('pointermove', track);
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
      const held = drag.current;
      drag.current = null;
      if (!held || held.begun || held.kind !== 'move' || !last || last.type !== 'pointerup' || !canvas.current) return;
      // The selected layer sits above the rest so that a drag inside it always moves it. A click is different: it goes to the layer
      // that is on top at that spot in the picture, which is how a layer covered by the selected one is reached.
      const under = document.elementsFromPoint(last.clientX, last.clientY).map((found) => found.dataset && found.dataset.layer).filter(Boolean);
      const top = [...design.layers].reverse().find((row) => under.includes(row.id));
      const box = top && top.id !== held.id && [...canvas.current.children].find((child) => child.dataset.layer === top.id);
      if (!box) return;
      onSelect(top.id);
      box.focus();
    };
    window.addEventListener('pointermove', track);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
  };

  // A key that takes a layer away, puts one back or reorders them can drop the layer the keyboard was on, so the picture itself takes the
  // keyboard first and the next key still lands here.
  const keyed = (event) => { if (onKey(event) && stage.current) stage.current.focus(); };

  return html`<div>
    <div class="wc-stage-wrap"><div ref=${wrap}>
    <div class="wc-stage" ref=${stage} tabindex="0" role="group" aria-label="The picture. Tab reaches each layer, and the arrow keys move the selected one." style=${{ width: fullWidth * scale + 'px', height: fullHeight * scale + 'px' }}
      onKeyDown=${keyed} onPointerDown=${(event) => { if (event.target === event.currentTarget || event.target.classList.contains('wc-stage-img') || event.target === canvas.current) onSelect(null); }}>
      ${shown ? html`<img class="wc-stage-img" src=${shown.image} alt="The welcome picture as it is drawn" draggable=${false} />` : html`<div class="wc-stage-wait">Drawing...</div>`}
      <div class="wc-canvas" ref=${canvas} style=${{ left: frame * scale + 'px', top: frame * scale + 'px', width: design.width * scale + 'px', height: design.height * scale + 'px' }}>
        ${design.layers.map((layer) => !layer.hidden && html`<div key=${layer.id} class=${'wc-box' + (layer.id === selected ? ' on' : '')} title=${layerLabel(layer)} data-layer=${layer.id}
          tabindex="0" role="button" aria-label=${spokenLabel(layer)} aria-pressed=${layer.id === selected} onFocus=${() => onSelect(layer.id)}
          style=${{ left: layer.x * scale + 'px', top: layer.y * scale + 'px', width: layer.w * scale + 'px', height: layer.h * scale + 'px', transform: `rotate(${layer.rotation}deg)` }}
          onPointerDown=${(event) => grab(event, layer, 'move')}>
          ${layer.id === selected && html`
            <div class="wc-turn-stem"></div>
            <div class="wc-handle wc-turn" title="Drag to turn. Hold Shift for steps of 15 degrees." onPointerDown=${(event) => grab(event, layer, 'turn')}></div>
            ${handlesOf(layer).map(([pullX, pullY, cursor]) => html`<div key=${pullX + ',' + pullY} class="wc-handle" style=${{ left: (pullX + 1) * 50 + '%', top: (pullY + 1) * 50 + '%', cursor }}
              onPointerDown=${(event) => grab(event, layer, 'size', pullX, pullY)}></div>`)}`}
        </div>`)}
      </div>
    </div>
    </div></div>
    <p class="muted small wc-keys">Click a layer to select it, or pick it in the list when another layer covers it. Drag it to move it, a corner or an edge to size it, and the round handle to turn it.
      On the keyboard, Tab reaches each layer on the picture: arrow keys move it, Alt with an arrow sizes it, Shift takes steps of 10, [ and ] change its order, Delete removes it, and Ctrl+Z and Ctrl+Y undo and redo.</p>
  </div>`;
}

// ---- the panel for one layer ----

// A number box that passes a value on only once it is inside its range, so typing 250 into a box that starts at 100 doesn't pass through 2 and 25.
function RangeInput({ label, value, low, high, step, help, onChange }) {
  const [draft, setDraft] = useState(null);
  const inside = (text) => text !== '' && Number.isFinite(Number(text)) && Number(text) >= low && Number(text) <= high;
  const bad = draft !== null && !inside(draft);
  return html`<div class="field">
    <label>${label}</label>
    <input type="number" class=${bad ? 'wc-bad' : ''} aria-label=${label} aria-invalid=${bad} min=${low} max=${high} step=${step} value=${draft === null ? value : draft}
      onInput=${(e) => { const text = e.currentTarget.value; setDraft(text); if (inside(text)) onChange(Number(text)); }} onBlur=${() => setDraft(null)} />
    ${bad && html`<div class="error">From ${low} to ${high}.</div>`}
    ${help && html`<div class="help">${help}</div>`}
  </div>`;
}

function Numbers({ fields }) {
  return html`<div class="wc-numbers">${fields.map(([label, value, onChange, low, high, step]) => html`
    <${RangeInput} key=${label} label=${label} value=${value} low=${low} high=${high} step=${step} onChange=${onChange} />`)}</div>`;
}

function Corners({ layer, patch }) {
  const radius = layer.radius || [0, 0, 0, 0];
  const set = (index, value) => patch({ radius: radius.map((old, at) => (at === index ? value : old)) }, 'radius');
  return html`<div class="field"><label>Corner radius</label>
    <${Numbers} fields=${[['Top left', radius[0], (v) => set(0, v), 0, 1000], ['Top right', radius[1], (v) => set(1, v), 0, 1000], ['Bottom right', radius[2], (v) => set(2, v), 0, 1000], ['Bottom left', radius[3], (v) => set(3, v), 0, 1000]]} />
    <button type="button" class="btn small" onClick=${() => patch({ radius: [radius[0], radius[0], radius[0], radius[0]] })}>Make all four match the first</button>
  </div>`;
}

function LayerPanel({ layer, patch, catalog, uploads }) {
  const family = catalog.fonts.find((font) => font.key === layer.font);
  const fillKinds = layer.type === 'text' ? ['color', 'avatar', 'gradient', 'stripes', 'stock', 'upload'] : ['color', 'avatar', 'gradient', 'stripes', 'stock', 'upload', 'none'];
  return html`<div class="wc-panel">
    <${TextInput} label="Name" value=${layer.name} maxLength=${60} placeholder=${TYPE_NAMES[layer.type]} onChange=${(name) => patch({ name }, 'name')} />
    ${layer.type === 'text' && html`
      <${TemplateArea} label="Text" value=${layer.text} maxLength=${catalog.limits.text} help=${TEXT_HELP} onChange=${(text) => patch({ text }, 'text')} />
      <${FontPicker} value=${layer.font} catalog=${catalog} onChange=${(font) => { const next = catalog.fonts.find((row) => row.key === font); patch({ font, weight: next ? next.display : layer.weight }); }} />
      <div class="wc-numbers">
        <${Select} label="Weight" value=${layer.weight} options=${(family ? family.weights : [layer.weight]).map((weight) => ({ value: weight, label: String(weight) }))} onChange=${(weight) => patch({ weight })} />
        <${RangeInput} label="Size" value=${layer.size} low=${catalog.limits.font_size[0]} high=${catalog.limits.font_size[1]} onChange=${(size) => patch({ size }, 'size')} />
        <${RangeInput} label="Line height" value=${layer.line_height} low=${0.6} high=${3} step=${0.1} onChange=${(line_height) => patch({ line_height }, 'line')} />
        <${RangeInput} label="Letter spacing" value=${layer.letter_spacing} low=${-20} high=${100} onChange=${(letter_spacing) => patch({ letter_spacing }, 'spacing')} />
      </div>
      <div class="wc-numbers">
        <${Select} label="Across" value=${layer.align} options=${[{ value: 'left', label: 'Left' }, { value: 'center', label: 'Center' }, { value: 'right', label: 'Right' }]} onChange=${(align) => patch({ align })} />
        <${Select} label="Up and down" value=${layer.valign} options=${[{ value: 'top', label: 'Top' }, { value: 'middle', label: 'Middle' }, { value: 'bottom', label: 'Bottom' }]} onChange=${(valign) => patch({ valign })} />
      </div>
      <${Check} label="Wrap long lines to fit the box" value=${layer.wrap} onChange=${(wrap) => patch({ wrap })} />
      <${Check} label="Shrink the text until it fits the box" value=${layer.shrink} onChange=${(shrink) => patch({ shrink })} />`}
    ${layer.type === 'picture' && html`
      <${Select} label="Picture" value=${layer.source.kind} options=${[{ value: 'address', label: 'From Discord, by address' }, { value: 'upload', label: 'An uploaded picture' }, { value: 'stock', label: 'A stock background' }]}
        onChange=${(kind) => patch({ source: kind === 'address' ? { kind, address: '{server(icon)}' } : kind === 'stock' ? { kind, name: catalog.stock[0].key } : uploads.uploads.length ? { kind, id: uploads.uploads[0].id } : layer.source })} />
      ${layer.source.kind === 'address' && html`<div>
        <${TextInput} label="Address" value=${layer.source.address} maxLength=${catalog.limits.address} onChange=${(address) => patch({ source: { kind: 'address', address } }, 'address')}
          help="A picture on Discord's own image servers. A block can give the address." />
        <div class="row wc-insert">
          ${[['Member avatar', '{user(avatar)}'], ['Server icon', '{server(icon)}']].map(([name, block]) => html`
            <button type="button" key=${name} class="btn small" onClick=${() => patch({ source: { kind: 'address', address: block } })}>${name}</button>`)}
        </div></div>`}
      ${layer.source.kind === 'upload' && html`<${UploadGrid} uploads=${uploads} picked=${layer.source.id} onPick=${(id) => patch({ source: { kind: 'upload', id } })} />`}
      ${layer.source.kind === 'stock' && html`<${StockGrid} catalog=${catalog} picked=${layer.source.name} onPick=${(name) => patch({ source: { kind: 'stock', name } })} />`}
      <${Select} label="Fit" value=${layer.fit} options=${[{ value: 'cover', label: 'Cover the box, cropped from the center' }, { value: 'contain', label: 'Show the whole picture' }, { value: 'stretch', label: 'Stretch to the box' }]}
        onChange=${(fit) => patch({ fit })} />
      <${Corners} layer=${layer} patch=${patch} />`}
    ${layer.type === 'avatar' && html`
      <${Select} label="Shape" value=${layer.shape} options=${[{ value: 'circle', label: 'Circle' }, { value: 'rounded', label: 'Rounded' }, { value: 'square', label: 'Square' }, { value: 'hexagon', label: 'Hexagon' }]}
        onChange=${(shape) => patch({ shape })} />
      ${layer.shape === 'rounded' && html`<${Corners} layer=${layer} patch=${patch} />`}`}
    ${layer.type === 'rect' && html`<${Corners} layer=${layer} patch=${patch} />`}
    <hr class="rule" />
    <${Numbers} fields=${[
      ['Left', layer.x, (x) => patch({ x }, 'x'), -catalog.limits.offset, catalog.limits.offset], ['Top', layer.y, (y) => patch({ y }, 'y'), -catalog.limits.offset, catalog.limits.offset],
      ['Width', layer.w, (w) => patch({ w }, 'w'), 1, catalog.limits.side[1]], ['Height', layer.h, (h) => patch({ h }, 'h'), 1, catalog.limits.side[1]],
      ['Turn, in degrees', layer.rotation, (rotation) => patch({ rotation }, 'rotation'), -360, 360], ['Opacity, 0 to 100', layer.opacity, (opacity) => patch({ opacity: Math.round(opacity) }, 'opacity'), 0, 100],
    ]} />
    <div class="row">
      <${Check} label="Flip left to right" value=${layer.flip_x} onChange=${(flip_x) => patch({ flip_x })} />
      <${Check} label="Flip top to bottom" value=${layer.flip_y} onChange=${(flip_y) => patch({ flip_y })} />
    </div>
    <hr class="rule" />
    <${FillPicker} label=${layer.type === 'text' ? 'Text fill' : layer.type === 'picture' || layer.type === 'avatar' ? 'Behind the picture' : 'Fill'} value=${layer.fill} kinds=${fillKinds} catalog=${catalog} uploads=${uploads}
      onChange=${(fill) => patch({ fill }, 'fill')} />
    <${ColorField} label=${layer.type === 'text' ? 'Outline color' : layer.type === 'avatar' ? 'Ring color' : 'Stroke color'} value=${layer.stroke.color} onChange=${(color) => patch({ stroke: { ...layer.stroke, color } }, 'stroke')} />
    <${RangeInput} label=${layer.type === 'text' ? 'Outline width' : layer.type === 'avatar' ? 'Ring width' : 'Stroke width'} value=${layer.stroke.width} low=${0} high=${catalog.limits.stroke}
      onChange=${(width) => patch({ stroke: { ...layer.stroke, width: Math.round(width) } }, 'stroke-width')} />
  </div>`;
}

// ---- the designer ----

export function Designer({ form, catalog, uploads, preview }) {
  const design = form.data.design;
  const [selected, setSelected] = useState(null);
  const [depth, setDepth] = useState({ past: 0, future: 0 });
  const [startFrom, setStartFrom] = useState(form.data.layout);
  const history = useRef({ past: [], future: [], tag: null, at: 0 });
  const layer = design.layers.find((row) => row.id === selected) || null;

  // Keeps the design as it stands so Undo can put it back. A run of small changes to one thing, such as typing or nudging, is one step.
  const remember = (tag) => {
    const kept = history.current;
    const now = Date.now();
    if (tag && kept.tag === tag && now - kept.at < 900) { kept.at = now; return; }
    kept.tag = tag || null;
    kept.at = now;
    kept.past.push(JSON.stringify(design));
    if (kept.past.length > HISTORY) kept.past.shift();
    kept.future = [];
    setDepth({ past: kept.past.length, future: 0 });
  };
  const apply = (next, tag) => { remember(tag); form.set('design', next); };
  const step = (from, to) => {
    const kept = history.current;
    if (!kept[from].length) return;
    kept[to].push(JSON.stringify(design));
    const back = JSON.parse(kept[from].pop());
    kept.tag = null;
    setDepth({ past: kept.past.length, future: kept.future.length });
    form.set('design', back);
    if (!back.layers.some((row) => row.id === selected)) setSelected(null);
  };
  const undo = () => step('past', 'future');
  const redo = () => step('future', 'past');

  const withLayer = (id, changes) => ({ ...design, layers: design.layers.map((row) => (row.id === id ? { ...row, ...changes } : row)) });
  const patch = (id, changes, tag) => apply(withLayer(id, changes), tag ? `${id}:${tag}` : null);
  const add = (type) => {
    if (design.layers.length >= catalog.limits.layers) return;
    const made = newLayer(type, design, catalog.stock[0].key);
    apply({ ...design, layers: [...design.layers, made] });
    setSelected(made.id);
  };
  const remove = (id) => { apply({ ...design, layers: design.layers.filter((row) => row.id !== id) }); if (selected === id) setSelected(null); };
  const duplicate = (id) => {
    const source = design.layers.find((row) => row.id === id);
    if (!source || design.layers.length >= catalog.limits.layers) return;
    const copy = { ...JSON.parse(JSON.stringify(source)), id: freshId(design), x: source.x + 16, y: source.y + 16 };
    const at = design.layers.indexOf(source);
    apply({ ...design, layers: [...design.layers.slice(0, at + 1), copy, ...design.layers.slice(at + 1)] });
    setSelected(copy.id);
  };
  const order = (id, by) => {
    const at = design.layers.findIndex((row) => row.id === id);
    const to = at + by;
    if (at < 0 || to < 0 || to >= design.layers.length) return;
    const layers = [...design.layers];
    [layers[at], layers[to]] = [layers[to], layers[at]];
    apply({ ...design, layers });
  };

  // The keys of the picture. The answer is true when the key changed which layers there are, their order or the selection.
  const onKey = (event) => {
    const meta = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();
    if (meta && key === 'z') { event.preventDefault(); if (event.shiftKey) redo(); else undo(); return true; }
    if (meta && key === 'y') { event.preventDefault(); redo(); return true; }
    const size = event.shiftKey ? 10 : 1;
    const arrows = { arrowleft: [-size, 0], arrowright: [size, 0], arrowup: [0, -size], arrowdown: [0, size] };
    // Alt with an arrow is the browser's Back and Forward, so the picture keeps it for itself even with nothing selected.
    if (arrows[key] && (layer || event.altKey)) event.preventDefault();
    if (!layer) return false;
    if (arrows[key]) {
      const [dx, dy] = arrows[key];
      if (event.altKey) patch(layer.id, { w: clamp(layer.w + dx, SMALLEST, 2000), h: clamp(layer.h + dy, SMALLEST, 2000) }, 'keys-size');
      else patch(layer.id, { x: layer.x + dx, y: layer.y + dy }, 'keys-move');
      return false;
    }
    if (key === 'delete' || key === 'backspace') {
      event.preventDefault();
      remove(layer.id);
    } else if (meta && key === 'd') {
      event.preventDefault();
      duplicate(layer.id);
    } else if (key === ']' && !meta) {
      order(layer.id, 1);
    } else if (key === '[' && !meta) {
      order(layer.id, -1);
    } else if (key === 'escape') {
      setSelected(null);
    } else {
      return false;
    }
    return true;
  };

  const fromLayout = async () => {
    if (design.layers.length && !(await confirmDialog({ title: 'Start again from this layout?', body: 'The layers on the canvas are replaced by the layout. Undo brings them back.', confirm: 'Replace' }))) return;
    try {
      const answer = await api.post('/welcomecard/design', { draft: { ...draftOfLayout(form.data), layout: startFrom } });
      apply(answer.design);
      setSelected(null);
    } catch (error) {
      toastError(error);
    }
  };

  const full = design.layers.length >= catalog.limits.layers;
  const canvasPanel = html`<div class="wc-panel">
    <${Numbers} fields=${[
      ['Width', design.width, (width) => apply({ ...design, width: Math.round(width) }, 'canvas-w'), catalog.limits.side[0], catalog.limits.side[1]],
      ['Height', design.height, (height) => apply({ ...design, height: Math.round(height) }, 'canvas-h'), catalog.limits.side[0], catalog.limits.side[1]],
    ]} />
    <p class="muted small">From ${catalog.limits.side[0]} to ${catalog.limits.side[1]} pixels on a side.</p>
    <${FillPicker} label="Background" value=${design.background} kinds=${['stock', 'color', 'avatar', 'stripes', 'gradient', 'upload', 'none']} catalog=${catalog} uploads=${uploads}
      onChange=${(background) => apply({ ...design, background }, 'canvas-bg')} />
    <${ColorField} label="Frame color" value=${design.frame.color} onChange=${(color) => apply({ ...design, frame: { ...design.frame, color } }, 'frame-color')} />
    <${RangeInput} label="Frame width" value=${design.frame.width} low=${0} high=${catalog.limits.stroke} help="The frame goes around the canvas and makes the picture that much larger on every side. 0 leaves it off."
      onChange=${(width) => apply({ ...design, frame: { ...design.frame, width: Math.round(width) } }, 'frame-width')} />
    <hr class="rule" />
    <div class="field"><label>Start again from a layout</label>
      <div class="row">
        <select aria-label="Layout to start from" class="wc-start" value=${startFrom} onChange=${(e) => setStartFrom(e.currentTarget.value)}>
          ${catalog.layouts.map((row) => html`<option key=${row.key} value=${row.key}>${row.name}, ${row.width} x ${row.height}</option>`)}</select>
        <button type="button" class="btn" onClick=${fromLayout}>Use this layout</button>
      </div>
      <div class="help">Its layers land on the canvas with the ready-made settings as they stand, ready to change one by one.</div></div>
  </div>`;
  return html`<div class="wc-designer">
    <div class="wc-designer-main">
      <${Card} icon="image" title="Picture" right=${html`
          <button type="button" class="btn small" disabled=${!depth.past} onClick=${undo} title="Undo, Ctrl+Z"><${Icon} name="undo-2" size=${14} /> Undo</button>
          <button type="button" class="btn small" disabled=${!depth.future} onClick=${redo} title="Redo, Ctrl+Y"><${Icon} name="undo-2" size=${14} class="wc-redo" /> Redo</button>`}>
        <${Stage} design=${design} selected=${selected} preview=${preview} onSelect=${setSelected} onBegin=${() => remember(null)}
          onPatch=${(id, changes) => form.set('design', withLayer(id, changes))} onKey=${onKey} />
        <${PreviewNotes} preview=${preview} />
        ${form.errors.design && html`<${Notice} kind="bad">${form.errors.design}<//>`}
      <//>
    </div>
    <div class="wc-designer-side">
      <${Card} icon="layers-3" title=${`Layers (${design.layers.length} of ${catalog.limits.layers})`}>
        <div class="row wc-add">
          ${Object.keys(TYPE_NAMES).map((type) => html`<button type="button" key=${type} class="btn small" disabled=${full} onClick=${() => add(type)}><${Icon} name="plus" size=${14} /> ${TYPE_NAMES[type]}</button>`)}
        </div>
        ${design.layers.length === 0 && html`<div class="empty">No layers yet. Add one above, or start from a layout below.</div>`}
        <ul class="wc-layers">
          ${[...design.layers].reverse().map((row) => {
            const at = design.layers.indexOf(row);
            return html`<li key=${row.id} class=${'wc-layer' + (row.id === selected ? ' on' : '') + (row.hidden ? ' off' : '')}>
              <button type="button" class="wc-layer-pick" aria-pressed=${row.id === selected} onClick=${() => setSelected(row.id)}><${Icon} name=${TYPE_ICONS[row.type]} size=${14} /> <span>${layerLabel(row)}</span></button>
              <button type="button" class="icon-btn" aria-label=${(row.hidden ? 'Show ' : 'Hide ') + layerLabel(row)} title=${row.hidden ? 'Show' : 'Hide'} onClick=${() => patch(row.id, { hidden: !row.hidden })}><${Icon} name=${row.hidden ? 'eye-off' : 'eye'} size=${14} /></button>
              <button type="button" class="icon-btn" aria-label=${'Bring ' + layerLabel(row) + ' forward'} title="Bring forward" disabled=${at === design.layers.length - 1} onClick=${() => order(row.id, 1)}><${Icon} name="arrow-up" size=${14} /></button>
              <button type="button" class="icon-btn" aria-label=${'Send ' + layerLabel(row) + ' backward'} title="Send backward" disabled=${at === 0} onClick=${() => order(row.id, -1)}><${Icon} name="arrow-down" size=${14} /></button>
              <button type="button" class="icon-btn" aria-label=${'Duplicate ' + layerLabel(row)} title="Duplicate" disabled=${full} onClick=${() => duplicate(row.id)}><${Icon} name="copy" size=${14} /></button>
              <button type="button" class="icon-btn danger" aria-label=${'Delete ' + layerLabel(row)} title="Delete" onClick=${() => remove(row.id)}><${Icon} name="trash-2" size=${14} /></button>
            </li>`;
          })}
          <li key="canvas" class=${'wc-layer wc-canvas-row' + (layer ? '' : ' on')}>
            <button type="button" class="wc-layer-pick" aria-pressed=${!layer} onClick=${() => setSelected(null)}><${Icon} name="image" size=${14} /> <span>Canvas: size, background and frame</span></button>
          </li>
        </ul>
      <//>
      ${layer ? html`<${Card} icon=${TYPE_ICONS[layer.type]} title=${TYPE_NAMES[layer.type] + ' layer'}>
        <${LayerPanel} key=${layer.id} layer=${layer} catalog=${catalog} uploads=${uploads} patch=${(changes, tag) => patch(layer.id, changes, tag)} />
      <//>` : html`<${Card} icon="settings" title="Canvas">${canvasPanel}<//>`}
    </div>
  </div>`;
}

// The ready-made settings as they stand on the page, which is what a layout is built from.
export const LAYOUT_FIELDS = ['layout', 'background', 'text', 'text_position', 'text_color', 'outline_color', 'outline_width', 'font', 'avatar', 'avatar_shape', 'ring_color', 'ring_width', 'frame', 'frame_color', 'frame_width'];
export function draftOfLayout(data) {
  const draft = {};
  for (const field of LAYOUT_FIELDS) draft[field] = data[field];
  return draft;
}
