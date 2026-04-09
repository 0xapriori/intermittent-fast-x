# intermittent-fast-x

A local macOS tool that reads curated X (Twitter) Lists, podcasts, forums,
GitHub repos, AI blogs, Hacker News, and MEV/DeFi research via RSS, asks
Claude to synthesize what actually happened into themed sections with real
external source links, and writes a clean HTML brief to disk on a schedule.

**Zero network egress except to Claude.** No email, no IMAP, no SMTP. The
only outbound call is `claude -p` via your existing Claude Max OAuth — the
same trust boundary you already accepted for normal Claude Code use.
Everything else — feeds, state, briefs, logs — stays on your machine.

---

## Why this exists

If you follow crypto, AI, or any fast-moving tech industry on X, most of the
signal is expensive to extract:

- Most of your timeline is retweets, memes, and engagement bait.
- Substantive tweets usually reference news without linking it.
- X's free API no longer gives you a home timeline.
- You don't want to open x.com 20 times a day.
- You also can't keep up with five podcasts, three governance forums, a
  dozen GitHub repos, the OpenAI/Anthropic/Google blogs, HN Top, and
  Flashbots research on top of actually doing your job.

This tool runs on a schedule (3x daily by default), pulls from all of your
sources at once, lets Claude research the real stories via WebSearch, and
produces a single reader-friendly HTML brief you can bookmark and open from
your browser. The output reads more like a Substack roundup than a
fragmented scroll — every bullet grounded in a real external link (CoinDesk,
GitHub, an official announcement), never a link back to x.com.

---

## Example output

```markdown
## Ethereum

- **ETH staking hits a new all-time high** — ~32% of all ETH is now locked
  in staking, up from ~30% in February. Institutional participation via
  treasury firms and ETFs continues to drive the increase, with validator
  exit queues near historic lows. [Staking Rewards](https://example.com)

- **Reth 2.0** shipped: Storage v2 is now default, ~20× faster persistence,
  SparseTrieCacheTask delivers state-root speedups. Headline: 1.7 Ggas/s.
  [Paradigm blog](https://example.com) · [Release notes](https://example.com)

## Solana

- **Jupiter ships a new Developer Platform** with unified API keys, real-time
  analytics, and usage dashboards. Every Jupiter API is accessible via a
  single key, and the platform is explicitly designed for agent integration.
  [developers.jup.ag](https://example.com)

## AI

- **Anthropic** launched **Claude Managed Agents** in public beta — a
  performance-tuned agent harness with sandboxed execution, auth, and
  checkpointing. Early adopters include Notion, Rakuten, and Asana.
  [Announcement](https://example.com)
```

Rendered as clean HTML with typography-focused styling (white background,
system fonts, bold navy headings, blue underlined links, source chips in
the header showing category counts).

---

## Architecture

```
  ┌────────────────────┐
  │ launchd cron (Mac) │   StartCalendarInterval
  │  7am / 2pm / 8pm   │
  └──────────┬─────────┘
             │
             ▼
  ┌────────────────────┐     ┌───────────────────┐
  │  python digest.py  │────▶│  trafilatura      │  article text extract
  │                    │     │  (for X-linked    │
  │                    │◀────│   external URLs)  │
  └──────────┬─────────┘     └───────────────────┘
             │ stdin (prompt)
             ▼
  ┌────────────────────┐
  │    claude -p       │     Opus 4.6 via Max OAuth.
  │  WebSearch /       │     Native tools, no API key needed.
  │   WebFetch         │
  └──────────┬─────────┘
             │ stdout (markdown)
             ▼
  ┌────────────────────┐
  │  markdown → HTML   │
  │  (light theme)     │
  └──────────┬─────────┘
             │
             ▼
  briefs/YYYY-MM-DD_HH-MM.html     (archive)
  latest-brief.html                (always newest)
  + macOS notification
```

### Data flow per run

1. Fetch every configured RSS source in parallel (X Lists, podcasts, forums,
   GitHub atom feeds, AI blogs, HN, MEV/DeFi research).
2. Dedupe against `state.json` (a ring buffer of recently-seen GUIDs).
3. Extract URLs from X tweets; pre-fetch non-X links concurrently via
   trafilatura for clean article text. Non-X sources already contain their
   full content, so they skip this step.
4. Build the prompt: tweets + forum threads + podcast show notes + GitHub
   activity + AI news + HN stories + MEV research, all grouped by category
   with inline instructions for Claude.
5. Invoke `claude -p` with `--allowedTools WebSearch,WebFetch`. Claude
   researches each topic, finds real sources, and writes markdown to stdout.
6. Convert markdown → clean HTML via the `markdown` library, wrap in a
   light-theme template with typography tuned for readability.
7. Write timestamped archive to `briefs/<YYYY-MM-DD_HH-MM>.html` and update
   `latest-brief.html` to the same content.
8. Fire a macOS notification so you know a fresh brief is ready.
9. Persist new GUIDs to `state.json` so they don't re-appear next run.

### Source categories

Items are classified at fetch time into one of seven categories, each with
tailored content normalization and prompt instructions:

| Category | Examples | Notes |
|---|---|---|
| `x-twitter` | rss.app feeds from private X Lists | Tweet-HTML stripped, URLs extracted for pre-fetch |
| `podcast` | Lex Fridman, Dwarkesh, Bankless, ZK Podcast, etc. | Claude labels each episode SKIP / SKIM / LISTEN with reasoning |
| `forum` | Ethereum Magicians, ethresear.ch, DAO governance | First-post excerpts, substantive threads only |
| `github` | commits.atom / releases.atom for tracked repos | Release notes, new EIPs |
| `ai-news` | OpenAI, Google, HuggingFace, Latent Space | Frontier model releases, research |
| `hn` | Hacker News frontpage | AI/ML/dev tooling signal |
| `mev-defi` | Flashbots writings + collective forum | MEV research and blockspace economics |

---

## Prerequisites

- **macOS.** Uses `launchd` for scheduling and macOS notifications. Porting
  to Linux means replacing the scheduler with systemd and swapping
  `osascript` for `notify-send`.
- **Python 3.10+.**
- **Claude Code CLI** (`claude`), logged in to a Claude Max account.
  Install from [claude.com/download](https://claude.com/download). Run
  `claude` once interactively and log in — that's the entire auth setup.
  No API key in this project.
- **An [rss.app](https://rss.app/) account** (optional — only needed for X
  Lists). Free tier = 2 feeds, paid tiers for more. Skip this if you're not
  using X.

No Gmail. No SMTP. No IMAP. No app passwords. No keychain entries.

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/YOUR_USER/intermittent-fast-x.git
cd intermittent-fast-x
python3 -m pip install --user -r requirements.txt
```

### 2. Create your config

```bash
cp config.example.json config.json
```

Edit `config.json`:

- **`sources`**: an array of source objects. Each has:
  - `name` (display name used in logs and the prompt)
  - `url` (RSS/Atom feed URL)
  - `category` (one of: `x-twitter`, `podcast`, `forum`, `github`, `ai-news`, `hn`, `mev-defi`)
  - `max_items` (optional; caps items from this source per run — useful for chatty feeds)

- **`sections.mandatory`**: section headers that always appear in the brief
  even if there's nothing to report (empty ones say "Nothing substantial this
  window"). Default: `["Ethereum", "Solana", "AI", "Hacker News"]`. The first
  four names are "known" and get built-in prompt guidance; any additional
  name you add will appear in the output but without per-section guidance
  (you can extend `SECTION_GUIDANCE` in `digest.py` to add more).

- **`sections.optional`**: sections that only appear when Claude finds
  substantive content. Default: `["Bitcoin"]`.

- **`sections.exclusions`**: topics Claude should drop entirely. Each entry
  is a sentence the model sees verbatim in the prompt's "Hard exclusions"
  block. Default drops politics, price speculation, and personal drama.

- **`output`**:
  - `briefs_dir`: where to write timestamped archives. Default: `briefs/` next to the script.
  - `latest_pointer`: where to write the always-newest copy. Default: `latest-brief.html` next to the script.
  - `show_macos_notification`: set to `false` to silence the post-run notification.

### 3. Run a manual test

```bash
python3 digest.py
```

Or cap the item count for a quick verification:

```bash
TW_LIMIT=15 python3 digest.py
```

Expected output:

```
[2026-04-08 21:11:48] digest run start · 33 feed(s)
[2026-04-08 21:11:48]   ✓ Ethereum Magicians [forum ] 15 items
[2026-04-08 21:11:48]   ✓ Hacker News Top    [hn    ] 30 items
...
[2026-04-08 21:11:50] 278 new items after dedup
[2026-04-08 21:11:50]   invoking: claude -p --model claude-opus-4-6 --allowedTools WebSearch,WebFetch
[2026-04-08 21:19:02] summary generated (19724 chars) · duration=432.3s
[2026-04-08 21:19:02] brief written: briefs/2026-04-08_21-19.html
[2026-04-08 21:19:02] latest pointer:  latest-brief.html
```

Open the brief in your browser:

```bash
open latest-brief.html
```

Bookmark that file URL so you can click it whenever you want to see the
latest brief.

### 4. Install the scheduler (optional)

If you want it to run automatically:

```bash
# 1. Make a per-user copy of the template
cp com.example.twitter-digest.plist ~/Library/LaunchAgents/com.yourname.intermittent-fast-x.plist

# 2. Edit ~/Library/LaunchAgents/com.yourname.intermittent-fast-x.plist:
#    - Label: change to your chosen label
#    - ProgramArguments[0]: absolute path to python3 (check with `which python3`)
#    - ProgramArguments[1]: absolute path to digest.py
#    - WorkingDirectory: absolute path to your clone
#    - EnvironmentVariables.PATH: make sure this includes the dir containing
#      `claude` (usually ~/.local/bin)
#    - EnvironmentVariables.HOME: your home directory absolute path
#    - StartCalendarInterval: adjust hours to your preference
#    - StandardOutPath / StandardErrorPath: absolute log paths

# 3. Validate and load
plutil -lint ~/Library/LaunchAgents/com.yourname.intermittent-fast-x.plist
launchctl load ~/Library/LaunchAgents/com.yourname.intermittent-fast-x.plist

# 4. Verify it's registered
launchctl list | grep intermittent-fast-x

# 5. Trigger an immediate run to test the launchd-invoked version
launchctl start com.yourname.intermittent-fast-x
```

---

## Viewing briefs

The output is a local HTML file. Options:

- **Bookmark** `file:///path/to/your/clone/latest-brief.html` in your
  browser. Click it whenever you want the latest.
- **Open on demand** from the terminal: `open latest-brief.html`
- **Browse archives** in `briefs/` — each run writes a timestamped file.

If `show_macos_notification` is enabled, you'll get a Notification Center
bubble after each run saying "Research Brief ready — N items across M sources".

---

## Monitoring & debugging

```bash
# Tail the application log (per-month file)
tail -f logs/$(date +%Y-%m).log

# Tail the launchd stdout/stderr
tail -f logs/launchd.stdout.log logs/launchd.stderr.log

# See the most recent prompt sent to Claude (for debugging why Claude did X)
cat last-prompt.md

# Reset state (forces the next run to treat everything as new)
rm state.json

# Check launchd job status
launchctl list com.yourname.intermittent-fast-x
```

---

## Configuration reference

| Key | Purpose | Default |
|---|---|---|
| `sources[]` | Array of `{name, url, category, max_items?}`. | required |
| `model` | Claude model passed to `claude -p --model`. | `claude-opus-4-6` |
| `sections.mandatory` | Section headers that always appear. | `[Ethereum, Solana, AI, Hacker News]` |
| `sections.optional` | Sections that appear only if there's content. | `[Bitcoin]` |
| `sections.exclusions` | Topics Claude must drop (verbatim into the prompt). | politics, price speculation, drama |
| `feed_fetch_timeout_seconds` | Per-feed HTTP timeout. | `30` |
| `output.briefs_dir` | Directory for timestamped brief archives. | `briefs/` next to script |
| `output.latest_pointer` | Path for the always-newest brief. | `latest-brief.html` next to script |
| `output.show_macos_notification` | Post-run notification. | `true` |
| `web_search_budget_hint` | Soft guidance to Claude on search count. | `10` |
| `claude_cli_timeout_seconds` | Hard timeout for the `claude -p` call. | `1800` |
| `max_seen_ids` | Ring-buffer size for dedup state. | `2000` |
| `link_fetch_timeout_seconds` | Per-URL fetch timeout for article expansion. | `7` |
| `link_max_content_chars` | Max chars of article text in the prompt. | `3500` |
| `link_max_per_tweet` | Max articles pre-fetched per tweet. | `3` |
| `link_concurrency` | Parallelism for article fetching. | `10` |

---

## Cost

**With Claude Max and `claude -p`**: $0 per run. The only marginal cost is
whatever you pay rss.app (free tier covers 2 feeds; paid tiers scale with
feed count).

Typical run on a full multi-source setup (~30 sources, ~200-300 items)
takes 5-8 minutes of Opus time, well within normal Max fair-use.

**If you want to use the paid Anthropic API instead**, replace the
`summarize()` function with an `anthropic.Anthropic().messages.create(...)`
call using the `web_search_20250305` server tool. Budget notes:

- Opus 4.6: $5/Mtok in, $25/Mtok out
- Sonnet 4.6: $3/Mtok in, $15/Mtok out
- Haiku 4.5: $1/Mtok in, $5/Mtok out (may struggle with synthesis — see limitations)
- `web_search` server tool: $10 per 1,000 searches (search cost dominates at Haiku prices)

---

## Privacy properties

This tool was specifically re-architected to remove all outbound channels
except Claude itself. What that buys you:

- **No Gmail involvement.** No SMTP credentials, no IMAP access, no OAuth
  tokens for third-party mail providers. The tool has literally no way to
  send email to anyone.
- **No persistent third-party services.** Feeds are fetched via HTTP, but
  no credentials are stored or sent to them.
- **All outputs stay local.** Briefs live on your disk. State lives on your
  disk. Logs live on your disk. Nothing is uploaded.
- **One network egress path: `claude -p`.** Everything Claude sees (your
  feed content, the prompt, search queries it runs) goes through Anthropic's
  normal Claude Code data path. If you trust Claude Code for your regular
  work, this adds no new trust assumptions.
- **No keychain entries needed.** Zero secrets to manage.

If even Anthropic is too broad a trust boundary for your use case, you can
swap `claude -p` for a local LLM (Ollama + a 70B model) — the architecture
supports this with minor changes, though quality will drop for the synthesis
task.

---

## Limitations

- **Your Mac must be awake at scheduled times.** launchd fires once on
  wake if a scheduled slot was missed, not for every missed slot. Fine for
  a daily-driver laptop on during waking hours; not fine for overnight
  delivery.
- **Truncated retweets.** X's RSS representation truncates RT bodies to
  ~140 chars. The LLM has to reason from fragments and from web search.
  Original (non-RT) tweets come through in full.
- **X-native articles can't be fetched.** Links to `x.com/i/article/...`
  (long-form X posts) are walled off; the tool skips them.
- **rss.app freshness.** Free tier refreshes hourly.
- **Haiku struggles with synthesis.** If you switch the model to Haiku 4.5
  to save cost, expect the model to under-use its search budget and
  sometimes misfile stories by section. Opus is significantly better at
  this task.

---

## Project layout

```
intermittent-fast-x/
├── digest.py                      # main script
├── config.example.json            # template for user config
├── config.json                    # YOUR config (gitignored)
├── requirements.txt
├── com.example.twitter-digest.plist   # launchd template
├── README.md
├── LICENSE
├── .gitignore
├── state.json                     # runtime state (gitignored)
├── latest-brief.html              # always-newest brief (gitignored)
├── briefs/                        # timestamped archive (gitignored)
│   ├── 2026-04-08_07-00.html
│   ├── 2026-04-08_14-00.html
│   └── ...
├── last-prompt.md                 # most recent prompt to Claude (gitignored)
└── logs/                          # per-month app log + launchd stdout/stderr (gitignored)
```

---

## Contributing

PRs welcome. Some ideas:

- **Linux/systemd port**: replace launchd with a `systemd --user` timer;
  swap `osascript` for `notify-send`.
- **Per-feed section hints**: let config say "feed X is the primary source
  for section Y" so the LLM has a weak prior.
- **Article caching**: dedupe fetched article content across runs so the
  same link isn't re-summarized multiple runs in a row.
- **Local LLM support**: swap `claude -p` for Ollama for maximum privacy.
- **Markdown-only output mode**: for users who prefer plain text in a
  terminal over HTML in a browser.

---

## License

MIT — see [LICENSE](LICENSE).
