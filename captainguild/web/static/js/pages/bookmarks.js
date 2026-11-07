// Bookmarks: members right-click a message and get a copy of it in their DMs.
import { html } from '../lib.js';
import { Card, SaveBar, SaveButton, SettingsGate, Switch, useSettings } from '../ui.js';

export default function BookmarksPage() {
  const form = useSettings('bookmarks');
  return html`<${SettingsGate} form=${form}>${() => html`
    <${Card} icon="inbox" title="Bookmarks" footer=${html`<${SaveButton} form=${form} />`}>
      <${Switch} label="Switched on" help="Adds Bookmark This Message to the right-click menu of every message in this server. Discord can take a minute to show it." ...${form.bind('enabled')} />
      <p class="muted">Anyone can bookmark a message. The bot sends them a copy in their DMs: the text, the first picture, a link back to the message and where it came from. The Delete Bookmark button under the copy deletes it, and so does reacting to it with a trash can or a red X.</p>
      <p class="muted">The bot keeps nothing. A member whose DMs are closed is told to open them. Bookmarks members already have stay deletable after you switch this off.</p>
    <//>
    <${SaveBar} form=${form} />`}<//>`;
}
