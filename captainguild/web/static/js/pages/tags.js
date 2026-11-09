// Custom commands: the table of tags, their form, sharing, and moving them in and out as JSON.
import { html, useEffect, useMemo, useState } from '../lib.js';
import { api } from '../api.js';
import { MessageEditor, emptyMessage } from '../embed.js';
import { ImportModal, TemplateField, TryItPanel, askBeforeTrusting, downloadJson } from '../templates.js';
import { ANY_CHANNEL, Card, ChannelPicker, Field, Icon, Loading, Modal, Notice, Pager, RolePicker, SaveBar, SaveButton, SettingsGate, Switch, confirmDialog, copyText, toast, toastError, useGuild, useSettings } from '../ui.js';

const BLANK = { name: '', message: emptyMessage(), restricted: false, nsfw: false, trusted: false, channel_wl: [], channel_bl: [], role_wl: [], role_bl: [] };
const PAGE_SIZES = [10, 25, 50, 100];
const shareLink = (code) => `${location.origin}/servers?share=${encodeURIComponent(code)}`;

function ShareModal({ tag, code, onCode, onClose }) {
  const [busy, setBusy] = useState(false);
  const act = async (work) => {
    setBusy(true);
    try {
      await work();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const create = () => act(async () => onCode((await api.post(`/tags/${tag.id}/share`)).code));
  const stop = () => act(async () => { await api.del(`/tags/${tag.id}/share`); onCode(null); toast('That code no longer works.'); });
  const file = async () => {
    try {
      downloadJson(`tag-${tag.name}.json`, await api.get(`/tags/export?ids=${tag.id}`));
    } catch (error) {
      toastError(error);
    }
  };
  return html`<${Modal} icon="link" title=${'Share ' + tag.name} onClose=${onClose} footer=${html`<div class="right"><button class="btn ghost" onClick=${onClose}>Close</button></div>`}>
    <p class="muted">A share code or link lets someone with Manage Server import this tag into any other server this bot runs in. The link opens the import with the tag filled in and checked, so they can read it first. Stopping the code takes effect at once. A file works anywhere and can't be recalled.</p>
    ${code
      ? html`<div class="row"><code>${code}</code>
          <button class="btn small" onClick=${() => copyText(code)}><${Icon} name="copy" size=${14} /> Copy code</button>
          <button class="btn small" onClick=${() => copyText(shareLink(code))}><${Icon} name="link" size=${14} /> Copy link</button>
          <button class="btn small danger" disabled=${busy} onClick=${stop}>Stop sharing</button></div>`
      : html`<button class="btn primary" disabled=${busy} onClick=${create}>Create a share code</button>`}
    <hr />
    <button class="btn" onClick=${file}><${Icon} name="download" size=${14} /> Download as JSON</button>
  <//>`;
}

function TagModal({ tag, prefix, canTrust, onClose, onSaved, onShareChanged }) {
  const [form, setForm] = useState(tag ? { ...BLANK, ...tag } : BLANK);
  const [shareCode, setShareCode] = useState(tag ? tag.share_code : null);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const [sharing, setSharing] = useState(false);
  const set = (key) => ({ value: form[key], error: errors[key], onChange: (value) => { setForm({ ...form, [key]: value }); setErrors({ ...errors, [key]: undefined }); } });
  // Pasting raw JSON for an embed shouldn't wipe the text above it.
  const setEmbeds = (next) => setForm({ ...form, message: { ...next, content: next.content || form.message.content } });
  const setContent = (content) => { setForm({ ...form, message: { ...form.message, content } }); setErrors({ ...errors, message: undefined }); };
  const save = async () => {
    setSaving(true);
    const body = { name: form.name, message: form.message, restricted: form.restricted, nsfw: form.nsfw, trusted: form.trusted, channel_wl: form.channel_wl, channel_bl: form.channel_bl, role_wl: form.role_wl, role_bl: form.role_bl };
    try {
      const answer = tag ? await api.put(`/tags/${tag.id}`, body) : await api.post('/tags', body);
      if (answer.trust_removed) toast('Saved. This tag is no longer trusted. An administrator has to trust it again.', 'bad');
      else toast(tag ? 'Saved.' : 'Created.');
      onSaved();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setSaving(false);
    }
  };
  const setTrusted = async (value) => {
    if (value && !(await askBeforeTrusting('/tags/preview', form.message, 'tag'))) {
      setForm((current) => ({ ...current }));  // the box was already ticked by the click, and this puts it back
      return;
    }
    setForm((current) => ({ ...current, trusted: value }));
    setErrors((current) => ({ ...current, trusted: undefined }));
  };
  const remove = async () => {
    const sure = await confirmDialog({ title: `Delete ${tag.name}?`, body: 'This also removes its aliases and any share code. It can\'t be undone.', confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      await api.del(`/tags/${tag.id}`);
      toast('Deleted.');
      onSaved();
    } catch (error) {
      toastError(error);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([form]);
  const [start] = useState(snapshot);
  return html`<${Modal} large icon="tag" title=${tag ? 'Edit tag' : 'Create tag'} onClose=${onClose} dirty=${snapshot() !== start} footer=${html`
      ${tag && html`<button class="icon-btn danger" aria-label="Delete tag" title="Delete tag" onClick=${remove}><${Icon} name="trash-2" size=${18} /></button>
        <button class="icon-btn" aria-label="Share tag" title="Share tag" onClick=${() => setSharing(true)}><${Icon} name="link" size=${18} /></button>`}
      <div class="right">
        <button class="btn ghost" onClick=${onClose}>Cancel</button>
        <button class=${'btn ' + (tag ? 'primary' : 'success')} disabled=${saving} onClick=${save}>${tag ? 'Save' : 'Create'}</button>
      </div>`}>
    <${Field} label="Name" error=${errors.name}>
      <div class="input-group"><span class="addon first">${prefix}</span>
        <input type="text" aria-label="Name" maxlength="50" value=${form.name} onInput=${(e) => { setForm({ ...form, name: e.currentTarget.value }); setErrors({ ...errors, name: undefined }); }} /></div>
    <//>
    ${tag && tag.aliases.length > 0 && html`<p class="muted small">Also runs as: ${tag.aliases.join(', ')}</p>`}
    <${TemplateField} label="Content" placeholder="Enter some tag content" value=${form.message.content} error=${errors.message} onChange=${setContent} />
    <${MessageEditor} value=${form.message} onChange=${setEmbeds} collapsed showContent=${false} />
    <${TryItPanel} path="/tags/preview" message=${form.message} extra=${{ trusted: form.trusted }} argsLabel="Text after the tag's name" />
    <hr />
    <${Switch} label="Restricted" help="When a member uses it outside the bot channel, the bot pings them there and answers there. Moderators get the answer where they are. Bot channels are set on the Commands page." ...${set('restricted')} />
    <${Switch} label="NSFW" help="It only runs in channels marked as NSFW." ...${set('nsfw')} />
    <${Switch} label="Trusted" help="A trusted tag runs its command blocks for everyone, can use {cmdAdmin} and {override}, can mention members and can post anywhere with {redirect}. Only the server owner or an Administrator can turn this on, and it turns off if anyone else changes the text or who can use the tag. Anyone else who changes the text also becomes the tag's owner." disabled=${!canTrust && !form.trusted} value=${form.trusted} error=${errors.trusted} onChange=${setTrusted} />
    <hr />
    ${tag && ((tag.gone_roles || []).length > 0 || (tag.gone_channels || []).length > 0) && html`<${Notice} kind="bad">This tag names a role or channel that was deleted. Its ID stays on the list, because an empty whitelist would let everyone use the tag. Remove it below when you've decided who should.<//>`}
    <${ChannelPicker} label="Whitelist Channels" help="When any are set, it only runs in these." multi categories types=${ANY_CHANNEL} ...${set('channel_wl')} />
    <${ChannelPicker} label="Blacklist Channels" multi categories types=${ANY_CHANNEL} ...${set('channel_bl')} />
    <${RolePicker} label="Whitelist Roles" help="When any are set, only these can run it." multi placeholder="Select roles" ...${set('role_wl')} />
    <${RolePicker} label="Blacklist Roles" multi placeholder="Select roles" ...${set('role_bl')} />
    ${sharing && html`<${ShareModal} tag=${tag} code=${shareCode} onCode=${(code) => { setShareCode(code); onShareChanged(); }} onClose=${() => setSharing(false)} />`}
  <//>`;
}

function Rules() {
  const form = useSettings('tags');
  return html`<${SettingsGate} form=${form}>${() => html`
    <${Card} icon="users" title="Who can change tags" footer=${html`<${SaveButton} form=${form} />`}>
      <${Switch} label="Ownership" help="Only a tag's owner or a moderator can edit, append to or remove it. Anyone can still give it an alias, and /tag claim takes over a tag whose owner has left." ...${form.bind('ownership')} />
      <${Switch} label="Mod only" help="Only moderators can manage tags. Everyone can still use them. Moderators are people with Manage Server, the bot manager role, or a bypass role on the tag command." ...${form.bind('mod_only')} />
    <//>
    <${SaveBar} form=${form} />`}<//>`;
}

// Tags imported from elsewhere come in untrusted, so their command blocks only run for their owner. This lists them for whoever can vouch.
function WaitingOnTrust({ version, onChanged }) {
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [canTrust, setCanTrust] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.get('/tags/waiting').then((data) => { setRows(data.tags); setTotal(data.total); setCanTrust(data.can_trust); }).catch(toastError);
  }, [version]);
  if (!rows.length) return null;
  const trust = async (ids, what) => {
    const sure = await confirmDialog({ title: `Trust ${what}?`, body: 'These run their commands for everyone who uses them, with the powers listed. Read them first.', confirm: 'Trust' });
    if (!sure) return;
    setBusy(true);
    try {
      const answer = await api.post('/tags/trust', { ids });
      toast(`Trusted ${answer.trusted}.`);
      onChanged();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Card} icon="shield" title="Commands waiting on trust">
    <p class="muted">${total} tag${total === 1 ? ' holds' : 's hold'} command blocks that only run for the tag's owner until someone trusts the tag.${canTrust ? '' : ' The server owner or an Administrator can do that.'}</p>
    ${canTrust && html`<div class="row" style=${{ marginBottom: '10px' }}><button class="btn" disabled=${busy} onClick=${() => trust(rows.map((row) => row.id), `all ${rows.length}`)}>Trust all shown</button></div>`}
    ${rows.map((row) => html`<div class="spread" key=${row.id} style=${{ marginBottom: '6px' }}>
      <span><b>${row.name}</b> ${row.commands.map((text, index) => html`<code key=${index}>${text}</code> `)}</span>
      ${canTrust && html`<button class="btn small" disabled=${busy} onClick=${() => trust([row.id], row.name)}>Trust</button>`}
    </div>`)}
  <//>`;
}

function SortHead({ label, column, sort, onSort }) {
  const on = sort.column === column;
  return html`<th class="sortable" aria-sort=${on ? (sort.down ? 'descending' : 'ascending') : 'none'}><button type="button" class="th-sort" onClick=${() => onSort({ column, down: on ? !sort.down : false })}>${label}${on ? (sort.down ? ' ↓' : ' ↑') : ''}</button></th>`;
}

// The list comes a page at a time, since a server can hold 10,000 tags, and the editor fetches the one it opens.
function TagTable({ version, onEdit, onLoaded }) {
  const [data, setData] = useState(null);
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(PAGE_SIZES[0]);
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState({ column: 'name', down: false });
  useEffect(() => {
    let live = true;
    const query = new URLSearchParams({ page, size, q: search.trim(), sort: sort.column, down: sort.down ? '1' : '0' });
    const timer = setTimeout(() => api.get(`/tags?${query}`).then((answer) => {
      if (live) {
        setData(answer);
        onLoaded(answer);
      }
    }).catch((error) => { toastError(error); if (live) setData({ tags: [], total: 0 }); }), search ? 250 : 0);
    return () => { live = false; clearTimeout(timer); };
  }, [page, size, search, sort, version]);
  const rows = data ? data.tags : [];
  const total = data ? data.total : 0;
  const pages = Math.max(1, Math.ceil(total / size));
  const start = (page - 1) * size;
  return html`<div>
    <div class="table-tools"><span>Records:</span>
      <select aria-label="Records per page" value=${size} onChange=${(e) => { setSize(Number(e.currentTarget.value)); setPage(1); }}>${PAGE_SIZES.map((n) => html`<option key=${n} value=${n}>${n}</option>`)}</select>
      <input type="text" placeholder="Find by name or text" aria-label="Find by name or text" value=${search} onInput=${(e) => { setSearch(e.currentTarget.value); setPage(1); }} />
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <${SortHead} label="Name" column="name" sort=${sort} onSort=${(next) => { setSort(next); setPage(1); }} />
        <th>Content</th>
        <${SortHead} label="Uses" column="uses" sort=${sort} onSort=${(next) => { setSort(next); setPage(1); }} />
        <th style=${{ width: '100px' }}></th>
      </tr></thead>
      <tbody>
        ${!data && html`<tr><td colspan="4"><${Loading} /></td></tr>`}
        ${data && rows.length === 0 && html`<tr><td colspan="4" class="muted">No matching records</td></tr>`}
        ${rows.map((row) => html`<tr key=${row.id}>
          <td>${row.name}${row.trusted ? html` <span class="badge green" title="Its command blocks run for everyone.">trusted</span>` : ''}</td>
          <td>${row.summary}${row.cut ? '...' : ''}
            ${row.has_embed && html` <span class="badge blue">embed</span>`}
            ${row.gone_roles.length > 0 && html` <span class="badge red" title="Its lists name a role that was deleted.">deleted role</span>`}
            ${row.gone_channels.length > 0 && html` <span class="badge red" title="Its lists name a channel that was deleted.">deleted channel</span>`}</td>
          <td>${row.uses}</td>
          <td><button class="btn outline block" onClick=${() => onEdit(row.id)}>Edit</button></td>
        </tr>`)}
      </tbody>
    </table></div>
    <div class="table-foot">
      <span class="muted small">Showing ${total ? start + 1 : 0} to ${Math.min(start + size, total)} of ${total} records</span>
      <${Pager} page=${page} pages=${pages} onPage=${setPage} />
    </div>
  </div>`;
}

export default function TagsPage() {
  const guild = useGuild();
  const startShare = useMemo(() => new URLSearchParams(location.search).get('share') || '', []);
  const [total, setTotal] = useState(0);
  const [prefix, setPrefix] = useState('!');
  const [canTrust, setCanTrust] = useState(false);
  const [shadowed, setShadowed] = useState([]);
  const [version, setVersion] = useState(0);
  const [editing, setEditing] = useState(null);
  const [importing, setImporting] = useState(Boolean(startShare));
  const load = () => setVersion((number) => number + 1);
  const onLoaded = (data) => {
    setTotal(data.total);
    setPrefix(data.prefixes[0] || '/tag get ');
    setCanTrust(data.can_trust);
    setShadowed(data.shadowed || []);
  };
  const edit = async (id) => {
    try {
      setEditing(await api.get(`/tags/${id}`));
    } catch (error) {
      toastError(error);
    }
  };
  const exportAll = async () => {
    try {
      downloadJson(`tags-${guild.id}.json`, await api.get('/tags/export'));
    } catch (error) {
      toastError(error);
    }
  };
  const closeAndLoad = () => { setEditing(null); setImporting(false); load(); };
  return html`
    <${Card} icon="tag" title="Tags">
      <div class="row" style=${{ marginBottom: '14px' }}>
        <button class="btn success" onClick=${() => setEditing('new')}><${Icon} name="plus" /> Create new tag</button>
        <div class="right row">
          <button class="btn" onClick=${() => setImporting(true)}><${Icon} name="upload" size=${14} /> Import</button>
          <button class="btn" disabled=${total === 0} onClick=${exportAll}><${Icon} name="download" size=${14} /> Export</button>
        </div>
      </div>
      <${TagTable} key=${guild.id} version=${version} onEdit=${edit} onLoaded=${onLoaded} />
      ${shadowed.length > 0 && html`<p class="muted small">These tags share a name with a command, so typing that name runs the command instead: ${shadowed.join(', ')}. /tag get still runs them, or rename them.</p>`}
    <//>
    <${WaitingOnTrust} key=${guild.id} version=${version} onChanged=${load} />
    <${Rules} />
    ${editing && html`<${TagModal} key=${editing === 'new' ? 'new' : editing.id} tag=${editing === 'new' ? null : editing} prefix=${prefix} canTrust=${canTrust} onClose=${() => setEditing(null)} onSaved=${closeAndLoad} onShareChanged=${load} />`}
    ${importing && html`<${ImportModal} title="Import tags" path="/tags/import" nameKey="name" nameLabel="Name" withCode startCode=${startShare} onClose=${() => setImporting(false)} onDone=${closeAndLoad} />`}`;
}
