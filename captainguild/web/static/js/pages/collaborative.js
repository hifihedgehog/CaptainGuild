// Collaborative messages: posts the bot made that named people and roles may edit, each with its history kept.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Col, DataTable, Grid, Icon, Loading, Modal, Notice, PeopleField, RolePicker, SaveBar, SaveButton, SettingsGate, Switch, channelName, roleName, toast, toastError, useGuild, useSettings, when } from '../ui.js';
import { memberLabel, useMembers } from '../members.js';

function CollaboratorsModal({ post, limits, onClose, onSaved }) {
  const [users, setUsers] = useState(post.users);
  const [roles, setRoles] = useState(post.roles);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const snapshot = () => JSON.stringify([users, roles]);
  const [start] = useState(snapshot);
  const save = async () => {
    setBusy(true);
    try {
      await api.put(`/collaborative/posts/${post.posted_id}`, { users, roles });
      toast('Saved.');
      onSaved();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Modal} icon="users" title="Collaborators" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn primary" disabled=${busy} onClick=${save}>${busy ? 'Saving...' : 'Save'}</button></div>`}>
    <p class="muted">${post.summary}</p>
    <${PeopleField} label="People" help=${`Up to ${limits.users}. Adding someone takes being able to post in that channel yourself.`} error=${errors.users} value=${users} onChange=${setUsers} />
    <${RolePicker} label="Roles" multi help=${`Up to ${limits.roles}. Everyone with one of these roles can edit the message.`} error=${errors.roles} value=${roles} onChange=${setRoles} />
  <//>`;
}

function Posts() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [editing, setEditing] = useState(null);
  const load = () => api.get('/collaborative/posts').then(setData).catch((error) => { toastError(error); setData({ rows: [], limits: { users: 0, roles: 0 } }); });
  useEffect(() => { load(); }, [guild.id]);
  useMembers(guild.id, data ? data.rows.flatMap((row) => row.users) : []);
  if (data === null) return html`<${Loading} />`;
  const people = (row) => row.users.map((id) => memberLabel(guild.id, id)).join(', ') || 'Nobody';
  const groups = (row) => row.roles.map((id) => roleName(guild, id)).join(', ') || 'None';
  return html`<div>
    <${DataTable} rows=${data.rows} rowKey="posted_id" empty="No collaborative messages yet. Create one with /create_collaborative_message." columns=${[
      { key: 'summary', label: 'Message', filter: true, render: (row) => html`<div class="truncate">${row.summary}</div>
          <div class="small dim">#${channelName(guild, row.channel_id)}, ${row.edited_at ? 'edited ' + when(row.edited_at) : 'posted ' + when(row.posted_at)}${row.deleted_at ? ', deleted' : ''}</div>` },
      { key: 'users', label: 'People', filter: true, text: people },
      { key: 'roles', label: 'Roles', filter: true, text: groups },
      { key: 'actions', label: '', width: '170px', render: (row) => html`<div class="row" style=${{ flexWrap: 'nowrap' }}>
          <button class="btn small primary" onClick=${() => setEditing(row)}>Collaborators</button>
          <a class="icon-btn" href=${row.url} target="_blank" rel="noopener noreferrer" aria-label="Open in Discord" title="Open in Discord"><${Icon} name="external-link" /></a></div>` },
    ]} />
    ${editing && html`<${CollaboratorsModal} post=${editing} limits=${data.limits} onClose=${() => setEditing(null)} onSaved=${() => { setEditing(null); load(); }} />`}
  </div>`;
}

export default function CollaborativePage() {
  const guild = useGuild();
  const form = useSettings('collaborative');
  return html`<${SettingsGate} form=${form}>${() => html`<div>
    <${Grid}>
      <${Col}>
        <${Card} icon="users" title="Collaborative messages" footer=${html`<${SaveButton} form=${form} />`}>
          <${Switch} label="Collaborative messages" help="Adds /create_collaborative_message, the commands that go with it, and five right-click menus to this server: Edit Collaborative Message, View Revision History, Revert to Previous Revision, Delete Collaborative Message and See Current Collaborators." ...${form.bind('enabled')} />
          <hr class="rule" />
          <h4>Who can create them</h4>
          <p class="muted">The owner and Administrators always can. Members with one of these roles can too.</p>
          ${!guild.is_admin && html`<${Notice}>Only the server owner or an Administrator can change these roles.<//>`}
          <${RolePicker} label="Creator roles" multi disabled=${!guild.is_admin} ...${form.bind('creator_roles')} />
          <hr class="rule" />
          <h4>Log</h4>
          <p class="muted">A line for each message created, edited, reverted, moved, copied or deleted, and each change to who can edit one.</p>
          <${ChannelPicker} label="Log channel" ...${form.bind('log_channel')} />
          <${Switch} label="Post the log" help="Off keeps the channel set and posts nothing." ...${form.bind('log_enabled')} />
        <//>
      <//>
      <${Col}>
        <${Card} icon="list" title="Collaborative messages on this server">
          <p class="muted">Collaborators edit, revert and delete a message from its right-click menu in Discord. Its full history is on the Embeds page too.</p>
          <${Posts} />
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />
  </div>`}<//>`;
}
