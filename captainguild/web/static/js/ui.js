// The dashboard's shared components. Pages build from these, so every page looks and behaves the same.
import { html, Component, createContext, useContext, useEffect, useMemo, useRef, useState } from './lib.js';
import { ICONS } from './icons.js';
import { api } from './api.js';
import { memberLabel, useMembers } from './members.js';

export const GuildContext = createContext(null);
export const useGuild = () => useContext(GuildContext);

export const sameId = (a, b) => String(a) === String(b);

// ---- unsaved work ----
// Pages and dialogs holding edits that aren't saved yet register here. The shell asks before it leaves a page, and the browser asks before a tab closes.
const unsavedHolders = new Set();
export const hasUnsaved = () => unsavedHolders.size > 0;
export function useUnsaved(flag) {
  const holder = useRef(null);
  if (holder.current === null) holder.current = {};
  useEffect(() => {
    if (flag) unsavedHolders.add(holder.current);
    else unsavedHolders.delete(holder.current);
    return () => { unsavedHolders.delete(holder.current); };
  }, [flag]);
}

// ---- icons ----
export function Icon({ name, size = 16, class: cls = '' }) {
  const body = ICONS[name] || ICONS['circle-help'];
  return html`<svg class=${'icon ' + cls} width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" dangerouslySetInnerHTML=${{ __html: body }}></svg>`;
}

// ---- toasts ----
let toastListener = null;
let toastId = 0;
const shownUntil = new Map();
export function toast(text, kind = 'ok', ms = 4500) {
  // A page that polls a failing server asks again every second or two, so the same error shows once while it is still on screen.
  const now = Date.now();
  if (kind === 'bad' && (shownUntil.get(text) || 0) > now) return;
  if (kind === 'bad') shownUntil.set(text, now + ms);
  if (toastListener) toastListener({ id: ++toastId, text, kind, ms });
}
export function toastError(error) {
  // A sign-in that ended has its own notice on the page.
  if (error && error.status === 401) return;
  toast(error && error.message ? error.message : String(error), 'bad', 7000);
}
export function Toasts() {
  const [items, setItems] = useState([]);
  useEffect(() => {
    toastListener = (item) => {
      setItems((list) => [...list, item]);
      setTimeout(() => setItems((list) => list.filter((x) => x.id !== item.id)), item.ms);
    };
    return () => { toastListener = null; };
  }, []);
  return html`<div class="toasts" role="status">${items.map((t) => html`<div key=${t.id} class=${'toast ' + t.kind}>${t.text}</div>`)}</div>`;
}

// ---- dialogs ----
let dialogListener = null;
let openDialog = null;
export function confirmDialog({ title = 'Are you sure?', body = '', confirm = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    if (!dialogListener) { resolve(false); return; }
    // A second question while one is open answers the first with No, so no caller waits forever.
    if (openDialog) openDialog.resolve(false);
    openDialog = { title, body, confirm, danger, resolve };
    dialogListener(openDialog);
  });
}
export function DialogHost() {
  const [dialog, setDialog] = useState(null);
  useEffect(() => {
    dialogListener = setDialog;
    return () => {
      dialogListener = null;
      if (openDialog) { openDialog.resolve(false); openDialog = null; }
    };
  }, []);
  if (!dialog) return null;
  const finish = (answer) => {
    dialog.resolve(answer);
    if (openDialog === dialog) openDialog = null;
    setDialog(null);
  };
  return html`<${Modal} key=${dialog.title + dialog.body} title=${dialog.title} onClose=${() => finish(false)} footer=${html`
      <div class="right">
        <button class="btn ghost" onClick=${() => finish(false)}>Cancel</button>
        <button class=${'btn ' + (dialog.danger ? 'danger' : 'primary')} onClick=${() => finish(true)}>${dialog.confirm}</button>
      </div>`}>
    <div>${dialog.body}</div>
  <//>`;
}

// ---- layout ----
export function Card({ icon, title, right, footer, children, class: cls = '' }) {
  return html`<div class=${'card ' + cls}>
    ${(title || right) && html`<div class="card-head">${icon && html`<${Icon} name=${icon} />`}<span>${title}</span>${right && html`<div class="right">${right}</div>`}</div>`}
    <div class="card-body">${children}</div>
    ${footer && html`<div class="card-foot">${footer}</div>`}
  </div>`;
}
export const Grid = ({ children }) => html`<div class="grid">${children}</div>`;
export const Col = ({ children }) => html`<div class="col">${children}</div>`;
export const Loading = ({ text = 'Loading...' }) => html`<div class="loading">${text}</div>`;
export const Empty = ({ children }) => html`<div class="empty">${children}</div>`;
export const Notice = ({ kind = '', children }) => html`<div class=${'notice ' + kind}>${children}</div>`;

// One render error stays inside the box around it, so the rest of the dashboard keeps working.
export class Boundary extends Component {
  constructor(props) {
    super(props);
    this.state = { failed: null };
  }

  static getDerivedStateFromError(error) { return { failed: error }; }

  componentDidCatch(error) { console.error(error); }

  render() {
    if (!this.state.failed) return this.props.children;
    return html`<${Notice} kind="bad">${this.props.what || 'This page'} hit an error. <button type="button" class="link-btn" onClick=${() => location.reload()}>Reload</button> to try again.<//>`;
  }
}

export function Tabs({ tabs, active, onChange }) {
  return html`<div class="tabs" role="tablist">${tabs.map((t) => html`
    <button key=${t.key} role="tab" aria-selected=${t.key === active} class=${'tab' + (t.key === active ? ' on' : '')} onClick=${() => onChange(t.key)}>${t.label}</button>`)}</div>`;
}

// The open dialogs, innermost last. Escape and Tab belong to the innermost one only.
const modalStack = [];
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Where Tab goes inside a dialog: the control to focus, or null to let the browser move on its own.
export function trapTab(items, active, backwards) {
  if (!items.length) return null;
  const at = items.indexOf(active);
  if (at < 0) return backwards ? items[items.length - 1] : items[0];
  if (backwards && at === 0) return items[items.length - 1];
  if (!backwards && at === items.length - 1) return items[0];
  return null;
}

export function Modal({ title, icon, large, onClose, footer, children, dirty = false }) {
  const dialog = useRef(null);
  const token = useRef(null);
  if (token.current === null) token.current = {};
  useUnsaved(!!dirty);
  // With edits in the form, closing from the corner or the keyboard asks first.
  const latest = useRef({});
  latest.current = { onClose, dirty };
  const requestClose = async () => {
    const { onClose: close, dirty: edited } = latest.current;
    if (!close) return;
    if (edited && !(await confirmDialog({ title: 'Discard your changes?', body: 'What you changed here has not been saved.', confirm: 'Discard', danger: true }))) return;
    close();
  };
  useEffect(() => {
    modalStack.push(token.current);
    const root = dialog.current;
    const previous = typeof document !== 'undefined' ? document.activeElement : null;
    if (root && root.querySelector) {
      const first = root.querySelector('.modal-body input:not([type=hidden]):not([disabled]), .modal-body select:not([disabled]), .modal-body textarea:not([disabled]), .modal-body button:not([disabled]), .modal-foot button:not([disabled])');
      const target = first || root;
      // A picker takes focus without opening its list, which would cover the next field and take the first Escape.
      const picker = target.getAttribute && target.getAttribute('role') === 'combobox' && target.dataset;
      if (picker) target.dataset.quiet = '1';
      if (target.focus) target.focus();
      if (picker) delete target.dataset.quiet;
    }
    return () => {
      const at = modalStack.indexOf(token.current);
      if (at >= 0) modalStack.splice(at, 1);
      if (previous && previous.focus && (!document.contains || document.contains(previous))) previous.focus();
    };
  }, []);
  useEffect(() => {
    const onKey = (event) => {
      if (modalStack[modalStack.length - 1] !== token.current) return;
      if (event.key === 'Escape' && !event.defaultPrevented) {
        if (latest.current.onClose) requestClose();
      } else if (event.key === 'Tab' && dialog.current && dialog.current.querySelectorAll) {
        const items = [...dialog.current.querySelectorAll(FOCUSABLE)];
        const target = trapTab(items, document.activeElement, event.shiftKey);
        if (target) { event.preventDefault(); target.focus(); }
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);
  return html`<div class="modal-back" role="dialog" aria-modal="true" aria-label=${title}>
    <div class=${'modal' + (large ? ' large' : '')} ref=${dialog} tabindex="-1">
      <div class="modal-head">${icon && html`<${Icon} name=${icon} size=${20} />`}<h3>${title}</h3>
        ${onClose && html`<button class="icon-btn" aria-label="Close" onClick=${requestClose}><${Icon} name="x" size=${18} /></button>`}</div>
      <div class="modal-body">${children}</div>
      ${footer && html`<div class="modal-foot">${footer}</div>`}
    </div>
  </div>`;
}

// ---- form fields ----
export function Field({ label, help, error, children }) {
  return html`<div class="field">
    ${label && html`<label>${label}</label>`}
    ${children}
    ${help && html`<div class="help">${help}</div>`}
    ${error && html`<div class="error">${error}</div>`}
  </div>`;
}

export function TextInput({ label, help, error, value, onChange, placeholder, maxLength, counter, type = 'text', disabled, addon, button }) {
  const text = value === null || value === undefined ? '' : value;
  const input = html`<input type=${type} value=${text} placeholder=${placeholder} disabled=${disabled} aria-label=${label || placeholder}
    onInput=${(e) => onChange && onChange(e.currentTarget.value)} />`;
  return html`<${Field} label=${label} help=${help} error=${error}>
    ${addon || button ? html`<div class="input-group">${input}${addon && html`<span class="addon">${addon}</span>`}${button}</div>` : input}
    ${maxLength && (counter || (counter === undefined && String(text).length >= maxLength * 0.8)) && html`<div class=${'counter' + (String(text).length > maxLength ? ' over' : '')}>${String(text).length}/${maxLength}</div>`}
  <//>`;
}

export function NumberInput({ label, help, error, value, onChange, min, max, step, placeholder, addon, disabled }) {
  const input = html`<input type="number" value=${value === null || value === undefined ? '' : value} min=${min} max=${max} step=${step} placeholder=${placeholder}
    disabled=${disabled} aria-label=${label || placeholder}
    onInput=${(e) => { const raw = e.currentTarget.value; onChange && onChange(raw === '' ? null : Number(raw)); }} />`;
  return html`<${Field} label=${label} help=${help} error=${error}>
    ${addon ? html`<div class="input-group">${input}<span class="addon">${addon}</span></div>` : input}
  <//>`;
}

export function TextArea({ label, help, error, value, onChange, placeholder, maxLength, rows = 4, disabled, counter = true }) {
  const text = value || '';
  return html`<${Field} label=${label} help=${help} error=${error}>
    <textarea rows=${rows} value=${text} placeholder=${placeholder} disabled=${disabled} aria-label=${label || placeholder} onInput=${(e) => onChange && onChange(e.currentTarget.value)}></textarea>
    ${counter && maxLength && html`<div class=${'counter' + (text.length > maxLength ? ' over' : '')}>${text.length}/${maxLength}</div>`}
  <//>`;
}

// A color typed or pasted as hex: "#1abc9c", "1abc9c" and the short "#1bc" all read as #1abc9c. Anything else is null.
export function normalizeHex(text) {
  const t = String(text || '').trim().replace(/^#/, '');
  if (/^[0-9a-fA-F]{6}$/.test(t)) return '#' + t.toLowerCase();
  if (/^[0-9a-fA-F]{3}$/.test(t)) return '#' + t.split('').map((c) => c + c).join('').toLowerCase();
  return null;
}
// onChange gets a '#rrggbb' string as soon as the box holds a whole color, and '' when it is emptied.
export function HexInput({ label = 'Color as hex', value, onChange, placeholder = '#rrggbb' }) {
  const [draft, setDraft] = useState(null);
  const text = draft === null ? (value || '') : draft;
  const bad = draft !== null && draft.trim() !== '' && normalizeHex(draft) === null;
  return html`<input type="text" class=${'hex-input' + (bad ? ' bad' : '')} aria-label=${label} aria-invalid=${bad} placeholder=${placeholder} value=${text} maxLength="7" spellcheck="false"
    onInput=${(e) => {
      const next = e.currentTarget.value;
      setDraft(next);
      const hex = normalizeHex(next);
      if (hex) onChange(hex);
      else if (next.trim() === '') onChange('');
    }} onBlur=${() => setDraft(null)} />`;
}

export function Select({ label, ariaLabel, help, error, value, onChange, options, disabled }) {
  const index = options.findIndex((o) => sameId(o.value, value));
  return html`<${Field} label=${label} help=${help} error=${error}>
    <select disabled=${disabled} aria-label=${label || ariaLabel} value=${String(index)} onChange=${(e) => onChange(options[Number(e.currentTarget.value)].value)}>
      ${index < 0 && html`<option value="-1" hidden>Select...</option>`}
      ${options.map((o, i) => html`<option key=${i} value=${String(i)} selected=${i === index}>${o.label}</option>`)}
    </select>
  <//>`;
}

export function Switch({ label, help, error, value, onChange, disabled }) {
  return html`<div class="field">
    <div class="switch-row">
      <span class="label" style=${{ marginBottom: 0 }}>${label}</span>
      <label class="switch"><input type="checkbox" role="switch" aria-label=${label} checked=${!!value} disabled=${disabled} onChange=${(e) => onChange(e.currentTarget.checked)} /><span></span></label>
    </div>
    ${help && html`<div class="help">${help}</div>`}
    ${error && html`<div class="error">${error}</div>`}
  </div>`;
}

export function Check({ label, value, onChange, disabled }) {
  return html`<label class="check"><input type="checkbox" checked=${!!value} disabled=${disabled} onChange=${(e) => onChange(e.currentTarget.checked)} /><span>${label}</span></label>`;
}

// A length of time shown as separate boxes, as seconds to the code. units is any of 'd', 'h', 'm'.
const UNIT = { d: ['Days', 86400], h: ['Hours', 3600], m: ['Minutes', 60] };
export function DurationInput({ label, help, error, value, onChange, units = ['h', 'm'], disabled }) {
  const seconds = Number(value) || 0;
  let left = seconds;
  const parts = {};
  for (const unit of units) {
    parts[unit] = Math.floor(left / UNIT[unit][1]);
    left -= parts[unit] * UNIT[unit][1];
  }
  const change = (unit, amount) => {
    const next = { ...parts, [unit]: Math.max(0, Number(amount) || 0) };
    onChange(units.reduce((sum, u) => sum + next[u] * UNIT[u][1], 0));
  };
  return html`<${Field} label=${label} help=${help} error=${error}>
    <div class="input-row">${units.map((unit) => html`
      <div class="input-group" key=${unit}>
        <input type="number" min="0" value=${parts[unit]} disabled=${disabled} aria-label=${UNIT[unit][0]} onInput=${(e) => change(unit, e.currentTarget.value)} />
        <span class="addon">${UNIT[unit][0]}</span>
      </div>`)}</div>
  <//>`;
}

/// ---- searching picker, one or many ----
// resolve(value) names a saved value that isn't among the options, such as a channel of a kind this list leaves out.
// A value it can't name is shown as gone, and warnMissing adds a line under the field saying so.
export function Picker({ label, help, error, value, onChange, options, multi = false, placeholder = 'Select option', clearable = true, disabled, renderOption, resolve, warnMissing }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const root = useRef(null);
  const list = useRef(null);
  const selected = multi ? (value || []) : (value === null || value === undefined || value === '' ? [] : [value]);
  const isOn = (v) => selected.some((s) => sameId(s, v));
  const byValue = useMemo(() => new Map(options.map((o) => [String(o.value), o])), [options]);

  useEffect(() => {
    if (!open) return undefined;
    const away = (event) => { if (root.current && !root.current.contains(event.target)) { setOpen(false); setQuery(''); } };
    document.addEventListener('mousedown', away);
    return () => document.removeEventListener('mousedown', away);
  }, [open]);

  const needle = query.trim().toLowerCase();
  const visible = options.filter((o) => !needle || o.label.toLowerCase().includes(needle));
  const shownCount = Math.min(visible.length, PICKER_LIMIT);
  // The highlighted row stays in view while the arrow keys move it.
  useEffect(() => {
    if (!open || !list.current || !list.current.querySelector) return;
    const row = list.current.querySelector('.ms-option.active');
    if (row && row.scrollIntoView) row.scrollIntoView({ block: 'nearest' });
  }, [open, active]);
  const close = () => { setOpen(false); setQuery(''); };
  const pick = (o) => {
    if (o.disabled) return;
    if (multi) {
      onChange(isOn(o.value) ? selected.filter((s) => !sameId(s, o.value)) : [...selected, o.value]);
      setQuery('');
    } else {
      onChange(o.value);
      close();
    }
  };
  const remove = (v) => onChange(multi ? selected.filter((s) => !sameId(s, v)) : null);
  const openList = () => {
    if (open || disabled) return;
    setOpen(true);
    const at = multi || selected.length === 0 ? 0 : visible.findIndex((o) => sameId(o.value, selected[0]));
    setActive(Math.max(0, at));
  };
  const onKey = (event) => {
    if (event.key === 'ArrowDown') { event.preventDefault(); if (!open) openList(); else setActive((i) => Math.min(i + 1, shownCount - 1)); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); if (!open) openList(); else setActive((i) => Math.max(i - 1, 0)); }
    else if (event.key === 'Enter') { event.preventDefault(); if (open && visible[active]) pick(visible[active]); else openList(); }
    else if (event.key === 'Escape') { if (open) { event.stopPropagation(); close(); } }
    else if (event.key === 'Tab') { if (open) close(); }
    else if (event.key === 'Backspace' && !query && multi && selected.length) { remove(selected[selected.length - 1]); }
  };
  const lookup = (v) => byValue.get(String(v)) || (resolve && resolve(v)) || null;
  const show = (v) => {
    const o = lookup(v) || { label: MISSING_LABEL, value: v, missing: true };
    return html`<span class="row" style=${{ gap: '5px', flexWrap: 'nowrap' }}>${renderOption ? renderOption(o) : defaultOption(o)}</span>`;
  };
  const gone = warnMissing ? selected.filter((v) => !lookup(v)) : [];
  const goneText = gone.length === 0 ? null : (gone.length === 1 ? "This is no longer in the server, or the bot can't see it." : `${gone.length} of these are no longer in the server, or the bot can't see them.`);
  let lastGroup = null;
  const name = label || placeholder;
  return html`<${Field} label=${label} help=${help} error=${error || goneText}>
    <div class="ms" ref=${root}>
      <div class="ms-box" onClick=${() => !disabled && openList()}>
        ${multi && selected.map((v) => html`<span class="chip" key=${String(v)}>${show(v)}
          <button type="button" aria-label="Remove" onClick=${(e) => { e.stopPropagation(); remove(v); }}><${Icon} name="x" size=${12} /></button></span>`)}
        ${!multi && selected.length > 0 && !open && html`<span class="ms-single">${show(selected[0])}</span>`}
        <input type="text" role="combobox" aria-expanded=${open} aria-haspopup="listbox" aria-autocomplete="list" value=${query} disabled=${disabled}
          placeholder=${selected.length ? '' : placeholder} aria-label=${name}
          onFocus=${(e) => { if (!(e.currentTarget.dataset || {}).quiet) openList(); }} onKeyDown=${onKey} onInput=${(e) => { setQuery(e.currentTarget.value); setActive(0); setOpen(true); }} />
      </div>
      ${!multi && clearable && selected.length > 0 && !disabled && html`<button type="button" class="icon-btn ms-caret" style=${{ pointerEvents: 'auto', top: '8px', right: '6px' }} aria-label="Clear" onClick=${() => onChange(null)}><${Icon} name="x" size=${14} /></button>`}
      ${(multi || !clearable || selected.length === 0) && html`<span class="ms-caret"><${Icon} name="chevron-down" size=${14} /></span>`}
      ${open && html`<div class="ms-list" role="listbox" ref=${list} aria-label=${name}>
        ${visible.length === 0 && html`<div class="ms-empty">Nothing matches.</div>`}
        ${visible.slice(0, PICKER_LIMIT).map((o, i) => {
          const groupKey = o.groupKey !== undefined ? o.groupKey : o.group;
          const heading = o.group && groupKey !== lastGroup ? html`<div class="ms-group">${o.group}</div>` : null;
          lastGroup = o.group ? groupKey : lastGroup;
          return html`${heading}<div role="option" aria-selected=${isOn(o.value)} class=${'ms-option' + (i === active ? ' active' : '') + (isOn(o.value) ? ' on' : '')}
            onMouseDown=${(e) => e.preventDefault()} onClick=${() => pick(o)}>${renderOption ? renderOption(o) : defaultOption(o)}</div>`;
        })}
        ${visible.length > PICKER_LIMIT && html`<div class="ms-empty">Showing ${PICKER_LIMIT} of ${visible.length}. Type to narrow it.</div>`}
      </div>`}
    </div>
  <//>`;
}
const PICKER_LIMIT = 300;
const MISSING_LABEL = 'Unknown (deleted or hidden)';

function defaultOption(o) {
  return html`${o.color !== undefined && html`<span class="dot" style=${{ background: o.color ? '#' + Number(o.color).toString(16).padStart(6, '0') : '#99aab5' }}></span>`}
    ${o.prefix && html`<span class="dim">${o.prefix}</span>`}<span>${o.label}</span>${o.note && html`<span class="dim">${o.note}</span>`}`;
}

// The channels of a server as picker rows, the way Discord's own list reads: channels with no category first, then each category in order with its channels under it.
// A picker that offers categories lists them together as one block above the rest.
// types: 0 text, 2 voice, 4 category, 5 announcement, 13 stage, 15 forum.
export function channelOptions(everyChannel, { types = [0, 5], categories = false } = {}) {
  const channels = everyChannel.filter((c) => !c.hidden); // what Discord hides from this person can't be picked, and a stored one still resolves to a row
  const wanted = new Set(types);
  const voiceLike = (c) => (c.type === 2 || c.type === 13 ? 1 : 0);
  const byPosition = (a, b) => (a.position - b.position) || String(a.id).localeCompare(String(b.id));
  const parents = channels.filter((c) => c.type === 4).sort(byPosition);
  const known = new Set(parents.map((c) => String(c.id)));
  const hasParent = (c) => c.parent_id !== null && c.parent_id !== undefined && known.has(String(c.parent_id));
  const icon = (c) => (voiceLike(c) ? '\u{1F50A}' : '#');
  // Discord lists text-like channels first and voice-like ones after, each in position order.
  const inside = (belongs) => channels.filter((c) => c.type !== 4 && wanted.has(c.type) && belongs(c)).sort((a, b) => (voiceLike(a) - voiceLike(b)) || byPosition(a, b));
  const rows = [];
  if (categories) {
    for (const c of parents) rows.push({ value: c.id, label: c.name, prefix: '', group: 'Categories', groupKey: 'categories' });
  }
  for (const c of inside((x) => !hasParent(x))) rows.push({ value: c.id, label: c.name, prefix: icon(c), group: 'No category', groupKey: 'none' });
  for (const parent of parents) {
    for (const c of inside((x) => hasParent(x) && String(x.parent_id) === String(parent.id))) {
      rows.push({ value: c.id, label: c.name, prefix: icon(c), group: parent.name, groupKey: 'c' + parent.id });
    }
  }
  return rows;
}

// Every kind of channel that has a text chat or can hold posts: text, voice, announcement, stage and forum.
export const ANY_CHANNEL = [0, 2, 5, 13, 15];

export function ChannelPicker({ types = [0, 5], categories = false, extra = [], ...props }) {
  const guild = useGuild();
  const options = useMemo(() => [...extra, ...channelOptions(guild.channels, { types, categories })], [guild, types.join(','), categories, extra.length]);
  // A channel the server has but this list leaves out, such as a voice channel in a text list, keeps its name.
  const resolve = (id) => {
    const found = guild.channels.find((c) => sameId(c.id, id));
    return found ? { value: found.id, label: found.name, prefix: found.type === 4 ? '' : '#', note: '(not in this list)' } : null;
  };
  return html`<${Picker} options=${options} resolve=${resolve} warnMissing=${true} ...${props} placeholder=${props.placeholder || (props.multi ? 'Select option' : 'Please select a channel')} />`;
}

export function RolePicker({ everyone = false, managed = false, extra = [], ...props }) {
  const guild = useGuild();
  const options = useMemo(() => {
    const rows = guild.roles
      .filter((r) => (everyone || !r.default) && (managed || !r.managed))
      .map((r) => ({ value: r.id, label: r.name, color: r.color }));
    return [...extra, ...rows];
  }, [guild, everyone, managed, extra.length]);
  const resolve = (id) => {
    const found = guild.roles.find((r) => sameId(r.id, id));
    return found ? { value: found.id, label: found.name, color: found.color, note: '(not in this list)' } : null;
  };
  return html`<${Picker} options=${options} resolve=${resolve} warnMissing=${true} ...${props} placeholder=${props.placeholder || (props.multi ? 'Select option' : 'Please select a role')} />`;
}

export function MemberPicker({ label, help, error, value, onChange, placeholder = 'Search by name or ID' }) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [picked, setPicked] = useState(null);
  const [active, setActive] = useState(0);
  useEffect(() => {
    if (!value) { setPicked(null); return undefined; }
    if (picked && sameId(picked.id, value)) return undefined;
    let live = true;
    api.get(`/members/${value}`).then((m) => { if (live) setPicked(m); }).catch(() => { if (live) setPicked({ id: value, name: String(value), display_name: String(value) }); });
    return () => { live = false; };
  }, [value]);
  useEffect(() => {
    if (query.trim().length < 2) { setResults([]); return undefined; }
    // Answers can arrive out of order, so only the newest question's answer is kept.
    let live = true;
    const timer = setTimeout(() => api.get(`/members/search?q=${encodeURIComponent(query.trim())}`)
      .then((r) => { if (live) { setResults(r); setActive(0); } }).catch(() => { if (live) setResults([]); }), 250);
    return () => { live = false; clearTimeout(timer); };
  }, [query]);
  const choose = (m) => { setPicked(m); onChange(m.id); setResults([]); setQuery(''); };
  const onKey = (event) => {
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive((i) => Math.min(i + 1, results.length - 1)); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
    else if (event.key === 'Enter' && results[active]) { event.preventDefault(); choose(results[active]); }
    else if (event.key === 'Escape' && results.length) { event.stopPropagation(); setResults([]); }
  };
  return html`<${Field} label=${label} help=${help} error=${error}>
    ${picked && value ? html`<span class="chip"><img class="avatar" style=${{ width: '20px', height: '20px' }} src=${picked.avatar} alt="" />${picked.display_name || picked.name}
        <button type="button" aria-label="Clear" onClick=${() => { onChange(null); setQuery(''); }}><${Icon} name="x" size=${12} /></button></span>`
      : html`<div class="ms"><input type="text" role="combobox" aria-expanded=${results.length > 0} aria-autocomplete="list" value=${query} placeholder=${placeholder} aria-label=${label || placeholder}
          onKeyDown=${onKey} onInput=${(e) => setQuery(e.currentTarget.value)} />
        ${results.length > 0 && html`<div class="ms-list" role="listbox">${results.map((m, i) => html`<div key=${m.id} role="option" aria-selected=${i === active} class=${'ms-option' + (i === active ? ' active' : '')}
          onMouseDown=${(e) => e.preventDefault()} onClick=${() => choose(m)}>
          <img class="avatar" style=${{ width: '22px', height: '22px' }} src=${m.avatar} alt="" />${m.display_name} <span class="dim">@${m.name}</span></div>`)}</div>`}</div>`}
  <//>`;
}

// Any number of people, each as a chip with their name. Their names come in batches, so a list of hundreds is a few requests. The picker is rebuilt after each pick, so its search box comes back empty.
export function PeopleField({ label, help, error, value, onChange, placeholder }) {
  const guild = useGuild();
  const [round, setRound] = useState(0);
  const people = value || [];
  const lookup = useMembers(guild.id, people);
  const add = (id) => {
    if (id && !people.some((p) => String(p) === String(id))) onChange([...people, id]);
    setRound(round + 1);
  };
  return html`<${Field} label=${label} help=${help} error=${error}>
    ${people.length > 0 && html`<div class="row log-chips">${people.map((id) => {
      const row = lookup(id);
      return html`<span class="chip" key=${String(id)}>
        ${row && html`<img class="avatar log-chip-avatar" src=${row.avatar} alt="" />`}
        <span>${memberLabel(guild.id, id)}</span>
        <button type="button" aria-label=${'Remove ' + memberLabel(guild.id, id)} onClick=${() => onChange(people.filter((p) => String(p) !== String(id)))}><${Icon} name="x" size=${12} /></button>
      </span>`;
    })}</div>`}
    <${MemberPicker} key=${round} value=${null} placeholder=${placeholder} onChange=${add} />
  <//>`;
}

// ---- tables ----
export function Pager({ page, pages, onPage }) {
  if (pages <= 1) return null;
  const numbers = [];
  for (let i = Math.max(1, page - 2); i <= Math.min(pages, page + 2); i += 1) numbers.push(i);
  return html`<div class="pager">
    <button disabled=${page <= 1} aria-label="Previous page" onClick=${() => onPage(page - 1)}>${'<'}</button>
    ${numbers.map((n) => html`<button key=${n} class=${n === page ? 'on' : ''} onClick=${() => onPage(n)}>${n}</button>`)}
    <button disabled=${page >= pages} aria-label="Next page" onClick=${() => onPage(page + 1)}>${'>'}</button>
  </div>`;
}

// columns: [{ key, label, render(row), text(row), sort: true, filter: true, width }]. Filters and sorting run on text(row), or row[key].
export function DataTable({ columns, rows, rowKey = 'id', pageSizes = [10, 25, 50, 100], empty = 'No matching records', loading }) {
  const [size, setSize] = useState(pageSizes[0]);
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState(null);
  const [filters, setFilters] = useState({});
  const textOf = (column, row) => {
    const value = column.text ? column.text(row) : row[column.key];
    return value === null || value === undefined ? '' : value;
  };
  const shown = useMemo(() => {
    let list = rows || [];
    for (const column of columns) {
      const needle = (filters[column.key] || '').trim().toLowerCase();
      if (needle) list = list.filter((row) => String(textOf(column, row)).toLowerCase().includes(needle));
    }
    if (sort) {
      const column = columns.find((c) => c.key === sort.key);
      list = [...list].sort((a, b) => {
        const x = textOf(column, a);
        const y = textOf(column, b);
        const order = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y));
        return sort.down ? -order : order;
      });
    }
    return list;
  }, [rows, filters, sort]);
  const pages = Math.max(1, Math.ceil(shown.length / size));
  const current = Math.min(page, pages);
  const start = (current - 1) * size;
  const hasFilters = columns.some((c) => c.filter);
  const flip = (column) => {
    if (!column.sort) return;
    setSort((s) => (s && s.key === column.key ? { key: column.key, down: !s.down } : { key: column.key, down: false }));
  };
  return html`<div>
    ${pageSizes.length > 1 && html`<div class="table-tools"><span>Records:</span>
      <select aria-label="Records per page" value=${size} onChange=${(e) => { setSize(Number(e.currentTarget.value)); setPage(1); }}>
        ${pageSizes.map((n) => html`<option key=${n} value=${n}>${n}</option>`)}</select></div>`}
    <div class="table-wrap"><table>
      <thead>
        <tr>${columns.map((c) => html`<th key=${c.key} style=${c.width ? { width: c.width } : null} class=${c.sort ? 'sortable' : ''}
          aria-sort=${c.sort ? (sort && sort.key === c.key ? (sort.down ? 'descending' : 'ascending') : 'none') : null}>
          ${c.sort ? html`<button type="button" class="th-sort" onClick=${() => flip(c)}>${c.label}${sort && sort.key === c.key ? (sort.down ? ' ↓' : ' ↑') : ''}</button>` : c.label}</th>`)}</tr>
        ${hasFilters && html`<tr>${columns.map((c) => html`<th key=${c.key}>${c.filter && html`<input type="text" placeholder=${'Filter by ' + c.label} aria-label=${'Filter by ' + c.label} value=${filters[c.key] || ''}
          onInput=${(e) => { setFilters({ ...filters, [c.key]: e.currentTarget.value }); setPage(1); }} />`}</th>`)}</tr>`}
      </thead>
      <tbody>
        ${loading && html`<tr><td colspan=${columns.length}><${Loading} /></td></tr>`}
        ${!loading && shown.length === 0 && html`<tr><td colspan=${columns.length}><${Empty}>${empty}<//></td></tr>`}
        ${!loading && shown.slice(start, start + size).map((row, index) => html`<tr key=${row[rowKey] !== undefined ? String(row[rowKey]) : index}>
          ${columns.map((c) => html`<td key=${c.key}>${c.render ? c.render(row) : textOf(c, row)}</td>`)}</tr>`)}
      </tbody>
    </table></div>
    <div class="table-foot">
      <span class="muted small">Showing ${shown.length ? start + 1 : 0} to ${Math.min(start + size, shown.length)} of ${shown.length} records</span>
      <${Pager} page=${current} pages=${pages} onPage=${setPage} />
    </div>
  </div>`;
}

// ---- settings forms ----
function getPath(object, path) {
  return path.split('.').reduce((value, key) => (value === null || value === undefined ? undefined : value[key]), object);
}
function setPath(object, path, value) {
  const keys = path.split('.');
  const copy = Array.isArray(object) ? [...object] : { ...object };
  let cursor = copy;
  keys.slice(0, -1).forEach((key) => {
    cursor[key] = Array.isArray(cursor[key]) ? [...cursor[key]] : { ...cursor[key] };
    cursor = cursor[key];
  });
  cursor[keys[keys.length - 1]] = value;
  return copy;
}

// The top-level settings whose value differs from the saved copy.
export function changedFields(data, saved) {
  const base = saved || {};
  return Object.keys(data || {}).filter((key) => JSON.stringify(data[key]) !== JSON.stringify(base[key]));
}
function pickFields(data, keys) {
  const out = {};
  for (const key of keys) out[key] = data[key];
  return out;
}

// Loads one module's settings. form.bind('some.path') gives value, onChange and error for any input above.
export function useSettings(module) {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [saved, setSaved] = useState(null);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(null);

  useEffect(() => {
    let live = true;
    setData(null);
    setFailed(null);
    api.get(`/settings/${module}`).then((d) => { if (live) { setData(d); setSaved(d); } }).catch((e) => { if (live) setFailed(e); });
    return () => { live = false; };
  }, [guild.id, module]);

  const set = (path, value) => {
    setData((current) => setPath(current, path, value));
    setErrors((current) => { const next = { ...current }; delete next[path]; return next; });
  };
  const dirty = data !== null && saved !== null && changedFields(data, saved).length > 0;
  useUnsaved(dirty);
  const save = async () => {
    setSaving(true);
    try {
      // Only what this page changed goes back, so a second moderator's change to another field, or one made by a command meanwhile, isn't undone.
      const fresh = await api.put(`/settings/${module}`, pickFields(data, changedFields(data, saved)));
      setData(fresh);
      setSaved(fresh);
      setErrors({});
      toast('Saved.');
      return true;
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
      return false;
    } finally {
      setSaving(false);
    }
  };
  // Saves just these fields now, such as a list where each add or remove should stick at once. Edits made to other fields stay where they are.
  const saveFields = async (changes) => {
    setSaving(true);
    try {
      const fresh = await api.put(`/settings/${module}`, changes);
      const keys = Object.keys(changes);
      setData((current) => ({ ...current, ...pickFields(fresh, keys) }));
      setSaved((current) => ({ ...current, ...pickFields(fresh, keys) }));
      toast('Saved.');
      return true;
    } catch (error) {
      toastError(error);
      return false;
    } finally {
      setSaving(false);
    }
  };
  return {
    data, saved, set, dirty, save, saveFields, saving, errors, failed,
    loading: data === null && !failed,
    reset: () => { setData(saved); setErrors({}); },
    bind: (path) => ({ value: getPath(data, path), onChange: (value) => set(path, value), error: errors[path] }),
  };
}

export function SaveButton({ form, label = 'Save' }) {
  return html`<button class="btn primary" disabled=${form.saving} onClick=${form.save}>${form.saving ? 'Saving...' : label}</button>`;
}

export function SaveBar({ form }) {
  if (!form.dirty) return null;
  return html`<div class="save-bar" role="status"><span>Changes detected</span>
    <button class="btn ghost right" onClick=${form.reset}>Reset</button>
    <button class="btn primary" disabled=${form.saving} onClick=${form.save}>${form.saving ? 'Saving...' : 'Confirm'}</button></div>`;
}

// Shows a loading or failed state until the module's settings have arrived, then renders the page.
export function SettingsGate({ form, children }) {
  if (form.failed) return html`<${Notice} kind="bad">${form.failed.message}<//>`;
  if (form.loading) return html`<${Loading} />`;
  return children();
}

// ---- small helpers ----
export function when(seconds) {
  if (!seconds) return '';
  const date = new Date(seconds * 1000);
  return date.toLocaleString(undefined, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
// A thread isn't in the server's channel list, so a page that knows its name from elsewhere passes it as the fallback.
export function channelName(guild, id, fallback) {
  const channel = guild.channels.find((c) => sameId(c.id, id));
  return channel ? channel.name : (fallback || 'unknown channel');
}
export function roleName(guild, id) {
  const role = guild.roles.find((r) => sameId(r.id, id));
  return role ? role.name : 'unknown role';
}
// One rule for adding a prefix, shared by Quick setup and Bot settings. Spaces count, since a prefix can end in one. Returns the reason it can't be added, or null.
export const PREFIX_MAX = 10;
export const PREFIX_COUNT = 15;
export function prefixProblem(prefix, existing) {
  if (!prefix || !prefix.trim()) return 'Type a prefix first.';
  if (prefix.length > PREFIX_MAX) return `A prefix can be at most ${PREFIX_MAX} characters.`;
  if (existing.includes(prefix)) return 'That prefix is already in the list.';
  if (existing.length >= PREFIX_COUNT) return `You can have up to ${PREFIX_COUNT} prefixes.`;
  return null;
}
export function copyText(text) {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => toast('Copied.')).catch(() => toast('Copying was blocked by the browser.', 'bad'));
  } else {
    toast('Copying is not available in this browser.', 'bad');
  }
}

// ---- emoji ----
let emojiData = null;
async function loadEmoji() {
  if (!emojiData) {
    const response = await fetch('/static/vendor/emoji.json');
    emojiData = await response.json();
  }
  return emojiData;
}

// A custom emoji is <:name:id> or <a:name:id>, and anything else is the character itself.
export function EmojiView({ value, size = 22 }) {
  const match = /^<(a?):([^:>]+):(\d+)>$/.exec(value || '');
  if (match) return html`<img src=${`https://cdn.discordapp.com/emojis/${match[3]}.${match[1] ? 'gif' : 'png'}`} alt=${match[2]} title=${match[2]} style=${{ width: size + 'px', height: size + 'px', objectFit: 'contain', verticalAlign: 'middle' }} />`;
  return html`<span style=${{ fontSize: size - 2 + 'px', lineHeight: 1 }}>${value}</span>`;
}

// The last emoji picked, newest first, kept in this browser so a repeated pick doesn't mean a new search.
const RECENT_KEY = 'cg-emoji-recent';
const RECENT_MAX = 30;
export function readRecent() {
  try {
    const list = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
    return Array.isArray(list) ? list.filter((v) => typeof v === 'string' && v).slice(0, RECENT_MAX) : [];
  } catch (error) {
    return [];
  }
}
export function addRecent(list, value) {
  return [value, ...list.filter((v) => v !== value)].slice(0, RECENT_MAX);
}
function saveRecent(value) {
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(addRecent(readRecent(), value))); } catch (error) { /* a private window can refuse storage */ }
}

export function EmojiPicker({ onPick, onClose }) {
  const guild = useGuild();
  const [query, setQuery] = useState('');
  const [groups, setGroups] = useState([]);
  const [recent] = useState(readRecent);
  const root = useRef(null);
  const search = useRef(null);
  useEffect(() => { loadEmoji().then(setGroups).catch(() => setGroups([])); }, []);
  useEffect(() => {
    if (search.current && search.current.focus) search.current.focus();
    const away = (event) => { if (root.current && !root.current.contains(event.target)) onClose(); };
    document.addEventListener('mousedown', away);
    return () => document.removeEventListener('mousedown', away);
  }, []);
  const choose = (value) => { saveRecent(value); onPick(value); };
  const needle = query.trim().toLowerCase();
  const custom = guild.emojis.filter((e) => !needle || e.name.toLowerCase().includes(needle));
  const customText = (e) => `<${e.animated ? 'a' : ''}:${e.name}:${e.id}>`;
  const recents = needle ? [] : recent;
  return html`<div class="emoji-pop" ref=${root} onKeyDown=${(e) => { if (e.key === 'Escape') { e.stopPropagation(); onClose(); } }}>
    <input type="search" ref=${search} placeholder="Search" aria-label="Search emoji" value=${query} onInput=${(e) => setQuery(e.currentTarget.value)} />
    <div class="emoji-scroll">
      ${recents.length > 0 && html`<div class="emoji-group">Recent</div><div class="emoji-grid">${recents.map((v) => html`
        <button key=${v} type="button" class="emoji-btn" title=${v} onClick=${() => choose(v)}><${EmojiView} value=${v} size=${22} /></button>`)}</div>`}
      ${custom.length > 0 && html`<div class="emoji-group">${guild.name}</div><div class="emoji-grid">${custom.map((e) => html`
        <button key=${e.id} type="button" class="emoji-btn" title=${e.name} onClick=${() => choose(customText(e))}><img src=${e.url} alt=${e.name} /></button>`)}</div>`}
      ${groups.map((group) => {
        const rows = group.emojis.filter((row) => !needle || row[1].includes(needle));
        if (!rows.length) return null;
        return html`<div key=${group.name}><div class="emoji-group">${group.name}</div><div class="emoji-grid">${rows.map((row) => html`
          <button key=${row[0]} type="button" class="emoji-btn" title=${row[1]} onClick=${() => choose(row[0])}>${row[0]}</button>`)}</div></div>`;
      })}
    </div>
  </div>`;
}

// onClear shows a small remove button next to an emoji that is set. Leave it out where an emoji is required.
export function EmojiButton({ value, onChange, onClear, label = 'Pick an emoji' }) {
  const [open, setOpen] = useState(false);
  return html`<span style=${{ position: 'relative', display: 'inline-flex', alignItems: 'center', gap: '2px' }}>
    <button type="button" class="btn" aria-label=${label} aria-expanded=${open} onClick=${() => setOpen(!open)}>
      ${value ? html`<${EmojiView} value=${value} />` : html`<${Icon} name="smile-plus" size=${18} />`}</button>
    ${value && onClear && html`<button type="button" class="icon-btn" aria-label="Remove the emoji" title="Remove the emoji" onClick=${() => { onClear(); setOpen(false); }}><${Icon} name="x" size=${14} /></button>`}
    ${open && html`<${EmojiPicker} onPick=${(v) => { onChange(v); setOpen(false); }} onClose=${() => setOpen(false)} />`}
  </span>`;
}
