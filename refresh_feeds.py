#!/usr/bin/env python3
"""Fetch every feed listed in the Good News RSS Feeds spreadsheet and build a
self-contained, filterable HTML page of articles.

Usage:
    python3 refresh_feeds.py                 # fetch feeds, then rebuild the page
    python3 refresh_feeds.py --skip-fetch    # rebuild the page from cached feeds_data.json
    python3 refresh_feeds.py --xlsx PATH     # use a different spreadsheet

Outputs (next to this script):
    Good News Feeds.html   the page (open in any browser)
    feeds_data.json        cached fetch results (used by --skip-fetch)
"""

import argparse
import calendar
import csv
import os
import subprocess
import concurrent.futures
import html as htmlmod
import json
import re
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

warnings.filterwarnings("ignore")

import feedparser
import openpyxl
import requests

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_XLSX = Path("/Users/pasha/Downloads/Good News RSS Feeds.xlsx")
OUT_HTML = SCRIPT_DIR / "Good News Feeds.html"
CACHE_JSON = SCRIPT_DIR / "feeds_data.json"

def _guardian_key():
    """Key comes from the GUARDIAN_API_KEY env var (e.g. a GitHub Actions secret)
    or a local .guardian_key file (gitignored). Never commit the key itself."""
    k = os.environ.get("GUARDIAN_API_KEY", "").strip()
    if k:
        return k
    f = SCRIPT_DIR / ".guardian_key"
    return f.read_text().strip() if f.exists() else ""


GUARDIAN_API_KEY = _guardian_key()

# Premium sources, as specified by the user (supersedes the sheet's highlighting).
PREMIUM = {
    "BBC News", "News24", "New York Post", "Axios", "The Guardian", "Al Jazeera",
    "Sydney Morning Herald", "Telegraph", "Los Angeles Times", "The Boston Globe",
    "CBC News", "The Times", "Fortune", "The Independent",
    "Times of India - Times Now", "Telegraph India", "New York Times", "Haaretz",
    "Sports Illustrated", "Saudi Gazette", "Washington Post", "Times of India",
    "Syndigate - Boston.com", "Forbes", "CityAM", "ArtNews",
}

# Hand-verified working feed URLs for sources whose sheet URLs are dead/blocked.
# Tried before the sheet's own URLs.
OVERRIDES = {
    "Forbes": ["https://www.forbes.com/most-popular/feed/",
               "https://www.forbes.com/business/feed/"],
    "Los Angeles Times": ["https://www.latimes.com/rss2.0.xml"],
    "Sports Illustrated": ["https://www.si.com/feed"],
    "The Times": ["https://www.thetimes.com/uk/rss"],
    "Saudi Gazette": ["https://saudigazette.com.sa/rssFeed/74"],
    "US News & World Report": ["https://www.usnews.com/rss/news"],
}

# Researched/verified feed URLs (feed_overrides.json next to this script,
# {"Source Name": ["url", ...], ...}) take priority over everything else.
_ov_path = SCRIPT_DIR / "feed_overrides.json"
if _ov_path.exists():
    import json as _json
    for _k, _v in _json.loads(_ov_path.read_text()).items():
        OVERRIDES[_k] = list(_v) + OVERRIDES.get(_k, [])

MAX_PER_SOURCE = 200          # sanity cap per source
MAX_URLS_PER_SOURCE = 6       # listed candidates + autodiscovered ones
DESC_MAX = 400
TITLE_MAX = 300
TIMEOUT = (8, 15)             # connect, read
WORKERS = 32
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

URL_RE = re.compile(r"https?://[^\s'\")\]]+")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def mask(s):
    if not GUARDIAN_API_KEY:
        return str(s)
    return str(s).replace(GUARDIAN_API_KEY, "***")


def extract_urls(cell):
    """Pull usable URLs out of a spreadsheet cell, skipping <placeholder> patterns."""
    out = []
    for tok in URL_RE.findall(cell or ""):
        tok = tok.rstrip(".,;)")
        if "<" in tok or ">" in tok:
            continue
        if tok not in out:
            out.append(tok)
    return out


def load_sources(xlsx_path):
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb["Feeds"]
    sources = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        name = (row[0] or "").strip() if row[0] else ""
        if not name:
            continue
        firehose = str(row[2] or "")
        main = str(row[3] or "")
        candidates = (OVERRIDES.get(name, []) +
                      extract_urls(firehose) + extract_urls(main))
        # Last resort: the source's homepage (feed autodiscovery kicks in on HTML)
        website = str(row[1] or "").strip()
        if website and website not in ("-", ""):
            if not website.startswith("http"):
                website = "https://" + website
            candidates.append(website.rstrip("/"))
        # de-dupe, preserve order
        seen, cands = set(), []
        for u in candidates:
            if u not in seen:
                seen.add(u)
                cands.append(u)
        sources.append({"name": name, "candidates": cands, "premium": name in PREMIUM})
    # Supplementary sources not in the spreadsheet (extra_sources.json next to this
    # script): [{"name": ..., "urls": [...feed guesses..., homepage]}, ...]
    extra_path = SCRIPT_DIR / "extra_sources.json"
    if extra_path.exists():
        existing = {s["name"] for s in sources}
        for e in json.loads(extra_path.read_text()):
            if e["name"] not in existing:
                cands = OVERRIDES.get(e["name"], []) + list(e["urls"])
                sources.append({"name": e["name"], "candidates": cands,
                                "premium": e["name"] in PREMIUM})
    return sources


def clean_text(s, limit):
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    s = htmlmod.unescape(s)
    s = WS_RE.sub(" ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def entry_epoch(e, now):
    t = e.get("published_parsed") or e.get("updated_parsed")
    if not t:
        return 0
    try:
        epoch = calendar.timegm(t)
    except Exception:
        return 0
    if epoch > now + 2 * 86400:  # bogus future date -> treat as undated
        return 0
    return epoch


def parse_feed_bytes(content, source_name, now):
    fp = feedparser.parse(content)
    if not fp.entries:
        return None
    articles, seen = [], set()
    for e in fp.entries[: MAX_PER_SOURCE * 2]:
        title = clean_text(e.get("title", ""), TITLE_MAX)
        if not title:
            continue
        link = e.get("link") or e.get("id") or ""
        key = link or title
        if key in seen:
            continue
        seen.add(key)
        desc = e.get("summary") or e.get("description") or ""
        if not desc and e.get("content"):
            try:
                desc = e["content"][0].get("value", "")
            except Exception:
                desc = ""
        articles.append({
            "title": title,
            "link": link,
            "desc": clean_text(desc, DESC_MAX),
            "t": entry_epoch(e, now),
        })
        if len(articles) >= MAX_PER_SOURCE:
            break
    return articles or None


def discover_feed_urls(html_text, base_url):
    """Find feed URLs inside an HTML page (directory pages like nytimes.com/rss)."""
    found = []
    # 1) proper autodiscovery <link> tags
    for tag in re.findall(r"<link\b[^>]*>", html_text, re.I):
        if re.search(r"type\s*=\s*[\"']application/(rss|atom)\+xml", tag, re.I):
            m = re.search(r"href\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
            if m:
                found.append(urljoin(base_url, m.group(1)))
    # 2) anchors that look like feeds, same registrable domain only
    base_dom = ".".join(urlparse(base_url).netloc.split(".")[-2:])
    for href in re.findall(r"href\s*=\s*[\"']([^\"']+)[\"']", html_text, re.I):
        if not re.search(r"(rss|feed|\.xml)(\b|$)", href, re.I):
            continue
        if re.search(r"(comment|sitemap|xmlrpc|feedback|manifest)", href, re.I):
            continue
        absu = urljoin(base_url, href)
        p = urlparse(absu)
        if p.scheme not in ("http", "https"):
            continue
        if not p.netloc.endswith(base_dom):
            continue
        if re.search(r"\.(css|js|png|jpg|svg|ico)(\?|$)", absu, re.I):
            continue
        found.append(absu)
    out, seen = [], set()
    for u in found:
        if u not in seen and u != base_url:
            seen.add(u)
            out.append(u)
    return out


def fetch_guardian(now):
    url = ("https://content.guardianapis.com/search?api-key=" + GUARDIAN_API_KEY +
           "&page-size=50&order-by=newest&show-fields=trailText")
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
    r.raise_for_status()
    results = r.json()["response"]["results"]
    articles = []
    for it in results:
        title = clean_text(it.get("webTitle", ""), TITLE_MAX)
        if not title:
            continue
        t = 0
        if it.get("webPublicationDate"):
            try:
                dt = datetime.strptime(it["webPublicationDate"], "%Y-%m-%dT%H:%M:%SZ")
                t = calendar.timegm(dt.timetuple())
            except Exception:
                t = 0
        articles.append({
            "title": title,
            "link": it.get("webUrl", ""),
            "desc": clean_text((it.get("fields") or {}).get("trailText", ""), DESC_MAX),
            "t": t if t <= now + 2 * 86400 else 0,
        })
    return articles


COMMON_PATTERNS = ["/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml",
                   "/index.xml", "/news/feed/", "/en/rss"]


def curl_fetch(url):
    """Fallback fetch via curl: different TLS stack, passes some bot-walls and
    servers our LibreSSL-built requests can't handshake with."""
    try:
        p = subprocess.run(
            ["curl", "-sL", "--compressed", "-m", "20", "-A", UA,
             "-H", "Accept: application/rss+xml, application/atom+xml, "
                   "application/xml, text/xml, */*", url],
            capture_output=True, timeout=25)
        if p.returncode == 0 and p.stdout:
            return p.stdout
    except Exception:
        pass
    return None


def try_feed_url(url, name, now):
    """Fetch one URL. Returns (articles|None, note, html_text|None, final_url)."""
    content, ctype, final_url, note = None, "", url, None
    try:
        r = requests.get(url, timeout=TIMEOUT,
                         headers={"User-Agent": UA,
                                  "Accept": "application/rss+xml, application/atom+xml, "
                                            "application/xml, text/xml, */*"})
        if r.status_code >= 400:
            note = f"HTTP {r.status_code}"
        else:
            content = r.content
            ctype = r.headers.get("content-type", "")
            final_url = r.url
    except Exception as exc:
        note = type(exc).__name__
    if content is None:
        content = curl_fetch(url)
        if content is None:
            return None, note or "fetch failed", None, url
    articles = parse_feed_bytes(content, name, now)
    if articles:
        return articles, "ok", None, final_url
    head = content[:2048].lower()
    if b"<html" in head or "text/html" in ctype:
        return None, "HTML page", content.decode("utf-8", "replace"), final_url
    return None, "no entries", None, final_url


def fetch_source(src):
    """Try candidates in order; autodiscover on HTML pages; then common patterns."""
    now = int(time.time())
    name = src["name"]

    if name == "The Guardian":
        try:
            articles = fetch_guardian(now)
            if articles:
                return {"name": name, "ok": True, "used": "Guardian Content API",
                        "articles": articles}
        except Exception:
            pass  # fall through to RSS candidates

    reasons = []
    seen_urls = set(src["candidates"])

    # Phase 1: listed candidates, with autodiscovery on HTML directory pages
    tried = 0
    queue = list(src["candidates"])
    while queue and tried < MAX_URLS_PER_SOURCE:
        url = queue.pop(0)
        tried += 1
        try:
            articles, note, html_text, final_url = try_feed_url(url, name, now)
            if articles:
                return {"name": name, "ok": True, "used": mask(url), "articles": articles}
            if html_text is not None:
                added = 0
                for u in discover_feed_urls(html_text, final_url):
                    if u not in seen_urls and added < 3:
                        seen_urls.add(u)
                        queue.insert(added, u)  # try discovered feeds next, in order
                        added += 1
                reasons.append(f"{mask(url)} -> HTML page ({added} feeds discovered)")
            else:
                reasons.append(f"{mask(url)} -> {note}")
        except Exception as exc:
            reasons.append(f"{mask(url)} -> {mask(type(exc).__name__ + ': ' + str(exc))[:100]}")

    # Phase 2: probe common feed paths on each origin we know for this source
    origins = []
    for u in src["candidates"]:
        p = urlparse(u)
        if p.scheme in ("http", "https"):
            o = f"{p.scheme}://{p.netloc}"
            if o not in origins:
                origins.append(o)
    probed = 0
    for origin in origins:
        for path in COMMON_PATTERNS:
            if probed >= 10:
                break
            url = origin + path
            if url in seen_urls:
                continue
            seen_urls.add(url)
            probed += 1
            try:
                articles, note, html_text, final_url = try_feed_url(url, name, now)
                if articles:
                    return {"name": name, "ok": True, "used": mask(url) + " (pattern)",
                            "articles": articles}
                # a pattern URL that serves an HTML feed directory: mine it
                if html_text is not None:
                    for u in discover_feed_urls(html_text, final_url)[:2]:
                        if u in seen_urls or probed >= 10:
                            continue
                        seen_urls.add(u)
                        probed += 1
                        try:
                            arts2, _, _, _ = try_feed_url(u, name, now)
                            if arts2:
                                return {"name": name, "ok": True,
                                        "used": mask(u) + " (discovered)",
                                        "articles": arts2}
                        except Exception:
                            pass
            except Exception:
                pass  # patterns are speculative; don't clutter the failure reason

    return {"name": name, "ok": False, "used": None,
            "reason": "; ".join(reasons) or "no candidate URLs", "articles": []}


def run_fetch(xlsx_path):
    sources = load_sources(xlsx_path)
    with_urls = [s for s in sources if s["candidates"] or s["name"] == "The Guardian"]
    print(f"{len(sources)} sources in sheet; {len(with_urls)} have candidate feed URLs")
    results = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_source, s): s for s in with_urls}
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            res["premium"] = futs[fut]["premium"]
            results.append(res)
            done += 1
            status = f"{len(res['articles']):4d} articles" if res["ok"] else "FAILED"
            print(f"[{done}/{len(with_urls)}] {res['name'][:40]:40s} {status}", flush=True)
    data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "results": results,
    }
    CACHE_JSON.write_text(json.dumps(data, ensure_ascii=False))
    ok = sum(1 for r in results if r["ok"])
    total = sum(len(r["articles"]) for r in results)
    print(f"\nFetched {total} articles from {ok} sources "
          f"({len(results) - ok} sources returned nothing)")
    return data


# ---------------------------------------------------------------- HTML build

def build_html(data):
    results = sorted(data["results"], key=lambda r: r["name"].lower())
    ok_sources = [r for r in results if r["articles"]]
    failed = [r for r in results if not r["articles"]]

    sources_payload = []
    articles_payload = []
    for si, r in enumerate(ok_sources):
        sources_payload.append({"n": r["name"], "p": 1 if r["premium"] else 0,
                                "c": len(r["articles"])})
        for a in r["articles"]:
            articles_payload.append([si, a["title"], a["link"], a["desc"], a["t"]])

    payload = json.dumps({"generated": data["generated"], "sources": sources_payload,
                          "articles": articles_payload},
                         ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    fail_items = "".join(
        f"<li><strong>{htmlmod.escape(r['name'])}</strong>"
        f"<span> — {htmlmod.escape(mask(r.get('reason', 'no articles')))}</span></li>"
        for r in failed)

    page = (TEMPLATE
            .replace("__PAYLOAD__", payload)
            .replace("__N_SOURCES__", str(len(ok_sources)))
            .replace("__N_ARTICLES__", f"{len(articles_payload):,}")
            .replace("__N_FAILED__", str(len(failed)))
            .replace("__FAIL_LIST__", fail_items))
    OUT_HTML.write_text(page, encoding="utf-8")
    size_mb = OUT_HTML.stat().st_size / 1e6
    print(f"Wrote {OUT_HTML.name}: {len(articles_payload)} articles, "
          f"{len(ok_sources)} sources, {size_mb:.1f} MB")


OUT_CSV = SCRIPT_DIR / "feed_status.csv"
OUT_FEEDLIST = SCRIPT_DIR / "working_feeds.json"


def build_feedlist(data):
    """Small list of known-working feeds, consumed by accumulate.py."""
    feeds = []
    for r in sorted(data["results"], key=lambda x: x["name"].lower()):
        if not r["articles"]:
            continue
        url = r.get("used") or ""
        for suffix in (" (pattern)", " (discovered)"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
        feeds.append({"name": r["name"], "url": url,
                      "premium": bool(r.get("premium"))})
    OUT_FEEDLIST.write_text(json.dumps(feeds, ensure_ascii=False, indent=1))
    print(f"Wrote {OUT_FEEDLIST.name}: {len(feeds)} working feeds")


def build_csv(data):
    rows = sorted(data["results"], key=lambda r: r["name"].lower())
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source", "rss_url", "premium", "returned", "article_count"])
        for r in rows:
            url = r.get("used") or ""
            for suffix in (" (pattern)", " (discovered)"):
                if url.endswith(suffix):
                    url = url[: -len(suffix)]
            w.writerow([r["name"], url, "yes" if r.get("premium") else "no",
                        "yes" if r["articles"] else "no", len(r["articles"])])
    print(f"Wrote {OUT_CSV.name}: {len(rows)} sources")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Good News Feeds</title>
<style>
  :root {
    --bg: #f4f7f5; --panel: #ffffff; --text: #1c2420; --muted: #5c6b63;
    --border: #dde5e0; --accent: #157a46; --accent-soft: #e3f2e9;
    --link: #0f62b7; --row-alt: #fafcfb; --hover: #eef5f1;
    --badge-bg: #d9ead3; --badge-text: #1e5c33; --badge-border: #b7d6ac;
    --input-bg: #ffffff; --shadow: 0 8px 24px rgba(20, 40, 30, .12);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #101513; --panel: #171d1a; --text: #e4eae6; --muted: #93a29a;
      --border: #2a332e; --accent: #4cc98a; --accent-soft: #1d3328;
      --link: #6cb2ff; --row-alt: #1b211e; --hover: #212a25;
      --badge-bg: #1e3a2a; --badge-text: #8fd8ae; --badge-border: #2e5540;
      --input-bg: #121815; --shadow: 0 8px 24px rgba(0, 0, 0, .5);
    }
  }
  * { box-sizing: border-box; margin: 0; }
  html, body { height: 100%; }
  body {
    font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text);
    display: flex; flex-direction: column;
  }
  header { padding: 14px 20px 10px; flex: none; }
  .titlebar { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
  h1 { font-size: 20px; letter-spacing: -.2px; }
  h1 .leaf { color: var(--accent); }
  .stats { color: var(--muted); font-size: 13px; }
  .stats b { color: var(--text); }
  .meta { margin-top: 4px; font-size: 12px; color: var(--muted);
          display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
  .meta details { display: inline; }
  .meta summary { cursor: pointer; color: var(--accent); }
  .meta ul { margin: 6px 0 4px 18px; max-height: 180px; overflow: auto; }
  .meta li span { color: var(--muted); }
  #clearBtn {
    border: 1px solid var(--border); background: var(--panel); color: var(--muted);
    border-radius: 6px; padding: 2px 10px; font-size: 12px; cursor: pointer; display: none;
  }
  #clearBtn.active { display: inline-block; color: var(--accent); border-color: var(--accent); }

  .table-wrap { flex: 1; overflow: auto; padding: 0 20px 20px; }
  table { border-collapse: separate; border-spacing: 0; width: 100%; min-width: 900px;
          background: var(--panel); border: 1px solid var(--border); border-radius: 10px; }
  thead th { position: sticky; background: var(--panel); z-index: 3; text-align: left; }
  thead tr.labels th {
    top: 0; padding: 10px 12px 6px; font-size: 12px; text-transform: uppercase;
    letter-spacing: .06em; color: var(--muted); user-select: none;
  }
  thead tr.labels th.sortable { cursor: pointer; }
  thead tr.labels th.sortable:hover { color: var(--accent); }
  thead tr.labels th .arrow { font-size: 10px; margin-left: 4px; }
  thead tr.filters th { top: 33px; padding: 0 12px 10px; border-bottom: 1px solid var(--border); }
  tr.filters th > input, tr.filters th > select, #srcBtn {
    width: 100%; padding: 6px 9px; font-size: 13px; color: var(--text);
    background: var(--input-bg); border: 1px solid var(--border); border-radius: 7px;
  }
  tr.filters th > input:focus, tr.filters th > select:focus,
  .dd-head input:focus { outline: 2px solid var(--accent-soft); border-color: var(--accent); }
  .dd-item input { flex: none; width: auto; }
  #srcBtn { text-align: left; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #srcBtn.filtered { border-color: var(--accent); color: var(--accent); font-weight: 600; }

  .dd { position: relative; }
  .dd-panel {
    display: none; position: absolute; top: calc(100% + 6px); left: 0; z-index: 50;
    width: 340px; max-height: 420px; background: var(--panel);
    border: 1px solid var(--border); border-radius: 10px; box-shadow: var(--shadow);
    flex-direction: column;
  }
  .dd-panel.open { display: flex; }
  .dd-head { padding: 10px 10px 8px; border-bottom: 1px solid var(--border); }
  .dd-head input { width: 100%; padding: 6px 9px; font-size: 13px;
    border: 1px solid var(--border); border-radius: 7px; background: var(--input-bg); color: var(--text); }
  .dd-links { display: flex; gap: 12px; margin-top: 8px; font-size: 12px; }
  .dd-links a { color: var(--accent); cursor: pointer; font-weight: 600; }
  .dd-list { overflow: auto; padding: 6px 4px; }
  .dd-item { display: flex; align-items: center; gap: 8px; padding: 5px 10px;
             border-radius: 6px; cursor: pointer; font-size: 13px; }
  .dd-item:hover { background: var(--hover); }
  .dd-item .nm { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .dd-item .cnt { margin-left: auto; padding-left: 8px; color: var(--muted); font-size: 12px;
                  font-variant-numeric: tabular-nums; }
  .badge {
    display: inline-block; font-size: 9.5px; font-weight: 700; letter-spacing: .05em;
    background: var(--badge-bg); color: var(--badge-text); border: 1px solid var(--badge-border);
    padding: 1px 6px; border-radius: 999px; vertical-align: 1px;
  }
  tbody td { padding: 9px 12px; border-top: 1px solid var(--border); vertical-align: top; }
  tbody tr:nth-child(even) { background: var(--row-alt); }
  tbody tr:hover { background: var(--hover); }
  td.src { white-space: nowrap; font-weight: 600; font-size: 13px; }
  td.src .badge { margin-left: 6px; }
  td.hl a { color: var(--link); text-decoration: none; font-weight: 600; }
  td.hl a:hover { text-decoration: underline; }
  td.hl span.nolink { font-weight: 600; }
  td.desc { color: var(--muted); font-size: 13px; }
  td.date { white-space: nowrap; color: var(--muted); font-size: 12.5px;
            font-variant-numeric: tabular-nums; }
  #empty { padding: 40px; text-align: center; color: var(--muted); display: none; }
</style>
</head>
<body>
<header>
  <div class="titlebar">
    <h1><span class="leaf">&#10087;</span> Good News Feeds</h1>
    <span class="stats"><b id="shownCount"></b> shown of <b>__N_ARTICLES__</b> articles
      &middot; <b>__N_SOURCES__</b> sources</span>
    <button id="clearBtn">Clear filters &times;</button>
  </div>
  <div class="meta">
    <span>Fetched <span id="genAt"></span></span>
    <span>Refresh: <code>python3 refresh_feeds.py</code></span>
    <details><summary>__N_FAILED__ sources returned nothing</summary>
      <ul>__FAIL_LIST__</ul>
    </details>
  </div>
</header>

<div class="table-wrap">
  <table>
    <colgroup>
      <col style="width:210px"><col style="width:32%"><col><col style="width:150px">
    </colgroup>
    <thead>
      <tr class="labels">
        <th class="sortable" data-k="s">Source<span class="arrow"></span></th>
        <th class="sortable" data-k="h">Headline<span class="arrow"></span></th>
        <th>Description</th>
        <th class="sortable" data-k="t">Date<span class="arrow"></span></th>
      </tr>
      <tr class="filters">
        <th>
          <div class="dd">
            <button id="srcBtn">All sources</button>
            <div class="dd-panel" id="srcPanel">
              <div class="dd-head">
                <input id="srcSearch" type="search" placeholder="Search sources...">
                <div class="dd-links">
                  <a id="srcAll">All</a><a id="srcNone">None</a>
                  <a id="srcPremium">Premium only</a>
                </div>
              </div>
              <div class="dd-list" id="srcList"></div>
            </div>
          </div>
        </th>
        <th><input id="hlFilter" type="search" placeholder="Filter headlines..."></th>
        <th><input id="descFilter" type="search" placeholder="Filter descriptions..."></th>
        <th>
          <select id="dateFilter">
            <option value="all">All time</option>
            <option value="86400">Last 24 hours</option>
            <option value="259200">Last 3 days</option>
            <option value="604800">Last 7 days</option>
            <option value="2592000">Last 30 days</option>
          </select>
        </th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
  <div id="empty">No articles match the current filters.</div>
  <div id="sentinel"></div>
</div>

<script id="data" type="application/json">__PAYLOAD__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("data").textContent);
const SRC = DATA.sources;                  // [{n, p, c}]
const ART = DATA.articles;                 // [srcIdx, title, link, desc, epoch]
const lowerTitle = ART.map(a => a[1].toLowerCase());
const lowerDesc = ART.map(a => a[3].toLowerCase());
const fmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
document.getElementById("genAt").textContent = fmt.format(new Date(DATA.generated));

const state = { src: null, hq: "", dq: "", dp: "all", sortK: "t", sortDir: -1 };
let matched = [];      // indices into ART after filtering+sorting
let rendered = 0;
const CHUNK = 400;

const tbody = document.getElementById("tbody");
const esc = s => s.replace(/[&<>"']/g, m =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));

function badge(si) { return SRC[si].p ? ' <span class="badge">PREMIUM</span>' : ""; }

function rowHtml(i) {
  const a = ART[i];
  const d = a[4] ? fmt.format(new Date(a[4] * 1000)) : "—";
  const hl = a[2]
    ? '<a href="' + esc(a[2]) + '" target="_blank" rel="noopener">' + esc(a[1]) + "</a>"
    : '<span class="nolink">' + esc(a[1]) + "</span>";
  return "<tr><td class='src'>" + esc(SRC[a[0]].n) + badge(a[0]) + "</td>" +
         "<td class='hl'>" + hl + "</td><td class='desc'>" + esc(a[3]) + "</td>" +
         "<td class='date'>" + d + "</td></tr>";
}

function renderChunk() {
  if (rendered >= matched.length) return;
  const end = Math.min(rendered + CHUNK, matched.length);
  let html = "";
  for (let i = rendered; i < end; i++) html += rowHtml(matched[i]);
  tbody.insertAdjacentHTML("beforeend", html);
  rendered = end;
}

function applyFilters() {
  const now = Date.now() / 1000;
  const cut = state.dp === "all" ? null : now - Number(state.dp);
  const srcSel = state.src;
  const hq = state.hq, dq = state.dq;
  matched = [];
  for (let i = 0; i < ART.length; i++) {
    const a = ART[i];
    if (srcSel && !srcSel.has(a[0])) continue;
    if (cut !== null && (!a[4] || a[4] < cut)) continue;
    if (hq && !lowerTitle[i].includes(hq)) continue;
    if (dq && !lowerDesc[i].includes(dq)) continue;
    matched.push(i);
  }
  sortMatched();
  tbody.innerHTML = "";
  rendered = 0;
  renderChunk();
  document.getElementById("shownCount").textContent = matched.length.toLocaleString();
  document.getElementById("empty").style.display = matched.length ? "none" : "block";
  updateClearBtn();
}

function sortMatched() {
  const k = state.sortK, dir = state.sortDir;
  if (k === "t") {
    matched.sort((x, y) => {
      const a = ART[x][4], b = ART[y][4];
      if (!a && !b) return 0;
      if (!a) return 1;              // undated always last
      if (!b) return -1;
      return dir * (a - b);
    });
  } else if (k === "s") {
    matched.sort((x, y) => dir * SRC[ART[x][0]].n.localeCompare(SRC[ART[y][0]].n) ||
                           ART[y][4] - ART[x][4]);
  } else {
    matched.sort((x, y) => dir * ART[x][1].localeCompare(ART[y][1]));
  }
  document.querySelectorAll("tr.labels th.sortable").forEach(th => {
    th.querySelector(".arrow").textContent =
      th.dataset.k === k ? (dir === 1 ? "▲" : "▼") : "";
  });
}

document.querySelectorAll("tr.labels th.sortable").forEach(th => {
  th.addEventListener("click", () => {
    if (state.sortK === th.dataset.k) state.sortDir *= -1;
    else { state.sortK = th.dataset.k; state.sortDir = th.dataset.k === "t" ? -1 : 1; }
    applyFilters();
  });
});

// ---- source dropdown ----
const srcBtn = document.getElementById("srcBtn");
const srcPanel = document.getElementById("srcPanel");
const srcList = document.getElementById("srcList");
const srcOrder = SRC.map((s, i) => i).sort((a, b) => SRC[a].n.localeCompare(SRC[b].n));

function buildSrcList(query) {
  const q = (query || "").toLowerCase();
  let html = "";
  for (const i of srcOrder) {
    const s = SRC[i];
    if (q && !s.n.toLowerCase().includes(q)) continue;
    const checked = !state.src || state.src.has(i) ? " checked" : "";
    html += '<label class="dd-item"><input type="checkbox" data-i="' + i + '"' + checked +
            '><span class="nm">' + esc(s.n) +
            (s.p ? ' <span class="badge">PREMIUM</span>' : "") +
            '</span><span class="cnt">' + s.c.toLocaleString() + "</span></label>";
  }
  srcList.innerHTML = html;
}

function updateSrcBtn() {
  if (!state.src) { srcBtn.textContent = "All sources (" + SRC.length + ")"; srcBtn.classList.remove("filtered"); }
  else {
    srcBtn.textContent = state.src.size + " of " + SRC.length + " sources";
    srcBtn.classList.add("filtered");
  }
}

srcBtn.addEventListener("click", e => {
  e.stopPropagation();
  const open = srcPanel.classList.toggle("open");
  if (open) { buildSrcList(document.getElementById("srcSearch").value); }
});
document.addEventListener("click", e => {
  if (!srcPanel.contains(e.target) && e.target !== srcBtn) srcPanel.classList.remove("open");
});
srcList.addEventListener("change", e => {
  const cb = e.target;
  if (!cb.dataset.i) return;
  let set = state.src;
  if (!set) { set = new Set(SRC.map((s, i) => i)); }   // was "all"
  const i = Number(cb.dataset.i);
  if (cb.checked) set.add(i); else set.delete(i);
  state.src = set.size === SRC.length ? null : set;
  updateSrcBtn(); applyFilters();
});
document.getElementById("srcSearch").addEventListener("input", e => buildSrcList(e.target.value));
document.getElementById("srcAll").addEventListener("click", () => {
  state.src = null; buildSrcList(srcSearch.value); updateSrcBtn(); applyFilters();
});
document.getElementById("srcNone").addEventListener("click", () => {
  state.src = new Set(); buildSrcList(srcSearch.value); updateSrcBtn(); applyFilters();
});
document.getElementById("srcPremium").addEventListener("click", () => {
  state.src = new Set(SRC.map((s, i) => i).filter(i => SRC[i].p));
  buildSrcList(srcSearch.value); updateSrcBtn(); applyFilters();
});

// ---- text + date filters ----
let debounceTimer;
function debounced(fn) { clearTimeout(debounceTimer); debounceTimer = setTimeout(fn, 150); }
document.getElementById("hlFilter").addEventListener("input", e =>
  debounced(() => { state.hq = e.target.value.toLowerCase().trim(); applyFilters(); }));
document.getElementById("descFilter").addEventListener("input", e =>
  debounced(() => { state.dq = e.target.value.toLowerCase().trim(); applyFilters(); }));
document.getElementById("dateFilter").addEventListener("change", e => {
  state.dp = e.target.value; applyFilters();
});

// ---- clear ----
const clearBtn = document.getElementById("clearBtn");
function anyFilter() {
  return state.src !== null || state.hq || state.dq || state.dp !== "all";
}
function updateClearBtn() { clearBtn.classList.toggle("active", !!anyFilter()); }
clearBtn.addEventListener("click", () => {
  state.src = null; state.hq = ""; state.dq = ""; state.dp = "all";
  document.getElementById("hlFilter").value = "";
  document.getElementById("descFilter").value = "";
  document.getElementById("dateFilter").value = "all";
  document.getElementById("srcSearch").value = "";
  updateSrcBtn(); applyFilters();
});

// ---- infinite scroll ----
const wrap = document.querySelector(".table-wrap");
new IntersectionObserver(entries => {
  if (entries[0].isIntersecting) renderChunk();
}, { root: wrap, rootMargin: "600px" })
  .observe(document.getElementById("sentinel"));
wrap.addEventListener("scroll", () => {
  if (wrap.scrollTop + wrap.clientHeight > wrap.scrollHeight - 1200) renderChunk();
}, { passive: true });

updateSrcBtn();
applyFilters();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-fetch", action="store_true",
                    help="rebuild the HTML from cached feeds_data.json")
    ap.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    args = ap.parse_args()

    if args.skip_fetch:
        if not CACHE_JSON.exists():
            sys.exit("No feeds_data.json cache; run without --skip-fetch first.")
        data = json.loads(CACHE_JSON.read_text())
    else:
        data = run_fetch(Path(args.xlsx))
    build_html(data)
    build_csv(data)
    build_feedlist(data)


if __name__ == "__main__":
    main()
