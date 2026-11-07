// Names for member IDs. A list that holds many people (ignored members, a mention in a message) asks for them in batches of 100, and every page shares the answers.
import { useEffect, useState } from './lib.js';
import { api } from './api.js';

const BATCH = 100;
const CACHE_LIMIT = 5000;
const known = new Map(); // "guild:id" -> the person's row, or null when the server has no such member
const asking = new Set();
const listeners = new Set(); // every mounted caller, so one that found its people already on the way hears the answer too
const queued = new Map(); // guild -> IDs waiting for the next call
let flushing = null;

const keyOf = (guildId, id) => `${guildId}:${id}`;
const isId = (value) => /^\d{1,20}$/.test(String(value));

// undefined: not asked yet. null: asked and the server doesn't have them. Otherwise the row.
export const knownMember = (guildId, id) => known.get(keyOf(guildId, id));

// What to show for someone: their name, a note that they left, or the ID itself while the answer is on its way.
export function memberLabel(guildId, id) {
  const row = knownMember(guildId, id);
  if (row) return row.display_name || row.name;
  return row === null ? `Left the server (${id})` : String(id);
}

// Rows of a log or a list each ask for their own people in the same moment, so the asks wait a beat and go out together.
function fetchSoon(guildId, ids) {
  if (!queued.has(guildId)) queued.set(guildId, new Set());
  ids.forEach((id) => queued.get(guildId).add(id));
  if (flushing) return;
  flushing = setTimeout(() => {
    flushing = null;
    const batches = [...queued.entries()];
    queued.clear();
    for (const [guild, set] of batches) {
      const all = [...set];
      for (let start = 0; start < all.length; start += BATCH) fetchBatch(guild, all.slice(start, start + BATCH));
    }
  }, 20);
}

function fetchBatch(guildId, part) {
  api.get(`/members?ids=${part.join(',')}`).then((answer) => {
    if (known.size > CACHE_LIMIT) known.clear();
    const found = new Map(answer.members.map((row) => [String(row.id), row]));
    part.forEach((id) => known.set(keyOf(guildId, id), found.get(id) || null));
  }).catch(() => {}).finally(() => {
    part.forEach((id) => asking.delete(keyOf(guildId, id)));
    listeners.forEach((listener) => listener());
  });
}

// Asks for whoever isn't known yet and re-renders the caller as answers arrive.
export function useMembers(guildId, ids) {
  const [, setTick] = useState(0);
  const wanted = [...new Set((ids || []).map(String).filter(isId))];
  const signature = `${guildId}|${wanted.join(',')}`;
  useEffect(() => {
    const hear = () => setTick((n) => n + 1);
    listeners.add(hear);
    return () => { listeners.delete(hear); };
  }, []);
  useEffect(() => {
    const need = wanted.filter((id) => !known.has(keyOf(guildId, id)) && !asking.has(keyOf(guildId, id)));
    if (need.length === 0) return;
    need.forEach((id) => asking.add(keyOf(guildId, id)));
    fetchSoon(guildId, need);
  }, [signature]);
  return (id) => knownMember(guildId, id);
}
