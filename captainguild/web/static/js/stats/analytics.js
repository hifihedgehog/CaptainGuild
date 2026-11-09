// Analytics and the drilldowns: messages and voice side by side, how much came from people who have left, and members or channels
// compared one against another. These take Administrator, Manage Server or View Server Insights.
import { html, useMemo, useState } from '../lib.js';
import { ChannelPicker, Icon, Loading, Notice } from '../ui.js';
import { ChartFrame, TimeChart } from '../charts.js';
import { Channel, Person, count, dateText, decimal, inUnit, listOf, useStats, viewParams } from './common.js';
import { PeoplePicker } from './querybar.js';
import { StatTable, unpack } from './table.js';

const stamp = (period, index) => new Date(period.buckets[index] * 1000).toISOString();
const STAT_CHOICES = [['both', 'Messages and voice'], ['messages', 'Messages'], ['voice', 'Voice']];

export function StatChoice({ value, onChange }) {
  return html`<div class="row stats-statpick" role="radiogroup" aria-label="Show">${STAT_CHOICES.map(([key, text]) => html`<label key=${key} class="check"><input type="radio" checked=${value === key}
    onChange=${() => onChange(key)} /><span>${text}</span></label>`)}</div>`;
}

export function Analytics({ ctx, view, stat, setStat, unit, smooth, openDrill }) {
  const params = viewParams(view);
  const main = useStats('/statistics/analytics', params);
  const unique = useStats('/statistics/analytics/unique', params);
  const people = useStats('/statistics/analytics/members', params);
  const places = useStats('/statistics/analytics/channels', params);
  const [pickedPeople, setPickedPeople] = useState(new Set());
  const [pickedPlaces, setPickedPlaces] = useState(new Set());
  const voiceIn = (seconds) => inUnit(seconds, unit);
  const panels = useMemo(() => {
    if (!main.data) return [];
    const out = [];
    const both = unique.data;
    for (const kind of ['messages', 'voice']) {
      if (stat !== 'both' && stat !== kind) continue;
      const data = main.data[kind];
      const convert = kind === 'voice' ? voiceIn : (v) => v;
      const format = kind === 'voice' ? (v) => `${decimal(v, 1)} ${unit}` : count;
      if (data.not_in_server) {
        const left = data.not_in_server.map(convert);
        const here = data.count.map((value, index) => convert(value - data.not_in_server[index]));
        out.push({ key: kind, title: kind === 'voice' ? `Voice (${unit})` : 'Messages', format, stacked: true, series: [
          { key: kind + '-here', name: 'From people still here', values: here, slot: 0, kind: 'bars' },
          { key: kind + '-gone', name: 'From people who left', values: left, slot: 1, kind: 'bars' }] });
      } else {
        out.push({ key: kind, title: kind === 'voice' ? `Voice (${unit})` : 'Messages', format, series: [{ key: kind, name: kind === 'voice' ? 'Voice' : 'Messages', values: data.count.map(convert), slot: 0, kind: 'area' }] });
      }
    }
    const people = [{ key: 'm-text', name: 'In text', values: main.data.messages.members, slot: 0, kind: 'line' }, { key: 'm-voice', name: 'In voice', values: main.data.voice.members, slot: 1, kind: 'line' }];
    const places = [{ key: 'c-text', name: 'Text', values: main.data.messages.channels, slot: 0, kind: 'line' }, { key: 'c-voice', name: 'Voice', values: main.data.voice.channels, slot: 1, kind: 'line' }];
    if (both) {
      people.push({ key: 'm-both', name: 'In either', values: both.members, slot: 2, kind: 'line' });
      places.push({ key: 'c-both', name: 'Either', values: both.channels, slot: 2, kind: 'line' });
    }
    out.push({ key: 'members', title: 'Different members', format: count, series: people });
    out.push({ key: 'channels', title: 'Different channels', format: count, series: places });
    return out;
  }, [main.data, unique.data, stat, unit]);
  const error = main.error || people.error || places.error;
  if (error) return html`<${Notice} kind="bad">${error.message}<//>`;
  if (!main.data) return html`<${Loading} />`;
  const period = main.data.period;
  const table = () => {
    const series = panels.flatMap((panel) => panel.series.map((one) => ({ name: `${panel.title}: ${one.name}`, values: one.values })));
    return { columns: ['Bucket', 'Starts', ...series.map((one) => one.name)], rows: period.labels.map((label, index) => [label, stamp(period, index), ...series.map((one) => one.values[index])]) };
  };
  const peopleRows = unpack(people.data);
  const placeRows = unpack(places.data);
  const personColumns = [
    { key: 'display_name', label: 'Member', render: (row) => html`<${Person} plain person=${{ id: row.id, name: row.name, display_name: row.display_name, in_server: row.in_server, bot: row.bot }} />` },
    { key: 'messages', label: 'Messages', numeric: true, text: (row) => count(row.messages) },
    { key: 'voice', label: `Voice (${unit})`, numeric: true, text: (row) => decimal(voiceIn(row.voice), 1), csv: (row) => voiceIn(row.voice) },
    { key: 'message_channels', label: 'Text channels', numeric: true },
    { key: 'voice_channels', label: 'Voice channels', numeric: true },
    { key: 'in_server', label: 'In the server', text: (row) => (row.in_server === false ? 'No' : 'Yes') },
    { key: 'joined_at', label: 'Joined', numeric: true, text: (row) => dateText(row.joined_at, period.zone) },
    { key: 'created_at', label: 'Account made', numeric: true, text: (row) => dateText(row.created_at, period.zone), hidden: true },
    { key: 'id', label: 'ID', hidden: true },
    { key: 'name', label: 'Username', hidden: true },
  ];
  const placeColumns = [
    { key: 'name', label: 'Channel', render: (row) => html`<${Channel} channel=${{ id: row.id, name: row.name, type: row.type, category: row.category, deleted: row.deleted, hidden: row.hidden }} />` },
    { key: 'category', label: 'Category', text: (row) => row.category || '' },
    { key: 'messages', label: 'Messages', numeric: true, text: (row) => count(row.messages) },
    { key: 'voice', label: `Voice (${unit})`, numeric: true, text: (row) => decimal(voiceIn(row.voice), 1), csv: (row) => voiceIn(row.voice) },
    { key: 'message_members', label: 'Writers', numeric: true },
    { key: 'voice_members', label: 'In voice', numeric: true },
    { key: 'created_at', label: 'Made', numeric: true, text: (row) => dateText(row.created_at, period.zone), hidden: true },
    { key: 'id', label: 'ID', hidden: true, text: (row) => row.id || '' },
  ];
  return html`<div class="stats-stack">
    ${main.data.roles_on && html`<${Notice}>A role filter reads the roles people hold now, so activity from people who left isn't shown apart.<//>`}
    <${ChartFrame} title="Messages and voice" subtitle=${`${period.label}, ${period.zone_label}`} table=${table} file="analytics" busy=${main.loading || unique.loading}
      tools=${html`<${StatChoice} value=${stat} onChange=${setStat} />`}>
      <${TimeChart} labels=${period.labels} panels=${panels} smooth=${smooth} height=${150} />
    <//>
    <${ChartFrame} title="Members" subtitle="Pick up to 25 to compare them in the member drilldown" image=${false} file="analytics-members">
      ${people.data ? html`<${StatTable} label="Members" columns=${personColumns} rows=${peopleRows} file="analytics-members" selectable selected=${pickedPeople} onSelect=${setPickedPeople}
        initialSort=${{ key: 'messages', down: true }} busy=${people.loading} tools=${html`<button type="button" class="btn small primary" disabled=${!pickedPeople.size || pickedPeople.size > 25}
          onClick=${() => openDrill('members', [...pickedPeople])}><${Icon} name="users" size=${14} /> Compare ${pickedPeople.size || ''}</button>`} />` : html`<${Loading} />`}
    <//>
    <${ChartFrame} title="Channels" subtitle="Pick up to 25 to compare them in the channel drilldown" image=${false} file="analytics-channels">
      ${places.data ? html`<${StatTable} label="Channels" columns=${placeColumns} rows=${placeRows} rowKey=${(row) => row.id || row.name} file="analytics-channels" selectable selected=${pickedPlaces}
        onSelect=${setPickedPlaces} initialSort=${{ key: 'messages', down: true }} busy=${places.loading} tools=${html`<button type="button" class="btn small primary"
          disabled=${!pickedPlaces.size || pickedPlaces.size > 25} onClick=${() => openDrill('channels', [...pickedPlaces])}><${Icon} name="hash" size=${14} /> Compare ${pickedPlaces.size || ''}</button>`} />` : html`<${Loading} />`}
    <//>
  </div>`;
}

// ---- drilldowns ----
function Averages({ value, unit, voice }) {
  return html`<span class="small">${['hour', 'day', 'week', 'month'].map((name) => `${decimal(voice ? inUnit(value[name], unit) : value[name], 2)} a ${name}`).join(', ')}</span>`;
}

export function Drilldown({ kind, ctx, view, ids, setIds, unit, smooth }) {
  const picked = listOf(ids);
  const members = kind === 'members';
  const { data, error, loading } = useStats(`/statistics/drilldown/${kind}`, { ids: picked.join(','), ...viewParams(view) }, picked.length > 0);
  const rows = data ? data[kind] : [];
  const known = useMemo(() => Object.fromEntries((data && members ? data.members : []).map((row) => [String(row.user_id), row.person])), [data]);
  const name = (row) => (members ? row.person.display_name || row.person.name : row.channel.name);
  const shown = rows.filter((row) => !row.hidden);
  const panels = useMemo(() => {
    if (!data || !shown.length) return [];
    const drawn = shown.slice(0, 8);
    return ['messages', 'voice'].map((part) => ({ key: part, title: part === 'voice' ? `Voice (${unit})` : 'Messages', format: (v) => (part === 'voice' ? `${decimal(v, 1)} ${unit}` : count(v)),
      series: drawn.map((row, slot) => ({ key: `${part}-${row.user_id || row.channel_id}`, name: name(row), values: part === 'voice' ? row.series.voice.map((v) => inUnit(v, unit)) : row.series.messages, slot, kind: 'line' })) }));
  }, [data, unit]);
  const picker = members
    ? html`<${PeoplePicker} value=${picked} known=${known} limit=${25} placeholder="Add a member by name or ID" onChange=${(next) => setIds(next.join(','))} />`
    : html`<${ChannelPicker} multi categories types=${[0, 2, 5, 13, 15]} placeholder="Add channels or categories" value=${picked} onChange=${(next) => setIds((next || []).slice(0, 25).join(','))} />`;
  const facts = members ? [
    ['Account made', (row) => dateText(row.created_at, data.period.zone)],
    ['Joined', (row) => (row.left_at ? `${dateText(row.joined_at, data.period.zone)}, left ${dateText(row.left_at, data.period.zone)}` : dateText(row.joined_at, data.period.zone))],
    ['Last message', (row) => (row.last_message ? html`${dateText(row.last_message.at, data.period.zone, true)} in <${Channel} channel=${row.last_message.channel} />` : 'None')],
    ['Last in voice', (row) => (row.last_voice ? html`${dateText(row.last_voice.at, data.period.zone, true)} in <${Channel} channel=${row.last_voice.channel} />` : 'None')],
    ['Messages', (row) => count(row.messages)],
    [`Voice (${unit})`, (row) => decimal(inUnit(row.voice, unit), 1)],
    ['Messages on average', (row) => html`<${Averages} value=${row.averages.messages} />`],
    [`Voice on average (${unit})`, (row) => html`<${Averages} value=${row.averages.voice} unit=${unit} voice />`],
    ['Members invited', (row) => `${count(row.invited)}${row.invited_left ? `, ${count(row.invited_left)} since left` : ''}`],
  ] : [
    ['Made', (row) => dateText(row.created_at, data.period.zone)],
    ['Last message', (row) => (row.last_message ? html`${dateText(row.last_message.at, data.period.zone, true)} by <${Person} plain person=${row.last_message.person} />` : 'None')],
    ['Last in voice', (row) => (row.last_voice ? html`${dateText(row.last_voice.at, data.period.zone, true)}, <${Person} plain person=${row.last_voice.person} />` : 'None')],
    ['Messages', (row) => count(row.messages)],
    [`Voice (${unit})`, (row) => decimal(inUnit(row.voice, unit), 1)],
    ['Messages on average', (row) => html`<${Averages} value=${row.averages.messages} />`],
    [`Voice on average (${unit})`, (row) => html`<${Averages} value=${row.averages.voice} unit=${unit} voice />`],
  ];
  const plainFacts = members ? [
    ['created_at', (row) => row.created_at], ['joined_at', (row) => row.joined_at], ['left_at', (row) => row.left_at], ['last_message_at', (row) => row.last_message && row.last_message.at],
    ['last_message_channel', (row) => row.last_message && row.last_message.channel.name], ['last_voice_at', (row) => row.last_voice && row.last_voice.at],
    ['last_voice_channel', (row) => row.last_voice && row.last_voice.channel.name], ['message_count', (row) => row.messages], [`voice_${unit}`, (row) => inUnit(row.voice, unit)],
    ['invite_count', (row) => row.invited],
  ] : [
    ['created_on', (row) => row.created_at], ['recent_message_timestamp', (row) => row.last_message && row.last_message.at], ['recent_message_member', (row) => row.last_message && row.last_message.person.name],
    ['recent_voice_timestamp', (row) => row.last_voice && row.last_voice.at], ['recent_voice_member', (row) => row.last_voice && row.last_voice.person.name],
    ['message_count', (row) => row.messages], [`voice_${unit}`, (row) => inUnit(row.voice, unit)],
  ];
  const compareTable = () => ({ columns: [members ? 'member' : 'channel', 'id', ...plainFacts.map(([label]) => label)],
    rows: shown.map((row) => [name(row), members ? row.user_id : row.channel_id, ...plainFacts.map(([, read]) => read(row) ?? '')]) });
  return html`<div class="stats-stack">
    <div class="stats-drill-pick">${picker}<p class="small muted">Compare up to 25 ${members ? 'members' : 'channels'}. ${members ? 'Anyone the statistics hold a name for can be added, people who left included.' : 'A category covers every channel filed under it.'}</p></div>
    ${!picked.length && html`<div class="empty">Add ${members ? 'members' : 'channels'} above to compare them.</div>`}
    ${error && html`<${Notice} kind="bad">${error.message}<//>`}
    ${picked.length > 0 && !data && !error && html`<${Loading} />`}
    ${data && html`
      ${rows.some((row) => row.hidden) && html`<${Notice}>A channel you can't see is left out.<//>`}
      <${ChartFrame} title=${members ? 'Members compared' : 'Channels compared'} subtitle=${`${data.period.label}, ${data.period.zone_label}`} image=${false} table=${compareTable} file=${`${kind}-compared`} busy=${loading}>
        <div class="table-wrap"><table class="stats-compare">
          <thead><tr><th></th>${shown.map((row) => html`<th key=${row.user_id || row.channel_id}>${members ? html`<${Person} person=${row.person} />` : html`<${Channel} channel=${row.channel} />`}
            <button type="button" class="icon-btn" aria-label=${'Remove ' + name(row)} onClick=${() => setIds(picked.filter((id) => id !== String(members ? row.user_id : row.channel_id)).join(','))}><${Icon} name="x" size=${12} /></button></th>`)}</tr></thead>
          <tbody>${facts.map(([label, read]) => html`<tr key=${label}><th scope="row">${label}</th>${shown.map((row) => html`<td key=${row.user_id || row.channel_id}>${read(row)}</td>`)}</tr>`)}</tbody>
        </table></div>
      <//>
      <${ChartFrame} title="Over time" subtitle=${shown.length > 8 ? 'The first eight picked are drawn. The table holds them all.' : data.period.label} file=${`${kind}-over-time`} busy=${loading}
        table=${() => ({ columns: ['Bucket', 'Starts', ...panels.flatMap((panel) => panel.series.map((one) => `${panel.title}: ${one.name}`))],
          rows: data.period.labels.map((label, index) => [label, stamp(data.period, index), ...panels.flatMap((panel) => panel.series.map((one) => one.values[index]))]) })}>
        <${TimeChart} labels=${data.period.labels} panels=${panels} smooth=${smooth} height=${150} />
      <//>
      ${shown.map((row) => html`<${ChartFrame} key=${row.user_id || row.channel_id} title=${members ? `${name(row)} by channel` : `${name(row)} by member`} image=${false}
        subtitle=${!members && row.members_total > row.members.length ? `${data.period.label}. The ${row.members.length.toLocaleString()} most active of ${row.members_total.toLocaleString()}.` : data.period.label}
        file=${`${name(row)}-${members ? 'channels' : 'members'}`}>
        <${StatTable} label=${name(row)} rows=${members ? row.channels : row.members.map(([id, messages, voice]) => ({ user_id: id, messages, voice, person: (data.people || {})[String(id)] }))}
          rowKey=${(part) => (members ? part.channel_id : part.user_id)} file=${`${name(row)}-breakdown`}
          initialSort=${{ key: 'messages', down: true }} columns=${[
            members ? { key: 'channel', label: 'Channel', render: (part) => html`<${Channel} channel=${part.channel} />`, text: (part) => part.channel.name }
              : { key: 'person', label: 'Member', render: (part) => html`<${Person} plain person=${part.person} />`, text: (part) => part.person.display_name || part.person.name },
            { key: 'messages', label: 'Messages', numeric: true, text: (part) => count(part.messages), csv: (part) => part.messages },
            { key: 'voice', label: `Voice (${unit})`, numeric: true, text: (part) => decimal(inUnit(part.voice, unit), 1), csv: (part) => inUnit(part.voice, unit) },
            { key: members ? 'channel_id' : 'user_id', label: 'ID', hidden: true },
          ]} />
      <//>`)}`}
  </div>`;
}
