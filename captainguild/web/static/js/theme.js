// The dashboard's two themes. Dark is the default. A choice of light is kept in this browser and applies on every page of the site.
const KEY = 'cg-theme';

export function savedTheme() {
  try {
    return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark';
  } catch (error) {
    return 'dark'; // a private window can refuse storage
  }
}

export function applyTheme(theme = savedTheme()) {
  if (theme === 'light') document.documentElement.dataset.theme = 'light';
  else delete document.documentElement.dataset.theme;
  return theme;
}

export function setTheme(theme) {
  try { localStorage.setItem(KEY, theme); } catch (error) { /* a private window can refuse storage */ }
  return applyTheme(theme);
}
