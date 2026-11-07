// What the Tags and Triggers pages share: the template box, trying a template out, and moving them in and out as JSON files.
import { html, useEffect, useState } from './lib.js';
import { api } from './api.js';
import { MessagePreview } from './embed.js';
import { Check, DataTable, EmojiPicker, Field, Icon, Modal, Notice, TextArea, TextInput, confirmDialog, toast, toastError } from './ui.js';

export const TEMPLATE_MAX = 25000;

export function TemplateField({ label, value, onChange, error, placeholder }) {
  const [picking, setPicking] = useState(false);
  return html`<div style=${{ position: 'relative' }}>
    <${TextArea} label=${label} rows=${6} value=${value} placeholder=${placeholder} maxLength=${TEMPLATE_MAX} error=${error} onChange=${onChange}
      help=${html`Blocks such as <code>{user}</code> or <code>{if({args}==hi):Hello|Bye}</code> work here. The <a href="/docs#templates" target="_blank" rel="noopener">template language</a> lists them all.`} />
    <div class="row" style=${{ marginTop: '-8px', marginBottom: '10px' }}>
      <button type="button" class="btn ghost small" onClick=${() => setPicking(!picking)}><${Icon} name="smile-plus" size=${14} /> Emoji</button>
    </div>
    ${picking && html`<${EmojiPicker} onPick=${(emoji) => { onChange((value || '') + emoji); setPicking(false); }} onClose=${() => setPicking(false)} />`}
  </div>`;
}

// Runs the template on the server as the signed-in person, without posting or doing anything. `extra` says what the server can't tell from the text, such as whether the switch for trust is on.
export function TryItPanel({ path, message, argsLabel, extra = {} }) {
  const [args, setArgs] = useState('');
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      setResult(await api.post(path, { ...extra, message, args }));
    } catch (error) {
      setResult(null);
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  return html`<div class="field-edit">
    <div class="spread"><b>Try it</b><span class="muted small">Nothing is posted and no block acts.</span></div>
    <div style=${{ marginTop: '8px' }}>
      <${TextInput} label=${argsLabel} value=${args} onChange=${setArgs} button=${html`<button type="button" class="btn primary" disabled=${busy} onClick=${run}><${Icon} name="eye" size=${14} /> Run</button>`} />
    </div>
    ${result && result.error && html`<${Notice} kind="bad">${result.error}<//>`}
    ${result && !result.error && html`<div>
      ${result.message.content || result.message.embeds.length ? html`<${MessagePreview} value=${result.message} />` : html`<p class="muted">It posts nothing.</p>`}
      ${result.does.length > 0 && html`<ul class="small" style=${{ margin: '8px 0 0', paddingLeft: '18px' }}>${result.does.map((line, index) => html`<li key=${index}>${line}</li>`)}</ul>`}
    </div>`}
  </div>`;
}

// What the blocks would do with trust, shown before anyone turns it on. Nobody can vouch for a tag without seeing it.
export async function askBeforeTrusting(path, message, kind) {
  let does = [];
  try {
    const answer = await api.post(path, { message, trusted: true });
    if (answer.error) {
      toast(answer.error, 'bad');
      return false;
    }
    does = answer.does;
  } catch (error) {
    toastError(error);
    return false;
  }
  const body = does.length ? `With trust, this ${kind} does the following for everyone. ${does.join(' ')}` : `This ${kind} has no blocks that act, so trust changes only who it can ping and where it can post.`;
  return confirmDialog({ title: `Trust this ${kind}?`, body, confirm: 'Trust it' });
}
export function downloadJson(name, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const ACTION_WORDS = { create: 'Add', replace: 'Replace', skip: 'Skip' };

// Reads a file or pasted JSON, shows what would happen, and only then imports. `withCode` adds the share code box. `startCode` arrives from a share link and is checked at once.
export function ImportModal({ title, path, nameKey, nameLabel, withCode, startCode = '', onClose, onDone }) {
  const [data, setData] = useState('');
  const [code, setCode] = useState(startCode);
  const [replace, setReplace] = useState(false);
  const [rows, setRows] = useState(null);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState({});
  const send = async (preview, useCode = code) => {
    setBusy(true);
    setErrors({});
    try {
      const body = { replace, preview };
      if (useCode.trim()) body.code = useCode.trim();
      else body.data = data;
      const answer = await api.post(path, body);
      if (preview) {
        setRows(answer.rows);
      } else {
        toast(`Imported ${answer.imported}.`);
        onDone();
      }
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => { if (startCode) send(true, startCode); }, []);
  const readFile = async (event) => {
    const file = event.currentTarget.files[0];
    if (file) setData(await file.text());
    setRows(null);
  };
  const ready = rows && rows.some((row) => row.action !== 'skip');
  // Closing with something changed asks first.
  const snapshot = () => JSON.stringify([data, code, replace]);
  const [start] = useState(snapshot);
  return html`<${Modal} large icon="upload" title=${title} onClose=${onClose} dirty=${snapshot() !== start} footer=${html`<div class="right">
      <button class="btn ghost" onClick=${onClose}>Cancel</button>
      <button class="btn" disabled=${busy || (!data.trim() && !code.trim())} onClick=${() => send(true)}>Check</button>
      <button class="btn success" disabled=${busy || !ready} onClick=${() => send(false)}>Import</button>
    </div>`}>
    ${withCode && html`<${TextInput} label="Share code" placeholder="From /tag share in another server" value=${code} error=${errors.code} onChange=${(v) => { setCode(v); setRows(null); }} />`}
    <${Field} label="File"><input type="file" accept=".json,application/json" aria-label="Choose a JSON file" onChange=${readFile} /><//>
    <${TextArea} label="Or paste JSON" rows=${6} value=${data} counter=${false} error=${errors.data} onChange=${(v) => { setData(v); setRows(null); }} />
    <${Check} label="Replace what already exists" value=${replace} onChange=${(v) => { setReplace(v); setRows(null); }} />
    ${rows && html`<hr /><p class="muted">Press Check again after changing anything. Blocks listed under Runs are commands this will be able to run with your trust, so read them first.</p>
      <${DataTable} rows=${rows} rowKey="position" pageSizes=${[10]} empty="Nothing to import." columns=${[
        { key: nameKey, label: nameLabel },
        { key: 'action', label: 'Action', width: '90px', render: (row) => ACTION_WORDS[row.action] },
        { key: 'reason', label: 'Note', render: (row) => row.reason || '' },
        ...(withCode ? [{ key: 'commands', label: 'Runs', render: (row) => (row.commands.length ? row.commands.map((text, index) => html`<div key=${index}><code>${text}</code></div>`) : '') }] : []),
      ]} />`}
  <//>`;
}
