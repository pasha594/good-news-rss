#!/usr/bin/env python3
"""Render docs/health/index.html - pipeline health for the last ~50 runs.

Run list (incl. failures) comes from the GitHub Actions API (GITHUB_TOKEN in
CI; unauthenticated locally; page still builds if the API is unreachable).
Per-run stats come from the data itself: every article written in one run
shares one first_seen, every gate row one `at`, every enrichment row one
enriched_at - so rows cluster into runs by timestamp and are matched to API
runs by time window. Clusters with no matching workflow run (local/manual
executions) get their own rows.

Never fails the workflow: the step runs with continue-on-error, and API
problems degrade to a data-only page.
"""

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

import store

ROOT = Path(__file__).resolve().parent
REPO = "pasha594/good-news-rss"
RUNS_SHOWN = 50
LOOKBACK_DAYS = 14


def fetch_runs():
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(
            f"https://api.github.com/repos/{REPO}/actions/workflows/fetch.yml/runs",
            params={"per_page": 60}, headers=headers, timeout=20)
        r.raise_for_status()
        runs = []
        for w in r.json().get("workflow_runs", []):
            runs.append({
                "start": store.parse_iso(w.get("run_started_at")),
                "end": store.parse_iso(w.get("updated_at")),
                "conclusion": w.get("conclusion") or "running",
                "event": w.get("event", ""),
                "url": w.get("html_url", ""),
            })
        return [r_ for r_ in runs if r_["start"]]
    except Exception as exc:
        print(f"health: runs API unavailable ({type(exc).__name__}); data-only page")
        return []


def recent(prefix, suffix=".jsonl"):
    floor = time.time() - LOOKBACK_DAYS * 86400
    return [p for p in store.shards(prefix, suffix)
            if store.week_start_epoch(store.shard_key(p, prefix)) + 7 * 86400 >= floor]


def collect_clusters():
    added = defaultdict(int)                       # first_seen -> articles added
    for row in store.read_jsonl(recent("articles")):
        ts = store.parse_iso(row.get("first_seen"))
        if ts:
            added[ts] += 1

    gate = defaultdict(lambda: {"gated": 0, "skipped": 0, "impactful": 0,
                                "uplifting": 0, "on_mission": 0})
    enr = defaultdict(int)                         # enriched_at -> rows
    for t in store.read_jsonl(recent("tags")):
        if "good" in t:                            # gate row (any version)
            ts = store.parse_iso(t.get("at"))
            if not ts:
                continue
            g = gate[ts]
            if t.get("skip"):
                g["skipped"] += 1
                continue
            g["gated"] += 1
            if t.get("v") == 3:
                g["impactful"] += 1 if t.get("impactful") else 0
                g["uplifting"] += 1 if t.get("uplifting") else 0
                g["on_mission"] += 1 if t.get("on_mission") else 0
        elif "enriched_at" in t:
            ts = store.parse_iso(t.get("enriched_at"))
            if ts:
                enr[ts] += 1
    return added, gate, enr


def match(runs, added, gate, enr):
    """Assign each timestamp cluster to the run whose window contains it."""
    SLACK = 180
    rows = {id(r): dict(r, added=0, gated=0, skipped=0, impactful=0,
                        uplifting=0, on_mission=0, enriched=0) for r in runs}
    orphans = {}

    def bucket(ts):
        for r in runs:
            if r["start"] - SLACK <= ts <= max(r["end"], r["start"]) + SLACK:
                return rows[id(r)]
        o = orphans.setdefault(ts - ts % 1800, {
            "start": ts, "end": ts, "conclusion": "local", "event": "manual/local",
            "url": "", "added": 0, "gated": 0, "skipped": 0, "impactful": 0,
            "uplifting": 0, "on_mission": 0, "enriched": 0})
        return o

    for ts, n in added.items():
        bucket(ts)["added"] += n
    for ts, g in gate.items():
        b = bucket(ts)
        for k, v in g.items():
            b[k] += v
    for ts, n in enr.items():
        bucket(ts)["enriched"] += n

    merged = list(rows.values()) + list(orphans.values())
    merged.sort(key=lambda r: r["start"], reverse=True)
    return merged[:RUNS_SHOWN]


def totals():
    n_articles = sum(1 for p in store.shards("articles") for _ in p.open())
    stats = {"articles": n_articles, "gate_rows": 0, "v3": 0, "impactful": 0,
             "uplifting": 0, "on_mission": 0, "enriched": 0}
    folded = {}
    floor = time.time() - 8 * 86400
    for t in store.read_jsonl(store.shards("tags")):
        if "good" in t:
            stats["gate_rows"] += 1
            if t.get("v") == 3 and not t.get("skip"):
                stats["v3"] += 1
                for k in ("impactful", "uplifting", "on_mission"):
                    stats[k] += 1 if t.get(k) else 0
        elif "enriched_at" in t:
            stats["enriched"] += 1
        if "id" in t:
            folded.setdefault(t["id"], {}).update(t)
    pending_gate = pending_enrich = 0
    recent_arts = [p for p in store.shards("articles")
                   if store.week_start_epoch(store.shard_key(p, "articles")) + 7 * 86400 >= floor]
    for row in store.read_jsonl(recent_arts):
        if store.parse_iso(row.get("first_seen")) < floor:
            continue
        aid = store.article_id(row["source"], row["title"], row.get("link", ""))
        f = folded.get(aid, {})
        if "good" not in f:
            pending_gate += 1
        elif (f.get("v") == 3 and f.get("impactful") and f.get("uplifting")
              and "virtues" not in f):
            pending_enrich += 1
    stats["pending_gate"], stats["pending_enrich"] = pending_gate, pending_enrich
    return stats


def fmt_ts(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%b %d %H:%M")


def pct(n, d):
    return f"{100 * n // d}%" if d else "-"


def main():
    runs = fetch_runs()
    added, gate, enr = collect_clusters()
    rows = match(runs, added, gate, enr)
    t = totals()
    gen = datetime.now(timezone.utc)

    def status_cell(r):
        icon = {"success": "&#10003;", "failure": "&#10007;", "running": "&#8635;",
                "local": "&#8962;"}.get(r["conclusion"], "?")
        cls = {"success": "ok", "failure": "bad", "running": "run"}.get(r["conclusion"], "loc")
        label = r["conclusion"]
        if r["url"]:
            label = f'<a href="{r["url"]}">{label}</a>'
        return f'<span class="st {cls}">{icon}</span> {label}'

    body_rows = []
    for r in rows:
        dur = max(0, r["end"] - r["start"])
        g = r["gated"]
        body_rows.append(
            "<tr>"
            f"<td>{fmt_ts(r['start'])}</td>"
            f"<td>{status_cell(r)}<span class='ev'> {r['event']}</span></td>"
            f"<td class='num'>{dur // 60}m{dur % 60:02d}s</td>"
            f"<td class='num'>{r['added'] or '-'}</td>"
            f"<td class='num'>{g or '-'}"
            + (f" <span class='ev'>(+{r['skipped']} skip)</span>" if r["skipped"] else "") + "</td>"
            f"<td class='num'>{r['impactful'] if g else '-'}</td>"
            f"<td class='num'>{r['uplifting'] if g else '-'}</td>"
            f"<td class='num'>{r['on_mission'] if g else '-'}</td>"
            f"<td class='num'>{r['enriched'] or '-'}</td>"
            "</tr>")

    cards = [
        ("articles stored", f"{t['articles']:,}"),
        ("classified (all versions)", f"{t['gate_rows']:,}"),
        ("v3 gated", f"{t['v3']:,}"),
        ("impactful", f"{t['impactful']:,} ({pct(t['impactful'], t['v3'])})"),
        ("uplifting", f"{t['uplifting']:,} ({pct(t['uplifting'], t['v3'])})"),
        ("on mission", f"{t['on_mission']:,} ({pct(t['on_mission'], t['v3'])})"),
        ("enriched", f"{t['enriched']:,}"),
        ("pending gate / enrich", f"{t['pending_gate']:,} / {t['pending_enrich']:,}"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{v}</div><div class="l">{k}</div></div>'
        for k, v in cards)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Pipeline Health</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cstyle%3Etext%7Bfill:%23157a46%7D@media(prefers-color-scheme:dark)%7Btext%7Bfill:%234cc98a%7D%7D%3C/style%3E%3Ctext x='8' y='8.5' text-anchor='middle' dominant-baseline='central' font-size='19'%3E%E2%9D%A7%3C/text%3E%3C/svg%3E">
<style>
  :root {{
    --bg: #f4f7f5; --panel: #ffffff; --text: #1c2420; --muted: #5c6b63;
    --border: #dde5e0; --accent: #157a46; --ok: #157a46; --bad: #c23b3b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #101513; --panel: #171d1a; --text: #e4eae6; --muted: #93a29a;
      --border: #2a332e; --accent: #4cc98a; --ok: #4cc98a; --bad: #ff7b7b;
    }}
  }}
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: var(--bg); color: var(--text); padding: 18px 22px; }}
  h1 {{ font-size: 20px; margin-bottom: 2px; }} h1 .leaf {{ color: var(--accent); }}
  .sub {{ color: var(--muted); font-size: 12.5px; margin-bottom: 14px; }}
  .sub a {{ color: var(--accent); font-weight: 600; text-decoration: none; }}
  #stale {{ display: none; background: var(--bad); color: #fff; font-weight: 600;
           padding: 8px 14px; border-radius: 8px; margin-bottom: 14px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
          padding: 10px 16px; min-width: 130px; }}
  .card .k {{ font-size: 17px; font-weight: 700; }}
  .card .l {{ font-size: 11px; color: var(--muted); text-transform: uppercase;
             letter-spacing: .05em; }}
  table {{ border-collapse: collapse; width: 100%; background: var(--panel);
          border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }}
  th {{ text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
       color: var(--muted); padding: 9px 12px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 7px 12px; border-top: 1px solid var(--border); font-size: 13px; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  th.num {{ text-align: right; }}
  .st.ok {{ color: var(--ok); font-weight: 700; }} .st.bad {{ color: var(--bad); font-weight: 700; }}
  .st.run {{ color: var(--muted); }} .st.loc {{ color: var(--muted); }}
  .ev {{ color: var(--muted); font-size: 11.5px; }}
  td a {{ color: inherit; }}
  .wrap {{ overflow-x: auto; }}
</style>
</head>
<body>
<h1><span class="leaf">&#10087;</span> Pipeline Health</h1>
<div class="sub">Generated <span id="genAt" data-ts="{int(gen.timestamp())}">{gen.strftime('%b %d %H:%M UTC')}</span>
 &middot; auto-refreshes every 5 min &middot; times UTC &middot;
 <a href="../">all articles</a> &middot; <a href="../good-news.html">dashboard</a></div>
<div id="stale">&#9888; No successful pipeline run in over 45 minutes &mdash; the cron may be stalled or failing. Check the Actions tab.</div>
<div class="cards">{cards_html}</div>
<div class="wrap">
<table>
<thead><tr><th>run started</th><th>status</th><th class="num">duration</th>
<th class="num">+articles</th><th class="num">gated</th><th class="num">impactful</th>
<th class="num">uplifting</th><th class="num">on mission</th><th class="num">enriched</th></tr></thead>
<tbody>{''.join(body_rows)}</tbody>
</table>
</div>
<script>
"use strict";
const ts = Number(document.getElementById("genAt").dataset.ts) * 1000;
if (Date.now() - ts > 45 * 60 * 1000) document.getElementById("stale").style.display = "block";
</script>
</body>
</html>"""

    out = ROOT / "docs" / "health"
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"Wrote docs/health/index.html: {len(rows)} runs, "
          f"{t['articles']:,} articles, {t['v3']:,} v3-gated")


if __name__ == "__main__":
    main()
