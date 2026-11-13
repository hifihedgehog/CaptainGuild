// How the courier hands a post to Captain Guild, version 1. The bot's side of this is captainguild/core/reddit.py, and the two must
// agree, so nothing here changes without the same change there.
//
// A post becomes a JSON object, written compactly and encoded as base64url without padding. That text is cut into chunks, and each
// chunk goes out as one Discord webhook message: an embed whose title names the delivery and whose description is the chunk. The
// title carries a check over the whole encoded text, so the bot can tell a full set of parts from a mixed or damaged one.
//
// Nothing in this file touches Reddit, Discord or storage, so it runs under plain Node for the tests.

export const PROTOCOL = 1;
export const CHUNK = 3900; // an embed's description holds 4,096 characters
export const DAY_SECONDS = 24 * 60 * 60;
export const MAX_ACCOUNTS = 10;
const ACCOUNT = /^[A-Za-z0-9_-]{3,20}$/;
const WEBHOOK_PATH = /^\/api\/webhooks\/\d{15,21}\/[A-Za-z0-9_-]{30,}$/;

/** The fields of a post the courier reads. Reddit's own Post object has all of them. */
export type PostLike = {
  id: string;
  title: string;
  permalink: string;
  url: string;
  createdAt: Date;
  authorName: string;
  subredditName: string;
  body?: string | undefined;
  bodyHtml?: string | undefined;
  gallery?: { url: string }[] | undefined;
  secureMedia?: { redditVideo?: { fallbackUrl?: string | undefined; duration?: number | undefined; width?: number | undefined; height?: number | undefined } | undefined } | undefined;
  crosspostParentId?: string | undefined;
  nsfw?: boolean | undefined;
  spoiler?: boolean | undefined;
  removed?: boolean | undefined;
};

export type Payload = {
  v: number;
  id: string;
  author: string;
  community: string;
  title: string;
  link: string;
  url: string;
  at: number;
  html: string;
  text: string;
  gallery?: string[];
  video?: { url: string; seconds?: number; width?: number; height?: number };
  crosspost?: string;
  nsfw: boolean;
  spoiler: boolean;
};

export type WebhookBody = {
  username: string;
  allowed_mentions: { parse: string[] };
  embeds: { title: string; description: string }[];
};

/** A post as the object the bot expects. The key order is fixed, because the tests on both sides check the same encoded text. */
export function payloadFor(post: PostLike): Payload {
  const payload: Payload = {
    v: PROTOCOL,
    id: post.id,
    author: post.authorName,
    community: post.subredditName,
    title: post.title,
    link: `https://www.reddit.com${post.permalink}`,
    url: post.url,
    at: Math.floor(post.createdAt.getTime() / 1000),
    html: post.bodyHtml ?? "",
    text: post.body ?? "",
    nsfw: Boolean(post.nsfw),
    spoiler: Boolean(post.spoiler),
  };
  // Reddit lists one preview picture for a post that isn't a gallery, so the list goes along only for a real one.
  const gallery = (post.gallery ?? []).map((item) => item.url).filter((address) => typeof address === "string" && address.length > 0);
  if (gallery.length > 0 && post.url.includes("/gallery/")) payload.gallery = gallery;
  const video = post.secureMedia?.redditVideo;
  if (video?.fallbackUrl) {
    payload.video = { url: video.fallbackUrl };
    if (typeof video.duration === "number") payload.video.seconds = video.duration;
    if (typeof video.width === "number") payload.video.width = video.width;
    if (typeof video.height === "number") payload.video.height = video.height;
  }
  if (post.crosspostParentId) payload.crosspost = post.crosspostParentId;
  return orderedPayload(payload);
}

function orderedPayload(payload: Payload): Payload {
  const ordered: Record<string, unknown> = {};
  for (const key of ["v", "id", "author", "community", "title", "link", "url", "at", "html", "text", "gallery", "video", "crosspost", "nsfw", "spoiler"]) {
    const value = (payload as Record<string, unknown>)[key];
    if (value !== undefined) ordered[key] = value;
  }
  return ordered as Payload;
}

/** The payload as base64url text without padding. */
export function encode(payload: Payload): string {
  const bytes = new TextEncoder().encode(JSON.stringify(payload));
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** The reverse of `encode`, for the tests. */
export function decode(encoded: string): Payload {
  const padded = encoded.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (encoded.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return JSON.parse(new TextDecoder().decode(bytes)) as Payload;
}

/** FNV-1a, 32 bits, over the encoded text, as eight hex digits. The text is ASCII, so each character is one byte. */
export function checksum(encoded: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < encoded.length; index += 1) {
    hash ^= encoded.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

export function chunksOf(encoded: string, size: number = CHUNK): string[] {
  const chunks: string[] = [];
  for (let start = 0; start < encoded.length; start += size) chunks.push(encoded.slice(start, start + size));
  return chunks;
}

/** The webhook messages one post goes out as, in order. */
export function messagesFor(payload: Payload): WebhookBody[] {
  const encoded = encode(payload);
  const chunks = chunksOf(encoded);
  const check = checksum(encoded);
  return chunks.map((chunk, index) => ({
    username: "Reddit courier",
    allowed_mentions: { parse: [] },
    embeds: [{ title: `cg-reddit ${PROTOCOL} ${payload.id} ${index + 1}/${chunks.length} ${check}`, description: chunk }],
  }));
}

/** The account names in the setting: separated by commas or spaces, with or without u/ in front. Throws with a sentence when one isn't a name. */
export function parseAccounts(raw: unknown): string[] {
  if (typeof raw !== "string") return [];
  const names: string[] = [];
  for (const piece of raw.split(/[\s,]+/)) {
    const name = piece.replace(/^\/?u\//i, "");
    if (!name) continue;
    if (!ACCOUNT.test(name)) throw new Error(`"${piece}" is not a Reddit account name. A name is 3 to 20 letters, numbers, hyphens and underscores.`);
    if (!names.some((known) => known.toLowerCase() === name.toLowerCase())) names.push(name);
  }
  if (names.length > MAX_ACCOUNTS) throw new Error(`The courier follows up to ${MAX_ACCOUNTS} accounts. This names ${names.length}.`);
  return names;
}

/** The delivery address, once it is known to be a Discord webhook. Throws with a sentence when it isn't. */
export function parseWebhookUrl(raw: string): URL {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    throw new Error("The delivery address is not a web address.");
  }
  if (url.protocol !== "https:" || url.host !== "discord.com" || !WEBHOOK_PATH.test(url.pathname)) {
    throw new Error("The delivery address must be the one Captain Guild shows, which starts with https://discord.com/api/webhooks/.");
  }
  return url;
}

/** Where a delivery is posted: the webhook's own address, asking Discord to answer once the message is stored. */
export function deliveryAddress(webhook: URL): string {
  return `https://discord.com${webhook.pathname}?wait=true`;
}
