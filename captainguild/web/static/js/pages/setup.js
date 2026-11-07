import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { MessagePreview } from '../embed.js';
import { Card, ChannelPicker, Icon, Loading, Notice, PREFIX_MAX, RolePicker, TextArea, TextInput, prefixProblem, toast, toastError, useGuild } from '../ui.js';

const WELCOME_PRESET = 'Welcome to {server}, {user}! Make yourself at home.';
const STEPS = [
  { key: 'general', title: 'General', needs: null },
  { key: 'welcome', title: 'Welcome message', needs: 'greetings' },
  { key: 'reactionroles', title: 'Reaction roles', needs: 'reactionroles' },
  { key: 'moderation', title: 'Moderation', needs: 'moderation' },
  { key: 'automod', title: 'Automod', needs: 'automod' },
];

// Shows the message the way a new member's greeting would read, with the variables filled in with sample names.
function sampleText(text, guild) {
  return text.split('{user}').join('@New Member').split('{server}').join(guild.name);
}

// The Previous and Next buttons. This is one component for every step, so a failed save leaves focus on the button that was pressed.
// A step that is still working on something passes hold, which keeps the person from moving on until it finishes.
function StepFooter({ step, save, hold }) {
  const { index, total, busy } = step;
  return html`<div style=${{ marginTop: '18px' }}><div class="row">
    ${index > 0 && html`<button class="btn ghost" disabled=${busy || hold} onClick=${step.back}>Previous step</button>`}
    <button class="btn primary right" disabled=${busy || hold} onClick=${() => step.go(save)}>${busy ? 'Saving...' : (index === total - 1 ? 'Finish' : 'Next')}</button>
  </div></div>`;
}

function GeneralStep({ state, step }) {
  const [prefixes, setPrefixes] = useState(state.prefixes);
  const [entry, setEntry] = useState('');
  const add = () => {
    const problem = prefixProblem(entry, prefixes);
    if (problem) { toast(problem, 'bad'); return; }
    setPrefixes([...prefixes, entry]);
    setEntry('');
  };
  const first = prefixes[0] || '@bot ';
  const save = async () => {
    await api.put('/settings/core', { prefixes });
    state.prefixes = prefixes;
  };
  return html`<p class="muted">Pick the prefixes the bot answers to in this server. Slash commands and mentioning the bot always work.</p>
    <${TextInput} label="Custom prefixes" help="You can have up to 15." placeholder="Add a prefix" maxLength=${PREFIX_MAX} value=${entry} onChange=${setEntry}
      button=${html`<button class="btn primary" disabled=${prefixes.length >= 15} onClick=${add}>Add</button>`} />
    <div class="row" style=${{ marginBottom: '14px' }}>${prefixes.map((p) => html`<span class="chip" key=${p}><code>${p}</code>
      <button type="button" aria-label=${'Remove prefix ' + p} onClick=${() => setPrefixes(prefixes.filter((x) => x !== p))}><${Icon} name="x" size=${12} /></button></span>`)}</div>
    <p class="muted small">Commands then look like <code>${first}ping</code>, <code>${first}warn @someone spamming</code> and <code>${first}tag support</code>.</p>
    <${StepFooter} step=${step} save=${save} />`;
}

function WelcomeStep({ state, step }) {
  const guild = useGuild();
  const existing = (state.welcome && state.welcome.content) || '';
  const embeds = (state.welcome && state.welcome.embeds) || [];
  const [mode, setMode] = useState(state.welcome_channel ? (existing === WELCOME_PRESET ? 'preset' : 'custom') : 'none');
  // The saved text stays as it is until it is changed. The preset only shows as a suggestion in an empty box.
  const [text, setText] = useState(existing);
  const [channel, setChannel] = useState(state.welcome_channel);
  const message = mode === 'preset' ? WELCOME_PRESET : text;
  const save = async () => {
    if (mode === 'none') {
      if (!state.welcome_channel) return;
      await api.put('/settings/greetings', { welcome_channel: null });
      state.welcome_channel = null;
      return;
    }
    if (!channel) throw new Error('Pick a channel for the welcome message, or choose None.');
    if (!message.trim() && embeds.length === 0) throw new Error('Write a welcome message, or choose Preset or None.');
    if (channel === state.welcome_channel && message === existing) return;
    const welcome = { ...(state.welcome || { embeds: [], buttons: [] }), content: message };
    await api.put('/settings/greetings', { welcome_channel: channel, welcome });
    state.welcome_channel = channel;
    state.welcome = welcome;
  };
  const choices = [['none', 'None'], ['preset', 'Preset'], ['custom', 'Custom']];
  const them = embeds.length === 1 ? 'it' : 'them';
  return html`<p class="muted">Greet new members the moment they join, or skip this and set it up later.</p>
    ${embeds.length > 0 && html`<${Notice}>The welcome message already has ${embeds.length === 1 ? 'an embed' : embeds.length + ' embeds'}. Quick setup leaves ${them} as is. Edit ${them} on the Greetings page.<//>`}
    <div class="row" style=${{ gap: '24px', marginBottom: '14px' }}>${choices.map(([key, label]) => html`<label class="check" key=${key}>
      <input type="radio" name="welcome-mode" checked=${mode === key} onChange=${() => setMode(key)} /><span>${label}</span></label>`)}</div>
    ${mode === 'custom' && html`<${TextArea} label="Your message" help="{user} is the new member and {server} is this server." placeholder=${WELCOME_PRESET} maxLength=${2000} value=${text} onChange=${setText} />`}
    ${mode !== 'none' && html`<div class="field"><label>Your message will look something like</label>
        <${MessagePreview} value=${{ content: sampleText(message, guild), embeds, buttons: (state.welcome && state.welcome.buttons) || [] }} /></div>
      <${ChannelPicker} label="Welcome channel" value=${channel} onChange=${setChannel} />`}
    <${StepFooter} step=${step} save=${save} />`;
}

function ReactionRolesStep({ state, step }) {
  const guild = useGuild();
  const [templateKey, setTemplateKey] = useState(null);
  const [title, setTitle] = useState('');
  const [channel, setChannel] = useState(null);
  const [color, setColor] = useState('#49d6df');
  const [working, setWorking] = useState(false);
  const [posted, setPosted] = useState([]);
  const template = state.templates.find((t) => t.key === templateKey);
  const pick = (next) => {
    setTemplateKey(next.key);
    setTitle(next.title);
    setColor('#' + next.color.toString(16).padStart(6, '0'));
  };
  const makeChannel = async () => {
    try {
      const made = await api.post('/setup/channel', { name: 'pick-your-roles' });
      await guild.reload();
      setChannel(made.id);
      toast(`Made #${made.name}.`);
    } catch (error) {
      toastError(error);
    }
  };
  const create = async () => {
    if (!channel) { toast('Pick a channel for the message first.', 'bad'); return; }
    setWorking(true);
    try {
      const { roles } = await api.post('/setup/role-template', { template: template.key });
      const lines = roles.map((role) => `${role.emoji} <@&${role.id}>`);
      await guild.reload();
      await api.post('/reactionroles', {
        create_mode: 'post', channel_id: channel, type: 'normal', mode: 'reactions',
        payload: { content: '', buttons: [], embeds: [{ title, description: lines.join('\n'), color }] },
        rows: roles.map((role) => ({ emoji: role.emoji, roles: [role.id] })),
      });
      setPosted([...posted, template.label]);
      setTemplateKey(null);
      toast('Reaction role message posted.');
    } catch (error) {
      toastError(error);
    } finally {
      setWorking(false);
    }
  };
  return html`<p class="muted">Let members give themselves roles by reacting to a message. You can add more from the Reaction roles page anytime.</p>
    ${posted.length > 0 && html`<${Notice} kind="ok">Posted: ${posted.join(', ')}. Pick another or go on to the next step.<//>`}
    <div class="field"><label>Reaction role templates</label>
      <div class="row">${state.templates.map((t) => html`<button key=${t.key} class=${'btn' + (t.key === templateKey ? ' primary' : '')} onClick=${() => pick(t)}>${t.label}</button>`)}</div></div>
    ${!template && html`<p class="muted">Select a template up top to get started.</p>`}
    ${template && html`<div class="field-edit">
      <p class="muted small">Roles: ${template.roles.map((role) => `${role.emoji} ${role.name}`).join(', ')}</p>
      <${TextInput} label="What should the message title say?" maxLength=${256} value=${title} onChange=${setTitle} />
      <${ChannelPicker} label="Which channel should the message be posted in?" value=${channel} onChange=${setChannel} />
      <button class="btn small" style=${{ marginBottom: '14px' }} onClick=${makeChannel}><${Icon} name="plus" /> Create one for me</button>
      <div class="field"><label>What color should the embed accent have?</label>
        <input class="color-input" type="color" aria-label="Embed color" value=${color} onInput=${(e) => setColor(e.currentTarget.value)} /></div>
      <button class="btn success" disabled=${working} onClick=${create}>${working ? 'Making the roles, this takes a few seconds...' : 'Create reaction role'}</button>
    </div>`}
    <${StepFooter} step=${step} hold=${working} />`;
}

function ModerationStep({ state, step }) {
  const guild = useGuild();
  const [muteRole, setMuteRole] = useState(state.mute_role);
  const [modlog, setModlog] = useState(state.modlog_channel);
  const [making, setMaking] = useState(null);
  // The server saves what it makes, so the step's own record is updated too. Otherwise Previous and Next would put the old value back.
  const make = async (kind, path, apply, what) => {
    setMaking(kind);
    try {
      const made = await api.post(path);
      await guild.reload();
      state[kind === 'mute' ? 'mute_role' : 'modlog_channel'] = made.id;
      apply(made.id);
      toast(`Made ${what}.`);
    } catch (error) {
      toastError(error);
    } finally {
      setMaking(null);
    }
  };
  const save = async () => {
    await api.put('/settings/moderation', { mute_role: muteRole, modlog_channel: modlog });
    state.mute_role = muteRole;
    state.modlog_channel = modlog;
  };
  return html`<p class="muted">Set the mute role the bot hands out, and where moderation actions get logged.</p>
    <h4>Mute role (highly recommended)</h4>
    <p class="muted small">This role is what every mute command and automated mute uses. Unless you know Discord's permissions well, let the bot make it, since it also sets the role's limits in every channel.</p>
    <${RolePicker} value=${muteRole} onChange=${setMuteRole} />
    ${!muteRole && html`<button class="btn small" style=${{ marginBottom: '14px' }} disabled=${making !== null} onClick=${() => make('mute', '/setup/mute-role', setMuteRole, 'the mute role')}><${Icon} name="plus" /> Create one for me</button>`}
    ${making === 'mute' && html`<p class="muted small">Making the role and setting its limits in every channel. On a big server this can take a few minutes. Stay on this page until it finishes.</p>`}
    <h4>Which channel should modlogs be posted in?</h4>
    <p class="muted small">Actions your moderators take get posted here, for transparency and to keep track. A channel made here is hidden from members. The bot manager role, the moderation staff roles and Administrators can read it.</p>
    <${ChannelPicker} value=${modlog} onChange=${setModlog} />
    ${!modlog && html`<button class="btn small" style=${{ marginBottom: '14px' }} disabled=${making !== null} onClick=${() => make('modlog', '/setup/modlog-channel', setModlog, 'the modlog channel')}><${Icon} name="plus" /> Create one for me</button>`}
    <${StepFooter} step=${step} save=${save} hold=${making !== null} />`;
}

function AutomodStep({ state, step }) {
  const [preset, setPreset] = useState('none');
  const save = () => api.post('/setup/automod', { preset });
  return html`<p class="muted">Pick a preset for automated moderation. You can tune each rule on the Automod page once you're done.</p>
    <div class="checklist" style=${{ marginBottom: '14px' }}>${state.presets.map((row) => html`<label class="check" key=${row.key}>
      <input type="radio" name="automod-preset" checked=${preset === row.key} onChange=${() => setPreset(row.key)} />
      <span><b>${row.label}</b><br /><span class="muted small">${row.hint}</span></span></label>`)}</div>
    <p class="muted small">If none of these fit, choose the first and set everything up in detail later.</p>
    <${StepFooter} step=${step} save=${save} />`;
}

const BODIES = { general: GeneralStep, welcome: WelcomeStep, reactionroles: ReactionRolesStep, moderation: ModerationStep, automod: AutomodStep };

function Done({ guild, onAgain }) {
  const links = [['Bot settings', 'botsettings', 'settings'], ['Commands', 'commands', 'wrench'], ['Automod', 'automod', 'shield'], ['Moderation', 'moderation', 'gavel'], ['Logging', 'logging', 'scroll-text'], ['Reaction roles', 'reactionroles', 'smile']];
  return html`<div class="setup-wrap"><${Card} icon="circle-check" title="All set" footer=${html`<button class="btn ghost" onClick=${onAgain}>Go through it again</button>`}>
    <h2>That's the basics</h2>
    <p class="muted">Everything here is on the regular pages too, along with much more. These are good places to look next.</p>
    <div class="row">${links.map(([name, key, icon]) => html`<a key=${key} class="btn" data-nav="1" href=${`/dashboard/${guild.id}/${key}`}><${Icon} name=${icon} /> ${name}</a>`)}</div>
  <//></div>`;
}

export default function SetupPage() {
  const guild = useGuild();
  const [state, setState] = useState(null);
  const [index, setIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const [finished, setFinished] = useState(false);
  useEffect(() => { api.get('/setup').then(setState).catch(toastError); }, [guild.id]);
  if (!state) return html`<${Loading} />`;
  const steps = STEPS.filter((row) => !row.needs || state.has[row.needs]);
  if (finished) return html`<${Done} guild=${guild} onAgain=${() => { setFinished(false); setIndex(0); }} />`;
  const current = steps[index];
  const Body = BODIES[current.key];
  const go = async (save) => {
    setBusy(true);
    try {
      if (save) await save();
      if (index === steps.length - 1) setFinished(true);
      else setIndex(index + 1);
      window.scrollTo(0, 0);
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const step = { index, total: steps.length, busy, back: () => setIndex(index - 1), go };
  return html`<div class="setup-wrap"><${Card} icon="wand-sparkles" title=${`Step ${index + 1} of ${steps.length}`}>
    <div class="progress" style=${{ marginTop: 0, marginBottom: '16px' }}><span style=${{ width: `${((index + 1) / steps.length) * 100}%` }}></span></div>
    <h2>${current.title}</h2>
    <${Body} key=${current.key} state=${state} step=${step} />
  <//></div>`;
}
