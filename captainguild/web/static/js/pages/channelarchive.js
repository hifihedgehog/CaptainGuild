// Channel archive: copy a channel or a forum, threads included, into another channel under each author's name.
import { html, useEffect, useRef, useState } from '../lib.js';
import { api } from '../api.js';
import { Card, ChannelPicker, Loading, Notice, SaveBar, SaveButton, SettingsGate, Switch, toast, toastError, useGuild, useSettings, when } from '../ui.js';

const STATES = { running: 'Running', stopped: 'Stopped', done: 'Done' };

function StartCopy({ onStarted, readsText }) {
  const [source, setSource] = useState(null);
  const [target, setTarget] = useState(null);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const start = async () => {
    setBusy(true);
    setErrors({});
    try {
      await api.post('/channelarchive', { source_channel_id: source, target_channel_id: target });
      toast('Copy started. It counts the messages first.');
      onStarted();
    } catch (error) {
      setErrors(error.fields || {});
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  return html`<${Card} icon="copy" title="Copy a channel">
    ${!readsText && html`<${Notice} kind="warn">Copying needs the Message Content intent, which this bot doesn't have. Whoever hosts it turns it on in the Discord Developer Portal.<//>`}
    <p class="muted">Every message goes over under its author's name and picture, with its files, embeds, stickers and a poll's results, one every two seconds. Nobody is pinged. Copying into the same place again brings only what's new. /copy_channel also copies into a thread.</p>
    <${ChannelPicker} label="Copy from" types=${[0, 5, 15]} value=${source} onChange=${setSource} error=${errors.source_channel_id} />
    <${ChannelPicker} label="Copy into" types=${[0, 5]} value=${target} onChange=${setTarget} error=${errors.target_channel_id} />
    <button class="btn primary" disabled=${busy || !source || !target || !readsText} onClick=${start}>Start copying</button>
  <//>`;
}

function Job({ job, onChanged, readsText }) {
  const act = async (verb) => {
    try {
      await api.post(`/channelarchive/${job.id}/${verb}`);
      toast(verb === 'stop' ? 'Stopped. Resume carries on from the same place.' : 'Carrying on.');
      onChanged();
    } catch (error) {
      toastError(error);
    }
  };
  const share = job.total ? Math.min(100, Math.round((job.copied / job.total) * 100)) : (job.state === 'done' ? 100 : 0);
  return html`<div class="field-edit">
    <div class="spread"><b>#${job.source_name} into #${job.target_name}</b>
      ${job.state === 'running' && html`<button class="btn outline danger" onClick=${() => act('stop')}>Stop</button>`}
      ${job.state === 'stopped' && html`<button class="btn primary" disabled=${!readsText} onClick=${() => act('resume')}>Resume</button>`}</div>
    <div class="progress"><span style=${{ width: share + '%' }}></span></div>
    <div class="muted">${STATES[job.state] || job.state}${job.stage ? `: ${job.stage}` : ''}. ${job.copied.toLocaleString()} of ${job.total.toLocaleString()} copied${job.failed ? `, ${job.failed.toLocaleString()} not copied` : ''}. Started ${when(job.created_at)}${job.started_by_name ? ` by ${job.started_by_name}` : ''}.</div>
    ${job.note && html`<${Notice} kind=${job.state === 'stopped' ? 'warn' : ''}>${job.note}<//>`}
    ${job.failures.length > 0 && html`<details><summary>Messages that weren't copied</summary>
      ${job.failures.map((failure) => html`<div key=${failure.url}><a href=${failure.url} target="_blank" rel="noopener noreferrer">${failure.url}</a> <span class="muted">${failure.error}</span></div>`)}</details>`}
  </div>`;
}

export default function ChannelArchivePage() {
  const guild = useGuild();
  const form = useSettings('channelarchive');
  const [data, setData] = useState(null);
  const timer = useRef(null);
  const load = async () => {
    clearTimeout(timer.current);
    try {
      const next = await api.get('/channelarchive');
      setData(next);
      if (next.jobs.some((job) => job.state === 'running')) timer.current = setTimeout(load, 3000);
    } catch (error) {
      toastError(error);
      setData({ jobs: [], reads_text: true });
    }
  };
  useEffect(() => { load(); return () => clearTimeout(timer.current); }, [guild.id]);
  return html`<${SettingsGate} form=${form}>${() => html`
    <${Card} icon="copy" title="Channel archive" footer=${html`<${SaveButton} form=${form} />`}>
      <${Switch} label="Switched on" help="Adds /copy_channel to this server. Discord can take a minute to show it." ...${form.bind('enabled')} />
      <p class="muted">A copy marks where the channel and each thread start and end, posts its progress every 100 messages, and keeps its place, so a restart carries on where it stopped. One copy runs at a time, and the next waits its turn. It takes Manage Webhooks, and only the threads the person starting it can read go over.</p>
    <//>
    ${data ? html`<${StartCopy} onStarted=${load} readsText=${data.reads_text} />
      <${Card} icon="history" title="Copies">
        ${data.jobs.length === 0 && html`<p class="muted">No copies yet.</p>`}
        ${data.jobs.map((job) => html`<${Job} key=${job.id} job=${job} onChanged=${load} readsText=${data.reads_text} />`)}
      <//>` : html`<${Loading} />`}
    <${SaveBar} form=${form} />`}<//>`;
}
