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
from datetime import datetime, timedelta, timezone
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
TOPICS_FILE = BASE_DIR / "recent-topics.md"
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
HOME = BASE_DIR  # back-compat for any remaining references

# Sources come from config.json. Each entry: {name, url, category, max_items?}.
FEEDS: list[dict] = CONFIG.get("sources") or []
if not FEEDS:
    raise SystemExit("config.json has no 'sources' list")

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


def _resolve_path(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


_output_cfg = CONFIG.get("output", {})
BRIEFS_DIR = _resolve_path(_output_cfg.get("briefs_dir", str(BASE_DIR / "briefs")))
LATEST_BRIEF = _resolve_path(
    _output_cfg.get("latest_pointer", str(BASE_DIR / "latest-brief.html"))
)
SHOW_NOTIFICATION: bool = bool(_output_cfg.get("show_macos_notification", True))

MAX_SEEN_IDS: int = CONFIG.get("max_seen_ids", 2000)
CLAUDE_CLI_TIMEOUT: int = CONFIG.get("claude_cli_timeout_seconds", 1800)
WEB_SEARCH_BUDGET_HINT: int = CONFIG.get("web_search_budget_hint", 10)

# Topic-level dedup: load previous briefs so Claude can avoid re-synthesizing
# the same stories that ran in earlier briefs. GUID-level dedup handles
# individual items but stories span multiple tweets/posts over days.
RECENT_BRIEFS_COUNT: int = CONFIG.get("recent_briefs_for_dedup", 3)
RECENT_BRIEFS_HOURS: int = CONFIG.get("recent_briefs_hours", 48)

# Post-synthesis topic filter: a second short `claude -p` pass marks each
# bullet KEEP / COLLAPSE / DROP against recent-topics.md so the draft doesn't
# reship stories already covered in recent briefs. GUID dedup handles items;
# this handles topic-level rehash (same story, fresh tweets).
TOPIC_COOLDOWN_HOURS: int = CONFIG.get("topic_cooldown_hours", 72)
TOPIC_FILTER_MODEL: str = CONFIG.get("topic_filter_model", "claude-sonnet-4-6")
TOPIC_FILTER_TIMEOUT_SECONDS: int = CONFIG.get("topic_filter_timeout_seconds", 300)
TOPIC_FILTER_ENABLED: bool = bool(CONFIG.get("topic_filter_enabled", True))

# Telegram delivery (opt-in, config.output.telegram.enabled)
_telegram_cfg = _output_cfg.get("telegram", {}) if isinstance(_output_cfg, dict) else {}
TELEGRAM_ENABLED: bool = bool(_telegram_cfg.get("enabled", False))
TELEGRAM_CHAT_ID: str = str(_telegram_cfg.get("chat_id", "") or "")
TELEGRAM_KEYCHAIN_SERVICE: str = _telegram_cfg.get(
    "keychain_service", "twitter-digest-telegram"
)
TELEGRAM_KEY_TWEETS: int = int(_telegram_cfg.get("key_tweets", 5))

# Link expansion
LINK_FETCH_TIMEOUT: int = CONFIG.get("link_fetch_timeout_seconds", 7)
LINK_MAX_CONTENT_CHARS: int = CONFIG.get("link_max_content_chars", 3500)
LINK_MAX_PER_TWEET: int = CONFIG.get("link_max_per_tweet", 3)
LINK_CONCURRENCY: int = CONFIG.get("link_concurrency", 10)

# Section config — user-overridable via CONFIG["sections"]
_sections_cfg = CONFIG.get("sections", {})
MANDATORY_SECTIONS: list[str] = _sections_cfg.get(
    "mandatory", ["Ethereum", "Solana", "AI", "Hacker News"]
)
OPTIONAL_SECTIONS: list[str] = _sections_cfg.get("optional", ["Bitcoin"])
EXCLUSIONS: list[str] = _sections_cfg.get(
    "exclusions",
    [
        "politics, elections, politicians, government policy debates, "
        "geopolitical conflict, culture war, ideology.",
        "crypto price speculation, TA charts, or 'wen moon' content "
        "unless it's a major macro shift with a concrete catalyst.",
        "personal drama, beef, or Twitter fights unless they're about "
        "a protocol's technical direction.",
    ],
)

SECTION_GUIDANCE: dict[str, str] = {
    "Ethereum":    "ETH core, L2s/rollups (Base, Arbitrum, Optimism, etc.), DeFi on ETH, restaking/LSTs, MEV, ETH-ecosystem apps and tooling",
    "Solana":      "SOL core, Solana DeFi, memecoin dynamics, Phantom/Jito/Jupiter/Pump.fun, Solana ecosystem apps",
    "AI":          "AI models, agents, Anthropic/OpenAI/Google/xAI/Meta, AI x crypto, ML infra, agentic commerce, AI tooling. This is the full AI industry, not just AI-crypto crossover.",
    "Hacker News": 'The top stories trending on HN frontpage right now, across any topic (not just AI/crypto). See the "Hacker News section requirements" below for specifics.',
    "Bitcoin":     "BTC core, ordinals, Lightning, ETF flows, regulatory news.",
}
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


def keychain_get(service: str) -> str:
    """Read a generic password from the macOS login keychain.

    Used for the Telegram bot token (service name from config) and any
    other small secret we don't want baked into source or config.json."""
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
    """Atomic write: serialize to state.json.tmp, then rename.

    os.replace() is atomic on POSIX filesystems, so state.json is either
    fully the old content or fully the new content — never half-written,
    even if the process is killed during the write."""
    state["seen_ids"] = state["seen_ids"][-MAX_SEEN_IDS:]
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


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


FEED_FETCH_TIMEOUT = 30  # seconds — hard cap per feed, prevents single-feed hangs
FORUM_ACTIVITY_HOURS = CONFIG.get("forum_activity_hours", 48)  # only keep forum topics bumped within this window


def _fetch_discourse_json(rss_url: str) -> dict | None:
    """Given a Discourse RSS URL like https://ethresear.ch/latest.rss,
    fetch the corresponding JSON endpoint and return the parsed result.
    Returns None on any failure (caller falls back to RSS)."""
    if ".rss" not in rss_url:
        return None
    json_url = rss_url.replace(".rss", ".json")
    try:
        r = requests.get(
            json_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=FEED_FETCH_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _parse_discourse_topics_as_items(
    data: dict,
    feed_name: str,
    category: str,
    max_items: int,
    source_url: str,
    activity_hours: int,
) -> tuple[list[dict], int]:
    """Convert a Discourse /latest.json response into our standard item
    dicts, filtering to topics bumped within the last N hours so only
    genuinely active discussions flow into the brief.

    Returns (items, total_topics_considered).
    """
    topics = (data.get("topic_list") or {}).get("topics", [])
    now = datetime.now(timezone.utc)

    def _hours_ago(iso: str) -> float:
        if not iso:
            return 9999.0
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return (now - dt).total_seconds() / 3600
        except Exception:
            return 9999.0

    fresh = [t for t in topics if _hours_ago(t.get("bumped_at", "")) <= activity_hours]
    # Sort by most-recently-bumped first
    fresh.sort(key=lambda t: t.get("bumped_at", ""), reverse=True)
    fresh = fresh[:max_items]

    # Derive the base URL from the source rss URL (strip "/latest.rss" etc.)
    base = re.sub(r"/(latest|top|new|unread|hot)\.(rss|json).*$", "", source_url)
    if not base:
        base = source_url.rsplit("/", 1)[0]

    items: list[dict] = []
    for t in fresh:
        slug = t.get("slug", "")
        tid = t.get("id", 0)
        if slug and tid:
            link = f"{base}/t/{slug}/{tid}"
        elif tid:
            link = f"{base}/t/{tid}"
        else:
            continue

        excerpt = (t.get("excerpt") or "").strip()
        reply_count = t.get("reply_count", 0) or 0
        posts_count = t.get("posts_count", 1) or 1
        bumped_ago = _hours_ago(t.get("bumped_at", ""))
        created_ago = _hours_ago(t.get("created_at", ""))
        last_poster = t.get("last_poster_username", "") or "unknown"

        # Tag the text with activity metadata so Claude knows what's
        # actually fresh vs what's just bumped.
        if bumped_ago < 1:
            bump_str = f"bumped {int(bumped_ago * 60)}m ago"
        else:
            bump_str = f"bumped {bumped_ago:.1f}h ago"
        if created_ago < 24:
            create_str = "new post"
        elif created_ago < 168:
            create_str = f"posted {created_ago / 24:.1f}d ago"
        else:
            create_str = f"posted {created_ago / 24:.0f}d ago"

        activity_prefix = (
            f"[{bump_str}, {create_str}, {reply_count} replies, "
            f"{posts_count} posts] "
        )
        text_body = activity_prefix + excerpt

        items.append({
            "id": f"discourse-{base}-{tid}",
            "feed": feed_name,
            "category": category,
            "author": last_poster,
            "title": t.get("title", ""),
            "text": text_body[: CONTENT_MAX_CHARS_BY_CATEGORY.get(category, 1500)],
            "description_html": excerpt,
            "link": link,
            "published": t.get("bumped_at", ""),
            "urls": [],
        })

    return items, len(topics)


def _fetch_feed_bytes(url: str) -> bytes | None:
    """Fetch a feed URL with a hard timeout, return raw bytes or None on error.

    feedparser.parse() has no timeout parameter and will silently hang on
    slow servers (we saw 15-minute gaps on individual feeds during the
    April 9 outage). Pre-fetching with requests.get(timeout=...) gives us
    a guaranteed upper bound, then we hand the bytes to feedparser.
    """
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
            timeout=FEED_FETCH_TIMEOUT,
            allow_redirects=True,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.content
    except Exception as e:
        # Re-raise with a short description so the caller can log it
        raise RuntimeError(str(e)[:200])


# Collected at runtime so we can surface them in the brief itself
FAILED_FEEDS: list[dict] = []


def fetch_all_feeds() -> list[dict]:
    """Fetch every configured feed and return a list of entries tagged with
    feed name and source category. Individual-feed failures are logged and
    recorded in FAILED_FEEDS but do NOT stop the run."""
    FAILED_FEEDS.clear()
    all_items: list[dict] = []
    for feed_cfg in FEEDS:
        category = feed_cfg.get("category", "x-twitter")
        max_items = feed_cfg.get("max_items")
        name = feed_cfg["name"]
        url = feed_cfg["url"]

        # For forum-category feeds that look like Discourse, prefer the
        # JSON endpoint + activity filter. This correctly surfaces old
        # threads with new replies and skips old threads with no activity.
        if category == "forum" and ".rss" in url:
            data = _fetch_discourse_json(url)
            if data:
                items, total_scanned = _parse_discourse_topics_as_items(
                    data,
                    feed_name=name,
                    category=category,
                    max_items=max_items or 15,
                    source_url=url,
                    activity_hours=FORUM_ACTIVITY_HOURS,
                )
                all_items.extend(items)
                log(
                    f"  ✓ {name:28s} [{category:10s}] "
                    f"{len(items)} active (of {total_scanned} topics, "
                    f"bumped ≤{FORUM_ACTIVITY_HOURS}h)"
                )
                continue
            # JSON fetch failed — fall through to RSS as before
            log(f"  {name}: discourse JSON failed, falling back to RSS")

        try:
            raw = _fetch_feed_bytes(url)
            parsed = feedparser.parse(raw)
            if parsed.bozo and not parsed.entries:
                reason = f"parse failed ({parsed.bozo_exception})"
                log(f"  ✗ {name:28s} [{category:10s}] {reason}")
                FAILED_FEEDS.append({"name": name, "category": category, "reason": reason})
                continue
            entries = parsed.entries
            if max_items:
                entries = entries[:max_items]
            item_count = 0
            for entry in entries:
                guid = entry.get("id") or entry.get("guid") or entry.get("link")
                if not guid:
                    continue
                desc_html = entry.get("summary", "") or entry.get("description", "")
                title = entry.get("title", "")
                text = normalize_content(category, desc_html, title)
                all_items.append({
                    "id": guid,
                    "feed": name,
                    "category": category,
                    "author": entry.get("author", "unknown"),
                    "title": title,
                    "text": text,
                    "description_html": desc_html,
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "urls": extract_urls(text, desc_html),
                })
                item_count += 1
            log(f"  ✓ {name:28s} [{category:10s}] {item_count} items")
        except Exception as e:
            reason = str(e)[:200]
            log(f"  ✗ {name:28s} [{category:10s}] {reason}")
            FAILED_FEEDS.append({"name": name, "category": category, "reason": reason})
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

def build_prompt(
    items: list[dict],
    defillama_text: str = "",
    recent_briefs: str = "",
    tools_available: bool = True,
) -> str:
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

    # --- dynamic section / exclusion scaffolding from config ---
    mandatory_count = len(MANDATORY_SECTIONS)
    mandatory_section_list = "\n".join(
        f"{i+1}. `## {name}` — {SECTION_GUIDANCE.get(name, name)}"
        for i, name in enumerate(MANDATORY_SECTIONS)
    )

    if "Hacker News" in MANDATORY_SECTIONS:
        hn_requirements_block = """## Hacker News section requirements

The Hacker News input category contains the current HN frontpage (top stories by points/comments). You MUST summarize them in a dedicated `## Hacker News` section with the following rules:

- **4-8 bullets**, one per meaningful story. Pick the highest-signal items — prioritize technical releases, research, novel tools, substantive writing, industry news. Skip pure rage-bait, off-topic memes, and low-effort link farms.
- **Scope is NOT limited to AI/crypto.** HN covers the entire tech world. Include anything a curious technical reader would find substantive: new programming languages, OS/kernel news, hardware launches, science papers, infrastructure research, novel products, postmortems, long-form essays, legal/policy news affecting tech, etc.
- Each bullet format: `- **<Story title or topic>**: 1-2 sentences of summary/context. [HN discussion](url) · [Source](url)`
- If the HN story links to an external article, link BOTH the HN comments page (usually https://news.ycombinator.com/item?id=...) AND the source URL, so the reader can choose discussion or article.
- If a top story is already covered in another mandatory section, skip it here to avoid duplication. Note at the end: "(Top AI stories covered in the AI section above.)"
- **Do NOT dismiss this section with "nothing substantial".** HN frontpage always has content; the job is to find the 4-8 most interesting items and explain them."""
    else:
        hn_requirements_block = ""

    if OPTIONAL_SECTIONS:
        optional_parts = []
        for name in OPTIONAL_SECTIONS:
            guide = SECTION_GUIDANCE.get(name, "")
            suffix = f" ({guide})" if guide else ""
            optional_parts.append(f"`## {name}`{suffix}")
        optional_list_str = ", ".join(optional_parts)
        optional_section_clause = (
            f"Optionally include {optional_list_str} ONLY IF there is substantive "
            f"content for it. **If there is no content for an optional section, "
            f"OMIT the section entirely. Do not print an empty header.**"
        )
    else:
        optional_section_clause = ""

    exclusions_block = "\n".join(f"- {e}" for e in EXCLUSIONS)
    first_section = MANDATORY_SECTIONS[0] if MANDATORY_SECTIONS else "Ethereum"

    if tools_available:
        search_instructions = f"""## Use WebSearch and WebFetch to find the real stories

You have WebSearch and WebFetch tools. Most tweets are truncated retweets without links — search to find the actual source. **Use approximately {WEB_SEARCH_BUDGET_HINT} searches for a full-size batch**, more for large batches. There is no per-search fee.

Search WHEN:
- A tweet / forum post / HN title references a launch, vote, hack, fundraise, partnership, release, paper, or data point without a link → find the underlying source
- Multiple items reference the same event → ONE search, consolidate
- An unfamiliar project or term needs a sentence of context → quick search
- A stat is claimed without a source → verify and link the data source

Do NOT search for:
- Memes, banter, vibes, reactions with no specific claim
- Anything already covered by a LINKED ARTICLE or sufficient show notes / release description
- HN stories where the title + description already give you enough context"""
    else:
        search_instructions = """## IMPORTANT: No web search tools available this run

Web search timed out on the primary attempt. You are running WITHOUT WebSearch or WebFetch.
Work EXCLUSIVELY from the source material, linked article excerpts, and DeFi/MEV data provided below.
Do NOT request tools, do NOT ask for permissions, do NOT mention that tools are unavailable.
Write your brief as normal using what you have. If you can't verify a claim, use the tweet URL as the source link."""

    # Topic-level dedup block — only included when we have recent briefs
    if recent_briefs:
        recent_briefs_block = f"""## CRITICAL: Avoid repeating topics from recent briefs

The user has already seen the briefs below in the last 48 hours. **DO NOT re-explain stories that were already covered** unless today's source material contains **materially new information** — a concrete follow-up, new data, a resolution, a contradiction, or the next step.

Fresh framing of stale content is NOT acceptable. If you would be saying the same thing with different words, DROP IT. The reader wants NEW signal, not reminders.

Brief one-line acknowledgment of continuing stories is fine only if there's genuinely new info:
  ✗ BAD: "The Drift Protocol lost $285M to North Korean attackers in a six-month social engineering campaign."
  ✓ OK:  "The Drift investigation continues: Circle now faces a class action over its refusal to freeze the $232M USDC."
  ✓ BETTER: [drop it entirely unless there's new news]

<recent_briefs>
{recent_briefs}
</recent_briefs>

Remember: if today's source material is genuinely thin on NEW stories because recent briefs already covered everything, produce a SHORT brief. A 3-bullet brief saying "nothing major has changed since yesterday" is more valuable than padding with retreads.

---

"""
    else:
        recent_briefs_block = ""

    return f"""{recent_briefs_block}You are producing a signal-driven multi-source digest for a crypto/AI researcher. They do NOT want to visit x.com, read dozens of podcast show notes, skim five governance forums, watch GitHub release feeds, or scan Hacker News themselves. Your job is to synthesize WHAT HAPPENED and WHAT IS BEING DISCUSSED across all sources, grounded in real external links.

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

{search_instructions}

## Mandatory section structure

Your digest MUST always include these {mandatory_count} sections in this order:

{mandatory_section_list}

{hn_requirements_block}

{optional_section_clause}

**Do not dismiss content too easily.** Check across ALL source types before marking a section empty.

**Minimum depth when content exists**: if a section has ANY relevant content in the batch, produce **at least 2 substantive bullets**. Don't stop at 1.

**Empty mandatory sections**: write exactly this single line under the header, nothing else: `_Nothing substantial this window._`

## Hard exclusions

{exclusions_block}

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

REMEMBER: first output characters must be `## {first_section}`. No preamble, no meta-commentary, no "I'll search".

## Required final section: Key Tweets

After all your content sections, add a FINAL section titled exactly `## Key Tweets`. In it, output exactly {TELEGRAM_KEY_TWEETS} tweet URLs — the ones from the X TWEETS source material that best represent the most interesting, share-worthy, or foundational stories of this batch. These will be used as standalone link previews in a push notification, so pick:

- Tweets that have substantive original content (not just reactions/memes)
- A mix across your mandatory sections where possible (don't pick all from one theme)
- High-signal primary sources over commentary/retweets when you have the choice
- The tweet URLs (the `tweet_link:` value in the source material), NOT links from web search

Format the section as plain URLs, one per line, no commentary, no markdown formatting:

```
## Key Tweets

https://x.com/author/status/123
https://x.com/author/status/456
https://x.com/author/status/789
https://x.com/author/status/012
https://x.com/author/status/345
```

If there are genuinely fewer than {TELEGRAM_KEY_TWEETS} worth-sharing tweets, output fewer — don't pad with junk. This section will be STRIPPED from the document the reader opens; it exists only to drive link previews in a separate channel.

{defillama_text}

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


def _run_claude_with_watchdog(cmd: list[str], prompt: str, timeout_s: int) -> subprocess.CompletedProcess:
    """Run a subprocess with a REAL timeout that actually kills the child.

    subprocess.run(timeout=...) has a known failure mode on macOS where it
    can hang indefinitely inside communicate() if the child process doesn't
    return promptly — the timeout exception never fires and the parent stays
    blocked. We observed this in production: a claude -p child hung for 3+
    hours on a stalled network call and subprocess.run never raised.

    This helper uses Popen + a watchdog poll loop. We escalate:
      1. At timeout: SIGTERM (graceful)
      2. 5 seconds later if still alive: SIGKILL (hard)
    We always return OR raise RuntimeError — never hang forever.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Write the prompt to stdin and close it, so the child can start consuming.
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except (BrokenPipeError, OSError) as e:
        proc.kill()
        proc.wait(timeout=5)
        raise RuntimeError(f"failed to write prompt to claude -p: {e}")

    deadline = datetime.now().timestamp() + timeout_s
    poll_interval = 2.0

    while True:
        rc = proc.poll()
        if rc is not None:
            # Child exited — drain remaining output
            stdout = proc.stdout.read() or ""
            stderr = proc.stderr.read() or ""
            return subprocess.CompletedProcess(
                args=cmd, returncode=rc, stdout=stdout, stderr=stderr,
            )
        if datetime.now().timestamp() >= deadline:
            # Graceful kill first
            log(f"  claude -p exceeded {timeout_s}s — sending SIGTERM")
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            # Wait up to 5s for graceful exit
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log("  child didn't exit on SIGTERM — sending SIGKILL")
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
            # Try to drain whatever output was produced
            try:
                stdout = proc.stdout.read() or ""
                stderr = proc.stderr.read() or ""
            except Exception:
                stdout = stderr = ""
            raise RuntimeError(
                f"claude -p killed by watchdog after {timeout_s}s "
                f"(last stderr: {stderr[:300]!r})"
            )
        __import__("time").sleep(poll_interval)


def _warmup_claude_session() -> bool:
    """Ping `claude -p` with a trivial prompt before the heavy synthesis.

    Three consecutive morning runs (2026-04-14/15/16) hung on `claude -p`
    inside both watchdog windows, while the same invocation works fine
    minutes later from an interactive shell. The only thing special about
    the morning context is the Mac woke from sleep shortly before. The
    working theory: the first post-wake `claude -p` call hangs on a stale
    OAuth/session state that a short ping forces to re-establish.

    We retry with increasing budgets (30/60/120s). On success the main
    synthesis is cheap — the session is warm. On total failure we log and
    proceed anyway; the main call's own watchdogs still protect against
    the worst case.
    """
    budgets = [30, 60, 120]
    cmd = ["claude", "-p", "--model", MODEL]
    for i, timeout in enumerate(budgets, 1):
        log(f"  warmup attempt {i}/{len(budgets)} ({timeout}s budget)")
        try:
            result = _run_claude_with_watchdog(cmd, "Respond with OK.", timeout)
        except RuntimeError as e:
            log(f"  warmup attempt {i} hung: {e}")
            continue
        if result.returncode == 0 and (result.stdout or "").strip():
            log("  warmup: session live")
            return True
        log(f"  warmup attempt {i}: rc={result.returncode} "
            f"stderr={(result.stderr or '').strip()[:120]!r}")
    log("  warmup: all attempts failed — proceeding with main synthesis anyway")
    return False


def summarize(
    items: list[dict],
    defillama_text: str = "",
    recent_briefs: str = "",
) -> tuple[str, dict]:
    """Invoke `claude -p` (Claude Code non-interactive) to summarize.

    Uses the authenticated Max subscription via OAuth. Grants the session
    WebSearch and WebFetch tools so Claude can research tweet topics
    without an API web_search add-on. Optionally includes a text version
    of the DefiLlama snapshot so the narrative can reference live numbers,
    and a block of recent briefs so the model actively avoids repeating
    stories already covered in the last 48 hours.
    """
    _warmup_claude_session()
    prompt_with_tools = build_prompt(
        items, defillama_text=defillama_text, recent_briefs=recent_briefs,
        tools_available=True,
    )
    prompt_without_tools = build_prompt(
        items, defillama_text=defillama_text, recent_briefs=recent_briefs,
        tools_available=False,
    )

    # Persist the prompt for debugging / reproducibility
    (HOME / "last-prompt.md").write_text(prompt_with_tools)

    start = datetime.now()
    cmd = [
        "claude", "-p",
        "--model", MODEL,
        "--allowedTools", "WebSearch,WebFetch",
    ]
    log(f"  invoking: {' '.join(cmd)}")

    # Retry cascade: attempt with WebSearch first (the valuable path),
    # fall back to no-tools if it hangs (the reliable path). Most runs
    # succeed on attempt 1. Failed runs degrade gracefully instead of
    # producing nothing — which is what the user actually needs.
    # Bumped from 600/300: two consecutive morning runs failed when the Mac
    # woke late and claude -p hit a stale-network hang inside the old window.
    # 900s gives a cushion for post-wake network warmup before the retry.
    ATTEMPT_1_TIMEOUT = min(CLAUDE_CLI_TIMEOUT, 900)  # 15 min with tools
    ATTEMPT_2_TIMEOUT = 450                            # 7.5 min without tools

    for attempt, (use_tools, timeout) in enumerate([
        (True, ATTEMPT_1_TIMEOUT),
        (False, ATTEMPT_2_TIMEOUT),
    ], 1):
        attempt_cmd = list(cmd)
        if not use_tools:
            # Replace the tools arg with empty
            try:
                idx = attempt_cmd.index("WebSearch,WebFetch")
                attempt_cmd[idx] = ""
            except ValueError:
                pass
            log(f"  attempt {attempt}: retrying WITHOUT WebSearch ({timeout}s timeout)")
        else:
            log(f"  attempt {attempt}: with WebSearch ({timeout}s timeout)")

        attempt_prompt = prompt_with_tools if use_tools else prompt_without_tools
        try:
            result = _run_claude_with_watchdog(attempt_cmd, attempt_prompt, timeout)
        except RuntimeError as e:
            if attempt == 1:
                log(f"  attempt 1 failed: {e}")
                notify_macos(
                    title="Brief degraded",
                    subtitle="WebSearch timed out — retrying without",
                    message="Next attempt uses pre-fetched context only",
                )
                continue  # try attempt 2
            else:
                raise  # both attempts failed

        duration = (datetime.now() - start).total_seconds()

        if result.returncode != 0:
            err = (result.stderr or "").strip()[:800]
            if attempt == 1:
                log(f"  attempt 1 exited {result.returncode}: {err[:200]}")
                continue  # try attempt 2
            raise RuntimeError(
                f"claude -p exited {result.returncode}: {err or '<no stderr>'}"
            )

        output = _strip_preamble(result.stdout.strip())
        if not output or len(output) < 100:
            if attempt == 1:
                log(f"  attempt 1 produced empty/tiny output ({len(output)} chars)")
                continue
            # Even attempt 2 is empty — still return what we have

        if attempt == 2:
            log("  completed on attempt 2 (no WebSearch — degraded quality)")

        stats = {
            "duration_seconds": round(duration, 1),
            "output_chars": len(output),
            "stderr_chars": len(result.stderr or ""),
            "attempt": attempt,
            "used_web_search": use_tools,
        }
        return output, stats

    # Should never reach here, but just in case
    raise RuntimeError("both attempts failed")


# --- HTML brief rendering ---------------------------------------------------

def _failed_feeds_html() -> str:
    """Build a small HTML block listing any feeds that failed this run.
    Returns empty string if nothing failed — section is only shown when
    there's something to warn about."""
    if not FAILED_FEEDS:
        return ""
    items_html = "".join(
        f'<li style="color:#9a3412;margin:4px 0;"><strong style="color:#9a3412;">{f["name"]}</strong> <span style="color:#a16207;">[{f["category"]}]</span> — {f["reason"]}</li>'
        for f in FAILED_FEEDS
    )
    return (
        '<div style="margin:20px 0;padding:12px 16px;background:#fef3c7;border:1px solid #fcd34d;border-radius:6px;">'
        '<div style="font-family:-apple-system,\'Segoe UI\',Roboto,sans-serif;font-size:12px;font-weight:700;color:#92400e;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px;">'
        f'⚠ {len(FAILED_FEEDS)} feed(s) failed this run'
        '</div>'
        f'<ul style="margin:4px 0 0 0;padding-left:18px;font-family:-apple-system,\'Segoe UI\',Roboto,sans-serif;font-size:13px;line-height:1.5;">{items_html}</ul>'
        '</div>'
    )


def render_brief_html(
    claude_markdown: str,
    items: list[dict],
    stats: dict,
    defillama_snapshot: dict | None = None,
    mev_snapshot: dict | None = None,
) -> str:
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

{_failed_feeds_html()}

{render_defillama_html(defillama_snapshot or {})}

{render_mev_html(mev_snapshot or {})}

<div>
{body_html}
</div>

<div style="margin-top:48px;padding-top:20px;border-top:1px solid #e2e8f0;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;font-size:12px;color:#94a3b8;line-height:1.5;">
Generated by digest.py &middot; model: {MODEL} via claude&nbsp;-p &middot; {stats.get('duration_seconds', 0)}s{f" &middot; {len(FAILED_FEEDS)} feeds failed" if FAILED_FEEDS else ""}
</div>

</div>
</body>
</html>
"""


# --- MEV-Boost snapshot from relayscan.io ----------------------------------

RELAYSCAN_URL = "https://relayscan.io/overview?t=24h"
MEV_TIMEOUT = 15


def _parse_relayscan_table(table_html: str, expected_cols: int) -> list[list[str]]:
    """Parse a relayscan HTML5 table (no closing </td> or </th>) into rows."""
    rows_raw = re.findall(
        r"<tr[^>]*>(.*?)(?=<tr|</tbody>|</table>|$)", table_html, re.DOTALL
    )
    rows: list[list[str]] = []
    for r in rows_raw:
        cells = re.findall(
            r"<td[^>]*>(.*?)(?=<t[dh]|</tr>|</table>|$)", r, re.DOTALL
        )
        if not cells:
            continue
        texts = []
        for c in cells[:expected_cols]:
            t = re.sub(r"<[^>]+>", "", c)
            t = unescape(t)
            t = re.sub(r"\s+", " ", t).strip()
            texts.append(t)
        if any(texts):
            rows.append(texts)
    return rows


def fetch_mev_snapshot() -> dict:
    """Fetch MEV-Boost stats from relayscan.io/overview (24h window) and
    parse the three HTML tables into structured data:
      - relays: market share (relay, payloads, percent)
      - builders: block share (builder, blocks, percent)
      - profits: builder profitability (builder, blocks, profit_eth, subsidy_eth)
    Returns empty dict on any failure — the brief still renders without it."""
    snapshot: dict = {}
    try:
        r = requests.get(
            RELAYSCAN_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=MEV_TIMEOUT,
        )
        if r.status_code != 200:
            log(f"  relayscan status {r.status_code}")
            return {}
        html = r.text
    except Exception as e:
        log(f"  relayscan fetch failed: {e}")
        return {}

    tables = list(re.finditer(r"<table[^>]*>(.*?)</table>", html, re.DOTALL))
    if len(tables) < 3:
        log(f"  relayscan: expected 3 tables, got {len(tables)}")
        return {}

    # Table 1: Relay market share
    try:
        rows = _parse_relayscan_table(tables[0].group(1), 3)
        snapshot["relays"] = [
            {
                "relay": row[0],
                "payloads": row[1],
                "percent": row[2],
            }
            for row in rows[:10]
            if len(row) >= 3
        ]
    except Exception as e:
        log(f"  relayscan relays parse failed: {e}")

    # Table 2: Builder block share
    try:
        rows = _parse_relayscan_table(tables[1].group(1), 3)
        snapshot["builders"] = [
            {
                "builder": row[0],
                "blocks": row[1],
                "percent": row[2],
            }
            for row in rows[:10]
            if len(row) >= 3
        ]
    except Exception as e:
        log(f"  relayscan builders parse failed: {e}")

    # Table 3: Builder profit
    try:
        rows = _parse_relayscan_table(tables[2].group(1), 6)
        snapshot["profits"] = [
            {
                "builder": row[0],
                "blocks": row[1],
                "blocks_profit": row[2],
                "blocks_subsidy": row[3],
                "profit_eth": row[4],
                "subsidy_eth": row[5],
            }
            for row in rows[:8]
            if len(row) >= 6
        ]
    except Exception as e:
        log(f"  relayscan profit parse failed: {e}")

    return snapshot


def render_mev_html(snapshot: dict) -> str:
    """Render the MEV snapshot as clean HTML tables, matching the
    DefiLlama section's typography."""
    if not snapshot:
        return ""

    H2 = (
        "font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"
        "font-size:20px;font-weight:700;color:#0f172a;"
        "margin:40px 0 14px 0;padding:0 0 8px 0;"
        "border-bottom:2px solid #e2e8f0;letter-spacing:-0.01em;"
    )
    CAPTION = (
        "font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"
        "font-size:13px;color:#475569;margin:18px 0 6px 0;font-weight:600;"
    )
    TABLE = (
        "width:100%;border-collapse:collapse;"
        "font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"
        "font-size:13px;margin:4px 0 14px 0;"
    )
    TH = (
        "text-align:left;padding:8px 12px 6px 12px;"
        "font-weight:700;color:#64748b;font-size:11px;"
        "text-transform:uppercase;letter-spacing:0.06em;"
        "border-bottom:2px solid #e2e8f0;"
    )
    TH_R = TH + "text-align:right;"
    TD = "padding:7px 12px;border-bottom:1px solid #f1f5f9;color:#1e293b;"
    TD_R = TD + "text-align:right;font-variant-numeric:tabular-nums;"
    TD_NAME = TD + "font-weight:600;color:#0f172a;"

    parts: list[str] = []
    parts.append(
        f'<h2 style="{H2}">MEV-Boost Activity '
        '<span style="font-size:12px;font-weight:500;color:#64748b;">'
        '(<a style="color:#2563eb;text-decoration:underline;" '
        'href="https://relayscan.io/overview?t=24h">relayscan.io</a> · '
        '<a style="color:#2563eb;text-decoration:underline;" '
        'href="https://mevboost.pics">mevboost.pics</a> · 24h window)'
        '</span>'
        '</h2>'
    )

    # Relay market share
    if snapshot.get("relays"):
        parts.append(f'<div style="{CAPTION}">Relay Market Share (last 24h)</div>')
        rows = "".join(
            f'<tr><td style="{TD_NAME}">{r["relay"]}</td>'
            f'<td style="{TD_R}">{r["payloads"]}</td>'
            f'<td style="{TD_R}">{r["percent"]}</td></tr>'
            for r in snapshot["relays"]
        )
        parts.append(
            f'<table style="{TABLE}">'
            f'<thead><tr><th style="{TH}">Relay</th>'
            f'<th style="{TH_R}">Payloads</th>'
            f'<th style="{TH_R}">Share</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    # Builder block share
    if snapshot.get("builders"):
        parts.append(f'<div style="{CAPTION}">Top Block Builders (last 24h)</div>')
        rows = "".join(
            f'<tr><td style="{TD_NAME}">{b["builder"]}</td>'
            f'<td style="{TD_R}">{b["blocks"]}</td>'
            f'<td style="{TD_R}">{b["percent"]}</td></tr>'
            for b in snapshot["builders"]
        )
        parts.append(
            f'<table style="{TABLE}">'
            f'<thead><tr><th style="{TH}">Builder</th>'
            f'<th style="{TH_R}">Blocks</th>'
            f'<th style="{TH_R}">Share</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    # Builder profit
    if snapshot.get("profits"):
        parts.append(f'<div style="{CAPTION}">Builder Profitability (24h, ETH)</div>')
        rows = "".join(
            f'<tr><td style="{TD_NAME}">{p["builder"][:40]}</td>'
            f'<td style="{TD_R}">{p["blocks"]}</td>'
            f'<td style="{TD_R}">{p["profit_eth"]}</td>'
            f'<td style="{TD_R}">{p["subsidy_eth"]}</td></tr>'
            for p in snapshot["profits"]
        )
        parts.append(
            f'<table style="{TABLE}">'
            f'<thead><tr><th style="{TH}">Builder</th>'
            f'<th style="{TH_R}">Blocks</th>'
            f'<th style="{TH_R}">Profit</th>'
            f'<th style="{TH_R}">Subsidy</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    return "".join(parts)


def render_mev_for_prompt(snapshot: dict) -> str:
    """Compact text version of the MEV snapshot for the Claude prompt."""
    if not snapshot:
        return ""
    lines = ["## MEV-Boost Activity (from relayscan.io, 24h window)\n"]
    def _short_name(s: str) -> str:
        """Pick a readable short label — use the first segment of the
        domain name, or the first whitespace-delimited word for builder
        names. Examples:
          'relay.ultrasound.money'         -> 'ultrasound.money'
          'bloxroute.max-profit.blxrbdn.com' -> 'bloxroute'
          'Titan (titanbuilder.xyz)'       -> 'Titan'
          'BuilderNet (Flashbots)'         -> 'BuilderNet (Flashbots)'
        """
        s = s.strip()
        # Parenthetical form: "Name (domain)"
        if " (" in s:
            return s.split(" (", 1)[0] + (
                f" ({s.split(' (', 1)[1].rstrip(')').split('.')[0]})"
                if "(" in s else ""
            )
        # Domain form: strip everything after the second dot for readability
        if s.count(".") >= 2 and " " not in s:
            parts = s.split(".")
            return ".".join(parts[1:]) if len(parts[0]) <= 6 else parts[0]
        return s[:28]

    if snapshot.get("relays"):
        top = snapshot["relays"][:5]
        lines.append("**Top relays**: " + " · ".join(
            f"{_short_name(r['relay'])} {r['percent']}" for r in top
        ))
    if snapshot.get("builders"):
        top = snapshot["builders"][:5]
        lines.append("**Top builders**: " + " · ".join(
            f"{_short_name(b['builder'])} {b['percent']}" for b in top
        ))
    if snapshot.get("profits"):
        top = sorted(
            snapshot["profits"],
            key=lambda p: float(p.get("profit_eth", "0").replace(",", "") or "0"),
            reverse=True,
        )[:5]
        lines.append(
            "**Most profitable builders (24h)**: "
            + " · ".join(
                f"{_short_name(p['builder'])} {p['profit_eth']}Ξ" for p in top
            )
        )
    lines.append(
        "\n_These numbers are current as of the run time. "
        "Reference them when relevant to MEV, relay censorship, "
        "builder centralization, or block auction topics._"
    )
    return "\n".join(lines)


# --- recent-brief history (topic-level dedup) ------------------------------

def load_recent_brief_context() -> str:
    """Load the most recent brief markdowns for topic-level dedup.

    Returns a formatted block suitable for injecting into the Claude prompt
    so the model can actively avoid repeating stories it already covered
    in the last 2-3 briefs. Returns empty string on first run (no history)
    or if the briefs directory is empty of .md files.
    """
    if not BRIEFS_DIR.exists():
        return ""
    cutoff = datetime.now().timestamp() - RECENT_BRIEFS_HOURS * 3600
    # Match only timestamped brief files: YYYY-MM-DD_HH-MM.md
    pat = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}\.md$")
    candidates = []
    for p in BRIEFS_DIR.glob("*.md"):
        if not pat.match(p.name):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        candidates.append((mtime, p))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    picks = candidates[:RECENT_BRIEFS_COUNT]

    blocks = []
    for mtime, p in picks:
        label = datetime.fromtimestamp(mtime).strftime("%A %b %-d, %-I:%M %p")
        try:
            content = p.read_text()
        except OSError:
            continue
        # Trim to save tokens — we care about what topics were covered,
        # not every link
        if len(content) > 6000:
            content = content[:6000] + "\n\n[...truncated]"
        blocks.append(f"### Brief from {label}\n\n{content}")
    if not blocks:
        return ""
    return "\n\n---\n\n".join(blocks)


# --- post-synthesis topic filter -------------------------------------------

_BULLET_HEADLINE_RE = re.compile(r"^\s*-\s+\*\*(.+?)\*\*")
_TOPICS_SECTION_RE = re.compile(
    r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) — (.+)$", re.MULTILINE
)
_TOPICS_HEADER = (
    "# Recent topics covered in briefs\n\n"
    "_Auto-managed by digest.py. Entries older than the cooldown "
    "are pruned on the next run. Hand-edit to force a topic to age "
    "out early or to block it from recurring._\n"
)


def _normalize_headline(s: str) -> str:
    return " ".join(s.strip().lower().split())


def extract_bullet_blocks(markdown: str) -> list[dict]:
    """Parse a brief-style markdown into bullet blocks.

    Returns a list of dicts, one per `- **Headline**: ...` bullet:
        {
          "headline": str,           # text between the first ** .. **
          "start": int,              # line index where the bullet begins
          "end": int,                # exclusive line index where it ends
          "lines": list[str],        # the raw lines (incl. continuations)
        }

    Non-bullet lines (section headers, paragraphs, blank lines) are not
    returned — callers rebuild the file by splicing bullets back into the
    original line list by index.
    """
    lines = markdown.split("\n")
    blocks: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _BULLET_HEADLINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        headline = m.group(1).strip()
        start = i
        j = i + 1
        # Continuation: indented lines, blank lines, or non-top-level content
        # that belongs to this bullet. Stop at next `- ` at column 0 or any
        # `## ` header at column 0.
        while j < n:
            nxt = lines[j]
            if nxt.startswith("## ") or nxt.startswith("# "):
                break
            if nxt.startswith("- "):
                break
            j += 1
        # Trim trailing blank lines off the block (keep one for spacing when
        # rebuilding)
        end = j
        while end > start + 1 and lines[end - 1].strip() == "":
            end -= 1
        blocks.append(
            {
                "headline": headline,
                "start": start,
                "end": end,
                "lines": lines[start:end],
            }
        )
        i = j
    return blocks


def load_recent_topics_for_filter() -> tuple[list[dict], str]:
    """Read recent-topics.md, prune expired sections, return (topics, rewritten).

    `topics` is a list of {"headline": str, "timestamp": datetime,
    "brief": str} with entries inside the cooldown window.
    `rewritten` is the pruned file contents (caller writes it back).
    Returns ([], header) if the file doesn't exist or is empty.
    """
    if not TOPICS_FILE.exists():
        return [], _TOPICS_HEADER
    try:
        raw = TOPICS_FILE.read_text()
    except OSError:
        return [], _TOPICS_HEADER

    cutoff = datetime.now() - timedelta(hours=TOPIC_COOLDOWN_HOURS)

    # Split into sections by `## <ts> — <brief>` headers. Preserve the file's
    # header preamble (everything before the first `## ` with a date).
    section_starts: list[tuple[int, str, str]] = []
    for m in _TOPICS_SECTION_RE.finditer(raw):
        section_starts.append((m.start(), m.group(1), m.group(2)))

    if not section_starts:
        # No dated sections yet — just ensure the canonical header exists
        return [], _TOPICS_HEADER

    preamble = raw[: section_starts[0][0]]
    if not preamble.strip():
        preamble = _TOPICS_HEADER

    kept_topics: list[dict] = []
    kept_sections: list[str] = []
    for idx, (offset, ts_str, brief_label) in enumerate(section_starts):
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if ts < cutoff:
            continue
        end = (
            section_starts[idx + 1][0]
            if idx + 1 < len(section_starts)
            else len(raw)
        )
        section_text = raw[offset:end].rstrip() + "\n"
        kept_sections.append(section_text)
        for line in section_text.split("\n"):
            stripped = line.lstrip("-* ").strip()
            if not stripped or line.startswith("#"):
                continue
            if line.lstrip().startswith(("-", "*")):
                kept_topics.append(
                    {
                        "headline": stripped,
                        "timestamp": ts,
                        "brief": brief_label,
                    }
                )

    rewritten = preamble.rstrip() + "\n\n" + "\n".join(kept_sections)
    return kept_topics, rewritten


def save_recent_topics(
    final_markdown: str, brief_label: str, run_ts: datetime
) -> None:
    """Append this run's headlines to recent-topics.md, pruning expired.

    Writes atomically via a .tmp rename. Never raises — failure is logged
    and the brief is already on disk, so topic tracking is best-effort.
    """
    try:
        topics, rewritten = load_recent_topics_for_filter()
        blocks = extract_bullet_blocks(final_markdown)
        headlines = [b["headline"] for b in blocks]
        if not headlines:
            log("  topics: no bullet headlines extracted — not appending")
            # Still write the prune-rewritten file so old entries age out
            _atomic_write_text(TOPICS_FILE, rewritten)
            return
        new_section = (
            f"## {run_ts.strftime('%Y-%m-%d %H:%M')} — {brief_label}\n"
            + "\n".join(f"- {h}" for h in headlines)
            + "\n"
        )
        body = rewritten.rstrip() + "\n\n" + new_section
        _atomic_write_text(TOPICS_FILE, body)
        log(
            f"  topics: appended {len(headlines)} headlines "
            f"({len(topics)} prior still within cooldown)"
        )
    except Exception as e:
        log(f"  topics: save failed ({type(e).__name__}: {e}) — non-fatal")


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _build_filter_prompt(draft: str, recent_topics: list[dict]) -> str:
    if recent_topics:
        lines = []
        for t in sorted(recent_topics, key=lambda x: x["timestamp"], reverse=True):
            lines.append(
                f"[{t['timestamp'].strftime('%Y-%m-%d %H:%M')}] {t['headline']}"
            )
        topic_block = "\n".join(lines)
    else:
        topic_block = "(no prior topics within cooldown window)"

    return f"""You are filtering a research brief draft for staleness.

The reader has ALREADY read every item in "Recently covered" below within the last {TOPIC_COOLDOWN_HOURS} hours. Your job: for each bullet in the draft with a **bold headline**, decide whether to keep it, collapse it to one line, or drop it.

Decision rules:
- **KEEP**: genuinely new story, OR a covered story with materially new facts (new data, concrete next step, resolution, contradiction, named participant change).
- **COLLAPSE**: incremental update on a covered story — rewrite as ONE short sentence starting with "Still developing:". Example: "Still developing: Arbitrum Security Council final voting round is now live; no result yet."
- **DROP**: pure rehash, no new information beyond what was already covered.

When comparing, match on the underlying STORY, not on the exact words — fresh podcasts/tweets covering an already-covered story are rehash, not new signal. Be willing to drop bullets the draft spent effort on; the reader wants signal, not padding.

<recently_covered>
{topic_block}
</recently_covered>

<draft>
{draft}
</draft>

Output VALID JSON ONLY — no preamble, no code fences, no commentary. Copy each headline EXACTLY as it appears between ** ** in the draft.

{{"decisions":[
  {{"headline":"<exact bold headline text>","action":"KEEP","collapsed":""}},
  {{"headline":"<...>","action":"COLLAPSE","collapsed":"Still developing: <one short sentence>"}},
  {{"headline":"<...>","action":"DROP","collapsed":""}}
]}}
"""


def _parse_filter_json(raw: str) -> dict | None:
    """Parse the filter pass output. Tolerates a stray code fence or prose
    leading/trailing the JSON; returns None if no valid JSON found."""
    s = raw.strip()
    # Strip common code-fence wrappers
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    # Find the outermost {...}
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None


def filter_stale_bullets(
    draft: str, recent_topics: list[dict]
) -> tuple[str, dict]:
    """Second-pass `claude -p` filter.

    Returns (filtered_markdown, stats). On any failure (timeout, bad JSON,
    empty response, all-drop result), returns the input draft unchanged
    with stats["ok"]=False and a reason.
    """
    stats = {"ok": False, "reason": "not-run", "kept": 0, "collapsed": 0, "dropped": 0}
    blocks = extract_bullet_blocks(draft)
    if not blocks:
        stats["reason"] = "no-bullets"
        return draft, stats

    prompt = _build_filter_prompt(draft, recent_topics)
    cmd = ["claude", "-p", "--model", TOPIC_FILTER_MODEL]
    log(
        f"  filter: {len(blocks)} bullets, {len(recent_topics)} prior topics, "
        f"model={TOPIC_FILTER_MODEL}"
    )
    try:
        result = _run_claude_with_watchdog(
            cmd, prompt, TOPIC_FILTER_TIMEOUT_SECONDS
        )
    except RuntimeError as e:
        stats["reason"] = f"watchdog: {e}"
        log(f"  filter: {stats['reason']} — shipping unfiltered")
        return draft, stats

    if result.returncode != 0:
        stats["reason"] = f"exit-{result.returncode}"
        log(f"  filter: claude exited {result.returncode} — shipping unfiltered")
        return draft, stats

    parsed = _parse_filter_json(result.stdout)
    if not parsed or "decisions" not in parsed:
        stats["reason"] = "bad-json"
        log("  filter: unparseable JSON — shipping unfiltered")
        return draft, stats

    # Build a decision map keyed by normalized headline
    decisions: dict[str, dict] = {}
    for d in parsed.get("decisions", []):
        h = d.get("headline", "")
        if not h:
            continue
        decisions[_normalize_headline(h)] = d

    # Apply decisions. Drop entries that would leave ALL bullets dropped
    # (safety check — something's wrong with the filter).
    lines = draft.split("\n")
    drop_spans: list[tuple[int, int]] = []
    replace_spans: list[tuple[int, int, str]] = []
    kept = collapsed_n = dropped = 0
    for b in blocks:
        key = _normalize_headline(b["headline"])
        d = decisions.get(key)
        if not d:
            kept += 1
            continue
        action = (d.get("action") or "KEEP").upper()
        if action == "DROP":
            drop_spans.append((b["start"], b["end"]))
            dropped += 1
        elif action == "COLLAPSE":
            collapsed_line = (d.get("collapsed") or "").strip()
            if not collapsed_line:
                kept += 1
                continue
            if not collapsed_line.startswith("- "):
                collapsed_line = f"- {collapsed_line}"
            replace_spans.append((b["start"], b["end"], collapsed_line))
            collapsed_n += 1
        else:
            kept += 1

    if kept == 0 and (dropped + collapsed_n) > 0:
        stats["reason"] = "all-dropped-safety-skip"
        log("  filter: would drop ALL bullets — skipping filter, shipping unfiltered")
        return draft, stats

    # Apply mutations from bottom up so indices stay valid
    mutations = (
        [("drop", s, e, None) for s, e in drop_spans]
        + [("replace", s, e, text) for s, e, text in replace_spans]
    )
    mutations.sort(key=lambda m: m[1], reverse=True)
    for kind, s, e, text in mutations:
        if kind == "drop":
            del lines[s:e]
        else:
            lines[s:e] = [text]

    filtered = "\n".join(lines)
    # Collapse 3+ consecutive blank lines left behind by drops
    filtered = re.sub(r"\n{3,}", "\n\n", filtered)
    # Drop sections whose only remaining content is the header + blank lines
    filtered = _drop_empty_sections(filtered)

    stats.update(
        {"ok": True, "reason": "ok", "kept": kept,
         "collapsed": collapsed_n, "dropped": dropped}
    )
    log(
        f"  filter: kept={kept} collapsed={collapsed_n} dropped={dropped}"
    )
    return filtered, stats


def _drop_empty_sections(markdown: str) -> str:
    """Remove `## Heading` blocks that contain no non-blank content before
    the next header or EOF. Keeps the very first line if it's not a header."""
    lines = markdown.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("## "):
            # Look ahead until next header or EOF
            j = i + 1
            has_content = False
            while j < n and not lines[j].startswith("## "):
                if lines[j].strip():
                    has_content = True
                    break
                j += 1
            if has_content:
                out.append(line)
                i += 1
            else:
                # Skip this header entirely; jump to next header position
                while j < n and not lines[j].startswith("## "):
                    j += 1
                i = j
        else:
            out.append(line)
            i += 1
    return "\n".join(out)


# --- DefiLlama snapshot -----------------------------------------------------

DEFILLAMA_TIMEOUT = 15  # seconds per endpoint
DEFILLAMA_TOP_N = 8     # rows per table


def fetch_defillama_snapshot() -> dict:
    """Fetch a compact DeFi analytics snapshot from DefiLlama's free public
    API. Covers DEX volumes, lending TVL, stablecoin supply, chain TVL.

    Returns a dict with up to four keys: dexs, lending, stablecoins, chains.
    Per-endpoint failures log and continue — one broken endpoint doesn't
    block the others. Returns an empty dict on total failure."""
    snapshot: dict = {}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    # 1. DEX volumes
    try:
        r = requests.get(
            "https://api.llama.fi/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true",
            timeout=DEFILLAMA_TIMEOUT,
            headers=headers,
        )
        if r.status_code == 200:
            d = r.json()
            protos = [p for p in (d.get("protocols") or []) if (p.get("total24h") or 0) > 0]
            protos.sort(key=lambda p: p.get("total24h", 0) or 0, reverse=True)
            snapshot["dexs"] = {
                "total_24h": d.get("total24h"),
                "total_7d": d.get("total7d"),
                "change_1d": d.get("change_1d"),
                "change_7d": d.get("change_7d"),
                "top": [
                    {
                        "name": p.get("name"),
                        "volume_24h": p.get("total24h"),
                        "change_1d": p.get("change_1d"),
                    }
                    for p in protos[:DEFILLAMA_TOP_N]
                ],
            }
    except Exception as e:
        log(f"  defillama dexs failed: {e}")

    # 2. Lending TVL (via /protocols filtered by Lending category)
    try:
        r = requests.get(
            "https://api.llama.fi/protocols",
            timeout=DEFILLAMA_TIMEOUT,
            headers=headers,
        )
        if r.status_code == 200:
            all_protos = r.json()
            lending = [p for p in all_protos if p.get("category") == "Lending"]
            lending.sort(key=lambda p: p.get("tvl", 0) or 0, reverse=True)
            snapshot["lending"] = {
                "total_tvl": sum((p.get("tvl", 0) or 0) for p in lending),
                "top": [
                    {
                        "name": p.get("name"),
                        "tvl": p.get("tvl"),
                        "change_1d": p.get("change_1d"),
                        "change_7d": p.get("change_7d"),
                    }
                    for p in lending[:DEFILLAMA_TOP_N]
                ],
            }
    except Exception as e:
        log(f"  defillama lending failed: {e}")

    # 3. Stablecoins (proxy for "payments")
    try:
        r = requests.get(
            "https://stablecoins.llama.fi/stablecoins?includePrices=false",
            timeout=DEFILLAMA_TIMEOUT,
            headers=headers,
        )
        if r.status_code == 200:
            data = r.json()
            coins = data.get("peggedAssets", [])

            def _circ(c):
                cm = c.get("circulating") or {}
                return cm.get("peggedUSD", 0) or 0

            def _prev(c):
                cm = c.get("circulatingPrevDay") or {}
                return cm.get("peggedUSD", 0) or 0

            coins.sort(key=_circ, reverse=True)
            snapshot["stablecoins"] = {
                "total_supply": sum(_circ(c) for c in coins),
                "top": [
                    {
                        "symbol": c.get("symbol"),
                        "name": c.get("name"),
                        "supply": _circ(c),
                        "delta_1d_usd": _circ(c) - _prev(c),
                    }
                    for c in coins[:DEFILLAMA_TOP_N]
                ],
            }
    except Exception as e:
        log(f"  defillama stablecoins failed: {e}")

    # 4. Chains (cross-chain TVL distribution)
    try:
        r = requests.get(
            "https://api.llama.fi/chains",
            timeout=DEFILLAMA_TIMEOUT,
            headers=headers,
        )
        if r.status_code == 200:
            chains = r.json()
            chains.sort(key=lambda c: c.get("tvl", 0) or 0, reverse=True)
            snapshot["chains"] = {
                "total_tvl": sum((c.get("tvl", 0) or 0) for c in chains),
                "top": [
                    {
                        "name": c.get("name"),
                        "tvl": c.get("tvl"),
                        "token": c.get("tokenSymbol", ""),
                    }
                    for c in chains[:10]
                ],
            }
    except Exception as e:
        log(f"  defillama chains failed: {e}")

    return snapshot


def _fmt_usd(n) -> str:
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if abs(n) >= 1e9:
        return f"${n/1e9:.2f}B"
    if abs(n) >= 1e6:
        return f"${n/1e6:.1f}M"
    if abs(n) >= 1e3:
        return f"${n/1e3:.0f}K"
    return f"${n:,.0f}"


def _fmt_pct(p) -> str:
    if p is None:
        return "—"
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "—"
    color = "#15803d" if p >= 0 else "#b91c1c"
    sign = "+" if p >= 0 else ""
    return f'<span style="color:{color};font-weight:600;">{sign}{p:.1f}%</span>'


def render_defillama_html(snapshot: dict) -> str:
    """Render the DefiLlama snapshot as four clean HTML tables."""
    if not snapshot:
        return ""

    H2 = (
        "font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"
        "font-size:20px;font-weight:700;color:#0f172a;"
        "margin:40px 0 14px 0;padding:0 0 8px 0;"
        "border-bottom:2px solid #e2e8f0;letter-spacing:-0.01em;"
    )
    CAPTION = (
        "font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"
        "font-size:13px;color:#475569;margin:18px 0 6px 0;font-weight:600;"
    )
    TABLE = (
        "width:100%;border-collapse:collapse;"
        "font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"
        "font-size:13px;margin:4px 0 14px 0;"
    )
    TH = (
        "text-align:left;padding:8px 12px 6px 12px;"
        "font-weight:700;color:#64748b;font-size:11px;"
        "text-transform:uppercase;letter-spacing:0.06em;"
        "border-bottom:2px solid #e2e8f0;"
    )
    TH_R = TH + "text-align:right;"
    TD = "padding:7px 12px;border-bottom:1px solid #f1f5f9;color:#1e293b;"
    TD_R = TD + "text-align:right;font-variant-numeric:tabular-nums;"
    TD_NAME = TD + "font-weight:600;color:#0f172a;"

    parts: list[str] = []
    parts.append(
        f'<h2 style="{H2}">DeFi Snapshot '
        '<span style="font-size:12px;font-weight:500;color:#64748b;">(DefiLlama · live)</span>'
        '</h2>'
    )

    # --- DEX volumes ---
    if "dexs" in snapshot:
        d = snapshot["dexs"]
        caption = (
            f'Top DEXs by 24h Volume &middot; Total <strong>{_fmt_usd(d["total_24h"])}</strong> '
            f'&middot; 1d {_fmt_pct(d["change_1d"])} &middot; 7d {_fmt_pct(d["change_7d"])}'
        )
        parts.append(f'<div style="{CAPTION}">{caption}</div>')
        rows = "".join(
            f'<tr><td style="{TD_NAME}">{p["name"]}</td>'
            f'<td style="{TD_R}">{_fmt_usd(p["volume_24h"])}</td>'
            f'<td style="{TD_R}">{_fmt_pct(p["change_1d"])}</td></tr>'
            for p in d["top"]
        )
        parts.append(
            f'<table style="{TABLE}">'
            f'<thead><tr><th style="{TH}">DEX</th>'
            f'<th style="{TH_R}">24h Volume</th>'
            f'<th style="{TH_R}">1d Δ</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    # --- Lending TVL ---
    if "lending" in snapshot:
        l = snapshot["lending"]
        caption = (
            f'Top Lending Protocols by TVL &middot; '
            f'Category total <strong>{_fmt_usd(l["total_tvl"])}</strong>'
        )
        parts.append(f'<div style="{CAPTION}">{caption}</div>')
        rows = "".join(
            f'<tr><td style="{TD_NAME}">{p["name"]}</td>'
            f'<td style="{TD_R}">{_fmt_usd(p["tvl"])}</td>'
            f'<td style="{TD_R}">{_fmt_pct(p["change_1d"])}</td>'
            f'<td style="{TD_R}">{_fmt_pct(p["change_7d"])}</td></tr>'
            for p in l["top"]
        )
        parts.append(
            f'<table style="{TABLE}">'
            f'<thead><tr><th style="{TH}">Protocol</th>'
            f'<th style="{TH_R}">TVL</th>'
            f'<th style="{TH_R}">1d Δ</th>'
            f'<th style="{TH_R}">7d Δ</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    # --- Stablecoins ---
    if "stablecoins" in snapshot:
        s = snapshot["stablecoins"]
        caption = (
            f'Top Stablecoins by Circulating Supply &middot; '
            f'Total market <strong>{_fmt_usd(s["total_supply"])}</strong>'
        )
        parts.append(f'<div style="{CAPTION}">{caption}</div>')

        def _fmt_signed_usd(n):
            if n is None:
                return "—"
            color = "#15803d" if n >= 0 else "#b91c1c"
            sign = "+" if n >= 0 else "−"
            return f'<span style="color:{color};font-weight:600;">{sign}{_fmt_usd(abs(n))}</span>'

        rows = "".join(
            f'<tr><td style="{TD_NAME}">{c["symbol"]}</td>'
            f'<td style="{TD}">{c["name"]}</td>'
            f'<td style="{TD_R}">{_fmt_usd(c["supply"])}</td>'
            f'<td style="{TD_R}">{_fmt_signed_usd(c["delta_1d_usd"])}</td></tr>'
            for c in s["top"]
        )
        parts.append(
            f'<table style="{TABLE}">'
            f'<thead><tr><th style="{TH}">Symbol</th>'
            f'<th style="{TH}">Name</th>'
            f'<th style="{TH_R}">Supply</th>'
            f'<th style="{TH_R}">1d Δ (USD)</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    # --- Cross-chain TVL ---
    if "chains" in snapshot:
        c = snapshot["chains"]
        caption = (
            f'Cross-chain TVL &middot; '
            f'Aggregate <strong>{_fmt_usd(c["total_tvl"])}</strong>'
        )
        parts.append(f'<div style="{CAPTION}">{caption}</div>')
        total = c["total_tvl"] or 1
        rows = "".join(
            f'<tr><td style="{TD_NAME}">{p["name"]}</td>'
            f'<td style="{TD}">{p["token"] or "—"}</td>'
            f'<td style="{TD_R}">{_fmt_usd(p["tvl"])}</td>'
            f'<td style="{TD_R}">{(p["tvl"] or 0) / total * 100:.1f}%</td></tr>'
            for p in c["top"]
        )
        parts.append(
            f'<table style="{TABLE}">'
            f'<thead><tr><th style="{TH}">Chain</th>'
            f'<th style="{TH}">Native</th>'
            f'<th style="{TH_R}">TVL</th>'
            f'<th style="{TH_R}">Share</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    return "".join(parts)


def render_defillama_for_prompt(snapshot: dict) -> str:
    """Compact text version of the DefiLlama snapshot, suitable for injecting
    into the Claude prompt so the narrative can reference the numbers."""
    if not snapshot:
        return ""
    lines = ["## DeFi Metrics (from DefiLlama — fresh at run time)\n"]

    if "dexs" in snapshot:
        d = snapshot["dexs"]
        lines.append(
            f"**DEX 24h volume**: {_fmt_usd(d['total_24h'])} "
            f"(1d {d['change_1d']:+.1f}% · 7d {d['change_7d']:+.1f}%)"
            if d.get("change_1d") is not None and d.get("change_7d") is not None
            else f"**DEX 24h volume**: {_fmt_usd(d['total_24h'])}"
        )
        tops = [f"{p['name']} {_fmt_usd(p['volume_24h'])}" for p in d["top"][:5]]
        lines.append(f"  Top: {', '.join(tops)}")

    if "lending" in snapshot:
        l = snapshot["lending"]
        lines.append(f"**Lending TVL (top lending protocols)**: {_fmt_usd(l['total_tvl'])}")
        tops = [f"{p['name']} {_fmt_usd(p['tvl'])}" for p in l["top"][:5]]
        lines.append(f"  Top: {', '.join(tops)}")

    if "stablecoins" in snapshot:
        s = snapshot["stablecoins"]
        lines.append(f"**Stablecoin total market**: {_fmt_usd(s['total_supply'])}")
        tops = [f"{c['symbol']} {_fmt_usd(c['supply'])}" for c in s["top"][:5]]
        lines.append(f"  Top: {', '.join(tops)}")

    if "chains" in snapshot:
        c = snapshot["chains"]
        lines.append(f"**Cross-chain TVL (aggregate)**: {_fmt_usd(c['total_tvl'])}")
        tops = [f"{p['name']} {_fmt_usd(p['tvl'])}" for p in c["top"][:6]]
        lines.append(f"  Top: {', '.join(tops)}")

    lines.append(
        "\n_These are authoritative live numbers. Feel free to reference "
        "them in your synthesis where relevant (e.g., citing DEX volume "
        "trends, lending protocol changes, stablecoin flows). Do NOT "
        "duplicate the table itself — the reader sees it separately._"
    )
    return "\n".join(lines)


# --- morning prepend: yesterday's Claude Code journal ----------------------

JOURNAL_DIR = Path.home() / "claude-code-journal"


def _find_recent_journal_file() -> Path | None:
    """Return the path to the most relevant recent journal file.

    Preference order:
      1. Yesterday's dated file (the normal case when the journal ran on
         schedule at 23:59 last night).
      2. Today's dated file (handles the late-fire case where the Mac was
         asleep at 23:59 and launchd didn't run the journal until this
         morning — the file ends up dated for today but it's still the
         coverage the brief wants).
      3. Any journal file modified in the last 48 hours (last-ditch fallback
         if the dated naming drifted for any reason).
    Returns None if nothing fits.
    """
    if not JOURNAL_DIR.exists():
        return None
    now = datetime.now()
    yesterday_path = JOURNAL_DIR / f"{(now - timedelta(days=1)):%Y-%m-%d}.md"
    today_path = JOURNAL_DIR / f"{now:%Y-%m-%d}.md"
    if yesterday_path.exists():
        return yesterday_path
    if today_path.exists():
        return today_path
    # Last-ditch: most recently modified dated journal file in the last 48h.
    # The filename must match YYYY-MM-DD.md exactly — this is how we avoid
    # picking up debug artifacts like last-prompt.md that live in the same dir.
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
    """Read the most relevant recent Claude Code journal entry (if any) and
    return it as a markdown block ready to prepend to today's morning brief.

    The full journal entry has a frontmatter header + body + session metadata
    footer. We keep only the body (Overview, Projects, Loose Ends, Tomorrow's
    Considerations), demote its inner `##` headers to `###` so they nest
    under a new `## Yesterday's Claude Code Work` wrapper heading.
    """
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

    # Split on the first horizontal rule after the frontmatter. The journal
    # format is:
    #   # Claude Code Journal — ...
    #   _Generated: ..._
    #   ---
    #   <body>
    #   ---
    #   ## Session Metadata
    #   ...
    parts = raw.split("\n---\n", 1)
    if len(parts) < 2:
        log("  journal file has unexpected format — skipping")
        return None
    body = parts[1].strip()

    # Strip trailing session metadata block if present
    meta_markers = ("\n---\n\n## Session Metadata", "\n---\n## Session Metadata")
    for marker in meta_markers:
        idx = body.find(marker)
        if idx >= 0:
            body = body[:idx].strip()
            break

    if not body or len(body) < 20:
        return None

    # Demote inner `## ` headers to `### ` so they nest under our wrapper
    body = re.sub(r"^## ", "### ", body, flags=re.MULTILINE)

    # Pretty header that references yesterday's date for context
    yesterday_display = (datetime.now() - timedelta(days=1)).strftime("%A, %B %-d")
    return f"## Yesterday's Claude Code Work — {yesterday_display}\n\n{body}"


# --- Telegram delivery (opt-in) ---------------------------------------------

KEY_TWEETS_HEADER_RE = re.compile(
    r"^##\s*Key\s*Tweets\s*$", re.IGNORECASE | re.MULTILINE
)


def extract_and_strip_key_tweets(claude_markdown: str) -> tuple[str, list[str]]:
    """Find the `## Key Tweets` section in Claude's output, extract the
    tweet URLs, and strip the section from the markdown so it doesn't
    render in the HTML brief.

    Returns (markdown_without_section, [url1, url2, ...]).
    """
    m = KEY_TWEETS_HEADER_RE.search(claude_markdown)
    if not m:
        return claude_markdown, []
    start = m.start()
    # The section runs from the header to the next `## ` header or EOF.
    rest = claude_markdown[m.end():]
    next_section = re.search(r"^##\s+", rest, re.MULTILINE)
    if next_section:
        section_text = rest[:next_section.start()]
        # Keep content after the Key Tweets section
        tail = rest[next_section.start():]
    else:
        section_text = rest
        tail = ""

    # Extract URLs — accept both bare lines and bulleted lines.
    urls: list[str] = []
    for line in section_text.splitlines():
        line = line.strip().lstrip("-*•").strip()
        # Also handle markdown link format: [label](url)
        md_match = re.search(r"\]\((https?://[^)]+)\)", line)
        if md_match:
            urls.append(md_match.group(1))
            continue
        # Plain URL
        plain = re.search(r"https?://\S+", line)
        if plain:
            url = plain.group(0).rstrip(".,);:!?")
            urls.append(url)
    # Dedupe while preserving order
    seen: set[str] = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]

    cleaned_markdown = (claude_markdown[:start].rstrip() + "\n\n" + tail.lstrip()).strip()
    return cleaned_markdown, urls


def build_telegram_summary(
    claude_markdown: str,
    item_count: int,
    feed_count: int,
    defillama_snapshot: dict | None,
) -> str:
    """Short text summary for Telegram — first bullet from each section,
    capped under Telegram's 4096-char message limit."""
    now = datetime.now()
    lines = [
        f"*Morning Brief* · {now:%a %b %-d, %-I:%M %p}",
        f"{item_count} items across {feed_count} sources",
    ]
    if defillama_snapshot and "dexs" in defillama_snapshot:
        d = defillama_snapshot["dexs"]
        t = d.get("total_24h")
        c = d.get("change_1d")
        if t:
            if c is not None:
                lines.append(f"DEX 24h: ${t/1e9:.1f}B ({c:+.1f}%)")
            else:
                lines.append(f"DEX 24h: ${t/1e9:.1f}B")
    lines.append("")

    # Extract first bullet from each `## Section`
    # Prepend \n so the split catches a leading `## Ethereum` at the start
    # of the markdown — without this, the first section lands in the
    # "preamble" slot and gets skipped (the persistent missing-Ethereum bug).
    sections = re.split(r"\n## ", "\n" + claude_markdown)
    for s in sections[1:]:  # skip preamble
        header, _, rest = s.partition("\n")
        header = header.strip()
        if header.lower().startswith("key tweets"):
            continue  # we handle key tweets separately
        # First bullet
        bullets = [ln.strip() for ln in rest.splitlines() if ln.strip().startswith("- ")]
        if not bullets:
            continue
        first = bullets[0][2:]  # strip "- "
        # Trim to keep each section short
        if len(first) > 240:
            first = first[:240].rstrip() + "…"
        lines.append(f"*{header}*")
        lines.append(first)
        lines.append("")

    text = "\n".join(lines).strip()
    # Telegram hard limit 4096 chars per message
    if len(text) > 3800:
        text = text[:3800].rstrip() + "…"
    return text


def _telegram_post(token: str, method: str, data: dict, files: dict | None = None, timeout: int = 30) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    r = requests.post(url, data=data, files=files, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "error_code": r.status_code, "description": r.text[:500]}


def send_telegram_brief(
    html_path: Path,
    claude_markdown: str,
    key_tweet_urls: list[str],
    item_count: int,
    stats: dict,
    defillama_snapshot: dict | None,
) -> bool:
    """Push the brief to Telegram: summary text → N tweet URLs (each gets
    its own message so Telegram renders preview cards) → HTML file
    attachment. Returns True on full success."""
    if not TELEGRAM_ENABLED:
        return False
    if not TELEGRAM_CHAT_ID:
        log("  telegram: no chat_id in config — skipping")
        return False
    try:
        token = keychain_get(TELEGRAM_KEYCHAIN_SERVICE)
    except RuntimeError as e:
        log(f"  telegram: keychain read failed: {e}")
        return False

    sent_ok = True

    # 1. Short text summary
    summary = build_telegram_summary(
        claude_markdown, item_count, len(FEEDS), defillama_snapshot
    )
    # Use plain text instead of Markdown for the summary — Telegram's
    # Markdown parser is strict and Claude's output often contains
    # unmatched asterisks, brackets, or other chars that break it.
    # Plain text is reliable and readable.
    resp = _telegram_post(
        token, "sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": summary.replace("*", "").replace("_", ""),
            "disable_web_page_preview": "true",
        },
    )
    if not resp.get("ok"):
        log(f"  telegram summary failed: {resp.get('description', resp)}")
        sent_ok = False

    # 2. Key tweet previews — one URL per message so Telegram makes cards.
    # Cap to TELEGRAM_KEY_TWEETS. If Claude didn't emit enough, just skip the rest.
    for i, url in enumerate(key_tweet_urls[:TELEGRAM_KEY_TWEETS], 1):
        resp = _telegram_post(
            token, "sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": url,
                "disable_web_page_preview": "false",
            },
        )
        if not resp.get("ok"):
            log(f"  telegram tweet {i} failed: {resp.get('description', resp)}")

    # 3. HTML file attachment — tap-to-open in Telegram's built-in browser
    try:
        with open(html_path, "rb") as f:
            caption = (
                f"Full brief · {item_count} items · "
                f"{stats.get('duration_seconds', 0)}s · "
                f"tap to read"
            )
            resp = _telegram_post(
                token, "sendDocument",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": caption,
                },
                files={
                    "document": (html_path.name, f, "text/html"),
                },
                timeout=60,
            )
        if not resp.get("ok"):
            log(f"  telegram sendDocument failed: {resp.get('description', resp)}")
            sent_ok = False
    except Exception as e:
        log(f"  telegram sendDocument error: {e}")
        sent_ok = False

    return sent_ok


# --- local file output + notification ---------------------------------------

def write_brief_to_disk(
    html: str,
    stats: dict,
    item_count: int,
    raw_markdown: str = "",
) -> Path:
    """Write the HTML brief to a timestamped archive file and update the
    latest-brief.html pointer. Also writes the raw Claude markdown as a
    sibling .md file so future runs can feed it back for topic-level dedup.
    Returns the path to the HTML archive file."""
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    archive_path = BRIEFS_DIR / f"{now:%Y-%m-%d_%H-%M}.html"
    archive_path.write_text(html)
    if raw_markdown:
        md_path = BRIEFS_DIR / f"{now:%Y-%m-%d_%H-%M}.md"
        md_path.write_text(raw_markdown)
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
    HOME.mkdir(exist_ok=True)
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

    # Fetch DefiLlama snapshot in parallel-ish with whatever comes next.
    # This is a quick I/O-bound call (~1-3 seconds total for 4 endpoints),
    # so we just do it inline before the claude -p call.
    log("fetching DefiLlama snapshot")
    defillama_snapshot = fetch_defillama_snapshot()
    log(
        f"  defillama sections: "
        f"{', '.join(defillama_snapshot.keys()) if defillama_snapshot else '(none)'}"
    )
    defillama_text = render_defillama_for_prompt(defillama_snapshot)

    log("fetching MEV snapshot (relayscan.io)")
    mev_snapshot = fetch_mev_snapshot()
    if mev_snapshot:
        log(
            f"  mev: {len(mev_snapshot.get('relays',[]))} relays, "
            f"{len(mev_snapshot.get('builders',[]))} builders, "
            f"{len(mev_snapshot.get('profits',[]))} profit rows"
        )
    else:
        log("  mev: no data")
    mev_text = render_mev_for_prompt(mev_snapshot)

    # Load the last few briefs so Claude can avoid repeating stories it
    # already wrote about in the recent past.
    recent_briefs = load_recent_brief_context()
    if recent_briefs:
        log(f"loaded recent brief context ({len(recent_briefs)} chars)")
    else:
        log("no recent briefs to dedup against (first run or empty archive)")

    # Combine the two onchain-data text blocks so Claude sees both
    combined_data_text = "\n\n".join(x for x in [defillama_text, mev_text] if x)

    try:
        claude_markdown, stats = summarize(
            new_items,
            defillama_text=combined_data_text,
            recent_briefs=recent_briefs,
        )
        log(
            f"summary generated ({len(claude_markdown)} chars) · "
            f"duration={stats['duration_seconds']}s"
        )
    except Exception as e:
        log(f"ERROR calling claude -p: {e}")
        notify_macos(
            title="Brief FAILED",
            subtitle=f"claude -p error ({type(e).__name__})",
            message=str(e)[:140],
        )
        return 3

    # Extract the ## Key Tweets section from Claude's output — URLs are
    # used for Telegram link previews and the section is stripped so it
    # never appears in the HTML document the user reads.
    claude_markdown, key_tweet_urls = extract_and_strip_key_tweets(claude_markdown)
    if key_tweet_urls:
        log(f"extracted {len(key_tweet_urls)} key tweet URLs for Telegram")

    # Post-synthesis topic filter: compact second pass against recent-topics.md
    # marks each bullet KEEP / COLLAPSE / DROP. The compact prompt (draft +
    # headline list only) lets the instruction actually bind — unlike at the
    # tail of a 100KB synthesis prompt. Safe by default: any failure falls
    # through to shipping the unfiltered draft.
    filter_stats = {"ok": False, "reason": "disabled"}
    if TOPIC_FILTER_ENABLED:
        recent_topics, _pruned = load_recent_topics_for_filter()
        filtered_markdown, filter_stats = filter_stale_bullets(
            claude_markdown, recent_topics
        )
        if filter_stats.get("ok"):
            claude_markdown = filtered_markdown
    stats["filter"] = filter_stats

    # Capture the brief's topic-bearing markdown BEFORE journal prepend, so
    # the project notes that get stitched on for morning runs don't pollute
    # recent-topics.md with non-news entries.
    topics_source_markdown = claude_markdown

    # Journal prepend removed — user found the previous-day Claude Code
    # session summaries were often incomplete or inaccurate, and preferred
    # to drop them entirely rather than ship unreliable info.

    html = render_brief_html(
        claude_markdown,
        new_items,
        stats,
        defillama_snapshot=defillama_snapshot,
        mev_snapshot=mev_snapshot,
    )
    archive_path = write_brief_to_disk(
        html, stats, len(new_items), raw_markdown=claude_markdown
    )
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
    # Include feed-failure count in notification subtitle so user sees it
    # immediately, before even opening the brief.
    subtitle = f"{len(new_items)} items across {len(FEEDS)} sources"
    if FAILED_FEEDS:
        subtitle += f" · {len(FAILED_FEEDS)} feeds failed"
    notify_macos(
        title="Research Brief ready",
        subtitle=subtitle,
        message=f"Top categories: {top_cats}",
    )

    # Push to Telegram if enabled. Non-fatal: failure logs but doesn't
    # block state update or return code.
    if TELEGRAM_ENABLED:
        log("pushing to Telegram")
        try:
            ok = send_telegram_brief(
                html_path=archive_path,
                claude_markdown=claude_markdown,
                key_tweet_urls=key_tweet_urls,
                item_count=len(new_items),
                stats=stats,
                defillama_snapshot=defillama_snapshot,
            )
            log(f"  telegram: {'sent' if ok else 'partial/failed'}")
        except Exception as e:
            log(f"  telegram error: {e}")

    state["seen_ids"].extend(it["id"] for it in new_items)
    save_state(state)
    log(f"state updated — now tracking {len(state['seen_ids'])} ids")

    # Persist this run's final headlines to recent-topics.md for the next
    # run's filter pass. Use the *post-filter* markdown so COLLAPSE/DROP
    # decisions don't reappear as "already covered" next time.
    try:
        brief_label = f"briefs/{archive_path.name}"
        save_recent_topics(topics_source_markdown, brief_label, datetime.now())
    except Exception as e:
        log(f"  topics: unexpected error ({type(e).__name__}: {e})")

    log("digest run end")
    return 0


def _main_with_fatal_notifier() -> int:
    """Wraps main() so any uncaught exception fires a failure notification
    and gets logged — nothing fails silently."""
    try:
        return main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        log("interrupted by user (SIGINT)")
        return 130
    except Exception as e:
        log(f"FATAL: uncaught {type(e).__name__}: {e}")
        import traceback
        log(traceback.format_exc())
        try:
            notify_macos(
                title="Brief CRASHED",
                subtitle=f"uncaught {type(e).__name__}",
                message=str(e)[:140],
            )
        except Exception:
            pass
        return 99


if __name__ == "__main__":
    sys.exit(_main_with_fatal_notifier())
