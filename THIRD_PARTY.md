# Third-party work

## Shipped with Captain Guild

Each project's copyright notice and license text is in `captainguild/web/static/vendor/licenses/`, and DejaVu's sits beside the font.

| Component | Version | Copyright | License | File |
|---|---|---|---|---|
| [Preact](https://github.com/preactjs/preact), the copy built into the htm standalone file | 10 | Jason Miller | MIT | `captainguild/web/static/vendor/htm-preact.js` |
| [htm](https://github.com/developit/htm), its standalone build | 3.1.1 | Google Inc. | Apache 2.0 | `captainguild/web/static/vendor/htm-preact.js` |
| [Lucide](https://github.com/lucide-icons/lucide) icons, the ones the dashboard uses | lucide-static 0.460.0 | Lucide Contributors, and Cole Bemis for icons from Feather | ISC, with Feather's under MIT | `captainguild/web/static/js/icons.js` |
| [unicode-emoji-json](https://github.com/muan/unicode-emoji-json) emoji list | 0.8.0 | Mu-An Chiou | MIT | `captainguild/web/static/vendor/emoji.json` |
| [Unicode](https://www.unicode.org/) emoji names, in that list | | Unicode, Inc. | Unicode License V3 | `captainguild/web/static/vendor/emoji.json` |
| [Roboto](https://fonts.google.com/specimen/Roboto), from [Fontsource](https://github.com/fontsource/fontsource) | 5.1.0 | Google Inc. | Apache 2.0 | `captainguild/web/static/vendor/fonts/roboto-*` |
| [Poppins](https://fonts.google.com/specimen/Poppins), from [Fontsource](https://github.com/fontsource/fontsource) | 5.1.0 | The Poppins Project Authors | SIL Open Font License 1.1 | `captainguild/web/static/vendor/fonts/poppins-*` |
| [DejaVu Sans Bold](https://dejavu-fonts.github.io/), for rank cards | 2.37 | Bitstream, Inc. and Tavmjong Bah, with the DejaVu changes in the public domain | Bitstream Vera and Arev font licenses | `captainguild/assets/fonts/DejaVuSans-Bold.ttf`, with `DejaVu-LICENSE.txt` beside it |

## Python packages

Pinned in `requirements.lock`, and not part of this repository.

| Package | License |
|---|---|
| [discord.py](https://github.com/Rapptz/discord.py) | MIT |
| [aiohttp](https://github.com/aio-libs/aiohttp) | Apache 2.0 and MIT |
| [aiosqlite](https://github.com/omnilib/aiosqlite) | MIT |
| [sqlcipher3-wheels](https://github.com/laggykiller/sqlcipher3) | zlib/libpng. Its wheels bundle [SQLCipher](https://github.com/sqlcipher/sqlcipher), BSD 3-Clause |
| [cryptography](https://github.com/pyca/cryptography) | Apache 2.0 or BSD 3-Clause |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU |
| [feedparser](https://github.com/kurtmckee/feedparser) | BSD 2-Clause |
| [feedparser-sgmllib](https://github.com/python-syndication/feedparser-sgmllib) | PSF 2.0 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | BSD 3-Clause |
| [yarl](https://github.com/aio-libs/yarl) | Apache 2.0 |

## Language design

The template language in `captainguild/core/tagscript` follows the behavior of [TagScript](https://github.com/JonSnowbd/TagScript) by JonSnowbd (CC BY 4.0), so existing templates keep working. Its code here is written from scratch.

## Outside services

The bot calls these only when someone uses the matching feature. Each one sees the request come from the machine running the bot.

| Service | Used for | What it receives |
|---|---|---|
| [Twitch Helix API](https://dev.twitch.tv/docs/api/) (`id.twitch.tv`, `api.twitch.tv`) | Live stream alerts | The client ID and secret the server enters, and the channels it follows |
| [YouTube](https://www.youtube.com/) feeds and channel pages | New video alerts | The channels the server follows |
| [GamerPower API](https://www.gamerpower.com/api-read) (`gamerpower.com`) | Free game alerts | Nothing from the server |
| [CheapShark API](https://apidocs.cheapshark.com/) (`cheapshark.com`) | Game deal alerts | Nothing from the server |
| [MEE6 public leaderboards](https://mee6.xyz/) (`mee6.xyz`) | Importing XP, when a server asks for it | The server's ID |
| [The Cat API](https://thecatapi.com/) (`thecatapi.com`), [Cataas](https://cataas.com/) (`cataas.com`), [Dog CEO](https://dog.ceo/) (`dog.ceo`), [RandomDog](https://random.dog/) (`random.dog`), [RandomFox](https://randomfox.ca/) (`randomfox.ca`), [Random-d.uk](https://random-d.uk/) (`random-d.uk`) | `/cute`, which needs no setup | Nothing from the server |
| [Urban Dictionary](https://www.urbandictionary.com/) (`urbandictionary.com`) | `/fun urbandictionary`, which needs no setup | The word the member typed |
| [Fortnite-API](https://fortnite-api.com/) (`fortnite-api.com`) | `/games fortnite`, once the bot's host adds a `FORTNITE_API_KEY` | That key, and the player name and platform the member typed |
| [Roblox web APIs](https://create.roblox.com/docs/cloud/legacy/users/v1) (`users.roblox.com`, `friends.roblox.com`, `thumbnails.roblox.com`) | `/games roblox`, which needs no setup. The profile link in its answer goes to `www.roblox.com` | The user name the member typed |
| [pastebin.com](https://pastebin.com/) and [starb.in](https://starb.in/) | A paste link someone gives a tag or embed command | The paste's address |
| Any address someone gives the bot | `/fun addemoji` from a link, and a rank card background from a link | That address |
