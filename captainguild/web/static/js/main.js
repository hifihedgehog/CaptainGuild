// The dashboard shell: top bar, sidebar, server switcher and page loading.
import { html, render, useEffect, useRef, useState } from './lib.js';
import { raw, api, setCsrf, setGuild, onSessionEnd, signInPath } from './api.js';
import { Boundary, DialogHost, GuildContext, Icon, Loading, Notice, Toasts, confirmDialog, hasUnsaved, toast } from './ui.js';
import { NAV, PAGE_INDEX, matchPages, pageOn } from './nav.js';

const currentPath = () => location.pathname;
const fullPath = () => location.pathname + location.search;

// The address the page on screen belongs to. A Back press with unsaved work puts it back while the question is asked.
let shownAddress = fullPath();

function go(path) {
  if (path !== fullPath()) history.pushState(null, '', path);
  shownAddress = fullPath();
  window.dispatchEvent(new Event('route'));
  window.scrollTo(0, 0);
}

let asking = false;
// Leaves the page, after a question when it holds changes that aren't saved.
export async function navigate(path) {
  if (hasUnsaved() && path !== fullPath()) {
    if (asking) return;
    asking = true;
    const leave = await confirmDialog({ title: 'Leave this page?', body: 'You have changes that are not saved. They are lost if you leave.', confirm: 'Leave', danger: true });
    asking = false;
    if (!leave) return;
  }
  go(path);
}

function useRoute() {
  const [path, setPath] = useState(currentPath());
  useEffect(() => {
    const change = () => setPath(currentPath());
    const back = () => {
      if (hasUnsaved() && fullPath() !== shownAddress) {
        const target = fullPath();
        history.pushState(null, '', shownAddress);
        navigate(target);
        return;
      }
      shownAddress = fullPath();
      change();
    };
    window.addEventListener('popstate', back);
    window.addEventListener('route', change);
    return () => { window.removeEventListener('popstate', back); window.removeEventListener('route', change); };
  }, []);
  return path;
}

// A tab with unsaved work asks the browser to confirm before it closes or reloads.
window.addEventListener('beforeunload', (event) => {
  if (!hasUnsaved()) return;
  event.preventDefault();
  event.returnValue = '';
});

// Inside the dashboard, plain links change the page without a reload.
function followLink(event) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
  const link = event.target.closest && event.target.closest('a[data-nav]');
  if (!link) return;
  event.preventDefault();
  navigate(link.getAttribute('href'));
}

// Unposted announcement text is kept in this browser, and signing out takes it away so the next person on a shared computer doesn't find it.
function forgetDrafts() {
  try {
    for (const key of Object.keys(localStorage)) if (key.startsWith('cg-embed-')) localStorage.removeItem(key);
  } catch (error) { /* a private window can refuse storage */ }
}

function GuildIcon({ guild, size = 32 }) {
  const style = { width: size + 'px', height: size + 'px' };
  if (guild.icon) return html`<img class="avatar" style=${style} src=${guild.icon} alt="" />`;
  const letters = guild.name.split(/\s+/).map((w) => w[0]).slice(0, 3).join('');
  return html`<span class="avatar avatar-letter" style=${style}>${letters}</span>`;
}

function Dropdown({ button, children, label }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return undefined;
    const close = () => setOpen(false);
    const onKey = (event) => { if (event.key === 'Escape') setOpen(false); };
    const timer = setTimeout(() => document.addEventListener('click', close, { once: true }), 0);
    document.addEventListener('keydown', onKey);
    return () => { clearTimeout(timer); document.removeEventListener('click', close); document.removeEventListener('keydown', onKey); };
  }, [open]);
  return html`<div class="menu">
    <button class="top-btn" aria-label=${label} aria-expanded=${open} onClick=${() => setOpen(!open)}>${button}<${Icon} name="chevron-down" size=${14} /></button>
    ${open && html`<div class="menu-list">${children}</div>`}
  </div>`;
}

function TopBar({ me, guild, page, onToggleSide }) {
  return html`<header class="top">
    <a class="brand" href="/servers" data-nav="1"><img src="/static/img/logo.svg" alt="" />${me.brand.name}</a>
    <button class="top-btn" aria-label="Show or hide the sidebar" onClick=${onToggleSide}><${Icon} name="menu" size=${20} /></button>
    <a class="top-btn top-link" href="/docs"><${Icon} name="book-open" /> Docs</a>
    <a class="top-btn top-link" href="/status"><${Icon} name="activity" /> Status</a>
    ${me.invite && html`<a class="top-btn top-link" href=${me.invite} target="_blank" rel="noopener noreferrer"><${Icon} name="plus" /> Invite</a>`}
    ${me.brand.repo && html`<a class="top-btn top-link" href=${me.brand.repo} target="_blank" rel="noopener noreferrer"><${Icon} name="code" /> Source</a>`}
    <div class="top-spacer"></div>
    ${me.guilds.length > 0 && html`<${Dropdown} label="Switch server" button=${guild ? html`<${GuildIcon} guild=${guild} />` : html`<${Icon} name="server" size=${20} />`}>
      ${me.guilds.map((g) => html`<a key=${g.id} class="menu-item" href=${`/dashboard/${g.id}/${pageOn(g.access, page)}`} data-nav="1"><${GuildIcon} guild=${g} size=${24} />${g.name}</a>`)}
    <//>`}
    <${Dropdown} label="Account" button=${html`<img class="avatar" src=${me.user.avatar || '/static/img/logo.svg'} alt="" />`}>
      <div class="menu-item" style=${{ cursor: 'default' }}><b>${me.user.name}</b></div>
      <a class="menu-item" href="/servers" data-nav="1"><${Icon} name="server" /> Servers</a>
      <a class="menu-item" href="/status"><${Icon} name="activity" /> Status</a>
      <form method="post" action="/logout" onSubmit=${forgetDrafts}><button class="menu-item" type="submit"><${Icon} name="log-out" /> Log out</button></form>
    <//>
  </header>`;
}

function SideBar({ guild, page }) {
  const key = 'cg-side-closed';
  const load = () => { try { return JSON.parse(localStorage.getItem(key) || '{}'); } catch (e) { return {}; } };
  const [closed, setClosed] = useState(load);
  const [find, setFind] = useState('');
  const flip = (title) => {
    const next = { ...closed, [title]: !closed[title] };
    setClosed(next);
    try { localStorage.setItem(key, JSON.stringify(next)); } catch (e) { /* a private window can refuse storage */ }
  };
  const allowed = (item) => guild.access === 'full' || item.staff;
  const searching = find.trim() !== '';
  const groups = NAV.map((group) => ({ group, items: matchPages(group.items.filter(allowed), find) })).filter((entry) => entry.items.length);
  return html`<nav class="side" aria-label="Dashboard pages">
    <div class="side-find"><input type="search" placeholder="Find a page" aria-label="Find a page" value=${find} onInput=${(e) => setFind(e.currentTarget.value)} /></div>
    ${groups.length === 0 && html`<div class="ms-empty">No page matches.</div>`}
    ${groups.map(({ group, items }) => {
      const isClosed = !searching && closed[group.title] && !items.some((i) => i.key === page);
      return html`<div class="side-group" key=${group.title}>
        <button class="side-title" aria-expanded=${!isClosed} onClick=${() => flip(group.title)}><${Icon} name=${isClosed ? 'chevron-right' : 'chevron-down'} size=${12} />${group.title}</button>
        ${!isClosed && items.map((item) => html`<a key=${item.key} class=${'side-link' + (item.key === page ? ' on' : '')} href=${`/dashboard/${guild.id}/${item.key}`} data-nav="1"><${Icon} name=${item.icon} />${item.name}</a>`)}
      </div>`;
    })}
  </nav>`;
}

function Page({ page, guild }) {
  const [view, setView] = useState({ Component: null, error: null, page: null });
  useEffect(() => {
    let live = true;
    setView({ Component: null, error: null, page });
    if (!PAGE_INDEX[page]) {
      setView({ Component: null, error: 'That page does not exist.', page });
      return undefined;
    }
    import(`./pages/${page}.js`)
      .then((module) => { if (live) setView({ Component: module.default, error: null, page }); })
      .catch((error) => { console.error(error); if (live) setView({ Component: null, error: 'This page could not load.', page }); });
    return () => { live = false; };
  }, [page]);
  if (view.error) return html`<${Notice} kind="bad">${view.error}<//>`;
  if (!view.Component || view.page !== page) return html`<${Loading} />`;
  const Component = view.Component;
  return html`<${Component} key=${guild.id} />`;
}

function GuildView({ me, guildId, page, toggleSide }) {
  const [guild, setGuildInfo] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    // A slow answer for the server left behind must not fill the page of the one switched to.
    let live = true;
    setGuild(guildId);
    setGuildInfo(null);
    setError(null);
    api.get('/info').then((info) => { if (live) setGuildInfo(info); }).catch((e) => { if (live) setError(e); });
    return () => { live = false; };
  }, [guildId]);
  const reload = () => api.get('/info').then(setGuildInfo).catch(setError);
  const shown = page || (guild && guild.access === 'full' ? 'botsettings' : 'cases');
  if (error) {
    return html`<${TopBar} me=${me} page=${shown} onToggleSide=${toggleSide} /><div class="main" style=${{ marginLeft: 0 }}><${Notice} kind="bad">${error.message}<//></div>`;
  }
  if (!guild) return html`<${TopBar} me=${me} page=${shown} onToggleSide=${toggleSide} /><${Loading} />`;
  document.title = `${PAGE_INDEX[shown] ? PAGE_INDEX[shown].name + ' - ' : ''}${guild.name} - ${me.brand.name}`;
  return html`<${GuildContext.Provider} value=${{ ...guild, reload }}>
    <${TopBar} me=${me} guild=${guild} page=${shown} onToggleSide=${toggleSide} />
    <${SideBar} guild=${guild} page=${shown} />
    <main class="main">
      ${!me.message_text && guild.access === 'full' && html`<${Notice}>Discord hasn't given this bot Message Content yet, so it can't read what members type. Automod's word and link rules and media-only channels, text commands, triggers and the text in message logs wait for it.<//>`}
      <${Boundary} key=${guild.id + '/' + shown}><${Page} page=${shown} guild=${guild} /><//>
    </main>
  <//>`;
}

function ServerPicker({ me, toggleSide }) {
  document.title = `Servers - ${me.brand.name}`;
  // A share link doesn't know which of your servers the tag is going to, so it comes through here.
  const share = new URLSearchParams(location.search).get('share');
  const tile = (g) => (share ? `/dashboard/${g.id}/tags?share=${encodeURIComponent(share)}` : `/dashboard/${g.id}`);
  return html`<${TopBar} me=${me} onToggleSide=${toggleSide} />
    <main class="main" style=${{ marginLeft: 0 }}>
      <div class="public">
        <h2>${share ? 'Pick a server to import this tag into' : 'Pick a server'}</h2>
        ${me.guilds.length === 0 && html`<${Notice}>None of the servers this bot is in list you as staff. You need Manage Server, the bot manager role or a dashboard staff role.${me.invite && html` To use the bot in a server of your own, <a href=${me.invite} target="_blank" rel="noopener noreferrer">add it there</a> first.`}<//>`}
        <div class="server-grid">${me.guilds.map((g) => html`<a key=${g.id} class="server-tile" href=${tile(g)} data-nav="1"><${GuildIcon} guild=${g} size=${44} /><span>${g.name}</span></a>`)}</div>
        ${me.invite && me.guilds.length > 0 && html`<p class="muted" style=${{ marginTop: '18px' }}>Not listed? <a href=${me.invite} target="_blank" rel="noopener noreferrer">Add the bot to another server</a>.</p>`}
      </div>
    </main>`;
}

function SessionNotice({ onContinue }) {
  return html`<div class="session-banner" role="alert">
    <span>Your sign-in ended. <a href=${signInPath()} target="_blank" rel="noopener noreferrer">Sign in again</a> in a new tab, then press Continue. What you typed on this page is still here.</span>
    <button type="button" class="btn small primary" onClick=${onContinue}>Continue</button>
  </div>`;
}

function App() {
  const path = useRoute();
  const [me, setMe] = useState(null);
  const [ended, setEnded] = useState(false);
  const [sideHidden, setSideHidden] = useState(window.innerWidth < 800);
  const first = useRef(true);
  useEffect(() => {
    raw.get('/api/me').then((data) => {
      if (!data.user) {
        location.href = signInPath();
        return;
      }
      setCsrf(data.csrf);
      setMe(data);
      onSessionEnd(() => setEnded(true));
    }).catch(() => setMe({ failed: true }));
  }, []);
  useEffect(() => { document.body.classList.toggle('side-hidden', sideHidden); }, [sideHidden]);
  // On a phone the sidebar covers the page, so choosing a page puts it away.
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    if (window.innerWidth < 800) setSideHidden(true);
  }, [path]);
  const resume = async () => {
    try {
      const data = await raw.get('/api/me');
      if (!data.user) { toast('You are not signed in yet.', 'bad'); return; }
      setCsrf(data.csrf);
      setEnded(false);
      toast('Signed in again. You can save now.');
    } catch (error) {
      toast(error.message, 'bad');
    }
  };
  if (!me) return html`<${Loading} />`;
  if (me.failed) return html`<${Notice} kind="bad">Could not reach the server. Reload the page to try again.<//>`;
  const toggleSide = () => setSideHidden(!sideHidden);
  const match = /^\/dashboard\/(\d+)(?:\/([a-z0-9-]+))?/.exec(path);
  return html`<div onClick=${followLink}>
    ${ended && html`<${SessionNotice} onContinue=${resume} />`}
    ${match ? html`<${GuildView} me=${me} guildId=${match[1]} page=${match[2]} toggleSide=${toggleSide} />` : html`<${ServerPicker} me=${me} toggleSide=${toggleSide} />`}
    <${Toasts} /><${DialogHost} />
  </div>`;
}

render(html`<${Boundary} what="The dashboard"><${App} /><//>`, document.getElementById('app'));
