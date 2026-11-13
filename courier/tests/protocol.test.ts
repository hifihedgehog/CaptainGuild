// Tests for the delivery protocol, run with Node's own runner: node --test tests/protocol.test.ts
// The fixed values below are also asserted by Captain Guild's tests (tests/test_reddit.py), so both sides agree on the format.
import assert from "node:assert/strict";
import { test } from "node:test";
import { CHUNK, checksum, chunksOf, decode, deliveryAddress, encode, messagesFor, parseAccounts, parseWebhookUrl, payloadFor } from "../src/shared/protocol.ts";

const LINK = "https://www.reddit.com/r/example/comments/abc123/hello/";
const ENCODED =
  "eyJ2IjoxLCJpZCI6InQzX2FiYzEyMyIsImF1dGhvciI6IkV4YW1wbGUiLCJjb21tdW5pdHkiOiJleGFtcGxlIiwidGl0bGUiOiJIZWxsbyIsImxpbmsiOiJodHRwczovL3d3dy5yZWRkaXQuY29tL3IvZXhhbXBsZS9jb21tZW50cy9hYmMxMjMvaGVsbG8vIiwidXJsIjoiaHR0cHM6Ly93d3cucmVkZGl0LmNvbS9yL2V4YW1wbGUvY29tbWVudHMvYWJjMTIzL2hlbGxvLyIsImF0IjoxNzk0MDAwMDAwLCJodG1sIjoiPGRpdiBjbGFzcz1cIm1kXCI-PHA-SGk8L3A-PC9kaXY-IiwidGV4dCI6IkhpIiwibnNmdyI6ZmFsc2UsInNwb2lsZXIiOmZhbHNlfQ";
const CHECK = "7157e974";

function post(more = {}) {
  return {
    id: "t3_abc123", title: "Hello", permalink: "/r/example/comments/abc123/hello/", url: LINK, createdAt: new Date(1794000000 * 1000), authorName: "Example", subredditName: "example",
    body: "Hi", bodyHtml: '<div class="md"><p>Hi</p></div>', gallery: [], nsfw: false, spoiler: false, removed: false, ...more,
  };
}

test("the check is FNV-1a as published", () => {
  assert.deepEqual(["", "a", "foobar"].map(checksum), ["811c9dc5", "e40c292c", "bf9cf968"]);
});

test("a post encodes to the text and check the bot's tests expect", () => {
  const payload = payloadFor(post());
  assert.equal(JSON.stringify(payload), '{"v":1,"id":"t3_abc123","author":"Example","community":"example","title":"Hello","link":"' + LINK + '","url":"' + LINK + '","at":1794000000,"html":"<div class=\\"md\\"><p>Hi</p></div>","text":"Hi","nsfw":false,"spoiler":false}');
  assert.equal(encode(payload), ENCODED);
  assert.equal(checksum(ENCODED), CHECK);
  assert.deepEqual(messagesFor(payload), [{ username: "Reddit courier", allowed_mentions: { parse: [] }, embeds: [{ title: `cg-reddit 1 t3_abc123 1/1 ${CHECK}`, description: ENCODED }] }]);
});

test("a long post splits into parts that go back together exactly", () => {
  const html = '<div class="md"><p>' + "Café ☃ \u{1F600} words ".repeat(1400) + "</p></div>";
  const payload = payloadFor(post({ bodyHtml: html }));
  const messages = messagesFor(payload);
  assert.ok(messages.length > 3);
  const chunks = messages.map((message) => message.embeds[0].description);
  assert.ok(chunks.every((chunk) => chunk.length <= CHUNK && /^[A-Za-z0-9_-]+$/.test(chunk)));
  const whole = chunks.join("");
  const check = checksum(whole);
  messages.forEach((message, index) => assert.equal(message.embeds[0].title, `cg-reddit 1 t3_abc123 ${index + 1}/${messages.length} ${check}`));
  assert.deepEqual(decode(whole), payload);
  assert.equal(decode(whole).html, html);
  assert.deepEqual(chunksOf("abcdefg", 3), ["abc", "def", "g"]);
});

test("a gallery, a video and a crosspost carry what the bot needs, in a fixed order", () => {
  const gallery = payloadFor(post({ url: "https://www.reddit.com/gallery/abc123", gallery: [{ url: "https://i.redd.it/a1.jpg" }, { url: "https://i.redd.it/b2.jpg" }] }));
  assert.deepEqual(gallery.gallery, ["https://i.redd.it/a1.jpg", "https://i.redd.it/b2.jpg"]);
  const picture = payloadFor(post({ url: "https://i.redd.it/a1.jpg", gallery: [{ url: "https://preview.redd.it/a1.jpg?s=1" }] }));
  assert.equal(picture.gallery, undefined, "one preview picture on a post that isn't a gallery is not a gallery");
  const video = payloadFor(post({ url: "https://v.redd.it/vid123", crosspostParentId: "t3_zzz999", secureMedia: { redditVideo: { fallbackUrl: "https://v.redd.it/vid123/DASH_720.mp4?source=fallback", duration: 14, width: 1280, height: 720 } } }));
  assert.deepEqual(video.video, { url: "https://v.redd.it/vid123/DASH_720.mp4?source=fallback", seconds: 14, width: 1280, height: 720 });
  assert.deepEqual(Object.keys(video), ["v", "id", "author", "community", "title", "link", "url", "at", "html", "text", "video", "crosspost", "nsfw", "spoiler"]);
  const bare = payloadFor(post({ body: undefined, bodyHtml: undefined, nsfw: undefined }));
  assert.deepEqual([bare.html, bare.text, bare.nsfw], ["", "", false]);
});

test("the accounts setting reads names in the forms people type", () => {
  assert.deepEqual(parseAccounts("u/Example, /u/Other_One  third-name\nexample"), ["Example", "Other_One", "third-name"]);
  assert.deepEqual(parseAccounts(""), []);
  assert.deepEqual(parseAccounts(undefined), []);
  assert.throws(() => parseAccounts("ok_name, no"), /not a Reddit account name/);
  assert.throws(() => parseAccounts("has.dot"), /not a Reddit account name/);
  assert.throws(() => parseAccounts(Array.from({ length: 11 }, (_, index) => `name${index}`).join(",")), /up to 10 accounts/);
});

test("only a Discord webhook address is taken as the delivery address", () => {
  const good = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_";
  assert.equal(deliveryAddress(parseWebhookUrl(` ${good}?wait=false `)), good + "?wait=true");
  for (const bad of ["", "not an address", "http://discord.com/api/webhooks/123456789012345678/" + "a".repeat(60), "https://discordapp.com/api/webhooks/123456789012345678/" + "a".repeat(60),
    "https://discord.com.evil.example/api/webhooks/123456789012345678/" + "a".repeat(60), "https://discord.com/api/webhooks/123/short", "https://discord.com/api/channels/123456789012345678/messages"]) {
    assert.throws(() => parseWebhookUrl(bad), /delivery address/, bad);
  }
});
