// The Messages and Voice pages: how much, how many people and channels, over time and by hour of the week, and who and where.
import { html, useMemo, useState } from '../lib.js';
import { Icon, Loading, Notice } from '../ui.js';
import { ChartFrame, Donut, Heatmap, TimeChart, average, heatTable, movingAverage, runningTotal, topSlices, useSlots } from '../charts.js';
import { Channel, Person, count, decimal, inUnit, isPublic, useStats, viewParams } from './common.js';
import { StatTable, unpack } from './table.js';
import { Tile } from './overview.js';

const DONUT_SIZES = [3, 5, 8];
const BUCKET_WORDS = { hour: 'hourly', day: 'daily', week: 'weekly', month: 'monthly' };

// The moving averages a page draws, kept in the address as "ma=7,30".
export function MovingAverages({ value, onChange }) {
  const [typed, setTyped] = useState('');
  const sizes = (value || '').split(',').map(Number).filter((n) => n >= 2);
  const add = (event) => {
    event.preventDefault();
    const size = Math.floor(Number(typed));
    if (size >= 2 && size <= 365 && !sizes.includes(size) && sizes.length < 4) onChange([...sizes, size].sort((a, b) => a - b).join(','));
    setTyped('');
  };
  return html`<form class="row stats-ma" onSubmit=${add} aria-label="Moving averages">
    <span class="small muted">Moving average</span>
    ${sizes.map((size) => html`<span class="chip" key=${size}>${size}<button type="button" aria-label=${`Remove the ${size}-point average`} onClick=${() => onChange(sizes.filter((n) => n !== size).join(','))}><${Icon} name="x" size=${12} /></button></span>`)}
    ${sizes.length < 4 && html`<input type="number" min="2" max="365" class="stats-ma-input" aria-label="Points to average over" placeholder="Points" value=${typed} onInput=${(e) => setTyped(e.currentTarget.value)} />`}
  </form>`;
}

export function Toggle({ label, on, onChange, title }) {
  return html`<button type="button" class=${'btn small ghost' + (on ? ' on' : '')} aria-pressed=${!!on} title=${title} onClick=${() => onChange(!on)}>${label}</button>`;
}

export function Activity({ ctx, view, kind, page, setPage, unit, smooth }) {
  const params = { kind, ...viewParams(view) };
  const series = useStats('/statistics/activity', params);
  const heat = useStats('/statistics/heatmap', params);
  const voice = kind === 'voice';
  const value = (number) => (voice ? inUnit(number, unit) : number);
  const label = voice ? `Voice (${unit})` : 'Messages';
  const format = (number) => (voice ? `${decimal(number, number < 10 ? 2 : 1)} ${unit}` : count(number));
  const data = series.data;
  const acc = page.acc === '1';
  const sizes = (page.ma || '').split(',').map(Number).filter((n) => n >= 2);
  const panels = useMemo(() => {
    if (!data) return [];
    const base = data.count.map(value);
    const main = acc ? runningTotal(base) : base;
    const lines = [{ key: 'count', name: acc ? `${label}, running total` : label, values: main, slot: 0, kind: 'area' }];
    sizes.forEach((size, index) => lines.push({ key: `ma${size}`, name: `${size}-point average`, values: movingAverage(acc ? main : base, size), slot: index + 1, kind: 'line' }));
    return [
      { key: 'count', title: label, format, series: lines, refs: acc ? [] : [{ value: average(base), label: `Average ${format(average(base))}` }] },
      { key: 'members', title: 'Different members', format: count, series: [{ key: 'members', name: 'Members', values: data.members, slot: 0, kind: 'line' }] },
      { key: 'channels', title: 'Different channels', format: count, series: [{ key: 'channels', name: 'Channels', values: data.channels, slot: 0, kind: 'line' }] },
    ];
  }, [data, acc, page.ma, unit]);
  if (series.error) return html`<${Notice} kind="bad">${series.error.message}<//>`;
  if (!data) return html`<${Loading} />`;
  const period = data.period;
  const fitted = period.interval === period.asked_interval ? '' : `${BUCKET_WORDS[period.interval]} buckets to fit the period, `;
  const subtitle = `${period.label}, ${fitted}the last ${period.interval} still running, ${period.zone_label}`;
  const table = () => ({ columns: ['Bucket', 'Starts', voice ? `voice_${unit}` : 'messages', 'members', 'channels', ...sizes.map((size) => `average_${size}`)],
    rows: period.labels.map((name, index) => [name, new Date(period.buckets[index] * 1000).toISOString(), value(data.count[index]), data.members[index], data.channels[index],
      ...sizes.map((size, at) => panels[0].series[at + 1].values[index])]) });
  return html`<div class="stats-stack">
    <div class="stat-tiles">
      <${Tile} label=${voice ? `Voice in ${unit}` : 'Messages'} value=${value(data.total)} format=${voice ? (v) => decimal(v, 1) : count} />
      <${Tile} label=${`Average per ${period.interval}`} value=${value(data.average)} format=${(v) => decimal(v, 1)} />
      <${Tile} label="Contributors" value=${data.unique_members} note="Different members" />
      <${Tile} label="Channels" value=${data.unique_channels} />
    </div>
    <${ChartFrame} title=${voice ? 'Voice over time' : 'Messages over time'} subtitle=${subtitle} table=${table} file=${kind + '-over-time'} busy=${series.loading}
      tools=${html`<${MovingAverages} value=${page.ma} onChange=${(ma) => setPage({ ma })} />
        <${Toggle} label="Running total" on=${acc} onChange=${(on) => setPage({ acc: on ? '1' : '' })} title="Add each bucket to the ones before it" />`}>
      <${TimeChart} labels=${period.labels} panels=${panels} smooth=${smooth} />
    <//>
    <${ChartFrame} title="By hour of the week" subtitle=${`${voice ? `Average ${unit} of voice` : 'Average messages'} in each hour, ${period.zone_label}`}
      table=${() => heatTable(heat.data ? heat.data.cells.map((row) => row.map(value)) : [])} file=${kind + '-heatmap'} busy=${heat.loading}>
      ${heat.data ? html`<${Heatmap} cells=${heat.data.cells.map((row) => row.map(value))} format=${(v) => decimal(v, 1)} what=${voice ? unit + ' on average' : 'messages on average'} />` : html`<${Loading} />`}
    <//>
    ${!isPublic() && html`<${Ranking} by="members" kind=${kind} view=${view} page=${page} setPage=${setPage} unit=${unit} />`}
    <${Ranking} by="channels" kind=${kind} view=${view} page=${page} setPage=${setPage} unit=${unit} />
  </div>`;
}

// Everyone, or every channel, ranked, with no cap, and the top few as a donut.
function Ranking({ by, kind, view, page, setPage, unit }) {
  const { data, error, loading } = useStats('/statistics/top', { kind, by, ...viewParams(view) });
  const voice = kind === 'voice';
  const rows = useMemo(() => unpack(data), [data]);
  const size = DONUT_SIZES.includes(Number(page.top)) ? Number(page.top) : 5;
  const slots = useSlots(rows.slice(0, size).map((row) => row.id || `rank-${row.rank}`));
  const show = (number) => (voice ? decimal(inUnit(number, unit), 1) : count(number));
  if (error) return html`<${Notice} kind="bad">${error.message}<//>`;
  const people = by === 'members';
  const columns = people ? [
    { key: 'rank', label: 'Rank', numeric: true, width: '70px' },
    { key: 'id', label: 'ID', hidden: true },
    { key: 'display_name', label: 'Member', render: (row) => html`<${Person} plain person=${{ id: row.id, name: row.name, display_name: row.display_name, in_server: row.in_server, bot: row.bot }} />` },
    { key: 'name', label: 'Username', hidden: true },
    { key: 'value', label: voice ? `Voice (${unit})` : 'Messages', numeric: true, text: (row) => show(row.value), csv: (row) => (voice ? inUnit(row.value, unit) : row.value) },
    { key: 'channels', label: 'Channels', numeric: true },
    { key: 'in_server', label: 'In the server', text: (row) => (row.in_server === false ? 'No' : row.in_server ? 'Yes' : ''), hidden: true },
  ] : [
    { key: 'rank', label: 'Rank', numeric: true, width: '70px' },
    { key: 'id', label: 'ID', hidden: true, text: (row) => row.id || '' },
    { key: 'name', label: 'Channel', render: (row) => html`<${Channel} channel=${{ id: row.id, name: row.name, type: row.type, category: row.category, deleted: row.deleted, hidden: row.hidden }} />` },
    { key: 'category', label: 'Category', text: (row) => row.category || '' },
    { key: 'value', label: voice ? `Voice (${unit})` : 'Messages', numeric: true, text: (row) => show(row.value), csv: (row) => (voice ? inUnit(row.value, unit) : row.value) },
    { key: 'members', label: 'Members', numeric: true },
    { key: 'deleted', label: 'Deleted', text: (row) => (row.deleted ? 'Yes' : 'No'), hidden: true },
  ];
  const slices = topSlices(rows, size, { key: (row) => row.id || `rank-${row.rank}`, name: (row) => (people ? row.display_name : row.name), value: (row) => row.value, slots });
  const title = `${people ? 'Top members' : 'Top channels'} by ${voice ? 'voice' : 'messages'}`;
  return html`<div class="stats-ranking">
    <${ChartFrame} title=${title} subtitle=${data ? `${data.period.label}. ${data.limited
      ? `The first ${rows.length.toLocaleString('en-US')}. The whole list takes the role that opens drilldowns.` : 'Everyone with something in the period.'}` : ''}
      file=${`top-${by}-${kind}`} image=${false} busy=${loading}>
      ${!data ? html`<${Loading} />` : html`<${StatTable} label=${title} columns=${columns} rows=${rows} rowKey=${(row) => row.id || `hidden-${row.rank}`} file=${`top-${by}-${kind}`}
        initialSort=${{ key: 'rank', down: false }} empty=${people ? 'Nobody has anything in this period.' : 'No channel has anything in this period.'} />`}
    <//>
    <${ChartFrame} title=${`Share of the top ${size}`} subtitle=${people ? 'Members' : 'Channels'} file=${`share-${by}-${kind}`} busy=${loading}
      table=${() => ({ columns: [people ? 'Member' : 'Channel', voice ? `Voice (${unit})` : 'Messages'], rows: slices.map((slice) => [slice.name, voice ? inUnit(slice.value, unit) : slice.value]) })}
      tools=${html`<select class="stats-choice" aria-label="How many to show" value=${size} onChange=${(e) => setPage({ top: e.currentTarget.value })}>
        ${DONUT_SIZES.map((n) => html`<option key=${n} value=${n}>Top ${n}</option>`)}</select>`}>
      ${data ? html`<${Donut} items=${slices} format=${show} />` : html`<${Loading} />`}
    <//>
  </div>`;
}
