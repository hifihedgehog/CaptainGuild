// Twitch live alerts: who's followed, where the alerts go, and the app the bot signs in to Twitch with.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, DataTable, Field, Icon, Loading, Modal, Notice, RolePicker, SettingsGate, TextArea, TextInput, channelName, confirmDialog, toast, toastError, useGuild, useSettings, when } from '../ui.js';

const VARIABLES = '{link} {name} {game} {title} {everyone} {role}';

function StreamSearch({ value, onChange, error }) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [problem, setProblem] = useState(null);
  useEffect(() => {
    if (query.trim().length < 2) { setResults([]); setProblem(null); return undefined; }
    const timer = setTimeout(() => api.get(`/twitch/channels?query=${encodeURIComponent(query.trim())}`)
      .then((rows) => { setResults(rows); setProblem(null); })
      .catch((failure) => { setResults([]); setProblem(failure.message); }), 500);
    return () => clearTimeout(timer);
  }, [query]);
  return html`<${Field} label="Twitch name" error=${error || problem}>
    ${value ? html`<span class="chip">${value.avatar && html`<img class="avatar" style=${{ width: '20px', height: '20px' }} src=${value.avatar} alt="" />`}${value.name}
        <button type="button" aria-label="Clear" onClick=${() => { onChange(null); setQuery(''); setResults([]); }}><${Icon} name="x" size=${12} /></button></span>`
      : html`<div class="ms"><input type="text" value=${query} placeholder="Please enter a channel name" aria-label="Twitch name" onInput=${(e) => setQuery(e.currentTarget.value)} />
        ${results.length > 0 && html`<div class="ms-list">${results.map((row) => html`<div key=${row.id} class="ms-option" onClick=${() => { onChange(row); setResults([]); }}>
          ${row.avatar && html`<img class="avatar" style=${{ width: '22px', height: '22px' }} src=${row.avatar} alt="" />`}${row.name}${row.live && html`<span class="badge red">live</span>`}</div>`)}</div>`}</div>`}
  <//>`;
}

function StreamForm({ row, defaultFormat, onClose, onSaved }) {
  const editing = !!row;
  const [stream, setStream] = useState(null);
  const [channel, setChannel] = useState(row ? row.channel_id : null);
  const [role, setRole] = useState(row ? row.role_id : null);
  const [fmt, setFmt] = useState(row ? row.fmt : '');
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    setErrors({});
    try {
      const body = { channel_id: channel, role_id: role, fmt };
      if (editing) await api.put(`/twitch/${row.stream_id}`, body);
      else await api.post('/twitch', { ...body, name: stream ? stream.login : '' });
      toast(editing ? 'Saved.' : 'Added.');
      onSaved();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([stream && stream.login, channel, role, fmt]);
  const [start] = useState(snapshot);
  return html`<${Modal} title=${editing ? 'Edit twitch stream' : 'Add twitch stream'} icon="twitch" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn twitch-btn" disabled=${busy} onClick=${save}>${editing ? 'Save' : 'Add'}</button></div>`}>
    ${editing ? html`<${Field} label="Twitch name"><b>${row.stream_name}</b><//>` : html`<${StreamSearch} value=${stream} onChange=${setStream} error=${errors.name} />`}
    <${ChannelPicker} label="Channel" clearable=${false} value=${channel} onChange=${setChannel} error=${errors.channel_id} />
    <${RolePicker} label="Role to mention" placeholder="No role" value=${role} onChange=${setRole} error=${errors.role_id} help="The role is pinged at the start of the alert, or wherever you put {role}. A role members can't ping takes Mention Everyone in that channel to save, and is made mentionable for a moment unless the bot holds Mention Everyone there." />
    <${TextArea} label="Announcement message" rows=${4} maxLength=${1000} placeholder=${defaultFormat} value=${fmt} onChange=${setFmt} error=${errors.fmt} help=${'These fill in: ' + VARIABLES + '. Only {everyone} pings everyone, and it takes Mention Everyone in that channel to save.'} />
  <//>`;
}

function Connection({ status, hint, configured, onChanged }) {
  const form = useSettings('notifications');
  const [checking, setChecking] = useState(false);
  const check = async () => {
    setChecking(true);
    try {
      await api.post('/twitch/check');
      toast('Twitch accepted these.');
    } catch (error) { toastError(error); } finally { setChecking(false); }
  };
  const save = async () => {
    if (await form.save()) onChanged();
  };
  return html`<${Card} icon="key" title="Twitch connection" footer=${html`
      <button class="btn primary" disabled=${form.saving} onClick=${save}>${form.saving ? 'Saving...' : 'Save'}</button>
      <button class="btn" disabled=${checking || !configured} onClick=${check}>${checking ? 'Checking...' : 'Test connection'}</button>`}>
    <p class="muted">Twitch only answers apps it knows, so this bot signs in with one of yours. Register an app at <a href="https://dev.twitch.tv/console/apps" target="_blank" rel="noopener noreferrer">dev.twitch.tv/console/apps</a> with any OAuth redirect URL, then copy its client ID and a new client secret here. They're kept for this server only and never shown again.</p>
    <${SettingsGate} form=${form}>${() => html`
      <${TextInput} label="Client ID" type="password" placeholder="Paste the client ID" help=${hint ? `The one saved now ends in ${hint}.` : null} ...${form.bind('twitch_client_id')} />
      <${TextInput} label="Client secret" type="password" placeholder="Paste the client secret" ...${form.bind('twitch_client_secret')} />
      <${ChannelPicker} label="Default channel" help="Where the twitch add command sends alerts when it names no channel. Leave it empty to use the channel the command runs in." ...${form.bind('twitch_channel')} />`}<//>
    ${status.error && html`<${Notice} kind="bad">${status.error}<//>`}
    ${!status.error && status.ok_at && html`<p class="muted small">Last checked with Twitch at ${when(status.ok_at)}.</p>`}
  <//>`;
}

export default function TwitchPage() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [editing, setEditing] = useState(null);
  const load = () => api.get('/twitch').then(setData).catch(toastError);
  useEffect(() => { load(); }, [guild.id]);
  if (!data) return html`<${Loading} />`;
  const remove = async (row) => {
    const sure = await confirmDialog({ title: `Stop alerts for ${row.stream_name}?`, confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      await api.del(`/twitch/${row.stream_id}`);
      toast('Deleted.');
      load();
    } catch (error) { toastError(error); }
  };
  const test = async (row) => {
    try {
      await api.post(`/twitch/${row.stream_id}/test`);
      toast(`A sample alert went to #${channelName(guild, row.channel_id)}. Tests don't ping anyone.`);
    } catch (error) { toastError(error); }
  };
  return html`
    ${!data.configured && html`<${Notice} kind="warn">Alerts can't run until a Twitch client ID and secret are saved below.<//>`}
    <${Card} icon="twitch" title="Twitch">
      <button class="btn twitch-btn" disabled=${!data.configured} onClick=${() => setEditing('new')}><${Icon} name="twitch" /> Add new twitch stream</button>
      <span class="muted small" style=${{ marginLeft: '10px' }}>${data.rows.length} / ${data.limit} streams used</span>
      <hr />
      <${DataTable} rows=${data.rows} rowKey="stream_id" columns=${[
        { key: 'stream_name', label: 'Stream', sort: true, filter: true, render: (row) => html`<a href=${'https://twitch.tv/' + row.stream_login} target="_blank" rel="noopener noreferrer">${row.stream_name}</a>${row.live && html` <span class="badge red">live</span>`}${row.last_error && html`<div class="error">${row.last_error}</div>`}` },
        { key: 'channel', label: 'Channel', sort: true, text: (row) => channelName(guild, row.channel_id), render: (row) => '#' + channelName(guild, row.channel_id) },
        { key: 'last_alert', label: 'Last alert', sort: true, width: '180px', render: (row) => (row.last_alert ? when(row.last_alert) : 'Not yet') },
        { key: 'options', label: 'Options', width: '230px', render: (row) => html`<div class="row">
          <button class="btn small primary" disabled=${!!row.hidden} onClick=${() => setEditing(row)}>Edit</button>
          <button class="btn small" disabled=${!!row.hidden} onClick=${() => test(row)}>Test</button>
          <button class="btn small outline danger" disabled=${!!row.hidden} onClick=${() => remove(row)}>Delete</button></div>` },
      ]} />
    <//>
    <div style=${{ marginTop: '18px', maxWidth: '760px' }}>
      <${Connection} status=${data.status} hint=${data.client_id_hint} configured=${data.configured} onChanged=${load} />
    </div>
    ${editing && html`<${StreamForm} row=${editing === 'new' ? null : editing} defaultFormat=${data.default_format} onClose=${() => setEditing(null)} onSaved=${() => { setEditing(null); load(); }} />`}`;
}
