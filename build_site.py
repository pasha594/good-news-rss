#!/usr/bin/env python3
"""Render docs/index.html (served by GitHub Pages) from the accumulated store.

Runs in the Actions cron after accumulate.py, so the live page tracks
data/articles.jsonl. Shows the most recent MAX_AGE_DAYS of articles (hard cap
MAX_ARTICLES) to keep the page a reasonable size as the store grows. Uses the
same page_template.html as the local snapshot builder; needs only the stdlib.
"""

import calendar
import csv
import html as htmlmod
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_AGE_DAYS = 7
MAX_ARTICLES = 25000


def parse_iso(s):
    if not s:
        return 0
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return 0


def main():
    rows = []
    with (ROOT / "data" / "articles.jsonl").open(encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    def when(r):  # publication time, falling back to when we first saw it
        return parse_iso(r.get("published")) or parse_iso(r.get("first_seen"))

    cutoff = int(time.time()) - MAX_AGE_DAYS * 86400
    rows = [r for r in rows if when(r) >= cutoff]
    rows.sort(key=when, reverse=True)
    rows = rows[:MAX_ARTICLES]

    counts, premium = {}, {}
    for r in rows:
        counts[r["source"]] = counts.get(r["source"], 0) + 1
        premium[r["source"]] = premium.get(r["source"], False) or bool(r.get("premium"))
    src_names = sorted(counts, key=str.lower)
    sidx = {n: i for i, n in enumerate(src_names)}
    sources = [{"n": n, "p": 1 if premium[n] else 0, "c": counts[n]} for n in src_names]
    articles = [[sidx[r["source"]], r["title"], r.get("link", ""),
                 r.get("description", ""), parse_iso(r.get("published"))]
                for r in rows]

    payload = json.dumps(
        {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "sources": sources, "articles": articles},
        ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    fail_items, n_failed = "", 0
    status = ROOT / "feed_status.csv"
    if status.exists():
        with status.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["returned"] == "no":
                    n_failed += 1
                    fail_items += f"<li><strong>{htmlmod.escape(row['source'])}</strong></li>"

    page = ((ROOT / "page_template.html").read_text(encoding="utf-8")
            .replace("__REFRESH_NOTE__",
                     f"Auto-updates every 30 minutes &middot; last {MAX_AGE_DAYS} days shown")
            .replace("__PAYLOAD__", payload)
            .replace("__N_SOURCES__", str(len(sources)))
            .replace("__N_ARTICLES__", f"{len(articles):,}")
            .replace("__N_FAILED__", str(n_failed))
            .replace("__FAIL_LIST__", fail_items))

    out = ROOT / "docs"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"Wrote docs/index.html: {len(articles)} articles, {len(sources)} sources")


if __name__ == "__main__":
    main()
