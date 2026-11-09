// The dashboard's charts, drawn as SVG. Colors come from tokens on .viz in app.css: eight series colors in a fixed order, a gray for
// "Other", a one-hue ramp for heatmaps, and the chart's own ink and rules, with a set for each theme. A chart never shows a ninth
// color: callers fold the rest into "Other". Every chart can switch to a table, and download as CSV or as an image.
import { createContext, html, useContext, useEffect, useMemo, useRef, useState } from './lib.js';
import { Icon } from './ui.js';

export const SLOTS = 8;
export const slotColor = (slot) => (slot >= 0 && slot < SLOTS ? `var(--series-${slot + 1})` : 'var(--viz-other)');
export const OTHER = -1;
const GAP = 2; // the surface gap between touching marks
const BAR_MAX = 24;

// ---- numbers ----
const fullFormat = new Intl.NumberFormat('en-US');
export const full = (value) => (value === null || value === undefined || Number.isNaN(value) ? '' : fullFormat.format(Math.round(value * 100) / 100));
export function compact(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '';
  const size = Math.abs(value);
  const trim = (text) => text.replace(/\.0$/, '');
  if (size >= 1e9) return trim((value / 1e9).toFixed(1)) + 'B';
  if (size >= 1e6) return trim((value / 1e6).toFixed(1)) + 'M';
  if (size >= 1e4) return trim((value / 1e3).toFixed(1)) + 'K';
  return full(value);
}

// Clean ticks from `smallest` (zero unless a line is fitted to its values) to past the largest value. An axis reaching 10,000 reads
// compact, below that in full.
export function niceTicks(largest, count = 4, smallest = 0, whole = false) {
  const top = largest > smallest ? largest : smallest + 1;
  const raw = (top - smallest) / count;
  const power = 10 ** Math.floor(Math.log10(raw));
  // Counts of things tick in whole numbers: no 2.5 steps below ten, and nothing under one.
  const steps = whole && power <= 1 ? [1, 2, 5, 10] : [1, 2, 2.5, 5, 10];
  const step = Math.max(steps.map((m) => m * power).find((s) => s >= raw) || raw, whole ? 1 : 0);
  const start = Math.floor(smallest / step + 1e-9) * step;
  const end = Math.ceil(top / step - 1e-9) * step;
  const ticks = [];
  for (let value = start; value <= end + step / 2; value += step) ticks.push(Math.round(value * 1e6) / 1e6);
  const label = end >= 10000 ? compact : full;
  return { start, end, ticks, label };
}

// ---- running values ----
export function runningTotal(values) {
  let sum = 0;
  return values.map((value) => (value === null || value === undefined ? null : (sum += value)));
}
export function movingAverage(values, size) {
  const out = [];
  let sum = 0;
  for (let index = 0; index < values.length; index += 1) {
    sum += values[index] || 0;
    if (index >= size) sum -= values[index - size] || 0;
    out.push(index >= size - 1 ? sum / size : null);
  }
  return out;
}
export const average = (values) => {
  const known = values.filter((value) => value !== null && value !== undefined);
  return known.length ? known.reduce((a, b) => a + b, 0) / known.length : 0;
};

// Colors that follow what they stand for: an entity keeps its slot while it stays on the chart, however the ranking moves.
export function useSlots(keys) {
  const kept = useRef(new Map());
  return useMemo(() => {
    const map = kept.current;
    const wanted = new Set(keys.map(String));
    for (const key of [...map.keys()]) if (!wanted.has(key)) map.delete(key);
    const used = new Set(map.values());
    for (const key of keys.map(String)) {
      if (map.has(key)) continue;
      let slot = 0;
      while (used.has(slot) && slot < SLOTS) slot += 1;
      map.set(key, slot < SLOTS ? slot : OTHER);
      used.add(slot);
    }
    return (key) => (map.has(String(key)) ? map.get(String(key)) : OTHER);
  }, [keys.join('|')]);
}

function useWidth(fallback = 640) {
  const box = useRef(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const node = box.current;
    if (!node) return undefined;
    const measure = () => setWidth(Math.max(260, Math.floor(node.getBoundingClientRect().width)));
    measure();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }
    const watcher = new ResizeObserver(measure);
    watcher.observe(node);
    return () => watcher.disconnect();
  }, []);
  return [box, width];
}

// ---- paths ----
function linePath(points, smooth) {
  // points: [[x, y] or null]. A null breaks the line.
  const runs = [];
  let run = [];
  for (const point of points) {
    if (point) run.push(point);
    else if (run.length) { runs.push(run); run = []; }
  }
  if (run.length) runs.push(run);
  return runs.map((part) => (smooth && part.length > 2 ? monotone(part) : 'M' + part.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join('L'))).join('');
}

// A smooth line that never overshoots the points it passes through (monotone cubic, Fritsch and Carlson).
function monotone(points) {
  const n = points.length;
  const dx = [];
  const slope = [];
  for (let i = 0; i < n - 1; i += 1) {
    dx.push(points[i + 1][0] - points[i][0]);
    slope.push((points[i + 1][1] - points[i][1]) / (dx[i] || 1));
  }
  const tangent = [slope[0]];
  for (let i = 1; i < n - 1; i += 1) tangent.push(slope[i - 1] * slope[i] <= 0 ? 0 : (slope[i - 1] + slope[i]) / 2);
  tangent.push(slope[n - 2]);
  for (let i = 0; i < n - 1; i += 1) {
    if (slope[i] === 0) { tangent[i] = 0; tangent[i + 1] = 0; continue; }
    const a = tangent[i] / slope[i];
    const b = tangent[i + 1] / slope[i];
    const h = a * a + b * b;
    if (h > 9) {
      const t = 3 / Math.sqrt(h);
      tangent[i] = t * a * slope[i];
      tangent[i + 1] = t * b * slope[i];
    }
  }
  let path = `M${points[0][0].toFixed(1)},${points[0][1].toFixed(1)}`;
  for (let i = 0; i < n - 1; i += 1) {
    const [x0, y0] = points[i];
    const [x1, y1] = points[i + 1];
    const third = dx[i] / 3;
    path += `C${(x0 + third).toFixed(1)},${(y0 + tangent[i] * third).toFixed(1)} ${(x1 - third).toFixed(1)},${(y1 - tangent[i + 1] * third).toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)}`;
  }
  return path;
}

// A column with a 4px rounded data end and a square foot on the baseline.
function columnPath(x, y, width, height) {
  if (height <= 0 || width <= 0) return '';
  const r = Math.min(4, width / 2, height);
  return `M${x},${y + height}V${y + r}Q${x},${y} ${x + r},${y}H${x + width - r}Q${x + width},${y} ${x + width},${y + r}V${y + height}Z`;
}

// ---- the time chart ----
// panels: [{ key, title, format(value), series: [{ key, name, values, slot, kind: 'line' | 'area' | 'bars' }], stacked, refs: [{ value, label }] }]
// Panels share the time axis and one crosshair, and each has its own y axis: measures of different sizes never share an axis.
export function TimeChart({ labels, panels, smooth = false, height = 170, empty = 'Nothing in this period.' }) {
  const [box, width] = useWidth();
  const [hover, setHover] = useState(null);
  const [hidden, setHidden] = useState({});
  const count = labels.length;
  const left = 58;
  const endLabels = panels.some((panel) => panel.series.length >= 2 && panel.series.length <= 4 && panel.series.every((s) => s.kind !== 'bars') && !panel.stacked);
  const right = endLabels ? 96 : 14;
  const plot = Math.max(width - left - right, 40);
  const band = count ? plot / count : plot;
  const center = (index) => left + (index + 0.5) * band;
  const panelGap = 30;
  const axisBand = 26;

  const geometry = useMemo(() => {
    let offset = 0;
    return panels.map((panel) => {
      const shown = panel.series.filter((series) => !hidden[series.key]);
      let largest = 0;
      if (panel.stacked) {
        for (let index = 0; index < count; index += 1) largest = Math.max(largest, shown.reduce((sum, series) => sum + (series.values[index] || 0), 0));
      } else {
        for (const series of shown) for (const value of series.values) if (value > largest) largest = value;
      }
      for (const ref of panel.refs || []) if (ref.value > largest) largest = ref.value;
      if (panel.top !== undefined) largest = Math.max(panel.top, 0);  // small multiples share one scale, so their heights compare
      // A line of something that never nears zero, such as a member count, fits its axis to its values. Bars and areas keep zero.
      let smallest = 0;
      if (panel.fit && !panel.stacked && shown.every((series) => series.kind === 'line')) {
        const values = shown.flatMap((series) => series.values.filter((value) => value !== null && value !== undefined));
        if (values.length) {
          const low = Math.min(...values);
          smallest = Math.max(0, low - (largest - low) * 0.1);
        }
      }
      const whole = shown.every((series) => series.values.every((value) => value === null || value === undefined || Number.isInteger(value)));
      const scale = niceTicks(largest, 4, smallest, whole);
      const top = offset + 18;
      const bottom = top + (panel.height || height);
      const y = (value) => bottom - ((Math.max(value, scale.start) - scale.start) / (scale.end - scale.start)) * (panel.height || height);
      offset = bottom + panelGap;
      return { panel, shown, scale, top, bottom, y };
    });
  }, [panels, hidden, height, count]);
  const total = geometry.length ? geometry[geometry.length - 1].bottom + axisBand : axisBand;

  const marks = useMemo(() => geometry.map(({ panel, shown, top, bottom, y }) => {
    const out = [];
    const columns = shown.filter((series) => series.kind === 'bars');
    if (panel.stacked) {
      const floor = new Array(count).fill(0);
      shown.forEach((series, order) => {
        const pieces = [];
        for (let index = 0; index < count; index += 1) {
          const value = series.values[index] || 0;
          if (!value) continue;
          const low = floor[index];
          floor[index] += value;
          const width = Math.max(Math.min(BAR_MAX, band - (band > 4 ? GAP : 0)), 1);
          const x = center(index) - width / 2;
          const yTop = y(floor[index]);
          const yLow = y(low);
          const isTop = order === shown.length - 1 || shown.slice(order + 1).every((other) => !(other.values[index] || 0));
          const height = Math.max(yLow - yTop - (low > 0 && band > 4 ? GAP : 0), 0.5);
          pieces.push(isTop ? columnPath(x, yTop, width, height) : `M${x},${yTop}h${width}v${height}h${-width}Z`);
        }
        out.push(html`<path key=${series.key} d=${pieces.join('')} fill=${slotColor(series.slot)} />`);
      });
      return out;
    }
    if (columns.length) {
      const groupWidth = Math.min(band - (band > 4 ? GAP : 0), BAR_MAX * columns.length + GAP * (columns.length - 1));
      const each = Math.max((groupWidth - GAP * (columns.length - 1)) / columns.length, 1);
      columns.forEach((series, order) => {
        const pieces = [];
        for (let index = 0; index < count; index += 1) {
          const value = series.values[index];
          if (!value) continue;
          const x = center(index) - groupWidth / 2 + order * (each + GAP);
          pieces.push(columnPath(x, y(value), each, bottom - y(value)));
        }
        out.push(html`<path key=${series.key} d=${pieces.join('')} fill=${slotColor(series.slot)} />`);
      });
    }
    for (const series of shown) {
      if (series.kind === 'bars') continue;
      const points = series.values.map((value, index) => (value === null || value === undefined ? null : [center(index), y(value)]));
      const line = linePath(points, smooth);
      if (series.kind === 'area') {
        const runs = [];
        let run = [];
        points.forEach((point) => { if (point) run.push(point); else if (run.length) { runs.push(run); run = []; } });
        if (run.length) runs.push(run);
        const fill = runs.map((part) => `${smooth && part.length > 2 ? monotone(part) : 'M' + part.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join('L')}L${part[part.length - 1][0].toFixed(1)},${bottom}L${part[0][0].toFixed(1)},${bottom}Z`).join('');
        out.push(html`<path key=${series.key + '-fill'} d=${fill} fill=${slotColor(series.slot)} fill-opacity="0.1" />`);
      }
      out.push(html`<path key=${series.key} d=${line} fill="none" stroke=${slotColor(series.slot)} stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />`);
      const known = points.filter(Boolean);
      if (known.length === 1) out.push(html`<circle key=${series.key + '-dot'} cx=${known[0][0]} cy=${known[0][1]} r="4" fill=${slotColor(series.slot)} stroke="var(--viz-surface)" stroke-width="2" />`);
    }
    return out;
  }), [geometry, smooth, band, count]);

  const tickEvery = Math.max(1, Math.ceil(count / Math.max(Math.floor(plot / 92), 2)));
  const pointAt = (event) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const index = Math.floor((event.clientX - rect.left - left) / band);
    setHover(index >= 0 && index < count ? index : null);
  };
  const onKey = (event) => {
    if (!count) return;
    if (event.key === 'ArrowRight') { event.preventDefault(); setHover((at) => (at === null ? 0 : Math.min(at + 1, count - 1))); }
    else if (event.key === 'ArrowLeft') { event.preventDefault(); setHover((at) => (at === null ? count - 1 : Math.max(at - 1, 0))); }
    else if (event.key === 'Escape') setHover(null);
  };
  const flip = (key) => setHidden((now) => ({ ...now, [key]: !now[key] }));
  if (!count) return html`<div class="viz-empty">${empty}</div>`;
  const tipLeft = hover === null ? 0 : center(hover);
  return html`<div class="viz-time" ref=${box}>
    <div class="viz-stage" tabindex="0" role="group" aria-label="Chart. Use the arrow keys to read each point." onKeyDown=${onKey} onBlur=${() => setHover(null)}>
      <svg class="viz-svg" width=${width} height=${total} viewBox=${`0 0 ${width} ${total}`} onPointerMove=${pointAt} onPointerLeave=${() => setHover(null)}>
        <rect x="0" y="0" width=${width} height=${total} fill="var(--viz-surface)" />
        ${geometry.map(({ panel, scale, top, bottom, y }, at) => html`<g key=${panel.key}>
          <text x=${left} y=${top - 6} class="viz-panel-title">${panel.title}</text>
          ${scale.ticks.map((tick) => html`<g key=${tick}>
            <line x1=${left} x2=${left + plot} y1=${y(tick)} y2=${y(tick)} stroke=${tick === scale.start ? 'var(--viz-axis)' : 'var(--viz-grid)'} stroke-width="1" shape-rendering="crispEdges" />
            <text x=${left - 8} y=${y(tick) + 4} text-anchor="end" class="viz-tick">${(panel.tick || scale.label)(tick)}</text>
          </g>`)}
          ${marks[at]}
          ${(panel.refs || []).map((ref) => html`<g key=${ref.label}>
            <line x1=${left} x2=${left + plot} y1=${y(ref.value)} y2=${y(ref.value)} stroke="var(--viz-ink-2)" stroke-width="1" shape-rendering="crispEdges" />
            <text x=${left + plot} y=${y(ref.value) - 5} text-anchor="end" class="viz-ref">${ref.label}</text>
          </g>`)}
          ${endLabels && panel.series.length >= 2 && panel.series.length <= 4 && !panel.stacked && endLabelMarks(panel.series.filter((s) => !hidden[s.key] && s.kind !== 'bars'), y, left + plot)}
          ${hover !== null && panel.series.filter((series) => !hidden[series.key] && series.kind !== 'bars').map((series) => {
            const value = series.values[hover];
            return value === null || value === undefined ? null : html`<circle key=${series.key} class="viz-hover" cx=${center(hover)} cy=${y(value)} r="4" fill=${slotColor(series.slot)} stroke="var(--viz-surface)" stroke-width="2" />`;
          })}
        </g>`)}
        ${hover !== null && html`<line class="viz-hover" x1=${center(hover)} x2=${center(hover)} y1=${geometry[0].top} y2=${geometry[geometry.length - 1].bottom} stroke="var(--viz-ink-2)" stroke-width="1" shape-rendering="crispEdges" pointer-events="none" />`}
        ${labels.map((label, index) => (index % tickEvery === 0 ? html`<text key=${index} x=${center(index)} y=${total - 8} text-anchor="middle" class="viz-tick">${label}</text>` : null))}
      </svg>
      ${hover !== null && html`<div class=${'viz-tip' + (tipLeft > width * 0.6 ? ' flip' : '')} style=${{ left: tipLeft + 'px' }} role="status">
        <div class="viz-tip-title">${labels[hover]}</div>
        ${geometry.map(({ panel }) => html`<div key=${panel.key} class="viz-tip-group">
          ${geometry.length > 1 && html`<div class="viz-tip-panel">${panel.title}</div>`}
          ${panel.series.filter((series) => !hidden[series.key]).map((series) => html`<div key=${series.key} class="viz-tip-row">
            <span class="viz-key line" style=${{ background: slotColor(series.slot) }}></span>
            <b>${series.values[hover] === null || series.values[hover] === undefined ? 'None' : panel.format(series.values[hover])}</b>
            <span class="viz-tip-name">${series.name}</span>
          </div>`)}
        </div>`)}
      </div>`}
    </div>
    ${geometry.some(({ panel }) => panel.series.length > 1) && html`<div class="viz-legends">${geometry.filter(({ panel }) => panel.series.length > 1).map(({ panel }) => html`
      <div key=${panel.key} class="viz-legend" role="group" aria-label=${panel.title + ' legend'}>
        ${geometry.length > 1 && html`<span class="viz-legend-title">${panel.title}</span>`}
        ${panel.series.map((series) => html`<button key=${series.key} type="button" class=${'viz-legend-item' + (hidden[series.key] ? ' off' : '')} aria-pressed=${!hidden[series.key]}
          title=${hidden[series.key] ? 'Show this series' : 'Hide this series'} onClick=${() => flip(series.key)}>
          <span class=${'viz-key ' + (series.kind === 'line' ? 'line' : 'box')} style=${{ background: slotColor(series.slot) }}></span>${series.name}</button>`)}
      </div>`)}</div>`}
  </div>`;
}

// The name of each line at its end, when the ends sit far enough apart to tell. When they crowd, the legend and the tooltip carry them.
function endLabelMarks(series, y, x) {
  const ends = series.map((one) => {
    for (let index = one.values.length - 1; index >= 0; index -= 1) {
      if (one.values[index] !== null && one.values[index] !== undefined) return { one, at: y(one.values[index]) };
    }
    return null;
  }).filter(Boolean).sort((a, b) => a.at - b.at);
  for (let index = 1; index < ends.length; index += 1) if (ends[index].at - ends[index - 1].at < 15) return null;
  return ends.map(({ one, at }) => html`<text key=${one.key} x=${x + 8} y=${at + 4} class="viz-end">${one.name.length > 14 ? one.name.slice(0, 13) + '…' : one.name}</text>`);
}

// ---- part of a whole ----
// items: [{ key, name, value, slot }]. The caller folds everything past the top few into one "Other" item with slot OTHER.
export function Donut({ items, format = full, center, size = 190 }) {
  const [hover, setHover] = useState(null);
  const total = items.reduce((sum, item) => sum + item.value, 0);
  const radius = size / 2;
  const ring = 30;
  const arcs = useMemo(() => {
    let angle = -Math.PI / 2;
    const gap = items.length > 1 ? GAP / (radius - ring / 2) : 0;
    return items.filter((item) => item.value > 0).map((item) => {
      const sweep = total ? (item.value / total) * Math.PI * 2 : 0;
      const start = angle + gap / 2;
      const end = angle + sweep - gap / 2;
      angle += sweep;
      return { item, start, end: Math.max(end, start + 0.0001) };
    });
  }, [items, total, size]);
  const arcPath = (start, end, outer, inner) => {
    const large = end - start > Math.PI ? 1 : 0;
    const point = (r, a) => `${(radius + r * Math.cos(a)).toFixed(2)},${(radius + r * Math.sin(a)).toFixed(2)}`;
    if (end - start >= Math.PI * 2 - 0.001) {
      return `M${point(outer, start)}A${outer},${outer} 0 1 1 ${point(outer, start + Math.PI)}A${outer},${outer} 0 1 1 ${point(outer, start)}M${point(inner, start)}A${inner},${inner} 0 1 0 ${point(inner, start + Math.PI)}A${inner},${inner} 0 1 0 ${point(inner, start)}Z`;
    }
    return `M${point(outer, start)}A${outer},${outer} 0 ${large} 1 ${point(outer, end)}L${point(inner, end)}A${inner},${inner} 0 ${large} 0 ${point(inner, start)}Z`;
  };
  const shown = hover === null ? null : arcs[hover];
  return html`<div class="viz-donut">
    <svg width=${size} height=${size} viewBox=${`0 0 ${size} ${size}`} role="img" aria-label=${`${items.length} parts, ${format(total)} in all`} onPointerLeave=${() => setHover(null)}>
      ${!arcs.length && html`<circle cx=${radius} cy=${radius} r=${radius - ring / 2} fill="none" stroke="var(--viz-grid)" stroke-width=${ring} />`}
      ${arcs.map((arc, index) => html`<path key=${arc.item.key} d=${arcPath(arc.start, arc.end, radius - (hover === index ? 0 : 3), radius - ring)} fill=${slotColor(arc.item.slot)}
        tabindex="0" role="img" aria-label=${`${arc.item.name}: ${format(arc.item.value)}, ${share(arc.item.value, total)}`}
        onPointerEnter=${() => setHover(index)} onFocus=${() => setHover(index)} onBlur=${() => setHover(null)} />`)}
      <text x=${radius} y=${radius - 2} text-anchor="middle" class="viz-donut-total">${shown ? format(shown.item.value) : (center || compact(total))}</text>
      <text x=${radius} y=${radius + 16} text-anchor="middle" class="viz-donut-label">${shown ? share(shown.item.value, total) : 'in all'}</text>
    </svg>
    <ol class="viz-donut-list">${items.map((item, index) => html`<li key=${item.key} class=${hover === index ? 'on' : ''} onPointerEnter=${() => setHover(index)} onPointerLeave=${() => setHover(null)}>
      <span class="viz-key box" style=${{ background: slotColor(item.slot) }}></span><span class="viz-donut-name" title=${item.title || item.name}>${item.name}</span>
      <b>${format(item.value)}</b><span class="dim">${share(item.value, total)}</span></li>`)}</ol>
  </div>`;
}
export const share = (value, total) => (total ? `${(Math.round((value / total) * 1000) / 10).toFixed(1)}%` : '0%');

// The top `count` of ranked rows as donut items, the rest as one "Other" slice.
export function topSlices(rows, count, { key = (row) => row.id, name = (row) => row.name, value = (row) => row.value, slots } = {}) {
  const head = rows.slice(0, count);
  const rest = rows.slice(count).reduce((sum, row) => sum + value(row), 0);
  const items = head.map((row, index) => ({ key: String(key(row)), name: name(row), value: value(row), slot: slots ? slots(key(row)) : index, title: row.title }));
  if (rest > 0) items.push({ key: 'other', name: `Other (${rows.length - count})`, value: rest, slot: OTHER });
  return items;
}

// ---- hour by weekday ----
const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
export const hourName = (hour) => `${hour % 12 || 12} ${hour < 12 ? 'AM' : 'PM'}`;
export function Heatmap({ cells, format = full, what = 'on average' }) {
  const [box, width] = useWidth();
  const [hover, setHover] = useState(null);
  const largest = Math.max(0, ...cells.flat());
  const steps = 7;
  const step = (value) => (largest > 0 && value > 0 ? Math.min(steps, Math.max(1, Math.ceil((value / largest) * steps))) : 0);
  const left = 44;
  const cell = Math.max(Math.floor((width - left - 4) / 24), 10);
  const tall = Math.min(cell, 26);
  const total = tall * 7 + 26;
  const onKey = (event) => {
    const moves = { ArrowRight: [0, 1], ArrowLeft: [0, -1], ArrowDown: [1, 0], ArrowUp: [-1, 0] };
    const move = moves[event.key];
    if (!move) return;
    event.preventDefault();
    setHover((at) => {
      const [row, hour] = at || [0, -1];
      return [Math.min(Math.max(row + move[0], 0), 6), Math.min(Math.max(hour + move[1], 0), 23)];
    });
  };
  return html`<div class="viz-heat" ref=${box}>
    <svg class="viz-stage" tabindex="0" width=${left + cell * 24 + 4} height=${total} role="img" aria-label="Average for each hour of each weekday. Use the arrow keys to read each hour."
      onKeyDown=${onKey} onPointerLeave=${() => setHover(null)}>
      ${DAYS.map((day, row) => html`<text key=${day} x=${left - 8} y=${row * tall + tall / 2 + 4} text-anchor="end" class="viz-tick">${day.slice(0, 3)}</text>`)}
      ${cells.map((hours, row) => hours.map((value, hour) => html`<rect key=${row + '-' + hour} x=${left + hour * cell + 1} y=${row * tall + 1} width=${cell - 2} height=${tall - 2} rx="2"
        fill=${`var(--seq-${step(value)})`} class=${hover && hover[0] === row && hover[1] === hour ? 'viz-cell on' : 'viz-cell'}
        onPointerEnter=${() => setHover([row, hour])} />`))}
      ${[0, 6, 12, 18].map((hour) => html`<text key=${hour} x=${left + hour * cell + cell / 2} y=${7 * tall + 16} text-anchor="middle" class="viz-tick">${hourName(hour)}</text>`)}
    </svg>
    <div class="viz-heat-foot">
      <span class="viz-heat-read" aria-live="polite">${hover ? html`<b>${format(cells[hover[0]][hover[1]])}</b> ${what}, ${DAYS[hover[0]]} at ${hourName(hover[1])}` : 'Point at a square to read it.'}</span>
      <span class="viz-scale"><span>${format(0)}</span>${[1, 2, 3, 4, 5, 6, 7].map((n) => html`<i key=${n} style=${{ background: `var(--seq-${n})` }}></i>`)}<span>${format(largest)}</span></span>
    </div>
  </div>`;
}
export const heatTable = (cells, format = full) => ({
  columns: ['Day', ...Array.from({ length: 24 }, (_, hour) => hourName(hour))],
  rows: cells.map((hours, row) => [DAYS[row], ...hours.map((value) => format(value))]),
});

// ---- small figures ----
export function Sparkline({ values, width = 120, height = 30 }) {
  const known = values.filter((value) => value !== null && value !== undefined);
  if (known.length < 2) return null;
  const largest = Math.max(...known);
  const smallest = Math.min(...known);
  const spread = largest - smallest || 1;
  const x = (index) => 2 + (index / (values.length - 1)) * (width - 6);
  const y = (value) => height - 3 - ((value - smallest) / spread) * (height - 6);
  const points = values.map((value, index) => (value === null || value === undefined ? null : [x(index), y(value)]));
  const last = points.filter(Boolean).pop();
  return html`<svg class="viz-spark" width=${width} height=${height} aria-hidden="true">
    <path d=${linePath(points, false)} fill="none" stroke="var(--viz-muted)" stroke-width="1.5" stroke-linejoin="round" />
    <circle cx=${last[0]} cy=${last[1]} r="3" fill="var(--series-1)" />
  </svg>`;
}

// ---- the frame every chart sits in ----
// Whether this reader may download CSV files. The server says so (can_csv): every CSV download takes the role that opens drilldowns.
export const Downloads = createContext({ csv: true });

// table() gives { columns, rows } for both the table view and the CSV. image is true for a chart that can be downloaded as a picture.
export function ChartFrame({ title, subtitle, table, file, image = true, tools, children, busy = false }) {
  const [asTable, setAsTable] = useState(false);
  const { csv } = useContext(Downloads);
  const body = useRef(null);
  const data = asTable && table ? table() : null;
  const name = (file || title || 'chart').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  return html`<figure class=${'viz-frame' + (busy ? ' busy' : '')}>
    <figcaption class="viz-head">
      <div><div class="viz-title">${title}</div>${subtitle && html`<div class="viz-sub">${subtitle}</div>`}</div>
      <div class="viz-tools">
        ${tools}
        ${table && html`<button type="button" class=${'btn small ghost' + (asTable ? ' on' : '')} aria-pressed=${asTable} onClick=${() => setAsTable(!asTable)} title="Show the numbers as a table"><${Icon} name="list" size=${14} /> Table</button>`}
        ${table && csv && html`<button type="button" class="btn small ghost" onClick=${() => { const t = table(); downloadCsv(name, t.columns, t.rows); }} title="Download the numbers as a CSV file"><${Icon} name="download" size=${14} /> CSV</button>`}
        ${image && !asTable && html`<button type="button" class="btn small ghost" onClick=${() => downloadImage(body.current, name, title, subtitle)} title="Download the chart as a picture"><${Icon} name="image" size=${14} /> Image</button>`}
      </div>
    </figcaption>
    <div class="viz-body" ref=${body}>${data ? html`<${ChartTable} columns=${data.columns} rows=${data.rows} />` : children}</div>
  </figure>`;
}

export function ChartTable({ columns, rows }) {
  return html`<div class="table-wrap viz-table"><table>
    <thead><tr>${columns.map((column) => html`<th key=${column}>${column}</th>`)}</tr></thead>
    <tbody>${rows.map((row, index) => html`<tr key=${index}>${row.map((cell, at) => html`<td key=${at}>${cell === null || cell === undefined ? '' : String(cell)}</td>`)}</tr>`)}</tbody>
  </table></div>`;
}

// ---- downloads ----
const csvCell = (value) => {
  if (value === null || value === undefined) return '';
  const text = String(value);
  // OWASP's rule: text that starts with =, +, -, @, a tab or a carriage return gets a leading apostrophe, so a spreadsheet reads it as
  // text and runs nothing. A number is written as it is, minus sign and all.
  const safe = typeof value !== 'number' && /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
  return /[",\n\r]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
};
export function toCsv(columns, rows) {
  return [columns, ...rows].map((row) => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
}
export function downloadCsv(name, columns, rows) {
  saveBlob(new Blob(['\uFEFF' + toCsv(columns, rows)], { type: 'text/csv;charset=utf-8' }), `${name}.csv`);
}
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

// A chart as an SVG picture: its drawing with the theme's colors written in, under its title, with its legend, and for a heatmap its
// color scale, the seven steps between the numbers at each end.
export function pictureOf(container, title, subtitle) {
  const svg = container && container.querySelector('svg.viz-svg, .viz-heat svg, .viz-donut svg');
  if (!svg) return null;
  const styles = getComputedStyle(svg);
  const resolve = (text) => text.replace(/var\((--[a-z0-9-]+)\)/g, (_, token) => styles.getPropertyValue(token).trim() || '#888');
  const width = Number(svg.getAttribute('width'));
  const height = Number(svg.getAttribute('height'));
  const legend = [...container.querySelectorAll('.viz-legend-item:not(.off), .viz-donut-list li')].map((item) => ({
    color: getComputedStyle(item.querySelector('.viz-key')).backgroundColor, text: item.textContent.trim().replace(/\s+/g, ' '),
  }));
  const scaleBox = container.querySelector('.viz-scale');
  const scale = scaleBox ? {
    ends: [...scaleBox.querySelectorAll('span')].map((node) => node.textContent.trim()),
    steps: [...scaleBox.querySelectorAll('i')].map((node) => getComputedStyle(node).backgroundColor),
  } : null;
  const ink = styles.getPropertyValue('--viz-ink').trim();
  const muted = styles.getPropertyValue('--viz-ink-2').trim();
  const surface = styles.getPropertyValue('--viz-surface').trim();
  const head = 56;
  const rowsOfLegend = Math.ceil(legend.length / 3);
  const legendHeight = legend.length ? rowsOfLegend * 22 + 12 : 0;
  const scaleHeight = scale ? 30 : 0;
  const foot = Math.max(legendHeight + scaleHeight, 8);
  const copy = svg.cloneNode(true);
  copy.querySelectorAll('.viz-hover').forEach((node) => node.remove());  // the crosshair and its dots belong to the pointer, not the picture
  const inner = resolve(new XMLSerializer().serializeToString(copy)).replace(/class="viz-tick"/g, `font-size="11" fill="${muted}"`)
    .replace(/class="viz-panel-title"/g, `font-size="12" font-weight="600" fill="${ink}"`).replace(/class="viz-ref"/g, `font-size="11" fill="${muted}"`)
    .replace(/class="viz-end"/g, `font-size="11" fill="${muted}"`).replace(/class="viz-donut-total"/g, `font-size="20" font-weight="600" fill="${ink}"`)
    .replace(/class="viz-donut-label"/g, `font-size="11" fill="${muted}"`);
  const escape = (text) => String(text || '').replace(/[<>&"]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));
  const legendSvg = legend.map((item, index) => {
    const x = 16 + (index % 3) * Math.floor((width - 16) / 3);
    const y = head + height + 18 + Math.floor(index / 3) * 22;
    return `<rect x="${x}" y="${y - 9}" width="10" height="10" rx="2" fill="${item.color}"/><text x="${x + 16}" y="${y}" font-size="12" fill="${muted}">${escape(item.text.slice(0, 48))}</text>`;
  }).join('');
  let scaleSvg = '';
  if (scale) {
    const y = head + height + legendHeight + 18;
    const low = escape(scale.ends[0] || '');
    const high = escape(scale.ends[scale.ends.length - 1] || '');
    const start = 16 + low.length * 7 + 8;
    scaleSvg = `<text x="16" y="${y}" font-size="11" fill="${muted}">${low}</text>`
      + scale.steps.map((color, index) => `<rect class="viz-scale-step" x="${start + index * 20}" y="${y - 9}" width="18" height="10" rx="2" fill="${color}"/>`).join('')
      + `<text x="${start + scale.steps.length * 20 + 6}" y="${y}" font-size="11" fill="${muted}">${high}</text>`;
  }
  const full = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${head + height + foot}" font-family="Segoe UI, Roboto, Arial, sans-serif">`
    + `<rect width="100%" height="100%" fill="${surface}"/><text x="16" y="26" font-size="16" font-weight="600" fill="${ink}">${escape(title)}</text>`
    + `<text x="16" y="44" font-size="12" fill="${muted}">${escape(subtitle)}</text><g transform="translate(0, ${head})">${inner}</g>${legendSvg}${scaleSvg}</svg>`;
  return { svg: full, width, height: head + height + foot };
}

// A chart as a PNG, at twice the size for sharp text.
export async function downloadImage(container, name, title, subtitle) {
  const picture = pictureOf(container, title, subtitle);
  if (!picture) return;
  const url = URL.createObjectURL(new Blob([picture.svg], { type: 'image/svg+xml' }));
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = picture.width * 2;
    canvas.height = picture.height * 2;
    const pen = canvas.getContext('2d');
    pen.scale(2, 2);
    pen.drawImage(image, 0, 0);
    await new Promise((done) => canvas.toBlob((blob) => { if (blob) saveBlob(blob, `${name}.png`); done(); }, 'image/png'));
  } finally {
    URL.revokeObjectURL(url);
  }
}
