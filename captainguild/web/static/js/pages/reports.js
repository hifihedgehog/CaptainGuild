import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import {
  Card, ChannelPicker, Col, DurationInput, Empty, Grid, Icon, Loading, Modal, Notice, NumberInput, Pager, RolePicker, SaveBar, SaveButton, Select, SettingsGate, Switch, Tabs, TextArea, TextInput,
  channelName, confirmDialog, toast, toastError, useGuild, useSettings, when,
} from '../ui.js';

const PAGE_SIZES = [10, 25, 50, 100];
const STATES = { open: { label: 'Open', badge: 'blue' }, actioned: { label: 'Actioned', badge: 'green' }, false_positive: { label: 'False positive', badge: 'red' } };
const REVIEW = { active: { label: 'In review', badge: 'blue' }, done: { label: 'Finished', badge: 'green' }, dismissed: { label: 'Dismissed', badge: 'outline' } };
const DEFAULT_LADDER = [
  { action: 'warn', seconds: 0 }, { action: 'mute', seconds: 86400 }, { action: 'mute', seconds: 259200 }, { action: 'tempban', seconds: 604800 }, { action: 'ban', seconds: 0 },
];
const LADDER_ACTIONS = [
  { value: 'warn', label: 'Warning' }, { value: 'mute', label: 'Mute' }, { value: 'timeout', label: 'Timeout' }, { value: 'kick', label: 'Kick' }, { value: 'tempban', label: 'Temporary ban' }, { value: 'ban', label: 'Permanent ban' },
];
const TIMED = new Set(['mute', 'timeout', 'tempban']);

function sizeText(bytes) {
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function usePaged(path, filters, version) {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(null);
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(PAGE_SIZES[0]);
  const latest = useRef(0);
  const key = JSON.stringify(filters);
  useEffect(() => { setPage(1); }, [key]);
  useEffect(() => {
    const mine = ++latest.current;
    const timer = setTimeout(() => {
      const query = new URLSearchParams({ page, size });
      for (const [name, value] of Object.entries(filters)) if (value) query.set(name, value);
      api.get(`${path}?${query}`)
        .then((fresh) => {
          if (mine !== latest.current) return;
          setData(fresh);
          setFailed(null);
          // The last row of the last page can go while a filter is on. Staying there would say nothing matches while rows exist.
          if (fresh.rows.length === 0 && fresh.total > 0 && page > 1) setPage(Math.ceil(fresh.total / size));
        })
        .catch((error) => { if (mine === latest.current) setFailed(error); });
    }, 200);
    return () => clearTimeout(timer);
  }, [key, page, size, version, guild.id]);
  return { data, failed, page, setPage, size, setSize };
}

function TableTools({ table }) {
  return html`<div class="table-tools"><span>Records:</span>
    <select aria-label="Records per page" value=${table.size} onChange=${(e) => { table.setSize(Number(e.currentTarget.value)); table.setPage(1); }}>
      ${PAGE_SIZES.map((n) => html`<option key=${n} value=${n}>${n}</option>`)}</select></div>`;
}

function TableFoot({ table }) {
  const total = table.data ? table.data.total : 0;
  const first = total ? (table.page - 1) * table.size + 1 : 0;
  const last = Math.min(table.page * table.size, total);
  return html`<div class="table-foot">
    <span class="muted small">Showing ${first} to ${last} of ${total} records</span>
    <${Pager} page=${table.page} pages=${Math.max(1, Math.ceil(total / table.size))} onPage=${table.setPage} />
  </div>`;
}

function Fact({ label, children }) {
  return html`<div><div class="dim small">${label}</div><div class="report-fact">${children}</div></div>`;
}

function ReportDetail({ id, onClose, onChanged }) {
  const guild = useGuild();
  const [row, setRow] = useState(null);
  const [busy, setBusy] = useState(false);
  const [picked, setPicked] = useState('');
  const load = () => api.get(`/reports/${id}`).then((fresh) => { setRow(fresh); setPicked(fresh.case_number || ''); }).catch((error) => { toastError(error); onClose(); });
  useEffect(() => { load(); }, [id]);
  const act = async (work, done) => {
    setBusy(true);
    try {
      await work();
      toast(done);
      await load();
      onChanged();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  if (!row) return html`<${Modal} title="Report" onClose=${onClose} large><${Loading} /><//>`;
  const open = row.state === 'open';
  const setState = (state, done) => act(() => api.post(`/reports/${id}/state`, { state }), done);
  const linkCase = () => act(() => api.post(`/reports/${id}/case`, { case: picked || null }), picked ? `Linked case ${picked}.` : 'Case link removed.');
  const canManage = guild.access === 'full';
  const remove = async () => {
    const sure = await confirmDialog({ title: `Delete report ${row.number}?`, body: 'This deletes the record and its post in the reports channel. What was copied to the review thread stays there. This can\'t be undone.', confirm: 'Delete', danger: true });
    if (!sure) return;
    setBusy(true);
    try {
      const result = await api.del(`/reports/${id}`);
      toast(result.left ? 'Deleted. The bot couldn\'t remove the post, so it stays in the channel.' : 'Deleted.');
      onChanged();
      onClose();
    } catch (error) {
      toastError(error);
      setBusy(false);
    }
  };
  const openReview = async () => {
    if (row.thread_id) {
      const sure = await confirmDialog({
        title: 'Restart this review?',
        body: `The checklist in ${row.target_name}'s thread goes back to step one, and the moderator role is pinged.`,
        confirm: 'Restart review',
      });
      if (!sure) return;
    }
    setBusy(true);
    try {
      await api.post('/reports/review', { user_id: row.target_id, report_id: row.id });
      toast('The review is open.');
      await load();
      onChanged();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const caseOptions = [{ value: '', label: 'No linked case' }, ...row.cases.map((c) => ({ value: c.number, label: `Case ${c.number}: ${c.action}${c.reason ? ', ' + c.reason.slice(0, 60) : ''}` }))];
  if (row.case_number && !row.cases.some((c) => c.number === row.case_number)) caseOptions.push({ value: row.case_number, label: `Case ${row.case_number}, no longer on this member's record` });
  return html`<${Modal} large icon="message-square-warning" title=${`Report ${row.number}`} onClose=${onClose} footer=${html`
    ${open && html`<button class="btn success" disabled=${busy} onClick=${() => setState('actioned', 'Marked as actioned.')}>Actioned</button>
      <button class="btn danger" disabled=${busy} onClick=${() => setState('false_positive', 'Marked as a false positive.')}>False Positive</button>`}
    ${!open && canManage && html`<button class="btn" disabled=${busy} onClick=${() => setState('open', 'Reopened.')}>Reopen</button>`}
    ${canManage && html`<button class="btn danger" disabled=${busy} onClick=${remove}>Delete</button>`}
    <div class="right"><button class="btn primary" disabled=${busy} onClick=${openReview}><${Icon} name="message-square" /> ${row.thread_id ? 'Restart review' : 'Open review thread'}</button></div>`}>
    <div class="row" style=${{ marginBottom: '14px' }}>
      <span class=${'badge ' + STATES[row.state].badge}>${STATES[row.state].label}</span>
      ${row.handled_name && html`<span class="muted small">by ${row.handled_name}, ${when(row.handled_at)}</span>`}
      ${row.reset_name && html`<span class="muted small">reopened by ${row.reset_name}</span>`}
    </div>
    <div class="report-facts">
      <${Fact} label="Reported">${row.target_name} <span class="dim mono small">${row.target_id}</span><//>
      <${Fact} label="Reported by">${row.reporter_name} <span class="dim mono small">${row.reporter_id}</span><//>
      <${Fact} label="Filed">${when(row.created_at)}<//>
      ${row.kind === 'message' && html`
        <${Fact} label="Channel">#${channelName(guild, row.channel_id)}<//>
        <${Fact} label="Message ID"><span class="mono small">${row.message_id}</span><//>
        <${Fact} label="Message sent">${when(row.message_created_at)}<//>
        <${Fact} label="Attachments">${row.attachment_count}<//>
        <${Fact} label="Reactions">${row.reaction_count}<//>`}
    </div>
    <h4>Reason</h4>
    <p class="report-text">${row.reason}</p>
    ${row.also_count > 0 && html`<h4>Also reported by</h4>
      <ul class="report-files">${row.also_reported.map((person) => html`<li key=${person.id}>${person.name} <span class="dim mono small">${person.id}</span><div class="report-text">${person.reason}</div></li>`)}</ul>
      ${row.also_count > row.also_reported.length && html`<p class="muted small">And ${row.also_count - row.also_reported.length} more.</p>`}`}
    ${row.kind === 'message' && html`<h4>Message</h4><p class="report-text">${row.content_hidden ? 'From a channel you can\'t read.' : row.content || 'No text'}</p>`}
    ${row.files.length > 0 && html`<h4>Files</h4>
      <ul class="report-files">${row.files.map((file) => html`<li key=${file.name}>
        <${Icon} name="paperclip" /> ${file.url ? html`<a href=${file.url} target="_blank" rel="noopener noreferrer">${file.filename}</a>` : file.filename} <span class="dim small">${sizeText(file.size)}, ${file.copied ? 'kept with the report post' : 'not copied'}</span></li>`)}</ul>`}
    <h4>Links</h4>
    <div class="row" style=${{ marginBottom: '16px' }}>
      ${row.post_url && html`<a class="btn small" href=${row.post_url} target="_blank" rel="noopener noreferrer"><${Icon} name="external-link" /> Report post</a>`}
      ${row.jump_url && html`<a class="btn small" href=${row.jump_url} target="_blank" rel="noopener noreferrer"><${Icon} name="external-link" /> ${row.kind === 'message' ? 'Reported message' : 'Where it was filed'}</a>`}
      ${row.thread_url && html`<a class="btn small" href=${row.thread_url} target="_blank" rel="noopener noreferrer"><${Icon} name="external-link" /> Review thread</a>`}
      ${row.has_moderation && html`<a class="btn small" href=${`/dashboard/${guild.id}/cases?member=${row.target_id}`} data-nav="1"><${Icon} name="file-text" /> Their cases</a>`}
      <a class="btn small" href=${`/dashboard/${guild.id}/logsearch?user=${row.target_id}`} data-nav="1"><${Icon} name="search" /> Their logs</a>
    </div>
    <h4>Linked case</h4>
    <div class="report-case">
      ${row.has_moderation
        ? html`<${Select} label="Case" value=${picked} options=${caseOptions} onChange=${setPicked} />`
        : html`<${NumberInput} label="Case number" min=${1} value=${picked || null} onChange=${(v) => setPicked(v || '')} />`}
      <button class="btn" disabled=${busy || String(picked) === String(row.case_number || '')} onClick=${linkCase}>Save link</button>
      ${row.case_number && row.has_moderation ? html`<a class="btn small ghost" href=${`/dashboard/${guild.id}/cases?case=${row.case_number}`} data-nav="1">Open case ${row.case_number}</a>` : ''}
    </div>
    ${row.has_moderation && row.cases.length === 0 && html`<p class="muted small">This member has no cases yet.</p>`}
  <//>`;
}

function ReportsTab() {
  const guild = useGuild();
  const [filters, setFilters] = useState({ state: '', target: '', reporter: '', q: '' });
  const [version, setVersion] = useState(0);
  const [viewing, setViewing] = useState(null);
  const table = usePaged('/reports', filters, version);
  const change = (key) => (event) => setFilters({ ...filters, [key]: event.currentTarget.value });
  const counts = table.data ? table.data.counts : null;
  return html`<div>
    ${counts && html`<div class="stat-tiles">
      ${Object.entries(STATES).map(([key, state]) => html`<button key=${key} class=${'stat-tile report-tile' + (filters.state === key ? ' on' : '')} onClick=${() => setFilters({ ...filters, state: filters.state === key ? '' : key })}>
        <b>${counts[key]}</b><span class="muted small">${state.label}</span></button>`)}
    </div>`}
    <${TableTools} table=${table} />
    ${table.failed && html`<${Notice} kind="bad">${table.failed.message}<//>`}
    <div class="table-wrap"><table>
      <thead>
        <tr><th style=${{ width: '56px' }}>No.</th><th style=${{ width: '130px' }}>State</th><th style=${{ width: '180px' }}>Reported</th><th style=${{ width: '180px' }}>Reported by</th><th>Reason</th><th style=${{ width: '170px' }}>Filed</th><th style=${{ width: '80px' }}>Case</th><th style=${{ width: '70px' }}></th></tr>
        <tr>
          <th></th>
          <th><select aria-label="Filter by state" value=${filters.state} onChange=${change('state')}><option value="">All</option>${Object.entries(STATES).map(([key, state]) => html`<option key=${key} value=${key}>${state.label}</option>`)}</select></th>
          <th><input type="text" placeholder="Filter by Reported" aria-label="Filter by reported person, name or ID" value=${filters.target} onInput=${change('target')} /></th>
          <th><input type="text" placeholder="Filter by Reporter" aria-label="Filter by reporter, name or ID" value=${filters.reporter} onInput=${change('reporter')} /></th>
          <th><input type="text" placeholder="Filter by Reason or Message" aria-label="Filter by reason or message text" value=${filters.q} onInput=${change('q')} /></th>
          <th></th><th></th><th></th>
        </tr>
      </thead>
      <tbody>
        ${!table.data && !table.failed && html`<tr><td colspan="8"><${Loading} /></td></tr>`}
        ${table.data && table.data.rows.length === 0 && html`<tr><td colspan="8"><${Empty}>No matching records<//></td></tr>`}
        ${table.data && table.data.rows.map((row) => html`<tr key=${row.id}>
          <td>${row.number}</td>
          <td><span class=${'badge ' + STATES[row.state].badge}>${STATES[row.state].label}</span><div class="dim small">${row.kind === 'message' ? 'Message' : 'Member'}</div></td>
          <td title=${String(row.target_id)}>${row.target_name}</td>
          <td title=${String(row.reporter_id)}>${row.reporter_name}</td>
          <td><div class="truncate">${row.reason}</div></td>
          <td class="small">${when(row.created_at)}</td>
          <td>${row.case_number ? html`<a href=${`/dashboard/${guild.id}/cases?case=${row.case_number}`} data-nav="1">${row.case_number}</a>` : ''}</td>
          <td><button class="btn small outline" onClick=${() => setViewing(row.id)}>View</button></td>
        </tr>`)}
      </tbody>
    </table></div>
    <${TableFoot} table=${table} />
    ${viewing && html`<${ReportDetail} id=${viewing} onClose=${() => setViewing(null)} onChanged=${() => setVersion((n) => n + 1)} />`}
  </div>`;
}

function ThreadsTab() {
  const [filters, setFilters] = useState({ member: '', status: '' });
  const [version, setVersion] = useState(0);
  const [pings, setPings] = useState(null);
  const guild = useGuild();
  const table = usePaged('/reports/threads', filters, version);
  useEffect(() => { api.get('/reports/pings').then(setPings).catch(() => setPings([])); }, [guild.id, version]);
  const change = (key) => (event) => setFilters({ ...filters, [key]: event.currentTarget.value });
  const steps = table.data ? table.data.steps : 5;
  const restart = async (row) => {
    const sure = await confirmDialog({
      title: 'Start this review?',
      body: `The checklist in ${row.user_name || 'this member'}'s thread starts at step one, even if it was finished or dismissed. The moderator role is pinged.`,
      confirm: 'Start review',
    });
    if (!sure) return;
    try {
      await api.post('/reports/review', { user_id: row.user_id });
      toast('The review is open.');
      setVersion((n) => n + 1);
    } catch (error) {
      toastError(error);
    }
  };
  return html`<div>
    <p class="muted">Each reported member has one thread in the review forum. Reviews are started by a report, by the review command, or from here.</p>
    <${TableTools} table=${table} />
    ${table.failed && html`<${Notice} kind="bad">${table.failed.message}<//>`}
    <div class="table-wrap"><table>
      <thead>
        <tr><th>Member</th><th style=${{ width: '200px' }}>Review</th><th style=${{ width: '140px' }}>Moderator pings</th><th style=${{ width: '170px' }}>Last change</th><th style=${{ width: '230px' }}></th></tr>
        <tr>
          <th><input type="text" placeholder="Filter by Member" aria-label="Filter by member, name or ID" value=${filters.member} onInput=${change('member')} /></th>
          <th><select aria-label="Filter by review state" value=${filters.status} onChange=${change('status')}>
            <option value="">All</option>${Object.entries(REVIEW).map(([key, state]) => html`<option key=${key} value=${key}>${state.label}</option>`)}<option value="none">No review yet</option></select></th>
          <th></th><th></th><th></th>
        </tr>
      </thead>
      <tbody>
        ${!table.data && !table.failed && html`<tr><td colspan="5"><${Loading} /></td></tr>`}
        ${table.data && table.data.rows.length === 0 && html`<tr><td colspan="5"><${Empty}>No matching records<//></td></tr>`}
        ${table.data && table.data.rows.map((row) => html`<tr key=${row.thread_id}>
          <td>${row.user_name || row.user_id} <div class="dim mono small">${row.user_id}</div></td>
          <td>${row.status
            ? html`<span class=${'badge ' + REVIEW[row.status].badge}>${REVIEW[row.status].label}</span>${row.status === 'active' ? html` <span class="small muted">Step ${row.current_step + 1} of ${steps}</span>` : ''}`
            : html`<span class="muted small">No review yet</span>`}</td>
          <td>${row.pings}</td>
          <td class="small">${when(row.updated_at)}</td>
          <td><div class="row">
            <a class="btn small" href=${row.url} target="_blank" rel="noopener noreferrer"><${Icon} name="external-link" /> Thread</a>
            <button class="btn small outline" onClick=${() => restart(row)}>Start review</button></div></td>
        </tr>`)}
      </tbody>
    </table></div>
    <${TableFoot} table=${table} />
    ${pings && pings.length > 0 && html`<div style=${{ marginTop: '20px' }}>
      <h4>Most moderator pings</h4>
      <p class="muted small">How often members without Manage Server called for a moderator.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Member</th><th style=${{ width: '100px' }}>Count</th><th style=${{ width: '170px' }}>Last</th></tr></thead>
        <tbody>${pings.map((row) => html`<tr key=${row.user_id}><td>${row.name}</td><td>${row.count}</td><td class="small">${when(row.last_at)}</td></tr>`)}</tbody>
      </table></div>
    </div>`}
  </div>`;
}

function LadderEditor({ form }) {
  const rows = form.data.ladder;
  const set = (next) => form.set('ladder', next);
  const change = (index, patch) => set(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  const move = (index, by) => {
    const next = [...rows];
    const [row] = next.splice(index, 1);
    next.splice(index + by, 0, row);
    set(next);
  };
  return html`<div>
    ${rows.map((row, index) => html`<div class="field-edit" key=${index}>
      <div class="spread"><b>${index === 0 ? 'First offense' : `Step ${index + 1}`}</b>
        <div class="row">
          <button class="icon-btn" disabled=${index === 0} aria-label="Move step up" onClick=${() => move(index, -1)}><${Icon} name="arrow-up" /></button>
          <button class="icon-btn" disabled=${index === rows.length - 1} aria-label="Move step down" onClick=${() => move(index, 1)}><${Icon} name="arrow-down" /></button>
          <button class="icon-btn danger" aria-label="Remove step" onClick=${() => set(rows.filter((_, i) => i !== index))}><${Icon} name="trash-2" /></button>
        </div></div>
      <${Select} label="Action" value=${row.action} options=${LADDER_ACTIONS} onChange=${(action) => change(index, { action, seconds: TIMED.has(action) ? (row.seconds || 86400) : 0 })} />
      ${TIMED.has(row.action) && html`<${DurationInput} label="Length" help=${row.action === 'mute' ? 'Zero means until someone lifts it.' : ''} units=${['d', 'h', 'm']} value=${row.seconds} onChange=${(seconds) => change(index, { seconds })} />`}
    </div>`)}
    ${form.errors.ladder && html`<div class="error">${form.errors.ladder}</div>`}
    <div class="row">
      <button class="btn" disabled=${rows.length >= 12} onClick=${() => set([...rows, { action: 'warn', seconds: 0 }])}><${Icon} name="plus" /> Add step</button>
      <button class="btn ghost" onClick=${() => set(DEFAULT_LADDER)}>Use the default ladder</button>
    </div>
  </div>`;
}

function packText(rows) {
  return rows.map((row) => (row.emoji ? `${row.emoji} ${row.name}` : row.name)).join('\n');
}

function packRows(text) {
  const rows = [];
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const match = /^(\S+)\s+(.+)$/.exec(line);
    // A first word with no letters or digits in it is the emoji.
    if (match && !/[\p{L}\p{N}]/u.test(match[1])) rows.push({ emoji: match[1], name: match[2].trim() });
    else rows.push({ emoji: '', name: line });
  }
  return rows;
}

function PackEditor({ form }) {
  const [text, setText] = useState(packText(form.data.fun_roles));
  useEffect(() => {
    if (JSON.stringify(packRows(text)) !== JSON.stringify(form.data.fun_roles)) setText(packText(form.data.fun_roles));
  }, [form.data.fun_roles]);
  const count = packRows(text).length;
  return html`<div>
    <${TextArea} label="Roles" rows=${8} counter=${false} help="One role per line. Put an emoji first if the role has one." placeholder=${'\u{1F3C6} Contest Winner\n\u{1F41B} Bug Finder'} error=${form.errors.fun_roles}
      value=${text} onChange=${(value) => { setText(value); form.set('fun_roles', packRows(value)); }} />
    <p class="muted small">${count} ${count === 1 ? 'role' : 'roles'} in the pack.</p>
  </div>`;
}

function SettingsTab() {
  const form = useSettings('reports');
  return html`<${SettingsGate} form=${form}>${() => {
    const save = html`<${SaveButton} form=${form} />`;
    return html`
    <${Grid}>
      <${Col}>
        <${Card} icon="message-square-warning" title="Reports" footer=${save}>
          <h2>Reports</h2>
          <p class="muted">Members report with the Report Message and Report User menus or the report command. Reports go to this channel with buttons for staff.</p>
          <${ChannelPicker} label="Reports channel" ...${form.bind('report_channel')} />
          <${RolePicker} label="Alert role" help="Pinged for a new report, at most once a minute. Another report of the same message joins the first with no ping." placeholder="No ping" ...${form.bind('alert_role')} />
          <${RolePicker} label="Banned role" help="Members with this role can't file reports." placeholder="Nobody is barred" ...${form.bind('banned_role')} />
          <${Switch} label="Report self" help="Members can report themselves." ...${form.bind('report_self')} />
          <${Switch} label="Report bots" help="Members can report bots and their messages." ...${form.bind('report_bots')} />
          <${Switch} label="Report admins" help="Members can report administrators and their messages." ...${form.bind('report_admins')} />
          <${NumberInput} label="Cooldown" addon="seconds" min=${0} max=${3600} help="Time between reports from one member, who can file 20 in an hour. Manage Server skips both." ...${form.bind('cooldown_seconds')} />
        <//>
        <${Card} icon="scroll-text" title="Punishment notice" footer=${save}>
          <h2>Punishment notice</h2>
          <p class="muted">Staff fill in a form with the punishment notice command, check a preview, and send this text to the ticket.</p>
          <${TextInput} label="Link to the rules" placeholder="https://discord.com/channels/..." ...${form.bind('notice_rules_link')} />
          <${TextArea} label="Notice" rows=${14} maxLength=${4000} ...${form.bind('notice_template')} />
          <p class="muted small">The form fills in <code>{user}</code> <code>{action}</code> <code>{rules}</code> <code>{recap}</code> <code>{consequence}</code> <code>{notes}</code> <code>{rules_link}</code>. A paragraph holding an empty <code>{recap}</code>, <code>{notes}</code> or <code>{rules_link}</code> is left out.</p>
        <//>
      <//>
      <${Col}>
        <${Card} icon="message-square" title="Review threads" footer=${save}>
          <h2>Review threads</h2>
          <p class="muted">Each reported member gets one thread in this forum, named with their ID, holding their record, the reports and a five-step checklist. Threads already named that way are found and reused.</p>
          <${ChannelPicker} label="Review forum" types=${[15]} placeholder="Please select a forum" ...${form.bind('forum_channel')} />
          <${RolePicker} label="Moderator role" help="Pinged in the thread when a report starts a review. Holders count as staff for reports and voice." placeholder="No ping" ...${form.bind('moderator_role')} />
          <${Switch} label="Open a review for each report" help="When off, reviews start only when staff ask." ...${form.bind('auto_review')} />
          <${TextInput} label="Ticket command" help="Shown at the ticket step. {user} becomes the member." ...${form.bind('ticket_command')} />
          <${TextInput} label="Moderator ping text" help="Members typing this are counted on their record, as are real pings of the moderator role. Empty counts only real pings." ...${form.bind('mod_ping_text')} />
          <${ChannelPicker} label="Older log channel" help="When the log store falls short, gathering looks for the member's entries in this channel's last 1,000 posts, and a new report looks for its message's card in the last 500." placeholder="None" ...${form.bind('log_channel_fallback')} />
        <//>
        <${Card} icon="gavel" title="Escalation ladder" footer=${save}>
          <h2>Escalation ladder</h2>
          <p class="muted">The checklist recommends the step after the highest one this member's record has reached.</p>
          <${LadderEditor} form=${form} />
        <//>
        <${Card} icon="sparkles" title="Fun role pack" footer=${save}>
          <h2>Fun role pack</h2>
          <p class="muted">A set of vanity roles. The fun roles commands create them as an alphabetical block below a role you pick and delete them again, with a dry run first.</p>
          <${PackEditor} form=${form} />
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />`;
  }}<//>`;
}

export default function ReportsPage() {
  const guild = useGuild();
  const [tab, setTab] = useState('reports');
  const tabs = [{ key: 'reports', label: 'Reports' }, { key: 'threads', label: 'Review threads' }];
  if (guild.access === 'full') tabs.push({ key: 'settings', label: 'Settings' });
  return html`<div>
    <${Tabs} tabs=${tabs} active=${tab} onChange=${setTab} />
    ${tab === 'reports' && html`<${Card} icon="message-square-warning" title="Reports"><${ReportsTab} /><//>`}
    ${tab === 'threads' && html`<${Card} icon="message-square" title="Review threads"><${ThreadsTab} /><//>`}
    ${tab === 'settings' && html`<${SettingsTab} />`}
  </div>`;
}
