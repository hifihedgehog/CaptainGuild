import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { ANY_CHANNEL, Card, ChannelPicker, Check, Icon, Loading, Modal, RolePicker, Switch, Tabs, confirmDialog, toast, toastError, useGuild, useUnsaved } from '../ui.js';

const BLANK = { enabled: true, channel_wl: [], channel_bl: [], role_wl: [], role_bl: [], bypass_roles: [], redirect: null, mod_only: false, restricted: false, muted: false, delete: false, disabled_subcommands: [] };
const GLOBAL_FIELDS = ['channel_wl', 'channel_bl', 'role_wl', 'role_bl', 'bot_channels', 'mod_role', 'mod_only', 'elevated_roles'];

// Discord's name for a permission where the server sends one, and the key in words otherwise.
export function permissionName(labels, key) {
  if (labels && labels[key]) return labels[key];
  return key.split('_').map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');
}

// What a command's saved rule changes from the defaults, in short phrases. Empty means the command runs as it always does.
export function ruleList(row) {
  if (!row) return [];
  const out = [];
  if (row.enabled === false) out.push('turned off');
  const count = (list, one, many) => { if (list && list.length) out.push(`${list.length} ${list.length === 1 ? one : many}`); };
  count(row.channel_wl, 'allowed channel', 'allowed channels');
  count(row.channel_bl, 'ignored channel', 'ignored channels');
  count(row.role_wl, 'allowed role', 'allowed roles');
  count(row.role_bl, 'ignored role', 'ignored roles');
  count(row.bypass_roles, 'bypass role', 'bypass roles');
  count(row.disabled_subcommands, 'subcommand off', 'subcommands off');
  if (row.redirect) out.push('output redirected');
  if (row.mod_only) out.push('mod only');
  if (row.restricted) out.push('restricted');
  if (row.muted) out.push('output muted');
  if (row.delete) out.push('invocation deleted');
  return out;
}

// Every command whose name, subcommand names, other names or (when asked) description holds the text, best match first, each with the tab it sits on.
export function searchCommands(categories, text, matchDescription) {
  const needle = text.trim().toLowerCase();
  if (!needle) return [];
  const hits = [];
  for (const category of categories) {
    for (const command of category.commands) {
      const name = command.name.toLowerCase();
      let score = 0;
      let why = null;
      if (name === needle) score = 5;
      else if (name.startsWith(needle)) score = 4;
      else if (name.includes(needle)) score = 3;
      if (!score) {
        const sub = command.subcommands.find((s) => s.toLowerCase().includes(needle));
        const alias = (command.aliases || []).find((a) => a.toLowerCase().includes(needle));
        if (alias) { score = 2; why = `answers to ${alias}`; }
        else if (sub) { score = 2; why = `has ${sub}`; }
      }
      if (!score && matchDescription && (command.description || '').toLowerCase().includes(needle)) { score = 1; why = 'description'; }
      if (score) hits.push({ command, category: category.category, score, why });
    }
  }
  return hits.sort((a, b) => b.score - a.score || a.command.name.localeCompare(b.command.name));
}

// The rule a bulk edit writes: what the person set, with each command's own on or off and turned-off subcommands kept as they are.
export function bulkRule(form, current) {
  const row = current || {};
  return { ...form, enabled: row.enabled !== false, disabled_subcommands: row.disabled_subcommands || [] };
}

function GlobalSettings({ settings, onSave }) {
  const start = Object.fromEntries(GLOBAL_FIELDS.map((key) => [key, settings[key]]));
  const [form, setForm] = useState(start);
  const set = (key) => ({ value: form[key], onChange: (value) => setForm({ ...form, [key]: value }) });
  const changed = GLOBAL_FIELDS.filter((key) => JSON.stringify(form[key]) !== JSON.stringify(start[key]));
  useUnsaved(changed.length > 0);
  const save = () => onSave(Object.fromEntries(changed.map((key) => [key, form[key]])));
  return html`<div class="card" style=${{ maxWidth: '640px' }}>
    <div class="card-head"><b>Global settings</b></div>
    <div class="card-body">
      <p class="muted">The rules here apply to every command. Inside "ignore overwrites" channels and roles, per-command allow and ignore lists don't apply. Outside per-command allowed channels and roles, a command can't run. A turned-off command is off even for moderators.</p>
      <${ChannelPicker} label="Ignore overwrites in" multi categories types=${ANY_CHANNEL} ...${set('channel_wl')} />
      <${ChannelPicker} label="Ignore channels/categories" multi categories types=${ANY_CHANNEL} ...${set('channel_bl')} />
      <${RolePicker} label="Roles that ignore overwrites" multi ...${set('role_wl')} />
      <${RolePicker} label="Ignored roles" multi ...${set('role_bl')} />
      <${ChannelPicker} label="Bot channels" multi categories help="Restricted commands answer in the first one listed. A category counts for every channel in it." ...${set('bot_channels')} />
      <${RolePicker} label="Bot manager role" placeholder="No bot manager selected" ...${set('mod_role')} />
      <${Switch} label="Entire bot mod only" ...${set('mod_only')} />
      <${Switch} label="Elevated roles: allow the bot to assign roles with elevated permissions"
        help="Lets features like reaction roles hand out roles that hold Administrator, Manage Server, Manage Channels, Manage Roles, Manage Messages, Manage Expressions, Manage Nicknames, Manage Webhooks, Ban Members, Kick Members or Timeout Members. Only the server owner or an Administrator can change this." ...${set('elevated_roles')} />
    </div>
    <div class="card-foot"><button class="btn ghost danger" disabled=${!changed.length} onClick=${() => setForm(start)}>Reset</button><button class="btn primary" disabled=${!changed.length} onClick=${save}>Save</button></div>
  </div>`;
}

function OverwriteEditor({ command, value, onClose, onSave }) {
  const [start] = useState(() => ({ ...BLANK, ...value }));
  const [form, setForm] = useState(start);
  const [saving, setSaving] = useState(false);
  const set = (key) => ({ value: form[key], onChange: (next) => setForm({ ...form, [key]: next }) });
  const toggleSub = (name, on) => setForm({ ...form, disabled_subcommands: on ? form.disabled_subcommands.filter((s) => s !== name) : [...form.disabled_subcommands, name] });
  const save = async () => {
    setSaving(true);
    try { await onSave(form); } finally { setSaving(false); }
  };
  return html`<${Modal} title=${'Overwrites for ' + command.name} onClose=${onClose} dirty=${JSON.stringify(form) !== JSON.stringify(start)} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button><button class="btn primary" disabled=${saving} onClick=${save}>${saving ? 'Saving...' : 'Save'}</button></div>`}>
    <p>${command.description}</p>
    <${ChannelPicker} label="Allowed channels" multi categories types=${ANY_CHANNEL} ...${set('channel_wl')} />
    <${ChannelPicker} label="Ignore channels/categories" multi categories types=${ANY_CHANNEL} ...${set('channel_bl')} />
    <${RolePicker} label="Allowed roles" multi ...${set('role_wl')} />
    <${RolePicker} label="Ignored roles" multi ...${set('role_bl')} />
    <${RolePicker} label="Bypass Discord permission requirements" multi ...${set('bypass_roles')} />
    <${ChannelPicker} label="Redirect output" placeholder="Do not redirect output" extra=${[{ value: 'dm', label: 'DM author', prefix: '', group: 'Options' }]} ...${set('redirect')} />
    <${Switch} label="Mod only" ...${set('mod_only')} />
    <${Switch} label="Restricted" help="Outside the bot channel, the bot pings the person there and answers there, so commands don't clutter main channels. Needs a bot channel." ...${set('restricted')} />
    <${Switch} label="Mute output" ...${set('muted')} />
    <${Switch} label="Delete invocation" ...${set('delete')} />
    ${command.subcommands.length > 0 && html`<h4>Subcommands</h4>
      ${command.subcommands.map((name) => html`<${Check} key=${name} label=${name} value=${!form.disabled_subcommands.includes(name)} onChange=${(on) => toggleSub(name, on)} />`)}`}
  <//>`;
}

function CommandTile({ command, row, category, why, picked, onPick, onToggle, onEdit, labels }) {
  const on = row.enabled !== false;
  const rules = ruleList(row);
  const needs = command.permissions || [];
  return html`<div class=${'tile' + (on ? '' : ' off')}>
    <div class="spread"><h3>${onPick && html`<input type="checkbox" aria-label=${'Select ' + command.name} checked=${picked}
        onChange=${(e) => onPick(e.currentTarget.checked)} /> `}${command.name}</h3>
      ${command.varies ? html`<span class="badge blue" title="Its subcommands need different things. The docs page lists each one.">Varies</span>`
        : command.staff ? html`<span class="badge blue" title="This command checks for this group itself. A bypass role doesn't open it.">${command.staff}</span>`
        : needs.slice(0, 1).map((p) => html`<span class="badge blue" title=${'This command needs ' + needs.map((n) => permissionName(labels, n)).join(', ') + '. A bypass role skips the need.'} key=${p}>${permissionName(labels, p)}</span>`)}</div>
    ${(category || rules.length > 0) && html`<div class="row" style=${{ marginTop: '6px', gap: '6px' }}>
      ${category && html`<span class="badge outline">${category}</span>`}
      ${rules.length > 0 && html`<span class="badge gold" title=${rules.join(', ')}>Has rules</span>`}
      ${why && html`<span class="muted small">${why}</span>`}
    </div>`}
    <p class="muted" style=${{ margin: '10px 0' }}>${command.description || 'No description.'}</p>
    <div class="spread">
      <label class="switch"><input type="checkbox" role="switch" aria-label=${'Turn ' + command.name + ' on or off'} checked=${on} onChange=${(e) => onToggle(e.currentTarget.checked)} /><span></span></label>
      <button class="icon-btn" aria-label=${'Overwrites for ' + command.name} onClick=${onEdit}><${Icon} name="settings" /></button>
    </div>
  </div>`;
}

export default function CommandsPage() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [tab, setTab] = useState('global');
  const [filter, setFilter] = useState('');
  const [matchDescription, setMatchDescription] = useState(false);
  const [onlyRules, setOnlyRules] = useState(false);
  const [editing, setEditing] = useState(null);
  const [picked, setPicked] = useState(null);
  const [, redraw] = useState(0);
  // Changes go to the server one at a time, in the order they were made.
  const queue = useRef(Promise.resolve());

  useEffect(() => {
    let live = true;
    api.get('/commands').then((d) => { if (live) setData(d); }).catch(toastError);
    return () => { live = false; };
  }, [guild.id]);
  if (!data) return html`<${Loading} />`;

  const settings = data.settings;
  const labels = data.permission_labels || {};
  // Runs one request after the ones before it, then keeps the answer. A refused change puts the screen back as the server has it.
  const send = (request) => {
    const run = queue.current.then(async () => {
      try {
        const fresh = await request();
        setData((d) => ({ ...d, settings: fresh }));
        toast('Saved.');
        return true;
      } catch (error) {
        toastError(error);
        redraw((n) => n + 1);
        return false;
      }
    });
    queue.current = run;
    return run;
  };
  const saveGlobal = (changes) => send(() => api.put('/settings/commands', changes));
  // The server applies these to the rules it holds now, so a change made elsewhere since this page opened stays.
  const setOverwrites = (changes) => send(() => api.patch('/commands/overwrites', { changes }));
  const overwriteOf = (name) => settings.overwrites[name] || {};
  const category = data.categories.find((c) => c.category === tab);
  const needle = filter.trim();
  const searching = needle !== '';
  const withRules = (name) => ruleList(overwriteOf(name)).length > 0;
  let shown = [];
  if (searching) shown = searchCommands(data.categories, needle, matchDescription).map((hit) => ({ ...hit, tag: hit.category }));
  else if (category) shown = category.commands.map((command) => ({ command, category: category.category, why: null, tag: null }));
  if (onlyRules) shown = shown.filter((hit) => withRules(hit.command.name));
  const tabs = [{ key: 'global', label: 'Global settings' }, ...data.categories.map((c) => ({
    key: c.category,
    label: c.commands.some((command) => withRules(command.name)) ? html`${c.category} <span class="tab-dot" title="Some commands here have rules"></span>` : c.category,
  }))];

  const setCategoryEnabled = (on) => {
    const changes = {};
    for (const command of category.commands) changes[command.name] = { ...overwriteOf(command.name), enabled: on };
    setOverwrites(changes);
  };
  const resetCategory = async () => {
    const sure = await confirmDialog({ title: 'Reset this category?', body: 'This removes every overwrite and turns the commands in this category back on.', confirm: 'Reset', danger: true });
    if (!sure) return;
    const changes = {};
    for (const command of category.commands) changes[command.name] = null;
    setOverwrites(changes);
  };
  const names = shown.map((hit) => hit.command.name);
  const chosen = picked ? picked.filter((n) => names.includes(n)) : [];
  const applyPicked = async (form) => {
    const changes = {};
    for (const name of editing.wholeCategory ? picked : chosen) changes[name] = bulkRule(form, overwriteOf(name));
    const done = await setOverwrites(changes);
    if (done) { setPicked(null); setEditing(null); }
  };
  const closeEditor = () => {
    // A category-wide edit was never a selection the person made, so closing it ends the selection too.
    if (editing && editing.wholeCategory) setPicked(null);
    setEditing(null);
  };

  return html`<${Card} title="Commands">
    <div class="row" style=${{ marginBottom: '12px' }}>
      <div class="input-group" style=${{ width: '280px', maxWidth: '100%' }}><input type="search" placeholder="Filter commands" aria-label="Filter commands" value=${filter} onInput=${(e) => setFilter(e.currentTarget.value)} /><span class="addon"><${Icon} name="search" /></span></div>
      <${Check} label="Match description" value=${matchDescription} onChange=${setMatchDescription} />
      <${Check} label="Only with rules" value=${onlyRules} onChange=${setOnlyRules} />
      ${searching && html`<span class="muted small">${shown.length} ${shown.length === 1 ? 'command matches' : 'commands match'}, from every tab</span>`}
    </div>
    <${Tabs} tabs=${tabs} active=${searching ? '' : tab} onChange=${(key) => { setTab(key); setFilter(''); setPicked(null); }} />
    ${!searching && tab === 'global' && html`<${GlobalSettings} key=${JSON.stringify(settings)} settings=${settings} onSave=${saveGlobal} />`}
    ${(searching || category) && html`<div>
      <div class="row" style=${{ marginBottom: '14px' }}>
        ${category && !searching && html`<button class="btn success" onClick=${() => { setPicked(category.commands.map((c) => c.name)); setEditing({ name: category.category + ' (all)', description: 'These apply to every command in this category and replace the rules each has now. Which commands are on, and which subcommands are off, stay as they are.', subcommands: [], wholeCategory: true }); }}>Manage category</button>`}
        ${category && !searching && html`<button class="btn danger" onClick=${resetCategory}>Reset category</button>`}
        <button class="btn primary" onClick=${() => setPicked(picked ? null : [])}>${picked ? 'Stop selecting' : 'Select multiple'}</button>
        ${picked && html`<button class="btn" onClick=${() => setPicked(names)}>${searching ? 'Select all shown' : 'Select category'}</button>`}
        ${picked && chosen.length > 0 && html`<button class="btn ghost" onClick=${() => setPicked([])}>Clear</button>`}
        ${picked && chosen.length > 0 && html`<button class="btn" onClick=${() => setEditing({ name: chosen.length + ' commands', description: 'These replace the rules each selected command has now. Which commands are on, and which subcommands are off, stay as they are.', subcommands: [] })}>Manage ${chosen.length} selected</button>`}
        ${category && !searching && html`<button class="btn outline success" onClick=${() => setCategoryEnabled(true)}>Enable category</button>`}
        ${category && !searching && html`<button class="btn outline danger" onClick=${() => setCategoryEnabled(false)}>Disable category</button>`}
      </div>
      ${shown.length === 0 && html`<p class="muted">${searching ? 'No command matches.' : (onlyRules ? 'No command here has rules.' : 'Nothing here.')}</p>`}
      <div class="tile-grid">${shown.map(({ command, tag, why }) => html`<${CommandTile} key=${command.name} command=${command} row=${overwriteOf(command.name)} category=${tag} why=${why} labels=${labels}
        picked=${!!picked && picked.includes(command.name)}
        onPick=${picked ? (on) => setPicked(on ? [...picked, command.name] : picked.filter((n) => n !== command.name)) : null}
        onToggle=${(on) => setOverwrites({ [command.name]: { ...overwriteOf(command.name), enabled: on } })}
        onEdit=${() => { setPicked(null); setEditing(command); }} />`)}</div>
    </div>`}
    ${editing && html`<${OverwriteEditor} command=${editing} value=${picked ? {} : overwriteOf(editing.name)} onClose=${closeEditor}
      onSave=${async (form) => {
        if (picked) { await applyPicked(form); return; }
        const done = await setOverwrites({ [editing.name]: form });
        if (done) setEditing(null);
      }} />`}
  <//>`;
}
