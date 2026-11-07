import { html, useState } from '../lib.js';
import { api } from '../api.js';
import { MessageEditor, MessagePreview, sampleMessage } from '../embed.js';
import { Card, ChannelPicker, Check, Col, DurationInput, Grid, Icon, Modal, NumberInput, Notice, RolePicker, SaveBar, SaveButton, Select, SettingsGate, TextArea, toast, toastError, useGuild, useSettings } from '../ui.js';

const MODES = [
  { value: 0, label: 'Do not DM the offender' },
  { value: 1, label: 'Send server and action' },
  { value: 2, label: 'Send server, action and reason' },
  { value: 3, label: 'Send server, action, reason and moderator' },
  { value: 4, label: 'Send a custom message' },
];

const PHRASES = {
  timeout: ['timed out in', true], mute: ['muted in', true], kick: ['kicked from', false], ban: ['banned from', false],
  warn: ['warned in', false], hardmute: ['muted in', true], tempban: ['banned from', true], softban: ['softbanned from', false],
};

const EVENTS = [
  ['ban', 'Bans'], ['mute', 'Mutes'], ['warn', 'Warnings'], ['tempban', 'Tempbans'], ['hardmute', 'Hardmutes'],
  ['kick', 'Kicks'], ['unmute', 'Unmutes'], ['unban', 'Unbans'], ['timeout', 'Timeout'], ['untimeout', 'Remove Timeout'],
];

const DANGEROUS = ['administrator', 'manage_guild', 'manage_channels', 'manage_roles', 'manage_messages', 'manage_expressions', 'manage_nicknames', 'manage_webhooks', 'ban_members', 'kick_members', 'moderate_members'];

function example(action, mode, guild) {
  if (mode === 0) return 'They get no message.';
  if (mode === 4) return 'What you write below.';
  const [phrase, timed] = PHRASES[action];
  let line = `You were ${phrase} ${guild.name}${timed ? ' for 20 minutes' : ''}.`;
  if (mode >= 2) line += " Reason: 'Posting ads'.";
  if (mode === 3) line += ` Moderator: ${guild.me.name} (id: ${guild.me.id}).`;
  return line;
}

function ActionCard({ form, action, icon, title, note, durationLabel, durationHelp, daysLabel }) {
  const guild = useGuild();
  const settings = form.data[action];
  const messageError = form.errors[`${action}.message`];
  return html`<${Card} icon=${icon} title=${title} footer=${html`<${SaveButton} form=${form} />`}>
    <h4>${title}</h4>
    ${note && html`<p class="muted">${note}</p>`}
    <${Select} label="Direct message" options=${MODES} ...${form.bind(`${action}.mode`)} />
    <p class="small"><b>Example:</b> ${example(action, settings.mode, guild)}</p>
    ${settings.mode === 4 && html`<div>
      <h4>Custom message</h4>
      <p class="muted small">Templates work here, plus these: <code>{reason}</code> is the reason, or N/A without one. <code>{moderator}</code> is who did it. <code>{offender}</code> is who it happened to. <code>{duration}</code> is how long, such as 3 hours and 24 minutes, and empty when there's no end.</p>
      <${MessageEditor} collapsed value=${settings.message} contentPlaceholder=${`Enter a ${action} message`} onChange=${(message) => form.set(`${action}.message`, message)} />
      ${messageError && html`<div class="error">${messageError}</div>`}
      <div class="field"><label>What the offender will see</label><${MessagePreview} value=${sampleMessage(settings.message, { reason: 'Spamming links', moderator: 'A moderator', offender: 'Sample Member', duration: '3 hours' })} /></div>
    </div>`}
    ${durationLabel && html`<hr /><${DurationInput} label=${durationLabel} help=${durationHelp} units=${['h', 'm']} ...${form.bind(`${action}.seconds`)} />`}
    ${daysLabel && html`<${NumberInput} label=${daysLabel} addon="Days" min=${0} max=${7} ...${form.bind(`${action}.days`)} />`}
  <//>`;
}

function ImportDialog({ onClose, onDone }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      await api.post('/moderation/import', { code });
      toast('Moderation settings are loaded.');
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
    <p>Paste a moderation settings export as JSON. It fills in what it holds and leaves everything else as it is. A channel or role that isn't in this server is left out.</p>
    <${TextArea} label="Settings as JSON" rows=${8} value=${code} onChange=${setCode} />
  <//>`;
}

function GeneralCard({ form, reports, both, onImport }) {
  const guild = useGuild();
  const modlog = form.data.modlog_channel;
  const missing = modlog && !guild.channels.some((c) => String(c.id) === String(modlog));
  const muteRole = guild.roles.find((r) => String(r.id) === String(form.data.mute_role));
  const muteProblem = muteRole && (muteRole.position >= guild.me.top_role_position
    ? 'The mute role sits at or above the bot, so mutes will fail. Move the bot above it.'
    : (muteRole.permissions.some((p) => DANGEROUS.includes(p)) ? 'The mute role holds moderator permissions, so mutes refuse to use it.' : null));
  return html`<${Card} icon="gavel" title="General moderation settings" footer=${html`<${SaveButton} form=${both} /><button class="btn ghost right" onClick=${onImport}><${Icon} name="upload" /> Import settings</button>`}>
    <h2>Moderation settings</h2>
    ${missing && html`<${Notice} kind="warn">The modlog channel no longer exists. Cases are stored and don't post anywhere until you pick another.<//>`}
    <${ChannelPicker} label="Modlogs channel" ...${form.bind('modlog_channel')} />
    ${reports.data && html`<${ChannelPicker} label="Send reports to" ...${reports.bind('report_channel')} />`}
    ${muteProblem && html`<${Notice} kind="warn">${muteProblem}<//>`}
    <${RolePicker} label="Mute role" help="Mutes hand out this role. Saving a new one sets its limits in every channel, and /muterole update does that again later." ...${form.bind('mute_role')} />
    <${NumberInput} label="Start case numbers at" help="New cases continue from the highest number already used, or this number if it's higher. Moving from another modlog? Set this past its last case." min=${1} ...${form.bind('case_start')} />
    <h4>Logged events</h4>
    <p class="muted small">These post once a modlog channel is picked. Every case is stored either way.</p>
    <div class="checklist">
      ${EVENTS.map(([key, label]) => html`<${Check} key=${key} label=${label} value=${form.data.logged[key]} onChange=${(on) => form.set(`logged.${key}`, on)} />`)}
    </div>
  <//>`;
}

// Importing changes the settings on the server, so the form starts again from what's stored.
export default function ModerationPage() {
  const [round, setRound] = useState(0);
  return html`<${ModerationForm} key=${round} reload=${() => setRound((n) => n + 1)} />`;
}

function ModerationForm({ reload }) {
  const [importing, setImporting] = useState(false);
  const form = useSettings('moderation');
  const reports = useSettings('reports');
  // The general card holds one field from the reports feature, so its save covers both.
  const both = {
    get dirty() { return form.dirty || reports.dirty; },
    saving: form.saving || reports.saving,
    save: async () => {
      const before = form.saved ? form.saved.mute_role : null;
      const saved = await form.save();
      if (saved && form.data.mute_role && String(form.data.mute_role) !== String(before)) {
        try {
          const { channels } = await api.post('/moderation/mute-role/apply', {});
          toast(`Setting the mute role's limits in ${channels} channels. Big servers take a minute.`);
        } catch (error) {
          toastError(error);
        }
      }
      return reports.dirty ? (await reports.save()) && saved : saved;
    },
    reset: () => { form.reset(); if (reports.data) reports.reset(); },
  };
  return html`<${SettingsGate} form=${form}>${() => html`
    <${Grid}>
      <${Col}>
        <${GeneralCard} form=${form} reports=${reports} both=${both} onImport=${() => setImporting(true)} />
        <${ActionCard} form=${form} action="timeout" icon="timer" title="Timeout settings" durationLabel="Default timeout length" durationHelp="Discord allows up to 28 days." />
        <${ActionCard} form=${form} action="mute" icon="mic-off" title="Mute settings" durationLabel="Default mute length" durationHelp="0 lasts until someone lifts it." />
        <${ActionCard} form=${form} action="kick" icon="user-minus" title="Kick settings" />
        <${ActionCard} form=${form} action="softban" icon="user-x" title="Softban settings" note="A softban bans and unbans at once, which removes someone and clears their recent messages." daysLabel="Default message purge days" />
      <//>
      <${Col}>
        <${ActionCard} form=${form} action="ban" icon="ban" title="Ban settings" daysLabel="Default ban message purge days" />
        <${ActionCard} form=${form} action="warn" icon="triangle-alert" title="Warn settings" />
        <${ActionCard} form=${form} action="hardmute" icon="volume-x" title="Hardmute settings" note="A hardmute also takes every other role away, and gives them back when it ends." durationLabel="Default hardmute length" durationHelp="0 lasts until someone lifts it." />
        <${ActionCard} form=${form} action="tempban" icon="clock" title="Tempban settings" durationLabel="Default tempban length" daysLabel="Default tempban message purge days" />
      <//>
    <//>
    ${importing && html`<${ImportDialog} onClose=${() => setImporting(false)} onDone=${reload} />`}
    <${SaveBar} form=${both} />`}<//>`;
}
