// Proposals: changes to the server put to a vote by members and by moderators.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Col, Empty, Grid, Icon, Loading, Modal, Notice, NumberInput, Pager, RolePicker, SaveBar, SaveButton, Select, SettingsGate, Tabs, TextArea, TextInput, toast, toastError, useGuild, useSettings, when } from '../ui.js';

const STATES = {
  open: { label: 'Voting', badge: 'gold' },
  approved: { label: 'Approved', badge: 'green' },
  rejected: { label: 'Rejected', badge: 'red' },
  withdrawn: { label: 'Withdrawn', badge: 'outline' },
};

function Settings({ form }) {
  return html`
    <${Grid}>
      <${Col}>
        <${Card} icon="thumbs-up" title="Proposal settings" footer=${html`<${SaveButton} form=${form} />`}>
          <p class="muted">A proposal goes to two votes on one post. Members have a set time to vote. Moderators have no deadline, and their vote is decided once more than half of all moderators agree. It passes only if both votes pass.</p>
          <${ChannelPicker} label="Proposal channel" placeholder="Please select a channel" help="The bot needs Add Reactions, Manage Messages and Create Public Threads here." ...${form.bind('channel_id')} />
          <${RolePicker} label="Moderator role" placeholder="Please select a role" help="Its members can submit proposals, and their votes are counted as the moderator vote." ...${form.bind('moderator_role')} />
          <${RolePicker} label="Role to notify" placeholder="None" help="Mentioned with the moderator role when a proposal opens." ...${form.bind('participation_role')} />
          <${TextInput} label="Post title" maxLength=${200} ...${form.bind('title')} />
        <//>
      <//>
      <${Col}>
        <${Card} icon="clock" title="Timing and results" footer=${html`<${SaveButton} form=${form} />`}>
          <${NumberInput} label="Community voting time" addon="hours" min=${0.1} max=${8760} step="any" help="A proposal keeps the time it opened with, so a change here applies to new proposals." ...${form.bind('vote_hours')} />
          <${NumberInput} label="Remind moderators every" addon="hours" min=${0} max=${8760} step=${1} help="Once the community vote is over, moderators who haven't voted get a DM this often. 0 turns reminders off." ...${form.bind('reminder_hours')} />
          <${ChannelPicker} label="Results channel" placeholder="None" help="Each result is posted here when a proposal is approved or rejected." ...${form.bind('results_channel')} />
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />`;
}

function SubmitDialog({ onClose, onDone }) {
  const [content, setContent] = useState('');
  const [credit, setCredit] = useState('');
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState({});
  const send = async () => {
    setBusy(true);
    setErrors({});
    try {
      const row = await api.post('/proposals', { content, credit });
      toast(`Proposal #${row.number} is up for a vote.`);
      onDone();
    } catch (failure) {
      setErrors(failure.fields || {});
      toastError(failure);
    } finally {
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([content, credit]);
  const [start] = useState(snapshot);
  return html`<${Modal} title="Submit a proposal" icon="thumbs-up" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button><button class="btn primary" disabled=${busy || !content.trim()} onClick=${send}>Post it</button></div>`}>
    <${TextArea} label="Proposal" rows=${8} maxLength=${4000} value=${content} onChange=${setContent} error=${errors.content} />
    <${TextInput} label="Credit (optional)" maxLength=${200} placeholder="Who suggested it, or a link to the post" value=${credit} onChange=${setCredit} error=${errors.credit} />
  <//>`;
}

function WithdrawDialog({ row, onClose, onDone }) {
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState({});
  const send = async () => {
    setBusy(true);
    setErrors({});
    try {
      await api.post(`/proposals/${row.number}/withdraw`, { reason });
      toast('Withdrawn.');
      onDone();
    } catch (failure) {
      setErrors(failure.fields || {});
      toastError(failure);
    } finally {
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([reason]);
  const [start] = useState(snapshot);
  return html`<${Modal} title=${`Withdraw proposal #${row.number}?`} icon="thumbs-up" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button><button class="btn danger" disabled=${busy} onClick=${send}>Withdraw</button></div>`}>
    <p class="muted">Both votes stop and the post says it was withdrawn. This can't be undone.</p>
    <${TextArea} label="Reason (optional)" rows=${3} maxLength=${500} value=${reason} onChange=${setReason} error=${errors.reason} help="Shown on the post." />
  <//>`;
}

function waitingText(row) {
  if (row.mod_total === 0) return 'The moderator role has no members, so the moderator vote can\'t finish';
  if (row.mod_total && row.mod_up + row.mod_down === row.mod_total) return 'Moderators are tied. One has to change their vote';
  return 'Waiting on moderators';
}

function Vote({ label, up, down, result }) {
  return html`<span class="muted small"><b>${label}</b> <${Icon} name="thumbs-up" size=${14} /> ${up} <${Icon} name="thumbs-down" size=${14} /> ${down}${result ? ` (${result})` : ''}</span>`;
}

function ProposalList() {
  const guild = useGuild();
  const [state, setState] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(10);
  const [data, setData] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [withdrawing, setWithdrawing] = useState(null);
  const load = () => api.get(`/proposals/list?state=${state}&q=${encodeURIComponent(query.trim())}&page=${page}&size=${size}`).then((fresh) => {
    setData(fresh);
    // The last row of the last page can go while a filter is on. Staying there would say nothing matches while rows exist.
    if (fresh.rows.length === 0 && fresh.total > 0 && page > 1) setPage(Math.ceil(fresh.total / size));
  }).catch((error) => { toastError(error); setData({ rows: [], total: 0 }); });
  useEffect(() => {
    const timer = setTimeout(load, 200);
    return () => clearTimeout(timer);
  }, [state, query, page, size, guild.id]);
  const recheck = async () => {
    try {
      const result = await api.post('/proposals/recheck', {});
      if (result.problem) toast(result.problem, 'bad', 7000);
      else if (result.counted < result.checked) toast(`Counted ${result.counted} of ${result.checked} open proposals. The next check tries the rest again.`, 'bad', 7000);
      else toast(`Counted ${result.checked} open ${result.checked === 1 ? 'proposal' : 'proposals'}.`);
      load();
    } catch (error) {
      toastError(error);
    }
  };
  const pages = data ? Math.max(1, Math.ceil(data.total / size)) : 1;
  const start = data && data.total ? (page - 1) * size + 1 : 0;
  return html`<${Card} icon="list" title="Proposals">
    ${data && data.problem && html`<${Notice} kind="warn">${data.problem}<//>`}
    <div class="row" style=${{ marginBottom: '14px', alignItems: 'flex-end' }}>
      <div style=${{ width: '200px' }}><${Select} label="State" value=${state} onChange=${(value) => { setState(value); setPage(1); }}
        options=${[{ value: '', label: 'All' }, ...Object.entries(STATES).map(([value, info]) => ({ value, label: info.label }))]} /></div>
      <div style=${{ width: '300px' }}><${TextInput} label="Search" placeholder="Text" value=${query} onChange=${(value) => { setQuery(value); setPage(1); }} /></div>
      <div class="table-tools" style=${{ marginBottom: '16px' }}>
        ${guild.access === 'full' && html`<button class="btn" onClick=${recheck}><${Icon} name="refresh-cw" size=${14} /> Count votes now</button>`}
        ${data && data.can_propose && html`<button class="btn primary" onClick=${() => setSubmitting(true)}><${Icon} name="plus" size=${14} /> Submit a proposal</button>`}
      </div>
    </div>
    ${!data && html`<${Loading} />`}
    ${data && data.rows.length === 0 && html`<${Empty}>No proposals match.<//>`}
    <div class="stack">
      ${data && data.rows.map((row) => html`<div class="tile" key=${row.number}>
        <div class="spread">
          <b>#${row.number} from ${row.submitter_name}</b>
          <span class=${'badge ' + STATES[row.state].badge}>${STATES[row.state].label}</span>
        </div>
        <p class="truncate" title=${row.content} style=${{ margin: '8px 0' }}>${row.content}</p>
        ${row.credit && html`<p class="small muted">Credit: ${row.credit}</p>`}
        ${row.status_reason && html`<p class="small muted">${row.status_reason}</p>`}
        <div class="spread">
          <div class="row">
            <${Vote} label="Community" up=${row.community_up} down=${row.community_down} result=${row.community_done ? (row.community_pass ? 'passed' : 'failed') : ''} />
            <${Vote} label="Moderators" up=${row.mod_up} down=${row.mod_down} result=${row.mod_done ? (row.mod_pass ? 'approved' : 'rejected') : ''} />
            ${row.state === 'open' && !row.community_done && html`<span class="small dim">Community vote ends ${when(row.ends_at)}</span>`}
            ${row.state === 'open' && row.community_done && html`<span class="small dim">${waitingText(row)}</span>`}
            ${row.state !== 'open' && row.finished_at && html`<span class="small dim">Finished ${when(row.finished_at)}</span>`}
            ${row.url && html`<a class="small" href=${row.url} target="_blank" rel="noopener noreferrer">Post</a>`}
            ${row.thread_url && html`<a class="small" href=${row.thread_url} target="_blank" rel="noopener noreferrer">Discussion</a>`}
          </div>
          ${row.state === 'open' && row.url && row.can_withdraw && html`<button class="btn small danger" onClick=${() => setWithdrawing(row)}>Withdraw</button>`}
        </div>
      </div>`)}
    </div>
    ${data && html`<div class="table-foot">
      <span class="muted small">Showing ${start} to ${Math.min(start + size - 1, data.total)} of ${data.total} records</span>
      <${Pager} page=${page} pages=${pages} onPage=${setPage} />
    </div>`}
    ${submitting && html`<${SubmitDialog} onClose=${() => setSubmitting(false)} onDone=${() => { setSubmitting(false); load(); }} />`}
    ${withdrawing && html`<${WithdrawDialog} row=${withdrawing} onClose=${() => setWithdrawing(null)} onDone=${() => { setWithdrawing(null); load(); }} />`}
  <//>`;
}

function SettingsTab() {
  const form = useSettings('proposals');
  return html`<${SettingsGate} form=${form}>${() => html`<${Settings} form=${form} />`}<//>`;
}

export default function ProposalsPage() {
  const guild = useGuild();
  const [tab, setTab] = useState('list');
  const tabs = [{ key: 'list', label: 'Proposals' }];
  if (guild.access === 'full') tabs.push({ key: 'settings', label: 'Settings' });
  return html`<div>
    <${Tabs} tabs=${tabs} active=${tab} onChange=${setTab} />
    ${tab === 'list' && html`<${ProposalList} />`}
    ${tab === 'settings' && html`<${SettingsTab} />`}
  </div>`;
}
