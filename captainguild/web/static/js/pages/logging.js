import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Col, Grid, Icon, Loading, Modal, Notice, NumberInput, PeopleField, Picker, RolePicker, SaveBar, SaveButton, SettingsGate, Switch, TextArea, channelName, confirmDialog, copyText, toast, toastError, useGuild, useSettings } from '../ui.js';

const CHANNEL_TYPES = [0, 2, 5, 13, 15, 16];

function PrefixField({ value, onChange, error }) {
  const [text, setText] = useState('');
  const prefixes = value || [];
  const add = () => {
    if (!text || prefixes.includes(text)) { setText(''); return; }
    onChange([...prefixes, text]);
    setText('');
  };
  return html`<div class="field">
    <label>Ignored prefixes</label>
    ${prefixes.length > 0 && html`<div class="row log-chips">${prefixes.map((prefix) => html`<span class="chip" key=${prefix}><code>${prefix}</code>
      <button type="button" aria-label=${'Remove ' + prefix} onClick=${() => onChange(prefixes.filter((p) => p !== prefix))}><${Icon} name="x" size=${12} /></button></span>`)}</div>`}
    <div class="input-group">
      <input type="text" maxlength="32" placeholder="Text a message starts with" aria-label="Prefix to ignore" value=${text}
        onInput=${(e) => setText(e.currentTarget.value)} onKeyDown=${(e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } }} />
      <button type="button" class="btn" onClick=${add}>Add</button>
    </div>
    <div class="help">Deletes, edits and purged messages that start with this text aren't logged, which keeps other bots' commands out.</div>
    ${error && html`<div class="error">${error}</div>`}
  </div>`;
}

function EventCard({ group, form, icon }) {
  const events = form.data.events;
  const keys = group.events.map((e) => e.key);
  const allOn = keys.every((k) => events.includes(k));
  const setAll = (on) => form.set('events', on ? [...new Set([...events, ...keys])] : events.filter((k) => !keys.includes(k)));
  const flip = (key, on) => form.set('events', on ? [...events, key] : events.filter((k) => k !== key));
  return html`<${Card} icon=${icon} title=${group.title} right=${html`<button type="button" class="btn ghost small" onClick=${() => setAll(!allOn)}>${allOn ? 'All off' : 'All on'}</button>`} footer=${html`<${SaveButton} form=${form} />`}>
    <h2>${group.title}</h2>
    <p class="muted">${group.help}</p>
    <div class="log-switches">
      ${group.events.map((event) => html`<${Switch} key=${event.key} label=${event.label} value=${events.includes(event.key)} onChange=${(on) => flip(event.key, on)} />`)}
    </div>
  <//>`;
}

function ImportDialog({ onClose, onDone }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      await api.post('/logging/import', { code });
      toast('Logging settings are loaded.');
      onDone();
    } catch (error) {
      toastError(error);
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([code]);
  const [start] = useState(snapshot);
  return html`<${Modal} title="Import settings" icon="upload" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn primary" disabled=${busy || !code.trim()} onClick=${run}>${busy ? 'Importing...' : 'Import'}</button></div>`}>
    <p>This replaces the settings the code holds. A channel or role from another server is dropped.</p>
    <${TextArea} label="Settings code" rows=${5} value=${code} onChange=${setCode} placeholder="Paste a code from the export here" />
  <//>`;
}

// Creating channels and importing change the settings on the server, so the form starts again from what's stored.
export default function LoggingPage() {
  const [round, setRound] = useState(0);
  return html`<${LoggingForm} key=${round} reload=${() => setRound((n) => n + 1)} />`;
}

function LoggingForm({ reload }) {
  const guild = useGuild();
  const form = useSettings('logging');
  const [catalog, setCatalog] = useState(null);
  const [bots, setBots] = useState([]);
  const [importing, setImporting] = useState(false);
  const [building, setBuilding] = useState(false);
  const [problems, setProblems] = useState([]);
  const [testing, setTesting] = useState(false);
  const [tested, setTested] = useState(null);

  useEffect(() => {
    api.get('/logging/catalog').then(setCatalog).catch(toastError);
    if (guild.access === 'full') {
      api.get('/logging/bots').then(setBots).catch(toastError);
      api.get('/logging/status').then((answer) => setProblems(answer.problems)).catch(toastError);
    }
  }, [guild.id]);

  const buildChannels = async () => {
    if (form.dirty) {
      toast('Save or reset your changes first.', 'bad');
      return;
    }
    const sure = await confirmDialog({
      title: 'Create log channels?',
      body: 'This creates a Logs category with five channels, hidden from everyone but the bot and your staff roles, and points message, member, join, server and voice logs at them.',
      confirm: 'Create channels',
    });
    if (!sure) return;
    setBuilding(true);
    try {
      await api.post('/logging/aio');
      await guild.reload();
      toast('The log channels are ready.');
      reload();
    } catch (error) {
      toastError(error);
      setBuilding(false);
    }
  };
  const sendTest = async () => {
    if (form.dirty) {
      toast('Save or reset your changes first.', 'bad');
      return;
    }
    setTesting(true);
    try {
      const { results } = await api.post('/logging/test');
      setTested(results);
      if (!results.length) toast('No log channels are set yet.', 'bad');
    } catch (error) {
      toastError(error);
    } finally {
      setTesting(false);
    }
  };
  const exportCode = async () => {
    try {
      const { code } = await api.get('/logging/export');
      copyText(code);
    } catch (error) {
      toastError(error);
    }
  };

  return html`<${SettingsGate} form=${form}>${() => {
    if (!catalog) return html`<${Loading} />`;
    const group = (key) => catalog.groups.find((g) => g.key === key);
    const permissions = guild.me.permissions;
    const has = (name) => permissions.includes('administrator') || permissions.includes(name);
    const channels = [
      ['default_channel', 'Default log channel'], ['member_channel', 'Member log channel'], ['server_channel', 'Server log channel'],
      ['voice_channel', 'Voice log channel'], ['message_channel', 'Message log channel'], ['join_channel', 'Join/leave log channel'],
    ];
    const ownChannels = [
      ['moderator_delete_channel', 'Moderator delete log channel', 'Deletes where the audit log names a moderator or bot, and purges. Leave it empty to keep them in the message log channel.'],
      ['role_channel', 'Role log channel', "Members' role changes. Leave it empty to keep them in the member log channel."],
      ['name_channel', 'Name log channel', 'Name and nickname changes. Leave it empty to keep them in the member log channel.'],
      ['timeout_channel', 'Timeout log channel', 'Timeouts and lifted timeouts. Leave it empty to keep them in the member log channel.'],
    ];
    return html`
    ${!has('view_audit_log') && html`<${Notice} kind="warn">Without View Audit Log, entries can't say who did something, and kicks, prunes, pins, bot and integration changes and moderator voice moves aren't logged at all. Give the bot's role that permission.<//>`}
    ${!has('manage_webhooks') && html`<${Notice} kind="warn">The bot doesn't have Manage Webhooks, so it posts entries as itself and can't group as many together.<//>`}
    ${problems.map((problem) => html`<${Notice} key=${problem.field} kind="warn">${problem.text}<//>`)}
    <${Grid}>
      <${Col}>
        <${Card} icon="list" title="Channel selection" footer=${html`<${SaveButton} form=${form} />
            <button type="button" class="btn right" disabled=${testing} onClick=${sendTest}><${Icon} name="send" /> ${testing ? 'Sending...' : 'Send a test entry'}</button>
            <button type="button" class="btn success" disabled=${building} onClick=${buildChannels}><${Icon} name="plus" /> ${building ? 'Creating...' : 'Create log channels'}</button>`}>
          <h2>Channel selection</h2>
          <p class="muted">Every event goes to the default log channel unless its own channel is set here. A busy server is easier to read with each kind of event in its own channel. The cards below show which events share a channel.</p>
          ${channels.map(([field, label]) => html`<${ChannelPicker} key=${field} label=${label} placeholder="No channel selected" ...${form.bind(field)} />`)}
          ${ownChannels.map(([field, label, help]) => html`<${ChannelPicker} key=${field} label=${label} placeholder="No channel selected" help=${help} ...${form.bind(field)} />`)}
          ${tested && tested.map((row) => html`<${Notice} key=${String(row.channel_id)} kind=${row.ok ? 'ok' : 'bad'}>#${channelName(guild, row.channel_id)}: ${row.ok ? 'the test entry went out.' : row.error}<//>`)}
        <//>
        <${EventCard} group=${group('message')} form=${form} icon="message-square" />
        <${EventCard} group=${group('join')} form=${form} icon="door-open" />
        <${Card} icon="eye-off" title="Ignored" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Ignored</h2>
          <p class="muted">Nothing on these lists is logged. That covers messages and also server events: a change to an ignored channel, or anything an ignored member does, stays out of the log.</p>
          <${ChannelPicker} label="Ignored channels" types=${CHANNEL_TYPES} categories multi ...${form.bind('ignored_channels')}
            help="Threads follow their channel, and channels follow their category." />
          <${PeopleField} label="Ignored members" ...${form.bind('ignored_members')} help="Their messages, their member events and the changes they make aren't logged." />
          <${RolePicker} label="Ignored roles" multi ...${form.bind('ignored_roles')} help="Giving, removing or changing these roles isn't logged." />
          <${PrefixField} ...${form.bind('ignored_prefixes')} />
          <${Picker} label="Bots whose channel renames are ignored" multi options=${bots.map((b) => ({ value: b.id, label: b.display_name }))}
            placeholder="Select bots" ...${form.bind('rename_ignored_bots')}
            help="Stat counter bots rename voice channels all day, which buries everything else in the server log. A rename by one of these isn't logged. Anything else they change still is." />
        <//>
      <//>
      <${Col}>
        <${EventCard} group=${group('server')} form=${form} icon="server" />
        <${EventCard} group=${group('member')} form=${form} icon="users" />
        <${EventCard} group=${group('voice')} form=${form} icon="volume-2" />
        <${Card} icon="shield-check" title="Details" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Details</h2>
          <${Switch} label="Log actions by bots" ...${form.bind('log_bot_actions')}
            help="When this is off, role changes, nickname changes, timeouts, deletes and purges made by a bot aren't logged, and neither are bots deleting their own messages. Entries name whoever made a change either way." />
          <${Switch} label="Copy deleted images into the log" ...${form.bind('attach_deleted_images')}
            help="Discord stops serving an image soon after it's deleted, so the bot fetches it right away and attaches it to the entry." />
          <${NumberInput} label="Warn about accounts newer than" addon="days" min=${0} max=${365} ...${form.bind('new_account_days')}
            help="A member joining with a newer account gets a warning in the join log. Set it to 0 for none." />
        <//>
        <${Card} icon="database" title="Storage" footer=${html`<${SaveButton} form=${form} />
            <button type="button" class="btn right" onClick=${exportCode}><${Icon} name="copy" /> Copy settings code</button>
            <button type="button" class="btn" onClick=${() => setImporting(true)}><${Icon} name="upload" /> Import</button>`}>
          <h2>Storage</h2>
          <p class="muted">Kept messages give edits their before text and deletes their text. Kept entries are what <a href=${`/dashboard/${guild.id}/logsearch`} data-nav="1">Log search</a> searches. An entry for a deleted, edited, purged or pinned message holds that message's text, and stays for as long as entries are kept.</p>
          <${NumberInput} label="Keep messages for" addon="days" min=${1} max=${365} ...${form.bind('message_retention_days')} />
          <${NumberInput} label="Keep log entries for" addon="days" min=${1} max=${730} ...${form.bind('log_retention_days')} />
        <//>
      <//>
    <//>
    ${importing && html`<${ImportDialog} onClose=${() => setImporting(false)} onDone=${reload} />`}
    <${SaveBar} form=${form} />`;
  }}<//>`;
}
