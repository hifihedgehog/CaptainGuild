// Statistics settings: when the statistics read time, what views show by default, what's recorded, invite rules, and who sees and uses
// all of it. People with Manage Server change everything here. Someone holding a role named under "Who changes what" changes that part.
// Merged channels (the aggregates setting) live on the Statistics data page.
import { html, useEffect, useMemo, useState } from '../lib.js';
import { api } from '../api.js';
import {
  Card, ChannelPicker, Col, Field, Grid, Icon, Loading, Notice, NumberInput, Picker, RolePicker, SaveBar, Select, Switch, changedFields, copyText,
  toast, toastError, useGuild, useUnsaved,
} from '../ui.js';
import { EMOJI_STATE_LABELS, EMOJI_TYPE_LABELS, EMOJI_USE_LABELS, FAKE_LABELS, STATE_LABELS, readFrom, statsGet } from '../stats/common.js';

const AREA_NAMES = {
  time: 'Time', filters: 'What views show', tracking: 'Counting games and apps', invites: 'Invites', presets: 'Saving and deleting presets',
  commands: 'Where and how commands work', dashboard: 'Who sees statistics', channels: 'Channels left out and merged channels',
  counters: 'Counter channels', statroles: 'Stat roles',
};
// Parts whose roles work on a page of their own rather than on this one.
const AREA_PAGES = { counters: 'counters', statroles: 'statroles' };
const ACCESS = [
  ['managers', 'People with Manage Server', 'Administrators and anyone with Manage Server.'],
  ['staff', 'Staff too', 'Also the bot manager role and the dashboard staff roles set in Bot settings.'],
  ['members', 'Everyone in the server', 'Members see the statistics pages, read only, once they sign in, counting only the channels Discord shows them.'],
  ['public', 'Anyone with the link', "Anyone, signed in or not, sees the server's totals and charts for the channels everyone in the server sees, with no member names. Members who sign in see what members see."],
];
const COMMAND_ACCESS = [
  ['all', 'Everyone can use every statistics command'], ['restricted', 'Everyone, but a command about a member answers only about themselves'],
  ['self', "Only commands about the person using them, such as /me"], ['admins', 'Only Administrators and people with Manage Server'],
];

function useStatsSettings() {
  const [data, setData] = useState(null);
  const [saved, setSaved] = useState(null);
  const [meta, setMeta] = useState(null);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(null);
  const guild = useGuild();
  useEffect(() => {
    api.get('/statistics/settings').then((answer) => { setData(answer.settings); setSaved(answer.settings); setMeta(answer); }).catch(setFailed);
  }, [guild.id]);
  const dirty = data !== null && saved !== null && changedFields(data, saved).length > 0;
  useUnsaved(dirty);
  const set = (name, value) => {
    setData((now) => ({ ...now, [name]: value }));
    setErrors((now) => { const next = { ...now }; delete next[name]; return next; });
  };
  const save = async () => {
    setSaving(true);
    try {
      const fresh = await api.put('/statistics/settings', Object.fromEntries(changedFields(data, saved).map((name) => [name, data[name]])));
      setData(fresh.settings);
      setSaved(fresh.settings);
      setErrors({});
      toast('Saved.');
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setSaving(false);
    }
  };
  const may = (area) => !!meta && meta.areas.includes(area);
  return {
    data, meta, errors, dirty, saving, failed, save, set, may, reset: () => { setData(saved); setErrors({}); },
    bind: (name, area) => ({ value: data[name], onChange: (value) => set(name, value), error: errors[name], disabled: area ? !may(area) : !(meta && meta.all) }),
  };
}

function Checks({ names, labels, value, onChange, disabled }) {
  const on = new Set(value || []);
  return html`<div class="stats-checks">${names.map((name) => html`<label key=${name} class="check"><input type="checkbox" checked=${on.has(name)} disabled=${disabled}
    onChange=${(e) => onChange(names.filter((item) => (item === name ? e.currentTarget.checked : on.has(item))))} /><span>${labels[name] || name}</span></label>`)}</div>`;
}

function Radios({ options, value, onChange, disabled, name }) {
  return html`<div class="stats-radios" role="radiogroup" aria-label=${name}>${options.map(([key, label, help]) => html`<label key=${key} class="check stats-radio">
    <input type="radio" name=${name} checked=${value === key} disabled=${disabled} onChange=${() => onChange(key)} />
    <span><b>${label}</b>${help && html`<span class="help">${help}</span>`}</span></label>`)}</div>`;
}

function Lookback({ value, onChange, disabled, error }) {
  const current = value || { mode: 'days', days: 14, since: null };
  return html`<${Field} label="Period views start from" help="A view can pick any other period. This is what pages and commands show first." error=${error}>
    <${Radios} name="Lookback" disabled=${disabled} value=${current.mode} onChange=${(mode) => onChange({ ...current, mode })} options=${[
      ['days', 'A number of days back'], ['since', 'Since a date'], ['all', 'All time']]} />
    ${current.mode === 'days' && html`<${NumberInput} label="Days" min=${1} max=${36500} disabled=${disabled} value=${current.days} onChange=${(days) => onChange({ ...current, days: days || 1 })} />`}
    ${current.mode === 'since' && html`<div class="field"><label>Start date</label><input type="date" aria-label="Start date" disabled=${disabled} value=${current.since || ''}
      onInput=${(e) => onChange({ ...current, since: e.currentTarget.value || null })} /></div>`}
  <//>`;
}

function ZonePicker({ zones, value, onChange, disabled, error }) {
  const options = useMemo(() => zones.map(([name, offset]) => ({ value: name, label: `${name.replace(/_/g, ' ')} (${offset})` })), [zones]);
  return html`<${Picker} label="Time zone" help="Every chart, table and command reads days and hours in this zone. Changing it changes no stored count."
    options=${options} value=${value} clearable=${false} disabled=${disabled} error=${error} placeholder="Search for a city or region" onChange=${(v) => v && onChange(v)} />`;
}

function Labels({ value, onChange, disabled, invites }) {
  const rows = Object.entries(value || {});
  const [code, setCode] = useState('');
  const [label, setLabel] = useState('');
  const add = () => {
    const cleaned = code.trim();
    if (!/^(vanity:)?[A-Za-z0-9-]{2,32}$/.test(cleaned) || !label.trim()) { toast('Give an invite code and a label.', 'bad'); return; }
    onChange({ ...(value || {}), [cleaned]: label.trim().slice(0, 100) });
    setCode('');
    setLabel('');
  };
  return html`<${Field} label="Invite labels" help="A name for an invite code, shown beside the code on the Invites page and in commands.">
    ${rows.length > 0 && html`<div class="table-wrap"><table><tbody>${rows.map(([key, text]) => html`<tr key=${key}><td><code>${key}</code></td><td>${text}</td>
      <td class="num"><button type="button" class="icon-btn danger" disabled=${disabled} aria-label=${'Remove the label for ' + key}
        onClick=${() => { const next = { ...value }; delete next[key]; onChange(next); }}><${Icon} name="trash-2" size=${14} /></button></td></tr>`)}</tbody></table></div>`}
    <div class="row" style=${{ marginTop: '8px' }}>
      <input type="text" list="stats-invite-codes" aria-label="Invite code" placeholder="Invite code" value=${code} disabled=${disabled} onInput=${(e) => setCode(e.currentTarget.value)} style=${{ maxWidth: '200px' }} />
      <datalist id="stats-invite-codes">${(invites || []).map((row) => html`<option key=${row.code} value=${row.code} />`)}</datalist>
      <input type="text" aria-label="Label" placeholder="Label" maxlength="100" value=${label} disabled=${disabled} onInput=${(e) => setLabel(e.currentTarget.value)} style=${{ maxWidth: '260px' }} />
      <button type="button" class="btn small" disabled=${disabled} onClick=${add}><${Icon} name="plus" size=${14} /> Add</button>
    </div>
  <//>`;
}

export default function StatisticsSettingsPage() {
  const guild = useGuild();
  readFrom({ public: false, guild: guild.id });
  const form = useStatsSettings();
  const [ctx, setCtx] = useState(null);
  useEffect(() => { statsGet('/statistics/context').then(setCtx).catch(() => setCtx({ activities: [], invites: [], emoji: [], fake_reasons: Object.keys(FAKE_LABELS) })); }, [guild.id]);
  if (form.failed) return html`<${Notice} kind="bad">${form.failed.message}<//>`;
  if (!form.data || !ctx) return html`<${Loading} />`;
  const { data, meta } = form;
  const admin = meta.is_admin;
  const areaRoles = data.area_roles || {};
  const activities = (ctx.activities || []).map((row) => ({ value: String(row.id), label: row.name, note: row.verified ? '' : '(not verified)' }));
  const invites = (ctx.invites || []).map((row) => ({ value: row.code, label: row.label ? `${row.label} (${row.code})` : row.code }));
  const emoji = (ctx.emoji || []).map((row) => ({ value: row.key, label: row.name, group: row.kind === 'sticker' ? 'Stickers' : 'Server emoji' }));
  const mode = (name, area, block = 'Leave these out', allow = 'Only these') => html`<${Radios} name=${name} options=${[['block', block], ['allow', allow]]} ...${form.bind(name, area)} />`;
  return html`<div class="stats-settings">
    ${!meta.all && html`<${Notice}>Your roles let you change ${meta.areas.map((area) => AREA_NAMES[area].toLowerCase()).join(', ')}. The rest takes Manage Server.
      ${meta.areas.filter((area) => AREA_PAGES[area]).map((area) => html` <a key=${area} href=${`/dashboard/${guild.id}/${AREA_PAGES[area]}`} data-nav="1">${AREA_NAMES[area]}</a>`)}<//>`}
    <${Grid}>
      <${Col}>
        <${Card} icon="chart-column" title="Statistics">
          <h2>Statistics</h2>
          <p class="muted">Counts of messages, voice time, members, statuses, games, invites and emoji, kept per quarter hour. No message text is kept for them.</p>
          <${Switch} label="Statistics on" help="Off stops counting. What's counted stays." ...${form.bind('enabled')} />
          <p class="small muted">Merged channels and recounts are on the <a href=${`/dashboard/${guild.id}/statsdata`} data-nav="1">Statistics data</a> page.</p>
        <//>
        <${Card} icon="clock" title="Time">
          <${ZonePicker} zones=${meta.zones} ...${form.bind('timezone', 'time')} />
          <${Lookback} ...${form.bind('lookback', 'time')} />
          <${Select} label="Bucket size" help="How much time each point of a chart covers. A long period moves to a longer bucket on its own." options=${[
            { value: 'hour', label: 'Hour' }, { value: 'day', label: 'Day' }, { value: 'week', label: 'Week' }, { value: 'month', label: 'Month' }]} ...${form.bind('interval', 'time')} />
          <${Select} label="Voice and activity time in" options=${[{ value: 'minutes', label: 'Minutes' }, { value: 'hours', label: 'Hours' }, { value: 'days', label: 'Days' }]} ...${form.bind('time_unit', 'time')} />
          <${Switch} label="Smooth chart lines" help="Lines curve through each point. Anyone can switch it for their own view." ...${form.bind('smooth_charts', 'time')} />
        <//>
        <${Card} icon="database" title="What's recorded">
          <${ChannelPicker} multi categories types=${[0, 2, 5, 13, 15]} label="Channels to leave out entirely" help="Nothing in these channels or categories is counted from now on. Counts already kept stay."
            ...${form.bind('untracked_channels', 'channels')} />
          <${Switch} label="Count games and apps" help=${meta.presences ? 'Minutes members spend in each game or app, read once a minute.' : "Waits for Discord to give the bot the Presence intent, which it hasn't yet."} ...${form.bind('track_activities', 'tracking')} />
          <${Switch} label="Count verified apps only" help="Leaves out an app Discord can't confirm by its ID, so a renamed custom status can't pass as a game." ...${form.bind('verify_activities', 'tracking')} />
        <//>
        <${Card} icon="user-plus" title="Invites">
          ${meta.invites_readable === false && html`<${Notice} kind="warn">The bot can't read invites here. It needs Manage Server.<//>`}
          <p class="muted">A join counts unless it's fake for one of the reasons on the Invites page. These set two of them.</p>
          <${NumberInput} label="Minimum account age" addon="minutes" min=${0} max=${5256000} help="A newer account's join is fake. 0 turns this off. 4320 is three days." ...${form.bind('invite_min_account_minutes', 'invites')} />
          <${NumberInput} label="Minimum time in the server" addon="minutes" min=${0} max=${5256000} help="A join is fake until the member has been here this long. 0 turns this off." ...${form.bind('invite_min_join_minutes', 'invites')} />
          <${RolePicker} multi label="Required roles" help="A join is fake until the member gains these roles. Losing one later doesn't undo it." ...${form.bind('invite_required_roles', 'invites')} />
          <${Radios} name="invite_required_match" options=${[['any', 'Any one of them'], ['all', 'All of them']]} ...${form.bind('invite_required_match', 'invites')} />
          <${Labels} invites=${ctx.invites} ...${form.bind('invite_labels', 'invites')} />
        <//>
      <//>
      <${Col}>
        <${Card} icon="filter" title="What views show">
          <p class="muted">Every page and command starts from these. They hide what they leave out and never change what's recorded. A view can change them for itself in its query bar.</p>
          <${Select} label="Count" options=${[{ value: 'humans', label: 'People' }, { value: 'bots', label: 'Bots' }, { value: 'both', label: 'People and bots' }]} ...${form.bind('include', 'filters')} />
          <${Field} label="Voice states that count"><${Checks} names=${Object.keys(STATE_LABELS)} labels=${STATE_LABELS} ...${form.bind('voice_states', 'filters')} /><//>
          <${Field} label="Channels">${mode('channel_mode', 'filters')}<//>
          <${ChannelPicker} multi categories types=${[0, 2, 5, 13, 15]} placeholder="Channels and categories" ...${form.bind('channels', 'filters')} />
          <${Field} label="Voice channels"><${Radios} name="voice_mode" options=${[['same', 'Use the channel list'], ['block', 'Leave these out'], ['allow', 'Only these']]} ...${form.bind('voice_mode', 'filters')} /><//>
          ${data.voice_mode !== 'same' && html`<${ChannelPicker} multi categories types=${[2, 13]} placeholder="Voice channels" ...${form.bind('voice_channels', 'filters')} />`}
          <${Field} label="Members">${mode('member_mode', 'filters')}<//>
          <${MembersField} ...${form.bind('members', 'filters')} />
          <${RolePicker} multi label="Only people with one of these roles" help="Roles read as people hold them now, so someone who left drops out." ...${form.bind('roles_allow', 'filters')} />
          <${RolePicker} multi label="Leave out people with any of these roles" ...${form.bind('roles_block', 'filters')} />
          <${Field} label="Games and apps">${mode('activity_mode', 'filters')}<//>
          <${Picker} multi options=${activities} placeholder="Games and apps" ...${form.bind('activities', 'filters')} />
          <${Field} label="Invites">${mode('invite_mode', 'filters')}<//>
          <${Picker} multi options=${invites} placeholder="Invites" ...${form.bind('invites', 'filters')} />
          <${Field} label="Fake joins">
            <${Radios} name="fake_mode" options=${[['block', 'Leave out joins with'], ['allow', 'Show only joins with']]} ...${form.bind('fake_mode', 'filters')} />
            <${Radios} name="fake_match" options=${[['any', 'any of these reasons'], ['all', 'all of these reasons']]} ...${form.bind('fake_match', 'filters')} />
            <${Checks} names=${ctx.fake_reasons || Object.keys(FAKE_LABELS)} labels=${FAKE_LABELS} ...${form.bind('fake_reasons', 'filters')} />
          <//>
          <${Field} label="Emoji">${mode('emoji_mode', 'filters')}<//>
          <${Picker} multi options=${emoji} placeholder="Server emoji and stickers, up to 25" ...${form.bind('emoji', 'filters')} />
          <${Field} label="Emoji states listed"><${Checks} names=${Object.keys(EMOJI_STATE_LABELS)} labels=${EMOJI_STATE_LABELS} ...${form.bind('emoji_states', 'filters')} /><//>
          <${Field} label="Emoji types">
            <${Radios} name="emoji_type_mode" options=${[['block', 'Leave out'], ['allow', 'Only']]} ...${form.bind('emoji_type_mode', 'filters')} />
            <${Radios} name="emoji_type_match" options=${[['any', 'any of these'], ['all', 'all of these']]} ...${form.bind('emoji_type_match', 'filters')} />
            <${Checks} names=${Object.keys(EMOJI_TYPE_LABELS)} labels=${EMOJI_TYPE_LABELS} ...${form.bind('emoji_types', 'filters')} />
          <//>
          <${Field} label="Emoji uses that count"><${Checks} names=${Object.keys(EMOJI_USE_LABELS)} labels=${EMOJI_USE_LABELS} ...${form.bind('emoji_uses', 'filters')} /><//>
        <//>
        <${Card} icon="lock" title="Who sees and uses statistics">
          ${!admin && html`<p class="small muted">Only the owner or an Administrator changes who sees statistics and the roles below.</p>`}
          <${Field} label="Who sees the Statistics pages">
            <${Radios} name="dashboard_access" options=${ACCESS} ...${form.bind('dashboard_access', 'dashboard')} disabled=${!admin} />
          <//>
          ${data.dashboard_access === 'public' && html`<div class="row"><code class="clip">${meta.public_url}</code><button type="button" class="btn small" onClick=${() => copyText(meta.public_url)}><${Icon} name="copy" size=${14} /> Copy</button></div>`}
          <${RolePicker} multi label="Roles that always see the pages" help="Whatever the choice above." ...${form.bind('viewer_roles', 'dashboard')} disabled=${!admin} />
          <${RolePicker} multi label="Roles that open Analytics, drilldowns and CSV downloads"
            help="Each holder also needs Administrator, Manage Server or View Server Insights. People with Manage Server always can." ...${form.bind('drill_roles', 'dashboard')} disabled=${!admin} />
          <${Switch} label="Everyone who sees the pages changes their own view"
            help="They change the period and filters for themselves and load presets. Off, only staff and the roles below do, and everyone else sees the view these settings set."
            ...${form.bind('viewer_filters', 'dashboard')} />
          <${RolePicker} multi label="Roles that change their own view" help="They change the period and filters for themselves and load presets." ...${form.bind('filter_roles', 'dashboard')} disabled=${!admin} />
          <${RolePicker} multi label="Roles that load presets" help="They load the presets saved here and change the view no other way." ...${form.bind('preset_read_roles', 'dashboard')} disabled=${!admin} />
          <h4>Who changes what</h4>
          <p class="small muted">People with these roles change that part of these settings, or do that work, without Manage Server. A role here also opens the Statistics pages to them.</p>
          ${Object.keys(AREA_NAMES).map((area) => html`<${RolePicker} key=${area} multi label=${AREA_NAMES[area]} value=${areaRoles[area] || []} disabled=${!admin}
            onChange=${(roles) => form.set('area_roles', { ...areaRoles, [area]: roles })} error=${form.errors.area_roles} />`)}
          <hr />
          <h4>Statistics commands</h4>
          <${Radios} name="command_access" options=${COMMAND_ACCESS} ...${form.bind('command_access', 'commands')} />
          <${Field} label="Channels"><${Radios} name="command_channel_mode" options=${[['block', 'Work everywhere except these'], ['allow', 'Work only in these']]} ...${form.bind('command_channel_mode', 'commands')} /><//>
          <${ChannelPicker} multi types=${[0, 2, 5, 13, 15]} placeholder="Channels" ...${form.bind('command_channels', 'commands')} />
          <${RolePicker} multi label="Roles that skip these limits" help="Administrators and people with Manage Server always do." ...${form.bind('override_roles', 'commands')} disabled=${!admin} />
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />
  </div>`;
}

// Members for the view filters, by name or ID, from everyone the statistics hold a name for.
function MembersField({ value, onChange, disabled }) {
  const [query, setQuery] = useState('');
  const [found, setFound] = useState([]);
  const [names, setNames] = useState({});
  useEffect(() => {
    if (query.trim().length < 2) { setFound([]); return undefined; }
    const timer = setTimeout(() => statsGet('/statistics/people', { q: query.trim() }).then((data) => setFound(data.people)).catch(() => setFound([])), 250);
    return () => clearTimeout(timer);
  }, [query]);
  const ids = (value || []).map(String);
  return html`<div class="field">
    ${ids.length > 0 && html`<div class="row log-chips">${ids.map((id) => html`<span class="chip" key=${id} title=${`ID ${id}`}>${names[id] || `ID ${id}`}
      <button type="button" disabled=${disabled} aria-label="Remove" onClick=${() => onChange(ids.filter((other) => other !== id))}><${Icon} name="x" size=${12} /></button></span>`)}</div>`}
    <div class="ms"><input type="text" disabled=${disabled} aria-label="Add a member by name or ID" placeholder="Add a member by name or ID" value=${query} onInput=${(e) => setQuery(e.currentTarget.value)} />
      ${found.length > 0 && html`<div class="ms-list" role="listbox">${found.map((person) => html`<div key=${person.id} role="option" class="ms-option" onMouseDown=${(e) => e.preventDefault()}
        onClick=${() => { setNames({ ...names, [person.id]: person.display_name }); if (!ids.includes(String(person.id))) onChange([...ids, String(person.id)]); setQuery(''); setFound([]); }}>
        ${person.display_name} <span class="dim">@${person.name}</span></div>`)}</div>`}</div>
  </div>`;
}
