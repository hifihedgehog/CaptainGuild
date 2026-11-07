// Scheduled announcements, and feeds, which pair a name with a role and a channel.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { MessageEditor, MessagePreview, emptyMessage, hasEmbeds } from '../embed.js';
import { Card, ChannelPicker, DataTable, Field, Icon, Loading, Modal, RolePicker, Tabs, TextInput, channelName, confirmDialog, roleName, toast, toastError, useGuild, when } from '../ui.js';

const MENTIONS = [
  { value: 'silent', label: 'Silent (no role)' },
  { value: 'everyone', label: '@everyone' },
  { value: 'here', label: '@here' },
];
const REPEAT_UNITS = ['Months', 'Days', 'Hours', 'Minutes'];

function localValue(seconds) {
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function repeatText(row) {
  const parts = REPEAT_UNITS.map((unit, index) => (row.repeat[index] ? `${row.repeat[index]} ${unit.toLowerCase().slice(0, row.repeat[index] === 1 ? -1 : undefined)}` : null)).filter(Boolean);
  return parts.length ? `Every ${parts.join(', ')}` : 'Once';
}

function repeats(row) {
  return row.repeat.some((count) => count > 0);
}

function mentionText(guild, row) {
  if (row.mention === 'everyone') return '@everyone';
  if (row.mention === 'here') return '@here';
  if (row.role_id) return roleName(guild, row.role_id);
  return 'silent';
}

function AutofeedForm({ row, onClose, onSaved }) {
  const guild = useGuild();
  const editing = !!row;
  const [channel, setChannel] = useState(row ? row.channel_id : null);
  const [message, setMessage] = useState(row ? row.message : emptyMessage());
  const [moment, setMoment] = useState(row ? localValue(row.next_at) : '');
  const [mention, setMention] = useState(row ? (row.mention === 'role' ? row.role_id : row.mention) : 'silent');
  const [repeat, setRepeat] = useState(row ? row.repeat : [0, 0, 0, 0]);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    setErrors({});
    // An edit that leaves the date alone sends the exact time it already holds, which a whole-minute box can't show.
    const when_ = editing && moment === localValue(row.next_at) ? row.next_at : (moment ? new Date(moment).getTime() / 1000 : null);
    const body = { channel_id: channel, message, when: when_, mention, repeat, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone };
    try {
      if (editing) await api.put(`/autofeeds/${row.id}`, body);
      else await api.post('/autofeeds', body);
      toast(editing ? 'Saved.' : 'Created.');
      onSaved();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    const sure = await confirmDialog({ title: 'Delete this autofeed?', confirm: 'Delete', danger: true });
    if (!sure) return;
    setBusy(true);
    try {
      await api.del(`/autofeeds/${row.id}`);
      toast('Deleted.');
      onSaved();
    } catch (error) { toastError(error); } finally { setBusy(false); }
  };
  const test = async () => {
    setBusy(true);
    try {
      await api.post(`/autofeeds/${row.id}/test`);
      toast(`It went to #${channelName(guild, row.channel_id)} without its ping. The schedule hasn't changed.`);
    } catch (error) { toastError(error); } finally { setBusy(false); }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([channel, message, moment, mention, repeat]);
  const [start] = useState(snapshot);
  return html`<${Modal} large title=${editing ? 'Edit Autofeed' : 'Create Autofeed'} icon="rss" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`
      ${editing && html`<button class="btn ghost danger" disabled=${busy} onClick=${remove}><${Icon} name="trash-2" /> Delete autofeed</button>
        <button class="btn" disabled=${busy} onClick=${test}>Send a test</button>`}
      <div class="right"><button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn success" disabled=${busy} onClick=${save}>${editing ? 'Save' : 'Create'}</button></div>`}>
    <${ChannelPicker} label="Channel" clearable=${false} value=${channel} onChange=${setChannel} error=${errors.channel_id} />
    <hr />
    <h4>Message</h4>
    <${MessageEditor} collapsed value=${message} contentPlaceholder="Please enter your message" contentRows=${5} onChange=${setMessage} help="These fill in: {server} {channel} {unix}, and the rest of the tag language." />
    ${errors.message && html`<div class="error">${errors.message}</div>`}
    ${(message.content || hasEmbeds(message)) && html`<${MessagePreview} value=${message} />`}
    <hr />
    <${Field} label="Date" help="In your local time zone. A repeating autofeed keeps that clock time when the clocks change." error=${errors.when}>
      <input type="datetime-local" aria-label="Date" value=${moment} onInput=${(e) => setMoment(e.currentTarget.value)} />
    <//>
    <hr />
    <${RolePicker} label="Role to mention" extra=${MENTIONS} clearable=${false} value=${mention} onChange=${setMention} error=${errors.mention}
      help="Pinging @everyone, @here or a role members can't ping takes Mention Everyone in that channel. A role that can't normally be pinged is made mentionable for a moment, unless the bot holds Mention Everyone there. That needs Manage Roles and a place above the role." />
    <hr />
    <${Field} label="Repeat every" help="All zeros means it posts once and is done. It can repeat at most once an hour." error=${errors.repeat}>
      <div class="input-row">${REPEAT_UNITS.map((unit, index) => html`<div class="input-group" key=${unit}>
        <input type="number" min="0" aria-label=${unit} value=${repeat[index]} onInput=${(e) => setRepeat(repeat.map((n, i) => (i === index ? Math.max(0, Number(e.currentTarget.value) || 0) : n)))} />
        <span class="addon">${unit}</span></div>`)}</div>
    <//>
  <//>`;
}

function FeedForm({ row, onClose, onSaved }) {
  const editing = !!row;
  const [name, setName] = useState(row ? row.name : '');
  const [role, setRole] = useState(row ? row.role_id : null);
  const [channel, setChannel] = useState(row ? row.channel_id : null);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    setErrors({});
    try {
      if (editing) await api.put(`/feeds/${encodeURIComponent(row.name)}`, { role_id: role, channel_id: channel });
      else await api.post('/feeds', { name, role_id: role, channel_id: channel });
      toast(editing ? 'Saved.' : 'Created.');
      onSaved();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([name, role, channel]);
  const [start] = useState(snapshot);
  return html`<${Modal} title=${editing ? 'Edit feed ' + row.name : 'Create feed'} icon="megaphone" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button><button class="btn success" disabled=${busy} onClick=${save}>${editing ? 'Save' : 'Create'}</button></div>`}>
    ${!editing && html`<${TextInput} label="Name" placeholder="news" maxLength=${50} value=${name} onChange=${setName} error=${errors.name} help="Staff use this name to announce: /feeds announce news Something happened" />`}
    <${RolePicker} label="Role to ping" clearable=${false} value=${role} onChange=${setRole} error=${errors.role_id} />
    <${ChannelPicker} label="Channel" clearable=${false} value=${channel} onChange=${setChannel} error=${errors.channel_id} />
  <//>`;
}

export default function AutofeedsPage() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [tab, setTab] = useState('autofeeds');
  const [editing, setEditing] = useState(null);
  const [editingFeed, setEditingFeed] = useState(null);
  const load = () => api.get('/autofeeds').then(setData).catch(toastError);
  useEffect(() => { load(); }, [guild.id]);
  if (!data) return html`<${Loading} />`;
  const removeFeed = async (row) => {
    const sure = await confirmDialog({ title: `Delete feed ${row.name}?`, confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      await api.del(`/feeds/${encodeURIComponent(row.name)}`);
      toast('Deleted.');
      load();
    } catch (error) { toastError(error); }
  };
  const done = () => { setEditing(null); setEditingFeed(null); load(); };
  return html`<${Card} icon="rss" title="Autofeeds">
    <${Tabs} active=${tab} onChange=${setTab} tabs=${[{ key: 'autofeeds', label: 'Autofeeds' }, { key: 'feeds', label: 'Feeds' }]} />
    ${tab === 'autofeeds' && html`
      <button class="btn success" onClick=${() => setEditing('new')}><${Icon} name="rss" /> Create new autofeed</button>
      <span class="muted small" style=${{ marginLeft: '10px' }}>${data.autofeeds.length} / ${data.autofeed_limit} autofeeds used</span>
      <hr />
      <${DataTable} rows=${data.autofeeds} columns=${[
        { key: 'message', label: 'Message', sort: true, filter: true, text: (row) => row.message.content || (row.message.embeds[0] && row.message.embeds[0].title) || '', render: (row) => (row.hidden ? html`<span class="muted">In a channel you can't see</span>` : html`<div class="truncate">${row.message.content || (row.message.embeds[0] && (row.message.embeds[0].title || row.message.embeds[0].description))}</div>${row.message.embeds.length > 0 && html`<span class="badge blue">embed</span>`}${row.last_error && !repeats(row) && html`<span class="badge red">Stopped</span>`}${row.last_error && html`<div class="error">${row.last_error}</div>`}`) },
        { key: 'channel', label: 'Channel', width: '160px', text: (row) => channelName(guild, row.channel_id), render: (row) => '#' + channelName(guild, row.channel_id) },
        { key: 'role', label: 'Role', width: '140px', text: (row) => mentionText(guild, row) },
        { key: 'repeat', label: 'Repeats', width: '130px', text: (row) => (row.hidden ? '' : repeatText(row)) },
        { key: 'next_at', label: 'Next Announcement', sort: true, width: '190px', render: (row) => (row.last_error && !repeats(row) ? 'Not scheduled' : when(row.next_at)) },
        { key: 'actions', label: '', width: '80px', render: (row) => html`<button class="btn small outline" disabled=${!!row.hidden} onClick=${() => setEditing(row)}>Edit</button>` },
      ]} />
      <p class="muted small" style=${{ marginTop: '10px' }}>An autofeed has to be at least a minute in the future. Commands take UTC, and this page takes your local time.</p>`}
    ${tab === 'feeds' && html`
      <p class="muted">A feed lets staff with Mention Everyone announce to a role that members can't ping themselves, with <code>/feeds announce</code>.</p>
      <button class="btn success" onClick=${() => setEditingFeed('new')}><${Icon} name="megaphone" /> Create new feed</button>
      <hr />
      <${DataTable} rows=${data.feeds} rowKey="name" columns=${[
        { key: 'name', label: 'Name', sort: true, filter: true },
        { key: 'channel', label: 'Channel', text: (row) => channelName(guild, row.channel_id), render: (row) => '#' + channelName(guild, row.channel_id) },
        { key: 'role', label: 'Role', text: (row) => roleName(guild, row.role_id) },
        { key: 'options', label: 'Options', width: '170px', render: (row) => html`<div class="row"><button class="btn small primary" disabled=${!!row.hidden} onClick=${() => setEditingFeed(row)}>Edit</button>
          <button class="btn small outline danger" disabled=${!!row.hidden} onClick=${() => removeFeed(row)}>Delete</button></div>` },
      ]} />`}
    ${editing && html`<${AutofeedForm} row=${editing === 'new' ? null : editing} onClose=${() => setEditing(null)} onSaved=${done} />`}
    ${editingFeed && html`<${FeedForm} row=${editingFeed === 'new' ? null : editingFeed} onClose=${() => setEditingFeed(null)} onSaved=${done} />`}
  <//>`;
}
