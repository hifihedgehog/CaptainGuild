import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, Col, DataTable, Grid, Icon, PREFIX_MAX, RolePicker, SaveBar, SaveButton, SettingsGate, TextInput, prefixProblem, toast, toastError, useGuild, useSettings, useUnsaved, when } from '../ui.js';

export default function BotSettingsPage() {
  const guild = useGuild();
  const form = useSettings('core');
  const [nick, setNick] = useState(guild.me.nick || '');
  const [prefix, setPrefix] = useState('');
  const [recent, setRecent] = useState(null);
  useUnsaved(nick.trim() !== (guild.me.nick || ''));
  useEffect(() => {
    let live = true;
    api.get('/activity').then((rows) => { if (live) setRecent(rows); }).catch(() => { if (live) setRecent([]); });
    return () => { live = false; };
  }, [guild.id]);

  const applyNick = async () => {
    try {
      await api.post('/nickname', { nick: nick.trim() });
      toast('Nickname changed.');
      guild.reload();
    } catch (error) {
      toastError(error);
    }
  };
  return html`<${SettingsGate} form=${form}>${() => {
    const prefixes = form.data.prefixes;
    // Like the nickname, a prefix takes effect the moment it is added or removed.
    const addPrefix = async () => {
      const problem = prefixProblem(prefix, prefixes);
      if (problem) { toast(problem, 'bad'); return; }
      if (await form.saveFields({ prefixes: [...prefixes, prefix] })) setPrefix('');
    };
    const removePrefix = (p) => form.saveFields({ prefixes: prefixes.filter((x) => x !== p) });
    return html`<${Grid}>
      <${Col}>
        <${Card} icon="settings" title="Bot settings" footer=${html`<${SaveButton} form=${form} />`}>
          <h3>Nickname</h3>
          <p class="muted">The bot's display name in this server.</p>
          <${TextInput} value=${nick} placeholder=${guild.me.name} maxLength=${32} onChange=${setNick}
            button=${html`<button class="btn primary" onClick=${applyNick}>Apply</button>`} />
          <h3>Custom prefix</h3>
          <p class="muted">Tags and text commands answer to these. Mentioning the bot and slash commands always work. Adding or removing one saves it at once.</p>
          <${TextInput} value=${prefix} placeholder="Enter a prefix" maxLength=${PREFIX_MAX} onChange=${setPrefix}
            button=${html`<button class="btn primary" disabled=${form.saving} onClick=${addPrefix}>Add</button>`} />
          ${prefixes.map((p) => html`<div class="spread field-edit" key=${p}><code>${p}</code>
            <button class="icon-btn danger" aria-label=${'Remove prefix ' + p} disabled=${form.saving} onClick=${() => removePrefix(p)}><${Icon} name="trash-2" /></button></div>`)}
          <h3 style=${{ marginTop: '16px' }}>Dashboard access</h3>
          <p class="muted">People with these roles can open cases, reports and log search here without Manage Server. Changing a case or a note still takes what the matching command takes.</p>
          <${RolePicker} multi ...${form.bind('dashboard_staff_roles')} />
        <//>
      <//>
      <${Col}>
        <${Card} icon="list" title="Last actions">
          <h2>Recent actions</h2>
          <${DataTable} loading=${recent === null} rows=${recent || []} columns=${[
            { key: 'at', label: 'Executed', sort: true, render: (row) => when(row.at) },
            { key: 'user_name', label: 'Username', sort: true, filter: true },
            { key: 'action', label: 'Action', sort: true, filter: true, text: (row) => row.action + (row.detail ? ' ' + row.detail : '') },
          ]} />
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />`;
  }}<//>`;
}
