// Statistics insights: moderation over time, help forums, and whether newcomers stay. Staff can open it, since it reads moderation records.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, Loading, Notice, Select, Tabs, toastError, useGuild, when } from '../ui.js';

const TABS = [
  { key: 'moderation', label: 'Moderation' },
  { key: 'forums', label: 'Help forums' },
  { key: 'newcomers', label: 'Newcomers' },
];
const PERIODS = [{ value: 7, label: 'Last 7 days' }, { value: 30, label: 'Last 30 days' }, { value: 90, label: 'Last 90 days' }, { value: 365, label: 'Last year' }];
const WEEKS = [{ value: 4, label: 'Last 4 weeks' }, { value: 12, label: 'Last 12 weeks' }, { value: 26, label: 'Last 26 weeks' }, { value: 52, label: 'Last 52 weeks' }];
// Every action a case records, as the Cases page names them.
const ACTIONS = { warn: 'Warnings', mute: 'Mutes', hardmute: 'Hard mutes', minimute: 'Mini mutes', timeout: 'Timeouts', kick: 'Kicks', ban: 'Bans', tempban: 'Timed bans',
  softban: 'Soft bans', unban: 'Unbans', unmute: 'Unmutes', unminimute: 'Mini mutes lifted', untimeout: 'Timeouts lifted' };

const count = (value) => Number(value || 0).toLocaleString();
const share = (value) => (value === null || value === undefined ? 'Too soon' : `${Math.round(value * 100)}%`);
function span(seconds) {
  if (seconds === null || seconds === undefined) return 'None yet';
  if (seconds < 90) return `${Math.round(seconds)} seconds`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} hours`;
  return `${(seconds / 86400).toFixed(1)} days`;
}
const out = (part, whole) => (whole ? ` (${Math.round((part / whole) * 100)}%)` : '');

// A series as a table, a row per bucket. `series` is [{ label, values }], each values array as long as `buckets`.
function Trend({ buckets, series, empty = 'Nothing in this period.' }) {
  const rows = buckets.map((bucket, index) => ({ bucket, values: series.map((item) => item.values[index]) })).filter((row) => row.values.some((value) => value));
  if (!rows.length) return html`<p class="muted">${empty}</p>`;
  return html`<div class="table-wrap"><table>
    <thead><tr><th>Period</th>${series.map((item) => html`<th key=${item.label}>${item.label}</th>`)}</tr></thead>
    <tbody>${rows.map((row) => html`<tr key=${row.bucket.at}><td class="nowrap">${row.bucket.label}</td>${row.values.map((value, index) => html`<td key=${index}>${series[index].format ? series[index].format(value) : count(value)}</td>`)}</tr>`)}</tbody>
  </table></div>`;
}

function Moderation({ days }) {
  const guild = useGuild();
  const [page, setPage] = useState(null);
  useEffect(() => { setPage(null); api.get(`/statistics/insights/moderation?days=${days}`).then(setPage).catch(toastError); }, [guild.id, days]);
  if (!page) return html`<${Loading} />`;
  const cases = page.cases;
  const automod = page.automod;
  const reports = page.reports;
  return html`
    ${cases ? html`<${Card} icon="gavel" title="Cases">
      <div class="stat-tiles">
        <div class="stat-tile"><b>${count(cases.total)}</b><span class="muted small">Cases</span></div>
        ${cases.by_action.slice(0, 4).map((row) => html`<div class="stat-tile" key=${row.action}><b>${count(row.total)}</b><span class="muted small">${ACTIONS[row.action] || row.action}</span></div>`)}
        <div class="stat-tile"><b>${count(cases.voided)}</b><span class="muted small">Voided, left out</span></div>
      </div>
      <${Trend} buckets=${page.buckets} series=${cases.by_action.map((row) => ({ label: ACTIONS[row.action] || row.action, values: row.series }))} />
      <h3>By moderator</h3>
      <${Trend} buckets=${page.buckets} series=${cases.moderators.slice(0, 6).map((row) => ({ label: row.name, values: row.series }))} />
      ${cases.moderators.length === 0 ? html`<p class="muted">No cases in this period.</p>` : html`<div class="table-wrap"><table>
        <thead><tr><th>Moderator</th><th>Cases</th><th>What they did</th></tr></thead>
        <tbody>${cases.moderators.map((row) => html`<tr key=${String(row.id || row.name)}><td title=${row.id ? String(row.id) : ''}>${row.name}</td><td>${count(row.total)}</td>
          <td class="muted small">${Object.entries(row.actions).sort((a, b) => b[1] - a[1]).map(([action, amount]) => `${ACTIONS[action] || action} ${count(amount)}`).join(', ')}</td></tr>`)}</tbody>
      </table></div>`}
    <//>` : html`<${Notice}>Moderation isn't part of this bot, so there are no cases to count.<//>`}
    ${automod && html`<${Card} icon="shield" title="Automod">
      ${automod.hits_kept ? html`<p class="muted">Every time a rule fired, from the log${page.log_days ? `, which keeps ${page.log_days} days` : ''}. Dry runs are counted apart.</p>` : ''}
      ${automod.hits.length === 0 && automod.punishments.length === 0 ? html`<p class="muted">No automod actions in this period.</p>` : html`<div class="table-wrap"><table>
        <thead><tr><th>Rule</th><th>Times it fired</th><th>Dry runs</th><th>Cases it made</th></tr></thead>
        <tbody>${[...new Set([...automod.hits.map((row) => row.rule), ...automod.punishments.map((row) => row.rule)])].map((rule) => {
          const hit = automod.hits.find((row) => row.rule === rule);
          const punished = automod.punishments.find((row) => row.rule === rule);
          return html`<tr key=${rule}><td>${rule}</td><td>${hit ? count(hit.count) : '0'}</td><td>${hit ? count(hit.dry_runs) : '0'}</td><td>${punished ? count(punished.count) : '0'}</td></tr>`;
        })}</tbody>
      </table></div>`}
      ${automod.dry_runs_only && automod.dry_runs_only.length > 0 && html`<p class="muted small">Only in dry runs: ${automod.dry_runs_only.map((row) => `${row.rule} ${count(row.count)}`).join(', ')}.</p>`}
    <//>`}
    ${reports ? html`<${Card} icon="message-square-warning" title="Reports">
      <div class="stat-tiles">
        <div class="stat-tile"><b>${count(reports.opened_total)}</b><span class="muted small">Opened</span></div>
        <div class="stat-tile"><b>${count(reports.handled_total)}</b><span class="muted small">Handled</span></div>
        <div class="stat-tile"><b>${count(reports.open_now)}</b><span class="muted small">Open now</span></div>
        <div class="stat-tile"><b>${span(reports.median_seconds)}</b><span class="muted small">Typical time to handle</span></div>
      </div>
      <p class="muted small">Typical is the median: half were handled sooner. The average is ${span(reports.average_seconds)}. ${count(reports.outcomes.actioned || 0)} were acted on and ${count(reports.outcomes.false_positive || 0)} marked as not a problem.</p>
      <${Trend} buckets=${page.buckets} series=${[{ label: 'Opened', values: reports.opened }, { label: 'Handled', values: reports.handled }]} />
      ${reports.handlers.length > 0 && html`<div class="table-wrap"><table>
        <thead><tr><th>Handled by</th><th>Reports</th><th>Typical time</th></tr></thead>
        <tbody>${reports.handlers.map((row) => html`<tr key=${String(row.id || row.name)}><td title=${row.id ? String(row.id) : ''}>${row.name}</td><td>${count(row.count)}</td><td>${span(row.median_seconds)}</td></tr>`)}</tbody>
      </table></div>`}
    <//>` : html`<${Notice}>Reports aren't part of this bot, so there are none to count.<//>`}`;
}

function Forums({ days }) {
  const guild = useGuild();
  const [page, setPage] = useState(null);
  useEffect(() => { setPage(null); api.get(`/statistics/insights/forums?days=${days}`).then(setPage).catch(toastError); }, [guild.id, days]);
  if (!page) return html`<${Loading} />`;
  return html`<${Card} icon="message-circle" title="Help forums">
    <p class="muted">For posts made in the period: how many got an answer from someone other than the poster, bots aside, and how soon. Solved counts posts with the forum's solved tag from the Forum messages page.${page.tracked_since ? ` Posts are counted since ${when(page.tracked_since)}. A recount with threads fills in older ones.` : ' No posts are counted yet. They count from now, and a recount with threads fills in older ones.'}</p>
    ${page.forums.length === 0 ? html`<p class="muted">No forum posts in this period.</p>` : html`<div><div class="table-wrap"><table>
      <thead><tr><th>Forum</th><th>Posts</th><th>Answered</th><th>Typical first answer</th><th>Within an hour</th><th>Within a day</th><th>Solved</th></tr></thead>
      <tbody>${page.forums.map((row) => html`<tr key=${String(row.forum_id)}><td title=${String(row.forum_id)}>#${row.name}</td><td>${count(row.posts)}</td>
        <td>${count(row.answered)}${out(row.answered, row.posts)}</td><td>${span(row.median_seconds)}</td><td>${share(row.within['3600'])}</td><td>${share(row.within['86400'])}</td>
        <td>${row.solved === null ? 'No solved tag' : `${count(row.solved)}${out(row.solved, row.posts)}`}</td></tr>`)}</tbody>
    </table></div>
    <${Trend} buckets=${page.buckets} series=${[{ label: 'Posts', values: page.posted }, { label: 'Answered', values: page.answered }, { label: 'Typical first answer', values: page.median_seconds, format: span }]} /></div>`}
  <//>`;
}

function Newcomers() {
  const guild = useGuild();
  const [weeks, setWeeks] = useState(12);
  const [page, setPage] = useState(null);
  useEffect(() => { setPage(null); api.get(`/statistics/insights/newcomers?weeks=${weeks}`).then(setPage).catch(toastError); }, [guild.id, weeks]);
  return html`<${Card} icon="user-plus" title="Newcomers" right=${html`<${Select} ariaLabel="Weeks" options=${WEEKS} value=${weeks} onChange=${setWeeks} />`}>
    <p class="muted">Of the people who joined each week: how many are still here, how many sent a message after joining, and how many were still sending them a day, a week and a month later. A week too recent to tell says so. Someone who chose privacy counts as a join and nothing more.${page && page.tracked_since ? ` Joins are counted since ${when(page.tracked_since)}.` : ''}</p>
    ${!page ? html`<${Loading} />` : html`<div class="table-wrap"><table>
      <thead><tr><th>Week</th><th>Joined</th><th>Still here</th><th>Spoke</th><th>A day on</th><th>A week on</th><th>A month on</th></tr></thead>
      <tbody>${[...page.weeks].reverse().map((row) => html`<tr key=${row.start}><td class="nowrap">${row.label}${row.complete ? '' : ' (so far)'}</td><td>${count(row.joined)}</td>
        <td>${count(row.still_here)}${out(row.still_here, row.joined)}</td><td>${count(row.spoke)}${out(row.spoke, row.joined)}</td>
        ${page.marks.map((days) => { const value = row.spoke_after[String(days)]; return html`<td key=${days}>${value === null ? 'Too soon' : `${count(value)}${out(value, row.joined)}`}</td>`; })}</tr>`)}</tbody>
    </table></div>`}
  <//>`;
}

export default function StatsInsightsPage() {
  const [tab, setTab] = useState(() => new URLSearchParams(location.search).get('tab') || 'moderation');
  const [days, setDays] = useState(30);
  const pick = (key) => {
    setTab(key);
    const params = new URLSearchParams(location.search);
    params.set('tab', key);
    history.replaceState(null, '', `${location.pathname}?${params}`);
  };
  const shown = TABS.some((item) => item.key === tab) ? tab : 'moderation';
  return html`<div>
    <h1 class="page-title">Statistics insights</h1>
    <div class="spread"><${Tabs} tabs=${TABS} active=${shown} onChange=${pick} />
      ${shown !== 'newcomers' && html`<${Select} ariaLabel="Period" options=${PERIODS} value=${days} onChange=${setDays} />`}</div>
    ${shown === 'forums' ? html`<${Forums} days=${days} />` : shown === 'newcomers' ? html`<${Newcomers} />` : html`<${Moderation} days=${days} />`}
  </div>`;
}
