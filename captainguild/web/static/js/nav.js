// The sidebar: Settings first, then the extras that most servers set up next, then the rest.
// `staff: true` pages also open for people with staff access, and the rest need Manage Server. `viewer: true` pages also open for someone a
// server lets view its statistics without staff access, and they see only those.
export const NAV = [
  { title: 'Settings', items: [
    { key: 'setup', name: 'Quick setup', icon: 'wand-sparkles' },
    { key: 'botsettings', name: 'Bot settings', icon: 'settings' },
    { key: 'commands', name: 'Commands', icon: 'wrench' },
    { key: 'activity', name: 'Activity (weblogs)', icon: 'history' },
    { key: 'backup', name: 'Backup & import', icon: 'database' },
  ] },
  { title: 'Extras', items: [
    { key: 'personalize', name: 'Personalize', icon: 'palette' },
    { key: 'levels', name: 'Levels', icon: 'trending-up' },
    { key: 'voicelink', name: 'Voice link', icon: 'volume-2' },
    { key: 'sticky', name: 'Sticky messages', icon: 'pin' },
    { key: 'autopurge', name: 'Auto purge', icon: 'trash-2' },
  ] },
  { title: 'Moderation', items: [
    { key: 'automod', name: 'Automod', icon: 'shield' },
    { key: 'moderation', name: 'Moderation', icon: 'gavel' },
    { key: 'cases', name: 'Cases', icon: 'file-text', staff: true },
    { key: 'reports', name: 'Reports', icon: 'message-square-warning', staff: true },
    { key: 'voice', name: 'Voice controls', icon: 'mic' },
    { key: 'logging', name: 'Logging', icon: 'scroll-text' },
    { key: 'logsearch', name: 'Log search', icon: 'search', staff: true },
  ] },
  { title: 'Roles', items: [
    { key: 'reactionroles', name: 'Reaction roles', icon: 'smile' },
    { key: 'autoroles', name: 'Autoroles', icon: 'user-plus' },
    { key: 'rolerules', name: 'Role rules', icon: 'git-merge' },
  ] },
  { title: 'Custom commands', items: [
    { key: 'tags', name: 'Tags', icon: 'tag' },
    { key: 'triggers', name: 'Triggers', icon: 'message-square' },
    { key: 'autofeeds', name: 'Autofeeds', icon: 'rss' },
  ] },
  { title: 'Notifications', items: [
    { key: 'gamealerts', name: 'Game alerts', icon: 'gamepad-2' },
    { key: 'twitch', name: 'Twitch', icon: 'twitch' },
    { key: 'youtube', name: 'YouTube', icon: 'youtube' },
    { key: 'rss', name: 'RSS feeds', icon: 'rss' },
  ] },
  { title: 'Statistics', items: [
    { key: 'statistics', name: 'Statistics', icon: 'chart-column', staff: true, viewer: true },
    { key: 'statssettings', name: 'Statistics settings', icon: 'settings', linked: true },
    { key: 'counters', name: 'Counter channels', icon: 'hash', linked: true },
    { key: 'statroles', name: 'Stat roles', icon: 'crown', linked: true },
    { key: 'statsdata', name: 'Statistics data', icon: 'database' },
    { key: 'statsinsights', name: 'Statistics insights', icon: 'gauge', staff: true },
  ] },
  { title: 'Utility', items: [
    { key: 'greetings', name: 'Greetings', icon: 'door-open' },
    { key: 'welcomecard', name: 'Welcome image', icon: 'layers-3' },
    { key: 'starboard', name: 'Starboard', icon: 'star' },
    { key: 'embeds', name: 'Embeds', icon: 'image' },
    { key: 'collaborative', name: 'Collaborative messages', icon: 'users' },
    { key: 'mover', name: 'Message mover', icon: 'corner-down-right' },
    { key: 'suggestions', name: 'Suggestions', icon: 'lightbulb' },
    { key: 'proposals', name: 'Proposals', icon: 'thumbs-up', staff: true },
    { key: 'translation', name: 'Translate & convert', icon: 'repeat' },
    { key: 'bookmarks', name: 'Bookmarks', icon: 'inbox' },
    { key: 'entrygiveaways', name: 'Entry giveaways', icon: 'gift' },
    { key: 'channelarchive', name: 'Channel archive', icon: 'copy' },
    { key: 'answers', name: 'Wiki answers', icon: 'book-open' },
    { key: 'forummessages', name: 'Forum messages', icon: 'message-circle' },
  ] },
];

export const PAGE_INDEX = Object.fromEntries(NAV.flatMap((group) => group.items.map((item) => [item.key, item])));

// The page to open on another server: the one on screen when that server lets this person use it, and the first page they can use otherwise.
export function pageOn(access, page) {
  const entry = PAGE_INDEX[page];
  if (access === 'full') return entry ? page : 'botsettings';
  if (access === 'viewer') return entry && entry.viewer ? page : 'statistics';
  return entry && entry.staff ? page : 'cases';
}

// The page a server opens on when the address names none.
export const firstPage = (access) => (access === 'full' ? 'botsettings' : access === 'viewer' ? 'statistics' : 'cases');

// Whether a page opens for someone at this level. Someone who only views statistics gets the pages listed for them, and a `linked` page, which
// the statistics page links to for whoever its own checks let change a part of it.
export function opens(access, page) {
  if (access !== 'viewer') return true;
  const entry = PAGE_INDEX[page];
  return !!entry && (!!entry.viewer || !!entry.linked);
}

// Whether the sidebar lists a page for someone at this level.
export const listed = (access, item) => access === 'full' || (access === 'viewer' ? !!item.viewer : !!item.staff);

// The pages a filter box leaves in the sidebar: those whose name holds what was typed.
export function matchPages(items, text) {
  const needle = text.trim().toLowerCase();
  return needle ? items.filter((item) => item.name.toLowerCase().includes(needle)) : items;
}
