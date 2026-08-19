#!/usr/bin/env python3
"""Render the GitHub Pages site from the accumulated article store.

Two pages, one template (page_template.html):
  docs/index.html      - all articles from the last MAX_AGE_DAYS
  docs/good-news.html  - the subset tagged good >= GOOD_THRESHOLD by classify.py

Article payload rows:
  [srcIdx, title, link, desc, epoch, cats|0, good|-1,
   virtues|0, topics|0, entities|0, flags]
flags: bit 8 = v3-classified, 1 = impactful, 2 = uplifting, 4 = on_mission.
All tag fields come from folding the tags-* shards by article id.
"""

import calendar
import csv
import hashlib
import html as htmlmod
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import store

ROOT = Path(__file__).resolve().parent
MAX_AGE_DAYS = 7
SLIDER_DEFAULT = 7    # default position of the min-score slider
DASHBOARD_FLOOR = 5   # dashboard embeds articles down to this score


parse_iso = store.parse_iso
article_id = store.article_id


def load_rows():
    # Rows qualify for the page only when published-or-first_seen is within
    # MAX_AGE_DAYS; published is clamped to first_seen+2d at fetch time, so
    # qualifying rows have first_seen >= cutoff-2d. Read just those shards.
    floor = time.time() - (MAX_AGE_DAYS + 2) * 86400
    paths = [p for p in store.shards("articles")
             if store.week_start_epoch(store.shard_key(p, "articles")) + 7 * 86400 >= floor]
    return list(store.read_jsonl(paths))


def load_tags():
    # Fold rows by id: v3 writes a gate row and later an enrichment row for
    # the same article; later fields update, never clobber, earlier ones.
    # Tags are written at-or-after their article's first_seen, so only shards
    # overlapping the display window can hold tags for displayed articles -
    # the fold stays bounded as history grows.
    floor = time.time() - (MAX_AGE_DAYS + 2) * 86400
    paths = [p for p in store.shards("tags")
             if store.week_start_epoch(store.shard_key(p, "tags")) + 7 * 86400 >= floor]
    folded = {}
    for t in store.read_jsonl(paths):
        if "id" in t:
            folded.setdefault(t["id"], {}).update(t)
    return folded


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
                 r.get("_cats") or 0, r.get("_good", -1),
                 r.get("_virtues", 0), r.get("_topics", 0),
                 r.get("_ents", 0), r.get("_flags", 0)]
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
        f = tags.get(article_id(r["source"], r["title"], r.get("link", "")))
        if f:
            n_tagged += 1
            r["_good"] = f.get("good", -1)
            r["_cats"] = f.get("cats", [])
            if f.get("v") == 3:
                r["_flags"] = (8 | (1 if f.get("impactful") else 0)
                               | (2 if f.get("uplifting") else 0)
                               | (4 if f.get("on_mission") else 0))
            r["_virtues"] = f.get("virtues") or 0
            r["_topics"] = f.get("topics") or 0
            ents = f.get("entities") or []
            r["_ents"] = [[e.get("name", ""),
                           1 if e.get("type") == "organization" else 0]
                          for e in ents if e.get("name")] or 0

    n_failed, fails = fail_list()

    build_page(
        rows, "index.html", "Good News Feeds",
        f"Auto-updates every 30 minutes &middot; last {MAX_AGE_DAYS} days shown",
        '<a class="nav" href="good-news.html">&rarr; Good News dashboard</a> '
        '<a class="nav" href="health/">&middot; pipeline health</a>',
        n_failed, fails, good_on=False, slider_min=0)

    good = [r for r in rows if r.get("_good", -1) >= DASHBOARD_FLOOR]
    build_page(
        good, "good-news.html", "Good News Dashboard",
        "Uplifting stories, tagged by AI &mdash; drag the slider to set the "
        "minimum score &middot; auto-updates every 30 minutes",
        '<a class="nav" href="index.html">&rarr; all articles</a> '
        '<a class="nav" href="health/">&middot; pipeline health</a>',
        n_failed, fails, good_on=True, slider_min=DASHBOARD_FLOOR)

    print(f"tags matched onto page rows: {n_tagged}/{len(rows)}; "
          f"dashboard subset (>= {DASHBOARD_FLOOR}): {len(good)}")


if __name__ == "__main__":
    main()
