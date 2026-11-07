// The embed editor: write a post, edit a posted one, browse everything posted with its history, schedule posts, and move posts off another bot.
import { html, useEffect, useMemo, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { JsonPanel, MessageEditor, MessagePreview, emptyMessage } from '../embed.js';
import { Card, ChannelPicker, Check, Col, DataTable, Empty, Field, Grid, Icon, Loading, Modal, Notice, Switch, Tabs, channelName, confirmDialog, sameId, toast, toastError, useGuild, when } from '../ui.js';

const remember = (key) => {
  try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (error) { return null; }
};
const keep = (key, value) => {
  try {
    if (value) localStorage.setItem(key, JSON.stringify(value));
    else localStorage.removeItem(key);
  } catch (error) { /* a private window can refuse storage */ }
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const DESTINATIONS = [0, 5, 2, 13, 11, 12];
const MAX_FILES = 5;
const MAX_FILE_BYTES = 8 * 1024 * 1024;
// The server refuses a ping this person couldn't send themselves, so the switch shows only where they could.
const mayPing = (guild, channelId) => (guild.channels.find((c) => sameId(c.id, channelId)) || {}).can_ping !== false;
const isBlank = (message) => !message.content && !(message.embeds || []).length && !(message.buttons || []).length;
const pad = (number) => String(number).padStart(2, '0');
function localInput(seconds) {
  const date = new Date(seconds * 1000);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
const fromLocalInput = (text) => (text ? new Date(text).getTime() / 1000 : null);
const SOURCES = { dashboard: 'Dashboard', command: 'Command', schedule: 'Scheduled', adopted: 'Reposted', found: 'Found' };

function JumpLink({ url, label = 'Open in Discord' }) {
  return html`<a class="icon-btn" href=${url} target="_blank" rel="noopener noreferrer" aria-label=${label} title=${label}><${Icon} name="external-link" /></a>`;
}

function PreviewColumn({ message, onChange, readOnly = false }) {
  return html`<div class="col tall"><div class="sticky-col">
    <${Card} icon="eye" title="Preview"><${MessagePreview} value=${message} /><//>
    <${Card} icon="code" title="Raw JSON">
      <p class="muted">${readOnly ? 'The whole message as JSON.' : 'The whole message as JSON. Paste your own here and press Apply to fill the editor.'}</p>
      <${JsonPanel} value=${message} onChange=${onChange} readOnly=${readOnly} />
    <//>
  </div></div>`;
}

// ---- create ----
function SavedMessages({ message, onLoad }) {
  const [rows, setRows] = useState(null);
  const [name, setName] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = () => api.get('/embeds/saved').then(setRows).catch((failure) => { toastError(failure); setRows([]); });
  useEffect(() => { load(); }, []);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post('/embeds/saved', { name, payload: message });
      toast('Saved.');
      setName('');
      await load();
    } catch (failure) {
      setError((failure.fields && (failure.fields.name || failure.fields.payload)) || failure.message);
    } finally {
      setBusy(false);
    }
  };
  const open = async (row) => {
    const sure = isBlank(message) || await confirmDialog({ title: 'Replace what is in the editor?', body: `This fills the editor with "${row.name}".`, confirm: 'Replace' });
    if (sure) onLoad(row.payload);
  };
  const remove = async (row) => {
    const sure = await confirmDialog({ title: 'Delete this saved message?', body: `"${row.name}" goes away. Anything already posted from it stays.`, confirm: 'Delete', danger: true });
    if (!sure) return;
    try {
      await api.del(`/embeds/saved/${row.id}`);
      toast('Deleted.');
      await load();
    } catch (failure) {
      toastError(failure);
    }
  };
  return html`<${Card} icon="list" title="Saved messages">
    <p class="muted">Keep a message here to use it again. Everyone who can open this page shares the list.</p>
    <${Field} error=${error}>
      <div class="input-group">
        <input type="text" maxlength="100" placeholder="Name for this message" aria-label="Name for this message" value=${name} onInput=${(e) => setName(e.currentTarget.value)} />
        <button class="btn primary" disabled=${busy || !name.trim() || isBlank(message)} onClick=${save}>Save</button>
      </div>
    <//>
    <${DataTable} loading=${rows === null} rows=${rows || []} empty="Nothing saved yet." columns=${[
      { key: 'name', label: 'Name', filter: true, sort: true },
      { key: 'saved_at', label: 'Saved', sort: true, width: '165px', render: (row) => when(row.saved_at) },
      { key: 'actions', label: '', width: '90px', render: (row) => html`<div class="row" style=${{ flexWrap: 'nowrap', gap: '2px' }}>
        <button class="icon-btn" aria-label="Put it in the editor" title="Put it in the editor" onClick=${() => open(row)}><${Icon} name="pencil" /></button>
        <button class="icon-btn danger" aria-label="Delete" title="Delete" onClick=${() => remove(row)}><${Icon} name="trash-2" /></button>
      </div>` },
    ]} />
  <//>`;
}

function CreateTab({ onOpen }) {
  const guild = useGuild();
  const storageKey = `cg-embed-draft-${guild.id}`;
  const saved = useMemo(() => remember(storageKey), []);
  const [message, setMessage] = useState((saved && saved.message) || emptyMessage());
  const [channel, setChannel] = useState((saved && saved.channel) || null);
  const [ping, setPing] = useState(false);
  const [later, setLater] = useState(false);
  const [at, setAt] = useState('');
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const [posted, setPosted] = useState(null);
  const [files, setFiles] = useState([]);
  // What just went out, so a second click can't send it again until something changes.
  const [sent, setSent] = useState(null);
  const fileInput = useRef(null);
  useEffect(() => { keep(storageKey, isBlank(message) && !channel ? null : { message, channel }); }, [message, channel]);
  const alreadySent = sent !== null && same(sent, { message, channel, later, at });
  const addFiles = (event) => {
    const picked = [...event.currentTarget.files];
    event.currentTarget.value = '';
    const tooBig = picked.find((file) => file.size > MAX_FILE_BYTES);
    if (tooBig) { toast(`${tooBig.name} is over 8 MB.`, 'bad'); return; }
    const next = [...files, ...picked];
    if (next.length > MAX_FILES) { toast(`A post can carry up to ${MAX_FILES} files.`, 'bad'); return; }
    setFiles(next);
    setSent(null);
  };

  const submit = async () => {
    setBusy(true);
    setErrors({});
    setPosted(null);
    try {
      const body = { channel_id: channel, payload: message, ping: ping && mayPing(guild, channel) };
      if (later) body.at = fromLocalInput(at);
      if (later && !body.at) {
        setErrors({ at: 'Pick a date and time.' });
        return;
      }
      let result;
      if (files.length && !later) {
        const upload = new FormData();
        upload.append('form', JSON.stringify(body));
        for (const file of files) upload.append(file.name, file);
        result = await api.upload('/embeds/post-files', upload);
        setFiles([]);
      } else {
        result = await api.post('/embeds/post', body);
      }
      keep(storageKey, null);
      setSent({ message, channel, later, at });
      if (later) toast(`Scheduled for ${when(result.due_at)}. It's on the Scheduled tab.`);
      else setPosted(result);
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const reset = async () => {
    const sure = isBlank(message) || await confirmDialog({ title: 'Reset the editor?', body: 'This clears the text, embeds and buttons.', confirm: 'Reset', danger: true });
    if (!sure) return;
    setMessage(emptyMessage());
    setPosted(null);
    setErrors({});
    setFiles([]);
    setSent(null);
  };

  return html`<${Grid}>
    <${Col}>
      <${Card} icon="image" title="Embed" footer=${html`
        <button class="btn primary" disabled=${busy || !channel || alreadySent} onClick=${submit}>${busy ? 'Working...' : alreadySent ? (later ? 'Scheduled' : 'Posted') : later ? 'Schedule' : 'Post'}</button>
        <button class="btn ghost danger" onClick=${reset}>Reset</button>`}>
        <h2>Embed builder</h2>
        ${posted && html`<${Notice} kind="ok">
          <div class="spread"><span>Posted. <a href=${posted.url} target="_blank" rel="noopener noreferrer">Open it in Discord</a></span>
            <button class="btn small outline" onClick=${() => onOpen(posted.url)}>Edit this post</button></div>
        <//>`}
        <${MessageEditor} value=${message} onChange=${setMessage} showJson=${false} />
        ${errors.payload && html`<div class="error">${errors.payload}</div>`}
        <hr class="rule" />
        <h4>Destination</h4>
        <${ChannelPicker} types=${DESTINATIONS} value=${channel} onChange=${setChannel} error=${errors.channel_id} />
        ${mayPing(guild, channel) && html`<${Switch} label="Allow this post to ping roles and everyone" help="Off means @everyone and role mentions in the text stay silent. People mentioned by name still get pinged." value=${ping} onChange=${setPing} />`}
        <${Field} label="Files" error=${errors.files}>
          <div class="row">
            <button class="btn outline" disabled=${later || files.length >= MAX_FILES} onClick=${() => fileInput.current.click()}><${Icon} name="upload" /> Attach a file</button>
            <input ref=${fileInput} class="hidden" type="file" multiple aria-label="Files to post" onChange=${addFiles} />
            ${files.map((file, index) => html`<span class="badge outline" key=${file.name + index}>${file.name}
              <button class="icon-btn" aria-label=${'Remove ' + file.name} title="Remove" onClick=${() => { setFiles(files.filter((_, at_) => at_ !== index)); setSent(null); }}><${Icon} name="trash-2" size=${12} /></button></span>`)}
          </div>
          <p class="small dim">${later ? 'A scheduled post can\'t carry files. Post it now, or link to the file in the text.' : `Up to ${MAX_FILES} files, 8 MB each and 16 MB in all. An embed's image box can show one as attachment://its-name.`}</p>
        <//>
        <${Switch} label="Post it later" value=${later} onChange=${setLater} />
        ${later && html`<${Field} label="When, in your time zone" error=${errors.at}>
          <input type="datetime-local" aria-label="When to post" value=${at} onInput=${(e) => setAt(e.currentTarget.value)} />
        <//>`}
      <//>
      <${SavedMessages} message=${message} onLoad=${(payload) => { setMessage(payload); setSent(null); }} />
    <//>
    <${PreviewColumn} message=${message} onChange=${setMessage} />
  <//>`;
}

// ---- edit ----
function EditTab({ request, onAdopt, onChanged }) {
  const guild = useGuild();
  const [link, setLink] = useState('');
  const [found, setFound] = useState(null);
  const [message, setMessage] = useState(null);
  const [restored, setRestored] = useState(false);
  const [newer, setNewer] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [showHistory, setShowHistory] = useState(false);
  const draftKey = found ? `cg-embed-edit-${guild.id}-${found.message.id}` : null;

  useEffect(() => {
    if (draftKey && message && found.message.mine) keep(draftKey, same(message, found.message.payload) ? null : { version: found.library ? found.library.version : null, message });
  }, [message]);

  const load = async (target) => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.get(`/embeds/lookup?link=${encodeURIComponent(target)}`);
      const stored = result.message.mine ? remember(`cg-embed-edit-${guild.id}-${result.message.id}`) : null;
      // Drafts saved before versions were kept hold the message itself.
      const draft = stored && stored.message ? stored : (stored ? { version: null, message: stored } : null);
      setFound(result);
      setMessage(draft ? draft.message : result.message.payload);
      setRestored(!!draft);
      setNewer(!!draft && draft.version !== (result.library ? result.library.version : null));
    } catch (failure) {
      setFound(null);
      setMessage(null);
      setError((failure.fields && failure.fields.link) || failure.message);
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    if (request) {
      setLink(request.link);
      load(request.link);
    }
  }, [request]);

  const discard = () => {
    keep(draftKey, null);
    setMessage(found.message.payload);
    setRestored(false);
    setNewer(false);
  };
  const update = async (force = false) => {
    setBusy(true);
    try {
      const result = await api.patch('/embeds/message', {
        channel_id: found.message.channel_id, message_id: found.message.id, payload: message, version: found.library ? found.library.version : null, force,
      });
      setFound({ ...found, message: { ...found.message, payload: message }, library: { id: result.id, version: result.version } });
      keep(draftKey, null);
      setRestored(false);
      toast(result.changed ? 'Updated.' : 'Nothing changed.');
      onChanged();
    } catch (failure) {
      if (failure.status === 409 && await confirmDialog({
        title: 'Someone else changed this message',
        body: 'They saved a change after you opened it. Replacing it keeps their version in the history, so you can still get it back.',
        confirm: 'Replace it',
        danger: true,
      })) {
        await update(true);
        return;
      }
      if (failure.status !== 409) toastError(failure);
    } finally {
      setBusy(false);
    }
  };

  const original = found && found.message;
  return html`<${Grid}>
    <${Col}>
      <${Card} icon="image" title="Embed" footer=${original && original.mine ? html`
        <button class="btn primary" disabled=${busy || same(message, original.payload)} onClick=${() => update()}>${busy ? 'Working...' : 'Update'}</button>
        <button class="btn ghost danger" disabled=${same(message, original.payload)} onClick=${discard}>Reset</button>
        ${found.library && html`<button class="btn ghost" onClick=${() => setShowHistory(true)}><${Icon} name="history" /> History</button>`}
        <a class="btn ghost right" href=${original.url} target="_blank" rel="noopener noreferrer"><${Icon} name="external-link" /> Open in Discord</a>` : null}>
        <h2>Edit Embed</h2>
        <p class="muted">Paste the link to a message. In Discord, right-click it and pick Copy Message Link, or use the Edit Message menu under Apps.</p>
        <${Field} error=${error}>
          <div class="input-group">
            <input type="text" placeholder="Enter message link" aria-label="Message link" value=${link} onInput=${(e) => setLink(e.currentTarget.value)}
              onKeyDown=${(e) => { if (e.key === 'Enter' && link) load(link); }} />
            <button class="btn primary" disabled=${!link || busy} onClick=${() => load(link)}>Edit</button>
          </div>
        <//>
        ${restored && html`<${Notice} kind="warn"><div class="spread"><span>Your unsaved changes to this message came back.${newer ? ' The message was saved again since you wrote them, so updating replaces that newer version. It stays in the history.' : ''}</span>
          <button class="btn small outline" onClick=${discard}>Discard them</button></div><//>`}
        ${original && !original.mine && html`<${Notice} kind="warn">
          <p><b>${original.author_name}</b> wrote this message, and Discord only lets a bot edit its own messages. Repost it as this bot, and you can edit the copy from then on.</p>
          <button class="btn primary" onClick=${() => onAdopt(original)}>Repost it as this bot</button>
        <//>`}
        ${original && original.mine && original.controls > 0 && html`<${Notice} kind="warn">This message has ${original.controls} button${original.controls === 1 ? '' : 's'} or menu${original.controls === 1 ? '' : 's'} from another feature. They stay as they are and keep working when you update the text. Its link buttons can't change here, unless it's a reaction role message.<//>`}
        ${original && original.mine && html`<div>
          <hr class="rule" />
          <p class="muted">In <b>#${channelName(guild, original.channel_id, original.channel_name)}</b>, posted ${when(original.created_at)}.${found.library ? ` This is version ${found.library.version}.` : ' Saving starts its history with what it says now.'}</p>
          <${MessageEditor} value=${message} onChange=${setMessage} showJson=${false} />
        </div>`}
      <//>
    <//>
    ${original ? html`<${PreviewColumn} message=${message} onChange=${setMessage} readOnly=${!original.mine} />` : html`<${Col}><${Card} icon="eye" title="Preview"><${Empty}>The message shows here once it's loaded.<//><//><//>`}
    ${showHistory && html`<${HistoryModal} postedId=${found.library.id} onClose=${() => setShowHistory(false)} onChanged=${() => { onChanged(); load(link); }} />`}
  <//>`;
}

// ---- history ----
function HistoryModal({ postedId, onClose, onChanged }) {
  const [data, setData] = useState(null);
  const [picked, setPicked] = useState(null);
  const [busy, setBusy] = useState(false);
  const fetchHistory = () => api.get(`/embeds/messages/${postedId}`).then((result) => {
    setData(result);
    setPicked(result.versions.length ? result.versions[0].number : null);
  }).catch((error) => { toastError(error); onClose(); });
  useEffect(() => { fetchHistory(); }, [postedId]);
  const revert = async (number) => {
    setBusy(true);
    try {
      const result = await api.post(`/embeds/messages/${postedId}/revert`, { number });
      toast(result.changed ? `The message is back to version ${number}.` : 'It already says that.');
      await fetchHistory();
      onChanged();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  if (!data) return html`<${Modal} title="Version history" large onClose=${onClose}><${Loading} /><//>`;
  const latest = data.versions.length ? data.versions[0].number : null;
  const version = data.versions.find((v) => v.number === picked);
  return html`<${Modal} title="Version history" icon="history" large onClose=${onClose}>
    ${data.deleted_at && html`<${Notice} kind="warn">This message was deleted from Discord ${when(data.deleted_at)}. Repost it from the Messages tab to bring it back.<//>`}
    <div class="history">
      <div class="pick-list" role="listbox" aria-label="Versions">
        ${data.versions.map((v) => html`<button type="button" key=${v.number} role="option" aria-selected=${v.number === picked} class=${'pick-row version' + (v.number === picked ? ' on' : '')} onClick=${() => setPicked(v.number)}>
          <div><b>Version ${v.number}</b>${v.number === latest && html` <span class="badge blue">Current</span>`}
            <div class="small muted">${v.note}</div>
            <div class="small dim">${when(v.saved_at)}${v.saved_by_name ? ` by ${v.saved_by_name}` : ''}</div></div>
        </button>`)}
      </div>
      <div>
        ${version && html`<${MessagePreview} value=${version.payload} />`}
        ${version && version.number !== latest && !data.deleted_at && html`<button class="btn primary" style=${{ marginTop: '12px' }} disabled=${busy} onClick=${() => revert(version.number)}><${Icon} name="undo-2" /> Revert to this version</button>`}
      </div>
    </div>
  <//>`;
}

// ---- everything posted ----
function MessagesTab({ active, refresh, onOpen }) {
  const guild = useGuild();
  const [rows, setRows] = useState(null);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState('');
  const [history, setHistory] = useState(null);
  const [reposting, setReposting] = useState(null);
  // Answers can arrive out of order, so a late answer to an older search is dropped.
  const asked = useRef(0);
  const load = (needle = query) => {
    asked.current += 1;
    const mine = asked.current;
    return api.get(`/embeds/messages?q=${encodeURIComponent(needle.trim())}`)
      .then((data) => { if (mine === asked.current) { setRows(data.rows); setTotal(data.total); } })
      .catch((error) => { if (mine === asked.current) { toastError(error); setRows([]); } });
  };
  useEffect(() => { if (active) load(); }, [active, refresh]);
  useEffect(() => {
    if (rows === null) return undefined;
    const timer = setTimeout(() => load(query), 250);
    return () => clearTimeout(timer);
  }, [query]);
  const remove = async (row) => {
    const sure = await confirmDialog({
      title: row.deleted_at ? 'Forget this message?' : 'Delete this message?',
      body: row.deleted_at ? 'It already left Discord. This removes it and its history from the list.' : `This deletes it from #${channelName(guild, row.channel_id)} and removes its history from the list.`,
      confirm: 'Delete',
      danger: true,
    });
    if (!sure) return;
    try {
      await api.del(`/embeds/messages/${row.id}`);
      toast('Deleted.');
      load();
    } catch (error) {
      toastError(error);
    }
  };
  const repost = async (row) => {
    setReposting(row.id);
    try {
      const result = await api.post(`/embeds/messages/${row.id}/repost`);
      toast('It\'s back in the channel, at the bottom.');
      load();
      return result;
    } catch (error) {
      toastError(error);
      return null;
    } finally {
      setReposting(null);
    }
  };
  return html`<${Card} icon="list" title="Messages">
    <h2>Everything posted through the bot</h2>
    <p class="muted">Each message keeps its versions, so any edit can be taken back. Search looks through every message's text, embeds included.</p>
    <div class="input-group" style=${{ maxWidth: '420px', marginBottom: '14px' }}>
      <input type="search" placeholder="Search messages" aria-label="Search messages" value=${query} onInput=${(e) => setQuery(e.currentTarget.value)} /><span class="addon"><${Icon} name="search" /></span>
    </div>
    ${rows && rows.length < total && html`<${Notice}>Showing the newest ${rows.length} of ${total}. Search to reach older ones.<//>`}
    <${DataTable} loading=${rows === null} rows=${rows || []} empty="Nothing here yet. Posts you make from this page and with /embed show up here." columns=${[
      { key: 'channel', label: 'Channel', sort: true, filter: true, width: '170px', text: (row) => channelName(guild, row.channel_id), render: (row) => '#' + channelName(guild, row.channel_id) },
      { key: 'summary', label: 'Message', filter: true, render: (row) => html`<div class="truncate">${row.summary}</div>
        ${row.deleted_at && html` <span class="badge red">Deleted</span>`}
        ${row.adopted_from && html` <span class="badge outline">From ${row.adopted_from}</span>`}` },
      { key: 'posted_by_name', label: 'Posted by', sort: true, filter: true, width: '150px', text: (row) => row.posted_by_name || SOURCES[row.source] || '' },
      { key: 'posted_at', label: 'Posted', sort: true, width: '165px', render: (row) => when(row.posted_at) },
      { key: 'edited_at', label: 'Edited', sort: true, width: '165px', text: (row) => row.edited_at || 0, render: (row) => when(row.edited_at) },
      { key: 'version', label: 'Versions', sort: true, width: '90px' },
      { key: 'actions', label: '', width: '150px', render: (row) => html`<div class="row" style=${{ flexWrap: 'nowrap', gap: '2px' }}>
        ${!row.deleted_at && html`<${JumpLink} url=${row.url} />`}
        ${!row.deleted_at && html`<button class="icon-btn" aria-label="Edit" title="Edit" onClick=${() => onOpen(row.url)}><${Icon} name="pencil" /></button>`}
        ${row.deleted_at && html`<button class="icon-btn" aria-label="Post it again" title="Post it again" disabled=${reposting === row.id} onClick=${() => repost(row)}><${Icon} name="refresh-cw" /></button>`}
        <button class="icon-btn" aria-label="Version history" title="Version history" onClick=${() => setHistory(row.id)}><${Icon} name="history" /></button>
        <button class="icon-btn danger" aria-label="Delete" title="Delete" onClick=${() => remove(row)}><${Icon} name="trash-2" /></button>
      </div>` },
    ]} />
    ${history && html`<${HistoryModal} postedId=${history} onClose=${() => setHistory(null)} onChanged=${load} />`}
  <//>`;
}

// ---- scheduled ----
function ScheduleEditor({ post, onClose, onSaved }) {
  const guild = useGuild();
  const [message, setMessage] = useState(post.payload);
  const [channel, setChannel] = useState(post.channel_id);
  const [at, setAt] = useState(localInput(post.due_at));
  const [ping, setPing] = useState(post.ping);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      await api.patch(`/embeds/scheduled/${post.id}`, { payload: message, channel_id: channel, at: fromLocalInput(at), ping });
      toast('Saved.');
      onSaved();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([message, channel, at, ping]);
  const [start] = useState(snapshot);
  return html`<${Modal} title="Edit scheduled post" icon="clock" large onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button><button class="btn primary" disabled=${busy} onClick=${save}>Save</button></div>`}>
    <div class="history wide">
      <div>
        <${MessageEditor} value=${message} onChange=${setMessage} showJson=${false} />
        ${errors.payload && html`<div class="error">${errors.payload}</div>`}
        <hr class="rule" />
        <${ChannelPicker} label="Destination" types=${DESTINATIONS} clearable=${false} value=${channel} onChange=${setChannel} error=${errors.channel_id} />
        <${Field} label="When, in your time zone" error=${errors.at}><input type="datetime-local" aria-label="When to post" value=${at} onInput=${(e) => setAt(e.currentTarget.value)} /><//>
        ${(post.ping || mayPing(guild, channel)) && html`<${Switch} label="Allow this post to ping roles and everyone" value=${ping} onChange=${setPing} />`}
      </div>
      <div><${MessagePreview} value=${message} /></div>
    </div>
  <//>`;
}

function ScheduledTab({ active, refresh, onPosted }) {
  const guild = useGuild();
  const [rows, setRows] = useState(null);
  const [editing, setEditing] = useState(null);
  const load = () => api.get('/embeds/scheduled').then(setRows).catch((error) => { toastError(error); setRows([]); });
  useEffect(() => { if (active) load(); }, [active, refresh]);
  const postNow = async (row) => {
    const sure = await confirmDialog({ title: 'Post it now?', body: `This posts it in #${channelName(guild, row.channel_id)} right away.`, confirm: 'Post now' });
    if (!sure) return;
    try {
      await api.post(`/embeds/scheduled/${row.id}/post`);
      toast('Posted.');
      onPosted();
    } catch (error) {
      toastError(error);
    }
    load();
  };
  const cancel = async (row) => {
    const sure = await confirmDialog({ title: 'Cancel this post?', body: 'It won\'t go out, and its text is gone for good.', confirm: 'Cancel the post', danger: true });
    if (!sure) return;
    try {
      await api.del(`/embeds/scheduled/${row.id}`);
      toast('Canceled.');
      load();
    } catch (error) {
      toastError(error);
    }
  };
  return html`<${Card} icon="clock" title="Scheduled">
    <h2>Scheduled posts</h2>
    <p class="muted">To schedule one, write it on the Create Embed tab and turn on Post it later. If Discord refuses a post when its time comes, it stays here with the reason.</p>
    <${DataTable} loading=${rows === null} rows=${rows || []} empty="Nothing is scheduled." columns=${[
      { key: 'due_at', label: 'When', sort: true, width: '180px', render: (row) => when(row.due_at) },
      { key: 'channel', label: 'Channel', sort: true, filter: true, width: '170px', text: (row) => channelName(guild, row.channel_id), render: (row) => '#' + channelName(guild, row.channel_id) },
      { key: 'summary', label: 'Message', filter: true, render: (row) => html`<div class="truncate">${row.summary}</div>
        ${row.state === 'failed' && html`<div class="error">${row.error}</div>`}` },
      { key: 'created_by_name', label: 'Who', sort: true, filter: true, width: '150px' },
      { key: 'state', label: 'State', width: '100px', render: (row) => (row.state === 'failed' ? html`<span class="badge red">Failed</span>` : row.state === 'posting' ? html`<span class="badge blue">Posting</span>` : html`<span class="badge outline">Waiting</span>`) },
      { key: 'actions', label: '', width: '120px', render: (row) => html`<div class="row" style=${{ flexWrap: 'nowrap', gap: '2px' }}>
        <button class="icon-btn" aria-label="Edit" title="Edit" onClick=${() => setEditing(row)}><${Icon} name="pencil" /></button>
        <button class="icon-btn" aria-label="Post now" title="Post now" onClick=${() => postNow(row)}><${Icon} name="send" /></button>
        <button class="icon-btn danger" aria-label="Cancel this post" title="Cancel this post" onClick=${() => cancel(row)}><${Icon} name="trash-2" /></button>
      </div>` },
    ]} />
    ${editing && html`<${ScheduleEditor} post=${editing} onClose=${() => setEditing(null)} onSaved=${() => { setEditing(null); load(); }} />`}
  <//>`;
}

// ---- adopting another bot's posts ----
const ITEM_ICONS = { waiting: ['clock', 'dim'], posting: ['refresh-cw', ''], posted: ['refresh-cw', ''], done: ['circle-check', 'ok-text'], skipped: ['minus', 'dim'], failed: ['triangle-alert', 'bad-text'] };

function JobCard({ job, onStop, onResume }) {
  const done = job.items.filter((item) => item.state === 'done').length;
  const failed = job.items.find((item) => item.state === 'failed');
  const label = { running: 'Reposting', stopped: 'Stopped', done: 'Finished' }[job.state];
  return html`<${Card} icon="refresh-cw" title=${`${label}: ${done} of ${job.items.length} reposted`} right=${html`<span class="small dim">${when(job.created_at)}${job.created_by_name ? ` by ${job.created_by_name}` : ''}</span>`}
    footer=${job.state === 'running' ? html`<button class="btn outline danger" onClick=${() => onStop(job)}>Stop</button>` : job.state === 'stopped' ? html`
      <button class="btn primary" onClick=${() => onResume(job, false)}>${failed ? 'Try that one again and carry on' : 'Carry on'}</button>
      ${failed && html`<button class="btn" onClick=${() => onResume(job, true)}>Skip that one and carry on</button>`}` : null}>
    <div class="stack">${job.items.map((item, index) => {
      const [icon, tone] = ITEM_ICONS[item.state] || ITEM_ICONS.waiting;
      return html`<div class="job-item" key=${item.id}>
        <span class=${tone}><${Icon} name=${icon} /></span>
        <div style=${{ minWidth: 0, flex: 1 }}>
          <div class="clip">${item.summary || `Message ${index + 1}`}</div>
          ${item.note && html`<div class=${'small ' + (item.state === 'failed' ? 'error' : 'muted')}>${item.note}</div>`}
        </div>
        ${item.url && html`<${JumpLink} url=${item.url} label="Open the copy in Discord" />`}
      </div>`;
    })}</div>
  <//>`;
}

function AdoptTab({ active, seed, onChanged }) {
  const guild = useGuild();
  const [rows, setRows] = useState([]);
  const [picked, setPicked] = useState({});
  const [focus, setFocus] = useState(null);
  const [link, setLink] = useState('');
  const [channel, setChannel] = useState(null);
  const [more, setMore] = useState(false);
  const [canDelete, setCanDelete] = useState(true);
  const [botsOnly, setBotsOnly] = useState(true);
  const [target, setTarget] = useState(null);
  const [remove, setRemove] = useState(false);
  const [reactions, setReactions] = useState(false);
  const [jobs, setJobs] = useState([]);
  const [busy, setBusy] = useState(false);
  // Messages added by link, kept apart from a channel's list so showing a list doesn't drop them.
  const linked = useRef(new Set());

  const loadJobs = () => api.get('/embeds/adoptions').then(setJobs).catch(toastError);
  useEffect(() => { if (active) loadJobs(); }, [active]);
  const running = jobs.some((job) => job.state === 'running');
  useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(loadJobs, 1500);
    return () => clearInterval(timer);
  }, [running]);
  useEffect(() => { if (!running && jobs.length) onChanged(); }, [running]);

  const canPick = (row) => !row.mine && !row.empty;
  const add = (row) => {
    linked.current.add(String(row.id));
    setRows((list) => (list.some((r) => sameId(r.id, row.id)) ? list : [...list, row].sort((a, b) => (BigInt(a.id) < BigInt(b.id) ? -1 : 1))));
    setFocus(row);
    if (canPick(row)) setPicked((map) => ({ ...map, [row.id]: row }));
  };
  useEffect(() => { if (seed) add(seed.message); }, [seed]);

  const addLink = async () => {
    setBusy(true);
    try {
      const result = await api.get(`/embeds/lookup?link=${encodeURIComponent(link)}`);
      add(result.message);
      setLink('');
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const loadChannel = async (older = false) => {
    setBusy(true);
    try {
      // Rows run oldest first across every channel, so the page before this channel's list starts at this channel's own oldest row.
      const own = rows.find((row) => sameId(row.channel_id, channel) && !linked.current.has(String(row.id)));
      const oldest = older && own ? `&before=${own.id}` : '';
      const result = await api.get(`/embeds/channel-messages?channel_id=${channel}${oldest}`);
      const fetched = [...result.messages].reverse();
      const byLink = (list) => list.filter((row) => linked.current.has(String(row.id)) && !fetched.some((f) => sameId(f.id, row.id)));
      setRows((list) => (older ? [...fetched, ...list] : [...fetched, ...byLink(list)].sort((a, b) => (BigInt(a.id) < BigInt(b.id) ? -1 : 1))));
      if (!older) {
        setPicked((map) => Object.fromEntries(Object.entries(map).filter(([id]) => linked.current.has(String(id)))));
        setFocus(fetched.length ? fetched[fetched.length - 1] : null);
      }
      setMore(result.more);
      setCanDelete(result.can_delete);
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const toggle = (row, on) => setPicked((map) => {
    const next = { ...map };
    if (on) next[row.id] = row;
    else delete next[row.id];
    return next;
  });
  const shown = rows.filter((row) => !botsOnly || row.author_bot);
  const count = Object.keys(picked).length;
  const pickAll = () => setPicked((map) => {
    const next = { ...map };
    for (const row of shown) if (canPick(row) && !row.adopted_url && !row.mine) next[row.id] = row;
    return next;
  });
  const start = async () => {
    const sure = await confirmDialog({
      title: `Repost ${count} message${count === 1 ? '' : 's'}?`,
      body: `They go out one at a time, oldest first${target ? ` in #${channelName(guild, target)}` : ', each in its own channel'}.${remove ? ' Each original is deleted once its copy is up.' : ' The originals stay.'}`,
      confirm: 'Repost',
    });
    if (!sure) return;
    setBusy(true);
    try {
      const job = await api.post('/embeds/adoptions', {
        messages: Object.values(picked).map((row) => ({ channel_id: row.channel_id, message_id: row.id })),
        channel_id: target, delete_originals: remove, copy_reactions: reactions,
      });
      setJobs((list) => [job, ...list]);
      setPicked({});
      setRows([]);
      setFocus(null);
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const stop = async (job) => {
    try { await api.post(`/embeds/adoptions/${job.id}/stop`); } catch (error) { toastError(error); }
    loadJobs();
  };
  const resume = async (job, skip) => {
    try { await api.post(`/embeds/adoptions/${job.id}/resume`, { skip }); } catch (error) { toastError(error); }
    loadJobs();
  };

  return html`<div>
    <${Grid}>
      <${Col}>
        <${Card} icon="refresh-cw" title="Adopt messages">
          <h2>Repost another bot's messages</h2>
          <p class="muted">Discord only lets a bot edit its own messages. Repost a message here and this bot owns the copy, with the same text and embeds, so you can edit it from then on.</p>
          <${Field} label="Paste a message link">
            <div class="input-group">
              <input type="text" placeholder="Enter message link" aria-label="Message link to adopt" value=${link} onInput=${(e) => setLink(e.currentTarget.value)} onKeyDown=${(e) => { if (e.key === 'Enter' && link) addLink(); }} />
              <button class="btn primary" disabled=${!link || busy} onClick=${addLink}>Add</button>
            </div>
          <//>
          <${ChannelPicker} label="Or pick a channel" types=${DESTINATIONS} value=${channel} onChange=${setChannel} />
          <div class="row" style=${{ marginBottom: '12px' }}>
            <button class="btn primary" disabled=${!channel || busy} onClick=${() => loadChannel(false)}>${busy ? 'Loading...' : 'Show its messages'}</button>
            <${Check} label="Only messages from bots" value=${botsOnly} onChange=${setBotsOnly} />
          </div>
          ${rows.length > 0 && html`<div>
            <div class="spread" style=${{ marginBottom: '6px' }}>
              <b>${count} picked</b>
              <div class="row">
                <button class="btn small" onClick=${pickAll}>Pick all shown</button>
                <button class="btn small ghost" disabled=${!count} onClick=${() => setPicked({})}>Clear</button>
              </div>
            </div>
            <div class="pick-list">
              ${more && html`<button type="button" class="btn block ghost" disabled=${busy} onClick=${() => loadChannel(true)}>Show older messages</button>`}
              ${shown.length === 0 && html`<${Empty}>No messages from bots here. Turn off the filter to see the rest.<//>`}
              ${shown.map((row) => html`<div key=${row.id} role="button" tabindex="0" aria-pressed=${!!(focus && sameId(focus.id, row.id))} class=${'pick-row' + (focus && sameId(focus.id, row.id) ? ' on' : '')} onClick=${() => setFocus(row)}
                onKeyDown=${(e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) { e.preventDefault(); setFocus(row); } }}>
                <input type="checkbox" aria-label=${'Pick this message from ' + row.author_name} checked=${!!picked[row.id]} disabled=${!canPick(row)} onClick=${(e) => e.stopPropagation()} onChange=${(e) => toggle(row, e.currentTarget.checked)} />
                <div style=${{ minWidth: 0, flex: 1 }}>
                  <div><b>${row.author_name}</b>${row.author_bot && html` <span class="dc-tag">APP</span>`} <span class="small dim">${when(row.created_at)}</span></div>
                  <div class="clip muted">${row.summary}</div>
                  ${row.mine && html`<span class="badge outline">This bot wrote it</span>`}
                  ${row.adopted_url && html`<a class="badge green" href=${row.adopted_url} target="_blank" rel="noopener noreferrer" onClick=${(e) => e.stopPropagation()}>Already reposted</a>`}
                  ${row.reaction_roles && html` <span class="badge blue">Reaction roles move too</span>`}
                  ${row.attachments.length > 0 && html` <span class="badge outline">${row.attachments.length} file${row.attachments.length === 1 ? '' : 's'}</span>`}
                  ${row.controls > 0 && html` <span class="badge gold">Its buttons or menus won't carry over</span>`}
                  ${row.stickers > 0 && html` <span class="badge gold">Its sticker won't carry over</span>`}
                </div>
              </div>`)}
            </div>
          </div>`}
        <//>
      <//>
      <div class="col tall">
        <div class="sticky-col">
          <${Card} icon="eye" title="Preview">
            ${focus ? html`<${MessagePreview} value=${focus.payload} name=${focus.author_name} />` : html`<${Empty}>Pick a message to see it here.<//>`}
          <//>
          <${Card} icon="send" title="Repost" footer=${html`<button class="btn primary" disabled=${!count || busy} onClick=${start}>Repost ${count || ''} message${count === 1 ? '' : 's'}</button>`}>
            <${ChannelPicker} label="Destination" types=${DESTINATIONS} placeholder="The same channel as each original" value=${target} onChange=${setTarget} />
            <${Switch} label="Delete the originals" help="Each original goes once its copy is up and its reaction roles have moved. This needs Manage Messages for you and for the bot." value=${remove} onChange=${setRemove} />
            ${remove && !canDelete && html`<${Notice} kind="warn">The bot lacks Manage Messages in that channel, so the originals would stay.<//>`}
            <${Switch} label="Add the original's reactions to the copy" help="The bot adds one of each emoji. Reaction role messages get theirs from the reaction roles feature either way." value=${reactions} onChange=${setReactions} />
          <//>
        </div>
      </div>
    <//>
    ${jobs.length > 0 && html`<div style=${{ marginTop: '18px' }}><h3>Recent reposts</h3>
      <div class="stack">${jobs.slice(0, 5).map((job) => html`<${JobCard} key=${job.id} job=${job} onStop=${stop} onResume=${resume} />`)}</div></div>`}
  </div>`;
}

// ---- the page ----
const TABS = [
  { key: 'create', label: 'Create Embed' },
  { key: 'edit', label: 'Edit Embed' },
  { key: 'messages', label: 'Messages' },
  { key: 'scheduled', label: 'Scheduled' },
  { key: 'adopt', label: 'Adopt' },
];

export default function EmbedsPage() {
  const startLink = useMemo(() => new URLSearchParams(location.search).get('edit'), []);
  const [tab, setTab] = useState(startLink ? 'edit' : 'create');
  const [editRequest, setEditRequest] = useState(startLink ? { link: startLink } : null);
  const [adoptSeed, setAdoptSeed] = useState(null);
  const [refresh, setRefresh] = useState(0);
  const changed = () => setRefresh((n) => n + 1);
  const openInEditor = (link) => {
    setEditRequest({ link });
    setTab('edit');
  };
  const adopt = (message) => {
    setAdoptSeed({ message });
    setTab('adopt');
  };
  // Every tab stays mounted, so moving between them can't lose what's in an editor.
  const pane = (key, content) => html`<div class=${tab === key ? '' : 'hidden'}>${content}</div>`;
  return html`<div>
    <${Tabs} tabs=${TABS} active=${tab} onChange=${setTab} />
    ${pane('create', html`<${CreateTab} onOpen=${openInEditor} />`)}
    ${pane('edit', html`<${EditTab} request=${editRequest} onAdopt=${adopt} onChanged=${changed} />`)}
    ${pane('messages', html`<${MessagesTab} active=${tab === 'messages'} refresh=${refresh} onOpen=${openInEditor} />`)}
    ${pane('scheduled', html`<${ScheduledTab} active=${tab === 'scheduled'} refresh=${refresh} onPosted=${changed} />`)}
    ${pane('adopt', html`<${AdoptTab} active=${tab === 'adopt'} seed=${adoptSeed} onChanged=${changed} />`)}
  </div>`;
}
