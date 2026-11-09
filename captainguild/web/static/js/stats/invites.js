// The Invites page: joins over time by invite, every invite, the people who invited, and each join with the reasons it doesn't count.
import { html, useMemo } from '../lib.js';
import { Loading, Notice } from '../ui.js';
import { ChartFrame, OTHER, TimeChart, useSlots } from '../charts.js';
import { Channel, FAKE_LABELS, Person, count, dateText, useStats, viewParams } from './common.js';
import { StatTable, unpack } from './table.js';
import { Tile } from './overview.js';

const codeName = (series) => (series.code === 'other' ? 'Other invites' : series.code === '' ? 'No invite found' : series.label ? `${series.label} (${series.code})` : series.code);
const personText = (person) => (person ? person.display_name || person.name : '');

export function Invites({ ctx, view }) {
  const { data, error, loading } = useStats('/statistics/invites', viewParams(view));
  const named = data ? data.series.filter((series) => series.code !== 'other' && series.code !== '').map((series) => series.code) : [];
  const slots = useSlots(named);
  const panels = useMemo(() => {
    if (!data || !data.series.length) return [];
    return [{ key: 'joins', title: 'Joins', format: count, stacked: true, series: data.series.map((series) => ({
      key: series.code || 'none', name: codeName(series), values: series.values, kind: 'bars', slot: series.code === 'other' || series.code === '' ? OTHER : slots(series.code) })) }];
  }, [data, slots]);
  const joins = useMemo(() => (data ? unpack(data.joins) : []), [data]);
  if (error) return html`<${Notice} kind="bad">${error.message}<//>`;
  if (!data) return html`<${Loading} />`;
  const period = data.period;
  const zone = period.zone;
  const who = (id) => (id === null || id === undefined ? null : (data.people || {})[String(id)] || null);
  const labelOf = (code) => (code ? (data.labels || {})[code] : null);
  const inviteColumns = [
    { key: 'code', label: 'Invite', render: (row) => (!row.code ? html`<span class="dim">No invite found</span>` : html`<span class="stats-name" title=${`Code ${row.code}`}><b>${row.label || row.code}</b>${row.label && html`<span class="dim">${row.code}</span>`}
      ${row.vanity && html`<span class="badge outline">vanity</span>`}${row.temporary && html`<span class="badge outline">temporary</span>`}${row.deleted_at && html`<span class="badge outline">deleted</span>`}</span>`),
      text: (row) => (!row.code ? 'No invite found' : row.label ? `${row.label} ${row.code}` : row.code) },
    { key: 'inviter', label: 'Made by', render: (row) => html`<${Person} plain person=${who(row.inviter_id)} />`, text: (row) => personText(who(row.inviter_id)) },
    { key: 'channel', label: 'Channel', render: (row) => html`<${Channel} channel=${row.channel} />`, text: (row) => (row.channel ? row.channel.name : '') },
    { key: 'joins', label: 'Joins', numeric: true },
    { key: 'real', label: 'Counted', numeric: true },
    { key: 'fake', label: 'Fake', numeric: true },
    { key: 'left', label: 'Left since', numeric: true },
    { key: 'uses', label: 'Uses in all', numeric: true },
    { key: 'created_at', label: 'Made', numeric: true, text: (row) => dateText(row.created_at, zone) },
    { key: 'max_uses', label: 'Most uses', hidden: true },
    { key: 'inviter_id', label: 'Maker ID', hidden: true },
  ];
  const inviterColumns = [
    { key: 'inviter', label: 'Member', render: (row) => html`<${Person} plain person=${who(row.user_id)} />`, text: (row) => personText(who(row.user_id)) },
    { key: 'real', label: 'Counted joins', numeric: true },
    { key: 'joins', label: 'All joins', numeric: true },
    { key: 'fake', label: 'Fake', numeric: true },
    { key: 'invites', label: 'Invites used', numeric: true },
    { key: 'user_id', label: 'ID', hidden: true },
  ];
  const joinColumns = [
    { key: 'at', label: 'Joined', numeric: true, text: (row) => dateText(row.at, zone, true), csv: (row) => new Date(row.at * 1000).toISOString() },
    { key: 'member', label: 'Member', render: (row) => html`<${Person} plain person=${who(row.user_id)} />`, text: (row) => personText(who(row.user_id)) },
    { key: 'code', label: 'Invite', text: (row) => (row.code ? (labelOf(row.code) ? `${labelOf(row.code)} (${row.code})` : row.code) : 'No invite found') },
    { key: 'inviter', label: 'Invited by', render: (row) => html`<${Person} plain person=${who(row.inviter_id)} />`, text: (row) => personText(who(row.inviter_id)) },
    { key: 'reasons', label: 'Fake because', text: (row) => row.reasons.map((reason) => FAKE_LABELS[reason] || reason).join(', '),
      render: (row) => (row.reasons.length ? html`<span class="small">${row.reasons.map((reason) => FAKE_LABELS[reason] || reason).join(', ')}</span>` : html`<span class="dim small">Counts</span>`) },
    { key: 'user_id', label: 'Member ID', hidden: true, value: (row) => row.user_id },
  ];
  const table = () => ({ columns: ['Bucket', 'Starts', ...panels[0].series.map((series) => series.name)],
    rows: period.labels.map((label, index) => [label, new Date(period.buckets[index] * 1000).toISOString(), ...panels[0].series.map((series) => series.values[index])]) });
  return html`<div class="stats-stack">
    ${data.readable === false && html`<${Notice} kind="warn">The bot can't read this server's invites, so new joins aren't matched to one. It needs the Manage Server permission.<//>`}
    <div class="stat-tiles">
      <${Tile} label="Joins" value=${data.total} />
      <${Tile} label="Counted" value=${data.total - data.fake} note="No fake reason" />
      <${Tile} label="Fake" value=${data.fake} note="At least one reason" />
    </div>
    <${ChartFrame} title="Joins by invite" subtitle=${`${period.label}, ${period.zone_label}`} table=${panels.length ? table : null} file="joins-by-invite" busy=${loading}>
      ${panels.length ? html`<${TimeChart} labels=${period.labels} panels=${panels} />` : html`<div class="viz-empty">No joins in this period.</div>`}
    <//>
    <${ChartFrame} title="Invites" subtitle="Every invite, with its joins in the period" image=${false} file="invites">
      <${StatTable} label="Invites" columns=${inviteColumns} rows=${data.invites} rowKey=${(row) => row.code} file="invites" initialSort=${{ key: 'real', down: true }} empty="No invites." />
    <//>
    <${ChartFrame} title="Top inviters" subtitle=${data.limited ? `${period.label}. The first ${data.inviters.length.toLocaleString('en-US')}. The whole list takes the role that opens drilldowns.` : period.label}
      image=${false} file="inviters">
      <${StatTable} label="Inviters" columns=${inviterColumns} rows=${data.inviters} rowKey=${(row) => row.user_id} file="inviters" initialSort=${{ key: 'real', down: true }} empty="Nobody invited anyone in this period." />
    <//>
    <${ChartFrame} title="Joins" subtitle=${`${period.label}, newest first${data.truncated ? `. The newest ${joins.length.toLocaleString()} of ${data.total.toLocaleString()} are listed.` : ''}`} image=${false} file="joins">
      <${StatTable} label="Joins" columns=${joinColumns} rows=${joins} rowKey=${(row) => `${row.user_id}-${row.at}`} file="joins" empty="No joins in this period." />
    <//>
  </div>`;
}
