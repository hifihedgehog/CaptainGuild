// XP settings, reward roles, the rank card, imports and backups, and the server's leaderboard.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { MessageEditor, MessagePreview } from '../embed.js';
import { Card, ChannelPicker, Col, DataTable, Empty, Grid, HexInput, Icon, Loading, Modal, Notice, NumberInput, Pager, RolePicker, SaveBar, SaveButton, SettingsGate, Switch, confirmDialog, roleName, toast, toastError, useGuild, useSettings, when } from '../ui.js';

const RISKY = ['administrator', 'manage_guild', 'manage_channels', 'manage_roles', 'manage_messages', 'manage_expressions', 'manage_nicknames', 'manage_webhooks', 'ban_members', 'kick_members', 'moderate_members'];
const DESTINATIONS = [
  { value: 'none', label: 'Do not send announcement', prefix: '', group: 'Options' },
  { value: 'same', label: 'Send announcement in same channel', prefix: '', group: 'Options' },
  { value: 'dm', label: 'DM announcement', prefix: '', group: 'Options' },
];
const hex = (value) => '#' + Number(value).toString(16).padStart(6, '0');
const WATCH_TRIES = 40; // a minute of failed requests before a page stops watching a job

function firstLevels(minimum, modulo) {
  const low = Math.max(Number(minimum) || 1, 1);
  const step = Math.max(Number(modulo) || 1, 1);
  const start = low % step ? low + (step - (low % step)) : Math.max(step, low);
  return [0, 1, 2, 3, 4].map((i) => start + i * step).join(', ');
}

function sampleMessage(message, me) {
  const fill = (text) => (text || '').replaceAll('{user}', 'Sample Member').replaceAll('{mention}', '@Sample Member').replaceAll('{lvl}', '12');
  return {
    ...message,
    content: fill(message.content),
    embeds: (message.embeds || []).map((embed) => ({ ...embed, title: embed.title && fill(embed.title), description: embed.description && fill(embed.description) })),
  };
}

function RoleWarning({ guild, roleId, elevated }) {
  const role = guild.roles.find((r) => String(r.id) === String(roleId));
  if (!role) return html`<span class="badge red">Role is gone</span>`;
  if (role.position >= guild.me.top_role_position) return html`<span class="badge gold" title="The bot can't give a role at or above its own top role. Move the bot's role up.">Above the bot</span>`;
  if (!elevated && role.permissions.some((p) => RISKY.includes(p))) return html`<span class="badge gold" title="The bot won't give this while elevated roles are off on the Commands page.">Moderator permissions</span>`;
  return null;
}

// Runs one job at a time for the server, and keeps watching through a dropped request, since a restart shouldn't read as the job ending.
function RoleSync() {
  const guild = useGuild();
  const [job, setJob] = useState(null);
  const poll = useRef(null);
  const misses = useRef(0);
  useEffect(() => () => clearTimeout(poll.current), []);
  const watch = async () => {
    try {
      const state = await api.get('/levels/sync');
      misses.current = 0;
      setJob(state);
      if (state.state === 'running') poll.current = setTimeout(watch, 2000);
    } catch (error) {
      misses.current += 1;
      if (misses.current < WATCH_TRIES) poll.current = setTimeout(watch, 2000);
      else toastError(error);
    }
  };
  useEffect(() => { watch(); }, [guild.id]);
  const start = async () => {
    try {
      setJob(await api.post('/levels/sync'));
      poll.current = setTimeout(watch, 1000);
    } catch (error) { toastError(error); }
  };
  const running = job && job.state === 'running';
  return html`<div class="field">
    <p class="muted small">Saving a change here brings everyone's roles in line: members past a level get its role, and a role taken off this list comes off the people who have it.</p>
    <button class="btn outline" disabled=${running} onClick=${start}>${running ? `Updating roles, ${job.checked.toLocaleString()} of ${job.total.toLocaleString()} checked...` : "Update everyone's roles now"}</button>
    ${job && job.state === 'done' && html`<p class="muted small">Last run ${when(job.finished_at)} changed ${job.changed.toLocaleString()} members.</p>`}
    ${job && job.state === 'failed' && html`<${Notice} kind="bad">The last role update stopped early. It's been logged.<//>`}
  </div>`;
}

function Rewards({ form }) {
  const guild = useGuild();
  const [role, setRole] = useState(null);
  const [level, setLevel] = useState(1);
  const [rules, setRules] = useState(null);
  useEffect(() => { api.get('/voicelink/rules').then(setRules).catch(() => {}); }, [guild.id]);
  const rows = form.data.rewards;
  const add = () => {
    if (!role || !level) { toast('Pick both a role and a level.', 'bad'); return; }
    if (rows.some((r) => Number(r.level) === Number(level))) { toast('That level already has a reward. Remove it first.', 'bad'); return; }
    form.set('rewards', [...rows, { level: Number(level), role_id: role }].sort((a, b) => a.level - b.level));
    setRole(null);
    setLevel(1);
  };
  return html`<${Card} icon="gift" title="Level rewards" footer=${html`<${SaveButton} form=${form} />`}>
    <div class="reward-add">
      <${RolePicker} value=${role} onChange=${setRole} />
      <div class="input-group"><span class="addon addon-start">lvl</span><input type="number" min="1" max="1000" aria-label="Level" value=${level} onInput=${(e) => setLevel(Number(e.currentTarget.value))} /></div>
      <button class="btn success" onClick=${add}><${Icon} name="plus" /> Add</button>
    </div>
    <hr />
    <${DataTable} rows=${rows} rowKey="level" pageSizes=${[10]} columns=${[
      { key: 'role', label: 'Role', filter: true, sort: true, text: (row) => roleName(guild, row.role_id), render: (row) => html`<span class="row">${roleName(guild, row.role_id)} <${RoleWarning} guild=${guild} roleId=${row.role_id} elevated=${rules && rules.elevated_roles} /></span>` },
      { key: 'level', label: 'Lvl', sort: true, width: '70px' },
      { key: 'options', label: 'Options', width: '90px', render: (row) => html`<button class="btn small outline danger" onClick=${() => form.set('rewards', rows.filter((r) => r.level !== row.level))}>Delete</button>` },
    ]} />
    ${form.errors.rewards && html`<div class="error">${form.errors.rewards}</div>`}
    <${RoleSync} />
  <//>`;
}

function RankCard({ form }) {
  const guild = useGuild();
  const [background, setBackground] = useState(null);
  const [src, setSrc] = useState('');
  const [bump, setBump] = useState(0);
  const file = useRef(null);
  const { card_color: color, card_accent: accent, card_opacity: opacity } = form.data;
  useEffect(() => { api.get('/levels/background').then(setBackground).catch(toastError); }, [guild.id]);
  useEffect(() => {
    const strength = Math.max(0, Math.min(100, Number(opacity) || 0));
    const url = `/api/g/${guild.id}/levels/card?color=${encodeURIComponent(hex(color))}&accent=${encodeURIComponent(hex(accent))}&opacity=${strength}&t=${bump}`;
    const timer = setTimeout(() => setSrc(url), 250);
    return () => clearTimeout(timer);
  }, [guild.id, color, accent, opacity, bump]);
  const upload = async (event) => {
    const picked = event.currentTarget.files[0];
    event.currentTarget.value = '';
    if (!picked) return;
    if (picked.size > 8 * 1024 * 1024) { toast('That file is over 8 MB.', 'bad'); return; }
    const body = new FormData();
    body.append('background', picked);
    try {
      setBackground(await api.upload('/levels/background', body));
      setBump(Date.now());
      toast('Background saved.');
    } catch (error) { toastError(error); }
  };
  const clear = async () => {
    try {
      setBackground(await api.del('/levels/background'));
      setBump(Date.now());
      toast('Background removed.');
    } catch (error) { toastError(error); }
  };
  return html`<${Card} icon="image" title="Rank card" footer=${html`<${SaveButton} form=${form} />`}>
    ${src && html`<img class="rank-preview" src=${src} alt="A sample rank card with these settings" />`}
    <p class="muted small">This is a sample. Members can set their own background with <code>/level background</code>.</p>
    <div class="input-row">
      <div class="field"><label>Bar color</label><input class="color-input wide" type="color" aria-label="Bar color" value=${hex(color)} onInput=${(e) => form.set('card_color', parseInt(e.currentTarget.value.slice(1), 16))} />
        <${HexInput} label="Bar color as hex" value=${hex(color)} onChange=${(text) => text && form.set('card_color', parseInt(text.slice(1), 16))} /></div>
      <div class="field"><label>Text color</label><input class="color-input wide" type="color" aria-label="Text color" value=${hex(accent)} onInput=${(e) => form.set('card_accent', parseInt(e.currentTarget.value.slice(1), 16))} />
        <${HexInput} label="Text color as hex" value=${hex(accent)} onChange=${(text) => text && form.set('card_accent', parseInt(text.slice(1), 16))} /></div>
      <${NumberInput} label="Panel opacity" addon="%" min=${0} max=${100} ...${form.bind('card_opacity')} />
    </div>
    <h4>Server background</h4>
    <p class="muted">The image is scaled to 934 pixels wide and cropped to 282 tall. JPG, PNG or GIF, up to 8 MB. A member's own background takes its place on their card.</p>
    <input ref=${file} class="hidden" type="file" accept="image/jpeg,image/png,image/gif" aria-label="Server background" onChange=${upload} />
    <div class="row">
      <button class="btn" onClick=${() => file.current.click()}><${Icon} name="upload" /> Upload image</button>
      ${background && background.set && html`<button class="btn outline danger" onClick=${clear}>Remove</button>`}
    </div>
  <//>`;
}

function Mee6Import({ onDone }) {
  const [job, setJob] = useState(null);
  const poll = useRef(null);
  const watching = useRef(false); // this page saw it running, so its end is news. A job that finished last week isn't.
  const misses = useRef(0);
  useEffect(() => () => clearTimeout(poll.current), []);
  const watch = async () => {
    try {
      const state = await api.get('/levels/import');
      misses.current = 0;
      setJob(state);
      if (state.state === 'running') {
        watching.current = true;
        poll.current = setTimeout(watch, 1500);
      } else if (watching.current) {
        watching.current = false;
        if (state.state === 'done') { toast(`Imported ${state.members} members.`); onDone(); }
      }
    } catch (error) {
      misses.current += 1;
      if (watching.current && misses.current < WATCH_TRIES) poll.current = setTimeout(watch, 1500);
      else {
        toastError(error);
        if (watching.current) setJob({ state: 'failed', error: 'The page lost touch with the import. Reload it to see how it went.' });
        watching.current = false;
      }
    }
  };
  useEffect(() => { watch(); }, []);
  const start = async () => {
    const sure = await confirmDialog({ title: 'Import from MEE6?', body: "This replaces everyone's XP here with their XP on MEE6's public leaderboard. The current XP goes into a backup first.", confirm: 'Import' });
    if (!sure) return;
    try {
      setJob(await api.post('/levels/import'));
      watching.current = true;
      poll.current = setTimeout(watch, 1000);
    } catch (error) { toastError(error); }
  };
  const running = job && job.state === 'running';
  return html`<${Card} icon="download" title="Import from MEE6">
    <p class="muted">Both bots use the same XP curve, so levels carry over exactly. MEE6's leaderboard has to be public, and reward roles join any you've set here.</p>
    ${job && job.state === 'failed' && html`<${Notice} kind="bad">${job.error}<//>`}
    ${job && job.state === 'done' && html`<${Notice} kind="ok">${when(job.finished_at)}: imported ${job.members.toLocaleString()} members and ${job.rewards} reward roles.${job.rewards_skipped ? ` ${job.rewards_skipped} reward roles were left out, because they sit above your top role or the bot's, or hold moderator permissions.` : ''}${job.role_updates ? ` Roles are being updated for ${job.role_updates.toLocaleString()} members.` : ''}<//>`}
    <button class="btn primary" disabled=${running} onClick=${start}>${running ? `Importing, ${job.members} members so far...` : 'Import from MEE6'}</button>
  <//>`;
}

function Backups({ onChanged, stamp }) {
  const guild = useGuild();
  const [rows, setRows] = useState(null);
  const load = () => api.get('/levels/backups').then(setRows).catch(toastError);
  useEffect(() => { load(); }, [guild.id, stamp]);
  const restore = async (row) => {
    const sure = await confirmDialog({ title: 'Restore this backup?', body: `Everyone's XP goes back to how it stood on ${when(row.made_at)}. Today's XP is kept as a backup too.`, confirm: 'Restore' });
    if (!sure) return;
    try {
      const restored = await api.post(`/levels/backups/${row.id}/restore`);
      toast(restored.role_updates ? `Restored. Roles are being updated for ${restored.role_updates.toLocaleString()} members.` : 'Restored.');
      onChanged();
    } catch (error) { toastError(error); }
  };
  const reset = async () => {
    const sure = await confirmDialog({ title: 'Reset everyone?', body: 'Every member goes back to zero XP. A backup is kept. Reward roles stay as they are.', confirm: 'Reset everyone', danger: true });
    if (!sure) return;
    try {
      await api.post('/levels/reset');
      toast('Everyone is back to zero.');
      onChanged();
    } catch (error) { toastError(error); }
  };
  return html`<${Card} icon="history" title="Backups" right=${html`<button class="btn small outline danger" onClick=${reset}>Reset everyone</button>`}>
    <p class="muted">A copy of everyone's XP is kept each day and before any reset, import or restore. The newest seven stay.</p>
    <${DataTable} loading=${rows === null} rows=${rows || []} pageSizes=${[10]} empty="No backups yet" columns=${[
      { key: 'made_at', label: 'Made', render: (row) => when(row.made_at) },
      { key: 'reason', label: 'Why' },
      { key: 'members', label: 'Members' },
      { key: 'restore', label: '', width: '90px', render: (row) => html`<button class="btn small outline" onClick=${() => restore(row)}>Restore</button>` },
    ]} />
  <//>`;
}

function MemberEditor({ row, onClose, onSaved }) {
  const [mode, setMode] = useState('level');
  const [level, setLevel] = useState(row.level);
  const [xp, setXp] = useState(row.xp);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      await api.put(`/levels/members/${row.user_id}`, mode === 'level' ? { level } : { xp });
      toast('Saved.');
      onSaved();
    } catch (error) { toastError(error); } finally { setBusy(false); }
  };
  const remove = async () => {
    const sure = await confirmDialog({ title: `Reset ${row.name}?`, body: 'They go back to zero XP and lose their reward roles.', confirm: 'Reset', danger: true });
    if (!sure) return;
    try {
      await api.del(`/levels/members/${row.user_id}`);
      toast('Reset.');
      onSaved();
    } catch (error) { toastError(error); }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([mode, level, xp]);
  const [start] = useState(snapshot);
  return html`<${Modal} title=${'Edit ' + row.name} icon="pencil" onClose=${onClose} dirty=${snapshot() !== start} footer=${html`
    <button class="btn ghost danger" onClick=${remove}>Reset member</button>
    <div class="right"><button class="btn ghost" onClick=${onClose}>Cancel</button><button class="btn primary" disabled=${busy} onClick=${save}>Save</button></div>`}>
    <p class="muted">Now level ${row.level} with ${row.xp.toLocaleString()} XP. Their reward roles change to match what you set.</p>
    <div class="row" role="radiogroup" aria-label="What to set" style=${{ marginBottom: '12px' }}>
      <label class="check"><input type="radio" name="mode" checked=${mode === 'level'} onChange=${() => setMode('level')} /><span>Set the level</span></label>
      <label class="check"><input type="radio" name="mode" checked=${mode === 'xp'} onChange=${() => setMode('xp')} /><span>Set the XP</span></label>
    </div>
    ${mode === 'level'
      ? html`<${NumberInput} label="Level" min=${0} max=${1000} value=${level} onChange=${setLevel} />`
      : html`<${NumberInput} label="XP" min=${0} value=${xp} onChange=${setXp} />`}
  <//>`;
}

function Leaderboard({ stamp, onChanged, canEdit }) {
  const guild = useGuild();
  const [query, setQuery] = useState('');
  const [typed, setTyped] = useState('');
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(10);
  const [left, setLeft] = useState(false);
  const [data, setData] = useState(null);
  const [editing, setEditing] = useState(null);
  const load = () => api.get(`/levels/leaderboard?page=${page}&size=${size}&left=${left ? 1 : 0}&q=${encodeURIComponent(query)}`).then(setData).catch(toastError);
  useEffect(() => { load(); }, [guild.id, page, size, query, left, stamp]);
  const search = (event) => { event.preventDefault(); setPage(1); setQuery(typed.trim()); };
  const clear = () => { setTyped(''); setQuery(''); setPage(1); };
  const start = data ? (data.page - 1) * size : 0;
  return html`<${Card} icon="crown" title="Server Leaderboard">
    <h4>Search members</h4>
    <p class="muted">Find someone by name or user ID.</p>
    <form class="row board-search" onSubmit=${search}>
      <div class="input-group"><input type="search" placeholder="Name or user ID" aria-label="Search the leaderboard" maxlength="100" value=${typed} onInput=${(e) => setTyped(e.currentTarget.value)} /><button type="submit" class="btn primary"><${Icon} name="search" /> Search</button></div>
      ${query && html`<button type="button" class="btn outline" onClick=${clear}>Clear</button>`}
      <label class="check"><input type="checkbox" checked=${left} onChange=${(e) => { setLeft(e.currentTarget.checked); setPage(1); }} /><span>Include members who left</span></label>
    </form>
    ${!data ? html`<${Loading} />` : html`
      <div class="table-tools"><span>Records:</span>
        <select aria-label="Records per page" value=${size} onChange=${(e) => { setSize(Number(e.currentTarget.value)); setPage(1); }}>${[10, 25, 50, 100].map((n) => html`<option key=${n} value=${n}>${n}</option>`)}</select></div>
      <div class="table-wrap"><table>
        <thead><tr><th style=${{ width: '70px' }}>Rank</th><th>Member</th><th style=${{ width: '120px' }}>XP</th><th style=${{ width: '80px' }}>Level</th><th style=${{ width: '110px' }}>Messages that earned XP</th><th style=${{ width: '120px' }}>Voice minutes</th>${canEdit && html`<th style=${{ width: '70px' }}></th>`}</tr></thead>
        <tbody>
          ${data.rows.length === 0 && html`<tr><td colspan=${canEdit ? 7 : 6}><${Empty}>${query ? "That person isn't on the leaderboard." : 'Nobody has earned XP yet.'}<//></td></tr>`}
          ${data.rows.map((row) => html`<tr key=${String(row.user_id)}>
            <td><b>${row.rank}</b></td>
            <td><span class="row">${row.avatar ? html`<img class="avatar" style=${{ width: '24px', height: '24px' }} src=${row.avatar} alt="" />` : html`<span class="avatar avatar-letter" style=${{ width: '24px', height: '24px', fontSize: '12px' }}>${(row.name || '?')[0].toUpperCase()}</span>`}${row.name}${row.left_at && html`<span class="badge outline">left</span>`}</span></td>
            <td>${row.xp.toLocaleString()}</td><td>${row.level}</td><td>${row.messages.toLocaleString()}</td><td>${row.voice_minutes.toLocaleString()}</td>
            ${canEdit && html`<td><button class="btn small outline" onClick=${() => setEditing(row)}>Edit</button></td>`}
          </tr>`)}
        </tbody>
      </table></div>
      <div class="table-foot">
        <span class="muted small">Showing ${data.total ? start + 1 : 0} to ${Math.min(start + size, data.total)} of ${data.total.toLocaleString()} records</span>
        <${Pager} page=${data.page} pages=${data.pages} onPage=${setPage} />
      </div>`}
    ${editing && html`<${MemberEditor} row=${editing} onClose=${() => setEditing(null)} onSaved=${() => { setEditing(null); onChanged(); }} />`}
  <//>`;
}

function RecentLevelUps({ stamp }) {
  const guild = useGuild();
  const [rows, setRows] = useState(null);
  useEffect(() => { api.get('/levels/events').then(setRows).catch(toastError); }, [guild.id, stamp]);
  return html`<${Card} icon="trending-up" title="Recent level-ups">
    <${DataTable} loading=${rows === null} rows=${rows || []} pageSizes=${[10]} empty="Nobody has leveled up yet" columns=${[
      { key: 'name', label: 'Member', filter: true },
      { key: 'level', label: 'Level', width: '80px' },
      { key: 'at', label: 'When', width: '190px', render: (row) => when(row.at) },
    ]} />
  <//>`;
}

export default function LevelsPage() {
  const guild = useGuild();
  const form = useSettings('levels');
  const [stamp, setStamp] = useState(0);
  const changed = () => setStamp((n) => n + 1);
  return html`<${SettingsGate} form=${form}>${() => html`
    ${!form.data.enabled && html`<${Notice} kind="warn">Levels are off, so nobody is earning XP. Turn them on in Level configuration and save.<//>`}
    <${Grid}>
      <${Col}>
        <${Card} icon="check" title="Level configuration" footer=${html`<${SaveButton} form=${form} />`}>
          <h2>Level configuration</h2>
          <p class="muted">Members earn XP for taking part and climb levels as it adds up. Reward milestones with roles, or just let people compare.</p>
          <${Switch} label="Levels on" ...${form.bind('enabled')} />
          <hr />
          <h4>Level message</h4>
          <p class="muted">What the bot posts when someone levels up. <code>${'{user}'}</code> <code>${'{lvl}'}</code> <code>${'{mention}'}</code> fill in, and so does the rest of the tag language.</p>
          <${MessageEditor} collapsed value=${form.data.message} contentPlaceholder="Enter a level up message" contentRows=${5} onChange=${(value) => form.set('message', value)} />
          ${form.errors.message && html`<div class="error">${form.errors.message}</div>`}
          <${MessagePreview} value=${sampleMessage(form.data.message, guild.me)} />
          <hr />
          <h4>Announcement destination</h4>
          <p class="muted">Where those messages go. Announcements can interrupt a conversation, so pick what suits your server.</p>
          <${ChannelPicker} extra=${DESTINATIONS} clearable=${false} ...${form.bind('destination')} />
          <hr />
          <${Switch} label="Only announce rewards" help="Stay quiet unless the new level gives a role." ...${form.bind('only_rewards')} />
          <hr />
          <${Switch} label="Replace rewards" help="Keep only the highest reward role, instead of collecting every one along the way." ...${form.bind('replace_rewards')} />
          <hr />
          <${Switch} label="Voice XP" help="Each minute in a voice channel with at least one other person earns what a message earns. Deafened members and the AFK channel earn nothing." ...${form.bind('voice_xp')} />
          <hr />
          <h4>Rate limit</h4>
          <p class="muted">How many messages can earn XP in a stretch of time, so spam earns nothing extra.</p>
          <div class="input-row">
            <${NumberInput} addon=${Number(form.data.rate) === 1 ? 'Message per' : 'Messages per'} min=${1} max=${25} ...${form.bind('rate')} />
            <${NumberInput} addon=${Number(form.data.window) === 1 ? 'Second' : 'Seconds'} min=${1} max=${600} ...${form.bind('window')} />
          </div>
          <hr />
          <h4>Custom XP per message</h4>
          <p class="muted">Leave this empty for the usual 15 to 25 XP.</p>
          <${NumberInput} addon="XP per message" placeholder="Default" min=${1} max=${100} ...${form.bind('custom_xp')} />
          <hr />
          <h4>Level modulo</h4>
          <p class="muted">Announce only levels evenly divisible by this number. With 5, that's levels 5, 10, 15 and so on.</p>
          <${NumberInput} min=${1} max=${100} ...${form.bind('modulo')} />
          <p class="muted small">Your first five announced levels: ${firstLevels(form.data.minimum, form.data.modulo)}</p>
          <hr />
          <h4>Minimum level</h4>
          <p class="muted">Announce nothing below this level. High levels take far longer to reach, so these stay rarer.</p>
          <${NumberInput} min=${0} max=${1000} ...${form.bind('minimum')} />
          <hr />
          <h4>Blacklist</h4>
          <p class="muted">These channels and roles earn no XP. A category covers all its channels.</p>
          <${ChannelPicker} label="No XP in these channels" multi categories types=${[0, 2, 5, 13, 15]} ...${form.bind('blacklist_channels')} />
          <${RolePicker} label="No XP for these roles" multi ...${form.bind('blacklist_roles')} />
        <//>
      <//>
      <${Col}>
        <${Rewards} form=${form} />
        <${RankCard} form=${form} />
        <${Mee6Import} onDone=${changed} />
        <${Backups} stamp=${stamp} onChanged=${changed} />
      <//>
    <//>
    <div style=${{ marginTop: '18px' }}><${Leaderboard} stamp=${stamp} onChanged=${changed} canEdit=${guild.access === 'full'} /></div>
    <div style=${{ marginTop: '18px' }}><${RecentLevelUps} stamp=${stamp} /></div>
    <${SaveBar} form=${form} />`}<//>`;
}
