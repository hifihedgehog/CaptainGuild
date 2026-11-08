import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import {
  Card, ChannelPicker, Col, DataTable, Empty, Grid, Icon, Loading, MemberPicker, Modal, Notice, NumberInput, Pager, RolePicker, SaveBar, SaveButton, SettingsGate, Switch, Tabs, TextInput,
  channelName, confirmDialog, sameId, toast, toastError, useGuild, useSettings, when,
} from '../ui.js';

const PAGE_SIZES = [10, 25, 50, 100];
const ACTION_NAMES = { mute: 'Server mute', deafen: 'Server deafen', block: 'Rejoin block' };
const ACCESS = { pending: { label: 'Waiting', badge: 'blue' }, approved: { label: 'Approved', badge: 'green' }, denied: { label: 'Denied', badge: 'red' }, revoked: { label: 'Revoked', badge: 'red' } };
const VOTES = { open: 'blue', applied: 'red', rejected: 'outline', vetoed: 'outline', expired: 'outline' };
const POWER = new Set(['administrator', 'manage_guild', 'manage_channels', 'manage_roles', 'manage_messages', 'manage_expressions', 'manage_nicknames', 'manage_webhooks', 'ban_members', 'kick_members', 'moderate_members']);

function RoleWarning({ id, handedOut }) {
  const guild = useGuild();
  const role = guild.roles.find((r) => sameId(r.id, id));
  if (!id || !role) return null;
  if (role.position >= guild.me.top_role_position) return html`<${Notice} kind="warn">This role sits at or above the bot's top role, so the bot can't give it. Move the bot's role above it.<//>`;
  if (handedOut && role.permissions.some((p) => POWER.has(p))) return html`<${Notice} kind="warn">This role holds moderation permissions, and the bot won't hand it out while elevated roles are off on the Commands page. Pick a plain role.<//>`;
  return null;
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
        .then((fresh) => { if (mine === latest.current) { setData(fresh); setFailed(null); } })
        .catch((error) => { if (mine === latest.current) setFailed(error); });
    }, 200);
    return () => clearTimeout(timer);
  }, [key, page, size, version, guild.id]);
  return { data, failed, page, setPage, size, setSize };
}

function TableTools({ table, children }) {
  return html`<div class="table-tools"><span>Records:</span>
    <select aria-label="Records per page" value=${table.size} onChange=${(e) => { table.setSize(Number(e.currentTarget.value)); table.setPage(1); }}>
      ${PAGE_SIZES.map((n) => html`<option key=${n} value=${n}>${n}</option>`)}</select>${children}</div>`;
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

function PostButton() {
  const [channel, setChannel] = useState(null);
  const [busy, setBusy] = useState(false);
  const post = async () => {
    setBusy(true);
    try {
      await api.post('/voice/button', { channel_id: channel });
      toast('The request button is posted.');
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  return html`<div class="field-edit">
    <${ChannelPicker} label="Post the request button" help="Post it again after changing the requirements, so its text stays right." value=${channel} onChange=${setChannel} />
    <button class="btn success" disabled=${!channel || busy} onClick=${post}><${Icon} name="send" /> Post button</button>
  </div>`;
}

const TIERS = [
  ['Temp mute, deafen and disconnect', 'Any length up to 7 days', 'Default length at most, logged', 'No'],
  ['Release a voice action', 'Yes', 'No', 'No'],
  ['Handle VC reports', 'Yes', 'No', 'No'],
  ['Approve access requests', 'Yes', 'Yes', 'No'],
  ['Deny access requests', 'Yes', 'No', 'No'],
  ['Grant access outright', 'Yes', 'Yes, logged, unless a moderator denied or revoked it', 'No'],
  ['Revoke access', 'Yes', 'No', 'No'],
  ['Mini mute', 'Yes, with no vote', 'No', 'Yes, by the vote count above'],
  ['Veto a vote, lift a mini mute', 'Yes', 'No', 'No'],
];

function SettingsTab() {
  const form = useSettings('voice');
  return html`<${SettingsGate} form=${form}>${() => {
    const save = html`<${SaveButton} form=${form} />`;
    return html`
    <${Grid}>
      <${Col}>
        <${Card} icon="mic" title="Voice moderation" footer=${save}>
          <h2>Voice moderation</h2>
          <p class="muted">Temporary server mutes, deafens and rejoin blocks with /vc_mute, /vc_deafen, /vc_disconnect and the Voice Actions menu. Members report with the VC Report menu or /vc_report, and those reports go to the reports channel with every other report.</p>
          <${RolePicker} label="Moderator role" help="Its holders are voice moderators, as are Manage Server and the bot manager role." placeholder="None" ...${form.bind('moderator_role')} />
          <${ChannelPicker} label="VC veteran log" help="Every action a VC veteran takes is posted here. Without one, veterans still act and nothing is posted." ...${form.bind('report_channel')} />
          <${NumberInput} label="Default length" addon="minutes" min=${1} max=${10080} help="Used by the Voice Actions menu and by commands given no length." ...${form.bind('temp_minutes')} />
        <//>
        <${Card} icon="user-check" title="VC veterans" footer=${save}>
          <h2>VC veterans</h2>
          <p class="muted">Trusted members who moderate voice only. They can't act on staff, bots or each other, and they're capped at the default length.</p>
          <${RolePicker} label="VC veteran role" placeholder="None" ...${form.bind('veteran_role')} />
        <//>
        <${Card} icon="shield-check" title="Voice access" footer=${save}>
          <h2>Voice access</h2>
          <p class="muted">Members press a button to ask for this role. Requests that meet the requirements go to staff for one approval, which lasts until revoked and comes back if the member leaves and returns.</p>
          <${RolePicker} label="Voice access role" help="Gate your voice channels with it." placeholder="None" ...${form.bind('access_role')} />
          <${RoleWarning} id=${form.data.access_role} handedOut />
          <${ChannelPicker} label="Approval channel" help="Requests, and grants and revokes, land here." ...${form.bind('approval_channel')} />
          <${NumberInput} label="Messages required" min=${0} max=${1000000} ...${form.bind('min_messages')} />
          <${NumberInput} label="Days on the server required" min=${0} max=${3650} ...${form.bind('min_days')} />
          <${PostButton} />
        <//>
      <//>
      <${Col}>
        <${Card} icon="volume-x" title="Mini mute" footer=${save}>
          <h2>Mini mute</h2>
          <p class="muted">Server Veterans can apply a role for a set length, without being moderators. They can't target staff, other veterans or bots, and nobody picks the length. A mini mute leaves no mark on a member's record.</p>
          <${RolePicker} label="Server Veteran role" placeholder="None" ...${form.bind('server_veteran_role')} />
          <${RolePicker} label="Mini mute role" help="Give it channel overwrites that deny Send Messages, Add Reactions and Speak. It can be the server's mute role. The role then stays on until no mute of either kind holds it." placeholder="None" ...${form.bind('mini_mute_role')} />
          <${RoleWarning} id=${form.data.mini_mute_role} handedOut />
          <${NumberInput} label="Length" addon="minutes" min=${1} max=${1440} ...${form.bind('mini_mute_minutes')} />
          <${ChannelPicker} label="Log channel" help="Mini mutes and their votes land here. Server Veterans need to see it." ...${form.bind('mini_mute_log_channel')} />
          <${NumberInput} label="Votes required" min=${1} max=${10} help="One applies at once. More opens a vote, counting the veteran who asked. Moderators never need a vote." ...${form.bind('mini_mute_votes_required')} />
          <${NumberInput} label="Vote stays open for" addon="minutes" min=${1} max=${1440} ...${form.bind('mini_mute_vote_window')} />
          <${Switch} label="Record mini mutes as cases" help="Off, a mini mute writes only to its log channel and this page. On, it also goes in the mod log, the member's case list and moderator rankings." ...${form.bind('mini_mute_cases')} />
        <//>
        <${Card} icon="users" title="Who can do what">
          <div class="table-wrap"><table>
            <thead><tr><th></th><th>Moderator</th><th>VC veteran</th><th>Server Veteran</th></tr></thead>
            <tbody>${TIERS.map((row) => html`<tr key=${row[0]}><td>${row[0]}</td><td class="small">${row[1]}</td><td class="small">${row[2]}</td><td class="small">${row[3]}</td></tr>`)}</tbody>
          </table></div>
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />`;
  }}<//>`;
}

function endsText(row) {
  if (!row.expires_at || row.expires_at * 1000 <= Date.now()) return 'Clears when they next connect';
  return when(row.expires_at);
}

function ActionsTab() {
  const guild = useGuild();
  const [rows, setRows] = useState(null);
  const load = () => api.get('/voice/actions').then((fresh) => setRows(fresh.map((row) => ({ ...row, row_key: `${row.user_id}:${row.action}` })))).catch((error) => { toastError(error); setRows([]); });
  useEffect(() => { load(); }, [guild.id]);
  const release = async (row) => {
    const sure = await confirmDialog({ title: `Release ${row.name}?`, body: 'Every temporary voice action on this member ends now.', confirm: 'Release' });
    if (!sure) return;
    try {
      const result = await api.post(`/voice/actions/${row.user_id}/release`);
      toast(result.cleared_now ? 'Released.' : "Released. They aren't in voice, so it clears when they next connect.");
      load();
    } catch (error) {
      toastError(error);
    }
  };
  return html`<div>
    <p class="muted">Temporary voice actions still running. Someone who left voice during a mute or deafen stays here until they next connect, because Discord only lets it be cleared while they're in voice.</p>
    <${DataTable} loading=${rows === null} rows=${rows || []} rowKey="row_key" empty="No voice actions are running" columns=${[
      { key: 'name', label: 'Member', sort: true, filter: true },
      { key: 'action', label: 'Action', sort: true, filter: true, text: (row) => ACTION_NAMES[row.action] || row.action, width: '160px' },
      { key: 'expires_at', label: 'Ends', sort: true, render: endsText, width: '220px' },
      { key: 'moderator_name', label: 'Moderator', sort: true, filter: true, width: '170px' },
      { key: 'reason', label: 'Reason', filter: true, render: (row) => html`<div class="truncate">${row.reason}</div>` },
      { key: 'release', label: '', width: '100px', render: (row) => html`<button class="btn small outline" onClick=${() => release(row)}>Release</button>` },
    ]} />
  </div>`;
}

function RevokeForm({ row, onClose, onDone }) {
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const revoke = async () => {
    setBusy(true);
    try {
      await api.post(`/voice/access/${row.user_id}/revoke`, { reason: reason.trim() || null });
      toast('Access revoked.');
      onDone();
    } catch (error) {
      toastError(error);
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([reason]);
  const [start] = useState(snapshot);
  return html`<${Modal} title=${`Revoke access for ${row.name}`} icon="user-x" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn danger" disabled=${busy} onClick=${revoke}>Revoke</button></div>`}>
    <p class="muted">The role comes off and they're disconnected from voice. They can't ask again until someone grants access.</p>
    <${TextInput} label="Reason" help="Goes in the audit log and the approval channel." maxLength=${500} value=${reason} onChange=${setReason} />
  <//>`;
}

function AccessTab() {
  const [filters, setFilters] = useState({ status: '', member: '' });
  const [version, setVersion] = useState(0);
  const [revoking, setRevoking] = useState(null);
  const [grantee, setGrantee] = useState(null);
  const table = usePaged('/voice/access', filters, version);
  const changed = () => { setRevoking(null); setVersion((n) => n + 1); };
  const grant = async (userId) => {
    try {
      await api.post(`/voice/access/${userId}/grant`);
      toast('Access granted.');
      setGrantee(null);
      changed();
    } catch (error) {
      toastError(error);
    }
  };
  return html`<div>
    <div class="row access-grant">
      <div class="grow"><${MemberPicker} label="Grant access to someone" help="Skips the requirements." value=${grantee} onChange=${setGrantee} /></div>
      <button class="btn success" disabled=${!grantee} onClick=${() => grant(grantee)}>Grant</button>
    </div>
    <${TableTools} table=${table} />
    ${table.failed && html`<${Notice} kind="bad">${table.failed.message}<//>`}
    <div class="table-wrap"><table>
      <thead>
        <tr><th>Member</th><th style=${{ width: '130px' }}>Status</th><th style=${{ width: '100px' }}>Messages</th><th style=${{ width: '170px' }}>Asked</th><th style=${{ width: '230px' }}>Decided</th><th style=${{ width: '100px' }}></th></tr>
        <tr>
          <th><input type="text" placeholder="Filter by Member" aria-label="Filter by member, name or ID" value=${filters.member} onInput=${(e) => setFilters({ ...filters, member: e.currentTarget.value })} /></th>
          <th><select aria-label="Filter by status" value=${filters.status} onChange=${(e) => setFilters({ ...filters, status: e.currentTarget.value })}>
            <option value="">All</option>${Object.entries(ACCESS).map(([key, state]) => html`<option key=${key} value=${key}>${state.label}</option>`)}</select></th>
          <th></th><th></th><th></th><th></th>
        </tr>
      </thead>
      <tbody>
        ${!table.data && !table.failed && html`<tr><td colspan="6"><${Loading} /></td></tr>`}
        ${table.data && table.data.rows.length === 0 && html`<tr><td colspan="6"><${Empty}>No matching records<//></td></tr>`}
        ${table.data && table.data.rows.map((row) => html`<tr key=${row.user_id}>
          <td>${row.name} <div class="dim mono small">${row.user_id}</div></td>
          <td><span class=${'badge ' + ACCESS[row.status].badge}>${ACCESS[row.status].label}</span></td>
          <td>${row.count}</td>
          <td class="small">${when(row.requested_at)}</td>
          <td class="small">${row.decided_by_name ? html`${row.decided_by_name}<div class="dim">${when(row.decided_at)}</div>` : ''}</td>
          <td>${row.status === 'approved'
            ? html`<button class="btn small outline danger" onClick=${() => setRevoking(row)}>Revoke</button>`
            : html`<button class="btn small outline success" onClick=${() => grant(row.user_id)}>Grant</button>`}</td>
        </tr>`)}
      </tbody>
    </table></div>
    <${TableFoot} table=${table} />
    ${revoking && html`<${RevokeForm} row=${revoking} onClose=${() => setRevoking(null)} onDone=${changed} />`}
  </div>`;
}

function MiniMutesTab() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const load = () => api.get('/voice/mini-mutes').then(setData).catch((error) => { toastError(error); setData({ mutes: [], votes: [] }); });
  useEffect(() => { load(); }, [guild.id]);
  const lift = async (row) => {
    const sure = await confirmDialog({ title: `Lift the mini mute on ${row.name}?`, body: 'The role comes off now, unless a regular mute on the same role still holds it.', confirm: 'Lift' });
    if (!sure) return;
    try {
      const result = await api.post(`/voice/mini-mutes/${row.user_id}/lift`);
      toast(result.released ? 'Lifted.' : "Lifted. They're still muted, because a regular mute holds the same role.");
      load();
    } catch (error) {
      toastError(error);
    }
  };
  return html`<div>
    <h4>Running now</h4>
    <${DataTable} loading=${data === null} rows=${data ? data.mutes : []} rowKey="user_id" empty="Nobody is mini muted" pageSizes=${[10, 25]} columns=${[
      { key: 'name', label: 'Member', sort: true, filter: true },
      { key: 'expires_at', label: 'Lifts', sort: true, render: (row) => when(row.expires_at), width: '200px' },
      { key: 'applied_by_name', label: 'Applied by', sort: true, filter: true, width: '170px' },
      { key: 'reason', label: 'Reason', filter: true, render: (row) => html`<div class="truncate">${row.reason}</div>` },
      { key: 'lift', label: '', width: '80px', render: (row) => html`<button class="btn small outline" onClick=${() => lift(row)}>Lift</button>` },
    ]} />
    <h4 style=${{ marginTop: '20px' }}>Recent votes</h4>
    <${DataTable} loading=${data === null} rows=${data ? data.votes : []} rowKey="vote_message_id" empty="No votes yet" pageSizes=${[10, 25]} columns=${[
      { key: 'name', label: 'Member', sort: true, filter: true },
      { key: 'initiator_name', label: 'Asked by', sort: true, width: '170px' },
      { key: 'votes', label: 'Votes', width: '120px', render: (row) => `${row.approvals} of ${row.required}, ${row.rejections} against` },
      { key: 'status', label: 'Outcome', sort: true, width: '110px', render: (row) => html`<span class=${'badge ' + VOTES[row.status]}>${row.status}</span>` },
      { key: 'reason', label: 'Reason', render: (row) => html`<div class="truncate">${row.reason}</div>` },
      { key: 'link', label: '', width: '80px', render: (row) => html`<a class="btn small" href=${row.url} target="_blank" rel="noopener noreferrer"><${Icon} name="external-link" /></a>` },
    ]} />
  </div>`;
}

const REPORT_STATUS = {
  open: ['Open', 'blue'], actioned: ['Actioned', 'green'], acted: ['Actioned', 'green'], false_positive: ['False positive', 'red'], handled: ['Dismissed', 'outline'],
};

function ReportsTab() {
  const guild = useGuild();
  const table = usePaged('/voice/reports', {}, 0);
  return html`<div>
    <p class="muted">Staff act on these with the buttons on each post. The Reports page has each one in full.</p>
    <${TableTools} table=${table} />
    ${table.failed && html`<${Notice} kind="bad">${table.failed.message}<//>`}
    <div class="table-wrap"><table>
      <thead><tr><th style=${{ width: '170px' }}>Reported</th><th style=${{ width: '170px' }}>Reported by</th><th style=${{ width: '140px' }}>Channel</th><th>Reason</th><th style=${{ width: '170px' }}>Filed</th><th style=${{ width: '100px' }}>Status</th><th style=${{ width: '60px' }}></th></tr></thead>
      <tbody>
        ${!table.data && !table.failed && html`<tr><td colspan="7"><${Loading} /></td></tr>`}
        ${table.data && table.data.rows.length === 0 && html`<tr><td colspan="7"><${Empty}>No VC reports yet<//></td></tr>`}
        ${table.data && table.data.rows.map((row) => html`<tr key=${String(row.post_message_id || row.created_at)}>
          <td title=${String(row.target_id)}>${row.target_name}</td>
          <td title=${String(row.reporter_id)}>${row.reporter_name}</td>
          <td>${row.voice_channel_id ? channelName(guild, row.voice_channel_id) : html`<span class="dim">Not in voice</span>`}</td>
          <td><div class="truncate">${row.reason}</div></td>
          <td class="small">${when(row.created_at)}</td>
          <td><span class=${'badge ' + (REPORT_STATUS[row.status] || REPORT_STATUS.handled)[1]}>${(REPORT_STATUS[row.status] || REPORT_STATUS.handled)[0]}</span></td>
          <td>${row.url && html`<a class="btn small" href=${row.url} target="_blank" rel="noopener noreferrer" aria-label="Open the report post"><${Icon} name="external-link" /></a>`}</td>
        </tr>`)}
      </tbody>
    </table></div>
    <${TableFoot} table=${table} />
  </div>`;
}

export default function VoicePage() {
  const [tab, setTab] = useState('settings');
  const tabs = [
    { key: 'settings', label: 'Settings' }, { key: 'actions', label: 'Running actions' }, { key: 'access', label: 'Voice access' }, { key: 'mutes', label: 'Mini mutes' }, { key: 'reports', label: 'VC reports' },
  ];
  return html`<div>
    <${Tabs} tabs=${tabs} active=${tab} onChange=${setTab} />
    ${tab === 'settings' && html`<${SettingsTab} />`}
    ${tab === 'actions' && html`<${Card} icon="mic-off" title="Running actions"><${ActionsTab} /><//>`}
    ${tab === 'access' && html`<${Card} icon="shield-check" title="Voice access"><${AccessTab} /><//>`}
    ${tab === 'mutes' && html`<${Card} icon="volume-x" title="Mini mutes"><${MiniMutesTab} /><//>`}
    ${tab === 'reports' && html`<${Card} icon="flag" title="VC reports"><${ReportsTab} /><//>`}
  </div>`;
}
