// Reddit posts: the accounts whose new posts are relayed in full, one post sent by hand, and the courier that can deliver them.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Icon, Loading, Modal, Notice, RolePicker, SaveBar, SaveButton, SettingsGate, Switch, TextInput, channelName, confirmDialog, copyText, roleName, toast, toastError, useGuild, useSettings, when } from '../ui.js';

const ROLE_HELP = "Each post ends with a line that mentions these roles. A role members can't ping takes Mention Everyone in that channel to save, and is made mentionable for a moment unless the bot holds Mention Everyone there.";

function rolesLine(guild, ids) {
  return ids.length ? 'mentioning ' + ids.map((id) => '@' + roleName(guild, id)).join(', ') : 'mentioning nobody';
}

function EditFeed({ feed, onClose, onSaved }) {
  const [channel, setChannel] = useState(feed.channel_id);
  const [roles, setRoles] = useState(feed.role_ids);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    setErrors({});
    try {
      await api.put(`/reddit/${feed.id}`, { channel_id: channel, role_ids: roles });
      toast('Saved.');
      onSaved();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const snapshot = () => JSON.stringify([channel, roles]);
  const [start] = useState(snapshot);
  return html`<${Modal} title=${'u/' + feed.account} icon="megaphone" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn primary" disabled=${busy} onClick=${save}>Save</button></div>`}>
    <${ChannelPicker} label="Channel" clearable=${false} value=${channel} onChange=${setChannel} error=${errors.channel_id} />
    <${RolePicker} label="Roles to mention" multi value=${roles} onChange=${setRoles} error=${errors.role_ids} help=${ROLE_HELP} />
  <//>`;
}

function Feeds({ data, onChanged }) {
  const guild = useGuild();
  const [editing, setEditing] = useState(null);
  const remove = async (feed) => {
    const sure = await confirmDialog({ title: `Stop relaying u/${feed.account}?`, body: `Its new posts stop going to #${channelName(guild, feed.channel_id)}. The ones already there stay.`, confirm: 'Remove', danger: true });
    if (!sure) return;
    try {
      await api.del(`/reddit/${feed.id}`);
      toast('Removed.');
      onChanged();
    } catch (error) {
      toastError(error);
    }
  };
  return html`<${Card} icon="megaphone" title=${`Accounts (${data.feeds.length} of ${data.most})`}>
    ${data.feeds.length === 0 && html`<p class="muted">No accounts yet. Add one below.</p>`}
    ${data.feeds.map((feed) => html`<div class="field-edit" key=${feed.id}>
      <div class="spread"><b><a href=${'https://www.reddit.com/user/' + feed.account} target="_blank" rel="noopener noreferrer">u/${feed.account}</a></b>
        <div class="row">
          <button class="btn small primary" disabled=${feed.hidden} onClick=${() => setEditing(feed)}>Edit</button>
          <button class="btn small outline danger" disabled=${feed.hidden} onClick=${() => remove(feed)}>Remove <${Icon} name="trash-2" size=${14} /></button>
        </div></div>
      <div class="muted">Posts in #${channelName(guild, feed.channel_id)}${feed.hidden ? '' : ', ' + rolesLine(guild, feed.role_ids)}${feed.created_by_name ? `, added by ${feed.created_by_name}` : ''}</div>
      <div class="muted">${feed.read_at ? `Feed read ${when(feed.read_at)}` : 'Feed not read yet'}${feed.posted_at ? `, last post relayed ${when(feed.posted_at)}` : ''}${feed.waiting ? `, ${feed.waiting} waiting to go out` : ''}</div>
      ${feed.read_error && html`<${Notice} kind="warn">${feed.read_error}<//>`}
      ${feed.last_error && html`<${Notice} kind="warn">${feed.last_error}<//>`}
    </div>`)}
    ${editing && html`<${EditFeed} feed=${editing} onClose=${() => setEditing(null)} onSaved=${() => { setEditing(null); onChanged(); }} />`}
  <//>`;
}

function AddFeed({ onAdded }) {
  const [account, setAccount] = useState('');
  const [channel, setChannel] = useState(null);
  const [roles, setRoles] = useState([]);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const add = async () => {
    setBusy(true);
    setErrors({});
    try {
      await api.post('/reddit', { account, channel_id: channel, role_ids: roles });
      toast('Added. Posts the account makes from now on are relayed.');
      setAccount('');
      onAdded();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Card} icon="plus" title="Add an account">
    <${TextInput} label="Reddit account" placeholder="u/example" maxLength=${120} value=${account} onChange=${setAccount} error=${errors.account} help="The account's name or the address of its profile. Only its own posts are relayed, never its comments." />
    <${ChannelPicker} label="Channel" clearable=${false} value=${channel} onChange=${setChannel} error=${errors.channel_id} />
    <${RolePicker} label="Roles to mention" multi value=${roles} onChange=${setRoles} error=${errors.role_ids} help=${ROLE_HELP} />
    <button class="btn primary" disabled=${busy || !account || !channel} onClick=${add}><${Icon} name="plus" /> Add account</button>
  <//>`;
}

function RelayOne({ relay, onStarted }) {
  const guild = useGuild();
  const [link, setLink] = useState('');
  const [channel, setChannel] = useState(null);
  const [roles, setRoles] = useState([]);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const running = relay && (relay.state === 'reading' || relay.state === 'sending');
  const send = async () => {
    setBusy(true);
    setErrors({});
    try {
      await api.post('/reddit/post', { link, channel_id: channel, role_ids: roles });
      toast(`On its way to #${channelName(guild, channel)}.`);
      setLink('');
      onStarted();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Card} icon="send" title="Relay one post now">
    <p class="muted">Sends any Reddit post to a channel in full, whatever its age and whoever made it.</p>
    <${TextInput} label="Post link" placeholder="https://www.reddit.com/r/example/comments/..." maxLength=${2048} value=${link} onChange=${setLink} error=${errors.link} />
    <${ChannelPicker} label="Channel" clearable=${false} value=${channel} onChange=${setChannel} error=${errors.channel_id} />
    <${RolePicker} label="Roles to mention" multi value=${roles} onChange=${setRoles} error=${errors.role_ids} help=${ROLE_HELP} />
    <button class="btn primary" disabled=${busy || running || !link || !channel} onClick=${send}><${Icon} name="send" /> Relay it</button>
    ${relay && relay.state === 'reading' && html`<p class="muted">Reading the post from Reddit. That can take up to a minute.</p>`}
    ${relay && relay.state === 'sending' && html`<p class="muted">Sending "${relay.title}": ${relay.done} of ${relay.of} messages are out.</p>`}
    ${relay && relay.state === 'done' && html`<p class="muted">"${relay.title}" went out ${when(relay.at)}.${relay.error ? ' ' + relay.error : ''}</p>`}
    ${relay && relay.state === 'failed' && html`<${Notice} kind="bad">${relay.title ? `"${relay.title}" stopped after ${relay.done} messages. ` : ''}${relay.error}<//>`}
  <//>`;
}

function Courier({ data, onChanged }) {
  const guild = useGuild();
  const intake = data.intake;
  const [channel, setChannel] = useState(intake.intake_channel);
  const [address, setAddress] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async (work) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (failure) {
      setError((failure.fields || {}).intake_channel || null);
      toastError(failure);
    } finally {
      setBusy(false);
    }
  };
  const make = () => run(async () => {
    if (intake.intake_webhook) {
      const sure = await confirmDialog({ title: 'Make a new delivery address?', body: 'The address the courier has now stops working, so the new one has to go into its settings on Reddit.', confirm: 'Make a new one', danger: true });
      if (!sure) return;
    }
    setAddress((await api.post('/reddit/intake', { intake_channel: channel })).address);
    onChanged();
  });
  const show = () => run(async () => setAddress((await api.get('/reddit/intake')).address));
  const drop = () => run(async () => {
    const sure = await confirmDialog({ title: 'Remove the courier?', body: 'Its delivery address stops working and its webhook is deleted. Accounts are still read from their feeds.', confirm: 'Remove', danger: true });
    if (!sure) return;
    await api.del('/reddit/intake');
    setAddress(null);
    setChannel(null);
    onChanged();
  });
  return html`<${Card} icon="radio" title="Courier">
    <p class="muted">A courier is a second source of posts: a small app on Reddit's developer platform, uploaded under your own Reddit account, that hands each new post to a webhook in a channel you pick here. The bot reads each delivery from that channel and clears it once the post is on record. With a courier, posts arrive whether or not an account's feed answers. The app and its setup steps are in the courier folder of this bot's source.</p>
    ${!intake.reads_text && html`<${Notice} kind="warn">This bot runs without the Message Content intent, so it can't read what a courier delivers.<//>`}
    ${!data.admin && html`<p class="muted">Only the server owner or an Administrator sets up the courier.</p>`}
    ${data.admin && html`
      <${ChannelPicker} label="Channel the courier delivers to" clearable=${false} value=${channel} onChange=${setChannel} error=${error} help="A private channel nobody else posts in. The bot makes a webhook there named Reddit courier, and reads only what that webhook posts." />
      <div class="row">
        <button class="btn primary" disabled=${busy || !channel} onClick=${make}>${intake.intake_webhook ? 'Make a new delivery address' : 'Make the delivery address'}</button>
        ${intake.intake_webhook && html`<button class="btn" disabled=${busy} onClick=${show}>Show the address</button>`}
        ${intake.intake_webhook && html`<button class="btn outline danger" disabled=${busy} onClick=${drop}>Remove the courier</button>`}
      </div>
      ${address && html`<${TextInput} label="Delivery address" value=${address} disabled=${true} help="Paste this into the courier's settings on Reddit. Anyone who has it can deliver posts here, so keep it private."
        button=${html`<button class="btn" type="button" onClick=${() => copyText(address)}>Copy</button>`} />`}`}
    ${intake.intake_webhook && html`<p class="muted small">Delivering to #${channelName(guild, intake.intake_channel)}. ${intake.delivered_at ? `Last delivery ${when(intake.delivered_at)}.` : 'No delivery since the bot last started.'}</p>`}
  <//>`;
}

function Recent({ posts }) {
  const line = (post) => {
    if (post.state === 'sent') return `Relayed ${when(post.posted_at)}${post.note ? '. ' + post.note : ''}`;
    if (post.state === 'waiting') return `Waiting to go out since ${when(post.found_at)}`;
    return post.note || 'Passed over';
  };
  return html`<${Card} icon="list" title="Latest posts">
    ${posts.length === 0 && html`<p class="muted">Nothing has been relayed yet.</p>`}
    ${posts.map((post) => html`<div class="field-edit" key=${post.id}>
      <div class="spread"><b style=${{ overflowWrap: 'anywhere' }}><a href=${post.link} target="_blank" rel="noopener noreferrer">${post.title}</a></b>
        ${post.url && html`<a class="btn small" href=${post.url} target="_blank" rel="noopener noreferrer">Open in Discord</a>`}</div>
      <div class="muted">u/${post.account}, ${post.source === 'courier' ? 'from the courier' : post.source === 'command' ? 'sent by hand' : 'from the feed'}. ${line(post)}</div>
    </div>`)}
  <//>`;
}

export default function RedditPage() {
  const guild = useGuild();
  const form = useSettings('reddit');
  const [data, setData] = useState(null);
  const load = () => api.get('/reddit').then(setData).catch(toastError);
  useEffect(() => { load(); }, [guild.id]);
  // While a post is on its way, the page keeps up with it.
  const running = data && data.relay && (data.relay.state === 'reading' || data.relay.state === 'sending');
  useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [running]);
  return html`<${SettingsGate} form=${form}>${() => html`
    <${Card} icon="megaphone" title="Reddit posts" footer=${html`<${SaveButton} form=${form} />`}>
      <${Switch} label="Switched on" help="Relays the accounts below and adds /reddit to this server. Discord can take a minute to show it." ...${form.bind('enabled')} />
      <p class="muted">Each new post from an account below goes to its channel in full: the title as a heading, the post's own words, and its pictures and video. A post too long for one message goes out as several, each ending where the post shows a picture. The last message ends with a line that mentions the roles you pick, and nothing else in a post can ping anyone. Posts made before an account was added are never relayed.</p>
    <//>
    ${data ? html`
      <${Feeds} data=${data} onChanged=${load} />
      <${AddFeed} onAdded=${load} />
      <${RelayOne} relay=${data.relay} onStarted=${load} />
      <${Courier} data=${data} onChanged=${load} />
      <${Recent} posts=${data.posts} />` : html`<${Loading} />`}
    <${SaveBar} form=${form} />`}<//>`;
}
