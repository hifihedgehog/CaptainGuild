// Reaction roles: the table of messages, and the forms for making and editing them.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, DataTable, DurationInput, EmojiButton, EmojiPicker, EmojiView, Icon, Loading, Modal, NumberInput, Notice, Picker, RolePicker, SaveBar, SaveButton, Select, SettingsGate, Switch, TextArea, TextInput, channelName, confirmDialog, roleName, sameId, toast, toastError, useGuild, useSettings, when } from '../ui.js';
import { MessageEditor, MessagePreview, emptyMessage } from '../embed.js';

const TYPES = [
  { value: 'normal', label: 'normal', help: 'Picking gives the role. Taking the reaction off, or clicking again, takes it away.' },
  { value: 'unique', label: 'unique', help: 'A member holds one role from this message at a time. Picking another swaps them.' },
  { value: 'verify', label: 'verify', help: 'Members pick up the role here and can\'t give it up here. The bot takes the reaction off right away, so the message stays clean.' },
  { value: 'drop', label: 'drop', help: 'Reacting gives up the role. Nobody can pick one up here.' },
  { value: 'reversed', label: 'reversed', help: 'Reacting takes the role away. Taking the reaction off gives it back.' },
  { value: 'limit', label: 'limit', help: 'A member can hold only so many roles from this message.' },
  { value: 'binding', label: 'binding', help: 'A member picks one role and keeps it. They can\'t swap it or give it up here.' },
  { value: 'temp', label: 'temp', help: 'The role lasts a set time. Then the bot takes it back.' },
  { value: 'lock', label: 'lock', help: 'Nobody can pick up a role from this message until it changes to another type.' },
];
const MODES = [
  { value: 'reactions', label: 'Reactions', help: 'Members react with an emoji. This works on any message, including one another bot wrote.' },
  { value: 'buttons', label: 'Buttons', help: 'Members click a button on the message and get a private answer. Discord allows this only on the bot\'s own messages.' },
  { value: 'select', label: 'Select menu', help: 'Members pick from a menu on the message and get a private answer. Picking a role they hold takes it away. Discord allows this only on the bot\'s own messages.' },
];
const CREATE_MODES = [
  { value: 'post', label: 'Post a new message' },
  { value: 'id', label: 'Use a message ID' },
  { value: 'latest', label: 'Use the most recent message in a channel' },
];
const STYLES = [
  { value: 'secondary', label: 'Gray', color: '#4e5058' },
  { value: 'primary', label: 'Blue', color: '#5865f2' },
  { value: 'success', label: 'Green', color: '#248046' },
  { value: 'danger', label: 'Red', color: '#da373c' },
];
const MAX_ROWS = { reactions: 20, buttons: 25, select: 125 };
const permissionName = (name) => name.split('_').map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');

// What's wrong with handing out these roles. The bot refuses at the moment someone picks, so this is where a person finds out first.
export function roleWarnings(guild, ids, rules) {
  const out = [];
  for (const id of ids) {
    const role = guild.roles.find((r) => sameId(r.id, id));
    if (!role) {
      out.push('One of these roles no longer exists. Remove it.');
      continue;
    }
    if (role.position >= guild.me.top_role_position) {
      out.push(`${role.name} sits at or above the bot's highest role, so the bot can't hand it out. Move the bot's role above it in Server Settings under Roles.`);
    }
    const risky = role.permissions.filter((p) => rules.dangerous.includes(p));
    if (risky.length) {
      const names = risky.map(permissionName).join(', ');
      out.push(rules.elevated_roles
        ? `${role.name} holds ${names}. Anyone who picks it up gets that power.`
        : `${role.name} holds ${names}. The bot won't hand it out while elevated roles are off on the Commands page.`);
    }
  }
  return [...new Set(out)];
}

function botWarnings(guild, mode) {
  const have = guild.me.permissions;
  if (have.includes('administrator')) return [];
  const out = [];
  if (!have.includes('manage_roles')) out.push('The bot needs the Manage Roles permission to hand out roles.');
  if (mode === 'reactions' && !have.includes('manage_messages')) out.push('The bot needs Manage Messages to take reactions off, which verify, drop and unique messages do, and any message does when it refuses someone.');
  if (mode === 'reactions' && !have.includes('add_reactions')) out.push('The bot needs Add Reactions to put the emoji on the message.');
  return out;
}

let rowCounter = 0;
const newRow = (emoji) => ({ emoji: emoji || '', roles: [], max: {}, label: '', description: '', style: 'secondary', _key: ++rowCounter });
const withKeys = (rows) => rows.map((row) => ({ ...row, _key: ++rowCounter }));
const rowsForServer = (rows) => rows.map(({ _key, open, ...row }) => row);
const typeHelp = (value) => (TYPES.find((t) => t.value === value) || TYPES[0]).help;
const modeHelp = (value) => (MODES.find((m) => m.value === value) || MODES[0]).help;

function RowEditor({ row, index, count, mode, errors, onChange, onRemove, onMove }) {
  const guild = useGuild();
  const set = (key, value) => onChange({ ...row, [key]: value });
  const prefix = `rows.${index}`;
  const firstRole = row.roles.length ? roleName(guild, row.roles[0]) : 'The role\'s name';
  return html`<div class="field-edit">
    <div style=${{ display: 'grid', gridTemplateColumns: 'auto minmax(0, 1fr) auto', gap: '10px', alignItems: 'start' }}>
      <div style=${{ paddingTop: '1px' }}><${EmojiButton} value=${row.emoji} onChange=${(emoji) => set('emoji', emoji)} onClear=${mode === 'reactions' ? null : () => set('emoji', '')} /></div>
      <${RolePicker} multi value=${row.roles} onChange=${(roles) => set('roles', roles)} placeholder="Pick the roles this hands out" error=${errors[prefix + '.roles']} />
      <div class="row" style=${{ gap: '2px', flexWrap: 'nowrap' }}>
        <button type="button" class="icon-btn" aria-label="Move up" disabled=${index === 0} onClick=${() => onMove(index, -1)}><${Icon} name="arrow-up" /></button>
        <button type="button" class="icon-btn" aria-label="Move down" disabled=${index === count - 1} onClick=${() => onMove(index, 1)}><${Icon} name="arrow-down" /></button>
        <button type="button" class="icon-btn" aria-label="Maximum holders" title="Limit how many members can hold each role" aria-pressed=${!!row.open} onClick=${() => set('open', !row.open)}><${Icon} name="users" /></button>
        <button type="button" class="icon-btn danger" aria-label="Remove this row" onClick=${onRemove}><${Icon} name="trash-2" /></button>
      </div>
    </div>
    ${errors[prefix + '.emoji'] && html`<div class="error" style=${{ marginBottom: '8px' }}>${errors[prefix + '.emoji']}</div>`}
    ${mode === 'buttons' && html`<div class="input-row">
      <${TextInput} label="Button label" value=${row.label} placeholder=${firstRole} maxLength=${80} onChange=${(v) => set('label', v)} error=${errors[prefix + '.label']} />
      <${Select} label="Button color" value=${row.style} options=${STYLES} onChange=${(v) => set('style', v)} />
    </div>`}
    ${mode === 'select' && html`<div class="input-row">
      <${TextInput} label="Option label" value=${row.label} placeholder=${firstRole} maxLength=${100} onChange=${(v) => set('label', v)} error=${errors[prefix + '.label']} />
      <${TextInput} label="Option description" value=${row.description} placeholder="Shown under the label" maxLength=${100} onChange=${(v) => set('description', v)} error=${errors[prefix + '.description']} />
    </div>`}
    ${row.open && html`<div>
      <p class="muted small" style=${{ margin: '0 0 8px' }}>The most members who can hold each role at once. Leave a box empty for no maximum. The bot counts who holds the role, however they got it.</p>
      ${row.roles.length === 0 && html`<p class="dim small">Pick a role first.</p>`}
      <div class="input-row">${row.roles.map((id) => html`<${NumberInput} key=${String(id)} label=${roleName(guild, id)} min=${1} value=${row.max[String(id)] || null} error=${errors[prefix + '.max']}
        onChange=${(value) => { const max = { ...row.max }; if (value) max[String(id)] = value; else delete max[String(id)]; set('max', max); }} />`)}</div>
    </div>`}
  </div>`;
}

function RowsEditor({ rows, onChange, mode, errors, linkButtons }) {
  const [picking, setPicking] = useState(false);
  const most = mode === 'buttons' ? Math.max(0, MAX_ROWS.buttons - linkButtons) : MAX_ROWS[mode];
  const keyOf = (emoji) => {
    const custom = /^<a?:[^:>]+:(\d+)>$/.exec(emoji || '');
    return custom ? custom[1] : (emoji || '').replace(/\ufe0f/g, '');
  };
  const addEmoji = (emoji) => {
    setPicking(false);
    if (rows.some((row) => row.emoji && keyOf(row.emoji) === keyOf(emoji))) {
      toast('That emoji is on a row already. Put several roles on that row instead.', 'bad');
      return;
    }
    onChange([...rows, newRow(emoji)]);
  };
  const move = (index, change) => {
    const target = index + change;
    if (target < 0 || target >= rows.length) return;
    const next = [...rows];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };
  return html`<div>
    <h4>Emoji and roles</h4>
    ${errors.rows && html`<div class="error" style=${{ marginBottom: '8px' }}>${errors.rows}</div>`}
    ${rows.length === 0 && html`<p class="muted">Add an emoji, then pick the roles it hands out.</p>`}
    ${rows.map((row, index) => html`<${RowEditor} key=${row._key} row=${row} index=${index} count=${rows.length} mode=${mode} errors=${errors}
      onChange=${(next) => onChange(rows.map((r) => (r._key === row._key ? next : r)))}
      onRemove=${() => onChange(rows.filter((r) => r._key !== row._key))} onMove=${move} />`)}
    <div style=${{ position: 'relative', marginTop: '10px' }}>
      ${rows.length < most ? html`<div class="row">
        <button type="button" class="btn success" style=${{ flex: 1 }} onClick=${() => setPicking(!picking)}><${Icon} name="smile-plus" /> Add emoji</button>
        ${mode !== 'reactions' && html`<button type="button" class="btn" onClick=${() => onChange([...rows, newRow('')])}>Add without an emoji</button>`}
      </div>` : html`<p class="muted small">This message holds as many as Discord allows.</p>`}
      ${picking && html`<${EmojiPicker} onPick=${addEmoji} onClose=${() => setPicking(false)} />`}
    </div>
  </div>`;
}

// How the role controls will look under the message.
function ControlsPreview({ mode, rows, placeholder }) {
  const guild = useGuild();
  const shown = rows.filter((row) => row.emoji || row.label || row.roles.length);
  if (!shown.length) return null;
  const nameOf = (row) => row.label || row.roles.map((id) => roleName(guild, id)).join(', ') || 'Role';
  if (mode === 'reactions') {
    return html`<div class="dc-buttons" aria-label="Reactions">${shown.filter((row) => row.emoji).map((row) => html`
      <span key=${row._key} style=${{ display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '2px 8px', borderRadius: '8px', background: '#3b3d43', border: '1px solid #5865f2', color: '#dbdee1', fontSize: '14px' }}><${EmojiView} value=${row.emoji} size=${18} />1</span>`)}</div>`;
  }
  if (mode === 'buttons') {
    return html`<div class="dc-buttons" aria-label="Buttons">${shown.map((row) => {
      const style = STYLES.find((s) => s.value === row.style) || STYLES[0];
      return html`<span class="dc-button" key=${row._key} style=${{ background: style.color }}>${row.emoji && html`<${EmojiView} value=${row.emoji} size=${18} />`}${nameOf(row)}</span>`;
    })}</div>`;
  }
  return html`<div aria-label="Select menu" style=${{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '8px', padding: '9px 12px', maxWidth: '400px', background: '#1e1f22', borderRadius: '4px', color: '#949ba4', fontSize: '14px' }}>
    <span>${placeholder || 'Pick a role'}</span><${Icon} name="chevron-down" size=${16} /></div>`;
}

// Everything about how a message hands out roles, the same in both forms.
function RoleSettings({ form, set, errors, foreign, rules, linkButtons, others }) {
  const guild = useGuild();
  const [advanced, setAdvanced] = useState(form.linked.length > 0 || form.destructOn);
  const chosen = form.rows.flatMap((row) => row.roles);
  const warnings = [...botWarnings(guild, form.mode), ...roleWarnings(guild, chosen, rules)];
  const linkOptions = others.map((row) => ({ value: row.message_id, label: `#${channelName(guild, row.channel_id)}: ${row.rows.flatMap((r) => r.roles).map((id) => roleName(guild, id)).join(', ') || 'no roles'}` }));
  return html`<div>
    <${RowsEditor} rows=${form.rows} onChange=${(rows) => set('rows', rows)} mode=${form.mode} errors=${errors} linkButtons=${linkButtons} />
    ${warnings.map((text) => html`<${Notice} key=${text} kind="warn">${text}<//>`)}
    <hr class="rule" />
    <${Select} label="How members pick" value=${form.mode} options=${foreign ? MODES.filter((m) => m.value === 'reactions') : MODES} disabled=${foreign} onChange=${(v) => set('mode', v)} help=${modeHelp(form.mode)} error=${errors.mode} />
    ${form.mode === 'select' && html`<${TextInput} label="Menu prompt" value=${form.placeholder} placeholder="Pick a role" maxLength=${150} onChange=${(v) => set('placeholder', v)} error=${errors.placeholder} />`}
    <${Select} label="Message type" value=${form.type} options=${TYPES} onChange=${(v) => set('type', v)} help=${typeHelp(form.type)} error=${errors.type} />
    ${form.type === 'limit' && html`<${NumberInput} label="Most roles a member can hold from this message" min=${1} max=${250} value=${form.limit} onChange=${(v) => set('limit', v)} error=${errors.limit} />`}
    ${form.type === 'temp' && html`<${DurationInput} label="How long the role lasts" units=${['d', 'h', 'm']} value=${form.temp_seconds} onChange=${(v) => set('temp_seconds', v)} error=${errors.temp_seconds} />`}
    <hr class="rule" />
    <${RolePicker} label="Only members with one of these roles can pick up roles" multi value=${form.whitelist} onChange=${(v) => set('whitelist', v)} error=${errors.whitelist} />
    <${RolePicker} label="Members with one of these roles can't pick up roles" multi value=${form.blacklist} onChange=${(v) => set('blacklist', v)} error=${errors.blacklist} />
    <button type="button" class="btn ghost small" aria-expanded=${advanced} onClick=${() => setAdvanced(!advanced)}><${Icon} name=${advanced ? 'chevron-down' : 'chevron-right'} size=${14} /> More options</button>
    ${advanced && html`<div style=${{ marginTop: '12px' }}>
      <${Picker} label="Linked messages" help="A member can hold one role across this message and these, and picking another swaps them." multi placeholder="None" options=${linkOptions} value=${form.linked} onChange=${(v) => set('linked', v)} error=${errors.linked} />
      <${Switch} label="Delete the message and its reaction roles later" value=${form.destructOn} onChange=${(v) => set('destructOn', v)} help=${form.destructAt ? `Set to delete on ${when(form.destructAt)}.` : null} />
      ${form.destructOn && html`<${DurationInput} label="Delete it after" units=${['d', 'h', 'm']} value=${form.destructSeconds} onChange=${(v) => { set('destructSeconds', v); set('destructChanged', true); }} error=${errors.destruct_seconds} />`}
    </div>`}
  </div>`;
}

const blankForm = () => ({ type: 'normal', mode: 'reactions', limit: 1, temp_seconds: 3600, placeholder: '', whitelist: [], blacklist: [], rows: [], linked: [], destructOn: false, destructAt: null, destructSeconds: 86400, destructChanged: false });

function serverFields(form) {
  const out = { type: form.type, mode: form.mode, limit: form.limit, temp_seconds: form.temp_seconds, placeholder: form.placeholder, whitelist: form.whitelist, blacklist: form.blacklist, rows: rowsForServer(form.rows), linked: form.linked };
  if (form.destructOn && (form.destructChanged || !form.destructAt)) out.destruct_seconds = form.destructSeconds;
  if (!form.destructOn && form.destructAt) out.destruct_seconds = null;
  return out;
}

function showWarnings(warnings) {
  for (const text of warnings || []) toast(text, 'bad', 12000);
}

function NotMineNotice({ author }) {
  const guild = useGuild();
  return html`<${Notice} kind="warn">${author || 'Another bot'} wrote this message, so Discord won't let this bot change its text or add buttons to it. Reactions work the same. To edit the text, repost it from the <a href=${`/dashboard/${guild.id}/embeds`} data-nav="1">Embeds page</a> and its reaction roles move along.<//>`;
}

function ContentEditor({ payload, onChange, form, error }) {
  return html`<div>
    <h4>Content</h4>
    ${error && html`<div class="error" style=${{ marginBottom: '8px' }}>${error}</div>`}
    <div class="grid" style=${{ gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))' }}>
      <div style=${{ minWidth: 0 }}><${MessageEditor} value=${payload} onChange=${onChange} collapsed contentPlaceholder="What the message says" /></div>
      <div style=${{ minWidth: 0 }}>
        <label class="label">Preview</label>
        <${MessagePreview} value=${payload} below=${html`<${ControlsPreview} mode=${form.mode} rows=${form.rows} placeholder=${form.placeholder} />`} />
      </div>
    </div>
  </div>`;
}

function CreateModal({ data, onClose, onDone }) {
  const [how, setHow] = useState('post');
  const [channel, setChannel] = useState(null);
  const [messageId, setMessageId] = useState('');
  const [payload, setPayload] = useState(emptyMessage());
  const [form, setForm] = useState(blankForm());
  const [found, setFound] = useState(null);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const set = (key, value) => setForm((current) => ({ ...current, [key]: value }));

  useEffect(() => {
    setFound(null);
    if (how === 'post' || !channel) return undefined;
    if (how === 'id' && !/^\d{15,25}$/.test(messageId.trim())) return undefined;
    const query = how === 'latest' ? `channel_id=${channel}&latest=1` : `channel_id=${channel}&message_id=${messageId.trim()}`;
    let live = true;
    const timer = setTimeout(() => {
      api.get(`/reactionroles/lookup?${query}`)
        .then((row) => { if (live) setFound(row); })
        .catch((error) => { if (live) setFound({ error: error.message }); });
    }, 300);
    return () => { live = false; clearTimeout(timer); };
  }, [how, channel, messageId]);

  const foreign = how !== 'post' && !!found && !found.error && !found.mine;
  useEffect(() => { if (foreign) set('mode', 'reactions'); }, [foreign]);
  const linkButtons = how === 'post' ? (payload.buttons || []).length : ((found && found.payload && found.payload.buttons) || []).length;

  const create = async () => {
    setBusy(true);
    try {
      const result = await api.post('/reactionroles', { ...serverFields(form), create_mode: how, channel_id: channel, message_id: how === 'id' ? messageId.trim() : undefined, payload: how === 'post' ? payload : undefined });
      showWarnings(result.warnings);
      toast('Created.');
      onDone();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([how, channel, messageId, payload, form]);
  const [start] = useState(snapshot);
  return html`<${Modal} large icon="smile" title="Create reaction role" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn success" disabled=${busy} onClick=${create}>${busy ? 'Creating...' : 'Create'}</button></div>`}>
    <h4>Mode</h4>
    <${Select} ariaLabel="Mode" value=${how} options=${CREATE_MODES} onChange=${setHow} error=${errors.create_mode} />
    ${how === 'id' && html`<${TextInput} value=${messageId} placeholder="The message's ID" onChange=${setMessageId} error=${errors.message_id} />`}
    <${ChannelPicker} value=${channel} onChange=${setChannel} clearable=${false} error=${errors.channel_id} />
    ${how !== 'post' && found && found.error && html`<${Notice} kind="bad">${found.error}<//>`}
    ${how !== 'post' && found && found.tracked && html`<${Notice} kind="bad">That message already has reaction roles. Edit it from the table instead.<//>`}
    ${foreign && html`<${NotMineNotice} author=${found.author_name} />`}
    ${how !== 'post' && found && found.payload && html`<div style=${{ marginBottom: '16px' }}>
      <label class="label">The message</label><${MessagePreview} value=${found.payload} name=${found.author_name} /></div>`}
    <hr class="rule" />
    ${how === 'post' && html`<${ContentEditor} payload=${payload} onChange=${setPayload} form=${form} error=${errors.payload} /><p class="muted small">Write {roles} in the text and the bot lists each emoji and its roles there, and keeps the list up to date.</p><hr class="rule" />`}
    <${RoleSettings} form=${form} set=${set} errors=${errors} foreign=${foreign} rules=${data} linkButtons=${linkButtons} others=${data.rr} />
  <//>`;
}

function EditModal({ row, data, onClose, onDone }) {
  const guild = useGuild();
  const [message, setMessage] = useState(null);
  const [payload, setPayload] = useState(emptyMessage());
  const original = useRef('');
  const [form, setForm] = useState(() => ({
    ...blankForm(), type: row.type, mode: row.mode, limit: row.limit || 1, temp_seconds: row.temp_seconds || 3600, placeholder: row.placeholder, whitelist: row.whitelist, blacklist: row.blacklist, rows: withKeys(row.rows),
    linked: row.group_id ? data.rr.filter((other) => other.group_id && sameId(other.group_id, row.group_id) && !sameId(other.message_id, row.message_id)).map((other) => other.message_id) : [],
    destructOn: !!row.destruct_at, destructAt: row.destruct_at,
  }));
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const [moveTo, setMoveTo] = useState({ channel: null, id: '' });
  const set = (key, value) => setForm((current) => ({ ...current, [key]: value }));

  useEffect(() => {
    api.get(`/reactionroles/${row.message_id}/message`).then((result) => {
      setMessage(result);
      if (result.payload) {
        setPayload(result.payload);
        original.current = JSON.stringify(result.payload);
      }
    }).catch((error) => setMessage({ readonly: true, error: error.message, payload: null }));
  }, [row.message_id]);

  const readonly = !message || message.readonly;
  const save = async () => {
    setBusy(true);
    try {
      const body = serverFields(form);
      if (!readonly && JSON.stringify(payload) !== original.current) body.payload = payload;
      const result = await api.patch(`/reactionroles/${row.message_id}`, body);
      showWarnings(result.warnings);
      toast('Saved.');
      onDone();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    const sure = await confirmDialog({ title: 'Delete these reaction roles?', body: 'The message stays, and members keep the roles they have. Nobody can pick up or drop roles from it anymore.', confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      await api.del(`/reactionroles/${row.message_id}`);
      toast('Deleted.');
      onDone();
    } catch (error) {
      toastError(error);
    }
  };
  const fix = async () => {
    try {
      const result = await api.post(`/reactionroles/${row.message_id}/fix`);
      showWarnings(result.warnings);
      toast(form.mode === 'reactions' ? 'The bot put back any missing reactions.' : 'The bot put the controls back on the message.');
    } catch (error) {
      toastError(error);
    }
  };
  const move = async () => {
    const sure = await confirmDialog({ title: 'Move these reaction roles?', body: 'They go to the other message, with the same type and settings. The bot takes its reactions or controls off this one.', confirm: 'Move' });
    if (!sure) return;
    try {
      const result = await api.post(`/reactionroles/${row.message_id}/move`, { channel_id: moveTo.channel, message_id: moveTo.id.trim() });
      showWarnings(result.warnings);
      toast('Moved.');
      onDone();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    }
  };
  const others = data.rr.filter((other) => !sameId(other.message_id, row.message_id));
  const edited = !!original.current && JSON.stringify(payload) !== original.current;
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([form, edited ? payload : null, moveTo]);
  const [start] = useState(snapshot);
  return html`<${Modal} large icon="smile" title="Edit reaction role" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`
      <button class="icon-btn danger" title="Delete reaction roles" aria-label="Delete reaction roles" onClick=${remove}><${Icon} name="trash-2" size=${20} /></button>
      <div class="right">
        <button class="btn ghost" onClick=${onClose}>Cancel</button>
        <button class="btn primary" disabled=${busy} onClick=${save}>${busy ? 'Saving...' : 'Save'}</button></div>`}>
    <div class="spread" style=${{ marginBottom: '12px' }}>
      <span class="muted">In #${channelName(guild, row.channel_id)}</span>
      <a href=${row.url} target="_blank" rel="noopener noreferrer"><${Icon} name="external-link" size=${14} /> Open in Discord</a>
    </div>
    ${!message && html`<${Loading} text="Loading the message..." />`}
    ${message && message.error && html`<${Notice} kind="bad">${message.error}<//>`}
    ${message && message.payload && message.readonly && html`<${NotMineNotice} author=${message.author_name} />
      <label class="label">The message</label><div style=${{ marginBottom: '16px' }}><${MessagePreview} value=${message.payload} name=${message.author_name} /></div>`}
    ${message && !message.readonly && message.payload && html`<${ContentEditor} payload=${payload} onChange=${setPayload} form=${form} error=${errors.payload} />`}
    <hr class="rule" />
    <${RoleSettings} form=${form} set=${set} errors=${errors} foreign=${!!message && message.readonly} rules=${data} linkButtons=${(payload.buttons || []).length} others=${others} />
    <hr class="rule" />
    <div class="row" style=${{ marginBottom: '14px' }}>
      <button type="button" class="btn" onClick=${fix}><${Icon} name="refresh-cw" size=${14} /> ${form.mode === 'reactions' ? 'Put back missing reactions' : 'Put back the controls'}</button>
    </div>
    <h4>Move to another message</h4>
    <div class="input-row">
      <${ChannelPicker} value=${moveTo.channel} onChange=${(channel) => setMoveTo({ ...moveTo, channel })} error=${errors.channel_id} />
      <${TextInput} value=${moveTo.id} placeholder="The other message's ID" onChange=${(id) => setMoveTo({ ...moveTo, id })} error=${errors.message_id} />
    </div>
    <button type="button" class="btn small" disabled=${!moveTo.channel || !moveTo.id.trim()} onClick=${move}>Move</button>
  <//>`;
}

function ImportModal({ onClose, onDone }) {
  const [text, setText] = useState('');
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const answer = await api.post('/reactionroles/import', { text });
      setResult(answer);
      onDone();
    } catch (failure) {
      setError((failure.fields && failure.fields.text) || failure.message);
    } finally {
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([text]);
  const [start] = useState(snapshot);
  return html`<${Modal} icon="upload" title="Import reaction roles" onClose=${onClose} dirty=${!result && snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>${result ? 'Close' : 'Cancel'}</button>
      ${!result && html`<button class="btn primary" disabled=${busy || !text.trim()} onClick=${run}>${busy ? 'Importing...' : 'Import'}</button>`}</div>`}>
    ${result ? html`<div>
      <${Notice} kind="ok">Imported ${result.imported} messages with ${result.pairs} roles.<//>
      ${result.skipped.length > 0 && html`<h4>Skipped</h4>${result.skipped.map((row) => html`<p class="small" key=${row.message_id}><span class="mono">${row.message_id}</span>: ${row.reason}</p>`)}`}
      ${(result.warnings || []).length > 0 && html`<h4>Imported, with a problem</h4>${result.warnings.map((row, index) => html`<p class="small" key=${index}><span class="mono">${row.message_id}</span>: ${row.reason}</p>`)}`}
      <p class="muted small">Reactions already on these messages stay, and nobody's roles change.</p>
    </div>` : html`<div>
      <p class="muted">Paste the JSON another bot's dashboard returns for its reaction roles. The messages keep working where they are.</p>
      <${TextArea} rows=${10} value=${text} onChange=${setText} error=${error} counter=${false} placeholder='{"rr": [...]}' />
    </div>`}
  <//>`;
}

function RanksCard({ rules }) {
  const guild = useGuild();
  const form = useSettings('roles');
  return html`<${SettingsGate} form=${form}>${() => html`
    <${Card} icon="tags" title="Ranks" footer=${html`<${SaveButton} form=${form} />`}>
      <h2>Ranks</h2>
      <p class="muted">Members can give themselves these roles with /rank custom, or take them off again.</p>
      <${RolePicker} label="Roles members can give themselves" multi ...${form.bind('ranks')} />
      ${roleWarnings(guild, form.data.ranks, rules).map((text) => html`<${Notice} key=${text} kind="warn">${text}<//>`)}
    <//>
    <${SaveBar} form=${form} />`}<//>`;
}

export default function ReactionRolesPage() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  const [editing, setEditing] = useState(null);
  const load = () => api.get('/reactionroles').then(setData).catch((error) => { toastError(error); setData({ rr: [], elevated_roles: false, dangerous: [] }); });
  useEffect(() => { load(); }, [guild.id]);

  const roleText = (row) => row.rows.flatMap((r) => r.roles).map((id) => roleName(guild, id)).join(', ');
  const typeText = (row) => {
    if (row.type === 'limit') return `limit ${row.limit}`;
    if (row.type === 'temp') return `temp ${Math.round(row.temp_seconds / 60)} min`;
    return row.type;
  };
  const columns = [
    { key: 'channel', label: 'Channel', sort: true, filter: true, text: (row) => '#' + channelName(guild, row.channel_id) },
    { key: 'roles', label: 'Roles', filter: true, text: roleText, render: (row) => {
      const problems = data ? [...new Set([...botWarnings(guild, row.mode), ...roleWarnings(guild, row.rows.flatMap((r) => r.roles), data)])] : [];
      return html`<span>${problems.length > 0 && html`<span class="error" title=${problems.join(' ')} aria-label=${problems.join(' ')}><${Icon} name="triangle-alert" size=${14} /> </span>`}${roleText(row)}</span>`;
    } },
    { key: 'type', label: 'Type', sort: true, filter: true, width: '150px', text: typeText },
    { key: 'mode', label: 'How members pick', sort: true, width: '150px', text: (row) => (MODES.find((m) => m.value === row.mode) || MODES[0]).label + (row.mine ? '' : ' (another bot\'s message)') },
    { key: 'actions', label: '', width: '100px', render: (row) => html`<button class="btn outline block" onClick=${() => setEditing(row)}>Edit</button>` },
  ];
  const closeAndReload = () => { setCreating(false); setEditing(null); load(); };
  return html`<div style=${{ display: 'grid', gap: '18px' }}>
    <${Card} icon="smile" title="Reaction Roles">
      <div class="row">
        <button class="btn success" onClick=${() => setCreating(true)}><${Icon} name="plus" /> Create new reaction role</button>
        <button class="btn" onClick=${() => setImporting(true)}><${Icon} name="upload" /> Import</button>
      </div>
      <hr class="rule" />
      ${data === null ? html`<${Loading} text="Loading reaction roles..." />` : html`<${DataTable} rows=${data.rr} rowKey="message_id" columns=${columns} empty="No reaction roles yet. Create one above." />`}
    <//>
    ${data && html`<${RanksCard} rules=${data} />`}
    ${creating && data && html`<${CreateModal} data=${data} onClose=${() => setCreating(false)} onDone=${closeAndReload} />`}
    ${editing && data && html`<${EditModal} key=${String(editing.message_id)} row=${editing} data=${data} onClose=${() => setEditing(null)} onDone=${closeAndReload} />`}
    ${importing && html`<${ImportModal} onClose=${() => setImporting(false)} onDone=${load} />`}
  </div>`;
}
