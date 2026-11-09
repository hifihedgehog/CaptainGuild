// The My data page: what the bot holds about the person signed in, with a download and a delete for what's theirs.
let csrf = '';

function element(tag, text, className) {
  const made = document.createElement(tag);
  if (text !== undefined) made.textContent = text;
  if (className) made.className = className;
  return made;
}

async function send(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: JSON.stringify(body),
  });
  const answer = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(answer.error || 'That didn\'t work. Reload the page and try again.');
  return answer;
}

async function remove(server, section, button) {
  if (!window.confirm(`Delete ${section.label.toLowerCase()} in ${server.name}? This can't be undone.`)) return;
  button.disabled = true;
  try {
    show(await send('/api/my-data/delete', { guild_id: server.id, section: section.key }));
  } catch (error) {
    button.disabled = false;
    window.alert(error.message || 'That could not be deleted. Check your connection and try again.');
  }
}

function statisticsLine(numbers) {
  const parts = [`${numbers.messages.toLocaleString()} messages`];
  if (numbers.voice_hours) parts.push(`${numbers.voice_hours.toLocaleString()} hours in voice`);
  if (numbers.activity_hours) parts.push(`${numbers.activity_hours.toLocaleString()} hours in games and apps`);
  if (numbers.emoji) parts.push(`${numbers.emoji.toLocaleString()} emoji`);
  if (numbers.joins) parts.push(`${numbers.joins.toLocaleString()} joins`);
  return `Statistics count ${parts.join(', ')} under your name here. They also make up the server's totals, so they aren't deleted: Statistics, below, takes your name off them in every server.`;
}

function serverBlock(server) {
  const block = element('section');
  block.append(element('h2', server.name));
  const sections = server.sections.filter((section) => !section.everywhere);
  if (sections.length) {
    const table = element('table');
    const body = element('tbody');
    for (const section of sections) {
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
  if (server.statistics) block.append(element('p', statisticsLine(server.statistics)));
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

function dateOf(seconds) {
  return new Date(seconds * 1000).toLocaleString(undefined, { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// The statistics choices, which apply in every server: whether counts carry the person's name, and whether replies come only to them.
function showStatistics(state) {
  const box = document.getElementById('statistics');
  box.hidden = false;
  box.replaceChildren(element('h2', 'Statistics'));
  const choose = async (button, body, question) => {
    if (question && !window.confirm(question)) return;
    button.disabled = true;
    try {
      showStatistics(await send('/api/my-data/statistics', body));
      if ('private' in body) load();
    } catch (error) {
      button.disabled = false;
      window.alert(error.message);
    }
  };
  if (state.private && state.pending) {
    box.append(element('p', `New statistics carry no name since ${dateOf(state.changed_at)}, but some of what was counted before still carries it. Moving it to no one stopped partway.`));
    const finish = element('button', 'Finish taking my name off', 'btn primary');
    finish.addEventListener('click', () => choose(finish, { private: true }));
    box.append(finish);
  } else if (state.private) {
    box.append(element('p', `Your statistics carry no name, in every server, since ${dateOf(state.changed_at)}. What was counted before then stays nameless for good.`));
    if (state.can_change) {
      const back = element('button', 'Count under my name again', 'btn');
      back.addEventListener('click', () => choose(back, { private: false }, 'From now on, what you do counts under your name again. What was counted before stays nameless. Go ahead?'));
      box.append(back);
    } else {
      box.append(element('p', `It can change once a day. You can change it again after ${dateOf(state.can_change_at)}.`, 'muted'));
    }
  } else {
    box.append(element('p', 'Servers with Statistics on count your messages, voice time, time in games and apps, emoji and joins under your name. No message text is kept for this. Taking your name off moves everything counted under it, in every server, to no one: it still adds to each server\'s totals, it never comes back to your name, and what you do from then on counts under no name too. It can change once a day.'));
    if (state.can_change) {
      const off = element('button', 'Take my name off my statistics', 'btn primary');
      off.addEventListener('click', () => choose(off, { private: true }, 'This takes your name off everything counted under it, in every server, and it can\'t be undone. Stat roles stop seeing you. Go ahead?'));
      box.append(off);
    } else {
      box.append(element('p', `It changed less than a day ago. You can change it again after ${dateOf(state.can_change_at)}.`, 'muted'));
    }
  }
  const replies = element('label', undefined, 'check');
  const tick = element('input');
  tick.type = 'checkbox';
  tick.checked = state.private_replies;
  tick.addEventListener('change', () => choose(tick, { private_replies: tick.checked }));
  replies.append(tick, element('span', 'Show the statistics I ask for in Discord only to me'));
  box.append(element('p'), replies);
}

async function loadStatistics() {
  try {
    const response = await fetch('/api/my-data/statistics', { credentials: 'same-origin' });
    if (response.ok) showStatistics(await response.json());
  } catch (error) {
    // the rest of the page still works without this part
  }
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

load().then(loadStatistics);
