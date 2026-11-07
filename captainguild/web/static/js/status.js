// Fills in the status page from /api/status.
function humanize(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

async function load() {
  const set = (id, text) => { document.getElementById(id).textContent = text; };
  try {
    const response = await fetch('/api/status');
    const data = await response.json();
    set('state', data.latency_ms === null ? 'Starting' : 'Online');
    set('uptime', humanize(data.uptime));
    set('latency', data.latency_ms === null ? 'n/a' : `${data.latency_ms} ms`);
    set('version', data.version);
  } catch (error) {
    set('state', 'Unreachable');
  }
}

load();
setInterval(load, 30000);
