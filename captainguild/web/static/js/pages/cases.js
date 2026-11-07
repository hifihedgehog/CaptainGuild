import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, Check, DataTable, Empty, Icon, Loading, Modal, Notice, Pager, Select, Tabs, TextArea, TextInput, confirmDialog, copyText, toast, toastError, useGuild, when } from '../ui.js';

const ACTIONS = ['ban', 'unban', 'tempban', 'softban', 'kick', 'mute', 'unmute', 'hardmute', 'timeout', 'untimeout', 'warn', 'minimute', 'unminimute'];
const BADGES = {
  ban: 'red', tempban: 'red', softban: 'red', kick: 'blue', mute: 'gold', hardmute: 'gold', minimute: 'gold', timeout: 'gold', warn: 'gold',
  unban: 'green', unmute: 'green', unminimute: 'green', untimeout: 'green',
};
const SOURCES = { automod: 'automod', external: 'outside the bot', timer: 'automatic', threshold: 'warn threshold', drama: 'drama channel', 'mini mute': 'mini mute' };
const PAGE_SIZES = [10, 25, 50, 100];
const NO_FILTERS = { q: '', number: '', action: '', user: '', moderator: '', reason: '', state: '' };

function lengthText(seconds) {
  if (!seconds) return '';
  const parts = [];
  let left = seconds;
  for (const [name, size] of [['d', 86400], ['h', 3600], ['m', 60], ['s', 1]]) {
    const amount = Math.floor(left / size);
    if (amount) parts.push(amount + name);
    left -= amount * size;
  }
  return parts.slice(0, 2).join(' ');
}

function ActionBadge({ row }) {
  return html`<span class=${'badge ' + (BADGES[row.action] || '')}>${row.action}</span>`;
}

function filterQuery(filters) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) query.set(key, value);
  }
  return query;
}

function EditReason({ row, onClose, onSaved }) {
  const [reason, setReason] = useState(row.reason || '');
  const [saving, setSaving] = useState(false);
  const save = async () => {
    setSaving(true);
    try {
      const fresh = await api.patch(`/cases/${row.number}`, { reason });
      toast(fresh.message_id && !fresh.post_updated ? "Saved. The modlog post couldn't be changed." : 'Saved.');
      onSaved(fresh);
    } catch (error) {
      toastError(error);
      setSaving(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([reason]);
  const [start] = useState(snapshot);
  return html`<${Modal} title=${'Reason for case ' + row.number} icon="pencil" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn primary" disabled=${saving || !reason.trim()} onClick=${save}>${saving ? 'Saving...' : 'Save'}</button></div>`}>
    <p class="muted">The modlog post changes to match.</p>
    <${TextArea} label="Reason" rows=${5} maxLength=${1024} value=${reason} onChange=${setReason} />
  <//>`;
}

function VoidCase({ row, onClose, onSaved }) {
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const save = async () => {
    setSaving(true);
    try {
      onSaved(await api.post(`/cases/${row.number}/void`, { reason }));
      toast('Voided.');
    } catch (error) {
      toastError(error);
      setSaving(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([reason]);
  const [start] = useState(snapshot);
  return html`<${Modal} title=${'Void case ' + row.number} icon="x" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn danger" disabled=${saving} onClick=${save}>${saving ? 'Voiding...' : 'Void case'}</button></div>`}>
    <p class="muted">A voided case stops counting against ${row.user_name}. It stays on record, marked as voided, and you can restore it later. This doesn't undo the action in Discord.</p>
    <${TextInput} label="Why" placeholder="Optional" maxLength=${1024} value=${reason} onChange=${setReason} />
  <//>`;
}

function CaseButtons({ guild, row, onEdit, onVoid, onRestore }) {
  return html`<div class="row nowrap" style=${{ flexWrap: 'nowrap', gap: '2px' }}>
    ${row.message_id && html`<a class="icon-btn" title="Open the modlog post" aria-label=${'Open the modlog post for case ' + row.number} target="_blank" rel="noopener noreferrer"
      href=${`https://discord.com/channels/${guild.id}/${row.channel_id}/${row.message_id}`}><${Icon} name="external-link" /></a>`}
    <button class="icon-btn" title="Edit reason" aria-label=${'Edit the reason for case ' + row.number} onClick=${() => onEdit(row)}><${Icon} name="pencil" /></button>
    ${row.voided
      ? html`<button class="icon-btn" title="Restore" aria-label=${'Restore case ' + row.number} onClick=${() => onRestore(row)}><${Icon} name="undo-2" /></button>`
      : html`<button class="icon-btn danger" title="Void" aria-label=${'Void case ' + row.number} onClick=${() => onVoid(row)}><${Icon} name="x" /></button>`}
  </div>`;
}

function CaseCell({ row }) {
  return row.reason ? html`<${LongText} text=${row.reason} />` : html`<span class="dim">No reason given</span>`;
}

function WhoCell({ name, bot, source }) {
  const shown = name || (source === 'automod' || source === 'threshold' ? 'Automod' : 'Unknown');
  return html`<span>${shown}${bot ? html` <span class="badge outline">bot</span>` : ''}</span>`;
}

// Long text stays clipped until someone asks for the rest.
function LongText({ text }) {
  const [open, setOpen] = useState(false);
  const long = text.length > 300 || text.split('\n').length > 6;
  return html`<div>
    <div class=${long && !open ? 'truncate' : ''}>${text}</div>
    ${long && html`<button type="button" class="link-btn small" aria-expanded=${open} onClick=${() => setOpen(!open)}>${open ? 'Show less' : 'Show more'}</button>`}
  </div>`;
}

function MemberPanel({ userId, onClose, onEdit, onVoid, onRestore, version }) {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const load = () => api.get(`/cases/member/${userId}`).then(setData).catch((error) => { toastError(error); onClose(); });
  useEffect(() => { load(); }, [userId, version]);
  const addNote = async () => {
    setSaving(true);
    try {
      await api.post(`/cases/member/${userId}/notes`, { text: note });
      setNote('');
      toast('Note saved.');
      load();
    } catch (error) {
      toastError(error);
    }
    setSaving(false);
  };
  const removeNote = async (row) => {
    const sure = await confirmDialog({ title: `Remove note ${row.number}?`, body: 'This deletes it for good.', confirm: 'Remove', danger: true });
    if (!sure) return;
    try {
      await api.del(`/cases/notes/${row.number}`);
      load();
    } catch (error) {
      toastError(error);
    }
  };
  const title = data ? (data.name || 'Unknown member') : 'Loading...';
  return html`<${Modal} large title=${title} icon="user" onClose=${onClose}>
    ${!data ? html`<${Loading} />` : html`<div>
      <div class="row" style=${{ marginBottom: '12px' }}>
        ${data.member && html`<img class="avatar" src=${data.member.avatar} alt="" />`}
        <span class="muted mono small">${String(data.user_id)}</span>
        <button class="icon-btn" title="Copy ID" aria-label="Copy ID" onClick=${() => copyText(String(data.user_id))}><${Icon} name="copy" size=${14} /></button>
        ${data.member ? html`<span class="badge green">In the server</span>` : html`<span class="badge outline">Not in the server</span>`}
        ${data.muted_until !== null && html`<span class="badge gold">${data.muted_until ? 'Muted until ' + when(data.muted_until) : 'Muted'}</span>`}
      </div>
      <div class="row" style=${{ marginBottom: '16px' }}>
        ${Object.entries(data.counts).map(([name, count]) => html`<span class="badge outline" key=${name}>${count} ${name}</span>`)}
      </div>
      <h4>Cases</h4>
      <${DataTable} rows=${data.cases} rowKey="number" pageSizes=${[10, 25, 100]} empty="No cases." columns=${[
        { key: 'number', label: 'Case', sort: true, width: '70px', render: (row) => html`<span class=${row.voided ? 'case-voided' : ''}>${row.number}</span>` },
        { key: 'action', label: 'Action', sort: true, width: '100px', render: (row) => html`<${ActionBadge} row=${row} />${row.voided ? html` <span class="badge outline">voided</span>` : ''}` },
        { key: 'moderator_name', label: 'Moderator', width: '150px', render: (row) => html`<${WhoCell} name=${row.moderator_name} bot=${row.moderator_bot} source=${row.source} />` },
        { key: 'reason', label: 'Reason', render: (row) => html`<${CaseCell} row=${row} />` },
        { key: 'created_at', label: 'When', sort: true, width: '170px', render: (row) => when(row.created_at) },
        { key: 'tools', label: '', width: '90px', render: (row) => html`<${CaseButtons} guild=${guild} row=${row} onEdit=${onEdit} onVoid=${onVoid} onRestore=${onRestore} />` },
      ]} />
      <hr />
      <h4>Notes</h4>
      ${data.notes.length === 0 && html`<p class="muted">No notes yet. Only staff can read these.</p>`}
      ${data.notes.map((row) => html`<div class="field-edit" key=${row.number}>
        <div class="spread"><span class="muted small">#${row.number} by ${row.author_name || 'Unknown'}, ${when(row.created_at)}</span>
          <button class="icon-btn danger" aria-label=${'Remove note ' + row.number} onClick=${() => removeNote(row)}><${Icon} name="trash-2" /></button></div>
        <${LongText} text=${row.text} />
      </div>`)}
      <${TextArea} label="Add a note" rows=${3} maxLength=${1800} value=${note} onChange=${setNote} />
      <button class="btn primary" disabled=${saving || !note.trim()} onClick=${addNote}>${saving ? 'Saving...' : 'Save note'}</button>
    </div>`}
  <//>`;
}

function CasesTab({ filters, setFilters, onMember, version, onEdit, onVoid, onRestore }) {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(25);
  const [failed, setFailed] = useState(null);
  const [moderators, setModerators] = useState([]);
  const latest = useRef(0);

  useEffect(() => { api.get('/cases/filters').then((found) => setModerators(found.moderators)).catch(() => setModerators([])); }, [guild.id, version]);

  useEffect(() => {
    const mine = ++latest.current;
    const timer = setTimeout(() => {
      const query = filterQuery(filters);
      query.set('page', page);
      query.set('size', size);
      api.get(`/cases?${query}`).then((fresh) => {
        if (mine !== latest.current) return;
        setData(fresh);
        setFailed(null);
        // The last row of the last page can go while a filter is on. Staying there would say nothing matches while rows exist.
        if (fresh.rows.length === 0 && fresh.total > 0 && page > 1) setPage(Math.ceil(fresh.total / size));
      }).catch((error) => { if (mine === latest.current) setFailed(error); });
    }, 200);
    return () => clearTimeout(timer);
  }, [JSON.stringify(filters), page, size, version, guild.id]);

  const change = (key) => (event) => { setFilters({ ...filters, [key]: event.currentTarget.value }); setPage(1); };
  const pages = data ? Math.max(1, Math.ceil(data.total / size)) : 1;
  const first = data && data.total ? (page - 1) * size + 1 : 0;
  const last = data ? Math.min(page * size, data.total) : 0;
  const exportLink = (kind) => {
    const query = filterQuery(filters);
    query.set('format', kind);
    return `/api/g/${guild.id}/cases/export?${query}`;
  };
  return html`<div>
    <div class="table-tools">
      <span>Records:</span>
      <select aria-label="Records per page" value=${size} onChange=${(e) => { setSize(Number(e.currentTarget.value)); setPage(1); }}>
        ${PAGE_SIZES.map((n) => html`<option key=${n} value=${n}>${n}</option>`)}</select>
      <div class="input-group case-search"><input type="search" placeholder="Search case, member, moderator or reason" aria-label="Search cases" value=${filters.q} onInput=${change('q')} /><span class="addon"><${Icon} name="search" /></span></div>
      <select aria-label="Which cases to show" value=${filters.state} onChange=${change('state')}><option value="">All cases</option><option value="active">Standing</option><option value="voided">Voided</option></select>
      <div class="right row">
        <a class="btn" href=${exportLink('csv')}><${Icon} name="download" /> CSV</a>
        <a class="btn" href=${exportLink('json')}><${Icon} name="download" /> JSON</a>
      </div>
    </div>
    ${failed && html`<${Notice} kind="bad">${failed.message}<//>`}
    <div class="table-wrap"><table>
      <thead>
        <tr><th style=${{ width: '90px' }}>Case</th><th style=${{ width: '130px' }}>Action</th><th style=${{ width: '190px' }}>Member</th><th style=${{ width: '170px' }}>Moderator</th><th>Reason</th><th style=${{ width: '170px' }}>When</th><th style=${{ width: '90px' }}></th></tr>
        <tr>
          <th><input type="text" inputmode="numeric" placeholder="Case" aria-label="Filter by case number" value=${filters.number} onInput=${change('number')} /></th>
          <th><select aria-label="Filter by action" value=${filters.action} onChange=${change('action')}><option value="">All</option>${ACTIONS.map((name) => html`<option key=${name} value=${name}>${name}</option>`)}</select></th>
          <th><input type="text" placeholder="Filter by Member" aria-label="Filter by member, name or ID" value=${filters.user} onInput=${change('user')} /></th>
          <th><input type="text" list="case-moderators" placeholder="Filter by Moderator" aria-label="Filter by moderator, name or ID" value=${filters.moderator} onInput=${change('moderator')} />
            <datalist id="case-moderators">${moderators.map((row) => html`<option key=${String(row.id)} value=${row.name}></option>`)}</datalist></th>
          <th><input type="text" placeholder="Filter by Reason" aria-label="Filter by reason" value=${filters.reason} onInput=${change('reason')} /></th>
          <th></th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${!data && !failed && html`<tr><td colspan="7"><${Loading} /></td></tr>`}
        ${data && data.rows.length === 0 && html`<tr><td colspan="7"><${Empty}>No matching records<//></td></tr>`}
        ${data && data.rows.map((row) => html`<tr key=${row.number} class=${row.voided ? 'case-voided' : ''}>
          <td>${row.number}</td>
          <td><${ActionBadge} row=${row} />${row.seconds ? html` <span class="dim small">${lengthText(row.seconds)}</span>` : ''}${row.voided ? html`<div><span class="badge outline">voided</span></div>` : ''}${SOURCES[row.source] ? html`<div class="dim small">${SOURCES[row.source]}</div>` : ''}</td>
          <td><button class="link-btn" title=${String(row.user_id)} onClick=${() => onMember(row.user_id)}>${row.user_name}</button></td>
          <td><${WhoCell} name=${row.moderator_name} bot=${row.moderator_bot} source=${row.source} /></td>
          <td><${CaseCell} row=${row} />${row.voided && row.void_reason ? html`<div class="dim small">Voided: ${row.void_reason}</div>` : ''}</td>
          <td class="small">${when(row.created_at)}</td>
          <td><${CaseButtons} guild=${guild} row=${row} onEdit=${onEdit} onVoid=${onVoid} onRestore=${onRestore} /></td>
        </tr>`)}
      </tbody>
    </table></div>
    <div class="table-foot">
      <span class="muted small">Showing ${first} to ${last} of ${data ? data.total : 0} records</span>
      <${Pager} page=${page} pages=${pages} onPage=${setPage} />
    </div>
  </div>`;
}

function endsText(row) {
  return row.expires_at ? when(row.expires_at) : 'No end';
}

function RunningSection({ title, rows, rowKey, columns, empty }) {
  return html`<div style=${{ marginBottom: '20px' }}>
    <h4>${title} (${rows.length})</h4>
    <${DataTable} rows=${rows} rowKey=${rowKey} pageSizes=${[25, 50, 100]} empty=${empty} columns=${columns} />
  </div>`;
}

function RaidCard({ data, onChange }) {
  const [busy, setBusy] = useState(false);
  const act = async (action, doneText) => {
    setBusy(true);
    try {
      const answer = await api.post('/moderation/raid', { action });
      if (answer.releasing) {
        toast(`Letting ${answer.releasing} people go. This takes a few minutes, and the modlog says when it's done.`);
      } else {
        toast(action === 'release' ? `Let ${answer.released} go.` + (answer.failed ? ` Discord refused ${answer.failed}.` : '') : doneText);
      }
      onChange();
    } catch (error) {
      toastError(error);
    }
    setBusy(false);
  };
  const start = async () => {
    const sure = await confirmDialog({ title: 'Turn on raid mode?', body: 'Everyone who joins from now on is held under the mute role until someone lets them go.', confirm: 'Turn on', danger: true });
    if (sure) act('on', 'Raid mode is on.');
  };
  return html`<div style=${{ marginBottom: '20px' }}>
    <h4>Raid mode</h4>
    <p class="muted">${data.raid ? `On since ${when(data.raid.started_at)}. ${data.raid.expires_at ? 'Ends ' + when(data.raid.expires_at) + '.' : 'No end.'}` : 'Off. When it is on, everyone who joins is held under the mute role until someone lets them go.'}${data.held ? ` Holding ${data.held}.` : ''}</p>
    <div class="row">
      ${data.raid
        ? html`<button class="btn" disabled=${busy} onClick=${() => act('off', 'Raid mode is off. Anyone held stays held.')}>Turn off</button>`
        : html`<button class="btn danger" disabled=${busy} onClick=${start}>Turn on</button>`}
      ${data.held > 0 && html`<button class="btn" disabled=${busy} onClick=${() => act('release')}>Let ${data.held} go</button>`}
    </div>
  </div>`;
}

function RunningTab({ onChanged }) {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(null);
  const [busy, setBusy] = useState('');
  const load = () => api.get('/moderation/running').then((fresh) => { setData(fresh); setFailed(null); }).catch(setFailed);
  useEffect(() => { load(); }, [guild.id]);
  const channelName = (id) => {
    const found = guild.channels.find((channel) => String(channel.id) === String(id));
    return found ? '#' + found.name : 'A deleted channel';
  };
  const lift = async (key, path, title, body, onDone) => {
    const sure = await confirmDialog({ title, body, confirm: 'Lift it', danger: true });
    if (!sure) return;
    setBusy(key);
    try {
      await api.post(path, {});
      toast('Lifted.');
      onDone();
      load();
    } catch (error) {
      toastError(error);
    }
    setBusy('');
  };
  if (failed) return html`<${Notice} kind="bad">${failed.message}<//>`;
  if (!data) return html`<${Loading} />`;
  const liftButton = (key, label, onClick) => html`<button class="btn small" disabled=${busy === key} aria-label=${label} onClick=${onClick}>${busy === key ? 'Lifting...' : 'Lift'}</button>`;
  return html`<div>
    <${RaidCard} data=${data} onChange=${load} />
    <${RunningSection} title="Mutes" rows=${data.mutes} rowKey="user_id" empty="Nobody is muted." columns=${[
      { key: 'name', label: 'Member', render: (row) => html`<span title=${String(row.user_id)}>${row.name || String(row.user_id)}${row.here ? '' : html` <span class="badge outline">left</span>`}</span>` },
      { key: 'kind', label: 'Kind', width: '100px' },
      { key: 'moderator_name', label: 'Moderator', width: '170px', render: (row) => row.moderator_name || 'Automod' },
      { key: 'expires_at', label: 'Ends', width: '170px', render: endsText },
      { key: 'tools', label: '', width: '80px', render: (row) => liftButton('m' + row.user_id, 'Lift the mute on ' + (row.name || row.user_id), () => lift('m' + row.user_id, `/moderation/running/mutes/${row.user_id}/lift`, `Unmute ${row.name || row.user_id}?`, 'This records an unmute case, as the command does.', onChanged)) },
    ]} />
    <${RunningSection} title="Temporary bans" rows=${data.bans} rowKey="user_id" empty="Nobody is on a temporary ban." columns=${[
      { key: 'name', label: 'Member', render: (row) => html`<span title=${String(row.user_id)}>${row.name || String(row.user_id)}</span>` },
      { key: 'moderator_name', label: 'Moderator', width: '170px', render: (row) => row.moderator_name || 'Automod' },
      { key: 'expires_at', label: 'Ends', width: '170px', render: endsText },
      { key: 'tools', label: '', width: '80px', render: (row) => liftButton('b' + row.user_id, 'Lift the ban on ' + (row.name || row.user_id), () => lift('b' + row.user_id, `/moderation/running/bans/${row.user_id}/lift`, `Unban ${row.name || row.user_id}?`, 'This records an unban case, as the command does.', onChanged)) },
    ]} />
    <${RunningSection} title="Locked channels" rows=${data.lockdowns} rowKey="channel_id" empty="No channel is locked." columns=${[
      { key: 'channel_id', label: 'Channel', render: (row) => channelName(row.channel_id) },
      { key: 'reason', label: 'Reason', render: (row) => row.reason || html`<span class="dim">No reason given</span>` },
      { key: 'moderator_name', label: 'Moderator', width: '170px', render: (row) => row.moderator_name || 'Unknown' },
      { key: 'expires_at', label: 'Ends', width: '170px', render: endsText },
      { key: 'tools', label: '', width: '80px', render: (row) => liftButton('l' + row.channel_id, 'Unlock ' + channelName(row.channel_id), () => lift('l' + row.channel_id, `/moderation/running/lockdowns/${row.channel_id}/lift`, `Unlock ${channelName(row.channel_id)}?`, 'Its permissions go back to what they were before the lock.', () => {})) },
    ]} />
  </div>`;
}

function LeaderboardTab({ onModerator }) {
  const guild = useGuild();
  const [days, setDays] = useState(0);
  const [bots, setBots] = useState(false);
  const [rows, setRows] = useState(null);
  useEffect(() => {
    setRows(null);
    api.get(`/cases/leaderboard?days=${days}${bots ? '&bots=1' : ''}`).then(setRows).catch((error) => { toastError(error); setRows([]); });
  }, [days, bots, guild.id]);
  return html`<div>
    <div class="row" style=${{ marginBottom: '12px', alignItems: 'flex-end' }}>
      <div style=${{ width: '200px' }}><${Select} label="Period" value=${days} onChange=${setDays} options=${[{ value: 0, label: 'All time' }, { value: 7, label: 'Last 7 days' }, { value: 30, label: 'Last 30 days' }, { value: 90, label: 'Last 90 days' }, { value: 365, label: 'Last year' }]} /></div>
      <div style=${{ marginBottom: '14px' }}><${Check} label="Include bots" value=${bots} onChange=${setBots} /></div>
    </div>
    <p class="muted small">Voided cases and automatic reversals don't count.</p>
    <${DataTable} loading=${rows === null} rows=${rows || []} rowKey="moderator_id" pageSizes=${[25, 50, 100]} columns=${[
      { key: 'rank', label: '#', width: '50px', render: (row) => (rows || []).indexOf(row) + 1 },
      { key: 'moderator_name', label: 'Moderator', sort: true, filter: true, render: (row) => html`<button class="link-btn" onClick=${() => onModerator(row)}>${row.moderator_name}</button>${row.moderator_bot ? html` <span class="badge outline">bot</span>` : ''}` },
      { key: 'total', label: 'Total', sort: true, width: '80px' },
      { key: 'warn', label: 'Warns', sort: true, width: '80px' },
      { key: 'mute', label: 'Mutes', sort: true, width: '80px' },
      { key: 'timeout', label: 'Timeouts', sort: true, width: '90px' },
      { key: 'kick', label: 'Kicks', sort: true, width: '80px' },
      { key: 'ban', label: 'Bans', sort: true, width: '80px' },
      { key: 'lifted', label: 'Lifted', sort: true, width: '80px' },
      { key: 'last_at', label: 'Last action', sort: true, width: '170px', render: (row) => when(row.last_at) },
    ]} />
  </div>`;
}

export default function CasesPage() {
  const start = new URLSearchParams(location.search);
  const [tab, setTab] = useState('cases');
  // Other pages link here with ?member=, ?case=, ?user=, ?moderator= or ?q=.
  const [filters, setFilters] = useState({ ...NO_FILTERS, q: start.get('q') || '', number: start.get('case') || '', user: start.get('user') || '', moderator: start.get('moderator') || '' });
  const [member, setMember] = useState(start.get('member'));
  const [editing, setEditing] = useState(null);
  const [voiding, setVoiding] = useState(null);
  const [version, setVersion] = useState(0);
  const changed = () => { setEditing(null); setVoiding(null); setVersion((n) => n + 1); };
  const restore = async (row) => {
    const sure = await confirmDialog({ title: `Restore case ${row.number}?`, body: 'It counts against the member again.', confirm: 'Restore' });
    if (!sure) return;
    try {
      await api.post(`/cases/${row.number}/restore`);
      toast('Restored.');
      changed();
    } catch (error) {
      toastError(error);
    }
  };
  return html`<${Card} icon="file-text" title="Cases">
    <${Tabs} tabs=${[{ key: 'cases', label: 'Cases' }, { key: 'running', label: 'Running' }, { key: 'leaderboard', label: 'Moderator leaderboard' }]} active=${tab} onChange=${setTab} />
    ${tab === 'cases' && html`<${CasesTab} filters=${filters} setFilters=${setFilters} onMember=${setMember} version=${version} onEdit=${setEditing} onVoid=${setVoiding} onRestore=${restore} />`}
    ${tab === 'running' && html`<${RunningTab} onChanged=${changed} />`}
    ${tab === 'leaderboard' && html`<${LeaderboardTab} onModerator=${(row) => { setFilters({ ...NO_FILTERS, moderator: String(row.moderator_id) }); setTab('cases'); }} />`}
    ${member && html`<${MemberPanel} userId=${member} version=${version} onClose=${() => setMember(null)} onEdit=${setEditing} onVoid=${setVoiding} onRestore=${restore} />`}
    ${editing && html`<${EditReason} row=${editing} onClose=${() => setEditing(null)} onSaved=${changed} />`}
    ${voiding && html`<${VoidCase} row=${voiding} onClose=${() => setVoiding(null)} onSaved=${changed} />`}
  <//>`;
}
