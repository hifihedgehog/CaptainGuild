// A table for tens of thousands of rows: it draws only the rows in view, sorts by any column, searches any field by a condition
// (IDs and other fields the table doesn't show included), and downloads what it shows as CSV.
import { html, useContext, useEffect, useMemo, useRef, useState } from '../lib.js';
import { Icon } from '../ui.js';
import { Downloads, downloadCsv } from '../charts.js';

export const OPERATORS = [
  ['contains', 'contains'], ['not_contains', "doesn't contain"], ['lt', 'is less than'], ['gt', 'is greater than'],
  ['eq', 'equals'], ['ne', "doesn't equal"], ['is', 'is'], ['is_not', 'is not'],
];
const ROW = 38;
const SHOWN = 12;
const OVERSCAN = 8;

const rawOf = (column, row) => (column.value ? column.value(row) : row[column.key]);
const textOf = (column, row) => {
  const value = column.text ? column.text(row) : rawOf(column, row);
  return value === null || value === undefined ? '' : String(value);
};
const asNumber = (text) => {
  const cleaned = String(text).replace(/,/g, '').trim();
  return cleaned !== '' && !Number.isNaN(Number(cleaned)) ? Number(cleaned) : null;
};
// A Discord ID runs past 2^53, where two IDs can read as one number, so whole numbers that long compare exactly, as text or as BigInt.
const WHOLE = /^\d+$/;
const long = (a, b) => (WHOLE.test(a) && a.length > 15) || (WHOLE.test(b) && b.length > 15);
const order = (a, b) => (WHOLE.test(a) && WHOLE.test(b) ? (BigInt(a) < BigInt(b) ? -1 : BigInt(a) > BigInt(b) ? 1 : 0) : null);

// Whether a row meets one condition. Numbers compare as numbers, everything else as text without regard to case.
export function meets(condition, column, row) {
  const needle = String(condition.value || '').trim();
  const text = textOf(column, row);
  const raw = rawOf(column, row);
  const number = typeof raw === 'number' ? raw : asNumber(text);
  const wanted = asNumber(needle);
  const lower = text.toLowerCase();
  const sought = needle.toLowerCase();
  const exact = long(text.trim(), needle) ? order(text.trim(), needle) : null;
  switch (condition.op) {
    case 'contains': return lower.includes(sought);
    case 'not_contains': return !lower.includes(sought);
    case 'lt': return exact !== null ? exact < 0 : number !== null && wanted !== null && number < wanted;
    case 'gt': return exact !== null ? exact > 0 : number !== null && wanted !== null && number > wanted;
    case 'eq': return exact !== null ? exact === 0 : number !== null && wanted !== null ? number === wanted : lower === sought;
    case 'ne': return exact !== null ? exact !== 0 : number !== null && wanted !== null ? number !== wanted : lower !== sought;
    case 'is': return lower === sought || String(raw ?? '') === needle;
    case 'is_not': return lower !== sought && String(raw ?? '') !== needle;
    default: return true;
  }
}

function compare(a, b) {
  if (a === b) return 0;
  if (a === null || a === undefined || a === '') return 1;
  if (b === null || b === undefined || b === '') return -1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  if (typeof a === 'boolean' || typeof b === 'boolean') return Number(b) - Number(a);
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
}

// columns: [{ key, label, value(row), text(row), render(row), numeric, hidden, width, csv(row) }]. hidden columns are searchable and in the CSV.
export function StatTable({ columns, rows, rowKey = (row) => row.id, file = 'table', empty = 'Nothing in this period.', selectable = false, selected, onSelect,
  initialSort = null, tools, busy = false, label = 'Table' }) {
  const [sort, setSort] = useState(initialSort);
  const [conditions, setConditions] = useState([]);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState({ field: columns[1] ? columns[1].key : columns[0].key, op: 'contains', value: '' });
  const [top, setTop] = useState(0);
  const { csv: mayDownload } = useContext(Downloads);
  const scroller = useRef(null);
  const byKey = useMemo(() => Object.fromEntries(columns.map((column) => [column.key, column])), [columns]);
  const shownColumns = columns.filter((column) => !column.hidden);

  const visible = useMemo(() => {
    let list = rows || [];
    for (const condition of conditions) {
      const column = byKey[condition.field];
      if (column) list = list.filter((row) => meets(condition, column, row));
    }
    if (sort && byKey[sort.key]) {
      const column = byKey[sort.key];
      list = [...list].sort((a, b) => {
        const order = compare(rawOf(column, a), rawOf(column, b));
        return sort.down ? -order : order;
      });
    }
    return list;
  }, [rows, conditions, sort, byKey]);

  useEffect(() => { if (scroller.current) scroller.current.scrollTop = 0; setTop(0); }, [conditions, sort, rows]);

  const first = Math.max(0, Math.floor(top / ROW) - OVERSCAN);
  const last = Math.min(visible.length, Math.ceil((top + SHOWN * ROW) / ROW) + OVERSCAN);
  const slice = visible.slice(first, last);
  const flip = (column) => setSort((now) => (now && now.key === column.key ? { key: column.key, down: !now.down } : { key: column.key, down: !!column.numeric }));
  const add = (event) => {
    event.preventDefault();
    if (!String(draft.value).trim()) return;
    setConditions([...conditions, draft]);
    setDraft({ ...draft, value: '' });
  };
  // A number goes in as a number, so a negative one isn't taken for a formula and a spreadsheet can add it up.
  const cellOf = (column, row) => {
    if (column.csv) return column.csv(row);
    const raw = rawOf(column, row);
    return column.numeric && typeof raw === 'number' ? raw : textOf(column, row);
  };
  const csv = () => downloadCsv(file, columns.map((column) => column.label), visible.map((row) => columns.map((column) => cellOf(column, row))));
  const allOn = selectable && visible.length > 0 && visible.every((row) => selected.has(String(rowKey(row))));
  const toggleAll = () => {
    if (!onSelect) return;
    const next = new Set(selected);
    if (allOn) visible.forEach((row) => next.delete(String(rowKey(row))));
    else visible.slice(0, 25).forEach((row) => next.add(String(rowKey(row))));
    onSelect(next);
  };
  const toggle = (row) => {
    const key = String(rowKey(row));
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onSelect(next);
  };
  const width = selectable ? 1 : 0;
  return html`<div class=${'stats-table' + (busy ? ' busy' : '')}>
    <div class="table-tools">
      <button type="button" class=${'btn small ghost' + (open ? ' on' : '')} aria-expanded=${open} onClick=${() => setOpen(!open)}><${Icon} name="search" size=${14} /> Search${conditions.length ? ` (${conditions.length})` : ''}</button>
      ${mayDownload && html`<button type="button" class="btn small ghost" onClick=${csv} title="Download these rows as a CSV file"><${Icon} name="download" size=${14} /> CSV</button>`}
      ${tools}
      <span class="muted small right">${visible.length === (rows || []).length ? `${visible.length.toLocaleString()} rows` : `${visible.length.toLocaleString()} of ${(rows || []).length.toLocaleString()} rows`}</span>
    </div>
    ${open && html`<form class="stats-search" onSubmit=${add} aria-label=${'Search ' + label}>
      <select aria-label="Field" value=${draft.field} onChange=${(e) => setDraft({ ...draft, field: e.currentTarget.value })}>
        ${columns.map((column) => html`<option key=${column.key} value=${column.key}>${column.label}</option>`)}</select>
      <select aria-label="Condition" value=${draft.op} onChange=${(e) => setDraft({ ...draft, op: e.currentTarget.value })}>
        ${OPERATORS.map(([key, text]) => html`<option key=${key} value=${key}>${text}</option>`)}</select>
      <input type="text" aria-label="Value" placeholder="Value" value=${draft.value} onInput=${(e) => setDraft({ ...draft, value: e.currentTarget.value })} />
      <button type="submit" class="btn small primary">Add</button>
      ${conditions.length > 0 && html`<div class="stats-conditions">${conditions.map((condition, index) => html`<span class="chip" key=${index}>
        ${(byKey[condition.field] || {}).label} ${OPERATORS.find(([key]) => key === condition.op)[1]} "${condition.value}"
        <button type="button" aria-label="Remove this condition" onClick=${() => setConditions(conditions.filter((_, at) => at !== index))}><${Icon} name="x" size=${12} /></button></span>`)}
        <button type="button" class="link-btn small" onClick=${() => setConditions([])}>Clear all</button></div>`}
    </form>`}
    <div class="table-wrap stats-scroll" ref=${scroller} style=${{ maxHeight: `${(SHOWN + 1) * ROW + 2}px` }} onScroll=${(e) => setTop(e.currentTarget.scrollTop)}>
      <table class="stats-grid" aria-rowcount=${visible.length}>
        <thead><tr>
          ${selectable && html`<th class="stats-check"><input type="checkbox" aria-label="Select the first 25 rows shown" checked=${allOn} onChange=${toggleAll} /></th>`}
          ${shownColumns.map((column) => html`<th key=${column.key} style=${column.width ? { width: column.width } : null} class=${column.numeric ? 'num' : ''}
            aria-sort=${sort && sort.key === column.key ? (sort.down ? 'descending' : 'ascending') : 'none'}>
            <button type="button" class="th-sort" onClick=${() => flip(column)}>${column.label}${sort && sort.key === column.key ? (sort.down ? ' ↓' : ' ↑') : ''}</button></th>`)}
        </tr></thead>
        <tbody>
          ${visible.length === 0 && html`<tr><td colspan=${shownColumns.length + width}><div class="empty">${empty}</div></td></tr>`}
          ${first > 0 && html`<tr aria-hidden="true" style=${{ height: `${first * ROW}px` }}><td colspan=${shownColumns.length + width}></td></tr>`}
          ${slice.map((row) => {
            const key = String(rowKey(row));
            return html`<tr key=${key} class=${selectable && selected.has(key) ? 'on' : ''}>
              ${selectable && html`<td class="stats-check"><input type="checkbox" aria-label="Select this row" checked=${selected.has(key)} onChange=${() => toggle(row)} /></td>`}
              ${shownColumns.map((column) => html`<td key=${column.key} class=${column.numeric ? 'num' : ''}>${column.render ? column.render(row) : textOf(column, row)}</td>`)}
            </tr>`;
          })}
          ${last < visible.length && html`<tr aria-hidden="true" style=${{ height: `${(visible.length - last) * ROW}px` }}><td colspan=${shownColumns.length + width}></td></tr>`}
        </tbody>
      </table>
    </div>
  </div>`;
}

// Rows the server sends as columns and arrays, as objects keyed by column.
export const unpack = (data) => (data ? data.rows.map((row) => Object.fromEntries(data.columns.map((name, index) => [name, row[index]]))) : []);
