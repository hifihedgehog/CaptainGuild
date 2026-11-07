// Automatic responses: the table of triggers, their form, and who and where they ignore.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { MessageEditor, emptyMessage } from '../embed.js';
import { ImportModal, TemplateField, TryItPanel, askBeforeTrusting, downloadJson } from '../templates.js';
import { ANY_CHANNEL, Card, ChannelPicker, DataTable, Field, Icon, Modal, NumberInput, PeopleField, RolePicker, SaveBar, SaveButton, Select, SettingsGate, Switch, TextInput, channelName, confirmDialog, toast, toastError, useGuild, useSettings } from '../ui.js';

const NORMAL = 1;
const CHANNEL = 3;
const REGEX = 7;
const ROLE = 8;
const BLANK = { match_type: NORMAL, trigger: '', message: emptyMessage(), channel_id: null, role_wl: [], role_bl: [], channel_wl: [], channel_bl: [], cooldown: 0, trusted: false };

const MATCH_HELP = {
  1: 'The message holds this text anywhere, even inside a longer word.',
  2: 'The message holds these exact words. "hell" doesn\'t match "hello".',
  3: 'Like Normal, but it only listens in the channel you pick, even when that channel is ignored.',
  4: 'The whole message is this text. Capitals and punctuation don\'t count.',
  5: 'The message starts with this text.',
  6: 'The message ends with this text.',
  7: 'The message matches this Python regular expression. A pattern that takes too long on a message skips that message, and the list marks it. A server can hold 50 of these.',
  8: 'A member gains or loses a role. {role} is that role, {target} is the member, {user} is whoever changed it. The same change on the same member fires once a minute at most, and one trigger posts at most 30 times a minute.',
};

function TriggerModal({ row, matchTypes, canTrust, onClose, onSaved }) {
  const [form, setForm] = useState(row ? { ...BLANK, ...row } : BLANK);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const set = (key) => ({ value: form[key], error: errors[key], onChange: (value) => { setForm({ ...form, [key]: value }); setErrors({ ...errors, [key]: undefined }); } });
  // Pasting raw JSON for an embed shouldn't wipe the text above it.
  const setEmbeds = (next) => setForm({ ...form, message: { ...next, content: next.content || form.message.content } });
  const kind = form.match_type;
  const save = async () => {
    setSaving(true);
    const body = {
      match_type: kind, trigger: kind === ROLE ? '' : form.trigger, message: form.message, channel_id: kind === CHANNEL || kind === ROLE ? form.channel_id : null,
      role_wl: form.role_wl, role_bl: form.role_bl, channel_wl: kind === CHANNEL || kind === ROLE ? [] : form.channel_wl, channel_bl: kind === CHANNEL || kind === ROLE ? [] : form.channel_bl,
      cooldown: form.cooldown || 0, trusted: form.trusted,
    };
    try {
      const answer = row ? await api.put(`/triggers/${row.id}`, body) : await api.post('/triggers', body);
      if (answer.trust_removed) toast('Saved. This trigger is no longer trusted. An administrator has to trust it again.', 'bad');
      else toast(row ? 'Saved.' : 'Created.');
      onSaved();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setSaving(false);
    }
  };
  const setTrusted = async (value) => {
    if (value && !(await askBeforeTrusting('/triggers/preview', form.message, 'trigger'))) {
      setForm((current) => ({ ...current }));  // the box was already ticked by the click, and this puts it back
      return;
    }
    setForm((current) => ({ ...current, trusted: value }));
    setErrors((current) => ({ ...current, trusted: undefined }));
  };
  const remove = async () => {
    const sure = await confirmDialog({ title: 'Delete this trigger?', body: 'This can\'t be undone.', confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      await api.del(`/triggers/${row.id}`);
      toast('Deleted.');
      onSaved();
    } catch (error) {
      toastError(error);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([form]);
  const [start] = useState(snapshot);
  return html`<${Modal} large icon="message-square" title=${row ? 'Edit Trigger' : 'Create Trigger'} onClose=${onClose} dirty=${snapshot() !== start} footer=${html`
      ${row && html`<button class="icon-btn danger" aria-label="Delete trigger" title="Delete trigger" onClick=${remove}><${Icon} name="trash-2" size=${18} /></button>`}
      <div class="right">
        <button class="btn ghost" onClick=${onClose}>Cancel</button>
        <button class=${'btn ' + (row ? 'primary' : 'success')} disabled=${saving} onClick=${save}>${row ? 'Save' : 'Create'}</button>
      </div>`}>
    <${Select} label="Match Type" help=${MATCH_HELP[kind]} options=${matchTypes.map((type) => ({ value: type.id, label: type.name }))} ...${set('match_type')} />
    ${kind !== ROLE && html`<${TextInput} label="Trigger" placeholder=${kind === REGEX ? 'hello+ (world|there)' : ''} ...${set('trigger')} />`}
    ${kind === CHANNEL && html`<${ChannelPicker} label="Specific Channel" clearable=${false} ...${set('channel_id')} />`}
    ${kind === ROLE && html`<${ChannelPicker} label="Target Channel" help="Where the response goes." clearable=${false} ...${set('channel_id')} />`}
    <hr />
    <${TemplateField} label="Response" placeholder="Enter some trigger content" value=${form.message.content} error=${errors.message} onChange=${(content) => { setForm({ ...form, message: { ...form.message, content } }); setErrors({ ...errors, message: undefined }); }} />
    <${MessageEditor} value=${form.message} onChange=${setEmbeds} collapsed showContent=${false} />
    <${TryItPanel} path="/triggers/preview" message=${form.message} extra=${{ trusted: form.trusted, match_type: kind }} argsLabel=${kind === ROLE ? 'Sample text for {args}' : 'A message that would set it off'} />
    <hr />
    <${RolePicker} label="Whitelist Roles" help=${kind === ROLE ? 'When any are set, it only fires if the role that changed or a role the member holds is listed.' : 'When any are set, only members with one of these set it off.'} multi placeholder="Select roles" ...${set('role_wl')} />
    <${RolePicker} label="Blacklist Roles" help=${kind === ROLE ? 'It doesn\'t fire if the role that changed or a role the member holds is listed.' : 'Members with one of these can\'t set it off.'} multi placeholder="Select roles" ...${set('role_bl')} />
    ${kind !== CHANNEL && kind !== ROLE && html`
      <${ChannelPicker} label="Whitelist Channels" help="When any are set, it only listens in these." multi categories types=${ANY_CHANNEL} ...${set('channel_wl')} />
      <${ChannelPicker} label="Blacklist Channels" multi categories types=${ANY_CHANNEL} ...${set('channel_bl')} />`}
    ${kind !== ROLE && html`<${NumberInput} label="Cooldown" help="How long it stays quiet in a channel after it fires. 0 means no wait." addon="seconds" min=${0} max=${86400} ...${set('cooldown')} />`}
    <${Switch} label="Trusted" help="A trusted trigger can use command blocks, {cmdAdmin} and {override}, and works in channels whoever wrote it can't reach. Without trust it only reads and posts where they can, and it stops if they leave. Only the server owner or an Administrator can turn this on, and it turns off if anyone else changes the response or what sets it off." disabled=${!canTrust && !form.trusted} value=${form.trusted} error=${errors.trusted} onChange=${setTrusted} />
  <//>`;
}

function Ignores() {
  const form = useSettings('triggers');
  return html`<${SettingsGate} form=${form}>${() => {
    const members = form.data.ignored_members;
    return html`<${IgnoresCard} form=${form} members=${members} />`;
  }}<//>`;
}

function IgnoresCard({ form, members }) {
  return html`
      <${Card} icon="eye-off" title="Ignored by triggers" footer=${html`<${SaveButton} form=${form} />`}>
        <p class="muted">Messages from these channels and members set off nothing, and neither do role changes on these members. A channel-specific trigger still listens in its own channel, and a role trigger still posts in the channel it names, even when that channel is ignored.</p>
        <${ChannelPicker} label="Ignored channels" multi categories types=${ANY_CHANNEL} ...${form.bind('ignored_channels')} />
        <${PeopleField} label="Ignored members" placeholder="Search by name or ID to add someone" ...${form.bind('ignored_members')} />
      <//>
      <${SaveBar} form=${form} />`;
}

export default function TriggersPage() {
  const guild = useGuild();
  const [rows, setRows] = useState(null);
  const [matchTypes, setMatchTypes] = useState([]);
  const [canTrust, setCanTrust] = useState(false);
  const [editing, setEditing] = useState(null);
  const [importing, setImporting] = useState(false);
  const load = () => api.get('/triggers').then((data) => { setRows(data.triggers); setMatchTypes(data.match_types); setCanTrust(data.can_trust); }).catch((error) => { toastError(error); setRows([]); });
  useEffect(() => { load(); }, [guild.id]);
  const nameOf = (id) => (matchTypes.find((type) => type.id === id) || { name: '' }).name;
  const shown = (row) => (row.match_type === ROLE ? `Any role change, posts in #${channelName(guild, row.channel_id)}` : row.trigger);
  const exportAll = async () => {
    try {
      downloadJson(`triggers-${guild.id}.json`, await api.get('/triggers/export'));
    } catch (error) {
      toastError(error);
    }
  };
  const closeAndLoad = () => { setEditing(null); setImporting(false); load(); };
  return html`
    <${Card} icon="message-square" title="Triggers">
      <div class="row" style=${{ marginBottom: '14px' }}>
        <button class="btn success" onClick=${() => setEditing('new')}><${Icon} name="plus" /> Create new trigger</button>
        <div class="right row">
          <button class="btn" onClick=${() => setImporting(true)}><${Icon} name="upload" size=${14} /> Import</button>
          <button class="btn" disabled=${!rows || rows.length === 0} onClick=${exportAll}><${Icon} name="download" size=${14} /> Export</button>
        </div>
      </div>
      <${DataTable} loading=${rows === null} rows=${rows || []} columns=${[
        { key: 'trigger', label: 'Trigger', sort: true, filter: true, width: '260px', text: shown, render: (row) => html`${shown(row)}
          ${row.trusted ? html` <span class="badge green" title="Its command blocks run.">trusted</span>` : ''}
          ${row.skipped ? html` <span class="badge red" title="This pattern took too long on a message lately. It skips that message and keeps judging others.">slow</span>` : ''}
          ${row.writer_gone ? html` <span class="badge red" title="Whoever wrote it has left, so it stays quiet until someone trusts it or saves it again.">writer left</span>` : ''}
          ${row.gone_roles.length > 0 ? html` <span class="badge red" title="Its lists name a role that was deleted.">deleted role</span>` : ''}
          ${row.gone_channels.length > 0 ? html` <span class="badge red" title="It names a channel that was deleted, so it may stay quiet.">deleted channel</span>` : ''}
          ${row.problem ? html` <span class="badge red" title=${`${row.problem} (${new Date(row.problem_at * 1000).toLocaleString()})`}>problem</span>` : ''}` },
        { key: 'response', label: 'Response', sort: true, filter: true, text: (row) => row.message.content, render: (row) => html`${row.message.content.slice(0, 300)}${row.message.content.length > 300 ? '...' : ''}
          ${row.message.embeds.length > 0 && html` <span class="badge blue">embed</span>`}` },
        { key: 'type', label: 'Type', width: '140px', text: (row) => nameOf(row.match_type) },
        { key: 'actions', label: '', width: '100px', render: (row) => html`<button class="btn outline block" onClick=${() => setEditing(row)}>Edit</button>` },
      ]} />
    <//>
    <${Ignores} />
    ${editing && html`<${TriggerModal} key=${editing === 'new' ? 'new' : editing.id} row=${editing === 'new' ? null : editing} matchTypes=${matchTypes} canTrust=${canTrust} onClose=${() => setEditing(null)} onSaved=${closeAndLoad} />`}
    ${importing && html`<${ImportModal} title="Import triggers" path="/triggers/import" nameKey="trigger" nameLabel="Trigger" onClose=${() => setImporting(false)} onDone=${closeAndLoad} />`}`;
}
