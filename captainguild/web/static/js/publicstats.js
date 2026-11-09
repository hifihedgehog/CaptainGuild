// The public statistics page at /stats/{server}: the server's totals and charts for anyone with the link, signed in or not, with no
// member's name on anything. It reads only the public routes, which refuse unless the server chose this.
import { html, render, useEffect, useState } from './lib.js';
import { raw } from './api.js';
import { Boundary, DialogHost, GuildContext, Icon, Loading, Notice, Toasts } from './ui.js';
import { readFrom } from './stats/common.js';
import { applyTheme, savedTheme, setTheme } from './theme.js';

applyTheme();
import { StatsApp } from './stats/app.js';

const found = /^\/stats\/(\d+)/.exec(location.pathname);
const guildId = found ? found[1] : null;

function PublicStats() {
  const [ctx, setCtx] = useState(null);
  const [theme, setShown] = useState(savedTheme);
  const [failed, setFailed] = useState(null);
  useEffect(() => {
    if (!guildId) { setFailed({ message: 'This address names no server.' }); return; }
    readFrom({ public: true, guild: guildId });
    raw.get(`/api/public/g/${guildId}/statistics/context`).then(setCtx).catch(setFailed);
  }, []);
  const name = ctx && ctx.guild ? ctx.guild.name : 'Statistics';
  document.title = ctx ? `${name} statistics` : 'Statistics';
  return html`<div>
    <header class="top public-top">
      <span class="brand">${ctx && ctx.guild && ctx.guild.icon ? html`<img class="avatar" src=${ctx.guild.icon} alt="" />` : html`<img src="/static/img/logo.svg" alt="" />`}${name}</span>
      <div class="top-spacer"></div>
      <button type="button" class="top-btn" aria-label=${theme === 'light' ? 'Use the dark theme' : 'Use the light theme'} onClick=${() => setShown(setTheme(theme === 'light' ? 'dark' : 'light'))}>
        <${Icon} name=${theme === 'light' ? 'moon' : 'sun'} size=${18} /></button>
      <a class="top-btn" href=${`/login?next=${encodeURIComponent(`/dashboard/${guildId}/statistics`)}`}>Members of this server can sign in to see more</a>
    </header>
    <main class="main public-main">
      ${failed && html`<${Notice} kind="bad">${failed.message}<//>`}
      ${!failed && !ctx && html`<${Loading} />`}
      ${ctx && html`<${GuildContext.Provider} value=${{ id: guildId, name, icon: ctx.guild && ctx.guild.icon, channels: ctx.channels || [], roles: [], emojis: ctx.emojis || [], access: 'public' }}>
        <${StatsApp} preloaded=${ctx} />
      <//>`}
    </main>
    <${Toasts} /><${DialogHost} />
  </div>`;
}

render(html`<${Boundary} what="This page"><${PublicStats} /><//>`, document.getElementById('app'));
