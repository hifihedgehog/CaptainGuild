import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { PAGE_INDEX } from '../nav.js';
import { Card, DataTable, confirmDialog, toast, toastError, useGuild, when } from '../ui.js';

const PAGE_SIZE = 200;
const VERBS = { update: 'changed', create: 'created', delete: 'deleted', restore: 'put back', add: 'added', remove: 'removed' };

// A settings page by the name the sidebar gives it, and the module's own key where no page has that name.
export function moduleLabel(key) {
  if (!key) return '';
  const names = { core: 'Bot settings', commands: 'Commands', reactionroles: 'Reaction roles', logs: 'Logging' };
  if (names[key]) return names[key];
  if (PAGE_INDEX[key]) return PAGE_INDEX[key].name;
  return key.charAt(0).toUpperCase() + key.slice(1).replace(/[_.]/g, ' ');
}

// "commands.update" reads "Commands changed". Any other action keeps its words after the name of where it happened.
export function actionLabel(action) {
  const [first, ...rest] = String(action || '').split('.');
  const tail = rest.join(' ');
  if (!tail) return moduleLabel(first) || String(action || '');
  if (VERBS[tail]) return `${moduleLabel(first)} ${VERBS[tail]}`;
  return `${moduleLabel(first)}: ${tail.replace(/_/g, ' ')}`;
}

// Rows within the chosen module and dates. A date covers its whole day, in this browser's time zone.
export function within(rows, { module, from, to }) {
  const start = from ? new Date(from + 'T00:00:00').getTime() / 1000 : null;
  const end = to ? new Date(to + 'T23:59:59').getTime() / 1000 : null;
  return rows.filter((row) => (!module || row.module === module) && (start === null || row.at >= start) && (end === null || row.at <= end));
}

export default function ActivityPage() {
  const guild = useGuild();
  const [rows, setRows] = useState(null);
  const [more, setMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [module, setModule] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const first = () => api.get('/activity').then((list) => { setRows(list); setMore(list.length >= PAGE_SIZE); }).catch((error) => { toastError(error); setRows([]); });
  useEffect(() => {
    let live = true;
    api.get('/activity').then((list) => { if (live) { setRows(list); setMore(list.length >= PAGE_SIZE); } })
      .catch((error) => { if (live) { toastError(error); setRows([]); } });
    return () => { live = false; };
  }, [guild.id]);
  const older = async () => {
    setLoadingMore(true);
    try {
      const list = await api.get(`/activity?before=${rows[rows.length - 1].id}`);
      setRows([...rows, ...list]);
      setMore(list.length >= PAGE_SIZE);
    } catch (error) {
      toastError(error);
    } finally {
      setLoadingMore(false);
    }
  };
  const undo = async (row) => {
    const sure = await confirmDialog({ title: 'Undo this change?', body: `This puts ${moduleLabel(row.module)} back the way it was before "${actionLabel(row.action)}". Any later change to it is replaced too.`, confirm: 'Undo' });
    if (!sure) return;
    try {
      await api.post(`/activity/${row.id}/undo`);
      toast('Put back.');
      first();
    } catch (error) {
      toastError(error);
    }
  };
  const modules = [...new Set((rows || []).map((row) => row.module).filter(Boolean))].sort((a, b) => moduleLabel(a).localeCompare(moduleLabel(b)));
  const shown = within(rows || [], { module, from, to });
  return html`<${Card} icon="history" title="Activity">
    <h2>Who changed what</h2>
    <p class="muted">Every change made from this dashboard or a settings command, newest first. Most settings changes can be undone.</p>
    <div class="row" style=${{ marginBottom: '12px' }}>
      <select aria-label="Show changes to" value=${module} onChange=${(e) => setModule(e.currentTarget.value)} style=${{ width: 'auto' }}>
        <option value="">All settings</option>
        ${modules.map((key) => html`<option key=${key} value=${key} selected=${key === module}>${moduleLabel(key)}</option>`)}
      </select>
      <label class="row small muted" style=${{ gap: '6px' }}>From <input type="date" aria-label="From date" value=${from} max=${to || undefined} onInput=${(e) => setFrom(e.currentTarget.value)} style=${{ width: 'auto' }} /></label>
      <label class="row small muted" style=${{ gap: '6px' }}>To <input type="date" aria-label="To date" value=${to} min=${from || undefined} onInput=${(e) => setTo(e.currentTarget.value)} style=${{ width: 'auto' }} /></label>
      ${(module || from || to) && html`<button class="btn ghost small" onClick=${() => { setModule(''); setFrom(''); setTo(''); }}>Clear</button>`}
    </div>
    <${DataTable} loading=${rows === null} rows=${shown} pageSizes=${[25, 50, 100]} columns=${[
      { key: 'at', label: 'When', sort: true, render: (row) => when(row.at), width: '180px' },
      { key: 'user_name', label: 'Who', sort: true, filter: true, width: '180px' },
      { key: 'action', label: 'What', sort: true, filter: true, text: (row) => actionLabel(row.action) + (row.detail ? ' ' + row.detail : ''),
        render: (row) => html`<span title=${row.action}>${actionLabel(row.action)}</span>${row.detail ? ' ' + row.detail : ''}` },
      { key: 'undo', label: '', width: '80px', render: (row) => (row.undoable && row.module ? html`<button class="btn small outline" onClick=${() => undo(row)}>Undo</button>` : null) },
    ]} />
    ${rows !== null && html`<div class="row" style=${{ marginTop: '12px' }}>
      <span class="muted small">${rows.length} ${rows.length === 1 ? 'change' : 'changes'} loaded${shown.length !== rows.length ? `, ${shown.length} shown` : ''}.</span>
      ${more && html`<button class="btn" disabled=${loadingMore} onClick=${older}>${loadingMore ? 'Loading...' : 'Show older changes'}</button>`}
    </div>`}
  <//>`;
}
