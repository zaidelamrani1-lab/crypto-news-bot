#!/usr/bin/env python3
"""
Crypto News -> Telegram bot.

Checks a set of crypto news RSS feeds, finds articles it hasn't seen before,
and posts each new one to your Telegram chat/channel.

Designed to be run on a schedule (e.g. GitHub Actions every 15 minutes).
State (which articles were already sent) is kept in seen.json.

Set two environment variables / GitHub secrets:
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your numeric chat or channel id (e.g. 123456789 or -1001234567890)
"""

import os
import sys
import json
import time
import html
from pathlib import Path

import feedparser
import requests

# ---------------------------------------------------------------------------
# 1. The news sources. Add or remove lines here to change coverage.
#    Format:  "Display name": "RSS feed URL",
# ---------------------------------------------------------------------------
FEEDS = {
    # --- Pure crypto outlets (every article is crypto news) ---
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Bitcoin.com News": "https://news.bitcoin.com/feed/",
    "crypto.news": "https://crypto.news/feed/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "The Block": "https://www.theblock.co/rss.xml",
    "Decrypt": "https://decrypt.co/feed",
    "Bitcoin Magazine": "https://bitcoinmagazine.com/feed",

    # --- Bloomberg's dedicated crypto feed (crypto coverage only) ---
    "Bloomberg Crypto": "https://feeds.bloomberg.com/crypto/news.rss",

    # --- CNBC and The Bitcoin Times have no clean crypto-only RSS of their own,
    #     so we pull their crypto coverage via Google News (filtered by site). ---
    "CNBC (crypto)": "https://news.google.com/rss/search?q=cryptocurrency+OR+bitcoin+site:cnbc.com&hl=en-US&gl=US&ceid=US:en",
    "The Bitcoin Times": "https://news.google.com/rss/search?q=site:btctimes.com&hl=en-US&gl=US&ceid=US:en",
}

# ---------------------------------------------------------------------------
# 2. Settings you can tweak
# ---------------------------------------------------------------------------
MAX_PER_RUN = 25          # safety cap so a busy feed can't flood you
SEEN_KEEP = 3000          # how many article ids to remember (prevents re-sends)
SLEEP_BETWEEN_SENDS = 1.1 # seconds between messages (stay under Telegram limits)
SEEN_FILE = Path(__file__).with_name("seen.json")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_seen():
    """Return (set_of_ids, is_first_run)."""
    if not SEEN_FILE.exists():
        return set(), True
    try:
        data = json.loads(SEEN_FILE.read_text() or "[]")
        ids = set(data)
        return ids, len(ids) == 0
    except Exception:
        return set(), True


def save_seen(ids):
    # keep only the most recent ids so the file doesn't grow forever
    trimmed = list(ids)[-SEEN_KEEP:]
    SEEN_FILE.write_text(json.dumps(trimmed, ensure_ascii=False))


def article_id(entry):
    """A stable unique key for an article."""
    return entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title", "")


def fetch_all():
    """Return a list of (sort_time, source, title, link, uid) for every current item."""
    items = []
    for source, url in FEEDS.items():
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"[warn] could not fetch {source}: {e}", file=sys.stderr)
            continue
        if parsed.bozo and not parsed.entries:
            print(f"[warn] {source} returned no usable entries", file=sys.stderr)
            continue
        for entry in parsed.entries:
            uid = article_id(entry)
            if not uid:
                continue
            # published_parsed is a time.struct_time when available; fall back to now
            t = entry.get("published_parsed") or entry.get("updated_parsed")
            sort_time = time.mktime(t) if t else time.time()
            title = entry.get("title", "(no title)")
            link = entry.get("link", "")
            items.append((sort_time, source, title, link, uid))
    return items


def build_message(source, title, link):
    title = html.escape(title.strip())
    text = f"<b>{title}</b>\n🗞 {source}"
    if link:
        text += f"\n{link}"
    return text


def telegram_send(text):
    """Send one message. Returns True on success."""
    resp = requests.post(
        f"{API}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if resp.status_code == 429:
        retry = resp.json().get("parameters", {}).get("retry_after", 5)
        print(f"[info] rate limited, waiting {retry}s", file=sys.stderr)
        time.sleep(retry + 1)
        return telegram_send(text)
    if not resp.ok:
        print(f"[error] telegram {resp.status_code}: {resp.text}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not TOKEN or not CHAT_ID:
        print("[fatal] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        sys.exit(1)

    seen, first_run = load_seen()
    items = fetch_all()

    if not items:
        print("[info] no items fetched this run")
        return

    # First ever run: don't dump the whole backlog. Mark everything as seen
    # and send a single confirmation so you only get genuinely NEW articles from now on.
    if first_run:
        for _, _, _, _, uid in items:
            seen.add(uid)
        save_seen(seen)
        telegram_send(
            f"✅ Crypto news bot is live. Watching {len(FEEDS)} sources: "
            + ", ".join(FEEDS.keys())
            + ".\nYou'll get new articles as they're published."
        )
        print(f"[info] first run: seeded {len(items)} existing articles")
        return

    # Normal run: find unseen articles, oldest first so they arrive in order.
    new_items = [it for it in items if it[4] not in seen]
    new_items.sort(key=lambda x: x[0])

    if not new_items:
        print("[info] no new articles")
        return

    if len(new_items) > MAX_PER_RUN:
        # keep the newest MAX_PER_RUN, mark the rest as seen silently
        overflow = new_items[:-MAX_PER_RUN]
        for _, _, _, _, uid in overflow:
            seen.add(uid)
        new_items = new_items[-MAX_PER_RUN:]

    sent = 0
    for _, source, title, link, uid in new_items:
        if telegram_send(build_message(source, title, link)):
            seen.add(uid)
            sent += 1
            time.sleep(SLEEP_BETWEEN_SENDS)
        else:
            # stop on hard failure; unsent items stay unseen and retry next run
            break

    save_seen(seen)
    print(f"[info] sent {sent} new article(s)")


if __name__ == "__main__":
    main()
