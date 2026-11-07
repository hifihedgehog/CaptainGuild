// Wiki answers: AI answers from copies of the server's wikis and documentation sites, billed to the bot host's own AI account.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Col, DataTable, Grid, Icon, Loading, Notice, NumberInput, SaveBar, SaveButton, Select, SettingsGate, Switch, TextArea, TextInput, toast, toastError, useGuild, useSettings, when } from '../ui.js';

const COMMANDS = [
  { key: 'how_do_i', label: '/how_do_i' },
  { key: 'brainstorm', label: '/brainstorm' },
  { key: 'ask', label: '/ask' },
  { key: 'make_model', label: '/make_model' },
];
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'].map((value) => ({ value, label: value }));
const KINDS = [{ value: 'sitemap', label: 'Its sitemap' }, { value: 'crawl', label: 'Links from a start page' }];
const NAMES = { how_do_i: '/how_do_i', brainstorm: '/brainstorm', ask: '/ask', make_model: '/make_model', auto_answer: 'Automatic answers' };
const SUBJECT = [
  { key: 'community', label: 'Community', help: 'As it reads in "members of ...", such as "the Example community Discord server".' },
  { key: 'subject', label: 'What the sources cover', help: 'As it reads in "questions about ...", such as "Example printers, filament and accessories".' },
  { key: 'maker', label: 'Maker', help: 'As it reads in "... products" and "not ... support", such as "Example". Until it\'s set, the wording says "the maker\'s".' },
  { key: 'support', label: 'Support', help: 'Where orders, warranty and account questions go: a link or a few words.' },
  { key: 'models', label: 'Models to compare', help: 'Every current model, which the model looks up when someone asks which is best.' },
  { key: 'search_hints', label: 'Search hints', help: 'What to search for when a search misses: model codes, part names, error codes.' },
  { key: 'search_example', label: 'Example search' },
  { key: 'slicer', label: 'Slicer', help: 'As in "a file they can open in ...".' },
  { key: 'model_site', label: 'Model-sharing site', help: 'Where /make_model sends someone who wants a detailed figure.' },
];
const NEW_SOURCE = { name: '', kind: 'sitemap', address: '', prefix: '/', include: [], exclude: [], selector: '', refresh_hours: 24 };

const money = (value) => '$' + Number(value || 0).toFixed(2);
const lines = (list) => (list || []).join('\n');
const fromLines = (value) => value.split('\n').map((line) => line.trim()).filter(Boolean);

function Tile({ label, value }) {
  return html`<div class="stat-tile"><b>${value}</b><span class="muted small">${label}</span></div>`;
}

function Checks({ status }) {
  const screening = status.screening;
  return html`
    ${!status.key_set && html`<${Notice} kind="bad">The bot's host hasn't added an API key for AI answers. Until it's in the bot's .env file as ANTHROPIC_API_KEY, the commands say so and nothing is posted.<//>`}
    ${screening.mode === 'automod' && html`<${Notice}>
      Requests and answers are checked against this server's automod bad-word rule${screening.word_rule ? '' : ', which is switched off, so nothing is checked'}. An answer the rule would flag isn't posted, since it can't be censored.
      ${screening.lists_loaded && ' The word lists the bot\'s host set up have no file for this server.'}
      ${screening.load_failed && " The word lists the bot's host named at start didn't load, and the host's log says why."}<//>`}
    ${!status.message_text && html`<${Notice} kind="warn">Discord hasn't given this bot Message Content, so automatic answers wait for it. The slash commands work.<//>`}
    ${!status.models_ready && html`<${Notice}>/make_model says it's unavailable here, because the bot's host hasn't installed OpenSCAD, Xvfb and prlimit.<//>`}
    ${!status.sketches_ready && html`<${Notice}>Sketches are off, because the cairo library isn't on the bot's host. /brainstorm and /ask answer without one.<//>`}`;
}

function Spend({ spend, limit }) {
  return html`<${Card} icon="gauge" title="What answers cost">
    <div class="stat-tiles">
      <${Tile} label=${limit ? 'This month, of ' + money(limit) : 'This month, with no budget'} value=${money(spend.month.cost)} />
      <${Tile} label="Requests this month" value=${spend.month.requests} />
      <${Tile} label="Last month" value=${money(spend.last_month.cost)} />
    </div>
    <p class="muted small">Estimated from list prices, with the requests the model refused or that failed. Every server with Wiki answers on spends from the same account.</p>
    <${DataTable} rows=${spend.month.by_command} rowKey="command" pageSizes=${[10]} empty="Nothing asked this month." columns=${[
      { key: 'command', label: 'Command', text: (row) => NAMES[row.command] || row.command },
      { key: 'requests', label: 'Requests', width: '110px' },
      { key: 'cost', label: 'Cost', width: '110px', render: (row) => money(row.cost) },
    ]} />
  <//>`;
}

function CopyStatus({ copy, locked, onRefresh, refreshing }) {
  const summary = copy.summary;
  return html`<div>
    <div class="row small muted" style=${{ gap: '14px' }}>
      <span>${copy.pages} pages in the copy</span>
      <span>Last update: ${copy.refreshed_at ? when(copy.refreshed_at) : 'not yet'}</span>
      ${!locked && html`<button class="btn small" disabled=${copy.running || refreshing} onClick=${onRefresh}><${Icon} name="refresh-cw" size=${14} /> Update now</button>`}
    </div>
    ${copy.running && html`<${Notice}>An update is running. A first copy of a large site takes a while.<//>`}
    ${copy.last_error && html`<${Notice} kind="bad">The last update failed. ${copy.last_error}<//>`}
    ${summary && summary.sitemap_pages === 0 && html`<${Notice} kind="warn">The last update found no pages that fit this source's path and patterns.<//>`}
    ${summary && summary.left_out > 0 && html`<${Notice} kind="warn">The site has more pages than a copy keeps, so ${summary.left_out} were left out.<//>`}
    ${summary && summary.kept_on_short_sitemap && html`<${Notice} kind="warn">The last update found fewer than half the pages the copy holds, so none were removed.<//>`}
    ${copy.pages > 0 && !copy.loaded && html`<${Notice}>The copy is being loaded for searching.<//>`}
  </div>`;
}

function SourceForm({ row, index, copy, saved, locked, onChange, onRemove, onRefresh, refreshing }) {
  const set = (key, value) => onChange({ ...row, [key]: value });
  return html`<div class="card" style=${{ padding: '12px', marginBottom: '12px' }}>
    <div class="row" style=${{ alignItems: 'flex-start' }}>
      <div style=${{ flex: '2 1 220px' }}><${TextInput} label="Name" help='As answers name it, such as "the Example Wiki".' disabled=${locked} value=${row.name} onChange=${(value) => set('name', value)} /></div>
      <div style=${{ flex: '1 1 180px' }}><${Select} label="Find pages from" options=${KINDS} disabled=${locked} value=${row.kind} onChange=${(value) => set('kind', value)} /></div>
    </div>
    <${TextInput} label=${row.kind === 'crawl' ? 'Start page' : 'Sitemap address'} placeholder=${row.kind === 'crawl' ? 'https://docs.example.org/guide/' : 'https://wiki.example.org/sitemap.xml'}
      disabled=${locked} value=${row.address} onChange=${(value) => set('address', value)} />
    <div class="row" style=${{ alignItems: 'flex-start' }}>
      <div style=${{ flex: '1 1 160px' }}><${TextInput} label="Pages under" help="Only pages whose path starts with this, such as /en/." disabled=${locked} value=${row.prefix} onChange=${(value) => set('prefix', value)} /></div>
      <div style=${{ flex: '1 1 120px' }}><${NumberInput} label="Update every (hours)" min=${1} max=${720} disabled=${locked} value=${row.refresh_hours} onChange=${(value) => set('refresh_hours', value)} /></div>
    </div>
    <div class="row" style=${{ alignItems: 'flex-start' }}>
      <div style=${{ flex: '1 1 200px' }}><${TextArea} label="Only these addresses" rows=${2} counter=${false} help="One pattern a line, * for any run of characters. A pattern starting with / reads the path. Empty takes every page." disabled=${locked}
        value=${lines(row.include)} onChange=${(value) => set('include', fromLines(value))} /></div>
      <div style=${{ flex: '1 1 200px' }}><${TextArea} label="Leave out" rows=${2} counter=${false} help="Patterns the same way, such as /en/drafts/*." disabled=${locked}
        value=${lines(row.exclude)} onChange=${(value) => set('exclude', fromLines(value))} /></div>
    </div>
    <${TextInput} label="Article selector" placeholder="main .content" help="The CSS selector of the article on each page, for a site the bot doesn't recognize. Empty lets the bot find it." disabled=${locked}
      value=${row.selector} onChange=${(value) => set('selector', value)} />
    ${saved && copy ? html`<${CopyStatus} copy=${copy} locked=${locked} onRefresh=${() => onRefresh(index)} refreshing=${refreshing} />` : html`<p class="muted small">Save to start this source's copy.</p>`}
    ${!locked && html`<button class="btn small outline danger" style=${{ marginTop: '8px' }} onClick=${onRemove}><${Icon} name="trash-2" size=${14} /> Remove this source</button>`}
  </div>`;
}

function Sources({ form, status, onRefresh, refreshing }) {
  const locked = !status.is_admin;
  const rows = form.data.sources || [];
  const savedRows = form.saved.sources || [];
  const change = (index, row) => form.set('sources', rows.map((other, at) => (at === index ? row : other)));
  const remove = (index) => form.set('sources', rows.filter((_, at) => at !== index));
  return html`<${Card} icon="book-open" title="Sources" footer=${html`<${SaveButton} form=${form} />`}>
    <p class="muted">The wikis and documentation sites answers come from. The bot keeps its own copy of each and searches them together, and each answer names the ones it drew from.</p>
    ${form.errors.sources && html`<${Notice} kind="bad">${form.errors.sources}<//>`}
    ${rows.map((row, index) => html`<${SourceForm} key=${index} row=${row} index=${index} copy=${status.sources[index]} locked=${locked}
      saved=${JSON.stringify(savedRows[index]) === JSON.stringify(row)} onChange=${(next) => change(index, next)} onRemove=${() => remove(index)} onRefresh=${onRefresh} refreshing=${refreshing} />`)}
    ${rows.length === 0 && html`<p class="muted">No sources yet.</p>`}
    ${!locked && rows.length < 5 && html`<button class="btn" onClick=${() => form.set('sources', [...rows, { ...NEW_SOURCE }])}><${Icon} name="plus" /> Add a source</button>`}
  <//>`;
}

function AutoAnswers({ form, locked }) {
  return html`<${Card} icon="message-square" title="Automatic answers" footer=${html`<${SaveButton} form=${form} />`}>
    <p class="muted">A new post in these forums gets one answer under it, posted only when the answer cites a source. Posts under five words, posts with a tag only moderators can apply, and posts the model would have to guess about are left to the helpers.</p>
    <${ChannelPicker} label="Forums" multi types=${[15]} disabled=${locked} ...${form.bind('auto_answer_forums')} />
    <${NumberInput} label="Automatic answers a day" help="For the whole server, counted from midnight UTC." min=${1} max=${5000} disabled=${locked} ...${form.bind('auto_answer_daily_limit')} />
    <${TextArea} label="Line above each answer" rows=${2} maxLength=${1000} help="{sources} names the sources the answer drew from." disabled=${locked} ...${form.bind('auto_intro')} />
  <//>`;
}

function Limits({ form, locked }) {
  return html`<${Card} icon="timer" title="Limits" footer=${html`<${SaveButton} form=${form} />`}>
    <${NumberInput} label="Monthly budget (dollars)" help="Once this month's answers have cost this much, the commands and automatic answers stop until the 1st, UTC. 0 means no budget." min=${0} max=${100000} disabled=${locked} ...${form.bind('monthly_limit')} />
    <${NumberInput} label="Requests a day for each person" help="/how_do_i, /brainstorm, /ask and /make_model share it, and it starts again at midnight UTC. A request nothing was billed for doesn't count, and neither do the first three a day that the bot sends to /ask. Everyone can also ask three times a minute." min=${1} max=${1000} disabled=${locked} ...${form.bind('daily_limit')} />
  <//>`;
}

function Models({ form, locked, choices }) {
  return html`<${Card} icon="bot" title="Models" footer=${html`<${SaveButton} form=${form} />`}>
    <p class="muted">The model each command asks, and how much it thinks first. A larger model and more effort cost more. /ask doesn't offer Haiku 5.5, which can't run its web search.</p>
    ${COMMANDS.map((command) => html`<div class="row" key=${command.key} style=${{ alignItems: 'flex-start' }}>
      <div style=${{ flex: '2 1 220px' }}><${Select} label=${command.label} options=${(choices[command.key] || []).map((value) => ({ value, label: value }))} disabled=${locked} ...${form.bind('models.' + command.key)} /></div>
      <div style=${{ flex: '1 1 120px' }}><${Select} label="Effort" options=${EFFORTS} disabled=${locked} ...${form.bind('efforts.' + command.key)} /></div>
    </div>`)}
  <//>`;
}

function Subject({ form, locked }) {
  return html`<${Card} icon="sparkles" title="Subject" footer=${html`<${SaveButton} form=${form} />`}>
    <p class="muted">What the model is told the server and its sources are about. These fill the wording each command gives the model.</p>
    ${SUBJECT.map((field) => html`<${TextInput} key=${field.key} label=${field.label} help=${field.help} disabled=${locked} ...${form.bind('subject.' + field.key)} />`)}
  <//>`;
}

function Wording({ form, locked }) {
  const [open, setOpen] = useState(false);
  return html`<${Card} icon="file-text" title="Rules and footers" right=${html`<button class="btn small" onClick=${() => setOpen(!open)}>${open ? 'Hide' : 'Show'}</button>`}>
    <p class="muted">The server's rules go after every command's prompt, and a request that breaks them gets a fixed notice that doesn't repeat it. In the footers, {sources} names the sources an answer drew from.</p>
    ${open && html`<div>
      <${TextArea} label="Server rules" rows=${10} maxLength=${20000} help="{wiki} names the server's sources." disabled=${locked} ...${form.bind('rules')} />
      ${COMMANDS.map((command) => html`<${TextInput} key=${command.key} label=${command.label + ' footer'} maxLength=${500} disabled=${locked} ...${form.bind('footers.' + command.key)} />`)}
      <${SaveButton} form=${form} />
    </div>`}
  <//>`;
}

function Loaded() {
  const guild = useGuild();
  const form = useSettings('answers');
  const [status, setStatus] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const timer = useRef(null);
  const load = () => api.get('/answers/status').then(setStatus).catch(toastError);
  const savedKey = form.saved ? JSON.stringify([form.saved.enabled, form.saved.sources]) : '';
  useEffect(() => { load(); return () => clearTimeout(timer.current); }, [guild.id, savedKey]);
  // While an update runs, the page asks again every few seconds so its numbers follow along.
  useEffect(() => {
    clearTimeout(timer.current);
    if (status && status.sources.some((copy) => copy.running)) timer.current = setTimeout(load, 4000);
  }, [status]);
  const refresh = async (index) => {
    setRefreshing(true);
    try {
      await api.post('/answers/refresh', { source: index });
      toast('The update started.');
      await load();
    } catch (error) {
      toastError(error);
    } finally {
      setRefreshing(false);
    }
  };
  return html`<${SettingsGate} form=${form}>${() => html`<div>
    ${!status && html`<${Loading} />`}
    ${status && html`<div>
      ${!status.allowed && html`<${Notice} kind="warn">The bot's host hasn't allowed Wiki answers in this server. Each answer is billed to the host's own AI account, so the host names the servers that may use it.<//>`}
      ${!status.is_admin && html`<${Notice}>Only the server owner or an Administrator can change these settings, since each answer is billed to the bot host's AI account.<//>`}
      <${Grid}>
        <${Col}>
          <${Card} icon="sparkles" title="Wiki answers" footer=${html`<${SaveButton} form=${form} />`}>
            <${Switch} label="Wiki answers on" help="Adds /how_do_i, /brainstorm, /ask, /make_model, /refresh_wiki and /auto_answer to this server, and answers new posts in the forums below." disabled=${!status.is_admin || !status.allowed} ...${form.bind('enabled')} value=${status.allowed && form.data.enabled} />
            <${Checks} status=${status} />
          <//>
          <${Sources} form=${form} status=${status} onRefresh=${refresh} refreshing=${refreshing} />
          <${AutoAnswers} form=${form} locked=${!status.is_admin} />
        <//>
        <${Col}>
          <${Spend} spend=${status.spend} limit=${form.saved.monthly_limit} />
          <${Subject} form=${form} locked=${!status.is_admin} />
          <${Limits} form=${form} locked=${!status.is_admin} />
          <${Models} form=${form} locked=${!status.is_admin} choices=${status.model_choices} />
          <${Wording} form=${form} locked=${!status.is_admin} />
        <//>
      <//>
    </div>`}
    <${SaveBar} form=${form} />
  </div>`}<//>`;
}

export default function AnswersPage() {
  const guild = useGuild();
  return html`<${Loaded} key=${guild.id} />`;
}
