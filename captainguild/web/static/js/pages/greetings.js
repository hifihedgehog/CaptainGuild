// Welcome, leave and ban messages, the join DM, and birthdays.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { MessageEditor, MessagePreview, emptyMessage, sampleMessage } from '../embed.js';
import { PAGE_INDEX } from '../nav.js';
import { roleWarnings } from './reactionroles.js';
import { Card, ChannelPicker, Col, DataTable, DurationInput, Grid, Icon, MemberPicker, Modal, Notice, NumberInput, RolePicker, SaveBar, SaveButton, Select, SettingsGate, Switch, TextArea, TextInput, confirmDialog, toast, toastError, useGuild, useSettings, useUnsaved } from '../ui.js';

const HOURS = Array.from({ length: 24 }, (_, hour) => ({ value: hour, label: `${String(hour).padStart(2, '0')}:00 UTC` }));
const DEFAULT_BIRTHDAY = { content: '\u{1F389} Happy Birthday {mention}!', embeds: [], buttons: [] };
// What the greeting tags fill in while the preview stands in for a real member. They're filled in this order, so a tag that holds another comes before it.
const sampleValues = (guild) => ({
  user: 'Sample Member', mention: '@Sample Member', server: guild.name, 'server(members)': '1,234', 'ord:{joined}': '1,235th', joined: '1235',
  invite: 'sample', 'invite(inviter)': 'Sample Inviter', 'invite(inviter_mention)': '@Sample Inviter', 'invite(uses)': '1',
  'user(profile_name)': 'Sample Member', 'user(global_name)': 'Sample Member', 'user(username)': 'samplemember',
});
const hasText = (message) => !!(message && (message.content || (message.embeds && message.embeds.length)));
const DELETE_UNITS = ['d', 'h', 'm'];
const DELETE_HELP = 'Up to 30 days. All zeros keeps every post. Posts already made follow a new time.';
// Who a message pings, as welcome_mentions, farewell_mentions and ban_mentions store it. `person` is who the message is about.
const pings = (person) => [
  { value: 'member', label: person },
  { value: 'roles', label: `${person}, and roles its text mentions` },
  { value: 'all', label: `${person}, roles, and @everyone or @here in its text` },
];
// What a number looked like where a pasted tag names no format of its own.
const NUMBER_FORMATS = [
  { value: 'plain', label: '8102nd' },
  { value: 'commas', label: '8,102nd' },
  { value: 'dots', label: '8.102nd' },
  { value: 'indian', label: '12,34,567th' },
  { value: 'arabic', label: 'Arabic-Indic digits' },
];
// A note names a tag between backticks, which the page shows as code.
const noteParts = (text) => text.split('`').map((part, index) => (index % 2 ? html`<code key=${index}>${part}</code>` : part));

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
    <p class="small dim" style=${{ marginTop: '8px' }}>The preview shows variables as you wrote them. The test fills them in, with a sample invite, and counts no join.</p>
  <//>`;
}

// Paste a message written with double-brace tags, see it in this bot's template language, and put it in the editor. Nothing is saved here.
function ImportDialog({ title, onUse, onClose }) {
  const [pasted, setPasted] = useState('');
  const [format, setFormat] = useState('plain');
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const convert = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.post('/greetings/convert', { text: pasted, number_format: format }));
    } catch (failure) {
      setResult(null);
      setError((failure.fields && (failure.fields.text || failure.fields.number_format)) || failure.message);
    } finally {
      setBusy(false);
    }
  };
  // Closing with something pasted asks first.
  const snapshot = () => JSON.stringify([pasted, format]);
  const [start] = useState(snapshot);
  return html`<${Modal} large title=${'Import the ' + title} icon="upload" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn" disabled=${busy || !pasted.trim()} onClick=${convert}>Convert</button>
      <button class="btn success" disabled=${busy || !result} onClick=${() => { onUse(result.message); onClose(); }}>Use this message</button></div>`}>
    <p class="muted">Paste a message that uses double-brace tags, such as <code>${'{{User.Mention}}'}</code> or <code>${'{{Ordinal(Guild.Members)}}'}</code>. It can be the message's text, or its JSON with the embeds. Convert writes it in this bot's variables and lists anything that has no match. Use this message puts it in the editor, and nothing is saved until you press Save.</p>
    <${TextArea} label="Text or JSON" rows=${8} value=${pasted} counter=${false} error=${error} onChange=${(value) => { setPasted(value); setResult(null); }} />
    <${Select} label="Number format" options=${NUMBER_FORMATS} value=${format} onChange=${(value) => { setFormat(value); setResult(null); }}
      help="How a number was written where its tag names no format, as in Ordinal(Guild.Members). Pick the one the message's old server used." />
    ${result && html`<hr class="rule" />
      ${result.notes.length > 0 && html`<${Notice} kind="warn"><b>Read these before you save.</b>
        <ul class="small" style=${{ margin: '6px 0 0', paddingLeft: '18px' }}>${result.notes.map((note, index) => html`<li key=${index}>${noteParts(note)}</li>`)}</ul><//>`}
      ${result.notes.length === 0 && html`<${Notice} kind="ok">Every tag has a match.<//>`}
      <div class="field"><label>Converted message</label>
        ${hasText(result.message) ? html`<${MessagePreview} value=${result.message} />` : html`<p class="muted">Nothing in it converts to a message.</p>`}</div>`}
  <//>`;
}

function MessageCard({ form, field, which, icon, cardTitle, heading, helper, placeholder, channel, onTest, onImport, children }) {
  const guild = useGuild();
  const message = form.data[field];
  const error = form.errors[field];
  return html`<${Card} icon=${icon} title=${cardTitle} footer=${html`
      <${SaveButton} form=${form} />
      <button class="btn outline" disabled=${!hasText(message)} onClick=${() => onTest({ which, title: heading.toLowerCase(), message, channel })}>Test</button>
      <button class="btn outline" onClick=${() => onImport({ field, title: heading.toLowerCase() })}>Import</button>
      <button class="btn ghost danger" onClick=${() => form.set(field, emptyMessage())}>Clear</button>`}>
    <h2>${heading}</h2>
    ${helper && html`<p class="muted">${helper}</p>`}
    <${MessageEditor} value=${message} onChange=${(next) => form.set(field, next)} collapsed contentRows=${5} contentPlaceholder=${placeholder} />
    ${error && html`<div class="error">${error}</div>`}
    ${hasText(message) && html`<div class="field"><label>Preview with a sample member</label><${MessagePreview} value=${sampleMessage(message, sampleValues(guild))} /></div>`}
    ${children}
  <//>`;
}

// The count behind {joined}. It saves on its own, apart from the page's settings.
function JoinedCard() {
  const guild = useGuild();
  const [held, setHeld] = useState(null);
  const [draft, setDraft] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    setHeld(null);
    api.get('/greetings/joined').then((answer) => { if (live) { setHeld(answer); setDraft(answer.joined); } }).catch(toastError);
    return () => { live = false; };
  }, [guild.id]);
  const changed = held !== null && draft !== null && draft !== held.joined;
  useUnsaved(changed);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const answer = await api.put('/greetings/joined', { joined: draft });
      setHeld(answer);
      setDraft(answer.joined);
      toast('Saved.');
    } catch (failure) {
      setError((failure.fields && failure.fields.joined) || failure.message);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Card} icon="users" title="Joined count" footer=${html`<button class="btn primary" disabled=${busy || !changed} onClick=${save}>${busy ? 'Saving...' : 'Save count'}</button>`}>
    <h2>Everyone who has joined</h2>
    <p class="muted"><code>{joined}</code> shows this count in a greeting. Every join adds one, a bot's too, and nobody leaving takes one away, so <code>{ord:{joined}}</code> reads <code>8,102nd</code> for the 8,102nd person to join.
      ${held && !held.counted ? ' Counting starts at the next join, from the number of members the server has then.' : ''}</p>
    <${NumberInput} label="People who have joined so far" min=${0} value=${draft} onChange=${setDraft} error=${error} disabled=${held === null}
      help="Set it to carry on from a count kept somewhere else. The next person to join gets this number plus one." />
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
  const [importing, setImporting] = useState(null);
  const [problems, setProblems] = useState([]);
  // A save can fix what a note is about, or bring a new one, so they're read again after each.
  useEffect(() => { api.get('/greetings/problems').then(setProblems).catch(() => {}); }, [guild.id, form.saved]);
  return html`<${SettingsGate} form=${form}>${() => {
    const data = form.data;
    const farewellChannel = data.farewell_channel || data.welcome_channel;
    // A ping the person saving can't send comes back under the ping's own name, such as welcome_mentions:roles or farewell:@everyone.
    const pingError = (kind) => form.errors[`${kind}_mentions`]
      || Object.keys(form.errors).filter((key) => key.startsWith(`${kind}_mentions:`) || key.startsWith(`${kind}:`)).map((key) => form.errors[key])[0];
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
            <code>{server(members)}</code> is how many members there are once they've joined or left.<br />
            <code>{joined}</code> is how many people have ever joined, which never goes down.<br />
            <code>{invite}</code> is the invite they joined through, written as its code, and <code>{invite(inviter)}</code> names who made it. The welcome message and the join DM know the invite.</p>
          <p class="muted">The rest of the template language works too. <code>{random: first ~ second}</code> picks one of the texts, split at each ~, and <code>{ord: {server(members)}}</code> turns <code>8102</code> into <code>8,102nd</code>. An embed's image boxes take variables as well, such as <code>{user(avatar)}</code> for the thumbnail.
            <code>{if({user(bot)}==true):A bot was added.|Welcome!}</code> says one thing for a bot and another for a person, and <code>{user(created_ago)}</code> shows how long ago their account was made.
            The <a href="/docs#templates" target="_blank" rel="noopener">template language</a> page lists every variable, with the number formats for <code>{ord}</code> and <code>{num}</code>.</p>
          <hr class="rule" />
          <${ChannelPicker} label="Welcome Channel" placeholder="No channel selected" ...${form.bind('welcome_channel')} />
          <${ChannelPicker} label="Farewell Channel" placeholder="Use the welcome channel" ...${form.bind('farewell_channel')} />
          <${ChannelPicker} label="Ban Channel" placeholder="Use the farewell channel" ...${form.bind('ban_channel')} />
          <${Switch} label="Wait until they accept the rules" help="With the server's rules screening on, the welcome and join DM go out once someone accepts, and not when they first arrive." ...${form.bind('wait_for_rules')} />
          <${NumberInput} label="Joins in a minute before welcomes pause" min=${0} max=${1000} help="Past this many, the bot skips each welcome and join DM, and posts one line with the count once joins go quiet. Goodbye and ban messages pause the same way, with their own count. 0 welcomes everyone." ...${form.bind('burst_joins')} />
        <//>
        <${JoinedCard} />
        <${MessageCard} form=${form} field="farewell" which="farewell" icon="log-out" cardTitle="Goodbye Message" heading="Leave message" channel=${farewellChannel} onTest=${setTesting} onImport=${setImporting}
          placeholder="{user} has left. We are now {server(members)}.">
          <hr class="rule" />
          <${Switch} label="Post the leave message" help="Off keeps the message and posts nothing. Test still sends it." ...${form.bind('farewell_on')} />
          <${Select} label="Who the leave message pings" options=${pings('The member who left')} ...${form.bind('farewell_mentions')} error=${pingError('farewell')}
            help="Roles, @everyone and @here get a ping only where the message's own text mentions them. A member's name never adds one. Saving a wider choice takes your own right to send that ping in the leave message's channel." />
          <${DurationInput} label="Delete each leave message after" units=${DELETE_UNITS} help=${DELETE_HELP} ...${form.bind('farewell_delete_after')} />
        <//>
        <${MessageCard} form=${form} field="ban" which="ban" icon="shield" cardTitle="Ban Message" heading="Ban message" channel=${data.ban_channel || farewellChannel} onTest=${setTesting} onImport=${setImporting}
          helper="This goes to the channel above when someone is banned. The banned person doesn't get it." placeholder="{user} broke the rules one time too many.">
          <hr class="rule" />
          <${Switch} label="Post the ban message" help="Off keeps the message and posts nothing. A banned person then gets the leave message, as with no ban message set." ...${form.bind('ban_on')} />
          <${Select} label="Who the ban message pings" options=${pings('The person who was banned')} ...${form.bind('ban_mentions')} error=${pingError('ban')}
            help="Roles, @everyone and @here get a ping only where the message's own text mentions them. A name never adds one. Saving a wider choice takes your own right to send that ping in the ban message's channel." />
          <${DurationInput} label="Delete each ban message after" units=${DELETE_UNITS} help=${DELETE_HELP} ...${form.bind('ban_delete_after')} />
        <//>
      <//>
      <${Col}>
        <${MessageCard} form=${form} field="welcome" which="welcome" icon="log-in" cardTitle="Welcome Message" heading="Welcome message" channel=${data.welcome_channel} onTest=${setTesting} onImport=${setImporting}
          placeholder="Hi {mention}, welcome to {server}.">
          ${PAGE_INDEX.welcomecard && html`<p class="muted">The welcome post and the join DM can carry a picture drawn for each member. Set it up on the <a href=${`/dashboard/${guild.id}/welcomecard`} data-nav="1">Welcome image</a> page.</p>`}
          <hr class="rule" />
          <${Switch} label="Post the welcome message" help="Off keeps the message and posts nothing. Test still sends it." ...${form.bind('welcome_on')} />
          <${Select} label="Who the welcome pings" options=${pings('The member it greets')} ...${form.bind('welcome_mentions')} error=${pingError('welcome')}
            help="Roles, @everyone and @here get a ping only where the message's own text mentions them. A member's name never adds one. Saving a wider choice takes your own right to send that ping in the welcome channel." />
          <${Switch} label="Welcome bots too" help="Off posts nothing when a bot is added. A bot never gets the join DM." ...${form.bind('welcome_bots')} />
          <${DurationInput} label="Delete each welcome after" units=${DELETE_UNITS} help=${DELETE_HELP} ...${form.bind('welcome_delete_after')} />
          <${Switch} label="Delete a member's welcome when they leave" help="Their welcome post goes when they leave, are kicked or are banned." ...${form.bind('welcome_delete_on_leave')} />
          <${NumberInput} label="Minutes before a rejoin is welcomed again" min=${0} max=${1440} help="Someone who leaves and comes back within this many minutes of their welcome gets no second welcome or join DM. 0 welcomes every join." ...${form.bind('welcome_again_after')} />
        <//>
        <${MessageCard} form=${form} field="dm" which="dm" icon="mail" cardTitle="DM on join message" heading="DM on join" onTest=${setTesting} onImport=${setImporting}
          placeholder="Hi {user}, welcome to {server}. Start with the rules channel.">
          <hr class="rule" />
          <${Switch} label="Send the join DM" help="Off keeps the message and sends nothing. Test still sends it." ...${form.bind('dm_on')} />
          <${Switch} label="Send the welcome message as the DM" help="The DM then says what the welcome post says, and the message above stays saved for when this is off." ...${form.bind('dm_same_as_welcome')} />
        <//>
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
    ${testing && html`<${TestDialog} ...${testing} onClose=${() => setTesting(null)} />`}
    ${importing && html`<${ImportDialog} title=${importing.title} onUse=${(message) => form.set(importing.field, message)} onClose=${() => setImporting(null)} />`}`;
  }}<//>`;
}
