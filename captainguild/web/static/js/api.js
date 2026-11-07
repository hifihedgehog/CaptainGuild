// Talks to the server. `api` works inside the server being viewed: api.get('/tags') is GET /api/g/{id}/tags.
let csrf = '';
let guildId = null;
let sessionEnded = null;

// Where sign-in sends a person back to: the page they are on, with its query.
export const signInPath = () => '/login?next=' + encodeURIComponent(location.pathname + location.search);

// Once the dashboard is up it passes a function here, so a request answered with 401 shows a notice on the page and leaves the form alone.
export function onSessionEnd(handler) { sessionEnded = handler; }

export function setCsrf(value) { csrf = value || ''; }
export function setGuild(id) { guildId = id; }

export class ApiError extends Error {
  constructor(message, status, fields) {
    super(message);
    this.status = status;
    this.fields = fields || {};
  }
}

async function send(method, path, body, isForm) {
  const headers = {};
  if (method !== 'GET') headers['X-CSRF-Token'] = csrf;
  const options = { method, headers, credentials: 'same-origin' };
  if (body !== undefined) {
    if (isForm) {
      options.body = body;
    } else {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new ApiError('Could not reach the server. Check your connection and try again.', 0);
  }
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    if (response.status === 401) {
      if (sessionEnded) sessionEnded();
      else location.href = signInPath();
      throw new ApiError('Your sign-in ended. Sign in again, then repeat what you were doing.', 401);
    }
    throw new ApiError((data && data.error) || `The server answered ${response.status}.`, response.status, data && data.fields);
  }
  return data;
}

export const raw = {
  get: (path) => send('GET', path),
  post: (path, body) => send('POST', path, body),
};

const scoped = (path) => `/api/g/${guildId}${path}`;

export const api = {
  get: (path) => send('GET', scoped(path)),
  post: (path, body) => send('POST', scoped(path), body === undefined ? {} : body),
  put: (path, body) => send('PUT', scoped(path), body === undefined ? {} : body),
  patch: (path, body) => send('PATCH', scoped(path), body === undefined ? {} : body),
  del: (path) => send('DELETE', scoped(path)),
  upload: (path, formData) => send('POST', scoped(path), formData, true),
};
