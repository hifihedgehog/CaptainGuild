// The sidebar: Settings first, then the extras that most servers set up next, then the rest.
// `staff: true` pages also open for people with staff access, and the rest need Manage Server.
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
  ] },
  { title: 'Utility', items: [
    { key: 'greetings', name: 'Greetings', icon: 'door-open' },
    { key: 'starboard', name: 'Starboard', icon: 'star' },
    { key: 'embeds', name: 'Embeds', icon: 'image' },
    { key: 'suggestions', name: 'Suggestions', icon: 'lightbulb' },
    { key: 'proposals', name: 'Proposals', icon: 'thumbs-up', staff: true },
  ] },
];

export const PAGE_INDEX = Object.fromEntries(NAV.flatMap((group) => group.items.map((item) => [item.key, item])));

// The page to open on another server: the one on screen when that server lets this person use it, and the first page they can use otherwise.
export function pageOn(access, page) {
  const entry = PAGE_INDEX[page];
  if (access === 'full') return entry ? page : 'botsettings';
  return entry && entry.staff ? page : 'cases';
}

// The pages a filter box leaves in the sidebar: those whose name holds what was typed.
export function matchPages(items, text) {
  const needle = text.trim().toLowerCase();
  return needle ? items.filter((item) => item.name.toLowerCase().includes(needle)) : items;
}
