// The statistics pages: one page with a tab for each view, the query bar above them, and the address holding all of it, so a link
// shows the same numbers to whoever opens it and a reload comes back to the same place.
import { html, useEffect, useState } from '../lib.js';
import { Boundary, Icon, Loading, Notice } from '../ui.js';
import { Activity, Toggle } from './activity.js';
import { Analytics, Drilldown } from './analytics.js';
import { Invites } from './invites.js';
import { Overview } from './overview.js';
import { QueryBar } from './querybar.js';
import { Apps, Emoji, Status } from './server.js';
import { Downloads } from '../charts.js';
import { addressOf, changeView, fieldValue, isPublic, periodText, readAddress, statsGet } from './common.js';

const TABS = [
  ['overview', 'Overview', 'gauge'], ['messages', 'Messages', 'message-square'], ['voice', 'Voice', 'mic'], ['status', 'Status', 'activity'],
  ['activity', 'Activity', 'gamepad-2'], ['invites', 'Invites', 'user-plus'], ['emoji', 'Emoji', 'smile'], ['analytics', 'Analytics', 'chart-column'],
  ['members', 'Member drilldown', 'users'], ['channels', 'Channel drilldown', 'hash'],
];
const MEMBERS_ONLY = new Set(['invites', 'analytics', 'members', 'channels']);
const DRILL = new Set(['analytics', 'members', 'channels']);

export function StatsApp({ preloaded = null, settingsLink = null, counterLink = null, statroleLink = null }) {
  const [ctx, setCtx] = useState(preloaded);
  const [failed, setFailed] = useState(null);
  const [address, setAddress] = useState(readAddress);
  useEffect(() => {
    if (!preloaded) statsGet('/statistics/context').then(setCtx).catch(setFailed);
    const back = () => setAddress(readAddress());
    window.addEventListener('popstate', back);
    return () => window.removeEventListener('popstate', back);
  }, []);
  if (failed) return html`<${Notice} kind="bad">${failed.message}<//>`;
  if (!ctx) return html`<${Loading} />`;
  const { view, page } = address;
  const go = (nextView, nextPage, push = false) => {
    const target = addressOf(nextView, nextPage);
    if (push) history.pushState(null, '', target);
    else history.replaceState(null, '', target);
    setAddress({ view: nextView, page: nextPage });
  };
  const setView = (next) => go(next, page);
  const downloads = { csv: !!ctx.can_csv };
  const setPage = (changes) => go(view, { ...page, ...changes });
  const tabs = TABS.filter(([key]) => !(isPublic() && MEMBERS_ONLY.has(key)));
  const tab = tabs.some(([key]) => key === page.tab) ? page.tab : 'overview';
  const unit = fieldValue(view, ctx.defaults, 'unit') || 'hours';
  const smooth = page.smooth === '1' ? true : page.smooth === '0' ? false : !!ctx.smooth;
  const stat = fieldValue(view, ctx.defaults, 'stat') || 'both';
  const openDrill = (kind, ids) => go(view, { ...page, tab: kind, ids: ids.join(',') }, true);
  const drillKind = tab === 'members' || tab === 'channels' ? tab : 'filters';
  // The preset's number goes along, so someone who may load presets but not change the query bar still gets its view from the server.
  const loadPreset = (preset) => go({ ...preset.query, ...(preset.kind === 'filters' || !preset.kind ? { preset: String(preset.id) } : {}) },
    { ...page, ids: preset.picks && preset.picks.length ? preset.picks.join(',') : page.ids }, true);
  const body = () => {
    if (DRILL.has(tab) && !ctx.can_drill) return html`<${Notice}>${ctx.needs_2fa
      ? 'This server requires two-factor authentication for moderation, and Analytics and drilldowns follow it. Turn on two-factor authentication for your Discord account, then sign out and sign in with Discord again.'
      : 'Analytics, drilldowns and CSV downloads take a role the statistics settings name, with Administrator, Manage Server or View Server Insights, as Discord asks of bots that show statistics.'}<//>`;
    switch (tab) {
      case 'messages': case 'voice': return html`<${Activity} key=${tab} ctx=${ctx} view=${view} kind=${tab} page=${page} setPage=${setPage} unit=${unit} smooth=${smooth} />`;
      case 'status': return html`<${Status} ctx=${ctx} view=${view} smooth=${smooth} />`;
      case 'activity': return html`<${Apps} ctx=${ctx} view=${view} unit=${unit} smooth=${smooth} />`;
      case 'invites': return html`<${Invites} ctx=${ctx} view=${view} />`;
      case 'emoji': return html`<${Emoji} ctx=${ctx} view=${view} smooth=${smooth} />`;
      case 'analytics': return html`<${Analytics} ctx=${ctx} view=${view} stat=${stat} setStat=${(value) => setView(changeView(view, ctx.defaults, { stat: value }))} unit=${unit} smooth=${smooth} openDrill=${openDrill} />`;
      case 'members': case 'channels': return html`<${Drilldown} key=${tab} kind=${tab} ctx=${ctx} view=${view} ids=${page.ids || ''} setIds=${(ids) => setPage({ ids })} unit=${unit} smooth=${smooth} />`;
      default: return html`<${Overview} ctx=${ctx} view=${view} unit=${unit} smooth=${smooth} />`;
    }
  };
  return html`<div class="stats-page viz">
    <div class="stats-head">
      <div>
        <h2>Statistics</h2>
        <p class="muted small">${ctx.guild && ctx.guild.name ? `${ctx.guild.name}. ` : ''}${periodText(view.period || '', ctx.lookback)}, times in ${ctx.zone_label}.</p>
      </div>
      <div class="row">
        <${Toggle} label=${html`<${Icon} name="trending-up" size=${14} /> Smooth lines`} on=${smooth} onChange=${(on) => setPage({ smooth: on ? '1' : '0' })} title="Draw lines as curves through each point" />
        ${ctx.public_url && !isPublic() && html`<a class="btn small ghost" href=${ctx.public_url} target="_blank" rel="noopener noreferrer"><${Icon} name="external-link" size=${14} /> Public page</a>`}
        ${counterLink && ctx.can_counters && html`<a class="btn small" href=${counterLink} data-nav="1"><${Icon} name="hash" size=${14} /> Counter channels</a>`}
        ${statroleLink && ctx.can_statroles && html`<a class="btn small" href=${statroleLink} data-nav="1"><${Icon} name="crown" size=${14} /> Stat roles</a>`}
        ${settingsLink && ctx.can_settings && html`<a class="btn small" href=${settingsLink} data-nav="1"><${Icon} name="settings" size=${14} /> Settings</a>`}
      </div>
    </div>
    ${!ctx.enabled && html`<${Notice} kind="warn">Statistics are off in this server, so nothing new is counted. ${ctx.can_settings ? 'Turn them on in Statistics settings.' : 'Someone with Manage Server can turn them on.'}<//>`}
    <${QueryBar} ctx=${ctx} view=${view} setView=${setView} presetKind=${drillKind} picks=${drillKind === 'filters' ? null : (page.ids || '').split(',').filter(Boolean)} onPreset=${loadPreset} />
    <div class="tabs stats-tabs" role="tablist">${tabs.map(([key, label, icon]) => html`<button key=${key} type="button" role="tab" aria-selected=${key === tab}
      class=${'tab' + (key === tab ? ' on' : '') + (DRILL.has(key) && !ctx.can_drill ? ' locked' : '')} onClick=${() => go(view, { ...page, tab: key }, true)}>
      <${Icon} name=${DRILL.has(key) && !ctx.can_drill ? 'lock' : icon} size=${14} /> ${label}</button>`)}</div>
    <${Downloads.Provider} value=${downloads}><${Boundary} key=${tab} what="This view">${body()}<//><//>
  </div>`;
}
