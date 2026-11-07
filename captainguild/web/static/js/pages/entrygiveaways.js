// Entry giveaways: a channel, a thread or a forum where each member's first message is their one entry.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Loading, Notice, RolePicker, SaveBar, SaveButton, SettingsGate, Switch, channelName, confirmDialog, toast, toastError, useGuild, useSettings, when } from '../ui.js';

const FORUM = 15;
const MEDIA = 16;

function Place({ view, onChange }) {
  const guild = useGuild();
  const [picked, setPicked] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const setup = view.setup;
  const save = async (channelId) => {
    if (setup) {
      const sure = await confirmDialog({
        title: channelId ? 'Move the giveaway?' : 'Stop limiting entries?',
        body: 'Every entry counted so far is forgotten, and closed giveaways open again. Messages already posted stay.',
        confirm: channelId ? 'Move it' : 'Stop', danger: true,
      });
      if (!sure) return;
    }
    setBusy(true);
    setError(null);
    try {
      onChange(await api.put('/entrygiveaways/channel', { channel_id: channelId }));
      setPicked(null);
      toast(channelId ? 'Entries are limited there now.' : 'Stopped.');
    } catch (failure) {
      setError((failure.fields && failure.fields.channel_id) || failure.message);
      toastError(failure);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Card} icon="gift" title="Giveaway channel">
    ${setup ? html`<p>Limiting entries in <b>${setup.name || '#' + channelName(guild, setup.channel_id)}</b>${setup.kind === 'multi' ? ', one entry in each post' : ''}. Set ${when(setup.set_at)}${setup.set_by_name ? ` by ${setup.set_by_name}` : ''}.</p>`
      : html`<p class="muted">No giveaway is limited yet.</p>`}
    <p class="muted">Pick a text channel for one giveaway, or a forum or media channel where each post is its own giveaway. /set_giveaway_channel also takes a thread. Setting it starts every count over.</p>
    <${ChannelPicker} label="Channel or forum" types=${[0, 5, FORUM, MEDIA]} value=${picked} onChange=${setPicked} error=${error} />
    <div class="row">
      <button class="btn primary" disabled=${busy || !picked} onClick=${() => save(picked)}>Limit entries here</button>
      ${setup && html`<button class="btn outline danger" disabled=${busy} onClick=${() => save(null)}>Stop</button>`}
    </div>
  <//>`;
}

function Areas({ view, onChange }) {
  const flip = async (area) => {
    try {
      onChange(await api.post(`/entrygiveaways/areas/${area.area_id}/${area.closed ? 'reopen' : 'close'}`));
      toast(area.closed ? 'Open again. Entries count from here on.' : 'Closed. Anyone can talk there now.');
    } catch (error) {
      toastError(error);
    }
  };
  return html`<${Card} icon="list" title="Entries">
    <p>${view.total.toLocaleString()} ${view.total === 1 ? 'entry' : 'entries'} from ${view.unique.toLocaleString()} ${view.unique === 1 ? 'member' : 'members'}.</p>
    ${view.areas.length === 0 && html`<p class="muted">Nobody has entered yet.</p>`}
    ${view.areas.map((area) => html`<div class="field-edit" key=${area.area_id}>
      <div class="spread"><b>${area.name || `Post ${area.area_id}`}</b>
        <button class=${'btn ' + (area.closed ? 'primary' : 'outline')} onClick=${() => flip(area)}>${area.closed ? 'Reopen' : 'Close'}</button></div>
      <div class="muted">${area.entries.toLocaleString()} ${area.entries === 1 ? 'entry' : 'entries'}${area.closed ? ', closed: anyone can post and nothing counts' : ''}</div>
    </div>`)}
    <p class="muted" style=${{ marginTop: '12px' }}>Pick winners with /draw in Discord. It reads what was posted in the channel or post while the giveaway was open, leaves out bots, the staff role and anyone who has left the server, and gives each member one chance.</p>
  <//>`;
}

export default function EntryGiveawaysPage() {
  const guild = useGuild();
  const form = useSettings('entrygiveaways');
  const [view, setView] = useState(null);
  useEffect(() => { api.get('/entrygiveaways').then(setView).catch((error) => { toastError(error); setView({ setup: null, areas: [], total: 0, unique: 0 }); }); }, [guild.id]);
  return html`<${SettingsGate} form=${form}>${() => html`
    <${Card} icon="gift" title="Entry giveaways" footer=${html`<${SaveButton} form=${form} />`}>
      <${Switch} label="Switched on" help="Limits entries in the giveaway channel and adds /set_giveaway_channel, /set_giveaway_staff, /mark_closed, /draw and /statistics to this server. Discord can take a minute to show them." ...${form.bind('enabled')} />
      <p class="muted">Each member's first message is their entry. A second one is deleted, and they're told why, once every 20 seconds at most. Other bots' messages are deleted too. Deleting someone's entry lets them enter again. This is separate from /giveaway, which runs a timed giveaway with a button.</p>
      <${RolePicker} label="Staff role" help="Members with this role post as often as they like and are never drawn. Only the owner or an Administrator can change it." ...${form.bind('staff_role')} />
      ${!guild.is_admin && html`<${Notice}>Only the owner or an Administrator can change the staff role.<//>`}
    <//>
    ${view ? html`<${Place} view=${view} onChange=${setView} /><${Areas} view=${view} onChange=${setView} />` : html`<${Loading} />`}
    <${SaveBar} form=${form} />`}<//>`;
}
