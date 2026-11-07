// YouTube upload alerts: which channels are followed and where the alerts go.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, DataTable, Field, Icon, Loading, Modal, RolePicker, Switch, TextArea, TextInput, channelName, confirmDialog, toast, toastError, useGuild, when } from '../ui.js';

const VARIABLES = '{link} {author} {title} {everyone} {role}';

function ChannelForm({ row, defaultFormat, onClose, onSaved }) {
  const editing = !!row;
  const [link, setLink] = useState('');
  const [channel, setChannel] = useState(row ? row.channel_id : null);
  const [role, setRole] = useState(row ? row.role_id : null);
  const [fmt, setFmt] = useState(row ? row.fmt : '');
  const [shorts, setShorts] = useState(row ? row.shorts : true);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    setErrors({});
    try {
      const body = { channel_id: channel, role_id: role, fmt, shorts };
      if (editing) {
        await api.put(`/youtube/${row.youtube_id}`, body);
        toast('Saved.');
      } else {
        const added = await api.post('/youtube', { ...body, link });
        toast(`Following ${added.youtube_name}.`);
      }
      onSaved();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([link, channel, role, fmt, shorts]);
  const [start] = useState(snapshot);
  return html`<${Modal} title=${editing ? 'Edit Youtube Alert' : 'Add Youtube Channel'} icon="youtube" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn youtube-btn" disabled=${busy} onClick=${save}>${busy ? 'Working...' : (editing ? 'Save' : 'Add')}</button></div>`}>
    ${editing ? html`<${Field} label="Youtube channel"><b>${row.youtube_name}</b><//>`
      : html`<${TextInput} label="Youtube link" placeholder="Enter youtube channel or video link" value=${link} onChange=${setLink} error=${errors.link} help="A channel link, a video from the channel or an @handle finds the right one. A name takes the top search result." />`}
    <${ChannelPicker} label="Channel" clearable=${false} value=${channel} onChange=${setChannel} error=${errors.channel_id} />
    <${RolePicker} label="Role to mention" placeholder="No role" value=${role} onChange=${setRole} error=${errors.role_id} help="The role is pinged at the start of the alert, or wherever you put {role}. A role members can't ping takes Mention Everyone in that channel to save, and is made mentionable for a moment unless the bot holds Mention Everyone there." />
    <${TextArea} label="Announcement message" rows=${4} maxLength=${1000} placeholder=${defaultFormat} value=${fmt} onChange=${setFmt} error=${errors.fmt} help=${'These fill in: ' + VARIABLES + '. Only {everyone} pings everyone, and it takes Mention Everyone in that channel to save.'} />
    <${Switch} label="Include Shorts" value=${shorts} onChange=${setShorts} />
  <//>`;
}

export default function YoutubePage() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [editing, setEditing] = useState(null);
  const load = () => api.get('/youtube').then(setData).catch(toastError);
  useEffect(() => { load(); }, [guild.id]);
  if (!data) return html`<${Loading} />`;
  const remove = async (row) => {
    const sure = await confirmDialog({ title: `Stop alerts for ${row.youtube_name}?`, confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      await api.del(`/youtube/${row.youtube_id}`);
      toast('Deleted.');
      load();
    } catch (error) { toastError(error); }
  };
  const test = async (row) => {
    try {
      await api.post(`/youtube/${row.youtube_id}/test`);
      toast(`A sample alert went to #${channelName(guild, row.channel_id)}. Tests don't ping anyone.`);
    } catch (error) { toastError(error); }
  };
  return html`<${Card} icon="youtube" title="Youtube">
    <button class="btn youtube-btn" onClick=${() => setEditing('new')}><${Icon} name="youtube" /> Add Youtube Channel</button>
    <span class="muted small" style=${{ marginLeft: '10px' }}>${data.rows.length} / ${data.limit} channels used</span>
    <hr />
    <${DataTable} rows=${data.rows} rowKey="youtube_id" columns=${[
      { key: 'youtube_name', label: 'Youtube channel', sort: true, filter: true, render: (row) => html`<a href=${'https://www.youtube.com/channel/' + row.youtube_id} target="_blank" rel="noopener noreferrer">${row.youtube_name}</a>${row.last_error && html`<div class="error">${row.last_error}</div>`}${row.feed_error && html`<div class="dim small">${row.feed_error} The bot keeps trying.</div>`}` },
      { key: 'channel', label: 'Channel', sort: true, text: (row) => channelName(guild, row.channel_id), render: (row) => '#' + channelName(guild, row.channel_id) },
      { key: 'last_alert', label: 'Last alert', sort: true, width: '180px', render: (row) => (row.last_alert ? when(row.last_alert) : 'Not yet') },
      { key: 'options', label: 'Options', width: '230px', render: (row) => html`<div class="row">
        <button class="btn small primary" disabled=${!!row.hidden} onClick=${() => setEditing(row)}>Edit</button>
        <button class="btn small" disabled=${!!row.hidden} onClick=${() => test(row)}>Test</button>
        <button class="btn small outline danger" disabled=${!!row.hidden} onClick=${() => remove(row)}>Delete</button></div>` },
    ]} />
    ${editing && html`<${ChannelForm} row=${editing === 'new' ? null : editing} defaultFormat=${data.default_format} onClose=${() => setEditing(null)} onSaved=${() => { setEditing(null); load(); }} />`}
  <//>`;
}
