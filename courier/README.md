# Guild Courier

A small app for Reddit's Developer Platform that hands a Reddit account's new posts to [Captain Guild](https://github.com/hifihedgehog/CaptainGuild), which relays them to a Discord channel in full.

Captain Guild can read an account's posts from the account's feed. The courier is its second source. It runs on Reddit, under your own Reddit account, on a private community of yours. Nobody else has to install or approve anything.

## What it does

Once a minute the courier reads the newest posts of each account you name. A post it has not handed over before goes to the delivery address you gave it, which is a Discord webhook Captain Guild made. The bot reads the post from that webhook's channel, sends it to the channel you chose with its pictures and video, and clears the delivery.

- Only an account's own posts are read, never its comments.
- When the courier first sees an account, it notes the posts already there. It hands over the newest one once, as a check that it reaches the bot, and Captain Guild's page then shows the time of that delivery. The bot relays only posts made after it began following the account.
- A post more than a day old, or one Reddit has removed, is not sent.
- A post Discord does not take stays unsent and is tried again the next minute.

Each delivery holds the post's ID, title, author, community, link, time, body and the addresses of its pictures or video. A long post is sent in several parts, which the bot puts back together.

## Fetch Domains

The following domains are requested for this app:

- `discord.com` - The courier posts each new post to one Discord webhook, the delivery address in its settings. It contacts no other site.

## What it stores

For each followed account, the IDs and post times of the last 300 posts it noted or handed over, so none goes twice, and the time it first looked at the account. It also keeps the time of its last hourly log line. See [PRIVACY.md](PRIVACY.md) and [TERMS.md](TERMS.md).

## Setup

You need Node.js 24, npm and a Reddit account.

In Captain Guild's dashboard, open **Reddit posts**, add the account under **Add an account**, then under **Courier** pick a private channel and press **Make the delivery address**. Copy the address.

Then, in this folder:

```sh
npm install
npx devvit login
npx devvit init --force
npx devvit upload
```

- `devvit login` signs the command line in to your Reddit account.
- `devvit init --force` creates the app under your account. Reddit asks for a name in your browser, and any free name works.
- `devvit upload` sends the app to Reddit. The first upload creates a private community for the app and installs it there. It prints the community's address and writes its name into `devvit.json`.

Open the app's settings for that community on Reddit. Put the address you copied from Captain Guild in **Delivery address**, and the account names in **Accounts to follow**, separated by commas, such as `u/example`.

If you build this copy only for yourself, you can put both values in `src/server/preset.ts` before the upload instead, and leave the settings empty. Don't share a copy with the delivery address in it.

Reddit asks an app that contacts another site for links to its terms and its privacy policy. On the app's developer settings page, give the links to [TERMS.md](TERMS.md) and [PRIVACY.md](PRIVACY.md) in your copy of this repository.

To check that it runs, read the log:

```sh
npx devvit logs <your community>
```

The first run for an account logs `now following u/<name>`. Each post handed over logs `handed over t3_...`. Once an hour the log says `running, and following ...`, so a quiet log still shows the courier is alive.

## Changing it

To follow other accounts, edit **Accounts to follow** in the app's settings. To pause the courier, empty **Delivery address**. To move the deliveries to another channel, make a new delivery address in Captain Guild and paste it in.

After changing the code, run `npx devvit upload`, then `npx devvit install <your community>` to put the new version in place. `npx devvit uninstall <your community>` removes the app.

## Tests

```sh
node --test tests/protocol.test.ts
```

The tests cover the delivery format. Captain Guild's own tests check the same fixed values, so the two sides agree.
