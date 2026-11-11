Tags, triggers, greetings, level-up messages and any other message a server writes can hold blocks. A block is a small instruction in curly braces. The bot replaces it with a result when the message goes out, so `Welcome, {user}!` greets each person by name.

## How a template runs

A block has a name, and sometimes a parameter in parentheses and a payload after a colon: `{name(parameter):payload}`.

Blocks run from the inside out and left to right, and each result takes the block's place in the text around it. An outer block therefore reads what its inner blocks produced, and a later block can use a variable an earlier one set.

| Example | Result |
|---|---|
| `{upper:{lower:ABC}d}` | `ABCD` |
| `{=(a):1}{=(a):{a}2}{a}` | `12` |

Every complete block runs, even inside the branch of an `if` that isn't taken. To make a block depend on a condition, put the condition inside the block. The second example below shows the other way to do it, which builds the block's name with an `if`.

| Example | Result |
|---|---|
| `{if(1==2):{=(x):set}}{x}` | `set` |
| `{=(x):off}{{if(1==1):=(x)}:on}{x}` | `on` |

A name nothing answers to stays as written, and so does a block that can't work with what it was given, such as math on a word. Spaces and blank lines at the very start and end of the result are removed. Block names ignore capitals. Variable names don't.

| Example | Result |
|---|---|
| `{nothing} {m:one+two}` | `{nothing} {m:one+two}` |
| `{UPPER:a}{=(Name):b}{name}` | `A{name}` |
| `  {=(a):1}hello  ` | `hello` |

Text from outside the template is data. What a member typed, a nickname or a channel's name can fill in a value, but its punctuation can't change what a block does. Someone typing `x==x` can't pass a check against a passphrase, and someone typing `cmd:ban me` can't turn `{{args}}` into a command block. A name built from outside text can only read a variable.

## Where blocks work

Tags and triggers run everything in this reference. In greetings and other messages a server writes, the blocks under Actions and Commands do nothing and leave no text behind, because no one ran them. Embed blocks work everywhere.

Text assigned in a message's text is there for its embed too, so a variable can hold a sentence the embed's title and description both use. An embed's parts run in the order they're shown on the page: author, title, link, description, fields, image, thumbnail, footer.

## What the examples assume

Every example in this reference runs against these values, and an automated test checks each one against the bot, so the results shown are the results you'd get.

| Value | In the examples |
|---|---|
| The person | Display name `Ada`, username `ada`, global name `Ada Lovelace`, ID `111111111111111111`, joined 2025-06-07 08:09:10, account made 2020-02-03 04:05:06 |
| Their roles | `Crew`, position 2, ID `222222222222222222`, color `#5989ab`, plus `@everyone` with the server's ID |
| The target | `Grace`, username `grace`, ID `555555555555555555`, mentioned in the message |
| The server | `Harbor`, ID `333333333333333333`, created 2019-01-01 00:00:00, owned by `owner`, with four members and one bot |
| The channel | `general`, ID `444444444444444444`, topic `Say hello`, slowmode 30, position 3 |
| The role, in role triggers | `Crew`, just added |
| The joined count, in greetings | 8102 |
| The invite, in greetings | Code `harbor`, made by `Grace` (username `grace`) on 2025-12-01 00:00:00 for the channel above, used 12 times of 50, ending 2026-02-01 00:00:00 |
| The text after the name | `one two three` |
| The tag's use count | 42 |
| The time | 2026-01-02 03:04:05 UTC |

## Variables

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{args}` | The text after the tag's name. In a trigger, the whole message. | `{args}` | `one two three` |
| `{message}` | The same text, as a separate variable. Assigning to one leaves the other alone. | `{=(args):x}{args} {message}` | `x one two three` |
| `{1}`, `{2}` and so on | One word of `{message}`. | `{2}` | `two` |
| `{uses}` | How many times the tag has run, counting this time. | `{uses}` | `42` |
| `{unix}` | The time now, in seconds since 1970. | `{unix}` | `1767323045` |
| `{mention}` | Mentions the person using the tag. | `{mention}` | `<@111111111111111111>` |

### Assigning

`{=}`, `{assign}`, `{let}` and `{var}` all do the same thing: they store the payload under the name in the parameter and leave no text. Read it back with the name in braces. A variable can have any name, but a block's name goes to the block first, so give variables names of two letters or more.

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{=(name):value}` | Nothing. Stores the value. | `{=(pet):cat}I have a {pet}` | `I have a cat` |
| `{assign(name):value}` | The same. | `{assign(pet):cat}{pet}` | `cat` |
| `{let(name):value}` | The same. | `{let(pet):cat}{pet}` | `cat` |
| `{var(name):value}` | The same. | `{var(pet):cat}{pet}` | `cat` |

A name can be built from other blocks, and an empty name is allowed. `{=():}` gives `{}` something empty to answer with, which is what makes a block's name that sometimes comes out empty safe to use.

| Example | Result |
|---|---|
| `{=(msg2):second}{=(n):2}{msg{n}}` | `second` |
| `[{=():}{}]` | `[]` |

### Reading part of a variable

Any variable holding text can be read a piece at a time, with a position in parentheses. Pieces are split on spaces, or on whatever the payload says. 1 is the first piece, 0 is the last and -1 is the one before it. A plus sign after the number means that piece onward, and before it means up to that piece. A position past the end gives the whole value, and a span past the end gives nothing.

| Example | Result |
|---|---|
| `{args(1)}` | `one` |
| `{args(0)}` | `three` |
| `{args(-1)}` | `two` |
| `{args(2+)}` | `two three` |
| `{args(+2)}` | `one two` |
| `{=(when):a,b,c}{when(2):,}` | `b` |
| `{args(9)}` | `one two three` |
| `[{args(9+)}]` | `[]` |

## People

`{user}` is the person using the tag. `{target}` is the first person they mention after the tag's name, or the same person when they mention nobody. On their own, both give the display name. In a trigger, the target comes from the message's mentions. In a role trigger, `{user}` is whoever changed the role and `{target}` is the member it changed on. Its require, blacklist, dm and command blocks act for that member.

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{user}` | The display name. | `{user}` | `Ada` |
| `{target}` | The target's display name. | `{target}` | `Grace` |
| `(id)` | The Discord ID. | `{user(id)}` | `111111111111111111` |
| `(name)` | The username. | `{user(name)}` | `ada` |
| `(proper)` | The username, with the old four-digit tag for accounts that still have one. | `{user(proper)}` | `ada` |
| `(nick)` | The display name. | `{target(nick)}` | `Grace` |
| `(mention)` | A mention. | `{target(mention)}` | `<@555555555555555555>` |
| `(avatar)` | A link to the avatar. | `{user(avatar)}` | `https://cdn.example/avatar.png` |
| `(icon)` | The same link. | `{user(icon)}` | `https://cdn.example/avatar.png` |
| `(created_at)` | When the account was made. | `{user(created_at)}` | `2020-02-03 04:05:06` |
| `(joined_at)` | When they joined this server. | `{user(joined_at)}` | `2025-06-07 08:09:10` |
| `(color)` | The color of their highest colored role. `#000000` when they have none. | `{user(color)}` | `#5989ab` |
| `(roleids)` | The IDs of their roles, lowest first, with spaces between. | `{user(roleids)}` | `333333333333333333 222222222222222222` |
| `(position)` | How high their top role sits. `@everyone` is 0. | `{user(position)}` | `2` |
| `(username)` | The username, the same as `(name)`. | `{user(username)}` | `ada` |
| `(global_name)` | The name they show across Discord. Nothing when they set none. | `{user(global_name)}` | `Ada Lovelace` |
| `(profile_name)` | The name on their own profile: the global name, or the username when they set none. A server nickname doesn't change it. | `{user(profile_name)}` | `Ada Lovelace` |
| `(profile_avatar)` | A link to the avatar on their own profile. A server avatar doesn't change it. | `{user(profile_avatar)}` | `https://cdn.example/avatar.png` |
| `(discriminator)` | The old four-digit tag. `0` for an account without one. | `{user(discriminator)}` | `0` |
| `(bot)` | `true` for a bot, `false` for a person. | `{user(bot)}` | `false` |
| `(pending)` | `true` until they pass the server's rules screen, then `false`. | `{user(pending)}` | `false` |
| `(created_ago)` | When the account was made, as a Discord timestamp. Discord shows it as "5 years ago" and keeps it current. | `{user(created_ago)}` | `<t:1580702706:R>` |
| `(joined_ago)` | When they joined this server, the same way. | `{user(joined_ago)}` | `<t:1749283750:R>` |

`(bot)` and `(pending)` are made for conditions, so one message can greet bots and people differently.

| Example | Result |
|---|---|
| `{if({user(bot)}==true):A bot joined.\|Welcome, {user}!}` | `Welcome, Ada!` |

## The server

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{server}` | The name. | `{server}` | `Harbor` |
| `(id)` | The ID. | `{server(id)}` | `333333333333333333` |
| `(name)` | The name. | `{server(name)}` | `Harbor` |
| `(icon)` | A link to the icon, or nothing when there's none. | `[{server(icon)}]` | `[]` |
| `(splash)` | A link to the invite splash picture, or nothing when there's none. | `[{server(splash)}]` | `[]` |
| `(banner)` | A link to the banner, or nothing when there's none. | `[{server(banner)}]` | `[]` |
| `(owner)` | The owner's username. | `{server(owner)}` | `owner` |
| `(members)` | How many members it has. | `{server(members)}` | `4` |
| `(bots)` | How many of them are bots. | `{server(bots)}` | `1` |
| `(humans)` | How many aren't. | `{server(humans)}` | `3` |
| `(roles)` | How many roles it has. | `{server(roles)}` | `2` |
| `(channels)` | How many channels it has. | `{server(channels)}` | `4` |
| `(created_at)` | When it was created. | `{server(created_at)}` | `2019-01-01 00:00:00` |
| `(random)` | The username of a random member. | `{server(random)}` | `ada` or `grace` or `owner` or `bot` |
| `(randomonline)` | Someone from those online. The bot doesn't watch who is online, so this is any member. | `{server(randomonline)}` | `ada` or `grace` or `owner` or `bot` |
| `(randomoffline)` | Someone from those offline, for the same reason any member. | `{server(randomoffline)}` | `ada` or `grace` or `owner` or `bot` |

## The channel

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{channel}` | The name. | `{channel}` | `general` |
| `(id)` | The ID. | `{channel(id)}` | `444444444444444444` |
| `(name)` | The name. | `{channel(name)}` | `general` |
| `(topic)` | The topic. | `{channel(topic)}` | `Say hello` |
| `(slowmode)` | The slowmode delay in seconds. | `{channel(slowmode)}` | `30` |
| `(position)` | Its place in the channel list. | `{channel(position)}` | `3` |
| `(mention)` | A link to it. | `{channel(mention)}` | `<#444444444444444444>` |

## The role, in role triggers

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{role}` | The name of the role that changed. | `{role}` | `Crew` |
| `(id)` | Its ID. | `{role(id)}` | `222222222222222222` |
| `(name)` | Its name. | `{role(name)}` | `Crew` |
| `(mention)` | A mention. | `{role(mention)}` | `<@&222222222222222222>` |
| `(position)` | How high it sits. | `{role(position)}` | `2` |
| `(is_added)` | `true` if it was added, `false` if it was removed. | `{role(is_added)}` | `true` |
| `(has_elevated_perms)` | `true` if it can moderate or manage the server. | `{role(has_elevated_perms)}` | `false` |

## Greetings

The welcome, goodbye and ban messages, the join DM and the welcome image read two more values.

`{joined}` counts everyone who has ever joined the server. It goes up by one with each join and never comes down when someone leaves, so it suits "you are our 8,102nd member". A welcome shows the count as it stood when that member joined. The Greetings page shows the count and lets you set it.

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{joined}` | How many people have joined so far. | `{joined}` | `8102` |

`{invite}` is the invite the member joined through, in the welcome message, the join DM and the welcome image. The bot needs Manage Server to read invites. When it can't tell which invite was used, the code and the inviter read `Unknown`, the numbers read `0` and the dates give nothing. A goodbye or a ban message reads it that way too. A server's vanity link counts as an invite with no inviter.

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{invite}` | The invite's code. | `{invite}` | `harbor` |
| `(code)` | The code. | `{invite(code)}` | `harbor` |
| `(uses)` | How many times it has been used, this join included. | `{invite(uses)}` | `12` |
| `(max_uses)` | How many uses it allows. `0` means no limit. | `{invite(max_uses)}` | `50` |
| `(max_age)` | How long it lasts, in seconds. `0` means it never ends. | `{invite(max_age)}` | `5356800` |
| `(temporary)` | `true` if it gives temporary membership, otherwise `false`. | `{invite(temporary)}` | `false` |
| `(channel)` | The channel it leads to, as an ID. | `{invite(channel)}` | `444444444444444444` |
| `(created_at)` | When it was made. | `{invite(created_at)}` | `2025-12-01 00:00:00` |
| `(expires_at)` | When it ends, or nothing when it never does. | `{invite(expires_at)}` | `2026-02-01 00:00:00` |
| `(created_ago)` | When it was made, as a Discord timestamp such as "1 month ago". | `{invite(created_ago)}` | `<t:1764547200:R>` |
| `(expires_in)` | When it ends, as a Discord timestamp such as "in 30 days". | `{invite(expires_in)}` | `<t:1769904000:R>` |
| `(inviter)` | The name of whoever made it, as their own profile shows it: the global name, or the username when they set none. | `{invite(inviter)}` | `Grace` |
| `(inviter_username)` | Their username. | `{invite(inviter_username)}` | `grace` |
| `(inviter_global_name)` | Their global name. Nothing when they set none. | `{invite(inviter_global_name)}` | `Grace` |
| `(inviter_mention)` | A mention of whoever made it. It doesn't ping them. | `{invite(inviter_mention)}` | `<@555555555555555555>` |
| `(inviter_id)` | Their ID, or `0` when there's no inviter. | `{invite(inviter_id)}` | `555555555555555555` |

## Conditions

A condition compares two sides with an operator. `==` and `!=` compare text exactly, capitals included. The others compare numbers.

| Operator | True when | Example | Result |
|---|---|---|---|
| `==` | The sides are the same text. | `{if(cat==cat):yes\|no}` | `yes` |
| `!=` | They differ. | `{if(tree!=car):yes\|no}` | `yes` |
| `>` | The left number is larger. | `{if(15>20):yes\|no}` | `no` |
| `>=` | Larger or equal. | `{if(17>=17):yes\|no}` | `yes` |
| `<` | The left number is smaller. | `{if(25<5):yes\|no}` | `no` |
| `<=` | Smaller or equal. | `{if(1<=1):yes\|no}` | `yes` |

In a block with an else, a pipe divides the payload: what's before it comes out when the condition holds, and what's after it when it doesn't. With no pipe, a condition that fails gives nothing. With two or more pipes, the whole payload comes out when it holds.

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{if(condition):then\|else}` | The then part or the else part. Stays as written when numbers are compared and one side isn't a number. | `{if({1}==one):first\|other}` | `first` |
| `{any(a\|b):then\|else}` | The then part when at least one condition holds. | `{any(1==2\|2==2):yes\|no}` | `yes` |
| `{or(a\|b):then\|else}` | The same. | `{or(1==2\|2==3):yes\|no}` | `no` |
| `{all(a\|b):then\|else}` | The then part when every condition holds. A side that isn't a number counts as not holding. | `{all(1==1\|2==2):yes\|no}` | `yes` |
| `{and(a\|b):then\|else}` | The same. | `{and(1==1\|2==3):yes\|no}` | `no` |
| `{break(condition):text}` | Nothing here, but when the condition holds the whole output becomes the text. Blocks after it still run. | `{break(1==1):Only this.} Not this.` | `Only this.` |
| `{short(condition):text}` | The same. | `{short(1==2):Only this.} This stays.` | `This stays.` |
| `{shortcircuit(condition):text}` | The same. | `{shortcircuit(1==1):Only this.} Not this.` | `Only this.` |
| `{stop(condition):text}` | When the condition holds, the text, and everything after it in that piece of text is dropped and no later block in it runs. An embed is a separate piece of text and still runs. Otherwise nothing. | `before {stop(1==1):end} after` | `before end` |
| `{halt(condition):text}` | The same. | `a{halt(1==2):x}b` | `ab` |

## Lists

`{list}`, `{cycle}` and `{random}` read a list split on tildes when there are any, otherwise on commas. Use tildes when an element holds a comma. Counting starts at 0, and a negative number counts back from the end.

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{list(n):a,b}` | Element n, or nothing when n is past the end. A message when n isn't a number. | `{list(3):red~green~blue~pink}` | `pink` |
| `{cycle(n):a,b}` | Element n, going round again past the end. A message when n isn't a number. | `{cycle(7):red,green,blue}` | `green` |
| `{index(word):words}` | Where the first copy of a word sits among words split on spaces, or -1. | `{index(blue):red green blue}` | `2` |
| `{in(text):text}` | `true` if the parameter appears anywhere in the payload, otherwise `false`. Capitals count. | `{in(spam):No spamming}` | `true` |
| `{contains(word):words}` | `true` if the parameter is one of the words, otherwise `false`. | `{contains(spam):No spamming}` | `false` |

## Text

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{ord:number}` | The number with its ordinal ending. Thousands get commas. Anything else comes back unchanged. | `{ord:8102}` | `8,102nd` |
| `{ord(format):number}` | The same, with the digits written in one of the number formats below. | `{ord(plain):8102}` | `8102nd` |
| `{num:number}` | The number with commas between its thousands. Anything that isn't a whole number stays as written. | `{num:1234567}` | `1,234,567` |
| `{num(format):number}` | The number in one of the number formats below. | `{num(dots):1234567}` | `1.234.567` |
| `{lower:text}` | The text in lower case. | `{lower:Tall Ship}` | `tall ship` |
| `{upper:text}` | The text in upper case. | `{upper:tall ship}` | `TALL SHIP` |
| `{title:text}` | The text with the first letter of each word in upper case. The other letters stay as written. | `{title:tall ship}` | `Tall Ship` |
| `{join(with):text}` | The text with each space replaced. The parameter can be empty but has to be there. | `{join(_):tall ship}` | `tall_ship` |
| `{replace(from,to):text}` | The text with every copy of one string replaced by another. An empty first string puts the second around every character. | `{replace(oo,ee):Goose Tooth}` | `Geese Teeth` |
| `{urlencode:text}` | The text made safe for a link. | `{urlencode:a b, c?}` | `a%20b%2C%20c%3F` |
| `{urlencode(+):text}` | The same, with plus signs for spaces. | `{urlencode(+):a b, c?}` | `a+b%2C+c%3F` |
| `{substr(from-to):text}` | The characters from one position up to another, counting from 0. | `{substr(1-3):abcde}` | `bc` |
| `{substring(from):text}` | The characters from a position to the end. | `{substring(2):abcde}` | `cde` |

### Number formats

`{ord}` and `{num}` take a format in parentheses. With none, both use `commas`.

| Format | How it writes a number | Example | Result |
|---|---|---|---|
| `plain` | Digits alone. | `{num(plain):1234567}` | `1234567` |
| `commas` | A comma between thousands. | `{num(commas):1234567}` | `1,234,567` |
| `dots` | A period between thousands. | `{num(dots):1234567}` | `1.234.567` |
| `indian` | The last three digits, then pairs. | `{num(indian):1234567}` | `12,34,567` |
| `arabic` | Arabic-Indic digits and their separator. | `{num(arabic):1234567}` | `١٬٢٣٤٬٥٦٧` |

A block's result can go straight into another, so a member count reads well in a welcome message.

| Example | Result |
|---|---|
| `{ord(plain):{server(members)}}` | `4th` |
| `{ord(dots):{joined}}` | `8.102nd` |
| `{num(indian):{joined}}` | `8,102` |

## Math

`{math}`, `{m}`, `{+}` and `{calc}` all work out the expression in their payload, in the usual order of operations. Numbers are decimal, so `{m:2+2}` gives `4.0`. Only `trunc`, `round` and `sgn` give whole numbers. An expression that can't be worked out stays as written.

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{math:expression}` | The result. | `{math:8+9}` | `17.0` |
| `{m:expression}` | The same. | `{m:5*6}` | `30.0` |
| `{+:expression}` | The same. | `{+:8/2}` | `4.0` |
| `{calc:expression}` | The same. | `{calc:7-4}` | `3.0` |

| Written | What it does | Example | Result |
|---|---|---|---|
| `a+b`, `a-b`, `a*b`, `a/b` | Add, subtract, multiply, divide. | `{m:1+2*3}` | `7.0` |
| `a%b` | The remainder. | `{m:7%4}` | `3.0` |
| `a^b` | To the power of. Powers group from the right. | `{m:2^3^2}` | `512.0` |
| `abs(x)` | Without its sign. | `{m:abs(-3)}` | `3.0` |
| `round(x)` | To the nearest whole number. Halves go to the even neighbor. | `{m:round(7.8)}` | `8` |
| `trunc(x)` | The decimals chopped off. | `{m:trunc(8.9)}` | `8` |
| `sin(x)`, `cos(x)`, `tan(x)` | Trigonometry in radians. | `{m:cos(pi)}` | `-1.0` |
| `sinh(x)`, `cosh(x)`, `tanh(x)` | Hyperbolic versions. | `{m:cosh(0)}` | `1.0` |
| `exp(x)` | e to the power of x. | `{m:exp(0)}` | `1.0` |
| `sgn(x)` | 1, 0 or -1 by the sign of x. | `{m:sgn(-5)}` | `-1` |
| `log(x)` | Logarithm, base 10. | `{m:log(1000)}` | `3.0` |
| `ln(x)` | Natural logarithm. | `{m:ln(e)}` | `1.0` |
| `log2(x)` | Logarithm, base 2. | `{m:log2(8)}` | `3.0` |
| `pi` | 3.141592653589793 | `{m:pi}` | `3.141592653589793` |
| `e` | 2.718281828459045 | `{m:e}` | `2.718281828459045` |

## Random

`{random}`, `{rand}` and `{#}` pick from a list split on tildes or commas. An element written `4|text` counts as four copies of that element. Give a seed in the parameter and the same seed picks the same place every time, which is how a tag can give each person a fixed result: `{random({user(id)}):Mage,Warrior}`. The same seed on a list of another length can land on a different element.

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{random:a,b}` | A random element. | `{random:red,green}` | `red` or `green` |
| `{random(seed):a,b}` | The element this seed locks to. | `{random(7):red,green,blue}` | `blue` |
| `{rand(seed):a~b}` | The same. | `{rand(7):red~green~blue}` | `blue` |
| `{#(seed):a,b}` | The same. | `{#(7):red,green,blue}` | `blue` |
| `{random:9\|a,1\|b}` | A weighted pick. | `{random(5):9\|common,1\|rare}` | `common` |
| `{range:low-high}` | A whole number in the range, both ends included. Takes a seed too. | `{range(7):1-100}` | `92` |
| `{rangef:low-high}` | A number with one decimal in the range. Takes a seed too. | `{rangef(7):1-2}` | `1.9` |
| `{5050:text}` | The text half the time, nothing the other half. | `{5050:Hi}` | `Hi` or nothing |
| `{50:text}` | The same. | `{50:Hi}` | `Hi` or nothing |
| `{?:text}` | The same. | `{?:Hi}` | `Hi` or nothing |

## Time

All times are UTC. A date is written `yyyy-mm-dd HH:MM:SS`, and periods work in place of the colons. A number means seconds since 1970.

| Block | What it gives | Example | Result |
|---|---|---|---|
| `{strf:format}` | The time now, in a format. | `{strf:%A %B %-d, %Y}` | `Friday January 2, 2026` |
| `{strf(date):format}` | A given time, in a format. | `{strf(1999-12-31 23.59.59):%A}` | `Friday` |
| `{td:date}` | How far away a date is from now, in calendar terms, largest three units. | `{td:2026-03-01 00:00:00}` | `1 month, 3 weeks and 5 days` |
| `{td(date):date}` | How far apart two dates are. | `{td(2026-01-01 00:00:00):2026-01-03 12:00:00}` | `2 days and 12 hours` |
| `{since:date}` | How far away a date is from now, as its one largest unit. | `{since:{user(created_at)}}` | `5 years` |
| `{since(date):date}` | How far apart two dates are, as one unit. | `{since(2026-01-01 00:00:00):2026-01-03 12:00:00}` | `2 days` |

`{since}` gives plain words that stay as they were when the message went out. Use it on a welcome image, where a Discord timestamp can't be drawn. In a message, `{user(created_ago)}` and `{user(joined_ago)}` give a timestamp Discord keeps current.

The format codes are the ones on Linux, in English, and the same on every server.

| Code | Gives | Example | Result |
|---|---|---|---|
| `%Y` | The year. | `{strf:%Y}` | `2026` |
| `%y` | The year's last two digits. | `{strf:%y}` | `26` |
| `%m` | The month as a number. | `{strf:%m}` | `01` |
| `%B` | The month's name. | `{strf:%B}` | `January` |
| `%b` | The month's short name. | `{strf:%b}` | `Jan` |
| `%d` | The day of the month. | `{strf:%d}` | `02` |
| `%-d` | The day, with no leading zero. A dash works with any number. | `{strf:%-d}` | `2` |
| `%e` | The day, padded with a space. | `{strf:[%e]}` | `[ 2]` |
| `%A` | The weekday's name. | `{strf:%A}` | `Friday` |
| `%a` | The weekday's short name. | `{strf:%a}` | `Fri` |
| `%H` | The hour, 24-hour clock. | `{strf:%H}` | `03` |
| `%I` | The hour, 12-hour clock. | `{strf:%I}` | `03` |
| `%M` | The minute. | `{strf:%M}` | `04` |
| `%S` | The second. | `{strf:%S}` | `05` |
| `%p` | AM or PM. | `{strf:%-I:%M %p}` | `3:04 AM` |
| `%j` | The day of the year. | `{strf:%j}` | `002` |
| `%s` | Seconds since 1970. | `{strf:%s}` | `1767323045` |
| `%F` | The same as `%Y-%m-%d`. | `{strf:%F}` | `2026-01-02` |
| `%T` | The same as `%H:%M:%S`. | `{strf:%T}` | `03:04:05` |
| `%x` | The date with slashes. | `{strf:%x}` | `2026/01/02` |
| `%X` | The same as `%H:%M:%S`. | `{strf:%X}` | `03:04:05` |
| `%c` | The date and time written out. | `{strf:%c}` | `Fri Jan  2 03:04:05 2026` |
| `%^a` | A caret puts any code in capitals. | `{strf:%^a}` | `FRI` |
| `%%` | A percent sign. | `{strf:100%%}` | `100%` |

## Actions

These blocks leave no text behind. They ask the bot to do something when a tag or trigger runs. The order is fixed: require and blacklist decide first, then the output goes out, then reactions, then commands, and the delete comes last.

| Block | What it does | Example | Result |
|---|---|---|---|
| `{delete}` | Deletes the message that set it off. | `{delete}Gone` | `Gone` |
| `{del}` | The same. | `{del}Gone` | `Gone` |
| `{silent}` | Keeps commands in command blocks from posting. Their errors still show. | `{silent}Done` | `Done` |
| `{silence}` | The same. | `{silence}Done` | `Done` |
| `{override}` | Commands in command blocks run as if the person held the bot manager role. Role order still applies, and Ban Members stays out of reach. Only counts on a trusted tag or trigger. | `{override}Done` | `Done` |
| `{dm}` | Sends the output to the person who ran it, by DM. Nobody else can be the target. | `{dm}Secret` | `Secret` |
| `{redirect:channel}` | Sends the output to a channel in this server, named by name, ID or mention. Whoever owns the tag or wrote the trigger has to be able to post there, unless it's trusted. With both, the last of this and `{dm}` wins. | `{redirect:general}Moved` | `Moved` |
| `{require(message):list}` | Only runs for someone with a listed role, or in a listed channel. Entries are names, IDs or mentions with commas between. If it stops someone, they see the message, or a warning reaction when there's none. | `{require(Crew only):Crew}Welcome` | `Welcome` |
| `{blacklist(message):list}` | Refuses someone with a listed role, or in a listed channel. | `{blacklist(Not here):Staff}Welcome` | `Welcome` |
| `{react:emoji}` | Reacts to the output. Spaces go between emoji. Paste the emoji itself, or write a server emoji as `:name:`. | `{react:👍}Done` | `Done` |
| `{reactu:emoji}` | Reacts to the message that set it off. | `{reactu:👍}Done` | `Done` |

### Embed blocks

These change the first embed the message has, and a message with no embed gets one.

| Block | What it does | Example | Result |
|---|---|---|---|
| `{embed(title):text}` | Sets the title. | `{embed(title):Hello}` | nothing |
| `{embed(description):text}` | Sets the description. | `{embed(description):Hello}` | nothing |
| `{embed(url):link}` | Sets the title's link. It has to start with http:// or https://. | `{embed(url):https://example.com}` | nothing |
| `{embed(color):#hex}` | Sets the color. | `{embed(color):#5989ab}` | nothing |
| `{embed(timestamp):now}` | Sets the timestamp to now, the only value it takes. | `{embed(timestamp):now}` | nothing |

## Commands

A command block runs one of the bot's commands, written as you'd write it after the prefix, short names included. The output of the tag goes first. Command blocks can't run tags or the tag and reaction role commands, and each tag runs at most five. If the person can't use the command, they get the same error the command would give them.

A tag or trigger starts out untrusted. A tag's command blocks then only run for its owner, nothing in its output pings anyone but the person it moves to the bot channel for, and automod reads what it's about to post. A trigger's command blocks don't run at all, and it only reads and posts in channels whoever wrote it can, so it goes quiet if they leave. The server owner or an Administrator can mark either one as trusted on its page, and it stops being trusted if anyone else changes its text or who can use it. Anyone else who changes a tag's text also becomes its owner, so its command blocks run only for whoever wrote the words.

What a trusted tag or a trigger posts can ping the person who set it off, the person they named, and anyone its own text mentions. Text a member typed pings nobody else.

| Block | What it does | Example | Result |
|---|---|---|---|
| `{command:text}` | Runs a command with the permissions of the person using the tag or setting off the trigger. It needs a trusted tag or trigger, or that person to own the tag. | `{command:ping}` | nothing |
| `{cmd:text}` | The same. | `{cmd:ping}` | nothing |
| `{c:text}` | The same. | `{c:ping}` | nothing |
| `{cmdAdmin:text}` | On a trusted tag or trigger, runs a command as the bot, with the bot's permissions and place in the role list. Anywhere else it's the same as `{command:text}`. | `{cmdAdmin:ping}` | nothing |

Since every complete block runs, an `if` around a command block doesn't stop it. Put the condition inside, or build the block's name with an `if` as below.

| Example | Result |
|---|---|
| `{cmd:{if({1}==one):ping\|help}}` | nothing |
| `{=():}{{if({1}==nine):cmd:ping}}` | nothing |

## A worked example

This tag greets whoever is mentioned, in an embed, using a random color for anyone whose color is the default, and gives them a role. Its text comes out empty, because every line is a block that leaves nothing.

```template
{=(hello):Welcome aboard, {target(mention)}. You're part of {server} now.}
{=(oops):Mention the person you want to greet.}
{embed(description):{if({user(id)}!={target(id)}):{hello}|{oops}}}
{=(digits):0,1,2,3,4,5,6,7,8,9,A,B,C,D,E,F}
{=(shade):#{#:{digits}}{#:{digits}}{#:{digits}}{#:{digits}}{#:{digits}}{#:{digits}}}
{embed(color):{if({target(color)}==#000000):{shade}|{target(color)}}}
{=():}
{{if({user(id)}!={target(id)}):cmd:role add {target(id)} Crew}}
{silent}
```

The next tag reads a small table of holidays. Each variable holds three values with pipes between them, and the name comes from what the person typed.

```template
{=(one):Christmas|December 25|a tree}
{=(two):Valentine's Day|February 14|a heart}
{any({1}==one|{1}==two):{{1}(1):|} falls on {{1}(2):|}. Look for {{1}(3):|}.}
```

```result
Christmas falls on December 25. Look for a tree.
```

## Limits

No template can hang the bot or fill the channel. A run that goes past a limit stops: a tag or trigger then posts nothing and does nothing, and other messages go out with the template left as written.

| What | Limit |
|---|---|
| Characters in a tag or trigger's text | 25,000 |
| Characters one block can produce | 100,000 |
| Characters handled in one run | 8,000,000 |
| Blocks in one template | 20,000 |
| Time for one run | 0.25 seconds |
| Command blocks that run per tag | 5 |
| Emoji per tag, for each of react and reactu | 5 |
| Characters in a math expression | 5,000 |
| How deep a math expression can nest | 100 levels |
| Size of a whole number in math | 4,096 bits |

A server's tags, its triggers and its other messages each have 5 seconds of processing time a minute, and one person has 1 second a minute across all of them. Past either, a tag or trigger does nothing until the next minute, a tag typed in chat gets a warning reaction, and other messages go out with the template left as written.

A tag or trigger that uses its whole 0.25 seconds is stopped for 10 minutes. It stops for everyone when its owner ran it, or once three different people have run it that far. Otherwise only the same text after its name stays stopped. A tag's owner gets a DM and a trigger leaves a note for staff. Changing the words turns it back on.

What comes out gets cut to what Discord accepts: 2,000 characters of text, 4,096 for an embed's description, 6,000 across a message's embeds. A link that doesn't come out as a link is dropped, and so is an embed field left empty, because Discord refuses a whole message over either.
