import { html } from '../lib.js';
import { Card, Col, DurationInput, Grid, Icon, Notice, RolePicker, SaveBar, SaveButton, NumberInput, SettingsGate, Switch, useGuild, useSettings } from '../ui.js';
import { roleWarnings } from './reactionroles.js';

export default function AutorolesPage() {
  const form = useSettings('autoroles');
  const guild = useGuild();
  const warnings = (ids) => roleWarnings(guild, ids, guild).map((text) => html`<${Notice} key=${text} kind="warn">${text}<//>`);
  return html`<${SettingsGate} form=${form}>${() => {
    const timed = form.data.timed;
    const setTimed = (rows) => form.set('timed', rows);
    return html`
    <${Grid}>
      <${Col}>
        <${Card} icon="user-plus" title="Autoroles" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Autoroles</h2>
          <p class="muted">Everyone who joins gets these roles. Someone returning gets these plus their old roles, when putting roles back is on.</p>
          <${RolePicker} label="Autoroles" multi ...${form.bind('roles')} />
          ${warnings(form.data.roles)}
          <${Switch} label="Skip bots" help="Bots that join get no autoroles or timed roles." ...${form.bind('ignore_bots')} />
        <//>
        <${Card} icon="timer" title="Timed roles" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Timed roles</h2>
          <p class="muted">These roles arrive some time after someone joins. Removing one here also cancels it for anyone still waiting.</p>
          ${form.errors.timed && html`<${Notice} kind="bad">${form.errors.timed}<//>`}
          ${timed.map((row, index) => html`<div class="field-edit" key=${index}>
            <div class="spread"><b>Timed role ${index + 1}</b>
              <button class="icon-btn danger" aria-label="Remove timed role" onClick=${() => setTimed(timed.filter((_, i) => i !== index))}><${Icon} name="trash-2" /></button></div>
            <${RolePicker} label="Role" clearable=${false} value=${row.role_id} onChange=${(role_id) => setTimed(timed.map((r, i) => (i === index ? { ...r, role_id } : r)))} />
            <${DurationInput} label="Give it after" units=${['d', 'h', 'm']} value=${row.seconds} onChange=${(seconds) => setTimed(timed.map((r, i) => (i === index ? { ...r, seconds } : r)))} />
          </div>`)}
          ${warnings(timed.map((row) => row.role_id).filter(Boolean))}
          <button class="btn" disabled=${timed.length >= 25} onClick=${() => setTimed([...timed, { role_id: null, seconds: 3600 }])}><${Icon} name="plus" /> Add timed role</button>
        <//>
      <//>
      <${Col}>
        <${Card} icon="refresh-cw" title="Sticky roles" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Sticky roles</h2>
          <p class="muted">Returning members don't need their roles set up again. Roles that last only for a while, such as a mute, a birthday or a temporary role, don't come back this way. A mute still running goes back on when someone returns.</p>
          <${Switch} label="Put roles back when someone returns" ...${form.bind('reassign')} />
          <${NumberInput} label="Remember roles for" addon="days" min=${1} max=${365} ...${form.bind('reassign_days')} />
          <${RolePicker} label="Never put back" multi ...${form.bind('blacklist')} />
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />`;
  }}<//>`;
}
