// The welcome picture: when it goes out, the ready-made layouts and the layer designer, with a preview the bot draws.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, Icon, Loading, Notice, SaveBar, SaveButton, Switch, Tabs, confirmDialog, toastError, useGuild, useSettings } from '../ui.js';
import { ColorField, FillPicker, FontPicker, PreviewNotes, TEXT_HELP, TemplateArea, usePreview, useUploads } from '../welcomecard/parts.js';
import { Designer, draftOfLayout } from '../welcomecard/designer.js';

const POSITION_NAMES = {
  top_left: 'Top left', top: 'Top', top_right: 'Top right', left: 'Left', center: 'Center', right: 'Right', bottom_left: 'Bottom left', bottom: 'Bottom', bottom_right: 'Bottom right',
};
const SHAPE_NAMES = { circle: 'Circle', rounded: 'Rounded', square: 'Square', hexagon: 'Hexagon' };

// A whole number typed into a box, passed on only while it is inside its range, so a half-typed number never reaches the preview.
function WholeNumber({ label, help, error, value, low, high, onChange, disabled }) {
  const [draft, setDraft] = useState(null);
  const inside = (text) => /^\d+$/.test(text) && Number(text) >= low && Number(text) <= high;
  const bad = draft !== null && !inside(draft);
  return html`<div class="field">
    <label>${label}</label>
    <div class="input-group wc-narrow">
      <input type="number" class=${bad ? 'wc-bad' : ''} aria-label=${label} aria-invalid=${bad} min=${low} max=${high} disabled=${disabled} value=${draft === null ? value : draft}
        onInput=${(e) => { const text = e.currentTarget.value; setDraft(text); if (inside(text)) onChange(Number(text)); }} onBlur=${() => setDraft(null)} />
      <span class="addon">pixels</span>
    </div>
    ${bad && html`<div class="error">From ${low} to ${high}.</div>`}
    ${help && html`<div class="help">${help}</div>`}
    ${error && html`<div class="error">${error}</div>`}
  </div>`;
}

function LayoutPicker({ value, onChange, catalog }) {
  const guild = useGuild();
  return html`<div class="wc-layouts" role="listbox" aria-label="Layout">
    ${catalog.layouts.map((row) => html`<button type="button" key=${row.key} role="option" aria-selected=${value === row.key} class=${'wc-layout' + (value === row.key ? ' on' : '')} title=${row.note} onClick=${() => onChange(row.key)}>
      <img src=${`/api/g/${guild.id}/welcomecard/layouts/${row.key}`} alt="" loading="lazy" style=${{ aspectRatio: `${row.width} / ${row.height}` }} />
      <span class="wc-layout-name">${row.name} <span class="dim">${row.width} x ${row.height}</span></span>
    </button>`)}
  </div>`;
}

// Where the text sits in its space: left to the layout, or one of nine places.
function PositionPicker({ value, onChange, catalog, layout }) {
  const own = (catalog.layouts.find((row) => row.key === layout) || {}).position;
  const places = catalog.positions.filter((key) => key !== 'auto');
  return html`<div class="field">
    <label>Text position</label>
    <div class="wc-positions">
      <button type="button" class=${'btn small' + (value === 'auto' ? ' primary' : '')} aria-pressed=${value === 'auto'} onClick=${() => onChange('auto')}>Automatic</button>
      <div class="wc-nine" role="group" aria-label="Text position">
        ${places.map((key) => html`<button type="button" key=${key} class=${'wc-place wc-place-' + key + (value === key ? ' on' : '') + (value === 'auto' && own === key ? ' own' : '')} aria-pressed=${value === key}
          aria-label=${POSITION_NAMES[key]} title=${POSITION_NAMES[key]} onClick=${() => onChange(key)}><span></span></button>`)}
      </div>
    </div>
    <div class="help">Automatic puts the text where each layout reads best${own ? html`, which is ${POSITION_NAMES[own].toLowerCase()} for this one` : ''}.</div>
  </div>`;
}

function ReadyMade({ form, catalog, uploads, onCustomize }) {
  const data = form.data;
  const set = (field) => (value) => form.set(field, value);
  return html`<div class="wc-cards">
    <${Switches} form=${form} />
    <${Card} icon="layers-3" title="Layout" footer=${html`<${SaveButton} form=${form} />`}>
      <${LayoutPicker} value=${data.layout} catalog=${catalog} onChange=${set('layout')} />
      ${form.errors.layout && html`<div class="error">${form.errors.layout}</div>`}
    <//>
    <${Card} icon="palette" title="Background" footer=${html`<${SaveButton} form=${form} />`}>
      <${FillPicker} value=${data.background} catalog=${catalog} uploads=${uploads} onChange=${set('background')} error=${form.errors.background} />
    <//>
    <${Card} icon="pencil" title="Text" footer=${html`<${SaveButton} form=${form} />`}>
      <${TemplateArea} label="Text on the picture" value=${data.text} maxLength=${catalog.limits.text} rows=${4} help=${TEXT_HELP} error=${form.errors.text} onChange=${set('text')} />
      <p class="muted small">The text wraps and shrinks to fit its space, so a long name never runs off the picture.</p>
      <${PositionPicker} value=${data.text_position} catalog=${catalog} layout=${data.layout} onChange=${set('text_position')} />
      <${FontPicker} value=${data.font} catalog=${catalog} onChange=${set('font')} />
      <${ColorField} label="Text color" value=${data.text_color} onChange=${set('text_color')} error=${form.errors.text_color} />
      <${ColorField} label="Outline color" value=${data.outline_color} onChange=${set('outline_color')} error=${form.errors.outline_color} />
      <${WholeNumber} label="Outline width" value=${data.outline_width} low=${0} high=${32} onChange=${set('outline_width')} error=${form.errors.outline_width} help="0 draws the text with no outline." />
    <//>
    <${Card} icon="user" title="Avatar" footer=${html`<${SaveButton} form=${form} />`}>
      <${Switch} label="Show the member's avatar" ...${form.bind('avatar')} />
      <div class="field"><label>Shape</label>
        <div class="row">${catalog.shapes.map((shape) => html`<button type="button" key=${shape} class=${'btn small' + (data.avatar_shape === shape ? ' primary' : '')} aria-pressed=${data.avatar_shape === shape}
          disabled=${!data.avatar} onClick=${() => form.set('avatar_shape', shape)}>${SHAPE_NAMES[shape]}</button>`)}</div></div>
      <${ColorField} label="Ring color" value=${data.ring_color} onChange=${set('ring_color')} error=${form.errors.ring_color} />
      <${WholeNumber} label="Ring width" value=${data.ring_width} low=${0} high=${catalog.limits.stroke} onChange=${set('ring_width')} error=${form.errors.ring_width} help="0 draws the avatar with no ring." />
    <//>
    <${Card} icon="image" title="Frame" footer=${html`<${SaveButton} form=${form} />`}>
      <${Switch} label="Put a frame around the picture" help="The frame is added outside the layout, so the picture grows by its width on every side." ...${form.bind('frame')} />
      <${ColorField} label="Frame color" value=${data.frame_color} onChange=${set('frame_color')} error=${form.errors.frame_color} />
      <${WholeNumber} label="Frame width" value=${data.frame_width} low=${1} high=${catalog.limits.stroke} disabled=${!data.frame} onChange=${set('frame_width')} error=${form.errors.frame_width} />
    <//>
    <${Card} icon="wand-sparkles" title="Go further">
      <p class="muted">The designer opens this layout as layers you can move, size, turn and add to: more text, shapes, pictures and a second avatar.</p>
      <button type="button" class="btn outline" onClick=${onCustomize}><${Icon} name="layers-3" /> Customize in the designer</button>
    <//>
  </div>`;
}

function Switches({ form }) {
  const guild = useGuild();
  return html`<${Card} icon="send" title="When it goes out" footer=${html`<${SaveButton} form=${form} />`}>
    <p class="muted">The picture rides along with the welcome message, which is written on the <a href=${`/dashboard/${guild.id}/greetings`} data-nav="1">Greetings page</a>.</p>
    <${Switch} label="Send the picture with each welcome message" ...${form.bind('enabled')} />
    <${Switch} label="Show it inside the message's embed" help="Off sends it as a file beside the message." ...${form.bind('in_embed')} />
    <${Switch} label="Send it with the join DM too" ...${form.bind('dm_image')} />
    <${Switch} label="Keep an animated background moving" help="With an animated GIF as the background, the picture goes out as a GIF. Off draws its first frame." ...${form.bind('animate')} />
  <//>`;
}

function Loaded({ form, catalog }) {
  const data = form.data;
  const uploads = useUploads();
  const [problems, setProblems] = useState([]);
  useEffect(() => { api.get('/welcomecard/problems').then(setProblems).catch(() => {}); }, []);
  const designing = data.mode === 'design';
  const draft = designing ? { mode: 'design', design: data.design, animate: data.animate } : { ...draftOfLayout(data), mode: 'layout', animate: data.animate };
  const preview = usePreview(draft);
  const customize = async () => {
    const sure = await confirmDialog({ title: 'Open this layout in the designer?', body: 'The designer starts from this layout as it looks now, in place of the design it held. Nothing is saved until you confirm the changes.', confirm: 'Open the designer' });
    if (!sure) return;
    try {
      const answer = await api.post('/welcomecard/design', { draft: draftOfLayout(data) });
      form.set('design', answer.design);
      form.set('mode', 'design');
      window.scrollTo(0, 0);
    } catch (error) {
      toastError(error);
    }
  };
  const shown = preview.shown;
  return html`
    ${problems.map((text) => html`<${Notice} key=${text} kind="warn">${text}<//>`)}
    <${Tabs} tabs=${[{ key: 'layout', label: 'Ready-made layouts' }, { key: 'design', label: 'Designer' }]} active=${data.mode} onChange=${(mode) => form.set('mode', mode)} />
    ${form.errors.mode && html`<div class="error">${form.errors.mode}</div>`}
    <p class="muted small wc-which">Welcome pictures are drawn from the tab that is open when you save: ${designing ? 'the design below' : 'the layout and its settings below'}.</p>
    ${designing ? html`
      <${Designer} form=${form} catalog=${catalog} uploads=${uploads} preview=${preview} />
      <${Switches} form=${form} />`
    : html`<div class="wc-ready">
      <div class="wc-ready-left">
        <${Card} icon="eye" title="Preview" right=${preview.busy && html`<span class="muted small">Drawing...</span>`}>
          ${shown ? html`<img class="wc-preview" src=${shown.image} alt="The welcome picture as it is drawn for you" style=${{ aspectRatio: `${shown.width} / ${shown.height}` }} />` : html`<${Loading} text="Drawing the picture..." />`}
          <${PreviewNotes} preview=${preview} />
        <//>
      </div>
      <${ReadyMade} form=${form} catalog=${catalog} uploads=${uploads} onCustomize=${customize} />
    </div>`}
    <${SaveBar} form=${form} />`;
}

export default function WelcomeCardPage() {
  const guild = useGuild();
  const form = useSettings('welcomecard');
  const [catalog, setCatalog] = useState(null);
  const [failed, setFailed] = useState(null);
  useEffect(() => { api.get('/welcomecard/catalog').then(setCatalog).catch(setFailed); }, [guild.id]);
  if (failed || form.failed) return html`<${Notice} kind="bad">${(failed || form.failed).message}<//>`;
  if (!catalog || form.loading) return html`<${Loading} />`;
  return html`<${Loaded} form=${form} catalog=${catalog} />`;
}
