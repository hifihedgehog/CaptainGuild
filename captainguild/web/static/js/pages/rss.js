// RSS feeds: any RSS or Atom address, read every minute, with each new entry posted under the feed's name.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Icon, Loading, Notice, SaveBar, SaveButton, SettingsGate, Switch, TextInput, channelName, confirmDialog, toast, toastError, useGuild, useSettings, when } from '../ui.js';

function AddFeed({ onAdded }) {
  const [url, setUrl] = useState('');
  const [channel, setChannel] = useState(null);
  const [name, setName] = useState('');
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const body = () => ({ url, channel_id: channel, name });
  const run = async (path, done) => {
    setBusy(true);
    setErrors({});
    try {
      await api.post(path, body());
      done();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const add = () => run('/rss', () => { toast('Feed added. Its newest entry posts within a minute.'); setUrl(''); setName(''); onAdded(); });
  const test = () => run('/rss/test', () => toast('The newest entry was posted.'));
  return html`<${Card} icon="plus" title="Add a feed">
    <${TextInput} label="Feed address" placeholder="https://example.com/feed.xml" maxLength=${2048} value=${url} onChange=${setUrl} error=${errors.url} help="Any RSS or Atom feed. The bot reads it once now to check it." />
    <${ChannelPicker} label="Channel" types=${[0, 5]} clearable=${false} value=${channel} onChange=${setChannel} error=${errors.channel_id} />
    <${TextInput} label="Name to post under" placeholder="The bot's own name" maxLength=${80} counter value=${name} onChange=${setName} error=${errors.name} help="Posts show this name with the bot's picture. Leave it empty to use the bot's name." />
    <div class="row">
      <button class="btn primary" disabled=${busy || !url || !channel} onClick=${add}><${Icon} name="plus" /> Add feed</button>
      <button class="btn" disabled=${busy || !url || !channel} onClick=${test}>Post the newest entry now</button>
    </div>
  <//>`;
}

function FeedList({ data, onChanged }) {
  const guild = useGuild();
  const remove = async (feed) => {
    const sure = await confirmDialog({ title: 'Stop this feed?', body: `${feed.url} stops posting in #${channelName(guild, feed.channel_id)}. Its earlier posts stay.`, confirm: 'Remove', danger: true });
    if (!sure) return;
    try {
      await api.delete(`/rss/${feed.id}`);
      toast('Removed.');
      onChanged();
    } catch (error) {
      toastError(error);
    }
  };
  return html`<${Card} icon="rss" title=${`Feeds (${data.total} of ${data.most})`}>
    ${data.feeds.length === 0 && html`<p class="muted">No feeds yet. Add one below.</p>`}
    ${data.feeds.map((feed) => html`<div class="field-edit" key=${feed.id}>
      <div class="spread"><b style=${{ overflowWrap: 'anywhere' }}>${feed.url}</b>
        <button class="btn outline danger" onClick=${() => remove(feed)}>Remove <${Icon} name="trash-2" size=${14} /></button></div>
      <div class="muted">Posts in #${channelName(guild, feed.channel_id)} as ${feed.name || 'the bot'}${feed.created_by_name ? `, added by ${feed.created_by_name}` : ''}</div>
      <div class="muted">${feed.checked_at ? `Read ${when(feed.checked_at)}` : 'Not read yet'}${feed.posted_at ? `, last post ${when(feed.posted_at)}` : ''}</div>
      ${feed.error && html`<${Notice} kind="warn">${feed.error}<//>`}
    </div>`)}
  <//>`;
}

export default function RssPage() {
  const guild = useGuild();
  const form = useSettings('rss');
  const [data, setData] = useState(null);
  const load = () => api.get('/rss').then(setData).catch((error) => { toastError(error); setData({ feeds: [], total: 0, most: 100 }); });
  useEffect(() => { load(); }, [guild.id]);
  return html`<${SettingsGate} form=${form}>${() => html`
    <${Card} icon="rss" title="RSS feeds" footer=${html`<${SaveButton} form=${form} />`}>
      <${Switch} label="Switched on" help="Reads every feed below once a minute and adds /add_feed, /remove_feed, /list_feeds, /test_feed and /rss_log_channel to this server. Discord can take a minute to show them." ...${form.bind('enabled')} />
      <p class="muted">Each new entry posts as its title, its text and a Read more link, under the feed's name. A new feed posts its newest entry first, as a check that it works. Up to five new entries post at a time, oldest first. Nobody is pinged. These are separate from Autofeeds, which posts your own messages on a schedule.</p>
      <${ChannelPicker} label="Log channel" help="Says here when someone adds or removes a feed." ...${form.bind('log_channel')} />
    <//>
    ${data ? html`<${FeedList} data=${data} onChanged=${load} />` : html`<${Loading} />`}
    <${AddFeed} onAdded=${load} />
    <${SaveBar} form=${form} />`}<//>`;
}
