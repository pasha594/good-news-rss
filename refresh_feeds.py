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
    servers our LibreSSL-built requests can't handshake with. Tries a browser
    UA first, then curl's default UA (some servers, e.g. ESPN, reject a Chrome
    UA coming from a non-browser client but accept plain clients)."""
    for ua_args in (["-A", UA], []):
        try:
            p = subprocess.run(
                ["curl", "-sL", "--compressed", "-m", "20", *ua_args,
                 "-H", "Accept: application/rss+xml, application/atom+xml, "
                       "application/xml, text/xml, */*", url],
                capture_output=True, timeout=25)
            if p.returncode == 0 and p.stdout and len(p.stdout) > 100:
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
            .replace("__REFRESH_NOTE__",
                     "Refresh: <code>python3 refresh_feeds.py</code>")
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


TEMPLATE = (SCRIPT_DIR / "page_template.html").read_text(encoding="utf-8")


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
