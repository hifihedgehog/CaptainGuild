// The My data page: what the bot holds about the person signed in, with a download and a delete for what's theirs.
let csrf = '';

function element(tag, text, className) {
  const made = document.createElement(tag);
  if (text !== undefined) made.textContent = text;
  if (className) made.className = className;
  return made;
}

async function remove(server, section, button) {
  if (!window.confirm(`Delete ${section.label.toLowerCase()} in ${server.name}? This can't be undone.`)) return;
  button.disabled = true;
  try {
    const response = await fetch('/api/my-data/delete', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ guild_id: server.id, section: section.key }),
    });
    if (!response.ok) {
      button.disabled = false;
      const answer = await response.json().catch(() => ({}));
      window.alert(answer.error || 'That could not be deleted. Reload the page and try again.');
      return;
    }
    show(await response.json());
  } catch (error) {
    button.disabled = false;
    window.alert('That could not be deleted. Check your connection and try again.');
  }
}

function serverBlock(server) {
  const block = element('section');
  block.append(element('h2', server.name));
  if (server.sections.length) {
    const table = element('table');
    const body = element('tbody');
    for (const section of server.sections) {
      const row = element('tr');
      row.append(element('td', section.label), element('td', String(section.count)));
      const cell = element('td');
      const button = element('button', 'Delete', 'btn');
      button.addEventListener('click', () => remove(server, section, button));
      cell.append(button);
      row.append(cell);
      body.append(row);
    }
    table.append(body);
    block.append(table);
  }
  if (server.messages) {
    const days = server.message_days ? `, each removed after ${server.message_days} days` : '';
    block.append(element('p', `${server.messages} of your messages are stored for the server's logs${days}. The download has them.`));
  }
  return block;
}

function show(data) {
  csrf = data.csrf;
  document.getElementById('who').textContent = `Signed in as ${data.user.name}.`;
  document.getElementById('download').hidden = false;
  document.getElementById('sign-out-everywhere').hidden = false;
  const servers = document.getElementById('servers');
  servers.replaceChildren();
  if (!data.servers.length) servers.append(element('p', 'The bot holds nothing about you that you can delete here.'));
  for (const server of data.servers) servers.append(serverBlock(server));
}

async function load() {
  const who = document.getElementById('who');
  try {
    const response = await fetch('/api/my-data', { credentials: 'same-origin' });
    if (response.status === 401) {
      location.href = '/login?next=/my-data';
      return;
    }
    if (!response.ok) {
      who.textContent = 'This page could not load. Reload to try again.';
      return;
    }
    show(await response.json());
  } catch (error) {
    who.textContent = 'This page could not load. Check your connection and reload to try again.';
  }
}

load();
