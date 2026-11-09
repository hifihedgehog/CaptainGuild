// Statistics data: recounts from Discord's history, merged channels, the statistics API key and scheduled reports.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Check, Loading, Notice, NumberInput, Select, Switch, Tabs, TextArea, TextInput, confirmDialog, copyText, sameId, toast, toastError, useGuild, when } from '../ui.js';

const TABS = [
  { key: 'recount', label: 'Recount' },
  { key: 'merged', label: 'Merged channels' },
  { key: 'api', label: 'Statistics API' },
  { key: 'reports', label: 'Scheduled reports' },
];
const RECOUNT_STATES = {
  planning: 'Listing channels and threads', reading: 'Reading messages', review: 'Waiting for review', approval: 'Waiting for approval',
  applying: 'Applying', done: 'Done', canceled: 'Canceled',
};
const RUNNING = ['planning', 'reading', 'applying'];
const KINDS = [{ value: 'both', label: 'Messages and voice time' }, { value: 'messages', label: 'Messages' }, { value: 'voice', label: 'Voice time' }];
const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const READABLE = [0, 5, 15, 2, 13];

const pad = (number) => String(number).padStart(2, '0');
// A moment as a datetime-local box shows it, in this browser's time zone.
function boxTime(seconds) {
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fromBox(text) {
  const moment = new Date(text).getTime();
  return Number.isFinite(moment) ? moment / 1000 : null;
}
const count = (value) => Number(value || 0).toLocaleString();

// ---- recount ----

function StartRecount({ defaults, onStarted }) {
  const [start, setStart] = useState(boxTime(defaults.start));
  const [end, setEnd] = useState(boxTime(defaults.end));
  const [everything, setEverything] = useState(true);
  const [channels, setChannels] = useState([]);
  const [threads, setThreads] = useState(true);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    setErrors({});
    try {
      await api.post('/statistics/data/recount', { start: fromBox(start), end: fromBox(end), threads, channels: everything ? null : channels });
      toast('Recount started. It lists the channels first, then reads them.');
      onStarted();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Card} icon="refresh-cw" title="Recount from Discord's history">
    <p class="muted">A recount reads what Discord holds for a period and replaces the message counts recorded for it. Use it to fill the time before statistics began, or to take a raid's spam out of the counts once its messages are deleted. Nothing changes until you approve what it found. Emoji are only ever added, and reactions only for a period from before statistics counted them as they happened. Voice time, member counts and statuses can't be recovered, so they stay as they are.</p>
    <div class="input-row">
      <${TextInput} label="From" type="datetime-local" value=${start} onChange=${setStart} error=${errors.start} help="In your browser's time zone. It starts at the server's creation." />
      <${TextInput} label="To" type="datetime-local" value=${end} onChange=${setEnd} error=${errors.end} help="It starts at the first message statistics recorded." />
    </div>
    <${Switch} label="Every channel" value=${everything} onChange=${setEverything} help="Channels statistics don't track are left out, and so are channels whose messages a merged channel holds for the period." />
    ${!everything && html`<${ChannelPicker} label="Channels and categories" multi=${true} categories=${true} types=${READABLE} value=${channels} onChange=${setChannels} error=${errors.channels} />`}
    <${Switch} label="Threads and forum posts" value=${threads} onChange=${setThreads} help="Active and archived, public and private. A thread's messages count toward its channel. Private threads need the bot to have Manage Threads, or to be in them." />
    <button class="btn primary" disabled=${busy || !start || !end || (!everything && channels.length === 0)} onClick=${go}>Read the history</button>
  <//>`;
}

function RecountChannels({ channels }) {
  if (!channels.length) return html`<p class="muted">Nothing to read in that period.</p>`;
  return html`<div class="table-wrap"><table>
    <thead><tr><th>Channel</th><th>Recorded now</th><th>Discord holds</th><th>Threads</th></tr></thead>
    <tbody>${channels.map((row) => html`<tr key=${String(row.channel_id)}>
      <td>#${row.name}${row.thread_count > 0 && html`<details><summary class="muted small">The biggest threads</summary>
        ${row.threads.map((thread) => html`<div key=${String(thread.id)} class="small">${thread.name}: ${count(thread.found)}${thread.state !== 'done' && thread.state !== 'applied' ? ` (${thread.state})` : ''}</div>`)}</details>`}</td>
      <td>${row.recorded === null || row.recorded === undefined ? '' : count(row.recorded)}</td>
      <td>${count(row.staged)}</td>
      <td>${count(row.thread_count)}</td>
    </tr>`)}</tbody>
  </table></div>`;
}

function RecountStatus({ page, onChanged }) {
  const recount = page.recount;
  const [busy, setBusy] = useState(false);
  const act = async (verb, question) => {
    if (question && !(await confirmDialog(question))) return;
    setBusy(true);
    try {
      await api.post(`/statistics/data/recount/${verb}`);
      onChanged();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const places = recount.places || {};
  const total = Object.values(places).reduce((sum, value) => sum + value, 0);
  const read = total - (places.waiting || 0);
  const percent = total ? Math.round((read / total) * 100) : 0;
  const open = !['done', 'canceled'].includes(recount.state);
  return html`<${Card} icon="refresh-cw" title="Recount">
    <div class="stat-tiles">
      <div class="stat-tile"><b>${RECOUNT_STATES[recount.state] || recount.state}</b><span class="muted small">${when(recount.start)} to ${when(recount.end)}</span></div>
      <div class="stat-tile"><b>${count(recount.found)}</b><span class="muted small">Messages found</span></div>
      <div class="stat-tile"><b>${count(read)} of ${count(total)}</b><span class="muted small">Channels and threads read</span></div>
    </div>
    ${RUNNING.includes(recount.state) && html`<div class="progress"><span style=${{ width: percent + '%' }}></span></div>`}
    ${recount.note && html`<${Notice} kind=${recount.state === 'review' ? 'warn' : ''}>${recount.note}<//>`}
    ${recount.left_out && html`<p class="muted small">${recount.left_out}</p>`}
    ${page.errors.length > 0 && html`<${Notice} kind="warn">${page.errors.map((row) => html`<div key=${String(row.id)}><b>${row.name}</b>: ${row.error}</div>`)}<//>`}
    ${['approval', 'review'].includes(recount.state) && html`<p class="muted">Approving swaps each channel's recorded messages for the period with what Discord holds, one channel at a time. A thread Discord says is gone, or a list of threads Discord won't give, stops the recount here first. A channel with a thread that couldn't be read keeps what it has.</p><${RecountChannels} channels=${page.channels} />`}
    <div class="row">
      ${recount.state === 'approval' && html`<button class="btn primary" disabled=${busy} onClick=${() => act('approve', { title: 'Apply this recount?', body: 'Each channel\'s recorded messages for the period are replaced with what Discord holds. This can\'t be undone.', confirm: 'Apply' })}>Approve</button>`}
      ${recount.state === 'review' && html`<button class="btn primary" disabled=${busy} onClick=${() => act('resume')}>Resume</button>
        <button class="btn" disabled=${busy} onClick=${() => act('skip')}>Leave those out</button>`}
      ${open && recount.state !== 'applying' && html`<button class="btn outline danger" disabled=${busy} onClick=${() => act('cancel', { title: 'Cancel this recount?', body: 'What it read is thrown away, and every count stays as it is.', confirm: 'Cancel recount', danger: true })}>Cancel</button>`}
    </div>
    <p class="muted small">Started ${when(recount.created_at)}${recount.started_by_name ? ` by ${recount.started_by_name}` : ''}${recount.decided_by_name ? `. ${recount.state === 'canceled' ? 'Canceled' : 'Approved'} by ${recount.decided_by_name}` : ''}${recount.finished_at ? `, ${when(recount.finished_at)}` : ''}.</p>
  <//>`;
}

function RecountTab() {
  const guild = useGuild();
  const [page, setPage] = useState(null);
  const timer = useRef(null);
  const load = async () => {
    clearTimeout(timer.current);
    try {
      const next = await api.get('/statistics/data/recount');
      setPage(next);
      if (next.recount && RUNNING.includes(next.recount.state)) timer.current = setTimeout(load, 3000);
    } catch (error) {
      toastError(error);
    }
  };
  useEffect(() => { load(); return () => clearTimeout(timer.current); }, [guild.id]);
  if (!page) return html`<${Loading} />`;
  const open = page.recount && !['done', 'canceled'].includes(page.recount.state);
  return html`
    ${page.recount && html`<${RecountStatus} page=${page} onChanged=${load} />`}
    ${!open && html`<${StartRecount} defaults=${page.defaults} onStarted=${load} />`}`;
}

// ---- merged channels ----

function AggregateRow({ row, onChanged }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(row.name);
  const [category, setCategory] = useState(row.category_id);
  const [auto, setAuto] = useState(row.auto_categories || []);
  const [kinds, setKinds] = useState(row.kinds);
  const [errors, setErrors] = useState({});
  const save = async () => {
    setErrors({});
    try {
      await api.patch(`/statistics/data/merged/${row.id}`, { name, category_id: category, auto_categories: auto, kinds });
      toast('Saved.');
      setEditing(false);
      onChanged();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    }
  };
  const remove = async () => {
    const sure = await confirmDialog({ title: `Delete ${row.name}?`, body: 'It leaves the channel lists. Only an empty merged channel can go.', confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      await api.del(`/statistics/data/merged/${row.id}`);
      toast('Deleted.');
      onChanged();
    } catch (error) {
      toastError(error);
    }
  };
  return html`<div class="field-edit">
    <div class="spread"><b>${row.name}</b>
      <span class="row">
        <button class="btn small" onClick=${() => setEditing(!editing)}>${editing ? 'Close' : 'Edit'}</button>
        <button class="btn small outline danger" disabled=${row.holds_counts} title=${row.holds_counts ? 'Merge it into another merged channel first.' : ''} onClick=${remove}>Delete</button>
      </span></div>
    <div class="muted small">${row.category_name ? `Filed under ${row.category_name}. ` : ''}${row.auto_names.length ? `New ${row.kinds === 'both' ? 'messages and voice time' : row.kinds === 'voice' ? 'voice time' : 'messages'} in ${row.auto_names.join(', ')} go here as they happen. ` : ''}${row.holds_counts ? 'Holds counts.' : 'Empty.'}</div>
    ${editing && html`<div>
      <${TextInput} label="Name" maxLength=${100} value=${name} onChange=${setName} error=${errors.name} />
      <${ChannelPicker} label="Category" types=${[]} categories=${true} placeholder="Pick a category" value=${category} onChange=${setCategory} error=${errors.category_id} help="Filters treat it as a channel in this category." />
      <${ChannelPicker} label="Merge new activity from these categories" types=${[]} categories=${true} multi=${true} value=${auto} onChange=${setAuto} error=${errors.auto_categories}
        help="Each category can feed one merged channel. With messages and voice time both picked, new channels there aren't listed on their own." />
      <${Select} label="What merges as it happens" options=${KINDS} value=${kinds} onChange=${setKinds} error=${errors.kinds} />
      <button class="btn primary" onClick=${save}>Save</button>
    </div>`}
  </div>`;
}

function MergeTask({ aggregates, onStarted }) {
  const [target, setTarget] = useState(aggregates.length ? aggregates[0].id : null);
  const [kinds, setKinds] = useState('both');
  const [categories, setCategories] = useState([]);
  const [patterns, setPatterns] = useState('');
  const [channels, setChannels] = useState([]);
  const [forget, setForget] = useState(true);
  const [preview, setPreview] = useState(null);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const body = () => ({ target_id: target, kinds, categories, channels, forget, patterns: patterns.split('\n').map((line) => line.trim()).filter(Boolean) });
  const run = async (path, extra, done) => {
    setBusy(true);
    setErrors({});
    try {
      done(await api.post(path, { ...body(), ...extra }));
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const look = () => run('/statistics/data/merged/preview', {}, setPreview);
  const merge = () => run('/statistics/data/merged/merge', { confirm: true }, () => { toast('Merging. Each channel moves in small batches.'); setPreview(null); onStarted(); });
  return html`<${Card} icon="git-merge" title="Merge channels">
    <p class="muted">A merge moves channels' recorded counts into a merged channel, a little at a time, so every total stays the same. Activity from after the merge starts stays where it is. Pick channels by category, by name, or one by one. In a name, * stands for any run of characters, a backslash takes the next character as it is, and case doesn't matter.</p>
    <${Select} label="Merge into" options=${aggregates.map((row) => ({ value: row.id, label: row.name }))} value=${target} onChange=${setTarget} error=${errors.target_id} />
    <${Select} label="What moves" options=${KINDS} value=${kinds} onChange=${setKinds} error=${errors.kinds} help="Emoji move with messages." />
    <${ChannelPicker} label="Every channel filed under these categories" types=${[]} categories=${true} multi=${true} value=${categories} onChange=${setCategories} help="Deleted channels that were filed there count too." />
    <${TextArea} label="Channel names, one a line" rows=${3} counter=${false} placeholder=${'ticket-*\nold-\\*announcements'} value=${patterns} onChange=${setPatterns} error=${errors.patterns} />
    <${ChannelPicker} label="These channels" types=${READABLE} multi=${true} value=${channels} onChange=${setChannels} error=${errors.channels} />
    <${Check} label="Forget a channel Discord no longer has once it's empty" value=${forget} onChange=${setForget} />
    <div class="row"><button class="btn" disabled=${busy || !target} onClick=${look}>See what it takes</button></div>
    ${preview && html`<div>
      <p><b>${count(preview.channels.length)} channels, ${count(preview.rows)} rows.</b>${forget && preview.would_forget ? ` ${count(preview.would_forget)} of them are gone from Discord and can be forgotten once empty.` : ''}</p>
      ${preview.channels.length > 0 && html`<div class="table-wrap"><table><thead><tr><th>Channel</th><th>Rows</th><th></th></tr></thead><tbody>
        ${preview.channels.slice(0, 500).map((row) => html`<tr key=${String(row.id)}><td>#${row.name}</td><td>${count(row.rows)}</td><td class="muted">${row.deleted ? 'Gone from Discord' : ''}</td></tr>`)}
      </tbody></table></div>`}
      ${preview.channels.length > 0 && html`<button class="btn primary" disabled=${busy} onClick=${merge}>Merge them</button>`}
    </div>`}
  <//>`;
}

function MergeStatus({ merge }) {
  if (!merge) return null;
  const percent = merge.total ? Math.round((merge.done / merge.total) * 100) : 100;
  return html`<div class="field-edit">
    <b>${merge.state === 'running' ? 'Merging' : merge.state === 'done' ? 'Last merge' : 'Stopped'} into ${merge.target_name}</b>
    ${merge.state === 'running' && html`<div class="progress"><span style=${{ width: percent + '%' }}></span></div>`}
    <div class="muted small">${count(merge.done)} of ${count(merge.total)} channels, ${count(merge.rows_moved)} rows of ${merge.kinds_label} moved${merge.forgotten ? `, ${count(merge.forgotten)} channels forgotten` : ''}. Started ${when(merge.created_at)}${merge.started_by_name ? ` by ${merge.started_by_name}` : ''}.</div>
    ${merge.note && html`<${Notice} kind="warn">${merge.note}<//>`}
  </div>`;
}

function MergedTab({ overview }) {
  const guild = useGuild();
  const [page, setPage] = useState(null);
  const [name, setName] = useState('');
  const [category, setCategory] = useState(null);
  const timer = useRef(null);
  const load = async () => {
    clearTimeout(timer.current);
    try {
      const next = await api.get('/statistics/data/merged');
      setPage(next);
      if (next.merge && next.merge.state === 'running') timer.current = setTimeout(load, 3000);
    } catch (error) {
      toastError(error);
    }
  };
  useEffect(() => { load(); return () => clearTimeout(timer.current); }, [guild.id]);
  const create = async () => {
    try {
      await api.post('/statistics/data/merged', { name, category_id: category });
      toast('Merged channel made.');
      setName('');
      load();
    } catch (error) {
      toastError(error);
    }
  };
  if (!page) return html`<${Loading} />`;
  // The server's `aggregates` setting holds these, and this page is where they change.
  const aggregates = page.aggregates;
  return html`
    ${overview && overview.channels.channels >= overview.channels_warning && html`<${Notice} kind="warn">This server keeps ${count(overview.channels.channels)} channels in its statistics, ${count(overview.channels.deleted)} of them deleted in Discord. Pages and pickers slow down past ${count(overview.channels_warning)}. Merge the deleted ones into a merged channel and let the merge forget them.<//>`}
    <${Card} icon="layers-3" title=${`Merged channels (${aggregates.length} of ${page.most})`}>
      <p class="muted">A merged channel exists only in statistics. It gathers many channels' counts, such as old tickets or channels that were deleted, and shows as one channel everywhere. One holding counts can't be deleted until it's merged into another.</p>
      ${aggregates.length === 0 && html`<p class="muted">None yet.</p>`}
      ${aggregates.map((row) => html`<${AggregateRow} key=${String(row.id)} row=${row} onChanged=${load} />`)}
      <div class="input-row">
        <${TextInput} label="New merged channel" placeholder="Old channels" maxLength=${100} value=${name} onChange=${setName} />
        <${ChannelPicker} label="Category" types=${[]} categories=${true} placeholder="Pick a category" value=${category} onChange=${setCategory} />
      </div>
      <button class="btn primary" disabled=${!name.trim() || aggregates.length >= page.most} onClick=${create}>Make it</button>
    <//>
    <${MergeStatus} merge=${page.merge} />
    ${aggregates.length > 0 && (!page.merge || page.merge.state !== 'running') && html`<${MergeTask} aggregates=${aggregates} onStarted=${load} />`}`;
}

// ---- the statistics API ----

function ApiTab() {
  const guild = useGuild();
  const [page, setPage] = useState(null);
  const [fresh, setFresh] = useState(null);
  const load = () => api.get('/statistics/data/api').then(setPage).catch(toastError);
  useEffect(() => { load(); }, [guild.id]);
  const make = async () => {
    if (page.key && !(await confirmDialog({ title: 'Replace the key?', body: 'The current key stops working at once.', confirm: 'Replace', danger: true }))) return;
    try {
      const next = await api.post('/statistics/data/api/key');
      setFresh(next.new_key);
      setPage(next);
    } catch (error) {
      toastError(error);
    }
  };
  const remove = async () => {
    if (!(await confirmDialog({ title: 'Delete the key?', body: 'Scripts using it stop working at once.', confirm: 'Delete', danger: true }))) return;
    try {
      setPage(await api.del('/statistics/data/api/key'));
      setFresh(null);
    } catch (error) {
      toastError(error);
    }
  };
  if (!page) return html`<${Loading} />`;
  const base = page.base.startsWith('/') ? location.origin + page.base : page.base; // a site with no public address set
  const example = `curl -H "Authorization: Bearer YOUR_KEY" "${base}/v1/guilds/${page.guild_id}/messages/sums"`;
  return html`<${Card} icon="key" title="Statistics API">
    <p class="muted">Scripts read this server's statistics with its key. The paths, parameters and answers follow the statistics API many servers already use, so a script written for it works once its base address changes to the one below. Each key works for this server only, and the bot keeps only a fingerprint of it. A key reads only the channels its maker can see, and stops working when its maker leaves the server or the bot does.</p>
    <p>Base address: <code class="mono">${base}</code></p>
    <p class="muted small">Send the key as <code>Authorization: Bearer</code>. Up to ${page.per_minute} requests a minute, with x-ratelimit-limit, x-ratelimit-remaining, x-ratelimit-reset and retry-after on every answer. Times are in milliseconds, voice and app time in minutes.</p>
    ${fresh && html`<${Notice} kind="warn">Copy this key now. It isn't shown again.
      <div class="input-group"><input class="mono" readonly value=${fresh} aria-label="New key" /><button class="btn" onClick=${() => copyText(fresh)}>Copy</button></div><//>`}
    ${page.note ? html`<${Notice}>${page.note}<//>` : html`
    ${page.key ? html`<p>A key ending in <b class="mono">${page.key.hint}</b> made ${when(page.key.created_at)}${page.key.created_by_name ? ` by ${page.key.created_by_name}` : ''}. ${page.key.used_at ? `Last used ${when(page.key.used_at)}.` : 'Not used yet.'}</p>` : html`<p class="muted">No key yet.</p>`}
    <div class="row">
      <button class="btn primary" onClick=${make}>${page.key ? 'Replace the key' : 'Make a key'}</button>
      ${page.key && html`<button class="btn outline danger" onClick=${remove}>Delete the key</button>`}
    </div>`}
    <details><summary>Every path</summary>${page.paths.map((path) => html`<div key=${path} class="mono small">GET ${path}</div>`)}</details>
    <details><summary>An example</summary><pre class="mono small" style=${{ whiteSpace: 'pre-wrap' }}>${example}</pre></details>
  <//>`;
}

// ---- scheduled reports ----

function ReportsTab() {
  const guild = useGuild();
  const [page, setPage] = useState(null);
  const [channel, setChannel] = useState(null);
  const [cadence, setCadence] = useState('week');
  const [weekday, setWeekday] = useState(0);
  const [hour, setHour] = useState(9);
  const [errors, setErrors] = useState({});
  const load = () => api.get('/statistics/data/reports').then(setPage).catch(toastError);
  useEffect(() => { load(); }, [guild.id]);
  const add = async () => {
    setErrors({});
    try {
      setPage(await api.post('/statistics/data/reports', { channel_id: channel, cadence, weekday, hour }));
      toast('Scheduled.');
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    }
  };
  const act = async (report, verb) => {
    try {
      if (verb === 'send') {
        setPage(await api.post(`/statistics/data/reports/${report.id}/send`));
        toast('Posted.');
      } else if (await confirmDialog({ title: 'Stop this report?', body: 'Reports already posted stay.', confirm: 'Stop', danger: true })) {
        setPage(await api.del(`/statistics/data/reports/${report.id}`));
      }
    } catch (error) {
      toastError(error);
    }
  };
  if (!page) return html`<${Loading} />`;
  return html`<${Card} icon="calendar" title=${`Scheduled reports (${page.reports.length} of ${page.most})`}>
    <p class="muted">A report posts the last full week or month: messages, voice time and active members beside the period before, joins and leaves, the member count, and the most active members and channels. A channel that not everyone who reads the report can see shows as Hidden channel. Nobody is pinged.</p>
    ${page.reports.length === 0 && html`<p class="muted">None yet.</p>`}
    ${page.reports.map((report) => html`<div class="field-edit" key=${report.id}>
      <div class="spread"><b>${report.cadence === 'week' ? `Weekly, ${WEEKDAYS[report.weekday]}s` : 'Monthly, on the 1st'} at ${pad(report.hour)}:00 in #${report.channel_name}</b>
        <span class="row"><button class="btn small" onClick=${() => act(report, 'send')}>Post now</button><button class="btn small outline danger" onClick=${() => act(report, 'delete')}>Stop</button></span></div>
      <div class="muted small">Next ${when(report.next_at)}${report.last_at ? `, last ${when(report.last_at)}` : ''}${report.created_by_name ? `. Set up by ${report.created_by_name}` : ''}.</div>
      ${report.last_error && html`<${Notice} kind="warn">${report.last_error}<//>`}
    </div>`)}
    <${ChannelPicker} label="Post in" types=${[0, 5]} value=${channel} onChange=${setChannel} error=${errors.channel_id} />
    <div class="input-row">
      <${Select} label="How often" options=${[{ value: 'week', label: 'Every week' }, { value: 'month', label: 'Every month, on the 1st' }]} value=${cadence} onChange=${setCadence} error=${errors.cadence} />
      ${cadence === 'week' && html`<${Select} label="Day" options=${WEEKDAYS.map((label, value) => ({ value, label }))} value=${weekday} onChange=${setWeekday} error=${errors.weekday} />`}
      <${NumberInput} label=${`Hour (${page.timezone})`} min=${0} max=${23} value=${hour} onChange=${(value) => setHour(value === null ? 0 : value)} error=${errors.hour} />
    </div>
    <button class="btn primary" disabled=${!channel || page.reports.length >= page.most} onClick=${add}>Schedule it</button>
  <//>`;
}

export default function StatsDataPage() {
  const guild = useGuild();
  const [tab, setTab] = useState(() => new URLSearchParams(location.search).get('tab') || 'recount');
  const [overview, setOverview] = useState(null);
  useEffect(() => { api.get('/statistics/data').then(setOverview).catch(toastError); }, [guild.id]);
  const pick = (key) => {
    setTab(key);
    const params = new URLSearchParams(location.search);
    params.set('tab', key);
    history.replaceState(null, '', `${location.pathname}?${params}`);
  };
  return html`<div>
    <h1 class="page-title">Statistics data</h1>
    ${overview && !overview.enabled && html`<${Notice} kind="warn">Statistics are switched off here, so nothing new is counted. Recounts need them on.<//>`}
    <${Tabs} tabs=${TABS} active=${TABS.some((item) => sameId(item.key, tab)) ? tab : 'recount'} onChange=${pick} />
    ${tab === 'merged' ? html`<${MergedTab} overview=${overview} />` : tab === 'api' ? html`<${ApiTab} />` : tab === 'reports' ? html`<${ReportsTab} />` : html`<${RecountTab} />`}
  </div>`;
}
