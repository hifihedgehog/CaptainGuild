// Voice link: roles members hold while they're in a voice channel.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, DataTable, Icon, Loading, Modal, Notice, RolePicker, channelName, confirmDialog, roleName, sameId, toast, toastError, useGuild } from '../ui.js';
import { roleWarnings } from './reactionroles.js';

function LinkModal({ link, rules, onClose, onSave }) {
  const guild = useGuild();
  const [channel, setChannel] = useState(link.channel_id);
  const [roles, setRoles] = useState(link.roles);
  const [problem, setProblem] = useState(null);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!channel || roles.length === 0) {
      setProblem('Pick a channel and at least one role.');
      return;
    }
    setBusy(true);
    const failure = await onSave(channel, roles);
    setBusy(false);
    if (failure) setProblem(failure);
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([channel, roles]);
  const [start] = useState(snapshot);
  return html`<${Modal} icon="link" title="Manage Voice Link" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn primary" disabled=${busy} onClick=${save}>${busy ? 'Saving...' : 'Save'}</button></div>`}>
    ${problem && html`<${Notice} kind="bad">${problem}<//>`}
    <${ChannelPicker} label="Channel" types=${[2, 13]} clearable=${false} value=${channel} onChange=${setChannel} />
    <${RolePicker} label="Roles to assign" multi value=${roles} onChange=${setRoles} />
    ${roleWarnings(guild, roles, rules).map((text) => html`<${Notice} key=${text} kind="warn">${text}<//>`)}
  <//>`;
}

export default function VoiceLinkPage() {
  const guild = useGuild();
  const [links, setLinks] = useState(null);
  const [rules, setRules] = useState({ elevated_roles: false, dangerous: [] });
  const [editing, setEditing] = useState(null);
  useEffect(() => {
    api.get('/settings/voicelink').then((data) => setLinks(data.links)).catch((error) => { toastError(error); setLinks([]); });
    api.get('/voicelink/rules').then(setRules).catch(toastError);
  }, [guild.id]);

  const store = async (next) => {
    const saved = await api.put('/settings/voicelink', { links: next });
    setLinks(saved.links);
  };
  const saveLink = async (channel, roles) => {
    const others = links.filter((row) => !editing.channel_id || !sameId(row.channel_id, editing.channel_id));
    try {
      await store([...others, { channel_id: channel, roles }]);
      toast('Saved.');
      setEditing(null);
      return null;
    } catch (error) {
      return (error.fields && error.fields.links) || error.message;
    }
  };
  const remove = async (row) => {
    const sure = await confirmDialog({ title: 'Delete this voice link?', body: `Members in ${channelName(guild, row.channel_id)} lose the roles it gave them.`, confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      await store(links.filter((other) => !sameId(other.channel_id, row.channel_id)));
      toast('Deleted.');
    } catch (error) {
      toastError(error);
    }
  };
  const rolesText = (row) => row.roles.map((id) => roleName(guild, id)).join(', ');
  return html`<div>
    <${Card} icon="link" title="Voice role linking">
      <button class="btn success" onClick=${() => setEditing({ channel_id: null, roles: [] })}><${Icon} name="plus" /> Create new voice link</button>
      <p class="muted" style=${{ marginTop: '12px', marginBottom: 0 }}>Members hold these roles while they're in the voice channel. The bot takes a role back when they leave, unless they had it before they joined.</p>
      <hr class="rule" />
      ${links === null ? html`<${Loading} />` : html`<${DataTable} rows=${links} rowKey="channel_id" empty="No voice links yet. Create one above." columns=${[
        { key: 'channel', label: 'Channel', sort: true, filter: true, text: (row) => channelName(guild, row.channel_id) },
        { key: 'roles', label: 'Roles', filter: true, text: rolesText },
        { key: 'actions', label: '', width: '220px', render: (row) => html`<div class="row" style=${{ flexWrap: 'nowrap' }}>
          <button class="btn primary" onClick=${() => setEditing(row)}>Edit</button>
          <button class="btn outline danger" onClick=${() => remove(row)}>Delete <${Icon} name="trash-2" size=${14} /></button></div>` },
      ]} />`}
    <//>
    ${editing && html`<${LinkModal} link=${editing} rules=${rules} onClose=${() => setEditing(null)} onSave=${saveLink} />`}
  </div>`;
}
