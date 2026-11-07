// Renders Discord's markdown as elements for the message preview. It builds nodes, never HTML text, so a message can't inject anything.
import { html } from './lib.js';
import { knownMember, useMembers } from './members.js';

const INLINE = [
  ['code', /^`([^`\n]+)`/],
  ['bold3', /^\*\*\*([\s\S]+?)\*\*\*/],
  ['bold', /^\*\*([\s\S]+?)\*\*/],
  ['under', /^__([\s\S]+?)__/],
  ['strike', /^~~([\s\S]+?)~~/],
  ['spoiler', /^\|\|([\s\S]+?)\|\|/],
  ['italic', /^\*([^*\n]+?)\*/],
  ['italic', /^_([^_\n]+?)_(?![a-zA-Z0-9])/],
  ['link', /^\[([^\]\n]+)\]\((https?:\/\/(?:[^\s()]|\([^\s()]*\))+)\)/],
  ['autolink', /^<(https?:\/\/[^\s>]+)>/],
  ['url', /^https?:\/\/[^\s<]+[^\s<.,:;"')\]]/],
  ['emoji', /^<(a?):([a-zA-Z0-9_~]+):(\d+)>/],
  ['user', /^<@!?(\d+)>/],
  ['role', /^<@&(\d+)>/],
  ['channel', /^<#(\d+)>/],
  ['command', /^<\/([^:>]+):(\d+)>/],
  ['time', /^<t:(-?\d+)(?::([tTdDfFR]))?>/],
  ['everyone', /^@(everyone|here)\b/],
];
const NEXT_SPECIAL = /[`*_~|[<@h\\]/;

function formatTime(seconds, style) {
  const date = new Date(Number(seconds) * 1000);
  if (style === 'R') {
    const diff = Math.round((date.getTime() - Date.now()) / 1000);
    const abs = Math.abs(diff);
    const [amount, unit] = abs < 60 ? [abs, 'second'] : abs < 3600 ? [Math.round(abs / 60), 'minute'] : abs < 86400 ? [Math.round(abs / 3600), 'hour'] : [Math.round(abs / 86400), 'day'];
    const label = `${amount} ${unit}${amount === 1 ? '' : 's'}`;
    return diff >= 0 ? `in ${label}` : `${label} ago`;
  }
  const options = {
    t: { hour: 'numeric', minute: '2-digit' },
    T: { hour: 'numeric', minute: '2-digit', second: '2-digit' },
    d: { year: 'numeric', month: '2-digit', day: '2-digit' },
    D: { year: 'numeric', month: 'long', day: 'numeric' },
    F: { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit' },
  }[style || 'f'] || { year: 'numeric', month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit' };
  return date.toLocaleString(undefined, options);
}

export function inline(text, guild) {
  const out = [];
  let rest = text;
  let plain = '';
  const flush = () => { if (plain) { out.push(plain); plain = ''; } };
  while (rest.length) {
    if (rest[0] === '\\' && rest.length > 1 && /[\\*_~`|>#<@[\]()-]/.test(rest[1])) {
      plain += rest[1];
      rest = rest.slice(2);
      continue;
    }
    let matched = false;
    for (const [kind, pattern] of INLINE) {
      const m = pattern.exec(rest);
      if (!m) continue;
      flush();
      out.push(token(kind, m, guild));
      rest = rest.slice(m[0].length);
      matched = true;
      break;
    }
    if (matched) continue;
    const skip = rest.slice(1).search(NEXT_SPECIAL);
    const take = skip < 0 ? rest.length : skip + 1;
    plain += rest.slice(0, take);
    rest = rest.slice(take);
  }
  flush();
  return out;
}

function token(kind, m, guild) {
  switch (kind) {
    case 'code': return html`<code class="dc-inline-code">${m[1]}</code>`;
    case 'bold3': return html`<b><i>${inline(m[1], guild)}</i></b>`;
    case 'bold': return html`<b>${inline(m[1], guild)}</b>`;
    case 'under': return html`<u>${inline(m[1], guild)}</u>`;
    case 'strike': return html`<s>${inline(m[1], guild)}</s>`;
    case 'spoiler': return html`<span class="dc-spoiler">${inline(m[1], guild)}</span>`;
    case 'italic': return html`<i>${inline(m[1], guild)}</i>`;
    case 'link': return html`<a href=${m[2]} target="_blank" rel="noopener noreferrer">${inline(m[1], guild)}</a>`;
    case 'autolink': return html`<a href=${m[1]} target="_blank" rel="noopener noreferrer">${m[1]}</a>`;
    case 'url': return html`<a href=${m[0]} target="_blank" rel="noopener noreferrer">${m[0]}</a>`;
    case 'emoji': return html`<img class="dc-emoji" alt=${m[2]} title=${m[2]} src=${`https://cdn.discordapp.com/emojis/${m[3]}.${m[1] ? 'gif' : 'png'}`} />`;
    case 'user': return html`<span class="dc-mention">@${userName(guild, m[1])}</span>`;
    case 'role': return html`<span class="dc-mention">@${roleName(guild, m[1])}</span>`;
    case 'channel': return html`<span class="dc-mention">#${channelName(guild, m[1])}</span>`;
    case 'command': return html`<span class="dc-mention">/${m[1]}</span>`;
    case 'time': return html`<span class="dc-inline-code">${formatTime(m[1], m[2])}</span>`;
    case 'everyone': return html`<span class="dc-mention">@${m[1]}</span>`;
    default: return m[0];
  }
}

function userName(guild, id) {
  if (guild && guild.me && String(guild.me.id) === id) return guild.me.name;
  const row = guild ? knownMember(guild.id, id) : undefined;
  if (row) return row.display_name || row.name;
  return row === null ? 'unknown-user' : 'user';
}
function roleName(guild, id) {
  const role = guild && guild.roles.find((r) => String(r.id) === id);
  return role ? role.name : 'unknown-role';
}
function channelName(guild, id) {
  const channel = guild && guild.channels.find((c) => String(c.id) === id);
  return channel ? channel.name : 'unknown-channel';
}

const MENTIONED = /<@!?(\d+)>/g;

export function Markdown({ text, guild }) {
  // Mentioned people are named where the dashboard can learn them, in one batched lookup that every preview shares.
  useMembers(guild && guild.id, text ? [...text.matchAll(MENTIONED)].map((m) => m[1]) : []);
  if (!text) return null;
  const blocks = [];
  const fence = /```(?:[a-zA-Z0-9+-]*\n)?([\s\S]*?)```/g;
  let last = 0;
  let match;
  while ((match = fence.exec(text))) {
    if (match.index > last) blocks.push(...lines(text.slice(last, match.index), guild));
    blocks.push(html`<code class="dc-code">${match[1]}</code>`);
    last = match.index + match[0].length;
  }
  if (last < text.length) blocks.push(...lines(text.slice(last), guild));
  return blocks;
}

function lines(text, guild) {
  const out = [];
  const rows = text.split('\n');
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    const newline = index < rows.length - 1 ? '\n' : '';
    let m;
    // ">>> " quotes everything after it, through the last line of the message.
    if ((m = /^>>> ?(.*)$/.exec(row))) {
      out.push(html`<div class="dc-quote">${inline([m[1], ...rows.slice(index + 1)].join('\n'), guild)}</div>`);
      break;
    }
    if ((m = /^(#{1,3}) (.+)$/.exec(row))) out.push(html`<div class=${'dc-h' + m[1].length}>${inline(m[2], guild)}</div>`);
    else if ((m = /^-# (.+)$/.exec(row))) out.push(html`<div class="dc-small">${inline(m[1], guild)}</div>`);
    else if ((m = /^> ?(.*)$/.exec(row))) out.push(html`<div class="dc-quote">${inline(m[1], guild)}</div>`);
    else if ((m = /^\s*[-*] (.+)$/.exec(row))) out.push(html`<div style=${{ paddingLeft: '16px' }}>• ${inline(m[1], guild)}</div>`);
    else out.push(...inline(row, guild), newline);
  }
  return out;
}
