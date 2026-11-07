// Suggestions: the settings, and every suggestion with its votes and decision.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Col, Empty, Field, Grid, Icon, Loading, Modal, NumberInput, Pager, SaveBar, SaveButton, Select, SettingsGate, Switch, Tabs, TextArea, TextInput, confirmDialog, toast, toastError, useGuild, useSettings, when } from '../ui.js';

const STATES = {
  open: { label: 'Open', badge: 'outline' },
  considered: { label: 'Being considered', badge: 'gold' },
  approved: { label: 'Approved', badge: 'green' },
  denied: { label: 'Denied', badge: 'red' },
  implemented: { label: 'Implemented', badge: 'blue' },
};
const DECISIONS = [
  { value: 'approved', label: 'Approve', icon: 'thumbs-up', tone: 'success' },
  { value: 'denied', label: 'Deny', icon: 'thumbs-down', tone: 'danger' },
  { value: 'considered', label: 'Consider', icon: 'eye', tone: '' },
  { value: 'implemented', label: 'Implemented', icon: 'check', tone: 'primary' },
];

function CreateCard({ form, onCreated }) {
  const [name, setName] = useState('suggestions');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post('/suggestions/create-channel', { name });
      toast('Created.');
      onCreated();
    } catch (failure) {
      setError((failure.fields && failure.fields.name) || failure.message);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Card} icon="lightbulb" title="Suggestion settings">
    <h2>No suggestion channel found</h2>
    <p class="muted">Suggestions need a channel. Pick one you have, or make a new one where members can vote and only the bot can post.</p>
    <${Field} label="Enter the name of the channel you want created" error=${error}>
      <div class="input-group"><span class="addon first">#</span><input type="text" aria-label="Channel name" value=${name} onInput=${(e) => setName(e.currentTarget.value)} />
        <button class="btn success" disabled=${busy || !name.trim()} onClick=${create}>Create</button></div>
    <//>
    <hr class="rule" />
    <${ChannelPicker} label="Select a channel from this dropdown" placeholder="Please select a channel" ...${form.bind('channel_id')} />
    <${SaveButton} form=${form} label="Select" />
  <//>`;
}

function Settings({ form }) {
  return html`
    <${Grid}>
      <${Col}>
        <${Card} icon="lightbulb" title="Suggestion settings" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Vote color intensity</h2>
          <p class="muted">How many more up or down votes it takes for a decision's color to be fully green or red, from 0 to 100. With 0, any lead gives the full color.</p>
          <${NumberInput} min=${0} max=${100} ...${form.bind('threshold')} />
          <hr class="rule" />
          <${Switch} label="Send DM on Decision" help="Tells the person who made the suggestion once it's decided." ...${form.bind('dm')} />
          <${Switch} label="Edit Instead of Reposting" help="The decision changes the original post in place. If a decision channel is set, it's posted there as well." ...${form.bind('edit')} />
          <${Switch} label="Anonymous Mode" help="Hides the name and avatar of whoever made a suggestion. Staff can still look it up with /suggestion who. Suggestions made before you turn this on don't change." ...${form.bind('anon')} />
        <//>
      <//>
      <${Col}>
        <${Card} icon="hash" title="Channels" footer=${html`<${SaveButton} form=${form} />`}>
          <${ChannelPicker} label="Suggestion channel" placeholder="Please select a channel" ...${form.bind('channel_id')} />
          <${ChannelPicker} label="Decision channel" placeholder="No decision channel selected" help="Decisions go here. With none set, they stay in the suggestion channel." ...${form.bind('decision_channel')} />
          <${ChannelPicker} label="Approved channel" placeholder="Use the decision channel" ...${form.bind('approve_channel')} />
          <${ChannelPicker} label="Denied channel" placeholder="Use the decision channel" ...${form.bind('deny_channel')} />
          <${ChannelPicker} label="Considered channel" placeholder="Use the decision channel" ...${form.bind('consider_channel')} />
          <${ChannelPicker} label="Implemented channel" placeholder="Use the decision channel" ...${form.bind('implemented_channel')} />
          <${ChannelPicker} label="Restrict suggestions to" placeholder="Do not restrict" help="Members can only make suggestions from this channel." ...${form.bind('submit_channel')} />
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />`;
}

function DecideDialog({ row, decision, onClose, onDone }) {
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const label = DECISIONS.find((d) => d.value === decision).label;
  const send = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/suggestions/${row.number}/decide`, { decision, reason });
      toast(`Suggestion #${row.number}: ${STATES[decision].label.toLowerCase()}.`);
      onDone();
    } catch (failure) {
      setError((failure.fields && failure.fields.reason) || null);
      toastError(failure);
    } finally {
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([reason]);
  const [start] = useState(snapshot);
  return html`<${Modal} title=${`${label} suggestion #${row.number}`} icon="lightbulb" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button><button class="btn primary" disabled=${busy} onClick=${send}>${label}</button></div>`}>
    <p class="truncate" title=${row.content}>${row.content}</p>
    <${TextArea} label="Reason (optional)" rows=${3} maxLength=${1000} value=${reason} onChange=${setReason} error=${error} help="Shown with the decision, and in the DM if that's on." />
  <//>`;
}

function PromoteDialog({ row, onClose, onDone }) {
  const [content, setContent] = useState(row.content);
  const [credit, setCredit] = useState(row.anonymous ? '' : row.author_name);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState({});
  const send = async () => {
    setBusy(true);
    setErrors({});
    try {
      const made = await api.post(`/proposals/from-suggestion/${row.number}`, { content, credit });
      toast(`Proposal #${made.number} is up for a vote.`);
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
  return html`<${Modal} title=${`Put suggestion #${row.number} to a vote`} icon="thumbs-up" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button><button class="btn primary" disabled=${busy || !content.trim()} onClick=${send}>Post the proposal</button></div>`}>
    <p class="muted">Members and moderators vote on it separately, as set on the Proposals page. The suggestion is marked as being considered, and the result becomes its decision.</p>
    <${TextArea} label="Proposal" rows=${8} maxLength=${4000} value=${content} onChange=${setContent} error=${errors.content} />
    <${TextInput} label="Credit (optional)" maxLength=${200} value=${credit} onChange=${setCredit} error=${errors.credit} />
  <//>`;
}

function SuggestionsList() {
  const guild = useGuild();
  const [state, setState] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(10);
  const [data, setData] = useState(null);
  const [deciding, setDeciding] = useState(null);
  const [promoting, setPromoting] = useState(null);
  const [left, setLeft] = useState(null);
  const load = () => api.get(`/suggestions/list?state=${state}&q=${encodeURIComponent(query.trim())}&page=${page}&size=${size}`).then((fresh) => {
    setData(fresh);
    // Deleting the only row on the last page leaves this page empty. Staying there would say nothing matches while rows exist.
    if (fresh.rows.length === 0 && fresh.total > 0 && page > 1) setPage(Math.ceil(fresh.total / size));
  }).catch((error) => { toastError(error); setData({ rows: [], total: 0 }); });
  useEffect(() => {
    const timer = setTimeout(load, 200);
    return () => clearTimeout(timer);
  }, [state, query, page, size, guild.id]);
  const remove = async (row) => {
    const sure = await confirmDialog({ title: `Delete suggestion #${row.number}?`, body: 'This deletes it from the list and its posts from Discord.', confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      const result = await api.del(`/suggestions/${row.number}`);
      if (result.left.length) setLeft(result.left);
      else toast('Deleted.');
      load();
    } catch (error) {
      toastError(error);
    }
  };
  const pages = data ? Math.max(1, Math.ceil(data.total / size)) : 1;
  const start = data && data.total ? (page - 1) * size + 1 : 0;
  return html`<${Card} icon="list" title="Suggestions">
    <div class="row" style=${{ marginBottom: '14px', alignItems: 'flex-end' }}>
      <div style=${{ width: '220px' }}><${Select} label="State" value=${state} onChange=${(value) => { setState(value); setPage(1); }}
        options=${[{ value: '', label: 'All' }, ...Object.entries(STATES).map(([value, info]) => ({ value, label: info.label }))]} /></div>
      <div style=${{ width: '320px' }}><${TextInput} label="Search" placeholder="Text or name" value=${query} onChange=${(value) => { setQuery(value); setPage(1); }} /></div>
      <div class="table-tools" style=${{ marginBottom: '16px' }}><span>Records:</span>
        <select aria-label="Records per page" value=${size} onChange=${(e) => { setSize(Number(e.currentTarget.value)); setPage(1); }}>
          ${[10, 25, 50, 100].map((n) => html`<option key=${n} value=${n}>${n}</option>`)}</select></div>
    </div>
    ${!data && html`<${Loading} />`}
    ${data && data.rows.length === 0 && html`<${Empty}>No suggestions match.<//>`}
    <div class="stack">
      ${data && data.rows.map((row) => html`<div class="tile" key=${row.number}>
        <div class="spread">
          <b>#${row.number} ${row.author_name}${row.anonymous ? ' (hidden from members)' : ''}</b>
          <span class=${'badge ' + STATES[row.state].badge}>${STATES[row.state].label}</span>
        </div>
        <p class="truncate" title=${row.content} style=${{ margin: '8px 0' }}>${row.content}</p>
        ${row.state !== 'open' && html`<p class="small muted">${STATES[row.state].label} by ${row.decided_by_name || 'a moderator'} ${when(row.decided_at)}${row.reason ? `: ${row.reason}` : ''}</p>`}
        <div class="spread">
          <div class="row">
            <span class="muted small"><${Icon} name="thumbs-up" size=${14} /> ${row.up}</span>
            <span class="muted small"><${Icon} name="thumbs-down" size=${14} /> ${row.down}</span>
            <span class="small dim">${when(row.created_at)}</span>
            ${row.url && html`<a class="small" href=${row.url} target="_blank" rel="noopener noreferrer">Original</a>`}
            ${row.entry_url && html`<a class="small" href=${row.entry_url} target="_blank" rel="noopener noreferrer">Decision</a>`}
          </div>
          <div class="row">
            ${(row.state === 'open' || row.state === 'considered') && html`<button class="btn small" onClick=${() => setPromoting(row)}><${Icon} name="send" size=${14} /> Put to a vote</button>`}
            ${DECISIONS.filter((d) => d.value !== row.state).map((d) => html`<button key=${d.value} class=${'btn small ' + d.tone} onClick=${() => setDeciding({ row, decision: d.value })}><${Icon} name=${d.icon} size=${14} /> ${d.label}</button>`)}
            <button class="icon-btn danger" aria-label="Delete this suggestion" title="Delete this suggestion" onClick=${() => remove(row)}><${Icon} name="trash-2" /></button>
          </div>
        </div>
      </div>`)}
    </div>
    ${data && html`<div class="table-foot">
      <span class="muted small">Showing ${start} to ${Math.min(start + size - 1, data.total)} of ${data.total} records</span>
      <${Pager} page=${page} pages=${pages} onPage=${setPage} />
    </div>`}
    ${deciding && html`<${DecideDialog} ...${deciding} onClose=${() => setDeciding(null)} onDone=${() => { setDeciding(null); load(); }} />`}
    ${promoting && html`<${PromoteDialog} row=${promoting} onClose=${() => setPromoting(null)} onDone=${() => { setPromoting(null); load(); }} />`}
    ${left && html`<${LeftPosts} links=${left} onClose=${() => setLeft(null)} />`}
  <//>`;
}

// The record is gone once this shows, so these links are the only way to the posts the bot couldn't delete.
function LeftPosts({ links, onClose }) {
  return html`<${Modal} title="Deleted, with posts left on Discord" icon="trash-2" onClose=${onClose} footer=${html`<div class="right"><button class="btn primary" onClick=${onClose}>Close</button></div>`}>
    <p>The suggestion is deleted. The bot couldn't remove ${links.length === 1 ? 'this post' : 'these posts'}, because the channel is gone or the bot lacks Manage Messages there. ${links.length === 1 ? 'Open it' : 'Open each one'} and delete ${links.length === 1 ? 'it' : 'them'} yourself.</p>
    <ul>${links.map((link, index) => html`<li key=${link}><a href=${link} target="_blank" rel="noopener noreferrer">${links.length === 1 ? 'Open the post' : `Open post ${index + 1}`}</a></li>`)}</ul>
  <//>`;
}

function Loaded({ onReload }) {
  const form = useSettings('suggestions');
  const [tab, setTab] = useState('settings');
  return html`<${SettingsGate} form=${form}>${() => html`<div>
    <${Tabs} tabs=${[{ key: 'settings', label: 'Settings' }, { key: 'list', label: 'Suggestions' }]} active=${tab} onChange=${setTab} />
    ${tab === 'settings' && (form.saved.channel_id || form.data.channel_id ? html`<${Settings} form=${form} />` : html`<div style=${{ maxWidth: '640px' }}><${CreateCard} form=${form} onCreated=${onReload} /></div>`)}
    ${tab === 'list' && html`<${SuggestionsList} />`}
  </div>`}<//>`;
}

export default function SuggestionsPage() {
  const guild = useGuild();
  const [version, setVersion] = useState(0);
  // A new channel changes the server's channel list as well as these settings, so both load again.
  const reload = async () => {
    await guild.reload();
    setVersion(version + 1);
  };
  return html`<${Loaded} key=${version} onReload=${reload} />`;
}
