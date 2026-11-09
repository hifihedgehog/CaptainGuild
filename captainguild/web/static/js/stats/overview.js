// The Overview: the member count with joins and leaves, the period's headline numbers beside the period before it, and the panels each
// person builds for themselves.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { Icon, Loading, Modal, Notice, Select, Switch, TextInput, confirmDialog, toast, toastError } from '../ui.js';
import { ChartFrame, Sparkline, TimeChart } from '../charts.js';
import { count, decimal, inUnit, isPublic, percent, periodText, statsGet, useStats, viewParams, voiceText } from './common.js';

export function Tile({ label, value, before, format = count, note, spark, since = 'on the period before' }) {
  const delta = before !== undefined && before !== null && value !== null && value !== undefined ? value - before : null;
  const change = delta === null ? null : before ? (delta / before) * 100 : null;
  return html`<div class="stat-tile stats-tile">
    <span class="muted small">${label}</span>
    <b>${value === null || value === undefined ? 'None' : format(value)}</b>
    ${delta !== null && html`<span class="small stats-delta">
      ${delta > 0 ? '↑' : delta < 0 ? '↓' : '='} ${format(Math.abs(delta))}${change !== null ? ` (${change > 0 ? '+' : ''}${decimal(change, 1)}%)` : ''} ${since}</span>`}
    ${note && html`<span class="small dim">${note}</span>`}
    ${spark && html`<${Sparkline} values=${spark} />`}
  </div>`;
}

export function Overview({ ctx, view, unit, smooth }) {
  const { data, error, loading } = useStats('/statistics/overview', viewParams(view));
  if (error) return html`<${Notice} kind="bad">${error.message}<//>`;
  if (!data) return html`<${Loading} />`;
  const t = data.totals;
  const period = data.period;
  const subtitle = `${period.label}, ${period.zone_label}`;
  const panels = [
    { key: 'members', title: 'Members', format: count, fit: true, series: [{ key: 'members', name: 'Members', values: data.series.members, slot: 0, kind: 'line' }] },
    { key: 'moves', title: 'Joins and leaves', format: count, series: [
      { key: 'joins', name: 'Joins', values: data.series.joins, slot: 0, kind: 'bars' }, { key: 'leaves', name: 'Leaves', values: data.series.leaves, slot: 1, kind: 'bars' }] },
  ];
  const table = () => ({ columns: ['Bucket', 'Starts', 'Members', 'Joins', 'Leaves'], rows: period.labels.map((label, index) => [label, new Date(period.buckets[index] * 1000).toISOString(),
    data.series.members[index], data.series.joins[index], data.series.leaves[index]]) });
  return html`<div class="stats-stack">
    <div class="stat-tiles">
      <${Tile} label="Members" value=${t.members_now} before=${t.members_start} since="since the period began" />
      <${Tile} label="Joins" value=${t.joins_now} before=${t.joins_before} />
      <${Tile} label="Leaves" value=${t.leaves_now} before=${t.leaves_before} />
      <${Tile} label="Messages" value=${t.messages_now} before=${t.messages_before} />
      <${Tile} label=${`Voice (${unit})`} value=${inUnit(t.voice_now, unit)} before=${inUnit(t.voice_before, unit)} format=${(v) => decimal(v, 1)} />
      <${Tile} label="Contributors" value=${t.contributors_now} before=${t.contributors_before} note="People with a message or voice time" />
      <${Tile} label="Online now" value=${t.online_now} format=${(v) => count(v)} note=${t.online_breakdown ? 'Members not offline' : "Discord's count of members online"} />
    </div>
    <${ChartFrame} title="Members" subtitle=${subtitle} table=${table} file="members" busy=${loading}>
      <${TimeChart} labels=${period.labels} panels=${panels} smooth=${smooth} />
    <//>
    ${!isPublic() && html`<${Panels} ctx=${ctx} unit=${unit} />`}
  </div>`;
}

// ---- personal panels ----
const STATS = [{ value: 'members', label: 'Members' }, { value: 'messages', label: 'Messages' }, { value: 'voice', label: 'Voice' }];
const VALUES = [{ value: 'count', label: 'Count (the latest bucket, or members now)' }, { value: 'sum', label: 'Sum over the period (joins, for members)' },
  { value: 'average', label: 'Average per bucket' }, { value: 'change', label: 'Change from the period before' }, { value: 'unique', label: 'Unique members or channels' }];
const BLANK = { title: '', stat: 'messages', value: 'sum', unique: 'members', percent: false, period: '7d', interval: 'day' };

function describe(panel) {
  const stat = { members: 'Members', messages: 'Messages', voice: 'Voice' }[panel.stat];
  const kind = { count: 'count', sum: 'sum', average: 'average', change: 'change', unique: `unique ${panel.unique}` }[panel.value];
  return `${stat}, ${kind}${panel.percent ? ' as a percentage' : ''}`;
}

function PanelEditor({ panel, onSave, onClose }) {
  const [draft, setDraft] = useState({ ...BLANK, ...panel });
  const set = (name, value) => setDraft({ ...draft, [name]: value });
  const percentOk = draft.value === 'change' || draft.value === 'unique';
  return html`<${Modal} title=${panel ? 'Edit panel' : 'Add a panel'} icon="gauge" onClose=${onClose} footer=${html`<div class="right">
      <button type="button" class="btn ghost" onClick=${onClose}>Cancel</button>
      <button type="button" class="btn primary" onClick=${() => onSave({ ...draft, percent: percentOk && draft.percent })}>Save</button></div>`}>
    <${TextInput} label="Title" placeholder="Shown on the panel. Leave empty to describe it." maxLength=${60} value=${draft.title} onChange=${(v) => set('title', v)} />
    <${Select} label="Statistic" options=${STATS} value=${draft.stat} onChange=${(v) => setDraft({ ...draft, stat: v, value: v === 'members' && draft.value === 'unique' ? 'count' : draft.value })} />
    <${Select} label="Value" options=${draft.stat === 'members' ? VALUES.filter((o) => o.value !== 'unique') : VALUES} value=${draft.value} onChange=${(v) => set('value', v)} />
    ${draft.value === 'unique' && html`<${Select} label="Count different" options=${[{ value: 'members', label: 'Members' }, { value: 'channels', label: 'Channels' }]} value=${draft.unique} onChange=${(v) => set('unique', v)} />`}
    ${percentOk && html`<${Switch} label="Show as a percentage" help=${draft.value === 'change' ? 'The change as a share of the period before.' : 'As a share of all members, or of all channels.'} value=${draft.percent} onChange=${(v) => set('percent', v)} />`}
    <${TextInput} label="Period" help="A number and h, d, w or mo, as in 7d or 3mo. Or since:2025-03-01, or all." value=${draft.period} onChange=${(v) => set('period', v.trim())} />
    <${Select} label="Bucket size" options=${[{ value: 'hour', label: 'Hour' }, { value: 'day', label: 'Day' }, { value: 'week', label: 'Week' }, { value: 'month', label: 'Month' }]} value=${draft.interval} onChange=${(v) => set('interval', v)} />
  <//>`;
}

function Panels({ ctx, unit }) {
  const [panels, setPanels] = useState(null);
  const [most, setMost] = useState(12);
  const [values, setValues] = useState([]);
  const [editing, setEditing] = useState(null);
  const file = useRef(null);
  const round = useRef(0);
  // Each answer holds the panels the server read in about half a second and says where to carry on, so a dozen panels over all time
  // fill in one after another rather than in one long wait. A newer set of panels stops the older round.
  const loadValues = () => {
    const mine = ++round.current;
    const step = (from) => statsGet('/statistics/panels/values', { from }).then((data) => {
      if (mine !== round.current) return;
      setValues((now) => {
        const next = from ? [...now] : [];
        data.values.forEach((value, index) => { next[from + index] = value; });
        return next;
      });
      if (data.next !== null && data.next !== undefined) step(data.next);
    }).catch(toastError);
    step(0);
  };
  useEffect(() => {
    statsGet('/statistics/panels').then((data) => { setPanels(data.panels); setMost(data.most || 12); if (data.panels.length) loadValues(); })
      .catch((error) => { toastError(error); setPanels([]); });
  }, []);
  const store = async (next) => {
    try {
      const saved = await api.put('/statistics/panels', { panels: next });
      setPanels(saved.panels);
      setValues([]);
      if (saved.panels.length) loadValues();
      return true;
    } catch (error) { toastError(error); return false; }
  };
  const exportPanels = () => {
    const blob = new Blob([JSON.stringify({ panels }, null, 2)], { type: 'application/json' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'statistics-panels.json';
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 2000);
  };
  const importPanels = async (event) => {
    const picked = event.currentTarget.files[0];
    event.currentTarget.value = '';
    if (!picked) return;
    try {
      const parsed = JSON.parse(await picked.text());
      const list = Array.isArray(parsed) ? parsed : parsed.panels;
      if (!Array.isArray(list)) throw new Error('That file holds no panels.');
      if (await store([...panels, ...list])) toast(`Added ${list.length} panel${list.length === 1 ? '' : 's'}.`);
    } catch (error) { toast(error.message || 'That file could not be read.', 'bad'); }
  };
  const remove = async (index) => {
    if (await confirmDialog({ title: 'Remove this panel?', body: describe(panels[index]), confirm: 'Remove', danger: true })) store(panels.filter((_, at) => at !== index));
  };
  const format = (panel, number) => {
    if (number === null || number === undefined) return 'None';
    if (panel.percent) return percent(number);
    if (panel.stat === 'voice' && panel.value !== 'unique') return voiceText(number, unit);
    return panel.value === 'average' ? decimal(number, 1) : (panel.value === 'change' && number > 0 ? '+' : '') + count(number);
  };
  return html`<section class="stats-panels" aria-label="Your panels">
    <div class="spread"><h3>Your panels</h3><div class="row">
      <button type="button" class="btn small" disabled=${!panels || panels.length >= most} title=${panels && panels.length >= most ? `You can keep ${most} panels here.` : ''}
        onClick=${() => setEditing({ index: -1 })}><${Icon} name="plus" size=${14} /> Add a panel</button>
      <button type="button" class="btn small ghost" disabled=${!panels || !panels.length} onClick=${exportPanels}><${Icon} name="download" size=${14} /> Export</button>
      <button type="button" class="btn small ghost" onClick=${() => file.current.click()}><${Icon} name="upload" size=${14} /> Import</button>
      <input ref=${file} class="hidden" type="file" accept="application/json,.json" aria-label="Import panels" onChange=${importPanels} />
    </div></div>
    <p class="muted small">Panels are yours alone, in this server. Each reads its own period under the server's filters.</p>
    ${panels === null && html`<${Loading} />`}
    ${panels && panels.length === 0 && html`<div class="empty">No panels yet. Add one to keep a number you check often in view.</div>`}
    ${panels && panels.length > 0 && html`<div class="stat-tiles">${panels.map((panel, index) => {
      const value = values[index];
      return html`<div key=${index} class="stat-tile stats-tile stats-panel">
        <span class="muted small">${panel.title || describe(panel)}</span>
        <b>${value ? format(panel, value.number) : '...'}</b>
        <span class="small dim">${value ? value.label : periodText(panel.period)}</span>
        ${value && value.series && value.series.length > 1 && html`<${Sparkline} values=${panel.stat === 'voice' && panel.value !== 'unique' ? value.series.map((v) => inUnit(v, unit)) : value.series} />`}
        <div class="stats-panel-tools">
          <button type="button" class="icon-btn" aria-label="Edit this panel" onClick=${() => setEditing({ index, panel })}><${Icon} name="pencil" size=${14} /></button>
          <button type="button" class="icon-btn" aria-label="Move this panel earlier" disabled=${index === 0} onClick=${() => { const next = [...panels]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; store(next); }}><${Icon} name="arrow-left" size=${14} /></button>
          <button type="button" class="icon-btn danger" aria-label="Remove this panel" onClick=${() => remove(index)}><${Icon} name="trash-2" size=${14} /></button>
        </div>
      </div>`;
    })}</div>`}
    ${editing && html`<${PanelEditor} panel=${editing.panel} onClose=${() => setEditing(null)} onSave=${async (panel) => {
      const next = editing.index < 0 ? [...panels, panel] : panels.map((old, at) => (at === editing.index ? panel : old));
      if (await store(next)) setEditing(null);
    }} />`}
  </section>`;
}
