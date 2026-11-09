// Counter channels: voice channels nobody can join, named with live counts and renamed on the clock.
// This file also holds the statistic pickers the Stat roles page reuses.
import { html, useEffect, useMemo, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import {
  Card, ChannelPicker, Check, EmojiButton, Field, Icon, Loading, Modal, Notice, NumberInput, PeopleField, Picker, RolePicker, Select, Switch, TextArea, TextInput,
  confirmDialog, sameId, toast, toastError, useGuild, when,
} from '../ui.js';

// ---- shared pieces ----

export const VOICE_STATES = [
  { value: 'talking', label: 'Talking' }, { value: 'afk', label: 'AFK' }, { value: 'self_muted', label: 'Muted themselves' },
  { value: 'self_deafened', label: 'Deafened themselves' }, { value: 'server_muted', label: 'Muted by staff' }, { value: 'server_deafened', label: 'Deafened by staff' },
];
export const ACTIVITY_KINDS = [
  { value: 'playing', label: 'Playing' }, { value: 'streaming', label: 'Streaming' }, { value: 'listening', label: 'Listening' },
  { value: 'watching', label: 'Watching' }, { value: 'competing', label: 'Competing' },
];
export const EMOJI_USES = [
  { value: 'reactions', label: 'Reactions' }, { value: 'self_reactions', label: 'Reactions to their own messages' },
  { value: 'messages', label: 'In messages' }, { value: 'stickers', label: 'Stickers' },
];
export const EMOJI_TYPES = [
  { value: 'server', label: 'This server’s' }, { value: 'animated', label: 'Animated' }, { value: 'managed', label: 'From an integration' },
  { value: 'restricted', label: 'Limited to roles' }, { value: 'super', label: 'Super reactions' },
];
export const EMOJI_STATES = [{ value: 'used', label: 'Still in the server' }, { value: 'deleted', label: 'Deleted' }, { value: 'unavailable', label: 'Unavailable' }];
export const FAKE_REASONS = [
  { value: 'self', label: 'Used their own invite' }, { value: 'rejoin', label: 'Joined before' }, { value: 'young_account', label: 'Account too new' },
  { value: 'new_member', label: 'Not in the server long enough yet' }, { value: 'no_avatar', label: 'No profile picture' }, { value: 'missing_role', label: 'Missing the required roles' },
  { value: 'onboarding', label: 'Onboarding unfinished' }, { value: 'home_actions', label: 'Server guide unfinished' }, { value: 'pending', label: 'Rules screen unfinished' },
  { value: 'left', label: 'Left' }, { value: 'bot', label: 'A bot' }, { value: 'temporary', label: 'Temporary membership' },
];
const INCLUDE = [{ value: 'both', label: 'People and bots' }, { value: 'humans', label: 'People only' }, { value: 'bots', label: 'Bots only' }];
const MODES = [{ value: 'block', label: 'Leave these out' }, { value: 'allow', label: 'Only these' }];

// A row of check boxes over a list, for options that take any of several values.
export function Checks({ label, help, options, value, onChange }) {
  const chosen = value || [];
  const flip = (item, on) => onChange(options.map((o) => o.value).filter((v) => (v === item ? on : chosen.includes(v))));
  return html`<${Field} label=${label} help=${help}>
    <div class="row">${options.map((o) => html`<${Check} key=${o.value} label=${o.label} value=${chosen.includes(o.value)} onChange=${(on) => flip(o.value, on)} />`)}</div>
  <//>`;
}

// How far back a statistic reaches: hours, days, a start date, or all time.
export function PeriodInput({ label = 'How far back', value, onChange, allowAll = true }) {
  const period = value || { amount: 1, unit: 'days' };
  const kind = period.all ? 'all' : period.since ? 'since' : period.unit || 'days';
  const options = [{ value: 'hours', label: 'Hours' }, { value: 'days', label: 'Days' }, { value: 'since', label: 'Since a date' }];
  if (allowAll) options.push({ value: 'all', label: 'All time' });
  const change = (next) => {
    if (next === 'all') onChange({ all: true });
    else if (next === 'since') onChange({ since: new Date().toISOString().slice(0, 10) });
    else onChange({ amount: period.amount || 1, unit: next });
  };
  return html`<${Field} label=${label}>
    <div class="input-row">
      <select aria-label=${label} value=${kind} onChange=${(e) => change(e.currentTarget.value)}>${options.map((o) => html`<option key=${o.value} value=${o.value}>${o.label}</option>`)}</select>
      ${(kind === 'hours' || kind === 'days') && html`<input type="number" min="1" aria-label="How many" value=${period.amount} onInput=${(e) => onChange({ amount: Number(e.currentTarget.value) || 1, unit: kind })} />`}
      ${kind === 'since' && html`<input type="date" aria-label="Start date" value=${period.since} onInput=${(e) => onChange({ since: e.currentTarget.value })} />`}
    </div>
  <//>`;
}

// The apps and emoji the server's statistics know, loaded once per page.
let choicesLoad = null;
export function useChoices() {
  const [choices, setChoices] = useState({ activities: [], emoji: [] });
  useEffect(() => {
    if (!choicesLoad) choicesLoad = api.get('/counters/choices').catch(() => ({ activities: [], emoji: [] }));
    choicesLoad.then(setChoices);
  }, []);
  return choices;
}

function EmojiList({ value, onChange, choices }) {
  const guild = useGuild();
  const keys = value || [];
  const named = (key) => {
    if (key.startsWith('u:')) return key.slice(2);
    const known = choices.emoji.find((row) => row.key === key) || (guild.emojis || []).find((row) => 'c:' + row.id === key);
    return known ? ':' + known.name + ':' : key;
  };
  const serverOptions = [
    ...(guild.emojis || []).map((row) => ({ value: 'c:' + row.id, label: ':' + row.name + ':' })),
    ...choices.emoji.filter((row) => row.key.startsWith('s:') || row.deleted).map((row) => ({ value: row.key, label: (row.key.startsWith('s:') ? 'Sticker ' : 'Deleted ') + row.name })),
  ];
  const add = (key) => { if (key && !keys.includes(key) && keys.length < 25) onChange([...keys, key]); };
  return html`<${Field} label="Emoji (up to 25)" help="Leave it empty to count every emoji.">
    ${keys.length > 0 && html`<div class="row">${keys.map((key) => html`<span class="chip" key=${key}>${named(key)}
      <button type="button" aria-label="Remove" onClick=${() => onChange(keys.filter((k) => k !== key))}><${Icon} name="x" size=${12} /></button></span>`)}</div>`}
    <div class="row">
      <${EmojiButton} label="Add an emoji" onChange=${(emoji) => { const custom = /^<a?:[^:>]+:(\d+)>$/.exec(emoji); add(custom ? 'c:' + custom[1] : 'u:' + emoji); }} />
      <div style=${{ minWidth: '220px', flex: 1 }}><${Picker} options=${serverOptions} value=${null} placeholder="Add a server emoji or sticker" onChange=${add} /></div>
    </div>
  <//>`;
}

function MatchList({ label, options, value, onChange }) {
  const current = value || { types: [], match: 'any' };
  return html`<div class="field-edit">
    <${Checks} label=${label} options=${options} value=${current.types} onChange=${(types) => onChange({ ...current, types })} />
    ${current.types.length > 1 && html`<${Select} label="Matching" value=${current.match} options=${[{ value: 'any', label: 'Any of them' }, { value: 'all', label: 'All of them' }]} onChange=${(match) => onChange({ ...current, match })} />`}
  </div>`;
}

// The filters a statistic can have. `people` adds the member and role lists, which counters count by and stat roles test by.
export function StatFilters({ stat, onChange, people = true, choices }) {
  const set = (key, value) => onChange({ ...stat, [key]: value });
  const kind = stat.kind;
  return html`<div>
    ${['messages', 'voice', 'activities', 'emoji', 'invites'].includes(kind) && stat.include !== undefined && html`<${Select} label="Whose" value=${stat.include || 'humans'} options=${INCLUDE} onChange=${(v) => set('include', v)} />`}
    ${['messages', 'voice', 'emoji'].includes(kind) && html`<div class="field-edit">
      <${Select} label="Channels" value=${stat.channel_mode || 'block'} options=${MODES} onChange=${(v) => set('channel_mode', v)} />
      <${ChannelPicker} label=${stat.channel_mode === 'allow' ? 'Count only these channels' : 'Leave out these channels'} multi categories types=${[0, 2, 5, 13, 15]} value=${stat.channels || []} onChange=${(v) => set('channels', v)} />
    </div>`}
    ${kind === 'voice' && html`<${Checks} label="Voice states" options=${VOICE_STATES} value=${stat.states} onChange=${(v) => set('states', v)} />`}
    ${kind === 'activities' && html`<div class="field-edit">
      <${Picker} label="Apps" help="Leave it empty to count every app." multi options=${choices.activities.map((row) => ({ value: row.id, label: row.name }))} value=${stat.activities || []} onChange=${(v) => set('activities', v)} />
      <${Checks} label="Kinds" options=${ACTIVITY_KINDS} value=${stat.activity_kinds} onChange=${(v) => set('activity_kinds', v)} />
    </div>`}
    ${kind === 'emoji' && html`<div class="field-edit">
      <${Select} label="Emoji" value=${stat.emoji_mode || 'block'} options=${MODES} onChange=${(v) => set('emoji_mode', v)} />
      <${EmojiList} value=${stat.emoji} choices=${choices} onChange=${(v) => set('emoji', v)} />
      <${Checks} label="Uses that count" options=${EMOJI_USES} value=${stat.emoji_uses} onChange=${(v) => set('emoji_uses', v)} />
      <${Checks} label="Emoji that count" options=${EMOJI_STATES} value=${stat.emoji_states} onChange=${(v) => set('emoji_states', v)} />
      <${MatchList} label="Count only these types" options=${EMOJI_TYPES} value=${stat.type_allow} onChange=${(v) => set('type_allow', v)} />
      <${MatchList} label="Leave out these types" options=${EMOJI_TYPES} value=${stat.type_block} onChange=${(v) => set('type_block', v)} />
      <p class="small dim">Types never leave stickers out, unless super reactions are among the types to count.</p>
    </div>`}
    ${kind === 'invites' && html`<div class="field-edit">
      <${Select} label="Invites" value=${stat.invite_mode || 'block'} options=${MODES} onChange=${(v) => set('invite_mode', v)} />
      <${TextInput} label="Invite codes" placeholder="abc123, def456" value=${(stat.invites || []).join(', ')} onChange=${(v) => set('invites', v.split(/[\s,]+/).filter(Boolean))} />
      <${MatchList} label="Count only joins with these fake reasons" options=${FAKE_REASONS} value=${stat.fake_allow} onChange=${(v) => set('fake_allow', v)} />
      <${MatchList} label="Leave out joins with these fake reasons" options=${FAKE_REASONS} value=${stat.fake_block} onChange=${(v) => set('fake_block', v)} />
    </div>`}
    ${kind === 'statuses' && html`<${Checks} label="Statuses" options=${[{ value: 'online', label: 'Online' }, { value: 'idle', label: 'Idle' }, { value: 'dnd', label: 'Do not disturb' }]} value=${stat.statuses} onChange=${(v) => set('statuses', v)} />`}
    ${people && ['messages', 'voice', 'activities', 'emoji', 'invites'].includes(kind) && html`<div class="field-edit">
      <${Select} label="Members" value=${stat.member_mode || 'block'} options=${MODES} onChange=${(v) => set('member_mode', v)} />
      <${PeopleField} label=${stat.member_mode === 'allow' ? 'Count only these members' : 'Leave out these members'} value=${stat.members || []} onChange=${(v) => set('members', v)} />
      <${RolePicker} label="Count only members with any of these roles" multi value=${stat.roles_allow || []} onChange=${(v) => set('roles_allow', v)} />
      <${RolePicker} label="Leave out members with any of these roles" multi value=${stat.roles_block || []} onChange=${(v) => set('roles_block', v)} />
    </div>`}
  </div>`;
}

// ---- counters ----

const TYPE_GROUPS = [
  { group: 'Discord', items: [['members', 'Members'], ['statuses', 'Members with a status'], ['role_members', 'Members in roles'], ['voice', 'Members in voice'], ['channels', 'Channels'], ['roles', 'Roles']] },
  { group: 'Time', items: [['datetime', 'Date and time'], ['countdown', 'Countdown']] },
  { group: 'Statistics', items: [['stat_count', 'Stat count'], ['stat_average', 'Stat average'], ['stat_change', 'Stat change'], ['stat_top', 'Stat top'], ['invites', 'Invite joins']] },
  { group: 'Other sites', items: [['youtube', 'YouTube subscribers'], ['instagram', 'Instagram followers'], ['tiktok', 'TikTok followers']] },
];
const TYPE_OPTIONS = TYPE_GROUPS.flatMap((g) => g.items.map(([value, label]) => ({ value, label, group: g.group })));
const TYPE_LABEL = Object.fromEntries(TYPE_OPTIONS.map((o) => [o.value, o.label]));
const WORDS = new Set(['countdown', 'datetime', 'stat_top']);
// What status counters show while Discord doesn't send the bot each member's status.
const SPLIT_WORDS = "Until Discord sends the bot each member's status, status counters show Discord's count of members present, split by this server's statuses at the same hour of the week over the last four weeks it has real statuses for.";
const NO_SPLIT_WORDS = "Discord hasn't given this bot each member's status. A counter of members online or not offline uses Discord's count of members present, which includes bots, and counters of idle or do not disturb alone keep their last value.";
const GOALS = new Set(['members', 'channels', 'statuses', 'role_members', 'roles', 'voice', 'stat_count', 'stat_average', 'stat_change', 'invites', 'youtube', 'instagram', 'tiktok']);
const CHANNEL_KINDS = [
  { value: 0, label: 'Text' }, { value: 5, label: 'Announcement' }, { value: 15, label: 'Forum' }, { value: 16, label: 'Media' },
  { value: 2, label: 'Voice' }, { value: 13, label: 'Stage' }, { value: 4, label: 'Category' },
];
const STATUS = [{ value: 'online', label: 'Online' }, { value: 'idle', label: 'Idle' }, { value: 'dnd', label: 'Do not disturb' }, { value: 'offline', label: 'Offline' }];
const STYLES = [{ value: 'plain', label: '13742' }, { value: 'separated', label: '13,742' }, { value: 'short', label: '13.7k' }];
const OFFSETS = [];
for (let minutes = -12 * 60; minutes <= 14 * 60; minutes += 15) {
  if (minutes % 60 === 0 || [30, 45].includes(Math.abs(minutes) % 60)) {
    const sign = minutes < 0 ? '-' : '+';
    const size = Math.abs(minutes);
    OFFSETS.push(`UTC${sign}${String(Math.floor(size / 60)).padStart(2, '0')}:${String(size % 60).padStart(2, '0')}`);
  }
}
const STAT_KINDS = {
  stat_count: ['messages', 'voice', 'activities', 'emoji', 'invites'],
  stat_average: ['messages', 'voice', 'activities', 'emoji', 'invites', 'statuses', 'members'],
  stat_change: ['messages', 'voice', 'activities', 'emoji', 'invites', 'statuses', 'members'],
  stat_top: ['messages', 'voice', 'activities', 'emoji', 'invites'],
};
const KIND_LABEL = { messages: 'Messages', voice: 'Voice time', activities: 'Time in apps', emoji: 'Emoji uses', invites: 'Invite joins', statuses: 'Members with a status', members: 'Member count' };
const TOP_OF = { messages: ['member', 'channel'], voice: ['member', 'channel'], activities: ['member', 'activity'], emoji: ['emoji', 'member', 'channel'], invites: ['member'] };
const TOP_LABEL = { member: 'Member', channel: 'Channel', emoji: 'Emoji', activity: 'App' };

export function blankStat(kind) {
  const stat = { kind, period: { amount: 1, unit: 'days' } };
  if (['messages', 'voice', 'activities', 'emoji', 'invites'].includes(kind)) Object.assign(stat, { include: 'humans', member_mode: 'block', members: [], roles_allow: [], roles_block: [] });
  if (['messages', 'voice', 'emoji'].includes(kind)) Object.assign(stat, { channel_mode: 'block', channels: [] });
  if (kind === 'voice') stat.states = VOICE_STATES.map((s) => s.value);
  if (kind === 'activities') Object.assign(stat, { activities: [], activity_kinds: ACTIVITY_KINDS.map((k) => k.value) });
  if (kind === 'emoji') Object.assign(stat, { emoji_mode: 'block', emoji: [], emoji_uses: EMOJI_USES.map((u) => u.value), emoji_states: EMOJI_STATES.map((s) => s.value), type_allow: { types: [], match: 'any' }, type_block: { types: [], match: 'any' }, measure: 'used' });
  if (kind === 'invites') Object.assign(stat, { invite_mode: 'block', invites: [], fake_allow: { types: [], match: 'any' }, fake_block: { types: [], match: 'any' } });
  if (kind === 'statuses') stat.statuses = ['online', 'idle', 'dnd'];
  return stat;
}

function blankCounter(type) {
  const base = { type };
  if (!WORDS.has(type)) base.style = 'plain';
  if (GOALS.has(type)) base.goal = null;
  if (['members', 'statuses', 'role_members', 'voice'].includes(type)) base.include = 'both';
  if (type === 'channels') base.channel_types = [0, 5, 15, 2, 13, 4];
  if (type === 'statuses') base.statuses = ['online', 'idle', 'dnd'];
  if (type === 'role_members') Object.assign(base, { roles: [], statuses: [] });
  if (type === 'roles') Object.assign(base, { managed: true, unmanaged: true });
  if (type === 'voice') Object.assign(base, { channel_mode: 'block', channels: [] });
  if (type === 'countdown') Object.assign(base, { end: Math.floor(Date.now() / 1000) + 86400, units: { days: 'd ', hours: 'h ', minutes: 'm' }, end_text: 'Countdown over' });
  if (type === 'datetime') Object.assign(base, { format: 'MMMM D, YYYY', zone: 'server' });
  if (type in STAT_KINDS) Object.assign(base, { stat: blankStat('messages'), decimals: type === 'stat_count' ? 0 : 1 });
  if (type === 'stat_average') base.per = 'day';
  if (type === 'stat_change') Object.assign(base, { as: 'value', plus: true });
  if (type === 'stat_top') Object.assign(base, { of: 'member', rank: 1, name: 'display', show_value: false, empty_text: 'Nobody yet' });
  if (type === 'invites') base.stat = { ...blankStat('invites'), period: { all: true } };
  if (['youtube', 'instagram', 'tiktok'].includes(type)) base.account = '';
  return base;
}

const blankSetup = () => ({ name: '', text: 'Members: {1}', counters: [blankCounter('members')], frequency: 10 });

function localInput(seconds) {
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function CounterOptions({ counter, onChange, choices }) {
  const guild = useGuild();
  const set = (key, value) => onChange({ ...counter, [key]: value });
  const type = counter.type;
  const units = counter.units || {};
  return html`<div>
    ${type === 'members' && html`<${Select} label="Count" value=${counter.include} options=${INCLUDE} onChange=${(v) => set('include', v)} />`}
    ${type === 'channels' && html`<${Checks} label="Kinds of channel" options=${CHANNEL_KINDS} value=${counter.channel_types} onChange=${(v) => set('channel_types', v)} />`}
    ${type === 'statuses' && html`<div>
      <${Checks} label="Statuses" options=${STATUS} value=${counter.statuses} onChange=${(v) => set('statuses', v)} />
      ${choices.presences === false && html`<div class="small muted">${choices.status_split ? SPLIT_WORDS : NO_SPLIT_WORDS}</div>`}
      <${Select} label="Count" value=${counter.include} options=${INCLUDE} onChange=${(v) => set('include', v)} />
    </div>`}
    ${type === 'role_members' && html`<div>
      <${RolePicker} label="Members with any of these roles" multi value=${counter.roles} onChange=${(v) => set('roles', v)} />
      <${Checks} label="Only with these statuses (leave them all off to count everyone)" options=${STATUS} value=${counter.statuses} onChange=${(v) => set('statuses', v)} />
      <${Select} label="Count" value=${counter.include} options=${INCLUDE} onChange=${(v) => set('include', v)} />
    </div>`}
    ${type === 'roles' && html`<div class="row">
      <${Check} label="Roles people hand out" value=${counter.unmanaged} onChange=${(v) => set('unmanaged', v)} />
      <${Check} label="Roles bots and integrations manage" value=${counter.managed} onChange=${(v) => set('managed', v)} />
    </div>`}
    ${type === 'voice' && html`<div>
      <${Select} label="Voice channels" value=${counter.channel_mode} options=${MODES} onChange=${(v) => set('channel_mode', v)} />
      <${ChannelPicker} label=${counter.channel_mode === 'allow' ? 'Count only these' : 'Leave out these'} multi categories types=${[2, 13]} value=${counter.channels} onChange=${(v) => set('channels', v)} />
      <${Select} label="Count" value=${counter.include} options=${INCLUDE} onChange=${(v) => set('include', v)} />
    </div>`}
    ${type === 'countdown' && html`<div>
      <${Field} label="Ends at (your time)">
        <input type="datetime-local" aria-label="Ends at" value=${localInput(counter.end)} onInput=${(e) => { const at = new Date(e.currentTarget.value).getTime(); if (at) set('end', Math.floor(at / 1000)); }} />
        <div class="row" style=${{ marginTop: '6px' }}>
          <button type="button" class="btn small" onClick=${() => set('end', Math.floor(Date.now() / 1000) + 86400)}>24 hours from now</button>
          <button type="button" class="btn small" onClick=${() => set('end', Math.floor(Date.now() / 1000) + 7 * 86400)}>One week from now</button>
        </div>
      <//>
      <${Field} label="Show" help="A unit left out rolls into the next smaller one, so days left out show as hours.">
        ${[['days', 'Days'], ['hours', 'Hours'], ['minutes', 'Minutes']].map(([key, label]) => html`<div class="row" key=${key}>
          <${Check} label=${label} value=${key in units} onChange=${(on) => { const next = { ...units }; if (on) next[key] = key[0]; else delete next[key]; set('units', next); }} />
          ${key in units && html`<input type="text" style=${{ maxWidth: '120px' }} aria-label=${label + ' text'} value=${units[key]} onInput=${(e) => set('units', { ...units, [key]: e.currentTarget.value })} />`}
        </div>`)}
      <//>
      <${TextInput} label="When it ends, the whole name becomes" maxLength=${100} value=${counter.end_text} onChange=${(v) => set('end_text', v)} />
    </div>`}
    ${type === 'datetime' && html`<div>
      <${TextInput} label="Format" help="Tokens such as dddd (Sunday), MMM (Nov), Do (8th), YYYY, h:mma (3:04pm), HH:mm and Q. Text in [brackets] stays as written." value=${counter.format} onChange=${(v) => set('format', v)} />
      <div class="row">${(choices.date_presets || []).map((preset) => html`<button type="button" class="btn small" key=${preset} onClick=${() => set('format', preset)}>${preset}</button>`)}</div>
      <${Select} label="Time zone" value=${counter.zone} options=${[{ value: 'server', label: 'The server’s zone, with daylight saving' }, ...(OFFSETS.includes(counter.zone) || counter.zone === 'server' ? [] : [{ value: counter.zone, label: counter.zone }]), ...OFFSETS.map((o) => ({ value: o, label: o + ' all year' }))]} onChange=${(v) => set('zone', v)} />
      <${TextInput} label="Or a named zone" placeholder="Europe/Paris" value=${OFFSETS.includes(counter.zone) || counter.zone === 'server' ? '' : counter.zone} onChange=${(v) => set('zone', v.trim() || 'server')} />
    </div>`}
    ${type in STAT_KINDS && html`<div>
      <${Select} label="Statistic" value=${counter.stat.kind} options=${STAT_KINDS[type].map((k) => ({ value: k, label: KIND_LABEL[k] }))}
        onChange=${(kind) => onChange({ ...counter, stat: { ...blankStat(kind), period: counter.stat.period }, ...(type === 'stat_top' ? { of: TOP_OF[kind][0] } : {}) })} />
      <${PeriodInput} value=${counter.stat.period} allowAll=${type !== 'stat_change'} onChange=${(v) => set('stat', { ...counter.stat, period: v })} />
      ${type === 'stat_average' && !['statuses', 'members'].includes(counter.stat.kind) && html`<${Select} label="Per" value=${counter.per} options=${[{ value: 'hour', label: 'Hour' }, { value: 'day', label: 'Day' }, { value: 'week', label: 'Week' }]} onChange=${(v) => set('per', v)} />`}
      ${type === 'stat_change' && html`<div class="input-row">
        <${Select} label="Show" value=${counter.as} options=${[{ value: 'value', label: 'The change' }, { value: 'percent', label: 'The change in percent' }]} onChange=${(v) => set('as', v)} />
        <${Switch} label="Plus sign on a rise" value=${counter.plus} onChange=${(v) => set('plus', v)} />
      </div>`}
      ${['voice', 'activities'].includes(counter.stat.kind) && type !== 'stat_top' && html`<${Select} label="In" value=${counter.unit || 'hours'} options=${[{ value: 'minutes', label: 'Minutes' }, { value: 'hours', label: 'Hours' }, { value: 'days', label: 'Days' }]} onChange=${(v) => set('unit', v)} />`}
      ${type !== 'stat_top' && html`<${NumberInput} label="Decimals" min=${0} max=${4} value=${counter.decimals} onChange=${(v) => set('decimals', v || 0)} />`}
      ${type === 'stat_top' && html`<div>
        <div class="input-row">
          <${Select} label="Show the" value=${counter.of} options=${TOP_OF[counter.stat.kind].map((v) => ({ value: v, label: TOP_LABEL[v] }))} onChange=${(v) => set('of', v)} />
          <${NumberInput} label="At place" min=${1} max=${50} value=${counter.rank} onChange=${(v) => set('rank', v || 1)} />
        </div>
        ${counter.of === 'member' && html`<${Select} label="Names show as" value=${counter.name} options=${[{ value: 'display', label: 'Display name' }, { value: 'nickname', label: 'Nickname here' }, { value: 'username', label: 'Username' }]} onChange=${(v) => set('name', v)} />`}
        ${counter.of === 'channel' && html`<p class="small dim">Only channels everyone in the server can see are ranked, since everyone sees the counter.</p>`}
        <${Switch} label="Show the amount too" value=${counter.show_value} onChange=${(v) => set('show_value', v)} />
        <${TextInput} label="When nobody is at that place" maxLength=${40} value=${counter.empty_text} onChange=${(v) => set('empty_text', v)} />
      </div>`}
      <${StatFilters} stat=${counter.stat} choices=${choices} onChange=${(v) => set('stat', v)} />
    </div>`}
    ${type === 'invites' && html`<div>
      <${PeriodInput} value=${counter.stat.period} onChange=${(v) => set('stat', { ...counter.stat, period: v })} />
      <${StatFilters} stat=${counter.stat} choices=${choices} onChange=${(v) => set('stat', v)} />
    </div>`}
    ${type === 'youtube' && html`<${TextInput} label="YouTube channel" placeholder="@handle or the channel ID that starts with UC" value=${counter.account} onChange=${(v) => set('account', v)} help="YouTube shows subscriber counts rounded to three digits, such as 246K, and so does the counter." />`}
    ${type === 'instagram' && html`<${TextInput} label="Instagram account" placeholder="username" value=${counter.account} onChange=${(v) => set('account', v)} />`}
    ${type === 'tiktok' && html`<${TextInput} label="TikTok account" placeholder="username" value=${counter.account} onChange=${(v) => set('account', v)} />`}
    ${!WORDS.has(type) && html`<div class="input-row">
      <${Select} label="Numbers show as" value=${counter.style || 'plain'} options=${STYLES} onChange=${(v) => set('style', v)} />
      ${GOALS.has(type) && html`<${NumberInput} label="Count down to a goal" help="Shows how far the count is from this number." min=${0} value=${counter.goal} onChange=${(v) => set('goal', v)} />`}
    </div>`}
  </div>`;
}

// The name as it will look, from the server's real values, refreshed a moment after each change.
// `where` names the channel it takes over, the category it goes in, or the counter it is, so the preview reads what that channel would show.
function LivePreview({ setup, where }) {
  const [shown, setShown] = useState(null);
  const [problem, setProblem] = useState(null);
  const [busy, setBusy] = useState(false);
  const latest = useRef(0);
  const body = JSON.stringify([setup, where]);
  useEffect(() => {
    const ask = ++latest.current;
    const timer = setTimeout(async () => {
      setBusy(true);
      try {
        const result = await api.post('/counters/preview', { ...setup, ...where });
        if (ask === latest.current) { setShown(result); setProblem(null); }
      } catch (error) {
        if (ask === latest.current) { setShown(null); setProblem((error.fields && error.fields.setup) || error.message); }
      } finally {
        if (ask === latest.current) setBusy(false);
      }
    }, 700);
    return () => clearTimeout(timer);
  }, [body]);
  return html`<div class="field-edit" aria-live="polite">
    <div class="spread"><b>Preview</b>${busy && html`<span class="small dim">Reading...</span>`}</div>
    ${problem && html`<div class="error">${problem}</div>`}
    ${shown && html`<div>
      <div style=${{ fontSize: '16px', margin: '6px 0' }}><${Icon} name="lock" size=${14} /> ${shown.name}</div>
      ${shown.readings.map((reading, index) => reading.note && html`<div class="small muted" key=${index}>Counter ${index + 1}: ${reading.note}</div>`)}
    </div>`}
  </div>`;
}

function CounterModal({ start, number, frequencies, choices, onClose, onSaved }) {
  const guild = useGuild();
  const [form, setForm] = useState(start);
  const [placing, setPlacing] = useState('new');
  const [parent, setParent] = useState(null);
  const [adopt, setAdopt] = useState(null);
  const [switchOn, setSwitchOn] = useState(true);
  const [problem, setProblem] = useState(null);
  const [busy, setBusy] = useState(false);
  const [first] = useState(() => JSON.stringify(start));
  const set = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const setCounter = (index, next) => set('counters', form.counters.map((c, i) => (i === index ? next : c)));
  const addCounter = (type) => {
    const counters = [...form.counters, blankCounter(type)];
    setForm({ ...form, counters, text: `${form.text}${form.text.endsWith(' ') || !form.text ? '' : ' '}{${counters.length}}` });
  };
  // Removing a counter renumbers the ones after it, and the text follows.
  const removeCounter = (index) => {
    const text = form.text.replace(/\{(\d{1,2})\}/g, (all, n) => {
      const at = Number(n) - 1;
      if (at === index) return '';
      return at > index ? `{${at}}` : all;
    }).replace(/\s{2,}/g, ' ').trim();
    setForm((current) => ({ ...current, text, counters: current.counters.filter((_, i) => i !== index) }));
  };
  const save = async () => {
    setBusy(true);
    setProblem(null);
    try {
      let saved;
      if (number) {
        saved = await api.put(`/counters/${number}`, form);
      } else {
        const where = switchOn ? (placing === 'adopt' ? { channel_id: adopt } : { parent_id: parent }) : {};
        saved = await api.post('/counters', { ...form, enabled: switchOn, ...where });
      }
      onSaved(saved);
    } catch (error) {
      setProblem((error.fields && (error.fields.setup || error.fields.channel_id || error.fields.parent_id)) || error.message);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Modal} large icon="hash" title=${number ? `Edit counter ${number}` : 'New counter channel'} onClose=${onClose} dirty=${JSON.stringify(form) !== first} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn primary" disabled=${busy || (!number && switchOn && placing === 'adopt' && !adopt)} onClick=${save}>${busy ? 'Saving...' : 'Save'}</button></div>`}>
    ${problem && html`<${Notice} kind="bad">${problem}<//>`}
    <${TextInput} label="Name for the list (optional)" maxLength=${100} value=${form.name} onChange=${(v) => set('name', v)} />
    <${TextInput} label="Channel text" maxLength=${200} value=${form.text} onChange=${(v) => set('text', v)}
      help=${'Write the name with {1}, {2} and so on where each counter goes. Emoji work too. Discord cuts a name at 100 characters.'} />
    <${LivePreview} setup=${form} where=${number ? { number } : (placing === 'adopt' ? { channel_id: adopt } : { parent_id: parent })} />
    <h3>Counters</h3>
    ${form.counters.map((counter, index) => html`<div class="field-edit" key=${index}>
      <div class="spread"><b>{${index + 1}} ${TYPE_LABEL[counter.type]}</b>
        <button class="icon-btn danger" aria-label=${'Remove counter ' + (index + 1)} disabled=${form.counters.length === 1} onClick=${() => removeCounter(index)}><${Icon} name="trash-2" /></button></div>
      <${Picker} label="Type" options=${TYPE_OPTIONS} value=${counter.type} clearable=${false} onChange=${(type) => setCounter(index, blankCounter(type))} />
      <${CounterOptions} counter=${counter} choices=${choices} onChange=${(next) => setCounter(index, next)} />
    </div>`)}
    <div style=${{ maxWidth: '360px' }}><${Picker} label="Add a counter" options=${TYPE_OPTIONS} value=${null} placeholder="Pick a type to add" onChange=${(type) => type && addCounter(type)} /></div>
    <${Select} label="Updates every" value=${form.frequency} options=${frequencies} onChange=${(v) => set('frequency', v)} />
    <p class="small dim">Updates line up with the clock: every 10 minutes means :00, :10, :20 and so on, counted in the zone of a date counter, or the server's zone. Discord allows two renames of a channel in ten minutes.</p>
    ${!number && html`<div class="field-edit">
      <${Switch} label="Switch it on now" value=${switchOn} onChange=${setSwitchOn} />
      ${switchOn && html`<div>
        <${Select} label="Its channel" value=${placing} options=${[{ value: 'new', label: 'Make a new voice channel' }, { value: 'adopt', label: 'Take over a voice channel, keeping its place and permissions' }]} onChange=${setPlacing} />
        ${placing === 'new' ? html`<${ChannelPicker} label="In the category" types=${[]} categories value=${parent} onChange=${setParent} />`
          : html`<${ChannelPicker} label="Voice channel" types=${[2, 13]} value=${adopt} onChange=${setAdopt} />`}
      </div>`}
    </div>`}
  <//>`;
}

function ImportModal({ onClose, onDone }) {
  const [text, setText] = useState('');
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState(null);
  const guild = useGuild();
  const channelLabel = (id) => { const found = guild.channels.find((c) => sameId(c.id, id)); return found ? found.name : 'a channel that is gone'; };
  const run = async (apply) => {
    setBusy(true);
    setProblem(null);
    try {
      const result = await api.post('/counters/import', { data: text, apply });
      if (apply) {
        const made = result.setups.filter((s) => s.number).length;
        toast(`Imported ${made} counter channel${made === 1 ? '' : 's'}.`);
        onDone();
      } else {
        setReport(result);
      }
    } catch (error) {
      setProblem((error.fields && error.fields.data) || error.message);
    } finally {
      setBusy(false);
    }
  };
  const readFile = (file) => { if (file) file.text().then((value) => { setText(value); setReport(null); }); };
  const words = { adopt: 'Takes over its channel in place', off: 'Comes in switched off', already: 'Already a counter here', skip: 'Left out' };
  const ready = report && report.setups.some((s) => s.action === 'adopt' || s.action === 'off');
  return html`<${Modal} large icon="upload" title="Import counter channels" onClose=${onClose} dirty=${!!text} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn" disabled=${busy || !text.trim()} onClick=${() => run(false)}>Check</button>
      <button class="btn primary" disabled=${busy || !ready} onClick=${() => run(true)}>Import</button></div>`}>
    <p class="muted">Paste or load a file from Export on this page. Each counter that was on takes over its channel where it is, when that channel is still here, so nothing moves in the channel list. Each one goes through the same checks as the counter form.</p>
    <input type="file" accept=".json,application/json" aria-label="Load a file" onChange=${(e) => readFile(e.currentTarget.files[0])} />
    <${TextArea} label="Export" rows=${6} value=${text} counter=${false} onChange=${(v) => { setText(v); setReport(null); }} />
    ${problem && html`<${Notice} kind="bad">${problem}<//>`}
    ${report && report.setups.map((entry, index) => html`<div class="field-edit" key=${index}>
      <div class="spread"><b>${entry.name || entry.text}</b><span class=${'badge ' + (entry.action === 'adopt' ? 'green' : entry.action === 'off' ? 'blue' : 'outline')}>${words[entry.action]}</span></div>
      <div class="small muted"><code>${entry.text}</code>${entry.channel_id ? html` in #${channelLabel(entry.channel_id)}` : ''}</div>
      ${entry.problems.map((line) => html`<div class="small error" key=${line}>${line}</div>`)}
    </div>`)}
  <//>`;
}

function CounterRow({ counter, onEdit, onChanged }) {
  const guild = useGuild();
  const [busy, setBusy] = useState(false);
  const channel = counter.channel_id ? guild.channels.find((c) => sameId(c.id, counter.channel_id)) : null;
  const act = async (work, done) => {
    setBusy(true);
    try {
      const result = await work();
      if (result && result.problem) toast(result.problem, 'bad', 9000);
      else if (done) toast(done);
      onChanged();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const switchTo = async (on) => {
    if (on) { act(() => api.post(`/counters/${counter.number}/on`, { parent_id: counter.parent_id }), 'Switched on.'); return; }
    const sure = await confirmDialog({ title: `Switch counter ${counter.number} off?`, body: 'Its channel is deleted. The setup stays, and switching it on makes a new channel.', confirm: 'Switch off', danger: true });
    if (sure) act(() => api.post(`/counters/${counter.number}/off`, {}), 'Switched off.');
  };
  const update = () => act(async () => {
    const { result } = await api.post(`/counters/${counter.number}/update`);
    if (result.waiting_until) toast(`Discord allows two renames in ten minutes. The new name shows at ${new Date(result.waiting_until * 1000).toLocaleTimeString()}.`, 'ok', 8000);
    else if (!result.problem) toast(`It reads ${result.name}.`);
    return result;
  });
  const remove = async () => {
    const sure = await confirmDialog({ title: `Delete counter ${counter.number}?`, body: counter.enabled ? 'Its setup and its channel are both deleted.' : 'Its setup is deleted.', confirm: 'Delete', danger: true });
    if (sure) act(() => api.del(`/counters/${counter.number}`), 'Deleted.');
  };
  return html`<div class="field-edit">
    <div class="spread"><b>${counter.number}. ${counter.name || counter.text}</b>
      <div class="row" style=${{ flexWrap: 'nowrap' }}>
        <button class="btn small" disabled=${busy || !counter.enabled} onClick=${update}><${Icon} name="refresh-cw" size=${14} /> Update now</button>
        <button class="btn small primary" disabled=${busy} onClick=${onEdit}>Edit</button>
        <button class="icon-btn" aria-label="Copy" disabled=${busy} onClick=${() => act(() => api.post(`/counters/${counter.number}/copy`), 'Copied, switched off.')}><${Icon} name="copy" /></button>
        <button class="icon-btn danger" aria-label="Delete" disabled=${busy} onClick=${remove}><${Icon} name="trash-2" /></button>
      </div></div>
    <${Switch} label=${counter.enabled ? (channel ? `On, in ${channel.name}` : 'On') : 'Off'} value=${counter.enabled} disabled=${busy} onChange=${switchTo} />
    <div><${Icon} name="lock" size=${13} /> ${counter.shown || counter.text}</div>
    <div class="small muted">${counter.described.join('; ')}. Every ${(FREQUENCY_LABELS[counter.frequency] || counter.frequency + ' minutes')}${counter.next_update ? `, next at ${new Date(counter.next_update * 1000).toLocaleTimeString()}` : ''}.</div>
    ${counter.rename_at && html`<div class="small muted">Discord's rename limit holds the next rename until ${new Date(counter.rename_at * 1000).toLocaleTimeString()}.</div>`}
    ${(counter.readings || []).map((reading, index) => reading && reading.note && html`<div class="small" key=${index}>Counter ${index + 1}: ${reading.note}</div>`)}
    ${counter.problem && html`<${Notice} kind="warn">${counter.problem}<//>`}
  </div>`;
}

let FREQUENCY_LABELS = {};

export default function CountersPage() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [editing, setEditing] = useState(null);
  const [importing, setImporting] = useState(false);
  const [busy, setBusy] = useState(false);
  const choices = useChoices();
  const load = () => api.get('/counters').then((found) => {
    FREQUENCY_LABELS = Object.fromEntries(found.frequencies.map((f) => [f.value, f.label]));
    setData(found);
  }).catch((error) => { toastError(error); setData({ counters: [], frequencies: [], date_presets: [], most: 100, on: true, presences: true }); });
  useEffect(() => { load(); }, [guild.id]);
  const download = async () => {
    try {
      const setups = await api.get('/counters/export');
      const link = document.createElement('a');
      link.href = URL.createObjectURL(new Blob([JSON.stringify(setups, null, 2)], { type: 'application/json' }));
      link.download = 'counter-channels.json';
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    } catch (error) {
      toastError(error);
    }
  };
  const quick = async () => {
    const sure = await confirmDialog({ title: 'Make the usual counters?', body: 'This makes a category at the top of the channel list with three counters: members, members online and the date.', confirm: 'Make them' });
    if (!sure) return;
    setBusy(true);
    try {
      const result = await api.post('/counters/quick');
      toast(result.problem || `Made counters ${result.made.join(', ')}.`, result.problem ? 'bad' : 'ok');
      load();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  if (data === null) return html`<${Loading} />`;
  const frequencies = data.frequencies;
  return html`<div>
    ${!data.on && html`<${Notice} kind="warn">Counter channels update while Statistics is switched on. Switch it on in Statistics settings.<//>`}
    ${!data.presences && html`<${Notice}>${data.status_split ? SPLIT_WORDS : NO_SPLIT_WORDS}<//>`}
    <${Card} icon="hash" title="Counter channels" right=${html`<span class="small dim">${data.counters.length} of ${data.most}</span>`}>
      <div class="row">
        <button class="btn success" disabled=${data.counters.length >= data.most} onClick=${() => setEditing({ start: blankSetup(), number: null })}><${Icon} name="plus" /> New counter channel</button>
        <button class="btn" disabled=${busy || data.counters.length + 3 > data.most} onClick=${quick}><${Icon} name="wand-sparkles" /> Quick setup</button>
        <button class="btn" onClick=${() => setImporting(true)}><${Icon} name="upload" /> Import</button>
        <button class="btn" disabled=${data.counters.length === 0} onClick=${download}><${Icon} name="download" /> Export</button>
      </div>
      <p class="muted" style=${{ marginTop: '12px', marginBottom: 0 }}>A counter channel is a voice channel named with live counts, such as members online. Members see it locked, and Administrators can still join. Switching one off deletes its channel and keeps its setup. Deleting the channel by hand switches it off.</p>
      <hr class="rule" />
      ${data.counters.length === 0 && html`<p class="muted">No counter channels yet.</p>`}
      ${data.counters.map((counter) => html`<${CounterRow} key=${counter.number} counter=${counter} onChanged=${load}
        onEdit=${() => setEditing({ start: { name: counter.name, text: counter.text, counters: counter.counters, frequency: counter.frequency }, number: counter.number })} />`)}
    <//>
    ${editing && html`<${CounterModal} start=${editing.start} number=${editing.number} frequencies=${frequencies} choices=${{ ...choices, date_presets: data.date_presets, presences: data.presences, status_split: data.status_split }}
      onClose=${() => setEditing(null)} onSaved=${(saved) => { setEditing(null); toast(saved.problem || 'Saved.', saved.problem ? 'bad' : 'ok'); load(); }} />`}
    ${importing && html`<${ImportModal} onClose=${() => setImporting(false)} onDone=${() => { setImporting(false); load(); }} />`}
  </div>`;
}
