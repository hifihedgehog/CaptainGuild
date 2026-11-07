// Messages kept at the bottom of a channel, and saved templates for them.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { MessageEditor, MessagePreview, emptyMessage } from '../embed.js';
import { Card, ChannelPicker, DataTable, DurationInput, Icon, Loading, Modal, Select, Switch, Tabs, TextInput, channelName, confirmDialog, toast, toastError, useGuild, when } from '../ui.js';

const DAY = 86400;

function Content({ row }) {
  return html`<div class="truncate">${row.summary}</div>
    ${row.payload.embeds.length > 0 && html`<span class="badge blue">embed</span>`}`;
}

function DeleteButton({ label, onClick }) {
  return html`<button class="icon-btn danger" aria-label=${label} title=${label} onClick=${onClick}><${Icon} name="trash-2" size=${18} /></button>`;
}

function StickyForm({ sticky, templates, onClose, onSaved }) {
  const guild = useGuild();
  const editing = !!sticky;
  const [message, setMessage] = useState(editing ? sticky.payload : emptyMessage());
  const [channel, setChannel] = useState(null);
  const [template, setTemplate] = useState('');
  const [seconds, setSeconds] = useState(DAY);
  const [forever, setForever] = useState(editing ? sticky.expires_at === null : false);
  const [changeTime, setChangeTime] = useState(false);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  // Closing with something typed asks first.
  const snapshot = () => JSON.stringify([message, channel, seconds, forever, changeTime]);
  const [start] = useState(snapshot);
  const pickTemplate = (id) => {
    setTemplate(id);
    const found = templates.find((t) => t.id === id);
    if (found) setMessage(found.payload);
  };
  const save = async () => {
    setBusy(true);
    setErrors({});
    try {
      if (editing) {
        const body = { payload: message };
        if (changeTime || forever !== (sticky.expires_at === null)) {
          if (forever) body.forever = true;
          else body.seconds = seconds;
        }
        await api.patch(`/sticky/${sticky.channel_id}`, body);
      } else {
        await api.post('/sticky', { channel_id: channel, payload: message, seconds, forever });
      }
      toast('Saved.');
      onSaved();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    const sure = await confirmDialog({ title: 'Remove this sticky message?', body: 'It stops coming back, and the copy in the channel is deleted.', confirm: 'Remove', danger: true });
    if (!sure) return;
    try {
      await api.del(`/sticky/${sticky.channel_id}`);
      toast('Removed.');
      onSaved();
    } catch (error) {
      toastError(error);
    }
  };
  return html`<${Modal} title=${editing ? 'Edit Sticky Message' : 'Create Sticky Message'} icon="pin" large onClose=${onClose} dirty=${snapshot() !== start} footer=${html`
      ${editing && html`<${DeleteButton} label="Delete sticky message" onClick=${remove} />`}
      <div class="right"><button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn primary" disabled=${busy || (!editing && !channel)} onClick=${save}>${editing ? 'Save' : 'Create'}</button></div>`}>
    <div class="history wide">
      <div>
        ${!editing && templates.length > 0 && html`<${Select} label="Template (Optional)" value=${template} onChange=${pickTemplate}
          options=${[{ value: '', label: 'No template' }, ...templates.map((t) => ({ value: t.id, label: t.name }))]} />`}
        ${editing && html`<${TextInput} label="Channel (Read-only)" disabled value=${'#' + channelName(guild, sticky.channel_id)} />`}
        ${editing && html`<${TextInput} label="Expires At (Read-only)" disabled value=${sticky.expires_at ? when(sticky.expires_at) : 'Stays until removed'} />`}
        ${!editing && html`<${ChannelPicker} label="Channel" value=${channel} onChange=${setChannel} error=${errors.channel_id} />`}
        <${MessageEditor} value=${message} onChange=${setMessage} contentLabel="Content" contentRows=${6} contentPlaceholder="Enter sticky message content" collapsed />
        ${errors.payload && html`<div class="error">${errors.payload}</div>`}
        <${Switch} label="Stays until removed" value=${forever} onChange=${setForever} />
        ${editing && !forever && sticky.expires_at !== null && html`<${Switch} label="Change when it ends" help="Counts from now." value=${changeTime} onChange=${setChangeTime} />`}
        ${!forever && (!editing || changeTime || sticky.expires_at === null) && html`<${DurationInput} label="Duration (Max duration 365 days)" units=${['d', 'h', 'm']} value=${seconds} onChange=${setSeconds} error=${errors.seconds} />`}
      </div>
      <div>
        <h4>Preview</h4>
        <${MessagePreview} value=${message} />
      </div>
    </div>
  <//>`;
}

function TemplateForm({ template, onClose, onSaved }) {
  const editing = !!template;
  const [name, setName] = useState(editing ? template.name : '');
  const [message, setMessage] = useState(editing ? template.payload : emptyMessage());
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const snapshot = () => JSON.stringify([name, message]);
  const [start] = useState(snapshot);
  const save = async () => {
    setBusy(true);
    setErrors({});
    try {
      if (editing) await api.patch(`/sticky/templates/${template.id}`, { name, payload: message });
      else await api.post('/sticky/templates', { name, payload: message });
      toast('Saved.');
      onSaved();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    const sure = await confirmDialog({ title: 'Delete this template?', body: 'Sticky messages already made from it stay as they are.', confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      await api.del(`/sticky/templates/${template.id}`);
      toast('Deleted.');
      onSaved();
    } catch (error) {
      toastError(error);
    }
  };
  return html`<${Modal} title=${editing ? 'Edit Sticky Template' : 'Create Sticky Template'} icon="layers-3" large onClose=${onClose} dirty=${snapshot() !== start} footer=${html`
      ${editing && html`<${DeleteButton} label="Delete template" onClick=${remove} />`}
      <div class="right"><button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn primary" disabled=${busy} onClick=${save}>${editing ? 'Save' : 'Create'}</button></div>`}>
    <div class="history wide">
      <div>
        <${TextInput} label="Template Name" placeholder="Enter template name" value=${name} onChange=${setName} error=${errors.name} />
        <${MessageEditor} value=${message} onChange=${setMessage} contentLabel="Content" contentRows=${6} contentPlaceholder="Enter sticky message content" collapsed />
        ${errors.payload && html`<div class="error">${errors.payload}</div>`}
      </div>
      <div>
        <h4>Preview</h4>
        <${MessagePreview} value=${message} />
      </div>
    </div>
  <//>`;
}

export default function StickyPage() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [tab, setTab] = useState('messages');
  const [dialog, setDialog] = useState(null);
  const load = () => api.get('/sticky').then(setData).catch((error) => { toastError(error); setData({ stickies: [], templates: [] }); });
  useEffect(() => { load(); }, [guild.id]);
  if (!data) return html`<${Loading} />`;
  const saved = () => {
    setDialog(null);
    load();
  };
  return html`<div>
    <${Tabs} tabs=${[{ key: 'messages', label: 'Messages' }, { key: 'templates', label: 'Templates' }]} active=${tab} onChange=${setTab} />
    ${tab === 'messages' && html`<${Card} icon="pin" title="Sticky Messages">
      <p class="muted">A sticky message stays at the bottom of its channel. When new messages arrive the bot posts it again, a few seconds after the channel goes quiet, and removes the old copy. Each channel holds one.</p>
      <button class="btn success" style=${{ marginBottom: '14px' }} onClick=${() => setDialog({ kind: 'sticky' })}><${Icon} name="plus" /> Create new sticky message</button>
      <${DataTable} rows=${data.stickies} rowKey="channel_id" empty="No sticky messages yet." columns=${[
        { key: 'channel', label: 'Channel', sort: true, filter: true, width: '220px', text: (row) => channelName(guild, row.channel_id), render: (row) => '#' + channelName(guild, row.channel_id) },
        { key: 'summary', label: 'Content', filter: true, render: (row) => html`<${Content} row=${row} />` },
        { key: 'expires', label: 'Expires', sort: true, width: '190px', text: (row) => row.expires_at || Infinity, render: (row) => (row.expires_at ? when(row.expires_at) : 'Until removed') },
        { key: 'actions', label: '', width: '90px', render: (row) => html`<button class="btn small outline" onClick=${() => setDialog({ kind: 'sticky', row })}>Edit</button>` },
      ]} />
    <//>`}
    ${tab === 'templates' && html`<${Card} icon="layers-3" title="Sticky Templates">
      <p class="muted">Save a message here once, then use it in any channel from the create form or with <code>/sticky template</code>.</p>
      <button class="btn success" style=${{ marginBottom: '14px' }} onClick=${() => setDialog({ kind: 'template' })}><${Icon} name="plus" /> Create new template</button>
      <${DataTable} rows=${data.templates} empty="No templates yet." columns=${[
        { key: 'name', label: 'Name', sort: true, filter: true, width: '240px' },
        { key: 'summary', label: 'Content', sort: true, filter: true, render: (row) => html`<${Content} row=${row} />` },
        { key: 'actions', label: '', width: '90px', render: (row) => html`<button class="btn small outline" onClick=${() => setDialog({ kind: 'template', row })}>Edit</button>` },
      ]} />
    <//>`}
    ${dialog && dialog.kind === 'sticky' && html`<${StickyForm} sticky=${dialog.row} templates=${data.templates} onClose=${() => setDialog(null)} onSaved=${saved} />`}
    ${dialog && dialog.kind === 'template' && html`<${TemplateForm} template=${dialog.row} onClose=${() => setDialog(null)} onSaved=${saved} />`}
  </div>`;
}
