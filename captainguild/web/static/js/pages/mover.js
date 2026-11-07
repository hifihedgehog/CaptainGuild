// The message mover: members move or copy their own messages, and moderators anyone's, under the author's name and picture.
import { html, useEffect, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Col, DataTable, Grid, Icon, Loading, SaveBar, SaveButton, SettingsGate, Switch, channelName, confirmDialog, toast, toastError, useGuild, useSettings, when } from '../ui.js';
import { memberLabel, useMembers } from '../members.js';

function Moved() {
  const guild = useGuild();
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = () => api.get('/mover/messages').then(setData).catch((error) => { toastError(error); setData({ rows: [], total: 0 }); });
  useEffect(() => { load(); }, [guild.id]);
  useMembers(guild.id, data ? data.rows.flatMap((row) => [row.user_id, row.author_id]) : []);
  const prune = async () => {
    const sure = await confirmDialog({ title: 'Forget messages that are gone?', body: 'The bot checks each moved or copied message and forgets the ones that were deleted, so nobody can edit them again. This is what /prune_messages does.', confirm: 'Prune' });
    if (!sure) return;
    setBusy(true);
    try {
      const answer = await api.post('/mover/prune', {});
      toast(`Pruned ${answer.pruned} out of ${answer.total} messages.${answer.finished ? '' : ' Run it again to check the rest.'}`);
      load();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  if (data === null) return html`<${Loading} />`;
  const kind = (row) => `${row.copied ? 'Copied' : 'Moved'}${row.imported ? ', brought over' : ''}`;
  return html`<div>
    <div class="row spread">
      <span class="muted small">${data.total} on record</span>
      ${guild.is_admin && html`<button class="btn small outline" disabled=${busy || data.total === 0} onClick=${prune}>${busy ? 'Checking...' : 'Prune'}</button>`}
    </div>
    <${DataTable} rows=${data.rows} empty="Nothing has been moved or copied yet." columns=${[
      { key: 'where', label: 'Where it is', filter: true, text: (row) => '#' + channelName(guild, row.thread_id || row.channel_id) },
      { key: 'user_id', label: 'Moved by', filter: true, text: (row) => memberLabel(guild.id, row.user_id) },
      { key: 'author_id', label: 'Written by', filter: true, text: (row) => (row.author_id ? memberLabel(guild.id, row.author_id) : '') },
      { key: 'kind', label: 'What', width: '150px', text: kind },
      { key: 'moved_at', label: 'When', sort: true, width: '165px', render: (row) => when(row.moved_at) },
      { key: 'jump', label: '', width: '40px', render: (row) => html`<a class="icon-btn" href=${row.url} target="_blank" rel="noopener noreferrer" aria-label="Open in Discord" title="Open in Discord"><${Icon} name="external-link" /></a>` },
    ]} />
  </div>`;
}

export default function MoverPage() {
  const form = useSettings('mover');
  return html`<${SettingsGate} form=${form}>${() => html`<div>
    <${Grid}>
      <${Col}>
        <${Card} icon="corner-down-right" title="Message mover" footer=${html`<${SaveButton} form=${form} />`}>
          <${Switch} label="Message mover" help="Adds four right-click menus to this server: Move Message, Copy Message, Edit Moved Message and Delete Moved Message, with /mover_log_channel and /prune_messages." ...${form.bind('enabled')} />
          <p class="muted">Members move or copy their own messages, and anyone with Manage Messages in the channel, or the bot manager role, can move or copy any message. The copy shows the author's name and picture, carries the text, files and embeds, and pings nobody. Moving deletes the original. Whoever moved or copied a message can edit or delete the copy later.</p>
          <p class="muted">The bot needs Manage Webhooks in each channel it posts copies in, and Manage Messages to delete originals.</p>
          <hr class="rule" />
          <h4>Log</h4>
          <p class="muted">A line for each message moved, copied, edited or deleted, naming who did it.</p>
          <${ChannelPicker} label="Log channel" ...${form.bind('log_channel')} />
        <//>
      <//>
      <${Col}>
        <${Card} icon="list" title="Moved and copied messages">
          <${Moved} />
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />
  </div>`}<//>`;
}
