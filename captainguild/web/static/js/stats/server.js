// The Status, Activity and Emoji pages: what the whole server does, with no one's name on it.
import { html, useMemo } from '../lib.js';
import { Loading, Notice } from '../ui.js';
import { ChartFrame, Heatmap, TimeChart, heatTable, useSlots } from '../charts.js';
import { KIND_LABELS, count, dateText, decimal, inUnit, useStats, viewParams } from './common.js';
import { StatTable } from './table.js';
import { Tile } from './overview.js';

const stamp = (period, index) => new Date(period.buckets[index] * 1000).toISOString();

export function Status({ ctx, view, smooth }) {
  const params = viewParams(view);
  const { data, error, loading } = useStats('/statistics/status', params);
  const heat = useStats('/statistics/heatmap', { kind: 'status', ...params });
  const panels = useMemo(() => {
    if (!data) return [];
    const round = (values) => values.map((value) => (value === null ? null : Math.round(value * 10) / 10));
    // Offline is most of a big server, so it gets a panel of its own and the rest keep a scale they can be read on.
    const lines = data.present_only
      ? [{ key: 'present', name: "Online (Discord's count)", values: round(data.present), slot: 0, kind: 'line' }]
      : [{ key: 'online', name: 'Online', values: round(data.online), slot: 0, kind: 'line' }, { key: 'idle', name: 'Idle', values: round(data.idle), slot: 1, kind: 'line' },
        { key: 'dnd', name: 'Do not disturb', values: round(data.dnd), slot: 2, kind: 'line' }, { key: 'present', name: 'Not offline', values: round(data.present), slot: 3, kind: 'line' }];
    return [{ key: 'status', title: 'Members by status, on average', format: (v) => decimal(v, 1), series: lines },
      { key: 'offline', title: 'Offline, on average', format: (v) => decimal(v, 1), fit: true, height: 110, series: [{ key: 'offline', name: 'Offline', values: round(data.offline), slot: 0, kind: 'line' }] }];
  }, [data]);
  if (error) return html`<${Notice} kind="bad">${error.message}<//>`;
  if (!data) return html`<${Loading} />`;
  const period = data.period;
  const every = panels.flatMap((panel) => panel.series);
  const table = () => ({ columns: ['Bucket', 'Starts', ...every.map((series) => series.name)],
    rows: period.labels.map((label, index) => [label, stamp(period, index), ...every.map((series) => series.values[index])]) });
  return html`<div class="stats-stack">
    ${data.present_only && html`<${Notice}>This server has only Discord's count of members online, because Discord hasn't given the bot the Presence intent. Online, idle and do not disturb show apart once it has.<//>`}
    ${!data.breakdown && !data.present_only && html`<${Notice}>No status readings in this period yet. The bot reads statuses once a minute while statistics are on.<//>`}
    <${ChartFrame} title="Statuses over time" subtitle=${`${period.label}, the average in each bucket, ${period.zone_label}`} table=${table} file="statuses" busy=${loading}>
      <${TimeChart} labels=${period.labels} panels=${panels} smooth=${smooth} />
    <//>
    <${ChartFrame} title="Members not offline, by hour of the week" subtitle=${`Average in each hour, ${period.zone_label}`} table=${() => heatTable(heat.data ? heat.data.cells : [], (v) => decimal(v, 1))}
      file="statuses-heatmap" busy=${heat.loading}>
      ${heat.data ? html`<${Heatmap} cells=${heat.data.cells} format=${(v) => decimal(v, 1)} what="members not offline on average" />` : html`<${Loading} />`}
    <//>
  </div>`;
}

export function Apps({ ctx, view, unit, smooth }) {
  const { data, error, loading } = useStats('/statistics/apps', viewParams(view));
  const time = (seconds) => inUnit(seconds, unit);
  const panels = useMemo(() => {
    if (!data || !data.series.length) return [];
    const names = Object.fromEntries(data.rows.map((row) => [row.id, row.name]));
    const top = Math.max(...data.series.flatMap((series) => series.values.map(time)));
    return data.series.map((series) => ({ key: String(series.id), title: names[series.id] || 'Unknown app', height: 70, top, format: (v) => `${decimal(v, 1)} ${unit}`,
      series: [{ key: String(series.id), name: names[series.id] || 'Unknown app', values: series.values.map(time), slot: 0, kind: 'area' }] }));
  }, [data, unit]);
  if (error) return html`<${Notice} kind="bad">${error.message}<//>`;
  if (!data) return html`<${Loading} />`;
  const period = data.period;
  const columns = [
    { key: 'name', label: 'Game or app', render: (row) => html`<span title=${`ID ${row.id}${row.application_id ? `\nDiscord application ${row.application_id}` : ''}`}>${row.name}</span>` },
    { key: 'kind', label: 'Kind', text: (row) => KIND_LABELS[row.kind] || row.kind },
    { key: 'verified', label: 'Verified', text: (row) => (row.verified ? 'Yes' : 'No') },
    { key: 'seconds', label: `Time (${unit})`, numeric: true, text: (row) => decimal(time(row.seconds), 1), csv: (row) => time(row.seconds) },
    { key: 'people', label: 'Members', numeric: true },
    { key: 'first_seen', label: 'First seen', numeric: true, text: (row) => dateText(row.first_seen, period.zone) },
    { key: 'id', label: 'ID', hidden: true },
    { key: 'application_id', label: 'Discord application', hidden: true },
  ];
  const table = () => ({ columns: ['Bucket', 'Starts', ...panels.map((panel) => `${panel.title} (${unit})`)],
    rows: period.labels.map((label, index) => [label, stamp(period, index), ...panels.map((panel) => panel.series[0].values[index])]) });
  return html`<div class="stats-stack">
    ${!data.tracking && html`<${Notice} kind="warn">Activity tracking is off, so no games or apps are being counted. ${ctx.can_settings ? 'Turn it on in Statistics settings.' : 'Someone with Manage Server can turn it on.'}<//>`}
    ${!data.presences && html`<${Notice}>Discord hasn't given this bot the Presence intent, so it can't see what members play or listen to. Counting starts once it has.<//>`}
    <div class="stat-tiles">
      <${Tile} label="Games and apps" value=${data.rows.length} />
      <${Tile} label=${`Time in them (${unit})`} value=${time(data.rows.reduce((sum, row) => sum + row.seconds, 0))} format=${(v) => decimal(v, 1)} />
    </div>
    <${ChartFrame} title="The top ten over time" subtitle=${`${period.label}, ${unit} in each bucket on one scale, ${period.zone_label}`} table=${table} file="activities-over-time" busy=${loading}>
      ${panels.length ? html`<${TimeChart} labels=${period.labels} panels=${panels} smooth=${smooth} height=${70} />` : html`<div class="viz-empty">No games or apps in this period.</div>`}
    <//>
    <${ChartFrame} title="Every game and app" subtitle=${period.label} image=${false} file="activities">
      <${StatTable} label="Games and apps" columns=${columns} rows=${data.rows} file="activities" initialSort=${{ key: 'seconds', down: true }} empty="No games or apps in this period." />
    <//>
  </div>`;
}

export function EmojiFace({ row, size = 22 }) {
  if (row.image) return html`<img class="stats-emoji" src=${row.image} alt=${row.name} title=${`${row.name}\n${row.key}`} width=${size} height=${size} loading="lazy" />`;
  return html`<span class="stats-emoji-text" title=${row.key}>${row.text}</span>`;
}

export function Emoji({ ctx, view, smooth }) {
  const { data, error, loading } = useStats('/statistics/emoji', viewParams(view));
  const byKey = useMemo(() => Object.fromEntries((data ? data.rows : []).map((row) => [row.key, row])), [data]);
  const slots = useSlots(data ? data.series.map((series) => series.key) : []);
  const panels = useMemo(() => {
    if (!data || !data.series.length) return [];
    return [{ key: 'uses', title: 'Uses of the top emoji', format: count, series: data.series.map((series) => ({
      key: series.key, name: byKey[series.key] ? (byKey[series.key].kind === 'standard' ? byKey[series.key].text : `:${byKey[series.key].name}:`) : series.key,
      values: series.values, slot: slots(series.key), kind: 'line' })) }];
  }, [data, byKey, slots]);
  if (error) return html`<${Notice} kind="bad">${error.message}<//>`;
  if (!data) return html`<${Loading} />`;
  const period = data.period;
  const columns = [
    { key: 'face', label: 'Emoji', render: (row) => html`<span class="stats-name"><${EmojiFace} row=${row} /><span>${row.kind === 'standard' ? '' : row.name}</span></span>`, text: (row) => row.name },
    { key: 'kind', label: 'Kind', text: (row) => ({ standard: 'Standard', emoji: 'Server emoji', sticker: 'Sticker' }[row.kind] || row.kind) },
    { key: 'state', label: 'State', text: (row) => ({ used: 'Used', unused: 'Not used', deleted: 'Deleted', unavailable: 'Unavailable' }[row.state] || row.state) },
    { key: 'uses', label: 'Uses', numeric: true },
    { key: 'reactions', label: 'Reactions', numeric: true },
    { key: 'self_reactions', label: 'On own messages', numeric: true },
    { key: 'messages', label: 'In messages', numeric: true },
    { key: 'stickers', label: 'As stickers', numeric: true },
    { key: 'super', label: 'Super reactions', numeric: true },
    { key: 'people', label: 'Members', numeric: true },
    { key: 'key', label: 'ID or character', hidden: true },
  ];
  const table = () => ({ columns: ['Bucket', 'Starts', ...panels[0].series.map((series) => series.name)],
    rows: period.labels.map((label, index) => [label, stamp(period, index), ...panels[0].series.map((series) => series.values[index])]) });
  return html`<div class="stats-stack">
    <${Notice}>What counts: standard emoji, and this server's own emoji and stickers, in reactions, in message text and as stickers. Emoji from other servers and Discord's own sticker packs don't count, and neither does anything a bot does, though reactions people add to a bot's message do. An emoji repeated in one message counts once.<//>
    <div class="stat-tiles">
      <${Tile} label="Uses" value=${data.rows.reduce((sum, row) => sum + row.uses, 0)} />
      <${Tile} label="Emoji used" value=${data.rows.filter((row) => row.uses).length} />
      <${Tile} label="Server emoji not used" value=${data.rows.filter((row) => row.state === 'unused').length} />
    </div>
    <${ChartFrame} title="The top emoji over time" subtitle=${`${period.label}, ${period.zone_label}`} table=${panels.length ? table : null} file="emoji-over-time" busy=${loading}>
      ${panels.length ? html`<${TimeChart} labels=${period.labels} panels=${panels} smooth=${smooth} />` : html`<div class="viz-empty">No emoji used in this period.</div>`}
    <//>
    <${ChartFrame} title="Every emoji and sticker" subtitle=${period.label} image=${false} file="emoji">
      <${StatTable} label="Emoji" columns=${columns} rows=${data.rows} rowKey=${(row) => row.key} file="emoji" initialSort=${{ key: 'uses', down: true }} empty="No emoji in this view." />
    <//>
  </div>`;
}
