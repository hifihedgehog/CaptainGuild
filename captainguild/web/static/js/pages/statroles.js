// Stat roles: a role for the members whose statistics pass a rule, given and taken on a schedule.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { MessageEditor, MessagePreview, sampleMessage } from '../embed.js';
import {
  Card, ChannelPicker, Check, Field, Icon, Loading, MemberPicker, Modal, Notice, NumberInput, PeopleField, RolePicker, Select, Switch, Tabs, TextInput,
  confirmDialog, sameId, toast, toastError, useGuild, useSettings, when,
} from '../ui.js';
import { Checks, PeriodInput, StatFilters, VOICE_STATES, blankStat, useChoices } from './counters.js';

const LEAF_KIND = { message: 'messages', voice: 'voice', activity: 'activities', invite: 'invites', emoji: 'emoji' };
const TYPES = [
  { value: 'message', label: 'Messages' }, { value: 'voice', label: 'Voice minutes' }, { value: 'activity', label: 'Minutes in apps' },
  { value: 'invite', label: 'People invited' }, { value: 'emoji', label: 'Emoji' }, { value: 'joined', label: 'Time since joining' },
  { value: 'account', label: 'Account age' }, { value: 'compare', label: 'Compare statistics' }, { value: 'statrole', label: 'Another stat role' },
  { value: 'match', label: 'A group of conditions' },
];
const UNIT = { message: 'messages', voice: 'minutes', activity: 'minutes', invite: 'people', emoji: 'uses' };
const SEND = [{ value: 'channel', label: 'In a channel or thread' }, { value: 'dm', label: 'By DM' }, { value: 'dm_fallback', label: 'By DM, or in a channel when DMs are closed' }];
const DEFAULT_TEXT = { give: '{mention} now has the {role(name)} role.', remove: '{mention} no longer has the {role(name)} role.' };
const MAX_NODES = 50;
const MAX_DEPTH = 6;  // the server's limit on how deep groups go, the rule's own group counting as the first

const leafStat = (type) => {
  const stat = blankStat(LEAF_KIND[type]);
  for (const key of ['include', 'member_mode', 'members', 'roles_allow', 'roles_block', 'period']) delete stat[key];
  return stat;
};

function blankNode(type) {
  const people = { member_mode: 'block', members: [], roles_allow: [], roles_block: [] };
  if (type === 'match') return { type, mode: 'all', invert: false, conditions: [blankNode('message')] };
  if (type === 'statrole') return { type, rule: null, invert: false };
  if (type === 'joined' || type === 'account') return { type, invert: false, compare: 'more', amount: 30, unit: 'days', ...people };
  if (type === 'compare') return { type, invert: false, operator: 'greater', left: [blankItem()], right: [], ...people };
  return { type, invert: false, ...people, stat: leafStat(type), limit: { kind: 'threshold', min: type === 'voice' ? 60 : 10, max: null, period: { amount: 30, unit: 'days' } } };
}

function blankItem() {
  return { kind: 'message', weight: 1, subtract: false, stat: { ...leafStat('message'), period: { amount: 30, unit: 'days' } } };
}

const blankRule = () => ({
  name: '', role_id: null, enabled: true, conditions: { type: 'match', mode: 'all', invert: false, conditions: [blankNode('message')] },
  apply_to: 'humans', ignore: [], permanent: false, schedule: { preset: '3h', cron: '0 */3 * * *' }, give: null, remove: null,
});

function countNodes(node) {
  return 1 + (node.type === 'match' ? node.conditions.reduce((sum, child) => sum + countNodes(child), 0) : 0);
}

function LimitEditor({ type, limit, onChange }) {
  const set = (key, value) => onChange({ ...limit, [key]: value });
  const unit = UNIT[type];
  const switchKind = (kind) => {
    if (kind === 'threshold') onChange({ kind, min: 1, max: null, period: limit.period || { amount: 30, unit: 'days' } });
    else if (kind === 'top') onChange({ kind, by: 'rank', from: 1, to: 10, matching_only: false, period: limit.period || { amount: 30, unit: 'days' } });
    else onChange({ kind, per_day: 1, days: 7, of: 7 });
  };
  return html`<div class="field-edit">
    <${Select} label="Limit" value=${limit.kind} options=${[{ value: 'threshold', label: 'At least an amount' }, { value: 'top', label: 'A place in the ranking' }, { value: 'days', label: 'An amount on enough days' }]} onChange=${switchKind} />
    ${limit.kind === 'threshold' && html`<div class="input-row">
      <${NumberInput} label=${'At least (' + unit + ')'} min=${0} value=${limit.min} onChange=${(v) => set('min', v || 0)} />
      <${NumberInput} label="At most (optional)" min=${0} value=${limit.max} onChange=${(v) => set('max', v)} />
    </div>`}
    ${limit.kind === 'threshold' && limit.min === 0 && html`<p class="small dim">A minimum of 0 needs a maximum, such as at most 0 for members with nothing at all.</p>`}
    ${limit.kind === 'top' && html`<div>
      <${Select} label="Rank by" value=${limit.by} options=${[{ value: 'rank', label: 'Place' }, { value: 'percent', label: 'Percentile' }]} onChange=${(by) => onChange({ ...limit, by, from: by === 'rank' ? 1 : 0, to: 10 })} />
      <div class="input-row">
        <${NumberInput} label=${limit.by === 'rank' ? 'From place' : 'From the top percent'} min=${limit.by === 'rank' ? 1 : 0} value=${limit.from} onChange=${(v) => set('from', v)} />
        <${NumberInput} label=${limit.by === 'rank' ? 'To place' : 'To percent'} min=${1} value=${limit.to} onChange=${(v) => set('to', v)} />
      </div>
      <${Switch} label="Rank only members who pass this condition's filters" value=${limit.matching_only} onChange=${(v) => set('matching_only', v)} help="Off, places and percentiles count the whole server." />
    </div>`}
    ${limit.kind !== 'days' && html`<${PeriodInput} label="Over" value=${limit.period} onChange=${(v) => set('period', v)} />`}
    ${limit.kind === 'days' && html`<div class="input-row">
      <${NumberInput} label=${'At least (' + unit + ') a day'} min=${1} value=${limit.per_day} onChange=${(v) => set('per_day', v || 1)} />
      <${NumberInput} label="On this many days" min=${1} value=${limit.days} onChange=${(v) => set('days', v || 1)} />
      <${NumberInput} label="Of the last days" min=${1} max=${366} value=${limit.of} onChange=${(v) => set('of', v || 1)} />
    </div>`}
  </div>`;
}

function PeopleFilters({ node, onChange }) {
  const set = (key, value) => onChange({ ...node, [key]: value });
  return html`<details><summary class="small">Members and roles it applies to</summary>
    <${Select} label="Listed members" value=${node.member_mode} options=${[{ value: 'block', label: 'Always fail this condition' }, { value: 'allow', label: 'Are the only ones who can pass it' }]} onChange=${(v) => set('member_mode', v)} />
    <${PeopleField} label=${node.member_mode === 'allow' ? 'Only these members can pass' : 'These members always fail'} value=${node.members} onChange=${(v) => set('members', v)} />
    <${RolePicker} label="Members need one of these roles to pass" multi value=${node.roles_allow} onChange=${(v) => set('roles_allow', v)} />
    <${RolePicker} label="Members with any of these roles fail" multi value=${node.roles_block} onChange=${(v) => set('roles_block', v)} />
  </details>`;
}

function CompareItems({ label, items, onChange, choices }) {
  const setItem = (index, next) => onChange(items.map((item, i) => (i === index ? next : item)));
  return html`<${Field} label=${label}>
    ${items.map((item, index) => html`<div class="field-edit" key=${index}>
      <div class="spread"><b>${index + 1}.</b><button class="icon-btn danger" aria-label="Remove" onClick=${() => onChange(items.filter((_, i) => i !== index))}><${Icon} name="trash-2" /></button></div>
      <div class="input-row">
        <${Select} label="Statistic" value=${item.kind} options=${TYPES.slice(0, 3)} onChange=${(kind) => setItem(index, { ...item, kind, stat: { ...leafStat(kind), period: item.stat.period } })} />
        <${NumberInput} label="Weight" step=${0.1} value=${item.weight} onChange=${(v) => setItem(index, { ...item, weight: v === null ? 1 : v })} />
      </div>
      <${Check} label="Subtract it" value=${item.subtract} onChange=${(v) => setItem(index, { ...item, subtract: v })} />
      <${PeriodInput} label="Over" value=${item.stat.period} onChange=${(v) => setItem(index, { ...item, stat: { ...item.stat, period: v } })} />
      <${StatFilters} stat=${item.stat} people=${false} choices=${choices} onChange=${(v) => setItem(index, { ...item, stat: { ...v, period: item.stat.period } })} />
    </div>`)}
    <button type="button" class="btn small" disabled=${items.length >= 10} onClick=${() => onChange([...items, blankItem()])}><${Icon} name="plus" size=${14} /> Add a statistic</button>
  <//>`;
}

function ConditionEditor({ node, onChange, onRemove, depth, others, choices, total }) {
  const set = (key, value) => onChange({ ...node, [key]: value });
  const type = node.type;
  return html`<div class="field-edit" style=${{ marginLeft: depth ? '10px' : 0 }}>
    <div class="spread">
      <div style=${{ minWidth: '220px', flex: 1 }}><${Select} ariaLabel="Condition type" value=${type} options=${TYPES} onChange=${(next) => onChange(blankNode(next))} /></div>
      ${onRemove && html`<button class="icon-btn danger" aria-label="Remove condition" onClick=${onRemove}><${Icon} name="trash-2" /></button>`}
    </div>
    ${type === 'match' && html`<div>
      <${Select} label="Passes when" value=${node.mode} options=${[{ value: 'all', label: 'All of these pass' }, { value: 'any', label: 'Any of these passes' }]} onChange=${(v) => set('mode', v)} />
      ${node.conditions.map((child, index) => html`<${ConditionEditor} key=${index} node=${child} depth=${depth + 1} others=${others} choices=${choices} total=${total}
        onChange=${(next) => set('conditions', node.conditions.map((c, i) => (i === index ? next : c)))}
        onRemove=${node.conditions.length > 1 ? () => set('conditions', node.conditions.filter((_, i) => i !== index)) : null} />`)}
      <div class="row">
        <button type="button" class="btn small" disabled=${total >= MAX_NODES} onClick=${() => set('conditions', [...node.conditions, blankNode('message')])}><${Icon} name="plus" size=${14} /> Condition</button>
        <button type="button" class="btn small" disabled=${total >= MAX_NODES - 1 || depth >= MAX_DEPTH - 2} onClick=${() => set('conditions', [...node.conditions, blankNode('match')])}><${Icon} name="plus" size=${14} /> Group</button>
      </div>
    </div>`}
    ${type === 'statrole' && html`<${Select} label="Passes for whoever passes" value=${node.rule} options=${others.map((rule) => ({ value: rule.number, label: rule.name }))} onChange=${(v) => set('rule', v)} />`}
    ${(type === 'joined' || type === 'account') && html`<div>
      <div class="small muted">An age goes in a group that matches all of its conditions, beside one on messages, voice, apps, invites, emoji, a comparison or another stat role.</div>
      <${Select} label=${type === 'joined' ? 'Joined' : 'Account made'} value=${node.compare} options=${[{ value: 'more', label: 'More than' }, { value: 'less', label: 'Less than' }, { value: 'before', label: 'Before a date' }, { value: 'after', label: 'On or after a date' }]}
        onChange=${(compare) => onChange({ ...node, compare, ...(compare === 'before' || compare === 'after' ? { date: node.date || new Date().toISOString().slice(0, 10) } : { amount: node.amount || 30, unit: node.unit || 'days' }) })} />
      ${(node.compare === 'more' || node.compare === 'less') ? html`<div class="input-row">
          <${NumberInput} label="Amount" min=${0} value=${node.amount} onChange=${(v) => set('amount', v || 0)} />
          <${Select} label="Of" value=${node.unit} options=${[{ value: 'days', label: 'Days ago' }, { value: 'hours', label: 'Hours ago' }]} onChange=${(v) => set('unit', v)} />
        </div>`
        : html`<${TextInput} label="Date" type="date" value=${node.date} onChange=${(v) => set('date', v)} />`}
    </div>`}
    ${type === 'compare' && html`<div>
      <${CompareItems} label="First list" items=${node.left} choices=${choices} onChange=${(v) => set('left', v)} />
      <${Select} label="Passes when the first list's total is" value=${node.operator} options=${[{ value: 'greater', label: 'Greater than the second list’s' }, { value: 'less', label: 'Less than the second list’s' }]} onChange=${(v) => set('operator', v)} />
      <${CompareItems} label="Second list" items=${node.right} choices=${choices} onChange=${(v) => set('right', v)} />
      <p class="small dim">Voice and app time count in minutes. Each statistic is multiplied by its weight.</p>
    </div>`}
    ${type in LEAF_KIND && html`<div>
      ${type === 'emoji' && html`<${Select} label="Count" value=${node.stat.measure || 'used'} options=${[{ value: 'used', label: 'Emoji they used' }, { value: 'received', label: 'Reactions others put on their messages' }]} onChange=${(v) => set('stat', { ...node.stat, measure: v })} />`}
      <${LimitEditor} type=${type} limit=${node.limit} onChange=${(v) => set('limit', v)} />
      <details><summary class="small">What counts</summary><${StatFilters} stat=${node.stat} people=${false} choices=${choices} onChange=${(v) => set('stat', v)} /></details>
    </div>`}
    ${type !== 'match' && type !== 'statrole' && html`<${PeopleFilters} node=${node} onChange=${onChange} />`}
    <${Check} label="Flip the result: pass when this fails" value=${node.invert} onChange=${(v) => set('invert', v)} />
  </div>`;
}

// A notification's place when it's a thread, which the channel list doesn't hold, shown in the thread box as its ID.
function threadText(guild, id) {
  if (!id || guild.channels.some((c) => sameId(c.id, id))) return '';
  return String(id);
}

function NotificationEditor({ which, value, onChange, roleId }) {
  const guild = useGuild();
  const [busy, setBusy] = useState(false);
  const on = !!value;
  const note = value || { message: { content: DEFAULT_TEXT[which], embeds: [], buttons: [] }, send: 'channel', channel_id: null, suppress: false, group: false };
  const set = (key, next) => onChange({ ...note, [key]: next });
  const test = async () => {
    setBusy(true);
    try {
      await api.post('/statroles/test', { which, notification: note, role_id: roleId });
      toast(note.send === 'dm' ? 'Sent to your DMs.' : 'Sent.');
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const role = guild.roles.find((r) => sameId(r.id, roleId));
  return html`<${Card} icon=${which === 'give' ? 'user-plus' : 'user-minus'} title=${which === 'give' ? 'When a member gains the role' : 'When a member loses the role'}>
    <${Switch} label="Send a message" value=${on} onChange=${(v) => onChange(v ? note : null)} />
    ${on && html`<div>
      <${MessageEditor} value=${note.message} collapsed contentRows=${3} onChange=${(v) => set('message', v)}
        help="{mention} pings the member, {user} is their name, and {role(name)} the role's. A grouped message lists everyone in {members}, with {count} for how many." />
      <${Field} label="Preview with a sample member"><${MessagePreview} value=${sampleMessage(note.message, { mention: '@Sample Member', user: 'Sample Member', 'role(name)': role ? role.name : 'Role', members: '@Sample Member, @Another Member', count: '2', server: guild.name })} /><//>
      <${Select} label="Send it" value=${note.send} options=${SEND} onChange=${(v) => onChange({ ...note, send: v, group: v === 'channel' ? note.group : false })} />
      ${note.send !== 'dm' && html`<div>
        <${ChannelPicker} label=${note.send === 'channel' ? 'Channel' : 'Channel when DMs are closed'} types=${[0, 5, 2, 13]} value=${note.channel_id} onChange=${(v) => set('channel_id', v)} />
        <${TextInput} label="Or a thread or forum post: its link or ID" placeholder="https://discord.com/channels/..." value=${threadText(guild, note.channel_id)}
          onChange=${(v) => { const found = /(\d{17,20})\s*\/?\s*$/.exec(v.trim()); set('channel_id', found ? found[1] : null); }} />
      </div>`}
      <${Switch} label="Don't ping anyone" value=${note.suppress} onChange=${(v) => set('suppress', v)} help="Role pings, @everyone and @here never go out, whatever this says." />
      ${note.send === 'channel' && html`<${Switch} label="One message per run, listing everyone" value=${note.group} onChange=${(v) => set('group', v)} />`}
      <button type="button" class="btn outline" disabled=${busy || (note.send !== 'dm' && !note.channel_id)} onClick=${test}><${Icon} name="send" size=${14} /> Send a test</button>
      <span class="small dim" style=${{ marginLeft: '8px' }}>The test is about you, as if you had just ${which === 'give' ? 'gained' : 'lost'} the role.</span>
    </div>`}
  <//>`;
}

function ResultTree({ entries }) {
  return html`<ul style=${{ listStyle: 'none', paddingLeft: '12px', margin: '4px 0' }}>${entries.map((entry, index) => html`<li key=${index}>
    <span class=${'badge ' + (entry.passed ? 'green' : 'red')}>${entry.passed ? 'passes' : 'fails'}</span> ${entry.text}${entry.value !== null && entry.value !== undefined ? html` <span class="dim">(${Number(entry.value).toLocaleString()})</span>` : ''}
    ${entry.note && html`<div class="small error">${entry.note}</div>`}
    ${entry.children && entry.children.length > 0 && html`<${ResultTree} entries=${entry.children} />`}
  </li>`)}</ul>`;
}

function PreviewPanel({ report, onEveryone, form, number }) {
  const [member, setMember] = useState(null);
  const [check, setCheck] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setCheck(null); }, [JSON.stringify(form)]);
  const ask = async (id) => {
    setMember(id);
    if (!id) { setCheck(null); return; }
    setBusy(true);
    try {
      setCheck(await api.post('/statroles/check', { ...form, number, member_id: id }));
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  if (!report) return html`<${Notice}>Press Preview to see who would gain and lose the role. Saving asks for a preview first.<//>`;
  const names = (rows, more) => html`${rows.map((row) => row.name).join(', ')}${more > rows.length ? ` and ${(more - rows.length).toLocaleString()} more` : ''}`;
  return html`<div>
    <${Notice} kind=${report.gain || report.lose ? 'warn' : ''}>
      <div>${report.matching.toLocaleString()} ${report.matching === 1 ? 'member passes' : 'members pass'} now. The next run gives the role to ${report.gain.toLocaleString()} and takes it from ${report.lose.toLocaleString()}, at about one a second.</div>
      ${report.left_alone > 0 && html`<div class="small">${report.left_alone.toLocaleString()} chose privacy for their statistics, so stat roles leave their roles as they are.</div>`}
      ${report.notifications > 0 && html`<div class="small">That sends ${report.notifications.toLocaleString()} notification${report.notifications === 1 ? '' : 's'}.</div>`}
      ${report.gain_names.length > 0 && html`<div class="small muted">Gaining: ${names(report.gain_names, report.gain)}</div>`}
      ${report.lose_names.length > 0 && html`<div class="small muted">Losing: ${names(report.lose_names, report.lose)}</div>`}
      <div class="small dim">Worked out in ${report.seconds} seconds.</div>
    <//>
    ${report.everyone ? html`<${Field} label=${`Everyone who passes (${report.matching.toLocaleString()})`}><div class="small">${names(report.everyone, report.matching)}</div><//>`
      : html`<button type="button" class="btn small" onClick=${onEveryone}>List everyone who passes</button>`}
    <${MemberPicker} label="Check one member's results" value=${member} onChange=${ask} />
    ${busy && html`<${Loading} text="Checking..." />`}
    ${check && html`<div class="field-edit">
      <b>${check.member.name} ${check.passes ? 'passes' : 'does not pass'}.</b>
      ${!check.in_server && html`<div class="small muted">They aren't in the server now.</div>`}
      ${check.private && html`<div class="small muted">They chose privacy, so their statistics count under no name and stat roles leave them as they are.</div>`}
      <${ResultTree} entries=${check.conditions} />
    </div>`}
  </div>`;
}

function RuleModal({ start, number, data, onClose, onSaved }) {
  const guild = useGuild();
  const choices = useChoices();
  const [form, setForm] = useState(start);
  const [tab, setTab] = useState('conditions');
  const [report, setReport] = useState(null);
  const [problem, setProblem] = useState(null);
  const [busy, setBusy] = useState(false);
  const [first] = useState(() => JSON.stringify(start));
  const set = (key, value) => { setForm((current) => ({ ...current, [key]: value })); setReport(null); };
  const others = data.rules.filter((rule) => rule.number !== number);
  const total = countNodes(form.conditions);
  const preview = async (everyone = false) => {
    setBusy(true);
    setProblem(null);
    try {
      setReport(await api.post('/statroles/preview', { ...form, number, everyone }));
      setTab('preview');
    } catch (error) {
      setProblem((error.fields && (error.fields.rule || error.fields.name || error.fields.role_id || error.fields.give || error.fields.remove)) || error.message);
    } finally {
      setBusy(false);
    }
  };
  // The first press shows what the rule would do. The second saves it.
  const save = async () => {
    if (!report) { await preview(); return; }
    setBusy(true);
    try {
      if (number) await api.put(`/statroles/${number}`, form);
      else await api.post('/statroles', form);
      onSaved();
    } catch (error) {
      setProblem((error.fields && Object.values(error.fields)[0]) || error.message);
    } finally {
      setBusy(false);
    }
  };
  const schedule = form.schedule;
  return html`<${Modal} large icon="crown" title=${number ? `Edit ${start.name}` : 'New stat role'} onClose=${onClose} dirty=${JSON.stringify(form) !== first} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn" disabled=${busy} onClick=${() => preview(false)}>Preview</button>
      <button class="btn primary" disabled=${busy} onClick=${save}>${busy ? 'Working...' : (report ? 'Save' : 'Preview and save')}</button></div>`}>
    ${problem && html`<${Notice} kind="bad">${problem}<//>`}
    <div class="input-row">
      <${TextInput} label="Name" maxLength=${100} value=${form.name} onChange=${(v) => set('name', v)} />
      <${RolePicker} label="Role it gives" value=${form.role_id} onChange=${(v) => set('role_id', v)} />
    </div>
    <${Tabs} active=${tab} onChange=${setTab} tabs=${[{ key: 'conditions', label: 'Conditions' }, { key: 'more', label: 'Schedule and members' }, { key: 'notes', label: 'Notifications' }, { key: 'preview', label: 'Preview' }]} />
    ${tab === 'conditions' && html`<div>
      <p class="muted">Members who pass get the role. Members who stop passing lose it at the next run. The conditions read the statistics already recorded, so a new rule works on the past at once.</p>
      <${ConditionEditor} node=${form.conditions} depth=${0} others=${others} choices=${choices} total=${total} onChange=${(v) => set('conditions', v)} />
    </div>`}
    ${tab === 'more' && html`<div>
      <${Select} label="Runs" value=${schedule.preset} options=${data.presets} onChange=${(preset) => set('schedule', { preset, cron: preset === 'custom' ? schedule.cron : '' })} />
      ${schedule.preset === 'custom' && html`<${TextInput} label="Cron schedule" placeholder="*/30 * * * *" value=${schedule.cron} onChange=${(v) => set('schedule', { preset: 'custom', cron: v })}
        help=${`Minute, hour, day of the month, month, day of the week, in ${data.timezone}. It can run at most every 10 minutes.`} />`}
      <${Select} label="Applies to" value=${form.apply_to} options=${[{ value: 'humans', label: 'People' }, { value: 'bots', label: 'Bots' }, { value: 'both', label: 'People and bots' }]} onChange=${(v) => set('apply_to', v)} />
      <${PeopleField} label="Ignored members" help="They never gain or lose the role through this rule." value=${form.ignore} onChange=${(v) => set('ignore', v)} />
      <${Switch} label="Permanent" help="Once given, the role is never taken back by this rule." value=${form.permanent} onChange=${(v) => set('permanent', v)} />
      <${Switch} label="Run on its schedule" value=${form.enabled} onChange=${(v) => set('enabled', v)} />
    </div>`}
    ${tab === 'notes' && html`<div>
      <${NotificationEditor} which="give" value=${form.give} roleId=${form.role_id} onChange=${(v) => set('give', v)} />
      <${NotificationEditor} which="remove" value=${form.remove} roleId=${form.role_id} onChange=${(v) => set('remove', v)} />
    </div>`}
    ${tab === 'preview' && html`<${PreviewPanel} report=${report} form=${form} number=${number} onEveryone=${() => preview(true)} />`}
  <//>`;
}

function RunsModal({ rule, onClose }) {
  const [runs, setRuns] = useState(null);
  useEffect(() => { api.get(`/statroles/${rule.number}/runs`).then((found) => setRuns(found.runs)).catch((error) => { toastError(error); setRuns([]); }); }, []);
  const words = { schedule: 'On schedule', manual: 'Run now', resumed: 'After a restart' };
  return html`<${Modal} icon="history" title=${`Runs of ${rule.name}`} onClose=${onClose}>
    ${runs === null ? html`<${Loading} />` : runs.length === 0 ? html`<p class="muted">No runs yet.</p>` : runs.map((run) => html`<div class="field-edit" key=${run.started_at}>
      <div class="spread"><b>${when(run.started_at)}</b><span class=${'badge ' + (run.state === 'done' ? 'green' : run.state === 'running' ? 'blue' : 'red')}>${run.state}</span></div>
      <div class="small muted">${words[run.reason] || run.reason}. ${run.matching.toLocaleString()} passed, ${run.gave.toLocaleString()} of ${run.to_give.toLocaleString()} given, ${run.took.toLocaleString()} of ${run.to_take.toLocaleString()} taken${run.failed ? `, ${run.failed} failed` : ''}.</div>
      ${run.note && html`<div class="small">${run.note}</div>`}
    </div>`)}
  <//>`;
}

function AboveOwnCard({ isAdmin }) {
  const form = useSettings('statistics');
  if (!form.data) return null;
  return html`<${Card} icon="shield" title="Who can set up which roles">
    <${Switch} label="Let members set up stat roles for roles at or above their own highest role" value=${form.data.statrole_above_own} disabled=${!isAdmin || form.saving}
      onChange=${(v) => form.saveFields({ statrole_above_own: v })}
      help="While this is off, anyone other than the owner and Administrators can only pick roles below their own highest role. No stat role can give a role at or above the bot's highest role. Only an Administrator changes this." />
  <//>`;
}

export default function StatRolesPage() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [editing, setEditing] = useState(null);
  const [history, setHistory] = useState(null);
  const timer = useRef(null);
  const load = () => api.get('/statroles').then((found) => {
    setData(found);
    clearTimeout(timer.current);
    if (found.rules.some((rule) => rule.running)) timer.current = setTimeout(load, 2000);
  }).catch((error) => { toastError(error); setData({ rules: [], presets: [], most: 250, on: true }); });
  useEffect(() => { load(); return () => clearTimeout(timer.current); }, [guild.id]);
  const act = async (work, done) => {
    try {
      await work();
      if (done) toast(done);
      load();
    } catch (error) {
      toastError(error);
    }
  };
  const remove = async (rule) => {
    const sure = await confirmDialog({ title: `Delete ${rule.name}?`, body: 'Roles it gave stay where they are.', confirm: 'Delete', danger: true });
    if (sure) act(() => api.del(`/statroles/${rule.number}`), 'Deleted.');
  };
  if (data === null) return html`<${Loading} />`;
  const roleName = (id) => { const role = guild.roles.find((r) => sameId(r.id, id)); return role ? role.name : 'a deleted role'; };
  const scheduleText = (rule) => (rule.schedule.preset === 'custom' ? `Cron ${rule.schedule.cron}` : (data.presets.find((p) => p.value === rule.schedule.preset) || {}).label);
  return html`<div>
    ${!data.on && html`<${Notice} kind="warn">Stat roles run while Statistics is switched on. Switch it on in Statistics settings.<//>`}
    <${Card} icon="crown" title="Stat roles" right=${html`<span class="small dim">${data.rules.length} of ${data.most}</span>`}>
      <button class="btn success" disabled=${data.rules.length >= data.most} onClick=${() => setEditing({ start: blankRule(), number: null })}><${Icon} name="plus" /> New stat role</button>
      <p class="muted" style=${{ marginTop: '12px', marginBottom: 0 }}>A stat role gives a role to the members whose statistics pass its conditions, and takes it from the ones who stop passing, on its schedule. Roles change at about one a second. Removing the bot's Manage Roles stops a run.</p>
      <hr class="rule" />
      ${data.rules.length === 0 && html`<p class="muted">No stat roles yet.</p>`}
      ${data.rules.map((rule) => html`<div class="field-edit" key=${rule.number}>
        <div class="spread"><b>${rule.name}</b>
          <div class="row" style=${{ flexWrap: 'nowrap' }}>
            <button class="btn small" disabled=${!!rule.running} onClick=${() => act(() => api.post(`/statroles/${rule.number}/run`), 'Running. Roles change at about one a second.')}><${Icon} name="zap" size=${14} /> Run now</button>
            <button class="btn small primary" onClick=${() => setEditing({ start: { name: rule.name, role_id: rule.role_id, enabled: rule.enabled, conditions: rule.conditions, apply_to: rule.apply_to, ignore: rule.ignore, permanent: rule.permanent, schedule: rule.schedule, give: rule.give, remove: rule.remove }, number: rule.number })}>Edit</button>
            <button class="icon-btn" aria-label="Runs" onClick=${() => setHistory(rule)}><${Icon} name="history" /></button>
            <button class="icon-btn danger" aria-label="Delete" onClick=${() => remove(rule)}><${Icon} name="trash-2" /></button>
          </div></div>
        <${Switch} label=${`Gives ${roleName(rule.role_id)}. ${scheduleText(rule)}.`} value=${rule.enabled} onChange=${(on) => act(() => api.patch(`/statroles/${rule.number}`, { enabled: on }), on ? 'Switched on.' : 'Switched off.')} />
        ${rule.paused && html`<${Notice} kind="warn">${rule.paused}<//>`}
        <div class="small muted">${rule.described.join('. ')}.</div>
        ${rule.running ? html`<div role="status">
            ${rule.running.working_out ? html`<div class="small">Working out who passes...</div>` : html`<div>
              <div class="progress"><span style=${{ width: Math.round(100 * (1 - rule.running.left / Math.max(1, rule.running.to_give + rule.running.to_take))) + '%' }}></span></div>
              <div class="small">${rule.running.gave.toLocaleString()} given, ${rule.running.took.toLocaleString()} taken, ${rule.running.left.toLocaleString()} to go.</div></div>`}
          </div>`
          : rule.last_run && html`<div class="small dim">Last run ${when(rule.last_run.started_at)}: ${rule.last_run.gave.toLocaleString()} given, ${rule.last_run.took.toLocaleString()} taken${rule.last_run.note ? `. ${rule.last_run.note}` : '.'}</div>`}
        ${rule.enabled && rule.next_run && html`<div class="small dim">Next run ${when(rule.next_run)}.</div>`}
      </div>`)}
    <//>
    <${AboveOwnCard} isAdmin=${data.is_admin} />
    ${editing && html`<${RuleModal} start=${editing.start} number=${editing.number} data=${data} onClose=${() => setEditing(null)} onSaved=${() => { setEditing(null); toast('Saved.'); load(); }} />`}
    ${history && html`<${RunsModal} rule=${history} onClose=${() => setHistory(null)} />`}
  </div>`;
}
