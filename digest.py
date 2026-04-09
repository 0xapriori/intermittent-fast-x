#!/usr/bin/env python3
"""Twitter digest: multi-feed, article expansion, plain-markdown email.

Fetches multiple rss.app feeds (each one turned into a feed from a private X
List), dedupes against state, pre-fetches any linked articles via trafilatura,
then invokes `claude -p` (Claude Code non-interactive) to synthesize a
themed digest that is delivered by Gmail SMTP.

By default this runs against your Claude Max subscription through the local
`claude` CLI — no Anthropic API key is required and there is no per-token or
per-search fee. If you prefer the paid API, see the README.

Secrets and per-user config:
  - config.json (gitignored)   : feeds, sender/recipient email, etc.
  - macOS keychain entry       : Gmail app password (service name from config)
  - Claude OAuth               : handled transparently by `claude -p` on first
                                  interactive login; no setup needed in-script.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.mime.text import MIMEText
from html import unescape
from pathlib import Path

import feedparser
import requests
import trafilatura

# --- paths ------------------------------------------------------------------

# Everything the script reads or writes lives next to the script itself,
# not in a hard-coded absolute path. This makes the tool portable and safe to
# clone to any machine.
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"


# --- config loading ---------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise SystemExit(
            f"Missing {CONFIG_FILE}. Copy config.example.json to config.json "
            f"and fill in your feeds + email addresses."
        )
    with open(CONFIG_FILE) as f:
        return json.load(f)


CONFIG = load_config()

FEEDS: list[dict] = CONFIG["feeds"]
EMAIL_FROM: str = CONFIG["email"]["from"]
EMAIL_TO: str = CONFIG["email"]["to"]
GMAIL_KEYCHAIN_SERVICE: str = CONFIG.get("gmail_keychain_service", "twitter-digest-gmail")

MODEL: str = CONFIG.get("model", "claude-opus-4-6")
CLAUDE_CLI_TIMEOUT: int = CONFIG.get("claude_cli_timeout_seconds", 900)
WEB_SEARCH_BUDGET_HINT: int = CONFIG.get("web_search_budget_hint", 10)
MAX_SEEN_IDS: int = CONFIG.get("max_seen_ids", 2000)

# Link expansion
LINK_FETCH_TIMEOUT: int = CONFIG.get("link_fetch_timeout_seconds", 7)
LINK_MAX_CONTENT_CHARS: int = CONFIG.get("link_max_content_chars", 3500)
LINK_MAX_PER_TWEET: int = CONFIG.get("link_max_per_tweet", 3)
LINK_CONCURRENCY: int = CONFIG.get("link_concurrency", 10)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Hosts where linked content isn't worth fetching (X itself, images, video)
SKIP_HOSTS = {
    "twitter.com", "x.com", "mobile.twitter.com", "mobile.x.com",
    "pic.twitter.com", "pic.x.com", "pbs.twimg.com", "abs.twimg.com",
    "video.twimg.com",
    "youtube.com", "youtu.be", "m.youtube.com",
    "tiktok.com", "vm.tiktok.com",
    "instagram.com",
    "apps.apple.com", "play.google.com",
}
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


def keychain_get(service: str) -> str:
    """Read a generic password from the macOS login keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-a",
         os.environ.get("USER", ""), "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"keychain read failed for '{service}': {result.stderr.strip()}"
        )
    return result.stdout.strip()


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


def extract_urls(tweet_text: str, tweet_description_html: str) -> list[str]:
    """Pull URLs from both the HTML description and text body."""
    urls: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', tweet_description_html or ""):
        u = m.group(1)
        if u not in seen:
            seen.add(u)
            urls.append(u)
    # Plain-text URLs. Reject anything containing a horizontal ellipsis —
    # those come from rss.app truncating display text for retweets.
    for m in re.finditer(r"https?://[^\s<>\"'\u2026]+", tweet_text or ""):
        u = m.group(0).rstrip(".,);:!?")
        if "\u2026" in u or u.endswith("..."):
            continue
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def fetch_all_feeds() -> list[dict]:
    all_items: list[dict] = []
    for feed_cfg in FEEDS:
        try:
            parsed = feedparser.parse(feed_cfg["url"])
            if parsed.bozo and not parsed.entries:
                log(f"  ✗ {feed_cfg['name']}: parse failed ({parsed.bozo_exception})")
                continue
            for entry in parsed.entries:
                guid = entry.get("id") or entry.get("guid") or entry.get("link")
                if not guid:
                    continue
                desc_html = entry.get("summary", "") or entry.get("description", "")
                text = strip_tweet_html(desc_html)
                all_items.append({
                    "id": guid,
                    "feed": feed_cfg["name"],
                    "author": entry.get("author", "unknown"),
                    "text": text,
                    "description_html": desc_html,
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "urls": extract_urls(text, desc_html),
                })
            log(f"  ✓ {feed_cfg['name']}: {len(parsed.entries)} items")
        except Exception as e:
            log(f"  ✗ {feed_cfg['name']}: {e}")
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
    if "/i/article/" in url or "/i/web/status/" in url:
        return False
    lowered = url.lower()
    if lowered.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp",
                         ".mp4", ".mov", ".pdf", ".zip", ".dmg")):
        return False
    return True


def resolve_shortener(url: str) -> str:
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
    """Fetch a URL and extract main article text via trafilatura."""
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
    """For each item, populate an 'articles' list of fetched article contents."""
    url_to_items: dict[str, list[dict]] = {}
    for it in items:
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
        it["articles"] = []

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


# --- prompt construction ----------------------------------------------------

def build_prompt(items: list[dict]) -> str:
    blocks: list[str] = []
    for i, it in enumerate(items, 1):
        text = it["text"].strip()
        is_rt = text.startswith("RT @") or text.startswith("RT ")
        rt_flag = " [RT]" if is_rt else ""
        header = f"[{i}] {it['author']}{rt_flag} · list={it['feed']} · {it['published']}"
        parts = [header, text, f"tweet_link: {it['link']}"]
        for art in it.get("articles", []):
            art_block = (
                f"    └── LINKED ARTICLE ({art['host']})\n"
                f"        title: {art['title']}\n"
                f"        url:   {art['url']}\n"
                f"        excerpt: {art['text']}"
            )
            parts.append(art_block)
        blocks.append("\n".join(parts))
    tweets_block = "\n\n".join(blocks)

    feed_names = ", ".join(f["name"] for f in FEEDS)

    sections_cfg = CONFIG.get("sections", {})
    mandatory = sections_cfg.get("mandatory", ["Ethereum", "Solana", "AI"])
    optional = sections_cfg.get("optional", ["Bitcoin"])
    exclusions = sections_cfg.get("exclusions", [
        "politics, elections, politicians, culture war, ideology",
        "price speculation, TA charts, 'wen moon'",
        "personal drama, Twitter fights",
    ])

    mandatory_block = "\n".join(
        f"{i+1}. `## {name}`" for i, name in enumerate(mandatory)
    )
    optional_block = ", ".join(f"`## {name}`" for name in optional) if optional else "(none)"
    exclusions_block = "\n".join(f"- {e}" for e in exclusions)

    return f"""You are producing a signal-driven digest of recent tweets from these Twitter Lists: {feed_names}. The reader does NOT want to visit x.com. Your job is to tell them WHAT HAPPENED and WHAT IS BEING DISCUSSED in the last window — not to list individual tweets.

## Core framing: events over tweets

- Focus on EVENTS and DISCUSSIONS, not individual accounts. Your unit of output is "a thing that happened or is being talked about", not "@someone tweeted X".
- Consolidate across the feed: if 5 accounts are all discussing the same protocol launch, that becomes ONE bullet with the underlying news, not 5 bullets.
- Only break out an individual tweet as its own bullet if it's an original, highly substantive take from a credible voice publishing a novel analysis or announcement directly. Reactions, memes, and vague takes are NOT substantive.
- Scale volume to substance: a quiet window gets a short digest. Don't pad.

## Use WebSearch and WebFetch to find the real stories

You have WebSearch and WebFetch tools available. Most tweets are truncated retweets or short references to news without links — search to find the actual source. **Use approximately {WEB_SEARCH_BUDGET_HINT} searches for a full-size batch**, more for large batches, fewer for small ones. Err on the side of searching more, not less.

Search WHEN:
- A tweet references a protocol launch, governance vote, hack, fundraise, partnership, release, or data point without a link → find the underlying announcement / news article / blog post / GitHub release
- Multiple tweets reference the same event → ONE search, consolidate
- An unfamiliar project or term needs a sentence of context → quick search
- A stat is claimed without a source → verify and link the data source
- You're not sure if an event is real or important → search to verify before dismissing

Do NOT search for:
- Memes, banter, vibes, reactions with no specific claim
- Anything already covered by a LINKED ARTICLE provided inline
- Topics unrelated to your mandatory sections (waste of budget)

## Mandatory section structure

Your digest MUST always include these sections in this order:

{mandatory_block}

Optionally include {optional_block} ONLY IF there is substantive content for it. If there is no content, OMIT the section entirely. Do not print an empty optional section header.

**Do not dismiss content too easily.** Before marking a mandatory section "nothing substantial", check that you have genuinely considered every tweet in the batch for that theme.

**Minimum depth when content exists**: if a section has ANY relevant content in the batch, produce at least 2 substantive bullets.

**Empty mandatory sections**: write exactly this single line under the header, nothing else: `_Nothing substantial this window._`

## Hard exclusions

Drop content matching any of these categories entirely — do not mention, do not search for:

{exclusions_block}

If a tweet mixes an excluded topic with a substantive on-topic fact (e.g., "the president's crypto EO"), keep the substantive fact and drop the excluded framing.

## Output format — PLAIN MARKDOWN

Output ONLY these markdown elements:

- `## Section` for the section headers
- `- ` bullet points (one per event/discussion, 1-3 sentences each)
- `**bold**` for key facts: numbers, protocol names, @handles when directly relevant, and the core "thing that happened"
- `[link label](https://url)` for real external URLs. Prefer official blogs > news articles > GitHub > tweet URLs as last resort. Never link to x.com/twitter.com unless there is no alternative.
- `_italic_` sparingly for hedges or qualifiers
- `> quote` blockquote only when a verbatim line is genuinely worth preserving

**Link requirement**: every substantive bullet MUST end with at least one `[label](url)` link to an external source. No exceptions.

**NO preamble.** Do NOT write "Here's the digest" or "I'll search for..." or "Based on the tweets..." or ANY intro text. Your very first characters of output must be `## {mandatory[0]}`. Anything before that will be stripped and discarded.

**NO tweet-number references.** Never write "(tweet 11)" or "tweet #5" or "item 3" or any numeric reference to the input list. The tweet numbering in the input is internal scaffolding — the reader will never see it. Refer to the account (@handle) or the topic instead.

## Tweets ({len(items)})

{tweets_block}
"""


# --- summarization ----------------------------------------------------------

def _strip_preamble(markdown: str) -> str:
    """Drop anything before the first `## ` heading."""
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
        raise RuntimeError(f"claude -p timed out after {CLAUDE_CLI_TIMEOUT}s")

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


# --- email body (plain markdown) --------------------------------------------

def render_email_body(claude_markdown: str, items: list[dict], stats: dict) -> str:
    now = datetime.now()
    header_date = now.strftime("%A %b %-d, %Y · %-I:%M %p %Z").strip(" ·")
    feed_names = ", ".join(f["name"] for f in FEEDS)
    article_count = sum(len(it.get("articles", [])) for it in items)

    header = (
        f"# Twitter Digest — {header_date}\n"
        f"\n"
        f"{len(items)} tweets across {len(FEEDS)} lists: {feed_names}\n"
        f"{article_count} linked articles pre-expanded\n"
        f"\n"
        f"---\n"
        f"\n"
    )

    footer = (
        f"\n\n---\n"
        f"\n"
        f"_Generated by digest.py · model: {MODEL} via claude -p · "
        f"{stats.get('duration_seconds', 0)}s_\n"
    )

    return header + claude_markdown.strip() + footer


# --- email ------------------------------------------------------------------

def send_email(subject: str, body: str, gmail_password: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(EMAIL_FROM, gmail_password)
        s.send_message(msg)


# --- main -------------------------------------------------------------------

def main() -> int:
    log("=" * 56)
    log(f"digest run start · {len(FEEDS)} feed(s)")

    state = load_state()
    seen = set(state["seen_ids"])
    log(f"state has {len(seen)} seen ids")

    all_items = fetch_all_feeds()
    new_items = [it for it in all_items if it["id"] not in seen]
    by_id: dict[str, dict] = {}
    for it in new_items:
        by_id.setdefault(it["id"], it)
    new_items = list(by_id.values())
    new_items.sort(key=lambda i: i.get("published", ""))
    log(f"{len(new_items)} new items after dedup")

    # Optional test/debug cap: TW_LIMIT=30 python3 digest.py
    limit_env = os.environ.get("TW_LIMIT")
    if limit_env and limit_env.isdigit():
        limit = int(limit_env)
        if 0 < limit < len(new_items):
            new_items = new_items[-limit:]
            log(f"TW_LIMIT={limit} applied — processing most recent {limit} items")

    if not new_items:
        log("no new items — no email sent")
        return 0

    expand_links_for_items(new_items)

    try:
        gmail_password = keychain_get(GMAIL_KEYCHAIN_SERVICE)
    except RuntimeError as e:
        log(f"ERROR reading keychain: {e}")
        return 2

    try:
        claude_markdown, stats = summarize(new_items)
        log(
            f"summary generated ({len(claude_markdown)} chars) · "
            f"duration={stats['duration_seconds']}s"
        )
    except Exception as e:
        log(f"ERROR calling claude -p: {e}")
        return 3

    body = render_email_body(claude_markdown, new_items, stats)
    (BASE_DIR / "last-digest.md").write_text(body)

    now = datetime.now()
    subject = (
        f"[tw digest] {len(new_items)} · "
        f"{now.strftime('%a %b ')}{now.day} {now.strftime('%-I:%M %p')}"
    )

    try:
        send_email(subject, body, gmail_password)
        log(f"email sent: {subject}")
    except Exception as e:
        log(f"ERROR sending email: {e}")
        return 4

    state["seen_ids"].extend(it["id"] for it in new_items)
    save_state(state)
    log(f"state updated — now tracking {len(state['seen_ids'])} ids")
    log("digest run end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
