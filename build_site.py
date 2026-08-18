#!/usr/bin/env python3
"""Render the GitHub Pages site from the accumulated article store.

Two pages, one template (page_template.html):
  docs/index.html      - all articles from the last MAX_AGE_DAYS
  docs/good-news.html  - the subset tagged good >= GOOD_THRESHOLD by classify.py

Article payload rows: [srcIdx, title, link, desc, epoch, cats|0, good|-1]
(the last two are filled from data/tags.jsonl when a tag exists).
"""

import calendar
import csv
import hashlib
import html as htmlmod
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_AGE_DAYS = 7
SLIDER_DEFAULT = 7    # default position of the min-score slider
DASHBOARD_FLOOR = 5   # dashboard embeds articles down to this score


def parse_iso(s):
    if not s:
        return 0
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return 0


def article_id(source, title, link):
    key = source + "|" + (link or title)
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()


def load_rows():
    rows = []
    with (ROOT / "data" / "articles.jsonl").open(encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def load_tags():
    tags = {}
    path = ROOT / "data" / "tags.jsonl"
    if path.exists():
        for line in path.open(encoding="utf-8"):
            try:
                t = json.loads(line)
                tags[t["id"]] = (t.get("good", -1), t.get("cats", []))
            except (json.JSONDecodeError, KeyError):
                pass
    return tags


def fail_list():
    items, n = "", 0
    status = ROOT / "feed_status.csv"
    if status.exists():
        with status.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["returned"] == "no":
                    n += 1
                    items += f"<li><strong>{htmlmod.escape(row['source'])}</strong></li>"
    return n, items


def build_page(rows, out_name, title, refresh_note, nav_link, n_failed, fails,
               good_on=False, slider_min=0):
    counts, premium = {}, {}
    for r in rows:
        counts[r["source"]] = counts.get(r["source"], 0) + 1
        premium[r["source"]] = premium.get(r["source"], False) or bool(r.get("premium"))
    src_names = sorted(counts, key=str.lower)
    sidx = {n: i for i, n in enumerate(src_names)}
    sources = [{"n": n, "p": 1 if premium[n] else 0, "c": counts[n]} for n in src_names]
    articles = [[sidx[r["source"]], r["title"], r.get("link", ""),
                 r.get("description", ""), parse_iso(r.get("published")),
                 r.get("_cats") or 0, r.get("_good", -1)]
                for r in rows]

    payload = json.dumps(
        {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "sources": sources, "articles": articles},
        ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    page = ((ROOT / "page_template.html").read_text(encoding="utf-8")
            .replace("__PAGE_TITLE__", title)
            .replace("__NAV_LINK__", nav_link)
            .replace("__GOOD_DEFAULT_ON__", "true" if good_on else "false")
            .replace("__GOOD_DEFAULT_MIN__", str(SLIDER_DEFAULT))
            .replace("__SLIDER_MIN__", str(slider_min))
            .replace("__REFRESH_NOTE__", refresh_note)
            .replace("__PAYLOAD__", payload)
            .replace("__N_SOURCES__", str(len(sources)))
            .replace("__N_ARTICLES__", f"{len(articles):,}")
            .replace("__N_FAILED__", str(n_failed))
            .replace("__FAIL_LIST__", fails))

    out = ROOT / "docs"
    out.mkdir(exist_ok=True)
    (out / out_name).write_text(page, encoding="utf-8")
    print(f"Wrote docs/{out_name}: {len(articles)} articles, {len(sources)} sources")


def main():
    rows = load_rows()
    tags = load_tags()

    def when(r):
        return parse_iso(r.get("published")) or parse_iso(r.get("first_seen"))

    cutoff = int(time.time()) - MAX_AGE_DAYS * 86400
    rows = [r for r in rows if when(r) >= cutoff]
    rows.sort(key=when, reverse=True)

    n_tagged = 0
    for r in rows:
        tag = tags.get(article_id(r["source"], r["title"], r.get("link", "")))
        if tag:
            n_tagged += 1
            r["_good"], r["_cats"] = tag[0], tag[1]

    n_failed, fails = fail_list()

    build_page(
        rows, "index.html", "Good News Feeds",
        f"Auto-updates every 30 minutes &middot; last {MAX_AGE_DAYS} days shown",
        '<a class="nav" href="good-news.html">&rarr; Good News dashboard</a>',
        n_failed, fails, good_on=False, slider_min=0)

    good = [r for r in rows if r.get("_good", -1) >= DASHBOARD_FLOOR]
    build_page(
        good, "good-news.html", "Good News Dashboard",
        "Uplifting stories, tagged by AI &mdash; drag the slider to set the "
        "minimum score &middot; auto-updates every 30 minutes",
        '<a class="nav" href="index.html">&rarr; all articles</a>',
        n_failed, fails, good_on=True, slider_min=DASHBOARD_FLOOR)

    print(f"tags matched onto page rows: {n_tagged}/{len(rows)}; "
          f"dashboard subset (>= {DASHBOARD_FLOOR}): {len(good)}")


if __name__ == "__main__":
    main()
