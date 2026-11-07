// The message editor: text, up to ten embeds and link buttons, with a live preview that looks like Discord.
// Tags, triggers, greetings, reaction roles, sticky messages and the Embeds page all edit the same form:
//   { content: '', embeds: [ <Discord embed JSON> ], buttons: [ { label, url, emoji } ] }
import { html, useEffect, useState } from './lib.js';
import { Markdown } from './markdown.js';
import { EmojiButton, EmojiPicker, EmojiView, HexInput, Icon, TextArea, copyText, toast, useGuild } from './ui.js';

export const emptyMessage = () => ({ content: '', embeds: [], buttons: [] });
export const hasEmbeds = (message) => !!(message && message.embeds && message.embeds.length);

const LIMITS = { title: 256, description: 4096, field: 25, fieldName: 256, fieldValue: 1024, footer: 2048, author: 256, total: 6000, embeds: 10, content: 2000, buttons: 25 };
const SWATCHES = ['#1abc9c', '#2ecc71', '#3498db', '#9b59b6', '#e91e63', '#f1c40f', '#e67e22', '#e74c3c', '#95a5a6', '#607d8b', '#00a2f8', '#202225'];

// A color in pasted JSON can be a number or text, and a bad one must not break the page.
function toHex(value) {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string' && /^#?[0-9a-fA-F]{6}$/.test(value.trim())) return '#' + value.trim().replace('#', '').toLowerCase();
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 && number <= 0xffffff ? '#' + number.toString(16).padStart(6, '0') : '';
}
// Discord would never fetch anything else, and an attachment link means nothing to a browser.
const shownUrl = (url) => (typeof url === 'string' && /^https?:\/\//i.test(url) ? url : null);

// An image in a message someone else wrote would tell its host who opened the page. So the preview loads images from Discord's own hosts and from addresses
// the person typed or pasted into the editor themselves. Any other image waits behind a click, which then allows that host until the page is closed.
const DISCORD_IMAGE_HOSTS = /^(cdn\.discordapp\.com|media\.discordapp\.net|images-ext-\d+\.discordapp\.net)$/;
const typedUrls = new Set();
const allowedHosts = new Set();
export function hostOf(url) {
  try { return new URL(url).hostname.toLowerCase(); } catch (error) { return ''; }
}
export function trustTyped(url) {
  if (typeof url === 'string' && url) typedUrls.add(url);
}
export function imageState(url) {
  if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) return 'none';
  if (!/^https:\/\//i.test(url)) return 'proxied';
  const host = hostOf(url);
  if (!host) return 'none';
  return DISCORD_IMAGE_HOSTS.test(host) || typedUrls.has(url) || allowedHosts.has(host) ? 'show' : 'ask';
}
const imageWatchers = new Set();
function RemoteImage({ url, class: cls }) {
  const [, again] = useState(0);
  // Allowing a host shows every waiting picture from it, in every preview on the page.
  useEffect(() => {
    const redraw = () => again((n) => n + 1);
    imageWatchers.add(redraw);
    return () => { imageWatchers.delete(redraw); };
  }, []);
  const state = imageState(url);
  if (state === 'none') return null;
  if (state === 'show') return html`<img class=${cls} src=${url} alt="" referrerpolicy="no-referrer" />`;
  if (state === 'proxied') return html`<span class="dc-remote dim">Discord shows an http image through its own proxy, which this preview can't.</span>`;
  const host = hostOf(url);
  return html`<button type="button" class="dc-remote" onClick=${() => { allowedHosts.add(host); imageWatchers.forEach((redraw) => redraw()); }}>Load image from ${host}</button>`;
}

export function embedSize(embed) {
  let size = (embed.title || '').length + (embed.description || '').length;
  size += ((embed.footer && embed.footer.text) || '').length + ((embed.author && embed.author.name) || '').length;
  for (const field of embed.fields || []) size += (field.name || '').length + (field.value || '').length;
  return size;
}

function ColorField({ value, onChange }) {
  const [open, setOpen] = useState(false);
  const hex = toHex(value);
  const pick = (text) => onChange(text ? parseInt(text.slice(1), 16) : null);
  return html`<div class="row" style=${{ marginBottom: '10px' }}>
    <button type="button" class="btn primary small" onClick=${() => setOpen(!open)}>Toggle color picker</button>
    <${HexInput} label="Embed color as hex" value=${hex} onChange=${pick} />
    ${open && html`<div class="row" style=${{ width: '100%', marginTop: '6px' }}>
      <input class="color-input" type="color" aria-label="Embed color" value=${hex || '#202225'} onInput=${(e) => onChange(parseInt(e.currentTarget.value.slice(1), 16))} />
      <div class="swatches">${SWATCHES.map((c) => html`<button key=${c} type="button" class="swatch" aria-label=${c} style=${{ background: c }} onClick=${() => onChange(parseInt(c.slice(1), 16))}></button>`)}</div>
      <button type="button" class="btn ghost small" onClick=${() => onChange(null)}>No color</button>
    </div>`}
  </div>`;
}

function localDate(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function EmbedEditor({ value, onChange, onRemove, onMoveUp, onMoveDown }) {
  const embed = value || {};
  const set = (key, next) => {
    const copy = { ...embed };
    if (next === '' || next === null || next === undefined) delete copy[key];
    else copy[key] = next;
    onChange(copy);
  };
  const setPart = (part, key, next) => {
    const copy = { ...(embed[part] || {}) };
    if (next === '' || next === null) delete copy[key];
    else copy[key] = next;
    set(part, Object.keys(copy).length ? copy : null);
  };
  const fields = embed.fields || [];
  const setField = (index, key, next) => set('fields', fields.map((f, i) => (i === index ? { ...f, [key]: next } : f)));
  const moveField = (index, change) => {
    const target = index + change;
    if (target < 0 || target >= fields.length) return;
    const copy = [...fields];
    [copy[index], copy[target]] = [copy[target], copy[index]];
    set('fields', copy);
  };
  const size = embedSize(embed);
  const borderColor = toHex(embed.color) || 'var(--panel-deep)';
  return html`<div class="embed-edit" style=${{ borderLeftColor: borderColor }}>
    <div class="spread">
      <${ColorField} value=${embed.color} onChange=${(c) => set('color', c)} />
      <div class="row" style=${{ marginBottom: '10px' }}>
        <span class=${'small ' + (size > LIMITS.total ? 'error' : 'dim')}>${size}/${LIMITS.total}</span>
        ${onMoveUp && html`<button type="button" class="icon-btn" aria-label="Move this embed up" title="Move this embed up" onClick=${onMoveUp}><${Icon} name="arrow-up" /></button>`}
        ${onMoveDown && html`<button type="button" class="icon-btn" aria-label="Move this embed down" title="Move this embed down" onClick=${onMoveDown}><${Icon} name="arrow-down" /></button>`}
        ${onRemove && html`<button type="button" class="icon-btn danger" aria-label="Remove this embed" title="Remove this embed" onClick=${onRemove}><${Icon} name="trash-2" /></button>`}
      </div>
    </div>
    <div class="grid-3">
      <input type="text" placeholder="Icon url" aria-label="Author icon link" value=${(embed.author && embed.author.icon_url) || ''} onInput=${(e) => { trustTyped(e.currentTarget.value); setPart('author', 'icon_url', e.currentTarget.value); }} />
      <input type="text" placeholder="Name" aria-label="Author name" value=${(embed.author && embed.author.name) || ''} onInput=${(e) => setPart('author', 'name', e.currentTarget.value)} />
      <input type="text" placeholder="Name url" aria-label="Author link" value=${(embed.author && embed.author.url) || ''} onInput=${(e) => setPart('author', 'url', e.currentTarget.value)} />
    </div>
    <div class="grid-title">
      <input type="text" placeholder="Title" aria-label="Title" value=${embed.title || ''} onInput=${(e) => set('title', e.currentTarget.value)} />
      <input type="text" placeholder="Title url" aria-label="Title link" value=${embed.url || ''} onInput=${(e) => set('url', e.currentTarget.value)} />
    </div>
    <div class=${'counter' + ((embed.title || '').length > LIMITS.title ? ' over' : '')}>${(embed.title || '').length}/${LIMITS.title}</div>
    <div style=${{ marginTop: '10px' }}>
      <textarea rows="5" placeholder="Description" aria-label="Description" value=${embed.description || ''} onInput=${(e) => set('description', e.currentTarget.value)}></textarea>
      <div class=${'counter' + ((embed.description || '').length > LIMITS.description ? ' over' : '')}>${(embed.description || '').length}/${LIMITS.description}</div>
    </div>
    ${fields.map((field, index) => html`<div class="field-edit" key=${index}>
      <div class="spread" style=${{ marginBottom: '6px' }}>
        <input type="text" placeholder="Title for field" aria-label=${'Field ' + (index + 1) + ' name'} value=${field.name || ''} onInput=${(e) => setField(index, 'name', e.currentTarget.value)} />
        <button type="button" class="icon-btn" aria-label="Move up" onClick=${() => moveField(index, -1)}><${Icon} name="arrow-up" /></button>
        <button type="button" class="icon-btn" aria-label="Move down" onClick=${() => moveField(index, 1)}><${Icon} name="arrow-down" /></button>
        <button type="button" class="icon-btn danger" aria-label="Remove field" onClick=${() => set('fields', fields.filter((_, i) => i !== index))}><${Icon} name="trash-2" /></button>
      </div>
      <div class=${'counter' + ((field.name || '').length > LIMITS.fieldName ? ' over' : '')} style=${{ marginBottom: '6px' }}>${(field.name || '').length}/${LIMITS.fieldName}</div>
      <textarea rows="3" placeholder="Description for field" aria-label=${'Field ' + (index + 1) + ' value'} value=${field.value || ''} onInput=${(e) => setField(index, 'value', e.currentTarget.value)}></textarea>
      <div class="spread"><label class="check"><input type="checkbox" checked=${!!field.inline} onChange=${(e) => setField(index, 'inline', e.currentTarget.checked)} /><span>Inline</span></label>
        <span class=${'counter' + ((field.value || '').length > LIMITS.fieldValue ? ' over' : '')}>${(field.value || '').length}/${LIMITS.fieldValue}</span></div>
    </div>`)}
    <button type="button" class="btn block" style=${{ marginTop: '10px' }} disabled=${fields.length >= LIMITS.field}
      onClick=${() => set('fields', [...fields, { name: '', value: '', inline: false }])}>Add field</button>
    <div class="counter" style=${{ marginBottom: '10px' }}>${fields.length}/${LIMITS.field} fields</div>
    <div class="grid-2">
      <input type="text" placeholder="Image url (big at the bottom)" aria-label="Image link" value=${(embed.image && embed.image.url) || ''} onInput=${(e) => { trustTyped(e.currentTarget.value); set('image', e.currentTarget.value ? { url: e.currentTarget.value } : null); }} />
      <input type="text" placeholder="Thumbnail url (small top right)" aria-label="Thumbnail link" value=${(embed.thumbnail && embed.thumbnail.url) || ''} onInput=${(e) => { trustTyped(e.currentTarget.value); set('thumbnail', e.currentTarget.value ? { url: e.currentTarget.value } : null); }} />
    </div>
    <div class="grid-foot">
      <input type="text" placeholder="Footer" aria-label="Footer" value=${(embed.footer && embed.footer.text) || ''} onInput=${(e) => setPart('footer', 'text', e.currentTarget.value)} />
      <input type="text" placeholder="Footer icon" aria-label="Footer icon link" value=${(embed.footer && embed.footer.icon_url) || ''} onInput=${(e) => { trustTyped(e.currentTarget.value); setPart('footer', 'icon_url', e.currentTarget.value); }} />
      <div class="input-group">
        <input type="datetime-local" aria-label="Timestamp" value=${localDate(embed.timestamp)} onInput=${(e) => set('timestamp', e.currentTarget.value ? new Date(e.currentTarget.value).toISOString() : null)} />
        <button type="button" class="btn danger" onClick=${() => set('timestamp', null)}>Clear</button>
      </div>
    </div>
    <div class=${'counter' + (((embed.footer && embed.footer.text) || '').length > LIMITS.footer ? ' over' : '')}>${((embed.footer && embed.footer.text) || '').length}/${LIMITS.footer}</div>
  </div>`;
}

function ButtonEditor({ value, onChange }) {
  const buttons = value || [];
  const set = (index, key, next) => onChange(buttons.map((b, i) => (i === index ? { ...b, [key]: next } : b)));
  return html`<div>
    ${buttons.map((button, index) => html`<div class="row" key=${index} style=${{ marginBottom: '6px', flexWrap: 'nowrap' }}>
      <${EmojiButton} value=${button.emoji} onChange=${(emoji) => set(index, 'emoji', emoji)} onClear=${() => set(index, 'emoji', '')} />
      <input type="text" placeholder="Label" aria-label="Button label" value=${button.label || ''} onInput=${(e) => set(index, 'label', e.currentTarget.value)} />
      <input type="text" placeholder="https://..." aria-label="Button link" value=${button.url || ''} onInput=${(e) => set(index, 'url', e.currentTarget.value)} />
      <button type="button" class="icon-btn danger" aria-label="Remove button" onClick=${() => onChange(buttons.filter((_, i) => i !== index))}><${Icon} name="trash-2" /></button>
    </div>`)}
    <button type="button" class="btn small" disabled=${buttons.length >= LIMITS.buttons} onClick=${() => onChange([...buttons, { label: '', url: '' }])}><${Icon} name="link" size=${14} /> Add link button</button>
  </div>`;
}

function textOfJson(message) {
  const embeds = message.embeds || [];
  const single = !message.content && (!message.buttons || !message.buttons.length) && embeds.length === 1;
  return JSON.stringify(single ? embeds[0] : message, null, 2);
}

class ShapeError extends Error {}

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
function requireText(value, where) {
  if (value !== undefined && value !== null && typeof value !== 'string') throw new ShapeError(`${where} must be text.`);
}
function requireObject(value, where) {
  if (value !== undefined && value !== null && !isObject(value)) throw new ShapeError(`${where} must be a set of fields.`);
}

// Pasted JSON keeps whatever shape it came in, and the preview and editor read it as a message. These are the same checks the server makes, so a wrong shape
// is refused with a sentence and never reaches the page.
function checkEmbed(embed, number) {
  const where = `Embed ${number}`;
  if (!isObject(embed)) throw new ShapeError(`${where} must be a set of fields.`);
  for (const key of ['title', 'description', 'url', 'timestamp']) requireText(embed[key], `${where} ${key}`);
  if (embed.color !== undefined && embed.color !== null && typeof embed.color !== 'number' && typeof embed.color !== 'string') throw new ShapeError(`${where} color must be a number or text.`);
  for (const part of ['author', 'footer', 'image', 'thumbnail']) {
    requireObject(embed[part], `${where} ${part}`);
    for (const key of ['name', 'url', 'icon_url', 'text']) if (embed[part]) requireText(embed[part][key], `${where} ${part} ${key}`);
  }
  if (embed.fields !== undefined && embed.fields !== null) {
    if (!Array.isArray(embed.fields)) throw new ShapeError(`${where} fields must be a list.`);
    embed.fields.forEach((field, index) => {
      if (!isObject(field)) throw new ShapeError(`${where} field ${index + 1} must be a set of fields.`);
      requireText(field.name, `${where} field ${index + 1} name`);
      requireText(field.value, `${where} field ${index + 1} value`);
    });
  }
  // Whatever the person pasted is theirs, so its pictures load in the preview.
  for (const part of ['author', 'footer']) if (embed[part]) trustTyped(embed[part].icon_url);
  for (const part of ['image', 'thumbnail']) if (embed[part]) trustTyped(embed[part].url);
}

function checkMessage(message) {
  requireText(message.content, 'The message text');
  if (message.embeds !== undefined && message.embeds !== null && !Array.isArray(message.embeds)) throw new ShapeError('Embeds must be a list.');
  (message.embeds || []).forEach((embed, index) => checkEmbed(embed, index + 1));
  if (message.buttons !== undefined && message.buttons !== null && !Array.isArray(message.buttons)) throw new ShapeError('Buttons must be a list.');
  (message.buttons || []).forEach((button, index) => {
    if (!isObject(button)) throw new ShapeError(`Button ${index + 1} must be a set of fields.`);
    requireText(button.url, `Button ${index + 1} link`);
    requireText(button.label, `Button ${index + 1} label`);
    requireText(button.emoji, `Button ${index + 1} emoji`);
  });
}

function emojiText(emoji) {
  if (!emoji || typeof emoji === 'string') return emoji || undefined;
  if (!isObject(emoji)) throw new ShapeError('A button emoji must be text or a set of fields.');
  // JSON turns a bare number into a float, which rounds an emoji ID. Discord sends the ID in quotes.
  if (typeof emoji.id === 'number') throw new ShapeError('A button emoji id must be in quotes. As a bare number its last digits change.');
  return emoji.id ? `<${emoji.animated ? 'a' : ''}:${emoji.name || 'emoji'}:${emoji.id}>` : emoji.name;
}

// Takes the shapes people paste: a whole message, one embed, a list of embeds, a backup from a web embed tool, or what /embed source gives.
export function parseJson(text) {
  let data = JSON.parse(text);
  if (Array.isArray(data)) data = { embeds: data };
  if (!isObject(data)) throw new ShapeError('A message must be a set of fields.');
  if (Array.isArray(data.messages) && data.messages[0]) data = data.messages[0].data || data.messages[0];
  if (!isObject(data)) throw new ShapeError('A message must be a set of fields.');
  if (isObject(data.embed)) data = { ...data, embeds: [data.embed] };
  if (!['content', 'embeds', 'buttons', 'components'].some((key) => key in data)) {
    checkEmbed(data, 1);
    return { content: '', embeds: [data], buttons: [] };
  }
  checkMessage(data);
  const buttons = Array.isArray(data.buttons) ? [...data.buttons] : [];
  if (data.components !== undefined && data.components !== null && !Array.isArray(data.components)) throw new ShapeError('Components must be a list.');
  for (const row of data.components || []) {
    if (row !== null && !isObject(row)) throw new ShapeError('Each row of components must be a set of fields.');
    const parts = (row && row.components) || [];
    if (!Array.isArray(parts)) throw new ShapeError('A row of components must hold a list.');
    for (const part of parts) {
      if (!isObject(part)) throw new ShapeError('A component must be a set of fields.');
      if (part.url) {
        requireText(part.url, 'A button link');
        requireText(part.label, 'A button label');
        buttons.push({ url: part.url, label: part.label || '', emoji: emojiText(part.emoji) });
      }
    }
  }
  return { content: data.content || '', embeds: Array.isArray(data.embeds) ? data.embeds : [], buttons };
}

// What to tell the person when pasted JSON is refused.
export function explainJson(error) {
  return error instanceof ShapeError ? error.message : "That isn't valid JSON.";
}

// The same tools in a larger box, for a page with room for them. What's shown follows the editor until someone types in it.
export function JsonPanel({ value, onChange, readOnly = false }) {
  const [draft, setDraft] = useState(null);
  const current = value || emptyMessage();
  const apply = () => {
    try {
      onChange(parseJson(draft === null ? textOfJson(current) : draft));
      setDraft(null);
      toast('Applied.');
    } catch (error) {
      toast(explainJson(error), 'bad');
    }
  };
  return html`<div>
    <textarea class="mono" rows="10" aria-label="Raw JSON" spellcheck="false" readOnly=${readOnly} value=${draft === null ? textOfJson(current) : draft} onInput=${(e) => setDraft(e.currentTarget.value)}></textarea>
    <div class="row" style=${{ marginTop: '8px' }}>
      <button type="button" class="btn outline" onClick=${() => copyText(draft === null ? textOfJson(current) : draft)}><${Icon} name="clipboard" size=${14} /> Copy to clipboard</button>
      ${!readOnly && html`<button type="button" class="btn primary" disabled=${draft === null} onClick=${apply}>Apply</button>`}
      ${draft !== null && html`<button type="button" class="btn ghost" onClick=${() => setDraft(null)}>Discard</button>`}
    </div>
  </div>`;
}

export function JsonTools({ value, onChange }) {
  const [text, setText] = useState('');
  const apply = () => {
    try {
      onChange(parseJson(text));
      setText('');
      toast('Applied.');
    } catch (error) {
      toast(explainJson(error), 'bad');
    }
  };
  return html`<div class="input-group" style=${{ marginTop: '10px' }}>
    <button type="button" class="btn outline" onClick=${() => copyText(textOfJson(value))}>Copy to clipboard</button>
    <input type="text" placeholder="Raw JSON" aria-label="Raw JSON" value=${text} onInput=${(e) => setText(e.currentTarget.value)} />
    <button type="button" class="btn primary" onClick=${apply}>Apply</button>
  </div>`;
}

export function MessageEditor({ value, onChange, collapsed = false, showContent = true, contentLabel, contentPlaceholder = 'Normal text sent with the embed', contentRows = 3, showButtons = true, showJson = true, help }) {
  const message = value || emptyMessage();
  const [open, setOpen] = useState(!collapsed || hasEmbeds(message));
  const [picking, setPicking] = useState(false);
  const embeds = message.embeds || [];
  const set = (key, next) => onChange({ ...message, [key]: next });
  // A template or a reset can fill the form from outside, and embeds nobody can see would go out unnoticed.
  const holdsEmbeds = embeds.length > 0;
  useEffect(() => { if (holdsEmbeds) setOpen(true); }, [holdsEmbeds]);
  const total = embeds.reduce((sum, embed) => sum + embedSize(embed), 0);
  const moveEmbed = (index, change) => {
    const copy = [...embeds];
    [copy[index], copy[index + change]] = [copy[index + change], copy[index]];
    set('embeds', copy);
  };
  return html`<div>
    ${showContent && html`<div style=${{ position: 'relative' }}>
      <${TextArea} label=${contentLabel} rows=${contentRows} value=${message.content} placeholder=${contentPlaceholder} maxLength=${LIMITS.content} onChange=${(v) => set('content', v)} help=${help} />
      <div class="row" style=${{ marginTop: '-8px', marginBottom: '10px' }}>
        <button type="button" class="btn ghost small" onClick=${() => setPicking(!picking)}><${Icon} name="smile-plus" size=${14} /> Emoji</button>
      </div>
      ${picking && html`<${EmojiPicker} onPick=${(emoji) => { set('content', (message.content || '') + emoji); setPicking(false); }} onClose=${() => setPicking(false)} />`}
    </div>`}
    ${collapsed && html`<button type="button" class="btn block" style=${{ marginBottom: '10px' }} onClick=${() => setOpen(!open)}>${open ? 'Hide embed builder' : 'Show embed builder'}</button>`}
    ${open && html`<div>
      ${(embeds.length ? embeds : [{}]).map((embed, index) => html`<${EmbedEditor} key=${index} value=${embed}
        onRemove=${embeds.length > 0 ? () => set('embeds', embeds.filter((_, i) => i !== index)) : null}
        onMoveUp=${index > 0 ? () => moveEmbed(index, -1) : null}
        onMoveDown=${index < embeds.length - 1 ? () => moveEmbed(index, 1) : null}
        onChange=${(next) => set('embeds', embeds.length ? embeds.map((e, i) => (i === index ? next : e)) : [next])} />`)}
      <button type="button" class="btn block" disabled=${embeds.length >= LIMITS.embeds} onClick=${() => set('embeds', [...(embeds.length ? embeds : [{}]), {}])}>Add embed</button>
      <div class=${'counter' + (total > LIMITS.total ? ' over' : '')} style=${{ marginBottom: '10px' }}>${embeds.length}/${LIMITS.embeds} embeds, ${total}/${LIMITS.total} characters across them all</div>
      ${showButtons && html`<${ButtonEditor} value=${message.buttons} onChange=${(buttons) => set('buttons', buttons)} />`}
      ${showJson && html`<${JsonTools} value=${message} onChange=${onChange} />`}
    </div>`}
  </div>`;
}

// ---- preview ----
// The message with each {name} in its text replaced by the sample value, for a preview of a message that is filled in when it is sent.
export function sampleMessage(message, values) {
  const fill = (text) => (typeof text === 'string' ? Object.entries(values).reduce((out, [name, value]) => out.split('{' + name + '}').join(value), text) : text);
  const part = (object, keys) => (object ? { ...object, ...Object.fromEntries(keys.filter((key) => object[key] !== undefined).map((key) => [key, fill(object[key])])) } : object);
  const base = message || emptyMessage();
  return {
    ...base,
    content: fill(base.content),
    embeds: (base.embeds || []).map((embed) => ({
      ...embed,
      title: fill(embed.title),
      description: fill(embed.description),
      author: part(embed.author, ['name']),
      footer: part(embed.footer, ['text']),
      fields: (embed.fields || []).map((field) => part(field, ['name', 'value'])),
    })),
  };
}

export function EmbedPreview({ embed, guild }) {
  const color = toHex(embed.color) || '#1e1f22';
  const fields = embed.fields || [];
  // Discord lays inline fields out in up to three columns.
  const spans = [];
  let run = 0;
  fields.forEach((field, index) => {
    if (field.inline) {
      run += 1;
      const nextInline = fields[index + 1] && fields[index + 1].inline;
      if (run === 3 || !nextInline) {
        const size = 12 / run;
        for (let i = 0; i < run; i += 1) spans[index - i] = size;
        run = 0;
      }
    } else {
      spans[index] = 12;
    }
  });
  let column = 1;
  const thumbnail = embed.thumbnail && shownUrl(embed.thumbnail.url);
  const image = embed.image && shownUrl(embed.image.url);
  const footerIcon = embed.footer && shownUrl(embed.footer.icon_url);
  const stamp = embed.timestamp ? new Date(embed.timestamp) : null;
  const stampText = stamp && !Number.isNaN(stamp.getTime()) ? stamp.toLocaleString() : '';
  return html`<div class=${'dc-embed' + (thumbnail ? ' thumbed' : '')} style=${{ borderLeftColor: color }}>
    ${embed.author && embed.author.name && html`<div class="dc-author"><${RemoteImage} url=${embed.author.icon_url} />
      ${shownUrl(embed.author.url) ? html`<a href=${embed.author.url} target="_blank" rel="noopener noreferrer" style=${{ color: 'inherit' }}>${embed.author.name}</a>` : embed.author.name}</div>`}
    ${embed.title && html`<div class="dc-title">${shownUrl(embed.url) ? html`<a href=${embed.url} target="_blank" rel="noopener noreferrer"><${Markdown} text=${embed.title} guild=${guild} /></a>` : html`<${Markdown} text=${embed.title} guild=${guild} />`}</div>`}
    ${embed.description && html`<div class="dc-desc"><${Markdown} text=${embed.description} guild=${guild} /></div>`}
    ${fields.length > 0 && html`<div class="dc-fields">${fields.map((field, index) => {
      const span = spans[index] || 12;
      const start = span === 12 ? 1 : column;
      column = span === 12 ? 1 : (column + span > 12 ? 1 : column + span);
      return html`<div class="dc-field" key=${index} style=${{ gridColumn: `${start} / span ${span}` }}><b><${Markdown} text=${field.name} guild=${guild} /></b><div style=${{ whiteSpace: 'pre-wrap' }}><${Markdown} text=${field.value} guild=${guild} /></div></div>`;
    })}</div>`}
    ${thumbnail && html`<${RemoteImage} class="dc-thumb" url=${thumbnail} />`}
    ${image && html`<${RemoteImage} class="dc-image" url=${image} />`}
    ${((embed.footer && embed.footer.text) || stampText) && html`<div class="dc-foot">
      ${footerIcon && html`<${RemoteImage} url=${footerIcon} />`}
      <span>${embed.footer && embed.footer.text}${embed.footer && embed.footer.text && stampText ? ' • ' : ''}${stampText}</span>
    </div>`}
  </div>`;
}

export function MessagePreview({ value, name, below }) {
  const guild = useGuild();
  const message = value || emptyMessage();
  const shown = (message.embeds || []).filter((e) => Object.keys(e).length > 0);
  return html`<div class="dc" aria-label="Preview">
    <div class="dc-msg">
      <img class="dc-avatar" src=${guild.me.avatar} alt="" />
      <div class="dc-body">
        <div><span class="dc-name">${name || guild.me.nick || guild.me.name}</span><span class="dc-tag">APP</span><span class="dc-time">Today at ${new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</span></div>
        ${message.content && html`<div class="dc-text"><${Markdown} text=${message.content} guild=${guild} /></div>`}
        ${shown.map((embed, index) => html`<${EmbedPreview} key=${index} embed=${embed} guild=${guild} />`)}
        ${message.buttons && message.buttons.length > 0 && html`<div class="dc-buttons">${message.buttons.map((b, i) => html`<span class="dc-button" key=${i}>${b.emoji && html`<${EmojiView} value=${b.emoji} size=${18} />`}${b.label}<${Icon} name="external-link" size=${14} /></span>`)}</div>`}
        ${!message.content && shown.length === 0 && html`<div class="dc-small">Nothing to post yet.</div>`}
        ${below}
      </div>
    </div>
  </div>`;
}
