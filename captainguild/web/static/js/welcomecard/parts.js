// What the Welcome image page's two modes share: colors, fills, fonts, uploads, the text box with its variables, and the preview.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { EmojiPicker, HexInput, Icon, Notice, confirmDialog, toast, toastError, useGuild } from '../ui.js';

const MAX_UPLOAD = 8 * 1024 * 1024;
const UPLOAD_TYPES = ['image/jpeg', 'image/png', 'image/gif'];

// A color is '#rrggbb', or '#rrggbbaa' when it is see-through.
export const hexOf = (color) => (typeof color === 'string' && color.length >= 7 ? color.slice(0, 7) : '#000000');
export const solidOf = (color) => (typeof color === 'string' && color.length === 9 ? Math.round((parseInt(color.slice(7), 16) / 255) * 100) : 100);
export function withSolid(hex, percent) {
  const share = Math.max(0, Math.min(100, Number(percent)));
  if (!Number.isFinite(share) || share >= 100) return hex;
  return hex + Math.round((share * 255) / 100).toString(16).padStart(2, '0');
}

export function ColorField({ label, value, onChange, solid = true, error }) {
  const hex = hexOf(value);
  return html`<div class="field">
    ${label && html`<label>${label}</label>`}
    <div class="wc-color">
      <input class="color-input" type="color" aria-label=${label || 'Color'} value=${hex} onInput=${(e) => onChange(withSolid(e.currentTarget.value, solidOf(value)))} />
      <${HexInput} label=${(label || 'Color') + ' as hex'} value=${hex} onChange=${(text) => text && onChange(withSolid(text, solidOf(value)))} />
      ${solid && html`<div class="input-group wc-solid" title="How solid the color is. 0 is clear.">
        <input type="number" min="0" max="100" aria-label=${(label || 'Color') + ', how solid'} value=${solidOf(value)}
          onInput=${(e) => { if (e.currentTarget.value !== '') onChange(withSolid(hex, e.currentTarget.value)); }} />
        <span class="addon">%</span></div>`}
    </div>
    ${error && html`<div class="error">${error}</div>`}
  </div>`;
}

// A list of colors for stripes or a gradient.
export function ColorList({ label, colors, onChange, least, most }) {
  const list = colors && colors.length ? colors : ['#ffffff'];
  const change = (index, color) => onChange(list.map((old, at) => (at === index ? color : old)));
  return html`<div class="field">
    <label>${label}</label>
    <div class="wc-color-list">
      ${list.map((color, index) => html`<div class="wc-color-row" key=${index}>
        <${ColorField} value=${color} onChange=${(next) => change(index, next)} />
        <button type="button" class="icon-btn danger" aria-label=${'Remove color ' + (index + 1)} title="Remove this color" disabled=${list.length <= least}
          onClick=${() => onChange(list.filter((_, at) => at !== index))}><${Icon} name="x" /></button>
      </div>`)}
    </div>
    <button type="button" class="btn small" disabled=${list.length >= most} onClick=${() => onChange([...list, list[list.length - 1]])}><${Icon} name="plus" size=${14} /> Add a color</button>
  </div>`;
}

// ---- uploads ----

function checkFile(file) {
  if (!UPLOAD_TYPES.includes(file.type)) return 'Use a JPG, PNG or GIF.';
  if (file.size > MAX_UPLOAD) return 'That file is over 8 MB.';
  return null;
}

// This server's uploaded pictures, with adding and removing. One copy serves every picker on the page.
export function useUploads() {
  const guild = useGuild();
  const [state, setState] = useState({ uploads: [], used: 0, most: 0, room: 0, loaded: false });
  const [busy, setBusy] = useState(false);
  const load = () => api.get('/welcomecard/uploads').then((data) => setState({ ...data, loaded: true })).catch(toastError);
  useEffect(() => { load(); }, [guild.id]);
  const add = async (file) => {
    const refusal = checkFile(file);
    if (refusal) { toast(refusal, 'bad'); return null; }
    const body = new FormData();
    body.append('image', file);
    body.append('name', file.name.replace(/\.[A-Za-z0-9]+$/, ''));
    setBusy(true);
    try {
      const row = await api.upload('/welcomecard/uploads', body);
      await load();
      toast('Picture uploaded.');
      return row;
    } catch (error) {
      toastError(error);
      return null;
    } finally {
      setBusy(false);
    }
  };
  const remove = async (row) => {
    const sure = await confirmDialog({ title: 'Remove this picture?', body: `"${row.name}" is deleted from the bot.`, confirm: 'Remove', danger: true });
    if (!sure) return false;
    try {
      await api.del(`/welcomecard/uploads/${row.id}`);
      await load();
      toast('Picture removed.');
      return true;
    } catch (error) {
      toastError(error);
      return false;
    }
  };
  return { ...state, busy, add, remove, reload: load };
}

export function UploadGrid({ uploads, picked, onPick }) {
  const input = useRef(null);
  const choose = async (event) => {
    const file = event.currentTarget.files[0];
    event.currentTarget.value = '';
    if (!file) return;
    const row = await uploads.add(file);
    if (row) onPick(row.id);
  };
  const megabytes = (bytes) => (bytes / 1048576).toFixed(1);
  return html`<div>
    <input ref=${input} class="hidden" type="file" accept=${UPLOAD_TYPES.join(',')} aria-label="Upload a picture" onChange=${choose} />
    <div class="row" style=${{ marginBottom: '10px' }}>
      <button type="button" class="btn" disabled=${uploads.busy} onClick=${() => input.current.click()}><${Icon} name="upload" /> ${uploads.busy ? 'Uploading...' : 'Upload a picture'}</button>
      ${uploads.loaded && html`<span class="muted small">${uploads.uploads.length} of ${uploads.most} pictures, ${megabytes(uploads.used)} of ${megabytes(uploads.room)} MB</span>`}
    </div>
    <p class="muted small">JPG, PNG or GIF, up to 8 MB. As a background or a fill, a picture is scaled to cover its place and cropped from the center. An animated GIF keeps moving when it is the background.</p>
    ${uploads.loaded && uploads.uploads.length === 0 && html`<div class="empty">No pictures uploaded yet.</div>`}
    <div class="wc-thumbs">
      ${uploads.uploads.map((row) => html`<div class=${'wc-thumb' + (picked === row.id ? ' on' : '')} key=${row.id}>
        <button type="button" class="wc-thumb-pick" aria-pressed=${picked === row.id} title=${`${row.name}, ${row.width}x${row.height}`} onClick=${() => onPick(row.id)}>
          <img src=${row.preview} alt=${row.name} loading="lazy" />
          <span class="wc-thumb-name">${row.animated ? html`<span class="badge blue">GIF</span> ` : ''}${row.name}</span>
        </button>
        <button type="button" class="icon-btn danger wc-thumb-remove" aria-label=${'Remove ' + row.name} title="Remove this picture" onClick=${() => uploads.remove(row)}><${Icon} name="trash-2" size=${14} /></button>
      </div>`)}
    </div>
  </div>`;
}

// ---- fills ----

const FILL_TABS = [
  ['stock', 'Stock'], ['color', 'Color'], ['avatar', 'Avatar color'], ['stripes', 'Stripes'], ['gradient', 'Gradient'], ['upload', 'Uploads'], ['none', 'None'],
];

export function StockGrid({ catalog, picked, onPick }) {
  const guild = useGuild();
  return html`<div>${catalog.categories.map((category) => html`<div key=${category}>
    <div class="wc-group">${category}</div>
    <div class="wc-thumbs">
      ${catalog.stock.filter((row) => row.category === category).map((row) => html`
        <button type="button" key=${row.key} class=${'wc-thumb wc-thumb-pick' + (picked === row.key ? ' on' : '')} aria-pressed=${picked === row.key} title=${row.name} onClick=${() => onPick(row.key)}>
          <img src=${`/api/g/${guild.id}/welcomecard/stock/${row.key}`} alt=${row.name} loading="lazy" width="240" height="90" />
          <span class="wc-thumb-name">${row.name}</span>
        </button>`)}
    </div>
  </div>`)}</div>`;
}

// Picks what fills a background or a layer. `kinds` says which choices this place offers.
export function FillPicker({ label, value, onChange, catalog, uploads, kinds = ['stock', 'color', 'avatar', 'stripes', 'gradient', 'upload'], error }) {
  const fill = value || { kind: 'none' };
  const [tab, setTab] = useState(kinds.includes(fill.kind) ? fill.kind : kinds[0]);
  useEffect(() => { if (kinds.includes(fill.kind)) setTab(fill.kind); }, [fill.kind]);
  const starts = {
    stock: () => ({ kind: 'stock', name: catalog.stock[0].key }),
    color: () => ({ kind: 'color', color: '#2b2d31' }),
    avatar: () => ({ kind: 'avatar' }),
    stripes: () => ({ kind: 'stripes', direction: 'horizontal', colors: catalog.stripes[0].colors }),
    gradient: () => ({ kind: 'gradient', angle: 90, colors: ['#3a1c71', '#d76d77', '#ffaf7b'] }),
    upload: () => (uploads.uploads.length ? { kind: 'upload', id: uploads.uploads[0].id } : null),
    none: () => ({ kind: 'none' }),
  };
  const open = (kind) => {
    setTab(kind);
    if (fill.kind === kind) return;
    const start = starts[kind]();
    if (start) onChange(start);
  };
  return html`<div class="field wc-fill">
    ${label && html`<label>${label}</label>`}
    <div class="tabs wc-fill-tabs" role="tablist">
      ${FILL_TABS.filter(([kind]) => kinds.includes(kind)).map(([kind, name]) => html`
        <button type="button" key=${kind} role="tab" aria-selected=${tab === kind} class=${'tab' + (tab === kind ? ' on' : '')} onClick=${() => open(kind)}>${name}</button>`)}
    </div>
    ${tab === 'stock' && html`<${StockGrid} catalog=${catalog} picked=${fill.kind === 'stock' ? fill.name : null} onPick=${(name) => onChange({ kind: 'stock', name })} />`}
    ${tab === 'color' && fill.kind === 'color' && html`<${ColorField} label="Color" value=${fill.color} onChange=${(color) => onChange({ kind: 'color', color })} />`}
    ${tab === 'avatar' && html`<p class="muted">The picture takes the main color of each member's avatar.</p>`}
    ${tab === 'stripes' && fill.kind === 'stripes' && html`<div>
      <div class="row" style=${{ marginBottom: '10px' }}>
        ${[['horizontal', 'Stacked, like a flag'], ['vertical', 'Side by side']].map(([direction, name]) => html`
          <button type="button" key=${direction} class=${'btn small' + (fill.direction === direction ? ' primary' : '')} aria-pressed=${fill.direction === direction}
            onClick=${() => onChange({ ...fill, direction })}>${name}</button>`)}
      </div>
      <div class="wc-group">Ready-made sets</div>
      <div class="row" style=${{ marginBottom: '10px' }}>
        ${catalog.stripes.map((set) => html`<button type="button" key=${set.name} class="wc-set" title=${set.name} aria-label=${'Use the ' + set.name + ' colors'} onClick=${() => onChange({ ...fill, colors: set.colors })}>
          ${set.colors.map((color, index) => html`<span key=${index} style=${{ background: color }}></span>`)}</button>`)}
      </div>
      <${ColorList} label="Colors" colors=${fill.colors} least=${1} most=${catalog.limits.colors} onChange=${(colors) => onChange({ ...fill, colors })} />
    </div>`}
    ${tab === 'gradient' && fill.kind === 'gradient' && html`<div>
      <div class="field"><label>Angle</label>
        <div class="input-group wc-narrow"><input type="number" min="0" max="360" aria-label="Gradient angle" value=${fill.angle}
          onInput=${(e) => { const angle = Number(e.currentTarget.value); if (e.currentTarget.value !== '' && angle >= 0 && angle <= 360) onChange({ ...fill, angle }); }} /><span class="addon">degrees</span></div>
        <div class="help">0 runs left to right and 90 runs top to bottom.</div></div>
      <${ColorList} label="Colors" colors=${fill.colors} least=${2} most=${catalog.limits.colors} onChange=${(colors) => onChange({ ...fill, colors })} />
    </div>`}
    ${tab === 'upload' && html`<${UploadGrid} uploads=${uploads} picked=${fill.kind === 'upload' ? fill.id : null} onPick=${(id) => onChange({ kind: 'upload', id })} />`}
    ${tab === 'none' && html`<p class="muted">Nothing is drawn here, so whatever is behind it shows through.</p>`}
    ${error && html`<div class="error">${error}</div>`}
  </div>`;
}

// ---- fonts ----

export function FontPicker({ label = 'Font', value, onChange, catalog }) {
  const guild = useGuild();
  return html`<div class="field">
    <label>${label}</label>
    <div class="wc-fonts" role="listbox" aria-label=${label}>
      ${catalog.fonts.map((font) => html`<button type="button" key=${font.key} role="option" aria-selected=${value === font.key} class=${'wc-font' + (value === font.key ? ' on' : '')}
        title=${`${font.name}: ${font.style}`} onClick=${() => onChange(font.key)}>
        <img src=${`/api/g/${guild.id}/welcomecard/fonts/${font.key}`} alt=${font.name} loading="lazy" width="300" height="56" />
        <span class="wc-font-name">${font.name}<span class="dim"> ${font.style}</span></span>
      </button>`)}
    </div>
  </div>`;
}

// ---- text with variables ----

export const VARIABLES = [
  ['Name', '{user}'],
  ['Username', '{user(name)}'],
  ['Server', '{server}'],
  ['Member count', '{server(members)}'],
  ['Count as 1,234th', '{ord:{server(members)}}'],
  ['Count as 1234th', '{ord(plain):{server(members)}}'],
  ['Joined so far', '{joined}'],
  ['Invite code', '{invite}'],
  ['Invited by', '{invite(inviter)}'],
  ['Account made', '{strf({user(created_at)}):%B %Y}'],
  ['Account age', '{since:{user(created_at)}}'],
];
const MARKS = [['bold', 'Bold', '**'], ['italic', 'Italic', '*']];

// The text of a picture: a box, buttons that drop a variable or a mark in at the cursor, and the emoji picker.
export function TemplateArea({ label, value, onChange, maxLength, rows = 4, error, help }) {
  const box = useRef(null);
  const [picking, setPicking] = useState(false);
  const text = value || '';
  const put = (before, after = '') => {
    const area = box.current;
    const start = area ? area.selectionStart : text.length;
    const end = area ? area.selectionEnd : text.length;
    const next = text.slice(0, start) + before + text.slice(start, end) + after + text.slice(end);
    onChange(next);
    const cursor = start + before.length + (end - start) + (end > start ? after.length : 0);
    setTimeout(() => { if (box.current) { box.current.focus(); box.current.setSelectionRange(cursor, cursor); } }, 0);
  };
  return html`<div class="field wc-template">
    ${label && html`<label>${label}</label>`}
    <textarea ref=${box} rows=${rows} value=${text} aria-label=${label || 'Text'} onInput=${(e) => onChange(e.currentTarget.value)}></textarea>
    ${maxLength && html`<div class=${'counter' + (text.length > maxLength ? ' over' : '')}>${text.length}/${maxLength}</div>`}
    <div class="row wc-insert">
      ${VARIABLES.map(([name, block]) => html`<button type="button" key=${name} class="btn small" title=${'Insert ' + block} onClick=${() => put(block)}>${name}</button>`)}
    </div>
    <div class="row wc-insert" style=${{ position: 'relative' }}>
      ${MARKS.map(([icon, name, mark]) => html`<button type="button" key=${name} class="btn small" aria-label=${name} title=${`${name}: ${mark}text${mark}`} onClick=${() => put(mark, mark)}><${Icon} name=${icon} size=${14} /></button>`)}
      <button type="button" class="btn small" aria-label="Underline" title="Underline: __text__" onClick=${() => put('__', '__')}><u>U</u></button>
      <button type="button" class="btn small" aria-expanded=${picking} onClick=${() => setPicking(!picking)}><${Icon} name="smile-plus" size=${14} /> Emoji</button>
      ${picking && html`<${EmojiPicker} onPick=${(emoji) => { put(emoji); setPicking(false); }} onClose=${() => setPicking(false)} />`}
    </div>
    ${help && html`<div class="help">${help}</div>`}
    ${error && html`<div class="error">${error}</div>`}
  </div>`;
}

export const TEXT_HELP = html`Each line break starts a new line. <code>**bold**</code>, <code>*italic*</code> and <code>__underline__</code> work, and so does every block of the
  <a href="/docs#templates" target="_blank" rel="noopener">template language</a>. A member's name is always drawn as written.`;

// ---- the preview ----

// Draws `draft` on the server shortly after it changes: the settings as they stand on the page, saved or not. One request runs at a time, and a
// change made meanwhile is drawn when it comes back.
export function usePreview(draft) {
  const guild = useGuild();
  const [shown, setShown] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const wanted = useRef(null);
  const running = useRef(false);
  const live = useRef(true);
  const body = JSON.stringify(draft);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const run = async () => {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    while (live.current && wanted.current !== null) {
      const sending = wanted.current;
      wanted.current = null;
      try {
        const answer = await api.post('/welcomecard/preview', { draft: JSON.parse(sending) });
        if (!live.current) break;
        setShown(answer);
        setError(null);
      } catch (failure) {
        if (!live.current) break;
        if (failure.status === 429) {
          wanted.current = wanted.current === null ? sending : wanted.current;
          await new Promise((resolve) => setTimeout(resolve, 3000));
        } else {
          setError(failure);
        }
      }
    }
    running.current = false;
    if (live.current) setBusy(false);
  };
  // A change is drawn a quarter second after the last one, and during a long drag at least every 700 milliseconds, so the picture follows the hand.
  const since = useRef(0);
  useEffect(() => {
    const now = Date.now();
    if (!since.current) since.current = now;
    const timer = setTimeout(() => { since.current = 0; wanted.current = body; run(); }, Math.max(0, Math.min(250, since.current + 700 - now)));
    return () => clearTimeout(timer);
  }, [body, guild.id]);
  return { shown, error, busy };
}

const kilobytes = (bytes) => (bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`);

// What the preview says under the picture: its size, and anything that didn't go as designed.
export function PreviewNotes({ preview }) {
  const { shown, error } = preview;
  return html`<div class="wc-notes">
    ${error && html`<${Notice} kind="bad">${(error.fields && Object.values(error.fields)[0]) || error.message}<//>`}
    ${shown && html`<div class="muted small" role="status">${shown.width} x ${shown.height} ${shown.kind.toUpperCase()}${shown.frames > 1 ? `, ${shown.frames} frames` : ''}, ${kilobytes(shown.bytes)}.
      Drawn for you, the way it is drawn for someone who joins.</div>`}
    ${shown && shown.notes.map((note) => html`<${Notice} key=${note} kind="warn">${note}<//>`)}
  </div>`;
}
