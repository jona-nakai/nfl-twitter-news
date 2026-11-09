# NFL news Discord bot

Watches trusted NFL reporters on X (Schefter, Rapoport, Pelissero, Fowler, Schultz) and reposts their news into a Discord server. An OpenAI model reads each post and decides:

- **whether it's news at all.** Promos, opinions, highlights and fantasy advice are skipped.
- **whether it's already been posted.** The first reporter to break a story wins. Later reports of the same news are dropped, but real new developments still get through (rumor → done deal, "feared ACL" → MRI confirms).
- **which channels it goes to:** `#breaking-news`, `#transactions`, `#injuries`, `#discipline`, `#rumors`, and `#all-news`, which gets every story plus league-wide news that fits no topic channel (ownership, rule changes). Breaking news also goes to its topic channel. Topic channels take smaller news too.

The cheap GPT-6 Luna reads every post on its own, without searching the web. Breaking news is decided only by the reporters' own labels (below), never by Luna's sense of how big a story is: its knowledge of who's a star now is out of date.

Reporters flag big news themselves: "BREAKING", "Bombshell", "Blockbuster", "Splash", "Stunner", "Huge news", "Wow", 🚨 and similar wording, or a BREAKING NEWS graphic on the post's image. Routine framing like "Sources:" or "Here we go" doesn't count. The wording alone decides: a labeled rumor goes to `#breaking-news` and `#rumors`. An unlabeled post never goes to `#breaking-news`, however big, but it still goes to its topic channel. Luna reads the attached image (at low resolution, about $0.03 per 1,000 images) only when the text has no label. If a story was first posted without a label and a later report of it carries one, the bot moves the story into `#breaking-news`. Only breaking-news subscribers who weren't already pinged for it get pinged.

Members choose which channels ping them by reacting in `#subscribe`. **Each member is pinged at most once per story**, in the first channel they follow that the story lands in (order: Breaking → Transactions → Injuries → Rumors).

## Setup

You need three accounts and their keys.

### 1. Discord bot

1. Go to <https://discord.com/developers/applications>, click **New Application**, and name it.
2. **Bot** tab → **Reset Token** → copy it into `DISCORD_TOKEN` in `.env`.
3. On the same page, under **Privileged Gateway Intents**, turn on **Server Members Intent** and save.
4. Create your Discord server if you haven't yet.
5. Run `uv run nflnews invite-url`, open the link, and add the bot to your server. The link already requests the permissions the bot needs.

On first start the bot creates the `NFL News` category, the channels, the subscription roles, and the `#subscribe` panel. Channels and roles that already exist with the same names get reused. After that you can rename or move anything you like.

### 2. X API

1. Go to <https://console.x.com>, create a project and an app, and add a payment method plus some credits. The API is pay-per-use: about **$0.005 per post read**.
2. Under the app's **Keys and tokens**, copy the **Bearer Token** into `X_BEARER_TOKEN`.

### 3. OpenAI

Create a key at <https://platform.openai.com/api-keys> and put it in `OPENAI_API_KEY`. The model is set in `.env` with `OPENAI_MODEL` (default `gpt-6-luna`).

### Run it

```bash
cp .env.example .env        # then fill in the three keys
uv sync

# Try the LLM on its own first:
uv run nflnews classify "The Cowboys are trading Micah Parsons to the Packers, sources tell ESPN." --author AdamSchefter

uv run nflnews              # run the bot
uv run pytest               # tests
```

The bot has to stay running to catch news. Run it on an always-on machine or a small VPS. One way to do that with Docker:

```bash
docker build -t nflnews .
docker run -d --name nflnews --restart unless-stopped --env-file .env -v nflnews-data:/data nflnews
```

## In Discord

- **`#subscribe`**: react with 🚨 🔁 🏥 👀 to get pinged for that channel, and remove the reaction to stop. Every channel stays visible to everyone; reactions only control pings. Reactions are the source of truth: on startup the bot syncs everyone's roles to their reactions, which also catches changes made while it was offline.
- **`#newsbot-log`** (admins only): one line per post explaining what the bot did and why. Use it to tune the bot.
- **`#api-credits`** (admins only): X and OpenAI credits left, updated every 2 minutes. X's balance comes straight from X. OpenAI has no balance API, so tell the bot your balance once with **`/newsbot openai-balance dollars:<amount>`** (from your OpenAI billing page) and it counts down using its own spend estimate.
- **`/newsbot status`**: shows connection state, the queue, and the last 24 hours of activity.
- **`/newsbot simulate text:<post> [author] [post:True]`**: runs a made-up post through the classifier. With `post:True` it's actually posted, pings included, which is handy for testing pings end to end.

## Tuning

- **What counts as a breaking label, which channel things go to, what's a duplicate:** edit `src/nflnews/prompt.md`. It's re-read for every post, so no restart is needed.
- **Accounts:** set `X_ACCOUNTS` in `.env`, then restart.
- **Cost vs. judgment:** `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT` (`low` by default), and `OPENAI_WEB_SEARCH`.
- **Claude models:** any model name starting with `claude-` (for example `claude-haiku-5-5`) is sent to Anthropic instead of OpenAI and needs `ANTHROPIC_API_KEY`. Web search is OpenAI-only.

## Comparing models

`evals/cases.json` holds hand-labeled posts: each one lists the channels it should land in, and `[]` means it shouldn't be posted. Score models on them:

```bash
uv run nflnews eval                                   # Luna, Haiku and Sol
uv run nflnews eval gpt-6-luna --effort medium --repeat 3 --only trade-talks,stat-line
```

It prints accuracy, breaking-news mistakes (false alarms ping everyone), cost per 1,000 posts, and latency, followed by each mistake with the model's reason. Full details are saved in `evals/results/`. A full run of all three models costs about $0.05. When the live bot gets something wrong, add that post to `cases.json` so future model and prompt changes are tested against it.

## How the once-per-story pings work

Discord role mentions can't express "people with role A but not role B", so the bot keeps two kinds of roles:

- **Subscription roles**, which members see: `Breaking News`, `Transactions`, `Injuries`, `Rumors`.
- **63 hidden "combo" roles** (one per possible set of the 6 channels), all named `🔔` so the ping line under a story stays short. Each member gets exactly the one that matches their subscriptions, and the bot keeps it in sync.

The first channel a story lands in pings its subscription role. Each later channel pings only the combo roles that don't include an earlier channel. So someone following Breaking + Injuries gets pinged in `#breaking-news` and not again in `#injuries`, while someone following only Injuries gets pinged in `#injuries`.

In **Server Settings → Roles**, keep the bot's own role above these roles, or it can't assign them.
