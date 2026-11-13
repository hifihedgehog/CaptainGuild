// The courier's one job: once a minute, read the newest posts of each followed account and hand the new ones to Captain Guild.
//
// A post counts as handed over only when Discord took every part of it. Anything that fails stays unsent and is tried again on the
// next run. The bot ignores a post it already has, so sending one twice does no harm.
import { reddit, redis, settings } from "@devvit/web/server";
import { DAY_SECONDS, deliveryAddress, messagesFor, parseAccounts, parseWebhookUrl, payloadFor, type Payload, type PostLike } from "../shared/protocol";
import { PRESET } from "./preset";

const BUDGET_MS = 20_000; // Reddit gives a request 30 seconds, and a run stops well short of that
const FETCH_TIMEOUT_MS = 8_000;
const LISTING = 25;
const KEPT = 300; // how many handed-over posts are remembered for each account
const HOUR_MS = 60 * 60 * 1000;
const BEAT_KEY = "beat";

function describe(error: unknown): string {
  if (!(error instanceof Error)) return String(error);
  const cause = error.cause instanceof Error ? ` (cause: ${error.cause.message})` : "";
  return `${error.name}: ${error.message}${cause}`;
}

function seconds(moment: Date): number {
  return Math.floor(moment.getTime() / 1000);
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export async function deliverAll(): Promise<void> {
  const started = Date.now();
  const rawAddress = (await settings.get<string>("discordWebhookUrl")) || PRESET.webhook;
  const rawAccounts = (await settings.get<string>("accounts")) || PRESET.accounts;
  if (!rawAddress || !rawAccounts) return; // installed but not set up yet
  const webhook = parseWebhookUrl(rawAddress);
  const accounts = parseAccounts(rawAccounts);
  // A run with nothing new says nothing, so one line an hour shows in the log that the courier is still running.
  if (started - Number((await redis.get(BEAT_KEY)) ?? 0) > HOUR_MS) {
    await redis.set(BEAT_KEY, String(started));
    console.log(`deliver: running, and following ${accounts.map((account) => `u/${account}`).join(", ")}`);
  }
  for (const account of accounts) {
    if (Date.now() - started > BUDGET_MS) {
      console.log("deliver: out of time, and the rest waits for the next run");
      return;
    }
    try {
      await deliverAccount(account, webhook, started);
    } catch (error) {
      console.error(`deliver: u/${account} failed: ${describe(error)}`); // one account failing must not stop the others
    }
  }
}

async function deliverAccount(account: string, webhook: URL, started: number): Promise<void> {
  const name = account.toLowerCase();
  const sentKey = `sent:${name}`;
  const seededKey = `seeded:5:${name}`; // a new number here makes every account's first look, and its check, run again
  const posts = await reddit.getPostsByUser({ username: account, sort: "new", limit: LISTING, pageSize: LISTING }).all();

  if (!(await redis.get(seededKey))) {
    // The first look at an account. What is already there is remembered, so installing the courier floods nobody.
    if (posts.length > 0) {
      await redis.zAdd(sentKey, ...posts.map((post) => ({ member: post.id, score: seconds(post.createdAt) })));
    }
    // The newest post goes out once all the same, as a check that the courier reaches the bot. The bot relays only posts made after it
    // began following the account, so an older one is taken in and goes no further. Until this check gets through, the first look repeats.
    console.log(`deliver: Reddit lists for u/${account}: ${posts.slice(0, 5).map((post) => `${post.id} ${post.createdAt.toISOString()}${post.removed ? " removed" : ""}`).join(", ")}`);
    // A post pinned to the account's profile leads the list whatever its age, so the newest is found by its time.
    const newest = posts.filter((post) => !post.removed).sort((first, second) => second.createdAt.getTime() - first.createdAt.getTime())[0];
    if (newest && !(await send(webhook, payloadFor(newest as PostLike), started))) {
      console.error(`deliver: the first delivery for u/${account} did not get through, and it is tried again next minute`);
      return;
    }
    await redis.set(seededKey, String(Date.now()));
    console.log(`deliver: now following u/${account}, with ${posts.length} earlier posts on record, and ${newest ? newest.id : "no post"} handed over as the check`);
    return;
  }

  const oldest = Date.now() / 1000 - DAY_SECONDS;
  const fresh = [];
  for (const post of posts) {
    if (post.removed || seconds(post.createdAt) < oldest) continue;
    const known = await redis.zScore(sentKey, post.id);
    if (known === undefined || known === null) fresh.push(post);
  }
  fresh.sort((first, second) => first.createdAt.getTime() - second.createdAt.getTime());

  for (const post of fresh) {
    if (Date.now() - started > BUDGET_MS) return;
    const delivered = await send(webhook, payloadFor(post as PostLike), started);
    if (!delivered) return; // it stays unsent, and the later ones wait behind it so they arrive in order
    await redis.zAdd(sentKey, { member: post.id, score: seconds(post.createdAt) });
    console.log(`deliver: handed over ${post.id} from u/${account}`);
  }

  const count = await redis.zCard(sentKey);
  if (count > KEPT) await redis.zRemRangeByRank(sentKey, 0, count - KEPT - 1);
}

/** Posts every part of one delivery. True when Discord took them all. */
async function send(webhook: URL, payload: Payload, started: number): Promise<boolean> {
  for (const body of messagesFor(payload)) {
    let taken = false;
    for (let attempt = 0; attempt < 2 && !taken; attempt += 1) {
      if (Date.now() - started > BUDGET_MS) return false;
      let response: Response;
      try {
        response = await fetch(deliveryAddress(webhook), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
        });
      } catch (error) {
        console.error(`deliver: Discord could not be reached for ${payload.id}: ${describe(error)}`);
        return false;
      }
      if (response.status === 200 || response.status === 204) {
        taken = true;
      } else if (response.status === 429 && attempt === 0) {
        // Discord says how long to wait. The wait is taken when it fits in the time this run has left.
        const pause = Math.ceil(Number(response.headers?.get("retry-after") ?? "1") * 1000);
        if (!Number.isFinite(pause) || Date.now() - started + pause > BUDGET_MS) return false;
        await wait(pause);
      } else {
        console.error(`deliver: Discord answered ${response.status} for ${payload.id}`);
        return false;
      }
    }
    if (!taken) return false;
  }
  return true;
}
