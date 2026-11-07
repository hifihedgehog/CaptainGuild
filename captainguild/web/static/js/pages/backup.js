import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { PAGE_INDEX } from '../nav.js';
import { Card, Check, Col, DataTable, Empty, Grid, Icon, Loading, Modal, Notice, NumberInput, SaveBar, SaveButton, Select, SettingsGate, Switch, confirmDialog, toast, toastError, useGuild, useSettings, when } from '../ui.js';

const INTERVALS = [
  { value: 6, label: 'Every 6 hours' }, { value: 12, label: 'Every 12 hours' }, { value: 24, label: 'Every day' },
  { value: 48, label: 'Every 2 days' }, { value: 72, label: 'Every 3 days' }, { value: 168, label: 'Every week' },
];
const MODULE_PAGES = { core: 'botsettings', logging: 'logging' };
const KIND_NAMES = { scheduled: 'Scheduled', manual: 'By hand', 'before import': 'Before a restore' };
const LARGE_EXPORT_ROWS = 200000;

function fileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let index = -1;
  do { value /= 1024; index += 1; } while (value >= 1024 && index < units.length - 1);
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`;
}

function moduleName(key) {
  const page = PAGE_INDEX[MODULE_PAGES[key] || key];
  if (page) return page.name;
  const words = String(key).replace(/_/g, ' ');
  return words[0].toUpperCase() + words.slice(1);
}

const tableName = (name) => name.replace(/_/g, ' ');
const count = (n) => Number(n || 0).toLocaleString();

function JobProgress({ job, onStop }) {
  if (!job || !job.running) return null;
  const share = job.total > 0 ? Math.min(100, Math.round((job.done / job.total) * 100)) : null;
  return html`<div class="backup-job" role="status">
    <div class="spread"><b>${job.stage || 'Working'}</b><span class="dim small">${share === null ? '' : `${share}%`}
      ${job.kind === 'preview' && onStop && html` <button class="btn small ghost" onClick=${onStop}>Stop</button>`}</span></div>
    <div class="progress"><span class=${share === null ? 'waiting' : ''} style=${{ width: `${share === null ? 100 : share}%` }}></span></div>
  </div>`;
}

function ExportCard({ data, busy, onExport }) {
  const [messages, setMessages] = useState(true);
  const [chosenPacked, setPacked] = useState(null);
  const main = data ? data.databases.main : null;
  const logs = data ? data.databases.messages : null;
  // A web server in front of the bot can cut off a very large download, and compressing keeps a big file well under that.
  const large = !!data && main.rows + (messages ? logs.rows : 0) >= LARGE_EXPORT_ROWS;
  const packed = chosenPacked === null ? large : chosenPacked;
  const denied = !!data && data.admin === false;
  return html`<${Card} icon="download" title="Export" footer=${html`<button class="btn success" disabled=${busy || denied} onClick=${() => onExport(messages, packed)}>
      <${Icon} name="download" /> Download export</button>`}>
    <h2>Export everything</h2>
    <p class="muted">One JSON file with every setting and every table this bot stores for this server. Keep it private: it holds member names and messages. API keys in it are sealed, so only this install can read them.</p>
    ${main && html`<p class="muted small">Settings and features: ${count(main.rows)} rows in ${count(main.tables)} tables. Messages and logs: ${count(logs.rows)} rows in ${count(logs.tables)} tables.</p>`}
    <${Switch} label="Include messages and logs" help="The message store and log search history. These are usually the largest part of the file." value=${messages} onChange=${setMessages} />
    <${Switch} label="Compress the file with gzip" help="Makes a much smaller .json.gz file. Import reads either kind." value=${packed} onChange=${setPacked} />
    ${data && data.left_out.length > 0 && html`<${Notice} kind="warn">These tables can't be tied to one server, so they stay out of the file: ${data.left_out.join(', ')}.<//>`}
  <//>`;
}

function ImportCard({ busy, denied, onUpload }) {
  const input = useRef(null);
  const choose = (event) => {
    const file = event.currentTarget.files[0];
    event.currentTarget.value = '';
    if (file) onUpload(file);
  };
  return html`<${Card} icon="upload" title="Import" footer=${html`<button class="btn primary" disabled=${busy || denied} onClick=${() => input.current.click()}><${Icon} name="upload" /> Choose a file</button>`}>
    <h2>Import a file</h2>
    <p class="muted">Choose a file exported from this server. Nothing changes until you've seen what the file would do and picked what to bring back.</p>
    <input ref=${input} class="hidden" type="file" accept=".json,.gz,application/json,application/gzip" aria-label="Backup file" onChange=${choose} />
  <//>`;
}

function ScheduleCard({ form }) {
  return html`<${Card} icon="clock" title="Automatic snapshots" footer=${html`<${SaveButton} form=${form} />`}>
    <h2>Automatic snapshots</h2>
    <p class="muted">Copies of this server's data, kept encrypted on the bot's own machine, ready to restore below.</p>
    <${Switch} label="Take snapshots on a schedule" ...${form.bind('enabled')} />
    <${Select} label="How often" options=${INTERVALS} disabled=${!form.data.enabled} ...${form.bind('interval_hours')} />
    <${NumberInput} label="How many to keep" min=${1} max=${100} disabled=${!form.data.enabled} help="The oldest scheduled snapshot goes when a new one would pass this number." ...${form.bind('keep')} />
    <${Switch} label="Include messages and logs" help="Off by default, because these snapshots can get very large. The message store already keeps its own history." ...${form.bind('include_messages')} />
  <//>`;
}

function SnapshotsCard({ data, busy, onTake, onPreview, onDelete }) {
  const guild = useGuild();
  const denied = !!data && data.admin === false;
  return html`<${Card} icon="database" title="Snapshots" right=${data && html`<span class="muted small">${fileSize(data.disk_bytes)} on disk</span>`}>
    <div class="spread" style=${{ marginBottom: '12px' }}>
      <h2 style=${{ margin: 0 }}>Snapshots</h2>
      <button class="btn success" disabled=${busy || denied} onClick=${onTake}><${Icon} name="plus" /> Take a snapshot now</button>
    </div>
    <${DataTable} loading=${!data} rows=${data ? data.snapshots : []} empty="No snapshots yet." columns=${[
      { key: 'created_at', label: 'When', sort: true, render: (row) => when(row.created_at) },
      { key: 'kind', label: 'Kind', sort: true, filter: true, text: (row) => KIND_NAMES[row.kind] || row.kind, render: (row) => html`${KIND_NAMES[row.kind] || row.kind}${row.created_by ? html`<span class="dim small"> ${row.created_by}</span>` : ''}` },
      { key: 'row_count', label: 'Rows', sort: true, render: (row) => html`${count(row.row_count)}${row.includes_messages ? html`<span class="dim small"> with messages</span>` : ''}` },
      { key: 'size', label: 'Size', sort: true, render: (row) => fileSize(row.size) },
      { key: 'actions', label: '', width: '250px', render: (row) => html`<div class="row">
        <button class="btn small outline" disabled=${busy || denied} onClick=${() => onPreview(row)}>Restore</button>
        ${!denied && html`<a class="btn small" href=${`/api/g/${guild.id}/backup/snapshots/${row.id}/download`} download="">Download</a>`}
        <button class="btn small outline danger" disabled=${busy || denied} onClick=${() => onDelete(row)}>Delete</button>
      </div>` },
    ]} />
  <//>`;
}

function hasChange(row) {
  if (!row.compared) return row.in_file > 0;
  if (row.mode === 'merge') return row.added > 0;
  return row.added > 0 || row.changed > 0 || row.removed > 0;
}

function describe(row) {
  const dropped = row.dropped + row.foreign;
  const tail = dropped ? `. ${count(dropped)} rows in the file were refused` : '';
  if (!row.compared) return `Too many rows to compare one by one. ${row.mode === 'merge' ? 'Rows missing here get added' : 'These rows replace what is here'}${tail}.`;
  if (row.mode === 'merge') return `${count(row.added)} missing rows get added. Nothing is removed${tail}.`;
  const parts = [];
  if (row.added) parts.push(`${count(row.added)} come back`);
  if (row.changed) parts.push(`${count(row.changed)} change`);
  if (row.removed) parts.push(`${count(row.removed)} go away`);
  return `${parts.join(', ') || 'No rows change'}${tail}.`;
}

function PreviewModal({ preview, busy, job, onClose, onRestore }) {
  const settings = preview.settings.filter((row) => row.changes.length > 0);
  const tables = preview.tables.filter(hasChange);
  const unchanged = preview.settings.length - settings.length + preview.tables.length - tables.length;
  const [picked, setPicked] = useState(() => new Set([...settings.map((row) => row.key), ...tables.map((row) => row.key)]));
  const [open, setOpen] = useState(null);
  const members = (row) => (row.group ? preview.tables.filter((t) => t.group === row.group).map((t) => t.key) : [row.key]);
  const flip = (row, on) => {
    const next = new Set(picked);
    for (const key of members(row)) { if (on) next.add(key); else next.delete(key); }
    setPicked(next);
  };
  const file = preview.file;
  const nothing = settings.length === 0 && tables.length === 0;
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([...picked].sort());
  const [start] = useState(snapshot);
  return html`<${Modal} large icon="upload" title=${preview.label} onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn danger" disabled=${busy || picked.size === 0} onClick=${() => onRestore([...picked])}>Restore ${picked.size} selected</button>
    </div>`}>
    <p class="muted">Made ${when(file.created_at)} from ${file.guild_name || 'this server'}${file.app_version ? ` on version ${file.app_version}` : ''}. ${file.includes_messages ? 'It holds messages and logs.' : 'It leaves out messages and logs.'}</p>
    <${Notice}>A snapshot of the current state is taken first. Settings and feature tables can go back with that snapshot. Scheduled jobs, logs and messages keep what they have now and gain what's missing, and what they gain stays.<//>
    ${preview.warnings.map((text, index) => html`<${Notice} key=${index} kind="warn">${text}<//>`)}
    <${JobProgress} job=${job} />
    ${nothing && html`<${Empty}>This file holds nothing that differs from what's here now.<//>`}
    ${settings.length > 0 && html`<h3>Settings</h3>
      <div class="table-wrap" style=${{ marginBottom: '16px' }}><table><tbody>${settings.map((row) => html`<tr key=${row.key}>
        <td style=${{ width: '30%' }}><${Check} label=${moduleName(row.module)} value=${picked.has(row.key)} onChange=${(on) => flip(row, on)} /></td>
        <td>
          <button class="btn small ghost" aria-expanded=${open === row.key} onClick=${() => setOpen(open === row.key ? null : row.key)}>
            <${Icon} name=${open === row.key ? 'chevron-down' : 'chevron-right'} size=${14} /> ${row.changes.length} setting${row.changes.length === 1 ? '' : 's'} change</button>
          ${open === row.key && html`<div class="backup-changes">${row.changes.map((change) => html`<div key=${change.field}><b>${change.field}</b>
            <div class="mono small"><span class="dim">now </span>${change.before}</div>
            <div class="mono small"><span class="dim">file </span>${change.after}</div></div>`)}</div>`}
        </td></tr>`)}</tbody></table></div>`}
    ${tables.length > 0 && html`<h3>Data</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>Table</th><th>Feature</th><th>Here now</th><th>In the file</th><th>What happens</th></tr></thead>
        <tbody>${tables.map((row) => html`<tr key=${row.key}>
          <td><${Check} label=${tableName(row.table)} value=${picked.has(row.key)} onChange=${(on) => flip(row, on)} /></td>
          <td>${moduleName(row.owner)}</td><td>${count(row.now)}</td><td>${count(row.in_file)}</td><td>${describe(row)}</td></tr>`)}</tbody>
      </table></div>`}
    ${unchanged > 0 && html`<p class="muted small" style=${{ marginTop: '10px' }}>${unchanged} other part${unchanged === 1 ? '' : 's'} of the file match what's here now.</p>`}
  <//>`;
}

function ReportModal({ report, onClose }) {
  const tables = Object.entries(report.tables);
  const added = tables.reduce((sum, [, row]) => sum + row.added, 0);
  const dropped = tables.reduce((sum, [, row]) => sum + row.dropped, 0);
  const moved = tables.reduce((sum, [, row]) => sum + row.renumbered, 0);
  return html`<${Modal} icon="circle-check" title="Restore finished" onClose=${onClose} footer=${html`<div class="right"><button class="btn primary" onClick=${onClose}>Done</button></div>`}>
    <p>${tables.length > 0 ? `${count(added)} rows came back, in ${tables.length} table${tables.length === 1 ? '' : 's'}. ` : ''}${report.settings.length} settings page${report.settings.length === 1 ? '' : 's'} changed.</p>
    ${dropped > 0 && html`<${Notice} kind="warn">${count(dropped)} rows in the file were refused.<//>`}
    ${moved > 0 && html`<${Notice} kind="warn">${count(moved)} rows got new numbers, because another server already uses the numbers they had.<//>`}
    ${report.warnings.map((text, index) => html`<${Notice} key=${index} kind="warn">${text}<//>`)}
    <p class="muted">Settings and feature tables from before this restore are saved as a snapshot, and can go back with it. Added log entries and scheduled jobs stay.</p>
  <//>`;
}

export default function BackupPage() {
  const guild = useGuild();
  const form = useSettings('backup');
  const [data, setData] = useState(null);
  const [job, setJob] = useState(null);
  const [preview, setPreview] = useState(null);
  const [report, setReport] = useState(null);
  const [uploading, setUploading] = useState(false);
  const wantedPacked = useRef(false);

  const load = () => api.get('/backup').then((fresh) => {
    setData(fresh);
    if (fresh.job && fresh.job.running) setJob(fresh.job);
  }).catch(toastError);
  useEffect(() => { load(); }, [guild.id]);

  const finished = (done) => {
    if (done.error) {
      if (done.error !== 'Stopped.') toast(done.error, 'bad', 9000);
    } else if (done.kind === 'preview') {
      setPreview(done.result);
    } else if (done.kind === 'restore') {
      setPreview(null);
      setReport(done.result);
      guild.reload();
    } else if (done.kind === 'snapshot') {
      toast('Snapshot saved.');
    } else if (done.kind === 'export') {
      const link = document.createElement('a');
      link.href = `/api/g/${guild.id}/backup/exports/${done.result.token}?gzip=${wantedPacked.current ? 1 : 0}`;
      link.download = '';
      link.click();
    }
    load();
  };

  const running = !!(job && job.running);
  useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(() => {
      api.get('/backup/job').then((answer) => {
        const fresh = answer.job;
        if (!fresh) { setJob(null); return; }
        setJob(fresh);
        if (!fresh.running) finished(fresh);
      }).catch(toastError);
    }, 700);
    return () => clearInterval(timer);
  }, [running]);

  const start = (promise) => promise.then((answer) => setJob(answer.job)).catch(toastError);
  const busy = running || uploading;

  const upload = async (file) => {
    setUploading(true);
    try {
      const answer = await api.upload('/backup/upload', file);
      setJob(answer.job);
    } catch (error) {
      toastError(error);
    } finally {
      setUploading(false);
    }
  };
  const exportFile = (messages, packed) => {
    wantedPacked.current = packed;
    start(api.post('/backup/exports', { messages }));
  };
  const stopPreview = () => {
    api.del('/backup/staged').then(() => setJob(null)).catch(toastError);
  };
  const closePreview = () => {
    setPreview(null);
    api.del('/backup/staged').catch(() => {});
  };
  const remove = async (row) => {
    const sure = await confirmDialog({ title: 'Delete this snapshot?', body: `The snapshot from ${when(row.created_at)} goes away for good.`, confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      await api.del(`/backup/snapshots/${row.id}`);
      toast('Deleted.');
      load();
    } catch (error) {
      toastError(error);
    }
  };

  return html`<${SettingsGate} form=${form}>${() => html`
    ${uploading && html`<${Notice}>Sending the file to the server. This takes a while for a large one.<//>`}
    <${JobProgress} job=${job} onStop=${stopPreview} />
    ${data && data.admin === false && html`<${Notice} kind="warn">Only an administrator can export, restore or remove this server's backups.<//>`}
    <${Grid}>
      <${Col}>
        <${ExportCard} data=${data} busy=${busy} onExport=${exportFile} />
        <${ImportCard} busy=${busy} denied=${!!data && data.admin === false} onUpload=${upload} />
      <//>
      <${Col}>
        <${ScheduleCard} form=${form} />
      <//>
    <//>
    <div style=${{ marginTop: '18px' }}>
      <${SnapshotsCard} data=${data} busy=${busy} onTake=${() => start(api.post('/backup/snapshots'))}
        onPreview=${(row) => start(api.post(`/backup/snapshots/${row.id}/preview`))} onDelete=${remove} />
    </div>
    ${preview && html`<${PreviewModal} preview=${preview} busy=${busy} job=${job} onClose=${closePreview}
      onRestore=${(items) => start(api.post('/backup/restore', { token: preview.token, items }))} />`}
    ${report && html`<${ReportModal} report=${report} onClose=${() => setReport(null)} />`}
    <${SaveBar} form=${form} />`}<//>`;
}
