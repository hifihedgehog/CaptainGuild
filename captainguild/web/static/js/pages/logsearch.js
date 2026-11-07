import { html, useEffect, useMemo, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { EmbedPreview } from '../embed.js';
import { Card, ChannelPicker, Empty, Icon, Loading, Modal, Notice, Pager, Picker, copyText, sameId, toastError, useGuild, when } from '../ui.js';

const SIZES = [10, 25, 50, 100];
const CHANNEL_TYPES = [0, 2, 5, 13, 15, 16];

// Epoch seconds from a date box, in the viewer's own time zone. "to" takes in that whole day.
function dayStart(value) {
  return value ? String(new Date(`${value}T00:00:00`).getTime() / 1000) : '';
}
function dayEnd(value) {
  return value ? String(new Date(`${value}T23:59:59`).getTime() / 1000) : '';
}

function fromAddress() {
  const params = new URLSearchParams(location.search);
  return {
    user: params.get('user') || '',
    channel: params.get('channel') || '',
    events: (params.get('event') || '').split(',').filter(Boolean),
    from: params.get('from') || '',
    to: params.get('to') || '',
    text: params.get('q') || '',
  };
}

function queryFor(filters) {
  const params = new URLSearchParams();
  if (filters.user) params.set('user', filters.user);
  if (filters.channel) params.set('channel', filters.channel);
  if (filters.events.length) params.set('event', filters.events.join(','));
  if (filters.from) params.set('from', dayStart(filters.from));
  if (filters.to) params.set('to', dayEnd(filters.to));
  if (filters.text.trim()) params.set('q', filters.text.trim());
  return params;
}

// A name to search for, or a person's ID, including someone who has left and can't be searched by name.
function PersonFilter({ value, label, onChange }) {
  const [typed, setTyped] = useState('');
  const [results, setResults] = useState([]);
  useEffect(() => {
    const query = typed.trim();
    if (query.length < 2 || /^\d+$/.test(query)) { setResults([]); return undefined; }
    const timer = setTimeout(() => api.get(`/members/search?q=${encodeURIComponent(query)}`).then(setResults).catch(() => setResults([])), 250);
    return () => clearTimeout(timer);
  }, [typed]);
  if (value) {
    return html`<span class="chip log-filter-chip" title=${value}><span class="log-ellipsis">${label || value}</span>
      <button type="button" aria-label="Clear the member filter" onClick=${() => { onChange(''); setTyped(''); }}><${Icon} name="x" size=${12} /></button></span>`;
  }
  const commit = () => { if (/^\d{15,21}$/.test(typed.trim())) { onChange(typed.trim()); setResults([]); } };
  return html`<div class="ms"><input type="text" placeholder="Name or ID" aria-label="Filter by member" value=${typed} onInput=${(e) => setTyped(e.currentTarget.value)}
      onKeyDown=${(e) => { if (e.key === 'Enter') commit(); }} onBlur=${commit} />
    ${results.length > 0 && html`<div class="ms-list log-person-list">${results.map((m) => html`<div key=${String(m.id)} class="ms-option"
        onMouseDown=${(e) => e.preventDefault()} onClick=${() => { onChange(String(m.id), m.display_name); setResults([]); setTyped(''); }}>
      <img class="avatar log-chip-avatar" src=${m.avatar} alt="" />${m.display_name}</div>`)}</div>`}
  </div>`;
}

function DetailDialog({ entry, onClose }) {
  const [row, setRow] = useState(null);
  useEffect(() => {
    api.get(`/logs/entries/${entry.id}`).then(setRow).catch((error) => { toastError(error); onClose(); });
  }, [entry.id]);
  return html`<${Modal} large title="Full text" icon="file-text" onClose=${onClose} footer=${html`<div class="right">
      <button class="btn" disabled=${!row} onClick=${() => copyText(row.detail)}><${Icon} name="copy" /> Copy</button>
      <button class="btn primary" onClick=${onClose}>Close</button></div>`}>
    ${row ? html`<pre class="log-detail">${row.detail}</pre>` : html`<${Loading} />`}
  <//>`;
}

export default function LogSearchPage() {
  const guild = useGuild();
  const [filters, setFilters] = useState(fromAddress);
  const [typed, setTyped] = useState(filters.text);
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(25);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [failure, setFailure] = useState(null);
  const [catalog, setCatalog] = useState(null);
  const [detail, setDetail] = useState(null);
  const [personLabel, setPersonLabel] = useState('');
  const asked = useRef(0);

  useEffect(() => { api.get('/logging/catalog').then(setCatalog).catch(toastError); }, [guild.id]);

  // A typed search waits for a pause, so it doesn't ask the server for every letter.
  useEffect(() => {
    const timer = setTimeout(() => { setFilters((current) => (current.text === typed ? current : { ...current, text: typed })); setPage(1); }, 350);
    return () => clearTimeout(timer);
  }, [typed]);

  const params = useMemo(() => queryFor(filters), [filters]);
  const queryText = params.toString();

  useEffect(() => {
    const number = ++asked.current;
    setLoading(true);
    const paged = new URLSearchParams(params);
    paged.set('page', String(page));
    paged.set('size', String(size));
    api.get(`/logs/search?${paged}`).then((answer) => {
      if (number !== asked.current) return;
      setData(answer);
      setFailure(null);
      setLoading(false);
    }).catch((error) => {
      if (number !== asked.current) return;
      setFailure(error.message);
      setLoading(false);
    });
    // The filters stay in the address, so a search can be shared and survives a reload.
    const kept = new URLSearchParams();
    if (filters.user) kept.set('user', filters.user);
    if (filters.channel) kept.set('channel', filters.channel);
    if (filters.events.length) kept.set('event', filters.events.join(','));
    if (filters.from) kept.set('from', filters.from);
    if (filters.to) kept.set('to', filters.to);
    if (filters.text.trim()) kept.set('q', filters.text.trim());
    const tail = kept.toString();
    history.replaceState(null, '', location.pathname + (tail ? `?${tail}` : ''));
  }, [queryText, page, size, guild.id]);

  const change = (key, value) => { setFilters((current) => ({ ...current, [key]: value })); setPage(1); };
  const pickPerson = (id, name) => { setPersonLabel(name || ''); change('user', id); };
  const clear = () => { setFilters({ user: '', channel: '', events: [], from: '', to: '', text: '' }); setTyped(''); setPage(1); };
  const filtered = filters.user || filters.channel || filters.events.length || filters.from || filters.to || filters.text.trim();
  const rows = data ? data.rows : [];
  const start = data && rows.length ? (data.page - 1) * data.size + 1 : 0;
  const end = data ? start + Math.max(rows.length - 1, 0) : 0;
  const eventOptions = useMemo(() => (catalog ? catalog.groups.flatMap((g) => g.events.map((e) => ({ value: e.key, label: e.label, group: g.title }))) : []), [catalog]);
  const exportLink = (kind) => {
    const query = new URLSearchParams(params);
    query.set('format', kind);
    return `/api/g/${guild.id}/logs/export?${query}`;
  };
  const personName = (row) => row.user_name || (row.embed.author && row.embed.author.name) || row.user_id;
  // A thread isn't in the server's channel list, so it goes by its parent, and picking it filters to the parent and all its threads.
  const named = (id) => guild.channels.find((c) => sameId(c.id, id));
  const channelLabel = (row) => {
    const channel = named(row.channel_id);
    if (channel) return `#${channel.name}`;
    const parent = row.parent_id && named(row.parent_id);
    return parent ? `#${parent.name} thread` : 'Thread or deleted channel';
  };

  return html`<${Card} icon="search" title="Log search" right=${html`
      <a class="btn small" href=${exportLink('csv')} download><${Icon} name="download" size=${14} /> CSV</a>
      <a class="btn small" href=${exportLink('json')} download><${Icon} name="download" size=${14} /> JSON</a>`}>
    <h2>Log search</h2>
    <p class="muted">Every log entry from the last ${data ? data.retention_days : '...'} days, shown as it looks in Discord. Filtering by a member finds entries about them and changes they made.</p>
    ${failure && html`<${Notice} kind="bad">${failure}<//>`}
    <div class="table-tools">
      <span>Records:</span>
      <select aria-label="Records per page" value=${size} onChange=${(e) => { setSize(Number(e.currentTarget.value)); setPage(1); }}>
        ${SIZES.map((n) => html`<option key=${n} value=${n}>${n}</option>`)}</select>
      ${filtered && html`<button type="button" class="btn ghost small" onClick=${clear}><${Icon} name="x" size=${14} /> Clear filters</button>`}
    </div>
    <div class="table-wrap"><table class="log-table">
      <thead>
        <tr><th class="log-col-when">When</th><th class="log-col-event">Event</th><th class="log-col-person">Member</th><th class="log-col-channel">Channel</th><th>Entry</th></tr>
        <tr class="log-filters">
          <th><input type="date" aria-label="From date" title="From" value=${filters.from} onInput=${(e) => change('from', e.currentTarget.value)} />
            <input type="date" aria-label="To date" title="To" value=${filters.to} onInput=${(e) => change('to', e.currentTarget.value)} /></th>
          <th><${Picker} multi options=${eventOptions} placeholder="Any event" value=${filters.events} onChange=${(value) => change('events', value)} /></th>
          <th><${PersonFilter} value=${filters.user} label=${personLabel} onChange=${pickPerson} /></th>
          <th><${ChannelPicker} types=${CHANNEL_TYPES} categories=${false} placeholder="Any channel" value=${filters.channel || null} onChange=${(value) => change('channel', value ? String(value) : '')} /></th>
          <th><input type="search" placeholder="Search the text" aria-label="Search the text" value=${typed} onInput=${(e) => setTyped(e.currentTarget.value)} /></th>
        </tr>
      </thead>
      <tbody>
        ${!data && loading && html`<tr><td colspan="5"><${Loading} /></td></tr>`}
        ${data && rows.length === 0 && html`<tr><td colspan="5"><${Empty}>No matching records<//></td></tr>`}
        ${rows.map((row) => html`<tr key=${row.id}>
          <td class="small">${when(row.at)}</td>
          <td><span class="badge outline">${row.label}</span></td>
          <td class="log-person">
            ${row.user_id ? html`<button type="button" class="log-link" title="Show only this member" onClick=${() => pickPerson(String(row.user_id), personName(row))}>${personName(row)}</button>` : html`<span class="dim">None</span>`}
            ${row.actor_id && String(row.actor_id) !== String(row.user_id) && html`<div class="small dim">by <button type="button" class="log-link" title="Show only this member" onClick=${() => pickPerson(String(row.actor_id), row.actor_name)}>${row.actor_name || row.actor_id}</button></div>`}
          </td>
          <td>${row.channel_id ? html`<button type="button" class="log-link" title="Show only this channel" onClick=${() => change('channel', String(row.parent_id || row.channel_id))}>${channelLabel(row)}</button>` : html`<span class="dim">None</span>`}</td>
          <td class="log-entry"><div class="dc log-dc"><${EmbedPreview} embed=${row.embed} guild=${guild} /></div>
            ${row.has_detail && html`<button type="button" class="btn small log-detail-btn" onClick=${() => setDetail(row)}><${Icon} name="file-text" size=${14} /> Show full text</button>`}
          </td>
        </tr>`)}
      </tbody>
    </table></div>
    <div class="table-foot">
      <span class="muted small">${data ? `Showing ${start} to ${end} of ${data.total.toLocaleString()}${data.capped ? '+' : ''} records` : ''}</span>
      ${data && html`<${Pager} page=${data.page} pages=${data.pages} onPage=${setPage} />`}
    </div>
    ${detail && html`<${DetailDialog} entry=${detail} onClose=${() => setDetail(null)} />`}
  <//>`;
}
