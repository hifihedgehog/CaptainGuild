// What every statistics page shares: where it reads from, the view the address holds, and how numbers, times and names read.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api, raw } from '../api.js';

// The dashboard reads through /api/g/{server}. The public page, signed in or not, reads through /api/public/g/{server}.
let source = { public: false, guild: null };
export function readFrom(next) { source = next; }
export const isPublic = () => source.public;
export function statsGet(path, params) {
  const query = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : '';
  return source.public ? raw.get(`/api/public/g/${source.guild}${path}${query}`) : api.get(path + query);
}

// A view's fields, as the server reads them from an address (core/stats/web.py FIELDS), and the page's own settings beside them. `preset`
// names a loaded preset, whose view the server reads for someone who may load presets but not change the query bar.
export const FIELDS = ['period', 'interval', 'unit', 'include', 'stat', 'voice_states', 'channel_mode', 'channels', 'voice_mode', 'voice_channels',
  'member_mode', 'members', 'roles_allow', 'roles_block', 'activity_mode', 'activities', 'invite_mode', 'invites', 'fake_mode', 'fake_match', 'fake_reasons',
  'emoji_mode', 'emoji', 'emoji_states', 'emoji_type_mode', 'emoji_type_match', 'emoji_types', 'emoji_uses', 'preset'];
export const LISTS = new Set(['voice_states', 'channels', 'voice_channels', 'members', 'roles_allow', 'roles_block', 'activities', 'invites', 'fake_reasons', 'emoji',
  'emoji_states', 'emoji_types', 'emoji_uses']);
export const PAGE_KEYS = ['tab', 'ma', 'acc', 'smooth', 'top', 'ids'];

export function readAddress() {
  const params = new URLSearchParams(location.search);
  const view = {};
  for (const name of FIELDS) if (params.has(name)) view[name] = params.get(name);
  const page = {};
  for (const name of PAGE_KEYS) if (params.has(name)) page[name] = params.get(name);
  return { view, page };
}

export function addressOf(view, page) {
  const params = new URLSearchParams();
  for (const name of PAGE_KEYS) if (page[name] !== undefined && page[name] !== null && page[name] !== '') params.set(name, page[name]);
  for (const name of FIELDS) if (view[name] !== undefined && view[name] !== null) params.set(name, view[name]);
  const text = params.toString();
  return location.pathname + (text ? '?' + text : '');
}

// The value a field has in this view: the address's, or the server's.
export function fieldValue(view, defaults, name) {
  if (view[name] !== undefined) return view[name];
  if (name === 'period') return '';
  if (name === 'stat') return 'both';
  return defaults[name] !== undefined ? defaults[name] : '';
}
export const listOf = (text) => (text ? String(text).split(',').filter(Boolean) : []);
const sameList = (a, b) => listOf(a).slice().sort().join(',') === listOf(b).slice().sort().join(',');

// A change to some fields, kept in the address only where it differs from what the server set, so links stay short.
export function changeView(view, defaults, changes) {
  const next = { ...view };
  delete next.preset;  // a changed view is no longer the preset's
  for (const [name, value] of Object.entries(changes)) {
    const text = Array.isArray(value) ? value.join(',') : String(value ?? '');
    const base = name === 'period' ? '' : name === 'stat' ? 'both' : (defaults[name] ?? '');
    const same = LISTS.has(name) ? sameList(text, base) : text === String(base);
    if (same) delete next[name];
    else next[name] = text;
  }
  return next;
}
// Only the fields a request needs, so two pages with the same view share the server's answer.
export const viewParams = (view) => Object.fromEntries(FIELDS.filter((name) => view[name] !== undefined).map((name) => [name, view[name]]));

// ---- reading data ----
// The latest answer for a path and its query. An earlier answer stays on screen while a new one loads.
export function useStats(path, params, enabled = true) {
  const key = path + '?' + new URLSearchParams(params || {}).toString();
  const [state, setState] = useState({ data: null, error: null, loading: enabled });
  const [round, setRound] = useState(0);
  const asked = useRef(0);
  useEffect(() => {
    if (!enabled) return;
    const number = ++asked.current;
    setState((now) => ({ ...now, loading: true, error: null }));
    statsGet(path, params).then((data) => {
      if (number === asked.current) setState({ data, error: null, loading: false });
    }).catch((error) => {
      if (number === asked.current) setState((now) => ({ ...now, error, loading: false }));
    });
  }, [key, enabled, round]);
  return { ...state, reload: () => setRound((n) => n + 1) };
}

// ---- numbers, time and units ----
const counts = new Intl.NumberFormat('en-US');
export const count = (value) => (value === null || value === undefined ? 'None' : counts.format(Math.round(value)));
export const decimal = (value, places = 1) => (value === null || value === undefined ? 'None' : new Intl.NumberFormat('en-US', { maximumFractionDigits: places }).format(value));
export const UNIT_SECONDS = { minutes: 60, hours: 3600, days: 86400 };
export const UNIT_WORDS = { minutes: ['minute', 'minutes'], hours: ['hour', 'hours'], days: ['day', 'days'] };
export const inUnit = (seconds, unit) => (seconds === null || seconds === undefined ? null : seconds / UNIT_SECONDS[unit || 'hours']);
export function voiceText(seconds, unit) {
  const value = inUnit(seconds, unit);
  if (value === null) return 'None';
  const words = UNIT_WORDS[unit || 'hours'];
  return `${decimal(value, value < 10 ? 2 : 1)} ${Math.abs(value - 1) < 1e-9 ? words[0] : words[1]}`;
}
export const percent = (value) => (value === null || value === undefined ? 'None' : `${decimal(value, 1)}%`);

function zoneOk(zone) {
  try { new Intl.DateTimeFormat('en-US', { timeZone: zone }); return zone; } catch (error) { return 'UTC'; }
}
export function dateText(seconds, zone, withTime = false) {
  if (!seconds) return 'None';
  const options = { timeZone: zoneOk(zone || 'UTC'), year: 'numeric', month: 'short', day: 'numeric' };
  if (withTime) Object.assign(options, { hour: 'numeric', minute: '2-digit' });
  return new Intl.DateTimeFormat('en-US', options).format(new Date(seconds * 1000));
}
export const isoText = (seconds) => (seconds ? new Date(seconds * 1000).toISOString() : '');

// How far back the server reads each unit (core/stats/web.py PERIOD_LIMITS): a hundred years of each.
export const PERIOD_LIMITS = { h: 876000, d: 36500, w: 5214, mo: 1200 };

// Whether the server reads a period as written. It answers one it can't read with the server's own period, so the page says that too.
export function readablePeriod(value) {
  const text = String(value || '').trim().toLowerCase();
  if (!text || text === 'all' || text === 'default') return true;
  if (text.startsWith('since:')) {
    const found = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(text.slice(6).trim());
    if (!found) return false;
    const [year, month, day] = found.slice(1).map(Number);
    const date = new Date(0);
    date.setUTCFullYear(year, month - 1, day);  // Date.UTC reads the years 0 to 99 as 1900 to 1999
    return year >= 1 && date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
  }
  const found = /^(\d{1,6})(h|d|w|mo)$/.exec(text);
  return !!found && Number(found[1]) >= 1 && Number(found[1]) <= PERIOD_LIMITS[found[2]];
}

export function periodText(value, lookback) {
  const words = { h: 'hour', d: 'day', w: 'week', mo: 'month' };
  if (!readablePeriod(value)) value = '';
  if (!value) {
    if (!lookback) return 'Server default';
    if (lookback.mode === 'all') return 'Server default (all time)';
    if (lookback.mode === 'since') return `Server default (since ${lookback.since})`;
    return `Server default (last ${lookback.days} day${lookback.days === 1 ? '' : 's'})`;
  }
  if (value === 'all') return 'All time';
  if (value.startsWith('since:')) {
    const [year, month, day] = value.slice(6).split('-').map(Number);
    return 'Since ' + new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(Date.UTC(year, month - 1, day));
  }
  const found = /^(\d+)(h|d|w|mo)$/.exec(value);
  if (!found) return value;
  const amount = Number(found[1]);
  return `Last ${amount} ${words[found[2]]}${amount === 1 ? '' : 's'}`;
}

// ---- names, each with its Discord ID on hover ----
export function Person({ person, plain = false }) {
  if (!person) return html`<span class="dim">Nobody</span>`;
  if (!person.id) return html`<span class="dim">${person.display_name || 'Someone who chose privacy'}</span>`;
  const title = `${person.name}${person.name !== person.display_name ? ` (${person.display_name})` : ''}\nID ${person.id}`;
  return html`<span class="stats-name" title=${title}>
    ${!plain && person.avatar && html`<img class="avatar stats-avatar" src=${person.avatar} alt="" loading="lazy" />`}
    <span>${person.display_name || person.name}</span>
    ${person.bot && html`<span class="badge outline">bot</span>`}
    ${person.in_server === false && html`<span class="badge outline">left</span>`}
  </span>`;
}

export function Channel({ channel }) {
  if (!channel) return html`<span class="dim">None</span>`;
  if (channel.hidden) return html`<span class="dim" title=${channel.id ? `ID ${channel.id}` : 'A channel you can\'t see'}>Hidden channel</span>`;
  const voice = channel.type === 2 || channel.type === 13;
  const merged = channel.type === 999;
  return html`<span class="stats-name" title=${`${channel.category ? channel.category + ' / ' : ''}${channel.name}\nID ${channel.id}`}>
    <span class="dim">${merged ? '' : voice ? '\u{1F50A}' : '#'}</span><span>${channel.name}</span>
    ${merged && html`<span class="badge outline">merged</span>`}
    ${channel.deleted && html`<span class="badge outline">deleted</span>`}
  </span>`;
}

export function RoleName({ role }) {
  if (!role) return html`<span class="dim">Unknown role</span>`;
  const color = role.color ? '#' + Number(role.color).toString(16).padStart(6, '0') : null;
  return html`<span class="stats-name" title=${`ID ${role.id}`}><span class="dot" style=${{ background: color || '#99aab5' }}></span>${role.name}</span>`;
}

export const FAKE_LABELS = {
  self: 'Used their own invite', rejoin: 'Had been a member before', young_account: 'Account newer than the minimum', new_member: 'Newer here than the minimum',
  no_avatar: 'No profile picture', missing_role: 'Missing a required role', onboarding: 'Onboarding not finished', home_actions: 'Server guide steps not finished',
  pending: 'Membership screening not passed', left: 'Left the server', bot: 'A bot', temporary: 'Temporary membership',
};
export const STATE_LABELS = { talking: 'Talking', afk: 'In the AFK channel', self_muted: 'Muted themselves', self_deafened: 'Deafened themselves', server_muted: 'Muted by staff', server_deafened: 'Deafened by staff' };
export const EMOJI_STATE_LABELS = { used: 'Used', unused: 'Not used in the period', deleted: 'Deleted from the server', unavailable: 'Unavailable' };
export const EMOJI_TYPE_LABELS = { server: "This server's own", animated: 'Animated', managed: 'From an integration', restricted: 'Limited to roles', super: 'Super reaction' };
export const EMOJI_USE_LABELS = { reactions: "Reactions on others' messages", self_reactions: 'Reactions on their own messages', messages: 'In message text', stickers: 'Stickers' };
export const KIND_LABELS = { playing: 'Playing', streaming: 'Streaming', listening: 'Listening', watching: 'Watching', competing: 'Competing' };
