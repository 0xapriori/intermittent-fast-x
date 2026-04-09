# twitter-digest

A local macOS tool that reads curated X (Twitter) Lists via RSS, asks Claude
to synthesize what actually happened into themed sections with real external
source links, and emails you a plain-text markdown digest on a schedule.

**Zero API cost when used with a Claude Max subscription.** The script shells
out to the `claude -p` CLI (non-interactive Claude Code), which authenticates
via your Max OAuth and uses Claude's built-in `WebSearch` / `WebFetch` tools
to find the real stories behind each tweet — not the $10-per-1k Anthropic
Messages API web_search add-on.

---

## Why this exists

If you follow crypto, AI, or any fast-moving tech industry on X, most of the
signal is time-expensive to extract:

- Most of your timeline is retweets, memes, and engagement bait.
- Substantive tweets usually reference a story without linking it.
- X's free API no longer gives you a home timeline.
- "Official" RSS options have all been killed.
- You don't want to open x.com 20 times a day.

This tool solves the last mile: give it your private X Lists via rss.app,
it collects what's new since the last run, lets Claude research the real
sources, and produces a curated digest you can read in your inbox in 90
seconds per run.

The output reads more like a Substack roundup than a Twitter scroll, with
every bullet grounded in a real external link (CoinDesk, GitHub, an official
announcement, etc.) — never a link back to x.com.

---

## Example output

```markdown
## Ethereum

- **ETH staking hits a new all-time high** — ~32% of all ETH is now locked
  in staking, up from ~30% in February. Institutional participation via
  treasury firms and ETFs continues to drive the increase, with validator
  exit queues near historic lows. [Staking Rewards](https://www.stakingrewards.com/asset/ethereum-2-0/analytics)

- **Monad Foundation launches a dedicated device subsidy program**, covering
  the cost of signing laptops for validators. Multiple prominent accounts
  highlighted Monad's strategy of skipping big incentive programs for vanity
  metrics in favor of infrastructure-first growth. [Announcement](https://example.com)

## Solana

- **Manifest launches on-chain options markets** — calls and puts on any
  token, listed on the orderbook and traded P2P trustlessly. [Manifest](https://example.com)

- **Jupiter ships a new Developer Platform** with unified API keys, real-time
  analytics, and usage dashboards. Every Jupiter API (swap, limit orders,
  DCA, perps) is accessible via a single key. [developers.jup.ag](https://example.com)

## AI

- **Anthropic** launched **Claude Managed Agents** in public beta — a
  performance-tuned agent harness with sandboxed execution, auth, and
  checkpointing. Early adopters include Notion, Rakuten, and Asana. [Announcement](https://example.com)
```

---

## Architecture

```
                    ┌──────────────────┐
                    │ rss.app feeds    │  one per X List
                    │  (public URLs)   │
                    └────────┬─────────┘
                             │ HTTPS
                             ▼
┌─────────────┐    ┌──────────────────┐    ┌──────────────────┐
│   launchd   │───▶│  python digest.py│───▶│ trafilatura      │
│ cron (Mac)  │    │                  │    │ article extract  │
└─────────────┘    └────────┬─────────┘    └──────────────────┘
                            │
                            │ stdin (prompt)
                            ▼
                   ┌──────────────────┐
                   │   claude -p      │  Opus 4.6 via Max
                   │  WebSearch /     │  OAuth. No API key.
                   │   WebFetch       │
                   └────────┬─────────┘
                            │ stdout (markdown)
                            ▼
                   ┌──────────────────┐
                   │  Gmail SMTP      │
                   │  (app password   │
                   │   in keychain)   │
                   └──────────────────┘
```

### Data flow per run

1. **Fetch** every configured rss.app feed.
2. **Dedupe** entries against `state.json` (a ring buffer of recently-seen
   GUIDs).
3. **Extract URLs** from each tweet's HTML description.
4. **Pre-fetch** non-X links concurrently, run HTML through `trafilatura`
   to get clean article text.
5. **Build prompt** with tweets + inline article excerpts + mandatory section
   scaffolding + exclusion rules.
6. **Invoke `claude -p`** with `--allowedTools WebSearch,WebFetch`. Claude
   reads the prompt via stdin, does its own research, and writes markdown to
   stdout.
7. **Strip preamble** (belt-and-suspenders cleanup in case the model ignores
   the "no intro text" instruction).
8. **Render** a markdown email body with a header + Claude's content +
   footer with run metadata.
9. **Send** via Gmail SMTP on port 465 using an app password read from
   macOS keychain.
10. **Persist** the new GUIDs to `state.json` so they don't get summarized
    again next run.

---

## Prerequisites

- **macOS.** Uses `launchd` and the macOS keychain via `security(1)`. Porting
  to Linux/Windows means replacing the scheduler and the keychain layer.
- **Python 3.10+.**
- **Claude Code CLI** (`claude`), logged in to a Claude Max account.
  Install from [claude.com/download](https://claude.com/download).
  After install, run `claude` once interactively and log in. That's it — no
  API key needed in this project.
- **A Gmail account with 2FA enabled** (to generate an app password).
- **An [rss.app](https://rss.app/) account** (free tier = 2 feeds; paid
  tiers for more). You'll create one feed per X List you want to track.

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/YOUR_USER/twitter-digest.git
cd twitter-digest
python3 -m pip install --user -r requirements.txt
```

### 2. Create your config

```bash
cp config.example.json config.json
```

Edit `config.json`:

- **`feeds`**: one entry per X List. For each list, sign in to rss.app,
  paste the list URL (e.g. `https://x.com/i/lists/123456789`), generate a
  feed, and copy the URL of the form `https://rss.app/feeds/<id>.xml`.
- **`email.from`** / **`email.to`**: your Gmail sender and destination.
  Can be the same address (send to yourself).
- **`sections.mandatory`**: the fixed sections every digest will include,
  even if empty. Default: Ethereum, Solana, AI.
- **`sections.optional`**: sections that appear only when there's
  substantive content (default: Bitcoin).
- **`sections.exclusions`**: topics Claude should drop from output
  (default: politics, price speculation, personal drama).

### 3. Gmail app password

App passwords require 2FA. Enable it at
[myaccount.google.com/security](https://myaccount.google.com/security)
if you haven't, then:

1. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
2. Name: `twitter-digest` → **Create**.
3. Copy the 16-character password.

Store it in the macOS keychain (the command below prompts for the password
interactively — your input won't be echoed):

```bash
security add-generic-password -U -a "$USER" -s "twitter-digest-gmail" -w
```

Verify it's stored:

```bash
security find-generic-password -a "$USER" -s "twitter-digest-gmail" >/dev/null \
  && echo "gmail: OK"
```

(The service name is configurable in `config.json` via
`gmail_keychain_service` if you want a different keychain identifier.)

### 4. Run a manual test

```bash
python3 digest.py
```

Or with a limit to control the volume of the first run:

```bash
TW_LIMIT=25 python3 digest.py
```

You should see a run log like:

```
[2026-04-08 18:51:19] digest run start · 6 feed(s)
[2026-04-08 18:51:19] state has 0 seen ids
[2026-04-08 18:51:19]   ✓ Ethereum: 25 items
...
[2026-04-08 18:51:19] 25 new items after dedup
[2026-04-08 18:51:19]   invoking: claude -p --model claude-opus-4-6 --allowedTools WebSearch,WebFetch
[2026-04-08 18:54:35] summary generated (6042 chars) · duration=195.8s
[2026-04-08 18:54:36] email sent: [tw digest] 25 · ...
```

Check your inbox. If the email arrived, you're done. Check
`last-digest.md` in the project directory for the exact content that was
sent.

### 5. Install the scheduler (optional)

If you want it to run automatically on a schedule:

```bash
# 1. Make a per-user copy of the template
cp com.example.twitter-digest.plist ~/Library/LaunchAgents/com.yourname.twitter-digest.plist

# 2. Edit ~/Library/LaunchAgents/com.yourname.twitter-digest.plist:
#    - Label: change "com.example.twitter-digest" to your chosen label
#    - ProgramArguments[0]: absolute path to python3 (check with `which python3`)
#    - ProgramArguments[1]: absolute path to digest.py
#    - WorkingDirectory: absolute path to your clone
#    - EnvironmentVariables.PATH: make sure this includes the dir containing
#      `claude` (usually ~/.local/bin)
#    - EnvironmentVariables.HOME: your home directory absolute path
#    - StartCalendarInterval: adjust hours to your preference
#    - StandardOutPath / StandardErrorPath: absolute log paths

# 3. Validate and load
plutil -lint ~/Library/LaunchAgents/com.yourname.twitter-digest.plist
launchctl load ~/Library/LaunchAgents/com.yourname.twitter-digest.plist

# 4. Verify it's registered
launchctl list | grep twitter-digest

# 5. Trigger an immediate run to test the launchd-invoked version
launchctl start com.yourname.twitter-digest
```

To stop and unload later:

```bash
launchctl unload ~/Library/LaunchAgents/com.yourname.twitter-digest.plist
```

---

## Monitoring & debugging

```bash
# Tail the application log (per-month file)
tail -f logs/$(date +%Y-%m).log

# Tail the launchd stdout/stderr
tail -f logs/launchd.stdout.log logs/launchd.stderr.log

# See the most recent digest (exactly what was emailed)
cat last-digest.md

# See the most recent prompt sent to Claude (for debugging why Claude did X)
cat last-prompt.md

# Reset state (forces the next run to treat everything as new)
rm state.json

# Check launchd job status
launchctl list com.yourname.twitter-digest
```

---

## Configuration reference

`config.json` (see `config.example.json` for the full template):

| Key | Purpose | Default |
|---|---|---|
| `feeds[]` | Array of `{name, url}` RSS feeds. | required |
| `email.from` | Gmail sender address. | required |
| `email.to` | Destination address. | required |
| `gmail_keychain_service` | Keychain service name for SMTP password. | `twitter-digest-gmail` |
| `model` | Claude model passed to `claude -p --model`. | `claude-opus-4-6` |
| `sections.mandatory` | Section headers that always appear. | `[Ethereum, Solana, AI]` |
| `sections.optional` | Sections that appear only if there's content. | `[Bitcoin]` |
| `sections.exclusions` | Topics Claude must drop. | politics, price speculation, drama |
| `web_search_budget_hint` | Soft guidance to Claude on search count. | `10` |
| `claude_cli_timeout_seconds` | Hard timeout for the `claude -p` call. | `900` |
| `max_seen_ids` | Ring-buffer size for dedup state. | `2000` |
| `link_fetch_timeout_seconds` | Per-URL fetch timeout for article expansion. | `7` |
| `link_max_content_chars` | Max chars of article text passed into prompt. | `3500` |
| `link_max_per_tweet` | Max articles pre-fetched per tweet. | `3` |
| `link_concurrency` | Parallelism for article fetching. | `10` |

---

## Cost

**With Claude Max and `claude -p`**: $0 per run. The only marginal cost is
whatever you pay rss.app (free tier covers 2 feeds; paid tiers scale with
feed count).

**If you prefer the paid Anthropic API**: replace the `summarize()` function
with an `anthropic.Anthropic().messages.create(...)` call using the
`web_search_20250305` server tool. Budget notes:

- Opus 4.6: $5/Mtok in, $25/Mtok out
- Sonnet 4.6: $3/Mtok in, $15/Mtok out
- Haiku 4.5: $1/Mtok in, $5/Mtok out
- `web_search` server tool: $10 per 1,000 searches (search cost dominates
  at Haiku prices)

Rough daily cost (3 runs/day, ~50 items/run) by model:

| Model | Est. daily | Notes |
|---|---|---|
| Opus 4.6 (API) | $1.50–3.00 | Highest quality |
| Sonnet 4.6 (API) | $0.70–1.50 | Best quality/cost ratio |
| Haiku 4.5 (API) | $0.50–1.00 | Cheapest, but quality issues on synthesis — see limitations |
| Opus 4.6 via `claude -p` + Max | **$0** | Subject to Max fair-use; ~3 min of Opus time per run is well within typical use |

---

## Limitations

- **Your Mac must be awake at scheduled times.** launchd fires once on
  wake if a scheduled slot is missed, not for every missed slot. This is
  fine for a daily-driver laptop that's on during waking hours; not fine
  for overnight delivery. The fix is to run on an always-on host, but that
  requires either (a) copying your Max OAuth credentials to a remote box
  (fragile, possibly against Max terms), or (b) falling back to the paid
  Anthropic API.
- **Truncated retweets.** X's RSS representations only include the first
  ~140 chars of retweeted content, cut off with `…`. The LLM has to reason
  from fragments and from web search. Original (non-RT) tweets come through
  in full.
- **X-native articles can't be fetched.** Links to `x.com/i/article/...`
  (long-form X posts) are walled off; the tool skips them rather than
  trying to crawl.
- **rss.app freshness.** Free tier refreshes hourly, so items posted
  between runs can have up to an hour of lag before they show up in a feed.
- **The Haiku model struggles with synthesis.** If you switch to API +
  Haiku to save cost, expect the model to under-use its search budget,
  misfile stories by section, and sometimes mark sections "nothing
  substantial" even when there's clear content. The prompt has been tuned
  to mitigate this but Opus is significantly better.

---

## Project layout

```
twitter-digest/
├── digest.py                      # main script
├── config.example.json            # template for user config
├── config.json                    # YOUR config (gitignored)
├── requirements.txt
├── com.example.twitter-digest.plist   # launchd template
├── README.md
├── LICENSE
├── .gitignore
├── state.json                     # runtime state (gitignored)
├── last-digest.md                 # most recent rendered email (gitignored)
├── last-prompt.md                 # most recent prompt to Claude (gitignored)
└── logs/                          # per-month app log + launchd stdout/stderr (gitignored)
```

---

## Contributing

PRs welcome. Some ideas:

- **Linux/systemd port**: replace launchd with a `systemd --user` timer and
  the keychain layer with `secret-tool` or a plaintext config with strict
  file permissions.
- **Alternative delivery**: Slack/Discord webhooks, Telegram, local file
  only.
- **Per-feed section hints**: let config.json say "feed X is the
  primary source for section Y" so the LLM has a weak prior.
- **Article caching**: dedupe fetched article content across runs so the
  same link isn't re-summarized three runs in a row.
- **HTML email option**: for users who want formatting rendering.

---

## License

MIT — see [LICENSE](LICENSE).
