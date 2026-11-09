// The row above every statistics page that picks what it shows: the period, the bucket size, the unit for time, people or bots, and
// every filter. It changes only this view, never the server's settings, and the page keeps it in the address.
import { html, useEffect, useMemo, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { ChannelPicker, EmojiButton, Icon, Modal, Picker, RolePicker, confirmDialog, copyText, toast, toastError } from '../ui.js';
import {
  EMOJI_STATE_LABELS, EMOJI_TYPE_LABELS, EMOJI_USE_LABELS, FAKE_LABELS, PERIOD_LIMITS, STATE_LABELS, changeView, fieldValue, isPublic, listOf, periodText,
  readablePeriod, statsGet,
} from './common.js';

const PERIODS = [['', null], ['24h', 'Last 24 hours'], ['7d', 'Last 7 days'], ['30d', 'Last 30 days'], ['90d', 'Last 90 days'], ['12mo', 'Last 12 months'], ['all', 'All time']];
const INTERVALS = [['hour', 'Hourly'], ['day', 'Daily'], ['week', 'Weekly'], ['month', 'Monthly']];
const UNITS = [['minutes', 'Minutes'], ['hours', 'Hours'], ['days', 'Days']];
const INCLUDE = [['humans', 'People'], ['bots', 'Bots'], ['both', 'People and bots']];
const UNIT_CODES = [['h', 'hours'], ['d', 'days'], ['w', 'weeks'], ['mo', 'months']];

function Popover({ label, button, children, wide = false }) {
  const [open, setOpen] = useState(false);
  const root = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (event) => { if (root.current && !root.current.contains(event.target)) setOpen(false); };
    const key = (event) => { if (event.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('mousedown', away); document.removeEventListener('keydown', key); };
  }, [open]);
  return html`<div class="stats-pop" ref=${root}>
    <button type="button" class="btn small" aria-haspopup="true" aria-expanded=${open} aria-label=${label} onClick=${() => setOpen(!open)}>${button}<${Icon} name="chevron-down" size=${14} /></button>
    ${open && html`<div class=${'stats-pop-body' + (wide ? ' wide' : '')}>${children(() => setOpen(false))}</div>`}
  </div>`;
}

function PeriodMenu({ value, lookback, onPick }) {
  const custom = /^(\d+)(h|d|w|mo)$/.exec(value || '');
  const [amount, setAmount] = useState(custom ? custom[1] : '14');
  const [unit, setUnit] = useState(custom ? custom[2] : 'd');
  const [since, setSince] = useState((value || '').startsWith('since:') ? value.slice(6) : '');
  return html`<${Popover} label="Period" button=${html`<${Icon} name="calendar" size=${14} /> ${periodText(value, lookback)}`}>${(close) => html`
    <div role="listbox" aria-label="Period">
      ${PERIODS.map(([key, text]) => html`<button key=${key} type="button" role="option" aria-selected=${(value || '') === key} class=${'stats-pop-row' + ((value || '') === key ? ' on' : '')}
        onClick=${() => { onPick(key); close(); }}><span class="stats-pop-check">${(value || '') === key ? html`<${Icon} name="check" size=${16} />` : ''}</span>${text || periodText('', lookback)}</button>`)}
    </div>
    <form class="stats-pop-foot" onSubmit=${(event) => {
      event.preventDefault();
      const picked = `${Math.floor(Number(amount))}${unit}`;
      if (!readablePeriod(picked)) { toast(`Pick 1 to ${PERIOD_LIMITS[unit].toLocaleString('en-US')} ${UNIT_CODES.find(([code]) => code === unit)[1]}.`, 'bad'); return; }
      onPick(picked);
      close();
    }}>
      <label class="small muted">Last</label>
      <input type="number" min="1" max=${PERIOD_LIMITS[unit]} aria-label="How many" value=${amount} onInput=${(e) => setAmount(e.currentTarget.value)} />
      <select aria-label="Of what" value=${unit} onChange=${(e) => setUnit(e.currentTarget.value)}>${UNIT_CODES.map(([code, text]) => html`<option key=${code} value=${code}>${text}</option>`)}</select>
      <button type="submit" class="btn small primary">Apply</button>
    </form>
    <form class="stats-pop-foot" onSubmit=${(event) => { event.preventDefault(); if (since) { onPick(`since:${since}`); close(); } }}>
      <label class="small muted">Since</label>
      <input type="date" aria-label="Since" value=${since} onInput=${(e) => setSince(e.currentTarget.value)} />
      <button type="submit" class="btn small primary">Apply</button>
    </form>`}<//>`;
}

function Choice({ label, value, options, onChange }) {
  return html`<select class="stats-choice" aria-label=${label} title=${label} value=${value} onChange=${(e) => onChange(e.currentTarget.value)}>
    ${options.map(([key, text]) => html`<option key=${key} value=${key}>${text}</option>`)}</select>`;
}

function Checks({ names, labels, value, onChange }) {
  const on = new Set(value);
  return html`<div class="stats-checks">${names.map((name) => html`<label key=${name} class="check"><input type="checkbox" checked=${on.has(name)}
    onChange=${(e) => onChange(names.filter((item) => (item === name ? e.currentTarget.checked : on.has(item))))} /><span>${labels[name] || name}</span></label>`)}</div>`;
}

// People to filter by, found by name or ID among everyone the server has statistics for.
export function PeoplePicker({ value, onChange, known = {}, placeholder = 'Search by name or ID', limit = 100 }) {
  const [query, setQuery] = useState('');
  const [found, setFound] = useState([]);
  const names = useRef({ ...known });
  useEffect(() => {
    const text = query.trim();
    if (text.length < 2) { setFound([]); return undefined; }
    let live = true;
    const timer = setTimeout(() => statsGet('/statistics/people', { q: text }).then((data) => {
      if (!live) return;
      data.people.forEach((person) => { names.current[person.id] = person; });
      setFound(data.people);
    }).catch(() => { if (live) setFound([]); }), 250);
    return () => { live = false; clearTimeout(timer); };
  }, [query]);
  const ids = value || [];
  const add = (person) => {
    if (!person.id || ids.includes(String(person.id))) return;
    if (ids.length >= limit) { toast(`A view can name up to ${limit} people.`, 'bad'); return; }
    onChange([...ids, String(person.id)]);
    setQuery('');
    setFound([]);
  };
  return html`<div>
    ${ids.length > 0 && html`<div class="row log-chips">${ids.map((id) => {
      const person = names.current[id] || known[id];
      return html`<span class="chip" key=${id} title=${`ID ${id}`}>${person ? person.display_name || person.name : `ID ${id}`}
        <button type="button" aria-label="Remove" onClick=${() => onChange(ids.filter((other) => other !== id))}><${Icon} name="x" size=${12} /></button></span>`;
    })}</div>`}
    <div class="ms"><input type="text" role="combobox" aria-expanded=${found.length > 0} aria-label=${placeholder} placeholder=${placeholder} value=${query}
      onInput=${(e) => setQuery(e.currentTarget.value)} onKeyDown=${(e) => { if (e.key === 'Enter') { e.preventDefault(); if (found[0]) add(found[0]); } }} />
      ${found.length > 0 && html`<div class="ms-list" role="listbox">${found.map((person) => html`<div key=${person.id} role="option" class="ms-option"
        onMouseDown=${(e) => e.preventDefault()} onClick=${() => add(person)} title=${`ID ${person.id}`}>
        ${person.avatar && html`<img class="avatar" style=${{ width: '22px', height: '22px' }} src=${person.avatar} alt="" />`}${person.display_name}
        <span class="dim">@${person.name}</span>${person.in_server === false && html`<span class="badge outline">left</span>`}</div>`)}</div>`}
    </div>
  </div>`;
}

// Emoji to filter by: this server's own from a list, standard ones from the picker. Each is kept as the key the counts use.
function EmojiChoice({ value, onChange, ctx }) {
  const own = useMemo(() => (ctx.emoji || []).map((row) => ({ value: row.key, label: row.name + (row.deleted ? ' (deleted)' : ''), group: row.kind === 'sticker' ? 'Stickers' : 'Server emoji', image: row.image })), [ctx]);
  const keys = value || [];
  const fromPicker = (text) => {
    const custom = /^<a?:[^:>]+:(\d+)>$/.exec(text);
    const key = custom ? `c:${custom[1]}` : `u:${text.replace(/\uFE0F/g, '')}`;
    if (keys.length >= 25) { toast('A filter holds up to 25 emoji.', 'bad'); return; }
    if (!keys.includes(key)) onChange([...keys, key]);
  };
  const standard = keys.filter((key) => key.startsWith('u:'));
  return html`<div>
    <${Picker} multi options=${own} value=${keys.filter((key) => !key.startsWith('u:'))} placeholder="Server emoji and stickers"
      renderOption=${(o) => html`${o.image && html`<img src=${o.image} alt="" style=${{ width: '18px', height: '18px' }} />`}<span>${o.label}</span>`}
      onChange=${(picked) => onChange([...standard, ...picked].slice(0, 25))} />
    <div class="row" style=${{ marginTop: '6px' }}>
      ${standard.map((key) => html`<span class="chip" key=${key}>${key.slice(2)}<button type="button" aria-label="Remove" onClick=${() => onChange(keys.filter((other) => other !== key))}><${Icon} name="x" size=${12} /></button></span>`)}
      <${EmojiButton} label="Add a standard emoji" value=${null} onChange=${fromPicker} />
    </div>
  </div>`;
}

function Mode({ value, onChange, block = 'Leave these out', allow = 'Only these', same }) {
  const options = [...(same ? [['same', same]] : []), ['block', block], ['allow', allow]];
  return html`<div class="stats-mode" role="radiogroup">${options.map(([key, text]) => html`<label key=${key} class="check"><input type="radio" checked=${value === key}
    onChange=${() => onChange(key)} /><span>${text}</span></label>`)}</div>`;
}

const FILTER_FIELDS = ['voice_states', 'channel_mode', 'channels', 'voice_mode', 'voice_channels', 'member_mode', 'members', 'roles_allow', 'roles_block', 'activity_mode',
  'activities', 'invite_mode', 'invites', 'fake_mode', 'fake_match', 'fake_reasons', 'emoji_mode', 'emoji', 'emoji_states', 'emoji_type_mode', 'emoji_type_match',
  'emoji_types', 'emoji_uses'];

function FilterDialog({ ctx, view, onApply, onClose }) {
  const start = () => Object.fromEntries(FILTER_FIELDS.map((name) => {
    const value = fieldValue(view, ctx.defaults, name);
    return [name, ['voice_states', 'channels', 'voice_channels', 'members', 'roles_allow', 'roles_block', 'activities', 'invites', 'fake_reasons', 'emoji', 'emoji_states',
      'emoji_types', 'emoji_uses'].includes(name) ? listOf(value) : value];
  }));
  const [draft, setDraft] = useState(start);
  const [first] = useState(() => JSON.stringify(start()));
  const set = (name, value) => setDraft({ ...draft, [name]: value });
  const bind = (name) => ({ value: draft[name], onChange: (value) => set(name, value) });
  const open = !isPublic();
  const activities = (ctx.activities || []).map((row) => ({ value: String(row.id), label: row.name, note: row.verified ? '' : '(not verified)' }));
  const invites = (ctx.invites || []).map((row) => ({ value: row.code, label: row.label ? `${row.label} (${row.code})` : row.code, note: row.deleted ? '(deleted)' : '' }));
  return html`<${Modal} large title="Filters" icon="filter" onClose=${onClose} dirty=${JSON.stringify(draft) !== first} footer=${html`
    <button type="button" class="btn ghost" onClick=${() => setDraft(Object.fromEntries(FILTER_FIELDS.map((name) => [name, Array.isArray(draft[name]) ? listOf(ctx.defaults[name]) : ctx.defaults[name]])))}>Use the server's filters</button>
    <div class="right"><button type="button" class="btn ghost" onClick=${onClose}>Cancel</button>
      <button type="button" class="btn primary" onClick=${() => onApply(draft)}>Apply</button></div>`}>
    <p class="muted">These change what this view shows and nothing else. Statistics settings hold what every view starts from.</p>
    <div class="stats-filter-grid">
      <section><h4>Channels</h4>
        <${Mode} ...${bind('channel_mode')} />
        <${ChannelPicker} multi categories types=${[0, 2, 5, 13, 15]} placeholder="Channels and categories" ...${bind('channels')} />
        <h4>Voice channels</h4>
        <${Mode} same="Use the channel list" ...${bind('voice_mode')} />
        ${draft.voice_mode !== 'same' && html`<${ChannelPicker} multi categories types=${[2, 13]} placeholder="Voice channels and categories" ...${bind('voice_channels')} />`}
      </section>
      <section><h4>Voice states</h4><p class="small muted">Each minute in voice counts under one state.</p>
        <${Checks} names=${Object.keys(STATE_LABELS)} labels=${STATE_LABELS} ...${bind('voice_states')} /></section>
      ${open && ctx.can_people && html`<section><h4>Members</h4><${Mode} ...${bind('member_mode')} /><${PeoplePicker} ...${bind('members')} /></section>`}
      ${open && html`<section><h4>Roles</h4><p class="small muted">Roles read as people hold them now, so someone who left drops out of a role filter.</p>
        <${RolePicker} multi label="Only people with one of these roles" ...${bind('roles_allow')} />
        <${RolePicker} multi label="Leave out people with any of these roles" ...${bind('roles_block')} /></section>`}
      <section><h4>Activities</h4><${Mode} ...${bind('activity_mode')} /><${Picker} multi options=${activities} placeholder="Games and apps" ...${bind('activities')} /></section>
      ${open && html`<section><h4>Invites</h4><${Mode} ...${bind('invite_mode')} /><${Picker} multi options=${invites} placeholder="Invites" ...${bind('invites')} />
        <h4>Fake joins</h4>
        <${Mode} block="Leave out joins with" allow="Only joins with" ...${bind('fake_mode')} />
        <div class="stats-mode" role="radiogroup">${[['any', 'any of these'], ['all', 'all of these']].map(([key, text]) => html`<label key=${key} class="check"><input type="radio"
          checked=${draft.fake_match === key} onChange=${() => set('fake_match', key)} /><span>${text}</span></label>`)}</div>
        <${Checks} names=${ctx.fake_reasons} labels=${FAKE_LABELS} ...${bind('fake_reasons')} /></section>`}
      <section><h4>Emoji</h4><${Mode} ...${bind('emoji_mode')} /><${EmojiChoice} ctx=${ctx} ...${bind('emoji')} />
        <h4>Emoji states</h4><${Checks} names=${Object.keys(EMOJI_STATE_LABELS)} labels=${EMOJI_STATE_LABELS} ...${bind('emoji_states')} />
        <h4>Emoji types</h4>
        <${Mode} block="Leave out" allow="Only" ...${bind('emoji_type_mode')} />
        <div class="stats-mode" role="radiogroup">${[['any', 'any of these'], ['all', 'all of these']].map(([key, text]) => html`<label key=${key} class="check"><input type="radio"
          checked=${draft.emoji_type_match === key} onChange=${() => set('emoji_type_match', key)} /><span>${text}</span></label>`)}</div>
        <${Checks} names=${Object.keys(EMOJI_TYPE_LABELS)} labels=${EMOJI_TYPE_LABELS} ...${bind('emoji_types')} />
        <h4>Uses that count</h4><${Checks} names=${Object.keys(EMOJI_USE_LABELS)} labels=${EMOJI_USE_LABELS} ...${bind('emoji_uses')} /></section>
    </div>
  <//>`;
}

function changedFilters(view, defaults) {
  return FILTER_FIELDS.filter((name) => view[name] !== undefined).length;
}

// Named presets: whoever changes the query bar or holds a role for loading them loads them, and Manage Server or a presets role saves and
// deletes them. Drilldown presets keep the members or channels picked, and are listed apart.
function Presets({ kind, view, picks, onLoad }) {
  const [data, setData] = useState(null);
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState('');
  const load = () => statsGet('/statistics/presets').then(setData).catch(() => setData({ filters: [], members: [], channels: [], can_edit: false }));
  useEffect(() => { load(); }, []);
  const rows = data ? data[kind] || [] : [];
  const save = async () => {
    try {
      await api.post('/statistics/presets', { name, kind, query: view, picks: picks || [] });
      toast('Preset saved.');
      setSaving(false);
      setName('');
      load();
    } catch (error) { toastError(error); }
  };
  const remove = async (preset) => {
    if (!(await confirmDialog({ title: `Delete ${preset.name}?`, body: 'Anyone who loads presets here loses it too.', confirm: 'Delete', danger: true }))) return;
    try { await api.del(`/statistics/presets/${preset.id}`); load(); } catch (error) { toastError(error); }
  };
  // Presets travel as a file in Captain Guild's own format, so another server can take them in.
  const file = useRef(null);
  const exportAll = async () => {
    try {
      const found = await statsGet('/statistics/presets/export');
      const link = document.createElement('a');
      link.href = URL.createObjectURL(new Blob([JSON.stringify(found, null, 2)], { type: 'application/json' }));
      link.download = 'statistics-presets.json';
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 2000);
    } catch (error) { toastError(error); }
  };
  const importFile = async (event) => {
    const picked = event.currentTarget.files[0];
    event.currentTarget.value = '';
    if (!picked) return;
    let list;
    try {
      const parsed = JSON.parse(await picked.text());
      list = Array.isArray(parsed) ? parsed : parsed.presets;
    } catch (error) { list = null; }
    if (!Array.isArray(list)) { toast('That file holds no presets.', 'bad'); return; }
    try {
      const done = await api.post('/statistics/presets/import', { presets: list });
      const kept = done.presets.filter((row) => !row.error).length;
      toast(kept === done.presets.length ? `Imported ${kept} preset${kept === 1 ? '' : 's'}.` : `Imported ${kept} of ${done.presets.length} presets.`, kept ? 'ok' : 'bad');
      load();
    } catch (error) { toastError(error); }
  };
  const title = kind === 'filters' ? 'Presets' : kind === 'members' ? 'Member presets' : 'Channel presets';
  return html`<${Popover} label=${title} wide button=${html`<${Icon} name="save" size=${14} /> ${title}`}>${(close) => html`
    ${rows.length === 0 && html`<div class="ms-empty">No presets yet.</div>`}
    ${rows.map((preset) => html`<div key=${preset.id} class="stats-pop-row stats-preset">
      <button type="button" class="link-btn" onClick=${() => { onLoad(preset); close(); }}>${preset.name}</button>
      ${preset.picks && preset.picks.length > 0 && html`<span class="dim small">${preset.picks.length} picked</span>`}
      ${data.can_edit && html`<button type="button" class="icon-btn danger right" aria-label=${'Delete ' + preset.name} onClick=${() => remove(preset)}><${Icon} name="trash-2" size=${14} /></button>`}
    </div>`)}
    ${data && data.can_edit && html`<div class="stats-pop-foot">${saving ? html`<form class="row" onSubmit=${(event) => { event.preventDefault(); save(); }}>
        <input type="text" maxlength="19" aria-label="Preset name" placeholder="Name, under 20 characters" value=${name} onInput=${(e) => setName(e.currentTarget.value)} />
        <button type="submit" class="btn small primary" disabled=${!name.trim()}>Save</button></form>`
      : html`<button type="button" class="btn small" onClick=${() => setSaving(true)}><${Icon} name="plus" size=${14} /> Save this view as...</button>`}</div>`}
    ${data && html`<div class="stats-pop-foot row">
      <button type="button" class="btn small ghost" onClick=${exportAll}><${Icon} name="download" size=${14} /> Export</button>
      ${data.can_edit && html`<button type="button" class="btn small ghost" onClick=${() => file.current.click()}><${Icon} name="upload" size=${14} /> Import</button>
        <input ref=${file} class="hidden" type="file" accept="application/json,.json" aria-label="Import presets" onChange=${importFile} />`}</div>`}
  `}<//>`;
}

export function QueryBar({ ctx, view, setView, presetKind = 'filters', picks, onPreset, showUnit = true }) {
  const [filtering, setFiltering] = useState(false);
  const pick = (name) => (value) => setView(changeView(view, ctx.defaults, { [name]: value }));
  if (!ctx.can_filter) {
    return html`<div class="stats-bar"><span class="badge outline"><${Icon} name="calendar" size=${12} /> ${periodText(view.preset ? view.period || '' : '', ctx.lookback)}</span>
      <span class="muted small">${view.preset ? 'A preset this server saved.' : 'This server shows everyone its own view of these numbers.'}</span>
      ${ctx.can_read_presets && !isPublic() && html`<span class="stats-bar-right">
        ${view.preset && html`<button type="button" class="btn small ghost" onClick=${() => setView({})} title="Back to what the server's settings show">Reset</button>`}
        <${Presets} kind=${presetKind} view=${view} picks=${picks} onLoad=${onPreset} /></span>`}</div>`;
  }
  const filters = changedFilters(view, ctx.defaults);
  return html`<div class="stats-bar" role="toolbar" aria-label="What these statistics show">
    <${PeriodMenu} value=${view.period || ''} lookback=${ctx.lookback} onPick=${pick('period')} />
    <${Choice} label="Bucket size" value=${fieldValue(view, ctx.defaults, 'interval')} options=${INTERVALS} onChange=${pick('interval')} />
    ${showUnit && html`<${Choice} label="Show time in" value=${fieldValue(view, ctx.defaults, 'unit')} options=${UNITS} onChange=${pick('unit')} />`}
    <${Choice} label="Count" value=${fieldValue(view, ctx.defaults, 'include')} options=${INCLUDE} onChange=${pick('include')} />
    <button type="button" class=${'btn small' + (filters ? ' primary' : '')} onClick=${() => setFiltering(true)}><${Icon} name="filter" size=${14} /> Filters${filters ? ` (${filters})` : ''}</button>
    ${Object.keys(view).length > 0 && html`<button type="button" class="btn small ghost" onClick=${() => setView({})} title="Back to what the server's settings show">Reset</button>`}
    <span class="stats-bar-right">
      ${!isPublic() && html`<${Presets} kind=${presetKind} view=${view} picks=${picks} onLoad=${onPreset} />`}
      <button type="button" class="btn small ghost" onClick=${() => copyText(location.href)} title="Copy a link to this exact view"><${Icon} name="link" size=${14} /> Copy link</button>
    </span>
    ${filtering && html`<${FilterDialog} ctx=${ctx} view=${view} onClose=${() => setFiltering(false)} onApply=${(draft) => { setView(changeView(view, ctx.defaults, draft)); setFiltering(false); }} />`}
  </div>`;
}
