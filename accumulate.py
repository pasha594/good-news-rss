#!/usr/bin/env python3
"""Fetch every known-working feed and append NEW articles to data/articles.jsonl.

Designed to run every 15 minutes (GitHub Actions or local cron). Lean by design:
no feed discovery or pattern probing — it only hits the URLs in
working_feeds.json (regenerate that with refresh_feeds.py when the source list
changes). Dedupe is by a stable id per article, tracked in data/seen_ids.txt.

Each appended JSONL row:
  {"first_seen": UTC ISO, "source": str, "premium": bool, "title": str,
   "link": str, "description": str, "published": UTC ISO or null}
"""

import calendar
import concurrent.futures
import hashlib
import html as htmlmod
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
FEEDS_FILE = ROOT / "working_feeds.json"
DATA_DIR = ROOT / "data"
ARTICLES = DATA_DIR / "articles.jsonl"
SEEN = DATA_DIR / "seen_ids.txt"

TIMEOUT = (6, 12)
WORKERS = 32
DESC_MAX = 400
TITLE_MAX = 300
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def guardian_key():
    k = os.environ.get("GUARDIAN_API_KEY", "").strip()
    if k:
        return k
    f = ROOT / ".guardian_key"
    return f.read_text().strip() if f.exists() else ""


def clean_text(s, limit):
    if not s:
        return ""
    s = WS_RE.sub(" ", htmlmod.unescape(TAG_RE.sub(" ", s))).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_epoch(e, now):
    t = e.get("published_parsed") or e.get("updated_parsed")
    if not t:
        return None
    try:
        epoch = calendar.timegm(t)
    except Exception:
        return None
    return None if epoch > now + 2 * 86400 else epoch


def http_get(url):
    """requests first, curl fallback (different TLS stack passes more hosts)."""
    try:
        r = requests.get(url, timeout=TIMEOUT,
                         headers={"User-Agent": UA,
                                  "Accept": "application/rss+xml, application/atom+xml, "
                                            "application/xml, text/xml, */*"})
        if r.status_code < 400:
            return r.content
    except Exception:
        pass
    try:
        p = subprocess.run(
            ["curl", "-sL", "--compressed", "-m", "20", "-A", UA, url],
            capture_output=True, timeout=25)
        if p.returncode == 0 and p.stdout:
            return p.stdout
    except Exception:
        pass
    return None


def fetch_feed(src):
    now = int(time.time())
    name, url = src["name"], src["url"]

    if url == "Guardian Content API":
        key = guardian_key()
        if not key:
            return name, []
        try:
            r = requests.get(
                "https://content.guardianapis.com/search?api-key=" + key +
                "&page-size=50&order-by=newest&show-fields=trailText",
                timeout=TIMEOUT, headers={"User-Agent": UA})
            r.raise_for_status()
            out = []
            for it in r.json()["response"]["results"]:
                title = clean_text(it.get("webTitle", ""), TITLE_MAX)
                if not title:
                    continue
                pub = it.get("webPublicationDate")
                out.append({"title": title, "link": it.get("webUrl", ""),
                            "description": clean_text(
                                (it.get("fields") or {}).get("trailText", ""), DESC_MAX),
                            "published": pub})
            return name, out
        except Exception:
            return name, []

    content = http_get(url)
    if content is None:
        return name, []
    fp = feedparser.parse(content)
    out = []
    for e in fp.entries[:300]:
        title = clean_text(e.get("title", ""), TITLE_MAX)
        if not title:
            continue
        desc = e.get("summary") or e.get("description") or ""
        if not desc and e.get("content"):
            try:
                desc = e["content"][0].get("value", "")
            except Exception:
                desc = ""
        epoch = entry_epoch(e, now)
        out.append({"title": title, "link": e.get("link") or e.get("id") or "",
                    "description": clean_text(desc, DESC_MAX),
                    "published": iso(epoch) if epoch else None})
    return name, out


def article_id(source, art):
    key = source + "|" + (art["link"] or art["title"])
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()


def main():
    feeds = json.loads(FEEDS_FILE.read_text())
    premium = {f["name"]: f["premium"] for f in feeds}
    DATA_DIR.mkdir(exist_ok=True)

    seen = set()
    if SEEN.exists():
        seen = set(SEEN.read_text().split())

    first_seen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_rows, new_ids = [], []
    ok_feeds = empty_feeds = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for name, articles in ex.map(fetch_feed, feeds):
            if articles:
                ok_feeds += 1
            else:
                empty_feeds += 1
            for a in articles:
                aid = article_id(name, a)
                if aid in seen:
                    continue
                seen.add(aid)
                new_ids.append(aid)
                new_rows.append({"first_seen": first_seen, "source": name,
                                 "premium": premium.get(name, False), **a})

    if new_rows:
        with ARTICLES.open("a", encoding="utf-8") as f:
            for row in new_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        with SEEN.open("a", encoding="utf-8") as f:
            f.write("".join(i + "\n" for i in new_ids))

    total = sum(1 for _ in ARTICLES.open()) if ARTICLES.exists() else 0
    print(f"+{len(new_rows)} new articles from {ok_feeds} feeds "
          f"({empty_feeds} feeds returned nothing); {total} total stored")


if __name__ == "__main__":
    main()
