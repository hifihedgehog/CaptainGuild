// Welcome, leave and ban messages, the join DM, and birthdays.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { MessageEditor, MessagePreview, emptyMessage, sampleMessage } from '../embed.js';
import { roleWarnings } from './reactionroles.js';
import { Card, ChannelPicker, Col, DataTable, Grid, Icon, MemberPicker, Modal, Notice, NumberInput, RolePicker, SaveBar, SaveButton, Select, SettingsGate, Switch, TextInput, confirmDialog, toast, toastError, useGuild, useSettings } from '../ui.js';

const HOURS = Array.from({ length: 24 }, (_, hour) => ({ value: hour, label: `${String(hour).padStart(2, '0')}:00 UTC` }));
const DEFAULT_BIRTHDAY = { content: '\u{1F389} Happy Birthday {mention}!', embeds: [], buttons: [] };
// What the greeting tags fill in while the preview stands in for a real member.
const sampleValues = (guild) => ({ user: 'Sample Member', mention: '@Sample Member', server: guild.name, 'server(members)': '1,234' });
const hasText = (message) => !!(message && (message.content || (message.embeds && message.embeds.length)));

function TestDialog({ which, title, message, channel, onClose }) {
  const [target, setTarget] = useState(channel);
  const [busy, setBusy] = useState(false);
  const send = async () => {
    setBusy(true);
    try {
      await api.post('/greetings/test', { which, payload: message, channel_id: target });
      toast(which === 'dm' ? 'Sent to your DMs.' : 'Sent.');
      onClose();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([target]);
  const [start] = useState(snapshot);
  return html`<${Modal} title=${'Test the ' + title} icon="send" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button><button class="btn primary" disabled=${busy || (which !== 'dm' && !target)} onClick=${send}>Send test</button></div>`}>
    <p class="muted">${which === 'dm' ? 'This sends it to your DMs, as if you had just joined.' : 'This posts it in the channel you pick, as if it were about you. Nobody gets pinged by a test.'}</p>
    ${which !== 'dm' && html`<${ChannelPicker} label="Send it to" value=${target} onChange=${setTarget} />`}
    <${MessagePreview} value=${message} />
    <p class="small dim" style=${{ marginTop: '8px' }}>The preview shows variables as you wrote them. The test fills them in.</p>
  <//>`;
}

function MessageCard({ form, field, which, icon, cardTitle, heading, helper, placeholder, channel, onTest, children }) {
  const guild = useGuild();
  const message = form.data[field];
  const error = form.errors[field];
  return html`<${Card} icon=${icon} title=${cardTitle} footer=${html`
      <${SaveButton} form=${form} />
      <button class="btn outline" disabled=${!hasText(message)} onClick=${() => onTest({ which, title: heading.toLowerCase(), message, channel })}>Test</button>
      <button class="btn ghost danger" onClick=${() => form.set(field, emptyMessage())}>Clear</button>`}>
    <h2>${heading}</h2>
    ${helper && html`<p class="muted">${helper}</p>`}
    <${MessageEditor} value=${message} onChange=${(next) => form.set(field, next)} collapsed contentRows=${5} contentPlaceholder=${placeholder} />
    ${error && html`<div class="error">${error}</div>`}
    ${hasText(message) && html`<div class="field"><label>Preview with a sample member</label><${MessagePreview} value=${sampleMessage(message, sampleValues(guild))} /></div>`}
    ${children}
  <//>`;
}

function BirthdaysCard() {
  const [rows, setRows] = useState(null);
  const [person, setPerson] = useState(null);
  const [date, setDate] = useState('');
  const [error, setError] = useState(null);
  const [added, setAdded] = useState(0);
  const load = () => api.get('/greetings/birthdays').then(setRows).catch((failure) => { toastError(failure); setRows([]); });
  useEffect(() => { load(); }, []);
  const add = async () => {
    setError(null);
    try {
      await api.put(`/greetings/birthdays/${person}`, { birthday: date });
      toast('Saved.');
      setPerson(null);
      setDate('');
      setAdded(added + 1);
      load();
    } catch (failure) {
      setError((failure.fields && (failure.fields.birthday || failure.fields.user_id)) || failure.message);
    }
  };
  const remove = async (row) => {
    const sure = await confirmDialog({ title: 'Remove this birthday?', body: `${row.name || row.user_id} won't be announced anymore.`, confirm: 'Remove', danger: true });
    if (!sure) return;
    try {
      await api.del(`/greetings/birthdays/${row.user_id}`);
      toast('Removed.');
      load();
    } catch (failure) {
      toastError(failure);
    }
  };
  return html`<${Card} icon="calendar" title="Birthdays">
    <h2>Who has one set</h2>
    <p class="muted">Members set their own with <code>/birthday set</code>. Soonest come first.</p>
    <div class="input-row" style=${{ alignItems: 'start' }}>
      <${MemberPicker} key=${added} label="Member" value=${person} onChange=${setPerson} />
      <${TextInput} label="Birthday" placeholder="Day/month, such as 25/12" value=${date} onChange=${setDate} error=${error} />
    </div>
    <button class="btn success" style=${{ marginBottom: '14px' }} disabled=${!person || !date} onClick=${add}><${Icon} name="plus" /> Add birthday</button>
    <${DataTable} loading=${rows === null} rows=${rows || []} rowKey="user_id" empty="No birthdays are set." columns=${[
      { key: 'name', label: 'Member', filter: true, text: (row) => row.name || `Left the server (${row.user_id})` },
      { key: 'date', label: 'Birthday', filter: true, width: '170px' },
      { key: 'actions', label: '', width: '50px', render: (row) => html`<button class="icon-btn danger" aria-label="Remove this birthday" title="Remove this birthday" onClick=${() => remove(row)}><${Icon} name="trash-2" /></button>` },
    ]} />
  <//>`;
}

export default function GreetingsPage() {
  const form = useSettings('greetings');
  const guild = useGuild();
  const [testing, setTesting] = useState(null);
  const [problems, setProblems] = useState([]);
  useEffect(() => { api.get('/greetings/problems').then(setProblems).catch(() => {}); }, [guild.id]);
  return html`<${SettingsGate} form=${form}>${() => {
    const data = form.data;
    const farewellChannel = data.farewell_channel || data.welcome_channel;
    return html`
    ${problems.map((text) => html`<${Notice} key=${text} kind="warn">${text}<//>`)}
    <${Grid}>
      <${Col}>
        <${Card} title="Channel selection" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Welcome channel</h2>
          <p>Useful variables:</p>
          <p><code>{mention}</code> mentions the person joining or leaving.<br />
            <code>{user}</code> is their name, and <code>{user(proper)}</code> their full username.<br />
            <code>{server}</code> is the server's name.<br />
            <code>{server(members)}</code> is how many members there are once they've joined or left.</p>
          <p class="muted">The rest of the template language works too. <code>{random: first ~ second}</code> picks one of the texts, split at each ~, and <code>{ord: {server(members)}}</code> turns <code>8102</code> into <code>8,102nd</code>. An embed's image boxes take variables as well, such as <code>{user(avatar)}</code> for the thumbnail.</p>
          <hr class="rule" />
          <${ChannelPicker} label="Welcome Channel" placeholder="No channel selected" ...${form.bind('welcome_channel')} />
          <${ChannelPicker} label="Farewell Channel" placeholder="Use the welcome channel" ...${form.bind('farewell_channel')} />
          <${ChannelPicker} label="Ban Channel" placeholder="Use the farewell channel" ...${form.bind('ban_channel')} />
          <${Switch} label="Wait until they accept the rules" help="With the server's rules screening on, the welcome and join DM go out once someone accepts, and not when they first arrive." ...${form.bind('wait_for_rules')} />
          <${NumberInput} label="Joins in a minute before welcomes pause" min=${0} max=${1000} help="Past this many, the bot skips each welcome and join DM, and posts one line with the count once joins go quiet. Goodbye and ban messages pause the same way, with their own count. 0 welcomes everyone." ...${form.bind('burst_joins')} />
        <//>
        <${MessageCard} form=${form} field="farewell" which="farewell" icon="log-out" cardTitle="Goodbye Message" heading="Leave message" channel=${farewellChannel} onTest=${setTesting}
          placeholder="{user} has left. We are now {server(members)}." />
        <${MessageCard} form=${form} field="ban" which="ban" icon="shield" cardTitle="Ban Message" heading="Ban message" channel=${data.ban_channel || farewellChannel} onTest=${setTesting}
          helper="This goes to the channel above when someone is banned. The banned person doesn't get it." placeholder="{user} broke the rules one time too many." />
      <//>
      <${Col}>
        <${MessageCard} form=${form} field="welcome" which="welcome" icon="log-in" cardTitle="Welcome Message" heading="Welcome message" channel=${data.welcome_channel} onTest=${setTesting}
          placeholder="Hi {mention}, welcome to {server}." />
        <${MessageCard} form=${form} field="dm" which="dm" icon="mail" cardTitle="DM on join message" heading="DM on join" onTest=${setTesting}
          placeholder="Hi {user}, welcome to {server}. Start with the rules channel." />
        <${Card} icon="gift" title="Birthday" footer=${html`
            <${SaveButton} form=${form} />
            <button class="btn outline" disabled=${!hasText(data.birthday)} onClick=${() => setTesting({ which: 'birthday', title: 'birthday message', message: data.birthday, channel: data.birthday_channel })}>Test</button>
            <button class="btn ghost danger" onClick=${() => { form.set('birthday', DEFAULT_BIRTHDAY); form.set('birthday_hour', 0); form.set('birthday_role', null); }}>Reset</button>`}>
          <h2>Birthday Message</h2>
          <p class="muted">Posted on each member's birthday. A February 29 birthday falls on the 28th in other years.</p>
          <${MessageEditor} value=${data.birthday} onChange=${(next) => form.set('birthday', next)} collapsed contentPlaceholder="Happy Birthday {mention}!" />
          ${form.errors.birthday && html`<div class="error">${form.errors.birthday}</div>`}
          ${hasText(data.birthday) && html`<div class="field"><label>Preview with a sample member</label><${MessagePreview} value=${sampleMessage(data.birthday, sampleValues(guild))} /></div>`}
          <hr class="rule" />
          <${ChannelPicker} label="Birthday Channel" placeholder="No announcements" ...${form.bind('birthday_channel')} />
          <${Select} label="Announcement Time" options=${HOURS} ...${form.bind('birthday_hour')} help="The whole server shares one time." />
          <${RolePicker} label="Birthday Role" placeholder="Select role" help="Members hold it for 24 hours from their announcement." ...${form.bind('birthday_role')} />
          ${data.birthday_role && roleWarnings(guild, [data.birthday_role], guild).map((text) => html`<${Notice} key=${text} kind="warn">${text}<//>`)}
          <${Switch} label="Combine birthday messages" help="Sends one announcement when several members share a birthday, with {mention} naming them all." ...${form.bind('birthday_combine')} />
        <//>
        <${BirthdaysCard} />
      <//>
    <//>
    <${SaveBar} form=${form} />
    ${testing && html`<${TestDialog} ...${testing} onClose=${() => setTesting(null)} />`}`;
  }}<//>`;
}
