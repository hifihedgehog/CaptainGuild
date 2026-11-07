// Forum messages: a welcome in each new post of the forums set up here, a reminder in a post that goes quiet, and ways to mark a post solved.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Col, Grid, Icon, Loading, Notice, RolePicker, SaveBar, SaveButton, Select, SettingsGate, Switch, TextArea, sameId, toastError, useGuild, useSettings } from '../ui.js';

const FORUM_TYPES = [15, 16];
const NEW_FORUM = { forum_id: null, message: '', inactivity_message: '', solved_tag: null, helper_roles: [] };
const MESSAGE_MAX = 2000;

function ForumForm({ row, info, onChange, onRemove }) {
  const set = (key, value) => onChange({ ...row, [key]: value });
  const forum = info.forums.find((found) => sameId(found.id, row.forum_id));
  const tags = forum ? forum.tags : [];
  const count = info.counts.find((found) => sameId(found.forum_id, row.forum_id));
  return html`<div class="card" style=${{ padding: '12px', marginBottom: '12px' }}>
    <${ChannelPicker} label="Forum" types=${FORUM_TYPES} value=${row.forum_id} onChange=${(value) => onChange({ ...row, forum_id: value, solved_tag: null })} />
    <${TextArea} label="Welcome" rows=${8} maxLength=${MESSAGE_MAX} help="Posted in each new post once its author's first message is in. Empty posts no welcome."
      value=${row.message} onChange=${(value) => set('message', value)} />
    <${Select} label="Solved tag" help="With one set, the welcome carries a Mark as Solved button, Mark as Solution works in this forum, and quiet posts get the reminder."
      options=${[{ value: null, label: 'None' }, ...tags.map((tag) => ({ value: tag.id, label: tag.name }))]} value=${row.solved_tag} onChange=${(value) => set('solved_tag', value)} />
    <${RolePicker} label="Helper roles" multi help="They can mark any post in this forum solved. The default reminder pings them."
      value=${row.helper_roles} onChange=${(value) => set('helper_roles', value)} />
    <${TextArea} label="Reminder" rows=${5} maxLength=${MESSAGE_MAX} placeholder=${info.default_reminder}
      help="Posted once in a post that has had no message for six days. Empty sends the default text shown, with the helper roles above it."
      value=${row.inactivity_message} onChange=${(value) => set('inactivity_message', value)} />
    ${count && html`<p class="muted small">${count.welcomed.toLocaleString()} ${count.welcomed === 1 ? 'post' : 'posts'} welcomed, ${count.reminded.toLocaleString()} reminded.</p>`}
    <button class="btn small outline danger" onClick=${onRemove}><${Icon} name="trash-2" size=${14} /> Remove this forum</button>
  </div>`;
}

function Forums({ form, info }) {
  const rows = form.data.forums || [];
  const change = (index, row) => form.set('forums', rows.map((other, at) => (at === index ? row : other)));
  const remove = (index) => form.set('forums', rows.filter((_, at) => at !== index));
  return html`<${Card} icon="list" title="Forums" footer=${html`<${SaveButton} form=${form} />`}>
    ${form.errors.forums && html`<${Notice} kind="bad">${form.errors.forums}<//>`}
    ${!info && html`<${Loading} />`}
    ${info && rows.map((row, index) => html`<${ForumForm} key=${index} row=${row} info=${info} onChange=${(next) => change(index, next)} onRemove=${() => remove(index)} />`)}
    ${rows.length === 0 && html`<p class="muted">No forums set up yet.</p>`}
    <button class="btn" onClick=${() => form.set('forums', [...rows, { ...NEW_FORUM }])}><${Icon} name="plus" /> Add a forum</button>
  <//>`;
}

export default function ForumMessagesPage() {
  const guild = useGuild();
  const form = useSettings('forummessages');
  const [info, setInfo] = useState(null);
  useEffect(() => {
    setInfo(null);
    api.get('/forummessages/forums').then(setInfo).catch((error) => { toastError(error); setInfo({ forums: [], counts: [], default_reminder: '' }); });
  }, [guild.id]);
  return html`<${SettingsGate} form=${form}>${() => html`<div>
    <${Grid}>
      <${Col}>
        <${Card} icon="message-circle" title="Forum messages" footer=${html`<${SaveButton} form=${form} />`}>
          <${Switch} label="Switched on" help="Posts the welcomes and reminders below, and adds /set_message, /set_inactivity_message, /set_solved_tag, /set_helper_roles, /list_messages, /delete_message, /forum_log_channel, /clean_threads, /close_solved_threads, /check_recent_threads and the Mark as Solution menu to this server. Discord can take a minute to show them." ...${form.bind('enabled')} />
          <p class="muted">With a solved tag set, a forum's welcome carries a Mark as Solved button. The post's author, the forum's helper roles, anyone with Manage Threads in the forum and roles that skip Manage Threads on /close_solved_threads can press it, or right-click the message that fixed the problem and pick Mark as Solution, which also pins that message. Either one adds the solved tag and closes the post. Each post gets one welcome and at most one reminder.</p>
          <p class="muted">A welcome or a reminder pings the post's author and the people and roles its text names. Saving a text that pings @everyone, @here or a role members can't ping takes Mention Everyone in that forum.</p>
          <${ChannelPicker} label="Log channel" help="A line for each welcome, reminder, solved post and change to these settings, naming who did it." ...${form.bind('log_channel')} />
        <//>
        <${Card} icon="info" title="Placeholders">
          <p class="muted">In a welcome or a reminder, {{OP.Mention}} pings the post's author. {{OP.Name}}, {{OP.Username}} and {{OP.GlobalName}} name them, and {{OP.ID}} is their ID. {{OP.CreatedAt}} and {{OP.JoinedAt}} say how long ago they made their account and joined the server. {{OP.Avatar}} links their picture, {{OP.Discriminator}} is the number an old-style name had, and {{OP.Bot}} reads True for a bot.</p>
          <p class="muted">Wiki answers can answer new posts in the same forums. A post there then gets this welcome and, a few seconds later, the wiki's answer as a reply.</p>
        <//>
      <//>
      <${Col}>
        <${Forums} form=${form} info=${info} />
      <//>
    <//>
    <${SaveBar} form=${form} />
  </div>`}<//>`;
}
