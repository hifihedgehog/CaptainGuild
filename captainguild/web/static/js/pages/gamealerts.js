// Free game and game deal alerts.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Col, Grid, Loading, NumberInput, Notice, Picker, RolePicker, SaveBar, SaveButton, SettingsGate, Switch, TextArea, toast, toastError, useGuild, useSettings, when } from '../ui.js';

function Status({ data }) {
  if (!data) return null;
  const problem = data.service.error || data.delivery.error;
  if (problem) return html`<${Notice} kind="bad">${problem} The bot keeps trying.<//>`;
  if (data.service.ok_at) return html`<p class="muted small">Last checked at ${when(data.service.ok_at)}.</p>`;
  return null;
}

export default function GameAlertsPage() {
  const guild = useGuild();
  const form = useSettings('notifications');
  const [info, setInfo] = useState(null);
  useEffect(() => { api.get('/gamealerts').then(setInfo).catch(toastError); }, [guild.id]);
  const test = async (kind) => {
    try {
      await api.post('/gamealerts/test', { kind });
      toast("A sample went to the notifications channel. Tests don't ping anyone.");
    } catch (error) { toastError(error); }
  };
  return html`<${SettingsGate} form=${form}>${() => (!info ? html`<${Loading} />` : html`
    ${!form.data.alert_channel && html`<${Notice} kind="warn">Pick a notifications channel, or nothing gets posted.<//>`}
    <${Grid}>
      <${Col}>
        <${Card} icon="gamepad-2" title="Game Alerts" footer=${html`<${SaveButton} form=${form} />`}>
          <${ChannelPicker} label="Notifications Channel" ...${form.bind('alert_channel')} />
          <${RolePicker} label="Role to mention" placeholder="No role" help="The role is pinged at the start of each alert, or wherever you put {role}. A role members can't ping takes Mention Everyone in that channel to save, and is made mentionable for a moment unless the bot holds Mention Everyone there." ...${form.bind('alert_role')} />
        <//>
        <${Card} icon="gift" title="Free games" footer=${html`<${SaveButton} form=${form} /><button class="btn" disabled=${form.dirty} onClick=${() => test('free')}>Send a sample</button>`}>
          <${Switch} label="Notifications for free games" help="Posts here when a game becomes free to keep, on the platforms you pick." ...${form.bind('alerts_enabled')} />
          <${Picker} label="Platforms" multi placeholder="Every platform" options=${info.platforms} ...${form.bind('free_platforms')} />
          <${TextArea} label="Text above the alert" rows=${2} maxLength=${1000} placeholder="Nothing, just the card" help="These fill in: {title} {platforms} {worth} {until} {link} {everyone} {role}. Only {everyone} pings everyone, and it takes Mention Everyone in that channel to save." ...${form.bind('free_format')} />
          <${Status} data=${info.free} />
          <p class="muted small">Free game data comes from <a href="https://www.gamerpower.com" target="_blank" rel="noopener noreferrer">GamerPower</a>, and each alert links back to it.</p>
        <//>
      <//>
      <${Col}>
        <${Card} icon="zap" title="Deals" footer=${html`<${SaveButton} form=${form} /><button class="btn" disabled=${form.dirty} onClick=${() => test('deals')}>Send a sample</button>`}>
          <${Switch} label="Notifications for discounts and offers on games" help="Posts here when a game goes on sale past the limits below. At most five alerts go out each hour." ...${form.bind('deals_enabled')} />
          <div class="input-row">
            <${NumberInput} label="Smallest discount" addon="%" min=${10} max=${95} ...${form.bind('discount_threshold')} />
            <${NumberInput} label="Lowest deal rating" addon="of 10" min=${0} max=${10} step=${0.5} help="CheapShark's own score of how good a deal is." ...${form.bind('deal_min_rating')} />
          </div>
          <${Picker} label="Stores" multi placeholder="Every store" options=${info.stores} ...${form.bind('deal_stores')} />
          <${TextArea} label="Text above the alert" rows=${2} maxLength=${1000} placeholder="Nothing, just the card" help="These fill in: {title} {store} {price} {discount} {link} {everyone} {role}. Only {everyone} pings everyone, and it takes Mention Everyone in that channel to save." ...${form.bind('deal_format')} />
          <${Status} data=${info.deals} />
          <p class="muted small">Deals and prices come from <a href="https://www.cheapshark.com" target="_blank" rel="noopener noreferrer">CheapShark</a>, in US dollars.</p>
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />`)}<//>`;
}
