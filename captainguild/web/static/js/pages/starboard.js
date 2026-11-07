// The starboard: its settings, and numbers on what got starred.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { ANY_CHANNEL, Card, ChannelPicker, Col, DataTable, EmojiButton, EmojiView, Field, Grid, Icon, Loading, Notice, NumberInput, SaveBar, SaveButton, SettingsGate, Switch, Tabs, channelName, toast, toastError, useGuild, useSettings, when } from '../ui.js';

function CreateCard({ form, onCreated }) {
  const [name, setName] = useState('starboard');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post('/starboard/create', { name });
      toast('Created.');
      onCreated();
    } catch (failure) {
      setError((failure.fields && failure.fields.name) || failure.message);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Card} icon="star" title="Starboard settings">
    <h2>No starboard found</h2>
    <h4>What is a Starboard?</h4>
    <p class="muted">Your members pick the highlights. Anyone can react to a message with a star, and once a message has enough stars the bot copies it to the starboard channel for everyone to see.</p>
    <hr class="rule" />
    <h4>Make a new channel for it</h4>
    <p class="muted">Members can read and react in the new channel, and only the bot can post.</p>
    <${Field} label="Channel name" error=${error}>
      <div class="input-group"><span class="addon first">#</span><input type="text" aria-label="Channel name" value=${name} onInput=${(e) => setName(e.currentTarget.value)} />
        <button class="btn success" disabled=${busy || !name.trim()} onClick=${create}>Create</button></div>
    <//>
    <hr class="rule" />
    <h4>Or use a channel you already have</h4>
    <${ChannelPicker} placeholder="Select a starboard channel" ...${form.bind('channel_id')} />
    <${SaveButton} form=${form} />
  <//>`;
}

function Settings({ form }) {
  const guild = useGuild();
  const emoji = form.data.emoji;
  const [status, setStatus] = useState(null);
  useEffect(() => { api.get('/starboard/status').then(setStatus).catch(() => {}); }, [guild.id, String(form.saved.channel_id)]);
  return html`
    ${status && status.problem && html`<${Notice} kind="warn">${status.problem}<//>`}
    ${status && status.last_error && status.last_error !== status.problem && html`<${Notice} kind="bad">${status.last_error} This happened ${when(status.last_error_at)}.<//>`}
    <${Grid}>
      <${Col}>
        <${Card} icon="star" title="Starboard settings" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Starboard Channel</h2>
          <p class="muted">Where starred messages get posted. Remove it to turn the starboard off. Existing posts stay where they are.</p>
          <${ChannelPicker} placeholder="Select a starboard channel" ...${form.bind('channel_id')} />
          <hr class="rule" />
          <h2>Star limit</h2>
          <p class="muted">How many stars a message needs before it's posted, from 1 to 100.</p>
          <${NumberInput} min=${1} max=${100} ...${form.bind('threshold')} />
          <hr class="rule" />
          <${Switch} label="Allow NSFW Content" help="Show images from messages starred in NSFW channels. A starboard that's an NSFW channel itself always shows them." ...${form.bind('nsfw')} />
          <${Switch} label="Show Jump Link" help="Adds a link back to the original message on each new post." ...${form.bind('show_jump')} />
          <${Switch} label="Allow Self Starring" help="Count stars people put on their own messages." ...${form.bind('star_self')} />
          <${Switch} label="Lock Starboard" help="Pauses the starboard. Stars added or removed while it's locked don't count." ...${form.bind('locked')} />
          <${Switch} label="Automatically Star New Entries" help="The bot adds a star to each new post." ...${form.bind('autostar')} />
        <//>
      <//>
      <${Col}>
        <${Card} icon="smile" title="Starboard Emoji" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Set Starboard Emoji</h2>
          <p class="muted">Which reaction counts as a star. A server emoji works too. If you change it, stars already counted stay, and only the new emoji counts from then on.</p>
          <div class="row">
            <${EmojiButton} value=${emoji} label="Pick the starboard emoji" onChange=${(value) => form.set('emoji', value)} />
            <span class="row"><${EmojiView} value=${emoji || '⭐'} size=${28} /> ${emoji ? '' : 'The standard star'}</span>
            ${emoji && html`<button class="btn ghost small" onClick=${() => form.set('emoji', null)}>Use the star</button>`}
          </div>
          ${form.errors.emoji && html`<div class="error">${form.errors.emoji}</div>`}
        <//>
        <${Card} icon="ban" title="Ignored channels" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Ignore stars in these channels</h2>
          <p class="muted">Stars in these channels or categories don't count, and that goes for their threads.</p>
          <${ChannelPicker} multi categories types=${ANY_CHANNEL} ...${form.bind('blacklist')} />
        <//>
        <${Card} icon="eye-off" title="Private channels" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Let private channels onto the board</h2>
          <p class="muted">A message stays off the board when someone who can open the board couldn't read its channel, since the post copies it for all of them. A channel or category listed here goes up anyway, and everyone who can open the board reads what's starred in it. Private threads never go up.</p>
          <${ChannelPicker} multi categories types=${ANY_CHANNEL} ...${form.bind('allow_private')} />
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />`;
}

function Tile({ label, value }) {
  return html`<div class="stat-tile"><b>${value}</b><span class="muted small">${label}</span></div>`;
}

function Stats() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  useEffect(() => { api.get('/starboard/stats').then(setData).catch((error) => { toastError(error); setData({ failed: true }); }); }, [guild.id]);
  if (!data) return html`<${Loading} />`;
  if (data.failed) return null;
  const person = (row) => row.name || `Left the server (${row.user_id})`;
  return html`<div>
    <div class="stat-tiles">
      <${Tile} label="Board posts" value=${data.totals.posts} />
      <${Tile} label="Stars on them" value=${data.totals.stars} />
      <${Tile} label="Stars given in all" value=${data.totals.given} />
      <${Tile} label="People who starred" value=${data.totals.givers} />
      ${data.totals.since && html`<${Tile} label="First post" value=${when(data.totals.since).split(',')[0]} />`}
    </div>
    <${Grid}>
      <${Col}>
        <${Card} icon="star" title="Most starred posts">
          <${DataTable} rows=${data.posts} rowKey="message_id" pageSizes=${[10]} empty="Nothing has reached the board yet." columns=${[
            { key: 'stars', label: 'Stars', width: '70px' },
            { key: 'preview', label: 'Message', render: (row) => html`<div class="truncate">${row.preview}</div><div class="small dim">${row.author_name || 'Left the server'} in #${channelName(guild, row.channel_id)}</div>` },
            { key: 'jump', label: '', width: '40px', render: (row) => html`<a class="icon-btn" href=${row.url} target="_blank" rel="noopener noreferrer" aria-label="Open the post in Discord" title="Open the post in Discord"><${Icon} name="external-link" /></a>` },
          ]} />
        <//>
      <//>
      <${Col}>
        <${Card} icon="trending-up" title="Most starred people">
          <${DataTable} rows=${data.authors} rowKey="user_id" pageSizes=${[10]} empty="Nobody yet." columns=${[
            { key: 'name', label: 'Member', text: person },
            { key: 'stars', label: 'Stars', width: '80px' },
            { key: 'posts', label: 'Posts', width: '80px' },
          ]} />
        <//>
        <${Card} icon="heart" title="Most generous">
          <${DataTable} rows=${data.givers} rowKey="user_id" pageSizes=${[10]} empty="Nobody yet." columns=${[
            { key: 'name', label: 'Member', text: person },
            { key: 'given', label: 'Messages starred', width: '150px' },
          ]} />
        <//>
      <//>
    <//>
  </div>`;
}

function Loaded({ onReload }) {
  const form = useSettings('starboard');
  const [tab, setTab] = useState('settings');
  return html`<${SettingsGate} form=${form}>${() => html`<div>
    <${Tabs} tabs=${[{ key: 'settings', label: 'Settings' }, { key: 'stats', label: 'Stats' }]} active=${tab} onChange=${setTab} />
    ${tab === 'settings' && (form.saved.channel_id || form.data.channel_id ? html`<${Settings} form=${form} />` : html`<div style=${{ maxWidth: '640px' }}><${CreateCard} form=${form} onCreated=${onReload} /></div>`)}
    ${tab === 'stats' && html`<${Stats} />`}
  </div>`}<//>`;
}

export default function StarboardPage() {
  const guild = useGuild();
  const [version, setVersion] = useState(0);
  // A new channel changes the server's channel list as well as these settings, so both load again.
  const reload = async () => {
    await guild.reload();
    setVersion(version + 1);
  };
  return html`<${Loaded} key=${version} onReload=${reload} />`;
}
