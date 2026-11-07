import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Col, DurationInput, Empty, Grid, Icon, Loading, Notice, Picker, channelName, confirmDialog, sameId, toast, toastError, useGuild, when } from '../ui.js';

const HOUR = 3600;
const MAX = 14 * 86400;
const TYPES = [
  { value: 'links', label: 'Links' }, { value: 'invites', label: 'Invites' }, { value: 'attachments', label: 'Attachments' },
  { value: 'bots', label: 'Bot messages' }, { value: 'humans', label: 'Human messages' },
];
const typeName = (value) => (TYPES.find((t) => t.value === value) || { label: 'All messages' }).label;
const BLANK = { channel: null, seconds: HOUR, type: null };

function every(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return [days ? `${days}d` : '', hours ? `${hours}h` : '', minutes ? `${minutes}m` : ''].filter(Boolean).join(' ') || '0m';
}

export default function AutoPurgePage() {
  const guild = useGuild();
  const [rows, setRows] = useState(null);
  const [form, setForm] = useState(BLANK);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState({});
  const load = () => api.get('/autopurge').then(setRows).catch((error) => { toastError(error); setRows([]); });
  useEffect(() => { load(); }, [guild.id]);

  const canDelete = guild.me.permissions.includes('administrator') || guild.me.permissions.includes('manage_messages');
  const tooShort = form.seconds < HOUR;
  const tooLong = form.seconds > MAX;
  const save = async () => {
    if (!form.channel) {
      setErrors({ channel_id: 'Pick a channel.' });
      return;
    }
    const current = rows.find((row) => sameId(row.channel_id, form.channel));
    if (!form.type && (!current || current.message_type !== 'all')) {
      const sure = await confirmDialog({ title: `Purge everything in #${channelName(guild, form.channel)}?`, body: `Every message except pinned ones is deleted every ${every(form.seconds)}. This can't be undone.`, confirm: 'Purge on a schedule', danger: true });
      if (!sure) return;
    }
    setSaving(true);
    try {
      await api.put(`/autopurge/${form.channel}`, { interval_seconds: form.seconds, message_type: form.type || 'all' });
      toast('Saved.');
      setForm(BLANK);
      setErrors({});
      load();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setSaving(false);
    }
  };
  const remove = async (row) => {
    const sure = await confirmDialog({ title: row.gone ? 'Remove this deleted channel from the list?' : `Stop purging #${channelName(guild, row.channel_id)}?`, body: 'The channel keeps what it has and is no longer purged.', confirm: 'Remove', danger: true });
    if (!sure) return;
    try {
      await api.del(`/autopurge/${row.channel_id}`);
      toast('Removed.');
      load();
    } catch (error) {
      toastError(error);
    }
  };
  if (rows === null) return html`<${Loading} />`;
  return html`<${Grid}>
    <${Col}>
      <${Card} icon="trash-2" title="Auto purge" footer=${html`<button class="btn primary" disabled=${saving || tooShort || tooLong} onClick=${save}>${saving ? 'Saving...' : 'Save'}</button>`}>
        <h4>Automatic message cleanup</h4>
        <p class="muted">Each listed channel loses its messages at a fixed interval. Pinned messages stay. Each run looks through the newest 1,000 messages.</p>
        ${!canDelete && html`<${Notice} kind="warn">The bot needs Manage Messages for this to work.<//>`}
        <${ChannelPicker} label="Channel" placeholder="Select a channel" error=${errors.channel_id} value=${form.channel} onChange=${(channel) => setForm({ ...form, channel })} />
        <${DurationInput} label="Repeat every" units=${['d', 'h', 'm']} help="From 1 hour to 14 days. A new interval restarts the countdown. Changing the message type doesn't." error=${errors.interval_seconds || (tooShort ? 'The interval must be at least 1 hour.' : (tooLong ? 'The interval can be at most 14 days.' : null))}
          value=${form.seconds} onChange=${(seconds) => setForm({ ...form, seconds })} />
        <${Picker} label="Message type" placeholder="All messages" help="Leave this empty to purge all messages." options=${TYPES} error=${errors.message_type} value=${form.type} onChange=${(type) => setForm({ ...form, type })} />
      <//>
    <//>
    <${Col}>
      <${Card} icon="list" title="Auto purge list">
        ${rows.length === 0 && html`<${Empty}>No channels are purged on a schedule.<//>`}
        ${rows.map((row) => html`<div class="field-edit spread" key=${String(row.channel_id)}>
          <div>
            <h4 style=${{ marginBottom: '2px' }}>${row.gone ? 'Deleted channel' : `#${channelName(guild, row.channel_id)}`}</h4>
            <div class="muted small">Every ${every(row.interval_seconds)}, ${typeName(row.message_type).toLowerCase()}</div>
            <div class="muted small">Next purge: ${when(row.next_run)}</div>
            ${row.last_run ? html`<div class="muted small">Last purge: ${when(row.last_run)}, ${row.last_deleted} deleted</div>` : ''}
            ${!row.can_purge && html`<div class="error">The bot needs Manage Messages and Read Message History in this channel.</div>`}
            ${row.can_purge && row.last_error && html`<div class="error">${row.last_error}</div>`}
          </div>
          <div class="row" style=${{ flexWrap: 'nowrap' }}>
            <button class="icon-btn" aria-label=${'Edit ' + channelName(guild, row.channel_id)} title="Edit" onClick=${() => setForm({ channel: row.channel_id, seconds: row.interval_seconds, type: row.message_type === 'all' ? null : row.message_type })}><${Icon} name="pencil" /></button>
            <button class="btn small outline danger" onClick=${() => remove(row)}>Remove</button>
          </div>
        </div>`)}
      <//>
    <//>
  <//>`;
}
