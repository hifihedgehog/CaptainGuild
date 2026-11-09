// The Statistics page: everything the server counts, as charts and tables, for whoever the server lets see it.
import { html } from '../lib.js';
import { useGuild } from '../ui.js';
import { readFrom } from '../stats/common.js';
import { StatsApp } from '../stats/app.js';

export default function StatisticsPage() {
  const guild = useGuild();
  readFrom({ public: false, guild: guild.id });
  return html`<${StatsApp} settingsLink=${`/dashboard/${guild.id}/statssettings`} counterLink=${`/dashboard/${guild.id}/counters`}
    statroleLink=${`/dashboard/${guild.id}/statroles`} />`;
}
