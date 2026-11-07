import { html, useState } from '../lib.js';
import { api } from '../api.js';
import { ANY_CHANNEL, Card, ChannelPicker, Col, DurationInput, Grid, Icon, Modal, Notice, NumberInput, Picker, RolePicker, SaveBar, SaveButton, Select, SettingsGate, Switch, TextArea, toast, toastError, useSettings } from '../ui.js';

const DELETE = 1, WARN = 2, TEMPMUTE = 4, MUTE = 8, KICK = 16, TEMPBAN = 32, BAN = 64, DRAMA = 128, CHANNEL_MESSAGE = 256, DM_OFFENDER = 512, TIMEOUT = 1024;
const HEAVY = TEMPMUTE | MUTE | KICK | TEMPBAN | BAN | TIMEOUT;
const TIMED = TEMPMUTE | TEMPBAN | TIMEOUT;
const PUNISHMENTS = [
  { value: DELETE, label: 'Delete message' }, { value: WARN, label: 'Warn' }, { value: TEMPMUTE, label: 'Tempmute' }, { value: MUTE, label: 'Mute' },
  { value: KICK, label: 'Kick' }, { value: TEMPBAN, label: 'Tempban' }, { value: BAN, label: 'Ban' }, { value: TIMEOUT, label: 'Timeout' },
  { value: DRAMA, label: 'Post to drama channel' }, { value: CHANNEL_MESSAGE, label: 'Send message in channel' }, { value: DM_OFFENDER, label: 'Send message to offender' },
];
const RULES = ['invites', 'mentions', 'attachments', 'caps', 'honeypot', 'links', 'words', 'copies', 'spam', 'warns', 'names'];
const KINDS = [{ value: 'substring', label: 'Anywhere in a word' }, { value: 'word', label: 'Whole word' }, { value: 'regex', label: 'Regex' }];
const KIND_BADGE = { word: 'whole word', regex: 'regex' };

function Punishment({ form, rule, allowed = PUNISHMENTS }) {
  const path = `${rule}.punishment`;
  const value = form.data[rule].punishment;
  const picked = allowed.filter((p) => value.actions & p.value).map((p) => p.value);
  // A rule takes one heavy action, so the others leave the list once one is chosen.
  const heavyPicked = value.actions & HEAVY;
  const options = allowed.filter((p) => !(p.value & HEAVY) || !heavyPicked || (p.value & heavyPicked));
  const change = (list) => form.set(path, { ...value, actions: list.reduce((sum, bit) => sum + bit, 0) });
  const seconds = value.seconds || 0;
  return html`<div class="field">
    <${Picker} multi label="Punishments" options=${options} error=${form.errors[path]} value=${picked} onChange=${change} />
    ${value.actions & TIMED ? html`<${DurationInput} label="How long" units=${['d', 'h', 'm']} error=${seconds < 60 ? 'The shortest length is one minute.' : null} value=${seconds} onChange=${(next) => form.set(path, { ...value, seconds: next })} />` : ''}
    ${value.actions & (BAN | TEMPBAN) ? html`<${NumberInput} label="Default message purge days" addon="Days" min=${0} max=${7} error=${value.purge_days < 0 || value.purge_days > 7 ? 'Purge days must be between 0 and 7.' : null} value=${value.purge_days} onChange=${(next) => form.set(path, { ...value, purge_days: next === null ? 0 : next })} />` : ''}
  </div>`;
}

function Rate({ form, rule, noun, help }) {
  const [count, seconds] = form.data[rule].rate;
  const set = (index, next) => form.set(`${rule}.rate`, index === 0 ? [next === null ? 0 : next, seconds] : [count, next === null ? 0 : next]);
  return html`<div class="field">
    <label>Punish after</label>
    <div class="input-row">
      <div class="input-group"><input type="number" min="0" aria-label=${noun + ' count'} value=${count} onInput=${(e) => set(0, e.currentTarget.value === '' ? null : Number(e.currentTarget.value))} /><span class="addon">${noun} in</span></div>
      <div class="input-group"><input type="number" min="0" aria-label="Seconds" value=${seconds} onInput=${(e) => set(1, e.currentTarget.value === '' ? null : Number(e.currentTarget.value))} /><span class="addon">Seconds</span></div>
    </div>
    ${help && html`<div class="help">${help}</div>`}
    ${(!count || !seconds) && html`<div class="error"><${Icon} name="triangle-alert" size=${13} /> One or both of these are 0, so this never trips.</div>`}
    ${form.errors[`${rule}.rate`] && html`<div class="error">${form.errors[`${rule}.rate`]}</div>`}
  </div>`;
}

function RuleOptions({ form, rule, channels = true }) {
  const [open, setOpen] = useState(false);
  const data = form.data[rule];
  const count = data.exempt_channels.length + data.exempt_roles.length;
  return html`<div>
    <hr />
    <${Switch} label="Dry run" help="Log what would happen, and do nothing else." ...${form.bind(`${rule}.dry_run`)} />
    ${rule !== 'warns' && rule !== 'names' && html`<${NumberInput} label="Pause the sender" addon="seconds" min=${0} max=${59} help="A timeout this long on every hit, to break up a burst. It makes no case. 0 turns it off." ...${form.bind(`${rule}.pause_seconds`)} />`}
    <button type="button" class="btn ghost small" aria-expanded=${open} onClick=${() => setOpen(!open)}><${Icon} name=${open ? 'chevron-down' : 'chevron-right'} size=${14} /> Exemptions${count ? ` (${count})` : ''}</button>
    ${open && html`<div style=${{ marginTop: '10px' }}>
      ${channels && html`<${ChannelPicker} label="Exempt channels" multi categories types=${ANY_CHANNEL} ...${form.bind(`${rule}.exempt_channels`)} />`}
      <${RolePicker} label="Exempt roles" multi ...${form.bind(`${rule}.exempt_roles`)} />
    </div>`}
  </div>`;
}

function RuleCard({ form, rule, icon, title, enable, heading, help, children, punishments, after, channels }) {
  return html`<${Card} icon=${icon} title=${title} footer=${html`<${SaveButton} form=${form} />`}
    right=${html`<span class="small muted">${enable}</span><label class="switch"><input type="checkbox" role="switch" aria-label=${enable} checked=${form.data[rule].enabled} onChange=${(e) => form.set(`${rule}.enabled`, e.currentTarget.checked)} /><span></span></label>`}>
    <h2>${heading}</h2>
    ${help && html`<p class="muted">${help}</p>`}
    ${children}
    <${Punishment} form=${form} rule=${rule} allowed=${punishments} />
    ${after}
    <${RuleOptions} form=${form} rule=${rule} channels=${channels} />
  <//>`;
}

// A list of short text entries. Pasting several at once works: they split on spaces, commas and new lines.
function TagList({ label, help, placeholder, error, value, onChange }) {
  const [text, setText] = useState('');
  const add = () => {
    const parts = text.split(/[\s,]+/).map((part) => part.trim()).filter(Boolean);
    if (!parts.length) return;
    onChange([...new Set([...value, ...parts])]);
    setText('');
  };
  return html`<div class="field">
    <label>${label}</label>
    ${value.length > 0 && html`<div class="tag-list">${value.map((entry) => html`<span class="chip" key=${entry}>${entry}
      <button type="button" aria-label=${'Remove ' + entry} onClick=${() => onChange(value.filter((other) => other !== entry))}><${Icon} name="x" size=${12} /></button></span>`)}</div>`}
    <div class="input-group">
      <input type="text" placeholder=${placeholder} aria-label=${placeholder} value=${text} onInput=${(e) => setText(e.currentTarget.value)} onKeyDown=${(e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } }} />
      <button type="button" class="btn" onClick=${add}><${Icon} name="plus" /> Add</button>
    </div>
    ${help && html`<div class="help">${help}</div>`}
    ${error && html`<div class="error">${error}</div>`}
  </div>`;
}

// A list of thousands of words would make the page crawl, so it shows the first of them until asked for the rest.
const WORD_LIMIT = 200;
function WordList({ form }) {
  const entries = form.data.words.entries;
  const [text, setText] = useState('');
  const [kind, setKind] = useState('substring');
  const [filter, setFilter] = useState('');
  const [showAll, setShowAll] = useState(false);
  const add = async () => {
    const cleaned = text.trim();
    if (!cleaned) return;
    if (kind === 'regex') {
      try {
        const { problem } = await api.post('/automod/check-pattern', { pattern: cleaned });
        if (problem) {
          toast(problem, 'bad', 7000);
          return;
        }
      } catch (error) {
        toastError(error);
        return;
      }
    }
    if (!entries.some((e) => e.text.toLowerCase() === cleaned.toLowerCase() && e.kind === kind)) form.set('words.entries', [...entries, { text: cleaned, kind }]);
    setText('');
  };
  const remove = (entry) => form.set('words.entries', entries.filter((e) => e !== entry));
  const needle = filter.trim().toLowerCase();
  const matching = needle ? entries.filter((e) => e.text.toLowerCase().includes(needle)) : entries;
  const capped = !showAll && matching.length > WORD_LIMIT;
  const shown = capped ? matching.slice(0, WORD_LIMIT) : matching;
  return html`<div class="field">
    <label>Bad words</label>
    ${entries.length > 12 && html`<input type="search" placeholder="Filter this list" aria-label="Filter the bad word list" style=${{ marginBottom: '6px' }} value=${filter} onInput=${(e) => setFilter(e.currentTarget.value)} />`}
    ${entries.length > 0 && html`<div class="tag-list">${shown.map((entry) => html`<span class="chip" key=${entry.kind + entry.text}>${KIND_BADGE[entry.kind] && html`<span class="dim small">${KIND_BADGE[entry.kind]}</span>`} ${entry.text}
      <button type="button" aria-label=${'Remove ' + entry.text} onClick=${() => remove(entry)}><${Icon} name="x" size=${12} /></button></span>`)}</div>`}
    ${capped && html`<p class="muted small">Showing ${WORD_LIMIT} of ${matching.length}. <button type="button" class="link-btn" onClick=${() => setShowAll(true)}>Show all</button> or filter the list.</p>`}
    <div class="input-group">
      <input type="text" placeholder="Add word" aria-label="Add word" value=${text} onInput=${(e) => setText(e.currentTarget.value)} onKeyDown=${(e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } }} />
      <select aria-label="How to match it" class="word-kind" value=${kind} onChange=${(e) => setKind(e.currentTarget.value)}>${KINDS.map((k) => html`<option key=${k.value} value=${k.value}>${k.label}</option>`)}</select>
      <button type="button" class="btn" onClick=${add}><${Icon} name="plus" /> Add</button>
    </div>
    <div class="help">Case, accents and lookalike letters never matter, and neither do invisible characters or spaced-out letters such as b o a t. "Anywhere in a word" ignores punctuation too, so boat catches fredboat and b.o.a.t. "Whole word" leaves fredboat alone. A regex reads the message as typed and with those differences taken off. An entry of under five letters doesn't catch a word written wholly in another alphabet, so Russian words that look like pot or hot stay up. A regex that runs too long on a message doesn't judge that message, and shows in the log.</div>
    ${form.errors['words.entries'] && html`<div class="error">${form.errors['words.entries']}</div>`}
  </div>`;
}

// More limits past the first, such as a mute above three warnings and a ban above five.
function WarnSteps({ form, allowed }) {
  const steps = form.data.warns.steps;
  const change = (index, next) => form.set('warns.steps', steps.map((step, at) => (at === index ? next : step)));
  const add = () => form.set('warns.steps', [...steps, { threshold: (steps.length ? steps[steps.length - 1].threshold : form.data.warns.threshold) + 2, punishment: { actions: KICK, seconds: 0, purge_days: 0 } }]);
  return html`<div class="field">
    <label>More steps</label>
    ${steps.map((step, index) => html`<div class="field-edit" key=${index}>
      <div class="spread"><${NumberInput} label="Punish above" addon="Warnings" min=${1} max=${1000} value=${step.threshold} onChange=${(next) => change(index, { ...step, threshold: next === null ? 1 : next })} />
        <button type="button" class="icon-btn danger" aria-label=${'Remove the step above ' + step.threshold} onClick=${() => form.set('warns.steps', steps.filter((_, at) => at !== index))}><${Icon} name="trash-2" /></button></div>
      <${Picker} multi label="Punishments" options=${allowed} value=${allowed.filter((p) => step.punishment.actions & p.value).map((p) => p.value)} onChange=${(list) => change(index, { ...step, punishment: { ...step.punishment, actions: list.reduce((sum, bit) => sum + bit, 0) } })} />
      ${step.punishment.actions & TIMED ? html`<${DurationInput} label="How long" units=${['d', 'h', 'm']} value=${step.punishment.seconds || 0} onChange=${(next) => change(index, { ...step, punishment: { ...step.punishment, seconds: next } })} />` : ''}
    </div>`)}
    <button type="button" class="btn small" onClick=${add}><${Icon} name="plus" /> Add a step</button>
    <div class="help">The highest limit someone is over decides what happens.</div>
    ${form.errors['warns.steps'] && html`<div class="error">${form.errors['warns.steps']}</div>`}
  </div>`;
}

function Warnings({ form, moderation }) {
  const data = form.data;
  const rules = RULES.map((name) => data[name]).filter((rule) => rule.enabled);
  const uses = (bits) => rules.some((rule) => rule.punishment.actions & bits);
  const notes = [];
  if (uses(MUTE | TEMPMUTE) && moderation.data && !moderation.data.mute_role) notes.push('A rule mutes people, and no mute role is picked.');
  if (uses(DRAMA) && !data.drama_channel) notes.push('A rule posts to the drama channel, and none is picked.');
  if ((rules.some((rule) => rule.dry_run) || data.general_dry_run) && !data.log_channel) notes.push('Dry run shows its results in the log, and no log channel is picked.');
  if (rules.some((rule) => !rule.punishment.actions && !(rule.steps && rule.steps.length))) notes.push('A rule is on with no punishment, so it only writes to the log.');
  if (!notes.length) return null;
  return html`<${Notice} kind="warn">${notes.map((line) => html`<div key=${line}>${line}</div>`)}<//>`;
}

function ImportDialog({ onClose, onDone }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      await api.post('/automod/import', { code });
      toast('Automod settings are loaded.');
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
    <p>Paste an automod settings export as JSON. What it holds replaces what's here. A channel or role that isn't in this server is left out, and a rule that was never set up stays off.</p>
    <${TextArea} label="Settings as JSON" rows=${8} value=${code} onChange=${setCode} />
  <//>`;
}

function GeneralCard({ form, moderation, both, onImport }) {
  return html`<${Card} icon="shield" title="Automod" footer=${html`<${SaveButton} form=${both} /><button class="btn ghost right" onClick=${onImport}><${Icon} name="upload" /> Import settings</button>`}>
    <h2>General settings</h2>
    <p class="muted">The bot manager role and members with <code>Manage Server</code>, <code>Administrator</code>, <code>Ban Members</code>, <code>Kick Members</code> or <code>Manage Messages</code> are always immune, so test these rules from another account. <code>/automod server</code> lists what's on.</p>
    <${Warnings} form=${form} moderation=${moderation} />
    <${ChannelPicker} label="Log actions to" ...${form.bind('log_channel')} />
    <${ChannelPicker} label="Drama channel" help="Incidents post here with Ban, Kick, Mute and Dismiss buttons, so staff can decide without digging through logs." ...${form.bind('drama_channel')} />
    ${moderation.data && html`<${RolePicker} label="Mute role" placeholder="No mute role selected" ...${moderation.bind('mute_role')} />`}
    <${ChannelPicker} label="Allowed channels/categories" multi categories types=${ANY_CHANNEL} help="Automod leaves these alone." ...${form.bind('allowed_channels')} />
    <${RolePicker} label="Allowed roles" help="Automod leaves members with these roles alone." multi ...${form.bind('allowed_roles')} />
    <${ChannelPicker} label="Media only channels" help="Messages here need a file, an embed or a link. Anything else is deleted. Threads started under one of these channels are left alone." multi categories types=${ANY_CHANNEL} ...${form.bind('media_channels')} />
    <${Switch} label="Delete unsafe files" help="Keeps png, jpg, jpeg, gif, svg, bmp, tif, tiff, webp, webm, mp4, mov, pdf, txt, mp3, flac, wav and ogg. Other file types are deleted." ...${form.bind('delete_files')} />
    <${Switch} label="Dry run for these two" help="Log what media-only channels and unsafe files would delete, and delete nothing." ...${form.bind('general_dry_run')} />
  <//>`;
}

// Importing changes the settings on the server, so the form starts again from what's stored.
export default function AutomodPage() {
  const [round, setRound] = useState(0);
  return html`<${AutomodForm} key=${round} reload=${() => setRound((n) => n + 1)} />`;
}

function AutomodForm({ reload }) {
  const [importing, setImporting] = useState(false);
  const form = useSettings('automod');
  const moderation = useSettings('moderation');
  // The mute role lives with moderation, and this page shows it too, so saving here covers both.
  const both = {
    get dirty() { return form.dirty || moderation.dirty; },
    saving: form.saving || moderation.saving,
    save: async () => {
      const saved = await form.save();
      if (!moderation.dirty) return saved;
      const before = moderation.saved ? moderation.saved.mute_role : null;
      const both_saved = (await moderation.save()) && saved;
      if (both_saved && moderation.data.mute_role && String(moderation.data.mute_role) !== String(before)) {
        try {
          const { channels } = await api.post('/moderation/mute-role/apply', {});
          toast(`Setting the mute role's limits in ${channels} channels. Big servers take a minute.`);
        } catch (error) {
          toastError(error);
        }
      }
      return both_saved;
    },
    reset: () => { form.reset(); if (moderation.data) moderation.reset(); },
  };
  const threshold = PUNISHMENTS.filter((p) => ![DELETE, WARN, CHANNEL_MESSAGE].includes(p.value));
  const noMessage = PUNISHMENTS.filter((p) => ![DELETE, CHANNEL_MESSAGE].includes(p.value));
  return html`<${SettingsGate} form=${form}>${() => html`
    <${Grid}>
      <${Col}>
        <${GeneralCard} form=${form} moderation=${moderation} both=${both} onImport=${() => setImporting(true)} />
        <${RuleCard} form=${form} rule="invites" icon="send" title="Invite spam" enable="Enable invite spam" heading="Bad invites" help="Catches invites to other servers. Invites to this server never count.">
          <${Switch} label="No-role mode" help="Only members without a role are punished." ...${form.bind('invites.no_role')} />
          <${TagList} label="Allowed invites" placeholder="Invite link, code or server ID" help="Invites to these servers don't count." error=${form.errors['invites.whitelist']}
            value=${form.data.invites.whitelist} onChange=${(list) => form.set('invites.whitelist', list)} />
        <//>
        <${RuleCard} form=${form} rule="mentions" icon="bell" title="Mention spam" enable="Enable mention spam" heading="Mention spam"
          after=${html`<${Rate} form=${form} rule="mentions" noun="Mentions" help="A member who reaches this many in this many seconds is punished." />`} />
        <${RuleCard} form=${form} rule="attachments" icon="image" title="Attachment spam" enable="Enable attachment spam" heading="Attachment spam"
          after=${html`<${Rate} form=${form} rule="attachments" noun="Files" help="A member can post this many in this many seconds. The next is punished." />`} />
        <${RuleCard} form=${form} rule="caps" icon="x" title="Caps" enable="Enable caps" heading="Caps threshold"
          after=${html`<${NumberInput} label="Punish at" addon="% capital letters" min=${0} max=${100} help="Messages under six letters don't count." ...${form.bind('caps.threshold')} />`} />
      <//>
      <${Col}>
        <${RuleCard} form=${form} rule="honeypot" icon="eye" title="Honeypot" enable="Enable honeypot" heading="Honeypot" help="Anyone who posts in these channels draws the punishment. Spam accounts post everywhere, and people don't post in a channel marked as a trap.">
          <${ChannelPicker} label="Honeypot channels" multi categories types=${ANY_CHANNEL} ...${form.bind('honeypot.channels')} />
        <//>
        <${RuleCard} form=${form} rule="links" icon="link" title="Link spam" enable="Enable link spam" heading="Bad links"
          after=${html`<${Rate} form=${form} rule="links" noun="Links" help="1 in 1 punishes the first link that counts." />`}>
          <${Select} label="Link spam mode" options=${[{ value: 'blacklist', label: 'Blacklist: only these sites count' }, { value: 'whitelist', label: 'Whitelist: every other site counts' }, { value: 'no_role', label: 'No roles: links from members without a role count' }]} ...${form.bind('links.mode')} />
          ${form.data.links.mode === 'blacklist' && html`<${TagList} label="Blacklisted domains" placeholder="Add link" help="A subdomain counts too. A site typed without https:// still counts." error=${form.errors['links.blacklist']}
            value=${form.data.links.blacklist} onChange=${(list) => form.set('links.blacklist', list)} />`}
          ${form.data.links.mode !== 'blacklist' && html`<${TagList} label="Whitelisted domains" placeholder="Add link" help="Links to these sites never count. A subdomain counts too, and example.com/docs covers only that part of the site." error=${form.errors['links.whitelist']}
            value=${form.data.links.whitelist} onChange=${(list) => form.set('links.whitelist', list)} />`}
        <//>
        <${RuleCard} form=${form} rule="words" icon="message-square" title="Bad words" enable="Enable bad words" heading="Bad words">
          <${WordList} form=${form} />
        <//>
        <${RuleCard} form=${form} rule="spam" icon="message-circle" title="Slowmode" enable="Enable slowmode" heading="Spam" help="A message over the limit is deleted even if the punishment doesn't say delete. A message in channel disappears after five seconds."
          after=${html`<${Rate} form=${form} rule="spam" noun="Messages" help="A member can post this many in this many seconds. The next is punished." />`} />
        <${RuleCard} form=${form} rule="warns" icon="triangle-alert" title="Warn" enable="Enable warn" heading="Warn threshold" punishments=${threshold} channels=${false}
          help="Warnings never expire on their own. Past the limit, each new warning draws the punishment again, until someone removes warnings."
          after=${html`<${NumberInput} label="Punish above" addon="Warnings" min=${0} help="0 leaves only the steps below." ...${form.bind('warns.threshold')} /><${WarnSteps} form=${form} allowed=${threshold} />`} />
        <${RuleCard} form=${form} rule="copies" icon="copy" title="Copied text" enable="Enable copied text" heading="The same text from several accounts" help="Raid accounts take turns to stay under a per-person limit. This counts one text in one channel, whoever posts it, and deleting takes the earlier copies too. Text under eight letters doesn't count."
          after=${html`<${Rate} form=${form} rule="copies" noun="Copies" help="The post that reaches this many in this many seconds is punished." />`} />
        <${RuleCard} form=${form} rule="names" icon="user-x" title="Bad names" enable="Enable bad names" heading="Names" punishments=${noMessage} channels=${false}
          help="Reads usernames and nicknames against the bad word list above, when someone joins and when they change one." />
      <//>
    <//>
    ${importing && html`<${ImportDialog} onClose=${() => setImporting(false)} onDone=${reload} />`}
    <${SaveBar} form=${both} />`}<//>`;
}
