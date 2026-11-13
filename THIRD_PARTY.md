# Third-party work

## Shipped with Captain Guild

Each project's copyright notice and license text is in `captainguild/web/static/vendor/licenses/`. Each font in `captainguild/assets/fonts/` has its license beside it.

| Component | Version | Copyright | License | File |
|---|---|---|---|---|
| [Preact](https://github.com/preactjs/preact), the copy built into the htm standalone file | 10 | Jason Miller | MIT | `captainguild/web/static/vendor/htm-preact.js` |
| [htm](https://github.com/developit/htm), its standalone build | 3.1.1 | Google Inc. | Apache 2.0 | `captainguild/web/static/vendor/htm-preact.js` |
| [Lucide](https://github.com/lucide-icons/lucide) icons, the ones the dashboard uses | lucide-static 0.460.0 | Lucide Contributors, and Cole Bemis for icons from Feather | ISC, with Feather's under MIT | `captainguild/web/static/js/icons.js` |
| [unicode-emoji-json](https://github.com/muan/unicode-emoji-json) emoji list | 0.8.0 | Mu-An Chiou | MIT | `captainguild/web/static/vendor/emoji.json` |
| [Unicode](https://www.unicode.org/) emoji names, in that list | | Unicode, Inc. | Unicode License V3 | `captainguild/web/static/vendor/emoji.json` |
| [Roboto](https://fonts.google.com/specimen/Roboto), from [Fontsource](https://github.com/fontsource/fontsource) | 5.1.0 | Google Inc. | Apache 2.0 | `captainguild/web/static/vendor/fonts/roboto-*` |
| [Poppins](https://fonts.google.com/specimen/Poppins), from [Fontsource](https://github.com/fontsource/fontsource) | 5.1.0 | The Poppins Project Authors | SIL Open Font License 1.1 | `captainguild/web/static/vendor/fonts/poppins-*` |
| [DejaVu Sans](https://dejavu-fonts.github.io/) and DejaVu Sans Bold, for rank cards, statistics cards and welcome pictures, from the [2.37 release](https://github.com/dejavu-fonts/dejavu-fonts/releases/tag/version_2_37) | 2.37 | Bitstream, Inc. and Tavmjong Bah, with the DejaVu changes in the public domain | Bitstream Vera and Arev font licenses | `captainguild/assets/fonts/DejaVuSans.ttf` and `DejaVuSans-Bold.ttf`, with `DejaVu-LICENSE.txt` beside them |

The fonts a welcome picture can use come unchanged from the [google/fonts](https://github.com/google/fonts) repository at commit `5bb28733cec313b63fbd415b574b5ae35c8aa8b8`. Each is saved in `captainguild/assets/fonts/` under a plain file name, with its license beside it as `<name>-LICENSE.txt`.

| Font | Version | Copyright | License | File, and its name in google/fonts |
|---|---|---|---|---|
| [Fredoka](https://fonts.google.com/specimen/Fredoka) | 2.001 | The Fredoka Project Authors | SIL Open Font License 1.1 | `Fredoka.ttf`, from `ofl/fredoka/Fredoka[wdth,wght].ttf` |
| [Manrope](https://fonts.google.com/specimen/Manrope) | 4.505 | The Manrope Project Authors | SIL Open Font License 1.1 | `Manrope.ttf`, from `ofl/manrope/Manrope[wght].ttf` |
| [Lora](https://fonts.google.com/specimen/Lora) | 3.008 | The Lora Project Authors | SIL Open Font License 1.1 | `Lora.ttf`, from `ofl/lora/Lora[wght].ttf` |
| [Roboto Slab](https://fonts.google.com/specimen/Roboto+Slab) | 2.002 | The Roboto Slab Project Authors | Apache 2.0 | `RobotoSlab.ttf`, from `apache/robotoslab/RobotoSlab[wght].ttf` |
| [Playfair Display](https://fonts.google.com/specimen/Playfair+Display) | 1.203 | The Playfair Display Project Authors | SIL Open Font License 1.1 | `PlayfairDisplay.ttf`, from `ofl/playfairdisplay/PlayfairDisplay[wght].ttf` |
| [Anton](https://fonts.google.com/specimen/Anton) | 2.116 | The Anton Project Authors | SIL Open Font License 1.1 | `Anton.ttf`, from `ofl/anton/Anton-Regular.ttf` |
| [Bangers](https://fonts.google.com/specimen/Bangers) | 2.100 | The Bangers Project Authors | SIL Open Font License 1.1 | `Bangers.ttf`, from `ofl/bangers/Bangers-Regular.ttf` |
| [Pacifico](https://fonts.google.com/specimen/Pacifico) | 3.001 | The Pacifico Project Authors | SIL Open Font License 1.1 | `Pacifico.ttf`, from `ofl/pacifico/Pacifico-Regular.ttf` |
| [Caveat](https://fonts.google.com/specimen/Caveat) | 2.000 | The Caveat Project Authors | SIL Open Font License 1.1 | `Caveat.ttf`, from `ofl/caveat/Caveat[wght].ttf` |
| [Orbitron](https://fonts.google.com/specimen/Orbitron) | 2.001 | The Orbitron Project Authors | SIL Open Font License 1.1 | `Orbitron.ttf`, from `ofl/orbitron/Orbitron[wght].ttf` |
| [Press Start 2P](https://fonts.google.com/specimen/Press+Start+2P) | 3.000 | The Press Start 2P Project Authors | SIL Open Font License 1.1 | `PressStart2P.ttf`, from `ofl/pressstart2p/PressStart2P-Regular.ttf` |
| [JetBrains Mono](https://fonts.google.com/specimen/JetBrains+Mono) | 2.211 | The JetBrains Mono Project Authors | SIL Open Font License 1.1 | `JetBrainsMono.ttf`, from `ofl/jetbrainsmono/JetBrainsMono[wght].ttf` |

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
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | Apache 2.0. Code merged before its version 2.0 is also under MIT |
| [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/) (beautifulsoup4) | MIT |
| [soupsieve](https://github.com/facelessuser/soupsieve) | MIT |
| [CairoSVG](https://github.com/Kozea/CairoSVG) | LGPL 3.0 or later. It brings cairocffi, cssselect2 and tinycss2 (BSD 3-Clause) and defusedxml (PSF 2.0), and draws through the cairo library on the machine |

## Language design

The template language in `captainguild/core/tagscript` follows the behavior of [TagScript](https://github.com/JonSnowbd/TagScript) by JonSnowbd (CC BY 4.0), so existing templates keep working. Its code here is written from scratch.

## Outside services

The bot calls these only when someone uses the matching feature. Each one sees the request come from the machine running the bot.

| Service | Used for | What it receives |
|---|---|---|
| [Twitch Helix API](https://dev.twitch.tv/docs/api/) (`id.twitch.tv`, `api.twitch.tv`) | Live stream alerts | The client ID and secret the server enters, and the channels it follows |
| [YouTube](https://www.youtube.com/) feeds and channel pages | New video alerts, and subscriber counts on counter channels | The channels the server follows, and the channel a counter names, at most once an hour for each |
| [Instagram](https://www.instagram.com/) (`www.instagram.com`) and [TikTok](https://www.tiktok.com/) (`www.tiktok.com`) profile pages | Follower counts on counter channels | The account a counter names, in a request for its public profile page, at most once an hour for each account |
| [GamerPower API](https://www.gamerpower.com/api-read) (`gamerpower.com`) | Free game alerts | Nothing from the server |
| [CheapShark API](https://apidocs.cheapshark.com/) (`cheapshark.com`) | Game deal alerts | Nothing from the server |
| [MEE6 public leaderboards](https://mee6.xyz/) (`mee6.xyz`) | Importing XP, when a server asks for it | The server's ID |
| [The Cat API](https://thecatapi.com/) (`thecatapi.com`), [Cataas](https://cataas.com/) (`cataas.com`), [Dog CEO](https://dog.ceo/) (`dog.ceo`), [RandomDog](https://random.dog/) (`random.dog`), [RandomFox](https://randomfox.ca/) (`randomfox.ca`), [Random-d.uk](https://random-d.uk/) (`random-d.uk`) | `/cute`, which needs no setup | Nothing from the server |
| [Urban Dictionary](https://www.urbandictionary.com/) (`urbandictionary.com`) | `/fun urbandictionary`, which needs no setup | The word the member typed |
| [Fortnite-API](https://fortnite-api.com/) (`fortnite-api.com`) | `/games fortnite`, once the bot's host adds a `FORTNITE_API_KEY` | That key, and the player name and platform the member typed |
| [Anthropic API](https://www.anthropic.com/) (`api.anthropic.com`) | Wiki answers: `/how_do_i`, `/brainstorm`, `/ask`, `/make_model` and answers to new forum posts, once the bot's host adds an `ANTHROPIC_API_KEY` | That key, the member's request or the post's title and first message, and the passages of the server's wiki copies the model reads. For `/ask` the service also searches the web |
| The wikis and documentation sites a server names on its Wiki answers page | The bot's copies of them, which Wiki answers searches | Requests for their pages, their sitemaps and their `robots.txt`, as often as each source's update schedule says |
| [Roblox web APIs](https://create.roblox.com/docs/cloud/legacy/users/v1) (`users.roblox.com`, `friends.roblox.com`, `thumbnails.roblox.com`) | `/games roblox`, which needs no setup. The profile link in its answer goes to `www.roblox.com` | The user name the member typed |
| [pastebin.com](https://pastebin.com/) and [starb.in](https://starb.in/) | A paste link someone gives a tag or embed command | The paste's address |
| [DeepL](https://www.deepl.com/) (`oneshot-free.www.deepl.com`, and `oneshot-pro.www.deepl.com` for a server that enters a DeepL Pro token) | Translations, in servers that switch on translation and conversions | The text to translate, the languages, and that server's DeepL Pro token when it has one |
| The free exchange rates published as `@fawazahmed0/currency-api` (`cdn.jsdelivr.net`, then `latest.currency-api.pages.dev`) | Currency conversions, in servers that switch on translation and conversions | Nothing from the server |
| Any address someone gives the bot | `/fun addemoji` from a link, and a rank card background from a link | That address |
| Any RSS or Atom feed a server adds | RSS feeds, read once a minute while the server has them on | A request for the feed's address, with the ETag and Last-Modified it sent last time |
| [Reddit](https://www.reddit.com/) (`www.reddit.com`, `embed.reddit.com`, `i.redd.it`, `preview.redd.it`, `packaged-media.redd.it`, `v.redd.it`) | Reddit posts, in servers that switch them on: the feed of one followed account a minute, the embed page of a post that has a gallery or a video, and each relayed post's pictures and video | Requests for those addresses, and nothing from the server |
