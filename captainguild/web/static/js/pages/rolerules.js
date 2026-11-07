// Role rules: roles that follow from the roles members hold.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Icon, Loading, Modal, Notice, NumberInput, RolePicker, Select, Switch, TextInput, confirmDialog, sameId, toast, toastError, useGuild } from '../ui.js';
import { roleWarnings } from './reactionroles.js';

const TYPES = [
  { value: 'has_some', label: 'Has any of these roles' },
  { value: 'has_all', label: 'Has all of these roles' },
  { value: 'lacks_some', label: 'Is missing one of these roles' },
  { value: 'lacks_all', label: 'Has none of these roles' },
  { value: 'exactly', label: 'Has exactly N of these roles' },
  { value: 'at_least', label: 'Has at least N of these roles' },
  { value: 'at_most', label: 'Has at most N of these roles' },
  { value: 'more_than', label: 'Has more than N of these roles' },
  { value: 'fewer_than', label: 'Has fewer than N of these roles' },
];
const COUNTED = new Set(['exactly', 'at_least', 'at_most', 'more_than', 'fewer_than']);
const MAX_CONDITIONS = 10;
const MAX_RULES = 50;
const KEEP_HELP = 'Members who stop matching lose the roles this rule added, unless another rule that matches them adds the same role.';

const blankRule = () => ({ name: '', enabled: true, keep_in_sync: true, conditions: [{ type: 'has_some', roles: [], count: 0 }], add: [], remove: [] });

// A role's name, or a plain note when the role was deleted. The rule keeps the ID until someone removes it.
function roleLabel(guild, id) {
  const role = guild.roles.find((r) => sameId(r.id, id));
  return role ? role.name : 'deleted role';
}

function conditionText(guild, condition) {
  const type = TYPES.find((t) => t.value === condition.type);
  const words = (type ? type.label : condition.type).replace('N', String(condition.count));
  return `${words.charAt(0).toLowerCase()}${words.slice(1)}: ${condition.roles.map((id) => roleLabel(guild, id)).join(', ')}`;
}

function PreviewReport({ report }) {
  const guild = useGuild();
  if (!report) return null;
  if (report.looping && report.looping.length) {
    return html`<${Notice} kind="bad">These rules would undo each other's changes: ${report.looping.join(', ')}. Change one so they agree.<//>`;
  }
  if (!report.members) return html`<${Notice}>Saving this changes nobody's roles right now.<//>`;
  return html`<${Notice} kind="warn">
    <div>Saving this changes the roles of ${report.members.toLocaleString()} ${report.members === 1 ? 'member' : 'members'}.</div>
    ${report.roles.map((entry) => html`<div key=${entry.role_id} style=${{ marginTop: '6px' }}>
      <b>${roleLabel(guild, entry.role_id)}</b>: ${entry.gain ? `${entry.gain.toLocaleString()} gain it` : ''}${entry.gain && entry.lose ? ', ' : ''}${entry.lose ? `${entry.lose.toLocaleString()} lose it` : ''}
      ${entry.gain_names.length > 0 && html`<div class="muted">Gaining: ${entry.gain_names.join(', ')}${entry.gain > entry.gain_names.length ? ` and ${(entry.gain - entry.gain_names.length).toLocaleString()} more` : ''}</div>`}
      ${entry.lose_names.length > 0 && html`<div class="muted">Losing: ${entry.lose_names.join(', ')}${entry.lose > entry.lose_names.length ? ` and ${(entry.lose - entry.lose_names.length).toLocaleString()} more` : ''}</div>`}
    </div>`)}
  <//>`;
}

function RuleModal({ rule, others, onClose, onSave }) {
  const guild = useGuild();
  const [form, setForm] = useState(rule);
  const [problem, setProblem] = useState(null);
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const [start] = useState(() => JSON.stringify(rule));
  const set = (key, value) => { setForm((current) => ({ ...current, [key]: value })); setReport(null); };
  const setCondition = (index, changes) => set('conditions', form.conditions.map((c, i) => (i === index ? { ...c, ...changes } : c)));
  const whole = () => others(form);
  const preview = async () => {
    setBusy(true);
    try {
      setReport(await api.post('/rolerules/preview', { rules: whole() }));
      setProblem(null);
    } catch (error) {
      setProblem((error.fields && error.fields.rules) || error.message);
    } finally {
      setBusy(false);
    }
  };
  // The first press shows what saving would change. The second saves.
  const save = async () => {
    if (!report) {
      await preview();
      return;
    }
    setBusy(true);
    const failure = await onSave(whole());
    setBusy(false);
    if (failure) setProblem(failure);
  };
  const chosen = [...form.add, ...form.remove];
  return html`<${Modal} large icon="git-merge" title=${rule.id ? 'Edit role rule' : 'New role rule'} onClose=${onClose} dirty=${JSON.stringify(form) !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn" disabled=${busy} onClick=${preview}>Preview</button>
      <button class="btn primary" disabled=${busy} onClick=${save}>${busy ? 'Working...' : (report ? 'Save' : 'Preview and save')}</button></div>`}>
    ${problem && html`<${Notice} kind="bad">${problem}<//>`}
    ${form.paused && html`<${Notice} kind="warn">Paused: ${form.paused}<//>`}
    <${TextInput} label="Name" maxLength=${100} value=${form.name} onChange=${(v) => set('name', v)} />
    <h3>When a member matches all of these</h3>
    ${form.conditions.map((condition, index) => html`<div class="field-edit" key=${index}>
      <div class="spread"><b>Condition ${index + 1}</b>
        <button class="icon-btn danger" aria-label="Remove condition" disabled=${form.conditions.length === 1} onClick=${() => set('conditions', form.conditions.filter((_, i) => i !== index))}><${Icon} name="trash-2" /></button></div>
      <${Select} label="Type" value=${condition.type} options=${TYPES} onChange=${(type) => setCondition(index, { type })} />
      ${COUNTED.has(condition.type) && html`<${NumberInput} label="N" min=${0} max=${250} value=${condition.count} onChange=${(count) => setCondition(index, { count })} />`}
      <${RolePicker} label="Roles" multi everyone value=${condition.roles} onChange=${(roles) => setCondition(index, { roles })} />
    </div>`)}
    <button class="btn" disabled=${form.conditions.length >= MAX_CONDITIONS} onClick=${() => set('conditions', [...form.conditions, { type: 'has_some', roles: [], count: 0 }])}><${Icon} name="plus" /> Add condition</button>
    <hr class="rule" />
    <h3>Then</h3>
    <${RolePicker} label="Roles to add" multi value=${form.add} onChange=${(v) => set('add', v)} />
    <${RolePicker} label="Roles to remove" multi value=${form.remove} onChange=${(v) => set('remove', v)} />
    ${roleWarnings(guild, chosen, guild).map((text) => html`<${Notice} key=${text} kind="warn">${text}<//>`)}
    <${Switch} label="Keep in sync" help=${KEEP_HELP} value=${form.keep_in_sync} onChange=${(v) => set('keep_in_sync', v)} />
    <${PreviewReport} report=${report} />
  <//>`;
}

function SyncCard({ count }) {
  const [job, setJob] = useState(null);
  const timer = useRef(null);
  const poll = async () => {
    try {
      const { job: current } = await api.get('/rolerules/sync');
      setJob(current);
      timer.current = current ? setTimeout(poll, 1500) : null;
    } catch (error) {
      toastError(error);
    }
  };
  useEffect(() => { poll(); return () => clearTimeout(timer.current); }, []);
  const start = async () => {
    try {
      const { job: started } = await api.post('/rolerules/sync');
      setJob(started);
      if (started) timer.current = setTimeout(poll, 1500);
      else toast('Every member already matches the rules.');
    } catch (error) {
      toastError(error);
    }
  };
  const share = job && job.total ? Math.round((job.done / job.total) * 100) : 100;
  return html`<${Card} icon="refresh-cw" title="Sync now">
    <p class="muted">The bot checks every member when a rule is saved and every 10 minutes. Sync now checks them at once and shows how far it has got. Changes go out at about one a second.</p>
    ${job ? html`<div role="status">
        <div class="progress"><span style=${{ width: share + '%' }}></span></div>
        <p>${job.done.toLocaleString()} of ${job.total.toLocaleString()} members checked, ${job.changed.toLocaleString()} changed.</p></div>`
      : html`<button class="btn primary" disabled=${!count} onClick=${start}><${Icon} name="refresh-cw" /> Sync now</button>`}
  <//>`;
}

export default function RoleRulesPage() {
  const guild = useGuild();
  const [rules, setRules] = useState(null);
  const [logChannel, setLogChannel] = useState(null);
  const [savedChannel, setSavedChannel] = useState(null);
  const [editing, setEditing] = useState(null);
  // The version of the rules this page holds. A save names it, and the server refuses one made from an older copy, so a change saved elsewhere since isn't undone.
  const version = useRef(null);
  useEffect(() => {
    api.get('/rolerules').then((data) => { version.current = data.version; setRules(data.rules); setLogChannel(data.log_channel); setSavedChannel(data.log_channel); })
      .catch((error) => { toastError(error); setRules([]); });
  }, [guild.id]);

  // Saves the whole list. A change to many members comes back asking for the count, and goes through once the person confirms it.
  const store = async (next, confirm) => {
    const body = { rules: next, version: version.current };
    const saved = await api.put('/rolerules', confirm ? { ...body, confirm } : body);
    version.current = saved.version;
    setRules(saved.rules);
    return saved;
  };
  const storeAsking = async (next) => {
    try {
      await store(next);
      return true;
    } catch (error) {
      const count = error.status === 409 && error.fields && error.fields.confirm;
      if (!count) throw error;
      const sure = await confirmDialog({ title: 'Change this many members?', body: `${error.message} Their roles change at about one a second.`, confirm: `Yes, change ${Number(count).toLocaleString()}` });
      if (!sure) return false;
      await store(next, Number(count));
      return true;
    }
  };
  const saveRule = async (next) => {
    try {
      if (await storeAsking(next)) {
        toast('Saved.');
        setEditing(null);
      }
      return null;
    } catch (error) {
      return (error.fields && error.fields.rules) || error.message;
    }
  };
  const quick = async (next) => {
    try {
      if (await storeAsking(next)) toast('Saved.');
    } catch (error) {
      toastError(error);
    }
  };
  const move = (index, step) => {
    const next = [...rules];
    const [picked] = next.splice(index, 1);
    next.splice(index + step, 0, picked);
    quick(next);
  };
  const remove = async (rule) => {
    const sure = await confirmDialog({ title: `Delete ${rule.name}?`, body: 'Roles it gave stay where they are.', confirm: 'Delete', danger: true });
    if (sure) quick(rules.filter((other) => other !== rule));
  };
  const saveChannel = async () => {
    try {
      const saved = await api.put('/rolerules', { log_channel: logChannel });
      version.current = saved.version;
      setSavedChannel(saved.log_channel);
      toast('Saved.');
    } catch (error) {
      toastError(error);
    }
  };
  // The list as it would be with this rule saved in its place, or added at the end.
  const withRule = (original) => (form) => (original ? rules.map((r) => (r === original ? form : r)) : [...rules, form]);
  if (rules === null) return html`<${Loading} />`;
  return html`<div>
    <${Card} icon="git-merge" title="Role rules">
      <button class="btn success" disabled=${rules.length >= MAX_RULES} onClick=${() => setEditing({ rule: blankRule(), original: null })}><${Icon} name="plus" /> Create role rule</button>
      <p class="muted" style=${{ marginTop: '12px', marginBottom: 0 }}>Each rule adds or removes roles for members who match all of its conditions. Rules run from the top, and a change one rule makes can set off the rules after it.</p>
      <hr class="rule" />
      ${rules.length === 0 && html`<p class="muted">No role rules yet. Create one above.</p>`}
      ${rules.map((rule, index) => html`<div class="field-edit" key=${rule.id || index}>
        <div class="spread"><b>${index + 1}. ${rule.name}</b>
          <div class="row" style=${{ flexWrap: 'nowrap' }}>
            <button class="icon-btn" aria-label="Move up" disabled=${index === 0} onClick=${() => move(index, -1)}><${Icon} name="arrow-up" /></button>
            <button class="icon-btn" aria-label="Move down" disabled=${index === rules.length - 1} onClick=${() => move(index, 1)}><${Icon} name="arrow-down" /></button>
            <button class="btn primary" onClick=${() => setEditing({ rule, original: rule })}>Edit</button>
            <button class="btn outline danger" onClick=${() => remove(rule)}>Delete <${Icon} name="trash-2" size=${14} /></button></div></div>
        <${Switch} label="Run this rule" value=${rule.enabled} onChange=${(on) => quick(rules.map((r) => (r === rule ? { ...r, enabled: on } : r)))} />
        ${rule.paused && html`<${Notice} kind="warn">Paused: ${rule.paused}<//>`}
        <div class="muted">When a member ${rule.conditions.map((c) => conditionText(guild, c)).join(' and ')}</div>
        ${rule.add.length > 0 && html`<div>Adds ${rule.add.map((id) => roleLabel(guild, id)).join(', ')}</div>`}
        ${rule.remove.length > 0 && html`<div>Removes ${rule.remove.map((id) => roleLabel(guild, id)).join(', ')}</div>`}
        ${rule.keep_in_sync && html`<div class="muted">Keeps in sync</div>`}
      </div>`)}
    <//>
    <${SyncCard} count=${rules.filter((rule) => rule.enabled).length} />
    <${Card} icon="scroll-text" title="Log channel">
      <p class="muted">Each change the rules make posts here as one line naming the member, the role and the rule. Nobody is pinged.</p>
      <${ChannelPicker} label="Channel" value=${logChannel} onChange=${setLogChannel} />
      <button class="btn primary" disabled=${String(logChannel) === String(savedChannel)} onClick=${saveChannel}>Save</button>
    <//>
    ${editing && html`<${RuleModal} rule=${editing.rule} others=${withRule(editing.original)} onClose=${() => setEditing(null)} onSave=${saveRule} />`}
  </div>`;
}
