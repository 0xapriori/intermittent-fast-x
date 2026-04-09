#!/usr/bin/env python3
"""Multi-source research brief: RSS feeds + article expansion → local HTML.

Fetches many RSS feeds (curated X Lists, podcasts, forums, GitHub, AI blogs,
HN, MEV/DeFi research), dedupes against state, expands linked articles via
trafilatura, then invokes `claude -p` (Claude Code non-interactive) to
synthesize a themed brief. Output is written to a local HTML file and
optionally surfaced via a macOS notification.

No email. No IMAP. No SMTP. No outbound credentials of any kind.
Everything stays on your machine except the `claude -p` invocation itself
(which uses your existing Claude Max OAuth — same trust boundary as any
other Claude Code session).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path

import feedparser
import markdown as md_lib
import requests
import trafilatura

# --- paths ------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"


# --- config loading ---------------------------------------------------------

def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise SystemExit(
            f"Missing {CONFIG_FILE}.\n"
            f"Copy config.example.json to config.json and fill in your sources."
        )
    with open(CONFIG_FILE) as f:
        return json.load(f)


CONFIG = _load_config()

# Sources come from config.json. Each entry: {name, url, category, max_items?}.
# Categories drive content normalization and how the prompt groups items.
# All feeds are parsed with feedparser regardless of category.
FEEDS: list[dict] = CONFIG.get("sources") or []
if not FEEDS:
    raise SystemExit("config.json has no 'sources' list — see config.example.json")

# Per-category content truncation (chars) — non-tweet items can have long
# descriptions; truncate to keep the prompt manageable.
CONTENT_MAX_CHARS_BY_CATEGORY = {
    "x-twitter": 600,
    "podcast":   2200,
    "forum":     1500,
    "github":    600,
    "ai-news":   1500,
    "hn":        400,
    "mev-defi":  2000,
}

# Display labels for categories in the prompt
CATEGORY_LABELS = {
    "x-twitter": "X TWEETS (from curated Lists)",
    "podcast":   "PODCAST EPISODES (show notes — flag worth listening)",
    "forum":     "FORUM THREADS (governance / research discussions)",
    "github":    "GITHUB ACTIVITY (commits / releases)",
    "ai-news":   "AI INDUSTRY NEWS",
    "hn":        "HACKER NEWS TOP",
    "mev-defi":  "MEV & DeFi RESEARCH",
}

# Order categories appear in the prompt (drives which Claude sees first)
CATEGORY_ORDER = ["x-twitter", "podcast", "forum", "github", "ai-news", "hn", "mev-defi"]

MODEL: str = CONFIG.get("model", "claude-opus-4-6")  # via Max through `claude -p`

# Local output paths. Resolve ~ if the user put a home-relative path in config.
def _resolve_path(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()

_output = CONFIG.get("output", {})
BRIEFS_DIR = _resolve_path(_output.get("briefs_dir", str(BASE_DIR / "briefs")))
LATEST_BRIEF = _resolve_path(
    _output.get("latest_pointer", str(BASE_DIR / "latest-brief.html"))
)
SHOW_NOTIFICATION: bool = bool(_output.get("show_macos_notification", True))

MAX_SEEN_IDS: int = CONFIG.get("max_seen_ids", 2000)
CLAUDE_CLI_TIMEOUT: int = CONFIG.get("claude_cli_timeout_seconds", 1800)
WEB_SEARCH_BUDGET_HINT: int = CONFIG.get("web_search_budget_hint", 10)

# Link expansion
LINK_FETCH_TIMEOUT: int = CONFIG.get("link_fetch_timeout_seconds", 7)
LINK_MAX_CONTENT_CHARS: int = CONFIG.get("link_max_content_chars", 3500)
LINK_MAX_PER_TWEET: int = CONFIG.get("link_max_per_tweet", 3)
LINK_CONCURRENCY: int = CONFIG.get("link_concurrency", 10)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Domains where the content isn't worth fetching (X itself, images, video, app stores)
SKIP_HOSTS = {
    "twitter.com", "x.com", "mobile.twitter.com", "mobile.x.com",
    "pic.twitter.com", "pic.x.com", "pbs.twimg.com", "abs.twimg.com",
    "video.twimg.com",
    "youtube.com", "youtu.be", "m.youtube.com",
    "tiktok.com", "vm.tiktok.com",
    "instagram.com",
    "apps.apple.com", "play.google.com",
}
# Shorteners we should follow-through before deciding
SHORTENER_HOSTS = {"t.co", "bit.ly", "buff.ly", "lnkd.in", "dlvr.it", "ow.ly"}


# --- utilities --------------------------------------------------------------

def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{datetime.now():%Y-%m}.log"
    with open(log_file, "a") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("state.json corrupt — starting fresh")
    return {"seen_ids": []}


def save_state(state: dict) -> None:
    state["seen_ids"] = state["seen_ids"][-MAX_SEEN_IDS:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --- feed parsing -----------------------------------------------------------

def strip_tweet_html(html: str) -> str:
    """rss.app wraps each tweet in <blockquote>...<p>BODY</p>— AUTHOR link</blockquote>."""
    if not html:
        return ""
    m = re.search(r"<p[^>]*>(.*?)</p>", html, re.DOTALL)
    text = m.group(1) if m else html
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def strip_generic_html(html: str) -> str:
    """Generic HTML → plain text for non-tweet RSS items (podcasts, forums,
    GitHub, blogs, HN). Keeps newlines between paragraphs but drops tags."""
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_content(category: str, description_html: str, title: str = "") -> str:
    """Return plain text body appropriate for a given source category."""
    if category == "x-twitter":
        return strip_tweet_html(description_html)
    body = strip_generic_html(description_html)
    # For most non-tweet sources the title is a critical signal. Prepend it
    # if it's not already in the body (avoid dupes for podcasts where the
    # title is usually the first line of the show notes).
    if title and title.strip() and title.strip().lower() not in body.lower()[:200]:
        body = f"{title.strip()}\n\n{body}".strip()
    cap = CONTENT_MAX_CHARS_BY_CATEGORY.get(category, 1500)
    return body[:cap]


def extract_urls(tweet_text: str, tweet_description_html: str) -> list[str]:
    """Pull URLs from both the HTML description (href attrs) and text body."""
    urls: list[str] = []
    seen: set[str] = set()
    # HTML href attributes — most reliable
    for m in re.finditer(r'href=["\']([^"\']+)["\']', tweet_description_html or ""):
        u = m.group(1)
        if u not in seen:
            seen.add(u)
            urls.append(u)
    # Plain-text URLs (catch anything not in an href). Reject anything
    # containing a horizontal ellipsis or ending in one — those come from
    # rss.app's truncated display text for retweets, not real URLs.
    for m in re.finditer(r"https?://[^\s<>\"'\u2026]+", tweet_text or ""):
        u = m.group(0).rstrip(".,);:!?")
        if "\u2026" in u or u.endswith("..."):
            continue
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def fetch_all_feeds() -> list[dict]:
    """Fetch every configured feed and return a list of entries tagged with
    feed name and source category."""
    all_items: list[dict] = []
    for feed_cfg in FEEDS:
        category = feed_cfg.get("category", "x-twitter")
        max_items = feed_cfg.get("max_items")
        try:
            parsed = feedparser.parse(feed_cfg["url"])
            if parsed.bozo and not parsed.entries:
                log(f"  ✗ {feed_cfg['name']} [{category}]: parse failed ({parsed.bozo_exception})")
                continue
            entries = parsed.entries
            if max_items:
                entries = entries[:max_items]
            for entry in entries:
                guid = entry.get("id") or entry.get("guid") or entry.get("link")
                if not guid:
                    continue
                desc_html = entry.get("summary", "") or entry.get("description", "")
                title = entry.get("title", "")
                text = normalize_content(category, desc_html, title)
                all_items.append({
                    "id": guid,
                    "feed": feed_cfg["name"],
                    "category": category,
                    "author": entry.get("author", "unknown"),
                    "title": title,
                    "text": text,
                    "description_html": desc_html,
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "urls": extract_urls(text, desc_html),
                })
            log(f"  ✓ {feed_cfg['name']:28s} [{category:10s}] {len(entries)} items")
        except Exception as e:
            log(f"  ✗ {feed_cfg['name']} [{category}]: {e}")
    return all_items


# --- link expansion ---------------------------------------------------------

def host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def should_fetch(url: str) -> bool:
    host = host_of(url)
    if not host:
        return False
    if host in SKIP_HOSTS:
        return False
    # Path-level skip for x.com article previews (still X)
    if "/i/article/" in url or "/i/web/status/" in url:
        return False
    lowered = url.lower()
    if lowered.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp",
                         ".mp4", ".mov", ".pdf", ".zip", ".dmg")):
        return False
    return True


def resolve_shortener(url: str) -> str:
    """If url is a known shortener, HEAD-follow to the final location."""
    host = host_of(url)
    if host not in SHORTENER_HOSTS:
        return url
    try:
        r = requests.head(url, headers={"User-Agent": USER_AGENT},
                          timeout=4, allow_redirects=True)
        return r.url or url
    except Exception:
        return url


def fetch_article(url: str) -> dict | None:
    """Fetch a URL and extract main article text. Returns None on any failure."""
    try:
        final = resolve_shortener(url)
        if not should_fetch(final):
            return None
        r = requests.get(final, headers={"User-Agent": USER_AGENT},
                         timeout=LINK_FETCH_TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            return None
        ctype = r.headers.get("content-type", "").lower()
        if "html" not in ctype and "text/plain" not in ctype:
            return None
        # trafilatura does the heavy lifting of main-content extraction
        extracted = trafilatura.extract(
            r.text,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if not extracted or len(extracted.strip()) < 80:
            return None
        title = ""
        title_m = re.search(r"<title[^>]*>(.*?)</title>", r.text,
                            re.DOTALL | re.IGNORECASE)
        if title_m:
            title = unescape(title_m.group(1)).strip()[:200]
        return {
            "url": final,
            "title": title,
            "text": extracted.strip()[:LINK_MAX_CONTENT_CHARS],
            "host": host_of(final),
        }
    except Exception:
        return None


def expand_links_for_items(items: list[dict]) -> None:
    """Mutates each item in place, adding an 'articles' list with fetched content.

    Only runs for X tweets. Non-X sources (podcasts, forums, github, blogs, HN,
    mev-defi) already contain their own long-form content in the description;
    pre-fetching every link they mention would explode the prompt size for
    no benefit. Those items pass through with an empty articles list."""
    url_to_items: dict[str, list[dict]] = {}
    for it in items:
        it["articles"] = []
        if it.get("category") != "x-twitter":
            continue
        count = 0
        for raw_url in it["urls"]:
            if count >= LINK_MAX_PER_TWEET:
                break
            host = host_of(raw_url)
            if host in SKIP_HOSTS and host not in SHORTENER_HOSTS:
                continue
            low = raw_url.lower()
            if low.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp",
                             ".mp4", ".mov")):
                continue
            url_to_items.setdefault(raw_url, []).append(it)
            count += 1

    if not url_to_items:
        log("  no external URLs to expand")
        return

    log(f"  expanding {len(url_to_items)} unique URLs across {len(items)} tweets")
    fetched_ok = 0
    with ThreadPoolExecutor(max_workers=LINK_CONCURRENCY) as pool:
        futures = {pool.submit(fetch_article, u): u for u in url_to_items}
        for fut in as_completed(futures):
            u = futures[fut]
            try:
                article = fut.result()
            except Exception:
                article = None
            if article:
                fetched_ok += 1
                for it in url_to_items[u]:
                    it["articles"].append(article)
    log(f"  fetched {fetched_ok}/{len(url_to_items)} articles")


# --- summarization ----------------------------------------------------------

def build_prompt(items: list[dict]) -> str:
    # Group items by category in the prompt so Claude sees source context
    items_by_cat: dict[str, list[dict]] = {}
    for it in items:
        cat = it.get("category", "x-twitter")
        items_by_cat.setdefault(cat, []).append(it)

    category_blocks: list[str] = []
    global_idx = 0
    for cat in CATEGORY_ORDER:
        cat_items = items_by_cat.get(cat, [])
        if not cat_items:
            continue
        label = CATEGORY_LABELS.get(cat, cat)
        lines = [f"\n### {label} ({len(cat_items)} items)\n"]
        for it in cat_items:
            global_idx += 1
            text = it["text"].strip()
            # Per-category header format
            if cat == "x-twitter":
                is_rt = text.startswith("RT @") or text.startswith("RT ")
                rt_flag = " [RT]" if is_rt else ""
                header = f"[{global_idx}] {it['author']}{rt_flag} · list={it['feed']} · {it['published']}"
            elif cat == "podcast":
                header = f"[{global_idx}] {it['feed']} · EPISODE · {it['published']}"
            elif cat == "forum":
                header = f"[{global_idx}] {it['feed']} · THREAD by {it['author']} · {it['published']}"
            elif cat == "github":
                header = f"[{global_idx}] {it['feed']} · {it['author']} · {it['published']}"
            elif cat == "ai-news":
                header = f"[{global_idx}] {it['feed']} · POST · {it['published']}"
            elif cat == "hn":
                header = f"[{global_idx}] HN · {it.get('title','(no title)')} · {it['published']}"
            elif cat == "mev-defi":
                header = f"[{global_idx}] {it['feed']} · {it['author']} · {it['published']}"
            else:
                header = f"[{global_idx}] {it['feed']} · {it['author']} · {it['published']}"

            parts = [header, text, f"link: {it['link']}"]
            for art in it.get("articles", []):
                art_block = (
                    f"    └── LINKED ARTICLE ({art['host']})\n"
                    f"        title: {art['title']}\n"
                    f"        url:   {art['url']}\n"
                    f"        excerpt: {art['text']}"
                )
                parts.append(art_block)
            lines.append("\n".join(parts))
        category_blocks.append("\n\n".join(lines))

    all_content_block = "\n\n".join(category_blocks)

    feed_names = ", ".join(f["name"] for f in FEEDS)
    category_counts = ", ".join(
        f"{len(items_by_cat.get(c, []))} {c}" for c in CATEGORY_ORDER if items_by_cat.get(c)
    )

    return f"""You are producing a signal-driven multi-source digest for a crypto/AI researcher. They do NOT want to visit x.com, read dozens of podcast show notes, skim five governance forums, watch GitHub release feeds, or scan Hacker News themselves. Your job is to synthesize WHAT HAPPENED and WHAT IS BEING DISCUSSED across all sources, grounded in real external links.

## Input sources ({category_counts})

You are being given material from SEVEN kinds of sources, clearly labelled in the content block below:

1. **X TWEETS** — curated X/Twitter Lists. Mostly truncated retweets and short takes. Low signal per item, high volume.
2. **PODCAST EPISODES** — show notes from podcasts the reader doesn't have time to listen to. For each episode you MUST decide: (a) summarize the 2-3 key ideas in the description, (b) flag explicitly whether it's worth actually listening to — "skip", "skim", or "listen" — with one line of why.
3. **FORUM THREADS** — Ethereum Magicians, ethresear.ch, and DAO governance forums (Uniswap, Optimism, Arbitrum). High-signal technical + governance discussions. Include substantive ones in the relevant section.
4. **GITHUB ACTIVITY** — commits on EIP repos, releases on geth/reth/foundry/agave. Filter noise; surface meaningful releases and new EIP proposals.
5. **AI INDUSTRY NEWS** — OpenAI, Google, HuggingFace, Latent Space. Frontier model releases, research papers, product launches. Goes in the AI section.
6. **HACKER NEWS TOP** — the current HN frontpage. Feed this into the dedicated `## Hacker News` section (see below). Coverage is broad, not limited to AI/crypto — include any substantive story a technical reader would care about.
7. **MEV & DeFi RESEARCH** — Flashbots writings, Flashbots collective forum. Technical posts about MEV, blockspace economics, DeFi primitives.

## Core framing: events over individual items

- Focus on EVENTS and DISCUSSIONS, not individual posts or accounts. Your unit of output is "a thing that happened or is being talked about", not "@someone tweeted X".
- Cross-source consolidation is the whole point: if an event appears in tweets AND a GitHub release AND a forum thread AND a podcast episode — that is ONE bullet with all sources cited, not four bullets. The more sources confirm it, the higher signal.
- Only break out an individual item as its own bullet if it's original and substantive — a novel analysis, announcement, release, or research result with concrete content.
- Scale volume to substance: a quiet window gets a short digest. Don't pad.

## Use WebSearch and WebFetch to find the real stories

You have WebSearch and WebFetch tools. Most tweets are truncated retweets without links — search to find the actual source. **Use approximately {WEB_SEARCH_BUDGET_HINT} searches for a full-size batch**, more for large batches. There is no per-search fee.

Search WHEN:
- A tweet / forum post / HN title references a launch, vote, hack, fundraise, partnership, release, paper, or data point without a link → find the underlying source
- Multiple items reference the same event → ONE search, consolidate
- An unfamiliar project or term needs a sentence of context → quick search
- A stat is claimed without a source → verify and link the data source

Do NOT search for:
- Memes, banter, vibes, reactions with no specific claim
- Anything already covered by a LINKED ARTICLE or sufficient show notes / release description
- HN stories where the title + description already give you enough context (search only for the ones that genuinely need it)

## Mandatory section structure

Your digest MUST always include these four sections in this order:

1. `## Ethereum` — ETH core, L2s/rollups (Base, Arbitrum, Optimism, etc.), DeFi on ETH, restaking/LSTs, MEV, ETH-ecosystem apps and tooling
2. `## Solana` — SOL core, Solana DeFi, memecoin dynamics, Phantom/Jito/Jupiter/Pump.fun, Solana ecosystem apps
3. `## AI` — AI models, agents, Anthropic/OpenAI/Google/xAI/Meta, AI x crypto, ML infra, agentic commerce, AI tooling. This is the full AI industry, not just AI-crypto crossover.
4. `## Hacker News` — The top stories trending on HN frontpage right now, across any topic (not just AI/crypto). See the "Hacker News section requirements" below for specifics.

## Hacker News section requirements

The Hacker News input category contains the current HN frontpage (top stories by points/comments). You MUST summarize them in a dedicated `## Hacker News` section with the following rules:

- **4-8 bullets**, one per meaningful story. Pick the highest-signal items — prioritize technical releases, research, novel tools, substantive writing, industry news. Skip pure rage-bait, off-topic memes, and low-effort link farms.
- **Scope is NOT limited to AI/crypto.** HN covers the entire tech world. Include anything a curious technical reader would find substantive: new programming languages, OS/kernel news, hardware launches, science papers, infrastructure research, novel products, postmortems, long-form essays, legal/policy news affecting tech, etc.
- Each bullet format: `- **<Story title or topic>**: 1-2 sentences of summary/context. [HN discussion](url) · [Source](url)`
- If the HN story links to an external article, link BOTH the HN comments page (usually https://news.ycombinator.com/item?id=...) AND the source URL, so the reader can choose discussion or article.
- If the top story is already covered in the Ethereum/Solana/AI sections (e.g., a major Anthropic release is #1 on HN AND in the AI section), skip it here to avoid duplication. Note at the end: "(Top AI stories covered in the AI section above.)"
- **Do NOT dismiss this section with "nothing substantial".** HN frontpage always has content; the job is to find the 4-8 most interesting items and explain them.

Optionally include `## Bitcoin` ONLY IF there is substantive Bitcoin content (BTC core, ordinals, Lightning, ETF flows, regulatory news). **If there is no Bitcoin content, OMIT the section entirely. Do not print an empty Bitcoin header.**

**Do not dismiss content too easily.** Check across ALL source types before marking a section empty. Signals by section:
- **Ethereum**: mentions of Base, Arbitrum, Optimism, Polygon, restaking, EigenLayer, LSTs, ETH staking, MEV, rollups, L2s, EIPs, Uniswap/Aave/Morpho/Pendle/Lido, ENS, Farcaster, Reth/Geth/Erigon, Foundry, Flashbots, any Ethereum governance thread, any ETH ecosystem forum post, any eip-* GitHub commit.
- **Solana**: mentions of Jupiter, Phantom, Jito, Pump.fun, Helius, Metaplex, Firedancer/Agave releases, SIMD proposals, SOL ETFs, Solana Mobile, any SOL ecosystem app.
- **AI**: mentions of Claude, GPT, Gemini, LLaMA, Anthropic, OpenAI, Google DeepMind, Meta AI, xAI, agents, RAG, MCP, HuggingFace releases, arxiv papers, model launches, inference cost / latency, training runs, GPU supply, AI safety, interpretability, autoresearch, any AI-adjacent HN story with enough signal.

**Minimum depth when content exists**: if a section has ANY relevant content in the batch, produce **at least 2 substantive bullets**. Don't stop at 1.

**Empty sections (Ethereum/Solana/AI only)**: write exactly this single line under the header, nothing else: `_Nothing substantial this window._`

## Hard exclusions

- **NO POLITICS**. Drop any tweet about elections, politicians, government policy debates, geopolitical conflict, culture war, or ideology. If a tweet is half-crypto half-politics (e.g., "the president's crypto EO"), keep the crypto fact and drop the political framing. If a tweet is purely political, drop it entirely — do not mention it, do not search for it.
- No crypto price speculation, TA charts, or "wen moon" content unless it's a major macro shift with a concrete catalyst.
- No personal drama, beef, or Twitter fights unless they're about a protocol's technical direction.

## Output format — PLAIN MARKDOWN

Output ONLY these markdown elements:

- `## Section` for the section headers
- `- ` bullet points (one per event/discussion, 1-3 sentences each)
- `**bold**` for key facts: numbers, protocol names, @handles when directly relevant, and the core "thing that happened"
- `[link label](https://url)` for real external URLs. Prefer official blogs > news articles > GitHub > tweet URLs as last resort. Never link to x.com/twitter.com unless there is no alternative.
- `_italic_` sparingly for hedges or qualifiers
- `> quote` blockquote only when a verbatim line is genuinely worth preserving

**Link requirement**: every substantive bullet MUST end with at least one `[label](url)` link to an external source. No exceptions.

**NO preamble.** Do NOT write "Here's the digest" or "I'll search for..." or "Based on the tweets..." or ANY intro text. Your very first characters of output must be `## Ethereum`. Anything before that will be stripped and discarded.

**NO item-number references.** Never write "(tweet 11)", "item 3", "post #5", or any numeric reference to the input scaffolding. The bracketed numbers on each item are internal — the reader will never see them. Refer to the account, the podcast name, the repo, or the topic instead.

Each bullet should read as a self-contained mini-update: what, why it matters, source link. Example style:

```
- **Monad Foundation** launches validator device subsidy program covering ~$3K of signing laptop costs, available to anyone running ≥3 months of mainnet validation. Designed to accelerate solo-validator decentralization ahead of Monad mainnet Q2. [Announcement](https://blog.monad.xyz/device-subsidy)
```

## Podcast section — special handling

Inside whichever section a podcast episode thematically belongs (Ethereum / Solana / AI / Bitcoin), include an additional line under the bullet that explicitly rates it:

```
- **a16z crypto podcast** with Paul Frambot covers how DeFi lending protocols like Morpho model risk, isolate collateral, and handle liquidations differently than Aave. Useful mental model if you're building on top of lending primitives. [Episode](https://...)
  Worth listening: **LISTEN** — substantive technical walkthrough of lending protocol design from a builder.
```

The "Worth listening" label must be exactly one of: **SKIP**, **SKIM**, or **LISTEN**. One line of justification. The reader will use this to decide whether to actually play the episode.

- **SKIP**: episode is off-topic, vibes, or covers well-known ground. Don't play it.
- **SKIM**: read the show notes, maybe play at 2x, maybe jump to timestamps. Worth awareness but not full attention.
- **LISTEN**: original substantive content (research, novel take, high-quality guest, new data). Play it properly.

REMEMBER: first output characters must be `## Ethereum`. No preamble, no meta-commentary, no "I'll search".

## Source material ({len(items)} items)

{all_content_block}
"""


def _extract_text(resp) -> str:
    """Concatenate all text blocks in the response, skipping tool_use blocks."""
    parts = []
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def _strip_preamble(markdown: str) -> str:
    """Drop any preamble before the first `## ` heading. Haiku sometimes
    ignores the 'no preamble' instruction and leaks meta-commentary."""
    idx = markdown.find("## ")
    if idx > 0:
        return markdown[idx:].strip()
    return markdown.strip()


def summarize(items: list[dict]) -> tuple[str, dict]:
    """Invoke `claude -p` (Claude Code non-interactive) to summarize.

    Uses the authenticated Max subscription via OAuth. Grants the session
    WebSearch and WebFetch tools so Claude can research tweet topics
    without an API web_search add-on.
    """
    prompt = build_prompt(items)

    # Persist the prompt for debugging / reproducibility
    (BASE_DIR / "last-prompt.md").write_text(prompt)

    start = datetime.now()
    cmd = [
        "claude", "-p",
        "--model", MODEL,
        "--allowedTools", "WebSearch,WebFetch",
    ]
    log(f"  invoking: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_CLI_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"claude -p timed out after {CLAUDE_CLI_TIMEOUT}s"
        )

    duration = (datetime.now() - start).total_seconds()

    if result.returncode != 0:
        err = (result.stderr or "").strip()[:800]
        raise RuntimeError(
            f"claude -p exited {result.returncode}: {err or '<no stderr>'}"
        )

    output = _strip_preamble(result.stdout.strip())
    stats = {
        "duration_seconds": round(duration, 1),
        "output_chars": len(output),
        "stderr_chars": len(result.stderr or ""),
    }
    return output, stats


# --- morning prepend: yesterday's Claude Code journal ----------------------

JOURNAL_DIR = Path.home() / "claude-code-journal"


def _find_recent_journal_file() -> Path | None:
    """Return the most relevant recent journal file.
    Prefers yesterday's dated file, falls back to today's (in case the
    23:59 journal fired late), then any file modified in the last 48h."""
    if not JOURNAL_DIR.exists():
        return None
    now = datetime.now()
    yesterday_path = JOURNAL_DIR / f"{(now - timedelta(days=1)):%Y-%m-%d}.md"
    today_path = JOURNAL_DIR / f"{now:%Y-%m-%d}.md"
    if yesterday_path.exists():
        return yesterday_path
    if today_path.exists():
        return today_path
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
    candidates = [
        (p.stat().st_mtime, p)
        for p in JOURNAL_DIR.glob("*.md")
        if date_pattern.match(p.name)
        and p.stat().st_mtime > (now - timedelta(hours=48)).timestamp()
    ]
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def load_yesterdays_journal() -> str | None:
    """Read the most relevant recent Claude Code journal entry and return
    it as a markdown block ready to prepend to the morning brief."""
    journal_file = _find_recent_journal_file()
    if journal_file is None:
        log("  no recent journal file found — skipping")
        return None
    try:
        raw = journal_file.read_text()
    except OSError as e:
        log(f"  failed to read journal file {journal_file}: {e}")
        return None
    log(f"  using journal file: {journal_file.name}")
    parts = raw.split("\n---\n", 1)
    if len(parts) < 2:
        log("  journal file has unexpected format — skipping")
        return None
    body = parts[1].strip()
    meta_markers = ("\n---\n\n## Session Metadata", "\n---\n## Session Metadata")
    for marker in meta_markers:
        idx = body.find(marker)
        if idx >= 0:
            body = body[:idx].strip()
            break
    if not body or len(body) < 20:
        return None
    body = re.sub(r"^## ", "### ", body, flags=re.MULTILINE)
    yesterday_display = (datetime.now() - timedelta(days=1)).strftime("%A, %B %-d")
    return f"## Yesterday's Claude Code Work — {yesterday_display}\n\n{body}"


# --- HTML brief rendering ---------------------------------------------------

def render_brief_html(claude_markdown: str, items: list[dict], stats: dict) -> str:
    """Render a readable HTML brief from Claude's markdown output.

    Uses the `markdown` library to convert to HTML, then wraps in a clean,
    typography-focused template. Light background, system fonts, generous
    whitespace. Designed to be opened in a browser from a local file URL.
    """
    now = datetime.now()
    header_date = now.strftime("%A, %B %-d, %Y · %-I:%M %p").strip(" ·")

    sources_by_cat: dict[str, int] = {}
    for it in items:
        sources_by_cat[it.get("category", "?")] = sources_by_cat.get(it.get("category", "?"), 0) + 1
    source_chips_html = "".join(
        f'<span style="display:inline-block;padding:3px 10px;margin:2px 4px 2px 0;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:999px;font-size:11px;color:#475569;font-weight:500;">{v}&nbsp;{k}</span>'
        for k, v in sources_by_cat.items()
    )

    body_html = md_lib.markdown(
        claude_markdown.strip(),
        extensions=["extra", "sane_lists", "smarty"],
    )

    # Inline styles so Gmail/other clients render properly
    def inline(tag: str, style: str, html: str) -> str:
        return re.sub(rf"<{tag}>", f'<{tag} style="{style}">', html)

    body_html = inline("h2", (
        "font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"
        "font-size:20px;font-weight:700;color:#0f172a;"
        "margin:40px 0 14px 0;padding:0 0 8px 0;"
        "border-bottom:2px solid #e2e8f0;letter-spacing:-0.01em;"
    ), body_html)
    body_html = inline("h3", (
        "font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"
        "font-size:16px;font-weight:600;color:#334155;"
        "margin:24px 0 10px 0;"
    ), body_html)
    body_html = inline("p", (
        "font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"
        "font-size:15px;line-height:1.65;color:#1e293b;"
        "margin:10px 0;"
    ), body_html)
    body_html = inline("ul", (
        "font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"
        "font-size:15px;line-height:1.65;color:#1e293b;"
        "margin:12px 0 18px 0;padding-left:22px;"
    ), body_html)
    body_html = inline("li", (
        "margin:12px 0;color:#1e293b;"
    ), body_html)
    body_html = inline("strong", (
        "color:#0f172a;font-weight:700;"
    ), body_html)
    body_html = inline("em", (
        "color:#64748b;font-style:italic;"
    ), body_html)
    body_html = inline("blockquote", (
        "margin:14px 0;padding:10px 16px;"
        "border-left:3px solid #94a3b8;"
        "background:#f8fafc;color:#475569;font-style:italic;"
    ), body_html)
    body_html = re.sub(
        r'<a href="',
        '<a style="color:#2563eb;text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:2px;" href="',
        body_html,
    )
    body_html = re.sub(
        r'<hr\s*/?>',
        '<hr style="border:none;border-top:1px solid #e2e8f0;margin:32px 0;"/>',
        body_html,
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Research Brief — {header_date}</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;">
<div style="max-width:720px;margin:0 auto;padding:32px 24px 48px 24px;background:#ffffff;">

<div style="padding-bottom:20px;border-bottom:3px solid #0f172a;margin-bottom:24px;">
<div style="font-family:-apple-system,'Segoe UI',Roboto,sans-serif;font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:#64748b;font-weight:600;">Research Brief</div>
<div style="font-family:-apple-system,'Segoe UI',Roboto,sans-serif;font-size:26px;font-weight:700;color:#0f172a;margin-top:6px;letter-spacing:-0.02em;">{header_date}</div>
<div style="font-family:-apple-system,'Segoe UI',Roboto,sans-serif;font-size:13px;color:#64748b;margin-top:12px;line-height:1.5;">
{len(items)} items across {len(FEEDS)} sources<br>
<span style="display:inline-block;margin-top:6px;">{source_chips_html}</span>
</div>
</div>

<div>
{body_html}
</div>

<div style="margin-top:48px;padding-top:20px;border-top:1px solid #e2e8f0;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;font-size:12px;color:#94a3b8;line-height:1.5;">
Generated by digest.py &middot; model: {MODEL} via claude&nbsp;-p &middot; {stats.get('duration_seconds', 0)}s
</div>

</div>
</body>
</html>
"""


# --- local file output + notification ---------------------------------------

def write_brief_to_disk(html: str, stats: dict, item_count: int) -> Path:
    """Write the HTML brief to a timestamped archive file and update the
    latest-brief.html pointer. Returns the path to the archive file."""
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    archive_path = BRIEFS_DIR / f"{now:%Y-%m-%d_%H-%M}.html"
    archive_path.write_text(html)
    # latest-brief.html always points at the newest run. We write a full copy
    # instead of a symlink so `open latest-brief.html` works even after the
    # archive is moved or deleted.
    LATEST_BRIEF.write_text(html)
    return archive_path


def notify_macos(title: str, subtitle: str, message: str) -> None:
    """Display a macOS notification bubble via osascript. Silent failure if
    notifications aren't available (e.g., running under launchd on a locked
    screen)."""
    if not SHOW_NOTIFICATION:
        return
    try:
        # Escape double-quotes for the AppleScript string
        def esc(s: str) -> str:
            return s.replace('"', '\\"')
        script = (
            f'display notification "{esc(message)}" '
            f'with title "{esc(title)}" '
            f'subtitle "{esc(subtitle)}" '
            f'sound name "Glass"'
        )
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass  # notification is best-effort


# --- main -------------------------------------------------------------------

def main() -> int:
    BASE_DIR.mkdir(exist_ok=True)
    log("=" * 56)
    log(f"digest run start · {len(FEEDS)} feed(s)")

    state = load_state()
    seen = set(state["seen_ids"])
    log(f"state has {len(seen)} seen ids")

    all_items = fetch_all_feeds()
    new_items = [it for it in all_items if it["id"] not in seen]
    # Dedup by id across feeds (same tweet in two lists)
    by_id: dict[str, dict] = {}
    for it in new_items:
        by_id.setdefault(it["id"], it)
    new_items = list(by_id.values())
    # Chronological, oldest first
    new_items.sort(key=lambda i: i.get("published", ""))
    log(f"{len(new_items)} new items after dedup")

    # Optional test/debug cap: TW_LIMIT=30 python3 digest.py
    limit_env = os.environ.get("TW_LIMIT")
    if limit_env and limit_env.isdigit():
        limit = int(limit_env)
        if 0 < limit < len(new_items):
            # Take the most recent N (keep latest context)
            new_items = new_items[-limit:]
            log(f"TW_LIMIT={limit} applied — processing most recent {limit} items")

    if not new_items:
        log("no new items — nothing to brief")
        return 0

    expand_links_for_items(new_items)

    try:
        claude_markdown, stats = summarize(new_items)
        log(
            f"summary generated ({len(claude_markdown)} chars) · "
            f"duration={stats['duration_seconds']}s"
        )
    except Exception as e:
        log(f"ERROR calling claude -p: {e}")
        return 3

    # Morning run (7am) prepends yesterday's Claude Code journal summary.
    # Set FORCE_MORNING_PREPEND=1 to test outside the 7am slot.
    is_morning_run = (
        datetime.now().hour == 7
        or os.environ.get("FORCE_MORNING_PREPEND", "").lower() in ("1", "true", "yes")
    )
    if is_morning_run:
        log("morning run: looking for yesterday's journal")
        yesterday_summary = load_yesterdays_journal()
        if yesterday_summary:
            claude_markdown = (
                yesterday_summary.strip()
                + "\n\n---\n\n"
                + claude_markdown.strip()
            )
            log(f"  prepended yesterday's journal ({len(yesterday_summary)} chars)")

    html = render_brief_html(claude_markdown, new_items, stats)
    archive_path = write_brief_to_disk(html, stats, len(new_items))
    log(f"brief written: {archive_path}")
    log(f"latest pointer:  {LATEST_BRIEF}")

    # macOS notification so you know a fresh brief is ready
    sources_by_cat: dict[str, int] = {}
    for it in new_items:
        c = it.get("category", "?")
        sources_by_cat[c] = sources_by_cat.get(c, 0) + 1
    top_cats = ", ".join(
        f"{v} {k}" for k, v in sorted(sources_by_cat.items(), key=lambda x: -x[1])[:3]
    )
    notify_macos(
        title="Research Brief ready",
        subtitle=f"{len(new_items)} items across {len(FEEDS)} sources",
        message=f"Top categories: {top_cats}",
    )

    state["seen_ids"].extend(it["id"] for it in new_items)
    save_state(state)
    log(f"state updated — now tracking {len(state['seen_ids'])} ids")
    log("digest run end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
